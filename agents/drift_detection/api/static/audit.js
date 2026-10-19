/* Audit log page. */
"use strict";

function describe(e) {
  const p = e.payload || {};
  switch (e.event) {
    case "drift_check": return `Batch ${p.batch_id}: ${p.severity}, score ${fmt(p.score)}. ${(p.reasons || [])[0] || ""}`;
    case "tool_call": return `${p.tool}(${JSON.stringify(p.args)})${p.output && p.output.error ? ` failed: ${p.output.error}` : ""}`;
    case "llm_message":
      return p.tool_calls && p.tool_calls.length
        ? `Asked for ${p.tool_calls.map((t) => t.name).join(", ")}${p.tokens ? ` (${p.tokens} tokens)` : ""}`
        : `Answered${p.tokens ? ` (${p.tokens} tokens)` : ""}`;
    case "llm_failed": return `Grok failed: ${p.error}. Fell back to rules.`;
    case "required_tools_reminder": return `Grok tried to conclude early; told to call ${p.missing.join(", ")}`;
    case "report_written": return `${p.drift_type} -> ${p.recommended_action} (${p.source}): ${p.summary}`;
    case "recommendation_received": return `${p.recommended_action} for ${p.drift_type}`;
    case "approval_requested": return `${p.action} (${p.risk} risk) waiting as #${p.action_id}`;
    case "action_done": case "action_failed": return `${p.action} (${p.risk} risk)`;
    case "approved": case "rejected": return `#${p.action_id} ${p.action} by ${p.by}${p.note ? `: ${p.note}` : ""}`;
    case "batch_generated": return `Batch ${p.batch_id}, ${p.rows} rows scored by ${p.model_version}${p.labels_released_for && p.labels_released_for.length ? `; labels arrived for batch ${p.labels_released_for.join(", ")}` : ""}`;
    case "simulation_reset": return `Scenario ${p.scenario}, drift from batch ${p.drift_start}`;
    default: return Object.keys(p).length ? JSON.stringify(p).slice(0, 220) : "";
  }
}

async function load() {
  const actor = document.getElementById("actor").value;
  const run = document.getElementById("run").value;
  const qs = new URLSearchParams({ limit: "400" });
  if (actor) qs.set("actor", actor);
  if (run) qs.set("run_id", run);
  const log = document.getElementById("log");
  let rows;
  try { rows = await api(`/api/audit?${qs}`); }
  catch (e) { log.innerHTML = `<div class="panel-body"><div class="error-box">${esc(e.message)}</div></div>`; return; }
  document.getElementById("count").textContent = `${rows.length} entries`;
  if (!rows.length) {
    log.innerHTML = `<div class="empty-inline">Nothing logged for this filter.</div>`;
    return;
  }
  log.innerHTML = rows.map((e) => `
    <div class="audit-row">
      <span class="when">${dateTime(e.created_at)}</span>
      <span class="actor actor-${esc(e.actor)}">${esc(e.actor)}</span>
      <div style="min-width:0">
        <details style="border:0;padding:0">
          <summary><span class="event">${esc(e.event.replaceAll("_", " "))}</span></summary>
          <pre>${esc(JSON.stringify(e.payload, null, 2))}</pre>
        </details>
        <div class="detail">${esc(describe(e))}</div>
      </div>
      <span>${e.run_id ? `<a href="/runs/${e.run_id}">run ${e.run_id}</a>` : ""}</span>
    </div>`).join("");
}

const params = new URLSearchParams(location.search);
if (params.get("run_id")) document.getElementById("run").value = params.get("run_id");
if (params.get("actor")) document.getElementById("actor").value = params.get("actor");
document.getElementById("filters").addEventListener("submit", (e) => { e.preventDefault(); load(); });
document.getElementById("actor").addEventListener("change", load);
load();
