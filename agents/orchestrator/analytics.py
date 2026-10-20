"""Numbers for the dashboard's analytics pages, computed from PostgreSQL on request."""
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from agents.orchestrator.catalog import AGENTS
from shared.db import get_connection
from shared.llm import profile

# Rough minutes a human would spend on the same task, for the "time saved" estimate.
MINUTES_SAVED = {"review_code": 20, "rank_causes": 25, "propose_fix": 30, "flag_conflict": 15,
                 "drift_report": 20, "auto_approved": 2}


def _q(cur, sql, params):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def compute(days: int = 30, repo: str | None = None) -> dict:
    days = max(1, min(int(days or 30), 365))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    conn = get_connection()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            p = {"start": start, "repo": repo}
            run_filter = "r.created_at >= %(start)s AND (%(repo)s::text IS NULL OR r.trigger->'params'->>'repo' = %(repo)s)"
            runs = _q(cur, f"""SELECT r.status, r.route, r.created_at, r.finished_at FROM orchestrator.runs r
                                WHERE {run_filter}""", p)
            steps = _q(cur, f"""SELECT s.agent, s.status FROM orchestrator.run_steps s
                                 JOIN orchestrator.runs r ON r.id = s.run_id WHERE {run_filter}""", p)
            actions = _q(cur, """SELECT a.agent_name, a.action_type, a.payload, a.created_at, ap.status AS approval,
                                        ap.decided_by, ap.decided_at, ap.policy_rule
                                 FROM actions a LEFT JOIN approvals ap ON ap.action_id = a.id
                                 WHERE a.created_at >= %(start)s""", p)
            ci_items = _q(cur, """SELECT repo, branch, ci, time FROM github.items
                                  WHERE kind = 'commit' AND time >= %(start)s AND ci IN ('success', 'failure')
                                    AND (%(repo)s::text IS NULL OR repo = %(repo)s) ORDER BY time""", p)
            repos_active = _q(cur, """SELECT count(DISTINCT repo) AS n FROM github.items
                                      WHERE updated_at >= %(start)s""", p)[0]["n"]
    finally:
        conn.close()

    by_day: dict[str, dict] = {}
    for i in range(days):
        d = (start + timedelta(days=i + 1)).date().isoformat()
        by_day[d] = {"date": d, "completed": 0, "failed": 0, "waiting": 0, "other": 0}
    for r in runs:
        d = r["created_at"].date().isoformat()
        if d in by_day:
            key = {"completed": "completed", "failed": "failed", "waiting_approval": "waiting",
                   "needs_triage": "waiting"}.get(r["status"], "other")
            by_day[d][key] += 1

    auto = [a for a in actions if a["decided_by"] == "guardrail:policy" and a["approval"] == "approved"]
    held = [a for a in actions if a["approval"] and a["decided_by"] != "guardrail:policy" and a["approval"] != "rejected"]
    rejected = [a for a in actions if a["approval"] in ("rejected", "expired")]
    by_agent, by_rule = {}, {}
    for a in actions:
        by_agent[a["agent_name"]] = by_agent.get(a["agent_name"], 0) + 1
        if a["policy_rule"]:
            by_rule[a["policy_rule"]] = by_rule.get(a["policy_rule"], 0) + 1

    def seconds(a):
        return (a["decided_at"] - a["created_at"]).total_seconds() if a["decided_at"] else None

    reviews = [a for a in actions if a["action_type"] == "review_code"]
    severity, category, findings_per_pr = {"high": 0, "medium": 0, "low": 0}, {}, []
    for a in reviews:
        findings = (a["payload"] or {}).get("findings") or []
        findings_per_pr.append(len(findings))
        for f in findings:
            severity[f.get("severity", "low")] = severity.get(f.get("severity", "low"), 0) + 1
            category[f.get("category", "other")] = category.get(f.get("category", "other"), 0) + 1

    causes = {}
    for a in actions:
        if a["action_type"] == "rank_causes":
            ranked = (a["payload"] or {}).get("ranked_causes") or []
            if ranked:
                c = ranked[0]["cause"][:120]
                causes[c] = causes.get(c, 0) + 1

    ci_days: dict[str, dict] = {}
    recoveries, failing_since = [], {}
    for item in ci_items:
        d = item["time"].date().isoformat()
        ci_days.setdefault(d, {"date": d, "success": 0, "failure": 0})[item["ci"]] += 1
        branch = (item["repo"], item["branch"])
        if item["ci"] == "failure":
            failing_since.setdefault(branch, item["time"])
        elif branch in failing_since:
            recoveries.append((item["time"] - failing_since.pop(branch)).total_seconds())
    passed = sum(1 for i in ci_items if i["ci"] == "success")

    agent_stats = []
    for name in AGENTS:
        mine = [s for s in steps if s["agent"] == name]
        props = [a for a in actions if a["agent_name"] == name]
        decided = [a for a in props if a["approval"] in ("approved", "rejected", "expired")]
        agent_stats.append({
            "name": name, "runs": len(mine), "model": profile(name).label,
            **{k: sum(1 for s in mine if s["status"] == k) for k in ("done", "degraded", "failed", "unavailable", "skipped")},
            "proposals": len(props),
            "approval_rate": round(sum(a["approval"] == "approved" for a in decided) / len(decided), 2) if decided else None,
            "fallback": sum(1 for s in mine if s["status"] == "degraded"),
            "llm": sum(1 for s in mine if s["status"] == "done")})

    durations: dict[str, list] = {}
    for r in runs:
        if r["status"] == "completed" and r["finished_at"] and r["route"]:
            durations.setdefault(r["route"], []).append((r["finished_at"] - r["created_at"]).total_seconds())

    saved = sum(MINUTES_SAVED.get(a["action_type"], 0) for a in actions) + MINUTES_SAVED["auto_approved"] * len(auto)
    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "days": days},
        "repos_active": repos_active,
        "runs": {"total": len(runs), "completed": sum(r["status"] == "completed" for r in runs),
                 "failed": sum(r["status"] == "failed" for r in runs), "by_day": list(by_day.values())},
        "proposals": {"total": len(actions), "auto_approved": len(auto), "held": len(held),
                      "rejected": len(rejected), "by_agent": by_agent},
        "decisions": {"median_seconds_auto": _median([seconds(a) for a in auto]),
                      "median_seconds_human": _median([seconds(a) for a in held if a["approval"] == "approved"]),
                      "by_rule": by_rule, "llm_escalations": by_rule.get("llm_escalation", 0)},
        "reviews": {"prs": len(reviews),
                    "avg_findings": round(sum(findings_per_pr) / len(findings_per_pr), 1) if findings_per_pr else None,
                    "by_severity": severity, "by_category": category},
        "ci": {"pass_rate": round(passed / len(ci_items), 3) if ci_items else None,
               "by_day": sorted(ci_days.values(), key=lambda d: d["date"]),
               "mttr_seconds": _median(recoveries)},
        "time_saved_minutes": saved,
        "root_causes": [{"cause": c, "count": n} for c, n in sorted(causes.items(), key=lambda kv: -kv[1])[:5]],
        "agents": agent_stats,
        "route_durations": [{"route": r, "median_seconds": _median(v)} for r, v in sorted(durations.items())],
    }
