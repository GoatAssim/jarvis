// verify_updates_panel_layout.js -- fold, move and drag the source blocks of Menu > Updates (Download Hub round 10, DH-D).
// Loads web/public/updates-panel.js into a vm context, twice over:
//   * PURE: the layout helpers (what is kept, what a bad storage value turns into, the order, the first-sight rule, the keys).
//   * FAKE PAGE: a small fake document and a fake localStorage, so open() really draws the panel from a fake hub answer and the
//     buttons, the grip and the drag events can be pressed. This is a fake, not a browser: nothing here was seen in a real one.
// What it proves: a big source starts folded once and his choice then wins; Collapse all / Expand all; Move up / Move down and a drag
// change the order; the order and the folds come back after a "reload" (a new context on the same fake storage); storage that is
// empty, throws or is missing changes nothing but the memory; a read that changes nothing does not undo a fold, a read that changes
// something does not undo it either; a read that arrives during a drag does not rebuild the page under it; nothing is written to
// the hub (only the one read route is called, by GET).
// Run: node tests/verify_updates_panel_layout.js        (not run by the agent that wrote it)
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "web", "public", "updates-panel.js"), "utf8");

let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) pass++; else { fail++; console.log("FAIL: " + name + (extra !== undefined ? "  -> " + JSON.stringify(extra) : "")); }
}
const clone = (o) => JSON.parse(JSON.stringify(o));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const tick = () => new Promise((r) => setImmediate(r));

// ---------------------------------------------------------------------------------------------------- the fake storage
function fakeStorage(initial) {
  const m = new Map(Object.entries(initial || {}));
  return {
    writes: 0, map: m,
    getItem(k) { return m.has(k) ? m.get(k) : null; },
    setItem(k, v) { this.writes += 1; m.set(k, String(v)); },
  };
}
const brokenStorage = {
  getItem() { throw new Error("blocked"); },
  setItem() { throw new Error("quota"); },
};

// ---------------------------------------------------------------------------------------------------- the fake page
class FNode {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.parentNode = null; this.listeners = {};
    this._t = ""; this.className = ""; this.hidden = false; this.disabled = false; this.scrollTop = 0; this.checked = false;
    this.title = ""; this.rect = { top: 0, height: 100 };
    const self = this;
    this.classList = {
      add(c) { const a = self.className.split(/\s+/).filter(Boolean); if (a.indexOf(c) < 0) a.push(c); self.className = a.join(" "); },
      remove(c) { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
      contains(c) { return self.className.split(/\s+/).indexOf(c) >= 0; },
    };
  }
  get offsetParent() { return this.parentNode; }
  get childNodes() { return this.children; }
  get textContent() { return this.tag === "#text" ? this._t : this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (this.tag === "#text") { this._t = String(v); return; } if (v !== "" && v !== null && v !== undefined) { const t = new FNode("#text"); t._t = String(v); t.parentNode = this; this.children.push(t); } }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); if (k === "disabled") this.disabled = true; if (k === "hidden") this.hidden = true; }
  getAttribute(k) { return k === "class" ? this.className : (k in this.attrs ? this.attrs[k] : null); }
  removeAttribute(k) { delete this.attrs[k]; if (k === "disabled") this.disabled = false; if (k === "hidden") this.hidden = false; }
  appendChild(n) { n.parentNode = this; this.children.push(n); return n; }
  removeChild(n) { this.children = this.children.filter((c) => c !== n); n.parentNode = null; return n; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  getBoundingClientRect() { return this.rect; }
  focus() { this.doc.activeElement = this; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  matches(sel) {
    const m = /^(?:\.([A-Za-z0-9_-]+)|\[([a-z-]+)(?:="([^"]*)")?\]|([a-z]+))$/.exec(sel);
    if (!m) return false;
    if (m[4]) return this.tag === m[4];
    if (m[1]) return this.className.split(/\s+/).indexOf(m[1]) >= 0;
    return m[3] === undefined ? m[2] in this.attrs : this.attrs[m[2]] === m[3];
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (c.tag !== "#text") { if (c.matches(sel)) out.push(c); walk(c); } });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function makeDocument() {
  const doc = {
    activeElement: null, hidden: false, listeners: {},
    createElement(tag) { const n = new FNode(tag); n.doc = doc; return n; },
    createTextNode(t) { const n = new FNode("#text"); n.doc = doc; n._t = String(t); return n; },
    getElementById() { return null; },
    addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
    removeEventListener() {},
    querySelector() { return null; },
    contains(n) { return doc.body.contains(n); },
  };
  doc.body = doc.createElement("body");
  return doc;
}
function fire(node, type, extra) {
  if (node.disabled && type === "click") return false;
  const ev = Object.assign({ type: type, target: node, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
    dataTransfer: { effectAllowed: "", dropEffect: "", data: {}, setData(k, v) { this.data[k] = v; } } }, extra || {});
  (node.listeners[type] || []).slice().forEach((fn) => fn(ev));
  return ev;
}

// ---------------------------------------------------------------------------------------------------- the fake hub
const ROUTES_SEEN = [];
function finding(provider, id, extra) {
  return Object.assign({ provider: provider, id: id, title: id, installed: "1.0", available: "2.0", size_bytes: null, source: "", detail: "",
    age_s: 120, group: "packages", can_apply: true, apply: { state: null, message: "", at: 0 } }, extra || {});
}
function hubData(pipCount, extra) {
  const updates = [finding("minecraft", "sodium", { group: "mods" }), finding("pkg_npm", "left-pad"), finding("pkg_npm", "chalk")];
  for (let i = 0; i < pipCount; i += 1) updates.push(finding("pkg_pip", "pkg" + String(100 + i)));
  const d = {
    ok: true, count: updates.length, daemon_alive: true, applying: 0,
    settings: { auto_update: false, auto_max_per_cycle: 10 },
    groups: ["mods", "games", "packages", "apps", "other"].map((id) => ({ id: id, label: id, included: true, count: updates.filter((u) => u.group === id).length })),
    providers: [
      { provider: "minecraft", label: "Minecraft", group: "mods", status: "ok", age_s: 300, count: 1, can_check: true, can_apply: true, update_interval_h: 24, problems: [] },
      { provider: "pkg_npm", label: "npm", group: "packages", status: "ok", age_s: 300, count: 2, can_check: true, can_apply: true, update_interval_h: 24, problems: [] },
      { provider: "pkg_pip", label: "pip", group: "packages", status: "ok", age_s: 300, count: pipCount, can_check: true, can_apply: true, update_interval_h: 24, problems: [] },
    ],
    updates: updates,
  };
  return Object.assign(d, extra || {});
}

// A fresh "page": a context with the fake document, the fake storage and a fetch that answers from `world.data`.
function page(storage, world) {
  const document = makeDocument();
  const sandbox = {
    document: document, setTimeout: () => 0, clearTimeout: () => {},
    fetch: async (route, opt) => { ROUTES_SEEN.push([route, opt && opt.method || "GET", opt && opt.body || ""]); return { ok: true, status: 200, json: async () => clone(world.data) }; },
  };
  if (storage !== undefined) sandbox.localStorage = storage;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "updates-panel.js" });
  return { U: sandbox.JarvisUpdates, doc: document, sandbox: sandbox };
}
const blocksIn = (doc) => doc.body.querySelectorAll(".up-block");
const idsIn = (doc) => blocksIn(doc).map((n) => n.getAttribute("data-provider"));
const blockOf = (doc, id) => blocksIn(doc).find((n) => n.getAttribute("data-provider") === id);
const focusBtn = (doc, key) => doc.body.querySelectorAll("[data-focus]").find((n) => n.getAttribute("data-focus") === key);
const rowsOf = (node) => node.querySelectorAll(".up-row").length;
async function opened(storage, world) {
  const pg = page(storage, world);
  pg.U.open();
  await tick(); await tick();
  return pg;
}

// ====================================================================================================== PURE
const probe = page(undefined, { data: hubData(0) });
const P = probe.U._pure;
check("the layout helpers are exported", ["cleanLayout", "readLayout", "writeLayout", "isCollapsed", "noteSeen", "setCollapsed", "orderBlocks", "materializeOrder", "moveRelative", "moveStep", "dropSide", "layoutSignature", "applySummary", "focusFallbacks"].every((k) => typeof P[k] === "function"), Object.keys(P));
check("the threshold for starting folded is a plain number and is said in the code", P.COLLAPSE_AT === 8);

// ---- reading what storage holds: anything wrong becomes the default, nothing throws
const empty = P.newLayout();
check("no storage, empty storage, broken storage and junk all read as the default layout",
  same(P.readLayout(null), empty) && same(P.readLayout(fakeStorage()), empty) && same(P.readLayout(brokenStorage), empty)
  && same(P.readLayout(fakeStorage({ [P.LAYOUT_KEY]: "{not json" })), empty) && same(P.readLayout(fakeStorage({ [P.LAYOUT_KEY]: "[1,2]" })), empty)
  && same(P.readLayout(fakeStorage({ [P.LAYOUT_KEY]: JSON.stringify({ v: 99, order: ["a"] }) })), empty));
const dirty = P.cleanLayout(JSON.stringify({ v: 1, order: ["a", "b", "a", 7, "", "x".repeat(300), "c\u0007d", null, "e"], choice: { a: true, b: "yes", c: false }, seen: { a: true, b: 1 }, auto: { a: true, z: false } }));
check("a stored layout is cleaned: ids unique, clean and short; choices must be booleans; seen and auto only when true",
  same(dirty.order, ["a", "b", "e"]) && dirty.choice.a === true && dirty.choice.c === false && !("b" in dirty.choice) && dirty.seen.a === true && !("b" in dirty.seen) && dirty.auto.a === true && !("z" in dirty.auto), dirty);
const evil = P.cleanLayout('{"v":1,"order":["__proto__","constructor"],"choice":{"__proto__":{"polluted":true},"constructor":true},"seen":{"__proto__":true},"auto":{}}');
check("hostile keys such as __proto__ and constructor are plain data: the prototype is not reached and nothing is polluted",
  ({}).polluted === undefined && Object.getPrototypeOf(evil.choice) === null && Object.getPrototypeOf(evil.seen) === null && evil.choice.constructor === true && same(evil.order, ["__proto__", "constructor"]) && P.isCollapsed(evil, "toString") === false);
const big = P.cleanLayout({ v: 1, order: Array.from({ length: 1000 }, (_, i) => "s" + i) });
check("the saved order is capped", big.order.length === P.MAX_IDS);

// ---- writing
const st = fakeStorage();
const lay = P.newLayout(); lay.order = ["b", "a"]; lay.choice.a = true; lay.seen.a = true; lay.auto.b = true;
check("writing keeps one key and what is read back is the same layout", P.writeLayout(st, lay) === true && st.map.size === 1 && st.map.has(P.LAYOUT_KEY) && same(P.readLayout(st), lay));
check("writing says false (and does not throw) when the browser refuses or there is no storage", P.writeLayout(brokenStorage, lay) === false && P.writeLayout(null, lay) === false);
check("what is kept holds source ids and flags only (no title, version, row or text from any source)", Object.keys(JSON.parse(st.map.get(P.LAYOUT_KEY))).sort().join() === "auto,choice,order,seen,v");

// ---- the first-sight rule and his own choice
const L = P.newLayout();
const mk = (id, n) => ({ id: id, rows: Array.from({ length: n }, (_, i) => ({ id: id + i })) });
check("a source with fewer updates than the threshold is not folded", P.noteSeen(L, [mk("small", 7)]) === true && P.isCollapsed(L, "small") === false);
check("a source with the threshold's count or more starts folded", P.noteSeen(L, [mk("bigone", 8)]) === true && P.isCollapsed(L, "bigone") === true);
check("a source with no updates is not 'seen' yet, so it can still start folded when it fills up later", P.noteSeen(L, [mk("later", 0)]) === false && P.noteSeen(L, [mk("later", 30)]) === true && P.isCollapsed(L, "later") === true);
check("the decision is made once: a source that was small and grows later is not folded behind his back", P.noteSeen(L, [mk("small", 40)]) === false && P.isCollapsed(L, "small") === false);
P.setCollapsed(L, ["bigone"], false);
check("his own choice wins over the automatic fold, and the automatic rule never overrides it again", P.isCollapsed(L, "bigone") === false && P.noteSeen(L, [mk("bigone", 99)]) === false && P.isCollapsed(L, "bigone") === false);
const L2 = P.newLayout(); P.setCollapsed(L2, ["tiny"], true);
check("he can fold a small source and it stays folded; setting the same value again changes nothing", P.isCollapsed(L2, "tiny") === true && P.setCollapsed(L2, ["tiny"], true) === false);
check("a fold he made before the source was ever 'seen' is kept by the first-sight rule", P.noteSeen(L2, [mk("tiny", 50)]) === true && P.isCollapsed(L2, "tiny") === true);
const L3 = P.newLayout(); P.setCollapsed(L3, ["x"], false); P.noteSeen(L3, [mk("x", 50)]);
check("an unfold he made before the first sight is kept too", P.isCollapsed(L3, "x") === false);

// ---- the order
const B = ["m", "n", "p"].map((id) => ({ id: id, rows: [] }));
check("with no saved order the default order is kept", same(P.orderBlocks(B, []).map((b) => b.id), ["m", "n", "p"]) && same(P.orderBlocks(B, undefined).map((b) => b.id), ["m", "n", "p"]));
check("saved places come first in the saved order, a source with no place goes to the end, in the default order",
  same(P.orderBlocks(B, ["p", "m"]).map((b) => b.id), ["p", "m", "n"]) && same(P.orderBlocks(B, ["p"]).map((b) => b.id), ["p", "m", "n"]));
check("a saved id that is not reported now is skipped and the input is not changed", same(P.orderBlocks(B, ["gone", "n"]).map((b) => b.id), ["n", "m", "p"]) && same(B.map((b) => b.id), ["m", "n", "p"]));
check("materializing adds every source with no place at the end and keeps the places of absent ones", same(P.materializeOrder(["gone", "p"], B), ["gone", "p", "m", "n"]));
check("moveRelative: before and after a target; unknown ids and itself change nothing",
  same(P.moveRelative(["a", "b", "c"], "c", "a", "before"), ["c", "a", "b"]) && same(P.moveRelative(["a", "b", "c"], "a", "c", "after"), ["b", "c", "a"])
  && same(P.moveRelative(["a", "b", "c"], "a", "a", "after"), ["a", "b", "c"]) && same(P.moveRelative(["a", "b", "c"], "x", "a", "before"), ["a", "b", "c"]) && same(P.moveRelative(["a", "b", "c"], "a", "x", "before"), ["a", "b", "c"]));
check("moveStep goes next to the neighbour that is SHOWN, skipping ids that are not drawn (filtered or absent)",
  same(P.moveStep(["a", "x", "b", "c"], ["a", "b", "c"], "b", -1), ["b", "a", "x", "c"]) && same(P.moveStep(["a", "x", "b", "c"], ["a", "b", "c"], "a", 1), ["x", "b", "a", "c"]));
check("moveStep at the top or bottom, or for an id not shown, changes nothing", same(P.moveStep(["a", "b"], ["a", "b"], "a", -1), ["a", "b"]) && same(P.moveStep(["a", "b"], ["a", "b"], "b", 1), ["a", "b"]) && same(P.moveStep(["a", "b"], ["a", "b"], "q", 1), ["a", "b"]));
check("a drop on the upper half is before, on the lower half after, and junk is before", P.dropSide(10, { top: 0, height: 100 }) === "before" && P.dropSide(90, { top: 0, height: 100 }) === "after" && P.dropSide(120, { top: 100, height: 100 }) === "before" && P.dropSide(NaN, null) === "before");

// ---- the redraw signature
const s1 = P.layoutSignature(lay);
const lay2 = P.cleanLayout(P.layoutToJson(lay)); lay2.order = ["a", "b"];
const lay3 = P.cleanLayout(P.layoutToJson(lay)); P.setCollapsed(lay3, ["b"], true);
check("the layout signature is the same for the same layout and changes with the order and with a fold",
  s1 === P.layoutSignature(P.cleanLayout(P.layoutToJson(lay))) && s1 !== P.layoutSignature(lay2) && s1 !== P.layoutSignature(lay3));
check("the data signature of the page is untouched by the layout (the same keys as before)", P.signature(hubData(3)) === P.signature(hubData(3)) && !/order|choice|auto/.test(Object.keys(JSON.parse(P.signature(hubData(3)))).join()));

// ---- what a folded block still says, and where focus goes
check("applySummary: running or waiting and failed are counted, nothing when idle",
  P.applySummary([{ apply: { state: "running" } }, { apply: { state: "queued" } }, { apply: { state: "failed", message: "x" } }, { apply: { state: null } }]).text === "2 updates running or waiting \u00b7 1 failed"
  && P.applySummary([{ apply: { state: "failed" } }]).bad === true && P.applySummary([{}]).text === "" && P.applySummary(null).text === "");
check("focus fallbacks: a disabled move button falls back to the fold toggle; a row button too; the toolbar buttons to each other",
  same(P.focusFallbacks("up:pkg_pip"), ["tog:pkg_pip", "chip:all"]) && same(P.focusFallbacks("upd:pkg_pip:abc"), ["tog:pkg_pip", "chip:all"]) && P.focusFallbacks("tools:collapse")[0] === "tools:expand" && P.focusFallbacks("tools:reset")[0] === "tools:collapse" && same(P.focusFallbacks("chip:all"), []));

// ====================================================================================================== FAKE PAGE
(async () => {
  // ---- first sight: a big source starts folded, a small one does not
  const world = { data: hubData(10) };
  const st1 = fakeStorage();
  let pg = await opened(st1, world);
  check("the panel draws one block per source with updates, in today's order (group, then name)", same(idsIn(pg.doc), ["minecraft", "pkg_npm", "pkg_pip"]), idsIn(pg.doc));
  let pip = blockOf(pg.doc, "pkg_pip"), npm = blockOf(pg.doc, "pkg_npm");
  check("pip has 10 updates (at or over the threshold) and starts folded: no rows, still its name, group tag, count", pip.className.indexOf("is-collapsed") >= 0 && rowsOf(pip) === 0
    && /pip/.test(pip.textContent) && /Packages/.test(pip.textContent) && /10 updates/.test(pip.textContent));
check("npm has 2 and is open with its rows", npm.className.indexOf("is-collapsed") < 0 && rowsOf(npm) === 2);
check("a folded block still has its own Update all button and a fold toggle that says it is folded",
  pip.querySelectorAll("button").some((b) => b.textContent === "Update all") && focusBtn(pg.doc, "tog:pkg_pip").getAttribute("aria-expanded") === "false" && /Expand pip/.test(focusBtn(pg.doc, "tog:pkg_pip").getAttribute("aria-label")));
check("the automatic decision was kept in the fake storage", st1.writes >= 1 && JSON.parse(st1.map.get(P.LAYOUT_KEY)).auto.pkg_pip === true);
check("Collapse all / Expand all / Reset order exist above the list; Reset order is off while no order is saved",
  !!focusBtn(pg.doc, "tools:collapse") && !!focusBtn(pg.doc, "tools:expand") && focusBtn(pg.doc, "tools:reset").disabled === true);

// ---- his choice wins and survives a read that changes nothing, and a read that changes something
fire(focusBtn(pg.doc, "tog:pkg_pip"), "click");
pip = blockOf(pg.doc, "pkg_pip");
check("pressing the toggle opens the folded block", pip.className.indexOf("is-collapsed") < 0 && rowsOf(pip) === 10 && focusBtn(pg.doc, "tog:pkg_pip").getAttribute("aria-expanded") === "true");
const pipNode = blockOf(pg.doc, "pkg_pip");
pg.U.refreshBadge(); await tick(); await tick();
check("a read with the same data does not redraw the list at all (same nodes) and keeps what he opened", blockOf(pg.doc, "pkg_pip") === pipNode && rowsOf(blockOf(pg.doc, "pkg_pip")) === 10);
world.data = hubData(11);
pg.U.refreshBadge(); await tick(); await tick();
check("a read with changed data does redraw (the count changes) and the block is still open: his choice beat the automatic fold",
  blockOf(pg.doc, "pkg_pip") !== pipNode && /11 updates/.test(blockOf(pg.doc, "pkg_pip").textContent) && rowsOf(blockOf(pg.doc, "pkg_pip")) === 11);
fire(focusBtn(pg.doc, "tog:pkg_npm"), "click");
check("he can fold a small block as well", blockOf(pg.doc, "pkg_npm").className.indexOf("is-collapsed") >= 0 && rowsOf(blockOf(pg.doc, "pkg_npm")) === 0);

// ---- Collapse all / Expand all
fire(focusBtn(pg.doc, "tools:collapse"), "click");
check("Collapse all folds every block in the list and then its own button is off", blocksIn(pg.doc).every((n) => n.className.indexOf("is-collapsed") >= 0) && focusBtn(pg.doc, "tools:collapse").disabled === true && focusBtn(pg.doc, "tools:expand").disabled === false);
fire(focusBtn(pg.doc, "tools:expand"), "click");
check("Expand all opens every block (even the one that started folded)", blocksIn(pg.doc).every((n) => n.className.indexOf("is-collapsed") < 0) && rowsOf(blockOf(pg.doc, "pkg_pip")) === 11 && focusBtn(pg.doc, "tools:expand").disabled === true);

fire(focusBtn(pg.doc, "tog:pkg_npm"), "click");     // Expand all opened npm as well; fold it again by hand so the reload below has a fold to bring back
check("npm is folded again by hand", blockOf(pg.doc, "pkg_npm").className.indexOf("is-collapsed") >= 0);

// ---- Move up / Move down
fire(focusBtn(pg.doc, "dn:minecraft"), "click");
check("Move down puts the first source below the second", same(idsIn(pg.doc), ["pkg_npm", "minecraft", "pkg_pip"]), idsIn(pg.doc));
check("the first block has no Move up and the last no Move down (disabled, not hidden)", focusBtn(pg.doc, "up:pkg_npm").disabled === true && focusBtn(pg.doc, "dn:pkg_pip").disabled === true && focusBtn(pg.doc, "dn:pkg_npm").disabled === false);
fire(focusBtn(pg.doc, "up:pkg_pip"), "click");
check("Move up puts the last source above the one before it", same(idsIn(pg.doc), ["pkg_npm", "pkg_pip", "minecraft"]), idsIn(pg.doc));
check("the move is announced in words in the live line", /moved up, place 2 of 3/.test(pg.doc.body.querySelector(".up-live").textContent), pg.doc.body.querySelector(".up-live").textContent);
check("the saved order is by source id", same(JSON.parse(st1.map.get(P.LAYOUT_KEY)).order, ["pkg_npm", "pkg_pip", "minecraft"]));
check("Reset order is on now", focusBtn(pg.doc, "tools:reset").disabled === false);

// ---- the layout comes back after a reload
const keep = st1.map.get(P.LAYOUT_KEY);
const world2 = { data: hubData(11) };
const pg2 = await opened(fakeStorage({ [P.LAYOUT_KEY]: keep }), world2);
check("after a reload (new page, same storage) the order is the saved one", same(idsIn(pg2.doc), ["pkg_npm", "pkg_pip", "minecraft"]), idsIn(pg2.doc));
check("...and the folds he made are still there: npm folded, pip open", blockOf(pg2.doc, "pkg_npm").className.indexOf("is-collapsed") >= 0 && blockOf(pg2.doc, "pkg_pip").className.indexOf("is-collapsed") < 0);

// ---- a source with no saved place shows at the end until it is moved
world2.data = hubData(11);
world2.data.providers.push({ provider: "pkg_choco", label: "Chocolatey", group: "packages", status: "ok", age_s: 60, count: 1, can_check: true, can_apply: true, problems: [] });
world2.data.updates.push(finding("pkg_choco", "git")); world2.data.count += 1;
pg2.U.refreshBadge(); await tick(); await tick();
check("a new source with no saved place is drawn at the end, not in its sorted place", same(idsIn(pg2.doc), ["pkg_npm", "pkg_pip", "minecraft", "pkg_choco"]), idsIn(pg2.doc));
fire(focusBtn(pg2.doc, "up:pkg_choco"), "click");
check("moving it puts it next to the one above, and now it has a place", same(idsIn(pg2.doc), ["pkg_npm", "pkg_pip", "pkg_choco", "minecraft"]), idsIn(pg2.doc));

// ---- Reset order: today's sort, folds untouched
fire(focusBtn(pg2.doc, "tools:reset"), "click");
check("Reset order brings back group, then name", same(idsIn(pg2.doc), ["minecraft", "pkg_choco", "pkg_npm", "pkg_pip"]), idsIn(pg2.doc));
check("...forgets the saved order but keeps the folds", same(JSON.parse(fakeStorageValue(pg2)).order, []) && blockOf(pg2.doc, "pkg_npm").className.indexOf("is-collapsed") >= 0);
function fakeStorageValue(p) { return p.sandbox.localStorage.map.get(P.LAYOUT_KEY); }

// ---- a group filter: Move works on what is shown
pg2.doc.body.querySelectorAll(".up-chip").find((c) => /packages/i.test(c.textContent)).listeners.click[0]();
check("with the Packages chip on only the package blocks are drawn", same(idsIn(pg2.doc), ["pkg_choco", "pkg_npm", "pkg_pip"]), idsIn(pg2.doc));
fire(focusBtn(pg2.doc, "dn:pkg_choco"), "click");
check("Move down inside a filter swaps with the next block that is shown", same(idsIn(pg2.doc), ["pkg_npm", "pkg_choco", "pkg_pip"]), idsIn(pg2.doc));

// ---- drag
const world3 = { data: hubData(11) };
const st3 = fakeStorage();
const pg3 = await opened(st3, world3);
fire(focusBtn(pg3.doc, "tools:expand"), "click");
check("before a drag: today's order", same(idsIn(pg3.doc), ["minecraft", "pkg_npm", "pkg_pip"]));
let src3 = blockOf(pg3.doc, "minecraft"), tgt3 = blockOf(pg3.doc, "pkg_pip");
let ev = fire(src3, "dragstart");
check("a drag that did not start at the grip does nothing", pg3.U && same(idsIn(pg3.doc), ["minecraft", "pkg_npm", "pkg_pip"]) && src3.className.indexOf("is-dragging") < 0 && ev.dataTransfer.data["text/plain"] === undefined);
fire(src3.querySelector(".up-grip"), "mousedown");
check("pressing the grip makes only that block draggable", src3.getAttribute("draggable") === "true" && tgt3.getAttribute("draggable") === null);
ev = fire(src3, "dragstart");
check("dragstart marks the block and carries its id as text", src3.className.indexOf("is-dragging") >= 0 && ev.dataTransfer.data["text/plain"] === "minecraft" && ev.dataTransfer.effectAllowed === "move");
ev = fire(tgt3, "dragover", { clientY: 90 });
check("over the lower half of another block it may be dropped there and shows a line below it", ev.defaultPrevented === true && tgt3.className.indexOf("is-drop-after") >= 0 && tgt3.className.indexOf("is-drop-before") < 0);
ev = fire(tgt3, "dragover", { clientY: 10 });
check("over the upper half the line moves above it", tgt3.className.indexOf("is-drop-before") >= 0 && tgt3.className.indexOf("is-drop-after") < 0);
ev = fire(src3, "dragover", { clientY: 10 });
check("over itself it is not a drop target", ev.defaultPrevented === false);

// a read that arrives during the drag must not rebuild the page under the hand
world3.data = hubData(12);
pg3.U.refreshBadge(); await tick(); await tick();
check("a read with new data during a drag does not rebuild the blocks", blockOf(pg3.doc, "minecraft") === src3 && blockOf(pg3.doc, "pkg_pip") === tgt3 && /11 updates/.test(tgt3.textContent));
fire(tgt3, "drop", { clientY: 90 });
check("dropping on the lower half of pip puts minecraft after it", same(idsIn(pg3.doc), ["pkg_npm", "pkg_pip", "minecraft"]), idsIn(pg3.doc));
check("the drop was saved by source id", same(JSON.parse(st3.map.get(P.LAYOUT_KEY)).order, ["pkg_npm", "pkg_pip", "minecraft"]));
check("the read that came in during the drag is drawn now (12 updates)", /12 updates/.test(blockOf(pg3.doc, "pkg_pip").textContent));
fire(src3, "dragend");
check("the late dragend of the old block changes nothing more", same(idsIn(pg3.doc), ["pkg_npm", "pkg_pip", "minecraft"]));

// a drag that is cancelled (dragend with no drop) leaves the order alone and clears the marks
const srcC = blockOf(pg3.doc, "pkg_npm");
fire(srcC.querySelector(".up-grip"), "mousedown"); fire(srcC, "dragstart");
fire(blockOf(pg3.doc, "minecraft"), "dragover", { clientY: 10 });
fire(srcC, "dragend");
check("a cancelled drag keeps the order and leaves no marks", same(idsIn(pg3.doc), ["pkg_npm", "pkg_pip", "minecraft"]) && blocksIn(pg3.doc).every((n) => !/is-drag|is-drop/.test(n.className)));
// a drag from a foreign source (text, a file) is ignored
ev = fire(blockOf(pg3.doc, "pkg_pip"), "dragover", { clientY: 10 });
check("a drag the panel did not start is not accepted", ev.defaultPrevented === false);

// ---- storage that is broken or missing changes nothing but the memory
const world4 = { data: hubData(10) };
for (const [name, store] of [["throws on every call", brokenStorage], ["is missing", undefined], ["is null", null]]) {
  const pgB = await opened(store, world4);
  const okOpen = same(idsIn(pgB.doc), ["minecraft", "pkg_npm", "pkg_pip"]) && blockOf(pgB.doc, "pkg_pip").className.indexOf("is-collapsed") >= 0;
  fire(focusBtn(pgB.doc, "tog:pkg_pip"), "click");
  fire(focusBtn(pgB.doc, "dn:minecraft"), "click");
  check("storage that " + name + ": the panel opens, the first-sight fold works, a fold and a move work for this page", okOpen && rowsOf(blockOf(pgB.doc, "pkg_pip")) === 10 && same(idsIn(pgB.doc), ["pkg_npm", "minecraft", "pkg_pip"]), idsIn(pgB.doc));
  check("storage that " + name + ": the person is told, in words, that it lasts only for this page", /would not keep the layout/.test(pgB.doc.body.textContent));
  pgB.U.refreshBadge(); await tick(); await tick();
  check("storage that " + name + ": a later read keeps the layout in memory", same(idsIn(pgB.doc), ["pkg_npm", "minecraft", "pkg_pip"]) && rowsOf(blockOf(pgB.doc, "pkg_pip")) === 10);
}
const pgOk = await opened(fakeStorage(), world4);
check("with working storage that note is not shown", !/would not keep the layout/.test(pgOk.doc.body.textContent));

// ---- closing and opening again keeps the layout
fire(focusBtn(pgOk.doc, "dn:minecraft"), "click");
const orderBefore = idsIn(pgOk.doc);
pgOk.U.close();
check("closing removes the panel", pgOk.doc.body.querySelectorAll(".up-block").length === 0);
pgOk.U.open(); await tick(); await tick();
check("opening it again keeps the order and the folds of this page", same(idsIn(pgOk.doc), orderBefore));

// ---- what the hub sees
const writes = ROUTES_SEEN.filter((r) => r[1] !== "GET");
check("none of this wrote anything to the hub: only the one read route was called, and only by GET", ROUTES_SEEN.length > 0 && writes.length === 0 && ROUTES_SEEN.every((r) => r[0] === "/api/hub/updates"), ROUTES_SEEN.slice(0, 5));

// ---- a folded block still shows its error
const world5 = { data: hubData(10) };
world5.data.providers[2].status = "failed"; world5.data.providers[2].error = "pip is not installed";
const pg5 = await opened(fakeStorage(), world5);
check("a folded source with a failed last check still shows that error", blockOf(pg5.doc, "pkg_pip").className.indexOf("is-collapsed") >= 0 && /pip is not installed/.test(blockOf(pg5.doc, "pkg_pip").textContent));
fire(focusBtn(pg5.doc, "tog:pkg_npm"), "click");
check("...but a folded source that is fine does not show the plain 'Checked ... ago' line, which an open one does",
  blockOf(pg5.doc, "pkg_npm").className.indexOf("is-collapsed") >= 0 && !/Looks again every/.test(blockOf(pg5.doc, "pkg_npm").textContent) && /Looks again every/.test(blockOf(pg5.doc, "minecraft").textContent));
const world6 = { data: hubData(10) };
world6.data.updates.find((f) => f.id === "pkg100").apply = { state: "failed", message: "disk full", at: 1 };
world6.data.updates.find((f) => f.id === "pkg101").apply = { state: "running", message: "", at: 1 };
const pg6 = await opened(fakeStorage(), world6);
check("a folded source tells how many of its updates are running and how many failed", /1 update running or waiting/.test(blockOf(pg6.doc, "pkg_pip").textContent) && /1 failed/.test(blockOf(pg6.doc, "pkg_pip").textContent));

console.log(pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
})().catch((err) => { console.log("CRASH: " + (err && err.stack || err)); process.exit(1); });
