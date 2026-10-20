"""Optional LangSmith tracing for the whole multi-agent system.

On when LANGSMITH_TRACING=true and LANGSMITH_API_KEY is set (LANGSMITH_PROJECT names the
project, default "opsagents"). Otherwise every helper here does nothing, so the app runs
the same without a LangSmith account.

One orchestrator run becomes one trace: the run span holds a span per agent step, and
each agent's model calls nest under its step (the Code Reviewer runs in a subprocess and
joins the same trace through OPSAGENTS_TRACE_PARENT). Guardrail decisions are their own
traces, tagged with the run's correlation id.
"""
import contextlib
import json
import os

PARENT_ENV = "OPSAGENTS_TRACE_PARENT"


def enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))


def _project() -> str:
    return os.getenv("LANGSMITH_PROJECT", "opsagents")


def wrap_client(client, agent: str | None = None):
    """Trace every call made with this OpenAI-compatible client (prompt, answer, tokens, latency)."""
    if not enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai
    except ImportError:
        return client
    tags = [agent] if agent else []
    return wrap_openai(client, tracing_extra={"tags": tags, "metadata": {"agent": agent}},
                       chat_name=f"llm:{agent}" if agent else "ChatOpenAI")


@contextlib.contextmanager
def span(name: str, run_type: str = "chain", inputs: dict | None = None,
         metadata: dict | None = None, tags: list[str] | None = None):
    """A traced block. Yields a handle with .outputs(dict); yields a no-op handle when tracing is off."""
    if not enabled():
        yield _NoSpan()
        return
    import langsmith as ls

    with ls.trace(name, run_type=run_type, inputs=inputs or {}, metadata=metadata or {},
                  tags=tags or [], project_name=_project()) as run:
        yield _Span(run)


class _NoSpan:
    def outputs(self, values: dict) -> None:
        pass


class _Span:
    def __init__(self, run):
        self.run = run

    def outputs(self, values: dict) -> None:
        self.run.add_outputs(values)


def child_env() -> dict:
    """Environment for a subprocess so its traces nest under the current span."""
    if not enabled():
        return {}
    try:
        from langsmith.run_helpers import get_current_run_tree
    except ImportError:
        return {}
    run = get_current_run_tree()
    return {PARENT_ENV: json.dumps(run.to_headers())} if run else {}


@contextlib.contextmanager
def from_parent_env():
    """In a subprocess: attach traces to the parent span passed by child_env()."""
    raw = os.getenv(PARENT_ENV)
    if not enabled() or not raw:
        yield
        return
    import langsmith as ls

    with ls.tracing_context(parent=json.loads(raw), project_name=_project()):
        yield


def flush() -> None:
    """Send pending traces now (short-lived processes exit before the background sender)."""
    if not enabled():
        return
    try:
        from langsmith.run_trees import get_cached_client
        get_cached_client().flush()
    except Exception as e:  # noqa: BLE001 - tracing must never break the app
        print(f"[tracing] flush failed: {e}")
