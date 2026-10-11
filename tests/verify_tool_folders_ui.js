// Verifies the pure parts of the Tool Manager's screen-file support:
//   * tool-manager.js (JarvisToolManager._pure): tab labels for a tool's files, dirty tracking
//     across the .py and its screen files, which import path a selection takes, and what the
//     Delete dialog says about a tool's folders.
//   * code-editor.js (JarvisCodeEditor._pure): Enter in a "text" editor only keeps the indent.
//
// Same technique as verify_tool_manager.js: the files are loaded with Node's vm into a bare
// `window`; nothing under test touches the DOM, so no jsdom.
//
//   node tests/verify_tool_folders_ui.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const pub = (name) => fs.readFileSync(path.join(__dirname, "..", "web", "public", name), "utf8");

const win = { localStorage: { getItem() { return null; }, setItem() {} } };
vm.runInNewContext(pub("tool-manager.js"), { window: win, console });
const P = win.JarvisToolManager && win.JarvisToolManager._pure;
assert.ok(P, "window.JarvisToolManager._pure did not load");

const ceWin = {};
vm.runInNewContext(pub("code-editor.js"), { window: ceWin, console });
const C = ceWin.JarvisCodeEditor && ceWin.JarvisCodeEditor._pure;
assert.ok(C, "window.JarvisCodeEditor._pure did not load");

let n = 0;
function ok(name, cond) { n += 1; if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---- tab labels ---------------------------------------------------------------------
ok("a file is labelled by its name", eq(P.webTabLabels(["dice/tool.js"]), { "dice/tool.js": "tool.js" }));
ok("two files with the same name get their folder", eq(P.webTabLabels(["a/tool.js", "b/tool.js", "a/tool.css"]),
  { "a/tool.js": "a/tool.js", "b/tool.js": "b/tool.js", "a/tool.css": "tool.css" }));
ok("a deep path with a clash keeps only the last folder", P.webTabLabels(["x/y/tool.js", "z/tool.js"])["x/y/tool.js"] === "y/tool.js");
ok("no files, no labels", eq(P.webTabLabels([]), {}) && eq(P.webTabLabels(null), {}));

// ---- dirty tracking -----------------------------------------------------------------
const clean = { dirty: false, webFiles: { "a/tool.js": { rel: "a/tool.js", dirty: false } } };
ok("nothing changed: not dirty", P.isDirty(clean) === false && eq(P.dirtyNames(clean), []));
const pyOnly = { dirty: true, webFiles: {} };
ok("only the .py changed", P.isDirty(pyOnly) && eq(P.dirtyNames(pyOnly), ["tool.py"]));
const webOnly = { dirty: false, webFiles: { "a/tool.js": { rel: "a/tool.js", dirty: true }, "a/tool.css": { rel: "a/tool.css", dirty: false } } };
ok("only a screen file changed still counts as unsaved", P.isDirty(webOnly) && eq(P.dirtyNames(webOnly), ["tool.js"]));
const both = { dirty: true, webFiles: { "a/tool.html": { rel: "a/tool.html", dirty: true } } };
ok("both are named, the .py first", eq(P.dirtyNames(both), ["tool.py", "tool.html"]) && P.webDirty(both).length === 1);
ok("an editor with no web state at all is safe", P.isDirty({ dirty: false }) === false && P.isDirty(null) === false && P.webDirty(undefined).length === 0);

// ---- which import path -----------------------------------------------------------------
const f = (name, p) => ({ name, path: p || name });
ok("nothing picked", P.importKind([]) === "none" && P.importKind(null) === "none");
ok("one .py is the original path", P.importKind([f("tool.py")]) === "py" && P.importKind([f("TOOL.PY")]) === "py");
ok("one .zip is the zip path", P.importKind([f("tool.zip")]) === "zip");
ok("several files are a bundle", P.importKind([f("a.py"), f("tool.html", "a/tool.html")]) === "bundle");
ok("a picked folder is a bundle", P.importKind([f("a.py", "mytool/a.py"), f("tool.js", "mytool/ui/tool.js")]) === "bundle");
ok("a .zip with anything else is refused as mixed", P.importKind([f("a.zip"), f("a.py")]) === "mixed" && P.importKind([f("a.zip"), f("b.zip")]) === "mixed");
ok("a lone html file is a bundle (the server explains why it can't be imported)", P.importKind([f("tool.html")]) === "bundle");
ok("only the kinds a tool uses are read", ["a.py", "a.html", "a.htm", "a.js", "a.css"].every((x) => P.IMPORT_TEXT_EXT.test(x))
  && !["a.png", "a.json", "a.md", "a.txt", "a.pyc", "a.jsx"].some((x) => P.IMPORT_TEXT_EXT.test(x)));

// ---- the Delete dialog -------------------------------------------------------------------
ok("no preview, no folders", eq(P.removalSummary(null), { removable: [], blocked: [], kept: 0 }));
const plan = [
  { path: "dice", remove: ["dice/tool.html", "dice/tool.js"], keep: ["dice/notes.txt"], blocked: "" },
  { path: "shared", remove: [], keep: [], blocked: "another tool file also names it" },
  { path: "other", remove: ["other/a.css"], keep: [], blocked: "" },
];
const sum = P.removalSummary(plan);
ok("removable folders carry how many files go", eq(sum.removable.map((x) => [x.path, x.count]), [["dice", 2], ["other", 1]]));
ok("blocked folders carry their reason", eq(sum.blocked, [{ path: "shared", why: "another tool file also names it" }]));
ok("files that stay are counted", sum.kept === 1);
ok("plural", P.plural(1, "screen file") === "1 screen file" && P.plural(3, "screen file") === "3 screen files" && P.plural(0, "file") === "0 files"
  && P.plural(2, "entry", "entries") === "2 entries");
ok("file kinds have labels", P.WEB_KIND_LABEL.html === "HTML" && P.WEB_KIND_LABEL.js === "JavaScript" && P.WEB_KIND_LABEL.css === "CSS");

// ---- the text editor -----------------------------------------------------------------------
let e = C.keepIndentEnter("    <div>", 9, 9);
ok("Enter in a text editor keeps the indent and nothing more", e.insert === "\n    " && e.selStart === 9 + e.insert.length);
e = C.keepIndentEnter("function f() {", 14, 14);
ok("... even after an open brace (no Python-style raise)", e.insert === "\n");
e = C.keepIndentEnter("a\n\tb", 4, 4);
ok("... and a tab indent is kept as a tab", e.insert === "\n\t");
e = C.smartEnter("if x:", 5, 5);
ok("the Python editor still raises the indent after a colon", e.insert === "\n    ");

console.log(`\n${n} checks passed`);
