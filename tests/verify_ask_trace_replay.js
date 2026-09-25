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
const endMarker = "async function loadAskTraceForConv(convId) {";
const start = src.indexOf(startMarker);
const end = src.indexOf(endMarker);
if (start === -1 || end === -1) {
  console.error("Could not find askLineFromStoredEntry in app.js — did the surrounding code move?");
  process.exit(1);
}
// eslint-disable-next-line no-eval
eval(src.slice(start, end));

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

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
