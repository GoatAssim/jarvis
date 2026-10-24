// Settings > Downloads (web/public/downloads-settings.js): Steam's update-size threshold (Download Hub N1 / track C), and (brief 19)
// the "Give up on a lost download after (hours)" row and the Stremio card: every state it shows, Locate... (saved / refused /
// cancelled), Start (working / ok / refused / the page's own time-out), the diagnostic text, and that none of it blocks the page.
// Builds the real section against a fake `ui` (the row helpers settings.js hands over), a fake fetch and a fake confirm dialog,
// so what the row is, what it writes, when it asks first and how an error reads are all checked without a browser.
//
//   node tests/verify_hub_settings.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let passed = 0;
let failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log("ok      " + name); } else { failed++; console.log("FAILED  " + name); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const file = path.join(__dirname, "..", "web", "public", "downloads-settings.js");
const src = fs.readFileSync(file, "utf8");

// An inert stand-in for a DOM element: any property reads as a harmless function, writes are kept.
function fakeEl(tag, attrs, kids) {
  const store = { tag, attrs: attrs || {}, kids: kids || [] };
  return new Proxy(store, {
    get(t, k) { if (k in t) return t[k]; if (k === "style") return {}; if (k === "classList") return { add() {}, remove() {}, toggle() {} }; return () => fakeEl("x"); },
    set(t, k, v) { t[k] = v; return true; },
  });
}

function world(opts) {
  const o = Object.assign({ providers: [], confirmRequired: false, confirmAnswer: true, runResult: { ok: true }, settings: {}, writable: [], prefsWritable: [],
    stremio: { ok: true, state: "running", title: "Running", sentence: "Stremio's server is running.", can_start: false, address: "http://127.0.0.1:11470", path: {} },
    stremioHang: false, pathsReply: null, startReply: null, hang: null, defaults: undefined, events: [], openModes: [], sources: null,
    qbt: { ok: true, file: "~/.jarvis/qbittorrent.json", exists: true, problem: null, values: { enabled: true, address: "http://127.0.0.1:8080", username: "admin" },
      defaults: { enabled: true, address: "http://127.0.0.1:8080", username: "" }, password_set: true, file_overrides: [], engine: { ok: true, reason: "" } },
    qbtPost: null }, opts || {});
  const calls = [];
  const win = {};
  win.fetch = async (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({ url, body, headers: (init && init.headers) || null, method: (init && init.method) || "GET" });
    const reply = (data, status) => ({ ok: !status || status < 400, status: status || 200, statusText: status ? "ERR" : "OK", json: async () => data });
    if (o.hang && o.hang.test(url)) return new Promise((resolve, reject) => { if (init && init.signal) init.signal.addEventListener("abort", () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); }); });
    if (url === "/api/hub/settings") return reply({ ok: true, settings: o.settings, defaults: o.defaults, providers: o.providers, writable: o.writable, prefs_writable: o.prefsWritable, events: o.events, open_modes: o.openModes });
    if (url === "/api/hub/sources" && o.sources) return reply(o.sources);
    if (url === "/api/hub/stremio") return o.stremioHang ? new Promise(() => {}) : (o.stremio && o.stremio.ok === false ? reply(o.stremio, 400) : reply(o.stremio));
    if (url === "/api/hub/stremio/paths" && o.pathsReply) return reply(o.pathsReply.body, o.pathsReply.status);
    if (url === "/api/hub/stremio/start" && o.startReply) return reply(o.startReply.body, o.startReply.status);
    if (url === "/api/hub/qbittorrent") {
      if (!o.qbt) return { ok: false, status: 404, statusText: "nope", json: async () => null };
      if (init && init.method === "POST") return o.qbtPost ? reply(o.qbtPost.body, o.qbtPost.status) : reply(Object.assign({}, o.qbt, { saved: Object.keys(body || {}) }));
      return o.qbt.ok === false ? reply(o.qbt, 400) : reply(o.qbt);
    }
    if (url === "/api/tools/preview") return reply({ confirm_required: o.confirmRequired });
    if (url === "/api/tools/run") return reply({ result: o.runResult });
    return { ok: false, status: 404, statusText: "nope", json: async () => null };
  };
  const confirms = [];
  win.JarvisUI = { confirm: async (c) => { confirms.push(c); return o.confirmAnswer; } };
  vm.runInContext(src, vm.createContext({ window: win, fetch: (...a) => win.fetch(...a), console, setTimeout, clearTimeout, AbortController }), { filename: "downloads-settings.js" });
  const controls = [];
  const shells = [];
  const flashes = [];
  const ui = {
    h: (tag, attrs, kids) => fakeEl(tag, attrs, kids),
    group: (title, rows) => ({ title, rows }),
    rowShell: (label, help, el, status) => { const r = { label, help, el, status }; shells.push(r); return r; },
    makeControl: (spec, value, commit, status) => { const c = { spec, value, commit, status, el: fakeEl("ctl"), set(v) { c.last = v; } }; controls.push(c); return c; },
    flash: (status, text, kind) => flashes.push({ text, kind }),
  };
  const section = win.JarvisSettingsSections[0];
  return { win, calls, confirms, ui, controls, shells, flashes, section, pure: win.JarvisDownloadsSettings._pure };
}

const steamP = (settings) => ({ id: "steam", label: "Steam", enabled: true, read_only: false, interval_s: 5, capabilities: ["reveal"], settings });
const playniteP = { id: "playnite", label: "Playnite", enabled: true, read_only: true, capabilities: [], settings: {} };

(async () => {
  // ---- the pure half
  {
    const w = world();
    const rows = w.pure.providerSettingRows(steamP({ notify_min_update_gb: 1 }));
    check("Steam reports its threshold -> one row, a number of GB with its current value", rows.length === 1 && rows[0].key === "notify_min_update_gb" && rows[0].type === "float" && rows[0].unit === "GB" && rows[0].value === 1);
    check("0 is a value, not an absence (it means every update)", w.pure.providerSettingRows(steamP({ notify_min_update_gb: 0 })).length === 1 && w.pure.providerSettingRows(steamP({ notify_min_update_gb: 0 }))[0].value === 0);
    check("a provider with no such setting gets no row (an older hub shows nothing, not a dead row)",
      w.pure.providerSettingRows(playniteP).length === 0 && w.pure.providerSettingRows(steamP({})).length === 0 && w.pure.providerSettingRows(steamP(undefined)).length === 0
      && w.pure.providerSettingRows(steamP({ notify_min_update_gb: "1" })).length === 0 && w.pure.providerSettingRows(null).length === 0
      && w.pure.providerSettingRows({ id: "other", settings: { notify_min_update_gb: 1 } }).length === 0);
    check("the write is one dl_provider_prefs `settings` object", eq(w.pure.providerSettingArgs("steam", "notify_min_update_gb", 2.5), { id: "steam", settings: { notify_min_update_gb: 2.5 } }));
  }

  // ---- the section, built for real
  {
    const w = world({ providers: [steamP({ notify_min_update_gb: 1 }), playniteP] });
    await w.section.build({ ui: w.ui });
    const steamRow = w.shells.find((r) => /Steam: announce updates/.test(r.label));
    check("the row is on the page with its words", !!steamRow && steamRow.label === "Steam: announce updates of at least" && /0 = announce every update/.test(steamRow.help) && /size isn't known stays quiet unless this is 0/.test(steamRow.help));
    const ctl = w.controls.find((c) => c.spec.unit === "GB");
    check("it is a number box in GB with a floor of 0 as an input hint, showing the hub's value", !!ctl && ctl.spec.type === "float" && ctl.spec.min === 0 && ctl.value === 1);
    check("Playnite gets its switch but no threshold row", w.shells.some((r) => r.label === "Playnite") && w.shells.filter((r) => /announce updates/.test(r.label)).length === 1);
    check("the rows sit in 'Other tools' downloads', after Steam's own switch", (() => {
      const labels = w.shells.map((r) => r.label);
      return labels.indexOf("Steam") > -1 && labels.indexOf("Steam") < labels.indexOf("Steam: announce updates of at least") && labels.indexOf("Steam: announce updates of at least") < labels.indexOf("Steam: check every");
    })());

    // writing
    await ctl.commit(2);
    const prev = w.calls.filter((c) => c.url === "/api/tools/preview").pop();
    const run = w.calls.filter((c) => c.url === "/api/tools/run").pop();
    check("saving asks the preview first and then runs dl_provider_prefs with the number", prev.body.name === "dl_provider_prefs" && run.body.name === "dl_provider_prefs"
      && eq(run.body.arguments, { id: "steam", settings: { notify_min_update_gb: 2 } }) && w.calls.indexOf(prev) < w.calls.indexOf(run));
    check("a saved row says Saved", eq(w.flashes.pop(), { text: "Saved", kind: "ok" }));
    await ctl.commit(0);
    const run0 = w.calls.filter((c) => c.url === "/api/tools/run").pop();
    check("0 is sent as 0", run0.body.arguments.settings.notify_min_update_gb === 0);
  }

  // ---- the confirmation
  {
    const w = world({ providers: [steamP({ notify_min_update_gb: 1 })], confirmRequired: true, confirmAnswer: true });
    await w.section.build({ ui: w.ui });
    const ctl = w.controls.find((c) => c.spec.unit === "GB");
    await ctl.commit(3);
    check("when the hub asks for a confirmation the owner sees what is being changed, then it runs", w.confirms.length === 1 && /notify_min_update_gb/.test(w.confirms[0].body) && w.calls.some((c) => c.url === "/api/tools/run"));
  }
  {
    const w = world({ providers: [steamP({ notify_min_update_gb: 1 })], confirmRequired: true, confirmAnswer: false });
    await w.section.build({ ui: w.ui });
    const ctl = w.controls.find((c) => c.spec.unit === "GB");
    let err = null;
    try { await ctl.commit(3); } catch (e) { err = e; }
    check("declining writes nothing and the row reports it (so it puts the old value back)", err && err.message === "Not changed: you declined." && !w.calls.some((c) => c.url === "/api/tools/run"));
  }

  // ---- the hub's own error text
  {
    const hubError = "notify_min_update_gb must be between 0 and 10000 GB";
    const w = world({ providers: [steamP({ notify_min_update_gb: 1 })], runResult: { ok: false, error: hubError } });
    await w.section.build({ ui: w.ui });
    const ctl = w.controls.find((c) => c.spec.unit === "GB");
    let err = null;
    try { await ctl.commit(-5); } catch (e) { err = e; }
    check("a value the hub refuses is sent as typed and its error shows as the hub wrote it (the page owns no range)", err && err.message === hubError && w.calls.some((c) => c.url === "/api/tools/run" && c.body.arguments.settings.notify_min_update_gb === -5));
    check("nothing says Saved after a refusal", !w.flashes.some((f) => f.text === "Saved"));
  }


  // ---- brief 19, step 2: the settle-hours row
  {
    const w = world({ settings: { unknown_settle_hours: 24 }, writable: ["unknown_settle_hours"], prefsWritable: ["unknown_settle_hours"] });
    const blocks = await w.section.build({ ui: w.ui });
    const row = w.shells.find((r) => r.label === "Give up on a lost download after (hours)");
    // moved (brief 25): the old "Lost downloads" group is now a row of the Sources section
    const lost = blocks[0].__dls.rowFor("unknown_settle_hours");
    check("with the hub reporting it writable, the row is on the page, in the Sources section", !!row && !!lost && lost.section === "sources" && blocks[0].__dls.sectionIds().includes("sources"));
    check("its help says what happens: cancelled, never failed, and a program that is only closed is never given up on",
      /cancelled/.test(row.help) && /never \"failed\"/.test(row.help) && /closed is never given up on/.test(row.help) && /Steam, Playnite/.test(row.help));
    const ctl = w.controls.find((c) => c.spec.path === "unknown_settle_hours");
    check("it is a whole-number box showing the hub's value, with no range of its own", !!ctl && ctl.spec.type === "int" && ctl.value === 24 && ctl.spec.min === undefined && ctl.spec.max === undefined);
    await ctl.commit(48);
    const prev = w.calls.filter((c) => c.url === "/api/tools/preview").pop();
    const run = w.calls.filter((c) => c.url === "/api/tools/run").pop();
    check("it is written through dl_prefs (the same path as the other writable settings), preview first",
      prev.body.name === "dl_prefs" && run.body.name === "dl_prefs" && eq(run.body.arguments, { set: { unknown_settle_hours: 48 } }) && w.calls.indexOf(prev) < w.calls.indexOf(run));
    check("a saved row says Saved", eq(w.flashes.pop(), { text: "Saved", kind: "ok" }));
    check("the Settings search finds it", /unknown lost give up settle hours/.test(src));
  }
  {
    const hubError = "unknown_settle_hours must be between 1 and 720";
    const w = world({ settings: { unknown_settle_hours: 24 }, writable: ["unknown_settle_hours"], prefsWritable: ["unknown_settle_hours"], runResult: { ok: false, error: hubError } });
    await w.section.build({ ui: w.ui });
    const ctl = w.controls.find((c) => c.spec.path === "unknown_settle_hours");
    let err = null;
    try { await ctl.commit(0); } catch (e) { err = e; }
    check("0 is sent as typed and the hub's own refusal text is what the row shows (the page checks no range)",
      err && err.message === hubError && w.calls.some((c) => c.url === "/api/tools/run" && c.body.arguments.set.unknown_settle_hours === 0));
  }
  {
    const w = world({ settings: { unknown_settle_hours: 24 }, writable: [], prefsWritable: [] });
    const blocks = await w.section.build({ ui: w.ui });
    check("an older hub that doesn't list the key gets no row (not a dead one)", blocks[0].__dls.rowFor("unknown_settle_hours") === null && !w.shells.some((r) => /Give up on a lost/.test(r.label)));
  }

  // ---- brief 19, step 3: the Stremio card, pure half (every state the hub can report)
  {
    const w = world();
    const P = w.pure;
    const found = { found: true, runtime_exe: "C:/Stremio/stremio-runtime.exe", server_js: "C:/Stremio/server.js", where: "registry", how_text: "in Windows' list of installed programs" };
    const st = (o) => Object.assign({ ok: true, address: "http://127.0.0.1:11470", version: "", problems: [], can_start: false, path: {}, wait_seconds: 60, other_url: "", web: { url: "http://127.0.0.1:8080", answers: false, configured: false } }, o);
    const notFound = P.stremioModel(st({ state: "not_found", title: "Not found", sentence: "I couldn't find Stremio on this PC.", next: "", path: { found: false } }));
    check("not found: the hub's sentence, a red title, Start off with the sentence as its tooltip, program and server.js 'not found'",
      notFound.loaded && notFound.sentence === "I couldn't find Stremio on this PC." && notFound.tone === "bad" && notFound.canStart === false && notFound.startTip === notFound.sentence
      && eq(notFound.facts.slice(0, 2), [["Program", "not found"], ["server.js", "not found"]]) && notFound.facts.some((f) => f[0] === "App" && f[1] === "not found"));
    const ready = P.stremioModel(st({ state: "ready", title: "Installed, not running", sentence: "Stremio is installed and its server isn't running.", next: "Press Start.", can_start: true, path: found, installed_version: "4.4.168" }));
    check("found but not running: Start on, the paths and how they were found shown, the installed version, the server 'not running'",
      ready.canStart && /may take up to 60 seconds/.test(ready.startTip) && ready.tone === "" && ready.next === "Press Start." && ready.facts.some((f) => f[0] === "Program" && f[1] === found.runtime_exe)
      && ready.facts.some((f) => f[0] === "Found" && /installed programs/.test(f[1])) && ready.facts.some((f) => f[0] === "App" && f[1] === "installed, version 4.4.168") && ready.facts.some((f) => f[0] === "Server" && f[1] === "not running"));
    const outside = P.stremioModel(st({ state: "running", title: "Running", version: "4.20.8", managed_by: "outside", sentence: "Stremio's server is running at http://127.0.0.1:11470 (version 4.20.8).", path: found }));
    const mine = P.stremioModel(st({ state: "running", title: "Running", version: "4.20.8", managed_by: "jarvis", path: found }));
    check("running: green, Start off and says there is nothing to start, the address and version, and who started it",
      outside.tone === "ok" && outside.canStart === false && /already running/.test(outside.startTip)
      && outside.facts.some((f) => f[0] === "Server" && f[1] === "running at http://127.0.0.1:11470 (version 4.20.8), started outside Jarvis")
      && mine.facts.some((f) => f[0] === "Server" && /started by Jarvis/.test(f[1])));
    const silent = P.stremioModel(st({ state: "silent", title: "Started but not answering", sentence: "Jarvis started Stremio's server 80 seconds ago and it still doesn't answer.", next: "Look at its console", path: found }));
    const appOpen = P.stremioModel(st({ state: "app_open", title: "App open, server silent", sentence: "The Stremio app is open, but its server isn't answering at http://127.0.0.1:11470. I won't start a second server while the app is open.", path: found }));
    check("server silent / app open: Start is off and its tooltip is the status sentence (nothing is started)",
      silent.canStart === false && silent.startTip === silent.sentence && appOpen.canStart === false && appOpen.startTip === appOpen.sentence && /won't start a second server/.test(appOpen.sentence)
      && appOpen.facts.some((f) => f[0] === "App" && f[1] === "open, but its server is silent"));
    const failed = P.stremioModel(st({ state: "start_failed", title: "Could not start", sentence: "Jarvis started the Stremio program but it stopped again (exit code 1).", can_start: true, path: found }));
    check("could not start: red, and Start stays on when the program was found (try again)", failed.tone === "bad" && failed.canStart === true);
    const port = P.stremioModel(st({ state: "other_port", title: "Running on another port", other_url: "http://127.0.0.1:11471", sentence: "Stremio's server is running, but not where Jarvis looks: it answers at http://127.0.0.1:11471.", path: found }));
    check("another port: the server line names both addresses", port.facts.some((f) => f[0] === "Server" && /http:\/\/127\.0\.0\.1:11471, not at http:\/\/127\.0\.0\.1:11470/.test(f[1])) && port.canStart === false);
    const webOn = P.stremioModel(st({ state: "running", web: { url: "http://127.0.0.1:8080", answers: true, configured: true }, path: found }));
    const webOff = P.stremioModel(st({ state: "ready", can_start: true, web: { url: "http://127.0.0.1:8080", answers: false, configured: true }, path: found }));
    const webNone = P.stremioModel(st({ state: "ready", can_start: true, web: { url: "http://127.0.0.1:8080", answers: false, configured: false }, path: found }));
    const webUnk = P.stremioModel(st({ state: "ready", can_start: true, web: null, path: found }));
    check("the web line: answering, not answering, not set up, unknown",
      webOn.facts.some((f) => f[0] === "Web" && f[1] === "answering at http://127.0.0.1:8080") && webOff.facts.some((f) => f[0] === "Web" && /^not answering at/.test(f[1]))
      && webNone.facts.some((f) => f[0] === "Web" && /not set up/.test(f[1])) && webUnk.facts.some((f) => f[0] === "Web" && f[1] === "unknown"));
    const probs = P.stremioModel(st({ state: "ready", can_start: true, path: found, problems: ["the saved server.js no longer exists"] }));
    check("config problems are listed as facts", probs.facts.some((f) => f[0] === "Problem" && /no longer exists/.test(f[1])));
    check("the tone of every state the hub can report is known", ["running", "other_port", "port_busy", "start_failed", "starting", "silent", "app_open", "files_missing", "not_found", "ready"].every((k) => k in P.STREMIO_TONE));
    check("nothing yet = 'Reading', and no Start", P.stremioModel(null).sentence.startsWith("Reading") && P.stremioModel(null).canStart === false);
    const bad = P.stremioModel({ ok: false, error: "Couldn't read Stremio's status: HTTP 500" });
    check("a status that couldn't be read shows the error, red, with no Start", bad.loaded === false && bad.tone === "bad" && /HTTP 500/.test(bad.sentence) && bad.canStart === false);
    check("an answer with no `can_start` never turns Start on", P.stremioModel({ ok: true, state: "ready", sentence: "x", path: found }).canStart === false);

    // Copy diagnostic
    check("Copy diagnostic copies the text the hub prepared, exactly", P.diagnosticFor({ state: "ready", diagnostic: "Stremio status: Installed\nline 2" }) === "Stremio status: Installed\nline 2");
    const built = P.diagnosticFor(st({ state: "ready", title: "Installed, not running", sentence: "S.", path: found, version: "" }));
    check("with none from the hub it is built from the fields, and says so",
      /built by the page/.test(built) && /Installed, not running/.test(built) && /Program: C:\/Stremio\/stremio-runtime\.exe/.test(built) && /address: http:\/\/127\.0\.0\.1:11470/.test(built));
    check("a diagnostic is never invented from nothing", P.diagnosticFor(null) === "" && P.stremioModel(null).diagnostic === "");
  }

  // ---- Locate...
  {
    const w = world();
    const P = w.pure;
    const found = { runtime_exe: "C:\\Stremio\\stremio-runtime.exe", server_js: "C:\\Stremio\\server.js" };
    const run = async (status, which, picks, saveWith, windows) => {
      const asked = [];
      const saved = [];
      const out = await P.locateFlow({
        windows: windows !== false,
        pick: async (o) => { asked.push(o); return picks.shift(); },
        save: async (b) => { saved.push(b); if (saveWith instanceof Error) throw saveWith; return saveWith || { ok: true, state: "ready", saved: b }; },
      }, status, which);
      return { out, asked, saved };
    };
    let r = await run({ state: "not_found", path: { found: false } }, "runtime_exe", ["D:\\S\\stremio-runtime.exe", "D:\\S\\server.js"]);
    check("nothing known: Locate asks for the program, then server.js, starting the second pick in the first one's folder, then saves both",
      r.asked.length === 2 && r.asked[0].kind === "file" && /stremio-runtime/.test(r.asked[0].title) && eq(r.asked[0].extensions, [".exe"]) && r.asked[0].start === ""
      && /server\.js/.test(r.asked[1].title) && eq(r.asked[1].extensions, [".js"]) && r.asked[1].start === "D:\\S"
      && eq(r.saved, [{ runtime_exe: "D:\\S\\stremio-runtime.exe", server_js: "D:\\S\\server.js" }]) && r.out.status === "saved");
    r = await run({ state: "ready", path: found }, "server_js", ["E:\\Other\\server.js"]);
    check("the other file is known: one pick, the known path is kept, and the picker opens where it lives",
      r.asked.length === 1 && r.asked[0].start === "C:\\Stremio" && eq(r.saved, [{ runtime_exe: found.runtime_exe, server_js: "E:\\Other\\server.js" }]));
    r = await run({ state: "ready", path: found }, "runtime_exe", [null]);
    check("cancelling the picker saves nothing", r.out.status === "cancelled" && r.saved.length === 0);
    r = await run({ state: "not_found", path: {} }, "runtime_exe", ["D:\\S\\stremio-runtime.exe", null]);
    check("cancelling the second pick saves nothing either", r.out.status === "cancelled" && r.saved.length === 0);
    r = await run({ state: "ready", path: found }, "server_js", ["E:\\x.js"], new Error("server.js doesn't exist: E:\\x.js"));
    check("Locate refused: the hub's own sentence comes back as it is, and the page doesn't say saved", r.out.status === "refused" && r.out.error === "server.js doesn't exist: E:\\x.js");
    r = await run({ state: "other_port", other_url: "http://127.0.0.1:11471", path: found }, "runtime_exe", ["C:\\Stremio\\stremio-runtime.exe"]);
    check("another port: the address the server answered at is saved with the paths (what the status sentence promises)", r.saved[0].server_url === "http://127.0.0.1:11471");
    r = await run({ state: "ready", path: found }, "runtime_exe", ["C:\\Stremio\\stremio-runtime.exe"]);
    check("in every other state no server address is sent", !("server_url" in r.saved[0]));
    r = await run({ state: "ready", path: {} }, "runtime_exe", ["/opt/s/stremio", "/opt/s/server.js"], null, false);
    check("off Windows the program picker has no .exe filter", eq(r.asked[0].extensions, []));
    check("the folder helper takes either slash", P.folderOf("C:\\a\\b.exe") === "C:\\a" && P.folderOf("/a/b/c.js") === "/a/b" && P.folderOf("b.exe") === "" && P.folderOf(null) === "");

    // Start
    const startWith = async (impl, status) => P.startFlow({ start: impl }, status || { wait_seconds: 90 });
    let called = [];
    let s = await startWith(async (ms) => { called.push(ms); return { ok: true, sentence: "Stremio's server is running at http://127.0.0.1:11470 (version 4.20.8). Jarvis started it.", state: "started" }; });
    check("Start ok: the hub's sentence is shown; the page's own time-out is the hub's wait plus 20 s", s.status === "ok" && /Jarvis started it/.test(s.text) && called[0] === 110000);
    s = await startWith(async () => ({ ok: true, state: "already_up", note: "already up; nothing was started" }));
    check("already up shows its note", s.status === "ok" && /nothing was started/.test(s.text));
    const refusal = Object.assign(new Error("The Stremio app is open, but its server isn't answering."), { data: { ok: false, error: "The Stremio app is open, but its server isn't answering.", outcome: "app_open" } });
    s = await startWith(async () => { throw refusal; });
    check("Start refused (app open): the hub's sentence, no success", s.status === "refused" && /app is open/.test(s.text) && s.result.outcome === "app_open");
    s = await startWith(async () => { throw Object.assign(new Error("timed out"), { timedOut: true }); });
    check("the page gives up on its own and says what to do", s.status === "timeout" && /110 seconds/.test(s.text) && /Check again/.test(s.text));
    s = await startWith(async () => { throw new Error("HTTP 500"); });
    check("a plain failure shows its message", s.status === "refused" && s.text === "HTTP 500");
    check("the time-out follows the hub's wait, 60 s when it is not known", P.startTimeoutMs({ wait_seconds: 300 }) === 320000 && P.startTimeoutMs({}) === 80000 && P.startTimeoutMs(null) === 80000 && P.startTimeoutMs({ wait_seconds: "x" }) === 80000);

    // hubPost: the header, the refusal, the time-out
    const w2 = world({ startReply: { status: 400, body: { ok: false, error: "no", outcome: "app_open" } }, pathsReply: { status: 200, body: { ok: true, saved: {} } } });
    let err = null;
    try { await w2.pure.hubPost("/api/hub/stremio/start", {}, 1000); } catch (e) { err = e; }
    const sent = w2.calls.find((c) => c.url === "/api/hub/stremio/start");
    check("a write to a hub route sends the X-Jarvis-Hub header and POSTs JSON", sent.method === "POST" && sent.headers["X-Jarvis-Hub"] === "1" && sent.headers["Content-Type"] === "application/json");
    check("a refusal throws with the hub's text and the whole answer", err && err.message === "no" && err.data.outcome === "app_open");
    const w3 = world({ hang: /stremio\/start/ });
    err = null;
    try { await w3.pure.hubPost("/api/hub/stremio/start", {}, 20); } catch (e) { err = e; }
    check("a request that never answers is given up on by the page (timedOut)", err && err.timedOut === true);
  }

  // ---- the card on the page
  {
    const w = world({ stremioHang: true });
    const done = await Promise.race([w.section.build({ ui: w.ui }).then(() => "built"), new Promise((r) => setTimeout(() => r("blocked"), 500))]);
    check("the page doesn't wait for Stremio's status: Settings builds while it is still being read", done === "built");
    check("the card asked for the status once, with a plain GET", w.calls.filter((c) => c.url === "/api/hub/stremio").length === 1 && w.calls.find((c) => c.url === "/api/hub/stremio").method === "GET");
    const w2 = world({ stremio: { ok: false, error: "no hub" } });
    const blocks = await w2.section.build({ ui: w2.ui });
    check("a status that can't be read doesn't break the section: the card is still there, in the Stremio source block", blocks[0].__dls.entries.some((e) => e.kind === "card" && e.source === "stremio")
      && blocks[0].__dls.sourceBlocks.some((b) => b.id === "stremio"));
  }
  {
    const code = src.slice(src.indexOf("function stremioBlock"), src.indexOf("// ---- the page (DOM)"));
    check("the card's buttons: Check again, Copy diagnostic, Locate program, Locate server.js, Start; no HTML is assigned",
      /"Check again"/.test(code) && /"Copy diagnostic"/.test(code) && /Locate program/.test(code) && /Locate server\.js/.test(code) && /text: "Start"/.test(code) && !/innerHTML|insertAdjacentHTML/.test(src));
    check("Start goes to the one start route with an empty body and its own time-out; Locate saves through the paths route",
      /hubPost\("\/api\/hub\/stremio\/start", \{\}, ms\)/.test(code) && /hubPost\("\/api\/hub\/stremio\/paths", body, 30000\)/.test(code));
    check("Start is disabled while anything works and unless the hub said can_start", /bStart\.disabled = !!state\.busy \|\| !m\.canStart/.test(code) && /all\.forEach\(\(b\) => \{ b\.disabled = !!word; \}\)/.test(code));
    check("Locate uses the existing picker (JarvisPathPicker.open) and no path is typed into the page", /global\.JarvisPathPicker/.test(code) && /picker\.open\(o\)/.test(code));
    check("the card never writes anything but through those two routes (no tool, no other fetch)", !/runHubTool|postJson/.test(code)
      && eq([...new Set(code.match(/\/api\/hub\/[a-z\/]+/g))].sort(), ["/api/hub/stremio", "/api/hub/stremio/paths", "/api/hub/stremio/start"]));
  }

  // ---- brief 29, part 1: the Stremio card's setting rows (server address, stremio-web, cache, repair)
  {
    const w = world();
    const P = w.pure;
    const status = (o) => Object.assign({ ok: true, state: "ready", address: "http://127.0.0.1:11470", path: {}, wait_seconds: 60, web: { url: "http://127.0.0.1:8080", answers: false, configured: false },
      web_config: { url: "http://127.0.0.1:8080", dir: "", dir_exists: false, start_cmd: "pnpm start", default_url: "http://127.0.0.1:8080", default_start_cmd: "pnpm start", server_url: "http://127.0.0.1:11470", default_server_url: "http://127.0.0.1:11470" },
      cache: { known: false, note: "Start the server to see it." }, config: { state: "ok", can_repair: false } }, o);

    const set = P.stremioSetupModel(status());
    check("setup model: the rows start from what the hub reports (address, web folder, command, address)", set.ready && set.server.value === "http://127.0.0.1:11470"
      && set.web.dir === "" && set.web.startCmd === "pnpm start" && set.web.url === "http://127.0.0.1:8080" && set.web.configured === false);
    check("setup model: an older hub with no web_config gives no rows (nothing is invented)", P.stremioSetupModel({ ok: true, state: "ready" }).ready === false && P.stremioSetupModel(null).ready === false);
    check("setup model: a saved folder that is gone is flagged", P.stremioSetupModel(status({ web_config: { dir: "D:\\gone", dir_exists: false } })).web.dirMissing === true);
    check("the Web fact no longer sends anyone to a file", P.stremioModel(status()).facts.some((f) => f[0] === "Web" && /choose the stremio-web folder below/.test(f[1])) && !/web\.dir is empty/.test(src));

    // brief 30: the account, player and addon rows (pure models and request bodies)
    check("account model: signed in shows the email; no account block gives no row", P.accountModel({ ok: true, account: { signed_in: true, email: "a@b.c" } }).sentence.includes("a@b.c")
      && P.accountModel({ ok: true, account: { signed_in: false } }).signedIn === false && P.accountModel({ ok: true }).ready === false);
    check("login body: needs both boxes; the key body needs the key", P.loginBody("a@b.c", "") === null && eq(P.loginBody(" a@b.c ", "pw"), { action: "login", email: "a@b.c", password: "pw" })
      && P.keyBody("  ") === null && eq(P.keyBody(" k "), { action: "key", auth_key: "k" }));
    check("player body: only what changed; empty program clears it", P.playerSaveBody({ kind: "auto", exe: "" }, { kind: "auto", exe: "" }) === null
      && eq(P.playerSaveBody({ kind: "vlc", exe: "" }, { kind: "auto", exe: "" }), { player: { kind: "vlc" } })
      && eq(P.playerSaveBody({ kind: "auto", exe: "" }, { kind: "auto", exe: "C:\\mpv.exe" }), { player: { exe: "" } }));
    check("addon list: hosts and positions only; add and remove bodies", P.addonsModel({ ok: true, addon_hosts: [{ index: 0, host: "a.example" }] }).rows[0].host === "a.example"
      && P.addonsModel({ ok: true }).ready === false && eq(P.addonsBody("https://x/manifest.json  https://y/manifest.json", null), { addon_urls: { add: ["https://x/manifest.json", "https://y/manifest.json"], remove: [] } })
      && eq(P.addonsBody("", 2), { addon_urls: { add: [], remove: [2] } }) && P.addonsBody("", null) === null);

    check("server body: nothing for an unchanged or empty box; the new address otherwise", P.serverSaveBody("http://127.0.0.1:11470", "http://127.0.0.1:11470") === null && P.serverSaveBody("  ", "x") === null
      && eq(P.serverSaveBody(" http://127.0.0.1:11471 ", "http://127.0.0.1:11470"), { server_url: "http://127.0.0.1:11471" }));
    check("web body: only the boxes that changed are sent, trimmed, as typed (the hub is the only validator)",
      eq(P.webSaveBody({ url: "http://127.0.0.1:8080", dir: " D:\\sw ", startCmd: "pnpm start" }, set.web), { web: { dir: "D:\\sw" } })
      && P.webSaveBody({ url: "http://127.0.0.1:8080", dir: "", startCmd: "pnpm start" }, set.web) === null
      && eq(P.webSaveBody({ startCmd: "a && b" }, set.web), { web: { start_cmd: "a && b" } }));
    check("web body: clearing the folder is sent as an empty string (it turns stremio-web off), not dropped",
      eq(P.webSaveBody({ dir: "" }, { dir: "D:\\sw" }), { web: { dir: "" } }));

    check("cache: not known -> the hub's sentence and no button", eq(P.stremioCacheModel(status()), { known: false, text: "Start the server to see it.", root: "", canAsk: false }));
    const cacheOn = P.stremioCacheModel(status({ cache: { known: true, root: "D:\\Cache", size_bytes: 4294967296 } }));
    check("cache: known -> the folder and its size limit, and the request button is on", cacheOn.canAsk && cacheOn.root === "D:\\Cache" && /D:\\Cache \(size limit 4 GB\)/.test(cacheOn.text));
    check("cache: an older hub (no cache block) says it isn't reported", P.stremioCacheModel({ ok: true }).text === "Not reported by this hub." && !P.stremioCacheModel({ ok: true }).canAsk);
    check("the cache request names the folder, keeps the size limit, and says Jarvis will ask first",
      /move Stremio's cache to E:\\New and keep its current size limit/.test(P.cacheRequestText("E:\\New")) && /stremio_prepare/.test(P.cacheRequestText("E:\\New")) && /confirm/.test(P.cacheRequestText("E:\\New")));

    const fixable = status({ config: { state: "stale", sentence: "A Stremio path saved in stremio.json no longer exists.", can_repair: true, repair_needs_copy: false,
      repair_pair: { runtime_exe: "C:\\S\\r.exe", server_js: "C:\\S\\server.js", how: "in the usual folders" } } });
    const rep = P.repairModel(fixable);
    check("repair model: one sentence from the hub, a button, what was found and what is written",
      rep.offer && rep.label === "Re-detect and save" && /no longer exists/.test(rep.sentence) && /C:\\S\\r\.exe/.test(rep.detail) && /Only the two paths are written/.test(rep.detail) && rep.needsCopy === false);
    const brokenOffer = P.repairModel(status({ config: { state: "broken", sentence: "x", can_repair: true, repair_needs_copy: true, repair_pair: { runtime_exe: "a", server_js: "b" } } }));
    check("repair model: a broken file says it is moved aside to stremio.json.bad, never deleted", brokenOffer.needsCopy && /stremio\.json\.bad/.test(brokenOffer.detail) && /nothing is deleted/.test(brokenOffer.detail));
    check("repair model: a good file or a hub without it offers nothing", P.repairModel(status()).offer === false && P.repairModel({ ok: true }).offer === false);
    check("a broken file with nothing found gets the hub's sentence and no button",
      P.repairModel(status({ config: { state: "broken", sentence: "stremio.json can't be read.", can_repair: false } })).offer === false
      && P.repairBrokenFileHint(status({ config: { state: "broken", sentence: "stremio.json can't be read.", can_repair: false } })) === "stremio.json can't be read.");

    const saves = [];
    const deps = (o) => Object.assign({ confirm: async () => true, save: async (b) => { saves.push(b); return { ok: true, saved: b }; } }, o || {});
    check("save flow: no body -> unchanged, no request", (await P.settingsSaveFlow(deps(), null)).status === "unchanged" && saves.length === 0);
    let r = await P.settingsSaveFlow(deps(), { web: { dir: "D:\\sw" } });
    check("save flow: a body is sent as it is and reported saved", r.status === "saved" && eq(saves, [{ web: { dir: "D:\\sw" } }]));
    r = await P.settingsSaveFlow(deps({ save: async () => { throw new Error("the stremio-web folder doesn't exist: D:\\sw"); } }), { web: { dir: "D:\\sw" } });
    check("save flow: the hub's refusal comes back word for word", r.status === "refused" && r.error === "the stremio-web folder doesn't exist: D:\\sw");

    saves.length = 0;
    r = await P.repairFlow(deps(), fixable);
    check("repair flow: a stale file is repaired with one request and no question", r.status === "saved" && eq(saves, [{ repair: true }]));
    saves.length = 0;
    const asked = [];
    const brokenStatus = status({ config: { state: "broken", sentence: "x", can_repair: true, repair_needs_copy: true, repair_pair: { runtime_exe: "a", server_js: "b" } } });
    r = await P.repairFlow(deps({ confirm: async (t) => { asked.push(t); return false; } }), brokenStatus);
    check("repair flow: a broken file asks first, and a no sends nothing", r.status === "cancelled" && saves.length === 0 && asked.length === 1 && /stremio\.json\.bad/.test(asked[0]) && /Nothing is deleted/.test(asked[0]));
    r = await P.repairFlow(deps({ confirm: async (t) => { asked.push(t); return true; } }), brokenStatus);
    check("repair flow: a yes sends repair with keep_copy", r.status === "saved" && eq(saves, [{ repair: true, keep_copy: true }]));
    check("repair flow: nothing to repair sends nothing", (await P.repairFlow(deps(), status())).status === "none" && saves.length === 1);

    let r2 = await P.startWebFlow({ start: async (ms) => ({ ok: true, sentence: "stremio-web is running at http://127.0.0.1:8080." }) }, status());
    check("start web: success shows the hub's sentence", r2.status === "ok" && /running at/.test(r2.text));
    r2 = await P.startWebFlow({ start: async () => { const e = new Error("x"); e.data = { ok: false, error: "set web.dir first" }; throw e; } }, status());
    check("start web: a refusal shows the hub's error", r2.status === "refused" && r2.text === "set web.dir first");
    r2 = await P.startWebFlow({ start: async () => { const e = new Error("t"); e.timedOut = true; throw e; } }, status());
    check("start web: a time-out says to press Check again", r2.status === "timeout" && /Check again/.test(r2.text));
  }

  // ---- brief 29: the same rows on the page, through the real build and its buttons
  {
    const st = { ok: true, state: "ready", title: "Installed, not running", sentence: "Stremio is installed.", can_start: true, address: "http://127.0.0.1:11470", path: {}, wait_seconds: 60,
      web: { url: "http://127.0.0.1:8080", answers: false, configured: false },
      web_config: { url: "http://127.0.0.1:8080", dir: "", dir_exists: false, start_cmd: "pnpm start", default_url: "http://127.0.0.1:8080", default_start_cmd: "pnpm start", server_url: "http://127.0.0.1:11470", default_server_url: "http://127.0.0.1:11470" },
      cache: { known: false, note: "Start the server to see it." },
      config: { state: "missing", sentence: "There is no stremio.json yet.", can_repair: true, repair_needs_copy: false, repair_pair: { runtime_exe: "C:\\S\\r.exe", server_js: "C:\\S\\server.js", how: "in the usual folders" } } };
    const w = world({ stremio: st, pathsReply: { body: Object.assign({}, st, { saved: {} }) }, startReply: { body: { ok: true, sentence: "stremio-web is running at http://127.0.0.1:8080." } } });
    await w.section.build({ ui: w.ui });
    await new Promise((r) => setTimeout(r, 20));
    const labels = w.shells.map((r) => r.label);
    check("the page has a row for the settings file, the server address, the stremio-web folder, command and address, and the cache",
      ["Stremio's settings file", "Server address", "stremio-web folder", "stremio-web start command", "stremio-web address", "Stremio's cache"].every((l) => labels.includes(l)));
    const find = (kids, text) => { const out = []; const walk = (n) => { if (!n || typeof n !== "object") return; if (n.attrs && n.attrs.text === text) out.push(n); (n.kids || []).forEach(walk); (n.el ? [n.el] : []).forEach(walk); }; kids.forEach(walk); return out[0]; };
    const row = (l) => w.shells.find((r) => r.label === l);
    const repairBtn = find([row("Stremio's settings file")], "Re-detect and save");
    check("the repair button is on the page", !!repairBtn && typeof repairBtn.attrs.onclick === "function");
    w.calls.length = 0;
    await repairBtn.attrs.onclick();
    const repairCall = w.calls.find((c) => c.url === "/api/hub/stremio/paths");
    check("pressing it posts {repair: true} to the paths route, with the hub header", !!repairCall && eq(repairCall.body, { repair: true }) && repairCall.method === "POST");
    const startWeb = find([...w.shells.map((r) => r.el), ...[]], "Start stremio-web");
    check("the page source sends the literal web to the start route and nothing else for it", /hubPost\("\/api\/hub\/stremio\/start", \{ what: "web" \}, ms\)/.test(src));
  }


  // ---- brief 25 (N20): the page's structure, search, defaults, reset, timing, source blocks and the two tables
  {
    const ALL = ["library_root", "folders.video", "folders.audio", "folders.file", "ext_folders", "rate_limit_kbps", "schedule", "concurrency.total", "concurrency.http",
      "concurrency.ytdlp", "concurrency.stremio", "concurrency.qbittorrent", "qbittorrent.prefer_for_magnets", "qbittorrent.category", "qbittorrent.tag",
      "qbittorrent.save_subfolder", "qbittorrent.after_done", "qbittorrent.cancel_deletes_data", "qbittorrent.metadata_timeout_s", "qbittorrent.stall_minutes",
      "qbittorrent.poll_s", "http.stall_seconds", "http.max_retries", "http.segments", "http.segment_min_bytes", "stremio.stall_seconds", "stremio.max_retries",
      "stremio.metadata_timeout", "stremio.start_wait_seconds", "after_download.open", "notify.enabled", "notify.min_seconds", "notify.coalesce_s", "notify.providers",
      "notify.done.enabled", "unknown_settle_hours", "allow_private_hosts", "brand_new.key"];
    const SET = { library_root: "", folders: { video: "Video", audio: "Audio", file: "Files" }, ext_folders: {}, rate_limit_kbps: 0, schedule: [],
      concurrency: { total: 6, http: 3, ytdlp: 2, stremio: 3, qbittorrent: 2 },
      qbittorrent: { prefer_for_magnets: false, category: "", tag: "jarvis", save_subfolder: "Torrents", after_done: "keep", cancel_deletes_data: true, metadata_timeout_s: 900, stall_minutes: 0, poll_s: 1 },
      http: { stall_seconds: 60, max_retries: 3, segments: 1, segment_min_bytes: 33554432 },
      stremio: { stall_seconds: 90, max_retries: 3, metadata_timeout: 90, start_wait_seconds: 60 }, after_download: { open: "off" },
      notify: { enabled: true, min_seconds: 0, coalesce_s: 8, providers: true, done: { enabled: true, level: null } }, unknown_settle_hours: 24, allow_private_hosts: [], brand_new: { key: true } };
    const DEF = JSON.parse(JSON.stringify(SET));
    DEF.concurrency.total = 4;
    const SOURCES = { ok: true, engines: [
      { id: "http", label: "HTTP", state: "ready", reason: "", config_file: null }, { id: "qbittorrent", label: "qBittorrent", state: "needs_setup", reason: "not set up", config_file: "~/.jarvis/qbittorrent.json" },
      { id: "stremio", label: "Stremio (torrent)", state: "not_running", reason: "", config_file: null }, { id: "ytdlp", label: "yt-dlp", state: "broken", reason: "", config_file: null }], providers: [] };
    const mk = (extra) => world(Object.assign({ settings: JSON.parse(JSON.stringify(SET)), defaults: DEF, writable: ALL, prefsWritable: ALL.filter((p) => p !== "library_root" && p !== "allow_private_hosts"),
      events: ["done"], openModes: ["off", "reveal"], sources: SOURCES, providers: [steamP({ notify_min_update_gb: 1 }), playniteP] }, extra || {}));
    const w = mk();
    const blocks = await w.section.build({ ui: w.ui });
    const page = blocks[0].__dls;
    const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.kids || []).forEach((k) => walk(k, f)); (n.el ? [n.el] : []).forEach((k) => walk(k, f)); };
    const nodes = (root, pred) => { const out = []; walk(root, (n) => { if (pred(n)) out.push(n); }); return out; };
    const P = w.pure;

    check("the page is ONE element holding a search bar, a rail and the sections", blocks.length === 1 && blocks[0].tag === "div" && blocks[0].attrs.class === "dls");
    check("the rail has the seven sections of N20, in order, and Other only for a key with no home",
      eq(page.sectionIds(), ["files", "speed", "running", "sources", "notifications", "finish", "safety", "other"]) && eq(P.PAGE_SECTIONS.map((x) => x.title),
        ["Files", "Speed and schedule", "Running at once", "Sources", "Notifications", "When a download finishes", "Safety"]));
    const w0 = mk({ writable: ALL.filter((p) => p !== "brand_new.key") });
    check("without such a key there is no Other section", !(await w0.section.build({ ui: w0.ui }))[0].__dls.sectionIds().includes("other"));
    const sec = (p) => page.rowFor(p).section;
    check("each setting is in the section a person would look in",
      sec("library_root") === "files" && sec("folders.audio") === "files" && sec("ext_folders") === "files" && sec("rate_limit_kbps") === "speed" && sec("schedule") === "speed"
      && sec("concurrency.total") === "running" && sec("notify.enabled") === "notifications" && sec("notify.done.enabled") === "notifications" && sec("after_download.open") === "finish"
      && sec("allow_private_hosts") === "safety" && sec("unknown_settle_hours") === "sources" && sec("brand_new.key") === "other");
    check("an engine's settings sit in its own block: HTTP, yt-dlp and Stremio",
      eq(page.entries.filter((e) => e.source === "http").map((e) => e.path).sort(), ["concurrency.http", "http.max_retries", "http.segment_min_bytes", "http.segments", "http.stall_seconds"])
      && eq(page.entries.filter((e) => e.source === "ytdlp" && e.path).map((e) => e.path), ["concurrency.ytdlp"])
      && page.entries.filter((e) => e.source === "stremio").map((e) => e.path).sort().join() === ",concurrency.stremio,stremio.max_retries,stremio.metadata_timeout,stremio.stall_seconds,stremio.start_wait_seconds");
    check("every writable key with a label has a row, none is dropped, and a key the page has no wording for still gets a generic one",
      ALL.every((p) => !!page.rowFor(p)) && page.rowFor("brand_new.key").spec.type === "bool");
    check("nothing about retention appears while the hub doesn't report it writable", !page.entries.some((e) => /retention|keep history/i.test(e.label + e.path)));
    check("the page adds no setting of its own: every setting row is one the hub listed", page.entries.filter((e) => e.kind === "setting" || e.kind === "table").every((e) => ALL.includes(e.path)));

    // defaults, reset, timing
    const conc = page.rowFor("concurrency.total");
    check("a row says its default, and offers Reset only when the value differs", conc.info().defaultText === "Default: 4" && conc.info().differs === true
      && page.rowFor("concurrency.http").info().differs === false && page.rowFor("concurrency.http").info().defaultText === "Default: 3");
    check("defaults read in words: Off/On, the folder default, 'none', a unit", P.formatValue({ type: "bool" }, false) === "Off" && P.formatValue({ path: "library_root", type: "text" }, "") === "the default Jarvis downloads folder"
      && P.formatValue({ type: "hosts" }, []) === "none" && P.formatValue({ type: "int", unit: "KB/s" }, 0) === "0 KB/s" && P.formatValue({ path: "schedule", type: "list" }, [1, 2]) === "2 windows"
      && P.formatValue({ path: "notify.done.level", type: "choice" }, null) === "follow Settings > Notifications" && P.formatValue({ type: "choice", path: "after_download.open" }, "reveal") === "Show the file in its folder");
    w.calls.length = 0;
    await conc.reset();
    const run = w.calls.filter((c) => c.url === "/api/tools/run").pop();
    check("Reset writes the hub's default through the same tool as a change, and the row goes back to its default",
      run && run.body.name === "dl_prefs" && eq(run.body.arguments, { set: { "concurrency.total": 4 } }) && conc.info().differs === false);
    const ctlTotal = w.controls.find((c) => c.spec.path === "concurrency.total");
    check("and the control shows the default again", ctlTotal.last === 4);
    w.calls.length = 0;
    await conc.reset();
    check("Reset on a row already at its default does nothing", w.calls.length === 0);
    await ctlTotal.commit(9);
    check("changing a row makes it differ at once, with no reload", conc.info().differs === true);
    const wl = mk();
    const pl = (await wl.section.build({ ui: wl.ui }))[0].__dls;
    const lib = wl.controls.find((c) => c.spec.path === "library_root");
    wl.calls.length = 0;
    await lib.commit("D:\\Media");
    check("a change to the library folder still goes through dl_settings, which asks first", wl.calls.some((c) => c.url === "/api/tools/preview" && c.body.name === "dl_settings"));
    wl.confirmRequired = false;
    const noDef = world({ settings: JSON.parse(JSON.stringify(SET)), writable: ALL, prefsWritable: ALL, events: [], openModes: [] });
    const pn = (await noDef.section.build({ ui: noDef.ui }))[0].__dls;
    check("a hub that reports no defaults gets no default text and no Reset (the page doesn't guess)", pn.rowFor("concurrency.total").info().hasDefault === false
      && pn.rowFor("concurrency.total").info().defaultText === "" && pn.rowFor("concurrency.total").info().differs === false);
    check("the timing is shown for keys whose reading was checked in the hub's code",
      P.timingFor("rate_limit_kbps") === "within a few seconds" && P.timingFor("schedule") === "within a few seconds" && P.timingFor("concurrency.http") === "within a few seconds"
      && P.timingFor("notify.done.enabled") === "from the next announcement" && P.timingFor("after_download.open") === "for downloads that finish after the change"
      && P.timingFor("http.stall_seconds") === "for the next download that starts" && P.timingFor("stremio.metadata_timeout") === "for the next download that starts"
      && /retry/.test(P.timingFor("http.max_retries")) && /other tools/.test(P.timingFor("unknown_settle_hours")));
    check("and nothing for the others: no guess", ["library_root", "folders.video", "ext_folders", "allow_private_hosts", "stremio.start_wait_seconds", "brand_new.key", "notify"].every((k) => P.timingFor(k) === ""));
    check("a row's meta says 'Takes effect ...' when it is known and nothing when it isn't", /^Takes effect within a few seconds/.test(conc.info().timing) && page.rowFor("library_root").info().timing === "");

    // search
    page.setQuery("speed");
    check("search narrows rows by label and help across every section", page.visiblePaths().includes("rate_limit_kbps") && page.visiblePaths().includes("schedule") && !page.visiblePaths().includes("library_root")
      && page.visiblePaths().length < 6);
    page.setQuery("shared by everything");
    check("it searches the help text too", eq(page.visiblePaths(), ["rate_limit_kbps"]));
    page.setQuery("torrent retries");
    check("every word has to match", eq(page.visiblePaths(), ["stremio.max_retries"]) && page.sectionIds().length > 0);
    page.setQuery("stremio");
    check("searching a source's name shows that block with all its settings and its card", page.visiblePaths().includes("stremio.stall_seconds") && page.visiblePaths().includes("concurrency.stremio")
      && page.entries.find((e) => e.kind === "card" && e.source === "stremio").visible === true && !page.visiblePaths().includes("http.stall_seconds"));
    check("a block with a hit opens by itself; one with none is hidden", page.sourceBlocks.find((b) => b.id === "stremio").details.open === true && page.sourceBlocks.find((b) => b.id === "http").node.hidden === true);
    page.setQuery("zzz nothing");
    check("a search nothing matches shows no rows", page.visiblePaths().length === 0);
    page.setQuery("");
    check("clearing the search shows everything and folds the blocks back", page.entries.every((e) => e.visible) && page.sourceBlocks.every((b) => b.details.open === false && b.node.hidden === false));
    // changed from default
    page.setChangedOnly(true);
    check("'Changed from default' shows only the rows whose value differs (and hides the card)", eq(page.visiblePaths(), ["concurrency.total"]) && page.entries.find((e) => e.kind === "card").visible === false);
    await conc.reset();
    check("and a reset row leaves it at once", page.visiblePaths().length === 0);
    page.setChangedOnly(false);

    // the Sources blocks
    check("one block per engine, in the Sources tab's order, with its name", eq(page.sourceBlocks.filter((b) => ["http", "qbittorrent", "stremio", "ytdlp"].includes(b.id)).map((b) => b.label), ["HTTP", "qBittorrent", "Stremio (torrent)", "yt-dlp"]));
    const ps = P.planSources({ providers: [steamP({}), playniteP] }, SOURCES, []);
    check("each block carries the Sources tab's status word and dot", eq(ps.filter((x) => x.kind === "engine").map((x) => [x.id, x.word, x.tone]), [["http", "Ready", "ok"], ["qbittorrent", "Needs setup", "warn"], ["stremio", "Not running", "warn"], ["ytdlp", "Broken", "bad"]]));
    check("a program that reports its downloads gets a block too, with its own dot", ps.filter((x) => x.kind === "provider").length === 2 && P.providerDot({ enabled: false }).tone === "off" && P.providerDot({ last_error: "x" }).tone === "bad"
      && P.providerDot({ live: 2, stale: 2 }).word === "Not reporting" && P.providerDot({ live: 2, stale: 1 }).tone === "ok");
    const anchorOf = (b) => b.node.attrs["data-settings-anchor"];
    check("the blocks' anchors are the ones the Sources tab's Configure buttons use: stremio, http, yt-dlp", anchorOf(page.sourceBlocks.find((b) => b.id === "stremio")) === "stremio"
      && anchorOf(page.sourceBlocks.find((b) => b.id === "http")) === "http" && anchorOf(page.sourceBlocks.find((b) => b.id === "ytdlp")) === "yt-dlp" && P.anchorOf("Other tools' downloads") === "other-tools-downloads");
    check("the provider group keeps the anchor its Settings button uses", nodes(blocks[0], (n) => n.attrs && n.attrs["data-settings-anchor"] === "other-tools-downloads").length === 1);
    check("a source's block is folded until someone asks, and scrolling to it opens it", page.sourceBlocks.every((b) => b.details.open === false || b.details.open === undefined)
      && (() => { const b = page.sourceBlocks.find((x) => x.id === "stremio"); b.node.scrollIntoView(); return b.details.open === true; })());
    const wf = mk({ sources: null });
    const pf = (await wf.section.build({ ui: wf.ui }))[0].__dls;
    check("when the sources answer can't be read the three engines that have settings still get their blocks", eq(pf.sourceBlocks.map((b) => b.id).slice(0, 3), ["http", "ytdlp", "stremio"]));
    {
      const wn = mk({ sources: Object.assign({}, SOURCES, { engines: SOURCES.engines.concat([{ id: "mystery", label: "Mystery", state: "off", reason: "", config_file: "~/.jarvis/mystery.json" }]) }) });
      const pn2 = (await wn.section.build({ ui: wn.ui }))[0].__dls;
      const b = pn2.sourceBlocks.find((x) => x.id === "mystery");
      check("a source with no settings says so, and the page shows its setup file",
        nodes(b.node, (n) => n.attrs && /no settings here/.test(n.attrs.text || "") && /mystery\.json/.test(n.attrs.text || "")).length === 1);
    }
    {
      // ---- the qBittorrent block: every hub setting has words and a home, and the connection card sits at its top
      const QB = ["concurrency.qbittorrent", "qbittorrent.after_done", "qbittorrent.cancel_deletes_data", "qbittorrent.category", "qbittorrent.metadata_timeout_s",
        "qbittorrent.poll_s", "qbittorrent.prefer_for_magnets", "qbittorrent.save_subfolder", "qbittorrent.stall_minutes", "qbittorrent.tag"];
      check("every qBittorrent setting the hub lists is a row in the qBittorrent block, not in Other",
        eq(page.entries.filter((e) => e.source === "qbittorrent" && e.path).map((e) => e.path).sort(), QB) && QB.every((p) => sec(p) === "sources"));
      check("each has a label and help in words, never the raw key", QB.every((p) => { const r = page.rowFor(p); return r.label && r.label !== p.replace(/[._]/g, " ") && !/[._]/.test(r.label) && r.help; }));
      check("the choice shows its own words, in the row and in 'Default: ...'", (() => {
        const row = page.rowFor("qbittorrent.after_done");
        return row.spec.choices.length === 3 && row.info().defaultText === "Default: Keep it seeding";
      })());
      check("the timing is only claimed where the engine reads the key when a run starts, or when a magnet link is added",
        P.timingFor("qbittorrent.poll_s") === "for the next torrent that starts" && P.timingFor("qbittorrent.prefer_for_magnets") === "for the next magnet link you add");
      check("the dangerous ones need the confirm-gated tool, the everyday ones don't", (() => {
        const pay = { prefs_writable: ["qbittorrent.poll_s", "qbittorrent.category"] };
        return P.toolFor("qbittorrent.poll_s", pay) === "dl_prefs" && P.toolFor("qbittorrent.cancel_deletes_data", pay) === "dl_settings";
      })());

      const wq = mk();
      const pq = (await wq.section.build({ ui: wq.ui }))[0].__dls;
      await new Promise((r) => setTimeout(r, 0));
      const card = pq.entries.find((e) => e.kind === "card" && e.source === "qbittorrent");
      check("the connection card is the first thing in the qBittorrent block, and searchable", !!card && pq.entries.filter((e) => e.source === "qbittorrent")[0] === card
        && (() => { pq.setQuery("password"); const v = card.visible === true; pq.setQuery(""); return v; })());
      check("the card read the connection once, from its own route", wq.calls.filter((c) => c.url === "/api/hub/qbittorrent").length === 1 && wq.calls.find((c) => c.url === "/api/hub/qbittorrent").method === "GET");
      const labels = wq.shells.map((r) => r.label);
      check("it has the switch, the address, the user name and the password", ["Use qBittorrent", "Web UI address", "User name", "Password"].every((l) => labels.includes(l)));
      const ctlOf = (label) => wq.controls.find((c) => c.spec.type && !c.spec.path && wq.shells.find((r) => r.el === c.el && r.label === label));
      const byKey = {};
      wq.controls.filter((c) => !c.spec.path && c.spec.type).forEach((c) => { byKey[c.spec.type + ":" + String(c.value)] = c; });
      check("the controls start from what the file holds", byKey["bool:true"] && byKey["text:http://127.0.0.1:8080"] && byKey["text:admin"]);
      wq.calls.length = 0;
      await byKey["text:http://127.0.0.1:8080"].commit("http://localhost:9090");
      const post = wq.calls.find((c) => c.method === "POST");
      check("a change is one POST of that one key, with the hub header", post && post.url === "/api/hub/qbittorrent" && eq(post.body, { address: "http://localhost:9090" })
        && post.headers && post.headers["X-Jarvis-Hub"] === "1" && wq.flashes.some((f) => f.text === "Saved"));
      wq.calls.length = 0;
      await byKey["bool:true"].commit(false);
      check("the switch posts enabled alone", eq(wq.calls.find((c) => c.method === "POST").body, { enabled: false }));

      // the password: typed, sent alone, never drawn back
      const pwShell = wq.shells.find((r) => r.label === "Password");
      const pwNodes = nodes(pwShell.el, () => true);
      const box = pwNodes.find((n) => n.tag === "input" && n.attrs.type === "password");
      const btn = (text) => pwNodes.find((n) => n.tag === "button" && n.attrs.text === text);
      check("the password box is a password field the browser must not fill", box && box.attrs.autocomplete === "new-password" && box.attrs["aria-label"] === "qBittorrent password");
      box.value = "";                      // (the fake element has no value of its own until one is written)
      wq.calls.length = 0;
      btn("Save password").attrs.onclick();
      await new Promise((r) => setTimeout(r, 0));
      check("an empty box sends nothing and says so", wq.calls.length === 0 && wq.flashes.some((f) => /Type the password first/.test(f.text)));
      box.value = "S3cret pw";
      btn("Save password").attrs.onclick();
      await new Promise((r) => setTimeout(r, 0));
      const pwPost = wq.calls.find((c) => c.method === "POST");
      check("Save password sends the password alone, and the box is emptied afterwards", pwPost && eq(pwPost.body, { password: "S3cret pw" }) && box.value === "");
      wq.calls.length = 0;
      btn("Forget it").attrs.onclick();
      await new Promise((r) => setTimeout(r, 0));
      check("Forget it sends clear_password and nothing else", eq(wq.calls.find((c) => c.method === "POST").body, { clear_password: true }));
      check("the page never puts a password in a row, a note or a label", !JSON.stringify(wq.shells.map((r) => [r.label, r.help])).includes("S3cret"));

      // the status sentence
      const pure = P.qbtModel(wq.qbtStatusSample || { ok: true, values: { enabled: true, address: "a", username: "" }, engine: { ok: true }, password_set: true, file: "f", exists: true });
      check("connected reads as connected; not connected shows the engine's own reason; a failed read says it couldn't be read",
        pure.sentence.startsWith("Connected") && pure.tone === "ok"
        && P.qbtModel({ ok: true, values: {}, engine: { ok: false, reason: "can't reach qBittorrent (is it running?)" } }).sentence === "can't reach qBittorrent (is it running?)"
        && P.qbtModel({ ok: false, error: "boom" }).loaded === false && P.qbtModel(null).tone === "bad");
      check("bodies: a key is sent alone, an empty password is no body, forget and release are one flag each",
        eq(P.qbtBody("username", "x"), { username: "x" }) && P.qbtPasswordBody("") === null && P.qbtPasswordBody(null) === null && eq(P.qbtPasswordBody(" a "), { password: " a " })
        && eq(P.qbtForgetBody(), { clear_password: true }) && eq(P.qbtReleaseBody(), { release_settings: true }));

      // a file that also holds behaviour keys wins over the rows: the card says so and offers the way out
      const wo = mk({ qbt: Object.assign({}, wq.win && {}, { ok: true, file: "~/.jarvis/qbittorrent.json", exists: true, problem: null, values: { enabled: true, address: "http://127.0.0.1:8080", username: "" },
        defaults: {}, password_set: false, file_overrides: ["tag", "poll_s"], engine: { ok: false, reason: "can't reach qBittorrent" } }) });
      await wo.section.build({ ui: wo.ui });
      await new Promise((r) => setTimeout(r, 0));
      check("the override text names the rows in the page's own words and says the file wins", /qBittorrent tag, Ask qBittorrent for progress every/.test(P.qbtOverrideText(["tag", "poll_s"]))
        && /file wins/.test(P.qbtOverrideText(["tag"])) && P.qbtOverrideText([]) === "");
      const noteOf = wo.shells.find((r) => r.label === "Connection");
      const overrideEl = noteOf.el.kids[2];
      check("the card shows that note, and the 'Let the rows below decide' button appears with it", /file wins/.test(overrideEl.textContent) && overrideEl.hidden === false);
      // a refused save shows the hub's own sentence on the row and leaves the card standing
      const wr = mk({ qbtPost: { status: 400, body: { ok: false, error: "the Web UI must be on this PC: use 127.0.0.1 or localhost" } } });
      await wr.section.build({ ui: wr.ui });
      await new Promise((r) => setTimeout(r, 0));
      const ca = wr.controls.find((c) => c.spec.type === "text" && c.value === "http://127.0.0.1:8080");
      let refusal = "";
      try { await ca.commit("http://10.0.0.5:8080"); } catch (e) { refusal = e.message; }
      check("a refused address reaches the row as the hub worded it (the control shows it)", /must be on this PC/.test(refusal));
      // an unreadable connection does not break the page
      const wb = mk({ qbt: { ok: false, error: "boom" } });
      const pb = (await wb.section.build({ ui: wb.ui }))[0].__dls;
      await new Promise((r) => setTimeout(r, 0));
      check("when the connection can't be read the rest of the block is still there", pb.entries.filter((e) => e.source === "qbittorrent" && e.path).length === QB.length && !wb.shells.some((r) => r.label === "Web UI address"));
    }
    check("Steam's switch and update size are in its block, with no default or timing claimed", (() => {
      const labels = w.shells.map((r) => r.label);
      return labels.includes("Steam: announce updates of at least") && labels.includes("Steam") && page.entries.filter((e) => e.source === "steam").every((e) => e.kind === "plain" && e.info().differs === false);
    })());

    // the two tables
    const sched = page.entries.find((e) => e.kind === "table" && e.path === "schedule");
    const btn = (root, text) => nodes(root, (n) => n.attrs && n.attrs.text === text)[0];
    const rowsOf = (e) => ({ kids: e.view });          // the table's rows as drawn
    check("the speed windows are a table with column headings and add, remove and Save buttons", nodes(sched.node, (n) => n.tag === "table").length === 1 && nodes(sched.node, (n) => n.tag === "th").length === 5
      && !!btn(sched.node, "Add a window") && !!btn(sched.node, "Save"));
    w.calls.length = 0;
    btn(sched.node, "Add a window").attrs.onclick();
    btn(sched.node, "Add a window").attrs.onclick();
    check("Add a row adds one, Remove takes one out (one Remove button per row)", nodes(rowsOf(sched), (n) => n.attrs && n.attrs.text === "Remove").length === 2);
    nodes(rowsOf(sched), (n) => n.attrs && n.attrs.text === "Remove")[1].attrs.onclick();
    check("removing a row leaves the others", nodes(rowsOf(sched), (n) => n.attrs && n.attrs.text === "Remove").length === 1);
    btn(sched.node, "Add a window").attrs.onclick();
    const w2 = mk({ runResult: { ok: false, error: "schedule window 2: from and to can't be the same time" } });
    const ps2 = (await w2.section.build({ ui: w2.ui }))[0].__dls;
    const sch2 = ps2.entries.find((e) => e.kind === "table" && e.path === "schedule");
    btn(sch2.node, "Add a window").attrs.onclick();
    btn(sch2.node, "Add a window").attrs.onclick();
    await btn(sch2.node, "Save").attrs.onclick();
    check("one Save writes the whole list, and a refusal shows in the hub's own words, unchanged", w2.flashes.some((f) => f.kind === "err" && f.text === "schedule window 2: from and to can't be the same time")
      && eq(w2.calls.filter((c) => c.url === "/api/tools/run").pop().body.arguments.set.schedule.length, 2));
    check("the refusal is attached to the row it names: '... window 2' is the second row", nodes(rowsOf(sch2), (n) => n.attrs && n.attrs.role === "alert").length === 1);
    check("the pure row finder: window N is row N-1; an extension is found by name; no number means no row", P.rowOfError("schedule", "schedule window 2: x", [1, 2, 3]) === 1 && P.rowOfError("schedule", "schedule window 9: x", [1, 2]) === -1
      && P.rowOfError("schedule", "at most 10 schedule windows", [1]) === -1 && P.rowOfError("ext", ".iso: the folder must be a plain sub-folder", [{ ext: "pdf" }, { ext: ".iso" }]) === 1
      && P.rowOfError("ext", "bad extension 'iso!': use something like .iso", [{ ext: "iso!" }]) === 0 && P.rowOfError("ext", "ext_folders must be an object", [{ ext: ".a" }]) === -1);
    const ext = page.entries.find((e) => e.kind === "table" && e.path === "ext_folders");
    check("the folder-per-file-type list is a table too, with its own add button and Save", !!btn(ext.node, "Add a file type") && !!btn(ext.node, "Save") && nodes(ext.node, (n) => n.tag === "th").length === 3);
    const w3 = mk({ settings: Object.assign({}, SET, { schedule: [{ days: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], from: "01:00", to: "07:00", rate_limit_kbps: 50 }] }) });
    const p3 = (await w3.section.build({ ui: w3.ui }))[0].__dls;
    const t3 = p3.entries.find((e) => e.kind === "table" && e.path === "schedule");
    check("a list that differs from its default offers Reset, and Reset writes the default and empties the table", t3.info().differs === true && nodes(rowsOf(t3), (n) => n.attrs && n.attrs.text === "Remove").length === 1);
    w3.calls.length = 0;
    await t3.reset();
    check("(the default of both lists is none)", eq(w3.calls.filter((c) => c.url === "/api/tools/run").pop().body.arguments, { set: { schedule: [] } }) && nodes(rowsOf(t3), (n) => n.attrs && n.attrs.text === "Remove").length === 0);
    check("the lists are searchable like any row", (() => { page.setQuery("midnight"); const v = page.visiblePaths(); page.setQuery(""); return eq(v, ["schedule"]); })());
  }

  // ---- safety greps
  check("the page still writes only through the three allowed tools and never assigns HTML", /RUNNABLE = \{ dl_prefs: 1, dl_settings: 1, dl_provider_prefs: 1 \}/.test(src) && !/innerHTML|insertAdjacentHTML|eval\(/.test(src));
  check("the new row has no limit or range of its own", !/Number\.isInteger|throw new Error\("[^"]*(must be|between)/.test(src));
  check("the row is found by Settings search", /steam update size threshold gb/.test(src));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
