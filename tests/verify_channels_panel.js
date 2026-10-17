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

// time-limited tool access (L.36-P6)
check("untilLeft: no deadline is empty", T.untilLeft(0, now) === "");
check("untilLeft: past reads ended", T.untilLeft(now - 5, now) === "ended" && T.untilLeft(now, now) === "ended");
check("untilLeft: seconds, minutes, hours, days", T.untilLeft(now + 30, now) === "in under 2 min" && T.untilLeft(now + 600, now) === "in 10 min" && T.untilLeft(now + 3 * 3600, now) === "in 3 h" && T.untilLeft(now + 2 * 86400, now) === "in 2 d");
check("TIME_LIMITS are whole minutes within 1 minute .. 30 days", T.TIME_LIMITS.length >= 3 && T.TIME_LIMITS.every((t) => Number.isInteger(t.minutes) && t.minutes >= 1 && t.minutes <= 43200 && t.label));

// status dropdown (L.36-P17): options + counts replace the old row of chips
{
  const people = [
    mk({ user_id: "1", owner: true }),
    mk({ user_id: "2", effective: { answered: true, tools: "custom" } }),
    mk({ user_id: "3", reply: { on: false }, effective: { answered: false, tools: "none" } }),
    mk({ user_id: "4", blocked: true, effective: { answered: false, tools: "none" } }),
    mk({ user_id: "5", platform: "instagram", handle: "nour.ig" }),
  ];
  const o = T.stateFilterOptions(people, { search: "", platform: "all" });
  check("dropdown: one option per filter, in the filters' order", o.map((x) => x.id).join() === T.STATE_FILTERS.map((x) => x.id).join());
  const n = (id) => o.find((x) => x.id === id).n;
  check("dropdown: counts - all 5, owner 1, tools 1, answered 3, not answered 1, blocked 1",
    n("all") === 5 && n("owner") === 1 && n("tools") === 1 && n("answered") === 3 && n("silent") === 1 && n("blocked") === 1, JSON.stringify(o.map((x) => [x.id, x.n])));
  const d = T.stateFilterOptions(people, { search: "", platform: "discord" });
  check("dropdown: counts follow the platform filter", d.find((x) => x.id === "all").n === 4 && d.find((x) => x.id === "answered").n === 2);
  const q = T.stateFilterOptions(people, { search: "nour", platform: "all" });
  check("dropdown: counts follow the search box", q.find((x) => x.id === "all").n === 1);
  check("dropdown: an empty list is all zeros, no throw", T.stateFilterOptions([], { search: "", platform: "all" }).every((x) => x.n === 0));
  check("dropdown: every option keeps its label", o.every((x) => typeof x.label === "string" && x.label.length));
}

// allowed_guilds editor helpers (L.36-P17)
{
  const v = {
    allowed_guilds: ["900000000000000001"],
    servers: [
      { id: "900000000000000001", name: "Alpha", known: true, left: false },
      { id: "900000000000000002", name: "Beta", known: true, left: false },
      { id: "900000000000000003", name: "", known: true, left: false },
      { id: "900000000000000004", name: "Gone", known: true, left: true },
      { id: "900000000000000005", name: "", known: false, left: false },
    ],
  };
  check("guildIdOk: digits only, 1..25", T.guildIdOk("123") && T.guildIdOk(" 900000000000000001 ") && !T.guildIdOk("") && !T.guildIdOk("12a") && !T.guildIdOk("-1") && !T.guildIdOk("1".repeat(26)) && !T.guildIdOk(null));
  check("guildName: finds a name, empty for none / unknown", T.guildName(v, "900000000000000001") === "Alpha" && T.guildName(v, "900000000000000003") === "" && T.guildName(v, "7") === "" && T.guildName(null, "1") === "");
  const c = T.guildChoices(v).map((x) => x.id);
  check("guildChoices: seen, still joined and not already allowed", c.join() === "900000000000000002,900000000000000003", c.join());
  check("guildChoices: nothing seen yet is an empty list, no throw", T.guildChoices({ servers: [], allowed_guilds: [] }).length === 0 && T.guildChoices(null).length === 0);
  check("guildPick: typed beats picked", T.guildPick("111", "222") === "111");
  check("guildPick: a bad typed id is NOT silently replaced by the dropdown", T.guildPick("abc", "222") === "");
  check("guildPick: falls back to the dropdown, then to nothing", T.guildPick("", "222") === "222" && T.guildPick("", "") === "" && T.guildPick("  ", "x") === "");
}

// allowed_channels helpers (L.36-P18) -- the twins of the guild helpers above.
{
  const v = {
    allowed_channels: ["800000000000000001", "800000000000000077"],
    servers: [
      { id: "900000000000000001", name: "Alpha", known: true, left: false, channels: [
        { id: "800000000000000001", name: "general" }, { id: "800000000000000002", name: "random" }] },
      { id: "900000000000000002", name: "Beta", known: true, left: false, channels: [{ id: "800000000000000003", name: "" }] },
      { id: "900000000000000003", name: "Gone", known: true, left: true, channels: [{ id: "800000000000000004", name: "old" }] },
      { id: "900000000000000004", name: "", known: false, left: false, channels: [{ id: "800000000000000005", name: "ghost" }] },
    ],
  };
  check("channelIdOk is the same digits-only rule as guildIdOk", T.channelIdOk("123") && !T.channelIdOk("12a") && !T.channelIdOk("") && !T.channelIdOk("1".repeat(26)));
  const i = T.channelInfo(v, "800000000000000001");
  check("channelInfo: channel and server name", i.name === "general" && i.guild === "Alpha", JSON.stringify(i));
  const u = T.channelInfo(v, "800000000000000077");
  check("channelInfo: an id nobody has seen is empty, no throw", u.name === "" && u.guild === "" && T.channelInfo(null, "1").name === "");
  check("channelLabel: #name in Server . id", T.channelLabel(v, "800000000000000001") === "#general in Alpha \u00b7 800000000000000001", T.channelLabel(v, "800000000000000001"));
  check("channelLabel: falls back to the bare id", T.channelLabel(v, "800000000000000077") === "800000000000000077");
  const g = T.channelChoices(v);
  check("channelChoices: grouped by server, minus allowed, left and unseen servers",
    g.length === 2 && g[0].name === "Alpha" && g[0].channels.map((c) => c.id).join() === "800000000000000002"
    && g[1].name === "Beta" && g[1].channels.map((c) => c.id).join() === "800000000000000003", JSON.stringify(g));
  check("channelChoices: a server with nothing left to pick is dropped",
    T.channelChoices({ allowed_channels: ["1"], servers: [{ id: "9", name: "S", known: true, left: false, channels: [{ id: "1", name: "a" }] }] }).length === 0);
  check("channelChoices: nothing seen yet is an empty list, no throw", T.channelChoices({ servers: [], allowed_channels: [] }).length === 0 && T.channelChoices(null).length === 0);
  check("channelPick: typed beats picked, a bad typed id is not replaced", T.channelPick("111", "222") === "111" && T.channelPick("abc", "222") === "" && T.channelPick("", "222") === "222" && T.channelPick("", "") === "");
}

// Home (the old side panel, picked like a person)
{
  check("HOME_KEY can never be a person's key (those always contain a colon)", T.HOME_KEY === "home" && !T.HOME_KEY.includes(":"));
  const ids = (pl) => T.homeTabs(pl).map((t) => t.id).join();
  check("homeTabs: with Discord there is a Servers tab", ids({ discord: {}, instagram: {} }) === "platforms,servers,denied,lists", ids({ discord: {}, instagram: {} }));
  check("homeTabs: without Discord the Servers tab is not offered", ids({ instagram: {} }) === "platforms,denied,lists", ids({ instagram: {} }));
  check("homeTabs: nothing loaded yet is still a usable tab set, no throw", ids({}) === "platforms,denied,lists" && ids(null) === "platforms,denied,lists");
  check("homeTabs: every tab has a label and an icon name", T.homeTabs({ discord: {} }).every((t) => t.label && t.ico));
  check("platformState: no token is bad, whatever 'enabled' says", T.platformState({ token_set: false, enabled: true }).ch === "bad" && T.platformState(null).ch === "bad");
  check("platformState: a token but switched off is off", T.platformState({ token_set: true, enabled: false }).ch === "off");
  check("platformState: token and enabled is on", T.platformState({ token_set: true, enabled: true }).ch === "on");
}

// The per-channel "Allowed channel" switch (a server's Permissions tab) and the pinned cards
// (Home / Discord / Instagram) folding away while the list scrolls down.
{
  const srv = { id: "9", left: false }, c = { id: "800000000000000001" };
  const row = (list, s) => T.channelAllowedRow({ allowed_channels: list }, s || srv, c);
  const none = row([]);
  check("channelAllowedRow: no filter -> off, usable, and says the filter is off",
    none.on === false && none.locked === "" && none.filter === false && /No filter is set/.test(none.hint), JSON.stringify(none));
  const listed = row([c.id, "800000000000000002"]);
  check("channelAllowedRow: on the list with others -> on and usable", listed.on === true && listed.locked === "" && listed.filter === true && /On the allowed list/.test(listed.hint), JSON.stringify(listed));
  const only = row([c.id]);
  check("channelAllowedRow: the only entry is locked (removing it would open every channel)", only.on === true && /only channel on the list/.test(only.locked), JSON.stringify(only));
  const out = row(["800000000000000002"]);
  check("channelAllowedRow: a filter that leaves it out says it is ignored, and it can be turned on", out.on === false && out.locked === "" && /ignores this channel/.test(out.hint), JSON.stringify(out));
  check("channelAllowedRow: a server Jarvis left can't be switched on, but a listed channel there can be switched off",
    row([], { left: true }).locked !== "" && row([c.id, "5"], { left: true }).locked === "");
  check("channelAllowedRow: a missing view or channel is an empty filter, no throw",
    T.channelAllowedRow(null, null, c).on === false && T.channelAllowedRow(undefined, undefined, {}).on === false && T.channelAllowedRow({ allowed_channels: "x" }, srv, c).on === false);

  const P = T.pinnedNext, D = T.PIN_DELTA;
  check("pinnedNext: at the top the cards are shown, whatever came before", P({ hidden: true, anchor: 300 }, 0, 500).hidden === false && P({ hidden: true, anchor: 300 }, T.PIN_TOP, 500).hidden === false && P(null, 5, 100).hidden === false);
  check("pinnedNext: scrolling down by the threshold hides them, and moves the anchor", (() => { const r = P({ hidden: false, anchor: 40 }, 40 + D, 100); return r.hidden === true && r.anchor === 40 + D; })());
  check("pinnedNext: ...but not when hiding would make the browser clamp the position (no room)", P({ hidden: false, anchor: 40 }, 40 + D, -1).hidden === false);
  check("pinnedNext: once hidden they stay hidden while the list keeps going down, even with no room", P({ hidden: true, anchor: 40 }, 40 + D, -1).hidden === true);
  check("pinnedNext: scrolling up by the threshold shows them", P({ hidden: true, anchor: 200 }, 200 - D, 100).hidden === false);
  check("pinnedNext: small moves change nothing and keep the anchor, so they add up",
    (() => { const a = P({ hidden: false, anchor: 100 }, 105, 100), b = P({ hidden: true, anchor: 100 }, 95, 100); return a.hidden === false && a.anchor === 100 && b.hidden === true && b.anchor === 100; })());
  check("pinnedNext: small steps in one direction eventually cross the threshold", (() => { let st = { hidden: false, anchor: 100 }; for (let t = 103; t <= 130; t += 3) st = P(st, t, 100); return st.hidden === true; })());

  const css = fs.readFileSync(path.join(__dirname, "..", "web", "public", "channels.css"), "utf8");
  check("the pinned slot folds with a transition and respects reduced motion", /\.ch-home-slot\.is-collapsed\s*\{/.test(css) && /prefers-reduced-motion[^{]*\{[^}]*\.ch-home-slot/.test(css));
  check("a folded slot is out of the tab order (inert) and the panel listens to the list's scroll", /toggleAttribute\("inert"/.test(src) && /dom\.list\.addEventListener\("scroll", onListScroll/.test(src));
  check("the per-channel switch goes through the same allowed-channels door as the Discord page (preview first)", /chanMutate\(c\.id, !turnOn, false\)/.test(src) && /chanConfirmNode\(state\.chanAdd\)/.test(src));
}

// Home > Global lists: names for the entries, who can still be picked, several ids at once.
{
  const people = [
    mk({ platform: "discord", user_id: "111", handle: "ann", name: "Ann" }),
    mk({ platform: "discord", user_id: "222", handle: "bob", name: "Bob" }),
    mk({ platform: "discord", user_id: "333", handle: "own", name: "Owner", owner: true }),
    mk({ platform: "discord", user_id: "444", handle: "bad", name: "Bad", blocked: true }),
    mk({ platform: "instagram", user_id: "111", handle: "ann_ig", name: "Ann IG" }),
    mk({ platform: "discord", user_id: "", handle: "later", name: "Later", placeholder: true }),
  ];
  check("listPerson: an id or a handle (any case, with or without @) finds the person on THAT platform",
    T.listPerson(people, "discord", "111").name === "Ann" && T.listPerson(people, "discord", "@ANN").user_id === "111" && T.listPerson(people, "instagram", "111").name === "Ann IG");
  check("listPerson: a handle-only person is found by handle", T.listPerson(people, "discord", "later").name === "Later");
  check("listPerson: * , an empty entry, a stranger and no people are all nobody, no throw",
    T.listPerson(people, "discord", "*") === null && T.listPerson(people, "discord", "") === null && T.listPerson(people, "discord", "999") === null && T.listPerson(null, "discord", "1") === null);
  const order = (entries, q) => T.listChips(people, "discord", entries, q).map((c) => c.entry).join();
  check("listChips: * first, then people the panel knows by name, then bare ids", order(["999", "222", "*", "111"], "") === "*,111,222,999", order(["999", "222", "*", "111"], ""));
  check("listChips: the filter matches entry, name and handle", order(["111", "222", "999"], "bob") === "222" && order(["111", "222", "999"], "ANN") === "111" && order(["111", "222", "999"], "99") === "999" && order(["111"], "zzz") === "");
  check("listChips: a wildcard is flagged, the owner is flagged, a list that isn't an array is empty",
    T.listChips(people, "discord", ["*"], "")[0].wildcard === true && T.listChips(people, "discord", ["333"], "")[0].owner === true && T.listChips(people, "discord", null, "").length === 0);
  const sug = (entries) => T.listSuggestions(people, "discord", entries).map((x) => x.value).join();
  check("listSuggestions: known people not on the list, by name; never the owner, a blocked person or another platform", sug(["111"]) === "222,later", sug(["111"]));
  check("listSuggestions: someone listed by handle (or with @) is not offered again", sug(["@bob"]) === "111,later" && sug(["111", "222", "later"]) === "");
  check("listSuggestions: a handle-only person is offered by handle, with their name", T.listSuggestions(people, "discord", [])[2].value === "later" && /Later/.test(T.listSuggestions(people, "discord", [])[2].label));
  check("splitEntries: spaces, commas, semicolons and new lines separate; repeats and blanks go", JSON.stringify(T.splitEntries("1, 2;3\n 2  @x,,")) === JSON.stringify(["1", "2", "3", "@x"]) && T.splitEntries("").length === 0 && T.splitEntries(null).length === 0);
  check("LIST_DEFS: the four lists, with the config key each one edits",
    T.LIST_DEFS.map((d) => d.short + ":" + d.key).join() === "dm:dm_allowlist,reply:reply_allowlist,tool:tool_allowlist,image:image_allowlist" && T.LIST_DEFS.every((d) => d.label && d.hint && d.everyone));
  check("the only way \"*\" is added is behind the second click", (src.match(/listAdd\(platform, def\.short, \["\*"\]\)/g) || []).length === 1 && /confirmBox\(`Confirm everyone on the/.test(src));
}

console.log(`${passed} passed, ${failed.length} failed`);
process.exit(failed.length ? 1 : 0);
