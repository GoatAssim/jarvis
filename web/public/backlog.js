/* ============================================================================
 * backlog.js — Menu → Backlog.
 *
 * Part H.1 rework. Untimed work, grouped by state (backlog.py's module
 * docstring has the full "why isn't this the scheduler" reasoning).
 *
 * Deliberately standalone, the same way daemons.js and test-checklist.js
 * are: its own tiny $/el/api/toast helpers, no reach into app.js's
 * internals. Talks to /api/backlog the same way app.js used to.
 *
 * WHAT CHANGED FROM THE OLD INLINE VERSION (see the master plan's Part H.1)
 * --------------------------------------------------------------------------
 *  - Own file, loaded after the panel's own markup (see index.html's
 *    "Scripts load LAST" comment) rather than living inside app.js.
 *  - A live search box (title/project/note) and a project filter — neither
 *    existed before; the board rendered whatever backlog-board returned,
 *    unfiltered, every time.
 *  - An Overview pane: counts per column, a blocked roll-up, the stale-doing
 *    roll-up backlog.summary() already computed server-side but the old UI
 *    never showed, and a project list (open/blocked/done per project) that
 *    doubles as the project filter's own picker.
 *  - Keyboard: "/" focuses search, "r" refreshes, Escape closes (clearing
 *    search first if there's something to clear, matching Test Checklist's
 *    own Escape behavior).
 *  - Real drag-and-drop between columns. style.css's .kanban__card comment
 *    has claimed cards were "drag-reorderable" for a while now; auditing
 *    app.js turned up no dragstart/dragover/drop handler anywhere backing
 *    that claim — the comment was aspirational, not a description of
 *    working code. This is the actual implementation. The existing
 *    advance/block/remove buttons are untouched — drag is one more way to
 *    move a card, not a replacement for them.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* ---- tiny helpers, same shape as daemons.js ---------------------------- */

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
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || `${method} ${url} failed (${res.status})`);
    return data;
  }
  const Api = { get: (p) => api("GET", p), post: (p, b) => api("POST", p, b), patch: (p, b) => api("PATCH", p, b), del: (p) => api("DELETE", p) };

  const COLUMNS = [
    ["idea", "Ideas"], ["todo", "To do"], ["doing", "Doing"],
    ["blocked", "Blocked"], ["done", "Done"],
  ];
  const NEXT_STATE = { idea: "todo", todo: "doing", doing: "done", blocked: "doing", done: "todo" };

  const state = {
    built: false,
    search: "",
    project: "",           // "" = every project
    board: {},              // last fetch, keyed by state
    summary: {},
    dragId: null,
    prevFocus: null,
  };

  let dom = null;
  function grabDom() {
    const overlay = $("#backlog-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#backlog-status-line"),
      search: $("#backlog-search"),
      projectFilter: $("#backlog-project-filter"),
      count: $("#backlog-toolbar-count"),
      board: $("#backlog-board"),
      side: $("#backlog-side"),
    };
  }

  /* ---- filtering ----------------------------------------------------------
   * Client-side over the already-fetched board — the board is small (500
   * items max, backlog.py's own MAX_ITEMS) and refetching per keystroke
   * would just be a slower version of the same filter. */

  function matches(item) {
    if (state.project && (item.project || "") !== state.project) return false;
    const q = state.search.trim().toLowerCase();
    if (!q) return true;
    const hay = [item.title, item.project, item.note, item.blocked_on].filter(Boolean).join("\n").toLowerCase();
    return hay.includes(q);
  }

  /* ---- drag-and-drop ------------------------------------------------------ */

  function wireCardDrag(card, item) {
    card.draggable = true;
    card.addEventListener("dragstart", (ev) => {
      state.dragId = item.id;
      card.classList.add("bk-dragging");
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", item.id);
    });
    card.addEventListener("dragend", () => {
      state.dragId = null;
      card.classList.remove("bk-dragging");
    });
  }

  function wireColumnDrop(column, targetState) {
    column.addEventListener("dragover", (ev) => {
      if (!state.dragId) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      column.classList.add("bk-drop-target");
    });
    column.addEventListener("dragleave", () => column.classList.remove("bk-drop-target"));
    column.addEventListener("drop", (ev) => {
      ev.preventDefault();
      column.classList.remove("bk-drop-target");
      const id = state.dragId || ev.dataTransfer.getData("text/plain");
      state.dragId = null;
      if (!id) return;
      const item = findItem(id);
      if (!item || item.state === targetState) return;
      const fields = { state: targetState };
      // Mirror the "Block" button's own unblock rule: leaving the Blocked
      // column drops a now-stale blocked_on note instead of leaving it
      // stranded (see the same reasoning in renderCard's Block handler).
      if (item.state === "blocked" && targetState !== "blocked") fields.blocked_on = "";
      updateItem(id, fields);
    });
  }

  function findItem(id) {
    for (const [stateName] of COLUMNS) {
      const hit = (state.board[stateName] || []).find((i) => i.id === id);
      if (hit) return hit;
    }
    return null;
  }

  /* ---- row rendering ------------------------------------------------------ */

  function renderCard(item) {
    const next = NEXT_STATE[item.state] || "todo";
    const card = el("div", { class: `kanban__card kanban__card--${item.priority || "normal"}` }, [
      el("div", { class: "kanban__title" }, item.title),
      item.project ? el("div", { class: "kanban__project" }, item.project) : null,
      item.blocked_on ? el("div", { class: "kanban__blocked" }, `waiting on ${item.blocked_on}`) : null,
      el("div", { class: "kanban__actions" }, [
        el("button", {
          class: "btn btn--ghost btn--sm",
          onclick: () => updateItem(item.id, { state: next }),
        }, item.state === "done" ? "Reopen" : `\u2192 ${next}`),
        el("button", {
          class: "btn btn--ghost btn--sm",
          onclick: () => {
            const reason = global.prompt("Blocked on what?", item.blocked_on || "");
            if (reason === null) return;
            const fields = { blocked_on: reason };
            if (!reason && item.state === "blocked") fields.state = "doing";
            updateItem(item.id, fields);
          },
        }, "Block"),
        el("button", { class: "btn btn--ghost btn--sm", onclick: () => removeItem(item.id) }, "\u00d7"),
      ]),
    ]);
    wireCardDrag(card, item);
    return card;
  }

  function renderBoard() {
    if (!dom) return;
    dom.board.textContent = "";
    let shown = 0, total = 0;
    for (const [key, label] of COLUMNS) {
      const items = state.board[key] || [];
      total += items.length;
      const visible = items.filter(matches);
      shown += visible.length;
      const column = el("div", { class: "kanban__col" }, [
        el("div", { class: "kanban__colhead" }, `${label} (${visible.length}${visible.length !== items.length ? ` of ${items.length}` : ""})`),
      ]);
      wireColumnDrop(column, key);
      for (const item of visible) column.appendChild(renderCard(item));
      if (!visible.length) column.appendChild(el("div", { class: "kanban__empty" }, items.length ? "\u2014 filtered out \u2014" : "\u2014"));
      dom.board.appendChild(column);
    }
    if (dom.count) {
      dom.count.textContent = (state.search.trim() || state.project) ? `${shown} of ${total}` : `${total} item${total === 1 ? "" : "s"}`;
    }
  }

  /* ---- Overview pane -------------------------------------------------------
   * Everything here comes straight out of backlog.summary() — counts,
   * blocked, stale_doing and projects were all already computed server-
   * side; the old panel just never read them. */

  function renderSide() {
    if (!dom || !dom.side) return;
    const s = state.summary || {};
    const counts = s.counts || {};
    const maxCount = Math.max(1, ...COLUMNS.map(([k]) => counts[k] || 0));
    dom.side.textContent = "";

    const countsBlock = el("div", null, [
      el("h4", null, "By column"),
      el("div", { class: "bk-counts" }, COLUMNS.map(([key, label]) =>
        el("div", { class: "bk-count-row" }, [
          el("span", { class: "bk-count-row__label" }, label),
          el("span", { class: "bk-count-row__n" }, String(counts[key] || 0)),
        ]))),
    ]);
    // One shared bar per column, right under the counts, so "which column is
    // actually heavy" reads at a glance rather than from raw numbers alone.
    const bars = el("div", { class: "bk-counts", style: "margin-top: -4px" }, COLUMNS.map(([key]) =>
      el("div", { class: "bk-count-bar" }, el("i", { style: `width:${((counts[key] || 0) / maxCount * 100).toFixed(1)}%` }))));
    countsBlock.appendChild(bars);
    dom.side.appendChild(countsBlock);

    const blocked = s.blocked || [];
    dom.side.appendChild(el("div", null, [
      el("h4", null, `Blocked (${blocked.length})`),
      el("div", { class: "bk-list" }, blocked.length
        ? blocked.map((b) => el("button", {
            class: "bk-flag-item", type: "button",
            onclick: () => { state.search = b.title; if (dom.search) dom.search.value = b.title; renderBoard(); },
          }, [
            el("div", { class: "bk-flag-item__title" }, b.title),
            el("div", { class: "bk-flag-item__note" }, `waiting on ${b.blocked_on || "?"}`),
          ]))
        : [el("div", { class: "bk-list-empty" }, "Nothing blocked.")]),
    ]));

    const stale = s.stale_doing || [];
    if (stale.length) {
      dom.side.appendChild(el("div", null, [
        el("h4", null, `Stale in Doing (${stale.length})`),
        el("div", { class: "bk-list" }, stale.map((it) => el("button", {
          class: "bk-flag-item bk-flag-item--stale", type: "button",
          onclick: () => { state.search = it.title; if (dom.search) dom.search.value = it.title; renderBoard(); },
        }, [
          el("div", { class: "bk-flag-item__title" }, it.title),
          el("div", { class: "bk-flag-item__note" }, `untouched ${it.days} day${it.days === 1 ? "" : "s"}`),
        ]))),
      ]));
    }

    const projects = s.projects || [];
    dom.side.appendChild(el("div", null, [
      el("h4", null, "Projects"),
      el("div", { class: "bk-projects" }, [
        el("button", {
          class: "bk-project-row" + (!state.project ? " is-active" : ""), type: "button",
          onclick: () => selectProject(""),
        }, [el("span", { class: "bk-project-row__name" }, "All projects"), el("span", { class: "bk-project-row__n" }, String(projects.reduce((n, p) => n + p.total, 0)))]),
        ...projects.map((p) => el("button", {
          class: "bk-project-row" + (state.project === p.project ? " is-active" : ""), type: "button",
          onclick: () => selectProject(p.project === "(none)" ? "" : p.project),
        }, [
          el("span", { class: "bk-project-row__name" }, p.project),
          p.blocked ? el("span", { class: "bk-project-row__blocked" }, `${p.blocked} blocked`) : null,
          el("span", { class: "bk-project-row__n" }, `${p.open}/${p.total}`),
        ])),
      ]),
    ]));
  }

  function selectProject(name) {
    state.project = name;
    if (dom.projectFilter) dom.projectFilter.value = name;
    renderBoard();
    renderSide();
  }

  function populateProjectFilter() {
    if (!dom.projectFilter) return;
    const projects = (state.summary && state.summary.projects) || [];
    const current = state.project;
    dom.projectFilter.textContent = "";
    dom.projectFilter.appendChild(el("option", { value: "" }, "All projects"));
    for (const p of projects) {
      if (p.project === "(none)") continue;
      dom.projectFilter.appendChild(el("option", { value: p.project }, `${p.project} (${p.open})`));
    }
    dom.projectFilter.value = current;
  }

  /* ---- fetch / mutate ------------------------------------------------------ */

  async function refresh() {
    if (!dom || dom.overlay.hidden) return;
    try {
      const data = await Api.get("/api/backlog");
      state.board = data.board || {};
      state.summary = data.summary || {};
      const blockedCount = (state.summary.blocked || []).length;
      const staleCount = (state.summary.stale_doing || []).length;
      dom.statusLine.textContent =
        `${state.summary.open || 0} open` + (blockedCount ? `, ${blockedCount} blocked` : "") +
        (staleCount ? `, ${staleCount} stale` : "");
      populateProjectFilter();
      renderBoard();
      renderSide();
    } catch (err) {
      dom.statusLine.textContent = "couldn't read the backlog";
      dom.board.textContent = "";
      dom.board.appendChild(el("div", { class: "skills-empty" }, err.message || "Failed."));
    }
  }

  async function updateItem(id, fields) {
    try {
      await Api.patch(`/api/backlog/${encodeURIComponent(id)}`, fields);
      refresh();
    } catch (err) {
      toast(err.message || "Couldn't update that item.");
    }
  }

  async function removeItem(id) {
    try {
      await Api.del(`/api/backlog/${encodeURIComponent(id)}`);
      refresh();
    } catch (err) {
      toast(err.message || "Couldn't remove that item.");
    }
  }

  /* ---- keyboard: "/" search, "r" refresh, Escape ---------------------------- */

  function onKey(e) {
    if (!dom || dom.overlay.hidden) return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") {
      if (t === dom.search && dom.search.value) {
        state.search = ""; dom.search.value = ""; renderBoard();
        return;
      }
      close();
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    if (e.key === "r" || e.key === "R") { e.preventDefault(); refresh(); }
  }

  /* ---- wiring, open/close ---------------------------------------------------- */

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;

    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderBoard(); });
    dom.projectFilter.addEventListener("change", () => selectProject(dom.projectFilter.value));

    $("#btn-backlog-add")?.addEventListener("click", async () => {
      const title = ($("#backlog-new-title")?.value || "").trim();
      if (!title) return;
      try {
        await Api.post("/api/backlog", {
          title,
          project: ($("#backlog-new-project")?.value || "").trim(),
          state: $("#backlog-new-state")?.value || "todo",
        });
        $("#backlog-new-title").value = "";
        refresh();
      } catch (err) {
        toast(err.message || "Couldn't add that.");
      }
    });
    $("#backlog-new-title")?.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") $("#btn-backlog-add")?.click();
    });

    $("#backlog-close")?.addEventListener("click", close);
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);

    state.built = true;
    return true;
  }

  function open() {
    if (!ensureBuilt()) {
      toast("Backlog markup is missing from index.html.", "error");
      return;
    }
    state.prevFocus = document.activeElement;
    dom.overlay.hidden = false;
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

  global.JarvisBacklog = { open, close, isOpen };
})(window);
