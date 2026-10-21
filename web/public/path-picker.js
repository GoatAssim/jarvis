/* ============================================================================
 * path-picker.js - the folder / file picker (Download Hub N14, brief 14).
 *
 *   JarvisPathPicker.open({ kind: "folder" | "file", start, title, extensions })
 *       -> Promise<string | null>          the chosen full path, or null when cancelled
 *
 * A web page may not learn the full path of something the person picks, so the SERVER lists
 * folders (POST /api/hub/browse, rules in jarvis/hub_browse.py) and this file only draws the
 * answer: a breadcrumb, a places list, the folder list, a path box, Select, and (for folder
 * picks) New folder.  Nothing here reads a file's contents or decides what a path may be used
 * for: the hub still validates the path when the field that asked for it is saved.
 *
 * Keyboard: Up / Down / Home / End / PageUp / PageDown move, Enter (or Right) opens a folder or
 * picks a file, Backspace (or Left) goes up, typing filters the list, Ctrl+Enter selects,
 * Escape clears the filter, then closes.  The dialog traps Tab, restores the focus it was
 * opened from, and sits above other dialogs without letting their Escape handling see its keys.
 *
 * EVERYTHING IS TEXT.  File and folder names come from the disk (anyone could have named
 * something "<img onerror=...>"): they only ever reach the page through textContent, never as
 * markup.  The pure helpers are exported as JarvisPathPicker._pure for tests/verify_path_picker.js.
 * ========================================================================= */
(function (global) {
  "use strict";

  const ROUTE = "/api/hub/browse";
  const ROUTE_NEW = "/api/hub/browse/new-folder";
  const PAGE = 10;                       // PageUp / PageDown step

  /* =======================================================================
   * PURE HELPERS - no DOM
   * ===================================================================== */

  function str(v) { return typeof v === "string" ? v : ""; }

  // Case-insensitive "contains" over the names; an empty filter keeps everything.
  function filterEntries(entries, text) {
    const list = Array.isArray(entries) ? entries : [];
    const needle = str(text).trim().toLowerCase();
    if (!needle) return list.slice();
    return list.filter((e) => str(e && e.name).toLowerCase().includes(needle));
  }

  // Move the highlighted row by `delta`, staying inside the list (-1 = nothing highlighted).
  function moveIndex(current, delta, count) {
    if (!count || count < 1) return -1;
    if (current < 0) return delta < 0 ? count - 1 : 0;
    return Math.max(0, Math.min(count - 1, current + delta));
  }

  function fmtSize(bytes) {
    if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "";
    if (bytes < 1024) return bytes + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let n = bytes / 1024;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (n >= 100 || i === 0 && n >= 10 ? Math.round(n) : Math.round(n * 10) / 10) + " " + units[i];
  }

  function fmtDate(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "";
    const d = new Date(seconds * 1000);
    if (Number.isNaN(d.getTime())) return "";
    const pad = (n) => (n < 10 ? "0" : "") + n;
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  // The label of the Select button: it follows what is highlighted.
  function selectLabel(kind, highlighted, listing) {
    const name = highlighted ? str(highlighted.name) : "";
    const short = name.length > 28 ? name.slice(0, 27) + "\u2026" : name;
    if (kind === "file") return "Select this file";
    if (highlighted) return "Select \u201c" + short + "\u201d";
    return listing && listing.path ? "Select this folder" : "Select";
  }

  // What Select would return right now, or "" when it cannot select anything yet.
  function selectTarget(kind, highlighted, listing) {
    if (kind === "file") return highlighted && highlighted.kind === "file" ? str(highlighted.path) : "";
    if (highlighted && highlighted.kind === "folder") return str(highlighted.path);
    return listing ? str(listing.path) : "";
  }

  // `picked` as a sub-folder of `root` ("Games/Old"), with "/" separators, or null when it is not inside it
  // (or is the root itself).  Windows paths compare without case and with either slash.
  function insideRoot(root, picked) {
    const r = str(root).trim();
    const p = str(picked).trim();
    if (!r || !p) return null;
    const windows = /^[A-Za-z]:/.test(r) || r.includes("\\");
    const norm = (s) => {
      let t = s.replace(/\\/g, "/");
      if (t.length > 1) t = t.replace(/\/+$/, "");
      return windows ? t.toLowerCase() : t;
    };
    const a = norm(r);
    const b = norm(p);
    if (b === a || !b.startsWith(a + "/")) return null;
    const rel = p.replace(/\\/g, "/").replace(/\/+$/, "").slice(a.length + 1);
    return rel || null;
  }

  // The start folder a caller should hand over: the field's text when it is a full path, the library root
  // joined with it when it is a sub-folder, the library root (or "") when it is empty.
  function startFor(fieldText, libraryRoot) {
    const text = str(fieldText).trim();
    const root = str(libraryRoot).trim();
    const isAbs = /^([A-Za-z]:|[\\/~])/.test(text);
    if (text && isAbs) return text;
    if (text && root) return root.replace(/[\\/]+$/, "") + (root.includes("\\") ? "\\" : "/") + text.replace(/\//g, root.includes("\\") ? "\\" : "/");
    return root;
  }

  /* =======================================================================
   * THE DIALOG
   * ===================================================================== */

  let active = null;                      // only one picker at a time
  let uid = 0;

  function el(tag, props, children) {
    const node = document.createElement(tag);
    const p = props || {};
    for (const key of Object.keys(p)) {
      const v = p[key];
      if (v === undefined || v === null || v === false) continue;
      if (key === "cls") node.className = v;
      else if (key === "text") node.textContent = String(v);
      else if (key === "on") { for (const ev of Object.keys(v)) node.addEventListener(ev, v[ev]); }
      else node.setAttribute(key, v === true ? "" : String(v));
    }
    for (const c of children || []) if (c) node.appendChild(c);
    return node;
  }

  async function post(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Jarvis-Hub": "1" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      return { ok: false, error: "Couldn't reach Jarvis. Is it still running?" };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!data || typeof data !== "object") return { ok: false, error: "Jarvis gave an answer the picker can't read." };
    if (!res.ok && data.ok !== false) return { ok: false, error: str(data.error) || "The picker was refused." };
    if (data.ok === undefined) data.ok = res.ok;
    if (data.ok === false && !data.error) data.error = str(data.message) || "That didn't work.";
    return data;
  }

  function open(options) {
    const o = options && typeof options === "object" ? options : {};
    const kind = o.kind === "file" ? "file" : "folder";
    const title = str(o.title) || (kind === "file" ? "Choose a file" : "Choose a folder");
    const extensions = Array.isArray(o.extensions) ? o.extensions.filter((e) => typeof e === "string").slice(0, 24) : [];
    const hidden = o.showHidden === true;
    if (typeof document === "undefined") return Promise.resolve(null);
    if (active) active.close(null);

    return new Promise((resolve) => {
      const id = ++uid;
      const S = {
        listing: null,                   // the last answer from the server
        shown: [],                       // entries after the filter
        index: -1,                       // highlighted row in `shown`
        filter: "",
        busy: false,
        token: 0,                        // answers to older requests are ignored
        newOpen: false,
      };
      const previousFocus = document.activeElement;
      let finished = false;

      // ---- parts
      const titleId = "jpp-title-" + id;
      const noteId = "jpp-note-" + id;
      const listId = "jpp-list-" + id;
      const closeBtn = el("button", { cls: "jpp__x", type: "button", "aria-label": "Close", text: "\u00d7", on: { click: () => finish(null) } });
      const crumbs = el("ol", { cls: "jpp__crumbs-list" });
      const places = el("ul", { cls: "jpp__places-list" });
      const note = el("div", { cls: "jpp__note", id: noteId, role: "status", "aria-live": "polite" });
      const list = el("ul", { cls: "jpp__list", id: listId, role: "listbox", tabindex: "0", "aria-label": kind === "file" ? "Files and folders" : "Folders", "aria-describedby": "jpp-hint-" + id });
      const more = el("div", { cls: "jpp__more" });
      const filterBar = el("div", { cls: "jpp__filter", hidden: "" });
      const hint = el("div", { cls: "jpp__hint", id: "jpp-hint-" + id,
        text: "Enter opens \u00b7 Backspace goes up \u00b7 type to filter \u00b7 Ctrl+Enter selects" });
      const pathBox = el("input", { cls: "jpp__path", type: "text", spellcheck: "false", autocomplete: "off", "aria-label": "Path", placeholder: "Type or paste a full path, then press Enter" });
      const goBtn = el("button", { cls: "jpp__btn", type: "button", text: "Go", on: { click: () => load(pathBox.value, true) } });
      const upBtn = el("button", { cls: "jpp__btn", type: "button", text: "Up", "aria-label": "Go up one folder", on: { click: () => goUp() } });
      const newInput = el("input", { cls: "jpp__newname", type: "text", spellcheck: "false", autocomplete: "off", "aria-label": "Name of the new folder", placeholder: "Name of the new folder" });
      const newMake = el("button", { cls: "jpp__btn jpp__btn--main", type: "button", text: "Create", on: { click: () => makeFolder() } });
      const newCancel = el("button", { cls: "jpp__btn", type: "button", text: "Cancel", on: { click: () => closeNew(true) } });
      const newRow = el("div", { cls: "jpp__newrow", hidden: "" }, [newInput, newMake, newCancel]);
      const errorBox = el("div", { cls: "jpp__error", role: "alert" });
      const newBtn = kind === "folder"
        ? el("button", { cls: "jpp__btn", type: "button", text: "New folder", on: { click: () => openNew() } })
        : null;
      const cancelBtn = el("button", { cls: "jpp__btn", type: "button", text: "Cancel", on: { click: () => finish(null) } });
      const selectBtn = el("button", { cls: "jpp__btn jpp__btn--main", type: "button", text: "Select", on: { click: () => selectNow() } });

      const card = el("div", { cls: "jpp", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId }, [
        el("div", { cls: "jpp__head" }, [el("h2", { cls: "jpp__title", id: titleId, text: title }), closeBtn]),
        el("nav", { cls: "jpp__crumbs", "aria-label": "Folder path" }, [crumbs]),
        el("div", { cls: "jpp__body" }, [
          el("nav", { cls: "jpp__places", "aria-label": "Places" }, [places]),
          el("div", { cls: "jpp__main" }, [note, list, more, filterBar, hint]),
        ]),
        el("div", { cls: "jpp__pathrow" }, [upBtn, pathBox, goBtn]),
        newRow,
        errorBox,
        el("div", { cls: "jpp__foot" }, [newBtn, el("span", { cls: "jpp__spacer" }), cancelBtn, selectBtn]),
      ]);
      const backdrop = el("div", { cls: "jpp-backdrop" }, [card]);

      // ---- finishing
      function finish(value) {
        if (finished) return;
        finished = true;
        global.removeEventListener("keydown", onKey, true);
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        if (active && active.id === id) active = null;
        try { if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus(); } catch (e) { /* gone */ }
        resolve(value);
      }
      active = { id, close: finish };

      // ---- drawing
      function highlighted() { return S.index >= 0 && S.index < S.shown.length ? S.shown[S.index] : null; }

      function setError(message) { errorBox.textContent = message || ""; }

      function drawCrumbs() {
        crumbs.textContent = "";
        const L = S.listing;
        const here = L && L.path ? L.crumbs || [] : [];
        const first = el("li", {}, [
          here.length || !L ? el("button", { cls: "jpp__crumb", type: "button", text: "This computer", on: { click: () => load("", false) } })
            : el("span", { cls: "jpp__crumb jpp__crumb--here", text: "This computer", "aria-current": "page" }),
        ]);
        crumbs.appendChild(first);
        here.forEach((c, i) => {
          const last = i === here.length - 1;
          crumbs.appendChild(el("li", {}, [
            last ? el("span", { cls: "jpp__crumb jpp__crumb--here", text: str(c.name), "aria-current": "page" })
              : el("button", { cls: "jpp__crumb", type: "button", text: str(c.name), on: { click: () => load(str(c.path), false) } }),
          ]));
        });
        const lastLi = crumbs.lastElementChild;
        if (lastLi && lastLi.scrollIntoView) { try { lastLi.scrollIntoView({ block: "nearest", inline: "end" }); } catch (e) { /* old browser */ } }
      }

      function drawPlaces() {
        places.textContent = "";
        const L = S.listing;
        for (const p of (L && L.places) || []) {
          const here = L.path && str(p.path).toLowerCase() === str(L.path).toLowerCase();
          places.appendChild(el("li", {}, [
            el("button", { cls: "jpp__place" + (here ? " is-here" : ""), type: "button", text: str(p.name), "aria-current": here ? "true" : null, on: { click: () => load(str(p.path), false) } }),
          ]));
        }
      }

      function drawList() {
        const L = S.listing;
        list.textContent = "";
        list.setAttribute("aria-busy", S.busy ? "true" : "false");
        S.shown = L ? filterEntries(L.entries, S.filter) : [];
        if (S.index >= S.shown.length) S.index = S.shown.length - 1;
        if (!L) {
          list.appendChild(el("li", { cls: "jpp__empty", role: "presentation", text: "Loading\u2026" }));
        } else if (!S.shown.length) {
          const text = S.filter ? "Nothing here matches \u201c" + S.filter + "\u201d."
            : !L.path ? "Nothing to show."
            : kind === "file" ? "No sub-folders or matching files here." : "No sub-folders here. You can still select this folder.";
          list.appendChild(el("li", { cls: "jpp__empty", role: "presentation", text }));
        }
        S.shown.forEach((e, i) => {
          const isFolder = e.kind === "folder";
          const facts = isFolder ? "" : [fmtSize(e.size), fmtDate(e.modified)].filter(Boolean).join("  \u00b7  ");
          const row = el("li", {
            cls: "jpp__row" + (i === S.index ? " is-on" : "") + (e.link ? " is-link" : ""),
            role: "option", id: listId + "-" + i, "aria-selected": i === S.index ? "true" : "false",
            "aria-label": str(e.name) + (isFolder ? ", folder" : ", file"), title: str(e.name),
            on: {
              click: () => { setIndex(i, false); list.focus(); },
              dblclick: () => { setIndex(i, false); activate(); },
            },
          }, [
            el("span", { cls: "jpp__ico jpp__ico--" + (isFolder ? "folder" : "file"), "aria-hidden": "true" }),
            el("span", { cls: "jpp__name", text: str(e.name) }),
            facts ? el("span", { cls: "jpp__facts", text: facts }) : null,
          ]);
          list.appendChild(row);
        });
        if (S.index >= 0) list.setAttribute("aria-activedescendant", listId + "-" + S.index);
        else list.removeAttribute("aria-activedescendant");
        const left = L && L.more > 0 ? L.more : 0;
        more.textContent = left
          ? "\u2026 and " + left + " more" + (L.scan_capped ? " (a very large folder; type a name to narrow it, or paste the path above)" : ". Type a name to narrow the list.")
          : "";
        filterBar.hidden = !S.filter;
        filterBar.textContent = S.filter ? "Filter: " + S.filter + "   (Backspace deletes, Escape clears)" : "";
        drawButtons();
      }

      function drawButtons() {
        const L = S.listing;
        const h = highlighted();
        selectBtn.textContent = selectLabel(kind, h, L);
        selectBtn.disabled = !selectTarget(kind, h, L) || S.busy;
        upBtn.disabled = !L || !L.path;
        if (newBtn) newBtn.disabled = !L || !L.path || S.busy;
      }

      function setIndex(i, scroll) {
        S.index = i;
        const rows = list.querySelectorAll(".jpp__row");
        rows.forEach((r, n) => {
          const on = n === i;
          r.classList.toggle("is-on", on);
          r.setAttribute("aria-selected", on ? "true" : "false");
          if (on && scroll !== false && r.scrollIntoView) { try { r.scrollIntoView({ block: "nearest" }); } catch (e) { /* old browser */ } }
        });
        if (i >= 0) list.setAttribute("aria-activedescendant", listId + "-" + i);
        else list.removeAttribute("aria-activedescendant");
        drawButtons();
      }

      // ---- talking to the server
      async function load(path, typed) {
        const token = ++S.token;
        S.busy = true;
        setError("");
        list.setAttribute("aria-busy", "true");
        drawButtons();
        const answer = await post(ROUTE, { path: str(path), kind, extensions, hidden });
        if (token !== S.token || finished) return;
        S.busy = false;
        if (!answer.ok) {
          setError(str(answer.error) || "That folder couldn't be opened.");
          if (!S.listing) { await load("", false); return; }            // the very first answer failed: show the roots
          drawList();
          return;
        }
        S.listing = answer;
        S.filter = "";
        S.index = -1;
        note.textContent = str(answer.note);
        pathBox.value = str(answer.path);
        drawCrumbs();
        drawPlaces();
        S.shown = filterEntries(answer.entries, "");
        const wanted = str(answer.selected);
        if (wanted) {
          const at = S.shown.findIndex((e) => e.name === wanted);
          if (at >= 0) S.index = at;
        } else if (S.shown.length) {
          S.index = -1;
        }
        drawList();
        if (S.index >= 0) setIndex(S.index, true);
        if (typed && kind === "file" && wanted && S.index >= 0 && S.shown[S.index].kind === "file") { finish(str(S.shown[S.index].path)); return; }
        if (document.activeElement !== pathBox && document.activeElement !== newInput) list.focus();
      }

      function goUp() {
        const L = S.listing;
        if (!L || !L.path) return;
        load(L.parent ? str(L.parent) : "", false);
      }

      function activate() {
        const h = highlighted();
        if (!h) return;
        if (h.kind === "folder") { load(str(h.path), false); return; }
        if (kind === "file") finish(str(h.path));
      }

      function selectNow() {
        const target = selectTarget(kind, highlighted(), S.listing);
        if (target) finish(target);
      }

      // ---- new folder
      function openNew() {
        if (!S.listing || !S.listing.path) return;
        S.newOpen = true;
        newRow.hidden = false;
        newInput.value = "";
        setError("");
        newInput.focus();
      }
      function closeNew(refocus) {
        S.newOpen = false;
        newRow.hidden = true;
        if (refocus) list.focus();
      }
      async function makeFolder() {
        const L = S.listing;
        if (!L || !L.path) return;
        const name = newInput.value.trim();
        if (!name) { setError("Give the new folder a name."); newInput.focus(); return; }
        newMake.disabled = true;
        const answer = await post(ROUTE_NEW, { parent: str(L.path), name });
        newMake.disabled = false;
        if (finished) return;
        if (!answer.ok) { setError(str(answer.error) || "Couldn't make that folder."); newInput.focus(); return; }
        closeNew(false);
        await load(str(answer.path), false);                       // go into it, so Select picks it
        list.focus();
      }

      // ---- keys (one handler on the window, in the capture phase: it sees the key before any dialog
      // underneath, so their Escape / Tab handling never fires for keys that belong to the picker)
      function focusables() {
        return Array.from(card.querySelectorAll("button, input, [tabindex]:not([tabindex=\"-1\"])"))
          .filter((n) => !n.disabled && !n.hidden && n.offsetParent !== null);
      }

      function onKey(e) {
        if (finished) return;
        const inside = card.contains(e.target);
        if (!inside) {
          if (e.key === "Tab") { e.preventDefault(); e.stopPropagation(); const f = focusables(); if (f.length) f[0].focus(); }
          return;
        }
        const consume = () => { e.preventDefault(); e.stopPropagation(); };
        const target = e.target;
        const inText = target === pathBox || target === newInput;

        if (e.key === "Escape") {
          consume();
          if (S.newOpen) { closeNew(true); return; }
          if (S.filter && !inText) { S.filter = ""; S.index = -1; drawList(); return; }
          finish(null);
          return;
        }
        if (e.key === "Tab") {
          const f = focusables();
          if (!f.length) return;
          const first = f[0];
          const last = f[f.length - 1];
          if (e.shiftKey && target === first) { consume(); last.focus(); }
          else if (!e.shiftKey && target === last) { consume(); first.focus(); }
          return;
        }
        if (target === pathBox) {
          if (e.key === "Enter") { consume(); load(pathBox.value, true); }
          return;
        }
        if (target === newInput) {
          if (e.key === "Enter") { consume(); makeFolder(); }
          return;
        }
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && target !== newInput) { consume(); selectNow(); return; }

        // the keys below belong to the list; a button keeps its own Enter / Space and arrows
        if (target !== list) return;
        const count = S.shown.length;
        switch (e.key) {
          case "ArrowDown": consume(); setIndex(moveIndex(S.index, 1, count), true); return;
          case "ArrowUp":
            consume();
            if (S.index === 0 && kind === "folder") setIndex(-1, true);          // back to "select this folder"
            else setIndex(moveIndex(S.index, -1, count), true);
            return;
          case "PageDown": consume(); setIndex(moveIndex(S.index, PAGE, count), true); return;
          case "PageUp": consume(); setIndex(moveIndex(S.index, -PAGE, count), true); return;
          case "Home": consume(); if (count) setIndex(0, true); return;
          case "End": consume(); if (count) setIndex(count - 1, true); return;
          case "Enter": consume(); activate(); return;
          case "ArrowRight": { const h = highlighted(); if (h && h.kind === "folder") { consume(); activate(); } return; }
          case "ArrowLeft": consume(); goUp(); return;
          case "Backspace":
            consume();
            if (S.filter) { S.filter = S.filter.slice(0, -1); S.index = S.filter ? 0 : -1; drawList(); if (S.index >= 0) setIndex(S.index, true); }
            else goUp();
            return;
          default: break;
        }
        // type to filter: a printable key with no Ctrl / Alt / Meta (a space only inside a filter that has started)
        if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey && (e.key !== " " || S.filter)) {
          consume();
          S.filter += e.key;
          S.index = 0;
          drawList();
          if (S.shown.length) setIndex(0, true); else S.index = -1;
        }
      }
      global.addEventListener("keydown", onKey, true);

      // ---- show it
      (document.body || document.documentElement).appendChild(backdrop);
      drawCrumbs();
      drawList();
      list.focus();
      load(str(o.start), false);
    });
  }

  global.JarvisPathPicker = {
    open,
    startFor,                            // a field's text + the library root -> the folder to open in
    insideRoot,                          // a picked path -> "Sub/Folder" when it is inside the library root
    _pure: { filterEntries, moveIndex, fmtSize, fmtDate, selectLabel, selectTarget, insideRoot, startFor },
  };
})(typeof window !== "undefined" ? window : globalThis);
