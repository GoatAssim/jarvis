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
  { id: "scheduled", label: "Scheduled" },
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

// The buttons on every list card: the everyday controls (Open / Show in folder stay in the detail pane).
// Each is offered only when the row's own capabilities allow it, so a read-only provider row has none.
const CARD_VERBS = ["pause", "resume", "retry", "cancel"];
const CARD_TITLES = {
  pause: "Stop it for now and keep what is already downloaded; Resume carries on",
  resume: "Carry on from where it stopped (a download waiting for its start time starts now)",
  retry: "Try it again",
  cancel: "Stop it and throw away the partly downloaded file",
};
function cardActions(row) {
  return rowActions(row).filter((a) => CARD_VERBS.indexOf(a.verb) !== -1);
}

// "today 3:00 AM", "tomorrow 3:00 AM", "Tue 3:00 AM", "Nov 2 3:00 AM" for a start time (epoch seconds).
function fmtWhen(sec, nowMs) {
  const s = num(sec);
  if (s === null) return "";
  const d = new Date(s * 1000);
  const now = new Date(nowMs === undefined || nowMs === null ? Date.now() : nowMs);
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(now)) / 86400000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (diff === 0) return "today " + time;
  if (diff === 1) return "tomorrow " + time;
  if (diff > 1 && diff < 7) return d.toLocaleDateString([], { weekday: "short" }) + " " + time;
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
}

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
    case "paused":
      if (row.scheduled_at) return "Scheduled" + DOT + "starts " + fmtWhen(row.scheduled_at);
      return "Paused" + (percentText(row) ? " at " + percentText(row) : "");
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
    case "scheduled": return !!row.scheduled_at && !stale;
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
    if (a.state === "queued" && b.state === "queued" && num(a.priority) !== null && num(b.priority) !== null && a.owner === "hub" && b.owner === "hub") {
      // the order the hub will actually start them in (shown by the live stream, which carries `priority`)
      return (b.priority - a.priority) || ((a.created_at || 0) - (b.created_at || 0));
    }
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

// ---- the add form calls the tool of the engine that will take each link (N4)
// dl_add's dry_run says, per link, which engine takes it and which add tool carries that engine's
// own options (decision.add_tool).  The form does what the model does: ask the router, then call
// that tool.  Anything unknown or unanswered stays with dl_add, which routes it again itself.
// -> [{ tool, urls }] in order of first appearance.
function groupByAddTool(urls, decisions) {
  const list = Array.isArray(decisions) ? decisions : [];
  const order = [];
  const by = {};
  (Array.isArray(urls) ? urls : []).forEach((u, i) => {
    const d = list[i];
    const t = d && /^dl_add[a-z0-9_]*$/.test(String(d.add_tool || "")) ? d.add_tool : "dl_add";
    if (!by[t]) { by[t] = []; order.push(t); }
    by[t].push(u);
  });
  return order.map((t) => ({ tool: t, urls: by[t] }));
}

// The calls to make: one per group, except dl_add_stremio, which takes one magnet at a time.
// `base` (dest, group, start_paused and the extras) goes to every call.  -> [{ tool, args }]
function addCalls(groups, base) {
  const calls = [];
  (groups || []).forEach((g) => {
    if (g.tool === "dl_add_stremio") {
      g.urls.forEach((u) => calls.push({ tool: g.tool, args: Object.assign({}, base, { magnet: u }) }));
    } else {
      calls.push({ tool: g.tool, args: Object.assign({}, base, { urls: g.urls }) });
    }
  });
  return calls;
}

// One answer out of several add calls: what was queued adds up; a declined confirmation or a
// failure in one does not hide what the others did.
function mergeAddResults(results) {
  const list = (Array.isArray(results) ? results : []).filter((r) => r && typeof r === "object");
  if (list.length === 1) return list[0];
  const out = { ok: false, count: 0, added: [], skipped: [] };
  const notes = [];
  list.forEach((r) => {
    if (r.declined) out.declined = true;
    if (r.ok) out.ok = true;
    out.count += Number(r.count) || 0;
    (r.added || []).forEach((a) => out.added.push(a));
    (r.skipped || []).forEach((a) => out.skipped.push(a));
    if (r.note) notes.push(r.note);
    if (!r.ok && r.error && !out.error) out.error = r.error;
  });
  if (notes.length) out.note = notes.join(" ");
  if (out.ok) delete out.error;
  return out;
}

// ---- helpers added for the three-pane layout (rework 17.1); pure, like everything above

// The one word that picks a row's colour (the dot and the left edge).  A silent provider row
// is "stale", whatever state it last reported.
function toneOf(row) {
  if (isStale(row)) return "stale";
  switch (row.state) {
    case "downloading": case "probing": case "connecting": case "postprocessing": return "active";
    case "queued": return "queued";
    case "paused": return row.scheduled_at ? "scheduled" : "paused";
    case "stalled": return "stalled";
    case "failed": return "failed";
    case "done": return "done";
    case "cancelled": return "cancelled";
    default: return "unknown";
  }
}

// Counts for the Overview pane, by tone.
function overviewStats(rows) {
  const out = { total: rows.length, active: 0, queued: 0, paused: 0, scheduled: 0, stalled: 0, failed: 0, done: 0, cancelled: 0, stale: 0, unknown: 0 };
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

// ---- preferences: speed limit, notifications, when it finishes, folders (all pure)

const LIMIT_PRESETS = [0, 256, 512, 1024, 2048, 5120, 10240];     // KB/s, 0 = no limit
const MAX_LIMIT_KBPS = 10000000;                                  // what the hub accepts

function fmtLimit(kbps) {
  const n = Math.round(Number(kbps) || 0);
  if (n <= 0) return "Unlimited";
  if (n < 1024) return n + " KB/s";
  return (Math.round((n / 1024) * 10) / 10) + " MB/s";
}

// A typed speed ("500", "2.5" with a unit) -> {kbps} or {error}.  Empty means no limit.
function parseLimit(value, unit) {
  const text = String(value === undefined || value === null ? "" : value).trim().replace(",", ".");
  if (text === "") return { kbps: 0 };
  const n = Number(text);
  if (!isFinite(n) || n < 0) return { error: "Enter a speed like 500 (KB/s) or 2 (MB/s), or leave it empty for no limit." };
  const kbps = Math.round(unit === "mb" ? n * 1024 : n);
  if (kbps > MAX_LIMIT_KBPS) return { error: "That speed is higher than the hub accepts." };
  return { kbps };
}

// The reverse, for filling the two boxes from a stored number.
function limitFields(kbps) {
  const n = Math.round(Number(kbps) || 0);
  if (n <= 0) return { value: "", unit: "kb" };
  if (n % 1024 === 0) return { value: String(n / 1024), unit: "mb" };
  return { value: String(n), unit: "kb" };
}

// "Limit 1 MB/s", "Unlimited (scheduled)": the limit in force right now, from summary.speed_limit.
function limitSummary(sl) {
  const kbps = Math.round(Number(sl && sl.kbps) || 0);
  const scheduled = !!(sl && sl.source === "schedule");
  if (scheduled) return (kbps > 0 ? "Limit " + fmtLimit(kbps) : "Unlimited") + " (scheduled)";
  return kbps > 0 ? "Limit " + fmtLimit(kbps) : "No speed limit";
}

// The options of the quick speed menu: the presets, the current value if it isn't one, and a way
// into the full settings.  `value` is KB/s as text, or "custom".
function quickSpeedOptions(sl) {
  const cur = Math.round(Number(sl && sl.kbps) || 0);
  const scheduled = !!(sl && sl.source === "schedule");
  const list = LIMIT_PRESETS.map((k) => ({ value: String(k), label: k === 0 ? "No limit" : fmtLimit(k), selected: k === cur }));
  if (LIMIT_PRESETS.indexOf(cur) === -1) list.push({ value: String(cur), label: fmtLimit(cur), selected: true });
  if (scheduled) list.forEach((o) => { if (o.selected) o.label += " (scheduled)"; });
  list.push({ value: "custom", label: "Custom or by time of day\u2026", selected: false });
  return list;
}

const OPEN_CHOICES = [
  { id: "off", label: "Do nothing", help: "The download just appears in the list." },
  { id: "reveal", label: "Show the file in its folder", help: "Opens the folder with the file selected." },
  { id: "folder", label: "Open its folder", help: "Opens the folder the file was saved in." },
  { id: "open", label: "Open the file", help: "Opens it with its usual program. Programs and scripts (.exe, .bat, .ps1, .js, .lnk and the like) are never opened this way; they are only shown in their folder." },
];

function openLabel(mode) {
  const c = OPEN_CHOICES.find((x) => x.id === mode);
  return c ? c.label : "Do nothing";
}

// Options of a "when it finishes" menu: first entry = follow the global choice.
function openMenu(globalMode) {
  return [{ value: "default", label: "Use my default (" + openLabel(globalMode || "off").toLowerCase() + ")" }]
    .concat(OPEN_CHOICES.map((c) => ({ value: c.id, label: c.label })));
}

// The notification options (the levels and the notification types), exactly as the notification
// catalog lists them right now.  There is NO copy of them in this file: the hub sends them with
// every poll (summary.notify_options, from notifier.describe_options(), which reads
// jarvis/notify_registry.py), applyNotifyOptions() below stores what arrived, and every menu and
// tick list is built from it.  A level or a type added to the notification module is offered here
// with no change to this file; until the first answer arrives the lists are empty and the panel
// says so.  `version` is the catalog's hash: when it changes the panel rebuilds its pickers.
const NOTIFY_OPTIONS = { actions: [], levels: [], version: "" };
const NOTIFY_OPTIONS_WAIT = "Loading the notification options\u2026";

// Every kind of download notification, in the order they are shown.
const NOTIFY_EVENTS = [
  { id: "done", label: "A download finishes", help: "One message per file, or one for a burst of finishes." },
  { id: "failed", label: "A download fails", help: "It couldn't finish, with the reason." },
  { id: "group_done", label: "A batch is finished", help: "The last file of a batch or playlist is done." },
  { id: "queue_empty", label: "Everything is finished", help: "Nothing is waiting or running any more." },
  { id: "stalled", label: "A download stalls", help: "It stopped receiving data and will be retried." },
  { id: "started", label: "A download starts", help: "Off by default: it is noisy for a big batch." },
];

// Store the notification options the hub just sent (see NOTIFY_OPTIONS above).  Returns true when
// something changed, so the caller rebuilds its pickers.  A missing or empty answer keeps what we
// have: a momentary failure must not blank the lists.  Every type in the catalog is offered for
// ONE download (the channels and the behaviours); the events that can announce it are the hub's
// own (ITEM_EVENTS below).  Empty = follow the overall notification settings.
function applyNotifyOptions(opts) {
  if (!opts || typeof opts !== "object") return false;
  const source = Array.isArray(opts.actions) && opts.actions.length ? opts.actions : (opts.channels || []);
  const actions = (Array.isArray(source) ? source : [])
    .filter((a) => a && typeof a.id === "string")
    .map((a) => ({ id: a.id, label: String(a.label || a.id), description: String(a.description || ""), dm: !!a.dm }));
  const levels = [];
  (Array.isArray(opts.levels) ? opts.levels : []).forEach((l) => {
    const n = num(l && l.value);
    if (n === null) return;
    const name = String((l && l.name) || "level " + n);
    levels.push({ value: n, label: String((l && l.label) || (n + " \u00B7 " + name.charAt(0).toUpperCase() + name.slice(1))),
      description: String((l && l.description) || "") });
  });
  if (!actions.length && !levels.length) return false;
  const next = { actions, levels, version: String(opts.version || "") };
  if (JSON.stringify(next) === JSON.stringify(NOTIFY_OPTIONS)) return false;
  NOTIFY_OPTIONS.actions = next.actions;
  NOTIFY_OPTIONS.levels = next.levels;
  NOTIFY_OPTIONS.version = next.version;
  return true;
}
const ITEM_EVENTS = [
  { id: "started", label: "Starts" },
  { id: "done", label: "Finishes" },
  { id: "failed", label: "Fails" },
  { id: "stalled", label: "Stalls" },
];

function channelLabel(id) {
  const c = NOTIFY_OPTIONS.actions.find((x) => x.id === id);
  return c ? c.label : String(id);
}

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABELS = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };

function numOr(v, d) {
  if (v === null || v === undefined || v === "") return d;
  const x = Number(v);
  return isFinite(x) ? x : d;
}

// The add form's three extras -> the dl_add arguments, or an error to show.
function addOptionsFromForm(f) {
  const out = {};
  const lim = parseLimit(f.limit, f.unit);
  if (lim.error) return { error: lim.error };
  if (lim.kbps > 0) out.speed_limit_kbps = lim.kbps;
  if (f.open && f.open !== "default") out.open_when_done = f.open;
  if (f.notify && f.notify !== "default") out.notify = f.notify;
  if (f.channels && f.channels.length) out.notify_channels = f.channels.slice();
  if (f.events && f.events.length) out.notify_events = f.events.slice();
  const start = String(f.start || "").trim();
  if (start && ["now", "right now", "immediately"].indexOf(start.toLowerCase()) === -1) out.start_at = start;
  return { args: out };
}

// One download's own choices, in words (row.prefs from the hub).
function itemPrefsText(prefs) {
  const p = prefs || {};
  const parts = [];
  if (p.limit_kbps) parts.push("limit " + fmtLimit(p.limit_kbps));
  if (p.open) parts.push("then: " + openLabel(p.open).toLowerCase());
  if (p.notify === "on") parts.push("always notify");
  if (p.notify === "off") parts.push("never notify");
  if (p.notify_channels && p.notify_channels.length) parts.push("notify via " + p.notify_channels.map(channelLabel).join(", "));
  if (p.notify_events && p.notify_events.length) {
    parts.push("notify when it " + p.notify_events.map((e) => (ITEM_EVENTS.find((x) => x.id === e) || { label: e }).label.toLowerCase()).join(" / "));
  }
  if (p.dest_abs) parts.push("saves in " + p.dest_abs);
  else if (p.subdir) parts.push("saves in " + p.subdir);
  return parts.join(DOT);
}

// ---- History, Providers and the queue order (phase 8 + phase 9 option B)

const HISTORY_STATES = ["done", "failed", "cancelled"];

// What the History tab lists: finished rows only, narrowed by state, source and a title search.
function historyFilter(rows, f) {
  const o = f || {};
  const q = String(o.search || "").trim().toLowerCase();
  return (rows || []).filter((r) => {
    if (HISTORY_STATES.indexOf(r.state) === -1) return false;
    if (o.state && o.state !== "all" && r.state !== o.state) return false;
    if (o.source && sourceKey(r) !== o.source) return false;
    if (q && String(r.title || r.id).toLowerCase().indexOf(q) === -1) return false;
    return true;
  }).sort((a, b) => (b.finished_at || b.created_at || 0) - (a.finished_at || a.created_at || 0));
}

// One spreadsheet cell.  A cell that starts like a formula is prefixed with ' so a title the web sent
// can't run anything when the file is opened in a spreadsheet.
function csvCell(v) {
  let t = v === null || v === undefined ? "" : String(v);
  if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;
  return /[",\r\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
}

const EXPORT_COLUMNS = [
  ["Title", (r) => r.title || r.id], ["State", (r) => r.state], ["Source", (r) => sourceLabel(r)],
  ["Kind", (r) => r.kind || ""], ["Size (bytes)", (r) => num(r.bytes_total) === null ? (num(r.bytes_done) === null ? "" : r.bytes_done) : r.bytes_total],
  ["Finished", (r) => (num(r.finished_at) === null ? "" : new Date(r.finished_at * 1000).toISOString())],
  ["Error", (r) => r.error || ""],
];
function historyCsv(rows) {
  const lines = [EXPORT_COLUMNS.map((c) => csvCell(c[0])).join(",")];
  (rows || []).forEach((r) => lines.push(EXPORT_COLUMNS.map((c) => csvCell(c[1](r))).join(",")));
  return lines.join("\r\n") + "\r\n";
}
function historyJson(rows) {
  return JSON.stringify((rows || []).map((r) => {
    const o = {};
    EXPORT_COLUMNS.forEach((c) => { o[c[0]] = c[1](r); });
    return o;
  }), null, 2);
}

// One History row's second line.
function historyMeta(row, nowMs) {
  const parts = [sourceLabel(row)];
  const size = num(row.bytes_total) !== null ? fmtBytes(row.bytes_total) : "";
  if (size) parts.push(size);
  const when = fmtWhen(row.finished_at, nowMs);
  if (when) parts.push(row.state === "done" ? when : row.state === "failed" ? "failed " + when : "cancelled " + when);
  if (row.state === "failed" && row.error) parts.push(String(row.error).slice(0, 140));
  return parts.join(DOT);
}

// How a provider looks on the Providers tab: off / bad (it reported an error) / quiet (every live row of
// it has stopped reporting) / ok.
function providerTone(p) {
  if (!p.enabled) return "off";
  if (p.last_error) return "bad";
  if (num(p.live) > 0 && num(p.stale) === num(p.live)) return "quiet";
  return "ok";
}
function providerFacts(p) {
  const facts = [];
  facts.push(["Downloads", String(num(p.rows) || 0) + (num(p.live) ? " (" + p.live + " active)" : "")]);
  facts.push(["Last report", num(p.last_report_age_s) === null ? "none yet" : fmtAgo(p.last_report_age_s)]);
  if (p.mode === "poll" || num(p.interval_s) !== null) {
    facts.push(["Checks every", num(p.interval_s) === null ? "\u2014" : p.interval_s + " s"]);
    facts.push(["Last good check", num(p.last_ok_age_s) === null ? "none yet" : fmtAgo(p.last_ok_age_s)]);
  }
  facts.push(["Can be controlled", p.read_only ? "No: it only reports" : (p.controls || []).join(", ") || "Yes"]);
  return facts;
}
function providerBadge(p) {
  return !p.enabled ? "Off" : p.read_only ? "Read-only" : p.mode === "poll" ? "Polled" : "Reports";
}

// The waiting hub queue in the order it will RUN (priority first, then oldest first).
function queueOrder(rows) {
  return (rows || []).filter((r) => r.owner === "hub" && r.state === "queued")
    .sort((a, b) => (num(b.priority) || 0) - (num(a.priority) || 0) || (a.created_at || 0) - (b.created_at || 0))
    .map((r) => r.id);
}
// `ids` with `id` moved next to `targetId` (before it, or after when `after`).  Pure, never mutates.
function moveInOrder(ids, id, targetId, after) {
  const list = (ids || []).filter((x) => x !== id);
  const at = list.indexOf(targetId);
  if (at === -1 || ids.indexOf(id) === -1) return (ids || []).slice();
  list.splice(after ? at + 1 : at, 0, id);
  return list;
}
// One step up (-1) or down (+1) in the queue, or null at the end.
function stepInOrder(ids, id, delta) {
  const i = (ids || []).indexOf(id);
  const j = i + delta;
  if (i === -1 || j < 0 || j >= ids.length) return null;
  const out = ids.slice();
  out.splice(j, 0, out.splice(i, 1)[0]);
  return out;
}
// A row may be dragged only when the live stream is giving us the queue's real order.
function canReorder(row, live) {
  return !!live && row.owner === "hub" && row.state === "queued" && num(row.priority) !== null;
}

const pure = {
  ACTIVE, FINAL, VIEWS, KIND_ICON, fmtBytes, fmtSpeed, fmtEta, fmtAgo, percentText, speedText, etaText,
  sizeText, statusText, barLook, rowActions, sourceKey, sourceLabel, viewMatches, viewCounts, sourceList,
  filterRows, sortRows, groupRows, moveSelection, extractInputs, hintFromDecisions, isStale,
  toneOf, overviewStats, needsAttention, runningNow, sublineText, countText,
  LIMIT_PRESETS, OPEN_CHOICES, NOTIFY_EVENTS, NOTIFY_OPTIONS, NOTIFY_OPTIONS_WAIT, DAYS, fmtLimit, parseLimit, limitFields,
  limitSummary, quickSpeedOptions, openLabel, openMenu,
  addOptionsFromForm, itemPrefsText, groupByAddTool, addCalls, mergeAddResults,
  ITEM_EVENTS, channelLabel,
  CARD_VERBS, cardActions, fmtWhen, applyNotifyOptions,
  HISTORY_STATES, historyFilter, csvCell, historyCsv, historyJson, historyMeta, providerTone, providerFacts, providerBadge,
  queueOrder, moveInOrder, stepInOrder, canReorder,
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
  hubPrefs: null,
  saving: false,
  tab: "downloads", live: false, liveAt: 0, es: null, liveTimer: 0, dragId: "",
  providers: [], provTimer: 0, provLoaded: false,
  hist: { rows: [], more: false, loaded: false, busy: false, state: "all", source: "", search: "", error: "" },
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

// Every one-line answer this panel gives ("Demo downloads started", "Scheduled", "That didn't work")
// is a Jarvis NOTIFICATION, not a toast of its own: host.notify hands it to the notifier under the
// `download` kind, the same kind and source the hub daemon's "finished" messages use, so it is
// recorded in the Notifications inbox, follows the owner's Downloads notification settings and is
// drawn by the one toast renderer.  This panel has no other way of telling the person anything.
function toast(msg, level) {
  try {
    host.notify(String(msg), level || "info", { kind: "download", source: "download_hub", title: "Download Hub" });
  } catch (e) { /* the host may already be closing */ }
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

function renderSpeedQuick() {
  const sel = $("#speedQuick");
  const sl = S.summary && S.summary.speed_limit;
  if (!S.summary || root.activeElement === sel) return;         // not yet known, or the menu is open
  if (!changed("speedq", sl || {})) return;
  sel.textContent = "";
  quickSpeedOptions(sl).forEach((o) => {
    const opt = el("option", { text: o.label, attrs: { value: o.value } });
    if (o.selected) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.title = limitSummary(sl) + ". Change it here, or set limits for certain times of day in Settings.";
}

function renderTop() {
  renderSpeedQuick();
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
  const buttons = cardActions(row).map((a) => el("button", {
    cls: "rbtn" + (a.danger ? " rbtn--danger" : ""), text: a.label, title: CARD_TITLES[a.verb] || a.label,
    attrs: { type: "button", "aria-label": a.label + ": " + (row.title || row.id) },
    on: { click: (e) => { e.stopPropagation(); e.preventDefault(); e.currentTarget.disabled = true; runAction(row, a); } },
  }));
  const draggable = canReorder(row, S.live);
  if (draggable) {
    const order = queueOrder(S.rows);
    const at = order.indexOf(row.id);
    [["\u2191", -1, "Move up in the queue"], ["\u2193", 1, "Move down in the queue"]].forEach((m) => {
      const attrs = { type: "button", "aria-label": m[2] + ": " + (row.title || row.id) };
      if (at + m[1] < 0 || at + m[1] >= order.length) attrs.disabled = "disabled";
      buttons.push(el("button", {
        cls: "rbtn", text: m[0], title: m[2],
        attrs: attrs,
        on: { click: (e) => { e.stopPropagation(); e.preventDefault(); moveQueued(row.id, m[1]); } },
      }));
    });
  }
  const rowEl = el("div", {
    cls: "row tone-" + tone + " state-" + row.state,
    attrs: { role: "option", "aria-selected": row.id === S.selected ? "true" : "false", "data-id": row.id, draggable: draggable ? "true" : "false" },
    on: { click: () => select(row.id) },
  }, [
    el("div", { cls: "row__top" }, [
      draggable ? el("span", { cls: "row__grip", text: "\u22EE\u22EE", title: "Drag to change the order downloads start in", attrs: { "aria-hidden": "true" } }) : null,
      el("span", { cls: "dot tone-" + tone, attrs: { "aria-hidden": "true" } }),
      el("span", { cls: "row__title", text: row.title || row.id, title: row.title || row.id }),
      el("span", { cls: "row__kind", text: KIND_ICON[row.kind] || KIND_ICON.other, attrs: { "aria-hidden": "true" } }),
    ]),
    renderBar(row, false),
    el("div", { cls: "row__meta" }, meta),
    buttons.length ? el("div", { cls: "row__acts" }, buttons) : null,
  ]);
  if (draggable) wireDrag(rowEl, row);
  return rowEl;
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
  if (!changed("list", [rows, S.selected, S.groupBy, S.live])) return;
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
    const buttons = actions.map((a) => el("button", {
      cls: "btn btn--sm" + (a.danger ? " btn--danger" : ""), text: a.label, attrs: { type: "button" },
      on: { click: () => runAction(row, a) },
    }));
    if (actions.some((a) => a.verb === "open")) {          // a finished file: its folder is one click too
      buttons.splice(actions.findIndex((a) => a.verb === "reveal") + 1 || buttons.length, 0, el("button", {
        cls: "btn btn--sm", text: "Open folder", attrs: { type: "button" },
        on: { click: async () => { const r = await callTool("dl_open", { id: row.id, action: "folder" }); if (r.ok === false) toast(r.error || "That didn't work", "warn"); } },
      }));
    }
    box.appendChild(el("div", { cls: "detail__actions" }, buttons));
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
  renderAddExtras();
  renderDetailPrefs();
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
  counts.appendChild(countRow("Scheduled", st.scheduled));
  counts.appendChild(countRow("Stalled", st.stalled, st.stalled ? "flag" : ""));
  counts.appendChild(countRow("Failed", st.failed, st.failed ? "bad" : ""));
  counts.appendChild(countRow("Finished", st.done));
  counts.appendChild(countRow("Not reporting", st.stale, st.stale ? "flag" : ""));
  box.appendChild(el("div", {}, [el("h4", { text: "Right now" }), counts]));

  const cc = sum.concurrency || null;
  if (cc && cc.total) {
    const once = el("div", {}, [el("h4", { text: "Downloads at once" })]);
    once.appendChild(el("div", { cls: "big-speed", text: (cc.running || 0) + " of " + cc.total }));
    once.appendChild(el("div", { cls: "small", text: "The most the hub runs at the same time; the rest wait their turn. Other tools' downloads aren't counted." }));
    once.appendChild(el("div", { cls: "demo" }, [
      el("button", { cls: "btn btn--sm", text: "\u2212 Fewer", attrs: { type: "button", "aria-label": "Allow one download fewer at once" }, on: { click: () => setConcurrency(cc.total - 1) } }),
      el("button", { cls: "btn btn--sm", text: "+ More", attrs: { type: "button", "aria-label": "Allow one more download at once" }, on: { click: () => setConcurrency(cc.total + 1) } }),
    ]));
    box.appendChild(once);
  }
  const sch = sum.scheduled || null;
  if (sch && sch.count) {
    box.appendChild(el("div", {}, [el("h4", { text: "Scheduled" }),
      el("div", { text: sch.count + (sch.count === 1 ? " download waits" : " downloads wait") + " for a start time" }),
      el("div", { cls: "small", text: "Next: " + fmtWhen(sch.next_at) })]));
  }

  const hubSpeed = c.speed_hub_bps || 0;
  const otherSpeed = c.speed_other_bps || 0;
  const speedBox = el("div", {}, [
    el("h4", { text: "Speed" }),
    el("div", { cls: "big-speed", text: "\u2193 " + (fmtSpeed(hubSpeed + otherSpeed) || "idle") }),
  ]);
  if (otherSpeed > 0) speedBox.appendChild(el("div", { cls: "small", text: "hub " + (fmtSpeed(hubSpeed) || "0") + DOT + "other tools " + fmtSpeed(otherSpeed) + " (not under hub control)" }));
  speedBox.appendChild(el("div", { cls: "small", text: limitSummary(sum.speed_limit) }));
  speedBox.appendChild(el("div", { cls: "demo" }, [
    el("button", { cls: "btn btn--sm", text: "Change limit", attrs: { type: "button" }, on: { click: () => openSettings() } }),
  ]));
  box.appendChild(speedBox);

  const free = sum.free_bytes;
  const storage = el("div", {}, [el("h4", { text: "Storage" })]);
  storage.appendChild(el("div", { text: free === null || free === undefined ? "free space unknown" : fmtBytes(free) + " free" }));
  if (sum.library_root) storage.appendChild(el("div", { cls: "small", text: sum.library_root }));
  storage.appendChild(el("div", { cls: "demo" }, [
    el("button", { cls: "btn btn--sm", text: "Open folder", attrs: { type: "button" }, on: { click: openLibrary } }),
    el("button", { cls: "btn btn--sm", text: "Change\u2026", attrs: { type: "button" }, on: { click: () => openSettings() } }),
  ]));
  box.appendChild(storage);

  const after = el("div", {}, [el("h4", { text: "When it finishes" })]);
  after.appendChild(el("div", { cls: "small", text: openLabel(sum.after_download) }));
  after.appendChild(el("div", { cls: "small", text: sum.notifications_on === false ? "Notifications are off" : "Notifications are on" }));
  after.appendChild(el("div", { cls: "demo" }, [
    el("button", { cls: "btn btn--sm", text: "Notifications", attrs: { type: "button" }, on: { click: () => openSettings() } }),
    el("button", { cls: "btn btn--sm", text: "After download", attrs: { type: "button" }, on: { click: () => openSettings() } }),
  ]));
  box.appendChild(after);

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
  S.timer = setTimeout(poll, delay || (S.live ? 30000 : doc.hidden ? 8000 : 2500));
}

async function poll() {
  if (S.closed || S.busy) return;
  S.busy = true;
  try {
    // While the live stream is up it owns the rows; this slower poll only refreshes the summary (the
    // notification choices the stream leaves out) and doubles as a check that the hub still answers.
    const streaming = S.live;
    const r = await host.runTool("dl_list", streaming ? { limit: 1, summary: true } : { limit: 500, summary: true });
    if (r && r.ok === false) throw new Error(r.error || "the hub didn't answer");
    if (!streaming) S.rows = Array.isArray(r.items) ? r.items : [];
    if (r.summary) {
      S.summary = r.summary;
      if (applyNotifyOptions(r.summary.notify_options)) { S.sig.addextras = ""; S.sig.dprefs = ""; }
    }
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
  const args = {};
  const dest = $("#addDest").value.trim();
  const group = $("#addGroup").value.trim();
  if (dest) args.dest = dest;
  if (group) args.group = group;
  if ($("#addPaused").checked) args.start_paused = true;
  const extra = addOptionsFromForm({ limit: $("#addLimit").value, unit: $("#addLimitUnit").value, open: $("#addOpen").value, notify: $("#addNotify").value,
    channels: ticked($("#addChannels")), events: ticked($("#addEvents")), start: $("#addStart").value });
  if (extra.error) { setHint(extra.error, true); return; }
  Object.assign(args, extra.args);
  const btn = $("#addBtn");
  btn.disabled = true;
  try {
    // Ask the router which engine takes each link, then call that engine's add tool.
    const pre = await callTool("dl_add", { urls: urls, dry_run: true });
    const calls = addCalls(groupByAddTool(urls, pre && pre.decisions), args);
    const results = [];
    for (const c of calls) {
      const one = await callTool(c.tool, c.args);
      results.push(one);
      if (one && one.declined) break;
    }
    const r = mergeAddResults(results);
    if (r.declined) { setHint("Cancelled", false); return; }
    if (r.ok) {
      const added = r.added || [];
      const dup = added.filter((a) => a.duplicate).length;
      const when = added.length && added[0].start_at ? added[0].start_at : "";
      toast((when ? "Scheduled " : "Queued ") + r.count + (dup ? " (" + dup + " already in the list)" : "") + (when ? ", starts " + when.replace("T", " ") : ""), "success");
      if (r.note) toast(r.note, "warn");
      if (r.skipped && r.skipped.length) toast(r.skipped[0].problem, "warn");
      if (added.length === 1) S.pendingSelect = added[0].id;       // show the one you just added
      input.value = "";
      $("#addLimit").value = "";                  // one-off choices don't stick to the next add
      $("#addStart").value = "";
      $("#addOpen").value = "default";
      $("#addNotify").value = "default";
      untick($("#addChannels"));
      untick($("#addEvents"));
      setHint("", false);
      schedule(200);
    } else {
      setHint(r.error || "That didn't work", true);
    }
  } finally {
    btn.disabled = false;
  }
}

// ---- the add form's extras and one download's own options

function fillSelect(sel, options, value) {
  sel.textContent = "";
  options.forEach((o) => {
    const opt = el("option", { text: o.label, attrs: { value: o.value } });
    if (String(o.value) === String(value)) opt.selected = true;
    sel.appendChild(opt);
  });
}

// A group of plain checkboxes in `box`; the ticked ids are read back with ticked().
function buildChecks(box, items, waiting) {
  if (!items.length) {                              // nothing to offer yet: say so instead of an empty box
    if (box.getAttribute("data-sig") === "" && box.childNodes.length) return;
    box.textContent = "";
    box.setAttribute("data-sig", "");
    box.appendChild(el("div", { cls: "field__help", text: waiting || NOTIFY_OPTIONS_WAIT }));
    return;
  }
  const sig = items.map((it) => it.id + "=" + it.label).join("|");
  if (box.childNodes.length && box.getAttribute("data-sig") === sig) return;
  const keep = ticked(box);                         // a list that changed keeps what was already ticked
  box.textContent = "";
  box.setAttribute("data-sig", sig);
  items.forEach((it) => {
    const input = el("input", { attrs: { type: "checkbox", value: it.id } });
    if (keep.indexOf(it.id) !== -1) input.checked = true;
    box.appendChild(el("label", { cls: "check" }, [input, el("span", { text: " " + it.label })]));
  });
}

function ticked(box) {
  return Array.prototype.slice.call(box.querySelectorAll("input[type=checkbox]"))
    .filter((i) => i.checked).map((i) => i.value);
}

function untick(box) {
  Array.prototype.forEach.call(box.querySelectorAll("input[type=checkbox]"), (i) => { i.checked = false; });
}

function renderAddExtras() {
  const sum = S.summary || {};
  buildChecks($("#addChannels"), NOTIFY_OPTIONS.actions);
  buildChecks($("#addEvents"), ITEM_EVENTS);
  const openSel = $("#addOpen");
  if (root.activeElement !== openSel && changed("addopen", [sum.after_download])) {
    fillSelect(openSel, openMenu(sum.after_download), openSel.value || "default");
  }
  if (changed("adddest", [sum.library_root])) {
    $("#addDestHelp").textContent = sum.library_root
      ? "A folder inside your downloads library (" + sum.library_root + "), or a full path (you'll be asked to confirm one outside the library). Leave empty to use the folder for its file type."
      : "A folder inside your downloads library, or a full path (you'll be asked to confirm one outside the library). Leave empty to use the folder for its file type.";
  }
}

async function setStartTime(row, text) {
  const r = await callTool("dl_schedule", { id: row.id, when: text });
  if (r.declined || r.cancelled) toast("Not changed", "info");
  else if (r.ok === false) toast(r.error || "That didn't work", "warn");
  else {
    const low = String(text).toLowerCase();
    toast(low === "now" ? "Starting now" : low === "clear" ? "Start time cleared" : "Scheduled" + (r.start_at ? ": starts " + r.start_at.replace("T", " ") : ""), "success");
    if (r.note) toast(r.note, "warn");
  }
  S.sig.dprefs = "";
  S.sig.detail = "";
  schedule(150);
}

async function setConcurrency(n) {
  const total = Math.max(1, Math.min(16, Math.round(n)));
  const r = await callTool("dl_prefs", { set: { "concurrency.total": total } });
  if (r.ok === false) { toast(r.error || "That didn't work", "warn"); return; }
  if (S.summary) S.summary.concurrency = Object.assign({}, S.summary.concurrency || {}, { total: total });
  if (r.settings) S.hubPrefs = r.settings;
  toast("Up to " + total + " download" + (total === 1 ? "" : "s") + " at once", "success");
  S.sig.side = "";
  renderSide();
  schedule(150);
}

async function setItemPref(row, set, after) {
  const r = await callTool("dl_prefs", { id: row.id, set: set });
  if (r.declined || r.cancelled) toast("Not changed", "info");
  else if (r.ok === false) toast(r.error || "That didn't work", "warn");
  else toast("Saved for this download", "success");
  S.sig.dprefs = "";
  if (after) after();
  schedule(150);
}

function renderDetailPrefs() {
  const box = $("#detailPrefs");
  const row = selectedRow();
  const show = !!row && row.owner === "hub" && !FINAL.has(row.state);
  box.hidden = !show;
  if (!show) { S.sig.dprefs = ""; return; }
  if (box.contains(root.activeElement)) return;                    // never rebuild under the cursor
  const global = S.summary && S.summary.after_download;
  if (!changed("dprefs", [row.id, row.prefs || {}, global, row.scheduled_at || 0, row.can_schedule, NOTIFY_OPTIONS])) return;
  box.textContent = "";
  const prefs = row.prefs || {};
  box.appendChild(el("div", { cls: "h5", text: "For this download" }));

  // Start it later (the same plain words as scheduling a daemon: "tomorrow at 3am", "in 2 hours").
  if (row.can_schedule) {
    const when = el("input", { attrs: { type: "text", spellcheck: "false", autocomplete: "off", placeholder: "e.g. tomorrow at 3am, in 2 hours", "aria-label": "Start this download at" } });
    const setWhen = (text) => setStartTime(row, text);
    when.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (when.value.trim()) setWhen(when.value.trim()); } });
    const btns = [el("button", { cls: "btn btn--sm", text: row.scheduled_at ? "Change" : "Schedule", attrs: { type: "button" },
      on: { click: () => { if (when.value.trim()) setWhen(when.value.trim()); else toast("Type when it should start, like tomorrow at 3am", "warn"); } } })];
    if (row.scheduled_at) {
      btns.push(el("button", { cls: "btn btn--sm", text: "Start now", attrs: { type: "button" }, on: { click: () => setWhen("now") } }));
      btns.push(el("button", { cls: "btn btn--sm", text: "Clear", attrs: { type: "button", title: "Remove the start time; it stays paused until you resume it" }, on: { click: () => setWhen("clear") } }));
    }
    box.appendChild(el("div", { cls: "field" }, [
      el("label", { cls: "field__label", text: "Start at" }),
      el("div", { cls: "inline" }, [when].concat(btns)),
      el("div", { cls: "field__help", text: row.scheduled_at
        ? "Starts " + fmtWhen(row.scheduled_at) + ". It only starts if the hub daemon is running; if it was down at that time it starts as soon as it is back."
        : "Hold it until a time you choose. It waits paused and starts by itself; the hub daemon has to be running then." }),
    ]));
  }
  const current = itemPrefsText(prefs);
  box.appendChild(el("div", { cls: "small", text: current || "It follows your overall settings." }));

  const f = limitFields(prefs.limit_kbps);
  const input = el("input", { attrs: { type: "text", inputmode: "decimal", spellcheck: "false", placeholder: "No limit of its own", "aria-label": "Speed limit for this download", value: f.value } });
  input.value = f.value;
  const unit = el("select", { attrs: { "aria-label": "Unit" } }, [
    el("option", { text: "KB/s", attrs: { value: "kb" } }), el("option", { text: "MB/s", attrs: { value: "mb" } }),
  ]);
  unit.value = f.unit;
  const apply = () => {
    const lim = parseLimit(input.value, unit.value);
    if (lim.error) { toast(lim.error, "warn"); return; }
    setItemPref(row, { limit_kbps: lim.kbps });                    // 0 clears it
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } });
  box.appendChild(el("div", { cls: "field" }, [
    el("label", { cls: "field__label", text: "Speed limit" }),
    el("div", { cls: "inline" }, [input, unit, el("button", { cls: "btn btn--sm", text: "Set", attrs: { type: "button" }, on: { click: apply } })]),
  ]));

  const openSel = el("select", { attrs: { "aria-label": "When this download finishes" } });
  fillSelect(openSel, openMenu(global), prefs.open || "default");
  openSel.addEventListener("change", () => setItemPref(row, { open: openSel.value }, () => { openSel.blur(); renderDetailPrefs(); }));
  box.appendChild(el("div", { cls: "field" }, [el("label", { cls: "field__label", text: "When it finishes" }), openSel]));

  const notifySel = el("select", { attrs: { "aria-label": "Notify me about this download" } });
  fillSelect(notifySel, [
    { value: "default", label: "Follow my notification settings" }, { value: "on", label: "Always tell me" }, { value: "off", label: "Never tell me" },
  ], prefs.notify || "default");
  notifySel.addEventListener("change", () => setItemPref(row, { notify: notifySel.value }));
  box.appendChild(el("div", { cls: "field" }, [el("label", { cls: "field__label", text: "Tell me when it finishes" }), notifySel]));

  // Where and when THIS download is announced, instead of the overall notification settings.
  const chans = el("div", { cls: "checks", attrs: { role: "group", "aria-label": "Notification channels for this download" } });
  buildChecks(chans, NOTIFY_OPTIONS.actions);
  (prefs.notify_channels || []).forEach((id) => { const i = chans.querySelector("input[value=" + id + "]"); if (i) i.checked = true; });
  const evs = el("div", { cls: "checks", attrs: { role: "group", "aria-label": "Which events notify for this download" } });
  buildChecks(evs, ITEM_EVENTS);
  (prefs.notify_events || []).forEach((id) => { const i = evs.querySelector("input[value=" + id + "]"); if (i) i.checked = true; });
  const applyNotify = () => setItemPref(row, { notify_channels: ticked(chans), notify_events: ticked(evs) });
  box.appendChild(el("div", { cls: "field" }, [
    el("label", { cls: "field__label", text: "Tell me through" }), chans,
    el("label", { cls: "field__label", text: "Tell me when it" }), evs,
    el("div", { cls: "inline" }, [
      el("button", { cls: "btn btn--sm", text: "Save notification choices", attrs: { type: "button" }, on: { click: applyNotify } }),
    ]),
    el("div", { cls: "field__help", text: "Nothing ticked = follow your notification settings. Ticking only Discord tells you there and nowhere else (the inbox still keeps a record)." }),
  ]));

  // Its own folder, until it has started writing the file.
  if (!row.dest) {
    const folder = el("input", { attrs: { type: "text", spellcheck: "false", placeholder: "Default for its file type", "aria-label": "Folder for this download" } });
    folder.value = prefs.dest_abs || prefs.subdir || "";
    const applyFolder = () => setItemPref(row, { folder: folder.value.trim() });
    folder.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); applyFolder(); } });
    box.appendChild(el("div", { cls: "field" }, [
      el("label", { cls: "field__label", text: "Save in folder" }),
      el("div", { cls: "inline" }, [folder, el("button", { cls: "btn btn--sm", text: "Set", attrs: { type: "button" }, on: { click: applyFolder } })]),
      el("div", { cls: "field__help", text: "A folder in the library, or a full path (you'll be asked to confirm one outside it). Empty = the folder for its file type." }),
    ]));
  }
}

async function openLibrary() {
  const r = await callTool("dl_open", { target: "library" });
  if (r.ok === false) toast(r.error || "Couldn't open the folder", "warn");
}

// ---- Settings: they live in Settings > Downloads (web/public/downloads-settings.js), the one place to change them.
// This panel keeps two shortcuts (the speed menu and the Fewer / More buttons) that write through the same
// dl_prefs tool; everything else about the hub's settings is edited there, not here.

function openSettings() {
  const s = window.JarvisSettings;
  if (s && typeof s.show === "function") s.show({ section: "downloads" });
  else toast("Settings can't be opened from here", "warn");
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
// N14: Browse... beside "Save in folder". The picker (web/public/path-picker.js) only fills the box; the hub still
// checks the folder when the download is added (library rules, the confirmation outside the library).
$("#addDestBrowse").addEventListener("click", async () => {
  const picker = window.JarvisPathPicker;
  if (!picker || typeof picker.open !== "function") { toast("The folder picker isn't available. Type the folder instead.", "warn"); return; }
  const box = $("#addDest");
  const libraryRoot = (S.summary && S.summary.library_root) || "";
  const chosen = await picker.open({ kind: "folder", title: "Save in folder", start: picker.startFor(box.value, libraryRoot) });
  if (chosen === null || chosen === undefined) { box.focus(); return; }
  box.value = picker.insideRoot(libraryRoot, chosen) || chosen;          // inside the library: just the sub-folder
  box.focus();
});
$("#search").addEventListener("input", (e) => { S.search = e.target.value; render(); });
$("#groupBy").addEventListener("change", (e) => { S.groupBy = e.target.value; savePrefs(); render(); });
$("#refresh").addEventListener("click", () => { S.lastError = ""; schedule(10); });
$("#newBtn").addEventListener("click", newDownload);
$("#openFolder").addEventListener("click", openLibrary);
$("#settingsBtn").addEventListener("click", openSettings);
$("#speedQuick").addEventListener("change", async (e) => {
  const menuEl = e.target;                 // e.target is cleared once an event inside a shadow root has been dispatched
  const v = menuEl.value;
  if (v === "custom") { S.sig.speedq = ""; renderSpeedQuick(); openSettings(); return; }
  const kbps = Number(v);
  const r = await callTool("dl_prefs", { set: { rate_limit_kbps: kbps } });
  if (r.ok === false) { toast(r.error || "That didn't work", "warn"); }
  else {
    const now = r.settings && r.settings.rate_limit_now;
    if (S.summary && now) S.summary.speed_limit = now;
    toast(kbps > 0 ? "Speed limit: " + fmtLimit(kbps) + (now && now.source === "schedule" ? " (a time window is overriding it right now)" : "") : "Speed limit removed", "success");
    if (r.settings) S.hubPrefs = r.settings;
  }
  S.sig.speedq = "";
  menuEl.blur();
  renderSpeedQuick();
  schedule(150);
});
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
  if (e.target && e.target.closest && e.target.closest("button")) return;      // a card's own button handles its keys
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
  if (S.dragId) return;                         // a queued row being reordered, not links being dropped
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


// ============================================================================== tabs, live stream, Providers, History, queue order

// One call to the hub's own web routes (jarvis/hub_admin.py through /api/hub/*).  Reads need nothing more; the
// writes send the header the server insists on so another page can't make the browser change anything.
async function hubFetch(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Jarvis-Hub": "1" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok || !data || data.ok === false) throw new Error((data && (data.error || data.message)) || ("HTTP " + res.status));
  return data;
}

// A confirmation for the few things that remove list entries.  The page's own dialog; with none, nothing runs.
async function askFirst(title, body, label) {
  const ui = window.JarvisUI;
  if (!ui || typeof ui.confirm !== "function") return false;
  try { return !!(await ui.confirm({ title: title, body: body, level: "warn", focusCancel: true, confirmLabel: label })); }
  catch (e) { return false; }
}

// ---- tabs

function setTab(name, focus) {
  if (["downloads", "providers", "history"].indexOf(name) === -1) return;
  S.tab = name;
  $("#hub").dataset.tab = name;
  ["downloads", "providers", "history"].forEach((t) => {
    const btn = $("#tab" + t.charAt(0).toUpperCase() + t.slice(1));
    const on = t === name;
    btn.setAttribute("aria-selected", on ? "true" : "false");
    btn.tabIndex = on ? 0 : -1;
    if (on && focus) btn.focus();
  });
  $("#body").hidden = name !== "downloads";
  $("#providersPane").hidden = name !== "providers";
  $("#historyPane").hidden = name !== "history";
  clearInterval(S.provTimer);
  if (name === "providers") {
    loadProviders();
    S.provTimer = setInterval(() => { if (!doc.hidden) loadProviders(); }, 5000);
  } else if (name === "history") {
    loadHistory();
  }
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target && e.target.closest ? e.target.closest("[data-tab]") : null;
  if (b) setTab(b.getAttribute("data-tab"), false);
});
$("#tabs").addEventListener("keydown", (e) => {
  const order = ["downloads", "providers", "history"];
  const at = order.indexOf(S.tab);
  let to = -1;
  if (e.key === "ArrowRight") to = (at + 1) % order.length;
  else if (e.key === "ArrowLeft") to = (at + order.length - 1) % order.length;
  else if (e.key === "Home") to = 0;
  else if (e.key === "End") to = order.length - 1;
  if (to >= 0) { e.preventDefault(); setTab(order[to], true); }
});

// ---- the live stream (one `jarvis hub-admin watch` process behind /api/hub/stream feeds every open view)

function setLiveTag() {
  const t = $("#liveTag");
  t.textContent = S.live ? "\u25CF Live" : "";
  t.classList.toggle("is-live", S.live);
  t.title = S.live ? "Updates arrive as they happen" : "";
}

function afterRowsChanged() {
  if (S.pendingSelect && S.rows.some((x) => x.id === S.pendingSelect)) { S.selected = S.pendingSelect; S.detail = null; }
  S.pendingSelect = "";
  if (S.selected && !S.rows.some((x) => x.id === S.selected)) { S.selected = null; S.detail = null; }
  S.lastError = "";
  render();
  const row = selectedRow();
  if (row && (!S.detail || S.detail.id !== row.id || S.detail.state !== row.state || Date.now() - S.detail.at > 8000)) loadDetail();
}

function liveDown() {
  if (!S.live) return;
  S.live = false;
  setLiveTag();
  schedule(300);                                  // back to polling at once
}

function startLive() {
  if (S.closed || S.es || typeof EventSource !== "function") return;
  let es;
  try { es = new EventSource("/api/hub/stream"); } catch (e) { return; }
  S.es = es;
  const parse = (ev) => { try { return JSON.parse(ev.data); } catch (e) { return null; } };
  es.addEventListener("snapshot", (ev) => {
    const m = parse(ev);
    if (!m || !Array.isArray(m.items)) return;
    S.live = true; S.liveAt = Date.now();
    S.rows = m.items;
    if (m.summary) S.summary = Object.assign({}, S.summary || {}, m.summary);
    setLiveTag();
    afterRowsChanged();
  });
  es.addEventListener("delta", (ev) => {
    const m = parse(ev);
    if (!m || !S.live) return;
    S.liveAt = Date.now();
    const byId = new Map(S.rows.map((r) => [r.id, r]));
    (m.upsert || []).forEach((r) => byId.set(r.id, r));
    (m.remove || []).forEach((id) => byId.delete(id));
    S.rows = Array.from(byId.values());
    if (m.summary) S.summary = Object.assign({}, S.summary || {}, m.summary);
    afterRowsChanged();
  });
  es.addEventListener("beat", () => { S.liveAt = Date.now(); });
  es.addEventListener("down", liveDown);
  es.onerror = liveDown;                          // EventSource reconnects by itself; polling covers the gap
  clearInterval(S.liveTimer);
  S.liveTimer = setInterval(() => { if (S.live && Date.now() - S.liveAt > 45000) liveDown(); }, 10000);
}

// ---- Providers

async function loadProviders() {
  const r = await callTool("dl_providers", {});
  if (S.closed) return;
  if (r.ok === false) {
    if (!S.provLoaded) $("#provSummary").textContent = r.error || "The providers couldn't be read.";
    return;
  }
  S.providers = Array.isArray(r.providers) ? r.providers : [];
  S.provLoaded = true;
  renderProviders();
}

function renderProviders() {
  const list = S.providers;
  $("#provCount").textContent = list.length ? String(list.length) : "";
  const bad = list.filter((p) => providerTone(p) === "bad").length;
  const quiet = list.filter((p) => providerTone(p) === "quiet").length;
  $("#provSummary").textContent = !list.length ? "No other tools report downloads here yet."
    : list.length + (list.length === 1 ? " provider" : " providers") + (bad ? " \u00B7 " + bad + " with an error" : "") + (quiet ? " \u00B7 " + quiet + " gone quiet" : "");
  if (!changed("providers", list)) return;
  const box = $("#provList");
  box.textContent = "";
  if (!list.length) {
    box.appendChild(el("div", { cls: "hist__empty", text: "Game installs and other tools that report into the hub appear here." }));
    return;
  }
  list.forEach((p) => {
    const tone = providerTone(p);
    const toggle = el("button", {
      cls: "btn btn--sm", text: p.enabled ? "Turn off" : "Turn on",
      attrs: { type: "button", "aria-label": (p.enabled ? "Turn off " : "Turn on ") + (p.label || p.id) },
      on: { click: async (e) => {
        e.currentTarget.disabled = true;
        const r = await callTool("dl_provider_prefs", { id: p.id, enabled: !p.enabled });
        if (r.declined) toast("Cancelled", "info");
        else if (r.ok === false) toast(r.error || "That didn't work", "warn");
        loadProviders();
      } },
    });
    const grid = el("div", { cls: "prov__grid" }, providerFacts(p).map((f) => el("div", { cls: "prov__kv" }, [
      el("span", { text: f[0] + ": " }), el("b", { text: f[1] }),
    ])));
    box.appendChild(el("div", { cls: "prov is-" + tone, attrs: { "data-id": p.id } }, [
      el("div", { cls: "prov__head" }, [
        el("span", { cls: "prov__name", text: p.label || p.id }),
        el("span", { cls: "prov__badge", text: providerBadge(p) }),
        tone === "quiet" ? el("span", { cls: "prov__badge", text: "Not reporting" }) : null,
        el("span", { cls: "prov__actions" }, [
          toggle,
          el("button", { cls: "btn btn--sm", text: "Settings\u2026", attrs: { type: "button" }, on: { click: openSettings } }),
        ]),
      ]),
      grid,
      p.last_error ? el("div", { cls: "prov__err", text: "Last error: " + String(p.last_error).slice(0, 240) }) : null,
    ]));
  });
}
$("#provRefresh").addEventListener("click", loadProviders);

// ---- History

let histTimer = 0;

async function loadHistory() {
  const h = S.hist;
  if (h.busy) return;
  h.busy = true;
  try {
    const r = await hubFetch("/api/hub/history?limit=500");
    h.rows = Array.isArray(r.items) ? r.items : [];
    h.more = !!r.more;
    h.error = "";
    h.loaded = true;
  } catch (err) {
    h.error = message(err);
  } finally {
    h.busy = false;
  }
  renderHistory();
}

function historyRows() {
  return historyFilter(S.hist.rows, { state: S.hist.state, source: S.hist.source, search: S.hist.search });
}

function renderHistory() {
  const h = S.hist;
  const rows = historyRows();
  // sources present in what is loaded
  const sel = $("#histSource");
  const keys = sourceList(h.rows);
  if (changed("histsrc", keys)) {
    sel.textContent = "";
    sel.appendChild(el("option", { text: "Every source", attrs: { value: "" } }));
    keys.forEach((k) => { const o = el("option", { text: k.label, attrs: { value: k.key } }); sel.appendChild(o); });
    sel.value = h.source;
  }
  $("#histCount").textContent = !h.loaded ? (h.error ? "" : "reading\u2026")
    : rows.length + (rows.length === 1 ? " entry" : " entries") + (h.more ? " (the newest " + h.rows.length + " loaded)" : "");
  $("#histCsv").disabled = !rows.length;
  $("#histJson").disabled = !rows.length;
  const box = $("#histList");
  if (!changed("hist", [rows, h.error, h.loaded])) return;
  box.textContent = "";
  if (h.error) { box.appendChild(el("div", { cls: "hist__empty", text: "The history couldn't be read: " + h.error })); return; }
  if (!h.loaded) return;
  if (!rows.length) {
    box.appendChild(el("div", { cls: "hist__empty", text: h.rows.length ? "Nothing matches this filter." : "Nothing has finished yet." }));
    return;
  }
  rows.forEach((r) => box.appendChild(historyRow(r)));
}

function historyRow(r) {
  const tone = r.state === "done" ? "ok" : r.state;
  const thumb = r.has_thumb
    ? el("img", { cls: "hist__thumb", attrs: { src: "/api/hub/thumb/" + encodeURIComponent(r.id), alt: "", loading: "lazy" } })
    : el("span", { cls: "hist__thumb", text: KIND_ICON[r.kind] || KIND_ICON.other, attrs: { "aria-hidden": "true" } });
  const acts = [];
  rowActions(r).filter((a) => ["open", "reveal", "retry"].indexOf(a.verb) !== -1).forEach((a) => {
    acts.push(el("button", {
      cls: "rbtn", text: a.label, attrs: { type: "button", "aria-label": a.label + ": " + (r.title || r.id) },
      on: { click: async (e) => { e.currentTarget.disabled = true; await runAction(r, a); loadHistory(); } },
    }));
  });
  if (r.owner === "hub" && r.state === "done") {
    acts.push(el("a", {
      cls: "rbtn", text: "Save a copy", title: "Download this file through the browser",
      attrs: { href: "/api/hub/file/" + encodeURIComponent(r.id), download: "", "aria-label": "Save a copy of " + (r.title || r.id) },
    }));
  }
  if (r.owner === "hub") {
    if (r.state !== "failed" || !acts.some((x) => x.textContent === "Retry")) {
      acts.push(el("button", {
        cls: "rbtn", text: "Download again", attrs: { type: "button", "aria-label": "Download again: " + (r.title || r.id) },
        on: { click: async (e) => {
          e.currentTarget.disabled = true;
          try { await hubFetch("/api/hub/redownload", { id: r.id }); toast("Queued again", "info"); schedule(300); }
          catch (err) { toast(message(err), "warn"); }
          e.currentTarget.disabled = false;
        } },
      }));
    }
    acts.push(el("button", {
      cls: "rbtn rbtn--danger", text: "Remove", title: "Remove from the list. The file is not deleted.",
      attrs: { type: "button", "aria-label": "Remove from history: " + (r.title || r.id) },
      on: { click: async () => {
        if (!(await askFirst("Remove from the history?", (r.title || r.id) + "\n\nOnly the list entry goes. The downloaded file is not touched.", "Remove"))) return;
        try { await hubFetch("/api/hub/history/remove", { ids: [r.id] }); toast("Removed from the history", "info"); }
        catch (err) { toast(message(err), "warn"); }
        loadHistory();
      } },
    }));
  }
  return el("div", { cls: "hist__row state-" + r.state + " tone-" + tone, attrs: { role: "listitem", "data-id": r.id } }, [
    thumb,
    el("div", { cls: "hist__main" }, [
      el("div", { cls: "hist__title", text: r.title || r.id, title: r.title || r.id }),
      el("div", { cls: "hist__meta", text: historyMeta(r), title: historyMeta(r) }),
    ]),
    el("div", { cls: "hist__actions" }, acts),
  ]);
}

function saveFile(name, mime, text) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = doc.createElement("a");
  a.href = url; a.download = name;
  doc.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
$("#histCsv").addEventListener("click", () => saveFile("download-history.csv", "text/csv;charset=utf-8", historyCsv(historyRows())));
$("#histJson").addEventListener("click", () => saveFile("download-history.json", "application/json", historyJson(historyRows())));
$("#histState").addEventListener("change", (e) => { S.hist.state = e.target.value; renderHistory(); });
$("#histSource").addEventListener("change", (e) => { S.hist.source = e.target.value; renderHistory(); });
$("#histSearch").addEventListener("input", (e) => {
  clearTimeout(histTimer);
  const v = e.target.value;
  histTimer = setTimeout(() => { S.hist.search = v; renderHistory(); }, 150);
});
$("#histClear").addEventListener("click", async () => {
  const days = Number.parseInt($("#histDays").value, 10);
  if (!Number.isFinite(days) || days < 1 || days > 3650) { toast("Enter a number of days from 1 to 3650", "warn"); return; }
  try {
    const dry = await hubFetch("/api/hub/history/clear", { days: days, dry_run: true });
    if (!dry.would_remove) { toast("Nothing is older than " + days + " days", "info"); return; }
    if (!(await askFirst("Clear old history?", "Remove " + dry.would_remove + " list " + (dry.would_remove === 1 ? "entry" : "entries")
      + " older than " + days + " days?\n\nOnly the list entries go. Downloaded files are not touched.", "Clear"))) return;
    const r = await hubFetch("/api/hub/history/clear", { days: days });
    toast("Removed " + r.removed + " from the history", "info");
  } catch (err) { toast(message(err), "warn"); }
  loadHistory();
});

// ---- the waiting queue: drag a row, or use its arrows (phase 9 option B)

async function sendOrder(ids) {
  try { await hubFetch("/api/hub/reorder", { ids: ids }); schedule(300); }
  catch (err) { toast(message(err), "warn"); }
}

function moveQueued(id, delta) {
  const next = stepInOrder(queueOrder(S.rows), id, delta);
  if (next) sendOrder(next);
}

function wireDrag(rowEl, row) {
  rowEl.addEventListener("dragstart", (e) => {
    S.dragId = row.id;
    S.hold = Date.now() + 60000;                  // don't rebuild the list under the drag
    try { e.dataTransfer.setData("application/x-jarvis-hub-row", row.id); e.dataTransfer.effectAllowed = "move"; } catch (err) { /* old browser */ }
    rowEl.classList.add("is-dragging");
  });
  rowEl.addEventListener("dragend", () => {
    S.dragId = "";
    S.hold = 0;
    rowEl.classList.remove("is-dragging");
    $("#list").querySelectorAll(".drop-before,.drop-after").forEach((n) => n.classList.remove("drop-before", "drop-after"));
    render();
  });
  rowEl.addEventListener("dragover", (e) => {
    if (!S.dragId || S.dragId === row.id) return;
    e.preventDefault();
    const box = rowEl.getBoundingClientRect();
    const after = e.clientY > box.top + box.height / 2;
    rowEl.classList.toggle("drop-after", after);
    rowEl.classList.toggle("drop-before", !after);
  });
  rowEl.addEventListener("dragleave", () => rowEl.classList.remove("drop-before", "drop-after"));
  rowEl.addEventListener("drop", (e) => {
    if (!S.dragId || S.dragId === row.id) return;
    e.preventDefault();
    e.stopPropagation();
    const after = rowEl.classList.contains("drop-after");
    const next = moveInOrder(queueOrder(S.rows), S.dragId, row.id, after);
    S.dragId = "";
    S.hold = 0;
    sendOrder(next);
  });
}

host.onClose(() => {
  S.closed = true; clearTimeout(S.timer); clearTimeout(S.holdTimer); clearTimeout(hintTimer);
  clearInterval(S.liveTimer); clearInterval(S.provTimer); clearTimeout(histTimer);
  if (S.es) { S.es.close(); S.es = null; }
});

(async function start() {
  await loadPrefs();
  $("#groupBy").value = S.groupBy;
  render();
  poll();
  startLive();
})();
