"""The orchestrator end to end on a real database and the real guardrail + OPA policy.
Agents are replaced by stubs so the tests are fast and need no LLM; their real
behaviour is covered by each agent's own tests."""
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agents.guardrail import agent as guardrail
from agents.guardrail import llm_judge, policy_client
from agents.guardrail import store as guardrail_store
from agents.guardrail.announce import announce
from agents.orchestrator import adapters, executors, github, routing, store
from agents.orchestrator.adapters import Proposal, StepResult
from agents.orchestrator.catalog import ROUTES, SCENARIOS
from agents.orchestrator.engine import Orchestrator
from shared import alerts
from shared.devstack import start_opa
from shared.llm import LLMUnavailable
from shared.testing import DbTestCase

_opa = {}


def setUpModule():
    _opa["url"], _opa["stop"] = start_opa()


def tearDownModule():
    _opa["stop"]()


RCA = StepResult("done", "Top cause: DATABASE_URL not set", proposals=[Proposal("rank_causes", {
    "failure_summary": "tests failed", "ranked_causes": [{"cause": "DATABASE_URL not set", "confidence": 0.8,
                                                          "source": "none"}]})], facts={"rca_summary": "x"})
IAM_FIX = StepResult("degraded", "1 cycle(s)", proposals=[Proposal("propose_fix", {
    "cycle": ["aws_iam_role.checkout", "aws_eks_cluster.checkout"], "category": "modeling_mistake",
    "proposed_diff": "split it", "triggered_by": "sami"})])


class Base(DbTestCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(policy_client, "OPA_URL", _opa["url"]),
                  mock.patch.object(llm_judge, "ENABLED", False),
                  mock.patch("agents.orchestrator.engine.for_agent", side_effect=LLMUnavailable("off")),
                  mock.patch.dict(adapters.ADAPTERS, {
                      "diagnostic_rca": lambda params, ctx: RCA,
                      "dependency_resolver": lambda params, ctx: IAM_FIX})):
            p.start()
            self.addCleanup(p.stop)
        self.orch = Orchestrator(workers=1)  # one worker: tasks run in submission order
        self.addCleanup(self.orch.pool.shutdown)

    def settle(self):
        """Let the guardrail decide, deliver its decisions, and finish queued work."""
        for _ in range(3):
            guardrail.run(once=True)
            self.orch.consumer().poll()
            self.orch.pool.submit(lambda: None).result()

    def run_of(self, route, **extra):
        return self.orch.start({"title": f"test {route}", "source": "scenario", "route": route, "params": {}, **extra},
                               requested_by="sami", wait=True)

    def actions(self, run_id):
        return {a["action_type"]: a for a in store.get_actions(run_id)}


class Lifecycle(Base):
    def test_advice_is_approved_and_the_run_completes_by_itself(self):
        run_id = self.run_of("build_failed")
        self.assertEqual(store.get_run(run_id)["status"], "waiting_approval")
        self.settle()
        run = store.get_run(run_id)
        self.assertEqual(run["status"], "completed")
        a = self.actions(run_id)["rank_causes"]
        self.assertEqual((a["decision"], a["execution"]), ("approved", "not_needed"))
        self.assertIn("1 proposal(s): 1 approved", run["summary"])
        self.assertEqual(self.sql("SELECT count(*) FROM verify_reasoning_chain()"), [(0,)])

    def test_high_risk_waits_for_a_human_then_executes(self):
        run_id = self.run_of("deploy_failed")
        self.settle()
        self.assertEqual(store.get_run(run_id)["status"], "waiting_approval")
        fix = self.actions(run_id)["propose_fix"]
        self.assertEqual((fix["decision"], fix["risk"], fix["policy_rule"]), ("pending", "high", "high_always_human"))
        with self.assertRaises(guardrail_store.ApprovalError):  # sami triggered it
            guardrail_store.cast_vote(str(fix["approval_id"]), "sami", "approve")
        result = guardrail_store.cast_vote(str(fix["approval_id"]), "ines", "approve")
        announce(result["correlation_id"], result["environment"], result["risk"], result["action_ref"],
                 result["status"], result["decided_by"])
        self.settle()
        self.assertEqual(store.get_run(run_id)["status"], "completed")
        self.assertEqual(self.actions(run_id)["propose_fix"]["execution"], "skipped")  # applied by hand
        titles = [a["title"] for a in alerts.list_alerts()]
        self.assertIn("Approval needed", titles)

    def test_rejected_proposal_is_never_executed(self):
        run_id = self.run_of("deploy_failed")
        self.settle()
        fix = self.actions(run_id)["propose_fix"]
        result = guardrail_store.cast_vote(str(fix["approval_id"]), "ines", "reject")
        announce(result["correlation_id"], result["environment"], result["risk"], result["action_ref"],
                 result["status"], result["decided_by"])
        self.settle()
        fix = self.actions(run_id)["propose_fix"]
        self.assertEqual((fix["decision"], fix["execution"]), ("rejected", "skipped"))

    def test_decisions_reach_the_run_even_without_the_event(self):
        run_id = self.run_of("build_failed")
        guardrail.run(once=True)  # decided, but the orchestrator never reads the event
        self.orch.sweep()
        self.orch.pool.submit(lambda: None).result()
        self.assertEqual(store.get_run(run_id)["status"], "completed")

    def test_a_crashing_agent_does_not_stop_the_others(self):
        def boom(params, ctx):
            raise RuntimeError("agent bug")
        with mock.patch.dict(adapters.ADAPTERS, {"dependency_resolver": boom}):
            run_id = self.run_of("deploy_failed")
        steps = {s["agent"]: s for s in store.get_steps(run_id)}
        self.assertEqual(steps["dependency_resolver"]["status"], "failed")
        self.assertEqual(steps["diagnostic_rca"]["status"], "done")
        self.settle()
        self.assertEqual(store.get_run(run_id)["status"], "completed")
        self.assertIn("Dependency Resolver failed", [a["title"] for a in alerts.list_alerts()])

    def test_run_with_no_proposals_completes(self):
        with mock.patch.dict(adapters.ADAPTERS, {"diagnostic_rca": lambda p, c: StepResult("skipped", "no log")}):
            run_id = self.run_of("build_failed")
        self.assertEqual(store.get_run(run_id)["status"], "completed")

    def test_conditional_step_is_skipped(self):
        reviewer = StepResult("done", "no issues")
        with mock.patch.dict(adapters.ADAPTERS, {"code_reviewer": lambda p, c: reviewer}), \
             mock.patch.dict(adapters.PREPARE, {"pull_request": lambda p, c: c["facts"].update(tests_failed=False)}):
            run_id = self.run_of("pull_request")
        steps = {s["agent"]: s["status"] for s in store.get_steps(run_id)}
        self.assertEqual(steps, {"diagnostic_rca": "skipped", "code_reviewer": "done"})

    def test_restart_fails_interrupted_runs_visibly(self):
        run_id = str(uuid.uuid4())
        store.create_run(run_id, "build_failed", {"title": "half done"})
        store.update_run(run_id, status="running")
        self.orch.recover()
        self.assertEqual(store.get_run(run_id)["status"], "failed")
        self.assertIn("Run interrupted", [a["title"] for a in alerts.list_alerts()])


class Routing(Base):
    def test_free_text_matching_one_route_is_routed_by_rule(self):
        run_id = self.orch.start({"title": "x", "source": "text", "text": "pytest failed with exit code 1"}, wait=True)
        run = store.get_run(run_id)
        self.assertEqual((run["route"], run["routed_by"]), ("build_failed", "rule"))
        self.assertEqual(run["trigger"]["params"]["log_text"], "pytest failed with exit code 1")

    def test_unclear_text_waits_for_a_human_then_runs(self):
        with mock.patch.object(routing, "llm_triage", side_effect=LLMUnavailable("down")):
            run_id = self.orch.start({"title": "x", "source": "text", "text": "the site is slow"}, wait=True)
        self.assertEqual(store.get_run(run_id)["status"], "needs_triage")
        self.orch.triage(run_id, "build_failed", "ines", wait=True)
        run = store.get_run(run_id)
        self.assertEqual((run["route"], run["routed_by"]), ("build_failed", "human"))
        with self.assertRaises(ValueError):  # already triaged
            self.orch.triage(run_id, "build_failed", "ines")

    def test_llm_triage_needs_confidence_and_a_known_route(self):
        text = {"text": "the checkout page throws 500s since this morning"}
        with mock.patch.object(routing, "llm_triage", return_value=("build_failed", 0.9, "logs")):
            self.assertEqual(routing.choose_route(text)["routed_by"], "llm")
        with mock.patch.object(routing, "llm_triage", return_value=("build_failed", 0.4, "unsure")):
            self.assertIsNone(routing.choose_route(text)["route"])
        model = mock.Mock()
        model.chat_json.return_value = {"route": "delete_prod", "confidence": 0.99}
        with mock.patch.object(routing, "for_agent", return_value=model):
            self.assertIsNone(routing.choose_route(text)["route"])  # invented routes are refused

    def test_every_scenario_uses_a_known_route(self):
        for s in SCENARIOS:
            self.assertIn(s["route"], ROUTES)


class Executors(unittest.TestCase):
    def test_outcomes(self):
        base = {"payload": {"pr": 3, "branch": "bot/minimal-fix/pr-3"}, "decided_by": "ines"}
        cases = {("rank_causes", "approved"): "not_needed", ("propose_fix", "approved"): "skipped",
                 ("propose_patch", "approved"): "dry_run", ("propose_fix", "rejected"): "skipped"}
        for (kind, decision), expected in cases.items():
            self.assertEqual(executors.execute({**base, "action_type": kind, "decision": decision})[0], expected)

    def test_model_change_fails_cleanly_when_drift_is_missing(self):
        with mock.patch.object(adapters, "drift_service", side_effect=ModuleNotFoundError("data")):
            execution, result = executors.execute({"action_type": "promote_model", "decision": "approved",
                                                   "payload": {"drift_action_id": 1}, "decided_by": "ines"})
        self.assertEqual(execution, "failed")


class Api(Base):
    def setUp(self):
        super().setUp()
        from app.api import create_app
        self.client = TestClient(create_app(self.orch))

    def test_scenario_run_and_vote_through_the_api(self):
        run_id = self.client.post("/api/runs", json={"scenario_id": "failed_deploy", "requested_by": "sami"}).json()["run_id"]
        self.orch.pool.submit(lambda: None).result()
        self.settle()
        detail = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual(detail["run"]["status"], "waiting_approval")
        self.assertTrue(detail["timeline"])
        pending = self.client.get("/api/approvals").json()
        self.assertEqual(len(pending), 1)
        aid = pending[0]["id"]
        self.assertEqual(self.client.post(f"/api/approvals/{aid}/approve", json={"decided_by": "sami"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/approvals/{aid}/approve", json={"decided_by": "ines"}).json()["status"],
                         "approved")
        self.assertEqual(self.client.post(f"/api/approvals/{aid}/approve", json={"decided_by": "karim"}).status_code, 409)
        self.settle()
        self.assertEqual(self.client.get(f"/api/runs/{run_id}").json()["run"]["status"], "completed")

    def test_bad_inputs(self):
        self.assertEqual(self.client.post("/api/runs", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/runs", json={"scenario_id": "nope"}).status_code, 404)
        self.assertEqual(self.client.get("/api/runs/not-a-uuid").status_code, 422)
        self.assertEqual(self.client.get(f"/api/runs/{uuid.uuid4()}").status_code, 404)
        self.assertEqual(self.client.post("/api/github/investigate", json={"kind": "commit", "sha": "abc"}).status_code, 400)

    def test_dashboard_endpoints_answer(self):
        for path in ("/api/overview", "/api/scenarios", "/api/routes", "/api/runs", "/api/agents", "/api/audit",
                     "/api/alerts", "/api/analytics?days=7", "/api/github/status", "/api/github/activity",
                     "/api/github/repos"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertFalse(self.client.get("/api/github/status").json()["connected"])
        alerts.raise_alert("critical", "x")
        self.assertEqual(self.client.post("/api/alerts/read", json={"ids": "all"}).json(), {"marked": 1})


# ---- GitHub watcher with GitHub's API faked -------------------------------------------------------
def gh_run(run_id, sha, conclusion, created, branch="main"):
    return {"id": run_id, "head_sha": sha, "status": "completed", "conclusion": conclusion, "name": "CI",
            "html_url": f"https://github.com/o/r/actions/runs/{run_id}", "head_branch": branch,
            "created_at": created, "display_title": f"commit {sha}",
            "head_commit": {"message": f"commit {sha}", "timestamp": created}, "actor": {"login": "dev"}}


class FakeGitHub:
    def __init__(self):
        self.runs, self.pulls, self.status = [], [], 200

    def get(self, path, tok=None, params=None, raw=False):
        if self.status != 200:
            raise github.GitHubError(self.status, "GitHub rejected the token")
        if path == "/user":
            return {"login": "rayen", "avatar_url": None}
        if path == "/repos/o/r":
            return {"full_name": "o/r", "default_branch": "main", "private": False}
        if path.endswith("/actions/runs"):
            return {"workflow_runs": self.runs}
        if path.endswith("/commits"):
            return [{"sha": r["head_sha"], "html_url": "u", "author": {"login": "dev"},
                     "commit": {"message": f"commit {r['head_sha']}", "author": {"name": "dev", "date": r["created_at"]}}}
                    for r in self.runs]
        if path.endswith("/pulls"):
            return self.pulls
        if "/jobs" in path and path.endswith("/logs"):
            return "step 1\nERROR tests failed\nGITHUB_TOKEN=ghp_" + "x" * 36
        if path.endswith("/jobs"):
            return {"jobs": [{"id": 7, "name": "test", "conclusion": "failure", "steps": []}]}
        raise AssertionError(path)


class GitHubWatcher(DbTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeGitHub()
        self.orch = mock.Mock()
        self.orch.start.side_effect = lambda trigger, requested_by=None: str(uuid.uuid4())
        for p in (mock.patch.object(github, "_get", side_effect=self.fake.get),
                  mock.patch.dict("os.environ", {"GITHUB_TOKEN": ""})):
            p.start()
            self.addCleanup(p.stop)
        github._state.update(error=None, last_sync=None)
        self.fake.runs = [gh_run(1, "a1", "failure", "2026-10-01T10:00:00Z")]  # old history
        github.connect("o/r", "ghp_" + "t" * 36)

    def titles(self):
        return [a["title"] for a in alerts.list_alerts()]

    def test_connecting_does_not_alert_on_history(self):
        self.assertEqual(self.titles(), ["GitHub connected: o/r"])
        self.assertEqual(github.status()["repos"], ["o/r"])
        self.assertEqual(github.list_items()[0]["ci"], "failure")

    def test_new_failure_alerts_and_investigates_once_with_a_redacted_log(self):
        self.fake.runs.append(gh_run(2, "b2", "failure", "2026-10-02T10:00:00Z"))
        github.sync(orchestrator=self.orch)
        github.sync(orchestrator=self.orch)  # the same failure again: nothing new
        self.assertEqual(self.titles().count("CI failed on main"), 1)
        self.assertEqual(self.orch.start.call_count, 1)
        trigger = self.orch.start.call_args.args[0]
        self.assertEqual(trigger["route"], "build_failed")
        self.assertIn("ERROR tests failed", trigger["params"]["log_text"])
        self.assertNotIn("ghp_xxxx", trigger["params"]["log_text"])

    def test_failed_tofu_step_is_routed_as_a_deploy(self):
        self.fake.runs.append(gh_run(2, "b2", "failure", "2026-10-02T10:00:00Z"))
        with mock.patch.object(github, "failed_log",
                               return_value="Run tofu validate\nError: Cycle: aws_iam_role.a, aws_iam_role_policy.a"):
            github.sync(orchestrator=self.orch)
        trigger = self.orch.start.call_args.args[0]
        self.assertEqual(trigger["route"], "deploy_failed")
        self.assertTrue(trigger["title"].startswith("Deploy failed"))
        self.assertEqual((trigger["params"]["source"], trigger["params"]["repo"]), ("github", "o/r"))

    def test_tf_folder_named_in_the_log_is_chosen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("modules/net", "infra"):
                (root / d).mkdir(parents=True)
                (root / d / "main.tf").write_text("")
            self.assertEqual(github.find_tf_dir(root, "Error in infra/main.tf"), root / "infra")
            self.assertEqual(github.find_tf_dir(root, "no folder named"), root / "infra")  # shallowest first
            self.assertIsNone(github.find_tf_dir(root / "modules" / "net" / "nothing"))

    def test_recovery_is_announced(self):
        self.fake.runs.append(gh_run(2, "b2", "failure", "2026-10-02T10:00:00Z"))
        github.sync(orchestrator=self.orch)
        self.fake.runs.append(gh_run(3, "c3", "success", "2026-10-03T10:00:00Z"))
        github.sync(orchestrator=self.orch)
        self.assertIn("CI is green again on main", self.titles())

    def test_new_pull_request_commits_get_a_review(self):
        pr = {"number": 5, "title": "Add feature", "state": "open", "html_url": "h", "body": "",
              "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z", "merged_at": None,
              "user": {"login": "dev"}, "head": {"sha": "p1", "ref": "feat"}, "base": {"sha": "a1", "ref": "main"}}
        self.fake.pulls = [pr]
        with mock.patch.object(github, "investigate", wraps=lambda *a, **k: "run") as inv:
            github.sync(orchestrator=self.orch)
            github.sync(orchestrator=self.orch)
            self.assertEqual(inv.call_count, 1)
            self.fake.pulls = [{**pr, "head": {"sha": "p2", "ref": "feat"}, "updated_at": "2026-10-03T00:00:00Z"}]
            github.sync(orchestrator=self.orch)
            self.assertEqual(inv.call_count, 2)  # new commits: a new review

    def test_revoked_token_raises_a_critical_alert(self):
        self.fake.status = 401
        github.sync(orchestrator=self.orch)
        self.assertIn("GitHub token rejected", self.titles())
        self.assertIn("rejected", github.status()["error"])

    def test_investigating_a_green_commit_is_refused(self):
        self.fake.runs.append(gh_run(2, "b2", "success", "2026-10-02T10:00:00Z"))
        github.sync()
        with self.assertRaises(github.GitHubError) as ctx:
            github.investigate("commit", "b2", self.orch)
        self.assertEqual(ctx.exception.status, 409)


class BrowserSignIn(DbTestCase):
    """GitHub's OAuth device flow, with GitHub faked."""

    def setUp(self):
        super().setUp()
        github._device.clear()
        self.answers = []
        self.posts = mock.patch.object(github, "_oauth_post", side_effect=lambda url, data: self.answers.pop(0))
        self.posts.start()
        self.addCleanup(self.posts.stop)
        get = mock.patch.object(github, "_get", side_effect=lambda path, tok=None, params=None, raw=False:
                                {"login": "rayen", "avatar_url": None} if path == "/user" else
                                [{"full_name": "o/r", "private": False, "default_branch": "main"}])
        get.start()
        self.addCleanup(get.stop)

    def test_needs_a_client_id_first(self):
        with mock.patch.dict("os.environ", {"GITHUB_CLIENT_ID": ""}):
            with self.assertRaises(github.GitHubError):
                github.device_start()
            with self.assertRaises(github.GitHubError):
                github.set_client_id("not a client id!")

    def test_code_then_pending_then_signed_in(self):
        github.set_client_id("Ov23liABCDEFGH123456")
        self.answers = [{"device_code": "dc", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
                         "expires_in": 900, "interval": 0},
                        {"error": "authorization_pending"}, {"access_token": "gho_" + "x" * 36}]
        start = github.device_start()
        self.assertEqual(start["user_code"], "ABCD-1234")
        self.assertEqual(github.device_poll()["state"], "pending")
        done = github.device_poll()
        self.assertEqual(done["state"], "connected")
        status = github.status()
        self.assertTrue(status["signed_in"])
        self.assertFalse(status["connected"])  # signed in, but no repository chosen yet
        self.assertEqual((status["login"], status["auth"]), ("rayen", "oauth"))
        self.assertEqual(github.available_repos()[0]["repo"], "o/r")

    def test_denied_and_expired(self):
        github.set_client_id("Ov23liABCDEFGH123456")
        self.answers = [{"device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 0},
                        {"error": "access_denied"}]
        github.device_start()
        self.assertEqual(github.device_poll()["state"], "denied")
        self.assertEqual(github.device_poll()["state"], "idle")
        self.assertFalse(github.status()["signed_in"])
