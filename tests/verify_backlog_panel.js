// Verifies web/public/backlog.js's pure helpers (JarvisBacklog._pure): the
// logic behind L.55's keyboard navigation, inline editing and reordering.
// Same convention as verify_daemons_panel.js: loaded with Node's vm into a
// bare `window`, no DOM, plain asserts.
//
//   node tests/verify_backlog_panel.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = {};
const ctx = vm.createContext({ window: win, console });
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "web", "public", "backlog.js"), "utf8"), ctx);
const P = win.JarvisBacklog && win.JarvisBacklog._pure;
if (!P) throw new Error("window.JarvisBacklog._pure did not load");

let n = 0;
function ok(name, cond) {
  n += 1;
  if (!cond) throw new Error(`FAILED: ${name}`);
  console.log(`ok       ${name}`);
}
const ids = (arr) => arr.map((i) => i.id).join(",");
const mkBoard = () => ({
  idea: [], todo: [{ id: "a", state: "todo" }, { id: "b", state: "todo" }, { id: "c", state: "todo" }],
  doing: [{ id: "d", state: "doing" }], blocked: [{ id: "e", state: "blocked", blocked_on: "x" }], done: [],
});

// --- text helpers -------------------------------------------------------------
ok("collapse joins whitespace", P.collapse("  a \n b\t c ") === "a b c");
ok("collapse of null is empty", P.collapse(null) === "");
ok("normProject lower-cases and collapses", P.normProject("  My   App ") === "my app");

// --- matchesItem -------------------------------------------------------------
const it = { title: "Fix Auth", project: "api", note: "rotate keys", blocked_on: "Ana", priority: "high" };
ok("matches: no filters", P.matchesItem(it, { project: "", priority: "", search: "" }));
ok("matches: search hits the note", P.matchesItem(it, { project: "", priority: "", search: "ROTATE" }));
ok("matches: search hits blocked_on", P.matchesItem(it, { project: "", priority: "", search: "ana" }));
ok("matches: wrong project", !P.matchesItem(it, { project: "web", priority: "", search: "" }));
ok("matches: priority filter", !P.matchesItem(it, { project: "", priority: "low", search: "" }));
ok("matches: missing priority counts as normal", P.matchesItem({ title: "x" }, { project: "", priority: "normal", search: "" }));

// --- ageText -----------------------------------------------------------------
ok("age: now", P.ageText(1000, 1030) === "now");
ok("age: minutes", P.ageText(0, 600) === "10m");
ok("age: hours", P.ageText(0, 3 * 3600) === "3h");
ok("age: days", P.ageText(0, 3 * 86400) === "3d");
ok("age: weeks", P.ageText(0, 21 * 86400) === "3w");
ok("age: months", P.ageText(0, 120 * 86400) === "4mo");
ok("age: no timestamp", P.ageText(null) === "");

// --- anchorAfter / dropTarget / dropAnchor -----------------------------------
const col = mkBoard().todo;
ok("anchorAfter: next card", P.anchorAfter(col, "a", "z") === "b");
ok("anchorAfter: skips the moving card", P.anchorAfter(col, "a", "b") === "c");
ok("anchorAfter: last card -> bottom", P.anchorAfter(col, "c", "z") === null);
ok("anchorAfter: unknown card -> bottom", P.anchorAfter(col, "nope", "z") === null);
const mids = [{ id: "a", mid: 10 }, { id: "b", mid: 30 }];
ok("dropTarget: above the first", P.dropTarget(mids, 5).beforeId === "a");
ok("dropTarget: between", P.dropTarget(mids, 20).beforeId === "b");
ok("dropTarget: below all", P.dropTarget(mids, 99).lastId === "b");
ok("dropTarget: empty column", P.dropTarget([], 5).lastId === null && P.dropTarget([], 5).beforeId === null);
ok("dropAnchor: before a card", P.dropAnchor(col, { beforeId: "b", lastId: null }, "z") === "b");
ok("dropAnchor: after the last VISIBLE card goes before the next hidden one",
   P.dropAnchor(col, { beforeId: null, lastId: "a" }, "z") === "b");
ok("dropAnchor: empty -> bottom", P.dropAnchor(col, { beforeId: null, lastId: null }, "z") === null);

// --- applyMove ---------------------------------------------------------------
let b = mkBoard();
ok("applyMove: reorder up", P.applyMove(b, "c", "todo", "a", 5) === true && ids(b.todo) === "c,a,b");
b = mkBoard();
ok("applyMove: reorder down to bottom", P.applyMove(b, "a", "todo", null, 5) === true && ids(b.todo) === "b,c,a");
b = mkBoard();
ok("applyMove: dropping where it already is changes nothing", P.applyMove(b, "a", "todo", "b", 5) === false && ids(b.todo) === "a,b,c");
ok("applyMove: ...and so does 'bottom' for the last card", P.applyMove(b, "c", "todo", null, 5) === false);
b = mkBoard();
P.applyMove(b, "a", "doing", "d", 7);
ok("applyMove: across columns, at a position", ids(b.doing) === "a,d" && ids(b.todo) === "b,c");
ok("applyMove: state and timestamp updated", b.doing[0].state === "doing" && b.doing[0].updated === 7);
b = mkBoard();
P.applyMove(b, "e", "todo", null, 7);
ok("applyMove: leaving blocked clears blocked_on", b.todo[3].blocked_on === "");
b = mkBoard();
P.applyMove(b, "a", "done", null, 9);
ok("applyMove: to done sets done_at", b.done[0].done_at === 9);
P.applyMove(b, "a", "todo", null, 10);
ok("applyMove: out of done clears done_at", b.todo[b.todo.length - 1].done_at === null);
ok("applyMove: unknown id", P.applyMove(mkBoard(), "zz", "todo", null) === false);
b = mkBoard();
P.applyMove(b, "a", "todo", "gone", 1);
ok("applyMove: a vanished anchor means the bottom", ids(b.todo) === "b,c,a");

// --- removeFromBoard ---------------------------------------------------------
b = mkBoard();
ok("removeFromBoard removes and returns", P.removeFromBoard(b, "b").id === "b" && ids(b.todo) === "a,c");
ok("removeFromBoard unknown", P.removeFromBoard(b, "q") === null);

// --- diffFields --------------------------------------------------------------
const orig = { title: "T one", project: "api", priority: "normal", note: "n", blocked_on: "", state: "todo" };
let d = P.diffFields(Object.assign({}, orig), orig);
ok("diff: nothing changed", d.fields && Object.keys(d.fields).length === 0);
d = P.diffFields(Object.assign({}, orig, { title: "  T   two " }), orig);
ok("diff: only the title, collapsed", JSON.stringify(d.fields) === '{"title":"T two"}');
d = P.diffFields(Object.assign({}, orig, { title: "x" }), orig);
ok("diff: too-short title is an error", typeof d.error === "string");
d = P.diffFields(Object.assign({}, orig, { project: " API " }), orig);
ok("diff: project compared case-insensitively", Object.keys(d.fields).length === 0);
d = P.diffFields(Object.assign({}, orig, { priority: "high", note: "" }), orig);
ok("diff: priority and cleared note", d.fields.priority === "high" && d.fields.note === "");
d = P.diffFields(Object.assign({}, orig, { blocked_on: "review" }), orig);
ok("diff: filling waiting-on sends only blocked_on", JSON.stringify(d.fields) === '{"blocked_on":"review"}');
const blockedOrig = Object.assign({}, orig, { state: "blocked", blocked_on: "review" });
d = P.diffFields(Object.assign({}, blockedOrig, { blocked_on: "" }), blockedOrig);
ok("diff: clearing it on a blocked card also moves it to doing", d.fields.blocked_on === "" && d.fields.state === "doing");
ok("diff: untouched fields are never sent (no revert of someone else's change)",
   !("note" in P.diffFields(Object.assign({}, orig, { title: "New" }), orig).fields));
ok("isDirty", !P.isDirty(Object.assign({}, orig), orig) && P.isDirty(Object.assign({}, orig, { note: "z" }), orig));

// --- pickNeighbour -----------------------------------------------------------
const grid = [["a", "b", "c"], [], ["d"], ["e", "f"], []];
ok("neighbour: down", P.pickNeighbour(grid, { col: 0, row: 0 }, 0, 1) === "b");
ok("neighbour: down at the end stays put", P.pickNeighbour(grid, { col: 0, row: 2 }, 0, 1) === null);
ok("neighbour: up at the top stays put", P.pickNeighbour(grid, { col: 0, row: 0 }, 0, -1) === null);
ok("neighbour: right skips an empty column", P.pickNeighbour(grid, { col: 0, row: 1 }, 1, 0) === "d");
ok("neighbour: right keeps the row, clamped", P.pickNeighbour(grid, { col: 2, row: 0 }, 1, 0) === "e");
ok("neighbour: row is clamped to the shorter column", P.pickNeighbour(grid, { col: 0, row: 2 }, 1, 0) === "d");
ok("neighbour: left", P.pickNeighbour(grid, { col: 3, row: 1 }, -1, 0) === "d");
ok("neighbour: nowhere to the right", P.pickNeighbour(grid, { col: 3, row: 0 }, 1, 0) === null);
ok("neighbour: no position", P.pickNeighbour(grid, null, 1, 0) === null);

// --- fallbackSelection -------------------------------------------------------
ok("fallback: same row in the same column", P.fallbackSelection(grid, { col: 0, row: 1 }) === "b");
ok("fallback: row clamped", P.fallbackSelection(grid, { col: 0, row: 9 }) === "c");
ok("fallback: empty column -> first card anywhere", P.fallbackSelection(grid, { col: 1, row: 0 }) === "a");
ok("fallback: empty board", P.fallbackSelection([[], []], null) === null);

// --- reorderPlan -------------------------------------------------------------
const full = [{ id: "a" }, { id: "h1" }, { id: "b" }, { id: "h2" }, { id: "c" }]; // h* hidden by a filter
const vis = [{ id: "a" }, { id: "b" }, { id: "c" }];
let plan = P.reorderPlan(vis, full, 1, -1);
ok("reorder up: before the previous visible card", plan.anchor === "a" && plan.target === 0);
plan = P.reorderPlan(vis, full, 0, 1);
ok("reorder down: after the next visible card, before the hidden one after it", plan.anchor === "h2" && plan.target === 1);
plan = P.reorderPlan(vis, full, 1, 1);
ok("reorder down onto the last: bottom", plan.anchor === null);
ok("reorder up at the top: nothing", P.reorderPlan(vis, full, 0, -1) === null);
ok("reorder down at the bottom: nothing", P.reorderPlan(vis, full, 2, 1) === null);

console.log(`\n${n} checks passed`);
