/* ============================================================================
 * schedules.js — Menu → Scheduled.
 *
 * Part H.2 rework, escalated to the full Test-Checklist-quality bar (see the
 * master plan's H.2 — the old add row was flagged as missing whole
 * categories of interaction, not just unpolished).
 *
 * Deliberately standalone, the same way daemons.js/backlog.js/logsearch.js
 * are: its own tiny $/el/api/toast helpers, no reach into app.js's
 * internals. Talks to /api/scheduled/* the same way app.js used to, plus
 * two additions this pass needed:
 *
 *   - GET /api/scheduled/:id (new, server.js) — the list endpoint only ever
 *     returned scheduler.summarize() shapes (title/kind/status/when/next_run/
 *     action TYPE), never enough to show a task's actual prompt/command/tool
 *     or its channels/level/trigger detail. Read-only, same id validation as
 *     the existing per-job action route.
 *   - The EXISTING /api/tools/run endpoint (already used by the Debug
 *     panel to run any AI tool directly) is how job CREATION now exposes the
 *     full field set scheduler.create() already accepts: remind_me/
 *     notify_me/schedule_task/schedule_watch, called directly with real
 *     arguments, instead of the old sched-add CLI path which is deliberately
 *     minimal (when/message/kind only — see cli.py's own comment on why).
 *     Nothing new was added server-side for this half; scheduler.create()'s
 *     own untrusted-by-default approval gate (_needs_approval) still applies
 *     exactly as it would from an ask, so a task/command/tool job created
 *     here still lands in needs_approval when it should — this form is not
 *     a way around that gate, just a way to reach the same tools without
 *     going through a conversation.
 *
 * WHAT CHANGED FROM THE OLD INLINE VERSION
 * --------------------------------------------------------------------------
 *  - Own file, loaded after the panel's own markup (see index.html's
 *    "Scripts load LAST" comment). The panel's markup itself moved from the
 *    smaller .menu-overlay/.menu-panel chrome to .debug-overlay/.debug-panel
 *    (see index.html's own comment on that).
 *  - A real create form: a kind selector (Reminder/Notify/Task/Watch, the
 *    last a friendlier front end onto schedule_watch exactly the way
 *    scheduler_tools.py's own module docstring describes it), the task
 *    prompt / command+args / tool+args fields the old when+message-only row
 *    had no room for, and an Advanced section (channels, importance level,
 *    repeat count, and for Task/Watch: catch-up, emit-on-done, report).
 *  - The list is grouped/filterable by kind and by status (needs approval /
 *    paused / failed), with a live search box — none of that existed before.
 *  - Every row shows its own next-run/recurrence text and is visually
 *    marked when paused/needs-approval/errored, rather than only readable by
 *    opening each one.
 *  - An Overview pane: counts per kind, a paused/failed roll-up, a "next up"
 *    list, and the recent-events/last-tick data scheduler.overview() has
 *    always returned but the old panel never read past `.jobs`/`.counts`.
 *  - Keyboard: "/" focuses search, Up/Down moves between jobs, "n" opens the
 *    new-job form, Escape clears search first (if there's something to
 *    clear) then closes.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ---- tiny helpers, same shape as the other H.1/H.2 modules ------------- */

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
    else console.error(message);
  }
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
  const Api = { get: (p) => api("GET", p), post: (p, b) => api("POST", p, b) };

  function timeAgo(iso) {
    if (!iso) return "";
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return iso;
    const s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  // The notification types and importance levels are NOT listed here: they are the
  // notification module's (notify_registry.py), read through JarvisNotifications'
  // pickerActions() / pickerLevels() (the catalog at /api/notifications/catalog). A type or
  // level added there appears in these forms with no edit to this file. With no catalog the
  // pickers disable themselves and say so.
  //
  // E2 / D-10: the native OS notification is `desktop`; jobs saved earlier say `toast`.
  const legacyChannel = (c) => (c === "toast" ? "desktop" : c);
  const notifications = () => window.JarvisNotifications || null;
  const pickerOf = (name) => {
    const N = notifications();
    return N && typeof N[name] === "function" ? N[name]() : { items: [], message: "The notification options could not be loaded. Reload the page to try again." };
  };
  // What a stored type id is called on screen (the catalog's own label; the id if unknown).
  const channelLabel = (id) => {
    const hit = pickerOf("pickerActions").items.find((a) => a.id === legacyChannel(id));
    return hit ? hit.label : String(id);
  };
  const KIND_CHIPS = [["all", "All"], ["reminder", "Reminder"], ["notify", "Notify"], ["task", "Task"]];
  // Active view: what is still going to happen. Finished view (H.2 history):
  // what already did, or was stopped. "Failed" and "Over budget" live in the
  // Finished view because that is where those jobs are -- the active list never
  // contains them (scheduler.ACTIVE_STATUSES), so chips for them there could
  // only ever read 0.
  const STATUS_CHIPS = [["all", "All statuses"], ["needs_approval", "Needs approval"], ["paused", "Paused"]];
  const FINISHED_STATUS_CHIPS = [["all", "All finished"], ["done", "Done"], ["error", "Failed"], ["over_budget", "Over budget"], ["cancelled", "Cancelled"]];
  // Cosmetic only, not a real scheduler.py kind — see the module comment
  // above and tool_schedule_watch's own docstring ("mechanically also
  // schedule_task with action={type: ask}"). A hand-authored ask job that
  // happens to start with "Watch: " would also match; that's an accepted,
  // low-stakes false positive for a display label, not a filter.
  const WATCH_TITLE_RE = /^Watch: /;

  const state = {
    built: false,
    jobs: [], finished: [], counts: {}, events: [], lastTick: null,
    view: "active",         // "active" | "finished"
    search: "", kindFilter: "all", statusFilter: "all",
    mode: "new",            // "new" | "detail" | "edit"
    editJob: null,          // the full job being edited (mode "edit")
    selectedId: null,
    newKind: "reminder",    // reminder | notify | task | watch
    taskDo: "ask",          // ask | command | tool
    watchChecks: [{ if_screen_shows: "", then: "" }],
    toolNames: null, commandNames: null, catalogueLoading: false,
    prevFocus: null,
  };

  let dom = null;
  function grabDom() {
    const overlay = $("#sched-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#sched-status-line"),
      search: $("#sch-search"),
      kindChips: $("#sch-kind-chips"),
      viewTabs: null,   // built in ensureBuilt()
      statusChips: $("#sch-status-chips"),
      count: $("#sch-count"),
      list: $("#sch-list"),
      midTitle: $("#sch-mid-title"),
      mid: $("#sch-mid"),
      side: $("#sch-side"),
      btnNew: $("#btn-sch-new"),
      btnTick: $("#btn-sched-tick"),
      btnRefresh: $("#btn-sched-refresh"),
    };
  }

  /* ---- filtering ------------------------------------------------------------ */

  // The list the left pane is showing: still-scheduled jobs, or finished history.
  function currentList() { return state.view === "finished" ? state.finished : state.jobs; }
  function findJob(id) {
    return state.jobs.find((j) => j.id === id) || state.finished.find((j) => j.id === id) || null;
  }

  function matches(job) {
    if (state.kindFilter !== "all" && job.kind !== state.kindFilter) return false;
    if (state.statusFilter === "needs_approval") { if (!job.needs_approval) return false; }
    else if (state.statusFilter !== "all" && job.status !== state.statusFilter) return false;
    const q = state.search.trim().toLowerCase();
    if (!q) return true;
    return [job.title, job.kind, job.when, job.action, job.last_result].filter(Boolean).join("\n").toLowerCase().includes(q);
  }
  function visibleJobs() { return currentList().filter(matches); }

  function setView(view) {
    if (state.view === view) return;
    state.view = view;
    state.statusFilter = "all";
    renderChips();
    renderList();
  }

  /* ---- Finished tab: clearing and deleting (pure text helpers) ---------------- */

  const FINISHED_STATUSES = ["done", "cancelled", "error", "over_budget"];
  function isFinished(job) { return Boolean(job) && FINISHED_STATUSES.includes(job.status); }

  // How many finished jobs there are: the server's own count (the list in the
  // overview is capped at the newest 200, the count is not), else the list.
  function finishedTotal() {
    return state.counts.finished != null ? state.counts.finished : state.finished.length;
  }

  // What "Clear finished" is about to remove, for its confirmation: the number and
  // the split by outcome, from the overview's counts. Plain data in, plain text out.
  function clearFinishedText(counts, total) {
    const c = counts && typeof counts === "object" ? counts : {};
    const n = Number.isFinite(total) ? total : (c.finished || 0);
    const split = [[c.done, "done"], [c.failed, "failed"], [c.stopped_over_budget, "over budget"], [c.cancelled, "cancelled"]]
      .filter(([k]) => k > 0).map(([k, label]) => `${k} ${label}`);
    const what = `${n} finished job${n === 1 ? "" : "s"}`;
    return {
      n,
      label: n === 1 ? "Clear finished (1)" : `Clear finished (${n})`,
      body: `This permanently removes all ${what}${split.length ? ` (${split.join(", ")})` : ""} from the history, whatever the filters above show.\n\n` +
        "Jobs that are still scheduled are not touched. The conversations their runs wrote and the logs stay.",
    };
  }

  // The confirmation for deleting ONE finished job.
  function deleteJobText(job) {
    const name = (job && job.title) || "this job";
    return `Delete \u201c${name}\u201d from the history? This can\u2019t be undone.\n\n` +
      "Only the job\u2019s record is removed. The conversation its runs wrote and the logs stay.";
  }

  // The approval card's capability warning (scheduler.preflight_of, sent by `sched-show` for a
  // job waiting on approval): the sentences to show, or [] when there is nothing to warn about.
  // Advice only -- approving is never blocked by it.
  function preflightLines(job) {
    const pf = job && job.preflight;
    if (!job || job.status !== "needs_approval" || !pf || pf.ok !== false) return [];
    return (Array.isArray(pf.lines) ? pf.lines : []).map((l) => String(l == null ? "" : l).trim()).filter(Boolean);
  }
  const PREFLIGHT_NOTE = "You can still approve it. When it is due nothing will run and no model will be called, " +
    "and you will get a \u201cCouldn\u2019t start\u201d notice; a one-off job then shows as failed, a repeating job tries again next time.";

  /* ---- left: chips + list ---------------------------------------------------- */

  function renderChips() {
    if (!dom) return;
    if (dom.viewTabs) {
      dom.viewTabs.textContent = "";
      [["active", "Active", state.jobs.length], ["finished", "Finished", state.counts.finished != null ? state.counts.finished : state.finished.length]]
        .forEach(([id, label, n]) => {
          dom.viewTabs.appendChild(el("button", {
            class: "sch-chip sch-viewtab" + (state.view === id ? " is-active" : ""), type: "button",
            "aria-pressed": state.view === id ? "true" : "false",
            onclick: () => setView(id),
          }, [label, el("span", { class: "sch-chip__n" }, String(n))]));
        });
    }
    if (dom.clearBar) {
      // Only on the Finished tab, and only when there is something to clear.
      const n = finishedTotal();
      dom.clearBar.hidden = !(state.view === "finished" && n > 0);
      if (dom.btnClear) dom.btnClear.textContent = clearFinishedText(state.counts, n).label;
    }
    const list = currentList();
    dom.kindChips.textContent = "";
    KIND_CHIPS.forEach(([id, label]) => {
      const n = id === "all" ? list.length : list.filter((j) => j.kind === id).length;
      dom.kindChips.appendChild(el("button", {
        class: "sch-chip" + (state.kindFilter === id ? " is-active" : ""), type: "button",
        onclick: () => { state.kindFilter = id; renderChips(); renderList(); },
      }, [label, el("span", { class: "sch-chip__n" }, String(n))]));
    });
    dom.statusChips.textContent = "";
    (state.view === "finished" ? FINISHED_STATUS_CHIPS : STATUS_CHIPS).forEach(([id, label]) => {
      const n = id === "all" ? list.length
        : id === "needs_approval" ? list.filter((j) => j.needs_approval).length
        : list.filter((j) => j.status === id).length;
      dom.statusChips.appendChild(el("button", {
        class: "sch-chip" + (state.statusFilter === id ? " is-active" : ""), type: "button",
        onclick: () => { state.statusFilter = id; renderChips(); renderList(); },
      }, [label, el("span", { class: "sch-chip__n" }, String(n))]));
    });
  }

  function rowTag(job) {
    if (job.kind !== "task" || !job.action) return null;
    const label = job.action === "ask" && WATCH_TITLE_RE.test(job.title || "") ? "watch" : job.action;
    return el("span", { class: "sch-tag" }, label);
  }

  function renderRow(job) {
    const dotStatus = job.needs_approval ? "needs_approval" : job.status;
    const rowClasses = ["sch-row"];
    if (job.id === state.selectedId) rowClasses.push("is-active");
    if (job.needs_approval) rowClasses.push("is-needs-approval");
    if (job.status === "paused") rowClasses.push("is-paused");
    if (job.status === "error") rowClasses.push("is-error");
    if (job.status === "over_budget") rowClasses.push("is-over-budget");
    if (job.status === "done") rowClasses.push("is-done");
    if (job.status === "cancelled") rowClasses.push("is-cancelled");
    return el("button", { class: rowClasses.join(" "), type: "button", onclick: () => selectJob(job.id) }, [
      el("div", { class: "sch-row__top" }, [
        el("span", { class: `sch-dot sch-dot--${dotStatus}` }),
        el("span", { class: "sch-row__title" }, job.title || "(untitled)"),
      ]),
      el("div", { class: "sch-row__meta" }, [
        el("span", { class: "sch-tag" }, job.kind),
        rowTag(job),
        job.when ? el("span", null, job.when) : null,
        job.in ? el("span", null, `in ${job.in}`) : null,
        state.view === "finished" && job.finished_at ? el("span", { title: job.finished_at }, `finished ${timeAgo(job.finished_at)}`) : null,
        job.status === "over_budget" ? el("span", { class: "sch-tag sch-tag--bad" }, "stopped: over budget")
          : job.last_error ? el("span", { class: "sch-tag sch-tag--bad" }, "error") : null,
      ].filter(Boolean)),
    ]);
  }

  function renderList() {
    if (!dom) return;
    const rows = visibleJobs();
    const total = currentList().length;
    dom.count.textContent = (state.search.trim() || state.kindFilter !== "all" || state.statusFilter !== "all")
      ? `${rows.length} of ${total}` : String(total);
    dom.list.textContent = "";
    if (!rows.length) {
      dom.list.appendChild(el("div", { class: "skills-empty" }, total ? "Nothing matches these filters."
        : state.view === "finished" ? "Nothing has finished yet. Reminders and tasks that have gone off, failed or been cancelled are kept here."
        : "Nothing scheduled yet."));
      return;
    }
    rows.forEach((job) => dom.list.appendChild(renderRow(job)));
  }

  /* ---- middle: new-job form -------------------------------------------------- */

  async function ensureCatalogue() {
    if (state.toolNames || state.catalogueLoading) return;
    state.catalogueLoading = true;
    try {
      const [tools, commands] = await Promise.all([
        Api.get("/api/tools").catch(() => []),
        Api.get("/api/commands").catch(() => ({})),
      ]);
      state.toolNames = (Array.isArray(tools) ? tools : []).map((t) => t.name).filter(Boolean);
      state.commandNames = Object.keys(commands || {});
    } finally {
      state.catalogueLoading = false;
      if (state.mode === "new" && state.newKind === "task") renderMid();
    }
  }

  // The notification-type ticks. Built from the catalog and rebuilt when it changes
  // (`jarvis:notify-catalog`), keeping what is ticked. `initial` (an edit form) is what the
  // job already has; it is applied the first time the boxes exist, which may be a moment
  // after the form opens if the catalog is still loading.
  function channelsField(idPrefix, initial) {
    const box = el("div", { class: "sch-channels", id: `${idPrefix}-channels` });
    const hint = el("div", { class: "sch-field__help" });
    let want = new Set((initial || []).map(legacyChannel));
    let built = false;
    const fill = () => {
      if (!box.isConnected && built) { window.removeEventListener("jarvis:notify-catalog", fill); return; }
      if (built) want = new Set(Array.from(box.querySelectorAll("input[data-channel]")).filter((i) => i.checked).map((i) => i.dataset.channel));
      const { items, message } = pickerOf("pickerActions");
      box.textContent = "";
      built = false;
      if (!items.length) {
        box.appendChild(el("div", { class: "sch-field__help", "data-disabled": "1" }, message));
        hint.textContent = "";
        return;
      }
      items.forEach((a) => {
        box.appendChild(el("label", { class: "sch-channel-toggle", title: a.description || null }, [
          el("input", { type: "checkbox", id: `${idPrefix}-ch-${a.id}`, "data-channel": a.id,
                        checked: want.has(a.id) ? true : null }),
          a.label,
        ]));
      });
      built = true;
    };
    const N = notifications();
    const setHint = () => { hint.textContent = N && typeof N.pickerHint === "function" ? N.pickerHint() : ""; };
    const fillAll = () => { fill(); setHint(); };
    window.addEventListener("jarvis:notify-catalog", fillAll);
    fillAll();
    return el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label" }, "Notify through (leave all unchecked to use your configured defaults)"),
      box, hint,
    ]);
  }

  // The ticked type ids, or undefined for none. `null` means the picker could not be read
  // (no boxes), which a caller must treat as "leave the job's channels alone", not "clear".
  function collectChannels(idPrefix) {
    const box = $(`#${idPrefix}-channels`);
    if (!box || !box.querySelector("input[data-channel]")) return null;
    const on = Array.from(box.querySelectorAll("input[data-channel]")).filter((i) => i.checked).map((i) => i.dataset.channel);
    return on.length ? on : undefined;
  }

  // The importance select: "Default" plus every level in the catalog. `current` (an edit form)
  // is the job's level; applied whenever the options are (re)built.
  function levelField(idPrefix, current) {
    const sel = el("select", { id: `${idPrefix}-level` });
    const msg = el("div", { class: "sch-field__help" });
    let chosen = current != null ? String(current) : "";
    const fill = () => {
      if (!sel.isConnected && sel.options.length) { window.removeEventListener("jarvis:notify-catalog", fill); return; }
      if (sel.options.length > 1) chosen = sel.value;
      const { items, message } = pickerOf("pickerLevels");
      sel.textContent = "";
      sel.appendChild(el("option", { value: "" }, "Default"));
      items.forEach((l) => sel.appendChild(el("option", { value: String(l.level), title: l.description || null }, l.label)));
      sel.disabled = !items.length;
      sel.dataset.ready = items.length ? "1" : "";
      sel.value = items.some((l) => String(l.level) === chosen) ? chosen : "";
      msg.textContent = items.length ? "" : message;
    };
    window.addEventListener("jarvis:notify-catalog", fill);
    fill();
    return el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label", for: `${idPrefix}-level` }, "Importance"), sel, msg,
    ]);
  }

  function renderNewForm() {
    // Refetch the notification catalog each time the form opens (no timer): if the table
    // changed since the page loaded, the pickers redraw through `jarvis:notify-catalog`.
    if (notifications() && notifications().loadCatalog) notifications().loadCatalog();
    dom.midTitle.textContent = "New job";
    const wrap = el("div", { class: "sch-form" });

    wrap.appendChild(el("div", { class: "sch-kindbar" }, ["reminder", "notify", "task", "watch"].map((k) =>
      el("button", {
        type: "button", class: "sch-kindbar-btn" + (state.newKind === k ? " is-active" : ""),
        onclick: () => { state.newKind = k; renderMid(); },
      }, k.charAt(0).toUpperCase() + k.slice(1)))));

    const when = el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label", for: "sch-f-when" }, state.newKind === "notify" ? "When (leave blank to send right now)" : "When"),
      el("input", { type: "text", id: "sch-f-when", placeholder: "tomorrow at 9am, every weekday at 08:30, in 20 minutes\u2026" }),
    ]);

    if (state.newKind === "reminder") {
      wrap.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label", for: "sch-f-message" }, "Message"),
        el("input", { type: "text", id: "sch-f-message", placeholder: "what to say" }),
      ]));
      wrap.appendChild(when);
    } else if (state.newKind === "notify") {
      wrap.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label", for: "sch-f-message" }, "Message"),
        el("input", { type: "text", id: "sch-f-message", placeholder: "what to say" }),
      ]));
      wrap.appendChild(when);
      wrap.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label", for: "sch-f-title" }, "Title (optional)"),
        el("input", { type: "text", id: "sch-f-title" }),
      ]));
    } else if (state.newKind === "task") {
      wrap.appendChild(when);
      wrap.appendChild(el("div", { class: "sch-kindbar" }, ["ask", "command", "tool"].map((d) =>
        el("button", {
          type: "button", class: "sch-kindbar-btn" + (state.taskDo === d ? " is-active" : ""),
          onclick: () => { state.taskDo = d; renderMid(); },
        }, d.charAt(0).toUpperCase() + d.slice(1)))));
      if (state.taskDo === "ask") {
        wrap.appendChild(el("div", { class: "sch-field" }, [
          el("label", { class: "sch-field__label", for: "sch-f-prompt" }, "Prompt \u2014 what should Jarvis do?"),
          el("textarea", { id: "sch-f-prompt", placeholder: "e.g. Summarize today's calendar and notify me." }),
        ]));
      } else if (state.taskDo === "command") {
        ensureCatalogue();
        wrap.appendChild(el("div", { class: "sch-field" }, [
          el("label", { class: "sch-field__label", for: "sch-f-command" }, "Saved command name"),
          el("input", { type: "text", id: "sch-f-command", list: "sch-command-list" }),
          el("datalist", { id: "sch-command-list" }, (state.commandNames || []).map((n) => el("option", { value: n }))),
        ]));
        wrap.appendChild(el("div", { class: "sch-field" }, [
          el("label", { class: "sch-field__label", for: "sch-f-args" }, "Arguments (JSON object, optional)"),
          el("textarea", { id: "sch-f-args", placeholder: "{}" }),
        ]));
      } else {
        ensureCatalogue();
        wrap.appendChild(el("div", { class: "sch-field" }, [
          el("label", { class: "sch-field__label", for: "sch-f-tool" }, "Tool name"),
          el("input", { type: "text", id: "sch-f-tool", list: "sch-tool-list" }),
          el("datalist", { id: "sch-tool-list" }, (state.toolNames || []).map((n) => el("option", { value: n }))),
        ]));
        wrap.appendChild(el("div", { class: "sch-field" }, [
          el("label", { class: "sch-field__label", for: "sch-f-args" }, "Arguments (JSON object, optional)"),
          el("textarea", { id: "sch-f-args", placeholder: "{}" }),
        ]));
      }
    } else { // watch
      wrap.appendChild(when);
      wrap.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label" }, "If the screen shows\u2026 then\u2026 (checked in order, first match wins)"),
        el("div", { class: "sch-checks", id: "sch-checks" }, state.watchChecks.map((c, i) => renderCheckRow(c, i))),
        el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.watchChecks.push({ if_screen_shows: "", then: "" }); renderMid(); } }, "+ Add condition"),
      ]));
      wrap.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label", for: "sch-f-otherwise" }, "Otherwise (optional)"),
        el("input", { type: "text", id: "sch-f-otherwise", placeholder: "Do nothing." }),
      ]));
    }

    const advanced = el("details", { class: "sch-advanced" }, [
      el("summary", null, "Advanced options"),
    ]);
    if (state.newKind !== "reminder") {
      advanced.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label", for: "sch-f-title2" }, "Title override (optional)"),
        el("input", { type: "text", id: "sch-f-title2" }),
      ]));
    }
    advanced.appendChild(channelsField("sch-f"));
    advanced.appendChild(levelField("sch-f"));
    advanced.appendChild(el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label", for: "sch-f-times" }, "Repeat this many times (blank = forever / once, per its own recurrence)"),
      el("input", { type: "number", id: "sch-f-times", min: "1" }),
    ]));
    if (state.newKind === "task" || state.newKind === "watch") {
      advanced.appendChild(el("label", { class: "sch-check-toggle" }, [el("input", { type: "checkbox", id: "sch-f-catchup" }), "Catch up on missed runs instead of skipping them"]));
      advanced.appendChild(el("label", { class: "sch-check-toggle" }, [el("input", { type: "checkbox", id: "sch-f-report", checked: true }), "Report back when it's done"]));
      advanced.appendChild(el("div", { class: "sch-field" }, [
        el("label", { class: "sch-field__label", for: "sch-f-emit" }, "Announce this event when done (optional)"),
        el("input", { type: "text", id: "sch-f-emit", placeholder: "e.g. backup_done" }),
      ]));
    }
    wrap.appendChild(advanced);

    wrap.appendChild(el("div", { class: "sch-submit-row" }, [
      el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: submitNewJob }, "Create job"),
    ]));

    dom.mid.textContent = "";
    dom.mid.appendChild(wrap);
  }

  function renderCheckRow(check, i) {
    return el("div", { class: "sch-check-row" }, [
      el("div", { class: "sch-check-row__head" }, [
        el("span", { class: "sch-check-row__label" }, `Condition ${i + 1}`),
        state.watchChecks.length > 1
          ? el("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => { state.watchChecks.splice(i, 1); renderMid(); } }, "\u00d7")
          : null,
      ].filter(Boolean)),
      el("input", { type: "text", placeholder: "if the screen shows\u2026", value: check.if_screen_shows, oninput: (e) => { check.if_screen_shows = e.target.value; } }),
      el("input", { type: "text", placeholder: "then\u2026", value: check.then, oninput: (e) => { check.then = e.target.value; } }),
    ]);
  }

  function readArgsJson() {
    const raw = ($("#sch-f-args")?.value || "").trim();
    if (!raw) return {};
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    } catch (_) { /* fall through */ }
    return undefined; // signals "invalid" to the caller
  }

  async function submitNewJob() {
    const when = ($("#sch-f-when")?.value || "").trim();
    const kind = state.newKind;
    let toolName, args = {};

    const level = $("#sch-f-level")?.value;
    const times = parseInt($("#sch-f-times")?.value, 10);
    const channels = collectChannels("sch-f");
    if (level) args.level = parseInt(level, 10);
    if (channels) args.channels = channels;
    if (Number.isFinite(times) && times > 0) args.times = times;

    if (kind === "reminder") {
      const message = ($("#sch-f-message")?.value || "").trim();
      if (!message) return toast("What should the reminder say?");
      if (!when) return toast("When should I remind you?");
      toolName = "remind_me";
      Object.assign(args, { message, when });
    } else if (kind === "notify") {
      const message = ($("#sch-f-message")?.value || "").trim();
      if (!message) return toast("What should the notification say?");
      toolName = "notify_me";
      Object.assign(args, { message, when: when || undefined, title: ($("#sch-f-title")?.value || "").trim() || undefined });
    } else if (kind === "task") {
      if (!when) return toast("When should this run?");
      toolName = "schedule_task";
      Object.assign(args, { when, do: state.taskDo, report: Boolean($("#sch-f-report")?.checked) });
      if (state.taskDo === "ask") {
        const prompt = ($("#sch-f-prompt")?.value || "").trim();
        if (!prompt) return toast("What should Jarvis do at that time?");
        args.prompt = prompt;
      } else if (state.taskDo === "command") {
        const command = ($("#sch-f-command")?.value || "").trim();
        if (!command) return toast("Which saved command should run?");
        const parsedArgs = readArgsJson();
        if (parsedArgs === undefined) return toast("Arguments must be a valid JSON object.");
        Object.assign(args, { command, args: parsedArgs });
      } else {
        const tool = ($("#sch-f-tool")?.value || "").trim();
        if (!tool) return toast("Which tool should run?");
        const parsedArgs = readArgsJson();
        if (parsedArgs === undefined) return toast("Arguments must be a valid JSON object.");
        Object.assign(args, { tool, args: parsedArgs });
      }
      const title2 = ($("#sch-f-title2")?.value || "").trim();
      if (title2) args.title = title2;
      if ($("#sch-f-catchup")?.checked) args.catch_up = true;
      const emit = ($("#sch-f-emit")?.value || "").trim();
      if (emit) args.emit_on_done = emit;
    } else { // watch
      if (!when) return toast("When should this run?");
      const checks = state.watchChecks
        .map((c) => ({ if_screen_shows: c.if_screen_shows.trim(), then: c.then.trim() }))
        .filter((c) => c.if_screen_shows && c.then);
      if (!checks.length) return toast("Give at least one condition with both fields filled in.");
      toolName = "schedule_watch";
      Object.assign(args, { when, checks, otherwise: ($("#sch-f-otherwise")?.value || "").trim() || undefined, report: Boolean($("#sch-f-report")?.checked) });
      const title2 = ($("#sch-f-title2")?.value || "").trim();
      if (title2) args.title = title2;
      if ($("#sch-f-catchup")?.checked) args.catch_up = true;
      const emit = ($("#sch-f-emit")?.value || "").trim();
      if (emit) args.emit_on_done = emit;
    }

    try {
      const data = await Api.post("/api/tools/run", { name: toolName, arguments: args });
      const result = data.result || {};
      if (result.needs_clarification) { toast(result.message || "Missing something."); return; }
      if (result.error) { toast(result.error); return; }
      if (!result.ok) { toast("Couldn't create that job."); return; }
      toast(result.note || (result.needs_approval ? `Created \u2014 waiting on your approval.` : "Scheduled."), "info");
      // Reset the free-text fields but keep the kind selected, so adding
      // several reminders/tasks in a row doesn't mean re-picking the kind
      // every time.
      state.watchChecks = [{ if_screen_shows: "", then: "" }];
      renderMid();
      refresh();
    } catch (err) {
      toast(err.message || "Couldn't create that job.");
    }
  }

  /* ---- middle: job detail ------------------------------------------------- */

  // What approving this job authorises, as sentences. Computed server-side from
  // the job's TOOL LIST (jarvis/job_risk.py), never from its prompt text, so the
  // wording cannot be steered by what the prompt says. Empty when the job
  // contains nothing flagged.
  function riskLines(job) {
    return ((job && job.risk && job.risk.lines) || []).filter(Boolean);
  }

  // Approval is the authorisation: once approved the job runs unattended with
  // no further confirm. So a job with flagged actions is approved through a
  // dialog that says what it contains; a plain job is approved in one click,
  // as before.
  async function confirmApprove(job) {
    const lines = riskLines(job);
    const warn = preflightLines(job);
    if (!lines.length && !warn.length) return true;
    const parts = [];
    if (lines.length) {
      parts.push(lines.join("\n") +
        "\n\nApproving lets it do this on its own, with nobody there to confirm, " +
        "every time it runs.");
    }
    // The capability warning (L.16 preflight at approval time): it would not start
    // right now. Asked about even when nothing risky is in the job, since a plain
    // one-click approval would otherwise approve a job that is known not to run.
    if (warn.length) parts.push("It would not start right now:\n" + warn.join("\n") + "\n\n" + PREFLIGHT_NOTE);
    const body = parts.join("\n\n");
    const UI = global.JarvisUI;
    if (UI && UI.confirm) {
      return UI.confirm({ title: "Approve this job?", body, pre: (job.action || {}).prompt || "",
                          confirmLabel: "Approve", level: "warn", focusCancel: true });
    }
    return global.confirm(body);
  }

  async function confirmText(title, body, confirmLabel) {
    const UI = global.JarvisUI;
    if (UI && UI.confirm) return UI.confirm({ title, body, confirmLabel, level: "warn", focusCancel: true });
    return global.confirm(body);
  }

  function deleteButton(job) {
    const b = el("button", { class: "btn btn--danger btn--sm", type: "button" }, "Delete");
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        if (!(await confirmText("Delete this job?", deleteJobText(job), "Delete"))) { b.disabled = false; return; }
        await Api.post(`/api/scheduled/${encodeURIComponent(job.id)}/delete`, {});
        toast("Deleted from the history.", "info");
        // Nothing to show for it any more: back to the form, then re-read the lists.
        if (state.selectedId === job.id) newJobMode();
        refresh();
      } catch (err) {
        toast(err.message || "That didn't work.");
        b.disabled = false;
      }
    });
    return b;
  }

  async function clearFinished() {
    const n = finishedTotal();
    if (!n || !dom || !dom.btnClear) return;
    const t = clearFinishedText(state.counts, n);
    dom.btnClear.disabled = true;
    try {
      if (!(await confirmText("Clear finished jobs?", t.body, "Clear finished"))) return;
      const res = await Api.post("/api/scheduled/clear", {});
      const removed = res && Number.isFinite(res.removed) ? res.removed : 0;
      toast(removed ? `Removed ${removed} finished job${removed === 1 ? "" : "s"}.` : "Nothing to clear.", "info");
      // The open job may have just been removed; refresh() drops back to the form
      // when the selected job is no longer in either list.
      refresh();
    } catch (err) {
      toast(err.message || "That didn't work.");
    } finally {
      dom.btnClear.disabled = false;
    }
  }

  function actionButtons(job) {
    const buttons = [];
    const act = (label, action, body, guard) => {
      const b = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, label);
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          if (guard && !(await guard(job))) { b.disabled = false; return; }
          await Api.post(`/api/scheduled/${job.id}/${action}`, body || {});
          refresh();
          selectJob(job.id);
        } catch (err) {
          toast(err.message || "That didn't work.");
          b.disabled = false;
        }
      });
      buttons.push(b);
    };
    if (job.status === "needs_approval") act("Approve", "approve", undefined, confirmApprove);
    if (job.status === "paused") act("Resume", "resume");
    else if (job.status === "over_budget") act("Resume", "resume");
    else if (job.status !== "needs_approval" && job.status !== "done" && job.status !== "cancelled") act("Pause", "pause");
    if (job.status !== "done" && job.status !== "cancelled") act("Snooze 10m", "snooze", { delay: "10 minutes" });
    if (job.status !== "cancelled") act("Cancel", "cancel");
    // Finished history only: an active job is cancelled first (the server refuses to
    // delete one that can still fire). After a delete there is no job to show, so
    // this does not go through act(), which re-selects the job it just acted on.
    if (isFinished(job)) buttons.push(deleteButton(job));
    // H.2: editing changes what the job IS, so it only makes sense while there is
    // still a job to change. Done and cancelled jobs are history.
    if (job.status !== "done" && job.status !== "cancelled") {
      buttons.unshift(el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => startEdit(job) }, "Edit"));
    }
    return buttons;
  }

  function actionDetail(job) {
    const a = job.action || {};
    if (a.type === "ask") return el("div", { class: "sch-detail__prompt" }, a.prompt || "");
    if (a.type === "command") return el("div", { class: "sch-detail__prompt" }, `${a.command}${Object.keys(a.args || {}).length ? "\n" + JSON.stringify(a.args, null, 2) : ""}`);
    if (a.type === "tool") return el("div", { class: "sch-detail__prompt" }, `${a.tool}${Object.keys(a.args || {}).length ? "\n" + JSON.stringify(a.args, null, 2) : ""}`);
    if (a.type === "notify") return el("div", { class: "sch-detail__prompt" }, a.message || "");
    return null;
  }

  /* Per-job token limit (L.16 item 8). Only an `ask` job spends model tokens.
     Default = the config's 30,000; "Custom" overrides it for this job; "No limit"
     means this job is never stopped for cost. Saved immediately, applies from
     the job's next run. */
  function tokenLimitControl(job) {
    if ((job.action || "") !== "ask" && (job.action || {}).type !== "ask") return null;
    const cur = job.token_budget;
    const mode = cur === null || cur === undefined ? "default" : cur === 0 ? "off" : "custom";
    const select = el("select", { class: "sch-input", "aria-label": "Token limit" }, [
      el("option", { value: "default" }, "Default (30,000 tokens)"),
      el("option", { value: "custom" }, "Custom limit\u2026"),
      el("option", { value: "off" }, "No limit \u2014 never stop this job for cost"),
    ]);
    select.value = mode;
    const number = el("input", { class: "sch-input", type: "number", min: "1000", step: "1000",
      value: mode === "custom" ? String(cur) : "30000", "aria-label": "Custom token limit" });
    number.style.display = mode === "custom" ? "" : "none";
    const save = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, "Save limit");
    select.addEventListener("change", () => { number.style.display = select.value === "custom" ? "" : "none"; });
    save.addEventListener("click", async () => {
      const limit = select.value === "custom" ? String(parseInt(number.value, 10) || "") : select.value;
      if (!limit) { toast("Enter a number of tokens."); return; }
      save.disabled = true;
      try {
        await Api.post(`/api/scheduled/${job.id}/budget`, { limit });
        toast(select.value === "off" ? "This job will no longer be stopped for cost." : "Limit saved.", "info");
        refresh();
        selectJob(job.id);
      } catch (err) {
        toast(err.message || "That didn't work.");
        save.disabled = false;
      }
    });
    return el("div", { class: "sch-detail__budget" }, [
      el("strong", null, "Token limit per run"),
      el("div", { class: "sch-detail__budget-row" }, [select, number, save]),
      el("div", { class: "sch-detail__hint" },
        "A scheduled run that goes over its limit is stopped, reported, and parked as \u201cstopped: over budget\u201d."),
    ]);
  }

  function renderDetailFor(job) {
    dom.midTitle.textContent = job.title || "(untitled)";
    const isWatch = job.kind === "task" && (job.action || {}).type === "ask" && WATCH_TITLE_RE.test(job.title || "");
    const dl = el("dl", null);
    const row = (label, value) => value !== undefined && value !== null && value !== "" && dl.appendChild(el("div", { class: "sch-detail__row" }, [el("dt", null, label), el("dd", null, String(value))]));
    row("Kind", isWatch ? "task (watch)" : job.kind);
    row("Status", job.status === "over_budget" ? "stopped: over budget"
      : job.status + (job.needs_approval ? " \u2014 needs approval" : ""));
    row("Next run", job.next_run ? `${job.next_run}${job.in ? ` (in ${job.in})` : ""}` : "\u2014");
    row("Action", (job.action || {}).type);
    row("Channels", (job.channels || []).map(channelLabel).join(", ") || "(configured defaults)");
    row("Level", job.level != null ? job.level : "(configured default)");
    row("Repeat", job.max_runs ? `${job.run_count || 0} / ${job.max_runs}` : (job.run_count ? `${job.run_count} run(s)` : "not yet run"));
    row("Catch up", job.catch_up ? "yes" : null);
    row("Emits on done", job.emit_on_done);
    row("Created", job.created_at);
    row("Last run", job.last_run);
    row("Finished", ["done", "cancelled", "error", "over_budget"].includes(job.status) ? (job.finished_at || job.last_run) : null);

    dom.mid.textContent = "";
    dom.mid.appendChild(dl);
    const risk = riskLines(job);
    if (risk.length) {
      dom.mid.appendChild(el("div", { class: "sch-detail__risk" },
        [el("strong", null, job.needs_approval ? "Approving this allows:" : "This job:")]
          .concat(risk.map((line) => el("div", null, line)))));
    }
    // Capability warning on the approval card (L.16 preflight, asked now instead of only
    // when the job runs): advice, so it sits beside the risk lines and blocks nothing.
    const warn = preflightLines(job);
    if (warn.length) {
      dom.mid.appendChild(el("div", { class: "sch-detail__preflight" },
        [el("strong", null, "Heads up \u2014 this would not start right now:")]
          .concat(warn.map((line) => el("div", null, line)))
          .concat([el("div", { class: "sch-detail__hint" }, PREFLIGHT_NOTE)])));
    }
    const detail = actionDetail(job);
    if (detail) dom.mid.appendChild(detail);
    if (job.status === "over_budget") {
      dom.mid.appendChild(el("div", { class: "sch-detail__error" },
        "Stopped: over budget. The last run used more tokens than this job's limit, so it was ended and the job is parked. " +
        "Raise its limit or turn the limit off below, then Resume."));
    } else if (job.last_error) {
      dom.mid.appendChild(el("div", { class: "sch-detail__error" }, `Last error: ${job.last_error}`));
    }
    if (job.last_result && ["done", "cancelled", "error", "over_budget"].includes(job.status)) {
      dom.mid.appendChild(el("div", { class: "sch-detail__result" }, [
        el("strong", null, "Last result"),
        el("div", { class: "sch-detail__prompt" }, String(job.last_result)),
      ]));
    }
    const budgetControl = tokenLimitControl(job);
    if (budgetControl) dom.mid.appendChild(budgetControl);
    if (job.conv_id && global.JarvisAsk) {
      dom.mid.appendChild(el("div", { class: "sch-detail__actions" }, [
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => { close(); global.JarvisAsk.openConversation(job.conv_id); } }, "Open conversation"),
      ]));
    }
    dom.mid.appendChild(el("div", { class: "sch-detail__actions" }, actionButtons(job)));
  }

  async function selectJob(id) {
    state.mode = "detail";
    state.editJob = null;
    state.selectedId = id;
    renderList();
    dom.midTitle.textContent = "Loading\u2026";
    dom.mid.textContent = "";
    try {
      const job = await Api.get(`/api/scheduled/${encodeURIComponent(id)}`);
      if (job.error) { dom.mid.appendChild(el("div", { class: "skills-empty" }, job.error)); return; }
      if (state.selectedId === id) renderDetailFor(job);
    } catch (err) {
      dom.mid.appendChild(el("div", { class: "skills-empty" }, err.message || "Couldn't load that job."));
    }
  }

  function newJobMode() {
    state.mode = "new";
    state.selectedId = null;
    state.editJob = null;
    renderList();
    renderMid();
  }

  function renderMid() {
    if (state.mode === "edit" && state.editJob) { renderEditForm(state.editJob); return; }
    if (state.mode === "detail" && state.selectedId) {
      if (findJob(state.selectedId)) { selectJob(state.selectedId); return; }
    }
    renderNewForm();
  }

  /* ---- middle: edit an existing job (H.2) ----------------------------------- */

  function startEdit(job) {
    state.mode = "edit";
    state.selectedId = job.id;
    state.editJob = job;
    renderList();
    renderEditForm(job);
  }

  function sameJson(a, b) { return JSON.stringify(a == null ? null : a) === JSON.stringify(b == null ? null : b); }

  function renderEditForm(job) {
    if (notifications() && notifications().loadCatalog) notifications().loadCatalog();
    const a = job.action || {};
    const summary = findJob(job.id) || {};
    dom.midTitle.textContent = `Edit: ${job.title || "(untitled)"}`;
    const wrap = el("div", { class: "sch-form" });
    const field = (label, id, control) => el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label", for: id }, label), control]);

    wrap.appendChild(field("Title", "sch-e-title", el("input", { type: "text", id: "sch-e-title", value: job.title || "" })));
    wrap.appendChild(field(`When \u2014 leave blank to keep the current schedule${summary.when ? ` (${summary.when})` : ""}`,
      "sch-e-when", el("input", { type: "text", id: "sch-e-when", placeholder: "tomorrow at 9am, every weekday at 08:30, in 20 minutes\u2026" })));

    if (a.type === "notify") {
      wrap.appendChild(field("Message", "sch-e-message", el("input", { type: "text", id: "sch-e-message", value: a.message || "" })));
    } else if (a.type === "ask") {
      const ta = el("textarea", { id: "sch-e-prompt" });
      ta.value = a.prompt || "";
      wrap.appendChild(field("Prompt \u2014 what should Jarvis do?", "sch-e-prompt", ta));
    } else if (a.type === "command") {
      wrap.appendChild(field("Saved command name", "sch-e-command", el("input", { type: "text", id: "sch-e-command", value: a.command || "" })));
    } else if (a.type === "tool") {
      wrap.appendChild(field("Tool name", "sch-e-tool", el("input", { type: "text", id: "sch-e-tool", value: a.tool || "" })));
    }
    if (a.type === "command" || a.type === "tool") {
      const ta = el("textarea", { id: "sch-e-args", placeholder: "{}" });
      ta.value = Object.keys(a.args || {}).length ? JSON.stringify(a.args, null, 2) : "";
      wrap.appendChild(field("Arguments (JSON object, optional)", "sch-e-args", ta));
    }
    if (a.type && a.type !== "notify") {
      wrap.appendChild(el("div", { class: "sch-detail__hint" },
        "Changing what this job runs may send it back to \u201cneeds approval\u201d: it won\u2019t run again until you approve what it now does."));
    }

    const advanced = el("details", { class: "sch-advanced" }, [el("summary", null, "More options")]);
    const kindSel = el("select", { id: "sch-e-kind" }, ["reminder", "notify", "task"].map((k) =>
      el("option", { value: k }, k.charAt(0).toUpperCase() + k.slice(1))));
    kindSel.value = job.kind || "task";
    advanced.appendChild(field("Kind (a label: which list it shows in, and its defaults)", "sch-e-kind", kindSel));
    // Built from the catalog; the job's own channels / level are applied as the options exist.
    advanced.appendChild(channelsField("sch-e", job.channels));
    advanced.appendChild(levelField("sch-e", job.level));
    advanced.appendChild(field("Repeat this many times (blank = no limit)", "sch-e-times",
      el("input", { type: "number", id: "sch-e-times", min: "1", value: job.max_runs || "" })));
    advanced.appendChild(el("label", { class: "sch-check-toggle" }, [
      el("input", { type: "checkbox", id: "sch-e-catchup", checked: job.catch_up ? true : null }),
      "Catch up on missed runs instead of skipping them"]));
    if (a.type && a.type !== "notify") {
      advanced.appendChild(el("label", { class: "sch-check-toggle" }, [
        el("input", { type: "checkbox", id: "sch-e-report", checked: a.report === false ? null : true }),
        "Report back when it's done"]));
    }
    advanced.appendChild(field("Announce this event when done (optional)", "sch-e-emit",
      el("input", { type: "text", id: "sch-e-emit", value: job.emit_on_done || "" })));
    wrap.appendChild(advanced);

    wrap.appendChild(el("div", { class: "sch-submit-row" }, [
      el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => submitEdit(job) }, "Save changes"),
      el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => { state.mode = "detail"; state.editJob = null; selectJob(job.id); } }, "Cancel"),
    ]));
    dom.mid.textContent = "";
    dom.mid.appendChild(wrap);
  }

  // Send ONLY what differs from the job as loaded, so saving an untouched form
  // changes nothing (and a field the form can't represent is never clobbered).
  async function submitEdit(job) {
    const a = job.action || {};
    const val = (id) => ($(`#${id}`)?.value || "").trim();
    const changes = {};
    if (val("sch-e-title") !== (job.title || "")) {
      if (!val("sch-e-title")) return toast("The title can't be empty.");
      changes.title = val("sch-e-title");
    }
    if (val("sch-e-when")) changes.when = val("sch-e-when");
    for (const [field, id, current] of [["message", "sch-e-message", a.message], ["prompt", "sch-e-prompt", a.prompt],
                                        ["command", "sch-e-command", a.command], ["tool", "sch-e-tool", a.tool]]) {
      if (!$(`#${id}`)) continue;
      if (!val(id)) return toast(`The ${field} can't be empty.`);
      if (val(id) !== (current || "").trim()) changes[field] = val(id);
    }
    if ($("#sch-e-args")) {
      const raw = val("sch-e-args");
      let parsed = {};
      if (raw) {
        try { parsed = JSON.parse(raw); } catch (_) { return toast("Arguments must be a valid JSON object."); }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return toast("Arguments must be a valid JSON object.");
      }
      if (!sameJson(parsed, a.args || {})) changes.args = parsed;
    }
    const kind = val("sch-e-kind");
    if (kind && kind !== job.kind) changes.kind = kind;
    // If the catalog could not be read the pickers are disabled: leave the job's channels and
    // level exactly as they are rather than clearing them.
    const picked = collectChannels("sch-e");          // null = picker unreadable
    if (picked !== null) {
      const channels = picked || [];
      const oldChannels = (job.channels || []).map(legacyChannel).sort();
      if (!sameJson(channels.slice().sort(), oldChannels)) changes.channels = channels.length ? channels : null;
    }
    if ($("#sch-e-level")?.dataset.ready) {
      const level = val("sch-e-level");
      if ((level === "" ? null : parseInt(level, 10)) !== (job.level == null ? null : job.level)) changes.level = level === "" ? null : parseInt(level, 10);
    }
    const times = val("sch-e-times");
    const timesNow = times === "" ? null : parseInt(times, 10);
    if (times !== "" && !(timesNow >= 1)) return toast("Repeat count must be a whole number of 1 or more.");
    if (timesNow !== (job.max_runs || null)) changes.max_runs = timesNow;
    const catchUp = Boolean($("#sch-e-catchup")?.checked);
    if (catchUp !== Boolean(job.catch_up)) changes.catch_up = catchUp;
    if ($("#sch-e-report")) {
      const report = Boolean($("#sch-e-report").checked);
      if (report !== (a.report !== false)) changes.report = report;
    }
    if (val("sch-e-emit") !== (job.emit_on_done || "")) changes.emit_on_done = val("sch-e-emit") || null;

    if (!Object.keys(changes).length) return toast("Nothing changed.", "info");
    try {
      const data = await Api.post(`/api/scheduled/${encodeURIComponent(job.id)}/edit`, { changes });
      toast((data && data.note) || "Saved.", "info");
      state.mode = "detail";
      state.editJob = null;
      await refresh();
      selectJob(job.id);
    } catch (err) {
      toast(err.message || "Couldn't save that.");
    }
  }

  /* ---- right: Overview pane ------------------------------------------------- */

  function renderSide() {
    if (!dom || !dom.side) return;
    dom.side.textContent = "";
    const c = state.counts || {};
    dom.side.appendChild(el("div", null, [
      el("h4", null, "By kind"),
      el("div", { class: "sch-side-counts" }, [
        ["Reminders", c.reminders || 0], ["Notifications", c.notifications || 0], ["Tasks", c.tasks || 0],
      ].map(([label, n]) => el("div", { class: "sch-side-count-row" }, [
        el("span", { class: "sch-side-count-row__label" }, label), el("span", { class: "sch-side-count-row__n" }, String(n)),
      ]))),
    ]));

    const paused = state.jobs.filter((j) => j.status === "paused").length;
    const failed = c.failed != null ? c.failed : state.finished.filter((j) => j.status === "error").length;
    const overBudget = c.stopped_over_budget != null ? c.stopped_over_budget : state.finished.filter((j) => j.status === "over_budget").length;
    dom.side.appendChild(el("div", null, [
      el("h4", null, "Needs attention"),
      el("div", { class: "sch-side-counts" }, [
        ["Needs approval", c.needs_approval || 0], ["Paused", paused], ["Failed", failed], ["Over budget", overBudget],
      ].map(([label, n]) => el("div", { class: "sch-side-count-row" + (n ? " is-flag" : "") }, [
        el("span", { class: "sch-side-count-row__label" }, label), el("span", { class: "sch-side-count-row__n" }, String(n)),
      ]))),
    ]));

    dom.side.appendChild(el("div", null, [
      el("h4", null, "History"),
      el("div", { class: "sch-side-counts" }, [
        ["Finished", c.finished || 0], ["Done", c.done || 0], ["Cancelled", c.cancelled || 0],
      ].map(([label, n]) => el("div", { class: "sch-side-count-row" }, [
        el("span", { class: "sch-side-count-row__label" }, label), el("span", { class: "sch-side-count-row__n" }, String(n)),
      ]))),
    ]));

    const nextUp = state.jobs
      .filter((j) => j.next_run && j.status !== "done" && j.status !== "cancelled")
      .slice().sort((a, b) => (a.next_run < b.next_run ? -1 : 1))
      .slice(0, 5);
    dom.side.appendChild(el("div", null, [
      el("h4", null, "Next up"),
      nextUp.length
        ? el("div", { class: "sch-nextup" }, nextUp.map((j) => el("button", {
            class: "sch-nextup-row", type: "button", onclick: () => selectJob(j.id),
          }, [
            el("div", { class: "sch-nextup-row__title" }, j.title || "(untitled)"),
            el("div", { class: "sch-nextup-row__in" }, j.in ? `in ${j.in}` : j.when),
          ])))
        : el("div", { class: "sch-side-empty" }, "Nothing upcoming."),
    ]));

    const events = state.events || [];
    dom.side.appendChild(el("div", null, [
      el("h4", null, "Recent signals"),
      events.length
        ? el("div", { class: "sch-events" }, events.slice(0, 6).map((e) => el("div", null, `${e.event} \u2014 ${timeAgo(e.at)}`)))
        : el("div", { class: "sch-events-empty" }, "None yet."),
      state.lastTick ? el("div", { class: "sch-last-tick" }, `Last tick: ${timeAgo(state.lastTick)}`) : null,
    ].filter(Boolean)));
  }

  /* ---- fetch ---------------------------------------------------------------- */

  async function refresh() {
    if (!dom || dom.overlay.hidden) return;
    try {
      const data = await Api.get("/api/scheduled");
      state.jobs = data.jobs || [];
      state.finished = data.finished || [];
      state.counts = data.counts || {};
      state.events = data.events || [];
      state.lastTick = data.last_tick || null;
      const finishedN = state.counts.finished != null ? state.counts.finished : state.finished.length;
      dom.statusLine.textContent = (state.jobs.length
        ? `${state.jobs.length} active \u2014 ${state.counts.reminders || 0} reminder(s), ${state.counts.tasks || 0} task(s)` +
          (state.counts.needs_approval ? `, ${state.counts.needs_approval} awaiting approval` : "")
        : "nothing scheduled") + (finishedN ? ` \u00b7 ${finishedN} finished` : "");
      renderChips();
      renderList();
      renderSide();
      // An open edit form is left alone: a tick-driven refresh must not wipe
      // what someone is in the middle of typing.
      if (state.mode === "detail" && state.selectedId) {
        // Re-pull the open job's own detail too, so an action taken
        // elsewhere (e.g. the websocket-driven tick refresh) doesn't leave
        // a stale status/next_run showing in an already-open detail pane.
        // A job that just finished moves lists; follow it to the other tab's
        // data rather than dropping back to the form.
        if (findJob(state.selectedId)) selectJob(state.selectedId);
        else newJobMode();
      }
    } catch (err) {
      dom.statusLine.textContent = "couldn't read the schedule";
      dom.list.textContent = "";
      dom.list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  /* ---- keyboard --------------------------------------------------------------- */

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") {
      if (t === dom.search && dom.search.value) { state.search = ""; dom.search.value = ""; renderList(); return; }
      close();
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    if (e.key === "n" || e.key === "N") { e.preventDefault(); newJobMode(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const rows = visibleJobs();
      if (!rows.length) return;
      e.preventDefault();
      const i = rows.findIndex((r) => r.id === state.selectedId);
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next) selectJob(next.id);
    }
  }

  /* ---- wiring, open/close ----------------------------------------------------- */

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;

    // Active / Finished tabs (H.2 history), built here so index.html is untouched.
    dom.viewTabs = el("div", { class: "sch-chips sch-viewtabs", role: "group", "aria-label": "Active or finished jobs" });
    dom.kindChips.parentNode.insertBefore(dom.viewTabs, dom.kindChips);
    // "Clear finished" lives under the tabs and shows only on the Finished tab (renderChips).
    dom.btnClear = el("button", { class: "btn btn--danger btn--sm", type: "button", onclick: clearFinished }, "Clear finished");
    dom.clearBar = el("div", { class: "sch-clearbar", hidden: true }, [dom.btnClear]);
    dom.kindChips.parentNode.insertBefore(dom.clearBar, dom.kindChips);

    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderList(); });
    dom.btnNew.addEventListener("click", newJobMode);
    dom.btnRefresh.addEventListener("click", refresh);
    dom.btnTick.addEventListener("click", async () => {
      try {
        const result = await Api.post("/api/scheduled/tick", {});
        const n = (result.ran || []).length;
        toast(n ? `Ran ${n} job(s).` : "Nothing was due.", "info");
        refresh();
      } catch (err) {
        toast(err.message || "Tick failed.");
      }
    });

    $("#sched-close")?.addEventListener("click", close);
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);

    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) {
      toast("Scheduled markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    global.JarvisNotifyPermission?.();
    renderChips();
    renderMid();
    refresh();
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    dom.overlay.hidden = true;
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  function isOpen() { return Boolean(dom && !dom.overlay.hidden); }

  global.JarvisSchedules = {
    open, close, isOpen, refresh,
    _pure: { clearFinishedText, deleteJobText, preflightLines, isFinished, PREFLIGHT_NOTE },
  };
})(window);
