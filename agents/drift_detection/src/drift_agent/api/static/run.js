/* Run detail page: per-feature statistics, distributions, quality, performance, incident report. */
"use strict";

let cfg = null;
let data = null;
let featuresChart = null;
let distChart = null;
let currentFeature = null;
let lastDist = null;

function statusColor(t, status) {
  return status === "significant" ? t.CRITICAL : status === "moderate" ? t.WARNING : t.ref;
}

function renderHeader() {
  const { run, detector } = data;
  document.getElementById("run-title").textContent = `Batch ${run.batch_id}`;
  document.getElementById("run-sev").innerHTML = sevBadge(run.severity);
  document.getElementById("run-model").textContent = `model ${run.model_version}`;
  document.title = `Batch ${run.batch_id} · Drift Agent`;
  document.getElementById("reasons").innerHTML = detector.reasons
    .map((r) => `<li><i class="ph ph-info" aria-hidden="true"></i><span>${esc(r)}</span></li>`).join("");
  for (const [id, target] of [["prev-run", data.prev_run_id], ["next-run", data.next_run_id]]) {
    const a = document.getElementById(id);
    a.hidden = !target;
    if (target) a.href = `/runs/${target}`;
  }
}

function renderFeatureChart() {
  const t = tokens();
  const feats = [...data.detector.features].sort((a, b) => b.psi - a.psi);
  const cap = cfg.dashboard.psi_display_cap;
  const config = {
    type: "bar",
    data: {
      labels: feats.map((f) => f.feature),
      datasets: [{
        data: feats.map((f) => Math.min(f.psi, cap)),
        backgroundColor: feats.map((f) => statusColor(t, f.status)),
        borderRadius: 3, maxBarThickness: 18,
      }],
    },
    options: {
      indexAxis: "y", maintainAspectRatio: false,
      scales: {
        x: { beginAtZero: true, suggestedMax: cfg.psi.significant * 1.2, title: { display: true, text: `PSI (capped at ${cap})` } },
        y: { grid: { display: false }, ticks: { font: { family: t.mono } } },
      },
      plugins: {
        tooltip: {
          callbacks: {
            label: (item) => {
              const f = feats[item.dataIndex];
              return [` PSI ${fmt(f.psi)} (${f.status})`, ` ${f.test === "ks" ? "KS D" : "JS distance"} ${fmt(f.effect)}`,
                      ` model importance ${pct(f.importance)}`];
            },
          },
        },
      },
      onClick: (evt, el) => { if (el.length) selectFeature(feats[el[0].index].feature); },
      onHover: (evt, el) => { evt.native.target.style.cursor = el.length ? "pointer" : "default"; },
    },
    plugins: [{
      id: "thresholds",
      afterDatasetsDraw(chart) {
        const { ctx, chartArea: { top, bottom }, scales: { x } } = chart;
        ctx.save();
        ctx.setLineDash([3, 4]);
        [[cfg.psi.moderate, t.WARNING], [cfg.psi.significant, t.CRITICAL]].forEach(([v, c]) => {
          if (v > x.max) return;
          const px = x.getPixelForValue(v);
          ctx.strokeStyle = c; ctx.globalAlpha = 0.6;
          ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, bottom); ctx.stroke();
        });
        ctx.restore();
      },
    }],
  };
  if (featuresChart) featuresChart.destroy();
  featuresChart = new Chart(document.getElementById("features-chart"), config);
}

function renderFeatureTable() {
  const p = (v) => (v === null ? "-" : v < 0.0001 ? "<0.0001" : fmt(v, 4));
  document.getElementById("features-body").innerHTML = data.detector.features.map((f) => `
    <tr class="clickable ${f.feature === currentFeature ? "selected" : ""}" data-f="${esc(f.feature)}">
      <td class="mono">${esc(f.feature)}</td>
      <td class="muted">${f.test === "ks" ? "KS" : "Chi-square"}</td>
      <td class="num">${f.statistic === null ? "-" : fmt(f.statistic, f.test === "ks" ? 3 : 1)}</td>
      <td class="num">${p(f.p_value)}</td>
      <td class="num">${fmt(f.effect)}</td>
      <td class="num">${fmt(f.psi)}</td>
      <td class="num">${pct(f.importance)}</td>
      <td class="status-${esc(f.status)}">${esc(f.status)}${f.status !== "stable" && !f.confirmed ? ' <span class="muted">(unconfirmed)</span>' : ""}</td>
    </tr>`).join("");
}

async function selectFeature(feature) {
  currentFeature = feature;
  document.getElementById("feature-select").value = feature;
  renderFeatureTable();
  try {
    lastDist = await api(`/api/runs/${RUN_ID}/distribution?feature=${encodeURIComponent(feature)}`);
    renderDist();
  } catch (e) {
    document.getElementById("dist-note").innerHTML = `<span class="error-box">${esc(e.message)}</span>`;
  }
}

function renderDist() {
  const d = lastDist;
  const t = tokens();
  const config = {
    type: "bar",
    data: {
      labels: d.labels,
      datasets: [
        { label: "Reference", data: d.reference, backgroundColor: t.ref, borderRadius: 2, categoryPercentage: 0.85, barPercentage: 0.95 },
        { label: "This batch", data: d.current, backgroundColor: t.accent, borderRadius: 2, categoryPercentage: 0.85, barPercentage: 0.95 },
      ],
    },
    options: {
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, ticks: { callback: (v) => pct(v, 0) } },
        x: { grid: { display: false }, ticks: { maxRotation: 45, autoSkip: true, font: { family: d.kind === "numeric" ? t.mono : t.font } } },
      },
      plugins: { tooltip: { callbacks: { label: (i) => ` ${i.dataset.label} ${pct(i.raw)}` } } },
    },
  };
  if (distChart) distChart.destroy();
  distChart = new Chart(document.getElementById("dist-chart"), config);
  const notes = [];
  if (d.kind === "numeric") notes.push(`Median ${fmt(d.reference_median, 1)} in reference, ${fmt(d.current_median, 1)} in this batch.`);
  if (d.current_missing > d.reference_missing + 0.001) notes.push(`Missing values ${pct(d.reference_missing)} to ${pct(d.current_missing)}.`);
  if (d.new_categories?.length) notes.push(`Never seen in training: ${d.new_categories.join(", ")}.`);
  document.getElementById("dist-note").textContent = notes.join(" ");
}

function renderModelMetrics() {
  const { prediction: pr, performance: pf } = data.detector;
  const items = [
    ["Score PSI", fmt(pr.score_psi), pr.drifted],
    ["Predicted yes-rate", `${pct(pr.reference_positive_rate)} to ${pct(pr.current_positive_rate)}`, pr.drifted],
  ];
  if (pf && pf.auc !== null) {
    const span = pf.batches.length > 1 ? `batches ${pf.batches[0]}-${pf.batches[pf.batches.length - 1]}` : `batch ${pf.batch_id}`;
    items.push([`ROC-AUC (${span})`, `${fmt(pf.auc)} vs ${fmt(pf.baseline_auc)}`, pf.degraded]);
    items.push(["F1", `${fmt(pf.f1)} vs ${fmt(pf.baseline_f1)}`, false]);
  } else {
    items.push(["ROC-AUC", pf?.skipped_reason || "labels not arrived yet", false]);
  }
  document.getElementById("model-metrics").innerHTML = items.map(([k, v, bad]) =>
    `<div class="metric"><div class="k">${esc(k)}</div><div class="v ${bad ? "delta-down" : ""}">${esc(v)}</div></div>`).join("");
}

function renderQuality() {
  const q = data.detector.quality;
  document.getElementById("quality").innerHTML = q.length
    ? `<ul class="reasons">${q.map((i) => `<li><i class="ph ph-warning-octagon" style="color:var(--crit)" aria-hidden="true"></i><span>${esc(i.detail)}</span></li>`).join("")}</ul>`
    : `<p class="muted"><i class="ph ph-check" aria-hidden="true"></i> Schema, missing values, categories and ranges all match the reference.</p>`;
}

function renderReport() {
  const box = document.getElementById("report");
  const r = data.investigation;
  const src = document.getElementById("report-source");
  if (!r) {
    src.textContent = "";
    box.innerHTML = `<div class="empty-inline">
      <i class="ph ph-magnifying-glass" style="font-size:26px" aria-hidden="true"></i>
      <span>No investigation for this run. The agent only starts automatically at Alert or Critical.</span>
      <button class="btn" id="investigate-btn" type="button"><i class="ph ph-magnifying-glass" aria-hidden="true"></i>Investigate now</button>
    </div>`;
    document.getElementById("investigate-btn").addEventListener("click", (e) => withBusy(e.currentTarget, "Investigating", async () => {
      await api(`/api/runs/${RUN_ID}/investigate`, { method: "POST" });
      await load();
    }));
    return;
  }
  const m = r.meta;
  src.textContent = m.source === "llm"
    ? `Grok ${m.llm_model}, ${m.llm_turns} turns, ${m.llm_tokens.toLocaleString()} tokens, ${m.duration_s}s`
    : `Rule-based fallback, ${m.duration_s}s`;
  const causes = r.root_causes.map((c) => `
    <div class="cause">
      <div class="cause-title"><span>${esc(c.hypothesis)}</span><span class="conf">${esc(c.confidence)} confidence</span></div>
      <ul class="evidence">${c.evidence.map((e) => `<li><span class="tool">${esc(e.tool)}</span><span>${esc(e.observation)}</span></li>`).join("")}</ul>
    </div>`).join("");
  const actions = data.actions.map((a) => `
    <li><span class="chip">${esc(ACTION_LABEL[a.action] || a.action)}</span>
      <span class="muted">${esc(a.status)}${a.risk !== "none" ? ` · ${esc(a.risk)} risk` : ""}</span>
      ${a.action === "train_challenger" && a.result?.trained ? `<span>${esc(a.result.challenger_version)}: ROC-AUC ${fmt(a.result.champion_metrics.auc)} to ${fmt(a.result.challenger_metrics.auc)} on held-out batches</span>` : ""}
      ${a.status === "pending" ? '<a href="/approvals">Review</a>' : ""}
    </li>`).join("");
  const llmError = m.llm_error ? `<div class="warn-box">Grok was unavailable (${esc(m.llm_error)}), so the rule-based investigator wrote this report.</div>` : "";
  const unverified = m.unverified_evidence_tools.length
    ? `<div class="warn-box">Cites tools it never called: ${esc(m.unverified_evidence_tools.join(", "))}. Treat that evidence as unverified.</div>` : "";
  const skipped = m.missing_required_tools?.length
    ? `<div class="warn-box">Answered without checking: ${esc(m.missing_required_tools.join(", "))}. Conclusions about those areas are unverified.</div>` : "";
  const invented = m.unknown_features?.length
    ? `<div class="warn-box">Mentioned features that do not exist (removed): ${esc(m.unknown_features.join(", "))}.</div>` : "";
  box.innerHTML = `<div class="incident fade-in">
    ${llmError}${unverified}${skipped}${invented}
    <div class="meta-row">
      <span class="chip">${esc(DRIFT_LABEL[r.drift_type] || r.drift_type)}</span>
      <span class="chip accent"><i class="ph ph-arrow-right" aria-hidden="true"></i>${esc(ACTION_LABEL[r.recommended_action] || r.recommended_action)}</span>
      ${r.affected_features.map((f) => `<span class="feat-tag">${esc(f)}</span>`).join("")}
    </div>
    <p class="summary">${esc(r.summary)}</p>
    <dl class="kv">
      <dt>Impact</dt><dd>${esc(r.impact)}</dd>
      <dt>Why this action</dt><dd>${esc(r.action_rationale)}</dd>
    </dl>
    <div class="grid-run" style="gap:24px">
      <div><h3 style="margin-bottom:10px">Root causes</h3>${causes}</div>
      <div><h3 style="margin-bottom:10px">Reasoning</h3><ol class="steps">${r.explanation.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div>
    </div>
    ${actions ? `<div><h3 style="margin-bottom:8px">What the guardrail did</h3><ul class="reasons">${actions}</ul></div>` : ""}
    <details>
      <summary>Tool calls (${m.tool_calls.length})</summary>
      <div class="trace">${m.tool_calls.map((c, i) => `<div>${i + 1}. ${esc(c.tool)}(${esc(JSON.stringify(c.args))})</div>`).join("")}</div>
      <p style="margin-top:8px"><a href="/audit?run_id=${RUN_ID}">Full trace with outputs in the audit log</a></p>
    </details>
  </div>`;
}

async function load() {
  try {
    data = await api(`/api/runs/${RUN_ID}`);
  } catch (e) {
    document.getElementById("reasons").innerHTML = `<li class="error-box">${esc(e.message)}</li>`;
    return;
  }
  renderHeader();
  renderFeatureChart();
  renderModelMetrics();
  renderQuality();
  renderReport();
  const sel = document.getElementById("feature-select");
  sel.innerHTML = data.detector.features.map((f) => `<option value="${esc(f.feature)}">${esc(f.feature)}</option>`).join("");
  await selectFeature(currentFeature || data.detector.features[0].feature);
}

document.getElementById("feature-select").addEventListener("change", (e) => selectFeature(e.target.value));
document.getElementById("features-body").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-f]");
  if (row) selectFeature(row.dataset.f);
});
onThemeChange(() => { if (data) { renderFeatureChart(); if (lastDist) renderDist(); } });
uiConfig.then((c) => {
  cfg = c;
  document.getElementById("psi-moderate").textContent = fmt(c.psi.moderate, 2);
  document.getElementById("psi-significant").textContent = fmt(c.psi.significant, 2);
  load();
}).catch((e) => toast(e.message, true));
