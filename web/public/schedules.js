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

  const CHANNELS = [
    ["inbox", "Inbox"], ["stream", "Stream"], ["toast", "Toast"], ["voice", "Voice"],
    ["playnite", "Playnite"], ["discord", "Discord"], ["instagram", "Instagram"],
  ];
  const LEVELS = [
    ["", "Default"], ["1", "1 \u00b7 Silent"], ["2", "2 \u00b7 Standard"],
    ["3", "3 \u00b7 Persistent"], ["4", "4 \u00b7 Broadcast"], ["5", "5 \u00b7 Confirm"],
  ];
  const KIND_CHIPS = [["all", "All"], ["reminder", "Reminder"], ["notify", "Notify"], ["task", "Task"]];
  const STATUS_CHIPS = [["all", "All statuses"], ["needs_approval", "Needs approval"], ["paused", "Paused"], ["error", "Failed"], ["over_budget", "Over budget"]];
  // Cosmetic only, not a real scheduler.py kind — see the module comment
  // above and tool_schedule_watch's own docstring ("mechanically also
  // schedule_task with action={type: ask}"). A hand-authored ask job that
  // happens to start with "Watch: " would also match; that's an accepted,
  // low-stakes false positive for a display label, not a filter.
  const WATCH_TITLE_RE = /^Watch: /;

  const state = {
    built: false,
    jobs: [], counts: {}, events: [], lastTick: null,
    search: "", kindFilter: "all", statusFilter: "all",
    mode: "new",            // "new" | "detail"
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

  function matches(job) {
    if (state.kindFilter !== "all" && job.kind !== state.kindFilter) return false;
    if (state.statusFilter === "needs_approval" && !job.needs_approval) return false;
    if (state.statusFilter === "paused" && job.status !== "paused") return false;
    if (state.statusFilter === "error" && job.status !== "error") return false;
    if (state.statusFilter === "over_budget" && job.status !== "over_budget") return false;
    const q = state.search.trim().toLowerCase();
    if (!q) return true;
    return [job.title, job.kind, job.when, job.action].filter(Boolean).join("\n").toLowerCase().includes(q);
  }
  function visibleJobs() { return state.jobs.filter(matches); }

  /* ---- left: chips + list ---------------------------------------------------- */

  function renderChips() {
    if (!dom) return;
    dom.kindChips.textContent = "";
    KIND_CHIPS.forEach(([id, label]) => {
      const n = id === "all" ? state.jobs.length
        : id === "task" ? (state.counts.tasks || 0)
        : id === "reminder" ? (state.counts.reminders || 0)
        : (state.counts.notifications || 0);
      dom.kindChips.appendChild(el("button", {
        class: "sch-chip" + (state.kindFilter === id ? " is-active" : ""), type: "button",
        onclick: () => { state.kindFilter = id; renderChips(); renderList(); },
      }, [label, el("span", { class: "sch-chip__n" }, String(n))]));
    });
    dom.statusChips.textContent = "";
    STATUS_CHIPS.forEach(([id, label]) => {
      const n = id === "all" ? state.jobs.length
        : id === "needs_approval" ? (state.counts.needs_approval || 0)
        : id === "paused" ? state.jobs.filter((j) => j.status === "paused").length
        : id === "over_budget" ? state.jobs.filter((j) => j.status === "over_budget").length
        : state.jobs.filter((j) => j.status === "error").length;
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
        job.status === "over_budget" ? el("span", { class: "sch-tag sch-tag--bad" }, "stopped: over budget")
          : job.last_error ? el("span", { class: "sch-tag sch-tag--bad" }, "error") : null,
      ].filter(Boolean)),
    ]);
  }

  function renderList() {
    if (!dom) return;
    const rows = visibleJobs();
    dom.count.textContent = (state.search.trim() || state.kindFilter !== "all" || state.statusFilter !== "all")
      ? `${rows.length} of ${state.jobs.length}` : String(state.jobs.length);
    dom.list.textContent = "";
    if (!rows.length) {
      dom.list.appendChild(el("div", { class: "skills-empty" }, state.jobs.length ? "Nothing matches these filters." : "Nothing scheduled yet."));
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

  function channelsField(idPrefix) {
    return el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label" }, "Channels (leave all unchecked to use your configured defaults)"),
      el("div", { class: "sch-channels" }, CHANNELS.map(([id, label]) =>
        el("label", { class: "sch-channel-toggle" }, [
          el("input", { type: "checkbox", id: `${idPrefix}-ch-${id}`, "data-channel": id }),
          label,
        ]))),
    ]);
  }

  function collectChannels(idPrefix) {
    const on = CHANNELS.map(([id]) => id).filter((id) => $(`#${idPrefix}-ch-${id}`)?.checked);
    return on.length ? on : undefined;
  }

  function levelField(idPrefix) {
    return el("div", { class: "sch-field" }, [
      el("label", { class: "sch-field__label", for: `${idPrefix}-level` }, "Importance"),
      el("select", { id: `${idPrefix}-level` }, LEVELS.map(([v, label]) => el("option", { value: v }, label))),
    ]);
  }

  function renderNewForm() {
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
    if (!lines.length) return true;
    const body = lines.join("\n") +
      "\n\nApproving lets it do this on its own, with nobody there to confirm, " +
      "every time it runs.";
    const UI = global.JarvisUI;
    if (UI && UI.confirm) {
      return UI.confirm({ title: "Approve this job?", body, pre: (job.action || {}).prompt || "",
                          confirmLabel: "Approve", level: "warn", focusCancel: true });
    }
    return global.confirm(body);
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
    row("Channels", (job.channels || []).join(", ") || "(configured defaults)");
    row("Level", job.level != null ? job.level : "(configured default)");
    row("Repeat", job.max_runs ? `${job.run_count || 0} / ${job.max_runs}` : (job.run_count ? `${job.run_count} run(s)` : "not yet run"));
    row("Catch up", job.catch_up ? "yes" : null);
    row("Emits on done", job.emit_on_done);
    row("Created", job.created_at);
    row("Last run", job.last_run);

    dom.mid.textContent = "";
    dom.mid.appendChild(dl);
    const risk = riskLines(job);
    if (risk.length) {
      dom.mid.appendChild(el("div", { class: "sch-detail__risk" },
        [el("strong", null, job.needs_approval ? "Approving this allows:" : "This job:")]
          .concat(risk.map((line) => el("div", null, line)))));
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
    renderList();
    renderMid();
  }

  function renderMid() {
    if (state.mode === "detail" && state.selectedId) {
      const job = state.jobs.find((j) => j.id === state.selectedId);
      if (job) { selectJob(state.selectedId); return; }
    }
    renderNewForm();
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
    const failed = state.jobs.filter((j) => j.status === "error").length;
    const overBudget = state.jobs.filter((j) => j.status === "over_budget").length;
    dom.side.appendChild(el("div", null, [
      el("h4", null, "Needs attention"),
      el("div", { class: "sch-side-counts" }, [
        ["Needs approval", c.needs_approval || 0], ["Paused", paused], ["Failed", failed], ["Over budget", overBudget],
      ].map(([label, n]) => el("div", { class: "sch-side-count-row" + (n ? " is-flag" : "") }, [
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
      state.counts = data.counts || {};
      state.events = data.events || [];
      state.lastTick = data.last_tick || null;
      dom.statusLine.textContent = state.jobs.length
        ? `${state.jobs.length} active \u2014 ${state.counts.reminders || 0} reminder(s), ${state.counts.tasks || 0} task(s)` +
          (state.counts.needs_approval ? `, ${state.counts.needs_approval} awaiting approval` : "")
        : "nothing scheduled";
      renderChips();
      renderList();
      renderSide();
      if (state.mode === "detail" && state.selectedId) {
        // Re-pull the open job's own detail too, so an action taken
        // elsewhere (e.g. the websocket-driven tick refresh) doesn't leave
        // a stale status/next_run showing in an already-open detail pane.
        if (state.jobs.some((j) => j.id === state.selectedId)) selectJob(state.selectedId);
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

  global.JarvisSchedules = { open, close, isOpen, refresh };
})(window);
