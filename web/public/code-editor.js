/* ============================================================================
 * code-editor.js — the code editor the Tool Manager uses (master plan L.33).
 *
 * A plain <textarea> with a painted layer behind it: the textarea keeps every
 * native behaviour (caret, selection, IME, scrolling, undo), its text is made
 * transparent, and a <pre> underneath draws the same text with syntax colours.
 * Nothing here is a framework; there is no build step and no network fetch.
 *
 * What it does
 *   - Python syntax highlighting (own tokenizer: strings incl. triple-quoted
 *     and f-strings, comments, numbers, keywords, builtins, def/class names,
 *     decorators, calls, CONSTANTS, self).
 *   - VS Code style editing: smart Enter (keeps/raises indent after ':' or an
 *     open bracket, dedents after return/pass/...), Tab / Shift+Tab on a
 *     selection, soft-tab Backspace, auto-closing brackets and quotes with
 *     type-over, Ctrl+/ comment toggle, Alt+Up/Down move line, Shift+Alt+
 *     Up/Down copy line, bracket-pair highlight, current-line highlight,
 *     error-line marker.
 *   - A completion popup (Ctrl+Space, or after two identifier characters):
 *     Jarvis tool snippets, Python keywords and builtins, words from the file.
 *   - Inline "ghost text" suggestions from Jarvis (VS Code's inline
 *     suggestions): after a pause at the end of a line the editor asks
 *     `opts.suggest()` for what comes next and shows it dimmed; Tab accepts,
 *     Ctrl+Right takes one word, Esc dismisses, Alt+\ asks right now. Typing
 *     the suggested characters keeps the rest on screen, like VS Code.
 *
 * What it does not do: it never talks to the network itself. `opts.suggest`
 * is supplied by the caller (the Tool Manager posts to /api/ctools/:name/
 * suggest). A suggestion is only text on screen until it is accepted, and the
 * editor never saves anything.
 *
 * The pure functions (tokenizer, edit helpers, completion ranking, outline)
 * are exposed as JarvisCodeEditor._pure and verified by
 * tests/verify_code_editor.js without a DOM.
 * ========================================================================= */

(function (global) {
  "use strict";

  const IND = "    ";

  /* =======================================================================
   * Tokenizer
   * ===================================================================== */

  const KEYWORDS = new Set(("and as assert async await break class continue def del elif else except finally for " +
    "from global if import in is lambda nonlocal not or pass raise return try while with yield").split(" "));
  const LITERALS = new Set(["True", "False", "None"]);
  const BUILTINS = new Set(("abs all any ascii bin bool breakpoint bytearray bytes callable chr classmethod compile " +
    "complex delattr dict dir divmod enumerate eval exec filter float format frozenset getattr globals hasattr hash " +
    "help hex id input int isinstance issubclass iter len list locals map max memoryview min next object oct open " +
    "ord pow print property range repr reversed round set setattr slice sorted staticmethod str sum super tuple " +
    "type vars zip Exception ValueError TypeError KeyError OSError RuntimeError NotImplementedError " +
    "FileNotFoundError TimeoutError StopIteration __name__ __file__").split(" "));

  const RE_ID = /[A-Za-z_][A-Za-z0-9_]*/y;
  const RE_NUM = /(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?[jJ]?)/y;
  const RE_STR_PREFIX = /^[rRbBfFuU]{1,2}$/;

  function atLineStart(src, i) {
    let j = i - 1;
    while (j >= 0 && (src[j] === " " || src[j] === "\t")) j--;
    return j < 0 || src[j] === "\n";
  }

  function nextNonSpace(src, i) {
    while (i < src.length && (src[i] === " " || src[i] === "\t")) i++;
    return src[i];
  }

  // -> [{t, s, e}] for every STYLED span, in order; anything not listed is plain.
  function tokenize(src) {
    src = String(src == null ? "" : src);
    const out = [], n = src.length;
    const push = (t, s, e) => { if (e > s) out.push({ t, s, e }); };
    let i = 0, prev = "";               // prev = text of the last significant token
    while (i < n) {
      const c = src[i];
      if (c === " " || c === "\t" || c === "\n" || c === "\r") { i++; continue; }
      if (c === "#") {
        let e = src.indexOf("\n", i); if (e < 0) e = n;
        push("comment", i, e); i = e; prev = "#"; continue;
      }
      if (c === "@" && atLineStart(src, i)) {
        let j = i + 1;
        while (j < n && /[A-Za-z0-9_.]/.test(src[j])) j++;
        push("decorator", i, j); i = j; prev = "@"; continue;
      }
      let strStart = -1, quoteAt = -1, isF = false;
      if (c === "'" || c === '"') { strStart = i; quoteAt = i; }
      else if (/[A-Za-z_]/.test(c)) {
        RE_ID.lastIndex = i; const m = RE_ID.exec(src);
        const word = m[0], end = i + word.length;
        if (RE_STR_PREFIX.test(word) && (src[end] === "'" || src[end] === '"')) {
          strStart = i; quoteAt = end; isF = /[fF]/.test(word);
        } else {
          let t = null;
          if (prev === "def") t = "fn-def";
          else if (prev === "class") t = "cls-def";
          else if (KEYWORDS.has(word)) t = "kw";
          else if (LITERALS.has(word)) t = "lit";
          else if (prev === ".") t = nextNonSpace(src, end) === "(" ? "call" : null;
          else if (nextNonSpace(src, end) === "(") t = BUILTINS.has(word) ? "builtin" : "call";
          else if (BUILTINS.has(word)) t = "builtin";
          else if (word === "self" || word === "cls") t = "self";
          else if (/^[A-Z][A-Z0-9_]+$/.test(word)) t = "const";
          else if (/^[A-Z]/.test(word)) t = "type";
          if (t) push(t, i, end);
          i = end; prev = word; continue;
        }
      }
      if (strStart >= 0) {
        const q = src[quoteAt];
        const triple = src.startsWith(q + q + q, quoteAt);
        const close = triple ? q + q + q : q;
        let j = quoteAt + (triple ? 3 : 1), seg = strStart;
        while (j < n) {
          const ch = src[j];
          if (ch === "\\") { j += 2; continue; }
          if (!triple && ch === "\n") break;                 // unterminated: stop at the line end
          if (src.startsWith(close, j)) { j += close.length; break; }
          if (isF && ch === "{") {
            if (src[j + 1] === "{") { j += 2; continue; }
            push("string", seg, j);
            let depth = 1, k = j + 1;
            while (k < n && depth > 0) {
              const d = src[k];
              if (d === "{") depth++; else if (d === "}") depth--; else if (d === "\n" && !triple) break;
              k++;
            }
            push("interp", j, k); seg = k; j = k; continue;
          }
          j++;
        }
        if (j > n) j = n;
        push("string", seg, j); i = j; prev = "str"; continue;
      }
      if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(src[i + 1] || ""))) {
        RE_NUM.lastIndex = i; const m = RE_NUM.exec(src);
        if (m && m[0].length) { push("num", i, i + m[0].length); i += m[0].length; prev = "num"; continue; }
      }
      if ("+-*/%=<>!&|^~".indexOf(c) >= 0) { push("op", i, i + 1); i++; prev = c; continue; }
      prev = c; i++;
    }
    return out;
  }

  function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  // marks: offsets (of single characters) to wrap in .ce-t-bm (the bracket pair).
  function renderHTML(src, tokens, marks) {
    src = String(src == null ? "" : src);
    const mo = marks && marks.length ? marks.slice().sort((a, b) => a - b) : [];
    let html = "", pos = 0;
    function span(s, e, cls) {
      if (e <= s) return;
      let cut = -1;
      for (let k = 0; k < mo.length; k++) if (mo[k] >= s && mo[k] < e) { cut = mo[k]; break; }
      if (cut < 0) { const t = esc(src.slice(s, e)); html += cls ? '<span class="ce-t-' + cls + '">' + t + "</span>" : t; return; }
      span(s, cut, cls);
      html += '<span class="' + (cls ? "ce-t-" + cls + " " : "") + 'ce-t-bm">' + esc(src[cut]) + "</span>";
      span(cut + 1, e, cls);
    }
    for (const tk of tokens) { span(pos, tk.s, null); span(tk.s, tk.e, tk.t); pos = tk.e; }
    span(pos, src.length, null);
    return html + "\n";
  }

  /* =======================================================================
   * Positions
   * ===================================================================== */

  function lineStartOf(text, pos) { return text.lastIndexOf("\n", pos - 1) + 1; }
  function lineEndOf(text, pos) { const e = text.indexOf("\n", pos); return e < 0 ? text.length : e; }

  function posToLineCol(text, pos) {
    pos = Math.max(0, Math.min(pos, text.length));
    let line = 0, last = -1, k = -1;
    while ((k = text.indexOf("\n", k + 1)) >= 0 && k < pos) { line++; last = k; }
    return { line, col: pos - (last + 1) };
  }

  function visualCol(lineText, col, tab) {
    tab = tab || 4;
    let v = 0;
    for (let i = 0; i < col && i < lineText.length; i++) v += lineText[i] === "\t" ? tab - (v % tab) : 1;
    return v;
  }

  function applyEdit(text, ed) { return text.slice(0, ed.start) + ed.insert + text.slice(ed.end); }

  /* =======================================================================
   * Editing helpers — each returns an edit {start, end, insert, selStart,
   * selEnd} against the ORIGINAL text (selStart/selEnd are offsets in the text
   * AFTER the edit), or null when the key should do its normal thing.
   * ===================================================================== */

  function stripTrailingComment(s) { return s.replace(/\s+#[^'"]*$/, ""); }

  function bracketBalance(s) {
    let b = 0;
    for (const ch of s) { if ("([{".indexOf(ch) >= 0) b++; else if (")]}".indexOf(ch) >= 0) b--; }
    return b;
  }

  function smartEnter(text, s, e) {
    const ls = lineStartOf(text, s);
    const before = text.slice(ls, s);
    const indent = /^[ \t]*/.exec(before)[0];
    const after = text.slice(e, lineEndOf(text, e));
    const code = stripTrailingComment(before).replace(/\s+$/, "");
    const last = code.slice(-1);
    const closers = { "(": ")", "[": "]", "{": "}" };
    if (closers[last] && after[0] === closers[last] && before.slice(-1) === last) {
      const ins = "\n" + indent + IND + "\n" + indent;
      const caret = s + 1 + indent.length + IND.length;
      return { start: s, end: e, insert: ins, selStart: caret, selEnd: caret };
    }
    let next = indent;
    if (closers[last] || last === ":") next = indent + IND;
    else if (/^\s*(return|pass|break|continue|raise)\b/.test(before) && bracketBalance(code) === 0 && indent.endsWith(IND)) {
      next = indent.slice(0, indent.length - IND.length);
    }
    const ins = "\n" + next;
    return { start: s, end: e, insert: ins, selStart: s + ins.length, selEnd: s + ins.length };
  }

  function lineBlock(text, s, e) {
    const bs = lineStartOf(text, s);
    let end = e;
    if (e > s && text[e - 1] === "\n") end = e - 1;
    return { bs, be: lineEndOf(text, end) };
  }

  function indentLines(text, s, e, outdent) {
    const { bs, be } = lineBlock(text, s, e);
    const lines = text.slice(bs, be).split("\n");
    let first = 0, total = 0;
    const multi = lines.length > 1;
    const out = lines.map((ln, idx) => {
      let d = 0, nl = ln;
      if (outdent) {
        const m = /^( {1,4}|\t)/.exec(ln);
        if (m) { nl = ln.slice(m[0].length); d = -m[0].length; }
      } else if (ln.trim() || !multi) { nl = IND + ln; d = IND.length; }
      if (idx === 0) first = d;
      total += d;
      return nl;
    });
    const ns = outdent ? Math.max(bs, s + first) : s + first;
    return { start: bs, end: be, insert: out.join("\n"), selStart: ns, selEnd: e + total };
  }

  function toggleComment(text, s, e) {
    const { bs, be } = lineBlock(text, s, e);
    const lines = text.slice(bs, be).split("\n");
    const live = lines.filter((l) => l.trim());
    if (!live.length) return null;
    const allCommented = live.every((l) => /^\s*#/.test(l));
    let col = Infinity;
    live.forEach((l) => { col = Math.min(col, /^[ \t]*/.exec(l)[0].length); });
    let firstDelta = 0;
    const out = lines.map((l, idx) => {
      if (!l.trim()) return l;
      let nl;
      if (allCommented) nl = l.replace(/^(\s*)# ?/, "$1");
      else nl = l.slice(0, col) + "# " + l.slice(col);
      if (idx === 0) firstDelta = nl.length - l.length;
      return nl;
    });
    const insert = out.join("\n");
    if (s === e) {
      const p = Math.max(bs, s + firstDelta);
      return { start: bs, end: be, insert, selStart: p, selEnd: p };
    }
    return { start: bs, end: be, insert, selStart: bs, selEnd: bs + insert.length };
  }

  function moveLines(text, s, e, dir) {
    const { bs, be } = lineBlock(text, s, e);
    const block = text.slice(bs, be);
    if (dir < 0) {
      if (bs === 0) return null;
      const ps = lineStartOf(text, bs - 1), prevLine = text.slice(ps, bs - 1);
      const shift = prevLine.length + 1;
      return { start: ps, end: be, insert: block + "\n" + prevLine, selStart: s - shift, selEnd: e - shift };
    }
    if (be >= text.length) return null;
    const ne = lineEndOf(text, be + 1), nextLine = text.slice(be + 1, ne);
    const shift = nextLine.length + 1;
    return { start: bs, end: ne, insert: nextLine + "\n" + block, selStart: s + shift, selEnd: e + shift };
  }

  function duplicateLines(text, s, e, dir) {
    const { bs, be } = lineBlock(text, s, e);
    const block = text.slice(bs, be), len = block.length + 1;
    if (dir > 0) return { start: be, end: be, insert: "\n" + block, selStart: s + len, selEnd: e + len };
    return { start: bs, end: bs, insert: block + "\n", selStart: s, selEnd: e };
  }

  const PAIRS = { "(": ")", "[": "]", "{": "}", '"': '"', "'": "'" };
  const CLOSERS = ")]}\"'";

  // -> {skip: pos} | edit | null
  function typeChar(text, s, e, ch) {
    if (s === e && CLOSERS.indexOf(ch) >= 0 && text[s] === ch) return { skip: s + 1 };
    const close = PAIRS[ch];
    if (!close) return null;
    if (s !== e) {
      const ins = ch + text.slice(s, e) + close;
      return { start: s, end: e, insert: ins, selStart: s + 1, selEnd: e + 1 };
    }
    const prevCh = text[s - 1] || "", nextCh = text[s] || "";
    if (ch === '"' || ch === "'") {
      if (/\w/.test(prevCh) || /\w/.test(nextCh) || prevCh === ch) return null;
    } else if (nextCh && !/[\s)\]},;:]/.test(nextCh)) return null;
    return { start: s, end: s, insert: ch + close, selStart: s + 1, selEnd: s + 1 };
  }

  function backspaceEdit(text, s, e) {
    if (s !== e) return null;
    const p = text[s - 1], q = text[s];
    if (p && q && PAIRS[p] === q) return { start: s - 1, end: s + 1, insert: "", selStart: s - 1, selEnd: s - 1 };
    const ls = lineStartOf(text, s), before = text.slice(ls, s);
    if (before.length >= 2 && /^ +$/.test(before)) {
      const n = before.length % IND.length === 0 ? IND.length : before.length % IND.length;
      if (n > 1) return { start: s - n, end: s, insert: "", selStart: s - n, selEnd: s - n };
    }
    return null;
  }

  /* =======================================================================
   * Brackets
   * ===================================================================== */

  // Offsets that sit inside a string or comment (brackets there don't count).
  function inertMask(len, tokens) {
    const m = new Uint8Array(len);
    for (const t of tokens) if (t.t === "string" || t.t === "comment") m.fill(1, t.s, Math.min(t.e, len));
    return m;
  }

  // -> [openOffset, closeOffset] for the bracket touching `pos`, or null.
  function matchBracket(text, tokens, pos) {
    const mask = inertMask(text.length, tokens);
    const open = "([{", close = ")]}";
    for (const at of [pos - 1, pos]) {
      const ch = text[at];
      if (at < 0 || !ch || mask[at]) continue;
      let oi = open.indexOf(ch), ci = close.indexOf(ch);
      if (oi >= 0) {
        let depth = 0;
        for (let i = at; i < text.length; i++) {
          if (mask[i]) continue;
          const d = text[i];
          if (d === ch) depth++; else if (d === close[oi]) { depth--; if (depth === 0) return [at, i]; }
        }
        return null;
      }
      if (ci >= 0) {
        let depth = 0;
        for (let i = at; i >= 0; i--) {
          if (mask[i]) continue;
          const d = text[i];
          if (d === ch) depth++; else if (d === open[ci]) { depth--; if (depth === 0) return [i, at]; }
        }
        return null;
      }
    }
    return null;
  }

  /* =======================================================================
   * Completions
   * ===================================================================== */

  const SNIPPETS = [
    { label: "jtool", detail: "Jarvis tool function", body:
      'def tool_$name(args):\n    args = args or {}\n    value = (args.get("value") or "").strip()\n    if not value:\n        return {"ok": False, "error": "value is required"}\n    $0\n    return {"ok": True, "result": value}\n' },
    { label: "jschema", detail: "TOOL_SCHEMAS entry", body:
      '    {\n        "name": "$name",\n        "description": "What it does, and when the model should use it.",\n        "parameters": {\n            "type": "object",\n            "properties": {\n                "value": {"type": "string", "description": "What this is."},\n            },\n            "required": ["value"],\n        },\n    },\n' },
    { label: "jparam", detail: "schema property", body: '"$name": {"type": "string", "description": "$0"},' },
    { label: "jchecklist", detail: "TEST_CHECKLIST entry", body:
      '"$name": {\n    "does": "$0",\n    "steps": [\n        {"ask": "Try it with <something>.", "expect": "What a pass looks like."},\n        {"run": {"value": "x"}, "expect": "Returns ok: true."},\n    ],\n},\n' },
    { label: "jerr", detail: "error result", body: 'return {"ok": False, "error": "$0"}' },
    { label: "try", detail: "try / except", body: "try:\n    $0\nexcept Exception as exc:\n    return {\"ok\": False, \"error\": str(exc)}\n" },
    { label: "def", detail: "function", body: "def $name($0):\n    pass\n" },
    { label: "for", detail: "for loop", body: "for item in $0:\n    pass\n" },
    { label: "with open", detail: "read a file", body: 'with open($0, "r", encoding="utf-8") as fh:\n    data = fh.read()\n' },
    { label: "ifmain", detail: "if __name__", body: 'if __name__ == "__main__":\n    $0\n' },
  ];

  function wordBefore(text, pos) {
    const slice = text.slice(Math.max(0, pos - 80), pos);
    const m = /[A-Za-z_][A-Za-z0-9_]*$/.exec(slice);
    if (!m) return null;
    const afterDot = slice.slice(0, slice.length - m[0].length).slice(-1) === ".";
    return { word: m[0], start: pos - m[0].length, afterDot };
  }

  function fileWords(text, skipAt, skipWord) {
    const seen = new Set(), out = [];
    const re = /[A-Za-z_][A-Za-z0-9_]{2,}/g;
    let m;
    while ((m = re.exec(text)) && out.length < 400) {
      if (m.index === skipAt && m[0] === skipWord) continue;
      if (seen.has(m[0])) continue;
      seen.add(m[0]); out.push(m[0]);
    }
    return out;
  }

  function completionsFor(text, pos, force) {
    const wb = wordBefore(text, pos);
    if (!wb || (!force && wb.word.length < 2)) return null;
    const w = wb.word.toLowerCase();
    const items = [];
    const rank = (label) => {
      const l = label.toLowerCase();
      if (l.startsWith(w)) return 0;
      if (l.indexOf(w) > 0) return 1;
      return -1;
    };
    const add = (label, kind, insert, detail) => {
      const r = rank(label);
      if (r < 0) return;
      if (label === wb.word && kind !== "snippet") return;
      items.push({ label, kind, insert: insert == null ? label : insert, detail: detail || "", r });
    };
    if (!wb.afterDot) {
      SNIPPETS.forEach((s) => add(s.label, "snippet", s.body, s.detail));
    }
    fileWords(text, wb.start, wb.word).forEach((x) => add(x, "word", x, "in this file"));
    if (!wb.afterDot) {
      KEYWORDS.forEach((k) => add(k, "keyword", k, "keyword"));
      LITERALS.forEach((k) => add(k, "keyword", k, "constant"));
      BUILTINS.forEach((k) => add(k, "builtin", k, "builtin"));
    }
    const order = { snippet: 0, word: 1, keyword: 2, builtin: 3 };
    items.sort((a, b) => a.r - b.r || order[a.kind] - order[b.kind] || a.label.length - b.label.length || (a.label < b.label ? -1 : 1));
    const seen = new Set(), uniq = [];
    for (const it of items) { const k = it.kind + ":" + it.label; if (!seen.has(k)) { seen.add(k); uniq.push(it); } }
    return uniq.length ? { start: wb.start, end: pos, items: uniq.slice(0, 8) } : null;
  }

  // "$name" -> a placeholder word the owner overtypes; "$0" -> where the caret goes.
  function expandSnippet(body, indent) {
    let text = body.replace(/\$name/g, "name");
    const idx = text.indexOf("$0");
    text = text.replace("$0", "");
    const ind = indent || "";
    const lines = text.split("\n").map((l, i) => (i === 0 ? l : (l ? ind + l : l)));
    text = lines.join("\n");
    let caret = idx;
    if (idx >= 0) {
      const head = body.slice(0, idx).replace(/\$name/g, "name");
      const hl = head.split("\n");
      caret = head.length + (hl.length - 1) * ind.length;
    } else caret = text.length;
    return { text, caret };
  }

  /* =======================================================================
   * Outline (what the side panel lists)
   * ===================================================================== */

  function outline(src) {
    const out = [];
    String(src || "").split("\n").forEach((ln, i) => {
      let m;
      if ((m = /^(?:async\s+)?def\s+([A-Za-z_]\w*)/.exec(ln))) out.push({ kind: "def", name: m[1], line: i + 1 });
      else if ((m = /^class\s+([A-Za-z_]\w*)/.exec(ln))) out.push({ kind: "class", name: m[1], line: i + 1 });
      else if ((m = /^([A-Z][A-Z0-9_]{2,})\s*=/.exec(ln))) out.push({ kind: "const", name: m[1], line: i + 1 });
    });
    return out;
  }

  /* =======================================================================
   * Suggestion helpers
   * ===================================================================== */

  function hashText(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }

  // Should the editor ask for a suggestion here? Only at the end of a line (text
  // after the caret means the owner is editing, not extending), and only once
  // the file has enough in it to go on. A blank line counts: predicting the next
  // statement is the most useful case.
  function suggestionWanted(text, pos) {
    if (text.trim().length < 20) return false;
    return !text.slice(pos, lineEndOf(text, pos)).trim();
  }

  function nextWordChunk(s) {
    const m = /^\s*(?:\w+|[^\w\s]+)/.exec(s);
    return m ? m[0] : s.slice(0, 1);
  }

  const _pure = {
    IND, tokenize, renderHTML, posToLineCol, visualCol, lineStartOf, lineEndOf, applyEdit,
    smartEnter, indentLines, toggleComment, moveLines, duplicateLines, typeChar, backspaceEdit,
    matchBracket, completionsFor, expandSnippet, outline, hashText, suggestionWanted, nextWordChunk,
    SNIPPETS,
  };

  /* =======================================================================
   * The component
   * ===================================================================== */

  const SUGGEST_IDLE_MS = 1000;
  const SUGGEST_SESSION_CAP = 40;

  function h(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs) for (const k of Object.keys(attrs)) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.slice(0, 2) === "on" && typeof v === "function") n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    (kids || []).forEach((c) => { if (c != null) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }

  /*
   * opts: { value, placeholder, label,
   *         onChange(value), onSave(), onValidate(),
   *         suggest({source, cursor, signal}) -> Promise<string>   (optional),
   *         suggestEnabled (bool), onSuggestToggle(bool) }
   */
  function create(opts) {
    opts = opts || {};
    const st = {
      value: String(opts.value || ""), tokens: [], marks: [], errorLine: 0, lines: 0,
      cw: 0, lh: 0, padL: 14, padT: 12,
      ghost: "", ghostAt: -1, pop: null, popIndex: 0,
      suggestOn: opts.suggestEnabled !== false, suggestState: "idle", suggestMsg: "",
      timer: 0, ctrl: null, requests: 0, cache: new Map(), composing: false, renderQueued: false, curLine: 0,
    };

    const ta = h("textarea", { class: "ce__ta", spellcheck: "false", autocapitalize: "off", autocomplete: "off",
      autocorrect: "off", wrap: "off", "aria-label": opts.label || "Code", placeholder: opts.placeholder || "" });
    ta.value = st.value;
    const code = h("code");
    const pre = h("pre", { class: "ce__hl", "aria-hidden": "true" }, [code]);
    const bandCur = h("div", { class: "ce__band ce__band--cur" });
    const bandErr = h("div", { class: "ce__band ce__band--err" });
    const ghostFirst = h("span", { class: "ce__ghost ce__ghost--first" });
    const ghostRest = h("div", { class: "ce__ghost ce__ghost--rest" });
    const inner = h("div", { class: "ce__inner" }, [bandCur, bandErr, pre, ghostFirst, ghostRest]);
    const layers = h("div", { class: "ce__layers", "aria-hidden": "true" }, [inner]);
    const gutterInner = h("div", { class: "ce__gutter-inner" });
    const gutter = h("div", { class: "ce__gutter", "aria-hidden": "true" }, [gutterInner]);
    const pop = h("div", { class: "ce__pop", role: "listbox", hidden: true });
    const measure = h("span", { class: "ce__measure", "aria-hidden": "true" }, ["MMMMMMMMMMMMMMMMMMMM"]);
    const stage = h("div", { class: "ce__stage" }, [layers, ta, pop, measure]);

    const posEl = h("span", { class: "ce__stat" }, ["Ln 1, Col 1"]);
    const lenEl = h("span", { class: "ce__stat ce__stat--dim" });
    const sugBtn = h("button", { class: "ce__sug", type: "button", onclick: () => setSuggestEnabled(!st.suggestOn) });
    const status = h("div", { class: "ce__status" }, [
      posEl, h("span", { class: "ce__stat ce__stat--dim" }, ["Spaces: 4"]), h("span", { class: "ce__stat ce__stat--dim" }, ["Python"]), lenEl,
      h("span", { class: "ce__spacer" }), sugBtn,
    ]);
    const root = h("div", { class: "ce" }, [h("div", { class: "ce__main" }, [gutter, stage]), status]);

    /* ---- metrics ---- */
    function metrics() {
      const w = measure.getBoundingClientRect().width / 20;
      if (w > 0) st.cw = w;
      const cs = global.getComputedStyle(ta);
      st.lh = parseFloat(cs.lineHeight) || st.lh || 20;
      st.padL = parseFloat(cs.paddingLeft) || 14;
      st.padT = parseFloat(cs.paddingTop) || 12;
    }

    /* ---- painting ---- */
    function paintGutter() {
      const n = st.value.split("\n").length;
      if (n !== st.lines) {
        st.lines = n;
        while (gutterInner.firstChild) gutterInner.removeChild(gutterInner.firstChild);
        const frag = document.createDocumentFragment();
        for (let i = 1; i <= n; i++) frag.appendChild(h("span", null, [String(i)]));
        gutterInner.appendChild(frag);
        gutter.style.minWidth = (Math.max(2, String(n).length) * (st.cw || 8) + 26) + "px";
      }
      Array.prototype.forEach.call(gutterInner.children, (el, i) => {
        const cls = (i + 1 === st.errorLine ? "is-err " : "") + (i === st.curLine ? "is-cur" : "");
        if (el.className !== cls.trim()) el.className = cls.trim();
      });
    }

    function paintBands() {
      if (!st.lh) metrics();
      bandCur.style.top = (st.padT + st.curLine * st.lh) + "px";
      bandCur.style.height = st.lh + "px";
      const e = st.errorLine > 0 && st.errorLine <= st.lines;
      bandErr.style.display = e ? "block" : "none";
      if (e) { bandErr.style.top = (st.padT + (st.errorLine - 1) * st.lh) + "px"; bandErr.style.height = st.lh + "px"; }
    }

    function paintCode() {
      st.tokens = tokenize(st.value);
      code.innerHTML = renderHTML(st.value, st.tokens, st.marks);
    }

    function paintGhost() {
      if (!st.ghost || ta.selectionStart !== ta.selectionEnd || ta.selectionStart !== st.ghostAt) {
        ghostFirst.style.display = "none"; ghostRest.style.display = "none"; return;
      }
      if (!st.lh) metrics();
      const lc = posToLineCol(st.value, st.ghostAt);
      const lineText = st.value.slice(lineStartOf(st.value, st.ghostAt), st.ghostAt);
      const parts = st.ghost.split("\n");
      ghostFirst.textContent = parts[0];
      ghostFirst.style.display = parts[0] ? "block" : "none";
      ghostFirst.style.left = (st.padL + visualCol(lineText, lc.col) * st.cw) + "px";
      ghostFirst.style.top = (st.padT + lc.line * st.lh) + "px";
      if (parts.length > 1) {
        ghostRest.textContent = parts.slice(1).join("\n");
        ghostRest.style.display = "block";
        ghostRest.style.left = "0px";
        ghostRest.style.paddingLeft = st.padL + "px";
        ghostRest.style.top = (st.padT + (lc.line + 1) * st.lh) + "px";
      } else ghostRest.style.display = "none";
    }

    function paintStatus() {
      const s = ta.selectionStart, e = ta.selectionEnd, lc = posToLineCol(st.value, e === s ? s : e);
      posEl.textContent = "Ln " + (lc.line + 1) + ", Col " + (lc.col + 1) + (e !== s ? " (" + (e - s) + " selected)" : "");
      lenEl.textContent = st.value.length.toLocaleString() + " chars";
      let label, title;
      if (!opts.suggest) { sugBtn.style.display = "none"; return; }
      sugBtn.style.display = "";
      if (!st.suggestOn) { label = "✦ Suggestions off"; title = "Jarvis inline suggestions are off. Click to turn them on."; }
      else if (st.suggestState === "thinking") { label = "✦ Thinking…"; title = "Asking Jarvis for a suggestion."; }
      else if (st.suggestState === "ready") { label = "✦ Tab to accept"; title = "Tab accepts · Ctrl+→ one word · Esc dismisses"; }
      else if (st.suggestState === "paused") { label = "✦ Paused"; title = st.suggestMsg || "Paused for this editing session. Click to resume."; }
      else if (st.suggestState === "error") { label = "✦ Unavailable"; title = st.suggestMsg || "No suggestion available."; }
      else { label = "✦ Suggestions on"; title = "Jarvis suggests code after a pause at the end of a line. Sends the code around the caret to your AI provider. Click to turn off."; }
      sugBtn.textContent = label; sugBtn.title = title;
      sugBtn.className = "ce__sug" + (st.suggestOn ? " is-on" : "") + (st.suggestState === "thinking" ? " is-busy" : "") +
        (st.suggestState === "error" || st.suggestState === "paused" ? " is-bad" : "");
    }

    function paintAll() {
      paintCode(); paintGutter(); paintBands(); paintGhost(); paintStatus();
    }

    function queueRender() {
      if (st.renderQueued) return;
      st.renderQueued = true;
      (global.requestAnimationFrame || ((f) => setTimeout(f, 0)))(() => { st.renderQueued = false; paintAll(); });
    }

    function syncScroll() {
      const x = -ta.scrollLeft, y = -ta.scrollTop;
      inner.style.transform = "translate(" + x + "px," + y + "px)";
      gutterInner.style.transform = "translateY(" + y + "px)";
      placePop();
    }

    /* ---- cursor-driven state ---- */
    function cursorMoved(force) {
      const s = ta.selectionStart, e = ta.selectionEnd;
      const lc = posToLineCol(st.value, e);
      const lineChanged = lc.line !== st.curLine;
      st.curLine = lc.line;
      let marks = [];
      if (s === e) { const m = matchBracket(st.value, st.tokens, s); if (m) marks = m; }
      const markChanged = marks.join() !== st.marks.join();
      st.marks = marks;
      if (markChanged) { code.innerHTML = renderHTML(st.value, st.tokens, st.marks); }
      if (lineChanged || force) paintGutter();
      paintBands(); paintGhost(); paintStatus();
      if (st.ghost && (s !== e || s !== st.ghostAt)) clearGhost();
      if (st.pop && (s !== e || s < st.pop.start || s > st.pop.end + 40)) closePop();
    }

    function ensureCaretVisible() {
      if (!st.lh) metrics();
      const lc = posToLineCol(st.value, ta.selectionEnd);
      const top = st.padT + lc.line * st.lh, bottom = top + st.lh;
      if (top < ta.scrollTop + 4) ta.scrollTop = Math.max(0, top - st.lh);
      else if (bottom > ta.scrollTop + ta.clientHeight - 4) ta.scrollTop = bottom - ta.clientHeight + st.lh;
      const lineText = st.value.slice(lineStartOf(st.value, ta.selectionEnd), ta.selectionEnd);
      const x = st.padL + visualCol(lineText, lc.col) * st.cw;
      if (x < ta.scrollLeft + 8) ta.scrollLeft = Math.max(0, x - 40);
      else if (x > ta.scrollLeft + ta.clientWidth - 24) ta.scrollLeft = x - ta.clientWidth + 60;
    }

    /* ---- applying edits ---- */
    function replace(ed) {
      ta.focus();
      // Work out what the text should become BEFORE touching it: execCommand
      // fires 'input' synchronously, which updates st.value, so comparing
      // against st.value afterwards would apply the edit a second time.
      const expected = applyEdit(ta.value, ed);
      ta.setSelectionRange(ed.start, ed.end);
      let done = false;
      try {
        done = ed.insert === "" ? document.execCommand("delete") : document.execCommand("insertText", false, ed.insert);
      } catch (_) { done = false; }
      if (!done || ta.value !== expected) {
        ta.value = expected;                       // no undo step, but never a wrong result
        onInput(null);
      }
      if (ed.selStart != null) ta.setSelectionRange(ed.selStart, ed.selEnd);
      cursorMoved(true);
      ensureCaretVisible();
    }

    /* ---- input ---- */
    function onInput(ev) {
      const before = st.value;
      st.value = ta.value;
      if (ev && ev.inputType === "insertText" && st.ghost && ev.data && st.ghost[0] === ev.data &&
          ta.selectionStart === st.ghostAt + 1) {
        st.ghost = st.ghost.slice(1); st.ghostAt += 1;       // typed along with the suggestion
        if (!st.ghost) clearGhost();
      } else if (st.ghost) clearGhost();
      st.errorLine = 0;
      if (before !== st.value && opts.onChange) opts.onChange(st.value);
      paintCode();
      const lc = posToLineCol(st.value, ta.selectionEnd); st.curLine = lc.line;
      paintGutter(); paintBands(); paintGhost(); paintStatus();
      ensureCaretVisible();
      afterTyping(ev);
    }

    function afterTyping(ev) {
      cancelSuggest();
      const typed = !ev || /^insert|^delete/.test(ev.inputType || "");
      if (!typed || st.composing) return;
      const s = ta.selectionStart;
      if (ev && ev.inputType === "insertText" && /[A-Za-z0-9_]/.test(ev.data || "") && ta.selectionEnd === s) {
        const c = completionsFor(st.value, s, false);
        if (c) { openPop(c); return; }
      }
      if (st.pop) { const c = completionsFor(st.value, s, false); if (c) openPop(c); else closePop(); }
      scheduleSuggest();
    }

    /* ---- completion popup ---- */
    function openPop(c) {
      st.pop = c; st.popIndex = 0; renderPop();
    }
    function closePop() { st.pop = null; pop.hidden = true; }
    function renderPop() {
      if (!st.pop) { pop.hidden = true; return; }
      while (pop.firstChild) pop.removeChild(pop.firstChild);
      st.pop.items.forEach((it, i) => {
        pop.appendChild(h("div", { class: "ce__pop-item" + (i === st.popIndex ? " is-sel" : ""), role: "option",
          "aria-selected": i === st.popIndex ? "true" : "false",
          onmousedown: (e) => { e.preventDefault(); st.popIndex = i; acceptPop(); } }, [
          h("span", { class: "ce__pop-kind ce__pop-kind--" + it.kind }, [it.kind === "snippet" ? "{}" : it.kind === "keyword" ? "k" : it.kind === "builtin" ? "ƒ" : "w"]),
          h("span", { class: "ce__pop-label" }, [it.label]),
          h("span", { class: "ce__pop-detail" }, [it.detail]),
        ]));
      });
      pop.hidden = false; placePop();
    }
    function placePop() {
      if (!st.pop || pop.hidden) return;
      if (!st.lh) metrics();
      const lc = posToLineCol(st.value, ta.selectionEnd);
      const lineText = st.value.slice(lineStartOf(st.value, ta.selectionEnd), ta.selectionEnd);
      let x = st.padL + visualCol(lineText, lc.col) * st.cw - ta.scrollLeft;
      let y = st.padT + (lc.line + 1) * st.lh - ta.scrollTop + 2;
      const sw = stage.clientWidth, sh = stage.clientHeight, pw = pop.offsetWidth || 260, ph = pop.offsetHeight || 180;
      if (x + pw > sw - 8) x = Math.max(8, sw - pw - 8);
      if (y + ph > sh - 4) y = Math.max(4, y - st.lh - ph - 4);
      pop.style.left = Math.max(0, x) + "px"; pop.style.top = Math.max(0, y) + "px";
    }
    function acceptPop() {
      const p = st.pop; if (!p) return;
      const it = p.items[st.popIndex]; closePop();
      const ls = lineStartOf(st.value, p.start);
      const indent = /^[ \t]*/.exec(st.value.slice(ls, p.start))[0];
      const ex = it.kind === "snippet" ? expandSnippet(it.insert, indent) : { text: it.insert, caret: it.insert.length };
      replace({ start: p.start, end: ta.selectionEnd, insert: ex.text, selStart: p.start + ex.caret, selEnd: p.start + ex.caret });
    }

    /* ---- ghost suggestions ---- */
    function clearGhost() { st.ghost = ""; st.ghostAt = -1; ghostFirst.style.display = "none"; ghostRest.style.display = "none"; if (st.suggestState === "ready") { st.suggestState = "idle"; paintStatus(); } }
    function cancelSuggest() {
      if (st.timer) { clearTimeout(st.timer); st.timer = 0; }
      if (st.ctrl) { try { st.ctrl.abort(); } catch (_) { /* gone */ } st.ctrl = null; }
      if (st.suggestState === "thinking") { st.suggestState = "idle"; paintStatus(); }
    }
    function setSuggestState(state, msg) { st.suggestState = state; st.suggestMsg = msg || ""; paintStatus(); }

    function scheduleSuggest() {
      if (!opts.suggest || !st.suggestOn || st.suggestState === "paused" || st.pop) return;
      if (ta.selectionStart !== ta.selectionEnd || !suggestionWanted(st.value, ta.selectionStart)) return;
      st.timer = setTimeout(() => { st.timer = 0; requestSuggest(false); }, SUGGEST_IDLE_MS);
    }

    async function requestSuggest(manual) {
      if (!opts.suggest || (!st.suggestOn && !manual)) return;
      const pos = ta.selectionStart, source = st.value;
      if (ta.selectionEnd !== pos) return;
      if (!manual && !suggestionWanted(source, pos)) return;
      const key = hashText(source) + ":" + pos;
      if (st.cache.has(key)) { showGhost(st.cache.get(key), pos); return; }
      if (st.requests >= SUGGEST_SESSION_CAP) {
        setSuggestState("paused", "Paused after " + SUGGEST_SESSION_CAP + " suggestions in this editing session, to save tokens. Click to resume.");
        return;
      }
      cancelSuggest();
      st.requests++;
      const ctrl = typeof AbortController === "function" ? new AbortController() : null;
      st.ctrl = ctrl;
      setSuggestState("thinking");
      let text = "";
      try {
        text = await opts.suggest({ source, cursor: pos, signal: ctrl ? ctrl.signal : undefined });
      } catch (e) {
        if (st.ctrl === ctrl) { st.ctrl = null; setSuggestState(e && e.name === "AbortError" ? "idle" : "error", e && e.message); }
        return;
      }
      if (st.ctrl !== ctrl) return;                      // superseded
      st.ctrl = null;
      if (st.value !== source || ta.selectionStart !== pos || ta.selectionEnd !== pos) { setSuggestState("idle"); return; }
      text = typeof text === "string" ? text : "";
      st.cache.set(key, text);
      if (st.cache.size > 30) st.cache.delete(st.cache.keys().next().value);
      if (!text) { setSuggestState("idle"); return; }
      showGhost(text, pos);
    }

    function showGhost(text, pos) {
      if (!text || ta.selectionStart !== pos) { setSuggestState("idle"); return; }
      st.ghost = text; st.ghostAt = pos;
      setSuggestState("ready"); paintGhost();
    }

    function acceptGhost(wordOnly) {
      if (!st.ghost) return false;
      const chunk = wordOnly ? nextWordChunk(st.ghost) : st.ghost;
      const at = st.ghostAt, rest = st.ghost.slice(chunk.length);
      st.ghost = ""; ghostFirst.style.display = "none"; ghostRest.style.display = "none";
      replace({ start: at, end: at, insert: chunk, selStart: at + chunk.length, selEnd: at + chunk.length });
      if (rest) { st.ghost = rest; st.ghostAt = at + chunk.length; setSuggestState("ready"); paintGhost(); }
      else setSuggestState("idle");
      return true;
    }

    function setSuggestEnabled(on) {
      st.suggestOn = !!on;
      if (!on) { cancelSuggest(); clearGhost(); }
      if (on && st.suggestState === "paused") { st.requests = 0; }
      st.suggestState = "idle"; st.suggestMsg = "";
      paintStatus();
      if (opts.onSuggestToggle) opts.onSuggestToggle(st.suggestOn);
      if (on) scheduleSuggest();
    }

    /* ---- keyboard ---- */
    function onKeyDown(e) {
      if (st.composing || e.isComposing) return;
      const s = ta.selectionStart, en = ta.selectionEnd, mod = e.ctrlKey || e.metaKey;
      const k = e.key;

      if (st.pop) {
        if (k === "ArrowDown" || k === "ArrowUp") {
          e.preventDefault();
          const n = st.pop.items.length;
          st.popIndex = (st.popIndex + (k === "ArrowDown" ? 1 : n - 1)) % n; renderPop(); return;
        }
        if (k === "Tab" || k === "Enter") { e.preventDefault(); acceptPop(); return; }
        if (k === "Escape") { e.preventDefault(); e.stopPropagation(); closePop(); return; }
      }
      if (st.ghost && s === en && s === st.ghostAt) {
        if (k === "Tab" && !e.shiftKey) { e.preventDefault(); acceptGhost(false); return; }
        if (k === "ArrowRight" && mod) { e.preventDefault(); acceptGhost(true); return; }
        if (k === "Escape") { e.preventDefault(); e.stopPropagation(); clearGhost(); return; }
      }

      if (mod && k === " ") { e.preventDefault(); const c = completionsFor(st.value, s, true); if (c) openPop(c); return; }
      if (e.altKey && (k === "\\" || e.code === "Backslash") && !mod) { e.preventDefault(); requestSuggest(true); return; }
      if (mod && !e.altKey && k.toLowerCase() === "s") { e.preventDefault(); if (opts.onSave) opts.onSave(); return; }
      if (mod && k === "Enter") { e.preventDefault(); if (opts.onValidate) opts.onValidate(); return; }
      if (mod && !e.shiftKey && !e.altKey && k === "/") { e.preventDefault(); const ed = toggleComment(st.value, s, en); if (ed) replace(ed); return; }
      if (e.altKey && !mod && (k === "ArrowUp" || k === "ArrowDown")) {
        e.preventDefault();
        const ed = e.shiftKey ? duplicateLines(st.value, s, en, k === "ArrowDown" ? 1 : -1) : moveLines(st.value, s, en, k === "ArrowDown" ? 1 : -1);
        if (ed) replace(ed); return;
      }
      if (k === "Tab" && !mod && !e.altKey) {
        e.preventDefault();
        const multi = st.value.slice(s, en).indexOf("\n") >= 0;
        if (e.shiftKey || multi) replace(indentLines(st.value, s, en, e.shiftKey));
        else {
          const col = s - lineStartOf(st.value, s), n = IND.length - (col % IND.length);
          replace({ start: s, end: en, insert: " ".repeat(n), selStart: s + n, selEnd: s + n });
        }
        return;
      }
      if (k === "Enter" && !mod && !e.shiftKey && !e.altKey) { e.preventDefault(); replace(smartEnter(st.value, s, en)); return; }
      if (k === "Backspace" && !mod && !e.altKey) { const ed = backspaceEdit(st.value, s, en); if (ed) { e.preventDefault(); replace(ed); } return; }
      if (k.length === 1 && !mod && !e.altKey) {
        const r = typeChar(st.value, s, en, k);
        if (r && r.skip != null) { e.preventDefault(); ta.setSelectionRange(r.skip, r.skip); cursorMoved(); return; }
        if (r) { e.preventDefault(); replace(r); return; }
      }
    }

    /* ---- wiring ---- */
    ta.addEventListener("input", onInput);
    ta.addEventListener("keydown", onKeyDown);
    ta.addEventListener("scroll", syncScroll);
    ta.addEventListener("compositionstart", () => { st.composing = true; });
    ta.addEventListener("compositionend", () => { st.composing = false; onInput(null); });
    ["keyup", "mouseup", "focus"].forEach((ev) => ta.addEventListener(ev, () => cursorMoved()));
    ta.addEventListener("blur", () => { closePop(); });
    ta.addEventListener("click", () => cursorMoved());
    document.addEventListener("selectionchange", onSelectionChange);
    function onSelectionChange() { if (document.activeElement === ta) cursorMoved(); }

    let ro = null;
    if (typeof global.ResizeObserver === "function") {
      ro = new global.ResizeObserver(() => { metrics(); paintBands(); paintGhost(); placePop(); });
      ro.observe(stage);
    }
    if (global.document && global.document.fonts && global.document.fonts.ready) {
      global.document.fonts.ready.then(() => { metrics(); paintAll(); });
    }

    st.curLine = 0;
    paintAll();

    function dispose() {
      cancelSuggest();
      document.removeEventListener("selectionchange", onSelectionChange);
      if (ro) ro.disconnect();
    }

    return {
      el: root,
      textarea: ta,
      getValue: () => st.value,
      setValue(text, o) {
        st.value = String(text == null ? "" : text); ta.value = st.value;
        cancelSuggest(); clearGhost(); closePop(); st.errorLine = 0;
        ta.setSelectionRange(0, 0); ta.scrollTop = 0; ta.scrollLeft = 0;
        st.lines = 0; paintAll(); syncScroll();
        if (!(o && o.silent) && opts.onChange) opts.onChange(st.value);
      },
      focus() { ta.focus(); },
      remeasure() { metrics(); st.lines = 0; paintAll(); },
      setErrorLine(n) { st.errorLine = n > 0 ? n : 0; paintGutter(); paintBands(); },
      revealLine(n) {
        if (!n) return;
        if (!st.lh) metrics();
        const top = st.padT + (n - 1) * st.lh;
        if (top < ta.scrollTop + st.lh || top + 2 * st.lh > ta.scrollTop + ta.clientHeight) ta.scrollTop = Math.max(0, top - st.lh * 3);
        syncScroll();
      },
      gotoLine(n) {
        const lines = st.value.split("\n"); n = Math.max(1, Math.min(n, lines.length));
        let off = 0; for (let i = 0; i < n - 1; i++) off += lines[i].length + 1;
        ta.focus(); ta.setSelectionRange(off, off); this.revealLine(n); cursorMoved(true);
      },
      insertAtCaret(text) {
        ta.focus();
        const s = ta.selectionStart, en = ta.selectionEnd;
        replace({ start: s, end: en, insert: text, selStart: s + text.length, selEnd: s + text.length });
      },
      insertSnippet(label) {
        const sn = SNIPPETS.find((x) => x.label === label); if (!sn) return;
        const s = ta.selectionStart, ls = lineStartOf(st.value, s);
        const indent = /^[ \t]*/.exec(st.value.slice(ls, s))[0];
        const ex = expandSnippet(sn.body, indent);
        const lead = st.value.slice(ls, s).trim() ? "\n" + indent : "";
        replace({ start: s, end: ta.selectionEnd, insert: lead + ex.text, selStart: s + lead.length + ex.caret, selEnd: s + lead.length + ex.caret });
      },
      setSuggestEnabled,
      isSuggestEnabled: () => st.suggestOn,
      suggestNow() { return requestSuggest(true); },
      cursor: () => posToLineCol(st.value, ta.selectionEnd),
      _state: st,
      dispose,
    };
  }

  global.JarvisCodeEditor = { create, _pure };
})(typeof window !== "undefined" ? window : globalThis);
