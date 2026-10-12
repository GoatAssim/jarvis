// Verifies the pure helpers behind the Tool Manager's tool-folder choices, via
// JarvisToolManager._pure (same technique as verify_tool_manager_tcmd.js: tool-manager.js is
// loaded with Node's vm into a bare `window`; nothing under test touches the DOM):
//   - commandsOfFile      the "Commands from this file" list on a tool's detail page
//   - bundleNameProblems  the Save-as checks of an import (taken / invalid / used twice /
//                         allowed to replace)
//   - unreadScripts       screen scripts that must be opened before Import turns on
//   - draftWebKey         the key that says whether a tab's screen text changed since its
//                         backup (order of the tabs must not matter)
//
//   node tests/verify_tool_folders_choices.js
//
// Written, not run, by the author of the patch: the owner runs it.

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = { localStorage: { getItem() { return null; }, setItem() {} } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "web", "public", "tool-manager.js"), "utf8"), { window: win, console });
const P = win.JarvisToolManager && win.JarvisToolManager._pure;
assert.ok(P, "window.JarvisToolManager._pure did not load");

let n = 0;
function ok(name, cond) { n += 1; if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }

// ---- commandsOfFile -------------------------------------------------------------------
{
  const CMDS = [
    { name: "disk", file: "disk_demo.py", summary: "How full is each drive" },
    { name: "wipe", file: "disk_demo.py", summary: "Wipe a target", disabled: true },
    { name: "other", file: "other.py", summary: "Other" },
  ];
  const rows = P.buildRows([], [], null, [], { toolCommands: CMDS });
  const mine = P.commandsOfFile(rows, "disk_demo.py");
  ok("only the commands declared by that file", mine.length === 2 && mine.every((r) => r.kind === "tcmd" && r.file === "disk_demo.py"));
  ok("they keep their order and names", mine.map((r) => r.name).join() === "disk,wipe");
  ok("a file with no commands gets none", P.commandsOfFile(rows, "nothing.py").length === 0);
  ok("an empty file name matches nothing (a row with no file is not 'from' every tool)", P.commandsOfFile(rows, "").length === 0
    && P.commandsOfFile(P.buildRows([], [], null, [], { toolCommands: [{ name: "x", summary: "s" }] }), "").length === 0);
  ok("rows that are not tool commands are ignored", P.commandsOfFile([{ kind: "tool", file: "disk_demo.py" }, null, undefined], "disk_demo.py").length === 0);
  ok("tolerates a missing list", P.commandsOfFile(null, "a.py").length === 0 && P.commandsOfFile(undefined, "a.py").length === 0);
}

// ---- bundleNameProblems ---------------------------------------------------------------
{
  const tools = [{ py_file: "a.py" }, { py_file: "b.py" }];
  const taken = ["a_tool"];
  let r = P.bundleNameProblems(tools, { "a.py": "a_tool", "b.py": "b_tool" }, taken, false);
  ok("a name that already exists is a problem for that tool only", r["a.py"] !== "" && r["b.py"] === "");
  r = P.bundleNameProblems(tools, { "a.py": "a_tool", "b.py": "b_tool" }, taken, true);
  ok("...and not when replacing is allowed", r["a.py"] === "" && r["b.py"] === "");
  r = P.bundleNameProblems(tools, { "a.py": "same", "b.py": "same" }, [], false);
  ok("two tools with one name are both flagged, replacing or not", /both be saved as|can.t both/.test(r["a.py"]) && r["b.py"] !== "");
  r = P.bundleNameProblems(tools, { "a.py": "same", "b.py": "same" }, [], true);
  ok("...(a clash inside the bundle is never a 'replace')", r["a.py"] !== "" && r["b.py"] !== "");
  r = P.bundleNameProblems(tools, { "a.py": "Bad Name", "b.py": "" }, [], true);
  ok("an invalid or empty name is a problem whatever the choice", r["a.py"] !== "" && r["b.py"] !== "");
  ok("no tools, no problems", Object.keys(P.bundleNameProblems([], {}, [], false)).length === 0 && Object.keys(P.bundleNameProblems(null, null, null, false)).length === 0);
}

// ---- unreadScripts --------------------------------------------------------------------
{
  const web = [
    { path: "p/tool.js", head: "x()", lines: 1 },
    { path: "p/tool.html", head: "<script></script>", lines: 1 },
    { path: "p/tool.css", size: 3 },
  ];
  ok("every script row must be opened, a stylesheet need not", P.unreadScripts(web, {}).map((f) => f.path).join() === "p/tool.js,p/tool.html");
  ok("opening one leaves the other", P.unreadScripts(web, { "p/tool.js": true }).map((f) => f.path).join() === "p/tool.html");
  ok("opening all of them clears the list", P.unreadScripts(web, { "p/tool.js": true, "p/tool.html": true }).length === 0);
  ok("a bundle with no screen files has nothing to read", P.unreadScripts([], {}).length === 0 && P.unreadScripts(null, null).length === 0);
  ok("an empty script still counts as one to open (head is '' not missing)", P.unreadScripts([{ path: "e.js", head: "" }], {}).length === 1);
}

// ---- draftWebKey ----------------------------------------------------------------------
{
  ok("no screen text is the empty key", P.draftWebKey({}) === "" && P.draftWebKey(null) === "");
  ok("the same text in another order is the same key", P.draftWebKey({ "a.js": "1", "b.css": "2" }) === P.draftWebKey({ "b.css": "2", "a.js": "1" }));
  ok("a changed character changes the key", P.draftWebKey({ "a.js": "1" }) !== P.draftWebKey({ "a.js": "2" }));
  ok("a different file with the same text changes the key", P.draftWebKey({ "a.js": "1" }) !== P.draftWebKey({ "b.js": "1" }));
  ok("text that looks like the separator can't forge another file", P.draftWebKey({ "a.js": "1\",[\"b.js\",\"2" }) !== P.draftWebKey({ "a.js": "1", "b.js": "2" }));
}

console.log(`\n${n} passed, 0 failed`);
