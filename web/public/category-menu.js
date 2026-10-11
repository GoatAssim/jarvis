/* ============================================================================
 * category-menu.js - the Commands panel's category dropdown (L.14 UI rework).
 *
 * It replaces the old "Categories" header button, which opened a chain of
 * separate dialogs and could not filter anything. One control now does both
 * jobs:
 *
 *   - FILTER   pick a category to show only its commands (or Uncategorized, or
 *              All). The choice is remembered in this browser.
 *   - MANAGE   hover a category to rename, merge or delete it in place. Renaming
 *              to a name that already exists IS the merge; the row says so
 *              before anything is saved.
 *
 * Nothing about the back end changed. The same routes do the work:
 *
 *   POST /api/command-categories/rename  { from, to }
 *   POST /api/command-categories/delete  { name }
 *
 * Counts are worked out from the commands the page already holds (the same
 * rules as category-input.js), so opening the menu costs no request.
 *
 * The Daemons panel keeps category-manager.js; this file is only for commands.
 *
 * EVERYTHING IS TEXT. Category names come from a file a person may have
 * hand-edited. They are only ever put in the page with text nodes /
 * textContent, never as markup.
 * ========================================================================= */
(function (global) {
  "use strict";

  const STORE_KEY = "jarvis.commands.category.v1";
  const ROUTE = "/api/command-categories";
  const FIND_BOX_FROM = 9; // show "Find a category" once there are more than 8

  const Cat = () => global.JarvisCategories || null;

  /* ---- pure -------------------------------------------------------------- */

  const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
  const commandsLabel = (n) => plural(n, "command", "commands");

  // What the menu is built from: { total, uncategorized, rows:[{key,name,count}] }.
  // rows are most-used first (ties alphabetical), a name spelt two ways is one row.
  function summarize(commands) {
    const C = Cat();
    const specs = commands && typeof commands === "object" ? Object.values(commands) : [];
    const lists = specs.map((s) => (C && s ? C.normalizeList(s.categories).list : []));
    return {
      total: specs.length,
      uncategorized: lists.filter((l) => !l.length).length,
      rows: C ? C.vocabulary(lists) : [],
    };
  }

  // "all" | "none" | "cat:<key>". Anything else (a stale or hand-edited value)
  // is "all".
  function parseSelection(raw) {
    if (raw === "none") return "none";
    if (typeof raw === "string" && raw.startsWith("cat:") && raw.length > 4) return raw;
    return "all";
  }

  // A saved choice that no longer points at anything falls back to All, so the
  // list is never empty for a reason nobody can see.
  function resolveSelection(sel, summary) {
    const s = parseSelection(sel);
    if (s === "none") return summary.uncategorized > 0 ? "none" : "all";
    if (s.startsWith("cat:")) return summary.rows.some((r) => "cat:" + r.key === s) ? s : "all";
    return "all";
  }

  function matches(spec, sel) {
    const C = Cat();
    if (sel === "all" || !C) return true;
    const list = C.normalizeList(spec && spec.categories).list;
    if (sel === "none") return list.length === 0;
    const key = sel.slice(4);
    return list.some((c) => C.keyOf(c) === key);
  }

  // The trigger's wording: { label, count, filtered }.
  function describe(sel, summary) {
    if (sel === "none") return { label: "Uncategorized", count: summary.uncategorized, filtered: true };
    if (sel.startsWith("cat:")) {
      const row = summary.rows.find((r) => "cat:" + r.key === sel);
      if (row) return { label: row.name, count: row.count, filtered: true };
    }
    return { label: summary.rows.length ? "All categories" : "No categories yet", count: summary.total, filtered: false };
  }

  // What was typed into a rename box, read as an instruction.
  //   { error }                       refused (blank, too long) - say why
  //   { unchanged: true }             nothing to do
  //   { name, merge }                 rename; merge is the row it would join, or null
  function readRename(summary, fromKey, typed) {
    const C = Cat();
    const from = summary.rows.find((r) => r.key === fromKey);
    if (!C || !from) return { error: "That category is gone." };
    const checked = C.checkNewName(typed);
    if (checked.error) return { error: checked.error.charAt(0).toUpperCase() + checked.error.slice(1) + "." };
    const name = checked.name;
    const key = name.toLowerCase();
    if (key === fromKey) return name === from.name ? { unchanged: true } : { name, merge: null };
    return { name, merge: summary.rows.find((r) => r.key === key) || null };
  }

  function visibleRows(summary, query) {
    const want = String(query || "").trim().toLowerCase();
    return want ? summary.rows.filter((r) => r.key.includes(want)) : summary.rows;
  }

  /* ---- small DOM helpers ------------------------------------------------- */

  const SVG_NS = "http://www.w3.org/2000/svg";
  function icon(paths, size) {
    const s = document.createElementNS(SVG_NS, "svg");
    s.setAttribute("viewBox", "0 0 16 16");
    s.setAttribute("width", String(size || 14));
    s.setAttribute("height", String(size || 14));
    s.setAttribute("aria-hidden", "true");
    s.setAttribute("focusable", "false");
    paths.forEach((d) => {
      const p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", "none");
      p.setAttribute("stroke", "currentColor");
      p.setAttribute("stroke-width", "1.5");
      p.setAttribute("stroke-linecap", "round");
      p.setAttribute("stroke-linejoin", "round");
      s.appendChild(p);
    });
    return s;
  }
  const ICONS = {
    caret: ["M3.5 6 8 10.5 12.5 6"],
    check: ["M3.5 8.5 6.5 11.5 12.5 4.5"],
    close: ["M4 4l8 8", "M12 4l-8 8"],
    pencil: ["M10.5 3.5l2 2L6 12l-2.8.8L4 10z"],
    trash: ["M3 4.5h10", "M6.5 4.5V3h3v1.5", "M4.5 4.5l.6 8h5.8l.6-8"],
  };

  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (Array.isArray(children) ? children : children === undefined || children === null ? [] : [children]).forEach((c) => {
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  /* ---- storage + network ------------------------------------------------- */

  function readStore() {
    try { return global.localStorage.getItem(STORE_KEY); } catch (_) { return null; }
  }
  function writeStore(value) {
    try {
      if (value === "all") global.localStorage.removeItem(STORE_KEY);
      else global.localStorage.setItem(STORE_KEY, value);
    } catch (_) { /* private mode: the choice just lasts until reload */ }
  }

  async function post(suffix, body) {
    const res = await fetch(ROUTE + suffix, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok || !data || data.ok === false || data.error) {
      throw new Error((data && data.error) || "Request failed (" + res.status + ").");
    }
    return data;
  }

  /* ---- the component ----------------------------------------------------- */

  // opts: { mount, getCommands: () => commandsObject,
  //         onSelect: () => void,            the filter changed - redraw the list
  //         onChanged: () => Promise|void,   a rename / delete was saved - reload commands
  //         toast: (message, level) => void }
  // Returns { update, matches, describe, getSelection, setSelection, clear, open, close, isOpen, el }.
  function create(opts) {
    const o = opts || {};
    let summary = summarize(o.getCommands ? o.getCommands() : {});
    // The menu is built before the commands have loaded. An empty list says nothing
    // about whether a saved category still exists, so it is only checked once there
    // are commands - otherwise every page load would wipe the saved choice.
    let selection = summary.total ? resolveSelection(readStore(), summary) : parseSelection(readStore());
    let isOpen = false;
    let mode = null;      // { type: "rename"|"delete", key } - one row is being edited
    let busy = false;
    let query = "";
    let note = null;      // { text, level } shown under the row being edited
    let renameDraft = ""; // what is in the rename box, kept across a redraw

    const say = (message, level) => { if (typeof o.toast === "function") o.toast(message, level || "info"); };
    const uid = "cmenu-" + Math.random().toString(36).slice(2, 8);
    const menuId = uid + "-menu";

    const trigger = h("button", {
      type: "button", class: "cmenu__trigger", "aria-haspopup": "true", "aria-expanded": "false", "aria-controls": menuId,
      onclick: () => (isOpen ? close(true) : open()),
    });
    const clearBtn = h("button", {
      type: "button", class: "cmenu__clear", title: "Show all commands", "aria-label": "Show all commands",
      onclick: () => { setSelection("all", true); },
    }, [icon(ICONS.close, 12)]);
    const menu = h("div", { class: "cmenu__menu", id: menuId, role: "group", "aria-label": "Command categories", hidden: true });
    const root = h("div", { class: "cmenu" }, [h("div", { class: "cmenu__bar" }, [trigger, clearBtn]), menu]);

    /* -- trigger -- */
    function paintTrigger() {
      const d = describe(selection, summary);
      trigger.textContent = "";
      trigger.classList.toggle("is-filtered", d.filtered);
      trigger.classList.toggle("is-empty", !summary.rows.length && !d.filtered);
      trigger.setAttribute("aria-expanded", isOpen ? "true" : "false");
      trigger.appendChild(h("span", { class: "cmenu__label" }, d.label));
      trigger.appendChild(h("span", { class: "cmenu__count" }, String(d.count)));
      trigger.appendChild(h("span", { class: "cmenu__caret" }, [icon(ICONS.caret, 12)]));
      trigger.title = d.filtered ? "Showing " + commandsLabel(d.count) + " in \u201c" + d.label + "\u201d" : "Filter commands by category";
      clearBtn.hidden = !d.filtered;
      root.classList.toggle("is-open", isOpen);
    }

    /* -- menu rows -- */
    function pickRow(value, name, count, active, extra) {
      const btn = h("button", {
        type: "button", class: "cmenu__pick" + (active ? " is-active" : ""), "data-value": value,
        "aria-current": active ? "true" : null,
        onclick: () => setSelection(value, true),
      }, [
        h("span", { class: "cmenu__tick" }, active ? [icon(ICONS.check, 12)] : []),
        h("span", { class: "cmenu__name" }, name),
        h("span", { class: "cmenu__n" }, String(count)),
      ]);
      const acts = extra || h("span", { class: "cmenu__acts cmenu__acts--spacer", "aria-hidden": "true" });
      return h("div", { class: "cmenu__item" + (active ? " is-active" : "") }, [btn, acts]);
    }

    function categoryRow(row) {
      const active = selection === "cat:" + row.key;
      const acts = h("span", { class: "cmenu__acts" }, [
        h("button", {
          type: "button", class: "cmenu__act", "data-act": "rename", title: "Rename or merge \u201c" + row.name + "\u201d",
          "aria-label": "Rename or merge " + row.name,
          onclick: () => startMode("rename", row),
        }, [icon(ICONS.pencil, 13)]),
        h("button", {
          type: "button", class: "cmenu__act cmenu__act--danger", "data-act": "delete", title: "Delete \u201c" + row.name + "\u201d",
          "aria-label": "Delete " + row.name,
          onclick: () => startMode("delete", row),
        }, [icon(ICONS.trash, 13)]),
      ]);
      return pickRow("cat:" + row.key, row.name, row.count, active, acts);
    }

    function noteLine() {
      return h("div", { class: "cmenu__note" + (note && note.level ? " is-" + note.level : ""), "aria-live": "polite", "data-note": "" }, note ? note.text : "");
    }

    const RENAME_HINT = "Use a name you already have to merge the two.";

    // What the box says about `value` right now: an error, a merge warning, or
    // nothing. Also whether the button should read Merge.
    function readDraft(row, value) {
      const r = readRename(summary, row.key, value);
      if (r.error) return { note: { text: r.error, level: "error" }, merge: false };
      if (r.merge) {
        return {
          note: { text: "Merges with \u201c" + r.merge.name + "\u201d. All " + commandsLabel(row.count) + " here join its " + r.merge.count + ".", level: "warn" },
          merge: true,
        };
      }
      return { note: null, merge: false };
    }

    function renameBlock(row) {
      const input = h("input", {
        type: "text", class: "cmenu__input", value: renameDraft || row.name, "aria-label": "New name for " + row.name,
        autocomplete: "off", spellcheck: "false", maxlength: "80", disabled: busy,
      });
      const save = h("button", { type: "button", class: "btn btn--primary btn--sm cmenu__save", disabled: busy, onclick: () => submitRename(row, input) }, "Rename");
      const cancel = h("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, onclick: () => endMode(row) }, "Cancel");
      const line = h("div", { class: "cmenu__note", "aria-live": "polite", "data-note": "" });
      const paint = (n, merge) => {
        line.textContent = n ? n.text : RENAME_HINT;
        line.className = "cmenu__note" + (n && n.level ? " is-" + n.level : "");
        save.textContent = merge ? "Merge" : "Rename";
        save.classList.toggle("is-merge", !!merge);
      };
      // Typing clears whatever the last save attempt said and reads the new text.
      input.addEventListener("input", () => {
        renameDraft = input.value;
        const d = readDraft(row, input.value);
        note = d.note;
        paint(d.note, d.merge);
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); submitRename(row, input); }
      });
      // A redraw (the editor is rebuilt after a refusal, or while saving) must keep
      // a standing error from the last attempt; it is only the draft's own hint
      // that is recomputed.
      const d0 = readDraft(row, input.value);
      const draftChanged = !!renameDraft && renameDraft !== row.name;
      paint(note || (draftChanged ? d0.note : null), d0.merge);
      return h("div", { class: "cmenu__edit", "data-edit": "rename" }, [
        h("div", { class: "cmenu__editrow" }, [input, save, cancel]),
        line,
      ]);
    }

    function deleteBlock(row) {
      const keep = h("button", { type: "button", class: "btn btn--ghost btn--sm cmenu__keep", disabled: busy, onclick: () => endMode(row) }, "Keep");
      const del = h("button", { type: "button", class: "btn btn--danger btn--sm cmenu__del", disabled: busy, onclick: () => submitDelete(row) }, "Delete");
      return h("div", { class: "cmenu__edit cmenu__edit--delete", "data-edit": "delete" }, [
        h("div", { class: "cmenu__ask" }, "Remove \u201c" + row.name + "\u201d from " + commandsLabel(row.count) + "? The commands stay."),
        h("div", { class: "cmenu__editrow cmenu__editrow--end" }, [keep, del]),
        noteLine(),
      ]);
    }

    function paintMenu() {
      menu.textContent = "";
      if (!isOpen) return;
      const rows = visibleRows(summary, query);
      if (summary.rows.length >= FIND_BOX_FROM) {
        const find = h("input", {
          type: "search", class: "cmenu__find", placeholder: "Find a category", value: query, "aria-label": "Find a category", autocomplete: "off",
          oninput: (e) => { query = e.target.value; paintMenu(); const f = menu.querySelector(".cmenu__find"); if (f) { f.focus(); f.setSelectionRange(query.length, query.length); } },
        });
        menu.appendChild(h("div", { class: "cmenu__findwrap" }, [find]));
      }
      const list = h("div", { class: "cmenu__list" });
      if (!query) {
        list.appendChild(pickRow("all", "All commands", summary.total, selection === "all"));
        if (summary.uncategorized > 0) list.appendChild(pickRow("none", "Uncategorized", summary.uncategorized, selection === "none"));
        if (summary.rows.length) list.appendChild(h("div", { class: "cmenu__rule", role: "separator" }));
      }
      rows.forEach((row) => {
        if (mode && mode.key === row.key) {
          list.appendChild(h("div", { class: "cmenu__item cmenu__item--editing" }, [mode.type === "rename" ? renameBlock(row) : deleteBlock(row)]));
        } else {
          list.appendChild(categoryRow(row));
        }
      });
      if (!summary.rows.length) {
        list.appendChild(h("div", { class: "cmenu__empty" }, "No categories yet. Open a command, press Edit and add one under Categories."));
      } else if (query && !rows.length) {
        list.appendChild(h("div", { class: "cmenu__empty" }, "No category matches \u201c" + query.trim() + "\u201d."));
      }
      menu.appendChild(list);
      if (summary.rows.length && !mode) {
        menu.appendChild(h("div", { class: "cmenu__foot" }, "Hover a row to rename, merge or delete."));
      }
    }

    /* -- focus helpers -- */
    function focusPick(value) {
      const el = menu.querySelector('.cmenu__pick[data-value="' + (global.CSS && CSS.escape ? CSS.escape(value) : value) + '"]');
      if (el) el.focus();
      return !!el;
    }
    // The panel clips anything that spills out of it, and on a short window the
    // Commands panel is barely taller than the menu. So the menu is sized to the
    // room that is actually there (it scrolls inside) and opens upward when there
    // is more room above the trigger than below it. Run each time it opens and when
    // the window changes. In the stacked phone layout the menu is in the flow, so
    // it is left alone.
    function fitMenu() {
      const panel = root.closest ? root.closest(".panel") : null;
      menu.style.maxHeight = "";
      menu.classList.remove("cmenu__menu--up", "cmenu__menu--tight");
      if (!isOpen || !panel || global.getComputedStyle(menu).position === "static") return;
      const pr = panel.getBoundingClientRect();
      const rr = root.getBoundingClientRect();
      const below = Math.floor(pr.bottom - rr.bottom - 12);
      const above = Math.floor(rr.top - pr.top - 8);
      const natural = menu.offsetHeight; // already capped by the stylesheet
      const up = below < natural && above > below;
      const height = Math.max(90, Math.min(natural, up ? above : below));
      menu.classList.toggle("cmenu__menu--up", up);
      menu.classList.toggle("cmenu__menu--tight", height < 240); // no room for the hint line
      menu.style.maxHeight = height + "px";
    }
    function focusRenameInput(select) {
      const input = menu.querySelector(".cmenu__input");
      if (input) { input.focus(); if (select) input.select(); }
    }
    function focusFirstUseful() {
      if (!focusPick(selection)) {
        const first = menu.querySelector(".cmenu__pick");
        if (first) first.focus();
      }
    }

    /* -- state changes -- */
    function open() {
      if (isOpen) return;
      isOpen = true; mode = null; note = null; query = ""; renameDraft = "";
      menu.hidden = false;
      paintTrigger(); paintMenu();
      fitMenu();
      global.document.addEventListener("pointerdown", onOutside, true);
      global.addEventListener("resize", fitMenu);
      focusFirstUseful();
    }

    function close(returnFocus) {
      if (!isOpen) return;
      isOpen = false; mode = null; note = null; query = ""; renameDraft = "";
      menu.hidden = true; menu.textContent = ""; menu.style.maxHeight = "";
      global.document.removeEventListener("pointerdown", onOutside, true);
      global.removeEventListener("resize", fitMenu);
      paintTrigger();
      if (returnFocus) trigger.focus();
    }

    function onOutside(e) { if (!root.contains(e.target)) close(false); }

    function setSelection(value, fromUser) {
      const next = resolveSelection(value, summary);
      const changed = next !== selection;
      selection = next;
      writeStore(selection);
      paintTrigger();
      if (fromUser) close(true);
      if (changed && typeof o.onSelect === "function") o.onSelect();
    }

    function startMode(type, row) {
      if (busy) return;
      mode = { type, key: row.key }; note = null; renameDraft = "";
      paintMenu();
      if (type === "rename") focusRenameInput(true);
      else { const keep = menu.querySelector(".cmenu__keep"); if (keep) keep.focus(); } // the safe answer is the default
    }

    function endMode(row) {
      mode = null; note = null; renameDraft = "";
      paintMenu();
      if (row) { const act = menu.querySelector('.cmenu__pick[data-value="cat:' + row.key + '"]'); if (act) act.focus(); }
    }

    async function afterSaved() {
      busy = false; mode = null; note = null; renameDraft = "";
      try { if (typeof o.onChanged === "function") await o.onChanged(); } catch (_) { /* the list reloads on the next push */ }
      update();
      if (isOpen) { paintMenu(); focusFirstUseful(); }
    }

    async function submitRename(row, input) {
      if (busy) return;
      const r = readRename(summary, row.key, input.value);
      if (r.error) { note = { text: r.error, level: "error" }; renameDraft = input.value; paintMenu(); focusRenameInput(false); return; }
      if (r.unchanged) { endMode(row); return; }
      busy = true; note = null; renameDraft = input.value; paintMenu();
      try {
        const res = await post("/rename", { from: row.name, to: r.name });
        if (selection === "cat:" + row.key) selection = "cat:" + (r.merge ? r.merge.key : r.name.toLowerCase());
        writeStore(selection);
        say((res.merged || r.merge ? "Merged into \u201c" : "Renamed to \u201c") + (r.merge ? r.merge.name : r.name) + "\u201d on " + commandsLabel(res.changed || 0) + ".");
        await afterSaved();
      } catch (err) {
        busy = false; note = { text: err.message, level: "error" }; paintMenu(); focusRenameInput(false);
      }
    }

    async function submitDelete(row) {
      if (busy) return;
      busy = true; paintMenu();
      try {
        const res = await post("/delete", { name: row.name });
        if (selection === "cat:" + row.key) { selection = "all"; writeStore(selection); }
        say("Deleted \u201c" + row.name + "\u201d from " + commandsLabel(res.changed || 0) + ".");
        await afterSaved();
      } catch (err) {
        busy = false; note = { text: err.message, level: "error" }; paintMenu();
      }
    }

    // Called whenever the commands change. Cheap enough to run on every redraw.
    // The open menu is only redrawn when what it shows changed, so a redraw of the
    // list (a favourite toggled, a push from the server) never steals focus from
    // a rename box.
    let lastSig = "";
    function update() {
      summary = summarize(o.getCommands ? o.getCommands() : {});
      const next = summary.total ? resolveSelection(selection, summary) : selection;
      if (next !== selection) { selection = next; writeStore(selection); }
      if (mode && !summary.rows.some((r) => r.key === mode.key)) { mode = null; note = null; }
      const sig = JSON.stringify([summary, selection, mode]);
      const same = sig === lastSig;
      lastSig = sig;
      paintTrigger();
      if (isOpen && !busy && !same) paintMenu();
    }

    /* -- keyboard -- */
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (!isOpen) return;
        e.preventDefault(); e.stopPropagation();
        if (mode) { const row = summary.rows.find((r) => r.key === mode.key); endMode(row); } else close(true);
        return;
      }
      if (e.target === trigger && (e.key === "ArrowDown" || e.key === "ArrowUp") && !isOpen) {
        e.preventDefault(); open(); return;
      }
      if (!isOpen || (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End")) return;
      if (e.target && e.target.tagName === "INPUT" && e.target.className.indexOf("cmenu__find") === -1) return; // arrows belong to the rename box
      const stops = Array.from(menu.querySelectorAll(".cmenu__find, .cmenu__pick"));
      if (!stops.length) return;
      e.preventDefault();
      const at = stops.indexOf(global.document.activeElement);
      let to = at;
      if (e.key === "Home") to = 0;
      else if (e.key === "End") to = stops.length - 1;
      else if (e.key === "ArrowDown") to = at < 0 ? 0 : Math.min(stops.length - 1, at + 1);
      else to = at <= 0 ? 0 : at - 1;
      stops[to].focus();
    });
    root.addEventListener("focusout", (e) => {
      const to = e.relatedTarget;
      if (isOpen && to && !root.contains(to)) close(false);
    });

    paintTrigger();
    if (o.mount) o.mount.appendChild(root);

    return {
      el: root, update, matches: (spec) => matches(spec, selection), describe: () => describe(selection, summary),
      getSelection: () => selection, setSelection: (v) => setSelection(v, false), clear: () => setSelection("all", false),
      open, close: () => close(false), isOpen: () => isOpen,
    };
  }

  global.JarvisCategoryMenu = {
    create,
    _pure: { summarize, parseSelection, resolveSelection, matches, describe, readRename, visibleRows },
  };
})(typeof window !== "undefined" ? window : globalThis);
