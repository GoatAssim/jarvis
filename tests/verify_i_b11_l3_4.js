// Verifies two small front-end pieces (pure helpers only; the DOM wiring is
// checked by hand -- see the checklist notes in the master plan):
//
//   I-B11  JarvisRich.speechText(): what Speak reads of a reply. Fenced code
//          becomes one spoken marker; prose around it is kept.
//   L.3.4  JarvisLogConversation taskMatches / eventMatches / filterIsActive:
//          the live "task" filter on a conversation's entries.
//
//   node tests/verify_i_b11_l3_4.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PUB = path.join(__dirname, "..", "web", "public");
function load(file, globals) {
  const win = {};
  const ctx = vm.createContext(Object.assign({ window: win, console }, globals || {}));
  vm.runInContext(fs.readFileSync(path.join(PUB, file), "utf8"), ctx);
  return { win, ctx };
}

let passed = 0;
function ok(name, fn) { fn(); passed += 1; console.log("ok      " + name); }

// ---- I-B11 ------------------------------------------------------------------
const rich = load("rich-text.js");
const R = vm.runInContext("globalThis.JarvisRich", rich.ctx);
assert.ok(R && typeof R.speechText === "function", "JarvisRich.speechText did not load");
const M = "Code block omitted.";

ok("text with no fence is returned unchanged", () => {
  const t = "Hello there.\n\nThis has `inline code` and **bold**.";
  assert.strictEqual(R.speechText(t), t);
});
ok("a fenced block becomes the marker, prose around it is kept", () => {
  const out = R.speechText("Here is the fix:\n\n```js\nconst a = {b: 1};\nconsole.log(a);\n```\n\nRun it and tell me.");
  assert.strictEqual(out, "Here is the fix:\n\n" + M + "\n\nRun it and tell me.");
  assert.ok(!/const|console|```/.test(out));
});
ok("~~~ fences are dropped too", () => {
  const out = R.speechText("Try:\n~~~sh\nrm -rf build\n~~~\nDone.");
  assert.strictEqual(out, "Try:\n\n" + M + "\n\nDone.");
});
ok("two separate blocks give two markers", () => {
  const out = R.speechText("A\n```\none\n```\nB\n```py\ntwo\n```\nC");
  assert.strictEqual(out, "A\n\n" + M + "\n\nB\n\n" + M + "\n\nC");
});
ok("back-to-back blocks collapse to one marker", () => {
  const out = R.speechText("```\none\n```\n```\ntwo\n```");
  assert.strictEqual(out, M);
});
ok("a message that is only code still speaks something", () => {
  assert.strictEqual(R.speechText("```python\nprint(1)\n```"), M);
});
ok("an unclosed fence runs to the end and is dropped", () => {
  assert.strictEqual(R.speechText("Start.\n```js\nlet x = 1;\nlet y = 2;"), "Start.\n\n" + M);
});
ok("a longer fence can contain a shorter one", () => {
  const out = R.speechText("Before\n````md\n```js\nx\n```\n````\nAfter");
  assert.strictEqual(out, "Before\n\n" + M + "\n\nAfter");
});
ok("inline triple-backtick code is not a fence", () => {
  const t = "Use ```like this``` inline.";
  assert.ok(R.speechText(t).includes("like this"));
});
ok("empty / null input never throws", () => {
  assert.strictEqual(R.speechText(""), "");
  assert.strictEqual(R.speechText(null), "");
  assert.strictEqual(R.speechText(undefined), "");
});

// ---- L.3.4 ------------------------------------------------------------------
const lc = load("log-conversation.js");
const P = vm.runInContext("window.JarvisLogConversation", lc.ctx)._pure;
assert.ok(P && typeof P.taskMatches === "function", "taskMatches is not exposed on _pure");

ok("taskMatches: an id matches exactly, ignoring case", () => {
  assert.strictEqual(P.taskMatches({ task_id: "9A8B7C6D", task_label: "x" }, "9a8b7c6d"), true);
  assert.strictEqual(P.taskMatches({ task_id: "9a8b7c6d", task_label: "x" }, "9a8b"), false);
});
ok("taskMatches: any part of the label matches, ignoring case", () => {
  assert.strictEqual(P.taskMatches({ task_id: "1", task_label: "Nightly backup" }, "BACKUP"), true);
  assert.strictEqual(P.taskMatches({ task_id: "1", task_label: "Nightly backup" }, "weather"), false);
});
ok("taskMatches: an entry with no task never matches a non-empty filter", () => {
  assert.strictEqual(P.taskMatches({}, "backup"), false);
  assert.strictEqual(P.taskMatches({ task_id: "", task_label: "" }, "backup"), false);
});
ok("taskMatches: an empty filter matches everything", () => {
  assert.strictEqual(P.taskMatches({}, ""), true);
});
ok("filterIsActive counts a task filter", () => {
  assert.strictEqual(P.filterIsActive({ task: "backup" }), true);
  assert.strictEqual(P.filterIsActive({ task: "" }), false);
  assert.strictEqual(P.filterIsActive(null), false);
});

console.log(`\n${passed} passed, 0 failed`);
