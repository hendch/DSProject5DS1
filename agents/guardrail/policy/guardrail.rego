# Layer 1: the policy gate. Evaluated by OPA (Open Policy Agent).
#
# Input:  the event fields + "risk" (computed by risk.py, never lower than
#         the agent's own blast_radius label).
# Output: data.guardrail.decision =
#         {decision: auto_approve | hold | reject, rule, required_approvals, reason}
#
# Rules are tried top to bottom; the first match wins. Anything unmatched
# falls through to the default, which holds for a human (fail closed).
package guardrail

import rego.v1

# Which action types each agent is allowed to propose.
allowed_actions := {
	"dependency_resolver": {"propose_fix"},
	"diagnostic_rca": {"rank_causes"},
	"merge_assistant": {"flag_conflict"},
}

# Action types that only give advice and change nothing.
advisory := {"rank_causes"}

default decision := {
	"decision": "hold",
	"rule": "default_hold",
	"required_approvals": 1,
	"reason": "No rule matched, so a human must decide (fail closed).",
}

decision := {
	"decision": "reject",
	"rule": "agent_not_allowed_action",
	"required_approvals": 0,
	"reason": sprintf("%s is not allowed to emit %s.", [input.agent_name, input.action_type]),
} if {
	not input.action_type in object.get(allowed_actions, input.agent_name, set())
} else := {
	"decision": "hold",
	"rule": "prod_high_two_person",
	"required_approvals": 2,
	"reason": "High-risk change in production: two different people must approve.",
} if {
	input.risk == "high"
	input.environment == "prod"
} else := {
	"decision": "hold",
	"rule": "high_always_human",
	"required_approvals": 1,
	"reason": "High risk always needs a human, whatever the environment.",
} if {
	input.risk == "high"
} else := {
	"decision": "auto_approve",
	"rule": "advisory_only",
	"required_approvals": 0,
	"reason": "Advice only: nothing is changed by approving it.",
} if {
	input.action_type in advisory
} else := {
	"decision": "hold",
	"rule": "prod_requires_human",
	"required_approvals": 1,
	"reason": "Every change in production needs a human.",
} if {
	input.environment == "prod"
} else := {
	"decision": "hold",
	"rule": "staging_medium_human",
	"required_approvals": 1,
	"reason": "Medium risk in staging needs a human.",
} if {
	input.environment == "staging"
	input.risk == "medium"
} else := {
	"decision": "auto_approve",
	"rule": "low_risk_auto",
	"required_approvals": 0,
	"reason": "Low risk outside production.",
} if {
	input.environment in {"dev", "staging"}
	input.risk == "low"
} else := {
	"decision": "auto_approve",
	"rule": "dev_medium_auto",
	"required_approvals": 0,
	"reason": "Medium risk in dev is easy to undo.",
} if {
	input.environment == "dev"
	input.risk == "medium"
}
