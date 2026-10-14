/* Overview page: controls, stats, timeline chart, latest incident, runs table. */
"use strict";

let cfg = null;
let timelineChart = null;
let lastTimeline = null;
let selectedScenario = null;
let lastSignature = "";
let lastControls = "";
let lastIncident = "";
let lastLiveError = null;

const scenarioLabel = (s, k) => (s.scenarios[k] ? s.scenarios[k].label : k);

/* Each render function only touches the DOM when its content changed, so polling
   does not replay animations or steal keyboard focus. */
function renderControls(s) {
  if (!selectedScenario) selectedScenario = s.scenario;
  const key = JSON.stringify([selectedScenario, s.scenario, s.drift_start, Object.keys(s.scenarios)]);
  if (key !== lastControls) {
    lastControls = key;
    document.getElementById("scenario-seg").innerHTML = Object.keys(s.scenarios).map((k) =>
      `<button type="button" data-k="${esc(k)}" aria-pressed="${k === selectedScenario}" title="${esc(s.scenarios[k].description)}">${esc(scenarioLabel(s, k))}</button>`
    ).join("");
    const active = s.scenario === selectedScenario;
    document.getElementById("scenario-note").textContent = active
      ? `${s.scenarios[s.scenario].description} Drift starts at batch ${s.drift_start}.`
      : `${s.scenarios[selectedScenario].description} Press Restart to switch (clears history).`;
  }
  document.getElementById("live-toggle").checked = s.live.running;
  if (s.live.last_error && s.live.last_error !== lastLiveError) toast(`Live mode: ${s.live.last_error}`, true);
  lastLiveError = s.live.last_error;
}

function renderStats(s) {
  const r = s.latest_run;
  document.getElementById("st-sev").innerHTML = r ? sevBadge(r.severity) : '<span class="muted">No runs yet</span>';
  document.getElementById("st-sev-sub").textContent = r ? r.reasons[0] : "Press Next batch to simulate production traffic.";
  document.getElementById("st-batch").textContent = s.latest_batch;
  document.getElementById("st-batch-sub").textContent = `Scenario: ${scenarioLabel(s, s.scenario)}`;
  document.getElementById("st-model").textContent = s.model.version;
  document.getElementById("st-model-sub").textContent =
    `Baseline ROC-AUC ${fmt(s.model.metrics.auc)} · F1 ${fmt(s.model.metrics.f1)}`;
  document.getElementById("st-pending").textContent = s.pending_approvals;
}

function renderIncident(s) {
  const box = document.getElementById("incident");
  const inc = s.latest_incident;
  const key = JSON.stringify(inc);
  if (key === lastIncident) return;
  lastIncident = key;
  document.getElementById("incident-when").textContent = inc ? `Batch ${inc.batch_id}` : "";
  if (!inc) {
    box.innerHTML = `<div class="empty-inline"><i class="ph ph-shield-check" style="font-size:26px" aria-hidden="true"></i>
      <span>No incidents. The agent investigates automatically when a check reaches Alert.</span></div>`;
    return;
  }
  const src = inc.source === "llm" ? `Grok ${esc(inc.llm_model)}` : "Rule-based";
  box.innerHTML = `<div class="incident fade-in">
      <div class="meta-row">${sevBadge(inc.severity)}<span class="chip">${esc(DRIFT_LABEL[inc.drift_type] || inc.drift_type)}</span></div>
      <p class="summary">${esc(inc.summary)}</p>
      <dl class="kv">
        <dt>Recommended</dt><dd>${esc(ACTION_LABEL[inc.recommended_action] || inc.recommended_action)}</dd>
        <dt>Investigated by</dt><dd>${src}</dd>
      </dl>
      <div><a class="btn small" href="/runs/${inc.run_id}"><i class="ph ph-file-text" aria-hidden="true"></i>Open report</a></div>
    </div>`;
}

function renderTimeline(data) {
  lastTimeline = data;
  const t = tokens();
  const box = document.getElementById("timeline-box");
  if (!data.points.length) {
    if (timelineChart) { timelineChart.destroy(); timelineChart = null; }
    box.querySelector("canvas").hidden = true;
    if (!box.querySelector(".empty-inline")) {
      box.insertAdjacentHTML("beforeend", `<div class="empty-inline" style="height:100%;align-content:center">
        <i class="ph ph-chart-bar" style="font-size:26px" aria-hidden="true"></i><span>No batches checked yet.</span></div>`);
    }
    return;
  }
  box.querySelector("canvas").hidden = false;
  box.querySelector(".empty-inline")?.remove();

  const labels = data.points.map((p) => p.batch_id);
  const scores = data.points.map((p) => Math.max(p.score, 0.004));
  const colors = data.points.map((p) => t[p.severity]);
  const aucByBatch = {};
  data.points.forEach((p) => { if (p.auc !== null && p.auc_batch !== null) aucByBatch[p.auc_batch] = p.auc; });
  const auc = labels.map((b) => (b in aucByBatch ? aucByBatch[b] : null));
  const baseline = data.points.find((p) => p.baseline_auc !== null)?.baseline_auc ?? null;

  const driftLine = {
    id: "driftStart",
    afterDatasetsDraw(chart) {
      const idx = labels.indexOf(data.drift_start);
      if (idx < 0 || data.scenario === "clean") return;
      const x = chart.scales.x.getPixelForValue(idx) - (chart.scales.x.getPixelForValue(1) - chart.scales.x.getPixelForValue(0)) / 2;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = t.text3; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = t.text3; ctx.font = `12px ${t.font}`;
      ctx.fillText("drift injected", x + 6, top + 12);
      ctx.restore();
    },
  };

  const config = {
    data: {
      labels,
      datasets: [
        { type: "bar", label: "Drift score", data: scores, backgroundColor: colors, borderRadius: 3, yAxisID: "y", maxBarThickness: 26 },
        { type: "line", label: "ROC-AUC", data: auc, yAxisID: "y2", borderColor: t.accent, backgroundColor: t.accent,
          spanGaps: true, pointRadius: 2.5, borderWidth: 2, tension: 0.25 },
        { type: "line", label: "Baseline AUC", data: labels.map(() => baseline), yAxisID: "y2", borderColor: t.accent,
          borderDash: [3, 4], borderWidth: 1, pointRadius: 0, hidden: baseline === null },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { title: { display: true, text: "Batch" }, grid: { display: false } },
        y: { beginAtZero: true, suggestedMax: cfg.severity.alert * 2.5, title: { display: true, text: "Drift score" } },
        y2: { position: "right", grid: { display: false }, title: { display: true, text: "ROC-AUC" },
              suggestedMin: baseline === null ? undefined : baseline - 3 * cfg.performance.auc_drop,
              suggestedMax: baseline === null ? undefined : baseline + cfg.performance.auc_drop },
      },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => `Batch ${items[0].label} · ${SEV_LABEL[data.points[items[0].dataIndex].severity]}`,
            label: (item) => item.dataset.label === "Drift score"
              ? ` Drift score ${fmt(data.points[item.dataIndex].score)}`
              : item.raw === null ? null : ` ${item.dataset.label} ${fmt(item.raw)}`,
          },
        },
      },
      onClick: (evt, elements) => {
        if (elements.length) window.location.href = `/runs/${data.points[elements[0].index].run_id}`;
      },
      onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? "pointer" : "default"; },
    },
    plugins: [driftLine],
  };
  if (timelineChart) timelineChart.destroy();
  timelineChart = new Chart(document.getElementById("timeline"), config);
}

function renderRuns(rows) {
  const body = document.getElementById("runs-body");
  document.getElementById("runs-hint").textContent = rows.length ? `${rows.length} checks, newest first` : "";
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7"><div class="empty-inline">No checks yet. Press <strong>Next batch</strong> or turn on <strong>Live</strong>.</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.slice(0, 40).map((r) => `
    <tr class="clickable" data-href="/runs/${r.id}">
      <td class="num" style="text-align:left">${r.batch_id}</td>
      <td>${sevBadge(r.severity)}</td>
      <td class="num">${fmt(r.score)}</td>
      <td><div class="feat-tags">${r.drifted.map((f) => `<span class="feat-tag ${esc(f.status)}">${esc(f.feature)} ${fmt(f.psi, 2)}</span>`).join("") || '<span class="muted">none</span>'}</div></td>
      <td class="mono">${esc(r.model_version)}</td>
      <td>${r.has_report ? '<span class="chip accent"><i class="ph ph-file-text" aria-hidden="true"></i>Report</span>' : ""}</td>
      <td class="muted mono">${timeOf(r.created_at)}</td>
    </tr>`).join("");
}

async function refresh(force = false) {
  const s = await refreshNav();
  if (!s || !s.initialised) return;
  renderControls(s);
  renderStats(s);
  renderIncident(s);
  const signature = `${s.latest_batch}|${s.latest_run?.id}|${s.pending_approvals}|${s.model.version}`;
  if (force || signature !== lastSignature) {
    lastSignature = signature;
    const [timeline, runs] = await Promise.all([api("/api/timeline"), api("/api/runs?limit=60")]);
    renderTimeline(timeline);
    renderRuns(runs);
  }
}

document.getElementById("scenario-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-k]");
  if (!b) return;
  selectedScenario = b.dataset.k;
  renderControls(window.__status);
});

document.getElementById("reset-btn").addEventListener("click", (e) => withBusy(e.currentTarget, "Restarting", async () => {
  await api("/api/simulate/reset", { method: "POST", body: { scenario: selectedScenario } });
  toast(`Restarted with scenario: ${scenarioLabel(window.__status, selectedScenario)}`);
  await refresh(true);
}));

document.getElementById("step-btn").addEventListener("click", (e) => withBusy(e.currentTarget, "Checking", async () => {
  const r = await api("/api/simulate/step", { method: "POST" });
  if (r.investigation) toast(`Batch ${r.batch_id}: ${SEV_LABEL[r.severity]}. Agent: ${DRIFT_LABEL[r.investigation.drift_type]}`);
  await refresh(true);
}));

document.getElementById("live-toggle").addEventListener("change", async (e) => {
  try {
    await api(e.target.checked ? "/api/live/start" : "/api/live/stop", { method: "POST" });
    toast(e.target.checked ? "Live mode on: a new batch is checked every few seconds" : "Live mode off");
  } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
});

document.getElementById("runs-body").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row) window.location.href = row.dataset.href;
});

onThemeChange(() => lastTimeline && renderTimeline(lastTimeline));
uiConfig.then((c) => {
  cfg = c;
  document.getElementById("label-delay").textContent = c.label_delay;
  refresh(true);
  setInterval(() => { if (!document.hidden) refresh(); }, c.dashboard.poll_interval_ms);
}).catch((e) => toast(e.message, true));
