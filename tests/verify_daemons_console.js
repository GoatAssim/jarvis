// Verifies H.1.7 — the Daemons console's line classifier (classifyLine /
// annotateLines / summarizeLines / filterLines in web/public/daemons.js).
//
// Owner report, 2026-09-29: "the error/system/etc console detector is not
// working properly". Each block below is a shape of real console text the
// old rules misfiled; they were reproduced first, then fixed. See the master
// plan's H.1.7 for the list.
//
// Same technique and conventions as tests/verify_daemons_panel.js (Node's vm
// module, a bare `window`, plain asserts, no test framework):
//
//   node tests/verify_daemons_console.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const win = {};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "..", "web", "public", "daemons.js"), "utf8"),
  { window: win, console },
);
const P = win.JarvisDaemons && win.JarvisDaemons._pure;
assert.ok(P, "window.JarvisDaemons._pure did not load");

let n = 0;
function ok(name, cond) {
  n += 1;
  if (!cond) throw new Error(`FAILED: ${name}`);
  console.log(`ok       ${name}`);
}
const cls = (raw) => P.classifyLine(raw).cls;
const classes = (lines) => P.annotateLines(lines).map((i) => i.cls);
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// --- 1. the supervisor's own lines are System; a daemon's own stamp is not ----
ok("supervisor banner is System", cls("[2026-09-29 12:00:00] === starting: python bot.py") === "meta");
ok("stdin echo is System", cls("[2026-09-29 12:00:05] <<< say hi") === "stdin");
ok("a failing exit banner is flagged bad",
  P.classifyLine("[2026-09-29 12:00:06] === exited with code 1 after 5.0s").bad === true);
ok("a clean exit banner is not flagged bad",
  P.classifyLine("[2026-09-29 12:00:06] === exited with code 0 after 5.0s").bad === false);
ok("restart-limit banner is flagged bad",
  P.classifyLine("[2026-09-29 12:00:06] === restart limit reached (5 in 10 minutes) — not restarting again.").bad === true);
ok("REGRESSION: a daemon that stamps its own stdout is Output, not System",
  cls("[2026-09-29 12:00:01] connected to gateway") === "stdout");
ok("REGRESSION: ...and keeps its text intact",
  P.classifyLine("[2026-09-29 12:00:01] connected to gateway").text === "[2026-09-29 12:00:01] connected to gateway");

// --- 2. routine stderr logging is not an error --------------------------------
ok("REGRESSION: logging INFO on stderr is Output", cls("E: INFO:discord.client:logging in") === "stdout");
ok("stderr [INFO] with a timestamp is Output", cls("E: [2026-09-29 12:00:00] [INFO    ] discord.client: hi") === "stdout");
ok("stderr 'ts - INFO - msg' is Output", cls("E: 2026-09-29 12:00:00,123 - INFO - started") === "stdout");
ok("stderr DEBUG is Output", cls("E: DEBUG:asyncio:Using selector: EpollSelector") === "stdout");
ok("stderr time-only 'HH:MM:SS INFO' is Output", cls("E: 12:00:00 INFO ready") === "stdout");
ok("stderr WARNING stays an error-filter line", cls("E: WARNING:root:careful") === "stderr");
ok("stderr with no level stays an error-filter line", cls("E: cannot open file") === "stderr");
ok("prose that merely begins with 'Info' is not read as a level", cls("E: Info about the thing") === "stderr");
ok("a blank stderr line is spacing, not an error", cls("E: ") === "stdout");
ok("the E: marker is stripped from displayed text", P.classifyLine("E: INFO:x:y").text === "INFO:x:y");
ok("origin is remembered even when the line is reclassified", P.classifyLine("E: INFO:x:y").origin === "stderr");

// --- 3. errors are errors wherever they were written --------------------------
ok("REGRESSION: an ERROR-level line on stdout is an error", cls("ERROR: something failed") === "stderr");
ok("a timestamped [ERROR] on stdout is an error", cls("[2026-09-29 12:00:00] [ERROR] db down") === "stderr");
ok("CRITICAL and FATAL on stdout are errors", cls("CRITICAL boom") === "stderr" && cls("FATAL: boom") === "stderr");
ok("lower-case 'error' prose on stdout is NOT an error", cls("error handling is fine now") === "stdout");
ok("an ordinary stdout line is Output", cls("hello from stdout") === "stdout");

// a traceback that arrived on stdout (a `2>&1` wrapper, or a framework that prints it)
const stdoutTrace = [
  "starting up",
  "Traceback (most recent call last):",
  '  File "x.py", line 1, in <module>',
  "    boom()",
  "ValueError: boom",
  "back to normal",
];
ok("REGRESSION: a traceback on stdout is grouped as one crash block",
  eq(classes(stdoutTrace), ["stdout", "crash", "crash", "crash", "crash", "stdout"]));

// the existing stderr traceback behaviour, including exceptions not named *Error
const loginCrash = [
  "E: Traceback (most recent call last):",
  'E:   File "bot.py", line 3, in <module>',
  "E:     client.run(token)",
  "E: discord.errors.LoginFailure: Improper token has been passed.",
  "E: ordinary stderr after the traceback",
];
ok("stderr traceback + non-*Error exception is one crash block; later stderr is not swept in",
  eq(classes(loginCrash), ["crash", "crash", "crash", "crash", "stderr"]));

// --- 4. warnings are not crashes; JS stacks group -------------------------------
ok("REGRESSION: a DeprecationWarning is not drawn as a crash", cls("E: DeprecationWarning: x is deprecated") === "stderr");
ok("a real exception tail still is a crash", cls("E: KeyError: 'id'") === "crash" || cls("E: ValueError: bad") === "crash");
const nodeCrash = [
  "E: Error: connect ECONNREFUSED 127.0.0.1:80",
  "E:     at TCPConnectWrap.afterConnect (node:net:1555:16)",
  "E:     at Object.<anonymous> (/app/x.js:10:11)",
  "E: next unrelated stderr line",
];
ok("REGRESSION: a Node stack groups with its message line",
  eq(classes(nodeCrash), ["crash", "crash", "crash", "stderr"]));
ok("prose like 'at 12:30 pm' is not mistaken for a stack frame", cls("    at 12:30 pm sharp") === "stdout");

// --- 5. streams interleave; a block survives that -----------------------------------
const interleaved = [
  "E: Traceback (most recent call last):",
  "heartbeat ok",                                       // stdout thread wrote mid-traceback
  'E:   File "x.py", line 2, in <module>',
  "E: RuntimeError: nope",
  "E: after",
];
ok("a stdout line between traceback lines does not split the crash block",
  eq(classes(interleaved), ["crash", "stdout", "crash", "crash", "stderr"]));
const bannerBreaks = [
  "E: Traceback (most recent call last):",
  "[2026-09-29 12:00:06] === exited with code 1 after 1.0s",
  "E: stray stderr",
];
ok("a supervisor banner ends any traceback block",
  eq(classes(bannerBreaks), ["crash", "meta", "stderr"]));

// --- 6. counts and filters agree with the classes --------------------------------------
const mixed = [
  "[2026-09-29 12:00:00] === starting: bot",     // System
  "[2026-09-29 12:00:01] up and running",         // Output
  "E: INFO:web:GET /health 200",                  // Output
  "E: WARNING:web:slow request",                  // Errors
  "ERROR: db timeout",                            // Errors
  "[2026-09-29 12:00:09] <<< reload",             // System
];
const items = P.annotateLines(mixed);
const counts = P.summarizeLines(items);
ok("counts: all=6, system=2, output=2, errors=2",
  counts.all === 6 && counts.system === 2 && counts.output === 2 && counts.errors === 2);
ok("Output filter shows the routine stderr INFO line",
  P.filterLines(items, "output", "", false).some((i) => i.text.includes("GET /health")));
ok("Errors filter no longer contains routine INFO noise",
  !P.filterLines(items, "errors", "", false).some((i) => i.text.includes("GET /health")));
ok("Errors filter contains the warning and the stdout ERROR",
  P.filterLines(items, "errors", "", false).length === 2);
ok("System filter is only the supervisor's own lines",
  P.filterLines(items, "system", "", false).length === 2);
ok("line numbers survive filtering", P.filterLines(items, "errors", "", false)[0].n === 4);

console.log(`\n${n}/${n} checks passed`);
