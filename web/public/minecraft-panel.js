/* ============================================================================
 * minecraft-panel.js — the "Minecraft" Menu panel (waves S3, M2, M5).
 *
 * One flow instead of five forms:
 *   - "What I play" (Minecraft version, loader, preferred source) is set ONCE in a bar at the top and used by every
 *     tab. It lives in this script's memory for the session (nothing is stored in the browser, nothing is secret).
 *   - Find: search results are cards (source, summary, downloads, date, and whether a file fits what you play);
 *     "Show files" opens the project: a files table with the recommended file marked and why, and its dependencies.
 *   - The tray (client-side only) holds the projects or files you picked. From it you queue downloads (one dialog for
 *     the whole batch) or make a pack. The tray is just a list in memory: nothing happens until you press a button.
 *   - OptiFine and Make a pack are the same tools as before, now reading the shared version and loader.
 *   - Updates (wave M3) has three parts: the watched mod folders (which folder, for which Minecraft version and
 *     loader), a check that lists the newer files with an Update button each, and an undo list for the last
 *     replacements. It replaced the one-off check_mc_updates run: a file is replaced only inside a watched folder.
 *
 * It is a thin face on the ELEVEN Minecraft tools (search_mc_mods, get_mc_project, list_optifine_versions,
 * download_mc_file, download_optifine, make_modpack_file, and, for the Updates tab, check_mc_watched,
 * apply_mc_update, mc_watch, list_mc_replaced, undo_mc_update) and nothing else:
 *
 *   - It never talks to a vendor. Every action is /api/tools/preview (the same confirmation gate the Debug panel
 *     uses) and then /api/tools/run.
 *   - It never receives the CurseForge key and never reads curseforge.json. "No key" is learned from the tools' own
 *     answer (reason no_key) and the panel then points at Settings > Integrations.
 *   - Vendor text (names, summaries, file names, errors) is untrusted. Everything is put on the page with
 *     textContent / setAttribute only, never as HTML, and a link is made only for an https address on a vendor's own
 *     host.
 *   - Nothing is installed. A queued download goes to Downloads ("downloaded, not installed"); a pack is a file.
 *     The one thing that touches a file you already have is Update (and Undo) in the Updates tab: only inside a
 *     folder you put on the watched list, only after the owner's yes in a dialog that shows Jarvis's own plan
 *     line, and the old file is moved to a backup folder, never deleted.
 *   - Every write is: a dry run first (no dialog: it writes nothing), then ONE dialog that shows the dry-run message,
 *     the exact arguments, the folder and, for a pack that downloads files to match them, the file count and total
 *     size. Only a yes goes through. A batch from the tray is one dialog for the whole batch.
 *   - Questions a tool asks WHILE it runs (the hub's "Save outside the downloads library?", the pack's "Download mod
 *     files to match them on CurseForge?") have nobody to answer them in /api/tools/run. The panel sends
 *     confirm_ui: true, which the server turns into a "y", ONLY for a real write (never a dry run), ONLY after a
 *     dialog of its own showed what the answer covers (confirmPlan / mayPassYes below; the tests pin both).
 *
 * Public:  window.JarvisMinecraft.open() / .close()
 * Tested:  window.JarvisMinecraft._pure and ._flow (tests/verify_minecraft_panel.js).
 * ========================================================================= */
(function (global) {
  "use strict";

  const TOOLS = ["search_mc_mods", "get_mc_project", "list_optifine_versions",
    "download_mc_file", "download_optifine", "make_modpack_file",
    "check_mc_watched", "apply_mc_update", "mc_watch", "list_mc_replaced", "undo_mc_update"];
  // The tools that write something. Only these can ever be sent a confirm_ui, and never on a dry run.
  // (mc_watch also has a read, action "list": it is never sent a yes, see mayPassYes.)
  const WRITERS = ["download_mc_file", "download_optifine", "make_modpack_file", "apply_mc_update", "mc_watch", "undo_mc_update"];
  const PLATFORMS = ["modrinth", "curseforge"];
  const KINDS = ["mod", "modpack", "resourcepack", "shader", "datapack"];
  const LOADERS = ["fabric", "forge", "neoforge", "quilt"];
  const SORTS = ["relevance", "downloads", "updated", "newest"];
  const CHANNELS = ["release", "beta", "alpha"];
  const LINK_HOSTS = ["modrinth.com", "curseforge.com", "optifine.net"];
  const CHANNEL_CHOICES = [["release", "releases only"], ["beta", "releases and betas"], ["alpha", "every file"]];
  const MAX_ENTRIES = 500;
  const TRAY_MAX = 100;
  const LIBRARY_FOLDER = "Minecraft";
  const SITE = { modrinth: "Modrinth", curseforge: "CurseForge" };
  const LOADER_NAME = { fabric: "Fabric", forge: "Forge", neoforge: "NeoForge", quilt: "Quilt", vanilla: "no loader" };

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

  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function strList(v) { return (Array.isArray(v) ? v : []).filter((x) => typeof x === "string" && x).map((x) => trimmed(x)); }

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

  // ---- what I play (shared by every tab)

  function playOf(p) {
    const o = p && typeof p === "object" ? p : {};
    return { mc_version: trimmed(o.mc_version), loader: oneOf(o.loader, LOADERS, ""), source: oneOf(o.source, PLATFORMS.concat(["both"]), "") };
  }

  // A soft hint, never a block: the tools normalise the version themselves and say plainly when they cannot.
  function versionHint(v) {
    const s = trimmed(v);
    return !s || /^\d{1,2}\.\d{1,2}(\.\d{1,2})?$/.test(s) ? "" : "Jarvis expects an exact version such as 1.20.1 or 26.3.";
  }

  // The one line under the bar: what the other tabs will use.
  function playWords(play) {
    const p = playOf(play);
    if (!p.mc_version && !p.loader && !p.source) {
      return "Not set yet. Searches show everything; a download or a pack will ask for your version and loader.";
    }
    const bits = [p.mc_version || "any Minecraft version", p.loader ? LOADER_NAME[p.loader] : "any loader"];
    const where = !p.source ? "the sites Jarvis picks" : (p.source === "both" ? "both sites" : SITE[p.source]);
    return "Every tab uses " + bits.join(" on ") + ", looking on " + where + ".";
  }

  // The platform a typed project name or a new pack is for, when nothing says otherwise.
  function homeSource(play) { return playOf(play).source === "curseforge" ? "curseforge" : "modrinth"; }

  // ---- does a file fit what I play? (a search hit lists only a few versions, so it can say "maybe")

  const HIT_LIST_CUT = 6;                                         // the tool lists at most six versions per hit

  function fitOf(hit, play) {
    const h = hit && typeof hit === "object" ? hit : {};
    const p = playOf(play);
    if (h.distributable === false) return { state: "blocked", words: "The author does not allow downloads from third parties. Use the project page." };
    if (!p.mc_version && !p.loader) return { state: "unset", words: "Set your Minecraft version and loader above to see whether a file fits." };
    const versions = strList(h.mc_versions);
    const loaders = strList(h.loaders).map((x) => x.toLowerCase());
    const mods = ["mod", "modpack"].indexOf(trimmed(h.kind) || "mod") >= 0;
    const marks = [];
    let viaFabric = false;
    const mark = (state, label) => marks.push({ state, label });
    if (p.mc_version) {
      if (versions.indexOf(p.mc_version) >= 0) mark("fits", "Minecraft " + p.mc_version);
      else if (!versions.length || versions.length >= HIT_LIST_CUT) mark("maybe", "Minecraft " + p.mc_version);
      else mark("no", "Minecraft " + p.mc_version);
    }
    if (p.loader && mods) {
      if (loaders.indexOf(p.loader) >= 0) mark("fits", LOADER_NAME[p.loader]);
      else if (p.loader === "quilt" && loaders.indexOf("fabric") >= 0) { viaFabric = true; mark("fits", "Quilt"); }
      else if (!loaders.length) mark("maybe", LOADER_NAME[p.loader]);
      else mark("no", LOADER_NAME[p.loader]);
    }
    if (!marks.length) return { state: "unset", words: "This kind of file has no loader. Set your Minecraft version to check it." };
    const named = (state) => marks.filter((m) => m.state === state).map((m) => m.label).join(" and ");
    if (named("no")) return { state: "no", words: "No file for " + named("no") + " is listed. Open the files to be sure." };
    if (named("maybe")) return { state: "maybe", words: "Not sure for " + named("maybe") + ": open the files to check." };
    return { state: "fits", words: "A file fits " + named("fits") + (viaFabric ? ", through its Fabric build" : "") + "." };
  }

  // ---- arguments

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
  // A fourth part ("Sodium | | | curseforge") says which platform THAT entry is named on. Only the panel writes it, for a
  // mixed pack (entriesText with sources); a line the owner types has its fourth part dropped before it gets here.
  function parseEntries(raw) {
    const out = [];
    const lines = text(raw).split(/\r?\n/);
    for (const line of lines) {
      const parts = line.split("|").map((p) => p.trim());
      if (!parts[0]) continue;
      const entry = { project: parts[0] };
      if (parts[1]) entry.file = parts[1];
      if (parts[2] && CHANNELS.indexOf(parts[2].toLowerCase()) >= 0) entry.channel = parts[2].toLowerCase();
      if (parts[3] && PLATFORMS.indexOf(parts[3].toLowerCase()) >= 0) entry.source = parts[3].toLowerCase();
      out.push(entry);
      if (out.length >= MAX_ENTRIES) break;
    }
    return out;
  }

  // The platform that is not `p` (only ever asked about a known platform).
  function otherPlatform(p) { return p === "curseforge" ? "modrinth" : "curseforge"; }

  // A pack is MIXED when some entry says it is named on the platform the pack is NOT for. The tool then needs
  // `entries_from` (the other platform) AND a `source` on every entry: an entry with no source would be taken to be
  // named on `entries_from`, which for a project of the pack's own platform would be wrong. So every entry that has
  // no source gets the pack's own platform, here and nowhere else.
  function isMixed(entries, format) {
    const target = oneOf(format, PLATFORMS, "modrinth");
    return (Array.isArray(entries) ? entries : []).some((e) => e && e.source && e.source !== target);
  }

  function packArgs(f, dry) {
    // The loader is never guessed: an empty one stays empty and `problem` says so before any call.
    const args = { format: oneOf(f.format, PLATFORMS, "modrinth"), name: trimmed(f.name),
      mc_version: trimmed(f.mc_version), loader: oneOf(f.loader, LOADERS.concat(["vanilla"]), ""),
      entries: parseEntries(f.entries), channel: oneOf(f.channel, CHANNELS, "release") };
    const mixed = isMixed(args.entries, args.format);
    if (mixed) args.entries.forEach((e) => { if (!e.source) e.source = args.format; });
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
    // A mixed pack names the other platform itself, whatever the form says.
    const from = mixed ? otherPlatform(args.format) : oneOf(f.entries_from, PLATFORMS, "");
    const direction = crossDirection(args.format, from);
    if (direction) args.entries_from = from;
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
    if (kind === "optifine-download") return args.mc_version ? "" : "Say the Minecraft version you play in the bar at the top, like 1.20.1.";
    if (kind === "watch-add" || kind === "watch-set" || kind === "watch-remove") {
      if (!args.folder) return kind === "watch-add" ? "Choose the folder to watch: type its path or press Browse." : "Jarvis has no folder to change: pick one from the list.";
      return "";                                              // version and loader are optional: an empty one means the check will ask
    }
    if (kind === "apply" || kind === "undo") return args.id ? "" : "Jarvis gave no id this panel can send back for that row, so it cannot be " + (kind === "apply" ? "updated" : "undone") + " here.";
    if (kind === "pack") {
      if (!args.name) return "Give the pack a name.";
      if (!args.mc_version) return "Say the Minecraft version you play in the bar at the top, like 1.20.1.";
      if (!args.loader) return "Choose your loader in the bar at the top, or pick \u201cno loader\u201d here for shaders, resource packs and data packs only.";
      if (!args.entries || !args.entries.length) return "Add at least one project: put it in the tray, or type it below (one per line).";
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

  // A failure in plain words: a headline for the reason code, and the tool's own sentence under it as text.
  // retry: trying again can help; settings: Settings > Integrations is the place to fix it.
  const REASON_WORDS = {
    no_key: ["CurseForge needs your own free key.", false, true],
    bad_key: ["CurseForge did not accept the key.", false, true],
    rate_limited: ["The site asked Jarvis to slow down.", true, false],
    not_found: ["Nothing was found with that name or id.", false, false],
    ambiguous: ["More than one project could be meant.", false, false],
    not_distributable: ["The author does not allow downloads from third parties.", false, false],
    no_compatible_file: ["No file fits your Minecraft version and loader.", false, false],
    no_loader_version: ["Jarvis could not work out a loader version.", false, false],
    bad_dest: ["That folder can't be used.", false, false],
    exists: ["A file with that name is already there.", false, false],
    too_big: ["The file is too big for the limit set for it.", false, false],
    verify_failed: ["The file did not match its published checksum, so it was not kept.", true, false],
    vendor_changed: ["The site answered in a way Jarvis did not expect.", true, false],
    offline: ["Jarvis could not reach the site.", true, false],
    unsupported: ["Jarvis can't do that as asked.", false, false],
    timeout: ["This took longer than the panel waits.", true, false],
    panel: ["The panel could not reach Jarvis.", true, false],
  };

  function reasonWords(result) {
    const c = classify(result);
    if (c.kind === "ok" || c.kind === "cancelled") return null;
    const r = result && typeof result === "object" ? result : {};
    const code = c.kind === "timeout" ? "timeout" : trimmed(r.reason);
    const known = Object.prototype.hasOwnProperty.call(REASON_WORDS, code) ? REASON_WORDS[code] : null;
    const tool = text(c.message, 400);
    return { code, title: known ? known[0] : (tool || "That did not work."), detail: known ? tool : "", retry: known ? known[1] : false,
      settings: known ? known[2] : false };
  }

  // The sites that did not answer in a "both" search: the other one's hits are still shown (partial results).
  function sourceProblems(res) {
    const out = [];
    if (!res || typeof res !== "object") return out;
    PLATFORMS.forEach((s) => {
      if (res[s + "_error"]) out.push({ source: s, reason: trimmed(res[s + "_reason"]), error: text(res[s + "_error"], 300) });
    });
    return out;
  }

  // Does the answer name a folder the hub will ask about? Absolute path, ~ or drive letter.
  function isOutsideLibrary(dest) {
    const d = trimmed(dest);
    return d === "" || /^(~|[a-zA-Z]:[\\/]|[\\/])/.test(d);   // omitted = the default folder (~/Downloads)
  }

  // ---- the files of a project: which one is recommended, and why, in words

  function recommendWords(res, file) {
    const r = res && typeof res === "object" ? res : {};
    const f = file && typeof file === "object" ? file : {};
    const mc = trimmed(r.mc_version);
    const loader = trimmed(r.loader);
    const channel = oneOf(r.channel, CHANNELS, "release");
    const allowed = channel === "release" ? "releases only" : (channel === "beta" ? "releases and betas" : "every file");
    const target = [mc ? "Minecraft " + mc : "", loader ? LOADER_NAME[loader] || loader : ""].filter(Boolean).join(" on ");
    let why = "This is the file Jarvis would download" + (target ? ": it is built for " + target + " and fits your channel setting (" + allowed + ")." : " under your channel setting (" + allowed + ").");
    if (r.unfiltered === true || !mc || !loader) {
      why += " Jarvis could not check it against both your Minecraft version and your loader, so set both above to be sure.";
    }
    if (f.via === "fabric") why += " It is a Fabric build, offered because there is no Quilt file; Quilt can run most of them.";
    if (f.channel && f.channel !== "release") why += " It is a " + text(f.channel, 10) + ", not a full release.";
    return why;
  }

  // ---- the tray (a list in memory; nothing here talks to a tool)

  function safeRef(v) { const s = trimmed(v); return /^[A-Za-z0-9_.\-]{1,64}$/.test(s) ? s : ""; }

  function refOf(item) { return item ? (safeRef(item.id) || safeRef(item.slug)) : ""; }

  function trayKey(source, id) { return oneOf(source, PLATFORMS, "modrinth") + ":" + trimmed(id); }

  // `p` is a project record (a search hit or a project block); `file` is an optional file record of it.
  function trayItemFrom(p, file) {
    const proj = p && typeof p === "object" ? p : null;
    if (!proj) return null;
    const item = { source: oneOf(proj.source, PLATFORMS, "modrinth"), id: safeRef(proj.id), slug: safeRef(proj.slug),
      name: text(proj.name || proj.slug || proj.id, 80), kind: oneOf(proj.kind, KINDS, "mod"), file: "", fileLabel: "" };
    if (!refOf(item)) return null;
    if (file && typeof file === "object" && safeRef(file.file_id)) {
      item.file = safeRef(file.file_id);
      item.fileLabel = text(file.version || file.name || file.file_id, 60);
    }
    item.key = trayKey(item.source, item.id || item.slug);
    return item;
  }

  // One entry per project: adding a file to a project already in the tray pins that file.
  function trayAdd(list, item) {
    const cur = Array.isArray(list) ? list.slice() : [];
    if (!item || !item.key) return { list: cur, added: false, replaced: false, full: false };
    const at = cur.findIndex((x) => x.key === item.key);
    if (at >= 0) { cur[at] = item; return { list: cur, added: false, replaced: true, full: false }; }
    if (cur.length >= TRAY_MAX) return { list: cur, added: false, replaced: false, full: true };
    cur.push(item);
    return { list: cur, added: true, replaced: false, full: false };
  }

  function trayRemove(list, key) { return (Array.isArray(list) ? list : []).filter((x) => x.key !== key); }

  function trayHas(list, key) { return (Array.isArray(list) ? list : []).some((x) => x.key === key); }

  // The download form of one tray item (the same fields the other download flows use).
  function trayDownloadFields(item, ctx) {
    const c = ctx && typeof ctx === "object" ? ctx : {};
    const p = playOf(c.play);
    const out = { project: refOf(item), source: oneOf(item && item.source, PLATFORMS, "modrinth"), kind: oneOf(item && item.kind, KINDS, "mod"),
      channel: oneOf(c.channel, CHANNELS, "release") };
    put(out, "mc_version", p.mc_version);
    if (p.loader) out.loader = p.loader;
    put(out, "file", item && item.file);
    put(out, "dest", c.dest);
    if (c.with_dependencies) out.with_dependencies = true;
    return out;
  }

  // Plain words before any call: what the tray still lacks to be queued.
  function trayProblems(items, play) {
    const list = Array.isArray(items) ? items : [];
    if (!list.length) return "The tray is empty. Add something from Find first.";
    const p = playOf(play);
    const bad = list.filter((i) => !refOf(i));
    if (bad.length) return "Something in the tray has no usable id. Remove it and add it again.";
    const need = list.filter((i) => ["mod", "modpack"].indexOf(i.kind || "mod") >= 0 && !i.file && (!p.mc_version || !p.loader));
    if (need.length) {
      const names = need.slice(0, 3).map((i) => text(i.name, 30)).join(", ");
      return "Set your Minecraft version and loader in the bar at the top first: " + names + (need.length > 3 ? " and more" : "")
        + " need both, unless you pick an exact file.";
    }
    return "";
  }

  // Which tray items a pack can use. A pack is for ONE platform; its entries are looked up on the platform they are
  // named on (`origin`). Items named on the other platform are left out, never mixed in.
  function trayForPack(items, format, origin) {
    const list = Array.isArray(items) ? items : [];
    const target = oneOf(format, PLATFORMS, "modrinth");
    const by = { modrinth: list.filter((i) => i.source !== "curseforge"), curseforge: list.filter((i) => i.source === "curseforge") };
    let from = oneOf(origin, PLATFORMS, "");
    if (!from) from = by[target].length || !by[target === "modrinth" ? "curseforge" : "modrinth"].length ? target : (target === "modrinth" ? "curseforge" : "modrinth");
    const other = from === "modrinth" ? "curseforge" : "modrinth";
    return { origin: from, used: by[from], left: by[other], mixed: by.modrinth.length > 0 && by.curseforge.length > 0 };
  }

  // What the pack tab builds from the tray. `choice` is the owner's "where the tray's projects are named":
  //   ""      automatic: a tray on one platform behaves exactly as trayForPack says; a tray with BOTH platforms
  //           builds a mixed pack (every project on its own platform).
  //   "both"  the same, asked for by name (with a one-platform tray there is nothing to mix, so it is trayForPack's).
  //   a platform  only the projects named there; the others are left out and listed (trayForPack, as before).
  // `both` true = a mixed pack: `native` go in as they are, `foreign` are matched on the pack's platform, `origin` is the
  // platform they are named on (the one the pack is NOT for), nothing is left out.
  function trayPlan(items, format, choice) {
    const list = Array.isArray(items) ? items : [];
    const target = oneOf(format, PLATFORMS, "modrinth");
    const pick = trimmed(choice).toLowerCase();
    const onMr = list.filter((i) => i.source !== "curseforge");
    const onCf = list.filter((i) => i.source === "curseforge");
    if ((pick === "" || pick === "both") && onMr.length && onCf.length) {
      return { both: true, mixed: true, origin: otherPlatform(target), used: list.slice(), left: [],
        native: target === "modrinth" ? onMr : onCf, foreign: target === "modrinth" ? onCf : onMr };
    }
    const plain = trayForPack(list, target, pick === "both" ? "" : pick);
    return Object.assign({}, plain, { both: false, native: plain.origin === target ? plain.used : [], foreign: plain.origin === target ? [] : plain.used });
  }

  // The pack form's `entries` text: the used tray items first (with a pinned file when there is one), then what was typed.
  // opts.sources (a mixed pack): every line also says which platform it is named on, as a fourth part. A tray item
  // carries its own; a line the owner typed is named on opts.own (the pack's platform), whatever it said after the third
  // bar. Without opts.sources the text is exactly what it always was.
  function entriesText(items, extra, opts) {
    const o = opts && typeof opts === "object" ? opts : {};
    const withSource = o.sources === true;
    const own = oneOf(o.own, PLATFORMS, "modrinth");
    const lines = (Array.isArray(items) ? items : []).map((i) => {
      const ref = refOf(i);
      if (!ref) return "";
      const file = i.file && safeRef(i.file) ? i.file : "";
      if (withSource) return ref + " | " + file + " | | " + oneOf(i.source, PLATFORMS, "modrinth");
      return file ? ref + " | " + file : ref;
    }).filter(Boolean);
    let typed = text(extra).split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    // A fourth part is the panel's own (parseEntries reads it as a platform), never the owner's: it is cut from a typed
    // line so a typed "| curseforge" can neither turn an ordinary pack into a mixed one nor change a mixed pack's sources.
    typed = typed.map((l) => {
      const parts = l.split("|");
      if (parts.length <= 3 && !withSource) return l;
      const kept = [0, 1, 2].map((n) => (parts[n] || "").trim());
      if (!kept[0]) return "";
      return withSource ? kept[0] + " | " + kept[1] + " | " + kept[2] + " | " + own : kept.join("|").replace(/\|+$/, "");
    }).filter(Boolean);
    return lines.concat(typed).join("\n");
  }

  // The mixed pack in numbers, read from the arguments that will be sent (so the dialog says what the call does):
  // how many entries go in as they are, how many are named on the other platform and matched, and how.
  function mixedSplit(args) {
    const a = args && typeof args === "object" ? args : {};
    const entries = Array.isArray(a.entries) ? a.entries.filter((e) => e && typeof e === "object") : [];
    const to = oneOf(a.format, PLATFORMS, "modrinth");
    if (!isMixed(entries, to)) return { mixed: false, native: 0, foreign: 0, from: "", to, method: "" };
    const from = otherPlatform(to);
    const foreign = entries.filter((e) => e.source === from).length;
    return { mixed: true, native: entries.length - foreign, foreign, from, to, method: from === "curseforge" ? "sha1" : "fingerprint" };
  }

  // One or two plain sentences for the dialog and the pack tab.
  function mixedWords(split) {
    const m = split && typeof split === "object" ? split : {};
    if (!m.mixed) return "";
    const one = (n, a, b) => (n === 1 ? a : b);
    const how = m.method === "sha1"
      ? "by SHA-1 hash; nothing is downloaded"
      : "by fingerprint; each of those mod files may have to be read once, in memory, and is not saved";
    return "Mixed pack for " + SITE[m.to] + ": " + m.native + " " + one(m.native, "project is", "projects are") + " named on " + SITE[m.to] + " and "
      + one(m.native, "goes", "go") + " in as " + one(m.native, "it is", "they are") + "; " + m.foreign + " " + one(m.foreign, "project is", "projects are")
      + " named on " + SITE[m.from] + " and matched to the identical file on " + SITE[m.to] + " " + how + ". One with no identical file is skipped and listed, never swapped for a lookalike.";
  }

  // Which way a pack's entries are matched, as the tool will be asked (the form's own entries_from can be overruled by a mixed list).
  function directionOf(f) {
    const a = packArgs(f && typeof f === "object" ? f : {}, true);
    return crossDirection(a.format, a.entries_from);
  }

  // ------------------------------------------------------------------ Updates tab (M3): the pure helpers
  //
  // The tab talks to FIVE tools whose schemas are written by other agents at the same time (M1: check_mc_watched,
  // apply_mc_update, mc_watch; M2: list_mc_replaced, undo_mc_update). The ARGUMENT names below come from the brief and
  // the pinned contract and nothing else is sent. Every ANSWER is read defensively: a key that is not there is simply
  // not drawn, and the whole answer stays visible in the "Raw answer" fold.

  const WATCH_ACTIONS = ["list", "add", "remove", "set"];
  const APPLY_STATES = ["done", "started", "unchanged", "failed"];
  // The one honest line the tab carries (the owner's words).
  const RUNS_HERE = "Updates run on this PC; Jarvis keeps the old file in a backup folder; the CurseForge route is unverified.";
  const AUTO_WORDS = "\u201cAutomatic\u201d is this folder's own wish. Jarvis updates it by itself only when the master switch in Menu > Updates is also on.";
  const APPLY_NOTE = "The old file is moved to a backup folder, not deleted. Updates run on this PC.";

  // The first non-empty plain value among `keys` of an answer, as clipped text. A nested object (a file) gives its
  // file name or name. Used for answers whose exact key names are another agent's to fix.
  function pick(o, keys, max) {
    if (!o || typeof o !== "object") return "";
    for (let i = 0; i < keys.length; i += 1) {
      let v = Object.prototype.hasOwnProperty.call(o, keys[i]) ? o[keys[i]] : undefined;
      if (v && typeof v === "object" && !Array.isArray(v)) v = v.file_name || v.file || v.name || v.version;
      if (typeof v === "number" && isFinite(v)) return String(v);
      if (typeof v === "string" && v.trim()) return text(v.trim(), max || 300);
    }
    return "";
  }

  // A finding's or a replacement's id goes back to the tool EXACTLY as the tool gave it, so it is either usable as it
  // is or not usable at all (never trimmed, never cleaned): no control characters, not blank, not huge.
  function safeId(v) {
    return typeof v === "string" && v.trim() !== "" && v.length <= 512 && text(v) === v ? v : "";
  }

  // `mc_watch` arguments. Empty optional fields are left out (the tool's own meaning for "not given" applies). A real
  // call carries no dry_run; a dry run carries it; the read (action list) carries neither.
  function watchArgs(action, f, dry) {
    const form = f && typeof f === "object" ? f : {};
    const act = oneOf(action, WATCH_ACTIONS, "");
    const args = { action: act };
    if (act === "list") return args;
    // A folder the tool itself listed goes back exactly as listed (exact: true); one the owner typed is trimmed.
    if (typeof form.folder === "string" && form.folder.trim()) args.folder = form.exact === true ? form.folder : form.folder.trim();
    if (act === "add" || act === "set") {
      put(args, "label", form.label);
      put(args, "mc_version", form.mc_version);
      const loader = oneOf(form.loader, LOADERS, "");
      if (loader) args.loader = loader;
      args.channel = oneOf(form.channel, CHANNELS, "release");
      args.auto = form.auto === true;
    }
    if (dry) args.dry_run = true;
    return args;
  }

  // `apply_mc_update` / `undo_mc_update`: the id and nothing else.
  function applyArgs(id, dry) {
    const args = { id: safeId(id) };
    if (dry) args.dry_run = true;
    return args;
  }

  // The watched list of an answer (check_mc_watched or mc_watch list): [{ folder, label, mc_version, loader, channel, auto, exact }].
  // `folder` is the exact string the tool gave (it names the entry in a later remove or set); `exact` says it can be sent back.
  function watchedOf(result) {
    const list = result && typeof result === "object" && Array.isArray(result.watched) ? result.watched : [];
    const out = [];
    list.slice(0, 100).forEach((w) => {
      if (!w || typeof w !== "object" || typeof w.folder !== "string" || !w.folder.trim()) return;
      out.push({ folder: w.folder, exact: w.folder.length <= 1000 && text(w.folder) === w.folder,
        label: trimmed(text(w.label, 80)), mc_version: trimmed(text(w.mc_version, 20)), loader: oneOf(w.loader, LOADERS, ""),
        channel: oneOf(w.channel, CHANNELS, "release"), auto: w.auto === true });
    });
    return out;
  }

  // The findings of check_mc_watched (the pinned contract: id, title, installed, available, size_bytes, source, detail).
  function findingsOf(result) {
    const list = result && typeof result === "object" && Array.isArray(result.updates) ? result.updates : [];
    const out = [];
    list.slice(0, 500).forEach((u) => {
      if (!u || typeof u !== "object") return;
      const proj = u.project && typeof u.project === "object" ? u.project : {};
      const id = safeId(u.id);
      out.push({ id, title: pick(u, ["title"], 120) || pick(proj, ["name", "slug"], 120) || text(id, 80) || "(no name)",
        installed: pick(u, ["installed"], 200), available: pick(u, ["available"], 200),
        size: typeof u.size_bytes === "number" ? size(u.size_bytes) : "", source: pick(u, ["source"], 20),
        detail: pick(u, ["detail"], 240), folder: pick(u, ["folder"], 300), page_url: trimmed(u.page_url || proj.page_url) });
    });
    return out;
  }

  const WHY_WORDS = {
    not_on_modrinth: "Modrinth does not know this file. It may come from CurseForge or somewhere else, and Jarvis does not guess from a name or a size.",
    needs_mc_version: "It works on several Minecraft versions. Say which one you play: press Edit on the folder and fill it in.",
    needs_loader: "It works with several loaders. Say which one you use: press Edit on the folder and fill it in.",
    no_minecraft_version: "Modrinth lists no Minecraft version for it.",
    no_loader: "Modrinth lists no loader for it.",
    other_minecraft_version: "It is built for another Minecraft version than the one chosen for the folder, so no update was looked up.",
    other_loader: "It is built for another loader than the one chosen for the folder, so no update was looked up.",
    offline: "The site could not be reached.",
    rate_limited: "The site asked Jarvis to slow down.",
    vendor_changed: "The site's answer was not what Jarvis expected.",
    not_distributable: "The author does not allow downloads from third parties, so Jarvis tries nothing else.",
  };

  // A not-checked code in plain words. A code the panel does not know (a newer tool may add some) shows the
  // tool's own sentence, else the code itself: never a guess.
  function whyWords(why, detail) {
    const code = trimmed(why);
    if (code && Object.prototype.hasOwnProperty.call(WHY_WORDS, code)) return WHY_WORDS[code];
    return text(detail, 240) || (code ? "Not checked (" + text(code, 40) + ")." : "Not checked.");
  }

  // What was NOT checked, in words, with the reason: one line per entry of `problems` (the contract's {source, error},
  // read with a few optional extras) and per entry of `not_checked` when the answer has one. Never fewer lines than
  // entries (the list is cut at 50 with a visible "and N more").
  function problemLines(res) {
    const out = [];
    if (!res || typeof res !== "object") return out;
    const add = (p) => {
      if (typeof p === "string") { out.push(text(p, 300)); return; }
      if (!p || typeof p !== "object") return;
      const who = pick(p, ["file", "title", "folder", "label"], 90);
      const src = pick(p, ["source"], 24);
      const known = Object.prototype.hasOwnProperty.call(SITE, src.toLowerCase()) ? SITE[src.toLowerCase()] : src;
      const why = pick(p, ["why", "reason", "code"], 40);
      const detail = pick(p, ["error", "detail", "message"], 240);
      const words = whyWords(why, detail);
      out.push((who ? who + ": " : "") + (known ? known + " \u2014 " : "") + words);
    };
    (Array.isArray(res.problems) ? res.problems : []).forEach(add);
    (Array.isArray(res.not_checked) ? res.not_checked : []).forEach(add);
    if (out.length > 50) { const more = out.length - 50; out.length = 50; out.push("\u2026and " + more + " more (see the Raw answer)."); }
    return out;
  }

  // The tool asked a question (which Minecraft version or loader?) that the panel answers through the folder's own fields.
  function needsAnswer(res) {
    return !!res && typeof res === "object" && res.needs_clarification === true;
  }

  // What an apply or undo answer means, as one of the pinned four (done, started, unchanged, failed) or the owner's own
  // cancel. NOTHING is "done" unless the tool said ok AND state done: a contradiction (ok false with state done), an
  // unknown state or a missing one is "failed" with words that say the folder should be looked at, never a claim.
  // -> { kind, message, restart, backup, plan, uncertain }
  function applyOutcome(result) {
    const r = result && typeof result === "object" ? result : null;
    if (!r) return { kind: "failed", message: "Jarvis gave no answer.", restart: false, backup: "", plan: "", uncertain: true };
    const plan = text(r.plan, 700);
    if (r.reason === "cancelled" || (r.cancelled === true && /cancel/i.test(text(r.error)))) {
      return { kind: "cancelled", message: text(r.error || "Cancelled: nothing was run.", 200), restart: false, backup: "", plan, uncertain: false };
    }
    const err = text(r.error || "", 300);
    const timedOut = r.reason === "timeout" || /timed out/i.test(err);
    const state = typeof r.state === "string" ? r.state.trim().toLowerCase() : "";
    if (r.ok !== true || APPLY_STATES.indexOf(state) < 0 || state === "failed") {
      let message = err || text(r.message, 300);
      let uncertain = timedOut;
      if (r.ok === true && APPLY_STATES.indexOf(state) < 0) {
        message = "Jarvis's answer did not say whether anything changed. Look in the folder and the backup folder before trying again.";
        uncertain = true;
      }
      if (timedOut) message = (message ? message + " " : "") + "It may still have finished: look in the folder and the backup folder, then check again.";
      return { kind: "failed", message: text(message || "That did not work.", 400), restart: false, backup: "", plan, uncertain };
    }
    const done = state === "done";
    return { kind: state, message: text(r.message, 300), restart: done && r.needs_restart === true,
      backup: done ? pick(r, ["backup", "backup_dir", "backup_path", "backup_folder", "moved_to"], 400) : "", plan, uncertain: false };
  }

  // The last replacements (list_mc_replaced). WARNING, A GUESS: the contract pins the TOOL'S ARGUMENTS (none) but not the shape of
  // its answer, so these key names are a best guess that the panel tolerates being wrong about: whatever it cannot read
  // is not drawn, an entry without a usable id gets no Undo button, and the whole answer stays in the Raw answer fold.
  function replacedOf(result) {
    const r = result && typeof result === "object" ? result : {};
    const list = ["replaced", "items", "replacements", "entries"].map((k) => r[k]).find((v) => Array.isArray(v)) || [];
    return list.slice(0, 200).filter((x) => x && typeof x === "object").map((x) => {
      const at = x.at !== undefined ? x.at : (x.replaced_at !== undefined ? x.replaced_at : (x.date !== undefined ? x.date : x.when));
      let when = "";
      if (typeof at === "number" && isFinite(at) && at > 0) {
        const d = new Date((at < 1e12 ? at * 1000 : at));
        when = isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 16).replace("T", " ");
      } else if (typeof at === "string") when = text(at.trim(), 40);
      return { id: safeId(x.id), title: pick(x, ["title", "project", "name"], 120), oldFile: pick(x, ["old_file", "old", "from", "installed"], 200),
        newFile: pick(x, ["new_file", "new", "to", "available"], 200), when, folder: pick(x, ["folder", "dest"], 300),
        backup: pick(x, ["backup", "backup_dir", "backup_path", "backup_folder"], 400) };
    });
  }

  // Did the answer carry a list at all (an empty list is "nothing replaced", a missing one is "could not read it").
  function replacedListed(result) {
    const r = result && typeof result === "object" ? result : {};
    return ["replaced", "items", "replacements", "entries"].some((k) => Array.isArray(r[k]));
  }

  const CHANNEL_WORDS = { release: "releases only", beta: "releases and betas", alpha: "every file" };

  // One watched folder in words, for its card and for the dialog.
  function watchedWords(w) {
    const x = w && typeof w === "object" ? w : {};
    return [x.mc_version ? "Minecraft " + x.mc_version : "Minecraft version: the check will ask",
      x.loader ? LOADER_NAME[x.loader] : "loader: the check will ask", CHANNEL_WORDS[oneOf(x.channel, CHANNELS, "release")]];
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
  // a dry run. `shown` = the dialog was shown and answered yes for THIS call; `wanted` = the caller asked for it.
  function mayPassYes(name, args, shown, wanted) {
    return wanted === true && shown === true && WRITERS.indexOf(name) >= 0 && !(args && args.dry_run === true) && !isWatchRead(name, args);
  }

  // mc_watch with action "list" only reads the watched list: it is not a write, so it is never sent a yes.
  function isWatchRead(name, args) {
    return name === "mc_watch" && !!args && typeof args === "object" && args.action === "list";
  }

  // A dry run of a writer writes nothing, so it opens no dialog of its own: the ONE dialog comes after it and
  // shows what the dry run said. (Every other call keeps the server's confirmation setting.)
  function dryRunNeedsNoDialog(name, args) {
    return (WRITERS.indexOf(name) >= 0 && !!args && typeof args === "object" && args.dry_run === true) || isWatchRead(name, args);
  }

  const NOT_INSTALLED = "Downloaded, not installed: Jarvis never opens, unzips or runs it.";

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

    // Updates tab (M3). An update or an undo: Jarvis's own `plan` line (exact folder, exact file names, where the old file
    // goes) IS the dialog. A dry run that gives no plan line gets no yes passed on.
    if (kind === "apply" || kind === "undo") {
      const planLine = text(d.plan, 700);
      if (!planLine) {
        return { ok: false, folder: "", summary: "", why: "Jarvis's check did not say exactly what it would do (it gave no plan line), so this panel will not pass your yes on. Ask for it in chat instead: Jarvis asks you itself." };
      }
      const where = trimmed(d.folder) || trimmed(d.dest);
      const own = lines.filter((l) => l !== planLine);
      own.push("Plan: " + planLine);
      if (where && planLine.indexOf(where) < 0) own.push("Folder: " + where);
      if (kind === "apply") own.push(APPLY_NOTE);
      return { ok: true, folder: where, summary: own.join("\n"), why: "" };
    }
    // Adding, changing or removing a watched folder: the dialog shows the exact folder and, for a folder that updates
    // itself, what that means.
    if (kind === "watch") {
      const where = trimmed(d.folder) || trimmed(d.dest) || trimmed(a.folder);
      if (!where) return { ok: false, folder: "", summary: "", why: "Choose the folder first." };
      lines.push("Folder: " + where);
      if (a.action === "add" || a.action === "set") {
        lines.push("Minecraft version: " + (a.mc_version || "not set, the check will ask") + ". Loader: " + (a.loader ? LOADER_NAME[a.loader] : "not set, the check will ask") + ". Include: " + CHANNEL_WORDS[oneOf(a.channel, CHANNELS, "release")] + ".");
        if (a.auto === true) lines.push("Automatic is ticked: once the master switch in Menu > Updates is also on, Jarvis may replace files in this folder by itself. The old file always goes to a backup folder.");
      }
      return { ok: true, folder: where, summary: lines.join("\n"), why: "" };
    }

    // The folder: the dry run names it (its `dest`). A pack that has to download files is answered by a
    // question that does not name it, so there the owner must have typed it.
    let folder = trimmed(d.dest);
    if (!folder && kind === "pack-download") folder = trimmed(a.dest);
    if (!folder) {
      return { ok: false, folder: "", summary: "", why: kind === "pack-download"
        ? "Choose the folder to write the pack to first. A pack that has to download files is confirmed only once, and that confirmation has to show the folder."
        : "Jarvis's check did not say which folder it would write to, so this panel will not pass your yes on. Ask for it in chat instead: Jarvis asks you itself." };
    }
    lines.push("Folder: " + folder);

    if (kind === "download" || kind === "optifine") lines.push(NOT_INSTALLED);
    if (kind === "download" && a.with_dependencies) {
      const deps = Array.isArray(d.dependencies_would_queue) ? d.dependencies_would_queue.filter((x) => x && typeof x === "object") : [];
      const names = deps.slice(0, 8).map((x) => text(x.name_of_project || x.name || x.file_name || x.project_id, 60)).join(", ");
      lines.push(deps.length ? "Also queued, in the same folder: " + deps.length + " required dependenc" + (deps.length === 1 ? "y" : "ies") + " (" + names + ")."
        : "No required dependencies to queue.");
    }
    if (kind === "pack" || kind === "pack-download") {
      if (d.path) lines.push("File: " + text(d.path, 260));
      if (a.src) lines.push("Extra files folder: " + text(a.src, 260));
      const mix = mixedSplit(a);
      if (mix.mixed) lines.push(mixedWords(mix));
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

  let uid = 0;
  function nextId(prefix) { uid += 1; return "mc-" + prefix + "-" + uid; }

  // A labelled control. The <label> wraps the control, so a click on the words focuses it and a screen reader names it.
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

  function badge(source) {
    const s = oneOf(source, PLATFORMS, "");
    return el("span", { class: "mc-badge" + (s ? " mc-badge--" + s : ""), text: s ? SITE[s] : text(source, 20) });
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
     A dry run of a writer opens no dialog (it writes nothing; the one dialog comes after it).
     `dialogShown` says the caller already showed ONE dialog that covered this very call (the batch from the tray).
     `passYes` asks for confirm_ui: the server then answers "y" to the questions the tool asks while it
     runs. It is sent only when mayPassYes() agrees: the tool is a writer, the call is not a dry run and
     the owner said yes to a dialog that covered THIS call. */
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
    let shown = o.dialogShown === true;
    if (gated && !o.skipGate && !shown && !dryRunNeedsNoDialog(name, args)) {
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

  /* The tray's batch: a dry run for every item (no dialog), then ONE dialog for the whole batch that shows every
     dry-run message, the folder and every item's exact arguments, then one real call per item.
     `ask({title, confirmLabel, body})` -> Promise<bool> is the panel's dialog (a stub in the tests).
     -> { stage: "invalid" | "check" | "cancelled" | "done", message?, rows: [{ item, args, dry, plan, result, why }] } */
  function batchBody(ready, left) {
    const folders = [];
    ready.forEach((r) => { if (folders.indexOf(r.plan.folder) < 0) folders.push(r.plan.folder); });
    const lines = ["Queue " + plural(ready.length, "download", "downloads") + " from the tray. " + NOT_INSTALLED,
      folders.length === 1 ? "Folder: " + folders[0] : "Folders: " + folders.join("; ")];
    if (left.length) lines.push(plural(left.length, "item was", "items were") + " left out because its check did not pass: " + left.slice(0, 6).map((r) => text(r.item.name, 40)).join(", ") + (left.length > 6 ? " and more" : "") + ".");
    ready.forEach((r, i) => {
      const own = r.plan.summary.split("\n").filter((l) => !/^Folder: /.test(l) && l !== NOT_INSTALLED);
      lines.push("", (i + 1) + " of " + ready.length + ": " + text(r.item.name, 60));
      own.forEach((l) => lines.push("  " + l));
      lines.push("  Exact arguments: " + JSON.stringify(r.args));
    });
    return text(lines.join("\n"), 20000);
  }

  async function batchDownloadSteps(items, ctx, call, ask) {
    const bad = trayProblems(items, ctx && ctx.play);
    if (bad) return { stage: "invalid", message: bad, rows: [] };
    const rows = (items || []).map((item) => {
      const f = trayDownloadFields(item, ctx);
      return { item, args: downloadArgs(f, false), dryArgs: downloadArgs(f, true), dry: null, plan: null, result: null, why: "" };
    });
    for (const r of rows) {
      r.dry = await call("download_mc_file", r.dryArgs, {});
      if (!r.dry || r.dry.ok === false || r.dry.needs_clarification === true) { r.why = "check"; continue; }
      r.plan = confirmPlan("download", r.dry, r.args);
      if (!r.plan.ok) { r.why = "blocked"; r.plan = null; }
    }
    const ready = rows.filter((r) => r.plan);
    if (!ready.length) return { stage: "check", rows };
    if (ctx && ctx.dryOnly === true) return { stage: "checked", rows };       // the "Check only" button: nothing is asked, nothing is written
    const left = rows.filter((r) => !r.plan);
    const yes = await ask({ title: "Queue " + plural(ready.length, "download", "downloads") + "?", confirmLabel: "Queue", body: batchBody(ready, left) });
    if (!yes) return { stage: "cancelled", rows };
    for (const r of ready) {
      // The dialog above covered this exact call (its arguments are in the dialog), so no second one is shown.
      r.result = await call("download_mc_file", r.args, { sure: true, dialogShown: true, passYes: isOutsideLibrary(r.args.dest) });
      if (r.result && (r.result.reason === "rate_limited" || r.result.reason === "offline")) {
        ready.slice(ready.indexOf(r) + 1).forEach((x) => { x.why = "not_tried"; });
        break;
      }
    }
    return { stage: "done", rows };
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
      if (!(directionOf(f) === "modrinth_to_curseforge" && f.download_to_match === true)) {
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

  // ---- Updates tab (M3): the three write flows. Same order as every other write: a dry run (no dialog), ONE dialog that
  // shows Jarvis's own plan, then the real call that asks for the yes to be passed on. `call` is runTool in the panel.

  const WATCH_DIALOG = {
    add: ["Watch this folder?", "Watch"],
    set: ["Change this watched folder?", "Save"],
    remove: ["Stop watching this folder?", "Stop watching"],
  };

  async function watchSteps(action, f, call) {
    const act = oneOf(action, ["add", "remove", "set"], "");
    if (!act) return { stage: "invalid", message: "Jarvis does not know that change." };
    const args = watchArgs(act, f, false);
    const bad = problem("watch-" + act, args);
    if (bad) return { stage: "invalid", message: bad };
    const check = await call("mc_watch", watchArgs(act, f, true), {});
    if (!check || check.ok === false || check.needs_clarification === true) return { stage: "check", result: check };
    const plan = confirmPlan("watch", check, args);
    if (!plan.ok) return { stage: "blocked", result: check, message: plan.why };
    const result = await call("mc_watch", args, { sure: true, title: WATCH_DIALOG[act][0], confirmLabel: WATCH_DIALOG[act][1],
      summary: plan.summary, passYes: true });
    return { stage: "done", result };
  }

  // One update: `id` is the finding's id exactly as check_mc_watched gave it.
  async function applySteps(id, call) {
    const args = applyArgs(id, false);
    const bad = problem("apply", args);
    if (bad) return { stage: "invalid", message: bad };
    const check = await call("apply_mc_update", applyArgs(id, true), {});
    // A dry run changes nothing, so its own state is not read as "nothing to do": what stops it is a failure, a question
    // or an answer with no plan line.
    if (!check || check.ok !== true || check.needs_clarification === true || trimmed(check.state) === "failed") return { stage: "check", result: check };
    const plan = confirmPlan("apply", check, args);
    if (!plan.ok) return { stage: "blocked", result: check, message: plan.why };
    const result = await call("apply_mc_update", args, { sure: true, title: "Replace this mod file?", confirmLabel: "Replace",
      summary: plan.summary, passYes: true });
    return { stage: "done", result, outcome: applyOutcome(result) };
  }

  async function undoSteps(id, call) {
    const args = applyArgs(id, false);
    const bad = problem("undo", args);
    if (bad) return { stage: "invalid", message: bad };
    const check = await call("undo_mc_update", applyArgs(id, true), {});
    if (!check || check.ok !== true || check.needs_clarification === true || trimmed(check.state) === "failed") return { stage: "check", result: check };
    const plan = confirmPlan("undo", check, args);
    if (!plan.ok) return { stage: "blocked", result: check, message: plan.why };
    const result = await call("undo_mc_update", args, { sure: true, title: "Undo this replacement?", confirmLabel: "Undo",
      summary: plan.summary, passYes: true });
    return { stage: "done", result, outcome: applyOutcome(result) };
  }

  // ------------------------------------------------------------------ the panel's state (outlives the window)
  //
  // Everything here is plain, harmless text kept only in this script's memory: no localStorage, no cookie, nothing
  // secret. It lasts until the page is reloaded. Closing the panel keeps it; reopening restores it.

  const S = {
    overlay: null, panel: null, tab: "find", prevFocus: null, onKey: null, tabs: {},
    play: { mc_version: "", loader: "", source: "" },     // what I play: shared by every tab
    tray: [],                                              // what I picked
    saved: {},                                             // each form's fields, so a reopened panel is as it was left
    fits: [], trayListeners: [], cleanups: [], playUI: null, live: null,
  };

  function announce(message) { if (S.live) S.live.textContent = text(message, 200); }

  // A result that arrives below the fold is brought into view, as the tray's already is (block "nearest": nothing moves
  // when it is already visible). The Pack and OptiFine tabs call it once their answer or their one-line problem is drawn.
  function reveal(host) {
    if (host && typeof host.scrollIntoView === "function") { try { host.scrollIntoView({ block: "nearest" }); } catch (_) { /* old browser */ } }
  }

  function setPlay(partial) {
    S.play = playOf(Object.assign({}, S.play, partial || {}));
    if (S.playUI) S.playUI.show();
    S.fits = S.fits.filter((x) => x.node.isConnected);       // cards that were replaced are forgotten
    S.fits.forEach((x) => paintFit(x.node, x.hit));
  }

  // Keep a form's fields in memory between openings (and fill them back in now).
  function remember(root, key) {
    const saved = S.saved[key];
    if (saved) Object.keys(saved).forEach((k) => setField(root, k, saved[k]));
    const keep = () => { S.saved[key] = readForm(root); };
    root.addEventListener("change", keep);
    root.addEventListener("input", keep);
  }

  // ------------------------------------------------------------------ small pieces

  function notice(tone, message, buttons) {
    return el("div", { class: "mc-notice mc-notice--" + tone, role: tone === "error" ? "alert" : "status" }, [
      el("div", { class: "mc-notice__text", text: message }),
      buttons && buttons.length ? el("div", { class: "mc-notice__actions" }, buttons) : null,
    ]);
  }

  function button(label, cls, onclick, attrs) {
    return el("button", Object.assign({ type: "button", class: "btn btn--sm " + (cls || "btn--outline"), text: label, onclick }, attrs || {}));
  }

  function rawDetails(result) {
    let json = "";
    try { json = JSON.stringify(result, null, 2); } catch (_) { json = ""; }
    return el("details", { class: "mc-raw" }, [el("summary", { text: "Raw answer" }), el("pre", { class: "mc-pre", text: text(json, 20000) })]);
  }

  // Loading: the words, and placeholder cards of the same height as the real ones so nothing jumps when they arrive.
  function busy(host, label, cards) {
    host.textContent = "";
    host.setAttribute("aria-busy", "true");
    host.appendChild(el("div", { class: "mc-loading", role: "status" }, [el("span", { class: "mc-loading__dot", "aria-hidden": "true" }), el("span", { text: label })]));
    for (let i = 0; i < (cards || 0); i += 1) host.appendChild(el("div", { class: "mc-skeleton", "aria-hidden": "true" }));
  }

  function openSettings() {
    close();
    if (global.JarvisSettings && global.JarvisSettings.show) global.JarvisSettings.show("integrations");
  }

  // A failure in words: what happened, the tool's own sentence under it, and the buttons that help.
  function failNotice(words, ctx) {
    const buttons = [];
    if (words.settings) buttons.push(button("Open Settings > Integrations", "btn--outline", openSettings));
    if (words.retry && ctx && ctx.retry) buttons.push(button("Try again", "btn--outline", () => ctx.retry()));
    const tone = words.code === "timeout" || words.code === "rate_limited" || words.code === "offline" ? "warn" : "error";
    return notice(tone, words.title + (words.detail ? "\n" + words.detail : ""), buttons);
  }

  // One site that did not answer in a "both" search. The other site's hits are still shown.
  function sourceNotice(p, ctx) {
    const known = Object.prototype.hasOwnProperty.call(REASON_WORDS, p.reason) ? REASON_WORDS[p.reason] : null;
    const buttons = [];
    if (known && known[2]) buttons.push(button("Open Settings > Integrations", "btn--outline", openSettings));
    if (known && known[1] && ctx && ctx.retry) buttons.push(button("Try again", "btn--outline", () => ctx.retry()));
    return notice("warn", SITE[p.source] + " did not answer, so only the other site is shown.\n" + (p.error || (known ? known[0] : "")), buttons);
  }

  const FIT_MARK = { fits: "\u2713", maybe: "?", no: "\u2715", unset: "\u2013", blocked: "!" };

  function paintFit(node, hit) {
    const f = fitOf(hit, S.play);
    node.className = "mc-fit mc-fit--" + f.state;
    node.textContent = "";
    node.appendChild(el("span", { class: "mc-fit__mark", "aria-hidden": "true", text: FIT_MARK[f.state] }));
    node.appendChild(document.createTextNode(f.words));
  }

  function fitLine(hit) {
    const node = el("div", { class: "mc-fit" });
    paintFit(node, hit);
    S.fits.push({ node, hit });
    return node;
  }

  // ---- the tray button: one control, three states (not in the tray, in it, in it with another file)

  function trayState(item) {
    const at = S.tray.find((x) => x.key === item.key);
    if (!at) return { label: "Add to tray", pressed: false };
    if ((at.file || "") !== (item.file || "")) return { label: item.file ? "Use this file" : "Add to tray", pressed: false };
    return { label: "In the tray: remove", pressed: true };
  }

  function paintTrayButton(btn) {
    const item = btn._item;
    if (!item) return;
    const st = trayState(item);
    btn.textContent = st.label;
    btn.setAttribute("aria-pressed", st.pressed ? "true" : "false");
    btn.classList.toggle("is-on", st.pressed);
  }

  function syncTrayButtons() {
    if (!S.panel) return;
    S.panel.querySelectorAll("[data-tray-button]").forEach(paintTrayButton);
  }

  function setTray(list) {
    S.tray = list;
    syncTrayButtons();
    S.trayListeners.forEach((fn) => fn());
  }

  function toggleTray(item) {
    if (!item) { say("This one has no id Jarvis can use, so it can't go in the tray.", "warn"); return; }
    const st = trayState(item);
    if (st.pressed) {
      setTray(trayRemove(S.tray, item.key));
      announce(item.name + " removed from the tray. " + plural(S.tray.length, "item", "items") + " left.");
      return;
    }
    const r = trayAdd(S.tray, item);
    if (r.full) { say("The tray holds at most " + TRAY_MAX + " items.", "warn"); return; }
    setTray(r.list);
    announce(item.name + (r.replaced ? " now uses this file." : " added to the tray.") + " " + plural(S.tray.length, "item", "items") + " in the tray.");
  }

  function trayButton(item) {
    const btn = el("button", { type: "button", class: "btn btn--sm btn--outline mc-traybtn", "data-tray-button": "", onclick: () => toggleTray(btn._item) });
    btn._item = item;
    paintTrayButton(btn);
    return btn;
  }

  // ---- cards

  function hitCard(hit, handlers) {
    const name = text(hit.name, 120) || text(hit.slug, 80) || text(hit.id, 80);
    const item = trayItemFrom(hit, null);
    const open = el("button", { type: "button", class: "btn btn--sm btn--primary", text: "Show files", onclick: (e) => handlers.files(hit, e.currentTarget) });
    return el("li", { class: "mc-card" }, [
      el("div", { class: "mc-card__head" }, [
        el("span", { class: "mc-card__title", text: name }), badge(hit.source),
        hit.kind && hit.kind !== "mod" ? el("span", { class: "mc-chip", text: text(hit.kind, 20) }) : null,
      ]),
      el("div", { class: "mc-card__meta" }, [
        hit.author ? el("span", { text: "by " + text(hit.author, 40) }) : null,
        hit.downloads ? el("span", { text: count(hit.downloads) + " downloads" }) : null,
        hit.updated ? el("span", { text: "updated " + day(hit.updated) }) : null,
      ]),
      hit.summary ? el("p", { class: "mc-card__summary", text: hit.summary }) : null,
      fitLine(hit),
      el("div", { class: "mc-card__foot" }, [
        chips((hit.loaders || []).concat((hit.mc_versions || []).slice(0, 4))),
        el("span", { class: "mc-spacer" }),
        hit.page_url ? link(hit.page_url, "Open page") : null,
        item ? trayButton(item) : null,
        open,
      ]),
    ]);
  }

  function plainTable(headers, rows, stack) {
    return el("div", { class: "mc-tablewrap" }, [el("table", { class: "mc-table" + (stack ? " mc-table--stack" : "") }, [
      el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { scope: "col", text: h })))]),
      el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((c, i) => el("td", { "data-label": headers[i] || "" }, [c]))))),
    ])]);
  }

  function cell(v, max) { return typeof v === "object" && v !== null && v.nodeType ? v : document.createTextNode(text(v, max || 120)); }

  function projectCell(p) {
    const proj = p && typeof p === "object" ? p : { name: p };
    const name = text(proj.name || proj.slug || proj.id, 80);
    return proj.page_url ? link(proj.page_url, name) : cell(name);
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
          el("thead", {}, [el("tr", {}, cols.map((c) => el("th", { scope: "col", text: c.replace(/_/g, " ") })))]),
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

  // ------------------------------------------------------------------ the project view

  function depNames(requires) {
    const list = (Array.isArray(requires) ? requires : []).filter((d) => d && typeof d === "object");
    return list.map((d) => text(d.name || ("project " + d.project_id), 40));
  }

  // `ctx`: { project, openProject(ref) }
  function fileTable(files, res, ctx) {
    const rec = trimmed(res.recommended_file_id);
    const rows = files.map((f) => {
      const item = trayItemFrom(ctx.project, f);
      const needs = depNames(f.requires);
      const isRec = !!f.file_id && f.file_id === rec;
      const tr = el("tr", { class: isRec ? "is-recommended" : "" }, [
        el("td", { "data-label": "File" }, [
          el("div", { class: "mc-file__ver" }, [el("span", { text: text(f.version, 60) || "(no label)" }),
            isRec ? el("span", { class: "mc-chip mc-chip--rec", text: "Recommended" }) : null]),
          el("div", { class: "mc-mono mc-muted", text: text(f.name, 90) }),
          needs.length ? el("div", { class: "mc-small mc-muted", text: "Needs: " + needs.slice(0, 3).join(", ") + (needs.length > 3 ? " and " + (needs.length - 3) + " more" : "") }) : null,
        ]),
        el("td", { "data-label": "Channel", text: text(f.channel, 10) }),
        el("td", { "data-label": "Minecraft" }, [chips((f.mc_versions || []).slice(0, 4))]),
        el("td", { "data-label": "Loaders" }, [chips(f.loaders || []), f.via ? el("span", { class: "mc-chip mc-chip--warn", text: "via " + text(f.via, 10) }) : null]),
        el("td", { "data-label": "Size", text: size(f.size) }),
        el("td", { "data-label": "Date", text: day(f.date) }),
        el("td", { "data-label": "" }, [f.downloadable && item ? trayButton(item)
          : el("span", { class: "mc-chip mc-chip--warn", text: "not downloadable" })]),
      ]);
      return tr;
    });
    return el("div", { class: "mc-tablewrap" }, [el("table", { class: "mc-table mc-table--stack" }, [
      el("caption", { class: "mc-sr", text: "Files of this project" }),
      el("thead", {}, [el("tr", {}, ["File", "Channel", "Minecraft", "Loaders", "Size", "Date", ""].map((h) => el("th", { scope: "col", text: h })))]),
      el("tbody", {}, rows),
    ])]);
  }

  function projectHeader(p, ctx) {
    const sides = [];
    if (p.client_side && p.client_side !== "unknown") sides.push("Client: " + text(p.client_side, 20));
    if (p.server_side && p.server_side !== "unknown") sides.push("Server: " + text(p.server_side, 20));
    return el("div", { class: "mc-card mc-card--project" }, [
      el("div", { class: "mc-card__head" }, [el("h3", { class: "mc-card__title", tabindex: "-1", text: text(p.name, 120) }), badge(p.source),
        p.kind && p.kind !== "mod" ? el("span", { class: "mc-chip", text: text(p.kind, 20) }) : null]),
      el("div", { class: "mc-card__meta" }, [
        p.author ? el("span", { text: "by " + text(p.author, 40) }) : null,
        p.downloads ? el("span", { text: count(p.downloads) + " downloads" }) : null,
        p.updated ? el("span", { text: "updated " + day(p.updated) }) : null,
        sides.length ? el("span", { text: sides.join(", ") }) : null,
      ]),
      p.summary ? el("p", { class: "mc-card__summary", text: text(p.summary, 300) }) : null,
      el("div", { class: "mc-card__foot" }, [chips(strList(p.categories).slice(0, 8)), el("span", { class: "mc-spacer" }),
        p.page_url ? link(p.page_url, "Open page") : null]),
    ]);
  }

  // The tool's own notes about the list (unfiltered, loader, Quilt, distribution), as plain text.
  function projectNotes(res) {
    return ["note", "via_note", "loader_note", "distribution_note"].filter((k) => res[k]).map((k) => notice(k === "distribution_note" ? "warn" : "info", text(res[k], 400)));
  }

  function renderProject(res, host, ctx) {
    const p = res.project && typeof res.project === "object" ? res.project : null;
    if (!p) return;
    const project = Object.assign({}, p, { source: oneOf(p.source, PLATFORMS, ctx.source) });
    const here = Object.assign({}, ctx, { project });
    host.appendChild(projectHeader(project, here));
    projectNotes(res).forEach((n) => host.appendChild(n));
    const files = (Array.isArray(res.files) ? res.files : []).filter((f) => f && typeof f === "object");
    if (res.ok !== false && !files.length) {
      host.appendChild(el("div", { class: "mc-empty", text: "No files are listed for this project with these settings." }));
    }
    if (files.length) {
      const recFile = files.find((f) => f.file_id && f.file_id === res.recommended_file_id) || null;
      host.appendChild(el("div", { class: "mc-subhead", text: "Files (" + (Number.isInteger(res.files_total) && res.files_total > files.length ? files.length + " of " + res.files_total : files.length) + ")" }));
      host.appendChild(recFile
        ? el("div", { class: "mc-explain mc-explain--rec" }, [el("span", { text: recommendWords(res, recFile) }),
          trayButton(trayItemFrom(project, recFile)) ].filter(Boolean))
        : notice("info", "No file is marked as recommended. Pick one from the list, or set your Minecraft version and loader above."));
      host.appendChild(fileTable(files, res, here));
      if (Number.isInteger(res.files_total) && res.files_total > files.length && ctx.showAll) {
        host.appendChild(el("div", { class: "mc-actions" }, [button("Show all " + res.files_total + " files", "btn--ghost", ctx.showAll)]));
      }
      const needs = recFile ? (Array.isArray(recFile.requires) ? recFile.requires.filter((d) => d && typeof d === "object") : []) : [];
      host.appendChild(el("div", { class: "mc-subhead", text: recFile ? "The recommended file needs" : "Dependencies" }));
      if (!needs.length) {
        host.appendChild(el("div", { class: "mc-muted", text: recFile ? "No required dependencies." : "Pick a file to see what it needs." }));
      } else {
        host.appendChild(el("ul", { class: "mc-deps" }, needs.map((d) => {
          const dep = trayItemFrom({ source: project.source, id: d.project_id, name: d.name || ("project " + d.project_id), kind: "mod" }, d.file_id ? { file_id: d.file_id, version: "the file it names" } : null);
          return el("li", { class: "mc-dep" }, [el("span", { class: "mc-dep__name", text: text(d.name || ("project " + d.project_id), 60) }),
            el("span", { class: "mc-spacer" }),
            dep ? button("Look at it", "btn--ghost", () => ctx.openProject({ source: project.source, id: dep.id || dep.slug, name: dep.name })) : null,
            dep ? trayButton(dep) : null]);
        })));
        host.appendChild(el("div", { class: "mc-small mc-muted", text: "Jarvis lists these; it adds them to a download only if you tick the box in the tray, and to a pack by default." }));
      }
    }
    const alts = (Array.isArray(res.alternatives) ? res.alternatives : []).filter((f) => f && typeof f === "object");
    if (alts.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Closest files (none fits what you play)" }));
      host.appendChild(fileTable(alts, {}, here));
    }
  }

  // ------------------------------------------------------------------ Updates and cross-platform packs: drawing

  // ------------------------------------------------------------------ Updates tab (M3): drawing
  //
  // Everything from a tool (a project name, a file name, a folder, a message) is put on the page with textContent only
  // (el() does that), and a link is made only by link() -> safeUrl(). Nothing here is HTML built from text.

  function baseName(p) {
    const parts = trimmed(p).split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : trimmed(p);
  }

  // The watched folders as cards. `ctx`: { edit(w), remove(w), auto(w, on) }.
  function renderWatched(list, host, ctx) {
    host.textContent = "";
    host.removeAttribute("aria-busy");
    if (!list.length) {
      host.appendChild(el("div", { class: "mc-empty", text: "No folder is watched yet. Press \u201cAdd a folder\u201d and choose the folder your mods are in: Jarvis can then check it and swap in newer files." }));
      return;
    }
    host.appendChild(el("ul", { class: "mc-cards" }, list.map((w) => {
      const title = text(w.label, 80) || text(baseName(w.folder), 80);
      const tick = el("input", { type: "checkbox", name: "auto", "aria-label": "Automatic update for " + title });
      tick.checked = w.auto === true;
      if (w.exact) tick.addEventListener("change", () => ctx.auto(w, tick.checked));
      else tick.disabled = true;
      return el("li", { class: "mc-card" }, [
        el("div", { class: "mc-card__head" }, [el("span", { class: "mc-card__title", text: title }), w.auto ? el("span", { class: "mc-chip", text: "automatic" }) : null]),
        el("div", { class: "mc-mono", text: text(w.folder, 400) }),
        el("div", { class: "mc-card__meta" }, watchedWords(w).map((t) => el("span", { text: t }))),
        el("div", { class: "mc-card__foot" }, [
          el("label", { class: "mc-check" }, [tick, el("span", { text: "Automatic" })]),
          el("span", { class: "mc-spacer" }),
          w.exact ? button("Edit", "btn--outline", () => ctx.edit(w), { "aria-label": "Edit " + title }) : el("span", { class: "mc-chip mc-chip--warn", text: "name has characters this panel will not send back" }),
          w.exact ? button("Remove", "btn--outline", (e) => guarded(e.currentTarget, () => ctx.remove(w)), { "aria-label": "Remove " + title }) : null,
        ]),
      ]);
    })));
  }

  // The result of Check now. `ctx`: { apply(finding, paint) } where paint(outcome) marks the row afterwards.
  function renderFindings(res, host, ctx) {
    const findings = findingsOf(res);
    const lines = problemLines(res);
    if (res.partial === true || res.complete === false) {
      host.appendChild(notice("warn", "The check did not cover everything, so this list may be missing updates. Check again to carry on."));
    }
    if (needsAnswer(res)) {
      host.appendChild(notice("warn", (text(res.ask, 300) || "Jarvis needs to know which Minecraft version and loader a folder is for.")
        + "\nPress Edit on that folder above, fill in its Minecraft version and loader, then check again."));
    }
    if (findings.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Newer files (" + findings.length + ")" }));
      host.appendChild(plainTable(["Project", "Old file \u2192 new file", "Size", "Source", ""], findings.map((u) => {
        const state = el("span", { class: "mc-small mc-muted", role: "status" });
        const go = button("Update", "btn--primary", (e) => guarded(e.currentTarget, () => ctx.apply(u, (o) => {
          state.textContent = o.kind === "done" ? "Replaced. Press Check now again to confirm." : (o.kind === "started" ? "Started." : (o.kind === "unchanged" ? "Nothing to do." : (o.kind === "failed" ? "Not replaced." : "")));
          if (o.kind === "done" || o.kind === "started") go.hidden = true;
        })), u.id ? { "aria-label": "Update " + u.title } : { disabled: true });
        return [
          el("span", {}, [projectCell({ name: u.title, page_url: u.page_url }), u.folder ? el("div", { class: "mc-mono mc-muted", text: u.folder }) : null]),
          el("span", {}, [el("div", { class: "mc-mono", text: u.installed || "?" }), el("div", { class: "mc-mono", text: "\u2192 " + (u.available || "?") }),
            u.detail ? el("div", { class: "mc-muted mc-small", text: u.detail }) : null]),
          cell(u.size),
          u.source ? badge(u.source) : cell(""),
          el("span", { class: "mc-rowactions" }, [u.id ? go : el("span", { class: "mc-chip mc-chip--warn", text: "no id to send" }), state]),
        ];
      }), true));
    } else if (res.ok !== false && !needsAnswer(res)) {
      host.appendChild(el("div", { class: "mc-empty", text: Array.isArray(res.watched) && !res.watched.length
        ? "No folder is watched yet, so there was nothing to check."
        : "No newer file was found for the files that could be checked." + (lines.length ? " Some could not be checked: see below." : "") }));
    }
    if (lines.length) {
      host.appendChild(el("div", { class: "mc-subhead", text: "Not checked (" + lines.length + ")" }));
      host.appendChild(el("ul", { class: "mc-lines" }, lines.map((l) => el("li", { text: l }))));
    }
    if (res.curseforge_note) host.appendChild(el("div", { class: "mc-muted mc-small", text: text(res.curseforge_note, 400) }));
  }

  // The last replacements. `ctx`: { undo(item, paint) }.
  function renderReplaced(res, host, ctx) {
    const items = replacedOf(res);
    if (!replacedListed(res)) {
      host.appendChild(notice("warn", "Jarvis answered, but this panel could not find the list of replacements in the answer. It is in the Raw answer below."));
      return;
    }
    if (!items.length) { host.appendChild(el("div", { class: "mc-empty", text: "Nothing has been replaced yet, so there is nothing to undo." })); return; }
    host.appendChild(plainTable(["Project", "Old file", "Replaced by", "When", "Backup", ""], items.map((x) => {
      const state = el("span", { class: "mc-small mc-muted", role: "status" });
      const go = button("Undo", "btn--outline", (e) => guarded(e.currentTarget, () => ctx.undo(x, (o) => {
        state.textContent = o.kind === "done" ? "Put back." : (o.kind === "failed" ? "Not undone." : "");
        if (o.kind === "done") go.hidden = true;
      })), x.id ? { "aria-label": "Undo " + (x.title || x.id) } : { disabled: true });
      return [cell(x.title || x.id), el("span", { class: "mc-mono", text: x.oldFile }), el("span", { class: "mc-mono", text: x.newFile }), cell(x.when),
        el("span", { class: "mc-mono", text: x.backup || x.folder }), el("span", { class: "mc-rowactions" }, [x.id ? go : el("span", { class: "mc-chip mc-chip--warn", text: "no id to send" }), state])];
    }), true));
  }

  // What a write flow ended with, in words. `out` is what watchSteps / applySteps / undoSteps returned.
  function showStepOutcome(host, out, words) {
    host.textContent = "";
    host.removeAttribute("aria-busy");
    if (out.stage === "invalid") { host.appendChild(notice("warn", out.message)); return; }
    if (out.stage !== "done") {
      showResult(host, out.result, { readOnly: false, noFacts: true, noTables: true });
      if (out.message) host.appendChild(notice("warn", out.message));
      return;
    }
    const o = out.outcome;
    if (!o) { showResult(host, out.result, { readOnly: false, noFacts: true, noTables: true }); return; }     // a watched-folder change
    if (o.kind === "cancelled") { host.appendChild(notice("info", "Cancelled. Nothing was changed.")); return; }
    if (o.kind === "done") {
      host.appendChild(notice("ok", o.message || (words && words.done) || "Done."));
      if (o.restart) host.appendChild(notice("info", "Restart Minecraft for this to take effect."));
      if (o.backup) host.appendChild(el("div", { class: "mc-small", text: "The old file is kept in: " + o.backup }));
    } else if (o.kind === "started") {
      host.appendChild(notice("info", (o.message || "Started.") + " Press Check now again later to see whether it went through."));
    } else if (o.kind === "unchanged") {
      host.appendChild(notice("info", o.message || "Nothing needed doing."));
    } else {
      if (classify(out.result).kind === "not_confirmed") {
        host.appendChild(notice("warn", "Nothing was changed. Jarvis asked its own question and no yes reached it. This panel passes your yes on through the Jarvis web server, so if you have just updated, restart the server and try again."));
      } else {
        const w = reasonWords(out.result);
        host.appendChild(w ? failNotice(w, {}) : notice("error", o.message));
        if (w && o.uncertain) host.appendChild(notice("warn", "It may still have finished: look in the folder and the backup folder, then press Check now again."));
      }
    }
    host.appendChild(rawDetails(out.result));
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
    host.appendChild(el("div", { class: "mc-subhead", text: "Matching " + text(s.from, 20) + " to " + text(s.to, 20) + " by " + text(s.method, 20) }));
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
      }), true));
      if (s.more) host.appendChild(el("div", { class: "mc-muted mc-small", text: s.more + " more are not listed." }));
    }
  }

  // ------------------------------------------------------------------ showing an answer

  // After a refused question: the extra explanation its situation calls for.
  function followUps(result, root, ctx) {
    const kind = classify(result).kind;
    const bits = [];
    if (kind === "not_confirmed") {
      // Reached only when a question the tool asked while it ran got no yes: the panel passes the owner's
      // yes on (confirm_ui) through the web server, so an old server that has not been restarted is the likely cause.
      const buttons = ctx && ctx.canUseLibrary && root ? [
        button("Use the \u201c" + LIBRARY_FOLDER + "\u201d folder in the library", "btn--outline",
          () => { setField(root, "dest", LIBRARY_FOLDER); say("Folder set. Press the button again.", "info"); })] : null;
      bits.push(notice("warn", "Nothing was saved. Jarvis asked its own question about the folder and no yes reached it. "
        + "This panel passes your yes on through the Jarvis web server, so if you have just updated, restart the server and try again."
        + (buttons ? " A folder inside the Downloads library needs no question." : ""), buttons));
    }
    if (kind === "cancelled") bits.push(notice("info", "Cancelled. Nothing was run, saved or queued."));
    if (kind === "timeout" && ctx && !ctx.readOnly) {
      bits.push(notice("warn", "It may still have finished: check Downloads or the folder before trying again."));
    }
    if (kind === "timeout" && ctx && ctx.readOnly) {
      bits.push(notice("info", "Nothing was changed. Files already read are remembered, so trying again goes faster."));
    }
    return bits;
  }

  /* `o`: { root (for the library fallback), retry (a function that runs it again), canUseLibrary, readOnly,
            body(result, host), skipTables, noFacts, noTables } */
  function showResult(host, result, opts) {
    const o = opts || {};
    host.textContent = "";
    host.removeAttribute("aria-busy");
    const c = classify(result);
    const words = reasonWords(result);
    const asks = !!result && typeof result === "object" && result.needs_clarification === true;
    if (c.kind === "cancelled") { /* followUps says it, once */ }
    else if (c.kind === "not_confirmed") host.appendChild(notice("warn", c.message || "Not confirmed."));
    else if (words) host.appendChild(failNotice(words, o));
    else if (c.message) host.appendChild(notice(result && (result.warning || asks) ? "warn" : "ok", c.message));
    if (result && result.warning) host.appendChild(notice("warn", text(result.warning, 400)));
    if (result && result.untrusted_note) host.appendChild(el("div", { class: "mc-muted mc-small", text: text(result.untrusted_note, 300) }));
    followUps(result, o.root, o).forEach((n) => host.appendChild(n));
    sourceProblems(result).forEach((p) => host.appendChild(sourceNotice(p, o)));
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
    btn.setAttribute("aria-busy", "true");
    try { await fn(); } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); }
  }

  function showDownloadOutcome(host, out, root, retry) {
    if (out.stage === "invalid") { host.textContent = ""; host.appendChild(notice("warn", out.message)); return; }
    showResult(host, out.result, { root, canUseLibrary: true, retry });
    if (out.message) host.appendChild(notice("warn", out.message));
    if (out.stage === "done" && out.result && out.result.ok !== false && out.result.queued) say("Queued in Downloads.", "success");
  }

  async function runDownloadFlow(host, f, root) {
    busy(host, "Checking, then asking you once\u2026", 0);
    const go = async () => showDownloadOutcome(host, await downloadSteps(f, runTool), root, () => runDownloadFlow(host, f, root));
    await go();
  }

  // The batch's results: one line per item, in words.
  function renderBatch(host, out, dryOnly) {
    host.textContent = "";
    host.removeAttribute("aria-busy");
    if (out.stage === "invalid") { host.appendChild(notice("warn", out.message)); return; }
    if (out.stage === "cancelled") host.appendChild(notice("info", "Cancelled. Nothing was queued."));
    const lines = out.rows.map((r) => {
      let state = "";
      let detail = "";
      let tone = "info";
      if (r.result) {
        const w = reasonWords(r.result);
        if (w) { state = "Not queued"; detail = w.title + (w.detail ? " " + w.detail : ""); tone = "error"; }
        else if (r.result.queued === false) { state = "Already in Downloads"; detail = text(r.result.message, 300); tone = "warn"; }
        else { state = "Queued, not installed"; detail = text(r.result.message, 300); tone = "ok"; }
      } else if (r.why === "not_tried") { state = "Not tried"; detail = "An earlier item hit a limit, so this one was left."; tone = "warn"; }
      else if (r.why) {
        const w = reasonWords(r.dry);
        state = "Left out";
        detail = w ? w.title + (w.detail ? " " + w.detail : "") : text((r.dry && (r.dry.message || r.dry.question)) || "Its check did not give a plan.", 300);
        tone = "warn";
      } else if (r.plan) { state = dryOnly ? "Ready" : "Checked"; detail = text(r.dry && r.dry.message, 300); tone = "ok"; }
      return el("li", { class: "mc-batch__row mc-batch__row--" + tone }, [
        el("div", { class: "mc-batch__name" }, [el("span", { class: "mc-card__title", text: text(r.item.name, 60) }), badge(r.item.source), el("span", { class: "mc-chip", text: state })]),
        detail ? el("div", { class: "mc-small mc-muted", text: detail }) : null]);
    });
    host.appendChild(el("ul", { class: "mc-batch" }, lines));
    if (dryOnly && out.rows.some((r) => r.plan)) host.appendChild(el("div", { class: "mc-small mc-muted", text: "This was a check only: nothing was queued." }));
    const queued = out.rows.filter((r) => r.result && !reasonWords(r.result) && r.result.queued !== false).length;
    if (out.stage === "done" && queued) say(plural(queued, "download", "downloads") + " queued in Downloads.", "success");
  }

  // ------------------------------------------------------------------ the panel

  const TAB_LIST = [["find", "Find"], ["updates", "Updates"], ["optifine", "OptiFine"], ["pack", "Make a pack"]];

  function pickFolder(root, name, kind) {
    const picker = global.JarvisPathPicker;
    if (!picker || !picker.open) { say("The folder picker is not available here: type the path.", "warn"); return; }
    const start = (root.querySelector('[name="' + name + '"]') || {}).value || "";
    Promise.resolve(picker.open({ kind: kind || "folder", start, title: "Choose a folder" })).then((picked) => {
      const p = picked && typeof picked === "object" ? (picked.path || picked.full || "") : picked;
      if (typeof p === "string" && p) { setField(root, name, p); root.dispatchEvent(new Event("change", { bubbles: true })); }
    }).catch(() => say("The folder picker failed.", "warn"));
  }

  function folderRow(root, name, label, hint, placeholder) {
    return field(label, el("div", { class: "mc-row" }, [
      input(name, "", { placeholder: placeholder || "empty = your default download folder" }),
      el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Browse\u2026", onclick: () => pickFolder(root, name) }),
    ]), hint);
  }

  function loaderOptions(anyLabel) { return [["", anyLabel]].concat(LOADERS.map((l) => [l, LOADER_NAME[l]])); }

  // The numbers the tools take from the bar at the top, as the form fields the argument builders read.
  function playFields() { return { mc_version: S.play.mc_version, loader: S.play.loader }; }

  // ---- tab: find (search, then one project)

  function buildFind() {
    const root = el("div", { class: "mc-tab" });
    const listView = el("div", { class: "mc-view" });
    const projView = el("div", { class: "mc-view", hidden: true });
    const results = el("div", { class: "mc-results", "aria-live": "polite" });
    const heard = el("div", { class: "mc-small mc-muted mc-lastsearch" });
    let backTo = null;

    // -- search
    const form = el("div", { class: "mc-form" }, [
      el("div", { class: "mc-row mc-row--grow" }, [
        field("Search for", input("query", "", { placeholder: "a mod name, or a few words" })),
        el("button", { type: "button", class: "btn btn--primary mc-go", text: "Search", onclick: (e) => guarded(e.currentTarget, () => run(0)) }),
      ]),
      el("div", { class: "mc-grid" }, [
        field("What", select("kind", KINDS.map((k) => [k, k]), "mod")),
        field("Sort by", select("sort", SORTS.map((s) => [s, s]), "relevance")),
        field("Or open one project", el("div", { class: "mc-row" }, [
          input("project", "", { placeholder: "slug or id, e.g. sodium" }),
          el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Open", onclick: () => openByName() })]),
          "Uses the site chosen above."),
      ]),
    ]);
    remember(form, "find");
    const queryBox = form.querySelector('[name="query"]');

    let last = null;                                  // the last search, so Previous / Next and "without my filters" can repeat it
    async function run(offset, loose) {
      const f = readForm(form);
      const base = loose ? { source: S.play.source } : Object.assign({}, playFields(), { source: S.play.source });
      const args = searchArgs(Object.assign({}, f, base, { limit: 10, offset }));
      const bad = problem("search", args);
      if (bad) { results.textContent = ""; results.appendChild(notice("warn", bad)); return; }
      last = { loose: !!loose };
      busy(results, "Searching\u2026", 3);
      const r = await runTool("search_mc_mods", args, {});
      heard.textContent = "Results for \u201c" + args.query + "\u201d"
        + (args.mc_version || args.loader ? " (" + [args.mc_version, args.loader ? LOADER_NAME[args.loader] : ""].filter(Boolean).join(" on ") + ")" : " (not filtered by version or loader)");
      showResult(results, r, { root, noTables: true, noFacts: true, retry: () => run(offset, loose), body: (res, host) => renderHits(res, host, args, run) });
    }

    function renderHits(res, host, args, rerun) {
      if (!res || res.ok === false) return;
      const handlers = { files: (hit, btn) => { backTo = btn; openProject({ source: hit.source, id: hit.id || hit.slug, name: hit.name }); } };
      const lists = [];
      if (Array.isArray(res.hits)) lists.push([oneOf(res.source, PLATFORMS, ""), res.hits, res.total, false]);
      PLATFORMS.forEach((s) => { if (Array.isArray(res[s + "_hits"])) lists.push([s, res[s + "_hits"], res[s + "_total"], true]); });
      lists.forEach((l) => {
        if (l[3]) host.appendChild(el("div", { class: "mc-subhead", text: SITE[l[0]] + (typeof l[2] === "number" ? ": " + count(l[2]) + " found" : "") }));
        else if (typeof l[2] === "number") host.appendChild(el("div", { class: "mc-small mc-muted", text: count(l[2]) + " found on " + (SITE[l[0]] || "the site") }));
        if (l[1].length) host.appendChild(el("ul", { class: "mc-cards" }, l[1].map((h) => hitCard(h, handlers))));
        else if (l[3] || lists.length === 1) host.appendChild(el("div", { class: "mc-empty", text: "Nothing matched on " + (SITE[l[0]] || "this site") + "." }));
      });
      const shown = lists.reduce((n, l) => Math.max(n, l[1].length), 0);
      if (!shown && lists.length) {
        const filtered = !!(args.mc_version || args.loader);
        host.appendChild(el("div", { class: "mc-empty" }, [
          el("div", { text: "Nothing matched \u201c" + args.query + "\u201d" + (filtered ? " for " + [args.mc_version, args.loader ? LOADER_NAME[args.loader] : ""].filter(Boolean).join(" on ") : "") + "." }),
          el("div", { class: "mc-small mc-muted", text: "Try fewer words" + (filtered ? ", or search without your version and loader." : ".") }),
          filtered ? el("div", { class: "mc-actions" }, [button("Search without my version and loader", "btn--outline", (e) => guarded(e.currentTarget, () => rerun(0, true)))]) : null]));
      }
      const limit = args.limit || 10;
      const off = args.offset || 0;
      if (off > 0 || shown >= limit) {
        host.appendChild(el("div", { class: "mc-actions mc-actions--start" }, [
          off > 0 ? button("Previous", "btn--ghost", (e) => guarded(e.currentTarget, () => rerun(Math.max(0, off - limit), last && last.loose))) : null,
          shown >= limit ? button("Next", "btn--ghost", (e) => guarded(e.currentTarget, () => rerun(off + limit, last && last.loose))) : null]));
      }
    }

    form.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !e.target || e.target.tagName !== "INPUT") return;
      e.preventDefault();
      if (e.target.getAttribute("name") === "project") openByName(); else guarded(form.querySelector(".mc-go"), () => run(0));
    });

    // -- one project
    const projBody = el("div", { class: "mc-results", "aria-live": "polite" });
    const projBar = el("div", { class: "mc-projbar" }, [
      el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Back to results", onclick: () => showList() }),
      el("span", { class: "mc-spacer" }),
      field("Include", select("channel", CHANNEL_CHOICES, "release")),
      field("Files shown", select("files_limit", [["5", "5 newest"], ["20", "20 newest"], ["0", "all"]], "5")),
    ]);
    remember(projBar, "project");
    let current = null;
    async function lookup() {
      if (!current) return;
      const f = Object.assign({ project: current.id, source: current.source }, readForm(projBar), playFields());
      const args = projectArgs(f);
      const bad = problem("project", args);
      if (bad) { projBody.textContent = ""; projBody.appendChild(notice("warn", bad)); return; }
      busy(projBody, "Looking up the project and its files\u2026", 2);
      const r = await runTool("get_mc_project", args, {});
      showResult(projBody, r, { root, retry: lookup, skipTables: ["files", "alternatives", "candidates"], noFacts: true, body: (res, host) => {
        if (res && Array.isArray(res.candidates)) {
          host.appendChild(el("div", { class: "mc-subhead", text: "Which one?" }));
          host.appendChild(el("ul", { class: "mc-cards" }, res.candidates.map((h) => hitCard(h, { files: (x) => openProject({ source: x.source, id: x.id || x.slug, name: x.name }) }))));
        }
        renderProject(res, host, { source: current.source, openProject, showAll: () => { setField(projBar, "files_limit", "0"); reload(); } });
      } });
      const title = projBody.querySelector(".mc-card--project .mc-card__title");
      if (title && title.focus) title.focus();
    }
    let loading = false;
    const reload = async () => {
      if (loading) return;
      loading = true;
      try { await lookup(); } finally { loading = false; }
    };
    projBar.addEventListener("change", reload);
    projView.appendChild(projBar);
    projView.appendChild(projBody);

    function showList() {
      projView.hidden = true; listView.hidden = false;
      if (backTo && S.panel && S.panel.contains(backTo) && backTo.focus) backTo.focus(); else if (queryBox && queryBox.focus) queryBox.focus();
    }
    function openProject(ref) {
      const id = trimmed(ref && (ref.id || ref.slug));
      if (!id) return;
      current = { id, source: oneOf(ref.source, PLATFORMS, homeSource(S.play)) };
      listView.hidden = true; projView.hidden = false;
      selectTab("find");
      reload();
    }
    function openByName() {
      const name = trimmed((form.querySelector('[name="project"]') || {}).value);
      if (!name) { say("Type a project's slug or id first.", "warn"); return; }
      backTo = form.querySelector('[name="project"]');
      openProject({ id: name, source: homeSource(S.play) });
    }

    listView.appendChild(form);
    listView.appendChild(heard);
    listView.appendChild(results);
    results.appendChild(el("div", { class: "mc-empty", text: "Search for a mod, shader or pack. Results show whether a file fits the version and loader you set above." }));
    root.appendChild(listView);
    root.appendChild(projView);
    root._focusFirst = () => (projView.hidden ? queryBox : projBar.querySelector("button"));
    return root;
  }

  // ---- tab: updates (M3): watched folders, check and update, undo

  function buildUpdates() {
    const root = el("div", { class: "mc-tab" });
    const tight = (extra) => el("div", { class: "mc-results mc-results--tight", "aria-live": "polite" }, extra || []);
    const watchHost = tight();
    const watchOut = tight();
    const checkHost = tight();
    const applyHost = tight();
    const undoOut = tight();
    const undoHost = tight();
    let watched = null;                // null = the list was not read yet
    let working = false;               // one update or undo at a time
    let shown = false;
    let formMode = "add";
    let editing = null;

    // ---- watched folders
    const paintWatched = () => {
      if (watched) renderWatched(watched, watchHost, { edit: (w) => openForm("set", w), remove: (w) => runWatch("remove", { folder: w.folder, exact: true }),
        auto: (w, on) => runWatch("set", Object.assign({}, w, { auto: on, exact: true })) });
    };
    const readWatched = async () => {
      busy(watchHost, "Reading the watched list\u2026", 0);
      const r = await runTool("mc_watch", watchArgs("list", {}, false), {});
      if (r && r.ok !== false && Array.isArray(r.watched)) { watched = watchedOf(r); paintWatched(); return; }
      if (r && r.ok !== false) {
        watchHost.textContent = "";
        watchHost.removeAttribute("aria-busy");
        watchHost.appendChild(notice("warn", "Jarvis answered, but this panel could not find the list of watched folders in the answer. It is in the Raw answer below."));
        watchHost.appendChild(rawDetails(r));
        return;
      }
      showResult(watchHost, r, { readOnly: true, noFacts: true, noTables: true, retry: readWatched });
    };

    const folderBox = input("folder", "", { placeholder: "D:/Minecraft/mods" });
    const browse = el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Browse\u2026", onclick: () => pickFolder(form, "folder") });
    const formTitle = el("div", { class: "mc-subhead" });
    const saveBtn = el("button", { type: "button", class: "btn btn--primary", text: "Add folder" });
    const tickBox = el("input", { type: "checkbox", name: "auto" });
    const form = el("div", { class: "mc-card mc-watchform", hidden: true }, [
      formTitle,
      field("Folder", el("div", { class: "mc-row" }, [folderBox, browse]), "Only this folder is read, not its sub-folders."),
      el("div", { class: "mc-grid" }, [
        field("Name (optional)", input("label", "", { placeholder: "for example: my survival mods" })),
        field("Minecraft version (optional)", input("mc_version", "", { placeholder: "1.20.1" })),
        field("Loader (optional)", select("loader", loaderOptions("the check will ask"), "")),
        field("Include", select("channel", CHANNEL_CHOICES, "release")),
      ]),
      el("div", { class: "mc-small mc-muted", text: "Version and loader are optional. Left empty, the check will ask you which one a file is for and will not guess. To go back to \u201casking\u201d for a folder that has them, remove it and add it again." }),
      el("label", { class: "mc-check" }, [tickBox, el("span", { text: "Automatic" })]),
      el("div", { class: "mc-small mc-muted", text: AUTO_WORDS }),
      el("div", { class: "mc-actions" }, [
        el("button", { type: "button", class: "btn btn--sm btn--ghost", text: "Use what I play (bar at the top)", onclick: () => {
          if (S.play.mc_version) setField(form, "mc_version", S.play.mc_version);
          if (S.play.loader) setField(form, "loader", S.play.loader);
        } }),
        el("button", { type: "button", class: "btn btn--outline", text: "Cancel", onclick: () => { form.hidden = true; } }),
        saveBtn,
      ]),
    ]);
    const openForm = (mode, w) => {
      formMode = mode;
      editing = w || null;
      formTitle.textContent = mode === "add" ? "Add a folder to watch" : "Change this watched folder";
      setField(form, "folder", w ? w.folder : "");
      setField(form, "label", w ? w.label : "");
      setField(form, "mc_version", w ? w.mc_version : "");
      setField(form, "loader", w ? w.loader : "");
      setField(form, "channel", w ? w.channel : "release");
      setField(form, "auto", w ? w.auto : false);
      folderBox.readOnly = mode === "set";
      browse.hidden = mode === "set";
      saveBtn.textContent = mode === "add" ? "Add folder" : "Save changes";
      form.hidden = false;
      (mode === "set" ? form.querySelector('[name="label"]') : folderBox).focus();
    };

    const runWatch = async (action, f) => {
      busy(watchOut, "Checking, then asking you once\u2026", 0);
      const out = await watchSteps(action, f, runTool);
      showStepOutcome(watchOut, out, null);
      const ok = out.stage === "done" && !!out.result && out.result.ok !== false && classify(out.result).kind === "ok";
      if (ok) { form.hidden = true; announce("Watched folders changed."); await readWatched(); }
      else paintWatched();                 // a tick the owner then cancelled goes back to what the list says
      return ok;
    };
    saveBtn.addEventListener("click", (e) => guarded(e.currentTarget, () => {
      const f = readForm(form);
      if (formMode === "set" && editing) { f.folder = editing.folder; f.exact = true; }
      return runWatch(formMode, f);
    }));

    // ---- check and update
    const apply = async (u, paint) => {
      if (working) { say("Another update or undo is still running. Wait for it to finish.", "warn"); return; }
      working = true;
      try {
        busy(applyHost, "Checking, then asking you once\u2026", 0);
        const out = await applySteps(u.id, runTool);
        showStepOutcome(applyHost, out, { done: "Replaced." });
        if (out.outcome) paint(out.outcome);
        if (out.outcome && out.outcome.kind === "done") { announce("Replaced."); readReplaced(); }
      } finally { working = false; }
    };
    const runCheck = async () => {
      applyHost.textContent = "";
      busy(checkHost, "Reading the watched folders and asking the sites\u2026", 0);
      const r = await runTool("check_mc_watched", {}, {});
      showResult(checkHost, r, { readOnly: true, noFacts: true, noTables: true, retry: runCheck, body: (res, host) => {
        if (!res || res.ok === false) return;
        if (Array.isArray(res.watched)) { watched = watchedOf(res); paintWatched(); }
        renderFindings(res, host, { apply });
      } });
    };

    // ---- undo
    const undo = async (x, paint) => {
      if (working) { say("Another update or undo is still running. Wait for it to finish.", "warn"); return; }
      working = true;
      try {
        busy(undoOut, "Checking, then asking you once\u2026", 0);
        const out = await undoSteps(x.id, runTool);
        showStepOutcome(undoOut, out, { done: "Put back." });
        if (out.outcome) paint(out.outcome);
        if (out.outcome && out.outcome.kind === "done") { announce("Put back."); readReplaced(); }
      } finally { working = false; }
    };
    const readReplaced = async () => {
      busy(undoHost, "Reading the list of replacements\u2026", 0);
      const r = await runTool("list_mc_replaced", {}, {});
      showResult(undoHost, r, { readOnly: true, noFacts: true, noTables: true, retry: readReplaced, body: (res, host) => {
        if (res && res.ok !== false) renderReplaced(res, host, { undo });
      } });
    };

    // ---- the tab
    const head = (id, title, hint) => [
      el("div", { class: "mc-subhead mc-section__title", id, role: "heading", "aria-level": "3", text: title }),
      hint ? el("p", { class: "mc-note-inline", text: hint }) : null,
    ];
    const addBtn = button("Add a folder", "btn--primary", () => openForm("add", null));
    root.appendChild(el("div", { class: "mc-explain" }, [el("div", { text: RUNS_HERE })]));
    root.appendChild(el("section", { class: "mc-section", "aria-labelledby": "mc-upd-watch" }, head("mc-upd-watch", "Watched folders",
      "Jarvis checks and updates only the folders on this list, and only a file it can match exactly: it never swaps in a similar one. " + AUTO_WORDS)
      .concat([el("div", { class: "mc-actions mc-actions--start" }, [addBtn, button("Refresh the list", "btn--outline", (e) => guarded(e.currentTarget, readWatched))]),
        form, watchOut, watchHost])));
    root.appendChild(el("section", { class: "mc-section", "aria-labelledby": "mc-upd-check" }, head("mc-upd-check", "Check and update",
      "Check now reads each watched folder and asks the sites about the files in it; only file hashes are sent. Update shows you exactly what it will do before it does anything.")
      .concat([el("div", { class: "mc-actions mc-actions--start" }, [button("Check now", "btn--primary", (e) => guarded(e.currentTarget, runCheck))]),
        checkHost, applyHost])));
    root.appendChild(el("section", { class: "mc-section", "aria-labelledby": "mc-upd-undo" }, head("mc-upd-undo", "Undo",
      "The last files Jarvis replaced. Undo puts the old file back, after showing you what it will do.")
      .concat([el("div", { class: "mc-actions mc-actions--start" }, [button("Refresh the list", "btn--outline", (e) => guarded(e.currentTarget, readReplaced))]),
        undoOut, undoHost])));
    checkHost.appendChild(el("div", { class: "mc-empty", text: "Press Check now to see which files in the watched folders have a newer file for the version and loader chosen for them." }));
    root._focusFirst = () => addBtn;
    // Read the two lists the first time the tab is shown (not while the panel opens on another tab).
    root._onShow = () => {
      if (shown) return;
      shown = true;
      readWatched();
      readReplaced();
    };
    return root;
  }

  // ---- tab: OptiFine

  function buildOptifine() {
    const root = el("div", { class: "mc-tab" });
    const listHost = el("div", { class: "mc-results", "aria-live": "polite" });
    const dlHost = el("div", { class: "mc-results", "aria-live": "polite" });
    const form = el("div", { class: "mc-form" }, [
      el("div", { class: "mc-note-inline", text: "Uses the Minecraft version from the bar at the top." }),
      field("Build", input("version", "", { placeholder: "empty = the recommended one" })),
      check("include_preview", "List preview (test) builds too", false),
      check("preview", "Download a preview build", false),
      folderRow(root, "dest", "Save to", "Your mods folder, for example. A folder outside Jarvis's library asks you once more."),
      field("Save as", input("filename", "", { placeholder: "empty = the official name (.jar)" })),
    ]);
    remember(form, "optifine");
    const formFields = () => Object.assign({}, readForm(form), { mc_version: S.play.mc_version });
    const list = async (overview) => {
      const f = formFields();
      if (overview) f.mc_version = "";
      busy(listHost, "Asking optifine.net\u2026", 0);
      const r = await runTool("list_optifine_versions", optifineListArgs(f), {});
      showResult(listHost, r, { root, retry: () => list(overview) });
      reveal(listHost);
    };
    const get = async (dry) => {
      const f = formFields();
      const args = optifineArgs(f, true);
      const bad = problem("optifine-download", args);
      if (bad) { dlHost.textContent = ""; dlHost.appendChild(notice("warn", bad)); reveal(dlHost); return; }
      busy(dlHost, dry ? "Checking\u2026" : "Checking, then asking you once\u2026", 0);
      if (dry) { showResult(dlHost, await runTool("download_optifine", args, {}), { root, retry: () => get(true) }); reveal(dlHost); return; }
      const out = await optifineSteps(f, runTool);
      showResult(dlHost, out.result, { root, retry: () => get(false) });                    // no library fallback: OptiFine is always written to a full path
      if (out.message) dlHost.appendChild(notice("warn", out.message));
      reveal(dlHost);
    };
    form.appendChild(el("div", { class: "mc-actions" }, [
      el("button", { type: "button", class: "btn btn--ghost", text: "List every version", onclick: (e) => guarded(e.currentTarget, () => list(true)) }),
      el("button", { type: "button", class: "btn btn--ghost", text: "List builds for my version", onclick: (e) => guarded(e.currentTarget, () => list(false)) }),
      el("button", { type: "button", class: "btn btn--ghost", text: "Check only (dry run)", onclick: (e) => guarded(e.currentTarget, () => get(true)) }),
      el("button", { type: "button", class: "btn btn--primary", text: "Download OptiFine", onclick: (e) => guarded(e.currentTarget, () => get(false)) }),
    ]));
    root.appendChild(form); root.appendChild(listHost); root.appendChild(dlHost);
    root._focusFirst = () => form.querySelector("input");
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
    // A mixed pack (the tray holds both platforms): what is added as it is and what is matched, in words.
    const crossMix = el("div", { class: "mc-explain", hidden: true });
    const fromTray = el("div", { class: "mc-explain mc-explain--tray", "aria-live": "polite" });
    const entryList = el("ul", { class: "mc-pack-entries", "aria-label": "What goes in the pack", hidden: true });
    const form = el("div", { class: "mc-form" }, [
      el("div", { class: "mc-grid" }, [
        field("Build the pack for", select("format", [["modrinth", "Modrinth (.mrpack)"], ["curseforge", "CurseForge (.zip)"]], homeSource(S.play))),
        field("Pack name", input("name", "")),
        field("Pack version", input("version", "", { placeholder: "1.0.0" })),
        field("Loader", select("loader_choice", [["", "the loader above"], ["vanilla", "no loader (shaders, resource packs, data packs)"]], "")),
        field("Include", select("channel", CHANNEL_CHOICES, "release")),
      ]),
      fromTray, entryList,
      field("Where the tray's projects are named", select("entries_from", [["", "automatic"], ["both", "Both: each project on its own platform"], ["modrinth", "Modrinth only"], ["curseforge", "CurseForge only"]], ""),
        "Automatic: a tray on one platform builds a pack from it; a tray with both builds a mixed pack. Pick one platform to use only its projects (the others are left out and listed), or the other one to build the pack from a list named there."),
      crossMix, crossCf, crossMr,
      field("More projects", el("textarea", { name: "entries_extra", class: "mc-input mc-textarea", rows: "4", spellcheck: "false",
        placeholder: "One per line, after the tray's:\nsodium\niris | <file id to pin>" }), "Slug, id or exact name on the same platform as the tray's (in a mixed pack: on the platform the pack is for). Add | file id to pin one file."),
      el("details", { class: "mc-more" }, [el("summary", { text: "More options" }), el("div", { class: "mc-grid" }, [
        field("Loader version", input("loader_version", "", { placeholder: "empty = the latest" })),
        field("Author (CurseForge)", input("author", "")),
        field("Summary (Modrinth)", input("summary", "")),
        field("File name", input("filename", "", { placeholder: "empty = name-version" })),
      ]), folderRow(root, "src", "Extra files folder", "Optional: packed into the pack's overrides.", "optional")]),
      folderRow(root, "dest", "Write the pack to", "A pack that has to download files needs this filled in."),
      check("include_dependencies", "Add required dependencies", true),
      check("overwrite", "Replace a file of the same name", false),
    ]);
    remember(form, "pack");
    // The pack form as the argument builders read it: version and loader from the bar, the entries from the tray.
    const packFields = () => {
      const f = readForm(form);
      const cfg = trayPlan(S.tray, f.format, f.entries_from);
      return Object.assign({}, f, { mc_version: S.play.mc_version, loader: f.loader_choice === "vanilla" ? "vanilla" : S.play.loader,
        entries_from: cfg.origin, entries: entriesText(cfg.used, f.entries_extra, { sources: cfg.both, own: oneOf(f.format, PLATFORMS, "modrinth") }) });
    };
    const syncCross = () => {
      const f = packFields();
      const dir = directionOf(f);
      const cfg = trayPlan(S.tray, f.format, f.entries_from);
      // In a mixed pack the CurseForge-to-Modrinth block would say "each project"; the mixed block says which ones.
      crossCf.hidden = dir !== "curseforge_to_modrinth" || cfg.both;
      crossMr.hidden = dir !== "modrinth_to_curseforge";
      const split = mixedSplit(packArgs(f, true));
      crossMix.hidden = !split.mixed;
      crossMix.textContent = "";
      if (split.mixed) crossMix.appendChild(el("span", { text: mixedWords(split) + (split.method === "sha1" ? " Needs your CurseForge key." : "") }));
      fromTray.textContent = "";
      entryList.textContent = "";
      entryList.hidden = !cfg.both;
      if (cfg.both) {
        // Each entry shows its own platform and what the pack will do with it.
        cfg.used.slice(0, 40).forEach((item) => {
          const own = item.source === oneOf(f.format, PLATFORMS, "modrinth");
          entryList.appendChild(el("li", { class: "mc-pack-entry" }, [el("span", { class: "mc-pack-entry__name", text: text(item.name, 60) }), badge(item.source),
            el("span", { class: "mc-small mc-muted", text: own ? "goes in as it is" : "matched on " + SITE[f.format] + (dir === "curseforge_to_modrinth" ? " by SHA-1" : " by fingerprint") })]));
        });
        if (cfg.used.length > 40) entryList.appendChild(el("li", { class: "mc-small mc-muted", text: "and " + (cfg.used.length - 40) + " more" }));
      }
      if (!S.tray.length) {
        fromTray.appendChild(el("span", { text: "The tray is empty. Add projects from Find, or type them below." }));
      } else if (cfg.both) {
        fromTray.appendChild(el("span", { text: plural(cfg.native.length, "project", "projects") + " named on " + SITE[f.format] + " and " + cfg.foreign.length + " named on "
          + SITE[cfg.origin] + " are in this pack. Each keeps its own platform; nothing is left out." }));
      } else {
        fromTray.appendChild(el("span", { text: plural(cfg.used.length, "project from the tray goes", "projects from the tray go") + " in this pack, named on " + SITE[cfg.origin] + "."
          + (cfg.left.length ? " " + plural(cfg.left.length, "other is", "others are") + " named on the other platform and left out: " + cfg.left.slice(0, 5).map((i) => text(i.name, 30)).join(", ") + (cfg.left.length > 5 ? " and more" : "") + "." : "") }));
      }
    };
    form.addEventListener("change", syncCross);
    S.trayListeners.push(syncCross);
    syncCross();
    const make = async (dry) => {
      const f = packFields();
      busy(host, dry ? "Checking every project\u2026" : "Checking, then asking you once\u2026", 0);
      const out = dry ? await packCheck(f, runTool) : await packWrite(f, runTool);
      if (out.stage === "invalid") { host.textContent = ""; host.removeAttribute("aria-busy"); host.appendChild(notice("warn", out.message)); reveal(host); return; }
      const ticked = directionOf(f) === "modrinth_to_curseforge" && f.download_to_match === true;
      showResult(host, out.result, { root, retry: () => make(dry), skipTables: ["skipped"], body: (res, h) => renderPackBody(res, h, { ticked }) });
      if (out.message) host.appendChild(notice("warn", out.message));
      reveal(host);
    };
    form.appendChild(el("div", { class: "mc-actions" }, [
      el("button", { type: "button", class: "btn btn--ghost", text: "Check only (dry run)", onclick: (e) => guarded(e.currentTarget, () => make(true)) }),
      el("button", { type: "button", class: "btn btn--primary", text: "Make pack", onclick: (e) => guarded(e.currentTarget, () => make(false)) }),
    ]));
    root.appendChild(form); root.appendChild(host);
    root._focusFirst = () => form.querySelector('[name="name"]');
    return root;
  }

  // ---- the tray (a side panel on a wide window, a drawer under the work on a narrow one)

  function buildTray() {
    const root = el("aside", { class: "mc-tray", "aria-label": "Tray" });
    const count = el("span", { class: "mc-tray__count" });
    const toggle = el("button", { type: "button", class: "mc-tray__toggle", "aria-expanded": "false", onclick: () => {
      const on = !root.classList.contains("is-open");
      root.classList.toggle("is-open", on);
      toggle.setAttribute("aria-expanded", on ? "true" : "false");
    } }, [el("span", { class: "mc-tray__title", text: "Tray" }), count]);
    // On a wide window the tray is always open, so its header is a heading, not a button.
    const mq = global.matchMedia ? global.matchMedia("(max-width: 820px)") : null;
    const narrow = () => { toggle.disabled = !(mq && mq.matches); };
    narrow();
    if (mq && mq.addEventListener) { mq.addEventListener("change", narrow); S.cleanups.push(() => mq.removeEventListener("change", narrow)); }
    const list = el("ul", { class: "mc-tray__list" });
    const empty = el("div", { class: "mc-empty", text: "Nothing picked yet. Use \u201cAdd to tray\u201d on a result or on a file, then queue the downloads or make a pack from here." });
    const results = el("div", { class: "mc-results mc-results--tray", "aria-live": "polite" });
    const form = el("div", { class: "mc-form mc-form--tray" }, [
      folderRow(root, "dest", "Save downloads to", "A folder outside Jarvis's library asks you once more."),
      el("div", { class: "mc-grid mc-grid--tray" }, [field("Include", select("channel", CHANNEL_CHOICES, "release"))]),
      check("with_dependencies", "Also queue the required dependencies", false),
    ]);
    remember(form, "tray");
    const ctx = (extra) => Object.assign({ play: S.play }, readForm(form), extra || {});
    const batch = async (dryOnly) => {
      busy(results, dryOnly ? "Checking each item\u2026" : "Checking each item, then asking you once\u2026", 0);
      const out = await batchDownloadSteps(S.tray.slice(), ctx({ dryOnly }), runTool, askBatch);
      renderBatch(results, out, dryOnly);
      if (results.scrollIntoView) results.scrollIntoView({ block: "nearest" });
      if (out.stage === "check") results.insertBefore(notice("warn", "None of the items passed its check, so nothing can be queued."), results.firstChild);
    };
    const render = () => {
      list.textContent = "";
      S.tray.forEach((item) => {
        list.appendChild(el("li", { class: "mc-tray__item" }, [
          el("div", { class: "mc-tray__what" }, [el("span", { class: "mc-tray__name", text: text(item.name, 60) }), badge(item.source),
            el("span", { class: "mc-small mc-muted", text: item.file ? "file: " + (item.fileLabel || item.file) : "the newest file that fits" })]),
          el("button", { type: "button", class: "btn btn--sm btn--ghost mc-tray__remove", "aria-label": "Remove " + text(item.name, 60) + " from the tray", text: "Remove",
            onclick: () => { setTray(trayRemove(S.tray, item.key)); announce(item.name + " removed from the tray."); } }),
        ]));
      });
      list.hidden = !S.tray.length;
      empty.hidden = S.tray.length > 0;
      count.textContent = String(S.tray.length);
      toggle.setAttribute("aria-label", "Tray, " + plural(S.tray.length, "item", "items"));
      queue.disabled = check_.disabled = clear.disabled = pack.disabled = !S.tray.length;
    };
    const check_ = button("Check only", "btn--ghost", (e) => guarded(e.currentTarget, () => batch(true)));
    const queue = button("Queue downloads", "btn--primary", (e) => guarded(e.currentTarget, () => batch(false)));
    const pack = button("Make a pack from the tray", "btn--outline", () => selectTab("pack"));
    const clear = button("Clear the tray", "btn--ghost", () => { setTray([]); results.textContent = ""; announce("The tray is empty."); });
    root.appendChild(toggle);
    root.appendChild(el("div", { class: "mc-tray__body" }, [empty, list, form,
      el("div", { class: "mc-actions mc-actions--tray" }, [check_, queue]), el("div", { class: "mc-actions mc-actions--tray" }, [pack, clear]), results]));
    S.trayListeners.push(render);
    render();
    return root;
  }

  // The batch dialog: ONE for every download in it. Its body already holds every dry-run message, the folder and each
  // item's exact arguments, so the real calls that follow carry no dialog of their own (see batchDownloadSteps).
  async function askBatch(o) {
    const ui = UI();
    if (!ui || !ui.confirm) return false;
    return !!(await ui.confirm({ title: o.title, level: "warn", focusCancel: true, confirmLabel: o.confirmLabel, body: o.body }));
  }

  // ---- "what I play": set once, used by every tab

  function buildPlayBar() {
    const mc = input("mc_version", S.play.mc_version, { placeholder: "1.20.1", "aria-describedby": "mc-play-hint" });
    const ld = select("loader", loaderOptions("any loader"), S.play.loader);
    const src = select("source", [["", "automatic"], ["modrinth", "Modrinth"], ["curseforge", "CurseForge"], ["both", "both sites"]], S.play.source);
    const words = el("div", { class: "mc-play__words", id: "mc-play-hint", "aria-live": "polite" });
    const bar = el("div", { class: "mc-play", role: "group", "aria-label": "What I play" }, [
      el("div", { class: "mc-play__fields" }, [
        field("Minecraft version", mc), field("Loader", ld), field("Look on", src)]),
      words,
    ]);
    const read = () => setPlay({ mc_version: mc.value, loader: ld.value, source: src.value });
    S.playUI = { show: () => {
      const hint = versionHint(S.play.mc_version);
      words.textContent = playWords(S.play) + (hint ? " " + hint : "");
      words.classList.toggle("is-warn", !!hint);
      if (document.activeElement !== mc && mc.value !== S.play.mc_version) mc.value = S.play.mc_version;
      if (ld.value !== S.play.loader) ld.value = S.play.loader;
      if (src.value !== S.play.source) src.value = S.play.source;
    } };
    mc.addEventListener("input", read);
    ld.addEventListener("change", read);
    src.addEventListener("change", read);
    S.playUI.show();
    return bar;
  }

  // ---- shell

  function selectTab(id) {
    if (!TAB_LIST.some((t) => t[0] === id)) return;
    S.tab = id;
    if (!S.panel) return;
    S.panel.querySelectorAll("[role=tab]").forEach((b) => {
      const on = b.getAttribute("data-tab") === id;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle("is-active", on);
    });
    TAB_LIST.forEach((t) => { if (S.tabs[t[0]]) S.tabs[t[0]].hidden = t[0] !== id; });
    const shownTab = S.tabs[id];
    if (shownTab && typeof shownTab._onShow === "function") shownTab._onShow();      // the Updates tab reads its two lists once
  }

  // Left / Right / Home / End move between tabs, as on any tab list.
  function tabKeys(e) {
    const ids = TAB_LIST.map((t) => t[0]);
    const at = ids.indexOf(S.tab);
    let to = -1;
    if (e.key === "ArrowRight") to = (at + 1) % ids.length;
    else if (e.key === "ArrowLeft") to = (at + ids.length - 1) % ids.length;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = ids.length - 1;
    if (to < 0) return;
    e.preventDefault();
    selectTab(ids[to]);
    const btn = S.panel && S.panel.querySelector('[role=tab][data-tab="' + ids[to] + '"]');
    if (btn) btn.focus();
  }

  // The shared folder picker (web/public/path-picker.js) adds ONE element to the page: `<div class="jpp-backdrop">`
  // holding its card `<div class="jpp" role="dialog">`. While either is there, the picker owns the keys, so the Tab
  // trap and Escape below stand aside. This list is read against path-picker.js by tests/test_minecraft_panel.py, so a
  // renamed class there fails a test here instead of silently turning the guard off (it once named classes the
  // picker never had).
  const PICKER_SELECTOR = ".jpp-backdrop, .jpp";
  function pickerOpen(doc) {
    const d = doc || (typeof document !== "undefined" ? document : null);
    return !!(d && typeof d.querySelector === "function" && d.querySelector(PICKER_SELECTOR));
  }

  const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex=\"-1\"])";

  function open() {
    if (typeof document === "undefined") return;
    if (S.overlay) { selectTab(S.tab); return; }
    S.prevFocus = document.activeElement;
    S.fits = []; S.trayListeners = []; S.cleanups = [];
    S.tabs = { find: buildFind(), updates: buildUpdates(), optifine: buildOptifine(), pack: buildPack() };
    const bar = el("div", { class: "mc-tabs", role: "tablist", "aria-label": "Minecraft" }, TAB_LIST.map((t) => el("button", {
      type: "button", role: "tab", class: "mc-tabbtn", "data-tab": t[0], id: "mc-tab-" + t[0], "aria-controls": "mc-panel-" + t[0], text: t[1], onclick: () => selectTab(t[0]), onkeydown: tabKeys,
    })));
    TAB_LIST.forEach((t) => { S.tabs[t[0]].setAttribute("role", "tabpanel"); S.tabs[t[0]].id = "mc-panel-" + t[0]; S.tabs[t[0]].setAttribute("aria-labelledby", "mc-tab-" + t[0]); });
    S.live = el("div", { class: "mc-sr", role: "status", "aria-live": "polite" });
    const body = el("div", { class: "mc-body" }, TAB_LIST.map((t) => S.tabs[t[0]]));
    S.panel = el("div", { class: "menu-panel menu-panel--minecraft", role: "dialog", "aria-modal": "true", "aria-label": "Minecraft" }, [
      el("div", { class: "panel__corner panel__corner--tl" }), el("div", { class: "panel__corner panel__corner--br" }),
      el("div", { class: "menu-panel__head" }, [
        el("div", { class: "menu-panel__titles" }, [el("div", { class: "menu-panel__title", text: "MINECRAFT" }),
          el("div", { class: "menu-panel__subtitle", text: "Looks things up, queues downloads and writes pack files. In a folder you chose to watch it can swap in a newer file after you say yes: the old file goes to a backup folder, never deleted. It never opens or runs a file." })]),
        el("button", { class: "modal__close", type: "button", "aria-label": "Close", text: "\u00d7", onclick: close }),
      ]),
      buildPlayBar(),
      bar,
      el("div", { class: "mc-work" }, [body, buildTray()]),
      S.live,
    ]);
    S.overlay = el("div", { class: "menu-overlay mc-overlay" }, [S.panel]);
    S.overlay.addEventListener("mousedown", (e) => { if (e.target === S.overlay) close(); });
    S.onKey = (e) => {
      if (!S.overlay) return;
      if (document.querySelector("#jui-layer .jui-modal")) return;               // a confirmation owns the keys
      if (pickerOpen(document)) return;                                          // so does the folder picker
      if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); close(); return; }
      if (e.key !== "Tab" || !S.panel) return;                                   // keep Tab inside the panel
      const items = Array.prototype.filter.call(S.panel.querySelectorAll(FOCUSABLE), (n) => n.offsetParent !== null || n === document.activeElement);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (!S.panel.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", S.onKey, true);
    document.body.appendChild(S.overlay);
    selectTab(S.tab);
    const t = S.tabs[S.tab];
    const first = t && t._focusFirst ? t._focusFirst() : S.panel.querySelector(".mc-tab:not([hidden]) input");
    if (first && first.focus) first.focus();
  }

  function close() {
    if (!S.overlay) return;
    document.removeEventListener("keydown", S.onKey, true);
    S.cleanups.forEach((fn) => { try { fn(); } catch (_) { /* gone */ } });
    S.cleanups = [];
    if (S.overlay.parentNode) S.overlay.parentNode.removeChild(S.overlay);
    const back = S.prevFocus;
    S.overlay = null; S.panel = null; S.onKey = null; S.tabs = {}; S.playUI = null; S.live = null; S.fits = []; S.trayListeners = [];
    if (back && back.focus && document.contains(back)) { try { back.focus(); } catch (_) { /* gone */ } }
  }

  global.JarvisMinecraft = {
    open, close,
    _pure: { text, count, size, day, plural, safeUrl, searchArgs, projectArgs, downloadArgs, optifineListArgs, optifineArgs, parseEntries,
      packArgs, problem, classify, wantsNoKeyHint, isOutsideLibrary, TOOLS, WRITERS, LIBRARY_FOLDER,
      whyWords, crossDirection, skipWords, crossSummary,
      downloadNeeds, argsText, mayPassYes, confirmPlan, dryRunNeedsNoDialog,
      watchArgs, applyArgs, watchedOf, findingsOf, problemLines, needsAnswer, applyOutcome, replacedOf, replacedListed, watchedWords, safeId, pick, isWatchRead,
      RUNS_HERE, AUTO_WORDS, APPLY_STATES,
      playOf, versionHint, playWords, homeSource, fitOf, reasonWords, sourceProblems, recommendWords,
      trayKey, trayItemFrom, trayAdd, trayRemove, trayHas, trayDownloadFields, trayProblems, trayForPack, entriesText, refOf, batchBody,
      trayPlan, isMixed, mixedSplit, mixedWords, directionOf, otherPlatform, PICKER_SELECTOR, pickerOpen, reveal },
    // The write flows and the one function that talks to the server, for tests/verify_minecraft_panel.js (a stub fetch and a stub JarvisUI).
    _flow: { runTool, downloadSteps, batchDownloadSteps, optifineSteps, packCheck, packWrite, watchSteps, applySteps, undoSteps },
    // The drawing functions for the Updates tab, for tests/verify_minecraft_panel.js (it passes a tiny stand-in for `document`).
    _dom: { renderWatched, renderFindings, renderReplaced, showStepOutcome },
  };
})(typeof window !== "undefined" ? window : globalThis);
