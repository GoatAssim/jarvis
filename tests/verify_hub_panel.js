// Menu > Downloads panel (jarvis/actions/hub_ui/tool.js): the pure half, plus wiring checks on the source.
//
// tool.js is the body of an async function that receives `host`.  Called with host.__hubPureOnly
// it returns its pure helpers instead of touching the DOM, so they can be checked here with no
// browser.  Covers: byte / speed / ETA / age formatting; the status words per state; the bar's look
// per state (and that a stale provider row never looks like a running one); which buttons a row
// offers; views, filters, sort order, grouping; arrow-key selection; what the add box extracts and
// the hint it shows; and static checks that the panel never builds markup from data.
//
// Run: node tests/verify_hub_panel.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// web/public/*.js are browser scripts that hang their API on `window`.  web/package.json says "type": "module",
// so require() would hand back an empty namespace: run them with vm in a bare `window` instead.
function loadBrowserScript(name) {
  const win = {};
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "web", "public", name), "utf8"), vm.createContext({ window: win, console }), { filename: name });
  return win;
}

const ui = path.join(__dirname, "..", "jarvis-cli", "jarvis", "actions", "hub_ui");
const source = fs.readFileSync(path.join(ui, "tool.js"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () { /* probe */ }).constructor;

let passed = 0;
let failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log("ok      " + name); } else { failed++; console.log("FAILED  " + name); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

(async () => {
  const pure = await new AsyncFunction("host", source)({ __hubPureOnly: true });
  check("the pure helpers come back without touching a DOM", pure && typeof pure.barLook === "function");

  // ---- formatting
  check("bytes", pure.fmtBytes(0) === "0 B" && pure.fmtBytes(1023) === "1023 B" && pure.fmtBytes(1536) === "1.5 KB"
    && pure.fmtBytes(5 * 1024 * 1024) === "5.0 MB" && pure.fmtBytes(150 * 1024 ** 3) === "150 GB");
  check("bytes: junk is blank", pure.fmtBytes(null) === "" && pure.fmtBytes(NaN) === "" && pure.fmtBytes(-1) === "" && pure.fmtBytes("x") === "");
  check("speed", pure.fmtSpeed(2 * 1024 * 1024) === "2.0 MB/s" && pure.fmtSpeed(0) === "" && pure.fmtSpeed(null) === "");
  check("eta", pure.fmtEta(45) === "45 s" && pure.fmtEta(600) === "10 min" && pure.fmtEta(3900) === "1 h 05 min" && pure.fmtEta(-3) === "" && pure.fmtEta(null) === "");
  check("age", pure.fmtAgo(30) === "30 s ago" && pure.fmtAgo(600) === "10 min ago" && pure.fmtAgo(7200) === "2 h ago" && pure.fmtAgo(null) === "a while ago");

  // ---- the words on a row
  const row = (o) => Object.assign({ id: "h_1", owner: "hub", engine: "http", state: "queued", derived: [], capabilities: [], stale: false }, o);
  check("queued", pure.statusText(row({})) === "Queued" && pure.statusText(row({ retry_in_s: 12 })) === "Waiting to retry in 12 s");
  check("downloading shows percent, speed, ETA",
    pure.statusText(row({ state: "downloading", percent: 41.6, speed_bps: 1048576, eta_s: 90 })) === "42% \u00B7 1.0 MB/s \u00B7 ETA 2 min");
  check("a value the hub estimated is marked with \u2248",
    pure.statusText(row({ state: "downloading", percent: 50, speed_bps: 1048576, eta_s: 60, derived: ["percent", "speed", "eta"] })).split("\u2248").length === 4);
  check("downloading with nothing known still says so", pure.statusText(row({ state: "downloading" })) === "Downloading");
  check("post-processing shows the phase and its place",
    pure.statusText(row({ state: "postprocessing", phase: "verifying", phase_index: 2, phase_count: 2 })) === "Verifying (2/2)");
  check("paused remembers where", pure.statusText(row({ state: "paused", percent: 37 })) === "Paused at 37%");
  check("stalled says when it retries", pure.statusText(row({ state: "stalled", retry_in_s: 8 })) === "Stalled \u2014 retry in 8 s");
  check("failed / cancelled / done", pure.statusText(row({ state: "failed" })) === "Failed" && pure.statusText(row({ state: "cancelled" })) === "Cancelled"
    && pure.statusText(row({ state: "done" })) === "Done");
  check("a silent provider row says how long it has been quiet",
    pure.statusText(row({ owner: "provider", state: "downloading", stale: true, age_s: 600 })) === "No update 10 min ago");
  check("sizes", pure.sizeText(row({ bytes_done: 1048576, bytes_total: 2097152 })) === "1.0 MB / 2.0 MB" && pure.sizeText(row({ bytes_total: 2048 })) === "2.0 KB" && pure.sizeText(row({})) === "");


  // ---- preferences: speed limit, notifications, when it finishes, folders
  check("limit text", pure.fmtLimit(0) === "Unlimited" && pure.fmtLimit(500) === "500 KB/s" && pure.fmtLimit(1024) === "1 MB/s" && pure.fmtLimit(2560) === "2.5 MB/s" && pure.fmtLimit(null) === "Unlimited");
  check("a typed speed becomes KB/s", eq(pure.parseLimit("500", "kb"), { kbps: 500 }) && eq(pure.parseLimit("2", "mb"), { kbps: 2048 }) && eq(pure.parseLimit("1,5", "mb"), { kbps: 1536 }));
  check("an empty speed means no limit", eq(pure.parseLimit("", "kb"), { kbps: 0 }) && eq(pure.parseLimit("  ", "mb"), { kbps: 0 }));
  check("a bad speed is an error, not a number", !!pure.parseLimit("fast", "kb").error && !!pure.parseLimit("-5", "kb").error && !!pure.parseLimit("99999999", "mb").error);
  check("a stored limit fills the two boxes", eq(pure.limitFields(2048), { value: "2", unit: "mb" }) && eq(pure.limitFields(300), { value: "300", unit: "kb" }) && eq(pure.limitFields(0), { value: "", unit: "kb" }) && eq(pure.limitFields(1536), { value: "1536", unit: "kb" }));
  check("the limit in force reads in words", pure.limitSummary({ kbps: 0, source: "none" }) === "No speed limit" && pure.limitSummary({ kbps: 1024, source: "manual" }) === "Limit 1 MB/s"
    && pure.limitSummary({ kbps: 256, source: "schedule" }) === "Limit 256 KB/s (scheduled)" && pure.limitSummary({ kbps: 0, source: "schedule" }) === "Unlimited (scheduled)" && pure.limitSummary(null) === "No speed limit");
  const q1 = pure.quickSpeedOptions({ kbps: 1024, source: "manual" });
  check("the quick menu: presets, the current one selected, a way into the full settings",
    q1.length === pure.LIMIT_PRESETS.length + 1 && q1.filter((o) => o.selected).length === 1 && q1.find((o) => o.selected).value === "1024" && q1[q1.length - 1].value === "custom" && q1[0].label === "No limit");
  const q2 = pure.quickSpeedOptions({ kbps: 777, source: "manual" });
  check("a limit that isn't a preset still shows up, selected", q2.find((o) => o.selected).value === "777" && q2.find((o) => o.selected).label === "777 KB/s");
  check("a scheduled limit is marked in the menu", pure.quickSpeedOptions({ kbps: 256, source: "schedule" }).find((o) => o.selected).label.includes("(scheduled)"));

  check("four 'when it finishes' choices; the dangerous one says what it won't do", eq(pure.OPEN_CHOICES.map((c) => c.id), ["off", "reveal", "folder", "open"]) && /never opened/.test(pure.OPEN_CHOICES[3].help));
  check("the per-download menu starts with 'use my default' and names it", pure.openMenu("reveal")[0].value === "default" && /show the file in its folder/.test(pure.openMenu("reveal")[0].label) && pure.openMenu("off").length === 5);
  // The panel keeps no list of levels or notification types: until the hub's answer arrives the menu holds
  // only "default", the tick lists are empty and the panel says it is loading.
  check("before the first answer there are no levels, no types, and a loading line",
    pure.NOTIFY_OPTIONS.actions.length === 0 && /Loading/.test(pure.NOTIFY_OPTIONS_WAIT || "Loading"));
  const FIVE = ["silent", "standard", "persistent", "broadcast", "confirm"].map((n, i) => ({ value: i + 1, name: n }));
  check("the first answer is applied and reported as a change",
    pure.applyNotifyOptions({ version: "v1", levels: FIVE, actions: [{ id: "inbox", label: "Inbox" }, { id: "small_toast", label: "Small toast" }, { id: "discord", label: "Discord DM", dm: true }] }) === true);
  check("the same answer again changes nothing (no redraw)", pure.applyNotifyOptions({ version: "v1", levels: FIVE, actions: [{ id: "inbox", label: "Inbox" }, { id: "small_toast", label: "Small toast" }, { id: "discord", label: "Discord DM", dm: true }] }) === false);
  check("every action of the catalog is offered for one download, labelled by the catalog",
    pure.NOTIFY_OPTIONS.actions.length === 3 && pure.channelLabel("small_toast") === "Small toast" && pure.channelLabel("nope") === "nope");
  check("every notification type is listed, in order", eq(pure.NOTIFY_EVENTS.map((e) => e.id), ["done", "failed", "group_done", "queue_empty", "stalled", "started"]));



  check("the add form: no extras -> no extra arguments", eq(pure.addOptionsFromForm({ limit: "", unit: "kb", open: "default", notify: "default" }), { args: {} }));
  check("the add form: extras become dl_add arguments", eq(pure.addOptionsFromForm({ limit: "2", unit: "mb", open: "reveal", notify: "on" }), { args: { speed_limit_kbps: 2048, open_when_done: "reveal", notify: "on" } }));
  check("the add form: a bad speed is an error", !!pure.addOptionsFromForm({ limit: "x", unit: "kb", open: "default", notify: "default" }).error);
  check("one download's own choices in words", pure.itemPrefsText({}) === "" && pure.itemPrefsText({ limit_kbps: 300, open: "reveal", notify: "off" }) === "limit 300 KB/s \u00B7 then: show the file in its folder \u00B7 never notify");
  check("the add form: channels and events become dl_add arguments", eq(pure.addOptionsFromForm({ limit: "", unit: "kb", open: "default", notify: "default", channels: ["discord"], events: ["done", "failed"] }), { args: { notify_channels: ["discord"], notify_events: ["done", "failed"] } }));
  check("the add form: nothing ticked sends no notification choices", eq(pure.addOptionsFromForm({ limit: "", unit: "kb", open: "default", notify: "default", channels: [], events: [] }), { args: {} }));
  check("a download's own channels, events and folder in words", pure.itemPrefsText({ notify_channels: ["discord"], notify_events: ["done"], subdir: "Y" }) === "notify via Discord DM \u00B7 notify when it finishes \u00B7 saves in Y");   // the label is the hub catalog's (loaded above), not the panel's
  check("a download's own full path in words", pure.itemPrefsText({ dest_abs: "D:\\Games" }) === "saves in D:\\Games");
  {
    const html3 = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
    check("the add form has the per-download notification pickers", html3.includes('id="addChannels"') && html3.includes('id="addEvents"'));
  }

  // ---- the bar (plan 11.4)
  const look = (o) => pure.barLook(row(o));
  check("determinate", eq(look({ state: "downloading", percent: 40 }), { kind: "determinate", pct: 40 }));
  check("indeterminate when the percent is unknown", look({ state: "downloading", percent: null }).kind === "indeterminate");
  check("post-processing is a full striped bar", eq(look({ state: "postprocessing", percent: 100 }), { kind: "striped", pct: 100 }));
  check("queued is empty", eq(look({ state: "queued" }), { kind: "queued", pct: 0 }));
  check("paused and stalled keep their fill", eq(look({ state: "paused", percent: 30 }), { kind: "paused", pct: 30 }) && eq(look({ state: "stalled", percent: 37 }), { kind: "stalled", pct: 37 }));
  check("failed keeps the percent it reached", eq(look({ state: "failed", percent: 41 }), { kind: "failed", pct: 41 }));
  check("cancelled is hollow", eq(look({ state: "cancelled", percent: 60 }), { kind: "cancelled", pct: 0 }));
  check("done is full", eq(look({ state: "done" }), { kind: "done", pct: 100 }));
  check("a stale provider row never looks like a running one", look({ owner: "provider", state: "downloading", percent: 20, stale: true }).kind === "stale");
  check("but a stale flag on a finished row is ignored", look({ state: "done", stale: true }).kind === "done");
  check("a wild percent is clamped for the bar", look({ state: "downloading", percent: 250 }).pct === 100 && look({ state: "downloading", percent: -5 }).pct === 0);

  // ---- buttons
  const verbs = (o) => pure.rowActions(row(o)).map((a) => a.verb);
  check("a row offers exactly its capabilities, in a fixed order", eq(verbs({ capabilities: ["cancel", "pause"] }), ["pause", "cancel"]));
  check("done rows can open and reveal", eq(verbs({ state: "done", capabilities: ["reveal", "open"] }), ["open", "reveal"]));
  check("a read-only row has no buttons", eq(verbs({ capabilities: [] }), []) && eq(verbs({ capabilities: undefined }), []));
  check("an unknown capability gets no button", eq(verbs({ capabilities: ["fly", "pause"] }), ["pause"]));
  const cancel = pure.rowActions(row({ capabilities: ["cancel"] }))[0];
  check("cancel is the dangerous one and runs dl_cancel", cancel.danger === true && cancel.tool === "dl_cancel");
  const reveal = pure.rowActions(row({ capabilities: ["reveal"] }))[0];
  check("reveal runs dl_open", reveal.tool === "dl_open" && reveal.verb === "reveal");

  // ---- views, filters, sort, groups
  const rows = [
    row({ id: "a", state: "downloading", created_at: 10, title: "Alpha movie", engine: "http" }),
    row({ id: "b", state: "queued", created_at: 30, title: "Beta" }),
    row({ id: "c", state: "failed", created_at: 20, title: "Gamma", error: "checksum" }),
    row({ id: "d", state: "done", created_at: 5, title: "Delta" }),
    row({ id: "e", state: "downloading", created_at: 40, title: "Silent one", owner: "provider", provider: "demo", provider_label: "Demo provider", stale: true }),
    row({ id: "f", state: "paused", created_at: 1, title: "Paused thing", group_id: "g1", group_title: "Season 1" }),
    row({ id: "g", state: "cancelled", created_at: 2, title: "Cancelled thing", group_id: "g1", group_title: "Season 1" }),
    row({ id: "h", state: "stalled", created_at: 3, title: "Stalled thing" }),
  ];
  const ids = (list) => list.map((r) => r.id);
  const f = (o) => ids(pure.filterRows(rows, o));
  check("view: active excludes a stale provider row", eq(f({ view: "active" }), ["a"]));
  check("view: waiting is queued, paused, stalled", eq(f({ view: "waiting" }), ["b", "f", "h"]));
  check("view: failed includes cancelled", eq(f({ view: "failed" }), ["c", "g"]));
  check("view: done", eq(f({ view: "done" }), ["d"]));
  check("view: not reporting is the stale provider row", eq(f({ view: "stale" }), ["e"]));
  check("view: all", f({ view: "all" }).length === rows.length);
  check("counts per view", eq(pure.viewCounts(rows), { all: 8, active: 1, waiting: 3, scheduled: 0, failed: 2, done: 1, stale: 1 }));
  // N22 part d: the source/provider filter left the panel.  A `source` in the filter (an old saved one) changes nothing.
  check("a source in the filter is ignored: the filter is gone from the panel (N22 part d)", f({ source: "provider:demo" }).length === rows.length && f({ source: "hub:http" }).length === rows.length && pure.sourceList === undefined);
  check("search looks in title, subtitle, message and error", eq(f({ search: "alpha" }), ["a"]) && eq(f({ search: "CHECKSUM" }), ["c"]) && f({ search: "zzz" }).length === 0);
  check("filters combine", eq(f({ view: "waiting", search: "paused" }), ["f"]));

  check("sort: running first, then stalled (a silent provider row counts as one), queued, paused, failed, done, cancelled",
    eq(ids(pure.sortRows(rows)), ["a", "e", "h", "b", "f", "c", "d", "g"]));
  check("sort: newest first within a state", eq(ids(pure.sortRows([row({ id: "x", created_at: 1 }), row({ id: "y", created_at: 9 })])), ["y", "x"]));
  check("sort does not mutate its input", ids(rows)[0] === "a");

  const none = pure.groupRows(rows, "none");
  check("no grouping is one headerless section", none.length === 1 && none[0].title === "" && none[0].rows.length === rows.length);
  const bySource = pure.groupRows(rows, "source");
  check("group by source", bySource.length === 2 && bySource.map((s) => s.title).sort().join() === "Demo provider,HTTP");
  const byBatch = pure.groupRows(rows, "group");
  check("group by batch: the batch first with its progress, the rest as single downloads",
    byBatch[0].title === "Season 1" && byBatch[0].meta === "0/2 done" && byBatch[byBatch.length - 1].title === "Single downloads");

  check("arrow keys move through what is shown", pure.moveSelection(["a", "b", "c"], "a", 1) === "b" && pure.moveSelection(["a", "b", "c"], "c", 1) === "c"
    && pure.moveSelection(["a", "b", "c"], "b", -1) === "a" && pure.moveSelection(["a", "b", "c"], "a", -1) === "a");
  check("with nothing selected, down picks the first and up the last", pure.moveSelection(["a", "b"], null, 1) === "a" && pure.moveSelection(["a", "b"], null, -1) === "b");
  check("no rows, no selection", pure.moveSelection([], "a", 1) === null);

  // ---- the add box
  check("it extracts links from pasted text",
    eq(pure.extractInputs("see https://a.example/x.zip and\nhttp://b.example/y.iso thanks"), ["https://a.example/x.zip", "http://b.example/y.iso"]));
  check("it takes magnet links and infohashes", pure.extractInputs("magnet:?xt=urn:btih:abc").length === 1 && pure.extractInputs("0123456789abcdef0123456789abcdef01234567").length === 1);
  check("it ignores everything else", pure.extractInputs("hello world javascript:alert(1) file:///etc/passwd ftp://x/y").length === 0);
  check("it drops duplicates and caps at 50", pure.extractInputs("https://a.example/1 https://a.example/1").length === 1
    && pure.extractInputs(Array.from({ length: 80 }, (_, i) => "https://a.example/" + i).join(" ")).length === 50);
  check("hint: one good decision", eq(pure.hintFromDecisions([{ ok: true, engine: "http", reason: "direct link (HTTP engine)" }]), ["\u2192 HTTP: direct link (HTTP engine)", false]));
  check("hint: a problem wins and is flagged", eq(pure.hintFromDecisions([{ ok: true, engine: "http", reason: "x" }, { ok: false, problem: "private address" }]), ["private address", true]));
  check("hint: several good ones", eq(pure.hintFromDecisions([{ ok: true, engine: "http", reason: "a" }, { ok: true, engine: "http", reason: "b" }]), ["\u2192 2 links, queued as one batch", false]));
  check("hint: nothing", eq(pure.hintFromDecisions([]), ["", false]) && eq(pure.hintFromDecisions(undefined), ["", false]));

  // ---- card buttons, timed starts, live notifier options
  const caps = (state, list, extra) => row(Object.assign({ state: state, capabilities: list }, extra || {}));
  check("a card offers pause and cancel for a running download", eq(pure.cardActions(caps("downloading", ["pause", "cancel"])).map((a) => a.verb), ["pause", "cancel"]));
  check("a paused card offers resume and cancel", eq(pure.cardActions(caps("paused", ["resume", "cancel"])).map((a) => a.verb), ["resume", "cancel"]));
  check("open / reveal never appear on a card", eq(pure.cardActions(caps("done", ["open", "reveal"])), []));
  check("a read-only row has no card buttons", eq(pure.cardActions(caps("downloading", [])), []));
  const sched = caps("paused", ["resume", "cancel"], { scheduled_at: 1900000000, can_schedule: true });
  check("a scheduled download says so and when", /^Scheduled · starts /.test(pure.statusText(sched)));
  check("a scheduled download has its own tone", pure.toneOf(sched) === "scheduled" && pure.toneOf(caps("paused", [])) === "paused");
  check("the Scheduled view only holds scheduled rows", pure.viewMatches("scheduled", sched) && !pure.viewMatches("scheduled", caps("paused", [])));
  check("fmtWhen: today / tomorrow", pure.fmtWhen(Date.UTC(2026, 5, 10, 12) / 1000, Date.UTC(2026, 5, 10, 8)).length > 0 && /^tomorrow /.test(pure.fmtWhen(new Date(2026, 5, 11, 3, 0).getTime() / 1000, new Date(2026, 5, 10, 20, 0).getTime())) && /^today /.test(pure.fmtWhen(new Date(2026, 5, 10, 23, 0).getTime() / 1000, new Date(2026, 5, 10, 20, 0).getTime())));
  check("the add form's start time becomes start_at; 'now' sends nothing", eq(pure.addOptionsFromForm({ start: " tomorrow 3am " }).args, { start_at: "tomorrow 3am" }) && eq(pure.addOptionsFromForm({ start: "now" }).args, {}));
  check("a type and a level the notifier gains are offered with no change here, and a new `version` redraws",
    pure.applyNotifyOptions({ version: "v2", actions: [{ id: "discord", label: "Discord" }, { id: "telegram", label: "Telegram" }], levels: [{ value: 1, name: "silent" }, { value: 6, name: "panic" }] })
    && pure.NOTIFY_OPTIONS.actions.some((c) => c.id === "telegram")
    && pure.NOTIFY_OPTIONS.version === "v2");
  check("an older server that sends only `channels` still works", pure.applyNotifyOptions({ levels: FIVE, channels: [{ id: "inbox", label: "Inbox" }] }) === true && pure.NOTIFY_OPTIONS.actions.length === 1);
  check("an empty or missing answer changes nothing (the lists are kept)", !pure.applyNotifyOptions(null) && !pure.applyNotifyOptions({ channels: [], levels: [], actions: [] }) && pure.NOTIFY_OPTIONS.actions.length === 1);
  check("the panel has no LEVEL_NAMES or NOTIFY_CHANNELS of its own any more", !/\bLEVEL_NAMES\b|\bNOTIFY_CHANNELS\b/.test(source));
  check("card buttons don't also select the card", /e\.stopPropagation\(\); e\.preventDefault\(\)/.test(source));
  check("the panel never keeps its own copy of the level range", !/\[1, 2, 3, 4, 5\]/.test(source));

  // ---- static wiring: nothing from a download is ever turned into markup
  check("the panel never assigns HTML", !/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/.test(source));
  check("untrusted text is written with textContent", /textContent/.test(source));
  const ids2 = new Set([...source.matchAll(/\$\("#([A-Za-z0-9_-]+)"\)/g)].map((m) => m[1]));
  const html = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
  check("every element the script looks up exists in tool.html", [...ids2].every((id) => html.includes('id="' + id + '"')));
  const pyFile = fs.readFileSync(path.join(__dirname, "..", "jarvis-cli", "jarvis", "actions", "download_hub.py"), "utf8");
  const toolsBlock = pyFile.slice(pyFile.indexOf("\nTOOLS = {"), pyFile.indexOf("}", pyFile.indexOf("\nTOOLS = {")));
  const known = new Set([...toolsBlock.matchAll(/"([a-z_]+)":/g)].map((m) => m[1]));
  const used = new Set([...source.matchAll(/(?:runTool|callTool)\("([a-z_]+)"/g)].map((m) => m[1]).concat([...source.matchAll(/tool: "([a-z_]+)"/g)].map((m) => m[1])));
  check("the panel only runs tools that download_hub.py defines", used.size > 0 && [...used].every((t) => known.has(t)));
  check("a provider's text never chooses which tool runs (no tool-kind links, no dynamic names from rows)",
    !/callTool\(\s*l\./.test(source) && !/runTool\(\s*(row|r|l)\./.test(source));
  const html2 = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
  check("the buttons the panel keeps exist in tool.html, and the settings screen is gone", ["speedQuick", "openFolder", "settingsBtn", "detailPrefs", "addLimit", "addOpen", "addNotify", "tabs", "sourcesPane", "historyPane"].every((id) => html2.includes('id="' + id + '"')) && !/id="(settings|settingsNav|settingsPage|settingsSave|settingsRevert|settingsBack)"/.test(html2));
  check("the Settings button opens Settings > Downloads and the panel has no settings screen of its own", /JarvisSettings[\s\S]{0,200}show\(want \? \{ section: "downloads", anchor: want \} : \{ section: "downloads" \}\)/.test(source) && !/renderSettings|saveSettings|revertSettings|closeSettings|settingsOpen/.test(source));
  check("the panel changes no setting itself: its two shortcuts write with dl_prefs, and dl_settings is never called from here (Settings > Downloads owns it)", /callTool\("dl_prefs"/.test(source) && !/dl_settings/.test(source.replace(/\/\/[^\n]*/g, "")));
  check("an event's target isn't read after an await (it is cleared inside a shadow root)", !/await[^\n]*\n[^\n]*e\.target/.test(source.slice(source.indexOf("speedQuick\").addEventListener"), source.indexOf("speedQuick\").addEventListener") + 1200)));
  const css = fs.readFileSync(path.join(ui, "tool.css"), "utf8");
  check("the stylesheet defines a look for every bar kind", ["determinate", "indeterminate", "striped", "queued", "done", "failed", "stalled", "paused", "stale", "cancelled", "unknown"]
    .every((k) => k === "determinate" || css.includes(".bar-track." + k) || css.includes("." + k)));
  check("motion respects prefers-reduced-motion", css.includes("prefers-reduced-motion"));
  check("the stylesheet styles the tabs, History, the Sources cards and the queue drag", [".tabs", ".tab[", ".hist__row", ".prov", ".drop-before", ".speedq"].every((c) => css.includes(c)));

  // ---- History tab: what is listed, the exports, the words on a row
  const H = (o) => Object.assign({ id: "h_1", owner: "hub", engine: "http", state: "done", kind: "file", title: "a.zip", derived: [], capabilities: [], finished_at: 1000 }, o);
  const hrows = [H({ id: "h_a", title: "Old.iso", finished_at: 1000 }), H({ id: "h_b", title: "New.mp4", kind: "video", engine: "ytdlp", finished_at: 3000 }),
    H({ id: "h_c", title: "Bad file", state: "failed", error: "HTTP 404", finished_at: 2000 }), H({ id: "h_d", title: "Live one", state: "downloading" }),
    H({ id: "h_e", title: "Dropped", state: "cancelled", finished_at: 2500 })];
  check("the old client-side History filter is gone: the hub filters, sorts and counts now (brief 25)", pure.historyFilter === undefined);
  // ---- History rework (brief 25, N21): query, range, groups, summary, rows, selection, confirmations, empty states
  check("a History request asks for a page of 100 with the sort, and says only what is filtered",
    pure.historyQuery({}, {}) === "limit=100&sort=newest" && pure.historyQuery({ state: "all", sort: "bogus" }, {}) === "limit=100&sort=newest"
    && pure.historyQuery({ state: "failed", sort: "largest", search: " a b " }, { limit: 7, before: "X_y", checkFiles: true }) === "limit=7&sort=largest&states=failed&search=a%20b&before=X_y&check_files=1"
    && pure.historyQuery({}, { limit: 9999 }).startsWith("limit=500&") && pure.historyQuery({ state: "evil" }, {}) === "limit=100&sort=newest");
  const rg = pure.historyRange("2026-10-01", "2026-10-02");
  check("a date range runs from the start of the first local day to the end of the last",
    rg.error === "" && rg.since === new Date(2026, 9, 1, 0, 0, 0).getTime() / 1000 && rg.until === new Date(2026, 9, 2, 23, 59, 59).getTime() / 1000
    && pure.historyRange("", "").since === null && pure.historyRange("2026-10-05", "").until === null);
  check("a range that ends before it starts, or a bad date, is an error in words",
    /ends before it starts/.test(pure.historyRange("2026-10-05", "2026-10-01").error) && /Pick the dates again/.test(pure.historyRange("soon", "").error)
    && !/since=/.test(pure.historyQuery({ from: "2026-10-05", to: "2026-10-01" }, {})) && /since=\d+&until=\d+/.test(pure.historyQuery({ from: "2026-10-01", to: "2026-10-02" }, {})));
  const NOW = new Date(2026, 9, 24, 15, 0, 0).getTime();
  const at = (y, m, d, hh) => new Date(y, m, d, hh || 12, 0, 0).getTime() / 1000;
  const gl = (t) => pure.historyGroup({ finished_at: t }, NOW).label;
  check("groups: Today, Yesterday, This week (2 to 6 days ago), then the month",
    gl(at(2026, 9, 24, 1)) === "Today" && gl(at(2026, 9, 23)) === "Yesterday" && gl(at(2026, 9, 22)) === "This week" && gl(at(2026, 9, 18)) === "This week"
    && gl(at(2026, 9, 17)) === new Date(2026, 9, 17).toLocaleDateString([], { month: "long", year: "numeric" }) && gl(at(2026, 5, 3)) === new Date(2026, 5, 3).toLocaleDateString([], { month: "long", year: "numeric" })
    && pure.historyGroup({}, NOW).label === "Earlier" && gl(at(2026, 9, 25)) === "Today");
  const grows = [{ id: "a", finished_at: at(2026, 9, 24, 2) }, { id: "b", finished_at: at(2026, 9, 24, 1) }, { id: "c", finished_at: at(2026, 9, 23) }, { id: "d", finished_at: at(2026, 9, 1) }];
  check("a newest-first list is grouped in order; any other sort is one flat group",
    eq(pure.historyGroups(grows, NOW, "newest").map((g) => [g.label, g.rows.length]), [["Today", 2], ["Yesterday", 1], [new Date(2026, 9, 1).toLocaleDateString([], { month: "long", year: "numeric" }), 1]])
    && eq(pure.historyGroups(grows, NOW, "largest").map((g) => [g.label, g.rows.length]), [["", 4]]));
  check("the summary line counts the whole set, leaves out what is zero, and says when it was capped",
    pure.historySummaryText({ count: 128, total_bytes: 15246000000, failed: 3 }) === "128 downloads \u00b7 14.2 GB \u00b7 3 failed" && pure.historySummaryText({ count: 1, total_bytes: 0, failed: 0 }) === "1 download"
    && /the newest 20,000 counted/.test(pure.historySummaryText({ count: 20000 }, true)) && pure.historySummaryText(null) === "0 downloads");
  check("how long a download took reads in words", pure.fmtTook(0.4) === "" && pure.fmtTook(42) === "42 s" && pure.fmtTook(134) === "2 min 14 s" && pure.fmtTook(180) === "3 min" && pure.fmtTook(3900) === "1 h 05 min"
    && pure.historyDuration({ started_at: 100, finished_at: 234 }) === "2 min 14 s" && pure.historyDuration({ finished_at: 234 }) === "");
  check("the destination is the file's own name, from either kind of path, and only for finished rows",
    pure.historyDestName({ state: "done", dest: "/home/me/Downloads/a.zip" }) === "a.zip" && pure.historyDestName({ state: "done", dest: "C:\\Users\\me\\Downloads\\b.iso" }) === "b.iso"
    && pure.historyDestName({ state: "done", dest: "/tmp/folder/" }) === "folder" && pure.historyDestName({ state: "failed", dest: "/x/y" }) === "" && pure.historyDestName({ state: "done" }) === "");
  check("a failed row's reason is its first sentence, shortened; with no reason it says so and does not guess",
    pure.historyReason({ state: "failed", error: "HTTP 503 from the server. Tried 3 times." }) === "HTTP 503 from the server." && pure.historyReason({ state: "failed", error: "" }) === "No reason was recorded."
    && pure.historyReason({ state: "failed", error: "x".repeat(400) }).length === 158 && pure.historyReason({ state: "done", error: "old" }) === "");
  const m = pure.historyRowModel(H({ id: "h_m", state: "done", bytes_total: 2048, started_at: 100, finished_at: 3000, dest: "/d/a.zip", file_exists: false }), 4000 * 1000);
  check("a row model carries size, duration, destination, and 'missing' only when the hub said the file is gone",
    m.size === "2.0 KB" && m.took === "48 min 20 s" && m.dest === "a.zip" && m.missing === true && m.selectable === true
    && pure.historyRowModel(H({ state: "done", file_exists: null }), 4000 * 1000).missing === false && pure.historyRowModel(H({ state: "done" }), 4000 * 1000).missing === false
    && pure.historyRowModel(H({ owner: "provider" }), 4000 * 1000).selectable === false && /^failed /.test(pure.historyRowModel(H({ state: "failed", error: "x" }), 4000 * 1000).when));
  const sel5 = ["a", "b", "c", "d", "e"];
  check("Shift-click selects every id between two, in either direction; an unknown id selects just itself",
    eq(pure.selectionRange(sel5, "b", "d"), ["b", "c", "d"]) && eq(pure.selectionRange(sel5, "d", "b"), ["b", "c", "d"]) && eq(pure.selectionRange(sel5, "x", "d"), ["d"]));
  check("the bulk confirmations say how many, and that no file is deleted",
    /^Remove 3 from the history\?$/.test(pure.historyRemoveText(3).title) && /3 list entries/.test(pure.historyRemoveText(3).body) && /No downloaded file is deleted/.test(pure.historyRemoveText(3).body)
    && /1 list entry/.test(pure.historyRemoveText(1).body) && /Queue 2 downloads again/.test(pure.historyRedownloadText(2).body) && /no file is deleted/.test(pure.historyRedownloadText(2).body)
    && pure.HISTORY_REDOWNLOAD_MAX === 50);
  check("the retention sentence appears only when the hub reports a retention", pure.historyRetentionNote(0) === "" && pure.historyRetentionNote(null) === ""
    && /older than 90 days/.test(pure.historyRetentionNote(90)) && /never deleted/.test(pure.historyRetentionNote(90)) && /1 day\./.test(pure.historyRetentionNote(1)));
  check("the empty states: none yet, nothing matches, and none while rows exist or the list is still loading",
    pure.historyEmpty({ loaded: true, summary: { count: 0 }, state: "all" }).kind === "none" && pure.historyEmpty({ loaded: true, summary: { count: 0 }, state: "failed" }).kind === "filtered"
    && pure.historyEmpty({ loaded: true, summary: { count: 0 }, search: "x" }).kind === "filtered" && pure.historyEmpty({ loaded: true, summary: { count: 3 } }) === null
    && pure.historyEmpty({ loaded: false, summary: null }) === null && pure.historyEmpty({ loaded: true, error: "x", summary: null }) === null);
  check("a spreadsheet cell that starts like a formula is defused, quotes and commas are escaped",
    pure.csvCell("=HYPERLINK(1)") === "'=HYPERLINK(1)" && pure.csvCell("+1") === "'+1" && pure.csvCell("-2") === "'-2" && pure.csvCell("@a") === "'@a"
    && pure.csvCell('say "hi", ok') === '"say ""hi"", ok"' && pure.csvCell(null) === "" && pure.csvCell("plain") === "plain");
  const csv = pure.historyCsv([hrows[2]]).split("\r\n");
  check("the CSV has a header row and one row per entry", csv[0] === "Title,State,Source,Kind,Size (bytes),Finished,Error" && /^Bad file,failed,.*,HTTP 404$/.test(csv[1]) && csv[2] === "");
  const json = JSON.parse(pure.historyJson([hrows[1]]));
  check("the JSON export is an array of readable fields", json.length === 1 && json[0].Title === "New.mp4" && json[0].State === "done" && /^1970-01-01T00:50:00/.test(json[0].Finished));
  check("a History row's second line: source, size, when, and why it failed",
    /HTTP 404/.test(pure.historyMeta(hrows[2], 4000 * 1000)) && /failed /.test(pure.historyMeta(hrows[2], 4000 * 1000)) && /cancelled /.test(pure.historyMeta(hrows[4], 4000 * 1000)));

  // ---- Providers tab
  const P = (o) => Object.assign({ id: "demo", label: "Demo", enabled: true, mode: "push", rows: 2, live: 1, stale: 0, interval_s: null, last_error: null, last_report_age_s: 5, last_ok_age_s: null, controls: ["pause"], read_only: false }, o);
  check("provider tone: off, error, gone quiet, fine", pure.providerTone(P({ enabled: false })) === "off" && pure.providerTone(P({ last_error: "boom" })) === "bad"
    && pure.providerTone(P({ live: 2, stale: 2 })) === "quiet" && pure.providerTone(P({ live: 2, stale: 1 })) === "ok" && pure.providerTone(P()) === "ok");
  check("provider facts read in words and never claim control a read-only tool lacks",
    pure.providerFacts(P({ read_only: true, controls: [] })).some((f) => f[0] === "Can be controlled" && /No/.test(f[1]))
    && pure.providerFacts(P({ mode: "poll", interval_s: 10, last_ok_age_s: 600 })).some((f) => f[0] === "Last good check" && f[1] === "10 min ago")
    && pure.providerFacts(P({ last_report_age_s: null })).some((f) => f[1] === "none yet"));
  check("provider badge", pure.providerBadge(P({ enabled: false })) === "Off" && pure.providerBadge(P({ read_only: true })) === "Read-only" && pure.providerBadge(P({ mode: "poll" })) === "Polled" && pure.providerBadge(P()) === "Reports");

  // ---- the Sources tab (brief 24)
  const E = (o) => Object.assign({ id: "stremio", label: "Stremio (torrent)", state: "ready", reason: "", handles: "magnets, torrent files and Stremio streams", active: 1, queued: 0, checks: [], configure: "stremio", config_file: null }, o);
  check("every engine state has its own word and its own colour: Ready, Needs setup, Not running, Off, Broken",
    eq(["ready", "needs_setup", "not_running", "off", "broken"].map(pure.engineWord), ["Ready", "Needs setup", "Not running", "Off", "Broken"])
    && eq(["ready", "needs_setup", "not_running", "off", "broken"].map(pure.engineTone), ["done", "stalled", "stalled", "stale", "failed"])
    && pure.engineWord("nonsense") === "Unknown" && pure.engineTone("nonsense") === "stale");
  check("an engine's line: what it is for, then how many run and wait (an engine the table doesn't know shows only the counts)",
    pure.engineLine(E({ active: 1, queued: 0 })) === "magnets, torrent files and Stremio streams" + " \u00B7 1 active \u00B7 0 waiting"
    && pure.engineLine(E({ handles: "", active: 0, queued: 3 })) === "0 active \u00B7 3 waiting" && pure.engineLine(E({ active: null, queued: undefined })).endsWith("0 active \u00B7 0 waiting"));
  check("Configure: each engine lands on its own block under Sources (moved: HTTP was Connections, yt-dlp was Running at once); qBittorrent lands on its own block, a load error gets no button",
    pure.engineConfigure(E()).anchor === "stremio" && pure.engineConfigure(E({ configure: "http" })).anchor === "http"
    && pure.engineConfigure(E({ configure: "ytdlp" })).anchor === "yt-dlp" && pure.engineConfigure(E({ configure: "qbittorrent" })).anchor === "qbittorrent"
    && pure.engineConfigure(E({ configure: null })) === null
    && pure.engineConfigure(E({ configure: "something-new" })) === null && pure.engineConfigure(null) === null);
  check("without a Configure button the card says where it is set up (qBittorrent's file) or that there are no settings, and says nothing when there is a button",
    pure.engineSettingsNote(E({ configure: null, config_file: "~/.jarvis/qbittorrent.json" })) === "Set up in ~/.jarvis/qbittorrent.json."
    && pure.engineSettingsNote(E({ configure: null })) === "No settings." && pure.engineSettingsNote(E()) === "");
  const srcData = { engines: [E(), E({ id: "http", state: "needs_setup" }), E({ id: "ytdlp", state: "broken" }), E({ id: "qb", state: "not_running" }), E({ id: "x", state: "off" })],
    providers: [P({ id: "steam" }), P({ id: "gone", last_error: "boom" }), { id: "playnite", label: "Playnite", registered: false, hint: "Appears here after the first install Jarvis starts through Playnite." }] };
  check("the summary counts every state and counts only the registered programs (the Playnite hint is not one)",
    pure.sourcesSummary(srcData) === "5 engines \u00B7 1 ready \u00B7 1 needs setup \u00B7 1 not running \u00B7 1 off \u00B7 1 broken | 2 programs \u00B7 1 with an error"
    && pure.sourcesSummary(null).startsWith("reading") && pure.sourcesSummary({ engines: [], providers: [] }) === "0 engines | 0 programs");
  const srcBlock = source.slice(source.indexOf("// ---- Sources (brief 24)"), source.indexOf("$(\"#srcRefresh\").addEventListener"));
  const htmlS = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
  check("the tab is called Sources and the old Providers ids are gone from the page and the script",
    />Sources<span class="tab__n" id="srcCount">/.test(htmlS) && /data-tab="sources"/.test(htmlS) && /id="sourcesPane"/.test(htmlS)
    && !/providersPane|tabProviders|provList|provSummary|provCount|provRefresh|data-tab="providers"/.test(htmlS)
    && !/providersPane|tabProviders|loadProviders|renderProviders|provTimer|S\.providers/.test(source) && !/data-tab="providers"/.test(fs.readFileSync(path.join(ui, "tool.css"), "utf8")));
  check("two sections, in order: Download engines, then Programs that report their own downloads",
    /sourceHeading\("Download engines"/.test(srcBlock) && /sourceHeading\("Programs that report their own downloads"/.test(srcBlock)
    && srcBlock.indexOf('sourceHeading("Download engines"') < srcBlock.indexOf('sourceHeading("Programs that report their own downloads"'));
  check("Check now only reads: a plain GET of /api/hub/sources (no body, so no write header), on every button and the bar",
    /hubFetch\("\/api\/hub\/sources"\)/.test(srcBlock) && !/hubFetch\("\/api\/hub\/sources",/.test(source) && /id="srcRefresh"/.test(htmlS) && (srcBlock.match(/src-check/g) || []).length >= 2 && /class="btn btn--sm src-check" id="srcRefresh"/.test(htmlS)
    && /loadSources\(true\)/.test(srcBlock));
  check("an engine card shows the dot, the word, the reason, the line, Configure and Check now; a Broken one shows its reason in red",
    /engineWord\(e\.state\)/.test(srcBlock) && /"dot tone-" \+ tone/.test(srcBlock) && /e\.reason \?/.test(srcBlock) && /prov__reason" \+ \(e\.state === "broken" \? " is-bad"/.test(srcBlock)
    && /openSettings\(cfg\.anchor\)/.test(srcBlock) && /cfg \? el\("button"/.test(srcBlock));
  check("an empty provider shows its hint instead of nothing (Playnite), with no switch to flip",
    /p\.registered === false/.test(srcBlock) && /p\.hint \|\|/.test(srcBlock) && /Not seen yet/.test(srcBlock)
    && srcBlock.slice(srcBlock.indexOf("p.registered === false"), srcBlock.indexOf("const tone = providerTone(p);")).indexOf("dl_provider_prefs") === -1);
  check("the programs section keeps every control the Providers tab had: the switch, Settings, the facts, the last error, the gone-quiet marker",
    /dl_provider_prefs", \{ id: p\.id, enabled: !p\.enabled \}/.test(srcBlock) && /Turn off/.test(srcBlock) && /Settings\\u2026/.test(srcBlock) && /providerFacts\(p\)/.test(srcBlock)
    && /Last error: /.test(srcBlock) && /Not reporting/.test(srcBlock) && /providerBadge\(p\)/.test(srcBlock) && /r\.declined/.test(srcBlock));
  check("a program's Settings button lands on the Other tools' downloads block; the old click handlers still open Settings (the event is not an anchor)",
    /openSettings\(PROVIDER_SETTINGS_ANCHOR\)/.test(srcBlock) && pure.CONFIGURE_ANCHORS.stremio === "stremio" && /typeof anchor === "string" && \/\^\[a-z0-9-\]\{1,40\}\$\/\.test\(anchor\)/.test(source)
    && /on: \{ click: openSettings \}/.test(source) === false);
  check("everything an engine or program says is drawn with textContent (no HTML from the answer)", !/innerHTML|insertAdjacentHTML|outerHTML/.test(srcBlock));
  check("the Sources tab loads on opening, reloads every 10 s while it is visible, and never overlaps two loads",
    /loadSources\(false\);\s*\n\s*S\.srcTimer = setInterval\(\(\) => \{ if \(!doc\.hidden\) loadSources\(false\); \}, 10000\)/.test(source) && /if \(S\.srcBusy\) return;/.test(srcBlock));
  const dsSrc2 = fs.readFileSync(path.join(__dirname, "..", "web", "public", "downloads-settings.js"), "utf8");
  const stSrc2 = fs.readFileSync(path.join(__dirname, "..", "web", "public", "settings.js"), "utf8");
  check("Settings > Downloads blocks carry data-settings-anchor (every section, every source block, Other tools' downloads) and Settings scrolls to one on show()",
    /"data-settings-anchor": src\.anchor/.test(dsSrc2) && /"data-settings-anchor": "other-tools-downloads"/.test(dsSrc2) && /"data-settings-anchor": anchorOf\(s\.title\)/.test(dsSrc2)
    && /details\.scrollIntoView = function/.test(dsSrc2) && /if \(want\.anchor\) scrollToAnchor/.test(stSrc2) && /data-settings-anchor="' \+ anchor/.test(stSrc2) && /\^\[a-z0-9-\]\{1,40\}\$/.test(stSrc2));

  // ---- N15: why a row has no arrows or grip, and the order shown at once
  const R = (o) => Object.assign({ id: "a", owner: "hub", state: "queued", priority: 3 }, o);
  check("a waiting hub row with the live stream has arrows, so it needs no explanation", pure.reorderNote(R(), true) === "" && pure.canReorder(R(), true));
  check("the live stream down: it says moving needs the connection", /live connection/.test(pure.reorderNote(R(), false)) && /live connection/.test(pure.reorderNote(R({ priority: undefined }), true)));
  check("a running, probing or stalled hub row says it already started", ["downloading", "probing", "connecting", "postprocessing", "stalled"].every((st) => /Already started/.test(pure.reorderNote(R({ state: st }), true))));
  check("a paused row says to resume it first", /Resume it first/.test(pure.reorderNote(R({ state: "paused" }), true)));
  check("a row another program runs names that program", /Steam runs this one/.test(pure.reorderNote(R({ owner: "provider", provider_label: "Steam" }), true)) && /This program runs/.test(pure.reorderNote(R({ owner: "provider" }), true)));
  check("a finished row has nothing to explain", ["done", "failed", "cancelled"].every((st) => pure.reorderNote(R({ state: st }), true) === "") && pure.reorderNote(null, true) === "");
  const orderRows = [R({ id: "a", priority: 0, created_at: 1 }), R({ id: "b", priority: 0, created_at: 2 }), R({ id: "c", priority: 0, created_at: 3 }), R({ id: "x", owner: "provider", state: "downloading" })];
  const moved = pure.applyOrder(orderRows, ["c", "a", "b"]);
  check("applyOrder shows a new order at once: the same sum the hub does, rows outside it untouched, nothing mutated",
    eq(pure.queueOrder(moved), ["c", "a", "b"]) && moved[0].priority === 2 && moved[2].priority === 3 && moved[3] === orderRows[3] && orderRows[0].priority === 0 && eq(pure.queueOrder(orderRows), ["a", "b", "c"]));
  check("a refused order is put back, and a drop or an arrow shows the new order before the hub answers",
    /const before = S\.rows;\s*\n\s*S\.rows = applyOrder\(S\.rows, ids\);\s*\n\s*render\(\);/.test(source) && /catch \(err\) \{ S\.rows = before; render\(\); toast/.test(source));
  check("a slow poll that started before the stream came up never overwrites the stream's rows (N15's cause)", /if \(!streaming && !S\.live\) S\.rows = /.test(source));
  check("the card's tooltip and the toggle sit where N15 and N16 put them", /title: noReorder/.test(source) && /\[prim \? toggleButton\(row, prim, false\) : null\]\.concat\(buttons\)/.test(source)
    && /\.rbtn \{[^}]*min-height: 32px/.test(fs.readFileSync(path.join(ui, "tool.css"), "utf8")) && /\.detail__actions--top \.tbtn--lg, \.detail__actions--top \.btn--sm/.test(fs.readFileSync(path.join(ui, "tool.css"), "utf8")));

  // ---- the waiting queue: order, drag and arrows
  const Q = (id, pr, at, o) => Object.assign({ id: id, owner: "hub", state: "queued", priority: pr, created_at: at }, o || {});
  const qrows = [Q("a", 0, 1), Q("b", 0, 2), Q("c", 5, 3), Q("p", 0, 0, { owner: "provider" }), Q("r", 0, 0, { state: "downloading" })];
  check("the queue is listed in the order it will run: priority first, then oldest first, hub rows only", eq(pure.queueOrder(qrows), ["c", "a", "b"]));
  check("moving a row before / after another", eq(pure.moveInOrder(["a", "b", "c"], "c", "a", false), ["c", "a", "b"]) && eq(pure.moveInOrder(["a", "b", "c"], "a", "c", true), ["b", "c", "a"])
    && eq(pure.moveInOrder(["a", "b"], "a", "zzz", false), ["a", "b"]) && eq(pure.moveInOrder(["a", "b"], "zzz", "a", false), ["a", "b"]));
  const before = ["a", "b", "c"]; pure.moveInOrder(before, "c", "a", false); pure.stepInOrder(before, "b", -1);
  check("reordering never changes the list it was given", eq(before, ["a", "b", "c"]));
  check("one step up or down, and nothing at either end", eq(pure.stepInOrder(["a", "b", "c"], "b", -1), ["b", "a", "c"]) && eq(pure.stepInOrder(["a", "b", "c"], "b", 1), ["a", "c", "b"])
    && pure.stepInOrder(["a", "b"], "a", -1) === null && pure.stepInOrder(["a", "b"], "b", 1) === null && pure.stepInOrder(["a"], "x", 1) === null);
  check("only a queued hub row, with the live order known, can be dragged", pure.canReorder(Q("a", 0, 1), true) && !pure.canReorder(Q("a", 0, 1), false)
    && !pure.canReorder(Q("a", 0, 1, { state: "downloading" }), true) && !pure.canReorder(Q("a", 0, 1, { owner: "provider" }), true)
    && !pure.canReorder({ id: "a", owner: "hub", state: "queued" }, true));
  check("waiting rows are listed in run order when the stream gave their priorities, and newest-first otherwise",
    eq(pure.sortRows([Q("a", 0, 1), Q("b", 0, 2), Q("c", 5, 3)]).map((r) => r.id), ["c", "a", "b"])
    && eq(pure.sortRows([{ id: "a", owner: "hub", state: "queued", created_at: 1 }, { id: "b", owner: "hub", state: "queued", created_at: 2 }]).map((r) => r.id), ["b", "a"]));

  // ---- static wiring of the new tabs
  const htmlT = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
  check("the tab strip is a real tablist: three tabs, one selected, each controls a pane", /role="tablist"/.test(htmlT) && (htmlT.match(/role="tab"/g) || []).length === 3
    && (htmlT.match(/aria-selected="true"/g) || []).length === 1 && /role="tabpanel"/.test(htmlT) && /aria-controls="sourcesPane"/.test(htmlT) && /aria-controls="historyPane"/.test(htmlT));
  check("the arrow keys, Home and End move between tabs", /ArrowRight/.test(source) && /ArrowLeft/.test(source) && /"Home"/.test(source) && /"End"/.test(source));
  check("the writes to the hub's web routes carry the header the server requires, and removing history asks first",
    /"X-Jarvis-Hub": "1"/.test(source) && /askFirst\("Remove from the history\?"/.test(source) && /askFirst\("Clear old history\?"/.test(source) && /dry_run: true/.test(source));
  check("the panel only talks to /api/hub/ routes it knows", [...source.matchAll(/["'`]\/api\/([a-z\/\-]+)/g)].every((m) => /^hub\/(stream|list|sources|dismiss|history|history\/remove|history\/clear|redownload|reorder|thumb\/|file\/)/.test(m[1])));
  check("the live stream falls back to polling: errors, a silent stream and a `down` message all hand over", /es\.onerror = liveDown/.test(source) && /addEventListener\("down", liveDown\)/.test(source) && /45000/.test(source));
  check("while the stream is live the poll only refreshes the summary", /streaming \? \{ limit: 1, summary: true \}/.test(source));
  check("a row drag doesn't turn into 'links dropped on the panel'", /if \(S\.dragId\) return;/.test(source));
  check("drag has a keyboard route: every draggable row also has move up / move down buttons", /Move up in the queue/.test(source) && /Move down in the queue/.test(source));

  // ---- the chat card keeps the panel's looks (its formatting half is a copy: they must agree on every state)
  const card = loadBrowserScript("download-status-card.js").JarvisDownloadCard;
  check("the card script loads and exposes its pure half", !!(card && card._pure && typeof card._pure.barLook === "function"));
  const states = ["queued", "probing", "connecting", "downloading", "postprocessing", "paused", "stalled", "failed", "cancelled", "done", "weird"];
  const variants = [{}, { percent: 40 }, { percent: 40, speed_bps: 2097152, eta_s: 90 }, { percent: null }, { stale: true, age_s: 600, owner: "provider" },
    { retry_in_s: 12 }, { derived: ["percent", "speed", "eta"], percent: 10, speed_bps: 1024, eta_s: 5 }, { phase: "verifying", phase_index: 1, phase_count: 2 },
    { capabilities: ["pause", "resume", "cancel", "retry", "open", "reveal"], bytes_done: 5, bytes_total: 10 }, { owner: "provider", read_only: true, capabilities: [] }];
  let same = true;
  states.forEach((st) => variants.forEach((v) => {
    const r = row(Object.assign({ state: st }, v));
    if (!eq(card._pure.barLook(r), pure.barLook(r)) || card._pure.statusText(r) !== pure.statusText(r) || !eq(card._pure.rowActions(r), pure.rowActions(r))) same = false;
  }));
  check("the chat card draws every state exactly like the Downloads panel (bar look, words, buttons)", same);
  check("the card's formatters agree with the panel's", [0, 1023, 1536, 5 * 1024 * 1024].every((n) => card._pure.fmtBytes(n) === pure.fmtBytes(n))
    && [0, 45, 600, 3900, null].every((n) => card._pure.fmtEta(n) === pure.fmtEta(n) && card._pure.fmtAgo(n) === pure.fmtAgo(n)) && card._pure.fmtSpeed(2097152) === pure.fmtSpeed(2097152));
  check("a card offers pause / resume / retry / cancel while live and open / show in folder when done, nothing for a group or a read-only row",
    eq(card._pure.cardVerbs(row({ state: "downloading", capabilities: ["pause", "cancel", "open"] })).map((a) => a.verb), ["pause", "cancel"])
    && card._pure.cardVerbs(row({ state: "done", capabilities: ["open", "reveal"] })).map((a) => a.verb).includes("open")
    && card._pure.cardVerbs({ card: "group", state: "downloading" }).length === 0
    && card._pure.cardVerbs(row({ owner: "provider", state: "downloading", capabilities: [] })).length === 0);
  check("a payload is cleaned: bad ids dropped, at most eight cards, groups kept as groups",
    card._pure.normalizeCards({ cards: [{ id: "ok1" }, { id: "bad id" }, { id: 5 }, null, { id: "g1", card: "group" }].concat(Array.from({ length: 12 }, (_, i) => ({ id: "x" + i }))) }).length === 8
    && card._pure.normalizeCards({ cards: [{ id: "g1", card: "group" }] })[0].card === "group" && card._pure.normalizeCards(null).length === 0);
  check("a batch card reads in words", card._pure.groupStatus({ counts: { done: 2, total: 5, running: 1, failed: 1 }, final: false }) === "2 of 5 done \u00B7 1 running \u00B7 1 failed");
  const cardSrc = fs.readFileSync(path.join(__dirname, "..", "web", "public", "download-status-card.js"), "utf8");
  check("the card never assigns HTML and writes text with textContent", !/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/.test(cardSrc) && /textContent/.test(cardSrc));
  check("the card can run only the five download controls", /RUNNABLE = \{ dl_pause: 1, dl_resume: 1, dl_retry: 1, dl_cancel: 1, dl_open: 1 \}/.test(cardSrc));
  check("the card's thumbnail comes from the id-based route, never a URL from the data", /"\/api\/hub\/thumb\/" \+ encodeURIComponent\(row\.id\)/.test(cardSrc) && !/\.src = row\./.test(cardSrc));
  check("the card stops following when everything is final, the element left the page, or on too many streams", /allFinal\(\)\) stop\(\)/.test(cardSrc) && /isConnected/.test(cardSrc) && /MAX_STREAMS = 3/.test(cardSrc));

  // ---- Settings > Downloads (web/public/downloads-settings.js): which rows exist, how they are written
  const dsetWin = loadBrowserScript("downloads-settings.js");
  const dset = dsetWin.JarvisDownloadsSettings._pure;
  const payload = { settings: { rate_limit_kbps: 0, folders: { video: "Video" }, concurrency: { total: 4 }, notify: { enabled: true, min_seconds: 5 }, steam_threshold_mb: 500, weird_list: [1], retention: { keep_history_days: 0 }, schedule: [], ext_folders: {} },
    writable: ["rate_limit_kbps", "folders.video", "concurrency.total", "notify.enabled", "notify.done.enabled", "notify.done.level", "steam_threshold_mb", "weird_list", "schedule", "ext_folders", "library_root"],
    prefs_writable: ["rate_limit_kbps", "folders.video", "concurrency.total", "notify.enabled", "schedule", "ext_folders"] };
  const planned = dset.planRows(payload);
  const paths = planned.reduce((a, g) => a.concat(g.rows.map((r) => r.path)), []);
  check("only settings the hub says are writable get a row", paths.includes("rate_limit_kbps") && paths.includes("concurrency.total") && paths.includes("library_root")
    && !paths.includes("notify.min_seconds") && !paths.includes("http.max_retries") && !paths.includes("retention.keep_history_days"));
  check("a writable setting this page has never heard of still gets a generic row; a list or map it can't edit is left out, never shown wrongly",
    paths.includes("steam_threshold_mb") && !paths.includes("weird_list") && !paths.includes("schedule") && !paths.includes("ext_folders") && !paths.includes("notify.done.enabled"));
  check("rows are grouped under headings in a fixed order", eq(planned.map((g) => g.group), ["Where files go", "Speed", "Running at once", "Notifications", "Other"].filter((g) => planned.some((x) => x.group === g))));
  check("a key dl_prefs takes goes there; the rest through the confirm-gated dl_settings", dset.toolFor("rate_limit_kbps", payload) === "dl_prefs" && dset.toolFor("library_root", payload) === "dl_settings");
  check("private hosts: one per line or comma separated, blanks dropped", eq(dset.textToHosts("a.local\n b.lan:8080 , ,c"), ["a.local", "b.lan:8080", "c"]) && dset.hostsToText(["a", "b"]) === "a\nb");
  check("speed windows round-trip: day words stay words, numbers stay numbers",
    eq(dset.modelToWindows(dset.windowsToModel([{ days: ["mon", "tue", "wed", "thu", "fri"], from: "01:00", to: "07:00", rate_limit_kbps: 512 }])), [{ days: "weekdays", from: "01:00", to: "07:00", rate_limit_kbps: 512 }])
    && dset.daysChoice(["mon", "wed"]) === "custom" && dset.daysChoice(["sat", "sun"]) === "weekends");
  check("folder-per-type: a dot is added, empty rows are dropped, the table round-trips", eq(dset.modelToExt([{ ext: "iso", folder: "ISOs" }, { ext: ".pdf", folder: "Docs" }, { ext: "", folder: "x" }, { ext: "zip", folder: "" }]), { ".iso": "ISOs", ".pdf": "Docs" })
    && eq(dset.extToModel({ ".pdf": "Docs", ".iso": "ISOs" }), [{ ext: ".iso", folder: "ISOs" }, { ext: ".pdf", folder: "Docs" }]));
  const dsSrc = fs.readFileSync(path.join(__dirname, "..", "web", "public", "downloads-settings.js"), "utf8");
  check("the section writes only through dl_prefs / dl_settings / dl_provider_prefs and never assigns HTML", /RUNNABLE = \{ dl_prefs: 1, dl_settings: 1, dl_provider_prefs: 1 \}/.test(dsSrc) && !/innerHTML|insertAdjacentHTML|eval\(/.test(dsSrc));
  check("the section keeps no limit or range of its own beyond input hints (the hub validates)", !/Number\.isInteger|throw new Error\("[^"]*(must be|between)/.test(dsSrc));
  const settingsSrc = fs.readFileSync(path.join(__dirname, "..", "web", "public", "settings.js"), "utf8");
  check("settings.js takes extra sections from JarvisSettingsSections and gives them the row helpers", /JarvisSettingsSections/.test(settingsSrc) && /\.\.\.EXTRA_SECTIONS/.test(settingsSrc) && /ui: \{ h, group, rowShell, makeControl, flash, linkRow \}/.test(settingsSrc));
  const idxHtml = fs.readFileSync(path.join(__dirname, "..", "web", "public", "index.html"), "utf8");
  check("index.html loads the card and the section before settings.js",
    idxHtml.indexOf("download-status-card.js") > -1 && idxHtml.indexOf("downloads-settings.js") > -1 && idxHtml.indexOf("downloads-settings.js") < idxHtml.indexOf('src="settings.js"') && idxHtml.includes("download-status-card.css"));

  // ---- N1: Pause / Resume on every card, and why a row that can't be paused says so
  const hubCaps = { queued: ["pause", "cancel"], probing: ["pause", "cancel"], connecting: ["pause", "cancel"], downloading: ["pause", "cancel"], postprocessing: ["pause", "cancel"],
    paused: ["resume", "cancel"], stalled: ["retry", "pause", "cancel"], failed: ["retry"], cancelled: ["retry"], done: ["open", "reveal"] };
  const prim = (o) => { const p = pure.primaryAction(row(o)); return p ? p.verb : null; };
  check("every running state shows Pause as the toggle", ["queued", "probing", "connecting", "downloading", "postprocessing", "stalled"].every((st) => prim({ state: st, capabilities: hubCaps[st] }) === "pause"));
  check("a paused download shows Resume", prim({ state: "paused", capabilities: hubCaps.paused }) === "resume");
  check("a failed or cancelled download shows Retry in the same place", prim({ state: "failed", capabilities: hubCaps.failed }) === "retry" && prim({ state: "cancelled", capabilities: hubCaps.cancelled }) === "retry");
  check("a finished download has no toggle", prim({ state: "done", capabilities: hubCaps.done, dest_file: "x" }) === null);
  const pa = pure.primaryAction(row({ state: "downloading", capabilities: hubCaps.downloading }));
  check("the toggle carries an icon, a word, the tool, and a tooltip that names the Space shortcut", pa.icon.length > 0 && pa.label === "Pause" && pa.tool === "dl_pause" && /Space/.test(pa.title) && pa.doing === "Pausing\u2026");
  const ra = pure.primaryAction(row({ state: "paused", capabilities: hubCaps.paused }));
  check("Resume says Resuming while it works, and a tooltip with Space", ra.label === "Resume" && ra.tool === "dl_resume" && ra.doing === "Resuming\u2026" && /Space/.test(ra.title) && /again/.test(ra.title));
  const sa = pure.primaryAction(row({ state: "paused", capabilities: hubCaps.paused, scheduled_at: 1900000000 }));
  check("a scheduled row says Start now (and still runs dl_resume)", sa.label === "Start now" && sa.tool === "dl_resume" && sa.doing === "Starting\u2026");
  check("a stalled row keeps Retry and Cancel as the small buttons beside the Pause toggle", eq(pure.secondaryActions(row({ state: "stalled", capabilities: hubCaps.stalled })).map((a) => a.verb), ["retry", "cancel"]));
  check("a running row keeps Cancel as the only small button; Retry on a failed row is the toggle, not repeated", eq(pure.secondaryActions(row({ state: "downloading", capabilities: hubCaps.downloading })).map((a) => a.verb), ["cancel"]) && pure.secondaryActions(row({ state: "failed", capabilities: hubCaps.failed })).length === 0);
  check("no verb is invented: a row whose capabilities lack pause, resume and retry gets no toggle; one that declares only Resume gets Resume, never Pause",
    prim({ state: "downloading", capabilities: ["cancel"] }) === null && prim({ state: "downloading", capabilities: [] }) === null && prim({ state: "downloading", capabilities: ["reveal"] }) === null
    && prim({ state: "downloading", capabilities: ["resume", "cancel"] }) === "resume" && prim({ state: "stalled", capabilities: ["retry", "cancel"] }) === "retry");
  check("the toggle is the same set the chat card offers (it only reorders the panel's buttons)",
    ["queued", "downloading", "paused", "stalled", "failed", "done"].every((st) => { const r = row({ state: st, capabilities: hubCaps[st], dest_file: "x" }); return eq(card._pure.rowActions(r), pure.rowActions(r)); }));

  const steam = (o) => row(Object.assign({ owner: "provider", provider: "steam", state: "downloading", capabilities: ["reveal"], read_only: false }, o));
  check("Steam: no toggle, and the reason in words (on the card as well)", prim(steam({})) === null
    && pure.controlNote(steam({})).text === "Steam runs this download; pause it in Steam" && pure.controlNote(steam({})).card === true);
  check("Steam: the same on a queued or stalled row", pure.controlNote(steam({ state: "queued" })).text === "Steam runs this download; pause it in Steam" && !!pure.controlNote(steam({ state: "stalled", stale: false })));
  check("Steam: an already paused row says resume it in Steam, not pause", pure.controlNote(steam({ state: "paused" })).text === "Steam runs this download; resume it in Steam");
  check("Steam: a finished row has nothing to explain", pure.controlNote(steam({ state: "done" })) === null && pure.controlNote(steam({ state: "failed" })) === null);
  check("Steam: if the provider ever declares pause, the explanation goes and the toggle appears", pure.controlNote(steam({ capabilities: ["pause", "cancel"] })) === null && prim(steam({ capabilities: ["pause", "cancel"] })) === "pause");
  const pl = (o) => row(Object.assign({ owner: "provider", provider: "playnite", state: "unknown", capabilities: [], read_only: true }, o));
  check("Playnite: no controls, said in words on the card, whatever the state while it is not finished", ["unknown", "downloading", "installing", "queued"].every((st) => pure.controlNote(pl({ state: st })).text === "Playnite gives no progress or controls" && pure.controlNote(pl({ state: st })).card === true) && prim(pl({})) === null);
  const other = row({ owner: "provider", provider: "somewhere", state: "downloading", capabilities: [], read_only: true });
  check("any other read-only tool keeps today's text, in the detail pane only", pure.controlNote(other).text === "This tool reports its progress but can't be paused or cancelled from here." && pure.controlNote(other).card === false && pure.controlNote(other).title === "Read-only");
  check("a demo or other provider row that can be paused gets the toggle and no explanation", prim({ owner: "provider", provider: "demo", state: "downloading", capabilities: ["pause", "cancel"] }) === "pause"
    && pure.controlNote(row({ owner: "provider", provider: "demo", state: "downloading", capabilities: ["pause", "cancel"] })) === null);
  check("a hub-owned row never gets an explanation", pure.controlNote(row({ state: "downloading", capabilities: ["pause", "cancel"] })) === null && pure.controlNote(null) === null);

  check("what the hub is about to do reads in words", pure.pendingWord("pause") === "Pausing\u2026" && pure.pendingWord("cancel") === "Cancelling\u2026" && pure.pendingWord("resume") === "Resuming\u2026" && pure.pendingWord("") === "" && pure.pendingWord(null) === "" && pure.pendingWord("prioritize") === "Prioritize\u2026");

  // what a control tool's answer tells the person (incl. N2: "Show in folder" fell back to the folder)
  const nt = (r, a) => pure.controlNotice(r, a);
  check("a plain success says nothing", nt({ ok: true }, pa) === null && nt({ ok: true, pending: [] }, pa) === null);
  check("a queued request says what is happening", eq(nt({ ok: true, pending: ["h_1"] }, pa), { msg: "Pausing\u2026", level: "info" }) && eq(nt({ ok: true, pending: ["h_1"] }, { label: "Cancel" }), { msg: "Cancel\u2026", level: "info" }));
  check("a declined confirmation says Cancelled", eq(nt({ ok: false, declined: true, error: "x" }, pa), { msg: "Cancelled", level: "info" }));
  check("an error shows the hub's own words as a warning", eq(nt({ ok: false, error: "No such download" }, pa), { msg: "No such download", level: "warn" }) && nt({ ok: false }, pa).msg === "That didn't work");
  check("N2: Show in folder that fell back to the folder shows the hub's note as a warning", eq(nt({ ok: true, note: "The file is gone, so its folder was opened instead" }, { label: "Show in folder" }), { msg: "The file is gone, so its folder was opened instead", level: "warn" }));
  check("N2: a note wins over nothing but not over an error", nt({ ok: false, error: "E", note: "n" }, pa).msg === "E");

  // wiring on the source: the panel draws the toggle from these helpers and shows the notice
    check("the card's button row holds the toggle, first (not a hover-only control; N16 moved it down from the top row)", /prim \? toggleButton\(row, prim, false\) : null/.test(source) && !/\.tbtn[^{]*\{[^}]*(opacity: 0|display: none|visibility: hidden)/.test(css) && !/:hover[^{]*\.tbtn\s*\{[^}]*display/.test(css));
  check("the toggle is at least 32px tall", /\.tbtn \{[^}]*min-height: 32px/.test(css));
  check("the detail pane puts the controls before the bar and Open / Show in folder after them", source.indexOf("detail__actions detail__actions--top") > -1 && source.indexOf("detail__actions detail__actions--top") < source.indexOf("const bar = renderBar(row, true)")
    && /const ctrl = actions\.filter/.test(source) && /const files = actions\.filter/.test(source));
  check("a paused row has a pause glyph and a dimmed bar", /tone === "paused"\) meta\.push\(el\("span", \{ cls: "pglyph"/.test(source) && /\.bar-track\.paused \{ opacity/.test(css));
  check("the card shows the explanation line only where controlNote says so", /why && why\.card \? el\("div", \{ cls: "row__why"/.test(source) && /\.row__why \{/.test(css));
  check("runAction shows the notice, and the open-folder button shows it too", /const n = controlNotice\(r, Object\.assign\(\{\}, action, \{ doing: doing \}\)\);\s*if \(n\) toast\(n\.msg, n\.level\);/.test(source) && /controlNotice\(await callTool\("dl_open", \{ id: row\.id, action: "folder" \}\)\)/.test(source));
  check("a pressed Pause / Resume / Retry says what is happening at once, and it is cleared on failure or when the state moves on", /S\.acting\[row\.id\] = \{ from: row\.state, doing: doing/.test(source) && /if \(r\.declined \|\| r\.ok === false\) delete S\.acting\[row\.id\]/.test(source) && /row\.state !== a\.from/.test(source) && /S\.acting\]\)\) return;|S\.acting\]\)/.test(source));
  check("the pending word shows on the toggle too (the hub's own request), and the button is disabled while busy", /busy \|\| prim\.label/.test(source) && /attrs\.disabled = "disabled"/.test(source) && /busyWord\(row\) \{[^}]*pendingWord\(row\.pending\)/.test(source.replace(/\n/g, " ")));
  check("the toggle is built with el() and textContent only (no HTML from data)", !/innerHTML|insertAdjacentHTML/.test(source.slice(source.indexOf("function toggleButton"), source.indexOf("function renderBar"))));

  // ---- brief 19: Dismiss, Clear finished, Show hidden
  const prow = (o) => Object.assign({ id: "steam:1", owner: "provider", provider: "steam", state: "done", derived: [], capabilities: ["reveal"], stale: false, dismissed_at: null }, o);
  check("a provider row can be dismissed when it is unknown or in a final state, and in no other",
    ["done", "failed", "cancelled", "unknown"].every((st) => pure.canDismiss(prow({ state: st })))
    && ["queued", "probing", "connecting", "downloading", "postprocessing", "paused", "stalled"].every((st) => !pure.canDismiss(prow({ state: st }))));
  check("a hub row is dismissable once it failed or was cancelled, never while running or when finished (History holds those)",
    pure.canDismiss(row({ state: "failed", owner: "hub" })) && pure.canDismiss(row({ state: "cancelled", owner: "hub" }))
    && !pure.canDismiss(row({ state: "done" })) && !pure.canDismiss(row({ state: "downloading", owner: "hub" })) && !pure.canDismiss(row({ state: "paused", owner: "hub" })));
check("Clear finished takes cancelled hub rows but leaves failed ones (they may want a Retry)",
    pure.dismissableRows([row({ id: "c", state: "cancelled", owner: "hub" }), row({ id: "f", state: "failed", owner: "hub" })]).map((r) => r.id).join() === "c");
  check("the row's own state decides: no capability named dismiss is read or invented",
    pure.canDismiss(prow({ capabilities: [] })) && !/\"dismiss\"/.test(source.slice(source.indexOf("const ACTION_DEFS"), source.indexOf("const ACTION_ORDER"))) && pure.rowActions(prow({ capabilities: ["reveal"] })).every((a) => a.verb !== "dismiss"));
  check("junk is not dismissable", !pure.canDismiss(null) && !pure.canDismiss({}) && !pure.canDismiss(prow({ state: undefined })));
  const hid = prow({ id: "steam:2", dismissed_at: 1700000000 });
  check("a hidden row is dimmed (and offers Show again) until it is listed again; it can't be dismissed twice",
    pure.isHidden(hid) && pure.isDimmed(hid, {}) && !pure.canDismiss(hid, {}) && !pure.isDimmed(hid, { "steam:2": true }) && pure.canDismiss(hid, { "steam:2": true }) && !pure.isHidden(prow({})));
  const live = [prow({ id: "steam:1" }), prow({ id: "steam:3", state: "downloading" }), row({ id: "h_1", state: "done" })];
  check("Clear finished counts the shown dismissable provider rows only (not running, not hub, not hidden)",
    eq(pure.dismissableRows(live.concat([hid]), {}).map((r) => r.id), ["steam:1"]));
  const hidden = [hid, prow({ id: "steam:1", dismissed_at: 5 }), prow({ id: "steam:9" })];
  check("Show hidden off lists only the live rows", eq(pure.mergeHidden(live, hidden, false, {}).map((r) => r.id), ["steam:1", "steam:3", "h_1"]));
  check("Show hidden on adds the hidden rows (a row the hub shows again wins; a row without dismissed_at is not added)",
    eq(pure.mergeHidden(live, hidden, true, {}).map((r) => r.id), ["steam:1", "steam:3", "h_1", "steam:2"]));
  check("a row listed again stays in the list with Show hidden off", eq(pure.mergeHidden(live, hidden, false, { "steam:2": true }).map((r) => r.id), ["steam:1", "steam:3", "h_1", "steam:2"]));
  check("mergeHidden copes with nothing", eq(pure.mergeHidden([], null, true, null), []) && eq(pure.mergeHidden(live, undefined, false, undefined), live));
  check("a hidden row still counts in its status view when listed (the Finished view finds it)", pure.viewMatches("done", hid) && eq(pure.filterRows(pure.mergeHidden(live, hidden, true, {}), { view: "done" }).map((r) => r.id), ["steam:1", "h_1", "steam:2"]));
  check("what the person is told: one row, several, none, already hidden",
    pure.dismissNotice({ ok: true, dismissed: ["a"], count: 1 }).msg === "Hidden. No file was touched."
    && pure.dismissNotice({ ok: true, dismissed: ["a", "b", "c"], count: 3 }).msg === "Hid 3 rows. No file was touched."
    && pure.dismissNotice({ ok: true, dismissed: [], count: 0, note: "nothing to clear" }).msg === "Nothing to clear"
    && pure.dismissNotice({ ok: true, dismissed: [], count: 0, note: "it was already hidden" }).msg === "It was already hidden"
    && pure.dismissNotice(null).msg === "Nothing to hide");
  check("the tooltips say that files are never touched, and Clear finished says hub rows and History are not touched",
    /no file is touched/i.test(pure.DISMISS_TIP) && /no file is touched/i.test(pure.CLEAR_TIP) && /hub made itself/.test(pure.CLEAR_TIP) && /History/.test(pure.CLEAR_TIP)
    && /Nothing hidden is deleted/.test(pure.SHOW_HIDDEN_TIP) && /hub keeps it hidden/.test(pure.SHOW_AGAIN_TIP));
  const htmlD = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
  check("tool.html has Clear finished in the top strip and the Show hidden toggle (aria-pressed) in the list bar",
    /id="clearFinished"[^>]*>Clear finished</.test(htmlD) && /id="showHidden"[^>]*aria-pressed="false"[^>]*>Show hidden</.test(htmlD)
    && htmlD.indexOf('id="clearFinished"') < htmlD.indexOf("</header>") && htmlD.indexOf('id="showHidden"') > htmlD.indexOf('id="groupBy"')
    && /no file is touched/.test(htmlD.slice(htmlD.indexOf('id="clearFinished"'), htmlD.indexOf('id="clearFinished"') + 400)));
  check("the wiring: Dismiss posts {id}, Clear finished posts {finished: true}, Show hidden reads ?include_dismissed=1",
    /hubFetch\("\/api\/hub\/dismiss", \{ id: row\.id \}\)/.test(source) && /hubFetch\("\/api\/hub\/dismiss", \{ finished: true \}\)/.test(source)
    && /hubFetch\("\/api\/hub\/list\?include_dismissed=1"\)/.test(source)
    && /\$\("#clearFinished"\)\.addEventListener\("click", clearFinished\)/.test(source) && /\$\("#showHidden"\)\.addEventListener\("click", toggleShowHidden\)/.test(source));
  const dismissSrc = source.slice(source.indexOf("async function dismissRow"), source.indexOf("async function demoAction"));
  check("there is no confirmation dialog on Dismiss or Clear finished, and no tool is run for them",
    !/askFirst|confirm\(/.test(dismissSrc) && !/callTool|runTool/.test(dismissSrc) && !/askFirst/.test(source.slice(source.indexOf("function dismissButton"), source.indexOf("function sectionHeader"))));
  check("Show again calls nothing: it only marks the row as listed again", /function showAgain\(row\) \{\s*S\.revealed\[row\.id\] = true;\s*render\(\);\s*\}/.test(source));
  check("the Dismiss button is drawn from canDismiss / isDimmed on the card and in the detail pane",
    /const hide = dismissButton\(row, false\);\s*if \(hide\) buttons\.push\(hide\)/.test(source) && /const hideBtn = dismissButton\(row, true\);/.test(source)
    && /if \(!dimmed && !canDismiss\(row, S\.revealed\)\) return null;/.test(source));
  check("a hidden row is dimmed in the CSS and tagged in words too; the button is built with el() and textContent",
    /\.row\.is-hidden \{ opacity/.test(css) && /text: "hidden"/.test(source) && !/innerHTML|insertAdjacentHTML/.test(source.slice(source.indexOf("function dismissButton"), source.indexOf("function sectionHeader"))));
  check("the hidden rows are asked for only while they are in view, and the poll refreshes them",
    /if \(!S\.showHidden && !Object\.keys\(S\.revealed\)\.length\) return;/.test(source) && /loadHidden\(false\);\s*\/\/ only does anything while hidden rows are in view/.test(source));
  check("the live rows (counts, needs attention, queue order) are not computed from the hidden ones",
    /overviewStats\(S\.rows\)/.test(source) && /needsAttention\(S\.rows\)/.test(source) && /queueOrder\(S\.rows\)/.test(source) && /const counts = viewCounts\(viewRows\(\)\)/.test(source));
  check("a collapsed group says what is inside it (counts by state), and a single download reads singular",
    pure.sectionSummary([{ state: "downloading" }, { state: "failed" }, { state: "failed" }, { state: "done" }]) === "4 downloads \u00B7 1 running \u00B7 2 failed \u00B7 1 done"
    && pure.sectionSummary([{ state: "paused" }]) === "1 download \u00B7 1 paused" && pure.sectionSummary(null) === "0 downloads");
  check("groups collapse from a chevron button and the title, remember it (prefs.collapsed), and arrow keys skip collapsed rows",
    /function toggleSection\(key\)/.test(source) && /"aria-expanded": open \? "true" : "false"/.test(source) && /collapsed: Object\.keys\(S\.collapsed\)/.test(source)
    && /p\.collapsed\.slice\(0, 200\)/.test(source) && /shownRows\(rows\)\.map\(\(r\) => r\.id\)/.test(source)
    && /id="collapseAll"/.test(fs.readFileSync(path.join(ui, "tool.html"), "utf8")) && /\.section__toggle \{/.test(css));
  check("the list's signature includes what Show hidden and Show again change", /\[rows, S\.selected, S\.groupBy, S\.live, S\.revealed, S\.showHidden, S\.collapsed, S\.acting\]/.test(source));

  // ---- brief 19 step 4: a running Steam row's note on the card
  const steamNote = "30.6 % of 2.1 GB, reported 20 s ago; Steam isn't reporting live progress";
  check("a running Steam row shows its own note on the card; nothing for other rows or states, and nothing is invented",
    pure.cardNote(prow({ state: "downloading", message: steamNote })) === steamNote && pure.cardNote(prow({ state: "downloading", message: "" })) === ""
    && pure.cardNote(prow({ state: "downloading", message: null })) === "" && pure.cardNote(prow({ state: "done", message: steamNote })) === ""
    && pure.cardNote(prow({ state: "downloading", provider: "playnite", message: steamNote })) === "" && pure.cardNote(row({ state: "downloading", message: steamNote })) === ""
    && pure.cardNote(null) === "" && /cardNote\(row\) \? el\("div", \{ cls: "row__note"/.test(source) && /\.row__note \{/.test(css));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
