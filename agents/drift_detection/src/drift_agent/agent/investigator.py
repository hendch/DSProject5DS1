"""The investigation loop: Grok + read-only tools, with a rule-based fallback.

Flow: send the detector's findings -> Grok calls tools -> we run them and send
results back -> repeat (bounded) -> Grok returns a JSON incident report ->
we validate it against the schema and check that every cited tool was
actually called. Any failure falls back to the rule-based investigator.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from pydantic import ValidationError

from drift_agent.agent import fallback
from drift_agent.agent.prompts import system_prompt, user_prompt
from drift_agent.agent.schemas import InvestigationReport
from drift_agent.agent.tools import ToolBox, openai_tools
from drift_agent.detection.schemas import DriftReport

if TYPE_CHECKING:
    from drift_agent.service import DriftService


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or surrounding prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in the answer")
    return json.loads(text[start : end + 1])


class Investigator:
    def __init__(self, service: "DriftService"):
        self.service = service
        self.store = service.store
        self.cfg = service.settings["agent"]
        self._client = None
        self._client_key: tuple | None = None
        self._pinned = False

    def set_client(self, client) -> None:
        """Use a specific client (e.g. a scripted fake in tests) instead of building one from settings."""
        self._client, self._pinned = client, True

    @property
    def client(self):
        if self._pinned:
            return self._client
        llm = self.service.settings.llm
        key = (llm.api_key, llm.model, llm.base_url)
        if self._client is None or key != self._client_key:
            from drift_agent.agent.llm import GrokClient

            self._client, self._client_key = GrokClient(llm), key
        return self._client

    def investigate(self, run_id: int, report: DriftReport) -> dict:
        tools = ToolBox(self.service, run_id, report)
        started = time.time()
        source, llm_model, error = "rules", None, None
        result: InvestigationReport | None = None
        self._stats = {"llm_turns": 0, "llm_tokens": 0, "reminders": 0}
        if self.service.use_llm:
            try:
                result = self._run_llm(run_id, report, tools)
                source, llm_model = "llm", self.client.model
            except Exception as exc:  # network, auth, bad model name, invalid output...
                error = f"{type(exc).__name__}: {exc}"
                self.store.audit("agent", "llm_failed", {"error": error, "fallback": "rules"}, run_id)
        if result is None:
            result = fallback.investigate(tools)

        called = tools.called_tools()
        unverified = sorted({ev.tool for rc in result.root_causes for ev in rc.evidence} - called)
        # Drop features the model invented; keep a record of them.
        known = set(self.service.settings.features)
        unknown_features = [f for f in result.affected_features if f not in known]
        result.affected_features = [f for f in result.affected_features if f in known]
        missing_required = sorted(set(self.cfg["required_tools"]) - called) if source == "llm" else []
        payload = {
            **result.model_dump(),
            "meta": {
                "source": source, "llm_model": llm_model, "llm_error": error,
                "tool_calls": [{"tool": c["tool"], "args": c["args"]} for c in tools.calls],
                "unverified_evidence_tools": unverified,
                "missing_required_tools": missing_required,
                "unknown_features": unknown_features,
                **self._stats,
                "duration_s": round(time.time() - started, 2),
            },
        }
        self.store.add_report(run_id, source, llm_model, payload)
        self.store.audit("agent", "report_written", {
            "source": source, "drift_type": result.drift_type,
            "recommended_action": result.recommended_action, "summary": result.summary,
            "unverified_evidence_tools": unverified, "missing_required_tools": missing_required,
            "unknown_features": unknown_features,
        }, run_id)
        return payload

    # ---- LLM loop -------------------------------------------------------------
    def _run_llm(self, run_id: int, report: DriftReport, tools: ToolBox) -> InvestigationReport:
        max_steps = int(self.cfg["max_steps"])
        temperature = float(self.cfg["temperature"])
        messages: list[dict] = [
            {"role": "system", "content": system_prompt(self.service.settings,
                                                        self.store.get_model(report.model_version))},
            {"role": "user", "content": user_prompt(report.batch_id, report.severity, report.reasons)},
        ]
        required = set(self.cfg["required_tools"])
        max_reminders = int(self.cfg["required_tool_reminders"])
        tool_calls_made, repaired = 0, False
        for _ in range(max_steps + max_reminders + 3):
            missing = required - tools.called_tools()
            # Required tools may still be called after the budget is spent.
            allow_tools = tool_calls_made < max_steps or bool(missing)
            resp = self.client.chat(messages, tools=openai_tools(self.service.settings) if allow_tools else None,
                                    temperature=temperature)
            msg = resp.choices[0].message
            usage = getattr(resp, "usage", None)
            self._stats["llm_turns"] += 1
            self._stats["llm_tokens"] += (usage.total_tokens or 0) if usage else 0
            self.store.audit("agent", "llm_message", {
                "content": msg.content, "tool_calls": [
                    {"name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (msg.tool_calls or [])],
                "tokens": usage.total_tokens if usage else None,
            }, run_id)

            if msg.tool_calls and allow_tools:
                messages.append({
                    "role": "assistant", "content": msg.content or "",
                    "tool_calls": [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                   for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    output = tools.call(tc.function.name, args)
                    tool_calls_made += 1
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(output, default=str)})
                if tool_calls_made >= max_steps:
                    messages.append({"role": "user", "content": "Tool budget used up. Return the final JSON "
                                                                "incident report now."})
                continue

            missing = required - tools.called_tools()
            if missing and self._stats["reminders"] < max_reminders:
                self._stats["reminders"] += 1
                names = ", ".join(sorted(missing))
                self.store.audit("agent", "required_tools_reminder", {"missing": sorted(missing)}, run_id)
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": f"Before concluding you must call: {names}. Call "
                                                            "them, revise your findings if needed, then return "
                                                            "the JSON report."})
                continue
            try:
                return InvestigationReport.model_validate(extract_json(msg.content or ""))
            except (ValueError, ValidationError) as exc:
                if repaired:
                    raise
                repaired = True
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": f"That answer is not valid: {exc}. Reply with ONLY "
                                                            "the JSON object matching the schema."})
        raise RuntimeError("investigation did not converge")
