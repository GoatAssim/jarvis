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
  check("counts per view", eq(pure.viewCounts(rows), { all: 8, active: 1, waiting: 3, failed: 2, done: 1, stale: 1 }));
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
  const css = fs.readFileSync(path.join(ui, "tool.css"), "utf8");
  check("the stylesheet defines a look for every bar kind", ["determinate", "indeterminate", "striped", "queued", "done", "failed", "stalled", "paused", "stale", "cancelled", "unknown"]
    .every((k) => k === "determinate" || css.includes(".bar-track." + k) || css.includes("." + k)));
  check("motion respects prefers-reduced-motion", css.includes("prefers-reduced-motion"));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
