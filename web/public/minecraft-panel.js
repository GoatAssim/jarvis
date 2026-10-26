/* ============================================================================
 * minecraft-panel.js — the "Minecraft" Menu panel (wave S3).
 *
 * Search Modrinth / CurseForge, list a project's compatible files, queue a download
 * to a folder, fetch OptiFine, make a modpack file. It is a thin face on the SIX
 * Minecraft tools (search_mc_mods, get_mc_project, list_optifine_versions,
 * download_mc_file, download_optifine, make_modpack_file) and nothing else:
 *
 *   - It never talks to a vendor. Every action is /api/tools/preview (the same
 *     confirmation gate the Debug panel uses) and then /api/tools/run.
 *   - It never receives the CurseForge key and never reads curseforge.json. "No key"
 *     is learned from the tools' own answer (reason no_key) and the panel then points
 *     at Settings > Integrations.
 *   - Vendor text (names, summaries, file names, errors) is untrusted. Everything is
 *     put on the page with textContent / setAttribute only, never as HTML, and a link
 *     is made only for an https address on a vendor's own host.
 *   - Nothing is installed. A queued download goes to Downloads; a pack is a file.
 *
 * Public:  window.JarvisMinecraft.open() / .close()
 * Tested:  window.JarvisMinecraft._pure (tests/verify_minecraft_panel.js).
 * ========================================================================= */
(function (global) {
  "use strict";

  const TOOLS = ["search_mc_mods", "get_mc_project", "list_optifine_versions",
    "download_mc_file", "download_optifine", "make_modpack_file"];
  const KINDS = ["mod", "modpack", "resourcepack", "shader", "datapack"];
  const LOADERS = ["fabric", "forge", "neoforge", "quilt"];
  const SORTS = ["relevance", "downloads", "updated", "newest"];
  const CHANNELS = ["release", "beta", "alpha"];
  const LINK_HOSTS = ["modrinth.com", "curseforge.com", "optifine.net"];
  const MAX_ENTRIES = 500;
  const LIBRARY_FOLDER = "Minecraft";

  // ------------------------------------------------------------------ pure helpers

  function text(v, max) {
    let s = v === null || v === undefined ? "" : String(v);
    s = s.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "");
    return max && s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
  }

  function trimmed(v) { return text(v).trim(); }

  function oneOf(v, list, fallback) {
    const s = trimmed(v).toLowerCase();
    return list.indexOf(s) >= 0 ? s : fallback;
  }

  function count(n) {
    if (typeof n !== "number" || !isFinite(n)) return "";
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n);
  }

  function size(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return "";
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  }

  function day(iso) { const m = /^(\d{4}-\d{2}-\d{2})/.exec(trimmed(iso)); return m ? m[1] : ""; }

  // A link only when it is https and on a vendor's own host; anything else stays plain text.
  function safeUrl(u) {
    const s = trimmed(u);
    let url;
    try { url = new URL(s); } catch (_) { return null; }
    if (url.protocol !== "https:" || url.username || url.password) return null;
    const host = url.hostname.toLowerCase();
    return LINK_HOSTS.some((h) => host === h || host.endsWith("." + h)) ? url.href : null;
  }

  function put(args, key, value) {
    const v = trimmed(value);
    if (v) args[key] = v;
  }

  function searchArgs(f) {
    const args = { query: trimmed(f.query), kind: oneOf(f.kind, KINDS, "mod"), sort: oneOf(f.sort, SORTS, "relevance") };
    const source = oneOf(f.source, ["modrinth", "curseforge", "both"], "");
    if (source) args.source = source;                          // empty = the tool's own default
    put(args, "mc_version", f.mc_version);
    const loader = oneOf(f.loader, LOADERS, "");
    if (loader) args.loader = loader;
    const limit = parseInt(f.limit, 10);
    if (limit >= 1 && limit <= 10) args.limit = limit;
    const offset = parseInt(f.offset, 10);
    if (offset > 0) args.offset = offset;
    return args;
  }

  function projectArgs(f) {
    const args = { project: trimmed(f.project), source: oneOf(f.source, ["modrinth", "curseforge"], "modrinth"),
      channel: oneOf(f.channel, CHANNELS, "release") };
    put(args, "mc_version", f.mc_version);
    const loader = oneOf(f.loader, LOADERS, "");
    if (loader) args.loader = loader;
    const n = parseInt(f.files_limit, 10);
    if (n >= 0) args.files_limit = n;
    return args;
  }

  function downloadArgs(f, dry) {
    const args = { project: trimmed(f.project), source: oneOf(f.source, ["modrinth", "curseforge"], "modrinth"),
      kind: oneOf(f.kind, KINDS, "mod"), channel: oneOf(f.channel, CHANNELS, "release") };
    put(args, "mc_version", f.mc_version);
    const loader = oneOf(f.loader, LOADERS, "");
    if (loader) args.loader = loader;
    put(args, "file", f.file);
    put(args, "dest", f.dest);
    put(args, "filename", f.filename);
    if (f.with_dependencies) args.with_dependencies = true;
    if (dry) args.dry_run = true;
    return args;
  }

  function optifineListArgs(f) {
    const args = {};
    put(args, "mc_version", f.mc_version);
    if (f.include_preview) args.include_preview = true;
    return args;
  }

  function optifineArgs(f, dry) {
    const args = { mc_version: trimmed(f.mc_version) };
    put(args, "version", f.version);
    if (f.preview) args.preview = true;
    put(args, "dest", f.dest);
    put(args, "filename", f.filename);
    if (dry) args.dry_run = true;
    return args;
  }

  // One project per line. "Sodium" or "Sodium | <file id>" (pin one file) or "Sodium | | beta".
  function parseEntries(raw) {
    const out = [];
    const lines = text(raw).split(/\r?\n/);
    for (const line of lines) {
      const parts = line.split("|").map((p) => p.trim());
      if (!parts[0]) continue;
      const entry = { project: parts[0] };
      if (parts[1]) entry.file = parts[1];
      if (parts[2] && CHANNELS.indexOf(parts[2].toLowerCase()) >= 0) entry.channel = parts[2].toLowerCase();
      out.push(entry);
      if (out.length >= MAX_ENTRIES) break;
    }
    return out;
  }

  function packArgs(f, dry) {
    const args = { format: oneOf(f.format, ["modrinth", "curseforge"], "modrinth"), name: trimmed(f.name),
      mc_version: trimmed(f.mc_version), loader: oneOf(f.loader, LOADERS.concat(["vanilla"]), "fabric"),
      entries: parseEntries(f.entries), channel: oneOf(f.channel, CHANNELS, "release") };
    put(args, "version", f.version);
    put(args, "author", f.author);
    put(args, "summary", f.summary);
    put(args, "loader_version", f.loader_version);
    put(args, "src", f.src);
    put(args, "dest", f.dest);
    put(args, "filename", f.filename);
    if (f.include_dependencies === false) args.include_dependencies = false;
    if (f.overwrite) args.overwrite = true;
    if (dry) args.dry_run = true;
    return args;
  }

  // The first thing wrong with a form, in plain words, before any call is made.
  function problem(kind, args) {
    if (kind === "search") return args.query ? "" : "Type what to search for.";
    if (kind === "project" || kind === "download") return args.project ? "" : "Say which project: its slug or id.";
    if (kind === "optifine-download") return args.mc_version ? "" : "Say the Minecraft version, like 1.20.1.";
    if (kind === "pack") {
      if (!args.name) return "Give the pack a name.";
      if (!args.mc_version) return "Say the Minecraft version, like 1.20.1.";
      if (!args.entries || !args.entries.length) return "Add at least one project (one per line).";
    }
    return "";
  }

  // What happened, as one of a few cases the panel knows how to explain.
  function classify(result) {
    if (!result || typeof result !== "object") return { kind: "fail", message: "Jarvis gave no answer." };
    const err = text(result.error || "");
    if (result.reason === "timeout" || /timed out/i.test(err)) return { kind: "timeout", message: err };
    if (result.ok === false) {
      if (result.cancelled === true || /not confirmed/i.test(err)) return { kind: "not_confirmed", message: err };
      if (result.reason === "no_key") return { kind: "no_key", message: err };
      if (result.reason === "ambiguous") return { kind: "ambiguous", message: err };
      return { kind: "fail", message: err || "That did not work." };
    }
    if (result.cancelled === true) return { kind: "not_confirmed", message: text(result.message || "") };
    return { kind: "ok", message: text(result.message || "") };
  }

  function wantsNoKeyHint(result) {
    return !!result && typeof result === "object"
      && (result.reason === "no_key" || result.curseforge_reason === "no_key");
  }

  // Does the answer name a folder the hub will ask about? Absolute path, ~ or drive letter.
  function isOutsideLibrary(dest) {
    const d = trimmed(dest);
    return d === "" || /^(~|[a-zA-Z]:[\\/]|[\\/])/.test(d);   // omitted = the default folder (~/Downloads)
  }

  // ------------------------------------------------------------------ DOM helpers

  function el(tag, props, children) {
    const node = document.createElement(tag);
    const p = props || {};
    Object.keys(p).forEach((k) => {
      const v = p[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = text(v);
      else if (k.slice(0, 2) === "on" && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    [].concat(children === undefined ? [] : children).forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(text(c)) : c);
    });
    return node;
  }

  function UI() { return global.JarvisUI || null; }

  function say(message, level) {
    const ui = UI();
    if (ui && ui.toast) ui.toast({ message: text(message, 240), level: level || "info" });
  }

  function field(label, control, hint) {
    return el("label", { class: "mc-field" }, [
      el("span", { class: "mc-field__label", text: label }), control,
      hint ? el("span", { class: "mc-field__hint", text: hint }) : null,
    ]);
  }

  function input(name, value, attrs) {
    return el("input", Object.assign({ type: "text", class: "mc-input", name, value: value || "", autocomplete: "off", spellcheck: "false" }, attrs || {}));
  }

  function select(name, options, value) {
    const s = el("select", { class: "mc-input", name });
    options.forEach((o) => {
      const opt = el("option", { value: o[0], text: o[1] });
      if (o[0] === value) opt.selected = true;
      s.appendChild(opt);
    });
    return s;
  }

  function check(name, label, on) {
    const box = el("input", { type: "checkbox", name });
    box.checked = !!on;
    return el("label", { class: "mc-check" }, [box, el("span", { text: label })]);
  }

  function readForm(root) {
    const out = {};
    root.querySelectorAll("[name]").forEach((c) => {
      out[c.getAttribute("name")] = c.type === "checkbox" ? c.checked : c.value;
    });
    return out;
  }

  function setField(root, name, value) {
    const c = root.querySelector('[name="' + name + '"]');
    if (!c) return;
    if (c.type === "checkbox") c.checked = !!value; else c.value = value === undefined || value === null ? "" : String(value);
  }

  function link(url, label) {
    const safe = safeUrl(url);
    return safe ? el("a", { href: safe, target: "_blank", rel: "noopener noreferrer", class: "mc-link", text: label || safe })
      : el("span", { class: "mc-muted", text: label || text(url, 120) });
  }

  function chips(values) {
    const list = (Array.isArray(values) ? values : []).filter((v) => v !== null && v !== undefined && v !== "");
    return list.length ? el("span", { class: "mc-chips" }, list.map((v) => el("span", { class: "mc-chip", text: v }))) : null;
  }

  // ------------------------------------------------------------------ talking to the tools

  async function post(url, body) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok && !(data && data.result)) throw new Error((data && (data.error || data.message)) || res.statusText || ("HTTP " + res.status));
    return data || {};
  }

  /* Same gate as the Debug panel: ask the server whether this call needs a confirmation,
     show it (with the exact folder), and run only after a yes. `summary` is what the owner
     should read; `sure` forces the question for a real write even if the server did not ask. */
  async function runTool(name, args, opts) {
    const o = opts || {};
    if (TOOLS.indexOf(name) < 0) return { ok: false, reason: "unsupported", error: name + " is not a Minecraft tool." };
    let gated = !!o.sure;
    if (!o.skipGate) {
      try {
        const pre = await post("/api/tools/preview", { name, arguments: args });
        if (pre && (pre.confirm_required || pre.ai_review)) gated = true;
      } catch (e) {
        return { ok: false, reason: "panel", error: "Could not check whether " + name + " needs confirmation: " + text(e.message, 160) };
      }
    }
    if (gated && !o.skipGate) {
      const ui = UI();
      const yes = ui && ui.confirm ? await ui.confirm({
        title: o.title || ("Run " + name + "?"), level: "warn", focusCancel: true, confirmLabel: o.confirmLabel || "Run",
        body: text(o.summary || "", 900) + (o.summary ? "\n\n" : "") + JSON.stringify(args, null, 2).slice(0, 1200),
      }) : false;
      if (!yes) return { ok: false, cancelled: true, reason: "cancelled", error: "Cancelled: nothing was run." };
    }
    try {
      // confirm_ui: the owner just read the exact folder in the dialog above; the server may pass that
      // yes on to the hub's own "Save outside the downloads library?" question (see "For the merge").
      const body = { name, arguments: args };
      if (o.answeredFolder) body.confirm_ui = true;
      const data = await post("/api/tools/run", body);
      return data.result && typeof data.result === "object" ? data.result : { ok: false, error: "Jarvis gave an answer the panel can't read." };
    } catch (e) {
      const msg = text(e.message, 200);
      return { ok: false, reason: /timed out/i.test(msg) ? "timeout" : "panel", error: msg };
    }
  }

  // ------------------------------------------------------------------ result rendering

  function notice(tone, message, buttons) {
    return el("div", { class: "mc-notice mc-notice--" + tone, role: tone === "error" ? "alert" : "status" }, [
      el("div", { class: "mc-notice__text", text: message }),
      buttons && buttons.length ? el("div", { class: "mc-notice__actions" }, buttons) : null,
    ]);
  }

  function rawDetails(result) {
    let json = "";
    try { json = JSON.stringify(result, null, 2); } catch (_) { json = ""; }
    return el("details", { class: "mc-raw" }, [el("summary", { text: "Raw answer" }), el("pre", { class: "mc-pre", text: text(json, 20000) })]);
  }

  function hitCard(hit, handlers) {
    const name = text(hit.name, 120) || text(hit.slug, 80) || text(hit.id, 80);
    const open = hit.distributable === false ? el("span", { class: "mc-chip mc-chip--warn", text: "not downloadable" }) : null;
    return el("li", { class: "mc-card" }, [
      el("div", { class: "mc-card__head" }, [
        el("span", { class: "mc-card__title", text: name }),
        el("span", { class: "mc-chip", text: text(hit.source, 20) }),
        hit.kind && hit.kind !== "mod" ? el("span", { class: "mc-chip", text: text(hit.kind, 20) }) : null, open,
      ]),
      el("div", { class: "mc-card__meta" }, [
        hit.author ? el("span", { text: "by " + text(hit.author, 40) }) : null,
        hit.downloads ? el("span", { text: count(hit.downloads) + " downloads" }) : null,
        hit.updated ? el("span", { text: "updated " + day(hit.updated) }) : null,
      ]),
      hit.summary ? el("p", { class: "mc-card__summary", text: hit.summary }) : null,
      el("div", { class: "mc-card__foot" }, [
        chips((hit.loaders || []).concat((hit.mc_versions || []).slice(0, 4))),
        el("span", { class: "mc-spacer" }),
        hit.page_url ? link(hit.page_url, "Open page") : null,
        el("button", { type: "button", class: "btn btn--sm btn--outline", text: "Files",
          onclick: () => handlers.files(hit) }),
      ]),
    ]);
  }

  function fileTable(files, recommended, onQueue) {
    const rows = (files || []).map((f) => el("tr", { class: f.file_id && f.file_id === recommended ? "is-recommended" : "" }, [
      el("td", { text: text(f.version, 60) || "(no label)" }),
      el("td", { class: "mc-mono", text: text(f.name, 90) }),
      el("td", { text: text(f.channel, 10) }),
      el("td", {}, [chips((f.mc_versions || []).slice(0, 4))]),
      el("td", {}, [chips(f.loaders || []), f.via ? el("span", { class: "mc-chip mc-chip--warn", text: "via " + text(f.via, 10) }) : null]),
      el("td", { text: size(f.size) }),
      el("td", { text: day(f.date) }),
      el("td", {}, [f.downloadable
        ? el("button", { type: "button", class: "btn btn--sm btn--outline", text: "Queue\u2026", onclick: () => onQueue(f) })
        : el("span", { class: "mc-chip mc-chip--warn", text: "not downloadable" })]),
    ]));
    return el("div", { class: "mc-tablewrap" }, [el("table", { class: "mc-table" }, [
      el("thead", {}, [el("tr", {}, ["Version", "File", "Channel", "Minecraft", "Loaders", "Size", "Date", ""].map((h) => el("th", { text: h })))]),
      el("tbody", {}, rows),
    ])]);
  }

  // Any array of plain objects in an answer (OptiFine builds, pack entries...) as a small table of its simple fields.
  function genericTables(result) {
    const out = [];
    Object.keys(result || {}).forEach((key) => {
      const v = result[key];
      if (!Array.isArray(v) || !v.length || typeof v[0] !== "object" || v[0] === null) return;
      const cols = [];
      v.slice(0, 200).forEach((row) => Object.keys(row).forEach((c) => {
        const x = row[c];
        if ((typeof x === "string" || typeof x === "number" || typeof x === "boolean") && cols.indexOf(c) < 0 && cols.length < 7) cols.push(c);
      }));
      if (!cols.length) return;
      out.push(el("div", { class: "mc-tablewrap" }, [
        el("div", { class: "mc-subhead", text: key.replace(/_/g, " ") + " (" + v.length + ")" }),
        el("table", { class: "mc-table" }, [
          el("thead", {}, [el("tr", {}, cols.map((c) => el("th", { text: c.replace(/_/g, " ") })))]),
          el("tbody", {}, v.slice(0, 200).map((row) => el("tr", {}, cols.map((c) => el("td", { text: text(row[c], 90) }))))),
        ]),
      ]));
    });
    return out;
  }

  function factsOf(result, keys) {
    const rows = [];
    keys.forEach((k) => {
      const v = result[k];
      if (v === undefined || v === null || v === "" || typeof v === "object") return;
      rows.push(el("div", { class: "mc-fact" }, [el("span", { class: "mc-fact__k", text: k.replace(/_/g, " ") }), el("span", { class: "mc-fact__v", text: text(v, 300) })]));
    });
    return rows.length ? el("div", { class: "mc-facts" }, rows) : null;
  }

  // ------------------------------------------------------------------ the panel

  const S = { overlay: null, panel: null, tab: "search", prevFocus: null, onKey: null, tabs: {} };

  const TAB_LIST = [["search", "Search"], ["project", "Project files"], ["optifine", "OptiFine"], ["pack", "Make a pack"]];

  function pickFolder(root, name, kind) {
    const picker = global.JarvisPathPicker;
    if (!picker || !picker.open) { say("The folder picker is not available here: type the path.", "warn"); return; }
    const start = (root.querySelector('[name="' + name + '"]') || {}).value || "";
    Promise.resolve(picker.open({ kind: kind || "folder", start, title: "Choose a folder" })).then((picked) => {
      const p = picked && typeof picked === "object" ? (picked.path || picked.full || "") : picked;
      if (typeof p === "string" && p) setField(root, name, p);
    }).catch(() => say("The folder picker failed.", "warn"));
  }

  function destRow(root, hint) {
    return field("Save to", el("div", { class: "mc-row" }, [
      input("dest", "", { placeholder: "empty = your default download folder" }),
      el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Browse\u2026", onclick: () => pickFolder(root, "dest") }),
    ]), hint);
  }

  // After any answer: the extra buttons its situation calls for.
  function followUps(result, root, ctx) {
    const kind = classify(result).kind;
    const bits = [];
    if (kind === "no_key" || wantsNoKeyHint(result)) {
      bits.push(notice("info", "CurseForge needs your own free key. Modrinth and OptiFine work without it.", [
        el("button", { type: "button", class: "btn btn--sm btn--outline", text: "Open Minecraft settings", onclick: () => {
          close();
          if (global.JarvisSettings && global.JarvisSettings.show) global.JarvisSettings.show("integrations");
        } })]));
    }
    if (kind === "not_confirmed" && ctx && ctx.canUseLibrary) {
      bits.push(notice("warn", "Nothing was saved. Jarvis's Downloads asks before it writes outside its own library folder, and "
        + "this panel can't pass that question on yet. You can save inside the library instead.", [
        el("button", { type: "button", class: "btn btn--sm btn--outline", text: "Use the \u201c" + LIBRARY_FOLDER + "\u201d folder in the library",
          onclick: () => { setField(root, "dest", LIBRARY_FOLDER); say("Folder set. Press the button again.", "info"); } })]));
    }
    if (kind === "timeout") {
      bits.push(notice("warn", "This took longer than the panel waits. It may still have finished: check Downloads or the folder before trying again."));
    }
    return bits;
  }

  function showResult(host, result, opts) {
    const o = opts || {};
    host.textContent = "";
    const c = classify(result);
    const tone = c.kind === "ok" ? (result && result.warning ? "warn" : "ok") : (c.kind === "not_confirmed" || c.kind === "timeout" ? "warn" : "error");
    host.appendChild(notice(tone, c.message || (c.kind === "ok" ? "Done." : "That did not work.")));
    if (result && result.warning) host.appendChild(notice("warn", text(result.warning, 400)));
    if (result && result.untrusted_note) host.appendChild(el("div", { class: "mc-muted mc-small", text: text(result.untrusted_note, 300) }));
    (o.root ? followUps(result, o.root, o) : []).forEach((n) => host.appendChild(n));
    if (o.body) o.body(result, host);
    if (result && typeof result === "object") {
      const f = factsOf(result, ["dest", "route", "source", "queued", "downloaded", "written", "loader_version", "loader_version_resolved_as"]);
      if (f) host.appendChild(f);
      genericTables(result).forEach((t) => host.appendChild(t));
      host.appendChild(rawDetails(result));
    }
  }

  async function guarded(btn, fn) {
    if (btn.disabled) return;
    btn.disabled = true;
    try { await fn(); } finally { btn.disabled = false; }
  }

  // ---- tab: search

  function buildSearch() {
    const root = el("div", { class: "mc-tab" });
    const results = el("div", { class: "mc-results", "aria-live": "polite" });
    const form = el("div", { class: "mc-form" }, [
      field("Search for", input("query", "", { placeholder: "a mod name, or a few words" })),
      el("div", { class: "mc-grid" }, [
        field("Where", select("source", [["", "Automatic"], ["modrinth", "Modrinth"], ["curseforge", "CurseForge"], ["both", "Both"]], "")),
        field("What", select("kind", KINDS.map((k) => [k, k]), "mod")),
        field("Minecraft version", input("mc_version", "", { placeholder: "1.20.1" })),
        field("Loader", select("loader", [["", "any"]].concat(LOADERS.map((l) => [l, l])), "")),
        field("Sort", select("sort", SORTS.map((s) => [s, s]), "relevance")),
      ]),
    ]);
    const run = async (offset) => {
      const f = readForm(form);
      const args = searchArgs(Object.assign({}, f, { limit: 10, offset }));
      const bad = problem("search", args);
      if (bad) { results.textContent = ""; results.appendChild(notice("warn", bad)); return; }
      results.textContent = ""; results.appendChild(el("div", { class: "mc-muted", text: "Searching\u2026" }));
      const r = await runTool("search_mc_mods", args, {});
      showResult(results, r, { root, body: (res, host) => renderHits(res, host, args, run) });
    };
    const go = el("button", { type: "button", class: "btn btn--primary", text: "Search", onclick: (e) => guarded(e.currentTarget, () => run(0)) });
    form.appendChild(el("div", { class: "mc-actions" }, [go]));
    form.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target && e.target.tagName === "INPUT" && e.target.type === "text") { e.preventDefault(); guarded(go, () => run(0)); } });
    root.appendChild(form); root.appendChild(results);
    return root;
  }

  function renderHits(res, host, args, rerun) {
    const handlers = { files: (hit) => openProject(hit) };
    const lists = [];
    if (Array.isArray(res.hits)) lists.push(["", res.hits, res.total]);
    ["modrinth", "curseforge"].forEach((s) => {
      if (Array.isArray(res[s + "_hits"])) lists.push([s, res[s + "_hits"], res[s + "_total"]]);
      if (res[s + "_error"]) host.appendChild(notice("warn", (s === "modrinth" ? "Modrinth: " : "CurseForge: ") + text(res[s + "_error"], 300)));
    });
    lists.forEach((l) => {
      if (l[0]) host.appendChild(el("div", { class: "mc-subhead", text: (l[0] === "modrinth" ? "Modrinth" : "CurseForge") + (typeof l[2] === "number" ? " \u2014 " + count(l[2]) + " found" : "") }));
      host.appendChild(l[1].length ? el("ul", { class: "mc-cards" }, l[1].map((h) => hitCard(h, handlers))) : el("div", { class: "mc-muted", text: "Nothing matched." }));
    });
    const shown = lists.reduce((n, l) => Math.max(n, l[1].length), 0);
    const off = args.offset || 0;
    if (off > 0 || shown >= (args.limit || 10)) {
      host.appendChild(el("div", { class: "mc-actions" }, [
        off > 0 ? el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Previous", onclick: () => rerun(Math.max(0, off - (args.limit || 10))) }) : null,
        shown >= (args.limit || 10) ? el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Next", onclick: () => rerun(off + (args.limit || 10)) }) : null,
      ]));
    }
  }

  // ---- tab: project files + queue a download

  function buildProject() {
    const root = el("div", { class: "mc-tab" });
    const results = el("div", { class: "mc-results", "aria-live": "polite" });
    const queueHost = el("div", { class: "mc-results" });
    const form = el("div", { class: "mc-form" }, [
      field("Project", input("project", "", { placeholder: "slug or id, e.g. sodium" })),
      el("div", { class: "mc-grid" }, [
        field("Where", select("source", [["modrinth", "Modrinth"], ["curseforge", "CurseForge"]], "modrinth")),
        field("Minecraft version", input("mc_version", "", { placeholder: "1.20.1" })),
        field("Loader", select("loader", [["", "any"]].concat(LOADERS.map((l) => [l, l])), "")),
        field("Least stable", select("channel", CHANNELS.map((c) => [c, c]), "release")),
        field("How many files", input("files_limit", "5")),
      ]),
    ]);
    const dl = el("div", { class: "mc-form mc-form--download" }, [
      el("div", { class: "mc-subhead", text: "Queue a download" }),
      el("div", { class: "mc-grid" }, [
        field("File id", input("file", "", { placeholder: "empty = newest compatible" })),
        field("Kind", select("kind", KINDS.map((k) => [k, k]), "mod")),
        field("Save as", input("filename", "", { placeholder: "empty = the official name" })),
      ]),
      destRow(root, "A folder outside Jarvis's library asks you once more."),
      check("with_dependencies", "Also queue the required dependencies", false),
    ]);
    const syncDownload = () => {
      ["project", "source", "mc_version", "loader", "channel"].forEach((k) => { const v = (form.querySelector('[name="' + k + '"]') || {}).value; if (v !== undefined) setField(dl, k, v); });
    };
    // The download form shares project/source/mc/loader/channel with the lookup form above it: read both.
    const downloadForm = () => Object.assign({}, readForm(form), readForm(dl));
    const lookup = async () => {
      const args = projectArgs(readForm(form));
      const bad = problem("project", args);
      if (bad) { results.textContent = ""; results.appendChild(notice("warn", bad)); return; }
      results.textContent = ""; results.appendChild(el("div", { class: "mc-muted", text: "Looking\u2026" }));
      const r = await runTool("get_mc_project", args, {});
      showResult(results, r, { root, body: (res, host) => {
        if (res.candidates) host.appendChild(el("ul", { class: "mc-cards" }, res.candidates.map((h) => hitCard(h, { files: (x) => fillAndLook(x) }))));
        if (res.project) {
          const p = res.project;
          host.appendChild(el("div", { class: "mc-card mc-card--project" }, [
            el("div", { class: "mc-card__head" }, [el("span", { class: "mc-card__title", text: text(p.name, 120) }), el("span", { class: "mc-chip", text: text(p.source, 20) })]),
            p.summary ? el("p", { class: "mc-card__summary", text: text(p.summary, 300) }) : null,
            p.page_url ? el("div", {}, [link(p.page_url, "Open page")]) : null,
          ]));
          if (res.files) host.appendChild(fileTable(res.files, res.recommended_file_id, (f) => { setField(dl, "file", f.file_id); syncDownload(); dl.scrollIntoView({ block: "nearest" }); say("File picked. Choose a folder and press Queue.", "info"); }));
        }
      } });
    };
    const fillAndLook = (hit) => { setField(form, "project", hit.slug || hit.id); setField(form, "source", hit.source === "curseforge" ? "curseforge" : "modrinth"); guarded(look, lookup); };
    const doDownload = async (dry) => {
      syncDownload();
      const f = downloadForm();
      const args = downloadArgs(f, dry);
      const bad = problem("download", args);
      if (bad) { queueHost.textContent = ""; queueHost.appendChild(notice("warn", bad)); return; }
      queueHost.textContent = ""; queueHost.appendChild(el("div", { class: "mc-muted", text: dry ? "Checking\u2026" : "Queuing\u2026" }));
      let r;
      if (dry) {
        r = await runTool("download_mc_file", args, {});
      } else {
        const check1 = await runTool("download_mc_file", downloadArgs(f, true), {});
        if (!check1 || check1.ok === false) { showResult(queueHost, check1, { root, canUseLibrary: true }); return; }
        r = await runTool("download_mc_file", args, { sure: true, title: "Queue this download?", confirmLabel: "Queue",
          summary: text(check1.message, 700), answeredFolder: isOutsideLibrary(args.dest) });
      }
      showResult(queueHost, r, { root, canUseLibrary: true });
      if (r && r.ok !== false && r.queued) say("Queued in Downloads. Nothing is installed.", "success");
    };
    const look = el("button", { type: "button", class: "btn btn--primary", text: "Show files", onclick: (e) => guarded(e.currentTarget, lookup) });
    const dry = el("button", { type: "button", class: "btn btn--ghost", text: "Check only (dry run)", onclick: (e) => guarded(e.currentTarget, () => doDownload(true)) });
    const queue = el("button", { type: "button", class: "btn btn--primary", text: "Queue download", onclick: (e) => guarded(e.currentTarget, () => doDownload(false)) });
    form.appendChild(el("div", { class: "mc-actions" }, [look]));
    dl.appendChild(el("div", { class: "mc-actions" }, [dry, queue]));
    root.appendChild(form); root.appendChild(results); root.appendChild(dl); root.appendChild(queueHost);
    root._fill = (hit) => fillAndLook(hit);
    return root;
  }

  // ---- tab: OptiFine

  function buildOptifine() {
    const root = el("div", { class: "mc-tab" });
    const listHost = el("div", { class: "mc-results", "aria-live": "polite" });
    const dlHost = el("div", { class: "mc-results", "aria-live": "polite" });
    const form = el("div", { class: "mc-form" }, [
      el("div", { class: "mc-grid" }, [
        field("Minecraft version", input("mc_version", "", { placeholder: "1.20.1 (empty = overview)" })),
        field("Build", input("version", "", { placeholder: "empty = the recommended one" })),
      ]),
      check("include_preview", "List preview (test) builds too", false),
      check("preview", "Download a preview build", false),
      destRow(root, "Mods folder, for example. Outside the library it asks once more."),
      field("Save as", input("filename", "", { placeholder: "empty = the official name (.jar)" })),
    ]);
    const list = async () => {
      listHost.textContent = ""; listHost.appendChild(el("div", { class: "mc-muted", text: "Asking optifine.net\u2026" }));
      const r = await runTool("list_optifine_versions", optifineListArgs(readForm(form)), {});
      showResult(listHost, r, { root });
    };
    const get = async (dry) => {
      const args = optifineArgs(readForm(form), dry);
      const bad = problem("optifine-download", args);
      if (bad) { dlHost.textContent = ""; dlHost.appendChild(notice("warn", bad)); return; }
      dlHost.textContent = ""; dlHost.appendChild(el("div", { class: "mc-muted", text: dry ? "Checking\u2026" : "Downloading\u2026" }));
      let r;
      if (dry) r = await runTool("download_optifine", args, {});
      else {
        const pre = await runTool("download_optifine", optifineArgs(readForm(form), true), {});
        if (!pre || pre.ok === false) { showResult(dlHost, pre, { root, canUseLibrary: true }); return; }
        r = await runTool("download_optifine", args, { sure: true, title: "Download OptiFine?", confirmLabel: "Download",
          summary: text(pre.message || "OptiFine will be downloaded from optifine.net. It is not installed.", 700), answeredFolder: isOutsideLibrary(args.dest) });
      }
      showResult(dlHost, r, { root, canUseLibrary: true });
    };
    form.appendChild(el("div", { class: "mc-actions" }, [
      el("button", { type: "button", class: "btn btn--ghost", text: "List versions", onclick: (e) => guarded(e.currentTarget, list) }),
      el("button", { type: "button", class: "btn btn--ghost", text: "Check only (dry run)", onclick: (e) => guarded(e.currentTarget, () => get(true)) }),
      el("button", { type: "button", class: "btn btn--primary", text: "Download OptiFine", onclick: (e) => guarded(e.currentTarget, () => get(false)) }),
    ]));
    root.appendChild(form); root.appendChild(listHost); root.appendChild(dlHost);
    return root;
  }

  // ---- tab: make a pack

  function buildPack() {
    const root = el("div", { class: "mc-tab" });
    const host = el("div", { class: "mc-results", "aria-live": "polite" });
    const form = el("div", { class: "mc-form" }, [
      el("div", { class: "mc-grid" }, [
        field("Format", select("format", [["modrinth", "Modrinth (.mrpack)"], ["curseforge", "CurseForge (.zip)"]], "modrinth")),
        field("Pack name", input("name", "")),
        field("Pack version", input("version", "", { placeholder: "1.0.0" })),
        field("Minecraft version", input("mc_version", "", { placeholder: "1.20.1" })),
        field("Loader", select("loader", LOADERS.concat(["vanilla"]).map((l) => [l, l]), "fabric")),
        field("Loader version", input("loader_version", "", { placeholder: "empty = the latest" })),
        field("Least stable", select("channel", CHANNELS.map((c) => [c, c]), "release")),
      ]),
      field("Projects", el("textarea", { name: "entries", class: "mc-input mc-textarea", rows: "6", spellcheck: "false",
        placeholder: "One per line:\nsodium\nlithium\niris | <file id to pin>" }), "Slug, id or exact name. Add | file id to pin one file."),
      el("div", { class: "mc-grid" }, [
        field("Author (CurseForge)", input("author", "")),
        field("Summary (Modrinth)", input("summary", "")),
        field("File name", input("filename", "", { placeholder: "empty = name-version" })),
      ]),
      field("Extra files folder", el("div", { class: "mc-row" }, [input("src", "", { placeholder: "optional: packed into overrides" }),
        el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Browse\u2026", onclick: () => pickFolder(root, "src") })])),
      destRow(root, "Where the pack file is written."),
      check("include_dependencies", "Add required dependencies", true),
      check("overwrite", "Replace a file of the same name", false),
    ]);
    const make = async (dry) => {
      const args = packArgs(readForm(form), dry);
      const bad = problem("pack", args);
      if (bad) { host.textContent = ""; host.appendChild(notice("warn", bad)); return; }
      host.textContent = ""; host.appendChild(el("div", { class: "mc-muted", text: dry ? "Checking every project\u2026" : "Making the pack\u2026" }));
      let r;
      if (dry) r = await runTool("make_modpack_file", args, {});
      else {
        const pre = await runTool("make_modpack_file", packArgs(readForm(form), true), {});
        if (!pre || pre.ok === false) { showResult(host, pre, { root, canUseLibrary: true }); return; }
        r = await runTool("make_modpack_file", args, { sure: true, title: "Write this pack file?", confirmLabel: "Write",
          summary: text(pre.message || "The pack file will be written to the folder shown below.", 700), answeredFolder: isOutsideLibrary(args.dest) });
      }
      showResult(host, r, { root, canUseLibrary: true });
    };
    form.appendChild(el("div", { class: "mc-actions" }, [
      el("button", { type: "button", class: "btn btn--ghost", text: "Check only (dry run)", onclick: (e) => guarded(e.currentTarget, () => make(true)) }),
      el("button", { type: "button", class: "btn btn--primary", text: "Make pack", onclick: (e) => guarded(e.currentTarget, () => make(false)) }),
    ]));
    root.appendChild(form); root.appendChild(host);
    return root;
  }

  // ---- shell

  function openProject(hit) {
    select_tab("project");
    const t = S.tabs.project;
    if (t && t._fill) t._fill(hit);
  }

  function select_tab(id) {
    S.tab = id;
    if (!S.panel) return;
    S.panel.querySelectorAll("[role=tab]").forEach((b) => {
      const on = b.getAttribute("data-tab") === id;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle("is-active", on);
    });
    TAB_LIST.forEach((t) => { if (S.tabs[t[0]]) S.tabs[t[0]].hidden = t[0] !== id; });
  }

  function open() {
    if (typeof document === "undefined") return;
    if (S.overlay) { select_tab(S.tab); return; }
    S.prevFocus = document.activeElement;
    S.tabs = { search: buildSearch(), project: buildProject(), optifine: buildOptifine(), pack: buildPack() };
    const bar = el("div", { class: "mc-tabs", role: "tablist", "aria-label": "Minecraft" }, TAB_LIST.map((t) => el("button", {
      type: "button", role: "tab", class: "mc-tabbtn", "data-tab": t[0], text: t[1], onclick: () => select_tab(t[0]),
    })));
    const body = el("div", { class: "mc-body" }, TAB_LIST.map((t) => S.tabs[t[0]]));
    S.panel = el("div", { class: "menu-panel menu-panel--minecraft", role: "dialog", "aria-modal": "true", "aria-label": "Minecraft" }, [
      el("div", { class: "panel__corner panel__corner--tl" }), el("div", { class: "panel__corner panel__corner--br" }),
      el("div", { class: "menu-panel__head" }, [
        el("div", { class: "menu-panel__titles" }, [el("div", { class: "menu-panel__title", text: "MINECRAFT" })]),
        el("button", { class: "modal__close", type: "button", "aria-label": "Close", text: "\u00d7", onclick: close }),
      ]),
      bar,
      el("div", { class: "mc-note", text: "Looks things up and queues files. Nothing is installed or run." }),
      body,
    ]);
    S.overlay = el("div", { class: "menu-overlay mc-overlay" }, [S.panel]);
    S.overlay.addEventListener("mousedown", (e) => { if (e.target === S.overlay) close(); });
    S.onKey = (e) => {
      if (e.key !== "Escape" || e.defaultPrevented || !S.overlay) return;
      if (document.querySelector("#jui-layer .jui-modal")) return;               // a confirmation owns Escape
      if (document.querySelector(".pp-overlay, .path-picker")) return;           // so does the folder picker
      e.preventDefault(); close();
    };
    document.addEventListener("keydown", S.onKey, true);
    document.body.appendChild(S.overlay);
    select_tab(S.tab);
    const first = S.panel.querySelector(".mc-tab:not([hidden]) input");
    if (first && first.focus) first.focus();
  }

  function close() {
    if (!S.overlay) return;
    document.removeEventListener("keydown", S.onKey, true);
    if (S.overlay.parentNode) S.overlay.parentNode.removeChild(S.overlay);
    const back = S.prevFocus;
    S.overlay = null; S.panel = null; S.onKey = null; S.tabs = {};
    if (back && back.focus && document.contains(back)) { try { back.focus(); } catch (_) { /* gone */ } }
  }

  global.JarvisMinecraft = {
    open, close,
    _pure: { text, count, size, day, safeUrl, searchArgs, projectArgs, downloadArgs, optifineListArgs, optifineArgs, parseEntries,
      packArgs, problem, classify, wantsNoKeyHint, isOutsideLibrary, TOOLS, LIBRARY_FOLDER },
  };
})(typeof window !== "undefined" ? window : globalThis);
