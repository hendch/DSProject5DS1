"""Guardrail: turns the agent's recommendation into actions according to a fixed risk policy.

The agent can only recommend. This module decides what runs automatically and
what waits in the approval queue for a human. Actions not listed in POLICY do
not exist, so there is no code path for anything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from drift_agent.guardrail.challenger import train_challenger

if TYPE_CHECKING:
    from drift_agent.service import DriftService

# action -> (risk, mode)
POLICY: dict[str, tuple[str, str]] = {
    "raise_alert": ("none", "auto"),
    "train_challenger": ("low", "auto"),
    "promote_model": ("high", "approval"),
    "rollback_model": ("high", "approval"),
}



class Guardrail:
    def __init__(self, service: "DriftService"):
        self.service = service
        self.store = service.store

    # ---- from agent recommendation -------------------------------------------
    def handle_recommendation(self, run_id: int, investigation: dict) -> list[dict]:
        rec = investigation["recommended_action"]
        drift_type = investigation["drift_type"]
        self.store.audit("guardrail", "recommendation_received",
                         {"recommended_action": rec, "drift_type": drift_type}, run_id)
        actions = []
        if rec != "no_action":
            actions.append(self._auto(run_id, "raise_alert", {
                "to": self._route(drift_type),
                "title": f"{drift_type.replace('_', ' ')} detected",
                "summary": investigation["summary"],
                "recommended_action": rec,
            }, lambda p: {"delivered": True, "channel": "dashboard"}))

        pending = [a for a in self.store.list_actions(status="pending") if a["action"] == "promote_model"]
        if rec == "retrain_challenger" and pending:
            self.store.audit("guardrail", "challenger_skipped", {
                "reason": f"challenger {pending[0]['payload']['version']} is already waiting for approval "
                          f"(action #{pending[0]['id']})"}, run_id)
        elif rec == "retrain_challenger":
            actions.append(self._auto(run_id, "train_challenger", {"reason": investigation["action_rationale"]},
                                      lambda p: train_challenger(self.service)))
            result = actions[-1]["result"] or {}
            if result.get("trained") and result.get("better"):
                actions.append(self._request(run_id, "promote_model", {
                    "version": result["challenger_version"], "replaces": result["champion_version"],
                    "auc_gain": result["auc_gain"], "champion_metrics": result["champion_metrics"],
                    "challenger_metrics": result["challenger_metrics"],
                    "holdout_batches": result["holdout_batches"],
                }))
            elif result.get("trained"):
                self.store.audit("guardrail", "promotion_not_requested", {
                    "reason": f"challenger AUC gain {result['auc_gain']} below required {result['min_auc_gain']}",
                }, run_id)
        elif rec == "rollback":
            current = self.store.current_model()
            if current and current["parent"]:
                actions.append(self._request(run_id, "rollback_model", {
                    "from_version": current["version"], "to_version": current["parent"],
                    "reason": investigation["action_rationale"],
                }))
            else:
                self.store.audit("guardrail", "rollback_impossible",
                                 {"reason": "current model has no previous version"}, run_id)
        return actions

    def _route(self, drift_type: str) -> str:
        routing = self.service.settings["alerts"]["routing"]
        return routing.get(drift_type, routing["default"])

    # ---- execution ---------------------------------------------------------
    def _auto(self, run_id: int, action: str, payload: dict, execute) -> dict:
        risk, mode = POLICY[action]
        assert mode == "auto", f"{action} is not allowed to run automatically"
        action_id = self.store.add_action(run_id, action, risk, "running", payload)
        try:
            result = execute(payload)
            status = "done"
        except Exception as exc:
            result, status = {"error": f"{type(exc).__name__}: {exc}"}, "failed"
        self.store.update_action(action_id, status, result=result, decided_by="guardrail (auto)")
        self.store.audit("guardrail", f"action_{status}", {"action": action, "risk": risk, "action_id": action_id,
                                                           "result": result}, run_id)
        return self.store.get_action(action_id)

    def _request(self, run_id: int, action: str, payload: dict) -> dict:
        risk, mode = POLICY[action]
        assert mode == "approval"
        action_id = self.store.add_action(run_id, action, risk, "pending", payload)
        self.store.audit("guardrail", "approval_requested", {"action": action, "risk": risk,
                                                             "action_id": action_id, "payload": payload}, run_id)
        return self.store.get_action(action_id)

    # ---- human decisions ----------------------------------------------------
    def approve(self, action_id: int, by: str = "human", note: str | None = None) -> dict:
        action = self._pending(action_id)
        if action["action"] == "promote_model":
            version = action["payload"]["version"]
            self.store.set_current_model(version)
            result = {"current_model": version, "previous_model": action["payload"]["replaces"],
                      "undo": f"rollback to {action['payload']['replaces']}"}
        elif action["action"] == "rollback_model":
            version = action["payload"]["to_version"]
            self.store.set_current_model(version)
            result = {"current_model": version, "previous_model": action["payload"]["from_version"]}
        else:
            raise ValueError(f"action {action['action']} cannot be approved")
        self.store.update_action(action_id, "approved", result=result, decided_by=by, note=note)
        self.store.audit("human", "approved", {"action_id": action_id, "action": action["action"],
                                               "by": by, "note": note, "result": result}, action["run_id"])
        return self.store.get_action(action_id)

    def reject(self, action_id: int, by: str = "human", note: str | None = None) -> dict:
        action = self._pending(action_id)
        self.store.update_action(action_id, "rejected", decided_by=by, note=note)
        self.store.audit("human", "rejected", {"action_id": action_id, "action": action["action"],
                                               "by": by, "note": note}, action["run_id"])
        return self.store.get_action(action_id)

    def request_rollback(self, by: str = "human") -> dict:
        """Manual undo: a human asks to go back to the previous model (still needs approval)."""
        current = self.store.current_model()
        if not current or not current["parent"]:
            raise ValueError("the current model has no previous version to roll back to")
        action = self._request(None, "rollback_model", {"from_version": current["version"],
                                                        "to_version": current["parent"],
                                                        "reason": f"manual rollback requested by {by}"})
        return action

    def _pending(self, action_id: int) -> dict:
        action = self.store.get_action(action_id)
        if action is None:
            raise KeyError(f"action {action_id} not found")
        if action["status"] != "pending":
            raise ValueError(f"action {action_id} is already {action['status']}")
        return action
