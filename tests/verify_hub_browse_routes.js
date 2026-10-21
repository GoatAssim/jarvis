// Verifies the two picker routes in web/server.js (Download Hub N14) against the REAL source: the guard
// functions and the route block are cut out of server.js and run in Node's vm with fake request / response
// objects.  The Express server itself is not started (no npm install needed), so this checks our code's
// decisions, not Express's plumbing.
//
//   node tests/verify_hub_browse_routes.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const server = fs.readFileSync(path.join(__dirname, "..", "web", "server.js"), "utf8");
let n = 0;
function ok(name, cond) {
  n += 1;
  if (!cond) throw new Error(`FAILED: ${name}`);
  console.log(`ok       ${name}`);
}
function between(startMarker, endMarker) {
  const a = server.indexOf(startMarker);
  const b = server.indexOf(endMarker, a);
  if (a < 0 || b < 0) throw new Error(`markers not found: ${startMarker} .. ${endMarker}`);
  return server.slice(a, b);
}

const loopbackLine = server.split("\n").find((l) => l.startsWith("const LOOPBACK_HOST_RE ="));
const guards = between("function hubOriginOk(req, res)", "const HUB_ID_RE");
const helpers = between("function hubText(value, max)", "app.get(\"/api/hub/card\"");
const block = between("// N14, the folder / file picker", "// The push stream");

const routes = {};
const calls = [];
const app = {
  post: (route, ...handlers) => { routes[route] = handlers; },
};
const ctx = vm.createContext({
  app, PORT: 4173, URL, Number, Array, Set, String,
  requireJarvis: (req, res, next) => next(),
  hubAdmin: (res, op, payload, timeout) => { calls.push({ op, payload, timeout }); return res.json({ ok: true }); },
});
vm.runInContext([loopbackLine, guards, helpers, block].join("\n"), ctx);

function fakeRes() {
  const res = { code: 200, body: null };
  res.status = (c) => { res.code = c; return res; };
  res.json = (b) => { res.body = b; return res; };
  return res;
}
// Runs a route's middleware chain; returns {res, reached} where `reached` is whether the final handler ran.
function hit(route, req) {
  const res = fakeRes();
  const chain = routes[route];
  let reached = false;
  const run = (i) => {
    if (i >= chain.length) return;
    const fn = chain[i];
    if (i === chain.length - 1) { reached = true; fn(req, res); return; }
    fn(req, res, () => run(i + 1));
  };
  run(0);
  return { res, reached };
}
const good = (extra) => ({ headers: Object.assign({ host: "127.0.0.1:4173", "x-jarvis-hub": "1" }, (extra && extra.headers) || {}), body: (extra && extra.body) || {} });

const BROWSE = "/api/hub/browse";
const NEW = "/api/hub/browse/new-folder";
ok("both routes are registered, POST", Array.isArray(routes[BROWSE]) && Array.isArray(routes[NEW]));

for (const route of [BROWSE, NEW]) {
  const tag = route.replace("/api/hub/", "");
  let r = hit(route, good());
  ok(`${tag}: a loopback request with the header gets through`, r.reached && r.res.code === 200);
  r = hit(route, good({ headers: { host: "192.168.1.20:4173" } }));
  ok(`${tag}: a non-loopback Host is refused (403), the handler never runs`, !r.reached && r.res.code === 403);
  r = hit(route, good({ headers: { host: "evil.example.com" } }));
  ok(`${tag}: a DNS-rebinding style Host is refused`, !r.reached && r.res.code === 403);
  r = hit(route, { headers: { host: "127.0.0.1:4173" }, body: {} });
  ok(`${tag}: no X-Jarvis-Hub header is refused`, !r.reached && r.res.code === 403);
  r = hit(route, good({ headers: { "x-jarvis-hub": "0" } }));
  ok(`${tag}: a wrong header value is refused`, !r.reached && r.res.code === 403);
  r = hit(route, good({ headers: { origin: "http://evil.example.com" } }));
  ok(`${tag}: a foreign Origin is refused`, !r.reached && r.res.code === 403);
  r = hit(route, good({ headers: { "sec-fetch-site": "cross-site" } }));
  ok(`${tag}: a cross-site request is refused`, !r.reached && r.res.code === 403);
  r = hit(route, good({ headers: { origin: "http://localhost:4173", "sec-fetch-site": "same-origin", host: "localhost:4173" } }));
  ok(`${tag}: the page's own origin is accepted`, r.reached);
  r = hit(route, good({ headers: { host: "[::1]:4173" } }));
  ok(`${tag}: [::1] counts as loopback`, r.reached);
}

// --- what the handlers pass on ------------------------------------------------------------------------------
calls.length = 0;
hit(BROWSE, good({ body: { path: "D:\\Games", kind: "file", extensions: [".exe", 5, "iso"], hidden: true } }));
let c = calls[0];
ok("browse passes the op, path, kind, extensions and hidden", c.op === "browse" && c.payload.path === "D:\\Games" && c.payload.kind === "file" && c.payload.extensions.join() === ".exe,iso" && c.payload.hidden === true);
calls.length = 0;
hit(BROWSE, good({ body: { kind: "disk", hidden: "yes", extensions: "x" } }));
c = calls[0];
ok("browse: an unknown kind becomes 'folder', hidden needs a real true, junk extensions become none", c.payload.kind === "folder" && c.payload.hidden === false && c.payload.extensions.length === 0 && c.payload.path === "");
calls.length = 0;
hit(BROWSE, good({ body: { path: "x".repeat(5000), extensions: Array.from({ length: 80 }, (_, i) => "e" + i) } }));
c = calls[0];
ok("browse: a long path and a long extension list are cut", c.payload.path.length === 1100 && c.payload.extensions.length === 24);
calls.length = 0;
hit(BROWSE, good({ body: { path: { toString() { return "/etc"; } } } }));
ok("browse: a path that is not text is dropped", calls[0].payload.path === "");
hit(BROWSE, { headers: { host: "127.0.0.1:4173", "x-jarvis-hub": "1" }, body: null });
ok("browse: a missing body does not crash", calls.length === 2);
calls.length = 0;
hit(NEW, good({ body: { parent: "/home/me", name: "Games" } }));
ok("new-folder passes parent and name to the mkdir op", calls[0].op === "mkdir" && calls[0].payload.parent === "/home/me" && calls[0].payload.name === "Games");
hit(NEW, good({ body: { parent: "p".repeat(5000), name: "n".repeat(500) } }));
ok("new-folder cuts a long parent and name", calls[1].payload.parent.length === 1100 && calls[1].payload.name.length === 200);

// --- the source -----------------------------------------------------------------------------------------------------
ok("the picker block never reads req.query or req.params", !/req\.(query|params)/.test(block));
ok("the picker block is POST only", !/app\.get\(/.test(block));
ok("the existing write routes are unchanged in number", (server.match(/requireHubWrite, requireJarvis/g) || []).length === 5); // 4 queue/history routes + the Stremio paths save (N9)

console.log(`\n${n} checks passed`);
