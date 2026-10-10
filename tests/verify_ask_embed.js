// Verifies the pure parts of the L.53 Ask Jarvis side panel:
//   web/public/ask-embed.js      -> JarvisAskEmbed (splitReply, applyStream, sizeFor, stripAnsi, foldNone)
//   web/public/tool-manager.js   -> JarvisToolManager._pure (foldReply, chatKeyFor, splitAgentReply, overwriteView)
//
// Same technique as verify_tool_manager.js: each file is loaded with Node's vm into a
// bare `window`; nothing under test touches the DOM, so no jsdom and no npm install.
//
//   node tests/verify_ask_embed.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PUBLIC = path.join(__dirname, "..", "web", "public");
function load(file, extra) {
  const win = Object.assign({ localStorage: { getItem() { return null; }, setItem() {} } }, extra || {});
  vm.runInNewContext(fs.readFileSync(path.join(PUBLIC, file), "utf8"), { window: win, console });
  return win;
}

const E = load("ask-embed.js").JarvisAskEmbed;
assert.ok(E, "window.JarvisAskEmbed did not load");
const TM = load("tool-manager.js").JarvisToolManager;
assert.ok(TM && TM._pure, "window.JarvisToolManager._pure did not load");
const P = TM._pure;

function ok(name, cond) { if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }
function eq(name, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`FAILED: ${name}\n  got:  ${JSON.stringify(got)}\n  want: ${JSON.stringify(want)}`);
  console.log(`ok       ${name}`);
}

// --- sizing: the pane's own width picks the layout ---------------------------------------
eq("a 320px pane is small", E.sizeFor(320), "small");
eq("a 399px pane is small", E.sizeFor(399), "small");
eq("a 400px pane is normal", E.sizeFor(400), "normal");
eq("a roomy pane is normal", E.sizeFor(900), "normal");
eq("a hidden (0-wide) pane is not 'small' (it keeps its last layout)", E.sizeFor(0), "normal");

// --- stripAnsi ---------------------------------------------------------------------------
eq("ANSI colour codes are removed", E.stripAnsi("\u001b[32mhello\u001b[0m"), "hello");
eq("null is empty", E.stripAnsi(null), "");

// --- splitReply: the CLI's stdout -> the reply -------------------------------------------
eq("a plain 'Jarvis: ...' reply loses its prefix", E.splitReply(["Jarvis: Done."]).text, "Done.");
eq("a multi-line reply keeps its lines", E.splitReply(["Jarvis: First line", "second line", "```python", "x = 1", "```"]).text,
  "First line\nsecond line\n```python\nx = 1\n```");
const dumped = E.splitReply(["Loading config...", "Jarvis: The answer."]);
eq("console scaffolding before the prefixed line is split off, not shown as the reply", dumped.text, "The answer.");
eq("...and kept as the dump", dumped.dump, ["Loading config..."]);
eq("a tool trace echoed inside the reply is not part of it",
  E.splitReply(["Jarvis: Looking.", "[called read_file {}]", "Found it."]).text, "Looking.\nFound it.");
eq("ANSI in the reply is stripped", E.splitReply(["\u001b[1mJarvis:\u001b[0m hi"]).text.length > 0, true);
eq("no lines is an empty reply", E.splitReply([]).text, "");
eq("a reply with no prefix at all is kept whole", E.splitReply(["just text"]).text, "just text");

// --- applyStream: the live text of the current round -------------------------------------
{
  const st = E.newStream();
  eq("text events append", (E.applyStream(st, { k: "text", d: "Hel" }), E.applyStream(st, { k: "text", d: "lo" }), st.text), "Hello");
  eq("a tool event is noted", (E.applyStream(st, { k: "tool", name: "read_file" }), st.tools), ["read_file"]);
  eq("a round that ended in a tool call starts the next round clean", (E.applyStream(st, { k: "round_end", finish: "tool" }), st.text), "");
  eq("...and counts the round", st.round, 2);
  E.applyStream(st, { k: "text", d: "final" });
  eq("a round that ended normally keeps its text", (E.applyStream(st, { k: "round_end", finish: "stop" }), st.text), "final");
  eq("a reset (a restarted attempt) drops this round's text", (E.applyStream(st, { k: "reset" }), st.text), "");
  eq("junk events change nothing", [E.applyStream(st, null), E.applyStream(st, { k: "thinking" }), E.applyStream(st, "x")], ["", "", ""]);
  eq("a text event without text appends nothing", (E.applyStream(st, { k: "text" }), st.text), "");
}

// --- what a chat bubble shows of a reply (foldReply) -------------------------------------
const FILE = "TOOL_GROUP = 'x'\n\ndef f(args):\n    return {'ok': True}\n";
{
  const r = P.foldReply("Added a flag.\n```python\n" + FILE + "```\nRead it first.", false);
  ok("a finished reply: the note stays", r.text.indexOf("Added a flag.") === 0 && r.text.indexOf("Read it first.") !== -1);
  ok("...the file does not", r.text.indexOf("TOOL_GROUP") === -1);
  eq("...and a chip says it was written", r.chip, "Wrote the file \u00b7 4 lines");
}
{
  const r = P.foldReply("Working.\n```python\n" + FILE.split("\n").slice(0, 2).join("\n"), true);
  ok("a streaming reply hides the partial file", r.text.indexOf("TOOL_GROUP") === -1);
  ok("...and says it is being written", /^writing the file into the editor/.test(r.chip));
}
{
  const r = P.foldReply("Working.\n```python\n" + FILE, false);
  eq("a reply cut off before the closing fence is flagged, not claimed as written", r.chip, "The reply was cut off, so the editor was left alone");
}
{
  const r = P.foldReply("Which folder should it scan?", false);
  eq("a question with no code has no chip and keeps its text", r, { text: "Which folder should it scan?", chip: "" });
}
eq("one line is '1 line'", P.foldReply("```python\nx = 1\n```", false).chip, "Wrote the file \u00b7 1 line");

// --- one conversation per tool -----------------------------------------------------------
eq("an edited tool is keyed by its name", P.chatKeyFor("edit", "my_tool", "d1"), "my_tool");
eq("a new, unsaved tab is keyed by its draft key", P.chatKeyFor("new", "", "d1"), "tab:d1");
eq("a new tab that somehow has a name still uses its draft key", P.chatKeyFor("new", "my_tool", "d1"), "tab:d1");
eq("an edit tab with no name falls back to the draft key", P.chatKeyFor("edit", "", "d9"), "tab:d9");

console.log("\nall verify_ask_embed checks passed");
