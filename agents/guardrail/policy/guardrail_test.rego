# Run: docker run --rm -v "${PWD}/agents/guardrail/policy:/policy" openpolicyagent/opa:1.20.2 test /policy -v
package guardrail_test

import rego.v1

import data.guardrail

ev(agent, action, env, risk) := {"agent_name": agent, "action_type": action, "environment": env, "risk": risk}

rule_for(agent, action, env, risk) := d.rule if {
	d := guardrail.decision with input as ev(agent, action, env, risk)
}

# The full environment x risk matrix for a change (propose_fix).
test_dev_low_auto if rule_for("dependency_resolver", "propose_fix", "dev", "low") == "low_risk_auto"

test_dev_medium_auto if rule_for("dependency_resolver", "propose_fix", "dev", "medium") == "dev_medium_auto"

test_dev_high_human if rule_for("dependency_resolver", "propose_fix", "dev", "high") == "high_always_human"

test_staging_low_auto if rule_for("dependency_resolver", "propose_fix", "staging", "low") == "low_risk_auto"

test_staging_medium_human if rule_for("dependency_resolver", "propose_fix", "staging", "medium") == "staging_medium_human"

test_staging_high_human if rule_for("dependency_resolver", "propose_fix", "staging", "high") == "high_always_human"

test_prod_low_human if rule_for("dependency_resolver", "propose_fix", "prod", "low") == "prod_requires_human"

test_prod_medium_human if rule_for("dependency_resolver", "propose_fix", "prod", "medium") == "prod_requires_human"

test_prod_high_two_people if {
	d := guardrail.decision with input as ev("dependency_resolver", "propose_fix", "prod", "high")
	d.rule == "prod_high_two_person"
	d.required_approvals == 2
}

# Advice is auto-approved, even in prod, unless it is labelled high.
test_advisory_prod_auto if rule_for("diagnostic_rca", "rank_causes", "prod", "medium") == "advisory_only"

test_advisory_high_still_human if rule_for("diagnostic_rca", "rank_causes", "dev", "high") == "high_always_human"

# Agents can only emit their own action types.
test_wrong_action_rejected if {
	d := guardrail.decision with input as ev("merge_assistant", "propose_fix", "dev", "low")
	d.decision == "reject"
}

test_unknown_agent_rejected if rule_for("someone_else", "propose_fix", "dev", "low") == "agent_not_allowed_action"

# Unknown values fall through to the fail-closed default.
test_unknown_env_holds if rule_for("dependency_resolver", "propose_fix", "moon", "low") == "default_hold"

test_missing_risk_holds if {
	d := guardrail.decision with input as {"agent_name": "dependency_resolver", "action_type": "propose_fix", "environment": "dev"}
	d.decision == "hold"
}

# The code reviewer and drift agents (added with the orchestrator).
test_code_review_is_advice if rule_for("code_reviewer", "review_code", "dev", "low") == "advisory_only"

test_patch_to_shared_repo_needs_human if rule_for("code_reviewer", "propose_patch", "staging", "medium") == "staging_medium_human"

test_drift_report_is_advice if rule_for("drift_detection", "drift_report", "prod", "low") == "advisory_only"

test_model_promotion_needs_two_people if {
	d := guardrail.decision with input as ev("drift_detection", "promote_model", "prod", "high")
	d.required_approvals == 2
}

test_drift_cannot_propose_code_fixes if rule_for("drift_detection", "propose_fix", "dev", "low") == "agent_not_allowed_action"
