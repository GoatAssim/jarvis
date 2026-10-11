// Verifies the pure helpers added to web/public/schedules.js (JarvisSchedules._pure) for the
// Finished tab's "Clear finished" / per-job Delete and the approval card's preflight warning,
// plus the wiring that has to stay true. The DOM itself is checked by hand (see the manual list
// that came with the patch).
//
// Same technique as verify_tool_manager.js: the file is loaded with Node's vm into a bare
// `window`; nothing under test touches the DOM, so no jsdom.
//
//   node tests/verify_schedules_leftovers.js
//
// Written, not run, by the author of the patch: the owner runs it.

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PUB = path.join(__dirname, "..", "web", "public");
const SRC = fs.readFileSync(path.join(PUB, "schedules.js"), "utf8");
const win = {};
vm.runInNewContext(SRC, { window: win, console });
const P = win.JarvisSchedules && win.JarvisSchedules._pure;
assert.ok(P, "window.JarvisSchedules._pure did not load");
assert.ok(typeof win.JarvisSchedules.open === "function", "the public API is intact");

let n = 0;
function ok(name, cond) { n += 1; if (!cond) throw new Error(`FAILED: ${name}`); console.log(`ok       ${name}`); }

// ---- isFinished ------------------------------------------------------------------------------
ok("finished = done, cancelled, error, over_budget", ["done", "cancelled", "error", "over_budget"].every((s) => P.isFinished({ status: s })));
ok("active statuses are not finished", ["pending", "paused", "needs_approval"].every((s) => !P.isFinished({ status: s })));
ok("a missing job or status is not finished and does not throw", !P.isFinished(null) && !P.isFinished({}) && !P.isFinished(undefined));

// ---- clearFinishedText -----------------------------------------------------------------------
const COUNTS = { finished: 7, done: 4, failed: 1, stopped_over_budget: 0, cancelled: 2 };
const t = P.clearFinishedText(COUNTS, 7);
ok("label carries the number", t.label === "Clear finished (7)" && t.n === 7);
ok("the body says how many and the split by outcome", /all 7 finished jobs \(4 done, 1 failed, 2 cancelled\)/.test(t.body));
ok("zero-count outcomes are left out of the split", !/over budget/.test(t.body));
ok("the body says it ignores the filters and spares active jobs and logs",
  /whatever the filters/.test(t.body) && /still scheduled are not touched/.test(t.body) && /logs stay/.test(t.body));
const one = P.clearFinishedText({ finished: 1, done: 1 }, 1);
ok("singular wording for one job", one.label === "Clear finished (1)" && /all 1 finished job \(1 done\)/.test(one.body));
const bare = P.clearFinishedText(null, 3);
ok("no counts at all still says the total, with no split", /all 3 finished jobs from the history/.test(bare.body) && !/\(/.test(bare.body.split("\n")[0].replace(/from the history.*/, "")));
ok("a missing total falls back to counts.finished", P.clearFinishedText({ finished: 9 }).n === 9);
ok("junk input never throws", P.clearFinishedText("x", NaN).n === 0 && P.clearFinishedText(undefined, undefined).n === 0);

// ---- deleteJobText ---------------------------------------------------------------------------
const d = P.deleteJobText({ title: "Nightly backup" });
ok("the delete confirmation names the job and says it can't be undone", /Nightly backup/.test(d) && /can\u2019t be undone/.test(d));
ok("...and that only the record goes", /only the job\u2019s record/i.test(d) && /logs stay/.test(d));
ok("an untitled job still reads sensibly", /this job/.test(P.deleteJobText({})) && /this job/.test(P.deleteJobText(null)));

// ---- preflightLines --------------------------------------------------------------------------
const waiting = (pf) => ({ status: "needs_approval", preflight: pf });
ok("a blocked job waiting on approval gives its sentences",
  JSON.stringify(P.preflightLines(waiting({ ok: false, lines: ["It has to read the screen."] }))) === JSON.stringify(["It has to read the screen."]));
ok("an ok result gives nothing", P.preflightLines(waiting({ ok: true, lines: [] })).length === 0);
ok("no preflight key gives nothing (an older CLI)", P.preflightLines({ status: "needs_approval" }).length === 0);
ok("only a job still waiting on approval shows it", P.preflightLines({ status: "pending", preflight: { ok: false, lines: ["x"] } }).length === 0);
ok("blank and non-text lines are dropped, text is trimmed",
  JSON.stringify(P.preflightLines(waiting({ ok: false, lines: ["  a  ", "", null, 5, "   "] }))) === JSON.stringify(["a", "5"]));
ok("lines that are not a list give nothing", P.preflightLines(waiting({ ok: false, lines: "x" })).length === 0);
ok("null / junk jobs give nothing and do not throw", P.preflightLines(null).length === 0 && P.preflightLines({}).length === 0);
ok("the note says approving is still allowed and what happens at run time",
  /You can still approve it/.test(P.PREFLIGHT_NOTE) && /no model will be called/.test(P.PREFLIGHT_NOTE) && /repeating job tries again/.test(P.PREFLIGHT_NOTE));

// ---- wiring that must stay true ---------------------------------------------------------------
ok("Delete posts to the per-job delete route and Clear to /api/scheduled/clear",
  /\/api\/scheduled\/\$\{encodeURIComponent\(job\.id\)\}\/delete/.test(SRC) && /"\/api\/scheduled\/clear"/.test(SRC));
ok("both go through the confirm helper", (SRC.match(/confirmText\(/g) || []).length >= 3); // definition + 2 uses
ok("Delete is only added for finished jobs", /if \(isFinished\(job\)\) buttons\.push\(deleteButton\(job\)\)/.test(SRC));
ok("the Clear bar is only shown on the Finished tab and only with something to clear",
  /dom\.clearBar\.hidden = !\(state\.view === "finished" && n > 0\)/.test(SRC));
ok("the Clear count is the server's (not the capped 200-row list)", /state\.counts\.finished != null \? state\.counts\.finished : state\.finished\.length/.test(SRC));
ok("deleting the open job drops back to the form", /if \(state\.selectedId === job\.id\) newJobMode\(\)/.test(SRC));
ok("the approve confirm still opens for a risky job and is skipped only when nothing at all is flagged",
  /if \(!lines\.length && !warn\.length\) return true;/.test(SRC));
ok("the server accepts the delete action and has the clear route",
  /"edit", "delete"\]\)/.test(fs.readFileSync(path.join(__dirname, "..", "web", "server.js"), "utf8"))
  && /app\.post\("\/api\/scheduled\/clear"/.test(fs.readFileSync(path.join(__dirname, "..", "web", "server.js"), "utf8")));

console.log(`\n${n} passed, 0 failed`);
