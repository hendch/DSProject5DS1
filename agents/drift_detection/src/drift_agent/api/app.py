"""FastAPI app: JSON API under /api and the web dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from drift_agent.detection import stats
from drift_agent.scheduler import LiveRunner
from drift_agent.service import DriftService

HERE = Path(__file__).parent


class ResetBody(BaseModel):
    scenario: str
    drift_start: int | None = None
    seed: int | None = None


class LiveBody(BaseModel):
    interval: int | None = None


class DecisionBody(BaseModel):
    by: str = "dashboard user"
    note: str | None = None


class CheckBody(BaseModel):
    batch_id: int | None = None


def create_app(service: DriftService | None = None, start_live: bool = False) -> FastAPI:
    svc = service or DriftService()
    live = LiveRunner(svc)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_live and svc.is_initialised():
            live.start()
        yield
        live.shutdown()

    app = FastAPI(title="Drift Detection Agent", version="0.1.0", lifespan=lifespan,
                  description="Statistical drift monitoring, an LLM investigator and human-approved actions.")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    # Appended to static URLs so browsers fetch fresh CSS/JS after an update.
    asset_version = str(int(max(p.stat().st_mtime for p in (HERE / "static").glob("*.*"))))
    app.state.service, app.state.live = svc, live

    def need_init() -> None:
        if not svc.is_initialised():
            raise HTTPException(409, "Not initialised: run `drift-agent init` first.")

    def run_or_404(run_id: int) -> dict:
        run = svc.store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        return run

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'"))
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    # ---- pages ------------------------------------------------------------------
    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"initialised": svc.is_initialised(), "v": asset_version,
                                                          **ctx})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def overview_page(request: Request):
        return page(request, "overview.html", active="overview")

    @app.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    def run_page(request: Request, run_id: int):
        run_or_404(run_id)
        return page(request, "run.html", active="overview", run_id=run_id)

    @app.get("/approvals", response_class=HTMLResponse, include_in_schema=False)
    def approvals_page(request: Request):
        return page(request, "approvals.html", active="approvals")

    @app.get("/audit", response_class=HTMLResponse, include_in_schema=False)
    def audit_page(request: Request):
        return page(request, "audit.html", active="audit")

    # ---- status -----------------------------------------------------------------
    @app.get("/api/health", tags=["status"])
    def health():
        return {"ok": True, "initialised": svc.is_initialised()}

    @app.get("/api/status", tags=["status"])
    def status():
        model = svc.store.current_model()
        latest = svc.store.latest_run()
        report = svc.store.get_report(latest["id"]) if latest else None
        open_incident = svc.store.latest_run_with_report()
        return {
            "initialised": model is not None,
            "scenario": svc.simulator.scenario if model else None,
            "scenarios": {k: {"label": v["label"], "description": v["description"]}
                          for k, v in svc.settings.scenarios.items()},
            "drift_start": svc.simulator.drift_start if model else None,
            "latest_batch": svc.store.latest_batch_id(),
            "model": {k: model[k] for k in ("version", "metrics", "threshold", "parent", "created_at")}
            if model else None,
            "llm": {"enabled": svc.use_llm, "model": svc.settings.llm.model if svc.use_llm else None},
            "live": {"running": live.running, "interval": live.interval, "last_error": live.last_error},
            "pending_approvals": len(svc.store.list_actions(status="pending")),
            "latest_run": {"id": latest["id"], "batch_id": latest["batch_id"], "severity": latest["severity"],
                           "score": latest["score"], "reasons": latest["report"]["reasons"],
                           "has_report": report is not None} if latest else None,
            "latest_incident": _incident(svc, open_incident["id"]) if open_incident else None,
        }

    @app.get("/api/config", tags=["status"])
    def ui_config():
        """Thresholds and settings the dashboard displays, so it never hardcodes them."""
        s = svc.settings
        return {
            "psi": s["detection"]["psi"], "severity": s["severity"],
            "label_delay": s["simulation"]["label_delay"],
            "performance": s["detection"]["performance"],
            "dashboard": s["dashboard"], "scheduler": s["scheduler"],
            "investigate_at": s["agent"]["investigate_at"],
        }

    # ---- runs -------------------------------------------------------------------
    @app.get("/api/runs", tags=["runs"])
    def list_runs(limit: int = 100):
        rows = svc.store.list_runs(limit=limit)
        for r in rows:
            feats = svc.store.feature_results(r["id"])
            r["drifted"] = [{"feature": f["feature"], "status": f["status"], "psi": f["psi"]}
                            for f in feats if f["status"] != "stable"][:4]
        return rows

    @app.get("/api/timeline", tags=["runs"])
    def timeline():
        points = []
        reported = svc.store.run_ids_with_report()
        for run in svc.store.all_runs():
            perf = run["report"].get("performance")
            points.append({
                "run_id": run["id"], "batch_id": run["batch_id"], "severity": run["severity"],
                "score": run["score"], "model_version": run["model_version"], "has_report": run["id"] in reported,
                "prediction_psi": run["report"]["prediction"]["score_psi"],
                "auc": perf.get("auc") if perf else None, "auc_batch": perf.get("batch_id") if perf else None,
                "baseline_auc": perf.get("baseline_auc") if perf else None,
            })
        return {"points": points, "drift_start": svc.simulator.drift_start if svc.is_initialised() else None,
                "scenario": svc.simulator.scenario if svc.is_initialised() else None}

    @app.get("/api/runs/{run_id}", tags=["runs"])
    def get_run(run_id: int):
        run = run_or_404(run_id)
        rep = svc.store.get_report(run_id)
        model = svc.store.get_model(run["model_version"])
        return {
            "run": {k: run[k] for k in ("id", "batch_id", "perf_batch_id", "created_at", "model_version",
                                        "severity", "score")},
            "detector": run["report"],
            "investigation": rep["report"] if rep else None,
            "investigation_created_at": rep["created_at"] if rep else None,
            "actions": svc.store.list_actions(run_id=run_id),
            "importance": model["importance"] if model else {},
            "events": svc.store.events_between(max(1, run["batch_id"] - 6), run["batch_id"]),
            "prev_run_id": (svc.store.previous_run(run_id) or {}).get("id"),
            "next_run_id": (svc.store.next_run(run_id) or {}).get("id"),
        }

    @app.get("/api/runs/{run_id}/distribution", tags=["runs"])
    def distribution(run_id: int, feature: str):
        run = run_or_404(run_id)
        if feature not in svc.settings.features:
            raise HTTPException(400, f"unknown feature {feature}")
        ref = svc.detector.reference_for(run["model_version"])[feature]
        cur = svc.simulator.load_batch(run["batch_id"])[feature]
        d = svc.settings["dashboard"]
        return _distribution(feature, ref, cur, feature in svc.settings.numeric,
                             d["distribution_bins"], d["distribution_top_categories"],
                             d["distribution_clip_percentile"])

    @app.post("/api/runs/{run_id}/investigate", tags=["runs"])
    def investigate(run_id: int):
        run_or_404(run_id)
        return guard(svc.investigate_run, run_id)

    @app.post("/api/check", tags=["runs"])
    def check(body: CheckBody | None = None):
        need_init()
        return guard(svc.check, body.batch_id if body else None)

    # ---- simulation ---------------------------------------------------------------
    @app.post("/api/simulate/step", tags=["simulation"])
    def step():
        need_init()
        return guard(svc.step)

    @app.post("/api/simulate/reset", tags=["simulation"])
    def reset(body: ResetBody):
        need_init()
        live.stop()
        guard(svc.reset, body.scenario, body.seed, body.drift_start)
        return {"scenario": body.scenario, "drift_start": svc.simulator.drift_start}

    @app.post("/api/live/start", tags=["simulation"])
    def live_start(body: LiveBody | None = None):
        need_init()
        live.start(body.interval if body else None)
        return {"running": True, "interval": live.interval}

    @app.post("/api/live/stop", tags=["simulation"])
    def live_stop():
        live.stop()
        return {"running": False}

    @app.get("/api/events", tags=["simulation"])
    def events(limit_batches: int = 30):
        latest = svc.store.latest_batch_id()
        return svc.store.events_between(max(1, latest - limit_batches), latest)

    # ---- approvals & models ---------------------------------------------------------
    @app.get("/api/approvals", tags=["approvals"])
    def approvals():
        all_actions = svc.store.list_actions()
        return {"pending": [a for a in all_actions if a["status"] == "pending"],
                "history": [a for a in all_actions if a["status"] != "pending"]}

    @app.post("/api/approvals/{action_id}/approve", tags=["approvals"])
    def approve(action_id: int, body: DecisionBody | None = None):
        body = body or DecisionBody()
        return guard(svc.guardrail.approve, action_id, by=body.by, note=body.note)

    @app.post("/api/approvals/{action_id}/reject", tags=["approvals"])
    def reject(action_id: int, body: DecisionBody | None = None):
        body = body or DecisionBody()
        return guard(svc.guardrail.reject, action_id, by=body.by, note=body.note)

    @app.get("/api/models", tags=["models"])
    def models():
        return svc.store.list_models()

    @app.post("/api/models/rollback", tags=["models"])
    def rollback(body: DecisionBody | None = None):
        body = body or DecisionBody()
        return guard(svc.guardrail.request_rollback, by=body.by)

    # ---- audit ----------------------------------------------------------------------
    @app.get("/api/audit", tags=["audit"])
    def audit(actor: str | None = None, run_id: int | None = None, limit: int = 300):
        return svc.store.list_audit(run_id=run_id, actor=actor or None, limit=min(limit, 2000))

    return app


def _incident(svc: DriftService, run_id: int) -> dict:
    rep = svc.store.get_report(run_id)
    run = svc.store.get_run(run_id)
    r = rep["report"]
    return {"run_id": run_id, "batch_id": run["batch_id"], "severity": run["severity"],
            "summary": r["summary"], "drift_type": r["drift_type"],
            "recommended_action": r["recommended_action"], "source": r["meta"]["source"],
            "llm_model": r["meta"]["llm_model"], "created_at": rep["created_at"]}


def _distribution(feature: str, ref: pd.Series, cur: pd.Series, numeric: bool, bins: int,
                  top_categories: int, clip: float) -> dict:
    if numeric:
        r = pd.to_numeric(ref, errors="coerce").dropna().to_numpy(float)
        c = pd.to_numeric(cur, errors="coerce").dropna().to_numpy(float)
        # Clip extreme tails so a few outliers don't squash the histogram.
        lo = float(min(np.percentile(r, clip), np.percentile(c, clip) if len(c) else np.inf))
        hi = float(max(np.percentile(r, 100 - clip), np.percentile(c, 100 - clip) if len(c) else -np.inf))
        if hi <= lo:
            hi = lo + 1
        edges = np.linspace(lo, hi, bins + 1)
        full = np.concatenate([[-np.inf], edges[1:-1], [np.inf]])
        rp, cp = stats.binned_proportions(r, full), stats.binned_proportions(c, full)
        labels = [f"{edges[i]:,.0f}" if abs(hi - lo) > 50 else f"{edges[i]:.1f}" for i in range(len(edges) - 1)]
        return {"feature": feature, "kind": "numeric", "labels": labels,
                "reference": rp.round(4).tolist(), "current": cp.round(4).tolist(),
                "reference_missing": float(ref.isna().mean()), "current_missing": float(cur.isna().mean()),
                "reference_median": float(np.median(r)) if len(r) else None,
                "current_median": float(np.median(c)) if len(c) else None}
    cats, rc, cc = stats.category_proportions(ref.dropna(), cur.dropna())
    rs, cs = rc / max(rc.sum(), 1), cc / max(cc.sum(), 1)
    order = np.argsort(-(rs + cs))[:top_categories]
    return {"feature": feature, "kind": "categorical", "labels": [cats[i] for i in order],
            "reference": [round(float(rs[i]), 4) for i in order], "current": [round(float(cs[i]), 4) for i in order],
            "new_categories": [cats[i] for i in range(len(cats)) if rc[i] == 0 and cc[i] > 0],
            "reference_missing": float(ref.isna().mean()), "current_missing": float(cur.isna().mean())}
