/* ============================================================================
 * notifications.js — L.30 notification rework (rev. 2026-10-04).
 *
 * Replaces two things that used to live in app.js:
 *
 *   1. THE TOAST. Notifications were shown through the one shared `#toast`
 *      element — the same one every error message uses — so two notifications
 *      in the same tick overwrote each other, and any error toast wiped a
 *      reminder off the screen. Now each notification is drawn in the page's one
 *      toast stack (ui-kit.js JarvisUI.toastStack, E3: shared with the one-line
 *      JarvisUI toasts), in the LOOK its record carries (`record.look`, set by
 *      notifier.resolve_delivery(); the page no longer guesses from the level):
 *        card     its own card: source, time, a coalesced "x3" count for bursts,
 *                 and real actions (Acknowledge, Open chat / schedule);
 *        compact  the small one-line JarvisUI.toast the rest of the interface
 *                 uses (the same one as the Daemons panel's "Autostart on"), in
 *                 the record's `tone` (info / success). Never a failure and never
 *                 anything that needs acknowledging: those are drawn as cards
 *                 whatever the look says, as a safety net behind the notifier;
 *        none     not drawn (the level mutes it). It is still counted, still in
 *                 the inbox and still in the panel.
 *      Compact and card draw into the SAME host; there is no second one.
 *
 *   2. THE PANEL. A flat read-only list with a browser-side unread counter
 *      that reset to zero on every page load. Now it is an inbox backed by
 *      the durable store: read / unread, filters, search, per-item dismiss,
 *      mark-all-read, clear-read, day grouping, burst collapsing, and a
 *      pinned "needs your acknowledgment" section. The badge is the server's
 *      count, not a number kept in the tab.
 *
 * WHAT IT DOES NOT CHANGE
 * -----------------------
 *  - Delivery. Which channel a notification takes (inbox, stream, OS toast,
 *    voice, Playnite, Discord, Instagram) is still notifier.py's decision.
 *  - The level rules (D.2.1). Which levels exist and what each adds is the
 *    notifier's table (notify_registry.PRESETS, read through the catalog); this
 *    file keeps no list of them. A persistent record re-surfaces until
 *    acknowledged (D-N1, owner-decided: every 10 minutes, at most 6 times); a
 *    confirm record gets a blocking Acknowledge dialog.
 *  - app.js's generic toast() for errors and "copied" messages (it hands off to
 *    JarvisUI.toast, which draws in the same stack as these cards).
 *
 * TWO KINDS OF "SEEN" — DON'T CONFLATE THEM
 * -----------------------------------------
 *  `seen_by` (notify-ack) means "this consumer was handed the notification".
 *  `read_at` / `acked_at` (notify-read) means "the OWNER has seen it / dealt
 *  with it". Delivery is not reading; only the second drives the badge, the
 *  Unread filter and the re-surfacing.
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 *  A notification's title and message can come from a job, a daemon, the
 *  clipboard or a chat guest. Every value goes in through textContent / text
 *  nodes, never innerHTML.
 *
 * STRUCTURE
 * ---------
 *   1. constants          4. toast stack
 *   2. pure helpers       5. panel
 *      (JarvisNotifications._pure, tested by tests/verify_notifications.js)
 *   3. server access      6. wiring
 *
 * NO HARD-CODED LISTS
 * -------------------
 *  Levels, notification types (channels / behaviours), kinds and positions all
 *  come from the catalog (`jarvis notify-config --catalog`, served at
 *  /api/notifications/catalog). pickerActions() / pickerLevels() / pickerKinds()
 *  / pickerPositions() are how every form, panel and Settings row reads them.
 *  With no catalog they return an empty list and a message, and the picker
 *  disables itself; the notifications themselves still draw, because each record
 *  carries its own look.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ======================================================================
   * 1. constants
   * ==================================================================== */

  const API = {
    history: "/api/notifications/history",
    summary: "/api/notifications/summary",
    catalog: "/api/notifications/catalog",
    read: "/api/notifications/read",
    dismiss: "/api/notifications/dismiss",
    send: "/api/notifications/send",
  };

  const AUTO_HIDE_MS = 8000;             // a standard (level 2) card
  const AUTO_HIDE_ACK_MS = 12000;        // a card that still needs acknowledging
  const AFTER_HOVER_MS = 3000;           // grace period once the pointer leaves a card
  const MAX_VISIBLE = 4;                 // cards on screen at once; the rest live in the panel
  // E2 / D-11 / D-12: where a toast is drawn. The list of places is the catalog's
  // (notify_registry.POSITIONS); this one constant only keeps a notification drawable
  // when the catalog has not loaded. The record carries `position`; anything else
  // (an older record, a bad value, no catalog) lands here.
  const DEFAULT_POSITION = "bottom-right";
  // The title the notifier gives a notification that has none. A compact toast leaves
  // it out: "Jarvis" above a one-line message says nothing.
  const GENERIC_TITLE = "Jarvis";
  // What a picker says when the catalog could not be read.
  const PICKER_UNAVAILABLE = "The notification options could not be loaded. Reload the page to try again.";
  const BURST_WINDOW_MS = 10 * 60 * 1000;
  // D-N1 (owner-decided 2026-09-26i): level 3+ re-surfaces on an interval
  // until acknowledged. The interval and cap were not owner-specified.
  const RESURFACE_INTERVAL_MS = 10 * 60 * 1000;
  const RESURFACE_MAX = 6;
  const SYNC_DEBOUNCE_MS = 1500;
  const SYNC_POLL_MS = 5 * 60 * 1000;
  const PANEL_LIMIT = 500;               // matches notifier.MAX_INBOX
  const SEARCH_DEBOUNCE_MS = 220;

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  /* ======================================================================
   * 2. pure helpers — no DOM, no network
   * ==================================================================== */

  function parseTime(iso) {
    const t = Date.parse(iso || "");
    return Number.isNaN(t) ? null : t;
  }

  /** Records that should collapse together in a burst. `failed` is part of
   *  the key so a failure is never hidden inside a run of successes. */
  function groupKey(n) {
    return [n.kind || "notify", n.source || "", n.title || "", n.failed ? "!" : ""].join("\u0001");
  }

  function needsAckOf(n) {
    if (!n) return false;
    if (typeof n.needs_ack === "boolean") return n.needs_ack;
    return Boolean((n.persistent || n.confirm_required) && !n.acked_at);
  }

  function isUnreadOf(n) {
    if (!n) return false;
    if (typeof n.unread === "boolean") return n.unread;
    return !n.read_at;
  }

  // E0: the catalog (jarvis notify-config --catalog, served at /api/notifications/
  // catalog): every action, level preset, kind, position, look and tone the notifier
  // knows. null until it loads. There is NO built-in copy of any of it: with no catalog
  // the pickers are empty (and say so), and a notification is drawn from its own record.
  let catalog = null;

  /** Accept a catalog only if it has what the page needs; keep the last good one
   *  otherwise (a failed refresh must not blank the pickers). Returns true if accepted. */
  function setCatalog(next) {
    const ok = next && Array.isArray(next.presets) && next.presets.length
      && Array.isArray(next.positions) && next.positions.length
      && Array.isArray(next.actions) && next.actions.length;
    if (ok) catalog = next;
    else if (next === null) catalog = null;
    return Boolean(ok);
  }

  function preset(level) {
    return catalog ? catalog.presets.find((p) => p.level === level) || null : null;
  }

  function positionIds() {
    return catalog ? catalog.positions.map((p) => p.id) : [];
  }

  function defaultPosition() {
    return catalog && positionIds().includes(catalog.default_position)
      ? catalog.default_position : DEFAULT_POSITION;
  }

  function positionOf(n) {
    const raw = n && typeof n.position === "string" ? n.position : "";
    const p = raw.toLowerCase().replace(/[_\s]+/g, "-").trim();
    return positionIds().includes(p) ? p : defaultPosition();
  }

  /** The record's level as a number, or null when it is not one this catalog knows
   *  (or when there is no catalog: the range is the catalog's). */
  function levelOf(n) {
    const lv = Number(n && n.level);
    if (!Number.isFinite(lv)) return null;
    if (!catalog) return lv;
    return lv >= catalog.min_level && lv <= catalog.max_level ? lv : null;
  }

  function levelBadge(n) {
    const lv = levelOf(n);
    if (!catalog || lv === null) return "";
    const p = preset(lv);
    return p && lv !== catalog.default_level ? p.id : "";
  }

  /** The action id that draws a look (the card's, the small toast's), from the catalog. */
  function lookAction(lookId) {
    const row = catalog && Array.isArray(catalog.looks)
      ? catalog.looks.find((l) => l.id === lookId) : null;
    return row ? row.action : null;
  }

  /** How a record is drawn: "card", "compact" or "none".
   *
   *  The record says (`look`, set by notifier.resolve_delivery()). Two things sit on top:
   *   - a record from before the upgrade has no look: derive it once from the catalog (a
   *     card when its level's preset delivers the card action, else nothing); with no
   *     catalog, a card, which is what the page always drew;
   *   - the safety net: a compact record that is a failure, persistent or needs a
   *     confirmation is a card whatever it says. The notifier already does this; this
   *     is the second lock. It never turns "none" into a card, because "none" is how a
   *     level mutes a notification. */
  function lookOf(n) {
    const raw = n && typeof n.look === "string" ? n.look.trim().toLowerCase() : "";
    let look = raw;
    if (!look) {
      const cardAction = lookAction("card");
      const p = catalog ? preset(levelOf(n)) : null;
      look = !catalog ? "card" : (p && cardAction && p.channels.includes(cardAction) ? "card" : "none");
    }
    if (look !== "card" && look !== "compact" && look !== "none") look = "card";
    if (look === "compact" && n && (n.failed || n.persistent || n.confirm_required || needsAckOf(n))) {
      look = "card";
    }
    return look;
  }

  /** Does this record need the blocking acknowledge dialog? The record's own flag; for a
   *  record that predates the flag, the catalog's preset for its level. */
  function needsConfirmOf(n) {
    if (!n) return false;
    if (n.confirm_required === true) return true;
    if (typeof n.confirm_required === "boolean") return false;
    const p = catalog ? preset(levelOf(n)) : null;
    return Boolean(p && p.flags && p.flags.confirm_required);
  }

  /** The compact toast's tone (its mark and colour), as JarvisUI.toast's `level`. The record
   *  carries it; an unknown one is left undefined so JarvisUI.toast uses its own default. */
  function toneOf(n) {
    const t = n && typeof n.tone === "string" ? n.tone : "";
    if (catalog && Array.isArray(catalog.tones)) {
      if (catalog.tones.includes(t)) return t;
      return catalog.default_tone || undefined;
    }
    return t || undefined;
  }

  /* ---- what the pickers read -------------------------------------------------
   * Every place that lets the owner tick a notification type, pick a level, a kind
   * or a position builds its rows from these, so adding one is a row in the
   * notification module and nothing else. Each returns { items, message }: `items`
   * is empty and `message` says why when there is no catalog (the picker disables
   * itself); `message` is "" otherwise. */

  function pickerResult(items) {
    return catalog ? { items, message: "" } : { items: [], message: PICKER_UNAVAILABLE };
  }

  function pickerActions() {
    return pickerResult((catalog ? catalog.actions : []).map((a) => ({
      id: a.id, label: a.label || a.id, type: a.type, description: a.description || "",
      dm: Boolean(a.dm), page_ok: Boolean(a.page_ok), look: a.look || null,
      always: Boolean(a.always),
    })));
  }

  function pickerLevels() {
    return pickerResult((catalog ? catalog.presets : []).map((p) => ({
      level: p.level, id: p.id, name: p.name, label: `${p.level} \u00b7 ${p.name}`,
      description: p.description || "", help: p.help || "", isDefault: p.level === catalog.default_level,
    })));
  }

  function pickerKinds() {
    return pickerResult((catalog ? catalog.kinds : []).map((k) => ({
      id: k.id, label: k.label || k.id, level: k.level, settings: k.settings !== false,
    })));
  }

  function pickerPositions() {
    return pickerResult((catalog ? catalog.positions : []).map((p) => ({
      id: p.id, label: p.label || p.id, menu: p.menu,
    })));
  }

  /** The one-line note under a picker that offers both on-page looks: ticking both draws
   *  the card once. Built from the catalog's own labels; "" when there are fewer than two. */
  function pickerHint() {
    const card = lookAction("card");
    const compact = lookAction("compact");
    const rows = catalog ? catalog.actions : [];
    const a = rows.find((x) => x.id === card);
    const b = rows.find((x) => x.id === compact);
    return a && b ? `Tick both "${a.label}" and "${b.label}" and the ${a.label.toLowerCase()} is shown (never twice).` : "";
  }

  function relTime(iso, now) {
    const t = parseTime(iso);
    if (t === null) return "";
    const s = Math.max(0, Math.round(((now === undefined ? Date.now() : now) - t) / 1000));
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  function clockTime(iso) {
    return typeof iso === "string" && iso.length >= 16 ? iso.slice(11, 16) : "";
  }

  function fullTime(iso) {
    return typeof iso === "string" ? iso.replace("T", " ").slice(0, 19) : "";
  }

  function dayLabel(iso, now) {
    const t = parseTime(iso);
    if (t === null) return "Earlier";
    const d = new Date(t);
    const n = new Date(now === undefined ? Date.now() : now);
    const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((startOf(n) - startOf(d)) / 86400000);
    if (diff <= 0) return "Today";
    if (diff === 1) return "Yesterday";
    const base = `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
    return d.getFullYear() === n.getFullYear() ? base : `${base} ${d.getFullYear()}`;
  }

  /** Collapse runs of the same notification (newest-first input) into one
   *  group each. Two records join a run only when they share a groupKey AND
   *  are within `windowMs` of each other, so a reminder that fires daily is
   *  never folded into yesterday's. */
  function collapseBursts(items, windowMs) {
    const win = windowMs === undefined ? BURST_WINDOW_MS : windowMs;
    const groups = [];
    for (const item of items || []) {
      const key = groupKey(item);
      const last = groups[groups.length - 1];
      if (last && last.key === key) {
        const prev = parseTime(last.items[last.items.length - 1].created_at);
        const cur = parseTime(item.created_at);
        if (prev !== null && cur !== null && prev - cur <= win) {
          last.items.push(item);
          continue;
        }
      }
      groups.push({ key, items: [item] });
    }
    return groups.map((g) => ({
      key: g.key,
      head: g.items[0],
      items: g.items,
      count: g.items.length,
      ids: g.items.map((i) => i.id).filter(Boolean),
      unreadIds: g.items.filter(isUnreadOf).map((i) => i.id).filter(Boolean),
    }));
  }

  /** Split collapsed groups into day sections, preserving order. */
  function sectionByDay(groups, now) {
    const sections = [];
    for (const g of groups) {
      const label = dayLabel(g.head.created_at, now);
      const last = sections[sections.length - 1];
      if (last && last.label === label) last.groups.push(g);
      else sections.push({ label, groups: [g] });
    }
    return sections;
  }

  /** The query string the history route understands, from the filter state. */
  function historyQuery(ui, limit) {
    const parts = [`limit=${limit === undefined ? PANEL_LIMIT : limit}`];
    if (ui.unread) parts.push("unread=1");
    if (ui.failed) parts.push("failed=1");
    if (ui.ack) parts.push("needs_ack=1");
    if (ui.kind) parts.push(`kind=${encodeURIComponent(ui.kind)}`);
    if (ui.q && ui.q.trim()) parts.push(`q=${encodeURIComponent(ui.q.trim())}`);
    return parts.join("&");
  }

  function filtersActive(ui) {
    return Boolean(ui.unread || ui.failed || ui.ack || ui.kind || (ui.q && ui.q.trim()));
  }

  /** Short body for a card / row: the pre-cleaned summary, never raw output. */
  function bodyOf(n, max) {
    const text = String((n && (n.summary || n.message)) || "").replace(/\s+\n/g, "\n").trim();
    return text.length > max ? text.slice(0, max - 1).trimEnd() + "\u2026" : text;
  }

  function statusLine(data, ui) {
    const total = data.total | 0;
    const unread = data.unread | 0;
    if (!total) return "no notifications";
    const base = unread ? `${unread} unread \u00b7 ${total} total` : `all read \u00b7 ${total} total`;
    if (filtersActive(ui)) return `${data.shown | 0} shown \u00b7 ${base}`;
    return base;
  }

  /** E3: what a browser (OS) notification raised by this page looks like, with
   *  the limits applied. A notifier record (`osNotify`) and a line the page
   *  wrote itself (`osNotice`, e.g. "your task is done") both build theirs
   *  here, so the title / body limits live in one place. */
  function osNoticeSpec(o) {
    const x = o || {};
    return {
      title: String(x.title || "Jarvis").slice(0, 88),
      body: String(x.body || "").slice(0, 160),
      tag: String(x.tag || "jarvis-notice"),
      silent: x.silent === true,
    };
  }

  const PURE = {
    parseTime, groupKey, needsAckOf, isUnreadOf, levelOf, levelBadge, relTime, clockTime,
    fullTime, dayLabel, collapseBursts, sectionByDay, historyQuery, filtersActive, bodyOf,
    statusLine, positionOf, setCatalog, lookOf, needsConfirmOf, toneOf, osNoticeSpec,
    pickerActions, pickerLevels, pickerKinds, pickerPositions, pickerHint,
    constants: {
      AUTO_HIDE_MS, AUTO_HIDE_ACK_MS, MAX_VISIBLE, BURST_WINDOW_MS, RESURFACE_INTERVAL_MS,
      RESURFACE_MAX, PANEL_LIMIT, DEFAULT_POSITION, GENERIC_TITLE, PICKER_UNAVAILABLE,
    },
  };

  // Everything below needs a browser. A bare context (the Node test) gets the
  // pure helpers and nothing else.
  if (typeof document === "undefined") {
    global.JarvisNotifications = { _pure: PURE };
    return;
  }

  /* ======================================================================
   * 3. server access + shared state
   * ==================================================================== */

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || `${method} ${url} failed (${res.status})`);
    return data;
  }

  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of [].concat(children === undefined ? [] : children)) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function hostToast(message, level) {
    const UI = global.JarvisUI;
    if (UI && UI.toast) UI.toast({ message, level: level || "info" });
    else console.error(message);
  }

  /** Fetch the catalog and keep it. Never throws: on a failure the last good copy stays
   *  (or none, and the pickers say so). Fires `jarvis:notify-catalog` on the first load
   *  and whenever its `version` (a hash of the notification table) differs from the copy
   *  held, so every open picker redraws; a refetch that changes nothing is silent. Forms
   *  and Settings call this each time they open, so a changed table shows up without a
   *  page reload and with no timer (every fetch starts a CLI process on the server).
   *  Resolves to the catalog (or null). */
  async function loadCatalog() {
    const before = catalog ? catalog.version || "" : null;
    try {
      const data = await api("GET", API.catalog);
      if (setCatalog(data) && (before === null || (catalog.version || "") !== before)) {
        global.dispatchEvent(new CustomEvent("jarvis:notify-catalog", { detail: catalog }));
      }
    } catch { /* keep the last good copy */ }
    return catalog;
  }

  const counts = { unread: 0, failedUnread: 0, total: 0 };
  const seenIds = new Set();   // ids already ingested this page-load (the stream and the push can both deliver one)

  const fab = () => document.getElementById("btn-notifications-fab");
  const badge = () => document.getElementById("notifications-fab-badge");

  function updateBadge() {
    const el = badge();
    if (!el) return;
    el.hidden = counts.unread <= 0;
    el.textContent = counts.unread > 99 ? "99+" : String(Math.max(0, counts.unread));
    el.classList.toggle("is-failed", counts.failedUnread > 0);
    const f = fab();
    if (f) {
      f.title = counts.unread
        ? `Notifications \u2014 ${counts.unread} unread`
        : "Notifications \u2014 everything Jarvis has sent";
    }
  }

  let syncTimer = null;
  function scheduleSync() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => { syncSummary(false); }, SYNC_DEBOUNCE_MS);
  }

  /** Pull the durable counts. With `surface`, also re-show anything still
   *  awaiting acknowledgment — this is what makes a persistent notification
   *  survive a page reload instead of vanishing with the tab. */
  async function syncSummary(surface) {
    let s;
    try {
      s = await api("GET", API.summary);
    } catch {
      return null;   // the server / CLI isn't ready; the next sync will catch up
    }
    if (!s || typeof s !== "object") return null;
    counts.unread = s.unread | 0;
    counts.failedUnread = s.failed_unread | 0;
    counts.total = s.total | 0;
    updateBadge();
    if (surface) {
      for (const n of (s.needs_ack || [])) {
        if (!n.id || seenIds.has(n.id)) continue;
        seenIds.add(n.id);
        toastFor(n);
        trackResurface(n);
      }
    }
    return s;
  }

  /** The owner read / acknowledged these. */
  async function markRead(ids) {
    const list = (ids || []).filter(Boolean);
    if (!list.length) return 0;
    stopResurface(list);
    try {
      const out = await api("POST", API.read, { ids: list });
      scheduleSync();
      return (out && out.read) | 0;
    } catch (err) {
      hostToast(`Couldn't mark as read: ${err.message}`, "error");
      scheduleSync();
      return 0;
    }
  }

  /* ======================================================================
   * 4. toast stack
   * ==================================================================== */

  const cards = new Map();        // groupKey -> card record
  const resurface = new Map();    // notification id -> { n, timer }

  /** E3: the cards are drawn into the page's one toast host, the per-position
   *  stack that ui-kit.js owns (JarvisUI.toastStack), so they stack with the
   *  one-line JarvisUI toasts instead of beside them. Returns null when ui-kit.js
   *  did not load: a card is then not drawn, but the notification is already in
   *  the inbox (the badge and the panel still show it). */
  function ensureStack(position) {
    const pos = positionIds().includes(position) ? position : defaultPosition();
    const UI = global.JarvisUI;
    return UI && typeof UI.toastStack === "function" ? UI.toastStack(pos) : null;
  }

  /** The compact look: the page's ordinary one-line JarvisUI.toast, so it is the same
   *  toast as "Autostart on" in the Daemons panel and stacks with the cards in the same
   *  corner. Not a new element and not a new host. The record's tone picks the mark and
   *  colour; the title is shown only when the notification has one of its own. Repeats
   *  simply stack (no id is passed: JarvisUI.toast keeps one map entry per id, so a
   *  repeated id would let the first node's timer remove the second). */
  function toastCompact(n) {
    const UI = global.JarvisUI;
    if (!UI || typeof UI.toast !== "function") return;
    const title = n.title && String(n.title).trim() !== GENERIC_TITLE ? String(n.title) : undefined;
    UI.toast({ title, message: bodyOf(n, 160), level: toneOf(n), position: positionOf(n) });
  }

  function targetOf(n) {
    if (n.conv_id) return { label: "Open chat", run: () => openChat(n.conv_id) };
    if (n.job_id) return { label: "Open schedule", run: () => openSchedule() };
    return null;
  }

  function openChat(id) {
    if (global.JarvisAsk && global.JarvisAsk.openConversation) {
      closePanel();
      global.JarvisAsk.openConversation(id);
    }
  }

  function openSchedule() {
    if (global.JarvisSchedules && global.JarvisSchedules.open) {
      closePanel();
      global.JarvisSchedules.open();
    }
  }

  function paintCard(card) {
    const n = card.latest;
    const ack = card.needsAck;
    card.node.className = "ntf" + (n.failed ? " ntf--failed" : "") + (ack ? " ntf--ack" : "");
    card.node.setAttribute("role", n.failed || ack ? "alert" : "status");
    card.kindEl.textContent = String(n.source || n.kind || "notify");
    card.countEl.textContent = card.ids.length > 1 ? `\u00d7${card.ids.length}` : "";
    card.timeEl.textContent = clockTime(n.created_at) || relTime(n.created_at);
    card.titleEl.textContent = String(n.title || "Jarvis").slice(0, 120);
    const body = bodyOf(n, 220);
    card.bodyEl.textContent = body;
    card.bodyEl.hidden = !body;

    card.actionsEl.textContent = "";
    if (ack) {
      card.actionsEl.appendChild(h("button", {
        type: "button", class: "ntf__btn ntf__btn--primary",
        onclick: (ev) => { ev.stopPropagation(); acknowledgeCard(card); },
      }, "Acknowledge"));
    }
    const target = targetOf(n);
    if (target) {
      card.actionsEl.appendChild(h("button", {
        type: "button", class: "ntf__btn",
        onclick: (ev) => { ev.stopPropagation(); target.run(); removeCard(card); },
      }, target.label));
    }
    card.actionsEl.hidden = !card.actionsEl.childNodes.length;
  }

  function removeCard(card) {
    clearTimeout(card.timer);
    cards.delete(card.key);
    card.node.classList.remove("is-in");
    setTimeout(() => card.node.remove(), 180);
  }

  function acknowledgeCard(card) {
    const ids = card.ids.slice();
    removeCard(card);
    markRead(ids);
  }

  function armHide(card, ms) {
    clearTimeout(card.timer);
    card.timer = setTimeout(() => removeCard(card), ms);
  }

  function enforceCap() {
    while (cards.size > MAX_VISIBLE) {
      // Drop the oldest card, preferring one that needs no acknowledgment —
      // an unacknowledged one is also pinned in the panel and re-surfaces.
      let victim = null;
      for (const card of cards.values()) {
        if (!card.needsAck) { victim = card; break; }
      }
      if (!victim) victim = cards.values().next().value;
      removeCard(victim);
    }
  }

  /** Show (or merge into) the card for one notification. */
  function toastFor(n) {
    const host = ensureStack(positionOf(n));
    if (!host) return null;                // no ui-kit.js: nowhere to draw (the inbox has it)
    const key = groupKey(n);
    const ack = needsAckOf(n);
    let card = cards.get(key);
    if (card) {
      if (n.id && !card.ids.includes(n.id)) card.ids.push(n.id);
      card.latest = n;
      card.needsAck = card.needsAck || ack;
      paintCard(card);
      card.node.classList.remove("is-bump");
      void card.node.offsetWidth;            // restart the bump animation
      card.node.classList.add("is-bump");
      armHide(card, card.needsAck ? AUTO_HIDE_ACK_MS : AUTO_HIDE_MS);
      return card;
    }
    card = {
      key, ids: n.id ? [n.id] : [], latest: n, needsAck: ack, timer: null,
      kindEl: h("span", { class: "ntf__kind" }),
      countEl: h("span", { class: "ntf__count" }),
      timeEl: h("span", { class: "ntf__time" }),
      titleEl: h("div", { class: "ntf__title" }),
      bodyEl: h("div", { class: "ntf__body" }),
      actionsEl: h("div", { class: "ntf__actions" }),
    };
    const closeBtn = h("button", {
      type: "button", class: "ntf__x", "aria-label": "Dismiss this notification",
      title: "Hide (it stays in the Notifications panel)",
      onclick: (ev) => { ev.stopPropagation(); removeCard(card); },
    }, "\u00d7");
    card.node = h("div", { class: "ntf", tabindex: "0" }, [
      h("div", { class: "ntf__top" }, [card.kindEl, card.countEl, card.timeEl, closeBtn]),
      card.titleEl, card.bodyEl, card.actionsEl,
    ]);
    // The card body opens the panel at this notification; the buttons stop
    // propagation so they don't also do that.
    card.node.addEventListener("click", () => {
      const id = card.latest && card.latest.id;
      removeCard(card);
      openPanel({ focusId: id });
    });
    card.node.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); card.node.click(); }
      else if (ev.key === "Escape") { ev.preventDefault(); removeCard(card); }
    });
    card.node.addEventListener("mouseenter", () => clearTimeout(card.timer));
    card.node.addEventListener("mouseleave", () => armHide(card, AFTER_HOVER_MS));
    card.node.addEventListener("focusin", () => clearTimeout(card.timer));
    paintCard(card);
    cards.set(key, card);
    host.appendChild(card.node);
    requestAnimationFrame(() => card.node.classList.add("is-in"));
    armHide(card, card.needsAck ? AUTO_HIDE_ACK_MS : AUTO_HIDE_MS);
    enforceCap();
    return card;
  }

  /* --- D-N1: re-surface until acknowledged --------------------------------
   * Per notification (the old code had ONE timer for the whole page, so a
   * second persistent notification cancelled the first one's re-surfacing),
   * and it re-checks the durable state each time, so acknowledging in another
   * tab — or in the panel — stops it here too. */
  function trackResurface(n) {
    if (!n || !n.id || !needsAckOf(n) || resurface.has(n.id)) return;
    const entry = { n: 1, timer: null };
    entry.timer = setInterval(async () => {
      entry.n += 1;
      if (entry.n > RESURFACE_MAX) { stopResurface([n.id]); return; }
      const s = await syncSummary(false);
      const still = s && (s.needs_ack || []).find((x) => x.id === n.id);
      if (!still) { stopResurface([n.id]); return; }
      toastFor(still);
    }, RESURFACE_INTERVAL_MS);
    resurface.set(n.id, entry);
  }

  function stopResurface(ids) {
    for (const id of ids || []) {
      const entry = resurface.get(id);
      if (!entry) continue;
      clearInterval(entry.timer);
      resurface.delete(id);
    }
  }

  /* --- level 5: a blocking Acknowledge dialog ------------------------------
   * One at a time, and never on top of another dialog. ui-kit's showModal()
   * closes whatever modal is already open (resolving it "no"), so the old
   * direct call could silently dismiss a pending approval. Wait for the
   * screen to be free instead. */
  const confirmQueue = [];
  let confirming = false;

  function modalOpen() {
    return Boolean(document.querySelector(".jui-modal"));
  }

  async function pumpConfirms() {
    if (confirming) return;
    confirming = true;
    try {
      while (confirmQueue.length) {
        const n = confirmQueue.shift();
        if (!needsAckOf(n) && n.acked_at) continue;
        while (modalOpen()) await new Promise((r) => setTimeout(r, 1000));
        const UI = global.JarvisUI;
        if (!UI || typeof UI.confirm !== "function") continue;
        let ok = false;
        try {
          ok = await UI.confirm({
            title: String(n.title || "Jarvis").slice(0, 120),
            body: bodyOf(n, 600) || "Acknowledge this notification.",
            confirmLabel: "Acknowledge",
            cancelLabel: "Later",
          });
        } catch { ok = false; }
        // The old code threw this answer away. Now "Acknowledge" is recorded
        // (the notification stops re-surfacing); "Later" leaves it pending.
        if (ok) {
          const card = cards.get(groupKey(n));
          if (card) removeCard(card);
          await markRead([n.id]);
        }
      }
    } finally {
      confirming = false;
    }
  }

  /** The ONE place this page builds a browser notification object (E3 / E5; the static
   *  test tests/test_one_notification_system.py keeps it that way). Raised only
   *  when the tab is hidden and permission is granted. Returns whether one was
   *  raised. `onclick` runs after the window is focused and the notice closed. */
  function raiseOs(spec, onclick) {
    if (document.visibilityState === "visible") return false;
    if (!("Notification" in global) || global.Notification.permission !== "granted") return false;
    try {
      const os = new global.Notification(spec.title, {
        body: spec.body, tag: spec.tag, silent: spec.silent,
      });
      os.onclick = () => { global.focus(); os.close(); if (typeof onclick === "function") onclick(); };
      return true;
    } catch { return false; /* private mode / unsupported */ }
  }

  /** A notifier record, raised as a browser notification while the tab is hidden. */
  function osNotify(n) {
    // Tagged per id so two reminders in one tick don't collapse into one.
    raiseOs(osNoticeSpec({ title: n.title, body: bodyOf(n, 160), tag: `jarvis-note-${n.id || Date.now()}` }),
            () => openPanel({ focusId: n.id }));
  }

  /** A line the page wrote itself ("your task is done"), raised as a browser
   *  notification while the tab is hidden. Deliberately NOT a notifier record:
   *  every finished chat reply would otherwise land in the inbox. Takes
   *  { title, body, tag?, silent?, onclick? }. */
  function osNotice(o) {
    return raiseOs(osNoticeSpec(o), o && o.onclick);
  }

  /** Raise a notification FROM the page, e.g. a tool panel's one-line answer to a click
   *  ("Demo downloads started"). It is handed to the notifier like any other (the server's
   *  /api/notifications/send runs `jarvis notify-send`), so it is an inbox record with a kind,
   *  a source, a level and a position, and it comes back to this page through the same push as
   *  every other notification: show() draws it, once. Nothing is drawn here, which is the
   *  point: there is no second toast path to keep in step. Takes
   *  { message, title?, kind?, source?, level?, failed?, channels?, tone? }; `channels` defaults to
   *  ["stream"] (the notification card + the inbox); pass ["small_toast"] for the one-line
   *  toast (and `tone`, "info" or "success", for its mark). Either way a click never raises a
   *  desktop or phone notification. Resolves to the notifier's record, or null when it could not be sent. */
  async function send(o) {
    const x = o || {};
    const message = String(x.message == null ? "" : x.message).trim();
    if (!message) return null;
    const body = { message, title: x.title ? String(x.title) : undefined };
    if (x.kind) body.kind = String(x.kind);
    if (x.source) body.source = String(x.source);
    if (Number.isInteger(x.level)) body.level = x.level;
    if (x.failed) body.failed = true;
    if (x.tone) body.tone = String(x.tone);
    body.channels = Array.isArray(x.channels) && x.channels.length ? x.channels : ["stream"];
    try {
      return await api("POST", API.send, body);
    } catch {
      return null;
    }
  }

  /** Ask the browser for notification permission once (a no-op after that). */
  function requestPermission() {
    if (!("Notification" in global)) return;
    if (global.Notification.permission !== "default") return;
    try {
      const asked = global.Notification.requestPermission();
      if (asked && typeof asked.catch === "function") asked.catch(() => {});
    } catch { /* an older browser that wants a callback: nothing to do */ }
  }

  /** The single entry point app.js calls for every notification, whichever
   *  path it arrived by (the live stderr stream during an ask, or the
   *  server's push of the durable inbox). The same one can arrive by both. */
  function show(n) {
    if (!n || (!n.title && !n.message)) return;
    if (n.id) {
      if (seenIds.has(n.id)) return;
      if (seenIds.size > 1000) seenIds.clear();
      seenIds.add(n.id);
    }
    // Optimistic bump; scheduleSync() replaces it with the durable count.
    counts.unread += 1;
    counts.total += 1;
    if (n.failed) counts.failedUnread += 1;
    updateBadge();
    scheduleSync();

    // Draw it the way the record says (see lookOf). A hidden tab still gets the browser
    // notification for a card; a small toast is "small and quiet", so it does not.
    const look = lookOf(n);
    if (look === "card") {
      toastFor(n);
      osNotify(n);
    } else if (look === "compact") {
      toastCompact(n);
    }
    if (needsAckOf(n)) trackResurface(n);
    if (needsConfirmOf(n)) {
      confirmQueue.push(n);
      pumpConfirms();
    }
    if (panelOpen()) scheduleRefresh();
  }

  /* ======================================================================
   * 5. panel
   * ==================================================================== */

  const ui = { unread: false, failed: false, ack: false, kind: "", q: "" };
  let data = { notifications: [], needs_ack: [], kinds: {}, total: 0, unread: 0, shown: 0 };
  const openGroups = new Set();    // head ids whose burst is expanded
  let focusId = null;
  let loadToken = 0;
  let refreshTimer = null;
  let searchTimer = null;
  let panelBuilt = false;

  const overlay = () => document.getElementById("notifications-overlay");
  const listEl = () => document.getElementById("notifications-list");
  const statusEl = () => document.getElementById("notifications-status-line");

  function panelOpen() {
    const o = overlay();
    return Boolean(o && !o.hidden);
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { loadPanel(); }, 400);
  }

  async function loadPanel() {
    const list = listEl();
    if (!list) return;
    const token = ++loadToken;
    let res;
    try {
      res = await api("GET", `${API.history}?${historyQuery(ui)}`);
    } catch (err) {
      if (token !== loadToken) return;
      const st = statusEl();
      if (st) { st.textContent = "couldn't read notifications"; st.className = "menu-panel__subtitle is-error"; }
      list.textContent = "";
      list.appendChild(h("div", { class: "nt-empty" }, [
        h("div", { class: "nt-empty__title" }, "Couldn't read the notification inbox"),
        h("div", { class: "nt-empty__text" }, err.message || "Failed."),
        h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: loadPanel }, "Try again"),
      ]));
      return;
    }
    if (token !== loadToken) return;   // a newer request is already in flight
    const items = Array.isArray(res.notifications) ? res.notifications : [];
    data = {
      notifications: items,
      needs_ack: Array.isArray(res.needs_ack) ? res.needs_ack : [],
      kinds: res.kinds || {},
      total: res.total | 0,
      unread: res.unread | 0,
      shown: items.length,
    };
    counts.unread = data.unread;
    counts.failedUnread = res.failed_unread | 0;
    counts.total = data.total;
    updateBadge();
    renderPanel();
  }

  function chip(label, active, onclick, title) {
    return h("button", {
      type: "button", class: "nt-chip" + (active ? " is-on" : ""),
      "aria-pressed": active ? "true" : "false", title: title || null, onclick,
    }, label);
  }

  function renderToolbar() {
    const chips = document.getElementById("notif-chips");
    if (chips) {
      chips.textContent = "";
      const none = !ui.unread && !ui.failed && !ui.ack;
      chips.appendChild(chip("All", none, () => { ui.unread = ui.failed = ui.ack = false; loadPanel(); }));
      chips.appendChild(chip(data.unread ? `Unread ${data.unread}` : "Unread", ui.unread,
        () => { ui.unread = !ui.unread; loadPanel(); }, "Only what you haven't read yet"));
      chips.appendChild(chip("Failed", ui.failed,
        () => { ui.failed = !ui.failed; loadPanel(); }, "Only failed jobs and tasks"));
      const awaiting = data.needs_ack.length;
      chips.appendChild(chip(awaiting ? `Needs ack ${awaiting}` : "Needs ack", ui.ack,
        () => { ui.ack = !ui.ack; loadPanel(); }, "Persistent / confirm notifications you haven't acknowledged"));
    }
    const sel = document.getElementById("notif-kind");
    if (sel) {
      const kinds = Object.keys(data.kinds).sort();
      // Keep a filter that no longer has any records selectable, so the
      // dropdown can't silently disagree with what's being filtered.
      if (ui.kind && !kinds.includes(ui.kind)) kinds.push(ui.kind);
      sel.textContent = "";
      sel.appendChild(h("option", { value: "" }, "All kinds"));
      for (const k of kinds) {
        sel.appendChild(h("option", { value: k, selected: k === ui.kind }, `${k} (${data.kinds[k] | 0})`));
      }
      sel.value = ui.kind;
    }
    const markAll = document.getElementById("btn-notifications-markall");
    if (markAll) markAll.disabled = !(data.unread > 0);
    const clearRead = document.getElementById("btn-notifications-clearread");
    if (clearRead) clearRead.disabled = !((data.total - data.unread) > 0);
  }

  function actionBtn(label, onclick, cls, title) {
    return h("button", {
      type: "button", class: "nt-btn" + (cls ? " " + cls : ""), title: title || null,
      onclick: (ev) => { ev.stopPropagation(); onclick(ev); },
    }, label);
  }

  function renderGroup(g, opts) {
    const n = g.head;
    const unread = g.unreadIds.length > 0;
    const ack = needsAckOf(n) || (opts && opts.pinned);
    const expandedBurst = openGroups.has(n.id);
    const bodyText = bodyOf(n, 600);

    let fullShown = false;
    const bodyEl = h("div", { class: "nt-body" }, bodyText);
    bodyEl.hidden = !bodyText;
    const hasMore = Boolean(n.summary_truncated) && n.message && n.message !== n.summary;
    const moreBtn = hasMore ? h("button", {
      type: "button", class: "nt-more",
      onclick: (ev) => {
        ev.stopPropagation();
        fullShown = !fullShown;
        bodyEl.textContent = fullShown ? String(n.message) : bodyText;
        moreBtn.textContent = fullShown ? "Show less" : "Show more";
      },
    }, "Show more") : null;

    const badges = [h("span", { class: "nt-badge" }, String(n.kind || "notify"))];
    if (n.source) badges.push(h("span", { class: "nt-badge nt-badge--src" }, String(n.source)));
    const lv = levelBadge(n);
    if (lv) badges.push(h("span", { class: "nt-badge nt-badge--lv" }, lv));
    if (n.failed) badges.push(h("span", { class: "nt-badge nt-badge--fail" }, "failed"));
    if (ack) badges.push(h("span", { class: "nt-badge nt-badge--ack" }, "needs acknowledgment"));

    const actions = [];
    if (ack) {
      actions.push(actionBtn("Acknowledge", async () => { await act(() => markRead(g.ids)); }, "nt-btn--primary",
        "Record that you've dealt with this; it stops re-surfacing"));
    } else if (unread) {
      actions.push(actionBtn("Mark read", async () => { await act(() => markRead(g.unreadIds)); }));
    }
    const target = targetOf(n);
    if (target) actions.push(actionBtn(target.label, target.run));
    actions.push(actionBtn(g.count > 1 ? `Dismiss ${g.count}` : "Dismiss", async () => {
      await act(() => dismissIds(g.ids));
    }, "nt-btn--quiet", "Delete from the inbox for good"));

    const row = h("div", {
      class: "nt-row" + (unread ? " is-unread" : "") + (n.failed ? " is-failed" : "")
        + (ack ? " is-ack" : "") + (focusId && g.ids.includes(focusId) ? " is-focus" : ""),
      tabindex: "0", "data-id": n.id || "", "data-ids": g.ids.join(","),
      "aria-label": `${unread ? "Unread. " : ""}${n.title || "Jarvis"}`,
    }, [
      h("span", { class: "nt-dot", "aria-hidden": "true" }),
      h("div", { class: "nt-main" }, [
        h("div", { class: "nt-top" }, [
          h("span", { class: "nt-title" }, String(n.title || "Jarvis")),
          g.count > 1 ? h("button", {
            type: "button", class: "nt-count", "aria-expanded": expandedBurst ? "true" : "false",
            title: expandedBurst ? "Hide the individual notifications" : "Show every one in this run",
            onclick: (ev) => {
              ev.stopPropagation();
              if (openGroups.has(n.id)) openGroups.delete(n.id); else openGroups.add(n.id);
              renderPanel();
            },
          }, `\u00d7${g.count}`) : null,
          h("span", { class: "nt-time", title: fullTime(n.created_at) }, relTime(n.created_at)),
        ]),
        h("div", { class: "nt-badges" }, badges),
        bodyEl, moreBtn,
        expandedBurst ? h("div", { class: "nt-burst" }, g.items.map((it) => h("div", { class: "nt-burst__row" }, [
          h("span", { class: "nt-burst__time", title: fullTime(it.created_at) }, clockTime(it.created_at) || relTime(it.created_at)),
          h("span", { class: "nt-burst__text" }, bodyOf(it, 140) || String(it.title || "")),
        ]))) : null,
        h("div", { class: "nt-actions" }, actions),
      ]),
    ]);
    // Opening a row is reading it; the buttons stop propagation so using one
    // of them doesn't also trigger this.
    row.addEventListener("click", () => {
      if (g.unreadIds.length && !ack) act(() => markRead(g.unreadIds));
    });
    return row;
  }

  function renderPanel() {
    const list = listEl();
    if (!list) return;
    renderToolbar();
    const st = statusEl();
    if (st) { st.textContent = statusLine(data, ui); st.className = "menu-panel__subtitle"; }

    // Keep the scroll position and the focused row across a re-render.
    const scroll = list.scrollTop;
    const activeId = document.activeElement && document.activeElement.getAttribute
      ? document.activeElement.getAttribute("data-id") : null;
    list.textContent = "";

    const filtering = filtersActive(ui);
    const pinned = (!filtering && data.needs_ack.length) ? data.needs_ack : [];
    const pinnedIds = new Set(pinned.map((p) => p.id));
    const rest = data.notifications.filter((i) => !pinnedIds.has(i.id));

    if (!data.notifications.length && !pinned.length) {
      list.appendChild(emptyState(filtering));
      return;
    }

    if (pinned.length) {
      list.appendChild(h("div", { class: "nt-section nt-section--ack" },
        `Needs your acknowledgment \u00b7 ${pinned.length}`));
      for (const g of collapseBursts(pinned, 0)) list.appendChild(renderGroup(g, { pinned: true }));
    }
    const now = Date.now();
    for (const sec of sectionByDay(collapseBursts(rest, BURST_WINDOW_MS), now)) {
      list.appendChild(h("div", { class: "nt-section" }, sec.label));
      for (const g of sec.groups) list.appendChild(renderGroup(g));
    }

    list.scrollTop = scroll;
    if (focusId) {
      const target = list.querySelector(`.nt-row[data-ids~="${CSS && CSS.escape ? CSS.escape(focusId) : focusId}"]`)
        || Array.from(list.querySelectorAll(".nt-row")).find((r) => (r.getAttribute("data-ids") || "").split(",").includes(focusId));
      if (target) {
        target.scrollIntoView({ block: "center" });
        target.focus({ preventScroll: true });
      }
      focusId = null;
    } else if (activeId) {
      const again = list.querySelector(`.nt-row[data-id="${activeId}"]`);
      if (again) again.focus({ preventScroll: true });
    }
  }

  function emptyState(filtering) {
    if (filtering) {
      return h("div", { class: "nt-empty" }, [
        h("div", { class: "nt-empty__title" }, "Nothing matches these filters"),
        h("button", {
          type: "button", class: "btn btn--ghost btn--sm",
          onclick: () => { clearFilters(); loadPanel(); },
        }, "Clear filters"),
      ]);
    }
    return h("div", { class: "nt-empty" }, [
      h("div", { class: "nt-empty__title" }, "No notifications"),
      h("div", { class: "nt-empty__text" },
        "Reminders, finished tasks, failed jobs and anything else Jarvis sends will collect here."),
    ]);
  }

  function clearFilters() {
    ui.unread = ui.failed = ui.ack = false;
    ui.kind = "";
    ui.q = "";
    const box = document.getElementById("notif-search");
    if (box) box.value = "";
  }

  /** Run a mutation, then reload; a failure reloads too so the list never
   *  shows state the server doesn't have. */
  async function act(fn) {
    try { await fn(); } finally { await loadPanel(); }
  }

  async function dismissIds(ids) {
    const list = (ids || []).filter(Boolean);
    if (!list.length) return 0;
    stopResurface(list);
    try {
      const out = await api("POST", API.dismiss, { ids: list });
      return (out && out.dismissed) | 0;
    } catch (err) {
      hostToast(`Couldn't dismiss: ${err.message}`, "error");
      return 0;
    }
  }

  async function markAllRead() {
    for (const id of Array.from(resurface.keys())) stopResurface([id]);
    for (const card of Array.from(cards.values())) removeCard(card);
    try { await api("POST", API.read, { all: true }); }
    catch (err) { hostToast(`Couldn't mark all read: ${err.message}`, "error"); }
    await loadPanel();
  }

  async function clearRead() {
    const readCount = Math.max(0, data.total - data.unread);
    if (!readCount) return;
    const UI = global.JarvisUI;
    if (UI && UI.confirm) {
      const ok = await UI.confirm({
        title: "Delete read notifications?",
        body: `${readCount} read notification${readCount === 1 ? "" : "s"} will be removed for good. Unread ones are kept.`,
        confirmLabel: "Delete", cancelLabel: "Cancel", level: "warn", focusCancel: true,
      });
      if (!ok) return;
    }
    try { await api("POST", API.dismiss, { scope: "read" }); }
    catch (err) { hostToast(`Couldn't clear: ${err.message}`, "error"); }
    await loadPanel();
  }

  function openPanel(opts) {
    const o = overlay();
    if (!o) return;
    buildPanelOnce();
    focusId = opts && opts.focusId ? opts.focusId : null;
    o.hidden = false;
    if (typeof global.JarvisNotifyPermission === "function") global.JarvisNotifyPermission();
    loadPanel();
  }

  function closePanel() {
    const o = overlay();
    if (o) o.hidden = true;
  }

  function moveFocus(delta) {
    const rows = Array.from(listEl().querySelectorAll(".nt-row"));
    if (!rows.length) return;
    const at = rows.indexOf(document.activeElement);
    const next = at < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, at + delta));
    rows[next].focus();
    rows[next].scrollIntoView({ block: "nearest" });
  }

  function onPanelKey(ev) {
    if (!panelOpen() || modalOpen()) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((ev.target && ev.target.tagName) || "");
    if (ev.key === "Escape") {
      const box = document.getElementById("notif-search");
      if (typing && box && box.value) { ev.preventDefault(); box.value = ""; ui.q = ""; loadPanel(); return; }
      ev.preventDefault();
      closePanel();
      return;
    }
    if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const row = document.activeElement && document.activeElement.classList
      && document.activeElement.classList.contains("nt-row") ? document.activeElement : null;
    if (ev.key === "/") {
      ev.preventDefault();
      const box = document.getElementById("notif-search");
      if (box) box.focus();
    } else if (ev.key === "j") { ev.preventDefault(); moveFocus(1); }
    else if (ev.key === "k") { ev.preventDefault(); moveFocus(-1); }
    else if (ev.key === "r" && row) {
      ev.preventDefault();
      const ids = (row.getAttribute("data-ids") || "").split(",").filter(Boolean);
      act(() => markRead(ids));
    } else if (ev.key === "x" && row) {
      ev.preventDefault();
      const ids = (row.getAttribute("data-ids") || "").split(",").filter(Boolean);
      act(() => dismissIds(ids));
    }
  }

  function buildPanelOnce() {
    if (panelBuilt) return;
    panelBuilt = true;
    const box = document.getElementById("notif-search");
    if (box) {
      box.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { ui.q = box.value; loadPanel(); }, SEARCH_DEBOUNCE_MS);
      });
    }
    const sel = document.getElementById("notif-kind");
    if (sel) sel.addEventListener("change", () => { ui.kind = sel.value; loadPanel(); });
  }

  /* ======================================================================
   * 6. wiring
   * ==================================================================== */

  function bind(id, event, fn) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(event, fn);
  }

  function init() {
    ensureStack(DEFAULT_POSITION);
    loadCatalog();
    bind("btn-notifications-fab", "click", () => openPanel());
    bind("btn-notifications-refresh", "click", () => loadPanel());
    bind("btn-notifications-markall", "click", () => markAllRead());
    bind("btn-notifications-clearread", "click", () => clearRead());
    bind("notifications-close", "click", closePanel);
    const o = overlay();
    if (o) o.addEventListener("click", (ev) => { if (ev.target === o) closePanel(); });
    document.addEventListener("keydown", onPanelKey);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") syncSummary(false);
    });
    setInterval(() => { if (document.visibilityState === "visible") syncSummary(false); }, SYNC_POLL_MS);
    // The badge is the durable count, and anything still awaiting an
    // acknowledgment comes back on screen after a reload.
    syncSummary(true);
  }

  global.JarvisNotifications = {
    show, open: openPanel, close: closePanel, isOpen: panelOpen,
    refresh: () => (panelOpen() ? loadPanel() : syncSummary(false)),
    markRead, send, _pure: PURE,
    // E3: the page's one browser-notification path (see raiseOs).
    osNotice, requestPermission,
    // E0: the catalog the notifier served (null until loaded); Settings reads it.
    catalog: () => catalog, loadCatalog,
    // The shared reader every picker uses (see "NO HARD-CODED LISTS" above).
    pickerActions, pickerLevels, pickerKinds, pickerPositions, pickerHint,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(window);
