// Verifies the tool-command routes in web/server.js (master plan I.2.11 / K.6.7):
//   GET  /api/tool-commands                    -- listing, minus names that are the palette's own words
//   POST /api/tool-commands/:name/prepare      -- name + argument validation, always the same argv shape
//   POST /api/tool-commands/:name/run
// against the REAL route source and the REAL slash-commands-data.js, with `app` and
// `runJarvisOnce` stubbed. server.js can't be imported (it starts Express), so this
// slices the section out between two markers and runs it in a vm sandbox; if someone
// moves the markers the test fails loudly instead of silently testing nothing.
//
//   node tests/verify_tool_commands_route.js
//
// No npm packages needed. Never starts a server, never runs `jarvis`.

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
const START = "const TOOL_COMMAND_NAME_RE";
const END = "// ---------------------------------------------------------------------------\n// Custom tools";
const a = src.indexOf(START), b = src.indexOf(END);
check("the routes' source markers are still in server.js", a > 0 && b > a, `start=${a} end=${b}`);

function harness({ readFileSyncImpl, jarvis } = {}) {
  const handlers = {};
  const spawned = [];
  const sandbox = {
    console: Object.assign(Object.create(console), { warn() {} }),
    path, __dirname: WEB,
    readFileSync: readFileSyncImpl || fs.readFileSync,
    app: {
      get: (route, _mw, h) => { handlers["GET " + route] = h; },
      post: (route, _mw, h) => { handlers["POST " + route] = h; },
    },
    requireJarvis: () => {},
    runJarvisOnce: async (argv, timeout, env) => {
      spawned.push({ argv, timeout, env });
      return jarvis ? jarvis(argv) : { ok: true, code: 0, stdout: "{}", stderr: "" };
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(src.slice(a, b), sandbox, { filename: "server.js (tool commands)" });
  const call = async (key, { params, body, query } = {}) => {
    let status = 200, json = null;
    const res = { status(c) { status = c; return this; }, json(o) { json = o; return this; } };
    await handlers[key]({ params: params || {}, body, query: query || {} }, res);
    return { status, json };
  };
  return { call, spawned, handlers };
}

(async () => {
  const listing = JSON.stringify({ commands: [
    { name: "disk", tool: "disk_report" }, { name: "new", tool: "x" }, { name: "chat", tool: "x" },
    { name: "doctor", tool: "x" }, { name: "spotify-login", tool: "x" }, { name: "Bad", tool: "x" }, { name: "wipe", tool: "w" }] });
  const reg = await (async () => {
    const text = fs.readFileSync(path.join(WEB, "public", "slash-commands-data.js"), "utf8");
    return JSON.parse(/\/\*JSON-BEGIN\*\/([\s\S]*)\/\*JSON-END\*\//.exec(text)[1]);
  })();
  const alias = (reg.verbs.find((v) => (v.aliases || []).length) || {}).aliases;

  {
    const h = harness({ jarvis: () => ({ ok: true, code: 0, stdout: listing, stderr: "" }) });
    const r = await h.call("GET /api/tool-commands");
    const names = r.json.commands.map((c) => c.name);
    check("listing keeps ordinary tool commands", names.includes("disk") && names.includes("wipe"), JSON.stringify(names));
    check("listing drops a verb's name", !names.includes("new") && !names.includes("chat"));
    check("listing drops a CLI passthrough name", !names.includes("doctor"));
    check("listing drops a not-exposed CLI name", !names.includes("spotify-login"));
    check("listing drops a badly formed name", !names.includes("Bad"));
    check("the dropped ones are reported with a reason", r.json.dropped.some((d) => d.name === "new" && /built-in/.test(d.reason)), JSON.stringify(r.json.dropped));
    check("the CLI is asked with a fixed argv", JSON.stringify(h.spawned[0].argv) === '["tool-commands","list"]');
    await h.call("GET /api/tool-commands", { query: { all: "1" } });
    check("?all=1 asks for --all", JSON.stringify(h.spawned[1].argv) === '["tool-commands","list","--all"]');
    if (alias && alias.length) {
      const h2 = harness({ jarvis: () => ({ ok: true, code: 0, stdout: JSON.stringify({ commands: [{ name: alias[0], tool: "x" }] }), stderr: "" }) });
      check("listing drops a verb's ALIAS too", (await h2.call("GET /api/tool-commands")).json.commands.length === 0);
    }
  }
  {
    const h = harness({ readFileSyncImpl: () => { throw new Error("gone"); }, jarvis: () => ({ ok: true, code: 0, stdout: listing, stderr: "" }) });
    const r = await h.call("GET /api/tool-commands");
    check("if the registry can't be read, nothing is listed (fails closed)", r.json.commands.length === 0 && !h.spawned.length, JSON.stringify(r));
  }
  {
    const h = harness({ jarvis: () => ({ ok: false, code: 1, stdout: "", stderr: "traceback", error: "" }) });
    const r = await h.call("GET /api/tool-commands");
    check("a CLI failure is a 500 with the reason", r.status === 500 && /traceback/.test(r.json.error), JSON.stringify(r));
    const h2 = harness({ jarvis: () => ({ ok: true, code: 0, stdout: "not json", stderr: "" }) });
    check("unparseable CLI output is a 500, not a crash", (await h2.call("GET /api/tool-commands")).status === 500);
  }

  for (const [kind, timeout, env] of [["prepare", 15000, undefined], ["run", 30000, { JARVIS_UI: "web" }]]) {
    const key = `POST /api/tool-commands/:name/${kind}`;
    const h = harness({ jarvis: () => ({ ok: true, code: 0, stdout: kind === "run" ? '{"ok":true,"message":"hi"}' : '{"ok":true,"tool":"t","arguments":{}}', stderr: "" }) });
    let r = await h.call(key, { params: { name: "disk" }, body: { args: ["C:", "--flag"] } });
    check(`${kind}: runs [tool-commands, ${kind}, name, <ONE json array>] with no shell`, r.status === 200
      && JSON.stringify(h.spawned[0].argv) === JSON.stringify(["tool-commands", kind, "disk", '["C:","--flag"]'])
      && h.spawned[0].timeout === timeout && JSON.stringify(h.spawned[0].env) === JSON.stringify(env), JSON.stringify(h.spawned[0]));
    r = await h.call(key, { params: { name: "disk" }, body: {} });
    check(`${kind}: no args means []`, h.spawned.slice(-1)[0].argv[3] === "[]");
    const before = h.spawned.length;
    for (const bad of ["Disk", "d", "../x", "a b", "x;rm", "-a", "a".repeat(40), "9a"]) {
      r = await h.call(key, { params: { name: bad }, body: { args: [] } });
      check(`${kind}: name ${JSON.stringify(bad)} is refused`, r.status === 400, JSON.stringify(r));
    }
    for (const [label, args] of [["a string", "x y"], ["an object", { a: 1 }], ["a number inside", [1]], ["a NUL", ["a\0b"]], ["a 2001-char word", ["x".repeat(2001)]], ["25 words", Array(25).fill("a")]]) {
      r = await h.call(key, { params: { name: "disk" }, body: { args } });
      check(`${kind}: args as ${label} is refused`, r.status === 400, JSON.stringify(r));
    }
    check(`${kind}: none of the refused requests spawned anything`, h.spawned.length === before);
    r = await h.call(key, { params: { name: "disk" }, body: { args: ["a".repeat(2000)] } });
    check(`${kind}: a 2000-char word is the longest allowed`, r.status === 200);
  }
  {
    const h = harness({ jarvis: () => ({ ok: true, code: 0, stdout: '{"error":"no palette command called \'x\'"}', stderr: "" }) });
    const r = await h.call("POST /api/tool-commands/:name/prepare", { params: { name: "xx" }, body: { args: [] } });
    check("prepare: a CLI refusal is a 400 carrying its message", r.status === 400 && /no palette command/.test(r.json.error), JSON.stringify(r));
    const h2 = harness({ jarvis: () => ({ ok: false, code: 1, stdout: '{"error":"/wipe is off: its tool disk_wipe is switched off"}', stderr: "" }) });
    const r2 = await h2.call("POST /api/tool-commands/:name/run", { params: { name: "wipe" }, body: { args: [] } });
    check("run: a refusal comes back as {ok:false, result:{error}}", r2.status === 200 && r2.json.ok === false && /switched off/.test(r2.json.result.error), JSON.stringify(r2));
    const h3 = harness({ jarvis: () => ({ ok: false, code: 1, stdout: "", stderr: "boom", error: "" }) });
    check("run: no JSON at all is a 500", (await h3.call("POST /api/tool-commands/:name/run", { params: { name: "wipe" }, body: { args: [] } })).status === 500);
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
