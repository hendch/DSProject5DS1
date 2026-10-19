"""The PostgreSQL store keeps the former SQLite store's behaviour. Needs no dataset:
runs on a throwaway database (shared.testing)."""
from pathlib import Path

from agents.drift_detection.store.db import Store, run_correlation_id, schema_for
from shared.testing import DbTestCase

FEATURE = {"feature": "age", "kind": "numeric", "status": "significant", "psi": 0.31,
           "statistic": 0.2, "p_value": 0.001, "effect": 0.2, "drift_score": 0.6,
           "importance": 0.3, "weighted": 0.25}


class StorePostgres(DbTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path("agents/drift_detection/runtime/drift_agent.db"))

    def test_normal_workspace_uses_the_drift_schema(self):
        self.assertEqual(self.store.schema, "drift")
        other = schema_for(Path("/tmp/eval/ws1/drift_agent.db"))
        self.assertTrue(other.startswith("drift_ws_"))
        self.assertEqual(other, schema_for(Path("/tmp/eval/ws1/drift_agent.db")))  # stable

    def test_runs_reports_and_features(self):
        self.store.add_batch(1, "gradual", 1000, "v1", "b1.csv")
        run_id = self.store.add_run(1, None, "v1", "ALERT", 0.12, {"severity": "ALERT"}, [FEATURE])
        self.assertEqual(self.store.get_run(run_id)["report"], {"severity": "ALERT"})
        self.assertEqual(self.store.feature_results(run_id)[0]["feature"], "age")
        self.store.add_report(run_id, "rules", None, {"summary": "pipeline bug"})
        self.assertEqual(self.store.get_report(run_id)["report"]["summary"], "pipeline bug")
        self.assertEqual(self.store.run_ids_with_report(), {run_id})
        self.assertEqual(self.store.list_runs()[0]["has_report"], 1)
        self.assertEqual(self.store.latest_batch_id(), 1)
        self.assertEqual(self.store.release_labels(1), [1])

    def test_actions_and_decisions(self):
        aid = self.store.add_action(None, "promote_model", "high", "pending", {"to": "v2"})
        self.store.update_action(aid, "approved", decided_by="ines", note="looks good")
        action = self.store.get_action(aid)
        self.assertEqual((action["status"], action["decided_by"], action["payload"]), ("approved", "ines", {"to": "v2"}))
        self.assertEqual([a["id"] for a in self.store.list_actions(status="approved")], [aid])

    def test_model_versions_replace_and_switch(self):
        for v in ("v1", "v2"):
            self.store.add_model_version(v, f"{v}.joblib", "ref.csv", None, 0.5, {"auc": 0.9}, {"age": 0.3},
                                         "", make_current=(v == "v1"))
        self.store.add_model_version("v2", "v2b.joblib", "ref.csv", "v1", 0.5, {"auc": 0.92}, {}, "retrained",
                                     make_current=False)  # same version again: replaced, not duplicated
        self.assertEqual(len(self.store.list_models()), 2)
        self.store.set_current_model("v2")
        self.assertEqual(self.store.current_model()["path"], "v2b.joblib")

    def test_simulated_events_and_state(self):
        self.store.add_event(3, "crm", "Weekly CRM export finished")
        self.assertEqual(self.store.events_between(1, 5)[0]["message"], "Weekly CRM export finished")
        self.store.set_state("scenario", "concept")
        self.store.set_state("scenario", "clean")
        self.assertEqual(self.store.get_state("scenario"), "clean")

    def test_audit_is_copied_to_the_shared_chain_and_survives_reset(self):
        self.store.audit("agent", "investigation_done", {"drift_type": "pipeline_bug"}, run_id=7)
        self.store.reset_simulation()
        self.assertEqual(self.store.list_audit(), [])  # the drift dashboard starts clean
        rows = self.sql("SELECT correlation_id::text, agent_name, decision FROM reasoning_logs")
        self.assertEqual(rows, [(run_correlation_id(7), "drift_detection", "agent: investigation_done")])
        self.assertEqual(self.sql("SELECT count(*) FROM verify_reasoning_chain()"), [(0,)])

    def test_isolated_workspaces_do_not_mix(self):
        import tempfile
        ws = Store(Path(tempfile.mkdtemp()) / "ws1" / "drift_agent.db")
        ws.add_batch(1, "clean", 10, "v1", "b.csv")
        self.assertEqual(self.store.list_batches(), [])
        self.assertEqual(len(ws.list_batches()), 1)
        ws.audit("agent", "x")
        self.assertEqual(self.sql("SELECT count(*) FROM reasoning_logs"), [(0,)])  # only the main workspace
