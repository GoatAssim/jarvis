// Verifies web/public/app.js's askLineFromStoredEntry() — the mapping from
// a console_store {kind, text, tool} line (Part E's persistence layer,
// "ask" surface) to the {text, cls} shape the Ask panel's trace already
// uses for a live-recorded run (see askPromptLine / askTraceByConv). This
// is the K.2.5.1 fix: without it, a genuine reload, a second tab, or a
// different browser showed an empty "Commands Jarvis runs will show up
// here live." placeholder for any conversation whose ask activity happened
// in a different session, even though the data was already durably saved.
//
// This repo has no JS test framework (see verify_math_rendering.js's own
// header for why) — same plain-assert, run-it-directly convention:
//   node tests/verify_ask_trace_replay.js
// No npm packages needed — askLineFromStoredEntry is a pure function with
// no dependency on marked, the DOM, or anything else in app.js.

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "web", "public", "app.js");
const src = fs.readFileSync(APP_JS, "utf8");

// Pull just askLineFromStoredEntry out of app.js by source range, so this
// test runs against the actual shipped code rather than a copy that could
// drift out of sync with it.
const startMarker = 'function askLineFromStoredEntry(line) {';
const endMarker = "async function loadAskTraceForConv(";
const start = src.indexOf(startMarker);
const end = src.indexOf(endMarker);
if (start === -1 || end === -1) {
  console.error("Could not find askLineFromStoredEntry in app.js — did the surrounding code move?");
  process.exit(1);
}
// eslint-disable-next-line no-eval
eval(src.slice(start, end));

// The saved-as-shown helpers (also pure): the two console views, and the merge of
// "what the page drew" with "what the server derived" for one turn's cards.
const mergeStart = src.indexOf("function mergeDisplayWithExtras(display, extras) {");
const mergeEnd = src.indexOf("// The thread items one finished turn drew, in plain JSON");
if (mergeStart === -1 || mergeEnd === -1) {
  console.error("Could not find mergeDisplayWithExtras in app.js — did the surrounding code move?");
  process.exit(1);
}
// eslint-disable-next-line no-eval
eval(src.slice(mergeStart, mergeEnd));

let pass = 0;
let fail = 0;

function check(name, cond, detail) {
  if (cond) {
    pass += 1;
    console.log("ok      " + name);
  } else {
    fail += 1;
    console.log("FAILED  " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : ""));
  }
}

// The exact kinds ai_client.py's console_store.log()/end_turn() calls
// actually write for the "ask" surface (see ai_client.py's own
// console_store.log/begin_turn/end_turn call sites).

let out = askLineFromStoredEntry({ kind: "command", text: "hello there", turn: "t1" });
check("command -> \"$ jarvis\", cls cmd (matches askPromptBegin's own live line exactly)",
      out && out.text === "$ jarvis" && out.cls === "cmd", out);

out = askLineFromStoredEntry({ kind: "provider", text: "openai (key 1/2)", turn: "t1" });
check("provider -> sys, mentions the key label", out && out.cls === "sys" && out.text.includes("openai (key 1/2)"), out);

out = askLineFromStoredEntry({ kind: "tool-call", text: '{"path": "notes.txt"}', tool: "read_file", turn: "t1" });
check("tool-call -> tool, uses the tool name (not the raw JSON args)",
      out && out.cls === "tool" && out.text.includes("read_file") && !out.text.includes("notes.txt"), out);

out = askLineFromStoredEntry({ kind: "tool-result", text: '{"ok": true, "content": "hi"}', tool: "read_file", turn: "t1" });
check("tool-result -> tool, names the tool", out && out.cls === "tool" && out.text.includes("read_file"), out);

out = askLineFromStoredEntry({ kind: "error", text: '{"ok": false, "error": "not found"}', tool: "read_file", turn: "t1" });
check("error with a tool -> fail, names the tool", out && out.cls === "fail" && out.text.includes("read_file"), out);

out = askLineFromStoredEntry({ kind: "error", text: "openai: rate limited", turn: "t1" });
check("error with no tool (a provider/turn-level failure) -> fail, plain text",
      out && out.cls === "fail" && out.text.includes("rate limited"), out);

out = askLineFromStoredEntry({ kind: "status", text: "answered", turn: "t1" });
check('status "answered" -> done (the only success text end_turn() ever writes)',
      out && out.cls === "done", out);

for (const text of ["no provider answered", "budget exhausted \u2014 reporting what ran", "interrupted (process ended)"]) {
  out = askLineFromStoredEntry({ kind: "status", text, turn: "t1" });
  check(`status "${text}" -> fail (a non-success ending)`, out && out.cls === "fail", out);
}

out = askLineFromStoredEntry({ kind: "narration", text: "Thinking about it...", turn: "t1" });
check("narration -> null (that's the reply/interim text itself, shown elsewhere, not this trace)",
      out === null, out);

out = askLineFromStoredEntry({ kind: "some-future-kind", text: "whatever", turn: "t1" });
check("an unrecognized kind -> null, not a guess (forward-compatible: a new kind is invisible here until mapped, never garbled)",
      out === null, out);

// Long tool arguments/results must be truncated, not dumped whole into a
// one-line trace panel.
const longResult = JSON.stringify({ content: "x".repeat(500) });
out = askLineFromStoredEntry({ kind: "tool-result", text: longResult, tool: "read_file", turn: "t1" });
check("a long tool-result is truncated, not shown in full", out && out.text.length < longResult.length, out && out.text.length);

// ---------------------------------------------------------------------------
// buildAskTraceViews: ONE saved history, two views -- Raw (the lines exactly as
// painted) and Summary (the described events). Lines are in seq order.
// ---------------------------------------------------------------------------
const SEP = "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500";
const NOTE = "(summary only \u2014 the raw console wasn't saved for this turn)";
const ev = (seq, kind, turn, text, extra) => Object.assign({ seq, kind, turn, text, surface: "ask" }, extra || {});

// The real console this was asked for from: raw lines carry the [tools] discovery,
// the "asking groq" line and the token lines that no summary line covers.
let stored = [
  ev(1, "command", "t1", "hi"),
  ev(2, "provider", "t1", "groq (key 1/4)"),
  ev(3, "trace", "b1", "$ jarvis", { cls: "cmd" }),
  ev(4, "trace", "b1", "[tools] Auto-discovered ['calendar_add_event'] from calendar_tools.py (group='calendar')", { cls: "sys" }),
  ev(5, "trace", "b1", "\u21b3 asking groq (key 1/4)\u2026", { cls: "sys" }),
  ev(6, "trace", "b1", "tokens: in=2235 out=1109 total=3344 rounds=1 tools=0", { cls: "tool" }),
  ev(7, "status", "t1", "answered"),
  ev(8, "trace", "b1", "done", { cls: "done" }),
];
let views = buildAskTraceViews(stored);
check("raw view is exactly the painted lines, text and colour class, in order",
      JSON.stringify(views.raw) === JSON.stringify([
        { text: "$ jarvis", cls: "cmd" },
        { text: "[tools] Auto-discovered ['calendar_add_event'] from calendar_tools.py (group='calendar')", cls: "sys" },
        { text: "\u21b3 asking groq (key 1/4)\u2026", cls: "sys" },
        { text: "tokens: in=2235 out=1109 total=3344 rounds=1 tools=0", cls: "tool" },
        { text: "done", cls: "done" },
      ]), views.raw);
check("raw view does NOT also carry the summary lines of a turn it covers",
      !views.raw.some((l) => l.text.startsWith("$ trying") || l.text === "answered"), views.raw);
check("summary view is the old described lines, and none of the raw ones",
      JSON.stringify(views.summary.map((l) => l.text)) === JSON.stringify(["$ jarvis", "$ trying  groq (key 1/4)", "answered"]), views.summary);

// A turn from before raw lines existed: raw view still shows it, as its summary, with a note.
stored = [
  ev(1, "command", "t0", "old"), ev(2, "provider", "t0", "groq"), ev(3, "status", "t0", "answered"),
  ev(4, "command", "t1", "new"),
  ev(5, "trace", "b1", "$ jarvis", { cls: "cmd" }), ev(6, "trace", "b1", "raw line", { cls: "sys" }),
  ev(7, "status", "t1", "answered"),
];
views = buildAskTraceViews(stored);
check("a turn with no raw lines is still in the raw view, as its summary, behind a note",
      views.raw[0].text === NOTE && views.raw[1].text === "$ jarvis" && views.raw[2].text === "$ trying  groq", views.raw.slice(0, 4));
check("turns are separated, and the covered turn shows only its raw lines",
      views.raw[4].text === SEP && views.raw[5].text === "$ jarvis" && views.raw[6].text === "raw line" && views.raw.length === 7, views.raw);
check("summary view still has both turns, separated",
      views.summary.filter((l) => l.text === SEP).length === 1, views.summary);

// Two raw turns are separated by their own turn ids.
stored = [
  ev(1, "command", "t1", "a"), ev(2, "trace", "b1", "one", { cls: "sys" }), ev(3, "status", "t1", "answered"),
  ev(4, "command", "t2", "b"), ev(5, "trace", "b2", "two", { cls: "sys" }), ev(6, "status", "t2", "answered"),
];
views = buildAskTraceViews(stored);
check("two raw turns -> their lines with one separator between",
      JSON.stringify(views.raw.map((l) => l.text)) === JSON.stringify(["one", SEP, "two"]), views.raw);

// Nothing saved at all / junk input never throws.
check("no stored lines -> two empty views", (() => { const v = buildAskTraceViews([]); return v.raw.length === 0 && v.summary.length === 0; })());
check("a non-array input -> two empty views, no throw", (() => { const v = buildAskTraceViews(null); return v.raw.length === 0 && v.summary.length === 0; })());
check("a trace line with no cls is painted as a plain sys line",
      buildAskTraceViews([ev(1, "trace", "b1", "bare")]).raw[0].cls === "sys");
check("only trace lines saved (no described events at all) -> raw view still works",
      buildAskTraceViews([ev(1, "trace", "b1", "x", { cls: "tool" })]).raw.length === 1);

// ---------------------------------------------------------------------------
// mergeDisplayWithExtras: what the page DREW wins for the card types it draws;
// the server's derived cards fill in the rest. Nothing is duplicated.
// ---------------------------------------------------------------------------
let merged = mergeDisplayWithExtras(
  [{ type: "screenshot", data: { filename: "drawn.png" } }, { type: "uiBubble", data: { title: "t" } }],
  [{ type: "thinking", data: { text: "hm" } }, { type: "screenshot", data: { filename: "derived.png" } }]);
check("the drawn card replaces the derived one of the same type, in the derived one's place",
      merged.map((m) => m.type).join() === "thinking,screenshot,uiBubble" && merged[1].data.filename === "drawn.png", merged);
check("a derived-only type (thinking) is kept; a drawn-only type (uiBubble) is added",
      merged.some((m) => m.type === "thinking") && merged.some((m) => m.type === "uiBubble"), merged);
merged = mergeDisplayWithExtras(
  [{ type: "devAgent", data: { jobId: "a" } }, { type: "devAgent", data: { jobId: "b" } }],
  [{ type: "devAgent", data: { jobId: "old" } }]);
check("several drawn cards of one type all survive, the derived one does not",
      merged.length === 2 && merged.every((m) => m.data.jobId !== "old"), merged);
check("no display at all -> the derived extras, unchanged",
      JSON.stringify(mergeDisplayWithExtras([], [{ type: "thinking", data: { a: 1 } }])) === JSON.stringify([{ type: "thinking", data: { a: 1 } }]));
check("junk entries are skipped, not thrown on",
      mergeDisplayWithExtras([null, {}, { type: "x" }], [undefined, { data: 1 }]).length === 1);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
