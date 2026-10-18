// Verifies web/public/notifications.js's pure helpers (JarvisNotifications._pure):
// burst collapsing, day sectioning and labels, the history query string, the
// unread / needs-ack readers (including records from before L.30), level
// badges, relative time and the status line.
//
// Same technique as verify_tool_manager.js: the file is loaded with Node's vm
// into a bare context with no `document`, which makes the module export only
// its pure half. No jsdom. The browser behaviour is covered separately by
// tests/verify_notifications_ui.py.
//
//   node tests/verify_notifications.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = {};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "..", "web", "public", "notifications.js"), "utf8"),
  { window: win, console, setTimeout, clearTimeout, setInterval, clearInterval, Date }
);
const P = win.JarvisNotifications && win.JarvisNotifications._pure;
if (!P) throw new Error("window.JarvisNotifications._pure did not load");

let n = 0;
let failed = 0;
function ok(name, cond, detail) {
  n += 1;
  if (!cond) { failed += 1; console.log(`FAILED   ${name}${detail === undefined ? "" : ": " + JSON.stringify(detail)}`); }
  else console.log(`ok       ${name}`);
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const rec = (id, title, at, extra) => Object.assign({ id, title, kind: "notify", created_at: at }, extra || {});

// --- groupKey -----------------------------------------------------------------
ok("same kind/source/title share a key",
   P.groupKey(rec("1", "A", "2026-10-04T10:00:00")) === P.groupKey(rec("2", "A", "2026-10-04T10:05:00")));
ok("a different title is a different key", P.groupKey(rec("1", "A")) !== P.groupKey(rec("1", "B")));
ok("a different kind is a different key", P.groupKey(rec("1", "A")) !== P.groupKey(rec("1", "A", null, { kind: "task" })));
ok("a different source is a different key", P.groupKey(rec("1", "A")) !== P.groupKey(rec("1", "A", null, { source: "x" })));
ok("a failure never shares a key with a success", P.groupKey(rec("1", "A")) !== P.groupKey(rec("1", "A", null, { failed: true })));

// --- needs-ack / unread readers ----------------------------------------------------
ok("needs_ack flag wins when present", P.needsAckOf({ needs_ack: true, persistent: false }) === true);
ok("needs_ack false wins over persistent", P.needsAckOf({ needs_ack: false, persistent: true }) === false);
ok("without the flag: persistent and not acked needs ack", P.needsAckOf({ persistent: true }) === true);
ok("without the flag: acked does not", P.needsAckOf({ persistent: true, acked_at: "x" }) === false);
ok("without the flag: confirm_required needs ack", P.needsAckOf({ confirm_required: true }) === true);
ok("a plain record needs none", P.needsAckOf({}) === false);
ok("null is safe", P.needsAckOf(null) === false && P.isUnreadOf(null) === false);
ok("unread flag wins", P.isUnreadOf({ unread: false, read_at: null }) === false);
ok("without the flag: no read_at is unread", P.isUnreadOf({}) === true);
ok("without the flag: read_at means read", P.isUnreadOf({ read_at: "2026-10-04T10:00:00" }) === false);

// --- levels --------------------------------------------------------------------------
ok("a record with no level is Standard (2)", P.levelOf({}) === 2);
ok("a junk level is Standard (2)", P.levelOf({ level: "banana" }) === 2 && P.levelOf({ level: 9 }) === 2 && P.levelOf({ level: 0 }) === 2);
ok("level 4 stays 4", P.levelOf({ level: 4 }) === 4);
ok("badges: 1 silent, 2 none, 3 persistent, 4 broadcast, 5 confirm",
   eq([1, 2, 3, 4, 5].map((level) => P.levelBadge({ level })), ["silent", "", "persistent", "broadcast", "confirm"]));

// --- collapseBursts ------------------------------------------------------------------------
const burst = [
  rec("c", "Clip", "2026-10-04T10:09:00"), rec("b", "Clip", "2026-10-04T10:05:00"), rec("a", "Clip", "2026-10-04T10:00:00"),
];
let g = P.collapseBursts(burst);
ok("three close ones collapse into one group", g.length === 1 && g[0].count === 3);
ok("the newest is the head", g[0].head.id === "c");
ok("every id is kept, newest first", eq(g[0].ids, ["c", "b", "a"]));
ok("unread ids list covers all three", eq(g[0].unreadIds, ["c", "b", "a"]));

g = P.collapseBursts([
  rec("c", "Clip", "2026-10-04T10:09:00", { read_at: "x" }), rec("b", "Clip", "2026-10-04T10:05:00"), rec("a", "Clip", "2026-10-04T10:00:00"),
]);
ok("unreadIds excludes the read member", eq(g[0].unreadIds, ["b", "a"]));

g = P.collapseBursts([rec("b", "Daily", "2026-10-04T09:00:00"), rec("a", "Daily", "2026-10-03T09:00:00")]);
ok("the same reminder a day apart is NOT folded together", g.length === 2);

g = P.collapseBursts([rec("c", "Clip", "2026-10-04T10:00:00"), rec("m", "Other", "2026-10-04T09:59:00"), rec("a", "Clip", "2026-10-04T09:58:00")]);
ok("a different notification in between breaks the run", g.length === 3);

g = P.collapseBursts([rec("b", "Clip", "2026-10-04T10:00:00", { failed: true }), rec("a", "Clip", "2026-10-04T09:59:00")]);
ok("a failure is not hidden inside a run of successes", g.length === 2);

g = P.collapseBursts([rec("b", "A", "2026-10-04T10:00:00"), rec("a", "A", "2026-10-04T10:00:00")], 0);
ok("a window of 0 still folds identical timestamps", g.length === 1);
g = P.collapseBursts([rec("b", "A", "2026-10-04T10:00:01"), rec("a", "A", "2026-10-04T10:00:00")], 0);
ok("a window of 0 does not fold anything else", g.length === 2);

g = P.collapseBursts([rec("b", "A", "not a date"), rec("a", "A", "also not")]);
ok("unparseable times never fold (and never throw)", g.length === 2);
ok("empty and missing input are safe", eq(P.collapseBursts([]), []) && eq(P.collapseBursts(null), []));

// --- days ----------------------------------------------------------------------------------------
const NOW = new Date(2026, 9, 4, 15, 0, 0).getTime();     // Sun 4 Oct 2026, local
ok("same day is Today", P.dayLabel("2026-10-04T01:00:00", NOW) === "Today");
ok("the day before is Yesterday", P.dayLabel("2026-10-03T23:59:00", NOW) === "Yesterday");
ok("older this year is dated", P.dayLabel("2026-09-29T10:00:00", NOW) === "Tue 29 Sep", P.dayLabel("2026-09-29T10:00:00", NOW));
ok("a previous year carries the year", P.dayLabel("2025-12-31T10:00:00", NOW) === "Wed 31 Dec 2025", P.dayLabel("2025-12-31T10:00:00", NOW));
ok("a future time is Today, not a negative day", P.dayLabel("2026-10-05T10:00:00", NOW) === "Today");
ok("an unparseable time is Earlier", P.dayLabel("garbage", NOW) === "Earlier" && P.dayLabel("", NOW) === "Earlier");

const secs = P.sectionByDay(P.collapseBursts([
  rec("4", "Z", "2026-10-04T10:00:00"), rec("3", "Y", "2026-10-04T09:00:00"),
  rec("2", "X", "2026-10-03T10:00:00"), rec("1", "W", "2026-09-29T10:00:00"),
], 0), NOW);
ok("sections keep order and split by day", eq(secs.map((s) => s.label), ["Today", "Yesterday", "Tue 29 Sep"]));
ok("two rows land in Today", secs[0].groups.length === 2);

// --- relative / clock time --------------------------------------------------------------------------
const T = (m) => new Date(NOW - m * 60000).toISOString();
ok("under a minute is just now", P.relTime(T(0), NOW) === "just now");
ok("minutes", P.relTime(T(5), NOW) === "5m ago");
ok("hours", P.relTime(T(180), NOW) === "3h ago");
ok("days", P.relTime(T(60 * 24 * 2), NOW) === "2d ago");
ok("garbage is empty", P.relTime("nope", NOW) === "" && P.relTime("", NOW) === "");
ok("clockTime pulls HH:MM", P.clockTime("2026-10-04T10:09:33") === "10:09" && P.clockTime("") === "" && P.clockTime(null) === "");
ok("fullTime reads naturally", P.fullTime("2026-10-04T10:09:33.123") === "2026-10-04 10:09:33");

// --- query string -------------------------------------------------------------------------------------
const none = { unread: false, failed: false, ack: false, kind: "", q: "" };
ok("no filters is just the limit", P.historyQuery(none) === "limit=500");
ok("every filter", P.historyQuery({ unread: true, failed: true, ack: true, kind: "task", q: "  disk full " }) ===
   "limit=500&unread=1&failed=1&needs_ack=1&kind=task&q=disk%20full", P.historyQuery({ unread: true, failed: true, ack: true, kind: "task", q: "  disk full " }));
ok("a search with & and = is encoded, not smuggled", P.historyQuery({ q: "a&b=c" }).endsWith("q=a%26b%3Dc"));
ok("a kind with odd characters is encoded", P.historyQuery({ kind: "a b/c" }).includes("kind=a%20b%2Fc"));
ok("whitespace-only search is ignored", P.historyQuery({ q: "   " }) === "limit=500");
ok("filtersActive", !P.filtersActive(none) && P.filtersActive({ failed: true }) && P.filtersActive({ q: "x" }) && !P.filtersActive({ q: "  " }));

// --- body / status ----------------------------------------------------------------------------------------
ok("body prefers the pre-cleaned summary", P.bodyOf({ summary: "short", message: "long long long" }, 100) === "short");
ok("body falls back to the message", P.bodyOf({ message: "only this" }, 100) === "only this");
ok("body is cut with an ellipsis", P.bodyOf({ message: "x".repeat(50) }, 10) === "xxxxxxxxx\u2026");
ok("body of nothing is empty", P.bodyOf({}, 10) === "" && P.bodyOf(null, 10) === "");
ok("status: empty", P.statusLine({ total: 0, unread: 0 }, none) === "no notifications");
ok("status: unread", P.statusLine({ total: 14, unread: 12 }, none) === "12 unread \u00b7 14 total");
ok("status: all read", P.statusLine({ total: 3, unread: 0 }, none) === "all read \u00b7 3 total");
ok("status: filtered", P.statusLine({ total: 14, unread: 12, shown: 2 }, { failed: true }) === "2 shown \u00b7 12 unread \u00b7 14 total");

// --- constants stay in step with the documented rules ---------------------------------------------------------
ok("D-N1 re-surfacing is still every 10 minutes, at most 6 times",
   P.constants.RESURFACE_INTERVAL_MS === 600000 && P.constants.RESURFACE_MAX === 6);
ok("the panel asks for as many as the inbox keeps", P.constants.PANEL_LIMIT === 500);
ok("no more than four toast cards at once", P.constants.MAX_VISIBLE === 4);

// --- E2: toast position -----------------------------------------------------------------------------------
const EIGHT = ["top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right"];
ok("the eight positions are the ones the backend accepts",
   JSON.stringify(P.constants.POSITIONS) === JSON.stringify(EIGHT));
ok("default position is bottom-right (D-12)", P.constants.DEFAULT_POSITION === "bottom-right");
EIGHT.forEach((pos) => ok(`positionOf keeps ${pos}`, P.positionOf({ position: pos }) === pos));
ok("positionOf: a record from before E2 has no position -> default", P.positionOf({ id: "a" }) === "bottom-right");
ok("positionOf: a bad value -> default", P.positionOf({ position: "middle" }) === "bottom-right");
ok("positionOf: not a string -> default", P.positionOf({ position: 7 }) === "bottom-right");
ok("positionOf: null record -> default", P.positionOf(null) === "bottom-right");
ok("positionOf: 'Top Left' and 'top_left' read as top-left",
   P.positionOf({ position: "Top Left" }) === "top-left" && P.positionOf({ position: "top_left" }) === "top-left");

// --- E0: the catalog ---------------------------------------------------------------------------------------
// Without one every helper above used the built-in lists (those checks already passed). With one, the
// helpers follow it, so a level or position added in notify_registry.py works without editing this file.
ok("setCatalog rejects nothing / junk / an empty catalog",
   P.setCatalog(null) === false && P.setCatalog({}) === false && P.setCatalog({ presets: [], positions: [] }) === false);
const fakeCatalog = {
  min_level: 1, max_level: 6, default_level: 2, default_position: "top",
  positions: [{ id: "top" }, { id: "bottom" }],
  presets: [
    { level: 1, id: "silent", channels: ["inbox"], flags: { persistent: false, confirm_required: false } },
    { level: 2, id: "standard", channels: ["inbox", "stream"], flags: { persistent: false, confirm_required: false } },
    { level: 3, id: "persistent", channels: ["inbox", "stream"], flags: { persistent: true, confirm_required: false } },
    { level: 4, id: "urgent", channels: ["inbox", "stream"], flags: { persistent: true, confirm_required: false } },
    { level: 5, id: "ack", channels: ["inbox", "stream"], flags: { persistent: true, confirm_required: false } },
    { level: 6, id: "confirm", channels: ["inbox", "stream"], flags: { persistent: true, confirm_required: true } },
  ],
};
ok("setCatalog accepts a real catalog", P.setCatalog(fakeCatalog) === true);
ok("a level above the old 1-5 range is kept when the catalog allows it", P.levelOf({ level: 6 }) === 6);
ok("a junk level falls to the catalog's default level", P.levelOf({ level: "banana" }) === 2);
ok("badges come from the preset ids; the default level has none",
   JSON.stringify([1, 2, 3, 4, 5, 6].map((level) => P.levelBadge({ level }))) === JSON.stringify(["silent", "", "persistent", "urgent", "ack", "confirm"]));
ok("showsToast follows the preset's channels", P.showsToast(1) === false && P.showsToast(2) === true && P.showsToast(6) === true);
ok("needsConfirm follows the preset's flags, not a hard-coded 5", P.needsConfirm(5) === false && P.needsConfirm(6) === true);
ok("positionOf keeps a position the catalog lists", P.positionOf({ position: "bottom" }) === "bottom");
ok("positionOf: one the catalog does not list falls to the catalog's default", P.positionOf({ position: "left" }) === "top");
P.setCatalog(null);
ok("with the catalog gone the built-in rules are back",
   P.levelOf({ level: 6 }) === 2 && P.showsToast(1) === false && P.showsToast(2) === true
   && P.needsConfirm(5) === true && P.needsConfirm(4) === false && P.positionOf({ position: "left" }) === "left");

console.log(`\n${n - failed}/${n} checks passed`);
process.exit(failed ? 1 : 0);
