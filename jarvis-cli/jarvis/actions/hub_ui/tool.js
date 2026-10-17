"use strict";
// Menu > Downloads (plan section 11, rework 17.1: the Scheduled panel's three panes).  `host` is provided by Jarvis; this file is run as the body
// of an async function, so a top-level `return` is legal.  host.root is a shadow root: query it,
// never `document`.  Only tools of download_hub.py can be run (host.runTool is checked server-side).
//
// SAFETY: every string that came from a download (title, message, error, a provider's detail,
// a file name ...) is untrusted.  It is only ever put on the page with textContent, never as
// markup; the only attribute built from provider data is an anchor's href, and the hub already
// dropped any scheme the owner hasn't allowed (https and steam by default).
//
// SHAPE: the first half is PURE (no DOM): formatting, the bar's look per state, which buttons
// a row offers, filtering, sorting, grouping.  The test (tests/verify_hub_panel.js) loads this
// file with host.__hubPureOnly = true and gets those helpers back.

// ============================================================================== pure helpers

const ACTIVE = new Set(["probing", "connecting", "downloading", "postprocessing"]);
const FINAL = new Set(["done", "failed", "cancelled"]);
const STATE_RANK = {
  downloading: 0, probing: 0, connecting: 0, postprocessing: 0, stalled: 1, queued: 2,
  paused: 3, failed: 4, done: 5, cancelled: 6, unknown: 7,
};
const KIND_ICON = {
  video: "\u25B6", audio: "\u266A", file: "\u25A4", torrent: "\u21C5", game: "\u25C6", other: "\u2022",
};
const APPROX = "\u2248 ";            // "≈ ": the hub estimated this number itself
const DOT = " \u00B7 ";

const VIEWS = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "waiting", label: "Waiting" },
  { id: "failed", label: "Failed" },
  { id: "done", label: "Finished" },
  { id: "stale", label: "Not reporting" },
];

const ACTION_DEFS = {
  pause: { label: "Pause", tool: "dl_pause" },
  resume: { label: "Resume", tool: "dl_resume" },
  retry: { label: "Retry", tool: "dl_retry" },
  open: { label: "Open", tool: "dl_open" },
  reveal: { label: "Show in folder", tool: "dl_open" },
  cancel: { label: "Cancel", tool: "dl_cancel", danger: true },
};
const ACTION_ORDER = ["pause", "resume", "retry", "open", "reveal", "cancel"];

function num(v) {
  return typeof v === "number" && isFinite(v) ? v : null;
}

function clampPct(v) {
  const n = num(v);
  return n === null ? null : Math.max(0, Math.min(100, n));
}

function fmtBytes(n) {
  n = num(n);
  if (n === null || n < 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? String(Math.round(n)) : n.toFixed(n >= 100 ? 0 : 1)) + " " + units[i];
}

function fmtSpeed(bps) {
  const n = num(bps);
  return n === null || n <= 0 ? "" : fmtBytes(n) + "/s";
}

function fmtEta(s) {
  s = num(s);
  if (s === null || s < 0) return "";
  s = Math.round(s);
  if (s < 60) return s + " s";
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min";
  const m = Math.floor((s % 3600) / 60);
  return Math.floor(s / 3600) + " h " + (m < 10 ? "0" : "") + m + " min";
}

function fmtAgo(s) {
  s = num(s);
  if (s === null || s < 0) return "a while ago";
  if (s < 90) return Math.round(s) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}

function derivedHas(row, key) {
  return Array.isArray(row.derived) && row.derived.indexOf(key) !== -1;
}

function isStale(row) {
  return !!row.stale && !FINAL.has(row.state);
}

function percentText(row) {
  const p = clampPct(row.percent);
  return p === null ? "" : (derivedHas(row, "percent") ? APPROX : "") + Math.round(p) + "%";
}

function speedText(row) {
  const t = fmtSpeed(row.speed_bps);
  return t ? (derivedHas(row, "speed") ? APPROX : "") + t : "";
}

function etaText(row) {
  const t = fmtEta(row.eta_s);
  return t ? (derivedHas(row, "eta") ? APPROX : "") + "ETA " + t : "";
}

function sizeText(row) {
  const done = fmtBytes(row.bytes_done);
  const total = fmtBytes(row.bytes_total);
  if (done && total) return done + " / " + total;
  return total || done || "";
}

// The first line on the right of a row: what is happening, in words (never colour alone).
function statusText(row) {
  const s = row.state;
  if (isStale(row)) return "No update " + fmtAgo(row.age_s);
  const parts = [];
  switch (s) {
    case "queued":
      return row.retry_in_s ? "Waiting to retry in " + row.retry_in_s + " s" : "Queued";
    case "probing": return "Checking\u2026";
    case "connecting": return "Connecting\u2026";
    case "downloading":
      [percentText(row), speedText(row), etaText(row)].forEach((t) => { if (t) parts.push(t); });
      return parts.length ? parts.join(DOT) : "Downloading";
    case "postprocessing": {
      const label = row.phase ? row.phase : "Processing";
      const idx = row.phase_index && row.phase_count ? " (" + row.phase_index + "/" + row.phase_count + ")" : "";
      return label.charAt(0).toUpperCase() + label.slice(1) + idx;
    }
    case "paused": return "Paused" + (percentText(row) ? " at " + percentText(row) : "");
    case "stalled": return "Stalled" + (row.retry_in_s ? " \u2014 retry in " + row.retry_in_s + " s" : "");
    case "failed": return "Failed" + (percentText(row) ? " at " + percentText(row) : "");
    case "cancelled": return "Cancelled";
    case "done": return "Done";
    default: return "Unknown";
  }
}

// The bar's look per state (plan 11.4): kind drives the CSS class, pct the fill width.
function barLook(row) {
  const pct = clampPct(row.percent);
  if (isStale(row)) return { kind: "stale", pct: pct === null ? 100 : pct };
  switch (row.state) {
    case "done": return { kind: "done", pct: 100 };
    case "failed": return { kind: "failed", pct: pct === null ? 0 : pct };
    case "cancelled": return { kind: "cancelled", pct: 0 };
    case "queued": return { kind: "queued", pct: 0 };
    case "paused": return { kind: "paused", pct: pct === null ? 0 : pct };
    case "stalled": return { kind: "stalled", pct: pct === null ? 0 : pct };
    case "unknown": return { kind: "unknown", pct: pct === null ? 0 : pct };
    case "postprocessing": return { kind: "striped", pct: 100 };
    default: return pct === null ? { kind: "indeterminate", pct: 35 } : { kind: "determinate", pct: pct };
  }
}

// Which buttons a row offers: exactly the capabilities the hub says it has, in a fixed order.
function rowActions(row) {
  const caps = Array.isArray(row.capabilities) ? row.capabilities : [];
  return ACTION_ORDER.filter((v) => caps.indexOf(v) !== -1).map((v) => Object.assign({ verb: v }, ACTION_DEFS[v]));
}

function sourceKey(row) {
  return row.owner === "hub" ? "hub:" + (row.engine || "") : "provider:" + (row.provider || "");
}

function sourceLabel(row) {
  if (row.owner === "hub") return String(row.engine || "hub").toUpperCase();
  return row.provider_label || row.provider || "?";
}

function viewMatches(view, row) {
  const stale = isStale(row);
  switch (view) {
    case "all": return true;
    case "active": return ACTIVE.has(row.state) && !stale;
    case "waiting": return (row.state === "queued" || row.state === "paused" || row.state === "stalled") && !stale;
    case "failed": return row.state === "failed" || row.state === "cancelled";
    case "done": return row.state === "done";
    case "stale": return stale;
    default: return true;
  }
}

function viewCounts(rows) {
  const out = {};
  VIEWS.forEach((v) => { out[v.id] = rows.filter((r) => viewMatches(v.id, r)).length; });
  return out;
}

function sourceList(rows) {
  const seen = {};
  const list = [];
  rows.forEach((r) => {
    const key = sourceKey(r);
    if (!seen[key]) { seen[key] = { key: key, label: sourceLabel(r), count: 0 }; list.push(seen[key]); }
    seen[key].count++;
  });
  return list.sort((a, b) => a.label.localeCompare(b.label));
}

function filterRows(rows, f) {
  const q = String(f.search || "").trim().toLowerCase();
  return rows.filter((r) => {
    if (!viewMatches(f.view || "all", r)) return false;
    if (f.source && sourceKey(r) !== f.source) return false;
    if (q) {
      const hay = ((r.title || "") + " " + (r.subtitle || "") + " " + (r.message || "") + " " + (r.error || "")).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    return true;
  });
}

// Running first, then stalled, queued, paused, failed, done, cancelled; newest first inside each.
function sortRows(rows) {
  return rows.slice().sort((a, b) => {
    const ra = isStale(a) ? 1 : (STATE_RANK[a.state] === undefined ? 9 : STATE_RANK[a.state]);
    const rb = isStale(b) ? 1 : (STATE_RANK[b.state] === undefined ? 9 : STATE_RANK[b.state]);
    if (ra !== rb) return ra - rb;
    return (b.created_at || 0) - (a.created_at || 0);
  });
}

// mode: "none" | "source" | "group".  Returns [{key, title, meta, rows}] in display order.
function groupRows(rows, mode) {
  if (mode !== "source" && mode !== "group") return [{ key: "", title: "", meta: "", rows: rows }];
  const sections = [];
  const index = {};
  const loose = [];
  rows.forEach((r) => {
    let key;
    let title;
    if (mode === "source") { key = sourceKey(r); title = sourceLabel(r); }
    else if (r.group_id) { key = "g:" + r.group_id; title = r.group_title || "Batch"; }
    else { loose.push(r); return; }
    if (!index[key]) { index[key] = { key: key, title: title, meta: "", rows: [] }; sections.push(index[key]); }
    index[key].rows.push(r);
  });
  sections.forEach((s) => {
    const done = s.rows.filter((r) => r.state === "done").length;
    s.meta = s.rows.length > 1 || mode === "group" ? done + "/" + s.rows.length + " done" : "";
  });
  if (loose.length) sections.push({ key: "loose", title: "Single downloads", meta: "", rows: loose });
  return sections;
}

// Arrow-key movement through the rows as displayed.
function moveSelection(ids, current, delta) {
  if (!ids.length) return null;
  const i = ids.indexOf(current);
  if (i === -1) return ids[delta > 0 ? 0 : ids.length - 1];
  return ids[Math.max(0, Math.min(ids.length - 1, i + delta))];
}

// Links the add box will send: http(s) links, magnet links and 40-hex infohashes, at most 50.
function extractInputs(text) {
  const out = [];
  String(text || "").split(/\s+/).forEach((t) => {
    if (!t || out.length >= 50 || out.indexOf(t) !== -1) return;
    if (/^(https?:\/\/|magnet:\?)\S+$/i.test(t) || /^[0-9a-f]{40}$/i.test(t)) out.push(t);
  });
  return out;
}

// The line under the add box, from dl_add's dry_run decisions.  [text, isProblem]
function hintFromDecisions(decisions) {
  const list = Array.isArray(decisions) ? decisions : [];
  if (!list.length) return ["", false];
  const bad = list.find((d) => !d.ok);
  if (bad) return [bad.problem || "I can't download that", true];
  if (list.length === 1) return ["\u2192 " + String(list[0].engine || "").toUpperCase() + ": " + (list[0].reason || ""), false];
  return ["\u2192 " + list.length + " links, queued as one batch", false];
}

// ---- helpers added for the three-pane layout (rework 17.1); pure, like everything above

// The one word that picks a row's colour (the dot and the left edge).  A silent provider row
// is "stale", whatever state it last reported.
function toneOf(row) {
  if (isStale(row)) return "stale";
  switch (row.state) {
    case "downloading": case "probing": case "connecting": case "postprocessing": return "active";
    case "queued": return "queued";
    case "paused": return "paused";
    case "stalled": return "stalled";
    case "failed": return "failed";
    case "done": return "done";
    case "cancelled": return "cancelled";
    default: return "unknown";
  }
}

// Counts for the Overview pane, by tone.
function overviewStats(rows) {
  const out = { total: rows.length, active: 0, queued: 0, paused: 0, stalled: 0, failed: 0, done: 0, cancelled: 0, stale: 0, unknown: 0 };
  rows.forEach((r) => { const t = toneOf(r); if (out[t] !== undefined) out[t]++; });
  return out;
}

// What needs a look: failed, stalled and silent rows, worst first.
function needsAttention(rows, limit) {
  const bad = rows.filter((r) => { const t = toneOf(r); return t === "failed" || t === "stalled" || t === "stale"; });
  return sortRows(bad).slice(0, limit || 6);
}

function runningNow(rows, limit) {
  return sortRows(rows.filter((r) => toneOf(r) === "active")).slice(0, limit || 5);
}

// The line at the top of the panel (the Scheduled panel's subtitle).  tone: "" | "ok" | "error"
function sublineText(sum, err) {
  if (err) return { text: "Can't reach the download hub", tone: "error" };
  if (!sum) return { text: "reading downloads\u2026", tone: "" };
  const c = sum.counts || {};
  const speed = (c.speed_hub_bps || 0) + (c.speed_other_bps || 0);
  const parts = [(c.active || 0) + " active", (c.queued || 0) + " queued"];
  if (c.failed) parts.push(c.failed + " failed");
  if (c.stale) parts.push(c.stale + " not reporting");
  parts.push("\u2193 " + (fmtSpeed(speed) || "idle"));
  const alive = !!(sum.daemon && sum.daemon.alive);
  parts.push(alive ? "hub running" : "hub NOT running");
  return { text: parts.join(DOT), tone: !alive ? "error" : (c.active ? "ok" : "") };
}

// "3 of 12" when a filter hides some, else "12".
function countText(shown, total) {
  return shown === total ? String(total) : shown + " of " + total;
}

const pure = {
  ACTIVE, FINAL, VIEWS, KIND_ICON, fmtBytes, fmtSpeed, fmtEta, fmtAgo, percentText, speedText, etaText,
  sizeText, statusText, barLook, rowActions, sourceKey, sourceLabel, viewMatches, viewCounts, sourceList,
  filterRows, sortRows, groupRows, moveSelection, extractInputs, hintFromDecisions, isStale,
  toneOf, overviewStats, needsAttention, runningNow, sublineText, countText,
};
if (host && host.__hubPureOnly) return pure;

// ============================================================================== the panel

const root = host.root;
const doc = root.ownerDocument || document;
const $ = (sel) => root.querySelector(sel);

const S = {
  rows: [], summary: null, selected: null, view: "all", source: "", search: "", groupBy: "none",
  detail: null, busy: false, timer: 0, closed: false, lastError: "", hold: 0, holdTimer: 0,
  pendingSelect: "", sig: {},
};
let hintTimer = 0;
let hintSeq = 0;

function el(tag, opts, kids) {
  const n = doc.createElement(tag);
  const o = opts || {};
  if (o.cls) n.className = o.cls;
  if (o.text !== undefined && o.text !== null) n.textContent = String(o.text);
  if (o.title) n.title = o.title;
  if (o.attrs) Object.keys(o.attrs).forEach((k) => n.setAttribute(k, String(o.attrs[k])));
  if (o.on) Object.keys(o.on).forEach((k) => n.addEventListener(k, o.on[k]));
  (kids || []).forEach((k) => { if (k) n.appendChild(k); });
  return n;
}

function toast(msg, level) {
  try { host.toast(String(msg), level || "info"); } catch (e) { /* the host may already be closing */ }
}

function message(err) {
  return String((err && err.message) || err || "something went wrong");
}

// Rebuild a part of the panel only when what it shows has changed: no flicker, and a button
// the pointer is on doesn't vanish under it on every poll.
function changed(key, value) {
  const v = typeof value === "string" ? value : JSON.stringify(value);
  if (S.sig[key] === v) return false;
  S.sig[key] = v;
  return true;
}

function savePrefs() {
  try {
    host.storage.set("prefs", JSON.stringify({ view: S.view, source: S.source, groupBy: S.groupBy })).catch(() => {});
  } catch (e) { /* storage is a convenience */ }
}

async function loadPrefs() {
  try {
    const raw = await host.storage.get("prefs");
    const p = raw ? JSON.parse(raw) : {};
    if (VIEWS.some((v) => v.id === p.view)) S.view = p.view;
    if (typeof p.source === "string") S.source = p.source;
    if (["none", "source", "group"].indexOf(p.groupBy) !== -1) S.groupBy = p.groupBy;
  } catch (e) { /* defaults */ }
}

function selectedRow() {
  return S.rows.find((r) => r.id === S.selected) || null;
}

function visibleRows() {
  return sortRows(filterRows(S.rows, { view: S.view, source: S.source, search: S.search }));
}

// ---- top strip

function renderTop() {
  const line = sublineText(S.summary, S.lastError);
  const n = $("#subline");
  n.textContent = line.text;
  n.className = "top__line" + (line.tone ? " is-" + line.tone : "");
  const st = overviewStats(S.rows);
  $("#pauseAll").disabled = !(st.active + st.queued + st.stalled);
  $("#resumeAll").disabled = !st.paused;
}

function renderBanner() {
  const b = $("#banner");
  const sum = S.summary;
  if (S.lastError) {
    b.className = "banner bad";
    b.textContent = "Can't reach the download hub: " + S.lastError;
    b.hidden = false;
  } else if (sum && sum.daemon && !sum.daemon.alive) {
    b.className = "banner";
    b.textContent = "The hub daemon isn't running, so queued downloads won't start. Open Menu \u203A Daemons and start \u201CDownload hub\u201D.";
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

// ---- left pane

function chip(label, n, active, flag, onClick) {
  return el("button", {
    cls: "chip" + (active ? " is-active" : "") + (flag && n ? " chip--flag" : ""),
    attrs: { type: "button", "aria-pressed": active ? "true" : "false" }, on: { click: onClick },
  }, [el("span", { text: label }), el("span", { cls: "chip__n", text: n })]);
}

function renderChips() {
  const counts = viewCounts(S.rows);
  const sources = sourceList(S.rows);
  if (!changed("chips", [counts, sources, S.view, S.source])) return;
  const vbox = $("#viewChips");
  vbox.textContent = "";
  VIEWS.forEach((v) => {
    vbox.appendChild(chip(v.label, counts[v.id], S.view === v.id, v.id === "failed" || v.id === "stale", () => {
      S.view = v.id; savePrefs(); render();
    }));
  });
  const sbox = $("#sourceChips");
  sbox.textContent = "";
  if (sources.length > 1) {
    sources.forEach((src) => {
      sbox.appendChild(chip(src.label, src.count, S.source === src.key, false, () => {
        S.source = S.source === src.key ? "" : src.key;
        savePrefs();
        render();
      }));
    });
  }
}

function renderBar(row, big) {
  const look = barLook(row);
  const fill = el("div", { cls: "bar-fill" });
  fill.style.width = look.pct + "%";
  return el("div", { cls: "bar-track " + look.kind + (big ? " big" : ""), attrs: { "aria-hidden": "true" } }, [fill]);
}

function renderRow(row) {
  const tone = toneOf(row);
  const meta = [el("span", { cls: "tag", text: sourceLabel(row) })];
  if (row.read_only) meta.push(el("span", { cls: "tag tag--ro", text: "read-only", title: "This tool reports its progress but can't be controlled from here" }));
  meta.push(el("span", { cls: "st" + (row.error ? " bad" : ""), text: statusText(row) }));
  const size = sizeText(row);
  if (size) meta.push(el("span", { text: size }));
  return el("div", {
    cls: "row tone-" + tone + " state-" + row.state,
    attrs: { role: "option", "aria-selected": row.id === S.selected ? "true" : "false", "data-id": row.id },
    on: { click: () => select(row.id) },
  }, [
    el("div", { cls: "row__top" }, [
      el("span", { cls: "dot tone-" + tone, attrs: { "aria-hidden": "true" } }),
      el("span", { cls: "row__title", text: row.title || row.id, title: row.title || row.id }),
      el("span", { cls: "row__kind", text: KIND_ICON[row.kind] || KIND_ICON.other, attrs: { "aria-hidden": "true" } }),
    ]),
    renderBar(row, false),
    el("div", { cls: "row__meta" }, meta),
  ]);
}

function sectionHeader(section) {
  const kids = [el("span", { cls: "section__t", text: section.title + (section.meta ? DOT + section.meta : "") })];
  // Batch headers get the group-wide controls: the same tools, aimed at the whole batch.
  if (S.groupBy === "group" && /^g:/.test(section.key)) {
    const gid = section.key.slice(2);
    const acts = el("span", { cls: "section__acts" });
    [["pause", "Pause"], ["resume", "Resume"], ["retry", "Retry"]].forEach((a) => {
      acts.appendChild(el("button", { cls: "btn", text: a[1], attrs: { type: "button" }, on: { click: () => groupAction(gid, a[0], a[1]) } }));
    });
    kids.push(acts);
  }
  return el("div", { cls: "section" }, kids);
}

function renderList() {
  const rows = visibleRows();
  $("#count").textContent = countText(rows.length, S.rows.length);
  if (!changed("list", [rows, S.selected, S.groupBy])) return;
  const list = $("#list");
  const scroll = list.scrollTop;
  list.textContent = "";
  if (!rows.length) {
    list.appendChild(el("div", {
      cls: "empty",
      text: S.rows.length ? "Nothing matches this filter." : "No downloads yet. Paste a link in the form, or ask Jarvis to download a file.",
    }));
    return;
  }
  groupRows(rows, S.groupBy).forEach((section) => {
    if (section.title) list.appendChild(sectionHeader(section));
    section.rows.forEach((r) => list.appendChild(renderRow(r)));
  });
  list.scrollTop = scroll;
}

// ---- middle pane

function pair(dl, label, value) {
  if (value === null || value === undefined || value === "") return;
  dl.appendChild(el("div", { cls: "drow" }, [el("dt", { text: label }), el("dd", { text: value })]));
}

function clock(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "";
}

function note(kind, title, text) {
  return el("div", { cls: "note" + (kind ? " note--" + kind : "") }, [el("strong", { text: title }), el("span", { text: text })]);
}

function buildDetail(row, full) {
  const box = $("#detail");
  box.textContent = "";
  const tone = toneOf(row);

  box.appendChild(el("div", { cls: "detail__title", text: row.title || row.id }));
  const badges = [el("span", { cls: "dot tone-" + tone, attrs: { "aria-hidden": "true" } }), el("span", { text: statusText(row) }), el("span", { cls: "tag", text: sourceLabel(row) })];
  if (row.read_only) badges.push(el("span", { cls: "tag tag--ro", text: "read-only" }));
  box.appendChild(el("div", { cls: "detail__status" }, badges));

  const bar = renderBar(row, true);
  bar.classList.add("detail__bar");
  box.appendChild(bar);
  const right = [speedText(row), etaText(row)].filter(Boolean).join(DOT);
  box.appendChild(el("div", { cls: "detail__nums" }, [
    el("span", { text: [percentText(row), sizeText(row)].filter(Boolean).join(DOT) }),
    el("span", { text: right }),
  ]));

  const actions = rowActions(row);
  if (actions.length) {
    box.appendChild(el("div", { cls: "detail__actions" }, actions.map((a) => el("button", {
      cls: "btn btn--sm" + (a.danger ? " btn--danger" : ""), text: a.label, attrs: { type: "button" },
      on: { click: () => runAction(row, a) },
    }))));
  }

  if (row.error) box.appendChild(note("bad", "It failed", row.error));
  if (isStale(row)) box.appendChild(note("warn", "Not reporting", "Nothing has been heard from this download " + fmtAgo(row.age_s) + ". It may have stopped, or the tool that runs it may be closed. It is not marked failed."));
  if (row.read_only) box.appendChild(note("", "Read-only", "This tool reports its progress but can't be paused or cancelled from here."));
  if (row.pending) box.appendChild(note("", "Waiting for the hub", "\u201C" + row.pending + "\u201D has been requested and will be applied in a moment."));

  const dl = el("dl", { cls: "detail__rows" });
  pair(dl, "Source", sourceLabel(row));
  pair(dl, "From", row.source);
  pair(dl, "Saved to", row.dest);
  pair(dl, "Size", sizeText(row));
  pair(dl, "Speed", speedText(row));
  pair(dl, "ETA", etaText(row).replace(/^ETA /, "").replace(APPROX + "ETA ", APPROX));
  pair(dl, "Peers", row.peers);
  pair(dl, "Tries", row.attempts ? row.attempts : "");
  pair(dl, "Added", clock(row.created_at));
  pair(dl, "Finished", clock(row.finished_at));
  pair(dl, "Batch", row.group_title);
  pair(dl, "Note", row.error ? "" : row.message);
  (full && full.detail ? full.detail : []).forEach((d) => pair(dl, d.label, d.value));
  box.appendChild(dl);

  const links = row.links || [];
  const urls = links.filter((l) => l.kind === "url");
  if (urls.length) {
    const wrap = el("div", { cls: "links" });
    urls.forEach((l) => {
      wrap.appendChild(el("a", { text: l.label, attrs: { href: l.target, target: "_blank", rel: "noopener noreferrer" } }));
    });
    box.appendChild(wrap);
  }
  // kind "tool" links are not rendered: a provider's own control tools live in ITS file, which
  // this panel may not run, and a provider's text must never choose which of the hub's tools a
  // click triggers.  Phase 5 wires them through the hub's own tools.

  const children = full && full.children ? full.children : [];
  if (children.length) {
    box.appendChild(el("div", { cls: "h5", text: "Files (" + children.length + ")" }));
    children.slice(0, 40).forEach((c) => {
      const bits = [c.name];
      if (typeof c.percent === "number") bits.push(Math.round(c.percent) + "%");
      if (c.state) bits.push(c.state);
      box.appendChild(el("div", { cls: "ev", text: bits.join(DOT) }));
    });
  }
  const events = full && full.events ? full.events : [];
  if (events.length) {
    box.appendChild(el("div", { cls: "h5", text: "Recent events" }));
    events.slice(-12).reverse().forEach((ev) => {
      box.appendChild(el("div", { cls: "ev " + (ev.level === "error" ? "error" : ""), text: new Date(ev.ts * 1000).toLocaleTimeString() + "  " + ev.message }));
    });
  }
}

function renderMid() {
  const row = selectedRow();
  $("#addForm").hidden = !!row;
  $("#detail").hidden = !row;
  $("#midTitle").textContent = row ? "Download" : "Add a download";
  if (!row) { S.sig.detail = ""; return; }
  const full = S.detail && S.detail.id === row.id ? S.detail.item : null;
  if (!changed("detail", [row, full && full.events, full && full.children, full && full.detail])) return;
  buildDetail(row, full);
}

// ---- right pane

function countRow(label, n, flag) {
  return el("div", { cls: "crow" + (flag ? " is-" + flag : "") }, [
    el("span", { cls: "crow__l", text: label }), el("span", { cls: "crow__n", text: n }),
  ]);
}

function pickRow(row, withBar) {
  const kids = [
    el("div", { cls: "pick-row__t", text: row.title || row.id }),
    el("div", { cls: "pick-row__s", text: statusText(row) }),
  ];
  if (withBar) kids.push(renderBar(row, false));
  return el("button", { cls: "pick-row", attrs: { type: "button", "data-id": row.id }, on: { click: () => select(row.id) } }, kids);
}

function renderSide() {
  const st = overviewStats(S.rows);
  const attention = needsAttention(S.rows);
  const running = runningNow(S.rows);
  if (!changed("side", [st, attention, running, S.summary, S.lastError])) return;
  const box = $("#side");
  box.textContent = "";
  const sum = S.summary || {};
  const c = sum.counts || {};

  const counts = el("div", { cls: "counts" });
  counts.appendChild(countRow("Active", st.active));
  counts.appendChild(countRow("Queued", st.queued));
  counts.appendChild(countRow("Paused", st.paused));
  counts.appendChild(countRow("Stalled", st.stalled, st.stalled ? "flag" : ""));
  counts.appendChild(countRow("Failed", st.failed, st.failed ? "bad" : ""));
  counts.appendChild(countRow("Finished", st.done));
  counts.appendChild(countRow("Not reporting", st.stale, st.stale ? "flag" : ""));
  box.appendChild(el("div", {}, [el("h4", { text: "Right now" }), counts]));

  const hubSpeed = c.speed_hub_bps || 0;
  const otherSpeed = c.speed_other_bps || 0;
  const speedBox = el("div", {}, [
    el("h4", { text: "Speed" }),
    el("div", { cls: "big-speed", text: "\u2193 " + (fmtSpeed(hubSpeed + otherSpeed) || "idle") }),
  ]);
  if (otherSpeed > 0) speedBox.appendChild(el("div", { cls: "small", text: "hub " + (fmtSpeed(hubSpeed) || "0") + DOT + "other tools " + fmtSpeed(otherSpeed) + " (not under hub control)" }));
  box.appendChild(speedBox);

  const free = sum.free_bytes;
  const storage = el("div", {}, [el("h4", { text: "Storage" })]);
  storage.appendChild(el("div", { text: free === null || free === undefined ? "free space unknown" : fmtBytes(free) + " free" }));
  if (sum.library_root) storage.appendChild(el("div", { cls: "small", text: sum.library_root }));
  box.appendChild(storage);

  const alive = !!(sum.daemon && sum.daemon.alive);
  const hubBox = el("div", {}, [el("h4", { text: "Hub" })]);
  hubBox.appendChild(el("div", { cls: "detail__status" }, [
    el("span", { cls: "dot tone-" + (S.lastError || !alive ? "failed" : "done"), attrs: { "aria-hidden": "true" } }),
    el("span", { text: S.lastError ? "not reachable" : (alive ? "daemon running" : "daemon not running") }),
  ]));
  if (!S.lastError && !alive) hubBox.appendChild(el("div", { cls: "small", text: "Start \u201CDownload hub\u201D in Menu \u203A Daemons so queued downloads can begin." }));
  box.appendChild(hubBox);

  const attn = el("div", {}, [el("h4", { text: "Needs attention" })]);
  if (attention.length) attention.forEach((r) => attn.appendChild(pickRow(r, false)));
  else attn.appendChild(el("div", { cls: "side-empty", text: "Nothing needs a look." }));
  box.appendChild(attn);

  const now = el("div", {}, [el("h4", { text: "Running now" })]);
  if (running.length) running.forEach((r) => now.appendChild(pickRow(r, true)));
  else now.appendChild(el("div", { cls: "side-empty", text: "Nothing is downloading." }));
  box.appendChild(now);

  const demo = el("div", {}, [
    el("h4", { text: "Try the panel" }),
    el("div", { cls: "small", text: "Fake downloads that show every look: climbing, no percent, stalled, failed, a silent source." }),
    el("div", { cls: "demo" }, [
      el("button", { cls: "btn btn--sm", text: "Start demo", attrs: { type: "button" }, on: { click: () => demoAction("start") } }),
      el("button", { cls: "btn btn--sm", text: "Clear demo", attrs: { type: "button" }, on: { click: () => demoAction("clear") } }),
    ]),
  ]);
  box.appendChild(demo);
}

function render() {
  if (S.hold && Date.now() < S.hold) { clearTimeout(S.holdTimer); S.holdTimer = setTimeout(render, 250); return; }   // don't rebuild under a click
  S.hold = 0;
  if (S.closed) return;
  renderTop();
  renderBanner();
  renderChips();
  renderList();
  renderMid();
  renderSide();
}

// ---- actions

async function callTool(name, args) {
  try {
    const r = await host.runTool(name, args || {});
    return r || {};
  } catch (err) {
    return { ok: false, error: message(err), declined: true };      // declined confirmations reject
  }
}

// One place that turns a control tool's answer into a corner message.
function reportControl(r, label) {
  if (r.declined) { toast("Cancelled", "info"); return; }
  if (r.ok === false) { toast(r.error || "That didn't work", "warn"); return; }
  const n = ((r.changed && r.changed.length) || 0) + ((r.pending && r.pending.length) || 0);
  toast(n > 1 ? label + ": " + n + " downloads" : label + "\u2026", "info");
}

async function runAction(row, action) {
  const args = action.tool === "dl_open" ? { id: row.id, action: action.verb } : { id: row.id };
  const r = await callTool(action.tool, args);
  if (r.declined) toast("Cancelled", "info");
  else if (r.ok === false) toast(r.error || "That didn't work", "warn");
  else if (r.pending && r.pending.length) toast(action.label + "\u2026", "info");
  schedule(300);
}

async function groupAction(gid, verb, label) {
  const r = verb === "pause" ? await callTool("dl_pause", { group: gid })
    : verb === "resume" ? await callTool("dl_resume", { group: gid })
      : await callTool("dl_retry", { group: gid });
  reportControl(r, label);
  schedule(300);
}

async function allAction(verb) {
  const btn = $(verb === "pause" ? "#pauseAll" : "#resumeAll");
  btn.disabled = true;
  const r = verb === "pause" ? await callTool("dl_pause", { all: true }) : await callTool("dl_resume", { all: true });
  reportControl(r, verb === "pause" ? "Pausing" : "Resuming");
  schedule(300);
}

async function demoAction(action) {
  const r = await callTool("hub_demo_provider", { action: action });
  if (r.declined) { toast("Cancelled", "info"); return; }
  if (r.ok === false) { toast(r.error || "That didn't work", "warn"); return; }
  if (r.note) toast(r.note, "warn");
  else toast(action === "start" ? "Demo downloads started" : "Demo downloads cleared", "success");
  schedule(300);
}

function select(id) {
  S.selected = id;
  S.detail = null;
  render();
  loadDetail();
}

function newDownload() {
  S.selected = null;
  S.detail = null;
  render();
  $("#addInput").focus();
}

async function loadDetail() {
  const id = S.selected;
  if (!id) return;
  const r = await callTool("dl_status", { id: id });
  if (r.ok && r.item && id === S.selected) {
    S.detail = { id: id, item: r.item, at: Date.now(), state: r.item.state };
    renderMid();
  }
}

// ---- polling: one dl_list call per round (it carries the totals too)

function schedule(delay) {
  clearTimeout(S.timer);
  if (S.closed) return;
  S.timer = setTimeout(poll, delay || (doc.hidden ? 8000 : 2500));
}

async function poll() {
  if (S.closed || S.busy) return;
  S.busy = true;
  try {
    const r = await host.runTool("dl_list", { limit: 500, summary: true });
    if (r && r.ok === false) throw new Error(r.error || "the hub didn't answer");
    S.rows = Array.isArray(r.items) ? r.items : [];
    if (r.summary) S.summary = r.summary;
    S.lastError = "";
    if (S.pendingSelect && S.rows.some((x) => x.id === S.pendingSelect)) { S.selected = S.pendingSelect; S.detail = null; }
    S.pendingSelect = "";
    if (S.selected && !S.rows.some((x) => x.id === S.selected)) { S.selected = null; S.detail = null; }
  } catch (err) {
    S.lastError = message(err);
  } finally {
    S.busy = false;
  }
  render();
  const row = selectedRow();
  if (row && (!S.detail || S.detail.id !== row.id || S.detail.state !== row.state || Date.now() - S.detail.at > 8000)) {
    loadDetail();
  }
  schedule();
}

// ---- the add form

function setHint(text, bad) {
  const h = $("#addHint");
  h.textContent = text;
  h.className = "hint" + (bad ? " bad" : "");
}

async function previewHint(urls) {
  const seq = ++hintSeq;
  const r = await callTool("dl_add", { urls: urls, dry_run: true });
  if (seq !== hintSeq) return;
  if (r.ok === false && !r.decisions) { setHint(r.error || "", true); return; }
  const h = hintFromDecisions(r.decisions);
  setHint(h[0], h[1]);
}

async function submitAdd(ev) {
  ev.preventDefault();
  const input = $("#addInput");
  const urls = extractInputs(input.value);
  if (!urls.length) { setHint("Paste a link that starts with http:// or https://", true); return; }
  const args = { urls: urls };
  const dest = $("#addDest").value.trim();
  const group = $("#addGroup").value.trim();
  if (dest) args.dest = dest;
  if (group) args.group = group;
  if ($("#addPaused").checked) args.start_paused = true;
  const btn = $("#addBtn");
  btn.disabled = true;
  try {
    const r = await callTool("dl_add", args);
    if (r.declined) { setHint("Cancelled", false); return; }
    if (r.ok) {
      const added = r.added || [];
      const dup = added.filter((a) => a.duplicate).length;
      toast("Queued " + r.count + (dup ? " (" + dup + " already in the list)" : ""), "success");
      if (r.note) toast(r.note, "warn");
      if (r.skipped && r.skipped.length) toast(r.skipped[0].problem, "warn");
      if (added.length === 1) S.pendingSelect = added[0].id;       // show the one you just added
      input.value = "";
      setHint("", false);
      schedule(200);
    } else {
      setHint(r.error || "That didn't work", true);
    }
  } finally {
    btn.disabled = false;
  }
}

// ---- wiring

$("#addForm").addEventListener("submit", submitAdd);
$("#addInput").addEventListener("input", () => {
  clearTimeout(hintTimer);
  const urls = extractInputs($("#addInput").value);
  if (!urls.length) { hintSeq++; setHint("", false); return; }
  hintTimer = setTimeout(() => previewHint(urls), 400);
});
$("#addInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#addForm").requestSubmit(); }
});
$("#search").addEventListener("input", (e) => { S.search = e.target.value; render(); });
$("#groupBy").addEventListener("change", (e) => { S.groupBy = e.target.value; savePrefs(); render(); });
$("#refresh").addEventListener("click", () => { S.lastError = ""; schedule(10); });
$("#newBtn").addEventListener("click", newDownload);
$("#pauseAll").addEventListener("click", () => allAction("pause"));
$("#resumeAll").addEventListener("click", () => allAction("resume"));

const list = $("#list");
// A poll must not rebuild the panes between pressing and releasing a button (the click would
// land on a node that no longer exists), so renders wait while a pointer is down.
["list", "mid", "side"].forEach((id) => {
  const pane = $("#" + id);
  pane.addEventListener("pointerdown", () => { S.hold = Date.now() + 1500; });
  pane.addEventListener("pointerup", () => { S.hold = 0; });
  pane.addEventListener("pointercancel", () => { S.hold = 0; });
});
list.addEventListener("keydown", (e) => {
  const rows = visibleRows();
  const ids = rows.map((r) => r.id);
  const current = rows.find((r) => r.id === S.selected);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = moveSelection(ids, S.selected, e.key === "ArrowDown" ? 1 : -1);
    if (next) { select(next); const n = list.querySelector('[data-id="' + String(next).replace(/"/g, "") + '"]'); if (n && n.scrollIntoView) n.scrollIntoView({ block: "nearest" }); }
    return;
  }
  if (!current) return;
  const actions = rowActions(current);
  const find = (v) => actions.find((a) => a.verb === v);
  if (e.key === " ") {
    e.preventDefault();
    const a = find("pause") || find("resume");
    if (a) runAction(current, a);
  } else if (e.key === "Enter") {
    const a = find("open");
    if (a) { e.preventDefault(); runAction(current, a); }
  } else if (e.key === "Delete") {
    const a = find("cancel");
    if (a) { e.preventDefault(); runAction(current, a); }
  }
});
root.addEventListener("keydown", (e) => {
  const tag = e.target && e.target.tagName;
  if (e.key === "/" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") { e.preventDefault(); $("#search").focus(); }
});

// Dropping links onto the panel fills the form (the person still presses Add).
const hub = $("#hub");
hub.addEventListener("dragover", (e) => { e.preventDefault(); });
hub.addEventListener("drop", (e) => {
  e.preventDefault();
  const dt = e.dataTransfer;
  const text = dt ? (dt.getData("text/uri-list") || dt.getData("text/plain")) : "";
  const urls = extractInputs(text);
  if (!urls.length) { toast("That isn't a link I can download", "warn"); return; }
  S.selected = null;
  S.detail = null;
  render();
  $("#addInput").value = urls.join("\n");
  previewHint(urls);
});

host.onClose(() => { S.closed = true; clearTimeout(S.timer); clearTimeout(S.holdTimer); clearTimeout(hintTimer); });

(async function start() {
  await loadPrefs();
  $("#groupBy").value = S.groupBy;
  render();
  poll();
})();
