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

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
