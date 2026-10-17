// Channel spaces S11: the shared-room parts of the Channels panel and the Ask thread's speaker chip.
// Pure logic of web/public/channels-panel.js and web/public/speaker-chip.js, loaded whole into a bare
// `window` with vm (the way verify_channels_panel.js does it), plus static checks that the panel only
// talks to routes web/server.js really has and never turns a stranger's text into markup.
//   node tests/verify_channels_rooms.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const PUB = path.join(__dirname, "..", "web", "public");
const read = (...p) => fs.readFileSync(path.join(__dirname, "..", ...p), "utf8");
const panelSrc = fs.readFileSync(path.join(PUB, "channels-panel.js"), "utf8");
const chipSrc = fs.readFileSync(path.join(PUB, "speaker-chip.js"), "utf8");
const win = {}; win.window = win;
vm.runInNewContext(chipSrc, win, { filename: "speaker-chip.js" });
vm.runInNewContext(panelSrc, win, { filename: "channels-panel.js" });
const T = win.JarvisChannels && win.JarvisChannels._pure;
const S = win.JarvisSpeaker;
let passed = 0; const failed = [];
function check(name, cond, detail) { if (cond) passed++; else failed.push(name + (detail ? " - " + detail : "")); }
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

check("panel exposes the S11 helpers", !!T && ["groupFlagOf", "usedInRooms", "ambientOf", "roomSwitchState", "filterRooms", "roomSpeaker", "homeTabs"].every((n) => typeof T[n] === "function"));
check("speaker-chip exposes label and chip", !!S && typeof S.label === "function" && typeof S.chip === "function");

// ---- speaker label: the same rules as channels/space.py speaker_label ----
check("name wins", S.label({ name: "Ann", handle: "ann_x", id: "123456" }) === "Ann");
check("handle gets an @, and a leading @ is not doubled", S.label({ handle: "ann_x" }) === "@ann_x" && S.label({ handle: "@@ann_x" }) === "@ann_x");
check("id becomes user…last4", S.label({ id: "9876543210" }) === "user\u20263210");
check("an empty-but-present speaker is someone", S.label({ name: "  ", handle: "", id: "" }) === "someone");
check("owner gets a suffix", S.label({ name: "Khalil", owner: true }) === "Khalil (owner)");
check("no speaker, no label", S.label(null) === "" && S.label(undefined) === "" && S.label({}) === "" && S.label([]) === "" && S.label("Ann") === "");
const long = S.label({ name: "x".repeat(200), owner: true });
check("a long name is capped INCLUDING the owner suffix", long.length === S.MAX_LABEL_CHARS && long.endsWith(" (owner)"), String(long.length));
check("whitespace in a name is flattened", S.label({ name: "Ann\n  Lee\t" }) === "Ann Lee");
const made = [];
const fakeDoc = { createElement: (tag) => { const n = { tag, className: "", textContent: "", title: "" }; made.push(n); return n; } };
const chip = S.chip({ name: "<img src=x onerror=alert(1)>", owner: false }, fakeDoc);
check("the chip carries the name as text, never markup", chip && chip.textContent === "<img src=x onerror=alert(1)>" && chip.innerHTML === undefined && chip.className === "speaker-chip");
check("an owner chip is marked", S.chip({ name: "K", owner: true }, fakeDoc).className === "speaker-chip speaker-chip--owner");
check("no speaker, no chip", S.chip(null, fakeDoc) === null && S.chip({}, fakeDoc) === null);

// ---- "In rooms" per item: the same rule as people.recall_allowed ----
check("flags: only inherit/on/off, anything else is inherit", T.groupFlagOf({ groups: "on" }) === "on" && T.groupFlagOf({ groups: "off" }) === "off"
  && T.groupFlagOf({ groups: "yes" }) === "inherit" && T.groupFlagOf({}) === "inherit" && T.groupFlagOf(null) === "inherit");
check("the three flags, in order", same(T.GROUP_FLAGS, ["inherit", "on", "off"]));
check("off always wins", T.usedInRooms("off", true) === false && T.usedInRooms("off", false) === false);
check("on wins over a master that is off", T.usedInRooms("on", false) === true && T.usedInRooms("on", true) === true);
check("inherit follows the master, and only a literal true", T.usedInRooms("inherit", true) === true && T.usedInRooms("inherit", false) === false
  && T.usedInRooms("inherit", undefined) === false && T.usedInRooms("inherit", "true") === false);
check("recallOn reads only the literal true", T.recallOn({ discord: { group_recall: true } }, "discord") === true
  && T.recallOn({ discord: { group_recall: "true" } }, "discord") === false && T.recallOn({ discord: {} }, "discord") === false
  && T.recallOn({}, "discord") === false && T.recallOn(null, "discord") === false);
check("the explanation line says what happens", /Never/.test(T.groupUseText("off", true)) && /whatever the master/.test(T.groupUseText("on", false))
  && /which is on: used/.test(T.groupUseText("inherit", true)) && /which is off: not used/.test(T.groupUseText("inherit", false)));

// ---- reading other messages: the same reading as config.ambient_limits ----
check("defaults", same(T.ambientOf({}, "discord"), { messages: 20, minutes: 30, senders: "approved" }) && same(T.AMBIENT_DEFAULTS, { messages: 20, minutes: 30, senders: "approved" }));
check("saved values are read", same(T.ambientOf({ discord: { ambient_messages: 5, ambient_minutes: 90, ambient_senders: "anyone" } }, "discord"), { messages: 5, minutes: 90, senders: "anyone" }));
check("0 messages is a real value (off)", T.ambientOf({ discord: { ambient_messages: 0 } }, "discord").messages === 0);
check("out-of-range or non-integer values fall back to the defaults", T.ambientOf({ discord: { ambient_messages: 101, ambient_minutes: 0 } }, "discord").messages === 20
  && T.ambientOf({ discord: { ambient_messages: "7", ambient_minutes: 1441 } }, "discord").minutes === 30 && T.ambientOf({ discord: { ambient_messages: 2.5 } }, "discord").messages === 20);
check("an unreadable senders value is the NARROWER one", T.ambientOf({ discord: { ambient_senders: "everyone" } }, "discord").senders === "approved"
  && T.ambientOf({ discord: { ambient_senders: "ANYONE" } }, "discord").senders === "anyone");
check("off reads as off, on says what is kept", /^Off/.test(T.ambientText({ messages: 0, minutes: 30, senders: "approved" }))
  && /last 20 messages/.test(T.ambientText({ messages: 20, minutes: 30, senders: "approved" })) && /approved/.test(T.ambientText({ messages: 20, minutes: 30, senders: "approved" }))
  && /anyone in the room/.test(T.ambientText({ messages: 1, minutes: 30, senders: "anyone" })) && /last 1 message of/.test(T.ambientText({ messages: 1, minutes: 30, senders: "anyone" })));
check("saving is on unless the config says otherwise", T.savingOn({}, "discord") === true && T.savingOn({ discord: {} }, "discord") === true
  && T.savingOn({ discord: { log_conversations: false } }, "discord") === false && T.savingOn({ discord: { log_conversations: true } }, "discord") === true);

// ---- the rooms list ----
const rows = [
  { platform: "discord", thread_id: "C1", kind: "server_channel", title: "#general in Guild", saved: true, switches: { log: "inherit", ambient: "inherit" } },
  { platform: "discord", thread_id: "T9", kind: "server_thread", title: "Plans", saved: true, switches: { log: "off", ambient: "inherit" } },
  { platform: "instagram", thread_id: "ig-live", kind: "group", title: "Cousins", saved: true },
  { platform: "instagram", thread_id: "import-aaaa1111", kind: "group", title: "Group 'Old Crew'", imported: true, saved: true },
];
check("roomsOf is safe", same(T.roomsOf(null), []) && same(T.roomsOf({}), []) && T.roomsOf({ spaces: rows }).length === 4);
check("keys and titles", T.roomKey(rows[0]) === "discord|C1" && T.roomTitle(rows[0]) === "#general in Guild" && T.roomTitle({ thread_id: "x" }) === "x");
check("kind labels", T.roomKindLabel("server_channel") === "Channel" && T.roomKindLabel("server_thread") === "Thread" && T.roomKindLabel("group") === "Group chat" && T.roomKindLabel("???") === "Room");
const ids = (list) => list.map((r) => r.thread_id);
check("filter: servers", same(ids(T.filterRooms(rows, { kind: "server" })), ["C1", "T9"]));
check("filter: groups", same(ids(T.filterRooms(rows, { kind: "group" })), ["ig-live", "import-aaaa1111"]));
check("filter: platform", same(ids(T.filterRooms(rows, { platform: "instagram" })), ["ig-live", "import-aaaa1111"]) && T.filterRooms(rows, { platform: "all" }).length === 4);
check("filter: search matches title or id, any case", same(ids(T.filterRooms(rows, { search: "OLD crew" })), ["import-aaaa1111"]) && same(ids(T.filterRooms(rows, { search: "t9" })), ["T9"]) && T.filterRooms(rows, { search: "nope" }).length === 0);
check("filter with no options keeps everything", T.filterRooms(rows).length === 4 && T.filterRooms(null).length === 0);
check("a Discord channel's room is found by its id", T.roomForChannel(rows, "discord", "C1") === rows[0] && T.roomForChannel(rows, "discord", "C2") === null && T.roomForChannel(rows, "instagram", "C1") === null);
check("a room's own switch reads off only when it says off", T.roomSwitchOf(rows[1], "log") === "off" && T.roomSwitchOf(rows[1], "ambient") === "inherit" && T.roomSwitchOf(rows[2], "log") === "inherit" && T.roomSwitchOf(null, "log") === "inherit");

// ---- room switch state: on / off, and WHY it cannot be turned on ----
const cfg = { discord: { log_conversations: true, ambient_messages: 20 } };
const view = { servers: [{ id: "G1", channels: [{ id: "C1" }], override: { log: "off", ambient: "inherit" } }] };
check("a plain room is on", same(T.roomSwitchState(rows[2], "log", cfg, null), { on: true, lock: "" }));
check("a room switched off itself can be turned back on (no lock)", same(T.roomSwitchState(rows[1], "log", cfg, null), { on: false, lock: "" }));
const platOff = T.roomSwitchState(rows[0], "log", { discord: { log_conversations: false } }, null);
check("platform saving off locks the switch and names the setting", platOff.on === false && /log_conversations/.test(platOff.lock) && /Discord/.test(platOff.lock), platOff.lock);
const readOff = T.roomSwitchState(rows[0], "ambient", { discord: { ambient_messages: 0 } }, null);
check("platform reading off locks the switch and names the setting", readOff.on === false && /ambient_messages/.test(readOff.lock), readOff.lock);
const srvOff = T.roomSwitchState(rows[0], "log", cfg, view);
check("a server that has it off locks its channel", srvOff.on === false && /whole server/.test(srvOff.lock), srvOff.lock);
check("...but only for the switch the server turned off", same(T.roomSwitchState(rows[0], "ambient", cfg, view), { on: true, lock: "" }));
check("a group chat is never affected by a Discord server", same(T.roomSwitchState(rows[2], "log", cfg, view), { on: true, lock: "" }));
check("serverSwitchOff", T.serverSwitchOff(view, "C1", "log") === true && T.serverSwitchOff(view, "C1", "ambient") === false && T.serverSwitchOff(view, "C9", "log") === false && T.serverSwitchOff(null, "C1", "log") === false);

// ---- who wrote a line ----
check("speaker: name, else @handle, else user…last4, else someone", T.roomSpeaker({ dir: "in", author_name: "Ann" }) === "Ann" && T.roomSpeaker({ dir: "in", user_handle: "ann_x" }) === "@ann_x"
  && T.roomSpeaker({ dir: "in", user_id: "name:Ann Lee" }) === "user\u2026 Lee" && T.roomSpeaker({ dir: "in", user_id: "123456789" }) === "user\u20266789" && T.roomSpeaker({ dir: "in" }) === "someone");
check("Jarvis's own lines say Jarvis", T.roomSpeaker({ dir: "out", text: "hi" }) === "Jarvis" && T.roomSpeaker(null) === "Jarvis");
check("the panel's label and the shared chip agree", T.roomSpeaker({ dir: "in", user_handle: "ann_x", user_id: "42" }) === S.label({ handle: "ann_x", id: "42" }));

// ---- home tabs ----
const tabIds = (defs) => defs.map((d) => d.id);
check("no Rooms tab without rooms (and the old one-argument call is unchanged)", same(tabIds(T.homeTabs({ discord: {} })), ["platforms", "servers", "denied", "lists"]) && same(tabIds(T.homeTabs({ discord: {} }, { rooms: 0 })), ["platforms", "servers", "denied", "lists"]));
check("Rooms sits after Servers once rooms exist", same(tabIds(T.homeTabs({ discord: {} }, { rooms: 3 })), ["platforms", "servers", "rooms", "denied", "lists"]));
check("an Instagram-only setup still gets Rooms", same(tabIds(T.homeTabs({ instagram: {} }, { rooms: 1 })), ["platforms", "rooms", "denied", "lists"]));

// ---- static: the panel talks only to routes the server has, and never makes markup from text ----
const server = read("web", "server.js");
const routeUsed = (needle) => panelSrc.includes(needle);
check("it posts room settings to the room-setting route", routeUsed("/room-setting") && server.includes('"/api/channels/platform/:platform/room-setting"'));
check("it posts room switches to the spaces route", routeUsed("/api/channels/spaces/${enc(row.platform)}/${enc(row.thread_id)}`") && server.includes('"/api/channels/spaces/:platform/:threadId"'));
check("it reads the room list and a room's conversation from the S10b routes", routeUsed('"/api/channels/spaces"') && routeUsed("/conversation?limit=") && server.includes('"/api/channels/spaces"') && server.includes("/api/channels/spaces/:platform/:threadId/conversation"));
check("it sets a note's / memory item's room flag through the existing 'groups' actions", routeUsed('action: "groups", note: item.id') && routeUsed('action: "groups", record: item.id')
  && server.includes('action !== "groups"') && server.includes('"groups"].includes(action)'));
check("only the four keys the route allows are used", ["group_recall", "ambient_messages", "ambient_minutes", "ambient_senders"].every((k) => panelSrc.includes(`"${k}"`)) && !/room-setting[^;]*bot_token/.test(panelSrc));
const inner = panelSrc.split("\n").filter((l) => /\.innerHTML\b/.test(l));
check("the only innerHTML is still the fixed icon template", inner.length === 1 && /t\.innerHTML = ICON\[name\]/.test(inner[0]), inner.join(" | "));
check("no outerHTML / insertAdjacentHTML / document.write", !/outerHTML|insertAdjacentHTML|document\.write/.test(panelSrc) && !/innerHTML|outerHTML/.test(chipSrc.replace(/never markup/g, "")));
check("no browser storage and no token", !/localStorage|sessionStorage/.test(panelSrc + chipSrc) && !/access_token|bot_token\s*[:=]/.test(chipSrc));
check("an 'on' that widens asks for a second click", /state\.recallAsk = id/.test(panelSrc) && /state\.ambientAsk = id/.test(panelSrc) && /state\.groupAsk = \{ key: k, kind, id: item\.id \}/.test(panelSrc));
check("turning group recall OFF does not ask", /roomSetting\(id, "group_recall", false/.test(panelSrc));
check("room lines are shown by who wrote them, as text", /roomSpeaker\(e\)/.test(panelSrc) && /messageNode\(e, \{ room: true \}\)/.test(panelSrc));
check("the Servers tab links a channel to its room and has the two new rows", /openRoom\("discord", c\.id\)/.test(panelSrc) && /readingSavingRows\(c, "channel"/.test(panelSrc) && /readingSavingRows\(s, "guild"/.test(panelSrc));

// ---- the Ask thread ----
const app = read("web", "public", "app.js");
const index = read("web", "public", "index.html");
check("app.js draws the chip from ex.speaker", /addUserBubble\(AI \? AI\.textOf\(ex\)[^;]*ex\.speaker\)/.test(app) && /window\.JarvisSpeaker \? window\.JarvisSpeaker\.chip\(speaker\) : null/.test(app));
check("with no speaker the role stays 'You'", /chip \|\| "You"/.test(app));
check("speaker-chip.js loads before app.js", index.indexOf('src="speaker-chip.js"') > 0 && index.indexOf('src="speaker-chip.js"') < index.indexOf('src="app.js"'));
const css = read("web", "public", "channels.css") + read("web", "public", "style.css");
check("the styles exist", [".ch-rooms", ".ch-room-view__msgs", ".ch-groups", ".ch-rs", ".ch-chanx", ".speaker-chip"].every((c) => css.includes(c)));

console.log(`${passed}/${passed + failed.length} passed`);
for (const f of failed) console.log("  FAIL:", f);
process.exit(failed.length ? 1 : 0);
