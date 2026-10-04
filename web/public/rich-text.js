// rich-text.js — fenced code blocks for the Ask thread (master plan Part I.1).
//
// Loads BEFORE app.js (like ui-kit.js) and depends on nothing in it. It owns:
//   * enhanceCodeBlocks(root, opts) — DOM post-processing that wraps every
//     <pre><code> in a .codeblock (sticky bar: language, line count, Wrap,
//     Copy; collapse for long blocks; syntax colouring when highlight.js is
//     present and the language is known).
//   * copyText(text)                — THE clipboard helper (navigator.clipboard
//     with an off-screen <textarea> + execCommand fallback). Every copy
//     button in the Ask panel goes through it (I-B9).
//   * codeTextForCopy / splitFences / normalizeLang — pure helpers.
//   * renderUserText(bubble, text)  — your own bubble: fences become code
//     blocks, prose stays plain text (D-I2).
//   * attach(root)                  — the one delegated click handler.
//
// Security model: the chrome is built here, AFTER DOMPurify has run, so
// DOMPurify never sees it. Model-authored HTML can carry any class or data-
// attribute it likes, so a button is only live when THIS file created it —
// tracked in a WeakMap, which cannot be forged from markup.
//
// Exposed as window.JarvisRich. app.js degrades to plain rendering if this
// file failed to load.
(() => {
  "use strict";

  // ---- tunables ----------------------------------------------------------
  const COLLAPSE_OVER = 30;      // lines above which a block starts collapsed
  const HIGHLIGHT_CAP = 100000;  // chars; bigger blocks stay plain (still copyable)
  const CACHE_MAX = 40;          // highlighted blocks remembered between re-renders
  const WRAP_KEY = "jarvis.code.wrap";
  const FLASH_MS = 1600;

  // ---- language handling -------------------------------------------------
  // highlight.js resolves most of its own aliases (js, ts, py, sh, yml, html,
  // md, console...). This map only covers common labels it does not know.
  const HL_ALIAS = {
    ps1: "powershell", pwsh: "powershell",
    jsonc: "json", json5: "json",
    rs: "rust", golang: "go",
    terminal: "shell", shellsession: "shell",
    bat: "dos", cmd: "dos",
  };
  const NO_HIGHLIGHT = new Set(["", "text", "txt", "plain", "plaintext"]);
  // D-I3 (K.6.4): only these labels get the leading "$ " stripped on copy.
  const SHELL_LANGS = new Set(["bash", "sh", "shell", "console", "zsh"]);

  function normalizeLang(raw) {
    const r = String(raw == null ? "" : raw).trim().toLowerCase();
    const hl = NO_HIGHLIGHT.has(r) ? "" : (HL_ALIAS[r] || r);
    return { raw: r, label: r || "text", hl };
  }

  function isShellLang(raw) {
    return SHELL_LANGS.has(String(raw == null ? "" : raw).trim().toLowerCase());
  }

  // What Copy puts on the clipboard. Exactly one trailing newline is dropped
  // (marked appends one to every block). A shell block whose every non-empty
  // line starts with "$ " loses that prompt (D-I3) and says so; anything else
  // is byte-for-byte.
  function codeTextForCopy(raw, lang) {
    let t = String(raw == null ? "" : raw);
    if (t.endsWith("\n")) t = t.slice(0, -1);
    if (isShellLang(lang)) {
      const lines = t.split("\n");
      const nonEmpty = lines.filter((l) => l.trim() !== "");
      if (nonEmpty.length && nonEmpty.every((l) => l.startsWith("$ "))) {
        return {
          text: lines.map((l) => (l.startsWith("$ ") ? l.slice(2) : l)).join("\n"),
          stripped: true,
        };
      }
    }
    return { text: t, stripped: false };
  }

  function countLines(text) {
    let t = String(text == null ? "" : text);
    if (t.endsWith("\n")) t = t.slice(0, -1);
    return t === "" ? 0 : t.split("\n").length;
  }

  // Split a plain-text message into prose and fenced-code segments (your own
  // bubble). Fences: ``` or ~~~, up to 3 spaces of indent, closed by the same
  // character at least as long and nothing else on the line; an unclosed fence
  // runs to the end, as in CommonMark. Prose segments are the lines between
  // fences joined with "\n" (the fence lines' own newlines are not included).
  function splitFences(text) {
    const lines = String(text == null ? "" : text).split("\n");
    const out = [];
    let prose = [];
    let open = null;
    let body = [];
    const flushProse = () => {
      if (prose.length) out.push({ type: "text", text: prose.join("\n") });
      prose = [];
    };
    for (const line of lines) {
      if (!open) {
        const m = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
        // A backtick fence's info string may not contain a backtick: that is
        // inline code ("```js``` like this"), not a fence.
        if (m && !(m[1][0] === "`" && m[2].includes("`"))) {
          flushProse();
          const word = m[2].trim().split(/\s+/)[0] || "";
          open = { ch: m[1][0], len: m[1].length, lang: word.replace(/[^A-Za-z0-9_+#.-]/g, "") };
          body = [];
        } else {
          prose.push(line);
        }
      } else {
        const m = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(line);
        if (m && m[1][0] === open.ch && m[1].length >= open.len) {
          out.push({ type: "code", lang: open.lang, text: body.join("\n") });
          open = null;
          body = [];
        } else {
          body.push(line);
        }
      }
    }
    if (open) out.push({ type: "code", lang: open.lang, text: body.join("\n") });
    else flushProse();
    return out;
  }

  // ---- clipboard (I-B9) --------------------------------------------------
  function legacyCopy(text) {
    if (typeof document === "undefined" || !document.body) return false;
    const active = document.activeElement;
    let selStart = null;
    let selEnd = null;
    try {
      if (active && typeof active.selectionStart === "number") {
        selStart = active.selectionStart;
        selEnd = active.selectionEnd;
      }
    } catch (_) { /* some input types throw */ }
    const sel = typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
    const ranges = [];
    if (sel) for (let i = 0; i < sel.rangeCount; i += 1) ranges.push(sel.getRangeAt(i));
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.setAttribute("aria-hidden", "true");
    ta.style.cssText = "position:fixed;top:-1000px;left:-1000px;opacity:0;pointer-events:none;";
    document.body.appendChild(ta);
    let ok = false;
    try {
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = document.execCommand("copy");
    } catch (_) {
      ok = false;
    }
    ta.remove();
    // Give the composer back exactly what it had: selection first, then focus.
    try {
      if (sel) {
        sel.removeAllRanges();
        ranges.forEach((r) => sel.addRange(r));
      }
      if (active && typeof active.focus === "function") {
        active.focus({ preventScroll: true });
        if (selStart !== null && typeof active.setSelectionRange === "function") {
          active.setSelectionRange(selStart, selEnd);
        }
      }
    } catch (_) { /* best effort */ }
    return ok;
  }

  // Browser clipboard on purpose (not the server's OS clipboard tool): the page
  // can do this itself, no round trip. Resolves to true/false so callers can
  // show "Copied" or "Couldn't copy".
  async function copyText(text) {
    const t = String(text == null ? "" : text);
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(t);
        return true;
      }
    } catch (_) { /* denied or insecure context: fall through to the fallback */ }
    return legacyCopy(t);
  }

  // ---- highlighting ------------------------------------------------------
  const hlCache = new Map(); // "lang\0text" -> sanitised highlighted HTML

  function highlightHtml(hl, text) {
    if (!hl || text.length > HIGHLIGHT_CAP) return null;
    if (typeof hljs === "undefined" || !hljs || typeof hljs.getLanguage !== "function") return null;
    if (!hljs.getLanguage(hl)) return null; // never highlightAuto: slow, often wrong
    const key = hl + "\u0000" + text;
    if (hlCache.has(key)) return hlCache.get(key);
    let html;
    try {
      html = hljs.highlight(text, { language: hl, ignoreIllegals: true }).value;
    } catch (_) {
      return null;
    }
    // The library's output is escaped HTML, but re-sanitise anyway so "nothing
    // unsanitised reaches the DOM" stays true: spans and their class only.
    if (typeof DOMPurify !== "undefined" && DOMPurify && DOMPurify.sanitize) {
      html = DOMPurify.sanitize(html, { ALLOWED_TAGS: ["span"], ALLOWED_ATTR: ["class"] });
    }
    if (hlCache.size >= CACHE_MAX) hlCache.delete(hlCache.keys().next().value);
    hlCache.set(key, html);
    return html;
  }

  function applyHighlight(code, hl, text) {
    const html = highlightHtml(hl, text);
    if (html == null) return;
    code.innerHTML = html;
    // Highlighting only wraps text in spans, so textContent must still be the
    // source. If it ever is not, plain text wins — Copy must be exact.
    if (code.textContent !== text) {
      code.textContent = text;
      return;
    }
    code.classList.add("hljs");
  }

  // ---- code block chrome -------------------------------------------------
  const blocks = new WeakMap();   // wrapper element -> block state
  const actions = new WeakMap();  // button element  -> function (only OUR buttons)

  function readWrapPref() {
    try { return localStorage.getItem(WRAP_KEY) === "1"; } catch (_) { return false; }
  }
  function writeWrapPref(on) {
    try { localStorage.setItem(WRAP_KEY, on ? "1" : "0"); } catch (_) { /* private mode etc. */ }
  }

  function mk(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function langOfCode(code) {
    for (const c of Array.from(code.classList || [])) {
      if (c.startsWith("language-")) return normalizeLang(c.slice(9));
    }
    return normalizeLang("");
  }

  function copyLabel(b) {
    if (b.streaming) return "Copy";
    return b.collapsed ? "Copy " + b.lines + " lines" : "Copy";
  }

  function syncCopy(b) {
    if (b.flashTimer) return; // feedback showing; it restores itself
    b.copyBtn.textContent = copyLabel(b);
  }

  function syncMore(b) {
    if (!b.moreBtn) return;
    b.moreBtn.textContent = b.collapsed ? "Show all " + b.lines + " lines" : "Collapse";
    b.moreBtn.setAttribute("aria-expanded", b.collapsed ? "false" : "true");
  }

  function setCollapsed(b, on) {
    b.collapsed = on;
    b.wrapper.classList.toggle("codeblock--collapsed", on);
    syncMore(b);
    syncCopy(b);
  }

  function applyWrap(b, on) {
    b.wrapper.classList.toggle("codeblock--wrap", on);
    b.wrapBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function setWrapEverywhere(on) {
    writeWrapPref(on);
    if (typeof document === "undefined") return;
    document.querySelectorAll(".codeblock").forEach((w) => {
      const b = blocks.get(w);
      if (b) applyWrap(b, on);
    });
  }

  function flash(b, label, ok) {
    if (b.flashTimer) clearTimeout(b.flashTimer);
    b.copyBtn.textContent = label;
    b.copyBtn.classList.toggle("is-ok", ok);
    b.copyBtn.classList.toggle("is-fail", !ok);
    b.flashTimer = setTimeout(() => {
      b.flashTimer = 0;
      b.copyBtn.classList.remove("is-ok", "is-fail");
      syncCopy(b);
    }, FLASH_MS);
  }

  async function copyBlock(b) {
    if (b.streaming) return;
    // textContent at click time: the live source, whatever highlighting did.
    const r = codeTextForCopy(b.code.textContent, b.lang.raw);
    const ok = await copyText(r.text);
    if (ok && r.stripped) {
      b.copyBtn.title = "The leading \u201c$ \u201d prompt was removed from what was copied.";
      flash(b, "Copied \u00b7 $ removed", true);
    } else {
      flash(b, ok ? "Copied" : "Couldn\u2019t copy", ok);
    }
  }

  function buildBlock(pre, code, opts) {
    const lang = langOfCode(code);
    const text = code.textContent;
    const lines = countLines(text);
    const streaming = Boolean(opts && opts.streaming);

    const wrapper = mk("div", "codeblock");
    wrapper.dataset.lang = lang.label;
    const bar = mk("div", "codeblock__bar");
    const langEl = mk("span", "codeblock__lang", lang.label);
    const metaEl = mk("span", "codeblock__meta",
      streaming ? "writing\u2026" : lines + (lines === 1 ? " line" : " lines"));
    const spacer = mk("span", "codeblock__spacer");
    const wrapBtn = mk("button", "codeblock__wrap", "Wrap");
    wrapBtn.type = "button";
    wrapBtn.title = "Wrap long lines";
    const copyBtn = mk("button", "codeblock__copy", "Copy");
    copyBtn.type = "button";
    copyBtn.setAttribute("aria-live", "polite");
    bar.append(langEl, metaEl, spacer, wrapBtn, copyBtn);

    const b = {
      wrapper, pre, code, lang, lines, streaming,
      copyBtn, wrapBtn, moreBtn: null,
      collapsed: false, flashTimer: 0,
    };

    if (streaming) {
      copyBtn.disabled = true;
      copyBtn.title = "Available once the block is complete";
      wrapper.classList.add("codeblock--streaming");
    } else if (isShellLang(lang.raw) && codeTextForCopy(text, lang.raw).stripped) {
      copyBtn.title = "Copy \u2014 the leading \u201c$ \u201d prompt will be removed";
    } else {
      copyBtn.title = "Copy this code";
    }

    pre.classList.add("codeblock__pre");
    pre.tabIndex = 0;
    pre.setAttribute("aria-label", lang.label + " code");

    pre.parentNode.replaceChild(wrapper, pre);
    wrapper.append(bar, pre);

    if (!streaming) applyHighlight(code, lang.hl, text);

    if (!streaming && lines > COLLAPSE_OVER) {
      b.moreBtn = mk("button", "codeblock__more");
      b.moreBtn.type = "button";
      wrapper.appendChild(b.moreBtn);
      actions.set(b.moreBtn, () => setCollapsed(b, !b.collapsed));
      setCollapsed(b, true);
    }

    actions.set(copyBtn, () => copyBlock(b));
    actions.set(wrapBtn, () => setWrapEverywhere(!wrapper.classList.contains("codeblock--wrap")));
    applyWrap(b, readWrapPref());
    blocks.set(wrapper, b);
    return b;
  }

  // Idempotent: a <pre> whose parent is one of OUR wrappers is skipped, so the
  // incremental renderer can re-run this over text it already enhanced. A
  // model-authored <div class="codeblock"> is not in `blocks`, so it earns no
  // trust — its <pre> is enhanced like any other.
  function enhanceCodeBlocks(root, opts) {
    if (!root || typeof root.querySelectorAll !== "function") return 0;
    const o = opts || {};
    const pres = Array.from(root.querySelectorAll("pre"));
    let made = 0;
    pres.forEach((pre, i) => {
      if (blocks.has(pre.parentNode)) return;
      const code = pre.firstElementChild;
      if (!code || code.tagName !== "CODE" || !pre.parentNode) return;
      buildBlock(pre, code, { streaming: Boolean(o.streamingLast) && i === pres.length - 1 });
      made += 1;
    });
    return made;
  }

  // [{ lang, text, lines }] for each enhanced block in a message element —
  // text is the source with one trailing newline removed, never prompt-stripped.
  function blocksIn(msgEl) {
    const out = [];
    if (!msgEl || typeof msgEl.querySelectorAll !== "function") return out;
    msgEl.querySelectorAll(".codeblock").forEach((w) => {
      const b = blocks.get(w);
      if (!b || b.streaming) return;
      let t = b.code.textContent;
      if (t.endsWith("\n")) t = t.slice(0, -1);
      out.push({ lang: b.lang.label, text: t, lines: b.lines });
    });
    return out;
  }

  // Your own bubble (D-I2): prose stays textContent — a ** in your message must
  // not turn bold — and fences go through the same block builder. Returns false
  // (and leaves the bubble alone) when the text has no fence at all.
  function renderUserText(bubbleEl, text) {
    const segs = splitFences(text);
    if (!segs.some((s) => s.type === "code")) return false;
    bubbleEl.textContent = "";
    for (const s of segs) {
      if (s.type === "text") {
        if (s.text === "") continue;
        bubbleEl.appendChild(mk("div", "codeblock-prose", s.text));
      } else {
        const pre = mk("pre");
        const code = mk("code", s.lang ? "language-" + s.lang.toLowerCase() : "", s.text + "\n");
        pre.appendChild(code);
        bubbleEl.appendChild(pre);
      }
    }
    enhanceCodeBlocks(bubbleEl);
    return true;
  }

  // One delegated listener per root (the Ask thread). Bubbles are rebuilt via
  // innerHTML, so per-button listeners would be lost; and only buttons THIS
  // file created are in `actions`, so a forged <button> does nothing.
  const attached = new WeakSet();
  function attach(root) {
    if (!root || attached.has(root)) return;
    attached.add(root);
    root.addEventListener("click", (e) => {
      const t = e.target;
      const btn = t && typeof t.closest === "function" ? t.closest("button") : null;
      if (!btn) return;
      const act = actions.get(btn);
      if (!act) return;
      e.preventDefault();
      act();
    });
  }

  globalThis.JarvisRich = {
    enhanceCodeBlocks, renderUserText, blocksIn, attach,
    copyText, codeTextForCopy, splitFences, normalizeLang, isShellLang, countLines,
  };
})();
