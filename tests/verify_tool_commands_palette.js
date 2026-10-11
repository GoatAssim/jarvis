// Verifies the "/" palette's TOOL COMMANDS (master plan I.2.11 / K.6.7): commands a
// custom tool contributes through TOOL_COMMANDS, fetched from /api/tool-commands and
// run through prepare -> /api/tools/preview -> run. Runs the REAL slash-palette.js
// against the REAL slash-commands-data.js with a fake DOM, a fake JarvisHost and a
// fake fetch (so it never starts a server or runs `jarvis`).
//
//   node tests/verify_tool_commands_palette.js
//
// No npm packages needed. Real layout/focus is the manual pass in
// DOCUMENTATION/COMMAND_PALETTE_TESTING.md.

"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PUBLIC = path.join(__dirname, "..", "web", "public");

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("ok   " + name); }
  else { fail++; console.log("FAIL " + name + (detail ? ": " + detail : "")); }
}

// ---- fake DOM ------------------------------------------------------------
class FakeEl {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.handlers = {};
    this.className = ""; this.hidden = false; this.value = ""; this.dataset = {};
    this._text = "";
    this.classList = {
      toggle: (c, on) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        if (on) set.add(c); else set.delete(c);
        this.className = Array.from(set).join(" ");
      },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v); if (k === "id") this.id = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  dispatchEvent(e) { (this.handlers[e.type] || []).forEach((f) => f(e)); return true; }
  set innerHTML(_v) { this.children = []; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent || "").join(""); }
  querySelectorAll(sel) {
    const cls = sel.replace(/^\./, "");
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.className && c.className.split(/\s+/).includes(cls)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  contains() { return false; }
  scrollIntoView() {}
  setSelectionRange() {}
  focus() {}
  requestSubmit() { this.submits = (this.submits || 0) + 1; }
}


const COMMANDS = [
  { name: "disk", tool: "disk_report", summary: "How full is each drive", riskTier: "safe", usage: "[drive] [count]",
    description: "Longer hover text.", example: "/disk C:", file: "disk_demo.py",
    args: [{ name: "drive", required: false, hint: "e.g. C:" }, { name: "count", required: false, hint: "" }] },
  { name: "wipe", tool: "disk_wipe", summary: "Wipe a target", riskTier: "dangerous", usage: "<target>", file: "disk_demo.py",
    args: [{ name: "target", required: true, hint: "what to wipe" }] },
  { name: "notier", tool: "disk_report", summary: "Declares nothing sensible", riskTier: "banana", usage: "", file: "x.py", args: [] },
  { name: "gated", tool: "gated_tool", summary: "Its tool asks first", riskTier: "safe", usage: "", file: "x.py", args: [] },
  // none of these may ever show up:
  { name: "new", tool: "disk_report", summary: "shadows a verb", riskTier: "safe", args: [] },
  { name: "doctor", tool: "disk_report", summary: "shadows a CLI command", riskTier: "safe", args: [] },
  { name: "spotify-login", tool: "disk_report", summary: "shadows a not-exposed name", riskTier: "safe", args: [] },
  { name: "notool", tool: "", summary: "no tool to run", riskTier: "safe", args: [] },
  { name: "BadName", tool: "disk_report", summary: "bad name", riskTier: "safe", args: [] },
  { name: "offcmd", tool: "disk_report", summary: "switched off", riskTier: "safe", disabled: true, args: [] },
];

function makeEnv(opts) {
  opts = opts || {};
  const els = { "slash-palette": new FakeEl("div"), "ask-input": new FakeEl("textarea"), "ask-form": new FakeEl("form") };
  els["slash-palette"].hidden = true;
  const document = {
    readyState: "complete",
    getElementById: (id) => els[id] || null,
    createElement: (t) => new FakeEl(t),
    createTextNode: (t) => { const n = new FakeEl("#text"); n._text = t; return n; },
    addEventListener() {},
  };
  const calls = [], toasts = [], fetches = [];
  const cfg = {
    confirmAnswer: true,
    listing: opts.listing === undefined ? { commands: COMMANDS } : opts.listing,
    listFails: false,
    prepare: (name, args) => ({ ok: true, name, tool: (COMMANDS.find((c) => c.name === name) || {}).tool, arguments: { words: args }, riskTier: (COMMANDS.find((c) => c.name === name) || {}).riskTier }),
    preview: { confirm_required: false, ai_review: false },
    previewFails: false,
    run: { ok: true, result: { ok: true, message: "drive C: is 40% full" } },
  };
  const host = {
    toast: (m, k) => toasts.push([k || "error", m]),
    confirm: async (o) => { calls.push(["confirm", o.title, o.body || "", o.pre || "", o.level]); return cfg.confirmAnswer; },
    dialog: (o) => calls.push(["dialog", o.title, o.pre, o.level]),
    refreshChats() {},
    originLabel: () => "",
    state: () => ({ activeConversationId: "c1", conversations: [], commands: {}, running: false, aiProviders: [] }),
    loadConversations: async () => [],
    openPanel: (id) => calls.push(["openPanel", id]),
  };
  const jsonRes = (o, status) => ({ ok: !status || status < 400, status: status || 200, json: async () => o });
  const fetchStub = async (url, o) => {
    const method = (o && o.method) || "GET";
    fetches.push([method, url, o && o.body]);
    if (url === "/api/tool-commands") return cfg.listFails ? jsonRes({ error: "boom" }, 500) : jsonRes(cfg.listing);
    let m = /^\/api\/tool-commands\/([^/]+)\/prepare$/.exec(url);
    if (m) {
      const r = cfg.prepare(decodeURIComponent(m[1]), JSON.parse(o.body).args);
      return r.error ? jsonRes(r, 400) : jsonRes(r);
    }
    if (url === "/api/tools/preview") return cfg.previewFails ? jsonRes({ error: "no" }, 500) : jsonRes(typeof cfg.preview === "function" ? cfg.preview(JSON.parse(o.body)) : cfg.preview);
    m = /^\/api\/tool-commands\/([^/]+)\/run$/.exec(url);
    if (m) return jsonRes(cfg.run);
    return jsonRes({ error: "nope" }, 404);
  };
  const store = {};
  const window = { JarvisHost: host, localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } } };
  const testConsole = Object.assign(Object.create(console), { warn() {} });
  const sandbox = { window, document, fetch: fetchStub, console: testConsole, Event, setTimeout, Date, Promise };
  window.window = window;
  vm.createContext(sandbox);
  for (const f of ["slash-commands-data.js", "slash-palette.js"]) vm.runInContext(fs.readFileSync(path.join(PUBLIC, f), "utf8"), sandbox, { filename: f });
  return { S: window.JarvisSlash, els, calls, toasts, fetches, cfg, input: els["ask-input"], palette: els["slash-palette"] };
}

const flush = () => new Promise((r) => setTimeout(r, 0));
async function ready(env) { for (let i = 0; i < 4; i++) await flush(); }
async function submit(env, text) { env.input.value = text; return env.S.handleSubmit(env.input, []); }
async function type(env, v) { env.input.value = v; env.input.dispatchEvent(new Event("input")); await ready(env); }
const rowNames = (env) => env.palette.querySelectorAll(".slash-row__name").map((n) => n.textContent);
const headings = (env) => env.palette.querySelectorAll(".slash-heading").map((n) => n.textContent);
const urls = (env, re) => env.fetches.filter((f) => re.test(f[1]));

(async () => {
  // ---- loading and filtering ------------------------------------------------
  {
    const env = makeEnv(); await ready(env);
    check("init fetches /api/tool-commands once", urls(env, /^\/api\/tool-commands$/).length === 1);
    check("the good commands are known", ["disk", "wipe", "notier", "gated"].every((n) => env.S.toolCmd(n)), "");
    check("a command named like a VERB is ignored (built-in wins)", env.S.toolCmd("new") === null);
    check("a command named like a CLI passthrough is ignored", env.S.toolCmd("doctor") === null);
    check("a command named like a not-exposed CLI name is ignored", env.S.toolCmd("spotify-login") === null);
    check("a command with no tool is ignored", env.S.toolCmd("notool") === null);
    check("a badly named command is ignored", env.S.toolCmd("BadName") === null && env.S.toolCmd("badname") === null);
    check("a command the server flagged disabled is ignored", env.S.toolCmd("offcmd") === null);
    check("an unknown tier is dangerous here too (fail closed)", env.S.toolCmd("notier").riskTier === "dangerous");
    check("a declared tier is kept", env.S.toolCmd("disk").riskTier === "safe" && env.S.toolCmd("wipe").riskTier === "dangerous");
  }
  {
    const env = makeEnv({ listing: { commands: "garbage" } }); await ready(env);
    check("a garbage listing means no tool commands, no crash", env.S.toolCmd("disk") === null);
    const env2 = makeEnv({ listing: null }); env2.cfg.listFails = true; await ready(env2);
    check("a failing listing means no tool commands, no crash", env2.S.toolCmd("disk") === null);
  }

  // ---- the list ---------------------------------------------------------------
  {
    const env = makeEnv(); await ready(env);
    await type(env, "/");
    check("the empty list has a 'Tool commands' section", headings(env).includes("Tool commands"), headings(env).join("|"));
    const names = rowNames(env);
    check("...with the good commands in it", ["/disk", "/wipe", "/notier", "/gated"].every((n) => names.includes(n)), names.join(" "));
    check("...and none of the rejected ones", !names.includes("/offcmd") && names.filter((n) => n === "/new").length === 1 && names.filter((n) => n === "/doctor").length === 0);
    const tags = env.palette.querySelectorAll(".slash-row__tag").map((n) => n.textContent);
    check("tool rows are tagged 'tool'", tags.filter((t) => t === "tool").length === 4, tags.join(","));
    await type(env, "/dis");
    check("typing filters to a matching tool command", rowNames(env).includes("/disk") && !rowNames(env).includes("/wipe"), rowNames(env).join(" "));
    await type(env, "/drive");
    check("a summary match finds a tool command too", rowNames(env).includes("/disk"), rowNames(env).join(" "));
    await type(env, "/disk ");
    check("after the name, the args hint shows", env.palette.textContent.includes("args: [drive] [count]"), env.palette.textContent.slice(0, 200));
    const rows = env.palette.querySelectorAll(".slash-row");
    check("the hover title carries summary, description and the file", rows.length && /How full is each drive/.test(rows[0].attrs.title || "") && /Longer hover text/.test(rows[0].attrs.title || "") && /disk_demo\.py/.test(rows[0].attrs.title || ""), rows[0] && rows[0].attrs.title);
    await type(env, "/wipe ");
    check("a dangerous command's footer says it asks first", /Asks first/.test(env.palette.children[1].textContent), env.palette.children[1].textContent);
    await type(env, "/dsik");
    env.toasts.length = 0;
    const nm = await submit(env, "/dsik");
    check("a typo of a tool command gets a did-you-mean", nm.status === "blocked" && env.toasts.some((t) => /did you mean \/disk/.test(t[1])), JSON.stringify(env.toasts));
  }

  // ---- isComplete -----------------------------------------------------------------
  {
    const env = makeEnv(); await ready(env);
    check("isComplete: a tool command with no required arg is complete as typed", env.S.isComplete("/disk"));
    check("isComplete: one with a required arg is not, bare", !env.S.isComplete("/wipe") && !env.S.isComplete("/wipe "));
    check("isComplete: ...until the arg is typed", env.S.isComplete("/wipe the cache"));
    check("isComplete: an unknown tool name is not", !env.S.isComplete("/nothing"));
  }

  // ---- running --------------------------------------------------------------------
  {
    const env = makeEnv(); await ready(env);
    env.cfg.listing = { commands: COMMANDS };
    const r = await submit(env, "/disk C: 3");
    check("a safe command is handled", r.status === "handled", JSON.stringify(r));
    const seq = env.fetches.filter((f) => /prepare|tools\/preview|\/run$/.test(f[1])).map((f) => f[1].replace("/api/", ""));
    check("it goes prepare -> preview -> run, in that order", seq.join(",") === "tool-commands/disk/prepare,tools/preview,tool-commands/disk/run", seq.join(","));
    check("the typed words are sent as an array", JSON.parse(env.fetches.find((f) => /prepare/.test(f[1]))[2]).args.join("|") === "C:|3");
    check("the preview is asked about the TOOL and the arguments the SERVER built", JSON.parse(urls(env, /tools\/preview/)[0][2]).name === "disk_report" && JSON.parse(urls(env, /tools\/preview/)[0][2]).arguments.words.join("|") === "C:|3");
    check("a safe, ungated command does not confirm", !env.calls.some((c) => c[0] === "confirm"));
    check("a short result is a toast", env.toasts.some((t) => t[0] === "info" && /40% full/.test(t[1])), JSON.stringify(env.toasts));
    check("the box is cleared", env.input.value === "");
  }
  {
    const env = makeEnv(); await ready(env);
    await submit(env, "/disk \"my drive\" 2");
    check("quoted words stay one argument", JSON.parse(env.fetches.find((f) => /prepare/.test(f[1]))[2]).args.join("|") === "my drive|2");
  }
  {
    const env = makeEnv(); await ready(env);
    const r = await submit(env, "/wipe");
    check("a missing required argument is refused before any request", r.status === "blocked" && !urls(env, /prepare|preview|\/run/).length && env.toasts.some((t) => /needs what to wipe/.test(t[1])), JSON.stringify(env.toasts));
    check("...and the text is given back", env.input.value === "/wipe");
  }
  {
    const env = makeEnv(); await ready(env);
    const r = await submit(env, "/wipe /tmp/x");
    check("a dangerous command confirms first", env.calls.some((c) => c[0] === "confirm" && /Run \/wipe\?/.test(c[1])), JSON.stringify(env.calls));
    check("...with the tool and arguments shown", env.calls.find((c) => c[0] === "confirm")[3].includes("disk_wipe") && env.calls.find((c) => c[0] === "confirm")[3].includes("/tmp/x"));
    check("...at the error level", env.calls.find((c) => c[0] === "confirm")[4] === "error");
    check("...and runs once approved", r.status === "handled" && urls(env, /\/run$/).length === 1);
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.confirmAnswer = false;
    const r = await submit(env, "/wipe /tmp/x");
    check("declining runs nothing", r.status === "blocked" && !urls(env, /\/run$/).length);
    check("...and gives the text back", env.input.value === "/wipe /tmp/x");
  }
  {
    const env = makeEnv(); await ready(env);
    await submit(env, "/notier");
    check("a command with a nonsense tier confirms (dangerous)", env.calls.some((c) => c[0] === "confirm"));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.preview = { confirm_required: true, risk_note: { note: "this deletes things" } };
    await submit(env, "/gated");
    const c = env.calls.find((x) => x[0] === "confirm");
    check("a tool that asks first is confirmed even for a safe command", !!c && /deletes things/.test(c[2]) && c[4] === "warn", JSON.stringify(env.calls));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.preview = { ai_review: true };
    await submit(env, "/gated");
    check("an AI-reviewed tool is confirmed too", env.calls.some((c) => c[0] === "confirm"));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.previewFails = true;
    const r = await submit(env, "/disk");
    check("if the confirmation check fails, nothing runs (fail closed)", r.status === "blocked" && !urls(env, /\/run$/).length && env.toasts.some((t) => /did not run/.test(t[1])), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.prepare = () => ({ error: "count must be a whole number (got 'x')" });
    const r = await submit(env, "/disk C: x");
    check("a prepare refusal is shown and nothing runs", r.status === "blocked" && !urls(env, /preview|\/run$/).length && env.toasts.some((t) => t[0] === "warn" && /whole number/.test(t[1])), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.run = { ok: true, result: { error: "disk not found" } };
    await submit(env, "/disk Z:");
    check("a tool's {error} result is an error toast", env.toasts.some((t) => t[0] === "error" && /disk not found/.test(t[1])), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.run = { ok: true, result: { ok: true, rows: [1, 2, 3], note: "x".repeat(200) } };
    await submit(env, "/disk");
    const d = env.calls.find((c) => c[0] === "dialog");
    check("a long / structured result opens a dialog with the JSON", !!d && d[1] === "/disk" && /"rows"/.test(d[2]) && d[3] === "info", JSON.stringify(env.calls));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.run = { ok: false, result: { ok: false, error: "x\ny" } };
    await submit(env, "/disk");
    const d = env.calls.find((c) => c[0] === "dialog");
    check("a failed multi-line result is an error dialog", !!d && /failed/.test(d[1]) && d[3] === "error", JSON.stringify(env.calls));
  }
  {
    const env = makeEnv(); await ready(env);
    env.cfg.run = { ok: true, result: null };
    await submit(env, "/disk");
    check("an empty result still says it finished", env.toasts.some((t) => /\/disk done/.test(t[1])), JSON.stringify(env.toasts));
  }

  // ---- built-ins still behave ---------------------------------------------------------
  {
    const env = makeEnv(); await ready(env);
    check("a plain prose message is untouched", (await submit(env, "hello")).status === "passthrough");
    check("an unknown /word that is not near anything is untouched", (await submit(env, "/qwertyuiop")).status === "passthrough");
    check("// still escapes a tool command's name", (await submit(env, "//disk is full")).status === "passthrough" && env.input.value === "/disk is full");
    check("a path starting with a tool command's name is prose", (await submit(env, "/disk/x is odd")).status === "passthrough");
    check("no request was made for any of that", !urls(env, /prepare|\/run$/).length);
  }
  {
    // Tool installed after the palette loaded: picked up on the next look once the 20 s throttle passes.
    const env = makeEnv({ listing: { commands: [] } }); await ready(env);
    check("with none installed, /disk is just unknown", env.S.toolCmd("disk") === null);
    env.cfg.listing = { commands: COMMANDS };
    const changed = await env.S.loadToolCommands(true);
    check("a forced reload picks the new tool up and says it changed", changed === true && env.S.toolCmd("disk") !== null);
    check("a second reload with nothing new says so", (await env.S.loadToolCommands(true)) === false);
    env.cfg.listing = { commands: [] };
    await env.S.loadToolCommands(true);
    check("a tool removed since is gone", env.S.toolCmd("disk") === null);
    const before = urls(env, /^\/api\/tool-commands$/).length;
    await env.S.loadToolCommands();
    await env.S.loadToolCommands();
    check("unforced reloads inside the throttle make no request", urls(env, /^\/api\/tool-commands$/).length === before);
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
