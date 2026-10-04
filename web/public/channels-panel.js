/* ============================================================================
 * channels-panel.js — Menu → Channels (L.36).
 *
 * WHAT IT IS
 * ----------
 * The Channels panel, per person instead of global. Laid out like the Test
 * Checklist — a list on the left, the selected item in the middle, an overview
 * on the right — but the items are the PEOPLE who have messaged Jarvis on
 * Discord / Instagram (registered only: anyone denied at the gate never gets a
 * record, see channels/permissions.py).
 *
 *   left    people: avatar, name, handle, what each can do at a glance
 *   middle  one person — Profile tab, and a Permissions tab with a switch for
 *           every thing they can be allowed to do (DMs, replies, tool use and
 *           WHICH tools, whether Jarvis may DM them, ownership, blocking)
 *   right   one card per platform (enabled, token set, owner, master tools
 *           switch) and, collapsed, the old global allow-lists as a fallback
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
  const OWNER_ONLY = new Set(["send_dm", "recent_dms"]);
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
    plus: P('<path d="M12 5v14M5 12h14"/>'),
    link: P('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
    edit: P('<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/>'),
    chat: P('<path d="M4 5h16v11H9l-5 4V5Z"/><path d="M8 9.5h8M8 12.5h5"/>'),
    chart: P('<path d="M4 4v16h16"/><path d="M8 16v-4M12 16V8M16 16v-6"/>'),
    flask: P('<path d="M9 3h6M10 3v6l-5.5 9.5A1.5 1.5 0 0 0 5.8 21h12.4a1.5 1.5 0 0 0 1.3-2.5L14 9V3"/><path d="M7.5 15h9"/>'),
    refresh: P('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
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
    search: "",
    platformFilter: "all",
    stateFilter: "all",
    tools: null, // live catalogue, loaded when first needed
    toolsState: "idle", // idle | loading | ok | error  (one attempt per refresh)
    toolsError: "",
    toolSearch: "",
    drafts: new Map(), // person key -> { mode, names:Set }
    confirm: null, // { key, flag, ... }
    busy: new Set(), // "<key>|<flag>"
    prevFocus: null,
    adding: false, // the "Add a new person" form is open
    addPlatform: "discord",
    editingName: null, // person key whose name is being typed
    confirmRemove: null, // person key awaiting "Remove" confirmation
    // Per-person data for the read-only tabs, keyed by person key. Cleared by
    // the Refresh button; each entry guards against a stale reply with `req`.
    conv: new Map(),   // { status, data, error, limit, req }
    usage: new Map(),  // { status, data, error, days, req }
    sim: new Map(),    // { context, mentioned, status, result, error, req }
    scrollEnd: false,  // after the next render, show the newest message
  };
  let dom = null;

  const personByKey = (k) => state.people.find((p) => pkey(p) === k) || null;
  const current = () => personByKey(state.selected);

  // ------------------------------------------------------------------- data
  async function load(keepSelection) {
    state.loading = true;
    state.error = "";
    renderAll();
    try {
      const [ppl, cfg] = await Promise.all([
        api("GET", "/api/channels/people"),
        api("GET", "/api/channels").catch(() => null),
      ]);
      state.platforms = ppl.platforms || {};
      state.people = Array.isArray(ppl.people) ? ppl.people : [];
      state.permsError = ppl.perms_error || "";
      state.config = (cfg && cfg.config) || {};
      // Drop drafts for people who no longer exist or whose stored scope now
      // equals the draft (saved elsewhere, e.g. from the terminal).
      for (const [k, d] of [...state.drafts]) {
        const p = personByKey(k);
        if (!p || sameScope(d, scopeOf(p))) state.drafts.delete(k);
      }
      if (!keepSelection || !personByKey(state.selected)) {
        const first = visiblePeople()[0] || state.people[0];
        state.selected = first ? pkey(first) : null;
      }
    } catch (err) {
      state.error = err.message || "Couldn't read channels.";
    }
    state.loading = false;
    renderAll();
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
    dom.stateChips.textContent = "";
    const base = filterPeople(state.people, { search: state.search, platform: state.platformFilter, state: "all" });
    for (const s of STATE_FILTERS) {
      const n = base.filter(s.test).length;
      dom.stateChips.appendChild(el("button", {
        type: "button", class: "ch-chip" + (state.stateFilter === s.id ? " is-active" : ""), "data-ch": s.ch || null,
        "aria-pressed": state.stateFilter === s.id ? "true" : "false",
        onclick: () => { state.stateFilter = s.id; renderList(); renderChips(); },
      }, [s.ch ? el("span", { class: "ch-chip__dot" }) : null, s.label, el("span", { class: "ch-chip__n" }, String(n))]));
    }
  }

  function renderList() {
    const list = dom.list;
    list.textContent = "";
    if (state.loading && !state.people.length) {
      for (let i = 0; i < 5; i++) list.appendChild(el("div", { class: "ch-skel" }));
      return;
    }
    const rows = visiblePeople();
    dom.count.textContent = rows.length === state.people.length ? `${rows.length}` : `${rows.length} / ${state.people.length}`;
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
        list.appendChild(el("div", { class: "ch-group-label" }, PLATFORMS[p.platform] ? PLATFORMS[p.platform].label : p.platform));
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
      const card = el("button", {
        type: "button", class: "ch-card" + (k === state.selected ? " is-active" : ""), role: "option", "aria-selected": k === state.selected ? "true" : "false",
        "data-key": k, "data-ch": postureOf(p),
        onclick: () => select(k),
      }, [
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
    state.editingName = null;
    state.confirmRemove = null;
    state.toolSearch = "";
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
    const editing = state.editingName === pkey(p);
    const nameLine = editing ? nameEditor(p) : el("div", { class: "ch-hero__nameline" }, [
      el("div", { class: "ch-hero__name" }, displayName(p)),
      el("button", { type: "button", class: "ch-iconbtn", title: "Change name", "aria-label": `Change ${displayName(p)}'s name`, onclick: () => { state.editingName = pkey(p); renderDetail(); } }, [icon("edit")]),
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
      { id: "usage", label: "Usage", ico: "chart" },
      { id: "test", label: "Test", ico: "flask", title: "Test as this person" },
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
    if (e.tools === "all") tools = statCard("Tools", "Everything", "Any tool the platform allows.", "limited");
    else if (e.tools === "custom") tools = statCard("Tools", `${e.tool_count} allowed`, "Only the tools you ticked.", "limited");
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
        el("div", null, [el("dt", null, "Name"), el("dd", null, [
          p.name || (p.name_from === "linked" ? p.name_effective + " (from the linked account)" : "— not told yet"),
          p.name && p.name_locked ? el("span", { class: "ch-row__hint", title: "Set by you. What they tell Jarvis in chat won't replace it." }, " · set by you") : null,
        ])]),
        el("div", null, [el("dt", null, "Handle"), el("dd", null, p.handle ? "@" + p.handle : "—")]),
        el("div", null, [el("dt", null, "First seen"), el("dd", null, dateText(p.first_seen))]),
        el("div", null, [el("dt", null, "Last seen"), el("dd", null, dateText(p.last_seen))]),
        el("div", null, [el("dt", null, "Messages"), el("dd", null, String(p.messages))]),
        p.manual ? el("div", null, [el("dt", null, "Added"), el("dd", null, p.messages ? "by you, then they wrote" : "by you — hasn't written yet")]) : null,
        el("div", null, [el("dt", null, "Follow status"), el("dd", null, p.follow)]),
      ]),
    ]));
    pane.appendChild(linkSection(p));
    const rm = removeSection(p);
    pane.appendChild(el("div", null, [
      el("div", { class: "ch-section-title" }, ["What Jarvis remembers", el("span", { class: "ch-section-title__aside" }, `${p.notes.length}`)]),
      p.notes.length ? el("ul", { class: "ch-notes" }, p.notes.map((n) => el("li", null, n)))
        : el("div", { class: "ch-row__hint" }, "Nothing yet. Notes come from the person telling Jarvis about themselves; they stay in this record and never enter your own memory."),
    ]));
    if (rm) pane.appendChild(rm);
    return pane;
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
      if (o.flag === "tool" && !c.off) {
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
      tags: p.tool.on ? [tag(p.tools.mode === "custom" ? "custom list" : "every tool", "limited")] : [],
      extra: p.tool.on || p.tool.via === "wildcard" ? toolScopePanel(p) : null,
    });
    pane.appendChild(el("div", null, [el("div", { class: "ch-section-title" }, "Capabilities"), el("div", { class: "ch-rows" }, [toolsRow])]));

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
    const p = current();
    if (!p) {
      dom.detail.appendChild(el("div", { class: "ch-state" }, [icon("people"), el("div", { class: "ch-state__t" }, state.loading ? "Loading…" : state.people.length ? "Pick someone" : "No one registered yet"),
        el("div", { class: "ch-state__d" }, state.loading ? "" : state.people.length ? "Choose a person on the left to see and change what they can do." : "People show up here the first time they DM or mention Jarvis.")]));
      return;
    }
    dom.detail.appendChild(hero(p));
    const tabBar = tabs(p);
    dom.detail.appendChild(tabBar);
    // Five tabs don't fit a phone: scroll the bar so the active one is visible.
    const activeTab = tabBar.querySelector('.ch-tab[aria-selected="true"]');
    if (activeTab && activeTab.offsetLeft + activeTab.offsetWidth > tabBar.clientWidth) tabBar.scrollLeft = activeTab.offsetLeft - 16;
    const pane = { profile: renderProfile, conv: renderConv, usage: renderUsage, test: renderTest }[state.tab] || renderPerms;
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
    else if (state.tab === "usage") ensureUsage(p);
    else if (state.tab === "test") runSim(p);
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

  function messageNode(e) {
    const mine = e.dir === "in";
    const wrap = el("div", { class: "ch-msg " + (mine ? "ch-msg--in" : "ch-msg--out"), "data-ch": mine && e.allowed === false ? "bad" : null });
    const meta = el("div", { class: "ch-msg__meta" }, [
      el("span", null, mine ? "They wrote" : "Jarvis"),
      clockOf(e.at) ? el("span", { class: "ch-msg__time" }, clockOf(e.at)) : null,
    ]);
    if (mine) {
      meta.appendChild(tag(e.context === "group" ? "group" : "DM", null, null));
      if (e.allowed === false) meta.appendChild(tag("Turned away", "bad", "blocked"));
      else if (e.may_use_tools) meta.appendChild(tag("tools on", "limited", "tool"));
    } else {
      if (e.ok === false) meta.appendChild(tag("Not delivered", "bad", "warn"));
      if (e.provider) meta.appendChild(el("span", { class: "ch-msg__prov" }, e.provider));
    }
    wrap.appendChild(meta);
    // Always text. These are strangers' words and the model's.
    const body = el("div", { class: "ch-msg__text" }, e.text || "");
    if (!e.text) body.classList.add("is-empty");
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
      el("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: s.status === "loading", onclick: () => { s.status = "idle"; ensureConv(p); } }, [icon("refresh"), "Refresh"]),
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

  function renderSide() {
    if (!dom) return;
    dom.side.textContent = "";
    for (const id of Object.keys(PLATFORMS)) {
      const pl = state.platforms[id];
      if (!pl) continue;
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
      dom.side.appendChild(card);
    }
    const g = el("details", { class: "ch-globals" }, [el("summary", null, "Global lists")]);
    const body = el("div", { class: "ch-globals__body" }, [
      el("div", { class: "ch-globals__note" }, "The raw allow-lists the switches on the left read and write. Use these to add someone who hasn't messaged yet, or to manage \"*\" entries."),
    ]);
    for (const id of Object.keys(PLATFORMS)) {
      if (!state.config[id]) continue;
      body.appendChild(el("div", { class: "ch-section-title" }, PLATFORMS[id].label));
      globalSets(id).forEach((n) => body.appendChild(n));
    }
    g.appendChild(body);
    if (state.globalOpen) g.setAttribute("open", "");
    g.addEventListener("toggle", () => { state.globalOpen = g.open; });
    dom.side.appendChild(g);
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

  function renderAll() { if (!dom) return; renderStatusLine(); renderChips(); renderList(); renderDetail(); renderSide(); renderAdd(); }

  // --------------------------------------------------------------- plumbing
  function grabDom() {
    const overlay = $("#channels-overlay");
    if (!overlay) return null;
    return {
      overlay, search: $("#ch-search"), platChips: $("#ch-platform-chips"), stateChips: $("#ch-state-chips"), list: $("#ch-list"),
      count: $("#ch-count"), detail: $("#ch-detail"), side: $("#ch-side"), statusLine: $("#channels-status-line"), refresh: $("#btn-ch-refresh"),
      addBtn: $("#btn-ch-add"), addOverlay: $("#channels-add-overlay"), addBody: $("#ch-add-body"),
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
      if (state.editingName) { state.editingName = null; renderDetail(); e.preventDefault(); return; }
      if (typing && e.target.value) { e.target.value = ""; e.target.dispatchEvent(new Event("input")); return; }
      close(); return;
    }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    const byKey = { 1: "profile", 2: "perms", 3: "conv", 4: "usage", 5: "test" }[e.key];
    if (byKey) { activateTab(byKey); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const rows = visiblePeople();
      if (!rows.length) return;
      e.preventDefault();
      const i = rows.findIndex((p) => pkey(p) === state.selected);
      const next = rows[Math.min(rows.length - 1, Math.max(0, (i < 0 ? 0 : i) + (e.key === "ArrowDown" ? 1 : -1)))];
      select(pkey(next));
      const card = dom.list.querySelector(".ch-card.is-active"); if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
    }
  }

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;
    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderChips(); renderList(); });
    dom.refresh.addEventListener("click", () => {
      state.tools = null; state.toolsState = "idle";
      state.conv.clear(); state.usage.clear(); state.sim.clear();
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
    state.conv.clear(); state.usage.clear(); state.sim.clear();   // don't keep their messages around while it's closed
    state.adding = false; state.editingName = null; state.confirmRemove = null;
    renderAdd();
    dom.overlay.hidden = true;
    const prev = state.prevFocus; state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  // _pure: the logic with no DOM, for tests/verify_channels_panel.js.
  global.JarvisChannels = {
    open, close,
    _pure: { initials, hueFor, relTime, filterPeople, postureOf, sameScope, scopeOf, groupTools, displayName, STATE_FILTERS,
             dayOf, clockOf, localDay, dayLabel, groupByDay, fmtTokens, barModel, convNote },
  };
})(window);
