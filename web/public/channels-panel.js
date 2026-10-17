/* ============================================================================
 * channels-panel.js — Menu → Channels (L.36).
 *
 * WHAT IT IS
 * ----------
 * The Channels panel, per person instead of global. Laid out like the Test
 * Checklist — a list on the left, the selected item in the middle — and the
 * items are the PEOPLE who have messaged Jarvis on Discord / Instagram
 * (registered only: anyone denied at the gate never gets a record, see
 * channels/permissions.py).
 *
 *   left    "Home" pinned on top, then people: avatar, name, handle, what each
 *           can do at a glance
 *   middle  one person — Profile tab, and a Permissions tab with a switch for
 *           every thing they can be allowed to do (DMs, replies, tool use and
 *           WHICH tools, whether Jarvis may DM them, ownership, blocking)
 *           ...or HOME, which is picked exactly like a person but holds what
 *           used to be the right-hand side panel, as tabs: Platforms (one card
 *           each: enabled, token set, owner, master tools switch), Servers
 *           (Discord), Turned away, and Global lists (the raw allow-lists).
 *           There is no third column any more; it was hidden under 1100px, which
 *           made platform health unreachable on a narrow window.
 *
 * WHERE THE TRUTH LIVES
 * ---------------------
 * Nothing is decided here. Every switch POSTs to /api/channels/people/...,
 * which runs `jarvis channels-user` — channels/user_admin.py, the same code a
 * terminal runs — so this panel and the CLI cannot disagree. After every
 * change the whole list is re-read, so what you see is what the gate reads.
 *
 * CONVERSATION / USAGE / TEST  (L.36-P1, P2, P3)
 * ----------------------------------------------
 * Three more tabs on a person, all READ-ONLY. Conversation shows what they sent
 * and how Jarvis answered (including what the gate turned away); Usage shows
 * their messages, tokens and tool calls; Test runs the gate as them — a dry run
 * that calls no model, sends nothing and saves nothing, so it never appears in
 * their conversation or in their usage. Each loads when its tab is opened, not
 * before, and every number comes from the server.
 *
 * QUICK SETUPS AND BULK EDIT  (L.36-P4, P5)
 * -----------------------------------------
 * A "quick setup" is the existing switches written together (No access, Chat
 * only, Chat + tell the owner, Trusted). It is only ever a PREVIEW first — a
 * dry run that writes nothing, with each switch's before and after — and
 * Apply is a separate click. "Select people…" lets you tick several people and
 * apply a setup or one switch to all of them; each person's refusals (blocked,
 * covered by "*", the owner) come back per person and are listed, never hidden.
 * The server runs both through the very calls the single switches use
 * (channels/preset_admin.py), so nothing here can make a state a switch can't.
 *
 * ADD / RENAME / LINK
 * -------------------
 * "+ Add a new person" opens a panel of its own (stacked over this one) that puts
 * someone who hasn't messaged yet on the list (an id, or an @handle —
 * Instagram only reveals an id after the person writes); it grants nothing,
 * the switches do. A person named in an allow-list but not
 * yet seen is listed too (the server reconciles that on every read). The
 * name is editable by hand and then locked against a guest's own "call me X".
 * A Discord and an Instagram account can be LINKED as one human: identity
 * only, no permission is shared.
 *
 * BOT TOKENS NEVER PASS THROUGH HERE. The platform cards only say whether one
 * is set and print the terminal command to set it (same standing rule as the
 * panel this replaces).
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 * Names, handles, notes and tool descriptions are typed by strangers or by
 * tool authors. They go in with textContent. The only innerHTML here parses
 * the fixed SVG icon strings below, which contain no data.
 *
 * Deliberately standalone, like test-checklist.js: app.js only needs the one
 * line that calls window.JarvisChannels.open().
 * ========================================================================= */

(function (global) {
  "use strict";

  const PLATFORMS = {
    discord: { label: "Discord" },
    instagram: { label: "Instagram" },
  };
  // Tools whose handlers refuse anyone but the owner (tools.OWNER_ONLY_TOOLS).
  // They can't be handed to a guest, so the picker shows them locked.
  const OWNER_ONLY = new Set(["send_dm", "recent_dms", "recent_spaces", "person_remember", "person_forget", "person_instruct", "person_recall", "person_review", "await_reply"]);
  // Tools the machinery adds for anyone with a custom list (user_perms.
  // PLUMBING_TOOLS) — shown as a footnote so the owner knows what "only
  // these" really means.
  const PLUMBING = ["search_tools", "get_tool_schema", "search_commands", "remember_sender", "who_am_i_talking_to"];

  // ------------------------------------------------------------------ icons
  // Fixed strings, no data. 24x24, stroke currentColor.
  const P = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICON = {
    dm: P('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>'),
    reply: P('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.5-4.7A8 8 0 1 1 21 12Z"/><path d="M8.5 11h7M8.5 14h4"/>'),
    tool: P('<path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L3.5 17.3a1.9 1.9 0 0 0 2.7 2.7l5.8-5.8a4 4 0 0 0 5.2-5.4l-2.5 2.5-2.4-.6-.6-2.4 2.5-2.5Z"/>'),
    thinking: P('<path d="M12 3a6 6 0 0 0-3.6 10.8V16h7.2v-2.2A6 6 0 0 0 12 3Z"/><path d="M9.5 19h5M10.5 21.5h3"/>'),
    send_dm: P('<path d="m21 3-9.5 9.5"/><path d="M21 3 14.5 21l-3-8.5L3 9.5 21 3Z"/>'),
    owner: P('<path d="M3 8l4.5 4L12 5l4.5 7L21 8l-2 11H5L3 8Z"/><path d="M5 19h14"/>'),
    blocked: P('<circle cx="12" cy="12" r="9"/><path d="m5.7 5.7 12.6 12.6"/>'),
    lock: P('<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>'),
    warn: P('<path d="M12 3.5 2.8 19.5h18.4L12 3.5Z"/><path d="M12 10v4.5M12 17.2v.1"/>'),
    info: P('<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.1"/>'),
    user: P('<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>'),
    shield: P('<path d="M12 3 4.5 6v5.5c0 4.4 3 7.8 7.5 9.5 4.5-1.7 7.5-5.1 7.5-9.5V6L12 3Z"/><path d="m8.8 12 2.3 2.3 4.2-4.6"/>'),
    discord: P('<path d="M7 7.5c1.6-.7 3.2-1 5-1s3.4.3 5 1c1.3 2.4 2 5 2 8-1.2.9-2.6 1.4-4 1.6l-.9-1.6M7 7.5c-1.3 2.4-2 5-2 8 1.2.9 2.6 1.4 4 1.6l.9-1.6"/><circle cx="9.4" cy="12.2" r="1.1"/><circle cx="14.6" cy="12.2" r="1.1"/>'),
    instagram: P('<rect x="4" y="4" width="16" height="16" rx="4.5"/><circle cx="12" cy="12" r="3.6"/><circle cx="16.8" cy="7.2" r=".6"/>'),
    image: P('<rect x="3.5" y="5" width="17" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17 5-4.5 3.5 3L16 12l4 4"/>'),
    plus: P('<path d="M12 5v14M5 12h14"/>'),
    link: P('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
    edit: P('<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/>'),
    trash: P('<path d="M5 7h14M10 7V4.5h4V7M7 7l1 13h8l1-13M10.5 11v6M13.5 11v6"/>'),
    chat: P('<path d="M4 5h16v11H9l-5 4V5Z"/><path d="M8 9.5h8M8 12.5h5"/>'),
    chart: P('<path d="M4 4v16h16"/><path d="M8 16v-4M12 16V8M16 16v-6"/>'),
    flask: P('<path d="M9 3h6M10 3v6l-5.5 9.5A1.5 1.5 0 0 0 5.8 21h12.4a1.5 1.5 0 0 0 1.3-2.5L14 9V3"/><path d="M7.5 15h9"/>'),
    refresh: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
    clock: P('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>'),
    server: P('<rect x="4" y="4" width="16" height="6" rx="1.5"/><rect x="4" y="14" width="16" height="6" rx="1.5"/><path d="M8 7h.01M8 17h.01"/>'),
    home: P('<path d="M3.5 11.2 12 4l8.5 7.2"/><path d="M5.5 10v9.5h13V10"/><path d="M10 19.5v-5h4v5"/>'),
    memory: P('<path d="M6 4h12v16l-6-4-6 4V4Z"/><path d="M9.5 8.5h5M9.5 11.5h3"/>'),
    download: P('<path d="M12 4v10M8 10.5l4 4 4-4"/><path d="M5 19h14"/>'),
    people: P('<circle cx="9" cy="8.5" r="3.2"/><path d="M3 19.5a6 6 0 0 1 12 0"/><path d="M16 5.6a3.2 3.2 0 0 1 0 5.8M18 14.2a6 6 0 0 1 3 5.3"/>'),
  };
  function icon(name) {
    const t = document.createElement("template");
    t.innerHTML = ICON[name] || "";
    return t.content.firstChild;
  }

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (Array.isArray(children) ? children : children !== undefined && children !== null ? [children] : []).forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return node;
  }
  const $ = (sel) => document.querySelector(sel);
  const UI = () => global.JarvisUI || null;
  function toast(message, level) {
    if (UI() && UI().toast) UI().toast({ message, level: level || "info" });
  }

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && (data.error || data.note)) || `${method} ${url} failed (${res.status})`);
    }
    return data;
  }

  const pkey = (p) => `${p.platform}:${p.user_id}`;

  // Their own name, else the linked account's (the server works that out),
  // else @handle, else the id.
  const nameOf = (p) => p.name_effective || p.name || "";
  function displayName(p) { return nameOf(p) || (p.handle ? "@" + p.handle : "") || p.user_id || "unknown"; }

  function initials(p) {
    const base = (nameOf(p) || p.handle || p.user_id || "?").replace(/^@/, "").trim();
    const parts = base.split(/[\s._-]+/).filter(Boolean);
    const letters = parts.length >= 2 ? parts[0][0] + parts[1][0] : base.slice(0, 2);
    return (letters || "?").toUpperCase();
  }

  // Stable hue per person, so the placeholder avatar is recognisable at a
  // glance and never changes between sessions (no data, just the id).
  function hueFor(text) {
    let h = 0;
    const s = String(text || "");
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % 360;
  }

  function relTime(ts, now) {
    if (!ts) return "never";
    const sec = Math.max(0, Math.round(((now || Date.now() / 1000) - ts)));
    if (sec < 45) return "just now";
    if (sec < 3600) return Math.round(sec / 60) + "m ago";
    if (sec < 86400) return Math.round(sec / 3600) + "h ago";
    if (sec < 86400 * 30) return Math.round(sec / 86400) + "d ago";
    if (sec < 86400 * 365) return Math.round(sec / (86400 * 30)) + "mo ago";
    return Math.round(sec / (86400 * 365)) + "y ago";
  }
  function dateText(ts) {
    if (!ts) return "—";
    try { return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch (_) { return "—"; }
  }

  // How long until an epoch-seconds deadline, in the panel's short style.
  // "now" is injectable so tests need no clock. Past deadlines read "ended".
  function untilLeft(ts, now) {
    if (!ts) return "";
    const sec = Math.round(ts - (now || Date.now() / 1000));
    if (sec <= 0) return "ended";
    if (sec < 90) return "in under 2 min";
    if (sec < 3600) return "in " + Math.round(sec / 60) + " min";
    if (sec < 86400) return "in " + Math.round(sec / 3600) + " h";
    return "in " + Math.round(sec / 86400) + " d";
  }
  // The time limits the panel offers: label, minutes. The server accepts any
  // whole number of minutes from 1 to 30 days; these are just the round ones.
  const TIME_LIMITS = [
    { label: "1 hour", minutes: 60 },
    { label: "8 hours", minutes: 480 },
    { label: "24 hours", minutes: 1440 },
    { label: "7 days", minutes: 10080 },
  ];

  // One status for a person's overall posture; drives the card's left rule.
  function postureOf(p) {
    if (p.blocked) return "bad";
    if (p.owner) return "owner";
    if (!p.reply.on) return "off";
    if (p.effective.tools === "all") return "limited"; // the broadest grant: worth the amber
    return "on";
  }

  const STATE_FILTERS = [
    { id: "all", label: "All", ch: "", test: () => true },
    { id: "owner", label: "Owner", ch: "owner", test: (p) => p.owner },
    { id: "tools", label: "Tools", ch: "limited", test: (p) => p.effective.tools !== "none" },
    { id: "answered", label: "Answered", ch: "on", test: (p) => p.effective.answered && !p.blocked },
    { id: "silent", label: "Not answered", ch: "off", test: (p) => !p.effective.answered && !p.blocked },
    { id: "blocked", label: "Blocked", ch: "bad", test: (p) => p.blocked },
  ];

  // The status dropdown's options: every filter with how many people it would
  // show under the current search + platform (same counts the chips used to carry).
  function stateFilterOptions(list, { search, platform }) {
    const base = filterPeople(list, { search, platform, state: "all" });
    return STATE_FILTERS.map((f) => ({ id: f.id, label: f.label, ch: f.ch, n: base.filter(f.test).length }));
  }

  // ---- allowed_guilds helpers (pure; v = the /api/channels/servers view) ----
  const GUILD_ID = /^[0-9]{1,25}$/;
  const guildIdOk = (text) => GUILD_ID.test(String(text || "").trim());
  function guildName(v, id) {
    const s = ((v && v.servers) || []).find((x) => x.id === id);
    return s && s.name ? s.name : "";
  }
  // Servers that can be picked from the dropdown: seen by the bot, still joined, not already allowed.
  function guildChoices(v) {
    const allowed = new Set((v && v.allowed_guilds) || []);
    return ((v && v.servers) || []).filter((s) => s.known && !s.left && !allowed.has(s.id)).map((s) => ({ id: s.id, name: s.name || "" }));
  }
  // What the user typed beats what they picked; "" when neither is a valid id.
  function guildPick(typed, picked) {
    const t = String(typed || "").trim();
    if (t) return guildIdOk(t) ? t : "";
    const p = String(picked || "").trim();
    return guildIdOk(p) ? p : "";
  }

  // ---- allowed_channels helpers (pure; v = the /api/channels/servers view) ----
  // A channel id is a snowflake like a server id, so the same check applies.
  const channelIdOk = guildIdOk;
  // The server + channel names for an id, from the servers the bot has seen
  // ({ name, guild } -- both "" for an id nobody has seen, e.g. a typo or a thread).
  function channelInfo(v, id) {
    for (const s of (v && v.servers) || []) {
      const c = (s.channels || []).find((x) => x.id === id);
      if (c) return { name: c.name || "", guild: s.name || "" };
    }
    return { name: "", guild: "" };
  }
  // "#general in My Server \u00b7 123", falling back to the bare id.
  function channelLabel(v, id) {
    const i = channelInfo(v, id);
    if (!i.name) return id;
    return `#${i.name}${i.guild ? ` in ${i.guild}` : ""} \u00b7 ${id}`;
  }
  // Channels that can be picked from the dropdown, grouped by server: seen by
  // the bot, in a server it is still in, not already allowed.
  function channelChoices(v) {
    const allowed = new Set((v && v.allowed_channels) || []);
    const out = [];
    for (const s of (v && v.servers) || []) {
      if (!s.known || s.left) continue;
      const channels = (s.channels || []).filter((c) => !allowed.has(c.id)).map((c) => ({ id: c.id, name: c.name || "" }));
      if (channels.length) out.push({ id: s.id, name: s.name || "", channels });
    }
    return out;
  }
  // What the user typed beats what they picked; "" when neither is a valid id.
  const channelPick = guildPick;

  // ---- shared rooms: pure helpers (channel spaces S11; no DOM, tested in verify_channels_rooms.js) ----
  // "Use in rooms" per saved note / memory item: inherit (follows the platform's group_recall
  // master), on, off. Same rule as channels/people.recall_allowed and person_memory.room_allows:
  // off always wins, on wins over a master that is off, inherit follows the master.
  const GROUP_FLAGS = ["inherit", "on", "off"];
  const groupFlagOf = (item) => (item && GROUP_FLAGS.includes(item.groups) ? item.groups : "inherit");
  const recallOn = (config, id) => !!(config && config[id] && config[id].group_recall === true);
  // S9c: only the literal true counts, and only Instagram has the switch.
  const transportOn = (config, id) => id === "instagram" && !!(config && config[id] && config[id].group_transport === true);
  function usedInRooms(flag, master) {
    if (flag === "off") return false;
    if (flag === "on") return true;
    return master === true;
  }
  function groupUseText(flag, master) {
    if (flag === "on") return "Used in room replies, whatever the master switch says.";
    if (flag === "off") return "Never used in room replies.";
    return master === true ? "Follows the master switch, which is on: used in room replies."
      : "Follows the master switch, which is off: not used in room replies.";
  }

  // Reading other messages in a room (channels/ambient.py): how many are kept per room (0 = off),
  // for how long, and whose. Held in memory only. Unreadable values read as the defaults, and an
  // unreadable `ambient_senders` as the narrower "approved", exactly as config.ambient_limits does.
  const AMBIENT_DEFAULTS = { messages: 20, minutes: 30, senders: "approved" };
  function ambientOf(config, id) {
    const c = (config && config[id]) || {};
    const whole = (v, d, lo, hi) => (Number.isInteger(v) && v >= lo && v <= hi ? v : d);
    return {
      messages: whole(c.ambient_messages, AMBIENT_DEFAULTS.messages, 0, 100),
      minutes: whole(c.ambient_minutes, AMBIENT_DEFAULTS.minutes, 1, 1440),
      senders: String(c.ambient_senders || "").toLowerCase() === "anyone" ? "anyone" : "approved",
    };
  }
  function ambientText(a) {
    if (!a.messages) return "Off: Jarvis only reads messages that are addressed to it.";
    return `Keeps the last ${a.messages} message${a.messages === 1 ? "" : "s"} of each room for ${a.minutes} min, from ${a.senders === "anyone" ? "anyone in the room" : "people you've approved"}. Held in memory only, never saved.`;
  }
  // Platform-wide saving (log_conversations): on unless the config says otherwise.
  const savingOn = (config, id) => { const c = (config && config[id]) || {}; return c.log_conversations === undefined ? true : !!c.log_conversations; };

  // The saved rooms (GET /api/channels/spaces rows).
  const ROOM_KIND_LABEL = { server_channel: "Channel", server_thread: "Thread", group: "Group chat", dm: "Direct message" };
  const roomKindLabel = (kind) => ROOM_KIND_LABEL[kind] || "Room";
  const roomsOf = (data) => (data && Array.isArray(data.spaces) ? data.spaces : []);
  const roomKey = (r) => `${r.platform}|${r.thread_id}`;
  const roomTitle = (r) => r.title || r.thread_id || "Room";
  const roomIsServer = (r) => r.kind === "server_channel" || r.kind === "server_thread";
  function filterRooms(rows, opts) {
    const o = opts || {};
    const q = String(o.search || "").trim().toLowerCase();
    return (rows || []).filter((r) => {
      if (o.platform && o.platform !== "all" && r.platform !== o.platform) return false;
      if (o.kind === "server" && !roomIsServer(r)) return false;
      if (o.kind === "group" && r.kind !== "group") return false;
      if (!q) return true;
      return [r.title, r.thread_id].some((v) => String(v || "").toLowerCase().includes(q));
    });
  }
  // The saved room behind a Discord channel row, if it has one (a channel's thread id is its id).
  const roomForChannel = (rows, platform, id) => (rows || []).find((r) => r.platform === platform && r.thread_id === String(id)) || null;
  const roomSwitchOf = (row, sw) => (row && row.switches && row.switches[sw] === "off" ? "off" : "inherit");
  // Is the Discord server around this channel switched off for `sw` ("log" or "ambient")?
  function serverSwitchOff(view, channelId, sw) {
    for (const s of (view && view.servers) || []) {
      if ((s.channels || []).some((c) => c.id === channelId)) return !!(s.override && s.override[sw === "log" ? "log" : "ambient"] === "off");
    }
    return false;
  }
  // Is this room's Saving ("log") or Reading ("ambient") switch on, and if not, is it the
  // room's own switch (can be turned back on here) or something wider (`lock` says what)?
  function roomSwitchState(row, sw, config, serversView) {
    const label = (PLATFORMS[row.platform] || {}).label || row.platform;
    if (sw === "log") {
      if (!savingOn(config, row.platform)) return { on: false, lock: `Saving is off for all of ${label} (log_conversations).` };
    } else if (!ambientOf(config, row.platform).messages) {
      return { on: false, lock: `Reading is off for all of ${label} (ambient_messages is 0).` };
    }
    if (roomSwitchOf(row, sw) === "off") return { on: false, lock: "" };
    if (serverSwitchOff(serversView, row.thread_id, sw)) return { on: false, lock: "The whole server has this off. Turn it on in the Servers tab first." };
    return { on: true, lock: "" };
  }
  // Who wrote a room line, as a label: their name, else @handle, else user…<last4>.
  function roomSpeaker(e) {
    if (!e || e.dir !== "in") return "Jarvis";
    const name = String(e.author_name || "").trim();
    const handle = String(e.user_handle || "").replace(/^@+/, "").trim();
    const uid = String(e.user_id || "").trim();
    return name || (handle ? "@" + handle : "") || (uid ? "user\u2026" + uid.slice(-4) : "someone");
  }

  // ---- helpers for the Conversation / Usage / Test tabs (pure, no DOM) ----
  // The transcript stamps local, zone-less ISO times ("2026-10-01T10:00:05").
  const dayOf = (at) => (typeof at === "string" && /^\d{4}-\d{2}-\d{2}/.test(at)) ? at.slice(0, 10) : "";
  const clockOf = (at) => (typeof at === "string" && at.length >= 16 && at[10] === "T") ? at.slice(11, 16) : "";
  function localDay(d) {
    const x = d || new Date();
    return `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
  }
  function dayLabel(day, today) {
    if (!day) return "Earlier";
    const t = today || localDay();
    if (day === t) return "Today";
    const y = new Date(`${t}T12:00:00`); y.setDate(y.getDate() - 1);
    if (day === localDay(y)) return "Yesterday";
    try { return new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }); } catch (_) { return day; }
  }
  // Consecutive messages sharing a day, in the order given.
  function groupByDay(entries) {
    const out = [];
    for (const e of entries || []) {
      const day = dayOf(e && e.at);
      const last = out[out.length - 1];
      if (last && last.day === day) last.entries.push(e); else out.push({ day, entries: [e] });
    }
    return out;
  }
  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n < 1000) return String(Math.round(n));
    if (n < 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    if (n < 1e6) return Math.round(n / 1000) + "k";
    return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  }
  // Heights (0-100) for the per-day bars. A day with any usage is never
  // invisible; a day with none is exactly 0.
  function barModel(series) {
    const rows = Array.isArray(series) ? series : [];
    const max = rows.reduce((m, r) => Math.max(m, Number(r.total_tokens) || 0), 0);
    return rows.map((r) => {
      const t = Number(r.total_tokens) || 0;
      return { date: r.date, asks: Number(r.asks) || 0, tokens: t, pct: t > 0 && max > 0 ? Math.max(6, Math.round((t / max) * 100)) : 0 };
    });
  }
  function convNote(d) {
    if (!d) return "";
    if (!d.total) return "No messages logged for them yet.";
    return d.shown < d.total ? `Showing the latest ${d.shown} of ${d.total} messages.` : `${d.total} message${d.total === 1 ? "" : "s"}.`;
  }

  function filterPeople(list, { search, platform, state }) {
    const q = String(search || "").trim().toLowerCase().replace(/^@/, "");
    const st = STATE_FILTERS.find((s) => s.id === state) || STATE_FILTERS[0];
    return list.filter((p) => {
      if (platform && platform !== "all" && p.platform !== platform) return false;
      if (!st.test(p)) return false;
      if (!q) return true;
      return [p.name, p.name_effective, p.handle, p.user_id, p.linked && p.linked.handle, p.linked && p.linked.name].some((v) => String(v || "").toLowerCase().includes(q));
    });
  }

  // Draft helpers — a tool scope is edited locally and saved in one go.
  function scopeOf(p) { return { mode: p.tools.mode, names: new Set(p.tools.allow || []) }; }
  function sameScope(a, b) {
    if (a.mode !== b.mode) return false;
    if (a.mode === "inherit") return true; // names are irrelevant in inherit mode
    if (a.names.size !== b.names.size) return false;
    for (const n of a.names) if (!b.names.has(n)) return false;
    return true;
  }

  function groupTools(tools, query) {
    const q = String(query || "").trim().toLowerCase();
    const groups = new Map();
    for (const t of tools) {
      if (q && !(t.name.toLowerCase().includes(q) || String(t.description || "").toLowerCase().includes(q) || String(t.group || "").toLowerCase().includes(q))) continue;
      const g = t.group || "other";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(t);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([id, items]) => ({ id, items: items.sort((x, y) => x.name.localeCompare(y.name)) }));
  }

  // ---- Home (the old side panel, now picked like a person) ----------------
  // A person's key is "<platform>:<id>", always with a colon, so this can never
  // collide with one.
  const HOME_KEY = "home";

  // Everything on the left that is not a person. A person's key is "<platform>:<user id>", so
  // none of these can be one (no platform is called "platform", "server" or "group").
  //   platform:discord          a platform's own page (token, tools master switch, room settings)
  //   server:<guild id>         one Discord server: Overview, Permissions, Conversation
  //   group:<platform>:<id>     one group chat (an Instagram thread): the same three tabs
  const PLAT_PREFIX = "platform:", SERVER_PREFIX = "server:", GROUP_PREFIX = "group:";
  const UNSORTED = "unsorted";   // rooms saved with no server we can name
  const platKey = (id) => PLAT_PREFIX + id;
  const serverKey = (id) => SERVER_PREFIX + id;
  const groupKey = (platform, id) => `${GROUP_PREFIX}${platform}:${id}`;
  function entryKind(k) {
    const s = String(k || "");
    if (!s) return "";
    if (s === HOME_KEY) return "home";
    if (s.startsWith(PLAT_PREFIX)) return "platform";
    if (s.startsWith(SERVER_PREFIX)) return "server";
    if (s.startsWith(GROUP_PREFIX)) return "group";
    return "person";
  }
  // A thread id may itself contain ":", so only the first colon after the prefix splits.
  function groupParts(k) {
    const rest = String(k || "").slice(GROUP_PREFIX.length), i = rest.indexOf(":");
    return String(k || "").startsWith(GROUP_PREFIX) && i > 0 ? { platform: rest.slice(0, i), id: rest.slice(i + 1) } : null;
  }

  // The tabs of a server or a group. Both have the same three.
  const SPACE_TABS = [
    { id: "overview", label: "Overview", ico: "info", title: "What it is and how it is doing" },
    { id: "perms", label: "Permissions", ico: "shield", title: "What Jarvis may do here" },
    { id: "conv", label: "Conversation", ico: "chat", title: "What was saved from it" },
  ];

  // Tabs on Home. Home is only what belongs to no platform: who was turned away, and the raw
  // lists. Platforms, servers and groups are entries of their own on the left.
  function homeTabs() {
    return [
      { id: "denied", label: "Turned away", ico: "blocked", title: "Who the gate refused lately" },
      { id: "lists", label: "Global lists", ico: "people", title: "The raw allow-lists" },
    ];
  }

  // ---- servers and groups as entries (pure) ----------------------------------------------
  // A room row's channel: its own id for a channel, the parent channel for a thread.
  const roomChannelId = (r) => r.channel_id || (r.kind === "server_channel" ? r.thread_id : "");
  // Room times are local, zone-less ISO strings ("2026-10-01T10:05:00"); the helpers above take
  // epoch seconds.
  function isoToSec(iso) {
    if (typeof iso !== "string" || !iso) return 0;
    const t = Date.parse(iso);
    return Number.isFinite(t) ? t / 1000 : 0;
  }
  // The Discord servers to list: every one in the registry view, plus any server a saved room
  // names that the registry lacks, plus (only if needed) one bucket for rooms with no server.
  // Each is { id, name, left, known, view (the /api/channels/servers row or null), rooms }.
  function serverEntries(view, rows) {
    const byId = new Map(), channelOwner = new Map();
    for (const s of (view && view.servers) || []) {
      byId.set(s.id, { id: s.id, name: s.name || "", left: !!s.left, known: !!s.known, view: s, rooms: [] });
      for (const c of s.channels || []) channelOwner.set(c.id, s.id);
    }
    for (const r of rows || []) {
      if (r.platform !== "discord" || !roomIsServer(r)) continue;
      const gid = r.guild_id || channelOwner.get(roomChannelId(r)) || UNSORTED;
      let e = byId.get(gid);
      if (!e) {
        e = { id: gid, name: gid === UNSORTED ? "Unplaced channels" : "", left: false, known: false, view: null, rooms: [] };
        byId.set(gid, e);
      }
      if (!e.name && r.guild_name) e.name = r.guild_name;
      e.rooms.push(r);
    }
    const label = (e) => (e.name || "~").toLowerCase();
    return [...byId.values()].sort((a, b) => ((a.id === UNSORTED) - (b.id === UNSORTED)) || (a.left - b.left) || label(a).localeCompare(label(b)));
  }
  // The group chats to list (an Instagram thread, live or imported), newest first.
  function groupEntries(rows) {
    return (rows || []).filter((r) => r.kind === "group").sort((a, b) => String(b.modified || "").localeCompare(String(a.modified || "")));
  }
  // The channels of one server for the Conversation tab: every channel the registry knows plus any
  // channel a saved room names, each with its own room (if anything was saved) and its threads.
  function serverChannels(entry) {
    const out = new Map();
    const slot = (id, name) => {
      if (!out.has(id)) out.set(id, { id, name: name || "", room: null, threads: [] });
      const c = out.get(id);
      if (!c.name && name) c.name = name;
      return c;
    };
    for (const c of (entry.view && entry.view.channels) || []) slot(c.id, c.name);
    for (const r of entry.rooms || []) {
      const cid = roomChannelId(r);
      if (r.kind === "server_channel") slot(r.thread_id, r.channel_name).room = r;
      else slot(cid, r.channel_name).threads.push(r);
    }
    const rows = [...out.values()];
    for (const c of rows) c.threads.sort((a, b) => String(b.modified || "").localeCompare(String(a.modified || "")));
    return rows.sort((a, b) => ((a.id === "") - (b.id === "")) || (a.name || "~").toLowerCase().localeCompare((b.name || "~").toLowerCase()));
  }
  // Sum one count over rooms. A room whose log was too big to count says null; the sum then reads "n+".
  function sumCount(rows, field) {
    let n = 0, unknown = false;
    for (const r of rows || []) { if (typeof r[field] === "number") n += r[field]; else unknown = true; }
    return { n, unknown };
  }
  const countText = (c) => (c.unknown ? `${c.n}+` : String(c.n));
  const newestOf = (rows) => (rows || []).reduce((m, r) => (String(r.modified || "") > m ? String(r.modified) : m), "");
  // Is a group answering, as far as the panel can tell: the platform is on and the room has not
  // switched itself off. (Who may be answered is still the person's own switch.)
  function groupAnswering(row, platforms) {
    const pl = platforms && platforms[row.platform];
    return !!(pl && pl.enabled) && !(row.switches && row.switches.enabled === "off") && !row.imported;
  }
  // The three switches a room has besides Saving and Reading, as { on, lock } for the switch row.
  // `lock` is why it can't be changed here ("" when it can). A room is only ever switched OFF
  // (mention: ON); the platform's own setting is shown but never written from a room.
  function roomAnswerState(row, sw, config, platforms) {
    const label = (PLATFORMS[row.platform] || {}).label || row.platform;
    const own = (row.switches && row.switches[sw]) || "inherit";
    if (sw === "enabled") {
      const pl = platforms && platforms[row.platform];
      if (!pl || !pl.enabled) return { on: false, lock: `${label} is switched off.` };
      return { on: own !== "off", lock: "" };
    }
    if (sw === "tools") {
      const pl = platforms && platforms[row.platform];
      if (!pl || !pl.allow_tools) return { on: false, lock: `Tools are off for all of ${label}.` };
      return { on: own !== "off", lock: "" };
    }
    const required = !(config && config[row.platform] && config[row.platform].require_mention === false);
    if (required && own !== "on") return { on: true, lock: `${label} requires a mention everywhere.` };
    return { on: own === "on", lock: "" };
  }

  // One word for how a platform is doing: red = can't work yet (no token),
  // dim = deliberately off, green = on.
  function platformState(pl) {
    if (!pl || !pl.token_set) return { text: "No token", ch: "bad" };
    if (!pl.enabled) return { text: "Off", ch: "off" };
    return { text: "On", ch: "on" };
  }

  // ------------------------------------------------------------------ state
  const state = {
    built: false,
    loading: false,
    error: "",
    platforms: {},
    people: [],
    config: {}, // redacted platform config, for the global lists
    permsError: "",
    selected: null,
    tab: "perms",
    homeTab: "denied", // the tab showing while Home is selected
    spaceTab: "overview", // the tab showing while a server or a group is selected
    chanPick: new Map(),  // server id -> roomKey of the channel or thread open in its Conversation tab
    search: "",
    platformFilter: "all",
    stateFilter: "all",
    tools: null, // live catalogue, loaded when first needed
    toolsState: "idle", // idle | loading | ok | error  (one attempt per refresh)
    toolsError: "",
    toolSearch: "",
    drafts: new Map(), // person key -> { mode, names:Set }
    confirm: null, // { key, flag, ... }  (flag "tool" + timed: minutes = confirming a time-limited grant, L.36-P6)
    instr: new Map(),  // person key -> the instruction being typed, kept across redraws (L.36-P12)
    busy: new Set(), // "<key>|<flag>"
    prevFocus: null,
    adding: false, // the "Add a new person" form is open
    addPlatform: "discord",
    editingName: null, // person key whose name is being typed
    nameAt: "hero",    // where that editor is drawn: "hero" (the pencil by the name) or "details"
    confirmRemove: null, // person key awaiting "Remove" confirmation
    editingNote: null, // { key, id } the remembered note being retyped
    confirmNote: null, // { key, id } a note awaiting its Delete confirmation
    forget: null,      // { key, history, status: loading|ready|error|busy, data, error, req } (L.36-P11)
    // Per-person data for the read-only tabs, keyed by person key. Cleared by
    // the Refresh button; each entry guards against a stale reply with `req`.
    conv: new Map(),   // { status, data, error, limit, req }
    usage: new Map(),  // { status, data, error, days, req }
    sim: new Map(),    // { context, mentioned, status, result, error, req }
    hist: new Map(),   // { status, data, error, limit, req }  (change log, L.36-P15)
    mem: new Map(),    // { status, data, eps, error, req, acting, adding }  (People view, L.21 P6)
    memConfirm: null,  // { key, id } a remembered item awaiting its Remove confirmation
    memDraft: new Map(), // person key -> the fact being typed, kept across redraws
    ruleDraft: new Map(), // person key -> the rule being composed { scope, directive, topic, hard, days } (L.22)
    editingHandle: null, // person key whose handle is being retyped (L.36-P10)
    scrollEnd: false,  // after the next render, show the newest message
    hideTools: false,  // Conversation tab: collapse the "used these tools" rows away
    // Discord servers (the registry + switches from /api/channels/servers).
    servers: null,     // the server's view, or null while unknown / unavailable
    serversError: "",
    serverBusy: new Set(), // "<kind>:<id>|<switch>" while a write is in flight
    // allowed_guilds editor: what is typed / picked survives a re-render; `preview`
    // is the server's "this needs a second click" answer for the add in question.
    guildAdd: { text: "", pick: "", busy: false, error: "", preview: null },
    // allowed_channels editor (L.36-P18): the same shape, for the channel list.
    chanAdd: { text: "", pick: "", busy: false, error: "", preview: null },
    // Quick setups (P4): the list the server sends, the open preview, and the
    // last result (shown until dismissed or another person is picked).
    presets: [],
    presetPrev: null,  // { key, preset, status: loading|ready|error, data, error, ack, req }
    presetDone: null,  // { key, result }
    // Platform tools master switch (P9): platform id -> { status, data, error }.
    master: new Map(),
    // Message one person from the panel (P13): person key -> { text, status, data, error, req }.
    dm: new Map(),
    // Recently turned-away senders (P14): { status: idle|loading|ready|error, data, error, req }.
    denied: { status: "idle", data: null, error: "", req: 0 },
    // Shared rooms (channel spaces S11): the saved-room list, the open room and what is in flight.
    spaces: { status: "idle", data: null, error: "" },
    roomConv: new Map(),           // roomKey -> { status, data, error, limit, req, scrollEnd }
    roomBusy: new Set(),           // "<roomKey>|<switch>" while a write is in flight
    settingBusy: new Set(),        // "<platform>|<key>" for the platform's room settings
    transportAsk: false,           // the Instagram group transport's turn-on awaits its second click
    recallAsk: null,               // platform id whose group-recall turn-on awaits its second click
    ambientDraft: new Map(),       // platform id -> { messages, minutes, senders } being edited
    ambientAsk: null,              // platform id whose "read anyone" choice awaits its second click
    groupAsk: null,                // { key, kind: "note"|"mem", id } an item whose "use in rooms: on" awaits it
    // Bulk edit (P5).
    selectMode: false,
    picked: new Set(), // person keys
    bulkOpen: false,   // the middle pane shows the bulk editor
    bulk: newBulk(),
  };
  let dom = null;

  const personByKey = (k) => state.people.find((p) => pkey(p) === k) || null;
  const current = () => personByKey(state.selected);

  // Is this key still something the left list can show? (After a reload a server may be gone.)
  function entryExists(k) {
    switch (entryKind(k)) {
      case "home": return true;
      case "platform": return !!state.platforms[String(k).slice(PLAT_PREFIX.length)] && !!PLATFORMS[String(k).slice(PLAT_PREFIX.length)];
      case "server": return !!serverEntryOf(k);
      case "group": return !!groupRowOf(k);
      case "person": return !!personByKey(k);
      default: return false;
    }
  }
  const allRooms = () => roomsOf(state.spaces.data);
  const serverEntryOf = (k) => serverEntries(state.servers, allRooms()).find((e) => serverKey(e.id) === k) || null;
  function groupRowOf(k) {
    const g = groupParts(k);
    return g ? groupEntries(allRooms()).find((r) => r.platform === g.platform && r.thread_id === g.id) || null : null;
  }

  // ------------------------------------------------------------------- data
  async function load(keepSelection) {
    let endedNow = [];   // people whose time-limited tool access ended during this load (L.36-P6)
    state.hist.clear();   // every write ends in load(); a cached change log would be one change behind
    state.loading = true;
    state.error = "";
    renderAll();
    try {
      const [ppl, cfg, srv, spc] = await Promise.all([
        api("GET", "/api/channels/people"),
        api("GET", "/api/channels").catch(() => null),
        api("GET", "/api/channels/servers?platform=discord").catch((e) => ({ _error: e.message || "Couldn't read servers." })),
        api("GET", "/api/channels/spaces").catch((e) => ({ _error: e.message || "Couldn't read the rooms." })),
      ]);
      state.spaces.data = spc && !spc._error ? spc : state.spaces.data;
      state.spaces.error = spc && spc._error ? spc._error : "";
      state.spaces.status = spc && spc._error ? "error" : "ok";
      state.servers = srv && !srv._error ? srv : null;
      state.serversError = srv && srv._error ? srv._error : "";
      state.platforms = ppl.platforms || {};
      state.people = Array.isArray(ppl.people) ? ppl.people : [];
      state.presets = Array.isArray(ppl.presets) ? ppl.presets : [];
      state.permsError = ppl.perms_error || "";
      endedNow = Array.isArray(ppl.expired_now) ? ppl.expired_now : [];
      // Someone removed or no longer listed can't stay ticked.
      for (const k of [...state.picked]) if (!personByKey(k)) state.picked.delete(k);
      if (!state.picked.size && state.bulkOpen) { state.bulkOpen = false; state.bulk = newBulk(); }
      state.config = (cfg && cfg.config) || {};
      // Drop drafts for people who no longer exist or whose stored scope now
      // equals the draft (saved elsewhere, e.g. from the terminal).
      for (const [k, d] of [...state.drafts]) {
        const p = personByKey(k);
        if (!p || sameScope(d, scopeOf(p))) state.drafts.delete(k);
      }
      if (!keepSelection || !entryExists(state.selected)) {
        // With nobody registered yet a platform's own page is the one that has something to
        // show (the token still to set), so it is the fallback rather than an empty pane.
        const first = visiblePeople()[0] || state.people[0];
        state.selected = first ? pkey(first) : platformIds().length ? platKey(platformIds()[0]) : HOME_KEY;
      }
    } catch (err) {
      state.error = err.message || "Couldn't read channels.";
    }
    state.loading = false;
    renderAll();
    if (endedNow.length) {
      const names = endedNow.map((x) => x.name || x.user_id).slice(0, 3).join(", ");
      toast(`Tool access ended for ${names}${endedNow.length > 3 ? " and others" : ""}: the time limit ran out. It's in their History.`, "info");
    }
    const here = current();
    if (here && state.tab === "history") ensureHistory(here);
    if (state.denied.status === "idle") loadDenied();
  }

  async function ensureTools() {
    // One attempt per refresh: renderDetail() calls this, and a failed load
    // that re-rendered would otherwise retry itself forever.
    if (state.toolsState !== "idle") return;
    state.toolsState = "loading";
    try {
      const list = await api("GET", "/api/tools");
      state.tools = (Array.isArray(list) ? list : []).map((t) => ({
        name: String(t.name || ""), description: String(t.description || ""), group: String(t.group || "other"),
        confirm: !!t.confirm_required, disabled: !!t.disabled,
      })).filter((t) => t.name);
      state.toolsState = "ok";
    } catch (err) {
      state.toolsState = "error";
      state.toolsError = err.message || "Couldn't read the tool list.";
    }
    renderDetail();
  }

  // Flip one switch. The server is the authority: we show the row as busy,
  // then re-read everything, so a refusal (wildcard, blocked, ...) simply
  // leaves the switch where the gate actually has it.
  async function setFlag(p, flag, value) {
    const bkey = `${pkey(p)}|${flag}`;
    state.busy.add(bkey);
    renderDetail();
    let note = "";
    try {
      const out = await api("POST", `/api/channels/people/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.user_id)}/flag`, { flag, value });
      note = out && out.note ? out.note : "";
    } catch (err) {
      toast(err.message || "Couldn't change that.", "error");
    }
    state.busy.delete(bkey);
    await load(true);
    if (note) toast(note, "info");
  }

  async function saveScope(p) {
    const d = state.drafts.get(pkey(p));
    if (!d) return;
    const names = d.mode === "custom" ? [...d.names].filter((n) => p.owner || !OWNER_ONLY.has(n)) : [];
    const bkey = `${pkey(p)}|tools`;
    state.busy.add(bkey);
    renderDetail();
    try {
      await api("POST", `/api/channels/people/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.user_id)}/tools`, { mode: d.mode, tools: names });
      state.drafts.delete(pkey(p));
      toast(d.mode === "custom" ? `${displayName(p)} can now use ${names.length} tool${names.length === 1 ? "" : "s"}.` : `${displayName(p)} can use any tool the platform allows.`, "success");
    } catch (err) {
      toast(err.message || "Couldn't save the tool list.", "error");
    }
    state.busy.delete(bkey);
    await load(true);
  }

  // Name, add, remove, link. Same pattern as setFlag: the server decides, then
  // the whole list is re-read so the panel shows what the gate has.
  async function personPost(p, action, body) {
    return api("POST", `/api/channels/people/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.user_id)}/${action}`, body || {});
  }

  async function saveName(p, name) {
    try {
      const out = await personPost(p, "name", { name });
      state.editingName = null;
      toast(out.name ? `Now called ${out.name}.` : "Name cleared.", "success");
    } catch (err) {
      toast(err.message || "Couldn't save the name.", "error");
      return;
    }
    await load(true);
  }

  // Time-limited tool access (L.36-P6). `scopeFirst` is the "Only tools I
  // choose" answer for someone with no list yet: the (empty) custom list is
  // saved BEFORE the grant, so a failure in between leaves them with no tools,
  // never with every tool. The server writes the deadline before the list entry
  // for the same reason.
  async function grantTools(p, minutes, scopeFirst) {
    const bkey = `${pkey(p)}|tool`;
    state.busy.add(bkey);
    renderDetail();
    let note = "";
    try {
      if (scopeFirst) await personPost(p, "tools", { mode: "custom", tools: [] });
      const out = await personPost(p, "tools-until", { minutes });
      note = out && out.note ? out.note : "";
      toast(`${displayName(p)} can use tools until ${out && out.until ? dateText(out.until) : "the time limit"}.`, "success");
    } catch (err) {
      toast(err.message || "Couldn't set the time limit.", "error");
    }
    state.busy.delete(bkey);
    await load(true);
    if (note) toast(note, "info");
  }

  // One line the owner typed about HOW to talk to a person (L.36-P12). Empty
  // text clears it. The server refuses the owner's own account and anything
  // over 240 characters; nothing here can grant access.
  async function saveInstruction(p, text) {
    const k = pkey(p);
    const bkey = `${k}|instruction`;
    state.busy.add(bkey);
    renderDetail();
    try {
      const out = await personPost(p, "instruction", { text });
      state.instr.delete(k);
      toast(out && out.instruction ? "Saved. Jarvis follows it from their next message." : "Instruction cleared.", "success");
    } catch (err) {
      toast(err.message || "Couldn't save that.", "error");
      state.busy.delete(bkey);
      renderDetail();
      return;
    }
    state.busy.delete(bkey);
    await load(true);
  }

  // The owner's thinking setting for one person (L.44). `value` is "on", "off" or
  // "default" (clears it); `lock` stops the person changing it with /thinking in
  // chat. The server refuses the owner's own account; the CLI does the work.
  async function saveThinking(p, value, lock) {
    const bkey = `${pkey(p)}|thinking`;
    state.busy.add(bkey);
    renderDetail();
    try {
      const out = await personPost(p, "thinking", value === "default" ? { value } : { value, lock: !!lock });
      toast(value === "default" ? "Thinking is back to the default for them."
        : `Thinking is ${value}${lock ? " and locked" : ""} for ${displayName(p)}.`, "success");
      if (out && out.note) toast(out.note, "info");
    } catch (err) {
      toast(err.message || "Couldn't save the thinking setting.", "error");
    }
    state.busy.delete(bkey);
    await load(true);
  }

  async function addPerson(platform, ident, name, errEl) {
    if (errEl) errEl.textContent = "";
    try {
      const out = await api("POST", "/api/channels/people", { platform, ident, name });
      state.adding = false;
      state.platformFilter = "all";
      state.stateFilter = "all";
      state.search = "";
      if (dom) dom.search.value = "";
      state.selected = `${platform}:${out.user_id}`;
      state.tab = "perms";
      await load(true);
      if (dom) dom.addBtn.focus({ preventScroll: true });
      toast(out.existing ? "They were already on the list." : (out.note || "Added."), out.existing ? "info" : "success");
      const card = dom && dom.list.querySelector(".ch-card.is-active");
      if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
    } catch (err) {
      if (errEl) errEl.textContent = err.message || "Couldn't add them.";
    }
  }

  async function removePerson(p) {
    try {
      await personPost(p, "remove");
      state.confirmRemove = null;
      toast(`${displayName(p)} removed.`, "success");
    } catch (err) {
      toast(err.message || "Couldn't remove them.", "error");
    }
    await load(false);
  }

  async function linkPerson(p, ident, errEl) {
    if (errEl) errEl.textContent = "";
    const other = otherPlatform(p.platform);
    try {
      const out = await personPost(p, "link", { other_platform: other, ident });
      await load(true);
      toast(out.note || `Linked to their ${PLATFORMS[other].label} account.`, out.note ? "info" : "success");
    } catch (err) {
      if (errEl) errEl.textContent = err.message || "Couldn't link them.";
    }
  }

  async function unlinkPerson(p) {
    try { await personPost(p, "unlink"); toast("Unlinked.", "success"); } catch (err) { toast(err.message || "Couldn't unlink.", "error"); }
    await load(true);
  }

  function otherPlatform(platform) { return Object.keys(PLATFORMS).find((id) => id !== platform) || platform; }

  // ---- Discord servers ---------------------------------------------------
  async function loadServers() {
    try {
      const srv = await api("GET", "/api/channels/servers?platform=discord");
      state.servers = srv; state.serversError = "";
    } catch (err) { state.serversError = err.message || "Couldn't read servers."; }
    renderHome();
  }
  async function serverSwitch(kind, id, sw, value) {
    const key = `${kind}:${id}|${sw}`;
    if (state.serverBusy.has(key)) return;
    state.serverBusy.add(key); renderHome();
    try {
      await api("POST", `/api/channels/servers/discord/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, { switch: sw, value });
    } catch (err) { toast(err.message || "Couldn't save that.", "error"); }
    state.serverBusy.delete(key);
    await loadServers();
  }
  // Add / remove one server on allowed_guilds. The server decides what needs a
  // second step: an add that would turn the filter on (or names a server the bot
  // hasn't seen) comes back as a preview and nothing is written until confirmed.
  async function guildMutate(id, remove, confirm) {
    const g = state.guildAdd;
    if (g.busy) return;
    g.busy = true; g.error = ""; renderHome();
    try {
      const res = await api("POST", "/api/channels/servers/discord/allowed-guilds", { id, remove: !!remove, confirm: !!confirm });
      if (res && res.needs_confirm && !res.applied) {
        g.preview = res;
      } else {
        g.preview = null;
        if (res && res.applied) { g.text = ""; g.pick = ""; }
        else if (res && res.note) toast(res.note);
      }
    } catch (err) {
      g.preview = null;
      g.error = err.message || "Couldn't save that.";
    }
    g.busy = false;
    await loadServers();
  }

  // Add / remove one channel on allowed_channels -- the twin of guildMutate: the
  // server answers with a preview when the add would turn the filter on or names
  // a channel the bot hasn't seen, and nothing is written until confirmed.
  async function chanMutate(id, remove, confirm) {
    const g = state.chanAdd;
    if (g.busy) return;
    g.busy = true; g.error = ""; renderHome();
    try {
      const res = await api("POST", "/api/channels/servers/discord/allowed-channels", { id, remove: !!remove, confirm: !!confirm });
      if (res && res.needs_confirm && !res.applied) {
        g.preview = res;
      } else {
        g.preview = null;
        if (res && res.applied) {
          g.text = ""; g.pick = "";
          if (res.guild_blocked) toast(res.note, "info");
        } else if (res && res.note) toast(res.note);
      }
    } catch (err) {
      g.preview = null;
      g.error = err.message || "Couldn't save that.";
    }
    g.busy = false;
    await loadServers();
  }

  async function globalMutate(platform, short, entry, remove, errEl) {
    if (errEl) errEl.textContent = "";
    try {
      await api("POST", `/api/channels/${encodeURIComponent(platform)}/${encodeURIComponent(short)}`, { entry, remove: !!remove });
      await load(true);
    } catch (err) {
      if (errEl) errEl.textContent = err.message || "Couldn't save that.";
    }
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) {
      try {
        const ta = el("textarea", { style: "position:fixed;opacity:0" });
        ta.value = text; document.body.appendChild(ta); ta.select();
        const ok = document.execCommand("copy"); ta.remove(); return ok;
      } catch (_) { return false; }
    }
  }

  // ----------------------------------------------------------------- render
  function visiblePeople() {
    return filterPeople(state.people, { search: state.search, platform: state.platformFilter, state: state.stateFilter });
  }

  function avatar(p, size) {
    const node = el("span", { class: "ch-avatar" + (size === "lg" ? " ch-avatar--lg" : ""), style: `--hue: hsl(${hueFor(pkey(p))} 70% 58%)`, "aria-hidden": "true" }, initials(p));
    if (/^https:\/\//.test(p.avatar || "")) {
      const img = el("img", { src: p.avatar, alt: "", loading: "lazy", referrerpolicy: "no-referrer", decoding: "async" });
      img.addEventListener("error", () => img.remove()); // initials stay underneath
      node.appendChild(img);
    }
    const wrap = el("span", { class: "ch-avatar-wrap", "data-plat": p.platform }, [node]);
    const plat = el("span", { class: "ch-avatar__plat" });
    plat.appendChild(icon(p.platform));
    wrap.appendChild(plat);
    return wrap;
  }

  function tag(text, ch, ico) {
    const t = el("span", { class: "ch-tag", "data-ch": ch || null }, []);
    if (ico) t.appendChild(icon(ico));
    t.appendChild(document.createTextNode(text));
    return t;
  }

  function renderChips() {
    dom.platChips.textContent = "";
    const counts = { all: state.people.length };
    for (const id of Object.keys(PLATFORMS)) counts[id] = state.people.filter((p) => p.platform === id).length;
    for (const id of ["all", ...Object.keys(PLATFORMS)]) {
      dom.platChips.appendChild(el("button", {
        type: "button", class: "ch-chip" + (state.platformFilter === id ? " is-active" : ""), "aria-pressed": state.platformFilter === id ? "true" : "false",
        onclick: () => { state.platformFilter = id; renderList(); renderChips(); },
      }, [id === "all" ? "All platforms" : PLATFORMS[id].label, el("span", { class: "ch-chip__n" }, String(counts[id] || 0))]));
    }
    // Status filter: one dropdown (All / Owner / Tools / Answered / Not answered / Blocked),
    // each option carrying its count. It replaced a row of six chips that wrapped badly.
    dom.stateChips.textContent = "";
    const opts = stateFilterOptions(state.people, { search: state.search, platform: state.platformFilter });
    const cur = opts.find((o) => o.id === state.stateFilter) || opts[0];
    const sel = el("select", { class: "ch-filter__select", id: "ch-state-select", "aria-label": "Filter by status", "data-ch": cur.ch || null },
      opts.map((o) => el("option", { value: o.id, selected: o.id === cur.id }, `${o.label} (${o.n})`)));
    sel.addEventListener("change", () => {
      state.stateFilter = sel.value; renderList(); renderChips();
      const again = dom.stateChips.querySelector("select");   // renderChips rebuilt it: keep the keyboard on it
      if (again) again.focus();
    });
    dom.stateChips.appendChild(sel);
  }

  function renderList() {
    renderHomeCard();
    const list = dom.list;
    list.textContent = "";
    if (state.loading && !state.people.length) {
      for (let i = 0; i < 5; i++) list.appendChild(el("div", { class: "ch-skel" }));
      return;
    }
    const rows = visiblePeople();
    dom.count.textContent = rows.length === state.people.length ? `${rows.length}` : `${rows.length} / ${state.people.length}`;
    appendSpaceEntries(list);
    if (!state.people.length) {
      list.appendChild(el("div", { class: "ch-empty" }, [
        el("b", null, "Nobody is on the list yet."), el("br"),
        "People appear here the first time they DM or mention Jarvis, or when you add them with “+ Add a new person” (an id or @handle is enough). Anyone you put in a Global list shows up here too.",
      ]));
      return;
    }
    if (!rows.length) {
      list.appendChild(el("div", { class: "ch-empty" }, "No one matches those filters."));
      return;
    }
    let lastPlat = null;
    for (const p of rows) {
      if (state.platformFilter === "all" && p.platform !== lastPlat) {
        lastPlat = p.platform;
        list.appendChild(el("div", { class: "ch-group-label" }, `${PLATFORMS[p.platform] ? PLATFORMS[p.platform].label : p.platform} people`));
      }
      const k = pkey(p);
      const badges = [];
      if (p.linked) badges.push(tag(PLATFORMS[p.linked.platform] ? PLATFORMS[p.linked.platform].label : "Linked", null, "link"));
      if (p.manual && !p.messages) badges.push(tag("Not seen yet", null, "plus"));
      if (p.owner) badges.push(tag("Owner", "owner", "owner"));
      if (p.blocked) badges.push(tag("Blocked", "bad", "blocked"));
      else {
        badges.push(p.effective.answered ? tag("Replies", "on") : tag("No reply", "off"));
        if (p.effective.tools === "all") badges.push(tag("All tools", "limited", "tool"));
        else if (p.effective.tools === "custom") badges.push(tag(`${p.effective.tool_count} tool${p.effective.tool_count === 1 ? "" : "s"}`, "limited", "tool"));
      }
      const picking = state.selectMode;
      const picked = picking && state.picked.has(k);
      const card = el("button", {
        type: "button", class: "ch-card" + (picking ? " ch-card--pick" : "") + (k === state.selected && !picking ? " is-active" : "") + (picked ? " is-picked" : ""), role: "option",
        "aria-selected": (picking ? picked : k === state.selected) ? "true" : "false",
        "data-key": k, "data-ch": postureOf(p),
        onclick: () => (picking ? togglePick(k) : select(k)),
      }, [
        picking ? el("span", { class: "ch-pick" + (picked ? " is-on" : ""), "aria-hidden": "true" }) : null,
        avatar(p),
        el("div", { style: "min-width:0" }, [
          el("div", { class: "ch-card__name" }, displayName(p)),
          el("div", { class: "ch-card__sub" }, subLine(p)),
          el("div", { class: "ch-card__badges" }, badges),
        ]),
        el("div", { class: "ch-card__when", title: dateText(p.last_seen) }, relTime(p.last_seen)),
      ]);
      list.appendChild(card);
    }
  }

  // ---- servers and groups in the list ------------------------------------------------------
  // One card shape for everything that is not a person: a platform, a server, a group.
  function entryCard(o) {
    const active = o.key === state.selected && !state.selectMode;
    return el("button", {
      type: "button", class: "ch-card" + (active ? " is-active" : ""), role: "option", "aria-selected": active ? "true" : "false",
      "data-key": o.key, "data-ch": o.ch || null, "data-entry": entryKind(o.key),
      onclick: () => select(o.key),
    }, [
      el("span", { class: "ch-avatar-wrap", "data-plat": o.plat || null }, [
        el("span", { class: "ch-avatar ch-avatar--entry", style: `--hue: hsl(${hueFor(o.key)} 70% 58%)`, "aria-hidden": "true" }, [icon(o.ico)]),
        o.plat ? el("span", { class: "ch-avatar__plat" }, [icon(o.plat)]) : null,
      ]),
      el("div", { style: "min-width:0" }, [
        el("div", { class: "ch-card__name" }, o.name),
        el("div", { class: "ch-card__sub" }, o.sub),
        o.badges && o.badges.length ? el("div", { class: "ch-card__badges" }, o.badges) : null,
      ]),
      o.when ? el("div", { class: "ch-card__when", title: o.whenTitle || null }, o.when) : null,
    ]);
  }

  function serverStatus(e) {
    if (e.left) return { text: "Left", ch: "off", ico: "blocked" };
    if (!e.view) return { text: "Not in the registry", ch: "off", ico: "info" };
    return e.view.answering ? { text: "Answering", ch: "on", ico: "chat" } : { text: "Not answering", ch: "off", ico: "blocked" };
  }

  function serverListCard(e) {
    const st = serverStatus(e), last = newestOf(e.rooms), chans = serverChannels(e).filter((c) => c.id).length;
    const msgs = sumCount(e.rooms, "messages_in");
    return entryCard({
      key: serverKey(e.id), ico: "server", plat: "discord", ch: st.ch === "on" ? "on" : "off",
      name: e.name || (e.id === UNSORTED ? "Unplaced channels" : "Unnamed server"),
      sub: [e.id === UNSORTED ? "" : e.id, `${chans} channel${chans === 1 ? "" : "s"}`, e.rooms.length ? `${countText(msgs)} in` : ""].filter(Boolean).join(" · "),
      badges: [tag(st.text, st.ch, st.ico)],
      when: last ? relTime(isoToSec(last)) : "", whenTitle: last ? dateText(isoToSec(last)) : "",
    });
  }

  function groupListCard(r) {
    const answering = groupAnswering(r, state.platforms);
    const badges = [r.imported ? tag("Imported", "limited", "download") : answering ? tag("Answering", "on", "chat") : tag("Not answering", "off", "blocked")];
    if (!r.imported && !r.saved) badges.push(tag("No log", "off"));
    return entryCard({
      key: groupKey(r.platform, r.thread_id), ico: "people", plat: r.platform, ch: answering ? "on" : "off",
      name: roomTitle(r),
      sub: [(PLATFORMS[r.platform] || {}).label || r.platform, typeof r.messages_in === "number" ? `${r.messages_in} in` : ""].filter(Boolean).join(" · "),
      badges, when: r.modified ? relTime(isoToSec(r.modified)) : "", whenTitle: r.modified ? dateText(isoToSec(r.modified)) : "",
    });
  }

  // Servers and groups above the people. The status filter is about people, so it hides these
  // (and so does ticking people for a bulk edit); the platform chips and the search narrow both.
  function appendSpaceEntries(list) {
    if (state.selectMode || state.stateFilter !== "all") return 0;
    const q = state.search.trim().toLowerCase();
    const hit = (text) => !q || String(text || "").toLowerCase().includes(q);
    const pf = state.platformFilter, rows = allRooms();
    const servers = pf === "all" || pf === "discord" ? serverEntries(state.servers, rows).filter((e) => hit(`${e.name} ${e.id}`)) : [];
    const groups = groupEntries(rows).filter((r) => (pf === "all" || pf === r.platform) && hit(`${roomTitle(r)} ${r.thread_id}`));
    if (servers.length) {
      list.appendChild(el("div", { class: "ch-group-label" }, "Discord servers"));
      servers.forEach((e) => list.appendChild(serverListCard(e)));
    }
    if (groups.length) {
      list.appendChild(el("div", { class: "ch-group-label" }, pf === "all" ? "Group chats" : `${(PLATFORMS[pf] || {}).label || pf} groups`));
      groups.forEach((r) => list.appendChild(groupListCard(r)));
    }
    return servers.length + groups.length;
  }

  // The grey line under a name. A handle-only person has no id yet, so say so
  // instead of printing the handle twice.
  function subLine(p) {
    if (p.placeholder) return "@" + p.handle + " · id arrives with their first message";
    return (nameOf(p) && p.handle ? "@" + p.handle + " · " : "") + p.user_id;
  }

  function select(k) {
    if (state.selected === k) return;
    state.selected = k;
    state.confirm = null;
    state.editingName = null; state.editingHandle = null;
    state.confirmRemove = null;
    state.editingNote = null; state.confirmNote = null; state.forget = null;
    state.toolSearch = "";
    state.presetPrev = null;
    state.presetDone = null;
    renderList();
    renderDetail(true);
    runTabLoader();
  }

  // ---- middle -----------------------------------------------------------
  function nameEditor(p) {
    const input = el("input", { type: "text", class: "ch-name-input", maxlength: "48", autocomplete: "off", spellcheck: "false", "aria-label": "Name", placeholder: "What should Jarvis call them?", value: p.name || "" });
    input.value = p.name || "";
    const save = () => saveName(p, input.value);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); state.editingName = null; renderDetail(); }
    });
    const row = el("div", { class: "ch-name-edit" }, [
      input,
      el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: save }, "Save"),
      p.name ? el("button", { type: "button", class: "btn btn--ghost btn--sm", title: "Remove the name, so Jarvis asks them again", onclick: () => saveName(p, "") }, "Clear") : null,
      el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.editingName = null; renderDetail(); } }, "Cancel"),
    ]);
    setTimeout(() => { if (input.isConnected) { input.focus(); input.select(); } }, 0);
    return row;
  }

  function hero(p) {
    const ids = el("div", { class: "ch-hero__ids" }, [
      el("span", { class: "ch-id" }, [p.placeholder ? "no id yet" : p.user_id, p.placeholder ? null : el("button", { type: "button", onclick: async () => toast((await copyText(p.user_id)) ? "ID copied" : "Couldn't copy", "info") }, "copy")]),
      tag(PLATFORMS[p.platform] ? PLATFORMS[p.platform].label : p.platform, null, p.platform),
    ]);
    if (p.owner) ids.appendChild(tag("Owner", "owner", "owner"));
    if (p.blocked) ids.appendChild(tag("Blocked", "bad", "blocked"));
    const editing = state.editingName === pkey(p) && state.nameAt !== "details";
    const nameLine = editing ? nameEditor(p) : el("div", { class: "ch-hero__nameline" }, [
      el("div", { class: "ch-hero__name" }, displayName(p)),
      el("button", { type: "button", class: "ch-iconbtn", title: "Change name", "aria-label": `Change ${displayName(p)}'s name`, onclick: () => { state.editingName = pkey(p); state.nameAt = "hero"; renderDetail(); } }, [icon("edit")]),
    ]);
    return el("div", { class: "ch-hero", style: `--hue: hsl(${hueFor(pkey(p))} 70% 58%)` }, [
      avatar(p, "lg"),
      el("div", { class: "ch-hero__main" }, [
        nameLine,
        nameOf(p) && p.handle ? el("div", { class: "ch-hero__handle" }, "@" + p.handle) : null,
        ids,
      ]),
    ]);
  }

  function tabs(p) {
    const defs = [
      { id: "profile", label: "Profile", ico: "user" },
      { id: "perms", label: "Permissions", ico: "shield" },
      { id: "conv", label: "Conversation", ico: "chat" },
      { id: "memory", label: "Memory", ico: "memory", title: "What Jarvis keeps about this person" },
      { id: "usage", label: "Usage", ico: "chart" },
      { id: "test", label: "Test", ico: "flask", title: "Test as this person" },
      { id: "history", label: "History", ico: "clock", title: "Changes to this person's access" },
    ];
    const bar = el("div", { class: "ch-tabs", role: "tablist", "aria-label": "Person" });
    defs.forEach((d, i) => {
      const dirty = d.id === "perms" && state.drafts.has(pkey(p));
      const b = el("button", {
        type: "button", class: "ch-tab", role: "tab", id: `ch-tab-${d.id}`, "aria-selected": state.tab === d.id ? "true" : "false",
        "aria-controls": `ch-pane-${d.id}`, tabindex: state.tab === d.id ? "0" : "-1",
        title: d.title || null,
        onclick: () => activateTab(d.id),
        onkeydown: (e) => {
          if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            const next = defs[(i + (e.key === "ArrowRight" ? 1 : defs.length - 1)) % defs.length];
            activateTab(next.id); const nb = $(`#ch-tab-${next.id}`); if (nb) nb.focus();
          }
        },
      }, [icon(d.ico), d.label, dirty ? el("span", { class: "ch-tab__n", title: "Unsaved tool changes" }, "•") : null]);
      bar.appendChild(b);
    });
    return bar;
  }

  function statCard(k, v, sub, ch) {
    return el("div", { class: "ch-stat", "data-ch": ch }, [el("div", { class: "ch-stat__k" }, k), el("div", { class: "ch-stat__v" }, v), el("div", { class: "ch-stat__s" }, sub)]);
  }

  function summaryOf(p) {
    const e = p.effective;
    const reply = p.blocked ? statCard("Replies", "Blocked", "Nothing gets through.", "bad")
      : e.answered ? statCard("Replies", "Answered", p.dm.on ? "In DMs and when mentioned." : "When mentioned (DMs are off).", "on")
        : statCard("Replies", "Ignored", "Not in the reply list.", "off");
    let tools;
    const ends = p.tool_until && !p.tool_expired ? ` Ends ${dateText(p.tool_until)} (${untilLeft(p.tool_until)}).` : "";
    if (e.tools === "all") tools = statCard("Tools", "Everything", "Any tool the platform allows." + ends, "limited");
    else if (e.tools === "custom") tools = statCard("Tools", `${e.tool_count} allowed`, "Only the tools you ticked." + ends, "limited");
    else if (e.tools_blocked_by_platform) tools = statCard("Tools", "Waiting", "Platform master switch is off.", "limited");
    else tools = statCard("Tools", "None", "Answers only, touches nothing.", "off");
    const auth = p.owner ? statCard("Role", "Owner", "Trusted as you, inside chats.", "owner")
      : p.blocked ? statCard("Role", "Blocked", "Off every list.", "bad")
        : statCard("Role", "Guest", p.send_dm ? "Jarvis may DM them for you." : "Jarvis won't DM them.", "off");
    return el("div", { class: "ch-summary" }, [reply, tools, auth]);
  }

  function renderProfile(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-profile", role: "tabpanel", "aria-labelledby": "ch-tab-profile" });
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Right now"), summaryOf(p)]));
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Details"),
      el("dl", { class: "ch-facts" }, [
        nameFact(p),
        handleFact(p),
        el("div", null, [el("dt", null, "First seen"), el("dd", null, dateText(p.first_seen))]),
        el("div", null, [el("dt", null, "Last seen"), el("dd", null, dateText(p.last_seen))]),
        el("div", null, [el("dt", null, "Messages"), el("dd", null, String(p.messages))]),
        p.manual ? el("div", null, [el("dt", null, "Added"), el("dd", null, p.messages ? "by you, then they wrote" : "by you — hasn't written yet")]) : null,
        el("div", null, [el("dt", null, "Follow status"), el("dd", null, p.follow)]),
      ]),
    ]));
    pane.appendChild(linkSection(p));
    const rm = removeSection(p);
    pane.appendChild(notesSection(p));
    const how = instructionSection(p);
    if (how) pane.appendChild(how);
    const msg = messageSection(p);
    if (msg) pane.appendChild(msg);
    if (rm) pane.appendChild(rm);
    const fg = forgetSection(p);
    if (fg) pane.appendChild(fg);
    return pane;
  }

  // ---- name, in the Details list ----------------------------------------
  // The same editor and the same route as the pencil by the name at the top;
  // this just puts the control where "— not told yet" is printed, so an
  // unnamed person can be named from the row that says they have no name.
  function nameFact(p) {
    const editing = state.editingName === pkey(p) && state.nameAt === "details";
    if (editing) return el("div", { class: "ch-facts__wide" }, [el("dt", null, "Name"), el("dd", null, [nameEditor(p)])]);
    const label = p.name || (p.name_from === "linked" ? p.name_effective + " (from the linked account)" : "— not told yet");
    return el("div", null, [el("dt", null, "Name"), el("dd", { class: "ch-namecell" }, [
      el("span", null, label),
      p.name && p.name_locked ? el("span", { class: "ch-row__hint", title: "Set by you. What they tell Jarvis in chat won't replace it." }, " · set by you") : null,
      el("button", {
        type: "button", class: "ch-iconbtn", title: p.name ? "Change name" : "Set a name",
        "aria-label": p.name ? `Change ${displayName(p)}'s name` : `Set a name for ${displayName(p)}`,
        onclick: () => { state.editingName = pkey(p); state.nameAt = "details"; renderDetail(); },
      }, [icon("edit")]),
    ])]);
  }

  // ---- handle, in the Details list  (L.36-P10) ----------------------------
  // Editable only for someone you added by hand who hasn't written yet (the
  // server enforces it; p.handle_editable only decides whether to draw the
  // pencil). Their access moves with the handle, which the hint says.
  async function saveHandle(p, value, errEl) {
    if (errEl) errEl.textContent = "";
    try {
      const out = await personPost(p, "handle", { handle: value });
      state.editingHandle = null;
      // A person known only by handle is keyed by it, so the card's key changes.
      if (out && out.user_id) state.selected = `${p.platform}:${out.user_id}`;
      toast(out.note || "Handle updated.", "success");
    } catch (err) {
      if (errEl) errEl.textContent = err.message || "Couldn't change the handle.";
      else toast(err.message || "Couldn't change the handle.", "error");
      return;
    }
    await load(true);
  }

  function handleFact(p) {
    if (state.editingHandle === pkey(p)) {
      const input = el("input", { type: "text", class: "ch-name-input", maxlength: "64", autocomplete: "off", spellcheck: "false", "aria-label": "Handle", placeholder: "their handle, without the @", value: p.handle || "" });
      input.value = p.handle || "";
      const err = el("div", { class: "ch-row__hint", role: "alert" });
      const save = () => saveHandle(p, input.value, err);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); state.editingHandle = null; renderDetail(); }
      });
      setTimeout(() => { if (input.isConnected) { input.focus(); input.select(); } }, 0);
      return el("div", { class: "ch-facts__wide" }, [el("dt", null, "Handle"), el("dd", null, [
        el("div", { class: "ch-name-edit" }, [
          input,
          el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: save }, "Save"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.editingHandle = null; renderDetail(); } }, "Cancel"),
        ]),
        el("div", { class: "ch-row__hint" }, "If this handle is on a list for them, the new one takes its place. You can only edit it until they write; after that the platform's own handle is used."),
        err,
      ])]);
    }
    return el("div", null, [el("dt", null, "Handle"), el("dd", { class: "ch-namecell" }, [
      el("span", null, p.handle ? "@" + p.handle : "—"),
      p.handle_editable ? el("button", {
        type: "button", class: "ch-iconbtn", title: "Correct the handle",
        "aria-label": `Correct ${displayName(p)}'s handle`,
        onclick: () => { state.editingHandle = pkey(p); renderDetail(); },
      }, [icon("edit")]) : null,
    ])]);
  }

  // ---- what Jarvis remembers: edit / delete a note ------------------------
  async function noteAction(p, body, okMsg) {
    try {
      await personPost(p, "note", body);
      state.editingNote = null; state.confirmNote = null;
      toast(okMsg, "success");
    } catch (err) {
      toast(err.message || "Couldn't change the note.", "error");
      state.confirmNote = null;
    }
    await load(true);
  }

  function noteItemBase(p, n) {
    const k = pkey(p);
    const editing = state.editingNote && state.editingNote.key === k && state.editingNote.id === n.id;
    const asking = state.confirmNote && state.confirmNote.key === k && state.confirmNote.id === n.id;
    if (editing) {
      const input = el("input", { type: "text", class: "ch-name-input ch-note-input", maxlength: "160", autocomplete: "off", spellcheck: "false", "aria-label": "Note", value: n.text });
      input.value = n.text;
      const save = () => { const v = input.value.trim(); if (!v) { toast("A note can't be empty — use Delete to remove it.", "error"); return; } noteAction(p, { action: "edit", note: n.id, text: v }, "Note updated."); };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); state.editingNote = null; renderDetail(); }
      });
      setTimeout(() => { if (input.isConnected) { input.focus(); input.select(); } }, 0);
      return el("li", { class: "ch-note is-editing" }, [el("div", { class: "ch-name-edit" }, [
        input,
        el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: save }, "Save"),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.editingNote = null; renderDetail(); } }, "Cancel"),
      ])]);
    }
    if (asking) {
      return el("li", { class: "ch-note" }, [el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm deleting this note" }, [
        el("div", { class: "ch-confirm__text" }, `Delete “${n.text}”? Jarvis stops using it right away.`),
        el("div", { class: "ch-confirm__btns" }, [
          el("button", { type: "button", class: "btn btn--danger btn--sm", onclick: () => noteAction(p, { action: "delete", note: n.id }, "Note deleted.") }, "Delete"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.confirmNote = null; renderDetail(); } }, "Keep"),
        ]),
      ])]);
    }
    return el("li", { class: "ch-note" }, [
      el("span", { class: "ch-note__text" }, n.text),
      el("span", { class: "ch-note__btns" }, [
        el("button", { type: "button", class: "ch-iconbtn", title: "Edit this note", "aria-label": "Edit this note", onclick: () => { state.confirmNote = null; state.editingNote = { key: k, id: n.id }; renderDetail(); } }, [icon("edit")]),
        el("button", { type: "button", class: "ch-iconbtn ch-iconbtn--danger", title: "Delete this note", "aria-label": "Delete this note", onclick: () => { state.editingNote = null; state.confirmNote = { key: k, id: n.id }; renderDetail(); } }, [icon("trash")]),
      ]),
    ]);
  }

  // The note row, plus (when it isn't being edited or deleted) its "In rooms" setting (S11).
  function noteItem(p, n) {
    const li = noteItemBase(p, n);
    const k = pkey(p);
    const busy = [state.editingNote, state.confirmNote].some((x) => x && x.key === k && x.id === n.id);
    if (!busy) { li.classList.add("ch-note--groups"); li.appendChild(groupsControl(p, "note", n)); }
    return li;
  }

  function notesSection(p) {
    const items = Array.isArray(p.note_items) ? p.note_items : [];
    const sec = el("div", null, [
      el("div", { class: "ch-section-title" }, ["What Jarvis remembers", el("span", { class: "ch-section-title__aside" }, `${items.length}`)]),
    ]);
    if (items.length) sec.appendChild(el("ul", { class: "ch-notes ch-notes--edit" }, items.map((n) => noteItem(p, n))));
    else sec.appendChild(el("div", { class: "ch-row__hint" }, "Nothing yet. Notes come from the person telling Jarvis about themselves; they stay in this record and never enter your own memory."));
    if (items.length) sec.appendChild(el("div", { class: "ch-row__hint" }, "Your edits go straight into what Jarvis is told about them. A new note from them can still push the oldest one out — only six are kept."));
    if (p.linked) sec.appendChild(el("div", { class: "ch-row__hint" }, "Notes saved on their linked account are shared with Jarvis too; edit those on that account."));
    return sec;
  }

  // ---- how Jarvis should talk to them (L.36-P12) ----------------------------
  // One owner-typed line, shown to the model as "your owner's note on how to
  // talk to them". Style only: it never changes what they may do. Not shown
  // for the owner's own account (Jarvis treats that as you at the PC).
  function instructionSection(p) {
    if (p.owner) return null;
    const k = pkey(p);
    const max = p.instruction_max || 240;
    const saved = p.instruction || "";
    const draft = state.instr.has(k) ? state.instr.get(k) : saved;
    const busy = state.busy.has(`${k}|instruction`);
    const input = el("input", { type: "text", class: "ch-name-input ch-instr-input", maxlength: String(max), autocomplete: "off", "aria-label": "How Jarvis should talk to them", placeholder: "e.g. Keep it short and casual with her.", disabled: busy });
    input.value = draft;
    const count = el("span", { class: "ch-instr__count" }, `${draft.length}/${max}`);
    const save = el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: busy || draft.trim() === saved, onclick: () => saveInstruction(p, input.value) }, busy ? "Saving…" : "Save");
    input.addEventListener("input", () => {
      state.instr.set(k, input.value);
      count.textContent = `${input.value.length}/${max}`;
      save.disabled = input.value.trim() === saved;
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !save.disabled) { e.preventDefault(); saveInstruction(p, input.value); }
    });
    const row = el("div", { class: "ch-name-edit" }, [
      input, save,
      saved ? el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, onclick: () => saveInstruction(p, "") }, "Clear") : null,
    ]);
    return el("div", null, [
      el("div", { class: "ch-section-title" }, ["How Jarvis talks to them", el("span", { class: "ch-section-title__aside" }, saved ? "set" : "none")]),
      row,
      el("div", { class: "ch-instr__meta" }, [count]),
      el("div", { class: "ch-row__hint" }, "Your own words on tone, length or language — Jarvis is told the note is from you. It only changes style: it can't give them any access, and they can't see or change it. Applies to this account only."),
    ]);
  }

  // ---- message them from here (L.36-P13) ---------------------------------
  // The owner writes ONE message to ONE person and clicks Send. Two steps: Send
  // asks the server for a PREVIEW (who gets exactly what, how much of the
  // hourly allowance is left, nothing sent); "Send it" is the only click that
  // delivers. The server runs the send_dm tool's own code, so the limits and the
  // refusals (owner, blocked, DMs off, placeholder, rate) are the tool's, not
  // copies kept here. The message is plain text and is never put in markup.
  function dmSlot(p) {
    const k = pkey(p);
    if (!state.dm.has(k)) state.dm.set(k, { text: "", status: "idle", data: null, error: "", req: 0 });
    return state.dm.get(k);
  }
  async function dmCall(p, confirm) {
    const d = dmSlot(p);
    const req = ++d.req;
    d.status = confirm ? "sending" : "loading"; d.error = "";
    renderDetail();
    try {
      const data = await api("POST", `/api/channels/people/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.user_id)}/dm`, { text: d.text, confirm: !!confirm });
      if (req !== d.req) return;
      if (confirm) { d.status = "sent"; d.data = data; d.text = ""; toast(`Sent to ${displayName(p)}.`, "success"); }
      else { d.status = "ready"; d.data = data; }
    } catch (e) {
      if (req !== d.req) return;
      d.status = "error"; d.error = e.message || "Couldn't send.";
    }
    renderDetail();
  }
  function messageSection(p) {
    if (p.owner) return null;
    const d = dmSlot(p);
    const box = el("div", { class: "ch-dm" });
    const why = p.blocked ? "Blocked — unblock them first."
      : p.placeholder ? "Known only by handle, so there's no id to message yet. They can be messaged after their first message."
      : !p.send_dm ? "Switch on “Jarvis may DM them” under Permissions first."
      : "";
    const area = el("textarea", { class: "ch-name-input ch-dm__text", rows: "3", maxlength: "1500", "aria-label": `Message to ${displayName(p)}`, placeholder: "Write one message…", disabled: !!why || d.status === "loading" || d.status === "sending" });
    area.value = d.text;
    const count = el("span", { class: "ch-instr__count" }, `${d.text.length}/1500`);
    const go = el("button", { type: "button", class: "btn btn--primary btn--sm", id: "ch-dm-preview", disabled: !!why || !d.text.trim() || d.status === "loading" || d.status === "sending", onclick: () => dmCall(p, false) }, d.status === "loading" ? "Checking…" : "Preview");
    area.addEventListener("input", () => {
      d.text = area.value; count.textContent = `${d.text.length}/1500`;
      if (d.status === "ready" || d.status === "sent" || d.status === "error") { d.status = "idle"; d.data = null; d.error = ""; renderDetail(); area.focus(); return; }
      go.disabled = !d.text.trim();
    });
    box.appendChild(area);
    box.appendChild(el("div", { class: "ch-instr__meta" }, [count]));
    if (why) box.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"), el("span", null, why)]));
    if (d.status === "error") box.appendChild(el("div", { class: "ch-row__note ch-row__note--bad", role: "alert" }, [icon("warn"), el("span", null, d.error)]));
    if (d.status === "sent") box.appendChild(el("div", { class: "ch-row__note ch-row__note--info", role: "status" }, [icon("info"), el("span", null, "Sent. It's in their Conversation tab.")]));
    if (d.status === "ready" && d.data) {
      const r = d.data.rate || {};
      const who = d.data.to || {};
      const lines = [
        el("div", { class: "ch-confirm__text" }, `Send this to ${displayName(p)} on ${PLATFORMS[p.platform] ? PLATFORMS[p.platform].label : p.platform}? It goes out exactly as written (${d.data.chars} characters).`),
        el("div", { class: "ch-dm__quote" }, d.data.message),
        el("div", { class: "ch-row__hint" }, `Allowance this hour: ${r.recipient_used}/${r.recipient_max} to them, ${r.overall_used}/${r.overall_max} overall — shared with the send_dm tool.`),
      ];
      if (d.data.limited) lines.push(el("div", { class: "ch-row__note ch-row__note--bad" }, [icon("warn"), el("span", null, d.data.limited)]));
      const send = el("button", { type: "button", class: "btn btn--primary btn--sm", id: "ch-dm-send", disabled: !!d.data.limited, onclick: () => dmCall(p, true) }, "Send it");
      const cancel = el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { d.status = "idle"; d.data = null; renderDetail(); } }, "Cancel");
      box.appendChild(el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm message" }, [...lines, el("div", { class: "ch-confirm__btns" }, [send, cancel])]));
    } else if (!why) {
      box.appendChild(el("div", { class: "ch-confirm__btns" }, [go]));
    }
    box.appendChild(el("div", { class: "ch-row__hint" }, "Sent as the bot, in your words, to this one person. Same limits and log as when Jarvis does it for you. Nothing here can give them access."));
    return el("div", null, [el("div", { class: "ch-section-title" }, "Send them a message"), box]);
  }

  // ---- forget this person (L.36-P11) --------------------------------------
  // The one real wipe. Opening it asks the server for a PREVIEW (the same code
  // as the real thing, nothing written); a refusal (the owner, someone a "*"
  // entry covers, unreadable limits) shows here instead of a button that would
  // fail. The chat history is a separate tick-box, off by default.
  async function openForget(p) {
    const req = Date.now();
    state.forget = { key: pkey(p), history: false, status: "loading", data: null, error: "", req };
    renderDetail();
    try {
      const out = await personPost(p, "forget", { history: false });
      if (state.forget && state.forget.req === req) { state.forget.status = "ready"; state.forget.data = out.report || {}; }
    } catch (err) {
      if (state.forget && state.forget.req === req) { state.forget.status = "error"; state.forget.error = err.message || "Couldn't check."; }
    }
    renderDetail();
  }

  async function forgetPerson(p) {
    const f = state.forget;
    if (!f || f.key !== pkey(p) || f.status === "busy") return;
    f.status = "busy"; renderDetail();
    try {
      const out = await personPost(p, "forget", { history: !!f.history, confirm: true });
      const k = pkey(p);
      state.conv.delete(k); state.usage.delete(k); state.sim.delete(k); state.hist.delete(k); state.mem.delete(k); state.memDraft.delete(k); state.ruleDraft.delete(k); state.drafts.delete(k);
      state.forget = null;
      toast(`${displayName(p)} forgotten.`, "success");
      if (out && out.report && out.report.history_purged && out.report.history && out.report.history.left_in_groups) {
        toast(`${out.report.history.left_in_groups} of their lines in group chats were left, because other people wrote in those files.`, "info");
      }
    } catch (err) {
      f.status = "ready";
      toast(err.message || "Couldn't forget them.", "error");
      await load(true); // they may be half-forgotten (off the lists already): show what is true now
      return;
    }
    await load(false);
  }

  function forgetSection(p) {
    if (p.owner) return null;
    if (p.manual && !p.messages) return null; // "Remove" covers someone who never wrote
    const f = state.forget && state.forget.key === pkey(p) ? state.forget : null;
    const sec = el("div", null, [el("div", { class: "ch-section-title" }, "Forget")]);
    if (!f) {
      sec.appendChild(el("div", { class: "ch-gset__add" }, [
        el("div", { class: "ch-row__hint", style: "flex:1" }, "Delete their record, notes and limits and take them off every list, as if they'd never written. Blocking keeps the record; this doesn't."),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => openForget(p) }, "Forget this person…"),
      ]));
      return sec;
    }
    const close = el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.forget = null; renderDetail(); } }, "Cancel");
    if (f.status === "loading") { sec.appendChild(el("div", { class: "ch-skel" })); return sec; }
    if (f.status === "error") {
      sec.appendChild(el("div", { class: "ch-row__note ch-row__note--bad" }, [icon("warn"), el("span", null, f.error)]));
      sec.appendChild(el("div", { class: "ch-confirm__btns" }, [close]));
      return sec;
    }
    const d = f.data || {};
    const h = d.history || {};
    const lists = (d.lists || []).map((w) => ({ dm_allowlist: "DM", reply_allowlist: "reply", tool_allowlist: "tool" }[w] || w));
    const will = [
      lists.length ? `Taken off the ${lists.join(", ")} list${lists.length === 1 ? "" : "s"}.` : "They're on none of the lists already.",
      d.limits ? "Their tool limits and DM setting are deleted." : null,
      `Their record is deleted: name, handle, first and last seen${d.notes ? `, and ${d.notes} remembered note${d.notes === 1 ? "" : "s"}` : ""}.`,
      d.linked ? `Their ${(PLATFORMS[d.linked.platform] || {}).label || d.linked.platform} account${d.linked.name ? " (" + d.linked.name + ")" : ""} stays; only the link goes.` : null,
      "If they message again they're a new person: Jarvis asks you about them again, with nothing switched on.",
    ].filter(Boolean);
    const box = el("input", { type: "checkbox", id: "ch-forget-history", "aria-describedby": "ch-forget-history-hint" });
    box.checked = !!f.history;
    box.addEventListener("change", () => { f.history = box.checked; renderDetail(); });
    const historyText = h.threads
      ? `${h.threads} private chat${h.threads === 1 ? "" : "s"}, ${h.messages} message${h.messages === 1 ? "" : "s"}, and the conversation Jarvis keeps for ${h.threads === 1 ? "it" : "them"}.`
      : "No private chat history on file.";
    const kept = [];
    if (h.left_in_groups) kept.push(`${h.left_in_groups} of their lines in group chats stay — other people's messages are in those files.`);
    kept.push("Usage counts (numbers only, no text) stay.");
    sec.appendChild(el("div", { class: "ch-confirm ch-confirm--col", role: "alertdialog", "aria-label": "Confirm forgetting this person" }, [
      el("div", { class: "ch-confirm__text" }, `Forget ${displayName(p)}? This can't be undone.`),
      el("ul", { class: "ch-forget__list" }, will.map((t) => el("li", null, t))),
      el("label", { class: "ch-forget__history", for: "ch-forget-history" }, [box, el("span", null, [
        el("b", null, "Also delete our chat history with them"),
        el("span", { class: "ch-row__hint", id: "ch-forget-history-hint" }, ` ${historyText}`),
      ])]),
      el("div", { class: "ch-row__hint" }, kept.join(" ")),
      el("div", { class: "ch-confirm__btns" }, [
        el("button", { type: "button", class: "btn btn--danger btn--sm", disabled: f.status === "busy", onclick: () => forgetPerson(p) }, f.status === "busy" ? "Forgetting…" : "Forget"),
        close,
      ]),
    ]));
    return sec;
  }

  // ---- linked account ---------------------------------------------------
  function linkSection(p) {
    const other = otherPlatform(p.platform);
    const oname = PLATFORMS[other].label;
    const sec = el("div", null, [el("div", { class: "ch-section-title" }, "Same person on " + oname)]);
    if (p.linked) {
      const l = p.linked;
      const target = state.people.find((x) => x.platform === l.platform && x.user_id === l.user_id);
      const who = l.name || (l.handle ? "@" + l.handle : l.user_id);
      sec.appendChild(el("div", { class: "ch-link" }, [
        avatar(target || { platform: l.platform, user_id: l.user_id, handle: l.handle, name: l.name, avatar: l.avatar }),
        el("div", { class: "ch-link__who" }, [
          el("div", { class: "ch-link__name" }, who),
          el("div", { class: "ch-row__hint" }, `${PLATFORMS[l.platform] ? PLATFORMS[l.platform].label : l.platform}${l.handle && l.name ? " · @" + l.handle : ""}${l.owner ? " · the owner account" : ""}`),
        ]),
        target ? el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => select(pkey(target)) }, "Open") : null,
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => unlinkPerson(p) }, "Unlink"),
      ]));
      if (l.owner && !p.owner) {
        sec.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"), el("span", null, `Linking doesn't make this account the owner. If this is you, switch Owner on for it under Permissions.`)]));
      }
    } else {
      const input = el("input", { type: "text", class: "ch-link-input", list: "ch-link-options", placeholder: `Their ${oname} id, @handle or name`, autocomplete: "off", maxlength: "64", "aria-label": `${oname} account to link` });
      const err = el("div", { class: "ch-gset__err" });
      const go = () => { const v = input.value.trim(); if (!v) { err.textContent = "Type their id, @handle or name."; return; } linkPerson(p, v, err); };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
      const options = el("datalist", { id: "ch-link-options" });
      for (const x of state.people.filter((x) => x.platform === other)) {
        if (x.name) options.appendChild(el("option", { value: x.name }, x.handle ? "@" + x.handle : x.user_id));
        if (x.handle) options.appendChild(el("option", { value: "@" + x.handle }, x.name || ""));
      }
      sec.appendChild(el("div", { class: "ch-gset__add" }, [input, options, el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: go }, "Link")]));
      sec.appendChild(err);
      sec.appendChild(el("div", { class: "ch-row__hint" }, `One human on both apps. Jarvis then shares their name and notes between the two accounts. It doesn't share permissions — each account keeps its own switches. An ${oname} id or @handle that isn't on the list yet is added for you.`));
    }
    return sec;
  }

  // Only someone added by hand who has never written can be removed; anyone
  // else is blocked instead (their history can't be regenerated).
  function removeSection(p) {
    if (!p.manual || p.messages || p.owner) return null;
    const asking = state.confirmRemove === pkey(p);
    const sec = el("div", null, [el("div", { class: "ch-section-title" }, "Remove")]);
    if (!asking) {
      sec.appendChild(el("div", { class: "ch-gset__add" }, [
        el("div", { class: "ch-row__hint", style: "flex:1" }, "You added them and they haven't written. Removing takes them off every list too."),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.confirmRemove = pkey(p); renderDetail(); } }, "Remove person"),
      ]));
    } else {
      sec.appendChild(el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm removal" }, [
        el("div", { class: "ch-confirm__text" }, `Remove ${displayName(p)}? They leave the DM, reply and tool lists.`),
        el("div", { class: "ch-confirm__btns" }, [
          el("button", { type: "button", class: "btn btn--danger btn--sm", onclick: () => removePerson(p) }, "Remove"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.confirmRemove = null; renderDetail(); } }, "Cancel"),
        ]),
      ]));
    }
    return sec;
  }

  // One switch row. `opts`: flag, ch (colour when on), hint, danger, locked
  // (string reason or ""), notes [{kind, text}], extra (nodes below the row).
  function permRow(p, o) {
    const entry = p[o.flag];
    const on = o.on !== undefined ? o.on : (entry && typeof entry === "object" ? !!entry.on : !!entry);
    const busy = state.busy.has(`${pkey(p)}|${o.flag}`);
    const locked = o.locked || "";
    const confirming = state.confirm && state.confirm.key === pkey(p) && state.confirm.flag === o.flag;
    const row = el("div", {
      class: "ch-row" + (on ? " is-on" : "") + (locked ? " is-locked" : "") + (busy ? " is-busy" : "") + (o.danger ? " ch-row--danger" : "") + (o.extra ? " ch-row--tools" : ""),
      "data-ch": on ? o.ch : "off", "data-flag": o.flag,
    });
    row.appendChild(el("div", { class: "ch-row__icon" }, [icon(o.icon || o.flag)]));
    const body = el("div", null, [
      el("div", { class: "ch-row__label" }, [o.label, ...(o.tags || [])]),
      el("div", { class: "ch-row__hint" }, o.hint),
    ]);
    for (const n of o.notes || []) {
      body.appendChild(el("div", { class: "ch-row__note" + (n.kind === "bad" ? " ch-row__note--bad" : n.kind === "info" ? " ch-row__note--info" : "") }, [icon(n.kind === "info" ? "info" : "warn"), el("span", null, n.text)]));
    }
    row.appendChild(body);

    const sw = el("button", {
      type: "button", class: "ch-switch", role: "switch", "aria-checked": on ? "true" : "false", "aria-label": `${o.label}: ${on ? "on" : "off"}`,
      disabled: !!locked || busy, title: locked || null,
      onclick: () => {
        if (locked) return;
        const turningOn = !on;
        if (turningOn && o.confirmOn) { state.confirm = { key: pkey(p), flag: o.flag }; renderDetail(); return; }
        if (!turningOn && o.confirmOff) { state.confirm = { key: pkey(p), flag: o.flag, off: true }; renderDetail(); return; }
        setFlag(p, o.flag, turningOn);
      },
    });
    row.appendChild(locked ? el("div", { class: "ch-row__lock", title: locked }, [icon("lock"), "locked"]) : sw);
    if (locked) row.appendChild(el("div", { class: "ch-row__note ch-row__note--info", style: "grid-column:2 / -1;margin-top:-4px" }, [icon("lock"), el("span", null, locked)]));

    if (confirming) {
      const c = state.confirm;
      const yes = (label, fn, cls) => el("button", { type: "button", class: "btn btn--sm " + (cls || "btn--danger"), onclick: fn }, label);
      const cancel = el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.confirm = null; renderDetail(); } }, "Cancel");
      let text, btns;
      if (o.flag === "tool" && c.timed) {
        const when = (TIME_LIMITS.find((t) => t.minutes === c.timed) || {}).label || `${c.timed} minutes`;
        const custom = p.tools.mode === "custom";
        const listed = custom ? (p.tools.allow || []).length : 0;
        text = `${displayName(p)} will be able to make Jarvis run tools on this PC for ${when}, then it switches off by itself.`
          + (custom ? (listed ? ` Only the ${listed} tool${listed === 1 ? "" : "s"} you ticked.` : " Their tool list is empty, so tick the tools you want them to have.") : " Pick how much to trust them:");
        btns = custom
          ? [yes(`Allow for ${when}`, () => { state.confirm = null; grantTools(p, c.timed, false); }, "btn--primary"), cancel]
          : [
            // Restriction first, permission second (see grantTools).
            yes("Only tools I choose", () => { state.confirm = null; grantTools(p, c.timed, true); }, "btn--primary"),
            yes("Every tool", () => { state.confirm = null; grantTools(p, c.timed, false); }),
            cancel,
          ];
      } else if (o.flag === "tool" && !c.off) {
        text = `${displayName(p)} will be able to make Jarvis run tools on this PC. Pick how much to trust them:`;
        btns = [
          // Restriction first, permission second: if the second call fails they
          // have an empty custom list (no tools), never a window with everything.
          yes("Only tools I choose", async () => {
            state.confirm = null;
            try {
              await api("POST", `/api/channels/people/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.user_id)}/tools`, { mode: "custom", tools: [] });
            } catch (e) { toast(e.message || "Couldn't set the tool list.", "error"); await load(true); return; }
            await setFlag(p, "tool", true);
          }, "btn--primary"),
          yes("Every tool", async () => { state.confirm = null; await setFlag(p, "tool", true); }),
          cancel,
        ];
      } else if (o.flag === "owner" && !c.off) {
        const holder = state.people.find((x) => x.platform === p.platform && x.owner && x.user_id !== p.user_id);
        text = `Make ${displayName(p)} the ${PLATFORMS[p.platform].label} owner? Jarvis will treat them as you inside chats and notify them instead of ${holder ? displayName(holder) : "no one"}.`;
        btns = [yes("Make owner", () => { state.confirm = null; setFlag(p, "owner", true); }), cancel];
      } else if (o.flag === "owner") {
        text = `Remove ${displayName(p)} as the ${PLATFORMS[p.platform].label} owner? Jarvis will have nobody to notify there until you pick one.`;
        btns = [yes("Remove owner", () => { state.confirm = null; setFlag(p, "owner", false); }), cancel];
      } else if (o.flag === "blocked") {
        text = `Block ${displayName(p)}? They're removed from the DM, reply and tool lists, and Jarvis stops asking about them.`;
        btns = [yes("Block", () => { state.confirm = null; setFlag(p, "blocked", true); }), cancel];
      } else {
        text = "Are you sure?";
        btns = [yes("Confirm", () => { state.confirm = null; setFlag(p, o.flag, !on); }), cancel];
      }
      row.appendChild(el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm" }, [el("div", { class: "ch-confirm__text" }, text), el("div", { class: "ch-confirm__btns" }, btns)]));
    }
    if (o.extra) row.appendChild(o.extra);
    return row;
  }

  // ---- tool scope --------------------------------------------------------
  function draftFor(p) {
    const k = pkey(p);
    return state.drafts.get(k) || scopeOf(p);
  }
  function setDraft(p, d) {
    const k = pkey(p);
    if (sameScope(d, scopeOf(p))) state.drafts.delete(k); else state.drafts.set(k, d);
    renderDetail();
  }

  function toolScopePanel(p) {
    const d = draftFor(p);
    const wrap = el("div", { class: "ch-toolscope" });
    const seg = el("div", { class: "ch-seg", role: "radiogroup", "aria-label": "Which tools" }, [
      el("button", { type: "button", role: "radio", "aria-checked": d.mode === "inherit" ? "true" : "false", onclick: () => setDraft(p, { mode: "inherit", names: new Set(d.names) }) }, "Every tool"),
      el("button", { type: "button", role: "radio", "aria-checked": d.mode === "custom" ? "true" : "false", onclick: () => { setDraft(p, { mode: "custom", names: new Set(d.names) }); ensureTools(); } }, "Only selected"),
    ]);
    wrap.appendChild(seg);
    if (d.mode === "inherit") {
      wrap.appendChild(el("div", { class: "ch-row__hint" }, "No limit beyond the platform's own: they can reach any tool the owner hasn't switched off in the Tool Manager."));
      return wrap;
    }
    if (state.toolsState === "error") {
      wrap.appendChild(el("div", { class: "ch-row__note ch-row__note--bad" }, [icon("warn"), el("span", null, state.toolsError)]));
      return wrap;
    }
    if (!state.tools) { wrap.appendChild(el("div", { class: "ch-skel" })); return wrap; }

    const groups = groupTools(state.tools, state.toolSearch);
    const total = state.tools.length;
    const picked = [...d.names].filter((n) => p.owner || !OWNER_ONLY.has(n)).length;
    const search = el("input", { type: "search", class: "ch-picker__search", placeholder: "Search tools…", autocomplete: "off", "aria-label": "Search tools", value: state.toolSearch });
    search.addEventListener("input", () => {
      state.toolSearch = search.value;
      const pos = search.selectionStart;
      renderDetail();
      const again = $(".ch-picker__search"); if (again) { again.focus(); try { again.setSelectionRange(pos, pos); } catch (_) { /* type=search */ } }
    });
    const mutate = (fn) => { const names = new Set(d.names); fn(names); setDraft(p, { mode: "custom", names }); };
    const selectable = (t) => p.owner || !OWNER_ONLY.has(t.name);
    wrap.appendChild(el("div", { class: "ch-picker__bar" }, [
      search,
      el("span", { class: "ch-picker__count" }, [el("b", null, String(picked)), ` of ${total}`]),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => mutate((n) => state.tools.filter(selectable).forEach((t) => n.add(t.name))) }, "All"),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => mutate((n) => n.clear()) }, "None"),
    ]));
    if (!picked) wrap.appendChild(el("div", { class: "ch-row__note" }, [icon("warn"), el("span", null, "Nothing selected — they'll get answers with no tools at all.")]));

    const list = el("div", { class: "ch-picker__list", role: "group", "aria-label": "Tools" });
    if (!groups.length) list.appendChild(el("div", { class: "ch-empty" }, "No tools match."));
    for (const g of groups) {
      const allOn = g.items.filter(selectable).every((t) => d.names.has(t.name));
      const n = g.items.filter((t) => d.names.has(t.name) && selectable(t)).length;
      list.appendChild(el("div", { class: "ch-pgroup__head" }, [
        g.id,
        el("span", { class: "ch-pgroup__n" }, `${n}/${g.items.length}`),
        el("button", { type: "button", onclick: () => mutate((s) => g.items.filter(selectable).forEach((t) => (allOn ? s.delete(t.name) : s.add(t.name)))) }, allOn ? "clear" : "all"),
      ]));
      for (const t of g.items) {
        const lock = !selectable(t);
        const checked = d.names.has(t.name) && !lock;
        const input = el("input", { type: "checkbox", checked: checked ? "" : null, disabled: lock ? "" : null, "aria-label": t.name });
        input.checked = checked;
        input.addEventListener("change", () => mutate((s) => (input.checked ? s.add(t.name) : s.delete(t.name))));
        const tags = [];
        if (lock) tags.push(tag("owner only", null, "lock"));
        if (t.confirm) tags.push(tag("asks first", "limited"));
        if (t.disabled) tags.push(tag("off in Tool Manager", "off"));
        list.appendChild(el("label", { class: "ch-tool" + (checked ? " is-checked" : "") + (lock ? " is-locked" : ""), title: lock ? "Only the owner can use this tool." : null }, [
          input, el("span", { class: "ch-tool__box" }),
          el("span", { style: "min-width:0" }, [el("div", { class: "ch-tool__name" }, t.name), t.description ? el("div", { class: "ch-tool__desc" }, t.description) : null]),
          el("span", { class: "ch-tool__tags" }, tags),
        ]));
      }
    }
    wrap.appendChild(list);
    wrap.appendChild(el("div", { class: "ch-row__hint" }, `Also available to them, because discovery needs it and none of them can act on the PC: ${PLUMBING.join(", ")}.`));
    return wrap;
  }

  // ---- time-limited tool access (L.36-P6) ----------------------------------
  // Sits under the Tool use switch. The grant is the ordinary tool list entry;
  // this only adds an end time. With tools on it narrows (no confirmation
  // needed); from off it WIDENS access, so it asks first, like the switch.
  function timeLimitPanel(p) {
    if (p.owner || p.blocked || p.tool.via === "wildcard") return null;
    const k = pkey(p);
    const busy = state.busy.has(`${k}|tool`);
    const on = !!p.tool.on;
    const counting = on && p.tool_until && !p.tool_expired;
    let head;
    let label;
    if (counting) {
      head = el("div", { class: "ch-row__hint" }, [`Tool use ends ${dateText(p.tool_until)} (${untilLeft(p.tool_until)}), then switches off by itself.`]);
      label = "Change to";
    } else if (on) {
      head = el("div", { class: "ch-row__hint" }, "No time limit — tool use stays on until you switch it off.");
      label = "End it after";
    } else {
      head = el("div", { class: "ch-row__hint" }, "Or let them use tools for a limited time; it switches off by itself.");
      label = "Allow for";
    }
    const btns = TIME_LIMITS.map((t) => el("button", {
      type: "button", class: "btn btn--ghost btn--sm", disabled: busy,
      onclick: () => {
        if (on) { grantTools(p, t.minutes, false); return; }
        state.confirm = { key: k, flag: "tool", timed: t.minutes };
        renderDetail();
      },
    }, t.label));
    if (counting) btns.push(el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, onclick: () => setFlag(p, "tool", true), title: "Remove the time limit" }, "Keep on for good"));
    return el("div", { class: "ch-timelimit" }, [
      head,
      el("div", { class: "ch-timelimit__btns" }, [el("span", { class: "ch-timelimit__label" }, label), ...btns]),
    ]);
  }

  function renderPerms(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-perms", role: "tabpanel", "aria-labelledby": "ch-tab-perms" });
    const plat = state.platforms[p.platform] || {};
    const pname = PLATFORMS[p.platform] ? PLATFORMS[p.platform].label : p.platform;
    const blockedLock = p.blocked ? "Blocked — unblock them to change this." : "";
    // A "*" entry covers everyone, so there is no per-person way to switch
    // someone off (the gate has no deny-list). Lock the row and say why,
    // rather than offering a switch the server would have to refuse.
    const wild = (m, label) => (m.via === "wildcard" ? `Covered by "*" (everyone) in the ${label} list. Remove "*" under Global lists to control people one by one.` : "");
    const lockFor = (m, label) => blockedLock || wild(m, label);

    if (state.permsError) pane.appendChild(el("div", { class: "ch-banner", style: "margin:0" }, `Per-person limits can't be read (${state.permsError}). Until that's fixed Jarvis answers these people without tools, and the tool and DM switches below can't be saved.`));
    if (!plat.enabled) pane.appendChild(el("div", { class: "ch-banner", style: "margin:0;border-color:var(--border);background:transparent" }, `${pname} isn't enabled, so nothing below has any effect yet. Run: jarvis channels-set ${p.platform} enabled true`));

    // --- Quick setups (P4)
    const quick = presetSection(p);
    if (quick) pane.appendChild(quick);

    // --- Access
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Access"),
      el("div", { class: "ch-rows" }, [
        permRow(p, { flag: "dm", icon: "dm", ch: "on", label: "Direct messages", hint: "May open a DM conversation with the bot at all.", locked: lockFor(p.dm, "DM") }),
        permRow(p, { flag: "reply", icon: "reply", ch: "on", label: "Replies", hint: "Gets an actual answer back. Without this Jarvis stays silent.", locked: lockFor(p.reply, "reply") }),
      ]),
    ]));

    // --- Capabilities
    const toolNotes = [];
    if (p.effective.tools_blocked_by_platform) toolNotes.push({ kind: "warn", text: `The ${pname} master switch (allow_tools) is off, so this has no effect yet. Run: jarvis channels-set ${p.platform} allow_tools true` });
    if (p.tool.on && !p.reply.on) toolNotes.push({ kind: "warn", text: "They aren't in the reply list, so Jarvis never answers them and tools never come into play." });
    if (p.owner && p.tool.on) toolNotes.push({ kind: "info", text: "A custom list limits the owner inside chats too. The PC and terminal are never limited." });
    const toolsRow = permRow(p, {
      flag: "tool", icon: "tool", ch: "limited", label: "Tool use",
      hint: "May make Jarvis run tools on this PC from chat. Turn it on, then choose exactly which tools.",
      locked: lockFor(p.tool, "tool"), confirmOn: true, notes: toolNotes,
      tags: p.tool.on ? [tag(p.tools.mode === "custom" ? "custom list" : "every tool", "limited"), ...(p.tool_until && !p.tool_expired ? [tag(`ends ${untilLeft(p.tool_until)}`, "limited")] : [])] : [],
      extra: (() => {
        const scope = p.tool.on || p.tool.via === "wildcard" ? toolScopePanel(p) : null;
        const limit = timeLimitPanel(p);
        return scope || limit ? el("div", { class: "ch-toolextra" }, [scope, limit]) : null;
      })(),
    });
    // L.36-P8: pictures. The owner's are always read; anyone else only when
    // this is switched on. Read by channels/media_in.py (Discord pictures, L.42.3).
    const imgNotes = [{ kind: "info", text: "On Discord and Instagram Jarvis reads pictures this person sends (a vision provider describes each, up to 3 per message), and keeps a copy with the conversation so you can see it." }];
    if (p.image.on && !p.reply.on) imgNotes.push({ kind: "warn", text: "They aren't in the reply list, so Jarvis never answers them and pictures never come into play." });
    const imagesRow = permRow(p, {
      flag: "image", icon: "image", ch: "on", label: "Pictures",
      hint: "May send Jarvis pictures to read. Off by default: a picture costs your vision provider, and text inside one can try to steer Jarvis.",
      on: p.owner ? true : undefined,
      locked: p.owner ? "This is the owner — your own pictures are always read." : lockFor(p.image, "picture"),
      notes: p.owner ? [] : imgNotes,
    });
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Capabilities"), el("div", { class: "ch-rows" }, [toolsRow, imagesRow, thinkingRow(p)])]));

    // --- Authority
    const sendLocked = p.owner ? "This is the owner's own account — Jarvis reaches you with notifications, not DMs." : blockedLock;
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Authority"),
      el("div", { class: "ch-rows" }, [
        permRow(p, {
          flag: "send_dm", icon: "send_dm", ch: "on", label: "Jarvis may DM them", on: p.owner ? false : p.send_dm,
          hint: "Lets the send_dm tool message this person when you ask it to. Only you can ever ask — the tool stays owner-only and still asks for confirmation each time.",
          locked: sendLocked, notes: p.owner || p.blocked ? [] : [{ kind: "info", text: "This is a ceiling, not a grant: it only matters once you ask Jarvis to message them." }],
        }),
        permRow(p, {
          flag: "owner", icon: "owner", ch: "owner", label: "Owner", danger: true, confirmOn: true, confirmOff: true, locked: blockedLock,
          hint: `The single ${pname} account Jarvis trusts as you inside chats and notifies. Only one person can hold it — switching it on moves it.`,
          notes: p.owner ? [{ kind: "info", text: "Ownership doesn't grant replies or tools on its own — those are the switches above." }] : [],
        }),
        permRow(p, {
          flag: "blocked", icon: "blocked", ch: "bad", label: "Blocked", danger: true, confirmOn: true,
          hint: "Removes them from every list and stops Jarvis asking about them. Switch off to start them again from nothing.",
          locked: p.owner ? "That's the owner — hand ownership to someone else first." : "",
        }),
      ]),
    ]));
    return pane;
  }

  // L.44: how much Jarvis reasons when it answers this person, and whether they
  // may change it themselves with /thinking in chat. This is the LEVEL only: no
  // one but the owner is ever shown Jarvis's reasoning (L.42.2 sends the trace to
  // the owner's own chat when they type /thinking on). Not a permission grant: it
  // can only change how much a reply costs. The owner has no setting here.
  function thinkingRow(p) {
    const value = p.thinking === "on" || p.thinking === "off" ? p.thinking : "default";
    const lock = !!p.thinking_lock;
    const busy = state.busy.has(`${pkey(p)}|thinking`);
    const disabled = !!p.owner || busy;
    const row = el("div", {
      class: "ch-row ch-row--tools" + (value === "on" ? " is-on" : "") + (busy ? " is-busy" : ""),
      "data-ch": value === "on" ? "limited" : "off", "data-flag": "thinking",
    });
    row.appendChild(el("div", { class: "ch-row__icon" }, [icon("thinking")]));
    const tags = [];
    if (!p.owner && value !== "default") tags.push(tag(lock ? `${value}, locked` : value, value === "on" ? "limited" : "off"));
    const body = el("div", null, [
      el("div", { class: "ch-row__label" }, ["Thinking", ...tags]),
      el("div", { class: "ch-row__hint" }, "How much Jarvis reasons when it answers this person. Default leaves chat as it was; On asks for reasoning (costs more tokens); Off asks for none. They are never shown Jarvis's reasoning \u2014 only you can see that, with /thinking on in your own chat."),
    ]);
    if (p.owner) {
      body.appendChild(noteLine("This is the owner \u2014 never limited. Type /thinking on in this chat to see Jarvis's reasoning after each reply.", "info"));
    } else {
      const pick = (v, label) => el("button", {
        type: "button", role: "radio", "aria-checked": value === v ? "true" : "false",
        disabled: disabled ? "" : null,
        onclick: () => { if (v !== value) saveThinking(p, v, v === "default" ? false : lock); },
      }, label);
      const lockBox = el("input", {
        type: "checkbox", id: `ch-think-lock-${pkey(p)}`, checked: lock ? "checked" : null,
        disabled: disabled || value === "default" ? "" : null,
        onchange: (ev) => saveThinking(p, value, ev.target.checked),
      });
      lockBox.checked = lock;
      body.appendChild(el("div", { class: "ch-thinking" }, [
        el("div", { class: "ch-seg", role: "radiogroup", "aria-label": "Thinking for this person" },
          [pick("default", "Default"), pick("on", "On"), pick("off", "Off")]),
        el("label", { class: "ch-thinking__lock", for: `ch-think-lock-${pkey(p)}` },
          [lockBox, el("span", null, "They can't change it with /thinking")]),
      ]));
    }
    row.appendChild(body);
    return row;
  }

  function savebar(p) {
    const d = state.drafts.get(pkey(p));
    if (!d) return null;
    const busy = state.busy.has(`${pkey(p)}|tools`);
    const count = d.mode === "custom" ? [...d.names].filter((n) => p.owner || !OWNER_ONLY.has(n)).length : null;
    return el("div", { class: "ch-savebar", role: "region", "aria-label": "Unsaved tool changes" }, [
      el("div", { class: "ch-savebar__text" }, [el("b", null, "Unsaved. "), d.mode === "custom" ? `Limit ${displayName(p)} to ${count} tool${count === 1 ? "" : "s"}.` : `Let ${displayName(p)} use every tool the platform allows.`]),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, onclick: () => { state.drafts.delete(pkey(p)); renderDetail(); } }, "Discard"),
      el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: busy, onclick: () => saveScope(p) }, busy ? "Saving…" : "Save"),
    ]);
  }

  // ---- Quick setups (L.36-P4) and bulk edit (L.36-P5) --------------------------
  // The server does all of it (channels/preset_admin.py -> the same calls the
  // single switches make). Here: a click on a setup only PREVIEWS it, Apply is a
  // second click, and every refusal is shown where it happened.
  const STEP_CH = { change: "on", done: "on", same: "off", refused: "bad", failed: "bad", skipped: "bad" };
  const STEP_TEXT = { change: "will change", done: "done", same: "already", refused: "can't", failed: "failed", skipped: "skipped" };
  const STATUS_CH = { applied: "on", unchanged: "off", partial: "limited", refused: "bad", failed: "bad", preview: "limited" };
  const STATUS_TEXT = { applied: "changed", unchanged: "no change", partial: "partly done", refused: "not changed", failed: "failed" };
  let presetReq = 0;
  let bulkReq = 0;

  function newBulk() {
    return { mode: "", preset: "", flag: "", value: false, status: "idle", data: null, results: null, summary: null, action: "", error: "", req: 0, ack: false };
  }
  function presetById(id) { return state.presets.find((x) => x.id === id) || null; }

  // "3 would change · 1 already set · 1 can't be changed" — pure.
  function summaryText(action, s) {
    if (!s) return "";
    const parts = [];
    const add = (n, t) => { if (n) parts.push(`${n} ${t}`); };
    if (action === "preview") {
      add(s.would_change, "would change"); add(s.unchanged, "already set"); add(s.refused, "can't be changed"); add(s.failed, "failed");
    } else {
      add(s.applied, "changed"); add(s.unchanged, "no change"); add(s.partial, "partly done"); add(s.refused, "not changed"); add(s.failed, "failed");
    }
    return parts.join(" \u00b7 ") || "Nothing to report.";
  }

  // The steps of one setup for one person. `onlyChanges` hides the switches
  // that are already right, which is what a long bulk list wants.
  function stepList(steps, onlyChanges) {
    const rows = (steps || []).filter((s) => !onlyChanges || s.state !== "same");
    const ul = el("ul", { class: "ch-steps" });
    if (!rows.length) { ul.appendChild(el("li", { class: "ch-steps__none" }, "Nothing to change \u2014 it's already set that way.")); return ul; }
    for (const s of rows) {
      ul.appendChild(el("li", { class: "ch-step", "data-ch": STEP_CH[s.state] || "off" }, [
        el("span", { class: "ch-step__label" }, s.label),
        el("span", { class: "ch-step__change" }, s.state === "same" ? s.to : `${s.from} \u2192 ${s.to}`),
        tag(STEP_TEXT[s.state] || s.state, STEP_CH[s.state] || "off"),
        s.reason ? el("span", { class: "ch-step__why" }, s.reason) : null,
      ]));
    }
    return ul;
  }

  function noteLine(text, kind) {
    return el("div", { class: "ch-row__note" + (kind === "bad" ? " ch-row__note--bad" : kind === "info" ? " ch-row__note--info" : "") }, [icon(kind === "info" ? "info" : "warn"), el("span", null, text)]);
  }

  function presetButtons(onPick, activeId, disabled, why) {
    const row = el("div", { class: "ch-presets", role: "group", "aria-label": "Quick setups" });
    for (const pr of state.presets) {
      const active = activeId === pr.id;
      row.appendChild(el("button", {
        type: "button", class: "ch-preset" + (active ? " is-active" : ""), "data-risk": pr.risk, "aria-pressed": active ? "true" : "false",
        disabled: disabled ? "" : null, title: disabled ? why : pr.summary, onclick: () => onPick(pr.id),
      }, [el("span", { class: "ch-preset__name" }, pr.label), el("span", { class: "ch-preset__sum" }, pr.summary)]));
    }
    return row;
  }

  // -- one person --------------------------------------------------------
  async function previewPreset(p, id) {
    const req = ++presetReq;
    state.presetDone = null;
    state.presetPrev = { key: pkey(p), preset: id, status: "loading", data: null, error: "", ack: false, req };
    renderDetail();
    try {
      const out = await personPost(p, "preset", { preset: id, preview: true });
      if (!state.presetPrev || state.presetPrev.req !== req) return;
      state.presetPrev.status = "ready";
      state.presetPrev.data = out;
    } catch (err) {
      if (!state.presetPrev || state.presetPrev.req !== req) return;
      state.presetPrev.status = "error";
      state.presetPrev.error = err.message || "Couldn't preview that.";
    }
    renderDetail();
  }

  async function applyPreset(p, id) {
    const bkey = `${pkey(p)}|preset`;
    state.busy.add(bkey);
    renderDetail();
    let out = null;
    let failure = "";
    try { out = await personPost(p, "preset", { preset: id }); } catch (err) { failure = err.message || "Couldn't apply that."; }
    state.busy.delete(bkey);
    state.presetPrev = null;
    const pr = presetById(id);
    if (out) {
      state.presetDone = { key: pkey(p), result: out };
      const label = pr ? pr.label : id;
      if (out.status === "applied") toast(`${label} applied to ${displayName(p)}.`, "success");
      else if (out.status === "unchanged") toast(`${displayName(p)} was already set up that way.`, "info");
      else toast(`${label} was only partly applied to ${displayName(p)} \u2014 see what wasn't.`, "info");
    } else {
      toast(failure, "error");
    }
    await load(true);
  }

  function presetCard(p) {
    const pv = state.presetPrev;
    if (!pv || pv.key !== pkey(p)) return null;
    const pr = presetById(pv.preset);
    const card = el("div", { class: "ch-prev", "data-risk": pr ? pr.risk : null, role: "region", "aria-label": "Quick setup preview" });
    card.appendChild(el("div", { class: "ch-prev__head" }, [el("b", null, pr ? pr.label : pv.preset), ` for ${displayName(p)}`]));
    const close = el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.presetPrev = null; renderDetail(); } }, "Cancel");
    if (pv.status === "loading") { card.appendChild(el("div", { class: "ch-skel" })); return card; }
    if (pv.status === "error") {
      card.appendChild(noteLine(pv.error, "bad"));
      card.appendChild(el("div", { class: "ch-confirm__btns" }, [close]));
      return card;
    }
    const d = pv.data || {};
    const busy = state.busy.has(`${pkey(p)}|preset`);
    const will = d.will_change || 0;
    card.appendChild(el("div", { class: "ch-prev__sub" }, "This is a preview \u2014 nothing has changed yet."));
    card.appendChild(stepList(d.steps, false));
    if ((d.steps || []).some((s) => s.state === "refused")) {
      card.appendChild(noteLine("A switch marked \u201ccan't\u201d stays as it is; the rest still applies.", "info"));
    }
    for (const n of d.notes || []) card.appendChild(noteLine(n, "info"));
    const broad = pr && pr.risk === "broad";
    if (broad && will) {
      const input = el("input", { type: "checkbox", id: "ch-preset-ack" });
      input.checked = !!pv.ack;
      input.addEventListener("change", () => { pv.ack = input.checked; renderDetail(); });
      card.appendChild(el("label", { class: "ch-ack" }, [input, el("span", null, `I understand ${displayName(p)} will be able to make Jarvis run any tool on this PC.`)]));
    }
    const apply = el("button", {
      type: "button", class: "btn btn--sm " + (broad ? "btn--danger" : "btn--primary"),
      disabled: (!will || busy || (broad && !pv.ack)) ? "" : null, onclick: () => applyPreset(p, pv.preset),
    }, busy ? "Applying\u2026" : will ? `Apply (${will} change${will === 1 ? "" : "s"})` : "Nothing to apply");
    card.appendChild(el("div", { class: "ch-confirm__btns" }, [apply, close]));
    return card;
  }

  function presetDoneCard(p) {
    const done = state.presetDone;
    if (!done || done.key !== pkey(p)) return null;
    const r = done.result || {};
    const card = el("div", { class: "ch-prev ch-prev--done", "data-ch": STATUS_CH[r.status] || "off", role: "status" });
    card.appendChild(el("div", { class: "ch-prev__head" }, [el("b", null, r.label || "Quick setup"), tag(STATUS_TEXT[r.status] || r.status, STATUS_CH[r.status] || "off")]));
    card.appendChild(stepList(r.steps, false));
    for (const n of r.notes || []) card.appendChild(noteLine(n, "info"));
    card.appendChild(el("div", { class: "ch-confirm__btns" }, [el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.presetDone = null; renderDetail(); } }, "Dismiss")]));
    return card;
  }

  function presetSection(p) {
    if (!state.presets.length) return null;
    const key = pkey(p);
    const locked = p.owner ? "Quick setups leave the owner's account alone \u2014 use the switches below."
      : state.drafts.has(key) ? "Save or discard the unsaved tool list first." : "";
    const busy = state.busy.has(`${key}|preset`);
    const active = state.presetPrev && state.presetPrev.key === key ? state.presetPrev.preset : "";
    const wrap = el("div", null, [
      el("div", { class: "ch-section-title" }, "Quick setups"),
      presetButtons((id) => previewPreset(p, id), active, !!locked || busy, locked),
    ]);
    if (locked) wrap.appendChild(el("div", { class: "ch-row__hint", style: "margin-top:6px" }, locked));
    else wrap.appendChild(el("div", { class: "ch-row__hint", style: "margin-top:6px" }, "A click shows what would change first. Nothing is saved until you press Apply."));
    const prev = presetCard(p);
    if (prev) wrap.appendChild(prev);
    const done = presetDoneCard(p);
    if (done) wrap.appendChild(done);
    return wrap;
  }

  // -- several people (P5) -----------------------------------------------
  function pickedPeople() { return state.people.filter((p) => state.picked.has(pkey(p))); }
  function bulkRefs() { return pickedPeople().map((p) => ({ platform: p.platform, id: p.user_id })); }

  function setSelectMode(on) {
    state.selectMode = !!on;
    if (!on) { state.picked.clear(); state.bulkOpen = false; state.bulk = newBulk(); }
    renderList(); renderSelBar(); renderDetail(true);
  }
  function togglePick(k) {
    if (state.picked.has(k)) state.picked.delete(k); else state.picked.add(k);
    // A different group: an old preview no longer describes who would change.
    if (state.bulkOpen) state.bulk = newBulk();
    if (!state.picked.size) state.bulkOpen = false;
    renderList(); renderSelBar();
    if (state.bulkOpen || !state.picked.size) renderDetail(true);
  }
  function pickAllShown() {
    for (const p of visiblePeople()) state.picked.add(pkey(p));
    if (state.bulkOpen) state.bulk = newBulk();
    renderList(); renderSelBar();
    if (state.bulkOpen) renderDetail(true);
  }
  function clearPicks() {
    state.picked.clear(); state.bulkOpen = false; state.bulk = newBulk();
    renderList(); renderSelBar(); renderDetail(true);
  }
  function openBulk() { state.bulkOpen = true; state.bulk = newBulk(); renderSelBar(); renderDetail(true); }
  function closeBulk() { state.bulkOpen = false; state.bulk = newBulk(); renderSelBar(); renderDetail(true); }

  function renderSelBar() {
    const bar = dom && dom.selbar;
    if (!bar) return;
    bar.textContent = "";
    if (!state.selectMode) {
      bar.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: state.people.length ? null : "", onclick: () => setSelectMode(true), title: "Tick several people and change them together" }, "Select people\u2026"));
      return;
    }
    const n = state.picked.size;
    bar.appendChild(el("span", { class: "ch-selbar__n" }, [el("b", null, String(n)), " selected"]));
    bar.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: pickAllShown, title: "Tick everyone the filters currently show" }, "All shown"));
    bar.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: n ? null : "", onclick: clearPicks }, "None"));
    bar.appendChild(el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: n ? null : "", onclick: openBulk }, "Edit selected\u2026"));
    bar.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => setSelectMode(false) }, "Done"));
  }

  async function bulkPreview(id) {
    const req = ++bulkReq;
    state.bulk = Object.assign(newBulk(), { mode: "preset", preset: id, status: "loading", req });
    renderDetail();
    try {
      const out = await api("POST", "/api/channels/bulk", { action: "preview", preset: id, people: bulkRefs() });
      if (state.bulk.req !== req) return;
      Object.assign(state.bulk, { status: "ready", data: out, results: out.results, summary: out.summary, action: "preview" });
    } catch (err) {
      if (state.bulk.req !== req) return;
      Object.assign(state.bulk, { status: "error", error: err.message || "Couldn't preview that." });
    }
    renderDetail();
  }

  function bulkChooseFlag(flag, value) {
    const req = ++bulkReq;
    state.bulk = Object.assign(newBulk(), { mode: "flag", flag, value, status: "ready", req });
    renderDetail();
  }

  async function bulkApply() {
    const b = state.bulk;
    const req = ++bulkReq;
    b.req = req;
    b.status = "applying";
    renderDetail();
    const people = bulkRefs();
    const body = b.mode === "preset" ? { action: "preset", preset: b.preset, people } : { action: "flag", flag: b.flag, value: b.value, people };
    let out = null;
    let failure = "";
    try { out = await api("POST", "/api/channels/bulk", body); } catch (err) { failure = err.message || "Couldn't apply that."; }
    if (state.bulk.req !== req) return;
    if (out) {
      Object.assign(b, { status: "done", results: out.results, summary: out.summary, action: out.action });
      toast(summaryText(out.action, out.summary), "info");
    } else {
      Object.assign(b, { status: "error", error: failure });
    }
    await load(true);
  }

  function bulkPersonRow(r, action) {
    const previewing = action === "preview";
    let text;
    if (previewing) text = !r.ok ? "can't" : (r.will_change ? "will change" : "already set");
    else text = STATUS_TEXT[r.status] || r.status;
    const ch = previewing ? (!r.ok ? "bad" : r.will_change ? "on" : "off") : (STATUS_CH[r.status] || "off");
    const li = el("li", { class: "ch-bres", "data-ch": ch }, [
      el("div", { class: "ch-bres__top" }, [
        el("span", { class: "ch-bres__name" }, r.name || r.user_id || "unknown"),
        tag(PLATFORMS[r.platform] ? PLATFORMS[r.platform].label : String(r.platform || ""), null),
        tag(text, ch),
      ]),
    ]);
    if (r.reason && !(r.steps && r.steps.length)) li.appendChild(el("div", { class: "ch-bres__why" }, r.reason));
    const changing = (r.steps || []).filter((s) => s.state !== "same");
    if (changing.length) li.appendChild(stepList(changing, false));
    for (const n of (r.notes || [])) li.appendChild(el("div", { class: "ch-bres__why ch-bres__why--info" }, n));
    if (r.note) li.appendChild(el("div", { class: "ch-bres__why ch-bres__why--info" }, r.note));
    return li;
  }

  function bulkResultList(results, action) {
    const ul = el("ul", { class: "ch-bres-list" });
    for (const r of results || []) ul.appendChild(bulkPersonRow(r, action));
    return ul;
  }

  const BULK_FLAG_ROWS = [
    { flag: "dm", label: "Direct messages", on: "Allow", off: "Stop allowing" },
    { flag: "reply", label: "Replies", on: "Answer them", off: "Stop answering" },
    { flag: "tool", label: "Tool use", off: "Take tools away", onNote: "To give tools, use a quick setup \u2014 it also decides which tools." },
    { flag: "send_dm", label: "Jarvis may DM them", on: "Allow", off: "Stop allowing" },
    { flag: "blocked", label: "Blocked", on: "Block", off: "Unblock", danger: true },
  ];

  function renderBulk() {
    const b = state.bulk;
    const people = pickedPeople();
    const n = people.length;
    const pane = el("div", { class: "ch-tabpane ch-bulk", role: "region", "aria-label": "Edit several people" });
    pane.appendChild(el("div", { class: "ch-bulk__head" }, [
      el("div", null, [el("div", { class: "ch-bulk__title" }, `Edit ${n} ${n === 1 ? "person" : "people"}`), el("div", { class: "ch-row__hint" }, "Each person is checked on their own. Anyone who can't be changed is listed with the reason.")]),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: closeBulk }, "Back"),
    ]));
    const chips = el("div", { class: "ch-bulk__who" });
    for (const p of people) chips.appendChild(el("span", { class: "channels-chip" }, [displayName(p), el("button", { type: "button", class: "channels-chip__remove", title: `Leave out ${displayName(p)}`, "aria-label": `Leave out ${displayName(p)}`, onclick: () => togglePick(pkey(p)) }, "\u00d7")]));
    pane.appendChild(chips);

    const drafted = people.filter((p) => state.drafts.has(pkey(p)));
    if (drafted.length) pane.appendChild(noteLine(`${drafted.map(displayName).join(", ")} ${drafted.length === 1 ? "has" : "have"} an unsaved tool list. Save or discard it first, so this can't overwrite it.`, "bad"));
    const busy = b.status === "loading" || b.status === "applying";
    const locked = !!drafted.length || busy || b.status === "done";

    if (b.status === "done") {
      pane.appendChild(el("div", { class: "ch-section-title" }, "Result"));
      pane.appendChild(el("div", { class: "ch-bulk__sum", role: "status" }, summaryText(b.action, b.summary)));
      pane.appendChild(bulkResultList(b.results, b.action));
      pane.appendChild(el("div", { class: "ch-confirm__btns" }, [
        el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: () => { state.bulk = newBulk(); renderDetail(); } }, "Do another change"),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: closeBulk }, "Close"),
      ]));
      return pane;
    }

    pane.appendChild(el("div", { class: "ch-section-title" }, "Quick setup"));
    pane.appendChild(presetButtons(bulkPreview, b.mode === "preset" ? b.preset : "", locked, drafted.length ? "Save or discard the unsaved tool list first." : ""));
    pane.appendChild(el("div", { class: "ch-section-title" }, "Or one switch"));
    const rows = el("div", { class: "ch-rows" });
    for (const r of BULK_FLAG_ROWS) {
      const mk = (label, value) => el("button", {
        type: "button", class: "btn btn--sm " + (r.danger && value ? "btn--danger" : "btn--ghost"), disabled: locked ? "" : null,
        "aria-pressed": b.mode === "flag" && b.flag === r.flag && b.value === value ? "true" : "false", onclick: () => bulkChooseFlag(r.flag, value),
      }, label);
      rows.appendChild(el("div", { class: "ch-row ch-bulkrow", "data-flag": r.flag }, [
        el("div", { class: "ch-row__icon" }, [icon(r.flag)]),
        el("div", null, [el("div", { class: "ch-row__label" }, r.label), r.onNote ? el("div", { class: "ch-row__hint" }, r.onNote) : null]),
        el("div", { class: "ch-bulkrow__btns" }, [r.on ? mk(r.on, true) : null, mk(r.off, false)]),
      ]));
    }
    pane.appendChild(rows);
    pane.appendChild(el("div", { class: "ch-row__hint" }, "Ownership isn't offered here: only one person can hold it, so set it on that person's own page."));

    if (b.mode === "preset") {
      const pr = presetById(b.preset);
      const card = el("div", { class: "ch-prev", "data-risk": pr ? pr.risk : null, role: "region", "aria-label": "Quick setup preview" });
      card.appendChild(el("div", { class: "ch-prev__head" }, [el("b", null, pr ? pr.label : b.preset), ` for ${n} ${n === 1 ? "person" : "people"}`]));
      if (b.status === "loading") card.appendChild(el("div", { class: "ch-skel" }));
      else if (b.status === "error") card.appendChild(noteLine(b.error, "bad"));
      else {
        card.appendChild(el("div", { class: "ch-prev__sub" }, "This is a preview \u2014 nothing has changed yet."));
        card.appendChild(el("div", { class: "ch-bulk__sum" }, summaryText("preview", b.summary)));
        card.appendChild(bulkResultList(b.results, "preview"));
        const will = (b.summary && b.summary.would_change) || 0;
        const broad = pr && pr.risk === "broad";
        if (broad && will) {
          const input = el("input", { type: "checkbox", id: "ch-bulk-ack" });
          input.checked = !!b.ack;
          input.addEventListener("change", () => { b.ack = input.checked; renderDetail(); });
          card.appendChild(el("label", { class: "ch-ack" }, [input, el("span", null, `I understand ${will === 1 ? "this person" : `these ${will} people`} will be able to make Jarvis run any tool on this PC.`)]));
        }
        card.appendChild(el("div", { class: "ch-confirm__btns" }, [
          el("button", { type: "button", class: "btn btn--sm " + (broad ? "btn--danger" : "btn--primary"), disabled: (!will || busy || drafted.length || (broad && !b.ack)) ? "" : null, onclick: bulkApply },
            busy ? "Applying\u2026" : will ? `Apply to ${will} ${will === 1 ? "person" : "people"}` : "Nothing to apply"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.bulk = newBulk(); renderDetail(); } }, "Cancel"),
        ]));
      }
      pane.appendChild(card);
    } else if (b.mode === "flag") {
      const row = BULK_FLAG_ROWS.find((x) => x.flag === b.flag);
      const verb = b.value ? (row.on || "Turn on") : (row.off || "Turn off");
      const danger = b.flag === "blocked" && b.value;
      const card = el("div", { class: "ch-prev", "data-risk": danger ? "broad" : "narrow", role: "region", "aria-label": "Confirm the change" });
      card.appendChild(el("div", { class: "ch-prev__head" }, [el("b", null, `${row.label}: ${verb.toLowerCase()}`), ` for ${n} ${n === 1 ? "person" : "people"}`]));
      card.appendChild(el("div", { class: "ch-prev__sub" }, danger
        ? "They're removed from the DM, reply and tool lists and Jarvis stops asking about them. The owner and anyone covered by \u201c*\u201d are refused and listed afterwards."
        : "Anyone who can't be changed (the owner, blocked, covered by \u201c*\u201d) is listed afterwards with the reason. There's no per-person preview for a single switch."));
      card.appendChild(el("div", { class: "ch-confirm__btns" }, [
        el("button", { type: "button", class: "btn btn--sm " + (danger ? "btn--danger" : "btn--primary"), disabled: (busy || drafted.length) ? "" : null, onclick: bulkApply }, busy ? "Applying\u2026" : `${verb} \u2014 ${n} ${n === 1 ? "person" : "people"}`),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.bulk = newBulk(); renderDetail(); } }, "Cancel"),
      ]));
      if (b.status === "error") card.appendChild(noteLine(b.error, "bad"));
      pane.appendChild(card);
    }
    return pane;
  }

  function renderDetail(resetScroll) {
    if (!dom) return;
    const keepTop = resetScroll ? 0 : dom.detail.scrollTop;
    // A redraw replaces every node, so keyboard focus would fall back to the
    // page. Put it back on the element with the same id (tabs, the Test
    // controls) when the same person is still showing.
    const focusId = (!resetScroll && document.activeElement && dom.detail.contains(document.activeElement)) ? document.activeElement.id : "";
    dom.detail.textContent = "";
    if (state.error) {
      dom.detail.appendChild(el("div", { class: "ch-state" }, [icon("warn"), el("div", { class: "ch-state__t" }, "Couldn't read channels"), el("div", { class: "ch-state__d" }, state.error),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => load(true) }, "Try again")]));
      return;
    }
    if (state.bulkOpen && state.selectMode && state.picked.size) {
      dom.detail.appendChild(renderBulk());
      dom.detail.scrollTop = keepTop;
      return;
    }
    const kind = entryKind(state.selected);
    if (kind !== "person") {
      let view = null;
      if (kind === "home") view = homeView();
      else if (kind === "platform" && entryExists(state.selected)) view = platformView(state.selected.slice(PLAT_PREFIX.length));
      else if (kind === "server" && serverEntryOf(state.selected)) view = serverView(serverEntryOf(state.selected));
      else if (kind === "group" && groupRowOf(state.selected)) view = groupView(groupRowOf(state.selected));
      if (view) {
        dom.detail.appendChild(view);
        dom.detail.scrollTop = keepTop;
        if (focusId) { const again = document.getElementById(focusId); if (again) again.focus({ preventScroll: true }); }
        return;
      }
    }
    const p = current();
    if (!p) {
      dom.detail.appendChild(el("div", { class: "ch-state" }, [icon("people"), el("div", { class: "ch-state__t" }, state.loading ? "Loading…" : state.people.length ? "Pick someone" : "No one registered yet"),
        el("div", { class: "ch-state__d" }, state.loading ? "" : state.people.length ? "Choose a person on the left to see and change what they can do." : "People show up here the first time they DM or mention Jarvis.")]));
      return;
    }
    dom.detail.appendChild(hero(p));
    const tabBar = tabs(p);
    dom.detail.appendChild(tabBar);
    // Seven tabs don't fit a phone: scroll the bar so the active one is visible.
    const activeTab = tabBar.querySelector('.ch-tab[aria-selected="true"]');
    if (activeTab && activeTab.offsetLeft + activeTab.offsetWidth > tabBar.clientWidth) tabBar.scrollLeft = activeTab.offsetLeft - 16;
    const pane = { profile: renderProfile, conv: renderConv, memory: renderMemory, usage: renderUsage, test: renderTest, history: renderHistory }[state.tab] || renderPerms;
    dom.detail.appendChild(pane(p));
    const bar = state.tab === "perms" ? savebar(p) : null;
    if (bar) dom.detail.appendChild(bar);
    dom.detail.scrollTop = keepTop;
    if (state.scrollEnd && state.tab === "conv") { dom.detail.scrollTop = dom.detail.scrollHeight; state.scrollEnd = false; }
    if (focusId) { const again = document.getElementById(focusId); if (again) again.focus({ preventScroll: true }); }
    if (state.tab === "perms") ensureTools();
  }

  // ---- Conversation / Usage / Test as this person  (L.36-P1 / P2 / P3) -------
  const CONV_STEP = 100, CONV_MAX = 500;
  const personUrl = (p, tail) => `/api/channels/people/${encodeURIComponent(p.platform)}/${encodeURIComponent(p.user_id)}/${tail}`;
  const slot = (map, p, init) => { const k = pkey(p); if (!map.has(k)) map.set(k, init()); return map.get(k); };
  const convSlot = (p) => slot(state.conv, p, () => ({ status: "idle", data: null, error: "", limit: CONV_STEP, req: 0 }));
  const usageSlot = (p) => slot(state.usage, p, () => ({ status: "idle", data: null, error: "", days: 30, req: 0 }));
  const histSlot = (p) => slot(state.hist, p, () => ({ status: "idle", data: null, error: "", limit: CONV_STEP, req: 0 }));
  const simSlot = (p) => slot(state.sim, p, () => ({ context: "dm", mentioned: true, status: "idle", result: null, error: "", req: 0 }));
  const showing = (p, tab) => !!dom && state.selected === pkey(p) && state.tab === tab;

  async function ensureConv(p, more) {
    const s = convSlot(p);
    if (more) s.limit = Math.min(CONV_MAX, s.limit + CONV_STEP * 2);
    else if (s.status === "ok" || s.status === "loading") return;
    const req = ++s.req;
    s.status = "loading"; s.error = "";
    if (showing(p, "conv")) renderDetail();
    // "Load older" adds rows ABOVE what is on screen: keep the same message under the cursor.
    const h0 = dom ? dom.detail.scrollHeight : 0, t0 = dom ? dom.detail.scrollTop : 0;
    try {
      const data = await api("GET", personUrl(p, `conversation?limit=${s.limit}`));
      if (s.req !== req) return;
      s.data = data; s.status = "ok";
      if (!more) state.scrollEnd = true;
    } catch (err) {
      if (s.req !== req) return;
      s.status = "error"; s.error = err.message || "Couldn't read the conversation.";
    }
    if (showing(p, "conv")) {
      renderDetail();
      if (more && dom) dom.detail.scrollTop = t0 + (dom.detail.scrollHeight - h0);
    }
  }

  async function ensureUsage(p, days) {
    const s = usageSlot(p);
    if (days && days !== s.days) { s.days = days; s.status = "idle"; }
    if (s.status === "ok" || s.status === "loading") return;
    const req = ++s.req;
    s.status = "loading"; s.error = "";
    if (showing(p, "usage")) renderDetail();
    try {
      const data = await api("GET", personUrl(p, `usage?days=${s.days}`));
      if (s.req !== req) return;
      s.data = data; s.status = "ok";
    } catch (err) {
      if (s.req !== req) return;
      s.status = "error"; s.error = err.message || "Couldn't read their usage.";
    }
    if (showing(p, "usage")) renderDetail();
  }

  async function ensureHistory(p, more) {
    const s = histSlot(p);
    if (more) s.limit = Math.min(CONV_MAX, s.limit + CONV_STEP * 2);
    else if (s.status === "ok" || s.status === "loading") return;
    const req = ++s.req;
    s.status = "loading"; s.error = "";
    if (showing(p, "history")) renderDetail();
    try {
      const data = await api("GET", personUrl(p, `history?limit=${s.limit}`));
      if (s.req !== req) return;
      s.data = data; s.status = "ok";
    } catch (err) {
      if (s.req !== req) return;
      s.status = "error"; s.error = err.message || "Couldn't read the change log.";
    }
    if (showing(p, "history")) renderDetail();
  }

  // The dry run. Re-run every time the tab is opened or an option changes,
  // because a switch flipped on the Permissions tab makes any earlier answer stale.
  async function runSim(p) {
    const s = simSlot(p);
    const req = ++s.req;
    s.status = "loading"; s.error = "";
    if (showing(p, "test")) renderDetail();
    try {
      const body = { context: s.context };
      if (s.context === "group") body.mentioned = s.mentioned;
      const result = await personPost(p, "test", body);
      if (s.req !== req) return;
      s.result = result; s.status = "ok";
    } catch (err) {
      if (s.req !== req) return;
      s.status = "error"; s.error = err.message || "The test couldn't run.";
    }
    if (showing(p, "test")) renderDetail();
  }

  // What to fetch when a tab (or a person) comes into view.
  function runTabLoader() {
    const p = current();
    if (!p) return;
    if (state.tab === "perms") ensureTools();
    else if (state.tab === "conv") ensureConv(p);
    else if (state.tab === "memory") ensureMem(p);
    else if (state.tab === "usage") ensureUsage(p);
    else if (state.tab === "test") runSim(p);
    else if (state.tab === "history") ensureHistory(p);
  }
  function activateTab(id) {
    state.tab = id;
    renderDetail();
    runTabLoader();
  }

  function loadingBlock(label) {
    return el("div", { class: "ch-state" }, [icon("info"), el("div", { class: "ch-state__t" }, label)]);
  }
  function errorBlock(message, retry) {
    return el("div", { class: "ch-state" }, [icon("warn"), el("div", { class: "ch-state__t" }, "Couldn't load this"),
      el("div", { class: "ch-state__d" }, message), el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: retry }, "Try again")]);
  }

  // One-line label for a reply's tool calls: "web_search, get_datetime ×2".
  function toolCallsLabel(calls) {
    const list = Array.isArray(calls) ? calls : [];
    if (!list.length) return "";
    const counts = new Map();
    for (const c of list) counts.set(c.name || "?", (counts.get(c.name || "?") || 0) + 1);
    return [...counts].map(([n, k]) => (k > 1 ? `${n} ×${k}` : n)).join(", ");
  }
  function toolCallsNode(calls) {
    const list = Array.isArray(calls) ? calls : [];
    if (!list.length) return null;
    const failed = list.filter((c) => c.ok === false).length;
    const box = el("details", { class: "ch-tools" + (failed ? " has-failed" : "") });
    box.appendChild(el("summary", null, [
      icon("tool"),
      el("span", { class: "ch-tools__count" }, `Used ${list.length} tool${list.length === 1 ? "" : "s"}`),
      el("span", { class: "ch-tools__names" }, toolCallsLabel(list)),
      failed ? tag(`${failed} failed`, "bad", "warn") : null,
    ]));
    const ul = el("ol", { class: "ch-tools__list" });
    list.forEach((c) => {
      const state_ = c.ok === false ? "bad" : c.ok === true ? "ok" : "unknown";
      const li = el("li", { class: "ch-tool", "data-state": state_ }, [
        el("div", { class: "ch-tool__head" }, [
          el("span", { class: "ch-tool__name" }, c.name || "?"),
          el("span", { class: "ch-tool__state" }, c.ok === false ? "failed" : c.ok === true ? "done" : "no result recorded"),
          c.confirm ? el("span", { class: "ch-tool__state" }, c.confirm) : null,
        ]),
      ]);
      if (c.args) li.appendChild(el("div", { class: "ch-tool__line" }, [el("span", { class: "ch-tool__k" }, "with"), el("code", null, c.args)]));
      if (c.result) li.appendChild(el("div", { class: "ch-tool__line" }, [el("span", { class: "ch-tool__k" }, c.ok === false ? "error" : "result"), el("code", null, c.result)]));
      ul.appendChild(li);
    });
    box.appendChild(ul);
    if (list.some((c) => c.clipped)) box.appendChild(el("div", { class: "ch-msg__more" }, "(long values are shortened here — the full run is in the conversation's raw log)"));
    return box;
  }

  function messageNode(e, opts) {
    const room = !!(opts && opts.room);   // S11: a line in a shared room, shown by who wrote it
    const mine = e.dir === "in";
    const wrap = el("div", { class: "ch-msg " + (mine ? "ch-msg--in" : "ch-msg--out"), "data-ch": mine && e.allowed === false ? "bad" : null });
    const meta = el("div", { class: "ch-msg__meta" }, [
      room && mine ? el("span", { class: "speaker-chip" }, roomSpeaker(e)) : el("span", null, mine ? "They wrote" : "Jarvis"),
      clockOf(e.at) ? el("span", { class: "ch-msg__time" }, clockOf(e.at)) : null,
    ]);
    if (mine) {
      if (!room) meta.appendChild(tag(e.context === "group" ? "group" : "DM", null, null));
      if (room && e.imported) meta.appendChild(tag("imported", null, null));
      if (!room && e.context === "group" && (e.channel_name || e.guild_name)) {
        meta.appendChild(el("span", { class: "ch-msg__where", title: [e.guild_name, e.channel_name && "#" + e.channel_name].filter(Boolean).join(" / ") },
          [e.channel_name ? "#" + e.channel_name : "", e.channel_name && e.guild_name ? " · " : "", e.guild_name || ""].join("")));
      }
      if (e.allowed === false) meta.appendChild(tag("Turned away", "bad", "blocked"));
      else if (e.may_use_tools) meta.appendChild(tag("tools on", "limited", "tool"));
    } else {
      if (e.ok === false) meta.appendChild(tag("Not delivered", "bad", "warn"));
      if (e.provider) meta.appendChild(el("span", { class: "ch-msg__prov" }, e.provider));
    }
    wrap.appendChild(meta);
    // The tools behind a reply come first: they happened before the words did.
    if (!mine && !state.hideTools) {
      const used = toolCallsNode(e.tool_calls);
      if (used) wrap.appendChild(used);
    }
    // What THEY wrote is always plain text — those are strangers' words. What
    // Jarvis wrote back is Markdown (L.45), through the shared sanitizing
    // renderer: no images, links in a new tab. If that renderer isn't there
    // (app.js failed) it stays text, never raw HTML.
    const body = el("div", { class: "ch-msg__text" }, mine || !e.text || !(global.JarvisMarkdown && global.JarvisMarkdown.renderInto) ? (e.text || "") : "");
    if (!e.text) body.classList.add("is-empty");
    else if (!mine && global.JarvisMarkdown && global.JarvisMarkdown.renderInto) {
      body.classList.add("ch-msg__text--md", "jv-md");
      try { global.JarvisMarkdown.renderInto(body, e.text, { balance: !!e.clipped }); }
      catch (_) { body.classList.remove("ch-msg__text--md", "jv-md"); body.textContent = e.text; }
    }
    wrap.appendChild(body);
    if (e.clipped) wrap.appendChild(el("div", { class: "ch-msg__more" }, "(shortened here — the full text is in the log)"));
    if (mine && e.allowed === false) {
      wrap.appendChild(el("div", { class: "ch-msg__why" }, [icon("info"), el("span", null, `Stopped at “${e.stage || "gate"}”${e.reason ? ": " + e.reason : ""}`)]));
    }
    if (!mine && e.ok === false && e.error) {
      wrap.appendChild(el("div", { class: "ch-msg__why" }, [icon("warn"), el("span", null, e.error)]));
    }
    if (e.via === "thread") wrap.title = "An older reply: matched to them because this chat is a DM with only them.";
    return wrap;
  }

  function renderConv(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-conv", role: "tabpanel", "aria-labelledby": "ch-tab-conv" });
    const s = convSlot(p);
    if (s.status === "idle" || (s.status === "loading" && !s.data)) { pane.appendChild(loadingBlock("Reading the log…")); return pane; }
    if (s.status === "error" && !s.data) { pane.appendChild(errorBlock(s.error, () => { s.status = "idle"; ensureConv(p); })); return pane; }
    const d = s.data || { entries: [], total: 0, shown: 0, threads: 0, unattributed_replies: 0 };
    const head = el("div", { class: "ch-conv__head" }, [
      el("div", { class: "ch-row__hint" }, [convNote(d), d.threads > 1 ? ` Across ${d.threads} chats.` : ""]),
      el("div", { class: "ch-conv__btns" }, [
        d.entries.some((x) => Array.isArray(x.tool_calls) && x.tool_calls.length)
          ? el("button", { type: "button", class: "btn btn--ghost btn--sm", "aria-pressed": state.hideTools ? "false" : "true", title: "Show or hide the tools Jarvis used for each reply",
              onclick: () => { state.hideTools = !state.hideTools; renderDetail(); } }, [icon("tool"), state.hideTools ? "Show tool calls" : "Hide tool calls"])
          : null,
        el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.status === "loading", onclick: () => { s.status = "idle"; ensureConv(p); } }, [icon("refresh"), "Refresh"]),
      ]),
    ]);
    pane.appendChild(head);
    if (d.total > d.shown && s.limit < CONV_MAX) {
      pane.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm ch-conv__older", disabled: s.status === "loading", onclick: () => ensureConv(p, true) }, "Show older messages"));
    } else if (d.total > d.shown) {
      pane.appendChild(el("div", { class: "ch-row__hint" }, `That's the most the panel shows (${CONV_MAX}). The full history is in the log: jarvis channels-log ${p.platform} <thread>.`));
    }
    if (!d.entries.length) {
      pane.appendChild(el("div", { class: "ch-empty" }, [el("b", null, "Nothing here yet. "), "Messages appear once they write in. If you've turned off ", el("code", null, "log_conversations"), " for this platform, nothing is recorded."]));
      return pane;
    }
    const today = localDay();
    for (const g of groupByDay(d.entries)) {
      pane.appendChild(el("div", { class: "ch-day" }, dayLabel(g.day, today)));
      const col = el("div", { class: "ch-conv" });
      g.entries.forEach((e) => col.appendChild(messageNode(e)));
      pane.appendChild(col);
    }
    if (d.unattributed_replies > 0) {
      pane.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"),
        el("span", null, `${d.unattributed_replies} older repl${d.unattributed_replies === 1 ? "y" : "ies"} in group chats ${d.unattributed_replies === 1 ? "isn't" : "aren't"} shown: before replies were tagged with who they were for, a group reply couldn't be tied to one person. New replies are.`)]));
    }
    return pane;
  }

  // ---- History: the change log of this person's access  (L.36-P15) ---------
  // Every string comes from the log and is shown with textContent (el()).
  function historyRow(e) {
    const how = e.via === "panel" ? "panel" : "terminal";
    return el("div", { class: "ch-hist__row" + (e.everyone ? " is-everyone" : "") }, [
      el("span", { class: "ch-msg__time" }, clockOf(e.at)),
      el("span", { class: "ch-hist__text" }, [
        el("span", null, e.text || e.kind),
        e.everyone ? el("span", { class: "ch-hist__tag", title: "This change applied to everyone on the platform, so it is shown on every person." }, "everyone") : null,
      ]),
      el("span", { class: "ch-hist__meta" }, [
        el("span", { class: "ch-hist__tag", title: how === "panel" ? "Made from this web panel." : "Made from the terminal (jarvis channels-…) or by Jarvis itself." }, how),
        e.why ? el("span", { class: "ch-hist__why" }, e.why) : null,
      ]),
    ]);
  }

  function renderHistory(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-history", role: "tabpanel", "aria-labelledby": "ch-tab-history" });
    const s = histSlot(p);
    if (s.status === "idle" || (s.status === "loading" && !s.data)) { pane.appendChild(loadingBlock("Reading the change log…")); return pane; }
    if (s.status === "error" && !s.data) { pane.appendChild(errorBlock(s.error, () => { s.status = "idle"; ensureHistory(p); })); return pane; }
    const d = s.data;
    pane.appendChild(el("div", { class: "ch-conv__head" }, [
      el("div", { class: "ch-row__hint" }, d.tracking_since
        ? `Changes to ${displayName(p)}'s access, newest last. Recorded since ${dateOnly(d.tracking_since)}; earlier changes weren't kept.`
        : "Nothing has been recorded yet. Changes are logged from now on."),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.status === "loading", onclick: () => { s.status = "idle"; ensureHistory(p); } }, [icon("refresh"), "Refresh"]),
    ]));
    if (d.truncated && s.limit < CONV_MAX) {
      pane.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm ch-conv__older", disabled: s.status === "loading", onclick: () => ensureHistory(p, true) }, "Show older changes"));
    } else if (d.truncated) {
      pane.appendChild(el("div", { class: "ch-row__hint" }, `That's the most the panel shows (${CONV_MAX}). The full log is ~/.jarvis/channels/changes.jsonl.`));
    }
    if (!d.entries.length) {
      pane.appendChild(el("div", { class: "ch-empty" }, [el("b", null, "No changes recorded for them. "), "Switching something on or off, a quick setup, a bulk edit or a handle edit will show up here."]));
      return pane;
    }
    const today = localDay();
    for (const g of groupByDay(d.entries)) {
      pane.appendChild(el("div", { class: "ch-day" }, dayLabel(g.day, today)));
      const col = el("div", { class: "ch-hist" });
      g.entries.forEach((e) => col.appendChild(historyRow(e)));
      pane.appendChild(col);
    }
    return pane;
  }

  // ---- Memory: what Jarvis keeps about this person  (L.21 P6) -----------------
  // The panel face of channels/person_memory.py. Every control goes through
  // POST .../memory, which proxies `jarvis channels-memory`, so the terminal
  // and the panel cannot disagree. Three groups:
  //   * "Waiting for your OK" -- `proposed` records: what the person said about
  //     themselves (remember_sender) and summaries of their past conversations
  //     (channels/episodes.py). NONE of these reaches the model until Approve.
  //   * "Jarvis uses" -- `active` facts and conversation summaries.
  //   * "Standing rules" -- read-only here; they are made with the person_instruct
  //     tool or `jarvis channels-memory <platform> <id> rule ...`.
  // Retired records are not listed (they are kept in the file, never shown to the model).
  const memSlot = (p) => slot(state.mem, p, () => ({ status: "idle", data: null, eps: null, error: "", req: 0, acting: false }));
  const MEM_HOW = {
    owner_stated: "you told Jarvis",
    person_stated: "they said it",
    observed: "from the conversation log",
    inferred: "a guess, never stated as fact",
  };
  const memDay = (sec) => { try { return sec ? new Date(sec * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "—"; } catch (_) { return "—"; } };

  async function ensureMem(p) {
    if (p.owner) return;
    const s = memSlot(p);
    if (s.status === "ok" || s.status === "loading") return;
    const req = ++s.req;
    s.status = "loading"; s.error = "";
    if (showing(p, "memory")) renderDetail();
    try {
      const [list, eps, rules] = await Promise.all([
        api("GET", personUrl(p, "memory")),
        api("GET", personUrl(p, "memory/episodes")).catch(() => null),
        api("GET", personUrl(p, "rules")).catch(() => null),   // L.22: their rules, the ones for everyone, conflicts
      ]);
      if (s.req !== req) return;
      s.data = Array.isArray(list.items) ? list.items : []; s.eps = eps; s.rules = rules; s.status = "ok";
    } catch (err) {
      if (s.req !== req) return;
      s.status = "error"; s.error = err.message || "Couldn't read what Jarvis keeps about them.";
    }
    if (showing(p, "memory")) renderDetail();
  }

  async function memAct(p, body, okMsg) {
    const s = memSlot(p);
    s.acting = true;
    if (showing(p, "memory")) renderDetail();
    try {
      const out = await personPost(p, "memory", body);
      toast(typeof okMsg === "function" ? okMsg(out) : okMsg, "success");
      if (body.action === "add") state.memDraft.delete(pkey(p));
    } catch (err) {
      toast(err.message || "Couldn't change that.", "error");
    }
    s.acting = false; state.memConfirm = null; s.status = "idle";
    ensureMem(p);
  }

  function summarizeToast(out) {
    const n = (out.written || []).length;
    if (n) return `Wrote ${n} summar${n === 1 ? "y" : "ies"} — they're under “Waiting for your OK”.${out.left ? ` ${out.left} more waiting.` : ""}`;
    if (out.note) return out.note.charAt(0).toUpperCase() + out.note.slice(1) + ".";
    if (out.handled) return "Nothing in those conversations was worth keeping.";
    return "No finished conversations are waiting.";
  }

  function memItemBase(p, rec, waiting) {
    const k = pkey(p);
    const isEp = rec.kind === "episode";
    const asking = state.memConfirm && state.memConfirm.key === k && state.memConfirm.id === rec.id;
    const span = isEp && rec.source && Array.isArray(rec.source.ts) ? rec.source.ts : null;
    const when = span ? dateOnly(span[0]) : memDay(rec.created);
    const where = isEp && rec.source && rec.source.thread
      ? ` · ${rec.source.thread}${rec.source.line_range ? `, lines ${rec.source.line_range[0]}–${rec.source.line_range[1]}` : ""}`
      : "";
    const meta = el("div", { class: "ch-row__hint" }, `${isEp ? "Conversation" : "Fact"} · ${when} · ${MEM_HOW[rec.provenance] || rec.provenance || "—"}${where}`);
    if (asking) {
      return el("li", { class: "ch-note" }, [el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm removing this" }, [
        el("div", { class: "ch-confirm__text" }, `Remove “${rec.text}”? It's deleted for good.`),
        el("div", { class: "ch-confirm__btns" }, [
          el("button", { type: "button", class: "btn btn--danger btn--sm", onclick: () => memAct(p, { action: "forget", record: rec.id }, "Removed.") }, "Remove"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.memConfirm = null; renderDetail(); } }, "Keep"),
        ]),
      ])]);
    }
    const busy = memSlot(p).acting;
    const btns = [];
    if (waiting) {
      btns.push(el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: busy, title: "Let Jarvis use this when it fits what they say", onclick: () => memAct(p, { action: "approve", record: rec.id }, "Approved. Jarvis can use it now.") }, "Approve"));
      btns.push(el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, title: "Never use this; it won't be written again", onclick: () => memAct(p, { action: "retire", record: rec.id }, "Set aside. Jarvis will never use it.") }, "Set aside"));
    } else {
      btns.push(el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, title: "Stop using this but keep it on file", onclick: () => memAct(p, { action: "retire", record: rec.id }, "Retired. Jarvis no longer uses it.") }, "Retire"));
    }
    btns.push(el("button", { type: "button", class: "ch-iconbtn ch-iconbtn--danger", title: "Delete this", "aria-label": "Delete this", onclick: () => { state.memConfirm = { key: k, id: rec.id }; renderDetail(); } }, [icon("trash")]));
    return el("li", { class: "ch-note" }, [
      el("span", { class: "ch-note__text" }, [el("span", null, rec.text), meta]),
      el("span", { class: "ch-note__btns" }, btns),
    ]);
  }

  // The memory row, plus its "In rooms" setting for what Jarvis already uses (S11). Things
  // still waiting for your OK aren't used anywhere yet, and rules are never gated.
  function memItem(p, rec, waiting) {
    const li = memItemBase(p, rec, waiting);
    const asking = state.memConfirm && state.memConfirm.key === pkey(p) && state.memConfirm.id === rec.id;
    if (!waiting && !asking && rec.kind !== "instruction") { li.classList.add("ch-note--groups"); li.appendChild(groupsControl(p, "mem", rec)); }
    return li;
  }

  function memSection(p, title, aside, items, waiting, empty) {
    const sec = el("div", null, [el("div", { class: "ch-section-title" }, [title, el("span", { class: "ch-section-title__aside" }, aside)])]);
    if (items.length) sec.appendChild(el("ul", { class: "ch-notes ch-notes--edit" }, items.map((r) => memItem(p, r, waiting))));
    else sec.appendChild(el("div", { class: "ch-row__hint" }, empty));
    return sec;
  }

  // ---- Export (L.21) -----------------------------------------------------------
  // Everything saved about ONE person, whatever its status, as a file the owner
  // keeps. The server returns it (GET .../memory/export, never cached); the
  // browser only turns it into a download. Nothing is sent anywhere else.
  function saveFile(name, text, type) {
    const blob = new Blob([text], { type: `${type};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function exportMem(p, format) {
    const s = memSlot(p);
    s.exporting = format;
    if (showing(p, "memory")) renderDetail();
    try {
      const out = await api("GET", personUrl(p, `memory/export?format=${format}`));
      const stamp = new Date().toISOString().slice(0, 10);
      const base = `jarvis-memory-${p.platform}-${p.user_id}-${stamp}`;
      if (format === "text") {
        saveFile(out.filename || `${base}.txt`, out.text || "", "text/plain");
      } else {
        const doc = Object.assign({}, out); delete doc.ok;
        saveFile(`${base}.json`, JSON.stringify(doc, null, 2), "application/json");
      }
      const n = out.count === undefined ? 0 : out.count;
      toast(n ? `Exported ${n} item${n === 1 ? "" : "s"}.` : "Exported — nothing is saved for them yet.", "success");
    } catch (err) {
      toast(err.message || "Couldn't export.", "error");
    }
    s.exporting = null;
    if (showing(p, "memory")) renderDetail();
  }

  // ---- Standing rules (L.22) ---------------------------------------------------
  // Made, ended and deleted here as well as by telling Jarvis; both go through
  // `jarvis channels-memory ... rule`, so the terminal and the panel cannot
  // disagree. Two scopes: "Just them", and "Everyone" (the shared _anyone file).
  // Jarvis follows a rule without telling the person it exists. "Hard" (only for
  // don't-discuss and ask-first) also checks each drafted reply before it is sent.
  const RULE_LABEL = {
    don_t_discuss: "Don't discuss", only_discuss: "Only discuss", ask_owner_first: "Ask you first",
    notify_owner: "Tell you when it comes up", tone: "Tone", custom: "Custom",
  };
  const RULE_HARD_OK = new Set(["don_t_discuss", "ask_owner_first"]);
  const RULE_WORDING = new Set(["tone", "custom"]);
  const RULE_DAYS = [["", "No end date"], ["1", "1 day"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"]];
  const RULE_KIND = { contradiction: "Contradiction", overlap: "Overlap", duplicate: "Duplicate" };

  function ruleLine(rec) {
    const b = rec.instruction || {};
    let text = b.topic || "";
    if (RULE_WORDING.has(b.directive) || !text) {
      const t = String(rec.text || "");
      text = t.includes("--") ? t.split("--").slice(1).join("--").trim() : (text || t);
    }
    return { what: RULE_LABEL[b.directive] || b.directive || "Rule", text };
  }

  async function ruleAct(p, scope, body, okMsg) {
    const s = memSlot(p);
    s.acting = true;
    if (showing(p, "memory")) renderDetail();
    try {
      const out = scope === "everyone" ? await api("POST", "/api/channels/rules/anyone", body) : await personPost(p, "rules", body);
      toast(okMsg, "success");
      if (body.action === "add") { const d = state.ruleDraft.get(pkey(p)); if (d) d.topic = ""; }
      if (out && out.conflicts && out.conflicts.length) toast("That rule pulls against another one — see “Rules that conflict” below.", "info");
    } catch (err) {
      toast(err.message || "Couldn't change that.", "error");
    }
    s.acting = false; state.memConfirm = null; s.status = "idle";
    ensureMem(p);
  }

  function ruleItem(p, rec, scope) {
    const k = pkey(p);
    const b = rec.instruction || {};
    const busy = memSlot(p).acting;
    const { what, text } = ruleLine(rec);
    const asking = state.memConfirm && state.memConfirm.key === `${k}#rule` && state.memConfirm.id === rec.id;
    if (asking) {
      return el("li", { class: "ch-note" }, [el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm deleting this rule" }, [
        el("div", { class: "ch-confirm__text" }, `Delete the rule “${what}${text ? " — " + text : ""}”? It stops applying and is removed for good.`),
        el("div", { class: "ch-confirm__btns" }, [
          el("button", { type: "button", class: "btn btn--danger btn--sm", onclick: () => ruleAct(p, scope, { action: "forget", record: rec.id }, "Rule deleted.") }, "Delete"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.memConfirm = null; renderDetail(); } }, "Keep"),
        ]),
      ])]);
    }
    const bits = [scope === "everyone" ? "Everyone" : "Just them", `added ${memDay(rec.created)}`];
    if (b.hard) bits.push("hard — every reply is checked first");
    const until = b.scope && b.scope.until;
    if (until) bits.push(`ends ${memDay(until)}`);
    return el("li", { class: "ch-note" }, [
      el("span", { class: "ch-note__text" }, [el("span", null, `${what}${text ? " — " + text : ""}`), el("div", { class: "ch-row__hint" }, bits.join(" · "))]),
      el("span", { class: "ch-note__btns" }, [
        el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, title: "Stop applying this rule but keep it on file", onclick: () => ruleAct(p, scope, { action: "retire", record: rec.id }, "Rule ended. Jarvis no longer applies it.") }, "End rule"),
        el("button", { type: "button", class: "ch-iconbtn ch-iconbtn--danger", title: "Delete this rule", "aria-label": "Delete this rule", disabled: busy, onclick: () => { state.memConfirm = { key: `${k}#rule`, id: rec.id }; renderDetail(); } }, [icon("trash")]),
      ]),
    ]);
  }

  function ruleForm(p) {
    const k = pkey(p);
    const s = memSlot(p);
    let d = state.ruleDraft.get(k);
    if (!d) { d = { scope: "person", directive: "don_t_discuss", topic: "", hard: false, days: "" }; state.ruleDraft.set(k, d); }
    const wording = RULE_WORDING.has(d.directive);
    const max = wording ? 240 : 120;
    const sel = (label, value, options, onchange) => {
      const node = el("select", { class: "ch-allowed__select", "aria-label": label, disabled: s.acting },
        options.map(([v, t]) => el("option", { value: v, selected: v === value }, t)));
      node.addEventListener("change", () => onchange(node.value));
      return node;
    };
    const scope = sel("Who the rule is for", d.scope, [["person", `Just ${displayName(p)}`], ["everyone", "Everyone"]], (v) => { d.scope = v; renderDetail(); });
    const directive = sel("What the rule does", d.directive, Object.keys(RULE_LABEL).map((v) => [v, RULE_LABEL[v]]), (v) => { d.directive = v; if (!RULE_HARD_OK.has(v)) d.hard = false; renderDetail(); });
    const days = sel("How long the rule lasts", d.days, RULE_DAYS, (v) => { d.days = v; });
    const input = el("input", { type: "text", class: "ch-name-input ch-instr-input", maxlength: String(max), autocomplete: "off", "aria-label": wording ? "What Jarvis should do" : "The topic", placeholder: wording ? "e.g. Keep answers to two lines." : "e.g. the surprise party", disabled: s.acting });
    input.value = d.topic;
    const add = el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: s.acting || !d.topic.trim(), onclick: () => {
      const body = { action: "add", directive: d.directive, topic: d.topic.trim() };
      if (d.hard && RULE_HARD_OK.has(d.directive)) body.hard = true;
      if (d.days) body.days = parseInt(d.days, 10);
      ruleAct(p, d.scope, body, "Rule saved. It applies from their next message.");
    } }, s.acting ? "Working…" : "Add rule");
    input.addEventListener("input", () => { d.topic = input.value; add.disabled = s.acting || !input.value.trim(); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !add.disabled) { e.preventDefault(); add.click(); } });
    const hardBox = el("input", { type: "checkbox", id: "ch-rule-hard", checked: d.hard ? "" : null, disabled: s.acting || !RULE_HARD_OK.has(d.directive) });
    hardBox.addEventListener("change", () => { d.hard = hardBox.checked; });
    return el("div", null, [
      el("div", { class: "ch-name-edit" }, [scope, directive]),
      el("div", { class: "ch-name-edit", style: "margin-top:6px" }, [input]),
      el("div", { class: "ch-name-edit", style: "margin-top:6px" }, [days, add]),
      el("label", { class: "ch-row__hint", for: "ch-rule-hard", style: "display:flex;gap:8px;align-items:center;margin-top:8px" }, [hardBox,
        "Hard: also check each reply for the topic before it's sent, and replace it if it leaks (one small model call per reply that touches the topic). Only for “Don't discuss” and “Ask you first”."]),
    ]);
  }

  function renderRules(p, s, all) {
    const rules = s.rules;
    const own = rules && Array.isArray(rules.items) ? rules.items : all.filter((r) => r.status === "active" && r.kind === "instruction");
    const everyone = rules && Array.isArray(rules.everyone) ? rules.everyone : [];
    const clashes = rules && Array.isArray(rules.conflicts) ? rules.conflicts : [];
    const box = el("div", null, [el("div", { class: "ch-section-title" }, ["Standing rules", el("span", { class: "ch-section-title__aside" }, String(own.length + everyone.length))])]);
    if (!rules) box.appendChild(el("div", { class: "ch-row__note ch-row__note--bad" }, [icon("warn"), el("span", null, "Couldn't read the rules just now. Use Refresh to try again; what's listed may be incomplete.")]));
    box.appendChild(el("div", { class: "ch-row__hint" }, "Jarvis follows these without telling them they exist, and only when a message touches the topic. You can also just tell Jarvis."));
    if (own.length) box.appendChild(el("ul", { class: "ch-notes ch-notes--edit" }, own.map((r) => ruleItem(p, r, "person"))));
    else box.appendChild(el("div", { class: "ch-row__hint" }, "None for them yet."));
    if (everyone.length) {
      box.appendChild(el("div", { class: "ch-row__hint", style: "margin-top:10px" }, "Rules for everyone (they apply to this person too):"));
      box.appendChild(el("ul", { class: "ch-notes ch-notes--edit" }, everyone.map((r) => ruleItem(p, r, "everyone"))));
    }
    if (clashes.length) {
      box.appendChild(el("div", { class: "ch-section-title", style: "margin-top:12px" }, ["Rules that conflict", el("span", { class: "ch-section-title__aside" }, String(clashes.length))]));
      box.appendChild(el("ul", { class: "ch-notes" }, clashes.map((c) => el("li", { class: "ch-note" }, [el("span", { class: "ch-note__text" }, [
        el("span", null, `“${c.winner_text}” wins over “${c.loser_text}”`),
        el("div", { class: "ch-row__hint" }, `${RULE_KIND[c.kind] || c.kind} · ${c.reason}${c.cross ? " · one of them is for everyone" : ""}. A person's own rule comes before one for everyone, then the newest. End one to settle it.`),
      ])]))));
    }
    box.appendChild(el("div", { class: "ch-section-title", style: "margin-top:12px" }, "Add a rule"));
    box.appendChild(ruleForm(p));
    return box;
  }

  function renderMemory(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-memory", role: "tabpanel", "aria-labelledby": "ch-tab-memory" });
    if (p.owner) {
      pane.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"),
        el("span", null, "This is you. Jarvis keeps what it knows about you in its own memory (the Memory settings), not in a per-person file.")]));
      return pane;
    }
    const s = memSlot(p);
    if (s.status === "idle" || (s.status === "loading" && !s.data)) { pane.appendChild(loadingBlock("Reading what Jarvis keeps…")); return pane; }
    if (s.status === "error" && !s.data) { pane.appendChild(errorBlock(s.error, () => { s.status = "idle"; ensureMem(p); })); return pane; }
    const all = s.data || [];
    const waiting = all.filter((r) => r.status === "proposed" && r.kind !== "instruction");
    const active = all.filter((r) => r.status === "active" && r.kind !== "instruction");
    const eps = s.eps || null;
    const k = pkey(p);

    pane.appendChild(el("div", { class: "ch-conv__head" }, [
      el("div", { class: "ch-row__hint" }, `What Jarvis keeps about ${displayName(p)} beyond the short notes on their Profile. Only “Jarvis uses” reaches the model, and only when it fits what they say.`),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.status === "loading" || s.acting, onclick: () => { s.status = "idle"; ensureMem(p); } }, [icon("refresh"), "Refresh"]),
    ]));

    pane.appendChild(memSection(p, "Waiting for your OK", String(waiting.length), waiting, true,
      "Nothing waiting. Things they say about themselves, and summaries of their finished conversations, land here first — Jarvis can't use them until you approve."));
    pane.appendChild(memSection(p, "Jarvis uses", String(active.length), active, false,
      "Nothing yet. Add a fact below, or approve something above."));

    // Add a fact
    const max = 300;
    const draft = state.memDraft.get(k) || "";
    const input = el("input", { type: "text", class: "ch-name-input ch-instr-input", maxlength: String(max), autocomplete: "off", "aria-label": "Add a fact about them", placeholder: "e.g. Prefers short answers; works nights.", disabled: s.acting });
    input.value = draft;
    const add = el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: s.acting || !draft.trim(), onclick: () => memAct(p, { action: "add", text: input.value.trim() }, "Saved. Jarvis can use it right away.") }, s.acting ? "Working…" : "Add");
    input.addEventListener("input", () => { state.memDraft.set(k, input.value); add.disabled = s.acting || !input.value.trim(); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !add.disabled) { e.preventDefault(); add.click(); } });
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Add a fact"),
      el("div", { class: "ch-name-edit" }, [input, add]),
      el("div", { class: "ch-row__hint" }, "One line. It's marked as coming from you and used when it fits what they say. It never enters your own memory."),
    ]));

    // Summaries of past conversations (P4)
    if (eps) {
      const n = eps.pending || 0;
      const run = el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.acting || !n, onclick: () => memAct(p, { action: "summarize", count: 3 }, summarizeToast) },
        s.acting ? "Working…" : n ? `Summarize ${Math.min(n, 3)} conversation${Math.min(n, 3) === 1 ? "" : "s"}` : "Nothing to summarize");
      const auto = el("input", { type: "checkbox", id: "ch-mem-auto", checked: eps.auto ? "" : null, onchange: async (e) => {
        const on = e.target.checked;
        try { await api("POST", "/api/channels/episodes/auto", { on }); toast(on ? "Jarvis will summarize finished conversations by itself." : "Automatic summaries are off.", "success"); }
        catch (err) { toast(err.message || "Couldn't change that.", "error"); }
        s.status = "idle"; ensureMem(p);
      } });
      pane.appendChild(el("div", null, [
        el("div", { class: "ch-section-title" }, ["Past conversations", el("span", { class: "ch-section-title__aside" }, `${n} waiting`)]),
        el("div", { class: "ch-name-edit" }, [run]),
        el("label", { class: "ch-row__hint", for: "ch-mem-auto", style: "display:flex;gap:8px;align-items:center;margin-top:8px" }, [auto, `Summarize by itself after they write (at most ${eps.daily_cap} a day, ${eps.calls_today} used today)`]),
        el("div", { class: "ch-row__hint" }, `A conversation is “finished” after ${eps.gap_hours} hours of quiet, and needs at least ${eps.min_messages} messages from them. Each summary is one small model call and always waits for your OK, because it's written from what they said.`),
      ]));
    }

    // Standing rules (L.22): made, ended and deleted here
    pane.appendChild(renderRules(p, s, all));

    // Export (L.21)
    const exporting = s.exporting;
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Export"),
      el("div", { class: "ch-name-edit" }, [
        el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: !!exporting || s.acting, onclick: () => exportMem(p, "json") }, [icon("download"), exporting === "json" ? "Exporting…" : "Download as JSON"]),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: !!exporting || s.acting, onclick: () => exportMem(p, "text") }, [icon("download"), exporting === "text" ? "Exporting…" : "Download as text"]),
      ]),
      el("div", { class: "ch-row__hint" }, "Everything saved about them — facts, conversation summaries and rules, including items waiting for your OK and ones you set aside. It contains what they said, so keep the file somewhere private. Nothing is sent anywhere."),
    ]));
    return pane;
  }

  function renderUsage(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-usage", role: "tabpanel", "aria-labelledby": "ch-tab-usage" });
    const s = usageSlot(p);
    if (s.status === "idle" || (s.status === "loading" && !s.data)) { pane.appendChild(loadingBlock("Adding it up…")); return pane; }
    if (s.status === "error" && !s.data) { pane.appendChild(errorBlock(s.error, () => { s.status = "idle"; ensureUsage(p); })); return pane; }
    const d = s.data;
    const w = d.window, m = d.messages, plat = PLATFORMS[p.platform] ? PLATFORMS[p.platform].label : p.platform;

    const range = el("div", { class: "ch-chips", role: "group", "aria-label": "Time range" }, [7, 30, 90].map((n) =>
      el("button", { type: "button", class: "ch-chip" + (d.days === n ? " is-active" : ""), "aria-pressed": d.days === n ? "true" : "false", onclick: () => ensureUsage(p, n) }, `${n} days`)));
    pane.appendChild(el("div", { class: "ch-conv__head" }, [range,
      el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.status === "loading", onclick: () => { s.status = "idle"; ensureUsage(p); } }, [icon("refresh"), "Refresh"])]));

    if (!d.has_ledger) {
      pane.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"),
        el("span", null, "Tokens and tool calls haven't been recorded yet. They're counted from the next message Jarvis answers; the message counts below cover the whole history.")]));
    } else if (!d.all_time.asks) {
      pane.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"),
        el("span", null, `Nothing counted for them since tracking began (${dateOnly(d.tracking_since)}).`)]));
    }

    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, `Last ${d.days} days`), el("div", { class: "ch-summary" }, [
      statCard("Questions answered", String(w.asks), w.failed ? `${w.failed} failed` : "None failed", w.failed ? "limited" : "on"),
      statCard("Tokens", fmtTokens(w.total_tokens), w.asks ? `about ${fmtTokens(w.avg_tokens_per_ask)} each` : "—", "limited"),
      statCard("Tool calls", String(w.tool_calls), d.tools.length ? `${d.tools.length} different tool${d.tools.length === 1 ? "" : "s"}` : "—", w.tool_calls ? "limited" : "off"),
      statCard("Messages sent", String(m.sent), m.turned_away ? `${m.turned_away} turned away` : "None turned away", m.turned_away ? "bad" : "off"),
      statCard("Replies", String(m.replies), "Jarvis sent them", "off"),
      statCard("Share of " + plat, d.share_of_platform === null ? "—" : `${d.share_of_platform}%`, d.share_of_platform === null ? "No usage counted yet." : `of ${fmtTokens(d.platform_total_tokens)} tokens`, "off"),
    ])]));

    const bars = barModel(d.series);
    const chart = el("div", { class: "ch-bars", role: "img", "aria-label": `Tokens per day, last ${d.days} days` });
    bars.forEach((b) => chart.appendChild(el("span", { class: "ch-bar" + (b.tokens ? "" : " is-zero"), style: `height:${b.pct}%`, title: `${b.date}: ${b.asks} answered, ${fmtTokens(b.tokens)} tokens` })));
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, ["Tokens per day", el("span", { class: "ch-section-title__aside" }, `${d.since} → today`)]), chart]));

    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Where the tokens went"), el("dl", { class: "ch-facts" }, [
      el("div", null, [el("dt", null, "Sent to the model"), el("dd", null, fmtTokens(w.input_tokens))]),
      el("div", null, [el("dt", null, "Written by the model"), el("dd", null, fmtTokens(w.output_tokens))]),
      el("div", null, [el("dt", null, "Extra thinking"), el("dd", null, fmtTokens(w.thinking_tokens))]),
      el("div", null, [el("dt", null, "Requests to providers"), el("dd", null, String(w.requests))]),
    ])]));

    if (d.tools.length) {
      pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Tools they caused"),
        el("ul", { class: "ch-notes" }, d.tools.map((t) => el("li", null, `${t.tool} — ${t.calls} call${t.calls === 1 ? "" : "s"}`)))]));
    }
    if (d.providers.length) {
      pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Answered by"),
        el("ul", { class: "ch-notes" }, d.providers.map((x) => el("li", null, `${x.provider} — ${x.asks} answer${x.asks === 1 ? "" : "s"}, ${fmtTokens(x.total_tokens)} tokens`)))]));
    }
    pane.appendChild(el("div", { class: "ch-row__hint" }, [
      "Tokens are the provider's own count, not money — no prices are known here. ",
      d.tracking_since ? `Counting began ${dateOnly(d.tracking_since)}; anything earlier was never tied to a person.` : "",
      " Ever, for them: ", `${d.all_time.asks} answer${d.all_time.asks === 1 ? "" : "s"}, ${fmtTokens(d.all_time.total_tokens)} tokens, ${m.sent_all_time} message${m.sent_all_time === 1 ? "" : "s"} sent.`,
    ]));
    return pane;
  }
  function dateOnly(at) { return dayOf(at) || "—"; }

  const STATE_GLYPH = { pass: "✓", fail: "✗", skipped: "–" };
  const STATE_CH = { pass: "on", fail: "bad", skipped: "off" };

  function renderTest(p) {
    const pane = el("div", { class: "ch-tabpane", id: "ch-pane-test", role: "tabpanel", "aria-labelledby": "ch-tab-test" });
    const s = simSlot(p);
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Test as " + displayName(p)),
      el("div", { class: "ch-row__hint" }, "See what Jarvis would do with a message from them. Nothing is sent, no model is called, and nothing is saved — it won't appear in their conversation or their usage. What they say doesn't matter to the gate, only who they are and where they say it."),
    ]));

    const seg = (label, value) => el("button", { type: "button", class: "ch-seg__btn", id: `ch-seg-${value}`, "aria-pressed": s.context === value ? "true" : "false",
      onclick: () => { if (s.context === value) return; s.context = value; runSim(p); } }, label);
    const controls = el("div", { class: "ch-test__controls" }, [
      el("div", { class: "ch-seg", role: "group", "aria-label": "Where they write" }, [seg("Direct message", "dm"), seg("Group chat", "group")]),
    ]);
    if (s.context === "group") {
      const box = el("input", { type: "checkbox", id: "ch-test-mention", checked: s.mentioned ? "" : null });
      box.checked = !!s.mentioned;
      box.addEventListener("change", () => { s.mentioned = box.checked; runSim(p); });
      controls.appendChild(el("label", { class: "ch-test__check", for: "ch-test-mention" }, [box, "They @mention Jarvis"]));
    }
    controls.appendChild(el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: s.status === "loading", onclick: () => runSim(p) }, s.status === "loading" ? "Testing…" : "Run again"));
    pane.appendChild(controls);

    if (s.status === "error" && !s.result) { pane.appendChild(errorBlock(s.error, () => runSim(p))); return pane; }
    const r = s.result;
    if (!r) { pane.appendChild(loadingBlock("Asking the gate…")); return pane; }
    const out = el("div", { class: "ch-test__out" + (s.status === "loading" ? " is-stale" : "") });

    const failRow = (r.stages || []).find((x) => x.state === "fail");
    const where = r.context === "dm" ? "a direct message" : (r.mentioned ? "a group chat, @mentioning Jarvis" : "a group chat, without mentioning Jarvis");
    out.appendChild(el("div", { class: "ch-verdict", "data-ch": r.answered ? "on" : "off" }, [
      el("div", { class: "ch-verdict__k" }, "In " + where),
      el("div", { class: "ch-verdict__v" }, r.answered ? "Jarvis would answer" : "Jarvis would ignore it"),
      el("div", { class: "ch-verdict__s" }, r.answered ? "The message gets through the gate." : `Stopped at “${failRow ? failRow.label : r.stage}”${r.reason ? " — " + r.reason : ""}.`),
    ]));

    out.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "The gate, step by step"),
      el("ol", { class: "ch-stages" }, (r.stages || []).map((x) => el("li", { class: "ch-stage", "data-ch": STATE_CH[x.state] || "off" }, [
        el("span", { class: "ch-stage__g", "aria-label": x.state }, STATE_GLYPH[x.state] || "?"),
        el("span", { class: "ch-stage__l" }, x.label),
        x.detail ? el("span", { class: "ch-stage__d" }, x.detail) : (x.state === "skipped" ? el("span", { class: "ch-stage__d" }, "not reached") : null),
      ])))]));

    const t = r.tools || { state: "none" };
    const toolHead = t.state === "all" ? statCard("Tools", "Everything", "Any tool the platform allows.", "limited")
      : t.state === "custom" ? statCard("Tools", `${t.count} allowed`, "Only the ones ticked for them.", "limited")
        : statCard("Tools", "None", r.answered ? "Answers only, touches nothing." : "Not answered, so no tools either.", "off");
    const toolBox = el("div", null, [el("div", { class: "ch-section-title" }, "Tools"), toolHead, el("div", { class: "ch-row__hint" }, t.why || "")]);
    if (t.state === "custom" && (t.allowed || []).length) {
      toolBox.appendChild(el("div", { class: "ch-gset__chips ch-test__tools" }, t.allowed.map((n) => el("span", { class: "ch-tag", "data-ch": "limited" }, n))));
      toolBox.appendChild(el("div", { class: "ch-row__hint" }, `Plus helpers Jarvis needs to find them: ${(t.plumbing || []).join(", ")}.`));
    }
    out.appendChild(toolBox);

    const im = r.images || { state: "none" };
    out.appendChild(el("div", { class: "ch-test__images" }, [el("div", { class: "ch-section-title" }, "Pictures"),
      statCard("Pictures", im.state === "owner" ? "Always read" : im.state === "on" ? "On" : im.state === "off" ? "Off" : "—", im.why || "", im.state === "on" || im.state === "owner" ? "limited" : "off")]));

    (r.notes || []).forEach((n) => out.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"), el("span", null, n)])));
    if (r.saved === false && r.model_called === false) {
      out.appendChild(el("div", { class: "ch-test__foot" }, [icon("shield"), "Nothing was sent, saved or counted, and no model was called."]));
    }
    pane.appendChild(out);
    return pane;
  }

  // ---- right -------------------------------------------------------------
  function kv(k, v, ch) {
    return el("div", { class: "ch-kv", "data-ch": ch || null }, [el("span", { class: "ch-kv__k" }, k), el("span", { class: "ch-kv__v" }, [ch ? el("i") : null, v])]);
  }

  function globalSets(platform) {
    const block = state.config[platform] || {};
    const SETS = [
      { key: "dm_allowlist", short: "dm", label: "DM list", hint: "may open a DM" },
      { key: "reply_allowlist", short: "reply", label: "Reply list", hint: "gets an answer" },
      { key: "tool_allowlist", short: "tool", label: "Tool list", hint: "may run tools" },
      { key: "image_allowlist", short: "image", label: "Picture list", hint: "may send Jarvis pictures (Discord)" },
    ];
    return SETS.map((set) => {
      const entries = Array.isArray(block[set.key]) ? block[set.key] : [];
      const chips = el("div", { class: "ch-gset__chips" });
      if (!entries.length) chips.appendChild(el("span", { class: "ch-row__hint" }, "nobody"));
      for (const entry of entries) {
        chips.appendChild(el("span", { class: "channels-chip" + (entry === "*" ? " channels-chip--wildcard" : "") }, [
          entry === "*" ? "everyone (*)" : entry,
          el("button", { type: "button", class: "channels-chip__remove", title: `Remove ${entry}`, onclick: () => globalMutate(platform, set.short, entry, true, err) }, "\u00d7"),
        ]));
      }
      const input = el("input", { type: "text", placeholder: "id, handle or *", autocomplete: "off", "aria-label": `Add to ${set.label}` });
      const err = el("div", { class: "ch-gset__err" });
      const add = () => { const v = input.value.trim(); if (!v) return; input.value = ""; globalMutate(platform, set.short, v, false, err); };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
      return el("div", { class: "ch-gset" }, [
        el("div", { class: "ch-gset__label" }, [set.label + " ", el("span", null, "— " + set.hint)]), chips,
        el("div", { class: "ch-gset__add" }, [input, el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: add }, "Add")]), err,
      ]);
    });
  }

  // ---- Servers (Discord) --------------------------------------------------
  // One compact switch row. `locked` is the reason it can't be changed.
  function srvRow(label, hint, on, locked, busy, onToggle) {
    const sw = el("button", {
      type: "button", class: "ch-switch", role: "switch", "aria-checked": on ? "true" : "false",
      "aria-label": `${label}: ${on ? "on" : "off"}`, disabled: !!locked || busy, title: locked || null,
      onclick: () => { if (!locked && !busy) onToggle(!on); },
    });
    return el("div", { class: "ch-srow" + (locked ? " is-locked" : "") + (busy ? " is-busy" : "") }, [
      el("div", { class: "ch-srow__t" }, [el("div", { class: "ch-srow__label" }, label),
        el("div", { class: "ch-srow__hint" }, locked || hint)]),
      sw,
    ]);
  }
  const srvBusy = (kind, id, sw) => state.serverBusy.has(`${kind}:${id}|${sw}`);

  function channelRow(s, c) {
    const offHere = c.override.enabled === "off";
    // We never write "on" for a channel inside a switched-off server (that would
    // be a grant), so the way back is to turn the server on first.
    const serverOff = s.override.enabled === "off";
    let locked = "";
    if (!offHere && !c.answering) locked = c.why_not.length ? `Not answering: ${c.why_not.join("; ")}.` : "Not answering here.";
    else if (offHere && serverOff) locked = "The whole server is switched off. Turn the server on first.";
    const row = srvRow("#" + (c.name || c.id), c.name ? c.id : "Name not seen yet", c.answering && !offHere, locked, srvBusy("channel", c.id, "enabled"),
      (turnOn) => serverSwitch("channel", c.id, "enabled", turnOn ? "inherit" : "off"));
    row.classList.add("ch-srow--chan");
    // Channel spaces S11: Reading / Saving for this channel, and a way into its saved conversation.
    const wrap = el("div", { class: "ch-chanx" }, [row]);
    readingSavingRows(c, "channel", c.id, s).forEach((r) => { r.classList.add("ch-srow--chan", "ch-srow--sub"); wrap.appendChild(r); });
    if (roomForChannel(roomsOf(state.spaces.data), "discord", c.id)) {
      wrap.appendChild(el("div", { class: "ch-chanx__btns" }, [el("button", { type: "button", class: "btn btn--ghost btn--sm", "data-open-room": c.id, title: "Read what was saved from this channel", onclick: () => openRoom("discord", c.id) }, [icon("chat"), "Conversation"])]));
    }
    return wrap;
  }

  // The switches of one server (a server entry's Permissions tab): answer, tools, mention,
  // reading, saving, then its channels. `s` is a row of /api/channels/servers, `v` the whole view.
  function serverSwitchRows(s, v) {
    const body = el("div", { class: "ch-server__body" });
    if (!s.known) body.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"),
      el("span", null, "Jarvis hasn't seen this server yet. It appears in the list because the config mentions it; its name shows up once the bot connects.")]));
    if (!s.left && !s.answering && s.why_not.length) body.appendChild(el("div", { class: "ch-row__note" }, [icon("warn"), el("span", null, "Not answering: " + s.why_not.join("; ") + ".")]));
    body.appendChild(srvRow("Answer here", "Jarvis replies when it's addressed in this server.", s.override.enabled !== "off", s.left ? "Jarvis is no longer in this server." : "", srvBusy("guild", s.id, "enabled"),
      (turnOn) => serverSwitch("guild", s.id, "enabled", turnOn ? "inherit" : "off")));
    body.appendChild(srvRow("Allow tools", "Whether people here can make Jarvis run tools.", s.tools, v.platform_tools ? "" : "Tools are off for all of Discord (allow_tools).", srvBusy("guild", s.id, "tools"),
      (turnOn) => serverSwitch("guild", s.id, "tools", turnOn ? "inherit" : "off")));
    body.appendChild(srvRow("Needs an @mention", "Only reply to messages that mention Jarvis.", s.mention, v.platform_mention && s.override.mention !== "on" ? "Discord requires a mention everywhere." : "", srvBusy("guild", s.id, "mention"),
      (turnOn) => serverSwitch("guild", s.id, "mention", turnOn ? "on" : "inherit")));
    readingSavingRows(s, "guild", s.id, null).forEach((r) => body.appendChild(r));
    if (s.channels.length) {
      body.appendChild(el("div", { class: "ch-section-title" }, "Channels"));
      const list = el("div", { class: "ch-chans" });
      s.channels.forEach((c) => list.appendChild(channelRow(s, c)));
      body.appendChild(list);
    } else if (s.known) {
      body.appendChild(el("div", { class: "ch-row__hint" }, "No text channels recorded yet. They're read when the bot connects."));
    }
    return body;
  }

  // "Allowed servers": the allowed_guilds filter, editable. Only narrows access --
  // the server refuses to remove the last entry (an empty list = every server).
  function allowedGuildsBlock(v) {
    const g = state.guildAdd;
    const ids = v.allowed_guilds || [];
    const box = el("div", { class: "ch-allowed", "data-allowed-guilds": "" });
    box.appendChild(el("div", { class: "ch-allowed__title" }, "Allowed servers"));
    box.appendChild(el("div", { class: "ch-row__hint" }, ids.length
      ? "Only these servers get answers. Any other server is ignored."
      : "No filter: Jarvis answers in every server it's in (the switches below still apply). Adding one turns the filter on."));
    if (ids.length) {
      const chips = el("div", { class: "ch-gset__chips ch-allowed__chips" });
      const only = ids.length === 1;
      ids.forEach((id) => {
        const name = guildName(v, id);
        chips.appendChild(el("span", { class: "channels-chip", "data-guild": id }, [
          name ? `${name} \u00b7 ${id}` : id,
          el("button", {
            type: "button", class: "channels-chip__remove", disabled: only || g.busy,
            title: only ? "This is the only entry. Removing it would open every server; clear it from a terminal if you mean that." : `Stop allowing ${name || id}`,
            "aria-label": `Stop allowing ${name || id}`, onclick: () => guildMutate(id, true, false),
          }, "\u00d7"),
        ]));
      });
      box.appendChild(chips);
      if (only) box.appendChild(el("div", { class: "ch-row__hint" }, [
        "This is the only entry, so it can't be removed here (an empty list means every server). To clear the filter on purpose: ",
        el("code", null, "jarvis channels-set discord allowed_guilds []")]));
    }
    const choices = guildChoices(v);
    const select = el("select", { class: "ch-allowed__select", "aria-label": "Pick a server to allow", disabled: g.busy || !choices.length },
      [el("option", { value: "" }, choices.length ? "Pick a server Jarvis is in\u2026" : "No other servers seen yet")]
        .concat(choices.map((c) => el("option", { value: c.id, selected: g.pick === c.id }, `${c.name || "Unnamed server"} \u00b7 ${c.id}`))));
    const input = el("input", { type: "text", class: "ch-allowed__id", inputmode: "numeric", autocomplete: "off", spellcheck: "false", maxlength: "25",
      placeholder: "or paste a server id", "aria-label": "Server id", value: g.text || null, disabled: g.busy });
    select.addEventListener("change", () => { g.pick = select.value; if (select.value) { g.text = ""; input.value = ""; } });
    input.addEventListener("input", () => { g.text = input.value; if (input.value.trim()) { g.pick = ""; select.value = ""; } });
    const submit = () => {
      const id = guildPick(input.value, select.value);
      if (!id) { g.error = "Pick a server from the list or paste its id (digits only)."; renderHome(); return; }
      g.error = ""; g.preview = null; guildMutate(id, false, false);
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    box.appendChild(el("div", { class: "ch-allowed__add" }, [select, input,
      el("button", { type: "button", class: "btn btn--ghost btn--sm ch-allowed__btn", disabled: g.busy, onclick: submit }, "Allow")]));
    if (g.preview) {
      const p = g.preview;
      const stop = p.would_stop || [];
      const total = p.would_stop_total || stop.length;
      const lines = [];
      if (total) lines.push(`${total} other server${total === 1 ? "" : "s"} would stop answering: ${stop.map((x) => x.name || x.id).join(", ")}${total > stop.length ? ` and ${total - stop.length} more` : ""}.`);
      else if (!ids.length) lines.push("This turns the filter on: any server not on the list will be ignored.");
      if (p.unknown) lines.push("Jarvis hasn't seen this server. If the id is wrong, Jarvis will stay silent everywhere.");
      box.appendChild(el("div", { class: "ch-allowed__confirm", role: "alert" }, [
        el("div", { class: "ch-allowed__confirm-t" }, `Allow ${guildName(v, p.id) || p.id}?`),
        ...lines.map((t) => el("div", { class: "ch-row__hint" }, t)),
        el("div", { class: "ch-allowed__confirm-btns" }, [
          el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: g.busy, onclick: () => guildMutate(p.id, false, true) }, "Yes, allow it"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: g.busy, onclick: () => { g.preview = null; renderHome(); } }, "Cancel"),
        ]),
      ]));
    }
    box.appendChild(el("div", { class: "ch-gset__err", role: "alert" }, g.error || ""));
    return box;
  }

  // "Allowed channels": the allowed_channels filter, editable (L.36-P18). Only
  // narrows access -- the server refuses to remove the last entry (an empty list
  // = every channel). Sits beside "Allowed servers"; both filters must pass.
  function allowedChannelsBlock(v) {
    const g = state.chanAdd;
    const ids = v.allowed_channels || [];
    const box = el("div", { class: "ch-allowed", "data-allowed-channels": "" });
    box.appendChild(el("div", { class: "ch-allowed__title" }, "Allowed channels"));
    box.appendChild(el("div", { class: "ch-row__hint" }, ids.length
      ? "Only these channels get answers (in a thread, the channel it belongs to). Any other channel is ignored; DMs aren't affected."
      : "No filter: Jarvis answers in every channel it can see (the switches below still apply). Adding one turns the filter on."));
    if (ids.length) {
      const chips = el("div", { class: "ch-gset__chips ch-allowed__chips" });
      const only = ids.length === 1;
      ids.forEach((id) => {
        const label = channelLabel(v, id);
        chips.appendChild(el("span", { class: "channels-chip", "data-channel": id }, [
          label,
          el("button", {
            type: "button", class: "channels-chip__remove", disabled: only || g.busy,
            title: only ? "This is the only entry. Removing it would open every channel; clear it from a terminal if you mean that." : `Stop allowing ${label}`,
            "aria-label": `Stop allowing ${label}`, onclick: () => chanMutate(id, true, false),
          }, "\u00d7"),
        ]));
      });
      box.appendChild(chips);
      if (only) box.appendChild(el("div", { class: "ch-row__hint" }, [
        "This is the only entry, so it can't be removed here (an empty list means every channel). To clear the filter on purpose: ",
        el("code", null, "jarvis channels-set discord allowed_channels []")]));
    }
    const groups = channelChoices(v);
    const total = groups.reduce((n, grp) => n + grp.channels.length, 0);
    const select = el("select", { class: "ch-allowed__select", "aria-label": "Pick a channel to allow", disabled: g.busy || !total },
      [el("option", { value: "" }, total ? "Pick a channel Jarvis can see\u2026" : "No other channels seen yet")]
        .concat(groups.map((grp) => el("optgroup", { label: grp.name || `Server ${grp.id}` },
          grp.channels.map((c) => el("option", { value: c.id, selected: g.pick === c.id }, `#${c.name || "unnamed"} \u00b7 ${c.id}`))))));
    const input = el("input", { type: "text", class: "ch-allowed__id", inputmode: "numeric", autocomplete: "off", spellcheck: "false", maxlength: "25",
      placeholder: "or paste a channel id", "aria-label": "Channel id", value: g.text || null, disabled: g.busy });
    select.addEventListener("change", () => { g.pick = select.value; if (select.value) { g.text = ""; input.value = ""; } });
    input.addEventListener("input", () => { g.text = input.value; if (input.value.trim()) { g.pick = ""; select.value = ""; } });
    const submit = () => {
      const id = channelPick(input.value, select.value);
      if (!id) { g.error = "Pick a channel from the list or paste its id (digits only)."; renderHome(); return; }
      g.error = ""; g.preview = null; chanMutate(id, false, false);
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    box.appendChild(el("div", { class: "ch-allowed__add" }, [select, input,
      el("button", { type: "button", class: "btn btn--ghost btn--sm ch-allowed__btn", disabled: g.busy, onclick: submit }, "Allow")]));
    if (g.preview) {
      const p = g.preview;
      const stop = p.would_stop || [];
      const total = p.would_stop_total || stop.length;
      const lines = [];
      if (total) lines.push(`${total} other channel${total === 1 ? "" : "s"} would stop answering: ${stop.map((x) => `#${x.name || x.id}${x.guild_name ? ` (${x.guild_name})` : ""}`).join(", ")}${total > stop.length ? ` and ${total - stop.length} more` : ""}.`);
      else if (!ids.length) lines.push("This turns the filter on: any channel not on the list will be ignored.");
      if (p.unknown) lines.push("Jarvis hasn't seen this channel. If the id is wrong (or it is a thread: list the channel it belongs to), Jarvis will stay silent everywhere.");
      box.appendChild(el("div", { class: "ch-allowed__confirm", role: "alert" }, [
        el("div", { class: "ch-allowed__confirm-t" }, `Allow ${p.channel_name ? `#${p.channel_name}${p.guild_name ? ` in ${p.guild_name}` : ""}` : p.id}?`),
        ...lines.map((t) => el("div", { class: "ch-row__hint" }, t)),
        p.guild_blocked ? el("div", { class: "ch-row__hint" }, "Its server isn't on the allowed servers, so it would stay silent until that server is allowed too.") : null,
        el("div", { class: "ch-allowed__confirm-btns" }, [
          el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: g.busy, onclick: () => chanMutate(p.id, false, true) }, "Yes, allow it"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: g.busy, onclick: () => { g.preview = null; renderHome(); } }, "Cancel"),
        ]),
      ]));
    }
    box.appendChild(el("div", { class: "ch-gset__err", role: "alert" }, g.error || ""));
    return box;
  }

  // The two filters that decide which servers and channels get answers at all, on the Discord
  // page. (Each server's own switches are on its own entry.)
  function allowedFiltersSection() {
    const v = state.servers;
    const box = el("div", { class: "ch-servers" });
    box.appendChild(el("div", { class: "ch-plat__head" }, [icon("server"), el("div", { class: "ch-plat__name" }, "Which servers and channels get answers"),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", title: "Reload the server list", onclick: loadServers }, [icon("refresh")])]));
    const inner = el("div", { class: "ch-servers__body" });
    if (!v) {
      inner.appendChild(el("div", { class: "ch-row__hint" }, state.serversError || "Servers aren't loaded."));
    } else {
      inner.appendChild(allowedGuildsBlock(v));
      inner.appendChild(allowedChannelsBlock(v));
      if (!v.servers.length) inner.appendChild(el("div", { class: "ch-row__hint" }, "No servers recorded yet. Start the bot (jarvis discord-daemon) and invite it to a server; it shows up on the left once connected."));
      const notes = [];
      if (v.allowed_channels.length) notes.push(`allowed_channels is set (${v.allowed_channels.length}): any channel not on it is ignored.`);
      if (v.orphans.length) notes.push(`${v.orphans.length} channel${v.orphans.length === 1 ? "" : "s"} in the config aren't in any server Jarvis has seen.`);
      for (const n of notes) inner.appendChild(el("div", { class: "ch-row__hint" }, n));
      inner.appendChild(el("div", { class: "ch-row__hint" }, "These two filters only narrow who is answered. A server's own switches (on its entry) only take access away too. To let more people in, use Home's Global lists or a person's Permissions tab."));
    }
    box.appendChild(inner);
    return box;
  }

  // ---- platform tools master switch (L.36-P9) -----------------------------
  // The one switch here that WIDENS access when turned on, so it takes two
  // steps and the second is the server's, not this page's: the first click
  // asks for a PREVIEW (who would be able to run tools right after; nothing
  // written), "Turn it on" sends confirm:true. Turning it off applies at once.
  // The server refuses it while the tool list holds "*".
  function masterSlot(id) {
    if (!state.master.has(id)) state.master.set(id, { status: "idle", data: null, error: "" });
    return state.master.get(id);
  }
  async function masterCall(id, value, confirm) {
    const m = masterSlot(id);
    m.status = "busy"; m.error = "";
    renderHome();
    try {
      const out = await api("POST", `/api/channels/platform/${encodeURIComponent(id)}/master-tools`, { value, confirm: !!confirm });
      if (out.dry_run) { m.status = "ready"; m.data = out.view; renderHome(); return; }
      m.status = "idle"; m.data = null;
      toast(out.note || (value ? "Tool use switched on." : "Tool use switched off."), value ? "success" : "info");
      await load(true);
    } catch (e) {
      m.status = "error"; m.error = e.message || "Couldn't change it."; renderHome();
    }
  }
  function masterControl(id, pl) {
    const m = masterSlot(id);
    const label = PLATFORMS[id].label;
    const box = el("div", { class: "ch-master", "data-plat": id });
    const busy = m.status === "busy";
    if (pl.allow_tools) {
      box.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm", id: `ch-master-off-${id}`, disabled: busy, onclick: () => masterCall(id, false, false) }, "Turn tool use off"));
      box.appendChild(el("span", { class: "ch-row__hint" }, `Nobody can run tools from ${label} while it's off; the lists and limits stay as they are.`));
    } else if (m.status === "ready" && m.data) {
      const v = m.data;
      const names = (v.people || []).filter((r) => r.reachable && !r.owner).map((r) => r.name);
      const text = v.reachable_others
        ? `${v.reachable_others} ${v.reachable_others === 1 ? "person" : "people"} besides you could then make Jarvis run tools on this PC from ${label}: ${names.slice(0, 5).join(", ")}${names.length > 5 ? " and others" : ""}.`
        : `Nobody besides you is on the ${label} tool list (and answered), so this changes nothing for anyone else yet.`;
      box.appendChild(el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": "Confirm tool use" }, [
        el("div", { class: "ch-confirm__text" }, `Turn tool use on for ${label}? ${text}`),
        v.perms_error ? el("div", { class: "ch-row__note ch-row__note--bad" }, [icon("warn"), el("span", null, "Their limits can't be read, so messages will run without tools until that's fixed.")]) : null,
        el("div", { class: "ch-confirm__btns" }, [
          el("button", { type: "button", class: "btn btn--danger btn--sm", id: `ch-master-confirm-${id}`, onclick: () => masterCall(id, true, true) }, "Turn it on"),
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { m.status = "idle"; m.data = null; renderHome(); } }, "Cancel"),
        ]),
      ]));
    } else {
      box.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm", id: `ch-master-on-${id}`, disabled: busy, onclick: () => masterCall(id, true, false) }, busy ? "Checking…" : "Turn tool use on…"));
      box.appendChild(el("span", { class: "ch-row__hint" }, "Shows who it would let run tools first; nothing changes until you confirm."));
    }
    if (m.status === "error") box.appendChild(el("div", { class: "ch-row__note ch-row__note--bad", role: "alert" }, [icon("warn"), el("span", null, m.error)]));
    return box;
  }

  // ---- turned away recently (L.36-P14) ------------------------------------
  // Who the gate refused lately, from the transcript the gate already writes:
  // handle, how often, when, which kind of chat, the gate's own reason. Never
  // what they wrote. Add is the ordinary add-a-person route, which creates a
  // row and nothing else; people already in the list get Open instead.
  async function loadDenied() {
    const d = state.denied;
    const req = ++d.req;
    d.status = "loading"; d.error = "";
    renderHome();
    try {
      const data = await api("GET", "/api/channels/denied?days=14");
      if (req !== d.req) return;
      d.status = "ready"; d.data = data;
    } catch (e) {
      if (req !== d.req) return;
      d.status = "error"; d.error = e.message || "Couldn't read it.";
    }
    renderHome();
  }
  async function addDenied(r) {
    try {
      await api("POST", "/api/channels/people", { platform: r.platform, ident: r.user_id });
    } catch (e) { toast(e.message || "Couldn't add them.", "error"); return; }
    toast("Added to People. They still have no access.", "success");
    await load(true);
    loadDenied();
  }
  function deniedSection() {
    const d = state.denied;
    const box = el("div", { class: "ch-servers ch-denied", id: "ch-denied" });
    box.appendChild(el("div", { class: "ch-plat__head" }, [icon("shield"), el("div", { class: "ch-plat__name" }, "Turned away recently"),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", title: "Reload", onclick: loadDenied }, [icon("refresh")])]));
    const inner = el("div", { class: "ch-servers__body" });
    if (d.status === "loading" && !d.data) inner.appendChild(el("div", { class: "ch-row__hint" }, "Reading the last two weeks…"));
    else if (d.status === "error") inner.appendChild(el("div", { class: "ch-row__hint" }, d.error));
    else if (d.data) {
      const list = d.data.senders || [];
      if (!list.length) inner.appendChild(el("div", { class: "ch-row__hint" }, "Nobody has been turned away in the last two weeks."));
      for (const r of list) {
        const who = r.name || (r.handle ? "@" + r.handle : r.user_id);
        const kind = (r.contexts || []).map((c) => (c === "dm" ? "DM" : "group")).join(" + ");
        const action = r.registered
          ? el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { select(`${r.platform}:${r.user_id}`); } }, "Open")
          : el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: () => addDenied(r) }, "Add");
        inner.appendChild(el("div", { class: "ch-denied__row", "data-user": r.user_id }, [
          el("div", null, [
            el("div", { class: "ch-row__label" }, [who, " ", el("span", { class: "ch-row__hint" }, `· ${PLATFORMS[r.platform] ? PLATFORMS[r.platform].label : r.platform}`)]),
            el("div", { class: "ch-row__hint" }, `${r.count} ${r.count === 1 ? "message" : "messages"} · ${kind} · ${relTime(Date.parse(r.last_at) / 1000)} · ${r.reason || "not allowed"}${r.blocked ? " · blocked" : ""}`),
          ]),
          action,
        ]));
      }
      if (d.data.hidden) inner.appendChild(el("div", { class: "ch-row__hint" }, `${d.data.hidden} more ${d.data.hidden === 1 ? "was" : "were"} turned away but ${d.data.hidden === 1 ? "is" : "are"} allowed now.`));
      inner.appendChild(el("div", { class: "ch-row__hint" }, "What they wrote isn't shown. Adding someone only puts them in People — they still get no replies, DMs, tools or pictures until you switch those on."));
    }
    box.appendChild(inner);
    return box;
  }

  // ---- Home: what used to be the right-hand side panel ----------------------
  // Picked like a person (a pinned card at the top of the list), drawn in the
  // middle pane with a hero and tabs like a person's. Nothing here is new
  // behaviour: every control below is the one the side panel had.
  //
  // Anything that changes what Home, a platform, a server or a group shows calls renderHome(), which redraws the
  // middle pane while one of those is selected, and the left list too (a server's badge and a
  // group's counts come from the same data the switches change).
  function renderHome() {
    if (!dom || entryKind(state.selected) === "person" || !entryKind(state.selected)) return;
    renderList();
    if (state.bulkOpen && state.selectMode && state.picked.size) return; // the bulk editor owns the pane
    renderDetail();
  }

  function platformIds() { return Object.keys(PLATFORMS).filter((id) => state.platforms[id]); }

  // The pinned rows above the list: Home, then one page per platform. They are not people: no
  // filters apply to them, they can't be ticked (so they are hidden while selecting), and they
  // live outside #ch-list so the list's own counts stay people-only.
  function renderHomeCard() {
    const slot = dom && dom.home;
    if (!slot) return;
    slot.textContent = "";
    if (state.selectMode) return;
    const active = state.selected === HOME_KEY;
    slot.appendChild(el("button", {
      type: "button", class: "ch-card ch-card--home" + (active ? " is-active" : ""), id: "ch-home-card",
      "aria-current": active ? "true" : null, "data-key": HOME_KEY,
      onclick: () => select(HOME_KEY),
    }, [
      el("span", { class: "ch-avatar-wrap" }, [el("span", { class: "ch-avatar ch-avatar--home", "aria-hidden": "true" }, [icon("home")])]),
      el("div", { style: "min-width:0" }, [
        el("div", { class: "ch-card__name" }, "Home"),
        el("div", { class: "ch-card__sub" }, "Turned away, global lists"),
      ]),
    ]));
    for (const id of platformIds()) {
      const st = platformState(state.platforms[id]);
      slot.appendChild(entryCard({
        key: platKey(id), ico: id, ch: st.ch === "on" ? "on" : st.ch === "bad" ? "bad" : "off",
        name: PLATFORMS[id].label, sub: "Bot, tools, room settings", badges: [tag(st.text, st.ch)],
      }));
    }
  }

  function homeHero() {
    return el("div", { class: "ch-hero" }, [
      el("span", { class: "ch-avatar-wrap" }, [el("span", { class: "ch-avatar ch-avatar--lg ch-avatar--home", "aria-hidden": "true" }, [icon("home")])]),
      el("div", { class: "ch-hero__main" }, [
        el("div", { class: "ch-hero__nameline" }, [el("div", { class: "ch-hero__name" }, "Home")]),
        el("div", { class: "ch-hero__handle" }, "Who was turned away, and the raw lists. Each platform, server and group chat has its own entry on the left."),
      ]),
    ]);
  }

  // The tab bar of Home, a server or a group: `onPick(id)` switches the tab, `label` names the bar.
  function homeTabBar(defs, active, onPick, label) {
    const pick = onPick || activateHomeTab;
    const bar = el("div", { class: "ch-tabs", role: "tablist", "aria-label": label || "Home" });
    defs.forEach((d, i) => {
      bar.appendChild(el("button", {
        type: "button", class: "ch-tab", role: "tab", id: `ch-tab-${d.id}`, "aria-selected": active === d.id ? "true" : "false",
        "aria-controls": `ch-pane-${d.id}`, tabindex: active === d.id ? "0" : "-1", title: d.title || null,
        onclick: () => pick(d.id),
        onkeydown: (e) => {
          if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
          e.preventDefault();
          const next = defs[(i + (e.key === "ArrowRight" ? 1 : defs.length - 1)) % defs.length];
          pick(next.id); const nb = $(`#ch-tab-${next.id}`); if (nb) nb.focus();
        },
      }, [icon(d.ico), d.label]));
    });
    return bar;
  }

  function activateHomeTab(id) {
    state.homeTab = id;
    renderDetail(true);
  }

  function homePane(id, children) {
    return el("div", { class: "ch-tabpane", id: `ch-pane-${id}`, role: "tabpanel", "aria-labelledby": `ch-tab-${id}` }, children);
  }

  // One status card per platform (enabled, token, owner, tools master switch).
  function platformCard(id) {
    const pl = state.platforms[id];
    const ownerRec = pl.owner ? state.people.find((x) => x.platform === id && (x.user_id === pl.owner || x.handle === pl.owner)) : null;
    const card = el("div", { class: "ch-plat", "data-plat": id }, [
      el("div", { class: "ch-plat__head" }, [icon(id), el("div", { class: "ch-plat__name" }, PLATFORMS[id].label)]),
      el("div", { class: "ch-plat__rows" }, [
        kv("Status", pl.enabled ? "enabled" : "disabled", pl.enabled ? "on" : "off"),
        kv("Bot token", pl.token_set ? "set" : "not set", pl.token_set ? "on" : "bad"),
        kv("Owner", ownerRec ? displayName(ownerRec) : pl.owner ? pl.owner : "unset", pl.owner ? "owner" : "off"),
        kv("Tools master switch", pl.allow_tools ? "on" : "off", pl.allow_tools ? "limited" : "off"),
        kv("People", String(state.people.filter((x) => x.platform === id).length)),
      ]),
    ]);
    const warns = [];
    if (pl.wildcard.reply) warns.push('"*" is in the reply list — anyone who reaches the bot gets an answer.');
    if (pl.wildcard.tool && pl.allow_tools) warns.push('"*" is in the tool list with the master switch on — anyone who reaches the bot can run tools.');
    for (const w of warns) card.appendChild(el("div", { class: "ch-plat__warn" }, w));
    const hints = [];
    if (!pl.token_set) hints.push(["Set the token from a terminal, so it never passes through this browser: ", `jarvis channels-set ${id} ${id === "discord" ? "bot_token" : "access_token"} YOUR_TOKEN`]);
    else if (!pl.enabled) hints.push(["Turn it on: ", `jarvis channels-set ${id} enabled true`]);
    if (!pl.owner) hints.push(["Pick an owner: ", `jarvis channels-set ${id} owner YOUR_USER_ID`]);
    for (const [lead, cmd] of hints) card.appendChild(el("div", { class: "ch-plat__hint" }, [lead, el("code", null, cmd)]));
    if (pl.token_set) card.appendChild(masterControl(id, pl));
    if (pl.token_set && state.config[id]) card.appendChild(roomSettingsBlock(id));
    return card;
  }

  function listsPane() {
    const blocks = [el("div", { class: "ch-globals__note" }, "The raw allow-lists that the switches on each person's Permissions tab read and write. Use these to add someone who hasn't messaged yet, or to manage \"*\" entries.")];
    for (const id of Object.keys(PLATFORMS)) {
      if (!state.config[id]) continue;
      blocks.push(el("div", { class: "ch-home__lists" }, [
        el("div", { class: "ch-section-title" }, PLATFORMS[id].label),
        el("div", { class: "ch-globals__body" }, globalSets(id)),
      ]));
    }
    return homePane("lists", blocks);
  }

  function homeView() {
    const frag = document.createDocumentFragment();
    frag.appendChild(homeHero());
    const defs = homeTabs();
    const active = (defs.find((d) => d.id === state.homeTab) || defs[0]).id;
    frag.appendChild(homeTabBar(defs, active));
    frag.appendChild({
      denied: () => homePane("denied", [deniedSection()]),
      lists: listsPane,
    }[active]());
    return frag;
  }

  // ======================================================================
  // Platform, server and group entries (the Channels tab, left list -> middle pane)
  //
  // Each is picked exactly like a person and has the same shape: a hero, then tabs. A server and
  // a group have Overview (what it is, how it is doing), Permissions (the switches) and
  // Conversation (what was saved: a Discord server shows its known channels to pick from, an
  // Instagram group is just the one conversation). A platform is one page: the bot's state, the
  // tools master switch, room settings and, for Discord, the two allow filters.
  // Nothing is decided here: every switch is the same POST the old Servers and Rooms tabs made.
  // ======================================================================
  function entryHero(o) {
    const hue = `hsl(${hueFor(o.key)} 70% 58%)`;
    const ids = el("div", { class: "ch-hero__ids" }, [
      o.id ? el("span", { class: "ch-id" }, [o.id, el("button", { type: "button", onclick: async () => toast((await copyText(o.id)) ? "ID copied" : "Couldn't copy", "info") }, "copy")]) : null,
      ...(o.tags || []),
    ]);
    return el("div", { class: "ch-hero", style: `--hue: ${hue}` }, [
      el("span", { class: "ch-avatar-wrap", "data-plat": o.plat || null }, [
        el("span", { class: "ch-avatar ch-avatar--lg ch-avatar--entry", style: `--hue: ${hue}`, "aria-hidden": "true" }, [icon(o.ico)]),
        o.plat ? el("span", { class: "ch-avatar__plat" }, [icon(o.plat)]) : null,
      ]),
      el("div", { class: "ch-hero__main" }, [
        el("div", { class: "ch-hero__nameline" }, [el("div", { class: "ch-hero__name" }, o.name)]),
        o.sub ? el("div", { class: "ch-hero__handle" }, o.sub) : null,
        ids,
      ]),
    ]);
  }

  function spacePane(id, children) {
    return el("div", { class: "ch-tabpane", id: `ch-pane-${id}`, role: "tabpanel", "aria-labelledby": `ch-tab-${id}` }, children);
  }
  function activateSpaceTab(id) { state.spaceTab = id; renderDetail(true); }
  const spaceTabBar = (label) => homeTabBar(SPACE_TABS, SPACE_TABS.some((t) => t.id === state.spaceTab) ? state.spaceTab : "overview", activateSpaceTab, label);
  const spaceTabId = () => (SPACE_TABS.some((t) => t.id === state.spaceTab) ? state.spaceTab : "overview");

  // ---- a platform's page
  function platformView(id) {
    const frag = document.createDocumentFragment();
    const st = platformState(state.platforms[id]);
    frag.appendChild(entryHero({ key: platKey(id), ico: id, name: PLATFORMS[id].label, sub: "The bot, the tools master switch and how it treats shared rooms", tags: [tag(st.text, st.ch)] }));
    const body = el("div", { class: "ch-tabpane", id: "ch-pane-platform" }, [platformCard(id)]);
    if (id === "discord") body.appendChild(allowedFiltersSection());
    frag.appendChild(body);
    return frag;
  }

  // ---- a Discord server
  function serverView(entry) {
    const frag = document.createDocumentFragment();
    const st = serverStatus(entry);
    frag.appendChild(entryHero({
      key: serverKey(entry.id), ico: "server", plat: "discord", name: entry.name || (entry.id === UNSORTED ? "Unplaced channels" : "Unnamed server"),
      sub: entry.id === UNSORTED ? "Saved channels whose server Jarvis could not tell" : "Discord server",
      id: entry.id === UNSORTED ? "" : entry.id, tags: [tag("Discord", null, "discord"), tag(st.text, st.ch, st.ico)],
    }));
    frag.appendChild(spaceTabBar("Server"));
    const pane = { perms: serverPerms, conv: serverConv }[spaceTabId()] || serverOverview;
    frag.appendChild(pane(entry));
    return frag;
  }

  function serverOverview(entry) {
    const v = entry.view, rooms = entry.rooms, chans = serverChannels(entry).filter((c) => c.id);
    const inn = sumCount(rooms, "messages_in"), out = sumCount(rooms, "messages_out");
    const last = newestOf(rooms), pane = spacePane("overview", []);
    const stats = [];
    if (!v) stats.push(statCard("Replies", "Unknown", "It isn't in the server registry, so its switches can't be read.", "off"));
    else if (entry.left) stats.push(statCard("Replies", "Left", "Jarvis is no longer in this server.", "off"));
    else if (v.answering) stats.push(statCard("Replies", "Answering", v.mention ? "When mentioned." : "To every message in answered channels.", "on"));
    else stats.push(statCard("Replies", "Not answering", v.why_not.length ? v.why_not.join("; ") + "." : "Switched off here.", "off"));
    stats.push(v ? (v.tools ? statCard("Tools", "Allowed", "People here may make Jarvis run tools.", "limited") : statCard("Tools", "Off", "Answers only, touches nothing.", "off")) : statCard("Tools", "—", "Not known.", "off"));
    stats.push(v ? statCard("Saving and reading", v.saving ? "Saved" : "Not saved", v.ambient ? "Other messages are read, in memory only." : "Other messages are not read.", v.saving ? "on" : "off") : statCard("Saving and reading", "—", "Not known.", "off"));
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Right now"), el("div", { class: "ch-summary" }, stats)]));
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Details"),
      el("dl", { class: "ch-facts" }, [
        el("div", null, [el("dt", null, "Channels"), el("dd", null, String(chans.length))]),
        el("div", null, [el("dt", null, "Saved rooms"), el("dd", null, `${rooms.filter((r) => r.saved).length} of ${rooms.length}`)]),
        el("div", null, [el("dt", null, "Messages in"), el("dd", null, countText(inn))]),
        el("div", null, [el("dt", null, "Replies from Jarvis"), el("dd", null, countText(out))]),
        el("div", null, [el("dt", null, "Last activity"), el("dd", null, last ? dateText(isoToSec(last)) : "—")]),
        v && v.seen ? el("div", null, [el("dt", null, "First seen"), el("dd", null, String(v.seen).replace("T", " ").slice(0, 16))]) : null,
      ]),
    ]));
    const list = el("div", { class: "ch-chan-list" });
    for (const c of chans) {
      const rs = [c.room, ...c.threads].filter(Boolean);
      const row = c.room || rs[0] || null;
      const vc = v && v.channels.find((x) => x.id === c.id);
      list.appendChild(el("button", {
        type: "button", class: "ch-room ch-room--chan" + (rs.length ? "" : " is-empty"), "data-channel": c.id, disabled: !rs.length,
        title: rs.length ? "Read what was saved from this channel" : "Nothing saved from this channel yet",
        onclick: () => openRoom("discord", (row || {}).thread_id),
      }, [
        el("span", { class: "ch-room__ico", "aria-hidden": "true" }, [icon("chat")]),
        el("span", { class: "ch-room__t" }, [
          el("span", { class: "ch-room__name" }, "#" + (c.name || c.id)),
          el("span", { class: "ch-room__sub" }, rs.length ? `${countText(sumCount(rs, "messages_in"))} in · ${countText(sumCount(rs, "messages_out"))} out${c.threads.length ? ` · ${c.threads.length} thread${c.threads.length === 1 ? "" : "s"}` : ""}` : "nothing saved yet"),
        ]),
        vc ? (vc.answering ? tag("Answering", "on") : tag("Not answering", "off")) : null,
      ]));
    }
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Channels"),
      chans.length ? list : el("div", { class: "ch-row__hint" }, "No channel recorded yet. They appear once the bot connects or someone is answered in one.")]));
    return pane;
  }

  function serverPerms(entry) {
    const pane = spacePane("perms", []);
    const v = state.servers;
    if (!entry.view || !v) {
      pane.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"), el("span", null, entry.id === UNSORTED
        ? "These channels have no server Jarvis could name, so there is nothing to switch for the server. Their own switches are in the Conversation tab's room settings once they are placed."
        : "Jarvis has no record of this server (it was only found in a saved room), so its switches can't be shown. They appear once the bot connects to it.")]));
      return pane;
    }
    pane.appendChild(el("div", { class: "ch-globals__note" }, "These switches only take access away; turning one back on removes the override. A server or channel that is off because something wider is off says so and stays locked."));
    pane.appendChild(serverSwitchRows(entry.view, v));
    return pane;
  }

  // The Conversation tab of a server: its known channels (and threads) to pick from, and the open one.
  function serverConv(entry) {
    const pane = spacePane("conv", []);
    const chans = serverChannels(entry), rooms = entry.rooms;
    if (!rooms.length) {
      pane.appendChild(el("div", { class: "ch-empty" }, [el("b", null, "Nothing saved from this server yet. "), "A channel's conversation appears once someone is answered there while saving is on."]));
      if (chans.some((c) => c.id)) pane.appendChild(el("div", { class: "ch-row__hint" }, `Known channels: ${chans.filter((c) => c.id).map((c) => "#" + (c.name || c.id)).join(", ")}.`));
      return pane;
    }
    const wanted = state.chanPick.get(entry.id);
    const newest = [...rooms].sort((a, b) => String(b.modified || "").localeCompare(String(a.modified || "")))[0];
    const open = rooms.find((r) => roomKey(r) === wanted) || newest;
    const listBox = el("div", { class: "ch-rooms__list", role: "list" });
    const pick = (r) => () => { state.chanPick.set(entry.id, roomKey(r)); renderDetail(); };
    for (const c of chans) {
      const rs = [c.room, ...c.threads].filter(Boolean);
      if (c.room) listBox.appendChild(roomCard(c.room, roomKey(c.room) === roomKey(open), pick(c.room), { title: "#" + (c.name || c.id) }));
      else if (c.id) listBox.appendChild(el("div", { class: "ch-room is-empty", "aria-disabled": "true" }, [
        el("span", { class: "ch-room__ico", "aria-hidden": "true" }, [icon("chat")]),
        el("span", { class: "ch-room__t" }, [el("span", { class: "ch-room__name" }, "#" + (c.name || c.id)), el("span", { class: "ch-room__sub" }, rs.length ? "only threads were saved" : "nothing saved yet")]),
      ]));
      for (const t of c.threads) {
        const card = roomCard(t, roomKey(t) === roomKey(open), pick(t));
        card.classList.add("ch-room--thread");
        listBox.appendChild(card);
      }
    }
    pane.appendChild(el("div", { class: "ch-rooms" }, [listBox, el("div", { class: "ch-rooms__view" }, [roomViewer(open, { switches: false })])]));
    return pane;
  }

  // ---- a group chat (an Instagram thread)
  function groupView(row) {
    const frag = document.createDocumentFragment();
    const answering = groupAnswering(row, state.platforms);
    frag.appendChild(entryHero({
      key: groupKey(row.platform, row.thread_id), ico: "people", plat: row.platform, name: roomTitle(row),
      sub: `${(PLATFORMS[row.platform] || {}).label || row.platform} · ${roomKindLabel(row.kind)}`, id: row.thread_id,
      tags: [row.imported ? tag("Imported", "limited", "download") : answering ? tag("Answering", "on", "chat") : tag("Not answering", "off", "blocked")],
    }));
    frag.appendChild(spaceTabBar("Group chat"));
    const pane = { perms: groupPerms, conv: groupConv }[spaceTabId()] || groupOverview;
    frag.appendChild(pane(row));
    return frag;
  }

  function groupOverview(row) {
    const pane = spacePane("overview", []);
    const pl = state.platforms[row.platform], label = (PLATFORMS[row.platform] || {}).label || row.platform;
    const ans = roomAnswerState(row, "enabled", state.config, state.platforms), tools = roomAnswerState(row, "tools", state.config, state.platforms);
    const sv = roomSwitchState(row, "log", state.config, state.servers), rd = roomSwitchState(row, "ambient", state.config, state.servers);
    const stats = [];
    if (row.imported) stats.push(statCard("Replies", "Read-only", "Imported from a data export; nothing here is live.", "off"));
    else stats.push(ans.on ? statCard("Replies", "Answering", roomAnswerState(row, "mention", state.config, state.platforms).on ? "When mentioned." : "To every message.", "on") : statCard("Replies", "Not answering", ans.lock || "Switched off for this group.", "off"));
    stats.push(row.imported ? statCard("Tools", "—", "Not applicable.", "off") : tools.on ? statCard("Tools", "Allowed", "People here may make Jarvis run tools.", "limited") : statCard("Tools", "Off", tools.lock || "Answers only, touches nothing.", "off"));
    stats.push(row.imported ? statCard("Saving and reading", "Saved", "A fixed history.", "on") : statCard("Saving and reading", sv.on ? "Saved" : "Not saved", rd.on ? "Other messages are read, in memory only." : "Other messages are not read.", sv.on ? "on" : "off"));
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Right now"), el("div", { class: "ch-summary" }, stats)]));
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, "Details"),
      el("dl", { class: "ch-facts" }, [
        el("div", null, [el("dt", null, "Platform"), el("dd", null, label + (pl && !pl.enabled ? " (switched off)" : ""))]),
        el("div", null, [el("dt", null, "People seen"), el("dd", null, typeof row.speakers === "number" ? String(row.speakers) : "—")]),
        el("div", null, [el("dt", null, "Messages in"), el("dd", null, typeof row.messages_in === "number" ? String(row.messages_in) : "too many to count here")]),
        el("div", null, [el("dt", null, "Replies from Jarvis"), el("dd", null, typeof row.messages_out === "number" ? String(row.messages_out) : "too many to count here")]),
        el("div", null, [el("dt", null, "Last activity"), el("dd", null, row.modified ? dateText(isoToSec(row.modified)) : "—")]),
        row.saved ? null : el("div", null, [el("dt", null, "Log"), el("dd", null, "nothing saved")]),
      ]),
    ]));
    return pane;
  }

  const GROUP_SWITCHES = [
    { sw: "enabled", label: "Answer here", hint: "Jarvis replies when it's addressed in this group.", on: "inherit", off: "off", okOff: "Jarvis stops answering in this group.", okOn: "Jarvis answers in this group again." },
    { sw: "tools", label: "Allow tools", hint: "Whether people here can make Jarvis run tools.", on: "inherit", off: "off", okOff: "People in this group can no longer make Jarvis run tools.", okOn: "People in this group follow the platform's tool setting again." },
    { sw: "mention", label: "Needs an @mention", hint: "Only reply to messages that mention Jarvis.", on: "on", off: "inherit", okOff: "This group follows the platform's mention setting again.", okOn: "Jarvis only answers when mentioned in this group." },
  ];

  function groupPerms(row) {
    const pane = spacePane("perms", []);
    if (row.imported) {
      pane.appendChild(el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"), el("span", null, "Imported from a data export: a read-only history. Nothing here is live, so there is nothing to switch.")]));
      return pane;
    }
    pane.appendChild(el("div", { class: "ch-globals__note" }, "These switches only take access away (the mention switch only ever asks for more care); turning one back removes the override. One that is locked is set for the whole platform."));
    const box = el("div", { class: "ch-room__switches" });
    for (const d of GROUP_SWITCHES) {
      const st = roomAnswerState(row, d.sw, state.config, state.platforms);
      box.appendChild(srvRow(d.label, d.hint, st.on, st.lock, state.roomBusy.has(`${roomKey(row)}|${d.sw}`), (turnOn) => roomSwitch(row, d.sw, turnOn ? d.on : d.off)));
    }
    for (const [sw, label, hint] of [["ambient", "Read other messages", "Jarvis sees what's said here without being mentioned (memory only)."], ["log", "Save conversation", "Keep what's said here in this group's log."]]) {
      const st = roomSwitchState(row, sw, state.config, state.servers);
      box.appendChild(srvRow(label, hint, st.on, st.lock, state.roomBusy.has(`${roomKey(row)}|${sw}`), (turnOn) => roomSwitch(row, sw, turnOn ? "inherit" : "off")));
    }
    pane.appendChild(box);
    return pane;
  }

  function groupConv(row) {
    return spacePane("conv", [roomViewer(row, { head: false, switches: false })]);
  }

  function renderStatusLine() {
    if (!dom) return;
    dom.statusLine.classList.remove("is-error", "is-busy");
    if (state.error) { dom.statusLine.textContent = "couldn't read channel status"; dom.statusLine.classList.add("is-error"); return; }
    if (state.loading && !state.people.length) { dom.statusLine.textContent = "reading people and permissions…"; dom.statusLine.classList.add("is-busy"); return; }
    const owners = state.people.filter((p) => p.owner).length;
    const answered = state.people.filter((p) => p.effective.answered && !p.blocked).length;
    const tooled = state.people.filter((p) => p.effective.tools !== "none").length;
    const blocked = state.people.filter((p) => p.blocked).length;
    dom.statusLine.textContent = `${state.people.length} registered · ${answered} answered · ${tooled} with tools · ${owners} owner${owners === 1 ? "" : "s"}${blocked ? ` · ${blocked} blocked` : ""}`;
  }

  // ---- "+ Add a new person" — a panel of its own --------------------------
  // Opens #channels-add-overlay, stacked above the Channels panel: platform
  // cards, id/handle, optional name, and a live preview of the list entry.
  // Same rule server.js applies to the id, so a typo is caught before the trip.
  const IDENT_OK = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const PLAT_BLURB = {
    discord: "A numeric user id is best — it never changes. A username works too.",
    instagram: "A handle is normal: Instagram only shows an id once the person writes.",
  };
  let addForm = null; // { plat, ident, name, err, notice, submit, sync } while the panel is open

  const identOf = (raw) => String(raw || "").trim().replace(/^@/, "");
  function findExisting(platform, ident) {
    const v = ident.toLowerCase();
    if (!v) return null;
    return state.people.find((p) => p.platform === platform && ((p.user_id || "").toLowerCase() === v || (p.handle || "").toLowerCase() === v)) || null;
  }

  function renderAdd() {
    if (!dom || !dom.addOverlay) return;
    dom.addOverlay.hidden = !state.adding;
    if (!state.adding) { dom.addBody.textContent = ""; addForm = null; return; }
    if (dom.addBody.childNodes.length) return; // keep what they're typing across re-renders
    buildAddForm();
  }

  function buildAddForm() {
    const body = dom.addBody;
    const ident = el("input", { type: "text", id: "ch-add-ident", class: "ch-addp__input ch-addp__input--mono", autocomplete: "off", spellcheck: "false", maxlength: "65", "aria-describedby": "ch-add-ident-hint" });
    const name = el("input", { type: "text", id: "ch-add-name", class: "ch-addp__input", autocomplete: "off", maxlength: "48", placeholder: "Optional" });
    const identHint = el("div", { class: "ch-addp__hint", id: "ch-add-ident-hint" });
    const nameHint = el("div", { class: "ch-addp__hint" });
    const notice = el("div", { class: "ch-addp__notice", hidden: true, role: "status" });
    const err = el("div", { class: "ch-addp__err", role: "alert" });
    const submit = dom.addSubmit;
    const previewHost = el("div", { class: "ch-addp__preview" });
    const nextHost = el("div", { class: "ch-addp__next" });

    // -- platform cards (a radio group)
    const cards = Object.keys(PLATFORMS).map((id) => el("button", {
      type: "button", class: "ch-addp__plat", role: "radio", "data-plat": id, "data-id": id,
      onclick: () => { state.addPlatform = id; sync(); ident.focus(); },
    }, [
      el("span", { class: "ch-addp__plat-ico" }, [icon(id)]),
      el("span", { class: "ch-addp__plat-txt" }, [el("b", null, PLATFORMS[id].label), el("small", null, PLAT_BLURB[id] || "")]),
    ]));
    const group = el("div", { class: "ch-addp__plats", role: "radiogroup", "aria-label": "Platform" }, cards);
    group.addEventListener("keydown", (e) => {
      if (!/^Arrow(Left|Right|Up|Down)$/.test(e.key)) return;
      e.preventDefault();
      const ids = Object.keys(PLATFORMS);
      const dir = e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 1;
      state.addPlatform = ids[(ids.indexOf(state.addPlatform) + dir + ids.length) % ids.length];
      sync();
      const on = group.querySelector('[aria-checked="true"]'); if (on) on.focus();
    });

    function renderPreview() {
      const v = identOf(ident.value);
      const nm = name.value.trim();
      const ghost = { platform: state.addPlatform, user_id: v || "?", handle: v, name: nm };
      previewHost.textContent = "";
      const title = nm || (v ? "@" + v : "Name or handle");
      previewHost.appendChild(el("div", { class: "ch-addp__card", style: `--hue: hsl(${hueFor(pkey(ghost))} 70% 58%)`, "data-plat": state.addPlatform }, [
        avatar(ghost, "lg"),
        el("div", { class: "ch-addp__card-main" }, [
          el("div", { class: "ch-hero__name" + (nm || v ? "" : " is-ghost") }, title),
          el("div", { class: "ch-hero__handle" }, v ? `${PLATFORMS[state.addPlatform].label} · ${nm && v ? "@" + v : v}` : PLATFORMS[state.addPlatform].label),
          el("div", { class: "ch-card__badges" }, [tag("Not seen yet", null, "plus"), tag("No reply", "off")]),
        ]),
      ]));
    }

    function renderNext() {
      nextHost.textContent = "";
      const nm = name.value.trim();
      const rows = [
        ["shield", "Grants nothing", "No replies, no DMs and no tools until you switch them on in their Permissions tab."],
        ["user", "Id fills in later", state.addPlatform === "discord"
          ? "If you typed a username, Jarvis swaps in the real id the first time they write."
          : "Jarvis swaps in the real id the first time they write — until then they are listed by handle."],
      ];
      if (nm) rows.push(["lock", "Name is locked", "They can't rename themselves by telling Jarvis “call me …”. You can change it later from their Profile tab."]);
      for (const [ico, t, d] of rows) {
        nextHost.appendChild(el("div", { class: "ch-addp__note" }, [
          el("span", { class: "ch-addp__note-ico" }, [icon(ico)]),
          el("div", null, [el("b", null, t), el("div", { class: "ch-row__hint" }, d)]),
        ]));
      }
    }

    function sync() {
      const plat = state.addPlatform;
      for (const c of cards) {
        const on = c.dataset.id === plat;
        c.setAttribute("aria-checked", on ? "true" : "false");
        c.tabIndex = on ? 0 : -1;
      }
      ident.placeholder = plat === "discord" ? "Discord user id or username" : "Instagram handle or id";
      identHint.textContent = plat === "discord"
        ? "Right-click their profile in Discord (Developer Mode on) → Copy User ID. A leading @ is ignored."
        : "Their Instagram @handle, without spaces. A leading @ is ignored.";
      nameHint.textContent = "What Jarvis should call them. Leave empty to show their handle or id instead.";
      const v = identOf(ident.value);
      const bad = !!v && !IDENT_OK.test(v);
      ident.setAttribute("aria-invalid", bad ? "true" : "false");
      if (!bad) err.textContent = "";
      const hit = !bad ? findExisting(plat, v) : null;
      notice.hidden = !hit;
      notice.textContent = hit ? `${displayName(hit)} is already on the list — adding will just open them.` : "";
      renderPreview();
      renderNext();
    }

    async function go() {
      const v = identOf(ident.value);
      if (!v) { err.textContent = "Type an id or @handle."; ident.setAttribute("aria-invalid", "true"); ident.focus(); return; }
      if (!IDENT_OK.test(v)) {
        err.textContent = "Letters, digits, . _ - only — no spaces, and it can't start with . or -. Put their name in the Name box.";
        ident.setAttribute("aria-invalid", "true"); ident.focus(); return;
      }
      submit.disabled = true; submit.textContent = "Adding…";
      try { await addPerson(state.addPlatform, v, name.value.trim(), err); }
      finally { submit.disabled = false; submit.textContent = "Add person"; }
    }

    for (const input of [ident, name]) {
      input.addEventListener("input", sync);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    }

    const field = (label, aside, input, hint) => el("div", { class: "ch-addp__field" }, [
      el("label", { class: "ch-addp__label", for: input.id }, aside ? [label, el("i", null, aside)] : label),
      input, hint,
    ]);
    body.appendChild(el("div", { class: "ch-addp__form" }, [
      el("div", { class: "ch-addp__field" }, [el("div", { class: "ch-section-title", id: "ch-add-plat-label" }, "1 · Platform"), group]),
      el("div", { class: "ch-addp__field" }, [el("div", { class: "ch-section-title" }, "2 · Who"), field("Id or @handle", "", ident, identHint), notice]),
      el("div", { class: "ch-addp__field" }, [el("div", { class: "ch-section-title" }, "3 · Name"), field("Display name", "optional", name, nameHint)]),
      err,
    ]));
    body.appendChild(el("aside", { class: "ch-addp__aside", "aria-label": "Preview" }, [
      el("div", null, [el("div", { class: "ch-section-title" }, "How they will appear"), previewHost]),
      el("div", null, [el("div", { class: "ch-section-title" }, "What adding does"), nextHost]),
    ]));
    group.setAttribute("aria-labelledby", "ch-add-plat-label");

    addForm = { ident, name, err, sync, go, isDirty: () => !!(ident.value.trim() || name.value.trim()) };
    sync();
    setTimeout(() => { if (ident.isConnected) ident.focus(); }, 0);
  }

  function openAdd() { state.adding = true; renderAdd(); }
  function closeAdd() {
    state.adding = false;
    if (dom) { renderAdd(); dom.addBtn.focus({ preventScroll: true }); }
  }
  // Tab stays inside the add panel while it is open.
  function trapAddTab(e) {
    if (e.key !== "Tab" || !state.adding) return;
    const nodes = [...dom.addOverlay.querySelectorAll("button, input, [tabindex]")].filter((n) => !n.disabled && n.tabIndex >= 0 && n.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function renderAll() { if (!dom) return; renderStatusLine(); renderChips(); renderList(); renderSelBar(); renderDetail(); renderAdd(); }

  // --------------------------------------------------------------- plumbing
  function grabDom() {
    const overlay = $("#channels-overlay");
    if (!overlay) return null;
    return {
      overlay, search: $("#ch-search"), platChips: $("#ch-platform-chips"), stateChips: $("#ch-state-chips"), list: $("#ch-list"),
      count: $("#ch-count"), detail: $("#ch-detail"), home: $("#ch-home"), statusLine: $("#channels-status-line"), refresh: $("#btn-ch-refresh"),
      addBtn: $("#btn-ch-add"), selbar: $("#ch-selbar"), addOverlay: $("#channels-add-overlay"), addBody: $("#ch-add-body"),
      addSubmit: $("#ch-add-submit"), addCancel: $("#ch-add-cancel"), addClose: $("#ch-add-close"),
    };
  }

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    if (state.adding) { // the add panel owns the keyboard while it is open
      if (e.key === "Escape") { e.preventDefault(); closeAdd(); }
      return;
    }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "");
    if (e.key === "Escape") {
      if (state.confirm) { state.confirm = null; renderDetail(); e.preventDefault(); return; }
      if (state.confirmRemove) { state.confirmRemove = null; renderDetail(); e.preventDefault(); return; }
      if (state.forget) { state.forget = null; renderDetail(); e.preventDefault(); return; }
      if (state.confirmNote) { state.confirmNote = null; renderDetail(); e.preventDefault(); return; }
      if (state.editingNote) { state.editingNote = null; renderDetail(); e.preventDefault(); return; }
      if (state.editingName) { state.editingName = null; renderDetail(); e.preventDefault(); return; }
      if (state.bulkOpen) { closeBulk(); e.preventDefault(); return; }
      if (state.selectMode && !(typing && e.target.value)) { setSelectMode(false); e.preventDefault(); return; }
      if (typing && e.target.value) { e.target.value = ""; e.target.dispatchEvent(new Event("input")); return; }
      close(); return;
    }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    if (state.bulkOpen) return; // the bulk editor has no tabs, and the arrows must not move the person behind it
    const kind = entryKind(state.selected);
    if (kind === "home" && /^[1-9]$/.test(e.key)) {   // Home has its own, shorter set of tabs
      const t = homeTabs()[Number(e.key) - 1];
      if (t) activateHomeTab(t.id);
      return;
    }
    if ((kind === "server" || kind === "group") && /^[1-9]$/.test(e.key)) {   // a server or a group: Overview, Permissions, Conversation
      const t = SPACE_TABS[Number(e.key) - 1];
      if (t) activateSpaceTab(t.id);
      return;
    }
    const byKey = kind === "person" ? { 1: "profile", 2: "perms", 3: "conv", 4: "usage", 5: "test", 6: "history" }[e.key] : null;
    if (byKey) { activateTab(byKey); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      // Home is the first stop, then the platforms, then the servers and groups the filters show,
      // then the people. While ticking people only they are shown, so only they are stops.
      const rows = visiblePeople().map(pkey);
      const spaces = Array.from(dom.list.querySelectorAll('.ch-card[data-entry="server"], .ch-card[data-entry="group"]')).map((c) => c.getAttribute("data-key"));
      const order = state.selectMode ? rows : [HOME_KEY, ...platformIds().map(platKey), ...spaces, ...rows];
      if (!order.length) return;
      e.preventDefault();
      const i = order.indexOf(state.selected);
      const next = i < 0 ? order[Math.min(1, order.length - 1)] : order[Math.min(order.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)))];
      select(next);
      const card = (dom.home.querySelector(".ch-card.is-active") || dom.list.querySelector(".ch-card.is-active")); if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
    }
  }

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;
    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderChips(); renderList(); });
    dom.refresh.addEventListener("click", () => {
      state.tools = null; state.toolsState = "idle";
      state.conv.clear(); state.usage.clear(); state.sim.clear(); state.hist.clear(); state.mem.clear(); state.roomConv.clear();
      load(true).then(runTabLoader);
    });
    if (dom.addBtn) dom.addBtn.addEventListener("click", openAdd);
    if (dom.addOverlay) {
      dom.addCancel.addEventListener("click", closeAdd);
      dom.addClose.addEventListener("click", closeAdd);
      dom.addSubmit.addEventListener("click", () => { if (addForm) addForm.go(); });
      // A stray click on the backdrop must not throw away what was typed.
      dom.addOverlay.addEventListener("click", (e) => { if (e.target === dom.addOverlay && !(addForm && addForm.isDirty())) closeAdd(); });
      dom.addOverlay.addEventListener("keydown", trapAddTab);
    }
    $("#channels-close").addEventListener("click", close);
    dom.overlay.addEventListener("click", (e) => { if (e.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);
    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) { toast("Channels markup is missing from index.html.", "error"); return; }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    state.confirm = null;
    load(false).then(runTabLoader);
    dom.search.focus({ preventScroll: true });
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    if (state.drafts.size && !global.confirm("You have unsaved tool changes. Close anyway?")) return;
    state.drafts.clear();
    state.conv.clear(); state.usage.clear(); state.sim.clear(); state.hist.clear(); state.mem.clear(); state.memDraft.clear(); state.ruleDraft.clear();   // don't keep their messages around while it's closed
    state.adding = false; state.editingName = null; state.confirmRemove = null;
    state.editingNote = null; state.confirmNote = null; state.forget = null;
    state.selectMode = false; state.picked.clear(); state.bulkOpen = false; state.bulk = newBulk();
    state.presetPrev = null; state.presetDone = null;
    renderAdd();
    dom.overlay.hidden = true;
    const prev = state.prevFocus; state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  // ======================================================================
  // Shared rooms (channel spaces S11): the controls and the room viewer.
  //
  // Nothing is decided here either. Every switch POSTs to a route that runs the same CLI a
  // terminal runs (`channels-set`, `channels-server-set`, `channels-space-set`,
  // `channels-note ... groups`, `channels-memory ... groups`), then the list is re-read. The
  // routes only ever take access away, except the three things below that widen what is said
  // or read in a room (group recall on, "use in rooms: on" for one item, reading messages from
  // anyone): those ask for a second click first. Names and messages are strangers' text and go
  // in with textContent only. No token is read or sent.
  // ======================================================================
  const enc = encodeURIComponent;

  async function roomSetting(id, key, value, okMsg) {
    const k = `${id}|${key}`;
    if (state.settingBusy.has(k)) return;
    state.settingBusy.add(k); renderHome();
    try {
      await api("POST", `/api/channels/platform/${enc(id)}/room-setting`, { key, value });
      if (okMsg) toast(okMsg, "success");
    } catch (err) { toast(err.message || "Couldn't save that.", "error"); }
    state.settingBusy.delete(k);
    await load(true);
  }

  async function roomSwitch(row, sw, value) {
    const k = `${roomKey(row)}|${sw}`;
    if (state.roomBusy.has(k)) return;
    state.roomBusy.add(k); renderHome();
    try {
      await api("POST", `/api/channels/spaces/${enc(row.platform)}/${enc(row.thread_id)}`, { switch: sw, value, thread: row.kind === "server_thread" });
      const g = GROUP_SWITCHES.find((d) => d.sw === sw);
      toast(g ? (value === g.off ? g.okOff : g.okOn) : sw === "log" ? (value === "off" ? "This room is no longer saved." : "This room is saved again.") : (value === "off" ? "Jarvis stops reading this room's other messages." : "Jarvis reads this room's other messages again."), "success");
    } catch (err) { toast(err.message || "Couldn't save that.", "error"); }
    state.roomBusy.delete(k);
    await load(true);
  }

  // ---- Platforms tab: the room settings of one platform ----------------------
  function confirmBox(label, text, okId, okLabel, onOk, onCancel) {
    return el("div", { class: "ch-confirm", role: "alertdialog", "aria-label": label }, [
      el("div", { class: "ch-confirm__text" }, text),
      el("div", { class: "ch-confirm__btns" }, [
        el("button", { type: "button", class: "btn btn--danger btn--sm", id: okId, onclick: onOk }, okLabel),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: onCancel }, "Cancel"),
      ]),
    ]);
  }

  function ambientDraftOf(id) {
    if (!state.ambientDraft.has(id)) state.ambientDraft.set(id, { ...ambientOf(state.config, id) });
    return state.ambientDraft.get(id);
  }
  async function saveAmbient(id, confirmed) {
    const saved = ambientOf(state.config, id), d = ambientDraftOf(id);
    if (!Number.isInteger(d.messages) || d.messages < 0 || d.messages > 100) { toast("Messages kept: a whole number from 0 to 100.", "error"); return; }
    if (!Number.isInteger(d.minutes) || d.minutes < 1 || d.minutes > 1440) { toast("Minutes kept: a whole number from 1 to 1440.", "error"); return; }
    if (d.senders === "anyone" && saved.senders !== "anyone" && !confirmed) { state.ambientAsk = id; renderHome(); return; }
    state.ambientAsk = null;
    const changes = [["ambient_messages", d.messages, saved.messages], ["ambient_minutes", d.minutes, saved.minutes], ["ambient_senders", d.senders, saved.senders]].filter((c) => c[1] !== c[2]);
    if (!changes.length) return;
    const k = `${id}|ambient`;
    state.settingBusy.add(k); renderHome();
    try {
      for (const [key, value] of changes) await api("POST", `/api/channels/platform/${enc(id)}/room-setting`, { key, value });
      state.ambientDraft.delete(id);
      toast("Reading settings saved.", "success");
    } catch (err) { toast(err.message || "Couldn't save that.", "error"); }
    state.settingBusy.delete(k);
    await load(true);
  }

  function roomSettingsBlock(id) {
    const label = PLATFORMS[id].label;
    const box = el("div", { class: "ch-rs", "data-plat": id });
    box.appendChild(el("div", { class: "ch-rs__title" }, "Shared rooms"));
    // 1. group recall: the master switch for what Jarvis may repeat about someone in a room.
    const recall = recallOn(state.config, id);
    box.appendChild(srvRow("Use saved notes and memories in rooms",
      recall ? "On: a note or fact that follows this switch can be used in a room reply. Each one has its own setting on the person's page."
        : "Off: room replies never use what Jarvis has saved about someone, unless that one item is set to on.",
      recall, "", state.settingBusy.has(`${id}|group_recall`),
      (turnOn) => { if (turnOn) { state.recallAsk = id; renderHome(); } else { state.recallAsk = null; roomSetting(id, "group_recall", false, "Room replies stop using saved notes and memories (unless an item is set to on)."); } }));
    if (state.recallAsk === id && !recall) {
      box.appendChild(confirmBox("Confirm group recall", `Turn on saved notes and memories in ${label} rooms? Everyone in a room can read what Jarvis says there, so a fact about one person could be repeated to the others. Items you set to "off" stay out.`,
        `ch-recall-confirm-${id}`, "Turn it on", () => { state.recallAsk = null; roomSetting(id, "group_recall", true, "Room replies can now use saved notes and memories that follow this switch."); }, () => { state.recallAsk = null; renderHome(); }));
    }
    // 1b. Instagram only: the UNOFFICIAL live group transport's kill switch (S9c).
    if (id === "instagram") {
      const live = transportOn(state.config, id);
      box.appendChild(srvRow("Live group chats (unofficial)",
        live ? "On: if `jarvis instagram-group-serve` is running, it reads the group threads listed as pt-<id> and answers when mentioned. Turn this off and it stops within one cycle."
          : "Off: nothing reads or answers Instagram group chats. Meta's official API does not support them.",
        live, "", state.settingBusy.has(`${id}|group_transport`),
        (turnOn) => { if (turnOn) { state.transportAsk = true; renderHome(); } else { state.transportAsk = false; roomSetting(id, "group_transport", false, "The unofficial group transport is off. A running one stops within a cycle."); } }));
      if (state.transportAsk && !live) {
        box.appendChild(confirmBox("Confirm the unofficial group transport", "This goes outside Meta's official API and Instagram's terms of use. The account it signs in as can be challenged, rate-limited or banned, and it can stop working without notice. Use a dedicated account, never your main one. This only flips the switch: signing in and starting it happen in a terminal (jarvis instagram-group-login, then jarvis instagram-group-serve), and nothing is read until you list a thread.",
          "ch-transport-confirm-" + id, "Turn it on", () => { state.transportAsk = false; roomSetting(id, "group_transport", true, "The unofficial group transport is on. Start it from a terminal."); }, () => { state.transportAsk = false; renderHome(); }));
      }
    }
    // 2. reading other messages
    const a = ambientOf(state.config, id), d = ambientDraftOf(id);
    const busy = state.settingBusy.has(`${id}|ambient`);
    const changed = d.messages !== a.messages || d.minutes !== a.minutes || d.senders !== a.senders;
    const num = (field, min, max, labelText) => {
      const input = el("input", { type: "number", class: "ch-num", id: `ch-amb-${field}-${id}`, min: String(min), max: String(max), step: "1", value: String(d[field]), "aria-label": labelText, disabled: busy });
      input.value = String(d[field]);
      input.addEventListener("change", () => { const v = Number(input.value); d[field] = input.value.trim() === "" ? NaN : v; renderHome(); });
      return el("label", { class: "ch-rs__field" }, [el("span", null, labelText), input]);
    };
    const senderBtn = (value, text) => el("button", { type: "button", role: "radio", "aria-checked": d.senders === value ? "true" : "false", id: `ch-amb-senders-${value}-${id}`, disabled: busy, onclick: () => { d.senders = value; renderHome(); } }, text);
    box.appendChild(el("div", { class: "ch-rs__block" }, [
      el("div", { class: "ch-srow__label" }, "Reading other messages in a room"),
      el("div", { class: "ch-srow__hint" }, ambientText(a)),
      el("div", { class: "ch-rs__fields" }, [
        num("messages", 0, 100, "Messages kept (0 = off)"),
        num("minutes", 1, 1440, "Minutes kept"),
        el("div", { class: "ch-rs__field" }, [el("span", null, "Whose messages"),
          el("div", { class: "ch-seg", role: "radiogroup", "aria-label": "Whose messages" }, [senderBtn("approved", "Approved"), senderBtn("anyone", "Anyone")])]),
      ]),
      el("div", { class: "ch-rs__btns" }, [
        el("button", { type: "button", class: "btn btn--primary btn--sm", id: `ch-amb-save-${id}`, disabled: busy || !changed, onclick: () => saveAmbient(id, false) }, busy ? "Saving…" : "Save"),
        changed ? el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.ambientDraft.delete(id); state.ambientAsk = null; renderHome(); } }, "Reset") : null,
      ]),
      state.ambientAsk === id ? confirmBox("Confirm reading everyone", `Let Jarvis read the messages of anyone in a ${label} room, not only people you've approved? They aren't saved, but Jarvis can see what strangers write there while it answers.`,
        `ch-amb-confirm-${id}`, "Read anyone", () => saveAmbient(id, true), () => { state.ambientAsk = null; renderHome(); }) : null,
      id === "discord" ? el("div", { class: "ch-srow__hint" }, "Discord only sends these messages when the bot has the Message Content intent (message_content_intent).") : null,
    ]));
    // 3. saving: platform-wide, changed from the terminal; per room it is on each room.
    const saving = savingOn(state.config, id);
    box.appendChild(srvRow("Save room conversations", "",
      saving, `Each room's messages are kept in its own log, which the Rooms tab shows. ${saving ? "On" : "Off"} for every room; change that from a terminal: jarvis channels-set ${id} log_conversations ${saving ? "false" : "true"}. One room can be switched off in the Rooms tab${id === "discord" ? ", or per server and channel in the Servers tab" : ""}.`, false, () => {}));
    return box;
  }

  // ---- "Use in rooms" on one note or memory item ------------------------------
  function groupsControl(p, kind, item) {
    const k = pkey(p), flag = groupFlagOf(item), master = recallOn(state.config, p.platform);
    const asking = state.groupAsk && state.groupAsk.key === k && state.groupAsk.kind === kind && state.groupAsk.id === item.id;
    const apply = (value, msg) => {
      state.groupAsk = null;
      if (kind === "note") noteAction(p, { action: "groups", note: item.id, value }, msg);
      else memAct(p, { action: "groups", record: item.id, value }, msg);
    };
    const pick = (value) => {
      if (value === flag) return;
      if (value === "on") { state.groupAsk = { key: k, kind, id: item.id }; renderDetail(); return; }
      apply(value, value === "off" ? "Won't be used in room replies." : "Follows the master switch again.");
    };
    const btn = (value) => el("button", { type: "button", role: "radio", "aria-checked": flag === value ? "true" : "false", "data-flag": value, title: groupUseText(value, master), onclick: () => pick(value) }, value);
    const box = el("div", { class: "ch-groups" }, [
      el("div", { class: "ch-groups__row" }, [
        el("span", { class: "ch-groups__label" }, "In rooms"),
        el("div", { class: "ch-seg ch-seg--sm", role: "radiogroup", "aria-label": "Use this in shared rooms" }, GROUP_FLAGS.map(btn)),
        el("span", { class: "ch-groups__hint" }, groupUseText(flag, master)),
      ]),
    ]);
    if (asking) {
      box.appendChild(confirmBox("Confirm using this in rooms", `Let Jarvis use this in shared rooms, even with the master switch off? Everyone in the room can read what it says there.`,
        `ch-groups-confirm-${item.id}`, "Use in rooms", () => apply("on", "Can be used in room replies."), () => { state.groupAsk = null; renderDetail(); }));
    }
    return box;
  }

  // ---- Servers tab: Reading / Saving per server and channel, and a way into the room ----
  // `scope` is a server or a channel row of the /api/channels/servers view; `serverScope` is
  // the server around a channel (null for a server). Like "Answer here": a room is only ever
  // switched OFF here (turning on removes the override), so a switch that is off because
  // something WIDER is off says so and stays locked.
  function readingSavingRows(scope, kind, id, serverScope) {
    const defs = [
      { sw: "ambient", label: "Read other messages", hint: "Jarvis sees what's said here without being mentioned, in memory only.", on: scope.ambient,
        platformOff: ambientOf(state.config, "discord").messages === 0, platformLock: "Reading is off for all of Discord (ambient_messages is 0)." },
      { sw: "log", label: "Save conversation", hint: "Keep what's said here in this room's log.", on: scope.saving,
        platformOff: !savingOn(state.config, "discord"), platformLock: "Saving is off for all of Discord (log_conversations)." },
    ];
    return defs.map((d) => {
      const offHere = scope.override[d.sw] === "off";
      const serverOff = !!(serverScope && serverScope.override[d.sw] === "off");
      let locked = "";
      if (d.platformOff && !offHere) locked = d.platformLock;
      else if (serverOff && (offHere || !d.on)) locked = "The whole server has this off. Turn the server on first.";
      const row = srvRow(d.label, d.hint, !!d.on && !offHere, locked, srvBusy(kind, id, d.sw), (turnOn) => serverSwitch(kind, id, d.sw, turnOn ? "inherit" : "off"));
      row.dataset.sw = d.sw;
      return row;
    });
  }

  // Jump to a saved room: the Conversation tab of the server it belongs to (with that channel or
  // thread open), or of the group. A room the list can't place stays where it is.
  function openRoom(platform, id) {
    const row = allRooms().find((r) => r.platform === platform && r.thread_id === String(id));
    if (!row) return;
    let key = "";
    if (row.kind === "group") key = groupKey(row.platform, row.thread_id);
    else {
      const e = serverEntries(state.servers, allRooms()).find((x) => x.rooms.some((r) => roomKey(r) === roomKey(row)));
      if (e) { key = serverKey(e.id); state.chanPick.set(e.id, roomKey(row)); }
    }
    if (!key) return;
    state.spaceTab = "conv";
    if (state.selected === key) { renderDetail(true); return; }
    select(key);
  }

  // ---- Rooms tab ---------------------------------------------------------------
  const ROOM_STEP = 100, ROOM_MAX = 500;
  const roomConvSlot = (row) => { const k = roomKey(row); if (!state.roomConv.has(k)) state.roomConv.set(k, { status: "idle", data: null, error: "", limit: ROOM_STEP, req: 0, scrollEnd: false }); return state.roomConv.get(k); };

  async function ensureRoomConv(row, more) {
    const s = roomConvSlot(row);
    if (more) s.limit = Math.min(ROOM_MAX, s.limit + ROOM_STEP * 2);
    else if (s.status === "ok" || s.status === "loading") return;
    const req = ++s.req;
    s.status = "loading"; s.error = "";
    renderHome();
    try {
      const data = await api("GET", `/api/channels/spaces/${enc(row.platform)}/${enc(row.thread_id)}/conversation?limit=${s.limit}`);
      if (s.req !== req) return;
      s.data = data; s.status = "ok"; if (!more) s.scrollEnd = true;
    } catch (err) {
      if (s.req !== req) return;
      s.status = "error"; s.error = err.message || "Couldn't read the room.";
    }
    renderHome();
  }

  async function loadSpaces() {
    try { const d = await api("GET", "/api/channels/spaces"); state.spaces.data = d; state.spaces.error = ""; state.spaces.status = "ok"; }
    catch (err) { state.spaces.error = err.message || "Couldn't read the rooms."; state.spaces.status = "error"; }
    state.roomConv.clear();
    renderHome();
  }

  function roomCard(row, active, onPick, opts) {
    const open = el("button", { type: "button", class: "ch-room" + (active ? " is-active" : ""), "data-room": roomKey(row), "aria-current": active ? "true" : null, onclick: onPick }, [
      el("span", { class: "ch-room__ico", "aria-hidden": "true" }, [icon(row.platform === "instagram" ? "instagram" : "server")]),
      el("span", { class: "ch-room__t" }, [
        el("span", { class: "ch-room__name" }, (opts && opts.title) || roomTitle(row)),
        el("span", { class: "ch-room__sub" }, [roomKindLabel(row.kind), typeof row.messages_in === "number" ? ` · ${row.messages_in} in` : "", typeof row.messages_out === "number" && row.messages_out ? ` · ${row.messages_out} out` : "", row.modified ? ` · ${dayLabel(dayOf(row.modified), localDay())}` : ""].join("")),
      ]),
      row.imported ? el("span", { class: "ch-room__tag" }, "imported") : (!row.saved ? el("span", { class: "ch-room__tag" }, "no log") : null),
    ]);
    return open;
  }

  function roomHeadSwitches(row) {
    if (row.imported) return el("div", { class: "ch-row__note ch-row__note--info" }, [icon("info"), el("span", null, "Imported from a data export: a read-only history. Nothing here is live, so there is nothing to switch.")]);
    const box = el("div", { class: "ch-room__switches" });
    for (const [sw, label, hint] of [["log", "Saving", "Keep what's said here in this room's log."], ["ambient", "Reading other messages", "Jarvis sees what's said here without being mentioned (memory only)."]]) {
      const st = roomSwitchState(row, sw, state.config, state.servers);
      box.appendChild(srvRow(label, hint, st.on, st.lock, state.roomBusy.has(`${roomKey(row)}|${sw}`), (turnOn) => roomSwitch(row, sw, turnOn ? "inherit" : "off")));
    }
    return box;
  }

  // opts.head = false drops the title line (an entry's hero already names the room);
  // opts.switches = false drops the Saving / Reading rows (the entry's Permissions tab has them).
  function roomViewer(row, opts) {
    const o = opts || {};
    const s = roomConvSlot(row);
    const box = el("div", { class: "ch-room-view", "data-room": roomKey(row) });
    if (s.status === "idle") setTimeout(() => ensureRoomConv(row), 0);
    const d = s.data;
    if (o.head !== false) box.appendChild(el("div", { class: "ch-room-view__head" }, [
      el("div", { class: "ch-room-view__title" }, roomTitle(row)),
      el("div", { class: "ch-room-view__tags" }, [tag((PLATFORMS[row.platform] || {}).label || row.platform, null, row.platform), tag(roomKindLabel(row.kind), null, null), row.imported ? tag("Imported", "limited", "download") : null]),
    ]));
    if (o.switches !== false) box.appendChild(roomHeadSwitches(row));
    if (s.status === "idle" || (s.status === "loading" && !d)) { box.appendChild(loadingBlock("Reading the room…")); return box; }
    if (s.status === "error" && !d) { box.appendChild(errorBlock(s.error, () => { s.status = "idle"; ensureRoomConv(row); })); return box; }
    const info = d || { entries: [], total: 0, shown: 0, participants: [], denied: 0 };
    box.appendChild(el("div", { class: "ch-conv__head" }, [
      el("div", { class: "ch-row__hint" }, [convNote(info).replace("for them", "in this room"), info.denied ? ` ${info.denied} line${info.denied === 1 ? " was" : "s were"} turned away.` : ""]),
      el("div", { class: "ch-conv__btns" }, [el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.status === "loading", onclick: () => { s.status = "idle"; ensureRoomConv(row); } }, [icon("refresh"), "Refresh"])]),
    ]));
    if ((info.participants || []).length) {
      box.appendChild(el("div", { class: "ch-room-view__people", "aria-label": "People in this room" },
        info.participants.slice(0, 12).map((who) => el("span", { class: "speaker-chip", title: `${who.messages || 0} message${who.messages === 1 ? "" : "s"}` }, who.name || (who.handle ? "@" + String(who.handle).replace(/^@+/, "") : "user\u2026" + String(who.user_id || "").slice(-4))))));
    }
    if (info.total > info.shown && s.limit < ROOM_MAX) box.appendChild(el("button", { type: "button", class: "btn btn--ghost btn--sm ch-conv__older", disabled: s.status === "loading", onclick: () => ensureRoomConv(row, true) }, "Show older messages"));
    else if (info.total > info.shown) box.appendChild(el("div", { class: "ch-row__hint" }, `That's the most the panel shows (${ROOM_MAX}). The whole log is in the room's file: jarvis channels-space ${row.platform} ${row.thread_id}.`));
    if (!(info.entries || []).length) { box.appendChild(el("div", { class: "ch-empty" }, [el("b", null, "Nothing saved here yet. "), "Messages appear once someone writes in this room while saving is on."])); return box; }
    const list = el("div", { class: "ch-room-view__msgs" });
    const today = localDay();
    for (const g of groupByDay(info.entries)) {
      list.appendChild(el("div", { class: "ch-day" }, dayLabel(g.day, today)));
      const col = el("div", { class: "ch-conv" });
      g.entries.forEach((e) => col.appendChild(messageNode(e, { room: true })));
      list.appendChild(col);
    }
    box.appendChild(list);
    if (s.scrollEnd) { s.scrollEnd = false; requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; }); }
    return box;
  }

  // _pure: the logic with no DOM, for tests/verify_channels_panel.js.
  global.JarvisChannels = {
    open, close,
    _pure: { initials, hueFor, relTime, filterPeople, postureOf, sameScope, scopeOf, groupTools, displayName, STATE_FILTERS, thinkingRow,
             dayOf, clockOf, localDay, dayLabel, groupByDay, fmtTokens, barModel, convNote, summaryText,
             toolCallsLabel, untilLeft, TIME_LIMITS,
             stateFilterOptions, guildIdOk, guildName, guildChoices, guildPick,
             channelIdOk, channelInfo, channelLabel, channelChoices, channelPick,
             homeTabs, platformState, HOME_KEY,
             GROUP_FLAGS, groupFlagOf, recallOn, transportOn, usedInRooms, groupUseText, AMBIENT_DEFAULTS, ambientOf, ambientText, savingOn,
             ROOM_KIND_LABEL, roomKindLabel, roomsOf, roomKey, roomTitle, filterRooms, roomForChannel, roomSwitchOf, serverSwitchOff,
             roomSwitchState, roomSpeaker },
  };
})(window);
