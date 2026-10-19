"""Rule-based investigator used when the LLM is unavailable (no key, network error, bad output).

It calls the same read-only tools as the LLM agent, so its evidence is real,
then applies fixed rules. It is also the baseline the LLM is compared against
in the evaluation benchmark.
"""

from __future__ import annotations

from drift_agent.agent.schemas import Evidence, InvestigationReport, RootCause
from drift_agent.agent.tools import ToolBox

def investigate(tools: ToolBox) -> InvestigationReport:
    cfg = tools.settings["agent"]["fallback"]
    upstream_sources = set(cfg["upstream_sources"])
    report = tools.call("get_drift_report")
    quality = report["quality_issues"]
    drifted = [f for f in report["drifted_features"] if f["confirmed"]] or report["drifted_features"]
    perf = report["performance"] if isinstance(report["performance"], dict) else None
    perf_hist = tools.call("performance_history")
    events = tools.call("recent_pipeline_events")["events"]
    steps = [f"Severity {report['severity']}: " + "; ".join(report["reasons"])]

    # 1. Broken data: quality issues, ideally a sudden onset matching a pipeline deployment.
    if quality:
        feats = sorted({q["feature"] for q in quality})
        # Follow the quality-failing feature whose distribution moved the most.
        psi = {f["feature"]: f["psi"] for f in report["drifted_features"]}
        main = max(feats, key=lambda f: psi.get(f, -1.0))
        hist = tools.call("drift_history", {"feature": main})
        dist = tools.call("compare_distributions", {"feature": main})
        upstream = [e for e in events if e["source"] in upstream_sources]
        evidence = [Evidence(tool="get_drift_report", observation="; ".join(q["detail"] for q in quality[:4])),
                    Evidence(tool="drift_history",
                             observation=f"'{main}' change was {hist['pattern']} with onset at batch "
                                         f"{hist['onset_batch']}")]
        ratio = dist.get("current_to_reference_quantile_ratio", {}).get("p50")
        if ratio:
            evidence.append(Evidence(tool="compare_distributions",
                                     observation=f"median of '{main}' is {ratio}x the reference median"))
        confidence = "medium"
        if upstream:
            e = upstream[-1]
            evidence.append(Evidence(tool="recent_pipeline_events",
                                     observation=f"batch {e['batch_id']}: {e['source']} - {e['message']}"))
            confidence = "high" if hist["pattern"] == "sudden" else "medium"
        steps += [f"Data quality checks failed on {', '.join(feats)}.",
                  f"The change in '{main}' is {hist['pattern']}, which points to a data problem rather "
                  "than a shift in the customer population.",
                  "Upstream events around the onset: " + (", ".join(e["message"] for e in upstream) or "none")]
        return InvestigationReport(
            summary=f"Production data looks broken rather than changed: {len(quality)} quality check(s) "
                    f"failed on {', '.join(feats)}. Predictions on these batches are unreliable.",
            drift_type="pipeline_bug", affected_features=feats,
            root_causes=[RootCause(hypothesis="An upstream data pipeline change corrupted the inputs "
                                              "(units, missing values or unexpected categories).",
                                   confidence=confidence, evidence=evidence)],
            impact="The model receives values it was never trained on; scores for affected rows are unreliable.",
            recommended_action="investigate_upstream",
            action_rationale="Retraining would teach the model the bug. The data pipeline must be fixed first.",
            explanation=steps,
        )

    # 2. Performance dropped.
    if perf and perf.get("degraded"):
        evidence = [Evidence(tool="get_drift_report",
                             observation=f"ROC-AUC {perf['auc']} vs baseline {perf['baseline_auc']} on batch "
                                         f"{perf['batch_id']}")]
        if isinstance(perf_hist["checks"], list):
            degraded = [c for c in perf_hist["checks"] if c["degraded"]]
            evidence.append(Evidence(tool="performance_history",
                                     observation=f"{len(degraded)} of the last {len(perf_hist['checks'])} "
                                                 "labelled batches are below the tolerance"))
        if not drifted:
            evidence.append(Evidence(tool="get_drift_report",
                                     observation="no confirmed drift in the input features"))
            steps += ["Model performance fell while the inputs look unchanged.",
                      "When X is stable but accuracy drops, the relationship between X and y has changed."]
            return InvestigationReport(
                summary="The model is getting less accurate although the input data looks the same: "
                        "the relationship between client profile and subscription has changed (concept drift).",
                drift_type="concept", affected_features=[],
                root_causes=[RootCause(hypothesis="Customer behaviour changed, so the patterns learned in "
                                                  "training no longer hold.", confidence="medium",
                                       evidence=evidence)],
                impact=f"ROC-AUC fell by {perf['auc_drop']:.3f}; the model mis-ranks clients.",
                recommended_action="retrain_challenger",
                action_rationale="Only retraining on recent labelled data can capture the new relationship.",
                explanation=steps,
            )
        names = [f["feature"] for f in drifted]
        steps += [f"Performance fell and the inputs drifted ({', '.join(names)})."]
        return InvestigationReport(
            summary=f"Input drift in {', '.join(names[:4])} is now hurting model accuracy.",
            drift_type="covariate", affected_features=names,
            root_causes=[RootCause(hypothesis="The client population shifted into regions the model "
                                              "handles poorly.", confidence="medium", evidence=evidence)],
            impact=f"ROC-AUC fell by {perf['auc_drop']:.3f}.",
            recommended_action="retrain_challenger",
            action_rationale="The shift is affecting accuracy, so a model trained on recent data should help.",
            explanation=steps,
        )

    # 3. Input drift without (known) performance loss.
    if drifted:
        main = drifted[0]["feature"]
        hist = tools.call("drift_history", {"feature": main})
        dist = tools.call("compare_distributions", {"feature": main})
        names = [f["feature"] for f in drifted]
        evidence = [
            Evidence(tool="get_drift_report",
                     observation=", ".join(f"{f['feature']} PSI {f['psi']}" for f in drifted[:5])),
            Evidence(tool="drift_history",
                     observation=f"'{main}' drift is {hist['pattern']}, onset at batch {hist['onset_batch']}"),
        ]
        if dist["kind"] == "numeric":
            evidence.append(Evidence(tool="compare_distributions",
                                     observation=f"'{main}' mean {dist['reference']['mean']} -> "
                                                 f"{dist['current']['mean']}"))
        perf_ok = perf is not None and not perf.get("degraded")
        steps += [f"Confirmed drift in {', '.join(names)}; no data quality failures.",
                  f"The change in '{main}' is {hist['pattern']}, typical of a real population shift.",
                  "Performance is still within tolerance." if perf_ok else
                  "No labelled batch confirms the impact on accuracy yet."]
        action = "monitor" if perf_ok or report["data_drift_score"] < cfg["retrain_score"] else "retrain_challenger"
        return InvestigationReport(
            summary=f"The client population is shifting ({', '.join(names[:4])}). The data is valid; "
                    "the world changed.",
            drift_type="covariate", affected_features=names,
            root_causes=[RootCause(hypothesis=f"A real change in who is being contacted, led by '{main}'.",
                                   confidence="medium", evidence=evidence)],
            impact="Accuracy is holding so far, but the model is working further from its training data."
            if perf_ok else "Impact on accuracy is not yet measurable.",
            recommended_action=action,
            action_rationale="Accuracy has not dropped, so retraining is not justified yet; keep watching."
            if action == "monitor" else "The shift is large and accuracy is unverified; prepare a challenger.",
            explanation=steps,
        )

    # 4. Only the model output moved.
    pred = report["prediction_drift"]
    if pred["drifted"]:
        return InvestigationReport(
            summary="The model's output distribution moved without clear input drift.",
            drift_type="prediction_only", affected_features=[],
            root_causes=[RootCause(hypothesis="Many small input shifts that add up in the score.",
                                   confidence="low",
                                   evidence=[Evidence(tool="get_drift_report",
                                                      observation=f"score PSI {pred['score_psi']}")])],
            impact="Predicted yes-rate changed; check downstream volumes.",
            recommended_action="monitor", action_rationale="Not enough evidence to act.", explanation=steps,
        )

    return InvestigationReport(
        summary="No meaningful drift found.", drift_type="none", affected_features=[],
        root_causes=[RootCause(hypothesis="Normal batch-to-batch variation.", confidence="medium",
                               evidence=[Evidence(tool="get_drift_report", observation="; ".join(report["reasons"]))])],
        impact="None.", recommended_action="no_action", action_rationale="Nothing to fix.", explanation=steps,
    )
