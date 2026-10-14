/* Approvals page: pending promotions/rollbacks, model registry, decision history. */
"use strict";

const METRICS = [["auc", "ROC-AUC"], ["f1", "F1"], ["precision", "Precision"], ["recall", "Recall"]];

function delta(a, b) {
  const d = b - a;
  const cls = Math.abs(d) < 0.0005 ? "muted" : d > 0 ? "delta-up" : "delta-down";
  return `<span class="${cls}">${d >= 0 ? "+" : ""}${fmt(d)}</span>`;
}

function pendingCard(a) {
  const p = a.payload;
  let title, body;
  if (a.action === "promote_model") {
    title = `Promote <span class="mono">${esc(p.version)}</span> to replace <span class="mono">${esc(p.replaces)}</span>`;
    body = `
      <p class="text-2">The agent saw accuracy fall and the guardrail trained a challenger in the sandbox.
        Both models were scored on held-out batches ${esc(p.holdout_batches.join(", "))}, which neither was trained on.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Metric</th><th class="num">Current (${esc(p.replaces)})</th><th class="num">Challenger (${esc(p.version)})</th><th class="num">Change</th></tr></thead>
        <tbody>${METRICS.map(([k, label]) => `<tr><td>${label}</td>
          <td class="num">${fmt(p.champion_metrics[k])}</td><td class="num">${fmt(p.challenger_metrics[k])}</td>
          <td class="num">${delta(p.champion_metrics[k], p.challenger_metrics[k])}</td></tr>`).join("")}</tbody>
      </table></div>`;
  } else {
    title = `Roll back <span class="mono">${esc(p.from_version)}</span> to <span class="mono">${esc(p.to_version)}</span>`;
    body = `<p class="text-2">${esc(p.reason)}</p>`;
  }
  return `<section class="panel approval fade-in" data-id="${a.id}">
    <div class="approval-head">
      <div style="display:grid;gap:4px"><h2>${title}</h2>
        <span class="muted">Requested ${dateTime(a.created_at)}${a.run_id ? ` after <a href="/runs/${a.run_id}">batch ${a.batch_id}</a>` : ""}</span></div>
      <span class="sev sev-CRITICAL"><i class="ph-fill ph-shield-warning" aria-hidden="true"></i>${esc(a.risk)} risk</span>
    </div>
    ${body}
    <div class="approval-form">
      <div class="field"><label for="by-${a.id}">Your name</label><input type="text" id="by-${a.id}" value="dashboard user" autocomplete="name"></div>
      <div class="field"><label for="note-${a.id}">Note (optional)</label><input type="text" id="note-${a.id}" placeholder="Why you decided this"></div>
    </div>
    <div class="approval-actions">
      <button class="btn primary" data-act="approve" type="button"><i class="ph ph-check" aria-hidden="true"></i>Approve</button>
      <button class="btn" data-act="reject" type="button"><i class="ph ph-x" aria-hidden="true"></i>Reject</button>
    </div>
  </section>`;
}

function historyDetail(a) {
  const r = a.result || {};
  if (a.action === "raise_alert") return `to ${esc(a.payload.to)}: ${esc(a.payload.title)}`;
  if (a.action === "train_challenger") {
    if (r.error) return `<span class="delta-down">${esc(r.error)}</span>`;
    if (!r.trained) return esc(r.reason || "");
    return `${esc(r.challenger_version)}: ROC-AUC ${fmt(r.champion_metrics.auc)} to ${fmt(r.challenger_metrics.auc)} (${r.better ? "promotion requested" : "not good enough to promote"})`;
  }
  if (a.status === "approved") return `current model is now <span class="mono">${esc(r.current_model)}</span>${a.note ? `. ${esc(a.note)}` : ""}`;
  return esc(a.note || "");
}

async function load() {
  const [ap, models] = await Promise.all([api("/api/approvals"), api("/api/models")]);
  const pending = document.getElementById("pending");
  pending.innerHTML = ap.pending.length ? ap.pending.map(pendingCard).join("") : `
    <section class="panel empty-state">
      <i class="ph ph-check-circle" aria-hidden="true"></i>
      <h2>Nothing waiting for you</h2>
      <p>When the agent recommends retraining and the challenger beats the current model, the promotion request appears here.</p>
    </section>`;

  const current = models.find((m) => m.is_current);
  document.getElementById("models").innerHTML = [...models].reverse().map((m) => `
    <div class="model-row">
      <span class="mono" style="font-weight:600">${esc(m.version)}</span>
      <div style="min-width:0"><div class="text-2" style="font-size:13px">ROC-AUC ${fmt(m.metrics.auc)} · F1 ${fmt(m.metrics.f1)}</div>
        <div class="muted" style="font-size:12px">${esc(m.notes)}</div></div>
      ${m.is_current ? '<span class="chip accent">current</span>' : '<span class="chip">standby</span>'}
    </div>`).join("");
  const rb = document.getElementById("rollback-btn");
  rb.hidden = !(current && current.parent);
  if (current && current.parent) rb.innerHTML = `<i class="ph ph-arrow-u-up-left" aria-hidden="true"></i>Request rollback to ${esc(current.parent)}`;

  document.getElementById("history").innerHTML = ap.history.length ? ap.history.map((a) => `
    <tr>
      <td class="mono muted">${a.id}</td>
      <td>${esc(ACTION_LABEL[a.action] || a.action)}</td>
      <td>${esc(a.status)}</td>
      <td class="text-2">${esc(a.decided_by || "")}</td>
      <td class="text-2">${historyDetail(a)}</td>
      <td>${a.run_id ? `<a href="/runs/${a.run_id}">${a.batch_id}</a>` : ""}</td>
      <td class="muted mono">${timeOf(a.decided_at || a.created_at)}</td>
    </tr>`).join("") : `<tr><td colspan="7"><div class="empty-inline">No actions yet.</div></td></tr>`;
}

document.getElementById("pending").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest("[data-id]");
  const id = card.dataset.id;
  const body = { by: card.querySelector(`#by-${id}`).value.trim() || "dashboard user",
                 note: card.querySelector(`#note-${id}`).value.trim() || null };
  withBusy(btn, btn.dataset.act === "approve" ? "Approving" : "Rejecting", async () => {
    const res = await api(`/api/approvals/${id}/${btn.dataset.act}`, { method: "POST", body });
    toast(btn.dataset.act === "approve" ? `Approved. Current model is now ${res.result.current_model}` : "Rejected");
    await Promise.all([load(), refreshNav()]);
  });
});

document.getElementById("rollback-btn").addEventListener("click", (e) => withBusy(e.currentTarget, "Requesting", async () => {
  await api("/api/models/rollback", { method: "POST", body: { by: "dashboard user" } });
  toast("Rollback requested. Approve it above to apply.");
  await Promise.all([load(), refreshNav()]);
}));

load().catch((e) => toast(e.message, true));
