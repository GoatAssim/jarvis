/* ============================================================================
 * tool-manager.js — Menu → Tool Manager (master plan L.25).
 *
 * WHAT IT IS
 * ----------
 * One place to see every tool Jarvis can call — shipped, your own, MCP — and to
 * decide how each behaves:
 *
 *   Ask before running   confirm_required   Yes/No before the tool runs
 *   AI overview          ai_review          a second AI adds a risk note to that prompt
 *   Approval summary     approval_summary   a scheduled job that can use the tool says so
 *                                           when you approve it (and the approval covers it)
 *
 * Your own tools (~/.jarvis/tools) can also be switched on/off per file,
 * imported, created and edited here. This panel REPLACES the old Custom Tools
 * panel; custom-tools.js keeps only the theme gallery the Skin modal shares.
 *
 * SWITCHING THINGS OFF (owner, 2026-10-03)
 * ----------------------------------------
 * Every tool — shipped, yours or MCP — and every saved command has an
 * "Available to Jarvis" switch. OFF means the MODEL can't see it and can't use
 * it; you still can (Debug, the Test Checklist, `jarvis <command>`). Scheduled
 * jobs that name it fail clearly when they fire, and the panel lists them before
 * you switch it off. A handful of discovery tools can't be switched off at all
 * (Jarvis breaks without them); they show a lock and the reason. The rules live
 * in jarvis/tool_disable.py — this file only shows and flips them.
 *
 * TALKS TO THE SERVER ONLY THROUGH EXISTING ROUTES
 *   GET  /api/tools                read-only catalogue (+ flags, defaults, group, file, disabled, protected)
 *   POST /api/tools/safety         flip one safeguard
 *   POST /api/tools/disabled       switch a tool off/on for the model
 *   GET  /api/commands             saved commands (read-only here)
 *   POST /api/commands/:n/disabled switch a saved command off/on for the model
 *   GET  /api/disabled             what is off + the protected map
 *   GET  /api/disabled/dependents  scheduled jobs that name a tool/command
 *   /api/ctools[...]               list / show / check / write / run / enabled / delete / draft / templates
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 * Same rule as ui-kit.js and test-checklist.js: tool descriptions, file
 * contents, error messages and parameter docs are inserted with textContent,
 * never innerHTML — a tool's description is written by someone else.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* =======================================================================
   * PURE HELPERS — no DOM, exported as JarvisToolManager._pure for tests.
   * ===================================================================== */

  const SAFEGUARDS = [
    { key: "confirm_required", short: "ASK", title: "Ask before running", hotkey: "1",
      hint: "Pauses and asks you Yes or No before this tool runs." },
    { key: "ai_review", short: "AI", title: "AI overview", hotkey: "2",
      hint: "A second AI reviews the call first and adds a plain-language risk note to the confirmation." },
    { key: "approval_summary", short: "SUM", title: "Approval summary", hotkey: "3",
      hint: "When a scheduled job that can use this tool is waiting for your approval, the approval says so." },
  ];
  const KEYS = SAFEGUARDS.map((s) => s.key);

  const SOURCE_BUCKETS = { builtin: "shipped", auto: "shipped", mcp: "mcp", user: "user", command: "command" };
  const SOURCE_LABELS = { shipped: "Shipped", user: "Yours", mcp: "MCP", command: "Command" };
  // Row states: loaded | off (switched off by you) | fileoff (its file is switched
  // off) | pending (valid file, not loaded yet) | failed (file rejected).
  const OFF_STATES = ["off", "fileoff"];
  const NAME_RE = /^[a-z][a-z0-9_]{0,48}$/;
  const MAX_SOURCE_CHARS = 120000;      // custom_tools_store.MAX_SOURCE_CHARS
  const STAGES = ["syntax", "import", "contract"];

  function bucketOf(source) { return SOURCE_BUCKETS[source] || "shipped"; }

  function groupLabel(id) {
    if (id === "__failed") return "Failed to load";
    if (id === "commands") return "Saved commands";
    return String(id || "other").replace(/_/g, " ");
  }

  // Rows the list shows. A tool is "loaded" when the CLI offers it and you
  // haven't switched it off ("off" when you have); a user tool whose FILE is
  // switched off is "fileoff"; an unreadable file is "failed". Saved commands
  // (kind "command") are "loaded" or "off". `disabledCommands` is the list from
  // /api/disabled.
  function buildRows(tools, files, commands, disabledCommands) {
    const rows = [];
    const byName = new Map();
    (Array.isArray(tools) ? tools : []).forEach((t) => {
      if (!t || !t.name || byName.has(t.name)) return;
      const row = {
        id: "t:" + t.name, kind: "tool", state: t.disabled ? "off" : "loaded", name: t.name,
        protectedReason: t.protected || "",
        description: t.description || "", source: t.source || "builtin",
        group: t.group || "other", file: t.file || "", userFile: null, error: "",
        flags: { confirm_required: !!t.confirm_required, ai_review: !!t.ai_review, approval_summary: !!t.approval_summary },
        defaults: t.defaults ? {
          confirm_required: !!t.defaults.confirm_required, ai_review: !!t.defaults.ai_review,
          approval_summary: !!t.defaults.approval_summary,
        } : null,
        parameters: t.parameters || {},
      };
      byName.set(t.name, row);
      rows.push(row);
    });
    (Array.isArray(files) ? files : []).forEach((f) => {
      if (!f || !f.name) return;
      if (!f.valid) {
        rows.push({
          id: "f:" + f.name, kind: "broken", state: "failed", name: f.file || f.name, description: "",
          source: "user", group: "__failed", file: f.file || f.name, userFile: f, error: f.error || "won't load",
          flags: null, defaults: null, parameters: {},
        });
        return;
      }
      (f.tools || []).forEach((toolName) => {
        const live = byName.get(toolName);
        if (live) { live.userFile = f; if (!live.file) live.file = f.file; return; }
        const row = {
          id: "t:" + toolName, kind: "tool", state: f.enabled ? "pending" : "fileoff", name: toolName, protectedReason: "",
          description: f.description || "", source: "user", group: f.group || "custom", file: f.file || "",
          userFile: f, error: "", flags: null, defaults: null, parameters: {},
        };
        byName.set(toolName, row);
        rows.push(row);
      });
    });
    const off = new Set(Array.isArray(disabledCommands) ? disabledCommands : []);
    if (commands && typeof commands === "object" && !Array.isArray(commands)) {
      Object.keys(commands).forEach((name) => {
        const spec = commands[name] && typeof commands[name] === "object" ? commands[name] : {};
        rows.push({
          id: "c:" + name, kind: "command", state: off.has(name) ? "off" : "loaded", name,
          protectedReason: "", description: spec.description || "", source: "command", group: "commands",
          file: "", userFile: null, error: "", flags: null, defaults: null, parameters: {}, spec,
        });
      });
    }
    return rows;
  }

  function matchesSearch(row, query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return true;
    return [row.name, row.description, row.group, row.file, row.error].some((s) => String(s || "").toLowerCase().includes(q));
  }

  function changedKeys(row) {
    if (!row || !row.flags || !row.defaults) return [];
    return KEYS.filter((k) => row.flags[k] !== row.defaults[k]);
  }

  function matchesSafeguard(row, mode) {
    if (!mode || mode === "any") return true;
    if (mode === "changed") return changedKeys(row).length > 0;
    if (!row.flags) return false;
    if (mode === "none") return !KEYS.some((k) => row.flags[k]);
    return !!row.flags[mode];
  }

  // "attention" = things that are broken or half-done. A tool you switched off on
  // purpose is not that; it has its own "off" filter.
  function needsAttention(row) { return row.state === "failed" || row.state === "pending"; }
  function isOff(row) { return OFF_STATES.includes(row.state); }

  function matchesSource(row, source) {
    if (!source || source === "all") return true;
    if (source === "attention") return needsAttention(row);
    if (source === "off") return isOff(row);
    return bucketOf(row.source) === source;
  }

  function filterRows(rows, f) {
    f = f || {};
    return rows.filter((r) => matchesSource(r, f.source) && (!f.group || f.group === "all" || r.group === f.group)
      && matchesSafeguard(r, f.safeguard) && matchesSearch(r, f.search));
  }

  // Groups in display order: failed files first, then alphabetical.
  function groupRows(rows) {
    const map = new Map();
    rows.forEach((r) => { if (!map.has(r.group)) map.set(r.group, []); map.get(r.group).push(r); });
    const ids = Array.from(map.keys()).sort((a, b) => {
      if (a === "__failed") return -1;
      if (b === "__failed") return 1;
      return a.localeCompare(b);
    });
    return ids.map((id) => ({ id, label: groupLabel(id), rows: map.get(id).sort((a, b) => a.name.localeCompare(b.name)) }));
  }

  function coverage(rows) {
    const loaded = rows.filter((r) => r.kind === "tool" && r.state === "loaded" && r.flags);
    const out = { total: loaded.length };
    out.off = rows.filter((r) => r.kind === "tool" && isOff(r)).length;
    out.commands = rows.filter((r) => r.kind === "command").length;
    out.commandsOff = rows.filter((r) => r.kind === "command" && isOff(r)).length;
    KEYS.forEach((k) => { out[k] = loaded.filter((r) => r.flags[k]).length; });
    out.bySource = { shipped: 0, user: 0, mcp: 0, command: 0 };
    loaded.forEach((r) => { out.bySource[bucketOf(r.source)] += 1; });
    out.none = loaded.filter((r) => !KEYS.some((k) => r.flags[k])).length;
    return out;
  }

  // What happens when the model calls the tool. The executor asks when EITHER
  // confirm_required or ai_review is on (ai_client.py), so AI overview alone
  // still produces a prompt — the copy must not imply otherwise.
  function callOutcome(flags) {
    if (!flags) return null;
    if (flags.ai_review) return { id: "ai", text: flags.confirm_required
      ? "Asks you first, with a risk note from a second AI."
      : "Asks you first, with a risk note from a second AI. AI overview alone is enough to trigger the prompt, even with “Ask before running” off." };
    if (flags.confirm_required) return { id: "ask", text: "Asks you first, then runs if you say yes." };
    return { id: "free", text: "Runs straight away when Jarvis calls it." };
  }

  // What the approval screen of a scheduled job does with the tool. When the
  // switch is off for a tool that is normally listed, the approval no longer
  // covers it (job_risk.kind_of), so an approved job can't run it unattended.
  function jobOutcome(row) {
    if (!row || !row.flags) return null;
    const on = row.flags.approval_summary;
    const def = row.defaults ? row.defaults.approval_summary : null;
    if (on) return "A scheduled job that can use it lists it when you approve the job, and approving covers it.";
    if (def) return "Not listed. Normally this tool is, so an approved scheduled job is not allowed to run it unattended.";
    return "Not listed. Scheduled jobs follow the normal policy for it.";
  }

  // The words under the "Available to Jarvis" switch and in the "what this means"
  // strip. One place, so the two can't disagree.
  function availabilityHint(row) {
    if (!row) return "";
    if (row.kind === "command") {
      return "Switch off and Jarvis can't see or run this saved command; a scheduled job that uses it fails with a clear message. You can still run it yourself.";
    }
    return "Switch off and Jarvis can't see this tool or use it — not in search, not by name, even if it guesses. You can still run it yourself from Debug.";
  }

  function offOutcome(row) {
    if (row && row.kind === "command") {
      return "Jarvis doesn’t know this command exists, and a call to it is refused. Scheduled jobs that run it fail when they fire.";
    }
    return "Jarvis doesn’t see this tool, and a call to it is refused without running. Scheduled jobs that use it fail when they fire.";
  }

  // The scheduled jobs a switch-off would break, as lines for a confirmation.
  function dependentLines(jobs) {
    return (Array.isArray(jobs) ? jobs : []).map((j) => {
      const next = j && j.next_run ? " — next " + String(j.next_run).replace("T", " ").slice(0, 16) : "";
      return "• " + ((j && j.title) || (j && j.id) || "(untitled job)") + next;
    });
  }

  // Sentences like "3 tools, 1 saved command" for the overview.
  function offSummary(cov) {
    const parts = [];
    if (cov.off) parts.push(cov.off + (cov.off === 1 ? " tool" : " tools"));
    if (cov.commandsOff) parts.push(cov.commandsOff + (cov.commandsOff === 1 ? " saved command" : " saved commands"));
    return parts.join(", ");
  }

  function commandSteps(spec) {
    const run = spec && spec.run;
    const steps = Array.isArray(run) ? run : (run == null ? [] : [run]);
    return steps.map((st) => (typeof st === "string" ? st : (st && typeof st.run === "string" ? st.run : JSON.stringify(st))));
  }

  function commandVars(spec) {
    const vars = (spec && spec.vars) || {};
    return Object.keys(vars).map((name) => {
      const v = vars[name] && typeof vars[name] === "object" ? vars[name] : {};
      return { name, description: v.description || "", hasDefault: Object.prototype.hasOwnProperty.call(v, "default"), def: v.default };
    });
  }

  function nameProblem(name, existing, mode) {
    const n = String(name || "").trim();
    if (!n) return "Give the file a name.";
    if (!NAME_RE.test(n)) return "Use lower_snake_case, start with a letter, 49 characters at most.";
    if (mode !== "edit" && (existing || []).includes(n)) return "A tool file called “" + n + "” already exists.";
    return "";
  }

  // "My Tool-v2.py" -> "my_tool_v2"; "" when nothing usable is left.
  function importNameFromFile(filename) {
    let n = String(filename || "").replace(/^.*[\\/]/, "").replace(/\.py(\.disabled)?$/i, "");
    n = n.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").replace(/_+/g, "_");
    if (n && !/^[a-z]/.test(n)) n = "tool_" + n;
    return n.slice(0, 49);
  }

  function importProblem(file) {
    if (!file) return "No file chosen.";
    if (!/\.py$/i.test(file.name || "")) return "Only .py tool files can be imported.";
    return "";
  }

  function sourceProblem(text) {
    if (!String(text || "").trim()) return "The file is empty.";
    if (String(text).length > MAX_SOURCE_CHARS) return "The file is too large (limit " + MAX_SOURCE_CHARS.toLocaleString("en-US") + " characters).";
    if (/\u0000/.test(text)) return "That doesn't look like a text file.";
    return "";
  }

  function parseErrorLine(error) {
    const m = /(?:^|\b)line (\d+)/i.exec(String(error || ""));
    return m ? parseInt(m[1], 10) : null;
  }

  // ok | bad | skip | idle for each of syntax -> import -> contract.
  function stageStates(result) {
    if (!result) return STAGES.map(() => "idle");
    if (result.ok) return STAGES.map(() => "ok");
    const at = result.stage === "empty" ? 0 : STAGES.indexOf(result.stage);
    if (at < 0) return STAGES.map(() => "bad");
    return STAGES.map((_, i) => (i < at ? "ok" : i === at ? "bad" : "skip"));
  }

  function lineCount(text) { return String(text || "").split("\n").length; }

  function formatBytes(n) {
    n = Number(n) || 0;
    return n < 1024 ? n + " B" : (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB";
  }

  function parseArgs(text) {
    const raw = String(text || "").trim() || "{}";
    let value;
    try { value = JSON.parse(raw); } catch (e) { return { ok: false, error: "Not valid JSON: " + e.message }; }
    if (!value || typeof value !== "object" || Array.isArray(value)) return { ok: false, error: "Arguments must be a JSON object, e.g. {}" };
    return { ok: true, value };
  }

  function paramList(parameters) {
    const props = (parameters && parameters.properties) || {};
    const required = new Set((parameters && parameters.required) || []);
    return Object.keys(props).map((name) => {
      const p = props[name] || {};
      let type = Array.isArray(p.type) ? p.type.filter((t) => t !== "null").join(" | ") : (p.type || "");
      if (Array.isArray(p.enum) && p.enum.length) type = p.enum.map(String).join(" | ");
      return { name, type, required: required.has(name), description: p.description || "" };
    });
  }

  const PURE = {
    availabilityHint, offOutcome, dependentLines, offSummary, commandSteps, commandVars, needsAttention, isOff, OFF_STATES,
    SAFEGUARDS, KEYS, NAME_RE, STAGES, MAX_SOURCE_CHARS, bucketOf, groupLabel, buildRows, matchesSearch, matchesSafeguard,
    matchesSource, filterRows, groupRows, coverage, callOutcome, jobOutcome, changedKeys, nameProblem, importNameFromFile,
    importProblem, sourceProblem, parseErrorLine, stageStates, lineCount, formatBytes, parseArgs, paramList,
  };

  /* =======================================================================
   * DOM / state
   * ===================================================================== */

  const UI = () => global.JarvisUI;
  const el = (...a) => global.JarvisUI._el(...a);
  const $ = (sel, root) => (root || document).querySelector(sel);
  const toast = (message, level) => { try { UI().toast({ message, level: level || "info" }); } catch (_) { /* ui-kit missing */ } };

  const PREF_KEY = "jarvis-tool-manager-ui";
  const state = {
    built: false, loading: false, tools: null, files: [], templates: [], rows: [],
    commands: null, disabledCommands: [], toolsError: "", filesError: "", commandsError: "",
    selected: null, view: "tool",
    search: "", source: "all", group: "all", safeguard: "any",
    collapsed: new Set(), busy: new Set(),
    editor: null, draft: null, run: null, prevFocus: null,
    suggest: true,                      // Jarvis inline suggestions in the editor (L.33); on by default, one click to turn off
  };
  let dom = null;

  function loadPrefs() {
    try {
      const p = JSON.parse(global.localStorage.getItem(PREF_KEY) || "{}");
      if (Array.isArray(p.collapsed)) state.collapsed = new Set(p.collapsed);
      if (typeof p.source === "string") state.source = p.source;
      if (typeof p.suggest === "boolean") state.suggest = p.suggest;
    } catch (_) { /* private mode / bad JSON: defaults */ }
  }
  function savePrefs() {
    try { global.localStorage.setItem(PREF_KEY, JSON.stringify({ collapsed: Array.from(state.collapsed), source: state.source, suggest: state.suggest })); }
    catch (_) { /* nowhere to save: fine */ }
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) {
      const err = new Error((body && (body.error || body.message)) || res.statusText || ("HTTP " + res.status));
      err.data = body;
      throw err;
    }
    return body;
  }
  const ctools = (suffix, opts) => api("/api/ctools" + suffix, opts);

  const rowById = (id) => state.rows.find((r) => r.id === id) || null;
  const rowByName = (name) => state.rows.find((r) => r.kind === "tool" && r.name === name) || null;
  const selectedRow = () => rowById(state.selected);
  const userFileNames = () => (state.files || []).map((f) => f.name);

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function setText(node, text) { node.textContent = text; }

  function grabDom() {
    const overlay = $("#tm-overlay");
    if (!overlay) return null;
    return {
      overlay, panel: $(".tm-panel", overlay), statusLine: $("#tm-status-line"), search: $("#tm-search"),
      chips: $("#tm-chips"), groupSel: $("#tm-group-filter"), safeSel: $("#tm-safeguard-filter"), list: $("#tm-list"),
      count: $("#tm-count"), midTitle: $("#tm-mid-title"), detail: $("#tm-detail"), foot: $("#tm-foot"), side: $("#tm-side"),
      file: $("#tm-file"), refresh: $("#btn-tm-refresh"), btnImport: $("#btn-tm-import"), btnCreate: $("#btn-tm-create"),
    };
  }

  function setStatusLine(text, cls) {
    dom.statusLine.textContent = text;
    dom.statusLine.classList.toggle("is-error", cls === "error");
  }

  /* ---- small builders ---------------------------------------------------- */

  function tag(text, cls, title) { return el("span", { class: "tm-tag" + (cls ? " tm-tag--" + cls : ""), title: title || null }, text); }

  function pips(flags) {
    return el("span", { class: "tm-pips", "aria-hidden": "true" }, SAFEGUARDS.map((s) =>
      el("span", { class: "tm-pip" + (flags && flags[s.key] ? " is-on" : ""), "data-k": s.key }, s.short)));
  }

  function sectionTitle(text, aside) {
    return el("h4", { class: "tm-section-title" }, [text, aside ? el("span", { class: "tm-section-title__aside" }, aside) : null]);
  }

  function emptyBlock(children) { return el("div", { class: "tm-empty" }, children); }

  function switchEl(opts) {
    const b = el("button", {
      class: "tm-switch" + (opts.busy ? " is-busy" : ""), type: "button", role: "switch",
      "aria-checked": opts.on ? "true" : "false", "aria-label": opts.label,
      "aria-describedby": opts.describedBy || null, disabled: opts.disabled || null,
      onclick: opts.onToggle,
    });
    return b;
  }

  /* ---- list ---------------------------------------------------------------- */

  function renderChips() {
    const counts = { all: state.rows.length, shipped: 0, user: 0, mcp: 0, command: 0, off: 0, attention: 0 };
    state.rows.forEach((r) => {
      counts[bucketOf(r.source)] += 1;
      if (isOff(r)) counts.off += 1;
      if (needsAttention(r)) counts.attention += 1;
    });
    const defs = [["all", "All"], ["shipped", "Shipped"], ["user", "Yours"], ["mcp", "MCP"], ["command", "Commands"], ["off", "Off"], ["attention", "Attention"]];
    clear(dom.chips);
    defs.forEach(([id, label]) => {
      if (["mcp", "command", "off", "attention"].includes(id) && !counts[id] && state.source !== id) return;
      dom.chips.appendChild(el("button", {
        class: "tm-chip" + (id === "attention" ? " tm-chip--bad" : ""), type: "button",
        "aria-pressed": state.source === id ? "true" : "false",
        onclick: () => { state.source = id; savePrefs(); renderChips(); renderList(); },
      }, [label, el("span", { class: "tm-chip__n" }, String(counts[id]))]));
    });
  }

  function renderGroupSelect() {
    const ids = Array.from(new Set(state.rows.map((r) => r.group))).sort((a, b) => (a === "__failed" ? -1 : b === "__failed" ? 1 : a.localeCompare(b)));
    if (state.group !== "all" && !ids.includes(state.group)) state.group = "all";
    clear(dom.groupSel);
    dom.groupSel.appendChild(el("option", { value: "all" }, "All categories"));
    ids.forEach((id) => dom.groupSel.appendChild(el("option", { value: id, selected: id === state.group }, groupLabel(id))));
    dom.groupSel.value = state.group;
  }

  function cardFor(row) {
    const active = state.selected === row.id;
    const meta = el("div", { class: "tm-card__meta" });
    meta.appendChild(tag(SOURCE_LABELS[bucketOf(row.source)], bucketOf(row.source) === "user" ? "warn" : (bucketOf(row.source) === "mcp" || bucketOf(row.source) === "command") ? "info" : ""));
    if (row.state === "off") meta.appendChild(tag("off", "bad", "Switched off — Jarvis can't see or use it"));
    if (row.state === "fileoff") meta.appendChild(tag("file off", ""));
    if (row.protectedReason) meta.appendChild(tag("always on", "info", row.protectedReason));
    if (row.state === "pending") meta.appendChild(tag("not loaded yet", "warn"));
    if (row.state === "failed") meta.appendChild(tag("rejected", "bad"));
    if (row.flags) { meta.appendChild(pips(row.flags)); if (changedKeys(row).length) meta.appendChild(tag("edited", "changed", "Differs from the built-in default")); }
    const card = el("button", {
      class: "tm-card" + (active ? " is-active" : ""), type: "button", "data-tm-state": row.state, "data-id": row.id,
      "aria-current": active ? "true" : null, onclick: () => select(row.id),
    }, [el("div", { class: "tm-card__row" }, [el("span", { class: "tm-card__name" }, row.name)]), meta]);
    if (row.state === "failed") card.appendChild(el("div", { class: "tm-card__err" }, String(row.error).slice(0, 140)));
    return card;
  }

  function visibleRows() {
    return filterRows(state.rows, { search: state.search, source: state.source, group: state.group, safeguard: state.safeguard });
  }

  function renderList() {
    if (!dom) return;
    const keep = dom.list.scrollTop;
    clear(dom.list);
    const rows = visibleRows();
    setText(dom.count, rows.length === state.rows.length ? String(rows.length) : rows.length + " of " + state.rows.length);

    if (state.toolsError && !state.rows.length) {
      dom.list.appendChild(emptyBlock([
        "Couldn’t read the tool catalogue.", el("br"), el("b", null, state.toolsError.slice(0, 120)), el("br"),
        "Is the Jarvis CLI reachable from the web server?", el("br"),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: loadAll }, "Try again"),
      ]));
      return;
    }
    if (!rows.length) {
      dom.list.appendChild(emptyBlock(state.rows.length
        ? ["Nothing matches these filters.", el("br"), el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: resetFilters }, "Clear filters")]
        : ["No tools yet."]));
      return;
    }
    groupRows(rows).forEach((g) => {
      const collapsed = state.collapsed.has(g.id) && !state.search;
      const wrap = el("div", { class: "tm-group" + (collapsed ? " is-collapsed" : "") + (g.id === "__failed" ? " tm-group--failed" : "") });
      wrap.appendChild(el("button", {
        class: "tm-group__head", type: "button", "aria-expanded": collapsed ? "false" : "true",
        onclick: () => { if (state.collapsed.has(g.id)) state.collapsed.delete(g.id); else state.collapsed.add(g.id); savePrefs(); renderList(); },
      }, [el("span", { class: "tm-group__caret", "aria-hidden": "true" }), el("span", { class: "tm-group__label" }, g.label),
          el("span", { class: "tm-group__count" }, String(g.rows.length))]));
      g.rows.forEach((r) => wrap.appendChild(cardFor(r)));
      dom.list.appendChild(wrap);
    });
    dom.list.scrollTop = keep;
  }

  function resetFilters() {
    state.search = ""; state.source = "all"; state.group = "all"; state.safeguard = "any";
    dom.search.value = ""; dom.safeSel.value = "any"; dom.groupSel.value = "all";
    savePrefs(); renderChips(); renderList();
  }

  /* ---- selection ----------------------------------------------------------- */

  async function select(id) {
    if (id === state.selected && state.view === "tool") return;
    if (!(await leaveEditor())) return;
    state.selected = id; state.view = "tool"; state.draft = null; state.run = null;
    renderMain();
    const card = dom.list.querySelector('.tm-card[data-id="' + (global.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
    if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
  }

  function renderMain() {
    renderList();
    renderDetail();
    renderSide();
  }

  /* ---- middle pane: a tool ------------------------------------------------- */

  function switchRow(row, def, locked) {
    const on = !!(row.flags && row.flags[def.key]);
    const busy = state.busy.has(row.name + ":" + def.key);
    const dflt = row.defaults ? row.defaults[def.key] : null;
    const hintId = "tm-hint-" + def.key;
    const body = el("div", null, [
      el("div", { class: "tm-sw-title" }, [def.title, el("span", { class: "tm-sw-key", "aria-hidden": "true", title: "Keyboard shortcut" }, def.hotkey),
        row.defaults && dflt !== on ? tag("edited", "changed", "Built-in default is " + (dflt ? "on" : "off")) : null]),
      el("div", { class: "tm-sw-hint", id: hintId }, def.hint),
    ]);
    if (def.key === "ai_review" && on && !(row.flags && row.flags.confirm_required)) {
      body.appendChild(el("div", { class: "tm-sw-note" }, "This tool will still ask first — AI overview needs the prompt to show its note on."));
    }
    if (def.key === "approval_summary" && !on && dflt) {
      body.appendChild(el("div", { class: "tm-sw-note" }, "Normally on. With it off, an approved scheduled job can’t use this tool unattended."));
    }
    return el("div", { class: "tm-sw-row" + (on ? " is-on" : "") + (locked ? " is-locked" : ""), "data-k": def.key }, [
      body,
      switchEl({ on, busy, disabled: locked || busy, label: def.title + " — " + row.name, describedBy: hintId, onToggle: () => setFlag(row.name, def.key, !on) }),
    ]);
  }

  // The "Available to Jarvis" switch. ON = the model can see and use it. A
  // protected tool shows it ON and locked, with the reason; a tool whose file is
  // off, or that hasn't loaded, shows it locked with why.
  function availabilityRow(row) {
    const on = row.state !== "off";
    const eligible = row.state === "loaded" || row.state === "off";
    const locked = !!row.protectedReason || !eligible;
    const busy = state.busy.has(row.name + ":enabled");
    const hintId = "tm-hint-enabled";
    const body = el("div", null, [
      el("div", { class: "tm-sw-title" }, ["Available to Jarvis", el("span", { class: "tm-sw-key", "aria-hidden": "true", title: "Keyboard shortcut" }, "4"),
        row.protectedReason ? tag("always on", "info", row.protectedReason) : null]),
      el("div", { class: "tm-sw-hint", id: hintId }, availabilityHint(row)),
    ]);
    if (row.protectedReason) body.appendChild(el("div", { class: "tm-sw-note" }, "This one can’t be switched off. " + row.protectedReason));
    else if (row.state === "fileoff") body.appendChild(el("div", { class: "tm-sw-note" }, "Its file is switched off, so Jarvis can’t use it either way. Turn the file on below."));
    else if (row.state === "pending") body.appendChild(el("div", { class: "tm-sw-note" }, "Not loaded yet. Refresh to pick it up."));
    return el("div", { class: "tm-sw-row" + (on && eligible ? " is-on" : "") + (locked ? " is-locked" : ""), "data-k": "enabled" }, [
      body,
      switchEl({ on: on && row.state !== "fileoff" && row.state !== "pending", busy, disabled: locked || busy,
        label: "Available to Jarvis — " + row.name, describedBy: hintId, onToggle: () => setAvailable(row, !on) }),
    ]);
  }

  function fileSection(row) {
    const f = row.userFile;
    const wrap = el("div");
    wrap.appendChild(sectionTitle("Your file", f ? formatBytes(f.size) : ""));
    const shared = f && (f.tools || []).length > 1;
    const box = el("div", { class: "tm-file" });
    box.appendChild(el("div", { class: "tm-file__row" }, [
      el("div", { style: "flex:1;min-width:0" }, [
        el("div", { class: "tm-file__name" }, "~/.jarvis/tools/" + f.file),
        el("div", { class: "tm-file__sub" }, "Updated " + (f.updated ? String(f.updated).replace("T", " ") : "—")
          + (shared ? " · provides " + f.tools.join(", ") : "")),
      ]),
    ]));
    const enabled = !!f.enabled;
    const swId = "tm-sw-file";
    box.appendChild(el("div", { class: "tm-file__row" }, [
      el("div", { style: "flex:1;min-width:0" }, [
        el("div", { class: "tm-sw-title", id: swId }, "File enabled"),
        el("div", { class: "tm-sw-hint" }, shared ? "Switches off every tool in this file (" + f.tools.length + ")." : "Switched-off files are kept as .py.disabled and not offered to the model."),
      ]),
      switchEl({ on: enabled, busy: state.busy.has("file:" + f.name), disabled: state.busy.has("file:" + f.name),
        label: "File enabled — " + f.name, describedBy: swId, onToggle: () => setFileEnabled(f.name, !enabled) }),
    ]));
    box.appendChild(el("div", { class: "tm-file__actions" }, [
      el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => editFile(f.name) }, "Edit"),
      el("button", { class: "btn btn--ghost btn--sm tm-danger", type: "button", onclick: () => deleteFile(f.name) }, "Delete"),
    ]));
    wrap.appendChild(box);
    return wrap;
  }

  function renderToolDetail(row) {
    const d = dom.detail;
    clear(d);
    setText(dom.midTitle, row.kind === "broken" ? "Rejected file" : row.kind === "command" ? "Saved command" : "Tool");
    if (row.kind === "command") { renderCommandDetail(row); return; }

    if (row.kind === "broken") {
      const f = row.userFile;
      d.appendChild(el("div", null, [
        el("div", { class: "tm-title tm-title--bad" }, row.name),
        el("div", { class: "tm-does" }, "Jarvis rejected this file when it started, so none of its tools are offered. Nothing else is affected."),
        el("div", { class: "tm-badges" }, [tag("Yours", "warn"), tag("rejected", "bad"), f.enabled ? null : tag("switched off", "")]),
      ]));
      d.appendChild(el("div", null, [sectionTitle("Why it was rejected"), el("div", { class: "tm-errbox" }, row.error || "No reason was reported.")]));
      d.appendChild(el("div", { class: "tm-file" }, [el("div", { class: "tm-file__actions", style: "border-top:none" }, [
        el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => editFile(f.name) }, "Open in editor"),
        el("button", { class: "btn btn--ghost btn--sm tm-danger", type: "button", onclick: () => deleteFile(f.name) }, "Delete"),
      ])]));
      return;
    }

    const head = el("div");
    head.appendChild(el("div", { class: "tm-title" }, row.name));
    const desc = row.description || (row.userFile && row.userFile.description) || "";
    head.appendChild(el("div", { class: "tm-does" + (desc ? "" : " is-empty") }, desc || "This tool has no description."));
    const bucket = bucketOf(row.source);
    head.appendChild(el("div", { class: "tm-badges" }, [
      tag(SOURCE_LABELS[bucket], bucket === "user" ? "warn" : bucket === "mcp" ? "info" : ""),
      tag(groupLabel(row.group)),
      row.file ? tag(row.file, "", "The file this tool comes from") : tag("built in"),
      row.state === "off" ? tag("switched off", "bad") : row.state === "fileoff" ? tag("file switched off", "bad")
        : row.state === "pending" ? tag("not loaded yet", "warn") : tag("loaded", "info"),
      el("button", { class: "btn btn--ghost btn--sm", type: "button", style: "margin-left:auto", onclick: () => openInDebug(row.name) }, "Try it in Debug"),
    ]));
    d.appendChild(head);

    d.appendChild(el("div", null, [sectionTitle("Availability"), el("div", { class: "tm-sw-list" }, [availabilityRow(row)])]));

    const locked = !row.flags;
    const changed = changedKeys(row);
    d.appendChild(el("div", null, [
      sectionTitle("Safeguards", changed.length ? el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => resetFlags(row) }, "Reset to defaults") : null),
      el("div", { class: "tm-sw-list", role: "group", "aria-label": "Safeguards for " + row.name }, SAFEGUARDS.map((def) => switchRow(row, def, locked))),
    ]));

    if (locked) {
      d.appendChild(el("div", { class: "tm-note" }, row.state === "fileoff"
        ? [el("b", null, "This tool’s file is switched off. "), "Turn the file on below to change its safeguards."]
        : [el("b", null, "This tool isn’t loaded yet. "), "Refresh to pick it up."]));
    } else if (row.state === "off") {
      d.appendChild(el("div", null, [
        sectionTitle("What this means"),
        el("div", { class: "tm-outcome", "data-o": "off" }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, "⏻"),
          el("div", null, [el("b", null, "Switched off. "), offOutcome(row), " Its safeguards are kept for when you switch it back on."])]),
      ]));
    } else {
      const call = callOutcome(row.flags);
      d.appendChild(el("div", null, [
        sectionTitle("What this means"),
        el("div", { class: "tm-outcome", "data-o": call.id }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, call.id === "free" ? "▷" : call.id === "ask" ? "◈" : "◉"),
          el("div", null, [el("b", null, "When Jarvis calls it. "), call.text])]),
        el("div", { class: "tm-outcome", "data-o": "free" }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, "⏱"),
          el("div", null, [el("b", null, "Scheduled jobs. "), jobOutcome(row)])]),
      ]));
    }

    const params = paramList(row.parameters);
    d.appendChild(el("div", null, [
      sectionTitle("Parameters", String(params.length)),
      params.length
        ? el("div", { class: "tm-params" }, params.map((p) => el("div", { class: "tm-param" }, [
            el("div", null, [el("span", { class: "tm-param__name" }, p.name), p.type ? el("span", { class: "tm-param__type" }, p.type) : null,
              p.required ? el("span", { class: "tm-param__req" }, "required") : null]),
            el("div", { class: "tm-param__desc" }, p.description || "—"),
          ])))
        : el("div", { class: "tm-none" }, (row.state === "loaded" || row.state === "off") ? "Takes no parameters." : "Not available until the tool is loaded."),
    ]));

    if (row.userFile) d.appendChild(fileSection(row));
    else if (row.source === "mcp") {
      d.appendChild(el("div", { class: "tm-note tm-note--info" }, [el("b", null, "MCP tool. "), "Its server is managed in Menu → MCP Servers. Switching this one tool off leaves the rest of the server alone."]));
    }
  }

  // A saved command (commands.json): read-only here apart from the on/off switch.
  // Its own safeguards and steps are edited in the Commands panel.
  function renderCommandDetail(row) {
    const d = dom.detail, spec = row.spec || {};
    d.appendChild(el("div", null, [
      el("div", { class: "tm-title" }, row.name),
      el("div", { class: "tm-does" + (row.description ? "" : " is-empty") }, row.description || "This command has no description."),
      el("div", { class: "tm-badges" }, [tag("Command", "info"), tag("saved command"), row.state === "off" ? tag("switched off", "bad") : tag("available", "info")]),
    ]));
    d.appendChild(el("div", null, [sectionTitle("Availability"), el("div", { class: "tm-sw-list" }, [availabilityRow(row)])]));
    d.appendChild(el("div", null, [
      sectionTitle("What this means"),
      row.state === "off"
        ? el("div", { class: "tm-outcome", "data-o": "off" }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, "⏻"),
            el("div", null, [el("b", null, "Switched off. "), offOutcome(row)])])
        : el("div", { class: "tm-outcome", "data-o": "free" }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, "▷"),
            el("div", null, [el("b", null, "Available. "), "Jarvis can find it with search_commands and run it with run_command or run_chain."])]),
    ]));
    const yes = (v) => (v ? "on" : "off");
    d.appendChild(el("div", null, [
      sectionTitle("Safeguards", "set in the Commands panel"),
      el("div", { class: "tm-params" }, [
        el("div", { class: "tm-param" }, [el("div", { class: "tm-param__name" }, "Ask before running"), el("div", { class: "tm-param__desc" }, yes(spec.confirm_required))]),
        el("div", { class: "tm-param" }, [el("div", { class: "tm-param__name" }, "AI overview"), el("div", { class: "tm-param__desc" }, yes(spec.ai_review))]),
      ]),
    ]));
    const steps = commandSteps(spec);
    d.appendChild(el("div", null, [
      sectionTitle("What it runs", steps.length ? String(steps.length) + (steps.length === 1 ? " step" : " steps") : ""),
      steps.length ? el("pre", { class: "tm-pre", tabindex: "0", "aria-label": "Command steps" }, steps.join("\n")) : el("div", { class: "tm-none" }, "No steps."),
    ]));
    const vars = commandVars(spec);
    if (vars.length) {
      d.appendChild(el("div", null, [
        sectionTitle("Variables", String(vars.length)),
        el("div", { class: "tm-params" }, vars.map((v) => el("div", { class: "tm-param" }, [
          el("div", null, [el("span", { class: "tm-param__name" }, v.name), v.hasDefault ? null : el("span", { class: "tm-param__req" }, "required")]),
          el("div", { class: "tm-param__desc" }, (v.description || "—") + (v.hasDefault ? "  (default: " + String(v.def) + ")" : "")),
        ]))),
      ]));
    }
  }

  function renderDetail() {
    if (state.view === "editor") return;            // the editor owns the pane while open
    const d = dom.detail;
    dom.foot.hidden = false;
    if (state.view === "import" && state.draft) { renderImportReview(); return; }
    setText(dom.midTitle, "Tool");
    const row = selectedRow();
    if (!row) {
      clear(d);
      d.appendChild(emptyBlock(state.loading ? ["Reading the catalogue…"]
        : state.rows.length ? ["Pick a tool on the left to see how it behaves."]
        : state.toolsError ? ["Nothing to show while the catalogue is unavailable."] : ["No tools loaded."]));
      return;
    }
    renderToolDetail(row);
  }

  /* ---- right pane ---------------------------------------------------------- */

  function legendItem(label, n, color) {
    const i = el("i"); i.style.setProperty("--seg-c", color);
    return el("span", null, [i, label, el("b", null, String(n))]);
  }

  function renderOverview(side) {
    const cov = coverage(state.rows);
    const segs = [["Shipped", cov.bySource.shipped, "var(--accent)"], ["Yours", cov.bySource.user, "var(--accent-secondary)"], ["MCP", cov.bySource.mcp, "var(--tm-sum)"]];
    const bar = el("div", { class: "tm-bar", role: "img", "aria-label": "Loaded tools by source" });
    segs.forEach(([, n, c]) => { const s = el("i"); s.style.setProperty("--seg-c", c); s.style.width = (cov.total ? (n / cov.total) * 100 : 0) + "%"; bar.appendChild(s); });
    side.appendChild(el("div", null, [
      el("div", { class: "tm-big" }, [el("span", { class: "tm-big__n" }, String(cov.total)), el("span", { class: "tm-big__l" }, "tools available")]),
      bar, el("div", { class: "tm-legend" }, segs.map(([l, n, c]) => legendItem(l, n, c))),
    ]));

    const cvDefs = [["confirm_required", "Ask before running", "var(--tm-ask)"], ["ai_review", "AI overview", "var(--tm-ai)"], ["approval_summary", "Approval summary", "var(--tm-sum)"]];
    side.appendChild(el("div", null, [sectionTitle("Safeguards in use"), el("div", { class: "tm-cov" }, cvDefs.map(([k, label, c]) => {
      const n = cov[k]; const b = el("div", { class: "tm-bar" }); const s = el("i"); s.style.setProperty("--seg-c", c); s.style.width = (cov.total ? (n / cov.total) * 100 : 0) + "%"; b.appendChild(s);
      return el("div", { class: "tm-cov__row" }, [el("span", null, label), el("span", { class: "tm-cov__n" }, n + " / " + cov.total), b]);
    })), el("div", { class: "tm-hint", style: "margin-top:10px" }, cov.none + " of " + cov.total + " tools run with no safeguard on.")]));

    const attn = state.rows.filter(needsAttention);
    const list = el("div", { class: "tm-attn" });
    attn.slice(0, 12).forEach((r) => {
      list.appendChild(el("button", { class: "tm-attn__item", type: "button", "data-tm-state": r.state, onclick: () => select(r.id) }, [
        el("div", { class: "tm-attn__name" }, r.name),
        el("div", { class: "tm-attn__why" }, r.state === "failed" ? String(r.error).slice(0, 110) : "Not loaded yet — refresh"),
      ]));
    });
    side.appendChild(el("div", null, [sectionTitle("Needs attention", attn.length ? String(attn.length) : ""),
      attn.length ? list : el("div", { class: "tm-clear" }, "✓ Everything loads cleanly.")]));

    const offRows = state.rows.filter(isOff);
    if (offRows.length) {
      const offList = el("div", { class: "tm-attn" });
      offRows.slice(0, 10).forEach((r) => {
        offList.appendChild(el("button", { class: "tm-attn__item", type: "button", "data-tm-state": r.state, onclick: () => select(r.id) }, [
          el("div", { class: "tm-attn__name" }, r.name),
          el("div", { class: "tm-attn__why" }, (r.kind === "command" ? "Saved command" : "Tool") + (r.state === "fileoff" ? " · its file is switched off" : " · switched off by you")),
        ]));
      });
      side.appendChild(el("div", null, [sectionTitle("Switched off", String(offRows.length)), offList,
        offRows.length > 10 ? el("div", { class: "tm-hint", style: "margin-top:6px" }, "+" + (offRows.length - 10) + " more — use the Off filter.") : null]));
    }

    side.appendChild(el("div", null, [sectionTitle("Add a tool"), el("div", { class: "tm-actions" }, [
      el("button", { class: "btn btn--primary", type: "button", onclick: () => enterEditor({ mode: "new" }) }, "Create a tool…"),
      el("button", { class: "btn btn--ghost", type: "button", onclick: () => dom.file.click() }, "Import a .py file…"),
      el("div", { class: "tm-hint" }, "You can also drop a .py file anywhere on this panel. Imports always ask first and are checked before anything is written."),
    ])]));
  }

  function runCard(fileName, tools, note) {
    if (!state.run || state.run.file !== fileName) state.run = { file: fileName, tool: (tools || [])[0] || "", args: "{}", busy: false, result: null, error: "" };
    const run = state.run;
    const card = el("div", { class: "tm-run" });
    card.appendChild(sectionTitle("Run a test"));
    if (tools && tools.length > 1) {
      card.appendChild(el("label", { class: "tm-field", style: "margin-bottom:8px" }, [el("span", { class: "tm-field__label" }, "Tool"),
        el("select", { class: "tm-select", onchange: (e) => { run.tool = e.target.value; } }, tools.map((t) => el("option", { value: t, selected: t === run.tool }, t)))]));
    }
    const ta = el("textarea", { class: "tm-input", spellcheck: "false", "aria-label": "Arguments as JSON", placeholder: "{}" });
    ta.value = run.args;
    ta.addEventListener("input", () => { run.args = ta.value; });
    card.appendChild(ta);
    card.appendChild(el("div", { class: "tm-hint", style: "margin-top:6px" }, (note || "") + "This runs the real tool — any side effect it has will happen."));
    const out = el("div");
    card.appendChild(el("div", { class: "tm-row" }, [el("button", { class: "btn btn--ghost btn--sm", type: "button", disabled: run.busy || null,
      onclick: () => runTest(out) }, run.busy ? "Running…" : "Run")]));
    card.appendChild(out);
    paintRunResult(out);
    return card;
  }

  function paintRunResult(out) {
    clear(out);
    const run = state.run;
    if (!run) return;
    if (run.error) out.appendChild(el("div", { class: "tm-errbox", style: "margin-top:8px" }, run.error));
    else if (run.result !== null) out.appendChild(el("pre", { class: "tm-pre", style: "margin-top:8px" }, typeof run.result === "string" ? run.result : JSON.stringify(run.result, null, 2)));
  }

  async function runTest(out) {
    const run = state.run;
    if (!run || run.busy) return;
    const parsed = parseArgs(run.args);
    run.error = ""; run.result = null;
    if (!parsed.ok) { run.error = parsed.error; paintRunResult(out); return; }
    run.busy = true; renderSide();
    try {
      const r = await ctools("/" + encodeURIComponent(run.file) + "/run", { method: "POST", body: JSON.stringify({ tool: run.tool, arguments: parsed.value }) });
      run.result = r; if (r && r.ok === false && r.error) run.error = String(r.error);
    } catch (e) { run.error = e.message; }
    run.busy = false; renderSide();
  }

  function renderSide() {
    if (!dom) return;
    const side = dom.side;
    const keepTop = side.scrollTop;
    clear(side);
    if (state.view === "editor" && state.editor) {
      renderEditorSide(side);
    } else {
      renderOverview(side);
      const row = selectedRow();
      if (state.view === "tool" && row && row.userFile && row.userFile.valid && row.userFile.enabled) {
        side.appendChild(runCard(row.userFile.name, row.userFile.tools, ""));
      }
    }
    side.scrollTop = keepTop;
  }

  /* ---- switches (talks to /api/tools/safety) -------------------------------- */

  async function setFlag(name, key, value) {
    const row = rowByName(name);
    const token = name + ":" + key;
    if (!row || !row.flags || state.busy.has(token)) return;
    const prev = row.flags[key];
    row.flags[key] = value;
    state.busy.add(token);
    refreshAfterFlag(row);
    try {
      const res = await api("/api/tools/safety", { method: "POST", body: JSON.stringify({ name, key, value }) });
      KEYS.forEach((k) => { if (typeof res[k] === "boolean") row.flags[k] = res[k]; });
      const def = SAFEGUARDS.find((s) => s.key === key);
      setStatusLine((def ? def.title : key) + " " + (row.flags[key] ? "on" : "off") + " for " + name);
    } catch (e) {
      row.flags[key] = prev;
      toast("Couldn’t change " + key.replace(/_/g, " ") + " for " + name + ": " + e.message, "error");
    } finally {
      state.busy.delete(token);
      refreshAfterFlag(row);
    }
  }

  function refreshAfterFlag(row) {
    renderList();
    if (state.view === "tool" && state.selected === row.id) { const t = dom.detail.scrollTop; renderToolDetail(row); dom.detail.scrollTop = t; }
    renderSide();
  }

  async function resetFlags(row) {
    for (const key of changedKeys(row)) await setFlag(row.name, key, row.defaults[key]);
  }

  // Switch a tool or saved command off/on for the MODEL. Switching OFF first asks
  // the server which scheduled jobs name it (they would fail when they fire) and
  // confirms only if there are some, or if the lookup itself failed — never
  // silently. Switching back on never asks. Nothing changes until the server
  // confirms it saved, so the switch never shows a state that wasn't stored.
  async function setAvailable(row, available) {
    const token = row.name + ":enabled";
    if (state.busy.has(token) || row.protectedReason) return;
    if (row.state !== "loaded" && row.state !== "off") return;
    const kind = row.kind === "command" ? "command" : "tool";
    const what = kind === "command" ? "saved command" : "tool";
    if (!available) {
      let jobs = [], unknown = false;
      try {
        const res = await api("/api/disabled/dependents?kind=" + kind + "&name=" + encodeURIComponent(row.name));
        jobs = (res && res.jobs) || [];
      } catch (_) { unknown = true; }
      if (jobs.length || unknown) {
        const ok = await UI().confirm({
          title: "Switch off " + row.name + "?", level: "warn", focusCancel: true, confirmLabel: "Switch off",
          body: unknown
            ? "Couldn’t check whether any scheduled jobs use this " + what + ". If one does, it will fail when it next fires. Switch it off anyway?"
            : "These scheduled jobs use this " + what + ". They will fail — with a notification — the next time they fire:\n\n" + dependentLines(jobs).join("\n")
              + "\n\nJobs that ask Jarvis to decide for itself can’t be checked.",
        });
        if (!ok) return;
      }
    }
    state.busy.add(token); refreshAfterAvailability(row);
    try {
      const path = kind === "command" ? "/api/commands/" + encodeURIComponent(row.name) + "/disabled" : "/api/tools/disabled";
      await api(path, { method: "POST", body: JSON.stringify({ name: row.name, value: !available }) });
      row.state = available ? "loaded" : "off";
      if (kind === "command") {
        state.disabledCommands = available ? state.disabledCommands.filter((n) => n !== row.name) : state.disabledCommands.concat([row.name]);
      }
      setStatusLine(row.name + (available ? " is available to Jarvis again" : " is switched off — Jarvis can’t see or use it"));
    } catch (e) {
      toast("Couldn’t " + (available ? "switch on " : "switch off ") + row.name + ": " + e.message, "error");
    } finally {
      state.busy.delete(token);
      renderChips(); refreshAfterAvailability(row);
    }
  }

  function refreshAfterAvailability(row) {
    renderList();
    if (state.view === "tool" && state.selected === row.id) { const t = dom.detail.scrollTop; renderToolDetail(row); dom.detail.scrollTop = t; }
    renderSide();
  }

  /* ---- user-tool files -------------------------------------------------------- */

  async function setFileEnabled(fileName, enabled) {
    const token = "file:" + fileName;
    if (state.busy.has(token)) return;
    state.busy.add(token); renderDetail();
    try {
      const r = await ctools("/" + encodeURIComponent(fileName) + "/enabled", { method: "POST", body: JSON.stringify({ enabled }) });
      if (r && r.ok === false) throw new Error(r.error || "Couldn’t switch the file.");
      await loadAll({ keepSelection: true, quiet: true });
      toast(fileName + (enabled ? " switched on." : " switched off — its tools are no longer offered to the model."), "success");
    } catch (e) { toast(e.message, "error"); }
    state.busy.delete(token);
    renderMain();
  }

  async function deleteFile(fileName) {
    const ok = await UI().confirm({
      title: "Delete " + fileName + "?", level: "error", focusCancel: true, confirmLabel: "Delete",
      body: "The file is removed from ~/.jarvis/tools. A .bak copy from the last save is left behind.",
    });
    if (!ok) return;
    try {
      const r = await ctools("/" + encodeURIComponent(fileName), { method: "DELETE" });
      if (r && r.ok === false) throw new Error(r.error || "Couldn’t delete.");
      if (state.editor && state.editor.origName === fileName) { state.editor = null; state.view = "tool"; }
      state.selected = null; state.run = null;
      await loadAll({ quiet: true });
      toast("Deleted " + fileName + ".", "success");
    } catch (e) { toast(e.message, "error"); }
  }

  function openInDebug(name) {
    const item = $("#menu-item-debug");
    if (!item) { toast("Couldn’t find the Debug panel.", "warn"); return; }
    close();
    item.click();
    const search = $("#debug-search");
    if (search) { search.value = name; search.dispatchEvent(new Event("input", { bubbles: true })); }
    let tries = 0;
    const timer = setInterval(() => {
      const hit = Array.from(document.querySelectorAll("#debug-tool-list .debug-tool-card"))
        .find((c) => { const n = c.querySelector(".debug-tool-card__name"); return n && n.textContent.trim() === name; });
      if (hit) { clearInterval(timer); hit.click(); } else if (++tries > 50) clearInterval(timer);
    }, 100);
  }

  /* =======================================================================
   * Editor (the reworked "Create a tool")
   * ===================================================================== */

  async function leaveEditor() {
    const ed = state.editor;
    if (state.view === "import" && state.draft) { state.draft = null; state.view = "tool"; return true; }
    if (state.view !== "editor" || !ed) return true;
    if (ed.dirty) {
      const ok = await UI().confirm({ title: "Discard unsaved changes?", level: "warn", focusCancel: true, confirmLabel: "Discard",
        body: "You’ve edited " + (ed.name || "this tool") + " without saving." });
      if (!ok) return false;
    }
    disposeEditor();
    state.editor = null; state.view = "tool";
    restoreDetailBox();
    return true;
  }

  function disposeEditor() {
    const ed = state.editor;
    if (ed && ed.cm) { try { ed.cm.dispose(); } catch (_) { /* already gone */ } ed.cm = null; }
  }

  async function editFile(fileName) {
    if (!(await leaveEditor())) return;
    try {
      const data = await ctools("/" + encodeURIComponent(fileName));
      await enterEditor({ mode: "edit", name: fileName, source: data.source || "", enabled: data.enabled !== false, initial: data });
    } catch (e) { toast("Couldn’t open " + fileName + ": " + e.message, "error"); }
  }

  async function enterEditor(opts) {
    if (!(await leaveEditor())) return;
    const mode = opts.mode || "new";
    state.editor = {
      mode, origName: mode === "edit" ? opts.name : "", name: mode === "edit" ? opts.name : "", source: opts.source || "",
      template: "minimal", dirty: false, saved: mode === "edit", result: null, errorLine: null, busy: "",
      nameTouched: false,
    };
    if (opts.initial && opts.initial.valid === false) {
      state.editor.result = { ok: false, stage: opts.initial.stage || "contract", error: opts.initial.error || "this file won’t load" };
      state.editor.errorLine = parseErrorLine(state.editor.result.error);
    }
    state.view = "editor"; state.draft = null; state.run = null;
    buildEditor();
    renderSide();
    renderList();
    if (mode === "new") await loadTemplate(state.editor.template, true);
    if (state.editor.errorLine) { state.editor.cm.setErrorLine(state.editor.errorLine); revealLine(state.editor.errorLine); }
    if (mode === "new") { const n = $("#tm-ed-name"); if (n) n.focus(); } else state.editor.cm.focus();
  }

  async function loadTemplate(id, silent) {
    const ed = state.editor;
    if (!ed) return;
    try {
      const data = await ctools("/draft?template=" + encodeURIComponent(id));
      ed.source = (data && data.source) || "";
    } catch (_) { ed.source = ""; if (!silent) toast("Couldn’t load that template — starting empty.", "warn"); }
    ed.template = id; ed.dirty = false;
    if (ed.cm) ed.cm.setValue(ed.source, { silent: true });
    updateChip(); updateOutline();
  }

  function updateChip() {
    const ed = state.editor, chip = $("#tm-ed-chip");
    if (!ed || !chip) return;
    chip.className = "tm-state-chip" + (ed.dirty ? " is-dirty" : ed.saved ? " is-saved" : "");
    chip.textContent = ed.dirty ? "Unsaved changes" : ed.saved ? "Saved" : "Not saved yet";
  }

  function nameHelp() {
    const ed = state.editor, input = $("#tm-ed-name"), help = $("#tm-ed-name-help");
    if (!ed || !input || !help) return;
    const problem = ed.mode === "edit" ? "" : nameProblem(input.value, userFileNames(), ed.mode);
    const show = ed.nameTouched && problem;
    input.classList.toggle("is-invalid", !!show);
    help.className = "tm-field__help" + (show ? " is-bad" : "");
    help.textContent = show ? problem : ed.mode === "edit" ? "The file name can’t change here." : "lower_snake_case — becomes ~/.jarvis/tools/<name>.py";
  }

  function buildEditor() {
    const ed = state.editor, d = dom.detail;
    clear(d);
    setText(dom.midTitle, ed.mode === "edit" ? "Editing " + ed.origName : "Create a tool");
    dom.foot.hidden = true;

    const name = el("input", { class: "tm-input", id: "tm-ed-name", type: "text", autocomplete: "off", spellcheck: "false",
      placeholder: "my_tool", value: ed.name, readonly: ed.mode === "edit" || null, "aria-describedby": "tm-ed-name-help", style: "width:210px" });
    name.addEventListener("input", () => { ed.name = name.value.trim().toLowerCase(); ed.nameTouched = true; ed.dirty = true; updateChip(); nameHelp(); });
    const tpl = el("select", { class: "tm-select", id: "tm-ed-template", "aria-label": "Start from a template", style: "min-width:150px",
      onchange: async (e) => {
        const id = e.target.value;
        if (ed.dirty && ed.source.trim() && !(await UI().confirm({ title: "Replace what’s in the editor?", level: "warn", focusCancel: true,
          confirmLabel: "Replace", body: "Loading a template overwrites the code you’ve typed." }))) { e.target.value = ed.template; return; }
        await loadTemplate(id);
      } }, (state.templates.length ? state.templates : [{ id: "minimal", label: "Minimal" }]).map((t) => el("option", { value: t.id, title: t.hint || "" }, t.label)));
    tpl.value = ed.template;

    const actions = el("div", { class: "tm-editor__actions" }, [
      el("button", { class: "btn btn--primary", type: "button", id: "tm-ed-save", onclick: saveEditor, title: "Save (Ctrl+S)" }, "Save"),
      el("button", { class: "btn btn--ghost", type: "button", id: "tm-ed-check", onclick: validateEditor, title: "Checks the file without saving it (Ctrl+Enter)" }, "Validate"),
      el("button", { class: "btn btn--ghost", type: "button", onclick: backFromEditor }, "Back"),
    ]);
    const bar = el("div", { class: "tm-editor__bar" }, [
      el("div", { class: "tm-field" }, [el("label", { class: "tm-field__label", for: "tm-ed-name" }, "File name"), name, el("span", { class: "tm-field__help", id: "tm-ed-name-help" })]),
      ed.mode === "new" ? el("div", { class: "tm-field" }, [el("label", { class: "tm-field__label", for: "tm-ed-template" }, "Template"), tpl, el("span", { class: "tm-field__help" }, " ")]) : null,
      el("span", { class: "tm-state-chip", id: "tm-ed-chip" }),
      actions,
    ]);

    ed.cm = makeCodeEditor(ed);
    d.appendChild(el("div", { class: "tm-editor" }, [bar, ed.cm.el]));
    d.style.padding = "0"; d.style.overflow = "hidden"; d.style.display = "flex";
    if (ed.cm.remeasure) ed.cm.remeasure();
    updateChip(); nameHelp();
  }

  // The editor itself is code-editor.js. If that script didn't load, a plain
  // textarea with the same small interface keeps the manager usable (the same
  // idea as I-B18(c): say so instead of leaving a dead panel).
  function makeCodeEditor(ed) {
    const common = {
      value: ed.source, label: "Tool source code", placeholder: "Pick a template above, or write a tool here.",
      onChange: (v) => { ed.source = v; ed.dirty = true; ed.errorLine = null; updateChip(); scheduleOutline(); },
      onSave: saveEditor, onValidate: validateEditor,
    };
    if (global.JarvisCodeEditor) {
      return global.JarvisCodeEditor.create(Object.assign(common, {
        suggestEnabled: state.suggest, suggest: suggestFetcher,
        onSuggestToggle: (on) => { state.suggest = on; savePrefs(); },
      }));
    }
    const ta = el("textarea", { class: "tm-textarea", spellcheck: "false", "aria-label": common.label, placeholder: common.placeholder });
    ta.value = ed.source;
    ta.addEventListener("input", () => common.onChange(ta.value));
    ta.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveEditor(); }
      else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); validateEditor(); }
    });
    const wrap = el("div", { class: "tm-code" }, [el("div", { class: "tm-note tm-note--bad", style: "margin:10px" }, "The code editor script (code-editor.js) didn’t load, so this is a plain text box. Saving and validating still work."), ta]);
    return {
      el: wrap, getValue: () => ta.value, focus: () => ta.focus(), dispose() {}, remeasure() {},
      setValue(t) { ta.value = t; }, setErrorLine() {}, revealLine() {}, gotoLine() { ta.focus(); },
      insertSnippet() {}, setSuggestEnabled() {}, isSuggestEnabled: () => false, fallback: true,
    };
  }

  async function suggestFetcher(req) {
    const ed = state.editor;
    const name = ed && NAME_RE.test(ed.name) ? ed.name : "draft";
    const r = await ctools("/" + encodeURIComponent(name) + "/suggest", { method: "POST", body: JSON.stringify({ source: req.source, cursor: req.cursor }), signal: req.signal });
    if (!r || r.ok === false) throw new Error((r && r.error) || "no suggestion");
    return r.text || "";
  }

  function restoreDetailBox() { dom.detail.style.padding = ""; dom.detail.style.overflow = ""; dom.detail.style.display = ""; }

  async function backFromEditor() {
    if (!(await leaveEditor())) return;
    restoreDetailBox();
    renderMain();
  }

  // Bring a line into the editor's viewport (a marker you can't see isn't a marker).
  function revealLine(n) {
    const ed = state.editor;
    if (ed && ed.cm && n) ed.cm.revealLine(n);
  }

  function applyResult(result) {
    const ed = state.editor;
    ed.result = result;
    ed.errorLine = result && !result.ok ? parseErrorLine(result.error) : null;
    if (ed.cm) ed.cm.setErrorLine(ed.errorLine || 0);
    renderSide();
    if (ed.errorLine) revealLine(ed.errorLine);
  }

  async function validateEditor() {
    const ed = state.editor;
    if (!ed || ed.busy) return;
    ed.busy = "check"; renderSide();
    try {
      const name = ed.name || "draft";
      applyResult(await ctools("/" + encodeURIComponent(NAME_RE.test(name) ? name : "draft") + "/check", { method: "POST", body: JSON.stringify({ source: ed.source }) }));
    } catch (e) { applyResult({ ok: false, stage: "error", error: e.message }); }
    ed.busy = ""; renderSide();
  }

  async function saveEditor() {
    const ed = state.editor;
    if (!ed || ed.busy) return;
    ed.nameTouched = true; nameHelp();
    const problem = nameProblem(ed.name, userFileNames(), ed.mode);
    if (problem) { $("#tm-ed-name").focus(); return; }
    ed.busy = "save"; renderSide();
    try {
      const r = await ctools("/" + encodeURIComponent(ed.name), { method: "PUT", body: JSON.stringify({ source: ed.source }) });
      applyResult(r);
      if (r && r.ok) {
        ed.mode = "edit"; ed.origName = ed.name; ed.dirty = false; ed.saved = true;
        setText(dom.midTitle, "Editing " + ed.origName);
        // From here on this IS that file. Lock the name (a retyped name in "edit"
        // mode would skip the collision check and could overwrite another tool)
        // and drop the template picker (it would replace the saved file's code).
        const nameInput = $("#tm-ed-name");
        if (nameInput) { nameInput.readOnly = true; nameInput.value = ed.origName; }
        const tplField = $("#tm-ed-template");
        if (tplField && tplField.closest(".tm-field")) tplField.closest(".tm-field").remove();
        toast("Saved " + ed.name + ". " + (r.note || ""), "success");
        await loadAll({ quiet: true, keepView: true });
        nameHelp();
      }
    } catch (e) { applyResult(e.data && e.data.error ? Object.assign({ ok: false }, e.data) : { ok: false, stage: "error", error: e.message }); }
    ed.busy = ""; updateChip(); renderSide();
  }

  /* ---- editor side panel: outline, snippets, suggestions, shortcuts (L.33) ---- */

  let outlineTimer = 0;
  function scheduleOutline() {
    if (outlineTimer) clearTimeout(outlineTimer);
    outlineTimer = setTimeout(() => { outlineTimer = 0; updateOutline(); }, 250);
  }

  function updateOutline() {
    const box = $("#tm-ed-outline"), ed = state.editor;
    if (!box || !ed) return;
    clear(box);
    const items = global.JarvisCodeEditor ? global.JarvisCodeEditor._pure.outline(ed.source) : [];
    if (!items.length) { box.appendChild(el("div", { class: "tm-hint" }, "Functions, classes and CONSTANTS in the file are listed here. Click one to jump to it.")); return; }
    const glyph = { def: "ƒ", class: "C", const: "#" };
    items.forEach((it) => box.appendChild(el("button", { class: "tm-outline__item", type: "button", title: "Go to line " + it.line,
      onclick: () => { if (ed.cm) ed.cm.gotoLine(it.line); } }, [
      el("span", { class: "tm-outline__kind tm-outline__kind--" + it.kind, "aria-hidden": "true" }, glyph[it.kind] || "·"),
      el("span", { class: "tm-outline__name" }, it.name),
      el("span", { class: "tm-outline__line" }, String(it.line)),
    ])));
  }

  function insertSection() {
    const list = global.JarvisCodeEditor ? global.JarvisCodeEditor._pure.SNIPPETS : [];
    const wrap = el("div", null, [sectionTitle("Insert")]);
    if (!list.length) { wrap.appendChild(el("div", { class: "tm-hint" }, "Snippets need the code editor script.")); return wrap; }
    const grid = el("div", { class: "tm-snips" });
    list.slice(0, 6).forEach((sn) => grid.appendChild(el("button", { class: "tm-snip", type: "button", title: sn.detail,
      onclick: () => { const ed = state.editor; if (ed && ed.cm) ed.cm.insertSnippet(sn.label); } }, [
      el("code", null, sn.label), el("span", null, sn.detail)])));
    wrap.appendChild(grid);
    wrap.appendChild(el("div", { class: "tm-hint", style: "margin-top:8px" }, "Or type a trigger such as jtool in the editor and press Tab."));
    return wrap;
  }

  function suggestSection() {
    const ed = state.editor;
    const wrap = el("div", null, [sectionTitle("Jarvis suggestions", state.suggest ? "on" : "off")]);
    if (!global.JarvisCodeEditor) { wrap.appendChild(el("div", { class: "tm-hint" }, "Needs the code editor script.")); return wrap; }
    const row = el("div", { class: "tm-switchrow" }, [
      switchEl({ on: state.suggest, label: "Jarvis inline suggestions", onToggle: () => {
        if (ed && ed.cm) ed.cm.setSuggestEnabled(!state.suggest); else { state.suggest = !state.suggest; savePrefs(); }
        renderSide();
      } }),
      el("span", null, state.suggest ? "Suggesting code as you pause" : "Off — no code is sent anywhere"),
    ]);
    wrap.appendChild(row);
    wrap.appendChild(el("div", { class: "tm-hint", style: "margin-top:8px" },
      "After a pause at the end of a line, Jarvis shows a dimmed suggestion. Tab accepts it, Ctrl+→ takes one word, Esc dismisses it, Alt+\\ asks right now. " +
      "Each suggestion sends the code around the caret to your AI provider (a few hundred tokens), at most 40 per editing session. A suggestion is only text on screen until you accept it; saving is still yours."));
    return wrap;
  }

  function shortcutsSection() {
    const rows = [
      ["Ctrl+S", "Save"], ["Ctrl+Enter", "Validate"], ["Ctrl+Space", "Completions"], ["Ctrl+/", "Comment line"],
      ["Tab / Shift+Tab", "Indent / outdent"], ["Alt+↑ / ↓", "Move line"], ["Shift+Alt+↑ / ↓", "Copy line"],
    ];
    const d = el("details", { class: "tm-shortcuts" }, [el("summary", null, "Keyboard shortcuts")]);
    const t = el("div", { class: "tm-keys" });
    rows.forEach((r) => t.appendChild(el("div", null, [el("kbd", { class: "tm-kbd" }, r[0]), el("span", null, r[1])])));
    d.appendChild(t);
    return el("div", null, [d]);
  }

  function checklistLines(result) {
    const lines = [];
    (result.checklist_problems || []).forEach((p) => lines.push("Ignored (the tool still loads): " + p));
    if ((result.checklist_missing || []).length) lines.push("No Test Checklist entry yet for: " + result.checklist_missing.join(", ") + ". Add a TEST_CHECKLIST dict — every template has an example.");
    return lines;
  }

  function renderEditorSide(side) {
    const ed = state.editor, r = ed.result;
    const stages = stageStates(r);
    const labels = ["Syntax", "Imports", "Contract"];
    const glyph = { ok: "✓", bad: "✕", skip: "–", idle: "○" };
    side.appendChild(el("div", null, [
      sectionTitle("Check", ed.busy ? (ed.busy === "save" ? "saving…" : "checking…") : ""),
      el("div", { class: "tm-stages", role: "list" }, labels.map((l, i) => el("div", { class: "tm-stage", role: "listitem", "data-s": stages[i], "aria-label": l + ": " + stages[i] },
        [el("span", { class: "tm-stage__glyph", "aria-hidden": "true" }, glyph[stages[i]]), l]))),
    ]));
    const res = el("div", { class: "tm-result", "aria-live": "polite" });
    if (!r) res.appendChild(el("div", { class: "tm-hint" }, "Validate to see whether the file would load. Save checks it too and refuses to write a file Jarvis would reject — nothing fails silently at the next start."));
    else if (r.ok) {
      res.appendChild(el("div", null, ["Loads cleanly. Provides ", el("b", null, (r.tools || []).join(", ") || "—"), r.group ? " in group “" + r.group + "”." : "."]));
      checklistLines(r).forEach((l) => res.appendChild(el("div", { class: "tm-note", style: "margin-top:8px" }, l)));
    } else {
      res.appendChild(el("div", { class: "tm-errbox" }, (r.stage ? "[" + r.stage + "] " : "") + (r.error || "Unknown error") + (ed.errorLine ? "\n→ line " + ed.errorLine + " is marked in the editor" : "")));
      if (ed.errorLine && ed.cm) res.appendChild(el("button", { class: "btn btn--ghost tm-jump", type: "button", onclick: () => ed.cm.gotoLine(ed.errorLine) }, "Go to line " + ed.errorLine));
      if (r.hint) res.appendChild(el("div", { class: "tm-note", style: "margin-top:8px" }, r.hint));
    }
    side.appendChild(res);
    if (ed.mode === "edit" && state.files.find((f) => f.name === ed.origName && f.valid && f.enabled)) {
      const f = state.files.find((x) => x.name === ed.origName);
      side.appendChild(runCard(f.name, f.tools, ed.dirty ? "Runs the saved file, not your unsaved edits. " : ""));
    }
    side.appendChild(el("div", null, [sectionTitle("Outline"), el("div", { class: "tm-outline", id: "tm-ed-outline" })]));
    side.appendChild(insertSection());
    side.appendChild(suggestSection());
    side.appendChild(shortcutsSection());
    side.appendChild(el("div", null, [sectionTitle("Good to know"), el("div", { class: "tm-hint" },
      "A custom tool is Python running as you. The model can’t write these files — that’s deliberate. Tools you save aren’t asked about again; imports are. Safeguards for a new tool appear in the list once it’s saved."),
    ]));
    updateOutline();
  }

  /* =======================================================================
   * Import (confirmation first: the file is arbitrary Python)
   * ===================================================================== */

  function startImport(file) {
    const bad = importProblem(file);
    if (bad) { toast(bad, "warn"); return; }
    const reader = new FileReader();
    reader.onerror = () => toast("Couldn’t read " + file.name + ".", "error");
    reader.onload = async () => {
      const text = String(reader.result || "");
      const problem = sourceProblem(text);
      if (problem) { toast(file.name + ": " + problem, "warn"); return; }
      if (!(await leaveEditor())) return;
      restoreDetailBox();
      state.draft = { fileName: file.name, name: importNameFromFile(file.name), source: text, size: file.size, result: null, busy: false, nameTouched: true };
      state.view = "import"; state.run = null;
      renderMain();
      const n = $("#tm-imp-name"); if (n) { n.focus(); n.select(); }
    };
    reader.readAsText(file);
  }

  function renderImportReview() {
    const dr = state.draft, d = dom.detail;
    clear(d);
    setText(dom.midTitle, "Import review");
    const nameErr = () => nameProblem(dr.name, userFileNames(), "new");
    const help = el("span", { class: "tm-field__help", id: "tm-imp-help" });
    const input = el("input", { class: "tm-input", id: "tm-imp-name", type: "text", value: dr.name, autocomplete: "off", spellcheck: "false", style: "width:240px", "aria-describedby": "tm-imp-help" });
    const go = el("button", { class: "btn btn--primary", type: "button", disabled: dr.busy || null, onclick: confirmImport }, dr.busy ? "Checking…" : "Import…");
    const paint = () => { const p = nameErr(); input.classList.toggle("is-invalid", !!p); help.className = "tm-field__help" + (p ? " is-bad" : ""); help.textContent = p || "Saved as ~/.jarvis/tools/" + dr.name + ".py"; go.disabled = !!p || dr.busy; };
    input.addEventListener("input", () => { dr.name = input.value.trim().toLowerCase(); paint(); });
    const lines = dr.source.split("\n");
    const preview = lines.slice(0, 80).join("\n") + (lines.length > 80 ? "\n… " + (lines.length - 80) + " more lines" : "");

    d.appendChild(el("div", null, [el("div", { class: "tm-title" }, dr.fileName),
      el("div", { class: "tm-does" }, "Review before importing. Nothing has been sent to Jarvis yet and nothing is written until you confirm."),
      el("div", { class: "tm-badges" }, [tag(formatBytes(dr.size)), tag(lineCount(dr.source) + " lines"), tag("not loaded yet", "warn")])]));
    d.appendChild(el("div", { class: "tm-note" }, [el("b", null, "This is Python that runs as you. "), "Importing a file means Jarvis will execute it — first to check it, then every time it starts. Only import tools you trust or have read."]));
    d.appendChild(el("div", { class: "tm-field" }, [el("label", { class: "tm-field__label", for: "tm-imp-name" }, "Save as"), input, help]));
    if (dr.result && !dr.result.ok) {
      d.appendChild(el("div", null, [sectionTitle("Refused — nothing was written"), el("div", { class: "tm-errbox" }, (dr.result.stage ? "[" + dr.result.stage + "] " : "") + (dr.result.error || "Unknown error")),
        dr.result.hint ? el("div", { class: "tm-note", style: "margin-top:8px" }, dr.result.hint) : null]));
    }
    d.appendChild(el("div", null, [sectionTitle("Source", "first " + Math.min(80, lines.length) + " lines"), el("pre", { class: "tm-pre tm-preview", tabindex: "0", "aria-label": "File source preview" }, preview)]));
    d.appendChild(el("div", { style: "display:flex;gap:8px" }, [go, el("button", { class: "btn btn--ghost", type: "button", onclick: cancelImport }, "Cancel")]));
    paint();
  }

  function cancelImport() { state.draft = null; state.view = "tool"; renderMain(); }

  async function confirmImport() {
    const dr = state.draft;
    if (!dr || dr.busy) return;
    const ok = await UI().confirm({
      title: "Import " + dr.name + ".py?", level: "warn", focusCancel: true, confirmLabel: "Import",
      body: "Jarvis will run this file to check it, and again at every start. It runs as you, with your access. Only continue if you trust it.",
      pre: dr.source.split("\n").slice(0, 40).join("\n") + (lineCount(dr.source) > 40 ? "\n…" : ""),
    });
    if (!ok) return;                                      // declining writes nothing and sends nothing
    dr.busy = true; dr.result = null; renderImportReview();
    try {
      const fresh = await ctools("");                     // never overwrite: re-read the folder first
      state.files = fresh.tools || state.files;
      if (nameProblem(dr.name, userFileNames(), "new")) { dr.busy = false; renderImportReview(); return; }
      const check = await ctools("/" + encodeURIComponent(dr.name) + "/check", { method: "POST", body: JSON.stringify({ source: dr.source }) });
      if (!check.ok) { dr.result = check; dr.busy = false; renderImportReview(); return; }
      const res = await ctools("/" + encodeURIComponent(dr.name), { method: "PUT", body: JSON.stringify({ source: dr.source }) });
      if (!res.ok) { dr.result = res; dr.busy = false; renderImportReview(); return; }
      const first = (res.tools || [])[0];
      state.draft = null; state.view = "tool";
      await loadAll({ quiet: true });
      const row = first && rowByName(first);
      state.selected = row ? row.id : null;
      renderMain();
      toast("Imported " + dr.name + " — " + (res.tools || []).join(", ") + ".", "success");
    } catch (e) {
      dr.busy = false; dr.result = Object.assign({ ok: false }, e.data && e.data.error ? e.data : { error: e.message });
      renderImportReview();
    }
  }

  /* =======================================================================
   * Load / open / close / keyboard
   * ===================================================================== */

  async function loadAll(opts) {
    opts = opts || {};
    if (!dom) return;
    state.loading = true;
    if (!opts.quiet) { setStatusLine("reading the catalogue…"); dom.refresh.disabled = true; }
    const [tools, files, templates, commands, disabled] = await Promise.allSettled([
      api("/api/tools"), ctools(""), state.templates.length ? Promise.resolve(null) : ctools("/templates"),
      api("/api/commands"), api("/api/disabled"),
    ]);
    state.loading = false;
    dom.refresh.disabled = false;
    state.toolsError = tools.status === "rejected" ? String(tools.reason && tools.reason.message || tools.reason) : "";
    state.filesError = files.status === "rejected" ? String(files.reason && files.reason.message || files.reason) : "";
    if (tools.status === "fulfilled") state.tools = Array.isArray(tools.value) ? tools.value : [];
    if (files.status === "fulfilled") state.files = (files.value && files.value.tools) || [];
    if (templates.status === "fulfilled" && templates.value) state.templates = templates.value.templates || [];
    state.commandsError = commands.status === "rejected" ? String(commands.reason && commands.reason.message || commands.reason) : "";
    if (commands.status === "fulfilled") state.commands = commands.value && typeof commands.value === "object" ? commands.value : {};
    // If /api/disabled is unreachable we can't tell which commands are off — say so
    // rather than show them all as available.
    if (disabled.status === "fulfilled") state.disabledCommands = (disabled.value && disabled.value.commands) || [];
    else if (!state.commandsError) state.commandsError = "couldn’t read which commands are switched off";
    state.rows = buildRows(state.toolsError ? [] : state.tools, state.files, state.commandsError ? null : state.commands, state.disabledCommands);
    if (state.selected && !rowById(state.selected)) state.selected = null;
    if (!state.selected && !opts.keepView) {
      // Land on a normal tool, not on a rejected file: people open this to set
      // safeguards. Rejections are still first in the list and in the status line.
      const groups = groupRows(state.rows.filter((r) => r.state === "loaded" && r.kind === "tool"));
      const any = groupRows(state.rows);
      const first = groups.length ? groups[0].rows[0] : (any.length ? any[0].rows[0] : null);
      state.selected = first ? first.id : null;
    }
    const cov = coverage(state.rows);
    const failed = state.rows.filter((r) => r.state === "failed").length;
    if (state.toolsError) setStatusLine("catalogue unavailable — " + state.toolsError.slice(0, 80), "error");
    else if (!opts.quiet || dom.statusLine.textContent.startsWith("reading")) setStatusLine(cov.total + " tools available" + (offSummary(cov) ? " · " + offSummary(cov) + " off" : "") + (cov.commands ? " · " + cov.commands + (cov.commands === 1 ? " saved command" : " saved commands") : "") + (state.commandsError ? " · saved commands unavailable" : "") + (failed ? " · " + failed + " file" + (failed > 1 ? "s" : "") + " rejected" : "") + (state.filesError ? " · your tool files couldn’t be read" : ""), state.filesError || state.commandsError ? "error" : "");
    renderChips(); renderGroupSelect();
    if (state.view === "editor") { renderList(); renderSide(); return; }
    renderMain();
  }

  function modalOpen() { return !!document.querySelector("#jui-layer .jui-modal"); }

  function onKey(e) {
    if (!dom || dom.overlay.hidden || modalOpen() || e.defaultPrevented) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Tab") { trapTab(e); return; }
    if (e.key === "Escape") {
      if (t === dom.search && dom.search.value) { state.search = ""; dom.search.value = ""; renderList(); return; }
      close(); return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const rows = visibleRows().filter((r) => !state.collapsed.has(r.group) || state.search);
      const ordered = groupRows(rows).flatMap((g) => g.rows);
      if (!ordered.length) return;
      e.preventDefault();
      const i = ordered.findIndex((r) => r.id === state.selected);
      const next = ordered[Math.max(0, Math.min(ordered.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next) select(next.id);
      return;
    }
    const def = SAFEGUARDS.find((s) => s.hotkey === e.key);
    const row = selectedRow();
    if (def && state.view === "tool" && row && row.flags) { e.preventDefault(); setFlag(row.name, def.key, !row.flags[def.key]); }
    else if (e.key === "4" && state.view === "tool" && row && (row.state === "loaded" || row.state === "off")) {
      e.preventDefault(); setAvailable(row, row.state === "off");
    }
  }

  function trapTab(e) {
    const items = Array.from(dom.panel.querySelectorAll('button:not([disabled]), input:not([disabled]):not(.tm-file-input), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'))
      .filter((n) => n.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function wireDrop() {
    let depth = 0;
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    dom.panel.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth += 1; dom.panel.classList.add("is-dragging"); });
    dom.panel.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
    dom.panel.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) dom.panel.classList.remove("is-dragging"); });
    dom.panel.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault(); depth = 0; dom.panel.classList.remove("is-dragging");
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length > 1) toast("One file at a time — importing " + files[0].name + ".", "info");
      if (files[0]) startImport(files[0]);
    });
  }

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;
    loadPrefs();
    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderList(); });
    dom.groupSel.addEventListener("change", () => { state.group = dom.groupSel.value; renderList(); });
    dom.safeSel.addEventListener("change", () => { state.safeguard = dom.safeSel.value; renderList(); });
    dom.refresh.addEventListener("click", () => loadAll());
    dom.btnImport.addEventListener("click", () => dom.file.click());
    dom.btnCreate.addEventListener("click", () => enterEditor({ mode: "new" }));
    dom.file.addEventListener("change", () => { const f = dom.file.files && dom.file.files[0]; dom.file.value = ""; if (f) startImport(f); });
    $("#tm-close").addEventListener("click", close);
    dom.overlay.addEventListener("click", (e) => { if (e.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);
    wireDrop();
    state.built = true;
    return true;
  }

  async function open(opts) {
    if (!ensureBuilt()) { toast("Tool Manager markup is missing from index.html.", "error"); return; }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    restoreDetailBox();
    if (state.view !== "editor") { state.view = "tool"; }
    renderChips(); renderGroupSelect();
    if (state.rows.length) renderMain(); else renderDetail();
    dom.search.focus({ preventScroll: true });
    await loadAll();
    if (opts && opts.create) enterEditor({ mode: "new" });
    else if (opts && opts.tool) { const r = rowByName(opts.tool); if (r) select(r.id); }
  }

  async function close() {
    if (!dom || dom.overlay.hidden) return;
    if (state.view === "editor" && state.editor && state.editor.dirty) {
      const ok = await UI().confirm({ title: "Close with unsaved changes?", level: "warn", focusCancel: true, confirmLabel: "Discard and close",
        body: "You’ve edited " + (state.editor.name || "a new tool") + " without saving." });
      if (!ok) return;
    }
    if (state.view === "editor") { state.editor = null; state.view = "tool"; restoreDetailBox(); }
    state.draft = null;
    dom.overlay.hidden = true;
    const prev = state.prevFocus; state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  global.JarvisToolManager = { open, close, refresh: () => loadAll(), _pure: PURE };
})(window);
