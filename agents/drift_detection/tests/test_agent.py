"""End-to-end behaviour: detection, diagnosis, guardrail and approvals on simulated scenarios."""

import json
from types import SimpleNamespace

import pytest

from drift_agent.agent.investigator import extract_json
from drift_agent.agent.tools import ToolBox
from drift_agent.detection.schemas import SEVERITY_ORDER


def run(service, scenario, batches, drift_start=4):
    service.reset(scenario, seed=7, drift_start=drift_start)
    return [service.step() for _ in range(batches)]


def test_clean_scenario_raises_no_alerts(service):
    results = run(service, "clean", 8)
    assert all(SEVERITY_ORDER[r["severity"]] < SEVERITY_ORDER["ALERT"] for r in results)
    assert not any(r["investigation"] for r in results)


def test_pipeline_bug_is_caught_immediately_and_not_retrained(service):
    results = run(service, "pipeline_bug", 5, drift_start=4)
    first = results[3]
    assert first["severity"] == "CRITICAL"
    inv = first["investigation"]
    assert inv["drift_type"] == "pipeline_bug"
    assert inv["recommended_action"] == "investigate_upstream"
    assert not any(a["action"] == "train_challenger" for r in results for a in r["actions"])
    # The same open incident is not investigated again on the next batch.
    assert results[4]["investigation"] is None


def test_evidence_only_cites_tools_that_were_called(service):
    results = run(service, "pipeline_bug", 4, drift_start=4)
    meta = results[3]["investigation"]["meta"]
    assert meta["unverified_evidence_tools"] == []
    assert {"get_drift_report", "drift_history", "recent_pipeline_events"} <= {c["tool"] for c in meta["tool_calls"]}


def test_concept_drift_leads_to_challenger_and_human_approval(service):
    results = run(service, "concept", 12, drift_start=3)
    alerted = [r for r in results if r["investigation"]]
    assert alerted, "concept drift was never investigated"
    assert alerted[0]["investigation"]["drift_type"] == "concept"
    pending = service.store.list_actions(status="pending")
    assert len(pending) == 1 and pending[0]["action"] == "promote_model"
    # Nothing changes until a human approves.
    assert service.store.current_model()["version"] == "v1"
    service.guardrail.approve(pending[0]["id"], by="test")
    assert service.store.current_model()["version"] == pending[0]["payload"]["version"]
    # The old model's labels must not be held against the new one.
    after = [service.step() for _ in range(2)]
    assert not service.store.list_actions(status="pending")
    assert all(r["severity"] != "CRITICAL" for r in after)
    # And it can be undone (with another approval).
    rb = service.guardrail.request_rollback(by="test")
    service.guardrail.approve(rb["id"], by="test")
    assert service.store.current_model()["version"] == "v1"


def test_actions_cannot_be_decided_twice(service):
    run(service, "concept", 12, drift_start=3)
    action = service.store.list_actions(status="pending")[0]
    service.guardrail.reject(action["id"], by="test")
    with pytest.raises(ValueError):
        service.guardrail.approve(action["id"], by="test")


def test_tools_reject_unknown_names_and_features(service):
    results = run(service, "clean", 2)
    report = service.get_report(results[-1]["run_id"])
    tools = ToolBox(service, results[-1]["run_id"], report)
    assert "error" in tools.call("delete_everything")
    assert "error" in tools.call("compare_distributions", {"feature": "salary"})
    assert "error" in tools.call("segment_breakdown", {"feature": "age", "by": "balance"})
    assert tools.call("compare_distributions", {"feature": "age"})["kind"] == "numeric"


def test_labels_are_hidden_until_they_arrive(service):
    run(service, "clean", 3)
    with pytest.raises(PermissionError):
        service.simulator.load_batch(3, with_labels=True)
    assert "__label" in service.simulator.load_batch(1, with_labels=True)


def test_extract_json_tolerates_fences_and_prose():
    obj = {"a": 1, "b": {"c": [1, 2]}}
    assert extract_json(json.dumps(obj)) == obj
    assert extract_json("Here you go:\n```json\n" + json.dumps(obj) + "\n```") == obj
    with pytest.raises(ValueError):
        extract_json("no json here")


class FakeGrok:
    """Scripted LLM. Each turn is either a list of (tool, args) calls or a final answer string.
    The last turn repeats if the conversation runs longer than the script."""

    model = "fake-grok"

    def __init__(self, *turns):
        self.turns, self.turn = turns, 0

    def chat(self, messages, tools=None, temperature=None, tool_choice="auto"):
        step = self.turns[min(self.turn, len(self.turns) - 1)]
        self.turn += 1
        if isinstance(step, str):
            msg = SimpleNamespace(content=step, tool_calls=None)
        else:
            msg = SimpleNamespace(content="", tool_calls=[
                SimpleNamespace(id=f"c{self.turn}-{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                for i, (n, a) in enumerate(step)])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(total_tokens=100))


VALID = json.dumps({
    "summary": "Balance is 100x larger since the ETL release.", "drift_type": "pipeline_bug",
    "affected_features": ["balance", "salary"],
    "root_causes": [{"hypothesis": "Unit change", "confidence": "high",
                     "evidence": [{"tool": "drift_history", "observation": "sudden jump"}]}],
    "impact": "Scores unreliable.", "recommended_action": "investigate_upstream",
    "action_rationale": "Fix the data first.", "explanation": ["Looked at history."],
})
REQUIRED = [("get_drift_report", {}), ("recent_pipeline_events", {}), ("performance_history", {})]


def investigate_with(service, fake):
    service.use_llm = True
    service.investigator.set_client(fake)
    return run(service, "pipeline_bug", 4, drift_start=4)[3]["investigation"]


def test_llm_loop_runs_tools_and_validates_answer(service):
    inv = investigate_with(service, FakeGrok(REQUIRED + [("drift_history", {"feature": "balance"})], VALID))
    m = inv["meta"]
    assert m["source"] == "llm"
    assert [c["tool"] for c in m["tool_calls"]] == [n for n, _ in REQUIRED] + ["drift_history"]
    assert m["missing_required_tools"] == [] and m["reminders"] == 0
    assert m["llm_turns"] == 2 and m["llm_tokens"] == 200


def test_invented_features_are_removed(service):
    inv = investigate_with(service, FakeGrok(REQUIRED + [("drift_history", {"feature": "balance"})], VALID))
    assert inv["affected_features"] == ["balance"]
    assert inv["meta"]["unknown_features"] == ["salary"]


def test_llm_is_made_to_check_required_tools(service):
    # Answers after one tool, gets reminded, calls the missing tools, answers again.
    fake = FakeGrok([("get_drift_report", {}), ("drift_history", {"feature": "balance"})], VALID,
                    [("recent_pipeline_events", {}), ("performance_history", {})], VALID)
    m = investigate_with(service, fake)["meta"]
    assert m["reminders"] == 1
    assert m["missing_required_tools"] == []
    assert {"recent_pipeline_events", "performance_history"} <= {c["tool"] for c in m["tool_calls"]}


def test_ignoring_required_tools_is_flagged(service):
    m = investigate_with(service, FakeGrok([("get_drift_report", {}), ("drift_history", {"feature": "balance"})],
                                           VALID))["meta"]
    assert m["source"] == "llm"
    assert m["reminders"] == service.settings["agent"]["required_tool_reminders"]
    assert m["missing_required_tools"] == ["performance_history", "recent_pipeline_events"]


def test_invalid_llm_answer_falls_back_to_rules(service):
    inv = investigate_with(service, FakeGrok(REQUIRED, '{"summary": "missing fields"}'))
    assert inv["meta"]["source"] == "rules"
    assert inv["meta"]["llm_error"]
    assert inv["drift_type"] == "pipeline_bug"
