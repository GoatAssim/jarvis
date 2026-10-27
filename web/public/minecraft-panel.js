/* ============================================================================
 * minecraft-panel.js — the "Minecraft" Menu panel (wave S3).
 *
 * Search Modrinth / CurseForge, list a project's compatible files, check a folder of mods
 * for newer files, queue a download to a folder, fetch OptiFine, make a modpack file (also
 * from entries named on the OTHER platform). It is a thin face on the SEVEN Minecraft tools
 * (search_mc_mods, get_mc_project, list_optifine_versions, download_mc_file,
 * download_optifine, make_modpack_file, check_mc_updates) and nothing else:
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
 *   - Questions a tool asks WHILE it runs (the hub's "Save outside the downloads library?",
 *     the pack's "Download mod files to match them on CurseForge?") have nobody to answer them
 *     in /api/tools/run. The panel sends confirm_ui: true, which the server turns into a "y"
 *     for every such question, ONLY for a real write (never a dry run), ONLY after its own
 *     dialog showed what the answer covers: the dry-run message, the exact arguments, the
 *     folder and, for a pack that downloads files to match them, the file count and total
 *     size. If the dry run does not give those, the panel sends no yes and says so
 *     (confirmPlan / mayPassYes below; tests/verify_minecraft_panel.js pins both).
 *
 * Public:  window.JarvisMinecraft.open() / .close()
 * Tested:  window.JarvisMinecraft._pure (tests/verify_minecraft_panel.js).
 * ========================================================================= */
(function (global) {
  "use strict";

  const TOOLS = ["search_mc_mods", "get_mc_project", "list_optifine_versions",
    "download_mc_file", "download_optifine", "make_modpack_file", "check_mc_updates"];
  // The tools that write something. Only these can ever be sent a confirm_ui, and never on a dry run.
  const WRITERS = ["download_mc_file", "download_optifine", "make_modpack_file"];
  const PLATFORMS = ["modrinth", "curseforge"];
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
    // Cross-platform (S1): the entries are NAMED on the other platform. Sent only when it really differs
    // from the pack's own, so an ordinary pack is byte-for-byte what it was.
    const direction = crossDirection(args.format, f.entries_from);
    if (direction) args.entries_from = oneOf(f.entries_from, PLATFORMS, "");
    // download_to_match lets the tool read every Modrinth file once to fingerprint it. Never on a dry run
    // (a dry run must not download), and only for the one direction that needs it.
    if (!dry && direction === "modrinth_to_curseforge" && f.download_to_match === true) args.download_to_match = true;
    if (dry) args.dry_run = true;
    return args;
  }

  // The first thing wrong with a form, in plain words, before any call is made.
  function problem(kind, args) {
    if (kind === "search") return args.query ? "" : "Type what to search for.";
    if (kind === "project" || kind === "download") return args.project ? "" : "Say which project: its slug or id.";
    if (kind === "optifine-download") return args.mc_version ? "" : "Say the Minecraft version, like 1.20.1.";
    if (kind === "updates") return "";                       // every field is optional: the tool has its own folder default
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
      if (result.reason === "cancelled") return { kind: "cancelled", message: err };          // the owner said no in the panel's own dialog
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

  // ------------------------------------------------------------------ Updates tab (M2)

  function updatesArgs(f) {
    const args = { channel: oneOf(f.channel, CHANNELS, "release") };
    put(args, "folder", f.folder);                              // empty = the tool's own default (last folder, else the default one)
    put(args, "mc_version", f.mc_version);
    const loader = oneOf(f.loader, LOADERS, "");
    if (loader) args.loader = loader;
    return args;
  }

  // What one "Queue download" button of the Updates list stands for: the fields of the Project tab's
  // download form, taken from the tool's own get_with (never rebuilt from a name). null = nothing to queue.
  function updateDownloadFields(update, f) {
    const g = update && typeof update === "object" ? update.get_with : null;
    if (!g || typeof g !== "object" || g.tool !== "download_mc_file") return null;
    const project = trimmed(g.project);
    const file = trimmed(g.file);
    if (!project || !file) return null;
    const out = { project, file, source: oneOf(g.source, PLATFORMS, "modrinth"), kind: "mod",
      channel: oneOf((f || {}).channel, CHANNELS, "release") };
    put(out, "mc_version", (f || {}).mc_version);
    const loader = oneOf((f || {}).loader, LOADERS, "");
    if (loader) out.loader = loader;
    put(out, "dest", (f || {}).dest);
    return out;
  }

  const WHY_WORDS = {
    not_on_modrinth: "Modrinth does not know this file. It may come from CurseForge or somewhere else, and Jarvis does not guess from a name or a size.",
    needs_mc_version: "It works on several Minecraft versions. Say which one you play.",
    needs_loader: "It works with several loaders. Say which one you use.",
    no_minecraft_version: "Modrinth lists no Minecraft version for it.",
    no_loader: "Modrinth lists no loader for it.",
    other_minecraft_version: "It is built for another Minecraft version than the one you chose, so no update was looked up.",
    other_loader: "It is built for another loader than the one you chose, so no update was looked up.",
    offline: "Modrinth could not be reached.",
    rate_limited: "Modrinth asked Jarvis to slow down.",
    vendor_changed: "Modrinth's answer was not what Jarvis expected.",
  };

  // A not-checked code in plain words. A code the panel does not know (a newer tool may add some) shows the
  // tool's own sentence, else the code itself: never a guess.
  function whyWords(why, detail) {
    const code = trimmed(why);
    if (code && Object.prototype.hasOwnProperty.call(WHY_WORDS, code)) return WHY_WORDS[code];
    return text(detail, 240) || (code ? "Not checked (" + text(code, 40) + ")." : "Not checked.");
  }

  const FOLDER_FROM = { given: "the folder you named", last_checked: "the last folder checked", default: "your default download folder" };

  function folderFromWords(how) {
    const k = trimmed(how);
    return Object.prototype.hasOwnProperty.call(FOLDER_FROM, k) ? FOLDER_FROM[k] : text(k, 60);
  }

  // Which inputs a needs_clarification answer is asking for. An ask the panel cannot read still gets both.
  function clarifyNeeds(result) {
    const out = { mc_version: false, loader: false };
    if (!result || typeof result !== "object" || result.needs_clarification !== true) return out;
    (Array.isArray(result.not_checked) ? result.not_checked : []).forEach((u) => {
      if (u && u.why === "needs_mc_version") out.mc_version = true;
      if (u && u.why === "needs_loader") out.loader = true;
    });
    if (!out.mc_version && !out.loader) { out.mc_version = true; out.loader = true; }
    return out;
  }

  // ------------------------------------------------------------------ cross-platform pack (M2)

  // "" = an ordinary one-platform pack. Otherwise which way the entries have to be matched.
  function crossDirection(format, from) {
    const target = oneOf(format, PLATFORMS, "modrinth");
    const origin = oneOf(from, PLATFORMS, "");
    if (!origin || origin === target) return "";
    return origin === "curseforge" ? "curseforge_to_modrinth" : "modrinth_to_curseforge";
  }

  const SKIP_WORDS = {
    not_found: "no identical file on the pack's platform",
    ambiguous: "more than one identical file (Jarvis does not pick)",
    not_distributable: "the author does not allow third-party downloads",
    hash_mismatch: "the file the pack would list is not the one that was matched",
    no_compatible_file: "no file for this Minecraft version and loader",
    no_hash: "no hash to look it up with",
    not_main_file: "the identical file is not its version's main file",
    bad_download_url: "no download link to fingerprint it from",
    too_big: "too big to fingerprint",
    verify_failed: "the downloaded file did not match its published hashes",
    vendor_changed: "the site's answer was not what Jarvis expected",
  };

  function skipWords(code) {
    const k = trimmed(code);
    return Object.prototype.hasOwnProperty.call(SKIP_WORDS, k) ? SKIP_WORDS[k] : (k ? text(k, 40) : "skipped");
  }

  // The counts a cross-platform result is summed up by. `present` is false for an ordinary pack.
  function crossSummary(result) {
    const r = result && typeof result === "object" ? result : {};
    const cm = r.crossmatch && typeof r.crossmatch === "object" ? r.crossmatch : null;
    const listed = (Array.isArray(r.skipped) ? r.skipped : []).filter((x) => x && typeof x === "object");
    const nth = (code) => listed.filter((x) => x.reason === code).length;
    const whole = r.counts && Number.isInteger(r.counts.skipped) ? r.counts.skipped : 0;
    const more = Number.isInteger(r.skipped_more) ? r.skipped_more : 0;
    const num = (v) => (typeof v === "number" && isFinite(v) && v >= 0 ? v : 0);
    return {
      present: !!cm,
      from: cm ? trimmed(cm.from) : "", to: cm ? trimmed(cm.to) : "", method: cm ? trimmed(cm.method) : "",
      entries: cm ? num(cm.entries) : 0, matched: cm ? num(cm.matched) : 0,
      downloaded_files: cm ? num(cm.downloaded_files) : 0, downloaded_bytes: cm ? num(cm.downloaded_bytes) : 0,
      skipped: Math.max(listed.length + more, whole),
      ambiguous: nth("ambiguous"), not_distributable: nth("not_distributable"), hash_mismatch: nth("hash_mismatch"),
      more,
    };
  }

  // The dry run of a Modrinth -> CurseForge pack that has to read files first answers with a question that
  // names how many and how big. `exposed` is true only when BOTH are really there: without them the panel
  // has nothing to show in its dialog and sends no yes.
  function downloadNeeds(result) {
    const none = { needed: false, count: 0, bytes: 0, files: [], exposed: false };
    if (!result || typeof result !== "object" || result.needs_download !== true) return none;
    const count = Number.isInteger(result.download_count) && result.download_count > 0 ? result.download_count : 0;
    const bytes = typeof result.download_bytes === "number" && isFinite(result.download_bytes) && result.download_bytes > 0 ? result.download_bytes : 0;
    const files = (Array.isArray(result.download_files) ? result.download_files : []).filter((x) => x && typeof x === "object");
    const sizesKnown = files.every((x) => typeof x.size === "number" && isFinite(x.size) && x.size >= 0);
    return { needed: true, count, bytes, files: files.slice(0, 5), exposed: count > 0 && bytes > 0 && sizesKnown };
  }

  // ------------------------------------------------------------------ what a dialog must show before a yes is passed on

  // The exact arguments, one per line (a pack's projects one per line), clipped LOUDLY: a cut ends in an ellipsis.
  function argsText(args) {
    const lines = [];
    const a = args && typeof args === "object" ? args : {};
    Object.keys(a).forEach((k) => {
      const v = a[k];
      if (k === "entries" && Array.isArray(v)) {
        lines.push("entries (" + v.length + "):");
        v.forEach((e) => lines.push("  " + JSON.stringify(e)));
      } else {
        lines.push(k + ": " + (typeof v === "string" ? v : JSON.stringify(v)));
      }
    });
    return text(lines.join("\n"), 6000);
  }

  // The panel may send confirm_ui only for a write the owner just said yes to in its own dialog, and never on
  // a dry run. `shown` = the dialog was shown and answered yes in THIS call; `wanted` = the caller asked for it.
  function mayPassYes(name, args, shown, wanted) {
    return wanted === true && shown === true && WRITERS.indexOf(name) >= 0 && !(args && args.dry_run === true);
  }

  /* What the one dialog says, and whether a yes may be passed on at all.
     kind: "download" | "optifine" | "pack" (a pack that downloads nothing) | "pack-download" (a pack that reads files).
     `dry` is the dry run's answer, `need` is downloadNeeds() of it for "pack-download".
     -> { ok, folder, summary, why }.  ok false = the dry run did not give something a later question could ask
     about, so the panel passes nothing on and `why` says what to do instead. */
  function confirmPlan(kind, dry, args, need) {
    const d = dry && typeof dry === "object" ? dry : {};
    const a = args && typeof args === "object" ? args : {};
    const lines = [];
    // The cross-platform question ends with a line written for the model ("Ask the owner; ..."): not for the dialog.
    const message = text(d.message || "", 900).replace(/\s*Ask the owner;[\s\S]*$/, "");
    if (message) lines.push(message);

    // The folder: the dry run names it (its `dest`). A pack that has to download files is answered by a
    // question that does not name it, so there the owner must have typed it.
    let folder = trimmed(d.dest);
    if (!folder && kind === "pack-download") folder = trimmed(a.dest);
    if (!folder) {
      return { ok: false, folder: "", summary: "", why: kind === "pack-download"
        ? "Choose the folder to write the pack to (Save to) first. A pack that has to download files is confirmed only once, and that confirmation has to show the folder."
        : "Jarvis's check did not say which folder it would write to, so this panel will not pass your yes on. Ask for it in chat instead: Jarvis asks you itself." };
    }
    lines.push("Folder: " + folder);

    if (kind === "download" && a.with_dependencies) {
      const deps = Array.isArray(d.dependencies_would_queue) ? d.dependencies_would_queue.filter((x) => x && typeof x === "object") : [];
      const names = deps.slice(0, 8).map((x) => text(x.name_of_project || x.name || x.file_name || x.project_id, 60)).join(", ");
      lines.push(deps.length ? "Also queued, in the same folder: " + deps.length + " required dependenc" + (deps.length === 1 ? "y" : "ies") + " (" + names + ")."
        : "No required dependencies to queue.");
    }
    if (kind === "pack" || kind === "pack-download") {
      if (d.path) lines.push("File: " + text(d.path, 260));
      if (a.src) lines.push("Extra files folder: " + text(a.src, 260));
      const s = crossSummary(d);
      if (s.present) {
        lines.push("Matched on " + text(s.to, 20) + " by " + text(s.method, 20) + ": " + s.matched + " of " + s.entries + " entries; "
          + s.skipped + " skipped" + (s.method === "sha1" ? ". Nothing is downloaded." : "."));
      }
    }
    if (kind === "pack-download") {
      const n = need || downloadNeeds(d);
      if (!n.exposed) {
        return { ok: false, folder, summary: "", why: "Jarvis's check did not say how many files it would download and how big they are, so this panel will not pass your yes on to the download question. Ask for this pack in chat instead: Jarvis asks you itself." };
      }
      const names = n.files.map((x) => text(x.project || x.file_name, 40)).filter(Boolean).join(", ");
      lines.push("Downloads: " + n.count + " mod file" + (n.count === 1 ? "" : "s") + ", " + size(n.bytes) + " in all, from cdn.modrinth.com. Each is read in memory to work out its CurseForge fingerprint and is not saved."
        + (names ? " Starting with: " + names + "." : ""));
    }
    return { ok: true, folder, summary: lines.join("\n"), why: "" };
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
     should read; `sure` forces the question for a real write even if the server did not ask.
     `passYes` asks for confirm_ui: the server then answers "y" to the questions the tool asks while it
     runs. It is sent only when mayPassYes() agrees: the tool is a writer, the call is not a dry run and
     the owner said yes to THIS call's dialog (whose summary the caller built with confirmPlan). */
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
    let shown = false;
    if (gated && !o.skipGate) {
      const ui = UI();
      const yes = ui && ui.confirm ? await ui.confirm({
        title: o.title || ("Run " + name + "?"), level: "warn", focusCancel: true, confirmLabel: o.confirmLabel || "Run",
        body: text(o.summary || "", 3000) + (o.summary ? "\n\n" : "") + "Exact arguments:\n" + argsText(args),
      }) : false;
      if (!yes) return { ok: false, cancelled: true, reason: "cancelled", error: "Cancelled: nothing was run." };
      shown = true;
    }
    try {
      const body = { name, arguments: args };
      if (mayPassYes(name, args, shown, o.passYes)) body.confirm_ui = true;
      const data = await post("/api/tools/run", body);
      return data.result && typeof data.result === "object" ? data.result : { ok: false, error: "Jarvis gave an answer the panel can't read." };
    } catch (e) {
      const msg = text(e.message, 200);
      return { ok: false, reason: /timed out/i.test(msg) ? "timeout" : "panel", error: msg };
    }
  }

  // ------------------------------------------------------------------ the write flows, free of the DOM
  //
  // `call(name, args, opts)` is runTool in the panel and a recording stub in tests/verify_minecraft_panel.js, so the
  // order of the steps (dry run first, then ONE dialog, then the real call) and what each step may carry
  // (confirm_ui, download_to_match) are tested without a browser.
  //
  // -> { stage, result?, message?, need? }
  //    "invalid"  a field is missing (message says which); no call was made
  //    "check"    the dry run answered something that is not a plan (a failure, a question, a partial result)
  //    "blocked"  the dry run did not give what the dialog must show (message says what to do instead)
  //    "needs_tick" a pack has to read files and the owner did not allow it
  //    "checked"  (a dry run only) the dry run's answer
  //    "done"     the real call was made (or the owner said no in the dialog: result.reason is "cancelled")

  async function downloadSteps(f, call) {
    const args = downloadArgs(f, false);
    const bad = problem("download", args);
    if (bad) return { stage: "invalid", message: bad };
    const check = await call("download_mc_file", downloadArgs(f, true), {});
    if (!check || check.ok === false || check.needs_clarification === true) return { stage: "check", result: check };
    const plan = confirmPlan("download", check, args);
    if (!plan.ok) return { stage: "blocked", result: check, message: plan.why };
    const result = await call("download_mc_file", args, { sure: true, title: "Queue this download?", confirmLabel: "Queue",
      summary: plan.summary, passYes: isOutsideLibrary(args.dest) });
    return { stage: "done", result };
  }

  async function optifineSteps(f, call) {
    const args = optifineArgs(f, false);
    const bad = problem("optifine-download", args);
    if (bad) return { stage: "invalid", message: bad };
    const check = await call("download_optifine", optifineArgs(f, true), {});
    if (!check || check.ok === false || check.needs_clarification === true) return { stage: "check", result: check };
    const plan = confirmPlan("optifine", check, args);
    if (!plan.ok) return { stage: "blocked", result: check, message: plan.why };
    // OptiFine is always written to a full path, so the tool always asks its folder question.
    const result = await call("download_optifine", args, { sure: true, title: "Download OptiFine?", confirmLabel: "Download",
      summary: plan.summary, passYes: true });
    return { stage: "done", result };
  }

  // The dry run of a pack. It never carries download_to_match, so it never downloads a file: for a pack that has to
  // read files it answers with the question that says how many and how big (see downloadNeeds).
  async function packCheck(f, call) {
    const args = packArgs(f, true);
    const bad = problem("pack", args);
    if (bad) return { stage: "invalid", message: bad };
    const result = await call("make_modpack_file", args, {});
    return { stage: "checked", result, need: downloadNeeds(result) };
  }

  async function packWrite(f, call) {
    const first = await packCheck(f, call);
    if (first.stage !== "checked") return first;
    const check = first.result;
    const need = first.need;
    if (!check || check.ok === false || check.partial === true) return { stage: "check", result: check, need };
    if (check.needs_clarification === true && !need.needed) return { stage: "check", result: check, need };
    if (need.needed) {
      if (!(crossDirection(f.format, f.entries_from) === "modrinth_to_curseforge" && f.download_to_match === true)) {
        return { stage: "needs_tick", result: check, need, message: "Tick \u201cDownload each file once\u2026\u201d to allow it, then press Make pack again. Nothing was downloaded or written." };
      }
      const args = packArgs(Object.assign({}, f, { download_to_match: true }), false);
      const plan = confirmPlan("pack-download", check, args, need);
      if (!plan.ok) return { stage: "blocked", result: check, need, message: plan.why };
      const result = await call("make_modpack_file", args, { sure: true, title: "Download files and write this pack?",
        confirmLabel: "Download and write", summary: plan.summary, passYes: true });
      return { stage: "done", result, need };
    }
    // Nothing has to be read (an ordinary pack, CurseForge to Modrinth, or every fingerprint is already remembered):
    // download_to_match is NOT sent, so a question about downloads can never be asked of a yes that did not cover it.
    const args = packArgs(Object.assign({}, f, { download_to_match: false }), false);
    const plan = confirmPlan("pack", check, args);
    if (!plan.ok) return { stage: "blocked", result: check, need, message: plan.why };
    const result = await call("make_modpack_file", args, { sure: true, title: "Write this pack file?", confirmLabel: "Write",
      summary: plan.summary, passYes: true });
    return { stage: "done", result, need };
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
  function genericTables(result, skip) {
    const out = [];
    Object.keys(result || {}).forEach((key) => {
      if (Array.isArray(skip) && skip.indexOf(key) >= 0) return;          // a key the caller draws itself
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

  // ------------------------------------------------------------------ Updates and cross-platform packs: drawing

  function plainTable(headers, rows) {
    return el("div", { class: "mc-tablewrap" }, [el("table", { class: "mc-table" }, [
      el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]),
      el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((c) => el("td", {}, [c]))))),
    ])]);
  }

  function cell(v, max) { return typeof v === "object" && v !== null && v.nodeType ? v : document.createTextNode(text(v, max || 120)); }

  function projectCell(p) {
    const proj = p && typeof p === "object" ? p : { name: p };
    const name = text(proj.name || proj.slug || proj.id, 80);
    return proj.page_url ? link(proj.page_url, name) : cell(name);
  }

  // `ctx`: { form, again(overrides), queue(update) }
  function renderUpdates(res, host, ctx) {
    const shown = { folder: res.folder, folder_chosen: folderFromWords(res.folder_from), minecraft_version: res.mc_version,
      loader: res.loader, channel: res.channel, files_checked: res.checked, newer_files: res.updates_available };
    const facts = factsOf(shown, Object.keys(shown));
    if (facts) host.appendChild(facts);
    if (res.partial === true) host.appendChild(notice("warn", "The check stopped early, so this list is partial. Check again to carry on: files already read are remembered."));
    if (Array.isArray(res.unreadable) && res.unreadable.length) {
      host.appendChild(notice("warn", "Could not be read (left out): " + res.unreadable.slice(0, 10).map((n) => text(n, 60)).join(", ")));
    }
    if (res.ignored && typeof res.ignored === "object") {
      const bits = Object.keys(res.ignored).filter((k) => typeof res.ignored[k] === "number" && res.ignored[k] > 0)
        .map((k) => res.ignored[k] + " " + k.replace(/_/g, " "));
      if (bits.length) host.appendChild(el("div", { class: "mc-muted mc-small", text: "Counted but never opened: " + bits.join(", ") + "." }));
    }
    const updates = Array.isArray(res.updates) ? res.updates.filter((u) => u && typeof u === "object") : [];
    if (updates.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Newer files (" + updates.length + ")" }));
      host.appendChild(plainTable(["File", "Project", "Now", "Newer", ""], updates.map((u) => {
        const inst = u.installed && typeof u.installed === "object" ? u.installed : {};
        const late = u.latest && typeof u.latest === "object" ? u.latest : {};
        const fields = updateDownloadFields(u, {});
        return [
          el("span", { class: "mc-mono", text: text(u.file, 90) }),
          projectCell(u.project),
          cell([inst.version, inst.channel, day(inst.date)].filter(Boolean).join(" \u00b7 ")),
          el("span", {}, [cell([late.version, late.channel, day(late.date), size(late.size)].filter(Boolean).join(" \u00b7 ")),
            late.via ? el("span", { class: "mc-chip mc-chip--warn", text: "via " + text(late.via, 10) }) : null,
            late.file_name ? el("div", { class: "mc-mono mc-muted", text: text(late.file_name, 90) }) : null]),
          fields ? el("button", { type: "button", class: "btn btn--sm btn--outline", text: "Queue download\u2026",
            onclick: (e) => guarded(e.currentTarget, () => ctx.queue(u)) })
            : el("span", { class: "mc-chip mc-chip--warn", text: "no file given" }),
        ];
      })));
    } else if (res.ok !== false && !res.needs_clarification) {
      host.appendChild(el("div", { class: "mc-muted", text: "No newer files found for the files that could be checked." }));
    }
    const current = Array.isArray(res.up_to_date) ? res.up_to_date.filter((u) => u && typeof u === "object") : [];
    if (current.length) {
      host.appendChild(el("details", { class: "mc-raw" }, [el("summary", { text: "Up to date (" + current.length + ")" }),
        plainTable(["File", "Project", "Version"], current.map((u) => [el("span", { class: "mc-mono", text: text(u.file, 90) }), cell(u.project, 80), cell(u.version, 60)]))]));
    }
    const other = Array.isArray(res.other_version) ? res.other_version.filter((u) => u && typeof u === "object") : [];
    if (other.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Built for another version or loader (" + other.length + ")" }));
      host.appendChild(plainTable(["File", "Why"], other.map((u) => [el("span", { class: "mc-mono", text: text(u.file, 90) }),
        el("span", {}, [cell(whyWords(u.why, u.detail)), u.detail ? el("div", { class: "mc-muted mc-small", text: text(u.detail, 240) }) : null])])));
    }
    const unchecked = Array.isArray(res.not_checked) ? res.not_checked.filter((u) => u && typeof u === "object") : [];
    if (unchecked.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Not checked (" + unchecked.length + ")" }));
      host.appendChild(plainTable(["File", "Why"], unchecked.map((u) => [el("span", { class: "mc-mono", text: text(u.file, 90) }),
        el("span", {}, [cell(whyWords(u.why, u.detail)), u.detail && WHY_WORDS[trimmed(u.why)] ? el("div", { class: "mc-muted mc-small", text: text(u.detail, 240) }) : null])])));
    }
    if (res.curseforge_note) host.appendChild(el("div", { class: "mc-muted mc-small", text: text(res.curseforge_note, 400) }));
    if (res.needs_clarification === true) {
      const need = clarifyNeeds(res);
      const now = ctx.form ? readForm(ctx.form) : {};
      const mc = need.mc_version ? input("ask_mc_version", trimmed(now.mc_version), { placeholder: "1.20.1" }) : null;
      const ld = need.loader ? select("ask_loader", [["", "choose\u2026"]].concat(LOADERS.map((l) => [l, l])), oneOf(now.loader, LOADERS, "")) : null;
      host.appendChild(el("div", { class: "mc-card" }, [
        el("div", { class: "mc-subhead", text: "Jarvis needs to know" }),
        el("p", { class: "mc-card__summary", text: text(res.ask, 300) || "Say which Minecraft version and loader you use." }),
        el("div", { class: "mc-grid" }, [mc ? field("Minecraft version", mc) : null, ld ? field("Loader", ld) : null]),
        el("div", { class: "mc-actions" }, [el("button", { type: "button", class: "btn btn--sm btn--primary", text: "Check again", onclick: (e) => {
          const over = {};
          if (mc) over.mc_version = trimmed(mc.value);
          if (ld) over.loader = trimmed(ld.value);
          if ((mc && !over.mc_version) || (ld && !over.loader)) { say("Fill in the answer first.", "warn"); return; }
          guarded(e.currentTarget, () => ctx.again(over));
        } })]),
      ]));
    }
  }

  function countBox(label, n, tone) {
    return el("div", { class: "mc-count" + (tone && n > 0 ? " mc-count--" + tone : "") }, [
      el("span", { class: "mc-count__n", text: String(n) }), el("span", { class: "mc-count__k", text: label })]);
  }

  // `ctx.ticked`: the owner allowed downloads to match.
  function renderPackBody(res, host, ctx) {
    const need = downloadNeeds(res);
    if (need.needed) {
      const names = need.files.map((x) => text(x.project || x.file_name, 40)).filter(Boolean).join(", ");
      host.appendChild(notice("info", need.exposed
        ? "To match these on CurseForge Jarvis would download " + need.count + " mod file" + (need.count === 1 ? "" : "s") + " from Modrinth, " + size(need.bytes)
          + " in all, and read each in memory. Nothing is saved." + (names ? " Starting with: " + names + "." : "")
          + (ctx && ctx.ticked ? " Press Make pack to go on: you are asked once, with these numbers." : " Tick the box above to allow it.")
        : "To match these on CurseForge Jarvis would have to download files from Modrinth, but its check did not say how many or how big. Nothing was downloaded."));
    }
    const s = crossSummary(res);
    if (!s.present) return;
    host.appendChild(el("div", { class: "mc-subhead", text: "Matching " + text(s.from, 20) + " \u2192 " + text(s.to, 20) + " by " + text(s.method, 20) }));
    host.appendChild(el("div", { class: "mc-counts" }, [
      countBox("matched", s.matched, "ok"), countBox("skipped", s.skipped, "warn"), countBox("ambiguous", s.ambiguous, "warn"),
      countBox("not distributable", s.not_distributable, "warn"), countBox("hash mismatch", s.hash_mismatch, "warn")]));
    if (s.downloaded_files) {
      host.appendChild(el("div", { class: "mc-muted mc-small", text: s.downloaded_files + " mod file" + (s.downloaded_files === 1 ? "" : "s") + " (" + size(s.downloaded_bytes) + ") read to fingerprint, not saved." }));
    }
    const skipped = (Array.isArray(res.skipped) ? res.skipped : []).filter((x) => x && typeof x === "object");
    if (skipped.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Skipped (" + s.skipped + ")" }));
      host.appendChild(plainTable(["Entry", "Why", "Detail", "Page"], skipped.map((x) => {
        const cands = Array.isArray(x.candidates) ? x.candidates.filter((c) => c && typeof c === "object").slice(0, 5)
          .map((c) => text(c.file_name || c.file_id, 50)).join(", ") : "";
        return [cell(x.name || x.project, 80), cell(skipWords(x.reason)),
          cell([x.error, cands ? "Candidates: " + cands : ""].filter(Boolean).join(" ")), x.page_url ? link(x.page_url, "Open page") : cell("")];
      })));
      if (s.more) host.appendChild(el("div", { class: "mc-muted mc-small", text: s.more + " more are not listed." }));
    }
  }

  function showDownloadOutcome(host, out, root) {
    if (out.stage === "invalid") { host.textContent = ""; host.appendChild(notice("warn", out.message)); return; }
    showResult(host, out.result, { root, canUseLibrary: true });
    if (out.message) host.appendChild(notice("warn", out.message));
    if (out.stage === "done" && out.result && out.result.ok !== false && out.result.queued) say("Queued in Downloads.", "success");
  }

  async function runDownloadFlow(host, f, root) {
    host.textContent = "";
    host.appendChild(el("div", { class: "mc-muted", text: "Checking\u2026" }));
    showDownloadOutcome(host, await downloadSteps(f, runTool), root);
  }

  // ------------------------------------------------------------------ the panel

  const S = { overlay: null, panel: null, tab: "search", prevFocus: null, onKey: null, tabs: {} };

  const TAB_LIST = [["search", "Search"], ["project", "Project files"], ["updates", "Updates"], ["optifine", "OptiFine"], ["pack", "Make a pack"]];

  function pickFolder(root, name, kind) {
    const picker = global.JarvisPathPicker;
    if (!picker || !picker.open) { say("The folder picker is not available here: type the path.", "warn"); return; }
    const start = (root.querySelector('[name="' + name + '"]') || {}).value || "";
    Promise.resolve(picker.open({ kind: kind || "folder", start, title: "Choose a folder" })).then((picked) => {
      const p = picked && typeof picked === "object" ? (picked.path || picked.full || "") : picked;
      if (typeof p === "string" && p) setField(root, name, p);
    }).catch(() => say("The folder picker failed.", "warn"));
  }

  function destRow(root, hint, label) {
    return field(label || "Save to", el("div", { class: "mc-row" }, [
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
    if (kind === "not_confirmed") {
      // Reached only when a question the tool asked while it ran got no yes: the panel passes the owner's
      // yes on (confirm_ui) through the web server, so an old server that has not been restarted is the likely cause.
      const buttons = ctx && ctx.canUseLibrary ? [
        el("button", { type: "button", class: "btn btn--sm btn--outline", text: "Use the \u201c" + LIBRARY_FOLDER + "\u201d folder in the library",
          onclick: () => { setField(root, "dest", LIBRARY_FOLDER); say("Folder set. Press the button again.", "info"); } })] : null;
      bits.push(notice("warn", "Nothing was saved. Jarvis asked its own question about the folder and no yes reached it. "
        + "This panel passes your yes on through the Jarvis web server, so if you have just updated, restart the server and try again."
        + (buttons ? " A folder inside the Downloads library needs no question." : ""), buttons));
    }
    if (kind === "cancelled") {
      bits.push(notice("info", "Cancelled. Nothing was run, saved or queued."));
    }
    if (kind === "timeout") {
      bits.push(notice("warn", ctx && ctx.readOnly
        ? "This took longer than the panel waits. Nothing was changed. Try again: files already read are remembered, so it goes faster."
        : "This took longer than the panel waits. It may still have finished: check Downloads or the folder before trying again."));
    }
    return bits;
  }

  function showResult(host, result, opts) {
    const o = opts || {};
    host.textContent = "";
    const c = classify(result);
    const asks = !!result && typeof result === "object" && result.needs_clarification === true;
    const tone = c.kind === "ok" ? (result && (result.warning || asks) ? "warn" : "ok")
      : (c.kind === "not_confirmed" || c.kind === "timeout" ? "warn" : (c.kind === "cancelled" ? "info" : "error"));
    host.appendChild(notice(tone, c.message || (c.kind === "ok" ? "Done." : "That did not work.")));
    if (result && result.warning) host.appendChild(notice("warn", text(result.warning, 400)));
    if (result && result.untrusted_note) host.appendChild(el("div", { class: "mc-muted mc-small", text: text(result.untrusted_note, 300) }));
    (o.root ? followUps(result, o.root, o) : []).forEach((n) => host.appendChild(n));
    if (o.body) o.body(result, host);
    if (result && typeof result === "object") {
      if (!o.noFacts) {
        const f = factsOf(result, ["dest", "route", "source", "queued", "downloaded", "written", "loader_version", "loader_version_resolved_as"]);
        if (f) host.appendChild(f);
      }
      if (!o.noTables) genericTables(result, o.skipTables).forEach((t) => host.appendChild(t));
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
      if (!dry) { await runDownloadFlow(queueHost, f, root); return; }
      const args = downloadArgs(f, true);
      const bad = problem("download", args);
      if (bad) { queueHost.textContent = ""; queueHost.appendChild(notice("warn", bad)); return; }
      queueHost.textContent = ""; queueHost.appendChild(el("div", { class: "mc-muted", text: "Checking\u2026" }));
      showResult(queueHost, await runTool("download_mc_file", args, {}), { root, canUseLibrary: true });
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

  // ---- tab: updates

  function buildUpdates() {
    const root = el("div", { class: "mc-tab" });
    const results = el("div", { class: "mc-results", "aria-live": "polite" });
    const queueHost = el("div", { class: "mc-results", "aria-live": "polite" });
    const form = el("div", { class: "mc-form" }, [
      field("Folder to check", el("div", { class: "mc-row" }, [
        input("folder", "", { placeholder: "empty = the last folder checked, else your default download folder" }),
        el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Browse\u2026", onclick: () => pickFolder(root, "folder") })]),
        "Only this folder is read, not its sub-folders. Only file hashes are sent to Modrinth. Nothing in the folder is changed."),
      el("div", { class: "mc-grid" }, [
        field("Minecraft version", input("mc_version", "", { placeholder: "1.20.1 (optional)" })),
        field("Loader", select("loader", [["", "any"]].concat(LOADERS.map((l) => [l, l])), "")),
        field("Least stable", select("channel", CHANNELS.map((c) => [c, c]), "release")),
      ]),
      destRow(root, "Used only by Queue download. A newer file is queued as a new download: it never replaces or removes the old one.", "Save newer files to"),
    ]);
    const run = async (overrides) => {
      Object.keys(overrides || {}).forEach((k) => setField(form, k, overrides[k]));
      const args = updatesArgs(readForm(form));
      queueHost.textContent = "";
      results.textContent = "";
      results.appendChild(el("div", { class: "mc-muted", text: "Reading the folder and asking Modrinth\u2026" }));
      const r = await runTool("check_mc_updates", args, {});
      // Every key of the answer the panel does not draw stays visible in the Raw answer fold.
      showResult(results, r, { root, readOnly: true, noFacts: true, noTables: true, body: (res, host) => {
        if (res && res.ok !== false) renderUpdates(res, host, { form, again: run, queue: (u) => {
          const f = updateDownloadFields(u, readForm(form));
          if (!f) { say("Jarvis gave no file to queue for this one.", "warn"); return Promise.resolve(); }
          return runDownloadFlow(queueHost, f, root);
        } });
      } });
    };
    form.appendChild(el("div", { class: "mc-actions" }, [
      el("button", { type: "button", class: "btn btn--primary", text: "Check for newer files", onclick: (e) => guarded(e.currentTarget, () => run()) })]));
    root.appendChild(form); root.appendChild(results); root.appendChild(queueHost);
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
      const f = readForm(form);
      const args = optifineArgs(f, true);
      const bad = problem("optifine-download", args);
      if (bad) { dlHost.textContent = ""; dlHost.appendChild(notice("warn", bad)); return; }
      dlHost.textContent = ""; dlHost.appendChild(el("div", { class: "mc-muted", text: dry ? "Checking\u2026" : "Downloading\u2026" }));
      if (dry) { showResult(dlHost, await runTool("download_optifine", args, {}), { root }); return; }
      const out = await optifineSteps(f, runTool);
      showResult(dlHost, out.result, { root });                    // no library fallback: OptiFine is always written to a full path
      if (out.message) dlHost.appendChild(notice("warn", out.message));
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
    const crossCf = el("div", { class: "mc-explain", hidden: true }, [
      el("span", { text: "Each project is looked up on CurseForge and matched to the identical file on Modrinth by its SHA-1 hash. Nothing is downloaded. "
        + "A project with no identical file is skipped and listed, never swapped for a lookalike. Needs your CurseForge key." })]);
    const matchBox = check("download_to_match", "Download each file once, in memory, to fingerprint it", false);
    const crossMr = el("div", { class: "mc-explain", hidden: true }, [
      el("span", { text: "CurseForge identifies a file by a fingerprint of its bytes, so Jarvis has to read each Modrinth file to find the same file there. "
        + "Ticking this lets it download each mod file once, in memory; nothing is saved, and fingerprints it already knows are not downloaded again. "
        + "Jarvis shows how many files and how much before anything is downloaded, and you confirm once. Needs your CurseForge key." }),
      matchBox]);
    const form = el("div", { class: "mc-form" }, [
      el("div", { class: "mc-grid" }, [
        field("Build the pack for", select("format", [["modrinth", "Modrinth (.mrpack)"], ["curseforge", "CurseForge (.zip)"]], "modrinth")),
        field("The projects below are named on", select("entries_from", [["", "the same platform"], ["modrinth", "Modrinth"], ["curseforge", "CurseForge"]], ""),
          "Pick the other platform to build this pack from a list named there."),
        field("Pack name", input("name", "")),
        field("Pack version", input("version", "", { placeholder: "1.0.0" })),
        field("Minecraft version", input("mc_version", "", { placeholder: "1.20.1" })),
        field("Loader", select("loader", LOADERS.concat(["vanilla"]).map((l) => [l, l]), "fabric")),
        field("Loader version", input("loader_version", "", { placeholder: "empty = the latest" })),
        field("Least stable", select("channel", CHANNELS.map((c) => [c, c]), "release")),
      ]),
      crossCf, crossMr,
      field("Projects", el("textarea", { name: "entries", class: "mc-input mc-textarea", rows: "6", spellcheck: "false",
        placeholder: "One per line:\nsodium\nlithium\niris | <file id to pin>" }), "Slug, id or exact name on the platform they are named on. Add | file id to pin one file."),
      el("div", { class: "mc-grid" }, [
        field("Author (CurseForge)", input("author", "")),
        field("Summary (Modrinth)", input("summary", "")),
        field("File name", input("filename", "", { placeholder: "empty = name-version" })),
      ]),
      field("Extra files folder", el("div", { class: "mc-row" }, [input("src", "", { placeholder: "optional: packed into overrides" }),
        el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Browse\u2026", onclick: () => pickFolder(root, "src") })])),
      destRow(root, "Where the pack file is written. A pack that has to download files needs this filled in."),
      check("include_dependencies", "Add required dependencies", true),
      check("overwrite", "Replace a file of the same name", false),
    ]);
    const syncCross = () => {
      const f = readForm(form);
      const dir = crossDirection(f.format, f.entries_from);
      crossCf.hidden = dir !== "curseforge_to_modrinth";
      crossMr.hidden = dir !== "modrinth_to_curseforge";
    };
    form.addEventListener("change", syncCross);
    const make = async (dry) => {
      const f = readForm(form);
      host.textContent = "";
      host.appendChild(el("div", { class: "mc-muted", text: dry ? "Checking every project\u2026" : "Checking, then making the pack\u2026" }));
      const out = dry ? await packCheck(f, runTool) : await packWrite(f, runTool);
      if (out.stage === "invalid") { host.textContent = ""; host.appendChild(notice("warn", out.message)); return; }
      const ticked = crossDirection(f.format, f.entries_from) === "modrinth_to_curseforge" && f.download_to_match === true;
      showResult(host, out.result, { root, skipTables: ["skipped"], body: (res, h) => renderPackBody(res, h, { ticked }) });
      if (out.message) host.appendChild(notice("warn", out.message));
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
    S.tabs = { search: buildSearch(), project: buildProject(), updates: buildUpdates(), optifine: buildOptifine(), pack: buildPack() };
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
      el("div", { class: "mc-note", text: "Looks things up, checks folders, queues downloads and writes pack files. It never opens, runs or changes a file you already have." }),
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
      packArgs, problem, classify, wantsNoKeyHint, isOutsideLibrary, TOOLS, WRITERS, LIBRARY_FOLDER,
      updatesArgs, updateDownloadFields, whyWords, folderFromWords, clarifyNeeds, crossDirection, skipWords, crossSummary,
      downloadNeeds, argsText, mayPassYes, confirmPlan },
    // The write flows and the one function that talks to the server, for tests/verify_minecraft_panel.js (a stub fetch and a stub JarvisUI).
    _flow: { runTool, downloadSteps, optifineSteps, packCheck, packWrite },
  };
})(typeof window !== "undefined" ? window : globalThis);
