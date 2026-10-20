"""One adapter per agent: run it on a task and return a StepResult (status, summary,
output, proposals, facts for later steps). Agents never publish here; the engine does.

Statuses: done | degraded (worked, but on a fallback: no LLM, no OpenTofu...) |
skipped (nothing to do for this input) | unavailable (the agent can't run) | failed.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from shared import tracing
from shared.llm import profile
from shared.redact import redact, tail

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Proposal:
    action_type: str
    payload: dict
    environment: str = "dev"
    blast_radius: str = "low"
    summary: str = ""


@dataclass
class StepResult:
    status: str
    summary: str
    output: dict = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def _requested_by(ctx: dict) -> dict:
    return {"triggered_by": ctx["requested_by"]} if ctx.get("requested_by") else {}


# ---- Diagnostic / RCA ---------------------------------------------------------------
def diagnostic_rca(params: dict, ctx: dict) -> StepResult:
    log = params.get("log_text") or ctx["facts"].get("failing_log")
    if not log and params.get("log_path"):
        log = Path(params["log_path"]).read_text(encoding="utf-8", errors="replace")
    if not log:
        return StepResult("skipped", "No build log to analyse in this trigger.")
    from agents.diagnostic_rca import agent as rca

    result = rca.analyze(tail(redact(log)))
    top = result["ranked_causes"][0]
    status = "degraded" if result["sources"]["ranking"] == "rules" else "done"
    note = " (rule-based: no LLM available)" if status == "degraded" else ""
    return StepResult(
        status, f"Top cause{note}: {top['cause']} ({top['confidence']:.0%})", output=result,
        proposals=[Proposal("rank_causes", result, params.get("environment", "dev"), "low",
                            f"top cause: {top['cause']}"[:200])],
        facts={"rca_summary": result["failure_summary"], "rca_top_cause": top["cause"]})


# ---- Dependency Resolver -----------------------------------------------------------------
def dependency_resolver(params: dict, ctx: dict) -> StepResult:
    tf_dir = params.get("tf_dir")
    if not tf_dir or not Path(tf_dir).is_dir():
        why = ctx["facts"].get("prepare_error")
        return StepResult("skipped", f"Couldn't get the .tf files: {why}" if why
                          else "No OpenTofu/Terraform folder in this trigger.")
    tf_files = sorted(Path(tf_dir).glob("*.tf"))
    if not tf_files:
        return StepResult("skipped", f"No .tf files in {Path(tf_dir).name}.")
    from agents.dependency_resolver import agent as resolver

    payloads = resolver.analyze(tf_dir, str(tf_files[0]))
    if not payloads:
        return StepResult("done", "No circular dependency found.")
    fallback = any(v in ("static", "heuristic") for p in payloads for v in p["sources"].values())
    proposals = [Proposal("propose_fix", {**p, **_requested_by(ctx)}, "dev", "low", p["category"])
                 for p in payloads]
    cycles = "; ".join(" -> ".join(p["cycle"]) for p in payloads)
    note = " (fallback: static analysis or keyword classifier)" if fallback else ""
    return StepResult("degraded" if fallback else "done",
                      f"{len(payloads)} cycle(s){note}: {cycles}", output={"cycles": payloads},
                      proposals=proposals)


# ---- Merge Assistant --------------------------------------------------------------------
def merge_assistant(params: dict, ctx: dict) -> StepResult:
    from agents.merge_assistant import agent as merger

    repo = params.get("repo_path")
    if not repo:  # demo: a scratch repo where both branches change TIMEOUT
        repo = Path(ctx["workdir"]) / "merge-demo"
        merger.make_demo_repo(repo)
    a, b = params.get("branch_a", "feature-a"), params.get("branch_b", "feature-b")
    result = merger.analyze(Path(repo), a, b)
    if result is None:
        return StepResult("done", f"{b} merges cleanly into {a}.")
    status = "degraded" if result["sources"]["resolution"] == "rules" else "done"
    return StepResult(status, result["conflict_summary"], output=result,
                      proposals=[Proposal("flag_conflict", {**result, **_requested_by(ctx)}, "dev", "low",
                                          result["conflict_summary"])])


# ---- Code Reviewer ----------------------------------------------------------------------
def _demo_module():
    spec = importlib.util.spec_from_file_location("run_local_demo", ROOT / "demo" / "run_local_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_pull_request(params: dict, ctx: dict) -> None:
    """Check out the PR and run the baseline lint + tests once, before the agents run, so
    RCA can diagnose failing tests and the reviewer can use the results."""
    workdir = Path(ctx["workdir"])
    reports = workdir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if params.get("source") == "github":
        from agents.orchestrator import github
        repo, base, head = github.checkout_pull_request(params, workdir)
        lint = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "--extend-select",
                               "I001,W291,W292,W293", "--output-format", "json", "--exit-zero", "."],
                              cwd=repo, capture_output=True, text=True, timeout=300)
        (reports / "lint.json").write_text(lint.stdout or "[]", encoding="utf-8")
        junit = None  # a whole repository's test suite is too slow to run here
        test_output = ""
    else:
        demo = _demo_module()
        repo, base, head = demo.make_repo(workdir, params.get("scenario", "leaky_pr"))
        demo.baseline(repo, reports)
        junit = reports / "junit.xml"
        test_output = junit.read_text(encoding="utf-8", errors="replace") if junit.exists() else ""
    failed = 'failures="0"' not in test_output and "<failure" in test_output
    ctx["facts"].update({"repo": str(repo), "base": base, "head": head, "lint": str(reports / "lint.json"),
                         "junit": str(junit) if junit and junit.exists() else None, "tests_failed": failed})
    if failed:
        ctx["facts"]["failing_log"] = test_output


def _run_reviewer(facts: dict, out: Path, review_json: Path | None, diagnosis: str | None) -> dict:
    argv = [sys.executable, "-m", "agents.code_reviewer.agent", "--repo", facts["repo"],
            "--base", facts["base"], "--head", facts["head"], "--lint", facts["lint"], "--out", str(out)]
    if facts.get("junit"):
        argv += ["--junit", facts["junit"]]
    if review_json:
        argv += ["--review-json", str(review_json)]
    if diagnosis:
        note = out.parent / "diagnosis.txt"
        note.write_text(diagnosis, encoding="utf-8")
        argv += ["--diagnosis", str(note)]
    prof = profile("code_reviewer")
    env = {**os.environ, "LLM_BACKEND": "openai", "LLM_BASE_URL": prof.base_url,
           "LLM_MODEL": prof.model, "LLM_API_KEY": prof.api_key, "PYTHONPATH": str(ROOT), **tracing.child_env()}
    subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    report = out / "report.json"
    return json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"status": "review_failed"}


def code_reviewer(params: dict, ctx: dict) -> StepResult:
    facts = ctx["facts"]
    if "repo" not in facts:
        return StepResult("skipped", "No pull request checked out.")
    workdir = Path(ctx["workdir"])
    report = _run_reviewer(facts, workdir / "review", None, facts.get("rca_summary"))
    status, note = "done", ""
    if report.get("status") == "review_failed":
        scripted = ROOT / "demo" / "scripted" / f"{params.get('scenario', 'leaky_pr')}.json"
        if params.get("source") != "github" and scripted.exists():
            report = _run_reviewer(facts, workdir / "review-offline", scripted, None)
            status, note = "degraded", " (no LLM: scripted demo review)"
        else:
            return StepResult("degraded", "The review needs an LLM and none was reachable; lint results only.",
                              output={"report": report})
    findings = report.get("findings", [])
    pr = params.get("pr")
    proposals = [Proposal("review_code", {
        "pr": pr, "status": report.get("status"), "summary": report.get("summary"),
        "findings": findings[:20], "model": report.get("model"), "fixed_files": report.get("fixed_files")},
        "dev", "low", (report.get("summary") or "")[:200])]
    patch = workdir / ("review-offline" if note else "review") / "fix.patch"
    if report.get("status") == "repaired" and patch.exists():
        text = patch.read_text(encoding="utf-8", errors="replace")
        proposals.append(Proposal("propose_patch", {
            "pr": pr, "branch": f"bot/minimal-fix/pr-{pr}", "fixed_files": report.get("fixed_files"),
            "changed_lines": sum(1 for l in text.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")),
            "patch": text[:8000], **_requested_by(ctx)},
            "staging", "medium", f"safe lint fixes for PR #{pr}"))
    high = [f for f in findings if f.get("severity") == "high"]
    return StepResult(status, f"{report.get('status')}: {len(findings)} finding(s), {len(high)} high{note}",
                      output={"report": {k: report.get(k) for k in
                                         ("status", "summary", "findings", "fixed_files", "validation", "model")}},
                      proposals=proposals)


# ---- Drift Detection ----------------------------------------------------------------------
_drift_service = None


def drift_service():
    """The drift agent's service, created once. Raises if the agent can't run."""
    global _drift_service
    if _drift_service is None:
        from agents.drift_detection.service import DriftService  # needs the agent's data package
        svc = DriftService()
        if svc.store.current_model() is None:
            raise RuntimeError("no model trained yet: run `drift-agent init` once")
        _drift_service = svc
    return _drift_service


def drift_detection(params: dict, ctx: dict) -> StepResult:
    try:
        svc = drift_service()
    except ModuleNotFoundError:
        return StepResult("unavailable", "The drift agent's data package isn't in the repository yet "
                                         "(agents/drift_detection/data).")
    except Exception as e:  # noqa: BLE001
        return StepResult("unavailable", f"The drift agent can't start: {e}")
    result = svc.step(investigate=True)
    inv = result.get("investigation") or {}
    proposals = [Proposal("drift_report", {
        "drift_run_id": result.get("run_id"), "batch_id": result.get("batch_id"),
        "severity": result.get("severity"), "score": result.get("score"), "reasons": result.get("reasons"),
        "summary": inv.get("summary"), "drift_type": inv.get("drift_type"),
        "recommended_action": inv.get("recommended_action")}, "prod", "low", str(result.get("severity")))]
    for a in result.get("actions") or []:
        if a.get("status") == "pending" and a.get("action") in ("promote_model", "rollback_model"):
            proposals.append(Proposal(a["action"], {"drift_action_id": a["id"], **(a.get("payload") or {})},
                                      "prod", "high", a["action"]))
    return StepResult("done", f"Batch {result.get('batch_id')}: severity {result.get('severity')}",
                      output=result, proposals=proposals, facts={"drift_type": inv.get("drift_type")})


ADAPTERS = {
    "diagnostic_rca": diagnostic_rca,
    "dependency_resolver": dependency_resolver,
    "merge_assistant": merge_assistant,
    "code_reviewer": code_reviewer,
    "drift_detection": drift_detection,
}
def prepare_deploy(params: dict, ctx: dict) -> None:
    """A deploy that failed on GitHub: check out the commit so the Dependency Resolver gets its .tf files."""
    if params.get("source") != "github" or params.get("tf_dir"):
        return  # scenarios and manual runs bring their own folder
    from agents.orchestrator import github

    root = github.checkout_commit(params["repo"], params["sha"], Path(ctx["workdir"]))
    tf_dir = github.find_tf_dir(root, params.get("log_text", ""))
    if tf_dir:
        params["tf_dir"] = str(tf_dir)


PREPARE = {"pull_request": prepare_pull_request, "deploy_failed": prepare_deploy}

# Conditional steps (catalog ROUTES "when"): run only if earlier steps found this.
CONDITIONS = {
    "tests_failed": lambda facts: bool(facts.get("tests_failed")),
    "pipeline_bug": lambda facts: facts.get("drift_type") == "pipeline_bug",
}


def new_workdir(run_id: str) -> str:
    return tempfile.mkdtemp(prefix=f"run-{str(run_id)[:8]}-")
