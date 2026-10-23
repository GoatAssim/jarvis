/* Settings > Downloads (Download Hub phase 8, item 17.3-3).  The ONE place the hub's settings are changed.
 *
 * Registered with settings.js through window.JarvisSettingsSections (settings.js adds it to the rail and
 * hands build() the same row helpers its other sections use as ctx.ui).  Nothing here owns a rule:
 *   - what exists and what may be written comes from `jarvis hub-admin settings` (GET /api/hub/settings),
 *     which reads hub/settings.py's own WRITABLE / PREFS_WRITABLE lists.  A setting the hub doesn't accept
 *     yet (retention today) simply has no row; a key another phase adds shows up as a generic row.
 *   - every change is written through the dl_prefs / dl_settings tools, with the confirmation dl_settings
 *     asks for (library folder, private hosts) and the hub's own validator.  An error the hub returns is
 *     shown as it is; no limit or range is checked twice here.
 * Each row saves as soon as it is changed, like the other sections; the speed windows and the per-extension
 * folders are lists, so those two have their own Save button.
 *
 * The Stremio card (brief 19, N9 step 3) is the one block that talks to hub routes of its own: GET /api/hub/stremio (what is
 * installed and running), POST /api/hub/stremio/paths (Locate...), POST /api/hub/stremio/start (Start).  Like every hub write
 * they carry the X-Jarvis-Hub header; the hub decides what is allowed and this page shows its sentences as they are.
 */
(function (global) {
  "use strict";

  const PREFS_TOOL = "dl_prefs";
  const SETTINGS_TOOL = "dl_settings";
  const PROVIDER_TOOL = "dl_provider_prefs";
  const RUNNABLE = { dl_prefs: 1, dl_settings: 1, dl_provider_prefs: 1 };
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

  // ---- pure helpers (no DOM; exported as _pure for tests/verify_hub_settings.js) ---------------------

  // label / help for the rows this page knows by name; `group` decides the heading.  A writable key that is
  // not listed here is shown anyway (see extraRows) so a new phase's setting never needs this file first.
  const FIELDS = [
    { path: "library_root", group: "Where files go", label: "Download folder", type: "text",
      help: "Where finished downloads are kept. Empty = the default Jarvis downloads folder. Changing it asks first." },
    { path: "folders.video", group: "Where files go", label: "Video folder", type: "text", help: "A sub-folder of the download folder." },
    { path: "folders.audio", group: "Where files go", label: "Audio folder", type: "text", help: "A sub-folder of the download folder." },
    { path: "folders.file", group: "Where files go", label: "Other files folder", type: "text", help: "A sub-folder of the download folder." },
    { path: "rate_limit_kbps", group: "Speed", label: "Speed limit", type: "int", min: 0, unit: "KB/s",
      help: "Shared by everything the hub downloads. 0 = unlimited." },
    { path: "concurrency.total", group: "Running at once", label: "Downloads at once (all)", type: "int", min: 1, max: 16 },
    { path: "concurrency.http", group: "Running at once", label: "Direct links at once", type: "int", min: 1, max: 16 },
    { path: "concurrency.ytdlp", group: "Running at once", label: "Video sites at once", type: "int", min: 1, max: 16 },
    { path: "concurrency.stremio", group: "Running at once", label: "Torrents at once", type: "int", min: 1, max: 16 },
    { path: "http.stall_seconds", group: "Connections", label: "Call a download stalled after", type: "int", min: 10, unit: "seconds",
      help: "No bytes for this long counts as stalled and is retried." },
    { path: "http.max_retries", group: "Connections", label: "Retries before it fails", type: "int", min: 0, max: 10 },
    { path: "after_download.open", group: "When a download finishes", label: "Do this when one finishes", type: "choice",
      help: "A single download can override this when you add it." },
    { path: "notify.enabled", group: "Notifications", label: "Tell me about downloads", type: "bool",
      help: "Delivery (inbox, toast, desktop, voice, phone) follows Settings > Notifications." },
    { path: "notify.min_seconds", group: "Notifications", label: "Skip ones that took less than", type: "int", min: 0, unit: "seconds" },
    { path: "notify.coalesce_s", group: "Notifications", label: "Fold finishes within", type: "int", min: 0, unit: "seconds",
      help: "Downloads that finish this close together become one message." },
    { path: "notify.providers", group: "Notifications", label: "Also tell me about other tools' downloads", type: "bool",
      help: "Game installs and other tools that report into the hub." },
    // brief 19: the hub's own range (1 to 720) is not repeated here; a value it refuses shows its refusal text.
    { path: "unknown_settle_hours", group: "Lost downloads", label: "Give up on a lost download after (hours)", type: "int",
      help: "When another tool (Steam, Playnite ...) stops listing a download that hadn't finished, its row waits as \"unknown\". After this long it becomes \"cancelled\" (never \"failed\"). A row that is only out of date because its program is closed is never given up on." },
    { path: "allow_private_hosts", group: "Safety", label: "Allowed private addresses", type: "hosts",
      help: "Links to your own network are refused unless listed here: one host or host:port per line. Changing it asks first." },
  ];
  const EVENT_LABELS = {
    started: "A download starts", done: "A download finishes", failed: "A download fails",
    stalled: "A download stalls", group_done: "A whole batch finishes", queue_empty: "The queue empties",
  };
  const OPEN_LABELS = { off: "Nothing", reveal: "Show the file in its folder", folder: "Open the folder", open: "Open the file" };
  const GROUP_ORDER = ["Where files go", "Speed", "Running at once", "Connections", "When a download finishes",
    "Notifications", "Lost downloads", "Safety", "Other"];
  // rows with their own editor, never generic
  const CUSTOM = { schedule: 1, ext_folders: 1 };

  function getPath(obj, path) {
    let node = obj;
    for (const key of String(path).split(".")) {
      if (node === null || typeof node !== "object" || !(key in node)) return undefined;
      node = node[key];
    }
    return node;
  }

  function isEventPath(path) { return /^notify\.[a-z_]+\.(enabled|level)$/.test(path); }

  // Which tool writes a key: the ones dl_prefs accepts go there (no extra question), the rest through
  // dl_settings (which asks).  The two lists come from the hub itself.
  function toolFor(path, payload) {
    return (payload.prefs_writable || []).indexOf(path) !== -1 ? PREFS_TOOL : SETTINGS_TOOL;
  }

  // The generic row for a writable key this page has no entry for.
  function extraRows(payload, known) {
    const out = [];
    (payload.writable || []).forEach((path) => {
      if (known[path] || CUSTOM[path] || isEventPath(path)) return;
      const value = getPath(payload.settings || {}, path);
      const type = typeof value === "boolean" ? "bool" : typeof value === "number" ? "float" : typeof value === "string" ? "text" : null;
      if (!type) return;      // lists / maps need their own editor: not shown rather than shown wrongly
      out.push({ path: path, group: "Other", label: path.replace(/[._]/g, " "), type: type });
    });
    return out;
  }

  // The rows to draw, in order, grouped: only keys the hub says are writable.
  function planRows(payload) {
    const writable = new Set(payload.writable || []);
    const known = {};
    FIELDS.forEach((f) => { known[f.path] = true; });
    const rows = FIELDS.filter((f) => writable.has(f.path)).concat(extraRows(payload, known));
    const groups = {};
    rows.forEach((r) => { (groups[r.group] = groups[r.group] || []).push(r); });
    return GROUP_ORDER.filter((g) => groups[g]).map((g) => ({ group: g, rows: groups[g] }));
  }

  // The provider settings this page edits (a provider's own `settings` block, as `dl_providers` reports it with
  // the defaults filled in).  Today that is Steam's update-size threshold (track C): a number of GB, 0 = every
  // update.  A row exists only when the hub reported the key, so an older hub shows nothing rather than a dead row.
  // The value is written with dl_provider_prefs, which asks first; the hub's validator owns the range.
  const PROVIDER_SETTING_FIELDS = {
    steam: [{ key: "notify_min_update_gb", label: "Steam: announce updates of at least", type: "float", min: 0, unit: "GB",
      help: "A Steam update smaller than this stays quiet; 0 = announce every update. An update whose size isn't known stays quiet unless this is 0." }],
  };
  function providerSettingRows(p) {
    const fields = PROVIDER_SETTING_FIELDS[String((p && p.id) || "")] || [];
    const have = (p && p.settings && typeof p.settings === "object") ? p.settings : {};
    return fields.filter((f) => typeof have[f.key] === "number").map((f) => Object.assign({}, f, { value: have[f.key] }));
  }
  function providerSettingArgs(id, key, value) { return { id: id, settings: { [key]: value } }; }

  function hostsToText(list) { return Array.isArray(list) ? list.join("\n") : ""; }
  function textToHosts(text) {
    return String(text || "").split(/[\n,;]+/).map((t) => t.trim()).filter(Boolean);
  }

  function daysChoice(days) {
    const key = DAYS.filter((d) => (days || []).indexOf(d) !== -1).join(",");
    if (key === DAYS.join(",")) return "all";
    if (key === "mon,tue,wed,thu,fri") return "weekdays";
    if (key === "sat,sun") return "weekends";
    return "custom";
  }
  function blankWindow() { return { days: "all", from: "01:00", to: "07:00", rate_limit_kbps: 0 }; }

  // The schedule as the editor holds it (days as the choice word), and back to what the hub validates.
  function windowsToModel(list) {
    return (Array.isArray(list) ? list : []).map((w) => ({
      days: daysChoice(w.days) === "custom" ? (w.days || []).join(",") : daysChoice(w.days),
      from: String(w.from || ""), to: String(w.to || ""), rate_limit_kbps: Number(w.rate_limit_kbps) || 0,
    }));
  }
  function modelToWindows(model) {
    return model.map((w) => ({ days: w.days, from: String(w.from).trim(), to: String(w.to).trim(),
      rate_limit_kbps: Number.isFinite(Number(w.rate_limit_kbps)) ? Number(w.rate_limit_kbps) : 0 }));
  }

  function extToModel(map) {
    return Object.keys(map || {}).sort().map((ext) => ({ ext: ext, folder: String(map[ext]) }));
  }
  function modelToExt(model) {
    const out = {};
    model.forEach((r) => {
      const ext = String(r.ext || "").trim();
      const folder = String(r.folder || "").trim();
      if (ext && folder) out[ext.charAt(0) === "." ? ext : "." + ext] = folder;
    });
    return out;
  }

  // ---- the Stremio card: pure half (brief 19, N9 step 3) -------------------------------------------------
  // The card is built from the hub's `stremio_status` answer (GET /api/hub/stremio).  Everything it says about Stremio comes
  // from that answer: the sentence is the hub's own, the buttons follow the hub's `can_start`, and the page decides nothing
  // about what Stremio is.  `stremioModel` turns an answer into what the card draws, so every state is checked without a DOM.

  // Which tone the state's title gets: ok (green), bad (red), or none.
  const STREMIO_TONE = { running: "ok", ready: "", starting: "", other_port: "", port_busy: "", silent: "", app_open: "",
    files_missing: "", start_failed: "bad", not_found: "bad" };

  function stremioFacts(s) {
    const path = (s && s.path) || {};
    const facts = [];
    facts.push(["Program", path.found && path.runtime_exe ? path.runtime_exe : "not found"]);
    facts.push(["server.js", path.found && path.server_js ? path.server_js : "not found"]);
    if (path.found && path.how_text) facts.push(["Found", path.how_text]);
    let app = "not found";
    if (s.state === "app_open") app = "open, but its server is silent";
    else if (s.installed_version) app = "installed, version " + s.installed_version;
    else if (path.found) app = "installed";
    facts.push(["App", app]);
    let server = "not running";
    if (s.state === "running") {
      server = "running at " + (s.address || "?") + (s.version ? " (version " + s.version + ")" : "")
        + (s.managed_by === "outside" ? ", started outside Jarvis" : s.managed_by === "jarvis" ? ", started by Jarvis" : "");
    } else if (s.state === "other_port") {
      server = "answers at " + (s.other_url || "another address") + ", not at " + (s.address || "?");
    } else if (s.state === "starting") {
      server = "starting";
    }
    facts.push(["Server", server]);
    const web = s.web;
    facts.push(["Web", !web ? "unknown" : web.answers ? "answering at " + web.url
      : web.configured ? "not answering at " + web.url : "not set up (web.dir is empty in the config)"]);
    (Array.isArray(s.problems) ? s.problems : []).forEach((p) => facts.push(["Problem", String(p)]));
    return facts;
  }

  // What the card draws for a status answer: title, tone, the hub's sentence and next step, the facts, and the Start button's state.
  function stremioModel(s) {
    if (!s) {
      return { loaded: false, title: "Stremio", tone: "", sentence: "Reading Stremio's status\u2026", next: "", facts: [], canStart: false,
        startTip: "Start isn't possible until the status has been read.", diagnostic: "" };
    }
    if (typeof s !== "object" || s.ok === false || !s.state) {
      const why = (s && s.error) ? String(s.error) : "Stremio's status couldn't be read.";
      return { loaded: false, title: "Stremio", tone: "bad", sentence: why, next: "", facts: [], canStart: false,
        startTip: "Start isn't possible until the status can be read.", diagnostic: "" };
    }
    const canStart = s.can_start === true;
    const startTip = canStart ? "Start Stremio's server (it may take up to " + (s.wait_seconds || 60) + " seconds)."
      : s.state === "running" ? "Stremio's server is already running; there is nothing to start."
        : (s.sentence || "Start isn't possible right now.");
    return { loaded: true, title: s.title || "Stremio", tone: STREMIO_TONE[s.state] || "", sentence: String(s.sentence || ""),
      next: String(s.next || ""), facts: stremioFacts(s), canStart: canStart, startTip: startTip, diagnostic: diagnosticFor(s) };
  }

  // The text Copy diagnostic copies: the one the hub prepared; if it sent none, one built here from the same fields.
  function diagnosticFor(s) {
    if (s && typeof s.diagnostic === "string" && s.diagnostic.trim()) return s.diagnostic;
    if (!s || typeof s !== "object") return "";
    const lines = ["Stremio status: " + (s.title || "?") + " (built by the page: the hub sent no diagnostic text)", s.sentence || "", "address: " + (s.address || "?")];
    stremioFacts(s).forEach((f) => lines.push(f[0] + ": " + f[1]));
    return lines.filter((l) => l !== "").join("\n");
  }

  // The folder a picker should open in for a path the hub knows (either slash), or "".
  function folderOf(p) {
    const text = String(p || "");
    const at = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
    return at > 0 ? text.slice(0, at) : "";
  }

  // The picker's options for one of the two files; it starts where the other known file lives, so the second pick is one click.
  function pickOptions(key, known, windows) {
    const here = known[key] || known[key === "runtime_exe" ? "server_js" : "runtime_exe"] || "";
    return {
      kind: "file", start: folderOf(here),
      title: key === "runtime_exe" ? "Locate the Stremio program (stremio-runtime.exe)" : "Locate Stremio's server.js",
      extensions: key === "server_js" ? [".js"] : (windows ? [".exe"] : []),
    };
  }

  // Locate...: pick one file (and the other too when the hub doesn't know it yet), then save the pair through stremio_save.
  // `deps.pick(options)` -> a path or null (cancelled); `deps.save(body)` -> the hub's answer or throws its refusal.  When the
  // hub reports the server answering at another address (state other_port) that address is saved with the paths: its own sentence
  // promises exactly that.  The hub checks both files exist and refuses with a sentence; it is shown as it is.
  async function locateFlow(deps, status, which) {
    const path = (status && status.path) || {};
    const known = { runtime_exe: path.runtime_exe || "", server_js: path.server_js || "" };
    const other = which === "runtime_exe" ? "server_js" : "runtime_exe";
    const order = known[other] ? [which] : [which, other];
    for (const key of order) {
      const picked = await deps.pick(pickOptions(key, known, deps.windows === true));
      if (!picked) return { status: "cancelled" };
      known[key] = String(picked);
    }
    const body = { runtime_exe: known.runtime_exe, server_js: known.server_js };
    if (status && status.state === "other_port" && status.other_url) body.server_url = status.other_url;
    try {
      return { status: "saved", result: await deps.save(body), body: body };
    } catch (err) {
      return { status: "refused", error: String((err && err.message) || err || "The hub didn't accept that."), body: body };
    }
  }

  // Start: one call that may take up to the hub's wait.  The page times out on its own a little after that, so a stuck request
  // can't leave the button working for ever.  `deps.start(timeoutMs)` -> the hub's answer, or throws (`timedOut` for the page's time-out).
  const START_SLACK_MS = 20000;
  function startTimeoutMs(status) {
    const wait = Number(status && status.wait_seconds);
    return (Number.isFinite(wait) && wait > 0 ? wait : 60) * 1000 + START_SLACK_MS;
  }
  async function startFlow(deps, status) {
    const ms = startTimeoutMs(status);
    try {
      const r = await deps.start(ms);
      return { status: "ok", text: String(r.sentence || r.note || "Stremio's server is running."), result: r };
    } catch (err) {
      if (err && err.timedOut) {
        return { status: "timeout", text: "Still no answer after " + Math.round(ms / 1000) + " seconds. Stremio may still be starting: press Check again in a moment." };
      }
      const data = (err && err.data) || {};
      return { status: "refused", text: String(data.error || (err && err.message) || "Stremio's server didn't start."), result: data };
    }
  }

  // ---- talking to the hub -------------------------------------------------------------------------------

  async function getJson(path) {
    const res = await fetch(path);
    let body = null;
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok || !body || body.ok === false) {
      throw new Error((body && (body.error || body.message)) || res.statusText || ("HTTP " + res.status));
    }
    return body;
  }

  async function postJson(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) throw new Error((data && (data.error || data.message)) || res.statusText || ("HTTP " + res.status));
    return data;
  }

  // A write to the hub's own web routes (/api/hub/*): they insist on the X-Jarvis-Hub header, so another web page can't make the
  // browser change anything.  Resolves with the hub's answer; throws its refusal (`err.data` = the whole answer), or an error with
  // `timedOut` when `timeoutMs` passes first.
  async function hubPost(path, body, timeoutMs) {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl && timeoutMs ? setTimeout(() => ctl.abort(), timeoutMs) : 0;
    try {
      const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Jarvis-Hub": "1" },
        body: JSON.stringify(body || {}), signal: ctl ? ctl.signal : undefined });
      let data = null;
      try { data = await res.json(); } catch (_) { /* not JSON */ }
      if (!res.ok || !data || data.ok === false) {
        const err = new Error((data && (data.error || data.message)) || res.statusText || ("HTTP " + res.status));
        err.data = data || {};
        throw err;
      }
      return data;
    } catch (err) {
      if (err && err.name === "AbortError") { const t = new Error("timed out"); t.timedOut = true; throw t; }
      throw err;
    } finally {
      if (timer && typeof clearTimeout === "function") clearTimeout(timer);
    }
  }

  // The same gate as the Downloads panel and the Debug panel: ask whether the call needs a confirmation,
  // show it, then run.  Declining rejects (the row puts its old value back).
  async function runHubTool(name, args) {
    if (!RUNNABLE[name]) throw new Error(name + " can't be run from Settings");
    const preview = await postJson("/api/tools/preview", { name: name, arguments: args });
    if (preview && (preview.confirm_required || preview.ai_review)) {
      const ui = global.JarvisUI;
      const ok = ui && typeof ui.confirm === "function"
        ? await ui.confirm({ title: "Change a download setting?", level: "warn", focusCancel: true, confirmLabel: "Change",
          body: JSON.stringify(args, null, 2).slice(0, 1200) })
        : false;
      if (!ok) throw new Error("Not changed: you declined.");
    }
    const r = await postJson("/api/tools/run", { name: name, arguments: args });
    const result = r && r.result !== undefined ? r.result : r;
    if (result && result.ok === false) throw new Error(result.error || "The hub didn't accept that.");
    return result;
  }

  function writeSetting(path, value, payload) {
    return runHubTool(toolFor(path, payload), { set: { [path]: value } });
  }

  // ---- the section --------------------------------------------------------------------------------------

  function build(ctx) {
    const ui = ctx.ui;
    const h = ui.h;
    return getJson("/api/hub/settings").then((payload) => {
      const blocks = [];

      function savedFlash(status) { ui.flash(status, "Saved", "ok"); setTimeout(() => ui.flash(status, ""), 1800); }

      function settingRow(spec, extra) {
        const status = h("span", { class: "prefs-row__status" });
        let value = getPath(payload.settings || {}, spec.path);
        let control = Object.assign({}, spec);
        if (spec.type === "choice") {
          const modes = (payload.open_modes || []).map((m) => [m, OPEN_LABELS[m] || m]);
          control = Object.assign({}, spec, { choices: modes });
        } else if (spec.type === "hosts") {
          control = Object.assign({}, spec, { type: "text" });
          value = (value || []).join(", ");
        }
        const commit = async (next) => {
          const sent = spec.type === "hosts" ? textToHosts(next) : next;
          await writeSetting(spec.path, sent, payload);
          savedFlash(status);
        };
        const ctl = ui.makeControl(control, value, commit, status);
        return ui.rowShell(spec.label, (extra && extra.help) || spec.help || "", ctl.el, status);
      }

      planRows(payload).forEach((g) => blocks.push(ui.group(g.group, g.rows.map((r) => settingRow(r)))));

      // ---- which events notify, and how important each is
      const writable = new Set(payload.writable || []);
      const N = global.JarvisNotifications;
      const levels = N && typeof N.pickerLevels === "function" ? N.pickerLevels() : { items: [] };
      const eventRows = [];
      (payload.events || []).forEach((ev) => {
        if (!writable.has("notify." + ev + ".enabled")) return;
        const onStatus = h("span", { class: "prefs-row__status" });
        const on = ui.makeControl({ type: "bool" }, getPath(payload.settings, "notify." + ev + ".enabled") === true,
          async (v) => { await writeSetting("notify." + ev + ".enabled", v, payload); savedFlash(onStatus); }, onStatus);
        eventRows.push(ui.rowShell(EVENT_LABELS[ev] || ev, "", on.el, onStatus));
        if (writable.has("notify." + ev + ".level") && levels.items.length) {
          const lvStatus = h("span", { class: "prefs-row__status" });
          const current = getPath(payload.settings, "notify." + ev + ".level");
          const choices = [["default", "Follow Settings > Notifications"]].concat(levels.items.map((l) => [l.level, l.label]));
          const lv = ui.makeControl({ type: "choice", choices: choices }, current === null || current === undefined ? "default" : current,
            async (v) => { await writeSetting("notify." + ev + ".level", v === "default" ? null : v, payload); savedFlash(lvStatus); }, lvStatus);
          eventRows.push(ui.rowShell((EVENT_LABELS[ev] || ev) + ": how important", "", lv.el, lvStatus));
        }
      });
      if (eventRows.length) blocks.push(ui.group("Which events notify", eventRows));

      // ---- speed windows
      if (writable.has("schedule")) blocks.push(scheduleBlock(ctx, payload));
      if (writable.has("ext_folders")) blocks.push(extFoldersBlock(ctx, payload));
      blocks.push(providersBlock(ctx, payload));
      blocks.push(stremioBlock(ctx));
      return blocks.filter(Boolean);
    });
  }

  // ---- the Stremio card (DOM) ---------------------------------------------------------------------------
  // Drawn at once with "Reading ..." and filled when the status arrives, so the rest of Settings never waits for it.
  function stremioBlock(ctx) {
    const ui = ctx.ui;
    const h = ui.h;
    const state = { status: null, busy: "" };
    const sentence = h("div", { class: "prefs-note", text: "Reading Stremio's status\u2026" });
    const next = h("div", { class: "prefs-row__help" });
    const title = h("div", { class: "prefs-row__label", text: "Stremio" });
    const facts = h("div", { class: "prefs-list" });
    const msg = h("span", { class: "prefs-row__status" });
    const diag = h("textarea", { class: "prefs-input prefs-input--wide", readonly: true, rows: 7, spellcheck: "false", "aria-label": "Stremio diagnostic text" });
    const btn = (text, onclick, extra) => h("button", Object.assign({ class: "btn btn--ghost btn--sm", type: "button", text: text, onclick: onclick }, extra || {}));
    const bCheck = btn("Check again", () => load());
    const bCopy = btn("Copy diagnostic", () => copy());
    const bProg = btn("Locate program\u2026", () => locate("runtime_exe"));
    const bJs = btn("Locate server.js\u2026", () => locate("server_js"));
    const bStart = h("button", { class: "btn btn--sm", type: "button", text: "Start", onclick: () => start() });
    const all = [bCheck, bCopy, bProg, bJs, bStart];

    function say(text, kind) { ui.flash(msg, text, kind); }
    function lock(word) { state.busy = word; all.forEach((b) => { b.disabled = !!word; }); }

    function paint() {
      const m = stremioModel(state.status);
      title.textContent = "Stremio: " + m.title;
      sentence.textContent = m.sentence;
      sentence.className = "prefs-note" + (m.tone === "bad" ? " prefs-note--warn" : "");
      next.textContent = m.next;
      facts.replaceChildren(...m.facts.map((f) => h("div", { class: "prefs-inline" }, [h("strong", { text: f[0] + ":" }), h("span", { text: " " + f[1] })])));
      diag.value = m.diagnostic;
      bStart.title = m.startTip;
      bStart.disabled = !!state.busy || !m.canStart;
      bCopy.disabled = !!state.busy || !m.diagnostic;
    }

    async function load() {
      lock("reading");
      say("");
      try { state.status = await getJson("/api/hub/stremio"); }
      catch (err) { state.status = { ok: false, error: "Couldn't read Stremio's status: " + err.message }; }
      lock("");
      paint();
    }

    async function copy() {
      const text = diagnosticFor(state.status);
      let ok = false;
      try { await global.navigator.clipboard.writeText(text); ok = true; } catch (_) { /* fall back below */ }
      if (!ok) {
        try { diag.focus(); diag.select(); ok = !!global.document.execCommand("copy"); } catch (_) { ok = false; }
      }
      say(ok ? "Copied" : "Couldn't copy: the text is selected below, press Ctrl+C", ok ? "ok" : "err");
    }

    async function locate(which) {
      const picker = global.JarvisPathPicker;
      if (!picker || typeof picker.open !== "function") { say("The file picker isn't available on this page.", "err"); return; }
      lock("locating");
      say("");
      const out = await locateFlow({
        pick: (o) => picker.open(o), save: (body) => hubPost("/api/hub/stremio/paths", body, 30000),
        windows: /win/i.test(String((global.navigator && (global.navigator.platform || global.navigator.userAgent)) || "")),
      }, state.status, which);
      lock("");
      if (out.status === "saved") { state.status = out.result; say("Saved", "ok"); }
      else if (out.status === "refused") say(out.error, "err");
      paint();
    }

    async function start() {
      lock("starting");
      say("Starting Stremio's server\u2026");
      const out = await startFlow({ start: (ms) => hubPost("/api/hub/stremio/start", {}, ms) }, state.status);
      lock("");
      say(out.text, out.status === "ok" ? "ok" : "err");
      await load2();
    }
    // after Start the status is read again (without clearing the sentence the button just said)
    async function load2() {
      try { state.status = await getJson("/api/hub/stremio"); } catch (_) { /* the message above stands */ }
      paint();
    }

    paint();
    load();
    return ui.group("Stremio", [
      ui.rowShell("Stremio's server", "What Jarvis found, whether it runs, and how to point it at Stremio. Locate... saves the two file paths for Jarvis only; it never changes Stremio.",
        h("div", {}, [title, sentence, next, facts]), msg),
      h("div", { class: "prefs-inline" }, all),
      h("details", {}, [h("summary", { text: "Diagnostic text" }), diag]),
    ]);
  }

  function listEditor(ctx, opts) {
    // opts: { title, help, model, columns:[{key,label,kind,choices?,width?}], blank(), save(model), addLabel, max }
    const ui = ctx.ui;
    const h = ui.h;
    const model = opts.model;
    const list = h("div", { class: "prefs-list" });
    const status = h("span", { class: "prefs-row__status" });
    function paint() {
      list.replaceChildren(...model.map((row, i) => {
        const cells = opts.columns.map((c) => {
          let input;
          if (c.kind === "select") {
            input = h("select", { class: "prefs-select", "aria-label": c.label });
            c.choices(row).forEach(([v, label]) => input.append(h("option", { value: String(v), text: label })));
            input.value = String(row[c.key]);
          } else {
            input = h("input", { type: c.kind === "number" ? "number" : "text", class: "prefs-input", "aria-label": c.label, placeholder: c.label, spellcheck: "false" });
            if (c.kind === "number") input.min = "0";
            input.value = row[c.key] === undefined || row[c.key] === null ? "" : String(row[c.key]);
          }
          input.addEventListener("change", () => { row[c.key] = c.kind === "number" ? Number(input.value) : input.value; });
          return input;
        });
        const remove = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Remove",
          onclick: () => { model.splice(i, 1); paint(); } });
        return h("div", { class: "prefs-inline" }, cells.concat([remove]));
      }));
      if (!model.length) list.append(h("div", { class: "prefs-empty", text: opts.empty || "None yet." }));
    }
    paint();
    const add = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: opts.addLabel,
      onclick: () => { if (model.length < (opts.max || 50)) { model.push(opts.blank()); paint(); } } });
    const save = h("button", { class: "btn btn--sm", type: "button", text: "Save", onclick: async () => {
      save.disabled = true;
      ui.flash(status, "");
      try { await opts.save(model); ui.flash(status, "Saved", "ok"); }
      catch (err) { ui.flash(status, err.message, "err"); }
      save.disabled = false;
    } });
    return ui.group(opts.title, [
      opts.help ? h("div", { class: "prefs-note", text: opts.help }) : null,
      list,
      h("div", { class: "prefs-inline" }, [add, save, status]),
    ].filter(Boolean));
  }

  function scheduleBlock(ctx, payload) {
    const dayChoices = (w) => [["all", "Every day"], ["weekdays", "Weekdays"], ["weekends", "Weekends"]]
      .concat(["all", "weekdays", "weekends"].indexOf(w.days) === -1 ? [[w.days, "Chosen days (" + w.days + ")"]] : []);
    return listEditor(ctx, {
      title: "Speed windows",
      help: "Inside a window its limit replaces the speed limit above (0 = unlimited there). The first window that matches wins; a window may cross midnight.",
      model: windowsToModel(getPath(payload.settings, "schedule")),
      columns: [
        { key: "days", label: "Days", kind: "select", choices: dayChoices },
        { key: "from", label: "From (hh:mm)", kind: "text" },
        { key: "to", label: "To (hh:mm)", kind: "text" },
        { key: "rate_limit_kbps", label: "Limit (KB/s)", kind: "number" },
      ],
      blank: blankWindow, addLabel: "Add a window", max: 10, empty: "No windows: the speed limit above always applies.",
      save: (model) => writeSetting("schedule", modelToWindows(model), payload),
    });
  }

  function extFoldersBlock(ctx, payload) {
    return listEditor(ctx, {
      title: "Folder per file type",
      help: "Send a kind of file somewhere of its own, e.g. .iso to ISOs. A folder name is inside the download folder; a longer ending (.tar.gz) wins over a shorter one.",
      model: extToModel(getPath(payload.settings, "ext_folders")),
      columns: [{ key: "ext", label: ".ext", kind: "text" }, { key: "folder", label: "Folder", kind: "text" }],
      blank: () => ({ ext: "", folder: "" }), addLabel: "Add a file type", max: 50, empty: "None: files go by kind (video, audio, other).",
      save: (model) => writeSetting("ext_folders", modelToExt(model), payload),
    });
  }

  function providersBlock(ctx, payload) {
    const ui = ctx.ui;
    const h = ui.h;
    const list = Array.isArray(payload.providers) ? payload.providers : [];
    if (!list.length) return null;
    const rows = [];
    list.forEach((p) => {
      const onStatus = h("span", { class: "prefs-row__status" });
      const on = ui.makeControl({ type: "bool" }, p.enabled === true, async (v) => {
        const r = await runHubTool(PROVIDER_TOOL, { id: p.id, enabled: v });
        if (r && r.ok === false) throw new Error(r.error);
        ui.flash(onStatus, "Saved", "ok");
      }, onStatus);
      rows.push(ui.rowShell(p.label || p.id, p.read_only ? "Shows its downloads here; has no controls." : "", on.el, onStatus));
      providerSettingRows(p).forEach((f) => {
        const stStatus = h("span", { class: "prefs-row__status" });
        const ctl = ui.makeControl({ type: f.type, min: f.min, unit: f.unit }, f.value, async (v) => {
          await runHubTool(PROVIDER_TOOL, providerSettingArgs(p.id, f.key, v));       // the hub's own error text reaches the row as it is
          ui.flash(stStatus, "Saved", "ok");
        }, stStatus);
        rows.push(ui.rowShell(f.label, f.help, ctl.el, stStatus));
      });
      if (typeof p.interval_s === "number") {
        const ivStatus = h("span", { class: "prefs-row__status" });
        const iv = ui.makeControl({ type: "float", min: 0.5, unit: "seconds" }, p.interval_s, async (v) => {
          await runHubTool(PROVIDER_TOOL, { id: p.id, interval_s: v });
          ui.flash(ivStatus, "Saved", "ok");
        }, ivStatus);
        rows.push(ui.rowShell((p.label || p.id) + ": check every", "", iv.el, ivStatus));
      }
    });
    return ui.group("Other tools' downloads", rows);
  }

  const section = {
    id: "downloads", label: "Downloads", title: "Downloads",
    lede: "Where downloads go, how fast and how many at once, what happens when one finishes, and which events notify you. The Downloads panel (Menu > Downloads) shows and controls the downloads themselves.",
    terms: "downloads download hub folder library speed limit bandwidth schedule window concurrency stremio yt-dlp video audio torrent steam provider notify notification finished after retry stall private host extension steam update size threshold gb unknown lost give up settle hours cancelled playnite stremio server start locate diagnostic",
    build: build,
  };

  global.JarvisSettingsSections = (global.JarvisSettingsSections || []).concat([section]);
  global.JarvisDownloadsSettings = { _pure: {
    FIELDS: FIELDS, planRows: planRows, extraRows: extraRows, toolFor: toolFor, getPath: getPath, hostsToText: hostsToText,
    textToHosts: textToHosts, daysChoice: daysChoice, windowsToModel: windowsToModel, modelToWindows: modelToWindows,
    extToModel: extToModel, modelToExt: modelToExt, isEventPath: isEventPath,
    PROVIDER_SETTING_FIELDS: PROVIDER_SETTING_FIELDS, providerSettingRows: providerSettingRows, providerSettingArgs: providerSettingArgs,
    stremioModel: stremioModel, stremioFacts: stremioFacts, diagnosticFor: diagnosticFor, folderOf: folderOf, pickOptions: pickOptions,
    locateFlow: locateFlow, startFlow: startFlow, startTimeoutMs: startTimeoutMs, hubPost: hubPost, STREMIO_TONE: STREMIO_TONE,
  } };
  if (typeof module !== "undefined" && module.exports) module.exports = global.JarvisDownloadsSettings;
})(typeof window !== "undefined" ? window : globalThis);
