/* OpsAgents dashboard. Vanilla JS, no build step, no dependencies.
 *
 * Layout of this file:
 *   1. helpers (escaping, formatting, icons)      5. pages
 *   2. API layer + state                          6. sidebar / header chrome, command palette
 *   3. UI primitives (regions, empty states,      7. routing, polling, actions, events
 *      toasts, popovers, filter chips)            8. MOCK backend (?mock=1)
 *   4. charts (inline SVG) + shared renderers     9. boot
 *
 * Every server-provided string goes through esc() (text) or safeUrl() (href/src).
 */
(function () {
  'use strict';

  var MOCK = /[?&]mock=1(?:&|$)/.test(window.location.search);
  var POLL_MS = 2000;
  var SLOW_EVERY = 5; // ticks between refreshes of GitHub status + sidebar recents
  var LS = {
    acting: 'opsagents.actingAs',
    theme: 'opsagents.theme',
    collapsed: 'opsagents.sidebarCollapsed',
    analytics: 'opsagents.analyticsOpen'
  };

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } }

  var AGENT_TITLES = {
    dependency_resolver: 'Dependency Resolver',
    diagnostic_rca: 'Diagnostic / RCA',
    merge_assistant: 'Merge Assistant',
    code_reviewer: 'Code Reviewer',
    drift_detection: 'Drift Detection',
    guardrail: 'Guardrail',
    orchestrator: 'Orchestrator'
  };

  // ===========================================================================
  // 1. Helpers
  // ===========================================================================

  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function humanize(s) {
    if (s === null || s === undefined || s === '') return '';
    return String(s).replace(/_/g, ' ');
  }

  function agentTitle(name) {
    if (!name) return '—';
    if (AGENT_TITLES[name]) return AGENT_TITLES[name];
    var s = String(name);
    if (/^[a-z0-9_]+$/.test(s) && s.indexOf('_') !== -1) {
      return s.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }
    return s;
  }

  function initialsOf(text) {
    var t = String(text || '').replace(/[^A-Za-z0-9 _.-]/g, ' ').trim().split(/[\s_.-]+/).filter(Boolean);
    if (!t.length) return '?';
    var a = t[0][0] + (t[1] ? t[1][0] : (t[0][1] || ''));
    return a.toUpperCase();
  }

  function arr(v) { return Array.isArray(v) ? v : []; }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  function hasVal(v) { return v !== undefined && v !== null && v !== ''; }

  function toneOf(status) {
    var s = String(status || '').toLowerCase();
    if (!s) return 'gray';
    if (/^(approved|auto_approved|completed|available|done|dry_run|ok|intact|low|success|succeeded|executed|merged_ok)$/.test(s)) return 'green';
    if (/^(pending|waiting|waiting_approval|running|executing|degraded|received|needs_triage|medium|queued|warning)$/.test(s)) return 'amber';
    if (/^(rejected|failed|failure|high|critical|unavailable|error|broken|denied)$/.test(s)) return 'red';
    if (/^(expired|skipped|not_run|not_needed|cancelled|canceled|none|unknown|closed)$/.test(s)) return 'gray';
    if (/^(open|info)$/.test(s)) return 'blue';
    if (s === 'merged') return 'violet';
    if (s.indexOf('reject') !== -1 || s.indexOf('fail') !== -1 || s.indexOf('block') !== -1) return 'red';
    if (s.indexOf('approv') !== -1 || s.indexOf('complete') !== -1 || s.indexOf('execut') !== -1) return 'green';
    if (s.indexOf('pend') !== -1 || s.indexOf('wait') !== -1 || s.indexOf('hold') !== -1) return 'amber';
    return 'gray';
  }

  function pill(status, label, tone) {
    var text = label !== undefined && label !== null ? label : humanize(status || 'unknown');
    return '<span class="pill pill-' + (tone || toneOf(status)) + '">' + esc(text) + '</span>';
  }

  function routedByChip(by, conf, withConf) {
    if (!by) return '';
    var txt = 'routed by ' + by;
    if (withConf && typeof conf === 'number') txt += ' · ' + Math.round(conf * 100) + '% confidence';
    return '<span class="chip chip-' + esc(by) + '">' + esc(txt) + '</span>';
  }

  function parseTime(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  // "2026-10-20" is a calendar day: parse it as local midnight, not UTC.
  function parseDay(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ''));
    if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
    return parseTime(s);
  }

  function shortDate(s) {
    var d = parseDay(s);
    return d ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : String(s || '');
  }

  function fmtTime(iso) {
    var d = parseTime(iso);
    if (!d) return iso ? esc(iso) : '—';
    return esc(d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
  }

  function fmtClock(iso) {
    var d = parseTime(iso);
    if (!d) return iso ? esc(iso) : '';
    var sameDay = d.toDateString() === new Date().toDateString();
    var t = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    return esc(sameDay ? t : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + t.slice(0, 5));
  }

  function fullTime(iso) {
    var d = parseTime(iso);
    return d ? d.toLocaleString() : (iso || '');
  }

  function relTime(iso) {
    var d = parseTime(iso);
    if (!d) return '—';
    var diff = (d.getTime() - Date.now()) / 1000;
    var abs = Math.abs(diff);
    var txt;
    if (abs < 45) return diff < 0 ? Math.max(1, Math.round(abs)) + 's ago' : 'in a few seconds';
    if (abs < 3600) txt = Math.round(abs / 60) + 'm';
    else if (abs < 86400) txt = Math.round(abs / 3600) + 'h';
    else txt = Math.round(abs / 86400) + 'd';
    return diff < 0 ? txt + ' ago' : 'in ' + txt;
  }

  // Coarser relative time for places that re-render every poll (avoids needless DOM churn).
  function relTimeCoarse(iso) {
    var d = parseTime(iso);
    if (!d) return '—';
    var abs = Math.abs(Date.now() - d.getTime()) / 1000;
    if (abs < 60) return 'just now';
    return relTime(iso);
  }

  function fmtDuration(startIso, endIso) {
    var a = parseTime(startIso);
    if (!a) return '—';
    var b = parseTime(endIso) || new Date();
    return fmtSeconds(Math.max(0, b.getTime() - a.getTime()) / 1000, true);
  }

  function fmtSeconds(sec, precise) {
    if (sec === null || sec === undefined || !isFinite(sec)) return '—';
    sec = Math.max(0, Number(sec));
    if (precise && sec < 1) return Math.round(sec * 1000) + 'ms';
    if (sec < 60) return Math.round(sec) + 's';
    var m = Math.floor(sec / 60);
    if (m < 60) return m + 'm ' + String(Math.round(sec % 60)).padStart(2, '0') + 's';
    var h = Math.floor(m / 60);
    if (h < 48) return h + 'h ' + (m % 60) + 'm';
    return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  }

  function fmtNum(n) {
    if (n === null || n === undefined || !isFinite(n)) return '—';
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });
  }

  function fmtPct(x) {
    if (x === null || x === undefined || !isFinite(x)) return '—';
    return Math.round(Number(x) * 100) + '%';
  }

  function detailMessage(data) {
    if (!data) return '';
    if (typeof data === 'string') return data.slice(0, 300);
    var d = data.detail !== undefined ? data.detail : data.message;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map(function (x) {
        if (x && typeof x === 'object') return (Array.isArray(x.loc) ? x.loc.join('.') + ': ' : '') + (x.msg || JSON.stringify(x));
        return String(x);
      }).join('; ');
    }
    if (d && typeof d === 'object') return JSON.stringify(d);
    return '';
  }

  // Only http(s) URLs may become href/src; javascript:, data:, etc. are dropped.
  function safeUrl(u) {
    if (!u || typeof u !== 'string') return '';
    return /^https?:\/\//i.test(u.trim()) ? u.trim() : '';
  }

  // Internal hash link ("#/runs/..") or external http(s) URL.
  function alertLink(link) {
    if (!link || typeof link !== 'string') return null;
    if (/^#\/[^\s"'<>]*$/.test(link)) return { href: link, external: false };
    var ext = safeUrl(link);
    return ext ? { href: ext, external: true } : null;
  }

  function firstLine(s) { return String(s || '').split(/\r?\n/)[0]; }

  function svgIcon(paths, cls) {
    return '<svg class="icon' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true">' + paths + '</svg>';
  }

  var ICON = {
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    x: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    dash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12"/></svg>',
    spinner: '<svg viewBox="0 0 24 24" aria-hidden="true"><path class="spin" d="M12 3a9 9 0 1 1-9 9"/></svg>',
    alert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5M12 16.5v.5"/><path d="M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
    info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.5"/></svg>',
    commit: svgIcon('<circle cx="12" cy="12" r="3.5"/><path d="M3 12h5.5M15.5 12H21"/>'),
    pr: svgIcon('<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5v7M18 15.5V9a3 3 0 0 0-3-3h-4M13 3.5 10.5 6 13 8.5"/>'),
    plusCircle: svgIcon('<circle cx="12" cy="12" r="8.5"/><path d="M12 8.5v7M8.5 12h7"/>'),
    hash: svgIcon('<path d="M9 4 7 20M17 4l-2 16M4.5 9h16M3.5 15h16"/>'),
    close: svgIcon('<path d="M6 6l12 12M18 6 6 18"/>'),
    calendar: svgIcon('<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'),
    sun: svgIcon('<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>'),
    moon: svgIcon('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
    external: svgIcon('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
    search: svgIcon('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    page: svgIcon('<path d="M6 3.5h8l4 4V20a.5.5 0 0 1-.5.5h-11A.5.5 0 0 1 6 20z"/><path d="M14 3.5V8h4"/>'),
    run: svgIcon('<path d="M7 4.5v15l12-7.5z"/>'),
    repo: svgIcon('<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19v-3"/>'),
    cpu: svgIcon('<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>'),
    sync: svgIcon('<path d="M20 11a8 8 0 0 0-14.7-4.3L4 8.5M4 13a8 8 0 0 0 14.7 4.3L20 15.5"/><path d="M4 4v4.5h4.5M20 20v-4.5h-4.5"/>'),
    gear: svgIcon('<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>')
  };

  // Friendly glyphs for empty states (no brand marks).
  var GLYPH = {
    robot: '<svg viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M28 9v5"/><circle cx="28" cy="6.5" r="2.5"/><rect x="12" y="14" width="32" height="23" rx="8"/><circle class="eye" cx="22" cy="25.5" r="3"/><circle class="eye" cx="34" cy="25.5" r="3"/><path d="M24 31.5h8"/><path d="M8 23v6M48 23v6"/><path d="M19 37v5a4 4 0 0 0 4 4h10a4 4 0 0 0 4-4v-5"/></svg>',
    shield: '<svg viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M28 6 12 12v12c0 10 6.8 18.5 16 22 9.2-3.5 16-12 16-22V12z"/><path class="acc" d="m21 27 5 5 10-10"/></svg>',
    bell: '<svg viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 37V26a13 13 0 0 1 26 0v11l3 4H12z"/><path d="M24 45a4 4 0 0 0 8 0"/><circle class="eye" cx="40" cy="14" r="4"/></svg>',
    plug: '<svg viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 8v10M36 8v10"/><path d="M14 18h28v6a14 14 0 0 1-28 0z"/><path d="M28 38v10"/><circle class="eye" cx="28" cy="27" r="3"/></svg>',
    chart: '<svg viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 46h40"/><rect x="12" y="28" width="7" height="18" rx="1.5"/><rect class="acc" x="24.5" y="16" width="7" height="30" rx="1.5"/><rect x="37" y="22" width="7" height="24" rx="1.5"/></svg>',
    search: '<svg viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="25" cy="25" r="14"/><path d="m35 35 11 11"/><circle class="eye" cx="25" cy="25" r="3"/></svg>'
  };

  // GitHub CI state -> badge.
  function ciBadge(ci, opts) {
    opts = opts || {};
    var map = {
      success: ['green', ICON.check, 'Passing'],
      failure: ['red', ICON.x, opts.failLabel || 'Failed'],
      running: ['amber', ICON.spinner, 'Running'],
      none: ['gray', ICON.dash, opts.noneLabel || 'No CI']
    };
    var m = map[ci] || map.none;
    return '<span class="ci ci-' + m[0] + (opts.large ? ' lg' : '') + '">' + m[1] + esc(m[2]) + '</span>';
  }

  function sevIcon(sev, small) {
    var s = /^(critical|warning|info|success)$/.test(sev) ? sev : 'info';
    var icon = { critical: ICON.alert, warning: ICON.alert, info: ICON.info, success: ICON.check }[s];
    return '<span class="sev sev-' + s + (small ? ' sm' : '') + '" title="' + esc(s) + '">' + icon + '</span>';
  }

  // Avatar with initials underneath; if the image fails to load it removes itself.
  function avatar(name, url, size) {
    var src = safeUrl(url);
    return '<span class="avatar' + (size ? ' ' + size : '') + '" aria-hidden="true">' + esc(initialsOf(name)) +
      (src ? '<img src="' + esc(src) + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">' : '') + '</span>';
  }

  // "GitHub · commit abc1234" / "GitHub · PR #12" / "Scenario" / "Incident text"
  function sourceLabel(run) {
    var t = (run && run.trigger) || {};
    var src = t.source || (run && run.text ? 'text' : '');
    if (!src) return '';
    if (src === 'github') {
      if (hasVal(t.pr)) return 'GitHub · PR #' + t.pr;
      if (t.sha) return 'GitHub · commit ' + String(t.sha).slice(0, 7);
      return 'GitHub';
    }
    if (src === 'scenario') return 'Scenario';
    if (src === 'text') return 'Incident text';
    return humanize(src);
  }
  function sourceKey(run) {
    var t = (run && run.trigger) || {};
    return t.source || (run && run.text ? 'text' : '');
  }
  function sourceChip(run) {
    var l = sourceLabel(run);
    return l ? '<span class="chip">' + esc(l) + '</span>' : '';
  }

  // ===========================================================================
  // 2. API layer + state
  // ===========================================================================

  function api(method, path, body) {
    if (MOCK) return mockApi(method, path, body);
    var opts = { method: method, headers: { 'Accept': 'application/json' }, cache: 'no-store' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    // location.origin, not a relative URL: a page opened as user:password@host would
    // otherwise make every request carry the credentials, which fetch refuses.
    return fetch(location.origin + '/api' + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
        if (res.status === 401) {  // signed out (password set on the server): back to the login page
          location.href = location.origin + '/login';
        }
        if (!res.ok) {
          var err = new Error(detailMessage(data) || (res.status + ' ' + res.statusText));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }, function () {
      var err = new Error("Can't reach the server");
      err.network = true;
      throw err;
    });
  }

  function isOutage(err) { return !!(err && (err.network || (err.status && err.status >= 502))); }

  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      if (hasVal(params[k])) parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  var state = {
    actingAs: 'ines',
    themePref: 'dark',
    route: { name: 'home', params: {} },
    token: 0,
    inFlight: false,
    queued: false,
    tickCount: 0,
    forceSlow: false,
    overview: null,
    overviewOk: false,
    offline: false,
    page: null,
    pageError: null,
    scenarios: null,
    routes: null,
    agents: null,
    githubStatus: null,
    recents: null,
    alerts: null,
    alertsSeen: null,
    search: '',
    incidentDraft: '',
    expanded: {},
    triageChoice: {},
    busy: {},
    filters: { runs: {}, activity: {}, approvals: {}, alerts: {}, analytics: { days: '30' } },
    repoFormError: '',
    repoSearch: '',
    availRepos: null,      // GET /github/available-repos, fetched once per visit of #/repositories
    availError: null,
    editingName: false,
    scrollToStart: false,
    palette: { open: false, items: [], index: 0, data: null, loadedAt: 0 }
  };

  var savedName = lsGet(LS.acting);
  if (savedName) state.actingAs = savedName;
  var savedTheme = lsGet(LS.theme);
  if (savedTheme === 'light' || savedTheme === 'dark' || savedTheme === 'system') state.themePref = savedTheme;

  var $ = function (id) { return document.getElementById(id); };

  // ===========================================================================
  // 3. UI primitives
  // ===========================================================================

  // Replace a region's HTML only when it changed, and never while the user is typing in it.
  function setHTML(el, html) {
    if (!el) return;
    if (el.__html === html) return;
    var a = document.activeElement;
    if (a && a !== document.body && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
    el.innerHTML = html;
    el.__html = html;
  }
  function region(id, html) { setHTML($(id), html); }

  function emptyState(o) {
    o = o || {};
    var action = '';
    if (o.action) {
      if (o.action.href) action = '<a class="btn" href="' + esc(o.action.href) + '">' + esc(o.action.label) + '</a>';
      else action = '<button class="btn" type="button" data-action="' + esc(o.action.action) + '"' + (o.action.attrs || '') + '>' + esc(o.action.label) + '</button>';
    }
    return '<div class="empty-state dotted' + (o.small ? ' sm' : '') + '">' +
      '<div class="glyph">' + (GLYPH[o.glyph] || GLYPH.robot) + '</div>' +
      '<div class="es-title">' + esc(o.title || 'Nothing here yet') + '</div>' +
      (o.text ? '<div class="es-text">' + esc(o.text) + '</div>' : '') + action + '</div>';
  }

  function skeletonRows(n) {
    var h = '';
    for (var i = 0; i < (n || 3); i++) h += '<div class="skel skel-row"></div>';
    return h;
  }

  function skeletonCards(n, wideIdx) {
    var h = '<div class="metrics">';
    for (var i = 0; i < n; i++) {
      h += '<div class="card metric' + (wideIdx && wideIdx.indexOf(i) !== -1 ? ' wide' : '') + '">' +
        '<div class="skel skel-title"></div><div class="skel skel-line"></div><div class="skel skel-block"></div></div>';
    }
    return h + '</div>';
  }

  function toast(message, kind, opts) {
    opts = opts || {};
    var wrap = $('toasts');
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || 'info') + (opts.link ? ' clickable' : '');
    el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    el.innerHTML = '<span class="msg"></span><button class="x" type="button" aria-label="Dismiss">×</button>';
    el.querySelector('.msg').textContent = message;
    var remove = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.querySelector('.x').addEventListener('click', function (ev) { ev.stopPropagation(); remove(); });
    if (opts.link) {
      el.addEventListener('click', function () { followLink(opts.link); remove(); });
    }
    wrap.appendChild(el);
    while (wrap.children.length > 4) wrap.removeChild(wrap.firstChild);
    setTimeout(remove, kind === 'err' || kind === 'warn' ? 7000 : 4000);
  }

  function followLink(l) {
    if (!l) return;
    if (l.external) window.open(l.href, '_blank', 'noopener,noreferrer');
    else window.location.hash = l.href;
  }

  function errorText(e) {
    if (!e) return 'Something went wrong';
    if (e.network) return "Can't reach the server";
    var prefix = { 401: 'Unauthorized: ', 403: 'Not allowed: ', 404: 'Not found: ', 409: 'Conflict: ', 410: 'Expired: ' }[e.status] || '';
    return prefix + (e.message || 'Request failed');
  }

  // ---- Popover (filter menus, alert bell, workspace menu) -------------------

  var pop = { kind: null, anchor: null, opts: null };

  function openPopover(anchor, kind, opts) {
    if (pop.kind && pop.anchor === anchor) { closePopover(); return; }
    pop.kind = kind;
    pop.anchor = anchor;
    pop.opts = opts || {};
    var el = $('popover');
    el.__html = null;
    el.hidden = false;
    renderPopover();
    var f = el.querySelector('input');
    if (f) f.focus();
  }

  function renderPopover() {
    if (!pop.kind) return;
    var el = $('popover');
    setHTML(el, POPOVERS[pop.kind](pop.opts));
    positionPopover();
  }

  function positionPopover() {
    var el = $('popover');
    var a = pop.anchor;
    if (!a || !document.body.contains(a)) { closePopover(); return; }
    var r = a.getBoundingClientRect();
    var w = el.offsetWidth, h = el.offsetHeight;
    var vw = window.innerWidth, vh = window.innerHeight;
    var left, top;
    if (pop.opts.side === 'right') {
      left = r.right + 8;
      top = r.bottom - h;
      if (left + w > vw - 8) left = r.left - w - 8;
    } else {
      left = pop.opts.alignRight ? r.right - w : r.left;
      top = r.bottom + 6;
      if (top + h > vh - 8 && r.top - h - 6 > 8) top = r.top - h - 6;
    }
    left = Math.max(8, Math.min(left, vw - w - 8));
    top = Math.max(8, Math.min(top, vh - h - 8));
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }

  function closePopover() {
    pop.kind = null;
    pop.anchor = null;
    var el = $('popover');
    el.hidden = true;
    el.innerHTML = '';
    el.__html = null;
  }

  var POPOVERS = {};

  // ---- Filter chips ----------------------------------------------------------

  function filterDefs(page) { return FILTERS[page] || []; }
  function filterDef(page, key) { return filterDefs(page).filter(function (d) { return d.key === key; })[0]; }

  function optionLabel(def, v) {
    var opts = def.options ? def.options() : [];
    var o = opts.filter(function (x) { return String(x.value) === String(v); })[0];
    return o ? o.label : String(v);
  }

  function toolbarHTML(page, right) {
    var f = state.filters[page] || {};
    var any = false;
    var chips = filterDefs(page).map(function (d) {
      var v = f[d.key];
      var attrs = ' data-page="' + esc(page) + '" data-key="' + esc(d.key) + '"';
      if (d.required) {
        return '<span class="fchip-on"><button class="fchip" type="button" data-action="filter-open"' + attrs + '>' +
          ICON.calendar + '<span>' + esc(d.display ? d.display(v) : optionLabel(d, v)) + '</span></button></span>';
      }
      var icon = d.icon === 'hash' ? ICON.hash : ICON.plusCircle;
      if (hasVal(v)) {
        any = true;
        return '<span class="fchip-on"><button class="fchip" type="button" data-action="filter-open"' + attrs + '>' + icon + esc(d.label) +
          ' <span class="fval">' + esc(d.icon === 'hash' ? '#' + v : optionLabel(d, v)) + '</span></button>' +
          '<span class="fsep"></span><button class="fx" type="button" data-action="filter-clear"' + attrs + ' aria-label="Remove ' + esc(d.label) + ' filter">' + ICON.close + '</button></span>';
      }
      return '<button class="fchip" type="button" data-action="filter-open"' + attrs + '>' + icon + esc(d.label) + '</button>';
    }).join('');
    return '<div class="toolbar">' + chips +
      (any ? '<button class="fclear" type="button" data-action="filter-clear-all" data-page="' + esc(page) + '">Clear</button>' : '') +
      '<span class="spacer"></span>' + (right || '') + '</div>';
  }

  POPOVERS.filter = function (o) {
    var def = filterDef(o.page, o.key);
    if (!def) return '';
    var cur = (state.filters[o.page] || {})[o.key];
    if (def.input) {
      return '<div class="pop-head">' + esc(def.label) + '</div>' +
        '<form class="pop-form" data-filter-form="1" data-page="' + esc(o.page) + '" data-key="' + esc(o.key) + '">' +
        '<input type="' + (def.input === 'number' ? 'number' : 'text') + '" name="v" min="1" placeholder="' + esc(def.placeholder || '') + '" value="' + esc(hasVal(cur) ? cur : '') + '">' +
        '<button class="btn primary sm" type="submit">Apply</button></form>';
    }
    var options = def.options ? def.options() : [];
    var list = options.length ? options.map(function (op) {
      var sel = String(op.value) === String(cur);
      return '<button class="pop-item' + (sel ? ' sel' : '') + '" type="button" data-action="filter-set" data-page="' + esc(o.page) + '" data-key="' + esc(o.key) + '" data-value="' + esc(op.value) + '" data-label="' + esc(String(op.label).toLowerCase()) + '">' +
        esc(op.label) + (op.hint ? ' <span class="faint small">' + esc(op.hint) + '</span>' : '') + (sel ? '<span class="check">✓</span>' : '') + '</button>';
    }).join('') : '<div class="pop-empty">' + esc(def.emptyText || 'No options yet') + '</div>';
    return '<div class="pop-head">' + esc(def.label) + '</div>' +
      (options.length > 7 ? '<div class="pop-search"><input type="text" data-pop-filter="1" placeholder="Filter…" aria-label="Filter options"></div>' : '') +
      '<div class="pop-list">' + list + '</div>';
  };

  // ===========================================================================
  // 4. Charts (hand-drawn inline SVG) + shared renderers
  // ===========================================================================

  function niceMax(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }

  function axisLabel(v, fmt) {
    if (fmt) return fmt(v);
    if (v >= 1000) return Math.round(v / 100) / 10 + 'k';
    return String(Math.round(v * 10) / 10);
  }

  function legend(series) {
    return '<div class="legend">' + series.map(function (s) {
      return '<span><i class="bg-' + s.cls + '"></i>' + esc(s.label) + '</span>';
    }).join('') + '</div>';
  }

  // Vertical (optionally stacked) columns. rows: [{label, values:{key:n}}]; series: [{key,label,cls}]
  function columnChart(rows, series, opts) {
    opts = opts || {};
    var W = 360, H = opts.height || 150, padL = 28, padR = 4, padT = 8, padB = 18;
    var ph = H - padT - padB;
    var totals = rows.map(function (r) {
      return series.reduce(function (s, se) { return s + num(r.values[se.key]); }, 0);
    });
    var max = niceMax(Math.max.apply(null, totals.concat([0])));
    var cw = (W - padL - padR) / Math.max(rows.length, 1);
    var bw = Math.max(1.5, Math.min(opts.barMax || 26, cw * 0.66));
    var out = [];
    [0, 0.5, 1].forEach(function (f) {
      var y = padT + ph * (1 - f);
      out.push('<line class="grid-line" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y + '" y2="' + y + '"/>');
      out.push('<text x="' + (padL - 5) + '" y="' + (y + 3) + '" text-anchor="end">' + esc(axisLabel(max * f, opts.fmt)) + '</text>');
    });
    var step = Math.max(1, Math.ceil(rows.length / (opts.maxLabels || 6)));
    rows.forEach(function (r, i) {
      var x = padL + cw * i + (cw - bw) / 2;
      var y = padT + ph;
      var tip = [esc(r.label)];
      series.forEach(function (se) {
        var v = num(r.values[se.key]);
        tip.push(esc(se.label) + ': ' + esc(axisLabel(v, opts.fmt)));
        if (!v) return;
        var h = v / max * ph;
        y -= h;
        out.push('<rect class="f-' + se.cls + '" x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + Math.max(h, 0.5).toFixed(1) + '" rx="1.5"/>');
      });
      // invisible full-height hit area carries the tooltip
      out.push('<rect x="' + (padL + cw * i).toFixed(1) + '" y="' + padT + '" width="' + cw.toFixed(1) + '" height="' + ph + '" fill="transparent"><title>' + tip.join('\n') + '</title></rect>');
      if (i % step === 0) {
        out.push('<text x="' + (padL + cw * i + cw / 2).toFixed(1) + '" y="' + (H - 4) + '" text-anchor="middle">' + esc(r.short || r.label) + '</text>');
      }
    });
    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(opts.title || 'chart') + '">' + out.join('') + '</svg>';
  }

  // Line chart. points: [{label, short, value|null}]
  function lineChart(points, opts) {
    opts = opts || {};
    var W = 360, H = opts.height || 150, padL = 34, padR = 8, padT = 10, padB = 18;
    var ph = H - padT - padB, pw = W - padL - padR;
    var vals = points.map(function (p) { return p.value; }).filter(function (v) { return v !== null && v !== undefined && isFinite(v); });
    if (!vals.length) return '';
    var max = niceMax(Math.max.apply(null, vals));
    var n = points.length;
    var xOf = function (i) { return padL + (n === 1 ? pw / 2 : pw * i / (n - 1)); };
    var yOf = function (v) { return padT + ph * (1 - v / max); };
    var out = [];
    [0, 0.5, 1].forEach(function (f) {
      var y = padT + ph * (1 - f);
      out.push('<line class="grid-line" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y + '" y2="' + y + '"/>');
      out.push('<text x="' + (padL - 5) + '" y="' + (y + 3) + '" text-anchor="end">' + esc(axisLabel(max * f, opts.fmt)) + '</text>');
    });
    var segs = [], cur = [];
    points.forEach(function (p, i) {
      if (p.value === null || p.value === undefined || !isFinite(p.value)) { if (cur.length) segs.push(cur); cur = []; return; }
      cur.push([xOf(i), yOf(p.value)]);
    });
    if (cur.length) segs.push(cur);
    var cls = opts.cls || 'accent';
    segs.forEach(function (s) {
      var d = s.map(function (pt, i) { return (i ? 'L' : 'M') + pt[0].toFixed(1) + ' ' + pt[1].toFixed(1); }).join(' ');
      if (cls === 'accent' && s.length > 1) {
        out.push('<path class="area-accent" d="' + d + ' L' + s[s.length - 1][0].toFixed(1) + ' ' + (padT + ph) + ' L' + s[0][0].toFixed(1) + ' ' + (padT + ph) + ' Z"/>');
      }
      out.push('<path class="st-line st-' + cls + '" d="' + d + '"/>');
    });
    var step = Math.max(1, Math.ceil(n / (opts.maxLabels || 6)));
    points.forEach(function (p, i) {
      if (p.value !== null && p.value !== undefined && isFinite(p.value)) {
        out.push('<circle class="f-' + cls + '" cx="' + xOf(i).toFixed(1) + '" cy="' + yOf(p.value).toFixed(1) + '" r="' + (n > 40 ? 1.2 : 2) + '"><title>' + esc(p.label) + ': ' + esc(axisLabel(p.value, opts.fmt)) + '</title></circle>');
      }
      if (i % step === 0) out.push('<text x="' + xOf(i).toFixed(1) + '" y="' + (H - 4) + '" text-anchor="middle">' + esc(p.short || p.label) + '</text>');
    });
    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(opts.title || 'line chart') + '">' + out.join('') + '</svg>';
  }

  function sparkline(values, cls) {
    var vals = values.filter(function (v) { return v !== null && v !== undefined && isFinite(v); });
    if (vals.length < 2) return '';
    var W = 140, H = 36, min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    if (max === min) { max += 1; min -= 1; }
    var pts = [];
    values.forEach(function (v, i) {
      if (v === null || v === undefined || !isFinite(v)) return;
      pts.push((W * i / (values.length - 1)).toFixed(1) + ',' + (2 + (H - 4) * (1 - (v - min) / (max - min))).toFixed(1));
    });
    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" style="max-width:160px" aria-hidden="true"><polyline class="st-line st-' + (cls || 'accent') + '" points="' + pts.join(' ') + '"/></svg>';
  }

  // Donut. segs: [{label, value, cls}]
  function donut(segs, opts) {
    opts = opts || {};
    var size = opts.size || 104, th = opts.thickness || 13;
    var r = (size - th) / 2, c = size / 2, C = 2 * Math.PI * r;
    var total = segs.reduce(function (s, x) { return s + num(x.value); }, 0);
    var out = ['<circle class="st-track" cx="' + c + '" cy="' + c + '" r="' + r + '" fill="none" stroke-width="' + th + '"/>'];
    var off = 0;
    if (total > 0) {
      segs.forEach(function (s) {
        var v = num(s.value);
        if (!v) return;
        var len = v / total * C;
        out.push('<circle class="st-' + s.cls + '" cx="' + c + '" cy="' + c + '" r="' + r + '" fill="none" stroke-width="' + th + '" stroke-dasharray="' + len.toFixed(2) + ' ' + (C - len).toFixed(2) + '" stroke-dashoffset="' + (-off).toFixed(2) + '" transform="rotate(-90 ' + c + ' ' + c + ')"><title>' + esc(s.label) + ': ' + esc(fmtNum(v)) + ' (' + Math.round(v / total * 100) + '%)</title></circle>');
        off += len;
      });
    }
    if (opts.center !== undefined) {
      out.push('<text x="' + c + '" y="' + (c + 2) + '" text-anchor="middle" style="font-size:18px;font-weight:650;fill:var(--text)">' + esc(opts.center) + '</text>');
      if (opts.sub) out.push('<text x="' + c + '" y="' + (c + 16) + '" text-anchor="middle">' + esc(opts.sub) + '</text>');
    }
    return '<svg class="chart" viewBox="0 0 ' + size + ' ' + size + '" style="width:' + size + 'px" role="img" aria-label="' + esc(opts.title || 'donut chart') + '">' + out.join('') + '</svg>';
  }

  // Radar. axes: [{label, value}]
  function radar(axes, opts) {
    opts = opts || {};
    var W = 300, H = 210, cx = W / 2, cy = H / 2 + 4, R = 72;
    var n = axes.length;
    // rings are unlabeled, so scale to the largest value (+10%) to let the shape fill the web
    var max = Math.max.apply(null, axes.map(function (a) { return num(a.value); }).concat([0])) * 1.1 || 1;
    var ang = function (i) { return -Math.PI / 2 + i * 2 * Math.PI / n; };
    var pt = function (i, rr) { return [cx + rr * Math.cos(ang(i)), cy + rr * Math.sin(ang(i))]; };
    var out = [];
    [1 / 3, 2 / 3, 1].forEach(function (f) {
      var ps = axes.map(function (a, i) { return pt(i, R * f).map(function (x) { return x.toFixed(1); }).join(','); });
      out.push('<polygon class="radar-web" points="' + ps.join(' ') + '"/>');
    });
    axes.forEach(function (a, i) {
      var p = pt(i, R);
      out.push('<line class="grid-line" x1="' + cx + '" y1="' + cy + '" x2="' + p[0].toFixed(1) + '" y2="' + p[1].toFixed(1) + '"/>');
      var lp = pt(i, R + 16);
      var anchor = Math.abs(lp[0] - cx) < 4 ? 'middle' : (lp[0] > cx ? 'start' : 'end');
      out.push('<text x="' + lp[0].toFixed(1) + '" y="' + (lp[1] + 3).toFixed(1) + '" text-anchor="' + anchor + '">' + esc(a.label) + ' <tspan class="val">' + esc(fmtNum(num(a.value))) + '</tspan></text>');
    });
    var dp = axes.map(function (a, i) { return pt(i, R * num(a.value) / max).map(function (x) { return x.toFixed(1); }).join(','); });
    out.push('<polygon class="area-accent st-accent" stroke-width="2" stroke-linejoin="round" points="' + dp.join(' ') + '"/>');
    axes.forEach(function (a, i) {
      var p = pt(i, R * num(a.value) / max);
      out.push('<circle class="f-accent" cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="2.5"><title>' + esc(a.label) + ': ' + esc(fmtNum(num(a.value))) + '</title></circle>');
    });
    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(opts.title || 'radar chart') + '">' + out.join('') + '</svg>';
  }

  // Horizontal bars in HTML (long labels stay readable). items: [{label, value, cls, display}]
  function hbars(items, opts) {
    opts = opts || {};
    var max = opts.max || Math.max.apply(null, items.map(function (i) { return num(i.value); }).concat([0])) || 1;
    return '<div class="hbars">' + items.map(function (i) {
      var w = Math.max(0, Math.min(100, num(i.value) / max * 100));
      return '<div class="hbar" title="' + esc(i.label) + ': ' + esc(i.display || fmtNum(i.value)) + '"><span class="name">' + esc(i.label) + '</span>' +
        '<span class="track"><i class="bg-' + (i.cls || opts.cls || 'accent') + '" style="width:' + w.toFixed(1) + '%"></i></span>' +
        '<span class="v">' + esc(i.display || fmtNum(i.value)) + '</span></div>';
    }).join('') + '</div>';
  }

  // One stacked horizontal bar. segs: [{label, value, cls}]
  function splitBar(segs) {
    var total = segs.reduce(function (s, x) { return s + num(x.value); }, 0);
    if (!total) return '<div class="split-bar"></div>';
    return '<div class="split-bar">' + segs.map(function (s) {
      var v = num(s.value);
      return v ? '<i class="bg-' + s.cls + '" style="width:' + (v / total * 100).toFixed(2) + '%" title="' + esc(s.label) + ': ' + esc(fmtNum(v)) + '"></i>' : '';
    }).join('') + '</div>';
  }

  // ---- Runs table ------------------------------------------------------------

  function runsTable(runs, emptyOpts) {
    if (!runs.length) return emptyState(emptyOpts || { title: 'No runs yet', text: 'Start one from Home — pick a scenario or describe an incident.', action: { label: 'Start a run', action: 'new-run' } });
    var rows = runs.map(function (r) {
      var href = '#/runs/' + encodeURIComponent(r.id);
      var agents = arr(r.agents).map(agentTitle).join(', ');
      var pending = r.pending ? '<span class="count-badge">' + esc(r.pending) + '</span>' : '<span class="faint">—</span>';
      var route = r.route ? '<span class="mono">' + esc(r.route) + '</span>' : '<span class="faint">no route</span>';
      var repo = r.trigger && r.trigger.repo ? '<span>' + esc(r.trigger.repo) + '</span>' : '';
      return '<tr class="clickable" data-href="' + esc(href) + '">' +
        '<td class="nowrap" title="' + esc(fullTime(r.created_at)) + '">' + fmtTime(r.created_at) + '</td>' +
        '<td class="title-cell"><a href="' + esc(href) + '">' + esc(r.title || r.trigger_type || 'Untitled run') + '</a>' +
          '<div class="sub">' + sourceChip(r) + repo + '</div></td>' +
        '<td><div class="row" style="gap:6px">' + route + routedByChip(r.routed_by) + '</div></td>' +
        '<td class="muted">' + (agents ? esc(agents) : '<span class="faint">—</span>') + '</td>' +
        '<td>' + pill(r.status) + '</td>' +
        '<td class="num">' + pending + '</td>' +
        '<td class="num muted nowrap">' + esc(fmtDuration(r.created_at, r.finished_at)) + '</td>' +
        '</tr>';
    }).join('');
    return '<div class="table-wrap"><table class="table"><thead><tr>' +
      '<th>Started</th><th>Trigger</th><th>Route</th><th>Agents</th><th>Status</th><th class="num">Pending</th><th class="num">Duration</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  // ---- Proposals / approvals -------------------------------------------------

  function fromApproval(a) {
    return {
      approvalId: a.id, agent: a.agent, actionType: a.action_type, decision: 'pending',
      approvalStatus: 'pending', environment: a.environment, blastRadius: a.blast_radius, risk: a.risk,
      policyRule: a.policy_rule, reason: a.reason, llmSummary: a.llm_summary,
      required: a.required_approvals, approvedBy: arr(a.approved_by), requestedBy: a.requested_by,
      expiresAt: a.expires_at, createdAt: a.created_at, runId: a.run_id, runTitle: a.run_title, repo: a.repo,
      hasApproval: true
    };
  }

  function fromAction(act) {
    var ap = act.approval || null;
    return {
      approvalId: ap ? ap.id : null, agent: act.agent, actionType: act.action_type, decision: act.decision,
      approvalStatus: ap ? ap.status : null, environment: act.environment || (ap ? ap.environment : null),
      // the Guardrail decided on its own (policy auto-approve / auto-reject): no human votes involved
      autoDecided: !!(ap && act.decision !== 'pending' && /^guardrail/.test(String(act.decided_by || '')) && !arr(ap.approved_by).length),
      blastRadius: ap ? ap.blast_radius : null, risk: ap ? ap.risk : null,
      policyRule: ap ? ap.policy_rule : null, reason: ap ? ap.reason : null, llmSummary: ap ? ap.llm_summary : null,
      required: ap ? ap.required_approvals : null, approvedBy: ap ? arr(ap.approved_by) : [],
      requestedBy: ap ? ap.requested_by : null, expiresAt: ap ? ap.expires_at : null,
      decidedBy: act.decided_by, execution: act.execution, executionResult: act.execution_result,
      hasApproval: !!ap, showExecution: true, payload: act.payload, actionId: act.action_id,
      // the backend creates the action first; "approval" stays null until the Guardrail has decided
      guardrailPending: !ap && act.decision === 'pending'
    };
  }

  function riskPill(risk) {
    if (!risk) return '<span class="faint">—</span>';
    return pill(risk, 'risk ' + risk);
  }

  function decisionPill(p) {
    if (p.guardrailPending) return pill('running', 'guardrail deciding…');
    if (p.approvalStatus === 'expired') return pill('expired');
    if (p.decision === 'approved' && (!p.hasApproval || p.autoDecided)) return pill('approved', 'auto-approved');
    if (p.decision === 'rejected' && p.autoDecided) return pill('rejected', 'auto-rejected');
    if (p.decision === 'pending') return pill('pending', 'pending approval');
    return pill(p.decision || 'unknown');
  }

  function isVotable(p) {
    return p.approvalId && p.decision === 'pending' && (p.approvalStatus === 'pending' || p.approvalStatus === null);
  }

  function proposalCard(p, opts) {
    opts = opts || {};
    var compact = !!opts.compact;
    var h = [];
    h.push('<div class="card prop-card' + (compact ? ' compact' : '') + '">');
    h.push('<div class="prop-head">');
    if (compact) h.push(riskPill(p.risk));
    h.push('<span class="prop-title">' + esc(agentTitle(p.agent)) + '<span class="sep">·</span><span class="mono">' + esc(p.actionType) + '</span></span>');
    if (p.environment) h.push('<span class="chip">' + esc(p.environment) + '</span>');
    h.push('<span class="spacer"></span>');
    h.push(decisionPill(p));
    h.push('</div><div class="prop-body">');
    var approvals = p.approvedBy.length ? esc(p.approvedBy.join(', ')) : '<span class="faint">none yet</span>';
    var req = p.required || 0;
    var runLink = p.runId ? '<a href="#/runs/' + esc(encodeURIComponent(p.runId)) + '">' + esc(p.runTitle || 'Open run') + ' →</a>' : '';

    if (compact) {
      var remaining = Math.max(0, req - p.approvedBy.length);
      h.push('<div class="muted small">' + (remaining ? 'Needs ' + esc(remaining) + ' more approval' + (remaining === 1 ? '' : 's') : 'Ready to decide') +
        ' · ' + approvals + ' (' + esc(p.approvedBy.length) + '/' + esc(req) + ')</div>');
      if (p.reason) h.push('<div class="small" style="color:var(--text-2)">' + esc(p.reason) + '</div>');
      if (runLink) h.push('<div class="small">' + runLink + '</div>');
    } else {
      if (p.hasApproval) {
        h.push('<div class="kv"><span class="k">Risk</span><span class="v">declared <b>' + esc(p.blastRadius || '—') + '</b> → ' + riskPill(p.risk) + '</span></div>');
        if (p.policyRule || p.reason) {
          h.push('<div class="kv"><span class="k">Policy</span><span class="v">' + (p.policyRule ? '<code>' + esc(p.policyRule) + '</code> ' : '') + esc(p.reason || '') + '</span></div>');
        }
        if (p.llmSummary) h.push('<div class="kv"><span class="k">Summary</span><span class="v ai-summary"><span class="ai-badge">AI</span>' + esc(p.llmSummary) + '</span></div>');
      }
      if (p.hasApproval && p.autoDecided) {
        h.push('<div class="kv"><span class="k">Guardrail</span><span class="v">' + (p.decision === 'rejected' ? 'Rejected' : 'Approved') +
          ' by policy, no human vote needed' + (p.decidedBy ? ' <span class="faint">(' + esc(p.decidedBy) + ')</span>' : '') + '</span></div>');
      } else if (p.hasApproval) {
        h.push('<div class="kv"><span class="k">Approvals</span><span class="v">' + approvals + ' (' + esc(p.approvedBy.length) + '/' + esc(req) + ')</span></div>');
        var reqLine = esc(p.requestedBy ? agentTitle(p.requestedBy) : '—');
        if (p.expiresAt && (p.decision === 'pending' || p.approvalStatus === 'expired')) {
          var exp = parseTime(p.expiresAt);
          var expired = p.approvalStatus === 'expired' || (exp && exp.getTime() < Date.now() && p.decision === 'pending');
          reqLine += ' <span class="faint">·</span> <span title="' + esc(fullTime(p.expiresAt)) + '">' +
            (expired ? 'expired ' : 'expires ') + esc(relTimeCoarse(p.expiresAt)) + '</span>';
        }
        h.push('<div class="kv"><span class="k">Requested by</span><span class="v">' + reqLine + '</span></div>');
        if (p.decision !== 'pending' && p.decidedBy) h.push('<div class="kv"><span class="k">Decided by</span><span class="v">' + esc(agentTitle(p.decidedBy)) + '</span></div>');
      } else if (p.guardrailPending) {
        h.push('<div class="kv"><span class="k">Guardrail</span><span class="v muted">Evaluating policy rules…</span></div>');
      } else {
        h.push('<div class="kv"><span class="k">Guardrail</span><span class="v">No human approval needed' + (p.decidedBy ? ' — decided by ' + esc(agentTitle(p.decidedBy)) : '') + '</span></div>');
      }
      if (runLink) h.push('<div class="kv"><span class="k">Run</span><span class="v">' + runLink + (p.repo ? ' <span class="chip">' + esc(p.repo) + '</span>' : '') + '</span></div>');
      if (p.showExecution) {
        var msg = p.executionResult && p.executionResult.message ? esc(p.executionResult.message) : '';
        h.push('<div class="kv"><span class="k">Execution</span><span class="v">' + pill(p.execution || 'not_run') + (msg ? ' <span style="margin-left:6px">' + msg + '</span>' : '') + '</span></div>');
      }
      var hasPayload = p.payload && typeof p.payload === 'object' && Object.keys(p.payload).length;
      if (hasPayload) {
        var pkey = 'payload:' + (p.actionId || p.approvalId);
        var open = !!state.expanded[pkey];
        h.push('<div class="kv"><span class="k">Payload</span><span class="v"><button class="btn ghost sm" type="button" data-action="toggle" data-key="' + esc(pkey) + '" style="margin-left:-8px">' + (open ? 'Hide payload' : 'Show payload') + '</button></span></div>');
        if (open) h.push('<pre class="json">' + esc(JSON.stringify(p.payload, null, 2)) + '</pre>');
      }
    }
    h.push('</div>');

    if (isVotable(p)) {
      var dis = state.busy['vote:' + p.approvalId] ? ' disabled' : '';
      var self = p.requestedBy && state.actingAs && p.requestedBy === state.actingAs;
      h.push('<div class="prop-actions">' +
        '<button class="btn primary sm" type="button" data-action="vote" data-vote="approve" data-id="' + esc(p.approvalId) + '"' + dis + '>Approve</button>' +
        '<button class="btn danger sm" type="button" data-action="vote" data-vote="reject" data-id="' + esc(p.approvalId) + '"' + dis + '>Reject</button>' +
        '<span class="hint">' + (self ? 'You requested this — someone else has to approve it' : 'Voting as ' + esc(state.actingAs || '(nobody)')) + '</span></div>');
    }
    h.push('</div>');
    return h.join('');
  }

  // ---- Triage ----------------------------------------------------------------

  function routeAgentsText(routeId) {
    var r = arr(state.routes).filter(function (x) { return x.id === routeId; })[0];
    if (!r) return 'Pick a route to see which agents will run.';
    var a = arr(r.agents).map(agentTitle).join(', ');
    return a ? 'Runs: ' + a : 'No agents on this route.';
  }

  function triagePanel(run, opts) {
    opts = opts || {};
    var choice = state.triageChoice[run.id] || '';
    var routes = arr(state.routes);
    var options = '<option value="">' + (routes.length ? 'Choose a route…' : 'Loading routes…') + '</option>' +
      routes.map(function (r) {
        return '<option value="' + esc(r.id) + '"' + (r.id === choice ? ' selected' : '') + '>' + esc(r.title || r.id) + '</option>';
      }).join('');
    var busy = !!state.busy['triage:' + run.id];
    var h = '<div class="card triage-panel">';
    if (opts.withTitle) {
      h += '<div class="prop-head"><a class="prop-title" href="#/runs/' + esc(encodeURIComponent(run.id)) + '">' + esc(run.title || 'Untitled run') + '</a>' +
        '<span class="spacer"></span><span class="muted small" title="' + esc(fullTime(run.created_at)) + '">' + esc(relTimeCoarse(run.created_at)) + '</span></div>';
    } else {
      h += '<h3>Pick a route</h3><p class="muted" style="margin-top:4px">Triage wasn\'t confident enough to route this run. Choose a route and the orchestrator starts the matching agents.</p>';
    }
    if (run.text) h += '<blockquote>' + esc(run.text) + '</blockquote>';
    else if (run.summary && opts.withTitle) h += '<p class="muted" style="margin:8px 0">' + esc(run.summary) + '</p>';
    if (opts.withTitle && typeof run.route_confidence === 'number') {
      h += '<p class="muted small" style="margin-bottom:8px">Triage confidence ' + esc(Math.round(run.route_confidence * 100)) + '%' +
        (run.route ? ' (best guess: <span class="mono">' + esc(run.route) + '</span>)' : '') + '</p>';
    }
    h += '<div class="row">' +
      '<select data-triage-select="' + esc(run.id) + '" aria-label="Route">' + options + '</select>' +
      '<button class="btn primary" type="button" data-action="triage" data-run="' + esc(run.id) + '"' + (busy ? ' disabled' : '') + '>Route run</button>' +
      '<span class="route-agents" data-route-agents="' + esc(run.id) + '">' + esc(routeAgentsText(choice)) + '</span>' +
      '</div></div>';
    return h;
  }

  function ensureRoutes() {
    if (state.routes) return Promise.resolve(state.routes);
    return api('GET', '/routes').then(function (r) { state.routes = arr(r); return state.routes; });
  }

  function ensureAgents() {
    if (state.agents) return Promise.resolve(state.agents);
    return api('GET', '/agents').then(function (r) { state.agents = arr(r); return state.agents; });
  }

  // ---- Pipeline strip (run detail) ------------------------------------------

  var ARROW_SVG = '<svg viewBox="0 0 22 12" aria-hidden="true"><path d="M1 6h19M15 1l5 5-5 5"/></svg>';

  function pipeNode(kicker, title, status, label) {
    var tone = toneOf(status);
    return '<div class="pipe-node tone-' + tone + '">' +
      '<div class="pipe-kicker">' + esc(kicker) + '</div>' +
      '<div class="pipe-title" title="' + esc(title) + '">' + esc(title) + '</div>' +
      '<div class="pipe-sub"><span class="dot dot-' + tone + '"></span>' + esc(label !== undefined ? label : humanize(status)) + '</div></div>';
  }

  function pipeline(run, steps, actions) {
    var arrow = '<div class="pipe-arrow">' + ARROW_SVG + '</div>';
    var parts = [];
    parts.push(pipeNode('Trigger', run.route || humanize(run.trigger_type) || 'trigger',
      run.status === 'needs_triage' ? 'needs_triage' : 'done',
      run.status === 'needs_triage' ? 'needs triage' : (run.routed_by ? 'routed by ' + run.routed_by : 'received')));
    if (steps.length) steps.forEach(function (s) { parts.push(pipeNode('Step ' + s.step_no, agentTitle(s.agent), s.status)); });
    else parts.push(pipeNode('Agents', run.status === 'needs_triage' ? 'Waiting for route' : 'Not started', 'not_run', '—'));

    var pending = actions.filter(function (a) { return a.decision === 'pending'; }).length;
    var approved = actions.filter(function (a) { return a.decision === 'approved'; }).length;
    var rejected = actions.filter(function (a) { return a.decision === 'rejected'; }).length;
    var g;
    if (!actions.length) g = ['not_run', /^(completed|failed|cancelled)$/.test(run.status) ? 'no proposals' : 'waiting'];
    else if (pending) g = ['pending', pending + ' pending'];
    else if (!approved && rejected) g = ['rejected', rejected + ' rejected'];
    else g = ['approved', approved + ' approved' + (rejected ? ', ' + rejected + ' rejected' : '')];
    parts.push(pipeNode('Policy', 'Guardrail', g[0], g[1]));

    var ex = actions.map(function (a) { return a.execution; });
    var e;
    if (ex.indexOf('failed') !== -1) e = ['failed', 'failed'];
    else if (run.status === 'executing') e = ['executing', 'executing'];
    else if (ex.indexOf('done') !== -1) e = ['done', ex.indexOf('dry_run') !== -1 ? 'done + dry run' : 'done'];
    else if (ex.indexOf('dry_run') !== -1) e = ['dry_run', 'dry run'];
    else if (pending) e = ['pending', 'waiting for approval'];
    else e = ['not_run', actions.length ? 'nothing to run' : 'not run'];
    parts.push(pipeNode('Orchestrator', 'Execute', e[0], e[1]));

    return '<div class="pipeline dotted">' + parts.join(arrow) + '</div>';
  }

  // ===========================================================================
  // 5. Pages
  // ===========================================================================

  var PAGES = {};

  function repoOptions() {
    var s = state.githubStatus || {};
    var list = arr(s.repos).length ? arr(s.repos) : (s.repo ? [s.repo] : []);
    return list.map(function (r) { return { value: r, label: r }; });
  }

  var FILTERS = {
    runs: [
      { key: 'repo', label: 'Repository', options: repoOptions, emptyText: 'No repositories connected' },
      { key: 'status', label: 'Status', options: function () {
        return ['received', 'needs_triage', 'running', 'waiting_approval', 'executing', 'completed', 'failed', 'cancelled']
          .map(function (v) { return { value: v, label: humanize(v) }; });
      } },
      { key: 'source', label: 'Source', options: function () {
        return [{ value: 'github', label: 'GitHub' }, { value: 'scenario', label: 'Scenario' }, { value: 'text', label: 'Incident text' }];
      } },
      { key: 'route', label: 'Route', options: function () {
        return arr(state.routes).map(function (r) { return { value: r.id, label: r.title || r.id }; });
      } }
    ],
    activity: [
      { key: 'repo', label: 'Repository', options: repoOptions, emptyText: 'No repositories connected' },
      { key: 'kind', label: 'Type', options: function () { return [{ value: 'commits', label: 'Commits' }, { value: 'pulls', label: 'Pull requests' }]; } },
      { key: 'ci', label: 'CI status', options: function () {
        return [{ value: 'failure', label: 'Failing only' }, { value: 'success', label: 'Passing' }, { value: 'running', label: 'Running' }, { value: 'none', label: 'No CI' }];
      } },
      { key: 'branch', label: 'Branch', options: function () {
        var p = state.route.name === 'activity' && state.page ? arr(state.page.branches) : [];
        return p.map(function (b) { return { value: b, label: b }; });
      }, emptyText: 'No branches synced yet' },
      { key: 'author', label: 'Author', options: function () {
        var seen = {};
        var items = state.route.name === 'activity' && state.page ? arr(state.page.items) : [];
        items.forEach(function (i) { if (i.author) seen[i.author] = true; });
        return Object.keys(seen).sort().map(function (a) { return { value: a, label: a }; });
      } },
      { key: 'number', label: 'PR number', icon: 'hash', input: 'number', placeholder: 'e.g. 12' }
    ],
    approvals: [
      { key: 'repo', label: 'Repository', options: repoOptions, emptyText: 'No repositories connected' },
      { key: 'risk', label: 'Risk', options: function () { return ['critical', 'high', 'medium', 'low'].map(function (v) { return { value: v, label: v }; }); } },
      { key: 'agent', label: 'Agent', options: agentOptions },
      { key: 'environment', label: 'Environment', options: function () {
        var seen = {};
        if (state.route.name === 'approvals') arr(state.page).forEach(function (a) { if (a.environment) seen[a.environment] = true; });
        return Object.keys(seen).sort().map(function (e) { return { value: e, label: e }; });
      } }
    ],
    alerts: [
      { key: 'severity', label: 'Severity', options: function () { return ['critical', 'warning', 'info', 'success'].map(function (v) { return { value: v, label: v }; }); } },
      { key: 'source', label: 'Source', options: function () { return ['github', 'guardrail', 'orchestrator', 'system'].map(function (v) { return { value: v, label: v }; }); } },
      { key: 'read', label: 'Status', options: function () { return [{ value: 'unread', label: 'Unread' }, { value: 'read', label: 'Read' }]; } }
    ],
    analytics: [
      { key: 'days', label: 'Range', required: true, options: function () {
        return [{ value: '7', label: 'Last 7 days' }, { value: '30', label: 'Last 30 days' }, { value: '90', label: 'Last 90 days' }];
      }, display: function (v) {
        var days = Number(v) || 30;
        var range = state.route.name === 'analytics' && state.page && state.page.range;
        var to = range && range.to ? parseDay(range.to) : new Date();
        var from = range && range.from ? parseDay(range.from) : new Date(Date.now() - (days - 1) * 86400000);
        var f = function (d) { return d ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '?'; };
        return days + ' days · ' + f(from) + ' – ' + f(to);
      } },
      { key: 'repo', label: 'Repositories', options: repoOptions, emptyText: 'No repositories connected' },
      { key: 'agent', label: 'Agent', options: agentOptions }
    ]
  };

  function agentOptions() {
    var list = arr(state.agents).length ? arr(state.agents).map(function (a) { return a.name; }) : Object.keys(AGENT_TITLES).filter(function (n) { return n !== 'guardrail' && n !== 'orchestrator'; });
    return list.map(function (n) { return { value: n, label: agentTitle(n) }; });
  }

  // ---- Home ----
  PAGES.home = {
    title: 'Home',
    shell: function () {
      return '' +
        '<div id="home-health"><div class="card"><div class="skel skel-title"></div><div class="skel skel-line"></div></div></div>' +
        '<div id="home-stats" class="stats"></div>' +
        '<section id="start-run"><div class="section-head"><h2>Start a run</h2><span class="muted small">Each scenario feeds a sample trigger to the orchestrator.</span></div><div id="home-scenarios">' + skeletonRows(2) + '</div></section>' +
        '<section class="card incident"><h2>Describe an incident</h2>' +
          '<p class="muted">Plain words are fine. LLM triage picks the agents; if it isn\'t sure, the run lands in Triage for a human.</p>' +
          '<textarea id="incident-text" rows="3" placeholder="e.g. The nightly training job fails with CUDA out of memory since yesterday\'s merge">' + esc(state.incidentDraft) + '</textarea>' +
          '<div class="row-end"><span class="hint">Ctrl+Enter to submit</span><button class="btn primary" type="button" id="incident-btn" data-action="submit-incident">Analyse incident</button></div>' +
        '</section>' +
        '<section><div class="section-head"><h2>Waiting for you</h2><a href="#/approvals">All approvals →</a></div><div id="home-waiting">' + skeletonRows(1) + '</div></section>' +
        '<section><div class="section-head"><h2>Recent runs</h2><div class="row"><div class="search">' + ICON.search +
          '<input id="home-search" type="search" placeholder="Search runs" aria-label="Search runs" value="' + esc(state.search) + '"></div><a href="#/runs">All runs →</a></div></div>' +
          '<div id="home-recent">' + skeletonRows(3) + '</div></section>';
    },
    fetch: function () {
      return Promise.all([
        state.scenarios ? Promise.resolve(state.scenarios) : api('GET', '/scenarios'),
        api('GET', '/approvals'),
        api('GET', '/runs?limit=50')
      ]).then(function (r) {
        state.scenarios = arr(r[0]);
        return { scenarios: state.scenarios, approvals: arr(r[1]), runs: arr(r[2]) };
      });
    },
    render: function (d) {
      renderHomeOverview();
      region('home-scenarios', d.scenarios.length ? '<div class="grid grid-3">' + d.scenarios.map(function (s) {
        var busy = !!state.busy['scenario:' + s.id];
        return '<div class="card scenario"><h3>' + esc(s.title || s.id) + '</h3>' +
          '<p>' + esc(s.description || '') + '</p>' +
          '<div class="chips">' + arr(s.agents).map(function (a) { return '<span class="chip">' + esc(agentTitle(a)) + '</span>'; }).join('') + '</div>' +
          '<div class="row-end"><span class="hint mono">' + esc(s.trigger_type || '') + '</span>' +
          '<button class="btn sm" type="button" data-action="start-scenario" data-id="' + esc(s.id) + '"' + (busy ? ' disabled' : '') + '>' + (busy ? 'Starting…' : 'Start') + '</button></div></div>';
      }).join('') + '</div>' : emptyState({ small: true, title: 'No scenarios available', text: 'The backend did not return any demo scenarios.' }));

      var waiting = d.approvals.slice(0, 4);
      region('home-waiting', waiting.length
        ? '<div class="grid grid-2">' + waiting.map(function (a) { return proposalCard(fromApproval(a), { compact: true }); }).join('') + '</div>'
        : emptyState({ small: true, glyph: 'shield', title: 'Nothing waiting for you', text: 'The Guardrail has not held any proposals for a human.' }));

      var q = state.search.trim().toLowerCase();
      var runs = d.runs;
      if (q) {
        runs = runs.filter(function (r) {
          var hay = [r.title, r.trigger_type, r.route, r.status, r.summary, r.text, r.routed_by, sourceLabel(r)]
            .concat(arr(r.agents), arr(r.agents).map(agentTitle)).join(' ').toLowerCase();
          return hay.indexOf(q) !== -1;
        });
      }
      region('home-recent', runsTable(runs.slice(0, 10), q ? { glyph: 'search', small: true, title: 'No matching runs', text: 'Nothing matches “' + state.search.trim() + '”.' } : undefined));
      if (state.scrollToStart) {
        state.scrollToStart = false;
        var s = $('start-run');
        if (s) s.scrollIntoView({ block: 'start' });
      }
    }
  };

  function renderHomeOverview() {
    renderHealth();
    if (!$('home-stats')) return;
    var o = state.overview;
    if (!o) return;
    var r = o.runs || {};
    var tiles = [
      ['Total runs', num(r.total), ''],
      ['Running', num(r.running) + num(r.executing), (r.running ? 'amber' : '')],
      ['Pending approvals', num(o.pending_approvals), (o.pending_approvals ? 'amber' : '')],
      ['Failed runs', num(r.failed), (r.failed ? 'red' : '')]
    ];
    region('home-stats', tiles.map(function (t) {
      return '<div class="stat"><div class="stat-label">' + esc(t[0]) + '</div><div class="stat-value ' + t[2] + '">' + esc(t[1]) + '</div></div>';
    }).join(''));
  }

  function renderHealth() {
    if (!$('home-health')) return;
    var o = state.overview;
    if (!o) return;
    var g = o.github || {};
    var unread = num(o.alerts_unread);
    if (!g.connected || (Array.isArray(g.repos) && !g.repos.length && !g.repo)) {
      var signed = ghSignedIn();
      region('home-health', '<div class="card health dotted"><div class="health-main">' + ciBadge('none', { large: true, noneLabel: 'No checks' }) +
        '<div class="health-commit"><div class="k">Repository health</div><div class="msg">' + (signed ? 'No repositories followed yet' : 'GitHub is not connected') + '</div>' +
        '<div class="meta2">' + (signed ? 'Choose the repositories to watch on the <a href="#/repositories">Repositories</a> page.' : '<a href="#/settings">Sign in with GitHub</a> to watch commits, pull requests and CI.') + '</div></div></div>' +
        '<a class="btn ' + (signed ? '' : 'gh-btn') + '" href="' + (signed ? '#/repositories' : '#/settings') + '">' + (signed ? 'Choose repositories' : GH_MARK + 'Sign in with GitHub') + '</a></div>');
      return;
    }
    var head = g.head;
    var main;
    if (head) {
      var url = safeUrl(head.html_url);
      var msg = esc(firstLine(head.message) || '(no message)');
      main = ciBadge(head.ci, { large: true, failLabel: 'Failing', noneLabel: 'No checks' }) +
        '<div class="health-commit"><div class="k">Last commit on ' + (head.branch ? '<span class="mono">' + esc(head.branch) + '</span>' : 'the default branch') +
        (g.repo ? ' · ' + esc(g.repo) : '') + '</div>' +
        '<div class="msg">' + (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + msg + '</a>' : msg) + '</div>' +
        '<div class="meta2"><span class="sha">' + esc(head.short_sha || '') + '</span>' + (head.author ? ' · ' + esc(head.author) : '') +
        ' · <span title="' + esc(fullTime(head.time)) + '">' + esc(relTimeCoarse(head.time)) + '</span></div></div>';
    } else {
      main = ciBadge('none', { large: true, noneLabel: 'No checks' }) +
        '<div class="health-commit"><div class="k">Repository health · ' + esc(g.repo || '') + '</div><div class="msg">No commits synced yet</div></div>';
    }
    var failing = num(g.failing_today);
    region('home-health', '<div class="card health dotted"><div class="health-main">' + main + '</div>' +
      '<div class="health-counts">' +
      '<a href="#/activity" data-action="goto-activity" data-ci="" data-kind="pulls"><div class="n">' + esc(num(g.open_prs)) + '</div><div class="l">Open PRs</div></a>' +
      '<a href="#/activity" data-action="goto-activity" data-ci="failure" data-kind=""><div class="n' + (failing ? ' red' : '') + '">' + esc(failing) + '</div><div class="l">Failing checks today</div></a>' +
      '<a href="#/alerts"><div class="n' + (unread ? ' amber' : '') + '">' + esc(unread) + '</div><div class="l">Open alerts</div></a>' +
      '</div></div>');
  }

  // ---- Runs ----
  PAGES.runs = {
    title: 'Runs',
    shell: function () {
      return '<div id="toolbar"></div><div id="runs-table">' + skeletonRows(5) + '</div>';
    },
    fetch: function () {
      var f = state.filters.runs;
      ensureRoutes().catch(function () {});
      return api('GET', '/runs' + qs({ limit: 50, status: f.status, repo: f.repo })).then(arr);
    },
    render: function (runs) {
      var f = state.filters.runs;
      region('toolbar', toolbarHTML('runs'));
      var list = runs.filter(function (r) {
        if (f.status && r.status !== f.status) return false;
        if (f.source && sourceKey(r) !== f.source) return false;
        if (f.route && r.route !== f.route) return false;
        if (f.repo && r.trigger && r.trigger.repo && r.trigger.repo !== f.repo) return false;
        return true;
      });
      var filtered = Object.keys(f).some(function (k) { return hasVal(f[k]); });
      region('runs-table', runsTable(list, filtered ? { glyph: 'search', title: 'No runs match these filters', text: 'Try removing a filter.', action: { label: 'Clear filters', action: 'filter-clear-all', attrs: ' data-page="runs"' } } : undefined));
    }
  };

  // ---- Run detail ----
  PAGES.run = {
    title: 'Run',
    shell: function () {
      return '<div id="run-head"><a class="back" href="#/runs">← Runs</a><div class="skel skel-title" style="margin-top:10px"></div><div class="skel skel-line"></div></div>' +
        '<div id="run-triage"></div>' +
        '<div id="run-pipeline"></div>' +
        '<section><h2>Agent steps</h2><div id="run-steps"></div></section>' +
        '<section><h2>Proposals</h2><div id="run-actions"></div></section>' +
        '<section><h2>Timeline</h2><div id="run-timeline"></div></section>';
    },
    fetch: function (params) {
      return api('GET', '/runs/' + encodeURIComponent(params.id)).then(function (d) {
        if (d && d.run && d.run.status === 'needs_triage' && !state.routes) {
          return ensureRoutes().then(function () { return d; }, function () { return d; });
        }
        return d;
      });
    },
    render: function (d, params) {
      var run = d.run || {};
      var steps = arr(d.steps), actions = arr(d.actions), timeline = arr(d.timeline);
      setPageTitle([['Runs', '#/runs'], [run.title || params.id]]);
      var gh = run.trigger && safeUrl(run.trigger.html_url);
      var head = '<a class="back" href="#/runs">← Runs</a>' +
        '<div class="run-head"><h1>' + esc(run.title || 'Untitled run') + '</h1>' + pill(run.status) +
        routedByChip(run.routed_by, run.route_confidence, true) + sourceChip(run) +
        (gh ? '<a class="btn sm ghost" href="' + esc(gh) + '" target="_blank" rel="noopener noreferrer">' + ICON.external + 'View on GitHub</a>' : '') + '</div>';
      if (run.summary) head += '<p class="run-summary">' + esc(run.summary) + '</p>';
      head += '<div class="meta">' +
        '<span>Started <b title="' + esc(fullTime(run.created_at)) + '">' + fmtTime(run.created_at) + '</b></span>' +
        '<span>Duration <b>' + esc(fmtDuration(run.created_at, run.finished_at)) + '</b></span>' +
        (run.trigger_type ? '<span>Trigger <b class="mono">' + esc(run.trigger_type) + '</b></span>' : '') +
        (run.trigger && run.trigger.repo ? '<span>Repository <b>' + esc(run.trigger.repo) + '</b></span>' : '') +
        (run.route ? '<span>Route <b class="mono">' + esc(run.route) + '</b></span>' : '') +
        '<span>Run <b class="mono">' + esc(String(run.id || params.id).slice(0, 8)) + '</b></span></div>';
      if (run.text) head += '<div class="meta"><span>Reported: <b>' + esc(run.text) + '</b></span></div>';
      if (run.error) head += '<div class="error-box">' + esc(typeof run.error === 'string' ? run.error : JSON.stringify(run.error)) + '</div>';
      region('run-head', head);

      region('run-triage', run.status === 'needs_triage' ? triagePanel(run) : '');
      region('run-pipeline', pipeline(run, steps, actions));

      region('run-steps', steps.length ? steps.map(function (s) {
        var key = 'step:' + run.id + ':' + s.step_no;
        var open = !!state.expanded[key];
        var hasOutput = s.output !== null && s.output !== undefined && !(typeof s.output === 'object' && Object.keys(s.output).length === 0);
        return '<div class="card"><div class="step-head">' +
          '<span class="step-no">' + esc(s.step_no) + '</span>' +
          '<strong>' + esc(agentTitle(s.agent)) + '</strong>' + pill(s.status) +
          '<span class="muted small">' + esc(s.started_at ? fmtDuration(s.started_at, s.finished_at) : '') + '</span>' +
          '<span class="spacer"></span>' +
          (hasOutput ? '<button class="btn ghost sm" type="button" data-action="toggle" data-key="' + esc(key) + '">' + (open ? 'Hide details' : 'Show details') + '</button>' : '') +
          '</div>' +
          (s.summary ? '<p class="step-summary">' + esc(s.summary) + '</p>' : (s.status === 'running' ? '<p class="step-summary muted">Working…</p>' : '')) +
          (s.error ? '<div class="error-box">' + esc(typeof s.error === 'string' ? s.error : JSON.stringify(s.error)) + '</div>' : '') +
          (open && hasOutput ? '<pre class="json">' + esc(JSON.stringify(s.output, null, 2)) + '</pre>' : '') +
          '</div>';
      }).join('') : emptyState({ small: true, title: run.status === 'needs_triage' ? 'Waiting for a route' : 'No agent steps yet', text: run.status === 'needs_triage' ? 'Pick a route above and the agents start working.' : 'The orchestrator has not started any agent.' }));

      region('run-actions', actions.length ? actions.map(function (a) { return proposalCard(fromAction(a)); }).join('')
        : emptyState({ small: true, glyph: 'shield', title: 'No proposals', text: /^(completed|failed)$/.test(run.status) ? 'The agents did not propose any actions.' : 'Proposals show up here as agents finish.' }));

      var items = timeline.slice().sort(function (a, b) {
        var ta = parseTime(a.time), tb = parseTime(b.time);
        return (ta ? ta.getTime() : 0) - (tb ? tb.getTime() : 0);
      });
      region('run-timeline', items.length ? '<ul class="timeline">' + items.map(function (t) {
        return '<li><time title="' + esc(fullTime(t.time)) + '">' + fmtClock(t.time) + '</time>' +
          '<span class="actor">' + esc(agentTitle(t.actor)) + '</span>' +
          '<span class="text"><span class="kind">' + esc(t.kind || '') + '</span>' + esc(t.text) + '</span></li>';
      }).join('') + '</ul>' : emptyState({ small: true, title: 'Nothing has happened yet' }));
    }
  };

  // ---- Approvals ----
  PAGES.approvals = {
    title: 'Approvals',
    shell: function () {
      return '<p class="page-intro">Proposals the Guardrail held for a human. You vote as the “Acting as” user (bottom of the sidebar).</p>' +
        '<div id="toolbar"></div><div id="approvals-list">' + skeletonRows(3) + '</div>';
    },
    fetch: function () {
      ensureAgents().catch(function () {});
      return api('GET', '/approvals' + qs({ repo: state.filters.approvals.repo })).then(arr);
    },
    render: function (list) {
      var f = state.filters.approvals;
      region('toolbar', toolbarHTML('approvals'));
      var shown = list.filter(function (a) {
        if (f.risk && a.risk !== f.risk) return false;
        if (f.agent && a.agent !== f.agent) return false;
        if (f.environment && a.environment !== f.environment) return false;
        if (f.repo && a.repo && a.repo !== f.repo) return false;
        return true;
      });
      region('approvals-list', shown.length
        ? shown.map(function (a) { return proposalCard(fromApproval(a)); }).join('')
        : (list.length
          ? emptyState({ glyph: 'search', title: 'No approvals match these filters', action: { label: 'Clear filters', action: 'filter-clear-all', attrs: ' data-page="approvals"' } })
          : emptyState({ glyph: 'shield', title: 'Nothing waiting for approval', text: 'When the Guardrail holds a risky proposal, it shows up here.' })));
    }
  };

  // ---- Triage ----
  PAGES.triage = {
    title: 'Triage',
    shell: function () {
      return '<p class="page-intro">Runs the LLM couldn\'t route with confidence. Pick a route to start the right agents.</p><div id="triage-list">' + skeletonRows(2) + '</div>';
    },
    fetch: function () {
      return Promise.all([api('GET', '/runs?limit=50&status=needs_triage'), ensureRoutes().catch(function () { return []; })])
        .then(function (r) { return arr(r[0]).filter(function (x) { return !x.status || x.status === 'needs_triage'; }); });
    },
    render: function (runs) {
      region('triage-list', runs.length
        ? runs.map(function (r) { return triagePanel(r, { withTitle: true }); }).join('')
        : emptyState({ title: 'Triage queue is empty', text: 'Every run found its way. Free-text incidents land here when the LLM is unsure.' }));
    }
  };

  // ---- Agent registry ----
  PAGES.agents = {
    title: 'Agent registry',
    shell: function () {
      return '<p class="page-intro">Specialists the orchestrator can call, their models and the actions each may propose.</p><div id="agents-list">' + skeletonRows(3) + '</div>';
    },
    fetch: function () { return api('GET', '/agents').then(function (a) { state.agents = arr(a); return state.agents; }); },
    render: function (agents) {
      region('agents-list', agents.length ? '<div class="grid grid-2">' + agents.map(function (a) {
        var tone = toneOf(a.status);
        return '<div class="card agent-card"><div class="agent-head">' +
          '<div class="agent-icon" aria-hidden="true">' + esc(initialsOf(agentTitle(a.name))) + '</div>' +
          '<div class="grow"><h3>' + esc(a.title || agentTitle(a.name)) + '</h3>' +
          '<div class="muted small"><span class="mono">' + esc(a.name) + '</span>' + (a.owner ? ' · owner ' + esc(a.owner) : '') + '</div></div>' +
          pill(a.status) + '</div>' +
          (a.description ? '<p class="desc">' + esc(a.description) + '</p>' : '') +
          '<div class="kv"><span class="k">Model</span><span class="v">' + (a.model ? '<code>' + esc(a.model) + '</code>' : '<span class="faint">none (rule-based)</span>') + '</span></div>' +
          (a.reason ? '<div class="reason tone-' + tone + '">' + esc(a.reason) + '</div>' : '') +
          '<div class="actions-label">May propose</div>' +
          '<div class="chips">' + (arr(a.actions).length ? arr(a.actions).map(function (x) { return '<span class="chip mono">' + esc(x) + '</span>'; }).join('') : '<span class="faint small">No actions (analysis only)</span>') + '</div>' +
          '</div>';
      }).join('') + '</div>' : emptyState({ title: 'No agents registered' }));
    }
  };

  // ---- Audit ----
  PAGES.audit = {
    title: 'Audit log',
    shell: function () {
      return '<p class="page-intro">Every agent decision, hash-chained so tampering shows up.</p><div id="audit-banner"></div><div id="audit-table">' + skeletonRows(6) + '</div>';
    },
    fetch: function () { return api('GET', '/audit?limit=100'); },
    render: function (d) {
      d = d || {};
      var rows = arr(d.rows), broken = arr(d.broken_rows), brokenMap = {};
      broken.forEach(function (b) { brokenMap[String(b.id)] = b.problem || 'hash mismatch'; });
      if (d.intact) {
        region('audit-banner', '<div class="banner ok"><span class="dot dot-green"></span>Hash chain intact (' + esc(rows.length) + ' row' + (rows.length === 1 ? '' : 's') + ' shown)</div>');
      } else {
        region('audit-banner', '<div class="banner bad"><div><span class="dot dot-red"></span> Hash chain broken — ' + esc(broken.length) + ' row' + (broken.length === 1 ? '' : 's') + ' failed verification</div>' +
          (broken.length ? '<ul>' + broken.map(function (b) { return '<li>Row #' + esc(b.id) + ': ' + esc(b.problem || 'hash mismatch') + '</li>'; }).join('') + '</ul>' : '') + '</div>');
      }
      if (!rows.length) { region('audit-table', emptyState({ glyph: 'shield', title: 'The audit log is empty' })); return; }
      region('audit-table', '<div class="table-wrap"><table class="table"><thead><tr>' +
        '<th class="num">#</th><th>Time</th><th>Agent</th><th>Decision</th><th>Input</th><th>Hash (prev → this)</th>' +
        '</tr></thead><tbody>' + rows.map(function (r) {
          var bad = brokenMap[String(r.id)];
          var prev = r.prev_hash ? String(r.prev_hash).slice(0, 8) : 'genesis';
          var cur = r.row_hash ? String(r.row_hash).slice(0, 8) : '—';
          return '<tr' + (bad ? ' class="broken" title="' + esc(bad) + '"' : '') + '>' +
            '<td class="num muted">' + esc(r.id) + '</td>' +
            '<td class="nowrap" title="' + esc(fullTime(r.created_at)) + '">' + fmtTime(r.created_at) + '</td>' +
            '<td class="nowrap">' + esc(agentTitle(r.agent_name)) + '</td>' +
            '<td>' + (r.decision ? pill(r.decision) : '<span class="faint">—</span>') + '</td>' +
            '<td class="wrap">' + esc(r.input_summary || '') + (bad ? '<div class="small" style="color:var(--red)">' + esc(bad) + '</div>' : '') + '</td>' +
            '<td><span class="hash">' + esc(prev) + ' → <b>' + esc(cur) + '</b></span></td></tr>';
        }).join('') + '</tbody></table></div>');
    }
  };

  // ---- Activity (GitHub commits + PRs) ----
  PAGES.activity = {
    title: 'Activity',
    shell: function () {
      return '<div id="toolbar"></div><div id="activity-feed">' + skeletonRows(6) + '</div>';
    },
    fetch: function () {
      var f = state.filters.activity;
      return api('GET', '/github/activity' + qs({
        kind: f.kind || 'all', branch: f.branch, repo: f.repo, failing: f.ci === 'failure' ? 1 : null
      }));
    },
    render: function (d) {
      d = d || {};
      var f = state.filters.activity;
      var multiRepo = repoOptions().length > 1 && !f.repo;
      region('toolbar', toolbarHTML('activity', d.connected === false ? '' : '<button class="btn sm ghost" type="button" data-action="gh-sync">' + ICON.sync + 'Sync now</button>'));
      if (d.connected === false || (state.githubStatus && ghSignedIn() && !ghRepoList().length)) {
        region('activity-feed', ghSignedIn()
          ? emptyState({ glyph: 'search', title: 'Choose repositories to follow', text: 'You are signed in to GitHub. Pick the repositories OpsAgents should watch and their commits and pull requests appear here.', action: { label: 'Choose repositories', href: '#/repositories' } })
          : emptyState({ glyph: 'plug', title: 'GitHub is not connected', text: 'Sign in with GitHub, then pick repositories to see their commits, pull requests and CI results here.', action: { label: 'Sign in with GitHub', href: '#/settings' } }));
        return;
      }
      var items = arr(d.items).filter(function (i) {
        if (f.kind === 'commits' && i.kind !== 'commit') return false;
        if (f.kind === 'pulls' && i.kind !== 'pull_request') return false;
        if (f.ci && i.ci !== f.ci) return false;
        if (f.branch && i.branch !== f.branch) return false;
        if (f.author && i.author !== f.author) return false;
        if (hasVal(f.number) && String(i.number) !== String(f.number)) return false;
        if (f.repo && i.repo && i.repo !== f.repo) return false;
        return true;
      });
      if (!items.length) {
        var filtered = Object.keys(f).some(function (k) { return hasVal(f[k]); });
        region('activity-feed', filtered
          ? emptyState({ glyph: 'search', title: 'No activity matches these filters', action: { label: 'Clear filters', action: 'filter-clear-all', attrs: ' data-page="activity"' } })
          : emptyState({ title: 'No activity yet', text: 'Push a commit or open a pull request and it shows up here after the next sync.' }));
        return;
      }
      region('activity-feed', '<div class="feed">' + items.map(function (i) { return activityRow(i, multiRepo); }).join('') + '</div>');
    }
  };

  function activityRow(i, showRepo) {
    var isPr = i.kind === 'pull_request';
    var url = safeUrl(i.html_url);
    var runUrl = safeUrl(i.run_url);
    var title = isPr ? (i.title || firstLine(i.message)) : firstLine(i.message || i.title);
    var shaHtml = i.short_sha || (i.sha ? String(i.sha).slice(0, 7) : '');
    shaHtml = shaHtml ? (url && !isPr ? '<a class="sha" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(shaHtml) + '</a>' : '<span class="sha">' + esc(shaHtml) + '</span>') : '';
    var titleHtml = (isPr && hasVal(i.number) ? '<span class="num">#' + esc(i.number) + '</span>' : '') + esc(title || '(no message)');
    if (isPr && url) titleHtml = '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" style="color:inherit">' + titleHtml + '</a>';

    var right = [];
    if (isPr && i.state) right.push(pill(i.state));
    right.push(ciBadge(i.ci));
    if (i.ci === 'failure' && runUrl) right.push('<a class="small" href="' + esc(runUrl) + '" target="_blank" rel="noopener noreferrer">View run ↗</a>');
    if (i.run_id) {
      right.push('<a class="btn sm" href="#/runs/' + esc(encodeURIComponent(i.run_id)) + '">Investigated →</a>');
    } else if (i.ci === 'failure') {
      var key = 'investigate:' + (isPr ? 'pr' + i.number : i.sha);
      var busy = !!state.busy[key];
      right.push('<button class="btn sm primary" type="button" data-action="investigate" data-kind="' + (isPr ? 'pull_request' : 'commit') + '"' +
        ' data-sha="' + esc(i.sha || '') + '" data-number="' + esc(hasVal(i.number) ? i.number : '') + '" data-repo="' + esc(i.repo || '') + '"' +
        (busy ? ' disabled' : '') + '>' + (busy ? 'Starting…' : 'Investigate') + '</button>');
    }
    return '<div class="feed-row">' +
      '<span class="kind-icon" title="' + (isPr ? 'Pull request' : 'Commit') + '">' + (isPr ? ICON.pr : ICON.commit) + '</span>' +
      '<div class="feed-main"><div class="feed-title">' + titleHtml + '</div>' +
      '<div class="feed-sub">' + (shaHtml ? shaHtml + '<span class="faint">·</span>' : '') +
        '<span class="author">' + avatar(i.author, i.avatar_url) + esc(i.author || 'unknown') + '</span>' +
        (i.branch ? '<span class="chip mono">' + esc(i.branch) + '</span>' : '') +
        (showRepo && i.repo ? '<span class="chip">' + esc(i.repo) + '</span>' : '') +
        '<span title="' + esc(fullTime(i.time)) + '">' + esc(relTimeCoarse(i.time)) + '</span></div></div>' +
      '<div class="feed-right">' + right.join('') + '</div></div>';
  }

  // ---- Alerts ----
  PAGES.alerts = {
    title: 'Alerts',
    shell: function () {
      return '<div id="toolbar"></div><div id="alerts-list">' + skeletonRows(4) + '</div>';
    },
    // The alert list is polled globally every tick (state.alerts); nothing extra to fetch.
    fetch: function () { return Promise.resolve(true); },
    render: function () {
      var f = state.filters.alerts;
      var all = state.alerts;
      var unread = arr(all).filter(function (a) { return !a.read; }).length;
      region('toolbar', toolbarHTML('alerts', notifyButtonHTML() +
        '<button class="btn sm" type="button" data-action="alerts-read-all"' + (unread ? '' : ' disabled') + '>Mark all read</button>'));
      if (all === null) return;
      var list = arr(all).slice().sort(function (a, b) {
        var ta = parseTime(a.created_at), tb = parseTime(b.created_at);
        return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
      }).filter(function (a) {
        if (f.severity && a.severity !== f.severity) return false;
        if (f.source && a.source !== f.source) return false;
        if (f.read === 'unread' && a.read) return false;
        if (f.read === 'read' && !a.read) return false;
        return true;
      });
      if (!list.length) {
        region('alerts-list', arr(all).length
          ? emptyState({ glyph: 'search', title: 'No alerts match these filters', action: { label: 'Clear filters', action: 'filter-clear-all', attrs: ' data-page="alerts"' } })
          : emptyState({ glyph: 'bell', title: 'No alerts', text: 'CI failures, held approvals and system problems will show up here.' }));
        return;
      }
      region('alerts-list', '<div class="feed">' + list.map(function (a) {
        var l = alertLink(a.link);
        var link = l ? '<a href="' + esc(l.href) + '"' + (l.external ? ' target="_blank" rel="noopener noreferrer"' : '') + ' data-action="alert-follow" data-id="' + esc(a.id) + '">' + (l.external ? 'Open ↗' : 'Open →') + '</a>' : '';
        return '<div class="alert-row' + (a.read ? '' : ' unread') + '">' + sevIcon(a.severity) +
          '<div class="body"><div class="t">' + esc(a.title || 'Alert') + '</div>' +
          (a.message ? '<div class="m">' + esc(a.message) + '</div>' : '') +
          '<div class="f">' + (a.read ? '' : '<span class="unread-dot" title="Unread"></span>') +
          '<span title="' + esc(fullTime(a.created_at)) + '">' + esc(relTimeCoarse(a.created_at)) + '</span>' +
          (a.source ? '<span class="chip">' + esc(a.source) + '</span>' : '') + link + '</div></div>' +
          (a.read ? '' : '<button class="btn sm ghost" type="button" data-action="alert-read" data-id="' + esc(a.id) + '">Mark read</button>') +
          '</div>';
      }).join('') + '</div>');
    }
  };

  function notifySupported() { try { return 'Notification' in window; } catch (e) { return false; } }

  function notifyButtonHTML() {
    if (!notifySupported()) return '<span class="hint">Desktop alerts not supported in this browser</span>';
    var p;
    try { p = window.Notification.permission; } catch (e) { p = 'denied'; }
    if (p === 'granted') return '<span class="hint">' + '<span class="dot dot-green"></span> Desktop alerts on</span>';
    if (p === 'denied') return '<span class="hint">Desktop alerts blocked in browser settings</span>';
    return '<button class="btn sm" type="button" data-action="notify-enable">Enable desktop alerts</button>';
  }

  // ---- GitHub sign-in (shared by Settings, Repositories, header chip) ----

  var GH_MARK = '<svg class="gh-mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>';

  // signed_in = we hold a GitHub token + login (possibly with zero followed repos).
  function ghSignedIn(s) {
    s = s || state.githubStatus || {};
    if (s.signed_in !== undefined && s.signed_in !== null) return !!s.signed_in;
    return !!s.connected;
  }
  function ghRepoList(s) {
    s = s || state.githubStatus || {};
    return arr(s.repos).length ? arr(s.repos) : (s.repo ? [s.repo] : []);
  }

  function tokenDisclosure() {
    return '<details class="disclosure"><summary>Or paste a personal access token</summary>' +
      '<form id="gh-token-form" class="form-grid two" autocomplete="off" style="margin-top:10px">' +
      '<label class="field"><span>Personal access token</span><input id="gh-token" type="password" placeholder="ghp_… or github_pat_…" autocomplete="new-password" spellcheck="false"></label>' +
      '<button class="btn" type="submit" id="gh-token-btn">Use token</button></form>' +
      '<p class="hint" style="margin-top:6px">Needs read access to Contents, Actions, Checks and Pull requests. It is sent once to the server and never shown again.</p>' +
      '<div class="form-error" id="gh-token-error"></div></details>';
  }

  // The forms only change when the sign-in mode changes, so typing is never interrupted by polling.
  function ghFormsHTML(mode) {
    if (mode === 'setup') {
      return '<div class="gh-setup"><p><b>Browser sign-in needs a GitHub OAuth App, created once.</b></p>' +
        '<p class="muted" style="margin-top:4px">On GitHub: Settings → Developer settings → OAuth Apps → New OAuth App. Use <code>http://localhost:8080</code> as Homepage and callback URL, tick “Enable Device Flow”, then paste the Client ID here.</p>' +
        '<form id="gh-client-form" class="form-grid two" autocomplete="off" style="margin-top:12px">' +
        '<label class="field"><span>OAuth App Client ID</span><input id="gh-client-id" type="text" placeholder="e.g. Ov23li…" spellcheck="false"></label>' +
        '<button class="btn primary" type="submit" id="gh-client-btn">Save</button></form>' +
        '<div class="form-error" id="gh-client-error"></div></div>' + tokenDisclosure();
    }
    if (mode === 'oauth') {
      return '<div class="gh-signin"><button class="btn gh-btn" type="button" data-action="gh-signin">' + GH_MARK + 'Sign in with GitHub</button>' +
        '<p class="muted small" style="margin-top:8px">You approve access on github.com. OpsAgents never sees your GitHub password.</p></div>' + tokenDisclosure();
    }
    return '';
  }

  // ---- Device flow modal ----
  var dev = null;

  function startSignIn() {
    if (state.busy['gh-signin']) return;
    withBusy('gh-signin', function () {
      return api('POST', '/github/oauth/start', {}).then(openDevice);
    }).catch(function (e) { toast(errorText(e), 'err'); });
  }

  function openDevice(res) {
    stopDevice();
    res = res || {};
    dev = {
      code: String(res.user_code || ''),
      uri: safeUrl(res.verification_uri) || 'https://github.com/login/device',
      expiresAt: Date.now() + (num(res.expires_in) || 900) * 1000,
      interval: Math.max(5, num(res.interval) || 5),
      phase: 'waiting', message: '', pollTimer: null, tickTimer: null
    };
    renderDevice();
    dev.tickTimer = setInterval(renderDeviceCountdown, 1000);
    schedulePoll();
  }

  function schedulePoll() {
    if (!dev || dev.phase !== 'waiting') return;
    dev.pollTimer = setTimeout(pollDevice, dev.interval * 1000);
  }

  function pollDevice() {
    if (!dev || dev.phase !== 'waiting') return;
    var mine = dev;
    api('POST', '/github/oauth/poll', {}).then(function (r) {
      if (dev !== mine) return;
      r = r || {};
      if (r.interval) mine.interval = Math.max(5, num(r.interval));
      if (r.state === 'connected') { finishDevice(r); return; }
      if (r.state === 'pending') {
        if (Date.now() > mine.expiresAt) failDevice('expired');
        else schedulePoll();
        return;
      }
      failDevice(r.state || 'error', r.detail || r.message || r.error);
    }, function (e) {
      if (dev !== mine) return;
      if (isOutage(e)) { schedulePoll(); return; } // server blip: keep waiting
      failDevice('error', errorText(e));
    });
  }

  function failDevice(phase, msg) {
    if (!dev) return;
    clearTimeout(dev.pollTimer);
    clearInterval(dev.tickTimer);
    dev.phase = phase;
    dev.message = msg || '';
    renderDevice();
  }

  function finishDevice(r) {
    stopDevice();
    api('GET', '/github/status').then(function (s) { state.githubStatus = s || {}; return state.githubStatus; }, function () { return state.githubStatus || {}; })
      .then(function (s) {
        toast('Signed in as ' + ((r && r.login) || s.login || 'your GitHub account'), 'ok');
        state.forceSlow = true;
        state.availRepos = null;
        if (state.route.name === 'repositories') refreshSoon(true);
        else window.location.hash = '#/repositories';
      });
  }

  function stopDevice() {
    if (dev) { clearTimeout(dev.pollTimer); clearInterval(dev.tickTimer); }
    dev = null;
    var m = $('device-modal');
    if (m) m.parentNode.removeChild(m);
  }

  function fmtCountdown(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  function renderDeviceCountdown() {
    if (!dev || dev.phase !== 'waiting') return;
    var left = dev.expiresAt - Date.now();
    if (left <= 0) { failDevice('expired'); return; }
    var el = $('device-countdown');
    if (el) el.textContent = fmtCountdown(left);
  }

  function renderDevice() {
    if (!dev) return;
    var m = $('device-modal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'device-modal';
      m.className = 'palette-wrap';
      document.body.appendChild(m);
    }
    var label = dev.uri.replace(/^https?:\/\//, '').replace(/\/$/, '');
    var body;
    if (dev.phase === 'waiting') {
      body = '<p class="muted">1. Copy this one-time code</p>' +
        '<div class="device-code-row"><span class="device-code" id="device-code">' + esc(dev.code) + '</span>' +
        '<button class="btn sm" type="button" data-action="device-copy">Copy</button></div>' +
        '<p class="muted">2. Open GitHub, paste the code and authorise OpsAgents.</p>' +
        '<button class="btn primary" type="button" data-action="device-open">' + ICON.external + 'Open ' + esc(label) + '</button>' +
        '<div class="device-wait"><span class="ci ci-amber">' + ICON.spinner + 'Waiting for you to authorise…</span>' +
        '<span class="muted small">Code expires in <b id="device-countdown">' + esc(fmtCountdown(dev.expiresAt - Date.now())) + '</b></span></div>';
    } else {
      var reason = {
        expired: 'The code expired before it was used.',
        denied: 'Access was denied on GitHub.',
        idle: 'This sign-in request is no longer active.'
      }[dev.phase] || 'Sign-in failed.';
      body = '<div class="error-box" style="margin-top:0">' + esc(reason) + (dev.message ? ' ' + esc(dev.message) : '') + '</div>' +
        '<div class="row" style="margin-top:14px"><button class="btn primary" type="button" data-action="gh-signin">Try again</button></div>';
    }
    m.innerHTML = '<div class="palette modal" role="dialog" aria-modal="true" aria-labelledby="device-title">' +
      '<div class="modal-head"><span class="row" style="gap:8px">' + GH_MARK + '<b id="device-title">Sign in with GitHub</b></span>' +
      '<button class="icon-btn" type="button" data-action="device-cancel" aria-label="Close">' + ICON.close + '</button></div>' +
      '<div class="modal-body">' + body + '</div>' +
      '<div class="modal-foot"><button class="btn ghost" type="button" data-action="device-cancel">' + (dev.phase === 'waiting' ? 'Cancel' : 'Close') + '</button></div></div>';
  }

  function copyDeviceCode() {
    if (!dev) return;
    var code = dev.code;
    var fallback = function () {
      var el = $('device-code');
      if (el && window.getSelection) {
        var r = document.createRange();
        r.selectNodeContents(el);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      }
      toast('Code selected — press Ctrl+C to copy', 'info');
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code).then(function () { toast('Code copied', 'ok'); }, fallback);
      } else fallback();
    } catch (e) { fallback(); }
  }

  function ghUseToken() {
    var inp = $('gh-token'), errEl = $('gh-token-error');
    var token = inp.value.trim();
    errEl.textContent = '';
    if (!token) { errEl.textContent = 'Paste a personal access token.'; inp.focus(); return; }
    withBusy('gh-token', function () {
      $('gh-token-btn').disabled = true;
      return api('POST', '/github/token', { token: token }).then(function (r) {
        inp.value = '';
        finishDevice(r || {});
      });
    }).catch(function (e) {
      var el = $('gh-token-error');
      if (el) el.textContent = errorText(e);
      toast(errorText(e), 'err');
    }).then(function () {
      var b = $('gh-token-btn');
      if (b) b.disabled = false;
    });
  }

  function ghSaveClient() {
    var inp = $('gh-client-id'), errEl = $('gh-client-error');
    var id = inp.value.trim();
    errEl.textContent = '';
    if (!id) { errEl.textContent = 'Paste the Client ID of your OAuth App.'; inp.focus(); return; }
    $('gh-client-btn').disabled = true;
    api('POST', '/github/oauth/client', { client_id: id }).then(function () {
      toast('OAuth App saved. You can sign in with GitHub now.', 'ok');
    }, function (e) {
      var el = $('gh-client-error');
      if (el) el.textContent = errorText(e);
      toast(errorText(e), 'err');
    }).then(function () {
      var b = $('gh-client-btn');
      if (b) b.disabled = false;
      refreshSoon(true);
    });
  }

  function followRepo(repo) {
    withBusy('repo-follow:' + repo, function () {
      return api('POST', '/github/repos', { repo: repo }).then(function () {
        arr(state.availRepos).forEach(function (r) { if (r.repo === repo) r.followed = true; });
        toast('Following ' + repo, 'ok');
      });
    }).catch(function (e) { toast(errorText(e), 'err'); }).then(function () { refreshSoon(true); });
  }

  // ---- Repositories ----
  PAGES.repositories = {
    title: 'Repositories',
    shell: function () {
      return '<section><div class="section-head"><div><h2>Your repositories</h2><div class="muted small">Repositories your GitHub account can read. Follow the ones OpsAgents should watch.</div></div>' +
          '<div class="search" id="repo-search-box" hidden>' + ICON.search + '<input id="repo-search" type="search" placeholder="Search repositories" aria-label="Search repositories" value="' + esc(state.repoSearch) + '"></div></div>' +
          '<div id="repo-picker">' + skeletonRows(3) + '</div></section>' +
        '<section id="following-section"><div class="section-head"><h2>Following</h2></div><div id="repos-list">' + skeletonRows(1) + '</div></section>' +
        '<div class="card" id="repo-manual"><div class="card-head"><div><h2>Add by name</h2><div class="sub">For a repository that is not in the list (e.g. from an organisation). Your GitHub sign-in must be able to read it.</div></div></div>' +
          '<form id="repo-form" class="form-grid two" autocomplete="off">' +
          '<label class="field"><span>Repository</span><input id="repo-add-input" type="text" placeholder="owner/name" spellcheck="false"></label>' +
          '<button class="btn" type="submit" id="repo-add-btn">Follow repository</button></form>' +
          '<div class="form-error" id="repo-form-error"></div></div>';
    },
    fetch: function () {
      return api('GET', '/github/status').then(function (s) { state.githubStatus = s || {}; return state.githubStatus; }, function () { return state.githubStatus || {}; })
        .then(function (s) {
          var signed = ghSignedIn(s);
          var jobs = [signed ? api('GET', '/github/repos') : Promise.resolve([])];
          if (signed && state.availRepos === null) {
            jobs.push(api('GET', '/github/available-repos').then(function (r) {
              state.availRepos = arr(r);
              state.availError = null;
            }, function (e) {
              state.availRepos = [];
              state.availError = e;
            }));
          }
          return Promise.all(jobs).then(function (r) { return { status: s, signed: signed, repos: arr(r[0]) }; });
        });
    },
    render: function (d) {
      var errEl = $('repo-form-error');
      if (errEl) errEl.textContent = state.repoFormError;
      $('following-section').hidden = !d.signed;
      $('repo-manual').hidden = !d.signed;
      if (!d.signed) {
        $('repo-search-box').hidden = true;
        region('repo-picker', emptyState({ glyph: 'plug', title: 'Sign in with GitHub first', text: 'Then pick the repositories OpsAgents should watch for commits, pull requests and CI.', action: { label: 'Sign in with GitHub', href: '#/settings' } }));
        return;
      }
      var followed = {};
      d.repos.forEach(function (r) { followed[r.repo] = true; });
      var avail = state.availRepos;
      $('repo-search-box').hidden = !(avail && avail.length > 5);
      if (avail === null) {
        region('repo-picker', skeletonRows(3));
      } else if (!avail.length) {
        region('repo-picker', state.availError
          ? '<div class="error-state">Couldn\'t list your repositories: ' + esc(errorText(state.availError)) + '</div>'
          : emptyState({ small: true, title: 'No repositories found', text: 'Your GitHub account has no repositories this sign-in can read. Use “Add by name” below.' }));
      } else {
        var q = state.repoSearch.trim().toLowerCase();
        var rows = avail.filter(function (r) {
          return !q || (String(r.repo) + ' ' + (r.description || '')).toLowerCase().indexOf(q) !== -1;
        }).sort(function (a, b) {
          var fa = followed[a.repo] || a.followed ? 1 : 0, fb = followed[b.repo] || b.followed ? 1 : 0;
          if (fa !== fb) return fb - fa;
          return String(b.pushed_at || '') < String(a.pushed_at || '') ? -1 : 1;
        });
        region('repo-picker', rows.length ? '<div class="feed">' + rows.map(function (r) {
          var isF = !!followed[r.repo];
          var right = isF
            ? pill('available', 'Following') + '<button class="btn sm danger" type="button" data-action="repo-remove" data-repo="' + esc(r.repo) + '"' + (state.busy['repo-remove:' + r.repo] ? ' disabled' : '') + '>Remove</button>'
            : '<button class="btn sm primary" type="button" data-action="repo-follow" data-repo="' + esc(r.repo) + '"' + (state.busy['repo-follow:' + r.repo] ? ' disabled' : '') + '>Follow</button>';
          return '<div class="feed-row"><span class="kind-icon">' + ICON.repo + '</span>' +
            '<div class="feed-main"><div class="feed-title">' + esc(r.repo) + '</div>' +
            '<div class="feed-sub"><span class="chip">' + (r.private ? 'private' : 'public') + '</span>' +
            (r.default_branch ? '<span class="chip mono">' + esc(r.default_branch) + '</span>' : '') +
            (r.pushed_at ? '<span title="' + esc(fullTime(r.pushed_at)) + '">pushed ' + esc(relTimeCoarse(r.pushed_at)) + '</span>' : '') +
            (r.description ? '<span class="desc-text">' + esc(r.description) + '</span>' : '') + '</div></div>' +
            '<div class="feed-right">' + right + '</div></div>';
        }).join('') + '</div>' : emptyState({ small: true, glyph: 'search', title: 'No repositories match “' + state.repoSearch.trim() + '”' }));
      }

      if (!d.repos.length) {
        region('repos-list', emptyState({ small: true, title: 'Not following any repositories yet', text: 'Pick some above. OpsAgents then watches their commits, pull requests and CI.' }));
        return;
      }
      region('repos-list', '<div class="table-wrap"><table class="table"><thead><tr>' +
        '<th>Repository</th><th>Health</th><th>Last commit</th><th class="num">Open PRs</th><th class="num">Failing today</th><th>Last sync</th><th></th>' +
        '</tr></thead><tbody>' + d.repos.map(function (r) {
          var head = r.head;
          var busy = !!state.busy['repo-remove:' + r.repo];
          return '<tr><td class="title-cell"><a href="#/activity" data-action="goto-activity" data-repo="' + esc(r.repo) + '">' + esc(r.repo) + '</a>' +
            '<div class="sub"><span class="chip">' + (r.private ? 'private' : 'public') + '</span>' +
            (r.default_branch ? '<span class="chip mono">' + esc(r.default_branch) + '</span>' : '') + '</div></td>' +
            '<td>' + ciBadge(head ? head.ci : 'none', { failLabel: 'Failing', noneLabel: 'No checks' }) + '</td>' +
            '<td class="muted">' + (head ? '<span class="sha">' + esc(head.short_sha || '') + '</span> ' + esc(firstLine(head.message).slice(0, 80)) : '—') + '</td>' +
            '<td class="num">' + esc(num(r.open_prs)) + '</td>' +
            '<td class="num">' + (num(r.failing_today) ? '<span style="color:var(--red);font-weight:600">' + esc(num(r.failing_today)) + '</span>' : '0') + '</td>' +
            '<td class="muted nowrap" title="' + esc(fullTime(r.last_sync)) + '">' + esc(r.last_sync ? relTimeCoarse(r.last_sync) : 'never') + '</td>' +
            '<td class="num"><button class="btn sm danger" type="button" data-action="repo-remove" data-repo="' + esc(r.repo) + '"' + (busy ? ' disabled' : '') + '>Remove</button></td></tr>';
        }).join('') + '</tbody></table></div>');
    }
  };

  // ---- Settings ----
  PAGES.settings = {
    title: 'Settings',
    shell: function () {
      return '<div class="settings-grid">' +
        '<div class="card span-all"><div class="card-head"><div><h2>GitHub</h2><div class="sub">OpsAgents watches your repositories for commits, pull requests and CI results.</div></div>' +
          '<div class="row" id="gh-actions"></div></div>' +
          '<div id="gh-status">' + skeletonRows(1) + '</div>' +
          '<div id="gh-forms"></div>' +
        '</div>' +
        '<div class="card"><h2>Automation</h2><div style="margin-top:12px" id="gh-auto"></div></div>' +
        '<div class="card"><h2>Notifications</h2><p class="muted" style="margin:6px 0 12px">Get a desktop notification when a critical or warning alert arrives while this tab is open.</p><div id="notify-box"></div></div>' +
        '<div class="card"><h2>Appearance</h2><p class="muted" style="margin:6px 0 12px">Dark is the default.</p><div id="theme-box"></div></div>' +
        '<div class="card"><div class="card-head" style="margin-bottom:8px"><h2>Models</h2><a href="#/agents" class="small">Agent registry →</a></div><div id="models-box">' + skeletonRows(2) + '</div></div>' +
        '<div class="card"><h2>System</h2><div style="margin-top:12px" id="system-box"></div></div>' +
        '</div>';
    },
    fetch: function () {
      return Promise.all([
        api('GET', '/github/status'),
        api('GET', '/agents').then(function (a) { state.agents = arr(a); return state.agents; }, function () { return arr(state.agents); })
      ]).then(function (r) { state.githubStatus = r[0] || {}; return { status: state.githubStatus, agents: r[1] }; });
    },
    render: function (d) {
      var s = d.status || {};
      var signed = ghSignedIn(s);
      var mode = signed ? 'signed' : (s.oauth_ready ? 'oauth' : 'setup');
      var forms = $('gh-forms');
      if (forms.__mode !== mode) { forms.innerHTML = ghFormsHTML(mode); forms.__mode = mode; }

      var st;
      if (signed) {
        var repos = ghRepoList(s);
        var rl = s.rate_limit;
        var via = s.auth === 'oauth' ? 'via browser sign-in' : (s.auth === 'token' ? 'via token' : '');
        st = '<div class="conn">' + avatar(s.login || '?', s.avatar_url, 'lg') +
          '<div class="grow"><div style="font-weight:600">' + esc(s.login || 'Signed in') + ' ' + pill('available', 'signed in') +
          (via ? ' <span class="muted small" style="font-weight:400">' + esc(via) + '</span>' : '') + '</div>' +
          '<div class="muted small">' + (repos.length ? 'Following ' + repos.map(esc).join(', ') : 'Not following any repositories yet — <a href="#/repositories">choose repositories →</a>') + '</div></div></div>' +
          '<div class="meta"><span>Last sync <b title="' + esc(fullTime(s.last_sync)) + '">' + esc(s.last_sync ? relTimeCoarse(s.last_sync) : 'never') + '</b></span>' +
          (hasVal(s.poll_seconds) ? '<span>Polling every <b>' + esc(s.poll_seconds) + 's</b></span>' : '') +
          (rl ? '<span>API rate limit <b>' + esc(fmtNum(rl.remaining)) + ' left</b>' + (rl.reset_at ? ', resets ' + esc(relTimeCoarse(rl.reset_at)) : '') + '</span>' : '') + '</div>';
      } else {
        st = '<div class="conn" style="margin-bottom:14px">' + avatar('?', null, 'lg') + '<div><div style="font-weight:600">Not signed in ' + pill('none', 'disconnected') + '</div>' +
          '<div class="muted small">' + (mode === 'oauth' ? 'Sign in with your GitHub account to choose repositories.' : 'Set up browser sign-in once, or paste a token.') + '</div></div></div>';
      }
      if (s.error) st += '<div class="error-box" style="margin-bottom:12px">' + esc(s.error) + '</div>';
      region('gh-status', st);
      region('gh-actions', signed
        ? '<a class="btn sm" href="#/repositories">Choose repositories</a>' +
          (ghRepoList(s).length ? '<button class="btn sm" type="button" data-action="gh-sync"' + (state.busy['gh-sync'] ? ' disabled' : '') + '>' + ICON.sync + 'Sync now</button>' : '') +
          '<button class="btn sm danger" type="button" data-action="gh-disconnect"' + (state.busy['gh-disconnect'] ? ' disabled' : '') + '>Disconnect</button>'
        : '');
      region('gh-auto', '<label class="switch"><input type="checkbox" id="auto-investigate"' + (s.auto_investigate ? ' checked' : '') + (signed ? '' : ' disabled') + '>' +
        '<span><b>Investigate failures automatically</b><br><span class="muted small">Start a Diagnostic / RCA run when CI fails on a watched branch.</span></span></label>');
      region('notify-box', notifyButtonHTML());
      region('theme-box', '<div class="seg" role="group" aria-label="Theme">' + ['dark', 'light', 'system'].map(function (t) {
        return '<button type="button" data-action="theme-set" data-theme-pref="' + t + '" class="' + (state.themePref === t ? 'on' : '') + '">' + t.charAt(0).toUpperCase() + t.slice(1) + '</button>';
      }).join('') + '</div>');
      region('models-box', d.agents.length ? '<div class="checklist">' + d.agents.map(function (a) {
        return '<div><span>' + esc(a.title || agentTitle(a.name)) + '</span><span class="row" style="gap:8px">' +
          (a.model ? '<code>' + esc(a.model) + '</code>' : '<span class="faint small">rule-based</span>') + pill(a.status) + '</span></div>';
      }).join('') + '</div>' : emptyState({ small: true, title: 'No agents registered' }));
      var o = state.overview || {};
      var sys = o.system || {};
      var check = function (label, ok, okText, badText) {
        var known = ok === true || ok === false;
        return '<div><span>' + esc(label) + '</span>' + (known ? pill(ok ? 'ok' : 'failed', ok ? okText : badText) : pill('unknown')) + '</div>';
      };
      region('system-box', '<div class="checklist">' +
        check('Database', sys.database, 'ok', 'down') +
        check('Policy engine', sys.policy_engine, 'ok', 'down') +
        check('Audit chain', hasVal(sys.audit_intact) ? sys.audit_intact : o.audit_intact, 'intact', 'broken') +
        check('LLM' + (o.llm && o.llm.label ? ' (' + o.llm.label + ')' : ''), o.llm ? o.llm.ok !== false : undefined, 'reachable', 'unavailable') +
        check('Server', !state.offline, 'reachable', 'unreachable') +
        (sys.tracing ? '<div><span>LangSmith tracing' + (sys.tracing.enabled ? ' (project ' + esc(sys.tracing.project) + ')' : '') + '</span>' +
          (sys.tracing.enabled ? '<a href="https://smith.langchain.com" target="_blank" rel="noopener">' + pill('ok', 'on') + '</a>'
                               : pill('unknown', 'off')) + '</div>' : '') +
        '</div>');
    }
  };

  // ---- Analytics ----
  var ANALYTICS_TABS = { summary: 'Summary', quality: 'Quality', time: 'Time', agents: 'Agents' };

  PAGES.analytics = {
    title: 'Analytics',
    shell: function (params) {
      var n = { summary: 10, quality: 5, time: 5, agents: 4 }[params.sub] || 6;
      return '<div id="toolbar">' + toolbarHTML('analytics') + '</div><div id="analytics-body">' + skeletonCards(n, params.sub === 'time' ? [0] : []) + '</div>';
    },
    fetch: function () {
      var f = state.filters.analytics;
      ensureAgents().catch(function () {});
      return api('GET', '/analytics' + qs({ days: f.days || 30, repo: f.repo }));
    },
    render: function (d, params) {
      region('toolbar', toolbarHTML('analytics'));
      var html;
      try {
        html = ({ summary: analyticsSummary, quality: analyticsQuality, time: analyticsTime, agents: analyticsAgents }[params.sub] || analyticsSummary)(d || {});
      } catch (e) {
        if (window.console) console.error(e);
        html = '<div class="error-state">Couldn\'t draw these charts: ' + esc(e.message) + '</div>';
      }
      region('analytics-body', html);
    }
  };

  function metric(title, desc, body, wide) {
    return '<div class="card metric' + (wide ? ' wide' : '') + '"><div class="m-title">' + esc(title) + '</div>' +
      (desc ? '<div class="m-desc">' + esc(desc) + '</div>' : '') + '<div class="m-body">' + body + '</div></div>';
  }
  function bigNum(value, label, opts) {
    opts = opts || {};
    return '<div><div class="big' + (opts.md ? ' md' : '') + '"' + (opts.color ? ' style="color:var(--' + opts.color + ')"' : '') + '>' + value + '</div>' +
      '<div class="lbl">' + (opts.dot ? '<span class="dot dot-' + opts.dot + '"></span>' : '') + esc(label) + '</div></div>';
  }
  function noData(text) { return '<div class="empty-state dotted sm" style="border:none;padding:18px"><div class="es-text">' + esc(text || 'No data in this range') + '</div></div>'; }
  function selAgent() { return state.filters.analytics.agent || ''; }

  function analyticsSummary(d) {
    var runs = d.runs || {}, props = d.proposals || {}, dec = d.decisions || {}, rev = d.reviews || {}, ci = d.ci || {};
    var cards = [];
    cards.push(metric('Active repositories', 'Repositories with at least one run or CI event in this range.',
      '<div class="nums">' + bigNum(esc(fmtNum(num(d.repos_active))), 'repositories') + '</div>'));
    cards.push(metric('Runs', 'Triggers the orchestrator handled.',
      '<div class="nums">' + bigNum(esc(fmtNum(num(runs.total))), 'Total') + bigNum(esc(fmtNum(num(runs.completed))), 'Completed', { dot: 'green' }) +
      bigNum(esc(fmtNum(num(runs.failed))), 'Failed', { dot: 'red' }) + '</div>'));
    var byAgent = props.by_agent || {};
    var agentNote = selAgent() ? '<div class="lbl">' + esc(agentTitle(selAgent())) + ': ' + esc(fmtNum(num(byAgent[selAgent()]))) + ' proposals</div>' : '';
    cards.push(metric('Proposals', 'Actions agents proposed, and what the Guardrail did with them.',
      '<div class="m-split">' + donut([
        { label: 'Auto-approved', value: props.auto_approved, cls: 'green' },
        { label: 'Held for humans', value: props.held, cls: 'amber' },
        { label: 'Rejected', value: props.rejected, cls: 'red' }
      ], { size: 92, thickness: 12, center: fmtNum(num(props.total)), sub: 'total' }) +
      '<div class="nums" style="gap:16px">' + bigNum(esc(fmtNum(num(props.auto_approved))), 'Auto-approved', { md: true, dot: 'green' }) +
      bigNum(esc(fmtNum(num(props.held))), 'Held', { md: true, dot: 'amber' }) + bigNum(esc(fmtNum(num(props.rejected))), 'Rejected', { md: true, dot: 'red' }) + '</div></div>' + agentNote));
    cards.push(metric('Pull requests reviewed', 'PRs the Code Reviewer commented on.',
      '<div class="nums">' + bigNum(esc(fmtNum(num(rev.prs))), 'PRs reviewed') + bigNum(esc(rev.avg_findings === null || rev.avg_findings === undefined ? '—' : fmtNum(rev.avg_findings)), 'Avg findings per PR') + '</div>'));
    cards.push(metric('Median time to decision', 'From proposal to approve/reject.',
      '<div class="nums">' + bigNum(esc(fmtSeconds(dec.median_seconds_auto)), 'Auto (Guardrail)', { dot: 'green' }) + bigNum(esc(fmtSeconds(dec.median_seconds_human)), 'Human', { dot: 'amber' }) + '</div>'));
    var ciDays = arr(ci.by_day);
    var rates = ciDays.map(function (x) { var t = num(x.success) + num(x.failure); return t ? num(x.success) / t : null; });
    cards.push(metric('CI pass rate', 'Share of CI runs that passed.',
      '<div class="nums" style="justify-content:space-between;width:100%">' + bigNum(esc(fmtPct(ci.pass_rate)), 'passed', { color: ci.pass_rate !== null && ci.pass_rate < 0.8 ? 'amber' : '' }) +
      '<div style="flex:1;max-width:170px">' + sparkline(rates, ci.pass_rate !== null && ci.pass_rate < 0.8 ? 'amber' : 'green') + '</div></div>'));
    cards.push(metric('Mean time to recovery', 'From the first failing CI run to green again.',
      '<div class="nums">' + bigNum(esc(fmtSeconds(ci.mttr_seconds)), 'MTTR') + '</div>'));
    var saved = num(d.time_saved_minutes);
    cards.push(metric('Reviewer time saved', 'Estimate: findings and RCAs a human did not have to produce.',
      '<div class="nums">' + bigNum(esc(saved >= 60 ? Math.floor(saved / 60) + 'h ' + (saved % 60) + 'm' : saved + 'm'), 'estimated') + '</div>'));
    var sev = rev.by_severity || {};
    var order = ['critical', 'high', 'medium', 'low', 'info'];
    var axes = order.filter(function (k) { return hasVal(sev[k]); }).map(function (k) { return { label: k, value: sev[k] }; });
    Object.keys(sev).forEach(function (k) { if (order.indexOf(k) === -1) axes.push({ label: k, value: sev[k] }); });
    cards.push(metric('Findings by severity', 'Code review findings in this range.',
      axes.length >= 3 ? radar(axes, { title: 'Findings by severity' }) : (axes.length ? hbars(axes.map(function (a) { return { label: a.label, value: a.value }; })) : noData())));
    var causes = arr(d.root_causes).slice(0, 5);
    cards.push(metric('Root causes found', 'Top causes the Diagnostic / RCA agent identified.',
      causes.length ? hbars(causes.map(function (c) { return { label: c.cause, value: c.count }; })) : noData('No root causes identified yet')));
    return '<div class="metrics">' + cards.join('') + '</div>';
  }

  function analyticsQuality(d) {
    var rev = d.reviews || {}, dec = d.decisions || {};
    var cats = rev.by_category || {};
    var catOrder = ['leakage', 'split', 'reproducibility', 'evaluation', 'bug', 'security', 'injection', 'style'];
    var catItems = catOrder.filter(function (k) { return hasVal(cats[k]); }).concat(Object.keys(cats).filter(function (k) { return catOrder.indexOf(k) === -1; }))
      .map(function (k) { return { label: k, value: cats[k], cls: /security|injection|leakage/.test(k) ? 'red' : (k === 'style' ? 'gray' : 'accent') }; });
    var rules = Object.keys(dec.by_rule || {}).map(function (k) { return { label: k, value: dec.by_rule[k] }; }).sort(function (a, b) { return num(b.value) - num(a.value); });
    var agents = arr(d.agents);
    if (selAgent()) agents = agents.filter(function (a) { return a.name === selAgent(); });
    var sev = rev.by_severity || {};
    var cards = [];
    cards.push(metric('Findings by category', 'What the Code Reviewer flags, across ML-specific and general categories.',
      catItems.length ? hbars(catItems) : noData(), true));
    cards.push(metric('Findings by severity', 'Share of findings per severity.',
      Object.keys(sev).length ? '<div class="m-split">' + donut(Object.keys(sev).map(function (k) {
        return { label: k, value: sev[k], cls: { critical: 'red', high: 'red', medium: 'amber', low: 'blue', info: 'gray' }[k] || 'gray' };
      }), { center: fmtNum(Object.keys(sev).reduce(function (s, k) { return s + num(sev[k]); }, 0)), sub: 'findings' }) +
      legend(Object.keys(sev).map(function (k) { return { label: k + ' ' + fmtNum(num(sev[k])), cls: { critical: 'red', high: 'red', medium: 'amber', low: 'blue', info: 'gray' }[k] || 'gray' }; })) + '</div>' : noData()));
    cards.push(metric('Guardrail decisions by rule', 'Which policy rules fired most often.',
      rules.length ? hbars(rules, { cls: 'amber' }) : noData()));
    cards.push(metric('LLM escalations', 'Times a rule-based Guardrail decision was escalated to the LLM for a second opinion.',
      '<div class="nums">' + bigNum(esc(fmtNum(num(dec.llm_escalations))), 'escalations') + '</div>'));
    cards.push(metric('Fallbacks per agent', 'How often each agent answered with its LLM vs. fell back to rules / static analysis.',
      agents.length ? legend([{ label: 'LLM', cls: 'accent' }, { label: 'Rules / static fallback', cls: 'gray' }]) +
        '<div class="hbars">' + agents.map(function (a) {
          return '<div class="hbar"><span class="name">' + esc(agentTitle(a.name)) + '</span>' +
            splitBar([{ label: 'LLM', value: a.llm, cls: 'accent' }, { label: 'Fallback', value: a.fallback, cls: 'gray' }]).replace('split-bar', 'track') +
            '<span class="v">' + esc(fmtNum(num(a.fallback))) + ' fb</span></div>';
        }).join('') + '</div>' : noData(), true));
    return '<div class="metrics">' + cards.join('') + '</div>';
  }

  function analyticsTime(d) {
    var runs = d.runs || {}, ci = d.ci || {}, dec = d.decisions || {};
    var days = arr(runs.by_day);
    var cards = [];
    var series = [
      { key: 'completed', label: 'Completed', cls: 'green' },
      { key: 'failed', label: 'Failed', cls: 'red' },
      { key: 'waiting', label: 'Waiting', cls: 'amber' },
      { key: 'other', label: 'Other', cls: 'gray' }
    ];
    cards.push(metric('Runs per day', 'Stacked by final status.',
      days.length ? legend(series) + columnChart(days.map(function (x) { return { label: shortDate(x.date), short: shortDate(x.date), values: x }; }), series, { title: 'Runs per day' }) : noData(), true));
    var routes = arr(d.route_durations).slice().sort(function (a, b) { return num(b.median_seconds) - num(a.median_seconds); });
    cards.push(metric('Median run duration per route', 'From trigger to the last agent step.',
      routes.length ? hbars(routes.map(function (r) { return { label: r.route, value: r.median_seconds, display: fmtSeconds(r.median_seconds) }; }), { cls: 'blue' }) : noData()));
    var waitPts = days.map(function (x) { return { label: shortDate(x.date), short: shortDate(x.date), value: hasVal(x.median_wait_seconds) ? num(x.median_wait_seconds) / 60 : null }; });
    cards.push(metric('Time waiting for approval', 'Daily median minutes a held proposal waited for a human. Overall median: ' + fmtSeconds(dec.median_seconds_human) + '.',
      lineChart(waitPts, { cls: 'amber', fmt: function (v) { return Math.round(v) + 'm'; }, title: 'Minutes waiting for approval' }) || noData('No held proposals in this range')));
    var ciDays = arr(ci.by_day);
    var ciSeries = [{ key: 'success', label: 'Passed', cls: 'green' }, { key: 'failure', label: 'Failed', cls: 'red' }];
    cards.push(metric('CI runs per day', 'GitHub Actions / check runs on watched repositories.',
      ciDays.length ? legend(ciSeries) + columnChart(ciDays.map(function (x) { return { label: shortDate(x.date), short: shortDate(x.date), values: x }; }), ciSeries, { title: 'CI runs per day' }) : noData()));
    var durPts = ciDays.map(function (x) { return { label: shortDate(x.date), short: shortDate(x.date), value: hasVal(x.median_seconds) ? num(x.median_seconds) / 60 : null }; });
    cards.push(metric('CI duration trend', 'Daily median CI run duration, in minutes.',
      lineChart(durPts, { cls: 'blue', fmt: function (v) { return (Math.round(v * 10) / 10) + 'm'; }, title: 'CI duration' }) || noData('No CI duration data in this range')));
    return '<div class="metrics">' + cards.join('') + '</div>';
  }

  function analyticsAgents(d) {
    var agents = arr(d.agents);
    if (selAgent()) agents = agents.filter(function (a) { return a.name === selAgent(); });
    if (!agents.length) return emptyState({ glyph: 'chart', title: 'No agent activity in this range', text: 'Start a run from Home to generate data.' });
    var series = [
      { key: 'done', label: 'Success', cls: 'green' }, { key: 'degraded', label: 'Degraded', cls: 'amber' },
      { key: 'failed', label: 'Failed', cls: 'red' }, { key: 'unavailable', label: 'Unavailable', cls: 'violet' }, { key: 'skipped', label: 'Skipped', cls: 'gray' }
    ];
    return '<div class="metrics">' + agents.map(function (a) {
      var segs = series.map(function (s) { return { label: s.label, value: a[s.key], cls: s.cls }; });
      return metric(agentTitle(a.name), a.model ? 'Model: ' + a.model : 'Rule-based (no LLM)',
        '<div class="nums">' + bigNum(esc(fmtNum(num(a.runs))), 'Runs') + bigNum(esc(fmtNum(num(a.proposals))), 'Proposals') +
        bigNum(esc(fmtPct(a.approval_rate)), 'Approval rate') + '</div>' +
        splitBar(segs) +
        '<div class="legend">' + segs.filter(function (s) { return num(s.value); }).map(function (s) {
          return '<span><i class="bg-' + s.cls + '"></i>' + esc(s.label) + ' ' + esc(fmtNum(num(s.value))) + '</span>';
        }).join('') + '</div>' +
        '<div class="lbl">LLM answers ' + esc(fmtNum(num(a.llm))) + ' · fallbacks ' + esc(fmtNum(num(a.fallback))) + '</div>');
    }).join('') + '</div>';
  }

  // ===========================================================================
  // 6. Chrome: sidebar, header, bell, workspace menu, theme, command palette
  // ===========================================================================

  function setBadge(id, n) {
    var el = $(id);
    if (!el) return;
    n = num(n);
    el.hidden = n <= 0;
    el.textContent = n > 99 ? '99+' : String(n);
    var item = el.closest('.nav-item');
    if (item) item.classList.toggle('has-badge', n > 0);
  }

  function unreadCount() {
    if (state.alerts) return arr(state.alerts).filter(function (a) { return !a.read; }).length;
    return state.overview ? num(state.overview.alerts_unread) : 0;
  }

  function renderChrome() {
    var o = state.overview;
    $('offline-banner').hidden = !state.offline;
    if (o) {
      setBadge('badge-approvals', o.pending_approvals);
      setBadge('badge-triage', o.runs && o.runs.needs_triage);
    }
    var unread = unreadCount();
    setBadge('badge-alerts', unread);
    var bc = $('bell-count');
    bc.hidden = unread <= 0;
    bc.textContent = unread > 9 ? '9+' : String(unread);
    $('bell-btn').setAttribute('aria-label', 'Alerts' + (unread ? ' (' + unread + ' unread)' : ''));

    // GitHub chip
    var s = state.githubStatus || {};
    var g = (o && o.github) || {};
    // "connected" here means: signed in AND following at least one repository
    var connected = state.githubStatus ? (ghSignedIn(s) && ghRepoList(s).length > 0) : !!g.connected;
    var chip = $('gh-chip');
    if (connected) {
      var repos = arr(s.repos).length ? arr(s.repos) : (s.repo || g.repo ? [s.repo || g.repo] : []);
      var last = s.last_sync || g.last_sync;
      var txt = (repos[0] || 'GitHub') + (repos.length > 1 ? ' +' + (repos.length - 1) : '') + (last ? ' · synced ' + relTimeCoarse(last) : '');
      setHTML(chip, '<span class="dot ' + (s.error ? 'dot-amber' : 'dot-green') + '"></span><span class="gh-chip-text">' + esc(txt) + '</span>');
      chip.classList.remove('off');
      chip.setAttribute('href', '#/activity');
      chip.title = s.error ? 'GitHub: ' + s.error : 'Connected to GitHub';
    } else if (ghSignedIn(s)) {
      // signed in but not following any repository yet
      setHTML(chip, '<span class="dot dot-amber"></span><span class="gh-chip-text">Choose repositories</span>');
      chip.classList.add('off');
      chip.setAttribute('href', '#/repositories');
      chip.title = 'Signed in to GitHub as ' + (s.login || '?') + ' — pick the repositories to watch';
    } else {
      setHTML(chip, GH_MARK + '<span class="gh-chip-text">Sign in with GitHub</span>');
      chip.classList.add('off');
      chip.setAttribute('href', '#/settings');
      chip.title = 'GitHub is not connected — sign in from Settings';
    }

    // Workspace row
    setHTML($('ws-avatar'), avatar(s.login || 'OpsAgents', s.avatar_url, 'md'));
    $('ws-name').textContent = s.login || 'OpsAgents';

    // Recents (last 3 runs)
    var rec = state.recents;
    if (rec !== null) {
      region('recents-list', rec.length ? rec.slice(0, 3).map(function (r) {
        return '<a class="recent-item" href="#/runs/' + esc(encodeURIComponent(r.id)) + '" title="' + esc(r.title || '') + '">' +
          '<span class="dot dot-' + toneOf(r.status) + '"></span><span class="t">' + esc(r.title || 'Untitled run') + '</span>' +
          '<span class="w">' + esc(relTimeCoarse(r.created_at).replace(' ago', '')) + '</span></a>';
      }).join('') : '<div class="recents-empty">No runs yet<br><button class="btn sm" type="button" data-action="new-run">Start a run</button></div>');
    }

    if (pop.kind === 'bell') renderPopover();
  }

  function renderUserRow() {
    setHTML($('user-avatar'), avatar(state.actingAs || '?', null, 'md'));
    var meta = $('user-meta');
    if (state.editingName) {
      meta.innerHTML = '<input class="user-name-input" id="acting-as" type="text" maxlength="64" spellcheck="false" aria-label="Acting as">' +
        '<div class="user-role">Press Enter to save</div>';
      var inp = $('acting-as');
      inp.value = state.actingAs;
      inp.focus();
      inp.select();
    } else {
      meta.innerHTML = '<button class="user-name" type="button" data-action="edit-name" title="Acting as — click to change">' + esc(state.actingAs || 'Set your name') + '</button>' +
        '<div class="user-role">Admin · acting as</div>';
    }
    meta.__html = null;
  }

  function saveName(value) {
    state.editingName = false;
    var v = String(value || '').trim();
    if (v && v !== state.actingAs) {
      state.actingAs = v;
      lsSet(LS.acting, v);
      toast('Now acting as ' + v, 'info');
    }
    renderUserRow();
    renderCurrent();
  }

  POPOVERS.bell = function () {
    var list = arr(state.alerts).slice().sort(function (a, b) {
      var ta = parseTime(a.created_at), tb = parseTime(b.created_at);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
    }).slice(0, 5);
    var unread = unreadCount();
    var body = list.length ? list.map(function (a) {
      return '<button class="pop-item bell-item' + (a.read ? '' : ' unread') + '" type="button" data-action="alert-open" data-id="' + esc(a.id) + '">' +
        sevIcon(a.severity, true) + '<div><div class="t">' + esc(a.title || 'Alert') + '</div><div class="m">' + esc(relTimeCoarse(a.created_at)) +
        (a.message ? ' · ' + esc(a.message) : '') + '</div></div></button>';
    }).join('') : '<div class="pop-empty">You\'re all caught up.</div>';
    return '<div class="pop-head" style="min-width:300px"><span>Alerts' + (unread ? ' <span class="faint">(' + unread + ' unread)</span>' : '') + '</span>' +
      (unread ? '<button class="btn link sm" type="button" data-action="alerts-read-all">Mark all read</button>' : '') + '</div>' +
      '<div class="pop-list">' + body + '</div><a class="pop-foot" href="#/alerts">View all alerts →</a>';
  };

  POPOVERS.workspace = function () {
    var s = state.githubStatus || {};
    var repos = arr(s.repos).length ? arr(s.repos) : (s.repo ? [s.repo] : []);
    return '<div class="pop-head" style="min-width:240px"><span>' + (ghSignedIn(s) ? 'GitHub · ' + esc(s.login || 'signed in') : 'GitHub not connected') + '</span></div>' +
      '<div class="pop-list">' +
      (repos.length ? repos.map(function (r) {
        return '<button class="pop-item" type="button" data-action="goto-activity" data-repo="' + esc(r) + '">' + ICON.repo + esc(r) + '</button>';
      }).join('') + '<div class="pop-sep"></div>' : '') +
      '<a class="pop-item" href="#/repositories">' + ICON.plusCircle + 'Add repositories</a>' +
      '<a class="pop-item" href="#/settings">' + ICON.gear + 'Settings</a>' +
      '<button class="pop-item" type="button" data-action="theme-toggle">' + (currentTheme() === 'dark' ? ICON.sun + 'Light theme' : ICON.moon + 'Dark theme') + '</button>' +
      '</div>';
  };

  // ---- Theme -------------------------------------------------------------------

  var mqLight = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  function currentTheme() { return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
  function applyTheme(pref, save) {
    state.themePref = pref;
    var t = pref === 'system' ? (mqLight && mqLight.matches ? 'light' : 'dark') : pref;
    document.documentElement.setAttribute('data-theme', t);
    if (save) lsSet(LS.theme, pref);
    var btn = $('theme-btn');
    btn.innerHTML = t === 'dark' ? ICON.sun : ICON.moon;
    btn.title = t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
  }
  if (mqLight && mqLight.addEventListener) {
    mqLight.addEventListener('change', function () { if (state.themePref === 'system') applyTheme('system'); });
  }

  // ---- Sidebar -------------------------------------------------------------------

  function setCollapsed(on) {
    document.body.classList.toggle('sb-collapsed', on);
    lsSet(LS.collapsed, on ? '1' : '0');
    $('collapse-btn').title = on ? 'Expand sidebar' : 'Collapse sidebar';
    closePopover();
  }

  function setAnalyticsOpen(on, save) {
    var g = $('analytics-group');
    g.classList.toggle('open', on);
    g.querySelector('[data-action="toggle-analytics"]').setAttribute('aria-expanded', on ? 'true' : 'false');
    if (save) lsSet(LS.analytics, on ? '1' : '0');
  }

  function closeDrawer() {
    document.body.classList.remove('sb-open');
    $('scrim').hidden = true;
  }

  function setPageTitle(parts) {
    // parts: [[label, href?], ...]
    var html = parts.map(function (p, i) {
      var last = i === parts.length - 1;
      var label = esc(p[0]);
      if (!last) return (p[1] ? '<a class="crumb-dim" href="' + esc(p[1]) + '">' + label + '</a>' : '<span class="crumb-dim">' + label + '</span>') + '<span class="crumb-sep">/</span>';
      return label;
    }).join('');
    setHTML($('page-title'), html);
    document.title = parts[parts.length - 1][0] + ' · OpsAgents';
  }

  // ---- Command palette ---------------------------------------------------------

  var PALETTE_PAGES = [
    ['Home', '#/home'], ['Activity', '#/activity'], ['Runs', '#/runs'], ['Approvals', '#/approvals'], ['Triage', '#/triage'],
    ['Alerts', '#/alerts'], ['Analytics · Summary', '#/analytics/summary'], ['Analytics · Quality', '#/analytics/quality'],
    ['Analytics · Time', '#/analytics/time'], ['Analytics · Agents', '#/analytics/agents'], ['Agent registry', '#/agents'],
    ['Audit log', '#/audit'], ['Repositories', '#/repositories'], ['Settings', '#/settings']
  ];

  function openPalette() {
    closePopover();
    var p = state.palette;
    p.open = true;
    p.index = 0;
    $('palette').hidden = false;
    var inp = $('palette-input');
    inp.value = '';
    inp.focus();
    renderPalette();
    if (!p.data || Date.now() - p.loadedAt > 15000) {
      Promise.all([
        api('GET', '/runs?limit=50').catch(function () { return []; }),
        api('GET', '/github/repos').catch(function () { return []; }),
        api('GET', '/agents').catch(function () { return arr(state.agents); })
      ]).then(function (r) {
        p.data = { runs: arr(r[0]), repos: arr(r[1]), agents: arr(r[2]) };
        p.loadedAt = Date.now();
        if (p.open) renderPalette();
      });
    }
  }

  function closePalette() {
    state.palette.open = false;
    $('palette').hidden = true;
  }

  // Subsequence fuzzy match. Returns null or {score, hits:[indices]}.
  function fuzzy(q, text) {
    if (!q) return { score: 0, hits: [] };
    var t = text.toLowerCase(), hits = [], ti = 0, score = 0, prev = -2;
    for (var qi = 0; qi < q.length; qi++) {
      var c = q[qi];
      if (c === ' ') continue;
      var found = t.indexOf(c, ti);
      if (found === -1) return null;
      score += 10;
      if (found === prev + 1) score += 8;
      if (found === 0 || /[\s/·_-]/.test(t[found - 1])) score += 6;
      score -= Math.min(found - ti, 6);
      hits.push(found);
      prev = found;
      ti = found + 1;
    }
    if (t.indexOf(q) !== -1) score += 25;
    return { score: score, hits: hits };
  }

  function highlight(text, hits) {
    var set = {};
    hits.forEach(function (i) { set[i] = true; });
    var out = '';
    for (var i = 0; i < text.length; i++) out += set[i] ? '<mark>' + esc(text[i]) + '</mark>' : esc(text[i]);
    return out;
  }

  function paletteItems(q) {
    var data = state.palette.data || { runs: [], repos: [], agents: [] };
    var groups = [
      { name: 'Pages', limit: q ? 6 : 14, items: PALETTE_PAGES.map(function (p) { return { label: p[0], icon: ICON.page, href: p[1] }; }) },
      { name: 'Runs', limit: q ? 8 : 5, items: data.runs.map(function (r) { return { label: r.title || 'Untitled run', hint: humanize(r.status), icon: ICON.run, href: '#/runs/' + encodeURIComponent(r.id) }; }) },
      { name: 'Repositories', limit: 6, items: data.repos.map(function (r) { return { label: r.repo, hint: 'activity', icon: ICON.repo, repo: r.repo }; }) },
      { name: 'Agents', limit: 6, items: data.agents.map(function (a) { return { label: a.title || agentTitle(a.name), hint: a.name, icon: ICON.cpu, href: '#/agents' }; }) }
    ];
    var out = [];
    groups.forEach(function (g) {
      var matched = [];
      g.items.forEach(function (it) {
        var m = fuzzy(q, it.label.toLowerCase());
        if (!m && it.hint) { var m2 = fuzzy(q, String(it.hint).toLowerCase()); if (m2) m = { score: m2.score - 5, hits: [] }; }
        if (m) matched.push({ it: it, m: m });
      });
      if (q) matched.sort(function (a, b) { return b.m.score - a.m.score; });
      matched.slice(0, g.limit).forEach(function (x, i) { out.push({ group: i === 0 ? g.name : null, it: x.it, hits: x.m.hits }); });
    });
    return out;
  }

  function renderPalette() {
    var p = state.palette;
    var q = $('palette-input').value.trim().toLowerCase();
    p.items = paletteItems(q);
    if (p.index >= p.items.length) p.index = Math.max(0, p.items.length - 1);
    var list = $('palette-list');
    list.innerHTML = p.items.length ? p.items.map(function (x, i) {
      return (x.group ? '<div class="palette-group">' + esc(x.group) + '</div>' : '') +
        '<div class="palette-item' + (i === p.index ? ' kb' : '') + '" role="option" data-pal="' + i + '">' + x.it.icon +
        '<span class="t">' + highlight(x.it.label, x.hits) + '</span>' + (x.it.hint ? '<span class="h">' + esc(x.it.hint) + '</span>' : '') + '</div>';
    }).join('') : '<div class="pop-empty">No matches for “' + esc(q) + '”</div>';
    var kb = list.querySelector('.kb');
    if (kb && kb.scrollIntoView) kb.scrollIntoView({ block: 'nearest' });
  }

  function activatePalette(i) {
    var x = state.palette.items[i];
    if (!x) return;
    closePalette();
    if (x.it.repo) gotoActivity({ repo: x.it.repo });
    else if (x.it.href) window.location.hash = x.it.href;
  }

  // ===========================================================================
  // 7. Routing, polling, actions, events
  // ===========================================================================

  function parseHash() {
    var h = (window.location.hash || '').replace(/^#\/?/, '');
    var parts = h.split('/').filter(Boolean).map(function (p) {
      try { return decodeURIComponent(p); } catch (e) { return p; }
    });
    var name = parts[0] || 'home';
    if (name === 'runs' && parts[1]) return { name: 'run', params: { id: parts[1] }, nav: 'runs' };
    if (name === 'analytics') {
      var sub = ANALYTICS_TABS[parts[1]] ? parts[1] : 'summary';
      return { name: 'analytics', params: { sub: sub }, nav: 'analytics/' + sub, fix: parts[1] !== sub ? '#/analytics/' + sub : null };
    }
    if (!PAGES[name] || name === 'run') return { name: 'home', params: {}, nav: 'home', fix: parts[0] ? '#/home' : null };
    return { name: name, params: {}, nav: name };
  }

  function onRoute() {
    var r = parseHash();
    if (r.fix) { window.location.replace(r.fix); return; }
    closePopover();
    closePalette();
    closeDrawer();
    state.route = r;
    state.token++;
    state.page = null;
    state.pageError = null;
    var page = PAGES[r.name];
    if (r.name === 'repositories') { state.availRepos = null; state.availError = null; }
    $('page').innerHTML = '<div id="page-error"></div>' + page.shell(r.params);
    Array.prototype.forEach.call(document.querySelectorAll('[data-nav]'), function (a) {
      var n = a.getAttribute('data-nav');
      a.classList.toggle('active', n === r.nav || (n === 'analytics' && r.name === 'analytics'));
    });
    if (r.name === 'analytics') {
      setAnalyticsOpen(true);
      setPageTitle([['Analytics', '#/analytics/summary'], [ANALYTICS_TABS[r.params.sub]]]);
    } else {
      setPageTitle([[page.title]]);
    }
    window.scrollTo(0, 0);
    renderHomeOverview();
    tick();
  }

  function renderCurrent() {
    var page = PAGES[state.route.name];
    var errEl = $('page-error');
    if (state.page !== null && state.page !== undefined) {
      try {
        page.render(state.page, state.route.params);
      } catch (e) {
        if (window.console) console.error(e);
        setHTML(errEl, '<div class="error-state">Something went wrong drawing this page: ' + esc(e.message) + '</div>');
        return;
      }
      setHTML(errEl, '');
    } else if (state.pageError) {
      var e2 = state.pageError;
      var msg;
      if (e2.status === 404 && state.route.name === 'run') msg = 'Run not found. It may have been removed — go back to <a href="#/runs">Runs</a>.';
      else if (isOutage(e2)) msg = "Can't reach the server. We'll keep trying.";
      else msg = 'Couldn\'t load this page: ' + esc(e2.message);
      setHTML(errEl, '<div class="error-state">' + msg + '</div>');
      Array.prototype.forEach.call(document.querySelectorAll('#page .skel'), function (n) { n.remove(); });
    }
  }

  function processAlerts(list) {
    list = arr(list);
    state.alerts = list;
    if (state.alertsSeen === null) {
      state.alertsSeen = {};
      list.forEach(function (a) { state.alertsSeen[a.id] = true; });
      return;
    }
    list.forEach(function (a) {
      if (state.alertsSeen[a.id]) return;
      state.alertsSeen[a.id] = true;
      if (!a.read && (a.severity === 'critical' || a.severity === 'warning')) notifyAlert(a);
    });
  }

  function notifyAlert(a) {
    var l = alertLink(a.link);
    toast((a.title || 'New alert') + (a.message ? ' — ' + a.message : ''), a.severity === 'critical' ? 'err' : 'warn', { link: l || { href: '#/alerts', external: false } });
    try {
      if (notifySupported() && window.Notification.permission === 'granted') {
        var n = new window.Notification('OpsAgents · ' + (a.title || 'Alert'), { body: a.message || '', tag: 'opsagents-' + a.id });
        n.onclick = function () {
          try { window.focus(); } catch (e) { /* ignore */ }
          followLink(l || { href: '#/alerts', external: false });
          n.close();
        };
      }
    } catch (e) { /* notifications unavailable (e.g. insecure context) */ }
  }

  function tick() {
    if (state.inFlight) { state.queued = true; return; }
    state.inFlight = true;
    state.tickCount++;
    var token = state.token;
    var route = state.route;
    var page = PAGES[route.name];
    var outage = false;
    var slow = state.tickCount === 1 || state.tickCount % SLOW_EVERY === 0 || state.forceSlow;
    state.forceSlow = false;
    var soft = function (p, ok) {
      return p.then(ok, function (e) { if (isOutage(e)) outage = true; return null; });
    };

    var jobs = [
      api('GET', '/overview').then(function (o) { state.overview = o; state.overviewOk = true; },
        function (e) { state.overviewOk = false; if (isOutage(e)) outage = true; }),
      soft(api('GET', '/alerts'), processAlerts)
    ];
    if (slow) {
      jobs.push(soft(api('GET', '/github/status'), function (s) { state.githubStatus = s || {}; }));
      jobs.push(soft(api('GET', '/runs?limit=3'), function (r) { state.recents = arr(r); }));
    }
    var pPage = Promise.resolve().then(function () { return page.fetch(route.params); }).then(function (d) { return { ok: true, d: d }; }, function (e) {
      if (isOutage(e)) outage = true;
      return { ok: false, e: e };
    });
    jobs.push(pPage);

    Promise.all(jobs).then(function (res) {
      var r = res[res.length - 1];
      state.offline = outage;
      if (token === state.token) {
        if (r.ok) { state.page = r.d; state.pageError = null; }
        else { state.pageError = r.e; if (!isOutage(r.e)) state.page = null; }
      }
      renderChrome();
      if (token === state.token) { renderHomeOverview(); renderCurrent(); }
    }).catch(function (e) {
      if (window.console) console.error(e);
    }).then(function () {
      state.inFlight = false;
      if (state.queued) { state.queued = false; tick(); }
    });
  }

  function refreshSoon(slow) {
    if (slow) state.forceSlow = true;
    tick();
  }

  // ---- Actions ----------------------------------------------------------------

  function withBusy(key, fn) {
    if (state.busy[key]) return Promise.resolve();
    state.busy[key] = true;
    renderCurrent();
    return Promise.resolve().then(fn).then(function (v) {
      delete state.busy[key];
      renderCurrent();
      return v;
    }, function (e) {
      delete state.busy[key];
      renderCurrent();
      throw e;
    });
  }

  function gotoRun(res) {
    if (res && res.run_id) window.location.hash = '#/runs/' + encodeURIComponent(res.run_id);
    else refreshSoon(true);
  }

  function startScenario(id) {
    withBusy('scenario:' + id, function () {
      return api('POST', '/runs', { scenario_id: id, requested_by: state.actingAs || undefined }).then(function (res) {
        toast('Run started', 'ok');
        state.forceSlow = true;
        gotoRun(res);
      });
    }).catch(function (e) { toast(errorText(e), 'err'); });
  }

  function submitIncident() {
    var ta = $('incident-text'), btn = $('incident-btn');
    if (!ta) return;
    var text = ta.value.trim();
    if (!text) { toast('Describe the incident first.', 'err'); ta.focus(); return; }
    if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
    api('POST', '/runs', { text: text, requested_by: state.actingAs || undefined }).then(function (res) {
      state.incidentDraft = '';
      ta.value = '';
      toast('Incident sent to triage', 'ok');
      state.forceSlow = true;
      gotoRun(res);
    }, function (e) {
      toast(errorText(e), 'err');
    }).then(function () {
      if (btn && document.body.contains(btn)) { btn.disabled = false; btn.textContent = 'Analyse incident'; }
    });
  }

  function requireName() {
    var who = (state.actingAs || '').trim();
    if (!who) {
      toast('Set your name first (bottom of the sidebar).', 'err');
      state.editingName = true;
      renderUserRow();
    }
    return who;
  }

  function vote(id, which) {
    var who = requireName();
    if (!who) return;
    withBusy('vote:' + id, function () {
      return api('POST', '/approvals/' + encodeURIComponent(id) + '/' + which, { decided_by: who }).then(function (res) {
        res = res || {};
        var n = arr(res.approvals).length, need = res.required_approvals;
        if (which === 'reject') toast('Rejected by ' + who, 'ok');
        else if (res.status === 'approved') toast('Approved' + (need ? ' (' + n + '/' + need + ')' : '') + ' — the orchestrator will execute it', 'ok');
        else toast('Vote recorded' + (need ? ' (' + n + '/' + need + ' approvals)' : ''), 'ok');
      });
    }).catch(function (e) { toast(errorText(e), 'err'); }).then(function () { refreshSoon(); });
  }

  function triage(runId) {
    var route = state.triageChoice[runId];
    if (!route) { toast('Pick a route first.', 'err'); return; }
    var who = requireName();
    if (!who) return;
    withBusy('triage:' + runId, function () {
      return api('POST', '/runs/' + encodeURIComponent(runId) + '/triage', { route: route, decided_by: who }).then(function () {
        delete state.triageChoice[runId];
        toast('Run routed to ' + route, 'ok');
      });
    }).catch(function (e) { toast(errorText(e), 'err'); }).then(function () { refreshSoon(); });
  }

  function investigate(btn) {
    var kind = btn.getAttribute('data-kind');
    var body = { kind: kind };
    if (kind === 'pull_request') body.number = Number(btn.getAttribute('data-number'));
    else body.sha = btn.getAttribute('data-sha');
    var repo = btn.getAttribute('data-repo');
    if (repo) body.repo = repo;
    var key = 'investigate:' + (kind === 'pull_request' ? 'pr' + body.number : body.sha);
    withBusy(key, function () {
      return api('POST', '/github/investigate', body).then(function (res) {
        toast('Investigation started', 'ok');
        state.forceSlow = true;
        gotoRun(res);
      });
    }).catch(function (e) { toast(errorText(e), 'err'); });
  }

  function markRead(ids) {
    // optimistic update so the badge reacts immediately
    arr(state.alerts).forEach(function (a) { if (ids === 'all' || ids.indexOf(a.id) !== -1) a.read = true; });
    renderChrome();
    renderCurrent();
    return api('POST', '/alerts/read', { ids: ids }).catch(function (e) { toast(errorText(e), 'err'); }).then(function () { refreshSoon(); });
  }

  function enableNotifications() {
    if (!notifySupported()) { toast('This browser does not support desktop notifications.', 'err'); return; }
    var done = function (p) {
      if (p === 'granted') toast('Desktop alerts enabled', 'ok');
      else if (p === 'denied') toast('Desktop alerts were blocked. You can allow them in the browser\'s site settings.', 'err');
      renderCurrent();
    };
    try {
      var r = window.Notification.requestPermission(done);
      if (r && typeof r.then === 'function') r.then(done, function () { done('denied'); });
    } catch (e) {
      toast('Could not ask for notification permission: ' + e.message, 'err');
    }
  }

  function ghSimple(path, key, okMsg, body) {
    withBusy(key, function () {
      return api('POST', path, body || {}).then(function () { if (okMsg) toast(okMsg, 'ok'); });
    }).catch(function (e) { toast(errorText(e), 'err'); }).then(function () { refreshSoon(true); });
  }

  function addRepo() {
    var inp = $('repo-add-input');
    var repo = inp.value.trim();
    state.repoFormError = '';
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) {
      state.repoFormError = 'Enter the repository as owner/name.';
      $('repo-form-error').textContent = state.repoFormError;
      return;
    }
    $('repo-add-btn').disabled = true;
    api('POST', '/github/repos', { repo: repo }).then(function () {
      inp.value = '';
      toast('Added ' + repo, 'ok');
    }, function (e) {
      state.repoFormError = errorText(e);
      toast(state.repoFormError, 'err');
    }).then(function () {
      var b = $('repo-add-btn');
      if (b) b.disabled = false;
      var el = $('repo-form-error');
      if (el) el.textContent = state.repoFormError;
      refreshSoon(true);
    });
  }

  function removeRepo(repo) {
    if (!window.confirm('Stop watching ' + repo + '?')) return;
    var parts = repo.split('/');
    withBusy('repo-remove:' + repo, function () {
      return api('DELETE', '/github/repos/' + encodeURIComponent(parts[0]) + '/' + encodeURIComponent(parts.slice(1).join('/'))).then(function () {
        toast('Removed ' + repo, 'ok');
        if (state.filters.activity.repo === repo) delete state.filters.activity.repo;
      });
    }).catch(function (e) { toast(errorText(e), 'err'); }).then(function () { refreshSoon(true); });
  }

  function gotoActivity(f) {
    closePopover();
    state.filters.activity = {};
    Object.keys(f).forEach(function (k) { if (hasVal(f[k])) state.filters.activity[k] = f[k]; });
    if (state.route.name === 'activity') filtersChanged('activity');
    else window.location.hash = '#/activity';
  }

  function filtersChanged(page) {
    closePopover();
    if (state.route.name === page || (page === 'analytics' && state.route.name === 'analytics')) {
      state.token++;
      var tb = $('toolbar');
      if (tb) setHTML(tb, toolbarHTML(page));
      if (page === 'analytics') {
        state.page = null;
        region('analytics-body', skeletonCards(6));
      } else {
        renderCurrent();
      }
      tick();
    }
  }

  // ---- Events -------------------------------------------------------------------

  document.addEventListener('click', function (ev) {
    var t = ev.target;
    if (!(t instanceof Element)) return;

    // close the popover when clicking outside it (but not on its own anchor)
    if (pop.kind && !t.closest('#popover') && !(pop.anchor && pop.anchor.contains(t))) closePopover();

    var palItem = t.closest('[data-pal]');
    if (palItem) { activatePalette(Number(palItem.getAttribute('data-pal'))); return; }
    if (t.id === 'palette') { closePalette(); return; }
    if (t.id === 'scrim') { closeDrawer(); return; }
    if (t.closest('#ws-btn')) { openPopover($('ws-btn'), 'workspace'); return; }
    if (t.closest('#bell-btn')) { openPopover($('bell-btn'), 'bell', { side: 'right' }); return; }
    if (t.closest('#collapse-btn')) { setCollapsed(!document.body.classList.contains('sb-collapsed')); return; }

    var btn = t.closest('[data-action]');
    if (btn) {
      if (btn.disabled) return;
      var a = btn.getAttribute('data-action');
      var page = btn.getAttribute('data-page');
      var key = btn.getAttribute('data-key');
      switch (a) {
        case 'palette-open': ev.preventDefault(); openPalette(); return;
        case 'new-run':
          closeDrawer();
          state.scrollToStart = true;
          if (state.route.name === 'home') { state.scrollToStart = false; var s = $('start-run'); if (s) s.scrollIntoView({ block: 'start' }); }
          else window.location.hash = '#/home';
          return;
        case 'toggle-analytics':
          if (document.body.classList.contains('sb-collapsed')) { window.location.hash = '#/analytics/summary'; return; }
          setAnalyticsOpen(!$('analytics-group').classList.contains('open'), true);
          return;
        case 'sidebar-open': document.body.classList.add('sb-open'); $('scrim').hidden = false; return;
        case 'theme-toggle': applyTheme(currentTheme() === 'dark' ? 'light' : 'dark', true); closePopover(); renderCurrent(); return;
        case 'theme-set': applyTheme(btn.getAttribute('data-theme-pref'), true); renderCurrent(); return;
        case 'edit-name': state.editingName = true; renderUserRow(); return;
        case 'filter-open': openPopover(btn, 'filter', { page: page, key: key }); return;
        case 'filter-set':
          state.filters[page][key] = btn.getAttribute('data-value');
          filtersChanged(page);
          return;
        case 'filter-clear': delete state.filters[page][key]; filtersChanged(page); return;
        case 'filter-clear-all':
          Object.keys(state.filters[page]).forEach(function (k) {
            var d = filterDef(page, k);
            if (!d || !d.required) delete state.filters[page][k];
          });
          filtersChanged(page);
          return;
        case 'goto-activity':
          ev.preventDefault();
          gotoActivity({ repo: btn.getAttribute('data-repo'), ci: btn.getAttribute('data-ci'), kind: btn.getAttribute('data-kind') });
          return;
        case 'start-scenario': startScenario(btn.getAttribute('data-id')); return;
        case 'submit-incident': submitIncident(); return;
        case 'vote': vote(btn.getAttribute('data-id'), btn.getAttribute('data-vote')); return;
        case 'triage': triage(btn.getAttribute('data-run')); return;
        case 'toggle':
          state.expanded[key] = !state.expanded[key];
          renderCurrent();
          return;
        case 'investigate': investigate(btn); return;
        case 'alert-read': {
          // send the id exactly as the API gave it (the real backend uses integer ids)
          var ar = arr(state.alerts).filter(function (x) { return String(x.id) === btn.getAttribute('data-id'); })[0];
          if (ar) markRead([ar.id]);
          return;
        }
        case 'alerts-read-all': markRead('all'); return;
        case 'alert-follow': {
          var al = arr(state.alerts).filter(function (x) { return String(x.id) === btn.getAttribute('data-id'); })[0];
          if (al && !al.read) markRead([al.id]);
          return; // let the link navigate normally
        }
        case 'alert-open': {
          var id = btn.getAttribute('data-id');
          var alr = arr(state.alerts).filter(function (x) { return String(x.id) === id; })[0];
          closePopover();
          if (!alr) return;
          if (!alr.read) markRead([alr.id]);
          followLink(alertLink(alr.link) || { href: '#/alerts', external: false });
          return;
        }
        case 'notify-enable': enableNotifications(); return;
        case 'gh-sync': ghSimple('/github/sync', 'gh-sync', 'Sync started'); return;
        case 'gh-disconnect':
          if (window.confirm('Disconnect GitHub? The stored token is deleted on the server.')) ghSimple('/github/disconnect', 'gh-disconnect', 'GitHub disconnected');
          return;
        case 'repo-remove': removeRepo(btn.getAttribute('data-repo')); return;
        case 'repo-follow': followRepo(btn.getAttribute('data-repo')); return;
        case 'gh-signin': startSignIn(); return;
        case 'device-copy': copyDeviceCode(); return;
        case 'device-open': if (dev) window.open(dev.uri, '_blank', 'noopener,noreferrer'); return;
        case 'device-cancel': stopDevice(); return;
      }
      return;
    }
    if (t.closest('a, button, input, select, textarea, label')) return;
    var row = t.closest('tr[data-href]');
    if (row) window.location.hash = row.getAttribute('data-href');
  });

  document.addEventListener('submit', function (ev) {
    var f = ev.target;
    ev.preventDefault();
    if (f.id === 'gh-token-form') ghUseToken();
    else if (f.id === 'gh-client-form') ghSaveClient();
    else if (f.id === 'repo-form') addRepo();
    else if (f.getAttribute('data-filter-form')) {
      var page = f.getAttribute('data-page'), key = f.getAttribute('data-key');
      var v = f.elements.v.value.trim();
      if (v) state.filters[page][key] = v; else delete state.filters[page][key];
      filtersChanged(page);
    }
  });

  document.addEventListener('input', function (ev) {
    var t = ev.target;
    if (t.id === 'home-search') { state.search = t.value; renderCurrent(); }
    else if (t.id === 'incident-text') state.incidentDraft = t.value;
    else if (t.id === 'repo-search') { state.repoSearch = t.value; renderCurrent(); }
    else if (t.id === 'palette-input') { state.palette.index = 0; renderPalette(); }
    else if (t.getAttribute && t.getAttribute('data-pop-filter')) {
      var q = t.value.trim().toLowerCase();
      Array.prototype.forEach.call($('popover').querySelectorAll('.pop-item[data-label]'), function (n) {
        n.hidden = q && n.getAttribute('data-label').indexOf(q) === -1;
      });
    }
  });

  document.addEventListener('change', function (ev) {
    var t = ev.target;
    if (t.id === 'auto-investigate') {
      var on = t.checked;
      api('POST', '/github/settings', { auto_investigate: on }).then(function () {
        toast(on ? 'Failures will be investigated automatically' : 'Automatic investigation turned off', 'ok');
      }, function (e) { t.checked = !on; toast(errorText(e), 'err'); }).then(function () { refreshSoon(true); });
      return;
    }
    var runId = t.getAttribute && t.getAttribute('data-triage-select');
    if (runId) {
      state.triageChoice[runId] = t.value;
      Array.prototype.forEach.call(document.querySelectorAll('[data-route-agents]'), function (n) {
        if (n.getAttribute('data-route-agents') === runId) n.textContent = routeAgentsText(t.value);
      });
    }
  });

  document.addEventListener('focusout', function (ev) {
    if (ev.target && ev.target.id === 'acting-as' && state.editingName) saveName(ev.target.value);
  });

  document.addEventListener('keydown', function (ev) {
    var t = ev.target;
    if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && (ev.key === 'k' || ev.key === 'K')) {
      ev.preventDefault();
      if (state.palette.open) closePalette(); else openPalette();
      return;
    }
    if (dev && ev.key === 'Escape') { stopDevice(); return; }
    if (state.palette.open) {
      var p = state.palette;
      if (ev.key === 'Escape') { ev.preventDefault(); closePalette(); return; }
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (p.items.length) p.index = (p.index + (ev.key === 'ArrowDown' ? 1 : -1) + p.items.length) % p.items.length;
        renderPalette();
        return;
      }
      if (ev.key === 'Enter') { ev.preventDefault(); activatePalette(p.index); return; }
      return;
    }
    if (t && t.id === 'acting-as') {
      if (ev.key === 'Enter') { ev.preventDefault(); saveName(t.value); }
      else if (ev.key === 'Escape') { state.editingName = false; renderUserRow(); }
      return;
    }
    if (ev.key === 'Escape') {
      if (pop.kind) { closePopover(); return; }
      closeDrawer();
      return;
    }
    if (t && t.id === 'incident-text' && ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      submitIncident();
    }
  });

  document.addEventListener('mousemove', function (ev) {
    if (!state.palette.open) return;
    var it = ev.target instanceof Element && ev.target.closest('[data-pal]');
    if (!it) return;
    var i = Number(it.getAttribute('data-pal'));
    if (i !== state.palette.index) {
      state.palette.index = i;
      Array.prototype.forEach.call(document.querySelectorAll('[data-pal]'), function (n) {
        n.classList.toggle('kb', Number(n.getAttribute('data-pal')) === i);
      });
    }
  });

  window.addEventListener('resize', function () { if (pop.kind) positionPopover(); });
  window.addEventListener('scroll', function (ev) {
    if (pop.kind !== 'filter') return;
    if (ev.target instanceof Element && ev.target.closest('#popover')) return; // scrolling the option list itself
    closePopover();
  }, true);

  // ===========================================================================
  // 8. MOCK MODE (?mock=1): an in-memory fake of the whole backend.
  //    Everything below only runs when the URL contains ?mock=1.
  // ===========================================================================

  var M = null;
  var MIN = 60000, HOUR = 60 * MIN, DAY = 24 * HOUR;

  function mockApi(method, path, body) {
    if (!M) mockSeed();
    return new Promise(function (resolve, reject) {
      setTimeout(function () {
        try {
          mockAdvance();
          var out = mockHandle(method, path, body);
          resolve(out === undefined ? null : JSON.parse(JSON.stringify(out, function (k, v) {
            return k.charAt(0) === '_' ? undefined : v;
          })));
        } catch (e) { reject(e); }
      }, 90 + Math.random() * 140);
    });
  }

  function mockErr(status, detail) { var e = new Error(detail); e.status = status; return e; }
  function iso(msOffset) { return new Date(Date.now() + msOffset).toISOString(); }
  function dayKey(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  function fakeHash(seed) {
    var out = '', h = 2166136261;
    for (var round = 0; round < 8; round++) {
      for (var i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i) + round; h = Math.imul(h, 16777619) >>> 0; }
      out += ('00000000' + h.toString(16)).slice(-8);
    }
    return out;
  }

  function seeded(seed) {
    var s = seed >>> 0 || 1;
    return function () { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
  }

  function mockAudit(agent, decision, summary, when) {
    var last = M.audit[M.audit.length - 1];
    var id = last ? last.id + 1 : 1;
    var prev = last ? last.row_hash : null;
    M.audit.push({ id: id, created_at: when || iso(0), agent_name: agent, decision: decision, input_summary: summary, prev_hash: prev, row_hash: fakeHash((prev || 'genesis') + '|' + id + '|' + summary) });
  }

  function mockAlert(severity, title, message, source, link, read, when) {
    M.alerts.push({ id: uuid(), severity: severity, title: title, message: message, source: source, link: link || null, created_at: when || iso(0), read: !!read });
  }

  var REPO_A = 'hendch/DSProject5DS1', REPO_B = 'hendch/ml-pipeline';
  function ghUrl(repo, path) { return 'https://github.com/' + repo + path; }

  var MOCK_SCENARIOS = [
    { id: 'review_pr', title: 'Review a pull request', description: 'Static checks plus an LLM review of the diff; posts review comments.', trigger_type: 'pull_request', agents: ['diagnostic_rca', 'code_reviewer'] },
    { id: 'failed_build', title: 'Diagnose a failed build', description: 'Reads the CI logs, ranks likely root causes and suggests a fix.', trigger_type: 'build_failed', agents: ['diagnostic_rca', 'dependency_resolver'] },
    { id: 'failed_deploy', title: 'Investigate a failed deploy', description: 'Finds what changed before the deploy broke and proposes a rollback.', trigger_type: 'deploy_failed', agents: ['diagnostic_rca'] },
    { id: 'merge', title: 'Untangle a merge conflict', description: 'Explains both sides of each hunk and proposes a resolution.', trigger_type: 'merge_conflict', agents: ['merge_assistant'] },
    { id: 'drift', title: 'Check for drift', description: 'Compares live infrastructure and model metrics with their baselines.', trigger_type: 'drift_alert', agents: ['drift_detection', 'diagnostic_rca'] }
  ];

  var MOCK_ROUTES = [
    { id: 'build_failed', title: 'Failed CI build', agents: ['diagnostic_rca', 'dependency_resolver'] },
    { id: 'deploy_failed', title: 'Failed deploy', agents: ['diagnostic_rca'] },
    { id: 'pull_request', title: 'Pull request review', agents: ['diagnostic_rca', 'code_reviewer'] },
    { id: 'merge_conflict', title: 'Merge conflict', agents: ['merge_assistant'] },
    { id: 'drift_detected', title: 'Infrastructure / model drift', agents: ['drift_detection', 'diagnostic_rca'] }
  ];

  var MOCK_AGENTS = [
    { name: 'dependency_resolver', title: 'Dependency Resolver', owner: 'rayen', description: 'Solves Python dependency conflicts with a constraint solver and explains the chosen versions.', model: 'x.ai:grok-4', status: 'available', reason: 'Solver and LLM reachable', actions: ['pin_dependency', 'open_pull_request'] },
    { name: 'diagnostic_rca', title: 'Diagnostic / RCA', owner: 'zrharounesprit', description: 'Reads logs and recent changes, then ranks likely root causes with evidence.', model: 'x.ai:grok-4', status: 'available', reason: 'Ready', actions: ['rank_causes', 'open_incident_ticket', 'rerun_pipeline', 'rollback_deploy'] },
    { name: 'merge_assistant', title: 'Merge Assistant', owner: 'karim', description: 'Explains both sides of a merge conflict and proposes a resolution patch.', model: 'x.ai:grok-4', status: 'available', reason: 'Ready', actions: ['propose_resolution'] },
    { name: 'code_reviewer', title: 'Code Reviewer', owner: 'ines', description: 'Runs static analysis and an LLM review on pull request diffs, with ML-specific checks (leakage, splits, reproducibility).', model: 'x.ai:grok-4', status: 'degraded', reason: 'LLM rate-limited — falling back to static checks only', actions: ['post_review_comment', 'request_changes'] },
    { name: 'drift_detection', title: 'Drift Detection', owner: 'yassine', description: 'Compares live cloud resources and model metrics against their declared baselines.', model: null, status: 'available', reason: 'Terraform state readable', actions: ['revert_iam_policy', 'open_incident_ticket'] }
  ];

  function mockSeed() {
    M = { runs: [], audit: [], alerts: [], activity: [], _bootAt: Date.now(), _events: {} };
    M.status = {
      connected: true, signed_in: true, auth: 'oauth', oauth_ready: true,
      repo: REPO_A, repos: [REPO_A, REPO_B], login: 'hendch', avatar_url: null,
      last_sync: iso(-20000), rate_limit: { remaining: 4873, reset_at: iso(38 * MIN) }, error: null, poll_seconds: 60, auto_investigate: true
    };
    M.repos = [
      { repo: REPO_A, default_branch: 'main', private: false, last_sync: iso(-20000) },
      { repo: REPO_B, default_branch: 'main', private: true, last_sync: iso(-50000) }
    ];
    M.available = [
      { repo: REPO_A, private: false, default_branch: 'main', pushed_at: iso(-3 * MIN), description: 'Multi-agent DevOps / MLOps assistant (school project).' },
      { repo: REPO_B, private: true, default_branch: 'main', pushed_at: iso(-26 * HOUR), description: 'Training and evaluation pipeline for the recommender.' },
      { repo: 'hendch/feature-store', private: true, default_branch: 'main', pushed_at: iso(-2 * DAY), description: 'Feature definitions and offline/online sync jobs.' },
      { repo: 'hendch/infra-terraform', private: true, default_branch: 'main', pushed_at: iso(-4 * DAY), description: 'Terraform for the EKS cluster, IAM and buckets.' },
      { repo: 'hendch/notebooks', private: false, default_branch: 'master', pushed_at: iso(-12 * DAY), description: 'Exploration notebooks.' },
      { repo: 'hendch/dotfiles', private: false, default_branch: 'main', pushed_at: iso(-40 * DAY), description: null },
      { repo: 'esprit-ds/ds-project-template', private: false, default_branch: 'main', pushed_at: iso(-70 * DAY), description: 'Course project template.' }
    ];
    // ?mock=1&gh=setup -> no OAuth App configured, signed out; &gh=signedout -> OAuth ready, signed out
    var ghMode = (/[?&]gh=(setup|signedout)(?:&|$)/.exec(window.location.search) || [])[1];
    if (ghMode) {
      mockSignOut();
      M.status.oauth_ready = ghMode === 'signedout';
    }

    // --- runs -------------------------------------------------------------
    var r1 = {
      id: uuid(), title: 'Drift alert: prod IAM policy changed outside Terraform', trigger_type: 'drift_alert',
      route: 'drift_detected', routed_by: 'rule', route_confidence: null, status: 'waiting_approval',
      summary: 'Drift Detection found 1 IAM policy on prod-data-bucket that grants s3:* to a CI role. A revert is waiting for 2 approvals.',
      agents: ['drift_detection', 'diagnostic_rca'], created_at: iso(-14 * MIN), finished_at: null, text: null,
      trigger: { source: 'scenario', scenario: 'drift', repo: REPO_A, resource: 'arn:aws:iam::123456789012:policy/prod-data-access' }, error: null,
      steps: [
        { step_no: 1, agent: 'drift_detection', status: 'done', summary: '1 drifted resource: inline policy on prod-data-access now allows s3:* on prod-data-bucket (Terraform declares s3:GetObject only).', output: { drifted_resources: [{ address: 'aws_iam_policy.prod_data_access', change: 'Action s3:GetObject → s3:*', changed_by: 'ci-bot' }], checked: 48 }, error: null, started_at: iso(-14 * MIN), finished_at: iso(-14 * MIN + 9200) },
        { step_no: 2, agent: 'diagnostic_rca', status: 'done', summary: 'Most likely cause: a manual hotfix during last night\'s incident was never reverted (0.81). Less likely: compromised CI credentials (0.12).', output: { causes: [{ cause: 'Manual hotfix not reverted', confidence: 0.81 }, { cause: 'Compromised CI credentials', confidence: 0.12 }] }, error: null, started_at: iso(-14 * MIN + 9300), finished_at: iso(-14 * MIN + 21800) }
      ],
      actions: [
        { action_id: uuid(), agent: 'drift_detection', action_type: 'revert_iam_policy', decision: 'pending', decided_by: null, execution: 'not_run', execution_result: null,
          payload: { policy: 'prod-data-access', target_version: 'v7' },
          _dry: 'Dry run: would apply policy version v7 to prod-data-access (s3:* → s3:GetObject). No changes made.',
          approval: { id: uuid(), status: 'pending', environment: 'prod', blast_radius: 'medium', risk: 'high', policy_rule: 'iam_changes_require_two_approvals', reason: 'IAM change in prod: two humans must approve.', llm_summary: 'Reverts the prod data policy to its Terraform version, removing broad s3:* access. Low chance of breaking reads; CI jobs that write to the bucket would start failing.', required_approvals: 2, approved_by: [], requested_by: 'drift_detection', expires_at: iso(46 * MIN) } },
        { action_id: uuid(), agent: 'diagnostic_rca', action_type: 'open_incident_ticket', decision: 'approved', decided_by: 'guardrail', execution: 'dry_run', payload: { board: 'Ops' }, execution_result: { message: 'Dry run: would open ticket "IAM drift on prod-data-access" in the Ops board.' }, approval: null }
      ],
      timeline: []
    };
    r1.timeline = [
      { time: r1.created_at, kind: 'event', actor: 'orchestrator', text: 'Received drift_alert; routed by rule to drift_detected.' },
      { time: r1.steps[0].finished_at, kind: 'event', actor: 'drift_detection', text: 'Finished: 1 drifted resource.' },
      { time: r1.steps[1].finished_at, kind: 'event', actor: 'diagnostic_rca', text: 'Finished: ranked 2 causes.' },
      { time: iso(-14 * MIN + 22500), kind: 'audit', actor: 'guardrail', text: 'open_incident_ticket auto-approved (low risk).' },
      { time: iso(-14 * MIN + 22600), kind: 'audit', actor: 'guardrail', text: 'revert_iam_policy held: risk high, needs 2 approvals.' }
    ];

    var r2 = {
      id: uuid(), title: 'Review PR #11: Add drift detection agent', trigger_type: 'pull_request',
      route: 'pull_request', routed_by: 'rule', route_confidence: null, status: 'completed',
      summary: 'Review posted with 2 comments. One possible train/test leakage in features/build.py.',
      agents: ['diagnostic_rca', 'code_reviewer'], created_at: iso(-95 * MIN), finished_at: iso(-95 * MIN + 48000), text: null,
      trigger: { source: 'github', pr: 11, repo: REPO_A, html_url: ghUrl(REPO_A, '/pull/11') }, error: null,
      steps: [
        { step_no: 1, agent: 'diagnostic_rca', status: 'done', summary: 'CI is green; no risky changes in the dependency graph.', output: { ci: 'passed', changed_files: 3 }, error: null, started_at: iso(-95 * MIN), finished_at: iso(-95 * MIN + 14000) },
        { step_no: 2, agent: 'code_reviewer', status: 'degraded', summary: 'LLM was rate-limited, so this review uses static checks only. Found 2 issues.', output: { findings: [{ file: 'features/build.py', line: 88, category: 'leakage', severity: 'high' }, { file: 'agents/drift.py', line: 12, category: 'style', severity: 'low' }], llm_used: false }, error: null, started_at: iso(-95 * MIN + 14100), finished_at: iso(-95 * MIN + 46000) }
      ],
      actions: [
        { action_id: uuid(), agent: 'code_reviewer', action_type: 'post_review_comment', decision: 'approved', decided_by: 'guardrail', execution: 'done', payload: { comments: 2 }, execution_result: { message: 'Posted 2 review comments on PR #11.' }, approval: null }
      ],
      timeline: []
    };
    r2.timeline = [
      { time: r2.created_at, kind: 'event', actor: 'orchestrator', text: 'Received pull_request #11 from GitHub; routed by rule.' },
      { time: r2.steps[1].finished_at, kind: 'event', actor: 'code_reviewer', text: 'Degraded: LLM rate-limited, static checks only.' },
      { time: iso(-95 * MIN + 46500), kind: 'audit', actor: 'guardrail', text: 'post_review_comment auto-approved (low risk).' },
      { time: r2.finished_at, kind: 'event', actor: 'orchestrator', text: 'Executed 1 action. Run completed.' }
    ];

    var r3 = {
      id: uuid(), title: 'Recommendation endpoint latency doubled', trigger_type: 'free_text',
      route: null, routed_by: null, route_confidence: 0.41, status: 'needs_triage',
      summary: 'LLM triage was not confident enough (41%) to pick a route.',
      agents: [], created_at: iso(-6 * MIN), finished_at: null,
      text: 'p95 latency on /recommend went from 120ms to 260ms since this morning. No deploys today, but the feature store was migrated last night.',
      trigger: { source: 'text' }, error: null, steps: [], actions: [],
      timeline: [
        { time: iso(-6 * MIN), kind: 'event', actor: 'orchestrator', text: 'Received a free-text incident.' },
        { time: iso(-6 * MIN + 3200), kind: 'event', actor: 'orchestrator', text: 'LLM triage: best guess drift_detected at 41% — below threshold, asking a human.' }
      ]
    };

    var r4sha = '9f2c1e4b7a0d3e5f6a7b8c9d0e1f2a3b4c5d6e7f';
    var r4 = {
      id: uuid(), title: 'Investigate failing commit 9f2c1e4: Pin torch 2.4 for faster training', trigger_type: 'build_failed',
      route: 'build_failed', routed_by: 'rule', route_confidence: null, status: 'failed',
      summary: 'Root cause found (torch 2.4 vs CUDA 11.8 mismatch), but the dependency step timed out and the rerun approval expired.',
      agents: ['diagnostic_rca', 'dependency_resolver'], created_at: iso(-26 * HOUR), finished_at: iso(-26 * HOUR + 75000), text: null,
      trigger: { source: 'github', sha: r4sha, repo: REPO_B, html_url: ghUrl(REPO_B, '/commit/' + r4sha) }, error: 'dependency_resolver failed: timed out after 60s querying the package index.',
      steps: [
        { step_no: 1, agent: 'diagnostic_rca', status: 'done', summary: 'torch 2.4.0 was pulled in transitively and needs CUDA 12; runners have CUDA 11.8.', output: { causes: [{ cause: 'torch/CUDA mismatch', confidence: 0.9 }] }, error: null, started_at: iso(-26 * HOUR), finished_at: iso(-26 * HOUR + 12000) },
        { step_no: 2, agent: 'dependency_resolver', status: 'failed', summary: null, output: null, error: 'Timed out after 60s querying the package index mirror.', started_at: iso(-26 * HOUR + 12100), finished_at: iso(-26 * HOUR + 72100) }
      ],
      actions: [
        { action_id: uuid(), agent: 'diagnostic_rca', action_type: 'rerun_pipeline', decision: 'rejected', decided_by: 'guardrail', execution: 'skipped', payload: { pipeline: 'nightly-train' }, execution_result: { message: 'Not executed: approval expired.' },
          approval: { id: uuid(), status: 'expired', environment: 'ci', blast_radius: 'low', risk: 'medium', policy_rule: 'reruns_on_main_need_approval', reason: 'Re-running on main uses shared GPU runners.', llm_summary: null, required_approvals: 1, approved_by: [], requested_by: 'diagnostic_rca', expires_at: iso(-25 * HOUR) } }
      ],
      timeline: [
        { time: iso(-26 * HOUR), kind: 'event', actor: 'orchestrator', text: 'CI failed on ' + REPO_B + '@9f2c1e4; auto-investigation started.' },
        { time: iso(-26 * HOUR + 72100), kind: 'event', actor: 'dependency_resolver', text: 'Failed: timeout.' },
        { time: iso(-25 * HOUR), kind: 'audit', actor: 'guardrail', text: 'rerun_pipeline approval expired with 0/1 votes.' }
      ]
    };
    M.runs = [r3, r1, r2, r4];

    // --- GitHub activity --------------------------------------------------
    var commit = function (repo, sha, msg, author, branch, ci, ago, extra) {
      var it = { kind: 'commit', repo: repo, sha: sha, short_sha: sha.slice(0, 7), number: null, title: firstLine(msg), message: msg, author: author, avatar_url: null,
        branch: branch, state: null, ci: ci, run_url: ci === 'failure' || ci === 'running' ? ghUrl(repo, '/actions/runs/' + (7000 + Math.floor(Math.random() * 999))) : null,
        html_url: ghUrl(repo, '/commit/' + sha), time: iso(-ago), run_id: null };
      Object.keys(extra || {}).forEach(function (k) { it[k] = extra[k]; });
      return it;
    };
    var pr = function (repo, n, title, author, branch, st, ci, ago) {
      return { kind: 'pull_request', repo: repo, sha: fakeHash(title).slice(0, 40), short_sha: null, number: n, title: title, message: title, author: author, avatar_url: null,
        branch: branch, state: st, ci: ci, run_url: ci === 'failure' ? ghUrl(repo, '/actions/runs/' + (8100 + n)) : null, html_url: ghUrl(repo, '/pull/' + n), time: iso(-ago), run_id: null };
    };
    M.activity = [
      commit(REPO_A, 'a41c09de5b7f2c3d4e5f60718293a4b5c6d7e8f9', 'Add LLM triage confidence threshold\n\nRuns below 0.6 go to the triage queue.', 'rayen', 'main', 'running', 3 * MIN, { _flipAt: Date.now() + 25000 }),
      pr(REPO_A, 12, 'Guardrail: require 2 approvals for IAM changes', 'ines', 'feat/guardrail-iam', 'open', 'success', 40 * MIN),
      commit(REPO_A, 'c7d2e9f01a2b3c4d5e6f708192a3b4c5d6e7f809', 'Bump pandas to 2.2', 'karim', 'main', 'failure', 2 * HOUR),
      pr(REPO_A, 11, 'Add drift detection agent', 'yassine', 'feat/drift', 'merged', 'success', 3 * HOUR),
      commit(REPO_B, r4sha, 'Pin torch 2.4 for faster training', 'zrharounesprit', 'main', 'failure', 26 * HOUR + 2 * MIN, { run_id: r4.id }),
      commit(REPO_A, 'e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3', 'Fix flaky test in merge assistant', 'karim', 'dev', 'success', 28 * HOUR),
      pr(REPO_B, 10, 'Experiment: switch to poetry', 'rayen', 'exp/poetry', 'closed', 'none', 2 * DAY),
      commit(REPO_A, 'f9e8d7c6b5a4938271605f4e3d2c1b0a99887766', 'Docs: explain the approval flow in README', 'ines', 'main', 'none', 3 * DAY)
    ];

    // --- audit + alerts ---------------------------------------------------
    mockAudit('orchestrator', 'routed', 'build_failed ' + REPO_B + '@9f2c1e4 → build_failed (rule)', r4.created_at);
    mockAudit('diagnostic_rca', 'proposed', 'rerun_pipeline on nightly-train', iso(-26 * HOUR + 12500));
    mockAudit('guardrail', 'pending', 'rerun_pipeline: medium risk, needs 1 approval', iso(-26 * HOUR + 12600));
    mockAudit('guardrail', 'expired', 'rerun_pipeline approval expired', iso(-25 * HOUR));
    mockAudit('orchestrator', 'routed', 'pull_request #11 → pull_request (rule)', r2.created_at);
    mockAudit('guardrail', 'auto_approved', 'post_review_comment on PR #11 (low risk)', iso(-95 * MIN + 46500));
    mockAudit('orchestrator', 'executed', 'post_review_comment: 2 comments posted', r2.finished_at);
    mockAudit('orchestrator', 'routed', 'drift_alert prod-data-access → drift_detected (rule)', r1.created_at);
    mockAudit('guardrail', 'auto_approved', 'open_incident_ticket (low risk)', iso(-14 * MIN + 22500));
    mockAudit('guardrail', 'pending', 'revert_iam_policy in prod: high risk, needs 2 approvals', iso(-14 * MIN + 22600));
    mockAudit('orchestrator', 'needs_triage', 'free text incident: confidence 0.41 below threshold', iso(-6 * MIN + 3200));

    mockAlert('success', 'PR #11 merged — all checks passed', 'Add drift detection agent', 'github', ghUrl(REPO_A, '/pull/11'), true, iso(-3 * HOUR));
    mockAlert('info', 'Synced 8 new commits', 'From ' + REPO_A + ' and ' + REPO_B + '.', 'github', '#/activity', true, iso(-50 * MIN));
    mockAlert('critical', 'CI failing on main', REPO_A + '@c7d2e9f “Bump pandas to 2.2” failed 3 checks.', 'github', '#/activity', false, iso(-2 * HOUR + 4 * MIN));
    mockAlert('warning', 'Approval needed: revert_iam_policy', 'High-risk proposal in prod needs 2 approvals.', 'guardrail', '#/runs/' + r1.id, false, iso(-14 * MIN + 22600));
  }

  function mockGhRefresh() {
    M.status.repo = M.status.repos[0] || null;
    M.status.connected = !!(M.status.signed_in && M.status.repos.length);
  }

  function mockSignOut() {
    M.status.signed_in = false;
    M.status.auth = null;
    M.status.login = null;
    M.status.repos = [];
    M.status.last_sync = null;
    M.status.rate_limit = null;
    M.repos = [];
    mockGhRefresh();
  }

  function mockSignIn(auth) {
    M.status.signed_in = true;
    M.status.auth = auth;
    M.status.login = 'hendch';
    M.status.error = null;
    M.status.rate_limit = { remaining: 4999, reset_at: iso(60 * MIN) };
    mockGhRefresh();
    mockAlert('success', 'Signed in to GitHub', 'Signed in as hendch. Choose the repositories to watch.', 'github', '#/repositories', false);
  }

  function mockSummary(r) {
    var pending = r.actions.filter(function (a) { return a.decision === 'pending'; }).length;
    return {
      id: r.id, title: r.title, trigger_type: r.trigger_type, route: r.route, routed_by: r.routed_by,
      route_confidence: r.route_confidence, status: r.status, summary: r.summary, agents: r.agents,
      pending: pending, created_at: r.created_at, finished_at: r.finished_at, text: r.text, trigger: r.trigger
    };
  }

  function mockFindRun(id) {
    var r = M.runs.filter(function (x) { return x.id === id; })[0];
    if (!r) throw mockErr(404, 'Run not found');
    return r;
  }

  function mockStartAgents(run, agents) {
    run.agents = agents.slice();
    run.status = 'running';
    run._doneAt = Date.now() + 4000 + Math.random() * 2500;
    run.steps = agents.map(function (a, i) {
      return { step_no: i + 1, agent: a, status: 'running', summary: null, output: null, error: null, started_at: iso(i * 1500), finished_at: null };
    });
    run.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Started ' + agents.join(', ') + '.' });
  }

  function mockAdvance() {
    var now = Date.now();
    M.runs.forEach(function (r) {
      if (r.status !== 'running' || !r._doneAt || now < r._doneAt) return;
      r.steps.forEach(function (s) {
        s.status = 'done';
        s.finished_at = iso(0);
        s.summary = agentTitle(s.agent) + ' finished its analysis (sample result).';
        s.output = { mock: true, findings: [{ item: 'example finding', score: 0.7 }] };
      });
      var first = r.agents[0];
      var actionType = ({ dependency_resolver: 'pin_dependency', diagnostic_rca: 'open_incident_ticket', merge_assistant: 'propose_resolution', code_reviewer: 'post_review_comment', drift_detection: 'open_incident_ticket' })[first] || 'note';
      r.actions.push({ action_id: uuid(), agent: first, action_type: actionType, decision: 'approved', decided_by: 'guardrail', execution: 'dry_run', payload: { sample: true }, execution_result: { message: 'Dry run: ' + actionType + ' would have been executed.' }, approval: null });
      r.status = 'completed';
      r.finished_at = iso(0);
      r.summary = 'Analysis finished. 1 low-risk action auto-approved and dry-run executed.';
      r.timeline.push({ time: iso(0), kind: 'audit', actor: 'guardrail', text: actionType + ' auto-approved (low risk).' });
      r.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Run completed.' });
      mockAudit('guardrail', 'auto_approved', actionType + ' (low risk)');
      mockAlert('success', 'Run completed', r.title, 'orchestrator', '#/runs/' + r.id, false);
    });
    // The running commit on main fails after ~25 s, which raises a new critical alert (demo of toast + desktop notification).
    M.activity.forEach(function (it) {
      if (it._flipAt && now >= it._flipAt) {
        delete it._flipAt;
        it.ci = 'failure';
        mockAlert('critical', 'CI failed on main', it.repo + '@' + it.short_sha + ' “' + it.title + '” failed 2 checks.', 'github', '#/activity', false);
      }
    });
  }

  function mockPendingApprovals(repo) {
    var out = [];
    M.runs.forEach(function (r) {
      if (repo && (!r.trigger || r.trigger.repo !== repo)) return;
      r.actions.forEach(function (a) {
        var ap = a.approval;
        if (!ap || ap.status !== 'pending' || a.decision !== 'pending') return;
        out.push({
          id: ap.id, run_id: r.id, run_title: r.title, agent: a.agent, action_type: a.action_type,
          environment: ap.environment, blast_radius: ap.blast_radius, risk: ap.risk, policy_rule: ap.policy_rule,
          reason: ap.reason, llm_summary: ap.llm_summary, required_approvals: ap.required_approvals,
          approved_by: ap.approved_by, requested_by: ap.requested_by, expires_at: ap.expires_at, created_at: r.created_at
        });
      });
    });
    return out;
  }

  function mockGuessRoute(text) {
    var t = text.toLowerCase();
    if (/merge|conflict|rebase/.test(t)) return 'merge_conflict';
    if (/deploy|rollout|release/.test(t)) return 'deploy_failed';
    if (/depend|version|pip|requirement|package|build|ci |pipeline|test fail/.test(t)) return 'build_failed';
    if (/drift|terraform|iam|policy/.test(t)) return 'drift_detected';
    if (/pull request|\bpr\b|review/.test(t)) return 'pull_request';
    return null;
  }

  function mockRepoHead(repo) {
    var r = M.repos.filter(function (x) { return x.repo === repo; })[0];
    var branch = r ? r.default_branch : 'main';
    var c = M.activity.filter(function (i) { return i.kind === 'commit' && i.repo === repo && i.branch === branch; })
      .sort(function (a, b) { return a.time < b.time ? 1 : -1; })[0];
    return c ? { short_sha: c.short_sha, message: c.message, ci: c.ci, html_url: c.html_url, time: c.time, author: c.author, branch: branch } : null;
  }

  function mockRepoCounts(repo) {
    var since = Date.now() - DAY;
    var items = M.activity.filter(function (i) { return !repo || i.repo === repo; });
    return {
      open_prs: items.filter(function (i) { return i.kind === 'pull_request' && i.state === 'open'; }).length,
      failing_today: items.filter(function (i) { return i.ci === 'failure' && Date.parse(i.time) >= since; }).length
    };
  }

  function mockAnalytics(days, repo) {
    days = [7, 30, 90].indexOf(days) === -1 ? 30 : days;
    var rnd = seeded(days * 31 + (repo ? repo.length * 7 : 3));
    var scale = repo ? (repo === REPO_B ? 0.4 : 0.65) : 1;
    var byDay = [], ciDay = [];
    var tot = { total: 0, completed: 0, failed: 0 };
    for (var i = days - 1; i >= 0; i--) {
      var d = new Date(Date.now() - i * DAY);
      var weekend = d.getDay() === 0 || d.getDay() === 6;
      var k = weekend ? 0.35 : 1;
      var completed = Math.round((3 + rnd() * 6) * scale * k);
      var failed = Math.round(rnd() * 2.2 * scale * k);
      var waiting = i < 2 ? Math.round(rnd() * 2) : 0;
      var other = rnd() < 0.15 ? 1 : 0;
      byDay.push({ date: dayKey(d), completed: completed, failed: failed, waiting: waiting, other: other, median_wait_seconds: rnd() < 0.25 ? null : Math.round(300 + rnd() * 2400) });
      tot.total += completed + failed + waiting + other;
      tot.completed += completed;
      tot.failed += failed;
      var succ = Math.round((5 + rnd() * 9) * scale * k), fail = Math.round(rnd() * 3 * scale * k);
      ciDay.push({ date: dayKey(d), success: succ, failure: fail, median_seconds: Math.round(420 - (days - i) * (150 / days) + rnd() * 90) });
    }
    var cs = ciDay.reduce(function (s, x) { return s + x.success; }, 0), cf = ciDay.reduce(function (s, x) { return s + x.failure; }, 0);
    var propTotal = Math.round(tot.total * 1.6);
    var auto = Math.round(propTotal * 0.63), held = Math.round(propTotal * 0.26);
    var prs = Math.round(days * 0.9 * scale);
    var agents = MOCK_AGENTS.map(function (a, idx) {
      var runs = Math.round(tot.total * [0.35, 0.9, 0.12, 0.45, 0.18][idx]);
      var degraded = a.name === 'code_reviewer' ? Math.round(runs * 0.3) : Math.round(runs * 0.04);
      var failedA = Math.round(runs * (0.03 + rnd() * 0.05));
      var unavailable = a.name === 'merge_assistant' ? Math.round(runs * 0.08) : 0;
      var skipped = Math.round(runs * 0.03);
      var llm = a.model ? Math.round(runs * (a.name === 'code_reviewer' ? 0.62 : 0.9)) : 0;
      return { name: a.name, runs: runs, done: Math.max(0, runs - degraded - failedA - unavailable - skipped), degraded: degraded, failed: failedA, unavailable: unavailable, skipped: skipped,
        proposals: Math.round(runs * [0.6, 1.1, 0.9, 1.4, 0.7][idx]), approval_rate: Math.round((0.7 + rnd() * 0.25) * 100) / 100, fallback: runs - llm, llm: llm, model: a.model };
    });
    var by_agent = {};
    agents.forEach(function (a) { by_agent[a.name] = a.proposals; });
    var f = function (n) { return Math.round(n * scale * days / 30); };
    return {
      range: { from: byDay[0].date, to: byDay[byDay.length - 1].date, days: days },
      repos_active: repo ? 1 : M.repos.length,
      runs: { total: tot.total, completed: tot.completed, failed: tot.failed, by_day: byDay },
      proposals: { total: propTotal, auto_approved: auto, held: held, rejected: propTotal - auto - held, by_agent: by_agent },
      decisions: { median_seconds_auto: 1.4, median_seconds_human: 1260, llm_escalations: f(23),
        by_rule: { low_risk_auto_approve: f(140), iam_changes_require_two_approvals: f(9), prod_changes_need_approval: f(31), reruns_on_main_need_approval: f(17), block_secrets_in_payload: f(3) } },
      reviews: { prs: prs, avg_findings: prs ? 3.4 : null, by_severity: { high: f(14), medium: f(41), low: f(63) },
        by_category: { leakage: f(6), split: f(9), reproducibility: f(17), evaluation: f(11), bug: f(24), security: f(5), injection: f(2), style: f(44) } },
      ci: { pass_rate: cs + cf ? Math.round(cs / (cs + cf) * 1000) / 1000 : null, by_day: ciDay, mttr_seconds: 2 * 3600 + 840 },
      time_saved_minutes: f(1130),
      root_causes: [
        { cause: 'Dependency version conflict', count: f(12) }, { cause: 'Flaky test', count: f(9) }, { cause: 'Config drift', count: f(6) },
        { cause: 'CUDA / driver mismatch', count: f(4) }, { cause: 'Expired credentials', count: f(2) }
      ],
      agents: agents,
      route_durations: [
        { route: 'build_failed', median_seconds: 38 }, { route: 'deploy_failed', median_seconds: 44 }, { route: 'pull_request', median_seconds: 52 },
        { route: 'merge_conflict', median_seconds: 21 }, { route: 'drift_detected', median_seconds: 29 }
      ]
    };
  }

  var REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

  function mockHandle(method, fullPath, body) {
    var qi = fullPath.indexOf('?');
    var path = qi === -1 ? fullPath : fullPath.slice(0, qi);
    var q = new URLSearchParams(qi === -1 ? '' : fullPath.slice(qi + 1));
    var seg = path.split('/').filter(Boolean).map(decodeURIComponent);
    body = body || {};

    if (method === 'GET') {
      if (path === '/overview') {
        var c = { total: M.runs.length, running: 0, executing: 0, waiting_approval: 0, completed: 0, failed: 0, needs_triage: 0 };
        M.runs.forEach(function (r) { if (c[r.status] !== undefined) c[r.status]++; });
        var primary = M.status.connected ? M.status.repo : null;
        var counts = mockRepoCounts(primary);
        return {
          runs: c, pending_approvals: mockPendingApprovals().length, audit_intact: true, llm: { label: 'x.ai:grok-4', ok: true },
          alerts_unread: M.alerts.filter(function (a) { return !a.read; }).length,
          github: { connected: M.status.connected, repo: primary, head: primary ? mockRepoHead(primary) : null, open_prs: counts.open_prs, failing_today: counts.failing_today, last_sync: M.status.last_sync },
          system: { database: true, policy_engine: true, audit_intact: true }
        };
      }
      if (path === '/scenarios') return MOCK_SCENARIOS;
      if (path === '/routes') return MOCK_ROUTES;
      if (path === '/agents') return MOCK_AGENTS;
      if (path === '/approvals') return mockPendingApprovals(q.get('repo'));
      if (path === '/alerts') return M.alerts.slice().reverse();
      if (path === '/analytics') return mockAnalytics(parseInt(q.get('days') || '30', 10), q.get('repo'));
      if (path === '/audit') return { intact: true, broken_rows: [], rows: M.audit.slice().reverse().slice(0, parseInt(q.get('limit') || '100', 10)) };
      if (path === '/github/status') return M.status;
      if (path === '/github/available-repos') {
        if (!M.status.signed_in) throw mockErr(401, 'Sign in with GitHub first');
        return M.available.map(function (r) {
          return { repo: r.repo, private: r.private, default_branch: r.default_branch, pushed_at: r.pushed_at, description: r.description, followed: M.status.repos.indexOf(r.repo) !== -1 };
        });
      }
      if (path === '/github/repos') {
        if (!M.status.signed_in) return [];
        return M.repos.map(function (r) {
          var cnt = mockRepoCounts(r.repo);
          return { repo: r.repo, default_branch: r.default_branch, private: r.private, head: mockRepoHead(r.repo), open_prs: cnt.open_prs, failing_today: cnt.failing_today, last_sync: r.last_sync };
        });
      }
      if (path === '/github/activity') {
        if (!M.status.connected) return { connected: false, branches: [], items: [] };
        var kind = q.get('kind') || 'all', branch = q.get('branch'), repo = q.get('repo'), failing = q.get('failing') === '1';
        var items = M.activity.filter(function (i) {
          if (kind === 'commits' && i.kind !== 'commit') return false;
          if (kind === 'pulls' && i.kind !== 'pull_request') return false;
          if (branch && i.branch !== branch) return false;
          if (repo && i.repo !== repo) return false;
          if (failing && i.ci !== 'failure') return false;
          return M.status.repos.indexOf(i.repo) !== -1;
        }).sort(function (a, b) { return a.time < b.time ? 1 : -1; });
        var branches = {};
        M.activity.forEach(function (i) { if (!repo || i.repo === repo) branches[i.branch] = true; });
        return { connected: true, branches: Object.keys(branches).sort(), items: items };
      }
      if (path === '/runs') {
        var st = q.get('status'), rp = q.get('repo');
        var list = M.runs.slice().sort(function (a, b) { return b.created_at < a.created_at ? -1 : 1; });
        if (st) list = list.filter(function (r) { return r.status === st; });
        if (rp) list = list.filter(function (r) { return r.trigger && r.trigger.repo === rp; });
        return list.slice(0, parseInt(q.get('limit') || '50', 10)).map(mockSummary);
      }
      if (seg[0] === 'runs' && seg.length === 2) {
        var r = mockFindRun(seg[1]);
        var run = mockSummary(r);
        run.error = r.error;
        return { run: run, steps: r.steps, actions: r.actions, timeline: r.timeline };
      }
      throw mockErr(404, 'Not found: ' + path);
    }

    if (method === 'DELETE') {
      if (seg[0] === 'github' && seg[1] === 'repos' && seg.length >= 4) {
        var name = seg[2] + '/' + seg.slice(3).join('/');
        var before = M.repos.length;
        M.repos = M.repos.filter(function (x) { return x.repo !== name; });
        if (M.repos.length === before) throw mockErr(404, 'Repository ' + name + ' is not connected');
        M.status.repos = M.status.repos.filter(function (x) { return x !== name; });
        mockGhRefresh();
        return { ok: true };
      }
      throw mockErr(404, 'Not found: ' + path);
    }

    if (method !== 'POST') throw mockErr(405, 'Method not allowed');

    if (path === '/runs') {
      var nr = { id: uuid(), title: '', trigger_type: '', route: null, routed_by: null, route_confidence: null, status: 'received', summary: '', agents: [], created_at: iso(0), finished_at: null, text: null, trigger: {}, error: null, steps: [], actions: [], timeline: [] };
      if (body.scenario_id) {
        var sc = MOCK_SCENARIOS.filter(function (s) { return s.id === body.scenario_id; })[0];
        if (!sc) throw mockErr(404, 'Unknown scenario: ' + body.scenario_id);
        nr.title = sc.title + ' (demo)';
        nr.trigger_type = sc.trigger_type;
        nr.route = sc.trigger_type;
        nr.routed_by = 'rule';
        nr.summary = 'Agents are working…';
        nr.trigger = { source: 'scenario', scenario: sc.id, repo: M.status.repo, requested_by: body.requested_by || null };
        nr.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Received scenario ' + sc.id + '; routed by rule.' });
        mockStartAgents(nr, sc.agents);
        mockAudit('orchestrator', 'routed', sc.id + ' (rule)');
      } else if (body.text) {
        var text = String(body.text);
        nr.text = text;
        nr.title = text.length > 70 ? text.slice(0, 67) + '…' : text;
        nr.trigger_type = 'free_text';
        nr.trigger = { source: 'text', requested_by: body.requested_by || null };
        var guess = mockGuessRoute(text);
        nr.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Received a free-text incident.' });
        if (guess) {
          nr.route = guess;
          nr.routed_by = 'llm';
          nr.route_confidence = 0.82;
          nr.summary = 'LLM triage routed this to ' + guess + ' (82%). Agents are working…';
          mockStartAgents(nr, MOCK_ROUTES.filter(function (x) { return x.id === guess; })[0].agents);
          mockAudit('orchestrator', 'routed', 'free text → ' + guess + ' (llm 0.82)');
        } else {
          nr.status = 'needs_triage';
          nr.route_confidence = 0.35;
          nr.summary = 'LLM triage was not confident enough (35%) to pick a route.';
          nr.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'LLM triage below threshold — asking a human.' });
          mockAudit('orchestrator', 'needs_triage', 'free text incident: confidence 0.35');
          mockAlert('info', 'Run needs triage', nr.title, 'orchestrator', '#/runs/' + nr.id, false);
        }
      } else {
        throw mockErr(422, 'Provide scenario_id, text, or trigger_type');
      }
      M.runs.push(nr);
      return { run_id: nr.id };
    }

    if (seg[0] === 'runs' && seg[2] === 'triage') {
      var tr = mockFindRun(seg[1]);
      if (tr.status !== 'needs_triage') throw mockErr(409, 'Run is not waiting for triage (status: ' + tr.status + ')');
      var route = MOCK_ROUTES.filter(function (x) { return x.id === body.route; })[0];
      if (!route) throw mockErr(422, 'Unknown route: ' + body.route);
      if (!body.decided_by) throw mockErr(422, 'decided_by is required');
      tr.route = route.id;
      tr.routed_by = 'human';
      tr.summary = 'Routed by ' + body.decided_by + ' to ' + route.id + '. Agents are working…';
      tr.timeline.push({ time: iso(0), kind: 'audit', actor: body.decided_by, text: 'Triage: routed to ' + route.id + '.' });
      mockAudit('orchestrator', 'routed', 'triage by ' + body.decided_by + ' → ' + route.id + ' (human)');
      mockStartAgents(tr, route.agents);
      return { ok: true };
    }

    if (seg[0] === 'approvals' && (seg[2] === 'approve' || seg[2] === 'reject')) {
      var found = null;
      M.runs.forEach(function (r) { r.actions.forEach(function (a) { if (a.approval && a.approval.id === seg[1]) found = { run: r, act: a }; }); });
      if (!found) throw mockErr(404, 'Approval not found');
      var ap = found.act.approval;
      var who = String(body.decided_by || '').trim();
      if (!who) throw mockErr(422, 'decided_by is required');
      if (ap.status === 'expired' || (ap.status === 'pending' && Date.parse(ap.expires_at) < Date.now())) { ap.status = 'expired'; throw mockErr(410, 'This approval expired ' + relTime(ap.expires_at) + '.'); }
      if (ap.status !== 'pending') throw mockErr(409, 'Approval already ' + ap.status + '.');
      if (who === ap.requested_by) throw mockErr(403, 'Self-approval is not allowed: ' + who + ' requested this action.');
      if (ap.approved_by.indexOf(who) !== -1) throw mockErr(409, who + ' has already voted on this approval.');
      var run2 = found.run, act = found.act;
      if (seg[2] === 'reject') {
        ap.status = 'rejected';
        act.decision = 'rejected';
        act.decided_by = who;
        act.execution = 'skipped';
        act.execution_result = { message: 'Not executed: rejected by ' + who + '.' };
        run2.timeline.push({ time: iso(0), kind: 'audit', actor: who, text: 'Rejected ' + act.action_type + '.' });
        mockAudit('guardrail', 'rejected', act.action_type + ' rejected by ' + who);
      } else {
        ap.approved_by.push(who);
        run2.timeline.push({ time: iso(0), kind: 'audit', actor: who, text: 'Approved ' + act.action_type + ' (' + ap.approved_by.length + '/' + ap.required_approvals + ').' });
        mockAudit('guardrail', 'vote', act.action_type + ' approved by ' + who + ' (' + ap.approved_by.length + '/' + ap.required_approvals + ')');
        if (ap.approved_by.length >= ap.required_approvals) {
          ap.status = 'approved';
          act.decision = 'approved';
          act.decided_by = who;
          act.execution = 'dry_run';
          act.execution_result = { message: act._dry || 'Dry run completed.' };
          run2.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Executed ' + act.action_type + ' as a dry run.' });
          mockAudit('orchestrator', 'executed', act.action_type + ' (dry run)');
        }
      }
      if (!run2.actions.some(function (a) { return a.decision === 'pending'; }) && run2.status === 'waiting_approval') {
        run2.status = 'completed';
        run2.finished_at = iso(0);
        run2.summary += ' All proposals decided.';
        run2.timeline.push({ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Run completed.' });
      }
      return { status: ap.status, approvals: ap.approved_by, required_approvals: ap.required_approvals };
    }

    if (path === '/alerts/read') {
      var ids = body.ids;
      M.alerts.forEach(function (a) { if (ids === 'all' || (Array.isArray(ids) && ids.indexOf(a.id) !== -1)) a.read = true; });
      return { ok: true };
    }

    if (path === '/github/connect') {
      var repoName = String(body.repo || '').trim();
      var token = String(body.token || '');
      if (!REPO_RE.test(repoName)) throw mockErr(422, 'Repository must look like owner/name');
      if (!token && !M.status.connected) throw mockErr(422, 'A token is required');
      if (token && !/^(ghp_|github_pat_)/.test(token)) throw mockErr(401, 'GitHub rejected the token (bad credentials)');
      if (/missing|notfound/i.test(repoName)) throw mockErr(404, 'Repository not found, or the token has no access to it');
      M.status.signed_in = true;
      M.status.auth = M.status.auth || 'token';
      M.status.login = M.status.login || 'hendch';
      M.status.error = null;
      if (M.status.repos.indexOf(repoName) === -1) {
        M.status.repos.unshift(repoName);
        M.repos.unshift({ repo: repoName, default_branch: 'main', private: false, last_sync: iso(0) });
      }
      mockGhRefresh();
      M.status.last_sync = iso(0);
      mockAlert('success', 'GitHub connected', 'Watching ' + M.status.repos.join(', ') + '.', 'github', '#/activity', false);
      return { ok: true };
    }
    if (path === '/github/disconnect') {
      mockSignOut();
      return { ok: true };
    }
    if (path === '/github/oauth/client') {
      var cid = String(body.client_id || '').trim();
      if (!/^[A-Za-z0-9._-]{8,}$/.test(cid)) throw mockErr(422, 'That does not look like an OAuth App Client ID (e.g. Ov23li…)');
      M.status.oauth_ready = true;
      return { ok: true };
    }
    if (path === '/github/oauth/start') {
      if (!M.status.oauth_ready) throw mockErr(409, 'No OAuth App Client ID configured');
      M.device = { startedAt: Date.now() };
      return { user_code: 'WDJB-MJHT', verification_uri: 'https://github.com/login/device', expires_in: 900, interval: 5 };
    }
    if (path === '/github/oauth/poll') {
      if (!M.device) return { state: 'idle' };
      if (Date.now() - M.device.startedAt < 9000) return { state: 'pending' };
      M.device = null;
      mockSignIn('oauth');
      return { state: 'connected', login: M.status.login };
    }
    if (path === '/github/token') {
      var tk = String(body.token || '').trim();
      if (!tk) throw mockErr(422, 'A token is required');
      if (!/^(ghp_|github_pat_)/.test(tk)) throw mockErr(401, 'GitHub rejected the token (bad credentials)');
      if (/nopermission/i.test(tk)) throw mockErr(403, 'The token is missing read access to Actions and Checks');
      mockSignIn('token');
      return { ok: true, login: M.status.login };
    }
    if (path === '/github/sync') {
      if (!M.status.connected) throw mockErr(409, 'GitHub is not connected');
      M.status.last_sync = iso(0);
      M.repos.forEach(function (r) { r.last_sync = iso(0); });
      return { ok: true };
    }
    if (path === '/github/settings') {
      if (typeof body.auto_investigate === 'boolean') M.status.auto_investigate = body.auto_investigate;
      return M.status;
    }
    if (path === '/github/repos') {
      var nm = String(body.repo || '').trim();
      if (!M.status.signed_in) throw mockErr(409, 'Sign in with GitHub first');
      if (!REPO_RE.test(nm)) throw mockErr(422, 'Repository must look like owner/name');
      if (/missing|notfound/i.test(nm)) throw mockErr(404, 'Repository not found, or the token has no access to it');
      if (M.status.repos.indexOf(nm) !== -1) throw mockErr(409, nm + ' is already connected');
      var item = { repo: nm, default_branch: 'main', private: false, last_sync: iso(0) };
      M.repos.push(item);
      M.status.repos.push(nm);
      mockGhRefresh();
      M.status.last_sync = iso(0);
      return { repo: nm, default_branch: 'main', private: false, head: null, open_prs: 0, failing_today: 0, last_sync: item.last_sync };
    }
    if (path === '/github/investigate') {
      var target = M.activity.filter(function (i) {
        if (body.repo && i.repo !== body.repo) return false;
        return body.kind === 'pull_request' ? i.kind === 'pull_request' && i.number === Number(body.number) : i.kind === 'commit' && i.sha === body.sha;
      })[0];
      if (!target) throw mockErr(404, 'Nothing to investigate for that ' + (body.kind === 'pull_request' ? 'pull request' : 'commit'));
      if (target.run_id) return { run_id: target.run_id };
      var label = target.kind === 'pull_request' ? 'PR #' + target.number : 'commit ' + target.short_sha;
      var ir = { id: uuid(), title: 'Investigate failing ' + label + ': ' + target.title, trigger_type: 'build_failed', route: 'build_failed', routed_by: 'rule', route_confidence: null,
        status: 'received', summary: 'Agents are working…', agents: [], created_at: iso(0), finished_at: null, text: null,
        trigger: target.kind === 'pull_request' ? { source: 'github', pr: target.number, repo: target.repo, html_url: target.html_url } : { source: 'github', sha: target.sha, repo: target.repo, html_url: target.html_url },
        error: null, steps: [], actions: [], timeline: [{ time: iso(0), kind: 'event', actor: 'orchestrator', text: 'Investigation of ' + target.repo + ' ' + label + ' requested.' }] };
      mockStartAgents(ir, ['diagnostic_rca', 'dependency_resolver']);
      M.runs.push(ir);
      target.run_id = ir.id;
      mockAudit('orchestrator', 'routed', 'investigate ' + label + ' → build_failed (rule)');
      return { run_id: ir.id };
    }
    throw mockErr(404, 'Not found: ' + path);
  }

  // ===========================================================================
  // 9. Boot
  // ===========================================================================

  function boot() {
    applyTheme(state.themePref);
    if (lsGet(LS.collapsed) === '1') document.body.classList.add('sb-collapsed');
    document.documentElement.classList.remove('pre-collapsed');
    setAnalyticsOpen(lsGet(LS.analytics) === '1' || /^#\/analytics/.test(window.location.hash));
    if (/Mac|iPhone|iPad/.test(navigator.platform || '')) $('kbd-hint').textContent = '⌘K';
    if (MOCK) $('mock-chip').hidden = false;
    renderUserRow();
    window.addEventListener('hashchange', onRoute);
    if (!window.location.hash || window.location.hash === '#' || window.location.hash === '#/') window.location.replace('#/home');
    onRoute();
    setInterval(function () {
      if (document.hidden) return;
      if (!state.inFlight) tick();
    }, POLL_MS);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
