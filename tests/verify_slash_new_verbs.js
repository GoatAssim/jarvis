// Verifies the four "/" verbs added for the deferred I.2 list -- /search, /export,
// /remind and /persona -- plus the two argument forms added after them, `/copy N` and
// `/backlog add <title>`, against the REAL registry and engine, with the same fake DOM
// and fake JarvisHost as verify_slash_palette.js (the harness below is copied from it,
// as the other verify_*.js files do).
//
//   node tests/verify_slash_new_verbs.js
//
// Not covered (needs a browser): the real Skin save path behind host.setPersona.

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
  let personaList = [{ id: "friday", name: "Friday", attitudeLabel: "Dry" }, { id: "edith", name: "Edith", attitudeLabel: "Warm" }, { id: "ember", name: "Ember", attitudeLabel: "" }];
  let personaResult = { ok: true };
  const favCommands = new Set();      // L.14.2: what the Commands list has starred
  let daemonFavs = [];                // L.13: ids in /api/favorite-daemons
  const state = {
    activeConversationId: "c1",
    conversations: [{ id: "c1", title: "Weekend trip", updated_at: Math.floor(Date.now() / 1000) - 7200 }, { id: "c2", title: "Taxes", origin: "discord", updated_at: Math.floor(Date.now() / 1000) - 30 }],
    commands: {
      nightly: { vars: { target: {}, mode: { default: "fast" } }, categories: ["backup", "ops"] },
      ping: { vars: {}, categories: ["Utility"] },
      deploy: { vars: {}, categories: ["ops"] },
      plain: { vars: {} },
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
    personas: () => personaList,
    setPersona: async (id) => { calls.push(["setPersona", id]); return personaResult; },
    stop: rec("stop"), redo: rec("redo"), copyLastReply: rec("copyLastReply"),
    setProvider: rec("setProvider"), setThink: async (l) => calls.push(["setThink", l]),
    setCapacity: async (m) => calls.push(["setCapacity", m]),
    setLayout: async (m) => calls.push(["setLayout", m]),
    runSegments: (s) => calls.push(["runSegments", JSON.stringify(s)]),
    skillSlash: async (v, a) => calls.push(["skillSlash", v, a]),
    openPanel: (id) => calls.push(["openPanel", id]),
    favoriteCommandNames: () => Array.from(favCommands),
    toggleFavoriteCommand: (name) => {
      if (!Object.prototype.hasOwnProperty.call(state.commands, name)) return null;
      if (favCommands.has(name)) favCommands.delete(name); else favCommands.add(name);
      calls.push(["toggleFavoriteCommand", name]);
      return favCommands.has(name);
    },
  };
  const fetches = [];
  let extraFetch = null;
  const fetchStub = async (url, opts) => {
    fetches.push([opts && opts.method || "GET", url, opts && opts.body]);
    if (extraFetch) { const r = extraFetch(url, opts); if (r) return r; }
    const json = (o) => ({ ok: true, status: 200, json: async () => o });
    if (url === "/api/slash/run") return typeof slashResponse === "function" ? slashResponse(JSON.parse(opts.body)) : json(slashResponse);
    if (url.startsWith("/api/daemons") && !url.startsWith("/api/daemons/") && failDaemons) {
      failDaemons = false;
      return { ok: false, status: 500, json: async () => ({ error: "boom" }) };
    }
    if (url === "/api/favorite-daemons") return json(daemonFavs);
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
    setExtraFetch: (f) => { extraFetch = f; },
    setPersonaResult: (r) => { personaResult = r; },
    setPersonaList: (l) => { personaList = l; },
    state,
    setSlash: (v) => { slashResponse = v; },
    failDaemonsOnce: () => { failDaemons = true; },
    store,
    setConfirm: (v) => { confirmAnswer = v; },
    favCommands, setDaemonFavs: (v) => { daemonFavs = v; },
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


// The registry itself (the engine doesn't export it): the same file the page loads.
const REGISTRY = (() => { const w = {}; vm.runInNewContext(fs.readFileSync(path.join(PUBLIC, "slash-commands-data.js"), "utf8"), { window: w }); return w.JARVIS_SLASH_COMMANDS; })();
const verbSpec = (name) => (REGISTRY.verbs || REGISTRY.commands || []).find((v) => v.verb === name);

const json = (o, status = 200) => ({ ok: status < 400, status, json: async () => o });

async function main() {
  // ---- registry ----------------------------------------------------------
  {
    const env = makeEnv();
    check("no 'no handler' warning for the new verbs", env.warnings.length === 0, env.warnings.join("; "));
    for (const v of ["search", "export", "remind", "persona"]) {
      env.input.value = "/" + v;
      const p = env.S.parse("/" + v);
      check(`/${v} parses as a command`, p && p.token === v);
    }
    check("/export alone is complete (its format is optional)", env.S.isComplete("/export"));
    check("/remind alone is not complete", !env.S.isComplete("/remind"));
    check("/search alone is not complete", !env.S.isComplete("/search"));
    check("/persona alone is not complete", !env.S.isComplete("/persona"));
  }

  // ---- splitReminder -----------------------------------------------------
  {
    const env = makeEnv();
    const sp = env.S._pure.splitReminder;
    const eq = (t, w, m) => { const r = sp(t); return r && r.when === w && r.msg === m; };
    check("a | splits time from message", eq("in 20 minutes | call Sam", "in 20 minutes", "call Sam"));
    check("a | wins even when a time phrase leads", eq("in 5 minutes | 3 apples", "in 5 minutes", "3 apples"));
    check("no bar: 'in N units' prefix", eq("in 20 minutes call Sam", "in 20 minutes", "call Sam"));
    check("no bar: compound duration", eq("in 2 hours 30 minutes tea", "in 2 hours 30 minutes", "tea"));
    check("no bar: bare 10m", eq("10m call mom", "10m", "call mom"));
    check("no bar: at 9pm", eq("at 9pm take pills", "at 9pm", "take pills"));
    check("no bar: tomorrow at 9am", eq("tomorrow at 9am send invoice", "tomorrow at 9am", "send invoice"));
    check("no bar: weekday at time", eq("friday at 5 dinner", "friday at 5", "dinner"));
    check("no bar: recurring", eq("every weekday at 8:30 standup", "every weekday at 8:30", "standup"));
    check("no bar: ISO date and time", eq("2026-10-12 09:00 meet", "2026-10-12 09:00", "meet"));
    check("no bar: noon", eq("noon lunch", "noon", "lunch"));
    check("quoted time", eq('"next friday 5pm" go', "next friday 5pm", "go"));
    check("a message with no recognisable time is refused, not guessed", sp("call mom") === null);
    check("a time with no message is refused", sp("in 20 minutes") === null && sp("in 20 minutes |") === null);
    check("an empty time before | is refused", sp("| call Sam") === null);
    check("empty input is refused", sp("") === null && sp("   ") === null);
  }

  // ---- /remind -----------------------------------------------------------
  {
    const env = makeEnv();
    env.setExtraFetch((url, opts) => (url === "/api/scheduled" && opts.method === "POST")
      ? json({ id: "abcdef12", when: "at 14:20", in: "20m" }) : null);
    const r = await submit(env, "/remind in 20 minutes | call Sam");
    const post = env.fetches.find((f) => f[1] === "/api/scheduled");
    check("/remind POSTs to /api/scheduled", !!post && post[0] === "POST");
    const body = post ? JSON.parse(post[2]) : {};
    check("...with the split time, the message and kind=reminder", body.when === "in 20 minutes" && body.message === "call Sam" && body.kind === "reminder", JSON.stringify(body));
    check("...and says when it will fire", env.toasts.some(([k, m]) => k === "info" && /Reminder set/.test(m) && /14:20/.test(m)), JSON.stringify(env.toasts));
    check("/remind is handled (nothing falls through to the model)", r && r.status === "handled", JSON.stringify(r));
  }
  {
    const env = makeEnv();
    const r = await submit(env, "/remind call mom");
    check("/remind with no recognisable time sends nothing", !env.fetches.some((f) => f[1] === "/api/scheduled"));
    check("...shows the usage", env.toasts.some(([, m]) => /Usage: \/remind/.test(m)), JSON.stringify(env.toasts));
    check("...and gives the text back to edit", env.input.value === "/remind call mom", env.input.value);
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url === "/api/scheduled" ? json({ error: "couldn't understand the time 'blorp'" }, 400) : null);
    await submit(env, "/remind blorp | x");
    check("a scheduler refusal is shown, not swallowed", env.toasts.some(([k, m]) => k === "error" && /blorp/.test(m)), JSON.stringify(env.toasts));
    check("...and the text comes back", env.input.value === "/remind blorp | x", env.input.value);
  }

  // ---- /search -----------------------------------------------------------
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url.startsWith("/api/conversations-search") ? json({ results: [] }) : null);
    await submit(env, "/search sourdough");
    const f = env.fetches.find((x) => x[1].startsWith("/api/conversations-search"));
    check("/search calls the conversation search with the words encoded", !!f && f[1] === "/api/conversations-search?q=sourdough");
    check("no hit says so", env.toasts.some(([k, m]) => k === "info" && /No chat matched "sourdough"/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url.startsWith("/api/conversations-search")
      ? json({ results: [{ id: "c2", title: "Taxes", match_count: 2, matches: [{ ts: "2026-10-01T10:00:00", snippet: "the form" }] }] }) : null);
    await submit(env, "/search form 1040");
    check("one other chat matching opens it", called(env, "selectChat") && env.calls.find((c) => c[0] === "selectChat")[1] === "c2");
    check("...and the query is URL-encoded", env.fetches.some((f) => f[1] === "/api/conversations-search?q=form%201040"));
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url.startsWith("/api/conversations-search")
      ? json({ results: [{ id: "c1", title: "Weekend trip", match_count: 1, matches: [] }] }) : null);
    await submit(env, "/search trip");
    check("the only match being the open chat does not reopen it", !called(env, "selectChat"));
    check("...it says so", env.toasts.some(([k, m]) => k === "info" && /Only this chat/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url.startsWith("/api/conversations-search")
      ? json({ results: [
        { id: "c1", title: "Weekend trip", match_count: 1, matches: [{ ts: "2026-10-01T10:00:00", snippet: "pack the tent" }] },
        { id: "c2", title: "Taxes", match_count: 2, matches: [{ ts: "2026-10-02T11:30:00", snippet: "tent deduction" }] }] }) : null);
    await submit(env, "/search tent");
    const dlg = env.calls.find((c) => c[0] === "dialog");
    check("several matches open a dialog", !!dlg && dlg[1] === "/search tent");
    check("...listing each chat with its id, title and snippet", dlg && /c1 {2}Weekend trip/.test(dlg[2]) && /c2 {2}Taxes/.test(dlg[2]) && /tent deduction/.test(dlg[2]), dlg && dlg[2]);
    check("...without switching chats", !called(env, "selectChat"));
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url.startsWith("/api/conversations-search") ? json({ error: "Missing or invalid query." }, 400) : null);
    await submit(env, "/search x");
    check("a failed search is reported", env.toasts.some(([k, m]) => k === "error" && /invalid query/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    const fmt = env.S._pure.formatSearchResults;
    check("formatSearchResults: empty list", /No conversations matched/.test(fmt([], "q")));
    const many = Array.from({ length: 25 }, (_, i) => ({ id: "id" + i, title: "T" + i, match_count: 1, matches: [{ ts: "2026-10-01T10:00", snippet: "s" }] }));
    const out = fmt(many, "q");
    check("formatSearchResults: caps at 20 chats and says how many were left out", /id19/.test(out) && !/id20 /.test(out) && /and 5 more conversations/.test(out), out.slice(-120));
  }

  // ---- /export -----------------------------------------------------------
  {
    const env = makeEnv();
    env.setSlash((req) => json({ ok: true, code: 0, stderr: "", error: "", stdout: JSON.stringify({ ok: true, format: req.args[2], path: "C:/Users/x/Downloads/jarvis-trip.md" }) }));
    await submit(env, "/export");
    const f = env.fetches.find((x) => x[1] === "/api/slash/run");
    const body = f ? JSON.parse(f[2]) : {};
    check("/export runs conv-export on the open chat, Markdown by default", body.name === "conv-export" && JSON.stringify(body.args) === JSON.stringify(["c1", "--format", "md"]), JSON.stringify(body));
    check("...and tells you where the file went", env.toasts.some(([k, m]) => k === "info" && /Downloads\/jarvis-trip\.md/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    env.setSlash((req) => json({ ok: true, code: 0, stderr: "", error: "", stdout: JSON.stringify({ ok: true, format: "html", path: "p.html", warning: "Couldn't make a PDF, so this is HTML." }) }));
    await submit(env, "/export PDF");
    const body = JSON.parse(env.fetches.find((x) => x[1] === "/api/slash/run")[2]);
    check("/export takes the format case-insensitively", body.args[2] === "pdf", JSON.stringify(body));
    check("a partial success carries its warning", env.toasts.some(([, m]) => /Couldn't make a PDF/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    await submit(env, "/export docx");
    check("an unknown format is refused before anything runs", !env.fetches.some((f) => f[1] === "/api/slash/run") && env.toasts.some(([, m]) => /Format must be/.test(m)));
    check("...and the text comes back", env.input.value === "/export docx");
  }
  {
    const env = makeEnv();
    env.state.activeConversationId = null;
    await submit(env, "/export");
    check("no open chat: nothing to export", !env.fetches.some((f) => f[1] === "/api/slash/run") && env.toasts.some(([, m]) => /Open a chat first/.test(m)));
  }
  {
    const env = makeEnv();
    env.setSlash(() => json({ ok: false, code: 1, stderr: "", error: "", stdout: JSON.stringify({ ok: false, error: "no such conversation" }) }));
    await submit(env, "/export md");
    check("an export failure is shown", env.toasts.some(([k, m]) => k === "error" && /no such conversation/.test(m)), JSON.stringify(env.toasts));
  }

  // ---- /persona ----------------------------------------------------------
  {
    const env = makeEnv();
    await submit(env, "/persona friday");
    check("/persona switches by id", env.calls.some((c) => c[0] === "setPersona" && c[1] === "friday"), JSON.stringify(env.calls));
  }
  {
    const env = makeEnv();
    await submit(env, "/persona EDITH");
    check("/persona matches the name case-insensitively", env.calls.some((c) => c[0] === "setPersona" && c[1] === "edith"), JSON.stringify(env.calls));
  }
  {
    const env = makeEnv();
    await submit(env, "/persona fri");
    check("/persona takes a unique partial name", env.calls.some((c) => c[0] === "setPersona" && c[1] === "friday"), JSON.stringify(env.calls));
  }
  {
    const env = makeEnv();
    await submit(env, "/persona nobody");
    check("an unknown persona changes nothing", !called(env, "setPersona") && env.toasts.some(([, m]) => /No persona called "nobody"/.test(m)), JSON.stringify(env.toasts));
    check("...and the text comes back", env.input.value === "/persona nobody");
  }
  {
    const env = makeEnv();
    env.setPersonaList([{ id: "ember", name: "Ember" }, { id: "embark", name: "Embark" }]);
    await submit(env, "/persona emb");
    check("an ambiguous partial name asks for a pick, not a guess", !called(env, "setPersona") && env.toasts.some(([, m]) => /matches 2 personas/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    env.setPersonaResult({ ok: false, error: "Friday is switched off in the Tool Manager." });
    await submit(env, "/persona friday");
    check("a refusal from the host is shown", env.toasts.some(([k, m]) => k === "error" && /switched off/.test(m)), JSON.stringify(env.toasts));
    check("...and the text comes back", env.input.value === "/persona friday");
  }

  // ---- the argument lists ------------------------------------------------
  {
    const env = makeEnv();
    env.input.value = "/persona ";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const rows = env.palette.querySelectorAll(".slash-row");
    check("typing '/persona ' lists the personas", rows.length === 3, `got ${rows.length}`);
    env.input.value = "/export ";
    env.input.dispatchEvent(new Event("input"));
    await flush();
    const fr = env.palette.querySelectorAll(".slash-row");
    check("typing '/export ' lists the five formats", fr.length === 5, `got ${fr.length}`);
  }

  // ---- /copy N -------------------------------------------------------------
  {
    const env = makeEnv();
    await submit(env, "/copy");
    check("/copy alone still copies the whole reply", last(env) && last(env)[0] === "copyLastReply" && last(env).length === 1, JSON.stringify(last(env)));
    await submit(env, "/copy 2");
    check("/copy 2 asks for the second code block", last(env) && last(env)[0] === "copyLastReply" && last(env)[1] === 2, JSON.stringify(last(env)));
    const n = env.calls.length;
    for (const bad of ["/copy 0", "/copy two", "/copy -1", "/copy 1.5", "/copy 2 3", "/copy 1000"]) {
      env.toasts.length = 0;
      const r = await submit(env, bad);
      check(`${bad} copies nothing and shows the usage`, env.calls.length === n && env.toasts.some(([, m]) => /Usage: \/copy/.test(m)), JSON.stringify(env.toasts));
      check(`...and ${bad} gives the text back to edit`, env.input.value === bad, env.input.value);
    }
    const spec = verbSpec("copy");
    check("the copy verb takes one optional free-text argument", spec.args.length === 1 && spec.args[0].required === false && spec.args[0].source === "freeText", JSON.stringify(spec.args));
    check("...so '/copy' is complete and runs on the first Enter", env.S.isComplete("/copy") && env.S.isComplete("/copy 2"));
  }
  {
    // The host says it couldn't (no such block): the typed text stays.
    const env = makeEnv();
    const real = env.calls;
    check("/copy N is still allowed while Jarvis is replying", (() => { env.setRunning(true); return true; })());
    await submit(env, "/copy 3");
    check("...and reaches the host", real.some((c) => c[0] === "copyLastReply" && c[1] === 3));
  }

  // ---- /backlog add --------------------------------------------------------
  {
    const env = makeEnv();
    await submit(env, "/backlog");
    check("/backlog alone still opens the panel", last(env) && last(env)[0] === "openPanel" && last(env)[1] === "backlog", JSON.stringify(last(env)));
    check("...and sends nothing to the server", !env.fetches.some((f) => f[1] === "/api/backlog"));
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url, opts) => (url === "/api/backlog" && opts.method === "POST") ? json({ ok: true, item: { id: "b_1234" } }) : null);
    const r = await submit(env, "/backlog add renew the passport");
    const post = env.fetches.find((f) => f[1] === "/api/backlog");
    check("/backlog add POSTs the title to /api/backlog", !!post && post[0] === "POST" && JSON.parse(post[2]).title === "renew the passport", JSON.stringify(post));
    check("...is handled without opening the panel or asking the model", r && r.status === "handled" && !env.calls.some((c) => c[0] === "openPanel"), JSON.stringify(r));
    check("...and says what it added", env.toasts.some(([k, m]) => k === "info" && /Added to the Backlog: renew the passport/.test(m)), JSON.stringify(env.toasts));
    await submit(env, "/backlog ADD   keep   spacing  ");
    const second = env.fetches.filter((f) => f[1] === "/api/backlog")[1];
    check("'add' is case-insensitive and the title is trimmed", second && JSON.parse(second[2]).title === "keep   spacing", JSON.stringify(second));
  }
  {
    const env = makeEnv();
    for (const bad of ["/backlog add", "/backlog add   ", "/backlog buy milk", "/backlog adding things"]) {
      env.toasts.length = 0;
      await submit(env, bad);
      check(`${bad.trim()} sends nothing and shows the usage`, !env.fetches.some((f) => f[1] === "/api/backlog") && env.toasts.some(([, m]) => /Usage: \/backlog add/.test(m)), JSON.stringify(env.toasts));
      check(`...and ${JSON.stringify(bad)} stays in the box`, env.input.value === bad, env.input.value);
    }
    await submit(env, "/backlog add " + "x".repeat(201));
    check("a title over 200 characters is refused before it is sent", !env.fetches.some((f) => f[1] === "/api/backlog") && env.toasts.some(([, m]) => /200 characters/.test(m)), JSON.stringify(env.toasts));
  }
  {
    const env = makeEnv();
    env.setExtraFetch((url) => url === "/api/backlog" ? json({ error: "give the item a title" }, 400) : null);
    await submit(env, "/backlog add x1");
    check("a server refusal is shown, not swallowed", env.toasts.some(([k, m]) => k === "warn" && /title/.test(m)), JSON.stringify(env.toasts));
    check("...and the text comes back", env.input.value === "/backlog add x1", env.input.value);
    const spec = verbSpec("backlog");
    check("the backlog verb takes one optional free-text argument and still covers the CLI names",
      spec.args.length === 1 && spec.args[0].required === false && spec.covers.includes("backlog-add"), JSON.stringify(spec));
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
