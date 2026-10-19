import json
from types import SimpleNamespace

from code_review_agent.context import build_context
from code_review_agent.llm import LLMConfig, run_review
from code_review_agent.policy import load_policy
from code_review_agent.tools import ToolBox

POLICY = load_policy()
CFG = LLMConfig("http://fake", "fake-model", "sk-test-SECRET-123")
REVIEW = {"summary": "s", "findings": []}


def _call(name, args, n=0):
    data = {"id": f"c{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return SimpleNamespace(id=data["id"], function=SimpleNamespace(**data["function"]), model_dump=lambda: data)


def _resp(content=None, calls=None):
    msg = SimpleNamespace(content=content, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _setup(make_repo):
    repo, base, head = make_repo({"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    ctx = build_context(repo, base, head)
    return ctx, ToolBox(ctx, POLICY)


def test_tool_loop_then_submit(make_repo):
    ctx, box = _setup(make_repo)
    client = FakeClient([_resp(calls=[_call("read_file", {"path": "a.py"})]),
                         _resp(calls=[_call("submit_review", REVIEW, 1)])])
    out = run_review(ctx, box, POLICY, CFG, client)
    assert out.review == REVIEW and out.stop_reason == "submitted"
    assert out.tool_calls == 2 and out.usage["input_tokens"] == 200
    assert CFG.api_key not in json.dumps(client.requests[-1]["messages"])  # key never in the prompt


def test_budget_forces_submit(make_repo):
    ctx, box = _setup(make_repo)
    many = [_call("list_changed_files", {}, n) for n in range(POLICY.limits.max_tool_calls + 3)]
    client = FakeClient([_resp(calls=many), _resp(calls=[_call("submit_review", REVIEW, 99)])])
    out = run_review(ctx, box, POLICY, CFG, client)
    assert out.tool_calls == POLICY.limits.max_tool_calls + 1  # budget plus the final submit
    assert client.requests[1]["tool_choice"]["function"]["name"] == "submit_review"
    assert out.review == REVIEW


def test_text_json_fallback(make_repo):
    ctx, box = _setup(make_repo)
    out = run_review(ctx, box, POLICY, CFG, FakeClient([_resp(content=f"Here: {json.dumps(REVIEW)}")]))
    assert out.review == REVIEW and out.stop_reason == "submitted_as_text"


def test_gives_up_after_max_turns(make_repo):
    ctx, box = _setup(make_repo)
    client = FakeClient([_resp(content="thinking...") for _ in range(POLICY.limits.max_llm_turns)])
    out = run_review(ctx, box, POLICY, CFG, client)
    assert out.review is None and out.stop_reason == "budget_exhausted"
    assert len(client.requests) == POLICY.limits.max_llm_turns
