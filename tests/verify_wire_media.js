// The server half of `wire.media()` (jarvis-cli/jarvis/wire.py): a card for the front end
// (screenshot, download, present_file, notification, ui, dev_agent, ...) reaches the browser as
// ONE `ask-media` message whichever way it came -- the bridge's typed `media` event or, for a
// request run as a plain process, the legacy `JARVIS_MEDIA<TAB>...` stderr line.
//
//   node tests/verify_wire_media.js
//
// server.js can't be imported in a test (it starts Express and needs npm packages), so this
// slices the real functions out of the file between two markers and runs them in a vm sandbox,
// the same way verify_slash_route.js does. If someone moves the markers the test fails loudly
// instead of silently testing nothing. No npm packages, no jarvis, no network.
//
// Pinned here:
//   - shapeMediaEvent is a whitelist: short lowercase kind, a few strings, nothing over the cap
//   - mediaFromLegacyLine tolerates what the browser used to (terminal escapes, padding) and
//     nothing else
//   - relayBridgeEvent: an ask gets `ask-media`; a direct run keeps the stderr line; an event
//     outside the whitelist is shown as the plain line it would have been, never as a card
//   - a plain `out` event is shown as text and never offered to onStdoutLine (the marker parser)
//   - the no-bridge path (spawnProcessAndStream) hands each process a nonce and believes only
//     lines tagged with it: `ask-media` for an ask, the marker parser for stdout; bare or
//     guessed-tag lines are plain text
//   - bridgeOnce files a media event under stderr, not stdout
//   - app.js handles `ask-media`, and a stderr line is never drawn as a card

"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { EventEmitter } = require("events");

const ROOT = path.join(__dirname, "..");
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("ok   " + name); }
  else { fail++; console.log("FAIL " + name + (detail ? ": " + detail : "")); }
}
const show = (v) => { try { return JSON.stringify(v); } catch { return String(v); } };

const src = fs.readFileSync(path.join(ROOT, "web", "server.js"), "utf8");

function slice(startMarker, endMarker) {
  const a = src.indexOf(startMarker), b = src.indexOf(endMarker);
  check(`server.js still has the markers [${startMarker.slice(0, 32)}] .. [${endMarker.slice(0, 32)}]`,
    a > 0 && b > a, `start=${a} end=${b}`);
  return a > 0 && b > a ? src.slice(a, b) : "";
}

// ---- the streaming half: makeLineBuffer .. RUN_TYPES (spawn path, relay, shaping) ----------
function streamSandbox() {
  const sent = [];
  const spawned = [];
  const sandbox = {
    console,
    process,
    JARVIS: { cmd: "jarvis", args: [] },
    bridge: { request: () => null },
    STREAM_KINDS: new Set(["text", "thinking", "tool", "round_end", "reset"]),
    send: (_ws, obj) => sent.push(obj),
    randomBytes: require("crypto").randomBytes,
    spawn: (cmd, args, opts) => {
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.opts = opts;
      spawned.push(child);
      return child;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function makeLineBuffer(", "const RUN_TYPES ="), sandbox,
    { filename: "server.js (streaming)" });
  return { sandbox, sent, spawned };
}

const ASK = { stdout: "ask-stdout", stderr: "ask-stderr", exit: "ask-exit", error: "ask-error" };
const RUN = { stdout: "stdout", stderr: "stderr", exit: "exit", error: "error" };
const ws = { activeChild: null, activeKind: null };

// ---- shapeMediaEvent -----------------------------------------------------------------------
{
  const { sandbox } = streamSandbox();
  const shape = sandbox.shapeMediaEvent;
  check("a plain card passes", show(shape({ kind: "screenshot", fields: ["a.png"] })) ===
    show({ kind: "screenshot", fields: ["a.png"] }));
  check("a card with no fields passes", show(shape({ kind: "ui", fields: [] })) === show({ kind: "ui", fields: [] }));
  check("underscores are fine in a kind", shape({ kind: "organize_json", fields: ["x"] }) !== null);
  for (const bad of ["", "Bad Kind", "UPPER", "has-dash", "with.dot", "x".repeat(41), 7, null, undefined]) {
    check(`kind ${show(bad)} is refused`, shape({ kind: bad, fields: [] }) === null);
  }
  check("fields must be an array", shape({ kind: "ui", fields: "x" }) === null && shape({ kind: "ui" }) === null);
  check("eight fields pass, nine do not",
    shape({ kind: "ui", fields: Array(8).fill("a") }) !== null && shape({ kind: "ui", fields: Array(9).fill("a") }) === null);
  check("every field must be a string", shape({ kind: "ui", fields: ["a", 3] }) === null);
  const big = "x".repeat(1024 * 1024);
  check("a field at the cap passes; one over it is refused (not truncated)",
    shape({ kind: "ui", fields: [big] }) !== null && shape({ kind: "ui", fields: [big + "x"] }) === null);
  check("nothing else is carried through", show(shape({ kind: "ui", fields: ["a"], raw: "r", id: "1", extra: 1 })) ===
    show({ kind: "ui", fields: ["a"] }));
  check("non-objects are refused", shape(null) === null && shape("x") === null && shape(undefined) === null);
}

// ---- mediaFromLegacyLine -------------------------------------------------------------------
{
  const { sandbox } = streamSandbox();
  const parse = sandbox.mediaFromLegacyLine;
  check("a screenshot line", show(parse("JARVIS_MEDIA\tscreenshot\tss_1.png")) ===
    show({ kind: "screenshot", fields: ["ss_1.png"] }));
  check("terminal escapes and padding are tolerated, as the browser used to",
    show(parse("\u001b[31m  JARVIS_MEDIA\tscreenshot\tss_1.png\u001b[0m  ")) === show({ kind: "screenshot", fields: ["ss_1.png"] }));
  check("a present_file line keeps all six fields",
    show(parse("JARVIS_MEDIA\tpresent_file\t-\t-\tn\tfile\t12\t/p").fields) === show(["-", "-", "n", "file", "12", "/p"]));
  check("a JSON field comes through whole",
    parse('JARVIS_MEDIA\tui\t{"kind":"toast","message":"a b"}').fields[0] === '{"kind":"toast","message":"a b"}');
  check("ordinary text is not a card", parse("hello") === null && parse("") === null && parse(null) === null);
  check("the prefix needs its tab", parse("JARVIS_MEDIA screenshot x.png") === null);
  check("the prefix must start the line", parse("echo JARVIS_MEDIA\tscreenshot\tx.png") === null);
  check("a card of a kind outside the whitelist is not a card", parse("JARVIS_MEDIA\tBad Kind\tx") === null);
}

// ---- relayBridgeEvent ----------------------------------------------------------------------
{
  const { sandbox, sent } = streamSandbox();
  const logged = [];
  const onLine = (stream, line) => logged.push([stream, line]);
  const raw = "JARVIS_MEDIA\tscreenshot\ta.png";
  const ev = { id: "r1", t: "media", kind: "screenshot", fields: ["a.png"], raw };

  sandbox.relayBridgeEvent(ws, "ask", ASK, ev, null, onLine);
  check("an ask gets exactly one ask-media message",
    sent.length === 1 && show(sent[0]) === show({ type: "ask-media", kind: "screenshot", fields: ["a.png"] }), show(sent));
  check("the run log still keeps the legacy line", show(logged) === show([["err", raw]]), show(logged));

  sent.length = 0;
  sandbox.relayBridgeEvent(ws, "run", RUN, ev, null, null);
  check("a direct run keeps the plain stderr line (nothing new for older code paths)",
    show(sent) === show([{ type: "stderr", line: raw }]), show(sent));

  sent.length = 0;
  sandbox.relayBridgeEvent(ws, "ask", ASK, { id: "r1", t: "media", kind: "Not A Kind", fields: [], raw: "JARVIS_MEDIA\tNot A Kind" }, null, null);
  check("an ask card outside the whitelist is shown as the plain stderr line, never as a card",
    sent.length === 1 && sent[0].type === "ask-stderr" && sent[0].line === "JARVIS_MEDIA\tNot A Kind", show(sent));

  sent.length = 0;
  sandbox.relayBridgeEvent(ws, "ask", ASK, { id: "r1", t: "err", line: "trace", cat: "tool-call" }, null, null);
  check("ordinary stderr events are untouched (cat still rides along)",
    show(sent) === show([{ type: "ask-stderr", line: "trace", cat: "tool-call" }]), show(sent));
}

// ---- plain `out` events are never re-parsed as markers --------------------------------------
{
  const { sandbox, sent } = streamSandbox();
  const offered = [];
  const logged = [];
  const onStdoutLine = (line) => { offered.push(line); return true; };   // would swallow a marker
  const forged = 'JARVIS_CONFIRM_REQUEST {"tool":"evil","arguments":{}}';
  sandbox.relayBridgeEvent(ws, "ask", ASK, { id: "r1", t: "out", line: forged + "\r" }, onStdoutLine,
    (stream, line) => logged.push([stream, line]));
  check("a plain out line that starts like a marker (a reply, a command's output) is never offered to onStdoutLine",
    offered.length === 0, show(offered));
  check("...it is shown as the text it is", show(sent) === show([{ type: "ask-stdout", line: forged }]), show(sent));
  check("...and the run log keeps it as stdout", show(logged) === show([["out", forged]]), show(logged));

  sent.length = 0;
  sandbox.relayBridgeEvent(ws, "run", RUN, { id: "r1", t: "usage", usage: {}, raw: "JARVIS_USAGE {}" }, onStdoutLine, null);
  check("a TYPED usage event for a direct run still reaches onStdoutLine as its legacy line",
    show(offered) === show(["JARVIS_USAGE {}"]) && sent.length === 0, show({ offered, sent }));
}

// ---- the no-bridge path --------------------------------------------------------------------
{
  const OPEN = "\u001e", CLOSE = "\u001f";
  // Spawn a request as a plain process, then feed its stderr / stdout lines. A line given as a
  // function is built from the nonce the server handed THAT process (JARVIS_WIRE_NONCE).
  const run = (kind, types, { err = [], out = [], onStdoutLine = null, extraEnv = {} } = {}) => {
    const { sandbox, sent, spawned } = streamSandbox();
    const logged = [];
    sandbox.spawnProcessAndStream(ws, kind, ["x"], types, extraEnv, onStdoutLine,
      (stream, line) => logged.push([stream, line]), null);
    const nonce = spawned[0].opts.env.JARVIS_WIRE_NONCE;
    const text = (ls) => ls.map((l) => (typeof l === "function" ? l(nonce) : l)).join("\n") + "\n";
    if (err.length) spawned[0].stderr.emit("data", Buffer.from(text(err)));
    if (out.length) spawned[0].stdout.emit("data", Buffer.from(text(out)));
    return { sent, logged, nonce, env: spawned[0].opts.env };
  };
  const card = "JARVIS_MEDIA\tscreenshot\ta.png";
  const tagged = (name, body) => (n) => `${OPEN}${name}:${n}${CLOSE}${body}`;

  // the nonce
  let r = run("ask", ASK, {});
  check("the process is handed a one-time nonce in JARVIS_WIRE_NONCE", /^[0-9a-f]{16}$/.test(r.nonce), r.nonce);
  check("a caller's extraEnv cannot replace it",
    run("ask", ASK, { extraEnv: { JARVIS_WIRE_NONCE: "chosen" } }).nonce !== "chosen");
  check("every request gets its own", run("ask", ASK, {}).nonce !== run("ask", ASK, {}).nonce);

  // stderr: cards
  r = run("ask", ASK, { err: ["before", tagged("media", card), "after",
    tagged("media", "\u001b[2mJARVIS_MEDIA\tdownload\tj1\tf.mp4\tTitle\u001b[0m")] });
  check("a TAGGED card from a plain process: trace, card, trace, card -- in order",
    show(r.sent.map((m) => m.type)) === show(["ask-stderr", "ask-media", "ask-stderr", "ask-media"]), show(r.sent));
  check("the card is the same message the bridge path sends",
    show(r.sent[1]) === show({ type: "ask-media", kind: "screenshot", fields: ["a.png"] }), show(r.sent[1]));
  check("the download card keeps its three fields", show(r.sent[3].fields) === show(["j1", "f.mp4", "Title"]), show(r.sent[3]));
  check("the run log sees the legacy line, not the tag", show(r.logged[1]) === show(["err", card]), show(r.logged));

  r = run("ask", ASK, { err: [card, `${OPEN}media${CLOSE}${card}`, `${OPEN}media:guess${CLOSE}${card}`,
    `${OPEN}marker:${"0".repeat(16)}${CLOSE}${card}`, "echo " + card] });
  check("a bare line, a bare tag and a guessed tag are plain stderr -- never a card",
    r.sent.length === 5 && r.sent.every((m) => m.type === "ask-stderr"), show(r.sent));
  check("...and the tag text is not left in them", r.sent.every((m) => !m.line.includes(OPEN) && !m.line.includes(CLOSE)), show(r.sent));
  check("a tagged line with a name other than `media` is not a card on stderr",
    run("ask", ASK, { err: [(n) => `${OPEN}marker:${n}${CLOSE}${card}`] }).sent[0].type === "ask-stderr");
  check("a card of a kind outside the whitelist is plain stderr even when tagged",
    run("ask", ASK, { err: [tagged("media", "JARVIS_MEDIA\tBad Kind\tx")] }).sent[0].type === "ask-stderr");
  r = run("run", RUN, { err: [tagged("media", card)] });
  check("a direct run is unchanged: the line stays a stderr message (tag removed)",
    show(r.sent) === show([{ type: "stderr", line: card }]), show(r.sent));

  // stdout: markers
  const confirm = 'JARVIS_CONFIRM_REQUEST {"tool":"evil","arguments":{}}';
  const offered = [];
  const parser = (line) => { offered.push(line); return line.startsWith("JARVIS_CONFIRM_REQUEST "); };
  r = run("ask", ASK, { out: [tagged("marker", confirm), confirm, `${OPEN}marker${CLOSE}${confirm}`,
    `${OPEN}marker:guess${CLOSE}${confirm}`, `${OPEN}media:`, "plain"], onStdoutLine: parser });
  check("only the TAGGED marker is offered to the marker parser", show(offered.slice(0, 1)) === show([confirm]) && offered.length === 1, show(offered));
  check("a bare marker line, a bare tag and a guessed tag are shown as plain stdout",
    r.sent.filter((m) => m.type === "ask-stdout" && m.line === confirm).length === 3, show(r.sent));
  check("the tagged marker itself was swallowed by the parser (not shown as text)",
    r.sent.filter((m) => m.type === "ask-stdout").length === 5, show(r.sent));
  check("a tag that never closes is left as the text it is", r.sent.some((m) => m.line === OPEN + "media:"), show(r.sent));
  check("the run log sees the legacy lines, not the tags",
    r.logged.filter(([s2]) => s2 === "out").every(([, l]) => !l.includes(CLOSE)), show(r.logged));
}

// ---- bridgeOnce ----------------------------------------------------------------------------
{
  const sandbox = {
    console, setTimeout, clearTimeout, Promise,
    bridge: { request: null },
  };
  vm.createContext(sandbox);
  vm.runInContext(slice("function bridgeOnce(", "function spawnJarvisOnce("), sandbox, { filename: "server.js (bridgeOnce)" });
  sandbox.bridge.request = (opts) => {
    setImmediate(() => {
      opts.onEvent({ id: "r", t: "out", line: "stdout line" });
      opts.onEvent({ id: "r", t: "err", line: "stderr line" });
      opts.onEvent({ id: "r", t: "media", kind: "screenshot", fields: ["a.png"], raw: "JARVIS_MEDIA\tscreenshot\ta.png" });
      opts.onEvent({ id: "r", t: "usage", usage: {}, raw: "JARVIS_USAGE {}" });
      opts.onFinish({ code: 0 });
    });
    return { kill() {} };
  };
  sandbox.bridgeOnce(["x"], 5000, {}, null).then((res) => {
    check("a media event is filed with stderr, as the process printed it",
      res.stderr === "stderr line\nJARVIS_MEDIA\tscreenshot\ta.png", show(res));
    check("...and not with stdout (which a panel parses as JSON)",
      res.stdout === "stdout line\nJARVIS_USAGE {}", show(res));
    finish();
  });
}

// ---- app.js --------------------------------------------------------------------------------
{
  const app = fs.readFileSync(path.join(ROOT, "web", "public", "app.js"), "utf8");
  check("app.js handles the ask-media message", /case "ask-media":[\s\S]{0,400}handleAskMedia\(msg\.kind, msg\.fields\)/.test(app));
  check("handleAskMedia exists once", (app.match(/function handleAskMedia\(/g) || []).length === 1);
  const trace = app.slice(app.indexOf("function addAskPromptTrace("), app.indexOf("function formatFileSize("));
  check("a stderr line is never drawn as a card: addAskPromptTrace has no JARVIS_MEDIA branch",
    !/JARVIS_MEDIA/.test(trace.replace(/\/\/[^\n]*/g, "")) && !/handleAskMedia/.test(trace.replace(/\/\/[^\n]*/g, "")));
}

function finish() {
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}
