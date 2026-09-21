/* ============================================================================
 * test-checklist.js — Menu → Test Checklist.
 *
 * WHAT IT IS
 * ----------
 * A tester's workbench for every tool Jarvis can call: what the tool does, how
 * to test it (prompts to type into Ask, and arguments to run straight from
 * Debug), what a pass looks like, and a place to record the verdict — fully
 * working / partial / not as intended / bug / blocked — with notes.
 *
 * PURELY FRONT END
 * ----------------
 *  - The CATALOGUE (what to test and how) is test-checklist-data.js, a static
 *    file that ships with the UI. See AGENTS.md: whoever adds a tool adds its
 *    entry there.
 *  - The RESULTS (verdicts, ticked steps, notes) live in this browser's
 *    localStorage and nowhere else. Nothing is sent to the server, nothing is
 *    written to ~/.jarvis, no CLI command is involved. Export / Import moves
 *    them between browsers as a plain JSON file.
 *  - The only network call is a read-only GET /api/tools — the same one the
 *    Debug panel makes — used to (a) notice tools that exist in the CLI but
 *    have no checklist entry yet, and (b) pick up entries a tool's OWN module
 *    supplied (see below). If the CLI is offline the panel still works from
 *    the static catalogue.
 *
 * TWO SOURCES FOR AN ENTRY (master plan G.1)
 * ------------------------------------------
 *  1. SHIPPED: test-checklist-data.js — tools that ship with jarvis.
 *  2. SUPPLIED: a tool module's own TEST_CHECKLIST (and, for a brand-new
 *     TOOL_GROUP, TEST_CHECKLIST_GROUP). A user's own custom tool can never be
 *     in the shipped file, so this is the only way it gets a real entry. The
 *     CLI puts it on that tool's item in /api/tools as `checklist` (+
 *     `checklist_group`); mergeCatalogue() below folds it into the view the
 *     panel renders. Same shape, same rendering either way. If both sources
 *     have an entry for one tool, the shipped one wins.
 *  window.JARVIS_TEST_CHECKLIST stays exactly the shipped file (never
 *  mutated, so a refresh after deleting a custom tool really drops its
 *  entry); JarvisTestChecklist.catalogue() returns the merged view.
 *
 * A tool with neither is listed by name only, marked NO CHECKLIST, with no
 * further details (there is nothing to show and nothing to record).
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 * Same rule as ui-kit.js: catalogue strings, notes and live tool descriptions
 * are inserted with textContent, never innerHTML.
 *
 * Deliberately standalone, like custom-tools.js: app.js only needs the one
 * line that calls window.JarvisTestChecklist.open() from the Menu item.
 * ========================================================================= */

(function (global) {
  "use strict";

  const STORE_KEY = "jarvis.testChecklist.v1";
  // The shipped file, never mutated. DATA is what the panel renders: SHIPPED
  // plus any entries tools supplied themselves (rebuilt on every live read).
  const SHIPPED = global.JARVIS_TEST_CHECKLIST || null;
  let DATA = SHIPPED;

  const STATUSES = [
    { id: "untested", label: "Untested",         short: "Untested",   glyph: "\u25CB", key: "0", hint: "Not tried yet." },
    { id: "working",  label: "Fully working",    short: "Working",    glyph: "\u2713", key: "1", hint: "Does exactly what it should." },
    { id: "partial",  label: "Partially working", short: "Partial",   glyph: "\u2248", key: "2", hint: "Works, but something is missing or flaky." },
    { id: "off",      label: "Not as intended",  short: "Not as intended", glyph: "\u2260", key: "3", hint: "Runs, but the behaviour isn't what was meant." },
    { id: "bug",      label: "Bug / broken",     short: "Bug",        glyph: "\u2717", key: "4", hint: "Errors, crashes or wrong results." },
    { id: "blocked",  label: "Blocked",          short: "Blocked",    glyph: "\u2298", key: "5", hint: "Can't test yet — missing setup, account or hardware." },
  ];
  const STATUS = Object.fromEntries(STATUSES.map((s) => [s.id, s]));
  const ATTENTION_ORDER = ["bug", "off", "partial"];

  /* ---- tiny helpers ---------------------------------------------------- */

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
  const $ = (sel) => document.querySelector(sel);
  const UI = () => global.JarvisUI || null;

  function toast(message, level) {
    if (UI() && UI().toast) UI().toast({ message, level: level || "info" });
  }
  async function confirmDialog(opts) {
    if (UI() && UI().confirm) return UI().confirm(opts);
    return global.confirm((opts.title ? opts.title + "\n\n" : "") + (opts.body || ""));
  }

  // djb2 — tiny, stable, and collisions here would only merge two ticks.
  function hash(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }
  function stepText(step) {
    if (typeof step.ask === "string") return step.ask;
    return JSON.stringify(step.run === undefined ? {} : step.run);
  }
  // Keyed by the step's own text, not its position: reordering steps keeps
  // ticks, and editing a step's text clears its tick — a changed test has not
  // been run yet.
  const stepKey = (step) => (typeof step.ask === "string" ? "a" : "r") + hash(stepText(step));

  function fmtWhen(ts) {
    if (!ts) return "never";
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const PLACEHOLDER_RE = /<[^<>\n]{2,60}>/;

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      // Non-secure context or permission denied — fall back to a hidden textarea.
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

  /* ---- persistence: this browser only ---------------------------------- */

  const fresh = () => ({ v: 1, tools: {}, ui: {} });
  let store = fresh();
  let storageOk = true;

  function loadStore() {
    try {
      const raw = global.localStorage.getItem(STORE_KEY);
      if (!raw) return fresh();
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.v !== 1 || typeof parsed.tools !== "object" || !parsed.tools) return fresh();
      parsed.ui = parsed.ui && typeof parsed.ui === "object" ? parsed.ui : {};
      return parsed;
    } catch (_) {
      storageOk = false; // blocked or corrupt — keep working in memory
      return fresh();
    }
  }
  function persist() {
    try {
      global.localStorage.setItem(STORE_KEY, JSON.stringify(store));
      storageOk = true;
    } catch (_) {
      storageOk = false;
    }
    renderStatusLine();
  }
  let noteTimer = null;
  function persistSoon() {
    clearTimeout(noteTimer);
    noteTimer = setTimeout(persist, 250);
  }
  function flushPending() {
    if (noteTimer) { clearTimeout(noteTimer); noteTimer = null; persist(); }
  }

  const rec = (name) => store.tools[name] || null;
  function ensure(name) {
    if (!store.tools[name]) store.tools[name] = {};
    return store.tools[name];
  }
  const statusOf = (name) => {
    const r = rec(name);
    return r && STATUS[r.s] ? r.s : "untested";
  };
  function setStatus(name, s) {
    if (!STATUS[s]) return;
    const r = ensure(name);
    if ((r.s || "untested") === s) return;
    r.s = s;
    r.t = Date.now();
    r.h = [{ s, t: r.t }].concat(r.h || []).slice(0, 12);
    persist();
  }

  /* ---- catalogue + live merge ------------------------------------------ */

  const state = {
    built: false,
    live: null,          // Map name -> live tool, once /api/tools answered
    supplied: new Set(), // tool names whose entry came from their own module
    liveBusy: false,
    liveError: "",
    rows: [],
    selected: null,
    search: "",
    filter: "all",       // all | <status id> | nolist
    group: "",           // "" | group id
    collapsed: new Set(),
    prevFocus: null,
  };

  const groupIndex = () => new Map(((DATA && DATA.groups) || []).map((g, i) => [g.id, i]));
  const groupLabel = (id) => {
    const g = ((DATA && DATA.groups) || []).find((x) => x.id === id);
    return g ? g.label : id === "__nolist" ? "No checklist yet" : id;
  };

  /* ---- entries a tool's own module supplied (G.1) --------------------- */

  const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);
  const strOr = (v, dflt) => (typeof v === "string" && v.trim() ? v : dflt);
  const strList = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === "string" && x.trim()) : []);

  // The CLI validates these already (checklist_schema.py), but this panel also
  // talks to whatever CLI build happens to be installed — so a malformed entry
  // is dropped here rather than allowed to throw inside a renderer.
  function cleanSuppliedEntry(e) {
    if (!isObj(e) || !strOr(e.does, "")) return null;
    const steps = (Array.isArray(e.steps) ? e.steps : []).filter((s) =>
      isObj(s) && strOr(s.expect, "") && (typeof s.ask === "string" && s.ask.trim() ? !isObj(s.run) : isObj(s.run)));
    if (!steps.length) return null;
    const out = { group: strOr(e.group, "custom"), does: e.does, steps };
    const needs = strList(e.needs), watch = strList(e.watch);
    if (needs.length) out.needs = needs;
    if (watch.length) out.watch = watch;
    if (strOr(e.os, "")) out.os = e.os;
    if (strOr(e.care, "")) out.care = e.care;
    return out;
  }

  // "my_group" -> "My group". Only the fallback for a group whose module gave
  // no TEST_CHECKLIST_GROUP label; a real label always wins.
  function prettyGroup(id) {
    const t = String(id).replace(/[_-]+/g, " ").trim();
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : "Other tools";
  }

  // Pure: (shipped file data, /api/tools list) -> { data, from }.
  //   data  shipped + supplied entries; shipped groups keep their order and
  //         labels, new groups are appended in the order the CLI listed them
  //   from  Set of tool names whose entry was supplied
  function mergeCatalogue(shipped, liveList) {
    const data = { ...shipped, groups: (shipped.groups || []).slice(), tools: { ...(shipped.tools || {}) } };
    const from = new Set();
    const known = new Set(data.groups.map((g) => g.id));
    (Array.isArray(liveList) ? liveList : []).forEach((t) => {
      if (!isObj(t) || typeof t.name !== "string" || !t.name || t.checklist === undefined) return;
      if (data.tools[t.name]) return; // shipped wins
      const entry = cleanSuppliedEntry(t.checklist);
      if (!entry) return;
      data.tools[t.name] = entry;
      from.add(t.name);
      if (!known.has(entry.group)) {
        const meta = isObj(t.checklist_group) ? t.checklist_group : {};
        data.groups.push({ id: entry.group, label: strOr(meta.label, prettyGroup(entry.group)), blurb: strOr(meta.blurb, "Supplied by the tool's own module.") });
        known.add(entry.group);
      }
    });
    return { data, from };
  }

  function buildRows() {
    const order = groupIndex();
    const rows = [];
    let idx = 0;
    Object.entries((DATA && DATA.tools) || {}).forEach(([name, def]) => {
      rows.push({
        name, def, idx: idx++, group: def.group, listed: true, supplied: state.supplied.has(name),
        live: state.live ? state.live.get(name) || null : null,
        stale: !!state.live && state.live.size > 0 && !state.live.has(name),
      });
    });
    rows.sort((a, b) => ((order.has(a.group) ? order.get(a.group) : 999) - (order.has(b.group) ? order.get(b.group) : 999)) || a.idx - b.idx);
    if (state.live) {
      const extra = [];
      state.live.forEach((live, name) => {
        if (!DATA.tools[name]) extra.push({ name, def: null, idx: 0, group: "__nolist", listed: false, live, stale: false });
      });
      extra.sort((a, b) => a.name.localeCompare(b.name));
      rows.push(...extra);
    }
    state.rows = rows;
  }

  function stepProgress(row) {
    if (!row.listed) return { done: 0, total: 0 };
    const r = rec(row.name);
    const ticks = (r && r.c) || {};
    const steps = row.def.steps || [];
    return { done: steps.filter((s) => ticks[stepKey(s)]).length, total: steps.length };
  }

  function counts() {
    const c = { untested: 0, working: 0, partial: 0, off: 0, bug: 0, blocked: 0 };
    let total = 0;
    state.rows.forEach((row) => { if (row.listed) { c[statusOf(row.name)]++; total++; } });
    return { c, total, tested: total - c.untested };
  }

  function searchHaystack(row) {
    const parts = [row.name, groupLabel(row.group)];
    if (row.listed) {
      parts.push(row.def.does || "");
      (row.def.steps || []).forEach((s) => parts.push(stepText(s), s.expect || ""));
      const r = rec(row.name);
      if (r && r.n) parts.push(r.n);
    }
    return parts.join("\n").toLowerCase();
  }

  function visibleRows() {
    const q = state.search.trim().toLowerCase();
    return state.rows.filter((row) => {
      if (state.filter === "nolist") { if (row.listed) return false; }
      else if (state.filter !== "all") { if (!row.listed || statusOf(row.name) !== state.filter) return false; }
      if (state.group && row.group !== state.group) return false;
      if (q && !searchHaystack(row).includes(q)) return false;
      return true;
    });
  }
  const filtering = () => !!(state.search.trim() || state.filter !== "all" || state.group);

  /* ---- DOM: built once on first open ----------------------------------- */

  let dom = null;
  function grabDom() {
    const overlay = $("#tc-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#tc-status-line"),
      search: $("#tc-search"),
      chips: $("#tc-chips"),
      groupSel: $("#tc-group-filter"),
      list: $("#tc-list"),
      count: $("#tc-count"),
      detail: $("#tc-detail"),
      side: $("#tc-side"),
      file: $("#tc-file"),
      refresh: $("#btn-tc-refresh"),
    };
  }

  /* ---- status line ------------------------------------------------------ */

  function renderStatusLine() {
    if (!dom) return;
    const { c, total, tested } = counts();
    const attention = c.bug + c.off + c.partial;
    const unlisted = state.rows.filter((r) => !r.listed).length;
    const bits = [`${total} tools`, `${tested} tested`, `${attention} need attention`];
    if (unlisted) bits.push(`${unlisted} without a checklist`);
    bits.push(storageOk ? "results stay in this browser" : "\u26A0 browser storage blocked \u2014 results won't survive a reload");
    dom.statusLine.textContent = state.liveBusy ? `reading live tool catalogue\u2026  \u00B7  ${bits.slice(0, 3).join(" \u00B7 ")}` : bits.join("  \u00B7  ");
    dom.statusLine.classList.toggle("is-busy", state.liveBusy);
    dom.statusLine.classList.toggle("is-error", !storageOk);
  }

  /* ---- left pane: filters + list --------------------------------------- */

  function renderChips() {
    const { c } = counts();
    const unlisted = state.rows.filter((r) => !r.listed).length;
    const totalListed = state.rows.filter((r) => r.listed).length;
    dom.chips.textContent = "";
    const mk = (id, label, n, statusForColour) => {
      const active = state.filter === id;
      const chip = el("button", {
        class: "tc-chip" + (active ? " is-active" : ""), type: "button",
        "data-status": statusForColour || null, "aria-pressed": active ? "true" : "false",
        onclick: () => { state.filter = active ? "all" : id; renderChips(); renderList(); renderOverview(); },
      }, [
        statusForColour ? el("span", { class: "tc-chip__dot" }) : null,
        label,
        el("span", { class: "tc-chip__n" }, String(n)),
      ]);
      dom.chips.appendChild(chip);
    };
    mk("all", "All", totalListed + unlisted);
    STATUSES.forEach((s) => mk(s.id, s.short, c[s.id], s.id));
    if (unlisted) mk("nolist", "No checklist", unlisted, "nolist");
  }

  function renderGroupSelect() {
    dom.groupSel.textContent = "";
    dom.groupSel.appendChild(el("option", { value: "" }, "All categories"));
    ((DATA && DATA.groups) || []).forEach((g) => {
      if (state.rows.some((r) => r.group === g.id)) dom.groupSel.appendChild(el("option", { value: g.id }, g.label));
    });
    if (state.rows.some((r) => r.group === "__nolist")) dom.groupSel.appendChild(el("option", { value: "__nolist" }, "No checklist yet"));
    dom.groupSel.value = state.group;
  }

  function statusBar(rows, extraClass) {
    const c = { untested: 0, working: 0, partial: 0, off: 0, bug: 0, blocked: 0 };
    rows.forEach((r) => { c[statusOf(r.name)]++; });
    const total = rows.length || 1;
    return el("div", { class: "tc-bar " + (extraClass || ""), "aria-hidden": "true" },
      STATUSES.filter((s) => s.id !== "untested" && c[s.id]).map((s) =>
        el("i", { "data-status": s.id, style: `width:${(c[s.id] / total * 100).toFixed(2)}%` })));
  }

  function renderCard(row) {
    const active = state.selected === row.name;
    if (!row.listed) {
      return el("button", {
        class: "tc-card tc-card--nolist" + (active ? " is-active" : ""), type: "button",
        "data-status": "nolist", "data-tool": row.name, onclick: () => select(row.name),
      }, [
        el("div", { class: "tc-card__row" }, [
          el("span", { class: "tc-card__name" }, row.name),
          el("span", { class: "tc-tag tc-tag--warn" }, "No checklist"),
        ]),
      ]);
    }
    const s = statusOf(row.name);
    const prog = stepProgress(row);
    const live = row.live;
    return el("button", {
      class: "tc-card" + (active ? " is-active" : ""), type: "button",
      "data-status": s, "data-tool": row.name, onclick: () => select(row.name),
      title: row.def.does || row.name,
    }, [
      el("div", { class: "tc-card__row" }, [
        el("span", { class: "tc-card__glyph", "aria-hidden": "true" }, STATUS[s].glyph),
        el("span", { class: "tc-card__name" }, row.name),
      ]),
      el("div", { class: "tc-card__meta" }, [
        el("span", { class: "tc-card__steps" + (prog.total && prog.done === prog.total ? " is-done" : "") },
          `${prog.done}/${prog.total} steps`),
        row.def.os ? el("span", { class: "tc-tag" }, "Windows") : null,
        row.def.care ? el("span", { class: "tc-tag tc-tag--warn", title: row.def.care }, "Careful") : null,
        live && live.confirm_required ? el("span", { class: "tc-tag tc-tag--info" }, "Confirms") : null,
        row.stale ? el("span", { class: "tc-tag tc-tag--bad", title: "Not in the live CLI catalogue" }, "Not in CLI") : null,
        s === "untested" && row.def.needs && row.def.needs.length ? el("span", { class: "tc-tag", title: row.def.needs.join(" \u00B7 ") }, "Setup") : null,
      ]),
    ]);
  }

  function renderList() {
    if (!dom) return;
    const keepScroll = dom.list.scrollTop;
    const rows = visibleRows();
    dom.count.textContent = filtering() ? `${rows.length} of ${state.rows.length}` : String(state.rows.length);
    dom.list.textContent = "";
    if (!rows.length) {
      dom.list.appendChild(el("div", { class: "debug-empty" }, state.rows.length ? "Nothing matches these filters." : "No tools to show."));
      return;
    }
    const open = filtering();
    const byGroup = new Map();
    rows.forEach((r) => { if (!byGroup.has(r.group)) byGroup.set(r.group, []); byGroup.get(r.group).push(r); });
    const ordered = [...groupIndex().keys(), "__nolist"].filter((g) => byGroup.has(g));
    ordered.forEach((gid) => {
      const shown = byGroup.get(gid);
      const all = state.rows.filter((r) => r.group === gid);
      const collapsed = !open && state.collapsed.has(gid);
      const listedAll = all.filter((r) => r.listed);
      const tested = listedAll.filter((r) => statusOf(r.name) !== "untested").length;
      const section = el("div", { class: "tc-group" + (collapsed ? " is-collapsed" : "") }, [
        el("button", {
          class: "tc-group__head", type: "button", "aria-expanded": collapsed ? "false" : "true",
          onclick: () => {
            if (state.collapsed.has(gid)) state.collapsed.delete(gid); else state.collapsed.add(gid);
            store.ui.collapsed = [...state.collapsed];
            persist();
            renderList();
          },
        }, [
          el("span", { class: "tc-group__caret", "aria-hidden": "true" }),
          el("span", { class: "tc-group__label" }, groupLabel(gid)),
          el("span", { class: "tc-group__count" }, gid === "__nolist" ? String(all.length) : `${tested}/${listedAll.length}`),
          gid === "__nolist" ? null : el("div", { class: "tc-group__bar" }, statusBar(listedAll)),
        ]),
      ].concat(shown.map(renderCard)));
      dom.list.appendChild(section);
    });
    dom.list.scrollTop = keepScroll;
  }

  /* ---- middle pane: detail --------------------------------------------- */

  function withPlaceholders(text) {
    const out = [];
    const re = /<[^<>\n]{2,60}>/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      out.push(el("span", { class: "tc-ph", title: "Replace this before you run it" }, m[0]));
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function renderVerdict(row) {
    const cur = statusOf(row.name);
    const r = rec(row.name);
    const prog = stepProgress(row);
    const group = el("div", { class: "tc-verdict", role: "radiogroup", "aria-label": "Verdict" },
      STATUSES.map((s) => el("button", {
        class: "tc-verdict__btn", type: "button", role: "radio", "data-status": s.id,
        "aria-checked": cur === s.id ? "true" : "false", title: `${s.label} \u2014 ${s.hint}  (key ${s.key})`,
        onclick: () => { setStatus(row.name, s.id); renderDetail(true); refreshDerived(); },
      }, [
        el("span", { class: "tc-verdict__glyph", "aria-hidden": "true" }, s.glyph),
        el("span", { class: "tc-verdict__label" }, s.short),
        el("span", { class: "tc-verdict__key" }, s.key),
      ])));
    const nudge = cur === "untested" && prog.total && prog.done === prog.total
      ? "  \u00B7  every step ticked \u2014 pick a verdict" : "";
    return el("div", { id: "tc-verdict-wrap" }, [
      el("h4", { class: "tc-section-title" }, "Verdict"),
      group,
      el("div", { class: "tc-verdict__when" }, `last touched ${fmtWhen(r && r.t)}${nudge}`),
    ]);
  }

  function renderStep(row, step, i) {
    const key = stepKey(step);
    const isAsk = typeof step.ask === "string";
    const text = stepText(step);
    const ticked = !!((rec(row.name) || {}).c || {})[key];
    const box = el("button", {
      class: "tc-check", type: "button", role: "checkbox", "aria-checked": ticked ? "true" : "false",
      "aria-label": `Step ${i + 1} done`,
      onclick: () => {
        const r = ensure(row.name);
        r.c = r.c || {};
        if (r.c[key]) delete r.c[key]; else r.c[key] = 1;
        r.t = Date.now();
        persist();
        renderDetail(true);
        refreshDerived();
      },
    });
    return el("div", { class: "tc-step" + (ticked ? " is-done" : ""), "data-step": key }, [
      box,
      el("div", null, [
        el("div", { class: "tc-step__head" }, [
          el("span", { class: "tc-step__kind" + (isAsk ? "" : " tc-step__kind--run") }, isAsk ? "ASK" : "RUN"),
          el("span", { class: "tc-step__where" }, isAsk ? `Step ${i + 1} \u2014 type this in Ask Jarvis` : `Step ${i + 1} \u2014 Debug \u2192 fill in these arguments \u2192 RUN (skips the model)`),
        ]),
        el("pre", { class: "tc-step__code" }, isAsk ? withPlaceholders(text) : withPlaceholders(text === "{}" ? "{}   (no arguments)" : text)),
        step.expect ? el("div", { class: "tc-step__expect" }, [el("b", null, "Pass"), step.expect]) : null,
        el("div", { class: "tc-step__actions" }, [
          el("button", { class: "btn btn--ghost btn--sm", type: "button",
            onclick: async () => toast((await copyText(text)) ? (isAsk ? "Prompt copied" : "Arguments copied") : "Couldn't copy", "info") }, isAsk ? "Copy prompt" : "Copy args"),
          isAsk
            ? el("button", { class: "btn btn--outline btn--sm", type: "button", onclick: () => sendToAsk(text) }, "Send to Ask")
            : el("button", { class: "btn btn--outline btn--sm", type: "button", onclick: () => openInDebug(row.name) }, "Open in Debug"),
        ]),
      ]),
    ]);
  }

  function renderSchema(row) {
    const live = row.live;
    if (!live) return null;
    const props = (live.parameters && live.parameters.properties) || {};
    const req = new Set((live.parameters && live.parameters.required) || []);
    const names = Object.keys(props);
    const table = names.length
      ? el("table", { class: "tc-params" }, el("tbody", null, names.map((n) => {
          const p = props[n] || {};
          const type = p.enum ? p.enum.join(" | ") : Array.isArray(p.type) ? p.type.join(" | ") : (p.type || "any");
          return el("tr", null, [
            el("td", null, [n, req.has(n) ? el("span", { class: "tc-req" }, "required") : null]),
            el("td", null, [el("div", { style: "font-family:var(--font-mono);font-size:10.5px;color:var(--text-dimmer)" }, type), p.description ? String(p.description).slice(0, 220) : ""]),
          ]);
        })))
      : el("div", { class: "tc-note" }, "Takes no arguments.");
    return el("details", { class: "tc-schema" }, [
      el("summary", null, "Live schema from the CLI"),
      el("div", { class: "tc-schema__body" }, [
        live.description ? el("div", { class: "tc-schema__desc" }, live.description) : null,
        table,
      ]),
    ]);
  }

  function renderDetail(keepScroll) {
    if (!dom) return;
    const scroll = keepScroll ? dom.detail.scrollTop : 0;
    dom.detail.textContent = "";
    const row = state.rows.find((r) => r.name === state.selected);
    if (!row) {
      dom.detail.appendChild(el("div", { class: "tc-empty" }, "Select a tool on the left."));
      return;
    }

    if (!row.listed) {
      dom.detail.appendChild(el("div", { class: "tc-nolist-card" }, [
        el("div", { class: "tc-title-row" }, [
          el("div", { class: "tc-title" }, row.name),
          el("span", { class: "tc-tag tc-tag--warn" }, "No checklist"),
        ]),
        el("p", null, "This tool exists in the CLI but has no checklist entry yet, so there is nothing to show beyond its name."),
        el("p", null, ["A tool that ships with jarvis gets its entry in ", el("code", null, "web/public/test-checklist-data.js"), " \u2014 see AGENTS.md \u2192 Test Checklist. A tool you wrote yourself (Menu \u2192 Custom Tools) carries its own: add a ", el("code", null, "TEST_CHECKLIST"), " dict to its file, and use the editor's Check button to see whether it was accepted."]),
        el("div", { class: "tc-step__actions", style: "margin-top:14px" }, [
          el("button", { class: "btn btn--outline btn--sm", type: "button", onclick: () => openInDebug(row.name) }, "Open in Debug"),
          el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: async () => toast((await copyText(row.name)) ? "Name copied" : "Couldn't copy") }, "Copy name"),
        ]),
      ]));
      renderFoot();
      return;
    }

    const def = row.def;
    const live = row.live;
    const badges = [
      el("span", { class: "tc-tag" }, groupLabel(row.group)),
      def.os ? el("span", { class: "tc-tag" }, "Windows only") : null,
      row.supplied ? el("span", { class: "tc-tag", title: "This entry is defined in the tool's own file (TEST_CHECKLIST), not in test-checklist-data.js" }, "From the tool's own file") : null,
      live && live.confirm_required ? el("span", { class: "tc-tag tc-tag--info", title: "Pauses for Yes/No before running (toggle in Debug)" }, "Confirms first") : null,
      live && live.ai_review ? el("span", { class: "tc-tag tc-tag--info", title: "A second AI reviews the call for risk" }, "AI review") : null,
      row.stale ? el("span", { class: "tc-tag tc-tag--bad", title: "The live CLI catalogue no longer has a tool with this name" }, "Not in CLI \u2014 renamed or removed?") : null,
    ];

    const nodes = [];
    nodes.push(el("div", null, [
      el("div", { class: "tc-title-row" }, [
        el("div", { class: "tc-title" }, row.name),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: async () => toast((await copyText(row.name)) ? "Name copied" : "Couldn't copy") }, "Copy name"),
      ]),
      el("div", { class: "tc-does" }, def.does || ""),
      el("div", { class: "tc-badges" }, badges),
    ]));
    nodes.push(renderVerdict(row));
    if (def.care) nodes.push(el("div", { class: "tc-alert", role: "note" }, [el("div", { class: "tc-alert__label" }, "Careful"), el("div", { class: "tc-alert__text" }, def.care)]));

    const needs = def.needs || [], watch = def.watch || [];
    if (needs.length || watch.length) {
      nodes.push(el("div", { class: "tc-facts" + (needs.length && watch.length ? "" : " tc-facts--one") }, [
        needs.length ? el("div", null, [el("h4", { class: "tc-section-title" }, "Needs"), el("ul", { class: "tc-list-plain" }, needs.map((n) => el("li", null, n)))]) : null,
        watch.length ? el("div", null, [el("h4", { class: "tc-section-title" }, "Watch for"), el("ul", { class: "tc-list-plain tc-list-plain--watch" }, watch.map((n) => el("li", null, n)))]) : null,
      ]));
    }

    const prog = stepProgress(row);
    nodes.push(el("div", null, [
      el("h4", { class: "tc-section-title" }, ["How to test", el("span", { class: "tc-section-title__aside" }, `${prog.done}/${prog.total} ticked`)]),
      ...(def.steps || []).map((s, i) => renderStep(row, s, i)),
    ]));

    const r = rec(row.name);
    const notes = el("textarea", {
      class: "tc-notes", id: "tc-notes", spellcheck: "false", rows: "4",
      placeholder: "What you saw \u2014 exact error text, which provider, what you expected instead\u2026",
      "aria-label": "Notes",
    });
    notes.value = (r && r.n) || "";
    const noteFoot = el("span", null, `${notes.value.length} chars`);
    notes.addEventListener("input", () => {
      const rr = ensure(row.name);
      rr.n = notes.value;
      if (!rr.n) delete rr.n;
      rr.t = Date.now();
      noteFoot.textContent = `${notes.value.length} chars`;
      persistSoon();
      refreshDerivedSoon();
    });
    notes.addEventListener("blur", flushPending);
    nodes.push(el("div", null, [
      el("h4", { class: "tc-section-title" }, "Notes"),
      notes,
      el("div", { class: "tc-notes__foot" }, [noteFoot, el("span", null, "saved in this browser as you type")]),
    ]));

    if (r && r.h && r.h.length) {
      nodes.push(el("div", null, [
        el("h4", { class: "tc-section-title" }, "History"),
        el("div", { class: "tc-history" }, r.h.map((h) => el("div", { class: "tc-history__row", "data-status": h.s }, [
          el("span", { class: "tc-chip__dot" }),
          el("span", null, fmtWhen(h.t)),
          el("b", null, (STATUS[h.s] || STATUS.untested).label),
        ]))),
      ]));
    }

    const schema = renderSchema(row);
    if (schema) nodes.push(schema);

    nodes.push(el("div", null, [
      el("button", {
        class: "btn btn--ghost btn--sm btn--danger", type: "button",
        onclick: async () => {
          const ok = await confirmDialog({ title: `Clear results for ${row.name}?`, body: "Removes its verdict, ticks, notes and history from this browser.", confirmLabel: "Clear", level: "warn" });
          if (!ok) return;
          delete store.tools[row.name];
          persist();
          renderDetail();
          refreshDerived();
        },
      }, "Clear this tool's results"),
    ]));

    nodes.forEach((n) => dom.detail.appendChild(n));
    renderFoot();
    dom.detail.scrollTop = scroll;
  }

  function renderFoot() {
    // Static hint bar lives in the markup; nothing to rebuild.
  }

  /* ---- right pane: overview -------------------------------------------- */

  function renderOverview() {
    if (!dom) return;
    const keepScroll = dom.side.scrollTop;
    const { c, total, tested } = counts();
    const pct = total ? tested / total : 0;
    const circ = 2 * Math.PI * 42;
    const listed = state.rows.filter((r) => r.listed);
    const unlisted = state.rows.filter((r) => !r.listed);
    const stale = state.rows.filter((r) => r.stale);
    const passRate = tested ? Math.round((c.working / tested) * 100) : 0;
    dom.side.textContent = "";

    // progress
    // SVG needs its own namespace — el() makes HTML elements, which would
    // silently render nothing here.
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "tc-ring");
    svg.setAttribute("viewBox", "0 0 100 100");
    svg.setAttribute("aria-hidden", "true");
    const ring = el("div", { class: "tc-ring-label" }, [
      svg,
      el("div", { class: "tc-ring__pct" }, [`${Math.round(pct * 100)}%`, el("small", null, "tested")]),
    ]);
    const mkc = (cls, extra) => {
      const node = document.createElementNS(ns, "circle");
      node.setAttribute("cx", "50"); node.setAttribute("cy", "50"); node.setAttribute("r", "42");
      node.setAttribute("class", cls);
      Object.entries(extra || {}).forEach(([k, v]) => node.setAttribute(k, v));
      return node;
    };
    svg.appendChild(mkc("tc-ring__track"));
    svg.appendChild(mkc("tc-ring__val", { "stroke-dasharray": circ.toFixed(2), "stroke-dashoffset": (circ * (1 - pct)).toFixed(2) }));
    dom.side.appendChild(el("div", { class: "tc-ring-wrap" }, [
      ring,
      el("div", { class: "tc-ring-side" }, [
        statusBar(listed),
        el("div", { class: "tc-ring-side__line" }, tested
          ? `${c.working} of ${tested} tested are fully working (${passRate}%).`
          : "Nothing tested yet. Pick a tool and start ticking."),
      ]),
    ]));

    // legend / filter
    dom.side.appendChild(el("div", null, [
      el("h4", { class: "tc-section-title" }, "By verdict"),
      el("div", { class: "tc-legend" }, STATUSES.map((s) => el("button", {
        class: "tc-legend__row" + (state.filter === s.id ? " is-active" : ""), type: "button", "data-status": s.id,
        onclick: () => { state.filter = state.filter === s.id ? "all" : s.id; renderChips(); renderList(); renderOverview(); },
        title: `Show only: ${s.label}`,
      }, [
        el("span", { class: "tc-legend__glyph", "aria-hidden": "true" }, s.glyph),
        el("span", null, s.label),
        el("span", { class: "tc-legend__n" }, String(c[s.id])),
        el("span", { class: "tc-legend__hint" }, s.hint),
      ]))),
    ]));

    // needs attention
    const attn = [];
    ATTENTION_ORDER.forEach((sid) => listed.forEach((row) => { if (statusOf(row.name) === sid) attn.push(row); }));
    if (attn.length) {
      const shown = attn.slice(0, 10);
      dom.side.appendChild(el("div", null, [
        el("h4", { class: "tc-section-title" }, ["Needs attention", el("span", { class: "tc-section-title__aside" }, String(attn.length))]),
        el("div", { class: "tc-attn" }, shown.map((row) => {
          const s = statusOf(row.name);
          const note = ((rec(row.name) || {}).n || "").replace(/\s+/g, " ").trim();
          return el("button", { class: "tc-attn__row", type: "button", "data-status": s, onclick: () => select(row.name, true) }, [
            el("span", { class: "tc-attn__glyph", "aria-hidden": "true" }, STATUS[s].glyph),
            el("span", { class: "tc-attn__name" }, row.name),
            note ? el("span", { class: "tc-attn__note" }, note) : null,
          ]);
        })),
        attn.length > shown.length ? el("div", { class: "tc-more" }, `+ ${attn.length - shown.length} more \u2014 use the verdict filters above`) : null,
      ]));
    }

    // by category
    const groups = ((DATA && DATA.groups) || []).map((g) => ({ g, rows: listed.filter((r) => r.group === g.id) })).filter((x) => x.rows.length);
    dom.side.appendChild(el("div", null, [
      el("h4", { class: "tc-section-title" }, "By category"),
      el("div", { class: "tc-mini" }, groups.map(({ g, rows }) => {
        const done = rows.filter((r) => statusOf(r.name) !== "untested").length;
        return el("button", {
          class: "tc-mini__row", type: "button", title: `Show only ${g.label}`,
          onclick: () => { state.group = state.group === g.id ? "" : g.id; dom.groupSel.value = state.group; renderList(); },
        }, [
          el("span", { class: "tc-mini__label" }, g.label),
          el("span", { class: "tc-mini__n" }, `${done}/${rows.length}`),
          el("div", { class: "tc-mini__bar" }, statusBar(rows)),
        ]);
      })),
    ]));

    // coverage (the AGENTS.md rule, made visible)
    if (state.live) {
      const lines = [];
      if (unlisted.length) lines.push(el("div", { class: "tc-note is-warn" }, `${unlisted.length} tool${unlisted.length === 1 ? "" : "s"} in the CLI ${unlisted.length === 1 ? "has" : "have"} no checklist entry (shown by name only): ${unlisted.slice(0, 6).map((r) => r.name).join(", ")}${unlisted.length > 6 ? "\u2026" : ""}`));
      if (stale.length) lines.push(el("div", { class: "tc-note is-warn" }, `${stale.length} checklist entr${stale.length === 1 ? "y matches" : "ies match"} no tool in the CLI (renamed or removed?): ${stale.slice(0, 6).map((r) => r.name).join(", ")}${stale.length > 6 ? "\u2026" : ""}`));
      if (!lines.length) lines.push(el("div", { class: "tc-note" }, "Every tool in the CLI has a checklist entry, and every entry matches a tool."));
      dom.side.appendChild(el("div", null, [el("h4", { class: "tc-section-title" }, "Coverage"), ...lines]));
    } else {
      dom.side.appendChild(el("div", null, [
        el("h4", { class: "tc-section-title" }, "Coverage"),
        el("div", { class: "tc-note" }, state.liveBusy ? "Reading the live catalogue\u2026" : `Live catalogue unavailable${state.liveError ? " (" + state.liveError + ")" : ""} \u2014 showing the checklist data only, so missing entries can't be detected right now.`),
      ]));
    }

    // actions
    dom.side.appendChild(el("div", null, [
      el("h4", { class: "tc-section-title" }, "Results"),
      el("div", { class: "tc-actions" }, [
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: exportResults }, "Export JSON"),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => dom.file.click() }, "Import JSON"),
        el("button", { class: "btn btn--outline btn--sm btn--wide", type: "button", onclick: copyReport }, "Copy report (Markdown)"),
        el("button", { class: "btn btn--ghost btn--sm btn--danger btn--wide", type: "button", onclick: resetAll }, "Reset all results"),
      ]),
      el("div", { class: "tc-note", style: "margin-top:8px" }, storageOk
        ? "Stored only in this browser (localStorage). Nothing is sent to the server or written by the CLI. Export to move results between browsers or keep a backup."
        : "Browser storage is blocked, so results only last until you close or reload this tab. Export before leaving."),
    ]));

    dom.side.appendChild(renderGuide());
    dom.side.scrollTop = keepScroll;
  }

  function renderGuide() {
    const p = (...c) => el("p", null, c);
    return el("details", { class: "tc-guide" }, [
      el("summary", null, "How to test"),
      el("div", { class: "tc-guide__body" }, [
        p(el("b", null, "Ask path. "), "Type the prompt in Ask Jarvis. This tests the whole chain \u2014 the model has to pick the tool and fill the arguments. Then open Logs and check the tool_call and tool_result rows to see what was really sent and returned."),
        p(el("b", null, "Debug path. "), "Run the tool directly with your own arguments. It skips the model, so it separates \u201Cthe tool is broken\u201D from \u201Cthe model never called it\u201D."),
        p(el("b", null, "Tool never called? "), "The router only sends the model the tools it thinks are relevant to the message. Check the request in Logs to see whether it was offered; the model can also find it with search_tools."),
        p(el("b", null, "Confirmations. "), "\u201CConfirms first\u201D tools pause for Yes/No before running. That is expected \u2014 unless you switched the toggle off in Debug. Decline anything you didn't mean to run."),
        p(el("b", null, "Verdicts. "), "Working: exactly right. Partial: works but flaky or incomplete. Not as intended: runs, wrong behaviour. Bug: errors or wrong results. Blocked: can't test yet (setup, account, hardware)."),
        p(el("b", null, "Keys. "), "Up/Down move between tools, 0\u20135 set the verdict, / searches, Esc closes."),
      ]),
    ]);
  }

  /* ---- selection, derived refresh -------------------------------------- */

  function refreshDerived() {
    renderStatusLine();
    renderChips();
    renderList();
    renderOverview();
  }
  let derivedTimer = null;
  function refreshDerivedSoon() {
    clearTimeout(derivedTimer);
    derivedTimer = setTimeout(refreshDerived, 350);
  }

  function select(name, reveal) {
    flushPending();
    state.selected = name;
    store.ui.selected = name;
    if (reveal) {
      // Jumping from the overview must be able to show a tool that the
      // current filters or a collapsed group would otherwise hide.
      state.filter = "all"; state.search = ""; state.group = "";
      dom.search.value = ""; dom.groupSel.value = "";
      const row = state.rows.find((r) => r.name === name);
      if (row) state.collapsed.delete(row.group);
      renderChips();
    }
    persist();
    renderList();
    renderDetail();
    const card = dom.list.querySelector(`[data-tool="${CSS && CSS.escape ? CSS.escape(name) : name}"]`);
    if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
  }

  function navRows() {
    const open = filtering();
    return visibleRows().filter((r) => open || !state.collapsed.has(r.group));
  }

  /* ---- actions: hand off to Ask / Debug -------------------------------- */

  function sendToAsk(text) {
    const input = $("#ask-input");
    const openBtn = $("#btn-ask-jarvis");
    if (!input || !openBtn) {
      copyText(text);
      toast("Couldn't find the Ask panel \u2014 the prompt was copied instead.", "warn");
      return;
    }
    close();
    openBtn.click(); // openAsk() — also focuses the input
    input.value = text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    try { input.setSelectionRange(text.length, text.length); } catch (_) { /* not supported on this input */ }
    if (PLACEHOLDER_RE.test(text)) toast("Replace the <placeholders> in the prompt before you send it.", "warn");
    else toast("Prompt loaded \u2014 press Send. Reopen Test Checklist from the Menu to record the result.", "info");
  }

  function openInDebug(name) {
    const item = $("#menu-item-debug");
    if (!item) { toast("Couldn't find the Debug panel.", "warn"); return; }
    close();
    item.click(); // closePanelMenu(); openDebug()
    const search = $("#debug-search");
    if (search) {
      search.value = name;
      search.dispatchEvent(new Event("input", { bubbles: true }));
    }
    // Debug loads its tool list asynchronously; select the card once it exists.
    let tries = 0;
    const timer = setInterval(() => {
      const hit = Array.from(document.querySelectorAll("#debug-tool-list .debug-tool-card"))
        .find((c) => { const n = c.querySelector(".debug-tool-card__name"); return n && n.textContent.trim() === name; });
      if (hit) { clearInterval(timer); hit.click(); }
      else if (++tries > 50) clearInterval(timer);
    }, 100);
    toast("Debug opened on " + name + ". Reopen Test Checklist from the Menu to record the result.", "info");
  }

  /* ---- export / import / report / reset -------------------------------- */

  function exportResults() {
    flushPending();
    const payload = {
      app: "jarvis-test-checklist", v: 1,
      exportedAt: new Date().toISOString(),
      catalogueUpdated: DATA && DATA.updated,
      tools: store.tools,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: `jarvis-test-checklist-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast(`Exported ${Object.keys(store.tools).length} tool records.`, "success");
  }

  async function importResults(file) {
    if (!file) return;
    let parsed;
    try { parsed = JSON.parse(await file.text()); } catch (_) { toast("That file isn't valid JSON.", "error"); return; }
    if (!parsed || parsed.v !== 1 || !parsed.tools || typeof parsed.tools !== "object") {
      toast("That doesn't look like a Test Checklist export.", "error");
      return;
    }
    const names = Object.keys(parsed.tools);
    const ok = await confirmDialog({
      title: `Import ${names.length} tool record${names.length === 1 ? "" : "s"}?`,
      body: "Merged into what's here: for a tool that already has results, whichever was touched most recently wins.",
      confirmLabel: "Import",
    });
    if (!ok) return;
    let took = 0;
    names.forEach((name) => {
      const incoming = parsed.tools[name];
      if (!incoming || typeof incoming !== "object") return;
      const mine = rec(name);
      if (!mine || (incoming.t || 0) > (mine.t || 0)) { store.tools[name] = incoming; took++; }
    });
    persist();
    refreshDerived();
    renderDetail(true);
    toast(`Imported ${took} record${took === 1 ? "" : "s"} (${names.length - took} kept as they were).`, "success");
  }

  function buildReport() {
    const { c, total, tested } = counts();
    const listed = state.rows.filter((r) => r.listed);
    const stamp = new Date().toISOString().slice(0, 10);
    const line = (row) => {
      const r = rec(row.name) || {};
      const prog = stepProgress(row);
      const note = (r.n || "").trim().replace(/\r?\n+/g, " / ");
      return `- \`${row.name}\` \u2014 ${prog.done}/${prog.total} steps${note ? " \u2014 " + note : ""}`;
    };
    const out = [
      `# Jarvis test checklist \u2014 ${stamp}`,
      "",
      `${total} tools in the checklist \u00B7 ${tested} tested (${total ? Math.round(tested / total * 100) : 0}%) \u00B7 catalogue updated ${(DATA && DATA.updated) || "?"}`,
      "",
      "| Verdict | Tools |",
      "|---|---|",
      ...STATUSES.map((s) => `| ${s.label} | ${c[s.id]} |`),
      "",
    ];
    ["bug", "off", "partial", "blocked", "working"].forEach((sid) => {
      const rows = listed.filter((r) => statusOf(r.name) === sid);
      if (!rows.length) return;
      out.push(`## ${STATUS[sid].label} (${rows.length})`, "", ...rows.map(line), "");
    });
    const untested = listed.filter((r) => statusOf(r.name) === "untested");
    if (untested.length) out.push(`## Untested (${untested.length})`, "", untested.map((r) => "`" + r.name + "`").join(", "), "");
    const unlisted = state.rows.filter((r) => !r.listed);
    if (unlisted.length) out.push(`## No checklist entry (${unlisted.length})`, "", unlisted.map((r) => "`" + r.name + "`").join(", "), "");
    return out.join("\n");
  }
  async function copyReport() {
    flushPending();
    toast((await copyText(buildReport())) ? "Report copied as Markdown." : "Couldn't copy the report.", "info");
  }

  async function resetAll() {
    const n = Object.keys(store.tools).length;
    if (!n) { toast("There are no results to reset.", "info"); return; }
    const ok = await confirmDialog({
      title: "Reset all results?",
      body: `This wipes verdicts, ticks, notes and history for ${n} tool${n === 1 ? "" : "s"} from this browser. Export first if you want a backup.`,
      confirmLabel: "Reset everything", level: "error",
    });
    if (!ok) return;
    store.tools = {};
    persist();
    refreshDerived();
    renderDetail();
    toast("All results cleared.", "info");
  }

  /* ---- live catalogue (read-only) -------------------------------------- */

  async function loadLive() {
    if (state.liveBusy) return;
    state.liveBusy = true;
    state.liveError = "";
    if (dom) { dom.refresh.disabled = true; renderStatusLine(); renderOverview(); }
    try {
      const res = await fetch("/api/tools");
      let body = null;
      try { body = await res.json(); } catch (_) { /* non-JSON error page */ }
      if (!res.ok) throw new Error((body && body.error) || res.statusText || "CLI offline");
      if (!Array.isArray(body)) throw new Error("unexpected response");
      state.live = new Map(body.filter((t) => t && t.name).map((t) => [t.name, t]));
      if (SHIPPED && SHIPPED.tools) {
        try { const m = mergeCatalogue(SHIPPED, body); DATA = m.data; state.supplied = m.from; }
        catch (_) { DATA = SHIPPED; state.supplied = new Set(); }
      }
    } catch (e) {
      state.live = null;
      // No live read, no supplied entries: fall back to the shipped file alone.
      DATA = SHIPPED;
      state.supplied = new Set();
      state.liveError = String(e && e.message ? e.message : e).slice(0, 80);
    } finally {
      state.liveBusy = false;
      if (dom) dom.refresh.disabled = false;
    }
    if (!dom || dom.overlay.hidden) return;
    buildRows();
    renderGroupSelect();
    refreshDerived();
    renderDetail(true);
  }

  /* ---- open / close / keyboard ----------------------------------------- */

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") {
      if (t === dom.search && dom.search.value) {
        state.search = ""; dom.search.value = ""; renderList(); return;
      }
      close();
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const rows = navRows();
      if (!rows.length) return;
      e.preventDefault();
      const i = rows.findIndex((r) => r.name === state.selected);
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next && next.name !== state.selected) select(next.name);
      return;
    }
    const s = STATUSES.find((x) => x.key === e.key);
    if (s) {
      const row = state.rows.find((r) => r.name === state.selected);
      if (row && row.listed) { e.preventDefault(); setStatus(row.name, s.id); renderDetail(true); refreshDerived(); }
    }
  }

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;
    store = loadStore();
    state.collapsed = new Set(Array.isArray(store.ui.collapsed) ? store.ui.collapsed : []);

    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderList(); });
    dom.groupSel.addEventListener("change", () => { state.group = dom.groupSel.value; renderList(); });
    dom.file.addEventListener("change", async () => { const f = dom.file.files && dom.file.files[0]; dom.file.value = ""; await importResults(f); });
    dom.refresh.addEventListener("click", loadLive);
    $("#tc-close").addEventListener("click", close);
    dom.overlay.addEventListener("click", (e) => { if (e.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);
    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) {
      toast("Test Checklist markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;

    if (!SHIPPED || !SHIPPED.tools) {
      dom.statusLine.textContent = "test-checklist-data.js didn't load";
      dom.statusLine.classList.add("is-error");
      dom.detail.textContent = "";
      dom.detail.appendChild(el("div", { class: "tc-empty" }, ["The checklist data file didn't load, so there is nothing to show. Check that ", el("b", null, "test-checklist-data.js"), " exists next to index.html and is loaded before test-checklist.js."]));
      return;
    }

    buildRows();
    if (!state.selected || !state.rows.some((r) => r.name === state.selected)) {
      const remembered = store.ui.selected;
      state.selected = state.rows.some((r) => r.name === remembered) ? remembered : (state.rows[0] && state.rows[0].name) || null;
    }
    renderGroupSelect();
    refreshDerived();
    renderDetail();
    const card = dom.list.querySelector(".tc-card.is-active");
    if (card && card.scrollIntoView) card.scrollIntoView({ block: "center" });
    dom.search.focus({ preventScroll: true });
    loadLive();
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    flushPending();
    dom.overlay.hidden = true;
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  // catalogue(): the merged view the panel renders (shipped + supplied).
  // _mergeCatalogue is exposed for tests/test_checklist_supplied.py only.
  global.JarvisTestChecklist = { open, close, catalogue: () => DATA, _mergeCatalogue: mergeCatalogue };
})(window);
