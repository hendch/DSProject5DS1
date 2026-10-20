"""The dashboard's API (FastAPI) and the static front end in web/.

Everything the UI shows comes from here: runs, approvals, GitHub activity, alerts,
analytics, agents and the audit log. `create_app(orchestrator)` wires it to a running
orchestrator; app/__main__.py starts the whole platform.
"""
import shutil
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.guardrail import policy_client
from agents.guardrail import store as guardrail_store
from agents.guardrail.announce import announce
from agents.orchestrator import analytics, github, store
from agents.orchestrator.catalog import AGENTS, ROUTES, SCENARIOS, SCENARIOS_BY_ID
from shared import alerts, knowledge
from shared.events import list_events
from shared.llm import for_agent

WEB = Path(__file__).resolve().parents[1] / "web"


# ---- request bodies -----------------------------------------------------------------------
class NewRun(BaseModel):
    scenario_id: str | None = None
    text: str | None = Field(None, max_length=20000)
    trigger_type: str | None = None
    params: dict | None = None
    requested_by: str | None = Field(None, max_length=100)


class Vote(BaseModel):
    decided_by: str = Field(min_length=1, max_length=100)


class Triage(BaseModel):
    route: str
    decided_by: str = Field(min_length=1, max_length=100)


class Connect(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    token: str = Field(min_length=8, max_length=400)


class TokenBody(BaseModel):
    token: str = Field(min_length=8, max_length=400)


class ClientIdBody(BaseModel):
    client_id: str = Field(min_length=10, max_length=60)


class RepoBody(BaseModel):
    repo: str = Field(min_length=3, max_length=200)


class GitHubSettings(BaseModel):
    auto_investigate: bool | None = None
    poll_seconds: int | None = Field(None, ge=20, le=3600)


class Investigate(BaseModel):
    kind: str = Field(pattern="^(commit|pull_request)$")
    sha: str | None = None
    number: int | None = None
    repo: str | None = None


class ReadAlerts(BaseModel):
    ids: list[int] | str


# ---- serialisers --------------------------------------------------------------------------
def run_view(r: dict) -> dict:
    t = r.get("trigger") or {}
    return {"id": str(r["id"]), "title": t.get("title"), "trigger_type": r["trigger_type"], "route": r.get("route"),
            "route_title": ROUTES.get(r.get("route") or "", {}).get("title"), "routed_by": r.get("routed_by"),
            "route_confidence": r.get("route_confidence"), "status": r["status"], "summary": r.get("summary"),
            "error": r.get("error"), "agents": r.get("agents") or [], "pending": int(r.get("pending") or 0),
            "created_at": r["created_at"], "finished_at": r.get("finished_at"), "text": t.get("text"),
            "source": t.get("source", "scenario"), "repo": (t.get("params") or {}).get("repo"),
            "requested_by": r.get("requested_by"), "trigger": {k: v for k, v in t.items() if k != "params"}}


def action_view(a: dict) -> dict:
    approval = None
    if a.get("approval_id"):
        approval = {"id": str(a["approval_id"]), "status": a["approval_status"], "blast_radius": a["declared_risk"],
                    "risk": a["risk"], "policy_rule": a["policy_rule"], "reason": a["reason"],
                    "llm_summary": a["llm_summary"], "required_approvals": a["required_approvals"],
                    "approved_by": a["approved_by"] or [], "requested_by": a["requested_by"],
                    "expires_at": a["expires_at"]}
    return {"action_id": str(a["action_id"]), "agent": a["agent"], "action_type": a["action_type"],
            "environment": a["environment"], "payload": a["payload"], "decision": a["decision"],
            "decided_by": a["decided_by"], "execution": a["execution"], "execution_result": a["execution_result"],
            "approval": approval}


def event_text(e: dict) -> tuple[str, str]:
    p = e.get("payload") or {}
    actor = e.get("agent_name", "?")
    if e.get("action_type") == "approval_request":
        return actor, f"held for a human: {p.get('rule')} ({e.get('blast_radius')} risk)"
    if e.get("action_type") == "approval_result":
        who = f" by {p['decided_by']}" if p.get("decided_by") else ""
        return actor, f"{p.get('decision')}{who}" + (f" · {p['rule']}" if p.get("rule") else "")
    if e.get("action_type") == "run_started":
        return actor, "run started"
    if e.get("action_type") == "run_finished":
        return actor, "run finished"
    return actor, f"proposed {e.get('action_type')}"


# ---- health checks (cached: some call the network) -----------------------------------------
_checks: dict[str, tuple[float, object]] = {}


def cached(key: str, seconds: float, fn):
    hit = _checks.get(key)
    if hit and time.monotonic() - hit[0] < seconds:
        return hit[1]
    try:
        value = fn()
    except Exception as e:  # noqa: BLE001
        value = e
    _checks[key] = (time.monotonic(), value)
    return value


def opa_ok() -> bool:
    return cached("opa", 10, lambda: httpx.get(f"{policy_client.OPA_URL}/health", timeout=2).status_code == 200) is True


def db_ok() -> bool:
    return cached("db", 5, lambda: store.run_counts() is not None) is True


def audit_intact() -> bool:
    return cached("audit", 15, lambda: not store.broken_chain_rows()) is True


def llm_check(agent: str) -> tuple[bool, str]:
    result = cached(f"llm:{agent}", 60, lambda: for_agent(agent).check())
    return result if isinstance(result, tuple) else (False, str(result))


def agent_view(name: str) -> dict:
    meta, llm = AGENTS[name], for_agent(name)
    ok, err = llm_check(name)
    status, reasons = "available", []
    if not ok:
        status = "degraded"
        reasons.append("LLM unreachable: falls back to rules" if name != "code_reviewer"
                       else "LLM unreachable: real reviews need it (the demo uses a scripted review)")
    if name == "dependency_resolver" and not (shutil.which("tofu") or shutil.which("terraform")):
        status = "degraded" if status == "available" else status
        reasons.append("OpenTofu not installed: static analysis of .tf files")
    if name == "diagnostic_rca":
        n = cached("kb", 30, lambda: knowledge.count())
        if isinstance(n, int) and n == 0:
            status = "degraded" if status == "available" else status
            reasons.append("knowledge base empty: no documents to cite")
    if name == "drift_detection":
        try:
            import agents.drift_detection.data  # noqa: F401
        except ModuleNotFoundError:
            status, reasons = "unavailable", ["data package not pushed yet (agents/drift_detection/data)"]
    return {"name": name, "title": meta["title"], "owner": meta["owner"], "description": meta["description"],
            "actions": meta["actions"], "model": llm.label, "status": status,
            "reason": "; ".join(reasons) or "ready"}


# ---- the app ------------------------------------------------------------------------------
def create_app(orchestrator) -> FastAPI:
    app = FastAPI(title="OpsAgents", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def gh_error(e: github.GitHubError):
        raise HTTPException(e.status if 400 <= e.status < 600 else 502, str(e))

    @app.get("/api/health")
    def health():
        return {"status": "ok", "database": db_ok(), "policy_engine": opa_ok()}

    # -- overview, scenarios, routes ------------------------------------------------------
    @app.get("/api/overview")
    def overview():
        gh = github.status()
        head = github.head() if gh["connected"] else None
        llm_ok, _ = llm_check("default")
        return {"runs": store.run_counts(), "pending_approvals": len(store.pending_approvals()),
                "audit_intact": audit_intact(), "alerts_unread": alerts.unread_count(),
                "llm": {"label": for_agent().label, "ok": llm_ok},
                "github": {"connected": gh["connected"], "signed_in": gh["signed_in"], "repo": gh["repo"],
                           "repos": gh["repos"], "last_sync": gh["last_sync"], "login": gh["login"],
                           "head": {"short_sha": head["sha"][:7], "message": head["title"], "ci": head["ci"],
                                    "html_url": head["html_url"], "time": head["time"]} if head else None,
                           **github.counts()},
                "system": {"database": db_ok(), "policy_engine": opa_ok(), "audit_intact": audit_intact()}}

    @app.get("/api/scenarios")
    def scenarios():
        return [{"id": s["id"], "title": s["title"], "description": s["description"], "trigger_type": s["route"],
                 "agents": [st["agent"] for st in ROUTES[s["route"]]["steps"]]} for s in SCENARIOS]

    @app.get("/api/routes")
    def routes():
        return [{"id": rid, "title": r["title"], "description": r["description"],
                 "agents": [s["agent"] for s in r["steps"]]} for rid, r in ROUTES.items()]

    # -- runs -----------------------------------------------------------------------------
    @app.post("/api/runs")
    def new_run(body: NewRun):
        if body.scenario_id:
            s = SCENARIOS_BY_ID.get(body.scenario_id)
            if not s:
                raise HTTPException(404, "unknown scenario")
            trigger = {"title": s["title"], "source": "scenario", "route": s["route"], "params": dict(s["params"])}
        elif body.text and body.text.strip():
            text = body.text.strip()
            trigger = {"title": text.splitlines()[0][:80], "source": "text", "text": text}
        elif body.trigger_type:
            if body.trigger_type not in ROUTES:
                raise HTTPException(400, f"unknown trigger type {body.trigger_type!r}")
            trigger = {"title": ROUTES[body.trigger_type]["title"], "source": "api", "route": body.trigger_type,
                       "params": body.params or {}}
        else:
            raise HTTPException(400, "give a scenario_id, some incident text, or a trigger_type")
        return {"run_id": orchestrator.start(trigger, requested_by=body.requested_by)}

    @app.get("/api/runs")
    def runs(limit: int = Query(50, ge=1, le=500), status: str | None = None, repo: str | None = None):
        rows = [run_view(r) for r in store.list_runs(limit, status)]
        return [r for r in rows if not repo or r["repo"] == repo]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: uuid.UUID):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        steps = store.get_steps(run_id)
        run["agents"] = [s["agent"] for s in steps]
        actions = store.get_actions(run_id)
        run["pending"] = sum(a["decision"] == "pending" for a in actions)
        timeline = []
        for e in list_events(run_id, limit=300):
            actor, text = event_text(e["event"])
            timeline.append({"time": e["created_at"], "kind": "event", "actor": actor, "text": text})
        for a in store.audit_for(run_id):
            timeline.append({"time": a["created_at"], "kind": "audit", "actor": a["agent_name"],
                             "text": " · ".join(x for x in (a["decision"], a["input_summary"]) if x)})
        timeline.sort(key=lambda t: t["time"])
        return {"run": run_view(run), "actions": [action_view(a) for a in actions], "timeline": timeline,
                "steps": [{"step_no": s["step_no"], "agent": s["agent"], "status": s["status"],
                           "summary": (s["output"] or {}).get("summary"), "output": s["output"], "error": s["error"],
                           "started_at": s["started_at"], "finished_at": s["finished_at"]} for s in steps]}

    @app.post("/api/runs/{run_id}/triage")
    def triage(run_id: uuid.UUID, body: Triage):
        try:
            orchestrator.triage(str(run_id), body.route, body.decided_by)
        except KeyError:
            raise HTTPException(404, "run not found") from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {"ok": True}

    # -- approvals ---------------------------------------------------------------------------
    @app.get("/api/approvals")
    def approvals(repo: str | None = None):
        rows = store.pending_approvals()
        return [{**r, "id": str(r["id"]), "run_id": str(r["run_id"]) if r["run_id"] else None} for r in rows]

    def vote(approval_id: uuid.UUID, body: Vote, kind: str):
        try:
            result = guardrail_store.cast_vote(str(approval_id), body.decided_by, kind)
        except guardrail_store.ApprovalError as e:
            raise HTTPException(e.http_status, str(e)) from None
        if result["status"] != "pending":
            announce(result["correlation_id"], result["environment"], result["risk"], result["action_ref"],
                     result["status"], result["decided_by"], approval_id=result["approval_id"],
                     approvals=result["approvals"])
            orchestrator.advance(result["correlation_id"])
        return result

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(approval_id: uuid.UUID, body: Vote):
        return vote(approval_id, body, "approve")

    @app.post("/api/approvals/{approval_id}/reject")
    def reject(approval_id: uuid.UUID, body: Vote):
        return vote(approval_id, body, "reject")

    # -- agents, audit, alerts, analytics -------------------------------------------------------
    @app.get("/api/agents")
    def agents():
        return [agent_view(n) for n in AGENTS]

    @app.get("/api/audit")
    def audit(limit: int = Query(100, ge=1, le=1000)):
        broken = store.broken_chain_rows()
        return {"intact": not broken, "broken_rows": broken, "rows": store.audit_rows(limit)}

    @app.get("/api/alerts")
    def list_alerts(limit: int = Query(100, ge=1, le=500)):
        return alerts.list_alerts(limit)

    @app.post("/api/alerts/read")
    def read_alerts(body: ReadAlerts):
        if body.ids != "all" and not isinstance(body.ids, list):
            raise HTTPException(400, 'ids must be a list or "all"')
        return {"marked": alerts.mark_read(body.ids)}

    @app.get("/api/analytics")
    def get_analytics(days: int = Query(30, ge=1, le=365), repo: str | None = None):
        return analytics.compute(days, repo)

    # -- GitHub ---------------------------------------------------------------------------
    @app.get("/api/github/status")
    def gh_status():
        return github.status()

    @app.post("/api/github/connect")
    def gh_connect(body: Connect):
        try:
            return github.connect(body.repo, body.token)
        except github.GitHubError as e:
            gh_error(e)

    @app.post("/api/github/token")
    def gh_token(body: TokenBody):
        """Sign in by pasting a personal access token (alternative to the browser sign-in)."""
        try:
            return github.sign_in(body.token.strip(), auth="token")
        except github.GitHubError as e:
            gh_error(e)

    @app.post("/api/github/oauth/client")
    def gh_oauth_client(body: ClientIdBody):
        try:
            github.set_client_id(body.client_id)
        except github.GitHubError as e:
            gh_error(e)
        return github.status()

    @app.post("/api/github/oauth/start")
    def gh_oauth_start():
        try:
            return github.device_start()
        except github.GitHubError as e:
            gh_error(e)

    @app.post("/api/github/oauth/poll")
    def gh_oauth_poll():
        try:
            return github.device_poll()
        except github.GitHubError as e:
            gh_error(e)

    @app.get("/api/github/available-repos")
    def gh_available():
        try:
            return github.available_repos()
        except github.GitHubError as e:
            gh_error(e)

    @app.post("/api/github/disconnect")
    def gh_disconnect():
        github.disconnect()
        return github.status()

    @app.post("/api/github/settings")
    def gh_settings(body: GitHubSettings):
        try:
            return github.update_settings(auto_investigate=body.auto_investigate, poll_seconds=body.poll_seconds)
        except github.GitHubError as e:
            gh_error(e)

    @app.post("/api/github/sync")
    def gh_sync():
        return github.sync(orchestrator=orchestrator)

    @app.get("/api/github/repos")
    def gh_repos():
        return [github.repo_summary(r) for r in github.repos()]

    @app.post("/api/github/repos")
    def gh_add_repo(body: RepoBody):
        try:
            return github.add_repo(body.repo)
        except github.GitHubError as e:
            gh_error(e)

    @app.delete("/api/github/repos/{owner}/{name}")
    def gh_remove_repo(owner: str, name: str):
        try:
            github.remove_repo(f"{owner}/{name}")
        except github.GitHubError as e:
            gh_error(e)
        return {"ok": True}

    @app.get("/api/github/activity")
    def gh_activity(kind: str = "all", branch: str | None = None, failing: bool = False, repo: str | None = None):
        kinds = {"commits": "commit", "pulls": "pull_request"}
        items = github.list_items(kinds.get(kind, kind), branch, failing, repo=repo)
        return {"connected": github.status()["connected"], "branches": github.branches(repo),
                "items": [{"kind": i["kind"], "repo": i["repo"], "sha": i["sha"],
                           "short_sha": (i["sha"] or "")[:7],
                           "number": int(i["key"]) if i["kind"] == "pull_request" else None,
                           "title": i["title"], "message": i["message"], "author": i["author"],
                           "avatar_url": i["avatar_url"], "branch": i["branch"], "state": i["state"],
                           "ci": i["ci"], "run_url": i["run_url"], "html_url": i["html_url"], "time": i["time"],
                           "run_id": str(i["run_id"]) if i["run_id"] else None} for i in items]}

    @app.post("/api/github/investigate")
    def gh_investigate(body: Investigate):
        key = body.sha if body.kind == "commit" else body.number
        if key is None:
            raise HTTPException(400, "give the commit sha or the pull request number")
        try:
            return {"run_id": github.investigate(body.kind, key, orchestrator, requested_by=None, repo=body.repo)}
        except github.GitHubError as e:
            gh_error(e)

    # -- front end ----------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def unexpected(request, exc):  # never leak a stack trace to the browser
        print(f"[api] {request.method} {request.url.path}: {type(exc).__name__}: {exc}")
        return JSONResponse({"detail": "Something went wrong on the server; see its console."}, status_code=500)

    if (WEB / "index.html").exists():
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB / "index.html", headers=NO_CACHE)
        app.mount("/", FreshStaticFiles(directory=WEB), name="web")
    return app


# The dashboard is small: always revalidate it, so an update is never hidden by a stale cache.
NO_CACHE = {"Cache-Control": "no-cache"}


class FreshStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
