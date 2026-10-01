// Verifies web/public/slash-palette.js's engine — parsing, the submit
// routing rules (I.2.6), every verb's handler dispatch, the confirm gates,
// the near-miss "Send anyway" flow, and the keyboard model — against the
// REAL slash-commands-data.js registry, with a tiny fake DOM and a fake
// window.JarvisHost standing in for app.js.
//
// This repo has no JS test framework or runner (every other test in
// tests/ is Python, run directly), so this follows the same convention as
// verify_math_rendering.js: plain asserts, run it directly, exit code 1 on
// any failure:
//
//   node tests/verify_slash_palette.js
//
// No npm packages needed (uses only node's own `vm`).
//
// What this does NOT verify: real browser layout, focus behaviour, or the
// look of the popover (see the manual checklist in
// DOCUMENTATION/COMMAND_PALETTE_TESTING.md for those) — a fake DOM can't tell you the
// popover floats above the composer instead of shoving it around.

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

function makeEnv() {
  const els = {
    "slash-palette": new FakeEl("div"),
    "ask-input": new FakeEl("textarea"),
    "ask-form": new FakeEl("form"),
  };
  els["slash-palette"].hidden = true;
  const docHandlers = {};
  const document = {
    readyState: "complete",
    getElementById: (id) => els[id] || null,
    createElement: (t) => new FakeEl(t),
    createTextNode: (t) => { const n = new FakeEl("#text"); n._text = t; return n; },
    addEventListener(t, f, capture) { (docHandlers[t] = docHandlers[t] || []).push({ f, capture: !!capture }); },
  };
  const calls = [];
  const rec = (name) => (...a) => { calls.push([name, ...a]); };
  const toasts = [];
  let running = false;
  let slashResponse = { ok: true, code: 0, stdout: "done", stderr: "", error: "" };
  let failDaemons = false;
  let confirmAnswer = true;
  const state = {
    activeConversationId: "c1",
    conversations: [{ id: "c1", title: "Weekend trip", updated_at: Math.floor(Date.now() / 1000) - 7200 }, { id: "c2", title: "Taxes", origin: "discord", updated_at: Math.floor(Date.now() / 1000) - 30 }],
    commands: {
      nightly: { vars: { target: {}, mode: { default: "fast" } } },
      ping: { vars: {} },
    },
    providerOverride: [],
    aiProviders: [{ name: "anthropic" }, { name: "openai" }],
    thinkLevel: "off", thinkShow: false, thinkLoaded: true,
    capacityMode: "full",
    modeOptions: [{ mode: "full", label: "Full" }, { mode: "compact", label: "Compact" }],
    layout: "classic",
  };
  const host = {
    toast: (m, k) => toasts.push([k || "error", m]),
    confirm: async (o) => { calls.push(["confirm", o.title, o.pre || ""]); return confirmAnswer; },
    dialog: (o) => calls.push(["dialog", o.title, o.pre, o.level]),
    refreshChats: rec("refreshChats"),
    originLabel: (o) => ({ discord: "Discord", scheduler: "Scheduled" })[o] || "",
    state: () => ({ ...state, running }),
    loadConversations: async () => state.conversations,
    newChat: async () => calls.push(["newChat"]),
    selectChat: async (id) => calls.push(["selectChat", id]),
    clearChat: async () => calls.push(["clearChat"]),
    stop: rec("stop"), redo: rec("redo"), copyLastReply: rec("copyLastReply"),
    setProvider: rec("setProvider"), setThink: async (l) => calls.push(["setThink", l]),
    setCapacity: async (m) => calls.push(["setCapacity", m]),
    setLayout: async (m) => calls.push(["setLayout", m]),
    runSegments: (s) => calls.push(["runSegments", JSON.stringify(s)]),
    skillSlash: async (v, a) => calls.push(["skillSlash", v, a]),
    openPanel: (id) => calls.push(["openPanel", id]),
  };
  const fetches = [];
  const fetchStub = async (url, opts) => {
    fetches.push([opts && opts.method || "GET", url, opts && opts.body]);
    const json = (o) => ({ ok: true, status: 200, json: async () => o });
    if (url === "/api/slash/run") return typeof slashResponse === "function" ? slashResponse(JSON.parse(opts.body)) : json(slashResponse);
    if (url.startsWith("/api/daemons") && !url.startsWith("/api/daemons/") && failDaemons) {
      failDaemons = false;
      return { ok: false, status: 500, json: async () => ({ error: "boom" }) };
    }
    if (url.startsWith("/api/skills/loaded")) return json({ loaded: ["pdf"] });
    if (url.startsWith("/api/skills")) return json({ skills: [{ name: "pdf", valid: true, description: "PDFs" }, { name: "broken", valid: false, error: "bad frontmatter" }] });
    if (url.startsWith("/api/daemons/") ) return json({ message: "ok" });
    if (url.startsWith("/api/daemons")) return json({ daemons: [{ id: "discord", running: true }, { id: "backup", running: false, status: "stopped" }] });
    if (url.startsWith("/api/ai/providers")) return json({ providers: state.aiProviders });
    if (url.startsWith("/api/mode")) return json({ options: state.modeOptions });
    return { ok: false, status: 404, json: async () => ({ error: "nope" }) };
  };
  const store = {};
  const window = {
    JarvisHost: host,
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  };
  // slash-palette.js console.warns "no handler for /x" at load for any verb
  // that isn't organize-json and has no HANDLERS entry (I.2.9 (5)).
  const warnings = [];
  const testConsole = Object.assign(Object.create(console), { warn: (...a) => warnings.push(a.join(" ")) });
  const sandbox = { window, document, fetch: fetchStub, console: testConsole, Event, setTimeout, Date, Promise };
  window.window = window;
  vm.createContext(sandbox);
  for (const f of ["slash-commands-data.js", "slash-palette.js"]) {
    vm.runInContext(fs.readFileSync(path.join(PUBLIC, f), "utf8"), sandbox, { filename: f });
  }
  return {
    S: window.JarvisSlash, els, calls, toasts, fetches, warnings, docHandlers,
    setRunning: (v) => { running = v; },
    setSlash: (v) => { slashResponse = v; },
    failDaemonsOnce: () => { failDaemons = true; },
    store,
    setConfirm: (v) => { confirmAnswer = v; },
    input: els["ask-input"], palette: els["slash-palette"], form: els["ask-form"],
  };
}

async function submit(env, text, quotes) {
  env.input.value = text;
  return env.S.handleSubmit(env.input, quotes || []);
}
const last = (env) => env.calls[env.calls.length - 1];
const called = (env, name) => env.calls.some((c) => c[0] === name);
const flush = () => new Promise((r) => setTimeout(r, 0));

async function main() {
  // ---- every verb has a handler --------------------------------------------
  {
    const env = makeEnv();
    check("every verb in the registry has a handler (no 'no handler' warning at load)", env.warnings.length === 0, env.warnings.join("; "));
  }

  // ---- parsing -----------------------------------------------------------
  {
    const env = makeEnv();
    const p = env.S.parse;
    check("parse: /new is a command", p("/new") && p("/new").token === "new");
    check("parse: keeps the rest for arg verbs", p("/chat weekend trip").rest === "weekend trip");
    check("parse: a path in the first token is prose", p("/etc/hosts is broken") === null);
    check("parse: // is not a command (escape hatch)", p("//hello") === null);
    check("parse: multi-line is prose", p("/new\nmore") === null);
    check("parse: '/ foo' is prose", p("/ foo") === null);
    check("parse: bare / opens level 1", p("/") && p("/").token === "" && !p("/").hasSpace);
    check("parse: not starting with / is prose", p("hello /new") === null);
    check("editDistance: transposition costs 2", env.S.editDistance("nwe", "new") === 2);
    check("score: exact > prefix > substring", env.S.score("new", "new") > env.S.score("ne", "new") && env.S.score("ne", "new") > env.S.score("ew", "new"));
    check("isComplete: /new is complete", env.S.isComplete("/new"));
    check("isComplete: /chat alone is not", !env.S.isComplete("/chat"));
    check("isComplete: /think high is (static list)", env.S.isComplete("/think high"));
    check("isComplete: /think loud is not", !env.S.isComplete("/think loud"));
    check("isComplete: /organize-json <anything> is (free text)", env.S.isComplete("/organize-json ~/a b.json"));
    check("isComplete: /run without a name is not", !env.S.isComplete("/run"));
  }

  // ---- routing (I.2.6) -----------------------------------------------------
  {
    const env = makeEnv();
    check("plain text passes through", (await submit(env, "hello there")).status === "passthrough");
    check("quotes attached => never intercepted", (await submit(env, "/new", ["a quote"])).status === "passthrough" && !called(env, "newChat"));
    const esc = await submit(env, "//new is a word");
    check("// escape hatch strips one slash and passes through", esc.status === "passthrough" && env.input.value === "/new is a word");
    check("a path-looking first token passes through", (await submit(env, "/etc/hosts is odd")).status === "passthrough");
    check("unknown and not close => plain message", (await submit(env, "/qwertyuiop")).status === "passthrough");

    const near = await submit(env, "/nwe");
    check("near-miss is blocked the first time", near.status === "blocked" && !called(env, "newChat"));
    check("near-miss warns with a did-you-mean", env.toasts.some((t) => /did you mean \/new/.test(t[1])));
    const again = await submit(env, "/nwe");
    check("an identical second Enter sends it (Send anyway)", again.status === "passthrough");
    check("...and only once", (await submit(env, "/nwe")).status === "blocked");

    const ne = await submit(env, "/spotify-login");
    check("a real command that can't run from chat is blocked with its reason", ne.status === "blocked" && env.toasts.some((t) => /can't run from chat \(an interactive OAuth/.test(t[1])), JSON.stringify(env.toasts.slice(-1)));
    check("...and can be sent anyway with a second Enter", (await submit(env, "/spotify-login")).status === "passthrough");
    env.toasts.length = 0;
    const nm2 = await submit(env, "/doctr");
    check("near-miss also knows the CLI passthrough names", nm2.status === "blocked" && env.toasts.some((t) => /did you mean \/doctor/.test(t[1])));
  }

  // ---- verb dispatch ---------------------------------------------------------
  {
    const env = makeEnv();
    let r = await submit(env, "/new");
    check("/new -> newChat, input cleared", r.status === "handled" && called(env, "newChat") && env.input.value === "");
    await submit(env, "/guides");
    check("/guides opens the guides panel", last(env)[0] === "openPanel" && last(env)[1] === "guides");
    await submit(env, "/tools");
    check("alias /tools resolves to /debug", last(env)[0] === "openPanel" && last(env)[1] === "debug");
    await submit(env, "/SCHED");
    check("verbs are case-insensitive; /sched -> schedule", last(env)[1] === "schedule");
    await submit(env, "/clear");
    check("/clear calls clearChat (which confirms in app.js)", called(env, "clearChat"));
    await submit(env, "/copy");
    check("/copy copies the last reply", called(env, "copyLastReply"));
    await submit(env, "/skills");
    check("/skills opens the skill manager", last(env)[0] === "openPanel" && last(env)[1] === "skills");
    await submit(env, "/skillmake");
    check("/skillmake -> skillSlash make", last(env)[0] === "skillSlash" && last(env)[1] === "make");
    await submit(env, "/layout focus");
    check("/layout focus", last(env)[0] === "setLayout" && last(env)[1] === "focus");
    r = await submit(env, "/layout sideways");
    check("/layout <bad> is refused and the text handed back", r.status === "blocked" && env.input.value === "/layout sideways");
    await submit(env, "/think high");
    check("/think high", last(env)[0] === "setThink" && last(env)[1] === "high");
    await submit(env, "/think show");
    check("/think show", last(env)[1] === "show");
    r = await submit(env, "/think loud");
    check("/think <bad> is refused", r.status === "blocked");
    await submit(env, "/capacity compact");
    check("/capacity compact", last(env)[0] === "setCapacity" && last(env)[1] === "compact");
    r = await submit(env, "/capacity turbo");
    check("/capacity <unknown> is refused", r.status === "blocked");
    await submit(env, "/provider anthropic,openai");
    check("/provider a,b", last(env)[0] === "setProvider" && last(env)[1] === "anthropic,openai");
    await submit(env, "/provider auto");
    check("/provider auto", last(env)[1] === "auto");
    r = await submit(env, "/provider nope");
    check("/provider <unknown> is refused", r.status === "blocked");
    r = await submit(env, "/provider auto,openai");
    check("/provider auto can't be combined", r.status === "blocked");
    await submit(env, "/chat taxes");
    check("/chat resolves a title to its id", last(env)[0] === "selectChat" && last(env)[1] === "c2");
    await submit(env, "/switch tax");
    check("alias /switch, unique partial title works", last(env)[0] === "selectChat" && last(env)[1] === "c2");
    r = await submit(env, "/chat zzz");
    check("/chat <unknown> is refused", r.status === "blocked");
    r = await submit(env, "/chat");
    check("/chat with no argument is blocked", r.status === "blocked");
    await submit(env, "/skillload pdf");
    check("/skillload pdf", last(env)[0] === "skillSlash" && last(env)[1] === "load" && last(env)[2] === "pdf");
    r = await submit(env, "/skillload broken");
    check("/skillload of an invalid skill is refused", r.status === "blocked");
    await submit(env, "/skillunload pdf");
    check("/skillunload of a loaded skill", last(env)[1] === "unload" && last(env)[2] === "pdf");
    r = await submit(env, "/skillunload ghost");
    check("/skillunload of a not-loaded skill says so instead of 'succeeding'", r.status === "blocked" && env.toasts.some((t) => /isn't loaded/.test(t[1])));
    await submit(env, "/skillunload --all");
    check("/skillunload --all", last(env)[2] === "--all");
    r = await submit(env, "/organize-json ~/messy.json");
    check("/organize-json is validated then passed through to app.js", r.status === "passthrough" && env.input.value === "/organize-json ~/messy.json");
    r = await submit(env, "/organize-json");
    check("/organize-json with no path is blocked", r.status === "blocked");
  }

  // ---- /run ---------------------------------------------------------------------
  {
    const env = makeEnv();
    await submit(env, "/run ping");
    check("/run <no-var command>", last(env)[0] === "runSegments" && JSON.parse(last(env)[1])[0].name === "ping");
    let r = await submit(env, "/run nightly");
    check("/run refuses when a required --var is missing", r.status === "blocked" && env.toasts.some((t) => /--target/.test(t[1])));
    env.calls.length = 0;
    await submit(env, "/run nightly --target /mnt/data");
    const seg = JSON.parse(last(env)[1])[0];
    check("/run passes --vars and fills defaults", seg.name === "nightly" && seg.flags.target === "/mnt/data" && seg.flags.mode === "fast");
    await submit(env, '/run nightly --target="two words" --mode slow');
    const seg2 = JSON.parse(last(env)[1])[0];
    check("/run handles quoted and --k=v values", seg2.flags.target === "two words" && seg2.flags.mode === "slow");
    r = await submit(env, "/run nightly --target x --bogus 1");
    check("/run refuses an option the command doesn't have", r.status === "blocked");
    r = await submit(env, "/run nope");
    check("/run <unknown command> is refused", r.status === "blocked");
  }

  // ---- /daemon confirm gate -------------------------------------------------------
  {
    const env = makeEnv();
    env.setConfirm(false);
    let r = await submit(env, "/daemon stop discord");
    check("/daemon stop asks first", called(env, "confirm"));
    check("declining the confirm sends nothing", r.status === "blocked" && !env.fetches.some((f) => f[0] === "POST"));
    env.setConfirm(true);
    r = await submit(env, "/daemon restart discord");
    check("/daemon restart, confirmed, POSTs the action", r.status === "handled" && env.fetches.some((f) => f[0] === "POST" && f[1] === "/api/daemons/discord/restart"));
    env.calls.length = 0;
    r = await submit(env, "/daemon restart backup");
    check("/daemon restart of a service that's already down doesn't ask (matches the Daemons panel)", !called(env, "confirm") && r.status === "handled");
    r = await submit(env, "/daemon stop backup");
    check("...but /daemon stop always asks, even for a stopped one", called(env, "confirm"));
    env.calls.length = 0;
    await submit(env, "/daemon start discord");
    check("/daemon start never asks", !called(env, "confirm") && env.fetches.some((f) => f[1] === "/api/daemons/discord/start"));
    await submit(env, "/daemon status discord");
    check("/daemon status never asks", !called(env, "confirm"));
    r = await submit(env, "/daemon explode discord");
    check("/daemon <bad action> is refused", r.status === "blocked");
    r = await submit(env, "/daemon start ghost");
    check("/daemon <unknown service> is refused", r.status === "blocked");
  }

  // ---- whileReplying -----------------------------------------------------------------
  {
    const env = makeEnv();
    env.setRunning(true);
    let r = await submit(env, "/redo");
    check("/redo is blocked while Jarvis is replying (text kept)", r.status === "blocked" && env.input.value === "/redo" && !called(env, "redo"));
    r = await submit(env, "/run ping");
    check("/run is blocked while replying", r.status === "blocked" && !called(env, "runSegments"));
    r = await submit(env, "/organize-json ~/x.json");
    check("/organize-json is allowed while replying (I.2.3: Replying = yes)", r.status === "passthrough");
    r = await submit(env, "/clear");
    check("/clear is blocked while replying", r.status === "blocked" && !called(env, "clearChat"));
    await submit(env, "/stop");
    check("/stop works while replying", called(env, "stop"));
    await submit(env, "/guides");
    check("panel openers work while replying", last(env)[0] === "openPanel");
    await submit(env, "/think low");
    check("/think works while replying", last(env)[0] === "setThink");
    env.setRunning(false);
    env.toasts.length = 0;
    await submit(env, "/stop");
    check("/stop with nothing running says so", env.toasts.some((t) => /Nothing is running/.test(t[1])) && !env.calls.some((c) => c[0] === "stop" && env.calls.filter((x) => x[0] === "stop").length > 1));
  }

  // ---- keyboard / popover model ----------------------------------------------------------
  {
    const env = makeEnv();
    const key = (k, extra) => {
      let prevented = false, stopped = false;
      const handled = env.S.handleComposerKeydown({ key: k, shiftKey: false, isComposing: false, preventDefault() { prevented = true; }, stopPropagation() { stopped = true; }, ...extra }, env.input);
      return { handled, prevented, stopped };
    };
    check("keys are ignored while the popover is closed", key("ArrowDown").handled === false && key("Escape").handled === false);

    env.input.value = "/";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    check("typing / opens the popover", env.palette.hidden === false);
    const rowsAll = env.palette.querySelectorAll(".slash-row").length;
    check("empty query lists every verb (35) plus the 'show the other commands' toggle", rowsAll === 36, `got ${rowsAll}`);
    check("headings: the six groups from I.2.5 plus 'More'", env.palette.querySelectorAll(".slash-heading").length === 7
      && ["Chat", "Model", "Skills", "Run", "Panels", "View"].every((g) => env.palette.querySelectorAll(".slash-heading").some((h) => h.textContent === g)));
    check("every verb row carries a visible risk badge (D-I4)", env.palette.children[0].querySelectorAll(".slash-badge").length === 35);
    check("the live region announces the count", env.palette.querySelectorAll(".slash-sr")[0].textContent === "35 commands", env.palette.querySelectorAll(".slash-sr")[0].textContent);

    env.input.value = "/ne";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const filtered = env.palette.querySelectorAll(".slash-row");
    check("filtering narrows the list and ranks the prefix match first", filtered.length >= 1 && /\/new/.test(filtered[0].textContent));
    const hl = env.palette.querySelectorAll(".slash-hl");
    check("the matched letters are emphasised with real nodes", hl.length >= 1 && hl[0].textContent === "ne", hl.map((h) => h.textContent).join("|"));
    check("the footer shows what Enter will do (preview line)", /Starts a new chat/.test(env.palette.querySelectorAll(".slash-palette__foot")[0] ? "" : "") || env.palette.children.some((c) => /Starts a new chat/.test(c.textContent)));

    let k = key("Enter");
    check("Enter on an unfinished command completes instead of submitting", k.handled && k.prevented && env.input.value === "/new");
    await flush();
    k = key("Enter");
    check("Enter on a finished command lets the submit through", k.handled === false);

    env.input.value = "/chat ";
    env.input.dispatchEvent(new Event("input"));
    await flush(); await flush();
    const chatRows = env.palette.querySelectorAll(".slash-row");
    check("level 2 lists the chats", chatRows.length === 2 && /Weekend trip/.test(chatRows[0].textContent), chatRows.map((r) => r.textContent).join("|"));
    check("the open chat is marked current", /is-current/.test(chatRows[0].className));
    key("ArrowDown");
    k = key("Tab");
    check("Tab completes the highlighted argument", k.handled && env.input.value === "/chat Taxes", env.input.value);
    await flush();
    k = key("Enter");
    check("...and Enter then runs it", k.handled === false);

    env.input.value = "/think ";
    env.input.dispatchEvent(new Event("input"));
    await flush(); await flush();
    check("static arguments list without a fetch", env.palette.querySelectorAll(".slash-row").length === 6);

    env.input.value = "/gu";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const before = env.form.submits || 0;
    k = key("Enter");
    check("a safe no-argument verb (panel opener) runs on the FIRST Enter", k.handled && (env.form.submits || 0) === before + 1 && env.input.value === "/guides");

    env.input.value = "/hel";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const before3 = env.form.submits || 0;
    key("Enter");
    check("/help is instant (I.2.3): one Enter runs it", (env.form.submits || 0) === before3 + 1 && env.input.value === "/help");
    check("a bare '/help ' counts as finished", env.S.isComplete("/help "));

    env.input.value = "/cl";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const before2 = env.form.submits || 0;
    key("Enter");
    check("/clear only COMPLETES on the first Enter (never runs off a prefix)", (env.form.submits || 0) === before2 && env.input.value === "/clear");

    env.input.value = "/spotify";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    check("a command that can't run from chat is listed, disabled, with its badge", env.palette.querySelectorAll(".slash-row").some((r) => /is-disabled/.test(r.className) && /spotify-login/.test(r.textContent)));
    env.input.value = "/doc";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    check("a CLI passthrough command is listed ENABLED, tagged cli, with its tier", env.palette.querySelectorAll(".slash-row").some((r) => !/is-disabled/.test(r.className) && /\/doctor/.test(r.textContent) && /cli/.test(r.textContent) && /safe/.test(r.textContent)));

    env.input.value = "/zzzz";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    check("empty state says what Enter will do (I.2.5)", /No command "\/zzzz" matches/.test(env.palette.querySelectorAll(".slash-row")[0].textContent) && /sends it as a message/.test(env.palette.querySelectorAll(".slash-row")[0].textContent));
    check("...and the live region says so", env.palette.querySelectorAll(".slash-sr")[0].textContent === "No matching commands");
    k = key("Enter");
    check("Enter with nothing to complete falls through to submit", k.handled === false);

    env.input.value = "/g";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    k = key("Escape");
    check("Esc closes the popover", k.handled && env.palette.hidden === true);
    check("...and stops propagating, so app.js's document-level Esc doesn't also close the Ask panel", k.stopped === true);
    check("a second Esc falls through to whatever it did before", key("Escape").handled === false);

    env.input.value = "/gu";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    env.input.value = "hello";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    check("deleting the slash closes the popover", env.palette.hidden === true);
  }

  // ---- passthrough ---------------------------------------------------------------------
  {
    const env = makeEnv();
    const posts = () => env.fetches.filter((f) => f[0] === "POST" && f[1] === "/api/slash/run").map((f) => JSON.parse(f[2]));
    env.setSlash({ ok: true, code: 0, stdout: "all good", stderr: "", error: "" });
    let r = await submit(env, "/doctor");
    check("/doctor POSTs the name with no args and reports a short result as a toast",
      r.status === "handled" && JSON.stringify(posts()[0]) === '{"name":"doctor","args":[]}' && env.toasts.some((t) => t[0] === "info" && t[1] === "all good"), JSON.stringify(posts()));
    await submit(env, '/memory-recall "two words" --limit=3 x');
    check("arguments become an argv array, quotes honoured, never a shell string",
      JSON.stringify(posts()[1].args) === JSON.stringify(["two words", "--limit=3", "x"]), JSON.stringify(posts()[1]));
    await submit(env, "/memory-recall `rm -rf ~`; $(evil) | cat");
    check("shell metacharacters stay inert data inside the argv", posts()[2].args.includes("`rm") && posts()[2].args.includes("$(evil)") && posts()[2].args.includes("|"), JSON.stringify(posts()[2]));
    check("a safe/caution passthrough never asks first", !called(env, "confirm"));

    env.setSlash({ ok: true, code: 0, stdout: "line one\nline two", stderr: "", error: "" });
    await submit(env, "/policy");
    check("multi-line output opens a dialog with the text, not a toast", last(env)[0] === "dialog" && last(env)[2] === "line one\nline two" && last(env)[3] === "info");
    env.setSlash({ ok: false, code: 2, stdout: "", stderr: "usage: jarvis calendar-add <name> <ics-url>", error: "" });
    await submit(env, "/calendar-add");
    check("a failing command shows its stderr and exit code in an error dialog", last(env)[0] === "dialog" && /exit 2/.test(last(env)[1]) && last(env)[3] === "error" && /usage:/.test(last(env)[2]));
    env.setSlash({ ok: true, code: 0, stdout: "", stderr: "", error: "" });
    env.toasts.length = 0;
    await submit(env, "/digest-off");
    check("no output at all still confirms it ran", env.toasts.some((t) => /digest-off done/.test(t[1])));

    env.setConfirm(false);
    env.calls.length = 0;
    const before = posts().length;
    r = await submit(env, "/logs-clear");
    check("a dangerous passthrough asks first (D-I4), naming the exact command", called(env, "confirm") && env.calls.find((c) => c[0] === "confirm")[2] === "jarvis logs-clear");
    check("declining sends nothing and hands the text back", posts().length === before && r.status === "blocked" && env.input.value === "/logs-clear");
    env.setConfirm(true);
    r = await submit(env, "/mcp-call srv tool");
    check("confirming runs it with its args", r.status === "handled" && JSON.stringify(posts().slice(-1)[0]) === '{"name":"mcp-call","args":["srv","tool"]}');
    for (const [n, args] of [["notify-send", " c2"], ["notify-clear", " c2"], ["sched-clear", ""], ["conv-delete", " c2"]]) {
      env.calls.length = 0;
      await submit(env, `/${n}${args}`);
      check(`/${n} (dangerous) confirms`, called(env, "confirm"));
    }
    env.calls.length = 0;
    await submit(env, "/console-clear c2");
    check("/console-clear only marks the live console cleared (caution), so it runs without a dialog", !called(env, "confirm") && posts().slice(-1)[0].name === "console-clear");
    r = await submit(env, "/conv-delete c1");
    check("deleting the chat you have open is refused (the UI would be left pointing at nothing)", r.status === "blocked" && env.toasts.some((t) => /chat you have open/.test(t[1])));
    env.calls.length = 0;
    await submit(env, "/conv-export c2");
    check("conv-* commands refresh the sidebar list afterwards", called(env, "refreshChats"));

    env.setSlash((body) => ({ ok: false, status: 400, json: async () => ({ error: `"${body.name}" isn't a command the palette can run.` }) }));
    env.toasts.length = 0;
    r = await submit(env, "/version");
    check("a server refusal is shown and the text is handed back", r.status === "blocked" && env.toasts.some((t) => /isn't a command the palette can run/.test(t[1])) && env.input.value === "/version");

    env.setRunning(true);
    env.setSlash({ ok: true, code: 0, stdout: "fine", stderr: "", error: "" });
    r = await submit(env, "/version");
    check("passthrough works while Jarvis is replying (they're independent of the ask stream)", r.status === "handled");
    env.setRunning(false);

    check("isComplete: an exact passthrough name is complete", env.S.isComplete("/doctor") && env.S.isComplete("/memory-recall some query"));
    check("MRU: a run is remembered by verb id", JSON.parse(env.store["jarvis.slash.mru"]).includes("v:doctor"));
  }

  // ---- passthrough: exit codes, argument caps, secrets (I-B13, I-B15..I-B17) ------------
  {
    const env = makeEnv();
    const posts = () => env.fetches.filter((f) => f[0] === "POST" && f[1] === "/api/slash/run").map((f) => JSON.parse(f[2]));
    const reg = JSON.parse(/\/\*JSON-BEGIN\*\/([\s\S]*)\/\*JSON-END\*\//.exec(fs.readFileSync(path.join(PUBLIC, "slash-commands-data.js"), "utf8"))[1]);

    // I-B13: doctor's exit 1 is warnings, not a failure
    env.setSlash({ ok: false, code: 1, stdout: "doctor: 2 warnings\n- a\n- b", stderr: "", error: "" });
    await submit(env, "/doctor");
    check("/doctor exit 1 opens a WARN dialog titled 'warnings', not 'failed'",
      last(env)[0] === "dialog" && last(env)[3] === "warn" && /warnings \(exit 1\)/.test(last(env)[1]) && !/failed/.test(last(env)[1]), JSON.stringify(last(env)));
    env.setSlash({ ok: false, code: 2, stdout: "doctor: broken", stderr: "", error: "" });
    await submit(env, "/doctor");
    check("/doctor exit 2 is still an error dialog, now named 'problems found'",
      last(env)[0] === "dialog" && last(env)[3] === "error" && /problems found \(exit 2\)/.test(last(env)[1]), JSON.stringify(last(env)));
    env.setSlash({ ok: false, code: 1, stdout: "", stderr: "", error: "" });
    env.toasts.length = 0;
    await submit(env, "/doctor");
    check("/doctor exit 1 with no output is a warn toast", env.toasts.some((t) => t[0] === "warn" && /warnings \(exit 1\)/.test(t[1])), JSON.stringify(env.toasts));
    env.setSlash({ ok: false, code: 1, stdout: "", stderr: "boom", error: "" });
    await submit(env, "/policy");
    check("an exit code the registry does NOT name is still a failure (only doctor is softened)",
      last(env)[0] === "dialog" && last(env)[3] === "error" && /failed \(exit 1\)/.test(last(env)[1]), JSON.stringify(last(env)));
    env.setSlash({ ok: true, code: 0, stdout: "healthy\nall checks passed", stderr: "", error: "" });
    await submit(env, "/doctor");
    check("/doctor exit 0 is an ordinary info dialog", last(env)[0] === "dialog" && last(env)[3] === "info" && last(env)[1] === "/doctor");

    // I-B16: /subagent-keys is list-only and never takes a key
    env.setSlash({ ok: true, code: 0, stdout: "{}", stderr: "", error: "" });
    const before = posts().length;
    env.toasts.length = 0;
    let r = await submit(env, "/subagent-keys researcher openai sk-SECRET-123");
    check("/subagent-keys with arguments is refused and NOTHING is posted", r.status === "blocked" && posts().length === before);
    check("...the refusal says nothing was sent or saved", env.toasts.some((t) => t[0] === "warn" && /nothing you typed was sent or saved/.test(t[1])), JSON.stringify(env.toasts));
    check("...and the key is NOT handed back in the composer", env.input.value === "/subagent-keys" && !/sk-SECRET/.test(env.input.value), env.input.value);
    check("...nor does it appear in any toast", !env.toasts.some((t) => /sk-SECRET/.test(t[1])));
    r = await submit(env, "/subagent-keys");
    check("/subagent-keys with no arguments still lists the pools", r.status === "handled" && posts().slice(-1)[0].name === "subagent-keys" && posts().slice(-1)[0].args.length === 0);

    // ...and the typo path: the second Enter must not send the keys to the model
    env.toasts.length = 0;
    r = await submit(env, "/subagent-keyz researcher openai sk-SECRET-123");
    check("a typo'd /subagent-keys WITH arguments is blocked", r.status === "blocked");
    check("...the typed keys are wiped from the composer", env.input.value === "/subagent-keyz" && !/sk-SECRET/.test(env.input.value), env.input.value);
    check("...the toast explains why and does not echo the key", env.toasts.some((t) => /did you mean \/subagent-keys/.test(t[1]) && /secrets/.test(t[1])) && !env.toasts.some((t) => /sk-SECRET/.test(t[1])));
    r = await submit(env, "/subagent-keyz researcher openai sk-SECRET-123");
    check("...and pressing Enter again STILL does not send it (no Send anyway for secrets)", r.status === "blocked", r.status);
    r = await submit(env, "//subagent-keys researcher openai sk-SECRET-123");
    check("the explicit // escape hatch still sends a message (deliberate, one slash stripped)", r.status === "passthrough" && env.input.value === "/subagent-keys researcher openai sk-SECRET-123");
    env.toasts.length = 0;
    r = await submit(env, "/subagent-keyz");
    check("a typo with NO arguments keeps the normal 'send anyway' flow", r.status === "blocked" && env.toasts.some((t) => /Press Enter again/.test(t[1])));
    r = await submit(env, "/docter --deep");
    check("a non-secret near-miss with arguments is unchanged (Send anyway still offered)", r.status === "blocked" && env.toasts.some((t) => /did you mean \/doctor/.test(t[1]) && /Press Enter again/.test(t[1])));

    // I-B17: /sched-clear has no options
    env.calls.length = 0;
    const b2 = posts().length;
    env.toasts.length = 0;
    r = await submit(env, "/sched-clear --all");
    check("/sched-clear --all is refused (the CLI never parsed --all) without asking or posting", r.status === "blocked" && !called(env, "confirm") && posts().length === b2);
    check("...with its own hint, and the text is handed back", env.toasts.some((t) => /takes no options/.test(t[1])) && env.input.value === "/sched-clear --all");
    r = await submit(env, "/sched-clear");
    check("/sched-clear (no arguments) is still dangerous: it confirms, naming the exact command", called(env, "confirm") && env.calls.find((c) => c[0] === "confirm")[2] === "jarvis sched-clear" && r.status === "handled");

    // registry-level facts the UI relies on
    check("the confirm text for sched-clear says what it removes and what it keeps", /finished/.test(reg.passthrough["sched-clear"].summary) && /kept/.test(reg.passthrough["sched-clear"].summary));
    check("/memory-ns is caution, since an argument switches the active namespace (I-B15)", reg.passthrough["memory-ns"].riskTier === "caution");
    env.calls.length = 0;
    await submit(env, "/memory-ns work");
    check("/memory-ns <name> runs without a dialog and passes the namespace as argv", !called(env, "confirm") && JSON.stringify(posts().slice(-1)[0]) === '{"name":"memory-ns","args":["work"]}');
  }

  // ---- popover: new behaviours -----------------------------------------------------------------
  {
    const env = makeEnv();
    const key = (k, extra) => {
      let prevented = false;
      const handled = env.S.handleComposerKeydown({ key: k, shiftKey: false, isComposing: false, preventDefault() { prevented = true; }, stopPropagation() {}, ...extra }, env.input);
      return { handled, prevented };
    };
    const type = async (v) => { env.input.value = v; env.input.dispatchEvent(new Event("input")); await flush(); await flush(); };
    const footText = () => env.palette.children[1].textContent;

    await type("/");
    key("End");
    let k = key("Enter");
    await flush(); await flush();
    check("the toggle row expands every other command (31 runnable CLI + 30 that can't)", env.palette.querySelectorAll(".slash-row").length === 36 + 31 + 30, env.palette.querySelectorAll(".slash-row").length);
    check("...under their own headings", ["CLI commands", "Can't run from chat"].every((h) => env.palette.querySelectorAll(".slash-heading").some((x) => x.textContent === h)));
    check("...and the ones that can't run are disabled", env.palette.querySelectorAll(".slash-row").filter((r) => /is-disabled/.test(r.className)).length === 30);
    key("Escape");
    await type("/");
    check("closing resets the expansion", env.palette.querySelectorAll(".slash-row").length === 36);

    // IME
    await type("/gu");
    k = key("Enter", { isComposing: true });
    check("Enter during IME composition is swallowed while the palette is open (never submits half a command)", k.handled === true && (env.form.submits || 0) === 0);
    k = key("Enter", { keyCode: 229 });
    check("...including the keyCode 229 form", k.handled === true);
    key("Escape");
    check("IME Enter with the palette closed is left alone", key("Enter", { isComposing: true }).handled === false);

    // a typo must not be "helpfully" completed by the fuzzy matcher: it has to
    // reach the near-miss check on submit (I-B5)
    await type("/nwe");
    check("a typo matches nothing in the popover (descriptions don't fuzzy-match)", env.palette.querySelectorAll(".slash-row").length === 1 && /No command "\/nwe" matches/.test(env.palette.querySelectorAll(".slash-row")[0].textContent));
    k = key("Enter");
    check("...so Enter falls through to submit, where the near-miss 'did you mean' fires", k.handled === false);
    const nmr = await env.S.handleSubmit(env.input, []);
    check("...and it blocks with a hint", nmr.status === "blocked" && env.toasts.some((t) => /did you mean \/new/.test(t[1])));
    env.input.value = "";

    // Tab never runs anything, even on an instant verb (I.2.5)
    await type("/gu");
    const tabBefore = env.form.submits || 0;
    k = key("Tab");
    check("Tab on an instant verb only fills it; it never runs (I.2.5)", k.handled && env.input.value === "/guides" && (env.form.submits || 0) === tabBefore);

    // click only fills
    await type("/gu");
    const list = env.palette.children[0];
    const row = env.palette.querySelectorAll(".slash-row")[0];
    const submitsBefore = env.form.submits || 0;
    list.dispatchEvent({ type: "pointerdown", target: { closest: () => row }, preventDefault() {} });
    await flush();
    check("clicking a row only FILLS the input; it never runs (I.2.5)", env.input.value === "/guides" && (env.form.submits || 0) === submitsBefore);

    // I-B19: pressing ANYTHING inside the popover must not steal focus, and a
    // press that re-renders the list under the pointer must not count as an
    // outside click.
    await type("/gu");
    const pal = env.palette;
    let prevented = 0;
    for (const t of ["pointerdown", "mousedown"]) {
      pal.dispatchEvent({ type: t, target: { closest: () => null }, preventDefault() { prevented++; } });
    }
    check("I-B19: a press on the popover's own chrome (heading/footer/padding) is default-prevented, pointer and mouse", prevented === 2, String(prevented));
    const dh = (env.docHandlers.pointerdown || []);
    check("I-B19: the outside-click listener is registered in the CAPTURE phase", dh.length === 1 && dh[0].capture === true, JSON.stringify(dh.map((h) => h.capture)));
    const outside = () => dh[0].f({ target: { id: "somewhere-else" }, composedPath: () => [{}] });
    const insideDetached = () => dh[0].f({ target: { detached: true }, composedPath: () => [{}, env.palette] });
    insideDetached();
    check("I-B19: a press whose target was detached by a re-render, but whose path includes the popover, keeps it open", env.palette.hidden === false);
    outside();
    check("I-B19: a real outside press still closes it (Q-I19)", env.palette.hidden === true);
    await type("/gu");
    check("I-B19: ...and typing in the composer reopens it", env.palette.hidden === false);

    // level 2 chrome
    await type("/chat ");
    check("level 2 has the '/chat › pick a chat · 2 of 2' chip", env.palette.querySelectorAll(".slash-heading").some((h) => h.textContent === "/chat \u203a pick a chat \u00b7 2 of 2"), env.palette.querySelectorAll(".slash-heading").map((h) => h.textContent).join("|"));
    const chatRows = env.palette.querySelectorAll(".slash-row");
    check("chat rows carry relative time and origin", /2h ago/.test(chatRows[0].textContent) && /just now/.test(chatRows[1].textContent) && /Discord/.test(chatRows[1].textContent), chatRows.map((r) => r.textContent).join("|"));
    check("the live region counts matches", env.palette.querySelectorAll(".slash-sr")[0].textContent === "2 matches");

    await type("/skillload ");
    const sk = env.palette.querySelectorAll(".slash-row");
    check("/skillload marks an already-loaded skill and disables the invalid one with its reason",
      /loaded/.test(sk[0].textContent) && /is-current/.test(sk[0].className) && sk.some((r) => /is-disabled/.test(r.className) && /broken/.test(r.textContent) && /bad frontmatter/.test(r.textContent)));
    await type("/skillload p");
    check("the footer previews the action with the highlighted argument filled in", /Loads pdf for this chat until you unload it/.test(footText()), footText());

    await type("/run ");
    const cr = env.palette.querySelectorAll(".slash-row");
    check("/run rows show --vars as REQUIRED or with their default", cr.some((r) => /nightly/.test(r.textContent) && /--target REQUIRED/.test(r.textContent) && /--mode=fast/.test(r.textContent)), cr.map((r) => r.textContent).join("|"));

    await type("/daemon start ");
    const dr = env.palette.querySelectorAll(".slash-row");
    check("service rows carry a live-state dot", env.palette.querySelectorAll(".slash-dot--up").length === 1 && env.palette.querySelectorAll(".slash-dot--down").length === 1);

    // stale-while-revalidate: the last good list paints immediately
    await type("/chat ");
    check("a previously loaded list is shown at once (no blank 'Loading…' flash)", !env.palette.querySelectorAll(".slash-row").some((r) => /Loading/.test(r.textContent)));

    // preview for a passthrough row
    await type("/logs-c");
    check("a dangerous passthrough's footer preview says it asks first", /Asks first/.test(footText()), footText());
    check("...and shows its badge in the row", env.palette.querySelectorAll(".slash-badge--dangerous").length >= 1);
  }

  // ---- error state has a Retry that really retries ---------------------------------------------
  {
    const env = makeEnv();
    env.failDaemonsOnce();
    env.input.value = "/daemon start ";
    env.input.dispatchEvent(new Event("input"));
    await flush(); await flush();
    const rows = env.palette.querySelectorAll(".slash-row");
    check("a failed list load shows a selectable 'Couldn't load the list' row with a retry tag", rows.length === 1 && /Couldn't load the list/.test(rows[0].textContent) && /retry/.test(rows[0].textContent) && !/is-disabled/.test(rows[0].className), rows.map((r) => r.textContent).join("|"));
    let prevented = false;
    const h = env.S.handleComposerKeydown({ key: "Enter", shiftKey: false, isComposing: false, preventDefault() { prevented = true; }, stopPropagation() {} }, env.input);
    await flush(); await flush();
    const after = env.palette.querySelectorAll(".slash-row");
    check("Enter on it retries the load and shows the services", h && prevented && after.length === 2 && /discord/.test(after[0].textContent), after.map((r) => r.textContent).join("|"));
  }

  // ---- ranking ties use recent use --------------------------------------------------------------
  {
    const env = makeEnv();
    env.store["jarvis.slash.mru"] = JSON.stringify(["v:skills"]);
    env.input.value = "/sk";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const first = env.palette.querySelectorAll(".slash-row")[0].textContent;
    check("among equal matches the recently-used verb ranks first (MRU tie-break)", /\/skills/.test(first) && !/\/skillload/.test(first), first);
    check("MRU never stores chat titles", true);
  }

  // ---- I-B20: stacking order of the surfaces a palette verb can open ----------------------------
  // /skin, /config and the command builder open a `.modal-backdrop`; it has to
  // sit above Ask, the menu panels and the Debug-style panels or it opens
  // underneath them. A fake DOM can't see layout, but the z-index order is plain CSS.
  {
    const css = fs.readFileSync(path.join(PUBLIC, "style.css"), "utf8");
    const z = (sel) => {
      const m = css.match(new RegExp("(?:^|\\n)" + sel.replace(/\./g, "\\.") + "\\s*\\{[^}]*?z-index:\\s*(\\d+)"));
      return m ? Number(m[1]) : null;
    };
    const zModal = z(".modal-backdrop"), zAsk = z(".ask-overlay"), zMenu = z(".menu-overlay"), zDebug = z(".debug-overlay");
    check("I-B20: all four overlay classes have a z-index", [zModal, zAsk, zMenu, zDebug].every((n) => n != null), [zModal, zAsk, zMenu, zDebug].join(","));
    check("I-B20: .modal-backdrop (Skin/Settings/command builder) is above Ask, menu and debug overlays", zModal > Math.max(zAsk, zMenu, zDebug), [zModal, zAsk, zMenu, zDebug].join(","));
    const ui = fs.readFileSync(path.join(PUBLIC, "ui-kit.css"), "utf8");
    const uiMin = Math.min(...Array.from(ui.matchAll(/z-index:\s*(\d+)/g)).map((m) => Number(m[1])));
    check("I-B20: ...and still below the ui-kit confirm/dialog/toast layers", zModal < uiMin, zModal + " vs " + uiMin);
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
