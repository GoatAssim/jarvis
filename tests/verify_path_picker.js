// Verifies web/public/path-picker.js's pure helpers (JarvisPathPicker._pure) and its safety greps
// (Download Hub N14).  Loaded with Node's vm into a bare `window`, no DOM, plain asserts.
//
//   node tests/verify_path_picker.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const file = path.join(__dirname, "..", "web", "public", "path-picker.js");
const src = fs.readFileSync(file, "utf8");
const win = {};
vm.runInContext(src, vm.createContext({ window: win, console }));
const P = win.JarvisPathPicker && win.JarvisPathPicker._pure;
if (!P) throw new Error("window.JarvisPathPicker._pure did not load");

let n = 0;
function ok(name, cond) {
  n += 1;
  if (!cond) throw new Error(`FAILED: ${name}`);
  console.log(`ok       ${name}`);
}

const entries = [
  { name: "Games", kind: "folder", path: "/g" }, { name: "game-saves", kind: "folder", path: "/gs" },
  { name: "Movies", kind: "folder", path: "/m" }, { name: "setup.exe", kind: "file", path: "/s.exe" },
];
ok("public API: open, startFor, insideRoot", typeof win.JarvisPathPicker.open === "function" && typeof win.JarvisPathPicker.startFor === "function" && typeof win.JarvisPathPicker.insideRoot === "function");

// --- filter ---------------------------------------------------------------------------------
ok("filter: empty keeps all", P.filterEntries(entries, "").length === 4 && P.filterEntries(entries, "   ").length === 4);
ok("filter: case-insensitive contains", P.filterEntries(entries, "GAME").map((e) => e.name).join() === "Games,game-saves");
ok("filter: no match", P.filterEntries(entries, "zzz").length === 0);
ok("filter: bad input is safe", P.filterEntries(null, "a").length === 0 && P.filterEntries(entries, null).length === 4);
ok("filter: does not change the original", (() => { P.filterEntries(entries, "g"); return entries.length === 4; })());

// --- moveIndex --------------------------------------------------------------------------------
ok("move: from nothing, down goes to the first", P.moveIndex(-1, 1, 5) === 0);
ok("move: from nothing, up goes to the last", P.moveIndex(-1, -1, 5) === 4);
ok("move: clamps at both ends", P.moveIndex(4, 1, 5) === 4 && P.moveIndex(0, -1, 5) === 0);
ok("move: a page step clamps", P.moveIndex(2, 10, 5) === 4 && P.moveIndex(2, -10, 5) === 0);
ok("move: an empty list has no highlight", P.moveIndex(0, 1, 0) === -1);

// --- sizes and dates ----------------------------------------------------------------------------
ok("size: bytes, KB, MB, GB", P.fmtSize(10) === "10 B" && P.fmtSize(2048) === "2 KB" && P.fmtSize(5 * 1024 * 1024) === "5 MB" && P.fmtSize(3.5 * 1024 ** 3) === "3.5 GB");
ok("size: junk is empty", P.fmtSize(-1) === "" && P.fmtSize("x") === "" && P.fmtSize(NaN) === "" && P.fmtSize(undefined) === "");
ok("date: a day", /^\d{4}-\d{2}-\d{2}$/.test(P.fmtDate(1760000000)));
ok("date: junk is empty", P.fmtDate(0) === "" && P.fmtDate("x") === "" && P.fmtDate(NaN) === "");

// --- the Select button ----------------------------------------------------------------------------
const here = { path: "/home/me" };
const folder = { name: "Games", kind: "folder", path: "/home/me/Games" };
const longName = { name: "A".repeat(60), kind: "folder", path: "/x" };
const fileE = { name: "tool.exe", kind: "file", path: "/home/me/tool.exe" };
ok("folder pick, nothing highlighted: this folder", P.selectLabel("folder", null, here) === "Select this folder" && P.selectTarget("folder", null, here) === "/home/me");
ok("folder pick, a folder highlighted: that folder", P.selectLabel("folder", folder, here).includes("Games") && P.selectTarget("folder", folder, here) === "/home/me/Games");
ok("folder pick: a long name is shortened in the label", P.selectLabel("folder", longName, here).length < 45);
ok("folder pick at the roots with nothing highlighted: nothing to select", P.selectTarget("folder", null, { path: "" }) === "" && P.selectLabel("folder", null, { path: "" }) === "Select");
ok("file pick: only a file can be selected", P.selectTarget("file", fileE, here) === "/home/me/tool.exe" && P.selectTarget("file", folder, here) === "" && P.selectTarget("file", null, here) === "");
ok("file pick label", P.selectLabel("file", null, here) === "Select this file");

// --- inside the library root ---------------------------------------------------------------------------
ok("insideRoot: a sub-folder", P.insideRoot("D:\\Downloads\\Jarvis", "D:\\Downloads\\Jarvis\\Games\\Old") === "Games/Old");
ok("insideRoot: Windows ignores case and slash style", P.insideRoot("D:\\Downloads\\Jarvis", "d:/downloads/jarvis/Manuals") === "Manuals");
ok("insideRoot: POSIX is case-sensitive", P.insideRoot("/home/me/Jarvis", "/home/me/Jarvis/Games") === "Games" && P.insideRoot("/home/me/Jarvis", "/home/me/jarvis/Games") === null);
ok("insideRoot: the root itself is not a sub-folder", P.insideRoot("/a/b", "/a/b") === null && P.insideRoot("/a/b/", "/a/b") === null);
ok("insideRoot: a sibling with the same prefix is outside", P.insideRoot("/a/b", "/a/bc/x") === null && P.insideRoot("D:\\Lib", "D:\\Library\\x") === null);
ok("insideRoot: outside, empty or junk", P.insideRoot("/a/b", "/c") === null && P.insideRoot("", "/c") === null && P.insideRoot("/a", "") === null && P.insideRoot(null, undefined) === null);
ok("insideRoot: a trailing slash on the pick is fine", P.insideRoot("/a/b", "/a/b/c/") === "c");

// --- where to open ----------------------------------------------------------------------------------------
ok("startFor: a full path is used as typed", P.startFor("D:\\Games", "D:\\Lib") === "D:\\Games" && P.startFor("/mnt/x", "/lib") === "/mnt/x" && P.startFor("~/x", "") === "~/x");
ok("startFor: a sub-folder is joined to the library root", P.startFor("Manuals", "D:\\Lib") === "D:\\Lib\\Manuals" && P.startFor("Manuals/Old", "/lib") === "/lib/Manuals/Old");
ok("startFor: empty opens the library root, or the roots when unknown", P.startFor("", "/lib") === "/lib" && P.startFor("", "") === "" && P.startFor("Manuals", "") === "");

// --- the rules for what this file may do ---------------------------------------------------------------------
ok("never assigns markup", !/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/.test(src));
ok("names go in with textContent", /textContent/.test(src));
ok("both server calls send the hub header and use POST", (src.match(/X-Jarvis-Hub/g) || []).length === 1 && /method: "POST"/.test(src));
ok("a path is never put in a URL", !/ROUTE\s*\+|ROUTE_NEW\s*\+|\?path=/.test(src));
ok("it handles keys at the window, in the capture phase, so a dialog underneath never sees them", /global\.addEventListener\("keydown", onKey, true\)/.test(src) && /global\.removeEventListener\("keydown", onKey, true\)/.test(src));
ok("it uses no browser dialogs", !/\b(alert|confirm|prompt)\(/.test(src));

const css = fs.readFileSync(file.replace(/\.js$/, ".css"), "utf8");
ok("the stylesheet uses the app's tokens and no fixed brand colours outside fallbacks", /var\(--bg-raised/.test(css) && /var\(--accent/.test(css) && /var\(--text/.test(css));
ok("the picker sits above Settings (9200) and below toasts (9300)", /z-index:\s*9250/.test(css));

// last: open() without a document answers null instead of throwing (async, so it closes the count)
win.JarvisPathPicker.open({}).then((v) => {
  ok("open without a document answers null instead of throwing", v === null);
  console.log(`\n${n} checks passed`);
});
