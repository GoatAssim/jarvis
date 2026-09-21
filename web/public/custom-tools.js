/* ============================================================================
 * custom-tools.js — the Custom Tools panel, plus the theme gallery it shares
 * with the Skin modal.
 *
 * Deliberately standalone: it talks to the server over /api/ctools and to the
 * popup layer over window.JarvisUI, and reaches into app.js for nothing at
 * all. That means it can't break the chat by existing, and app.js needs one
 * line (the menu click) to adopt it.
 *
 * THE EDITOR'S ONE JOB
 * --------------------
 * Tell you why your file is broken, immediately, in the editor — not at the
 * next process start, in a stderr line nobody reads. A rejected tool file is
 * completely silent today: no error in the UI, no tool in the catalog, and
 * actions/_template.py warns about exactly this ("a typo here fails quiet,
 * not loud"). So Save validates first and refuses to write something that
 * would be rejected, and Check does the same without writing.
 * ========================================================================= */

(function (global) {
  "use strict";

  const UI = () => global.JarvisUI;
  const el = (...a) => global.JarvisUI._el(...a);
  const qs = (sel) => document.querySelector(sel);

  let state = { tools: [], active: null, dirty: false, templates: [] };

  async function api(path, opts) {
    const res = await fetch("/api/ctools" + path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) {
      const err = new Error((body && (body.error || body.message)) || res.statusText);
      err.data = body;
      throw err;
    }
    return body;
  }

  /* --- status line -------------------------------------------------------- */

  function setStatus(text, ok) {
    const node = qs("#ctools-status");
    if (!node) return;
    node.textContent = text || "";
    node.className = "ctools__status" + (ok === true ? " is-ok" : ok === false ? " is-bad" : "");
  }

  function showResult(value) {
    const node = qs("#ctools-result");
    if (!node) return;
    if (value === null || value === undefined) { node.hidden = true; return; }
    node.hidden = false;
    node.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  // What Menu > Test Checklist will show for this file (master plan G.1). The
  // check/save results carry checklist / checklist_missing / checklist_problems
  // from the CLI. A checklist entry is notes about a tool, so this is never an
  // error: it goes in the result box, and only when there is something to say.
  function checklistNote(result) {
    if (!result) return null;
    const lines = [];
    const problems = result.checklist_problems || [];
    const missing = result.checklist_missing || [];
    if (problems.length) {
      lines.push("Test Checklist \u2014 these were ignored (the tool itself still loads):");
      problems.forEach((p) => lines.push("  \u2022 " + p));
    }
    if (missing.length) {
      lines.push("Test Checklist \u2014 no entry yet for: " + missing.join(", ")
        + ". Add a TEST_CHECKLIST dict (every template has an example) so the panel can show how to test it.");
    }
    return lines.length ? lines.join("\n") : null;
  }

  /* --- list --------------------------------------------------------------- */

  function renderList() {
    const host = qs("#ctools-list");
    if (!host) return;
    host.innerHTML = "";
    if (!state.tools.length) {
      host.appendChild(el("div", { class: "ctools__meta", style: "padding:10px" },
        "No custom tools yet. Pick a template and hit New."));
      return;
    }
    state.tools.forEach((tool) => {
      const classes = ["ctools__item"];
      if (state.active === tool.name) classes.push("is-active");
      if (!tool.valid) classes.push("is-invalid");
      if (!tool.enabled) classes.push("is-off");
      host.appendChild(el("button", {
        class: classes.join(" "), type: "button",
        onclick: () => select(tool.name),
      }, [
        el("span", { class: "ctools__name" }, tool.name),
        el("span", { class: "ctools__meta" },
          tool.valid
            ? (tool.tools || []).join(", ") + (tool.enabled ? "" : "  · disabled")
            : "\u2717 " + (tool.error || "won't load").slice(0, 60)),
      ]));
    });
  }

  async function refresh() {
    try {
      const data = await api("");
      state.tools = data.tools || [];
      renderList();
    } catch (e) {
      UI().toast({ message: "Couldn't list custom tools: " + e.message, level: "error" });
    }
  }

  async function select(name) {
    if (state.dirty && !(await UI().confirm({
      title: "Discard unsaved changes?",
      body: "You've edited " + (state.active || "this tool") + " without saving.",
      confirmLabel: "Discard", level: "warn",
    }))) return;

    try {
      const data = await api("/" + encodeURIComponent(name));
      state.active = name;
      state.dirty = false;
      qs("#ctools-name").value = name;
      qs("#ctools-code").value = data.source || "";
      qs("#ctools-enabled").checked = !!data.enabled;
      setStatus(data.valid ? "loads cleanly \u2014 " + (data.tools || []).join(", ")
                           : data.error, data.valid);
      showResult(null);
      renderList();
    } catch (e) {
      UI().toast({ message: e.message, level: "error" });
    }
  }

  /* --- actions ------------------------------------------------------------ */

  function currentName() {
    return (qs("#ctools-name").value || "").trim().toLowerCase();
  }

  async function check() {
    const name = currentName() || "draft";
    try {
      const result = await api("/" + encodeURIComponent(name) + "/check", {
        method: "POST",
        body: JSON.stringify({ source: qs("#ctools-code").value }),
      });
      if (result.ok) {
        setStatus("Valid \u2014 provides " + (result.tools || []).join(", ")
                  + " in group '" + result.group + "'", true);
        const note = checklistNote(result);
        if (note) showResult(note);
      } else {
        setStatus("[" + (result.stage || "error") + "] " + result.error, false);
        if (result.hint) showResult(result.hint);
      }
      return result.ok;
    } catch (e) {
      setStatus(e.message, false);
      return false;
    }
  }

  async function save() {
    const name = currentName();
    if (!/^[a-z][a-z0-9_]{0,48}$/.test(name)) {
      setStatus("Name must be lower_snake_case and start with a letter.", false);
      return;
    }
    try {
      const result = await api("/" + encodeURIComponent(name), {
        method: "PUT",
        body: JSON.stringify({ source: qs("#ctools-code").value }),
      });
      if (!result.ok) {
        setStatus("[" + (result.stage || "error") + "] " + result.error, false);
        return;
      }
      state.active = name;
      state.dirty = false;
      setStatus("Saved \u2014 " + (result.tools || []).join(", "), true);
      const note = checklistNote(result);
      if (note) showResult(note);
      UI().toast({
        message: "Saved " + name + ". " + (result.note || ""),
        level: "success",
      });
      await refresh();
    } catch (e) {
      const data = e.data || {};
      setStatus(data.error || e.message, false);
    }
  }

  async function run() {
    const name = state.active || currentName();
    if (!name) return;
    const tool = state.tools.find((t) => t.name === name);
    const toolNames = (tool && tool.tools) || [];
    let target = toolNames[0];
    if (toolNames.length > 1) {
      target = await UI().choose({
        title: "Which handler?", options: toolNames, default: toolNames[0],
      });
      if (!target) return;
    }
    const raw = await UI().prompt({
      title: "Arguments for " + (target || name),
      body: "JSON object passed to the handler. This runs the REAL tool \u2014 "
            + "any side effect it has will actually happen.",
      default: "{}", level: "warn", confirmLabel: "Run",
    });
    let args;
    try { args = JSON.parse(raw || "{}"); }
    catch (e) { return UI().toast({ message: "Arguments aren't valid JSON.", level: "error" }); }

    setStatus("Running\u2026");
    try {
      const result = await api("/" + encodeURIComponent(name) + "/run", {
        method: "POST",
        body: JSON.stringify({ tool: target, arguments: args }),
      });
      showResult(result);
      setStatus(result.ok ? "Ran cleanly." : (result.error || "Failed."), result.ok);
    } catch (e) {
      setStatus(e.message, false);
    }
  }

  async function remove() {
    const name = state.active;
    if (!name) return;
    if (!(await UI().confirm({
      title: "Delete " + name + "?",
      body: "The file is removed from ~/.jarvis/tools. A .bak from the last save "
            + "is left behind.",
      confirmLabel: "Delete", level: "error",
    }))) return;
    try {
      await api("/" + encodeURIComponent(name), { method: "DELETE" });
      state.active = null;
      state.dirty = false;
      qs("#ctools-name").value = "";
      qs("#ctools-code").value = "";
      setStatus("");
      showResult(null);
      await refresh();
      UI().toast({ message: "Deleted " + name, level: "success" });
    } catch (e) {
      UI().toast({ message: e.message, level: "error" });
    }
  }

  async function toggleEnabled() {
    const name = state.active;
    if (!name) return;
    const enabled = qs("#ctools-enabled").checked;
    try {
      await api("/" + encodeURIComponent(name) + "/enabled", {
        method: "POST", body: JSON.stringify({ enabled }),
      });
      await refresh();
    } catch (e) {
      UI().toast({ message: e.message, level: "error" });
    }
  }

  async function newFromTemplate() {
    const id = qs("#ctools-template").value || "minimal";
    try {
      const data = await api("/draft?template=" + encodeURIComponent(id))
        .catch(() => null);
      // The CLI exposes template bodies through ctools-show --template; if
      // that round trip fails for any reason, an empty editor is still a
      // usable starting point rather than a dead button.
      qs("#ctools-code").value = (data && data.source) || "";
    } catch (_) {
      qs("#ctools-code").value = "";
    }
    state.active = null;
    state.dirty = true;
    qs("#ctools-name").value = "";
    qs("#ctools-name").focus();
    setStatus("New file \u2014 give it a name and Save.");
    showResult(null);
    renderList();
  }

  async function loadTemplates() {
    try {
      const data = await api("/templates");
      state.templates = data.templates || [];
    } catch (_) {
      state.templates = [{ id: "minimal", label: "Minimal" }];
    }
    const select = qs("#ctools-template");
    if (!select) return;
    select.innerHTML = "";
    state.templates.forEach((t) => {
      select.appendChild(el("option", { value: t.id, title: t.hint || "" }, t.label));
    });
  }

  /* --- open / close ------------------------------------------------------- */

  function open() {
    const overlay = qs("#ctools-overlay");
    if (!overlay) return;
    overlay.hidden = false;
    loadTemplates();
    refresh();
  }

  function close() {
    const overlay = qs("#ctools-overlay");
    if (overlay) overlay.hidden = true;
  }

  function wireSkin() {
    qs("#btn-theme-import")?.addEventListener("click", importThemeDialog);
    qs("#btn-theme-export")?.addEventListener("click", exportThemeDialog);
    qs("#btn-theme-save")?.addEventListener("click", async () => {
      const label = await UI().prompt({ title: "Name this theme", placeholder: "My skin" });
      if (!label) return;
      // Snapshot whatever :root currently resolves to, so tweaks made with
      // the accent picker are captured rather than only the base theme.
      const base = UI().themes.all()[UI().themes.current()] || {};
      const computed = getComputedStyle(document.documentElement);
      const vars = {};
      Object.keys(base.vars || {}).forEach((k) => {
        const v = computed.getPropertyValue(k).trim();
        if (v) vars[k] = v;
      });
      const id = label.toLowerCase().replace(/[^a-z0-9_]+/g, "_").slice(0, 32) || "custom";
      UI().themes.save(id, { label, author: "you", dark: base.dark !== false, vars });
      UI().themes.apply(id);
      renderThemeGallery();
      UI().toast({ message: "Saved theme \"" + label + "\".", level: "success" });
    });

    const tunes = { radius: "#tune-radius", glow: "#tune-glow", fontScale: "#tune-font" };
    const current = UI().themes.tuning();
    Object.entries(tunes).forEach(([key, sel]) => {
      const input = qs(sel);
      if (!input) return;
      input.value = current[key];
      input.addEventListener("input", () => UI().themes.setTuning({ [key]: Number(input.value) }));
    });
    [["#tune-motion", "motion"], ["#tune-scanlines", "scanlines"]].forEach(([sel, key]) => {
      const box = qs(sel);
      if (!box) return;
      box.checked = current[key] !== false;
      box.addEventListener("change", () => UI().themes.setTuning({ [key]: box.checked }));
    });

    // The Skin modal is opened by app.js; re-render the gallery whenever it
    // becomes visible so a theme saved elsewhere shows up without a reload.
    const backdrop = qs("#skin-backdrop");
    if (backdrop && "MutationObserver" in window) {
      new MutationObserver(() => { if (!backdrop.hidden) renderThemeGallery(); })
        .observe(backdrop, { attributes: true, attributeFilter: ["hidden"] });
    }
  }

  function wire() {
    wireSkin();
    qs("#ctools-close")?.addEventListener("click", close);
    // Same click-outside-to-close / Escape pattern every other menu-overlay
    // panel gets in app.js — this one was built here instead and had never
    // gotten either.
    qs("#ctools-overlay")?.addEventListener("click", (e) => {
      if (e.target === qs("#ctools-overlay")) close();
    });
    document.addEventListener("keydown", (e) => {
      const overlay = qs("#ctools-overlay");
      if (e.key === "Escape" && overlay && !overlay.hidden) close();
    });
    qs("#btn-ctools-refresh")?.addEventListener("click", refresh);
    qs("#btn-ctools-save")?.addEventListener("click", save);
    qs("#btn-ctools-check")?.addEventListener("click", check);
    qs("#btn-ctools-run")?.addEventListener("click", run);
    qs("#btn-ctools-delete")?.addEventListener("click", remove);
    qs("#btn-ctools-new")?.addEventListener("click", newFromTemplate);
    qs("#ctools-enabled")?.addEventListener("change", toggleEnabled);
    qs("#ctools-code")?.addEventListener("input", () => {
      state.dirty = true;
      setStatus("Unsaved changes.");
    });
    // Tab inserts four spaces instead of leaving the textarea — in a Python
    // editor, losing the ability to indent is not a small annoyance.
    qs("#ctools-code")?.addEventListener("keydown", (e) => {
      if (e.key !== "Tab") return;
      e.preventDefault();
      const ta = e.target;
      const start = ta.selectionStart;
      ta.value = ta.value.slice(0, start) + "    " + ta.value.slice(ta.selectionEnd);
      ta.selectionStart = ta.selectionEnd = start + 4;
      state.dirty = true;
    });
    // Ctrl/Cmd+S saves, because everyone tries it in a code box.
    qs("#ctools-code")?.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        save();
      }
    });
  }

  /* =======================================================================
   * Theme gallery — rendered into any container with #jui-theme-gallery,
   * which the Skin modal provides. Kept here rather than in ui-kit.js so
   * ui-kit stays a pure library with no markup assumptions.
   * ===================================================================== */

  function renderThemeGallery(host) {
    host = host || qs("#jui-theme-gallery");
    if (!host) return;
    const themes = UI().themes.all();
    const custom = UI().themes.custom();
    const current = UI().themes.current();
    host.innerHTML = "";
    host.className = "jui-theme-grid";

    Object.entries(themes).forEach(([id, theme]) => {
      const vars = theme.vars || {};
      // "None" has no colours to swatch — falling back to the same
      // defaults every other theme's missing vars use would make its card
      // look like a plain copy of the default Jarvis theme, which is
      // actively misleading for the one option that promises to apply
      // nothing. It gets a distinct checkerboard instead.
      const swatch = theme.isNone
        ? el("div", { class: "jui-theme__swatch jui-theme__swatch--none" })
        : el("div", { class: "jui-theme__swatch" }, [
            el("span", { style: "background:" + (vars["--bg"] || "#000") }),
            el("span", { style: "background:" + (vars["--accent"] || "#4fd8ff") }),
            el("span", { style: "background:" + (vars["--accent-secondary"] || "#f2b544") }),
            el("span", { style: "background:" + (vars["--bg-raised"] || "#111") }),
          ]);
      const card = el("button", {
        class: "jui-theme" + (id === current ? " is-active" : ""),
        type: "button", title: theme.hint || theme.label,
        onclick: () => { UI().themes.apply(id); renderThemeGallery(host); },
      }, [
        swatch,
        el("div", { class: "jui-theme__meta" }, [
          el("div", { class: "jui-theme__name" }, theme.label || id),
          el("div", { class: "jui-theme__hint" }, theme.author || ""),
        ]),
      ]);
      if (custom[id]) {
        card.appendChild(el("button", {
          class: "jui-theme__del", type: "button", title: "Delete this theme",
          onclick: async (e) => {
            e.stopPropagation();
            if (await UI().confirm({ title: "Delete theme " + (theme.label || id) + "?",
                                     confirmLabel: "Delete", level: "error" })) {
              UI().themes.remove(id);
              if (current === id) UI().themes.apply("jarvis");
              renderThemeGallery(host);
            }
          },
        }, "\u00d7"));
      }
      host.appendChild(card);
    });
  }

  async function importThemeDialog() {
    const json = await UI().prompt({
      title: "Import a theme",
      body: "Paste a theme JSON file. Only recognised CSS variables are applied.",
      placeholder: '{"label":"My skin","vars":{"--accent":"#ff0066"}}',
    });
    if (!json) return;
    const result = UI().themes.import(json);
    if (!result.ok) return UI().toast({ message: result.error, level: "error" });
    UI().themes.apply(result.id);
    renderThemeGallery();
    UI().toast({ message: "Imported and applied.", level: "success" });
  }

  function exportThemeDialog() {
    UI().dialog({
      title: "Export theme",
      body: "Copy this and save it as a .json file to share it.",
      pre: UI().themes.export(),
      level: "info",
    });
  }

  global.JarvisCustomTools = { open, close, refresh, renderThemeGallery,
                               importThemeDialog, exportThemeDialog };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})(window);
