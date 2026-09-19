/* ============================================================================
 * ui-kit.js — Jarvis's generic popup layer + theme engine.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * app.js grew one bespoke surface per feature: a toast for its own errors, a
 * bottom-left popup for direct-command confirmation, an in-thread bubble for
 * ask-time confirmation, plus screenshot / download / organize-json / present-
 * file / dev-agent cards. Each one meant new markup, new CSS and a new branch
 * in the JARVIS_MEDIA dispatch. That's fine for a fixed set of built-ins and
 * impossible as the answer to "my custom tool wants to warn the user".
 *
 * So this is the generalization: FOUR surfaces, one API, reachable from
 * JavaScript *and* from any Python tool via jarvis/ui_bridge.py.
 *
 *   JarvisUI.toast(...)    transient corner message   (the error popup)
 *   JarvisUI.bubble(...)   a card inside the chat     (persists in the thread)
 *   JarvisUI.dialog(...)   a real modal               (interrupts)
 *   JarvisUI.confirm(...)  modal that returns a Promise<bool>
 *   JarvisUI.choose(...)   modal that returns Promise<string>
 *   JarvisUI.prompt(...)   modal that returns Promise<string>
 *   JarvisUI.form(...)     modal that returns Promise<object>
 *   JarvisUI.progress(...) an updatable row
 *
 * EVERYTHING IS TEXT, NEVER MARKUP
 * --------------------------------
 * Every field a tool supplies is inserted with textContent, never innerHTML.
 * A tool's output can contain anything — a filename with a `<script>` in it,
 * a web page title, a stack trace — and none of it is trusted. The only
 * styling a tool can influence is `level`, which is validated against a fixed
 * list. This is the single most important property of this file; if you add a
 * field, add it as text.
 *
 * FOCUS AND ESCAPE
 * ----------------
 * A blocking modal traps Tab, focuses its primary control on open, restores
 * focus to whatever was focused before on close, and treats Escape as the
 * SAFE answer (cancel / default), never as confirm. A dialog you can't
 * dismiss with the keyboard is a dialog that traps someone.
 *
 * THE THEME ENGINE
 * ----------------
 * style.css is already entirely var()-driven off :root, so theming is just
 * "write different values onto :root". A theme here is a flat object of CSS
 * variable overrides plus a little metadata, which means a new skin needs no
 * code — it's data, and can therefore be shipped, user-created, exported as
 * JSON, and shared as a file.
 * ========================================================================= */

(function (global) {
  "use strict";

  const LEVELS = ["info", "success", "warn", "error"];
  const LEVEL_MARK = { info: "\u2139", success: "\u2713", warn: "\u26a0", error: "\u2717" };
  const DEFAULT_TOAST_MS = 4200;

  function level(value) {
    const v = String(value || "info").toLowerCase();
    const alias = { warning: "warn", danger: "error", fail: "error", ok: "success", done: "success" };
    const mapped = alias[v] || v;
    return LEVELS.indexOf(mapped) >= 0 ? mapped : "info";
  }

  /* --- tiny DOM helper (mirrors app.js's own `el`, kept local so this file
         has no load-order dependency on app.js) --- */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (Array.isArray(children) ? children : children ? [children] : []).forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number"
        ? document.createTextNode(String(c)) : c);
    });
    return node;
  }

  function layer() {
    let root = document.getElementById("jui-layer");
    if (!root) {
      root = el("div", { id: "jui-layer", class: "jui-layer" });
      document.body.appendChild(root);
    }
    return root;
  }

  function toastHost() {
    let host = document.getElementById("jui-toasts");
    if (!host) {
      host = el("div", { id: "jui-toasts", class: "jui-toasts", role: "status",
                         "aria-live": "polite" });
      layer().appendChild(host);
    }
    return host;
  }

  /* =======================================================================
   * TOAST
   * ===================================================================== */

  const liveToasts = new Map();

  function toast(opts) {
    const o = typeof opts === "string" ? { message: opts } : (opts || {});
    const lvl = level(o.level);
    const id = o.id || ("t" + Date.now() + Math.random().toString(16).slice(2, 6));
    const ms = Number(o.timeout) > 0 ? Number(o.timeout) * (o.timeout < 1000 ? 1000 : 1)
                                     : DEFAULT_TOAST_MS;

    const node = el("div", { class: "jui-toast jui-toast--" + lvl, "data-id": id }, [
      el("span", { class: "jui-toast__mark", "aria-hidden": "true" }, LEVEL_MARK[lvl]),
      el("div", { class: "jui-toast__text" }, [
        o.title ? el("div", { class: "jui-toast__title" }, o.title) : null,
        el("div", { class: "jui-toast__msg" }, String(o.message || "")),
      ]),
      el("button", { class: "jui-toast__close", type: "button", "aria-label": "Dismiss",
                     onclick: () => dropToast(id) }, "\u00d7"),
    ]);

    toastHost().appendChild(node);
    requestAnimationFrame(() => node.classList.add("is-in"));

    // An error stays until dismissed. Auto-hiding the one message that says
    // something actually broke — while it's likely off-screen or the user is
    // mid-sentence — is how errors go unnoticed.
    const timer = lvl === "error" && !o.timeout ? null : setTimeout(() => dropToast(id), ms);
    liveToasts.set(id, { node, timer });
    return id;
  }

  function dropToast(id) {
    const entry = liveToasts.get(id);
    if (!entry) return;
    clearTimeout(entry.timer);
    liveToasts.delete(id);
    entry.node.classList.remove("is-in");
    setTimeout(() => entry.node.remove(), 200);
  }

  /* =======================================================================
   * MODALS
   * ===================================================================== */

  let openModal = null;

  function closeModal(result) {
    if (!openModal) return;
    const { node, resolve, previousFocus, onKey } = openModal;
    openModal = null;
    document.removeEventListener("keydown", onKey, true);
    node.classList.remove("is-in");
    setTimeout(() => node.remove(), 160);
    if (previousFocus && previousFocus.focus) {
      try { previousFocus.focus(); } catch (_) { /* node may be gone */ }
    }
    if (resolve) resolve(result);
  }

  function buildModal(o, bodyNodes, footNodes, opts) {
    const lvl = level(o.level);
    const settings = opts || {};
    const card = el("div", {
      class: "jui-modal jui-modal--" + lvl + (settings.wide ? " jui-modal--wide" : ""),
      role: "dialog", "aria-modal": "true", "aria-label": String(o.title || "Dialog"),
    }, [
      el("div", { class: "jui-modal__head" }, [
        el("span", { class: "jui-modal__mark", "aria-hidden": "true" }, LEVEL_MARK[lvl]),
        el("h2", { class: "jui-modal__title" }, String(o.title || "")),
        settings.dismissable === false ? null
          : el("button", { class: "jui-modal__close", type: "button", "aria-label": "Close",
                           onclick: () => closeModal(settings.escapeValue) }, "\u00d7"),
      ]),
      el("div", { class: "jui-modal__body" }, bodyNodes),
      footNodes ? el("div", { class: "jui-modal__foot" }, footNodes) : null,
    ]);

    const backdrop = el("div", {
      class: "jui-backdrop",
      onclick: (e) => {
        // Only a click on the backdrop itself closes — a drag that starts
        // inside the card and ends outside must not count as dismissal.
        if (e.target === backdrop && settings.dismissable !== false) {
          closeModal(settings.escapeValue);
        }
      },
    }, [card]);

    return { backdrop, card };
  }

  function showModal(o, bodyNodes, footNodes, opts) {
    // One modal at a time: a stack of them is a trap, and the confirm flow
    // that needs them most is inherently sequential.
    if (openModal) closeModal(openModal.escapeValue);

    const settings = opts || {};
    const { backdrop, card } = buildModal(o, bodyNodes, footNodes, settings);

    return new Promise((resolve) => {
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          if (settings.dismissable !== false) closeModal(settings.escapeValue);
          return;
        }
        if (e.key !== "Tab") return;
        // Focus trap. Without it, Tab walks into the page behind the modal,
        // which for a blocking confirm means tabbing to controls that are
        // supposed to be unreachable.
        const focusables = card.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      };

      openModal = {
        node: backdrop, resolve, onKey,
        previousFocus: document.activeElement,
        escapeValue: settings.escapeValue,
      };
      document.addEventListener("keydown", onKey, true);
      layer().appendChild(backdrop);
      requestAnimationFrame(() => {
        backdrop.classList.add("is-in");
        const focusTarget = card.querySelector("[data-autofocus]")
          || card.querySelector("input, select, textarea, button");
        if (focusTarget) focusTarget.focus();
      });
    });
  }

  function bodyOf(o) {
    const nodes = [];
    if (o.body) nodes.push(el("div", { class: "jui-modal__text" }, String(o.body)));
    if (o.pre) nodes.push(el("pre", { class: "jui-modal__pre" }, String(o.pre)));
    return nodes;
  }

  function dialog(o) {
    o = o || {};
    const actions = (o.actions || []).map((a) =>
      el("button", {
        class: "jui-btn jui-btn--" + level(a.level),
        type: "button",
        onclick: () => closeModal(a.value),
      }, String(a.label)));
    if (!actions.length) {
      actions.push(el("button", { class: "jui-btn jui-btn--primary", type: "button",
                                  "data-autofocus": true,
                                  onclick: () => closeModal(null) }, "Close"));
    }
    return showModal(o, bodyOf(o), actions, { escapeValue: null });
  }

  function confirm(o) {
    o = typeof o === "string" ? { title: o } : (o || {});
    const foot = [
      el("button", { class: "jui-btn", type: "button",
                     onclick: () => closeModal(false) }, o.cancelLabel || "Cancel"),
      el("button", {
        class: "jui-btn jui-btn--" + (level(o.level) === "info" ? "primary" : level(o.level)),
        type: "button", "data-autofocus": true,
        onclick: () => closeModal(true),
      }, o.confirmLabel || "Confirm"),
    ];
    // escapeValue false: dismissing a confirm never means yes.
    return showModal(o, bodyOf(o), foot, { escapeValue: false });
  }

  function choose(o) {
    o = o || {};
    const options = (o.options || []).map((opt) =>
      typeof opt === "string" ? { label: opt, value: opt } : opt);
    const list = el("div", { class: "jui-choices" },
      options.map((opt, i) => el("button", {
        class: "jui-choice", type: "button",
        "data-autofocus": i === 0 ? true : null,
        onclick: () => closeModal(opt.value),
      }, [
        el("span", { class: "jui-choice__label" }, String(opt.label)),
        opt.hint ? el("span", { class: "jui-choice__hint" }, String(opt.hint)) : null,
      ])));
    const foot = [el("button", { class: "jui-btn", type: "button",
                                 onclick: () => closeModal(o.default ?? null) }, "Cancel")];
    return showModal(o, bodyOf(o).concat([list]), foot,
                     { escapeValue: o.default ?? null });
  }

  function prompt(o) {
    o = typeof o === "string" ? { title: o } : (o || {});
    const input = el("input", {
      class: "jui-input", type: o.secret ? "password" : "text",
      value: o.default || "", placeholder: o.placeholder || "", "data-autofocus": true,
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); closeModal(input.value); }
    });
    const foot = [
      el("button", { class: "jui-btn", type: "button",
                     onclick: () => closeModal(o.default || "") }, "Cancel"),
      el("button", { class: "jui-btn jui-btn--primary", type: "button",
                     onclick: () => closeModal(input.value) }, o.confirmLabel || "OK"),
    ];
    return showModal(o, bodyOf(o).concat([input]), foot,
                     { escapeValue: o.default || "" });
  }

  function form(o) {
    o = o || {};
    const fields = o.fields || [];
    const inputs = {};
    const rows = fields.map((f, i) => {
      let control;
      if (f.type === "select") {
        control = el("select", { class: "jui-input" },
          (f.options || []).map((opt) =>
            el("option", { value: String(opt), selected: String(opt) === String(f.default) },
               String(opt))));
      } else if (f.type === "checkbox") {
        control = el("input", { class: "jui-check", type: "checkbox", checked: !!f.default });
      } else if (f.type === "textarea") {
        control = el("textarea", { class: "jui-input jui-input--area", rows: 4,
                                   placeholder: f.placeholder || "" }, String(f.default || ""));
      } else {
        control = el("input", {
          class: "jui-input",
          type: f.type === "password" ? "password" : (f.type === "number" ? "number" : "text"),
          value: f.default === undefined || f.default === null ? "" : String(f.default),
          placeholder: f.placeholder || "",
        });
      }
      if (i === 0) control.setAttribute("data-autofocus", "");
      inputs[f.name] = { control, type: f.type };
      return el("label", { class: "jui-field" }, [
        el("span", { class: "jui-field__label" }, String(f.label || f.name)),
        control,
      ]);
    });

    function collect() {
      const out = {};
      Object.entries(inputs).forEach(([name, { control, type }]) => {
        if (type === "checkbox") out[name] = control.checked;
        else if (type === "number") {
          const n = parseFloat(control.value);
          out[name] = Number.isNaN(n) ? null : n;
        } else out[name] = control.value;
      });
      return out;
    }

    const defaults = {};
    fields.forEach((f) => { defaults[f.name] = f.default; });

    const foot = [
      el("button", { class: "jui-btn", type: "button",
                     onclick: () => closeModal(defaults) }, "Cancel"),
      el("button", { class: "jui-btn jui-btn--primary", type: "button",
                     onclick: () => closeModal(collect()) }, o.confirmLabel || "Save"),
    ];
    return showModal(o, bodyOf(o).concat(rows), foot,
                     { escapeValue: defaults, wide: fields.length > 4 });
  }

  /* =======================================================================
   * PROGRESS
   * ===================================================================== */

  function progressHost() {
    let host = document.getElementById("jui-progress");
    if (!host) {
      host = el("div", { id: "jui-progress", class: "jui-progress-host" });
      layer().appendChild(host);
    }
    return host;
  }

  function progress(o) {
    o = o || {};
    const id = o.id || ("p" + Date.now());
    let row = progressHost().querySelector('[data-id="' + CSS.escape(id) + '"]');
    if (!row) {
      row = el("div", { class: "jui-prog", "data-id": id }, [
        el("div", { class: "jui-prog__label" }, String(o.label || "")),
        el("div", { class: "jui-prog__track" }, [el("div", { class: "jui-prog__fill" })]),
      ]);
      progressHost().appendChild(row);
    }
    row.querySelector(".jui-prog__label").textContent = String(o.label || "");
    const fill = row.querySelector(".jui-prog__fill");
    if (o.total) {
      const pct = Math.max(0, Math.min(100, (Number(o.value) || 0) / Number(o.total) * 100));
      fill.style.width = pct + "%";
      fill.classList.remove("is-indeterminate");
    } else {
      fill.classList.add("is-indeterminate");
    }
    if (o.done) {
      row.classList.add("is-done");
      setTimeout(() => row.remove(), 1200);
    }
    return id;
  }

  function dismiss(id) {
    dropToast(id);
    const row = progressHost().querySelector('[data-id="' + CSS.escape(id) + '"]');
    if (row) row.remove();
    if (openModal) closeModal(openModal.escapeValue);
  }

  /* =======================================================================
   * THEMES
   *
   * A theme is a flat map of CSS custom properties. style.css already reads
   * everything through var(), so applying one is a loop of setProperty — no
   * stylesheet swapping, no reload, no FOUC.
   * ===================================================================== */

  const BUILTIN_THEMES = {
    jarvis: {
      label: "Jarvis (default)", author: "built-in", dark: true,
      vars: {
        "--bg": "#04070d", "--bg-1": "#070d16", "--bg-raised": "#0d1826",
        "--bg-panel": "rgba(9, 18, 30, 0.68)", "--bg-panel-2": "rgba(13, 24, 38, 0.55)",
        "--accent": "#4fd8ff", "--accent-soft": "#2ea9d6", "--accent-dim": "#164a63",
        "--accent-glow": "rgba(79, 216, 255, 0.35)",
        "--accent-secondary": "#f2b544", "--accent-tertiary": "#2b5cff",
        "--text": "#e6f5fc", "--text-dim": "#85a2b6", "--text-dimmer": "#4d6377",
      },
    },
    mark_i: {
      label: "Mark I", author: "built-in", dark: true,
      hint: "Hot rod red and gold.",
      vars: {
        "--bg": "#0b0406", "--bg-1": "#150709", "--bg-raised": "#1f0b0e",
        "--bg-panel": "rgba(31, 11, 14, 0.7)", "--bg-panel-2": "rgba(42, 15, 19, 0.55)",
        "--accent": "#ffb627", "--accent-soft": "#e0921a", "--accent-dim": "#5c3a08",
        "--accent-glow": "rgba(255, 182, 39, 0.35)",
        "--accent-secondary": "#e63946", "--accent-tertiary": "#ff7b00",
        "--text": "#fdf0dc", "--text-dim": "#c0967a", "--text-dimmer": "#7a5a48",
      },
    },
    terminal: {
      label: "Terminal", author: "built-in", dark: true,
      hint: "Phosphor green on black. No glow, no gradients.",
      vars: {
        "--bg": "#000000", "--bg-1": "#020602", "--bg-raised": "#0a120a",
        "--bg-panel": "rgba(4, 12, 4, 0.85)", "--bg-panel-2": "rgba(8, 20, 8, 0.7)",
        "--accent": "#33ff66", "--accent-soft": "#22cc4d", "--accent-dim": "#0c3d1a",
        "--accent-glow": "rgba(51, 255, 102, 0.25)",
        "--accent-secondary": "#aaff00", "--accent-tertiary": "#00cc88",
        "--text": "#c8ffd4", "--text-dim": "#5fa06f", "--text-dimmer": "#356342",
      },
    },
    mono: {
      label: "Mono", author: "built-in", dark: true,
      hint: "No colour at all. Maximum contrast, minimum distraction.",
      vars: {
        "--bg": "#0a0a0a", "--bg-1": "#101010", "--bg-raised": "#181818",
        "--bg-panel": "rgba(20, 20, 20, 0.8)", "--bg-panel-2": "rgba(28, 28, 28, 0.6)",
        "--accent": "#e8e8e8", "--accent-soft": "#b4b4b4", "--accent-dim": "#3a3a3a",
        "--accent-glow": "rgba(232, 232, 232, 0.18)",
        "--accent-secondary": "#9a9a9a", "--accent-tertiary": "#6e6e6e",
        "--text": "#f2f2f2", "--text-dim": "#9a9a9a", "--text-dimmer": "#5e5e5e",
      },
    },
    daylight: {
      label: "Daylight", author: "built-in", dark: false,
      hint: "The light theme. Genuinely light, not a dimmed dark one.",
      vars: {
        "--bg": "#f4f6f9", "--bg-1": "#eaeef4", "--bg-raised": "#ffffff",
        "--bg-panel": "rgba(255, 255, 255, 0.86)", "--bg-panel-2": "rgba(242, 246, 251, 0.8)",
        "--accent": "#0a6ed1", "--accent-soft": "#0956a5", "--accent-dim": "#bcd8f5",
        "--accent-glow": "rgba(10, 110, 209, 0.2)",
        "--accent-secondary": "#b25e00", "--accent-tertiary": "#5b2bd9",
        "--text": "#101720", "--text-dim": "#4a5a6b", "--text-dimmer": "#7d8b99",
      },
    },
    nebula: {
      label: "Nebula", author: "built-in", dark: true,
      hint: "Deep violet with magenta highlights.",
      vars: {
        "--bg": "#070312", "--bg-1": "#0d0620", "--bg-raised": "#160c31",
        "--bg-panel": "rgba(22, 12, 49, 0.7)", "--bg-panel-2": "rgba(30, 17, 64, 0.55)",
        "--accent": "#c66bff", "--accent-soft": "#a145e0", "--accent-dim": "#421d63",
        "--accent-glow": "rgba(198, 107, 255, 0.35)",
        "--accent-secondary": "#ff5fa2", "--accent-tertiary": "#5b8cff",
        "--text": "#f0e6ff", "--text-dim": "#a58fc4", "--text-dimmer": "#6b5a85",
      },
    },
    // Deliberately empty. This is the escape hatch for personas and the
    // legacy accent/skin picker: picking a real theme here calls
    // markActive(), which makes app.js's persona-restore code back off and
    // leave colours alone (see the ACTIVE_KEY comment above) — a normal
    // theme MEANS to take over. "None" means the opposite: it should never
    // fight a persona for control of --accent/--bg/etc. So applyTheme()
    // below special-cases isNone to skip the vars loop entirely (nothing to
    // skip anyway, vars is {}) AND calls deactivate() instead of
    // markActive() — the one entry in this table that turns the theme
    // system OFF rather than on.
    none: {
      label: "None", author: "built-in", dark: true, isNone: true,
      hint: "Applies nothing. Lets a persona or the Skin panel's own accent picker control colour instead.",
      vars: {},
    },
  };

  const THEME_KEY = "jarvis.theme";
  const CUSTOM_KEY = "jarvis.themes.custom";
  const TUNING_KEY = "jarvis.theme.tuning";
  // Whether the NEW theme gallery is the thing actually in charge of colours
  // right now, as opposed to the older accent-swatch/custom-color/saturation
  // picker that already existed in the Skin modal before this file did.
  //
  // THE BUG THIS FLAG FIXES: that older picker restores its own saved accent
  // whenever the Skin modal closes, whenever the page loads, and on the
  // modal's main Save button — always, unconditionally, with no idea this
  // theme system exists. Its restore path (applyAccent) sets --bg, --accent
  // and everything derived from it, but never touches --text/--text-dim/
  // --text-dimmer. So picking or saving a theme here would look right for a
  // moment and then get silently overwritten back to the old accent the
  // instant the modal closed or the page reloaded — except the text colour,
  // which the old code has no opinion on. That mismatch (colours revert,
  // text doesn't) was the visible symptom.
  //
  // The fix is this flag: set the moment someone deliberately picks, saves,
  // or imports a theme HERE; cleared the moment they touch the OLD swatches,
  // custom-color input, or saturation slider. app.js's three restore sites
  // check it and, when set, re-apply THIS system's saved theme instead of
  // the legacy accent — so whichever picker was touched most recently is the
  // one that survives a close/reload, and the other stays out of its way.
  const ACTIVE_KEY = "jarvis.theme.active";

  function markActive() {
    try { localStorage.setItem(ACTIVE_KEY, "1"); } catch (_) { /* private mode */ }
  }

  function isActive() {
    try { return localStorage.getItem(ACTIVE_KEY) === "1"; } catch (_) { return false; }
  }

  function deactivate() {
    try { localStorage.removeItem(ACTIVE_KEY); } catch (_) { /* private mode */ }
  }

  function customThemes() {
    try {
      const raw = JSON.parse(localStorage.getItem(CUSTOM_KEY) || "{}");
      return raw && typeof raw === "object" ? raw : {};
    } catch (_) { return {}; }
  }

  function allThemes() {
    return Object.assign({}, BUILTIN_THEMES, customThemes());
  }

  function saveCustomTheme(id, theme) {
    const all = customThemes();
    all[id] = Object.assign({ label: id, author: "you", dark: true }, theme);
    try { localStorage.setItem(CUSTOM_KEY, JSON.stringify(all)); } catch (_) { /* quota */ }
    return all[id];
  }

  function deleteCustomTheme(id) {
    const all = customThemes();
    delete all[id];
    try { localStorage.setItem(CUSTOM_KEY, JSON.stringify(all)); } catch (_) { /* quota */ }
  }

  /* Per-theme tuning the user can dial on top of any theme without editing
     it — the reason this is separate from the theme itself is that someone
     who likes Nebula but wants less saturation shouldn't have to fork it. */
  function tuning() {
    try {
      return Object.assign({ saturation: 100, contrast: 100, radius: 100, glow: 100,
                             fontScale: 100, motion: true, scanlines: true },
                           JSON.parse(localStorage.getItem(TUNING_KEY) || "{}"));
    } catch (_) {
      return { saturation: 100, contrast: 100, radius: 100, glow: 100,
               fontScale: 100, motion: true, scanlines: true };
    }
  }

  function saveTuning(next) {
    const merged = Object.assign(tuning(), next || {});
    try { localStorage.setItem(TUNING_KEY, JSON.stringify(merged)); } catch (_) { /* quota */ }
    applyTheme();
    return merged;
  }

  function applyTheme(id) {
    const themes = allThemes();
    const chosen = id || localStorage.getItem(THEME_KEY) || "jarvis";
    const theme = themes[chosen] || themes.jarvis;
    const root = document.documentElement;

    // "None" deactivates instead of activating — see its entry in
    // BUILTIN_THEMES above for why — and skips writing any of its (empty)
    // vars, so whatever a persona or the legacy accent picker already put
    // on :root is left exactly as it was.
    if (theme.isNone) {
      deactivate();
    } else if (id) {
      markActive();
    }

    if (!theme.isNone) {
      Object.entries(theme.vars || {}).forEach(([k, v]) => root.style.setProperty(k, v));

      // rgb companions for the colours style.css also uses inside rgba().
      ["--accent", "--accent-secondary", "--accent-tertiary"].forEach((name) => {
        const rgb = hexToRgb(theme.vars && theme.vars[name]);
        if (rgb) root.style.setProperty(name + "-rgb", rgb.join(", "));
      });
    }

    const t = tuning();
    root.style.setProperty("--jui-saturation", (t.saturation / 100).toFixed(2));
    root.style.setProperty("--jui-radius-scale", (t.radius / 100).toFixed(2));
    root.style.setProperty("--jui-glow-scale", (t.glow / 100).toFixed(2));
    root.style.setProperty("--jui-font-scale", (t.fontScale / 100).toFixed(2));
    root.classList.toggle("jui-no-motion", !t.motion);
    root.classList.toggle("jui-no-scanlines", !t.scanlines);
    root.classList.toggle("jui-light", theme.dark === false);
    root.setAttribute("data-theme", chosen);

    try { localStorage.setItem(THEME_KEY, chosen); } catch (_) { /* private mode */ }
    document.dispatchEvent(new CustomEvent("jarvis:theme", { detail: { id: chosen, theme } }));
    return chosen;
  }

  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(String(hex || "").trim());
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
  }

  function currentTheme() {
    return localStorage.getItem(THEME_KEY) || "jarvis";
  }

  function exportTheme(id) {
    const theme = allThemes()[id || currentTheme()];
    return JSON.stringify({ id: id || currentTheme(), ...theme }, null, 2);
  }

  function importTheme(json) {
    let parsed;
    try { parsed = typeof json === "string" ? JSON.parse(json) : json; }
    catch (e) { return { ok: false, error: "that isn't valid JSON: " + e.message }; }
    if (!parsed || typeof parsed !== "object" || !parsed.vars) {
      return { ok: false, error: 'a theme needs a "vars" object of CSS variables' };
    }
    const id = String(parsed.id || parsed.label || "imported")
      .toLowerCase().replace(/[^a-z0-9_]+/g, "_").slice(0, 32) || "imported";
    // Only known variables are accepted. An imported theme is a file from
    // someone else; letting it set arbitrary properties on :root would make
    // "install this skin" a way to restyle anything, including hiding a
    // confirmation dialog's buttons.
    const allowed = new Set(Object.keys(BUILTIN_THEMES.jarvis.vars));
    const vars = {};
    Object.entries(parsed.vars).forEach(([k, v]) => {
      if (allowed.has(k) && typeof v === "string" && v.length < 64) vars[k] = v;
    });
    if (!Object.keys(vars).length) {
      return { ok: false, error: "no recognised CSS variables in that file" };
    }
    saveCustomTheme(id, { label: parsed.label || id, author: parsed.author || "imported",
                          dark: parsed.dark !== false, hint: parsed.hint || "", vars });
    return { ok: true, id };
  }

  /* =======================================================================
   * Tool-driven events — the bridge from jarvis/ui_bridge.py
   * ===================================================================== */

  function handleEvent(event) {
    if (!event || typeof event !== "object") return null;
    switch (event.kind) {
      case "toast":    return toast(event);
      case "dialog":   return dialog(event);
      case "confirm":  return confirm(event);
      case "choose":   return choose(event);
      case "prompt":   return prompt(event);
      case "form":     return form(event);
      case "progress": return progress(event);
      case "dismiss":  return dismiss(event.id);
      case "bubble":
        // A bubble belongs in the chat thread, which app.js owns. It
        // publishes the event instead of rendering it, so app.js can put it
        // in the right place in the turn order — and so a bubble that
        // arrives while the user is looking at another panel is still
        // recorded rather than dropped on the floor.
        document.dispatchEvent(new CustomEvent("jarvis:ui-bubble", { detail: event }));
        return event.id;
      default:
        return null;
    }
  }

  global.JarvisUI = {
    toast, bubble: (o) => handleEvent(Object.assign({ kind: "bubble" }, o)),
    dialog, confirm, choose, prompt, form, progress, dismiss,
    handleEvent, close: closeModal,
    themes: {
      all: allThemes, builtin: BUILTIN_THEMES, custom: customThemes,
      apply: applyTheme, current: currentTheme, save: saveCustomTheme,
      remove: deleteCustomTheme, tuning, setTuning: saveTuning,
      export: exportTheme, import: importTheme,
      isActive, deactivate,
    },
    _el: el,
  };

  // Apply the saved theme before first paint where possible, so a reload
  // doesn't flash the default palette first.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => applyTheme());
  } else {
    applyTheme();
  }
})(window);
