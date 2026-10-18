// Express matches routes in registration order. `POST /api/daemons/:id/:action` also matches
// `/api/daemons/:id/schedule` and `/api/daemons/:id/input`, so it must be registered AFTER
// them -- otherwise the Daemons panel's "schedule start" and "send input" calls were answered
// with 400 "Unknown action." (a stack of red toasts in the panel).
//
//   node tests/verify_daemon_route_order.js
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "server.js"), "utf8");
const at = (needle) => {
  const i = src.indexOf(needle);
  assert.ok(i >= 0, `route not found: ${needle}`);
  return i;
};

const generic = at('app.post("/api/daemons/:id/:action"');
const schedule = at('app.post("/api/daemons/:id/schedule"');
const input = at('app.post("/api/daemons/:id/input"');

assert.ok(schedule < generic, "/schedule must be registered before /:id/:action");
assert.ok(input < generic, "/input must be registered before /:id/:action");
console.log("ok       daemon schedule/input routes come before the generic :action route");
