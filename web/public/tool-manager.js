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
 * DAEMONS, PERSONAS AND SCREENS
 * ------------------------------
 * The list also carries three more kinds of thing, each with the same switch:
 *   Daemon    a background service (Daemons panel). OFF edits the daemon's own `enabled`
 *             flag: it can't be started by hand, by a schedule, by autostart or by Jarvis.
 *             One that is already running keeps running; stop it in the Daemons panel.
 *   Persona   one a tool file registered (PERSONAS). OFF hides it from the Skin picker.
 *   Screen    a button or Menu entry a tool file ships (TOOL_UI). OFF removes it and
 *             refuses to serve its files; the tool itself keeps working.
 * Personas and screens are kept in ~/.jarvis/disabled.json beside tools and commands.
 *
 * UNFINISHED TOOLS
 * ----------------
 * Every editor tab with changes is backed up to ~/.jarvis/tools/_drafts a couple of
 * seconds after you stop typing, saved or not, and a Save Jarvis refuses is backed up
 * too. The Overview lists them under "Unfinished tools" so a closed tab, a cleared
 * browser or a Discard never loses the text. Restore opens a new tab; the backup is
 * removed when that text saves successfully.
 *
 * EDITOR TABS, ASK JARVIS, CLOSABLE PANES (L.43 / L.44)
 * -----------------------------------------------------
 * Every tool you open or create is a TAB over the middle pane, like an IDE.
 * Closing the Tool Manager does not close them: the overlay only hides, and the
 * tabs (with unsaved text) are also kept in this browser so a reload brings them
 * back. "Create a tool" opens a NEW tab each time. Under the code of every tab
 * is an "Ask Jarvis" dock: describe the tool, and Jarvis writes the file into
 * that tab's editor while you watch it stream in (/api/ctools/:name/agent).
 * Jarvis only fills the unsaved buffer. It never saves, never runs anything,
 * and "Undo AI edit" puts the previous text back; Save is still yours. The Tools
 * list and the Overview panel each have an x (and a toggle in the header) so the
 * editor can have the whole width.
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
 *   /api/ctools/drafts[/:key]      the unfinished-tool backups
 *   GET  /api/daemons, PATCH /api/daemons/:id   daemons and their `enabled` flag
 *   GET  /api/personas?all=1, GET /api/tool-ui?all=1   registered personas / shipped screens (incl. switched-off)
 *   POST /api/tools/disabled {kind}  switch a persona or screen off/on
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

  const SOURCE_BUCKETS = { builtin: "shipped", auto: "shipped", mcp: "mcp", user: "user", command: "command", daemon: "daemon", persona: "persona", skin: "skin", ui: "ui" };
  const SOURCE_LABELS = { shipped: "Shipped", user: "Yours", mcp: "MCP", command: "Command", daemon: "Daemon", persona: "Persona", skin: "Skin", ui: "Screen" };
  // Kinds that are not tools: they get the switch and a short detail, nothing else.
  const EXTRA_KINDS = ["daemon", "persona", "skin", "ui"];
  const KIND_NOUN = { tool: "tool", command: "saved command", daemon: "daemon", persona: "persona", skin: "skin", ui: "screen" };
  // Buckets drawn with the "info" colour (everything that is not a plain tool).
  const INFO_BUCKETS = ["mcp", "command", "daemon", "persona", "skin", "ui"];
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
    if (id === "daemons") return "Daemons";
    if (id === "personas") return "Personas";
    if (id === "skins") return "Skins";
    if (id === "ui_elements") return "Screens";
    return String(id || "other").replace(/_/g, " ");
  }

  // WHERE a row comes from, in one line, for the Source column and the search box.
  // Tools carry it from the server (`origin`, tools.py _tool_origin); the rest are
  // worked out here from what the row is. Display only.
  function originOf(row) {
    if (!row) return "";
    if (row.origin) return row.origin;
    const x = row.extra || {};
    switch (row.kind) {
      case "command": return "Saved command (~/.jarvis/commands.json)";
      case "daemon": return x.builtin ? "Built-in daemon (ships with Jarvis)" : "Your daemon (~/.jarvis/daemons.json)";
      case "persona": return x.builtin ? "Ships with Jarvis (web/public/app.js)" : (x.file ? "Tool file: " + x.file : "A tool file");
      case "skin": return x.skinKind === "preset" ? "Ships with Jarvis: accent swatch (web/public/app.js)" : "Ships with Jarvis: built-in theme (web/public/ui-kit.js)";
      case "ui": return x.file ? "Tool file: " + x.file : "A tool file";
      case "broken": return row.file ? "Your file: ~/.jarvis/tools/" + row.file : "Your file";
      default:
        if (row.source === "user") return row.file ? "Your file: ~/.jarvis/tools/" + row.file : "Your file in ~/.jarvis/tools/";
        if (row.source === "mcp") return "MCP server";
        return row.file ? "Ships with Jarvis: actions/" + row.file : "Built in to Jarvis";
    }
  }

  // Rows the list shows. A tool is "loaded" when the CLI offers it and you
  // haven't switched it off ("off" when you have); a user tool whose FILE is
  // switched off is "fileoff"; an unreadable file is "failed". Saved commands
  // (kind "command") are "loaded" or "off". `disabledCommands` is the list from
  // /api/disabled.
  function buildRows(tools, files, commands, disabledCommands, extras) {
    const rows = [];
    const byName = new Map();
    (Array.isArray(tools) ? tools : []).forEach((t) => {
      if (!t || !t.name || byName.has(t.name)) return;
      const row = {
        id: "t:" + t.name, kind: "tool", state: t.disabled ? "off" : "loaded", name: t.name,
        protectedReason: t.protected || "",
        description: t.description || "", source: t.source || "builtin",
        group: t.group || "other", file: t.file || "", userFile: null, error: "", origin: t.origin || "",
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
    // Daemons, personas and screens (extras = {daemons, personas, ui}, each an array or
    // missing). A daemon is "off" when its own `enabled` flag is false; a persona or screen
    // when the owner switched it off (the server flags it `disabled`).
    const ex = extras && typeof extras === "object" ? extras : {};
    (Array.isArray(ex.daemons) ? ex.daemons : []).forEach((d) => {
      if (!d || typeof d.id !== "string" || !d.id) return;
      rows.push({
        id: "d:" + d.id, kind: "daemon", state: d.enabled === false ? "off" : "loaded", name: d.id, protectedReason: "",
        description: d.description || "", source: "daemon", group: "daemons", file: "", userFile: null, error: "",
        flags: null, defaults: null, parameters: {}, extra: d,
      });
    });
    (Array.isArray(ex.personas) ? ex.personas : []).forEach((p) => {
      if (!p || typeof p.id !== "string" || !p.id) return;
      rows.push({
        id: "p:" + p.id, kind: "persona", state: p.disabled ? "off" : "loaded", name: p.id, protectedReason: "",
        description: p.name ? (p.name + (p.assistant_name && p.assistant_name !== p.name ? " — " + p.assistant_name : "")) : "",
        source: "persona", group: "personas", file: p.file || "", userFile: null, error: "",
        flags: null, defaults: null, parameters: {}, extra: p,
      });
    });
    // The five personas that ship inside the web app (the browser supplies the list) and the
    // Skin modal's built-in accent swatches and themes. They share the same off lists as
    // everything else; `disabledPersonas` / `disabledSkins` come from /api/disabled.
    const offPersonas = new Set(Array.isArray(ex.disabledPersonas) ? ex.disabledPersonas : []);
    const offSkins = new Set(Array.isArray(ex.disabledSkins) ? ex.disabledSkins : []);
    (Array.isArray(ex.builtinPersonas) ? ex.builtinPersonas : []).forEach((p) => {
      if (!p || typeof p.id !== "string" || !p.id) return;
      rows.push({
        id: "p:" + p.id, kind: "persona", state: offPersonas.has(p.id) ? "off" : "loaded", name: p.id, protectedReason: "",
        description: p.name || "", source: "persona", group: "personas", file: "", userFile: null, error: "",
        flags: null, defaults: null, parameters: {}, extra: { id: p.id, name: p.name, hex: p.hex, builtin: true, disabled: offPersonas.has(p.id) },
      });
    });
    (Array.isArray(ex.skins) ? ex.skins : []).forEach((k) => {
      if (!k || typeof k.id !== "string" || !k.id) return;
      const skinId = (k.skinKind === "preset" ? "preset:" : "theme:") + k.id;
      rows.push({
        id: "s:" + skinId, kind: "skin", state: offSkins.has(skinId) ? "off" : "loaded", name: skinId, protectedReason: "",
        description: (k.skinKind === "preset" ? "Accent swatch — " : "Theme — ") + (k.name || k.id), source: "skin", group: "skins", file: "", userFile: null, error: "",
        flags: null, defaults: null, parameters: {}, extra: { id: skinId, name: k.name || k.id, hex: k.hex, skinKind: k.skinKind, builtin: true, disabled: offSkins.has(skinId) },
      });
    });
    (Array.isArray(ex.ui) ? ex.ui : []).forEach((u) => {
      if (!u || typeof u.id !== "string" || !u.id) return;
      rows.push({
        id: "u:" + u.id, kind: "ui", state: u.disabled ? "off" : "loaded", name: u.id, protectedReason: "",
        description: u.hint || u.title || u.label || "", source: "ui", group: "ui_elements", file: u.file || "", userFile: null, error: "",
        flags: null, defaults: null, parameters: {}, extra: u,
      });
    });
    return rows;
  }

  function matchesSearch(row, query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return true;
    return [row.name, row.description, row.group, row.file, row.error, originOf(row)].some((s) => String(s || "").toLowerCase().includes(q));
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
    out.extraOff = { daemon: 0, persona: 0, skin: 0, ui: 0 };
    rows.forEach((r) => { if (EXTRA_KINDS.includes(r.kind) && isOff(r)) out.extraOff[r.kind] += 1; });
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
    if (row.kind === "daemon") {
      return "Switch off and this daemon can't be started — by you, by a schedule, by autostart or by Jarvis. One that is already running keeps running until you stop it.";
    }
    if (row.kind === "persona") {
      return "Switch off and this persona no longer appears in the Skin picker. Nothing else about Jarvis changes.";
    }
    if (row.kind === "skin") {
      return "Switch off and this " + (row.extra && row.extra.skinKind === "preset" ? "colour swatch" : "theme") + " no longer appears in the Skin modal. If it is the one in use it stays applied until you pick another.";
    }
    if (row.kind === "ui") {
      return "Switch off and this button or Menu entry disappears, and its page is no longer served. The tool it belongs to keeps working.";
    }
    if (row.kind === "command") {
      return "Switch off and Jarvis can't see or run this saved command; a scheduled job that uses it fails with a clear message. You can still run it yourself.";
    }
    return "Switch off and Jarvis can't see this tool or use it — not in search, not by name, even if it guesses. You can still run it yourself from Debug.";
  }

  function offOutcome(row) {
    if (row && row.kind === "daemon") {
      return "It can’t be started. If it is running right now it keeps running — stop it in the Daemons panel.";
    }
    if (row && row.kind === "persona") return "It is hidden from the Skin picker.";
    if (row && row.kind === "skin") return "It is hidden from the Skin modal. If it is applied right now it stays applied.";
    if (row && row.kind === "ui") return "Its button or Menu entry is gone and its page isn’t served.";
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
    const eo = cov.extraOff || {};
    if (eo.daemon) parts.push(eo.daemon + (eo.daemon === 1 ? " daemon" : " daemons"));
    if (eo.persona) parts.push(eo.persona + (eo.persona === 1 ? " persona" : " personas"));
    if (eo.skin) parts.push(eo.skin + (eo.skin === 1 ? " skin" : " skins"));
    if (eo.ui) parts.push(eo.ui + (eo.ui === 1 ? " screen" : " screens"));
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

  // L.43: the smallest "untitled N" not already used by an open tab.
  function nextFree(used) {
    const taken = new Set(used || []);
    let n = 1;
    while (taken.has(n)) n += 1;
    return n;
  }

  // L.43: split a (possibly still streaming) Ask Jarvis reply into the short
  // note and the file. Mirrors custom_tools_agent.split_reply on the Python
  // side; the DONE line from the server is authoritative, this only lets the
  // editor type along while tokens arrive. A trailing partial fence ("\n``")
  // is held back so it never flashes into the code.
  const AGENT_FENCE_OPEN = /```[ \t]*(?:python|py)?[ \t]*\n/i;
  function splitAgentReply(text) {
    const t = String(text == null ? "" : text).replace(/\r\n/g, "\n");
    const m = AGENT_FENCE_OPEN.exec(t);
    if (!m) return { note: t.trim(), code: "", complete: false };
    let note = t.slice(0, m.index).trim();
    const rest = t.slice(m.index + m[0].length);
    let body, complete = false, after = "";
    const close = rest.indexOf("\n```");
    if (close >= 0) { body = rest.slice(0, close); after = rest.slice(close + 4).trim(); complete = true; }
    else {
      const trimmed = rest.replace(/\s+$/, "");
      if (trimmed.endsWith("```")) { body = trimmed.slice(0, -3); complete = true; }
      else body = rest.replace(/\n`{1,2}$/, "");
    }
    let code = body.replace(/^\n+|\n+$/g, "");
    if (code) code += "\n";
    if (after && complete) note = note ? note + "\n\n" + after : after;
    return { note, code, complete };
  }

  // L.45: what the editor SHOWS while Jarvis rewrites a file. The model sends
  // the whole new file, so showing only what has arrived makes a 300-line tool
  // collapse to five lines and grow back — it reads as a reload. Instead the
  // lines that have arrived replace the same lines of the old file and the old
  // lines not yet reached stay underneath (dimmed by the editor), like
  // overwriting in place. `head` is the 0-based line being written.
  function overwriteView(code, old) {
    const c = String(code == null ? "" : code), o = String(old == null ? "" : old);
    const done = c.replace(/\n$/, "").split("\n").length;           // lines the new text covers so far
    const oldLines = o.split("\n");
    const rest = oldLines.length > done ? oldLines.slice(done).join("\n") : "";
    const text = rest ? c.replace(/\n?$/, "\n") + rest : c;
    return { text, head: c ? done - 1 : -1, tail: !!rest };
  }

  // Unfinished-tool backups (the Overview's list). `d` is one entry of /api/ctools/drafts.
  function draftTitle(d) {
    const n = (d && (d.orig_name || d.name)) || "";
    return n ? n : "untitled tool";
  }

  function draftAge(saved, now) {
    const t = Date.parse(String(saved || ""));
    if (!isFinite(t)) return "";
    const secs = Math.max(0, Math.round(((now == null ? Date.now() : now) - t) / 1000));
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + " min ago";
    if (secs < 86400) return Math.floor(secs / 3600) + " h ago";
    return Math.floor(secs / 86400) + " d ago";
  }

  // A key for a new editor tab's backup: lowercase, matches the server's DRAFT_KEY_RE.
  const DRAFT_KEY_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
  function newDraftKey(rand, now) {
    const r = (rand == null ? Math.random() : rand).toString(36).slice(2, 8) || "x";
    return "d" + (now == null ? Date.now() : now).toString(36) + r;
  }

  // The lines under a passing check about TOOL_UI. For a NEW tool made from a template
  // that ships a folder, the folder doesn't exist until the first save, so the server's
  // "nothing to show" about it is expected: say what will happen instead of alarming.
  function uiLines(result, templateFolder, isNew) {
    const lines = [];
    (result && result.ui_problems || []).forEach((p) => {
      if (isNew && templateFolder && /no tool\.html|nothing to show/.test(p) && p.indexOf('"' + templateFolder + '"') >= 0) return;
      if (isNew && templateFolder && /no tool\.html|nothing to show/.test(p) && p.indexOf(templateFolder) >= 0) return;
      lines.push("Ignored (the tool still loads): " + p);
    });
    if (isNew && templateFolder) lines.push("Saving also creates ~/.jarvis/tools/" + templateFolder + "/ with a starter tool.html, tool.js and tool.css (never over a folder that already exists).");
    return lines;
  }

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
    SAFEGUARDS, KEYS, NAME_RE, STAGES, MAX_SOURCE_CHARS, bucketOf, groupLabel, originOf, buildRows, matchesSearch, matchesSafeguard,
    matchesSource, filterRows, groupRows, coverage, callOutcome, jobOutcome, changedKeys, nameProblem, importNameFromFile,
    importProblem, sourceProblem, parseErrorLine, stageStates, lineCount, formatBytes, parseArgs, paramList,
    nextFree, splitAgentReply, overwriteView, EXTRA_KINDS, KIND_NOUN, draftTitle, draftAge, newDraftKey, DRAFT_KEY_RE, uiLines,
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
    disabledPersonas: [], disabledSkins: [],                      // /api/disabled: persona ids and skin ids switched off
    daemons: [], personas: [], uiElements: [], extrasError: "",   // the non-tool rows: daemons, tool-registered personas, shipped screens
    drafts: [], draftsError: "",                                  // backups of tools still being written (see "UNFINISHED TOOLS")
    selected: null, view: "tool",
    search: "", source: "all", group: "all", safeguard: "any",
    collapsed: new Set(), busy: new Set(),
    editor: null, draft: null, run: null, prevFocus: null,
    suggest: true,                      // Jarvis inline suggestions in the editor (L.33); on by default, one click to turn off
    tabs: [],                           // L.43: every open editor, in tab order; state.editor is the active one while view === "editor"
    tabSeq: 0, restored: false, persistOk: true,
    hideTools: false, hideSide: false,  // L.44: the owner closed that pane
    agentOpen: true,                    // L.43: the Ask Jarvis dock is expanded
  };
  let dom = null;

  function loadPrefs() {
    try {
      const p = JSON.parse(global.localStorage.getItem(PREF_KEY) || "{}");
      if (Array.isArray(p.collapsed)) state.collapsed = new Set(p.collapsed);
      if (typeof p.source === "string") state.source = p.source;
      if (typeof p.suggest === "boolean") state.suggest = p.suggest;
      if (typeof p.hideTools === "boolean") state.hideTools = p.hideTools;
      if (typeof p.hideSide === "boolean") state.hideSide = p.hideSide;
      if (typeof p.agentOpen === "boolean") state.agentOpen = p.agentOpen;
    } catch (_) { /* private mode / bad JSON: defaults */ }
  }
  function savePrefs() {
    try { global.localStorage.setItem(PREF_KEY, JSON.stringify({ collapsed: Array.from(state.collapsed), source: state.source, suggest: state.suggest,
      hideTools: state.hideTools, hideSide: state.hideSide, agentOpen: state.agentOpen })); }
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
      tabs: $("#tm-tabs"), toggleTools: $("#btn-tm-toggle-tools"), toggleSide: $("#btn-tm-toggle-side"),
      closeTools: $("#tm-close-tools"), closeSide: $("#tm-close-side"),
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
    const counts = { all: state.rows.length, shipped: 0, user: 0, mcp: 0, command: 0, daemon: 0, persona: 0, skin: 0, ui: 0, off: 0, attention: 0 };
    state.rows.forEach((r) => {
      counts[bucketOf(r.source)] += 1;
      if (isOff(r)) counts.off += 1;
      if (needsAttention(r)) counts.attention += 1;
    });
    const defs = [["all", "All"], ["shipped", "Shipped"], ["user", "Yours"], ["mcp", "MCP"], ["command", "Commands"], ["daemon", "Daemons"], ["persona", "Personas"], ["skin", "Skins"], ["ui", "Screens"], ["off", "Off"], ["attention", "Attention"]];
    clear(dom.chips);
    defs.forEach(([id, label]) => {
      if (["mcp", "command", "daemon", "persona", "skin", "ui", "off", "attention"].includes(id) && !counts[id] && state.source !== id) return;
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
    meta.appendChild(tag(SOURCE_LABELS[bucketOf(row.source)], bucketOf(row.source) === "user" ? "warn" : INFO_BUCKETS.includes(bucketOf(row.source)) ? "info" : ""));
    if (row.state === "off") meta.appendChild(tag("off", "bad", "Switched off — Jarvis can't see or use it"));
    if (row.state === "fileoff") meta.appendChild(tag("file off", ""));
    if (row.protectedReason) meta.appendChild(tag("always on", "info", row.protectedReason));
    if (row.state === "pending") meta.appendChild(tag("not loaded yet", "warn"));
    if (row.state === "failed") meta.appendChild(tag("rejected", "bad"));
    if (row.flags) { meta.appendChild(pips(row.flags)); if (changedKeys(row).length) meta.appendChild(tag("edited", "changed", "Differs from the built-in default")); }
    const card = el("button", {
      class: "tm-card" + (active ? " is-active" : ""), type: "button", "data-tm-state": row.state, "data-id": row.id,
      "aria-current": active ? "true" : null, onclick: () => select(row.id),
    }, [el("div", { class: "tm-card__row" }, [el("span", { class: "tm-card__name" }, row.name)]),
      el("div", { class: "tm-card__origin", title: "Where this comes from" }, originOf(row)), meta]);
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
    // L.43: picking a tool in the list never discards an open editor - it just
    // shows that tool's details; the editor stays in its tab.
    state.draft = null; state.run = null; state.selected = id;
    if (state.view === "editor") showDetails(); else { state.view = "tool"; renderMain(); }
    const card = dom.list.querySelector('.tm-card[data-id="' + (global.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
    if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
  }

  function renderMain() {
    renderList();
    renderDetail();
    renderSide();
    renderTabs();
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
    const busy = state.busy.has(row.id + ":enabled");
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
    setText(dom.midTitle, row.kind === "broken" ? "Rejected file" : row.kind === "command" ? "Saved command"
      : row.kind === "daemon" ? "Daemon" : row.kind === "persona" ? "Persona" : row.kind === "ui" ? "Screen" : "Tool");
    if (row.kind === "command") { renderCommandDetail(row); return; }
    if (EXTRA_KINDS.includes(row.kind)) { renderExtraDetail(row); return; }

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
    head.appendChild(el("div", { class: "tm-origin" }, [el("span", { class: "tm-origin__k" }, "Source"), originOf(row)]));
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

    const screens = screensSection(row);
    if (screens) d.appendChild(screens);
    if (row.userFile) d.appendChild(fileSection(row));
    else if (row.source === "mcp") {
      d.appendChild(el("div", { class: "tm-note tm-note--info" }, [el("b", null, "MCP tool. "), "Its server is managed in Menu → MCP Servers. Switching this one tool off leaves the rest of the server alone."]));
    }
  }

  // A daemon, a tool-registered persona or a shipped screen: the on/off switch plus what
  // it is and where it came from. Everything else about them is edited where it always
  // was (Daemons panel, the tool file) — this panel only decides whether they are on.
  function infoRows(pairs) {
    return el("div", { class: "tm-params" }, pairs.filter((p) => p[1] !== "" && p[1] != null).map((p) => el("div", { class: "tm-param" }, [
      el("div", { class: "tm-param__name" }, p[0]), el("div", { class: "tm-param__desc" }, String(p[1])),
    ])));
  }

  function renderExtraDetail(row) {
    const d = dom.detail, x = row.extra || {};
    const label = SOURCE_LABELS[bucketOf(row.source)];
    const badges = [tag(label, "info"), row.state === "off" ? tag("switched off", "bad") : tag("on", "info")];
    if (row.kind === "daemon") {
      if (x.builtin) badges.push(tag("built in"));
      if (x.status) badges.push(tag(String(x.status), x.running ? "info" : ""));
    }
    if (row.kind === "ui") badges.push(tag(x.mode === "menu" ? "Menu entry" : "button"));
    d.appendChild(el("div", null, [
      el("div", { class: "tm-title" }, row.kind === "daemon" ? (x.name || row.name) : (row.kind === "persona" || row.kind === "skin") ? (x.name || row.name) : (x.label || row.name)),
      el("div", { class: "tm-origin" }, [el("span", { class: "tm-origin__k" }, "Source"), originOf(row)]),
      el("div", { class: "tm-does" + (row.description ? "" : " is-empty") }, row.description || "No description."),
      el("div", { class: "tm-badges" }, badges),
    ]));
    d.appendChild(el("div", null, [sectionTitle("Availability"), el("div", { class: "tm-sw-list" }, [availabilityRow(row)])]));
    if (row.kind === "daemon" && row.state === "off" && x.running) {
      d.appendChild(el("div", { class: "tm-note" }, [el("b", null, "It is still running. "), "Switching it off only stops it being started again. Stop it from the Daemons panel."]));
    }
    d.appendChild(el("div", null, [
      sectionTitle("What this means"),
      row.state === "off"
        ? el("div", { class: "tm-outcome", "data-o": "off" }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, "⏻"), el("div", null, [el("b", null, "Switched off. "), offOutcome(row)])])
        : el("div", { class: "tm-outcome", "data-o": "free" }, [el("span", { class: "tm-outcome__glyph", "aria-hidden": "true" }, "▷"), el("div", null, [el("b", null, "On. "), availabilityHint(row).replace(/^Switch off and /, "If you switch it off, ")])]),
    ]));
    if (row.kind === "daemon") {
      d.appendChild(el("div", null, [sectionTitle("Details"), infoRows([
        ["Id", row.name], ["Status", x.status], ["Autostart", x.autostart ? "yes" : "no"],
        ["Categories", (x.categories || []).join(", ")], ["Next start", x.next_start || ""], ["Last error", x.last_error || ""],
      ])]));
      d.appendChild(el("div", { class: "tm-actions" }, [el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => { close(); if (global.JarvisDaemons) global.JarvisDaemons.open(); } }, "Open the Daemons panel")]));
    } else if (row.kind === "persona") {
      d.appendChild(el("div", null, [sectionTitle("Details"), infoRows([
        ["Id", row.name], ["Assistant name", x.assistant_name], ["Addresses you as", x.address_user_as], ["Colour", x.hex],
        ["Registered by", x.builtin ? "Ships with the web app (web/public/app.js)" : (x.file ? "~/.jarvis/tools/ or actions/ — " + x.file : "")],
      ])]));
    } else if (row.kind === "skin") {
      d.appendChild(el("div", null, [sectionTitle("Details"), infoRows([
        ["Id", row.name], ["Kind", x.skinKind === "preset" ? "Accent swatch (Skin modal → Accent)" : "Theme (Skin modal → Themes)"], ["Colour", x.hex],
        ["Defined in", x.skinKind === "preset" ? "web/public/app.js (SKIN_PRESETS)" : "web/public/ui-kit.js (BUILTIN_THEMES)"],
      ])]));
      d.appendChild(el("div", { class: "tm-actions" }, [el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => { close(); if (global.JarvisHost && global.JarvisHost.openPanel) global.JarvisHost.openPanel("skin"); } }, "Open the Skin modal")]));
    } else {
      const files = x.files || {};
      d.appendChild(el("div", null, [sectionTitle("Details"), infoRows([
        ["Id", row.name], ["Declared in", x.file], ["Opens as", x.mode === "menu" ? "an entry in the Menu, in a panel" : "a button beside the Menu button, in a sandboxed window"],
        ["Folder", x.path], ["Files", ["html", "js", "css"].map((k) => files[k]).filter(Boolean).join(", ")], ["Its tool", x.tool || ""],
        ["Problems", (x.missing || []).length ? "declared but missing: " + x.missing.join(", ") : ""],
      ])]));
      if (row.state !== "off") {
        d.appendChild(el("div", { class: "tm-actions" }, [el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => { close(); if (global.JarvisToolUI) global.JarvisToolUI.open(row.name); } }, "Open it now")]));
      }
    }
  }

  // The screens a tool file ships, listed on the detail of any tool from that file.
  function screensSection(row) {
    const file = row.file || (row.userFile && row.userFile.file) || "";
    if (!file) return null;
    const mine = state.rows.filter((r) => r.kind === "ui" && r.file === file);
    if (!mine.length) return null;
    return el("div", null, [sectionTitle("Screens from this file", String(mine.length)),
      el("div", { class: "tm-attn" }, mine.map((r) => el("button", { class: "tm-attn__item", type: "button", "data-tm-state": r.state, onclick: () => select(r.id) }, [
        el("div", { class: "tm-attn__name" }, r.name),
        el("div", { class: "tm-attn__why" }, ((r.extra && r.extra.mode === "menu") ? "Menu entry" : "Button") + (r.state === "off" ? " · switched off" : "")),
      ])))]);
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
          el("div", { class: "tm-attn__why" }, (r.kind === "command" ? "Saved command" : EXTRA_KINDS.includes(r.kind) ? SOURCE_LABELS[bucketOf(r.source)] : "Tool") + (r.state === "fileoff" ? " · its file is switched off" : " · switched off by you")),
        ]));
      });
      side.appendChild(el("div", null, [sectionTitle("Switched off", String(offRows.length)), offList,
        offRows.length > 10 ? el("div", { class: "tm-hint", style: "margin-top:6px" }, "+" + (offRows.length - 10) + " more — use the Off filter.") : null]));
    }

    if (state.drafts.length || state.draftsError) side.appendChild(draftsSection());

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
    const token = row.id + ":enabled";
    if (state.busy.has(token) || row.protectedReason) return;
    if (row.state !== "loaded" && row.state !== "off") return;
    const kind = row.kind === "command" ? "command" : EXTRA_KINDS.includes(row.kind) ? row.kind : "tool";
    const what = KIND_NOUN[kind] || "tool";
    if (!available && (kind === "tool" || kind === "command")) {
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
      if (kind === "daemon") {
        // A daemon's own `enabled` flag (the Daemons panel edits the same one), not a second copy.
        await api("/api/daemons/" + encodeURIComponent(row.name), { method: "PATCH", body: JSON.stringify({ enabled: available }) });
      } else if (kind === "persona" || kind === "skin" || kind === "ui") {
        await api("/api/tools/disabled", { method: "POST", body: JSON.stringify({ name: row.name, value: !available, kind }) });
      } else {
        const path = kind === "command" ? "/api/commands/" + encodeURIComponent(row.name) + "/disabled" : "/api/tools/disabled";
        await api(path, { method: "POST", body: JSON.stringify({ name: row.name, value: !available }) });
      }
      row.state = available ? "loaded" : "off";
      if (row.extra) row.extra = Object.assign({}, row.extra, kind === "daemon" ? { enabled: available } : { disabled: !available });
      // The rest of the app reads these once; tell it.
      if (kind === "ui" && global.JarvisToolUI) global.JarvisToolUI.refresh();
      // Personas (built-in or registered) and skins are drawn by the Skin modal, which reads the off lists once.
      if ((kind === "persona" || kind === "skin") && global.JarvisHost && global.JarvisHost.reloadPersonas) global.JarvisHost.reloadPersonas();
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

  // The personas and skins that ship inside the web app. Only the browser knows them
  // (app.js SKIN_PRESETS / PERSONA_PRESETS, ui-kit.js BUILTIN_THEMES), so the list is
  // asked of it; if it isn't there (a test, a stripped build) these rows are simply absent.
  function skinExtras() {
    const host = global.JarvisHost;
    let cat = null;
    try { cat = host && host.builtinSkinCatalog ? host.builtinSkinCatalog() : null; } catch (_) { cat = null; }
    const out = { disabledPersonas: state.disabledPersonas, disabledSkins: state.disabledSkins };
    if (!cat) return out;
    out.builtinPersonas = cat.personas || [];
    out.skins = (cat.presets || []).map((p) => Object.assign({ skinKind: "preset" }, p))
      .concat((cat.themes || []).map((t) => Object.assign({ skinKind: "theme" }, t)));
    return out;
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
      // The file is gone, so its tab (if open) goes too, no questions asked.
      const openTab = state.tabs.find((t) => t.mode === "edit" && t.origName === fileName);
      if (openTab) await closeTab(openTab, { force: true });
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

  // L.43: a pending import review is the only thing that "leaving" still
  // discards; editors are tabs now and stay open until their x is pressed.
  async function leaveEditor() {
    if (state.view === "import" && state.draft) { state.draft = null; state.view = "tool"; }
    return true;
  }

  function tabLabel(ed) {
    return ed.mode === "edit" ? ed.origName : (ed.name || "untitled " + ed.untitled);
  }

  function newEditorState(opts) {
    const mode = opts.mode || "new";
    state.tabSeq += 1;
    const ed = {
      id: "ed" + state.tabSeq, mode,
      origName: mode === "edit" ? opts.name : "", name: mode === "edit" ? opts.name : (opts.name || ""),
      source: opts.source || "", template: opts.template || "minimal", dirty: !!opts.dirty, saved: mode === "edit",
      result: null, errorLine: null, busy: "", nameTouched: false, untitled: 0,
      pane: null, cm: null, ui: null, sourceBeforeAi: null,
      // The key this tab's backup is stored under (see "UNFINISHED TOOLS"); draftSynced is
      // the text last sent there, so an unchanged tab sends nothing; draftTimer debounces.
      draftKey: DRAFT_KEY_RE.test(opts.draftKey || "") ? opts.draftKey : newDraftKey(), draftSynced: opts.draftSynced || "", draftTimer: 0,
      chat: { messages: [], history: [], busy: false, abort: null, backup: null, undoable: false },
    };
    if (mode === "new") {
      const used = state.tabs.filter((t) => t.mode === "new").map((t) => t.untitled);
      ed.untitled = opts.untitled && !used.includes(opts.untitled) ? opts.untitled : nextFree(used);
    }
    if (opts.initial && opts.initial.valid === false) {
      ed.result = { ok: false, stage: opts.initial.stage || "contract", error: opts.initial.error || "this file won’t load" };
      ed.errorLine = parseErrorLine(ed.result.error);
    }
    return ed;
  }

  /* ---- tab bar ----------------------------------------------------------- */

  function renderTabs() {
    if (!dom || !dom.tabs) return;
    const bar = dom.tabs;
    clear(bar);
    bar.hidden = !state.tabs.length;
    if (!state.tabs.length) return;
    const onDetails = state.view !== "editor";
    const row = selectedRow();
    bar.appendChild(el("button", { class: "tm-tab tm-tab--static" + (onDetails ? " is-active" : ""), type: "button", role: "tab",
      "aria-selected": onDetails ? "true" : "false", title: "The selected tool’s details and safeguards", onclick: () => { if (!onDetails) showDetails(); } },
    [el("span", { class: "tm-tab__label" }, row ? row.name : "Tool details")]));
    state.tabs.forEach((ed) => {
      const active = state.view === "editor" && state.editor === ed;
      const label = tabLabel(ed);
      const close = (e) => { if (e) { e.stopPropagation(); e.preventDefault(); } closeTab(ed); };
      bar.appendChild(el("div", {
        class: "tm-tab" + (active ? " is-active" : "") + (ed.dirty ? " is-dirty" : "") + (ed.chat.busy ? " is-ai" : ""),
        role: "tab", tabindex: "0", "aria-selected": active ? "true" : "false",
        title: label + (ed.dirty ? " — unsaved changes" : "") + (ed.chat.busy ? " — Jarvis is writing" : ""),
        onclick: () => { if (!active) showEditor(ed); },
        onauxclick: (e) => { if (e.button === 1) close(e); },
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showEditor(ed); }
          else if (e.key === "Delete") close(e);
        },
      }, [
        el("span", { class: "tm-tab__dot", "aria-hidden": "true" }),
        el("span", { class: "tm-tab__label" }, label),
        el("button", { class: "tm-tab__x", type: "button", tabindex: "-1", "aria-label": "Close " + label, title: "Close tab", onclick: close }, "×"),
      ]));
    });
    bar.appendChild(el("button", { class: "tm-tab tm-tab--add", type: "button", "aria-label": "New tool tab", title: "New tool in a new tab",
      onclick: () => enterEditor({ mode: "new" }) }, "+"));
  }

  function showDetails() {
    state.editor = null; state.view = "tool"; state.draft = null;
    restoreDetailBox();
    renderMain();
  }

  function showEditor(ed) {
    if (!ed || !state.tabs.includes(ed)) return;
    state.draft = null; state.run = null;
    state.editor = ed; state.view = "editor";
    buildEditor(ed);
    renderTabs(); renderSide(); renderList();
    if (ed.cm && !ed.chat.busy) ed.cm.focus();
  }

  async function closeTab(ed, opts) {
    if (!ed || !state.tabs.includes(ed)) return true;
    if (!(opts && opts.force)) {
      const note = ed.chat.busy ? " Jarvis is still writing into it." : "";
      if (ed.dirty || ed.chat.busy) {
        const ok = await UI().confirm({ title: "Close " + tabLabel(ed) + " without saving?", level: "warn", focusCancel: true, confirmLabel: "Discard",
          body: ed.dirty ? "You’ve edited " + tabLabel(ed) + " without saving." + note : "Jarvis is still writing into it." });
        if (!ok) return false;
      }
    }
    if (ed.chat.abort) { try { ed.chat.abort.abort(); } catch (_) { /* already finished */ } }
    if (state.tabs.includes(ed)) await finishDraftOnClose(ed, !!(opts && opts.force));
    const idx = state.tabs.indexOf(ed);
    if (idx < 0) return true;                       // closed twice while the confirm was open
    const wasActive = state.view === "editor" && state.editor === ed;
    state.tabs.splice(idx, 1);
    if (ed.cm) { try { ed.cm.dispose(); } catch (_) { /* already gone */ } ed.cm = null; }
    if (ed.pane && ed.pane.parentNode) ed.pane.parentNode.removeChild(ed.pane);
    ed.pane = null; ed.ui = null;
    if (state.editor === ed) state.editor = null;
    if (wasActive) {
      const next = state.tabs[Math.min(idx, state.tabs.length - 1)];   // the tab that slid into its place, else the one before
      if (next) showEditor(next); else showDetails();
    } else renderTabs();
    persistSoon();
    return true;
  }

  async function editFile(fileName) {
    const open = state.tabs.find((t) => t.mode === "edit" && t.origName === fileName);
    if (open) { showEditor(open); return; }
    try {
      const data = await ctools("/" + encodeURIComponent(fileName));
      await enterEditor({ mode: "edit", name: fileName, source: data.source || "", enabled: data.enabled !== false, initial: data });
    } catch (e) { toast("Couldn’t open " + fileName + ": " + e.message, "error"); }
  }

  async function enterEditor(opts) {
    opts = opts || {};
    if (state.view === "import" && state.draft) state.draft = null;
    const mode = opts.mode || "new";
    if (mode === "edit") {
      const open = state.tabs.find((t) => t.mode === "edit" && t.origName === opts.name);
      if (open) { showEditor(open); return open; }
    }
    const ed = newEditorState(opts);
    state.tabs.push(ed);
    showEditor(ed);
    if (mode === "new" && !opts.keepSource) await loadTemplate(ed, ed.template, true);
    if (ed.errorLine && ed.cm) { ed.cm.setErrorLine(ed.errorLine); if (state.editor === ed) revealLine(ed.errorLine); }
    if (state.editor === ed && ed.pane) {
      if (mode === "new") { const n = ed.pane.querySelector("#tm-ed-name"); if (n) n.focus(); } else if (ed.cm) ed.cm.focus();
    }
    persistSoon();
    return ed;
  }

  async function loadTemplate(ed, id, silent) {
    if (!ed) return;
    try {
      const data = await ctools("/draft?template=" + encodeURIComponent(id));
      ed.source = (data && data.source) || "";
    } catch (_) { ed.source = ""; if (!silent) toast("Couldn’t load that template — starting empty.", "warn"); }
    ed.template = id; ed.dirty = false;
    if (ed.cm) ed.cm.setValue(ed.source, { silent: true });
    updateChip(ed); renderTabs();
    if (state.editor === ed) updateOutline();
  }

  const inPane = (ed, sel) => (ed && ed.pane ? ed.pane.querySelector(sel) : null);

  function updateChip(ed) {
    ed = ed || state.editor;
    const chip = inPane(ed, "#tm-ed-chip");
    if (!chip) return;
    chip.className = "tm-state-chip" + (ed.dirty ? " is-dirty" : ed.saved ? " is-saved" : "");
    chip.textContent = ed.dirty ? "Unsaved changes" : ed.saved ? "Saved" : "Not saved yet";
  }

  // What choosing a template will also create. The two TOOL_UI templates ship a folder.
  function templateFolderOf(id) {
    const t = (state.templates || []).find((x) => x.id === id);
    return t && t.folder ? t.folder : "";
  }
  function templateHelpText(id) {
    const folder = templateFolderOf(id);
    return folder ? "Saving also creates ~/.jarvis/tools/" + folder + "/" : " ";
  }

  function nameHelp(ed) {
    ed = ed || state.editor;
    const input = inPane(ed, "#tm-ed-name"), help = inPane(ed, "#tm-ed-name-help");
    if (!ed || !input || !help) return;
    const problem = ed.mode === "edit" ? "" : nameProblem(input.value, userFileNames(), ed.mode);
    const show = ed.nameTouched && problem;
    input.classList.toggle("is-invalid", !!show);
    help.className = "tm-field__help" + (show ? " is-bad" : "");
    help.textContent = show ? problem : ed.mode === "edit" ? "The file name can’t change here." : "lower_snake_case — becomes ~/.jarvis/tools/<name>.py";
  }

  // Attach this tab's editor to the middle pane, building it the first time.
  // The pane (and the code editor inside it, with its caret, scroll and undo
  // history) is kept alive while another tab is showing - that is what makes
  // switching tabs lossless.
  function buildEditor(ed) {
    const d = dom.detail;
    clear(d);
    setText(dom.midTitle, ed.mode === "edit" ? "Editing " + ed.origName : "Create a tool");
    dom.foot.hidden = true;
    if (!ed.pane) createEditorPane(ed);
    d.appendChild(ed.pane);
    d.style.padding = "0"; d.style.overflow = "hidden"; d.style.display = "flex";
    if (ed.cm && ed.cm.remeasure) ed.cm.remeasure();
    updateChip(ed); nameHelp(ed); renderAgent(ed);
  }

  function createEditorPane(ed) {
    const name = el("input", { class: "tm-input", id: "tm-ed-name", type: "text", autocomplete: "off", spellcheck: "false",
      placeholder: "my_tool", value: ed.name, readonly: ed.mode === "edit" || null, "aria-describedby": "tm-ed-name-help", style: "width:210px" });
    name.addEventListener("input", () => { ed.name = name.value.trim().toLowerCase(); ed.nameTouched = true; ed.dirty = true; updateChip(ed); nameHelp(ed); renderTabs(); persistSoon(); draftSoon(ed); });
    const tpl = el("select", { class: "tm-select", id: "tm-ed-template", "aria-label": "Start from a template", style: "min-width:150px",
      onchange: async (e) => {
        const id = e.target.value;
        if (ed.dirty && ed.source.trim() && !(await UI().confirm({ title: "Replace what’s in the editor?", level: "warn", focusCancel: true,
          confirmLabel: "Replace", body: "Loading a template overwrites the code you’ve typed." }))) { e.target.value = ed.template; return; }
        await loadTemplate(ed, id);
      } }, (state.templates.length ? state.templates : [{ id: "minimal", label: "Minimal" }]).map((t) => el("option", { value: t.id, title: t.hint || "" }, t.label)));
    tpl.value = ed.template;
    tpl.addEventListener("change", () => { const h = inPane(ed, "#tm-ed-template-help"); if (h) h.textContent = templateHelpText(tpl.value); });

    const actions = el("div", { class: "tm-editor__actions" }, [
      el("button", { class: "btn btn--primary", type: "button", id: "tm-ed-save", onclick: saveEditor, title: "Save (Ctrl+S)" }, "Save"),
      el("button", { class: "btn btn--ghost", type: "button", id: "tm-ed-check", onclick: validateEditor, title: "Checks the file without saving it (Ctrl+Enter)" }, "Validate"),
      el("button", { class: "btn btn--ghost", type: "button", onclick: () => closeTab(ed), title: "Close this tab" }, "Close"),
    ]);
    const bar = el("div", { class: "tm-editor__bar" }, [
      el("div", { class: "tm-field" }, [el("label", { class: "tm-field__label", for: "tm-ed-name" }, "File name"), name, el("span", { class: "tm-field__help", id: "tm-ed-name-help" })]),
      ed.mode === "new" ? el("div", { class: "tm-field" }, [el("label", { class: "tm-field__label", for: "tm-ed-template" }, "Template"), tpl, el("span", { class: "tm-field__help", id: "tm-ed-template-help" }, templateHelpText(ed.template))]) : null,
      el("span", { class: "tm-state-chip", id: "tm-ed-chip" }),
      actions,
    ]);

    ed.cm = makeCodeEditor(ed);
    ed.pane = el("div", { class: "tm-editor", "data-tab": ed.id }, [bar, ed.cm.el, buildAgentDock(ed)]);
  }

  // The editor itself is code-editor.js. If that script didn't load, a plain
  // textarea with the same small interface keeps the manager usable (the same
  // idea as I-B18(c): say so instead of leaving a dead panel).
  function makeCodeEditor(ed) {
    const common = {
      value: ed.source, label: "Tool source code", placeholder: "Pick a template above, write a tool here, or ask Jarvis below.",
      onChange: (v) => { ed.source = v; ed.dirty = true; ed.errorLine = null; updateChip(ed); renderTabs(); if (state.editor === ed) scheduleOutline(); persistSoon(); draftSoon(ed); },
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
      setReadOnly(on) { ta.readOnly = !!on; }, scrollToEnd() { ta.scrollTop = ta.scrollHeight; },
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

  // Bring a line into the editor's viewport (a marker you can't see isn't a marker).
  function revealLine(n) {
    const ed = state.editor;
    if (ed && ed.cm && n) ed.cm.revealLine(n);
  }

  function applyResult(result, ed) {
    ed = ed || state.editor;
    ed.result = result;
    ed.errorLine = result && !result.ok ? parseErrorLine(result.error) : null;
    if (ed.cm) ed.cm.setErrorLine(ed.errorLine || 0);
    if (state.editor === ed) { renderSide(); if (ed.errorLine) revealLine(ed.errorLine); }
  }

  async function validateEditor() {
    const ed = state.editor;
    if (!ed || ed.busy) return;
    if (ed.chat.busy) { toast("Jarvis is still writing — stop it or wait for it to finish first.", "info"); return; }
    ed.busy = "check"; renderSide();
    try {
      const name = ed.name || "draft";
      applyResult(await ctools("/" + encodeURIComponent(NAME_RE.test(name) ? name : "draft") + "/check", { method: "POST", body: JSON.stringify({ source: ed.source }) }), ed);
    } catch (e) { applyResult({ ok: false, stage: "error", error: e.message }, ed); }
    ed.busy = ""; if (state.editor === ed) renderSide();
  }

  async function saveEditor() {
    const ed = state.editor;
    if (!ed || ed.busy) return;
    if (ed.chat.busy) { toast("Jarvis is still writing — stop it or wait for it to finish before saving.", "info"); return; }
    ed.nameTouched = true; nameHelp(ed);
    const problem = nameProblem(ed.name, userFileNames(), ed.mode);
    if (problem) { const n = inPane(ed, "#tm-ed-name"); if (n) n.focus(); return; }
    ed.busy = "save"; renderSide();
    try {
      // draftKey: a successful save removes this tab's backup, a refused one keeps the text
      // there. scaffold: the template a NEW file started from, so its html/js/css folder is made.
      if (ed.draftTimer) { clearTimeout(ed.draftTimer); ed.draftTimer = 0; }
      const r = await ctools("/" + encodeURIComponent(ed.name), { method: "PUT", body: JSON.stringify({
        source: ed.source, draftKey: ed.draftKey, scaffold: ed.mode === "new" ? ed.template : "" }) });
      applyResult(r, ed);
      if (r && r.ok) {
        ed.draftSynced = "";
        if (r.scaffolded && r.scaffolded.length) toast("Also created " + r.scaffolded.join(", ") + " in ~/.jarvis/tools.", "success");
        else if (r.scaffold_note) toast(r.scaffold_note, "info");
        loadDraftsSoon();
        ed.mode = "edit"; ed.origName = ed.name; ed.dirty = false; ed.saved = true;
        if (state.editor === ed) setText(dom.midTitle, "Editing " + ed.origName);
        // From here on this IS that file. Lock the name (a retyped name in "edit"
        // mode would skip the collision check and could overwrite another tool)
        // and drop the template picker (it would replace the saved file's code).
        const nameInput = inPane(ed, "#tm-ed-name");
        if (nameInput) { nameInput.readOnly = true; nameInput.value = ed.origName; }
        const tplField = inPane(ed, "#tm-ed-template");
        if (tplField && tplField.closest(".tm-field")) tplField.closest(".tm-field").remove();
        toast("Saved " + ed.name + ". " + (r.note || ""), "success");
        await loadAll({ quiet: true, keepView: true });
        nameHelp(ed); renderTabs(); persistSoon();
      }
    } catch (e) {
      applyResult(e.data && e.data.error ? Object.assign({ ok: false }, e.data) : { ok: false, stage: "error", error: e.message }, ed);
      // A refused save is stashed on the server (draft_saved) so it isn't only in this tab.
      if (e.data && e.data.draft_saved) { ed.draftSynced = ed.source; loadDraftsSoon(); }
    }
    ed.busy = ""; updateChip(ed); if (state.editor === ed) renderSide();
  }

  /* ---- unfinished-tool backups ------------------------------------------------------ */

  // A couple of seconds after the last change, copy a changed tab's text to the server.
  // Failure is silent: the tab is still kept in this browser (persistTabs); this is the
  // second copy that survives a cleared browser, a Discard and a refused save.
  function draftSoon(ed) {
    if (!ed || !ed.dirty) return;
    if (ed.draftTimer) clearTimeout(ed.draftTimer);
    ed.draftTimer = setTimeout(() => { ed.draftTimer = 0; syncDraft(ed); }, 2500);
  }

  async function syncDraft(ed) {
    if (!ed || !ed.dirty || !String(ed.source || "").trim() || ed.source === ed.draftSynced) return;
    const text = ed.source;
    try {
      await api("/api/ctools/drafts/" + encodeURIComponent(ed.draftKey), { method: "PUT", body: JSON.stringify({
        source: text, name: ed.name, mode: ed.mode, origName: ed.origName, template: ed.template }) });
      ed.draftSynced = text;
      loadDraftsSoon();
    } catch (_) { /* kept in the browser regardless */ }
  }

  let draftsTimer = 0;
  function loadDraftsSoon() {
    if (draftsTimer) clearTimeout(draftsTimer);
    draftsTimer = setTimeout(() => { draftsTimer = 0; loadDrafts(); }, 800);
  }

  async function loadDrafts() {
    try { const r = await ctools("/drafts"); state.drafts = (r && r.drafts) || []; state.draftsError = ""; }
    catch (e) { state.draftsError = e.message; }
    if (dom && state.view !== "editor") renderSide();
  }

  // On closing a tab: flush a changed one (so the last seconds aren't lost) and say where
  // it went; a tab with nothing unfinished drops its backup. Never blocks the close.
  async function finishDraftOnClose(ed, force) {
    if (ed.draftTimer) { clearTimeout(ed.draftTimer); ed.draftTimer = 0; }
    try {
      if (!force && ed.dirty && String(ed.source || "").trim()) {
        await syncDraft(ed);
        if (ed.draftSynced === ed.source) toast(tabLabel(ed) + " closed — its unsaved text is kept under Unfinished tools.", "info");
      } else if (ed.draftSynced) {
        await api("/api/ctools/drafts/" + encodeURIComponent(ed.draftKey), { method: "DELETE" });
        ed.draftSynced = "";
      }
    } catch (_) { /* best effort */ }
    loadDrafts();
  }

  async function restoreDraft(key, version) {
    const openTab = state.tabs.find((t) => t.draftKey === key);
    if (openTab) { toast("That one is already open in a tab.", "info"); showEditor(openTab); return; }
    let d;
    try { d = await ctools("/drafts/" + encodeURIComponent(key)); }
    catch (e) { toast("Couldn’t open that backup: " + e.message, "error"); return; }
    let source = d.source || "";
    if (version != null && d.history && d.history[version]) source = d.history[version].source || "";
    if (d.file_newer) {
      const ok = await UI().confirm({ title: "Open an older copy?", level: "warn", focusCancel: true, confirmLabel: "Open it",
        body: "The saved file " + (d.orig_name || d.name) + ".py was changed after this backup was made. Opening the backup puts the older text in a tab; nothing is replaced until you Save." });
      if (!ok) return;
    }
    const asEdit = d.mode === "edit" && NAME_RE.test(d.orig_name || "") && userFileNames().includes(d.orig_name);
    const latest = version == null;
    if (asEdit) {
      const same = state.tabs.find((t) => t.mode === "edit" && t.origName === d.orig_name);
      if (same) { toast(d.orig_name + " is already open in a tab — close it first to restore into a new one.", "info"); showEditor(same); return; }
      await enterEditor({ mode: "edit", name: d.orig_name, source, dirty: true, draftKey: key, draftSynced: latest ? source : "", keepSource: true });
    } else {
      await enterEditor({ mode: "new", name: NAME_RE.test(d.name || "") ? d.name : "", source, dirty: true,
        template: d.template || "minimal", draftKey: key, draftSynced: latest ? source : "", keepSource: true });
    }
  }

  async function restoreEarlier(key) {
    let d;
    try { d = await ctools("/drafts/" + encodeURIComponent(key)); }
    catch (e) { toast("Couldn’t read that backup: " + e.message, "error"); return; }
    const hist = (d.history || []).slice().reverse();      // newest first
    if (!hist.length) { toast("No earlier versions yet.", "info"); return; }
    const options = hist.map((h, i) => ({ label: String(h.saved || "").replace("T", " ") || "earlier", value: String(hist.length - 1 - i),
      hint: String(h.source || "").split("\n").length + " lines" }));
    const pick = await UI().choose({ title: "Restore an earlier version of " + draftTitle(d), body: "Opens that version in a new tab. The newest backup is left as it is.", options });
    if (pick === null || pick === undefined) return;
    restoreDraft(key, Number(pick));
  }

  async function deleteDraft(d) {
    const ok = await UI().confirm({ title: "Delete the backup of " + draftTitle(d) + "?", level: "error", focusCancel: true, confirmLabel: "Delete",
      body: "This removes the backed-up text. It can’t be undone." });
    if (!ok) return;
    try {
      await api("/api/ctools/drafts/" + encodeURIComponent(d.key), { method: "DELETE" });
      const tab = state.tabs.find((t) => t.draftKey === d.key);
      if (tab) tab.draftSynced = "";
    } catch (e) { toast("Couldn’t delete it: " + e.message, "error"); }
    loadDrafts();
  }

  function draftsSection() {
    const openKeys = new Set(state.tabs.map((t) => t.draftKey));
    const wrap = el("div", null, [sectionTitle("Unfinished tools", state.drafts.length ? String(state.drafts.length) : "")]);
    if (state.draftsError && !state.drafts.length) { wrap.appendChild(el("div", { class: "tm-hint" }, "Couldn’t read the backups: " + state.draftsError)); return wrap; }
    wrap.appendChild(el("div", { class: "tm-hint", style: "margin-bottom:8px" }, "Tools you were still writing are backed up as you type — saved or not — so a closed tab or a cleared browser doesn’t lose them."));
    const list = el("div", { class: "tm-attn" });
    state.drafts.slice(0, 12).forEach((d) => {
      const isOpen = openKeys.has(d.key);
      const why = (d.reason ? d.reason + " · " : "") + draftAge(d.saved) + " · " + d.lines + (d.lines === 1 ? " line" : " lines")
        + (d.file_newer ? " · the saved file is newer" : "") + (isOpen ? " · open in a tab" : "");
      list.appendChild(el("div", { class: "tm-draft", "data-key": d.key }, [
        el("div", { class: "tm-attn__name" }, draftTitle(d)),
        el("div", { class: "tm-attn__why" }, why),
        d.preview ? el("div", { class: "tm-draft__preview" }, d.preview) : null,
        el("div", { class: "tm-draft__btns" }, [
          el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => restoreDraft(d.key) }, isOpen ? "Show tab" : "Restore"),
          d.versions ? el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => restoreEarlier(d.key) }, "Earlier…") : null,
          el("button", { class: "btn btn--ghost btn--sm tm-danger", type: "button", onclick: () => deleteDraft(d) }, "Delete"),
        ]),
      ]));
    });
    wrap.appendChild(list);
    if (state.drafts.length > 12) wrap.appendChild(el("div", { class: "tm-hint", style: "margin-top:6px" }, "+" + (state.drafts.length - 12) + " older ones on disk in ~/.jarvis/tools/_drafts."));
    return wrap;
  }

  /* ---- keeping tabs across closing the panel and reloading the page ---------- */

  const TABS_KEY = "jarvis-tool-manager-tabs";
  const TABS_MAX_CHARS = 400000;
  let persistTimer = 0;

  function persistSoon() {
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => { persistTimer = 0; persistTabs(); }, 500);
  }

  function persistTabs() {
    try {
      const items = state.tabs.map((ed) => ({
        mode: ed.mode, name: ed.name, origName: ed.origName, dirty: !!ed.dirty, untitled: ed.untitled, template: ed.template, draftKey: ed.draftKey,
        // A clean saved file is re-read from disk on restore; anything else needs its text kept.
        source: ed.dirty || ed.mode === "new" ? ed.source : "",
      }));
      const payload = JSON.stringify({ v: 1, tabs: items });
      if (payload.length > TABS_MAX_CHARS) { state.persistOk = false; return; }
      if (items.length) global.localStorage.setItem(TABS_KEY, payload); else global.localStorage.removeItem(TABS_KEY);
      state.persistOk = true;
    } catch (_) { state.persistOk = false; }
  }

  async function restoreTabs() {
    if (state.restored) return;
    state.restored = true;
    let saved = null;
    try { saved = JSON.parse(global.localStorage.getItem(TABS_KEY) || "null"); } catch (_) { return; }
    if (!saved || !Array.isArray(saved.tabs)) return;
    for (const t of saved.tabs.slice(0, 12)) {
      if (!t || typeof t !== "object") continue;
      const text = typeof t.source === "string" ? t.source : "";
      if (t.mode === "edit") {
        if (!NAME_RE.test(t.origName || "") || !userFileNames().includes(t.origName)) continue;      // deleted since
        if (state.tabs.some((x) => x.mode === "edit" && x.origName === t.origName)) continue;
        let source = text, initial = null;
        if (!(t.dirty && text)) {
          try { initial = await ctools("/" + encodeURIComponent(t.origName)); source = (initial && initial.source) || ""; } catch (_) { continue; }
        }
        state.tabs.push(newEditorState({ mode: "edit", name: t.origName, source, initial, dirty: !!(t.dirty && text), draftKey: t.draftKey }));
      } else if (t.mode === "new") {
        const ed = newEditorState({ mode: "new", name: NAME_RE.test(t.name || "") ? t.name : "", source: text, dirty: !!t.dirty,
          template: typeof t.template === "string" ? t.template : "minimal", untitled: Number(t.untitled) || 0, draftKey: t.draftKey });
        state.tabs.push(ed);
      }
    }
    renderTabs();
  }

  /* ---- Ask Jarvis: the agent dock under the code (L.43) ------------------------ */

  function buildAgentDock(ed) {
    const caret = el("span", { class: "tm-agent__caret", "aria-hidden": "true" });
    const stateEl = el("span", { class: "tm-agent__state" });
    const head = el("div", { class: "tm-agent__head", role: "button", tabindex: "0", "aria-expanded": "true",
      title: "Describe what you want and Jarvis writes it into this editor",
      onclick: () => toggleAgent(ed),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleAgent(ed); } } },
    [el("span", { class: "tm-agent__title" }, "Ask Jarvis"), stateEl, caret]);
    const log = el("div", { class: "tm-agent__log", "aria-live": "polite" });
    const input = el("textarea", { class: "tm-agent__input", rows: "2", maxlength: "4000", spellcheck: "true",
      "aria-label": "Tell Jarvis what the tool should do",
      placeholder: ed.mode === "edit" ? "Ask Jarvis to change this tool…  (Enter sends, Shift+Enter adds a line)" : "Describe the tool you want — Jarvis writes it here while you watch…  (Enter sends)" });
    const send = el("button", { class: "btn btn--primary btn--sm", type: "button", onclick: () => sendAgent(ed) }, "Send");
    const stop = el("button", { class: "btn btn--ghost btn--sm", type: "button", hidden: true, title: "Stop writing (keeps what is already in the editor)", onclick: () => stopAgent(ed) }, "Stop");
    const undo = el("button", { class: "btn btn--ghost btn--sm", type: "button", hidden: true, title: "Put back the text from before Jarvis’s last edit", onclick: () => undoAgent(ed) }, "Undo AI edit");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendAgent(ed); }
    });
    const compose = el("div", { class: "tm-agent__compose" }, [input, el("div", { class: "tm-agent__btns" }, [send, stop, undo])]);
    const dock = el("div", { class: "tm-agent" }, [head, log, compose]);
    ed.ui = { dock, head, caret, stateEl, log, compose, input, send, stop, undo };
    return dock;
  }

  function toggleAgent(ed) { state.agentOpen = !state.agentOpen; savePrefs(); renderAgent(ed); }

  // L.45: Jarvis's replies are Markdown (the shared renderer in app.js:
  // sanitized, no images, links in a new tab). What the owner typed and error
  // messages stay plain text, as does everything if the renderer isn't there.
  function paintAgentText(node, m) {
    const md = global.JarvisMarkdown;
    const text = m.text || (m.pending ? "Thinking" : "");
    if (m.role !== "user" && !m.err && m.text && md && md.renderInto) {
      node.classList.add("jv-md");
      try { md.renderInto(node, m.text, { streaming: !!m.pending }); return; }
      catch (_) { node.classList.remove("jv-md"); }
    }
    node.textContent = text;
  }

  function paintAgentLog(ed) {
    const u = ed.ui; if (!u) return;
    const nearEnd = u.log.scrollHeight - u.log.scrollTop - u.log.clientHeight < 40;
    clear(u.log);
    ed.chat.messages.forEach((m) => {
      const node = el("div", { class: "tm-agent__msg " + (m.role === "user" ? "tm-agent__msg--user" : "tm-agent__msg--ai") + (m.err ? " tm-agent__msg--err" : "") + (m.pending ? " is-pending" : "") });
      paintAgentText(node, m);
      u.log.appendChild(node);
    });
    u.log.hidden = !state.agentOpen || !ed.chat.messages.length;
    if (nearEnd) u.log.scrollTop = u.log.scrollHeight;
  }

  function renderAgent(ed) {
    const u = ed && ed.ui; if (!u) return;
    const busy = ed.chat.busy;
    u.head.setAttribute("aria-expanded", state.agentOpen ? "true" : "false");
    u.caret.textContent = state.agentOpen ? "▾" : "▴";
    u.stateEl.textContent = busy ? "writing into this editor…" : (ed.chat.messages.length ? "" : "describe a tool, or ask for a change");
    u.stateEl.classList.toggle("is-live", busy);
    u.compose.hidden = !state.agentOpen;
    u.input.disabled = busy;
    u.send.hidden = busy; u.stop.hidden = !busy;
    u.undo.hidden = busy || !ed.chat.undoable;
    paintAgentLog(ed);
  }

  // While tokens stream, only the last bubble's text changes - no rebuild, so
  // the caret in the input and the scroll position of the log are left alone.
  function paintAgentLive(ed) {
    const u = ed.ui; if (!u) return;
    const last = u.log.lastElementChild, msg = ed.chat.messages[ed.chat.messages.length - 1];
    if (!last || !msg) { paintAgentLog(ed); return; }
    last.classList.remove("jv-md");
    paintAgentText(last, msg);
    u.log.scrollTop = u.log.scrollHeight;
  }

  function setAgentBuffer(ed, text) {
    if (!ed.cm) return;
    // L.45: keep the view where it is (stream(), not setValue()) so the end of
    // an edit, an Undo and a failed edit's restore don't snap the editor to line 1.
    if (ed.cm.stream) ed.cm.stream(text, -1); else ed.cm.setValue(text, { silent: true });
    ed.source = text; ed.dirty = true; ed.errorLine = null;
    ed.result = null;                                   // a check of the old text says nothing about this one
    updateChip(ed); renderTabs();
    if (state.editor === ed) renderSide();
    draftSoon(ed);
  }

  function sendAgent(ed) {
    const u = ed.ui; if (!u) return;
    const text = u.input.value.trim();
    if (!text) { u.input.focus(); return; }
    u.input.value = "";
    if (!state.agentOpen) { state.agentOpen = true; savePrefs(); }
    askAgent(ed, text);
  }

  function stopAgent(ed) {
    ed.chat.stopped = true;                             // also ends the typed-in reveal, which has no request to abort
    if (ed.chat.abort) { try { ed.chat.abort.abort(); } catch (_) { /* done already */ } }
  }

  function undoAgent(ed) {
    const chat = ed.chat;
    if (chat.busy || chat.backup === null || !chat.undoable) return;
    setAgentBuffer(ed, chat.backup);
    chat.undoable = false;
    chat.messages.push({ role: "ai", text: "Put back the text from before my last edit." });
    renderAgent(ed); persistSoon();
  }

  async function askAgent(ed, instruction) {
    const chat = ed.chat;
    if (chat.busy || !ed.cm) return;
    const name = NAME_RE.test(ed.name) ? ed.name : "draft";
    const before = ed.cm.getValue();
    const reply = { role: "ai", text: "", pending: true, err: false };
    chat.messages.push({ role: "user", text: instruction }, reply);
    chat.busy = true; chat.backup = before; chat.undoable = false; chat.stopped = false;
    chat.abort = typeof AbortController !== "undefined" ? new AbortController() : null;
    if (ed.cm.setReadOnly) ed.cm.setReadOnly(true);
    ed.cm.el.classList.add("is-ai-writing");
    renderAgent(ed); renderTabs();

    let acc = "", wrote = false, finished = false, raf = 0, pendingCode = null;
    let lastCode = null, tailShown = false;
    // L.45: paint what has arrived over the old file (see overwriteView). ed.source
    // holds only the NEW text so far, never the old tail mixed in.
    const paintStream = (code) => {
      if (!ed.cm) return;
      lastCode = code;
      if (ed.cm.stream) {
        const v = overwriteView(code, before);
        tailShown = v.tail;
        ed.cm.stream(v.text, v.head);
      } else { ed.cm.setValue(code, { silent: true }); if (ed.cm.scrollToEnd) ed.cm.scrollToEnd(); }
      ed.source = code; ed.dirty = true;
    };
    const flush = () => {
      raf = 0;
      if (pendingCode === null || !ed.cm) return;
      const code = pendingCode; pendingCode = null;
      paintStream(code);
    };
    const begin = () => {
      if (wrote) return;
      wrote = true; ed.errorLine = null; ed.result = null; updateChip(ed); renderTabs(); if (state.editor === ed) renderSide();
    };
    const live = (code) => {
      pendingCode = code;
      begin();
      if (!raf) raf = (global.requestAnimationFrame || ((f) => setTimeout(f, 16)))(flush);
    };
    // A model/provider that doesn't stream hands over the whole file at the end.
    // Type it in by lines (about a second and a half at most) so the edit is
    // still seen happening instead of the file silently swapping. Stop ends it.
    const reveal = async (full) => {
      const lines = full.replace(/\n$/, "").split("\n"), step = Math.max(1, Math.ceil(lines.length / 50));
      begin();
      for (let n = step; n < lines.length; n += step) {
        if (chat.stopped) return false;
        paintStream(lines.slice(0, n).join("\n") + "\n");
        await new Promise((r) => setTimeout(r, 30));
      }
      return !chat.stopped;
    };
    const commit = (text) => { tailShown = false; setAgentBuffer(ed, text); };
    const settle = () => { if (raf && global.cancelAnimationFrame) global.cancelAnimationFrame(raf); raf = 0; flush(); };

    try {
      const res = await fetch("/api/ctools/" + encodeURIComponent(name) + "/agent", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: chat.abort ? chat.abort.signal : undefined,
        body: JSON.stringify({ instruction, source: before, history: chat.history }),
      });
      if (!res.ok) {
        let msg = res.statusText || ("HTTP " + res.status);
        try { const j = await res.json(); msg = j.error || msg; } catch (_) { /* not JSON */ }
        throw new Error(msg);
      }
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line) continue;
          let ev; try { ev = JSON.parse(line); } catch (_) { continue; }
          if (ev.type === "stream") {
            if (ev.k === "reset") {                    // a key failed mid-reply; the next one starts over
              acc = ""; reply.text = ""; pendingCode = null;
              if (wrote) { settle(); commit(before); wrote = false; }
            } else {
              acc += ev.d || "";
              const sp = splitAgentReply(acc);
              reply.text = sp.note;
              if (sp.code) live(sp.code);
            }
            if (state.editor === ed) paintAgentLive(ed);
          } else if (ev.type === "done") {
            finished = true;
            settle();
            if (ev.ok) {
              if (ev.code) {
                if (!wrote && ev.code !== before && !(await reveal(ev.code))) { const e = new Error("stopped"); e.name = "AbortError"; throw e; }
                commit(ev.code);
                chat.undoable = ev.code !== before;
                reply.text = (ev.note || "Done — the file is updated.") + "\n\nRead it through, then Validate or Save. Validate runs the file’s top level, so look before you click.";
              } else {
                if (wrote) commit(before);   // no complete file came back: leave the editor as it was
                reply.text = ev.note || "Jarvis had nothing to change.";
              }
              chat.history.push({ role: "user", content: instruction }, { role: "assistant", content: (ev.note || (ev.code ? "(updated the file)" : "")).slice(0, 600) });
              if (chat.history.length > 12) chat.history.splice(0, chat.history.length - 12);
            } else {
              if (wrote) commit(before);
              reply.text = ev.error || "Jarvis couldn’t answer.";
              reply.err = true;
            }
          }
        }
      }
      if (!finished) {
        settle();
        reply.text = (reply.text ? reply.text + "\n\n" : "") + "The connection ended before Jarvis finished. What was written so far is in the editor; Undo AI edit puts the old text back.";
        reply.err = true; chat.undoable = wrote;
      }
    } catch (e) {
      settle();
      if (e && e.name === "AbortError") {
        reply.text = (reply.text ? reply.text + "\n\n" : "") + "Stopped." + (wrote ? " The editor keeps what was written; Undo AI edit puts the old text back." : "");
        chat.undoable = wrote;
      } else {
        if (wrote) { commit(before); wrote = false; }
        reply.text = e && e.message ? e.message : "Couldn’t reach Jarvis.";
        reply.err = true;
      }
    } finally {
      settle();
      // Interrupted (Stop, dropped connection): keep what was written, minus the
      // old text that was only showing underneath it.
      if (tailShown && lastCode !== null && ed.cm && ed.cm.stream) { ed.cm.stream(lastCode, -1); tailShown = false; }
      reply.pending = false;
      chat.busy = false; chat.abort = null;
      if (ed.cm) { if (ed.cm.setReadOnly) ed.cm.setReadOnly(false); ed.cm.el.classList.remove("is-ai-writing"); }
      renderAgent(ed); renderTabs(); updateChip(ed); persistSoon();
      if (state.editor === ed && ed.ui) ed.ui.input.focus();
    }
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
      if ((r.ui || []).length) res.appendChild(el("div", { class: "tm-note tm-note--info", style: "margin-top:8px" }, "Screens: " + r.ui.map((u) => u.label + " (" + (u.mode === "menu" ? "Menu entry" : "button") + ", " + u.path + "/)").join("; ")));
      uiLines(r, templateFolderOf(ed.template), ed.mode === "new").forEach((l) => res.appendChild(el("div", { class: "tm-note", style: "margin-top:8px" }, l)));
    } else {
      res.appendChild(el("div", { class: "tm-errbox" }, (r.stage ? "[" + r.stage + "] " : "") + (r.error || "Unknown error") + (ed.errorLine ? "\n→ line " + ed.errorLine + " is marked in the editor" : "")));
      if (ed.errorLine && ed.cm) res.appendChild(el("button", { class: "btn btn--ghost tm-jump", type: "button", onclick: () => ed.cm.gotoLine(ed.errorLine) }, "Go to line " + ed.errorLine));
      if (r.hint) res.appendChild(el("div", { class: "tm-note", style: "margin-top:8px" }, r.hint));
      if (r.draft_saved) res.appendChild(el("div", { class: "tm-note tm-note--info", style: "margin-top:8px" }, "Nothing was saved as a tool, but this text is backed up under Unfinished tools."));
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
      "A custom tool is Python running as you. Jarvis can draft code into this editor (Ask Jarvis, under the code) but it can’t save or run a file — only your Save does, and Validate runs the file’s top level, so read AI-written code first. Tools you save aren’t asked about again; imports are. Safeguards for a new tool appear in the list once it’s saved."),
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
      state.editor = null;                              // L.43: open tabs stay open; the review just takes the pane
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
    const [tools, files, templates, commands, disabled, daemons, personas, uiEls, drafts] = await Promise.allSettled([
      api("/api/tools"), ctools(""), state.templates.length ? Promise.resolve(null) : ctools("/templates"),
      api("/api/commands"), api("/api/disabled"),
      api("/api/daemons"), api("/api/personas?all=1"), api("/api/tool-ui?all=1"), ctools("/drafts"),
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
    if (disabled.status === "fulfilled") {
      state.disabledCommands = (disabled.value && disabled.value.commands) || [];
      state.disabledPersonas = (disabled.value && disabled.value.personas) || [];
      state.disabledSkins = (disabled.value && disabled.value.skins) || [];
    }
    else if (!state.commandsError) state.commandsError = "couldn’t read which commands are switched off";
    // The non-tool rows. Any of the three failing just leaves its rows out and says so in
    // the status line; it never hides the tools.
    const failedExtras = [];
    if (daemons.status === "fulfilled") state.daemons = (daemons.value && daemons.value.daemons) || []; else failedExtras.push("daemons");
    if (personas.status === "fulfilled") state.personas = (personas.value && personas.value.personas) || []; else failedExtras.push("personas");
    if (uiEls.status === "fulfilled") state.uiElements = (uiEls.value && uiEls.value.ui) || []; else failedExtras.push("screens");
    state.extrasError = failedExtras.length ? failedExtras.join(", ") : "";
    state.draftsError = drafts.status === "rejected" ? String(drafts.reason && drafts.reason.message || drafts.reason) : "";
    if (drafts.status === "fulfilled") state.drafts = (drafts.value && drafts.value.drafts) || [];
    state.rows = buildRows(state.toolsError ? [] : state.tools, state.files, state.commandsError ? null : state.commands, state.disabledCommands,
      { daemons: state.daemons, personas: state.personas, ui: state.uiElements, ...skinExtras() });
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
    else if (!opts.quiet || dom.statusLine.textContent.startsWith("reading")) setStatusLine(cov.total + " tools available" + (offSummary(cov) ? " · " + offSummary(cov) + " off" : "") + (cov.commands ? " · " + cov.commands + (cov.commands === 1 ? " saved command" : " saved commands") : "") + (state.commandsError ? " · saved commands unavailable" : "") + (state.extrasError ? " · couldn’t read " + state.extrasError : "") + (failed ? " · " + failed + " file" + (failed > 1 ? "s" : "") + " rejected" : "") + (state.filesError ? " · your tool files couldn’t be read" : ""), state.filesError || state.commandsError ? "error" : "");
    renderChips(); renderGroupSelect();
    if (state.view === "editor") { renderList(); renderSide(); renderTabs(); return; }
    renderMain();
  }

  /* ---- L.44: closable side panes ------------------------------------------ */

  function applyPanes() {
    if (!dom) return;
    dom.panel.classList.toggle("tm-hide-tools", state.hideTools);
    dom.panel.classList.toggle("tm-hide-side", state.hideSide);
    if (dom.toggleTools) dom.toggleTools.setAttribute("aria-pressed", state.hideTools ? "false" : "true");
    if (dom.toggleSide) dom.toggleSide.setAttribute("aria-pressed", state.hideSide ? "false" : "true");
    // The editor's column just changed width; let it re-measure once layout settles.
    const ed = state.view === "editor" ? state.editor : null;
    if (ed && ed.cm && ed.cm.remeasure) (global.requestAnimationFrame || setTimeout)(() => { if (ed.cm) ed.cm.remeasure(); });
  }

  function setPane(which, hidden) {
    if (which === "tools") state.hideTools = !!hidden; else state.hideSide = !!hidden;
    savePrefs(); applyPanes();
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
    if (e.key === "/") { e.preventDefault(); if (state.hideTools) setPane("tools", false); dom.search.focus(); return; }
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
    // L.44: hide / show the two side panes (remembered between visits).
    dom.closeTools.addEventListener("click", () => setPane("tools", true));
    dom.closeSide.addEventListener("click", () => setPane("side", true));
    dom.toggleTools.addEventListener("click", () => setPane("tools", !state.hideTools));
    dom.toggleSide.addEventListener("click", () => setPane("side", !state.hideSide));
    applyPanes();
    // L.43: unsaved tabs are also kept in this browser; only warn about leaving when
    // that could not be done (storage blocked / too big) or Jarvis is mid-write.
    global.addEventListener("beforeunload", (e) => {
      const risky = state.tabs.some((t) => t.chat.busy || (t.dirty && !state.persistOk));
      if (risky) { e.preventDefault(); e.returnValue = ""; }
    });
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
    // L.43: reopening lands exactly where you left it - an open editor tab stays on screen.
    if (state.view !== "editor") { restoreDetailBox(); state.view = "tool"; }
    renderChips(); renderGroupSelect();
    if (state.view === "editor") { renderList(); renderSide(); renderTabs(); if (state.editor && state.editor.cm && state.editor.cm.remeasure) state.editor.cm.remeasure(); }
    else if (state.rows.length) renderMain(); else renderDetail();
    if (state.view !== "editor" && !state.hideTools) dom.search.focus({ preventScroll: true });
    await loadAll();
    await restoreTabs();
    if (opts && opts.create) enterEditor({ mode: "new" });
    else if (opts && opts.tool) { const r = rowByName(opts.tool); if (r) select(r.id); }
  }

  // L.43: closing only hides the panel. Open editor tabs (and anything unsaved in
  // them) stay exactly as they are, so there is nothing to confirm or lose here.
  async function close() {
    if (!dom || dom.overlay.hidden) return;
    state.draft = state.view === "import" ? state.draft : null;
    dom.overlay.hidden = true;
    const prev = state.prevFocus; state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  global.JarvisToolManager = { open, close, refresh: () => loadAll(), _pure: PURE };
})(window);
