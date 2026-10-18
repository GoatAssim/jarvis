/* ============================================================================
 * calendar.js — Menu → Calendars.
 *
 * L.54. Look at events (Agenda / Week / Month), link a calendar with a
 * step-by-step guide, manage Jarvis's own events, and set a Jarvis reminder.
 *
 * Deliberately standalone, like backlog.js and daemons.js: its own tiny
 * el/api/toast helpers, no reach into app.js's internals. Everything goes
 * through /api/calendar/* (server.js → `jarvis calendar-admin`), plus the
 * existing POST /api/scheduled for "Remind me".
 *
 * RULES THIS FILE KEEPS (they come from the plan's L.54 entry and AGENTS.md):
 *  - A linked calendar is read-only. Only Jarvis's own events get Edit/Delete.
 *  - A calendar link is a secret. The server only ever sends it back masked;
 *    the one place this page holds a real link is the guide's paste box, and
 *    it is dropped the moment the guide closes.
 *  - Event text comes from other people's calendars. It is only ever put on
 *    the page with textContent (never innerHTML), and never turned into links.
 *  - "Remind me" only CREATES a reminder. The scheduler has no operation that
 *    changes an existing job, so there is no editing a reminder from here:
 *    cancel it in Menu → Scheduled and set a new one.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ---- tiny helpers, same shape as backlog.js ---------------------------- */

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
  const $ = (sel, root) => (root || document).querySelector(sel);
  const UI = () => global.JarvisUI || null;
  function toast(message, level) {
    if (UI() && UI().toast) UI().toast({ message, level: level || "info" });
    else console.error(message); // last-resort, never silent
  }

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: Object.assign(
        { "X-Jarvis-Calendar": "1" },
        body ? { "Content-Type": "application/json" } : {},
      ),
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || `${method} ${url} failed (${res.status})`);
    return data;
  }
  const Api = {
    get: (p) => api("GET", p),
    post: (p, b) => api("POST", p, b || {}),
    put: (p, b) => api("PUT", p, b || {}),
    del: (p) => api("DELETE", p),
  };

  /* ---- pure helpers (exposed as JarvisCalendar._pure for node tests) ------- *
   * Times are naive local times everywhere, the way the rest of the project
   * (timespec.py) treats them: "2026-10-12T14:00:00" means 14:00 on this PC. */

  const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const MONTH_LONG = ["January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"];
  const AGENDA_DAYS = 14;
  const POLL_MS = 60 * 1000;   // how often an open panel quietly re-reads (see pollEvents)
  const LEAD_CHOICES = [5, 10, 15, 30, 60, 120, 1440];
  const ALL_DAY_REMINDER_HOUR = 9;

  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

  function parseLocal(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(String(iso || ""));
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
  }
  function toLocalIso(d) {
    return `${ymd(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  function addDays(d, n) {
    // setDate keeps local midnight across a clock change; adding 24h of ms would not.
    const out = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds());
    out.setDate(out.getDate() + n);
    return out;
  }
  /* The day a week starts on is a setting (calendar.json "week_start": mon / sun / sat).
   * Functions below take it as a JS weekday number (0 = Sunday ... 6 = Saturday) and
   * default to Monday, which is what the panel did before it was a setting. */
  const WEEK_START_DAYS = { mon: 1, sun: 0, sat: 6 };
  const weekStartIndex = (code) => (Object.prototype.hasOwnProperty.call(WEEK_START_DAYS, code) ? WEEK_START_DAYS[code] : 1);
  function startOfWeek(d, ws) {
    const first = ws === undefined ? 1 : ws;
    const day = startOfDay(d);
    return addDays(day, -((day.getDay() - first + 7) % 7));
  }
  /* The seven weekday names, the week's first day first. */
  function dowNames(ws) {
    const first = ws === undefined ? 1 : ws;
    return Array.from({ length: 7 }, (_, i) => DAY_NAMES[(first + i) % 7]);
  }
  function monthStart(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }

  /* The window of days each view asks the server for. */
  function windowFor(view, anchor, ws) {
    if (view === "week") {
      const start = startOfWeek(anchor, ws);
      return { start, end: addDays(start, 7) };
    }
    if (view === "month") {
      const start = startOfWeek(monthStart(anchor), ws);
      return { start, end: addDays(start, 42) };
    }
    const start = startOfDay(anchor);
    return { start, end: addDays(start, AGENDA_DAYS) };
  }
  function stepAnchor(view, anchor, dir) {
    if (view === "week") return addDays(anchor, 7 * dir);
    if (view === "month") return new Date(anchor.getFullYear(), anchor.getMonth() + dir, 1);
    return addDays(anchor, AGENDA_DAYS * dir);
  }
  function rangeLabel(view, anchor, ws) {
    if (view === "month") return `${MONTH_LONG[anchor.getMonth()]} ${anchor.getFullYear()}`;
    const { start, end } = windowFor(view, anchor, ws);
    const last = addDays(end, -1);
    const a = `${start.getDate()} ${MONTH_NAMES[start.getMonth()]}`;
    const b = `${last.getDate()} ${MONTH_NAMES[last.getMonth()]} ${last.getFullYear()}`;
    return `${a} – ${b}`;
  }
  /* Six whole weeks (Monday-first unless told otherwise) covering the anchor's month. */
  function monthGrid(anchor, ws) {
    const first = monthStart(anchor);
    const start = startOfWeek(first, ws);
    const cells = [];
    for (let i = 0; i < 42; i++) {
      const date = addDays(start, i);
      cells.push({ date, key: ymd(date), inMonth: date.getMonth() === first.getMonth() });
    }
    return cells;
  }

  /* The days (as YYYY-MM-DD keys) an event touches. An end at exactly midnight does
   * not reach that day: an all-day event ends at the NEXT midnight (exclusive). */
  function eventDayKeys(ev) {
    const start = parseLocal(ev.start);
    let end = parseLocal(ev.end) || start;
    if (!start) return [];
    if (end > start) end = new Date(end.getTime() - 1);
    else end = start;
    const keys = [];
    let day = startOfDay(start);
    const last = startOfDay(end);
    for (let guard = 0; day <= last && guard < 400; guard++) {
      keys.push(ymd(day));
      day = addDays(day, 1);
    }
    return keys;
  }
  function sortEvents(list) {
    return list.slice().sort((a, b) =>
      (b.all_day ? 1 : 0) - (a.all_day ? 1 : 0) ||
      String(a.start).localeCompare(String(b.start)) ||
      String(a.title).localeCompare(String(b.title)));
  }
  function groupByDay(events) {
    const out = {};
    (events || []).forEach((ev) => {
      eventDayKeys(ev).forEach((k) => { (out[k] = out[k] || []).push(ev); });
    });
    Object.keys(out).forEach((k) => { out[k] = sortEvents(out[k]); });
    return out;
  }
  function dayLabel(date, now) {
    const today = startOfDay(now || new Date());
    const diff = Math.round((startOfDay(date) - today) / 86400000);
    const base = `${DAY_NAMES[date.getDay()]} ${date.getDate()} ${MONTH_NAMES[date.getMonth()]}`;
    if (diff === 0) return `Today · ${base}`;
    if (diff === 1) return `Tomorrow · ${base}`;
    if (diff === -1) return `Yesterday · ${base}`;
    return base;
  }
  function timeLabel(ev) {
    if (ev.all_day) return "All day";
    const s = parseLocal(ev.start);
    const e = parseLocal(ev.end);
    if (!s) return "";
    if (!e || e <= s) return hhmm(s);
    const sameDay = ymd(s) === ymd(e);
    return sameDay ? `${hhmm(s)}–${hhmm(e)}` : `${hhmm(s)} → ${DAY_NAMES[e.getDay()]} ${hhmm(e)}`;
  }
  function whenLabel(ev) {
    const s = parseLocal(ev.start);
    if (!s) return "";
    const day = `${DAY_NAMES[s.getDay()]} ${s.getDate()} ${MONTH_NAMES[s.getMonth()]} ${s.getFullYear()}`;
    if (ev.all_day) {
      const e = parseLocal(ev.end);
      const lastDay = e ? addDays(e, -1) : s;
      return ymd(lastDay) > ymd(s)
        ? `${day} → ${DAY_NAMES[lastDay.getDay()]} ${lastDay.getDate()} ${MONTH_NAMES[lastDay.getMonth()]} · all day`
        : `${day} · all day`;
    }
    return `${day} · ${timeLabel(ev)}`;
  }
  function leadLabel(minutes) {
    if (minutes === 1440) return "1 day before";
    if (minutes >= 120 && minutes % 60 === 0) return `${minutes / 60} hours before`;
    if (minutes === 60) return "1 hour before";
    return `${minutes} minutes before`;
  }
  function agoLabel(iso, now) {
    const t = parseLocal(iso);
    if (!t) return "never";
    const mins = Math.max(0, Math.round(((now || new Date()) - t) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    if (mins < 1440) return `${Math.round(mins / 60)} h ago`;
    return `${Math.round(mins / 1440)} d ago`;
  }

  /* What "Remind me" would create, or why it can't. A reminder is a one-shot scheduled
   * job at a real timestamp (the plan's L.54: the existing schedule path, not a calendar
   * entry). An all-day event has no start time to count back from, so it reminds at
   * 09:00 that morning. Nothing here edits anything: it only builds a new reminder. */
  function reminderPlan(ev, leadMinutes, now) {
    const start = parseLocal(ev && ev.start);
    const current = now || new Date();
    if (!start) return { ok: false, error: "This event has no usable start time." };
    let fire;
    let lead = 0;
    if (ev.all_day) {
      fire = new Date(start.getFullYear(), start.getMonth(), start.getDate(), ALL_DAY_REMINDER_HOUR, 0, 0);
    } else {
      lead = Number(leadMinutes);
      if (!Number.isFinite(lead) || lead < 0 || lead > 10080) return { ok: false, error: "Pick how long before." };
      fire = new Date(start.getTime() - lead * 60000);
    }
    if (fire.getTime() <= current.getTime() + 30000) {
      return {
        ok: false,
        error: ev.all_day
          ? "That reminder time (09:00 on the day) has already passed."
          : "That reminder time has already passed. Pick a shorter lead time.",
      };
    }
    const where = ev.location ? ` — ${ev.location}` : "";
    const message = ev.all_day
      ? `Reminder: ${ev.title} today (all day)${where}`
      : `Reminder: ${ev.title} at ${hhmm(start)}${where} (in ${leadLabel(lead).replace(" before", "")})`;
    return { ok: true, when: toLocalIso(fire), fire, message: message.slice(0, 300) };
  }

  /* The event form's values -> what the server takes, or an error to show. */
  function eventPayload(form) {
    const title = String(form.title || "").trim();
    if (!title) return { ok: false, error: "Give the event a title." };
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(form.date || ""))) return { ok: false, error: "Pick a date." };
    const out = {
      title,
      date: form.date,
      all_day: Boolean(form.all_day),
      location: String(form.location || "").trim(),
      description: String(form.description || "").trim(),
    };
    if (!out.all_day) {
      if (!/^\d{2}:\d{2}$/.test(String(form.time || ""))) return { ok: false, error: "Pick a start time, or tick All day." };
      const minutes = Number(form.duration);
      if (!Number.isFinite(minutes) || minutes < 5 || minutes > 1440) {
        return { ok: false, error: "The duration must be between 5 and 1440 minutes." };
      }
      out.time = form.time;
      out.duration_minutes = Math.round(minutes);
    }
    return { ok: true, event: out };
  }
  /* A stored event -> the form's starting values. */
  function formFromEvent(ev) {
    const s = parseLocal(ev.start);
    const e = parseLocal(ev.end);
    return {
      title: ev.title || "",
      date: s ? ymd(s) : "",
      all_day: Boolean(ev.all_day),
      time: s && !ev.all_day ? hhmm(s) : "09:00",
      duration: s && e && !ev.all_day ? Math.max(5, Math.round((e - s) / 60000)) : 60,
      location: ev.location || "",
      description: ev.description || "",
    };
  }
  function sourceClass(name) {
    let h = 0;
    for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return "cal-src-" + (h % 5 + 1); // 0 is reserved for Jarvis's own calendar
  }

  /* The guide. These are the SHAPE of each provider's steps, not verified text: the
   * screens and names change, which the guide says out loud. */
  const PROVIDERS = [
    {
      id: "google", label: "Google Calendar", hint: "Gmail / Google account", defaultName: "Google",
      steps: [
        "Open Google Calendar in a web browser on a computer (the phone app doesn't show this).",
        "Under “My calendars” on the left, hover the calendar, click ⋮ and choose “Settings and sharing”.",
        "Scroll to “Integrate calendar” and copy the “Secret address in iCal format”.",
      ],
    },
    {
      id: "outlook", label: "Outlook / Microsoft 365", hint: "outlook.com, Office 365", defaultName: "Outlook",
      steps: [
        "Open Outlook on the web and go to Settings → Calendar → Shared calendars.",
        "Under “Publish a calendar”, pick the calendar and “Can view all details”, then click Publish.",
        "Copy the ICS link (not the HTML link).",
      ],
    },
    {
      id: "apple", label: "Apple / iCloud", hint: "Calendar on Mac, iPhone or iCloud.com", defaultName: "iCloud",
      steps: [
        "Open iCloud.com → Calendar, or the Calendar app on a Mac.",
        "Share the calendar and switch on “Public Calendar”.",
        "Copy the link. It usually starts with webcal:// — that works as it is.",
      ],
    },
    {
      id: "fastmail", label: "Fastmail", hint: "Fastmail calendars", defaultName: "Fastmail",
      steps: [
        "Open Fastmail → Settings → Calendars.",
        "Open the calendar and look for its sharing or export options.",
        "Create or copy its iCalendar (.ics) subscription link.",
      ],
    },
    {
      id: "other", label: "Any other calendar", hint: "Anything that gives you an .ics link", defaultName: "Calendar",
      steps: [
        "Look for “iCal”, “ICS”, “subscribe” or “export” in the calendar's sharing settings.",
        "You want a link that ends in .ics (or starts with webcal://), not the calendar's normal web page.",
        "Copy that link.",
      ],
    },
  ];

  const pure = {
    pad, ymd, hhmm, parseLocal, toLocalIso, startOfDay, addDays, startOfWeek, monthStart,
    weekStartIndex, dowNames, windowFor, stepAnchor, rangeLabel, monthGrid, eventDayKeys, sortEvents, groupByDay,
    dayLabel, timeLabel, whenLabel, leadLabel, agoLabel, reminderPlan, eventPayload,
    formFromEvent, sourceClass, PROVIDERS, LEAD_CHOICES, AGENDA_DAYS,
  };

  /* ---- state ------------------------------------------------------------- */

  const state = {
    built: false,
    tab: "events",            // "events" | "calendars"
    view: "agenda",           // "agenda" | "week" | "month"
    anchor: startOfDay(new Date()),
    events: [],
    problems: [],
    truncated: false,
    loading: false,
    loadError: "",
    reqId: 0,
    selectedId: null,
    selectedDay: ymd(new Date()),
    feeds: [],
    meta: null,               // feeds_view() extras (limits, default lead)
    leadMinutes: 60,
    reminded: new Set(),      // this session only: "<event id>|<start>|<lead>"
    form: null,               // the open event form, or null
    wizard: null,             // the open "link a calendar" guide, or null
    prevFocus: null,
    openedOn: ymd(new Date()), // the calendar day the window above was last aligned to (see rollDay)
    pollTimer: null,           // background re-read while the panel is open (see startPolling)
  };

  try {
    const saved = global.localStorage && global.localStorage.getItem("jarvis.calendar.view");
    if (saved === "agenda" || saved === "week" || saved === "month") state.view = saved;
  } catch { /* storage can be unavailable; the default is fine */ }

  let dom = null;
  function grabDom() {
    const overlay = $("#calendar-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#calendar-status-line"),
      tabEvents: $("#calendar-tab-events"),
      tabCalendars: $("#calendar-tab-calendars"),
      toolbar: $("#calendar-toolbar"),
      problems: $("#calendar-problems"),
      main: $("#calendar-main"),
      side: $("#calendar-side"),
      body: $("#calendar-body"),
      layer: $("#calendar-layer"),
    };
  }

  const eventById = (id) => state.events.find((e) => e.id === id) || null;
  /* The week start the owner chose (Calendars tab), as a JS weekday number. */
  const weekIdx = () => weekStartIndex(state.meta && state.meta.week_start);

  /* ---- data ---------------------------------------------------------------- */

  async function loadFeeds() {
    try {
      const data = await Api.get("/api/calendar/feeds");
      state.feeds = data.feeds || [];
      state.meta = data;
      if (!state.reminderLeadTouched) state.leadMinutes = data.default_reminder_minutes || 60;
    } catch (err) {
      state.feeds = [];
      state.meta = null;
      state.loadError = err.message || "Couldn't read the calendars.";
    }
  }

  async function loadEvents(refresh) {
    const mine = ++state.reqId;
    const { start, end } = windowFor(state.view, state.anchor, weekIdx());
    state.loading = true;
    state.loadError = "";
    renderStatus();
    renderMain();
    try {
      const qs = `start=${encodeURIComponent(ymd(start))}&end=${encodeURIComponent(ymd(end))}${refresh ? "&refresh=1" : ""}`;
      const data = await Api.get(`/api/calendar/window?${qs}`);
      if (mine !== state.reqId) return; // a newer request is on its way
      state.events = data.events || [];
      state.problems = data.problems || [];
      state.truncated = Boolean(data.truncated);
      if (state.selectedId && !eventById(state.selectedId)) state.selectedId = null;
    } catch (err) {
      if (mine !== state.reqId) return;
      state.events = [];
      state.problems = [];
      state.loadError = err.message || "Couldn't read the calendar.";
    }
    state.loading = false;
    renderAll();
  }

  /* ---- keeping an open panel current ---------------------------------------------
   * The fetch path was never the problem (Refresh and the r key force a re-download and
   * work). What was missing was anything that ASKED again: the panel read once when it
   * opened and then never again, and `anchor` / `selectedDay` were fixed when the page
   * loaded, so a browser tab left open overnight opened on yesterday's agenda. */

  // Move "today" forward when the calendar day has changed since the window was last aligned.
  // Only a view that was sitting on today follows it; one the person paged away from stays.
  function rollDay() {
    const today = ymd(new Date());
    if (state.openedOn === today) return false;
    const was = state.openedOn;
    state.openedOn = today;
    if (ymd(state.anchor) === was) state.anchor = startOfDay(new Date());
    if (state.selectedDay === was) state.selectedDay = today;
    return true;
  }

  // A quiet re-read (cached on the server, so it only re-downloads a calendar whose copy is
  // older than the cache time). It repaints only when something changed, so it never resets
  // a scroll position or an open event, and it never touches a form or the link guide.
  async function pollEvents() {
    if (!isOpen() || (typeof document !== "undefined" && document.hidden)) return;
    if (state.form || state.wizard || state.loading || state.tab !== "events") return;
    if (rollDay()) { loadEvents(false); return; }
    const mine = ++state.reqId;
    const { start, end } = windowFor(state.view, state.anchor, weekIdx());
    try {
      const data = await Api.get(`/api/calendar/window?start=${encodeURIComponent(ymd(start))}&end=${encodeURIComponent(ymd(end))}`);
      if (mine !== state.reqId || state.form || state.wizard || state.loading) return;
      const events = data.events || [];
      const problems = data.problems || [];
      const truncated = Boolean(data.truncated);
      if (JSON.stringify([events, problems, truncated]) === JSON.stringify([state.events, state.problems, state.truncated])) return;
      state.events = events;
      state.problems = problems;
      state.truncated = truncated;
      state.loadError = "";
      if (state.selectedId && !eventById(state.selectedId)) state.selectedId = null;
      renderAll();
    } catch (_) { /* a failed background read keeps what is on screen; the next one retries */ }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollEvents, POLL_MS);
  }
  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  async function refreshAll(refresh) {
    // Settings first: the window a Week / Month view asks for depends on the week start.
    await loadFeeds();
    await loadEvents(refresh);
    renderAll();
  }

  /* ---- rendering: shell -------------------------------------------------------- */

  function renderStatus() {
    if (!dom) return;
    const linked = state.feeds.length;
    let text;
    if (state.loadError) text = "couldn't read the calendar";
    else if (state.loading && !state.events.length) text = "reading calendars…";
    else {
      const n = state.events.length;
      text = `${n} event${n === 1 ? "" : "s"} · ${linked ? `${linked} linked calendar${linked === 1 ? "" : "s"}` : "no linked calendars"}`;
    }
    dom.statusLine.textContent = text;
  }

  function renderTabs() {
    dom.tabEvents.classList.toggle("is-active", state.tab === "events");
    dom.tabCalendars.classList.toggle("is-active", state.tab === "calendars");
    dom.tabEvents.setAttribute("aria-selected", String(state.tab === "events"));
    dom.tabCalendars.setAttribute("aria-selected", String(state.tab === "calendars"));
  }

  function renderToolbar() {
    dom.toolbar.textContent = "";
    dom.toolbar.hidden = state.tab !== "events";
    if (state.tab !== "events") return;
    const seg = el("div", { class: "cal-seg", role: "group", "aria-label": "View" },
      [["agenda", "Agenda"], ["week", "Week"], ["month", "Month"]].map(([id, label]) =>
        el("button", {
          type: "button", class: "cal-seg__btn" + (state.view === id ? " is-active" : ""),
          "aria-pressed": String(state.view === id), onclick: () => setView(id),
        }, label)));
    const nav = el("div", { class: "cal-nav" }, [
      el("button", { type: "button", class: "btn btn--ghost btn--sm", "aria-label": "Previous", title: "Previous (←)", onclick: () => step(-1) }, "‹"),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", title: "Jump to today (t)", onclick: goToday }, "Today"),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", "aria-label": "Next", title: "Next (→)", onclick: () => step(1) }, "›"),
    ]);
    dom.toolbar.append(
      seg, nav,
      el("span", { class: "cal-range" }, rangeLabel(state.view, state.anchor, weekIdx())),
      el("span", { class: "cal-toolbar__spacer" }),
      el("button", { type: "button", class: "btn btn--ghost btn--sm", title: "Read the linked calendars again (r)", onclick: () => refreshAll(true) }, "Refresh"),
      el("button", { type: "button", class: "btn btn--primary btn--sm", title: "Add an event to Jarvis's calendar (n)", onclick: () => openForm(null) }, "+ Add event"),
    );
  }

  function renderProblems() {
    dom.problems.textContent = "";
    const lines = [];
    if (state.tab === "events") {
      state.problems.forEach((p) => lines.push(p));
      if (state.truncated) lines.push("Showing the first 2000 events of this window.");
    }
    dom.problems.hidden = !lines.length;
    if (!lines.length) return;
    dom.problems.append(
      el("strong", {}, "Heads up: "),
      lines.join(" · "),
      el("span", { class: "cal-problems__note" }, " (showing the last copy Jarvis has, if any)"),
    );
  }

  function renderAll() {
    if (!dom) return;
    renderStatus();
    renderTabs();
    renderToolbar();
    renderProblems();
    renderMain();
    renderSide();
    renderLayer();
  }

  function renderMain() {
    if (!dom) return;
    dom.main.textContent = "";
    if (state.tab === "calendars") {
      dom.main.append(state.wizard ? buildWizard() : buildCalendarsTab());
      return;
    }
    if (state.loadError) {
      dom.main.append(el("div", { class: "cal-empty" }, [
        el("div", { class: "cal-empty__title" }, "Couldn't read the calendar"),
        el("div", { class: "cal-empty__text" }, state.loadError),
        el("button", { type: "button", class: "btn btn--sm", onclick: () => refreshAll(false) }, "Try again"),
      ]));
      return;
    }
    if (state.loading && !state.events.length) {
      dom.main.append(el("div", { class: "cal-empty" }, el("div", { class: "cal-empty__text" }, "Reading calendars…")));
      return;
    }
    dom.main.append(state.view === "month" ? buildMonth() : state.view === "week" ? buildWeek() : buildAgenda());
  }

  /* ---- rendering: events ---------------------------------------------------------- */

  function eventChip(ev, opts) {
    const o = opts || {};
    const now = new Date();
    const ended = parseLocal(ev.end) && parseLocal(ev.end) < now;
    const cls = ["cal-ev", ev.local ? "cal-src-0" : sourceClass(ev.source)];
    if (ev.all_day) cls.push("cal-ev--allday");
    if (ended) cls.push("is-past");
    if (state.selectedId === ev.id) cls.push("is-selected");
    const kids = [];
    if (!o.compact) kids.push(el("span", { class: "cal-ev__time" }, timeLabel(ev)));
    else if (!ev.all_day) kids.push(el("span", { class: "cal-ev__time" }, hhmm(parseLocal(ev.start))));
    kids.push(el("span", { class: "cal-ev__title" }, ev.title));
    if (!o.compact && ev.location) kids.push(el("span", { class: "cal-ev__where" }, ev.location));
    if (!o.compact) kids.push(el("span", { class: "cal-ev__src" }, ev.source));
    return el("button", {
      type: "button", class: cls.join(" "), "data-event-id": ev.id,
      title: `${ev.title} — ${timeLabel(ev)}`, onclick: () => selectEvent(ev.id),
    }, kids);
  }

  function emptyState() {
    const noFeeds = !state.feeds.length;
    return el("div", { class: "cal-empty" }, [
      el("div", { class: "cal-empty__title" }, "Nothing here"),
      el("div", { class: "cal-empty__text" }, noFeeds
        ? "No events in this window, and no calendar is linked yet. Link one, or add an event to Jarvis's own calendar."
        : "No events in this window."),
      el("div", { class: "cal-empty__actions" }, [
        noFeeds ? el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: () => startWizard() }, "Link a calendar") : null,
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => openForm(null) }, "+ Add event"),
      ]),
    ]);
  }

  function buildAgenda() {
    const { start } = windowFor("agenda", state.anchor);
    const groups = groupByDay(state.events);
    const wrap = el("div", { class: "cal-agenda" });
    const now = new Date();
    let any = false;
    for (let i = 0; i < AGENDA_DAYS; i++) {
      const day = addDays(start, i);
      const key = ymd(day);
      const list = groups[key] || [];
      if (!list.length && key !== ymd(now)) continue;
      any = any || list.length > 0;
      wrap.append(el("section", { class: "cal-day" + (key === ymd(now) ? " is-today" : "") }, [
        el("h3", { class: "cal-day__head" }, [
          dayLabel(day, now),
          el("button", {
            type: "button", class: "cal-day__add", title: "Add an event on this day", "aria-label": `Add an event on ${key}`,
            onclick: () => openForm(null, key),
          }, "+"),
        ]),
        list.length
          ? el("div", { class: "cal-day__list" }, list.map((ev) => eventChip(ev)))
          : el("div", { class: "cal-day__none" }, "Nothing on."),
      ]));
    }
    if (!any && !state.events.length) return emptyState();
    return wrap;
  }

  function buildWeek() {
    const { start } = windowFor("week", state.anchor, weekIdx());
    const groups = groupByDay(state.events);
    const now = new Date();
    const grid = el("div", { class: "cal-week" });
    for (let i = 0; i < 7; i++) {
      const day = addDays(start, i);
      const key = ymd(day);
      const list = groups[key] || [];
      grid.append(el("section", {
        class: "cal-col" + (key === ymd(now) ? " is-today" : "") + (key === state.selectedDay ? " is-picked" : ""),
      }, [
        el("button", { type: "button", class: "cal-col__head", onclick: () => pickDay(key) }, [
          el("span", { class: "cal-col__dow" }, DAY_NAMES[day.getDay()]),
          el("span", { class: "cal-col__num" }, String(day.getDate())),
        ]),
        el("div", { class: "cal-col__list" }, list.length
          ? list.map((ev) => eventChip(ev, { compact: true }))
          : el("div", { class: "cal-col__none" }, "—")),
      ]));
    }
    return grid;
  }

  function buildMonth() {
    const cells = monthGrid(state.anchor, weekIdx());
    const groups = groupByDay(state.events);
    const now = new Date();
    const grid = el("div", { class: "cal-month" });
    dowNames(weekIdx()).forEach((d) => grid.append(el("div", { class: "cal-month__dow" }, d)));
    cells.forEach((cell) => {
      const list = groups[cell.key] || [];
      const shown = list.slice(0, 3);
      const cls = ["cal-cell"];
      if (!cell.inMonth) cls.push("is-outside");
      if (cell.key === ymd(now)) cls.push("is-today");
      if (cell.key === state.selectedDay) cls.push("is-picked");
      grid.append(el("div", { class: cls.join(" ") }, [
        el("button", {
          type: "button", class: "cal-cell__num", "aria-label": `${cell.key}, ${list.length} event${list.length === 1 ? "" : "s"}`,
          onclick: () => pickDay(cell.key),
        }, String(cell.date.getDate())),
        ...shown.map((ev) => eventChip(ev, { compact: true })),
        list.length > shown.length
          ? el("button", { type: "button", class: "cal-cell__more", onclick: () => pickDay(cell.key) }, `+${list.length - shown.length} more`)
          : null,
      ]));
    });
    return grid;
  }

  /* ---- rendering: side pane ---------------------------------------------------------- */

  function renderSide() {
    if (!dom) return;
    dom.side.textContent = "";
    dom.body.classList.toggle("cal-body--no-side", state.tab !== "events");
    if (state.tab !== "events") return;
    const ev = state.selectedId ? eventById(state.selectedId) : null;
    dom.side.append(ev ? buildDetail(ev) : buildDayPane());
  }

  function buildDayPane() {
    const day = parseLocal(state.selectedDay) || new Date();
    const list = groupByDay(state.events)[ymd(day)] || [];
    return el("div", { class: "cal-pane" }, [
      el("h3", { class: "cal-pane__title" }, dayLabel(day)),
      list.length
        ? el("div", { class: "cal-day__list" }, list.map((e) => eventChip(e)))
        : el("div", { class: "cal-pane__muted" }, "Nothing on this day."),
      el("button", { type: "button", class: "btn btn--primary btn--sm cal-pane__btn", onclick: () => openForm(null, ymd(day)) }, "+ Add event on this day"),
      el("p", { class: "cal-pane__note" }, "Pick an event to see its details, set a reminder, or (for Jarvis's own events) edit it."),
    ]);
  }

  function buildDetail(ev) {
    const reminderKey = `${ev.id}|${ev.start}|${ev.all_day ? "allday" : state.leadMinutes}`;
    const done = state.reminded.has(reminderKey);
    const past = parseLocal(ev.end) && parseLocal(ev.end) < new Date();
    const lead = el("select", {
      id: "cal-lead", class: "cal-select", "aria-label": "How long before",
      onchange: (e) => { state.leadMinutes = Number(e.target.value); state.reminderLeadTouched = true; renderSide(); },
    }, LEAD_CHOICES.concat(LEAD_CHOICES.includes(state.leadMinutes) ? [] : [state.leadMinutes])
      .sort((a, b) => a - b)
      .map((m) => el("option", { value: String(m), selected: m === state.leadMinutes ? true : null }, leadLabel(m))));

    const rows = [
      el("button", { type: "button", class: "cal-pane__back", onclick: () => selectEvent(null) }, "‹ Back to the day"),
      el("h3", { class: "cal-pane__title cal-pane__title--event" }, ev.title),
      el("div", { class: "cal-pane__when" }, whenLabel(ev)),
      ev.tz_note ? el("div", { class: "cal-pane__warn" }, ev.tz_note) : null,
      ev.location ? el("div", { class: "cal-pane__row" }, [el("span", { class: "cal-pane__k" }, "Where"), el("span", {}, ev.location)]) : null,
      el("div", { class: "cal-pane__row" }, [
        el("span", { class: "cal-pane__k" }, "Calendar"),
        el("span", { class: "cal-pill " + (ev.local ? "cal-src-0" : sourceClass(ev.source)) }, ev.source),
      ]),
      ev.description ? el("div", { class: "cal-pane__desc" }, ev.description) : null,
    ];

    if (ev.local && ev.editable) {
      rows.push(el("div", { class: "cal-pane__actions" }, [
        el("button", { type: "button", class: "btn btn--sm", onclick: () => openForm(ev) }, "Edit"),
        el("button", { type: "button", class: "btn btn--sm btn--danger", onclick: () => deleteEvent(ev) }, "Delete"),
      ]));
    } else {
      rows.push(el("p", { class: "cal-pane__note" },
        ev.local
          ? "This event has no id in calendar.json, so it can't be edited or deleted here."
          : `From the linked calendar “${ev.source}”, which is read-only: Jarvis can't change or delete its events. Change it in the calendar itself and Refresh.`));
    }

    rows.push(el("div", { class: "cal-remind" }, [
      el("div", { class: "cal-remind__title" }, "Remind me"),
      ev.all_day
        ? el("div", { class: "cal-pane__muted" }, "An all-day event has no start time, so the reminder fires at 09:00 that morning.")
        : el("label", { class: "cal-remind__row" }, [el("span", {}, "Remind me"), lead]),
      el("button", {
        type: "button", class: "btn btn--primary btn--sm cal-pane__btn", disabled: done || past ? true : null,
        onclick: () => remindMe(ev),
      }, done ? "Reminder set ✓" : past ? "Already over" : "Set reminder"),
      el("p", { class: "cal-pane__note" },
        "This creates a Jarvis reminder, not a calendar entry. A reminder can't be edited once it exists — to change or cancel one, open Menu → Scheduled."),
    ]));
    return el("div", { class: "cal-pane" }, rows);
  }

  /* ---- rendering: Calendars tab + guide ---------------------------------------------------- */

  function buildCalendarsTab() {
    const meta = state.meta || {};
    const list = state.feeds.length
      ? el("div", { class: "cal-feeds" }, state.feeds.map(buildFeedCard))
      : el("div", { class: "cal-empty cal-empty--inline" }, [
        el("div", { class: "cal-empty__title" }, "No calendar linked yet"),
        el("div", { class: "cal-empty__text" }, "Link Google, Outlook, Apple or any calendar that gives you an .ics link. It takes about a minute and the guide walks you through it."),
      ]);
    return el("div", { class: "cal-cals" }, [
      el("div", { class: "cal-cals__head" }, [
        el("div", {}, [
          el("h3", { class: "cal-h" }, "Linked calendars"),
          el("p", { class: "cal-muted" }, "Read-only copies. Jarvis can show their events and remind you about them, but never changes them."),
        ]),
        el("button", {
          type: "button", class: "btn btn--primary btn--sm",
          disabled: state.feeds.length >= (meta.max_feeds || 12) ? true : null, onclick: () => startWizard(),
        }, "+ Link a calendar"),
      ]),
      list,
      el("div", { class: "cal-note" }, [
        el("strong", {}, "How fresh is this? "),
        `A linked calendar is a copy the provider refreshes every few hours, and Jarvis re-reads each one at most every ${meta.cache_minutes || 15} minutes. Refresh re-reads now, but it can't make the provider publish sooner.`,
      ]),
      buildSettings(meta),
    ]);
  }

  /* Jarvis's own calendar: how long its events are kept, and the day weeks start on. */
  function buildSettings(meta) {
    const keep = meta.retention_days || 60;
    const limit = meta.retention_days_max || 3650;
    const dueNow = meta.expiring_count || 0;
    const days = el("input", {
      type: "number", class: "cal-input cal-input--short", id: "cal-set-keep", min: "1", max: String(limit), step: "1",
      value: String(keep), "aria-label": "Days to keep an event after it ends",
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); saveSettings(); } },
    });
    const week = el("select", { class: "cal-select", id: "cal-set-week", "aria-label": "First day of the week" },
      [["mon", "Monday"], ["sun", "Sunday"], ["sat", "Saturday"]].map(([id, label]) =>
        el("option", { value: id, selected: id === (meta.week_start || "mon") ? true : null }, label)));
    return el("div", { class: "cal-note cal-settings" }, [
      el("strong", {}, "Jarvis's own calendar. "),
      `${meta.local_event_count || 0} event${meta.local_event_count === 1 ? "" : "s"} saved, at most ${meta.retention_max || 500} (a full calendar refuses a new event; it never drops one).`,
      el("div", { class: "cal-settings__row" }, [
        el("label", { class: "cal-settings__field" }, [el("span", { class: "cal-label" }, "Keep an event for"), el("span", { class: "cal-input-row" }, [days, el("span", {}, "days after it ends")])]),
        el("label", { class: "cal-settings__field" }, [el("span", { class: "cal-label" }, "Weeks start on"), week]),
        el("button", { type: "button", class: "btn btn--primary btn--sm", id: "cal-set-save", onclick: saveSettings }, "Save"),
      ]),
      el("div", { class: "cal-hint", id: "cal-set-hint" },
        `Older events are removed only when a new event is added, and the add says how many went. ${dueNow ? `${dueNow} saved event${dueNow === 1 ? "" : "s"} ended more than ${keep} days ago and will go the next time one is added.` : "None is past that now."}`),
    ]);
  }

  async function saveSettings() {
    const days = $("#cal-set-keep");
    const week = $("#cal-set-week");
    const btn = $("#cal-set-save");
    if (!days || !week) return;
    const keep = Number(days.value);
    if (!Number.isInteger(keep) || keep < 1 || keep > ((state.meta && state.meta.retention_days_max) || 3650)) {
      toast(`Keep events for a whole number of days from 1 to ${(state.meta && state.meta.retention_days_max) || 3650}.`, "warn");
      return;
    }
    if (btn) btn.disabled = true;
    try {
      const before = weekIdx();
      await Api.post("/api/calendar/settings", { keep_days: keep, week_start: week.value });
      await loadFeeds();
      toast("Calendar settings saved.", "info");
      // A different first day moves the Week / Month window, so read it again.
      if (weekIdx() !== before) await loadEvents(false);
      renderAll();
    } catch (err) {
      toast(err.message || "Couldn't save the settings.", "error");
      if (btn) btn.disabled = false;
    }
  }

  function buildFeedCard(feed) {
    return el("div", { class: "cal-feed" + (feed.enabled ? "" : " is-off") }, [
      el("div", { class: "cal-feed__main" }, [
        el("div", { class: "cal-feed__name" }, [
          el("span", { class: "cal-dot " + sourceClass(feed.name) }),
          feed.name,
          feed.enabled ? null : el("span", { class: "cal-tag" }, "off"),
        ]),
        el("code", { class: "cal-feed__url", title: "The link is a secret, so only part of it is shown." }, feed.url_masked || "(no link)"),
        el("div", { class: "cal-feed__meta" }, `Last read ${feed.last_fetched ? agoLabel(feed.last_fetched) : "never"}`),
      ]),
      el("div", { class: "cal-feed__actions" }, [
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => toggleFeed(feed) }, feed.enabled ? "Turn off" : "Turn on"),
        el("button", { type: "button", class: "btn btn--ghost btn--sm btn--danger", onclick: () => removeFeed(feed) }, "Remove"),
      ]),
    ]);
  }

  function startWizard() {
    state.tab = "calendars";
    state.wizard = { step: 1, provider: null, url: "", name: "", show: false, checking: false, check: null, error: "", linking: false, done: null };
    renderAll();
  }
  function closeWizard() {
    state.wizard = null; // drops the pasted link with it
    renderAll();
  }

  function wizardDots(step) {
    const labels = ["Pick", "Find the link", "Paste & check", "Done"];
    return el("ol", { class: "cal-steps" }, labels.map((label, i) =>
      el("li", { class: "cal-steps__item" + (i + 1 === step ? " is-current" : i + 1 < step ? " is-done" : "") }, [
        el("span", { class: "cal-steps__n" }, String(i + 1)), el("span", { class: "cal-steps__l" }, label),
      ])));
  }

  function buildWizard() {
    const w = state.wizard;
    const provider = PROVIDERS.find((p) => p.id === w.provider) || null;
    const frame = (body) => el("div", { class: "cal-wizard" }, [
      el("div", { class: "cal-wizard__head" }, [
        el("h3", { class: "cal-h" }, "Link a calendar"),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: closeWizard }, w.step === 4 ? "Close" : "Cancel"),
      ]),
      wizardDots(w.step),
      body,
    ]);

    if (w.step === 1) {
      return frame(el("div", {}, [
        el("p", { class: "cal-muted" }, "Which calendar do you want to link?"),
        el("div", { class: "cal-providers" }, PROVIDERS.map((p) =>
          el("button", {
            type: "button", class: "cal-provider", onclick: () => { w.provider = p.id; w.name = p.defaultName; w.step = 2; renderMain(); },
          }, [el("span", { class: "cal-provider__label" }, p.label), el("span", { class: "cal-provider__hint" }, p.hint)]))),
      ]));
    }

    if (w.step === 2 && provider) {
      return frame(el("div", {}, [
        el("p", { class: "cal-muted" }, `Find your ${provider.label} calendar link:`),
        el("ol", { class: "cal-howto" }, provider.steps.map((t) => el("li", {}, t))),
        el("div", { class: "cal-warn" }, [
          el("strong", {}, "Treat the link like a password. "),
          "Anyone who has it can read that calendar. Jarvis keeps it on this PC, and this page only ever shows a hidden version of it.",
        ]),
        el("p", { class: "cal-pane__note" }, "These steps are a guide: providers rename their menus now and then. If you can't find it, search that provider's help for “iCal link” or “ICS”."),
        el("div", { class: "cal-wizard__nav" }, [
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { w.step = 1; renderMain(); } }, "Back"),
          el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: () => { w.step = 3; renderMain(); } }, "I've copied the link"),
        ]),
      ]));
    }

    if (w.step === 3) {
      const input = el("input", {
        type: w.show ? "text" : "password", id: "cal-wiz-url", class: "cal-input", autocomplete: "off", spellcheck: "false",
        "data-lpignore": "true", placeholder: "Paste the link here (https://… or webcal://…)", value: w.url,
        oninput: (e) => { w.url = e.target.value; w.check = null; w.error = ""; refreshWizardControls(); },
        onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); runCheck(); } },
      });
      const result = el("div", { id: "cal-wiz-result", class: "cal-wiz-result" });
      const nameRow = el("div", { id: "cal-wiz-namerow", class: "cal-wiz-namerow" });
      const out = frame(el("div", {}, [
        el("p", { class: "cal-muted" }, "Paste the link, then check it before saving."),
        el("div", { class: "cal-input-row" }, [
          input,
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { w.show = !w.show; input.type = w.show ? "text" : "password"; } }, w.show ? "Hide" : "Show"),
        ]),
        el("div", { class: "cal-wizard__nav" }, [
          el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { w.step = 2; renderMain(); } }, "Back"),
          el("button", { type: "button", id: "cal-wiz-check", class: "btn btn--primary btn--sm", onclick: runCheck }, "Check link"),
        ]),
        result,
        nameRow,
      ]));
      queueMicrotask(() => { refreshWizardControls(); input.focus(); });
      return out;
    }

    // step 4
    const done = w.done || {};
    return frame(el("div", { class: "cal-done" }, [
      el("div", { class: "cal-done__mark", "aria-hidden": "true" }, "✓"),
      el("div", { class: "cal-done__title" }, `“${done.name || "Calendar"}” is linked`),
      el("div", { class: "cal-muted" }, done.next_event
        ? `Found ${done.events_found} event${done.events_found === 1 ? "" : "s"}. Next up: ${done.next_event.title} on ${whenLabel({ start: done.next_event.start, end: done.next_event.start, all_day: false })}.`
        : `Found ${done.events_found || 0} event${done.events_found === 1 ? "" : "s"}, none coming up.`),
      el("div", { class: "cal-wizard__nav" }, [
        el("button", { type: "button", class: "btn btn--primary btn--sm", onclick: () => { closeWizard(); state.tab = "events"; state.view = "agenda"; state.anchor = startOfDay(new Date()); refreshAll(false); } }, "See my agenda"),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: closeWizard }, "Back to calendars"),
      ]),
    ]));
  }

  /* Step 3 updates its result area in place so typing in the box never loses focus. */
  function refreshWizardControls() {
    const w = state.wizard;
    if (!w || w.step !== 3) return;
    const result = $("#cal-wiz-result");
    const nameRow = $("#cal-wiz-namerow");
    const checkBtn = $("#cal-wiz-check");
    if (!result || !nameRow || !checkBtn) return;
    checkBtn.disabled = w.checking || !w.url.trim();
    checkBtn.textContent = w.checking ? "Checking…" : "Check link";
    result.textContent = "";
    nameRow.textContent = "";
    if (w.error) result.append(el("div", { class: "cal-bad" }, w.error));
    if (w.check && w.check.ok) {
      const c = w.check;
      result.append(el("div", { class: "cal-good" }, [
        el("strong", {}, "That works. "),
        `Found ${c.events_found} event${c.events_found === 1 ? "" : "s"}` +
        (c.next_event ? `; the next one is “${c.next_event.title}” on ${whenLabel({ start: c.next_event.start, end: c.next_event.start, all_day: false })}.` : ", none coming up."),
        c.insecure ? el("div", { class: "cal-bad" }, "This link starts with http://, which isn't encrypted. Use the https:// version if the provider offers one.") : null,
      ]));
      const nameInput = el("input", {
        type: "text", class: "cal-input", maxlength: "40", value: w.name, "aria-label": "Calendar name",
        oninput: (e) => { w.name = e.target.value; },
        onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); runLink(); } },
      });
      nameRow.append(
        el("label", { class: "cal-label" }, "Name it"),
        el("div", { class: "cal-input-row" }, [
          nameInput,
          el("button", { type: "button", class: "btn btn--primary btn--sm", disabled: w.linking ? true : null, onclick: runLink }, w.linking ? "Linking…" : "Link calendar"),
        ]),
      );
    }
  }

  async function runCheck() {
    const w = state.wizard;
    if (!w || w.checking || !w.url.trim()) return;
    w.checking = true; w.error = ""; w.check = null;
    refreshWizardControls();
    try {
      const data = await Api.post("/api/calendar/check", { url: w.url });
      if (state.wizard !== w) return;
      w.check = data;
    } catch (err) {
      if (state.wizard !== w) return;
      w.error = err.message || "Couldn't check that link.";
    }
    w.checking = false;
    refreshWizardControls();
  }

  async function runLink() {
    const w = state.wizard;
    if (!w || w.linking) return;
    if (!String(w.name || "").trim()) { w.error = "Give the calendar a name."; refreshWizardControls(); return; }
    w.linking = true; w.error = "";
    refreshWizardControls();
    try {
      const data = await Api.post("/api/calendar/link", { name: w.name, url: w.url });
      if (state.wizard !== w) return;
      w.url = ""; // the real link is no longer needed on this page
      w.done = data;
      w.step = 4;
      await loadFeeds();
    } catch (err) {
      if (state.wizard !== w) return;
      w.error = err.message || "Couldn't link that calendar.";
    }
    w.linking = false;
    if (state.wizard === w && w.step === 4) renderAll(); else refreshWizardControls();
  }

  /* ---- actions ---------------------------------------------------------------------------- */

  function setView(view) {
    if (state.view === view) return;
    state.view = view;
    try { global.localStorage && global.localStorage.setItem("jarvis.calendar.view", view); } catch { /* optional */ }
    loadEvents(false);
  }
  function step(dir) {
    state.anchor = stepAnchor(state.view, state.anchor, dir);
    state.selectedId = null;
    loadEvents(false);
  }
  function goToday() {
    state.anchor = startOfDay(new Date());
    state.selectedDay = ymd(new Date());
    state.selectedId = null;
    loadEvents(false);
  }
  function pickDay(key) {
    state.selectedDay = key;
    state.selectedId = null;
    renderMain();
    renderSide();
  }
  function selectEvent(id) {
    state.selectedId = id;
    const ev = id ? eventById(id) : null;
    if (ev) state.selectedDay = ymd(parseLocal(ev.start));
    renderMain();
    renderSide();
  }

  async function remindMe(ev) {
    const plan = reminderPlan(ev, state.leadMinutes, new Date());
    if (!plan.ok) { toast(plan.error, "warn"); return; }
    try {
      await Api.post("/api/scheduled", { when: plan.when, message: plan.message, kind: "reminder" });
      state.reminded.add(`${ev.id}|${ev.start}|${ev.all_day ? "allday" : state.leadMinutes}`);
      toast(`Reminder set for ${DAY_NAMES[plan.fire.getDay()]} ${plan.fire.getDate()} ${MONTH_NAMES[plan.fire.getMonth()]} at ${hhmm(plan.fire)}. See it in Menu → Scheduled.`, "info");
      renderSide();
    } catch (err) {
      toast(err.message || "Couldn't set that reminder.", "error");
    }
  }

  async function deleteEvent(ev) {
    let ok = true;
    if (UI() && UI().confirm) {
      ok = await UI().confirm({
        title: "Delete this event?",
        body: `“${ev.title}” will be removed from Jarvis's calendar. This can't be undone.`,
        confirmLabel: "Delete", level: "danger", focusCancel: true,
      });
    }
    if (!ok) return;
    try {
      await Api.del(`/api/calendar/event/${encodeURIComponent(ev.id)}`);
      state.selectedId = null;
      toast("Event deleted.", "info");
      await refreshAll(false);
    } catch (err) {
      toast(err.message || "Couldn't delete that event.", "error");
    }
  }

  async function toggleFeed(feed) {
    try {
      await Api.post("/api/calendar/feed/enable", { name: feed.name, enabled: !feed.enabled });
      await refreshAll(false);
    } catch (err) {
      toast(err.message || "Couldn't change that calendar.", "error");
    }
  }

  async function removeFeed(feed) {
    let ok = true;
    if (UI() && UI().confirm) {
      ok = await UI().confirm({
        title: `Remove “${feed.name}”?`,
        body: "Jarvis stops showing this calendar. The calendar itself is not touched, and you can link it again later with its link.",
        confirmLabel: "Remove", level: "danger", focusCancel: true,
      });
    }
    if (!ok) return;
    try {
      await Api.post("/api/calendar/feed/remove", { name: feed.name });
      await refreshAll(false);
    } catch (err) {
      toast(err.message || "Couldn't remove that calendar.", "error");
    }
  }

  /* ---- the event form (Jarvis's own events only) ---------------------------------------------- */

  function openForm(ev, dateKey) {
    const base = ev ? formFromEvent(ev) : {
      title: "", date: dateKey || state.selectedDay || ymd(new Date()), all_day: false,
      time: "09:00", duration: 60, location: "", description: "",
    };
    state.form = { id: ev ? ev.id : null, values: base, error: "", saving: false };
    renderLayer();
    const first = $("#cal-form-title");
    if (first) first.focus();
  }
  function closeForm() {
    state.form = null;
    renderLayer();
  }

  function renderLayer() {
    if (!dom) return;
    dom.layer.textContent = "";
    dom.layer.hidden = !state.form;
    if (!state.form) return;
    const f = state.form;
    const v = f.values;
    const field = (label, control, hint) => el("label", { class: "cal-field" }, [
      el("span", { class: "cal-label" }, label), control, hint ? el("span", { class: "cal-hint" }, hint) : null,
    ]);
    const timeRow = el("div", { class: "cal-form__times" }, [
      field("Starts", el("input", { type: "time", class: "cal-input", value: v.time, id: "cal-form-time", disabled: v.all_day ? true : null, oninput: (e) => { v.time = e.target.value; } })),
      field("Lasts (minutes)", el("input", { type: "number", class: "cal-input", min: "5", max: "1440", step: "5", value: String(v.duration), id: "cal-form-duration", disabled: v.all_day ? true : null, oninput: (e) => { v.duration = e.target.value; } })),
    ]);
    const err = el("div", { class: "cal-bad", id: "cal-form-error", hidden: f.error ? null : true }, f.error);
    dom.layer.append(el("div", { class: "cal-modal", role: "dialog", "aria-modal": "true", "aria-label": f.id ? "Edit event" : "Add event" }, [
      el("div", { class: "cal-modal__head" }, [
        el("h3", { class: "cal-h" }, f.id ? "Edit event" : "Add event"),
        el("button", { type: "button", class: "modal__close", "aria-label": "Close", onclick: closeForm }, "×"),
      ]),
      el("p", { class: "cal-muted" }, "This goes in Jarvis's own calendar. Linked calendars are read-only, so it won't appear in Google or Outlook."),
      field("Title", el("input", { type: "text", class: "cal-input", id: "cal-form-title", maxlength: "200", value: v.title, oninput: (e) => { v.title = e.target.value; }, onkeydown: formEnter })),
      el("div", { class: "cal-form__times" }, [
        field("Date", el("input", { type: "date", class: "cal-input", id: "cal-form-date", value: v.date, oninput: (e) => { v.date = e.target.value; } })),
        el("label", { class: "cal-check" }, [
          el("input", {
            type: "checkbox", id: "cal-form-allday", checked: v.all_day ? true : null,
            onchange: (e) => {
              v.all_day = e.target.checked;
              $("#cal-form-time").disabled = v.all_day;
              $("#cal-form-duration").disabled = v.all_day;
            },
          }),
          el("span", {}, "All day"),
        ]),
      ]),
      timeRow,
      field("Where", el("input", { type: "text", class: "cal-input", maxlength: "200", value: v.location, oninput: (e) => { v.location = e.target.value; }, onkeydown: formEnter })),
      field("Notes", el("textarea", { class: "cal-input cal-textarea", maxlength: "500", rows: "3", oninput: (e) => { v.description = e.target.value; } }, v.description)),
      err,
      el("div", { class: "cal-wizard__nav" }, [
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: closeForm }, "Cancel"),
        el("button", { type: "button", class: "btn btn--primary btn--sm", id: "cal-form-save", onclick: saveForm }, f.id ? "Save changes" : "Add event"),
      ]),
    ]));
  }
  function formEnter(e) { if (e.key === "Enter") { e.preventDefault(); saveForm(); } }

  async function saveForm() {
    const f = state.form;
    if (!f || f.saving) return;
    const built = eventPayload(f.values);
    const errBox = $("#cal-form-error");
    if (!built.ok) {
      f.error = built.error;
      if (errBox) { errBox.textContent = built.error; errBox.hidden = false; }
      return;
    }
    f.saving = true;
    const btn = $("#cal-form-save");
    if (btn) btn.disabled = true;
    try {
      const data = f.id
        ? await Api.put(`/api/calendar/event/${encodeURIComponent(f.id)}`, { event: built.event })
        : await Api.post("/api/calendar/event", { event: built.event });
      state.form = null;
      const saved = data && data.event;
      if (saved) {
        const day = parseLocal(saved.start);
        // Jump to the saved event's day so it is on screen, and keep it selected.
        if (day) { state.anchor = state.view === "agenda" ? startOfDay(day) : day; state.selectedDay = ymd(day); }
        state.selectedId = saved.id;
      }
      await refreshAll(false);
      toast(f.id ? "Event saved." : "Event added to Jarvis's calendar.", "info");
    } catch (err) {
      f.saving = false;
      f.error = err.message || "Couldn't save that event.";
      if (errBox) { errBox.textContent = f.error; errBox.hidden = false; }
      if (btn) btn.disabled = false;
    }
  }

  /* ---- tabs, keyboard, open/close ----------------------------------------------------------------- */

  function setTab(tab) {
    state.tab = tab;
    if (tab === "events") state.wizard = null;
    renderAll();
  }

  function anyConfirmOpen() { return Boolean(document.querySelector(".jui-backdrop")); }

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    if (anyConfirmOpen()) return; // a confirm dialog owns Escape and Enter
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") {
      if (state.form) { closeForm(); return; }
      if (state.wizard) { closeWizard(); return; }
      if (state.selectedId) { selectEvent(null); return; }
      close();
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || state.form || state.tab !== "events") return;
    if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (e.key === "t" || e.key === "T") { e.preventDefault(); goToday(); }
    else if (e.key === "a" || e.key === "A") { e.preventDefault(); setView("agenda"); }
    else if (e.key === "w" || e.key === "W") { e.preventDefault(); setView("week"); }
    else if (e.key === "m" || e.key === "M") { e.preventDefault(); setView("month"); }
    else if (e.key === "n" || e.key === "N") { e.preventDefault(); openForm(null); }
    else if (e.key === "r" || e.key === "R") { e.preventDefault(); refreshAll(true); }
  }

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;
    // Coming back to the tab (or the window) is the moment a panel left open went stale.
    document.addEventListener("visibilitychange", () => { if (!document.hidden) pollEvents(); });
    window.addEventListener("focus", () => { pollEvents(); });
    dom.tabEvents.addEventListener("click", () => setTab("events"));
    dom.tabCalendars.addEventListener("click", () => setTab("calendars"));
    $("#calendar-close")?.addEventListener("click", close);
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);
    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) {
      toast("Calendars markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    state.selectedDay = state.selectedDay || ymd(new Date());
    rollDay();
    renderAll();
    refreshAll(false);
    startPolling();
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    dom.overlay.hidden = true;
    stopPolling();
    state.form = null;
    state.wizard = null; // never leave a pasted link sitting in a closed panel
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  function isOpen() { return Boolean(dom && !dom.overlay.hidden); }

  global.JarvisCalendar = { open, close, isOpen, _pure: pure };
})(window);
