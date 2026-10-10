/* Settings (L.43): the section rail, the search box, every pane except Skin, and the
 * Advanced tab. Skin is still app.js's (its open/seed/revert/save are unchanged); this
 * file only decides which pane is showing.
 *
 * All reads and writes go through /api/settings/*, which shells out to
 * `jarvis settings-admin` -- the rules (secret masking, type and structure checks,
 * stale-write detection, backups, guarded files) live in jarvis/settings_admin.py and
 * are NOT repeated here. This file only asks, shows what came back and relays a refusal
 * in the owner's words. Nothing from the model reaches this screen.
 *
 * Values from the server are put in the page with textContent / .value, never innerHTML.
 */
(function () {
  "use strict";

  const LAST_KEY = "jarvis.prefs.section";
  const $ = (sel, root) => (root || document).querySelector(sel);

  function h(tag, attrs, kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "value") el.value = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, String(v));
    }
    for (const c of [].concat(kids === null || kids === undefined ? [] : kids)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const host = () => window.JarvisHost || {};
  const toast = (msg, kind) => { if (host().toast) host().toast(msg, kind || "error"); };

  async function confirmDialog(opts) {
    if (window.JarvisUI && typeof window.JarvisUI.confirm === "function") {
      return window.JarvisUI.confirm(opts);
    }
    return window.confirm((opts.title || "") + "\n\n" + (opts.body || ""));
  }

  // ---- API ------------------------------------------------------------------------------
  // The custom header is what /api/settings/* requires (a page on another site cannot add
  // it without a CORS preflight, which this server never answers).
  async function api(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", "X-Jarvis-Settings": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok || (data && data.ok === false)) {
      const problems = data && Array.isArray(data.problems) ? data.problems.join(" ") : "";
      const err = new Error((data && (data.error || problems)) || ("Request failed (" + res.status + ")."));
      err.data = data || {};
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ---- state ----------------------------------------------------------------------------
  const S = {
    section: loadLast(),
    catalog: null,
    renderToken: 0,
    adv: { tab: "files", file: null, dirty: false, reveal: false, version: null, meta: null },
  };

  function loadLast() {
    try { return localStorage.getItem(LAST_KEY) || "skin"; } catch { return "skin"; }
  }
  function saveLast(id) {
    try { localStorage.setItem(LAST_KEY, id); } catch { /* private mode: not remembering is fine */ }
  }

  async function loadCatalog() {
    S.catalog = await api("GET", "/api/settings/catalog");
    return S.catalog;
  }

  // ---- small helpers --------------------------------------------------------------------
  function getAt(doc, path) {
    let node = doc;
    for (const step of path) {
      if (node === null || typeof node !== "object" || !(step in node)) return undefined;
      node = node[step];
    }
    return node;
  }
  function setAt(doc, path, value) {
    let node = doc;
    for (let i = 0; i < path.length - 1; i++) {
      if (node[path[i]] === null || typeof node[path[i]] !== "object") node[path[i]] = {};
      node = node[path[i]];
    }
    node[path[path.length - 1]] = value;
  }

  function flash(el, text, kind) {
    el.textContent = text || "";
    el.className = "prefs-row__status" + (kind ? " is-" + kind : "");
  }

  function fileMeta(name) {
    return (S.catalog && S.catalog.files.find((f) => f.name === name)) || null;
  }
  function appliesFor(name) {
    const m = fileMeta(name);
    return m && m.applies ? m.applies.replace(/\.$/, "") : "";
  }

  // Docs read once per section render and shared by every row of that file, so a save from
  // one row hands its new version to the next (a stale version would be refused).
  async function readDoc(name) {
    const r = await api("POST", "/api/settings/file/read", { name, reveal: false });
    let doc = {};
    try { doc = JSON.parse(r.text); } catch { doc = {}; }
    return { doc, version: r.version, parseOk: r.parse_ok !== false, queue: Promise.resolve() };
  }

  // ---- controls -------------------------------------------------------------------------
  // makeControl(spec, value, commit) -> {el, set(v)}; `commit(value)` is awaited and may throw.
  function makeControl(spec, value, commit, status) {
    const ctl = { prev: value };
    const run = async (next, revert) => {
      try {
        await commit(next);
        ctl.prev = next;
      } catch (err) {
        revert();
        flash(status, err.message, "err");
      }
    };
    if (spec.type === "bool") {
      const box = h("input", { type: "checkbox" });
      box.checked = value === true;
      box.addEventListener("change", () => run(box.checked, () => { box.checked = ctl.prev === true; }));
      ctl.el = h("label", { class: "check" }, [box, h("span", { text: box.checked ? "On" : "Off" })]);
      box.addEventListener("change", () => { ctl.el.lastChild.textContent = box.checked ? "On" : "Off"; });
      ctl.set = (v) => { box.checked = v === true; ctl.el.lastChild.textContent = box.checked ? "On" : "Off"; };
      return ctl;
    }
    if (spec.type === "choice") {
      const sel = h("select", { class: "prefs-select" });
      if (value === undefined || value === null) sel.append(h("option", { value: "", text: "Default" }));
      for (const [v, label] of spec.choices) sel.append(h("option", { value: String(v), text: label }));
      sel.value = value === undefined || value === null ? "" : String(value);
      sel.addEventListener("change", () => {
        if (sel.value === "") return;
        const raw = spec.choices.find(([v]) => String(v) === sel.value)[0];
        run(raw, () => { sel.value = ctl.prev === undefined || ctl.prev === null ? "" : String(ctl.prev); });
      });
      ctl.el = sel;
      ctl.set = (v) => { sel.value = String(v); };
      return ctl;
    }
    if (spec.type === "int" || spec.type === "float") {
      const input = h("input", { type: "number", class: "prefs-input", step: spec.type === "int" ? "1" : "any" });
      if (spec.min !== undefined) input.min = String(spec.min);
      if (spec.max !== undefined) input.max = String(spec.max);
      input.value = value === undefined || value === null ? "" : String(value);
      input.addEventListener("change", () => {
        const n = Number(input.value);
        if (input.value.trim() === "" || !Number.isFinite(n) || (spec.type === "int" && !Number.isInteger(n))) {
          flash(status, spec.type === "int" ? "Enter a whole number." : "Enter a number.", "err");
          input.value = ctl.prev === undefined || ctl.prev === null ? "" : String(ctl.prev);
          return;
        }
        run(n, () => { input.value = ctl.prev === undefined || ctl.prev === null ? "" : String(ctl.prev); });
      });
      ctl.el = h("span", { class: "prefs-inline" }, [input, spec.unit ? h("span", { class: "prefs-unit", text: spec.unit }) : null]);
      ctl.set = (v) => { input.value = v === undefined || v === null ? "" : String(v); };
      return ctl;
    }
    const input = h("input", { type: "text", class: "prefs-input prefs-input--wide", spellcheck: "false" });
    input.value = value === undefined || value === null ? "" : String(value);
    input.addEventListener("change", () => run(input.value, () => { input.value = ctl.prev === undefined || ctl.prev === null ? "" : String(ctl.prev); }));
    ctl.el = input;
    ctl.set = (v) => { input.value = v === undefined || v === null ? "" : String(v); };
    return ctl;
  }

  function rowShell(label, help, ctlEl, statusEl, meta) {
    return h("div", { class: "prefs-row", "data-search": (label + " " + (help || "")).toLowerCase() }, [
      h("div", { class: "prefs-row__text" }, [
        h("div", { class: "prefs-row__label", text: label }),
        help ? h("div", { class: "prefs-row__help", text: help }) : null,
        meta ? h("div", { class: "prefs-row__meta" }, meta) : null,
      ]),
      h("div", { class: "prefs-row__ctl" }, [ctlEl, statusEl]),
    ]);
  }

  // One value in a settings file. Saved with `settings-admin set`, so it passes the same
  // validation, backup and stale-write checks as the Advanced editor.
  function fileRow(ctx, spec) {
    const st = ctx.docs[spec.file];
    const current = getAt(st.doc, spec.path);
    const shown = current === undefined ? spec.fallback : current;
    const status = h("span", { class: "prefs-row__status" });
    const ctl = makeControl(spec, shown, (value) => {
      // One save at a time per file, each carrying the version the previous one produced.
      const job = async () => {
        try {
          const r = await api("POST", "/api/settings/file/set", {
            name: spec.file, path: spec.path, value, base_version: st.version,
          });
          st.version = r.version;
          setAt(st.doc, spec.path, value);
          flash(status, r.changed === false ? "No change." : "Saved. Applies: " + (appliesFor(spec.file) || "when it next runs") + ".", "ok");
        } catch (err) {
          if (err.data && err.data.code === "stale") {
            const fresh = await readDoc(spec.file);
            st.doc = fresh.doc;
            st.version = fresh.version;
            err.message = "That file changed on disk, so it was reloaded. Try again.";
          }
          throw err;
        }
      };
      // Chain behind the previous save, but never let one failure poison the chain.
      const run = st.queue.then(job);
      st.queue = run.catch(() => {});
      return run;
    }, status);
    const meta = current === undefined && spec.fallback !== undefined ? "Not set in the file; Jarvis uses its built-in default." : null;
    return rowShell(spec.label, spec.help, ctl.el, status, meta);
  }

  function tunableRow(ctx, t) {
    const status = h("span", { class: "prefs-row__status" });
    const holder = h("div", { class: "prefs-row" });
    const render = () => {
      const bits = [];
      bits.push(h("span", { class: "prefs-tag" + (t.source === "override" ? " is-override" : t.source === "env" ? " is-env" : ""),
                           text: t.source === "override" ? "your override" : t.source === "env" ? "environment variable" : "default" }));
      bits.push(" " + t.applies_label + ".");
      if (t.env_wins) bits.push(" An environment variable is set on the server and wins over this value.");
      const ctl = makeControl(t, t.value, async (value) => {
        const r = await api("POST", "/api/settings/tunable", { name: t.name, value });
        await refreshTunable(t);
        flash(status, "Saved. Applies: " + t.applies_label.toLowerCase() + ".", "ok");
        return r;
      }, status);
      const reset = t.has_override
        ? h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Reset to default", onclick: async () => {
            try {
              await api("POST", "/api/settings/tunable/reset", { name: t.name });
              await refreshTunable(t);
              flash(status, "Back to the default.", "ok");
            } catch (err) { flash(status, err.message, "err"); }
          } })
        : null;
      const fresh = rowShell(t.label, t.help + " (Default " + String(t.default) + (t.unit ? " " + t.unit : "") + ".)",
                             h("span", { class: "prefs-inline" }, [ctl.el, reset]), status, bits);
      holder.replaceChildren(...Array.from(fresh.childNodes));
      holder.dataset.search = fresh.dataset.search;
    };
    const refreshTunable = async (entry) => {
      const cat = await loadCatalog();
      const next = cat.tunables.find((x) => x.name === entry.name);
      if (next) Object.assign(entry, next);
      render();
    };
    render();
    return holder;
  }

  function linkRow(spec) {
    return rowShell(spec.label, spec.help, h("button", { class: "btn btn--ghost btn--sm", type: "button", text: spec.button || "Open", onclick: spec.run }), h("span", { class: "prefs-row__status" }));
  }

  function group(title, rows) {
    return h("section", { class: "prefs-group" }, [h("h3", { text: title }), ...rows]);
  }

  function openPanel(id) {
    if (host().closePrefs) host().closePrefs();
    if (host().openPanel) host().openPanel(id);
  }
  function openAdvancedFile(name) {
    S.adv.tab = "files";
    S.adv.file = name;
    select("advanced");
  }

  // ---- sections -------------------------------------------------------------------------
  const LEVELS = [[1, "1 · Silent"], [2, "2 · Standard"], [3, "3 · Persistent"], [4, "4 · Broadcast"], [5, "5 · Confirm"]];

  function tunableRows(ctx, key) {
    return ctx.catalog.tunables.filter((t) => t.section === key).map((t) => tunableRow(ctx, t));
  }

  const SECTIONS = [
    { id: "skin", label: "Skin", static: true,
      terms: "persona name how jarvis addresses you attitude theme accent colour color interface saturation motion scanlines appearance" },
    { id: "ai", label: "AI", title: "AI",
      lede: "How Jarvis talks to the model. Provider order, keys and the persona text are in Advanced (keys stay hidden until you reveal them).",
      files: ["ai_config.json"],
      terms: "model provider max tokens timeout tools prompt cache budget compact scheduled key priority",
      build: async (ctx) => {
        const rows = [
          { file: "ai_config.json", path: ["defaults", "max_tokens"], type: "int", min: 1, fallback: 700, label: "Longest reply", unit: "tokens", help: "Upper limit on one reply. Higher costs more and can run into a provider's own cap." },
          { file: "ai_config.json", path: ["defaults", "timeout"], type: "float", min: 1, fallback: 30, label: "Wait for a provider", unit: "seconds", help: "How long to wait for one provider before trying the next." },
          { file: "ai_config.json", path: ["defaults", "tools_enabled"], type: "bool", fallback: true, label: "Let Jarvis use tools", help: "Off = answers from the model only; no tool is ever called." },
          { file: "ai_config.json", path: ["defaults", "compact_prompt"], type: "bool", fallback: true, label: "Compact system prompt", help: "A shorter prompt for providers with small token limits." },
          { file: "ai_config.json", path: ["defaults", "prompt_cache"], type: "bool", fallback: true, label: "Prompt caching", help: "Lets providers that support it reuse the unchanged start of the prompt." },
          { file: "ai_config.json", path: ["defaults", "scheduled_token_budget"], type: "int", min: 0, fallback: 30000, label: "Token limit for one scheduled run", unit: "tokens", help: "Stops a runaway scheduled job. 0 turns the limit off." },
        ].map((spec) => fileRow(ctx, spec));
        const out = [group("Answers", rows)];
        const providers = getAt(ctx.docs["ai_config.json"].doc, ["providers"]);
        if (Array.isArray(providers) && providers.length) {
          const prow = [];
          providers.forEach((p, i) => {
            const name = p && p.name ? p.name : "provider " + (i + 1);
            const keys = Array.isArray(p.api_keys) ? p.api_keys.filter((k) => k).length : 0;
            prow.push(fileRow(ctx, { file: "ai_config.json", path: ["providers", i, "enabled"], type: "bool", fallback: true,
              label: name + ": enabled", help: (p.model ? "Model " + p.model + ". " : "") + keys + " API key" + (keys === 1 ? "" : "s") + " set (hidden)." }));
            prow.push(fileRow(ctx, { file: "ai_config.json", path: ["providers", i, "model"], type: "text", fallback: "",
              label: name + ": model", help: "The model name sent to this provider." }));
          });
          out.push(group("Providers", prow));
        }
        const t = tunableRows(ctx, "ai");
        if (t.length) out.push(group("Limits", t));
        out.push(group("Elsewhere", [linkRow({ label: "Name, how Jarvis addresses you, attitude", help: "The persona fields are in the Skin section.", button: "Go to Skin", run: () => select("skin") })]));
        return out;
      } },
    { id: "layout", label: "Layout", title: "Layout",
      lede: "How the page is arranged. Theme, accent and interface tuning are in Skin.",
      files: [],
      terms: "classic focus layout web stream streaming tick interval server",
      build: async (ctx) => {
        const status = h("span", { class: "prefs-row__status" });
        const current = host().getLayout ? host().getLayout() : "classic";
        const ctl = makeControl({ type: "choice", choices: [["classic", "Classic"], ["focus", "Focus"]] }, current, async (mode) => {
          if (host().setLayout) await host().setLayout(mode);
          flash(status, "Applied.", "ok");
        }, status);
        const out = [group("Page", [rowShell("Layout", "Classic shows every panel button; Focus hides them behind the Menu for a calmer page.", ctl.el, status)])];
        const t = tunableRows(ctx, "web");
        if (t.length) out.push(group("Web console", t));
        return out;
      } },
    { id: "notifications", label: "Notifications", title: "Notifications",
      lede: "How loudly each kind of notification is delivered. Inbox contents and per-item controls are in the Notifications panel.",
      files: ["notify_config.json"],
      terms: "notification level toast inbox silent standard persistent broadcast confirm reminder task clipboard ambient",
      build: async (ctx) => {
        const rows = [
          ["reminder", "Reminders", 2], ["notify", "Messages from Jarvis", 2], ["task", "Finished tasks", 2],
          ["clipboard_watch", "Clipboard watcher", 2], ["ambient", "Ambient suggestions", 1],
        ].map(([key, label, fb]) => fileRow(ctx, { file: "notify_config.json", path: ["levels", key], type: "choice", choices: LEVELS, fallback: fb, label,
          help: "Default importance when nothing sets one. Silent = inbox only; Confirm = stays until you acknowledge it." }));
        const out = [group("Default level by kind", rows)];
        const t = tunableRows(ctx, "notifications");
        if (t.length) out.push(group("Limits", t));
        out.push(group("Elsewhere", [linkRow({ label: "Notifications panel", help: "Read, acknowledge and filter the inbox.", button: "Open", run: () => openPanel("notifications") })]));
        return out;
      } },
    { id: "tools", label: "Tools & safety", title: "Tools & safety",
      lede: "Limits for tool-using features. What each tool may do, and which calls ask first, is managed in the Tool Manager; the safety files are in Advanced and ask you to confirm before they change.",
      files: [],
      terms: "tool safety confirm fetch timeout search agent continuation policy unattended permission",
      build: async (ctx) => {
        const out = [];
        const t = tunableRows(ctx, "tools");
        if (t.length) out.push(group("Limits", t));
        out.push(group("Elsewhere", [
          linkRow({ label: "Tool Manager", help: "Turn tools on or off and set how each one confirms.", button: "Open", run: () => openPanel("ctools") }),
          linkRow({ label: "Tool safety file", help: "Which tool calls ask before they run.", button: "Open in Advanced", run: () => openAdvancedFile("tool_safety.json") }),
          linkRow({ label: "Unattended-run policy", help: "What a scheduled or chat-triggered run may do.", button: "Open in Advanced", run: () => openAdvancedFile("policy.json") }),
        ]));
        return out;
      } },
    { id: "integrations", label: "Integrations", title: "Integrations",
      lede: "Connected services. Each opens its own panel or its settings file.",
      files: [],
      terms: "channels discord telegram mcp spotify playnite everything voice calendar daemons",
      build: async () => [group("Panels", [
        linkRow({ label: "Chat platforms", help: "Who may talk to Jarvis, and on which platform.", button: "Open", run: () => openPanel("channels") }),
        linkRow({ label: "MCP servers", help: "Add, remove and check Model Context Protocol servers.", button: "Open", run: () => openPanel("mcp") }),
        linkRow({ label: "Daemons", help: "Background watchers and their schedules.", button: "Open", run: () => openPanel("daemons") }),
      ]), group("Settings files", [
        linkRow({ label: "Spotify", help: "spotify.json", button: "Open in Advanced", run: () => openAdvancedFile("spotify.json") }),
        linkRow({ label: "Playnite", help: "playnite.json", button: "Open in Advanced", run: () => openAdvancedFile("playnite.json") }),
        linkRow({ label: "Everything search", help: "everything.json", button: "Open in Advanced", run: () => openAdvancedFile("everything.json") }),
        linkRow({ label: "Voice", help: "voice_config.json", button: "Open in Advanced", run: () => openAdvancedFile("voice_config.json") }),
        linkRow({ label: "Calendar", help: "calendar.json", button: "Open in Advanced", run: () => openAdvancedFile("calendar.json") }),
      ])] },
    { id: "memory", label: "Memory & storage", title: "Memory & storage",
      lede: "What Jarvis remembers and keeps on disk.",
      files: [],
      terms: "memory facts raw archive event log storage backups logs history",
      build: async (ctx) => {
        const out = [];
        const t = tunableRows(ctx, "memory");
        if (t.length) out.push(group("Limits", t));
        out.push(group("Elsewhere", [
          linkRow({ label: "Long-term memory file", help: "memory.json", button: "Open in Advanced", run: () => openAdvancedFile("memory.json") }),
          linkRow({ label: "Logs", help: "Search and read Jarvis's logs.", button: "Open", run: () => openPanel("logs") }),
        ]));
        out.push(h("div", { class: "prefs-note", text: "Every change made in Settings is backed up first, in " + ctx.catalog.jarvis_dir + "/settings_backups (the last " + ctx.catalog.backup_keep + " versions of each file)." }));
        return out;
      } },
    { id: "about", label: "About", title: "About",
      lede: "What is installed and where Jarvis keeps its files.",
      files: [],
      terms: "about version build path folder directory changes history",
      build: async (ctx) => {
        let status = null;
        try { status = await fetch("/api/status").then((r) => r.json()); } catch { status = null; }
        const kv = (k, v) => h("tr", {}, [h("td", { text: k }), h("td", { class: "mono", text: v || "unknown" })]);
        const rows = [kv("Version", ctx.catalog.version), kv("Settings folder", ctx.catalog.jarvis_dir), kv("Saved tunables", ctx.catalog.tunables_file)];
        if (status && status.invocation) rows.push(kv("Command", Array.isArray(status.invocation) ? status.invocation.join(" ") : String(status.invocation)));
        let changes = [];
        try { changes = (await api("GET", "/api/settings/changes")).changes.slice(0, 10); } catch { changes = []; }
        return [
          group("This install", [h("table", { class: "adv-table" }, [h("tbody", {}, rows)])]),
          group("Last changes made in Settings", changes.length ? [changeTable(changes)] : [h("div", { class: "prefs-empty", text: "Nothing has been changed from Settings yet." })]),
        ];
      } },
    { id: "advanced", label: "Advanced", advanced: true,
      terms: "advanced json file edit tunables environment plumbing backup undo reveal secrets raw" },
  ];
  const BY_ID = Object.fromEntries(SECTIONS.map((s) => [s.id, s]));

  function changeTable(changes) {
    return h("table", { class: "adv-table" }, [
      h("thead", {}, h("tr", {}, [h("th", { text: "When" }), h("th", { text: "File" }), h("th", { text: "What" }), h("th", { text: "Where" })])),
      h("tbody", {}, changes.map((c) => h("tr", {}, [
        h("td", { text: (c.at || "").replace("T", " ").replace("+00:00", " UTC") }),
        h("td", { class: "mono", text: c.file || "" }),
        h("td", { text: c.action || "" }),
        h("td", { class: "mono", text: (c.paths || []).slice(0, 4).join(", ") + ((c.paths || []).length > 4 ? "…" : "") }),
      ]))),
    ]);
  }

  // ---- shell: rail, panes, search -------------------------------------------------------
  function paneFor(id) {
    if (id === "skin") return $("#prefs-pane-skin");
    let pane = $("#prefs-pane-" + id);
    if (!pane) {
      pane = h("div", { class: "prefs-pane", id: "prefs-pane-" + id, "data-section": id, hidden: true });
      $("#prefs-main").insertBefore(pane, $("#prefs-results"));
    }
    return pane;
  }

  function buildRail() {
    const rail = $("#prefs-rail");
    if (!rail || rail.childElementCount) return;
    SECTIONS.forEach((s) => {
      if (s.id === "advanced") rail.append(h("div", { class: "prefs-rail__sep" }));
      rail.append(h("button", { class: "prefs-rail__btn", type: "button", "data-section": s.id, text: s.label, onclick: () => { clearSearch(); select(s.id); } }));
    });
  }

  function select(id) {
    if (!BY_ID[id]) id = "skin";
    S.section = id;
    saveLast(id);
    buildRail();
    document.querySelectorAll("#prefs-rail .prefs-rail__btn").forEach((b) => {
      if (b.dataset.section === id) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    document.querySelectorAll("#prefs-main > .prefs-pane").forEach((p) => { p.hidden = true; });
    $("#prefs-results").hidden = true;
    const pane = paneFor(id);
    pane.hidden = false;
    if (id === "skin") return;
    if (BY_ID[id].advanced) { renderAdvanced(pane); return; }
    renderSection(id, pane);
  }

  async function renderSection(id, pane) {
    const token = ++S.renderToken;
    pane.replaceChildren(h("div", { class: "prefs-loading", text: "Loading…" }));
    try {
      const def = BY_ID[id];
      const catalog = await loadCatalog();
      const docs = {};
      for (const f of def.files || []) docs[f] = await readDoc(f);
      if (token !== S.renderToken) return;
      const ctx = { catalog, docs };
      const blocks = await def.build(ctx);
      if (token !== S.renderToken) return;
      pane.replaceChildren(
        h("h3", { class: "prefs-title", text: def.title }),
        def.lede ? h("p", { class: "prefs-lede", text: def.lede }) : null,
        h("div", { class: "modal__body prefs-body" }, blocks),
      );
    } catch (err) {
      if (token === S.renderToken) pane.replaceChildren(h("div", { class: "prefs-error", text: "Couldn't load this section: " + err.message }));
    }
  }

  // ---- search ---------------------------------------------------------------------------
  function clearSearch() {
    const box = $("#prefs-search");
    if (box && box.value) { box.value = ""; }
    const res = $("#prefs-results");
    if (res) res.hidden = true;
  }

  function searchIndex() {
    const out = [];
    SECTIONS.forEach((s) => out.push({ section: s.id, label: s.label, help: "Section", hay: (s.label + " " + (s.terms || "")).toLowerCase() }));
    if (S.catalog) {
      S.catalog.tunables.forEach((t) => out.push({ section: t.section === "web" ? "layout" : t.section, label: t.label, help: "Tunable · " + t.name, hay: (t.label + " " + t.name + " " + t.help).toLowerCase() }));
      S.catalog.files.forEach((f) => out.push({ section: "advanced", file: f.name, label: f.name, help: "File · " + (f.title || f.group_label), hay: (f.name + " " + f.title + " " + f.note).toLowerCase() }));
    }
    return out;
  }

  function runSearch(q) {
    const res = $("#prefs-results");
    const needle = q.trim().toLowerCase();
    if (!needle) { res.hidden = true; select(S.section); return; }
    const words = needle.split(/\s+/);
    const hits = searchIndex().filter((e) => words.every((w) => e.hay.includes(w))).slice(0, 40);
    document.querySelectorAll("#prefs-main > .prefs-pane").forEach((p) => { p.hidden = true; });
    res.hidden = false;
    res.replaceChildren(...(hits.length ? hits.map((e) => h("button", { class: "prefs-hit", type: "button", onclick: () => {
      clearSearch();
      if (e.file) openAdvancedFile(e.file); else select(e.section);
    } }, [e.label, h("small", { text: (BY_ID[e.section] ? BY_ID[e.section].label + " · " : "") + e.help })])) : [h("div", { class: "prefs-empty", text: "Nothing matches “" + q.trim() + "”." })]));
  }

  // ---- Advanced -------------------------------------------------------------------------
  function renderAdvanced(pane) {
    const A = S.adv;
    pane.replaceChildren();
    const tabs = [["files", "Files"], ["tunables", "Tunables"], ["plumbing", "Plumbing"], ["history", "History"]];
    const bar = h("div", { class: "adv-tabs", role: "tablist" }, tabs.map(([id, label]) =>
      h("button", { class: "adv-tab", type: "button", role: "tab", "aria-selected": A.tab === id ? "true" : "false", text: label,
        onclick: async () => {
          if (A.tab === "files" && A.dirty && id !== "files" && !(await confirmDialog({ title: "Discard unsaved edits?", body: "You have edits to " + A.file + " that are not saved.", confirmLabel: "Discard", level: "danger", focusCancel: true }))) return;
          A.tab = id; A.dirty = false; renderAdvanced(pane);
        } })));
    const content = h("div", { class: "prefs-pane", style: "flex:1 1 auto;min-height:0" });
    pane.append(bar, content);
    const token = ++S.renderToken;
    loadCatalog().then((cat) => {
      if (token !== S.renderToken) return;
      if (A.tab === "files") filesTab(content, cat);
      else if (A.tab === "tunables") tunablesTab(content, cat);
      else if (A.tab === "plumbing") plumbingTab(content, cat);
      else historyTab(content);
    }).catch((err) => content.replaceChildren(h("div", { class: "prefs-error", text: "Couldn't load Advanced: " + err.message })));
  }

  function tunablesTab(content, cat) {
    const ctx = { catalog: cat, docs: {} };
    const bySection = {};
    cat.tunables.forEach((t) => { (bySection[t.section] = bySection[t.section] || []).push(t); });
    const titles = { ai: "AI", tools: "Tools", memory: "Memory & storage", notifications: "Notifications", web: "Web server" };
    content.replaceChildren(h("div", { class: "modal__body prefs-body" }, [
      h("div", { class: "prefs-note", text: "These are the numbers and switches that used to need a source edit. Each is range-checked before it is saved. Anything not listed here is deliberately fixed (for example, the cap on tool rounds per ask and the confirmation prompts)." }),
      ...Object.keys(bySection).map((k) => group(titles[k] || k, bySection[k].map((t) => tunableRow(ctx, t)))),
    ]));
  }

  function plumbingTab(content, cat) {
    content.replaceChildren(h("div", { class: "modal__body prefs-body" }, [
      h("div", { class: "prefs-note", text: "Jarvis sets these for itself while it runs one ask, one scheduled job or one chat message. They are shown so you can see what exists; they cannot be edited, because a saved value would land on the wrong process." }),
      h("table", { class: "adv-table" }, [h("thead", {}, h("tr", {}, [h("th", { text: "Variable" }), h("th", { text: "What it does" })])),
        h("tbody", {}, cat.plumbing.map((p) => h("tr", {}, [h("td", { class: "mono", text: p.name }), h("td", { text: p.help })])))]),
    ]));
  }

  async function historyTab(content) {
    content.replaceChildren(h("div", { class: "prefs-loading", text: "Loading…" }));
    try {
      const r = await api("GET", "/api/settings/changes");
      content.replaceChildren(h("div", { class: "modal__body prefs-body" }, [
        h("div", { class: "prefs-note", text: "Every save, restore and tunable change made from Settings. Only the file and the setting names are recorded, never the values." }),
        r.changes.length ? changeTable(r.changes) : h("div", { class: "prefs-empty", text: "Nothing has been changed from Settings yet." }),
      ]));
    } catch (err) {
      content.replaceChildren(h("div", { class: "prefs-error", text: err.message }));
    }
  }

  function filesTab(content, cat) {
    const A = S.adv;
    const list = h("div", { class: "adv-list__scroll" });
    const filter = h("input", { type: "search", class: "prefs-input adv-list__filter", placeholder: "Filter files…", "aria-label": "Filter files", spellcheck: "false" });
    const editor = h("div", { class: "adv-editor" });
    content.replaceChildren(h("div", { class: "adv-files" }, [h("div", { class: "adv-list" }, [filter, list]), editor]));

    const drawList = () => {
      const needle = filter.value.trim().toLowerCase();
      list.replaceChildren();
      let lastGroup = "";
      cat.files.filter((f) => !needle || (f.name + " " + f.title).toLowerCase().includes(needle)).forEach((f) => {
        if (f.group_label !== lastGroup) { lastGroup = f.group_label; list.append(h("div", { class: "adv-list__group", text: f.group_label })); }
        list.append(h("button", { class: "adv-file" + (f.exists ? "" : " is-missing"), type: "button", title: f.title || f.name, text: f.name,
          "aria-current": A.file === f.name ? "true" : null, onclick: () => openFile(f.name) }));
      });
    };
    filter.addEventListener("input", drawList);

    async function openFile(name, force) {
      if (!force && A.dirty && A.file !== name && !(await confirmDialog({ title: "Discard unsaved edits?", body: "You have edits to " + A.file + " that are not saved.", confirmLabel: "Discard", level: "danger", focusCancel: true }))) return;
      A.file = name; A.dirty = false; A.reveal = false;
      drawList();
      editor.replaceChildren(h("div", { class: "prefs-loading", text: "Loading…" }));
      try {
        const r = await api("POST", "/api/settings/file/read", { name, reveal: false });
        drawEditor(r);
      } catch (err) {
        editor.replaceChildren(h("div", { class: "prefs-error", text: err.message }));
      }
    }

    function drawEditor(r) {
      const meta = r.meta;
      A.meta = meta; A.version = r.version;
      const ta = h("textarea", { class: "adv-text", spellcheck: "false", "aria-label": "Contents of " + r.name, readonly: !meta.editable });
      ta.value = r.text;
      const hint = h("div", { class: "adv-hint" });
      const report = h("div", { class: "adv-report", hidden: true });
      const backups = h("div", { class: "adv-backups", hidden: true });
      const saveBtn = h("button", { class: "btn btn--primary btn--sm", type: "button", text: "Save", disabled: !meta.editable });
      const previewBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Preview changes", disabled: !meta.editable });
      const guardBox = h("input", { type: "checkbox" });
      const guard = meta.guarded ? h("label", { class: "check" }, [guardBox, h("span", { text: "I understand this changes what Jarvis is allowed to do." })]) : null;

      const localCheck = () => {
        try { JSON.parse(ta.value); hint.textContent = "Valid JSON."; hint.className = "adv-hint is-ok"; return true; }
        catch (e) { hint.textContent = "Not valid JSON yet: " + e.message; hint.className = "adv-hint is-err"; return false; }
      };
      ta.addEventListener("input", () => { A.dirty = true; report.hidden = true; if (!ta.readOnly) localCheck(); });

      const showProblems = (problems) => {
        report.replaceChildren(h("ul", {}, problems.map((p) => h("li", { text: p }))));
        report.hidden = false;
      };
      const showChanges = (changes) => {
        if (!changes.length) { report.replaceChildren(h("div", { class: "prefs-empty", text: "No changes." })); report.hidden = false; return; }
        report.replaceChildren(h("table", { class: "adv-diff" }, [h("tbody", {}, changes.map((c) => h("tr", {}, [
          h("td", { class: "k-" + c.kind, text: c.kind }), h("td", { text: c.path }),
          h("td", { text: c.kind === "added" ? "" : c.before }), h("td", { text: c.kind === "removed" ? "" : "→ " + c.after }),
        ])))]));
        report.hidden = false;
      };

      previewBtn.addEventListener("click", async () => {
        try {
          const p = await api("POST", "/api/settings/file/preview", { name: r.name, text: ta.value, base_version: A.version });
          if (!p.valid) { showProblems(p.problems); hint.textContent = "Not ready to save."; hint.className = "adv-hint is-err"; }
          else { showChanges(p.changes); hint.textContent = p.changes.length + " change" + (p.changes.length === 1 ? "" : "s") + " would be saved."; hint.className = "adv-hint is-ok"; }
        } catch (err) { showProblems([err.message]); }
      });

      saveBtn.addEventListener("click", async () => {
        if (meta.guarded && !guardBox.checked) { hint.textContent = "Tick the confirmation box first."; hint.className = "adv-hint is-err"; return; }
        if (A.reveal && !(await confirmDialog({ title: "Save with secrets visible?", body: "Values you can see now will be written as shown.", confirmLabel: "Save" }))) return;
        try {
          const res = await api("PUT", "/api/settings/file", { name: r.name, text: ta.value, base_version: A.version, confirm_guarded: meta.guarded && guardBox.checked });
          toast(res.changed === false ? "No changes to save." : "Saved " + r.name + ". Applies: " + (meta.applies || "when it next runs").replace(/\.$/, "") + ".", "info");
          A.dirty = false;
          await openFile(r.name, true);
        } catch (err) {
          const problems = err.data && err.data.problems;
          if (problems && problems.length) showProblems(problems); else showProblems([err.message]);
          hint.textContent = err.data && err.data.code === "stale" ? "Reload the file, then make your edit again." : "Not saved.";
          hint.className = "adv-hint is-err";
        }
      });

      const reloadBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Reload", onclick: async () => {
        if (A.dirty && !(await confirmDialog({ title: "Discard unsaved edits?", body: "Reloading drops the edits you have not saved.", confirmLabel: "Discard", level: "danger", focusCancel: true }))) return;
        openFile(r.name, true);
      } });

      const revealBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: A.reveal ? "Hide secrets" : "Reveal secrets", onclick: async () => {
        if (!A.reveal) {
          if (A.dirty && !(await confirmDialog({ title: "Discard unsaved edits?", body: "Revealing reloads the file.", confirmLabel: "Discard", level: "danger", focusCancel: true }))) return;
          if (!(await confirmDialog({ title: "Reveal secrets?", body: "API keys, tokens and passwords in " + r.name + " will be shown on screen.", confirmLabel: "Reveal", level: "warn", focusCancel: true }))) return;
        }
        A.reveal = !A.reveal; A.dirty = false;
        try {
          const r2 = await api("POST", "/api/settings/file/read", { name: r.name, reveal: A.reveal });
          drawEditor(r2);
        } catch (err) { toast(err.message); }
      } });

      const restore = async (id) => {
        if (meta.guarded && !guardBox.checked) { hint.textContent = "Tick the confirmation box first."; hint.className = "adv-hint is-err"; return; }
        if (!(await confirmDialog({ title: id ? "Restore this version?" : "Undo the last change?", body: "The file as it is now is backed up first, so this can itself be undone.", confirmLabel: id ? "Restore" : "Undo" }))) return;
        try {
          const res = await api("POST", "/api/settings/file/restore", { name: r.name, backup_id: id || null, base_version: A.version, confirm_guarded: meta.guarded && guardBox.checked });
          toast(res.changed === false ? "Already the same." : "Restored " + r.name + ".", "info");
          A.dirty = false;
          await openFile(r.name, true);
        } catch (err) { toast(err.message); }
      };
      const undoBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Undo last change", disabled: !meta.editable, onclick: () => restore(null) });
      const backupsBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Backups", onclick: async () => {
        if (!backups.hidden) { backups.hidden = true; return; }
        try {
          const b = await api("POST", "/api/settings/file/backups", { name: r.name });
          backups.replaceChildren(...(b.backups.length ? b.backups.map((x) => h("div", { class: "adv-backup" }, [
            h("span", { text: x.at.replace("T", " ").replace("+00:00", " UTC") + " · " + x.size + " bytes" }),
            meta.editable ? h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Restore", onclick: () => restore(x.id) }) : null,
          ])) : [h("div", { class: "prefs-empty", text: "No backups yet. One is made before each save." })]));
          backups.hidden = false;
        } catch (err) { toast(err.message); }
      } });
      const formatBtn = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Format", disabled: !meta.editable, onclick: () => {
        try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2) + "\n"; A.dirty = true; localCheck(); } catch { localCheck(); }
      } });

      const head = h("div", { class: "adv-editor__head" }, [
        h("span", { class: "adv-editor__name", text: r.name }),
        meta.title ? h("span", { class: "prefs-unit", text: meta.title }) : null,
        meta.guarded ? h("span", { class: "prefs-tag is-guard", text: "safety file" }) : null,
        !meta.editable ? h("span", { class: "prefs-tag", text: "read-only here" }) : null,
        r.exists ? null : h("span", { class: "prefs-tag", text: "not created yet" }),
      ]);
      const notes = [];
      if (meta.note) notes.push(h("div", { class: "prefs-note", text: meta.note }));
      notes.push(h("div", { class: "prefs-row__meta", text: "Applies: " + (meta.applies || "not recorded") }));
      if (r.masked) notes.push(h("div", { class: "prefs-note", text: r.masked + " hidden value" + (r.masked === 1 ? "" : "s") + " shown as ••••••••. Leave them as they are to keep them; use Reveal secrets to change one." }));
      if (A.reveal) notes.push(h("div", { class: "prefs-note prefs-note--warn", text: "Secrets are visible. They are hidden again when you switch file or close Settings." }));
      if (r.parse_ok === false) notes.push(h("div", { class: "prefs-note prefs-note--warn", text: "This file is not valid JSON right now (" + r.parse_error + "). Fix it here and save." }));

      const bar = h("div", { class: "adv-editor__bar" }, [
        previewBtn, saveBtn, formatBtn, undoBtn, backupsBtn, reloadBtn, r.masked || A.reveal ? revealBtn : (hasSecretsLikely(r.name) ? revealBtn : null), guard,
      ]);
      editor.replaceChildren(head, ...notes, bar, ta, hint, report, backups);
      if (meta.editable) localCheck(); else hint.textContent = "";
    }

    drawList();
    if (A.file && cat.files.some((f) => f.name === A.file)) openFile(A.file, true);
    else editor.replaceChildren(h("div", { class: "prefs-note", text: "Pick a file on the left. Every save is checked, backed up first and can be undone. Files another panel manages, and state Jarvis rewrites on its own, are shown read-only." }));
  }

  // The Reveal button also appears on files that are known to hold keys even when nothing is
  // masked right now (an empty key list), so a first key can be typed in.
  function hasSecretsLikely(name) {
    return ["ai_config.json", "mcp_config.json", "spotify.json", "channels.json"].includes(name);
  }

  // ---- public ---------------------------------------------------------------------------
  function show(section) {
    const backdrop = $("#prefs-backdrop");
    if (!backdrop) return;
    S.adv.reveal = false;
    S.adv.dirty = false;
    backdrop.hidden = false;
    buildRail();
    clearSearch();
    select(section || S.section);
    loadCatalog().catch(() => { /* search just has fewer entries until a section loads it */ });
  }

  async function confirmDiscardAdvanced() {
    return confirmDialog({ title: "Discard unsaved edits?", body: "You have edits to " + S.adv.file + " that are not saved.", confirmLabel: "Discard", level: "danger", focusCancel: true });
  }
  function closeNow() {
    S.adv.dirty = false;
    if (host().closePrefs) host().closePrefs();
  }

  function init() {
    buildRail();
    const box = $("#prefs-search");
    if (box) box.addEventListener("input", () => runSearch(box.value));
    const backdrop = $("#prefs-backdrop");
    if (!backdrop) return;
    // Closing with the X or a click outside would drop unsaved Advanced edits silently.
    // Capture phase so this runs before app.js's own close handlers and can stop them.
    backdrop.addEventListener("click", async (e) => {
      const isClose = e.target === backdrop || (e.target.closest && e.target.closest("#skin-close"));
      if (!isClose || !(S.adv.dirty && S.section === "advanced")) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      if (await confirmDiscardAdvanced()) closeNow();
    }, true);
    // Secrets that were revealed must not linger in a hidden page: when Settings closes,
    // forget the revealed text.
    new MutationObserver(() => {
      if (backdrop.hidden && S.adv.reveal) {
        S.adv.reveal = false;
        const pane = $("#prefs-pane-advanced");
        if (pane) pane.replaceChildren();
      }
    }).observe(backdrop, { attributes: true, attributeFilter: ["hidden"] });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  // Escape closes Settings, except while a dialog from ui-kit is open on top of it (that
  // dialog's own Escape handler owns the key), and the first Escape in a filled search box
  // only clears the box.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    const backdrop = $("#prefs-backdrop");
    if (!backdrop || backdrop.hidden) return;
    if (document.querySelector(".jui-modal")) return;
    const box = $("#prefs-search");
    if (box && box.value && document.activeElement === box) { clearSearch(); select(S.section); return; }
    if (S.adv.dirty && S.section === "advanced") {
      confirmDiscardAdvanced().then((ok) => { if (ok) closeNow(); });
      return;
    }
    if (host().closePrefs) host().closePrefs();
  });

  window.JarvisSettings = { show, lastSection: () => S.section };
})();
