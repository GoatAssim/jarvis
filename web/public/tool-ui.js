/* ============================================================================
 * tool-ui.js — the screens tool files ship (TOOL_UI, jarvis/tool_ui.py).
 *
 * WHAT IT DOES
 * ------------
 * A tool file can declare TOOL_UI elements: a page it wrote (tool.html / tool.js /
 * tool.css, in a folder it names) and HOW that page is reached:
 *
 *   mode "button"  a button beside the Menu button. Clicking it opens the page in a
 *                  window of its own, inside a SANDBOXED frame: the page can't see or
 *                  touch the rest of Jarvis, only the `host` object below.
 *   mode "menu"    an entry in the Menu. Clicking it opens a panel; the tool's HTML
 *                  goes into a div (a shadow root, so its CSS can't leak out and
 *                  Jarvis's can't leak in) and its JS runs against that div.
 *
 * This file only DISPLAYS what the server hands it. Which elements exist, which the
 * owner switched off in the Tool Manager, and what is inside each folder are all
 * decided by the CLI (`jarvis tool-ui`); the browser names an element id, never a path.
 *
 * THE `host` OBJECT (the same in both modes)
 * ------------------------------------------
 *   host.id, host.mode, host.tools   which element this is, and the tools of its file
 *   host.root                        what to query: the frame's document (button) or the
 *                                    panel's shadow root (menu)
 *   host.runTool(name, args)         run one of host.tools -> Promise<that tool's result>.
 *                                    A tool that asks first still asks first (the same
 *                                    confirmation as the Debug panel); declining rejects.
 *   host.toast(message, level)       a corner message ("info" | "success" | "warn" | "error")
 *   host.setTitle(text)              the window / panel heading
 *   host.close()                     close it
 *   host.storage.get/set/remove      small per-element values kept in this browser (Promise)
 *   host.onClose(fn)                 run fn when it closes (menu mode; in a frame the page
 *                                    is simply removed)
 *
 * TRUST
 * -----
 * A tool's page is its author's code, like its Python: written by the person who
 * installed it, never by the model (nothing model-facing can create one). Button mode
 * is sandboxed anyway; Menu mode shares the document, so read a menu panel's JS before
 * installing someone else's. Everything the PAGE shows is its own; everything Jarvis
 * shows about it (labels, hints, errors) is inserted as text, never markup.
 * ========================================================================= */

(function (global) {
  "use strict";

  /* =======================================================================
   * PURE HELPERS — no DOM, exported as JarvisToolUI._pure for tests.
   * ===================================================================== */

  const ID_RE = /^[a-z][a-z0-9_]{0,48}$/;
  const MODES = ["button", "menu"];
  const STORAGE_PREFIX = "jarvis-tool-ui:";
  const MAX_STORED_CHARS = 100000;
  // The CSS variables a sandboxed frame is given (a frame doesn't inherit them).
  const THEME_VARS = [
    "--bg", "--bg-1", "--bg-panel", "--bg-panel-2", "--bg-raised", "--accent", "--accent-soft", "--accent-dim",
    "--accent-glow", "--accent-secondary", "--accent-tertiary", "--border", "--border-strong", "--text",
    "--text-dim", "--text-dimmer", "--red", "--green", "--font-mono", "--font-display", "--radius",
  ];

  // The elements worth showing, from a /api/tool-ui payload. Defensive: the payload
  // is JSON from our own server, but a malformed entry must never break the bar.
  function cleanList(payload) {
    const raw = payload && Array.isArray(payload.ui) ? payload.ui : [];
    const seen = new Set();
    const out = [];
    raw.forEach((u) => {
      if (!u || typeof u !== "object" || typeof u.id !== "string" || !ID_RE.test(u.id) || seen.has(u.id)) return;
      if (!MODES.includes(u.mode) || u.disabled) return;
      const label = String(u.label || "").trim();
      if (!label) return;
      seen.add(u.id);
      out.push({
        id: u.id, mode: u.mode, label: label.slice(0, 40), title: String(u.title || label).slice(0, 80),
        hint: String(u.hint || "").slice(0, 140), icon: String(u.icon || "").slice(0, 4),
        tool: typeof u.tool === "string" ? u.tool : "", tools: Array.isArray(u.tools) ? u.tools.filter((t) => typeof t === "string") : [],
      });
    });
    return out;
  }

  const byMode = (items, mode) => items.filter((u) => u.mode === mode);

  // A tool the page may run: only those of the element's own file.
  function toolAllowed(item, name) {
    return !!item && typeof name === "string" && Array.isArray(item.tools) && item.tools.includes(name);
  }

  function storageKey(id, key) {
    return STORAGE_PREFIX + String(id) + ":" + String(key);
  }

  function storageKeyProblem(key) {
    if (typeof key !== "string" || !key || key.length > 80) return "storage keys must be 1-80 characters";
    return "";
  }

  // The risk line shown in the confirmation. `note` may be a string or {note,...}.
  function riskText(preview) {
    if (!preview) return "";
    const n = preview.risk_note;
    if (!n) return "";
    if (typeof n === "string") return n;
    return String(n.note || n.summary || "");
  }

  // "</script" inside the tool's JS would end the script block that carries it.
  function escapeScript(js) {
    return String(js == null ? "" : js).replace(/<\/(script)/gi, "<\\/$1");
  }

  function escapeStyle(css) {
    return String(css == null ? "" : css).replace(/<\/(style)/gi, "<\\/$1");
  }

  function varsBlock(vars) {
    const lines = [];
    Object.keys(vars || {}).forEach((k) => {
      const v = String(vars[k] == null ? "" : vars[k]).trim();
      if (/^--[a-z0-9-]+$/.test(k) && v && !/[{};<>]/.test(v)) lines.push(k + ":" + v + ";");
    });
    return ":root{" + lines.join("") + "}";
  }

  // The shim that gives a sandboxed page its `host`. Runs inside the frame, talks to
  // the parent only by postMessage. Kept as one string so it can be tested.
  const FRAME_SHIM = [
    "(function(){",
    "var pending={},seq=0,closers=[];",
    "function call(op,data){return new Promise(function(res,rej){var id=++seq;pending[id]={res:res,rej:rej};",
    "parent.postMessage(Object.assign({jv:1,id:id,op:op},data||{}),'*');});}",
    "window.addEventListener('message',function(e){var m=e.data;if(!m||m.jv!==1)return;",
    "if(m.op==='closing'){closers.forEach(function(f){try{f()}catch(_){}});return;}",
    "var p=pending[m.reply];if(!p)return;delete pending[m.reply];",
    "if(m.ok)p.res(m.value);else p.rej(new Error(m.error||'failed'));});",
    "window.host={id:HOST_INFO.id,mode:'button',tools:HOST_INFO.tools,root:document,",
    "runTool:function(n,a){return call('runTool',{tool:n,args:a||{}});},",
    "toast:function(m,l){parent.postMessage({jv:1,id:0,op:'toast',message:String(m),level:l||'info'},'*');},",
    "setTitle:function(t){parent.postMessage({jv:1,id:0,op:'title',text:String(t)},'*');},",
    "close:function(){parent.postMessage({jv:1,id:0,op:'close'},'*');},",
    "onClose:function(f){if(typeof f==='function')closers.push(f);},",
    "storage:{get:function(k){return call('get',{key:k});},set:function(k,v){return call('set',{key:k,value:v});},",
    "remove:function(k){return call('remove',{key:k});}}};",
    "window.addEventListener('error',function(e){window.host.toast(e.message||'script error','error');});",
    "window.addEventListener('unhandledrejection',function(e){window.host.toast(String(e.reason&&e.reason.message||e.reason),'error');});",
    "})();",
  ].join("");

  // The document a sandboxed frame loads. Sections are escaped so none can end its own
  // block early; the tool's JS runs as a module (top-level await works, errors are
  // reported through the toast above).
  function buildSrcdoc(bundle, vars, info) {
    const b = bundle || {};
    const hostInfo = JSON.stringify({ id: String((info && info.id) || ""), tools: (info && info.tools) || [] }).replace(/</g, "\\u003c");
    return "<!doctype html><html><head><meta charset=\"utf-8\">"
      + "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
      + "<style>" + escapeStyle(varsBlock(vars)) + "html,body{margin:0;background:var(--bg-panel,#0b1118);color:var(--text,#dde)}</style>"
      + "<style>" + escapeStyle(b.css) + "</style></head><body>" + String(b.html || "")
      + "<script>var HOST_INFO=" + hostInfo + ";" + escapeScript(FRAME_SHIM) + "</script>"
      + (b.js ? "<script type=\"module\">" + escapeScript(b.js) + "</script>" : "")
      + "</body></html>";
  }

  const PURE = {
    ID_RE, MODES, THEME_VARS, STORAGE_PREFIX, MAX_STORED_CHARS, FRAME_SHIM,
    cleanList, byMode, toolAllowed, storageKey, storageKeyProblem, riskText,
    escapeScript, escapeStyle, varsBlock, buildSrcdoc,
  };

  /* =======================================================================
   * DOM / state
   * ===================================================================== */

  const UI = () => global.JarvisUI;
  const toast = (message, level) => { try { UI().toast({ message: String(message), level: level || "info" }); } catch (_) { /* ui-kit missing */ } };

  const state = { items: [], loaded: false, open: new Map() };   // id -> { close() }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok) {
      const err = new Error((body && (body.error || body.message)) || res.statusText || ("HTTP " + res.status));
      err.data = body;
      throw err;
    }
    return body;
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    (children === undefined ? [] : Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  /* ---- the host a page talks to --------------------------------------------- */

  async function runTool(item, name, args) {
    if (!toolAllowed(item, name)) throw new Error(name + " is not one of this page's tools");
    const arguments_ = args && typeof args === "object" && !Array.isArray(args) ? args : {};
    // Same gate as the Debug panel: ask the server whether the call would be confirmed,
    // and show it, before anything runs. Declining runs nothing.
    let preview = null;
    try { preview = await api("/api/tools/preview", { method: "POST", body: JSON.stringify({ name, arguments: arguments_ }) }); }
    catch (e) { throw new Error("Couldn't check whether " + name + " needs confirmation: " + e.message); }
    if (preview && (preview.confirm_required || preview.ai_review)) {
      const note = riskText(preview);
      const ok = await UI().confirm({
        title: "Run " + name + "?", level: "warn", focusCancel: true, confirmLabel: "Run",
        body: "The page " + item.title + " wants to run this tool." + (note ? "\n\n" + note : "")
          + "\n\n" + JSON.stringify(arguments_, null, 2).slice(0, 1200),
      });
      if (!ok) throw new Error("Cancelled — you declined to run " + name + ".");
    }
    const r = await api("/api/tool-ui/" + encodeURIComponent(item.id) + "/run", { method: "POST", body: JSON.stringify({ tool: name, arguments: arguments_ }) });
    const result = r ? r.result : null;
    if (r && r.ok === false && result && result.error && !Object.prototype.hasOwnProperty.call(result, "ok")) throw new Error(result.error);
    return result;
  }

  function storageGet(item, key) {
    const bad = storageKeyProblem(key);
    if (bad) return Promise.reject(new Error(bad));
    try {
      const raw = global.localStorage.getItem(storageKey(item.id, key));
      return Promise.resolve(raw === null ? null : JSON.parse(raw));
    } catch (e) { return Promise.reject(new Error("storage unavailable: " + e.message)); }
  }

  function storageSet(item, key, value) {
    const bad = storageKeyProblem(key);
    if (bad) return Promise.reject(new Error(bad));
    try {
      const text = JSON.stringify(value === undefined ? null : value);
      if (text.length > MAX_STORED_CHARS) return Promise.reject(new Error("value is too large (max " + MAX_STORED_CHARS + " characters)"));
      global.localStorage.setItem(storageKey(item.id, key), text);
      return Promise.resolve(true);
    } catch (e) { return Promise.reject(new Error("storage unavailable: " + e.message)); }
  }

  function storageRemove(item, key) {
    const bad = storageKeyProblem(key);
    if (bad) return Promise.reject(new Error(bad));
    try { global.localStorage.removeItem(storageKey(item.id, key)); return Promise.resolve(true); }
    catch (e) { return Promise.reject(new Error("storage unavailable: " + e.message)); }
  }

  /* ---- the window (both modes share the chrome) ----------------------------------- */

  // overlay > panel > head (title + close) > body. Same classes the other panels use,
  // so the frame, corners, glow and the skin all apply untouched.
  function makeWindow(item, bundle) {
    const titleEl = el("div", { class: "menu-panel__title" });
    titleEl.textContent = String(bundle.title || item.title).toUpperCase();
    const closeBtn = el("button", { class: "modal__close", type: "button", "aria-label": "Close" }, "\u00d7");
    const body = el("div", { class: "tool-ui-body" });
    const panel = el("div", { class: "menu-panel menu-panel--tool-ui", role: "dialog", "aria-modal": "true", "aria-label": item.title }, [
      el("div", { class: "panel__corner panel__corner--tl" }), el("div", { class: "panel__corner panel__corner--br" }),
      el("div", { class: "menu-panel__head" }, [el("div", { class: "menu-panel__titles" }, [titleEl]), closeBtn]),
      body,
    ]);
    const overlay = el("div", { class: "menu-overlay tool-ui-overlay", "data-tool-ui-id": item.id }, [panel]);
    const prevFocus = document.activeElement;
    document.body.appendChild(overlay);
    return { overlay, panel, body, titleEl, closeBtn, prevFocus };
  }

  function wireClose(win, item, teardown) {
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey, true);
      try { teardown(); } catch (_) { /* a page's cleanup must not strand the overlay */ }
      if (win.overlay.parentNode) win.overlay.parentNode.removeChild(win.overlay);
      state.open.delete(item.id);
      if (win.prevFocus && win.prevFocus.focus && document.contains(win.prevFocus)) { try { win.prevFocus.focus(); } catch (_) { /* gone */ } }
    };
    function onKey(e) {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (document.querySelector("#jui-layer .jui-modal")) return;     // a confirmation is on top: it owns Escape
      e.preventDefault(); e.stopPropagation(); close();
    }
    document.addEventListener("keydown", onKey, true);
    win.closeBtn.addEventListener("click", close);
    win.overlay.addEventListener("mousedown", (e) => { if (e.target === win.overlay) close(); });
    return close;
  }

  async function fetchBundle(item) {
    try { return await api("/api/tool-ui/" + encodeURIComponent(item.id) + "/bundle"); }
    catch (e) {
      toast(item.label + ": " + e.message, "error");
      if (e.data && e.data.disabled) refresh();
      return null;
    }
  }

  function themeVars() {
    const cs = getComputedStyle(document.documentElement);
    const out = {};
    THEME_VARS.forEach((k) => { const v = cs.getPropertyValue(k).trim(); if (v) out[k] = v; });
    return out;
  }

  /* ---- mode "button": a sandboxed frame ---------------------------------------------- */

  async function openButton(item) {
    if (state.open.has(item.id)) { state.open.get(item.id).focus(); return; }
    const bundle = await fetchBundle(item);
    if (!bundle) return;
    (bundle.problems || []).forEach((p) => toast(item.label + ": " + p, "warn"));
    const win = makeWindow(item, bundle);
    const frame = el("iframe", { class: "tool-ui-frame", title: item.title, sandbox: "allow-scripts" });
    frame.srcdoc = buildSrcdoc(bundle, themeVars(), { id: item.id, tools: item.tools });
    win.body.appendChild(frame);

    const onMessage = async (e) => {
      if (e.source !== frame.contentWindow) return;       // only OUR frame, nobody else's window
      const m = e.data;
      if (!m || m.jv !== 1) return;
      const reply = (ok, payload) => { try { frame.contentWindow.postMessage(Object.assign({ jv: 1, reply: m.id, ok }, ok ? { value: payload } : { error: String(payload) }), "*"); } catch (_) { /* frame gone */ } };
      try {
        if (m.op === "toast") toast(m.message, ["info", "success", "warn", "error"].includes(m.level) ? m.level : "info");
        else if (m.op === "title") win.titleEl.textContent = String(m.text || item.title).slice(0, 80).toUpperCase();
        else if (m.op === "close") close();
        else if (m.op === "runTool") reply(true, await runTool(item, m.tool, m.args));
        else if (m.op === "get") reply(true, await storageGet(item, m.key));
        else if (m.op === "set") reply(true, await storageSet(item, m.key, m.value));
        else if (m.op === "remove") reply(true, await storageRemove(item, m.key));
      } catch (err) { if (m.id) reply(false, err && err.message ? err.message : err); }
    };
    global.addEventListener("message", onMessage);
    const close = wireClose(win, item, () => {
      global.removeEventListener("message", onMessage);
      try { frame.contentWindow.postMessage({ jv: 1, op: "closing" }, "*"); } catch (_) { /* gone */ }
    });
    state.open.set(item.id, { close, focus: () => { try { frame.focus(); } catch (_) { /* ignore */ } } });
    try { frame.focus(); } catch (_) { /* ignore */ }
  }

  /* ---- mode "menu": a div in a panel, with the tool's CSS scoped to it -------------------- */

  async function openMenu(item) {
    if (state.open.has(item.id)) { state.open.get(item.id).focus(); return; }
    const bundle = await fetchBundle(item);
    if (!bundle) return;
    (bundle.problems || []).forEach((p) => toast(item.label + ": " + p, "warn"));
    const win = makeWindow(item, bundle);
    const hostDiv = el("div", { class: "tool-ui-host" });
    win.body.appendChild(hostDiv);
    const root = hostDiv.attachShadow ? hostDiv.attachShadow({ mode: "open" }) : hostDiv;
    // :host rules in the tool's CSS apply to hostDiv; the base rule keeps it filling the panel.
    const baseStyle = el("style");
    baseStyle.textContent = ":host{display:block;min-height:100%;color:var(--text);font-family:inherit}";
    const toolStyle = el("style");
    toolStyle.textContent = String(bundle.css || "");
    root.appendChild(baseStyle); root.appendChild(toolStyle);
    const content = el("div", { class: "tool-ui-content" });
    content.innerHTML = String(bundle.html || "");      // the tool's own markup, by design (see TRUST above)
    root.appendChild(content);

    const closers = [];
    const host = {
      id: item.id, mode: "menu", tools: item.tools.slice(), root,
      runTool: (name, args) => runTool(item, name, args),
      toast: (m, l) => toast(m, ["info", "success", "warn", "error"].includes(l) ? l : "info"),
      setTitle: (t) => { win.titleEl.textContent = String(t || item.title).slice(0, 80).toUpperCase(); },
      close: () => close(),
      onClose: (fn) => { if (typeof fn === "function") closers.push(fn); },
      storage: { get: (k) => storageGet(item, k), set: (k, v) => storageSet(item, k, v), remove: (k) => storageRemove(item, k) },
    };
    const close = wireClose(win, item, () => { closers.forEach((fn) => { try { fn(); } catch (_) { /* page cleanup */ } }); });
    state.open.set(item.id, { close, focus: () => { try { win.closeBtn.focus(); } catch (_) { /* ignore */ } } });

    if (bundle.js) {
      try {
        const AsyncFunction = Object.getPrototypeOf(async function () { /* probe */ }).constructor;
        const run = new AsyncFunction("host", String(bundle.js));
        run(host).catch((e) => toast(item.label + ": " + (e && e.message ? e.message : e), "error"));
      } catch (e) {
        toast(item.label + ": the page's script didn't start — " + e.message, "error");
      }
    }
    try { win.closeBtn.focus(); } catch (_) { /* ignore */ }
  }

  function open(id) {
    const item = state.items.find((u) => u.id === id);
    if (!item) { toast("No tool screen called " + id + " (or it is switched off).", "warn"); return Promise.resolve(); }
    return item.mode === "menu" ? openMenu(item) : openButton(item);
  }

  /* ---- the buttons and Menu entries ------------------------------------------------------- */

  function closeMenuList() {
    const list = document.getElementById("panel-menu-list");
    if (list) list.hidden = true;
    ["btn-panel-menu", "btn-panel-menu-focus"].forEach((id) => { const b = document.getElementById(id); if (b) b.setAttribute("aria-expanded", "false"); });
  }

  function render() {
    // Buttons: every bar container (Classic layout and Focus layout each have one).
    document.querySelectorAll("[data-tool-ui-bar]").forEach((bar) => {
      while (bar.firstChild) bar.removeChild(bar.firstChild);
      const buttons = byMode(state.items, "button");
      bar.hidden = !buttons.length;
      buttons.forEach((u) => {
        const b = el("button", { class: "btn btn--ghost tool-ui-btn", type: "button", "data-tool-ui-id": u.id, title: u.hint || u.title, onclick: () => open(u.id) });
        if (u.icon) b.appendChild(el("span", { class: "tool-ui-btn__icon", "aria-hidden": "true" }, u.icon));
        b.appendChild(el("span", { class: "tool-ui-btn__label" }, u.label));
        bar.appendChild(b);
      });
    });
    // Menu entries: after the built-in ones, in the one shared list.
    const list = document.getElementById("panel-menu-list");
    if (list) {
      list.querySelectorAll("[data-tool-ui-entry]").forEach((n) => n.parentNode && n.parentNode.removeChild(n));
      byMode(state.items, "menu").forEach((u) => {
        list.appendChild(el("button", {
          class: "panel-menu__item", type: "button", role: "menuitem", "data-tool-ui-entry": u.id,
          onclick: () => { closeMenuList(); open(u.id); },
        }, [el("span", { class: "panel-menu__item-label" }, u.label), el("span", { class: "panel-menu__item-hint" }, u.hint || "")]));
      });
    }
    // An open window whose element was just switched off (or removed) closes.
    Array.from(state.open.keys()).forEach((id) => { if (!state.items.some((u) => u.id === id)) state.open.get(id).close(); });
  }

  async function refresh() {
    try {
      state.items = cleanList(await api("/api/tool-ui"));
      state.loaded = true;
    } catch (_) {
      // Offline / server older than this feature: keep whatever was there and say nothing.
      // The app is fully usable without tool screens.
      if (!state.loaded) state.items = [];
    }
    render();
  }

  function start() { refresh(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();

  global.JarvisToolUI = { refresh, open, list: () => state.items.slice(), _pure: PURE };
})(window);
