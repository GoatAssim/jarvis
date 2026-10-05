/* ============================================================================
 * agent-panel.js — the Focus layout's "Agent" side panel.
 *
 * WHAT IT IS
 * ----------
 * While dev_agent (builds a NEW project) or code_agent (works inside an
 * EXISTING codebase) is running, Focus opens a panel beside the conversation
 * that shows the codebase Jarvis is working on, the file it is on right now,
 * a file tree with a status on every file Jarvis has touched, and a short log
 * of installs / runs / shell commands. Classic layout is untouched: the same
 * events still draw the stepper card in the chat there.
 *
 * WHERE THE DATA COMES FROM (no new server route)
 * -----------------------------------------------
 * Both agents already print one JARVIS_MEDIA\tdev_agent\t<json> line per step
 * (jarvis-cli/jarvis/dev_agent_events.py). app.js's addAskPromptTrace hands
 * every one of them to feed(); a reloaded conversation hands its saved `steps`
 * to restore(). The panel therefore knows only what the agent REPORTED, which
 * is deliberate:
 *   - it never lists a directory itself, so it adds no way to read the disk;
 *   - it shows PATHS and one-line outcomes only, never file contents, so a
 *     masked .env value (code_agent F.5) cannot leak through it.
 * dev_agent's plan:start carries project_dir and plan:ok carries the whole file
 * list; code_agent's plan:start carries root and each list_dir result carries
 * a capped `listing` (code_agent.LISTING_EVENT_CAP).
 *
 * BEHAVIOUR DECISIONS (change any on request)
 * -------------------------------------------
 *   - Opens by itself when a NEW job starts, in Focus only. It never re-opens
 *     for later events of the same job, so closing it mid-run sticks.
 *   - Docked beside the conversation (the stage steps aside) at >= 1000 px;
 *     below that it is an overlay drawer with the Focus scrim.
 *   - The "Agent" button in the Ask header appears with the first job and
 *     glows while one is running.
 *   - restore() never replaces a job that is still running.
 *
 * Standalone like daemons.js / backlog.js: no reach into app.js internals.
 * The reducer and the tree builder are pure and exposed on _pure so they can
 * be run outside a browser.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ---- pure: paths -------------------------------------------------------- */

  const WIN_ABS_RE = /^[a-zA-Z]:\//;

  // Turn whatever an agent event carried (relative, "./x", absolute inside the
  // root, Windows separators) into a root-relative, forward-slash path.
  // "" means the root itself; null means "not inside the root / unusable", so
  // the caller logs it instead of drawing it as a file.
  function normPath(raw, root) {
    let p = String(raw == null ? "" : raw).trim().replace(/\\/g, "/");
    if (!p) return "";
    const r = String(root || "").trim().replace(/\\/g, "/").replace(/\/+$/, "");
    if (r && p.length >= r.length && p.slice(0, r.length).toLowerCase() === r.toLowerCase()
        && (p.length === r.length || p[r.length] === "/")) {
      p = p.slice(r.length);
    } else if (p.startsWith("/") || WIN_ABS_RE.test(p) || p.startsWith("//")) {
      return null;                      // absolute, and not under the root
    }
    const out = [];
    for (const seg of p.split("/")) {
      if (!seg || seg === ".") continue;
      if (seg === "..") { if (!out.length) return null; out.pop(); continue; }
      out.push(seg);
    }
    return out.join("/");
  }

  function baseName(p) {
    const i = p.lastIndexOf("/");
    return i === -1 ? p : p.slice(i + 1);
  }

  const firstLine = (s) => String(s == null ? "" : s).split("\n")[0].trim();
  const clip = (s, n) => {
    const t = String(s == null ? "" : s);
    return t.length <= n ? t : t.slice(0, n - 1) + "\u2026";
  };

  /* ---- pure: the job model + reducer --------------------------------------- */

  const FILE_TOOLS = { read_file: 1, edit_file: 1, write_file: 1 };
  const VERBS = {
    read_file: "Reading", list_dir: "Listing", search_code: "Searching",
    edit_file: "Editing", write_file: "Creating", run_shell: "Running",
  };
  const LOG_CAP = 60;

  // A later status replaces an earlier one only if it is at least as strong, so
  // a file that was edited does not drop back to "read" when it is read again.
  // writing/fixing are transient (anything resolves them); failed is overridden
  // by any real outcome but not by "we merely know the file exists".
  const RANK = { listed: 0, planned: 1, read: 2, written: 3, fixed: 3, edited: 4, created: 4 };
  // Statuses that mean the file's content on disk was changed by the agent.
  const CHANGED = { written: 1, fixed: 1, edited: 1, created: 1 };
  // In-flight statuses: they overlay the file's real status for a moment and
  // must never erase it (setFile keeps the real one in `settled`).
  const TRANSIENT = { writing: 1, fixing: 1 };
  function stronger(oldS, newS) {
    if (!oldS) return true;
    if (newS === "writing" || newS === "fixing" || newS === "failed") return true;
    if (oldS === "writing" || oldS === "fixing") return true;
    if (oldS === "failed") return newS !== "listed" && newS !== "planned";
    return RANK[newS] >= RANK[oldS];
  }

  function newJob(event, now, convId) {
    const code = event.phase === "step" || event.root !== undefined || event.task !== undefined;
    return {
      id: event.job_id, kind: code ? "code" : "dev",
      root: "", task: "", status: "running", error: "",
      startedAt: now == null ? null : now, endedAt: null, convId: convId == null ? null : convId,
      files: {}, dirs: {}, current: null, log: [], pending: null,
    };
  }

  function setFile(job, rawPath, status, extra) {
    const path = normPath(rawPath, job.root);
    if (!path) return null;
    let f = job.files[path];
    if (!f) { f = job.files[path] = { path, status: null, bytes: null, note: "", settled: null }; }
    // `settled` is the file's real status while a transient one is showing.
    const real = TRANSIENT[f.status] ? f.settled : f.status;
    if (TRANSIENT[status]) {
      if (!TRANSIENT[f.status]) f.settled = f.status;
      f.status = status;
    } else if (status === "failed") {
      // A step that failed on a file the agent had ALREADY changed (a second
      // edit whose old_str did not match, say) did not undo the first change:
      // keep showing it as changed; the failure is in the log. `force` is for
      // a failed fix, where the file is still broken and "failed" is the truth.
      f.status = (CHANGED[real] && !(extra && extra.force)) ? real : "failed";
      if (f.status !== "failed" && extra) extra = Object.assign({}, extra, { note: undefined });
    } else {
      f.status = stronger(real, status) ? status : real;
    }
    if (extra) {
      if (extra.bytes !== undefined && extra.bytes !== null) f.bytes = extra.bytes;
      if (extra.note !== undefined) f.note = extra.note;
    }
    // every ancestor is a directory, whether or not anything listed it
    const parts = path.split("/");
    for (let i = 1; i < parts.length; i++) job.dirs[parts.slice(0, i).join("/")] = true;
    return f;
  }

  function addLog(job, text, level) {
    job.log.push({ text: clip(text, 220), level: level || "info" });
    if (job.log.length > LOG_CAP) job.log.splice(0, job.log.length - LOG_CAP);
  }

  // list_dir's lines are relative to the listed directory: two spaces per
  // level, a trailing "/" on directories (code_agent._list_dir_impl).
  function mergeListing(job, listing, basePath) {
    if (!Array.isArray(listing)) return;
    const base = normPath(basePath || "", job.root);
    if (base === null) return;
    const stack = [];
    for (const line of listing) {
      const text = String(line);
      const indent = Math.floor((text.match(/^ */)[0].length) / 2);
      let name = text.trim();
      if (!name || name.startsWith("...") || name.startsWith("[")) continue;
      const isDir = name.endsWith("/");
      if (isDir) name = name.slice(0, -1);
      if (!name) continue;
      stack.length = Math.min(stack.length, indent);
      const full = [base].concat(stack, name).filter(Boolean).join("/");
      if (isDir) { job.dirs[full] = true; stack[indent] = name; }
      else setFile(job, full, "listed");
    }
  }

  function describeStep(tool, args, root) {
    const a = args && typeof args === "object" ? args : {};
    const verb = VERBS[tool] || "Working";
    if (tool === "run_shell") return { verb, path: "", text: clip(a.command || "", 160) };
    if (tool === "search_code") {
      const where = normPath(a.path || "", root);
      return { verb, path: "", text: clip(a.pattern || "", 100) + (where ? "  in " + where : "") };
    }
    if (tool === "list_dir") {
      const d = normPath(a.path || ".", root);
      return { verb, path: "", text: d === null ? "outside the project" : (d || "/") };
    }
    const p = normPath(a.path || "", root);
    return { verb, path: p || "", text: p === null ? clip(a.path, 120) : "" };
  }

  // Apply ONE agent event to the job. Pure apart from mutating `job`.
  function applyEvent(job, e, now) {
    const phase = e.phase, st = e.status;
    if (phase === "plan") {
      if (st === "start") {
        job.task = clip(e.task || e.description || "", 300);
        const r = e.root || e.project_dir;
        if (r && !job.root) job.root = String(r);
        job.current = { verb: "Planning", path: "", text: "" };
      } else if (st === "ok") {
        for (const p of Array.isArray(e.files) ? e.files : []) {
          if (typeof p === "string") setFile(job, p, "planned");
        }
        if (e.run_command) addLog(job, "plan: run with " + e.run_command, "info");
      } else if (st === "fail") {
        job.error = firstLine(e.error) || "planning failed";
        addLog(job, "plan failed \u2014 " + job.error, "fail");
      }
    } else if (phase === "write") {
      if (st === "start") {
        setFile(job, e.path, "writing");
        job.current = { verb: "Writing", path: normPath(e.path, job.root) || "", text: "" };
      } else if (st === "ok") {
        setFile(job, e.path, "written", { bytes: typeof e.bytes === "number" ? e.bytes : null, note: "" });
      } else if (st === "fail") {
        setFile(job, e.path, "failed", { note: firstLine(e.error) || "write failed" });
        addLog(job, "write " + (e.path || "?") + " failed \u2014 " + firstLine(e.error), "fail");
      }
    } else if (phase === "install") {
      const deps = (Array.isArray(e.dependencies) ? e.dependencies : []).join(", ");
      if (st === "start") job.current = { verb: "Installing", path: "", text: deps || "dependencies" };
      else if (st === "ok") addLog(job, "installed " + (deps || "nothing"), "ok");
      else addLog(job, "install failed \u2014 " + (firstLine(e.stderr_tail) || deps), "fail");
    } else if (phase === "run") {
      if (st === "start") job.current = { verb: "Running", path: "", text: clip(e.command || "", 160) };
      else if (st === "ok") addLog(job, e.long_running ? "run \u2713 started and kept running" + (e.url ? " at " + e.url : "") : "run \u2713 exit " + e.exit_code, "ok");
      else addLog(job, "run \u2717 exit " + e.exit_code + (firstLine(e.stderr_tail) ? " \u2014 " + firstLine(e.stderr_tail) : ""), "fail");
    } else if (phase === "deliver") {
      if (st === "start") job.current = { verb: "Copying", path: "", text: clip(e.output_dir || "", 160) };
      else if (st === "ok") addLog(job, "delivered " + (e.files_copied == null ? "" : e.files_copied + " files ") + "\u2192 " + (e.output_dir || "?"), "ok");
      else addLog(job, "delivery failed \u2014 " + (firstLine(e.error) || "could not copy"), "fail");
    } else if (phase === "fix") {
      const target = e.target_file ? normPath(e.target_file, job.root) : null;
      if (st === "start") {
        if (target) setFile(job, target, "fixing");
        job.current = { verb: "Fixing", path: target || "", text: e.classified_error || "" };
      } else if (st === "ok") {
        if (target) setFile(job, target, "fixed");
        addLog(job, "fix " + e.attempt + "/" + e.max_attempts + " applied" + (e.classified_error ? " (" + e.classified_error + ")" : ""), "ok");
      } else {
        if (target) setFile(job, target, "failed", { note: firstLine(e.note) || "fix failed", force: true });
        addLog(job, "fix " + e.attempt + "/" + e.max_attempts + " failed" + (e.note ? " \u2014 " + firstLine(e.note) : ""), "fail");
      }
    } else if (phase === "step") {
      const tool = e.tool || "";
      if (st === "start") {
        job.pending = { tool, args: e.arguments && typeof e.arguments === "object" ? e.arguments : {} };
        job.current = describeStep(tool, job.pending.args, job.root);
        if (tool === "edit_file" || tool === "write_file") setFile(job, job.pending.args.path, "writing");
      } else {
        const args = job.pending && job.pending.tool === tool ? job.pending.args : {};
        job.pending = null;
        const ok = st === "ok";
        const outcome = firstLine(e.outcome) || firstLine(e.error);
        if (FILE_TOOLS[tool]) {
          const target = normPath(args.path, job.root);
          if (target) {
            if (!ok) setFile(job, target, "failed", { note: outcome });
            else if (tool === "read_file") setFile(job, target, "read", { note: "" });
            else if (tool === "edit_file") setFile(job, target, "edited", { note: "" });
            else setFile(job, target, "created", { note: "" });
          }
          if (!ok) addLog(job, tool + " " + (args.path || "?") + " \u2014 " + outcome, "fail");
        } else if (tool === "list_dir") {
          if (ok) mergeListing(job, e.listing, args.path || ".");
          else addLog(job, "list_dir \u2014 " + outcome, "fail");
        } else if (tool === "run_shell") {
          addLog(job, "$ " + clip(args.command || "", 90) + "  \u2192  " + outcome, ok && /^exit 0\b/.test(outcome) ? "ok" : "fail");
        } else if (tool === "search_code") {
          addLog(job, "search " + clip(args.pattern || "", 50) + "  \u2192  " + outcome, ok ? "info" : "fail");
        }
      }
    } else if (phase === "done") {
      job.status = st === "ok" ? "ok" : "fail";
      job.endedAt = now == null ? null : now;
      job.current = null;
      if (e.project_dir && !job.root) job.root = String(e.project_dir);
      if (st !== "ok") {
        job.error = firstLine(e.reason) || firstLine(e.error) || job.error || "gave up";
        const detail = firstLine(e.last_error);
        addLog(job, "stopped \u2014 " + job.error + (detail ? ": " + detail : ""), "fail");
      } else {
        addLog(job, "finished", "ok");
      }
    }
    return job;
  }

  /* ---- pure: the tree -------------------------------------------------------- */

  const BUSY = { writing: 1, fixing: 1 };
  const TOUCHED_HIDES = { listed: 1 };

  // Flat, ordered rows for the file tree. When showAll is false, files that were
  // only seen in a directory listing are left out (with their now-empty
  // folders); `hidden` says how many, so the panel can offer them.
  function buildRows(job, showAll, maxRows) {
    const cap = maxRows || 400;
    const root = { name: "", path: "", dir: true, kids: new Map() };
    function node(path, isDir) {
      let cur = root, acc = "";
      const parts = path.split("/");
      parts.forEach((seg, i) => {
        acc = acc ? acc + "/" + seg : seg;
        const last = i === parts.length - 1;
        let n = cur.kids.get(seg);
        if (!n) { n = { name: seg, path: acc, dir: last ? isDir : true, kids: new Map() }; cur.kids.set(seg, n); }
        cur = n;
      });
      return cur;
    }
    Object.keys(job.dirs).forEach((d) => { if (d) node(d, true); });
    Object.keys(job.files).forEach((p) => { node(p, false).file = job.files[p]; });

    let hidden = 0;
    const visible = (n) => {
      if (!n.dir) return !!n.file && (showAll || !TOUCHED_HIDES[n.file.status]);
      let any = false;
      n.kids.forEach((k) => { if (visible(k)) any = true; });
      return any;
    };
    Object.values(job.files).forEach((f) => { if (!showAll && TOUCHED_HIDES[f.status]) hidden += 1; });

    const rows = [];
    let truncated = false;
    (function walk(n, depth) {
      const kids = Array.from(n.kids.values()).sort((a, b) =>
        (a.dir === b.dir ? 0 : a.dir ? -1 : 1) || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
      for (const k of kids) {
        if (!visible(k)) continue;
        if (rows.length >= cap) { truncated = true; return; }
        rows.push({
          depth, name: k.name, path: k.path, dir: k.dir,
          status: k.file ? k.file.status : null, bytes: k.file ? k.file.bytes : null,
          note: k.file ? k.file.note : "",
          active: !!(job.current && job.current.path && job.current.path === k.path),
          busy: !!(k.file && BUSY[k.file.status]),
        });
        if (k.dir) walk(k, depth + 1);
      }
    })(root, 0);
    return { rows, hidden, truncated };
  }

  function summarize(job) {
    const c = { files: 0, edited: 0, created: 0, written: 0, read: 0, failed: 0 };
    Object.values(job.files).forEach((f) => {
      if (f.status === "listed") return;
      c.files += 1;
      if (f.status === "edited" || f.status === "fixed") c.edited += 1;
      else if (f.status === "created") c.created += 1;
      else if (f.status === "written") c.written += 1;
      else if (f.status === "read") c.read += 1;
      else if (f.status === "failed") c.failed += 1;
    });
    return c;
  }

  function fmtElapsed(ms) {
    if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return "";
    const s = Math.floor(ms / 1000);
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  }

  function fmtBytes(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return "";
    if (n < 1024) return n + " B";
    const v = n / 1024;
    return (v >= 10 ? Math.round(v) : v.toFixed(1)) + " KB";
  }

  /* ---- DOM ----------------------------------------------------------------- */

  const STATUS_GLYPH = {
    planned: "\u25cb", listed: "\u00b7", read: "r", writing: "\u22ef", fixing: "\u22ef",
    written: "\u2713", fixed: "\u2713", edited: "\u270e", created: "+", failed: "\u2717",
  };
  const STATUS_WORD = {
    planned: "planned", listed: "not touched", read: "read", writing: "writing\u2026", fixing: "fixing\u2026",
    written: "written", fixed: "fixed", edited: "edited", created: "created", failed: "failed",
  };

  let job = null;            // the one job the panel shows
  let showAll = false;       // tree: touched files only vs every listed file
  let tick = null;           // 1 s timer while running, for the elapsed readout
  let raf = 0;
  let wired = false;

  const $ = (sel) => document.querySelector(sel);
  const body = () => document.body;
  const isFocus = () => body().classList.contains("layout--focus");
  const isOpen = () => body().classList.contains("focus-agent-open");
  const isNarrow = () => global.innerWidth < 1000;     // same breakpoint the CSS uses

  function hostConvId() {
    try { return global.JarvisHost && global.JarvisHost.state().activeConversationId; } catch { return null; }
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function open() {
    if (!isFocus()) return;
    const b = body();
    b.classList.remove("focus-activity-open");          // same side of the screen
    if (isNarrow()) b.classList.remove("focus-chats-open");
    b.classList.add("focus-agent-open");
    render();
  }
  function close() { body().classList.remove("focus-agent-open"); }
  function toggle() { if (isOpen()) close(); else open(); }

  function syncButton() {
    const btn = $("#btn-focus-agent");
    if (!btn) return;
    btn.hidden = !job;
    btn.classList.toggle("is-live", !!job && job.status === "running");
    btn.setAttribute("aria-expanded", String(isOpen()));
  }

  function scheduleRender() {
    if (raf) return;
    const run = () => { raf = 0; render(); };
    raf = typeof global.requestAnimationFrame === "function" ? global.requestAnimationFrame(run) : (run(), 0);
  }

  function render() {
    syncButton();
    const panel = $("#agent-panel");
    if (!panel || !job) return;

    const stateEl = $("#agent-state");
    const word = job.status === "running" ? "running" : job.status === "ok" ? "done" : "stopped";
    stateEl.textContent = word;
    stateEl.dataset.state = job.status;

    const cur = hostConvId();
    const other = job.convId != null && cur != null && job.convId !== cur;
    $("#agent-kind").textContent =
      (job.kind === "code" ? "working on your codebase" : "building a new project") + (other ? " \u00b7 other chat" : "");

    const rootEl = $("#agent-root");
    rootEl.textContent = job.root || "(folder not reported yet)";
    rootEl.title = job.root || "";
    $("#btn-agent-open-folder").disabled = !job.root;
    const taskEl = $("#agent-task");
    taskEl.textContent = job.task;
    taskEl.title = job.task;
    taskEl.hidden = !job.task;

    // working on
    const nowEl = $("#agent-now");
    nowEl.textContent = "";
    if (job.current) {
      nowEl.appendChild(el("span", "agent-now__verb", job.current.verb));
      const what = job.current.path || job.current.text;
      if (what) nowEl.appendChild(el("span", "agent-now__what", what));
      nowEl.classList.toggle("is-live", job.status === "running");
    } else {
      nowEl.classList.remove("is-live");
      nowEl.appendChild(el("span", "agent-now__idle",
        job.status === "running" ? "waiting for the next step\u2026" : job.status === "ok" ? "finished" : (job.error || "stopped")));
    }
    const elapsed = job.startedAt == null ? "" :
      fmtElapsed((job.endedAt != null ? job.endedAt : Date.now()) - job.startedAt);
    $("#agent-elapsed").textContent = elapsed;

    // files
    const built = buildRows(job, showAll);
    const sum = summarize(job);
    const bits = [];
    if (sum.created + sum.written) bits.push((sum.created + sum.written) + " new");
    if (sum.edited) bits.push(sum.edited + " edited");
    if (sum.read) bits.push(sum.read + " read");
    if (sum.failed) bits.push(sum.failed + " failed");
    $("#agent-files-count").textContent = bits.length ? bits.join(" \u00b7 ") : (sum.files ? sum.files + " files" : "none yet");
    const moreBtn = $("#btn-agent-all");
    const listedTotal = Object.values(job.files).filter((f) => f.status === "listed").length;
    moreBtn.hidden = listedTotal === 0;
    moreBtn.textContent = showAll ? "touched only" : "show all (" + listedTotal + " more)";

    const tree = $("#agent-tree");
    const keepTop = tree.scrollTop;
    tree.textContent = "";
    if (!built.rows.length) {
      tree.appendChild(el("div", "agent-empty", job.status === "running" ? "No files yet\u2026" : "No files were reported."));
    }
    let activeNode = null;
    for (const r of built.rows) {
      const row = el("div", "agent-row" + (r.dir ? " agent-row--dir" : "") + (r.active ? " is-active" : "") + (r.busy ? " is-busy" : ""));
      row.style.paddingLeft = (8 + r.depth * 14) + "px";
      if (r.status) row.dataset.status = r.status;
      row.title = r.path + (r.status ? "  \u2014  " + (STATUS_WORD[r.status] || r.status) : "") + (r.note ? "  \u2014  " + r.note : "");
      row.appendChild(el("span", "agent-row__mark", r.dir ? "\u25b8" : (STATUS_GLYPH[r.status] || "\u00b7")));
      row.appendChild(el("span", "agent-row__name", r.name + (r.dir ? "/" : "")));
      if (!r.dir) {
        const meta = r.status === "failed" && r.note ? clip(r.note, 40) : (fmtBytes(r.bytes) || (r.status === "planned" || r.status === "listed" ? "" : STATUS_WORD[r.status] || ""));
        if (meta) row.appendChild(el("span", "agent-row__meta", meta));
      }
      tree.appendChild(row);
      if (r.active) activeNode = row;
    }
    if (built.truncated) tree.appendChild(el("div", "agent-empty", "\u2026 more files not shown"));
    // A code_agent job reloaded from a saved conversation only knows the files it
    // read or changed: the folder listings it browsed are deliberately not saved
    // (they would be a second copy of the project's file names in every chat).
    if (job.restored && job.kind === "code") {
      tree.appendChild(el("div", "agent-empty", "Restored after a reload \u2014 only files the agent read or changed are shown."));
    }
    tree.scrollTop = keepTop;
    // follow the file Jarvis is on, unless the person is looking around the tree
    if (activeNode && job.status === "running" && !tree.matches(":hover") && activeNode.scrollIntoView) {
      activeNode.scrollIntoView({ block: "nearest" });
    }

    // log (newest last)
    const logEl = $("#agent-log");
    logEl.textContent = "";
    const recent = job.log.slice(-12);
    if (!recent.length) logEl.appendChild(el("div", "agent-empty", "Nothing yet."));
    for (const l of recent) logEl.appendChild(el("div", "agent-log__line agent-log__line--" + l.level, l.text));
    logEl.scrollTop = logEl.scrollHeight;

    // the elapsed readout only needs a timer while the job runs
    if (job.status === "running" && !tick) tick = global.setInterval(() => {
      if (!job || job.status !== "running") { global.clearInterval(tick); tick = null; return; }
      $("#agent-elapsed").textContent = job.startedAt == null ? "" : fmtElapsed(Date.now() - job.startedAt);
    }, 1000);
    if (job.status !== "running" && tick) { global.clearInterval(tick); tick = null; }
  }

  /* ---- public API ----------------------------------------------------------- */

  // One live progress event (the parsed JSON of a JARVIS_MEDIA dev_agent line).
  function feed(event) {
    if (!event || typeof event !== "object" || !event.job_id) return;
    let started = false;
    if (!job || job.id !== event.job_id) {
      job = newJob(event, Date.now(), hostConvId());
      showAll = false;
      started = true;
    }
    applyEvent(job, event, Date.now());
    if (started && isFocus()) open();          // a NEW job opens the panel; later events never do
    scheduleRender();
  }

  // A finished job from a reloaded conversation: {jobId, ok, projectDir, steps}.
  // Never replaces a job that is still running, and never opens the panel.
  function restore(data) {
    if (!data || !Array.isArray(data.steps) || !data.steps.length) return;
    if (job && job.status === "running") return;
    const first = data.steps[0];
    if (!first || typeof first !== "object") return;
    const j = newJob(Object.assign({ job_id: data.jobId || first.job_id }, first), null, hostConvId());
    j.restored = true;     // render() says so for a code job: its directory listings are not saved
    if (data.projectDir) j.root = String(data.projectDir);
    for (const e of data.steps) { if (e && typeof e === "object") applyEvent(j, e, null); }
    if (j.status === "running") j.status = data.ok === false ? "fail" : "ok";   // an interrupted run is not "running"
    job = j;
    showAll = false;
    scheduleRender();
  }

  async function openFolder() {
    if (!job || !job.root) return;
    try {
      const res = await fetch("/api/tools/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "open_file_location", arguments: { path: job.root } }),
      });
      const data = await res.json().catch(() => ({}));
      const err = (data && data.result && data.result.error) || (!res.ok && (data.error || "Couldn't open that folder."));
      if (err && global.JarvisHost) global.JarvisHost.toast(String(err));
    } catch (e) {
      if (global.JarvisHost) global.JarvisHost.toast(e.message || "Couldn't open that folder.");
    }
  }

  function wire() {
    if (wired) return;
    wired = true;
    $("#btn-focus-agent")?.addEventListener("click", toggle);
    $("#btn-agent-close")?.addEventListener("click", close);
    $("#btn-agent-open-folder")?.addEventListener("click", openFolder);
    $("#btn-agent-all")?.addEventListener("click", () => { showAll = !showAll; render(); });
    $("#focus-scrim")?.addEventListener("click", close);     // narrow: the scrim is what closes it

    // Same side of the screen as Activity, and on a narrow screen the same slot
    // as Chats: opening either of those puts this away. Leaving Focus closes it
    // too (app.js applyLayout clears the class; this keeps the button honest).
    new MutationObserver(() => {
      const b = body();
      if (isOpen() && (b.classList.contains("focus-activity-open") || (isNarrow() && b.classList.contains("focus-chats-open")))) {
        close();
      }
      syncButton();
    }).observe(document.body, { attributes: true, attributeFilter: ["class"] });

    // Escape closes it as a drawer on a narrow screen, or when focus is inside
    // it. On a wide screen it is docked, so Escape typed in the composer (to
    // dismiss the / palette, say) must NOT close it. Registered after
    // initFocusStage's capture handler, which stops propagation when it has
    // already closed Chats or Activity.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || !isOpen()) return;
      const panel = $("#agent-panel");
      if (isNarrow() || (panel && panel.contains(e.target))) { e.stopImmediatePropagation(); close(); }
    }, true);
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
    else wire();
  }

  global.JarvisAgentPanel = {
    feed, restore, open, close, toggle,
    _pure: { normPath, newJob, applyEvent, mergeListing, buildRows, summarize, fmtElapsed, fmtBytes, describeStep, stronger },
  };
})(typeof window !== "undefined" ? window : globalThis);
