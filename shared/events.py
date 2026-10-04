import json, uuid
from datetime import datetime, timezone
from shared.db import get_connection
from shared.redis_client import get_redis

def publish_and_log(agent_name, action_type, payload, correlation_id,
                    environment="dev", blast_radius="low",
                    category=None, input_summary=None, use_shared=False):
    action_id = str(uuid.uuid4())
    event = {
        "event_id": action_id, "correlation_id": correlation_id,
        "agent_name": agent_name, "action_type": action_type,
        "environment": environment, "blast_radius": blast_radius,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    get_redis().xadd("pipeline_events", {"data": json.dumps(event)})

    conn = get_connection(use_shared)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO actions (id, correlation_id, agent_name, action_type,
                   environment, blast_radius, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (action_id, correlation_id, agent_name, action_type,
                 environment, blast_radius, json.dumps(payload)),
            )
            cur.execute(
                """INSERT INTO reasoning_logs (correlation_id, agent_name, input_summary, decision)
                   VALUES (%s,%s,%s,%s)""",
                (correlation_id, agent_name, input_summary, category),
            )
        conn.commit()
    finally:
        conn.close()
    return action_id