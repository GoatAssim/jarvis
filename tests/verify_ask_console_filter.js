// Verifies the Ask Jarvis "Console" filter (Part E.5 / K.2.5.3, moved out of
// the Live Feed panel and into the Ask Jarvis header, rev. 2026-10-10).
//
// Same no-framework convention as verify_ask_trace_replay.js: the real
// functions are sliced straight out of web/public/app.js by source range, so
// this runs against the shipped code and not a copy that could drift.
//   node tests/verify_ask_console_filter.js
// No npm packages needed.

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const read = (...p) => fs.readFileSync(path.join(ROOT, ...p), "utf8");
const appSrc = read("web", "public", "app.js");
const html = read("web", "public", "index.html");
const css = read("web", "public", "style.css");

let pass = 0;
let fail = 0;
function check(name, cond, detail) {
  if (cond) { pass += 1; console.log("ok      " + name); }
  else { fail += 1; console.log("FAILED  " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : "")); }
}

// ---- slice the classifier + category list out of app.js -------------------
const start = appSrc.indexOf("const ASK_CONSOLE_CATEGORIES = [");
const end = appSrc.indexOf("function askConsoleCounts() {");
if (start === -1 || end === -1) {
  console.error("Could not find the Ask Console filter block in app.js — did it move?");
  process.exit(1);
}
// eslint-disable-next-line no-new-func
const api = new Function(
  appSrc.slice(start, end) +
  "\nreturn { ASK_CONSOLE_CATEGORIES, ASK_CONSOLE_CHROME, isHideableAskKind, askConsoleKindOf };"
)();
const { ASK_CONSOLE_CATEGORIES, ASK_CONSOLE_CHROME, isHideableAskKind, askConsoleKindOf } = api;
const kindOf = askConsoleKindOf;

// ---- classifier: the exact line shapes the CLI / app.js produce -----------
// CLI stderr (cli.py), after stripAnsi().trim():
check("provider: '↳ asking X…'", kindOf("\u21b3 asking openai gpt-5\u2026", "sys") === "provider");
check("discovery: '$ routed: …'", kindOf('$ routed: playnite (matched "launch" on playnite_launch_game)', "tool") === "discovery");
check("discovery: '[tools] Auto-discovered …'", kindOf("[tools] Auto-discovered ['x'] from x.py (group='g')", "sys") === "discovery");
check("discovery: '[tools] Rejected …' / Warning", kindOf("[tools] Warning: foo.py has no TOOL_KEYWORDS", "sys") === "discovery");
check("tool-call: '$ running command  command=…'", kindOf("$ running command  command=dir", "tool") === "tool-call");
check("tokens: per-tool 'tokens: in= out='", kindOf("tokens: in=120 out=30 total=150", "sys") === "tokens");
check("tokens: per-ask 'tokens: … rounds= tools='", kindOf("tokens: in=1 out=2 total=3  rounds=1 tools=0", "sys") === "tokens");
check("tokens: CLI 'ask total=…'", kindOf("ask total=900 (in=800 out=100) across 1 attempt, 2 rounds", "sys") === "tokens");
check("narration: '» text'", kindOf("\u00bb I'll check that now", "sys") === "narration");
check("error: '✗ label — err'", kindOf("\u2717 openai \u2014 HTTP 429", "fail") === "error");
check("status: '(note: …)'", kindOf("(note: the reply hit its output limit)", "sys") === "status");
// app.js's own lines:
check("tokens: client '$ tokens  in=…'", kindOf("$ tokens  in=1 out=2 total=3  rounds=1 tools=0", "tool") === "tokens");
check("tokens: client '$ ask total=…'", kindOf("$ ask total=9 (in=1 out=8) over 2 rounds, 1 attempt", "tool") === "tokens");
check("tool-call: '$ screenshot …'", kindOf("$ screenshot  C:/x.png", "tool") === "tool-call");
check("tool-call: '$ dev_agent …'", kindOf("$ dev_agent  run:ok", "tool") === "tool-call");
check("tool-call: '$ notify'", kindOf("$ notify  Reminder", "tool") === "tool-call");
check("status: 'done'", kindOf("done", "done") === "status");
check("status: 'exit 2'", kindOf("exit 2", "fail") === "status");
check("status: 'stopped (SIGTERM)'", kindOf("stopped (SIGTERM)", "fail") === "status");
// Replay shapes (askLineFromStoredEntry's output):
check("replay: '$ trying …' is provider", kindOf("$ trying  openai 1/2", "sys") === "provider");
check("replay: '$ tool  name' is tool-call", kindOf("$ tool  run_command", "tool") === "tool-call");
check("replay: '✓ name  result' is tool-result", kindOf("\u2713 run_command  ok", "tool") === "tool-result");
check("replay: plain failing line is error", kindOf("quota exceeded", "fail") === "error");
check("replay: 'answered' is status", kindOf("answered", "done") === "status");
// Chrome is never a category:
check("chrome: '$ jarvis'", kindOf("$ jarvis", "cmd") === ASK_CONSOLE_CHROME);
check("chrome: turn separator", kindOf("\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "sys") === ASK_CONSOLE_CHROME);
check("fallback: unknown line is other", kindOf("something odd", "sys") === "other");
check("fallback: empty text is other", kindOf("", "sys") === "other");
check("every classifier result is a known category or chrome", [
  "\u21b3 asking x", "$ routed: a", "[tools] x", "tokens: in=1", "\u00bb hi", "\u2713 a", "\u2717 a", "done",
  "$ foo", "plain", "$ jarvis",
].every((t) => kindOf(t, "sys") === ASK_CONSOLE_CHROME || isHideableAskKind(kindOf(t, "sys"))));

// ---- categories ------------------------------------------------------------
const ids = ASK_CONSOLE_CATEGORIES.map((c) => c.id);
check("category ids are unique", new Set(ids).size === ids.length, ids);
check("chrome is not hideable", !isHideableAskKind(ASK_CONSOLE_CHROME));
check("each category has a label", ASK_CONSOLE_CATEGORIES.every((c) => typeof c.label === "string" && c.label.length > 0));
check("the three categories the owner asked for exist (tool calls, token cost, [tools])",
  ["tool-call", "tokens", "discovery"].every((k) => ids.includes(k)), ids);

// ---- CSS + markup wiring ---------------------------------------------------
ids.forEach((id) => {
  check(`style.css hides .ask-prompt-line[data-kind="${id}"] under data-ask-hidden`,
    css.includes(`[data-ask-hidden~="${id}"] .ask-prompt-line[data-kind="${id}"]`));
});
check("no CSS rule can hide chrome lines", !css.includes('data-kind="chrome"'));
check("Ask header has the Console picker", html.includes('id="btn-console-filter"') && html.includes('id="console-filter-menu"'));
check("Console picker lives inside the Ask panel header actions",
  html.indexOf('id="console-filter-picker"') > html.indexOf('class="ask-panel__actions"')
  && html.indexOf('id="console-filter-picker"') < html.indexOf('class="ask-body"'));
check("PROMPT head has the hidden-count chip", html.includes('id="ask-console-hidden-chip"'));

// ---- the old Live Feed filter is gone --------------------------------------
[
  'id="console-filter"', 'id="console-filter-kinds"', 'id="console-search"',
  'id="console-search-mode"', 'id="console-filter-preset"', 'id="console-filter-errors-only"',
  'id="console-filter-scope"', 'id="console-filter-tool"', 'id="console-filter-hidden-chip"',
].forEach((frag) => check(`index.html no longer has ${frag}`, !html.includes(frag)));
[
  "consoleHiddenKinds", "applyConsoleFilter", "consoleSearchPredicate", "saveConsoleFilterPrefs",
  "loadConsoleFilterPrefs", "/api/console-filter-prefs", "CONSOLE_KIND_LABELS",
].forEach((frag) => check(`app.js no longer has ${frag}`, !appSrc.includes(frag)));
check("style.css no longer has the Live Feed filter rules",
  !css.includes(".console-filter__row") && !css.includes(".console-line.is-filtered"));

// ---- every trace line renderer tags data-kind ------------------------------
const lineBuilders = (appSrc.match(/ask-prompt-line ask-prompt-line--\$\{/g) || []).length;
const kindTags = (appSrc.match(/\.dataset\.kind = askConsoleKindOf\(/g) || []).length;
check("every ask-prompt-line renderer (live, replay, subagent pane) sets data-kind",
  lineBuilders > 0 && kindTags === lineBuilders, { lineBuilders, kindTags });

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
