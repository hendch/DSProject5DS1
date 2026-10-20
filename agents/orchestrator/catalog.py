"""What the orchestrator knows: the agents, the routes (which agents handle which kind
of trigger) and the demo scenarios offered on the dashboard. Plain data, no logic."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

AGENTS = {
    "dependency_resolver": {
        "title": "Dependency Resolver", "owner": "Lyna",
        "description": "Finds circular dependencies in OpenTofu code, classifies them and explains the fix.",
        "actions": ["propose_fix"]},
    "diagnostic_rca": {
        "title": "Diagnostic / RCA", "owner": "zrharounesprit",
        "description": "Reads a failed build log, searches the knowledge base and ranks the likely root causes.",
        "actions": ["rank_causes"]},
    "merge_assistant": {
        "title": "Merge Assistant", "owner": "Muaad",
        "description": "Detects git merge conflicts in a scratch copy and proposes a resolution. Never merges.",
        "actions": ["flag_conflict"]},
    "code_reviewer": {
        "title": "Code Reviewer", "owner": "hendch",
        "description": "Reviews a pull request for ML/MLOps mistakes and proposes safe lint fixes as a draft PR.",
        "actions": ["review_code", "propose_patch"]},
    "drift_detection": {
        "title": "Drift Detection", "owner": "omar",
        "description": "Monitors a deployed model for data and concept drift and proposes retrain, promote or rollback.",
        "actions": ["drift_report", "promote_model", "rollback_model"]},
}

# A route is the ordered list of agents for one kind of trigger. "when" makes a step
# conditional on what the earlier steps found (see engine.STEP_CONDITIONS).
ROUTES = {
    "build_failed": {
        "title": "Failed CI build", "steps": [{"agent": "diagnostic_rca"}],
        "description": "A CI build or test run failed; find out why.",
        "keywords": ["build failed", "test failed", "tests failed", "pytest", "ci failed", "traceback",
                     "connection refused", "exit code"]},
    "deploy_failed": {
        "title": "Failed deployment", "steps": [{"agent": "dependency_resolver"}, {"agent": "diagnostic_rca"}],
        "description": "An infrastructure deployment (OpenTofu/Terraform) failed.",
        "keywords": ["terraform", "tofu", "deploy failed", "deployment failed", "apply failed", "cycle"]},
    "merge_requested": {
        "title": "Merge two branches", "steps": [{"agent": "merge_assistant"}],
        "description": "Two branches must be merged; check for conflicts.",
        "keywords": ["merge", "conflict", "rebase"]},
    "pull_request": {
        "title": "Pull request review",
        "steps": [{"agent": "diagnostic_rca", "when": "tests_failed"}, {"agent": "code_reviewer"}],
        "description": "A pull request was opened or updated; review it (diagnosing failing tests first).",
        "keywords": ["pull request", "review", "pr #", "code review"]},
    "drift_check": {
        "title": "Model drift check",
        "steps": [{"agent": "drift_detection"}, {"agent": "diagnostic_rca", "when": "pipeline_bug"}],
        "description": "Check the deployed model's latest data for drift.",
        "keywords": ["drift", "model performance", "auc", "data quality", "accuracy dropped"]},
}

SAMPLE_BUILD_LOG = ROOT / "agents" / "diagnostic_rca" / "logs" / "sample_failed_build.log"
DEPLOY_LOG = """\
Run tofu apply -auto-approve
Error: Cycle: aws_iam_role.checkout, aws_eks_cluster.checkout, aws_iam_role_policy.checkout
  on main.tf: resources depend on each other, so OpenTofu cannot order them.
Error: Process completed with exit code 1.
"""

SCENARIOS = [
    {"id": "review_pr", "route": "pull_request", "title": "Review a pull request",
     "description": "A data scientist's PR fits the scaler before the train/test split (data leakage).",
     "params": {"pr": 12, "scenario": "leaky_pr"}},
    {"id": "failed_deploy", "route": "deploy_failed", "title": "Failed deploy: IAM cycle",
     "description": "Friday 17:40, tofu apply fails on a cycle between an IAM role, its policy and a cluster.",
     "params": {"tf_dir": str(ROOT / "demo" / "infra" / "iam_cycle"), "log_text": DEPLOY_LOG}},
    {"id": "failed_build", "route": "build_failed", "title": "Failed CI build",
     "description": "pytest can't reach the database in CI: find the root cause.",
     "params": {"log_path": str(SAMPLE_BUILD_LOG)}},
    {"id": "merge", "route": "merge_requested", "title": "Merge two branches",
     "description": "feature-a and feature-b both change TIMEOUT in config.py.",
     "params": {"branch_a": "feature-a", "branch_b": "feature-b"}},
    {"id": "drift", "route": "drift_check", "title": "Check model drift",
     "description": "Run the drift detector on the next production batch.",
     "params": {}},
]
SCENARIOS_BY_ID = {s["id"]: s for s in SCENARIOS}
