// Verifies POST /api/slash/run in web/server.js: the allowlist (the registry's
// `passthrough` keys), the argument validation, and the registry's `maxArgs`
// cap (master plan I-B16 / I-B17) - against the REAL route source and the REAL
// slash-commands-data.js, with `app` and `runJarvisOnce` stubbed.
//
// server.js can't be imported in a test (it starts Express and needs npm
// packages), so this slices the route's source out of the file between two
// markers and runs it in a vm sandbox. If someone moves the markers the test
// fails loudly instead of silently testing nothing.
//
//   node tests/verify_slash_route.js
//
// No npm packages needed. It does not start a server and never runs `jarvis`.

"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "web");
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("ok   " + name); }
  else { fail++; console.log("FAIL " + name + (detail ? ": " + detail : "")); }
}

const src = fs.readFileSync(path.join(WEB, "server.js"), "utf8");
const START = "const SLASH_PASSTHROUGH = (() => {";
const END = "// Read-only: which skills are manually loaded right now";
const a = src.indexOf(START), b = src.indexOf(END);
check("the route's source markers are still in server.js", a > 0 && b > a, `start=${a} end=${b}`);

async function harness(readFileSyncImpl) {
  const handlers = {};
  const spawned = [];
  const sandbox = {
    console: Object.assign(Object.create(console), { warn() {} }),
    path, __dirname: WEB,
    readFileSync: readFileSyncImpl || fs.readFileSync,
    app: { post: (route, _mw, h) => { handlers[route] = h; } },
    requireJarvis: () => {},
    runJarvisOnce: async (argv, timeout) => { spawned.push({ argv, timeout }); return { ok: true, code: 0, stdout: "ran", stderr: "" }; },
  };
  vm.createContext(sandbox);
  vm.runInContext(src.slice(a, b).replace(/\bapp\.get\([\s\S]*$/, ""), sandbox, { filename: "server.js (slash route)" });
  const call = async (body) => {
    let status = 200, json = null;
    const res = { status(c) { status = c; return this; }, json(o) { json = o; return this; } };
    await handlers["/api/slash/run"]({ body }, res);
    return { status, json };
  };
  return { call, spawned };
}

(async () => {
  const { call, spawned } = await harness();

  let r = await call({ name: "doctor", args: ["--deep"] });
  check("an allowlisted command runs as [name, ...args] with no shell", r.status === 200 && r.json.ok === true
    && JSON.stringify(spawned.slice(-1)[0].argv) === '["doctor","--deep"]' && spawned.slice(-1)[0].timeout === 60000, JSON.stringify(r));
  const n0 = spawned.length;
  r = await call({ name: "speak", args: [] });
  check("a name in notExposed is refused (allowlist = passthrough only)", r.status === 400 && spawned.length === n0);
  r = await call({ name: "rm", args: [] });
  check("an unknown name is refused", r.status === 400 && spawned.length === n0);
  r = await call({ name: "doctor", args: "x" });
  check("args must be an array", r.status === 400);
  r = await call({ name: "doctor", args: new Array(25).fill("a") });
  check("more than 24 args is refused", r.status === 400);
  r = await call({ name: "doctor", args: ["a\0b"] });
  check("a NUL byte is refused", r.status === 400);
  r = await call({ name: "doctor", args: [5] });
  check("a non-string arg is refused", r.status === 400 && spawned.length === n0);

  // maxArgs (I-B16, I-B17)
  r = await call({ name: "subagent-keys", args: ["researcher", "openai", "sk-SECRET"] });
  check("/subagent-keys with arguments is refused by the SERVER, and nothing is spawned", r.status === 400 && spawned.length === n0, JSON.stringify(r));
  check("...the refusal never echoes the key", !JSON.stringify(r.json).includes("sk-SECRET"));
  r = await call({ name: "subagent-keys", args: ["--clear"] });
  check("...including the --clear form (list-only means list-only)", r.status === 400 && spawned.length === n0);
  r = await call({ name: "subagent-keys", args: [] });
  check("/subagent-keys with no arguments runs (lists the pools)", r.status === 200 && JSON.stringify(spawned.slice(-1)[0].argv) === '["subagent-keys"]');
  const n1 = spawned.length;
  r = await call({ name: "sched-clear", args: ["--all"] });
  check("/sched-clear --all is refused (the CLI ignores flags, so it must not look like it took one)", r.status === 400 && spawned.length === n1);
  r = await call({ name: "sched-clear", args: [] });
  check("/sched-clear with no arguments runs", r.status === 200 && spawned.length === n1 + 1);
  r = await call({ name: "memory-recall", args: ["a", "b", "c", "d", "e"] });
  check("a command with no maxArgs still takes any number of arguments (up to 24)", r.status === 200);

  // fail closed
  const closed = await harness(() => { throw new Error("no registry"); });
  const n2 = closed.spawned.length;
  r = await closed.call({ name: "doctor", args: [] });
  check("if the registry can't be read, nothing runs (fails closed)", r.status === 400 && closed.spawned.length === n2);

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail) process.exit(1);
})();
