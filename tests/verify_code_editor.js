// Verifies web/public/code-editor.js's pure helpers (JarvisCodeEditor._pure):
// the Python tokenizer, the editing helpers behind Enter / Tab / Backspace /
// auto-close / comment toggle / move+copy line, bracket matching, completion
// ranking, snippet expansion, the outline and the "should we ask for a
// suggestion here" rule (master plan L.33).
//
// Same technique as verify_tool_manager.js: the file is loaded with Node's vm
// into a bare `window`; nothing under test touches the DOM, so no jsdom.
//
//   node tests/verify_code_editor.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "web", "public", "code-editor.js"), "utf8"), { window: win, console });
const P = win.JarvisCodeEditor && win.JarvisCodeEditor._pure;
assert.ok(P, "window.JarvisCodeEditor._pure did not load");

function ok(name, cond) { if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }

// tokens as "type:text" for a source
function toks(src) { return P.tokenize(src).map((t) => `${t.t}:${src.slice(t.s, t.e)}`); }
function has(src, entry) { return toks(src).includes(entry); }

// --- tokenizer ---------------------------------------------------------------
ok("def name is a function definition", has("def tool_hello(args):", "fn-def:tool_hello"));
ok("class name is a class definition", has("class Box:", "cls-def:Box"));
ok("keywords", has("if x and not y:", "kw:if") && has("if x and not y:", "kw:and") && has("if x and not y:", "kw:not"));
ok("True/False/None are literals", has("x = None", "lit:None") && has("x = True", "lit:True"));
ok("a call is styled, a plain variable is not", has("run(x)", "call:run") && !toks("run = 1").some((t) => t.startsWith("call:")));
ok("a builtin call is a builtin, not a call", has("len(x)", "builtin:len") && !has("len(x)", "call:len"));
ok("a method call after a dot is a call", has("obj.fetch(1)", "call:fetch"));
ok("an attribute after a dot is plain", !toks("obj.value").some((t) => t.includes("value")));
ok("self is styled", has("self.x = 1", "self:self"));
ok("CONSTANT names are styled", has("TOOL_GROUP = 'x'", "const:TOOL_GROUP"));
ok("CamelCase is a type", has("x = Path('a')", "call:Path") || has("x = Path", "type:Path"));
ok("a decorator is one token", has("@app.route('/')\ndef f(): pass", "decorator:@app.route"));
ok("@ in the middle of a line is not a decorator", !toks("a = b @ c").some((t) => t.startsWith("decorator")));
ok("comment runs to end of line", has("x = 1  # note: 'quoted'", "comment:# note: 'quoted'"));
ok("a # inside a string is not a comment", !toks('s = "a # b"').some((t) => t.startsWith("comment")) && has('s = "a # b"', 'string:"a # b"'));
ok("single and double quoted strings", has("a = 'x'", "string:'x'") && has('a = "y"', 'string:"y"'));
ok("escaped quote stays inside the string", has('a = "he said \\"hi\\" ok"', 'string:"he said \\"hi\\" ok"'));
ok("triple-quoted string spans lines", has('"""doc\nmore"""\nx = 1', 'string:"""doc\nmore"""'));
ok("a docstring after def is one string", has('def f():\n    """Say hi."""\n', 'string:"""Say hi."""'));
ok("unterminated string stops at the line end", has("a = 'oops\nb = 2", "string:'oops") && has("a = 'oops\nb = 2", "num:2"));
ok("string prefixes belong to the string", has("p = r'C:\\x'", "string:r'C:\\x'") && has("b = b'ab'", "string:b'ab'"));
ok("an identifier that merely ends in r is not a prefix", has("bar = 'x'", "string:'x'") && !toks("bar = 'x'").some((t) => t.startsWith("string:bar")));
ok("f-string interpolation is split out", has("f'hi {name}!'", "interp:{name}") && has("f'hi {name}!'", "string:f'hi ") && has("f'hi {name}!'", "string:!'"));
ok("f-string {{ }} is literal text", !toks("f'{{x}}'").some((t) => t.startsWith("interp")));
ok("numbers", has("a = 0xFF", "num:0xFF") && has("a = 1_000", "num:1_000") && has("a = 3.14e-2", "num:3.14e-2") && has("a = .5", "num:.5"));
ok("a digit inside a name is not a number", !toks("x1 = y2").some((t) => t.startsWith("num")));
ok("operators are styled, punctuation is not", has("a == b", "op:=") && !toks("f(a, b)").some((t) => t === "op:,"));
ok("empty and null input do not throw", P.tokenize("").length === 0 && P.tokenize(null).length === 0);
const big = "x = 1\n".repeat(20000);
const t0 = Date.now(); P.tokenize(big);
ok("a 20,000-line file tokenizes in well under a second", Date.now() - t0 < 1500);

// --- rendering ---------------------------------------------------------------
ok("renderHTML escapes markup and keeps the text", (() => {
  const src = "a = '<b>' & 1";
  const html = P.renderHTML(src, P.tokenize(src), []);
  return html.includes("&lt;b&gt;") && html.includes("&amp;") && !html.includes("<b>");
})());
ok("renderHTML round-trips to the same text", (() => {
  const src = "def f(a):\n    return f'{a}' + \"x\"  # c\n";
  const html = P.renderHTML(src, P.tokenize(src), []);
  const text = html.replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  return text === src + "\n";
})());
ok("renderHTML ends with a newline so a trailing empty line keeps its height", P.renderHTML("a\n", [], []).endsWith("\n\n"));
ok("bracket marks wrap exactly one character each", (() => {
  const src = "f(a)";
  const html = P.renderHTML(src, P.tokenize(src), [1, 3]);
  return (html.match(/ce-t-bm/g) || []).length === 2;
})());
ok("a bracket mark inside a styled token keeps the token class", (() => {
  const src = "len(x)";
  const html = P.renderHTML(src, P.tokenize(src), [0]);
  return html.includes("ce-t-builtin ce-t-bm");
})());

// --- positions ---------------------------------------------------------------
ok("posToLineCol", (() => { const r = P.posToLineCol("ab\ncde\nf", 5); return r.line === 1 && r.col === 2; })());
ok("posToLineCol at the very start and end", P.posToLineCol("ab", 0).line === 0 && P.posToLineCol("ab\n", 3).line === 1);
ok("visualCol expands tabs to the next stop", P.visualCol("\tx", 1) === 4 && P.visualCol("a\tx", 2) === 4 && P.visualCol("abc", 3) === 3);

// --- smartEnter --------------------------------------------------------------
function run(edit, text) { return P.applyEdit(text, edit); }
ok("Enter keeps the indent", run(P.smartEnter("    x = 1", 9, 9), "    x = 1") === "    x = 1\n    ");
ok("Enter after a colon indents one level", run(P.smartEnter("def f():", 8, 8), "def f():") === "def f():\n    ");
ok("Enter after a colon keeps the outer indent too", run(P.smartEnter("    if x:", 9, 9), "    if x:") === "    if x:\n        ");
ok("Enter after an open bracket indents", run(P.smartEnter("x = [", 5, 5), "x = [") === "x = [\n    ");
ok("Enter between a bracket pair opens a block", (() => {
  const e = P.smartEnter("f()", 2, 2);
  return P.applyEdit("f()", e) === "f(\n    \n)" && e.selStart === 7;
})());
ok("Enter after return dedents one level", run(P.smartEnter("        return 1", 16, 16), "        return 1") === "        return 1\n    ");
ok("Enter after return at column 0 stays put", run(P.smartEnter("return 1", 8, 8), "return 1") === "return 1\n");
ok("Enter after return inside an open bracket does not dedent", run(P.smartEnter("    return foo(", 15, 15), "    return foo(") === "    return foo(\n        ");
ok("a trailing comment does not hide the colon", run(P.smartEnter("if x:  # why", 12, 12), "if x:  # why") === "if x:  # why\n    ");
ok("Enter in the middle of a line splits it", run(P.smartEnter("    ab", 5, 5), "    ab") === "    a\n    b");
ok("Enter replaces a selection", run(P.smartEnter("abXYcd", 2, 4), "abXYcd") === "ab\ncd");

// --- indent / outdent ---------------------------------------------------------
ok("Tab on several lines indents each non-empty line", run(P.indentLines("a\n\nb", 0, 4, false), "a\n\nb") === "    a\n\n    b");
ok("Shift+Tab removes up to four spaces", run(P.indentLines("        a\n  b", 0, 13, true), "        a\n  b") === "    a\nb");
ok("Shift+Tab removes a tab", run(P.indentLines("\ta", 1, 1, true), "\ta") === "a");
ok("indenting keeps the selection on the same text", (() => {
  const t = "ab\ncd"; const e = P.indentLines(t, 1, 4, false); const n = P.applyEdit(t, e);
  return n.slice(e.selStart, e.selEnd) === "b\n    c";
})());
ok("a selection ending at the start of a line does not indent that line", run(P.indentLines("a\nb\n", 0, 2, false), "a\nb\n") === "    a\nb\n");

// --- comment toggle -----------------------------------------------------------
ok("comment a line", run(P.toggleComment("x = 1", 0, 0), "x = 1") === "# x = 1");
ok("uncomment a line", run(P.toggleComment("# x = 1", 0, 0), "# x = 1") === "x = 1");
ok("comment keeps the indent", run(P.toggleComment("    x = 1", 4, 4), "    x = 1") === "    # x = 1");
ok("comment a block at the shallowest indent", run(P.toggleComment("if x:\n    y\n", 0, 11), "if x:\n    y\n") === "# if x:\n#     y\n");
ok("uncomment only when every line is commented", run(P.toggleComment("# a\nb", 0, 5), "# a\nb") === "# # a\n# b");
ok("toggling twice is a no-op", (() => { const a = "  a\n  b"; const e1 = P.toggleComment(a, 0, a.length); const b = P.applyEdit(a, e1); return P.applyEdit(b, P.toggleComment(b, 0, b.length)) === a; })());
ok("blank text has nothing to comment", P.toggleComment("\n\n", 0, 2) === null);

// --- move / duplicate lines ----------------------------------------------------
ok("move a line down", run(P.moveLines("a\nb\nc", 0, 0, 1), "a\nb\nc") === "b\na\nc");
ok("move a line up", run(P.moveLines("a\nb\nc", 4, 4, -1), "a\nb\nc") === "a\nc\nb");
ok("the first line cannot move up, the last cannot move down", P.moveLines("a\nb", 0, 0, -1) === null && P.moveLines("a\nb", 3, 3, 1) === null);
ok("the caret follows a moved line", (() => { const e = P.moveLines("a\nb\nc", 0, 0, 1); return e.selStart === 2; })());
ok("copy a line down", run(P.duplicateLines("a\nb", 0, 0, 1), "a\nb") === "a\na\nb");
ok("copy a line up", run(P.duplicateLines("a\nb", 2, 2, -1), "a\nb") === "a\nb\nb");

// --- typing: auto-close ---------------------------------------------------------
ok("( auto-closes", (() => { const e = P.typeChar("x", 1, 1, "("); return P.applyEdit("x", e) === "x()" && e.selStart === 2; })());
ok("typing the closer types over it", (() => { const r = P.typeChar("f()", 2, 2, ")"); return r.skip === 3; })());
ok("quotes auto-close", P.applyEdit("a = ", P.typeChar("a = ", 4, 4, '"')) === 'a = ""');
ok("a quote after a word character is an apostrophe, not a pair", P.typeChar("don", 3, 3, "'") === null);
ok("a quote before a word character does not pair", P.typeChar("x", 0, 0, '"') === null);
ok("the third quote of a triple does not pair", P.typeChar('""', 2, 2, '"') === null);
ok("an opening bracket before text does not pair", P.typeChar("ab", 0, 0, "(") === null);
ok("typing a bracket over a selection wraps it", (() => { const e = P.typeChar("abc", 0, 3, "("); return P.applyEdit("abc", e) === "(abc)" && e.selStart === 1 && e.selEnd === 4; })());
ok("an ordinary character is left alone", P.typeChar("a", 1, 1, "x") === null);

// --- backspace -----------------------------------------------------------------
ok("Backspace between a pair deletes both", P.applyEdit("f()", P.backspaceEdit("f()", 2, 2)) === "f");
ok("Backspace in indentation deletes to the previous stop", P.applyEdit("        x", P.backspaceEdit("        x", 8, 8)) === "    x");
ok("Backspace at an odd indent removes the odd spaces", P.applyEdit("      x", P.backspaceEdit("      x", 6, 6)) === "    x");
ok("Backspace after one space is a normal delete", P.backspaceEdit(" x", 1, 1) === null);
ok("Backspace after text is a normal delete", P.backspaceEdit("ab", 2, 2) === null);
ok("Backspace with a selection is a normal delete", P.backspaceEdit("abc", 0, 2) === null);

// --- bracket matching ----------------------------------------------------------
function match(src, pos) { return P.matchBracket(src, P.tokenize(src), pos); }
ok("matches a pair next to the caret", JSON.stringify(match("f(a)", 2)) === "[1,3]" && JSON.stringify(match("f(a)", 4)) === "[1,3]");
ok("matches nested brackets", JSON.stringify(match("f(g(x))", 2)) === "[1,6]");
ok("ignores brackets inside strings and comments", match("f(')')", 2) !== null && JSON.stringify(match("f(')')", 2)) === "[1,5]" && match("# (", 3) === null);
ok("no match for an unclosed bracket", match("f(a", 2) === null);
ok("no bracket, no match", match("abc", 1) === null);

// --- completions ----------------------------------------------------------------
function labels(text, pos, force) { const c = P.completionsFor(text, pos, force); return c ? c.items.map((i) => i.label) : []; }
ok("a snippet is offered by its trigger", labels("jt", 2).includes("jtool"));
ok("keywords and builtins are offered", labels("ret", 3).includes("return") && labels("pri", 3).includes("print"));
ok("words from the file are offered", labels("hello_world = 1\nhel", 19).includes("hello_world"));
ok("the word being typed is not offered to itself", !labels("zzzzzz = 1\nzzzzzz", 17).includes("zzzzzz"));
ok("one character does not open the popup, Ctrl+Space does", P.completionsFor("r", 1, false) === null && P.completionsFor("r", 1, true) !== null);
ok("after a dot only file words are offered, not keywords or snippets", (() => { const l = labels("self.parse_value = 1\nself.par", 29); return l.includes("parse_value") && !l.includes("pass"); })());
ok("at most eight items, snippets first", (() => { const l = labels("j", 1, true); return l.length <= 8 && l[0].startsWith("j"); })());
ok("nothing matches nonsense", P.completionsFor("qqqzzz", 6, false) === null);
ok("completion range covers the typed word", (() => { const c = P.completionsFor("x = ret", 7, false); return c.start === 4 && c.end === 7; })());

// --- snippets --------------------------------------------------------------------
ok("a snippet expands with the caret at $0", (() => { const x = P.expandSnippet('return {"ok": False, "error": "$0"}', ""); return x.text === 'return {"ok": False, "error": ""}' && x.caret === x.text.indexOf('""') + 1; })());
ok("a snippet re-indents continuation lines", (() => { const x = P.expandSnippet("try:\n    $0\nexcept:\n    pass\n", "    "); return x.text.split("\n")[1] === "        " && x.caret === x.text.indexOf("\n", 0) + 1 + 8; })());
ok("$name becomes a placeholder word", P.expandSnippet("def $name():", "").text === "def name():");
ok("every snippet has a trigger, a detail and a body", P.SNIPPETS.every((s) => s.label && s.detail && s.body));
ok("every snippet body is valid-looking: one $0 at most", P.SNIPPETS.every((s) => (s.body.match(/\$0/g) || []).length <= 1));

// --- outline ---------------------------------------------------------------------
ok("outline lists functions, classes and constants", (() => {
  const o = P.outline('"""doc"""\n\nTOOL_GROUP = "x"\n\ndef tool_a(args):\n    def inner(): pass\n\nclass B:\n    def m(self): pass\nasync def go(): pass\n');
  return o.map((x) => x.kind + ":" + x.name + "@" + x.line).join(",") === "const:TOOL_GROUP@3,def:tool_a@5,class:B@8,def:go@10";
})());
ok("outline of nothing is empty", P.outline("").length === 0 && P.outline(null).length === 0);

// --- suggestions -----------------------------------------------------------------
const code1 = "def tool_a(args):\n    x = 1\n    y = foo(";
const code2 = "def tool_a(args):\n    x = 1\ndef tool_b(args):\n    ";
ok("a suggestion is wanted at the end of a code line", P.suggestionWanted(code1, code1.length) === true);
ok("not wanted mid-line", P.suggestionWanted("def tool_a(args):\n    x = 1 + 2\n", 29) === false);
ok("not wanted for a tiny file", P.suggestionWanted("x =", 3) === false);
ok("wanted on a fresh line after a colon", P.suggestionWanted(code2, code2.length) === true);
ok("wanted on a fresh line after an ordinary statement", P.suggestionWanted("def tool_a(args):\n    x = 1\n    ", 31) === true);
ok("hash is stable and differs", P.hashText("abc") === P.hashText("abc") && P.hashText("abc") !== P.hashText("abd"));
ok("accepting one word takes the next chunk", P.nextWordChunk("foo(bar)") === "foo" && P.nextWordChunk("(bar)") === "(" && P.nextWordChunk(" bar baz") === " bar");

console.log("\nAll code-editor helper checks passed.");
