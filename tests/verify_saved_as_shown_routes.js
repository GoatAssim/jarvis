// The two server routes behind "save it as shown" (web/server.js), run for real out of the
// source (sliced by range, like the other verify_*.js scripts) against stubs -- no express,
// no jarvis process:
//
//   POST /api/console/:id/trace         -> `console-append-trace <id>` with the lines on stdin
//   POST /api/conversations/:id/display -> `conv-save-display <id>` with the items on stdin
//
// Run: node tests/verify_saved_as_shown_routes.js

const fs = require("fs");
const path = require("path");

const server = fs.readFileSync(path.join(__dirname, "..", "web", "server.js"), "utf8");
let passed = 0;
let failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("ok      " + name); }
  else { failed++; console.log("FAILED  " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : "")); }
}

function loadRoute(routePath) {
  const start = server.indexOf(`app.post("${routePath}"`);
  if (start === -1) { console.error("route not found: " + routePath); process.exit(2); }
  const end = server.indexOf("\n});", start) + 4;
  const code = server.slice(start, end);
  const routes = {};
  const calls = [];
  let reply = { ok: true, code: 0, stdout: '{"ok":true}', stderr: "" };
  const fakeApp = { post: (p, ...handlers) => { routes[p] = handlers[handlers.length - 1]; } };
  new Function("app", "requireJarvis", "runJarvisOnce", "isValidConversationId", code)(
    fakeApp, () => {},
    async (args, timeout, env, stdin) => { calls.push({ args, timeout, env, stdin }); return reply; },
    (id) => typeof id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(id));
  return { handler: routes[routePath], calls, setReply: (r) => { reply = r; } };
}

function res() {
  const r = { statusCode: 200, body: null };
  r.status = (c) => { r.statusCode = c; return r; };
  r.json = (b) => { r.body = b; return r; };
  return r;
}

(async () => {
  {
    const route = loadRoute("/api/console/:id/trace");
    let r = res();
    await route.handler({ params: { id: "abc123" }, body: { turn: "b1", lines: [{ text: "[tools] x", cls: "sys" }, { text: 5 }, null] } }, r);
    const sent = route.calls[0];
    check("trace: runs console-append-trace for that conversation", sent && sent.args[0] === "console-append-trace" && sent.args[1] === "abc123", sent && sent.args);
    const payload = sent && JSON.parse(sent.stdin);
    check("trace: the lines go over stdin, text and class intact", payload && payload.turn === "b1" && payload.lines[0].text === "[tools] x" && payload.lines[0].cls === "sys", payload);
    check("trace: a line with no text becomes an empty plain line, never undefined", payload && payload.lines[1].text === "" && payload.lines[1].cls === "sys" && payload.lines[2].text === "", payload);
    check("trace: answers with the CLI's reply", r.statusCode === 200 && r.body && r.body.ok === true, r);

    r = res();
    await route.handler({ params: { id: "../etc" }, body: { lines: [] } }, r);
    check("trace: a bad conversation id is a 400 and runs nothing", r.statusCode === 400 && route.calls.length === 1, r);
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { lines: "nope" } }, r);
    check("trace: lines that are not a list are a 400", r.statusCode === 400, r);
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { lines: [{ text: "x".repeat(1_100_000) }] } }, r);
    check("trace: an oversized body is a 413 and runs nothing", r.statusCode === 413 && route.calls.length === 1, r);
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { lines: new Array(5000).fill({ text: "a" }) } }, r);
    check("trace: at most 2000 lines are passed on in one call", JSON.parse(route.calls[1].stdin).lines.length === 2000);

    route.setReply({ ok: true, code: 1, stdout: '{"error":"invalid conversation id"}', stderr: "" });
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { lines: [{ text: "a" }] } }, r);
    check("trace: an error the CLI reports comes back as a 400", r.statusCode === 400 && r.body.error, r);
    route.setReply({ ok: false, error: "spawn failed", stdout: "", stderr: "" });
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { lines: [{ text: "a" }] } }, r);
    check("trace: a CLI that could not run is a 500 with its reason", r.statusCode === 500 && /spawn failed/.test(r.body.error), r);
  }

  {
    const route = loadRoute("/api/conversations/:id/display");
    let r = res();
    const items = [{ type: "screenshot", data: { filename: "a.png" } }];
    await route.handler({ params: { id: "abc123" }, body: { items } }, r);
    const sent = route.calls[0];
    const payload = sent && JSON.parse(sent.stdin);
    check("display: runs conv-save-display for that conversation", sent && sent.args[0] === "conv-save-display" && sent.args[1] === "abc123", sent && sent.args);
    check("display: the items go over stdin untouched; no user text means 'the newest finished exchange'",
      payload && JSON.stringify(payload.items) === JSON.stringify(items) && payload.user === null, payload);
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { items, user: "q" } }, r);
    check("display: a user text is passed on to pick that turn", JSON.parse(route.calls[1].stdin).user === "q");
    r = res();
    await route.handler({ params: { id: "no spaces allowed" }, body: { items } }, r);
    check("display: a bad conversation id is a 400", r.statusCode === 400, r);
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { items: {} } }, r);
    check("display: items that are not a list are a 400", r.statusCode === 400, r);
    r = res();
    await route.handler({ params: { id: "abc123" }, body: { items: [{ type: "console", data: { dumpLines: ["x".repeat(1_100_000)] } }] } }, r);
    check("display: an oversized body is a 413", r.statusCode === 413, r);
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
