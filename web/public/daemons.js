/* ============================================================================
 * daemons.js — Menu → Daemons.
 *
 * H.1.5 rework (rev. 2026-09-28): the panel was rebuilt to the Test Checklist
 * bar after the H.1 / H.1.2–H.1.4 rounds still read as "the old panel with
 * extras". Same three-pane information architecture as Test Checklist:
 *
 *   LEFT    Services — live search (`/`), state chips with counts, a category
 *           filter (Built-in, your own categories, Undefined — L.11), grouped
 *           cards with a state colour, uptime and a crash-loop badge in the
 *           card itself.
 *   MIDDLE  The selected service — state + actions, alerts that say WHAT is
 *           wrong, a Console tab (stdout / stderr / traceback / system, search
 *           with highlight, tail size, rotated logs, stdin) and a Details tab
 *           (runtime facts, configuration, quick settings, scheduled start).
 *           "Add" and "Edit" replace this pane with a real form editor.
 *   RIGHT   Overview — how many are up, by-state legend (click to filter),
 *           needs-attention list, what is scheduled, actions, a short guide.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 *  - No new backend subsystem. It reads /api/daemons and friends exactly as
 *    before. The only backend additions are small and listed in the H.1.5
 *    notes: daemon-edit learned `--description` and `--env-replace`, and the
 *    PATCH route forwards the full field set (so an existing daemon can be
 *    edited, which H.1.4 flagged as missing).
 *  - Categories (L.11) are labels, nothing more. `builtin` stays a separate
 *    flag because it is also a LOCK (a built-in's command can't be changed);
 *    a category never locks or unlocks anything. The rules for a name and the
 *    chip editor live in category-input.js (JarvisCategories), shared with
 *    commands (L.14) - this file must load after it.
 *  - Nothing here can create a daemon on the model's behalf. The AGENTS.md
 *    invariant ("the model may start, stop and inspect daemons; it may not
 *    create one") is about the model's tools; this is the human's panel.
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 * Same rule as ui-kit.js / test-checklist.js. Console output, commands, env
 * values and error strings all come from processes and other people; they are
 * inserted with textContent / text nodes, never innerHTML.
 *
 * STRUCTURE OF THIS FILE
 * ----------------------
 *   1. constants
 *   2. pure helpers (no DOM, no network) — also exposed as
 *      JarvisDaemons._pure for tests/verify_daemons_panel.js
 *   3. DOM helpers + state
 *   4. left pane, right pane
 *   5. middle pane: summary, console, details
 *   6. editor (add / edit)
 *   7. actions, polling, keyboard, open / close
 *
 * Deliberately standalone, like test-checklist.js: app.js only calls
 * window.JarvisDaemons.open() from the Menu item.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ======================================================================
   * 1. constants
   * ==================================================================== */

  const UI_KEY = "jarvis.daemons.ui.v1";

  // One entry per bucket the panel shows. `bucketOf()` maps the backend's
  // status strings onto these (starting + restarting share a bucket; anything
  // unknown reads as stopped rather than inventing a state).
  const STATES = [
    { id: "running",   label: "Running",   glyph: "\u25CF", hint: "The process is up." },
    { id: "starting",  label: "Starting",  glyph: "\u25D0", hint: "Being launched, or waiting out a restart delay." },
    { id: "stopped",   label: "Stopped",   glyph: "\u25CB", hint: "Not running, and nothing is trying to run it." },
    { id: "crashed",   label: "Crashed",   glyph: "\u2717", hint: "Exited unexpectedly, or never managed to start." },
    { id: "scheduled", label: "Scheduled", glyph: "\u25D4", hint: "Down, with a start time set." },
  ];
  const STATE = Object.fromEntries(STATES.map((s) => [s.id, s]));

  // The console line classes and the filters over them. `crash` is a stderr
  // line shaped like a traceback; it is counted with Errors.
  const CONSOLE_FILTERS = [
    { id: "all",    label: "All" },
    { id: "output", label: "Output", classes: ["stdout"] },
    { id: "errors", label: "Errors", classes: ["stderr", "crash"] },
    { id: "system", label: "System", classes: ["meta", "stdin"] },
  ];

  const TAIL_OPTIONS = [100, 300, 1000];
  const RESTART_POLICIES = [
    { id: "never",      label: "Never",      hint: "Leave it stopped if it exits." },
    { id: "on-failure", label: "On failure", hint: "Restart only when it exits with an error." },
    { id: "always",     label: "Always",     hint: "Restart whenever it exits, even cleanly." },
  ];
  const STOP_SIGNALS = ["TERM", "INT", "KILL"];
  const DEFAULTS = { restart: "never", restart_delay: 5, max_restarts: 5, stop_signal: "TERM", stop_timeout: 10 };
  // daemons.py counts restarts inside this window (RESTART_WINDOW_SECONDS).
  const RESTART_WINDOW_MIN = 10;

  const ID_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/; // web/server.js DAEMON_ID

  /* ======================================================================
   * 2. pure helpers
   * ==================================================================== */

  // Mirrors daemons.normalize_id(): lower-case, spaces to dashes, only
  // [a-z0-9_-], 40 chars. Used to tell the user what their id will become.
  function normalizeId(value) {
    const text = String(value == null ? "" : value).trim().toLowerCase().replace(/ /g, "-");
    return text.replace(/[^a-z0-9_-]/g, "").slice(0, 40);
  }

  function bucketOf(entry) {
    if (!entry) return "stopped";
    const s = String(entry.status || "");
    if (entry.running || s === "running") return "running";
    if (s === "starting" || s === "restarting") return "starting";
    if (s === "crashed") return "crashed";
    if (s === "scheduled") return "scheduled";
    return "stopped";
  }

  // Registry entries for built-ins don't carry every tuning field; the
  // supervisor falls back to these defaults, so the panel shows the same.
  function withDefaults(entry) {
    const e = entry || {};
    return Object.assign({}, e, {
      restart: e.restart || DEFAULTS.restart,
      restart_delay: e.restart_delay != null ? Number(e.restart_delay) : DEFAULTS.restart_delay,
      max_restarts: e.max_restarts != null ? Number(e.max_restarts) : DEFAULTS.max_restarts,
      stop_signal: e.stop_signal || DEFAULTS.stop_signal,
      stop_timeout: e.stop_timeout != null ? Number(e.stop_timeout) : DEFAULTS.stop_timeout,
      enabled: e.enabled !== false,
    });
  }

  // H.1.3: a daemon mid-start or mid-backoff isn't `running` but already has a
  // live supervisor, so it must still be stoppable. `supervisor_pid` is
  // "the recorded supervisor pid, or null if not alive" (daemons.status()).
  function isStoppable(entry) {
    return Boolean(entry && (entry.running || entry.supervisor_pid));
  }

  function fmtDuration(totalSeconds) {
    let s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    if (s < 60) return `${s}s`;
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    return `${m}m ${String(s).padStart(2, "0")}s`;
  }

  // Seconds the process has been up, or null. `started_at` is epoch seconds
  // (time.time() in daemons.py).
  function uptimeSeconds(entry, nowMs) {
    if (!entry || !entry.running || !entry.started_at) return null;
    const started = Number(entry.started_at);
    if (!isFinite(started) || started <= 0) return null;
    const secs = (nowMs / 1000) - started;
    return secs >= 0 ? secs : 0;
  }

  /* ---- CPU / memory (daemons.py sample_usage) ------------------------------
   * Each running row from /api/daemons carries `usage` ({cpu_percent,
   * cpu_core_percent, rss_bytes, processes, procs[]}) and the response carries
   * `usage_total`. cpu_percent is a share of the whole machine (Task Manager's
   * reading); cpu_core_percent a share of one core. These numbers change on every
   * poll but are NOT part of entrySignature: they are written into existing nodes
   * (refreshUsage) so a card under the cursor is never repainted. */
  function fmtBytes(n) {
    const v = Number(n);
    if (!isFinite(v) || v < 0) return "\u2014";
    if (v < 1024) return `${Math.round(v)} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let x = v / 1024;
    let i = 0;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
    return `${x < 10 ? x.toFixed(1) : Math.round(x)} ${units[i]}`;
  }

  function fmtCpu(p) {
    const v = Number(p);
    if (!isFinite(v) || v < 0) return "\u2014";
    if (v > 0 && v < 0.1) return "<0.1%";
    return `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;
  }

  // "CPU 3.2% · RAM 120 MB", or "" when there is nothing to show.
  function usageText(u) {
    if (!u || typeof u !== "object") return "";
    return `CPU ${fmtCpu(u.cpu_percent)} \u00B7 RAM ${fmtBytes(u.rss_bytes)}`;
  }

  function usageTitle(u) {
    if (!u || typeof u !== "object") return "";
    const n = Number(u.processes) || 0;
    return `CPU is a share of the whole machine (${fmtCpu(u.cpu_core_percent)} of one core). `
      + `RAM is what ${n} process${n === 1 ? "" : "es"} (the supervisor, the service and anything it started) hold in memory.`;
  }

  // The whole-panel line: "all running: CPU 12% · RAM 480 MB (7 processes)".
  function usageTotalText(t) {
    if (!t || typeof t !== "object") return "";
    const n = Number(t.processes) || 0;
    return `all services: CPU ${fmtCpu(t.cpu_percent)} \u00B7 RAM ${fmtBytes(t.rss_bytes)} (${n} process${n === 1 ? "" : "es"})`;
  }

  const pad2 = (n) => String(n).padStart(2, "0");

  function fmtStamp(epochSeconds) {
    const n = Number(epochSeconds);
    if (!isFinite(n) || n <= 0) return "\u2014";
    const d = new Date(n * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function fmtClock(ms) {
    const d = new Date(ms);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  // next_start is an ISO string (timespec.to_iso). Returns {label, relative}
  // or falls back to the raw text when it can't be parsed.
  function describeNextStart(iso, nowMs) {
    if (!iso) return null;
    const t = Date.parse(iso);
    if (!isFinite(t)) return { label: String(iso), relative: "" };
    const d = new Date(t);
    const label = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    const delta = (t - nowMs) / 1000;
    const relative = delta > 0 ? `in ${fmtDuration(delta)}` : "due now";
    return { label, relative };
  }

  function restartSummary(entry) {
    const e = withDefaults(entry);
    if (e.restart === "never") return "Never restarts";
    const limit = e.max_restarts > 0
      ? `up to ${e.max_restarts} per ${RESTART_WINDOW_MIN} min`
      : "no limit";
    return `${e.restart === "always" ? "Always" : "On failure"} \u00B7 wait ${e.restart_delay}s \u00B7 ${limit}`;
  }

  function stopSummary(entry) {
    const e = withDefaults(entry);
    return `${e.stop_signal}, force-kill after ${e.stop_timeout}s`;
  }

  // Everything that is wrong with one service, worst first. The overview's
  // "needs attention" list, the card badge and the detail alerts all read
  // this, so they cannot disagree with each other.
  function attentionOf(entry) {
    const out = [];
    if (!entry) return out;
    const restarts = Number(entry.restarts) || 0;
    const max = entry.max_restarts != null ? Number(entry.max_restarts) : DEFAULTS.max_restarts;
    if (bucketOf(entry) === "crashed") {
      const note = String(entry.last_error || "").replace(/\s+/g, " ").trim();
      out.push({
        kind: "crashed", level: "bad", label: "Crashed",
        note: note || (entry.exit_code != null ? `exited with code ${entry.exit_code}` : "exited unexpectedly"),
      });
    }
    if (max > 0 && restarts >= max) {
      out.push({
        kind: "limit", level: "bad", label: "Restart limit reached",
        note: `${restarts}/${max} restarts in ${RESTART_WINDOW_MIN} min \u2014 it won't be restarted again until you start it yourself`,
      });
    } else if (restarts > 0) {
      out.push({
        kind: "looping", level: "warn", label: "Restarting",
        note: `restarted ${restarts}\u00D7 in the last ${RESTART_WINDOW_MIN} min`,
      });
    }
    return out;
  }

  /* ---- categories (L.11) -------------------------------------------------- */

  // Names are cleaned by JarvisCategories (category-input.js), the same rules
  // as categories.py. daemons.open() refuses to open without it.
  const cat = () => global.JarvisCategories;

  // The names on one daemon, cleaned again here because a test fixture or an
  // older server can hand over anything. Always an array; [] = Undefined.
  function categoriesOf(entry) {
    return cat().normalizeList(entry && entry.categories).list;
  }

  // The kind filter is one value: "all" | "builtin" | "undefined" | "cat:<key>"
  // (key = the lower-cased name). "custom" used to be a value; it is retired
  // - uncategorised daemons are "undefined" now - so anything else, including
  // an old saved "custom", means "all".
  const KIND_PATTERN = /^(all|builtin|undefined|cat:.+)$/;
  function normalizeKind(value) {
    return typeof value === "string" && KIND_PATTERN.test(value) ? value : "all";
  }

  // A kind that no longer matches anything - a category nobody has any more -
  // falls back to "all", so the list is never empty for a reason the person
  // can't see. ("undefined" is always meaningful: it just means "none left".)
  function reconcileKind(kind, entries) {
    const k = normalizeKind(kind);
    if (!k.startsWith("cat:")) return k;
    const want = k.slice(4);
    return (entries || []).some((e) => categoriesOf(e).some((c) => c.toLowerCase() === want)) ? k : "all";
  }

  function searchHaystack(entry) {
    const e = entry || {};
    const bits = [
      e.id, e.name, e.description, e.command, e.cwd, e.notes, e.status, e.last_error,
      e.builtin ? "built-in builtin" : "custom",
      categoriesOf(e).join(" "),
      e.autostart ? "autostart" : "",
      e.enabled === false ? "disabled" : "",
      e.adopted ? "outside" : "",
    ];
    return bits.filter(Boolean).join(" \u0001 ").toLowerCase();
  }

  // filters = { search, state: all|attention|<bucket>,
  //             kind: all|builtin|undefined|cat:<key>  (see normalizeKind),
  //             favOnly?: boolean, favorites?: Set<id> }
  // favOnly is its own switch rather than another `state` value so it composes
  // (AND) with the state chips, the kind select and the search box.
  function matchesFilters(entry, filters) {
    const f = filters || {};
    if (f.favOnly && !(f.favorites && f.favorites.has && f.favorites.has(entry.id))) return false;
    if (f.state && f.state !== "all") {
      if (f.state === "attention") { if (!attentionOf(entry).length) return false; }
      else if (bucketOf(entry) !== f.state) return false;
    }
    const kind = normalizeKind(f.kind);
    if (kind === "builtin") { if (!entry.builtin) return false; }
    else if (kind === "undefined") { if (categoriesOf(entry).length) return false; }
    else if (kind.startsWith("cat:")) {
      const want = kind.slice(4);
      if (!categoriesOf(entry).some((c) => c.toLowerCase() === want)) return false;
    }
    const terms = String(f.search || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length) {
      const hay = searchHaystack(entry);
      if (!terms.every((t) => hay.includes(t))) return false;
    }
    return true;
  }

  // `favorites` (optional Set of ids) adds a `favorites` count. It counts only
  // ids that still have an entry, so a stale id left in the saved list can
  // never inflate the chip.
  function countEntries(entries, favorites) {
    const byState = { running: 0, starting: 0, stopped: 0, crashed: 0, scheduled: 0 };
    let attention = 0;
    let favs = 0;
    const kinds = { builtin: { total: 0, running: 0 }, custom: { total: 0, running: 0 } };
    // L.11: `categories` is every category in use, most-used first, each with
    // how many of its services are up; `uncategorised` is the "Undefined"
    // bucket. A service in two categories counts in both, so these do not add
    // up to `total` - they are filters, not a partition.
    const runningBy = {};
    const uncategorised = { total: 0, running: 0 };
    (entries || []).forEach((e) => {
      const b = bucketOf(e);
      byState[b] += 1;
      if (attentionOf(e).length) attention += 1;
      const k = e.builtin ? kinds.builtin : kinds.custom;
      k.total += 1;
      if (b === "running") k.running += 1;
      if (favorites && favorites.has && favorites.has(e.id)) favs += 1;
      const cats = categoriesOf(e);
      if (!cats.length) { uncategorised.total += 1; if (b === "running") uncategorised.running += 1; }
      cats.forEach((c) => { if (b === "running") runningBy[c.toLowerCase()] = (runningBy[c.toLowerCase()] || 0) + 1; });
    });
    const categories = cat().vocabulary((entries || []).map(categoriesOf))
      .map((v) => ({ key: v.key, name: v.name, total: v.count, running: runningBy[v.key] || 0 }));
    return { total: (entries || []).length, byState, attention, kinds, categories, uncategorised, favorites: favs };
  }

  // Favorites float to the top, everything else keeps its incoming order
  // (a stable partition, not a sort, so the order the backend gave is kept
  // inside each half). Applied per group by both the list and the arrow-key
  // walk, so what you see and what ArrowDown visits stay the same order.
  function sortFavoritesFirst(entries, favorites) {
    if (!favorites || !favorites.size) return entries;
    const top = [];
    const rest = [];
    entries.forEach((e) => (favorites.has(e.id) ? top : rest).push(e));
    return top.concat(rest);
  }

  // The favorites list after toggling one id, as a new array (the caller
  // owns the Set). Returns { ids, now } where `now` is the new state of `id`.
  function toggleFavoriteId(ids, id) {
    const set = new Set(ids || []);
    const now = !set.has(id);
    if (now) set.add(id); else set.delete(id);
    return { ids: [...set], now };
  }

  // The fields whose change should repaint a card / the summary. Uptime is
  // deliberately absent: it ticks on its own timer, and repainting a row
  // under the cursor every poll makes buttons miss clicks.
  function entrySignature(entry) {
    const e = entry || {};
    return JSON.stringify([
      e.id, e.name, e.status, e.running, e.pid, e.supervisor_pid, e.adopted, e.restarts,
      e.max_restarts, e.last_error, e.exit_code, e.next_start, e.enabled, e.autostart,
      e.supports_stdin, e.command, e.cwd, e.description, e.notes, e.shell, e.restart,
      e.restart_delay, e.stop_signal, e.stop_timeout, e.started_at, e.env, e.categories,
    ]);
  }

  /* ---- console lines ---------------------------------------------------- */

  // Meta ("=== ...") and stdin-echo ("<<< ...") lines are stamped by
  // daemons.py's _stamp() with "[YYYY-MM-DD HH:MM:SS] ". A stderr-origin line
  // carries a plain "E: " marker (run_supervisor's _pump); a traceback is a
  // stderr line of one of the shapes below.
  //
  // H.1.7 (owner-reported, 2026-09-29: the Error / System / Output detector
  // misfiled lines). The old rules were too shallow in four ways, each
  // reproduced against realistic console text before being fixed:
  //   1. Any "[YYYY-MM-DD HH:MM:SS] " prefix meant System. Plenty of daemons
  //      stamp their own output the same way, so their ordinary stdout
  //      landed in System. Only the supervisor's own "=== " banners and
  //      "<<< " stdin echoes are System now.
  //   2. Everything on stderr was an "error". Python's logging, discord.py,
  //      uvicorn, Flask and friends write routine INFO/DEBUG lines to stderr,
  //      so the Errors chip and the "N error lines" counter filled up with
  //      health-check noise. A stderr line that states an INFO/DEBUG level is
  //      Output now.
  //   3. The reverse: a daemon that logs to stdout ("ERROR ..." from print,
  //      console.log, or a shell wrapper doing `2>&1`) never showed anything
  //      under Errors, and a Python traceback on stdout was plain output. An
  //      explicit ERROR/CRITICAL/FATAL level, or a traceback, is an error
  //      wherever it was written.
  //   4. "...Warning:" counted as the end of a crash, so a DeprecationWarning
  //      was drawn as a crash block; and Node stack frames ("    at ...")
  //      were never grouped with the error above them.
  const STAMP_RE = /^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] /;
  const TRACEBACK_HEAD_RE = /^Traceback \(most recent call last\):/;
  const TRACEBACK_FRAME_RE = /^\s+File "/;
  const TRACEBACK_TAIL_RE = /^[A-Za-z_][\w.]*(Error|Exception)\b/;
  const JS_FRAME_RE = /^\s+at\s+(?:\S+\s+\(.*:\d+:\d+\)|[^\s(]+:\d+:\d+)\s*$/;
  const JS_ERROR_HEAD_RE = /^(?:[A-Za-z_][\w.]*)?(?:Error|Exception)\b/;
  const BAD_EXIT_RE = /=== exited with code (?!0\b)-?\d+/;
  const BAD_META_RE = /=== (?:failed to start|restart limit reached)/;

  // An explicit severity token near the start of a line, optionally behind a
  // timestamp: "INFO:discord.client: ...", "[INFO] ...", "2026-09-29
  // 12:00:00,123 - INFO - ...", "12:00:00 ERROR ...". Upper-case only, so
  // ordinary prose that happens to begin with the word "info" or "error" is
  // not read as a log level.
  const LEVEL_PREFIX = "^(?:\\[?\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2}(?:[.,]\\d+)?(?:Z|[+-]\\d{2}:?\\d{2})?\\]?[\\s:,|-]*"
    + "|\\[?\\d{2}:\\d{2}:\\d{2}(?:[.,]\\d+)?\\]?[\\s:,|-]*)?\\[?\\s*";
  const LEVEL_END = "\\s*\\]?(?=[\\s:|,-]|$)";
  const LEVEL_QUIET_RE = new RegExp(LEVEL_PREFIX + "(?:DEBUG|INFO|NOTICE|TRACE|VERBOSE)" + LEVEL_END);
  const LEVEL_ERROR_RE = new RegExp(LEVEL_PREFIX + "(?:ERROR|CRITICAL|FATAL|SEVERE)" + LEVEL_END);

  function classifyLine(raw) {
    const line = String(raw == null ? "" : raw);
    if (STAMP_RE.test(line)) {
      const rest = line.replace(STAMP_RE, "");
      if (rest.startsWith("<<< ")) return { cls: "stdin", text: line, bad: false, origin: "meta" };
      if (rest.startsWith("=== ")) {
        return { cls: "meta", text: line, bad: BAD_EXIT_RE.test(rest) || BAD_META_RE.test(rest), origin: "meta" };
      }
      // A stamp that isn't one of ours: the daemon's own output.
      return classifyOutput(line, "stdout");
    }
    if (line.startsWith("E: ")) return classifyOutput(line.slice(3), "stderr");
    return classifyOutput(line, "stdout");
  }

  // `origin` is the stream the line arrived on; `cls` is what it *is*.
  function classifyOutput(text, origin) {
    if (TRACEBACK_HEAD_RE.test(text) || TRACEBACK_FRAME_RE.test(text) || JS_FRAME_RE.test(text)) {
      return { cls: "crash", text, bad: false, origin };
    }
    if (origin === "stderr") {
      // A blank line on stderr is spacing, not an error.
      if (!text.trim()) return { cls: "stdout", text, bad: false, origin };
      if (TRACEBACK_TAIL_RE.test(text)) return { cls: "crash", text, bad: false, origin };
      if (LEVEL_QUIET_RE.test(text)) return { cls: "stdout", text, bad: false, origin };
      return { cls: "stderr", text, bad: false, origin };
    }
    if (LEVEL_ERROR_RE.test(text)) return { cls: "stderr", text, bad: false, origin };
    return { cls: "stdout", text, bad: false, origin };
  }

  // n is the 1-based position in the tail that was fetched, so a line keeps
  // its number while a filter is on.
  //
  // A real traceback's own shape defeats a purely per-line regex: the
  // frame/source lines between the head and the exception message ("    File
  // ...", then the literal source line under it, e.g. "    client.run(token)")
  // don't look like anything classifyLine alone can recognize, and the
  // exception message itself is only reliably "the first unindented line
  // after the head" — plenty of real exception class names (Discord.py's own
  // LoginFailure, for one) don't end in Error/Exception at all. So this
  // keeps a small piece of state — the stream a traceback started on —
  // across the whole tail: once the head line is seen, every following line
  // from the SAME stream is swept into the same block until the first
  // unindented one closes it, which is exactly how CPython lays a traceback
  // out. A line from the other stream ends the block (the two streams are
  // written by separate threads, so they can interleave), and so does
  // anything the supervisor wrote.
  function annotateLines(rawLines) {
    let traceOrigin = null;
    const out = [];
    (rawLines || []).forEach((raw, i) => {
      const base = classifyLine(raw);
      let cls = base.cls;
      const indented = /^\s/.test(base.text);
      const blank = !base.text.trim();
      if (cls === "crash" && TRACEBACK_HEAD_RE.test(base.text)) {
        traceOrigin = base.origin;
      } else if (traceOrigin && base.origin === traceOrigin && (cls === "stderr" || cls === "stdout" || cls === "crash")) {
        cls = "crash";
        // An unindented, non-blank line is the exception line: the block ends here.
        if (!indented && !blank) traceOrigin = null;
      } else if (traceOrigin && base.origin !== traceOrigin && base.origin !== "meta") {
        // The other stream interleaved. Leave this line as classified and
        // keep the block open: its remaining lines are still coming.
      } else {
        traceOrigin = null;
      }
      // A JS stack is "Error: message" followed by "    at ..." frames; the
      // message line above the first frame belongs to the same block.
      if (cls === "crash" && JS_FRAME_RE.test(base.text) && out.length) {
        const prev = out[out.length - 1];
        if (prev.cls === "stderr" && prev.origin === base.origin && JS_ERROR_HEAD_RE.test(prev.text)) prev.cls = "crash";
      }
      out.push(Object.assign({ n: i + 1 }, base, { cls }));
    });
    return out;
  }

  function summarizeLines(items) {
    const c = { all: 0, output: 0, errors: 0, system: 0 };
    (items || []).forEach((it) => {
      c.all += 1;
      if (it.cls === "stdout") c.output += 1;
      else if (it.cls === "stderr" || it.cls === "crash") c.errors += 1;
      else c.system += 1;
    });
    return c;
  }

  function filterLines(items, filterId, query, onlyMatches) {
    const f = CONSOLE_FILTERS.find((x) => x.id === filterId) || CONSOLE_FILTERS[0];
    const q = String(query || "").toLowerCase();
    return (items || []).filter((it) => {
      if (f.classes && !f.classes.includes(it.cls)) return false;
      if (q && onlyMatches && !it.text.toLowerCase().includes(q)) return false;
      return true;
    });
  }

  // Consecutive traceback lines become one block so a crash reads as one
  // event instead of a scatter of red lines.
  function groupTraces(items) {
    const blocks = [];
    (items || []).forEach((it) => {
      const last = blocks[blocks.length - 1];
      if (it.cls === "crash") {
        if (last && last.type === "trace") last.items.push(it);
        else blocks.push({ type: "trace", items: [it] });
      } else {
        blocks.push({ type: "line", items: [it] });
      }
    });
    return blocks;
  }

  // Splits text around case-insensitive plain-text matches of q. Never
  // treats q as a regex, and never produces markup.
  function highlightSegments(text, query) {
    const s = String(text == null ? "" : text);
    const q = String(query || "");
    if (!q) return [{ text: s, hit: false }];
    const hay = s.toLowerCase();
    const needle = q.toLowerCase();
    const out = [];
    let from = 0;
    for (;;) {
      const at = hay.indexOf(needle, from);
      if (at < 0) break;
      if (at > from) out.push({ text: s.slice(from, at), hit: false });
      out.push({ text: s.slice(at, at + needle.length), hit: true });
      from = at + needle.length;
    }
    if (from < s.length) out.push({ text: s.slice(from), hit: false });
    return out.length ? out : [{ text: s, hit: false }];
  }

  /* ---- editor: draft <-> entry <-> request body -------------------------- */

  function draftFromEntry(entry) {
    const e = withDefaults(entry || {});
    const env = e.env && typeof e.env === "object"
      ? Object.entries(e.env).map(([k, v]) => ({ k: String(k), v: String(v) }))
      : [];
    return {
      id: e.id || "",
      // add() defaults a custom daemon's name to its id; show that as blank so
      // "no display name" and "name equals id" are the same thing in the form.
      name: !e.name || e.name === e.id ? "" : String(e.name),
      description: e.description || "",
      command: e.command || "",
      shell: Boolean(e.shell),
      cwd: e.cwd || "",
      env,
      restart: e.restart,
      restartDelay: String(e.restart_delay),
      maxRestarts: String(e.max_restarts),
      stopSignal: e.stop_signal,
      stopTimeout: String(e.stop_timeout),
      stdin: Boolean(e.supports_stdin),
      autostart: Boolean(e.autostart),
      enabled: e.enabled !== false,
      notes: e.notes || "",
      categories: categoriesOf(e),
    };
  }

  function blankDraft() {
    return draftFromEntry({
      id: "", name: "", description: "", command: "", shell: false, cwd: "", env: {},
      supports_stdin: false, autostart: false, enabled: true, notes: "", categories: [],
    });
  }

  const wholeNumber = (text) => (/^\d+$/.test(String(text).trim()) ? parseInt(String(text).trim(), 10) : NaN);
  const ENV_KEY_RE = /^[^\s=]+$/;

  // ctx = { mode: "add"|"edit", builtin, existingIds:Set }
  // Returns { errors: {field: message}, order: [field...], ok }.
  // `order` is the form order, so "needs: id, command" reads top to bottom.
  function validateDraft(draft, ctx) {
    const c = ctx || {};
    const errors = {};
    const d = draft || blankDraft();

    if (c.mode === "add") {
      const norm = normalizeId(d.id);
      if (!String(d.id).trim()) errors.id = "An id is required.";
      else if (!ID_RE.test(norm)) errors.id = "Must start with a letter or digit.";
      else if (c.existingIds && c.existingIds.has(norm)) errors.id = `'${norm}' already exists.`;
    }
    if (!c.builtin) {
      if (!String(d.command).trim()) errors.command = "A command is required.";
    }
    // The chip box can't produce a bad set (it caps names and the count), so
    // this is the backstop for a draft that did not come from it.
    const cats = cat().normalizeList(d.categories, true);
    if (cats.error) errors.categories = cats.error.charAt(0).toUpperCase() + cats.error.slice(1) + ".";
    const delay = wholeNumber(d.restartDelay);
    if (!(delay >= 1)) errors.restartDelay = "Whole seconds, 1 or more.";
    const max = wholeNumber(d.maxRestarts);
    if (!(max >= 0)) errors.maxRestarts = "Whole number, 0 or more (0 = no limit).";
    const stopT = wholeNumber(d.stopTimeout);
    if (!(stopT >= 1)) errors.stopTimeout = "Whole seconds, 1 or more.";

    const seen = new Set();
    (d.env || []).forEach((row, i) => {
      const k = String(row.k || "").trim();
      const v = String(row.v || "");
      if (!k && !v) return; // an untouched blank row is not an error
      if (!k) { errors[`env.${i}`] = "Name is required."; return; }
      if (!ENV_KEY_RE.test(k)) { errors[`env.${i}`] = "No spaces or '=' in a name."; return; }
      if (seen.has(k)) { errors[`env.${i}`] = `'${k}' is set twice.`; return; }
      seen.add(k);
    });

    const order = ["id", "categories", "command", "restartDelay", "maxRestarts", "stopTimeout"]
      .concat((d.env || []).map((_, i) => `env.${i}`))
      .filter((f) => errors[f]);
    return { errors, order, ok: order.length === 0 };
  }

  function envPairs(rows) {
    return (rows || [])
      .map((r) => ({ k: String(r.k || "").trim(), v: String(r.v == null ? "" : r.v) }))
      .filter((r) => r.k)
      .map((r) => `${r.k}=${r.v}`);
  }

  // POST /api/daemons body.
  function buildAddPayload(draft) {
    const body = {
      id: normalizeId(draft.id),
      command: String(draft.command).trim(),
      restart: draft.restart,
      restartDelay: String(wholeNumber(draft.restartDelay)),
      maxRestarts: String(wholeNumber(draft.maxRestarts)),
      stopSignal: draft.stopSignal,
      stopTimeout: String(wholeNumber(draft.stopTimeout)),
      stdin: Boolean(draft.stdin),
      shell: Boolean(draft.shell),
      autostart: Boolean(draft.autostart),
      env: envPairs(draft.env),
    };
    if (String(draft.name).trim()) body.name = String(draft.name).trim();
    if (String(draft.description).trim()) body.description = String(draft.description).trim();
    if (String(draft.cwd).trim()) body.cwd = String(draft.cwd).trim();
    if (String(draft.notes).trim()) body.notes = String(draft.notes).trim();
    if ((draft.categories || []).length) body.categories = draft.categories.slice();
    return body;
  }

  // PATCH /api/daemons/:id body — only what changed against the draft the
  // editor opened with. A built-in never sends command / shell / stdin:
  // daemons.edit() refuses them even when unchanged, which would fail the
  // whole save.
  function buildEditPayload(draft, initial, opts) {
    const builtin = Boolean(opts && opts.builtin);
    const body = {};
    const str = (key) => { if (String(draft[key]).trim() !== String(initial[key]).trim()) body[key] = String(draft[key]).trim(); };
    ["name", "description", "cwd", "notes"].forEach(str);
    if (!builtin) {
      if (String(draft.command).trim() !== String(initial.command).trim()) body.command = String(draft.command).trim();
      if (Boolean(draft.shell) !== Boolean(initial.shell)) body.shell = Boolean(draft.shell);
      if (Boolean(draft.stdin) !== Boolean(initial.stdin)) body.stdin = Boolean(draft.stdin);
    }
    if (Boolean(draft.autostart) !== Boolean(initial.autostart)) body.autostart = Boolean(draft.autostart);
    if (Boolean(draft.enabled) !== Boolean(initial.enabled)) body.enabled = Boolean(draft.enabled);
    if (draft.restart !== initial.restart) body.restart = draft.restart;
    if (draft.stopSignal !== initial.stopSignal) body.stopSignal = draft.stopSignal;
    const num = (key) => {
      if (wholeNumber(draft[key]) !== wholeNumber(initial[key])) body[key] = String(wholeNumber(draft[key]));
    };
    ["restartDelay", "maxRestarts", "stopTimeout"].forEach(num);
    // The whole set, sent whenever it differs - including [] ("remove them
    // all"), which the server turns into --clear-categories. Order counts: it
    // is the order the chips are shown in.
    if (JSON.stringify(draft.categories || []) !== JSON.stringify(initial.categories || [])) {
      body.categories = (draft.categories || []).slice();
    }
    const a = JSON.stringify(envPairs(draft.env));
    const b = JSON.stringify(envPairs(initial.env));
    if (a !== b) { body.env = envPairs(draft.env); body.envReplace = true; }
    return body;
  }

  /* ---- markdown status report ------------------------------------------- */

  function buildReport(entries, nowMs) {
    const list = entries || [];
    const counts = countEntries(list);
    const lines = [];
    lines.push(`# Jarvis daemons \u2014 ${fmtStamp(nowMs / 1000)}`);
    lines.push("");
    lines.push(`${counts.total} services \u00B7 ${counts.byState.running} running \u00B7 ${counts.byState.crashed} crashed \u00B7 ${counts.attention} need attention`);
    lines.push("");
    lines.push("| Service | State | Uptime | Restarts | Notes |");
    lines.push("|---|---|---|---|---|");
    list.forEach((e) => {
      const up = uptimeSeconds(e, nowMs);
      const attn = attentionOf(e);
      const cell = (t) => String(t == null ? "" : t).replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();
      lines.push(`| ${cell(e.id)}${e.builtin ? "" : " (custom)"} | ${cell(STATE[bucketOf(e)].label)} | ${up == null ? "\u2014" : fmtDuration(up)} | ${Number(e.restarts) || 0} | ${cell(attn.length ? attn[0].note : (e.enabled === false ? "disabled" : ""))} |`);
    });
    const bad = list.filter((e) => attentionOf(e).length);
    if (bad.length) {
      lines.push("");
      lines.push("## Needs attention");
      bad.forEach((e) => {
        attentionOf(e).forEach((a) => lines.push(`- **${e.id}** \u2014 ${a.label}: ${a.note}`));
      });
    }
    return lines.join("\n");
  }

  /* ======================================================================
   * 3. DOM helpers + state
   * ==================================================================== */

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "value") node.value = v;
      else if (k === "checked") node.checked = Boolean(v);
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
    else if (level === "error") console.error(message);
  }
  async function confirmDialog(opts) {
    if (UI() && UI().confirm) return UI().confirm(opts);
    return global.confirm((opts.title ? opts.title + "\n\n" : "") + (opts.body || ""));
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      try {
        const ta = el("textarea", { style: "position:fixed;opacity:0;left:-9999px" });
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        return ok;
      } catch (_2) { return false; }
    }
  }
  async function copyWithToast(text, what) {
    const ok = await copyText(text);
    toast(ok ? `Copied ${what}.` : `Couldn't copy ${what} \u2014 select it and copy by hand.`, ok ? "info" : "warn");
  }

  // The server answers 200 with {ok:false, message} for an action the CLI
  // refused (already running, disabled, ...), and 400 with {error} for
  // request-level failures. Both are failures here; the old panel toasted the
  // first kind as if it had worked.
  async function api(method, url, body) {
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (_) {
      throw new Error("Can't reach the Jarvis web server.");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) throw new Error((data && (data.error || data.message)) || `${method} ${url} failed (${res.status})`);
    if (data && data.ok === false) throw new Error(data.message || data.error || "That didn't work.");
    return data || {};
  }
  const Api = {
    get: (p) => api("GET", p),
    post: (p, b) => api("POST", p, b || {}),
    patch: (p, b) => api("PATCH", p, b),
    del: (p) => api("DELETE", p),
  };
  const enc = encodeURIComponent;

  const state = {
    built: false,
    entries: [],
    loadedOnce: false,
    usageTotal: null,       // the response's usage_total (all daemons together), or null
    loadError: "",
    lastOk: 0,
    inflight: false,
    dataSig: "",
    selected: null,
    search: "",
    stateFilter: "all",
    kindFilter: "all",
    // L.13: favorites. The ids live on the server (data/favorite-daemons.json,
    // so every browser sees the same stars); only the on/off of the filter is
    // a per-browser preference like the other filters.
    favorites: new Set(),
    favLoaded: false,
    favSaving: Promise.resolve(),
    favOnly: false,
    collapsed: new Set(),
    tab: "console",
    // console
    consoleFilter: "all",
    consoleQuery: "",
    onlyMatches: true,
    autoscroll: true,
    tail: 300,
    logFile: "",
    backups: [],
    consoleRaw: null,
    consoleItems: [],
    consoleFor: null,
    consoleAt: 0,
    consoleError: "",
    scrollGuardUntil: 0,
    stdinHistory: [],
    stdinPos: -1,
    // details
    showEnv: false,
    // editor
    editor: null,
    busy: {},
    live: true,
    pollTimer: null,
    tickTimer: null,
    listSig: "",
    sideSig: "",
    shell: null,
    shellFor: null,
    prevFocus: null,
  };
  let dom = null;
  let storageOk = true;

  function loadPrefs() {
    try {
      const raw = global.localStorage.getItem(UI_KEY);
      const ui = raw ? JSON.parse(raw) : {};
      if (ui && typeof ui === "object") {
        if (typeof ui.selected === "string") state.selected = ui.selected;
        if (typeof ui.stateFilter === "string") state.stateFilter = ui.stateFilter;
        // An old saved "custom" (retired by L.11) lands on "all", not on a
        // filter that matches nothing.
        state.kindFilter = normalizeKind(ui.kindFilter);
        if (typeof ui.favOnly === "boolean") state.favOnly = ui.favOnly;
        if (Array.isArray(ui.collapsed)) state.collapsed = new Set(ui.collapsed.filter((x) => typeof x === "string"));
        if (["console", "details"].includes(ui.tab)) state.tab = ui.tab;
        if (CONSOLE_FILTERS.some((f) => f.id === ui.consoleFilter)) state.consoleFilter = ui.consoleFilter;
        if (typeof ui.onlyMatches === "boolean") state.onlyMatches = ui.onlyMatches;
        if (TAIL_OPTIONS.includes(ui.tail)) state.tail = ui.tail;
      }
    } catch (_) { storageOk = false; }
  }
  function savePrefs() {
    try {
      global.localStorage.setItem(UI_KEY, JSON.stringify({
        selected: state.selected, stateFilter: state.stateFilter, kindFilter: state.kindFilter,
        favOnly: state.favOnly, collapsed: [...state.collapsed], tab: state.tab, consoleFilter: state.consoleFilter,
        onlyMatches: state.onlyMatches, tail: state.tail,
      }));
    } catch (_) { storageOk = false; }
  }

  function grabDom() {
    const overlay = $("#daemons-overlay");
    if (!overlay) return null;
    const d = {
      overlay,
      panel: $("#dmn-panel"),
      statusLine: $("#daemons-status-line"),
      live: $("#btn-daemon-live"),
      list: $("#daemons-list"),
      count: $("#dmn-count"),
      search: $("#dmn-search"),
      chips: $("#dmn-chips"),
      kind: $("#dmn-kind-filter"),
      main: $("#dmn-main"),
      foot: $("#dmn-foot"),
      side: $("#dmn-side"),
    };
    return Object.values(d).every(Boolean) ? d : null;
  }

  const entryById = (id) => state.entries.find((e) => e.id === id) || null;
  const selectedEntry = () => (state.selected ? entryById(state.selected) : null);
  const displayName = (e) => (e && (e.name || e.id)) || "";
  const activeFilters = () => ({
    search: state.search, state: state.stateFilter, kind: state.kindFilter,
    favOnly: state.favOnly, favorites: state.favorites,
  });
  const isFiltering = () => Boolean(state.search.trim()) || state.stateFilter !== "all" || state.kindFilter !== "all" || state.favOnly;
  const isFavorite = (id) => state.favorites.has(id);
  const visibleEntries = () => state.entries.filter((e) => matchesFilters(e, activeFilters()));

  // The order the cards are drawn in, which is the order the arrow keys walk.
  function navEntries() {
    const shown = visibleEntries();
    const filtering = isFiltering();
    const out = [];
    ["builtin", "custom"].forEach((gid) => {
      if (!filtering && state.collapsed.has(gid)) return;
      sortFavoritesFirst(shown.filter((e) => (gid === "builtin") === Boolean(e.builtin)), state.favorites).forEach((e) => out.push(e));
    });
    return out;
  }

  /* ======================================================================
   * 4a. status line + chips + list (left pane)
   * ==================================================================== */

  function renderStatusLine() {
    const n = countEntries(state.entries);
    const bits = [`${n.total} service${n.total === 1 ? "" : "s"}`, `${n.byState.running} running`];
    if (n.byState.crashed) bits.push(`${n.byState.crashed} crashed`);
    if (n.attention) bits.push(`${n.attention} need${n.attention === 1 ? "s" : ""} attention`);
    const totalText = usageTotalText(state.usageTotal);
    if (totalText && n.byState.running) bits.push(totalText);
    let text;
    let cls = "";
    if (!state.loadedOnce) { text = state.loadError ? "couldn\u2019t read services" : "reading services\u2026"; cls = state.loadError ? "is-error" : "is-busy"; }
    else if (state.loadError) { text = `${bits.join("  \u00B7  ")}  \u00B7  \u26A0 refresh failed \u2014 showing the last state from ${fmtClock(state.lastOk)}`; cls = "is-error"; }
    else {
      bits.push(state.live ? `updated ${fmtClock(state.lastOk)}` : "auto-refresh paused");
      text = bits.join("  \u00B7  ");
    }
    dom.statusLine.textContent = text;
    dom.statusLine.classList.toggle("is-error", cls === "is-error");
    dom.statusLine.classList.toggle("is-busy", cls === "is-busy");
    dom.live.textContent = state.live ? "\u25CF Live" : "\u2016 Paused";
    dom.live.setAttribute("aria-pressed", state.live ? "true" : "false");
    dom.live.classList.toggle("is-paused", !state.live);
  }

  function setStateFilter(id) {
    state.stateFilter = state.stateFilter === id ? "all" : id;
    savePrefs();
    renderChips();
    renderList(true);
    renderSide(true);
  }

  function setFavOnly(on) {
    state.favOnly = Boolean(on);
    savePrefs();
    renderChips();
    renderList(true);
    renderSide(true);
  }

  function renderChips() {
    const n = countEntries(state.entries, state.favorites);
    dom.chips.textContent = "";
    const mk = (id, label, count, colourState, always) => {
      const active = state.stateFilter === id;
      if (!always && !count && !active) return;
      dom.chips.appendChild(el("button", {
        class: "dmn-chip" + (active ? " is-active" : ""), type: "button",
        "data-state": colourState || null, "aria-pressed": active ? "true" : "false",
        onclick: () => setStateFilter(id),
      }, [
        colourState ? el("span", { class: "dmn-chip__dot" }) : null,
        label,
        el("span", { class: "dmn-chip__n" }, String(count)),
      ]));
    };
    mk("all", "All", n.total, null, true);
    // Favorites is a toggle on top of the state chips, not one of them: it
    // narrows whatever else is selected (e.g. Favorites + Crashed).
    dom.chips.appendChild(el("button", {
      class: "dmn-chip dmn-chip--fav" + (state.favOnly ? " is-active" : ""), type: "button",
      "aria-pressed": state.favOnly ? "true" : "false", title: "Show only favorites",
      onclick: () => setFavOnly(!state.favOnly),
    }, [
      el("span", { class: "dmn-chip__star", "aria-hidden": "true" }, "\u2605"),
      "Favorites",
      el("span", { class: "dmn-chip__n" }, String(n.favorites)),
    ]));
    mk("attention", "Attention", n.attention, "attention", true);
    mk("running", "Running", n.byState.running, "running", true);
    mk("starting", "Starting", n.byState.starting, "starting", false);
    mk("stopped", "Stopped", n.byState.stopped, "stopped", true);
    mk("crashed", "Crashed", n.byState.crashed, "crashed", true);
    mk("scheduled", "Scheduled", n.byState.scheduled, "scheduled", false);
  }

  // One place that changes the kind filter, so the select, the saved
  // preference, the list and the overview can't disagree.
  function setKindFilter(kind) {
    state.kindFilter = normalizeKind(kind);
    dom.kind.value = state.kindFilter;
    savePrefs();
    renderList(true);
    renderSide(true);
  }

  function renderKindSelect() {
    // Once the real list is known, a remembered category that has since
    // vanished falls back to "all" (see reconcileKind).
    if (state.loadedOnce) {
      const fixed = reconcileKind(state.kindFilter, state.entries);
      if (fixed !== state.kindFilter) { state.kindFilter = fixed; savePrefs(); }
    }
    const n = countEntries(state.entries);
    dom.kind.textContent = "";
    dom.kind.appendChild(el("option", { value: "all" }, `All services (${n.total})`));
    dom.kind.appendChild(el("option", { value: "builtin" }, `Built-in (${n.kinds.builtin.total})`));
    const group = el("optgroup", { label: "Categories" });
    n.categories.forEach((c) => group.appendChild(el("option", { value: "cat:" + c.key }, `${c.name} (${c.total})`)));
    // Undefined = no category at all. Shown whenever any service has none, or
    // when it is the active filter, so the select never loses its own value.
    if (n.uncategorised.total || state.kindFilter === "undefined") {
      group.appendChild(el("option", { value: "undefined" }, `Undefined (${n.uncategorised.total})`));
    }
    if (group.children.length) dom.kind.appendChild(group);
    // Before the first load there is no vocabulary yet; keep a remembered
    // category selectable rather than showing a blank select.
    if (![...dom.kind.querySelectorAll("option")].some((o) => o.value === state.kindFilter)) {
      dom.kind.appendChild(el("option", { value: state.kindFilter }, state.kindFilter.replace(/^cat:/, "")));
    }
    dom.kind.value = state.kindFilter;
  }

  // Stacked bar of how a set of services splits across states.
  function stateBar(entries, extraClass) {
    const total = entries.length || 1;
    const c = countEntries(entries).byState;
    return el("div", { class: "dmn-bar " + (extraClass || ""), "aria-hidden": "true" },
      STATES.filter((s) => c[s.id]).map((s) =>
        el("i", { "data-state": s.id, style: `width:${(c[s.id] / total * 100).toFixed(2)}%` })));
  }

  function crashBadge(entry) {
    const restarts = Number(entry.restarts) || 0;
    if (!restarts) return null;
    const max = entry.max_restarts != null ? Number(entry.max_restarts) : DEFAULTS.max_restarts;
    const maxed = max > 0 && restarts >= max;
    return el("span", {
      class: "dmn-tag " + (maxed ? "dmn-tag--bad" : "dmn-tag--warn"),
      title: maxed
        ? `Hit its restart limit (${restarts}/${max} in ${RESTART_WINDOW_MIN} min) \u2014 not restarted again until started manually.`
        : `Restarted ${restarts} time${restarts === 1 ? "" : "s"} in the last ${RESTART_WINDOW_MIN} min.`,
    }, maxed ? `\u21BB ${restarts}/${max}` : `\u21BB ${restarts}`);
  }

  // Uptime text lives in a node the 1-second ticker updates in place.
  function uptimeNode(entry) {
    const secs = uptimeSeconds(entry, Date.now());
    if (secs == null) return null;
    return el("span", { class: "dmn-uptime", "data-started": String(entry.started_at) }, fmtDuration(secs));
  }

  // A usage readout the poll rewrites in place (see refreshUsage). Rendered only
  // for a running service; `variant` picks the layout ("card" | "hero").
  function usageNode(entry, variant) {
    if (!entry.running && !entry.supervisor_pid) return null;
    return el("span", {
      class: "dmn-usage dmn-usage--" + variant, "data-usage-id": entry.id,
      title: usageTitle(entry.usage) || null,
    }, usageText(entry.usage));
  }

  // Details tab: one row per process of the selected service, biggest first.
  function usageProcsNode(entry) {
    return el("div", { class: "dmn-procs", "data-usage-procs": entry.id });
  }

  function fillProcs(node, u) {
    node.textContent = "";
    const rows = u && Array.isArray(u.procs) ? u.procs : [];
    if (!rows.length) { node.appendChild(dash()); return; }
    rows.forEach((p) => node.appendChild(el("div", { class: "dmn-procs__row" }, [
      el("code", { class: "dmn-procs__name", title: `pid ${p.pid}` }, `${p.name || "process"} \u00B7 ${p.pid}`),
      el("span", { class: "dmn-procs__cpu" }, fmtCpu(p.cpu_core_percent)),
      el("span", { class: "dmn-procs__mem" }, fmtBytes(p.rss_bytes)),
    ])));
    if (u.processes > rows.length) {
      node.appendChild(el("div", { class: "dmn-dim" }, `\u2026and ${u.processes - rows.length} more`));
    }
  }

  // Called after every poll, whether or not anything repainted: the numbers move
  // constantly and must not cost a repaint (that is what entrySignature avoids).
  function refreshUsage() {
    if (!dom) return;
    dom.overlay.querySelectorAll("[data-usage-id]").forEach((node) => {
      const e = entryById(node.getAttribute("data-usage-id"));
      node.textContent = e ? usageText(e.usage) : "";
      const t = e ? usageTitle(e.usage) : "";
      if (t) node.setAttribute("title", t);
    });
    dom.overlay.querySelectorAll("[data-usage-detail]").forEach((node) => {
      const e = entryById(node.getAttribute("data-usage-detail"));
      const u = e && e.usage;
      const kind = node.getAttribute("data-usage-kind");
      node.textContent = !u ? "\u2014"
        : kind === "cpu" ? `${fmtCpu(u.cpu_percent)} of the machine \u00B7 ${fmtCpu(u.cpu_core_percent)} of one core`
          : kind === "mem" ? fmtBytes(u.rss_bytes)
            : String(u.processes);
    });
    dom.overlay.querySelectorAll("[data-usage-procs]").forEach((node) => {
      const e = entryById(node.getAttribute("data-usage-procs"));
      fillProcs(node, e && e.usage);
    });
  }

  function usageDetail(entry, kind) {
    return el("span", { "data-usage-detail": entry.id, "data-usage-kind": kind }, "\u2014");
  }

  // The star on a card. The card itself is a <button>, so this is a
  // role="button" span (a real <button> inside one is invalid HTML and some
  // browsers won't deliver the click to it). It is a mouse/touch shortcut and
  // is kept out of the tab order: keyboard users have the * key and the real
  // Favorite button in the detail pane, both of which do the same thing.
  function favStar(entry) {
    const on = isFavorite(entry.id);
    return el("span", {
      class: "dmn-star" + (on ? " is-on" : ""), role: "button", tabindex: "-1",
      "aria-pressed": on ? "true" : "false",
      "aria-label": (on ? "Remove " : "Add ") + displayName(entry) + (on ? " from favorites" : " to favorites"),
      title: on ? "Unfavorite" : "Favorite",
      onclick: (ev) => { ev.stopPropagation(); ev.preventDefault(); toggleFavorite(entry.id); },
    }, on ? "\u2605" : "\u2606");
  }

  // One row, clipped: flex-wrap plus a one-line max-height in daemons.css means
  // a chip that doesn't fit wraps onto a hidden second line and is simply not
  // shown - omitted rather than truncated or wrapped. The Details tab always
  // lists them all.
  function categoryRow(entry) {
    const cs = categoriesOf(entry);
    if (!cs.length) return null;
    return el("div", { class: "dmn-card__cats", "aria-label": "Categories" },
      cs.map((c) => el("span", { class: "dmn-tag dmn-tag--cat", title: `Category: ${c}` }, c)));
  }

  function renderCard(entry) {
    const b = bucketOf(entry);
    const active = state.selected === entry.id;
    const next = entry.next_start ? describeNextStart(entry.next_start, Date.now()) : null;
    const tags = [
      crashBadge(entry),
      entry.enabled === false ? el("span", { class: "dmn-tag" }, "Disabled") : null,
      entry.adopted ? el("span", { class: "dmn-tag dmn-tag--info", title: "Started outside Jarvis; Jarvis can see it but didn't launch it." }, "Outside Jarvis") : null,
      entry.autostart ? el("span", { class: "dmn-tag", title: "Marked to start automatically." }, "Autostart") : null,
      next && b !== "running" ? el("span", { class: "dmn-tag dmn-tag--info", title: `Scheduled start ${next.label}` }, `\u25D4 ${next.label.slice(5)}`) : null,
    ].filter(Boolean);
    return el("button", {
      class: "dmn-card" + (active ? " is-active" : ""), type: "button",
      "data-state": b, "data-id": entry.id,
      "aria-current": active ? "true" : null,
      title: entry.description || entry.command || entry.id,
      onclick: () => select(entry.id),
    }, [
      el("div", { class: "dmn-card__row" }, [
        el("span", { class: "dmn-dot", "aria-hidden": "true" }),
        el("span", { class: "dmn-card__name" }, displayName(entry)),
        el("span", { class: "dmn-card__state" }, [STATE[b].label, uptimeNode(entry) ? " \u00B7 " : null, uptimeNode(entry)]),
        favStar(entry),
      ]),
      el("div", { class: "dmn-card__cmd" }, entry.command || "(no command)"),
      usageNode(entry, "card"),
      categoryRow(entry),
      tags.length ? el("div", { class: "dmn-card__meta" }, tags) : null,
    ]);
  }

  function renderGroup(gid, label, all, shown) {
    const filtering = isFiltering();
    const collapsed = !filtering && state.collapsed.has(gid);
    const up = all.filter((e) => bucketOf(e) === "running").length;
    const section = el("div", { class: "dmn-group" + (collapsed ? " is-collapsed" : ""), "data-group": gid }, [
      el("button", {
        class: "dmn-group__head", type: "button", "aria-expanded": collapsed ? "false" : "true",
        onclick: () => {
          if (state.collapsed.has(gid)) state.collapsed.delete(gid); else state.collapsed.add(gid);
          savePrefs();
          renderList(true);
        },
      }, [
        el("span", { class: "dmn-group__caret", "aria-hidden": "true" }),
        el("span", { class: "dmn-group__label" }, label),
        el("span", { class: "dmn-group__count" }, `${up}/${all.length} up`),
        all.length ? el("div", { class: "dmn-group__bar" }, stateBar(all)) : null,
      ]),
    ]);
    shown.forEach((e) => section.appendChild(renderCard(e)));
    if (gid === "custom" && !all.length && !filtering) {
      section.appendChild(el("button", {
        class: "dmn-card dmn-card--empty", type: "button", onclick: () => openEditor("add"),
      }, [
        el("div", { class: "dmn-card__name" }, "No custom daemons yet"),
        el("div", { class: "dmn-card__cmd" }, "Register any long-running command \u2014 a web server, a watcher, a bot \u2014 and Jarvis supervises it. Add one\u2026"),
      ]));
    }
    return section;
  }

  function renderList(force) {
    if (!dom) return;
    const sig = JSON.stringify([state.dataSig, activeFilters(), [...state.favorites].sort(), state.selected, [...state.collapsed], state.loadedOnce, state.loadError && 1]);
    if (!force && sig === state.listSig) return;
    state.listSig = sig;

    const keep = dom.list.scrollTop;
    const shown = visibleEntries();
    dom.count.textContent = isFiltering() ? `${shown.length} of ${state.entries.length}` : String(state.entries.length);
    dom.list.textContent = "";

    if (!state.loadedOnce) {
      dom.list.appendChild(el("div", { class: "debug-empty" }, state.loadError || "Loading\u2026"));
      return;
    }
    if (state.loadError) {
      dom.list.appendChild(el("div", { class: "dmn-banner dmn-banner--bad" }, [
        el("b", null, "Couldn\u2019t refresh."), ` ${state.loadError} Showing the last known state.`,
      ]));
    }
    if (!state.entries.length) {
      dom.list.appendChild(el("div", { class: "debug-empty" }, "No services are registered."));
      return;
    }
    if (isFiltering() && !shown.length) {
      // Favorites on with nothing starred is not "no match", it is "you
      // haven't picked any yet" - say how, rather than show a blank list.
      const noneStarred = state.favOnly && !countEntries(state.entries, state.favorites).favorites;
      dom.list.appendChild(el("div", { class: "dmn-empty-list" }, [
        el("div", null, noneStarred
          ? "No favorites yet. Click the \u2606 on a service (or select it and press *) and it will show up here."
          : "Nothing matches these filters."),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: clearFilters }, "Clear filters"),
      ]));
      return;
    }
    const groups = [
      ["builtin", "Built-in", state.entries.filter((e) => e.builtin)],
      ["custom", "Custom", state.entries.filter((e) => !e.builtin)],
    ];
    groups.forEach(([gid, label, all]) => {
      const inGroup = sortFavoritesFirst(shown.filter((e) => (gid === "builtin") === Boolean(e.builtin)), state.favorites);
      if (isFiltering() && !inGroup.length) return;
      dom.list.appendChild(renderGroup(gid, label, all, inGroup));
    });
    dom.list.scrollTop = keep;
  }

  function clearFilters() {
    state.search = "";
    dom.search.value = "";
    state.stateFilter = "all";
    state.kindFilter = "all";
    state.favOnly = false;
    dom.kind.value = "all";
    savePrefs();
    renderChips();
    renderList(true);
    renderSide(true);
  }

  /* ======================================================================
   * 4b. overview (right pane)
   * ==================================================================== */

  const SVG_NS = "http://www.w3.org/2000/svg";

  function ringNode(fraction, centre, caption) {
    const circ = 2 * Math.PI * 42;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "dmn-ring");
    svg.setAttribute("viewBox", "0 0 100 100");
    svg.setAttribute("aria-hidden", "true");
    const mk = (cls, extra) => {
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", "50"); c.setAttribute("cy", "50"); c.setAttribute("r", "42");
      c.setAttribute("class", cls);
      Object.entries(extra || {}).forEach(([k, v]) => c.setAttribute(k, v));
      return c;
    };
    svg.appendChild(mk("dmn-ring__track"));
    svg.appendChild(mk("dmn-ring__val", {
      "stroke-dasharray": circ.toFixed(2),
      "stroke-dashoffset": (circ * (1 - Math.max(0, Math.min(1, fraction)))).toFixed(2),
    }));
    return el("div", { class: "dmn-ring-label" }, [
      svg,
      el("div", { class: "dmn-ring__pct" }, [centre, el("small", null, caption)]),
    ]);
  }

  function renderSide(force) {
    if (!dom) return;
    const sig = JSON.stringify([state.dataSig, state.stateFilter, state.kindFilter, state.favOnly, state.live, state.loadedOnce, state.loadError && 1]);
    if (!force && sig === state.sideSig) return;
    state.sideSig = sig;
    const keep = dom.side.scrollTop;
    const side = dom.side;
    side.textContent = "";

    const list = state.entries;
    const n = countEntries(list);
    if (!state.loadedOnce) {
      side.appendChild(el("div", { class: "dmn-note" }, state.loadError || "Reading services\u2026"));
      return;
    }

    // ring + summary
    const fraction = n.total ? n.byState.running / n.total : 0;
    let line;
    if (!n.total) line = "Nothing is registered.";
    else if (!n.byState.running) line = "Nothing is running right now.";
    else line = `${n.byState.running} of ${n.total} service${n.total === 1 ? " is" : "s are"} running.`;
    if (n.byState.crashed) line += ` ${n.byState.crashed} crashed.`;
    side.appendChild(el("div", { class: "dmn-ring-wrap" }, [
      ringNode(fraction, `${n.byState.running}/${n.total}`, "up"),
      el("div", { class: "dmn-ring-side" }, [
        stateBar(list),
        el("div", { class: "dmn-ring-side__line" }, line),
      ]),
    ]));

    // by state
    side.appendChild(el("div", null, [
      el("h4", { class: "dmn-section-title" }, "By state"),
      el("div", { class: "dmn-legend" }, STATES.map((s) => el("button", {
        class: "dmn-legend__row" + (state.stateFilter === s.id ? " is-active" : ""), type: "button", "data-state": s.id,
        title: `Show only: ${s.label}`,
        onclick: () => setStateFilter(s.id),
      }, [
        el("span", { class: "dmn-legend__glyph", "aria-hidden": "true" }, s.glyph),
        el("span", null, s.label),
        el("span", { class: "dmn-legend__n" }, String(n.byState[s.id])),
        el("span", { class: "dmn-legend__hint" }, s.hint),
      ]))),
    ]));

    // needs attention
    const attn = [];
    list.forEach((e) => attentionOf(e).forEach((a) => attn.push({ e, a })));
    attn.sort((x, y) => (x.a.level === y.a.level ? 0 : x.a.level === "bad" ? -1 : 1));
    const attnBlock = el("div", null, [
      el("h4", { class: "dmn-section-title" }, ["Needs attention", el("span", { class: "dmn-section-title__aside" }, String(attn.length))]),
    ]);
    if (attn.length) {
      attnBlock.appendChild(el("div", { class: "dmn-attn" }, attn.slice(0, 8).map(({ e, a }) =>
        el("button", {
          class: "dmn-attn__row", type: "button", "data-state": a.level === "bad" ? "crashed" : "starting",
          onclick: () => select(e.id, true),
        }, [
          el("span", { class: "dmn-attn__glyph", "aria-hidden": "true" }, a.level === "bad" ? "\u2717" : "\u21BB"),
          el("span", { class: "dmn-attn__name" }, displayName(e)),
          el("span", { class: "dmn-attn__note" }, `${a.label}: ${a.note}`),
        ]))));
      if (attn.length > 8) attnBlock.appendChild(el("div", { class: "dmn-more" }, `+ ${attn.length - 8} more \u2014 use the Attention filter`));
    } else {
      attnBlock.appendChild(el("div", { class: "dmn-note is-ok" }, "Nothing needs attention."));
    }
    side.appendChild(attnBlock);

    // coming up
    const sched = list.filter((e) => e.next_start).sort((x, y) => String(x.next_start).localeCompare(String(y.next_start)));
    if (sched.length) {
      side.appendChild(el("div", null, [
        el("h4", { class: "dmn-section-title" }, ["Coming up", el("span", { class: "dmn-section-title__aside" }, String(sched.length))]),
        el("div", { class: "dmn-attn" }, sched.slice(0, 6).map((e) => {
          const nx = describeNextStart(e.next_start, Date.now());
          return el("button", { class: "dmn-attn__row", type: "button", "data-state": "scheduled", onclick: () => select(e.id, true) }, [
            el("span", { class: "dmn-attn__glyph", "aria-hidden": "true" }, STATE.scheduled.glyph),
            el("span", { class: "dmn-attn__name" }, displayName(e)),
            el("span", { class: "dmn-attn__note" }, `${nx.label}${nx.relative ? " \u00B7 " + nx.relative : ""}`),
          ]);
        })),
      ]));
    }

    // by category (L.11). Built-in first: it is a real flag (and a lock), not
    // a category. Then every category in use, then Undefined (= none). Each
    // row toggles the same filter as the select in the left pane.
    const MAX_CAT_ROWS = 12;
    const rows = [{ id: "builtin", label: "Built-in", tip: "Show only built-in services", group: list.filter((e) => e.builtin), up: n.kinds.builtin.running }];
    n.categories.slice(0, MAX_CAT_ROWS).forEach((c) => rows.push({
      id: "cat:" + c.key, label: c.name, tip: `Show only services in \u201C${c.name}\u201D`,
      group: list.filter((e) => categoriesOf(e).some((x) => x.toLowerCase() === c.key)), up: c.running,
    }));
    if (n.uncategorised.total || state.kindFilter === "undefined") {
      rows.push({ id: "undefined", label: "Undefined", tip: "Show only services with no category", group: list.filter((e) => !categoriesOf(e).length), up: n.uncategorised.running });
    }
    const catBlock = el("div", null, [
      el("h4", { class: "dmn-section-title" }, "By category"),
      el("div", { class: "dmn-mini" }, rows.map((r) => el("button", {
        class: "dmn-mini__row" + (state.kindFilter === r.id ? " is-active" : ""), type: "button", title: r.tip,
        "aria-pressed": state.kindFilter === r.id ? "true" : "false",
        onclick: () => setKindFilter(state.kindFilter === r.id ? "all" : r.id),
      }, [
        el("span", { class: "dmn-mini__label" }, r.label),
        el("span", { class: "dmn-mini__n" }, `${r.up}/${r.group.length} up`),
        el("div", { class: "dmn-mini__bar" }, r.group.length ? stateBar(r.group) : null),
      ]))),
    ]);
    if (n.categories.length > MAX_CAT_ROWS) {
      catBlock.appendChild(el("div", { class: "dmn-more" }, `+ ${n.categories.length - MAX_CAT_ROWS} more \u2014 use the category filter`));
    }
    side.appendChild(catBlock);

    // actions
    side.appendChild(el("div", null, [
      el("h4", { class: "dmn-section-title" }, "Actions"),
      el("div", { class: "dmn-actions-grid" }, [
        el("button", { class: "btn btn--primary btn--sm btn--wide", type: "button", onclick: () => openEditor("add") }, "+ Add daemon"),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => refreshAll(true) }, "Refresh now"),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: toggleLive }, state.live ? "Pause updates" : "Resume updates"),
        el("button", {
          class: "btn btn--outline btn--sm btn--wide", type: "button",
          onclick: () => copyWithToast(buildReport(state.entries, Date.now()), "the status report"),
        }, "Copy status report (Markdown)"),
      ]),
      el("div", { class: "dmn-note", style: "margin-top:8px" },
        storageOk ? "Your filters and selection are remembered in this browser." : "Browser storage is blocked, so filters won\u2019t be remembered."),
    ]));

    side.appendChild(renderGuide());
    side.scrollTop = keep;
  }

  function renderGuide() {
    const p = (...c) => el("p", null, c);
    return el("details", { class: "dmn-guide" }, [
      el("summary", null, "How daemons work"),
      el("div", { class: "dmn-guide__body" }, [
        p(el("b", null, "Supervisor and process. "), "Starting a daemon launches a small supervisor that runs your command, captures its output and applies the restart policy. The supervisor keeps going after this page closes."),
        p(el("b", null, "Running vs Starting. "), "Starting is the short gap while the supervisor spawns the process, or the wait between automatic restarts. Stop works in both."),
        p(el("b", null, "Crashed vs Stopped. "), "Crashed means the process died on its own (or never started) and the console usually says why. After a reboot or a hard kill nothing ran the supervisor, so those read Stopped, not Crashed."),
        p(el("b", null, "Restart policy. "), "Restarts are counted in a rolling 10-minute window. At the limit Jarvis stops retrying and shows \u201CRestart limit reached\u201D until you start it yourself."),
        p(el("b", null, "Outside Jarvis. "), "A scheduler or Discord gateway that was started in a terminal still shows as running; its console isn\u2019t captured here."),
        p(el("b", null, "Who can create daemons. "), "You can, here. Jarvis\u2019s model can start, stop and inspect them but never register one."),
        p(el("b", null, "Keys. "), "\u2191/\u2193 move, / search, S start/stop, Shift+R restart, E edit, * favorite, N new, 1 console, 2 details, Esc closes."),
      ]),
    ]);
  }

  /* ======================================================================
   * 5. middle pane: the selected service
   * ==================================================================== */

  function renderFoot() {
    const k = (t) => el("kbd", { class: "dmn-kbd" }, t);
    dom.foot.textContent = "";
    if (state.editor) {
      dom.foot.appendChild(el("span", null, [k("Ctrl"), "+", k("Enter"), " save \u00A0", k("Esc"), " cancel"]));
      return;
    }
    dom.foot.appendChild(el("span", null, [
      k("\u2191"), k("\u2193"), " move \u00A0", k("/"), " search \u00A0", k("S"), " start/stop \u00A0",
      k("\u21E7R"), " restart \u00A0", k("E"), " edit \u00A0", k("N"), " new \u00A0", k("1"), k("2"), " tabs \u00A0", k("Esc"), " close",
    ]));
  }

  function renderMain() {
    if (!dom) return;
    if (state.editor) { renderEditor(); renderFoot(); return; }
    const entry = selectedEntry();
    if (!state.loadedOnce) {
      state.shell = null; state.shellFor = null;
      dom.main.textContent = "";
      dom.main.appendChild(el("div", { class: "dmn-empty" }, state.loadError || "Reading services\u2026"));
    } else if (!entry) {
      state.shell = null; state.shellFor = null;
      dom.main.textContent = "";
      dom.main.appendChild(el("div", { class: "dmn-empty" }, state.entries.length
        ? [el("div", { class: "dmn-empty__big" }, "Pick a service"), "Choose one on the left to see its state, output and settings."]
        : [el("div", { class: "dmn-empty__big" }, "No services registered"), "Add a daemon to have Jarvis run and supervise a command for you.",
           el("div", null, el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => openEditor("add") }, "+ Add daemon"))]));
    } else {
      if (state.shellFor !== entry.id || !state.shell || !dom.main.contains(state.shell.root)) buildShell(entry);
      updateShell(entry, true);
    }
    renderFoot();
  }

  /* ---- shell: built once per selected service ------------------------------ */

  function buildShell(entry) {
    dom.main.textContent = "";
    const sh = { root: el("div", { class: "dmn-detail", "data-role": "detail" }), con: {}, det: {}, sig: "" };
    sh.summary = el("div", { class: "dmn-summary", "data-role": "summary" });
    sh.tabs = {
      console: el("button", { class: "dmn-tab", type: "button", role: "tab", "data-tab": "console", onclick: () => setTab("console") }),
      details: el("button", { class: "dmn-tab", type: "button", role: "tab", "data-tab": "details", onclick: () => setTab("details") }),
    };
    sh.tabbar = el("div", { class: "dmn-tabs", role: "tablist" }, [sh.tabs.console, sh.tabs.details]);
    sh.panels = {
      console: buildConsolePanel(sh),
      details: buildDetailsPanel(sh),
    };
    sh.root.append(sh.summary, sh.tabbar, sh.panels.console, sh.panels.details);
    dom.main.appendChild(sh.root);
    state.shell = sh;
    state.shellFor = entry.id;
    applyTab();
  }

  function updateShell(entry, force) {
    const sh = state.shell;
    if (!sh) return;
    const sig = entrySignature(entry) + "|" + (state.busy[entry.id] || "") + "|" + state.showEnv + "|" + (isFavorite(entry.id) ? "fav" : "");
    if (force || sig !== sh.sig) {
      sh.sig = sig;
      renderSummary(entry);
      renderDetails(entry);
    }
    // cheap, every time
    const showStdin = Boolean(entry.running && entry.supports_stdin);
    sh.con.stdin.hidden = !showStdin;
    const errs = summarizeLines(state.consoleItems).errors;
    sh.tabs.console.textContent = "";
    sh.tabs.console.append("Console", errs ? el("span", { class: "dmn-tab__n" }, String(errs)) : "");
    sh.tabs.details.textContent = "Details";
  }

  function setTab(tab) {
    if (tab !== "console" && tab !== "details") return;
    state.tab = tab;
    savePrefs();
    applyTab();
    if (tab === "console") { loadConsole({ force: true }); }
  }

  function applyTab() {
    const sh = state.shell;
    if (!sh) return;
    ["console", "details"].forEach((t) => {
      const on = state.tab === t;
      sh.panels[t].hidden = !on;
      sh.tabs[t].classList.toggle("is-active", on);
      sh.tabs[t].setAttribute("aria-selected", on ? "true" : "false");
    });
    if (state.tab === "console") pinConsole();
  }

  /* ---- summary: title, badges, alerts, actions ------------------------------ */

  function badge(text, cls, title) {
    return el("span", { class: "dmn-tag" + (cls ? " dmn-tag--" + cls : ""), title: title || null }, text);
  }

  function renderSummary(entry) {
    const sh = state.shell;
    const e = withDefaults(entry);
    const b = bucketOf(entry);
    const s = sh.summary;
    s.textContent = "";
    s.setAttribute("data-state", b);

    const up = uptimeNode(entry);
    s.appendChild(el("div", { class: "dmn-hero" }, [
      el("span", { class: "dmn-hero__dot", "aria-hidden": "true" }),
      el("div", { class: "dmn-hero__text" }, [
        el("div", { class: "dmn-title-row" }, [
          el("div", { class: "dmn-title" }, displayName(entry)),
          el("span", { class: "dmn-pill", "data-state": b }, [STATE[b].label, up ? " \u00B7 " : null, up]),
        ]),
        el("div", { class: "dmn-sub" }, [
          el("code", null, entry.id),
          entry.builtin ? " \u00B7 built-in" : " \u00B7 custom",
          entry.pid ? ` \u00B7 pid ${entry.pid}` : "",
        ]),
        usageNode(entry, "hero"),
        entry.description ? el("div", { class: "dmn-does" }, entry.description) : null,
        el("div", { class: "dmn-badges" }, [
          e.enabled ? null : badge("Disabled", "warn"),
          entry.adopted ? badge("Started outside Jarvis", "info", "Jarvis can see this process but didn't launch it, so its output isn't captured here.") : null,
          entry.autostart ? badge("Autostart", null, "Marked to start automatically.") : null,
          entry.supports_stdin ? badge("Reads stdin") : null,
          entry.shell ? badge("Via shell") : null,
          badge(restartSummary(entry), e.restart === "never" ? null : "info"),
        ]),
      ]),
    ]));

    const alerts = alertNodes(entry);
    if (alerts.length) s.appendChild(el("div", { class: "dmn-alerts" }, alerts));

    s.appendChild(actionBar(entry));
  }

  function alertNode(level, label, text, buttons) {
    return el("div", { class: "dmn-alert dmn-alert--" + level }, [
      el("div", { class: "dmn-alert__label" }, label),
      el("div", { class: "dmn-alert__text" }, text),
      buttons && buttons.length ? el("div", { class: "dmn-alert__btns" }, buttons) : null,
    ]);
  }

  function alertNodes(entry) {
    const out = [];
    const errBtn = el("button", {
      class: "btn btn--ghost btn--sm", type: "button",
      onclick: () => { state.consoleFilter = "errors"; savePrefs(); setTab("console"); renderConsole(true); },
    }, "Show errors in console");
    attentionOf(entry).forEach((a) => {
      out.push(alertNode(a.level, a.label, a.note, a.kind === "crashed" ? [errBtn] : null));
    });
    if (entry.enabled === false) {
      out.push(alertNode("warn", "Disabled",
        "It can\u2019t be started \u2014 by hand, by a schedule or by autostart \u2014 until it\u2019s enabled.",
        [el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => quickPatch(entry.id, { enabled: true }, "Enabled") }, "Enable")]));
    }
    if (entry.next_start) {
      const nx = describeNextStart(entry.next_start, Date.now());
      out.push(alertNode("info", "Scheduled start",
        `Will start ${nx.label}${nx.relative ? " (" + nx.relative + ")" : ""}.`,
        [el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => scheduleStart(entry.id, "") }, "Clear schedule")]));
    }
    if (entry.adopted) {
      out.push(alertNode("info", "Started outside Jarvis",
        `Process ${entry.pid || ""} was launched by hand or by another tool. You can stop it from here, but its console isn\u2019t captured.`));
    }
    return out;
  }

  function actionBar(entry) {
    const busy = state.busy[entry.id];
    const stoppable = isStoppable(entry);
    const cantStart = !stoppable && entry.enabled === false;
    const act = (id, glyph, label, key, o) => el("button", {
      class: "dmn-act" + (o && o.primary ? " is-primary" : "") + (o && o.danger ? " dmn-act--danger" : ""),
      type: "button", "data-act": id,
      disabled: Boolean(busy || (o && o.disabled)),
      title: (o && o.title) || null,
      onclick: o && o.onclick,
    }, [
      el("span", { class: "dmn-act__glyph", "aria-hidden": "true" }, glyph),
      el("span", { class: "dmn-act__label" }, label),
      key ? el("span", { class: "dmn-act__key" }, key) : null,
    ]);
    const verb = { start: "Starting\u2026", stop: "Stopping\u2026", restart: "Restarting\u2026", remove: "Removing\u2026" };
    const buttons = [
      stoppable
        ? act("stop", "\u25A0", busy === "stop" ? verb.stop : "Stop", "S", { onclick: () => runAction(entry.id, "stop") })
        : act("start", "\u25B6", busy === "start" ? verb.start : "Start", "S", {
          primary: true, disabled: cantStart, title: cantStart ? "Disabled \u2014 enable it first." : null,
          onclick: () => runAction(entry.id, "start"),
        }),
      act("restart", "\u21BB", busy === "restart" ? verb.restart : "Restart", "\u21E7R", {
        disabled: cantStart, title: cantStart ? "Disabled \u2014 enable it first." : null,
        onclick: () => runAction(entry.id, "restart"),
      }),
      act("edit", "\u270E", "Edit", "E", { onclick: () => openEditor("edit", entry.id) }),
      act("favorite", isFavorite(entry.id) ? "\u2605" : "\u2606", isFavorite(entry.id) ? "Favorited" : "Favorite", "*", {
        title: isFavorite(entry.id) ? "Remove from favorites" : "Add to favorites",
        onclick: () => toggleFavorite(entry.id),
      }),
    ];
    if (!entry.builtin) {
      buttons.push(act("remove", "\u2715", busy === "remove" ? verb.remove : "Remove", "", {
        danger: true, onclick: () => runAction(entry.id, "remove"),
      }));
    }
    return el("div", { class: "dmn-actions", role: "group", "aria-label": "Actions" }, buttons);
  }

  /* ---- console tab ------------------------------------------------------------ */

  function buildConsolePanel(sh) {
    const con = sh.con;
    con.chips = el("div", { class: "dmn-cchips", role: "group", "aria-label": "Filter console lines" });
    con.query = el("input", {
      type: "search", class: "dmn-csearch", placeholder: "Search this output\u2026", autocomplete: "off",
      "aria-label": "Search console output", value: state.consoleQuery,
    });
    con.count = el("span", { class: "dmn-csearch__n" });
    con.only = el("input", { type: "checkbox", checked: state.onlyMatches });
    con.tail = el("select", { class: "dmn-select", "aria-label": "How many lines to load" },
      TAIL_OPTIONS.map((n) => el("option", { value: String(n) }, `Last ${n}`)));
    con.tail.value = String(state.tail);
    con.file = el("select", { class: "dmn-select", "aria-label": "Current console or an older rotated log" });
    con.auto = el("input", { type: "checkbox", checked: state.autoscroll });
    con.copy = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, "Copy");
    con.rot = el("div", { class: "dmn-banner dmn-banner--info", hidden: true });
    con.pane = el("div", { class: "dmn-console", tabindex: "0", role: "log", "aria-label": "Console output" });
    con.jump = el("button", { class: "dmn-jump", type: "button", hidden: true }, "\u2193 Jump to latest");
    con.stdinInput = el("input", { type: "text", class: "dmn-stdin__input", placeholder: "Type a line into this service\u2019s stdin\u2026 (\u2191 recalls)", "aria-label": "Send a line to stdin" });
    con.stdinSend = el("button", { class: "btn btn--primary btn--sm", type: "button" }, "Send");
    con.stdin = el("div", { class: "dmn-stdin", hidden: true }, [con.stdinInput, con.stdinSend]);
    con.meta = el("div", { class: "dmn-cmeta" });

    con.query.addEventListener("input", () => { state.consoleQuery = con.query.value; renderConsole(true); });
    con.query.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && con.query.value) { ev.stopPropagation(); con.query.value = ""; state.consoleQuery = ""; renderConsole(true); }
    });
    con.only.addEventListener("change", () => { state.onlyMatches = con.only.checked; savePrefs(); renderConsole(true); });
    con.tail.addEventListener("change", () => { state.tail = Number(con.tail.value) || 300; savePrefs(); loadConsole({ force: true }); });
    con.file.addEventListener("change", () => { state.logFile = con.file.value; state.consoleRaw = null; loadConsole({ force: true }); });
    con.auto.addEventListener("change", () => setAutoscroll(con.auto.checked));
    con.copy.addEventListener("click", copyConsole);
    con.jump.addEventListener("click", () => { setAutoscroll(true); pinConsole(); });
    con.pane.addEventListener("scroll", onConsoleScroll);
    con.stdinSend.addEventListener("click", sendStdin);
    con.stdinInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); sendStdin(); }
      else if (ev.key === "ArrowUp" && state.stdinHistory.length) {
        ev.preventDefault();
        state.stdinPos = Math.min(state.stdinHistory.length - 1, state.stdinPos + 1);
        con.stdinInput.value = state.stdinHistory[state.stdinHistory.length - 1 - state.stdinPos];
      } else if (ev.key === "ArrowDown") {
        ev.preventDefault();
        state.stdinPos = Math.max(-1, state.stdinPos - 1);
        con.stdinInput.value = state.stdinPos < 0 ? "" : state.stdinHistory[state.stdinHistory.length - 1 - state.stdinPos];
      }
    });

    return el("div", { class: "dmn-tabpanel dmn-tabpanel--console", role: "tabpanel", "data-role": "console" }, [
      el("div", { class: "dmn-cbar" }, [
        con.chips,
        el("div", { class: "dmn-csearch-wrap" }, [con.query, con.count]),
        el("label", { class: "dmn-check", title: "Hide lines that don\u2019t match the search" }, [con.only, "only matching"]),
      ]),
      el("div", { class: "dmn-cbar dmn-cbar--2" }, [
        con.tail, con.file,
        el("label", { class: "dmn-check", title: "Keep the newest line in view" }, [con.auto, "auto-scroll"]),
        con.copy,
      ]),
      con.rot,
      el("div", { class: "dmn-cwrap" }, [con.pane, con.jump]),
      con.stdin,
      con.meta,
    ]);
  }

  function setAutoscroll(on) {
    state.autoscroll = Boolean(on);
    const sh = state.shell;
    if (!sh) return;
    sh.con.auto.checked = state.autoscroll;
    sh.con.jump.hidden = state.autoscroll;
    if (state.autoscroll) pinConsole();
  }

  function pinConsole() {
    const sh = state.shell;
    if (!sh || sh.panels.console.hidden) return;
    state.scrollGuardUntil = performance.now() + 180;
    sh.con.pane.scrollTop = sh.con.pane.scrollHeight;
  }

  // Scrolling up by hand pauses auto-scroll; coming back to the bottom resumes
  // it. Our own repaints move scrollTop too, so they set a short guard.
  function onConsoleScroll() {
    const sh = state.shell;
    if (!sh || performance.now() < state.scrollGuardUntil) return;
    const pane = sh.con.pane;
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 32;
    if (!atBottom && state.autoscroll) setAutoscroll(false);
    else if (atBottom && !state.autoscroll) setAutoscroll(true);
  }

  function renderConsoleChips(counts) {
    const sh = state.shell;
    sh.con.chips.textContent = "";
    CONSOLE_FILTERS.forEach((f) => {
      const active = state.consoleFilter === f.id;
      sh.con.chips.appendChild(el("button", {
        class: "dmn-cchip" + (active ? " is-active" : ""), type: "button", "data-cf": f.id,
        "data-state": f.id === "errors" && counts.errors ? "crashed" : null,
        "aria-pressed": active ? "true" : "false",
        onclick: () => { state.consoleFilter = f.id; savePrefs(); renderConsole(true); },
      }, [f.label, el("span", { class: "dmn-chip__n" }, String(counts[f.id]))]));
    });
  }

  function lineNode(it, query) {
    const hits = query ? highlightSegments(it.text, query) : null;
    const isHit = hits && hits.some((s) => s.hit);
    const body = hits
      ? hits.map((s) => (s.hit ? el("mark", null, s.text) : s.text))
      : [it.text];
    return el("div", { class: `dmn-line dmn-line--${it.cls}` + (it.bad ? " is-bad" : "") + (isHit ? " is-match" : ""), "data-n": String(it.n) }, [
      el("span", { class: "dmn-ln", "aria-hidden": "true" }, String(it.n)),
      el("span", { class: "dmn-tx" }, body),
    ]);
  }

  function renderConsole(rebuild) {
    const sh = state.shell;
    if (!sh) return;
    const con = sh.con;
    const items = state.consoleItems;
    const counts = summarizeLines(items);
    renderConsoleChips(counts);
    const shown = filterLines(items, state.consoleFilter, state.consoleQuery, state.onlyMatches);
    const q = state.consoleQuery.trim() ? state.consoleQuery : "";
    const matches = q ? items.filter((it) => it.text.toLowerCase().includes(q.toLowerCase())).length : 0;
    con.count.textContent = q ? `${matches} match${matches === 1 ? "" : "es"}` : "";

    con.rot.hidden = !state.logFile;
    if (state.logFile) {
      con.rot.textContent = "";
      con.rot.append(`Viewing an older, rotated log (${state.logFile.split(/[\\/]/).pop()}). `,
        el("button", { class: "dmn-link", type: "button", onclick: () => { state.logFile = ""; state.consoleRaw = null; con.file.value = ""; loadConsole({ force: true }); } }, "Back to the live console"));
    }

    if (rebuild) {
      const pane = con.pane;
      const prevTop = pane.scrollTop;
      pane.textContent = "";
      if (state.consoleError) {
        pane.appendChild(el("div", { class: "dmn-console-empty is-error" }, state.consoleError));
      } else if (state.consoleRaw === null) {
        pane.appendChild(el("div", { class: "dmn-console-empty" }, "Loading\u2026"));
      } else if (!items.length) {
        const entry = selectedEntry();
        pane.appendChild(el("div", { class: "dmn-console-empty" },
          state.logFile ? "This log file is empty."
            : entry && entry.adopted ? "This process was started outside Jarvis, so there is no captured output."
              : "No output yet. Start the service and its stdout and stderr appear here."));
      } else if (!shown.length) {
        pane.appendChild(el("div", { class: "dmn-console-empty" }, [
          q ? `No lines match \u201C${q}\u201D` : `No ${CONSOLE_FILTERS.find((f) => f.id === state.consoleFilter).label.toLowerCase()} lines`,
          " in the last ", String(items.length), ". ",
          el("button", { class: "dmn-link", type: "button", onclick: () => { state.consoleFilter = "all"; con.query.value = ""; state.consoleQuery = ""; savePrefs(); renderConsole(true); } }, "Show everything"),
        ]));
      } else {
        const frag = document.createDocumentFragment();
        groupTraces(shown).forEach((block) => {
          if (block.type === "trace") {
            const wrap = el("div", { class: "dmn-trace" }, [el("div", { class: "dmn-trace__head" }, "Traceback")]);
            block.items.forEach((it) => wrap.appendChild(lineNode(it, q)));
            frag.appendChild(wrap);
          } else {
            frag.appendChild(lineNode(block.items[0], q));
          }
        });
        pane.appendChild(frag);
      }
      state.scrollGuardUntil = performance.now() + 180;
      if (state.autoscroll) pane.scrollTop = pane.scrollHeight; else pane.scrollTop = prevTop;
    }

    const stamp = state.consoleAt ? `updated ${fmtClock(state.consoleAt)}` : "";
    con.meta.textContent = items.length
      ? `${shown.length === items.length ? items.length : `${shown.length} of ${items.length}`} line${items.length === 1 ? "" : "s"} \u00B7 ${counts.errors} error line${counts.errors === 1 ? "" : "s"}${state.logFile ? " \u00B7 rotated log" : ""}${stamp ? " \u00B7 " + stamp : ""}`
      : stamp;
    con.jump.hidden = state.autoscroll;
  }

  function updateFilePicker() {
    const sh = state.shell;
    if (!sh) return;
    const want = ["", ...state.backups].join("|");
    if (sh.con.file.dataset.loaded === want && sh.con.file.value === state.logFile) return;
    sh.con.file.dataset.loaded = want;
    sh.con.file.textContent = "";
    sh.con.file.appendChild(el("option", { value: "" }, "Live console"));
    state.backups.forEach((p) => sh.con.file.appendChild(el("option", { value: p }, p.split(/[\\/]/).pop())));
    sh.con.file.value = state.logFile;
    sh.con.file.hidden = !state.backups.length;
  }

  async function loadConsole(opts) {
    const sh = state.shell;
    const id = state.selected;
    if (!sh || !id || state.shellFor !== id) return;
    const force = Boolean(opts && opts.force);
    if (force) { state.consoleError = ""; }
    const file = state.logFile ? `&file=${enc(state.logFile)}` : "";
    try {
      const data = await Api.get(`/api/daemons/${enc(id)}/console?lines=${state.tail}${file}`);
      if (state.selected !== id || state.shell !== sh) return; // moved on while waiting
      const raw = Array.isArray(data.lines) ? data.lines : [];
      state.backups = Array.isArray(data.backups) ? data.backups : [];
      updateFilePicker();
      state.consoleAt = Date.now();
      const joined = raw.join("\n");
      const changed = force || state.consoleRaw === null || state.consoleJoined !== joined || Boolean(state.consoleError);
      state.consoleError = "";
      state.consoleJoined = joined;
      if (changed) { state.consoleRaw = raw; state.consoleItems = annotateLines(raw); }
      renderConsole(changed);
      const entry = selectedEntry();
      if (entry) updateShell(entry, false);
    } catch (err) {
      if (state.selected !== id || state.shell !== sh) return;
      state.consoleError = err.message || "Couldn\u2019t read that console.";
      renderConsole(true);
    }
  }

  async function copyConsole() {
    const shown = filterLines(state.consoleItems, state.consoleFilter, state.consoleQuery, state.onlyMatches);
    if (!shown.length) { toast("Nothing to copy \u2014 the console view is empty.", "warn"); return; }
    await copyWithToast(shown.map((it) => it.text).join("\n"), `${shown.length} line${shown.length === 1 ? "" : "s"}`);
  }

  async function sendStdin() {
    const sh = state.shell;
    const id = state.selected;
    if (!sh || !id) return;
    const text = sh.con.stdinInput.value.trim();
    if (!text) return;
    try {
      await Api.post(`/api/daemons/${enc(id)}/input`, { text });
      state.stdinHistory.push(text);
      state.stdinPos = -1;
      sh.con.stdinInput.value = "";
      setAutoscroll(true);
      setTimeout(() => loadConsole({ force: true }), 500);
    } catch (err) {
      toast(err.message || "Couldn\u2019t send that.", "error");
    }
  }

  /* ---- details tab -------------------------------------------------------------- */

  function kv(label, value, o) {
    return el("div", { class: "dmn-kv" + (o && o.wide ? " dmn-kv--wide" : "") }, [
      el("div", { class: "dmn-kv__k" }, label),
      el("div", { class: "dmn-kv__v" + (o && o.mono ? " is-mono" : "") }, value),
    ]);
  }
  const dash = () => el("span", { class: "dmn-dim" }, "\u2014");
  const copyBtn = (text, what) => el("button", { class: "btn btn--ghost btn--sm dmn-copy", type: "button", onclick: () => copyWithToast(text, what) }, "Copy");

  function buildDetailsPanel(sh) {
    const det = sh.det;
    det.runtime = el("div", { class: "dmn-block" });
    det.config = el("div", { class: "dmn-block" });
    det.quick = el("div", { class: "dmn-block" });
    det.whenInput = el("input", {
      type: "text", class: "dmn-input", id: "dmn-schedule-when", autocomplete: "off",
      placeholder: "e.g. in 2 hours, tomorrow 9am, 2026-10-01 08:00", "aria-label": "When to start",
    });
    det.whenNote = el("div", { class: "dmn-note" });
    det.whenSet = el("button", { class: "btn btn--primary btn--sm", type: "button" }, "Schedule start");
    det.whenClear = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, "Clear");
    det.whenSet.addEventListener("click", () => {
      const v = det.whenInput.value.trim();
      if (!v) { toast("Type when it should start first, e.g. \u201Cin 2 hours\u201D.", "warn"); return; }
      scheduleStart(state.selected, v);
    });
    det.whenClear.addEventListener("click", () => scheduleStart(state.selected, ""));
    det.whenInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") det.whenSet.click(); });
    det.schedule = el("div", { class: "dmn-block" }, [
      el("h4", { class: "dmn-section-title" }, "Start it later"),
      el("div", { class: "dmn-row" }, [det.whenInput, det.whenSet, det.whenClear]),
      det.whenNote,
    ]);
    return el("div", { class: "dmn-tabpanel dmn-tabpanel--details", role: "tabpanel", "data-role": "details", hidden: true }, [
      el("div", { class: "dmn-detail-scroll" }, [det.runtime, det.config, det.quick, det.schedule]),
    ]);
  }

  function switchNode(checked, label, hint, onChange, disabled) {
    const input = el("input", { type: "checkbox", checked, disabled: disabled || null });
    input.addEventListener("change", () => onChange(input.checked, input));
    return el("label", { class: "dmn-switch" }, [
      input, el("span", { class: "dmn-switch__track", "aria-hidden": "true" }),
      el("span", { class: "dmn-switch__text" }, [label, hint ? el("small", null, hint) : null]),
    ]);
  }

  function renderDetails(entry) {
    const sh = state.shell;
    const det = sh.det;
    const e = withDefaults(entry);
    const b = bucketOf(entry);

    // runtime
    det.runtime.textContent = "";
    det.runtime.appendChild(el("h4", { class: "dmn-section-title" }, "Runtime"));
    const restarts = Number(entry.restarts) || 0;
    const max = e.max_restarts;
    const meter = el("div", { class: "dmn-meter", title: `${restarts} restart${restarts === 1 ? "" : "s"} in the last ${RESTART_WINDOW_MIN} min` }, [
      el("i", { "data-state": max > 0 && restarts >= max ? "crashed" : restarts ? "starting" : "running", style: `width:${max > 0 ? Math.min(100, restarts / max * 100) : (restarts ? 100 : 0)}%` }),
    ]);
    det.runtime.appendChild(el("div", { class: "dmn-kvs" }, [
      kv("State", el("span", { class: "dmn-statetext", "data-state": b }, [el("span", { class: "dmn-dot" }), STATE[b].label])),
      kv("Uptime", uptimeNode(entry) || dash()),
      kv("Process id", entry.pid ? String(entry.pid) : dash(), { mono: true }),
      kv("Supervisor id", entry.supervisor_pid ? String(entry.supervisor_pid) : dash(), { mono: true }),
      kv("CPU", entry.running ? usageDetail(entry, "cpu") : dash()),
      kv("Memory", entry.running ? usageDetail(entry, "mem") : dash()),
      entry.running ? kv("Processes", el("div", null, [usageDetail(entry, "procs"), usageProcsNode(entry)]), { wide: true }) : null,
      kv("Started", entry.running && entry.started_at ? fmtStamp(entry.started_at) : dash()),
      kv("Last exit", entry.exit_code != null ? `code ${entry.exit_code}` : dash()),
      kv("Restarts", el("div", null, [`${restarts} of ${max > 0 ? max : "\u221E"} in ${RESTART_WINDOW_MIN} min`, meter])),
      kv("Next start", entry.next_start ? (() => { const nx = describeNextStart(entry.next_start, Date.now()); return `${nx.label}${nx.relative ? " \u00B7 " + nx.relative : ""}`; })() : dash()),
    ].filter(Boolean)));
    refreshUsage();

    // configuration
    det.config.textContent = "";
    det.config.appendChild(el("h4", { class: "dmn-section-title" }, "Configuration"));
    const envRows = e.env && typeof e.env === "object" ? Object.entries(e.env) : [];
    const envNode = envRows.length
      ? el("div", null, [
        el("div", { class: "dmn-env" }, envRows.map(([k, v]) => el("div", { class: "dmn-env__row" }, [
          el("code", { class: "dmn-env__k" }, k), el("span", { class: "dmn-env__eq" }, "="),
          el("code", { class: "dmn-env__v" + (state.showEnv ? "" : " is-masked") }, state.showEnv ? String(v) : "\u2022\u2022\u2022\u2022\u2022\u2022"),
        ]))),
        el("button", { class: "dmn-link", type: "button", onclick: () => { state.showEnv = !state.showEnv; renderDetails(selectedEntry()); } }, state.showEnv ? "Hide values" : "Show values"),
      ])
      : dash();
    const cats = categoriesOf(entry);
    det.config.appendChild(el("div", { class: "dmn-kvs" }, [
      kv("Categories", cats.length
        ? el("div", { class: "dmn-cats" }, cats.map((c) => badge(c, "cat", `Category: ${c}`)))
        : el("span", { class: "dmn-dim" }, "Undefined \u2014 none set"), { wide: true }),
      kv("Command", el("div", { class: "dmn-cmdrow" }, [el("code", { class: "dmn-cmd" }, entry.command || "(none)"), entry.command ? copyBtn(entry.command, "the command") : null]), { wide: true }),
      kv("Working directory", entry.cwd ? entry.cwd : el("span", { class: "dmn-dim" }, "Jarvis\u2019s own folder"), { mono: true }),
      kv("Run via shell", entry.shell ? "Yes" : "No"),
      kv("Reads stdin", entry.supports_stdin ? "Yes" : "No"),
      kv("Restart policy", restartSummary(entry)),
      kv("When stopped", stopSummary(entry)),
      kv("Environment", envNode, { wide: true }),
      entry.notes ? kv("Notes", el("div", { class: "dmn-notes" }, entry.notes), { wide: true }) : null,
      entry.console ? kv("Console file", el("div", { class: "dmn-cmdrow" }, [el("code", { class: "dmn-cmd" }, entry.console), copyBtn(entry.console, "the console path")]), { wide: true }) : null,
    ].filter(Boolean)));

    // quick settings
    det.quick.textContent = "";
    det.quick.appendChild(el("h4", { class: "dmn-section-title" }, "Quick settings"));
    det.quick.appendChild(el("div", { class: "dmn-switches" }, [
      switchNode(e.enabled, "Enabled", "A disabled service can\u2019t be started or scheduled.", (on, input) => quickPatch(entry.id, { enabled: on }, on ? "Enabled" : "Disabled", input)),
      switchNode(Boolean(entry.autostart), "Autostart", "Starts it each time Jarvis starts (the web console or the scheduler coming up). One you stopped by hand stays stopped until then.", (on, input) => quickPatch(entry.id, { autostart: on }, on ? "Autostart on" : "Autostart off", input)),
    ]));
    det.quick.appendChild(el("div", { class: "dmn-note" }, "For anything else \u2014 command, restart policy, environment \u2014 use Edit. Changes apply the next time it starts."));

    // schedule note
    det.whenClear.hidden = !entry.next_start;
    det.whenNote.textContent = entry.next_start
      ? (() => { const nx = describeNextStart(entry.next_start, Date.now()); return `Set: starts ${nx.label}${nx.relative ? " (" + nx.relative + ")" : ""}. It only fires if the scheduler daemon is running.`; })()
      : "One-shot. It only fires if the scheduler daemon is running.";
  }

  /* ======================================================================
   * 6. editor: add a daemon / edit one
   *
   * Replaces the middle pane (the Custom Tools panel's editor works the same
   * way). Built once per open; typing never rebuilds the form, so focus and
   * caret stay where they are. Validation mirrors daemons.py's own rules and
   * only shows an error once a field has been touched (or Save was tried).
   * ==================================================================== */

  const FIELD_LABEL = {
    id: "id", categories: "categories", command: "command", restartDelay: "restart delay", maxRestarts: "max restarts", stopTimeout: "stop timeout",
  };

  function editorDirty() {
    const ed = state.editor;
    return Boolean(ed) && JSON.stringify(ed.draft) !== JSON.stringify(ed.initial);
  }

  async function confirmDiscard() {
    return confirmDialog({
      title: "Discard your changes?",
      body: "The form has edits that haven\u2019t been saved.",
      confirmLabel: "Discard", cancelLabel: "Keep editing", level: "warn", focusCancel: true,
    });
  }

  async function openEditor(mode, id) {
    if (state.editor && editorDirty() && !(await confirmDiscard())) return;
    const entry = mode === "edit" ? entryById(id || state.selected) : null;
    if (mode === "edit" && !entry) { toast("Pick a service to edit first.", "warn"); return; }
    const initial = mode === "edit" ? draftFromEntry(entry) : blankDraft();
    state.editor = {
      mode, id: entry ? entry.id : "", builtin: Boolean(entry && entry.builtin), entry,
      initial, draft: JSON.parse(JSON.stringify(initial)),
      touched: new Set(), attempted: false, saving: false, showEnv: false, error: "", validation: null,
    };
    renderMain();
    revalidate();
    const first = mode === "add" ? $("#dmn-f-id") : $("#dmn-f-name");
    if (first) first.focus();
  }

  async function cancelEditor() {
    if (!state.editor) return;
    if (editorDirty() && !(await confirmDiscard())) return;
    state.editor = null;
    renderMain();
    if (state.shell) loadConsole({ force: true });
  }

  function editorCtx() {
    const ed = state.editor;
    return { mode: ed.mode, builtin: ed.builtin, existingIds: new Set(state.entries.map((e) => e.id)) };
  }

  function revalidate() {
    const ed = state.editor;
    if (!ed || !ed.form) return;
    const v = validateDraft(ed.draft, editorCtx());
    ed.validation = v;
    ed.form.querySelectorAll("[data-err]").forEach((node) => {
      const key = node.getAttribute("data-err");
      const show = Boolean(v.errors[key]) && (ed.attempted || ed.touched.has(key));
      node.textContent = show ? v.errors[key] : "";
      const wrap = node.closest(".dmn-field, .dmn-env-row");
      if (wrap) wrap.classList.toggle("has-error", show);
    });
    // the id hint says what the id will actually become
    if (ed.mode === "add" && ed.idHint) {
      const norm = normalizeId(ed.draft.id);
      ed.idHint.textContent = ed.draft.id.trim() && norm !== ed.draft.id.trim()
        ? `Will be saved as \u201C${norm}\u201D.`
        : "Lower-case letters, digits, dashes and underscores. Used by the CLI and the model\u2019s daemon tools.";
    }
    updateSaveUI();
  }

  function updateSaveUI() {
    const ed = state.editor;
    if (!ed || !ed.save) return;
    const v = ed.validation || { ok: true, order: [] };
    const dirty = editorDirty();
    const blocked = !v.ok || ed.saving || (ed.mode === "edit" && !dirty);
    ed.save.disabled = blocked;
    if (ed.saveStart) ed.saveStart.disabled = blocked;
    ed.save.textContent = ed.saving ? "Saving\u2026" : ed.mode === "add" ? "Register" : "Save changes";
    let msg;
    if (!v.ok) msg = "Fix: " + [...new Set(v.order.map((k) => FIELD_LABEL[k.split(".")[0]] || (k.startsWith("env") ? "environment" : k)))].join(", ");
    else if (ed.mode === "edit" && !dirty) msg = "No changes yet.";
    else msg = ed.mode === "edit" ? "Changes apply the next time it starts." : "Ready to register.";
    ed.status.textContent = msg;
    ed.status.classList.toggle("is-bad", !v.ok && (ed.attempted || ed.touched.size > 0));
  }

  function touch(key) {
    const ed = state.editor;
    ed.touched.add(key);
    revalidate();
  }

  function field(key, label, control, hint, wide) {
    return el("div", { class: "dmn-field" + (wide ? " dmn-field--wide" : ""), "data-field": key }, [
      el("label", { class: "dmn-field__label", for: control.id || null }, label),
      control,
      hint ? el("div", { class: "dmn-field__hint" }, hint) : null,
      el("div", { class: "dmn-field__err", "data-err": key, role: "alert" }),
    ]);
  }

  function textInput(key, o) {
    const ed = state.editor;
    const input = el(o && o.area ? "textarea" : "input", Object.assign({
      id: `dmn-f-${key}`, class: "dmn-input" + (o && o.mono ? " is-mono" : ""),
      autocomplete: "off", spellcheck: "false", value: ed.draft[key],
    }, o && o.area ? { rows: String(o.rows || 3) } : { type: (o && o.type) || "text" }, (o && o.attrs) || {}));
    input.addEventListener("input", () => { ed.draft[key] = input.value; touch(key); });
    input.addEventListener("blur", () => touch(key));
    return input;
  }

  function checkInput(key, label, hint, locked) {
    const ed = state.editor;
    const input = el("input", { type: "checkbox", id: `dmn-f-${key}`, checked: ed.draft[key], disabled: locked || null });
    input.addEventListener("change", () => { ed.draft[key] = input.checked; touch(key); });
    return el("label", { class: "dmn-check dmn-check--block" + (locked ? " is-locked" : ""), for: `dmn-f-${key}` }, [
      input, el("span", null, [label, hint ? el("small", null, hint) : null]),
    ]);
  }

  function lockedNote(text) {
    return el("div", { class: "dmn-locked" }, [el("span", { "aria-hidden": "true" }, "\u{1F512}"), text]);
  }

  function renderEnvRows() {
    const ed = state.editor;
    const list = ed.envList;
    list.textContent = "";
    if (!ed.draft.env.length) {
      list.appendChild(el("div", { class: "dmn-dim" }, "No variables. The process inherits Jarvis\u2019s own environment."));
    }
    ed.draft.env.forEach((row, i) => {
      const k = el("input", { type: "text", class: "dmn-input is-mono", placeholder: "NAME", value: row.k, "aria-label": `Variable ${i + 1} name`, autocomplete: "off", spellcheck: "false" });
      const v = el("input", { type: "text", class: "dmn-input is-mono" + (ed.showEnv ? "" : " is-masked"), placeholder: "value", value: row.v, "aria-label": `Variable ${i + 1} value`, autocomplete: "off", spellcheck: "false" });
      k.addEventListener("input", () => { row.k = k.value; ed.touched.add("env"); ed.touched.add(`env.${i}`); revalidate(); });
      v.addEventListener("input", () => { row.v = v.value; ed.touched.add("env"); revalidate(); });
      const rm = el("button", { class: "btn btn--ghost btn--sm", type: "button", "aria-label": `Remove variable ${i + 1}`, onclick: () => { ed.draft.env.splice(i, 1); ed.touched.add("env"); renderEnvRows(); revalidate(); } }, "\u2715");
      list.appendChild(el("div", { class: "dmn-env-row" }, [k, el("span", { class: "dmn-env__eq" }, "="), v, rm,
        el("div", { class: "dmn-field__err", "data-err": `env.${i}`, role: "alert" })]));
    });
  }

  function renderRestartSummary() {
    const ed = state.editor;
    const d = ed.draft;
    const delay = wholeNumberSafe(d.restartDelay, 5);
    const max = wholeNumberSafe(d.maxRestarts, 5);
    ed.restartSum.textContent = d.restart === "never"
      ? "It stays down when it exits. Delay and limit below are ignored."
      : `${d.restart === "always" ? "Restarts whenever it exits" : "Restarts when it exits with an error"}, waiting ${delay}s each time, ${max > 0 ? `up to ${max} times in ${RESTART_WINDOW_MIN} minutes, then gives up` : "with no limit"}.`;
    ed.restartBox.classList.toggle("is-off", d.restart === "never");
  }
  const wholeNumberSafe = (t, fallback) => (/^\d+$/.test(String(t).trim()) ? parseInt(String(t).trim(), 10) : fallback);

  // The shared chip box (category-input.js). Its suggestions come from the
  // categories already on THIS panel's services only - commands keep their own
  // vocabulary (owner decision Q5). Built-ins can be re-categorised too:
  // category is a label, and does not touch the fields a built-in locks.
  function categoryField() {
    const ed = state.editor;
    const C = cat();
    ed.catInput = C.createInput({
      id: "dmn-f-categories", value: ed.draft.categories, label: "Categories",
      placeholder: "Add a category\u2026 (Enter to add)",
      getVocabulary: () => C.vocabulary(state.entries.map(categoriesOf)),
      onChange: (list) => { ed.draft.categories = list; touch("categories"); },
    });
    const f = field("categories", "Categories", ed.catInput.el,
      `Optional. Group services your own way \u2014 \u201Cchat\u201D, \u201Ctools\u201D \u2014 and filter by them. Up to ${C.MAX_PER_ITEM}, ${C.MAX_NAME_LEN} characters each; with none, a service shows as Undefined.`, true);
    f.querySelector("label").setAttribute("for", ed.catInput.inputId);
    return f;
  }

  function renderEditor() {
    const ed = state.editor;
    const d = ed.draft;
    const builtin = ed.builtin;
    const title = ed.mode === "add" ? "Add a daemon" : `Edit \u2014 ${displayName(ed.entry)}`;

    const form = el("div", { class: "dmn-form", "data-role": "editor" });
    ed.form = form;
    ed.error = "";

    ed.errorBox = el("div", { class: "dmn-banner dmn-banner--bad", hidden: true, role: "alert" });

    // identity
    let idControl;
    if (ed.mode === "add") {
      idControl = textInput("id", { mono: true, attrs: { placeholder: "e.g. web", maxlength: "40" } });
      ed.idHint = el("div", { class: "dmn-field__hint" });
    } else {
      idControl = el("code", { class: "dmn-fixed", id: "dmn-f-id" }, ed.id);
    }
    const identity = el("section", { class: "dmn-fsec" }, [
      el("h4", { class: "dmn-section-title" }, "Identity"),
      el("div", { class: "dmn-grid" }, [
        (() => {
          const f = field("id", "Id", idControl, ed.mode === "add" ? " " : "The id can\u2019t be changed after it\u2019s registered.");
          if (ed.idHint) { f.querySelector(".dmn-field__hint").replaceWith(ed.idHint); }
          return f;
        })(),
        field("name", "Display name", textInput("name", { attrs: { placeholder: ed.mode === "add" ? "optional \u2014 defaults to the id" : "" } }), null),
      ]),
      field("description", "Description", textInput("description", { area: true, rows: 2, attrs: { placeholder: "optional \u2014 shown under the name" } }), null, true),
      categoryField(),
    ]);

    // command
    const commandSec = el("section", { class: "dmn-fsec" }, [
      el("h4", { class: "dmn-section-title" }, "Command"),
      builtin
        ? el("div", { class: "dmn-field dmn-field--wide" }, [
          el("div", { class: "dmn-field__label" }, "Command"),
          el("code", { class: "dmn-cmd dmn-cmd--block" }, ed.entry.command),
          lockedNote("Owned by Jarvis \u2014 a built-in\u2019s command, shell mode and stdin can\u2019t be changed."),
        ])
        : field("command", "Command", textInput("command", { mono: true, attrs: { placeholder: "node server.js --port 3000" } }),
          ed.mode === "edit"
            ? "Shown as stored \u2014 quote any argument that contains spaces. Runs without a shell unless you tick the box below."
            : "Runs without a shell: arguments split on spaces, quote paths that contain spaces. Tick \u201Crun via shell\u201D for pipes and redirects.", true),
      el("div", { class: "dmn-grid" }, [
        field("cwd", "Working directory", textInput("cwd", { mono: true, attrs: { placeholder: "optional \u2014 blank uses Jarvis\u2019s own folder" } }),
          "Must already exist, or it crashes immediately."),
        el("div", { class: "dmn-field" }, [
          checkInput("shell", "Run via shell", "cmd.exe / sh interprets the command, so pipes and redirects work.", builtin),
          checkInput("stdin", "Reads stdin", "Lets you type lines into it from the Console.", builtin),
        ]),
      ]),
    ]);

    // environment
    ed.envList = el("div", { class: "dmn-env-list" });
    const showToggle = el("button", { class: "dmn-link", type: "button" }, "Show values");
    showToggle.addEventListener("click", () => { ed.showEnv = !ed.showEnv; showToggle.textContent = ed.showEnv ? "Hide values" : "Show values"; renderEnvRows(); revalidate(); });
    const envSec = el("section", { class: "dmn-fsec" }, [
      el("h4", { class: "dmn-section-title" }, "Environment variables"),
      ed.envList,
      el("div", { class: "dmn-row" }, [
        el("button", {
          class: "btn btn--ghost btn--sm", type: "button",
          onclick: () => { ed.draft.env.push({ k: "", v: "" }); renderEnvRows(); const ks = ed.envList.querySelectorAll("input"); const last = ks[ks.length - 2]; if (last) last.focus(); revalidate(); },
        }, "+ Variable"),
        showToggle,
      ]),
      el("div", { class: "dmn-field__hint" }, "Added on top of Jarvis\u2019s own environment. Values are hidden here; they\u2019re stored as plain text in the registry."),
    ]);

    // restart policy
    ed.restartSum = el("div", { class: "dmn-field__hint" });
    const cards = el("div", { class: "dmn-radios", role: "radiogroup", "aria-label": "Restart policy" },
      RESTART_POLICIES.map((p) => el("button", {
        class: "dmn-radio" + (d.restart === p.id ? " is-active" : ""), type: "button", role: "radio",
        "aria-checked": d.restart === p.id ? "true" : "false", "data-policy": p.id,
        onclick: () => {
          d.restart = p.id; ed.touched.add("restart");
          cards.querySelectorAll(".dmn-radio").forEach((b) => {
            const on = b.getAttribute("data-policy") === p.id;
            b.classList.toggle("is-active", on); b.setAttribute("aria-checked", on ? "true" : "false");
          });
          renderRestartSummary(); revalidate();
        },
      }, [el("span", { class: "dmn-radio__label" }, p.label), el("span", { class: "dmn-radio__hint" }, p.hint)])));
    ed.restartBox = el("div", { class: "dmn-grid dmn-grid--3" }, [
      field("restartDelay", "Wait before restarting (s)", textInput("restartDelay", { attrs: { inputmode: "numeric" } }), null),
      field("maxRestarts", "Max restarts per 10 min", textInput("maxRestarts", { attrs: { inputmode: "numeric" } }), "0 means no limit."),
    ]);
    ["restartDelay", "maxRestarts"].forEach((k) => {
      const node = ed.restartBox.querySelector(`#dmn-f-${k}`);
      node.addEventListener("input", renderRestartSummary);
    });
    const restartSec = el("section", { class: "dmn-fsec" }, [
      el("h4", { class: "dmn-section-title" }, "Restart policy"),
      cards, ed.restartBox, ed.restartSum,
    ]);

    // stop behaviour
    const sig = el("select", { class: "dmn-input", id: "dmn-f-stopSignal" }, STOP_SIGNALS.map((s) =>
      el("option", { value: s }, s === "TERM" ? "TERM \u2014 ask politely" : s === "INT" ? "INT \u2014 like Ctrl+C" : "KILL \u2014 force immediately")));
    sig.value = d.stopSignal;
    sig.addEventListener("change", () => { d.stopSignal = sig.value; touch("stopSignal"); });
    const stopSec = el("section", { class: "dmn-fsec" }, [
      el("h4", { class: "dmn-section-title" }, "When it\u2019s stopped"),
      el("div", { class: "dmn-grid dmn-grid--3" }, [
        field("stopSignal", "Signal", sig, "Windows always force-kills."),
        field("stopTimeout", "Force-kill after (s)", textInput("stopTimeout", { attrs: { inputmode: "numeric" } }), "How long to wait for a clean exit."),
      ]),
    ]);

    // options
    const optSec = el("section", { class: "dmn-fsec" }, [
      el("h4", { class: "dmn-section-title" }, "Options"),
      checkInput("autostart", "Autostart", "Starts it each time Jarvis starts (the web console or the scheduler coming up). One you stopped by hand stays stopped until then."),
      ed.mode === "edit" ? checkInput("enabled", "Enabled", "A disabled service can\u2019t be started or scheduled.") : null,
      field("notes", "Notes", textInput("notes", { area: true, rows: 3, attrs: { placeholder: "anything worth remembering about this service" } }), null, true),
    ]);

    // footer
    ed.status = el("span", { class: "dmn-editor-status", "aria-live": "polite" });
    ed.save = el("button", { class: "btn btn--primary btn--sm", type: "button", "data-act": "save" }, ed.mode === "add" ? "Register" : "Save changes");
    ed.save.addEventListener("click", () => saveEditor(false));
    ed.saveStart = ed.mode === "add" ? el("button", { class: "btn btn--outline btn--sm", type: "button", "data-act": "save-start" }, "Register & start") : null;
    if (ed.saveStart) ed.saveStart.addEventListener("click", () => saveEditor(true));
    const cancel = el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: cancelEditor }, "Cancel");
    const foot = el("div", { class: "dmn-editor-foot" }, [ed.status, el("div", { class: "dmn-row" }, [cancel, ed.saveStart, ed.save])]);

    form.append(ed.errorBox, identity, commandSec, envSec, restartSec, stopSec, optSec);
    dom.main.textContent = "";
    state.shell = null; state.shellFor = null;
    dom.main.appendChild(el("div", { class: "dmn-editor", "data-mode": ed.mode }, [
      el("div", { class: "dmn-editor-head" }, [
        el("div", { class: "dmn-title" }, title),
        el("div", { class: "dmn-sub" }, ed.mode === "add"
          ? "Register a long-running command. Jarvis supervises it, captures its output and can restart it."
          : builtin ? "Built-in service \u2014 the command is fixed; everything else is yours to tune." : `Registered service \u00B7 ${ed.id}`),
      ]),
      el("div", { class: "dmn-editor-body" }, [form]),
      foot,
    ]));
    renderEnvRows();
    renderRestartSummary();
  }

  function focusFirstInvalid() {
    const ed = state.editor;
    const key = ed.validation && ed.validation.order[0];
    if (!key) return;
    const node = key.startsWith("env.")
      ? ed.envList.querySelectorAll(".dmn-env-row")[Number(key.split(".")[1])]?.querySelector("input")
      : $(`#dmn-f-${key}`);
    if (node) { node.focus(); if (node.scrollIntoView) node.scrollIntoView({ block: "center" }); }
  }

  async function saveEditor(startAfter) {
    const ed = state.editor;
    if (!ed || ed.saving) return;
    ed.attempted = true;
    revalidate();
    if (!ed.validation.ok) { focusFirstInvalid(); return; }

    let body;
    if (ed.mode === "edit") {
      body = buildEditPayload(ed.draft, ed.initial, { builtin: ed.builtin });
      if (!Object.keys(body).length) { toast("Nothing has changed.", "info"); return; }
    } else {
      body = buildAddPayload(ed.draft);
    }
    ed.saving = true;
    ed.errorBox.hidden = true;
    updateSaveUI();
    try {
      const id = ed.mode === "add" ? body.id : ed.id;
      const wasLive = ed.mode === "edit" && isStoppable(entryById(id));
      if (ed.mode === "add") await Api.post("/api/daemons", body);
      else await Api.patch(`/api/daemons/${enc(id)}`, body);
      toast(ed.mode === "add" ? `Registered \u2018${id}\u2019.` : (wasLive ? "Saved. Restart it to apply the changes." : "Saved."), "success");
      state.editor = null;
      if (state.selected !== id) resetConsoleState();
      state.selected = id;
      savePrefs();
      await loadEntries();
      renderMain();
      loadConsole({ force: true });
      if (startAfter) runAction(id, "start");
    } catch (err) {
      ed.saving = false;
      ed.errorBox.hidden = false;
      ed.errorBox.textContent = "";
      ed.errorBox.append(el("b", null, "Couldn\u2019t save."), ` ${err.message || "Unknown error."}`);
      if (ed.errorBox.scrollIntoView) ed.errorBox.scrollIntoView({ block: "nearest" });
      updateSaveUI();
    }
  }

  /* ======================================================================
   * 7. actions, polling, keyboard, open / close
   * ==================================================================== */

  function resetConsoleState() {
    state.logFile = "";
    state.backups = [];
    state.consoleRaw = null;
    state.consoleJoined = "";
    state.consoleItems = [];
    state.consoleError = "";
    state.consoleQuery = "";
    state.consoleSig = "";
    state.autoscroll = true;
    state.showEnv = false;
  }

  async function select(id, reveal) {
    if (state.editor) {
      if (editorDirty() && !(await confirmDiscard())) return;
      state.editor = null;
    }
    if (state.selected !== id) resetConsoleState();
    state.selected = id;
    savePrefs();
    renderList(true);
    renderMain();
    if (reveal && dom.list.querySelector(".dmn-card.is-active")) {
      dom.list.querySelector(".dmn-card.is-active").scrollIntoView({ block: "nearest" });
    }
    loadConsole({ force: true });
  }

  function moveSelection(delta) {
    const rows = navEntries();
    if (!rows.length) return;
    const i = rows.findIndex((e) => e.id === state.selected);
    const next = rows[Math.max(0, Math.min(rows.length - 1, i + delta))];
    if (next && next.id !== state.selected) select(next.id, true);
  }

  /* ---- data ------------------------------------------------------------------ */

  function loadEntries() {
    if (state.inflight) return state.inflight;
    state.inflight = (async () => {
      try {
        const data = await Api.get("/api/daemons");
        state.entries = Array.isArray(data.daemons) ? data.daemons : [];
        state.usageTotal = data.usage_total && typeof data.usage_total === "object" ? data.usage_total : null;
        state.loadedOnce = true;
        state.loadError = "";
        state.lastOk = Date.now();
      } catch (err) {
        state.loadError = err.message || "Failed.";
      } finally {
        state.inflight = null;
      }
      onEntries();
    })();
    return state.inflight;
  }

  // Favorites come from the server, separately from the (CLI-backed) service
  // list, so a failure here never blanks the panel: the stars just don't show.
  async function loadFavorites() {
    try {
      const data = await Api.get("/api/favorite-daemons");
      state.favorites = new Set(Array.isArray(data) ? data.filter((x) => typeof x === "string") : []);
      state.favLoaded = true;
    } catch (_) { /* keep whatever we had */ }
    if (dom) onFavoritesChanged();
  }

  function onFavoritesChanged() {
    renderChips();
    renderList(true);
    renderSide(true);
    const entry = selectedEntry();
    if (entry && state.shell && !state.editor) updateShell(entry, true);
  }

  async function toggleFavorite(id) {
    if (!entryById(id)) return;
    // Never save before the saved list has been read once: the POST replaces
    // the whole list, so saving first would wipe every existing favorite.
    if (!state.favLoaded) {
      await loadFavorites();
      if (!state.favLoaded) { toast("Couldn\u2019t read your favorites, so nothing was changed.", "error"); return; }
    }
    const { ids } = toggleFavoriteId([...state.favorites], id);
    state.favorites = new Set(ids);
    onFavoritesChanged();
    // Saves run one after another and each sends the set as it is *then*, so
    // two quick clicks can't land out of order and drop the second one.
    state.favSaving = state.favSaving.then(async () => {
      try {
        await Api.post("/api/favorite-daemons", { ids: [...state.favorites] });
      } catch (err) {
        toast(`Couldn\u2019t save favorites: ${err.message}`, "error");
        await loadFavorites(); // show what the server really has
      }
    });
    return state.favSaving;
  }

  function onEntries() {
    if (!dom) return;
    state.dataSig = state.entries.map(entrySignature).join("|");
    let selectionChanged = false;
    if (state.loadedOnce && !state.editor) {
      const before = state.selected;
      if (state.selected && !entryById(state.selected)) state.selected = null;
      if (!state.selected && state.entries.length) state.selected = (navEntries()[0] || state.entries[0]).id;
      if (state.selected !== before) { selectionChanged = true; resetConsoleState(); savePrefs(); }
    }
    renderStatusLine();
    renderChips();
    renderKindSelect();
    renderList();
    renderSide();
    if (state.editor) { refreshUsage(); return; }
    const entry = selectedEntry();
    if (selectionChanged || !entry || !state.shell || state.shellFor !== entry.id) {
      renderMain();
      if (entry) loadConsole({ force: true });
    } else {
      updateShell(entry, false);
    }
    refreshUsage();
  }

  async function refreshAll(manual) {
    if (manual) { dom.statusLine.textContent = "refreshing\u2026"; dom.statusLine.classList.add("is-busy"); loadFavorites(); }
    await loadEntries();
    if (state.shell && !state.editor && state.tab === "console") await loadConsole({ force: Boolean(manual) });
  }

  function toggleLive() {
    state.live = !state.live;
    renderStatusLine();
    renderSide(true);
    if (state.live) refreshAll();
  }

  /* ---- actions ---------------------------------------------------------------- */

  async function runAction(id, action) {
    const entry = entryById(id);
    if (!entry || state.busy[id]) return;
    const name = displayName(entry);
    const e = withDefaults(entry);
    const live = isStoppable(entry);

    if (action === "stop") {
      const ok = await confirmDialog({
        title: `Stop \u201C${name}\u201D?`,
        body: `It\u2019s asked to shut down (${e.stop_signal}) and force-killed if it hasn\u2019t exited after ${e.stop_timeout}s. It stays down until you start it again.`,
        confirmLabel: "Stop", level: "warn",
      });
      if (!ok) return;
    } else if (action === "restart" && live) {
      const ok = await confirmDialog({
        title: `Restart \u201C${name}\u201D?`,
        body: "It goes down briefly while it stops and starts again. Anything relying on it is interrupted meanwhile.",
        confirmLabel: "Restart", level: "warn",
      });
      if (!ok) return;
    } else if (action === "remove") {
      const ok = await confirmDialog({
        title: `Remove \u201C${name}\u201D?`,
        body: (live ? "It\u2019s still running, so it will be stopped first. " : "") + "It\u2019s deleted from the registry. Its console logs stay on disk.",
        confirmLabel: "Remove", level: "error", focusCancel: true,
      });
      if (!ok) return;
    }

    state.busy[id] = action;
    if (state.selected === id && state.shell) updateShell(entry, true);
    try {
      if (action === "remove") {
        if (live) await Api.post(`/api/daemons/${enc(id)}/stop`);
        await Api.del(`/api/daemons/${enc(id)}`);
        toast(`Removed \u201C${name}\u201D. Its console logs stay on disk.`, "info");
        if (state.selected === id) { state.selected = null; resetConsoleState(); }
        state.favorites.delete(id); // the server prunes its copy on a clean removal
      } else {
        const data = await Api.post(`/api/daemons/${enc(id)}/${action}`);
        toast(data.message || `${action} requested.`, "success");
      }
    } catch (err) {
      toast(`Couldn\u2019t ${action} \u201C${name}\u201D: ${err.message}`, "error");
    } finally {
      delete state.busy[id];
    }
    await loadEntries();
    if (state.shell && state.selected === id) { updateShell(selectedEntry(), true); loadConsole({ force: true }); }
    if (action !== "remove") {
      // starting is asynchronous by design; settle on the real state
      setTimeout(() => { if (isOpen()) refreshAll(); }, 1200);
      setTimeout(() => { if (isOpen()) refreshAll(); }, 3500);
    }
  }

  async function quickPatch(id, fields, label, input) {
    try {
      await Api.patch(`/api/daemons/${enc(id)}`, fields);
      toast(label, "success");
    } catch (err) {
      toast(err.message || "Couldn\u2019t save that.", "error");
      if (input) input.checked = !input.checked;
    }
    await loadEntries();
  }

  async function scheduleStart(id, when) {
    if (!id) return;
    try {
      const data = await Api.post(`/api/daemons/${enc(id)}/schedule`, { when });
      const nx = when && data.next_start ? describeNextStart(data.next_start, Date.now()) : null;
      toast(when ? `Will start ${nx ? nx.label : when}.` : "Schedule cleared.", "success");
      if (state.shell) state.shell.det.whenInput.value = "";
    } catch (err) {
      toast(err.message || "Couldn\u2019t schedule that.", "error");
    }
    await loadEntries();
  }

  /* ---- polling + uptime ticker ------------------------------------------------- */

  const isOpen = () => Boolean(dom && !dom.overlay.hidden);

  function schedulePoll() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    if (!isOpen()) { state.pollTimer = null; return; }
    const fast = Object.keys(state.busy).length > 0 || state.entries.some((e) => bucketOf(e) === "starting");
    state.pollTimer = setTimeout(pollTick, fast ? 1500 : 4000);
  }

  async function pollTick() {
    state.pollTimer = null;
    if (!isOpen()) return;
    if (state.live && !document.hidden) {
      await loadEntries();
      const entry = selectedEntry();
      if (entry && state.shell && !state.editor && !state.logFile && state.tab === "console") {
        // A running service streams output; a stopped one only needs a re-read
        // when its state just changed (that's when the traceback arrives).
        const sig = entrySignature(entry);
        if (entry.running || bucketOf(entry) === "starting" || sig !== state.consoleSig || state.consoleRaw === null) {
          state.consoleSig = sig;
          await loadConsole();
        }
      }
    }
    schedulePoll();
  }

  function tickUptimes() {
    if (!isOpen()) return;
    const now = Date.now() / 1000;
    dom.overlay.querySelectorAll(".dmn-uptime").forEach((node) => {
      const started = Number(node.getAttribute("data-started"));
      if (started > 0) node.textContent = fmtDuration(now - started);
    });
  }

  /* ---- keyboard ------------------------------------------------------------------ */

  function cycleConsoleFilter() {
    const i = CONSOLE_FILTERS.findIndex((f) => f.id === state.consoleFilter);
    state.consoleFilter = CONSOLE_FILTERS[(i + 1) % CONSOLE_FILTERS.length].id;
    savePrefs();
    if (state.shell) renderConsole(true);
  }

  function onKey(e) {
    if (!isOpen()) return;
    // A confirm / prompt modal owns the keyboard while it is up.
    if (document.querySelector(".jui-backdrop")) return;
    const t = e.target;
    const typing = Boolean(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable));

    if (e.key === "Escape") {
      if (state.editor) { e.preventDefault(); cancelEditor(); return; }
      if (t === dom.search && dom.search.value) { state.search = ""; dom.search.value = ""; renderList(true); return; }
      if (typing) { t.blur(); return; }
      close();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && state.editor) { e.preventDefault(); saveEditor(false); return; }

    if (t === dom.search) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
      else if (e.key === "Enter") { e.preventDefault(); dom.search.blur(); }
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (state.editor) return; // the form owns single-letter keys

    const entry = selectedEntry();
    switch (e.key) {
      case "/": e.preventDefault(); dom.search.focus(); return;
      case "ArrowDown": case "j": e.preventDefault(); moveSelection(1); return;
      case "ArrowUp": case "k": e.preventDefault(); moveSelection(-1); return;
      case "s": if (entry) { e.preventDefault(); runAction(entry.id, isStoppable(entry) ? "stop" : "start"); } return;
      case "R": if (entry) { e.preventDefault(); runAction(entry.id, "restart"); } return;
      case "e": if (entry) { e.preventDefault(); openEditor("edit", entry.id); } return;
      case "*": if (entry) { e.preventDefault(); toggleFavorite(entry.id); } return;
      case "n": e.preventDefault(); openEditor("add"); return;
      case "1": e.preventDefault(); setTab("console"); return;
      case "2": e.preventDefault(); setTab("details"); return;
      case "a": if (state.shell && state.tab === "console") { e.preventDefault(); setAutoscroll(!state.autoscroll); } return;
      case "f": if (state.shell && state.tab === "console") { e.preventDefault(); cycleConsoleFilter(); } return;
      case "p": e.preventDefault(); toggleLive(); return;
      case "r": e.preventDefault(); refreshAll(true); return;
      default: break;
    }
    if (["PageDown", "PageUp", "Home", "End"].includes(e.key) && state.shell && state.tab === "console") {
      const pane = state.shell.con.pane;
      if (t === pane) return; // it is focused: the browser already scrolls it
      e.preventDefault();
      state.scrollGuardUntil = 0;
      if (e.key === "PageDown") pane.scrollTop += pane.clientHeight * 0.9;
      else if (e.key === "PageUp") pane.scrollTop -= pane.clientHeight * 0.9;
      else if (e.key === "Home") pane.scrollTop = 0;
      else { setAutoscroll(true); }
    }
  }

  /* ---- open / close ----------------------------------------------------------------- */

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;
    loadPrefs();

    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderList(true); });
    dom.kind.addEventListener("change", () => { state.kindFilter = dom.kind.value; savePrefs(); renderList(true); renderSide(true); });
    dom.live.addEventListener("click", toggleLive);
    $("#btn-daemon-refresh").addEventListener("click", () => refreshAll(true));
    $("#btn-daemon-add-open").addEventListener("click", () => openEditor("add"));
    $("#daemons-close").addEventListener("click", () => close());
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);
    document.addEventListener("visibilitychange", () => { if (isOpen() && !document.hidden && state.live) refreshAll(); });

    state.built = true;
    return true;
  }

  function open() {
    // The category filter and editor field are built from it; failing here, with
    // a reason, beats a half-rendered panel and a TypeError.
    if (!global.JarvisCategories) {
      toast("category-input.js didn\u2019t load, so the Daemons panel can\u2019t open.", "error");
      return;
    }
    if (!ensureBuilt()) {
      toast("Daemons markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    dom.search.value = state.search;
    renderStatusLine();
    renderChips();
    renderKindSelect();
    renderList(true);
    renderSide(true);
    renderMain();
    loadFavorites();
    loadEntries().then(() => {
      const card = dom.list.querySelector(".dmn-card.is-active");
      if (card && card.scrollIntoView) card.scrollIntoView({ block: "center" });
    });
    if (state.tickTimer) clearInterval(state.tickTimer);
    state.tickTimer = setInterval(tickUptimes, 1000);
    schedulePoll();
  }

  async function close() {
    if (!dom || dom.overlay.hidden) return;
    if (state.editor && editorDirty() && !(await confirmDiscard())) return;
    state.editor = null;
    dom.overlay.hidden = true;
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
    if (state.tickTimer) { clearInterval(state.tickTimer); state.tickTimer = null; }
    // The shell belongs to the visible pane; drop it so the next open rebuilds
    // from fresh data instead of showing a stale console for a moment.
    state.shell = null; state.shellFor = null;
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  global.JarvisDaemons = {
    open, close, isOpen,
    // Pure helpers, exposed for tests/verify_daemons_panel.js only.
    _pure: {
      normalizeId, bucketOf, withDefaults, isStoppable, fmtDuration, uptimeSeconds, fmtStamp,
      fmtBytes, fmtCpu, usageText, usageTitle, usageTotalText,
      describeNextStart, restartSummary, stopSummary, attentionOf, searchHaystack, matchesFilters,
      countEntries, categoriesOf, normalizeKind, reconcileKind, sortFavoritesFirst, toggleFavoriteId, entrySignature, classifyLine, annotateLines, summarizeLines, filterLines,
      groupTraces, highlightSegments, draftFromEntry, blankDraft, validateDraft, envPairs,
      buildAddPayload, buildEditPayload, buildReport, STATES, CONSOLE_FILTERS,
    },
  };
})(window);
