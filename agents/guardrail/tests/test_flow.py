"""End to end: agents publish -> guardrail decides with the REAL OPA policy
-> a human approves through the API -> the Orchestrator sees the result.

Needs an `opa` binary (on PATH or OPA_BIN) and Postgres (see support.py).
The LLM judge is switched off here; it is covered by test_decide.py.
"""
import os
import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import psycopg2
from fastapi.testclient import TestClient

from agents.guardrail import agent, api, llm_judge, policy_client
from agents.guardrail.tests.support import DbTestCase

POLICY_DIR = Path(__file__).resolve().parents[1] / "policy"
_opa = {}


def setUpModule():
    binary = os.getenv("OPA_BIN") or shutil.which("opa")
    if not binary:
        raise unittest.SkipTest("no opa binary: set OPA_BIN or put opa on PATH")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _opa["proc"] = subprocess.Popen([binary, "run", "--server", f"--addr=127.0.0.1:{port}",
                                     str(POLICY_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _opa["url"] = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(f"{_opa['url']}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError("OPA did not start")


def tearDownModule():
    if "proc" in _opa:
        _opa["proc"].terminate()
        _opa["proc"].wait()


class Flow(DbTestCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(policy_client, "OPA_URL", _opa["url"]),
                  mock.patch.object(llm_judge, "ENABLED", False)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(api.app)

    def results(self):
        return {e["payload"]["action_ref"]: e for e in self.stream()
                if e["agent_name"] == "guardrail"}

    def test_friday_evening_scenario(self):
        clean = self.publish()  # dev/low, nothing sensitive
        iam = self.publish(payload={"triggered_by": "sami",
                                    "cycle": ["aws_iam_role.checkout", "aws_eks_cluster.checkout"]})
        rca = self.publish(agent_name="diagnostic_rca", action_type="rank_causes", environment="prod",
                           payload={"failure_summary": "tests failed", "ranked_causes": []})
        prod = self.publish(environment="prod", blast_radius="high")
        sneaky = self.publish(agent_name="merge_assistant", action_type="propose_fix")

        agent.run(once=True)
        out = self.results()
        self.assertEqual(out[clean["event_id"]]["payload"]["decision"], "approved")
        self.assertEqual(out[clean["event_id"]]["payload"]["rule"], "low_risk_auto")
        self.assertEqual(out[rca["event_id"]]["payload"]["rule"], "advisory_only")
        self.assertEqual(out[sneaky["event_id"]]["payload"]["decision"], "rejected")
        self.assertEqual(out[iam["event_id"]]["action_type"], "approval_request")
        self.assertEqual(out[iam["event_id"]]["blast_radius"], "high")  # raised from "low"
        self.assertEqual(out[prod["event_id"]]["payload"]["required_approvals"], 2)

        # Every agent event was acknowledged; nothing is left pending in the group.
        self.assertEqual(self.redis.xpending("pipeline_events", "guardrail")["pending"], 0)
        # Running again (e.g. after a restart) decides nothing twice.
        agent.run(once=True)
        self.assertEqual(len(self.results()), 5)

        pending = self.client.get("/pending-approvals").json()
        self.assertEqual({p["action_id"] for p in pending}, {iam["event_id"], prod["event_id"]})
        iam_id = out[iam["event_id"]]["payload"]["approval_id"]

        self.assertEqual(self.client.post(f"/approve/{iam_id}", json={"decided_by": "sami"}).status_code, 403)
        r = self.client.post(f"/approve/{iam_id}", json={"decided_by": "ines"})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "approved"))
        self.assertEqual(self.client.post(f"/approve/{iam_id}", json={"decided_by": "karim"}).status_code, 409)

        final = [e for e in self.stream() if e["action_type"] == "approval_result"
                 and e["payload"]["action_ref"] == iam["event_id"]]
        self.assertEqual(final[-1]["payload"]["decided_by"], "ines")

        prod_id = out[prod["event_id"]]["payload"]["approval_id"]
        self.assertEqual(self.client.post(f"/approve/{prod_id}", json={"decided_by": "ines"}).json()["status"],
                         "pending")
        self.assertEqual(self.client.post(f"/approve/{prod_id}", json={"decided_by": "karim"}).json()["status"],
                         "approved")

        self.assertEqual(self.client.get("/audit/verify").json(), {"intact": True, "broken_rows": []})

    def test_malformed_and_garbage_events_do_not_block_the_stream(self):
        self.redis.xadd("pipeline_events", {"data": "not json"})
        self.redis.xadd("pipeline_events", {"data": '{"agent_name": "dependency_resolver"}'})
        ok = self.publish()
        agent.run(once=True)
        self.assertIn(ok["event_id"], self.results())
        self.assertEqual(self.redis.xpending("pipeline_events", "guardrail")["pending"], 0)
        malformed = self.sql("SELECT count(*) FROM reasoning_logs WHERE decision = 'reject: malformed_event'")
        self.assertEqual(malformed[0][0], 2)

    def test_database_down_leaves_event_for_retry(self):
        e = self.publish()
        with mock.patch.object(agent.store, "record_decision", side_effect=ConnectionError("db down")):
            agent.run(once=True)
        self.assertEqual(self.redis.xpending("pipeline_events", "guardrail")["pending"], 1)
        with mock.patch.object(agent, "RECLAIM_IDLE_MS", 0):
            agent.run(once=True)
        self.assertIn(e["event_id"], self.results())
        self.assertEqual(self.redis.xpending("pipeline_events", "guardrail")["pending"], 0)

    def test_event_seen_before_its_db_row_is_decided_without_waiting_for_reclaim(self):
        e = self.publish()
        real, calls = agent.store.record_decision, []

        def not_committed_yet(*args):
            calls.append(1)
            if len(calls) < 3:  # the agent's actions row "is not committed yet"
                raise psycopg2.errors.ForeignKeyViolation()
            return real(*args)

        with mock.patch.object(agent.store, "record_decision", side_effect=not_committed_yet):
            agent.run(once=True)
        self.assertEqual(len(calls), 3)
        self.assertIn(e["event_id"], self.results())
        self.assertEqual(self.redis.xpending("pipeline_events", "guardrail")["pending"], 0)

    def test_api_validation(self):
        self.assertEqual(self.client.get("/approvals/not-a-uuid").status_code, 422)
        self.assertEqual(self.client.get("/approvals/00000000-0000-0000-0000-000000000000").status_code, 404)
        self.assertEqual(self.client.post("/approve/00000000-0000-0000-0000-000000000000",
                                          json={"decided_by": ""}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
