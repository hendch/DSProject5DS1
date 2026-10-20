"""The orchestrator's run lifecycle, persisted in PostgreSQL:

    received ──route──► running ──agents──► waiting_approval ──decided──► executing ──► completed
        └─► needs_triage (no route: a human picks one)        failed (an error the run can't survive)

`advance(run_id)` moves a run as far as it can go and is safe to call any number of
times: from the API, from the event consumer when the guardrail decides, from the
periodic sweep, and on start-up for runs a restart interrupted.
"""
import json
import re
import threading
import traceback
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from agents.orchestrator import adapters, executors, routing, store
from agents.orchestrator.catalog import AGENTS, ROUTES
from shared.alerts import raise_alert
from shared.db import get_connection
from shared.events import Consumer, make_event, publish, publish_and_log
from shared.llm import LLMUnavailable, for_agent

CONSUMER = "orchestrator"


class Orchestrator:
    def __init__(self, workers: int = 4):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run")
        self.locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # ---- public entry points ----------------------------------------------------------
    def start(self, trigger: dict, requested_by: str | None = None, wait: bool = False) -> str:
        """Create a run for a trigger and start it. `trigger`: {title, source, route?, params?, text?,
        sha?, pr?, html_url?}. Returns the run id (also its correlation_id)."""
        run_id = str(uuid.uuid4())
        trigger = {**trigger, "title": trigger.get("title") or "Incident"}
        store.create_run(run_id, trigger.get("route") or "free_text", trigger, requested_by)
        publish(make_event("orchestrator", "run_started", {"title": trigger["title"]}, run_id))
        self.advance(run_id, wait=wait)
        return run_id

    def triage(self, run_id: str, route: str, decided_by: str, wait: bool = False) -> None:
        """A human picks the route of a run the rules and the LLM could not place."""
        run = store.get_run(run_id)
        if run is None:
            raise KeyError("run not found")
        if run["status"] != "needs_triage":
            raise ValueError(f"run is {run['status']}, not waiting for triage")
        if route not in ROUTES:
            raise ValueError(f"unknown route {route!r}")
        trigger = {**run["trigger"], "route": route,
                   "params": {**routing.params_for_text(route, run["trigger"].get("text") or ""),
                              **(run["trigger"].get("params") or {})}}
        store.update_run(run_id, trigger=trigger, route=route, routed_by="human", route_confidence=None,
                         status="received", error=None)
        self._audit(run_id, f"triaged by {decided_by}", {"route": route}, f"route: {route} (human)")
        self.advance(run_id, wait=wait)

    def advance(self, run_id: str, wait: bool = False) -> None:
        future = self.pool.submit(self._advance_safely, str(run_id))
        if wait:
            future.result()

    # ---- the state machine ---------------------------------------------------------------
    def _advance_safely(self, run_id: str) -> None:
        with self.locks[run_id]:
            try:
                self._advance(run_id)
            except Exception as e:  # noqa: BLE001 - a bug or an outage must not lose the run
                traceback.print_exc()
                store.update_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
                raise_alert("critical", "Run failed", f"{type(e).__name__}: {e}", "orchestrator",
                            f"#/runs/{run_id}", f"run-failed:{run_id}")

    def _advance(self, run_id: str) -> None:
        run = store.get_run(run_id)
        if run is None:
            return
        if run["status"] == "received":
            if not self._route(run):
                return
            self._run_agents(store.get_run(run_id))
            run = store.get_run(run_id)
        if run["status"] == "waiting_approval":
            self._sync_decisions(run_id)
            if any(a["decision"] == "pending" for a in store.get_actions(run_id)):
                return
            store.update_run(run_id, status="executing")
            run = store.get_run(run_id)
        if run["status"] == "executing":
            self._execute(run_id)

    def _route(self, run: dict) -> bool:
        trigger = run["trigger"]
        if run.get("routed_by") == "human" and run.get("route"):  # a person already chose: keep it
            choice = {"route": run["route"], "routed_by": "human", "confidence": None, "reason": "picked by a human"}
        else:
            choice = routing.choose_route(trigger)
        if choice["route"] is None:
            store.update_run(run["id"], status="needs_triage", error=choice["reason"])
            self._audit(run["id"], "needs triage", choice, "route: none")
            raise_alert("info", "An incident needs triage", f"{trigger['title']}: {choice['reason']}",
                        "orchestrator", f"#/runs/{run['id']}", f"triage:{run['id']}")
            return False
        params = {**routing.params_for_text(choice["route"], trigger.get("text") or ""), **(trigger.get("params") or {})}
        store.update_run(run["id"], route=choice["route"], routed_by=choice["routed_by"],
                         route_confidence=choice["confidence"], status="running", error=None,
                         trigger={**trigger, "params": params})
        self._audit(run["id"], f"routed by {choice['routed_by']}", choice, f"route: {choice['route']}")
        return True

    def _run_agents(self, run: dict) -> None:
        route = ROUTES[run["route"]]
        params = run["trigger"].get("params") or {}
        ctx = {"run_id": str(run["id"]), "requested_by": run.get("requested_by"), "facts": {},
               "workdir": adapters.new_workdir(run["id"])}
        if route_prepare := adapters.PREPARE.get(run["route"]):
            try:
                route_prepare(params, ctx)
            except Exception as e:  # noqa: BLE001 - e.g. checkout failed: the agents that need it skip
                ctx["facts"]["prepare_error"] = f"{type(e).__name__}: {e}"
                print(f"[orchestrator] preparing {run['route']} failed: {e}")
        published = 0
        for step_no, step in enumerate(route["steps"], 1):
            agent = step["agent"]
            step_id = store.start_step(run["id"], step_no, agent, params)
            if step.get("when") and not adapters.CONDITIONS[step["when"]](ctx["facts"]):
                store.finish_step(step_id, "skipped", {"summary": f"Not needed: no {step['when'].replace('_', ' ')}."})
                continue
            try:
                result = adapters.ADAPTERS[agent](params, ctx)
            except Exception as e:  # noqa: BLE001 - one agent failing doesn't stop the others
                traceback.print_exc()
                store.finish_step(step_id, "failed", {"summary": "The agent crashed."}, f"{type(e).__name__}: {e}")
                raise_alert("warning", f"{AGENTS[agent]['title']} failed", f"{type(e).__name__}: {e}",
                            "orchestrator", f"#/runs/{run['id']}", f"step-failed:{run['id']}:{step_no}")
                continue
            ctx["facts"].update(result.facts)
            for p in result.proposals:
                action_id = publish_and_log(
                    agent, p.action_type, p.payload, str(run["id"]), p.environment, p.blast_radius,
                    category=(p.summary or p.action_type)[:200], input_summary=result.summary[:300])
                store.add_action(action_id, run["id"], step_id, agent, p.action_type)
                published += 1
            store.finish_step(step_id, result.status, {"summary": result.summary, **result.output})
            if result.status == "unavailable":
                raise_alert("warning", f"{AGENTS[agent]['title']} unavailable", result.summary,
                            "orchestrator", f"#/runs/{run['id']}", f"unavailable:{agent}")
        store.update_run(run["id"], status="waiting_approval" if published else "executing")

    def _sync_decisions(self, run_id: str) -> None:
        """Copy the guardrail's decisions (the approvals table is the source of truth) onto
        the run's pending proposals. Expired approvals count as rejected."""
        conn = get_connection(store.USE_SHARED)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""UPDATE orchestrator.run_actions ra
                               SET decision = CASE ap.status WHEN 'approved' THEN 'approved' ELSE 'rejected' END,
                                   decided_by = coalesce(ap.decided_by, 'expired'),
                                   decided_at = coalesce(ap.decided_at, now())
                               FROM approvals ap
                               WHERE ap.action_id = ra.action_id AND ra.run_id = %s
                                 AND ra.decision = 'pending' AND ap.status <> 'pending'""", (run_id,))
        finally:
            conn.close()

    def _execute(self, run_id: str) -> None:
        for action in store.get_actions(run_id):
            if action["execution"] != "not_run":
                continue  # already executed: executors run at most once
            execution, result = executors.execute(action)
            if action["decision"] == "rejected":
                executors.on_rejected(action)
            store.record_execution(action["action_id"], execution, result)
            self._audit(run_id, f"execution: {action['action_type']}", result, f"{execution}: {action['action_type']}")
        run = store.get_run(run_id)
        summary = self._summary(run)
        store.update_run(run_id, status="completed", summary=summary)
        publish(make_event("orchestrator", "run_finished", {"status": "completed", "summary": summary}, run_id))
        failed_steps = [s for s in store.get_steps(run_id) if s["status"] in ("failed", "unavailable")]
        raise_alert("warning" if failed_steps else "success", f"Run finished: {run['trigger']['title']}",
                    summary[:400], "orchestrator", f"#/runs/{run_id}", f"run-done:{run_id}")

    def _summary(self, run: dict) -> str:
        steps, actions = store.get_steps(run["id"]), store.get_actions(run["id"])
        approved = sum(a["decision"] == "approved" for a in actions)
        rejected = sum(a["decision"] == "rejected" for a in actions)
        lines = [f"{AGENTS[s['agent']]['title']}: {(s['output'] or {}).get('summary', s['status'])}" for s in steps]
        facts = (f"{ROUTES[run['route']]['title']} handled by {len(steps)} step(s). {len(actions)} proposal(s): "
                 f"{approved} approved, {rejected} rejected.\n" + "\n".join(lines))
        try:  # a short plain-English recap; the template above is the fallback
            llm = for_agent("orchestrator")
            recap = llm.chat("Summarise this DevOps run for an engineer in at most 2 sentences. Plain text: "
                             "no markdown, no heading, no 'Summary:' label. Facts only, no advice:\n" + facts[:3000],
                             max_tokens=200)
            recap = re.sub(r"^\s*(\*\*)?summary:?(\*\*)?:?\s*", "", recap.replace("**", ""), flags=re.I).strip()
            return recap + "\n\n" + facts
        except LLMUnavailable:
            return facts

    # ---- events, sweep and recovery -----------------------------------------------------------
    def on_event(self, event) -> None:
        """Guardrail decisions wake the run they belong to."""
        if not isinstance(event, dict) or event.get("agent_name") != "guardrail":
            return
        payload = event.get("payload") or {}
        run_id = store.record_decision(payload.get("action_ref"), payload.get("decision", "pending"),
                                       payload.get("decided_by")) if payload.get("decision") in (
            "approved", "rejected") else None
        if event.get("action_type") == "approval_request":
            row = store.get_actions(event.get("correlation_id")) if event.get("correlation_id") else []
            mine = [a for a in row if str(a["action_id"]) == payload.get("action_ref")]
            if mine:
                a = mine[0]
                raise_alert("warning", "Approval needed",
                            f"{AGENTS.get(a['agent'], {}).get('title', a['agent'])} · {a['action_type']} "
                            f"(risk {a.get('risk')}): {payload.get('reason', '')}",
                            "guardrail", f"#/runs/{a['run_id']}", f"approval:{payload.get('approval_id')}")
        if run_id:
            self.advance(run_id)

    def sweep(self) -> None:
        """Advance waiting runs whose decisions arrived while nobody was listening."""
        for run in store.runs_with_status("waiting_approval", "executing", "received"):
            self.advance(str(run["id"]))

    def recover(self) -> None:
        """On start-up: runs a restart interrupted in the middle of their agents can't be
        resumed safely (an agent may have half-run), so they fail visibly; the rest resume."""
        for run in store.runs_with_status("running"):
            store.update_run(run["id"], status="failed", error="Interrupted by a restart while its agents were running.")
            raise_alert("warning", "Run interrupted", f"{run['trigger'].get('title')}: the app restarted mid-run.",
                        "orchestrator", f"#/runs/{run['id']}", f"interrupted:{run['id']}")
        self.sweep()

    def consumer(self) -> Consumer:
        return Consumer(CONSUMER, self.on_event, use_shared=store.USE_SHARED)

    # ---- helpers -------------------------------------------------------------------------
    @staticmethod
    def _audit(run_id: str, summary: str, trace, decision: str) -> None:
        conn = get_connection(store.USE_SHARED)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO reasoning_logs (correlation_id, agent_name, input_summary,
                               reasoning_trace, decision) VALUES (%s, 'orchestrator', %s, %s, %s)""",
                            (run_id, summary[:300], json.dumps(trace, default=str), decision[:200]))
        finally:
            conn.close()
