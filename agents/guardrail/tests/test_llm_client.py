"""LLM backends with HTTP mocked: the right request goes out for each
provider, answers are parsed, and failures raise (so the judge falls back)."""
import unittest
from unittest import mock

import httpx

from agents.guardrail import llm_client


def http(status=200, body=None):
    return httpx.Response(status, json=body or {}, request=httpx.Request("POST", "https://x"))


def provider(name, key="sk-test-123", model="m"):
    return mock.patch.multiple(llm_client, PROVIDER=name, API_KEY=key, MODEL=model,
                               BASE_URL="https://api.groq.com/openai/v1")


class OpenAICompatible(unittest.TestCase):
    def test_request_and_answer(self):
        answer = {"choices": [{"message": {"content": '{"risky": false, "summary": "ok"}'}}]}
        with provider("openai", model="llama-3.1-8b-instant"), \
             mock.patch.object(llm_client.httpx, "post", return_value=http(body=answer)) as post:
            self.assertEqual(llm_client.chat_json("SYS", "USER"), {"risky": False, "summary": "ok"})
        url, kw = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kw["headers"], {"Authorization": "Bearer sk-test-123"})
        self.assertEqual(kw["json"]["model"], "llama-3.1-8b-instant")
        self.assertEqual(kw["json"]["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in kw["json"]["messages"]], ["system", "user"])

    def test_rate_limit_raises_without_leaking_the_key(self):
        with provider("openai"), mock.patch.object(llm_client.httpx, "post",
                                                    return_value=http(429, {"error": "slow down"})):
            with self.assertRaises(RuntimeError) as ctx:
                llm_client.chat_json("s", "u")
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertNotIn("sk-test-123", str(ctx.exception))

    def test_missing_key_raises_before_any_call(self):
        with provider("openai", key=""), mock.patch.object(llm_client.httpx, "post") as post:
            self.assertRaisesRegex(RuntimeError, "LLM_API_KEY", llm_client.chat_json, "s", "u")
        post.assert_not_called()


class Anthropic(unittest.TestCase):
    def test_request_and_answer_wrapped_in_prose(self):
        answer = {"content": [{"type": "text",
                               "text": 'Here you go:\n```json\n{"risky": true, "summary": "IAM"}\n```'}]}
        with provider("anthropic", model="claude-haiku-5-5"), \
             mock.patch.object(llm_client.httpx, "post", return_value=http(body=answer)) as post:
            self.assertEqual(llm_client.chat_json("SYS", "USER"), {"risky": True, "summary": "IAM"})
        kw = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(kw["headers"]["x-api-key"], "sk-test-123")
        self.assertEqual(kw["json"]["system"], "SYS")
        self.assertEqual(kw["json"]["messages"], [{"role": "user", "content": "USER"}])


class Parsing(unittest.TestCase):
    def test_garbage_raises(self):
        self.assertRaises(ValueError, llm_client._parse, "I think it is fine")
        self.assertRaises(ValueError, llm_client._parse, "[1, 2]")

    def test_unknown_provider(self):
        with provider("skynet"):
            self.assertRaisesRegex(RuntimeError, "unknown LLM_PROVIDER", llm_client.chat_json, "s", "u")


if __name__ == "__main__":
    unittest.main()
