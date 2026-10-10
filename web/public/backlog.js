/* ============================================================================
 * backlog.js — Menu → Backlog.
 *
 * Part H.1 rework, then rework 2 (L.55). Untimed work, grouped by state
 * (backlog.py's module docstring has the full "why isn't this the scheduler"
 * reasoning).
 *
 * Deliberately standalone, the same way daemons.js and test-checklist.js
 * are: its own tiny $/el/api/toast helpers, no reach into app.js's
 * internals. Talks to /api/backlog.
 *
 * WHAT THE FIRST REWORK (H.1) ADDED
 *  - Own file; live search; project filter; an Overview pane (counts, blocked
 *    and stale roll-ups, per-project totals); drag-and-drop between columns.
 *
 * WHAT THIS REWORK (L.55) ADDS
 *  - Keyboard: arrow keys walk the board (one tab stop, roving, like the
 *    Daemons list), Home/End, Enter/E edits, B sets "waiting on", 1/2/3 set
 *    priority, Delete removes (after a confirm), N new item, ? the cheat sheet.
 *  - Inline editing of title, project, priority, note and "waiting on" on the
 *    card itself. Only the fields the person actually changed are sent, so an
 *    edit never reverts something Jarvis changed in the meantime.
 *  - Reordering INSIDE a column, which needed a stored order (backlog.py's
 *    `order` + POST /api/backlog/:id/move): drag with a drop line, or
 *    Shift+Up/Down. Shift+Left/Right moves a card to the next/previous column.
 *  - Every change goes through one queue. The CLI is a new process per call
 *    and backlog.json is read-modify-write, so two quick key presses must not
 *    overlap; the board also updates at once and is re-read from the server
 *    when the queue drains (which is also what undoes a change that failed).
 *  - A new card design (priority, project, age, note, waiting-on, stale), a
 *    priority filter, per-column empty states and a live region for screen
 *    readers.
 *
 * The pure logic (matching, ordering, diffing, neighbour picking) is exported
 * as JarvisBacklog._pure so tests/verify_backlog_panel.js can run it in Node.
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
  async function confirmDialog(opts) {
    if (UI() && UI().confirm) return UI().confirm(opts);
    return global.confirm((opts.title ? opts.title + "\n\n" : "") + (opts.body || ""));
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
  const COLUMN_EMPTY = {
    idea: "Rough thoughts go here",
    todo: "Nothing queued",
    doing: "Nothing in progress",
    blocked: "Nothing is blocked",
    done: "Nothing finished yet",
  };
  const NEXT_STATE = { idea: "todo", todo: "doing", doing: "done", blocked: "doing", done: "todo" };
  const PRIORITIES = [["", "All"], ["high", "High"], ["normal", "Normal"], ["low", "Low"]];
  const PRIORITY_KEYS = { "1": "high", "2": "normal", "3": "low" };
  const EDIT_FIELDS = ["title", "project", "priority", "note", "blocked_on"];

  const KEYS = [
    ["\u2190 \u2191 \u2192 \u2193", "Move between cards"],
    ["Home / End", "First / last card in the column"],
    ["Shift + \u2191 \u2193", "Reorder inside the column"],
    ["Shift + \u2190 \u2192", "Move to the previous / next column"],
    ["Enter or E", "Edit the card"],
    ["B", "Set what it is waiting on"],
    ["1  2  3", "Priority: high / normal / low"],
    ["Delete", "Remove the card (asks first)"],
    ["N", "New item"],
    ["/", "Search"],
    ["R", "Refresh"],
    ["Esc", "Cancel an edit \u00b7 clear the search \u00b7 close"],
    ["?", "Show / hide this list"],
  ];

  /* ===========================================================================
   * PURE LOGIC — no DOM, no module state. Exported as _pure.
   * ========================================================================= */

  const collapse = (v) => String(v === null || v === undefined ? "" : v).split(/\s+/).filter(Boolean).join(" ");
  const normProject = (v) => collapse(v).toLowerCase();

  /** Does `item` pass the filters? f = { project, priority, search } */
  function matchesItem(item, f) {
    if (f.project && (item.project || "") !== f.project) return false;
    if (f.priority && (item.priority || "normal") !== f.priority) return false;
    const q = String(f.search || "").trim().toLowerCase();
    if (!q) return true;
    const hay = [item.title, item.project, item.note, item.blocked_on].filter(Boolean).join("\n").toLowerCase();
    return hay.includes(q);
  }

  /** "now", "5m", "3h", "2d", "3w", "4mo" — `ts` is epoch seconds. */
  function ageText(ts, now) {
    if (!ts || typeof ts !== "number") return "";
    const secs = Math.max(0, (now === undefined ? Date.now() / 1000 : now) - ts);
    if (secs < 90) return "now";
    const mins = secs / 60;
    if (mins < 90) return `${Math.round(mins)}m`;
    const hrs = mins / 60;
    if (hrs < 36) return `${Math.round(hrs)}h`;
    const days = hrs / 24;
    if (days < 14) return `${Math.round(days)}d`;
    if (days < 60) return `${Math.round(days / 7)}w`;
    return `${Math.round(days / 30)}mo`;
  }

  /**
   * The id the server should be told to place a card *before* so that it
   * lands directly after `afterId` in this (full, unfiltered) column. The
   * moving card is ignored. null = bottom of the column.
   */
  function anchorAfter(columnItems, afterId, movingId) {
    const rest = (columnItems || []).filter((i) => i.id !== movingId);
    const at = rest.findIndex((i) => i.id === afterId);
    if (at < 0) return null;
    return at + 1 < rest.length ? rest[at + 1].id : null;
  }

  /**
   * Where a drop lands, from the vertical midpoints of the visible cards.
   * mids = [{ id, mid }] top to bottom, WITHOUT the card being dragged.
   * Returns { beforeId, lastId }: the card to sit above, or (when below all
   * of them) the last visible card.
   */
  function dropTarget(mids, y) {
    for (const m of mids) if (y < m.mid) return { beforeId: m.id, lastId: null };
    return { beforeId: null, lastId: mids.length ? mids[mids.length - 1].id : null };
  }

  /** Turn a dropTarget into the server's `before` id (null = bottom). */
  function dropAnchor(columnItems, tgt, movingId) {
    if (tgt.beforeId) return tgt.beforeId;
    if (tgt.lastId) return anchorAfter(columnItems, tgt.lastId, movingId);
    return null;
  }

  /**
   * Move an item inside `board` (state -> [items]) exactly the way
   * backlog.move() will, so the screen can update before the server answers.
   * `board` arrays are replaced, not mutated in place. Returns true when
   * anything changed (false = it was already there, send nothing).
   */
  function applyMove(board, id, toState, beforeId, now) {
    let item = null, fromState = null;
    for (const key of Object.keys(board)) {
      const hit = (board[key] || []).find((i) => i.id === id);
      if (hit) { item = hit; fromState = key; break; }
    }
    if (!item) return false;
    const fromCol = board[fromState] || [];
    const oldIndex = fromCol.indexOf(item);
    const rest = (board[toState] || []).filter((i) => i !== item);
    let at = beforeId ? rest.findIndex((i) => i.id === beforeId) : -1;
    if (at < 0) at = rest.length;
    if (fromState === toState && at === oldIndex) return false;

    board[fromState] = fromCol.filter((i) => i !== item);
    if (fromState !== toState) {
      const t = now === undefined ? Date.now() / 1000 : now;
      item.state = toState;
      if (toState !== "blocked") item.blocked_on = "";
      item.done_at = toState === "done" ? (item.done_at || t) : null;
      item.updated = t;
    }
    rest.splice(at, 0, item);
    board[toState] = rest;
    return true;
  }

  /** Remove an item from the board; returns it (or null). */
  function removeFromBoard(board, id) {
    for (const key of Object.keys(board)) {
      const col = board[key] || [];
      const hit = col.find((i) => i.id === id);
      if (hit) { board[key] = col.filter((i) => i !== hit); return hit; }
    }
    return null;
  }

  /**
   * What changed in an edit. Compares the draft with what the person STARTED
   * from (`original`), never with the live item, so an edit to the title
   * cannot silently put back an old note that Jarvis has since rewritten.
   * Returns { fields } or { error }.
   */
  function diffFields(draft, original) {
    const title = collapse(draft.title);
    if (title.length < 2) return { error: "Give it a title (at least 2 characters)." };
    const fields = {};
    if (title !== collapse(original.title)) fields.title = title;
    const project = normProject(draft.project);
    if (project !== normProject(original.project)) fields.project = project;
    const priority = draft.priority || "normal";
    if (priority !== (original.priority || "normal")) fields.priority = priority;
    const note = collapse(draft.note);
    if (note !== collapse(original.note)) fields.note = note;
    const waiting = collapse(draft.blocked_on);
    if (waiting !== collapse(original.blocked_on)) {
      fields.blocked_on = waiting;
      // Clearing "waiting on" on a blocked card is how you unblock it.
      if (!waiting && original.state === "blocked") fields.state = "doing";
    }
    return { fields };
  }

  function isDirty(draft, original) {
    return EDIT_FIELDS.some((k) => {
      if (k === "project") return normProject(draft[k]) !== normProject(original[k]);
      if (k === "priority") return (draft[k] || "normal") !== (original[k] || "normal");
      return collapse(draft[k]) !== collapse(original[k]);
    });
  }

  /**
   * Arrow-key target. cols = [[id, ...], ...] — the VISIBLE ids per column,
   * left to right. pos = { col, row }. Up/down stay in the column and stop
   * at its ends; left/right skip empty columns and keep the row (clamped).
   * Returns an id, or null for "nowhere to go".
   */
  function pickNeighbour(cols, pos, dc, dr) {
    if (!pos || !cols[pos.col]) return null;
    if (dc === 0) {
      const ids = cols[pos.col];
      const row = Math.min(ids.length - 1, Math.max(0, pos.row + dr));
      return row === pos.row ? null : ids[row];
    }
    for (let c = pos.col + dc; c >= 0 && c < cols.length; c += dc) {
      if (cols[c].length) return cols[c][Math.min(pos.row, cols[c].length - 1)];
    }
    return null;
  }

  /** Which card to select when the selected one is gone (deleted, filtered). */
  function fallbackSelection(cols, hint) {
    if (hint && cols[hint.col] && cols[hint.col].length) {
      return cols[hint.col][Math.min(hint.row, cols[hint.col].length - 1)];
    }
    for (const ids of cols) if (ids.length) return ids[0];
    return null;
  }

  /**
   * Shift+Up/Down. vis = the column's visible items, full = all of its items,
   * row = the card's row in `vis`. Returns { anchor, target } (`anchor` is
   * the server's `before` id) or null when it is already at that end.
   */
  function reorderPlan(vis, full, row, dr) {
    const target = row + dr;
    if (target < 0 || target >= vis.length) return null;
    const moving = vis[row].id;
    const anchor = dr < 0 ? vis[target].id : anchorAfter(full, vis[target].id, moving);
    return { anchor, target };
  }

  /* ===========================================================================
   * STATE + DOM
   * ========================================================================= */

  const state = {
    built: false,
    loaded: false,
    loadError: "",
    search: "",
    project: "",              // "" = every project
    priority: "",             // "" = every priority
    board: {},                // last fetch (plus optimistic changes), keyed by state
    summary: {},
    staleIds: new Set(),
    selectedId: null,
    lastPos: null,            // { col, row } of the selected card, for fallbacks
    editing: null,            // { id, original, draft, focusField, error, saving }
    drag: null,               // { id }
    pending: 0,               // queued requests not yet answered
    fetchSeq: 0,
    showKeys: false,
    focusBoard: false,        // put focus on the selected card after the next render
    flip: false,
    prevFocus: null,
  };

  let chain = Promise.resolve();
  let dom = null;

  function grabDom() {
    const overlay = $("#backlog-overlay");
    if (!overlay) return null;
    return {
      overlay,
      statusLine: $("#backlog-status-line"),
      search: $("#backlog-search"),
      projectFilter: $("#backlog-project-filter"),
      projectList: $("#backlog-project-list"),
      priorityFilter: $("#backlog-priority-filter"),
      saving: $("#backlog-saving"),
      help: $("#backlog-help-btn"),
      count: $("#backlog-toolbar-count"),
      board: $("#backlog-board"),
      keys: $("#backlog-keys"),
      live: $("#backlog-live"),
      side: $("#backlog-side"),
    };
  }

  const cssId = (id) => String(id).replace(/[^A-Za-z0-9_-]/g, "");
  const cardEl = (id) => (dom && id ? dom.board.querySelector(`.bk-card[data-id="${cssId(id)}"]`) : null);
  const filters = () => ({ project: state.project, priority: state.priority, search: state.search });
  const isEditing = (id) => Boolean(state.editing && state.editing.id === id);

  function findItem(id) {
    for (const [key] of COLUMNS) {
      const hit = (state.board[key] || []).find((i) => i.id === id);
      if (hit) return hit;
    }
    return null;
  }

  /** The columns as the person sees them: all items, and the visible ones. */
  function visibleCols() {
    return COLUMNS.map(([key, label]) => {
      const all = state.board[key] || [];
      // The card being edited stays on screen even if typing in the search
      // box would filter it out — otherwise the editor would vanish under them.
      const visible = all.filter((i) => matchesItem(i, filters()) || isEditing(i.id));
      return { key, label, all, visible };
    });
  }

  function positionOf(id, cols) {
    if (!id) return null;
    const list = cols || visibleCols();
    for (let c = 0; c < list.length; c++) {
      const r = list[c].visible.findIndex((i) => i.id === id);
      if (r >= 0) return { col: c, row: r, cols: list };
    }
    return null;
  }

  const idGrid = (cols) => cols.map((c) => c.visible.map((i) => i.id));

  function announce(message) {
    if (!dom || !dom.live) return;
    state.flip = !state.flip;
    dom.live.textContent = message + (state.flip ? "\u200b" : "");
  }

  /* ---- selection ----------------------------------------------------------- */

  function ensureSelection(cols) {
    const grid = idGrid(cols);
    if (state.selectedId && grid.some((ids) => ids.includes(state.selectedId))) return;
    state.selectedId = fallbackSelection(grid, state.lastPos);
  }

  function setCardTabStops(card, on) {
    card.tabIndex = on ? 0 : -1;
    card.querySelectorAll(".bk-act").forEach((b) => { b.tabIndex = on ? 0 : -1; });
  }

  function select(id, opts) {
    state.selectedId = id;
    const pos = positionOf(id);
    if (pos) state.lastPos = { col: pos.col, row: pos.row };
    if (!dom) return;
    dom.board.querySelectorAll(".bk-card.is-selected").forEach((n) => {
      if (n.dataset.id === id) return;
      n.classList.remove("is-selected");
      n.removeAttribute("aria-current");
      setCardTabStops(n, false);
    });
    const node = cardEl(id);
    if (!node) return;
    node.classList.add("is-selected");
    node.setAttribute("aria-current", "true");
    setCardTabStops(node, true);
    if (opts && opts.focus) {
      node.focus();
      try { node.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (_) { /* old browser */ }
    }
  }

  function focusSelected() {
    const node = cardEl(state.selectedId);
    if (node) node.focus();
  }

  /* ---- request queue ---------------------------------------------------------
   * One request at a time, in the order they were made. Each CLI call is a
   * fresh process doing read-modify-write on backlog.json; two overlapping
   * ones would lose an update. When the queue empties the board is re-read,
   * which also puts right anything that was applied optimistically and then
   * failed. */

  function setSaving() {
    if (!dom) return;
    if (dom.saving) dom.saving.hidden = state.pending === 0;
    dom.board.setAttribute("aria-busy", state.pending ? "true" : "false");
  }

  function enqueue(task) {
    state.pending += 1;
    setSaving();
    const run = chain.then(task);
    chain = run.catch(() => {});
    return run.finally(() => {
      state.pending -= 1;
      setSaving();
      if (state.pending === 0) refresh();
    });
  }

  /* ---- drag-and-drop -------------------------------------------------------- */

  function clearDropMarks() {
    if (!dom) return;
    dom.board.querySelectorAll(".bk-drop-before, .bk-drop-after, .bk-drop-target").forEach((n) => {
      n.classList.remove("bk-drop-before", "bk-drop-after", "bk-drop-target");
    });
  }

  function columnTarget(colEl, clientY) {
    const dragId = state.drag && state.drag.id;
    const mids = Array.from(colEl.querySelectorAll(".bk-card"))
      .filter((c) => c.dataset.id !== dragId)
      .map((c) => { const r = c.getBoundingClientRect(); return { id: c.dataset.id, mid: r.top + r.height / 2 }; });
    return dropTarget(mids, clientY);
  }

  function showDropMark(colEl, tgt) {
    clearDropMarks();
    colEl.classList.add("bk-drop-target");
    const id = tgt.beforeId || tgt.lastId;
    const card = id ? colEl.querySelector(`.bk-card[data-id="${cssId(id)}"]`) : null;
    if (card) card.classList.add(tgt.beforeId ? "bk-drop-before" : "bk-drop-after");
  }

  function wireCardDrag(card, item) {
    card.draggable = true;
    card.addEventListener("dragstart", (ev) => {
      if (state.editing || ev.target !== card) { ev.preventDefault(); return; }
      state.drag = { id: item.id };
      card.classList.add("bk-dragging");
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", item.id);
      select(item.id);
    });
    card.addEventListener("dragend", () => {
      state.drag = null;
      card.classList.remove("bk-dragging");
      clearDropMarks();
    });
  }

  function wireColumnDrop(colEl, key, label) {
    colEl.addEventListener("dragover", (ev) => {
      if (!state.drag) return; // not one of ours (a file, a link, text)
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      showDropMark(colEl, columnTarget(colEl, ev.clientY));
    });
    colEl.addEventListener("dragleave", (ev) => {
      if (!ev.relatedTarget || !colEl.contains(ev.relatedTarget)) clearDropMarks();
    });
    colEl.addEventListener("drop", (ev) => {
      ev.preventDefault();
      const id = (state.drag && state.drag.id) || ev.dataTransfer.getData("text/plain");
      const tgt = columnTarget(colEl, ev.clientY);
      clearDropMarks();
      state.drag = null;
      const item = id ? findItem(id) : null;
      if (!item) return;
      const full = state.board[key] || [];
      moveItem(id, key, dropAnchor(full, tgt, id), item.state === key ? "Reordered" : `Moved to ${label}`);
    });
  }

  /* ---- mutations ------------------------------------------------------------- */

  function moveItem(id, toState, beforeId, say) {
    if (!applyMove(state.board, id, toState, beforeId)) return;
    state.selectedId = id;
    if (say) announce(say);
    renderBoard();
    enqueue(() => Api.post(`/api/backlog/${encodeURIComponent(id)}/move`, { state: toState, before: beforeId || "" }))
      .catch((err) => toast(err.message || "Couldn't move that item.", "error"));
  }

  function setPriority(id, priority) {
    const item = findItem(id);
    if (!item || (item.priority || "normal") === priority) return;
    item.priority = priority;
    announce(`Priority ${priority}`);
    renderBoard();
    enqueue(() => Api.patch(`/api/backlog/${encodeURIComponent(id)}`, { priority }))
      .catch((err) => toast(err.message || "Couldn't change the priority.", "error"));
  }

  function unblock(id) {
    const item = findItem(id);
    if (!item) return;
    // Same effect as clearing "waiting on" in the editor.
    enqueue(() => Api.patch(`/api/backlog/${encodeURIComponent(id)}`, { blocked_on: "", state: "doing" }))
      .catch((err) => toast(err.message || "Couldn't unblock that item.", "error"));
  }

  async function removeWithConfirm(id) {
    const item = findItem(id);
    if (!item) return;
    const ok = await confirmDialog({
      title: "Remove this item?",
      body: `\u201c${item.title}\u201d will be deleted from the backlog. This can\u2019t be undone.`,
      confirmLabel: "Remove", cancelLabel: "Keep it", level: "warn", focusCancel: true,
    });
    if (!ok) { state.focusBoard = true; focusSelected(); return; }
    if (!removeFromBoard(state.board, id)) return;
    if (isEditing(id)) state.editing = null;
    announce(`Removed ${item.title}`);
    state.focusBoard = true;
    renderBoard();
    enqueue(() => Api.del(`/api/backlog/${encodeURIComponent(id)}`))
      .catch((err) => toast(err.message || "Couldn't remove that item.", "error"));
  }

  async function addItem() {
    const titleEl = $("#backlog-new-title");
    const title = (titleEl && titleEl.value || "").trim();
    if (!title) { if (titleEl) titleEl.focus(); return; }
    if (title.length < 2) { toast("Give it a slightly longer title.", "warn"); return; }
    const body = {
      title,
      project: ($("#backlog-new-project") && $("#backlog-new-project").value || "").trim(),
      state: ($("#backlog-new-state") && $("#backlog-new-state").value) || "todo",
      priority: ($("#backlog-new-priority") && $("#backlog-new-priority").value) || "normal",
    };
    try {
      const data = await enqueue(() => Api.post("/api/backlog", body));
      if (titleEl) titleEl.value = "";
      if (data && data.item && data.item.id) state.selectedId = data.item.id;
      announce("Added " + title);
    } catch (err) {
      toast(err.message || "Couldn't add that.", "error");
    }
  }

  /* ---- inline editing ---------------------------------------------------------- */

  function editDirty() {
    return Boolean(state.editing) && isDirty(state.editing.draft, state.editing.original);
  }

  async function confirmDiscard() {
    return confirmDialog({
      title: "Discard your changes?",
      body: "This card has edits that haven\u2019t been saved.",
      confirmLabel: "Discard", cancelLabel: "Keep editing", level: "warn", focusCancel: true,
    });
  }

  function startEdit(id, focusField) {
    const item = findItem(id);
    if (!item) return;
    if (state.editing) {
      if (state.editing.id === id) {
        state.editing.focusField = focusField || "title";
        renderBoard();
        return;
      }
      if (editDirty()) {
        toast("Finish or cancel the edit that\u2019s open first.", "warn");
        state.editing.focusField = "title";
        renderBoard();
        return;
      }
    }
    const original = {
      title: item.title || "", project: item.project || "", priority: item.priority || "normal",
      note: item.note || "", blocked_on: item.blocked_on || "", state: item.state,
    };
    state.editing = { id, original, draft: Object.assign({}, original), focusField: focusField || "title", error: "", saving: false };
    state.selectedId = id;
    renderBoard();
  }

  async function cancelEdit() {
    const ed = state.editing;
    if (!ed || ed.saving) return;
    if (editDirty() && !(await confirmDiscard())) {
      if (state.editing === ed) { ed.focusField = "title"; renderBoard(); }
      return;
    }
    if (state.editing !== ed) return;
    state.editing = null;
    state.focusBoard = true;
    renderBoard();
  }

  async function saveEdit() {
    const ed = state.editing;
    if (!ed || ed.saving) return;
    if (!findItem(ed.id)) {
      state.editing = null;
      toast("That item isn\u2019t on the board any more.", "warn");
      renderBoard();
      return;
    }
    const diff = diffFields(ed.draft, ed.original);
    if (diff.error) { ed.error = diff.error; ed.focusField = "title"; renderBoard(); return; }
    if (!Object.keys(diff.fields).length) {
      state.editing = null;
      state.focusBoard = true;
      renderBoard();
      return;
    }
    ed.saving = true;
    ed.error = "";
    renderBoard();
    try {
      await enqueue(async () => {
        await Api.patch(`/api/backlog/${encodeURIComponent(ed.id)}`, diff.fields);
        // Cleared before the queue drains and the board is re-read.
        if (state.editing === ed) state.editing = null;
      });
      state.focusBoard = true;
      announce("Saved");
    } catch (err) {
      ed.saving = false;
      ed.error = err.message || "Couldn\u2019t save that.";
      ed.focusField = "title";
      if (state.editing === ed) renderBoard();
    }
  }

  function editorKey(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancelEdit(); return; }
    if (e.key !== "Enter" || e.isComposing) return;
    const t = e.target;
    if (t && t.tagName === "BUTTON") return; // Save / Cancel activate themselves
    e.preventDefault();
    // Notes are stored on one line, so Shift+Enter would only be thrown away.
    if (t && t.tagName === "TEXTAREA" && e.shiftKey) return;
    saveEdit();
  }

  /* ---- rendering ------------------------------------------------------------------ */

  function cardLabel(item) {
    const bits = [item.title, `${item.priority || "normal"} priority`];
    if (item.project) bits.push(`project ${item.project}`);
    if (item.blocked_on) bits.push(`waiting on ${item.blocked_on}`);
    if (state.staleIds.has(item.id)) bits.push("stale");
    return bits.join(", ");
  }

  function renderActions(item) {
    const next = NEXT_STATE[item.state] || "todo";
    const button = (act, text, title, onclick, extra) => el("button", {
      class: "bk-act" + (extra || ""), type: "button", "data-act": act, title,
      "aria-label": `${title}: ${item.title}`, tabindex: "-1", onclick,
    }, text);
    return el("div", { class: "bk-card__actions" }, [
      button("advance", item.state === "done" ? "Reopen" : `\u2192 ${next}`,
        item.state === "done" ? "Reopen (move to To do)" : `Move to ${next}`,
        () => moveItem(item.id, next, null, `Moved to ${(COLUMNS.find((c) => c[0] === next) || [0, next])[1]}`)),
      button("edit", "Edit", "Edit (Enter)", () => startEdit(item.id, "title")),
      item.state === "blocked"
        ? button("unblock", "Unblock", "Unblock", () => unblock(item.id))
        : button("block", "Block", "Set what it is waiting on (B)", () => startEdit(item.id, "blocked_on")),
      button("remove", "\u00d7", "Remove (Delete)", () => removeWithConfirm(item.id), " bk-act--danger"),
    ]);
  }

  function renderCardBody(item) {
    const prio = item.priority || "normal";
    const meta = [];
    if (item.project) meta.push(el("span", { class: "bk-chip bk-chip--project", title: "Project" }, item.project));
    if (state.staleIds.has(item.id)) {
      meta.push(el("span", { class: "bk-chip bk-chip--stale", title: "In Doing, untouched for over a week" }, "stale"));
    }
    const when = item.state === "done" ? (item.done_at || item.updated) : item.updated;
    const age = ageText(when);
    if (age) meta.push(el("span", { class: "bk-card__age", title: item.state === "done" ? "Finished" : "Last changed" }, age));
    return [
      el("div", { class: "bk-card__head" }, [
        el("div", { class: "bk-card__title" }, item.title),
        prio !== "normal" ? el("span", { class: `bk-chip bk-chip--${prio}`, title: `${prio} priority` }, prio) : null,
      ]),
      meta.length ? el("div", { class: "bk-card__meta" }, meta) : null,
      item.note ? el("div", { class: "bk-card__note", title: item.note }, item.note) : null,
      item.blocked_on ? el("div", { class: "bk-card__blocked" }, `waiting on ${item.blocked_on}`) : null,
      renderActions(item),
    ];
  }

  function renderEditor(item) {
    const ed = state.editing;
    const d = ed.draft;
    const dis = ed.saving ? true : null;
    const field = (label, control) => el("label", { class: "bk-edit__field" }, [el("span", { class: "bk-edit__label" }, label), control]);
    const title = el("input", { class: "bk-input", type: "text", "data-field": "title", value: d.title, maxlength: "200", autocomplete: "off", disabled: dis });
    const project = el("input", { class: "bk-input", type: "text", "data-field": "project", value: d.project, maxlength: "60", list: "backlog-project-list", autocomplete: "off", disabled: dis });
    const priority = el("select", { class: "bk-input", "data-field": "priority", disabled: dis },
      [["low", "Low"], ["normal", "Normal"], ["high", "High"]].map(([v, t]) => el("option", { value: v }, t)));
    priority.value = d.priority || "normal";
    const waiting = el("input", {
      class: "bk-input", type: "text", "data-field": "blocked_on", value: d.blocked_on, maxlength: "1000", autocomplete: "off", disabled: dis,
      placeholder: item.state === "blocked" ? "clear this to unblock" : "filling this in blocks the card",
    });
    const note = el("textarea", { class: "bk-input bk-input--note", "data-field": "note", rows: "3", maxlength: "1000", disabled: dis, placeholder: "optional" }, d.note);
    return el("div", { class: "bk-edit", role: "group", "aria-label": `Edit ${item.title}` }, [
      field("Title", title),
      el("div", { class: "bk-edit__row" }, [field("Project", project), field("Priority", priority)]),
      field("Waiting on", waiting),
      field("Note", note),
      ed.error ? el("div", { class: "bk-edit__error", role: "alert" }, ed.error) : null,
      el("div", { class: "bk-edit__actions" }, [
        el("button", { class: "btn btn--primary btn--sm", type: "button", disabled: dis, onclick: saveEdit }, ed.saving ? "Saving\u2026" : "Save"),
        el("button", { class: "btn btn--ghost btn--sm", type: "button", disabled: dis, onclick: () => cancelEdit() }, "Cancel"),
        el("span", { class: "bk-edit__hint" }, "Enter saves \u00b7 Esc cancels"),
      ]),
    ]);
  }

  function renderCard(item) {
    const editing = isEditing(item.id);
    const selected = state.selectedId === item.id;
    const classes = ["bk-card", `bk-card--${item.priority || "normal"}`];
    if (selected) classes.push("is-selected");
    if (editing) classes.push("is-editing");
    if (state.staleIds.has(item.id)) classes.push("is-stale");
    const card = el("div", {
      class: classes.join(" "), role: "listitem", "data-id": item.id,
      tabindex: selected ? "0" : "-1",
      "aria-current": selected ? "true" : null,
      "aria-label": editing ? null : cardLabel(item),
    }, editing ? [renderEditor(item)] : renderCardBody(item));
    if (selected) card.querySelectorAll(".bk-act").forEach((b) => { b.tabIndex = 0; });
    card.addEventListener("click", () => { if (state.selectedId !== item.id) select(item.id); });
    if (!editing) {
      card.addEventListener("dblclick", (ev) => { if (!ev.target.closest("button")) startEdit(item.id, "title"); });
      wireCardDrag(card, item);
    }
    return card;
  }

  function boardMessage(kind) {
    if (kind === "loading") return el("div", { class: "bk-board-msg" }, "Reading the backlog\u2026");
    return el("div", { class: "bk-board-msg bk-board-msg--error" }, [
      el("div", null, state.loadError || "Couldn\u2019t read the backlog."),
      el("button", { class: "btn btn--ghost btn--sm", type: "button", onclick: () => refresh() }, "Try again"),
    ]);
  }

  function captureFocus() {
    const a = document.activeElement;
    if (!dom || !a || !dom.board.contains(a)) return null;
    const card = a.closest(".bk-card");
    const ds = a.dataset || {};
    return {
      id: card ? card.dataset.id : null,
      field: ds.field || null,
      act: ds.act || null,
      start: typeof a.selectionStart === "number" ? a.selectionStart : null,
      end: typeof a.selectionEnd === "number" ? a.selectionEnd : null,
    };
  }

  function restoreFocus(f) {
    if (!f || !f.id) return false;
    const card = cardEl(f.id);
    if (!card) return false;
    let target = null;
    if (f.field) target = card.querySelector(`[data-field="${f.field}"]`);
    else if (f.act) target = card.querySelector(`[data-act="${f.act}"]`);
    if (!target) target = card;
    target.focus({ preventScroll: true });
    if (f.start !== null && typeof target.setSelectionRange === "function") {
      try { target.setSelectionRange(f.start, f.end === null ? f.start : f.end); } catch (_) { /* not a text field */ }
    }
    return true;
  }

  function renderBoard() {
    if (!dom) return;
    const focus = captureFocus();
    dom.board.textContent = "";
    if (!state.loaded) {
      dom.board.appendChild(boardMessage(state.loadError ? "error" : "loading"));
      if (dom.count) dom.count.textContent = "";
      return;
    }
    const cols = visibleCols();
    ensureSelection(cols);

    let shown = 0, total = 0;
    const done = ((state.summary && state.summary.counts) || {}).done || 0;
    for (const c of cols) { total += c.all.length; shown += c.visible.length; }

    if (!total) {
      dom.board.appendChild(el("div", { class: "bk-board-msg" }, [
        el("div", { class: "bk-board-msg__title" }, "Nothing in the backlog yet"),
        el("div", null, "Add something below, or tell Jarvis \u201cadd X to my backlog\u201d."),
      ]));
    } else {
      for (const c of cols) {
        const filtered = c.visible.length !== c.all.length;
        const col = el("div", { class: `kanban__col bk-col bk-col--${c.key}`, "data-col": c.key, role: "group", "aria-label": `${c.label}, ${c.visible.length} item${c.visible.length === 1 ? "" : "s"}` }, [
          el("div", { class: "bk-col__head" }, [
            el("span", { class: "bk-col__dot", "aria-hidden": "true" }),
            el("span", { class: "bk-col__name" }, c.label),
            el("span", { class: "bk-col__count" }, filtered ? `${c.visible.length} of ${c.all.length}` : String(c.all.length)),
          ]),
        ]);
        const list = el("div", { class: "bk-col__list", role: "list" });
        c.visible.forEach((item) => list.appendChild(renderCard(item)));
        col.appendChild(list);
        if (!c.visible.length) {
          col.appendChild(el("div", { class: "bk-col__empty" }, c.all.length ? "Filtered out" : COLUMN_EMPTY[c.key]));
        }
        if (c.key === "done" && done > c.all.length) {
          col.appendChild(el("div", { class: "bk-col__more" }, `${done - c.all.length} older finished not shown`));
        }
        wireColumnDrop(col, c.key, c.label);
        dom.board.appendChild(col);
      }
    }
    if (dom.count) {
      dom.count.textContent = (state.search.trim() || state.project || state.priority)
        ? `${shown} of ${total}` : `${total} item${total === 1 ? "" : "s"}`;
    }

    const pos = positionOf(state.selectedId, cols);
    state.lastPos = pos ? { col: pos.col, row: pos.row } : state.lastPos;

    // Focus: an editor that was just opened wins; then wherever it already
    // was (so a re-render never throws the caret out of a field); then the
    // selected card when something asked for it.
    const ed = state.editing;
    if (ed && ed.focusField) {
      const card = cardEl(ed.id);
      const input = card && card.querySelector(`[data-field="${ed.focusField}"]`);
      ed.focusField = null;
      if (input) {
        input.focus();
        if (typeof input.select === "function") input.select();
        return;
      }
    }
    if (state.focusBoard) {
      state.focusBoard = false;
      if (!focus || !restoreFocus(focus)) focusSelected();
      return;
    }
    if (focus && !restoreFocus(focus)) focusSelected();
  }

  /* ---- Overview pane -------------------------------------------------------
   * Everything here comes straight out of backlog.summary() — counts,
   * blocked, stale_doing and projects were all already computed server-
   * side; the old panel just never read them. */

  function jumpTo(id) {
    const item = findItem(id);
    if (!item) { toast("That item isn\u2019t on the board any more.", "warn"); return; }
    if (!matchesItem(item, filters())) {
      state.search = ""; state.project = ""; state.priority = "";
      if (dom.search) dom.search.value = "";
      if (dom.projectFilter) dom.projectFilter.value = "";
      renderPriorityFilter();
    }
    state.selectedId = id;
    state.focusBoard = true;
    renderBoard();
    renderSide();
  }

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
          el("span", { class: `bk-count-row__dot bk-col--${key}`, "aria-hidden": "true" }),
          el("span", { class: "bk-count-row__label" }, label),
          el("span", { class: "bk-count-row__n" }, String(counts[key] || 0)),
        ]))),
    ]);
    const bars = el("div", { class: "bk-counts", style: "margin-top: -4px" }, COLUMNS.map(([key]) =>
      el("div", { class: "bk-count-bar" }, el("i", { style: `width:${((counts[key] || 0) / maxCount * 100).toFixed(1)}%` }))));
    countsBlock.appendChild(bars);
    dom.side.appendChild(countsBlock);

    const blocked = s.blocked || [];
    dom.side.appendChild(el("div", null, [
      el("h4", null, `Blocked (${blocked.length})`),
      el("div", { class: "bk-list" }, blocked.length
        ? blocked.map((b) => el("button", {
            class: "bk-flag-item", type: "button", onclick: () => jumpTo(b.id),
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
          class: "bk-flag-item bk-flag-item--stale", type: "button", onclick: () => jumpTo(it.id),
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
    const projects = ((state.summary && state.summary.projects) || []).filter((p) => p.project !== "(none)");
    if (dom.projectFilter) {
      const current = state.project;
      dom.projectFilter.textContent = "";
      dom.projectFilter.appendChild(el("option", { value: "" }, "All projects"));
      for (const p of projects) dom.projectFilter.appendChild(el("option", { value: p.project }, `${p.project} (${p.open})`));
      dom.projectFilter.value = current;
    }
    if (dom.projectList) {
      dom.projectList.textContent = "";
      for (const p of projects) dom.projectList.appendChild(el("option", { value: p.project }));
    }
  }

  function renderPriorityFilter() {
    if (!dom || !dom.priorityFilter) return;
    dom.priorityFilter.textContent = "";
    for (const [value, label] of PRIORITIES) {
      dom.priorityFilter.appendChild(el("button", {
        class: "bk-seg__btn" + (state.priority === value ? " is-active" : ""), type: "button",
        "aria-pressed": state.priority === value ? "true" : "false",
        title: value ? `Only ${value} priority` : "Every priority",
        onclick: () => { state.priority = value; renderPriorityFilter(); renderBoard(); },
      }, label));
    }
  }

  function renderKeys() {
    if (!dom || !dom.keys) return;
    dom.keys.textContent = "";
    dom.keys.classList.toggle("is-open", state.showKeys);
    if (dom.help) dom.help.setAttribute("aria-expanded", state.showKeys ? "true" : "false");
    dom.keys.appendChild(el("div", { class: "bk-keys__line" }, [
      el("span", null, [el("kbd", null, "\u2190\u2191\u2192\u2193"), " move"]),
      el("span", null, [el("kbd", null, "Shift"), "+", el("kbd", null, "arrows"), " reorder"]),
      el("span", null, [el("kbd", null, "Enter"), " edit"]),
      el("span", null, [el("kbd", null, "Del"), " remove"]),
      el("span", null, [el("kbd", null, "?"), " all shortcuts"]),
    ]));
    if (state.showKeys) {
      dom.keys.appendChild(el("div", { class: "bk-keys__sheet" }, KEYS.map(([k, what]) =>
        el("div", { class: "bk-keys__row" }, [el("kbd", null, k), el("span", null, what)]))));
    }
  }

  function setKeys(open) {
    state.showKeys = Boolean(open);
    renderKeys();
  }

  /* ---- fetch ---------------------------------------------------------------------- */

  function updateStatusLine() {
    const blockedCount = (state.summary.blocked || []).length;
    const staleCount = (state.summary.stale_doing || []).length;
    dom.statusLine.textContent =
      `${state.summary.open || 0} open` + (blockedCount ? `, ${blockedCount} blocked` : "") +
      (staleCount ? `, ${staleCount} stale` : "");
  }

  async function refresh() {
    if (!dom || dom.overlay.hidden) return;
    // Something is still being saved: the queue re-reads when it finishes,
    // and reading now would show the board as it was before that change.
    if (state.pending > 0) return;
    const seq = ++state.fetchSeq;
    try {
      const data = await Api.get("/api/backlog");
      if (seq !== state.fetchSeq || state.pending > 0) return;
      state.board = data.board || {};
      state.summary = data.summary || {};
      state.staleIds = new Set((state.summary.stale_doing || []).map((i) => i.id));
      state.loaded = true;
      state.loadError = "";
      if (state.editing && !findItem(state.editing.id)) {
        state.editing = null;
        toast("The item you were editing was removed.", "warn");
      }
      updateStatusLine();
      populateProjectFilter();
      renderBoard();
      renderSide();
    } catch (err) {
      if (seq !== state.fetchSeq) return;
      dom.statusLine.textContent = "couldn't read the backlog";
      if (state.loaded) {
        toast(err.message || "Couldn\u2019t refresh the backlog.", "error");
      } else {
        state.loadError = err.message || "Failed.";
        renderBoard();
      }
    }
  }

  /* ---- keyboard ---------------------------------------------------------------------- */

  function firstVisibleId() {
    const grid = idGrid(visibleCols());
    const ids = grid.find((g) => g.length);
    return ids ? ids[0] : null;
  }

  function moveSelection(dc, dr) {
    const pos = positionOf(state.selectedId);
    if (!pos) {
      const first = firstVisibleId();
      if (first) select(first, { focus: true });
      return;
    }
    const id = pickNeighbour(idGrid(pos.cols), pos, dc, dr);
    if (id) select(id, { focus: true });
  }

  function jumpEdge(first) {
    const pos = positionOf(state.selectedId);
    if (!pos) return;
    const ids = idGrid(pos.cols)[pos.col];
    select(first ? ids[0] : ids[ids.length - 1], { focus: true });
  }

  function reorderBy(dr) {
    const pos = positionOf(state.selectedId);
    if (!pos) return;
    const col = pos.cols[pos.col];
    const plan = reorderPlan(col.visible, col.all, pos.row, dr);
    if (!plan) { announce(dr < 0 ? "Already at the top" : "Already at the bottom"); return; }
    const item = col.visible[pos.row];
    moveItem(item.id, col.key, plan.anchor, `${item.title}, position ${plan.target + 1} of ${col.visible.length}`);
  }

  function shiftColumn(dc) {
    const item = findItem(state.selectedId);
    if (!item) return;
    const at = COLUMNS.findIndex(([k]) => k === item.state);
    const to = at + dc;
    if (to < 0 || to >= COLUMNS.length) { announce("No column that way"); return; }
    moveItem(item.id, COLUMNS[to][0], null, `Moved to ${COLUMNS[to][1]}`);
  }

  function inBoardContext(t) {
    return !t || t === document.body || t === document.documentElement || t === dom.overlay || dom.board.contains(t);
  }

  function onKey(e) {
    if (!isOpen()) return;
    // A confirm or any other modal owns the keyboard while it is up.
    if (document.querySelector(".jui-backdrop")) return;
    const t = e.target;
    const tag = t && t.tagName;
    const typing = Boolean(t) && (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable);

    if (state.editing && t && t.closest && t.closest(".bk-edit")) { editorKey(e); return; }

    if (e.key === "Escape") {
      if (state.editing) { e.preventDefault(); cancelEdit(); return; }
      if (t === dom.search && dom.search.value) {
        state.search = ""; dom.search.value = ""; renderBoard();
        return;
      }
      if (state.showKeys) { setKeys(false); return; }
      close();
      return;
    }
    if (state.editing) return;

    // From the search box, Down steps into the board.
    if (t === dom.search && e.key === "ArrowDown") {
      e.preventDefault();
      const first = state.selectedId && positionOf(state.selectedId) ? state.selectedId : firstVisibleId();
      if (first) select(first, { focus: true });
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;

    if (e.key === "/") { e.preventDefault(); dom.search.focus(); return; }
    if (e.key === "?") { e.preventDefault(); setKeys(!state.showKeys); return; }
    if (e.key === "r" || e.key === "R") { e.preventDefault(); refresh(); return; }
    if (e.key === "n" || e.key === "N") {
      const input = $("#backlog-new-title");
      if (input) { e.preventDefault(); input.focus(); }
      return;
    }
    if (!inBoardContext(t)) return;

    const id = state.selectedId;
    switch (e.key) {
      case "ArrowUp": case "ArrowDown": case "ArrowLeft": case "ArrowRight": {
        e.preventDefault();
        const dc = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        const dr = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
        if (e.shiftKey) { if (dc) shiftColumn(dc); else reorderBy(dr); }
        else moveSelection(dc, dr);
        return;
      }
      case "Home": case "End":
        e.preventDefault(); jumpEdge(e.key === "Home"); return;
      case "Enter":
        if (tag === "BUTTON") return; // let the focused button do its own thing
        if (id) { e.preventDefault(); startEdit(id, "title"); }
        return;
      case "F2": case "e": case "E":
        if (id) { e.preventDefault(); startEdit(id, "title"); }
        return;
      case "b": case "B":
        if (id) { e.preventDefault(); startEdit(id, "blocked_on"); }
        return;
      case "Delete":
        if (id) { e.preventDefault(); removeWithConfirm(id); }
        return;
      case "1": case "2": case "3":
        if (id) { e.preventDefault(); setPriority(id, PRIORITY_KEYS[e.key]); }
        return;
      default:
    }
  }

  /* ---- wiring, open/close ----------------------------------------------------------- */

  function ensureBuilt() {
    if (state.built) return true;
    dom = grabDom();
    if (!dom) return false;

    dom.search.addEventListener("input", () => { state.search = dom.search.value; renderBoard(); });
    dom.projectFilter.addEventListener("change", () => selectProject(dom.projectFilter.value));
    if (dom.help) dom.help.addEventListener("click", () => setKeys(!state.showKeys));

    // Keep the open editor's draft current, so a re-render never loses typing.
    const track = (ev) => {
      const f = ev.target && ev.target.dataset && ev.target.dataset.field;
      if (f && state.editing) state.editing.draft[f] = ev.target.value;
    };
    dom.board.addEventListener("input", track);
    dom.board.addEventListener("change", track);
    // Tab or a click landing in a card makes it the selected one.
    dom.board.addEventListener("focusin", (ev) => {
      const card = ev.target.closest && ev.target.closest(".bk-card");
      if (card && card.dataset.id !== state.selectedId) select(card.dataset.id);
    });

    $("#btn-backlog-add")?.addEventListener("click", addItem);
    $("#backlog-new-title")?.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); addItem(); }
    });

    $("#backlog-close")?.addEventListener("click", close);
    dom.overlay.addEventListener("click", (ev) => { if (ev.target === dom.overlay) close(); });
    document.addEventListener("keydown", onKey);

    renderPriorityFilter();
    renderKeys();
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
    state.focusBoard = true;
    renderBoard(); // what we had last time, at once; refresh() corrects it
    refresh();
  }

  function hide() {
    state.editing = null;
    state.drag = null;
    clearDropMarks();
    dom.overlay.hidden = true;
    const prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && prev.focus && document.contains(prev)) { try { prev.focus(); } catch (_) { /* gone */ } }
  }

  function close() {
    if (!dom || dom.overlay.hidden) return;
    if (editDirty()) {
      confirmDiscard().then((ok) => { if (ok && dom && !dom.overlay.hidden) hide(); });
      return;
    }
    hide();
  }

  function isOpen() { return Boolean(dom && !dom.overlay.hidden); }

  global.JarvisBacklog = {
    open, close, isOpen,
    _pure: {
      collapse, normProject, matchesItem, ageText, anchorAfter, dropTarget, dropAnchor,
      applyMove, removeFromBoard, diffFields, isDirty, pickNeighbour, fallbackSelection, reorderPlan,
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
