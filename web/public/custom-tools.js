/* ============================================================================
 * custom-tools.js — the theme gallery the Skin modal uses.
 *
 * This file used to hold the Custom Tools panel. That panel was replaced by the
 * Tool Manager (tool-manager.js, master plan L.25), which keeps everything it
 * did — list, edit, validate, enable/disable, run, delete, templates — and adds
 * the loaded-tools view, per-tool safeguards, import and a reworked editor.
 * What stays here is the theme gallery and the Skin modal's import/export/
 * save-theme wiring, which the two used to share this file for. It is also why
 * `JarvisCustomTools.open()` still exists: it forwards to the Tool Manager, so
 * anything that still calls it lands in the right place.
 * ========================================================================= */

(function (global) {
  "use strict";

  const UI = () => global.JarvisUI;
  const el = (...a) => global.JarvisUI._el(...a);
  const qs = (sel) => document.querySelector(sel);

  function open(opts) {
    if (global.JarvisToolManager) global.JarvisToolManager.open(opts);
  }
  function close() {
    if (global.JarvisToolManager) global.JarvisToolManager.close();
  }
  function refresh() {
    if (global.JarvisToolManager) global.JarvisToolManager.refresh();
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
      // A built-in theme the owner switched off in the Tool Manager is not offered,
      // unless it is the one applied. Imported / saved themes are the owner's own and
      // are never hidden here (they can be deleted instead).
      if (!custom[id] && id !== current && global.JarvisHost && global.JarvisHost.isSkinOff && global.JarvisHost.isSkinOff("theme:" + id)) return;
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
