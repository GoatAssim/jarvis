// L.43 follow-up: settings.js - the tree's pure helpers (build, filter, defaults,
// leaf input), the deep-link parser, and a structural check of the wiring. No browser,
// no npm install:  node tests/verify_settings_tree.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PUBLIC = path.join(__dirname, "..", "web", "public");
const SRC = fs.readFileSync(path.join(PUBLIC, "settings.js"), "utf8");
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("ok      " + name); }
  else { fail++; console.log("FAILED  " + name + (detail !== undefined ? "  -> " + JSON.stringify(detail) : "")); }
}

// settings.js only touches the DOM from init() and from event listeners, so a document
// that is "still loading" and ignores listeners is enough to load the whole file.
const win = {};
const fakeDocument = { readyState: "loading", addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; } };
vm.runInContext(SRC, vm.createContext({ window: win, document: fakeDocument, console }));
const API = win.JarvisSettings;
check("settings.js loads and exposes show, lastSection, parseHash and _pure", API && typeof API.show === "function" && typeof API.lastSection === "function" && typeof API.parseHash === "function" && API._pure);
const P = API._pure;

// ---- fmtPath / pathKey / kindOf -----------------------------------------------------------
check("fmtPath reads like the server's", P.fmtPath([]) === "(top level)" && P.fmtPath(["levels", "notify"]) === "levels.notify" && P.fmtPath(["providers", 0, "model"]) === "providers[0].model" && P.fmtPath([0, "a"]) === "[0].a");
check("kindOf", P.kindOf(null) === "null" && P.kindOf([1]) === "list" && P.kindOf({}) === "object" && P.kindOf("x") === "string" && P.kindOf(2) === "number" && P.kindOf(true) === "boolean");

// ---- buildTree ----------------------------------------------------------------------------
const DOC = { enabled: true, levels: { notify: 2, task: 2 }, channels: { reminder: ["inbox", "toast"] }, name: "x", empty: {}, nothing: null };
const tree = P.buildTree(DOC, [], 0);
const kids = (n) => n.children.map((c) => c.key);
check("the root is an object with one child per key, in file order", tree.kind === "object" && kids(tree).join() === "enabled,levels,channels,name,empty,nothing", kids(tree));
const levels = tree.children.find((c) => c.key === "levels");
check("children carry their full path", levels.children[0].path.join("/") === "levels/notify" && levels.children[0].value === 2);
const lst = tree.children.find((c) => c.key === "channels").children[0];
check("a list's elements are children keyed by position (numbers, not text)", lst.kind === "list" && lst.children.length === 2 && lst.children[1].path[2] === 1 && typeof lst.children[1].key === "number");
check("null and an empty object are shown, not dropped", tree.children.find((c) => c.key === "nothing").kind === "null" && tree.children.find((c) => c.key === "empty").children.length === 0);
const wide = P.buildTree({ list: Array.from({ length: 500 }, (_, i) => i) }, [], 0).children[0];
check("a very long list is cut and says how many are left", wide.children.length === 400 && wide.more === 100, [wide.children.length, wide.more]);
let deep = { v: 1 };
for (let i = 0; i < 20; i++) deep = { d: deep };
let node = P.buildTree(deep, [], 0), depth = 0;
while (node.children.length) { node = node.children[0]; depth++; }
check("a very deep document stops at a depth limit instead of recursing forever", depth === 12 && node.more === 1, [depth, node.more]);

// A file whose top level is a list (web-data/favorites.json) builds a tree of its entries, and an
// entry's path is its position, which is what the one-value edit sends back to the server.
{
  const t = P.buildTree(["spotify", "tts_bot"], [], 0);
  check("a list at the top builds one child per entry, keyed by position", t.kind === "list" && t.children.length === 2 && t.children[1].path.length === 1 && t.children[1].path[0] === 1 && t.children[1].value === "tts_bot", t);
  check("an entry of such a list can be edited as text", P.parseLeafInput("string", "other").ok === true);
}

// ---- deviations --------------------------------------------------------------------------
const DEVS = [
  { path: ["levels", "notify"], path_text: "levels.notify", state: "changed", current: "5", default: "2", resettable: true },
  { path: ["levels", "queue"], path_text: "levels.queue", state: "missing", current: null, default: "2", resettable: true },
  { path: ["channels", "reminder"], path_text: "channels.reminder", state: "changed", current: "[]", default: "[]", resettable: true },
  { path: ["mine"], path_text: "mine", state: "extra", current: "1", default: null, resettable: false },
];
const idx = P.indexDeviations(DEVS);
check("indexDeviations keys by the path", idx.get(P.pathKey(["levels", "notify"])).state === "changed" && idx.size === 4 && P.indexDeviations(null).size === 0);
check("deviationsUnder counts what lies below a node", P.deviationsUnder(DEVS, ["levels"]).length === 2 && P.deviationsUnder(DEVS, []).length === 4 && P.deviationsUnder(DEVS, ["name"]).length === 0);
check("missingUnder finds defaults absent from a section, direct children only",
  P.missingUnder(DEVS, ["levels"]).length === 1 && P.missingUnder(DEVS, ["levels"])[0].path[1] === "queue" && P.missingUnder(DEVS, []).length === 0 && P.missingUnder(DEVS, ["channels"]).length === 0);

// ---- filterTree --------------------------------------------------------------------------
const leaves = (n) => (n.children.length ? n.children.flatMap(leaves) : [P.fmtPath(n.path)]);
{
  const all = P.filterTree(tree, { needle: "", onlyChanged: false, devs: [] });
  check("no filter keeps everything", leaves(all).length === leaves(tree).length);
  const f = P.filterTree(tree, { needle: "notify", onlyChanged: false, devs: [] });
  check("a word in a setting's name keeps it and its parents", leaves(f).join() === "levels.notify", leaves(f));
  const f2 = P.filterTree(tree, { needle: "inbox", onlyChanged: false, devs: [] });
  check("a word in a VALUE finds it", leaves(f2).join() === "channels.reminder[0]", leaves(f2));
  const f3 = P.filterTree(tree, { needle: "levels 2", onlyChanged: false, devs: [] });
  check("several words must all match (name and value together)", leaves(f3).join() === "levels.notify,levels.task", leaves(f3));
  const f4 = P.filterTree(tree, { needle: "zzzz", onlyChanged: false, devs: [] });
  check("nothing matching leaves an empty root, never null", f4 && f4.children.length === 0);
  const f5 = P.filterTree(tree, { needle: "", onlyChanged: true, devs: DEVS });
  check("only-changed keeps what differs, plus the elements of a list that differs as a whole",
    leaves(f5).join() === "levels.notify,channels.reminder[0],channels.reminder[1]", leaves(f5));
  const f6 = P.filterTree(tree, { needle: "", onlyChanged: true, devs: [DEVS[1]] });
  check("a section whose only difference is a MISSING default stays (so the missing row can show)", f6.children.some((c) => c.key === "levels"), f6.children.map((c) => c.key));
  const f7 = P.filterTree(tree, { needle: "", onlyChanged: true, devs: [] });
  check("only-changed with no differences is empty", f7.children.length === 0);
  const f8 = P.filterTree(tree, { needle: "NOTIFY", onlyChanged: false, devs: [] });
  check("matching ignores case", leaves(f8).join() === "levels.notify");
  check("the original tree is not changed by filtering", leaves(tree).length === 8, leaves(tree));
}

// ---- parseLeafInput ----------------------------------------------------------------------
{
  const p = P.parseLeafInput;
  check("a checkbox is a boolean", p("boolean", true).value === true && p("boolean", false).value === false);
  check("a number is read as a number", p("number", " 12 ").value === 12 && p("number", "1.5").value === 1.5 && p("number", "-3").value === -3);
  check("blank and non-numbers are refused with a reason", p("number", "").ok === false && p("number", "abc").error === "That is not a number." && p("number", "Infinity").ok === false && p("number", null).ok === false);
  check("text is kept exactly (spaces included)", p("string", "  a b ").value === "  a b " && p("string", null).value === "");
  check("lists, objects and null are not edited in the tree", p("list", "x").ok === false && p("object", "x").ok === false && p("null", "x").ok === false);
}

// ---- parseHash ---------------------------------------------------------------------------
{
  const h = API.parseHash;
  check("a non-settings hash is not ours", h("") === null && h("#other") === null && h("#settingsx") === null && h(null) === null);
  check("#settings alone opens Settings where it was", JSON.stringify(h("#settings")) === "{}");
  check("a section", h("#settings/ai").section === "ai" && h("#/settings/Layout").section === "layout");
  check("an unknown section is dropped, not an error", JSON.stringify(h("#settings/nonsense")) === "{}");
  check("advanced with a tab", JSON.stringify(h("#settings/advanced/tunables")) === JSON.stringify({ section: "advanced", tab: "tunables" }));
  check("advanced, files, a file", JSON.stringify(h("#settings/advanced/files/ai_config.json")) === JSON.stringify({ section: "advanced", tab: "files", file: "ai_config.json" }));
  check("a channels file keeps its folder", h("#settings/advanced/files/channels/people.json").file === "channels/people.json");
  check("a web-data file keeps its folder", h("#settings/advanced/files/web-data/favorites.json").file === "web-data/favorites.json");
  check("a folder that is not ours, or a nested one, is dropped", h("#settings/advanced/files/other/favorites.json").file === undefined && h("#settings/advanced/files/web-data/a/b.json").file === undefined && h("#settings/advanced/files/web-data/x.txt").file === undefined);
  check("an encoded name is decoded", h("#settings/advanced/files/ai%5Fconfig.json").file === "ai_config.json");
  check("a file name that could not be one is dropped", h("#settings/advanced/files/..%2Fsecret.json").file === undefined && h("#settings/advanced/files/x.txt").file === undefined && h("#settings/advanced/files/a b.json").file === undefined);
  check("a file is only read under Advanced > Files", h("#settings/ai/files/ai_config.json").file === undefined && h("#settings/advanced/history/ai_config.json").file === undefined);
  check("a malformed escape does not throw", h("#settings/advanced/files/%E0%A4%A.json") !== undefined);
}

// ---- wiring ------------------------------------------------------------------------------
{
  const app = fs.readFileSync(path.join(PUBLIC, "app.js"), "utf8");
  check("/config opens Settings > Advanced > Files", /config: \(\) => openSettingsModal\(\{ section: "advanced", tab: "files" \}\)/.test(app));
  check("the old Config modal is still reachable from its own button", /qs\("#btn-settings"\)\.addEventListener\("click", \(\) => openSettings\("commands\.json"\)\)/.test(app));
  check("app.js reads the hash on load and on change", /addEventListener\("hashchange", openSettingsFromHash\)/.test(app) && /addEventListener\("load", openSettingsFromHash\)/.test(app));
  check("settings.js accepts a section id or an object", /function show\(arg\)/.test(SRC) && /ADV_TABS\.includes\(want\.tab\)/.test(SRC));
  check("settings.js never writes markup from data", !/\.innerHTML\b/.test(SRC));
  const data = fs.readFileSync(path.join(PUBLIC, "slash-commands-data.js"), "utf8");
  check("the palette entry for /config says where it goes now", /"summary": "Open Settings \(Advanced > Files\)"/.test(data));
}

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
