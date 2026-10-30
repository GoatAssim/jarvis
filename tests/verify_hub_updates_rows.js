// Menu > Downloads panel (jarvis/actions/hub_ui/tool.js), DH-A: the available updates are listed inside the Downloads list.
//
// Three layers, no network, no browser:
//   1. the pure half (updateRowsFrom, updateSections, updateRowModel ...): what a finding becomes, the sections, the words;
//   2. the source: no scan is ever asked for, updates join the lists only in viewRows(), everything from a finding is drawn with
//      textContent, the Update button runs the Updates page's own flow and does not name its routes;
//   3. the whole panel run against a FAKE DOM and a FAKE hub (a fetch that answers from a list in this file, the real
//      web/public/updates-panel.js for the Update flow): "tested against a fake", never seen in a browser.
//
// Run: node tests/verify_hub_updates_rows.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..");
const ui = path.join(root, "jarvis-cli", "jarvis", "actions", "hub_ui");
const source = fs.readFileSync(path.join(ui, "tool.js"), "utf8");
const html = fs.readFileSync(path.join(ui, "tool.html"), "utf8");
const css = fs.readFileSync(path.join(ui, "tool.css"), "utf8");
const panelSource = fs.readFileSync(path.join(root, "web", "public", "updates-panel.js"), "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () { /* probe */ }).constructor;

let passed = 0;
let failed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log("ok      " + name); } else { failed++; console.log("FAILED  " + name); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const tick = async (n) => { for (let i = 0; i < (n || 20); i++) await new Promise((r) => setTimeout(r, 0)); };
const between = (text, from, to) => { const a = text.indexOf(from); const b = text.indexOf(to, a + 1); return a === -1 || b === -1 ? "" : text.slice(a, b); };

// ------------------------------------------------------------------------------------------------ fixtures
const F = (o) => Object.assign({
  provider: "npm", provider_label: "npm", id: "x", title: "x", installed: "1.0.0", available: "2.0.0", size_bytes: null, source: "npm",
  group: "packages", can_apply: true, apply: null, age_s: 120,
}, o);
const HOSTILE = "<img src=x onerror=alert(1)>\u202e";
function findings() {
  return [
    F({ id: "a", title: "left-pad" }),
    F({ id: "b", title: "lodash" }),
    F({ id: "c", title: HOSTILE }),
    F({ id: "f", title: "broken-pkg", apply: { state: "failed", message: "disk full <b>x</b>" } }),
    F({ provider: "modrinth", provider_label: "Modrinth", id: "m1", title: "Sodium", group: "mods", installed: "0.5.0", available: "0.5.3", size_bytes: 2 * 1024 * 1024, source: "Modrinth" }),
    F({ provider: "steam", provider_label: "Steam", id: "570", title: "Portal 2", group: "games", can_apply: false, source: "Steam" }),
    F({ provider: "someapp", provider_label: "Some App", id: "z", title: "Some App", group: "weird-group" }),
  ];
}
const PROVIDERS = [{ provider: "steam", label: "Steam", apply_problem: null, can_apply: false }];

(async () => {
  const pure = await new AsyncFunction("host", source)({ __hubPureOnly: true });
  check("the pure helpers come back without touching a DOM", pure && typeof pure.updateRowsFrom === "function" && typeof pure.updateSections === "function");

  // ---------------------------------------------------------------------------------------------- 1. the pure half
  const rows = pure.updateRowsFrom({ updates: findings(), providers: PROVIDERS });
  check("every finding becomes one row, marked as an update (owner, kind, state, flag)", rows.length === 7
    && rows.every((r) => r.owner === "update" && r.kind === "update" && r.state === "update" && r.update === true && pure.isUpdateRow(r)));
  check("a row id can never be a download id (it starts with upd:, then provider and id)", rows.every((r) => /^upd:[^:]+:.+/.test(r.id)) && rows[0].id === "upd:npm:a");
  check("an unknown group lands in Other", rows.find((r) => r.local_id === "z").group_key === "other");
  const clean = pure.updateRowsFrom({ updates: [F({ id: "k", title: "a\u0000b\u202ec\u0007d" })] });
  check("control and bidi characters are stripped from every text of a finding", clean[0].title === "abcd");
  check("long text is capped", pure.updateRowsFrom({ updates: [F({ id: "k", title: "t".repeat(900), available: "v".repeat(900) })] })[0].title.length <= 200
    && pure.updateRowsFrom({ updates: [F({ id: "k", available: "v".repeat(900) })] })[0].available.length <= 80);
  check("a finding with no provider or no id, a hidden one, junk and non-objects are dropped; junk input gives no rows",
    pure.updateRowsFrom({ updates: [F({ provider: "" }), F({ id: "" }), F({ id: "h", hidden: true }), null, 7, "x", [], F({ id: "ok" })] }).length === 1
    && pure.updateRowsFrom(null).length === 0 && pure.updateRowsFrom({ updates: "no" }).length === 0 && pure.updateRowsFrom(undefined).length === 0);
  check("the number of rows is capped", pure.updateRowsFrom({ updates: Array.from({ length: 1500 }, (_, i) => F({ id: "n" + i })) }).length === 1000);
  check("can_apply must be exactly true; a finding that cannot be applied carries the reason in words",
    rows.find((r) => r.local_id === "570").can_apply === false && rows.find((r) => r.local_id === "570").why === pure.UPD_NO_UPDATER
    && rows.find((r) => r.local_id === "a").can_apply === true && rows.find((r) => r.local_id === "a").why === ""
    && pure.updateRowsFrom({ updates: [F({ id: "t", can_apply: "yes" })] })[0].can_apply === false);
  check("the provider's own apply problem is shown as the reason when the answer carries one",
    pure.updateRowsFrom({ updates: [F({ id: "t", can_apply: false })], providers: [{ provider: "npm", apply_problem: "its schema is wrong" }] })[0].why === "Its updater can't be used: its schema is wrong");

  const secs = pure.updateSections(rows, null);
  check("one section per group that has findings, in the fixed order Mods, Games, Packages, Apps, Other (no empty Apps)",
    eq(secs.map((s) => s.title), ["Mods", "Games", "Packages", "Other"]) && eq(secs.map((s) => s.count), [1, 1, 4, 1]));
  check("sections start collapsed the first time (nothing remembered as open)", secs.every((s) => s.open === false)
    && pure.updateSections(rows, {}).every((s) => s.open === false));
  check("a group the owner opened is open, the others stay collapsed", eq(pure.updateSections(rows, { mods: true, junk: true }).map((s) => s.open), [true, false, false, false]));
  check("the count is in the label, collapsed or not", pure.updateSectionLabel(secs[2]) === "Updates: Packages \u00B7 4 updates" && pure.updateSectionLabel(secs[0]) === "Updates: Mods \u00B7 1 update");
  check("inside a section the rows are in name order (so a poll never shuffles them)", eq(secs[2].rows.map((r) => r.title), ["<img src=x onerror=alert(1)>", "broken-pkg", "left-pad", "lodash"]));

  const model = pure.updateRowModel(rows.find((r) => r.local_id === "m1"));
  check("a row says 'installed \u2192 available', its size, its source and when it was checked", model.version === "0.5.0 \u2192 0.5.3"
    && model.meta.indexOf("2.0 MB") !== -1 && model.meta.indexOf("via Modrinth") !== -1 && model.meta.some((t) => /^checked /.test(t)));
  check("a version the source did not state is said so, not left blank", pure.updateRowModel(pure.updateRowsFrom({ updates: [F({ id: "t", installed: "", available: "" })] })[0]).version === "version not stated \u2192 version not stated");
  check("an applicable finding offers Update; a failed one offers Try again and shows the program's message as it was",
    pure.updateRowModel(rows.find((r) => r.local_id === "a")).canUpdate === true && pure.updateRowModel(rows.find((r) => r.local_id === "a")).updateLabel === "Update"
    && pure.updateRowModel(rows.find((r) => r.local_id === "f")).updateLabel === "Try again"
    && pure.updateRowModel(rows.find((r) => r.local_id === "f")).apply.detail === "disk full <b>x</b>" && pure.updateRowModel(rows.find((r) => r.local_id === "f")).apply.tone === "bad");
  check("a finding that cannot be applied: no Update, and it says why", pure.updateRowModel(rows.find((r) => r.local_id === "570")).canUpdate === false
    && pure.updateRowModel(rows.find((r) => r.local_id === "570")).why === pure.UPD_NO_UPDATER);
  check("a failed apply with no message still says something", pure.updApplyView({ state: "failed", message: "" }).detail === "No reason was given.");
  check("queued and running are busy (the Update button is off, reads speed up); done, started and failed are not",
    pure.updApplyView({ state: "queued" }).busy && pure.updApplyView({ state: "running" }).busy && !pure.updApplyView({ state: "done" }).busy
    && !pure.updApplyView({ state: "started" }).busy && !pure.updApplyView({ state: "failed" }).busy && pure.updApplyView({ state: "weird" }) === null && pure.updApplyView(null) === null);
  check("updatesBusy sees a queued or running apply", pure.updatesBusy(pure.updateRowsFrom({ updates: [F({ id: "t", apply: { state: "running", message: "" } })] })) === true && pure.updatesBusy(rows) === false);
  check("reads are every few minutes, faster only while an apply is busy", pure.UPD_POLL_MS >= 120000 && pure.UPD_POLL_MS <= 600000 && pure.UPD_BUSY_POLL_MS < pure.UPD_POLL_MS);

  // an update row must be invisible to every count and to every view but "All"
  const dl = (o) => Object.assign({ id: "h_1", owner: "hub", engine: "http", state: "downloading", derived: [], capabilities: [], stale: false, created_at: 1 }, o);
  const downloads = [dl({ id: "h_1" }), dl({ id: "h_2", state: "failed" }), dl({ id: "h_3", state: "done" })];
  const mixed = downloads.concat(rows);
  check("the status chip counts are the same with or without update rows (viewCounts leaves them out)", eq(pure.viewCounts(mixed), pure.viewCounts(downloads)) && pure.viewCounts(mixed).all === 3);
  check("an update row matches no view but All, and the search finds it by name", ["active", "waiting", "scheduled", "failed", "done", "stale"].every((v) => pure.filterRows(rows, { view: v, search: "" }).length === 0)
    && pure.filterRows(rows, { view: "all", search: "left-pad" }).length === 1 && pure.filterRows(mixed, { view: "failed", search: "" }).length === 1);
  const sp = pure.splitUpdates(mixed);
  check("splitUpdates separates the two kinds and loses none", sp.downloads.length === 3 && sp.updates.length === 7 && pure.splitUpdates(null).downloads.length === 0);
  check("overviewStats, needsAttention and runningNow over the downloads are what they were (updates are never passed to them)",
    eq(pure.overviewStats(sp.downloads), pure.overviewStats(downloads)));

  // ---------------------------------------------------------------------------------------------- 2. the source
  check("the panel never asks for a scan: nothing in tool.js names a check route", !/updates\/check/.test(source) && !/updates_check|updates-check/.test(source));
  check("tool.js names only the read and the dismiss route of the updates; apply and dry-run belong to the Updates page's flow",
    source.split('"/api/hub/updates"').length - 1 === 2 && source.split('"/api/hub/updates/dismiss"').length - 1 === 1
    && !/updates\/(apply|dry-run|set)/.test(source));
  check("the Update button runs the Updates page's own flow object (window.JarvisUpdates._flows), not a copy of it",
    /window\.JarvisUpdates/.test(source) && /_flows\(\)/.test(source) && /flows\.updateOne\(/.test(source));
  check("updates join the lists in viewRows() only; the rows array they are kept in is S.upd.rows, never S.rows",
    /downloadViewRows\(\)\.concat\(S\.upd\.on \? S\.upd\.rows : \[\]\)/.test(source) && !/S\.rows\s*=[^=][^;\n]*S\.upd/.test(source) && !/S\.rows\.(push|concat|unshift)\([^)]*upd/i.test(source)
    && !/S\.upd\.rows\s*=\s*S\.rows/.test(source) && source.split("S.upd.rows =").length - 1 === 3);
  check("what counts, orders or acts on downloads still reads S.rows (the running line, Pause all / Resume all, Clear finished, the queue order)",
    /overviewStats\(S\.rows\)/.test(source) && /needsAttention\(S\.rows\)/.test(source) && /runningNow\(S\.rows\)/.test(source) && /dismissableRows\(S\.rows\)/.test(source)
    && /queueOrder\(S\.rows\)/.test(source) && /const counts = viewCounts\(viewRows\(\)\)/.test(source));
  const drawing = between(source, "function updateSectionHeader", "// ---- middle pane") + between(source, "// ---- DH-A: reading the updates", "// ---- the add form");
  check("the update code was found in the source", drawing.length > 3000);
  check("everything from a finding is drawn with textContent: no innerHTML, outerHTML, insertAdjacentHTML, eval or Function in the update code",
    !/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/.test(drawing) && !/innerHTML|outerHTML|insertAdjacentHTML/.test(between(source, "function renderList", "// ---- middle pane")));
  check("updates never make a Jarvis notification: the update code calls neither toast() nor host.notify", !/(^|[^.\w])toast\(|host\.notify/.test(drawing));
  check("the list's signature (pinned by verify_hub_panel.js) is untouched; the update parts have a signature of their own",
    /\[rows, S\.selected, S\.groupBy, S\.live, S\.revealed, S\.showHidden, S\.collapsed, S\.acting\]/.test(source) && /changed\("listupd", \[S\.upd\.open, S\.upd\.on, S\.upd\.error\]\)/.test(source));
  check("the update rows are not selectable: no select() on a click of an update row, and arrow keys walk the downloads only",
    !/select\(row\.id\)/.test(between(source, "function renderUpdateRow", "// ---- middle pane")) && /const rows = splitUpdates\(visibleRows\(\)\)\.downloads;\s*\/\/ an update row is never selected/.test(source));
  check("the switch is remembered under its own key, and the open sections under another", /host\.storage\.get\("show_updates"\)/.test(source) && /host\.storage\.set\("show_updates"/.test(source)
    && /host\.storage\.get\("updates_open"\)/.test(source) && /host\.storage\.set\("updates_open"/.test(source));
  check("a read answer that arrives after the switch went off is thrown away (a sequence number)", /const seq = \+\+S\.upd\.seq;/.test(source) && /seq !== S\.upd\.seq/.test(source));
  check("the timer is stopped when the panel closes", /clearTimeout\(S\.upd\.timer\);\s*\n\}\);/.test(source) || /clearTimeout\(histTimer\); clearTimeout\(S\.upd\.timer\);/.test(source));
  check("tool.html: a Show updates switch sits next to Show hidden, on by default",
    /id="showHidden"[^>]*>Show hidden<\/button>\s*<button[^>]*id="showUpdates"[^>]*aria-pressed="true"[^>]*>Show updates<\/button>/.test(html));
  check("tool.css: the update rows and sections have their own rules at the end of the file", css.lastIndexOf(".row--update") > css.lastIndexOf(".picker__why") && /\.tag--upd \{/.test(css) && /\.section--upd \{/.test(css));

  // ---------------------------------------------------------------------------------------------- 3. the whole panel against a fake
  const cssClasses = (n) => String(n.className || "").split(/\s+/).filter(Boolean);
  function makeDom(htmlText) {
    const created = [];
    class El {
      constructor(tag) {
        this.tag = String(tag).toLowerCase(); this.tagName = this.tag.toUpperCase(); this.children = []; this.parentNode = null;
        this._attrs = {}; this._text = ""; this._on = {}; this._disabled = false; this._hidden = false;
        this.className = ""; this.value = ""; this.title = ""; this.style = {}; this.dataset = {}; this.scrollTop = 0; this.isConnected = true;
        this.checked = false; this.id = ""; this.options = []; this.selectedIndex = 0; this.files = [];
        const self = this;
        this.classList = {
          add: (...c) => { const s = new Set(cssClasses(self)); c.forEach((x) => s.add(x)); self.className = Array.from(s).join(" "); },
          remove: (...c) => { self.className = cssClasses(self).filter((x) => c.indexOf(x) === -1).join(" "); },
          toggle: (c, force) => { const has = cssClasses(self).indexOf(c) !== -1; const want = force === undefined ? !has : !!force; if (want) self.classList.add(c); else self.classList.remove(c); return want; },
          contains: (c) => cssClasses(self).indexOf(c) !== -1,
        };
      }
      get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
      set textContent(v) { this._text = String(v); this.children = []; }
      get childNodes() { return this.children; }
      get firstChild() { return this.children[0] || null; }
      get lastChild() { return this.children[this.children.length - 1] || null; }
      get disabled() { return this._disabled; }
      set disabled(v) { this._disabled = !!v; if (v) this._attrs.disabled = "disabled"; else delete this._attrs.disabled; }
      get hidden() { return this._hidden; }
      set hidden(v) { this._hidden = !!v; }
      appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
      append(...cs) { cs.forEach((c) => this.appendChild(c)); }
      removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
      remove() { if (this.parentNode) this.parentNode.removeChild(this); }
      setAttribute(k, v) {
        this._attrs[k] = String(v);
        if (k === "disabled") this._disabled = true;
        if (k === "hidden") this._hidden = true;
        if (k === "class") this.className = String(v);
        if (k === "id") this.id = String(v);
      }
      getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; }
      hasAttribute(k) { return this._attrs[k] !== undefined; }
      removeAttribute(k) { delete this._attrs[k]; if (k === "disabled") this._disabled = false; if (k === "hidden") this._hidden = false; }
      addEventListener(t, f) { (this._on[t] = this._on[t] || []).push(f); }
      removeEventListener(t, f) { this._on[t] = (this._on[t] || []).filter((x) => x !== f); }
      dispatchEvent() { return true; }
      async click() {
        const ev = { type: "click", target: this, currentTarget: this, stopPropagation() {}, preventDefault() {} };
        for (const f of (this._on.click || []).slice()) await f(ev);
      }
      focus() {} blur() {} select() {} scrollIntoView() {} setSelectionRange() {}
      getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }; }
      closest() { return null; }
      contains() { return false; }
      matches(sel) { return matchesSel(this, sel); }
      querySelector(sel) { return this.querySelectorAll(sel)[0] || (/^#[\w-]+$/.test(sel) ? new El("div") : null); }
      querySelectorAll(sel) {
        const out = [];
        const walk = (n) => { n.children.forEach((c) => { if (matchesSel(c, sel)) out.push(c); walk(c); }); };
        walk(this);
        return out;
      }
    }
    function matchesSel(n, sel) {
      return String(sel).split(",").some((one) => {
        const last = one.trim().split(/\s+/).pop();
        const re = /(^[a-z][a-z0-9]*)|(#[\w-]+)|(\.[\w-]+)|(\[[^\]]+\])/gi;
        let m; let ok = true; let any = false;
        while ((m = re.exec(last))) {
          any = true;
          const t = m[0];
          if (t[0] === "#") ok = ok && n.id === t.slice(1);
          else if (t[0] === ".") ok = ok && n.classList.contains(t.slice(1));
          else if (t[0] === "[") {
            const mm = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(t);
            ok = ok && !!mm && (mm[2] === undefined ? n._attrs[mm[1]] !== undefined : n._attrs[mm[1]] === mm[2]);
          } else ok = ok && n.tag === t.toLowerCase();
        }
        return any && ok;
      });
    }
    const rootEl = new El("div");
    const re = /<([a-z][a-z0-9]*)\b([^>]*?)>/gi;
    let m;
    while ((m = re.exec(htmlText))) {
      const idm = /\sid="([^"]+)"/.exec(" " + m[2]);
      if (!idm) continue;
      const n = new El(m[1]);
      n.setAttribute("id", idm[1]);
      const cm = /\sclass="([^"]*)"/.exec(" " + m[2]);
      if (cm) n.className = cm[1];
      if (/\shidden(\s|$|=)/.test(" " + m[2])) n.hidden = true;
      const pm = /\saria-pressed="([^"]*)"/.exec(" " + m[2]);
      if (pm) n.setAttribute("aria-pressed", pm[1]);
      rootEl.appendChild(n);
    }
    const doc = { hidden: false, body: new El("body"), activeElement: null, createElement: (tag) => { const n = new El(tag); created.push(n.tag); return n; }, addEventListener() {}, removeEventListener() {} };
    rootEl.ownerDocument = doc;
    return { root: rootEl, doc: doc, created: created, El: El };
  }
  const find = (n, pred, out) => { out = out || []; n.children.forEach((c) => { if (pred(c)) out.push(c); find(c, pred, out); }); return out; };
  const hasClass = (n, c) => cssClasses(n).indexOf(c) !== -1;
  const byText = (n, tag, text) => find(n, (c) => c.tag === tag && c.textContent === text);
  const updRowsIn = (r) => find(r, (c) => hasClass(c, "row--update"));
  const updSecsIn = (r) => find(r, (c) => hasClass(c, "section--upd"));
  const rowOf = (r, title) => updRowsIn(r).find((n) => find(n, (c) => hasClass(c, "row__title") && c.textContent === title).length === 1);

  // one panel, wired to the fake hub.  opts: store (host.storage), answer (false = decline every dialog), updates (the hub's list), downloads
  async function harness(opts) {
    const o = Object.assign({ store: {}, downloads: downloads, updates: findings(), providers: PROVIDERS, readFails: false, yes: true }, opts || {});
    const dom = makeDom(html);
    const log = [];                       // every fetch: { method, path, body }
    const confirms = [];
    const toasts = [];
    const notes = [];
    const sets = [];
    const state = { updates: o.updates.map((f) => Object.assign({}, f)), yes: o.yes, readFails: o.readFails };
    const win = {};
    win.JarvisUI = { confirm: async (c) => { confirms.push(c); return state.yes; }, toast: (t) => { toasts.push(t); } };
    win.fetch = async (p, opt) => {
      const method = opt && opt.method ? opt.method : "GET";
      const body = opt && opt.body ? JSON.parse(opt.body) : null;
      log.push({ method: method, path: p, body: body });
      let data = { ok: true };
      if (p === "/api/hub/updates" && method === "GET") {
        if (state.readFails) return { ok: false, status: 500, json: async () => ({ ok: false, error: "hub is down" }) };
        data = { ok: true, updates: state.updates, providers: o.providers, groups: [], settings: {} };
      } else if (p === "/api/hub/updates/dry-run") data = { ok: true, plan: "npm install -g " + body.id + "@2.0.0" };
      else if (p === "/api/hub/updates/apply") {
        const f = state.updates.find((x) => x.provider === body.provider && x.id === body.id);
        if (f) f.apply = { state: "queued", message: "" };
        data = { ok: true, results: [{ status: "queued", provider: body.provider, id: body.id }] };
      } else if (p === "/api/hub/updates/dismiss") state.updates = state.updates.filter((x) => !(x.provider === body.provider && x.id === body.id));
      else if (/^\/api\/hub\/list/.test(p)) data = { ok: true, items: [] };
      return { ok: true, status: 200, json: async () => data };
    };
    vm.runInContext(panelSource, vm.createContext({ window: win, console }), { filename: "updates-panel.js" });
    globalThis.window = win;
    globalThis.fetch = win.fetch;
    let closer = null;
    const host = {
      root: dom.root,
      runTool: async (name) => (name === "dl_list" ? { ok: true, items: o.downloads, summary: null } : { ok: true, items: [], item: null }),
      notify: (...a) => { notes.push(a); },
      storage: {
        get: async (k) => (Object.prototype.hasOwnProperty.call(o.store, k) ? o.store[k] : null),
        set: async (k, v) => { o.store[k] = v; sets.push([k, v]); },
      },
      onClose: (cb) => { closer = cb; },
    };
    await new AsyncFunction("host", source)(host);
    await tick(30);
    const ctx = { dom, root: dom.root, log, confirms, toasts, notes, sets, state, store: o.store, host,
      close: () => { if (closer) closer(); },
      gets: () => log.filter((x) => x.method === "GET" && x.path === "/api/hub/updates").length,
      posts: () => log.filter((x) => x.method === "POST").map((x) => x.path),
      text: (id) => dom.root.querySelector(id).textContent };
    return ctx;
  }

  // -- default: on, one read when the panel opens, sections collapsed with their counts
  const h = await harness();
  check("on by default: the panel reads the updates once when it opens, and the Show updates switch says so", h.gets() === 1
    && h.root.querySelector("#showUpdates").getAttribute("aria-pressed") === "true");
  check("no scan, no write at all just from opening the panel", h.posts().length === 0 && !h.log.some((x) => /updates\/check/.test(x.path)));
  const secs0 = updSecsIn(h.root);
  check("one section per update group, in order, each showing its count while collapsed",
    eq(secs0.map((s) => s.textContent.replace(/^[\u25BE\u25B8]/, "")), ["Updates: Mods \u00B7 1 update", "Updates: Games \u00B7 1 update", "Updates: Packages \u00B7 4 updates", "Updates: Other \u00B7 1 update"]));
  check("the first time every section is collapsed: no update row is drawn, and the toggles say so",
    updRowsIn(h.root).length === 0 && secs0.every((s) => find(s, (c) => c.tag === "button")[0].getAttribute("aria-expanded") === "false"));
  check("the real downloads are drawn as before, ahead of the update sections", find(h.root.querySelector("#list"), (c) => hasClass(c, "row") && !hasClass(c, "row--update")).length === 3
    && h.root.querySelector("#list").children.indexOf(secs0[0]) > h.root.querySelector("#list").children.findIndex((c) => hasClass(c, "row")));

  // -- the updates change nothing a download owns
  const snap = (c) => ({ chips: c.text("#viewChips"), count: c.text("#count"), sub: c.text("#subline"), pause: c.root.querySelector("#pauseAll").disabled,
    resume: c.root.querySelector("#resumeAll").disabled, clear: c.text("#clearFinished"), clearOff: c.root.querySelector("#clearFinished").disabled });
  const withUpdates = snap(h);
  const notesBefore = h.notes.length;
  await h.root.querySelector("#showUpdates").click();
  await tick();
  check("switching off removes every update from the list at once", updSecsIn(h.root).length === 0 && updRowsIn(h.root).length === 0
    && h.root.querySelector("#showUpdates").getAttribute("aria-pressed") === "false");
  const withoutUpdates = snap(h);
  check("the status chip counts, the list count, the running line, Pause all / Resume all and Clear finished are identical with updates on and off", eq(withUpdates, withoutUpdates));
  check("the new switch is remembered under its own key (0 when off)", eq(h.sets.filter((s) => s[0] === "show_updates"), [["show_updates", "0"]]));
  const getsOff = h.gets();
  await h.root.querySelector("#showUpdates").click();
  await tick();
  check("switching on reads once and brings the sections back (collapsed); the key is written 1", h.gets() === getsOff + 1 && updSecsIn(h.root).length === 4
    && h.sets.filter((s) => s[0] === "show_updates").pop()[1] === "1");
  check("switching on and off made no notification", h.notes.length === notesBefore);

  // -- open a section: the rows
  const packages = updSecsIn(h.root)[2];
  await find(packages, (c) => c.tag === "button")[0].click();
  await tick();
  check("opening a section draws its rows, and remembers it under its own key", updRowsIn(h.root).length === 4
    && eq(h.sets.filter((s) => s[0] === "updates_open").pop(), ["updates_open", JSON.stringify(["packages"])]));
  const rowA = rowOf(h.root, "left-pad");
  check("a row is clearly an update: an Update tag and 'installed \u2192 available'", !!rowA && byText(rowA, "span", "Update").length === 1 && byText(rowA, "span", "1.0.0 \u2192 2.0.0").length === 1
    && hasClass(rowA, "row") && hasClass(rowA, "row--update") && rowA.getAttribute("data-kind") === "update");
  check("an update row has no progress bar and no download buttons (no Pause, Resume, Cancel, Retry)", find(rowA, (c) => hasClass(c, "bar-track")).length === 0
    && find(rowA, (c) => c.tag === "button").map((b) => b.textContent).join("|") === "Update|Hide");
  const hostile = rowOf(h.root, "<img src=x onerror=alert(1)>");      // the bidi character in the fixture is stripped
  check("a hostile name is drawn as plain text: the literal text is in the title, and no element was ever built from it",
    !!hostile && !h.dom.created.some((t) => t === "img" || t === "script" || t === "iframe") && hostile.textContent.indexOf("\u202e") === -1);
  const failedRow = rowOf(h.root, "broken-pkg");
  check("a failed apply shows apply.message as text, and offers Try again", !!failedRow && failedRow.textContent.indexOf("Failed disk full <b>x</b>") !== -1 && byText(failedRow, "button", "Try again").length === 1
    && find(failedRow, (c) => c.tag === "b").length === 1);
  await find(updSecsIn(h.root)[1], (c) => c.tag === "button")[0].click();
  await tick();
  const portal = rowOf(h.root, "Portal 2");
  check("a finding with can_apply false gets no Update button and says why; it can still be hidden", !!portal && byText(portal, "button", "Update").length === 0 && byText(portal, "button", "Hide").length === 1
    && portal.textContent.indexOf(pure.UPD_NO_UPDATER) !== -1);

  // -- Update: the Updates page's flow exactly
  h.state.yes = false;
  h.log.length = 0; h.confirms.length = 0;
  await byText(rowOf(h.root, "left-pad"), "button", "Update")[0].click();
  await tick();
  check("Update: a dry run first, ONE dialog, and a no sends no apply", eq(h.posts(), ["/api/hub/updates/dry-run"]) && h.confirms.length === 1);
  check("the dialog is the Updates page's own: it names the item, shows the versions and the exact plan line",
    h.confirms[0].title === "Update left-pad?" && /1\.0\.0 \u2192 2\.0\.0/.test(h.confirms[0].body) && h.confirms[0].pre === "npm install -g a@2.0.0" && h.confirms[0].focusCancel === true);
  h.state.yes = true;
  h.log.length = 0; h.confirms.length = 0;
  await byText(rowOf(h.root, "left-pad"), "button", "Update")[0].click();
  await tick(40);
  check("Update, a yes: dry run, then apply, in that order, with the provider and id", eq(h.posts(), ["/api/hub/updates/dry-run", "/api/hub/updates/apply"]) && h.confirms.length === 1
    && eq(h.log.filter((x) => x.path === "/api/hub/updates/apply")[0].body, { provider: "npm", id: "a" }));
  check("no check was asked for, ever", !h.log.some((x) => /updates\/check/.test(x.path)));
  check("after an apply the list is read again at once, and the row shows its state with its Update button off",
    h.gets() >= 1 && (function () { const r = rowOf(h.root, "left-pad"); return !!r && r.textContent.indexOf("Waiting its turn") !== -1 && byText(r, "button", "Update")[0].disabled === true; })());
  check("an update never shows up as a Jarvis notification (the answers use the page's own toast)", h.notes.length === notesBefore && h.toasts.length >= 1);

  // -- Hide
  h.state.yes = false;
  h.log.length = 0; h.confirms.length = 0;
  await byText(rowOf(h.root, "lodash"), "button", "Hide")[0].click();
  await tick();
  check("Hide asks once; a no writes nothing", h.confirms.length === 1 && h.confirms[0].title === "Hide this update?" && h.posts().length === 0 && !!rowOf(h.root, "lodash"));
  h.state.yes = true;
  h.log.length = 0; h.confirms.length = 0;
  await byText(rowOf(h.root, "lodash"), "button", "Hide")[0].click();
  await tick(40);
  check("Hide, a yes: one dismiss call with the provider and id, and the row goes", eq(h.posts(), ["/api/hub/updates/dismiss"]) && eq(h.log.filter((x) => x.method === "POST")[0].body, { provider: "npm", id: "b" })
    && !rowOf(h.root, "lodash") && updRowsIn(h.root).length === 4);
  h.close();

  // -- a second panel: remembered state, and Off means nothing is fetched
  const off = await harness({ store: { show_updates: "0", updates_open: JSON.stringify(["mods", "bogus"]) } });
  check("Show updates remembered as off: nothing is shown and nothing is fetched", off.gets() === 0 && off.log.filter((x) => /updates/.test(x.path)).length === 0 && updSecsIn(off.root).length === 0
    && off.root.querySelector("#showUpdates").getAttribute("aria-pressed") === "false");
  await off.root.querySelector("#showUpdates").click();
  await tick();
  check("switching it on reads once; the section the owner left open is open, the rest collapsed", off.gets() === 1 && updSecsIn(off.root).length === 4
    && updRowsIn(off.root).length === 1 && !!rowOf(off.root, "Sodium"));
  off.close();

  // -- only updates, no downloads at all
  const only = await harness({ downloads: [] });
  const onlyOff = await harness({ downloads: [], store: { show_updates: "0" } });
  check("with no downloads the update sections still show, under a plain 'No downloads yet.'; every download control stays as it was (off)",
    updSecsIn(only.root).length === 4 && only.text("#list").indexOf("No downloads yet.") !== -1
    && snap(only).pause === true && snap(only).resume === true && snap(only).clearOff === true && snap(only).clear === "Clear finished" && snap(only).count === "0"
    && eq(snap(only), snap(onlyOff)));
  only.close(); onlyOff.close();

  // -- a failing read keeps what was shown and says so in one line (not a notification)
  const bad = await harness({ readFails: true });
  check("a failed read lists nothing, says so in one line, and makes no notification", updSecsIn(bad.root).length === 0 && bad.text("#list").indexOf("Couldn't read the updates: hub is down") !== -1 && bad.notes.length === 0);
  bad.close();

  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("FAILED  the test itself threw: " + (e && e.stack || e)); process.exit(1); });
