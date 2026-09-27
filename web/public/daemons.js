/* ============================================================================
 * daemons.js — Menu → Daemons.
 *
 * Part H.1 rework. Every long-running background service in one place: the
 * built-ins (scheduler, Discord, Instagram) and anything the user has
 * registered.
 *
 * Deliberately standalone, the same way test-checklist.js is: this file
 * defines its own tiny $/el/api/toast helpers and does not reach into app.js's
 * internals. It talks to JarvisUI (ui-kit.js) directly for toasts, and to
 * /api/daemons/* the same way app.js used to.
 *
 * WHAT CHANGED FROM THE OLD INLINE VERSION (see the master plan's Part H.1)
 * --------------------------------------------------------------------------
 *  - Own file, loaded after the panel's own markup (see index.html's
 *    "Scripts load LAST" comment) rather than living inside app.js.
 *  - The console is now a list of classed lines (stdout / stderr / crash
 *    traceback / meta / stdin-echo) instead of one flat <pre>. The
 *    stdout/stderr split is real: daemons.py's run_supervisor() now pipes
 *    them separately and tags stderr-origin lines with a plain "E: " marker
 *    in console.log (see classifyLine() below) — this file only reads that
 *    tag, it doesn't guess.
 *  - A crash-loop badge on the row itself, from data the backend already had
 *    (status()'s `restarts`, the registry's own `max_restarts`) but nothing
 *    surfaced before.
 *  - Keyboard: Up/Down moves between services, PageUp/PageDown/Home/End page
 *    the console, "r" refreshes, Escape closes.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ---- tiny helpers, same shape as test-checklist.js -------------------- */

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
    else if (level !== "info") global.alert ? null : console.error(message); // last-resort, never silent
  }

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) {
      const message = (data && data.error) || `${method} ${url} failed (${res.status})`;
      throw new Error(message);
    }
    return data;
  }
  const Api = { get: (p) => api("GET", p), post: (p, b) => api("POST", p, b) };

  /* ---- line classification ----------------------------------------------
   * Meta ("=== ...") and stdin-echo ("<<< ...") lines are stamped by
   * daemons.py's _stamp() with a leading "[YYYY-MM-DD HH:MM:SS] ". Real
   * process output never gets that stamp, so it can't collide with these.
   * A stderr-origin line carries a plain "E: " marker (see run_supervisor()'s
   * _pump()); a traceback is a stderr line matching one of the shapes below.
   */
  const STAMP_RE = /^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] /;
  const TRACEBACK_HEAD_RE = /^Traceback \(most recent call last\):/;
  const TRACEBACK_FRAME_RE = /^\s+File "/;
  const TRACEBACK_TAIL_RE = /^[A-Za-z_][\w.]*(Error|Exception|Warning)\b/;

  function classifyLine(raw) {
    if (STAMP_RE.test(raw)) {
      const rest = raw.replace(STAMP_RE, "");
      return { cls: rest.startsWith("<<< ") ? "stdin" : "meta", text: raw };
    }
    if (raw.startsWith("E: ")) {
      const text = raw.slice(3);
      const crash = TRACEBACK_HEAD_RE.test(text) || TRACEBACK_FRAME_RE.test(text) || TRACEBACK_TAIL_RE.test(text);
      return { cls: crash ? "crash" : "stderr", text };
    }
    return { cls: "stdout", text: raw };
  }

  /* ---- state -------------------------------------------------------------*/

  const CONSOLE_FILTERS = [
    { id: "all", label: "All" },
    { id: "output", label: "Output", classes: ["stdout", "meta", "stdin"] },
    { id: "errors", label: "Errors", classes: ["stderr", "crash"] },
  ];

  const state = {
    built: false,
    selected: null,
    focusIndex: -1,     // keyboard row cursor; separate from `selected`
    rows: [],           // last-rendered daemon entries, in list order
    consoleFilter: "all",
    autoscroll: true,
    pollTimer: null,
    prevFocus: null,
  };

  let dom = null;

  function grabDom() {
    const overlay = $("#daemons-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#daemons-status-line"),
      list: $("#daemons-list"),
      consoleTitle: $("#daemon-console-title"),
      consolePane: $("#daemon-console"),
      filePicker: $("#daemon-console-file"),
      toolbar: $("#dmn-console-toolbar"),
      autoscrollBox: $("#dmn-autoscroll"),
      inputRow: $("#daemon-input-row"),
      inputText: $("#daemon-input-text"),
    };
  }

  /* ---- row rendering ------------------------------------------------------*/

  function daemonDotClass(status) {
    if (status === "running") return "daemon-dot daemon-dot--up";
    if (status === "crashed") return "daemon-dot daemon-dot--bad";
    if (status === "scheduled") return "daemon-dot daemon-dot--wait";
    if (status === "stopped") return "daemon-dot daemon-dot--stopped";
    return "daemon-dot";
  }

  function crashLoopBadge(entry) {
    const restarts = entry.restarts || 0;
    if (!restarts) return null;
    const max = entry.max_restarts || 0;
    const maxed = max > 0 && restarts >= max;
    return el("span", {
      class: "dmn-badge " + (maxed ? "dmn-badge--maxed" : "dmn-badge--looping"),
      title: maxed
        ? `Hit its restart limit (${restarts}/${max} in the last 10 min) — not restarting again until it's started manually.`
        : `Restarted ${restarts} time${restarts === 1 ? "" : "s"} in the last 10 min.`,
    }, maxed ? `\u21bb ${restarts}/${max} \u2014 limit hit` : `\u21bb ${restarts} recently`);
  }

  function renderDaemonRow(entry, index) {
    const running = Boolean(entry.running);
    const actions = el("div", { class: "daemon-actions" }, [
      el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: (ev) => { ev.stopPropagation(); daemonAction(entry.id, running ? "stop" : "start"); },
      }, running ? "Stop" : "Start"),
      el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: (ev) => { ev.stopPropagation(); daemonAction(entry.id, "restart"); },
      }, "Restart"),
      el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: (ev) => { ev.stopPropagation(); selectDaemon(entry.id); },
      }, "Console"),
    ]);
    if (!entry.builtin) {
      actions.appendChild(el("button", {
        class: "btn btn--ghost btn--sm",
        onclick: (ev) => { ev.stopPropagation(); removeDaemon(entry.id); },
      }, "Remove"));
    }

    const meta = [];
    if (entry.pid) meta.push(`pid ${entry.pid}`);
    if (entry.adopted) meta.push("started outside Jarvis");
    if (entry.next_start) meta.push(`starts ${entry.next_start}`);
    if (!entry.enabled) meta.push("disabled");
    if (entry.last_error) meta.push(entry.last_error);

    const badge = crashLoopBadge(entry);
    const classes = ["skill-row", "daemon-row"];
    if (state.selected === entry.id) classes.push("is-selected");
    if (state.focusIndex === index) classes.push("is-focused");

    return el("div", {
      class: classes.join(" "),
      "data-daemon-id": entry.id,
      onclick: () => { state.focusIndex = index; selectDaemon(entry.id); },
    }, [
      el("div", { class: "daemon-row__main" }, [
        el("span", { class: daemonDotClass(entry.status) }),
        el("div", { class: "daemon-row__text" }, [
          el("div", { class: "skill-row__name" }, entry.name || entry.id),
          el("div", { class: "skill-row__desc" },
            `${entry.status}${meta.length ? " \u2014 " + meta.join(", ") : ""}`),
          el("div", { class: "daemon-row__cmd" }, entry.command || ""),
          badge ? el("div", { class: "daemon-row__badges" }, [badge]) : null,
        ]),
      ]),
      actions,
    ]);
  }

  async function refreshDaemons() {
    if (!dom || dom.overlay.hidden) return;
    try {
      const data = await Api.get("/api/daemons");
      const entries = data.daemons || [];
      state.rows = entries;
      const up = entries.filter((d) => d.running).length;
      const broken = entries.filter((d) => d.status === "crashed").length;
      const looping = entries.filter((d) => (d.restarts || 0) > 0).length;
      dom.statusLine.textContent =
        `${up} of ${entries.length} running`
        + (broken ? `, ${broken} crashed` : "")
        + (looping ? `, ${looping} restarting` : "");
      dom.list.innerHTML = "";
      entries.forEach((entry, i) => dom.list.appendChild(renderDaemonRow(entry, i)));
      if (!entries.length) {
        dom.list.appendChild(el("div", { class: "skills-empty" }, "No services registered."));
      }
    } catch (err) {
      dom.statusLine.textContent = "couldn't read services";
      dom.list.innerHTML = "";
      dom.list.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  async function daemonAction(id, action) {
    try {
      const data = await Api.post(`/api/daemons/${encodeURIComponent(id)}/${action}`, {});
      toast(data.message || `${action}ed ${id}`, "info");
    } catch (err) {
      toast(err.message || `Couldn't ${action} ${id}.`);
    }
    // Starting is asynchronous by design — one refresh now, one shortly
    // after, so the row settles on the real state rather than "starting".
    refreshDaemons();
    setTimeout(refreshDaemons, 1500);
    if (state.selected === id) setTimeout(refreshDaemonConsole, 1500);
  }

  async function removeDaemon(id) {
    if (!global.confirm(`Remove the '${id}' service? Its console logs stay on disk.`)) return;
    try {
      await api("DELETE", `/api/daemons/${encodeURIComponent(id)}`);
      if (state.selected === id) state.selected = null;
      refreshDaemons();
    } catch (err) {
      toast(err.message || "Couldn't remove that service.");
    }
  }

  async function selectDaemon(id) {
    state.selected = id;
    if (dom.consoleTitle) dom.consoleTitle.textContent = `Console \u2014 ${id}`;
    await refreshDaemonConsole();
    refreshDaemons();
  }

  /* ---- console ------------------------------------------------------------*/

  function activeFilter() {
    return CONSOLE_FILTERS.find((f) => f.id === state.consoleFilter) || CONSOLE_FILTERS[0];
  }

  function renderConsoleLines(rawLines) {
    const pane = dom.consolePane;
    const wasAtBottom = state.autoscroll
      || (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 24);
    pane.innerHTML = "";
    const filter = activeFilter();
    let shown = 0;
    for (const raw of rawLines) {
      const { cls, text } = classifyLine(raw);
      if (filter.classes && !filter.classes.includes(cls)) continue;
      shown += 1;
      pane.appendChild(el("div", { class: `dmn-line dmn-line--${cls}` }, text));
    }
    if (!rawLines.length) {
      pane.appendChild(el("div", { class: "dmn-console-empty" }, "(no output yet)"));
    } else if (!shown) {
      pane.appendChild(el("div", { class: "dmn-console-empty" },
        `(nothing matches "${filter.label}" \u2014 try All)`));
    }
    if (wasAtBottom) pane.scrollTop = pane.scrollHeight;
  }

  function renderConsoleToolbar() {
    const bar = dom.toolbar;
    if (!bar) return;
    bar.innerHTML = "";
    CONSOLE_FILTERS.forEach((f) => {
      bar.appendChild(el("button", {
        class: "dmn-chip" + (state.consoleFilter === f.id ? " is-active" : ""),
        type: "button",
        onclick: () => { state.consoleFilter = f.id; refreshDaemonConsole(); },
      }, f.label));
    });
    const auto = el("label", { class: "dmn-autoscroll" }, [
      el("input", {
        type: "checkbox",
        id: "dmn-autoscroll",
        checked: state.autoscroll || undefined,
        onchange: (ev) => { state.autoscroll = ev.target.checked; },
      }),
      "auto-scroll",
    ]);
    bar.appendChild(auto);
  }

  async function refreshDaemonConsole() {
    const pane = dom.consolePane;
    if (!pane || !state.selected) return;
    renderConsoleToolbar();
    const picker = dom.filePicker;
    const file = picker && picker.value ? `&file=${encodeURIComponent(picker.value)}` : "";
    try {
      const data = await Api.get(
        `/api/daemons/${encodeURIComponent(state.selected)}/console?lines=300${file}`);
      const lines = data.lines || [];
      renderConsoleLines(lines);

      if (picker && !file) {
        const backups = data.backups || [];
        const want = ["", ...backups].join("|");
        if (picker.dataset.loaded !== want) {
          picker.dataset.loaded = want;
          picker.innerHTML = "";
          picker.appendChild(el("option", { value: "" }, "current"));
          for (const path of backups) {
            picker.appendChild(el("option", { value: path }, path.split(/[\\/]/).pop()));
          }
        }
      }

      // stdin is only offered where it can actually work: a running daemon
      // whose definition says its process reads stdin.
      const entry = (state.rows || []).find((d) => d.id === state.selected)
        || (await Api.get("/api/daemons")).daemons.find((d) => d.id === state.selected);
      if (dom.inputRow) dom.inputRow.hidden = !(entry && entry.running && entry.supports_stdin);
    } catch (err) {
      pane.innerHTML = "";
      pane.appendChild(el("div", { class: "dmn-console-empty" }, err.message || "Couldn't read that console."));
    }
  }

  /* ---- keyboard: Up/Down between services, page the console -------------*/

  function moveSelection(delta) {
    const rows = state.rows;
    if (!rows.length) return;
    const from = state.focusIndex >= 0 ? state.focusIndex
      : Math.max(0, rows.findIndex((r) => r.id === state.selected));
    const next = Math.max(0, Math.min(rows.length - 1, from + delta));
    state.focusIndex = next;
    const row = dom.list.children[next];
    if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    refreshDaemons();
  }

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") { close(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); return; }
    if (e.key === "Enter") {
      const row = state.rows[state.focusIndex];
      if (row) { e.preventDefault(); selectDaemon(row.id); }
      return;
    }
    if (["PageDown", "PageUp", "Home", "End"].includes(e.key) && dom.consolePane) {
      e.preventDefault();
      const pane = dom.consolePane;
      if (e.key === "PageDown") pane.scrollTop += pane.clientHeight * 0.9;
      else if (e.key === "PageUp") pane.scrollTop -= pane.clientHeight * 0.9;
      else if (e.key === "Home") pane.scrollTop = 0;
      else pane.scrollTop = pane.scrollHeight;
      return;
    }
    if (e.key === "r" || e.key === "R") {
      e.preventDefault();
      refreshDaemons();
      refreshDaemonConsole();
    }
  }

  /* ---- wiring, open/close -------------------------------------------------*/

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;

    $("#btn-daemon-refresh")?.addEventListener("click", () => { refreshDaemons(); refreshDaemonConsole(); });
    dom.filePicker?.addEventListener("change", refreshDaemonConsole);

    $("#btn-daemon-add")?.addEventListener("click", async () => {
      const id = ($("#daemon-new-id")?.value || "").trim();
      const command = ($("#daemon-new-command")?.value || "").trim();
      if (!id || !command) { toast("An id and a command are both required."); return; }
      try {
        await Api.post("/api/daemons", {
          id, command,
          cwd: ($("#daemon-new-cwd")?.value || "").trim(),
          stdin: Boolean($("#daemon-new-stdin")?.checked),
        });
        $("#daemon-new-id").value = "";
        $("#daemon-new-command").value = "";
        $("#daemon-new-cwd").value = "";
        refreshDaemons();
      } catch (err) {
        toast(err.message || "Couldn't register that service.");
      }
    });

    $("#btn-daemon-send")?.addEventListener("click", async () => {
      const input = dom.inputText;
      const text = (input?.value || "").trim();
      if (!state.selected || !text) return;
      try {
        await Api.post(`/api/daemons/${encodeURIComponent(state.selected)}/input`, { text });
        input.value = "";
        setTimeout(refreshDaemonConsole, 400);
      } catch (err) {
        toast(err.message || "Couldn't send that.");
      }
    });
    dom.inputText?.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") $("#btn-daemon-send")?.click();
    });

    $("#btn-daemon-schedule")?.addEventListener("click", async () => {
      const when = ($("#daemon-schedule-when")?.value || "").trim();
      if (!state.selected) { toast("Pick a service first."); return; }
      try {
        const data = await Api.post(
          `/api/daemons/${encodeURIComponent(state.selected)}/schedule`, { when });
        toast(when ? `Will start at ${data.next_start || when}` : "Schedule cleared", "info");
        refreshDaemons();
      } catch (err) {
        toast(err.message || "Couldn't schedule that.");
      }
    });

    $("#daemons-close")?.addEventListener("click", close);
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);

    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) {
      toast("Daemons markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    renderConsoleToolbar();
    refreshDaemons();
    if (state.selected) refreshDaemonConsole();
    // A console is only useful live; polling stops the moment the panel
    // closes, so a closed overlay never leaves an interval (and subprocess
    // spawns for status reads) running behind it.
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => { refreshDaemons(); refreshDaemonConsole(); }, 4000);
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    dom.overlay.hidden = true;
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  function isOpen() {
    return Boolean(dom && !dom.overlay.hidden);
  }

  global.JarvisDaemons = { open, close, isOpen };
})(window);
