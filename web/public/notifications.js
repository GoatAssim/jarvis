/* ============================================================================
 * notifications.js — L.30 notification rework (rev. 2026-10-04).
 *
 * Replaces two things that used to live in app.js:
 *
 *   1. THE TOAST. Notifications were shown through the one shared `#toast`
 *      element — the same one every error message uses — so two notifications
 *      in the same tick overwrote each other, and any error toast wiped a
 *      reminder off the screen. Now each notification is its own card in a
 *      stack (`#ntf-stack`), with source, time, a coalesced "x3" count for
 *      bursts, and real actions (Acknowledge, Open chat / schedule).
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
 *  - The level rules (D.2.1). 1 = inbox only; 2 = + toast; 3 = + re-surfaces
 *    until acknowledged (D-N1, owner-decided: every 10 minutes, at most 6
 *    times); 4 = + broadcast; 5 = + a blocking Acknowledge dialog.
 *  - app.js's generic toast() for errors and "copied" messages.
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
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ======================================================================
   * 1. constants
   * ==================================================================== */

  const API = {
    history: "/api/notifications/history",
    summary: "/api/notifications/summary",
    read: "/api/notifications/read",
    dismiss: "/api/notifications/dismiss",
  };

  const AUTO_HIDE_MS = 8000;             // a standard (level 2) card
  const AUTO_HIDE_ACK_MS = 12000;        // a card that still needs acknowledging
  const AFTER_HOVER_MS = 3000;           // grace period once the pointer leaves a card
  const MAX_VISIBLE = 4;                 // cards on screen at once; the rest live in the panel
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

  function levelOf(n) {
    const lv = Number(n && n.level);
    return Number.isFinite(lv) && lv >= 1 && lv <= 5 ? lv : 2;   // old records: Standard
  }

  function levelBadge(n) {
    const lv = levelOf(n);
    if (lv === 1) return "silent";
    if (lv === 3) return "persistent";
    if (lv === 4) return "broadcast";
    if (lv >= 5) return "confirm";
    return "";
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

  const PURE = {
    parseTime, groupKey, needsAckOf, isUnreadOf, levelOf, levelBadge, relTime, clockTime,
    fullTime, dayLabel, collapseBursts, sectionByDay, historyQuery, filtersActive, bodyOf,
    statusLine,
    constants: {
      AUTO_HIDE_MS, AUTO_HIDE_ACK_MS, MAX_VISIBLE, BURST_WINDOW_MS, RESURFACE_INTERVAL_MS,
      RESURFACE_MAX, PANEL_LIMIT,
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
  let stackHost = null;

  function ensureStack() {
    if (stackHost && document.body.contains(stackHost)) return stackHost;
    stackHost = h("div", {
      id: "ntf-stack", class: "ntf-stack", role: "region", "aria-label": "Notifications",
    });
    document.body.appendChild(stackHost);
    return stackHost;
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
    const host = ensureStack();
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

  function osNotify(n) {
    if (document.visibilityState === "visible") return;
    if (!("Notification" in global) || global.Notification.permission !== "granted") return;
    try {
      // Tagged per id so two reminders in one tick don't collapse into one.
      const os = new global.Notification(String(n.title || "Jarvis").slice(0, 88), {
        body: bodyOf(n, 160),
        tag: `jarvis-note-${n.id || Date.now()}`,
      });
      os.onclick = () => { global.focus(); os.close(); openPanel({ focusId: n.id }); };
    } catch { /* private mode / unsupported */ }
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

    const level = levelOf(n);
    if (level >= 2) {
      toastFor(n);
      osNotify(n);
    }
    if (needsAckOf(n)) trackResurface(n);
    if (level >= 5 || n.confirm_required) {
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
    ensureStack();
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
    markRead, _pure: PURE,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(window);
