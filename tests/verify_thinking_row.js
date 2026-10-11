// The Thinking row in Channels > Permissions > Capabilities (L.44 panel row).
// Loads web/public/channels-panel.js whole into a bare context with a tiny DOM
// stand-in, builds the row for a few people and presses its controls against a
// mocked fetch. No npm install, no browser.
//   node tests/verify_thinking_row.js
const fs = require("fs"), path = require("path"), vm = require("vm");
process.on("unhandledRejection", () => {}); // the panel's re-read after a save has no server here
const src = fs.readFileSync(path.join(__dirname, "..", "web", "public", "channels-panel.js"), "utf8");

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.checked = false; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, f) { this.listeners[t] = f; }
  appendChild(c) { this.children.push(c); return c; }
  set innerHTML(h) { this.content = { firstChild: new Node("svg") }; }
}
const doc = {
  createElement: (t) => new Node(t),
  createTextNode: (text) => ({ text: String(text), children: [] }),
  querySelector: () => null,
  activeElement: null,
};
const calls = [];
const win = { document: doc, fetch: async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ ok: true }) }; } };
win.window = win;
vm.runInNewContext(src, win, { filename: "channels-panel.js" });
const T = win.JarvisChannels._pure;

let passed = 0; const failed = [];
function check(name, cond, detail) { if (cond) passed++; else failed.push(name + (detail ? " - " + detail : "")); }
const walk = (n, f) => { if (!n) return; f(n); (n.children || []).forEach((c) => walk(c, f)); };
const all = (n, pred) => { const out = []; walk(n, (x) => { if (pred(x)) out.push(x); }); return out; };
const text = (n) => { let t = ""; walk(n, (x) => { if (x.text) t += x.text + " "; }); return t; };
const radios = (n) => all(n, (x) => x.attrs && x.attrs.role === "radio");
const boxes = (n) => all(n, (x) => x.tag === "input" && x.attrs.type === "checkbox");
const person = (o) => Object.assign({ platform: "discord", user_id: "7", handle: "friend", owner: false, blocked: false }, o);
const lastBody = () => JSON.parse(calls[calls.length - 1].opts.body);
const tick = () => new Promise((r) => setTimeout(r, 5));

check("the panel exports the row", typeof T.thinkingRow === "function");

// default: nothing set
let row = T.thinkingRow(person());
let rs = radios(row);
check("three choices: Default, On, Off", rs.length === 3 && ["Default", "On", "Off"].every((l, i) => text(rs[i]).trim() === l), rs.map((r) => text(r)).join("|"));
check("Default is the checked one", rs[0].attrs["aria-checked"] === "true" && rs[1].attrs["aria-checked"] === "false" && rs[2].attrs["aria-checked"] === "false");
check("there is nothing to lock while on Default: the box is disabled", "disabled" in boxes(row)[0].attrs);
check("the row says only the owner is shown the reasoning", /only you can see that/.test(text(row)));
check("no tag on a person with no setting", all(row, (x) => x.className === "ch-tag").length === 0);

// on + locked
row = T.thinkingRow(person({ thinking: "on", thinking_lock: true }));
rs = radios(row);
check("'on' is the checked choice", rs[1].attrs["aria-checked"] === "true" && rs[0].attrs["aria-checked"] === "false");
check("the lock box is ticked and enabled", boxes(row)[0].checked === true && !("disabled" in boxes(row)[0].attrs));
check("the tag says 'on, locked'", /on, locked/.test(text(row)));

// off, not locked
row = T.thinkingRow(person({ thinking: "off", thinking_lock: false }));
check("'off' is the checked choice, box unticked", radios(row)[2].attrs["aria-checked"] === "true" && boxes(row)[0].checked === false);

// the owner
row = T.thinkingRow(person({ owner: true }));
check("the owner gets no controls", radios(row).length === 0 && boxes(row).length === 0);
check("...and is told how to see the trace", /never limited/.test(text(row)) && /\/thinking on/.test(text(row)));

(async () => {
  // pressing On on a person with no setting
  calls.length = 0;
  row = T.thinkingRow(person());
  radios(row)[1].listeners.click();
  await tick();
  check("choosing On posts to the thinking route", calls.length >= 1 && calls[0].url === "/api/channels/people/discord/7/thinking" && calls[0].opts.method === "POST", JSON.stringify(calls[0]));
  check("...as value on, not locked", JSON.parse(calls[0].opts.body).value === "on" && JSON.parse(calls[0].opts.body).lock === false, calls[0].opts.body);

  // ticking the lock for someone on 'on'
  calls.length = 0;
  row = T.thinkingRow(person({ thinking: "on", thinking_lock: false }));
  boxes(row)[0].listeners.change({ target: { checked: true } });
  await tick();
  check("ticking the lock keeps the value and sets lock", calls.length >= 1 && JSON.parse(calls[0].opts.body).value === "on" && JSON.parse(calls[0].opts.body).lock === true, calls[0] && calls[0].opts.body);

  // moving a locked person to Off keeps the lock
  calls.length = 0;
  row = T.thinkingRow(person({ thinking: "on", thinking_lock: true }));
  radios(row)[2].listeners.click();
  await tick();
  check("switching a locked person to Off keeps them locked", JSON.parse(calls[0].opts.body).value === "off" && JSON.parse(calls[0].opts.body).lock === true, calls[0].opts.body);

  // back to default sends only the value (the server clears the lock itself)
  calls.length = 0;
  row = T.thinkingRow(person({ thinking: "on", thinking_lock: true }));
  radios(row)[0].listeners.click();
  await tick();
  check("choosing Default sends only value: default", calls.length >= 1 && Object.keys(JSON.parse(calls[0].opts.body)).join() === "value" && JSON.parse(calls[0].opts.body).value === "default", calls[0] && calls[0].opts.body);

  // pressing the choice that is already on does nothing
  calls.length = 0;
  row = T.thinkingRow(person({ thinking: "on" }));
  radios(row)[1].listeners.click();
  await tick();
  check("pressing the current choice saves nothing", calls.length === 0);

  console.log(`${passed} passed, ${failed.length} failed`);
  failed.forEach((f) => console.log("FAILED:", f));
  process.exit(failed.length ? 1 : 0);
})();
