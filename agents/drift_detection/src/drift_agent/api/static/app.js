/* Shared helpers for every dashboard page. */
"use strict";

const SEV_ICON = { OK: "ph-check-circle", WARNING: "ph-warning", ALERT: "ph-warning-diamond", CRITICAL: "ph-warning-octagon" };
const SEV_LABEL = { OK: "OK", WARNING: "Warning", ALERT: "Alert", CRITICAL: "Critical" };
const DRIFT_LABEL = {
  covariate: "Population shift", pipeline_bug: "Pipeline bug", concept: "Concept drift",
  prediction_only: "Prediction drift", none: "No drift",
};
const ACTION_LABEL = {
  monitor: "Monitor", investigate_upstream: "Investigate upstream", retrain_challenger: "Retrain challenger",
  rollback: "Roll back", no_action: "No action",
  raise_alert: "Alert raised", train_challenger: "Challenger trained",
  promote_model: "Promote model", rollback_model: "Roll back model",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(message, isError = false) {
  const el = document.createElement("div");
  el.className = "toast fade-in" + (isError ? " err" : "");
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3500);
}

/* Run an async action from a button, showing a busy state and surfacing errors. */
async function withBusy(button, label, fn) {
  const original = button.innerHTML;
  button.disabled = true;
  button.classList.add("busy");
  if (label) button.innerHTML = `<i class="ph ph-circle-notch" aria-hidden="true"></i>${esc(label)}`;
  try { return await fn(); }
  catch (e) { toast(e.message, true); throw e; }
  finally { button.disabled = false; button.classList.remove("busy"); button.innerHTML = original; }
}

function sevBadge(sev) {
  return `<span class="sev sev-${esc(sev)}"><i class="ph-fill ${SEV_ICON[sev] || "ph-circle"}" aria-hidden="true"></i>${esc(SEV_LABEL[sev] || sev)}</span>`;
}

function fmt(n, digits = 3) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return Number(n).toFixed(digits);
}
function pct(n, digits = 1) {
  if (n === null || n === undefined) return "-";
  return (Number(n) * 100).toFixed(digits) + "%";
}
function timeOf(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function dateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/* Theme-aware colours for Chart.js, read from CSS tokens. */
function tokens() {
  const s = getComputedStyle(document.documentElement);
  const v = (name) => s.getPropertyValue(name).trim();
  return {
    text: v("--text"), text2: v("--text-2"), text3: v("--text-3"), border: v("--border"),
    accent: v("--accent"), ref: v("--chart-ref"), surface: v("--surface"),
    OK: v("--ok"), WARNING: v("--warn"), ALERT: v("--alert"), CRITICAL: v("--crit"),
    mono: v("--mono"), font: v("--font"),
  };
}

function chartDefaults() {
  const t = tokens();
  Chart.defaults.color = t.text3;
  Chart.defaults.borderColor = t.border;
  Chart.defaults.font.family = t.font;
  Chart.defaults.font.size = 12;
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.plugins.tooltip.backgroundColor = t.text;
  Chart.defaults.plugins.tooltip.titleColor = t.surface;
  Chart.defaults.plugins.tooltip.bodyColor = t.surface;
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 6;
  Chart.defaults.plugins.tooltip.displayColors = true;
  Chart.defaults.animation = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 350 };
  return t;
}

/* Re-render charts when the theme changes. Pages register a callback. */
const themeListeners = [];
function onThemeChange(fn) { themeListeners.push(fn); }
function notifyTheme() { chartDefaults(); themeListeners.forEach((fn) => fn()); }
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", notifyTheme);

document.getElementById("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("drift-theme", root.dataset.theme); } catch (e) { /* storage unavailable */ }
  notifyTheme();
});

/* Top bar status: current model, Grok, pending approvals. */
async function refreshNav() {
  try {
    const s = await api("/api/status");
    window.__status = s;
    const pending = document.getElementById("nav-pending");
    pending.hidden = !s.pending_approvals;
    pending.textContent = s.pending_approvals;
    if (s.model) {
      const m = document.getElementById("nav-model");
      m.hidden = false;
      m.innerHTML = `<i class="ph ph-cube" aria-hidden="true"></i>Model <span class="mono">${esc(s.model.version)}</span>`;
    }
    const l = document.getElementById("nav-llm");
    l.hidden = false;
    l.className = "chip" + (s.llm.enabled ? " accent" : "");
    l.innerHTML = s.llm.enabled
      ? `<i class="ph ph-sparkle" aria-hidden="true"></i>Grok <span class="mono">${esc(s.llm.model)}</span>`
      : `<i class="ph ph-list-checks" aria-hidden="true"></i>Rule-based agent`;
    l.title = s.llm.enabled ? "Investigations use Grok via the xAI API" : "No XAI_API_KEY set: investigations use the rule-based fallback";
    return s;
  } catch (e) {
    return null;
  }
}

/* Thresholds, label delay, poll interval etc. come from config.yaml via the API. */
const uiConfig = api("/api/config");

chartDefaults();
refreshNav();
