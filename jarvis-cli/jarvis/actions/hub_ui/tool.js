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

// ---- N1: the one toggle every card shows, what it says while it works, and why a row has none.
// The toggle is Pause while a download runs and Resume while it is paused (Start now for one waiting
// for its start time); a failed or cancelled one gets Retry in the same place.  It is drawn only when the
// row's own capabilities carry the verb (cardActions), so nothing is invented for a provider that
// didn't declare it.  The words and icon live here, in one place, for the card and the detail pane.
const TOGGLE_ICONS = { pause: "\u275A\u275A", resume: "\u25B6", retry: "\u21BB" };
const DOING = { pause: "Pausing\u2026", resume: "Resuming\u2026", retry: "Retrying\u2026", cancel: "Cancelling\u2026" };
const SPACE_NOTE = " Space does the same on the selected card.";

// The first of Pause / Resume the row offers; Retry only when it has neither (failed, cancelled).
function primaryAction(row) {
  const acts = cardActions(row);
  const a = acts.find((x) => x.verb === "pause") || acts.find((x) => x.verb === "resume") || acts.find((x) => x.verb === "retry");
  if (!a) return null;
  const timed = a.verb === "resume" && !!row.scheduled_at;
  const label = timed ? "Start now" : a.label;
  const title = a.verb === "pause" ? "Stop it for now and keep what is already downloaded." + SPACE_NOTE
    : timed ? "Start it now, without waiting for its start time." + SPACE_NOTE
      : a.verb === "resume" ? "Start it again from where it stopped." + SPACE_NOTE
        : "Try it again.";
  return Object.assign({}, a, { label: label, icon: TOGGLE_ICONS[a.verb] || "", title: title, doing: timed ? "Starting\u2026" : DOING[a.verb] });
}

// The card's other buttons (Retry on a stalled row, Cancel), in their usual order.
function secondaryActions(row) {
  const p = primaryAction(row);
  return cardActions(row).filter((a) => !p || a.verb !== p.verb);
}

// What the hub says it is about to do (`row.pending` is the verb it was asked: "pause", "cancel").
function pendingWord(pending) {
  const v = String(pending || "").toLowerCase();
  if (!v) return "";
  return DOING[v] || (v.charAt(0).toUpperCase() + v.slice(1) + "\u2026");
}

// Why a row has no Pause / Resume, in words.  null = nothing to explain (it can be paused, or it is
// finished).  `card` says whether the sentence also goes on the list card (Steam and Playnite) or only
// in the detail pane (any other read-only tool keeps the text it always had).
function controlNote(row) {
  if (!row || row.owner !== "provider" || FINAL.has(row.state)) return null;
  const caps = Array.isArray(row.capabilities) ? row.capabilities : [];
  if (caps.indexOf("pause") !== -1 || caps.indexOf("resume") !== -1) return null;
  const id = String(row.provider || "").toLowerCase();
  if (id === "steam") {
    return { title: "Controlled in Steam", card: true,
      text: row.state === "paused" ? "Steam runs this download; resume it in Steam" : "Steam runs this download; pause it in Steam" };
  }
  if (id === "playnite") return { title: "No controls", card: true, text: "Playnite gives no progress or controls" };
  if (row.read_only) return { title: "Read-only", card: false, text: "This tool reports its progress but can't be paused or cancelled from here." };
  return null;
}

// What a control tool's answer tells the person, if anything: { msg, level } or null.  A "Show in
// folder" that fell back to opening the folder answers ok with a `note`: that is a warning, not a plain
// success (N2).
function controlNotice(r, action) {
  const res = r || {};
  if (res.declined) return { msg: "Cancelled", level: "info" };
  if (res.ok === false) return { msg: res.error || "That didn't work", level: "warn" };
  if (res.note) return { msg: String(res.note), level: "warn" };
  if (res.pending && res.pending.length) {
    const what = action && (action.doing || (action.label ? action.label + "\u2026" : ""));
    return { msg: what || "Working\u2026", level: "info" };
  }
  return null;
}

// The second small line a running Steam row shows on its card: the hub's own note for it ("30.6 % ..., reported 20 s ago; Steam isn't
// reporting live progress"), which used to be in the detail pane only.  Nothing is made up: it is `row.message`, and only a running
// Steam row has one (brief 19, step 4).
function cardNote(row) {
  if (!row || row.owner !== "provider" || String(row.provider || "").toLowerCase() !== "steam" || !ACTIVE.has(row.state)) return "";
  return typeof row.message === "string" ? row.message.trim() : "";
}

// ---- brief 19: Dismiss, Clear finished, Show hidden.
// A row another tool reported (Steam, Playnite ...) that is unknown or finished can be HIDDEN.  Hiding deletes no row and
// touches no file, so there is no question first.  The row's own `owner` and `state` decide (the hub sends no "can dismiss"
// capability, and none is invented here); the hub applies the same rule again and says so if it disagrees (a hub download,
// a row that is still running).  A hidden row carries `dismissed_at`; the live stream never lists it, so the panel asks for
// the hidden rows (`/api/hub/list?include_dismissed=1`) only while "Show hidden" is on.  "Show again" calls nothing: it
// lists the row as a normal row in this view (`revealed`), until the panel is reloaded; the hub un-hides a row by itself
// when its tool reports it live again.
const DISMISSABLE = new Set(["done", "failed", "cancelled", "unknown"]);
const DISMISS_TIP = "Hide this row from the list. Nothing is deleted and no file is touched; it comes back by itself if its tool reports it running again, or if you Retry it.";
const CLEAR_TIP = "Hide every finished, failed, cancelled or lost row that another tool (Steam, Playnite ...) reported, and hub downloads you cancelled. Other downloads the hub made itself (failed or finished ones) and the History tab are not touched, and no file is touched.";
const SHOW_AGAIN_TIP = "List this row again for now. The hub keeps it hidden; it comes back for good when its tool reports it running again.";
const SHOW_HIDDEN_TIP = "Also list the rows you hid, dimmed. Nothing hidden is deleted.";

function isHidden(row) { return !!(row && row.dismissed_at); }

// Can this row be dismissed (a hidden row that was listed again counts as shown)?
function canDismiss(row, revealed) {
  if (!row || !DISMISSABLE.has(row.state)) return false;
  // a provider row that is unknown or finished; a hub download only once it failed or was cancelled (a finished one lives in History)
  if (row.owner === "hub" ? (row.state !== "failed" && row.state !== "cancelled") : row.owner !== "provider") return false;
  return !isHidden(row) || !!(revealed && revealed[row.id]);
}

// A hidden row the person has not listed again: it is drawn dimmed and offers "Show again".
function isDimmed(row, revealed) { return isHidden(row) && !(revealed && revealed[row.id]); }

// The rows Clear finished would hide: shown rows only (a hidden one is already hidden).
function dismissableRows(rows, revealed) {
  // Clear finished: provider rows that are done / failed / cancelled / lost, and hub downloads the owner cancelled
  // (a failed hub download is hidden one by one: it may still be worth a Retry)
  return (Array.isArray(rows) ? rows : []).filter((r) => !isHidden(r) && canDismiss(r, revealed) && (r.owner === "provider" || r.state === "cancelled"));
}

// The live rows plus the hidden ones the view lists (all of them while Show hidden is on, else only the ones
// listed again).  A row the hub shows again is in `live` and wins over its old hidden copy.
function mergeHidden(live, hidden, showHidden, revealed) {
  const have = new Set(live.map((r) => r.id));
  const extra = (Array.isArray(hidden) ? hidden : []).filter((r) => isHidden(r) && !have.has(r.id) && (showHidden || (revealed && revealed[r.id])));
  return extra.length ? live.concat(extra) : live;
}

// What the person is told after the hub answered ok.  `res` is service.dismiss's answer.
function dismissNotice(res) {
  const r = res || {};
  const n = Array.isArray(r.dismissed) ? r.dismissed.length : (Number(r.count) || 0);
  if (!n) return { msg: r.note ? String(r.note).charAt(0).toUpperCase() + String(r.note).slice(1) : "Nothing to hide", level: "info" };
  return { msg: (n === 1 ? "Hidden" : "Hid " + n + " rows") + ". No file was touched.", level: "info" };
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
    case "connecting": return row.phase ? row.phase.charAt(0).toUpperCase() + row.phase.slice(1) : "Connecting\u2026";
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

function filterRows(rows, f) {
  const q = String(f.search || "").trim().toLowerCase();
  return rows.filter((r) => {
    if (!viewMatches(f.view || "all", r)) return false;
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
    const tor = mode === "group" ? s.rows.find((r) => r.torrent) : null;     // N24: the files chosen from one torrent
    if (tor) { s.torrent = tor.torrent; s.meta = torrentGroupMeta(s.rows, tor.torrent); }
  });
  if (loose.length) sections.push({ key: "loose", title: "Single downloads", meta: "", rows: loose });
  return sections;
}

// What a collapsed group still says about itself: "9 downloads - 1 running - 8 failed".  Pure.
function sectionSummary(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const count = (states) => list.filter((r) => states.indexOf(r.state) !== -1).length;
  const parts = [list.length + (list.length === 1 ? " download" : " downloads")];
  [["running", ["probing", "connecting", "downloading", "postprocessing"]], ["waiting", ["queued", "stalled"]], ["paused", ["paused"]],
    ["failed", ["failed"]], ["cancelled", ["cancelled"]], ["done", ["done"]]].forEach((g) => {
    const n = count(g[1]);
    if (n) parts.push(n + " " + g[0]);
  });
  return parts.join(DOT);
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
// `base` (dest, group, start_paused and the extras) goes to every call; `perTool` ({ dl_add_ytdlp: {...} }) only to the call
// of that tool.  -> [{ tool, args }]
function addCalls(groups, base, perTool) {
  const calls = [];
  const extra = perTool && typeof perTool === "object" ? perTool : {};
  (groups || []).forEach((g) => {
    // 9C: a tool's OWN options (the video choices, for dl_add_ytdlp) go to that tool only, never to the others (N4).
    const own = Object.prototype.hasOwnProperty.call(extra, g.tool) && extra[g.tool] && typeof extra[g.tool] === "object" ? extra[g.tool] : {};
    if (g.tool === "dl_add_stremio") {
      g.urls.forEach((u) => calls.push({ tool: g.tool, args: Object.assign({}, base, own, { magnet: u }) }));
    } else {
      calls.push({ tool: g.tool, args: Object.assign({}, base, own, { urls: g.urls }) });
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

// ---- the add form, pure half (9C) -----------------------------------------------------------------------------
// What each pasted link is and which engine takes it, what each option group holds, the video / audio choices, and what
// an add came to.  No DOM here.  Every link, file name and reason is DATA: it is only ever drawn with textContent.

const DRY_RUN_CHUNK = 10;                // the hub's dry run answers for at most ten links per call
const ADD_MAX_LINKS = 50;                // what extractInputs takes
const ADD_LIST_SHOWN = 10;               // how many per-link rows the form draws before "and N more"
const START_NOW_WORDS = ["now", "right now", "immediately", "asap", "start now"];   // the hub reads these as "no start time"
const LINK_TOKEN = /^(https?:\/\/|magnet:\?)\S+$/i;
const HASH_TOKEN = /^[0-9a-f]{40}$/i;
const LINK_ENGINE_WORDS = { ytdlp: "yt-dlp", stremio: "Stremio", http: "HTTP", qbittorrent: "qBittorrent" };

function linkEngineWord(engine) {
  const e = String(engine || "").toLowerCase();
  if (!e) return "";
  return Object.prototype.hasOwnProperty.call(LINK_ENGINE_WORDS, e) ? LINK_ENGINE_WORDS[e] : e.slice(0, 24);
}

// What a link is, in words: from its shape first (a magnet is a magnet whoever takes it), then from the engine.
function linkKindOf(url, decision) {
  const t = String(url || "").trim();
  const engine = decision && decision.engine ? String(decision.engine).toLowerCase() : "";
  if (/^magnet:/i.test(t)) return { id: "magnet", one: "magnet", many: "magnets" };
  if (HASH_TOKEN.test(t)) return { id: "hash", one: "info hash", many: "info hashes" };
  if (/^https?:\/\/\S+\.torrent(\?\S*)?$/i.test(t)) return { id: "torrentfile", one: "torrent file link", many: "torrent file links" };
  if (engine === "ytdlp") return { id: "video", one: "video page", many: "video pages" };
  if (engine === "http") return { id: "file", one: "direct file", many: "direct files" };
  if (engine === "stremio" || engine === "qbittorrent") return { id: "torrent", one: "torrent", many: "torrents" };
  return { id: "link", one: "link", many: "links" };
}

// The hub's reason without its engine tail ("YouTube page (yt-dlp engine)" -> "YouTube page"), and nothing when it only repeats the
// kind: the row already says the kind and the engine.
function reasonWords(reason, kind) {
  const r = String(reason || "").replace(/\s*\((?:[^()]*\s)?engine\)\s*$/i, "").trim();
  return kind && r.toLowerCase() === String(kind.one).toLowerCase() ? "" : r;
}

// How many of these links are torrents (by their shape), and the one torrent source when there is exactly one link.
function torrentLinkCount(urls) {
  return (Array.isArray(urls) ? urls : []).filter((u) => /^magnet:/i.test(u) || HASH_TOKEN.test(u) || /^https?:\/\/\S+\.torrent(\?\S*)?$/i.test(u)).length;
}

// The dry run's answers (index by index with the links) as one list of rows plus the sentence for the line under the box.
//   "3 links: 2 video pages (yt-dlp), 1 magnet (Stremio)"   problems and links not yet checked are named after it.
// -> { items: [{ url, checked, ok, kind, engine, engineWord, addTool, reason, problem }], text, spoken, bad, unchecked, ok, video, torrents }
function describeLinks(urls, decisions) {
  const list = Array.isArray(urls) ? urls : [];
  const dec = Array.isArray(decisions) ? decisions : [];
  const items = list.map((u, i) => {
    const d = dec[i] && typeof dec[i] === "object" ? dec[i] : null;
    const ok = !!d && !!d.ok;
    const kind = linkKindOf(u, ok ? d : null);
    return {
      url: String(u), checked: !!d, ok: ok, kind: kind,
      engine: ok ? String(d.engine || "") : "", engineWord: ok ? linkEngineWord(d.engine) : "",
      addTool: d && /^dl_add[a-z0-9_]*$/.test(String(d.add_tool || "")) ? d.add_tool : "",
      reason: d && ok ? reasonWords(d.reason, kind) : "", problem: d && !ok ? String(d.problem || "I can't download that") : "",
    };
  });
  const groups = [];
  const by = {};
  items.filter((x) => x.ok).forEach((x) => {
    const key = x.kind.id + "|" + x.engine;
    if (!by[key]) { by[key] = { kind: x.kind, word: x.engineWord, n: 0 }; groups.push(by[key]); }
    by[key].n++;
  });
  const bad = items.filter((x) => x.checked && !x.ok).length;
  const unchecked = items.filter((x) => !x.checked).length;
  const good = items.length - bad - unchecked;
  const n = items.length;
  let text = "";
  if (n) {
    text = n + (n === 1 ? " link" : " links");
    const parts = groups.map((g) => (n === 1 ? "" : g.n + " ") + (g.n === 1 ? g.kind.one : g.kind.many) + (g.word ? " (" + g.word + ")" : ""));
    if (parts.length) text += ": " + parts.join(", ");
    else if (bad && !unchecked) text += ": " + (n === 1 ? "I can't add it" : "none can be added");
    if (bad && parts.length) text += DOT + bad + " can't be added";
    if (unchecked) text += (parts.length || bad ? DOT : ": ") + unchecked + " not checked yet";
    if (n === 1 && good === 1 && items[0].reason) text += " \u2014 " + items[0].reason;
  }
  const spoken = text.split(DOT).join(". ").replace(/\(([^)]+)\)/g, "taken by $1");
  return {
    items: items, text: text, spoken: spoken, bad: bad, unchecked: unchecked, ok: good,
    video: items.some((x) => x.ok && x.addTool === "dl_add_ytdlp"),
    torrents: items.filter((x) => x.ok && ["magnet", "hash", "torrentfile", "torrent"].indexOf(x.kind.id) !== -1).length,
  };
}

// Split a list into parts of at most `size`; the dry run takes ten links at a time.
function chunked(list, size) {
  const out = [];
  const n = Math.max(1, Math.floor(Number(size) || 1));
  (Array.isArray(list) ? list : []).forEach((x, i) => { if (i % n === 0) out.push([]); out[out.length - 1].push(x); });
  return out;
}

// The answers of the chunked dry runs as ONE list of decisions in the order of the links.  A chunk the hub did not answer
// leaves null in its place (that link is "not checked yet") and its error is kept.  -> { decisions, error }
function mergeDecisions(parts, answers) {
  const decisions = [];
  let error = "";
  (Array.isArray(parts) ? parts : []).forEach((part, i) => {
    const a = answers && answers[i] && typeof answers[i] === "object" ? answers[i] : {};
    const got = Array.isArray(a.decisions) ? a.decisions : null;
    if (!got && !error) error = String(a.error || "");
    part.forEach((_, j) => decisions.push(got && got[j] && typeof got[j] === "object" ? got[j] : null));
  });
  return { decisions: decisions, error: error };
}

// Words the box has to say about what it will NOT use: text that isn't a link, links beyond the 50 it takes.  "" = nothing.
function ignoredNote(text) {
  const seen = [];
  let ignored = 0;
  String(text || "").split(/\s+/).forEach((t) => {
    if (!t) return;
    if (LINK_TOKEN.test(t) || HASH_TOKEN.test(t)) { if (seen.indexOf(t) === -1) seen.push(t); } else ignored++;
  });
  const parts = [];
  if (seen.length > ADD_MAX_LINKS) parts.push("only the first " + ADD_MAX_LINKS + " links are used");
  if (ignored && seen.length) parts.push(ignored + (ignored === 1 ? " piece of text that isn't a link is" : " pieces of text that aren't links are") + " ignored");
  return parts.join(DOT);
}

// A folder text that is a full path (a drive, a leading slash, a UNC share, ~): the hub asks before saving there.
function looksAbsolute(text) {
  return /^(?:[A-Za-z]:[\\/]|[\\/]|~(?:[\\/]|$))/.test(String(text || "").trim());
}

// The button's words, from what the form will do.
function addButtonLabel(o) {
  const x = o || {};
  const n = Math.max(0, Math.floor(Number(x.count) || 0));
  if (!n) return "Add to downloads";
  const things = n + (n === 1 ? " download" : " downloads");
  const start = String(x.start || "").trim();
  if (start && START_NOW_WORDS.indexOf(start.toLowerCase()) === -1) return "Schedule " + things;
  return x.paused ? "Add " + things + ", paused" : "Add " + things;
}

// What each closed option group still says about itself ("" = nothing set, the form shows its usual words).
// f: { dest, open, start, paused, limit, unit, notify, channels, events, group }
function groupStates(f) {
  const x = f || {};
  const where = [];
  const dest = String(x.dest || "").trim();
  if (dest) where.push(looksAbsolute(dest) ? dest + " (outside the library: asks first)" : "in " + dest);
  if (x.open && x.open !== "default") where.push("then " + openLabel(x.open).toLowerCase());
  const when = [];
  const start = String(x.start || "").trim();
  if (start && START_NOW_WORDS.indexOf(start.toLowerCase()) === -1) when.push("starts " + start);
  else if (x.paused) when.push("added paused");
  const lim = parseLimit(x.limit, x.unit);
  const speed = lim.error ? "check the speed" : lim.kbps > 0 ? "limit " + fmtLimit(lim.kbps) : "";
  const tell = [];
  if (x.notify === "on") tell.push("always");
  if (x.notify === "off") tell.push("never");
  if (x.channels && x.channels.length) tell.push("through " + x.channels.map(channelLabel).join(", "));
  if (x.events && x.events.length) tell.push("when it " + x.events.map((e) => (ITEM_EVENTS.find((v) => v.id === e) || { label: e }).label.toLowerCase()).join(" / "));
  return { where: where.join(DOT), when: when.join(DOT), speed: speed, tell: tell.join(DOT), batch: String(x.group || "").trim() };
}

// ---- yt-dlp's own choices.  These are the arguments of dl_add_ytdlp (see _ALL_PROPS in download_hub.py) and go to that tool
// ONLY (N4): never to dl_add, never to dl_add_stremio.  The lists below are that schema's own enums.
const VIDEO_QUALITIES = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p"];
const VIDEO_CONTAINERS = ["mp4", "mkv", "webm"];
const VIDEO_CODECS = ["h264", "vp9", "av1"];
const AUDIO_FORMATS = ["mp3", "m4a", "opus", "wav", "flac", "aac", "vorbis", "alac"];
const VIDEO_FLAGS = ["embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs", "skip_if_downloaded"];

// f: { mode, quality, container, video_codec, audio_format, audio_quality, <the six flags>, sub_langs, sponsorblock_remove }
// Only what differs from the tool's own default is sent, and only what fits the mode.  -> { args }
function videoOptionsFromForm(f) {
  const x = f || {};
  const out = {};
  if (x.mode === "audio") {
    out.mode = "audio";
    if (AUDIO_FORMATS.indexOf(x.audio_format) !== -1 && x.audio_format !== "mp3") out.audio_format = x.audio_format;
    const aq = String(x.audio_quality || "").trim();
    if (aq) out.audio_quality = aq.slice(0, 12);
  } else {
    if (VIDEO_QUALITIES.indexOf(x.quality) !== -1 && x.quality !== "best") out.quality = x.quality;
    if (VIDEO_CONTAINERS.indexOf(x.container) !== -1 && x.container !== "mp4") out.container = x.container;
    if (VIDEO_CODECS.indexOf(x.video_codec) !== -1) out.video_codec = x.video_codec;
  }
  VIDEO_FLAGS.forEach((k) => { if (x[k] === true) out[k] = true; });
  const langs = String(x.sub_langs || "").trim();
  if (langs && (x.write_subs === true || x.embed_subs === true)) out.sub_langs = langs.slice(0, 60);
  const sb = String(x.sponsorblock_remove || "").trim();
  if (sb) out.sponsorblock_remove = sb.slice(0, 120);
  return { args: out };
}

// What a question the hub asked back (needs_clarification) offers the person.  Buttons only where the hub's own call is
// one the form can make: a playlist for one link (playlist="all"), yt-dlp's generic extractor for one link
// (generic=true), the torrent's file list.  -> { message, label, tool, extra, urls } (label "" = just the words)
function followUpFor(result, entry) {
  const r = result || {};
  const urls = entry && Array.isArray(entry.urls) ? entry.urls : [];
  const tool = entry && entry.tool ? entry.tool : "";
  const message = String(r.message || "The hub needs an answer before it adds this.");
  const base = { message: message, label: "", tool: tool, extra: {}, urls: urls };
  if (r.torrent && urls.length === 1) return Object.assign(base, { label: "Choose files\u2026", tool: "", source: urls[0] });
  if (tool === "dl_add_ytdlp" && urls.length === 1 && r.playlist && r.call) {
    const cap = Math.max(1, Math.round(Number(r.playlist.cap) || 50));
    return Object.assign(base, { label: "Add the playlist (up to " + cap + " videos)", extra: { playlist: "all" } });
  }
  if (tool === "dl_add_ytdlp" && urls.length === 1 && r.generic_available && r.call) {
    return Object.assign(base, { label: "Let yt-dlp try anyway", extra: { generic: true } });
  }
  return base;
}

// A start time from the hub ("2026-10-28T03:00:00") for people.
function startWords(iso) {
  const t = String(iso || "");
  return /^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(t) ? t.slice(0, 16).replace("T", " ") : t.replace("T", " ");
}

// What a set of add calls came to.  entries: [{ tool, urls, result, why }] (result null = not tried, `why` says why);
// o: { paused }.  The links that were NOT added stay in the box (`keep`) so they can be fixed and sent again.
// -> { tone: ok | partial | ask | info | bad, headline, lines: [{ url, text }], asks, note, queued, when, paused, dup, total, keep, select }
function describeAdd(entries, o) {
  const opt = o || {};
  const list = (Array.isArray(entries) ? entries : []).filter((e) => e && typeof e === "object");
  const lines = [];
  const asks = [];
  const keep = [];
  const oks = [];
  let total = 0;
  let declined = 0;
  list.forEach((e) => {
    const urls = (Array.isArray(e.urls) ? e.urls : []).map(String);
    const r = e.result && typeof e.result === "object" ? e.result : null;
    total += urls.length;
    const add = (u, text) => lines.push({ url: String(u || ""), text: String(text || "") });
    if (!r) { urls.forEach((u) => { keep.push(u); add(u, e.why || "Not tried."); }); return; }
    if (r.ok) {
      oks.push(r);
      const skipped = Array.isArray(r.skipped) ? r.skipped : [];
      const inputs = skipped.map((s) => String((s && s.input) || ""));
      urls.forEach((u) => { if (inputs.indexOf(u.slice(0, 120)) !== -1) keep.push(u); });
      skipped.forEach((s) => add(s && s.input, (s && s.problem) || "Refused."));
      return;
    }
    urls.forEach((u) => keep.push(u));
    if (r.needs_clarification) { asks.push(followUpFor(r, e)); return; }
    if (r.declined || r.cancelled) declined++;
    const why = r.error ? String(r.error) : r.declined || r.cancelled ? "You did not confirm." : "That didn't work.";
    urls.forEach((u) => add(u, why));
  });
  const merged = oks.length ? mergeAddResults(oks) : null;
  const added = merged ? merged.added || [] : [];
  const queued = merged ? Number(merged.count) || 0 : 0;
  const dup = added.filter((a) => a && a.duplicate).length;
  const timed = added.find((a) => a && a.start_at);
  const when = timed ? startWords(timed.start_at) : "";
  const paused = !!opt.paused && !when;
  const things = (n) => n + (n === 1 ? " download" : " downloads");
  const tail = (when ? ", will start at " + when : paused ? ", paused" : "") + (dup ? " (" + dup + " already in the list)" : "");
  let tone;
  let headline;
  if (queued && !lines.length && !asks.length) {
    tone = "ok";
    headline = (when ? "Scheduled " : "Added ") + things(queued) + tail;
    if (paused) headline += ": start " + (queued === 1 ? "it" : "them") + " from the list when you are ready";
  } else if (queued) {
    tone = "partial";
    headline = "Added " + queued + " of " + total + (total === 1 ? " link" : " links") + tail;
  } else if (asks.length && !lines.length) {
    tone = "ask";
    headline = "Nothing was added yet: it needs your answer.";
  } else {
    tone = lines.length && declined === list.filter((e) => e.result).length && !asks.length ? "info" : "bad";
    headline = "Nothing was added.";
  }
  return {
    tone: tone, headline: headline, lines: lines, asks: asks, note: merged && merged.note ? String(merged.note) : "", queued: queued, when: when,
    paused: paused, dup: dup, total: total, keep: keep,
    select: tone === "ok" && queued === 1 && added[0] && added[0].id ? String(added[0].id) : "",
  };
}

// The links left in the box after an add: everything the entries did not touch, plus the ones that were not added.
function remainingLinks(allUrls, entries, keep) {
  const touched = [];
  (Array.isArray(entries) ? entries : []).forEach((e) => (e && Array.isArray(e.urls) ? e.urls : []).forEach((u) => touched.push(String(u))));
  const kept = Array.isArray(keep) ? keep : [];
  return (Array.isArray(allUrls) ? allUrls : []).filter((u) => touched.indexOf(u) === -1 || kept.indexOf(u) !== -1);
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
  if (start && START_NOW_WORDS.indexOf(start.toLowerCase()) === -1) out.start_at = start;
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

// ---- History (brief 25, N21).  The hub does the filtering, sorting and counting (`history` answers one page plus a
// summary of the WHOLE filtered set); everything here only words, groups and selects what it was given.
const HISTORY_PAGE = 100;                // rows asked for per page (the hub's cap is 500)
const HISTORY_SORTS = [["newest", "Newest first"], ["largest", "Largest first"], ["slowest", "Slowest first"], ["name", "Name A to Z"]];
const HISTORY_EXPORT_PAGES = 40;         // an export reads at most this many pages of 500
const HISTORY_REDOWNLOAD_MAX = 50;       // a bulk Re-download queues at most this many in one go

function dayStartMs(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }

// The group a row sits in when the list is newest first: Today, Yesterday, This week (2 to 6 days ago), then its month.
function historyGroup(row, nowMs) {
  const t = num(row.finished_at) !== null ? row.finished_at : num(row.created_at);
  if (t === null) return { key: "earlier", label: "Earlier" };
  const d = new Date(t * 1000);
  const now = new Date(nowMs === undefined || nowMs === null ? Date.now() : nowMs);
  const age = Math.round((dayStartMs(now) - dayStartMs(d)) / 86400000);
  if (age <= 0) return { key: "today", label: "Today" };
  if (age === 1) return { key: "yesterday", label: "Yesterday" };
  if (age < 7) return { key: "week", label: "This week" };
  return { key: "m:" + d.getFullYear() + "-" + (d.getMonth() + 1), label: d.toLocaleDateString([], { month: "long", year: "numeric" }) };
}

// Consecutive rows with the same group, in the order given.  Only a newest-first list is grouped by day: under any other
// sort the days would be scattered, so it is one flat group.
function historyGroups(rows, nowMs, sort) {
  if (sort && sort !== "newest") return [{ key: "all", label: "", rows: (rows || []).slice() }];
  const out = [];
  (rows || []).forEach((r) => {
    const g = historyGroup(r, nowMs);
    if (!out.length || out[out.length - 1].key !== g.key) out.push({ key: g.key, label: g.label, rows: [] });
    out[out.length - 1].rows.push(r);
  });
  return out;
}

// A date box's value ("2026-10-24") as the start / end of that local day, in epoch seconds.
function historyRange(from, to) {
  const out = { since: null, until: null, error: "" };
  const day = (text) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || "")); return m ? [Number(m[1]), Number(m[2]) - 1, Number(m[3])] : null; };
  const f = day(from), t = day(to);
  if ((from && !f) || (to && !t)) { out.error = "Pick the dates again."; return out; }
  if (f) out.since = new Date(f[0], f[1], f[2], 0, 0, 0).getTime() / 1000;
  if (t) out.until = new Date(t[0], t[1], t[2], 23, 59, 59).getTime() / 1000;
  if (out.since !== null && out.until !== null && out.since > out.until) out.error = "The date range ends before it starts.";
  return out;
}

// The query string of one History request.  `f`: {state, search, sort, from, to}; `o`: {limit, before, checkFiles}.
function historyQuery(f, o) {
  const x = f || {}, opt = o || {};
  const parts = ["limit=" + Math.max(1, Math.min(500, Math.round(num(opt.limit) || HISTORY_PAGE)))];
  parts.push("sort=" + encodeURIComponent(HISTORY_SORTS.some((s) => s[0] === x.sort) ? x.sort : "newest"));
  if (x.state && x.state !== "all" && HISTORY_STATES.indexOf(x.state) !== -1) parts.push("states=" + x.state);
  const q = String(x.search || "").trim();
  if (q) parts.push("search=" + encodeURIComponent(q.slice(0, 80)));
  const r = historyRange(x.from, x.to);
  if (!r.error) {
    if (r.since !== null) parts.push("since=" + Math.round(r.since));
    if (r.until !== null) parts.push("until=" + Math.round(r.until));
  }
  if (opt.before) parts.push("before=" + encodeURIComponent(String(opt.before)));
  if (opt.checkFiles) parts.push("check_files=1");
  return parts.join("&");
}

function historyFiltersActive(f) {
  const x = f || {};
  return !!((x.state && x.state !== "all") || String(x.search || "").trim() || x.from || x.to);
}

// "128 downloads . 14.2 GB . 3 failed" for the whole filtered set (the hub counted it, not this page).
function historySummaryText(summary, truncated) {
  const s = summary || {};
  const n = num(s.count) || 0;
  const parts = [n + (n === 1 ? " download" : " downloads")];
  if (num(s.total_bytes) > 0) parts.push(fmtBytes(s.total_bytes));
  if (num(s.failed) > 0) parts.push(s.failed + " failed");
  return parts.join(DOT) + (truncated ? " (the newest " + HISTORY_SCAN_NOTE + " counted)" : "");
}
const HISTORY_SCAN_NOTE = "20,000";

function fmtTook(sec) {
  const s = num(sec);
  if (s === null || s < 1) return "";
  if (s < 60) return Math.round(s) + " s";
  if (s < 3600) { const r = Math.round(s % 60); return Math.floor(s / 60) + " min" + (r ? " " + r + " s" : ""); }
  const m = Math.floor((s % 3600) / 60);
  return Math.floor(s / 3600) + " h " + (m < 10 ? "0" : "") + m + " min";
}

function historyDuration(row) {
  return num(row.started_at) !== null && num(row.finished_at) !== null ? fmtTook(row.finished_at - row.started_at) : "";
}

// The name of the file a finished download ended up as (the last part of its path), or "".
function historyDestName(row) {
  if (row.state !== "done" || typeof row.dest !== "string") return "";
  const parts = row.dest.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

// Why a failed row failed, in one sentence: the first sentence of what the hub recorded; never a guess.
function historyReason(row) {
  if (row.state !== "failed") return "";
  const text = String(row.error || "").replace(/\s+/g, " ").trim();
  if (!text) return "No reason was recorded.";
  const first = text.split(/(?<=[.!?])\s/)[0];
  return first.length > 160 ? first.slice(0, 157) + "\u2026" : first;
}

// Everything one History row says, as plain data (the page only draws it).
function historyRowModel(row, nowMs) {
  const when = fmtWhen(row.finished_at, nowMs);
  const size = num(row.bytes_total) !== null ? fmtBytes(row.bytes_total) : (num(row.bytes_done) !== null ? fmtBytes(row.bytes_done) : "");
  return {
    id: row.id, title: row.title || row.id, state: row.state, tone: row.state === "done" ? "ok" : row.state,
    badge: sourceLabel(row), size: size, took: historyDuration(row),
    when: when ? (row.state === "failed" ? "failed " : row.state === "cancelled" ? "cancelled " : "") + when : "",
    dest: historyDestName(row), reason: historyReason(row), missing: row.state === "done" && row.file_exists === false,
    selectable: row.owner === "hub",
  };
}

// Shift-click: every id from `a` to `b` in the order shown (just `b` when either is not in the list).
function selectionRange(ids, a, b) {
  const i = ids.indexOf(a), j = ids.indexOf(b);
  if (i === -1 || j === -1) return [b];
  return ids.slice(Math.min(i, j), Math.max(i, j) + 1);
}

// The confirmations of the two bulk buttons: they say how many, and that no file is deleted.
function historyRemoveText(n) {
  return { title: "Remove " + n + " from the history?",
    body: "Remove " + n + " list " + (n === 1 ? "entry" : "entries") + " from the history?\n\nOnly the list entries go. No downloaded file is deleted or touched.", label: "Remove" };
}
function historyRedownloadText(n) {
  return { title: "Download " + n + " again?",
    body: "Queue " + n + " " + (n === 1 ? "download" : "downloads") + " again?\n\nEach one starts as a new download from the same link and the same folder choice. The old entries stay in the history and no file is deleted.", label: "Download again" };
}

function historyRetentionNote(days) {
  const d = num(days);
  return d !== null && d > 0 ? "The hub removes history entries older than " + d + (d === 1 ? " day" : " days") + ". Downloaded files are never deleted." : "";
}

// Which of the three empty states the list is in, or null when there are rows to show.
function historyEmpty(h) {
  if (!h || h.error || !h.loaded) return null;
  if (num(h.summary && h.summary.count) > 0) return null;
  return historyFiltersActive(h)
    ? { kind: "filtered", text: "Nothing matches these filters." }
    : { kind: "none", text: "Nothing has finished yet. Finished, failed and cancelled downloads are listed here." };
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

// ---- the Sources tab (brief 24).  Everything an engine card says comes from the hub's `sources` answer; the page
// decides only the word, the colour and which settings block a Configure button lands on.
const ENGINE_WORDS = { ready: "Ready", needs_setup: "Needs setup", not_running: "Not running", off: "Off", broken: "Broken" };
const ENGINE_TONES = { ready: "done", needs_setup: "stalled", not_running: "stalled", off: "stale", broken: "failed" };
// The block of Settings > Downloads an engine's Configure button scrolls to (data-settings-anchor in downloads-settings.js).
// Brief 25: each engine has its own collapsed block under Sources (Stremio's holds the Stremio card); the block's anchor is the
// engine's name.  Opening a block by its anchor unfolds it.
const CONFIGURE_ANCHORS = { stremio: "stremio", http: "http", ytdlp: "yt-dlp", qbittorrent: "qbittorrent" };
const PROVIDER_SETTINGS_ANCHOR = "other-tools-downloads";
function engineWord(state) { return ENGINE_WORDS[state] || "Unknown"; }
function engineTone(state) { return ENGINE_TONES[state] || "stale"; }
// "magnets and torrent files · 1 active · 0 waiting"
function engineLine(e) {
  const parts = [];
  if (e && e.handles) parts.push(e.handles);
  parts.push((num(e && e.active) || 0) + " active");
  parts.push((num(e && e.queued) || 0) + " waiting");
  return parts.join(DOT);
}
// {anchor} for an engine that has a settings block to land on, else null (no button).
function engineConfigure(e) {
  const anchor = e ? CONFIGURE_ANCHORS[e.configure] : "";
  return anchor ? { anchor: anchor } : null;
}
// What the card says in place of a Configure button: where it is set up, or that it has no settings.
function engineSettingsNote(e) {
  if (!e || engineConfigure(e)) return "";
  return e.config_file ? "Set up in " + e.config_file + "." : "No settings.";
}
function sourcesSummary(d) {
  if (!d) return "reading the sources\u2026";
  const engines = d.engines || [];
  const progs = (d.providers || []).filter((p) => p.registered !== false);
  const by = (s) => engines.filter((e) => e.state === s).length;
  const bits = [engines.length + (engines.length === 1 ? " engine" : " engines")];
  if (by("ready")) bits.push(by("ready") + " ready");
  if (by("needs_setup")) bits.push(by("needs_setup") + " needs setup");
  if (by("not_running")) bits.push(by("not_running") + " not running");
  if (by("off")) bits.push(by("off") + " off");
  if (by("broken")) bits.push(by("broken") + " broken");
  const bad = progs.filter((p) => providerTone(p) === "bad").length;
  const quiet = progs.filter((p) => providerTone(p) === "quiet").length;
  return bits.join(DOT) + " | " + progs.length + (progs.length === 1 ? " program" : " programs") + (bad ? DOT + bad + " with an error" : "") + (quiet ? DOT + quiet + " gone quiet" : "");
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

// Why a row has no arrows and no grip, in a sentence (the card's tooltip, N15).  "" when the row HAS them, and for a
// finished row, where "order" means nothing.  Pure.
function reorderNote(row, live) {
  if (!row) return "";
  if (row.owner !== "hub") return "Only downloads the hub itself queues can be put in a different order. " + (row.provider_label || "This program") + " runs this one.";
  if (row.state === "paused") return "A paused download isn't waiting in the queue. Resume it first; then it can be moved.";
  if (row.state === "queued") {
    if (!live || num(row.priority) === null) return "Moving a waiting download needs the live connection to the hub, and it is not up right now. It comes back by itself.";
    return "";
  }
  if (ACTIVE.has(row.state) || row.state === "stalled") return "Already started. Only downloads that are still waiting can be put in a different order.";
  return "";
}


// ---- dragging a whole group (the "Group by: Batch" header) --------------------------------------------------
// A section key is "g:<group id>".  A group moves as ONE block of its waiting rows; `ids` is the waiting queue in
// the order it will run (queueOrder), `rows` the panel's rows (they say which group a row is in).  All pure.

function groupIdOf(key) {
  const m = /^g:(.+)$/.exec(String(key || ""));
  return m ? m[1] : "";
}
// The ids of the waiting rows of one group, in the order `ids` has them.
function groupWaiting(ids, rows, key) {
  const gid = groupIdOf(key);
  if (!gid) return [];
  const mine = {};
  (rows || []).forEach((r) => { if (r && r.group_id === gid) mine[r.id] = true; });
  return (ids || []).filter((id) => mine[id] === true);
}
// The groups that have waiting rows, in the order their first waiting row comes.
function groupOrderKeys(ids, rows) {
  const by = {};
  (rows || []).forEach((r) => { if (r && r.group_id) by[r.id] = r.group_id; });
  const out = [];
  (ids || []).forEach((id) => { const g = by[id]; if (g && out.indexOf("g:" + g) === -1) out.push("g:" + g); });
  return out;
}
// `ids` with group `groupKey`'s waiting rows moved, as one block and in their own order, before the first waiting row of
// `targetKey` (or after its last one when `after`).  A key that isn't a group, the same group twice, a group with no
// waiting rows on either side: the order comes back unchanged.  Never mutates `ids`.
function moveGroupInOrder(ids, rows, groupKey, targetKey, after) {
  const list = (ids || []).slice();
  if (!groupIdOf(groupKey) || !groupIdOf(targetKey) || groupKey === targetKey) return list;
  const block = groupWaiting(list, rows, groupKey);
  const target = groupWaiting(list, rows, targetKey);
  if (!block.length || !target.length) return list;
  const rest = list.filter((id) => block.indexOf(id) === -1);
  const at = rest.indexOf(after ? target[target.length - 1] : target[0]);
  if (at === -1) return list;
  rest.splice.apply(rest, [after ? at + 1 : at, 0].concat(block));
  return rest;
}
// One group up (-1) or down (+1) among the groups that are waiting, or null at the end / for a group that isn't waiting.
function stepGroupInOrder(ids, rows, groupKey, delta) {
  const keys = groupOrderKeys(ids, rows);
  const i = keys.indexOf(groupKey);
  const j = i + delta;
  if (i === -1 || j < 0 || j >= keys.length) return null;
  const next = moveGroupInOrder(ids, rows, groupKey, keys[j], delta > 0);
  return JSON.stringify(next) === JSON.stringify(ids) ? null : next;
}
// A group may be dragged only when one of its rows may (canReorder): waiting, the hub's own, the live order known.
function canReorderGroup(rows, live) {
  return (rows || []).some((r) => canReorder(r, live));
}
// Why a group header has no grip and no arrows, in the wording of reorderNote.  "" when it has them.
function groupReorderNote(rows, live) {
  if (canReorderGroup(rows, live)) return "";
  if (!live) return "Moving a waiting download needs the live connection to the hub, and it is not up right now. It comes back by itself.";
  return "Only downloads that are still waiting can be put in a different order. Nothing in this group is waiting.";
}

// ---- the torrent file picker (N24) ---------------------------------------------------------------------------
// The hub sends a torrent's files as [{index, path, size, video?, sample?, refused?, in_group?}] (paths are already cleaned
// and cut; they are still only DATA here: everything is drawn with textContent, never as HTML).  All pure.

const PICK_MAX = 100;
const PICK_MARGIN = 1.1;

// The text of the add box as ONE torrent source (a magnet, a 40-letter hash, a .torrent link or the full path of a
// .torrent file on this PC), else "".
function torrentSource(text) {
  let t = String(text || "").trim();
  if (!t || t.length > 4096 || /[\r\n]/.test(t)) return "";
  t = t.replace(/^"(.*)"$/, "$1");
  if (/^magnet:\?\S+$/i.test(t) || /^[0-9a-f]{40}$/i.test(t)) return t;
  if (/^https?:\/\/\S+\.torrent(\?\S*)?$/i.test(t)) return t;
  if (/^(?:[A-Za-z]:[\\/]|\/|~[\\/]|\\\\).+\.torrent$/i.test(t)) return t;
  return "";
}
// A file the person may tick: not refused (an unsafe name), not already in the group, not empty.
function selectableFile(f) {
  return !!f && !f.refused && !f.in_group && (num(f.size) || 0) > 0;
}
// The files as folders: {name, path, dirs: [], files: [], ids: [selectable indexes below], count, size}.  Folder names come
// from the swarm: the lookup table has NO prototype, so a folder called "__proto__" or "constructor" is just a name.
function torrentTree(files) {
  const mk = (name, path) => ({ name: name, path: path, dirs: [], files: [], ids: [], count: 0, size: 0, map: Object.create(null) });
  const root = mk("", "");
  (files || []).forEach((f) => {
    const parts = String(f.path || "").split("/");
    const leaf = parts.pop();
    let node = root;
    parts.forEach((part) => {
      let child = node.map[part];
      if (!child) { child = mk(part, node.path ? node.path + "/" + part : part); node.map[part] = child; node.dirs.push(child); }
      node = child;
    });
    node.files.push(Object.assign({}, f, { leaf: leaf }));
  });
  (function total(node) {
    node.files.forEach((f) => { node.count += 1; node.size += num(f.size) || 0; if (selectableFile(f)) node.ids.push(f.index); });
    node.dirs.forEach((d) => { total(d); node.count += d.count; node.size += d.size; d.ids.forEach((i) => node.ids.push(i)); });
  })(root);
  return root;
}
// The quick choices: which indexes each one ticks.
const PICK_QUICK = [["videos", "All videos"], ["biggest", "Biggest video"], ["nosample", "All but samples"], ["all", "Everything"], ["none", "None"]];
function pickerQuick(files, kind) {
  const ok = (files || []).filter(selectableFile);
  const videos = ok.filter((f) => f.video && !f.sample);
  if (kind === "videos") return videos.map((f) => f.index);
  if (kind === "nosample") return ok.filter((f) => !f.sample).map((f) => f.index);
  if (kind === "all") return ok.map((f) => f.index);
  if (kind === "biggest") {
    const pool = videos.length ? videos : ok;
    if (!pool.length) return [];
    return [pool.reduce((a, b) => ((num(b.size) || 0) > (num(a.size) || 0) ? b : a)).index];
  }
  return [];
}
function pickerFilter(files, query) {
  const q = String(query || "").trim().toLowerCase();
  const list = (files || []).slice();
  return q ? list.filter((f) => String(f.path || "").toLowerCase().indexOf(q) !== -1) : list;
}
// What is ticked: how many, how big, and whether it breaks a limit (100 files a go; the free space with the hub's margin).
function pickerTotals(files, selected, free) {
  const sel = selected || {};
  let count = 0;
  let bytes = 0;
  (files || []).forEach((f) => { if (sel[f.index] && selectableFile(f)) { count += 1; bytes += num(f.size) || 0; } });
  const room = num(free);
  return { count: count, bytes: bytes, free: room, tooMany: count > PICK_MAX, tooBig: room !== null && bytes * PICK_MARGIN > room };
}
function pickerTotalText(t) {
  if (!t.count) return "Nothing chosen yet";
  return t.count + (t.count === 1 ? " file" : " files") + DOT + fmtBytes(t.bytes) + (t.free !== null ? " of " + fmtBytes(t.free) + " free" : "");
}
// "" when Add may go; else the reason it may not (shown under the total, and the button is off).
function pickerBlock(t) {
  if (!t.count) return "Tick at least one file.";
  if (t.tooMany) return "That is more than " + PICK_MAX + " files. Untick some; add the rest in a second go.";
  if (t.tooBig) return "These files need more room than is free. Untick some, or free some space.";
  return "";
}
// The header of a torrent group: how many files were chosen out of how many the torrent has.
function torrentGroupMeta(rows, info) {
  const done = (rows || []).filter((r) => r.state === "done").length;
  const of = info && num(info.files) !== null && info.files > rows.length ? DOT + rows.length + " of " + info.files + " files" : "";
  return done + "/" + rows.length + " done" + of;
}

// ---- Updates (N23 part f): the words and the order of the Updates tab.  Pure.  Every name, version, source and message in a
// finding comes from another program's output, so these functions only CHOOSE words and order; the panel draws them with
// textContent.  The hub never compares versions (N23 part a): "installed -> available" is the two texts the source gave.
const UPDATES_NOTE = "This list only tells you. Nothing here installs or upgrades anything: ask Jarvis, or update it in the program itself. "
  + "A program whose update checks are switched off in Settings > Downloads is not listed.";

function updText(v) { return typeof v === "string" ? v.trim() : ""; }

function updateVersionText(f) {
  return (updText(f && f.installed) || "version not stated") + " \u2192 " + (updText(f && f.available) || "a newer version");
}

function updateSizeText(f) {
  const n = num(f && f.size_bytes);
  return n !== null && n > 0 ? fmtBytes(n) : "";
}

// The small line under a finding: the size when the source gave one, the source's name, and when it was last confirmed.
function updateMetaParts(f) {
  const out = [];
  const size = updateSizeText(f);
  if (size) out.push(size);
  const via = updText(f && f.source);
  if (via) out.push("via " + via);
  if (num(f && f.age_s) !== null) out.push("checked " + fmtAgo(f.age_s));
  return out;
}

// The health line of one provider: when it was last checked, what failed, and which part (the `problems` the hub keeps).
function updateHealthText(p) {
  if (!p || typeof p !== "object") return { text: "", tone: "" };
  const age = num(p.age_s) !== null ? fmtAgo(p.age_s) : "";
  const problems = (Array.isArray(p.problems) ? p.problems : []).map((x) => {
    const src = updText(x && x.source);
    return (src ? src + ": " : "") + (updText(x && x.error) || "it failed");
  });
  if (p.status === "checking") return { text: "Checking now\u2026", tone: "" };
  if (p.status === "failed") {
    const why = updText(p.error) || "it failed";
    return { text: "The last check failed: " + why + (p.count && age ? ". The list below is from " + age + "." : "."), tone: "bad" };
  }
  if (p.status === "partial") return { text: "Checked " + age + ", but not everything: " + problems.join("; ") + ".", tone: "warn" };
  if (p.status === "never") return { text: "Not checked yet. The first check runs a few minutes after the hub daemon starts, or press Check now.", tone: "" };
  if (p.status === "pushed") return { text: "This program reports its own updates; the hub does not check it.", tone: "" };
  const h = num(p.update_interval_h);
  return { text: "Checked " + (age || "a while ago") + (h !== null ? ". Looks again every " + (Math.round(h * 10) / 10) + " h." : "."), tone: "" };
}

// One entry per program that has findings or a health line, programs A to Z, findings A to Z inside.
function updateGroups(data) {
  const d = data && typeof data === "object" ? data : {};
  const rows = Array.isArray(d.updates) ? d.updates.filter((f) => f && typeof f === "object" && updText(f.provider) && updText(f.id)) : [];
  const byId = new Map();
  (Array.isArray(d.providers) ? d.providers : []).forEach((p) => {
    if (p && updText(p.provider)) byId.set(p.provider, { id: p.provider, label: updText(p.label) || p.provider, rows: [], health: p });
  });
  rows.forEach((f) => {
    if (!byId.has(f.provider)) byId.set(f.provider, { id: f.provider, label: updText(f.provider_label) || f.provider, rows: [], health: null });
    byId.get(f.provider).rows.push(f);
  });
  const key = (v) => String(v || "").toLowerCase();
  const groups = Array.from(byId.values());
  groups.forEach((g) => g.rows.sort((a, b) => key(a.title || a.id).localeCompare(key(b.title || b.id)) || key(a.id).localeCompare(key(b.id))));
  groups.sort((a, b) => key(a.label).localeCompare(key(b.label)) || key(a.id).localeCompare(key(b.id)));
  return groups;
}

// The line above the list.
function updateSummaryText(data) {
  if (!data || typeof data !== "object") return "";
  const n = num(data.count) || 0;
  const provs = Array.isArray(data.providers) ? data.providers : [];
  if (!provs.length && !n) return "No program has an update check yet.";
  let t = n === 0 ? "No updates found" : n + (n === 1 ? " update" : " updates") + " available";
  const ages = provs.map((p) => num(p && p.age_s)).filter((v) => v !== null);
  if (ages.length) t += DOT + "last checked " + fmtAgo(Math.min.apply(null, ages));
  if (data.daemon_alive === false) t += DOT + "the hub daemon isn't running, so nothing is checked";
  return t;
}

// The number on the tab (nothing when there are no findings; the badge is the signal, N23c).
function updateBadge(data) {
  const n = data ? num(data.count) : null;
  return n !== null && n > 0 ? (n > 99 ? "99+" : String(n)) : "";
}

// What to say after Check now.  `started` is how many checks the daemon will begin.
function updateCheckNotice(r) {
  const res = r && Array.isArray(r.results) ? r.results.filter((x) => x && typeof x === "object") : [];
  if (!res.length) return { text: String((r && r.message) || "No program has an update check that is switched on."), level: "info", started: 0 };
  const started = res.filter((x) => x.status === "requested").length;
  if (started) return { text: started === 1 ? "Checking now. The answer appears here in a few seconds."
    : "Checking " + started + " programs now. The answers appear here in a few seconds.", level: "info", started: started };
  if (res.some((x) => x.status === "already_checking")) return { text: "Already checking. The answer appears here when it finishes.", level: "info", started: 0 };
  const first = res.find((x) => updText(x.message));
  return { text: first ? updText(first.message).slice(0, 200) : "Nothing was started.", level: "warn", started: 0 };
}

// How long to wait before the next read: quick while a check runs or was just asked for, otherwise slow; the badge still follows.
function updatesPollDelay(o) {
  const x = o || {};
  if (x.visible) return x.checking || x.recent ? 4000 : 20000;
  return 60000;
}

// What the list shows, with ages in whole minutes, so an unchanged list is not redrawn on every read.
function updatesSignature(data) {
  const d = data && typeof data === "object" ? data : {};
  const mins = (v) => (num(v) === null ? null : Math.round(v / 60));
  return JSON.stringify({
    u: (Array.isArray(d.updates) ? d.updates : []).map((f) => [f.provider, f.id, f.title, f.installed, f.available, f.size_bytes, f.source, mins(f.age_s)]),
    p: (Array.isArray(d.providers) ? d.providers : []).map((p) => [p.provider, p.label, p.status, p.error, p.problems, p.count, p.can_check, mins(p.age_s), p.update_interval_h]),
    a: d.daemon_alive === true,
  });
}

const pure = {
  ACTIVE, FINAL, VIEWS, KIND_ICON, fmtBytes, fmtSpeed, fmtEta, fmtAgo, percentText, speedText, etaText,
  sizeText, statusText, barLook, rowActions, sourceKey, sourceLabel, viewMatches, viewCounts,
  filterRows, sortRows, groupRows, sectionSummary, moveSelection, extractInputs, hintFromDecisions, isStale,
  toneOf, overviewStats, needsAttention, runningNow, sublineText, countText,
  LIMIT_PRESETS, OPEN_CHOICES, NOTIFY_EVENTS, NOTIFY_OPTIONS, NOTIFY_OPTIONS_WAIT, DAYS, fmtLimit, parseLimit, limitFields,
  limitSummary, quickSpeedOptions, openLabel, openMenu,
  addOptionsFromForm, itemPrefsText, groupByAddTool, addCalls, mergeAddResults,
  ITEM_EVENTS, channelLabel,
  CARD_VERBS, cardActions, fmtWhen, applyNotifyOptions,
  primaryAction, secondaryActions, pendingWord, controlNote, controlNotice, TOGGLE_ICONS,
  cardNote, DISMISSABLE, DISMISS_TIP, CLEAR_TIP, SHOW_AGAIN_TIP, SHOW_HIDDEN_TIP, isHidden, canDismiss, isDimmed, dismissableRows, mergeHidden, dismissNotice,
  HISTORY_STATES, HISTORY_SORTS, HISTORY_PAGE, HISTORY_REDOWNLOAD_MAX, historyGroup, historyGroups, historyRange, historyQuery, historyFiltersActive, historySummaryText, fmtTook, historyDuration, historyDestName, historyReason, historyRowModel, selectionRange, historyRemoveText, historyRedownloadText, historyRetentionNote, historyEmpty, csvCell, historyCsv, historyJson, historyMeta, providerTone, providerFacts, providerBadge, ENGINE_WORDS, ENGINE_TONES, CONFIGURE_ANCHORS, engineWord, engineTone, engineLine, engineConfigure, engineSettingsNote, sourcesSummary,
  queueOrder, moveInOrder, stepInOrder, canReorder, reorderNote, applyOrder,
  groupIdOf, groupWaiting, groupOrderKeys, moveGroupInOrder, stepGroupInOrder, canReorderGroup, groupReorderNote,
  PICK_MAX, PICK_QUICK, torrentSource, selectableFile, torrentTree, pickerQuick, pickerFilter, pickerTotals, pickerTotalText, pickerBlock, torrentGroupMeta,
  UPDATES_NOTE, updateVersionText, updateSizeText, updateMetaParts, updateHealthText, updateGroups, updateSummaryText, updateBadge, updateCheckNotice,
  updatesPollDelay, updatesSignature,
  DRY_RUN_CHUNK, ADD_LIST_SHOWN, reasonWords, linkEngineWord, linkKindOf, torrentLinkCount, describeLinks, chunked, mergeDecisions, ignoredNote, looksAbsolute,
  addButtonLabel, groupStates, VIDEO_QUALITIES, VIDEO_CONTAINERS, VIDEO_CODECS, AUDIO_FORMATS, VIDEO_FLAGS, videoOptionsFromForm,
  followUpFor, startWords, describeAdd, remainingLinks,
};
if (host && host.__hubPureOnly) return pure;

// ============================================================================== the panel

const root = host.root;
const doc = root.ownerDocument || document;
const $ = (sel) => root.querySelector(sel);

const S = {
  rows: [], summary: null, selected: null, view: "all", search: "", groupBy: "group", groupTouched: false, collapsed: {},
  detail: null, busy: false, timer: 0, closed: false, lastError: "", hold: 0, holdTimer: 0,
  pendingSelect: "", sig: {},
  showHidden: false, hiddenRows: [], revealed: {}, clearing: false,   // brief 19: the "Show hidden" switch, the rows it lists, the ones listed again
  acting: {},                       // id -> { from, doing, until }: a Pause / Resume / Retry just pressed
  hubPrefs: null,
  saving: false,
  tab: "downloads", live: false, liveAt: 0, es: null, liveTimer: 0, dragId: "", dragGroup: "", picker: null,
  sources: null, srcTimer: 0, srcLoaded: false, srcBusy: false,
  upd: null, updLoaded: false, updBusy: false, updAsking: false, updTimer: 0, updFastUntil: 0,     // Updates tab (N23 part f)
  // History (brief 25): the rows loaded so far (a page at a time), the cursor of the next page, the hub's summary of the whole
  // filtered set, the filters, the selected ids and how much of the list is already drawn.
  hist: { rows: [], next: null, more: false, loaded: false, busy: false, seq: 0, state: "all", search: "", sort: "newest", from: "", to: "",
    error: "", summary: null, states: null, truncated: false, retention: 0, sel: {}, last: "", drawn: 0, lastGroup: "", exporting: false },
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
    // groupBy is saved only after the owner picked one: clicking a status chip used to save the default "Nothing" for good,
    // and a batch (a whole season) then never showed as a group.
    const prefs = { view: S.view, collapsed: Object.keys(S.collapsed).filter((k) => S.collapsed[k]).slice(0, 200) };
    if (S.groupTouched) { prefs.groupBy = S.groupBy; prefs.groupTouched = true; }
    host.storage.set("prefs", JSON.stringify(prefs)).catch(() => {});
  } catch (e) { /* storage is a convenience */ }
}

async function loadPrefs() {
  try {
    const raw = await host.storage.get("prefs");
    const p = raw ? JSON.parse(raw) : {};
    if (VIEWS.some((v) => v.id === p.view)) S.view = p.view;
    // a `source` saved by an older panel is ignored on purpose: the source chips are gone (N22 part d), so it would hide rows with no control to clear it
    // an older panel saved groupBy on every chip click, so only a value saved with groupTouched counts as the owner's choice
    if (p.groupTouched === true && ["none", "source", "group"].indexOf(p.groupBy) !== -1) { S.groupBy = p.groupBy; S.groupTouched = true; }
    if (Array.isArray(p.collapsed)) p.collapsed.slice(0, 200).forEach((k) => { if (typeof k === "string") S.collapsed[k] = true; });
  } catch (e) { /* defaults */ }
}

// Every row the lists show: the live rows, plus the hidden ones when "Show hidden" is on (or one was listed again).
function viewRows() {
  return mergeHidden(S.rows, S.hiddenRows, S.showHidden, S.revealed);
}

function selectedRow() {
  return viewRows().find((r) => r.id === S.selected) || null;
}

function visibleRows() {
  return sortRows(filterRows(viewRows(), { view: S.view, search: S.search }));
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
  const clearable = dismissableRows(S.rows).length;
  const clear = $("#clearFinished");
  if (!S.clearing) clear.disabled = !clearable;
  clear.textContent = clearable ? "Clear finished (" + clearable + ")" : "Clear finished";
  const sh = $("#showHidden");
  sh.setAttribute("aria-pressed", S.showHidden ? "true" : "false");
  sh.classList.toggle("is-active", S.showHidden);
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
  const counts = viewCounts(viewRows());
  if (!changed("chips", [counts, S.view])) return;
  const vbox = $("#viewChips");
  vbox.textContent = "";
  VIEWS.forEach((v) => {
    vbox.appendChild(chip(v.label, counts[v.id], S.view === v.id, v.id === "failed" || v.id === "stale", () => {
      S.view = v.id; savePrefs(); render();
    }));
  });
}

// A button the person just pressed says what is happening ("Pausing...") at once, and keeps saying it
// until the row's state moves on, the hub reports the request (`row.pending`), or ACTING_MS pass.
const ACTING_MS = 12000;
function pruneActing() {
  const now = Date.now();
  Object.keys(S.acting).forEach((id) => {
    const a = S.acting[id];
    const row = S.rows.find((r) => r.id === id);
    if (!row || now > a.until || row.state !== a.from) delete S.acting[id];
  });
}
function busyWord(row) {
  const a = S.acting[row.id];
  return (a && a.doing) || pendingWord(row.pending);
}

// The toggle (Pause / Resume / Start now / Retry): an icon and a word, always drawn, never on hover.
function toggleButton(row, prim, large) {
  const busy = busyWord(row);
  const attrs = { type: "button", "aria-label": prim.label + ": " + (row.title || row.id) };
  if (busy) { attrs.disabled = "disabled"; attrs["aria-busy"] = "true"; }
  return el("button", {
    cls: "tbtn tbtn--" + prim.verb + (large ? " tbtn--lg" : "") + (busy ? " is-busy" : ""),
    title: busy ? busy : prim.title, attrs: attrs,
    on: { click: (e) => { e.stopPropagation(); e.preventDefault(); runAction(row, prim); } },
  }, [
    el("span", { cls: "tbtn__i", text: prim.icon, attrs: { "aria-hidden": "true" } }),
    el("span", { cls: "tbtn__t", text: busy || prim.label }),
  ]);
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
  // a paused row carries a pause glyph as well as the word, so it is never taken for a stalled one
  if (tone === "paused") meta.push(el("span", { cls: "pglyph", text: TOGGLE_ICONS.pause, attrs: { "aria-hidden": "true" } }));
  meta.push(el("span", { cls: "st" + (row.error ? " bad" : ""), text: statusText(row) }));
  const size = sizeText(row);
  if (size) meta.push(el("span", { text: size }));
  const dimmed = isDimmed(row, S.revealed);
  if (dimmed) meta.push(el("span", { cls: "tag", text: "hidden", title: "You hid this row. Nothing was deleted." }));
  const prim = primaryAction(row);
  const why = controlNote(row);
  const buttons = secondaryActions(row).map((a) => el("button", {
    cls: "rbtn" + (a.danger ? " rbtn--danger" : ""), text: a.label, title: CARD_TITLES[a.verb] || a.label,
    attrs: { type: "button", "aria-label": a.label + ": " + (row.title || row.id) },
    on: { click: (e) => { e.stopPropagation(); e.preventDefault(); e.currentTarget.disabled = true; runAction(row, a); } },
  }));
  const hide = dismissButton(row, false);
  if (hide) buttons.push(hide);
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
  // N16: the toggle and Cancel sit on ONE row, toggle first, the same height (.tbtn / .rbtn share their box in tool.css)
  const acts = [prim ? toggleButton(row, prim, false) : null].concat(buttons).filter(Boolean);
  const noReorder = draggable ? "" : reorderNote(row, S.live);
  const rowEl = el("div", {
    cls: "row tone-" + tone + " state-" + row.state + (dimmed ? " is-hidden" : ""),
    title: noReorder,
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
    why && why.card ? el("div", { cls: "row__why", text: why.text }) : null,
    cardNote(row) ? el("div", { cls: "row__note", text: cardNote(row), title: cardNote(row) }) : null,
    acts.length ? el("div", { cls: "row__acts" }, acts) : null,
  ]);
  if (draggable) wireDrag(rowEl, row);
  return rowEl;
}

// Dismiss on a row that can be hidden; "Show again" on a hidden one (that one calls nothing).  null for any other row.
function dismissButton(row, large) {
  const dimmed = isDimmed(row, S.revealed);
  if (!dimmed && !canDismiss(row, S.revealed)) return null;
  const label = dimmed ? "Show again" : "Dismiss";
  return el("button", {
    cls: (large ? "btn btn--sm" : "rbtn") + " dismiss", text: label, title: dimmed ? SHOW_AGAIN_TIP : DISMISS_TIP,
    attrs: { type: "button", "aria-label": label + ": " + (row.title || row.id) },
    on: { click: (e) => { e.stopPropagation(); e.preventDefault(); if (dimmed) showAgain(row); else dismissRow(row, e.currentTarget); } },
  });
}

// Collapse / expand one group (a batch, or a source).  Remembered between visits.
function toggleSection(key) {
  if (S.collapsed[key]) delete S.collapsed[key]; else S.collapsed[key] = true;
  savePrefs();
  renderList();
}

// The rows to show, with the ones inside a collapsed group left out (arrow keys must not walk into them).
function groupMode(rows) {
  // "Batch" with no batch on the list would only add a "Single downloads" header over everything: show the plain list then
  return S.groupBy === "group" && !rows.some((r) => r.group_id) ? "none" : S.groupBy;
}
function shownRows(rows) {
  const out = [];
  groupRows(rows, groupMode(rows)).forEach((section) => { if (!(section.title && S.collapsed[section.key])) section.rows.forEach((r) => out.push(r)); });
  return out;
}

function sectionHeader(section) {
  const open = !S.collapsed[section.key];
  const label = section.title + (section.meta ? DOT + section.meta : "") + (open ? "" : DOT + sectionSummary(section.rows));
  const flip = () => toggleSection(section.key);
  const kids = [
    el("button", {
      cls: "section__toggle", text: open ? "\u25BE" : "\u25B8", title: open ? "Collapse: hide these downloads" : "Expand: show these downloads",
      attrs: { type: "button", "aria-expanded": open ? "true" : "false", "aria-label": (open ? "Collapse " : "Expand ") + section.title },
      on: { click: (e) => { e.stopPropagation(); flip(); } },
    }),
    el("span", { cls: "section__t", text: label, on: { click: flip } }),
  ];
  let head = null;
  // Batch headers get the group-wide controls: the same tools, aimed at the whole batch.
  if (S.groupBy === "group" && /^g:/.test(section.key)) {
    const gid = section.key.slice(2);
    const reorderable = canReorderGroup(section.rows, S.live);
    const acts = el("span", { cls: "section__acts" });
    const verbs = section.torrent ? [["pause", "Pause"], ["resume", "Resume"], ["cancel", "Cancel"]] : [["pause", "Pause"], ["resume", "Resume"], ["retry", "Retry"]];
    verbs.forEach((a) => {
      acts.appendChild(el("button", { cls: "btn", text: a[1], attrs: { type: "button" }, on: { click: () => groupAction(gid, a[0], a[1]) } }));
    });
    if (section.torrent) {
      acts.appendChild(el("button", {
        cls: "btn", text: "Add more files", title: "Choose more files of this torrent",
        attrs: { type: "button" }, on: { click: () => openTorrentGroup(section) },
      }));
    }
    if (reorderable) {
      const order = groupOrderKeys(queueOrder(S.rows), S.rows);
      const at = order.indexOf(section.key);
      [["\u2191", -1, "Move group up"], ["\u2193", 1, "Move group down"]].forEach((m) => {
        const attrs = { type: "button", "aria-label": m[2] + ": " + section.title };
        if (at === -1 || at + m[1] < 0 || at + m[1] >= order.length) attrs.disabled = "disabled";
        acts.appendChild(el("button", { cls: "btn", text: m[0], title: m[2], attrs: attrs,
          on: { click: (e) => { e.stopPropagation(); e.preventDefault(); moveGroup(section.key, m[1]); } } }));
      });
      kids.unshift(el("span", { cls: "section__grip", text: "\u22EE\u22EE", title: "Drag to change the order this group starts in", attrs: { "aria-hidden": "true" } }));
    }
    kids.push(acts);
    head = el("div", {
      cls: "section", title: reorderable ? "" : groupReorderNote(section.rows, S.live),
      attrs: { draggable: reorderable ? "true" : "false", "data-group": section.key },
    }, kids);
    wireGroupDrag(head, section.key, reorderable);
    return head;
  }
  return el("div", { cls: "section" }, kids);
}

function renderList() {
  const rows = visibleRows();
  const total = viewRows().length;
  $("#count").textContent = countText(rows.length, total);
  if (!changed("list", [rows, S.selected, S.groupBy, S.live, S.revealed, S.showHidden, S.collapsed, S.acting])) return;
  const list = $("#list");
  const scroll = list.scrollTop;
  list.textContent = "";
  if (!rows.length) {
    list.appendChild(el("div", {
      cls: "empty",
      text: total ? "Nothing matches this filter." : "No downloads yet. Paste a link in the form, or ask Jarvis to download a file.",
    }));
    return;
  }
  const sections = groupRows(rows, groupMode(rows));
  sections.forEach((section) => {
    if (section.title) list.appendChild(sectionHeader(section));
    if (section.title && S.collapsed[section.key]) return;
    section.rows.forEach((r) => list.appendChild(renderRow(r)));
  });
  const named = sections.filter((section) => section.title);
  const all = $("#collapseAll");
  if (all) {
    const anyOpen = named.some((section) => !S.collapsed[section.key]);
    all.hidden = named.length === 0;
    all.textContent = anyOpen ? "Collapse all" : "Expand all";
    all.title = anyOpen ? "Hide the downloads inside every group" : "Show the downloads inside every group";
    all.setAttribute("aria-label", all.title);
  }
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

  // The controls come first: Pause / Resume (or Retry) and Cancel, then Open / Show in folder.
  const actions = rowActions(row);
  const prim = primaryAction(row);
  const ctrl = actions.filter((a) => a.verb === "pause" || a.verb === "resume" || a.verb === "retry" || a.verb === "cancel");
  const files = actions.filter((a) => a.verb === "open" || a.verb === "reveal");
  const plain = (a) => el("button", {
    cls: "btn btn--sm" + (a.danger ? " btn--danger" : ""), text: a.label, attrs: { type: "button" },
    on: { click: () => runAction(row, a) },
  });
  // N16: the toggle first, then Retry / Cancel, all the same height (tool.css, .detail__actions--top)
  const ctrlFirst = ctrl.filter((a) => prim && a.verb === prim.verb).concat(ctrl.filter((a) => !(prim && a.verb === prim.verb)));
  const buttons = ctrlFirst.map((a) => (prim && a.verb === prim.verb ? toggleButton(row, prim, true) : plain(a)));
  files.forEach((a) => buttons.push(plain(a)));
  const hideBtn = dismissButton(row, true);
  if (hideBtn) buttons.push(hideBtn);
  if (files.some((a) => a.verb === "open")) {            // a finished file: its folder is one click too
    buttons.push(el("button", {
      cls: "btn btn--sm", text: "Open folder", attrs: { type: "button" },
      on: { click: async () => { const n = controlNotice(await callTool("dl_open", { id: row.id, action: "folder" })); if (n) toast(n.msg, n.level); } },
    }));
  }
  if (buttons.length) box.appendChild(el("div", { cls: "detail__actions detail__actions--top" }, buttons));
  const why = controlNote(row);
  if (why) box.appendChild(note("", why.title, why.text));

  const bar = renderBar(row, true);
  bar.classList.add("detail__bar");
  box.appendChild(bar);
  const right = [speedText(row), etaText(row)].filter(Boolean).join(DOT);
  box.appendChild(el("div", { cls: "detail__nums" }, [
    el("span", { text: [percentText(row), sizeText(row)].filter(Boolean).join(DOT) }),
    el("span", { text: right }),
  ]));

  if (row.error) box.appendChild(note("bad", "It failed", row.error));
  if (isStale(row)) box.appendChild(note("warn", "Not reporting", "Nothing has been heard from this download " + fmtAgo(row.age_s) + ". It may have stopped, or the tool that runs it may be closed. It is not marked failed."));
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
  const picking = !!S.picker && !row;
  $("#addForm").hidden = !!row || picking;
  $("#detail").hidden = !row;
  $("#picker").hidden = !picking;
  $("#midTitle").textContent = row ? "Download" : (picking ? "Choose files" : "Add a download");
  renderAddExtras();
  renderDetailPrefs();
  if (!row) { S.sig.detail = ""; return; }
  const full = S.detail && S.detail.id === row.id ? S.detail.item : null;
  if (!changed("detail", [row, full && full.events, full && full.children, full && full.detail, S.acting[row.id] || null])) return;
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
  pruneActing();
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
  // Pause / Resume / Retry say "Pausing..." on the button at once (N1); Cancel asks first, so it doesn't.
  const doing = action.doing || ((action.verb === "pause" || action.verb === "resume" || action.verb === "retry") ? DOING[action.verb] : "");
  if (doing) {
    S.acting[row.id] = { from: row.state, doing: doing, until: Date.now() + ACTING_MS };
    render();
    setTimeout(() => { pruneActing(); render(); }, ACTING_MS + 100);
  }
  const r = await callTool(action.tool, args);
  if (r.declined || r.ok === false) delete S.acting[row.id];
  // errors, a declined confirmation, a request the hub queued, and (N2) a note such as "its folder was
  // opened instead" all reach the person; only a plain success is silent
  const n = controlNotice(r, Object.assign({}, action, { doing: doing }));
  if (n) toast(n.msg, n.level);
  if (doing) render();
  schedule(300);
}

async function groupAction(gid, verb, label) {
  // Cancel is asked about by the tool itself (dl_cancel confirms); on a torrent group it only touches unfinished files.
  const r = verb === "pause" ? await callTool("dl_pause", { group: gid })
    : verb === "resume" ? await callTool("dl_resume", { group: gid })
      : verb === "cancel" ? await callTool("dl_cancel", { group: gid })
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

// ---- brief 19: Dismiss, Clear finished, Show hidden.  The writes go to the hub's own routes (hubFetch sends the header the
// server insists on); the hub's refusals (a hub download, a row still running) reach the person as the hub worded them.

// The rows the owner hid, only while the view needs them (Show hidden is on, or one was listed again).
async function loadHidden(loud) {
  if (!S.showHidden && !Object.keys(S.revealed).length) return;
  try {
    const r = await hubFetch("/api/hub/list?include_dismissed=1");
    S.hiddenRows = (Array.isArray(r.items) ? r.items : []).filter(isHidden);
  } catch (err) {
    if (loud) toast("Couldn't read the hidden rows: " + message(err), "warn");
  }
  if (!S.closed) render();
}

async function dismissRow(row, btn) {
  if (btn) btn.disabled = true;
  try {
    const n = dismissNotice(await hubFetch("/api/hub/dismiss", { id: row.id }));
    toast(n.msg, n.level);
    delete S.revealed[row.id];
  } catch (err) {
    toast(message(err), "warn");
    if (btn) btn.disabled = false;
    return;
  }
  await loadHidden(false);
  schedule(300);
}

function showAgain(row) {
  S.revealed[row.id] = true;
  render();
}

async function clearFinished() {
  if (S.clearing) return;
  S.clearing = true;
  $("#clearFinished").disabled = true;
  try {
    const n = dismissNotice(await hubFetch("/api/hub/dismiss", { finished: true }));
    toast(n.msg, n.level);
  } catch (err) {
    toast(message(err), "warn");
  }
  S.clearing = false;
  await loadHidden(false);
  render();
  schedule(300);
}

function toggleShowHidden() {
  S.showHidden = !S.showHidden;
  if (S.showHidden) {
    loadHidden(true);
  } else {
    S.revealed = {};
    if (S.selected && !viewRows().some((x) => x.id === S.selected)) { S.selected = null; S.detail = null; }
  }
  render();
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
  S.picker = null;
  S.selected = id;
  S.detail = null;
  render();
  loadDetail();
}

function newDownload() {
  S.picker = null;
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
    // `streaming` was read BEFORE the await: a slow dl_list that started while the stream was still connecting
    // lands after the stream's snapshot.  Its rows carry no `priority`, so letting it overwrite them took the
    // arrows and the grip away until some row changed (N15).  Look again now.
    if (!streaming && !S.live) S.rows = Array.isArray(r.items) ? r.items : [];
    if (r.summary) {
      S.summary = r.summary;
      if (applyNotifyOptions(r.summary.notify_options)) { S.sig.addextras = ""; S.sig.dprefs = ""; }
    }
    S.lastError = "";
    if (S.pendingSelect && S.rows.some((x) => x.id === S.pendingSelect)) { S.selected = S.pendingSelect; S.detail = null; }
    S.pendingSelect = "";
    if (S.selected && !viewRows().some((x) => x.id === S.selected)) { S.selected = null; S.detail = null; }
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
  loadHidden(false);                    // only does anything while hidden rows are in view
  schedule();
}

// ---- the add form (9C): links in, what each one is and who takes it, then Add.  Everything else sits in closed groups.

let addBusy = false;                      // an add is running: a second press (button, Enter, Ctrl+Enter) does nothing
let addView = null;                       // { urls, decisions }: the dry run's last answer for what is in the box
let addCtx = null;                        // what the last add sent: { args, perTool, paused }, so a follow-up question can be answered
const ADD_GROUPS = ["Where", "When", "Speed", "Tell", "Batch"];
const ADD_STATE_USUAL = { Where: "usual folder", When: "right away", Speed: "no limit of its own", Tell: "my notification settings", Batch: "none" };

// The line under the box: `text` for the eye, `spoken` (default: the same words) for a screen reader.
function setHint(text, bad, spoken) {
  const h = $("#addHint");
  h.textContent = text;
  h.className = "af-sum" + (bad ? " bad" : "");
  $("#addHintSr").textContent = spoken === undefined ? text : spoken;
}

function addFormFields() {
  return { dest: $("#addDest").value, open: $("#addOpen").value, start: $("#addStart").value, paused: $("#addPaused").checked,
    limit: $("#addLimit").value, unit: $("#addLimitUnit").value, notify: $("#addNotify").value,
    channels: ticked($("#addChannels")), events: ticked($("#addEvents")), group: $("#addGroup").value };
}

function videoFields() {
  return { mode: $("#addVMode").value, quality: $("#addVQuality").value, container: $("#addVContainer").value, video_codec: $("#addVCodec").value,
    audio_format: $("#addVAudioFormat").value, audio_quality: $("#addVAudioQuality").value,
    embed_thumbnail: $("#addVThumb").checked, embed_metadata: $("#addVMeta").checked, embed_chapters: $("#addVChapters").checked,
    write_subs: $("#addVSubs").checked, embed_subs: $("#addVSubsEmbed").checked, skip_if_downloaded: $("#addVSkip").checked,
    sub_langs: $("#addVSubLangs").value, sponsorblock_remove: $("#addVSponsor").value };
}

function openAddGroup(name) {
  const box = $("#add" + name);
  if (box) box.open = true;
}

// After a failed or partial add, open every group that holds a choice, so what was sent can be seen.
function openGroupsWithValues() {
  const st = groupStates(addFormFields());
  ADD_GROUPS.forEach((g) => { if (st[g.toLowerCase()]) openAddGroup(g); });
}

// Everything on the form that follows from what is in the box and in the groups: the button's words, what each closed group
// says, the torrent row, the folder warning, the video / audio block.  Cheap; called on every change.
function refreshAddUi() {
  const text = $("#addInput").value;
  const urls = extractInputs(text);
  const single = !!torrentSource(text);
  const torrents = torrentLinkCount(urls);
  $("#addTorrent").hidden = !(single || torrents > 0);
  $("#addChoose").hidden = !single;
  $("#addTorrentHelp").textContent = single
    ? "This is a torrent. If it holds many files you can choose which to download. Or just press Add: a torrent with several files asks you first."
    : torrents ? "To choose which files of a torrent to download, paste just that one torrent's link. A torrent with several files asks you first when you add it." : "";
  const fields = addFormFields();
  const states = groupStates(fields);
  ADD_GROUPS.forEach((g) => {
    const t = states[g.toLowerCase()];
    const node = $("#addState" + g);
    node.textContent = t || ADD_STATE_USUAL[g];
    node.classList.toggle("is-set", !!t);
    node.title = t || "";
  });
  const abs = looksAbsolute(fields.dest);
  const note = $("#addDestNote");
  note.hidden = !abs;
  note.textContent = abs ? "That is a full path, so you'll be asked to confirm before anything is saved there." : "";
  const audio = $("#addVMode").value === "audio";
  Array.prototype.forEach.call(root.querySelectorAll("#addVideo .af-vonly"), (n) => { n.hidden = audio; });
  Array.prototype.forEach.call(root.querySelectorAll("#addVideo .af-aonly"), (n) => { n.hidden = !audio; });
  $("#addVideo").hidden = !(urls.length && addView && describeLinks(addView.urls, addView.decisions).video);
  if (!addBusy) $("#addBtn").textContent = addButtonLabel({ count: urls.length, paused: fields.paused, start: fields.start });
}

// The line under the box and the row for each link, from the dry run's answer.
function showLinks(sum, note) {
  const list = $("#addLinks");
  list.textContent = "";
  if (!sum || !sum.items.length) { list.hidden = true; return; }
  const text = sum.text + (note ? DOT + note : "");
  setHint(text, sum.bad > 0, sum.spoken + (note ? ". " + note : ""));
  list.hidden = !(sum.items.length > 1 || sum.bad > 0);
  sum.items.slice(0, ADD_LIST_SHOWN).forEach((x) => {
    const what = x.ok ? x.kind.one + (x.engineWord ? DOT + x.engineWord : "") + (x.reason ? " \u2014 " + x.reason : "") : x.checked ? x.problem : "Checked when you press Add.";
    list.appendChild(el("li", { cls: "af-link " + (x.ok ? "is-ok" : x.checked ? "is-bad" : "is-wait") }, [
      el("span", { cls: "af-link__tag", text: x.ok ? "Ready" : x.checked ? "Can't add" : "Not checked" }),
      el("span", { cls: "af-link__url", text: x.url, title: x.url }),
      el("span", { cls: "af-link__what", text: what }),
    ]));
  });
  if (sum.items.length > ADD_LIST_SHOWN) list.appendChild(el("li", { cls: "af-link af-link--more", text: "and " + (sum.items.length - ADD_LIST_SHOWN) + " more" }));
}

// The dry run, ten links at a time (the hub answers for ten per call), so every link is checked and the options reach them all.
async function dryRunAll(urls) {
  const parts = chunked(urls, DRY_RUN_CHUNK);
  const answers = await Promise.all(parts.map((part) => callTool("dl_add", { urls: part, dry_run: true })));
  return mergeDecisions(parts, answers);
}

async function previewHint(urls) {
  const seq = ++hintSeq;
  clearAddResult();
  refreshAddUi();                          // also after a drop of links, which fills the box without an input event
  setHint("Checking " + urls.length + (urls.length === 1 ? " link" : " links") + "\u2026", false);
  const checked = await dryRunAll(urls);
  if (seq !== hintSeq) return;
  if (checked.error && !checked.decisions.some(Boolean)) { addView = null; showLinks(null); setHint(checked.error, true); refreshAddUi(); return; }
  addView = { urls: urls, decisions: checked.decisions };
  showLinks(describeLinks(urls, checked.decisions), ignoredNote($("#addInput").value));
  refreshAddUi();
}

function clearAddResult() {
  const box = $("#addResult");
  box.hidden = true;
  box.textContent = "";
}

// What happened, in the form itself: the headline, every link that was refused with its reason, a note, and any question the hub asked.
function renderAddResult(model) {
  const box = $("#addResult");
  box.textContent = "";
  box.className = "af-result is-" + model.tone;
  box.appendChild(el("div", { cls: "af-result__head", text: model.headline }));
  if (model.lines.length) {
    const ul = el("ul", { cls: "af-result__list", attrs: { "aria-label": "Links that were not added" } });
    model.lines.slice(0, 12).forEach((l) => ul.appendChild(el("li", {}, [
      el("span", { cls: "af-result__url", text: l.url, title: l.url }),
      el("span", { cls: "af-result__why", text: l.text }),
    ])));
    if (model.lines.length > 12) ul.appendChild(el("li", { text: "and " + (model.lines.length - 12) + " more" }));
    box.appendChild(ul);
  }
  model.asks.forEach((ask) => {
    const row = el("div", { cls: "af-result__ask" }, [el("div", { text: ask.message })]);
    if (ask.label) row.appendChild(el("button", { cls: "btn btn--sm", text: ask.label, attrs: { type: "button" }, on: { click: () => runFollowUp(ask) } }));
    box.appendChild(row);
  });
  if (model.note) box.appendChild(el("div", { cls: "af-result__note", text: model.note }));
  box.hidden = false;
  if (typeof box.scrollIntoView === "function") box.scrollIntoView({ block: "nearest" });     // the form is long: the answer must be seen
}

function setAddBusy(on) {
  addBusy = !!on;
  $("#addLock").disabled = addBusy;
  $("#addForm").setAttribute("aria-busy", addBusy ? "true" : "false");
  if (addBusy) { $("#addBtn").textContent = "Adding\u2026"; return; }
  refreshAddUi();
  if (!root.activeElement) $("#addInput").focus();         // a disabled form drops the focus: put it back in the box
}

// The form as the add calls need it: the links, the arguments every add tool takes, and the video choices (for dl_add_ytdlp only).
function readAddForm() {
  const urls = extractInputs($("#addInput").value);
  const args = {};
  const dest = $("#addDest").value.trim();
  const group = $("#addGroup").value.trim();
  if (dest) args.dest = dest;
  if (group) args.group = group;
  if ($("#addPaused").checked) args.start_paused = true;
  const extra = addOptionsFromForm({ limit: $("#addLimit").value, unit: $("#addLimitUnit").value, open: $("#addOpen").value, notify: $("#addNotify").value,
    channels: ticked($("#addChannels")), events: ticked($("#addEvents")), start: $("#addStart").value });
  if (extra.error) return { urls: urls, error: extra.error, group: "Speed", focus: "#addLimit" };
  Object.assign(args, extra.args);
  return { urls: urls, args: args, perTool: { dl_add_ytdlp: videoOptionsFromForm(videoFields()).args } };
}

function resetAddOneOffs() {
  $("#addLimit").value = "";                  // one-off choices don't stick to the next add
  $("#addStart").value = "";
  $("#addOpen").value = "default";
  $("#addNotify").value = "default";
  untick($("#addChannels"));
  untick($("#addEvents"));
  $("#addVMode").value = "video";
  ["#addVQuality", "#addVContainer", "#addVCodec", "#addVAudioFormat"].forEach((id) => { $(id).selectedIndex = 0; });
  ["#addVAudioQuality", "#addVSubLangs", "#addVSponsor"].forEach((id) => { $(id).value = ""; });
  ["#addVThumb", "#addVMeta", "#addVChapters", "#addVSubs", "#addVSubsEmbed", "#addVSkip"].forEach((id) => { $(id).checked = false; });
}

// Show what the calls came to, keep the links that were not added in the box, and say it once in the corner too.
function finishAdd(entries, ctx, allUrls) {
  addCtx = ctx;
  const model = describeAdd(entries, { paused: ctx.paused });
  if (model.queued) toast(model.headline, model.tone === "ok" ? "success" : "warn");
  if (model.select) S.pendingSelect = model.select;               // show the one you just added
  const left = remainingLinks(allUrls, entries, model.keep);
  $("#addInput").value = left.join("\n");
  if (!left.length) resetAddOneOffs();
  else if (model.tone !== "ok") openGroupsWithValues();
  addView = null;
  if (left.length) previewHint(left);
  else { hintSeq++; showLinks(null); setHint("", false); }
  renderAddResult(model);
  refreshAddUi();
  if (model.queued) schedule(200);
}

async function submitAdd(ev) {
  ev.preventDefault();
  if (addBusy) return;                                            // one add at a time
  const input = $("#addInput");
  const form = readAddForm();
  if (!form.urls.length) {
    clearAddResult();
    setHint("Paste a link first: it starts with http://, https:// or magnet:", true);
    input.focus();
    return;
  }
  if (form.error) {
    clearAddResult();
    setHint(form.error, true);
    openAddGroup(form.group);
    $(form.focus).focus();
    return;
  }
  clearAddResult();
  clearTimeout(hintTimer);
  hintSeq++;                                                      // a preview still on its way must not overwrite what comes next
  setAddBusy(true);
  try {
    // Ask the router which engine takes each link, then call that engine's add tool.
    setHint("Adding " + form.urls.length + (form.urls.length === 1 ? " link" : " links") + "\u2026", false);
    const checked = await dryRunAll(form.urls);
    const calls = addCalls(groupByAddTool(form.urls, checked.decisions), form.args, form.perTool);
    const entries = [];
    for (let i = 0; i < calls.length; i++) {
      const c = calls[i];
      const one = await callTool(c.tool, c.args);
      entries.push({ tool: c.tool, urls: c.args.magnet ? [c.args.magnet] : c.args.urls, result: one });
      let why = "";
      if (one && one.declined) why = "Not tried: you declined the question before it.";
      if (one && one.needs_clarification && one.torrent && c.args.magnet) {
        // N24: a torrent with several files asked which to take: nothing was queued; the list opens instead
        openPicker(c.args.magnet, "");
        why = "Not tried yet: the torrent above needs your choice first.";
      }
      if (why) {
        calls.slice(i + 1).forEach((rest) => entries.push({ tool: rest.tool, urls: rest.args.magnet ? [rest.args.magnet] : rest.args.urls, result: null, why: why }));
        break;
      }
    }
    finishAdd(entries, { args: form.args, perTool: form.perTool, paused: !!form.args.start_paused }, form.urls);
  } finally {
    setAddBusy(false);
  }
}

// A button on a question the hub asked back (a playlist, a page only yt-dlp's generic extractor might read, a torrent's files).
async function runFollowUp(ask) {
  if (addBusy || !ask) return;
  if (!ask.tool) { if (ask.source) openPicker(ask.source, ""); return; }
  if (!addCtx) return;
  setAddBusy(true);
  try {
    const args = Object.assign({}, addCtx.args, (addCtx.perTool && addCtx.perTool[ask.tool]) || {}, ask.extra, { urls: ask.urls });
    const entry = { tool: ask.tool, urls: ask.urls, result: await callTool(ask.tool, args) };
    finishAdd([entry], addCtx, extractInputs($("#addInput").value));
  } finally {
    setAddBusy(false);
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
    refreshAddUi();
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

// `anchor` (a string, brief 24) names a block of the Downloads page to scroll to: "stremio", "connections" ...  A click
// handler that passes the event instead gets the page as before.
function openSettings(anchor) {
  const s = window.JarvisSettings;
  const want = typeof anchor === "string" && /^[a-z0-9-]{1,40}$/.test(anchor) ? anchor : "";
  if (s && typeof s.show === "function") s.show(want ? { section: "downloads", anchor: want } : { section: "downloads" });
  else toast("Settings can't be opened from here", "warn");
}

// ---- wiring

$("#addForm").addEventListener("submit", submitAdd);
$("#addInput").addEventListener("input", () => {
  clearTimeout(hintTimer);
  clearAddResult();
  const urls = extractInputs($("#addInput").value);
  if (!urls.length) { hintSeq++; addView = null; showLinks(null); setHint("", false); refreshAddUi(); return; }
  refreshAddUi();
  hintTimer = setTimeout(() => previewHint(urls), 400);
});
$("#addInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#addForm").requestSubmit(); }
});
// Every choice in the form updates the button's words and what each closed group says about itself.
$("#addForm").addEventListener("input", refreshAddUi);
$("#addForm").addEventListener("change", refreshAddUi);
$("#newBtn").addEventListener("click", clearAddResult);          // a new add starts clean: last time's result is gone
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
  refreshAddUi();
  box.focus();
});
$("#search").addEventListener("input", (e) => { S.search = e.target.value; render(); });
$("#groupBy").addEventListener("change", (e) => { S.groupBy = e.target.value; S.groupTouched = true; savePrefs(); render(); });
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
$("#clearFinished").addEventListener("click", clearFinished);
$("#showHidden").addEventListener("click", toggleShowHidden);
$("#collapseAll").addEventListener("click", () => {
  const rows = visibleRows();
  const named = groupRows(rows, groupMode(rows)).filter((section) => section.title);
  const anyOpen = named.some((section) => !S.collapsed[section.key]);
  named.forEach((section) => { if (anyOpen) S.collapsed[section.key] = true; else delete S.collapsed[section.key]; });
  savePrefs();
  renderList();
});

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
  const ids = shownRows(rows).map((r) => r.id);
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
  if (S.dragId || S.dragGroup) return;          // a queued row or a whole group being reordered, not links being dropped
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
  if (["downloads", "sources", "updates", "history"].indexOf(name) === -1) return;
  S.tab = name;
  $("#hub").dataset.tab = name;
  ["downloads", "sources", "updates", "history"].forEach((t) => {
    const btn = $("#tab" + t.charAt(0).toUpperCase() + t.slice(1));
    const on = t === name;
    btn.setAttribute("aria-selected", on ? "true" : "false");
    btn.tabIndex = on ? 0 : -1;
    if (on && focus) btn.focus();
  });
  $("#body").hidden = name !== "downloads";
  $("#sourcesPane").hidden = name !== "sources";
  $("#updatesPane").hidden = name !== "updates";
  $("#historyPane").hidden = name !== "history";
  clearInterval(S.srcTimer);
  if (name === "sources") {
    loadSources(false);
    S.srcTimer = setInterval(() => { if (!doc.hidden) loadSources(false); }, 10000);
  } else if (name === "updates") {
    loadUpdates(false);
  } else if (name === "history") {
    loadHistory();
  }
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target && e.target.closest ? e.target.closest("[data-tab]") : null;
  if (b) setTab(b.getAttribute("data-tab"), false);
});
$("#tabs").addEventListener("keydown", (e) => {
  const order = ["downloads", "sources", "updates", "history"];
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
  if (S.selected && !viewRows().some((x) => x.id === S.selected)) { S.selected = null; S.detail = null; }
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

// ---- Sources (brief 24): the Providers tab, now with every download engine above the programs that report their own downloads

function setChecking(on) {
  $("#sourcesPane").querySelectorAll(".src-check").forEach((b) => { b.disabled = !!on; b.textContent = on ? "Checking\u2026" : "Check now"; });
}

// One read of the hub's `sources` answer.  It only looks: it never starts, stops or changes anything.  `manual` is the
// person pressing Check now (so the buttons say so); the timer reloads quietly.
async function loadSources(manual) {
  if (S.srcBusy) return;
  S.srcBusy = true;
  if (manual) setChecking(true);
  try {
    const r = await hubFetch("/api/hub/sources");
    if (S.closed) return;
    S.sources = { engines: Array.isArray(r.engines) ? r.engines : [], providers: Array.isArray(r.providers) ? r.providers : [] };
    S.srcLoaded = true;
    renderSources();
  } catch (err) {
    if (S.closed) return;
    if (!S.srcLoaded) $("#srcSummary").textContent = message(err) || "The sources couldn't be read.";
    else if (manual) toast(message(err) || "The sources couldn't be read.", "warn");
  } finally {
    S.srcBusy = false;
    if (!S.closed) setChecking(false);
  }
}

function sourceHeading(text, note) {
  return el("div", { cls: "src__h" }, [el("span", { cls: "src__title", text: text }), note ? el("span", { cls: "src__note", text: note }) : null]);
}

function renderEngine(e) {
  const tone = engineTone(e.state);
  const cfg = engineConfigure(e);
  const checks = (Array.isArray(e.checks) ? e.checks : []).filter((c) => c && c.detail && c.detail !== e.reason);
  const settingsNote = engineSettingsNote(e);
  const cardTone = e.state === "ready" ? "ok" : e.state === "broken" ? "bad" : e.state === "off" ? "off" : "warn";
  return el("div", { cls: "prov src-engine is-" + cardTone, attrs: { "data-id": e.id, "data-kind": "engine", "data-state": e.state } }, [
    el("div", { cls: "prov__head" }, [
      el("span", { cls: "dot tone-" + tone, attrs: { "aria-hidden": "true" } }),
      el("span", { cls: "prov__name", text: e.label || e.id }),
      el("span", { cls: "prov__badge src__state", text: engineWord(e.state) }),
      el("span", { cls: "prov__actions" }, [
        cfg ? el("button", {
          cls: "btn btn--sm src-configure", text: "Configure", attrs: { type: "button", "aria-label": "Configure " + (e.label || e.id) },
          on: { click: () => openSettings(cfg.anchor) },
        }) : null,
        el("button", {
          cls: "btn btn--sm src-check", text: "Check now",
          attrs: { type: "button", "aria-label": "Check " + (e.label || e.id) + " again", title: "Ask again. This only looks; it never starts or changes anything." },
          on: { click: () => loadSources(true) },
        }),
      ]),
    ]),
    e.reason ? el("div", { cls: "prov__reason" + (e.state === "broken" ? " is-bad" : ""), text: e.reason }) : null,
    el("div", { cls: "prov__line", text: engineLine(e) }),
    settingsNote ? el("div", { cls: "prov__line prov__line--dim", text: settingsNote }) : null,
    checks.length ? el("div", { cls: "prov__checks" }, checks.map((c) => el("div", { cls: "prov__check is-" + (c.status || "info"), text: (c.name ? c.name + ": " : "") + c.detail }))) : null,
  ]);
}

function renderProviderCard(p) {
  // not registered yet: a program the hub knows about that only appears after its first use; say so instead of nothing
  if (p.registered === false) {
    return el("div", { cls: "prov is-off src-provider", attrs: { "data-id": p.id, "data-kind": "provider", "data-registered": "no" } }, [
      el("div", { cls: "prov__head" }, [
        el("span", { cls: "dot tone-stale", attrs: { "aria-hidden": "true" } }),
        el("span", { cls: "prov__name", text: p.label || p.id }),
        el("span", { cls: "prov__badge", text: "Not seen yet" }),
      ]),
      el("div", { cls: "prov__reason", text: p.hint || "It appears here once it reports its first download." }),
    ]);
  }
  const tone = providerTone(p);
  const toggle = el("button", {
    cls: "btn btn--sm", text: p.enabled ? "Turn off" : "Turn on",
    attrs: { type: "button", "aria-label": (p.enabled ? "Turn off " : "Turn on ") + (p.label || p.id) },
    on: { click: async (e) => {
      e.currentTarget.disabled = true;
      const r = await callTool("dl_provider_prefs", { id: p.id, enabled: !p.enabled });
      if (r.declined) toast("Cancelled", "info");
      else if (r.ok === false) toast(r.error || "That didn't work", "warn");
      loadSources(false);
    } },
  });
  const grid = el("div", { cls: "prov__grid" }, providerFacts(p).map((f) => el("div", { cls: "prov__kv" }, [
    el("span", { text: f[0] + ": " }), el("b", { text: f[1] }),
  ])));
  return el("div", { cls: "prov src-provider is-" + tone, attrs: { "data-id": p.id, "data-kind": "provider" } }, [
    el("div", { cls: "prov__head" }, [
      el("span", { cls: "prov__name", text: p.label || p.id }),
      el("span", { cls: "prov__badge", text: providerBadge(p) }),
      tone === "quiet" ? el("span", { cls: "prov__badge", text: "Not reporting" }) : null,
      el("span", { cls: "prov__actions" }, [
        toggle,
        el("button", { cls: "btn btn--sm", text: "Settings\u2026", attrs: { type: "button" }, on: { click: () => openSettings(PROVIDER_SETTINGS_ANCHOR) } }),
      ]),
    ]),
    grid,
    p.last_error ? el("div", { cls: "prov__err", text: "Last error: " + String(p.last_error).slice(0, 240) }) : null,
  ]);
}

function renderSources() {
  const data = S.sources;
  const engines = (data && data.engines) || [];
  const progs = (data && data.providers) || [];
  $("#srcCount").textContent = data ? String(engines.length + progs.filter((p) => p.registered !== false).length) : "";
  $("#srcSummary").textContent = sourcesSummary(data);
  if (!changed("sources", data)) return;
  const box = $("#srcList");
  box.textContent = "";
  box.appendChild(sourceHeading("Download engines", "What the hub downloads with itself."));
  if (!engines.length) box.appendChild(el("div", { cls: "hist__empty", text: "The hub reported no download engines." }));
  engines.forEach((e) => box.appendChild(renderEngine(e)));
  box.appendChild(sourceHeading("Programs that report their own downloads", "Game installs and other tools; the hub shows them, it does not run them."));
  if (!progs.length) box.appendChild(el("div", { cls: "hist__empty", text: "Game installs and other tools that report into the hub appear here." }));
  progs.forEach((p) => box.appendChild(renderProviderCard(p)));
  setChecking(S.srcBusy);
}
$("#srcRefresh").addEventListener("click", () => loadSources(true));

// ---- Updates (N23 part f): what the hub found out of date.  It only tells; there is no Update button (N23d), no source filter
// (N22 part d) and no notification (N23c: the number on the tab is the signal).  Every word that came from another program is
// drawn with textContent (el() never builds markup).  Reads are GET /api/hub/updates; Check now and Ignore this version are
// writes (POST, with the X-Jarvis-Hub header, refused by the hub for anything but this page).

function setUpdChecking(on) {
  $("#updatesPane").querySelectorAll(".upd-check").forEach((b) => { b.disabled = !!on; b.textContent = on ? "Asking\u2026" : "Check now"; });
}

async function loadUpdates(manual) {
  if (S.updBusy) return;
  S.updBusy = true;
  try {
    const r = await hubFetch("/api/hub/updates");
    if (S.closed) return;
    S.upd = r;
    S.updLoaded = true;
    renderUpdates();
  } catch (err) {
    if (S.closed) return;
    if (!S.updLoaded) $("#updSummary").textContent = message(err) || "The updates couldn't be read.";
    else if (manual) toast(message(err) || "The updates couldn't be read.", "warn");
  } finally {
    S.updBusy = false;
    scheduleUpdates();
  }
}

function scheduleUpdates() {
  clearTimeout(S.updTimer);
  if (S.closed) return;
  const checking = !!(S.upd && Array.isArray(S.upd.providers) && S.upd.providers.some((p) => p && p.status === "checking"));
  const delay = updatesPollDelay({ visible: S.tab === "updates", checking: checking, recent: Date.now() < S.updFastUntil });
  S.updTimer = setTimeout(() => { if (doc.hidden) scheduleUpdates(); else loadUpdates(false); }, delay);
}

function renderUpdateRow(f) {
  const meta = updateMetaParts(f);
  return el("div", { cls: "upd__row", attrs: { role: "listitem", "data-provider": f.provider, "data-id": f.id } }, [
    el("div", { cls: "upd__main" }, [
      el("div", { cls: "upd__title", text: f.title || f.id, title: f.title || f.id }),
      el("div", { cls: "upd__ver", text: updateVersionText(f) }),
      meta.length ? el("div", { cls: "upd__meta" }, meta.map((t) => el("span", { text: t }))) : null,
    ]),
    el("button", {
      cls: "btn btn--sm", text: "Ignore this version",
      attrs: { type: "button", "aria-label": "Ignore this version of " + (f.title || f.id), title: "Hide this one until the program reports a different version." },
      on: { click: (e) => ignoreUpdate(f, e.currentTarget) },
    }),
  ]);
}

function renderUpdateGroup(g) {
  const health = updateHealthText(g.health);
  const can = !!(g.health && g.health.can_check);
  return el("div", { cls: "upd__group", attrs: { role: "group", "data-provider": g.id, "aria-label": g.label } }, [
    el("div", { cls: "upd__head" }, [
      el("span", { cls: "upd__name", text: g.label }),
      el("span", { cls: "upd__n", text: g.rows.length + (g.rows.length === 1 ? " update" : " updates") }),
      el("span", { cls: "upd__spacer" }),
      can ? el("button", {
        cls: "btn btn--sm upd-check", text: "Check now",
        attrs: { type: "button", "aria-label": "Check " + g.label + " again", title: "Ask again. This only looks; it never installs or changes anything." },
        on: { click: () => checkUpdates(g.id) },
      }) : null,
      health.text ? el("div", { cls: "upd__health" + (health.tone ? " is-" + health.tone : ""), text: health.text }) : null,
    ]),
  ].concat(g.rows.map(renderUpdateRow)));
}

function renderUpdates() {
  const d = S.upd;
  $("#updCount").textContent = updateBadge(d);
  $("#updSummary").textContent = updateSummaryText(d);
  if (!changed("updates", updatesSignature(d))) return;
  const box = $("#updList");
  box.textContent = "";
  const groups = updateGroups(d);
  if (!groups.length) box.appendChild(el("div", { cls: "hist__empty", text: "Programs that can report an available update appear here once they have been checked." }));
  groups.forEach((g) => box.appendChild(renderUpdateGroup(g)));
  box.appendChild(el("div", { cls: "upd__note", text: UPDATES_NOTE }));
  setUpdChecking(S.updAsking);
}

// Check now: every program (provider "") or one.  The hub only sets a flag; the daemon's runner starts the check within a few
// seconds, so the list is read again, quickly for a while, and the answer shows up by itself.
async function checkUpdates(provider) {
  if (S.updAsking) return;
  S.updAsking = true;
  setUpdChecking(true);
  try {
    const r = await hubFetch("/api/hub/updates/check", { provider: provider || "" });
    const n = updateCheckNotice(r);
    if (n.started) S.updFastUntil = Date.now() + 90000;
    toast(n.text, n.level);
  } catch (err) {
    toast(message(err), "warn");
  } finally {
    S.updAsking = false;
    setUpdChecking(false);
  }
  loadUpdates(false);
}

// Ignore this version: hides one finding until the source reports a different version.  Nothing is installed, changed or deleted,
// but the tab has no way to bring an ignored one back, so it asks first.
async function ignoreUpdate(f, btn) {
  if (!(await askFirst("Ignore this version?", "It stays hidden until the program reports a different version. Nothing is installed, changed or deleted.", "Ignore"))) return;
  btn.disabled = true;
  try {
    await hubFetch("/api/hub/updates/dismiss", { provider: f.provider, id: f.id });
  } catch (err) {
    toast(message(err), "warn");
    btn.disabled = false;
    return;
  }
  loadUpdates(false);
}
$("#updCheck").addEventListener("click", () => checkUpdates(""));

// ---- History (brief 25, N21)

let histTimer = 0;

function histFilters() {
  const h = S.hist;
  return { state: h.state, search: h.search, sort: h.sort, from: h.from, to: h.to };
}

// Load the first page again (`keep`: as many rows as were showing, so a Remove or a Retry doesn't throw the person back to the top)
// or, with `more`, the next page.  A newer request makes an older answer that arrives late go unused.
async function loadHistory(more, keep) {
  const h = S.hist;
  if (more && (h.busy || !h.more || !h.next)) return;
  const range = historyRange(h.from, h.to);
  if (range.error) { h.error = range.error; h.loaded = true; h.rows = []; h.summary = null; renderHistory(true); return; }
  const seq = ++h.seq;
  h.busy = true;
  try {
    const limit = more ? HISTORY_PAGE : Math.max(HISTORY_PAGE, Math.min(500, keep || 0));
    const r = await hubFetch("/api/hub/history?" + historyQuery(histFilters(), { limit: limit, before: more ? h.next : "", checkFiles: true }));
    if (seq !== h.seq) return;
    const items = Array.isArray(r.items) ? r.items : [];
    h.rows = more ? h.rows.concat(items) : items;
    h.next = r.next_cursor || null;
    h.more = !!r.more && !!h.next;
    h.summary = r.summary || { count: h.rows.length, total_bytes: 0, failed: 0 };
    h.states = r.states || null;
    h.truncated = !!r.summary_truncated;
    h.retention = num(r.retention_days) || 0;
    h.error = "";
    h.loaded = true;
    if (!more) {
      const have = {};
      h.rows.forEach((x) => { have[x.id] = true; });
      Object.keys(h.sel).forEach((id) => { if (!have[id]) delete h.sel[id]; });
    }
  } catch (err) {
    if (seq !== h.seq) return;
    h.error = message(err);
    if (!more) h.loaded = true;
  } finally {
    if (seq === h.seq) h.busy = false;
  }
  renderHistory(!more);
}

function histReload() { loadHistory(false, S.hist.rows.length); }

function histSelectedIds() {
  const have = {};
  S.hist.rows.forEach((r) => { if (r.owner === "hub") have[r.id] = true; });
  return Object.keys(S.hist.sel).filter((id) => S.hist.sel[id] && have[id]);
}

function renderHistory(reset) {
  const h = S.hist;
  const box = $("#histList");
  const empty = historyEmpty(h);
  const summary = h.summary || { count: 0, total_bytes: 0, failed: 0 };
  $("#histCount").textContent = h.error && !h.loaded ? "" : !h.loaded ? "reading\u2026" : historySummaryText(summary, h.truncated);
  const failed = h.states ? num(h.states.failed) || 0 : summary.failed;
  const chip = $("#histFailedChip");
  chip.hidden = !h.loaded || (!failed && h.state !== "failed");
  chip.textContent = "Failed " + failed;
  chip.setAttribute("aria-pressed", h.state === "failed" ? "true" : "false");
  chip.classList.toggle("is-active", h.state === "failed");
  $("#histClearFilters").hidden = !historyFiltersActive(h);
  $("#histCsv").disabled = !(summary.count > 0) || h.exporting;
  $("#histJson").disabled = !(summary.count > 0) || h.exporting;
  // the list: a reset rebuilds it; otherwise only the rows that arrived are added at the end
  const keepTop = box.scrollTop;
  if (reset) { box.textContent = ""; h.drawn = 0; h.lastGroup = ""; }
  if (reset || h.drawn < h.rows.length || !box.firstChild) {
    box.querySelectorAll(".hist__empty,.hist__note").forEach((n) => n.remove());
    if (h.error && !h.rows.length) {
      box.appendChild(el("div", { cls: "hist__empty", text: "The history couldn't be read: " + h.error }));
    } else if (empty) {
      const wrap = el("div", { cls: "hist__empty hist__empty--" + empty.kind }, [el("div", { text: empty.text })]);
      if (empty.kind === "filtered") wrap.appendChild(el("button", { cls: "btn btn--sm", text: "Clear filters", attrs: { type: "button", id: "histEmptyClear" }, on: { click: clearHistoryFilters } }));
      else { const note = historyRetentionNote(h.retention); if (note) wrap.appendChild(el("div", { cls: "small", text: note })); }
      box.appendChild(wrap);
    } else if (h.loaded) {
      drawHistoryRows();
    }
  }
  if (!reset) box.scrollTop = keepTop;
  if (h.loaded && !empty && !h.more && h.rows.length && !h.error) {
    const note = historyRetentionNote(h.retention);
    if (note && !box.querySelector(".hist__note")) box.appendChild(el("div", { cls: "hist__note small", text: note }));
  }
  const moreBtn = $("#histMore");
  moreBtn.hidden = !h.more && !(h.error && h.rows.length);
  moreBtn.disabled = h.busy;
  moreBtn.textContent = h.error && h.rows.length ? "Couldn't load more: " + h.error + " (try again)" : h.busy ? "Loading\u2026" : "Load more (" + Math.max(0, summary.count - h.rows.length) + " left)";
  renderHistoryBulk();
}

// Draw the rows that are loaded but not drawn yet, with a sticky header wherever the day group changes.
function drawHistoryRows() {
  const h = S.hist;
  const box = $("#histList");
  const now = Date.now();
  for (let i = h.drawn; i < h.rows.length; i++) {
    const r = h.rows[i];
    if (h.sort === "newest") {
      const g = historyGroup(r, now);
      if (g.key !== h.lastGroup) { box.appendChild(el("div", { cls: "hist__group", text: g.label, attrs: { role: "presentation", "data-group": g.key } })); h.lastGroup = g.key; }
    }
    box.appendChild(historyRow(r, now));
  }
  h.drawn = h.rows.length;
}

function renderHistoryBulk() {
  const ids = histSelectedIds();
  const bar = $("#histBulk");
  bar.hidden = !ids.length;
  $("#histSelCount").textContent = ids.length + " selected";
  const redo = $("#histBulkRedo");
  redo.disabled = ids.length > HISTORY_REDOWNLOAD_MAX;
  redo.title = ids.length > HISTORY_REDOWNLOAD_MAX ? "Select at most " + HISTORY_REDOWNLOAD_MAX + " to download again in one go" : "Queue the selected downloads again";
  $("#histList").querySelectorAll(".hist__check").forEach((c) => {
    const on = !!S.hist.sel[c.getAttribute("data-id")];
    c.checked = on;
    const row = c.closest ? c.closest(".hist__row") : null;
    if (row) row.classList.toggle("is-selected", on);
  });
}

function clearHistoryFilters() {
  const h = S.hist;
  h.state = "all"; h.search = ""; h.from = ""; h.to = ""; h.sel = {}; h.last = "";
  $("#histState").value = "all"; $("#histSearch").value = ""; $("#histFrom").value = ""; $("#histTo").value = "";
  loadHistory(false, 0);
}

function historyRow(r, nowMs) {
  const m = historyRowModel(r, nowMs);
  const thumb = r.has_thumb
    ? el("img", { cls: "hist__thumb", attrs: { src: "/api/hub/thumb/" + encodeURIComponent(r.id), alt: "", loading: "lazy" } })
    : el("span", { cls: "hist__thumb", text: KIND_ICON[r.kind] || KIND_ICON.other, attrs: { "aria-hidden": "true" } });
  const acts = [];
  rowActions(r).filter((a) => ["open", "reveal", "retry"].indexOf(a.verb) !== -1).forEach((a) => {
    const fileVerb = a.verb === "open" || a.verb === "reveal";
    acts.push(el("button", {
      cls: "rbtn", text: a.label, attrs: { type: "button", "aria-label": a.label + ": " + m.title },
      on: { click: async (e) => {
        // a file the hub found missing is said so; nothing is sent to open it
        if (fileVerb && m.missing) { toast("That file is no longer there (it was moved or deleted), so there is nothing to " + (a.verb === "open" ? "open" : "show") + ".", "warn"); return; }
        e.currentTarget.disabled = true; await runAction(r, a); histReload();
      } },
    }));
  });
  if (r.owner === "hub" && r.state === "done" && !m.missing) {
    acts.push(el("a", {
      cls: "rbtn", text: "Save a copy", title: "Download this file through the browser",
      attrs: { href: "/api/hub/file/" + encodeURIComponent(r.id), download: "", "aria-label": "Save a copy of " + m.title },
    }));
  }
  if (r.owner === "hub") {
    if (r.state !== "failed" || !acts.some((x) => x.textContent === "Retry")) {
      acts.push(el("button", {
        cls: "rbtn", text: r.state === "failed" ? "Retry" : "Download again", attrs: { type: "button", "aria-label": (r.state === "failed" ? "Retry: " : "Download again: ") + m.title },
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
      attrs: { type: "button", "aria-label": "Remove from history: " + m.title },
      on: { click: async () => {
        if (!(await askFirst("Remove from the history?", m.title + "\n\nOnly the list entry goes. The downloaded file is not touched.", "Remove"))) return;
        try { await hubFetch("/api/hub/history/remove", { ids: [r.id] }); toast("Removed from the history", "info"); }
        catch (err) { toast(message(err), "warn"); }
        histReload();
      } },
    }));
  }
  const line = [el("span", { cls: "tag", text: m.badge })];
  [m.size, m.took, m.when].forEach((t) => { if (t) line.push(el("span", { text: t })); });
  if (m.missing) line.push(el("span", { cls: "tag tag--warn", text: "file missing", title: "The file is no longer where the hub saved it" }));
  const main = [
    el("div", { cls: "hist__title", text: m.title, title: m.title }),
    el("div", { cls: "hist__meta", title: historyMeta(r, nowMs) }, line),
  ];
  if (m.dest) main.push(el("div", { cls: "hist__dest", text: "\u2192 " + m.dest, title: String(r.dest || "") }));
  if (m.reason) main.push(el("div", { cls: "hist__reason", text: m.reason, title: String(r.error || "") }));
  const kids = [];
  if (m.selectable) {
    kids.push(el("input", {
      cls: "hist__check", attrs: { type: "checkbox", "data-id": r.id, "aria-label": "Select " + m.title },
      on: { click: (e) => {
        const h = S.hist;
        const on = e.currentTarget.checked;
        if (e.shiftKey && h.last) {
          const ids = h.rows.filter((x) => x.owner === "hub").map((x) => x.id);
          selectionRange(ids, h.last, r.id).forEach((id) => { if (on) h.sel[id] = true; else delete h.sel[id]; });
        } else if (on) h.sel[r.id] = true; else delete h.sel[r.id];
        h.last = r.id;
        renderHistoryBulk();
      } },
    }));
  } else {
    kids.push(el("span", { cls: "hist__check hist__check--none", attrs: { "aria-hidden": "true" } }));
  }
  kids.push(thumb, el("div", { cls: "hist__main" }, main), el("div", { cls: "hist__actions" }, acts));
  return el("div", { cls: "hist__row state-" + r.state + " tone-" + m.tone + (m.missing ? " is-missing" : "") + (S.hist.sel[r.id] ? " is-selected" : ""), attrs: { role: "listitem", "data-id": r.id } }, kids);
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

// What is listed, all of it: the rows loaded so far, and when the list has more, the rest of the same filtered set (pages of 500).
async function historyAll() {
  const h = S.hist;
  if (!h.more) return h.rows.slice();
  const rows = h.rows.slice();
  let cursor = h.next;
  for (let page = 0; cursor && page < HISTORY_EXPORT_PAGES; page++) {
    const r = await hubFetch("/api/hub/history?" + historyQuery(histFilters(), { limit: 500, before: cursor }));
    (Array.isArray(r.items) ? r.items : []).forEach((x) => rows.push(x));
    cursor = r.more ? r.next_cursor : null;
  }
  return rows;
}
async function historyExport(name, mime, make) {
  const h = S.hist;
  if (h.exporting) return;
  h.exporting = true; renderHistory(false);
  try { saveFile(name, mime, make(await historyAll())); }
  catch (err) { toast("The export failed: " + message(err), "warn"); }
  h.exporting = false; renderHistory(false);
}
$("#histCsv").addEventListener("click", () => historyExport("download-history.csv", "text/csv;charset=utf-8", historyCsv));
$("#histJson").addEventListener("click", () => historyExport("download-history.json", "application/json", historyJson));

function histFilterChanged() { S.hist.sel = {}; S.hist.last = ""; loadHistory(false, 0); }
$("#histState").addEventListener("change", (e) => { S.hist.state = e.target.value; histFilterChanged(); });
$("#histSort").addEventListener("change", (e) => { S.hist.sort = e.target.value; histFilterChanged(); });
$("#histFrom").addEventListener("change", (e) => { S.hist.from = e.target.value; histFilterChanged(); });
$("#histTo").addEventListener("change", (e) => { S.hist.to = e.target.value; histFilterChanged(); });
$("#histFailedChip").addEventListener("click", () => {
  S.hist.state = S.hist.state === "failed" ? "all" : "failed";
  $("#histState").value = S.hist.state;
  histFilterChanged();
});
$("#histClearFilters").addEventListener("click", clearHistoryFilters);
$("#histSearch").addEventListener("input", (e) => {
  clearTimeout(histTimer);
  const v = e.target.value;
  histTimer = setTimeout(() => { S.hist.search = v; histFilterChanged(); }, 250);
});
// more rows as the person scrolls near the end; the button under the list does the same for the keyboard
$("#histList").addEventListener("scroll", () => {
  const b = $("#histList");
  if (b.scrollTop + b.clientHeight >= b.scrollHeight - 2000) loadHistory(true);
});
$("#histMore").addEventListener("click", () => loadHistory(true));

// ---- selection and the two bulk buttons.  Neither touches a file: Remove takes list entries out, Re-download queues new downloads.
$("#histSelAll").addEventListener("click", () => {
  S.hist.rows.forEach((r) => { if (r.owner === "hub") S.hist.sel[r.id] = true; });
  renderHistoryBulk();
});
$("#histSelNone").addEventListener("click", () => { S.hist.sel = {}; S.hist.last = ""; renderHistoryBulk(); });
$("#histBulkRemove").addEventListener("click", async () => {
  const ids = histSelectedIds();
  if (!ids.length) return;
  const t = historyRemoveText(ids.length);
  if (!(await askFirst(t.title, t.body, t.label))) return;
  let removed = 0;
  const skipped = [];
  try {
    for (let i = 0; i < ids.length; i += 200) {
      const r = await hubFetch("/api/hub/history/remove", { ids: ids.slice(i, i + 200) });
      removed += r.removed || 0;
      (r.skipped || []).forEach((x) => skipped.push(x));
    }
    toast("Removed " + removed + " from the history" + (skipped.length ? " (" + skipped.length + " could not be removed: " + String(skipped[0].why || "").slice(0, 120) + ")" : ""), skipped.length ? "warn" : "info");
  } catch (err) { toast(message(err), "warn"); }
  S.hist.sel = {}; S.hist.last = "";
  histReload();
});
$("#histBulkRedo").addEventListener("click", async () => {
  const ids = histSelectedIds();
  if (!ids.length || ids.length > HISTORY_REDOWNLOAD_MAX) return;
  const t = historyRedownloadText(ids.length);
  if (!(await askFirst(t.title, t.body, t.label))) return;
  let queued = 0;
  const why = [];
  for (const id of ids) {
    try { await hubFetch("/api/hub/redownload", { id: id }); queued++; } catch (err) { why.push(message(err)); }
  }
  toast("Queued " + queued + " again" + (why.length ? " (" + why.length + " could not be: " + why[0].slice(0, 120) + ")" : ""), why.length ? "warn" : "info");
  S.hist.sel = {}; S.hist.last = "";
  schedule(300);
  renderHistoryBulk();
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
  S.hist.sel = {};
  histReload();
});

// ---- the waiting queue: drag a row, or use its arrows (phase 9 option B)

// The rows with the waiting queue in the order `ids` (earlier = starts sooner).  The hub's own `reorder` does the same
// sum: priority counts down from the length.  Pure; rows not in `ids` are returned as they were.
function applyOrder(rows, ids) {
  const list = ids || [];
  return (rows || []).map((r) => (list.indexOf(r.id) === -1 ? r : Object.assign({}, r, { priority: list.length - list.indexOf(r.id) })));
}

async function sendOrder(ids) {
  // Show the new order at once; the stream's next tick (up to a second away) would otherwise be the first the
  // person sees of it.  If the hub refuses, put the old rows back and say so.
  const before = S.rows;
  S.rows = applyOrder(S.rows, ids);
  render();
  try { await hubFetch("/api/hub/reorder", { ids: ids }); schedule(300); }
  catch (err) { S.rows = before; render(); toast(message(err), "warn"); }
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

// ---- dragging a whole group, or moving it with the arrows (the Batch headers)

function moveGroup(key, delta) {
  const next = stepGroupInOrder(queueOrder(S.rows), S.rows, key, delta);
  if (next) sendOrder(next);
}

// Every Batch header takes a drop; only a header with waiting rows can be picked up.  The drop goes to the same
// reorder route a row's does (sendOrder): the group's waiting rows travel as one block (moveGroupInOrder).
function wireGroupDrag(head, key, draggable) {
  const clear = () => head.classList.remove("drop-before", "drop-after", "is-dragging");
  if (draggable) {
    head.addEventListener("dragstart", (e) => {
      if (S.dragId) return;
      S.dragGroup = key;
      S.hold = Date.now() + 60000;                // don't rebuild the list under the drag
      try { e.dataTransfer.setData("application/x-jarvis-hub-group", key); e.dataTransfer.effectAllowed = "move"; } catch (err) { /* old browser */ }
      head.classList.add("is-dragging");
    });
    head.addEventListener("dragend", () => {
      S.dragGroup = "";
      S.hold = 0;
      $("#list").querySelectorAll(".drop-before,.drop-after,.is-dragging").forEach((n) => n.classList.remove("drop-before", "drop-after", "is-dragging"));
      render();
    });
  }
  head.addEventListener("dragover", (e) => {
    if (!S.dragGroup || S.dragGroup === key) return;
    e.preventDefault();
    const box = head.getBoundingClientRect();
    const after = e.clientY > box.top + box.height / 2;
    head.classList.toggle("drop-after", after);
    head.classList.toggle("drop-before", !after);
  });
  head.addEventListener("dragleave", () => head.classList.remove("drop-before", "drop-after"));
  head.addEventListener("drop", (e) => {
    if (!S.dragGroup || S.dragGroup === key) return;
    e.preventDefault();
    e.stopPropagation();
    const after = head.classList.contains("drop-after");
    const moving = S.dragGroup;
    const order = queueOrder(S.rows);
    clear();
    S.dragGroup = "";
    S.hold = 0;
    if (!groupWaiting(order, S.rows, key).length) { toast("Drop it on a group that still has downloads waiting.", "warn"); render(); return; }
    sendOrder(moveGroupInOrder(order, S.rows, moving, key, after));
  });
}

// ---- the torrent file picker (N24): a folder tree with tick boxes, search, quick choices and a running total

function openTorrentGroup(section) {
  const info = section.torrent || {};
  if (!/^[0-9a-f]{40}$/i.test(String(info.infohash || ""))) { toast("I can't tell which torrent this group came from.", "warn"); return; }
  openPicker("magnet:?xt=urn:btih:" + info.infohash, section.key.slice(2), section.title);
}

function openPicker(source, group, name) {
  const seq = (S.picker ? S.picker.seq : 0) + 1;
  S.picker = { seq: seq, source: String(source || ""), group: group || "", name: name || "", loading: true, error: "", data: null,
    sel: {}, search: "", open: {}, busy: false, shell: null };
  S.selected = null;
  S.detail = null;
  render();
  buildPickerShell();
  drawPicker();
  loadPicker(seq);
}

async function loadPicker(seq) {
  const P = S.picker;
  try {
    const data = await hubFetch("/api/hub/torrent/files", { source: P.source });
    if (!S.picker || S.picker.seq !== seq) return;
    P.data = data;
    P.loading = false;
    if (!P.group && data.group) P.group = data.group;            // this torrent already has a group: the files join it
    P.name = P.name || data.name || "";
  } catch (err) {
    if (!S.picker || S.picker.seq !== seq) return;
    P.loading = false;
    P.error = message(err);
  }
  drawPicker();
}

function closePicker() {
  S.picker = null;
  S.sig.detail = "";
  render();
}

function buildPickerShell() {
  const P = S.picker;
  const box = $("#picker");
  box.textContent = "";
  const search = el("input", { cls: "search__input", attrs: { type: "search", spellcheck: "false", autocomplete: "off", "aria-label": "Search the files", placeholder: "Search the files\u2026" },
    on: { input: (e) => { P.search = e.target.value; drawPickerTree(); } } });
  const quick = el("span", { cls: "picker__quick", attrs: { role: "group", "aria-label": "Quick choices" } });
  PICK_QUICK.forEach((q) => quick.appendChild(el("button", { cls: "btn btn--sm", text: q[1], attrs: { type: "button" }, on: { click: () => {
    P.sel = {};
    pickerQuick(P.data ? P.data.files : [], q[0]).forEach((i) => { P.sel[i] = true; });
    drawPickerTree(); drawPickerFoot();
  } } })));
  P.shell = {
    head: el("div", { cls: "picker__head" }),
    status: el("div", { cls: "picker__status", attrs: { "aria-live": "polite" } }),
    bar: el("div", { cls: "picker__bar" }, [search, quick]),
    tree: el("div", { cls: "picker__tree", attrs: { role: "tree", "aria-label": "Files of the torrent" } }),
    foot: el("div", { cls: "picker__foot" }),
    search: search,
  };
  [P.shell.head, P.shell.status, P.shell.bar, P.shell.tree, P.shell.foot].forEach((n) => box.appendChild(n));
}

function drawPicker() {
  const P = S.picker;
  if (!P || !P.shell) return;
  P.shell.head.textContent = "";
  P.shell.head.appendChild(el("span", { cls: "picker__name", text: P.name || "A torrent", title: P.name || "" }));
  P.shell.head.appendChild(el("button", { cls: "btn btn--sm", text: "Back", attrs: { type: "button" }, on: { click: closePicker } }));
  const D = P.data;
  P.shell.status.className = "picker__status" + (P.error ? " bad" : "");
  P.shell.status.textContent = P.loading ? "Reading the file list\u2026 a magnet link can take up to half a minute while the torrent finds peers."
    : P.error ? P.error
      : D ? D.count + (D.count === 1 ? " file" : " files") + DOT + fmtBytes(D.size) + (D.hidden ? DOT + D.hidden + " more not shown" : "") + (P.group ? DOT + "adding to the group already on the list" : "") : "";
  P.shell.bar.hidden = !D;
  drawPickerTree();
  drawPickerFoot();
}

function drawPickerTree() {
  const P = S.picker;
  if (!P || !P.shell) return;
  const tree = P.shell.tree;
  tree.textContent = "";
  if (!P.data) return;
  const files = P.data.files || [];
  const tick = (ids, on) => { ids.forEach((i) => { if (on) P.sel[i] = true; else delete P.sel[i]; }); drawPickerTree(); drawPickerFoot(); };
  const fileRow = (f, depth, name) => {
    const ok = selectableFile(f);
    const box = el("input", { attrs: { type: "checkbox", "aria-label": "Choose " + name } });
    box.checked = !!P.sel[f.index] && ok;
    box.disabled = !ok;
    box.addEventListener("change", () => tick([f.index], box.checked));
    const flags = [];
    if (f.video) flags.push("video");
    if (f.sample) flags.push("sample");
    if (f.in_group) flags.push("already added");
    return el("label", { cls: "pk pk--file" + (ok ? "" : " is-off"), attrs: { role: "treeitem", style: "padding-left:" + (6 + depth * 14) + "px" }, title: f.refused ? "Not downloadable: " + f.refused : "" }, [
      box,
      el("span", { cls: "pk__n", text: name }),
      el("span", { cls: "pk__f", text: f.refused ? "unsafe name: skipped" : flags.join(DOT) }),
      el("span", { cls: "pk__s", text: fmtBytes(f.size) }),
    ]);
  };
  const query = P.search.trim();
  if (query) {
    const hits = pickerFilter(files, query);
    if (!hits.length) tree.appendChild(el("div", { cls: "empty", text: "No file matches that." }));
    hits.slice(0, 500).forEach((f) => tree.appendChild(fileRow(f, 0, f.path)));
    return;
  }
  const walk = (node, depth) => {
    node.dirs.forEach((d) => {
      const isOpen = P.open[d.path] === undefined ? depth < 1 : P.open[d.path];
      const on = d.ids.length > 0 && d.ids.every((i) => P.sel[i]);
      const some = !on && d.ids.some((i) => P.sel[i]);
      const box = el("input", { attrs: { type: "checkbox", "aria-label": "Choose every file in " + d.name } });
      box.checked = on;
      box.indeterminate = some;
      box.disabled = !d.ids.length;
      box.addEventListener("change", () => tick(d.ids, box.checked));
      tree.appendChild(el("div", { cls: "pk pk--dir", attrs: { role: "treeitem", "aria-expanded": isOpen ? "true" : "false", style: "padding-left:" + (6 + depth * 14) + "px" } }, [
        el("button", { cls: "section__toggle", text: isOpen ? "\u25BE" : "\u25B8", attrs: { type: "button", "aria-label": (isOpen ? "Collapse " : "Expand ") + d.name },
          on: { click: () => { P.open[d.path] = !isOpen; drawPickerTree(); } } }),
        box,
        el("span", { cls: "pk__n", text: d.name, title: d.path }),
        el("span", { cls: "pk__f", text: d.count + (d.count === 1 ? " file" : " files") }),
        el("span", { cls: "pk__s", text: fmtBytes(d.size) }),
      ]));
      if (isOpen) walk(d, depth + 1);
    });
    node.files.forEach((f) => tree.appendChild(fileRow(f, depth, f.leaf)));
  };
  walk(torrentTree(files), 0);
}

function drawPickerFoot() {
  const P = S.picker;
  if (!P || !P.shell) return;
  const foot = P.shell.foot;
  foot.textContent = "";
  if (!P.data) return;
  const t = pickerTotals(P.data.files, P.sel, P.data.free_bytes);
  const block = pickerBlock(t);
  const btn = el("button", { cls: "btn btn--primary", text: t.count ? "Add " + t.count + (t.count === 1 ? " file" : " files") : "Add files", attrs: { type: "button" }, on: { click: () => submitPicker() } });
  btn.disabled = !!block || P.busy;
  foot.appendChild(el("span", { cls: "picker__total" + (t.tooMany || t.tooBig ? " bad" : ""), text: pickerTotalText(t), attrs: { "aria-live": "polite" } }));
  if (block && t.count) foot.appendChild(el("span", { cls: "picker__why bad", text: block }));
  foot.appendChild(btn);
}

async function submitPicker() {
  const P = S.picker;
  if (!P || !P.data || P.busy) return;
  const t = pickerTotals(P.data.files, P.sel, P.data.free_bytes);
  if (pickerBlock(t)) return;
  const indexes = P.data.files.filter((f) => P.sel[f.index] && selectableFile(f)).map((f) => f.index);
  P.busy = true;
  drawPickerFoot();
  try {
    const r = await hubFetch("/api/hub/torrent/add", { source: P.source, indexes: indexes, group: P.group });
    toast("Queued " + r.count + (r.count === 1 ? " file" : " files") + " of " + (r.group_title || "the torrent"), "success");
    if (r.refused && r.refused.length) toast(r.refused.length + " file(s) skipped: their names in the torrent aren't safe to save.", "warn");
    if (r.skipped && r.skipped.length) toast(r.skipped.length + " already in the group.", "info");
    if (r.note) toast(r.note, "warn");
    closePicker();
    schedule(200);
  } catch (err) {
    P.busy = false;
    toast(message(err), "warn");
    drawPickerFoot();
  }
}

$("#addChoose").addEventListener("click", () => {
  const source = torrentSource($("#addInput").value);
  if (source) openPicker(source, "");
});

host.onClose(() => {
  S.closed = true; clearTimeout(S.timer); clearTimeout(S.holdTimer); clearTimeout(hintTimer);
  clearInterval(S.liveTimer); clearInterval(S.srcTimer); clearTimeout(histTimer); clearTimeout(S.updTimer);
  if (S.es) { S.es.close(); S.es = null; }
});

(async function start() {
  await loadPrefs();
  $("#groupBy").value = S.groupBy;
  render();
  poll();
  startLive();
  loadUpdates(false);                              // the number on the Updates tab
})();
