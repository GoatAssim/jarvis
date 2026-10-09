/* ============================================================================
 * category-input.js - the one category component (L.11 daemons, L.14 commands).
 *
 * L.14.3 asks for "one small module, used by both surfaces, so behaviour
 * cannot drift". This is it. It has two halves:
 *
 *   PURE   normalizeName / normalizeList / vocabulary / suggest / canonical -
 *          no DOM, no network. normalizeName and normalizeList are the same
 *          rules as jarvis-cli/jarvis/categories.py, and
 *          tests/test_categories.py runs one corpus through both and fails if
 *          they disagree, so a name this editor accepts is never re-spelled
 *          by the registry on save.
 *   WIDGET createInput() - chips plus a text box with a suggestion list.
 *
 * What is shared and what is not (owner decision Q5, 2026-10-01): the rules
 * and the widget are shared; the VOCABULARY is not. A surface passes in the
 * names in use on *its own* items, so a category made for a command is never
 * suggested for a daemon.
 *
 * Behaviour, as specified in L.14.3:
 *   - Suggests on prefix, case-insensitive, most-used first.
 *   - A suggestion shows the existing name's exact spelling, and typing
 *     `utility` when `Utility` exists stores `Utility`, so the two can never
 *     become separate categories.
 *   - Enter or Tab accepts the highlighted suggestion; a name that matches
 *     nothing is created by Enter. When what you typed is only the START of
 *     an existing name, press Esc (or type a comma) first to keep it as typed.
 *   - Limits: MAX_NAME_LEN characters a name, MAX_PER_ITEM names an item.
 *
 * EVERYTHING IS TEXT, NEVER MARKUP. Names come from a file a person may have
 * hand-edited; they go in with textContent / text nodes, never innerHTML.
 * ========================================================================= */

(function (global) {
  "use strict";

  const MAX_NAME_LEN = 24;
  const MAX_PER_ITEM = 8;

  // The same explicit whitespace class as categories.py, not \s: the two
  // languages' \s disagree about a few characters and these must agree.
  const WS_RE = /[ \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g;
  const CC_RE = /\p{Cc}/gu;

  /* ---- pure -------------------------------------------------------------- */

  // One name, cleaned; "" means there was nothing usable. `truncate = false`
  // returns the full cleaned text so a strict caller can measure the overflow.
  function normalizeName(value, truncate) {
    if (typeof value !== "string") return "";
    let text = value.replace(CC_RE, " ").replace(WS_RE, " ").replace(/^ +| +$/g, "");
    if (truncate !== false) {
      // Array.from counts code points, as Python's len() does.
      const points = Array.from(text);
      if (points.length > MAX_NAME_LEN) text = points.slice(0, MAX_NAME_LEN).join("").replace(/ +$/, "");
    }
    return text;
  }

  // A category's identity: case-insensitive.
  const keyOf = (name) => normalizeName(name).toLowerCase();

  // { list, error } - the same contract as categories.normalize_list().
  // Lenient by default (fix what can be fixed, never refuse); strict says
  // what is wrong, for a write.
  function normalizeList(items, strict) {
    if (items === null || items === undefined) return { list: [], error: "" };
    if (typeof items === "string") items = [items];
    if (!Array.isArray(items)) return { list: [], error: strict ? "categories must be a list of names" : "" };
    let out = [];
    const seen = new Set();
    for (const raw of items) {
      if (typeof raw !== "string") {
        if (strict) return { list: [], error: "category names must be text" };
        continue;
      }
      let full = normalizeName(raw, false);
      if (!full) continue;
      const points = Array.from(full);
      if (points.length > MAX_NAME_LEN) {
        if (strict) return { list: [], error: `category '${points.slice(0, MAX_NAME_LEN).join("")}...' is longer than ${MAX_NAME_LEN} characters` };
        full = points.slice(0, MAX_NAME_LEN).join("").replace(/ +$/, "");
      }
      const key = full.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(full);
    }
    if (out.length > MAX_PER_ITEM) {
      if (strict) return { list: [], error: `at most ${MAX_PER_ITEM} categories per item` };
      out = out.slice(0, MAX_PER_ITEM);
    }
    return { list: out, error: "" };
  }

  // The categories in use across a set of items: [{ key, name, count }],
  // most-used first (ties alphabetical). `lists` is one array of names per
  // item. A name spelt two ways is ONE entry, shown in its most common
  // spelling (ties: the one seen first).
  function vocabulary(lists) {
    const byKey = new Map();
    (Array.isArray(lists) ? lists : []).forEach((list) => {
      const seen = new Set();
      (Array.isArray(list) ? list : []).forEach((raw) => {
        const name = normalizeName(raw);
        if (!name) return;
        const key = name.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        let rec = byKey.get(key);
        if (!rec) { rec = { key, count: 0, spellings: new Map() }; byKey.set(key, rec); }
        rec.count += 1;
        rec.spellings.set(name, (rec.spellings.get(name) || 0) + 1);
      });
    });
    const out = [];
    byKey.forEach((rec) => {
      let best = null;
      let bestN = -1;
      rec.spellings.forEach((n, spelling) => { if (n > bestN) { best = spelling; bestN = n; } });
      out.push({ key: rec.key, name: best, count: rec.count });
    });
    out.sort((a, b) => (b.count - a.count) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
    return out;
  }

  // Suggestions for what has been typed so far: names that START with it,
  // case-insensitive, most-used first, an exact match ahead of the rest, and
  // never one that is already on the item. Nothing typed yet lists everything.
  function suggest(vocab, typed, exclude, limit) {
    const want = keyOf(typed);
    const skip = new Set((exclude || []).map(keyOf));
    const hits = (vocab || []).filter((v) => !skip.has(v.key) && (!want || v.key.startsWith(want)));
    const exact = hits.filter((v) => v.key === want);
    const rest = hits.filter((v) => v.key !== want);
    return exact.concat(rest).slice(0, limit || 8);
  }

  // `name`, spelt the way the vocabulary already spells it, if it has it.
  function canonical(vocab, name) {
    const clean = normalizeName(name);
    const key = clean.toLowerCase();
    const hit = (vocab || []).find((v) => v.key === key);
    return hit ? hit.name : clean;
  }

  /* ---- widget ------------------------------------------------------------ */

  let uid = 0;

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

  // opts: { id, value: [names], getVocabulary: () => [{key,name,count}],
  //         onChange: (list) => void, label, placeholder, disabled }
  // Returns { el, input, inputId, getValue, setValue, focus }.
  function createInput(opts) {
    const o = opts || {};
    const idBase = o.id || `jcat-${++uid}`;
    const listId = `${idBase}-list`;
    let value = normalizeList(o.value).list;
    let shown = [];
    let hi = -1;
    let isOpen = false;
    let dismissed = false;

    const vocab = () => (typeof o.getVocabulary === "function" ? o.getVocabulary() || [] : []);

    const input = h("input", {
      class: "jcat__input", type: "text", id: idBase, role: "combobox", autocomplete: "off", spellcheck: "false",
      maxlength: String(MAX_NAME_LEN), "aria-autocomplete": "list", "aria-expanded": "false", "aria-controls": listId,
      "aria-label": o.label || "Categories",
    });
    const box = h("div", { class: "jcat__box" }, [input]);
    const list = h("ul", { class: "jcat__list", id: listId, role: "listbox", hidden: true, "aria-label": "Suggested categories" });
    // mousedown, not click: a click would blur the input first, which commits
    // the half-typed text and closes the list before the click lands.
    list.addEventListener("mousedown", (ev) => ev.preventDefault());
    const root = h("div", { class: "jcat" }, [box, list]);
    box.addEventListener("mousedown", (ev) => { if (ev.target === box) { ev.preventDefault(); input.focus(); } });

    function emit() {
      if (typeof o.onChange === "function") o.onChange(value.slice());
    }

    function renderChips() {
      box.querySelectorAll(".jcat__chip").forEach((n) => n.remove());
      value.forEach((name, i) => {
        const chip = h("span", { class: "jcat__chip", "data-key": name.toLowerCase() }, [
          h("span", { class: "jcat__name" }, name),
          h("button", {
            class: "jcat__x", type: "button", "aria-label": `Remove ${name}`, title: `Remove ${name}`,
            onclick: () => { removeAt(i); input.focus(); },
          }, "\u00D7"),
        ]);
        box.insertBefore(chip, input);
      });
      const full = value.length >= MAX_PER_ITEM;
      input.disabled = full || Boolean(o.disabled);
      input.placeholder = full ? `Up to ${MAX_PER_ITEM} categories` : (value.length ? "" : (o.placeholder || "Add a category\u2026"));
      root.classList.toggle("is-full", full);
    }

    function paintList() {
      list.textContent = "";
      shown.forEach((v, i) => {
        const li = h("li", {
          class: "jcat__opt" + (i === hi ? " is-hi" : ""), role: "option", id: `${idBase}-opt-${i}`,
          "aria-selected": i === hi ? "true" : "false",
        }, [
          h("span", { class: "jcat__optname" }, v.name),
          h("span", { class: "jcat__optn", title: `on ${v.count} item${v.count === 1 ? "" : "s"}` }, `\u00D7${v.count}`),
        ]);
        li.addEventListener("click", () => { commit(v.name); input.focus(); });
        list.appendChild(li);
      });
      list.hidden = !isOpen;
      input.setAttribute("aria-expanded", isOpen ? "true" : "false");
      if (isOpen && hi >= 0) input.setAttribute("aria-activedescendant", `${idBase}-opt-${hi}`);
      else input.removeAttribute("aria-activedescendant");
    }

    // Recompute what is suggested. Called when the text changes or the box
    // gains focus - NOT on arrow keys, which only move the highlight.
    function refresh() {
      shown = dismissed || input.disabled ? [] : suggest(vocab(), input.value, value, 8);
      isOpen = shown.length > 0;
      // With text typed, the best match is pre-highlighted so Enter/Tab
      // accepts it. With nothing typed the list is a menu to browse, and
      // Enter must not pick something nobody pointed at.
      hi = isOpen && keyOf(input.value) ? 0 : -1;
      paintList();
    }

    function close() { isOpen = false; hi = -1; paintList(); }

    function removeAt(i) {
      if (i < 0 || i >= value.length) return;
      value = value.slice(0, i).concat(value.slice(i + 1));
      renderChips();
      refresh();
      emit();
    }

    // Add one name. Returns whether anything was added; a duplicate or an
    // empty name is quietly ignored (the box is cleared either way).
    function commit(raw) {
      const name = canonical(vocab(), raw);
      input.value = "";
      dismissed = false;
      if (!name) { refresh(); return false; }
      const next = normalizeList(value.concat([name])).list;
      const added = next.length > value.length;
      if (added) { value = next; renderChips(); emit(); }
      refresh();
      return added;
    }

    const commitTyped = () => (keyOf(input.value) ? commit(input.value) : false);

    input.addEventListener("input", () => { dismissed = false; refresh(); });
    input.addEventListener("focus", () => { refresh(); });
    input.addEventListener("blur", () => { commitTyped(); close(); });
    input.addEventListener("keydown", (ev) => {
      const k = ev.key;
      if (k === "ArrowDown" || k === "ArrowUp") {
        if (!shown.length) { dismissed = false; refresh(); }
        if (!shown.length) return;
        ev.preventDefault();
        isOpen = true;
        const step = k === "ArrowDown" ? 1 : -1;
        hi = hi < 0 ? (step > 0 ? 0 : shown.length - 1) : (hi + step + shown.length) % shown.length;
        paintList();
      } else if (k === "Enter") {
        if (isOpen && hi >= 0) { ev.preventDefault(); commit(shown[hi].name); }
        else if (keyOf(input.value)) { ev.preventDefault(); commit(input.value); }
        // Empty box: leave Enter alone, so it still does whatever it does elsewhere.
      } else if (k === "Tab") {
        if (isOpen && hi >= 0) { ev.preventDefault(); commit(shown[hi].name); }
        // Otherwise Tab moves on, and blur keeps whatever was typed.
      } else if (k === ",") {
        ev.preventDefault();
        commitTyped();
      } else if (k === "Escape") {
        // Only claim Esc while there is a list to dismiss; otherwise it goes
        // on to close the form like any other field.
        if (isOpen) { ev.preventDefault(); ev.stopPropagation(); dismissed = true; close(); }
      } else if (k === "Backspace" && !input.value && value.length) {
        removeAt(value.length - 1);
      }
    });

    renderChips();
    return {
      el: root,
      input,
      inputId: idBase,
      getValue: () => value.slice(),
      setValue: (items) => { value = normalizeList(items).list; renderChips(); refresh(); },
      focus: () => input.focus(),
    };
  }

  // L.14: how many of a row's trailing chips fit beside its fixed badges.
  // `widths` is every item's width in order, the first `fixedCount` of them
  // fixed (always shown, whether or not they fit - they were there before
  // categories existed); the rest are chips. A chip that does not fit is left
  // out - never wrapped, never cut short - and so is everything after it, so
  // the chips shown are always the first ones, in the order they were set.
  // Returns the number of CHIPS to show. Pure, so it needs no browser to test.
  function fitCount(widths, available, gap, fixedCount) {
    const list = Array.isArray(widths) ? widths.map((w) => Math.max(0, Number(w) || 0)) : [];
    const fixed = Math.max(0, Math.min(list.length, Number(fixedCount) || 0));
    const space = Number(gap) || 0;
    let used = 0;
    for (let i = 0; i < list.length; i++) {
      used += (i > 0 ? space : 0) + list[i];
      if (i >= fixed && used > available) return i - fixed;
    }
    return list.length - fixed;
  }

  global.JarvisCategories = {
    MAX_NAME_LEN, MAX_PER_ITEM,
    normalizeName, keyOf, normalizeList, vocabulary, suggest, canonical,
    createInput, fitCount,
  };
})(typeof window !== "undefined" ? window : globalThis);
