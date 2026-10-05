// BUG-5 / BUG-6 (DEV_AGENT_KNOWN_BUGS.md): the cards of an agent turn, and
// the live view versus a reload.
//
//   BUG-5  too many cards stacked around one agent turn
//            - an answered confirmation is ONE line, not a full card
//            - one dev_agent progress card per job
//            - saved "thinking" is hidden unless "Show reasoning trace" is on
//   BUG-6  the live thread and the reloaded thread must read the same
//            - cards come BEFORE the final reply, as they do live
//            - saved thinking obeys the same toggle the live stream obeys
//
// No JS test framework in this repo (see verify_math_rendering.js), so, like
// the other verify_*.js files, this slices the REAL functions out of
// web/public/app.js by source range (the shipped code, not a copy) and adds
// plain source-order checks for the parts that need a browser to run.
//   node tests/verify_thread_extras_collapse.js
// No npm packages needed.

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "public", "app.js"), "utf8");

let pass = 0;
let fail = 0;
function check(name, cond, detail) {
  if (cond) {
    pass += 1;
    console.log("ok      " + name);
  } else {
    fail += 1;
    console.log("FAILED  " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : ""));
  }
}

function slice(startMarker, endMarker) {
  const a = src.indexOf(startMarker);
  const b = src.indexOf(endMarker, a + 1);
  if (a === -1 || b === -1) {
    console.error("Could not find " + JSON.stringify(startMarker) + " .. " + JSON.stringify(endMarker) +
      " in app.js -- did the surrounding code move?");
    process.exit(1);
  }
  return src.slice(a, b);
}

// ---- the two pure functions -------------------------------------------------
// eslint-disable-next-line no-eval
const replayExtrasForBucket = eval("(" + slice("function replayExtrasForBucket(", "// The server now saves each turn's screenshots").trim() + ")");
// eslint-disable-next-line no-eval
const confirmOneLineHint = eval("(" + slice("function confirmOneLineHint(", "// BUG-5: a confirmation that has been answered").trim() + ")");

// ---- replayExtrasForBucket --------------------------------------------------
const dev = (jobId, bucket, extra) => ({ bucket, type: "devAgent", data: Object.assign({ jobId, steps: [] }, extra || {}) });
const conf = (bucket) => ({ bucket, type: "confirm", data: { tool: "dev_agent", resolved: true } });

let out = replayExtrasForBucket([conf(0), dev("j1", 0, { ok: null }), dev("j1", 0, { ok: true })], 0);
check("two cards for one job in one exchange -> one card", out.filter((x) => x.type === "devAgent").length === 1, out);
check("...and it is the LAST one (the finished state)", out.find((x) => x.type === "devAgent").data.ok === true, out);

out = replayExtrasForBucket([dev("j1", 0), dev("j2", 0)], 0);
check("two different jobs -> two cards", out.length === 2, out);

out = replayExtrasForBucket([dev(null, 0), dev(null, 0)], 0);
check("cards with no job id are never merged", out.length === 2, out);

out = replayExtrasForBucket([dev("j1", 0), dev("j1", 1)], 0);
check("only this exchange's extras are returned", out.length === 1 && out[0].bucket === 0, out);
out = replayExtrasForBucket([dev("j1", 0), dev("j1", 1)], 1);
check("the same job id in ANOTHER exchange is a different card", out.length === 1 && out[0].bucket === 1, out);

const mixed = [
  { bucket: 0, type: "confirm", data: { tool: "a", resolved: true } },
  { bucket: 0, type: "thinking", data: { text: "t" } },
  dev("j1", 0),
  { bucket: 0, type: "screenshot", data: { filename: "x.png" } },
];
out = replayExtrasForBucket(mixed, 0);
check("saved order is kept", out.map((x) => x.type).join(",") === "confirm,thinking,devAgent,screenshot", out.map((x) => x.type));
check("an empty or missing list is fine", replayExtrasForBucket([], 0).length === 0 && replayExtrasForBucket(undefined, 0).length === 0);
check("the input list is not modified", mixed.length === 4);

// ---- confirmOneLineHint -----------------------------------------------------
check("hint: the command argument", confirmOneLineHint({ arguments: { command: "node  server.js" } }) === "node server.js");
check("hint: cmd argument", confirmOneLineHint({ arguments: { cmd: "dir" } }) === "dir");
check("hint: falls back to the reviewed command", confirmOneLineHint({ arguments: {}, risk_note: { command_run: "npm install" } }) === "npm install");
check("hint: nothing to say -> empty", confirmOneLineHint({ arguments: { description: "make an app" } }) === "");
check("hint: no data at all does not throw", confirmOneLineHint({}) === "" && confirmOneLineHint(undefined) === "");
const long = confirmOneLineHint({ arguments: { command: "x".repeat(300) } });
check("hint: long commands are cut to 90 chars with an ellipsis", long.length === 90 && long.endsWith("\u2026"), long.length);

// ---- source-order checks (these need a browser to run for real) --------------
const load = slice("function loadConversationIntoThread(", "async function loadConsoleHistoryForConv(");
const iExtras = load.indexOf("renderExtrasForBucket(i);");
const iReply = load.indexOf("addJarvisStaticBubble(ex.jarvis");
check("BUG-6: replay draws an exchange's cards BEFORE its reply",
  iExtras !== -1 && iReply !== -1 && iExtras < iReply, { iExtras, iReply });
check("BUG-6: replay goes through replayExtrasForBucket (one card per job)",
  /replayExtrasForBucket\(extras, bucket\)/.test(load));

const thinkCase = slice('case "thinking":', 'case "interimText"');
check("BUG-5/6: a saved thinking block starts hidden unless the toggle is on",
  /details\.hidden = !thinkState\.show/.test(thinkCase) && /thinking-block--saved/.test(thinkCase));
const sync = slice("function syncStreamThinkingVisibility()", "function commitRoundText(");
check("BUG-5/6: the reasoning toggle also flips saved blocks",
  /thinking-block--saved/.test(sync) && /thinking-block--stream/.test(sync));

const liveConfirm = slice("function addAskConfirmBubble(", "// The detail rows of a confirmation");
check("BUG-5: answering a live prompt swaps it for the one-line summary",
  /msg\.replaceWith\(buildResolvedConfirmLine\(/.test(liveConfirm));
const resolvedFn = slice("function renderResolvedConfirmBubble(", "// ---- \u00a78 live streaming");
check("BUG-5: a reload draws that same one-line summary",
  /buildResolvedConfirmLine\(data\)/.test(resolvedFn));
check("BUG-5: the summary is a <details> (click for the full card)",
  /el\("details", \{ class: "ask-confirm-line" \}/.test(slice("function buildResolvedConfirmLine(", "function renderResolvedConfirmBubble(")));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
