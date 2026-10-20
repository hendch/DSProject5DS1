"""What happens to a proposal once the guardrail and the humans have decided.

Every executor is idempotent (it runs at most once per action, recorded in
run_actions.execution) and anything that would touch GitHub stays a dry run unless
ORCHESTRATOR_GITHUB_PUBLISH=true.
"""
import os

ADVISORY = {"rank_causes", "review_code", "drift_report"}


def execute(action: dict) -> tuple[str, dict]:
    """Returns (execution, result) for one decided proposal."""
    kind, payload, decision = action["action_type"], action.get("payload") or {}, action["decision"]
    if decision != "approved":
        return "skipped", {"message": "Rejected or expired: nothing was executed."}
    if kind in ADVISORY:
        return "not_needed", {"message": "Advice only: nothing to execute."}
    if kind == "propose_fix":
        return "skipped", {"message": "Approved. The fix is an explanation, not a code patch: "
                                      "the owner applies it by hand."}
    if kind == "flag_conflict":
        return "skipped", {"message": "Resolution approved. Merge by hand: the assistant never merges."}
    if kind == "propose_patch":
        branch = payload.get("branch", "bot/minimal-fix")
        if os.getenv("ORCHESTRATOR_GITHUB_PUBLISH", "false").lower() == "true":
            return "failed", {"message": "Publishing to GitHub needs the gh CLI on this machine; not done."}
        return "dry_run", {"message": f"Dry run: would push {branch} and open a draft pull request for "
                                      f"PR #{payload.get('pr')} (publishing is off)."}
    if kind in ("promote_model", "rollback_model"):
        from agents.orchestrator.adapters import drift_service
        try:
            svc = drift_service()
            svc.guardrail.approve(payload["drift_action_id"], by=f"guardrail:{action.get('decided_by')}")
        except Exception as e:  # noqa: BLE001
            return "failed", {"message": f"The drift agent could not apply it: {e}"}
        return "done", {"message": f"{kind.replace('_', ' ').capitalize()} applied by the drift agent."}
    return "skipped", {"message": f"No executor for {kind}: nothing was done."}


def on_rejected(action: dict) -> None:
    """Tell an agent with its own pending queue that the guardrail said no."""
    if action["action_type"] in ("promote_model", "rollback_model"):
        try:
            from agents.orchestrator.adapters import drift_service
            drift_service().guardrail.reject(action["payload"]["drift_action_id"],
                                             by=f"guardrail:{action.get('decided_by')}")
        except Exception as e:  # noqa: BLE001
            print(f"[orchestrator] could not tell the drift agent about the rejection: {e}")
