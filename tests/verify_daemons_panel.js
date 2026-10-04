// Verifies web/public/daemons.js's pure helpers (JarvisDaemons._pure) — the
// parts of the H.1.5 Daemons rework that have real logic and no DOM: status
// bucketing, uptime/relative-time formatting, the "what needs attention"
// rules, search/filter matching, console-line classification and grouping,
// highlight segmentation, and the editor's draft <-> request-body mapping
// and validation.
//
// This repo has no JS test framework or runner (every other test in tests/
// is Python, run directly with `python3 tests/test_X.py`) — this script
// follows the same plain-assert, run-it-directly convention as
// verify_math_rendering.js and verify_ask_trace_replay.js instead of
// introducing new test tooling for one file.
//
// The panel is loaded with Node's vm module into a bare `window` object, the
// same technique tests/test_checklist_supplied.py's node harness uses for
// test-checklist.js's _mergeCatalogue. Nothing under test touches the DOM —
// that's the point of keeping these functions pure — so no jsdom is needed.
//
//   node tests/verify_daemons_panel.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PANEL_JS = path.join(__dirname, "..", "web", "public", "daemons.js");
const win = {};
vm.runInNewContext(fs.readFileSync(PANEL_JS, "utf8"), { window: win, console });
const P = win.JarvisDaemons && win.JarvisDaemons._pure;
assert.ok(P, "window.JarvisDaemons._pure did not load");

let n = 0;
function ok(name, cond) {
  n += 1;
  if (!cond) throw new Error(`FAILED: ${name}`);
  console.log(`ok       ${name}`);
}

// --- normalizeId -------------------------------------------------------------
// Mirrors daemons.normalize_id() (jarvis-cli/jarvis/daemons.py): lower-case,
// spaces to dashes, only [a-z0-9_-], 40 chars.
ok("normalizeId lower-cases", P.normalizeId("Web") === "web");
ok("normalizeId turns spaces into dashes", P.normalizeId("my service") === "my-service");
ok("normalizeId strips disallowed characters", P.normalizeId("a!b@c#1") === "abc1");
ok("normalizeId truncates to 40 chars", P.normalizeId("a".repeat(50)).length === 40);
ok("normalizeId of empty/whitespace is empty", P.normalizeId("   ") === "");

// --- bucketOf / isStoppable ---------------------------------------------------
ok("bucketOf: running wins even if status disagrees", P.bucketOf({ running: true, status: "crashed" }) === "running");
ok("bucketOf: starting", P.bucketOf({ status: "starting" }) === "starting");
ok("bucketOf: restarting maps to the starting bucket", P.bucketOf({ status: "restarting" }) === "starting");
ok("bucketOf: crashed", P.bucketOf({ status: "crashed" }) === "crashed");
ok("bucketOf: scheduled", P.bucketOf({ status: "scheduled" }) === "scheduled");
ok("bucketOf: unknown status reads as stopped, not invented", P.bucketOf({ status: "???" }) === "stopped");
ok("bucketOf: null entry reads as stopped", P.bucketOf(null) === "stopped");

// H.1.3's own fix, pinned down at the panel level too (the backend regression
// lives in tests/test_h12_daemon_fixes.py) — a daemon mid-start/backoff has a
// live supervisor but isn't `running` yet, and must still read as stoppable.
ok("isStoppable: running is stoppable", P.isStoppable({ running: true }));
ok("isStoppable: starting with a live supervisor_pid is stoppable", P.isStoppable({ running: false, supervisor_pid: 4242 }));
ok("isStoppable: truly stopped is not stoppable", !P.isStoppable({ running: false, supervisor_pid: null }));

// --- fmtDuration / uptimeSeconds / fmtStamp / describeNextStart --------------
ok("fmtDuration: seconds", P.fmtDuration(45) === "45s");
ok("fmtDuration: minutes+seconds", P.fmtDuration(125) === "2m 05s");
ok("fmtDuration: hours+minutes", P.fmtDuration(3 * 3600 + 61) === "3h 01m");
ok("fmtDuration: days+hours", P.fmtDuration(2 * 86400 + 3600) === "2d 1h");
ok("fmtDuration: negative clamps to 0s", P.fmtDuration(-5) === "0s");

ok("uptimeSeconds: not running is null", P.uptimeSeconds({ running: false, started_at: 1000 }, 5000000) === null);
ok("uptimeSeconds: no started_at is null", P.uptimeSeconds({ running: true, started_at: null }, 5000000) === null);
{
  const started = 1_000_000; // epoch seconds
  const now = (started + 90) * 1000; // 90s later, in ms
  const secs = P.uptimeSeconds({ running: true, started_at: started }, now);
  ok("uptimeSeconds: computes elapsed time correctly", Math.abs(secs - 90) < 0.01);
}

{
  const now = Date.UTC(2026, 8, 28, 12, 0, 0); // 2026-09-28 12:00:00 UTC
  const soon = new Date(now + 3600 * 1000).toISOString();
  const nx = P.describeNextStart(soon, now);
  ok("describeNextStart: future time reads 'in ...'", nx.relative.startsWith("in "));
  const past = new Date(now - 60 * 1000).toISOString();
  ok("describeNextStart: past/due time reads 'due now'", P.describeNextStart(past, now).relative === "due now");
  ok("describeNextStart: empty input is null", P.describeNextStart("", now) === null);
  ok("describeNextStart: unparsable text falls back to the raw string", P.describeNextStart("whenever", now).label === "whenever");
}

// --- restartSummary / stopSummary ---------------------------------------------
ok("restartSummary: never", P.restartSummary({ restart: "never" }) === "Never restarts");
ok("restartSummary: on-failure with a limit", /On failure/.test(P.restartSummary({ restart: "on-failure", restart_delay: 5, max_restarts: 5 })));
ok("restartSummary: max_restarts 0 reads as no limit", /no limit/.test(P.restartSummary({ restart: "always", restart_delay: 2, max_restarts: 0 })));
ok("stopSummary: names the signal and the timeout", P.stopSummary({ stop_signal: "KILL", stop_timeout: 3 }) === "KILL, force-kill after 3s");

// --- attentionOf ---------------------------------------------------------------
ok("attentionOf: a healthy running daemon has nothing to report", P.attentionOf({ status: "running", running: true, restarts: 0 }).length === 0);
{
  const a = P.attentionOf({ status: "crashed", last_error: "exited with code 1", restarts: 0 });
  ok("attentionOf: crashed produces exactly one item", a.length === 1);
  ok("attentionOf: crashed item carries the real error text", a[0].note.includes("exited with code 1"));
  ok("attentionOf: crashed is severity 'bad'", a[0].level === "bad");
}
{
  const a = P.attentionOf({ status: "restarting", restarts: 3, max_restarts: 5 });
  ok("attentionOf: under the limit is a warning, not bad", a.length === 1 && a[0].level === "warn" && a[0].kind === "looping");
}
{
  const a = P.attentionOf({ status: "restarting", restarts: 5, max_restarts: 5 });
  ok("attentionOf: at the limit escalates to bad", a.some((x) => x.kind === "limit" && x.level === "bad"));
}
{
  // max_restarts defaults to 5 (daemons.py's DEFAULT_MAX_RESTARTS) when the
  // field is genuinely absent, not 0 — 0 has its own meaning ("no limit").
  const a = P.attentionOf({ status: "restarting", restarts: 5, max_restarts: undefined });
  ok("attentionOf: an absent max_restarts falls back to the real default, not 0", a.some((x) => x.kind === "limit"));
}
{
  const a = P.attentionOf({ status: "restarting", restarts: 3, max_restarts: 0 });
  ok("attentionOf: max_restarts 0 (no limit) never reports 'limit reached'", !a.some((x) => x.kind === "limit"));
}

// --- searchHaystack / matchesFilters -------------------------------------------
{
  const e = { id: "web", name: "Web server", description: "serves the UI", command: "node server.js", builtin: false };
  ok("matchesFilters: matches on name", P.matchesFilters(e, { search: "web server" }));
  ok("matchesFilters: matches on command", P.matchesFilters(e, { search: "server.js" }));
  ok("matchesFilters: every search term must match (AND)", !P.matchesFilters(e, { search: "web nonexistentterm" }));
  ok("matchesFilters: kind=builtin excludes a custom daemon", !P.matchesFilters(e, { kind: "builtin" }));
  ok("matchesFilters: kind=custom includes a custom daemon", P.matchesFilters(e, { kind: "custom" }));
}
{
  const running = { id: "a", status: "running", running: true };
  const crashed = { id: "b", status: "crashed" };
  ok("matchesFilters: state filter matches its own bucket", P.matchesFilters(running, { state: "running" }));
  ok("matchesFilters: state filter excludes a different bucket", !P.matchesFilters(crashed, { state: "running" }));
  ok("matchesFilters: state=attention matches only entries with something to report", P.matchesFilters(crashed, { state: "attention" }) && !P.matchesFilters(running, { state: "attention" }));
}

// --- countEntries ---------------------------------------------------------------
{
  const entries = [
    { id: "a", status: "running", running: true, builtin: true },
    { id: "b", status: "crashed", builtin: true },
    { id: "c", status: "stopped", builtin: false },
  ];
  const c = P.countEntries(entries);
  ok("countEntries: total", c.total === 3);
  ok("countEntries: by state", c.byState.running === 1 && c.byState.crashed === 1 && c.byState.stopped === 1);
  ok("countEntries: attention counts the crashed one", c.attention === 1);
  ok("countEntries: by kind splits built-in vs custom", c.kinds.builtin.total === 2 && c.kinds.custom.total === 1);
}

// --- console: classifyLine / annotateLines / summarizeLines / filterLines -----
ok("classifyLine: plain output is stdout", P.classifyLine("hello").cls === "stdout");
ok("classifyLine: E: prefix is stderr", P.classifyLine("E: oops").cls === "stderr");
ok("classifyLine: E: strips its own prefix from the text", P.classifyLine("E: oops").text === "oops");
ok("classifyLine: a stamped meta line", P.classifyLine("[2026-09-28 12:00:00] === starting: x").cls === "meta");
ok("classifyLine: a stamped stdin echo", P.classifyLine("[2026-09-28 12:00:00] <<< hello").cls === "stdin");
ok("classifyLine: E: + a traceback head is 'crash', not plain stderr", P.classifyLine("E: Traceback (most recent call last):").cls === "crash");
ok("classifyLine: E: + a traceback frame is 'crash'", P.classifyLine('E:   File "x.py", line 1').cls === "crash");
ok("classifyLine: E: + an *Error line is 'crash'", P.classifyLine("E: ValueError: bad").cls === "crash");
ok("classifyLine: a nonzero exit banner is flagged bad", P.classifyLine("[2026-09-28 12:00:00] === exited with code 1 after 0.5s").bad === true);
ok("classifyLine: a clean exit banner is NOT flagged bad", P.classifyLine("[2026-09-28 12:00:00] === exited with code 0 after 0.5s").bad === false);

{
  const items = P.annotateLines(["a", "E: b", "c"]);
  ok("annotateLines: numbers lines from 1", items[0].n === 1 && items[2].n === 3);
  const s = P.summarizeLines(items);
  ok("summarizeLines: counts by bucket", s.all === 3 && s.output === 2 && s.errors === 1);
  const errOnly = P.filterLines(items, "errors", "", false);
  ok("filterLines: the errors filter keeps only stderr/crash", errOnly.length === 1 && errOnly[0].cls === "stderr");
  const searched = P.filterLines(items, "all", "b", true);
  ok("filterLines: onlyMatches with a query narrows to matching lines", searched.length === 1 && searched[0].text === "b");
}

{
  const items = P.annotateLines(["before", "E: Traceback (most recent call last):", 'E:   File "x.py", line 3', "E: ValueError: bad", "after"]);
  const blocks = P.groupTraces(items);
  ok("groupTraces: three consecutive crash lines collapse into one block", blocks.length === 3);
  ok("groupTraces: the trace block holds all three crash lines", blocks[1].type === "trace" && blocks[1].items.length === 3);
  ok("groupTraces: a non-crash line stays its own block", blocks[0].type === "line" && blocks[2].type === "line");
}

// A real, messy traceback: a source-context line under the "File" frame that
// matches no per-line shape at all, and an exception class name (Discord.py's
// own LoginFailure) that doesn't end in Error/Exception/Warning — both used
// to fall OUT of the crash grouping and render as plain, uncolored stderr.
{
  const raw = [
    "[2026-09-28 09:00:00] === starting: python -m jarvis.discord_daemon ===",
    "E: Traceback (most recent call last):",
    'E:   File "discord_daemon.py", line 40, in <module>',
    "E:     client.run(token)",
    "E: discord.errors.LoginFailure: Improper token has been passed.",
    "[2026-09-28 09:00:01] === exited with code 1 after 1.1s ===",
  ];
  const items = P.annotateLines(raw);
  ok("annotateLines: the starting banner is meta, not swept into the trace", items[0].cls === "meta");
  ok("annotateLines: the head line is crash", items[1].cls === "crash");
  ok("annotateLines: the File frame line is crash", items[2].cls === "crash");
  ok("annotateLines: a source-context line with no recognizable shape is still swept in as crash", items[3].cls === "crash");
  ok("annotateLines: a non-standard exception name (no Error/Exception/Warning) is still crash", items[4].cls === "crash");
  ok("annotateLines: the exit banner after the trace is NOT swept in", items[5].cls === "meta");
  const blocks = P.groupTraces(items);
  ok("groupTraces: the whole real traceback collapses into exactly one block", blocks.length === 3 && blocks[1].type === "trace" && blocks[1].items.length === 4);
}

// Ordinary stderr logging that follows a completed traceback must NOT be
// swept in forever — the block has to actually close.
{
  const raw = [
    "E: Traceback (most recent call last):",
    "E: ValueError: bad",
    "E: an unrelated later warning, logged well after the crash",
  ];
  const items = P.annotateLines(raw);
  ok("annotateLines: a traceback with zero frame lines still closes after its exception line", items[1].cls === "crash");
  ok("annotateLines: stderr logged after the block closed is NOT swept into it", items[2].cls === "stderr");
}

// --- highlightSegments -----------------------------------------------------------
{
  const segs = P.highlightSegments("hello world hello", "hello");
  ok("highlightSegments: finds every case-insensitive occurrence", segs.filter((s) => s.hit).length === 2);
  ok("highlightSegments: text is preserved in order", segs.map((s) => s.text).join("") === "hello world hello");
  const none = P.highlightSegments("plain text", "");
  ok("highlightSegments: an empty query highlights nothing", none.length === 1 && !none[0].hit);
  const ci = P.highlightSegments("Hello World", "WORLD");
  ok("highlightSegments: matching is case-insensitive", ci.some((s) => s.hit && s.text === "World"));
}

// --- editor: draftFromEntry / validateDraft / envPairs / payload builders ------
{
  const entry = { id: "web", name: "web", command: "node x.js", env: { A: "1" }, restart: "on-failure", restart_delay: 5, max_restarts: 5, stop_signal: "TERM", stop_timeout: 10 };
  const d = P.draftFromEntry(entry);
  ok("draftFromEntry: a name equal to the id shows as blank in the form", d.name === "");
  ok("draftFromEntry: env becomes an editable row list", d.env.length === 1 && d.env[0].k === "A" && d.env[0].v === "1");
}
{
  const blank = P.blankDraft();
  const v = P.validateDraft(blank, { mode: "add", builtin: false, existingIds: new Set() });
  ok("validateDraft: a blank add-mode draft needs an id and a command", v.errors.id && v.errors.command);
  ok("validateDraft: id comes before command in the error order", v.order.indexOf("id") < v.order.indexOf("command"));

  const good = Object.assign({}, blank, { id: "web", command: "node x.js" });
  const v2 = P.validateDraft(good, { mode: "add", builtin: false, existingIds: new Set() });
  ok("validateDraft: a filled-in draft with valid defaults is ok", v2.ok);

  const dup = P.validateDraft(good, { mode: "add", builtin: false, existingIds: new Set(["web"]) });
  ok("validateDraft: a duplicate id is rejected", !dup.ok && /already exists/.test(dup.errors.id));

  const editMode = P.validateDraft(Object.assign({}, blank, { command: "" }), { mode: "edit", builtin: false, existingIds: new Set() });
  ok("validateDraft: edit mode doesn't require an id field at all", !editMode.errors.id);
  ok("validateDraft: edit mode still requires a command for a non-built-in", editMode.errors.command);

  const builtinEdit = P.validateDraft(Object.assign({}, blank, { command: "" }), { mode: "edit", builtin: true, existingIds: new Set() });
  ok("validateDraft: a built-in never needs a command (it's locked, not editable)", !builtinEdit.errors.command);

  const badNum = Object.assign({}, good, { restartDelay: "0", maxRestarts: "-1", stopTimeout: "abc" });
  const v3 = P.validateDraft(badNum, { mode: "add", builtin: false, existingIds: new Set() });
  ok("validateDraft: restartDelay must be >= 1", Boolean(v3.errors.restartDelay));
  ok("validateDraft: maxRestarts rejects a negative number", Boolean(v3.errors.maxRestarts));
  ok("validateDraft: a non-numeric stopTimeout is rejected", Boolean(v3.errors.stopTimeout));

  const dupEnv = Object.assign({}, good, { env: [{ k: "A", v: "1" }, { k: "A", v: "2" }] });
  const v4 = P.validateDraft(dupEnv, { mode: "add", builtin: false, existingIds: new Set() });
  ok("validateDraft: the same env key twice is rejected", Boolean(v4.errors["env.1"]));

  const blankEnvRow = Object.assign({}, good, { env: [{ k: "", v: "" }] });
  const v5 = P.validateDraft(blankEnvRow, { mode: "add", builtin: false, existingIds: new Set() });
  ok("validateDraft: a wholly untouched blank env row is not an error", v5.ok);

  const halfEnvRow = Object.assign({}, good, { env: [{ k: "", v: "set" }] });
  const v6 = P.validateDraft(halfEnvRow, { mode: "add", builtin: false, existingIds: new Set() });
  ok("validateDraft: a value with no name IS an error", Boolean(v6.errors["env.0"]));
}
ok("envPairs: drops rows with no name, keeps the rest as K=V", JSON.stringify(P.envPairs([{ k: "A", v: "1" }, { k: "", v: "x" }, { k: "B", v: "" }])) === JSON.stringify(["A=1", "B="]));

{
  const draft = Object.assign({}, P.blankDraft(), { id: "web", command: "node x.js", env: [{ k: "PORT", v: "3000" }] });
  const body = P.buildAddPayload(draft);
  ok("buildAddPayload: normalizes the id", body.id === "web");
  ok("buildAddPayload: passes the command through", body.command === "node x.js");
  ok("buildAddPayload: env becomes K=V strings", JSON.stringify(body.env) === JSON.stringify(["PORT=3000"]));
  ok("buildAddPayload: an untouched optional field (name) is left out entirely", !("name" in body));
}
{
  const initial = P.draftFromEntry({ id: "web", name: "web", command: "node x.js", env: {}, restart: "never", restart_delay: 5, max_restarts: 5, stop_signal: "TERM", stop_timeout: 10 });
  const unchanged = P.buildEditPayload(initial, initial, { builtin: false });
  ok("buildEditPayload: nothing changed means an empty body", Object.keys(unchanged).length === 0);

  const changed = Object.assign({}, initial, { command: "node y.js", restartDelay: "9" });
  const body = P.buildEditPayload(changed, initial, { builtin: false });
  ok("buildEditPayload: only the fields that actually changed are sent", Object.keys(body).sort().join(",") === "command,restartDelay");
  ok("buildEditPayload: restartDelay is sent as a string for the CLI flag", body.restartDelay === "9");

  const builtinChanged = Object.assign({}, initial, { command: "rm -rf /", name: "renamed" });
  const builtinBody = P.buildEditPayload(builtinChanged, initial, { builtin: true });
  ok("buildEditPayload: a built-in's command is never sent, even if the draft has one", !("command" in builtinBody));
  ok("buildEditPayload: a built-in's other fields still go through", builtinBody.name === "renamed");

  const envChanged = Object.assign({}, initial, { env: [{ k: "A", v: "1" }] });
  const envBody = P.buildEditPayload(envChanged, initial, { builtin: false });
  ok("buildEditPayload: a changed env sets envReplace", envBody.envReplace === true);
  ok("buildEditPayload: a changed env carries the full new set", JSON.stringify(envBody.env) === JSON.stringify(["A=1"]));
}

// --- buildReport -------------------------------------------------------------------
{
  const entries = [
    { id: "web", status: "running", running: true, started_at: 1000, restarts: 0 },
    { id: "worker", status: "crashed", last_error: "boom", restarts: 2, max_restarts: 5 },
  ];
  const report = P.buildReport(entries, (1000 + 30) * 1000);
  ok("buildReport: is a markdown table with a header row", report.includes("| Service | State |"));
  ok("buildReport: lists every service by id", report.includes("web") && report.includes("worker"));
  ok("buildReport: surfaces the crashed one's error under Needs attention", /Needs attention[\s\S]*worker[\s\S]*boom/.test(report));
}

// --- favorites (L.13) -------------------------------------------------------------
{
  const entries = [
    { id: "web", status: "running", running: true, builtin: true },
    { id: "bot", status: "crashed", last_error: "boom", restarts: 1 },
    { id: "spotify", status: "stopped" },
    { id: "cron", status: "stopped" },
  ];
  const favs = new Set(["bot", "spotify", "gone"]); // "gone" has no entry any more
  const f = (extra) => entries.filter((e) => P.matchesFilters(e, Object.assign({ favorites: favs }, extra))).map((e) => e.id);

  ok("matchesFilters: favOnly keeps only starred services", JSON.stringify(f({ favOnly: true })) === JSON.stringify(["bot", "spotify"]));
  ok("matchesFilters: favOnly off ignores the favorites set", f({ favOnly: false }).length === 4);
  ok("matchesFilters: favOnly with no favorites set matches nothing", entries.filter((e) => P.matchesFilters(e, { favOnly: true })).length === 0);
  ok("matchesFilters: favorites AND a state filter", JSON.stringify(f({ favOnly: true, state: "crashed" })) === JSON.stringify(["bot"]));
  ok("matchesFilters: favorites AND search", JSON.stringify(f({ favOnly: true, search: "spot" })) === JSON.stringify(["spotify"]));
  ok("matchesFilters: favorites AND kind", f({ favOnly: true, kind: "builtin" }).length === 0);
  ok("matchesFilters: old filter shape (no favOnly) is unchanged", entries.every((e) => P.matchesFilters(e, { search: "", state: "all", kind: "all" })));

  ok("countEntries: counts only favorites that still exist", P.countEntries(entries, favs).favorites === 2);
  ok("countEntries: no favorites argument counts zero", P.countEntries(entries).favorites === 0);
  ok("countEntries: totals are unchanged by passing favorites", P.countEntries(entries, favs).total === 4);

  const sorted = P.sortFavoritesFirst(entries, favs).map((e) => e.id);
  ok("sortFavoritesFirst: starred first, each half keeps its order", JSON.stringify(sorted) === JSON.stringify(["bot", "spotify", "web", "cron"]));
  ok("sortFavoritesFirst: nothing starred returns the same order", JSON.stringify(P.sortFavoritesFirst(entries, new Set()).map((e) => e.id)) === JSON.stringify(entries.map((e) => e.id)));
  ok("sortFavoritesFirst: does not mutate its input", entries[0].id === "web");

  const on = P.toggleFavoriteId(["a"], "b");
  ok("toggleFavoriteId: adds an id that was not starred", on.now === true && JSON.stringify(on.ids) === JSON.stringify(["a", "b"]));
  const off = P.toggleFavoriteId(["a", "b"], "a");
  ok("toggleFavoriteId: removes one that was", off.now === false && JSON.stringify(off.ids) === JSON.stringify(["b"]));
  ok("toggleFavoriteId: tolerates a missing list", JSON.stringify(P.toggleFavoriteId(undefined, "x").ids) === JSON.stringify(["x"]));
}

console.log(`\n${n}/${n} checks passed`);
