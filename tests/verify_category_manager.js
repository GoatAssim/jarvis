// L.12 follow-up: category-manager.js (Manage categories) - the pure helpers, and the
// whole dialog chain driven against a fake JarvisUI and a fake fetch. No browser, no
// npm install:  node tests/verify_category_manager.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "web", "public", "category-manager.js"), "utf8");
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("ok      " + name); }
  else { fail++; console.log("FAILED  " + name + (detail !== undefined ? "  -> " + JSON.stringify(detail) : "")); }
}

function load(win) {
  const ctx = vm.createContext({ window: win, console, fetch: win.fetch });
  vm.runInContext(SRC, ctx);
  return win.JarvisCategoryManager;
}

// ---- pure ------------------------------------------------------------------------------
{
  const M = load({});
  const P = M._pure;
  const rows = [{ name: "Media", count: 3 }, { name: "Work", count: 1 }];
  check("countLabel pluralises per kind", P.countLabel(1, "daemon") === "1 service" && P.countLabel(2, "daemon") === "2 services"
    && P.countLabel(1, "command") === "1 command" && P.countLabel(0, "command") === "0 commands");
  const opts = P.listOptions(rows, "command");
  check("listOptions: one per category with the count as the hint", opts.length === 2 && opts[0].value === "Media" && opts[0].hint === "3 commands" && opts[1].hint === "1 command");
  check("listOptions tolerates junk", P.listOptions(null, "daemon").length === 0);
  check("mergeTarget finds an existing name case-insensitively", P.mergeTarget(rows, "Work", "media").name === "Media");
  check("mergeTarget: a new name is not a merge", P.mergeTarget(rows, "Work", "Fresh") === null);
  check("mergeTarget: a different spelling of itself is not a merge", P.mergeTarget(rows, "Media", "MEDIA") === null);
  check("mergeTarget: blank is nothing", P.mergeTarget(rows, "Work", "  ") === null);
  check("readRenameAnswer: unchanged or blank means nothing to do", P.readRenameAnswer(rows, "Work", "Work").action === "none" && P.readRenameAnswer(rows, "Work", "   ").action === "none" && P.readRenameAnswer(rows, "Work", null).action === "none");
  const a = P.readRenameAnswer(rows, "Work", "  Home   office ");
  check("readRenameAnswer: collapses spaces and reports no merge", a.action === "rename" && a.to === "Home office" && a.merge === null);
  const b = P.readRenameAnswer(rows, "Work", "media");
  check("readRenameAnswer: a name in use carries the row it would join", b.action === "rename" && b.merge && b.merge.name === "Media");
  check("a case-only rename is a rename, not a no-op", P.readRenameAnswer(rows, "Media", "MEDIA").action === "rename");
}

// ---- the dialog chain --------------------------------------------------------------------
// script: answers the fake UI gives, in order. Each entry is { fn, value } and fn must
// be the method that is called, so a flow that asks something unexpected fails loudly.
function harness(initial, script) {
  const state = { rows: initial.slice(), calls: [], toasts: [], dialogs: [], changed: 0 };
  const queue = script.slice();
  const take = (fn, arg) => {
    const next = queue.shift();
    state.dialogs.push({ fn, arg });
    if (!next || next.fn !== fn) { state.unexpected = { fn, expected: next && next.fn }; return Promise.resolve(null); }
    return Promise.resolve(typeof next.value === "function" ? next.value(arg) : next.value);
  };
  const win = {
    JarvisUI: {
      choose: (o) => take("choose", o), prompt: (o) => take("prompt", o),
      confirm: (o) => take("confirm", o), dialog: (o) => take("dialog", o),
    },
    JarvisHost: { toast: (m, k) => state.toasts.push([m, k]) },
    fetch: async (url, init) => {
      const method = (init && init.method) || "GET";
      const body = init && init.body ? JSON.parse(init.body) : null;
      state.calls.push({ url, method, body });
      const json = (status, data) => ({ ok: status < 400, status, json: async () => data });
      if (state.fail) return json(400, { error: state.fail });
      if (method === "GET") return json(200, { ok: true, categories: state.rows });
      if (url.endsWith("/rename")) {
        state.rows = state.rows.filter((r) => r.name !== body.from).concat([{ name: body.to, count: 1 }]);
        return json(200, { ok: true, changed: 2, merged: !!body.merged });
      }
      if (url.endsWith("/delete")) {
        state.rows = state.rows.filter((r) => r.name !== body.name);
        return json(200, { ok: true, changed: 2 });
      }
      return json(404, { error: "no such route" });
    },
  };
  return { win, state };
}

(async () => {
  {
    const { win, state } = harness([{ name: "Media", count: 3 }, { name: "Work", count: 1 }], [
      { fn: "choose", value: "Work" },          // pick Work
      { fn: "choose", value: "rename" },        // rename it
      { fn: "prompt", value: "Home" },          // to Home
      { fn: "choose", value: null },            // back at the list: Cancel
    ]);
    const M = load(win);
    await M.open({ kind: "command", onChanged: () => { state.changed++; } });
    const post = state.calls.find((c) => c.method === "POST");
    check("rename posts from/to to the command route", post && post.url === "/api/command-categories/rename" && post.body.from === "Work" && post.body.to === "Home", state.calls);
    check("the list is read again after a change (it loops)", state.calls.filter((c) => c.method === "GET").length === 2);
    check("onChanged ran once, after the save", state.changed === 1);
    check("a toast says what happened", state.toasts.length === 1 && /Renamed to/.test(state.toasts[0][0]));
    check("no dialog was asked out of order", !state.unexpected, state.unexpected);
  }
  {
    const { win, state } = harness([{ name: "Media", count: 3 }, { name: "Work", count: 1 }], [
      { fn: "choose", value: "Work" }, { fn: "choose", value: "rename" },
      { fn: "prompt", value: "media" },         // a name in use
      { fn: "confirm", value: false },          // ...but the merge is not confirmed
      { fn: "choose", value: null },
    ]);
    const M = load(win);
    await M.open({ kind: "daemon" });
    check("declining the merge sends nothing", !state.calls.some((c) => c.method === "POST"), state.calls);
    const conf = state.dialogs.find((d) => d.fn === "confirm");
    check("the merge confirm names the target and how many it already has", conf && /Merge into/.test(conf.arg.title) && /3 services/.test(conf.arg.body), conf);
  }
  {
    const { win, state } = harness([{ name: "Media", count: 3 }], [
      { fn: "choose", value: "Media" }, { fn: "choose", value: "rename" },
      { fn: "prompt", value: "Media" },         // typed the same name: Cancel returns the default
      { fn: "choose", value: null },
    ]);
    await load(win).open({ kind: "daemon" });
    check("an unchanged name is treated as cancel", !state.calls.some((c) => c.method === "POST"));
  }
  {
    const { win, state } = harness([{ name: "Media", count: 3 }], [
      { fn: "choose", value: "Media" }, { fn: "choose", value: "delete" },
      { fn: "confirm", value: true },
      { fn: "dialog", value: null },            // nothing left: "No service has a category yet"
    ]);
    await load(win).open({ kind: "daemon" });
    const post = state.calls.find((c) => c.method === "POST");
    check("delete posts the name to the daemon route", post && post.url === "/api/daemon-categories/delete" && post.body.name === "Media");
    const conf = state.dialogs.find((d) => d.fn === "confirm");
    check("the delete confirm is the danger kind, cancel-focused, and says nothing is stopped", conf && conf.arg.level === "danger" && conf.arg.focusCancel === true && /not touched/.test(conf.arg.body), conf);
    check("an empty list ends with the explanation, not a loop", state.dialogs[state.dialogs.length - 1].fn === "dialog" && !state.unexpected, state.unexpected);
  }
  {
    const { win, state } = harness([{ name: "Media", count: 3 }], [
      { fn: "choose", value: "Media" }, { fn: "choose", value: "delete" },
      { fn: "confirm", value: false },
      { fn: "choose", value: null },
    ]);
    await load(win).open({ kind: "command" });
    check("declining a delete sends nothing", !state.calls.some((c) => c.method === "POST"));
  }
  {
    const { win, state } = harness([{ name: "Media", count: 3 }], [
      { fn: "choose", value: "Media" }, { fn: "choose", value: "rename" }, { fn: "prompt", value: "x".repeat(30) },
      { fn: "choose", value: null },
    ]);
    // make the server refuse the rename, like an over-long name would be refused
    const realFetch = win.fetch;
    win.fetch = async (url, init) => (init && init.method === "POST" ? { ok: false, status: 400, json: async () => ({ error: "Category 'xxx...' is longer than 24 characters." }) } : realFetch(url, init));
    let changed = 0;
    await load(win).open({ kind: "command", onChanged: () => { changed++; } });
    check("a refusal is shown as an error toast and changes nothing", state.toasts.some((t) => t[1] === "error" && /longer than 24/.test(t[0])) && changed === 0, state.toasts);
    check("...and the dialog goes on (back to the list), it does not close", state.dialogs.filter((d) => d.fn === "choose").length === 3 && !state.unexpected, state.dialogs.map((d) => d.fn));
  }
  {
    const { win, state } = harness([], [{ fn: "dialog", value: null }]);
    await load(win).open({ kind: "daemon" });
    check("with no categories it only explains", state.dialogs.length === 1 && state.dialogs[0].fn === "dialog" && /No service has a category/.test(state.dialogs[0].arg.body));
  }
  {
    const { win, state } = harness([{ name: "A", count: 1 }], []);
    win.fetch = async () => { throw new Error("offline"); };
    await load(win).open({ kind: "daemon" });
    check("a failed read says so and stops", state.toasts.length === 1 && state.toasts[0][1] === "error" && state.dialogs.length === 0);
  }
  {
    const win = { fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, categories: [] }) }), JarvisHost: { toast() {} } };
    let told = null;
    win.JarvisHost.toast = (m, k) => { told = [m, k]; };
    await load(win).open({ kind: "daemon" });
    check("without the dialog kit it says so instead of throwing", told && told[1] === "error" && /dialog kit/.test(told[0]), told);
  }

  console.log("\n" + pass + " passed, " + fail + " failed");
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
