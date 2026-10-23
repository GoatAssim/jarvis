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
    stremioHang: false, pathsReply: null, startReply: null, hang: null }, opts || {});
  const calls = [];
  const win = {};
  win.fetch = async (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({ url, body, headers: (init && init.headers) || null, method: (init && init.method) || "GET" });
    const reply = (data, status) => ({ ok: !status || status < 400, status: status || 200, statusText: status ? "ERR" : "OK", json: async () => data });
    if (o.hang && o.hang.test(url)) return new Promise((resolve, reject) => { if (init && init.signal) init.signal.addEventListener("abort", () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); }); });
    if (url === "/api/hub/settings") return reply({ ok: true, settings: o.settings, providers: o.providers, writable: o.writable, prefs_writable: o.prefsWritable, events: [], open_modes: [] });
    if (url === "/api/hub/stremio") return o.stremioHang ? new Promise(() => {}) : (o.stremio && o.stremio.ok === false ? reply(o.stremio, 400) : reply(o.stremio));
    if (url === "/api/hub/stremio/paths" && o.pathsReply) return reply(o.pathsReply.body, o.pathsReply.status);
    if (url === "/api/hub/stremio/start" && o.startReply) return reply(o.startReply.body, o.startReply.status);
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
    makeControl: (spec, value, commit, status) => { const c = { spec, value, commit, status, el: fakeEl("ctl") }; controls.push(c); return c; },
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
    const grp = blocks.find((b) => b && b.title === "Lost downloads");
    const row = w.shells.find((r) => r.label === "Give up on a lost download after (hours)");
    check("with the hub reporting it writable, the row is on the page in its own group", !!grp && !!row && grp.rows.length === 1);
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
    check("an older hub that doesn't list the key gets no row (not a dead one)", !blocks.some((b) => b && b.title === "Lost downloads") && !w.shells.some((r) => /Give up on a lost/.test(r.label)));
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
    check("a status that can't be read doesn't break the section: the card is still there", blocks.some((b) => b && b.title === "Stremio"));
  }
  {
    const code = src.slice(src.indexOf("function stremioBlock"), src.indexOf("function listEditor"));
    check("the card's buttons: Check again, Copy diagnostic, Locate program, Locate server.js, Start; no HTML is assigned",
      /"Check again"/.test(code) && /"Copy diagnostic"/.test(code) && /Locate program/.test(code) && /Locate server\.js/.test(code) && /text: "Start"/.test(code) && !/innerHTML|insertAdjacentHTML/.test(src));
    check("Start goes to the one start route with an empty body and its own time-out; Locate saves through the paths route",
      /hubPost\("\/api\/hub\/stremio\/start", \{\}, ms\)/.test(code) && /hubPost\("\/api\/hub\/stremio\/paths", body, 30000\)/.test(code));
    check("Start is disabled while anything works and unless the hub said can_start", /bStart\.disabled = !!state\.busy \|\| !m\.canStart/.test(code) && /all\.forEach\(\(b\) => \{ b\.disabled = !!word; \}\)/.test(code));
    check("Locate uses the existing picker (JarvisPathPicker.open) and no path is typed into the page", /global\.JarvisPathPicker/.test(code) && /picker\.open\(o\)/.test(code));
    check("the card never writes anything but through those two routes (no tool, no other fetch)", !/runHubTool|postJson/.test(code)
      && eq([...new Set(code.match(/\/api\/hub\/[a-z\/]+/g))].sort(), ["/api/hub/stremio", "/api/hub/stremio/paths", "/api/hub/stremio/start"]));
  }

  // ---- safety greps
  check("the page still writes only through the three allowed tools and never assigns HTML", /RUNNABLE = \{ dl_prefs: 1, dl_settings: 1, dl_provider_prefs: 1 \}/.test(src) && !/innerHTML|insertAdjacentHTML|eval\(/.test(src));
  check("the new row has no limit or range of its own", !/Number\.isInteger|throw new Error\("[^"]*(must be|between)/.test(src));
  check("the row is found by Settings search", /steam update size threshold gb/.test(src));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
