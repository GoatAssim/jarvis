// Master plan L.38 follow-up: the web UI replays from the raw event log.
//
// Runs the REAL code out of web/server.js and web/public/app.js (sliced by
// source range, like the other verify_*.js scripts, so it can't drift from a
// copy) against stubs -- no express, no browser, no jarvis process:
//
//   - GET /api/conversations/:id  -> asks `conv-show <id> --replay`, unless
//     ?source=file; still validates the id; still maps errors the same way
//   - GET /api/console/:id        -> adds --full only for ?full=1 / true
//   - Api.getConsole              -> sends full=1 only when asked
//   - loadAskTraceForConv         -> asks for the full console history
//
// Run: node tests/verify_replay_from_log.js

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..", "web");
const server = fs.readFileSync(path.join(ROOT, "server.js"), "utf8");
const app = fs.readFileSync(path.join(ROOT, "public", "app.js"), "utf8");

let passed = 0;
let failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("ok      " + name); }
  else { failed++; console.log("FAILED  " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : "")); }
}

function slice(src, startMarker, endMarker, label) {
  const a = src.indexOf(startMarker);
  const b = src.indexOf(endMarker, a + startMarker.length);
  if (a === -1 || b === -1) {
    console.error("Could not find " + label + " -- did the surrounding code move?");
    process.exit(2);
  }
  return src.slice(a, b);
}

// ---- server.js routes ---------------------------------------------------------

function loadRoute(routePath) {
  const start = server.indexOf(`app.get("${routePath}"`);
  if (start === -1) { console.error("route not found: " + routePath); process.exit(2); }
  const end = server.indexOf("\n});", start) + 4;
  const code = server.slice(start, end);
  const routes = {};
  const fakeApp = { get: (p, ...handlers) => { routes[p] = handlers[handlers.length - 1]; } };
  const calls = [];
  let reply = { ok: true, stdout: "{}", stderr: "" };
  const ctx = {
    runJarvisOnce: async (args, timeout) => { calls.push({ args, timeout }); return reply; },
    isValidConversationId: (id) => typeof id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(id),
    requireJarvis: () => {},
  };
  new Function("app", "requireJarvis", "runJarvisOnce", "isValidConversationId", code)(
    fakeApp, ctx.requireJarvis, ctx.runJarvisOnce, ctx.isValidConversationId);
  return {
    handler: routes[routePath],
    calls,
    setReply: (r) => { reply = r; },
  };
}

function fakeRes() {
  const res = { statusCode: 200, body: undefined };
  res.status = (c) => { res.statusCode = c; return res; };
  res.json = (b) => { res.body = b; return res; };
  return res;
}

(async () => {
  // conversation route
  {
    const r = loadRoute("/api/conversations/:id");
    r.setReply({ ok: true, stdout: JSON.stringify({ id: "abc", exchanges: [1, 2], replay: { source: "events" } }), stderr: "" });
    let res = fakeRes();
    await r.handler({ params: { id: "abc" }, query: {} }, res);
    check("conversation: default asks conv-show with --replay",
      JSON.stringify(r.calls[0].args) === JSON.stringify(["conv-show", "abc", "--replay"]), r.calls[0]);
    check("conversation: the reply (incl. `replay`) is passed through unchanged",
      res.statusCode === 200 && res.body.replay.source === "events" && res.body.exchanges.length === 2, res.body);
    check("conversation: allows enough time for a long history (> the old 10 s)", r.calls[0].timeout >= 15000, r.calls[0].timeout);

    res = fakeRes();
    await r.handler({ params: { id: "abc" }, query: { source: "file" } }, res);
    check("conversation: ?source=file asks for the file alone (no --replay)",
      JSON.stringify(r.calls[1].args) === JSON.stringify(["conv-show", "abc"]), r.calls[1]);

    res = fakeRes();
    await r.handler({ params: { id: "../x" }, query: {} }, res);
    check("conversation: an invalid id is still a 400 and runs nothing", res.statusCode === 400 && r.calls.length === 2, res);

    r.setReply({ ok: false, stdout: JSON.stringify({ error: "no such conversation" }), stderr: "" });
    res = fakeRes();
    await r.handler({ params: { id: "gone" }, query: {} }, res);
    check("conversation: 'no such conversation' is still a 404", res.statusCode === 404, res);

    r.setReply({ ok: false, stdout: "", stderr: "boom", error: "" });
    res = fakeRes();
    await r.handler({ params: { id: "abc" }, query: {} }, res);
    check("conversation: a crashed CLI is still a 500", res.statusCode === 500 && /boom/.test(res.body.error), res);
  }

  // console route
  {
    const r = loadRoute("/api/console/:id");
    r.setReply({ ok: true, stdout: JSON.stringify({ id: "abc", lines: [] }), stderr: "" });
    const run = async (query) => { const res = fakeRes(); await r.handler({ params: { id: "abc" }, query }, res); return r.calls[r.calls.length - 1].args; };

    let args = await run({ surface: "ask", limit: "2000" });
    check("console: no `full` -> no --full (the poll path is unchanged)", !args.includes("--full"), args);
    args = await run({ surface: "ask", limit: "2000", full: "1" });
    check("console: full=1 -> --full", args.includes("--full"), args);
    args = await run({ full: "true", afterLastClear: "1" });
    check("console: full=true works and composes with afterLastClear",
      args.includes("--full") && args.includes("--after-last-clear"), args);
    args = await run({ full: "yes please; rm -rf" });
    check("console: anything else is ignored (no injection into argv)", !args.includes("--full") && args.length === 2, args);
    const bad = fakeRes();
    await r.handler({ params: { id: "../x" }, query: { full: "1" } }, bad);
    check("console: an invalid id is still a 400", bad.statusCode === 400, bad);
  }

  // ---- app.js ----------------------------------------------------------------------

  {
    const code = slice(app, "getConsole: (id, opts = {}) => {", "clearConsole:", "Api.getConsole");
    const urls = [];
    const api = (method, url) => { urls.push(url); return Promise.resolve({}); };
    const Api = new Function("api", "return {" + code.replace(/,\s*$/, "") + "};")(api);
    Api.getConsole("c1", { surface: "ask", afterLastClear: true, limit: 2000, full: true });
    Api.getConsole("c1", { since: 5, surface: "ask", limit: 500 });
    check("Api.getConsole: full:true -> full=1 in the query", /[?&]full=1(&|$)/.test(urls[0]), urls[0]);
    check("Api.getConsole: the other options are still sent", /surface=ask/.test(urls[0]) && /afterLastClear=1/.test(urls[0]) && /limit=2000/.test(urls[0]), urls[0]);
    check("Api.getConsole: the poll (no full) never carries it", !/full=/.test(urls[1]) && /since=5/.test(urls[1]), urls[1]);
  }

  {
    const code = slice(app, "async function loadAskTraceForConv(convId) {", "// Maps a console_store {kind, text} line", "loadAskTraceForConv");
    const seen = [];
    const state = { askTraceByConv: {}, activeConversationId: "c1" };
    const Api = { getConsole: async (id, opts) => { seen.push(opts); return { lines: [{ kind: "tool-call", text: "x", turn: "t" }] }; } };
    const askLineFromStoredEntry = (l) => ({ text: l.text, cls: "k" });
    const fn = new Function("state", "Api", "askLineFromStoredEntry", code + "; return loadAskTraceForConv;")(state, Api, askLineFromStoredEntry);
    await fn("c1");
    check("loadAskTraceForConv: asks for the full history from the event log",
      seen.length === 1 && seen[0].full === true && seen[0].surface === "ask" && seen[0].afterLastClear === true, seen);
    check("loadAskTraceForConv: still hydrates the trace", state.askTraceByConv.c1 && state.askTraceByConv.c1.length === 1, state.askTraceByConv);
  }

  check("the Live output history load asks for full too",
    /Api\.getConsole\(convId, \{ surface: "live", afterLastClear: true, limit: 2000, full: true \}\)/.test(app));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
