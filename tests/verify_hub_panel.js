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
  check("the level menu uses the notifier's own names", pure.levelMenu()[0].value === "default" && pure.levelMenu().length === 6 && /Persistent/.test(pure.levelMenu()[3].label));
  check("every notification type is listed, in order", eq(pure.NOTIFY_EVENTS.map((e) => e.id), ["done", "failed", "group_done", "queue_empty", "stalled", "started"]));

  check("days: presets and a single day round-trip", pure.daysChoice(pure.DAYS) === "all" && pure.daysChoice(["mon", "tue", "wed", "thu", "fri"]) === "weekdays" && pure.daysChoice(["sat", "sun"]) === "weekends" && pure.daysChoice(["wed"]) === "wed" && pure.daysChoice(["mon", "wed"]) === "custom");
  check("days: a custom set is kept when nothing is chosen", eq(pure.daysFromChoice("custom", ["mon", "wed"]), ["mon", "wed"]) && eq(pure.daysFromChoice("weekdays"), ["mon", "tue", "wed", "thu", "fri"]) && eq(pure.daysFromChoice("fri"), ["fri"]));
  check("a new time window starts overnight, unlimited", eq(pure.blankWindow(), { days: pure.DAYS, from: "01:00", to: "07:00", rate_limit_kbps: 0 }));

  const hubSettings = {
    rate_limit_kbps: 0, schedule: [], after_download: { open: "off" }, concurrency: { total: 4, http: 2 }, http: { stall_seconds: 60, max_retries: 4 },
    folders: { video: "Video", audio: "Audio", file: "Files" },
    notify: { enabled: true, min_seconds: 0, coalesce_s: 8, providers: true, started: { enabled: false, level: null }, done: { enabled: true, level: null }, failed: { enabled: true, level: null }, stalled: { enabled: false, level: null }, group_done: { enabled: true, level: null }, queue_empty: { enabled: true, level: null } },
  };
  const base = pure.prefsFromSettings(hubSettings);
  const copy = () => JSON.parse(JSON.stringify(base));
  check("the form model reads the hub's settings (an unset level stays unset)", base.notify.events.done.enabled === true && base.notify.events.done.level === null && base.notify.events.started.enabled === false && base.after_open === "off" && base.folders.file === "Files");
  check("nothing edited, nothing to save", eq(pure.prefsDiff(base, copy()), {}));
  const edited = copy();
  edited.rate_limit_kbps = 2048; edited.after_open = "reveal"; edited.notify.events.done.enabled = false; edited.notify.events.failed.level = 4; edited.notify.events.stalled.level = null;
  edited.notify.coalesce_s = 0; edited.folders.video = "Movies"; edited.concurrency.total = 6; edited.http.max_retries = 0; edited.schedule = [pure.blankWindow()];
  const diff = pure.prefsDiff(base, edited);
  check("only what changed is sent, under the keys dl_prefs takes", eq(Object.keys(diff).sort(), ["after_download.open", "concurrency.total", "folders.video", "http.max_retries", "notify.coalesce_s", "notify.done.enabled", "notify.failed.level", "rate_limit_kbps", "schedule"]));
  check("values keep their types (a level is a number, 0 retries is 0)", diff["notify.failed.level"] === 4 && diff["http.max_retries"] === 0 && diff.rate_limit_kbps === 2048 && diff["notify.done.enabled"] === false);
  const back = copy(); back.notify.events.done.level = 3; const base3 = pure.prefsFromSettings(Object.assign({}, hubSettings, { notify: Object.assign({}, hubSettings.notify, { done: { enabled: true, level: 3 } }) }));
  check("going back to the notifier's default is sent as 'default'", pure.prefsDiff(base3, copy()).hasOwnProperty("notify.done.level") && pure.prefsDiff(base3, copy())["notify.done.level"] === "default" && pure.prefsDiff(base, back)["notify.done.level"] === 3);
  check("the overview summarises notifications", pure.notifySummary(base) === "4 of 6 notification types on" && pure.notifySummary(Object.assign(copy(), { notify: Object.assign(copy().notify, { enabled: false }) })) === "Notifications are off");

  check("the add form: no extras -> no extra arguments", eq(pure.addOptionsFromForm({ limit: "", unit: "kb", open: "default", notify: "default" }), { args: {} }));
  check("the add form: extras become dl_add arguments", eq(pure.addOptionsFromForm({ limit: "2", unit: "mb", open: "reveal", notify: "on" }), { args: { speed_limit_kbps: 2048, open_when_done: "reveal", notify: "on" } }));
  check("the add form: a bad speed is an error", !!pure.addOptionsFromForm({ limit: "x", unit: "kb", open: "default", notify: "default" }).error);
  check("one download's own choices in words", pure.itemPrefsText({}) === "" && pure.itemPrefsText({ limit_kbps: 300, open: "reveal", notify: "off" }) === "limit 300 KB/s \u00B7 then: show the file in its folder \u00B7 never notify");
  check("the add form: channels and events become dl_add arguments", eq(pure.addOptionsFromForm({ limit: "", unit: "kb", open: "default", notify: "default", channels: ["discord"], events: ["done", "failed"] }), { args: { notify_channels: ["discord"], notify_events: ["done", "failed"] } }));
  check("the add form: nothing ticked sends no notification choices", eq(pure.addOptionsFromForm({ limit: "", unit: "kb", open: "default", notify: "default", channels: [], events: [] }), { args: {} }));
  check("a download's own channels, events and folder in words", pure.itemPrefsText({ notify_channels: ["discord"], notify_events: ["done"], subdir: "Y" }) === "notify via Discord \u00B7 notify when it finishes \u00B7 saves in Y");
  check("a download's own full path in words", pure.itemPrefsText({ dest_abs: "D:\\Games" }) === "saves in D:\\Games");
  check("cleanExt", pure.cleanExt("ISO") === ".iso" && pure.cleanExt(".tar.gz") === ".tar.gz" && pure.cleanExt("a/b") === "" && pure.cleanExt("") === "");
  check("isFullPath", pure.isFullPath("D:\\x") && pure.isFullPath("/mnt/x") && pure.isFullPath("~/x") && !pure.isFullPath("Movies/Kids") && !pure.isFullPath(""));
  check("extFolderRows sorts by extension", eq(pure.extFolderRows({ ".pdf": "Docs", ".iso": "ISOs" }), [[".iso", "ISOs"], [".pdf", "Docs"]]));
  {
    const base = pure.prefsFromSettings({ ext_folders: { ".iso": "ISOs" } });
    const cur = pure.prefsFromSettings({ ext_folders: { ".iso": "ISOs" } });
    check("file-type folders unchanged -> no update", eq(pure.prefsDiff(base, cur), {}));
    cur.ext_folders[".pdf"] = "Docs";
    check("a new file-type folder sends the whole map", eq(pure.prefsDiff(base, cur), { ext_folders: { ".iso": "ISOs", ".pdf": "Docs" } }));
    delete cur.ext_folders[".iso"];
    delete cur.ext_folders[".pdf"];
    check("removing the last one sends an empty map", eq(pure.prefsDiff(base, cur), { ext_folders: {} }));
  }
  {
    const html3 = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
    check("the add form has the per-download notification pickers", html3.includes('id="addChannels"') && html3.includes('id="addEvents"'));
  }
  check("the test result names the channels that delivered", pure.testResultText({ ok: true, delivered: ["inbox", "desktop"], failed_channels: [] }) === "Test sent: inbox, desktop." && /Couldn't reach: voice/.test(pure.testResultText({ ok: true, delivered: ["inbox"], failed_channels: ["voice"] })) && pure.testResultText({ ok: false, error: "nope" }) === "nope");

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
  check("source filter: hub engine / provider", eq(f({ source: "provider:demo" }), ["e"]) && f({ source: "hub:http" }).length === 7);
  check("the sources list is labelled and counted", eq(pure.sourceList(rows).map((s) => [s.label, s.count]), [["Demo provider", 1], ["HTTP", 7]]));
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
  const savedCh = pure.NOTIFY_CHANNELS.map((c) => Object.assign({}, c));
  const savedLv = Object.assign({}, pure.LEVEL_NAMES);
  check("a channel and a level the notifier gains are offered with no change here",
    pure.applyNotifyOptions({ channels: [{ id: "discord", label: "Discord" }, { id: "telegram", label: "Telegram" }], levels: [{ value: 1, name: "silent" }, { value: 6, name: "panic" }] })
    && pure.NOTIFY_CHANNELS.some((c) => c.id === "telegram") && pure.levelMenu().some((o) => o.value === "6" && /Panic/.test(o.label)) && !pure.levelMenu().some((o) => o.value === "3"));
  check("an empty or missing answer changes nothing", !pure.applyNotifyOptions(null) && !pure.applyNotifyOptions({ channels: [], levels: [] }));
  pure.NOTIFY_CHANNELS.length = 0; savedCh.forEach((c) => pure.NOTIFY_CHANNELS.push(c));
  Object.keys(pure.LEVEL_NAMES).forEach((k) => delete pure.LEVEL_NAMES[k]); Object.assign(pure.LEVEL_NAMES, savedLv);
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
  check("the settings screen and its buttons exist in tool.html", ["settings", "settingsNav", "settingsPage", "settingsSave", "settingsRevert", "settingsBack", "speedQuick", "openFolder", "settingsBtn", "detailPrefs", "addLimit", "addOpen", "addNotify"].every((id) => html2.includes('id="' + id + '"')));
  check("the panel can open the Settings screen with Escape and close it", /Escape/.test(source) && /closeSettings/.test(source));
  check("the settings are saved with dl_prefs; only the library folder and a full-path file-type folder go through the confirm-gated dl_settings", /callTool\("dl_prefs"/.test(source) && /callTool\("dl_settings", \{ set: \{ library_root/.test(source) && /callTool\("dl_settings", \{ set: \{ ext_folders/.test(source) && (source.match(/callTool\("dl_settings"/g) || []).length === 2);
  check("an event's target isn't read after an await (it is cleared inside a shadow root)", !/await[^\n]*\n[^\n]*e\.target/.test(source.slice(source.indexOf("speedQuick\").addEventListener"), source.indexOf("speedQuick\").addEventListener") + 1200)));
  const css = fs.readFileSync(path.join(ui, "tool.css"), "utf8");
  check("the stylesheet defines a look for every bar kind", ["determinate", "indeterminate", "striped", "queued", "done", "failed", "stalled", "paused", "stale", "cancelled", "unknown"]
    .every((k) => k === "determinate" || css.includes(".bar-track." + k) || css.includes("." + k)));
  check("motion respects prefers-reduced-motion", css.includes("prefers-reduced-motion"));
  check("the stylesheet styles the settings screen", [".settings", ".snav", ".ntable", ".choice", ".prefsbox", ".speedq"].every((c) => css.includes(c)));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
