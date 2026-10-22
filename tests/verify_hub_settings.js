// Settings > Downloads, the provider rows (web/public/downloads-settings.js): Steam's update-size threshold (Download Hub N1 / track C).
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
  const o = Object.assign({ providers: [], confirmRequired: false, confirmAnswer: true, runResult: { ok: true } }, opts || {});
  const calls = [];
  const win = {};
  win.fetch = async (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({ url, body });
    const reply = (data) => ({ ok: true, status: 200, statusText: "OK", json: async () => data });
    if (url === "/api/hub/settings") return reply({ ok: true, settings: {}, providers: o.providers, writable: [], prefs_writable: [], events: [], open_modes: [] });
    if (url === "/api/tools/preview") return reply({ confirm_required: o.confirmRequired });
    if (url === "/api/tools/run") return reply({ result: o.runResult });
    return { ok: false, status: 404, statusText: "nope", json: async () => null };
  };
  const confirms = [];
  win.JarvisUI = { confirm: async (c) => { confirms.push(c); return o.confirmAnswer; } };
  vm.runInContext(src, vm.createContext({ window: win, fetch: (...a) => win.fetch(...a), console, setTimeout }), { filename: "downloads-settings.js" });
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

  // ---- safety greps
  check("the page still writes only through the three allowed tools and never assigns HTML", /RUNNABLE = \{ dl_prefs: 1, dl_settings: 1, dl_provider_prefs: 1 \}/.test(src) && !/innerHTML|insertAdjacentHTML|eval\(/.test(src));
  check("the new row has no limit or range of its own", !/Number\.isInteger|throw new Error\("[^"]*(must be|between)/.test(src));
  check("the row is found by Settings search", /steam update size threshold gb/.test(src));

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
