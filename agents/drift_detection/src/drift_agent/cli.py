"""Command line interface: `drift-agent --help`."""

from __future__ import annotations

import time
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape as e
from rich.panel import Panel
from rich.table import Table

from drift_agent.config import get_settings

app = typer.Typer(help="Drift Detection Agent: monitor a model, explain drift, act with human approval.",
                  no_args_is_help=True, add_completion=False)
llm_app = typer.Typer(help="Grok (xAI) connection.", no_args_is_help=True)
app.add_typer(llm_app, name="llm")
console = Console()

SEV_STYLE = {"OK": "green", "WARNING": "yellow", "ALERT": "dark_orange", "CRITICAL": "bold red"}


def _service(use_llm: Optional[bool] = None):
    from drift_agent.service import DriftService

    svc = DriftService(use_llm=use_llm)
    return svc


def _require_init(svc) -> None:
    if not svc.is_initialised():
        console.print("[red]Not initialised.[/] Run [bold]drift-agent init[/] first.")
        raise typer.Exit(1)


def _print_result(r: dict) -> None:
    sev = r["severity"]
    console.print(f"batch [bold]{r['batch_id']:>3}[/]  [{SEV_STYLE[sev]}]{sev:<8}[/]  "
                  f"score {r['score']:.3f}  {e(r['reasons'][0])}")
    inv = r.get("investigation")
    if inv:
        src = inv["meta"]["source"]
        console.print(f"      [cyan]agent ({src})[/]: {inv['drift_type']} -> {inv['recommended_action']}: "
                      f"{e(inv['summary'])}")
    for a in r.get("actions", []):
        console.print(f"      [magenta]guardrail[/]: {a['action']} ({a['status']}, {a['risk']} risk)")


@app.command()
def init(force_download: bool = typer.Option(False, help="Re-download the dataset")):
    """Download the dataset, train the baseline model (v1) and create the database."""
    svc = _service(use_llm=False)
    with console.status("Downloading data and training the baseline model..."):
        meta = svc.initialise(force_download=force_download)
    m = meta["metrics"]
    console.print(Panel.fit(
        f"Model [bold]v1[/] registered\nROC-AUC {m['auc']:.3f}, F1 {m['f1']:.3f}, precision {m['precision']:.3f}"
        f", recall {m['recall']:.3f}\nDecision threshold {meta['threshold']:.3f}, scenario reset to 'clean'",
        title="Initialised", border_style="green"))
    top = sorted(meta["importance"].items(), key=lambda kv: -kv[1])[:6]
    console.print("Most important features: " + ", ".join(f"{k} ({v:.0%})" for k, v in top))


@app.command()
def reset(scenario: str = typer.Option(next(iter(get_settings().scenarios)),
                                      help=f"One of: {', '.join(get_settings().scenarios)}"),
          drift_start: Optional[int] = typer.Option(None, help="Batch at which drift starts"),
          seed: Optional[int] = typer.Option(None, help="Random seed")):
    """Clear production history and start a scenario from batch 1."""
    svc = _service(use_llm=False)
    _require_init(svc)
    scenarios = svc.settings.scenarios
    if scenario not in scenarios:
        console.print(f"[red]Unknown scenario[/] {scenario!r}. Choose from: {', '.join(scenarios)}")
        raise typer.Exit(1)
    svc.reset(scenario, seed=seed, drift_start=drift_start)
    console.print(f"Scenario [bold]{scenario}[/]: {scenarios[scenario]['description']} Drift starts at batch "
                  f"{svc.simulator.drift_start}.")


@app.command()
def simulate(batches: int = typer.Option(1, "--batches", "-n", help="How many batches to generate"),
             check: bool = typer.Option(True, help="Run a drift check after each batch"),
             agent: bool = typer.Option(True, help="Let the agent investigate alerts"),
             llm: Optional[bool] = typer.Option(None, "--llm/--no-llm", help="Force Grok on/off")):
    """Generate production batches (and check each one)."""
    svc = _service(use_llm=llm)
    _require_init(svc)
    console.print(f"Scenario [bold]{svc.simulator.scenario}[/], investigator: "
                  f"{'Grok ' + svc.settings.llm.model if svc.use_llm else 'rule-based'}")
    for _ in range(batches):
        if check:
            _print_result(svc.step(investigate=agent))
        else:
            console.print(f"batch {svc.simulator.next_batch()} generated")


@app.command("check")
def check_cmd(batch: Optional[int] = typer.Option(None, help="Batch id (default: latest)"),
              agent: bool = typer.Option(True, help="Let the agent investigate alerts"),
              llm: Optional[bool] = typer.Option(None, "--llm/--no-llm", help="Force Grok on/off")):
    """Run a drift check on an existing batch."""
    svc = _service(use_llm=llm)
    _require_init(svc)
    _print_result(svc.check(batch, investigate=agent))


@app.command()
def status(last: int = typer.Option(12, help="How many recent runs to show")):
    """Scenario, current model, recent runs and pending approvals."""
    svc = _service(use_llm=False)
    _require_init(svc)
    model = svc.store.current_model()
    console.print(f"Scenario [bold]{svc.simulator.scenario}[/] (drift from batch {svc.simulator.drift_start}), "
                  f"latest batch {svc.store.latest_batch_id()}, current model [bold]{model['version']}[/] "
                  f"(AUC {model['metrics']['auc']:.3f}), Grok {'on' if svc.use_llm else 'off'}")
    t = Table("run", "batch", "severity", "score", "model", "report")
    for r in svc.store.list_runs(limit=last):
        t.add_row(str(r["id"]), str(r["batch_id"]), f"[{SEV_STYLE[r['severity']]}]{r['severity']}[/]",
                  f"{r['score']:.3f}", r["model_version"], "yes" if r["has_report"] else "")
    console.print(t)
    pending = svc.store.list_actions(status="pending")
    if pending:
        console.print(f"[yellow]{len(pending)} action(s) awaiting approval:[/] run [bold]drift-agent approvals[/]")


@app.command()
def report(run_id: Optional[int] = typer.Argument(None, help="Run id (default: latest with a report)")):
    """Print the agent's incident report for a run."""
    svc = _service(use_llm=False)
    if run_id is None:
        run_id = next((r["id"] for r in svc.store.list_runs() if r["has_report"]), None)
    rep = svc.store.get_report(run_id) if run_id else None
    if not rep:
        console.print("No incident report found.")
        raise typer.Exit(1)
    r = rep["report"]
    body = [f"[bold]{e(r['summary'])}[/]", "",
            f"Type: [cyan]{r['drift_type']}[/], Recommended: [magenta]{r['recommended_action']}[/], "
            f"Source: {r['meta']['source']}" + (f" ({r['meta']['llm_model']})" if r['meta']['llm_model'] else ""),
            f"Impact: {e(r['impact'])}", f"Why this action: {e(r['action_rationale'])}", "", "[bold]Root causes[/]"]
    for i, rc in enumerate(r["root_causes"], 1):
        body.append(f"{i}. {e(rc['hypothesis'])} [dim]({rc['confidence']} confidence)[/]")
        body += [f"   - [dim]{e(ev['tool'])}[/]: {e(ev['observation'])}" for ev in rc["evidence"]]
    body += ["", "[bold]Reasoning[/]"] + [f"{i}. {e(s)}" for i, s in enumerate(r["explanation"], 1)]
    if r["meta"]["unverified_evidence_tools"]:
        body.append(f"\n[red]Cites tools it never called:[/] {r['meta']['unverified_evidence_tools']}")
    console.print(Panel("\n".join(body), title=f"Incident report, run {run_id}", border_style="cyan"))


@app.command()
def approvals():
    """List actions waiting for a human decision."""
    svc = _service(use_llm=False)
    pending = svc.store.list_actions(status="pending")
    if not pending:
        console.print("Nothing is waiting for approval.")
        return
    for a in pending:
        p = a["payload"]
        if a["action"] == "promote_model":
            cm, nm = p["champion_metrics"], p["challenger_metrics"]
            detail = (f"promote {p['version']} to replace {p['replaces']}: AUC {cm['auc']:.3f} -> {nm['auc']:.3f} "
                      f"(+{p['auc_gain']:.3f}), F1 {cm['f1']:.3f} -> {nm['f1']:.3f} on batches {p['holdout_batches']}")
        else:
            detail = f"roll back {p['from_version']} -> {p['to_version']}: {p['reason']}"
        console.print(f"[bold]#{a['id']}[/] ({a['risk']} risk) {detail}")
    console.print("Decide with [bold]drift-agent approve ID[/] or [bold]drift-agent reject ID[/].")


@app.command()
def approve(action_id: int, by: str = typer.Option("cli-user", help="Who approves"),
            note: Optional[str] = typer.Option(None)):
    """Approve a pending action (promotion or rollback)."""
    svc = _service(use_llm=False)
    a = svc.guardrail.approve(action_id, by=by, note=note)
    console.print(f"[green]Approved[/] #{action_id}: current model is now [bold]{a['result']['current_model']}[/]")


@app.command()
def reject(action_id: int, by: str = typer.Option("cli-user"), note: Optional[str] = typer.Option(None)):
    """Reject a pending action."""
    svc = _service(use_llm=False)
    svc.guardrail.reject(action_id, by=by, note=note)
    console.print(f"[yellow]Rejected[/] #{action_id}")


@app.command()
def rollback(by: str = typer.Option("cli-user")):
    """Request a rollback to the previous model (still needs approval)."""
    svc = _service(use_llm=False)
    a = svc.guardrail.request_rollback(by=by)
    console.print(f"Rollback requested as action #{a['id']}. Approve it with [bold]drift-agent approve {a['id']}[/].")


@app.command()
def schedule(interval: Optional[int] = typer.Option(None, help="Seconds between checks"),
             llm: Optional[bool] = typer.Option(None, "--llm/--no-llm")):
    """Run checks forever on a timer (Ctrl+C to stop)."""
    svc = _service(use_llm=llm)
    _require_init(svc)
    interval = interval or svc.settings["scheduler"]["interval_seconds"]
    console.print(f"Checking every {interval}s. Ctrl+C to stop.")
    try:
        while True:
            _print_result(svc.step())
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("Stopped.")


@app.command()
def serve(host: Optional[str] = typer.Option(None, help="Default: server.host in config.yaml"),
          port: Optional[int] = typer.Option(None, help="Default: server.port in config.yaml"),
          live: bool = typer.Option(False, help="Start generating and checking batches immediately")):
    """Start the API and web dashboard."""
    import uvicorn

    from drift_agent.api.app import create_app

    cfg = get_settings()["server"]
    host, port = host or cfg["host"], port or cfg["port"]
    console.print(f"Dashboard: [bold]http://{host}:{port}[/], API docs: http://{host}:{port}/docs")
    uvicorn.run(create_app(start_live=live), host=host, port=port, log_level="warning")


@app.command()
def evaluate(seeds: Optional[int] = typer.Option(None, help="Runs per scenario (default: config.yaml)"),
             batches: Optional[int] = typer.Option(None, help="Batches per run (default: config.yaml)"),
             llm: bool = typer.Option(False, "--llm/--no-llm", help="Use Grok (costs API credits)"),
             scenario: Optional[list[str]] = typer.Option(None, help="Limit to some scenarios")):
    """Benchmark detection and diagnosis on every scenario."""
    from drift_agent.evaluation import run_benchmark

    svc = _service(use_llm=False)
    _require_init(svc)
    if llm and not svc.settings.llm.enabled:
        console.print("[red]XAI_API_KEY is not set.[/]")
        raise typer.Exit(1)
    seeds = seeds or svc.settings["evaluation"]["seeds"]
    batches = batches or svc.settings["evaluation"]["batches"]
    with console.status("Running benchmark...") as st:
        out = run_benchmark(svc.settings, list(range(1, seeds + 1)), batches, llm, scenario,
                            progress=lambda m: st.update(f"Running benchmark: {m}"))
    t = Table("scenario", "detection", "false alarms", "time to detect", "diagnosis", "action", "challenger gain")
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    for r in out["summary"]:
        t.add_row(r["scenario"], pct(r["detection_rate"]), pct(r["false_alarm_rate"]),
                  "-" if r["mean_time_to_detect"] is None else f"{r['mean_time_to_detect']:.1f} batches",
                  pct(r["diagnosis_accuracy"]), pct(r["action_accuracy"]),
                  "-" if r["mean_challenger_auc_gain"] is None else f"+{r['mean_challenger_auc_gain']:.3f} AUC")
    console.print(t)
    console.print(f"Saved to {out['path']}")


@llm_app.command("check")
def llm_check():
    """Test the xAI key and list the models it can use."""
    from drift_agent.agent.llm import GrokClient

    s = get_settings().llm
    if not s.enabled:
        console.print("[red]XAI_API_KEY is not set.[/] Copy .env.example to .env and add your key.")
        raise typer.Exit(1)
    client = GrokClient(s)
    try:
        models = client.list_models()
    except Exception as exc:
        console.print(f"[red]Could not reach xAI:[/] {exc}")
        raise typer.Exit(1)
    console.print("Available models: " + ", ".join(models))
    if s.model not in models:
        console.print(f"[yellow]XAI_MODEL={s.model} is not in that list.[/] Set XAI_MODEL in .env to one of them.")
        raise typer.Exit(1)
    resp = client.chat([{"role": "user", "content": "Reply with the single word: ready"}])
    console.print(f"[green]OK[/] {s.model} replied: {resp.choices[0].message.content!r}")


if __name__ == "__main__":
    app()
