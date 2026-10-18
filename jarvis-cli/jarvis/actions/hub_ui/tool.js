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

// The notifier's own importance levels (jarvis/notifier.py LEVEL_NAMES).
const LEVEL_NAMES = { 1: "Silent", 2: "Standard", 3: "Persistent", 4: "Broadcast", 5: "Confirm" };

function levelMenu() {
  return [{ value: "default", label: "Notifier's default" }]
    .concat([1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: n + " \u00B7 " + LEVEL_NAMES[n] })));
}

// Every kind of download notification, in the order they are shown.
const NOTIFY_EVENTS = [
  { id: "done", label: "A download finishes", help: "One message per file, or one for a burst of finishes." },
  { id: "failed", label: "A download fails", help: "It couldn't finish, with the reason." },
  { id: "group_done", label: "A batch is finished", help: "The last file of a batch or playlist is done." },
  { id: "queue_empty", label: "Everything is finished", help: "Nothing is waiting or running any more." },
  { id: "stalled", label: "A download stalls", help: "It stopped receiving data and will be retried." },
  { id: "started", label: "A download starts", help: "Off by default: it is noisy for a big batch." },
];

// Where ONE download can be announced (the notifier's channels, jarvis/notifier.py CHANNELS) and which
// events can announce it.  Empty = follow the overall notification settings.
const NOTIFY_CHANNELS = [
  { id: "discord", label: "Discord" },
  { id: "desktop", label: "Desktop notification" },
  { id: "stream", label: "In-app toast" },
  { id: "voice", label: "Voice" },
  { id: "instagram", label: "Instagram" },
  { id: "playnite", label: "Playnite" },
  { id: "inbox", label: "Inbox only (silent)" },
];
const ITEM_EVENTS = [
  { id: "started", label: "Starts" },
  { id: "done", label: "Finishes" },
  { id: "failed", label: "Fails" },
  { id: "stalled", label: "Stalls" },
];

function channelLabel(id) {
  const c = NOTIFY_CHANNELS.find((x) => x.id === id);
  return c ? c.label : String(id);
}

// "D:\\x", "/mnt/x" and "~/x" are full paths; "Movies/Kids" is a folder inside the library.
function isFullPath(text) {
  return /^([A-Za-z]:|[\\/]|~)/.test(String(text || "").trim());
}

// ".ISO" / "iso" -> ".iso"; "" when it isn't a plain extension.
function cleanExt(text) {
  let t = String(text || "").trim().toLowerCase();
  if (t && t[0] !== ".") t = "." + t;
  return /^\.[a-z0-9_+-]{1,12}(\.[a-z0-9_+-]{1,12}){0,2}$/.test(t) ? t : "";
}

// {".iso": "ISOs"} as sorted [ext, folder] pairs for the settings table.
function extFolderRows(map) {
  return Object.keys(map || {}).sort().map((k) => [k, map[k]]);
}

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABELS = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };

function daysChoice(days) {
  const key = DAYS.filter((d) => (days || []).indexOf(d) !== -1).join(",");
  if (key === DAYS.join(",")) return "all";
  if (key === "mon,tue,wed,thu,fri") return "weekdays";
  if (key === "sat,sun") return "weekends";
  if (DAYS.indexOf(key) !== -1) return key;
  return "custom";
}

function daysFromChoice(choice, previous) {
  if (choice === "all") return DAYS.slice();
  if (choice === "weekdays") return DAYS.slice(0, 5);
  if (choice === "weekends") return DAYS.slice(5);
  if (DAYS.indexOf(choice) !== -1) return [choice];
  return (previous && previous.length ? previous : DAYS).slice();
}

function blankWindow() {
  return { days: DAYS.slice(), from: "01:00", to: "07:00", rate_limit_kbps: 0 };
}

function numOr(v, d) {
  if (v === null || v === undefined || v === "") return d;
  const x = Number(v);
  return isFinite(x) ? x : d;
}

// The settings screen edits a plain copy of what the hub reports ("the form model").
function prefsFromSettings(s) {
  const n = (s && s.notify) || {};
  const events = {};
  NOTIFY_EVENTS.forEach((e) => {
    const b = n[e.id] || {};
    events[e.id] = { enabled: !!b.enabled, level: b.level === undefined || b.level === null ? null : Number(b.level) };
  });
  const f = (s && s.folders) || {};
  const c = (s && s.concurrency) || {};
  const h = (s && s.http) || {};
  return {
    rate_limit_kbps: Math.round(numOr(s && s.rate_limit_kbps, 0)),
    schedule: JSON.parse(JSON.stringify((s && s.schedule) || [])),
    after_open: (s && s.after_download && s.after_download.open) || "off",
    notify: {
      enabled: n.enabled !== false, min_seconds: numOr(n.min_seconds, 0), coalesce_s: numOr(n.coalesce_s, 8),
      providers: n.providers !== false, events: events,
    },
    folders: { video: f.video || "Video", audio: f.audio || "Audio", file: f.file || "Files" },
    ext_folders: Object.assign({}, (s && s.ext_folders) || {}),
    concurrency: { total: numOr(c.total, 4), http: numOr(c.http, 2) },
    http: { stall_seconds: numOr(h.stall_seconds, 60), max_retries: numOr(h.max_retries, 4) },
  };
}

// What changed between two form models, as the dotted keys `dl_prefs` takes.  Nothing changed -> {}.
function prefsDiff(base, cur) {
  const u = {};
  if (cur.rate_limit_kbps !== base.rate_limit_kbps) u.rate_limit_kbps = cur.rate_limit_kbps;
  if (JSON.stringify(cur.schedule) !== JSON.stringify(base.schedule)) u.schedule = cur.schedule;
  if (cur.after_open !== base.after_open) u["after_download.open"] = cur.after_open;
  ["enabled", "min_seconds", "coalesce_s", "providers"].forEach((k) => {
    if (cur.notify[k] !== base.notify[k]) u["notify." + k] = cur.notify[k];
  });
  NOTIFY_EVENTS.forEach((e) => {
    const a = base.notify.events[e.id];
    const b = cur.notify.events[e.id];
    if (a.enabled !== b.enabled) u["notify." + e.id + ".enabled"] = b.enabled;
    if (a.level !== b.level) u["notify." + e.id + ".level"] = b.level === null ? "default" : b.level;
  });
  ["video", "audio", "file"].forEach((k) => { if (cur.folders[k] !== base.folders[k]) u["folders." + k] = cur.folders[k]; });
  if (JSON.stringify(extFolderRows(cur.ext_folders)) !== JSON.stringify(extFolderRows(base.ext_folders))) {
    // The whole map is sent; a removed type is simply missing from it.
    u.ext_folders = Object.assign({}, cur.ext_folders);
  }
  ["total", "http"].forEach((k) => { if (cur.concurrency[k] !== base.concurrency[k]) u["concurrency." + k] = cur.concurrency[k]; });
  ["stall_seconds", "max_retries"].forEach((k) => { if (cur.http[k] !== base.http[k]) u["http." + k] = cur.http[k]; });
  return u;
}

// "3 of 6 types on" for the overview; "off" when the master switch is off.
function notifySummary(model) {
  if (!model || !model.notify.enabled) return "Notifications are off";
  const on = NOTIFY_EVENTS.filter((e) => model.notify.events[e.id].enabled).length;
  return on === 0 ? "No notification types are on" : on + " of " + NOTIFY_EVENTS.length + " notification types on";
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

// What "Send test" tells the person.
function testResultText(r) {
  if (!r || r.ok === false) return (r && r.error) || "The test didn't go through.";
  const got = (r.delivered || []).join(", ");
  const miss = (r.failed_channels || []).join(", ");
  return "Test sent" + (got ? ": " + got : "") + (miss ? ". Couldn't reach: " + miss : "") + ".";
}

const pure = {
  ACTIVE, FINAL, VIEWS, KIND_ICON, fmtBytes, fmtSpeed, fmtEta, fmtAgo, percentText, speedText, etaText,
  sizeText, statusText, barLook, rowActions, sourceKey, sourceLabel, viewMatches, viewCounts, sourceList,
  filterRows, sortRows, groupRows, moveSelection, extractInputs, hintFromDecisions, isStale,
  toneOf, overviewStats, needsAttention, runningNow, sublineText, countText,
  LIMIT_PRESETS, OPEN_CHOICES, NOTIFY_EVENTS, LEVEL_NAMES, DAYS, fmtLimit, parseLimit, limitFields,
  limitSummary, quickSpeedOptions, openLabel, openMenu, levelMenu, daysChoice, daysFromChoice,
  blankWindow, prefsFromSettings, prefsDiff, notifySummary, addOptionsFromForm, itemPrefsText,
  NOTIFY_CHANNELS, ITEM_EVENTS, channelLabel, isFullPath, cleanExt, extFolderRows,
  testResultText,
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
  settingsOpen: false, page: "location", hubPrefs: null, base: null, model: null, formError: "", customSpeed: false,
  saving: false,
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
  speedBox.appendChild(el("div", { cls: "small", text: limitSummary(sum.speed_limit) }));
  speedBox.appendChild(el("div", { cls: "demo" }, [
    el("button", { cls: "btn btn--sm", text: "Change limit", attrs: { type: "button" }, on: { click: () => openSettings("speed") } }),
  ]));
  box.appendChild(speedBox);

  const free = sum.free_bytes;
  const storage = el("div", {}, [el("h4", { text: "Storage" })]);
  storage.appendChild(el("div", { text: free === null || free === undefined ? "free space unknown" : fmtBytes(free) + " free" }));
  if (sum.library_root) storage.appendChild(el("div", { cls: "small", text: sum.library_root }));
  storage.appendChild(el("div", { cls: "demo" }, [
    el("button", { cls: "btn btn--sm", text: "Open folder", attrs: { type: "button" }, on: { click: openLibrary } }),
    el("button", { cls: "btn btn--sm", text: "Change\u2026", attrs: { type: "button" }, on: { click: () => openSettings("location") } }),
  ]));
  box.appendChild(storage);

  const after = el("div", {}, [el("h4", { text: "When it finishes" })]);
  after.appendChild(el("div", { cls: "small", text: openLabel(sum.after_download) }));
  after.appendChild(el("div", { cls: "small", text: sum.notifications_on === false ? "Notifications are off" : "Notifications are on" }));
  after.appendChild(el("div", { cls: "demo" }, [
    el("button", { cls: "btn btn--sm", text: "Notifications", attrs: { type: "button" }, on: { click: () => openSettings("notifications") } }),
    el("button", { cls: "btn btn--sm", text: "After download", attrs: { type: "button" }, on: { click: () => openSettings("finished") } }),
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
  $("#hub").classList.toggle("is-settings", S.settingsOpen);
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
  const extra = addOptionsFromForm({ limit: $("#addLimit").value, unit: $("#addLimitUnit").value, open: $("#addOpen").value, notify: $("#addNotify").value,
    channels: ticked($("#addChannels")), events: ticked($("#addEvents")) });
  if (extra.error) { setHint(extra.error, true); return; }
  Object.assign(args, extra.args);
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
      $("#addLimit").value = "";                  // one-off choices don't stick to the next add
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
function buildChecks(box, items) {
  if (box.childNodes.length) return;
  items.forEach((it) => {
    const input = el("input", { attrs: { type: "checkbox", value: it.id } });
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
  buildChecks($("#addChannels"), NOTIFY_CHANNELS);
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
  if (!changed("dprefs", [row.id, row.prefs || {}, global])) return;
  box.textContent = "";
  const prefs = row.prefs || {};
  box.appendChild(el("div", { cls: "h5", text: "For this download" }));
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
  buildChecks(chans, NOTIFY_CHANNELS);
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

// ---- the Settings screen: location, speed, notifications, when it finishes, connections

const PAGES = [
  { id: "location", label: "Download location" },
  { id: "speed", label: "Speed limit" },
  { id: "notifications", label: "Notifications" },
  { id: "finished", label: "When it finishes" },
  { id: "connections", label: "Connections" },
];

function isDirty() {
  return !!(S.base && S.model && Object.keys(prefsDiff(S.base, S.model)).length);
}

function resetForm() {
  S.base = prefsFromSettings(S.hubPrefs);
  S.model = prefsFromSettings(S.hubPrefs);
  S.formError = "";
  S.customSpeed = false;
}

async function loadHubPrefs() {
  const r = await callTool("dl_prefs", {});
  if (r.ok && r.settings) { S.hubPrefs = r.settings; return true; }
  S.formError = r.error || "Couldn't read the settings from the hub.";
  return false;
}

async function openSettings(page) {
  S.settingsOpen = true;
  if (page) S.page = page;
  render();
  if (!S.model || !isDirty()) {
    const ok = await loadHubPrefs();
    if (ok) resetForm();
  }
  renderSettings();
}

function closeSettings() {
  S.settingsOpen = false;
  render();
}

function updateFooter() {
  const dirty = isDirty();
  $("#settingsDirty").textContent = S.formError ? S.formError : (dirty ? "Unsaved changes" : "");
  $("#settingsDirty").className = "settings__dirty" + (S.formError ? " is-bad" : "");
  $("#settingsSave").disabled = !dirty || S.saving;
  $("#settingsRevert").disabled = !dirty || S.saving;
}

// small builders -----------------------------------------------------------------------------

function setRow(label, control, help) {
  return el("div", { cls: "field" }, [el("label", { cls: "field__label", text: label }), control, help ? el("div", { cls: "field__help", text: help }) : null]);
}

function menu(options, value, onChange, label) {
  const sel = el("select", { attrs: { "aria-label": label || "" } });
  fillSelect(sel, options, value);
  sel.addEventListener("change", () => onChange(sel.value));
  return sel;
}

function numberBox(value, onInput, attrs) {
  const box = el("input", { attrs: Object.assign({ type: "number", inputmode: "numeric" }, attrs || {}) });
  box.value = String(value);
  box.addEventListener("input", () => { const n = Number(box.value); if (box.value !== "" && isFinite(n)) onInput(Math.round(n)); updateFooter(); });
  return box;
}

function checkBox(label, checked, onChange) {
  const box = el("input", { attrs: { type: "checkbox" } });
  box.checked = !!checked;
  box.addEventListener("change", () => { onChange(box.checked); updateFooter(); });
  return el("label", { cls: "check" }, [box, el("span", { text: " " + label })]);
}

function pageHead(title, text) {
  return [el("h4", { cls: "sp__h", text: title }), text ? el("div", { cls: "field__help sp__lead", text: text }) : null];
}

// pages ---------------------------------------------------------------------------------------

function pageLocation(box) {
  const lib = (S.hubPrefs && S.hubPrefs.library) || {};
  pageHead("Where downloads are saved", "Every download is saved inside this folder.").forEach((n) => n && box.appendChild(n));
  box.appendChild(el("div", { cls: "pathbox", text: lib.path || (S.hubPrefs && S.hubPrefs.library_root) || "" }));
  const bits = [];
  bits.push(lib.exists === false ? "The folder doesn't exist yet; it is created with the first download." : "The folder exists.");
  if (lib.writable === false) bits.push("It can't be written to: pick another folder.");
  if (lib.free_bytes !== null && lib.free_bytes !== undefined) bits.push(fmtBytes(lib.free_bytes) + " free.");
  box.appendChild(el("div", { cls: "small" + (lib.writable === false ? " bad" : ""), text: bits.join(" ") }));
  box.appendChild(el("div", { cls: "demo sp__gap" }, [el("button", { cls: "btn btn--sm", text: "Open folder", attrs: { type: "button" }, on: { click: openLibrary } })]));

  const path = el("input", { attrs: { type: "text", spellcheck: "false", placeholder: "Full path, e.g. D:\\Downloads\\Jarvis", "aria-label": "New download folder" } });
  const change = async () => {
    const value = path.value.trim();
    if (!value) { toast("Type the full path of the folder first", "warn"); return; }
    const r = await callTool("dl_settings", { set: { library_root: value } });
    if (r.declined || r.cancelled) { toast("Not changed", "info"); return; }
    if (r.ok === false) { S.formError = r.error || "That folder can't be used."; updateFooter(); return; }
    S.formError = "";
    if (r.settings) S.hubPrefs = r.settings;
    toast("Downloads will now be saved in " + (r.settings && r.settings.library_root), "success");
    schedule(100);
    renderSettings();
  };
  path.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); change(); } });
  box.appendChild(el("h4", { cls: "sp__h", text: "Use a different folder" }));
  box.appendChild(setRow("New download folder", el("div", { cls: "inline" }, [path, el("button", { cls: "btn btn--sm", text: "Change", attrs: { type: "button" }, on: { click: change } })]),
    "You'll be asked to confirm. Only new downloads go there; files already saved stay where they are."));

  box.appendChild(el("h4", { cls: "sp__h", text: "Folders by kind of file" }));
  box.appendChild(el("div", { cls: "field__help sp__lead", text: "When you don't choose a folder while adding, a file goes into the folder for its kind, inside the download folder." }));
  [["video", "Videos"], ["audio", "Audio"], ["file", "Everything else"]].forEach((pair2) => {
    const input = el("input", { attrs: { type: "text", spellcheck: "false", "aria-label": pair2[1] + " folder" } });
    input.value = S.model.folders[pair2[0]];
    input.addEventListener("input", () => { S.model.folders[pair2[0]] = input.value.trim(); updateFooter(); });
    box.appendChild(setRow(pair2[1], input));
  });

  box.appendChild(el("h4", { cls: "sp__h", text: "Folders by file type" }));
  box.appendChild(el("div", { cls: "field__help sp__lead", text: "A folder for one file extension, like .iso or .pdf. It beats the kind-of-file folders above, and a folder you pick while adding beats it. Use a folder inside the library (ISOs) or a full path (D:\\\\ISOs); a full path asks you to confirm when you save. The longer extension wins (.tar.gz over .gz)." }));
  const map = S.model.ext_folders;
  extFolderRows(map).forEach((pair2) => {
    const folder = el("input", { attrs: { type: "text", spellcheck: "false", "aria-label": "Folder for " + pair2[0] } });
    folder.value = pair2[1];
    folder.addEventListener("input", () => { map[pair2[0]] = folder.value.trim(); updateFooter(); });
    folder.addEventListener("change", () => { if (!map[pair2[0]]) { delete map[pair2[0]]; renderSettings(); } });
    box.appendChild(el("div", { cls: "extrow" }, [
      el("span", { cls: "ext", text: pair2[0] }), folder,
      el("button", { cls: "btn btn--sm", text: "Remove", attrs: { type: "button", "aria-label": "Remove the folder for " + pair2[0] },
        on: { click: () => { delete map[pair2[0]]; renderSettings(); } } }),
    ]));
  });
  const newExt = el("input", { attrs: { type: "text", spellcheck: "false", placeholder: ".iso", "aria-label": "File extension" } });
  const newFolder = el("input", { attrs: { type: "text", spellcheck: "false", placeholder: "Folder, e.g. ISOs or D:\\\\ISOs", "aria-label": "Folder for that extension" } });
  const addExt = () => {
    const ext = cleanExt(newExt.value);
    const target = newFolder.value.trim();
    if (!ext) { toast("Type an extension like .iso or .tar.gz", "warn"); return; }
    if (!target) { toast("Type the folder for " + ext, "warn"); return; }
    map[ext] = target;
    renderSettings();
  };
  newFolder.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addExt(); } });
  box.appendChild(el("div", { cls: "extrow" }, [newExt, newFolder, el("button", { cls: "btn btn--sm", text: "Add", attrs: { type: "button" }, on: { click: addExt } })]));
}

function pageSpeed(box) {
  const m = S.model;
  pageHead("Overall speed limit", "The most all your downloads together may use. It is shared between the downloads that are running.").forEach((n) => n && box.appendChild(n));
  box.appendChild(el("div", { cls: "small", text: "In force right now: " + limitSummary(S.summary && S.summary.speed_limit) }));

  const isPreset = LIMIT_PRESETS.indexOf(m.rate_limit_kbps) !== -1 && !S.customSpeed;
  const options = LIMIT_PRESETS.map((k) => ({ value: String(k), label: k === 0 ? "No limit" : fmtLimit(k) })).concat([{ value: "custom", label: "Custom\u2026" }]);
  box.appendChild(setRow("Limit", menu(options, isPreset ? String(m.rate_limit_kbps) : "custom", (v) => {
    if (v === "custom") { S.customSpeed = true; if (!m.rate_limit_kbps) m.rate_limit_kbps = 1024; }
    else { S.customSpeed = false; m.rate_limit_kbps = Number(v); }
    renderSettings();
  }, "Overall speed limit")));
  if (!isPreset) {
    const f = limitFields(m.rate_limit_kbps);
    const input = el("input", { attrs: { type: "text", inputmode: "decimal", spellcheck: "false", "aria-label": "Custom speed limit" } });
    input.value = f.value;
    const unit = el("select", { attrs: { "aria-label": "Unit" } }, [el("option", { text: "KB/s", attrs: { value: "kb" } }), el("option", { text: "MB/s", attrs: { value: "mb" } })]);
    unit.value = f.unit;
    const hint = el("div", { cls: "field__help" });
    const apply = () => {
      const lim = parseLimit(input.value, unit.value);
      if (lim.error) { hint.textContent = lim.error; hint.className = "field__help bad"; return; }
      hint.textContent = lim.kbps ? "= " + fmtLimit(lim.kbps) : "No limit";
      hint.className = "field__help";
      m.rate_limit_kbps = lim.kbps;
      updateFooter();
    };
    input.addEventListener("input", apply);
    unit.addEventListener("change", apply);
    box.appendChild(setRow("Custom speed", el("div", { cls: "inline" }, [input, unit]), null));
    box.appendChild(hint);
  }

  box.appendChild(el("h4", { cls: "sp__h", text: "By time of day" }));
  box.appendChild(el("div", { cls: "field__help sp__lead", text: "For example: slow down while you work, no limit overnight. Inside a window its speed replaces the overall limit above (empty = no limit). A window that ends earlier than it starts runs overnight." }));
  const dayOptions = [{ value: "all", label: "Every day" }, { value: "weekdays", label: "Weekdays" }, { value: "weekends", label: "Weekends" }]
    .concat(S.model.schedule.some((w) => daysChoice(w.days) === "custom") ? [{ value: "custom", label: "Chosen days" }] : [])
    .concat(["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((d) => ({ value: d, label: { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" }[d] })));
  m.schedule.forEach((w, i) => {
    const days = menu(dayOptions, daysChoice(w.days), (v) => { w.days = daysFromChoice(v, w.days); updateFooter(); }, "Days");
    const from = el("input", { attrs: { type: "time", "aria-label": "From" } }); from.value = w.from;
    const to = el("input", { attrs: { type: "time", "aria-label": "Until" } }); to.value = w.to;
    from.addEventListener("input", () => { w.from = from.value; updateFooter(); });
    to.addEventListener("input", () => { w.to = to.value; updateFooter(); });
    const f = limitFields(w.rate_limit_kbps);
    const lim = el("input", { attrs: { type: "text", inputmode: "decimal", spellcheck: "false", placeholder: "No limit", "aria-label": "Speed in this window" } }); lim.value = f.value;
    const unit = el("select", { attrs: { "aria-label": "Unit" } }, [el("option", { text: "KB/s", attrs: { value: "kb" } }), el("option", { text: "MB/s", attrs: { value: "mb" } })]); unit.value = f.unit;
    const hint = el("div", { cls: "field__help" });
    const apply = () => {
      const parsed = parseLimit(lim.value, unit.value);
      if (parsed.error) { hint.textContent = parsed.error; hint.className = "field__help bad"; return; }
      hint.textContent = ""; w.rate_limit_kbps = parsed.kbps; updateFooter();
    };
    lim.addEventListener("input", apply); unit.addEventListener("change", apply);
    box.appendChild(el("div", { cls: "win" }, [
      el("div", { cls: "inline" }, [days, from, el("span", { cls: "small", text: "to" }), to]),
      el("div", { cls: "inline" }, [el("span", { cls: "small", text: "Speed" }), lim, unit,
        el("button", { cls: "btn btn--sm btn--danger", text: "Remove", attrs: { type: "button" }, on: { click: () => { m.schedule.splice(i, 1); renderSettings(); } } })]),
      hint,
    ]));
  });
  box.appendChild(el("div", { cls: "demo sp__gap" }, [el("button", {
    cls: "btn btn--sm", text: "+ Add a time window", attrs: { type: "button", disabled: m.schedule.length >= 10 ? "disabled" : null },
    on: { click: () => { if (m.schedule.length < 10) { m.schedule.push(blankWindow()); renderSettings(); } } },
  })]));
  box.appendChild(el("div", { cls: "field__help sp__lead", text: "A single download can also have its own limit: in Options when you add it, or in its details while it is waiting or running." }));
}

async function sendTest(id, button) {
  button.disabled = true;
  const r = await callTool("dl_prefs", { test_notification: id });
  button.disabled = false;
  toast(testResultText(r), r.ok === false ? "warn" : "success");
}

function pageNotifications(box) {
  const m = S.model;
  pageHead("Notifications", "Messages go through Jarvis's normal notifications, so where they appear (in the app, on the desktop, by voice, on your phone) follows your notification settings. The level changes how loudly; \u201CNotifier's default\u201D leaves that to those settings.").forEach((n) => n && box.appendChild(n));
  box.appendChild(checkBox("Tell me about downloads", m.notify.enabled, (v) => { m.notify.enabled = v; renderSettings(); }));
  const table = el("div", { cls: "ntable" + (m.notify.enabled ? "" : " is-off") });
  NOTIFY_EVENTS.forEach((e) => {
    const ev = m.notify.events[e.id];
    const test = el("button", { cls: "btn btn--sm", text: "Send test", attrs: { type: "button", title: "Sends a sample through the notifier, using the saved settings" } });
    test.addEventListener("click", () => sendTest(e.id, test));
    table.appendChild(el("div", { cls: "nrow" }, [
      el("div", { cls: "nrow__main" }, [
        checkBox(e.label, ev.enabled, (v) => { ev.enabled = v; }),
        el("div", { cls: "field__help", text: e.help }),
      ]),
      menu(levelMenu(), ev.level === null ? "default" : String(ev.level), (v) => { ev.level = v === "default" ? null : Number(v); updateFooter(); }, "Level for: " + e.label),
      test,
    ]));
  });
  box.appendChild(table);
  box.appendChild(el("h4", { cls: "sp__h", text: "Fewer messages" }));
  box.appendChild(setRow("Only tell me about downloads that took at least (seconds)", numberBox(m.notify.min_seconds, (n) => { m.notify.min_seconds = Math.max(0, n); }, { min: "0", max: "86400" }), "0 = every download. Failures are always reported."));
  box.appendChild(setRow("Group finishes that come within (seconds)", numberBox(m.notify.coalesce_s, (n) => { m.notify.coalesce_s = Math.max(0, n); }, { min: "0", max: "600" }), "Several downloads finishing together become one message. 0 = one message each."));
  box.appendChild(checkBox("Also tell me about downloads other tools run (Steam and the like)", m.notify.providers, (v) => { m.notify.providers = v; }));
}

function pageFinished(box) {
  const m = S.model;
  pageHead("When a download finishes", "What Jarvis does by itself the moment a file is complete. A single download can choose differently in Options when you add it.").forEach((n) => n && box.appendChild(n));
  OPEN_CHOICES.forEach((c) => {
    const input = el("input", { attrs: { type: "radio", name: "openmode", value: c.id } });
    input.checked = m.after_open === c.id;
    input.addEventListener("change", () => { if (input.checked) { m.after_open = c.id; renderSettings(); } });
    box.appendChild(el("label", { cls: "choice" + (m.after_open === c.id ? " is-on" : "") }, [
      input, el("span", { cls: "choice__t", text: c.label }), el("span", { cls: "choice__h", text: c.help }),
    ]));
  });
  if (m.after_open === "open") {
    box.appendChild(el("div", { cls: "note note--warn" }, [el("strong", { text: "You'll be asked to confirm" }),
      doc.createTextNode("Opening files by itself is the one choice here that runs something you haven't looked at yet. At most a few files are opened per minute, so a big batch can't flood the screen.")]));
  }
}

function pageConnections(box) {
  const m = S.model;
  pageHead("Connections", "How many downloads run at once and when a stuck one is given up on.").forEach((n) => n && box.appendChild(n));
  box.appendChild(setRow("Downloads at once (all together)", numberBox(m.concurrency.total, (n) => { m.concurrency.total = n; }, { min: "1", max: "16" })));
  box.appendChild(setRow("Direct-link downloads at once", numberBox(m.concurrency.http, (n) => { m.concurrency.http = n; }, { min: "1", max: "16" })));
  box.appendChild(setRow("Call a download stalled after (seconds without data)", numberBox(m.http.stall_seconds, (n) => { m.http.stall_seconds = n; }, { min: "10", max: "3600" })));
  box.appendChild(setRow("Retry a failed download up to (times)", numberBox(m.http.max_retries, (n) => { m.http.max_retries = n; }, { min: "0", max: "10" })));
}

const PAGE_BUILDERS = { location: pageLocation, speed: pageSpeed, notifications: pageNotifications, finished: pageFinished, connections: pageConnections };

function renderSettings() {
  if (!S.settingsOpen) return;
  const nav = $("#settingsNav");
  nav.textContent = "";
  PAGES.forEach((pg) => {
    nav.appendChild(el("button", {
      cls: "snav" + (pg.id === S.page ? " is-on" : ""), text: pg.label,
      attrs: { type: "button", "aria-current": pg.id === S.page ? "page" : "false" },
      on: { click: () => { S.page = pg.id; renderSettings(); } },
    }));
  });
  const page = $("#settingsPage");
  page.textContent = "";
  if (!S.model) {
    page.appendChild(el("div", { cls: "empty", text: S.formError || "Reading the settings\u2026" }));
    updateFooter();
    return;
  }
  const box = el("div", { cls: "sp" });
  (PAGE_BUILDERS[S.page] || pageLocation)(box);
  page.appendChild(box);
  updateFooter();
}

async function saveSettings() {
  const updates = prefsDiff(S.base, S.model);
  if (!Object.keys(updates).length) return;
  S.saving = true;
  updateFooter();
  let r;
  const needsOk = updates.ext_folders && Object.keys(updates.ext_folders).some((k) => isFullPath(updates.ext_folders[k]));
  if (needsOk) {
    // A folder outside the library is saved through dl_settings, which asks you first.
    const first = await callTool("dl_settings", { set: { ext_folders: updates.ext_folders } });
    delete updates.ext_folders;
    r = first;
    if (first.ok !== false && !first.declined && !first.cancelled && Object.keys(updates).length) {
      r = await callTool("dl_prefs", { set: updates });
    }
  } else {
    r = await callTool("dl_prefs", { set: updates });
  }
  S.saving = false;
  if (r.declined || r.cancelled) {
    S.formError = needsOk ? "Not saved: a folder outside the library needs your OK." : "Not saved: opening files by itself needs your OK.";
  } else if (r.ok === false) {
    S.formError = r.error || "That didn't work.";
  } else {
    S.hubPrefs = r.settings;
    resetForm();
    toast("Settings saved", "success");
    if (S.summary && r.settings) S.summary.speed_limit = r.settings.rate_limit_now;
    S.sig.speedq = "";
    schedule(100);
  }
  renderSettings();
}

function revertSettings() {
  if (S.hubPrefs) resetForm();
  renderSettings();
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
$("#openFolder").addEventListener("click", openLibrary);
$("#settingsBtn").addEventListener("click", () => (S.settingsOpen ? closeSettings() : openSettings()));
$("#settingsBack").addEventListener("click", closeSettings);
$("#settingsSave").addEventListener("click", saveSettings);
$("#settingsRevert").addEventListener("click", revertSettings);
$("#speedQuick").addEventListener("change", async (e) => {
  const menuEl = e.target;                 // e.target is cleared once an event inside a shadow root has been dispatched
  const v = menuEl.value;
  if (v === "custom") { S.sig.speedq = ""; renderSpeedQuick(); openSettings("speed"); return; }
  const kbps = Number(v);
  const r = await callTool("dl_prefs", { set: { rate_limit_kbps: kbps } });
  if (r.ok === false) { toast(r.error || "That didn't work", "warn"); }
  else {
    const now = r.settings && r.settings.rate_limit_now;
    if (S.summary && now) S.summary.speed_limit = now;
    toast(kbps > 0 ? "Speed limit: " + fmtLimit(kbps) + (now && now.source === "schedule" ? " (a time window is overriding it right now)" : "") : "Speed limit removed", "success");
    if (r.settings) { S.hubPrefs = r.settings; if (!isDirty()) resetForm(); }
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
  if (e.key === "Escape" && S.settingsOpen) { e.preventDefault(); closeSettings(); return; }
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
