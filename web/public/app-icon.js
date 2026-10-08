/* app-icon.js -- the browser-tab icon (favicon) for the web UI (L.48).
 *
 * WHAT IT DOES
 * ------------
 * The tab used to have no icon at all (`<link rel="icon" href="data:,">`). It now
 * shows Jarvis's own mark:
 *
 *   - the SAME mark the topbar shows, so it follows the skin: the default
 *     arc-reactor rings, Verity's smiley, a tool-registered persona's own SVG or
 *     PNG logo (whatever app.js's applyPersonaLogo() last put into `.brand-mark`);
 *   - in the skin's colour: the live `--accent` (and friends) from :root, so a
 *     persona preset, a theme, a custom accent and the saturation slider all
 *     carry over;
 *   - and a DIFFERENT icon while Jarvis is working on something: the same mark
 *     with a spinning arc and a badge in the secondary colour. It is a real
 *     change of picture rather than an animation of the normal one, so it is
 *     still visible in a browser that never animates tab icons.
 *
 * HOW IT STAYS IN SYNC (nothing in app.js has to call it for the look)
 * ------------------------------------------------------------------
 * A MutationObserver watches (1) the `.brand-mark` element(s) -- the logo is
 * swapped by innerHTML -- and (2) <html>'s style / class / data-* attributes --
 * every accent and theme change is a setProperty() or a class toggle there. A
 * change schedules ONE rebuild a moment later; a rebuild whose result would be
 * identical to what is already shown does not touch the <link>.
 *
 * The only thing app.js has to tell it is "busy": setRunning() calls
 * JarvisIcon.setBusy(running). Any other part of the UI can do the same with its
 * own reason string (`setBusy(true, "backup")`); the icon shows "working" while
 * ANY reason is active.
 *
 * WHY IT IS BUILT AS A STANDALONE SVG, NOT A COPY OF THE DOM
 * ----------------------------------------------------------
 * A favicon is rendered as an isolated image: it cannot see the page's CSS or
 * its custom properties. So the markup is rewritten before use: every
 * `var(--x)` is replaced by that variable's resolved value, and the handful of
 * classes the logo markup relies on (`.brand-mark__ring`, `--in`, `__core`) are
 * restated in a <style> inside the icon, minus the animation and the glow filter
 * (a 16px tile has no use for either). The page's own logo is never modified.
 *
 * The pure helpers (no DOM) are on JarvisIcon._pure for tests/verify_app_icon.js.
 */
(function (global) {
  "use strict";

  var DEFAULTS = {
    "--accent": "#4fd8ff",
    "--accent-soft": "#2ea9d6",
    "--accent-dim": "#164a63",
    "--accent-secondary": "#f2b544",
    "--accent-tertiary": "#2b5cff",
    "--bg": "#04070d",
  };

  // The logo markup is drawn in the 40x40 box of `.brand-mark`. The tile is a
  // dark rounded square so the coloured rings read on a light AND a dark tab
  // strip, and the mark sits slightly inside it so its outer ring isn't clipped.
  var TILE = 40;
  var TILE_RADIUS = 9;
  var MARK_SCALE = 0.84;
  var MARK_OFFSET = (TILE - TILE * MARK_SCALE) / 2;

  // The busy frames: the arc turns a step each frame. 8 steps of 45 degrees.
  var FRAME_COUNT = 8;
  var FRAME_MS = 260;

  var HEX = /^#[0-9a-f]{6}$/i;
  var SHORT_HEX = /^#[0-9a-f]{3}$/i;
  var RGB = /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})/i;

  // Turn whatever getComputedStyle gave us into something an <svg> attribute
  // accepts everywhere: "#rrggbb". "" or garbage -> the fallback.
  function cleanColor(value, fallback) {
    var v = String(value == null ? "" : value).trim();
    if (HEX.test(v)) return v.toLowerCase();
    if (SHORT_HEX.test(v)) {
      return ("#" + v[1] + v[1] + v[2] + v[2] + v[3] + v[3]).toLowerCase();
    }
    var m = RGB.exec(v);
    if (m) {
      var to = function (n) {
        var x = Math.max(0, Math.min(255, parseInt(n, 10) || 0));
        return (x < 16 ? "0" : "") + x.toString(16);
      };
      return "#" + to(m[1]) + to(m[2]) + to(m[3]);
    }
    return fallback;
  }

  // Resolve the palette the icon uses from a `read(name)` function (the live
  // computed style in the browser, a plain object in tests). Always returns six
  // valid #rrggbb strings.
  function resolvePalette(read) {
    var get = typeof read === "function" ? read : function () { return ""; };
    var out = {};
    Object.keys(DEFAULTS).forEach(function (name) {
      out[name] = cleanColor(get(name), DEFAULTS[name]);
    });
    return out;
  }

  // Replace every `var(--x)` / `var(--x, fallback)` in some markup with a
  // concrete colour. A variable the palette doesn't know becomes the accent: an
  // unresolved var() in an isolated image renders as nothing at all, and a mark
  // that quietly disappears is worse than one in the wrong shade.
  function resolveVars(markup, palette) {
    return String(markup == null ? "" : markup).replace(
      /var\(\s*(--[a-z0-9-]+)\s*(?:,[^)]*)?\)/gi,
      function (_all, name) {
        return palette[name.toLowerCase()] || palette["--accent"];
      }
    );
  }

  // What an icon must never carry out of the page's markup: anything that runs
  // or loads from elsewhere. The logo comes from the page's own DOM (so it is
  // already trusted), but a tab icon is also a place nobody looks at, so it is
  // cheap to be strict. Embedded data: images (a PNG persona logo) stay.
  function stripUnsafe(markup) {
    return String(markup == null ? "" : markup)
      .replace(/<\s*script[\s\S]*?<\s*\/\s*script\s*>/gi, "")
      .replace(/<\s*script[^>]*\/?\s*>/gi, "")
      .replace(/<\s*foreignObject[\s\S]*?<\s*\/\s*foreignObject\s*>/gi, "")
      .replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
      .replace(/(href|src)\s*=\s*("|')\s*(?!data:image\/|#)[^"']*\2/gi, "");
  }

  // The class rules the stock logo markup (and Verity's) leans on, restated with
  // concrete colours. No animation, no drop-shadow filter.
  function iconStyle(p) {
    return (
      ".brand-mark__ring{fill:none;stroke:" + p["--accent"] + ";stroke-width:1.9;opacity:.95}" +
      ".brand-mark__ring--in{stroke:" + p["--accent-soft"] + ";stroke-dasharray:3 4}" +
      ".brand-mark__core{fill:" + p["--accent"] + "}"
    );
  }

  // The "working" decoration: an arc turning around the outside of the tile and
  // a badge in the secondary colour, bottom right. `frame` picks the arc's angle.
  function busyDecoration(p, frame) {
    var n = ((Number(frame) || 0) % FRAME_COUNT + FRAME_COUNT) % FRAME_COUNT;
    var angle = n * (360 / FRAME_COUNT);
    var c = p["--accent-secondary"];
    return (
      '<g transform="rotate(' + angle + ' 20 20)">' +
        '<circle cx="20" cy="20" r="19" fill="none" stroke="' + c +
        '" stroke-width="2.4" stroke-linecap="round" stroke-dasharray="26 94"/>' +
      "</g>" +
      '<circle cx="31" cy="31" r="7.2" fill="' + p["--bg"] + '"/>' +
      '<circle cx="31" cy="31" r="5.6" fill="' + c + '"/>' +
      '<circle cx="31" cy="31" r="1.9" fill="' + p["--bg"] + '"/>'
    );
  }

  // The whole icon as one SVG string.
  //   brand   the inner markup of `.brand-mark` (what applyPersonaLogo() put there)
  //   palette from resolvePalette()
  //   busy    true -> the "working" variant
  //   frame   which step of the turning arc (busy only)
  function buildSvg(opts) {
    var o = opts || {};
    var p = o.palette || resolvePalette(null);
    var inner = resolveVars(stripUnsafe(o.brand || ""), p);
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"' +
      ' viewBox="0 0 ' + TILE + " " + TILE + '" width="64" height="64">' +
      "<style>" + iconStyle(p) + "</style>" +
      '<rect width="' + TILE + '" height="' + TILE + '" rx="' + TILE_RADIUS + '" fill="' + p["--bg"] + '"/>' +
      '<g transform="translate(' + MARK_OFFSET.toFixed(2) + " " + MARK_OFFSET.toFixed(2) +
      ") scale(" + MARK_SCALE + ')">' + inner + "</g>" +
      (o.busy ? busyDecoration(p, o.frame) : "") +
      "</svg>"
    );
  }

  function toDataUri(svg) {
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }

  // What would be shown, as a string. Two equal signatures mean the <link> need
  // not be touched. (The brand markup can be a long PNG data URI; it is part of
  // the signature on purpose -- a different logo must always win.)
  function signature(opts) {
    var o = opts || {};
    var p = o.palette || {};
    return [
      o.busy ? "busy:" + (((Number(o.frame) || 0) % FRAME_COUNT + FRAME_COUNT) % FRAME_COUNT) : "idle",
      Object.keys(DEFAULTS).map(function (k) { return p[k] || ""; }).join(","),
      o.brand || "",
    ].join("|");
  }

  // ----------------------------------------------------------------- browser
  var api = { _pure: {
    cleanColor: cleanColor, resolvePalette: resolvePalette, resolveVars: resolveVars,
    stripUnsafe: stripUnsafe, iconStyle: iconStyle, busyDecoration: busyDecoration,
    buildSvg: buildSvg, toDataUri: toDataUri, signature: signature,
    FRAME_COUNT: FRAME_COUNT, FRAME_MS: FRAME_MS, DEFAULTS: DEFAULTS,
  } };
  global.JarvisIcon = api;

  var doc = global.document;
  if (!doc || !doc.documentElement || typeof global.getComputedStyle !== "function") return;

  var reasons = {};          // busy reason -> true
  var frame = 0;
  var timer = null;          // the frame ticker (busy + motion allowed)
  var pending = null;        // the debounce timer for a rebuild
  var shown = "";            // signature of what the <link> shows now
  var linkEl = null;

  function isBusy() { return Object.keys(reasons).length > 0; }

  // Motion is off when the owner turned Animations off in the Skin modal
  // (ui-kit.js puts `jui-no-motion` on <html>) or the OS asks for reduced motion.
  function motionAllowed() {
    if (doc.documentElement.classList.contains("jui-no-motion")) return false;
    try {
      if (global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
    } catch (_) { /* no matchMedia: assume motion is fine */ }
    return true;
  }

  // Swap in a FRESH <link> each time rather than editing href in place: some
  // browsers only re-read a tab icon when the element itself is replaced.
  function setHref(href) {
    var old = (linkEl && linkEl.parentNode) ? linkEl
      : (doc.getElementById("app-icon") || doc.querySelector('link[rel~="icon"]'));
    var next = doc.createElement("link");
    next.rel = "icon";
    next.type = "image/svg+xml";
    next.id = "app-icon";
    next.href = href;
    if (old && old.parentNode) old.parentNode.replaceChild(next, old);
    else doc.head.appendChild(next);
    linkEl = next;
  }

  function readPalette() {
    var cs = global.getComputedStyle(doc.documentElement);
    return resolvePalette(function (name) { return cs.getPropertyValue(name); });
  }

  function currentBrand() {
    var mark = doc.querySelector(".brand-mark");
    return mark ? mark.innerHTML : "";
  }

  function render() {
    pending = null;
    var opts = { brand: currentBrand(), palette: readPalette(), busy: isBusy(), frame: frame };
    var sig = signature(opts);
    if (sig === shown) return;
    shown = sig;
    try { setHref(toDataUri(buildSvg(opts))); } catch (_) { /* an icon is never worth an error */ }
  }

  // Coalesce a burst (applyAccent sets a dozen properties) into one rebuild.
  function schedule() {
    if (pending !== null) return;
    pending = global.setTimeout(render, 80);
  }

  function syncTicker() {
    var want = isBusy() && motionAllowed();
    if (want && timer === null) {
      timer = global.setInterval(function () {
        frame = (frame + 1) % FRAME_COUNT;
        render();
      }, FRAME_MS);
    } else if (!want && timer !== null) {
      global.clearInterval(timer);
      timer = null;
      frame = 0;
    }
  }

  // setBusy(true, "reason") / setBusy(false, "reason"); the reason defaults to
  // "run". The icon is "working" while any reason is set.
  api.setBusy = function (on, reason) {
    var key = String(reason || "run");
    if (on) reasons[key] = true; else delete reasons[key];
    syncTicker();
    render();
  };
  api.isBusy = isBusy;
  api.refresh = function () { shown = ""; render(); };

  function start() {
    if (typeof global.MutationObserver === "function") {
      var obs = new global.MutationObserver(function () { syncTicker(); schedule(); });
      obs.observe(doc.documentElement, { attributes: true, attributeFilter: ["style", "class", "data-theme", "data-skin"] });
      doc.querySelectorAll(".brand-mark").forEach(function (m) {
        obs.observe(m, { childList: true, subtree: true });
      });
    }
    render();
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start);
  else start();
})(typeof window !== "undefined" ? window : this);
