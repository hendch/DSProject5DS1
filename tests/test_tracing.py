"""shared.tracing: a no-op without LangSmith, and a real wrapper when it is configured. No network."""
import os
import unittest
from unittest import mock

from shared import tracing

OFF = {"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": ""}
ON = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "lsv2_test_not_a_real_key"}


class TracingOff(unittest.TestCase):
    def test_everything_is_a_no_op(self):
        with mock.patch.dict(os.environ, OFF):
            client = object()
            self.assertFalse(tracing.enabled())
            self.assertIs(tracing.wrap_client(client, "guardrail"), client)
            with tracing.span("run") as span:
                span.outputs({"ok": True})  # accepted and ignored
            self.assertEqual(tracing.child_env(), {})
            tracing.flush()

    def test_key_without_the_switch_stays_off(self):
        with mock.patch.dict(os.environ, {**ON, "LANGSMITH_TRACING": "false"}):
            self.assertFalse(tracing.enabled())


class TracingOn(unittest.TestCase):
    def test_client_is_wrapped(self):
        try:
            import openai  # noqa: F401
            import langsmith  # noqa: F401
        except ImportError:
            self.skipTest("langsmith not installed")
        from openai import OpenAI

        with mock.patch.dict(os.environ, ON):
            raw = OpenAI(base_url="http://127.0.0.1:9/v1", api_key="k")
            wrapped = tracing.wrap_client(raw, "diagnostic_rca")
            self.assertTrue(tracing.enabled())
            self.assertTrue(hasattr(wrapped, "chat"))


if __name__ == "__main__":
    unittest.main()
