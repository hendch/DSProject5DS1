"""shared.llm (profiles, reasoning models, cooldown) and shared.redact. No network."""
import os
import unittest
from unittest import mock

from shared import llm
from shared.redact import redact, tail


class Profiles(unittest.TestCase):
    def test_agent_override_then_default(self):
        env = {"LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": "llama3.1:8b", "LLM_API_KEY": "ollama",
               "DIAGNOSTIC_RCA_LLM_MODEL": "deepseek-r1:8b",
               "GUARDRAIL_LLM_BASE_URL": "https://router.huggingface.co/v1", "GUARDRAIL_LLM_MODEL": "Qwen/Qwen2.5-7B"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(llm.profile("diagnostic_rca").model, "deepseek-r1:8b")
            self.assertEqual(llm.profile("diagnostic_rca").base_url, "http://localhost:11434/v1")
            self.assertEqual(llm.profile("merge_assistant").model, "llama3.1:8b")
            self.assertEqual(llm.profile("guardrail").label, "router.huggingface.co:Qwen/Qwen2.5-7B")
            self.assertEqual(llm.profile("merge_assistant").label, "ollama:llama3.1:8b")


class Reasoning(unittest.TestCase):
    def test_think_blocks_are_split_off(self):
        answer, reasoning = llm.split_reasoning("<think>check the log first</think>\n{\"a\": 1}")
        self.assertEqual((answer, reasoning), ('{"a": 1}', "check the log first"))

    def test_unterminated_think(self):
        answer, reasoning = llm.split_reasoning("ok <think> still thinking")
        self.assertEqual(answer, "ok")
        self.assertIn("still thinking", reasoning)

    def test_json_in_prose(self):
        self.assertEqual(llm.parse_json_object('Sure! ```json\n{"risky": true}\n```'), {"risky": True})
        with self.assertRaises(llm.LLMUnavailable):
            llm.parse_json_object("no json here")


class Cooldown(unittest.TestCase):
    def setUp(self):
        llm.reset_cooldowns()
        self.addCleanup(llm.reset_cooldowns)

    def test_failure_skips_the_profile_for_a_while(self):
        model = llm.LLM(llm.Profile("x", "http://127.0.0.1:9/v1", "k", "m", 1))
        with mock.patch("openai.OpenAI", side_effect=ConnectionError("refused")) as client:
            with self.assertRaises(llm.LLMUnavailable):
                model.chat("hi")
            with self.assertRaisesRegex(llm.LLMUnavailable, "cooling down"):
                model.chat("hi")
        self.assertEqual(client.call_count, 1)

    def test_reasoning_answer_is_cleaned(self):
        model = llm.LLM(llm.Profile("x", "http://h/v1", "k", "m", 1))
        reply = mock.Mock()
        reply.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(
            content="<think>hmm</think>The cause is X."))]
        with mock.patch("openai.OpenAI", return_value=reply):
            self.assertEqual(model.chat("why?"), "The cause is X.")
        self.assertEqual(model.last_reasoning, "hmm")


class Redaction(unittest.TestCase):
    def test_secrets_are_masked(self):
        text = ("token ghp_" + "a" * 36 + " and xai-" + "b" * 40 + "\nDB_PASSWORD=hunter2\n"
                "Authorization: Bearer abc.def\npostgresql://devops:secretpw@db:5432/x\nAKIAABCDEFGHIJKLMNOP")
        out = redact(text)
        for secret in ("ghp_aaaa", "xai-bbbb", "hunter2", "abc.def", "secretpw", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(secret, out)
        self.assertIn("DB_PASSWORD=[REDACTED]", out)

    def test_ordinary_text_untouched(self):
        line = "FAILED tests/test_db.py::test_create_user - connection refused"
        self.assertEqual(redact(line), line)

    def test_tail_keeps_the_end(self):
        log = "\n".join(f"line {i}" for i in range(1000))
        out = tail(log, max_lines=10)
        self.assertTrue(out.endswith("line 999"))
        self.assertIn("990 earlier lines cut", out)


class ProfileOptions(unittest.TestCase):
    def test_reasoning_model_options(self):
        env = {"DIAGNOSTIC_RCA_LLM_JSON_MODE": "false", "DIAGNOSTIC_RCA_LLM_MAX_TOKENS": "2500",
               "LANGSMITH_TRACING": "false"}  # tracing would wrap the mock client
        with mock.patch.dict(os.environ, env, clear=False):
            p = llm.profile("diagnostic_rca")
            self.assertEqual((p.json_mode, p.max_tokens), (False, 2500))
            model = llm.LLM(p)
            reply = mock.Mock()
            reply.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(content='{"ok": 1}'))]
            with mock.patch("openai.OpenAI", return_value=reply):
                model.chat("x", json_mode=True, max_tokens=100)
        kwargs = reply.chat.completions.create.call_args.kwargs
        self.assertNotIn("response_format", kwargs)  # JSON mode off for this profile
        self.assertEqual(kwargs["max_tokens"], 2500)
