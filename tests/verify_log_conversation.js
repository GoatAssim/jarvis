// Verifies web/public/log-conversation.js — the Logs "Conversation" view (L.2).
//
// Covers the PURE half: build() turning flat log entries into turns/events
// (turn splitting, tool call/result pairing, requested-but-never-ran tool
// calls, provider attempt vs switch rows, side requests, error responses,
// synthetic "user" messages that must not become the user bubble, end-of-ask
// markers, malformed entries) plus the provider-shape parsers and the
// filter/focus helpers. It also replays the real fixtures in tests/fixtures/
// through build() to make sure real logs never throw and never lose a
// tool_call / tool_result line.
//
// The DOM half (render) is checked by hand — see the L.2 checklist in the
// master plan. Same plain-assert, run-it-directly convention as
// verify_daemons_panel.js: the module is loaded with Node's vm into a bare
// `window`; nothing under test touches the DOM.
//
//   node tests/verify_log_conversation.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const MODULE_JS = path.join(__dirname, "..", "web", "public", "log-conversation.js");
const win = {};
const ctx = vm.createContext({ window: win, console });
vm.runInContext(fs.readFileSync(MODULE_JS, "utf8"), ctx);
const JLC = win.JarvisLogConversation;
assert.ok(JLC && typeof JLC.build === "function" && JLC._pure, "window.JarvisLogConversation did not load");
const P = JLC._pure;

let n = 0;
function ok(name, cond) {
  n += 1;
  if (!cond) throw new Error(`FAILED: ${name}`);
  console.log(`ok       ${name}`);
}

// --- entry builders -------------------------------------------------------------
let clock = Date.parse("2026-10-09T08:00:00.000Z");
function tick(ms) { clock += ms; return new Date(clock).toISOString().replace("Z", "+00:00"); }
function entry(direction, data, extra) {
  return Object.assign({ ts: tick(10), direction, provider: "gemini (key 1/2)", round: null, data }, extra || {});
}
const GEM_URL = "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent";
function gemReq(userText, extraContents) {
  const contents = [{ role: "user", parts: [{ text: userText }] }].concat(extraContents || []);
  return entry("request", { url: GEM_URL, payload: { contents } });
}
function gemResp(parts) {
  return entry("response", { status: 200, body: { candidates: [{ content: { parts }, finishReason: "STOP" }] } });
}
const usage = (round, i, o) => entry("usage", { round, input_tokens: i, output_tokens: o });
const capacity = (label) => entry("info", { capacity_mode: "full", capacity_label: "100% Capacity" }, { provider: label });
const toolCall = (name, args) => entry("tool_call", { name, arguments: args, input_tokens: 12 });
const toolResult = (name, result) => entry("tool_result", { name, result, output_tokens: 34 });

// --- parsers ---------------------------------------------------------------------
ok("extractUserText: gemini last user text", P.extractUserText({ contents: [{ role: "user", parts: [{ text: "hello" }] }] }) === "hello");
ok("extractUserText: skips gemini functionResponse-only user turns", P.extractUserText({
  contents: [
    { role: "user", parts: [{ text: "do it" }] },
    { role: "model", parts: [{ functionCall: { name: "a", args: {} } }] },
    { role: "user", parts: [{ functionResponse: { name: "a", response: {} } }] },
  ] }) === "do it");
ok("extractUserText: openai messages", P.extractUserText({ messages: [{ role: "system", content: "s" }, { role: "user", content: "hi there" }] }) === "hi there");
ok("extractUserText: anthropic block content, ignores tool_result blocks", P.extractUserText({
  messages: [
    { role: "user", content: [{ type: "text", text: "run it" }] },
    { role: "assistant", content: [{ type: "tool_use", name: "t", input: {} }] },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "1", content: "x" }] },
  ] }) === "run it");
ok("extractUserText: 'Result of x:' history lines are not the person", P.extractUserText({
  messages: [{ role: "user", content: "real ask" }, { role: "user", content: "Result of read_file:\nabc" }] }) === "real ask");
ok("extractUserText: tools-withheld notice is stripped from the user text", P.extractUserText({
  messages: [{ role: "user", content: "real ask\n\nYou can't use tools for the rest of this reply. Answer now." }] }) === "real ask");
ok("extractUserText: null payload is null, not a throw", P.extractUserText(null) === null);

const g = P.parseResponseBody({ candidates: [{ content: { parts: [
  { text: "plan", thought: true }, { text: "Hello" }, { functionCall: { name: "read_file", args: { path: "a" } } }] }, finishReason: "STOP" }] });
ok("parseResponseBody gemini: text / thinking / call / finish", g.text === "Hello" && g.thinking === "plan" && g.calls.length === 1 && g.calls[0].name === "read_file" && g.finish === "STOP");
const o = P.parseResponseBody({ choices: [{ message: { content: "", reasoning: "hmm", tool_calls: [{ function: { name: "t", arguments: "{\"a\":1}" } }] }, finish_reason: "tool_calls" }] });
ok("parseResponseBody openai: reasoning kept, arguments JSON parsed", o.text === "" && o.thinking === "hmm" && o.calls[0].args.a === 1);
const a = P.parseResponseBody({ type: "message", content: [{ type: "thinking", thinking: "t" }, { type: "text", text: "yo" }, { type: "tool_use", name: "x", input: { k: 1 } }], stop_reason: "tool_use" });
ok("parseResponseBody anthropic blocks", a.text === "yo" && a.thinking === "t" && a.calls[0].name === "x" && a.finish === "tool_use");
const ol = P.parseResponseBody({ message: { content: "ok", thinking: "t2", tool_calls: [{ function: { name: "z", arguments: { q: 2 } } }] }, done_reason: "stop" });
ok("parseResponseBody ollama native", ol.text === "ok" && ol.thinking === "t2" && ol.calls[0].args.q === 2);
ok("parseResponseBody: junk body is empty, not a throw", P.parseResponseBody("nope").text === "" && P.parseResponseBody(null).calls.length === 0);

// --- small formatters / classifiers ------------------------------------------------
ok("fmtDuration ms / s / min", P.fmtDuration(120) === "120 ms" && P.fmtDuration(2300) === "2.30 s" && P.fmtDuration(64000) === "1 m 04 s");
ok("fmtDuration: negative or missing is empty", P.fmtDuration(-1) === "" && P.fmtDuration(null) === "");
ok("summarizeArgs: key=value, truncates, handles empty", P.summarizeArgs({ path: "main.py", n: 3 }) === 'path="main.py", n=3'
  && P.summarizeArgs({}) === "(no arguments)" && P.summarizeArgs(null) === "(no arguments)" && P.summarizeArgs({ a: "x".repeat(300) }, 50).length <= 50);
ok("classifyResult: error key / ok:false => error", P.classifyResult({ error: "boom" }).status === "error" && P.classifyResult({ ok: false }).status === "error");
ok("classifyResult: cancelled / denied", P.classifyResult({ cancelled: true }).status === "cancelled" && P.classifyResult({ status: "denied" }).status === "cancelled");
ok("classifyResult: plain result and non-object are ok", P.classifyResult({ stdout: "x" }).status === "ok" && P.classifyResult("text").status === "ok");
ok("hostOf", P.hostOf("https://api.groq.com/openai/v1/chat/completions") === "api.groq.com" && P.hostOf("nonsense") === "");
ok("fmtLocal: bad timestamp falls back to the raw string, never 'Invalid Date'", P.fmtLocal("not-a-date") === "not-a-date" && P.fmtLocal(null) === "?");
ok("fmtLocal: valid timestamp keeps milliseconds", /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$/.test(P.fmtLocal("2026-10-09T08:00:00.123+00:00")));

// --- build(): one ask with a tool round -----------------------------------------------
{
  const log = [
    capacity("gemini (key 1/2)"),
    gemReq("what does cli.py do?"),
    gemResp([{ text: "Let me look." }, { functionCall: { name: "read_file", args: { path: "cli.py" } } }]),
    usage(0, 100, 20),
    toolCall("read_file", { path: "cli.py" }),
    toolResult("read_file", { content: "print(1)" }),
    gemReq("what does cli.py do?", [{ role: "model", parts: [{ functionCall: { name: "read_file", args: {} } }] }, { role: "user", parts: [{ functionResponse: { name: "read_file", response: {} } }] }]),
    gemResp([{ text: "It prints 1." }]),
    usage(1, 150, 10),
    entry("info", { ask_usage: { input_tokens: 250, output_tokens: 30 } }, { provider: "ask total" }),
  ];
  const t = JLC.build(log);
  ok("one ask => one turn", t.turns.length === 1);
  const turn = t.turns[0];
  ok("user text captured from the request", turn.user && turn.user.text === "what does cli.py do?");
  ok("first event is the user bubble", turn.events[0].type === "user");
  const types = turn.events.map((e) => e.type).join(",");
  ok("event order: user, attempt, assistant, tool, assistant", types === "user,attempt,assistant,tool,assistant");
  const tool = turn.events.find((e) => e.type === "tool");
  ok("tool call and result paired into ONE event", tool.callEntry && tool.resultEntry && tool.status === "ok" && !tool.requestedOnly);
  ok("tool args come from the logged tool_call", tool.args.path === "cli.py");
  ok("tool round falls back to the model call's usage round", tool.round === 0);
  ok("tool duration is derived from the preceding log line, not call->result", tool.durationMs !== null && tool.durationMs >= 0);
  const [mid, last] = turn.events.filter((e) => e.type === "assistant");
  ok("narration before a tool call is interim, final reply is final", mid.final === false && last.final === true);
  ok("tokens summed from usage lines", turn.tokensIn === 250 && turn.tokensOut === 30);
  ok("ask_usage info closes the turn and is kept as the total", turn.closed === true && turn.askTotal && turn.askTotal.input_tokens === 250);
  ok("tool/call counters", turn.toolCount === 1 && turn.errorCount === 0);
  ok("assistant event carries its raw request/response/usage entries", mid.entries.length >= 3 && mid.entries.some((e) => e.direction === "request"));
}

// --- build(): turn splitting ----------------------------------------------------------
{
  const t = JLC.build([gemReq("first"), gemResp([{ text: "a" }]), usage(0, 1, 1), gemReq("second"), gemResp([{ text: "b" }]), usage(0, 1, 1)]);
  ok("a changed user text starts a new turn", t.turns.length === 2 && t.turns[1].user.text === "second");
}
{
  const t = JLC.build([
    gemReq("same"), gemResp([{ text: "a" }]), usage(0, 1, 1),
    entry("info", { ask_usage: {} }, { provider: "ask total" }),
    gemReq("same"), gemResp([{ text: "b" }]), usage(0, 1, 1),
  ]);
  ok("the end-of-ask marker splits two identical consecutive messages", t.turns.length === 2);
}
{
  // Older logs have no ask_usage: the capacity line of ask #2 is logged before
  // its request and must land in ask #2, not as a fake 'switch' at the end of #1.
  const t = JLC.build([
    capacity("gemini (key 1/2)"), gemReq("one"), gemResp([{ text: "a" }]), usage(0, 1, 1),
    capacity("gemini (key 1/2)"), gemReq("two"), gemResp([{ text: "b" }]), usage(0, 1, 1),
  ]);
  ok("trailing attempt row moves to the ask it belongs to", t.turns.length === 2
    && t.turns[0].events.every((e) => e.type !== "attempt" || t.turns[0].events.indexOf(e) < 2)
    && t.turns[1].events.some((e) => e.type === "attempt" && e.switched === false));
  ok("...and the user bubble is still first in each turn", t.turns.every((x) => x.events[0].type === "user"));
}
{
  const t = JLC.build([
    gemReq("do it"), gemResp([{ text: "ok" }]), usage(0, 1, 1),
    gemReq("do it", [{ role: "user", parts: [{ text: "Result of x:\nabc" }] }]), gemResp([{ text: "ok2" }]), usage(1, 1, 1),
  ]);
  ok("a synthetic 'Result of' user line does NOT start a new turn", t.turns.length === 1);
}

// --- build(): failover ----------------------------------------------------------------
{
  const t = JLC.build([
    capacity("gemini (key 1/2)"), gemReq("go"),
    entry("response", { status: 429, body: { error: { message: "quota exceeded" } } }),
    capacity("gemini (key 2/2)"),
    gemReq("go"), gemResp([{ text: "done" }]), usage(0, 5, 5),
  ]);
  const kinds = t.turns[0].events.map((e) => e.type + (e.switched ? "*" : ""));
  ok("failover: attempt, error, switched attempt, reply", kinds.join(",") === "user,attempt,error,attempt*,assistant");
  const err = t.turns[0].events.find((e) => e.type === "error");
  ok("an HTTP 429 response becomes an error event with the provider's message", err.httpStatus === 429 && /quota/.test(err.message));
  ok("error counted on the turn", t.turns[0].errorCount === 1);
  const sw = t.turns[0].events.find((e) => e.switched);
  ok("switch row knows which key it left", sw.from === "gemini (key 1/2)" && sw.provider === "gemini (key 2/2)");
}

// --- build(): tool calls that never ran / never returned ------------------------------
{
  const t = JLC.build([
    gemReq("x"), gemResp([{ functionCall: { name: "run_shell", args: { cmd: "ls" } } }]), usage(0, 1, 1),
  ]);
  const tool = t.turns[0].events.find((e) => e.type === "tool");
  ok("a requested call with no tool_call line stays visible as 'no execution logged'", tool && tool.status === "no_run" && tool.requestedOnly);
  ok("...with the call's raw model entries as evidence", tool.entries.length >= 2);
}
{
  const t = JLC.build([
    gemReq("x"), gemResp([{ functionCall: { name: "run_shell", args: {} } }]), usage(0, 1, 1), toolCall("run_shell", { cmd: "ls" }),
  ]);
  ok("a tool_call with no result reads 'no result logged'", t.turns[0].events.find((e) => e.type === "tool").status === "no_result");
}
{
  const t = JLC.build([gemReq("x"), toolResult("mystery", { ok: true })]);
  const tool = t.turns[0].events.find((e) => e.type === "tool");
  ok("an orphan tool_result is shown, flagged as call-missing, not dropped", tool && tool.callMissing === true && tool.resultEntry);
}
{
  const t = JLC.build([
    gemReq("x"),
    gemResp([{ functionCall: { name: "a", args: {} } }, { functionCall: { name: "b", args: {} } }]), usage(0, 1, 1),
    toolCall("a", {}), toolResult("a", { error: "bad" }), toolCall("b", {}), toolResult("b", { fine: 1 }),
  ]);
  const tools = t.turns[0].events.filter((e) => e.type === "tool");
  ok("two calls in one response pair with their own results by name", tools.length === 2 && tools[0].status === "error" && tools[1].status === "ok");
  ok("tool error counted on the turn", t.turns[0].errorCount === 1);
}

// --- build(): side requests -----------------------------------------------------------
{
  const t = JLC.build([
    capacity("gemini (key 1/2)"), gemReq("x"),
    entry("request", { url: "https://api.groq.com/openai/v1/chat/completions", payload: { model: "llama", messages: [{ role: "user", content: "A personal-assistant program is about to run this tool call: rm" }] } }),
    entry("response", { status: 200, body: { choices: [{ message: { content: "SAFE" } }] } }),
    usage(0, 9, 1),
    gemResp([{ text: "hi" }]), usage(0, 5, 5),
  ]);
  const side = t.turns[0].events.find((e) => e.type === "side");
  ok("a request to a different host than the attempt's is a side request", !!side && side.host === "api.groq.com");
  ok("risk-review prompt is recognised from its own text", side.riskReview === true);
  ok("the side response is attached to the side event, not shown as a reply", side.response && t.turns[0].events.filter((e) => e.type === "assistant").length === 1);
}
{
  const t = JLC.build([gemReq("x"), entry("request", { url: GEM_URL, payload: { contents: [{ role: "user", parts: [{ text: "audit" }] }] } }, { provider: "groq (side request during gemini (key 1/2))" })]);
  ok("the (side request during …) provider label also marks a side request", t.turns[0].events.some((e) => e.type === "side"));
}

// --- build(): thinking-only and empty responses ---------------------------------------
{
  const t = JLC.build([gemReq("x"), entry("response", { status: 200, body: { choices: [{ message: { content: "", reasoning: "thinking..." } }] } })]);
  const a = t.turns[0].events.find((e) => e.type === "assistant");
  ok("a response with reasoning but no text keeps the reasoning", a && a.thinking === "thinking..." && a.text === "");
  ok("a thinking-only response is never marked as the final reply", a.final === false || !a.text.trim());
}
{
  const t = JLC.build([gemReq("x"), entry("response", { status: 200, body: { candidates: [{ content: { parts: [] } }] } })]);
  ok("an empty response is shown as such", t.turns[0].events.some((e) => e.type === "empty"));
}

// --- build(): hostile input -----------------------------------------------------------
{
  const t = JLC.build([null, 42, "str", [], {}, { direction: 7 }, { direction: "request", data: null }, { direction: "response" }, { direction: "tool_call" }, { direction: "usage", data: "x" }, entry("tool_result", null)]);
  ok("malformed / partial entries never throw", Array.isArray(t.turns));
  ok("non-object lines are counted, not silently lost", t.stats.malformed >= 3);
}
ok("build(undefined) and build([]) are empty timelines", JLC.build(undefined).turns.length === 0 && JLC.build([]).turns.length === 0);
{
  const t = JLC.build([entry("command_run", { cmdline: "ls", exit_code: 0, lines: [] }), entry("something_new", { a: 1 })]);
  ok("command_run and unknown directions become rows", t.turns[0].events.map((e) => e.type).join(",") === "command,other");
}

// --- filter + focus -------------------------------------------------------------------
{
  const log = [
    capacity("gemini (key 1/2)"), gemReq("find it"),
    gemResp([{ functionCall: { name: "a", args: {} } }]), usage(0, 1, 1),
    toolCall("a", {}, ), toolResult("a", { v: 1 }),
    gemReq("find it"), gemResp([{ text: "found" }]), usage(1, 1, 1),
  ];
  log[4].source = "scheduler";
  const t = JLC.build(log);
  const turn = t.turns[0];
  const tool = turn.events.find((e) => e.type === "tool");
  ok("direction filter matches the event that contains such an entry", P.eventMatches(tool, { direction: "tool_call" }) && !P.eventMatches(turn.events[0], { direction: "tool_call" }));
  ok("source filter matches on the entry's source", P.eventMatches(tool, { source: "scheduler" }) && !P.eventMatches(tool, { source: "discord" }));
  ok("no filter matches everything", P.eventMatches(tool, null) && !P.filterIsActive(null) && !P.filterIsActive({ direction: "", source: "" }));
  const hit = new Set([P.entryKey(log[5])]);
  ok("search-hit filter matches by ts+direction", P.eventMatches(tool, { hits: hit }) && !P.eventMatches(turn.events[turn.events.length - 1], { hits: hit }));

  const plan = P.planVisibility(turn, { direction: "tool_call" }, false, null);
  const idx = turn.events.indexOf(tool);
  ok("a match keeps its neighbours visible", plan.keep[idx] && plan.keep[idx - 1] && plan.keep[idx + 1]);
  ok("the user bubble is always visible", plan.keep[0] === true);
  ok("matched count is reported", plan.matched === 1);
  const all = P.planVisibility(turn, { direction: "tool_call" }, true, null);
  ok("showAll keeps everything", all.keep.every(Boolean));
  const folded = P.planVisibility(turn, { direction: "tool_result" }, false, null);
  ok("far-away events fold when the filter is narrow", folded.keep.some((k) => k === false) || turn.events.length <= 4);
  const forced = P.planVisibility(turn, { direction: "tool_call" }, false, turn.events[turn.events.length - 1]);
  ok("the focused event is never folded", forced.keep[turn.events.length - 1] === true);

  const found = P.findEventForEntry(t, { ts: log[4].ts, direction: "tool_call" });
  ok("focus: a search result (ts + direction) resolves to its logical event", found === tool);
  ok("focus: a request hit resolves to the model-call event", P.findEventForEntry(t, { ts: log[6].ts, direction: "request" }) !== null);
  ok("focus: an unknown entry resolves to nothing", P.findEventForEntry(t, { ts: "nope", direction: "request" }) === null && P.findEventForEntry(t, null) === null);
}

// --- real fixtures ----------------------------------------------------------------------
const FIX = path.join(__dirname, "fixtures");
let fixturesRun = 0;
if (fs.existsSync(FIX)) {
  for (const f of fs.readdirSync(FIX).filter((x) => x.endsWith(".jsonl"))) {
    const rows = fs.readFileSync(path.join(FIX, f), "utf8").split(/\r?\n/).filter(Boolean).map((l) => { try { return JSON.parse(l); } catch (_) { return null; } }).filter(Boolean);
    if (!rows.length) continue;
    fixturesRun += 1;
    let t;
    try { t = JLC.build(rows); } catch (err) { throw new Error(`FAILED: build() threw on fixture ${f}: ${err.message}`); }
    const calls = rows.filter((r) => r.direction === "tool_call").length;
    const results = rows.filter((r) => r.direction === "tool_result").length;
    const evs = t.turns.flatMap((x) => x.events);
    ok(`fixture ${f}: every tool_call line lands in exactly one tool event`, evs.filter((e) => e.type === "tool" && e.callEntry).length === calls);
    ok(`fixture ${f}: every tool_result line is kept`, evs.filter((e) => e.type === "tool" && e.resultEntry).length === results);
    ok(`fixture ${f}: at least one turn found its user message`, t.turns.some((x) => x.user && x.user.text.length > 0));
    ok(`fixture ${f}: the user message is not a tool result or the withheld-tools notice`, t.turns.every((x) => !x.user || (!/^Result of /.test(x.user.text) && !x.user.text.includes("You can't use tools for the rest of this reply"))));
    ok(`fixture ${f}: every event carries at least one raw entry`, evs.every((e) => Array.isArray(e.entries) && e.entries.length > 0));
  }
}
ok("fixtures were found and replayed", fixturesRun >= 1);

console.log(`\n${n} checks passed.`);
