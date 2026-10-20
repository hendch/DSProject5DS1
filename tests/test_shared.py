"""The shared PostgreSQL layer: migrations, the event bus and the knowledge base.
Runs on a throwaway database (shared.testing); no Docker, no model download."""
import hashlib
import threading
import time
from unittest import mock

from shared import events, knowledge
from shared.migrate import migrate
from shared.testing import DbTestCase


class Migrations(DbTestCase):
    def test_everything_applied_once_and_rerun_is_a_no_op(self):
        applied = [r[0] for r in self.sql("SELECT version FROM schema_migrations ORDER BY version")]
        self.assertEqual(applied, ["001_core", "002_guardrail", "003_events", "004_orchestrator", "005_knowledge",
                                   "006_github_alerts"])
        self.assertEqual(migrate(self.db_uri, quiet=True), [])

    def test_all_areas_exist(self):
        tables = {r[0] for r in self.sql("""SELECT table_schema || '.' || table_name FROM information_schema.tables
                                            WHERE table_schema IN ('public', 'orchestrator', 'knowledge', 'github')""")}
        for t in ("public.actions", "public.approvals", "public.reasoning_logs", "public.events",
                  "public.event_receipts", "orchestrator.runs", "orchestrator.run_steps",
                  "orchestrator.run_actions", "knowledge.documents", "public.alerts", "public.app_settings",
                  "github.items", "github.seen"):
            self.assertIn(t, tables)


class Bus(DbTestCase):
    def publish(self, n=1, **kw):
        return [events.publish_and_log("dependency_resolver", "propose_fix", {"i": i},
                                       "00000000-0000-0000-0000-00000000000a", **kw) for i in range(n)]

    def test_publish_writes_action_audit_and_event_together(self):
        (event_id,) = self.publish()
        self.assertEqual(self.sql("SELECT count(*) FROM actions WHERE id = %s", (event_id,)), [(1,)])
        self.assertEqual(self.sql("SELECT count(*) FROM reasoning_logs"), [(1,)])
        self.assertEqual(self.events()[0]["event_id"], event_id)

    def test_events_are_append_only(self):
        self.publish()
        with self.assertRaisesRegex(Exception, "append-only"):
            self.sql("UPDATE events SET body = '{}'")

    def test_consumer_sees_each_event_once_in_order(self):
        ids = self.publish(3)
        seen = []
        c = events.Consumer("test", lambda e: seen.append(e["event_id"]))
        self.assertEqual(c.poll(), 3)
        self.assertEqual(c.poll(), 0)
        self.assertEqual(seen, ids)
        self.assertEqual(self.unhandled("test"), 0)

    def test_each_consumer_has_its_own_receipts(self):
        self.publish(2)
        events.Consumer("a", lambda e: None).poll()
        self.assertEqual(self.unhandled("a"), 0)
        self.assertEqual(self.unhandled("b"), 2)

    def test_failures_back_off_then_die(self):
        self.publish()
        c = events.Consumer("test", mock.Mock(side_effect=RuntimeError("boom")), max_attempts=3, retry_seconds=60)
        for expected in (("retry", 1), ("retry", 2), ("dead", 3)):
            c.poll()
            self.assertEqual(self.sql("SELECT status, attempts FROM event_receipts"), [expected])
            self.assertEqual(c.poll(), 0)  # not due yet: the backoff holds it back
            self.sql("UPDATE event_receipts SET next_attempt_at = now()")
        self.assertEqual(c.handler.call_count, 3)
        self.assertIn("boom", self.sql("SELECT last_error FROM event_receipts")[0][0])

    def test_list_events_by_correlation(self):
        self.publish(2)
        events.publish(events.make_event("guardrail", "approval_result", {}, "00000000-0000-0000-0000-00000000000b"))
        self.assertEqual(len(events.list_events("00000000-0000-0000-0000-00000000000a")), 2)
        self.assertEqual(len(events.list_events()), 3)

    def test_run_wakes_up_on_notify(self):
        seen, stop = [], threading.Event()
        c = events.Consumer("live", lambda e: seen.append(e))
        t = threading.Thread(target=c.run, kwargs={"stop": stop, "idle_seconds": 30})
        t.start()
        try:
            time.sleep(0.5)  # let it start listening
            started = time.monotonic()
            self.publish()
            while not seen and time.monotonic() - started < 10:
                time.sleep(0.05)
            self.assertEqual(len(seen), 1)
            self.assertLess(time.monotonic() - started, 5)  # NOTIFY, not the 30 s idle timeout
        finally:
            stop.set()
            t.join(35)


def fake_embed(texts):
    """Deterministic 384-d vectors where texts sharing words point in similar directions."""
    out = []
    for text in texts:
        v = [0.0] * knowledge.DIMENSIONS
        for word in text.lower().split():
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % knowledge.DIMENSIONS] += 1.0
        out.append(v)
    return out


class Knowledge(DbTestCase):
    def setUp(self):
        super().setUp()
        knowledge.set_embedder(fake_embed)
        self.addCleanup(knowledge.set_embedder, None)
        knowledge.add_documents([
            {"id": "pg", "source": "kb-pg", "content": "database connection refused postgres not ready"},
            {"id": "npm", "source": "kb-npm", "content": "npm peer dependency conflict eresolve"},
        ], collection="t")

    def test_closest_document_first(self):
        hits = knowledge.search("postgres connection refused", collection="t")
        self.assertEqual([h["source"] for h in hits][:1], ["kb-pg"])
        self.assertLess(hits[0]["distance"], hits[-1]["distance"])

    def test_cutoff_and_collections(self):
        self.assertEqual(knowledge.search("postgres connection refused", collection="t", max_distance=0.0001), [])
        self.assertEqual(knowledge.search("postgres", collection="other"), [])
        self.assertEqual(knowledge.count("t"), 2)

    def test_upsert_replaces(self):
        knowledge.add_documents([{"id": "pg", "source": "kb-pg-v2", "content": "updated text"}], collection="t")
        self.assertEqual(knowledge.count("t"), 2)
        self.assertEqual(self.sql("SELECT source FROM knowledge.documents WHERE id = 'pg'"), [("kb-pg-v2",)])

    def test_wrong_dimension_is_refused(self):
        knowledge.set_embedder(lambda texts: [[0.1] * 10 for _ in texts])
        with self.assertRaisesRegex(ValueError, "384"):
            knowledge.search("x", collection="t")

    def test_rca_retrieval_degrades_gracefully(self):
        from agents.diagnostic_rca import retrieval
        with mock.patch.object(knowledge, "search", side_effect=ConnectionError("db down")):
            self.assertEqual(retrieval.retrieve(["anything"]), [])
