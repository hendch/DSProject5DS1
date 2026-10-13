"""Run with: python -m unittest tests.test_dependency_resolver_fallback -v
No tofu, Ollama, Redis or Postgres needed: the failures are simulated."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.dependency_resolver import agent, fallback

CYCLE_TF = '''
resource "terraform_data" "cluster_role"   { input = terraform_data.cluster_policy.id }
resource "terraform_data" "cluster"        { input = terraform_data.cluster_role.id }
resource "terraform_data" "cluster_policy" { input = terraform_data.cluster.id }
'''
CLEAN_TF = '''
resource "terraform_data" "a" { input = "x" }
resource "terraform_data" "b" { input = terraform_data.a.id }
'''


def tf_dir(content):
    d = tempfile.mkdtemp()
    (Path(d) / "main.tf").write_text(content)
    return d


class StaticCycles(unittest.TestCase):
    def test_finds_three_node_cycle(self):
        cycles = fallback.find_cycles_static(tf_dir(CYCLE_TF))
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"terraform_data.cluster_role",
                                          "terraform_data.cluster",
                                          "terraform_data.cluster_policy"})

    def test_no_false_positive(self):
        self.assertEqual(fallback.find_cycles_static(tf_dir(CLEAN_TF)), [])

    def test_ignores_commented_out_reference(self):
        tf = CLEAN_TF.replace('input = "x"', '# input = terraform_data.b.id\n  input = "x"')
        self.assertEqual(fallback.find_cycles_static(tf_dir(tf)), [])

    def test_self_reference(self):
        tf = 'resource "terraform_data" "x" { input = terraform_data.x.id }'
        self.assertEqual(fallback.find_cycles_static(tf_dir(tf)), [["terraform_data.x"]])

    def test_agent_falls_back_when_tofu_missing(self):
        with mock.patch.object(agent, "tf_binary", side_effect=RuntimeError("no tofu")):
            cycles, source = agent.find_cycles(tf_dir(CYCLE_TF))
        self.assertEqual(source, "static")
        self.assertEqual(len(cycles), 1)


class Heuristics(unittest.TestCase):
    def test_classifier_returns_valid_category(self):
        for code in ["aws_iam_role policy attach", "s3 bucket backend state", "", "zzz"]:
            self.assertIn(fallback.heuristic_classify(code), agent.VALID)

    def test_llm_down_uses_heuristics(self):
        with mock.patch.object(agent.ollama, "chat", side_effect=ConnectionError("refused")), \
             mock.patch.object(agent, "LLM_RETRIES", 0):
            cat, src = agent.classify_cycle("aws_iam_role policy")
            exp, esrc = agent.explain_fix(cat, "code", ["a.b", "c.d"])
        self.assertEqual((src, esrc), ("heuristic", "heuristic"))
        self.assertIn("a.b -> c.d -> a.b", exp)

    def test_garbage_llm_answer_uses_heuristics(self):
        with mock.patch.object(agent, "chat", return_value="I think it is probably a loop"):
            _, src = agent.classify_cycle("code")
        self.assertEqual(src, "heuristic")

    def test_good_llm_answer_is_used(self):
        with mock.patch.object(agent, "chat", return_value="sequencing_fix"):
            self.assertEqual(agent.classify_cycle("code"), ("sequencing_fix", "llm"))


class Outbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "pending.jsonl"
        self.p = mock.patch.object(fallback, "OUTBOX", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_queues_when_infra_down_then_replays(self):
        kw = dict(agent_name="dependency_resolver", action_type="propose_fix",
                  payload={"x": 1}, correlation_id="c1")
        with mock.patch.object(agent, "publish_and_log", side_effect=ConnectionError("down")):
            _, sink = agent.publish_safely(**kw)
        self.assertEqual(sink, "outbox")
        self.assertEqual(len(self.tmp.read_text().splitlines()), 1)

        with mock.patch("shared.events.publish_and_log", return_value="id") as pub:
            self.assertEqual(fallback.replay_outbox(), 1)
        pub.assert_called_once_with(**kw)
        self.assertFalse(self.tmp.exists())

    def test_replay_keeps_failures(self):
        fallback.write_outbox({"kwargs": {"correlation_id": "c"}})
        with mock.patch("shared.events.publish_and_log", side_effect=ConnectionError("down")):
            self.assertEqual(fallback.replay_outbox(), 0)
        self.assertTrue(self.tmp.exists())


class EndToEndNoInfra(unittest.TestCase):
    def test_full_run_with_everything_down(self):
        d = tf_dir(CYCLE_TF)
        out = Path(tempfile.mkdtemp()) / "o.jsonl"
        with mock.patch.object(agent, "tf_binary", side_effect=RuntimeError("no tofu")), \
             mock.patch.object(agent.ollama, "chat", side_effect=ConnectionError("x")), \
             mock.patch.object(agent, "LLM_RETRIES", 0), \
             mock.patch.object(agent, "publish_and_log", side_effect=ConnectionError("x")), \
             mock.patch.object(fallback, "OUTBOX", out):
            agent.run(d, str(Path(d) / "main.tf"))
        rec = json.loads(out.read_text().splitlines()[0])
        self.assertEqual(rec["kwargs"]["payload"]["sources"],
                         {"detection": "static", "classification": "heuristic",
                          "explanation": "heuristic"})


if __name__ == "__main__":
    unittest.main()