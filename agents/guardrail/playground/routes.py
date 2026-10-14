"""Dev-only endpoints behind the playground page: publish test proposals,
watch the stream and the audit log, and simulate failures. Never mount
these in a real deployment."""
import json
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.guardrail import agent, llm_client, llm_judge, policy_client, store
from agents.guardrail.playground.scenarios import BY_ID, SCENARIOS
from shared.events import publish_and_log
from shared.redis_client import get_redis

router = APIRouter(prefix="/playground", tags=["playground (dev only)"])
DEAD_OPA = "http://127.0.0.1:9"  # nothing listens here: simulates a policy-engine outage
state = {"real_opa_url": policy_client.OPA_URL, "tampered": None, "consumer": None}


def _publish(event: dict) -> dict:
    event_id = publish_and_log(
        agent_name=event["agent_name"], action_type=event["action_type"], payload=event["payload"],
        correlation_id=str(uuid.uuid4()), environment=event["environment"],
        blast_radius=event["blast_radius"], input_summary=f"playground: {event['action_type']}")
    return {"event_id": event_id}


@router.get("/scenarios")
def scenarios():
    return SCENARIOS


@router.post("/publish/{scenario_id}")
def publish_scenario(scenario_id: str):
    s = BY_ID.get(scenario_id)
    if s is None:
        raise HTTPException(404, "unknown scenario")
    if "raw" in s:  # deliberately broken: bypass publish_and_log
        get_redis().xadd(agent.STREAM, {"data": s["raw"]})
        return {"event_id": None}
    return _publish(s["event"])


class CustomEvent(BaseModel):
    agent_name: str
    action_type: str
    environment: str
    blast_radius: str
    payload: dict


@router.post("/publish-custom")
def publish_custom(event: CustomEvent):
    try:
        return _publish(event.model_dump())
    except Exception as e:  # noqa: BLE001 - e.g. a value the DB rejects
        raise HTTPException(400, f"{type(e).__name__}: {e}") from None


@router.get("/events")
def events(limit: int = 60):
    out = []
    for msg_id, fields in get_redis().xrevrange(agent.STREAM, count=limit):
        raw = fields.get(b"data", b"").decode(errors="replace")
        try:
            out.append({"id": msg_id.decode(), "event": json.loads(raw)})
        except json.JSONDecodeError:
            out.append({"id": msg_id.decode(), "raw": raw})
    return out


@router.get("/approvals")
def approvals():
    return list(reversed(store._query(store.PENDING_SQL.format(where="true"), ())))


@router.get("/trace/{action_id}")
def trace(action_id: uuid.UUID):
    """The guardrail's reasoning for one action: signals, policy verdict, LLM review."""
    return store._query(
        """SELECT id, created_at, decision, reasoning_trace FROM reasoning_logs
           WHERE agent_name = 'guardrail' AND reasoning_trace->>'action_ref' = %s ORDER BY id""",
        (str(action_id),))


@router.get("/audit")
def audit(limit: int = 40):
    rows = store._query(
        """SELECT id, created_at, agent_name, decision, input_summary, prev_hash, row_hash
           FROM reasoning_logs ORDER BY id DESC LIMIT %s""", (limit,))
    broken = {b["id"]: b["problem"] for b in store.verify_chain()}
    for r in rows:
        r["problem"] = broken.get(r["id"])
    return {"intact": not broken, "rows": rows, "tampered": state["tampered"]}


def _admin_sql(query: str, params=()):
    """What a DB admin could do: switch the append-only protection off for one statement."""
    conn = store.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE reasoning_logs DISABLE TRIGGER USER")
            cur.execute(query, params)
            cur.execute("ALTER TABLE reasoning_logs ENABLE TRIGGER USER")
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


@router.post("/tamper")
def tamper():
    """Quietly rewrite the most recent human decision (or any guardrail row)."""
    if state["tampered"]:
        raise HTTPException(409, "already tampered: undo first")
    rows = store._query(
        """SELECT id, decision FROM reasoning_logs WHERE agent_name = 'guardrail'
           ORDER BY (decision LIKE 'approved by%%' OR decision LIKE 'rejected by%%') DESC, id DESC LIMIT 1""", ())
    if not rows:
        raise HTTPException(409, "nothing to tamper with yet: send a proposal first")
    row = rows[0]
    forged = "approved by nobody-saw-this"
    _admin_sql("UPDATE reasoning_logs SET decision = %s WHERE id = %s", (forged, row["id"]))
    state["tampered"] = {"id": row["id"], "original": row["decision"], "forged": forged}
    return state["tampered"]


@router.post("/untamper")
def untamper():
    t = state["tampered"]
    if not t:
        raise HTTPException(409, "nothing to undo")
    _admin_sql("UPDATE reasoning_logs SET decision = %s WHERE id = %s", (t["original"], t["id"]))
    state["tampered"] = None
    return {"restored": t["id"]}


@router.post("/expire/{approval_id}")
def expire_now(approval_id: uuid.UUID):
    """Fast-forward the clock for one pending approval, then run the expiry sweep."""
    conn = store.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""UPDATE approvals SET expires_at = now() - interval '1 second'
                           WHERE id = %s AND status = 'pending'""", (str(approval_id),))
    finally:
        conn.close()
    agent.expire_and_announce()
    return {"expired": str(approval_id)}


class Toggles(BaseModel):
    opa_outage: bool | None = None
    llm: bool | None = None


@router.post("/toggles")
def toggles(t: Toggles):
    if t.opa_outage is not None:
        policy_client.OPA_URL = DEAD_OPA if t.opa_outage else state["real_opa_url"]
    if t.llm is not None:
        llm_judge.ENABLED = t.llm
    return status()


def _up(fn) -> bool:
    try:
        fn()
        return True
    except Exception:  # noqa: BLE001
        return False


_llm_check = {"at": 0.0, "result": (False, "")}


def llm_status() -> tuple[bool, str]:
    """Cached for 60 s: for hosted APIs this is a real HTTP call (no tokens spent)."""
    import time
    if time.monotonic() - _llm_check["at"] > 60:
        _llm_check.update(at=time.monotonic(), result=llm_client.check())
    return _llm_check["result"]


@router.get("/status")
def status():
    llm_ok, llm_error = llm_status()
    consumer = state["consumer"]
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "postgres": _up(lambda: store._query("SELECT 1", ())),
        "redis": _up(lambda: get_redis().ping()),
        "opa": _up(lambda: httpx.get(f"{state['real_opa_url']}/health", timeout=0.5).raise_for_status()),
        "opa_outage": policy_client.OPA_URL == DEAD_OPA,
        "llm_ok": llm_ok, "llm_error": llm_error, "llm_label": llm_client.label(),
        "llm_enabled": llm_judge.ENABLED,
        "consumer": bool(consumer and consumer.is_alive()),
    }
