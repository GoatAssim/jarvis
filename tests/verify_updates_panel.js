// verify_updates_panel.js -- web/public/updates-panel.js without a browser (Download Hub round 5, agent H2).
// Loads the panel file into a vm context (no DOM: only the pure helpers and the DOM-free write flows are used) and checks them
// against a small FAKE HUB built from exactly the payloads and op names of the round-5 updates contract (item 5 and 6):
//   * the words: the header line, each source's last check, the apply state of a row (`started` is NOT "updated"), the empty states;
//   * the list: one block per source, in group order and then by name, never by version; a group that is switched off is gone;
//   * Update all: how many, which groups, the arguments it sends; rows that cannot be applied or are already busy are not counted;
//   * the automatic-update tick: disabled with a reason when there is no apply tool or the master switch is off;
//   * the safety core: the ORDER and CONTENT of every write flow (dry run first, one dialog that shows the plan, then the real call;
//     a no, a failed dry run or no dialog at all starts nothing; the master switch asks first; only the allowed setting keys are sent);
//   * hostile provider text stays text: capped, stripped of control characters, never turned into markup.
// What is NOT checked here (no DOM): how the panel, blocks and drawer are drawn and wired. tests/test_updates_panel.py reads the
// file for that, and the manual checks in the delivery notes cover the rest (nothing here ran in a browser or against a real hub).
// Run: node tests/verify_updates_panel.js        (not run by the agent that wrote it)
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "web", "public", "updates-panel.js"), "utf8");

function load(extra) {
  const sandbox = Object.assign({}, extra || {});
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "updates-panel.js" });
  return sandbox.JarvisUpdates;
}
const U = load();
const P = U && U._pure;

let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) pass++; else { fail++; console.log("FAIL: " + name + (extra !== undefined ? "  -> " + JSON.stringify(extra) : "")); }
}
const clone = (o) => JSON.parse(JSON.stringify(o));

// ---- the fake hub: the `updates` answer as the contract describes it (a finding adds group, can_apply, apply; a provider adds
// group, auto_update, can_apply, updates_only; the top level adds groups, settings, applying).
const NOW_APPLY = { state: null, message: "", at: 0 };
function finding(provider, id, title, extra) {
  return Object.assign({ provider: provider, id: id, title: title, installed: "1.0", available: "2.0", size_bytes: null, source: "", detail: "",
    age_s: 120, group: "other", can_apply: true, apply: Object.assign({}, NOW_APPLY) }, extra || {});
}
function hub() {
  return {
    ok: true, count: 5, daemon_alive: true, applying: 0, limits: { interval_h_min: 0.5, manual_check_s: 30 },
    settings: { auto_update: false, include_groups: ["mods", "games", "packages", "apps", "other"], auto_max_per_cycle: 10 },
    groups: [
      { id: "mods", label: "Mods", included: true, count: 1, providers: ["minecraft"] },
      { id: "games", label: "Games", included: true, count: 1, providers: ["steam_updates"] },
      { id: "packages", label: "Packages", included: true, count: 3, providers: ["pkg_pip", "pkg_npm"] },
      { id: "apps", label: "Apps", included: true, count: 0, providers: [] },
      { id: "other", label: "Other", included: true, count: 0, providers: [] },
    ],
    providers: [
      { provider: "minecraft", label: "Minecraft", group: "mods", status: "ok", age_s: 300, count: 1, can_check: true, can_apply: true, auto_update: false, updates_only: false, update_interval_h: 24, problems: [] },
      { provider: "steam_updates", label: "Steam", group: "games", status: "ok", age_s: 300, count: 1, can_check: true, can_apply: true, auto_update: false, updates_only: true, update_interval_h: 12, problems: [] },
      { provider: "pkg_pip", label: "pip", group: "packages", status: "partial", age_s: 90, count: 2, can_check: true, can_apply: true, auto_update: true, updates_only: true, update_interval_h: 24, problems: [{ source: "pip", error: "one index did not answer" }] },
      { provider: "pkg_npm", label: "npm", group: "packages", status: "failed", error: "npm is not installed", age_s: 7200, count: 1, can_check: true, can_apply: false, auto_update: false, updates_only: true, update_interval_h: 24, problems: [] },
      { provider: "pkg_choco", label: "Chocolatey", group: "packages", status: "ok", age_s: 60, count: 0, can_check: true, can_apply: true, auto_update: false, updates_only: true, update_interval_h: 24, problems: [] },
    ],
    updates: [
      // 0.5.1 -> 0.5.0 on purpose: the panel must show the two texts and say nothing about which is newer.
      finding("minecraft", "sodium", "Sodium", { group: "mods", installed: "0.5.1", available: "0.5.0", size_bytes: 2097152, source: "modrinth" }),
      finding("steam_updates", "730", "Counter-Strike 2", { group: "games", installed: "build 10", available: "build 12", source: "steam" }),
      finding("pkg_pip", "requests", "requests", { group: "packages", installed: "2.9.0", available: "2.10.0", source: "pip" }),
      finding("pkg_pip", "Black", "Black", { group: "packages", installed: "24.1", available: "24.2", source: "pip" }),
      finding("pkg_npm", "left-pad", "left-pad", { group: "packages", can_apply: false, source: "npm" }),
    ],
  };
}
const D = hub();

check("the panel exports its pure helpers and flows", !!P && typeof P.blocksOf === "function" && typeof U._flows === "function" && typeof U.open === "function");
check("it names the three existing routes and the three new ones, and no other",
  JSON.stringify(Object.values(P.ROUTES).sort()) === JSON.stringify(["/api/hub/updates", "/api/hub/updates/apply", "/api/hub/updates/check", "/api/hub/updates/dismiss", "/api/hub/updates/dry-run", "/api/hub/updates/set"]), P.ROUTES);

// ---- text from another program is data
check("text(): control and bidi characters are stripped and the length is capped", P.text("a\u001b[31mb\u202ec\u0007", 50) === "a[31mbc" && P.text("x".repeat(20), 5) === "xxxx\u2026" && P.text(null) === "");
check("text(): markup is not touched (it is only ever drawn with textContent), so a hostile name stays a plain string", P.text("<img src=x onerror=alert(1)>") === "<img src=x onerror=alert(1)>");
check("a hostile title is capped in the version, meta and health lines too",
  P.versionText({ installed: "x".repeat(500), available: "y" }).length <= 80 + 3 + 80 && P.metaParts({ source: "s".repeat(300) }).every((t) => t.length <= 60)
  && P.healthText({ status: "failed", error: "e".repeat(900) }).text.length < 300);
check("fmtAgo: in words, never a date", P.fmtAgo(10) === "just now" && P.fmtAgo(300) === "5 min ago" && P.fmtAgo(7200) === "2 h ago" && P.fmtAgo(3 * 86400) === "3 d ago" && P.fmtAgo(-1) === "" && P.fmtAgo("x") === "");
check("fmtBytes: a size only when the source gave one", P.fmtBytes(2097152) === "2.0 MB" && P.fmtBytes(0) === "" && P.fmtBytes(null) === "" && P.fmtBytes(5 * 1073741824) === "5.00 GB");

// ---- the finding row: versions are two texts, nothing more
check("versions: the two texts as the source gave them, with an arrow, nothing about which is newer",
  P.versionText(D.updates[0]) === "0.5.1 \u2192 0.5.0" && !/newer|older|latest|upgrade/i.test(P.versionText(D.updates[0])));
check("versions: a missing one says so instead of guessing", P.versionText({}) === "version not stated \u2192 version not stated");
check("meta: size when known, which program reported it, how old the finding is",
  JSON.stringify(P.metaParts(D.updates[0])) === JSON.stringify(["2.0 MB", "via modrinth", "checked 2 min ago"]) && JSON.stringify(P.metaParts(D.updates[1])) === JSON.stringify(["via steam", "checked 2 min ago"]));

// ---- the apply state, in words
const av = (state, message) => P.applyView({ state: state, message: message || "", at: 1 });
check("no apply state: nothing is drawn", P.applyView(null) === null && P.applyView({ state: null, message: "", at: 0 }) === null && P.applyView({ state: "weird" }) === null);
check("queued and running are busy (the Update button is off, the list is polled fast)", av("queued").busy === true && av("running").busy === true && av("queued").label === "Waiting its turn" && av("running").label === "Updating now\u2026");
check("done says Done and that the row goes when a check stops listing it", av("done", "pip upgraded").label === "Done" && /pip upgraded/.test(av("done", "pip upgraded").detail) && /stops listing it/.test(av("done").detail) && av("done").busy === false);
check("started is NOT \"updated\": it says the program was handed the job and that the row stays until a later check stops listing it",
  av("started").label === "Handed over" && /handed to the program/i.test(av("started").detail) && /stays until a later check stops listing it/.test(av("started").detail)
  && !/updated/i.test(av("started").label + av("started").detail) && /Steam/.test(av("started").detail));
check("failed shows the message as given (capped), or says none was given", av("failed", "disk full").detail === "disk full" && av("failed").detail === "No reason was given." && av("failed").tone === "bad" && av("failed", "m".repeat(500)).detail.length <= 200);
check("isBusy follows the apply state of a finding", P.isBusy({ apply: { state: "running" } }) === true && P.isBusy({ apply: { state: "failed" } }) === false && P.isBusy({}) === false);

// ---- the header line and the badge
check("header: how many, when it last checked, that the daemon runs",
  P.headerText(D) === "5 updates available \u00b7 last checked 1 min ago \u00b7 hub daemon running", P.headerText(D));
const down = clone(D); down.daemon_alive = false; down.applying = 2;
check("header: with the daemon down it says so and where to start it (Menu > Daemons); a running apply is counted",
  /the hub daemon is not running: start it in Menu > Daemons/.test(P.headerText(down)) && /2 updates are running or waiting/.test(P.headerText(down)), P.headerText(down));
check("header: nothing found, nothing checked yet", P.headerText({ count: 0, daemon_alive: true, providers: [] }) === "No updates found \u00b7 nothing checked yet \u00b7 hub daemon running");
check("header: before the first answer it says it is reading, or shows the error", P.headerText(null) === "reading the updates\u2026" && P.headerText(null, { error: "HTTP 500" }) === "HTTP 500");
check("badge: the count the updates op returns, nothing at 0, 99+ above", P.badgeText(D) === "5" && P.badgeText({ count: 0 }) === "" && P.badgeText({ count: 250 }) === "99+" && P.badgeText(null) === "" && P.badgeText({}) === "");

// ---- the last check of one source, in words
const hp = (id) => D.providers.find((p) => p.provider === id);
check("health: ok says when and how often", P.healthText(hp("minecraft")).text === "Checked 5 min ago. Looks again every 24 h." && P.healthText(hp("minecraft")).tone === "");
check("health: partial lists each problem as text", P.healthText(hp("pkg_pip")).tone === "warn" && /not everything/.test(P.healthText(hp("pkg_pip")).text) && P.healthText(hp("pkg_pip")).lines[0] === "pip: one index did not answer");
check("health: failed says which and what it said", P.healthText(hp("pkg_npm")).tone === "bad" && /npm is not installed/.test(P.healthText(hp("pkg_npm")).text) && /The list below is from 2 h ago/.test(P.healthText(hp("pkg_npm")).text));
check("health: never checked, checking, pushed", /Not checked yet/.test(P.healthText({ status: "never" }).text) && P.healthText({ status: "checking" }).text === "Checking now\u2026" && /reports its own updates/.test(P.healthText({ status: "pushed" }).text));
check("health: a source with its update checks off says nothing is checked or shown", /switched off/.test(P.healthText({ provider: "x", update_checks: false }).text));

// ---- groups and chips
check("chips: All plus the five groups with the count the hub gave", JSON.stringify(P.chipsOf(D).map((c) => [c.id, c.count])) === JSON.stringify([["all", 5], ["mods", 1], ["games", 1], ["packages", 3], ["apps", 0], ["other", 0]]), P.chipsOf(D));
const old = clone(D); delete old.groups;
check("chips: an answer without `groups` is counted from the findings (an older hub), never a crash", JSON.stringify(P.chipsOf(old).map((c) => c.count)) === JSON.stringify([5, 1, 1, 3, 0, 0]), P.chipsOf(old));
check("a group the panel does not know counts as Other", P.groupOf("nonsense") === "other" && P.groupOf(" MODS ") === "mods" && P.groupOf(undefined) === "other");
const noPk = clone(D); noPk.groups[2].included = false; noPk.groups[2].count = 0; noPk.count = 2;
noPk.updates = noPk.updates.filter((f) => f.group !== "packages");
check("a group that is switched off: its sources leave the list, and the chip says off with the switch still available",
  !P.blocksOf(noPk, "all", {}).some((b) => b.group === "packages") && P.groupsOf(noPk)[2].included === false && P.blocksOf(noPk, "packages", {}).length === 0);
check("settings: the master switch and the per-round cap, off by default when the hub says nothing", P.settingsOf(D).auto_update === false && P.settingsOf(D).auto_max_per_cycle === 10 && P.settingsOf({}).auto_update === false);

// ---- the list: one block per source, in group order and then by name; rows by name, never by version
const blocks = P.blocksOf(D, "all", {});
check("blocks: one per source, mods then games then packages, then by name", JSON.stringify(blocks.map((b) => b.id)) === JSON.stringify(["minecraft", "steam_updates", "pkg_choco", "pkg_npm", "pkg_pip"]), blocks.map((b) => b.id));
check("blocks: rows inside a source are ordered by name, not by version", JSON.stringify(blocks.find((b) => b.id === "pkg_pip").rows.map((f) => f.id)) === JSON.stringify(["Black", "requests"]));
check("blocks: a filter keeps only that group", JSON.stringify(P.blocksOf(D, "packages", {}).map((b) => b.id)) === JSON.stringify(["pkg_choco", "pkg_npm", "pkg_pip"]) && P.blocksOf(D, "mods", {}).length === 1);
check("blocks: a finding of a source the providers list does not have still gets a block", P.blocksOf({ updates: [finding("lone", "a", "A", { group: "apps" })] }, "all", {})[0].id === "lone");
const split = P.splitBlocks(blocks, "");
check("a source that was checked and found nothing is one name in a closing line, a failed or partial one keeps its block",
  JSON.stringify(split.quiet.map((b) => b.id)) === JSON.stringify(["pkg_choco"]) && split.shown.some((b) => b.id === "pkg_npm") && split.shown.some((b) => b.id === "pkg_pip")
  && P.splitBlocks(blocks, "pkg_choco").quiet.length === 0);
const sources = P.blocksOf(D, "all", { gone: { label: "Gone", group: "apps" } });
check("a source the person switched off in this session stays reachable (the hub leaves it out of its answer)",
  sources.some((b) => b.id === "gone") && P.splitBlocks(sources, "").shown.some((b) => b.id === "gone") && P.providersOf(D, { gone: { label: "Gone", group: "apps" } }).find((p) => p.provider === "gone").update_checks === false);

// ---- honest empty states
const none = { ok: true, count: 0, daemon_alive: true, providers: [], updates: [], groups: [] };
check("empty: no source at all", P.emptyState(none, "all", P.blocksOf(none, "all", {})).kind === "none");
const never = { ok: true, count: 0, daemon_alive: true, providers: [{ provider: "a", label: "A", group: "apps", status: "never", can_check: true }], updates: [] };
check("empty: nothing checked yet, and it offers Check now", (() => { const e = P.emptyState(never, "all", P.blocksOf(never, "all", {})); return e.kind === "never" && e.check === true && /Nothing has been checked yet/.test(e.text); })());
const clean = { ok: true, count: 0, daemon_alive: true, providers: [{ provider: "a", label: "A", group: "apps", status: "ok", age_s: 600, can_check: true }], updates: [] };
check("empty: nothing needs updating (said as what the sources reported, not as a version fact)", (() => { const e = P.emptyState(clean, "all", P.blocksOf(clean, "all", {})); return e.kind === "clean" && /Nothing reported an update/.test(e.text) && /10 min ago/.test(e.text); })());
const failed = { ok: true, count: 0, daemon_alive: true, providers: [{ provider: "a", label: "A", group: "apps", status: "failed", error: "boom", can_check: true }], updates: [] };
check("empty: a failed source never reads as \"all up to date\"", (() => { const e = P.emptyState(failed, "all", P.blocksOf(failed, "all", {})); return e.kind === "failed" && /does not mean everything is up to date/.test(e.text); })());
check("empty: a group that is switched off says so", P.emptyState(noPk, "packages", []).kind === "group_off");
check("empty: there is no empty state when there is a list", P.emptyState(D, "all", blocks) === null);

// ---- Update all and a single Update: what the dialog says and what is sent
const plan = P.planAll(D, { group: "all" });
check("Update all counts only what can be applied now, per group, and the rest separately",
  plan.count === 4 && plan.cant === 1 && plan.busy === 0 && JSON.stringify(plan.groups) === JSON.stringify([{ id: "mods", label: "Mods", count: 1 }, { id: "games", label: "Games", count: 1 }, { id: "packages", label: "Packages", count: 2 }]), plan);
check("Update all for everything sends {all:true}; for a group {all:true, group}; for one source {provider, all:true}",
  JSON.stringify(plan.args) === JSON.stringify({ all: true }) && JSON.stringify(P.planAll(D, { group: "packages" }).args) === JSON.stringify({ all: true, group: "packages" })
  && JSON.stringify(P.planAll(D, { provider: "pkg_pip" }).args) === JSON.stringify({ provider: "pkg_pip", all: true }) && P.planAll(D, { provider: "pkg_pip" }).count === 2);
const busyHub = clone(D); busyHub.updates[2].apply = { state: "running", message: "", at: 1 }; busyHub.updates[3].apply = { state: "queued", message: "", at: 1 };
check("a finding already queued or running is not counted again", P.planAll(busyHub, { provider: "pkg_pip" }).count === 0 && P.planAll(busyHub, { provider: "pkg_pip" }).busy === 2);
const dlgAll = P.confirmAllText(plan, "");
check("the Update all dialog says how many, which groups, and that it runs each program's own updater",
  dlgAll.title === "Update 4 things?" && /Mods 1, Games 1, Packages 2/.test(dlgAll.body) && /own updater/.test(dlgAll.body) && /1 more cannot be updated from here/.test(dlgAll.body) && dlgAll.focusCancel === true);
const dlgOne = P.confirmOneText(D.updates[0], "pip install --upgrade Sodium==0.5.0");
check("the single Update dialog shows the plan line the dry run gave, as its own block, and says whose updater runs",
  dlgOne.pre === "pip install --upgrade Sodium==0.5.0" && /own updater/.test(dlgOne.body) && /exactly what will run/.test(dlgOne.body) && dlgOne.title === "Update Sodium?");
check("with no plan line the dialog says the program did not say what it will run", /did not say what it will run/.test(P.confirmOneText(D.updates[0], "").body) && P.confirmOneText(D.updates[0], "").pre === "");

// ---- the automatic-update tick
check("tick: disabled with a reason when the source has no apply tool", (() => { const t = P.autoTick(hp("pkg_npm"), true); return t.disabled && /no updater of its own/.test(t.reason); })());
check("tick: disabled with a reason when the master switch is off, usable when it is on",
  (() => { const off = P.autoTick(hp("minecraft"), false); const on = P.autoTick(hp("minecraft"), true); return off.disabled && /Update by itself/.test(off.reason) && !on.disabled && on.reason === ""; })());
check("tick: shows the source's own state, and a source with update checks off cannot be ticked", P.autoTick(hp("pkg_pip"), true).checked === true && P.autoTick({ can_apply: true, update_checks: false }, true).disabled === true);

// ---- what the panel may write through /api/hub/updates/set
check("set: only the three scopes and their own keys; anything else is not sent",
  JSON.stringify(P.setArgs("global", "", "auto_update", true)) === JSON.stringify({ scope: "global", key: "auto_update", value: true })
  && JSON.stringify(P.setArgs("group", "packages", "included", false)) === JSON.stringify({ scope: "group", id: "packages", key: "included", value: false })
  && JSON.stringify(P.setArgs("provider", "pkg_pip", "update_checks", true)) === JSON.stringify({ scope: "provider", id: "pkg_pip", key: "update_checks", value: true })
  && JSON.stringify(P.setArgs("provider", "pkg_pip", "auto_update", false)) === JSON.stringify({ scope: "provider", id: "pkg_pip", key: "auto_update", value: false }));
check("set: a key the panel has no switch for, a scope it does not know, a group it does not know and a non-boolean are all refused",
  P.setArgs("global", "", "auto_max_per_cycle", true) === null && P.setArgs("provider", "x", "update_interval_h", true) === null && P.setArgs("group", "packages", "update_checks", true) === null
  && P.setArgs("everything", "x", "auto_update", true) === null && P.setArgs("group", "nope", "included", true) === null && P.setArgs("provider", "", "auto_update", true) === null
  && P.setArgs("global", "", "auto_update", "yes") === null && P.setArgs("__proto__", "x", "auto_update", true) === null);

// ---- polling
check("poll: slow when idle, quick while a check runs or was just asked for, quickest while an apply runs, slowest with the panel closed",
  P.pollDelay({ open: true }) === 20000 && P.pollDelay({ open: true, checking: true }) === 4000 && P.pollDelay({ open: true, recent: true }) === 4000
  && P.pollDelay({ open: true, applying: 1 }) === 3000 && P.pollDelay({ open: false }) === 60000 && P.pollDelay({ open: false, applying: 3 }) === 60000 && P.pollDelay() === 60000);
const aged = clone(D); aged.updates.forEach((f) => { f.age_s += 5; });
const moved = clone(D); moved.updates[0].apply = { state: "running", message: "", at: 1 };
check("signature: a few seconds of age do not redraw the list; an apply state, a switch or a group does",
  P.signature(D) === P.signature(aged) && P.signature(D) !== P.signature(moved) && P.signature(D) !== P.signature(noPk) && P.signature(null) === P.signature({}));

// ---- the notices
check("Check now: started, already checking, refused", P.checkNotice({ ok: true, results: [{ provider: "a", status: "requested" }, { provider: "b", status: "requested" }] }).started === 2
  && /Already checking/.test(P.checkNotice({ ok: true, results: [{ status: "already_checking" }] }).text) && P.checkNotice({ ok: true, results: [{ status: "too_soon", message: "Wait a bit." }] }).text === "Wait a bit.");
check("apply notice: how many were queued and why the others were not", (() => {
  const n = P.applyNotice({ ok: true, results: [{ status: "queued" }, { status: "queued" }, { status: "already_running" }, { status: "not_applicable" }, { status: "refused" }] });
  return n.queued === 2 && /Queued 2 updates/.test(n.text) && /1 already running/.test(n.text) && /1 cannot be updated from here/.test(n.text) && /1 refused/.test(n.text);
})());
check("apply notice: nothing queued shows the hub's own sentence, or the error of an answer without results",
  P.applyNotice({ ok: false, results: [{ status: "refused", message: "Not allowed here." }] }).text === "Not allowed here." && P.applyNotice({ ok: false, error: "unattended" }).text === "unattended" && P.applyNotice(null).queued === 0);

// ---- round 10 (DH-D): the layout helpers exist and leave today's list alone (their own tests: tests/verify_updates_panel_layout.js)
check("the layout helpers are exported", ["orderBlocks", "cleanLayout", "readLayout", "writeLayout", "noteSeen", "isCollapsed", "moveRelative", "layoutSignature"].every((k) => typeof P[k] === "function"));
check("with no saved order the list is today's: group, then name (the saved order only moves blocks, blocksOf itself is unchanged)",
  JSON.stringify(P.orderBlocks(blocks, []).map((b) => b.id)) === JSON.stringify(blocks.map((b) => b.id)) && JSON.stringify(P.orderBlocks(blocks, undefined).map((b) => b.id)) === JSON.stringify(["minecraft", "steam_updates", "pkg_choco", "pkg_npm", "pkg_pip"]));
check("a missing, empty or broken storage reads as the default layout and never throws",
  JSON.stringify(P.readLayout(null)) === JSON.stringify(P.newLayout()) && JSON.stringify(P.readLayout({ getItem() { throw new Error("blocked"); } })) === JSON.stringify(P.newLayout()));

// ---- the flows: the ORDER and CONTENT of every write.  A fake hub records every call.
function fakeFlows(opts) {
  const o = opts || {};
  const log = [];
  const calls = { ask: [], say: [] };
  const deps = {
    call: async (route, body) => {
      log.push(["call", route, clone(body)]);
      if (o.failRoute === route) throw new Error(o.failMessage || "the hub said no");
      if (route === "/api/hub/updates/dry-run") return { ok: true, plan: o.plan === undefined ? "winget upgrade --id Foo.Bar --exact" : o.plan };
      if (route === "/api/hub/updates/check") return { ok: true, results: o.checkResults || [{ provider: "a", status: "requested", message: "" }] };
      return { ok: true };
    },
    raw: async (route, body) => {
      log.push(["raw", route, clone(body)]);
      return o.applyAnswer || { ok: true, results: [{ provider: "pkg_pip", id: "Black", status: "queued", message: "" }] };
    },
    ask: async (dialog) => { calls.ask.push(dialog); log.push(["ask", dialog.title]); return o.yes !== false; },
    say: (message, level) => { calls.say.push([message, level]); },
  };
  return { flows: U._flows(deps), log: log, calls: calls };
}
const steps = (log) => log.map((l) => l[0] + ":" + (l[1] || ""));

(async () => {
  // one update: dry run, one dialog with the plan, then the real call
  let t = fakeFlows();
  let r = await t.flows.updateOne(D.updates[3]);
  check("update: a dry run first, then ONE dialog, then the real apply (in that order)",
    JSON.stringify(steps(t.log)) === JSON.stringify(["call:/api/hub/updates/dry-run", "ask:Update Black?", "raw:/api/hub/updates/apply"]), steps(t.log));
  check("update: the dry run and the apply carry the provider and the finding id exactly as given, and nothing else",
    JSON.stringify(t.log[0][2]) === JSON.stringify({ provider: "pkg_pip", id: "Black" }) && JSON.stringify(t.log[2][2]) === JSON.stringify({ provider: "pkg_pip", id: "Black" }));
  check("update: the dialog shows the plan line the dry run returned, and the answer says it was queued", t.calls.ask.length === 1 && t.calls.ask[0].pre === "winget upgrade --id Foo.Bar --exact" && r.ok === true && r.queued === 1 && /Queued 1 update/.test(t.calls.say[0][0]));

  t = fakeFlows({ yes: false });
  r = await t.flows.updateOne(D.updates[3]);
  check("update: a no starts nothing (no apply call at all)", r.declined === true && !t.log.some((l) => l[1] === "/api/hub/updates/apply") && t.calls.ask.length === 1);

  t = fakeFlows({ failRoute: "/api/hub/updates/dry-run", failMessage: "this program cannot say" });
  r = await t.flows.updateOne(D.updates[3]);
  check("update: when the dry run fails nothing is asked and nothing is started; the person is told why",
    r.ok === false && r.stage === "dry_run" && t.calls.ask.length === 0 && !t.log.some((l) => l[1] === "/api/hub/updates/apply") && /this program cannot say/.test(t.calls.say[0][0]) && t.calls.say[0][1] === "warn");

  t = fakeFlows({ plan: "" });
  r = await t.flows.updateOne(D.updates[3]);
  check("update: an empty plan is still shown as empty, in words, before anything runs", t.calls.ask[0].pre === "" && /did not say what it will run/.test(t.calls.ask[0].body) && r.queued === 1);

  // no dialog at all: a yes must come from a person (the real `ask` finds no JarvisUI in this context)
  const log2 = [];
  const bare = U._flows({ call: async (route, body) => { log2.push(route); return { ok: true, plan: "x" }; }, raw: async (route) => { log2.push(route); return { ok: true, results: [] }; }, say: () => {} });
  r = await bare.updateOne(D.updates[3]);
  check("update: with no dialog available nothing runs (only the dry run, which changes nothing)", r.declined === true && JSON.stringify(log2) === JSON.stringify(["/api/hub/updates/dry-run"]), log2);
  r = await bare.switchAuto(true);
  check("the master switch: with no dialog available it stays off and writes nothing", r.declined === true && JSON.stringify(log2) === JSON.stringify(["/api/hub/updates/dry-run"]), log2);

  t = fakeFlows({ applyAnswer: { ok: false, error: "an unattended caller may not apply" } });
  r = await t.flows.updateOne(D.updates[3]);
  check("update: a refused apply is shown as the hub's sentence and counts as not queued", r.ok === false && r.queued === 0 && /unattended/.test(t.calls.say[t.calls.say.length - 1][0]));

  // Update all
  t = fakeFlows();
  r = await t.flows.updateMany(D, { group: "all" }, "");
  check("Update all: ONE dialog (how many, which groups), then ONE apply call with {all:true}",
    JSON.stringify(steps(t.log)) === JSON.stringify(["ask:Update 4 things?", "raw:/api/hub/updates/apply"]) && JSON.stringify(t.log[1][2]) === JSON.stringify({ all: true }), steps(t.log));
  t = fakeFlows();
  await t.flows.updateMany(D, { group: "packages" }, "Packages");
  check("Update all inside a group sends that group; the dialog names the scope", JSON.stringify(t.log[1][2]) === JSON.stringify({ all: true, group: "packages" }) && /\(Packages\)/.test(t.calls.ask[0].body));
  t = fakeFlows({ yes: false });
  r = await t.flows.updateMany(D, { group: "all" }, "");
  check("Update all: a no starts nothing", r.declined === true && !t.log.some((l) => l[0] === "raw"));
  t = fakeFlows();
  r = await t.flows.updateMany(none, { group: "all" }, "");
  check("Update all with nothing to update asks nothing and calls nothing", r.stage === "nothing" && t.log.length === 0 && t.calls.ask.length === 0);
  t = fakeFlows();
  r = await t.flows.updateMany(busyHub, { provider: "pkg_pip" }, "pip");
  check("Update all for a source whose rows are all busy asks nothing and says so", t.log.length === 0 && /already queued or running/.test(t.calls.say[0][0]));

  // automatic updates
  t = fakeFlows();
  r = await t.flows.switchAuto(true);
  check("the master switch ON: the plain dialog first, then the write to the global setting",
    JSON.stringify(steps(t.log)) === JSON.stringify(["ask:Switch on \u201cUpdate by itself\u201d?", "call:/api/hub/updates/set"]) && JSON.stringify(t.log[1][2]) === JSON.stringify({ scope: "global", key: "auto_update", value: true }) && r.ok === true, steps(t.log));
  const body = t.calls.ask[0].body;
  check("the dialog says what it will do, that only sources with their own tick are touched, that failures are not retried, and that it is off by default",
    /starts a source\u2019s own updater by itself/.test(body) && /only touches sources that have their own/.test(body) && /not retried by itself/.test(body) && /off by default/.test(body) && /switch it off here again/.test(body));
  t = fakeFlows({ yes: false });
  r = await t.flows.switchAuto(true);
  check("the master switch: a no leaves it off and writes nothing", r.declined === true && !t.log.some((l) => l[0] === "call"));
  t = fakeFlows();
  r = await t.flows.switchAuto(false);
  check("the master switch OFF needs no dialog", JSON.stringify(steps(t.log)) === JSON.stringify(["call:/api/hub/updates/set"]) && t.log[0][2].value === false && r.ok === true);
  t = fakeFlows();
  r = await t.flows.setSetting("group", "packages", "included", false);
  check("a group switch writes scope group, key included", JSON.stringify(t.log[0][2]) === JSON.stringify({ scope: "group", id: "packages", key: "included", value: false }) && r.ok === true);
  t = fakeFlows();
  r = await t.flows.setSetting("provider", "pkg_pip", "update_interval_h", true);
  check("a setting the panel has no switch for is never sent", r.ok === false && r.stage === "input" && t.log.length === 0);
  t = fakeFlows({ failRoute: "/api/hub/updates/set", failMessage: "setting refused" });
  r = await t.flows.setSetting("provider", "pkg_pip", "auto_update", true);
  check("a refused setting is told in the hub's words", r.ok === false && /setting refused/.test(t.calls.say[0][0]));

  // check and ignore
  t = fakeFlows();
  r = await t.flows.check("");
  check("Check now for everything sends an empty provider; for one source its id", JSON.stringify(t.log[0][2]) === JSON.stringify({ provider: "" }) && r.started === 1);
  t = fakeFlows();
  await t.flows.check("pkg_pip");
  check("Check now for one source carries that id", JSON.stringify(t.log[0][2]) === JSON.stringify({ provider: "pkg_pip" }));
  t = fakeFlows({ failRoute: "/api/hub/updates/check", failMessage: "the hub daemon is not running" });
  r = await t.flows.check("");
  check("Check now with no daemon says what the hub said and starts nothing", r.ok === false && r.started === 0 && /daemon/.test(t.calls.say[0][0]));
  t = fakeFlows();
  r = await t.flows.ignore(D.updates[3]);
  check("Ignore this version asks first, then dismisses exactly that finding",
    JSON.stringify(steps(t.log)) === JSON.stringify(["ask:Ignore this version?", "call:/api/hub/updates/dismiss"]) && JSON.stringify(t.log[1][2]) === JSON.stringify({ provider: "pkg_pip", id: "Black" }) && r.ok === true);
  t = fakeFlows({ yes: false });
  r = await t.flows.ignore(D.updates[3]);
  check("Ignore this version: a no hides nothing", r.declined === true && !t.log.some((l) => l[0] === "call"));

  // hostile text through the flows: it reaches the dialog as plain strings and nothing else
  const evil = finding("pkg_pip", "e1", "<script>alert(1)</script>\u001b[31m" + "A".repeat(500), { installed: "<b>x</b>", available: "\u202eevil" });
  t = fakeFlows({ plan: "<img src=x onerror=alert(1)>\nrm -rf /" + "!".repeat(900) });
  await t.flows.updateOne(evil);
  const dlg = t.calls.ask[0];
  check("hostile text reaches the dialog only as capped plain strings (no control characters, no unbounded length)",
    dlg.title.length <= 140 && !/[\u0000-\u0008\u000b-\u001f\u202a-\u202e]/.test(dlg.title + dlg.body) && dlg.pre.length <= 400 && typeof dlg.pre === "string");
  check("...and the finding's own id is what goes to the hub, never the title", t.log[0][2].id === "e1");

  console.log(pass + " passed, " + fail + " failed");
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log("CRASH: " + (err && err.stack || err)); process.exit(1); });
