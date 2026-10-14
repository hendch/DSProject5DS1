"""Postgres behaviour against a real database: approvals, the human vote
rules, race conditions, expiry, and the tamper-evident audit log."""
import threading
import unittest

import psycopg2

from agents.guardrail import store
from agents.guardrail.tests.support import DbTestCase

AUTO = {"decision": "auto_approve", "rule": "low_risk_auto", "required_approvals": 0, "reason": "ok"}
HOLD = {"decision": "hold", "rule": "high_always_human", "required_approvals": 1, "reason": "high"}
TWO = {"decision": "hold", "rule": "prod_high_two_person", "required_approvals": 2, "reason": "prod"}


class Decisions(DbTestCase):
    def test_auto_approve_is_recorded_with_its_reasons(self):
        e = self.publish()
        a = store.record_decision(e, "low", [{"signal": "declared_by_agent"}], AUTO, None, None)
        self.assertEqual((a["status"], a["decided_by"]), ("approved", "guardrail:policy"))
        trace, decision = self.sql("""SELECT reasoning_trace, decision FROM reasoning_logs
                                      WHERE agent_name = 'guardrail'""")[0]
        self.assertEqual(decision, "auto_approve: low_risk_auto")
        self.assertEqual(trace["computed_risk"], "low")
        self.assertEqual(trace["action_ref"], e["event_id"])

    def test_hold_is_pending_with_expiry(self):
        a = store.record_decision(self.publish(), "high", [], HOLD, None, None)
        self.assertEqual(a["status"], "pending")
        self.assertIsNotNone(a["expires_at"])

    def test_redelivered_event_is_not_decided_twice(self):
        e = self.publish()
        self.assertIsNotNone(store.record_decision(e, "low", [], AUTO, None, None))
        self.assertIsNone(store.record_decision(e, "low", [], AUTO, None, None))
        self.assertEqual(self.sql("SELECT count(*) FROM approvals")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM reasoning_logs WHERE agent_name='guardrail'")[0][0], 1)

    def test_event_before_its_action_row_raises_so_it_is_retried(self):
        from agents.guardrail.tests.support import event
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            store.record_decision(event(), "low", [], AUTO, None, None)


class Votes(DbTestCase):
    def pending(self, verdict=HOLD, requested_by=None):
        return store.record_decision(self.publish(), "high", [], verdict, None, requested_by)["id"]

    def assertRefused(self, status, fn, *args):
        with self.assertRaises(store.ApprovalError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.http_status, status)
        return str(ctx.exception)

    def test_approve(self):
        r = store.cast_vote(self.pending(), "ines", "approve")
        self.assertEqual((r["status"], r["decided_by"]), ("approved", "ines"))

    def test_cannot_approve_own_action(self):
        aid = self.pending(requested_by="sami")
        self.assertRefused(403, store.cast_vote, aid, "sami", "approve")
        self.assertEqual(store.cast_vote(aid, "ines", "approve")["status"], "approved")

    def test_second_click_after_decision_is_refused(self):
        aid = self.pending()
        store.cast_vote(aid, "ines", "approve")
        msg = self.assertRefused(409, store.cast_vote, aid, "karim", "approve")
        self.assertIn("already approved by ines", msg)

    def test_two_person_rule(self):
        aid = self.pending(TWO)
        first = store.cast_vote(aid, "ines", "approve")
        self.assertEqual((first["status"], first["approvals"]), ("pending", ["ines"]))
        self.assertRefused(409, store.cast_vote, aid, "ines", "approve")  # same person again
        second = store.cast_vote(aid, "karim", "approve")
        self.assertEqual((second["status"], second["decided_by"]), ("approved", "ines, karim"))

    def test_one_reject_is_final(self):
        aid = self.pending(TWO)
        store.cast_vote(aid, "ines", "approve")
        self.assertEqual(store.cast_vote(aid, "karim", "reject")["status"], "rejected")

    def test_unknown_and_blank(self):
        self.assertRefused(404, store.cast_vote, "00000000-0000-0000-0000-000000000000", "ines", "approve")
        self.assertRefused(400, store.cast_vote, self.pending(), "  ", "approve")

    def test_expired_approval_cannot_be_approved(self):
        aid = self.pending()
        self.sql("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s", (aid,))
        self.assertRefused(410, store.cast_vote, aid, "ines", "approve")
        self.assertEqual(self.sql("SELECT status FROM approvals WHERE id = %s", (aid,))[0][0], "expired")

    def test_expiry_sweep(self):
        aid, keep = self.pending(), self.pending()
        self.sql("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = %s", (aid,))
        expired = store.expire_overdue()
        self.assertEqual([str(r["id"]) for r in expired], [aid])
        self.assertEqual([str(r["id"]) for r in store.list_pending()], [keep])
        self.assertRefused(410, store.cast_vote, aid, "ines", "approve")

    def test_simultaneous_clicks_only_one_wins(self):
        for _ in range(5):
            aid, results = self.pending(), []
            barrier = threading.Barrier(2)

            def click(who):
                barrier.wait()
                try:
                    results.append(store.cast_vote(aid, who, "approve")["status"])
                except store.ApprovalError as e:
                    results.append(e.http_status)

            threads = [threading.Thread(target=click, args=(w,)) for w in ("ines", "karim")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results, key=str), [409, "approved"])
            self.assertEqual(self.sql("SELECT count(*) FROM approval_votes WHERE approval_id = %s",
                                      (aid,))[0][0], 1)


class AuditChain(DbTestCase):
    def setUp(self):
        super().setUp()
        for verdict in (AUTO, HOLD, AUTO):  # agents' own rows (publish_and_log) + guardrail rows
            store.record_decision(self.publish(), "low", [], verdict, None, None)

    def test_chain_is_intact_and_covers_every_agent(self):
        self.assertEqual(store.verify_chain(), [])
        rows = self.sql("SELECT agent_name, prev_hash, row_hash FROM reasoning_logs ORDER BY id")
        self.assertEqual(len(rows), 6)
        self.assertIsNone(rows[0][1])
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur[1], prev[2])

    def test_update_delete_truncate_are_refused(self):
        for q in ("UPDATE reasoning_logs SET decision = 'x'", "DELETE FROM reasoning_logs",
                  "TRUNCATE reasoning_logs"):
            with self.subTest(q), self.assertRaisesRegex(psycopg2.Error, "append-only"):
                self.sql(q)

    def tamper(self, query):
        """What an admin could do by switching the protection off."""
        self.sql("ALTER TABLE reasoning_logs DISABLE TRIGGER USER")
        self.sql(query)
        self.sql("ALTER TABLE reasoning_logs ENABLE TRIGGER USER")

    def test_edited_row_is_detected(self):
        rid = self.sql("SELECT id FROM reasoning_logs ORDER BY id LIMIT 1 OFFSET 2")[0][0]
        self.tamper(f"UPDATE reasoning_logs SET decision = 'approved by nobody' WHERE id = {rid}")
        self.assertEqual(store.verify_chain(), [{"id": rid, "problem": "content changed after it was written"}])

    def test_deleted_row_is_detected(self):
        ids = [r[0] for r in self.sql("SELECT id FROM reasoning_logs ORDER BY id")]
        self.tamper(f"DELETE FROM reasoning_logs WHERE id = {ids[2]}")
        broken = store.verify_chain()
        self.assertEqual([b["id"] for b in broken], [ids[3]])
        self.assertIn("deleted or reordered", broken[0]["problem"])


if __name__ == "__main__":
    unittest.main()
