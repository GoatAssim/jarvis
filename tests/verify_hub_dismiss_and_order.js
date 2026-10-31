// Menu > Downloads panel, DH-B: Dismiss on finished downloads, and the display order of the section headers.
//
// tool.js is the body of an async function that receives `host`.  Called with host.__hubPureOnly it returns its pure helpers, so
// they are checked here with no browser (same way as tests/verify_hub_panel.js).  Part 2 also has source checks on the wiring.
// Nothing here ran in a browser: a drag with a real mouse is NOT covered (see the report).
//
// Run: node tests/verify_hub_dismiss_and_order.js
"use strict";
const fs = require("fs");
const path = require("path");

const ui = path.join(__dirname, "..", "jarvis-cli", "jarvis", "actions", "hub_ui");
const source = fs.readFileSync(path.join(ui, "tool.js"), "utf8");
const css = fs.readFileSync(path.join(ui, "tool.css"), "utf8");
const html = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () { /* probe */ }).constructor;

let passed = 0;
let failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log("ok      " + name); } else { failed++; console.log("FAILED  " + name); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const between = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from) + 1));

(async () => {
  const pure = await new AsyncFunction("host", source)({ __hubPureOnly: true });
  const hub = (o) => Object.assign({ id: "h_1", owner: "hub", engine: "http", state: "done", derived: [], capabilities: [], stale: false, dismissed_at: null }, o);
  const prov = (o) => Object.assign({ id: "steam:1", owner: "provider", provider: "steam", state: "done", derived: [], capabilities: [], stale: false, dismissed_at: null }, o);

  // ============================================================ part 1: Dismiss on finished downloads
  check("a hub download can be dismissed once it is done, failed or cancelled",
    ["done", "failed", "cancelled"].every((st) => pure.canDismiss(hub({ state: st }))));
  check("never while it waits, runs, pauses or stalls, and never as 'unknown' (a hub row has no such state)",
    ["queued", "probing", "connecting", "downloading", "postprocessing", "paused", "stalled", "unknown"].every((st) => !pure.canDismiss(hub({ state: st }))));
  check("a provider row's rule did not change",
    ["done", "failed", "cancelled", "unknown"].every((st) => pure.canDismiss(prov({ state: st }))) && !pure.canDismiss(prov({ state: "downloading" })) && !pure.canDismiss(prov({ state: "paused" })));
  const gone = hub({ dismissed_at: 1700000000 });
  check("a hidden finished hub row is dimmed and offers Show again, and can be dismissed again only after it was listed again",
    pure.isHidden(gone) && pure.isDimmed(gone, {}) && !pure.canDismiss(gone, {}) && pure.canDismiss(gone, { h_1: true }) && !pure.isDimmed(gone, { h_1: true }));
  check("junk is still not dismissable", !pure.canDismiss(null) && !pure.canDismiss({}) && !pure.canDismiss(hub({ state: undefined })) && !pure.canDismiss(hub({ owner: "other" })));

  const rows = [hub({ id: "d" }), hub({ id: "c", state: "cancelled" }), hub({ id: "f", state: "failed" }), hub({ id: "q", state: "queued" }),
    hub({ id: "r", state: "downloading" }), prov({ id: "steam:ok" }), prov({ id: "steam:bad", state: "failed" }), prov({ id: "steam:run", state: "downloading" }),
    hub({ id: "hid", dismissed_at: 5 })];
  check("Clear finished takes done and cancelled hub rows and the provider rows, not failed hub rows, not running or waiting ones, not hidden ones",
    eq(pure.dismissableRows(rows, {}).map((r) => r.id), ["d", "c", "steam:ok", "steam:bad"]));
  check("a hidden row never counts for Clear finished, even when it was listed again (it is hidden already)", eq(pure.dismissableRows([hub({ id: "x", dismissed_at: 5 })], { x: true }), []));
  check("dismissableRows copes with nothing", eq(pure.dismissableRows(null, {}), []) && eq(pure.dismissableRows(undefined), []));

  // the Dismiss button is on the card and in the detail pane, and both are drawn from canDismiss
  check("the Dismiss button is drawn from canDismiss on the card and in the detail pane (no second rule)",
    /const hide = dismissButton\(row, false\);\s*if \(hide\) buttons\.push\(hide\)/.test(source) && /const hideBtn = dismissButton\(row, true\);/.test(source)
    && /if \(!dimmed && !canDismiss\(row, S\.revealed\)\) return null;/.test(source));
  check("canDismiss names the three states of a hub row", /row\.state !== "done" && row\.state !== "failed" && row\.state !== "cancelled"/.test(between("function canDismiss", "function isDimmed")));

  // the words
  const T = [pure.DISMISS_TIP, pure.CLEAR_TIP];
  check("both tooltips say no file is touched", T.every((t) => /no file is touched/i.test(t)));
  check("the Dismiss tooltip says a finished download stays in History and that Show hidden lists it", /stays in History/.test(pure.DISMISS_TIP) && /Show hidden/.test(pure.DISMISS_TIP));
  check("the Clear finished tooltip names done and cancelled hub downloads, says failed ones stay, and says History is not touched",
    /done or that you cancelled/.test(pure.CLEAR_TIP) && /Failed downloads of the hub stay/.test(pure.CLEAR_TIP) && /History tab is not touched/.test(pure.CLEAR_TIP));
  check("the old words are gone (finished hub downloads 'are not touched' / 'live in History')",
    !/finished ones\)/.test(pure.CLEAR_TIP) && !/lives in History/.test(source));
  const clearTitle = (/id="clearFinished" title="([^"]*)"/.exec(html) || [])[1] || "";
  check("the #clearFinished tooltip in tool.html says the same as CLEAR_TIP (same rule, same facts)",
    /done or that you cancelled/.test(clearTitle) && /Failed downloads of the hub stay/.test(clearTitle) && /History tab is not touched/.test(clearTitle) && /no file is touched/.test(clearTitle));
  check("tool.html still has Clear finished first in the top strip", /id="clearFinished"[^>]*>Clear finished</.test(html));
  check("Clear finished still posts {finished: true} and Dismiss posts {id}",
    /hubFetch\("\/api\/hub\/dismiss", \{ finished: true \}\)/.test(source) && /hubFetch\("\/api\/hub\/dismiss", \{ id: row\.id \}\)/.test(source));
  check("there is still no confirmation on Dismiss or Clear finished", !/askFirst|confirm\(/.test(between("async function dismissRow", "async function demoAction")));

  // ============================================================ part 2: the display order of the sections
  const sec = (key) => ({ key: key, title: key, meta: "", rows: [] });
  const keys = (list) => list.map((s) => s.key);
  const defaults = ["g:1", "g:2", "g:3", "loose"].map(sec);

  check("applySectionOrder with no order changes nothing", eq(keys(pure.applySectionOrder(defaults, [])), ["g:1", "g:2", "g:3", "loose"]) && eq(keys(pure.applySectionOrder(defaults, null)), ["g:1", "g:2", "g:3", "loose"]));
  check("a saved order puts the sections in that order", eq(keys(pure.applySectionOrder(defaults, ["loose", "g:3", "g:1", "g:2"])), ["loose", "g:3", "g:1", "g:2"]));
  check("sections the order doesn't name keep their default place after the known ones",
    eq(keys(pure.applySectionOrder(defaults, ["g:3", "g:1"])), ["g:3", "g:1", "g:2", "loose"]));
  check("keys of sections that are not on screen are skipped", eq(keys(pure.applySectionOrder(defaults, ["zzz", "g:2", "yyy"])), ["g:2", "g:1", "g:3", "loose"]));
  check("it returns a new list and leaves the input alone", (() => {
    const before = JSON.stringify(defaults); const out = pure.applySectionOrder(defaults, ["g:3"]);
    return out !== defaults && JSON.stringify(defaults) === before;
  })());
  check("one section, junk sections and junk order are fine",
    eq(keys(pure.applySectionOrder([sec("g:1")], ["g:1"])), ["g:1"]) && eq(pure.applySectionOrder(null, ["a"]), []) && eq(pure.applySectionOrder(undefined, undefined), [])
    && pure.applySectionOrder([null, { x: 1 }, sec("g:1")], ["g:1"]).length === 3 && keys(pure.applySectionOrder(defaults, "not a list")).join() === "g:1,g:2,g:3,loose");
  check("update-section keys (any string) work like any other", eq(keys(pure.applySectionOrder(["upd:steam", "upd:playnite", "g:1"].map(sec), ["g:1", "upd:playnite"])), ["g:1", "upd:playnite", "upd:steam"]));
  check("a key that looks like an object property is just a name", eq(keys(pure.applySectionOrder(["__proto__", "constructor", "g:1"].map(sec), ["constructor", "__proto__"])), ["constructor", "__proto__", "g:1"]));
  check("the unnamed section of 'Group by: Nothing' stays where it is", eq(keys(pure.applySectionOrder([sec("")], ["g:1"])), [""]));

  // storage: its own key, garbage ignored, capped
  check("cleanSectionOrder keeps unique, non-empty strings", eq(pure.cleanSectionOrder(["a", "b", "a", "", 5, null, {}, ["x"], "c"]), ["a", "b", "c"]));
  check("garbage in storage is ignored (not JSON, not a list, null, a number, an object)",
    ["not json{", "{\"a\":1}", "null", "42", "\"text\"", "", undefined, null, 7, {}].every((g) => eq(pure.cleanSectionOrder(g), [])));
  check("a JSON string of a list is read", eq(pure.cleanSectionOrder("[\"g:1\",\"g:2\"]"), ["g:1", "g:2"]));
  const many = Array.from({ length: pure.SECTION_ORDER_MAX + 50 }, (_, i) => "k" + i);
  check("the list is capped in length and keeps the first ones", pure.cleanSectionOrder(many).length === pure.SECTION_ORDER_MAX && pure.cleanSectionOrder(many)[0] === "k0");
  check("a key longer than the cap is dropped", eq(pure.cleanSectionOrder(["x".repeat(pure.SECTION_KEY_MAX + 1), "ok"]), ["ok"]) && pure.cleanSectionOrder(["x".repeat(pure.SECTION_KEY_MAX)]).length === 1);
  check("serializeSectionOrder gives JSON of the cleaned list", pure.serializeSectionOrder(["a", "a", 3, "b"]) === "[\"a\",\"b\"]" && pure.serializeSectionOrder(null) === "[]");

  // moving
  const shown = ["g:1", "g:2", "g:3", "loose"];
  check("moveSectionInOrder: before a target", eq(pure.moveSectionInOrder([], shown, "g:3", "g:1", false), ["g:3", "g:1", "g:2", "loose"]));
  check("moveSectionInOrder: after a target", eq(pure.moveSectionInOrder([], shown, "g:1", "g:3", true), ["g:2", "g:3", "g:1", "loose"]));
  check("moveSectionInOrder: any section can go to the very end or the very start",
    eq(pure.moveSectionInOrder([], shown, "g:1", "loose", true), ["g:2", "g:3", "loose", "g:1"]) && eq(pure.moveSectionInOrder([], shown, "loose", "g:1", false), ["loose", "g:1", "g:2", "g:3"]));
  check("moveSectionInOrder: nothing moves for the same key, an unknown key, or a drop where it already is",
    pure.moveSectionInOrder([], shown, "g:1", "g:1", true) === null && pure.moveSectionInOrder([], shown, "nope", "g:1", false) === null
    && pure.moveSectionInOrder([], shown, "g:1", "nope", false) === null && pure.moveSectionInOrder([], shown, "g:1", "g:2", false) === null && pure.moveSectionInOrder([], shown, "g:2", "g:1", true) === null);
  check("stepSectionInOrder: one up, one down, null at either end",
    eq(pure.stepSectionInOrder([], shown, "g:2", -1), ["g:2", "g:1", "g:3", "loose"]) && eq(pure.stepSectionInOrder([], shown, "g:2", 1), ["g:1", "g:3", "g:2", "loose"])
    && pure.stepSectionInOrder([], shown, "g:1", -1) === null && pure.stepSectionInOrder([], shown, "loose", 1) === null && pure.stepSectionInOrder([], shown, "nope", 1) === null);
  check("the input lists are never mutated", (() => {
    const a = shown.slice(); const o = ["g:3", "g:1"]; pure.moveSectionInOrder(o, a, "g:3", "loose", true); pure.stepSectionInOrder(o, a, "g:1", 1);
    return eq(a, shown) && eq(o, ["g:3", "g:1"]);
  })());
  check("a saved key that is not on screen right now keeps its place behind the key it followed",
    eq(pure.moveSectionInOrder(["g:1", "hidden", "g:2"], ["g:1", "g:2"], "g:2", "g:1", false), ["g:2", "g:1", "hidden"]));
  check("a saved key at the very start of the order stays at the start",
    eq(pure.stepSectionInOrder(["hidden", "g:1", "g:2"], ["g:1", "g:2"], "g:2", -1), ["hidden", "g:2", "g:1"]));
  check("moving from the default order saves the whole shown order (sections that were unknown become known)",
    eq(pure.stepSectionInOrder([], ["a", "b", "c"], "c", -1), ["a", "c", "b"]));
  check("round trip: what a move saves is what applySectionOrder shows",
    eq(keys(pure.applySectionOrder(defaults, pure.stepSectionInOrder(["g:3"], keys(pure.applySectionOrder(defaults, ["g:3"])), "g:1", -1))), ["g:1", "g:3", "g:2", "loose"]));

  // the start order helpers are the same as before (their source is untouched, see the report's diff)
  check("the start-order helpers are still exported and still work",
    ["groupIdOf", "groupWaiting", "groupOrderKeys", "moveGroupInOrder", "stepGroupInOrder", "canReorderGroup", "groupReorderNote"].every((n) => typeof pure[n] === "function")
    && pure.groupIdOf("g:7") === "7" && pure.groupIdOf("loose") === "");
  check("a section key that is not a batch is no start-order group", pure.groupIdOf("loose") === "" && pure.groupIdOf("upd:steam") === "");

  // ---- wiring, from the source
  check("the order has its own storage key, read and written by its own functions (not part of `prefs`)",
    /host\.storage\.get\("section_order"\)/.test(source) && /host\.storage\.set\("section_order", serializeSectionOrder\(order\)\)/.test(source)
    && !/section_order/.test(between("function savePrefs", "async function loadPrefs")) && !/section_order/.test(between("async function loadPrefs", "async function loadSectionOrder")));
  check("loading ignores garbage and failures (try/catch, cleaned)", /async function loadSectionOrder\(\) \{\s*try \{ S\.sectionOrder = cleanSectionOrder\(await host\.storage\.get\("section_order"\)\); \} catch \(e\) \{ S\.sectionOrder = \[\]; \}/.test(source));
  check("it is loaded at start, after the prefs", /await loadPrefs\(\);\s*await loadSectionOrder\(\);/.test(source));
  check("renderList applies it to the result of groupRows and the keyboard walk (shownRows) uses the same order",
    /const sections = applySectionOrder\(groupRows\((rows|split\.downloads), groupMode\(\1\)\), S\.sectionOrder\);/.test(between("function renderList", "// ---- middle pane"))
    && /applySectionOrder\(groupRows\(rows, groupMode\(rows\)\), S\.sectionOrder\)\.forEach/.test(between("function shownRows", "function sectionHeader")));
  check("renderList's change-detection list has the order in it",
    /changed\("list", \[rows, S\.selected, S\.groupBy, S\.live, S\.revealed, S\.showHidden, S\.collapsed, S\.acting, S\.sectionOrder\]\)/.test(source));
  check("a Reset order button is built once, next to Collapse all, hidden until an order exists",
    /id: "resetSectionOrder", hidden: "hidden"/.test(source) && /text: "Reset order"/.test(source) && /b\.hidden = !S\.sectionOrder\.length;/.test(source) && !/resetSectionOrder/.test(html));
  check("Reset order clears the order and the storage", /function resetSectionOrder\(\) \{\s*S\.sectionOrder = \[\];\s*saveSectionOrder\(\);/.test(source) && /host\.storage\.remove\("section_order"\)/.test(source));
  check("every named header gets the controls (Batch headers and the others), and the header carries data-section",
    (between("function sectionHeader", "// ---- the display order of the section headers").match(/wireSectionDrag\(head, section\.key, look\.grip\)/g) || []).length === 2
    && (between("function sectionHeader", "// ---- the display order of the section headers").match(/"data-section": section\.key/g) || []).length === 2);
  check("there are Move up / Move down buttons for the keyboard, built with el() and textContent", /\["\\u25B2", -1, "up"\], \["\\u25BC", 1, "down"\]/.test(source)
    && /moveSectionBy\(section\.key, m\[1\]\)/.test(source) && !/innerHTML|insertAdjacentHTML/.test(between("function sectionLookControls", "function sectionHeader")));
  check("the buttons are disabled at the ends and the keyboard is put back on the used button after the list is rebuilt",
    /attrs\.disabled = "disabled"/.test(between("function sectionLookControls", "function sectionHeader")) && /function focusLookButton/.test(source) && /focusLookButton\(key, dir\)/.test(source));
  check("the section drag uses its own state and data type, and stops the header's own start-order drag from also starting",
    /S\.dragSection = key;/.test(source) && /application\/x-jarvis-hub-section/.test(source) && /e\.stopPropagation\(\);\s*\/\/ the header's own drag/.test(source)
    && !/S\.dragGroup/.test(between("function wireSectionDrag", "// ---- the torrent file picker (N24): a folder tree").replace("if (S.dragId || S.dragGroup) { e.preventDefault(); return; }", "")));
  check("the section drag never talks to the hub (no sendOrder, no fetch, no tool call)",
    !/sendOrder|hubFetch|callTool/.test(between("function wireSectionDrag", "// ---- the torrent file picker (N24): a folder tree")) && !/sendOrder|hubFetch|callTool/.test(between("// ---- the display order of the section headers (DH-B): where", "function renderList")));
  const gd = between("function wireGroupDrag", "// ---- dragging a section header only to put it");
  check("the start-order drag is as it was: same state, same type, same reorder route",
    /S\.dragGroup = key;/.test(gd) && /application\/x-jarvis-hub-group/.test(gd) && /sendOrder\(moveGroupInOrder\(order, S\.rows, moving, key, after\)\)/.test(gd) && !/sectionOrder|dragSection/.test(gd));
  check("the start-order arrows keep their names and call moveGroup",
    /"Move group up"/.test(source) && /"Move group down"/.test(source) && /moveGroup\(section\.key, m\[1\]\)/.test(source) && /draggable: reorderable \? "true" : "false"/.test(source));

  // the tooltips tell the two controls apart
  const startTips = [pure.START_GRIP_TIP, pure.START_MOVE_TIP];
  const lookTips = [pure.LOOK_GRIP_TIP, pure.LOOK_MOVE_TIP];
  check("the start-order controls say they change the order downloads START in", startTips.every((t) => /start/i.test(t)) && /start order/i.test(pure.START_GRIP_TIP));
  check("the look controls say they only change how the list looks and that the start order does not change", lookTips.every((t) => /only changes how the list looks/.test(t) && /order downloads start in does not change/.test(t)));
  check("the start-order grip points at the other handle only when the other handle is there",
    /title: START_GRIP_TIP \+ \(look \? START_GRIP_LOOK_NOTE : ""\)/.test(source) && pure.START_GRIP_LOOK_NOTE.indexOf("\u2195") !== -1 && /text: "\\u2195", title: LOOK_GRIP_TIP/.test(source));
  check("the start-order arrows' tooltip says (start order) and the look buttons' says on screen",
    /title: m\[2\] \+ " \(start order\): " \+ START_MOVE_TIP/.test(source) && /title: "Move this section " \+ m\[2\] \+ " on screen\. " \+ LOOK_MOVE_TIP/.test(source));

  // the CSS and the html
  check("the new controls have CSS, placed after the section grip rule",
    /\.section__look \{/.test(css) && /\.section__grip--look \{/.test(css) && /\.rbtn\.section__lookbtn \{/.test(css) && css.indexOf(".section__look {") > css.indexOf(".section__grip {"));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
