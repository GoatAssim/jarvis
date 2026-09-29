/* ============================================================================
 * logsearch.js — Menu → Log Search.
 *
 * Part H.1 rework. Greps the raw log FILES on disk (see log_files.py's
 * module docstring for how this differs from the parsed-conversation search
 * elsewhere in the app) — a line that failed to parse, a daemon's console
 * traceback, or any file the user points it at.
 *
 * Deliberately standalone, the same way daemons.js/backlog.js are: its own
 * tiny $/el/api/toast helpers, no reach into app.js's internals — except for
 * two narrow, intentional exceptions, both read-only jump actions:
 *   - window.JarvisAsk.openConversation(id), a small hook app.js now
 *     exports for exactly this (see app.js's own comment on it) — a
 *     conversation-log hit can jump straight into that conversation the
 *     same way the Subagents panel's own "transcript" button already does.
 *   - window.JarvisDaemons.open(), already public (app.js has called it
 *     for the menu item since the H.1 daemons rework) — a daemon-console
 *     hit opens the Daemons panel so the person can pick that service's
 *     console. It does NOT reach into daemons.js beyond this one existing
 *     open() call — no new coupling, and nothing in daemons.js/.css is
 *     touched by this patch.
 *   - window.JarvisSchedules.open(), the sibling module this same patch
 *     adds — a scheduler-ask-log hit opens the Schedules panel.
 *
 * WHAT CHANGED FROM THE OLD INLINE VERSION (see the master plan's Part H.1)
 * --------------------------------------------------------------------------
 *  - Own file, loaded after the panel's own markup (see index.html's
 *    "Scripts load LAST" comment) rather than living inside app.js.
 *  - Live search-as-you-type (debounced ~300ms), not only on the Search
 *    button's click.
 *  - The query term(s) are highlighted inside every hit and inside the
 *    detail pane's context lines — the old panel rendered the whole line
 *    unhighlighted, so finding the actual match in a long line meant
 *    reading it word by word.
 *  - A real two-pane layout: a compact results list on the left (still
 *    built on style.css's existing .logsearch-hit look) and a detail pane
 *    on the right showing the hit's full ±N-line context (log_files.search's
 *    `context` param, already supported server-side, already requested by
 *    this panel — just never displayed beyond the one matching line) plus
 *    jump-to-source actions, instead of every hit being a dead end.
 *  - Sets are now individually-toggleable checkboxes (Conversations /
 *    Daemon consoles / Scheduler / Notifications) instead of one
 *    single-choice <select> — log_files.py's `sets=` already took a list;
 *    the old UI could only ever send one. Notifications is a real
 *    SEARCH_ROOTS entry (log_files.py) the old dropdown never even offered.
 *  - Keyboard: Up/Down moves between results, Enter opens the top jump
 *    action for the selected hit (if any), "/" focuses the query, Escape
 *    clears the query first (if there's something to clear) then closes.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ---- tiny helpers, same shape as daemons.js/backlog.js ------------------ */

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
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) {
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

  async function apiGet(url) {
    const res = await fetch(url);
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || `GET ${url} failed (${res.status})`);
    return data;
  }

  const SETS = [
    { id: "conversations", label: "Conversations", checked: true },
    { id: "daemons", label: "Daemon consoles", checked: true },
    { id: "scheduler", label: "Scheduler", checked: true },
    { id: "notifications", label: "Notifications", checked: false },
  ];

  const state = {
    built: false,
    results: [],
    selected: -1,
    debounce: null,
    prevFocus: null,
  };

  let dom = null;
  function grabDom() {
    const overlay = $("#logsearch-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#logsearch-status-line"),
      query: $("#logsearch-query"),
      mode: $("#logsearch-mode"),
      path: $("#logsearch-path"),
      searchBtn: $("#btn-logsearch"),
      results: $("#logsearch-results"),
      detail: $("#logsearch-detail"),
      summary: $("#logsearch-summary"),
      setChecks: SETS.map((s) => $(`#ls-set-${s.id}`)),
    };
  }

  /* ---- highlighting --------------------------------------------------------
   * Cosmetic only — the backend already decided what's a hit; this just
   * shows WHERE inside the line. Falls back to no highlighting rather than
   * throwing if the query can't compile as a JS regex (e.g. a Python-only
   * construct in regex mode). */

  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  function buildMatcher(query, mode) {
    try {
      if (mode === "regex") return new RegExp(query, "gi");
      if (mode === "phrase") return new RegExp(escapeRe(query), "gi");
      const terms = query.split(/\s+/).filter(Boolean).map(escapeRe);
      return terms.length ? new RegExp(terms.join("|"), "gi") : null;
    } catch (_) { return null; }
  }

  function highlight(text, matcher) {
    if (!matcher) return [text];
    matcher.lastIndex = 0;
    const out = [];
    let last = 0, m, guard = 0;
    while ((m = matcher.exec(text)) && guard++ < 500) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (!m[0]) { matcher.lastIndex++; continue; } // zero-width match guard
      out.push(el("mark", { class: "ls-mark" }, m[0]));
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out.length ? out : [text];
  }

  /* ---- jump targets ----------------------------------------------------- */

  const CONV_RE = /^logs[\\/]([A-Za-z0-9_-]+)\.jsonl$/;
  const DAEMON_RE = /^daemons[\\/]([^\\/]+)[\\/]console\.log/;

  function jumpTarget(hit) {
    const file = hit.file || "";
    let m = CONV_RE.exec(file);
    if (m) return { type: "ask", id: m[1], label: "Open this conversation" };
    m = DAEMON_RE.exec(file);
    if (m) return { type: "daemon", id: m[1], label: `Open Daemons (${m[1]})` };
    if (/^sched_ask_log\.jsonl/.test(file)) return { type: "scheduler", label: "Open Schedules" };
    return null;
  }

  function runJump(target) {
    if (!target) return;
    if (target.type === "ask") {
      if (!global.JarvisAsk || !global.JarvisAsk.openConversation) {
        toast("Ask panel isn't available.", "error");
        return;
      }
      close();
      global.JarvisAsk.openConversation(target.id);
      return;
    }
    if (target.type === "daemon") {
      if (!global.JarvisDaemons) { toast("Daemons panel isn't available.", "error"); return; }
      global.JarvisDaemons.open();
      toast(`Pick "${target.id}" in the list to see this console.`, "info");
      return;
    }
    if (target.type === "scheduler") {
      if (!global.JarvisSchedules) { toast("Schedules panel isn't available.", "error"); return; }
      global.JarvisSchedules.open();
    }
  }

  /* ---- results list ------------------------------------------------------ */

  function renderResults(matcher) {
    if (!dom) return;
    dom.results.textContent = "";
    if (!state.results.length) {
      dom.results.appendChild(el("div", { class: "skills-empty" }, "Nothing matched."));
      return;
    }
    state.results.forEach((hit, i) => {
      const row = el("button", {
        class: "logsearch-hit" + (i === state.selected ? " is-active" : ""), type: "button",
        onclick: () => selectHit(i),
      }, [
        el("div", { class: "logsearch-hit__where" }, `${hit.file}:${hit.line}`),
        el("pre", { class: "logsearch-hit__text" }, highlight(hit.text, matcher)),
      ]);
      dom.results.appendChild(row);
    });
  }

  function renderDetail() {
    if (!dom) return;
    dom.detail.textContent = "";
    const hit = state.results[state.selected];
    if (!hit) {
      dom.detail.appendChild(el("div", { class: "ls-detail-empty" }, "Select a result to see its context."));
      return;
    }
    const matcher = buildMatcher((dom.query.value || "").trim(), dom.mode.value);
    const target = jumpTarget(hit);
    const contextLines = hit.context && hit.context.length
      ? hit.context
      : [{ line: hit.line, text: hit.text }];

    dom.detail.appendChild(el("div", { class: "ls-detail__where" }, `${hit.file}:${hit.line}`));
    dom.detail.appendChild(el("div", { class: "ls-detail__context" }, contextLines.map((c) =>
      el("span", { class: "ls-detail__context-line" + (c.line === hit.line ? " is-match" : "") }, [
        el("span", { class: "ls-lineno" }, String(c.line)),
        ...highlight(c.text, matcher),
        "\n",
      ]))));
    dom.detail.appendChild(el("div", { class: "ls-detail__actions" }, [
      target ? el("button", { class: "btn btn--primary btn--sm", onclick: () => runJump(target) }, target.label) : null,
      el("button", { class: "btn btn--ghost btn--sm", onclick: async () => { toast((await copyText(hit.text)) ? "Copied line." : "Couldn't copy.", "info"); } }, "Copy line"),
      el("button", { class: "btn btn--ghost btn--sm", onclick: async () => { toast((await copyText(hit.path || hit.file)) ? "Copied path." : "Couldn't copy.", "info"); } }, "Copy path"),
    ].filter(Boolean)));
  }

  function selectHit(i) {
    state.selected = i;
    renderResults(buildMatcher((dom.query.value || "").trim(), dom.mode.value));
    renderDetail();
    const row = dom.results.children[i];
    if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
  }

  /* ---- search -------------------------------------------------------------- */

  function activeSets() {
    const on = [];
    SETS.forEach((s, i) => { if (dom.setChecks[i] && dom.setChecks[i].checked) on.push(s.id); });
    return on;
  }

  async function runSearch() {
    const query = (dom.query.value || "").trim();
    if (!query) {
      state.results = []; state.selected = -1;
      dom.results.textContent = "";
      dom.results.appendChild(el("div", { class: "skills-empty" }, "Enter something to search for."));
      dom.summary.textContent = "";
      renderDetail();
      return;
    }
    const sets = activeSets();
    const path = (dom.path.value || "").trim();
    if (!sets.length && !path) {
      dom.summary.textContent = "Check at least one log set, or give a file/glob.";
      return;
    }
    dom.results.textContent = "";
    dom.results.appendChild(el("div", { class: "skills-empty" }, "Searching\u2026"));
    const params = new URLSearchParams({ q: query, context: "2", limit: "150" });
    if (dom.mode.value) params.set("mode", dom.mode.value);
    for (const s of sets) params.append("set", s);
    if (path) params.set("path", path);
    try {
      const data = await apiGet(`/api/log-files/search?${params.toString()}`);
      state.results = data.results || [];
      state.selected = state.results.length ? 0 : -1;
      dom.summary.textContent =
        `${state.results.length} match(es) across ${data.files_scanned || 0} file(s) scanned` +
        (data.truncated ? " \u2014 truncated, narrow the query" : "");
      dom.summary.classList.toggle("is-truncated", Boolean(data.truncated));
      const matcher = buildMatcher(query, dom.mode.value);
      renderResults(matcher);
      renderDetail();
    } catch (err) {
      dom.summary.textContent = "search failed";
      dom.results.textContent = "";
      dom.results.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
      renderDetail();
    }
  }

  function scheduleSearch() {
    clearTimeout(state.debounce);
    state.debounce = setTimeout(runSearch, 300);
  }

  /* ---- keyboard: "/" query, Up/Down results, Enter jump, Escape ----------- */

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") {
      if (t === dom.query && dom.query.value) { dom.query.value = ""; runSearch(); return; }
      close();
      return;
    }
    if (e.key === "/" && !typing) { e.preventDefault(); dom.query.focus(); return; }
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !typing) {
      if (!state.results.length) return;
      e.preventDefault();
      const next = Math.max(0, Math.min(state.results.length - 1, state.selected + (e.key === "ArrowDown" ? 1 : -1)));
      selectHit(next);
      return;
    }
    if (e.key === "Enter" && !typing && state.selected >= 0) {
      const target = jumpTarget(state.results[state.selected]);
      if (target) { e.preventDefault(); runJump(target); }
    }
  }

  /* ---- wiring, open/close --------------------------------------------------- */

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;

    dom.query.addEventListener("input", scheduleSearch);
    dom.query.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { clearTimeout(state.debounce); runSearch(); } });
    dom.mode.addEventListener("change", runSearch);
    dom.path.addEventListener("keydown", (ev) => { if (ev.key === "Enter") runSearch(); });
    dom.setChecks.forEach((cb) => cb && cb.addEventListener("change", runSearch));
    dom.searchBtn.addEventListener("click", () => { clearTimeout(state.debounce); runSearch(); });

    $("#logsearch-close")?.addEventListener("click", close);
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);

    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) {
      toast("Log Search markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
    renderDetail();
    dom.query.focus({ preventScroll: true });
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    dom.overlay.hidden = true;
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  function isOpen() { return Boolean(dom && !dom.overlay.hidden); }

  global.JarvisLogSearch = { open, close, isOpen };
})(window);
