// Pure logic of web/public/channels-panel.js (L.36), loaded whole into a bare
// `window` with vm, the way verify_daemons_panel.js does it. No npm install.
//   node tests/verify_channels_panel.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "public", "channels-panel.js"), "utf8");
const win = {}; win.window = win;
vm.runInNewContext(src, win, { filename: "channels-panel.js" });
const T = win.JarvisChannels && win.JarvisChannels._pure;
let passed = 0; const failed = [];
function check(name, cond, detail) { if (cond) passed++; else failed.push(name + (detail ? " - " + detail : "")); }
check("module exposes open/close/_pure", !!T && typeof win.JarvisChannels.open === "function");

const mk = (o) => Object.assign({ platform: "discord", user_id: "1", handle: "", name: "", owner: false, blocked: false,
  reply: { on: true }, dm: { on: true }, tool: { on: false },
  tools: { mode: "inherit", allow: [] }, effective: { answered: true, tools: "none" } }, o);

// initials / hue
check("initials from two words", T.initials(mk({ name: "Maryem Khaled" })) === "MK");
check("initials from one word", T.initials(mk({ name: "Tarek" })) === "TA");
check("initials from handle", T.initials(mk({ handle: "nour.ig" })) === "NI");
check("initials never empty", T.initials(mk({ user_id: "" })) .length > 0);
check("hue is stable", T.hueFor("discord:1") === T.hueFor("discord:1"));
check("hue in range", [..."abcdefghij"].every((c) => { const h = T.hueFor(c + "x"); return h >= 0 && h < 360; }));

// relTime
const now = 1_000_000;
check("just now", T.relTime(now - 5, now) === "just now");
check("minutes", T.relTime(now - 600, now) === "10m ago");
check("hours", T.relTime(now - 3 * 3600, now) === "3h ago");
check("days", T.relTime(now - 2 * 86400, now) === "2d ago");
check("never", T.relTime(0, now) === "never");
check("future timestamp does not go negative", T.relTime(now + 500, now) === "just now");

// posture drives the card rail
check("blocked is bad", T.postureOf(mk({ blocked: true })) === "bad");
check("owner is owner", T.postureOf(mk({ owner: true })) === "owner");
check("not answered is off", T.postureOf(mk({ reply: { on: false }, effective: { answered: false, tools: "none" } })) === "off");
check("every-tool guest is amber", T.postureOf(mk({ effective: { answered: true, tools: "all" } })) === "limited");
check("plain answered guest is on", T.postureOf(mk()) === "on");

// filtering
const people = [
  mk({ user_id: "1", name: "Khalil", handle: "khalil", owner: true, effective: { answered: true, tools: "all" } }),
  mk({ user_id: "2", name: "Maryem", handle: "maryem_k", effective: { answered: true, tools: "custom" } }),
  mk({ user_id: "3", name: "", handle: "stranger", reply: { on: false }, effective: { answered: false, tools: "none" } }),
  mk({ user_id: "4", name: "Spam", blocked: true, effective: { answered: false, tools: "none" } }),
  mk({ platform: "instagram", user_id: "17841", name: "Nour", effective: { answered: true, tools: "none" } }),
];
const f = (o) => T.filterPeople(people, Object.assign({ search: "", platform: "all", state: "all" }, o)).map((p) => p.user_id);
check("no filter returns everyone", f({}).length === 5);
check("platform filter", f({ platform: "instagram" }).join() === "17841");
check("owner filter", f({ state: "owner" }).join() === "1");
check("tools filter = anyone with tools", f({ state: "tools" }).join() === "1,2");
check("blocked filter", f({ state: "blocked" }).join() === "4");
check("silent excludes blocked", f({ state: "silent" }).join() === "3");
check("answered excludes blocked", !f({ state: "answered" }).includes("4"));
check("search by name, case-insensitive", f({ search: "MARY" }).join() === "2");
check("search by @handle with the @", f({ search: "@stranger" }).join() === "3");
check("search by id", f({ search: "17841" }).join() === "17841");
check("combined filters AND together", f({ platform: "discord", state: "tools", search: "khal" }).join() === "1");
check("unknown state falls back to all", f({ state: "nonsense" }).length === 5);

// draft comparison
const A = (mode, names) => ({ mode, names: new Set(names) });
check("same custom set is equal regardless of order", T.sameScope(A("custom", ["a", "b"]), A("custom", ["b", "a"])));
check("different custom sets differ", !T.sameScope(A("custom", ["a"]), A("custom", ["a", "b"])));
check("inherit ignores stale names", T.sameScope(A("inherit", ["a"]), A("inherit", [])));
check("mode change is a difference", !T.sameScope(A("inherit", []), A("custom", [])));
check("scopeOf reads the stored shape", T.scopeOf(mk({ tools: { mode: "custom", allow: ["x"] } })).names.has("x"));

// tool grouping
const tools = [
  { name: "web_search", description: "Search the web", group: "web" },
  { name: "get_datetime", description: "Local time", group: "core" },
  { name: "fetch_url", description: "Read a page", group: "web" },
];
const g = T.groupTools(tools, "");
check("groups sorted by id", g.map((x) => x.id).join() === "core,web");
check("tools sorted within a group", g[1].items.map((t) => t.name).join() === "fetch_url,web_search");
check("search matches name", T.groupTools(tools, "datetime").length === 1);
check("search matches description", T.groupTools(tools, "page")[0].items[0].name === "fetch_url");
check("search matches group", T.groupTools(tools, "core")[0].items[0].name === "get_datetime");
check("no match -> no groups", T.groupTools(tools, "zzz").length === 0);

check("displayName prefers name, then @handle, then id",
  T.displayName(mk({ name: "N", handle: "h" })) === "N" && T.displayName(mk({ handle: "h" })) === "@h" && T.displayName(mk({ user_id: "42" })) === "42");

// L.36b: linked accounts, hand-added people
check("displayName uses the linked account's name when they have none",
  T.displayName(mk({ name: "", name_effective: "Sam", handle: "sam_ig" })) === "Sam");
check("own name beats the linked name", T.displayName(mk({ name: "Own", name_effective: "Own" })) === "Own");
check("initials follow the effective name", T.initials(mk({ name: "", name_effective: "Maryem Khaled", handle: "x" })) === "MK");
const linkedPeople = [
  mk({ user_id: "7", handle: "d_sam", name_effective: "Sam", linked: { platform: "instagram", user_id: "9", handle: "sam_on_ig", name: "" } }),
  mk({ user_id: "8", handle: "other" }),
];
const fl = (q) => T.filterPeople(linkedPeople, { search: q, platform: "all", state: "all" }).map((p) => p.user_id);
check("search finds someone by their linked account's handle", fl("@sam_on_ig").join() === "7");
check("search finds someone by an inherited name", fl("sam").join() === "7");
check("a handle-only person with no id shows their handle",
  T.displayName(mk({ user_id: "bobby", handle: "bobby", placeholder: true })) === "@bobby");

// the panel script must not use innerHTML for data
const dataInner = src.split("\n").filter((l) => /innerHTML/.test(l) && !/^\s*(\/\/|\*)/.test(l));
check("innerHTML only parses the fixed icon strings", dataInner.length === 1 && /t\.innerHTML = ICON\[name\]/.test(dataInner[0]), dataInner.join(" | "));

for (const m of failed) console.log("FAILED:", m);
// ---- L.36-P1/P2/P3: helpers behind the Conversation / Usage / Test tabs
check("dayOf reads the local ISO stamp", T.dayOf("2026-10-01T10:00:05") === "2026-10-01");
check("dayOf tolerates junk", T.dayOf("") === "" && T.dayOf(null) === "" && T.dayOf("yesterday") === "");
check("clockOf is HH:MM", T.clockOf("2026-10-01T10:00:05") === "10:00" && T.clockOf("2026-10-01") === "");
check("dayLabel: today / yesterday / unknown", T.dayLabel("2026-10-04", "2026-10-04") === "Today" && T.dayLabel("2026-10-03", "2026-10-04") === "Yesterday" && T.dayLabel("", "2026-10-04") === "Earlier");
check("dayLabel crosses a month boundary", T.dayLabel("2026-09-30", "2026-10-01") === "Yesterday");
const grouped = T.groupByDay([{ at: "2026-10-01T09:00:00" }, { at: "2026-10-01T10:00:00" }, { at: "2026-10-02T08:00:00" }, {}]);
check("groupByDay keeps order and splits on the day", grouped.length === 3 && grouped[0].entries.length === 2 && grouped[1].day === "2026-10-02" && grouped[2].day === "");
check("groupByDay of nothing is nothing", T.groupByDay([]).length === 0 && T.groupByDay(null).length === 0);
check("fmtTokens small / k / M", T.fmtTokens(0) === "0" && T.fmtTokens(999) === "999" && T.fmtTokens(1200) === "1.2k" && T.fmtTokens(17000) === "17k" && T.fmtTokens(2500000) === "2.5M");
check("fmtTokens never prints NaN", T.fmtTokens(undefined) === "0" && T.fmtTokens("x") === "0");
const bm = T.barModel([{ date: "a", total_tokens: 0, asks: 0 }, { date: "b", total_tokens: 1, asks: 1 }, { date: "c", total_tokens: 1000, asks: 2 }]);
check("barModel: empty day is 0, tiny day is still visible, biggest is 100", bm[0].pct === 0 && bm[1].pct >= 6 && bm[2].pct === 100);
check("barModel: all-zero series stays flat, no divide by zero", T.barModel([{ date: "a", total_tokens: 0 }]).every((b) => b.pct === 0) && T.barModel(null).length === 0);
check("convNote wording", T.convNote({ total: 0 }) === "No messages logged for them yet." && T.convNote({ total: 1, shown: 1 }) === "1 message." && T.convNote({ total: 9, shown: 5 }) === "Showing the latest 5 of 9 messages.");

console.log(`${passed} passed, ${failed.length} failed`);
process.exit(failed.length ? 1 : 0);
