// Verifies web/public/app.js's math-extraction pipeline (extractMath /
// restoreMath / renderMarkdown) against the REAL `marked` package, since
// this is what actually catches the two bugs that motivated writing
// extraction in the first place:
//
//   1. marked.parse() silently strips the backslash from \( and \] —
//      CommonMark's backslash-escape rule treats "\(" as an escaped
//      literal "(" — so \(...\)/\[...\] delimiters would never reach
//      KaTeX's auto-render intact without being extracted first.
//   2. A bare "*" or "_" inside $...$ (e.g. "$2*x+3*y$", ordinary,
//      unremarkable LaTeX) is ordinary GFM emphasis syntax to marked,
//      which mangles it into "$2<em>x + 3</em>y$" if not extracted first.
//
// This repo has no JS test framework or runner (every other test in
// tests/ is Python, run directly with `python3 tests/test_X.py`) — this
// script follows the same plain-assert, run-it-directly convention
// instead of introducing new test tooling for one file.
//
// Requires the `marked` package (not vendored — it's loaded via CDN in
// index.html at runtime, same as in production):
//   npm install marked@15
//   node tests/verify_math_rendering.js
//
// What this does NOT verify (see KNOWN-ISSUES-AND-GAPS.md's "What has NOT
// been tested" for the same caveat applied to the rest of the web UI):
// actual KaTeX rendering, actual DOMPurify sanitization, or anything in a
// real browser — this sandbox has no network access to fetch KaTeX or
// DOMPurify from their CDNs, and this repo has no local browser-test
// harness (Playwright, jsdom, etc.) set up. DOMPurify's interaction with
// the Private Use Area placeholder characters is reasoned through (a
// sanitizer that operates on HTML structure/attributes has no reason to
// touch arbitrary Unicode text content) but not empirically run.

const fs = require("fs");
const path = require("path");
const { marked } = require("marked");

const APP_JS = path.join(__dirname, "..", "web", "public", "app.js");
const src = fs.readFileSync(APP_JS, "utf8");

// Pull just the math-extraction functions out of app.js by source range,
// so this test runs against the actual shipped code rather than a copy
// that could drift out of sync with it.
const startMarker = "const MATH_PLACEHOLDER_OPEN";
const endMarker = "function renderMathIn";
const start = src.indexOf(startMarker);
const end = src.indexOf(endMarker);
if (start === -1 || end === -1) {
  console.error("Could not find extractMath/restoreMath in app.js — did the surrounding code move?");
  process.exit(1);
}
// eslint-disable-next-line no-eval
eval(src.slice(start, end));

function renderMarkdown(text) {
  const { text: withPlaceholders, stash } = extractMath(text);
  const html = marked.parse(withPlaceholders, { breaks: true, gfm: true });
  return stash.length ? restoreMath(html, stash) : html;
}

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) {
    pass++;
    console.log("ok       " + name);
  } else {
    fail++;
    console.log("FAILED   " + name + (detail ? ": " + detail : ""));
  }
}

// --- The two bugs this whole thing exists to fix -----------------------

let out = renderMarkdown("Inline: \\(x_B = 1\\) done.");
check("\\(...\\) survives marked with its backslashes intact",
      out.includes("\\(x_B = 1\\)"), out);

out = renderMarkdown("Block:\n\n\\[\\vec{OB} = \\vec{OC} + \\vec{OI}\\]\n\ndone");
check("\\[...\\] survives marked with its backslashes intact",
      out.includes("\\[\\vec{OB} = \\vec{OC} + \\vec{OI}\\]"), out);

out = renderMarkdown("$2*x + 3*y$ is a plane");
check("bare * inside $...$ is not turned into <em> by GFM",
      out.includes("$2*x + 3*y$") && !out.includes("<em>"), out);

out = renderMarkdown("$x_B = 1$ and $y_P$ are coordinates");
check("underscores inside $...$ survive untouched",
      out.includes("$x_B = 1$") && out.includes("$y_P$"), out);

// --- Things that must keep working alongside math -----------------------

out = renderMarkdown("Block:\n\n$$\\vec{OB} = \\vec{OC} + \\vec{OI}$$\n\ndone");
check("$$...$$ block math survives intact",
      out.includes("$$\\vec{OB} = \\vec{OC} + \\vec{OI}$$"), out);

out = renderMarkdown("**Bottom line:** $x_B = 1$ always.");
check("bold markdown outside math still renders",
      out.includes("<strong>Bottom line:</strong>") && out.includes("$x_B = 1$"), out);

out = renderMarkdown("It costs $5 today.");
check("a single stray dollar sign (no closing pair) is left as plain text",
      out.includes("$5 today"), out);

out = renderMarkdown("Run `echo $HOME` in your shell.");
check("a dollar sign inside inline code is untouched by extraction",
      out.includes("<code>echo $HOME</code>"), out);

out = renderMarkdown("$a < b$ and $c > d$");
check("angle brackets inside restored math are HTML-escaped, not parsed as tags",
      out.includes("&lt; b") && out.includes("&gt; d") && !/<b>|<d>/.test(out), out);

// --- I-B1 regression cases: math extraction must not eat code ----------
// (extractMath must be segment-aware — see the I-B1 fix in extractMath)

// Case A (control) — a fence containing a dollar-pair-looking shell line.
// Already worked before the fix via exact-byte restore; must still work
// now that the whole fence is a code segment untouched by extraction.
out = renderMarkdown('```bash\necho "$HOME and $PATH"\n```');
check("A: dollar signs inside a fenced block survive untouched",
      out.includes('echo &quot;$HOME and $PATH&quot;') || out.includes('echo "$HOME and $PATH"'), out);
check("A: the fence still closes (single <pre>, not swallowed)",
      (out.match(/<pre>/g) || []).length === 1, out);

// Case B — "$$" inside a fence, prose after it, then an inline `$$`. Used
// to swallow the closing fence, the prose, and the inline code together.
out = renderMarkdown('```bash\necho $$\n```\n\nProse in between.\n\nInline: `$$`.');
check("B: the fence closes on its own (doesn't swallow the prose after it)",
      (out.match(/<pre>/g) || []).length === 1, out);
check("B: the prose between the fence and the inline code still renders as its own paragraph",
      out.includes("Prose in between."), out);
check("B: the later inline `$$` stays a code span, not consumed as math",
      /<code>\$\$<\/code>/.test(out), out);

// Case C — two separate inline code spans, each containing a dollar sign.
// Used to merge into one code span reading across both, with literal
// backticks left in the middle.
out = renderMarkdown("Use `echo $HOME` then `echo $PATH` to compare.");
check("C: two separate <code> spans, not one merged span",
      (out.match(/<code>/g) || []).length === 2, out);
check("C: first code span content is exactly 'echo $HOME'",
      out.includes("<code>echo $HOME</code>"), out);
check("C: second code span content is exactly 'echo $PATH'",
      out.includes("<code>echo $PATH</code>"), out);

// Case D — two js fences, the first containing "/\\[(.*)/", the second
// containing "/(.*)\\]/". The mismatched-but-code-only "\[...\]"-shaped
// text used to be read as a math block spanning both fences, merging them.
out = renderMarkdown('```js\nconst re = /\\[(.*)/;\n```\n\nText between.\n\n```js\nconst re2 = /(.*)\\]/;\n```');
check("D: two separate <pre> blocks, not merged into one",
      (out.match(/<pre>/g) || []).length === 2, out);
check("D: the text between the two fences renders as its own paragraph",
      out.includes("Text between."), out);

// ~~~ fences work the same as ``` fences.
out = renderMarkdown('~~~\n$$ this looks like math but is code $$\n~~~');
check("tilde fences are treated as fences too",
      (out.match(/<pre>/g) || []).length === 1 && out.includes("this looks like math but is code"), out);

// A longer closing fence (more backticks than the opener) still closes it.
out = renderMarkdown('```\n$$code$$\n````');
check("a longer closing fence than the opener still closes the block",
      (out.match(/<pre>/g) || []).length === 1, out);

// An indented fence (<=3 spaces) is still recognized as a fence.
out = renderMarkdown('  ```\n  $$code$$\n  ```');
check("an indented (<=3 space) fence is still recognized",
      (out.match(/<pre>/g) || []).length === 1, out);

// An unclosed fence runs to the end of the text rather than leaking math
// extraction into it (and is what streaming needs once §8 lands).
out = renderMarkdown('Some text.\n\n```\n$$unclosed $$ fence content');
check("an unclosed fence is still treated as one code segment to the end",
      out.includes("$$unclosed $$ fence content"), out);

// Math immediately adjacent to inline code keeps both: the code span isn't
// treated as math, and the math on either side of it still renders.
out = renderMarkdown("$x$ and `code` and $y$");
check("math adjacent to inline code keeps both math spans and the code",
      out.includes("$x$") && out.includes("$y$") && out.includes("<code>code</code>"), out);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
