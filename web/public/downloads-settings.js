/* Settings > Downloads (Download Hub phase 8, item 17.3-3).  The ONE place the hub's settings are changed.
 *
 * Registered with settings.js through window.JarvisSettingsSections (settings.js adds it to the rail and
 * hands build() the same row helpers its other sections use as ctx.ui).  Nothing here owns a rule:
 *   - what exists and what may be written comes from `jarvis hub-admin settings` (GET /api/hub/settings),
 *     which reads hub/settings.py's own WRITABLE / PREFS_WRITABLE lists.  A setting the hub doesn't accept
 *     yet simply has no row; a key another phase adds shows up as a generic row.  Two rows have a rule of their
 *     own (brief 7C): the History retention row asks first when it would remove rows, and a qBittorrent key that
 *     ~/.jarvis/qbittorrent.json also sets (`overridden_by` in the same answer) is shown read-only.
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
    { path: "concurrency.qbittorrent", group: "Running at once", label: "qBittorrent torrents at once", type: "int",
      help: "How many torrents the hub keeps running in qBittorrent together; the rest wait their turn." },
    { path: "http.stall_seconds", group: "Connections", label: "Call a download stalled after", type: "int", min: 10, unit: "seconds",
      help: "No bytes for this long counts as stalled and is retried." },
    { path: "http.max_retries", group: "Connections", label: "Retries before it fails", type: "int", min: 0, max: 10 },
    // labels only (brief 25): the hub's own range and rules are not repeated here; a value it refuses shows its refusal text
    { path: "http.segments", group: "Connections", label: "Parallel connections per file", type: "int",
      help: "1 = off. Only used for a file at least the size below, from a server that supports resuming. One download can override it." },
    { path: "http.segment_min_bytes", group: "Connections", label: "Smallest file that is split up", type: "int", unit: "bytes" },
    { path: "stremio.stall_seconds", group: "Torrents", label: "Call a torrent stalled after", type: "int", unit: "seconds",
      help: "No bytes for this long counts as stalled and is retried." },
    { path: "stremio.max_retries", group: "Torrents", label: "Torrent retries before it fails", type: "int" },
    { path: "stremio.metadata_timeout", group: "Torrents", label: "Wait for a torrent's file list", type: "int", unit: "seconds",
      help: "No peers means no list. After this long the torrent fails instead of waiting." },
    { path: "stremio.start_wait_seconds", group: "Torrents", label: "Wait for the Stremio server to start", type: "int", unit: "seconds" },
    // qBittorrent's behaviour (brief 31): the hub validates every value, so no range is repeated here.  Its connection (switch, address,
    // user name, password) is not a hub setting: it has its own card at the top of the qBittorrent block.
    { path: "qbittorrent.prefer_for_magnets", group: "Torrents", label: "Prefer qBittorrent for magnet links", type: "bool",
      help: "On = a magnet link goes to qBittorrent while it answers. Off = it is used only when you pick it for a download." },
    { path: "qbittorrent.category", group: "Torrents", label: "qBittorrent category", type: "text",
      help: "The category every torrent Jarvis adds is given. Empty = none." },
    { path: "qbittorrent.tag", group: "Torrents", label: "qBittorrent tag", type: "text",
      help: "A tag on every torrent Jarvis adds, so they are easy to find in qBittorrent and Jarvis counts only these. One word, no commas." },
    { path: "qbittorrent.save_subfolder", group: "Torrents", label: "qBittorrent saves into", type: "text",
      help: "A sub-folder of the download folder (like Torrents or Media/Torrents). Empty = the download folder itself. Changing it asks first." },
    { path: "qbittorrent.after_done", group: "Torrents", label: "When a qBittorrent torrent is done", type: "choice",
      choices: [["keep", "Keep it seeding"], ["pause", "Pause it"], ["remove", "Remove it from qBittorrent (keeps the files)"]],
      help: "Only for torrents Jarvis added. Changing it asks first." },
    { path: "qbittorrent.cancel_deletes_data", group: "Torrents", label: "Cancelling deletes unfinished data", type: "bool",
      help: "On = cancelling a torrent Jarvis added also deletes its unfinished data inside the download folder. Off = it is kept. Changing it asks first." },
    { path: "qbittorrent.metadata_timeout_s", group: "Torrents", label: "Wait for a magnet link's file list", type: "int", unit: "seconds",
      help: "No peers means no list. After this long the torrent fails instead of waiting." },
    { path: "qbittorrent.stall_minutes", group: "Torrents", label: "Call a qBittorrent torrent stalled after", type: "int", unit: "minutes",
      help: "No data for this long and the torrent fails. 0 = never." },
    { path: "qbittorrent.poll_s", group: "Torrents", label: "Ask qBittorrent for progress every", type: "float", unit: "seconds",
      help: "How often the hub checks qBittorrent. Faster means smoother progress and more requests." },
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
    // brief 7C: the hub's own range (0 to 3650) is not repeated here; a value it refuses shows its refusal text.  Lowering it asks first.
    { path: "retention.keep_history_days", group: "When a download finishes", label: "Keep finished downloads in History for", type: "int", min: 0, unit: "days",
      help: "Finished downloads older than this many days are removed from History. The files on disk are never deleted. 0 keeps everything." },
    { path: "allow_private_hosts", group: "Safety", label: "Allowed private addresses", type: "hosts",
      help: "Links to your own network are refused unless listed here: one host or host:port per line. Changing it asks first." },
  ];
  const EVENT_LABELS = {
    started: "A download starts", done: "A download finishes", failed: "A download fails",
    stalled: "A download stalls", group_done: "A whole batch finishes", queue_empty: "The queue empties",
  };
  const OPEN_LABELS = { off: "Nothing", reveal: "Show the file in its folder", folder: "Open the folder", open: "Open the file" };
  const GROUP_ORDER = ["Where files go", "Speed", "Running at once", "Connections", "Torrents", "When a download finishes",
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

  // ---- the qBittorrent connection card: pure half (the DOM half is below) ---------------------------------------------------
  // The card edits only the CONNECTION in ~/.jarvis/qbittorrent.json through GET / POST /api/hub/qbittorrent (hub_admin
  // qbittorrent_status / qbittorrent_save): the switch, the Web UI address, the user name and the password.  The hub's validator
  // (hub/engines/qbittorrent.py save_connection) is the only check.  The password is never drawn: the status says only whether one is saved.

  // What the card draws for a status answer.
  function qbtModel(s) {
    if (!s || typeof s !== "object" || s.ok === false || !s.values || typeof s.values !== "object") {
      return { loaded: false, tone: "bad", sentence: String((s && s.error) || "qBittorrent's connection settings couldn't be read."),
        file: "", exists: false, problem: "", passwordSet: false, overrides: [], values: null };
    }
    const eng = (s.engine && typeof s.engine === "object") ? s.engine : {};
    const connected = eng.ok === true;
    return {
      loaded: true, tone: connected ? "ok" : "",
      sentence: connected ? "Connected: qBittorrent answers on this PC." : String(eng.reason || s.problem || "Not connected yet."),
      file: String(s.file || ""), exists: s.exists === true, problem: s.problem ? String(s.problem) : "",
      passwordSet: s.password_set === true,
      overrides: Array.isArray(s.file_overrides) ? s.file_overrides.map(String).slice(0, 12) : [],
      values: s.values,
    };
  }

  // The names of the settings rows the file also holds (it wins over them), in the page's own words.
  function qbtOverrideNames(keys) {
    return (keys || []).map((k) => {
      const f = FIELDS.find((x) => x.path === "qbittorrent." + k);
      return f ? f.label : String(k);
    });
  }
  function qbtOverrideText(keys) {
    if (!keys || !keys.length) return "";
    return "The settings file also holds " + qbtOverrideNames(keys).join(", ") + ". The file wins, so those rows below change nothing until "
      + "the file lets go of them.";
  }

  // What a save sends.  A password is sent only when one was typed; an empty box sends nothing.
  function qbtBody(key, value) { return { [key]: value }; }
  function qbtPasswordBody(text) { return String(text || "") === "" ? null : { password: String(text) }; }
  function qbtForgetBody() { return { clear_password: true }; }
  function qbtReleaseBody() { return { release_settings: true }; }

  // ---- the page's structure, defaults and timing (brief 25, N20): pure, tested without a DOM -------------------------------

  // The rail, in order.  Every row lands in exactly one of these (or in Other, shown only when something has no home).
  const PAGE_SECTIONS = [
    { id: "files", title: "Files", lede: "Where finished downloads are kept and which folder each kind of file goes to." },
    { id: "speed", title: "Speed and schedule", lede: "One limit for everything the hub downloads, and windows of the day with a limit of their own." },
    { id: "running", title: "Running at once", lede: "How many downloads run together. Each source's own limit is in its block under Sources." },
    { id: "sources", title: "Sources", lede: "One block per download engine and per program that reports its own downloads. Open one to see its settings." },
    { id: "notifications", title: "Notifications", lede: "Which events speak up. How they reach you follows Settings > Notifications." },
    { id: "finish", title: "When a download finishes", lede: "What the hub does the moment a download is done." },
    { id: "safety", title: "Safety", lede: "Limits that keep the hub away from places it shouldn't go." },
  ];
  const OTHER_SECTION = { id: "other", title: "Other", lede: "Settings the hub accepts that this page has no wording for yet." };

  // Where a setting lives: {section, source}.  `source` names an engine block under Sources ("" = a row of the Sources section itself).
  function placeOf(path) {
    const p = String(path || "");
    let m;
    if (p === "library_root" || p === "ext_folders" || /^folders\./.test(p)) return { section: "files", source: null };
    if (p === "rate_limit_kbps" || p === "schedule") return { section: "speed", source: null };
    if (p === "concurrency.total") return { section: "running", source: null };
    if ((m = /^concurrency\.([a-z0-9_]+)$/.exec(p))) return { section: "sources", source: m[1] };
    if ((m = /^(http|stremio|qbittorrent)\.[a-z0-9_]+$/.exec(p))) return { section: "sources", source: m[1] };
    if (p === "unknown_settle_hours") return { section: "sources", source: "" };
    if (/^notify\./.test(p)) return { section: "notifications", source: null };
    if (/^after_download\./.test(p) || p === "retention.keep_history_days") return { section: "finish", source: null };
    if (p === "allow_private_hosts") return { section: "safety", source: null };
    return { section: "other", source: null };
  }

  // When a change takes effect, ONLY for the keys whose reading was checked in the hub's code (listed in the report):
  //   hub/daemon.py re-reads the settings every SETTINGS_TTL (2 s) and uses them for the speed in force (_global_bps), the slots
  //   (_caps), the finish pass (notifications, open-when-done), settling lost rows and the retry decision (_max_retries);
  //   hub/engines/http.py and stremio.py read their own blocks once, when a download starts.
  // Any other key says nothing rather than guess.
  const TIMING_RULES = [
    [/^(rate_limit_kbps|schedule)$/, "within a few seconds"],
    [/^concurrency\./, "within a few seconds"],
    [/^notify\./, "from the next announcement"],
    [/^after_download\./, "for downloads that finish after the change"],
    [/^unknown_settle_hours$/, "at the hub's next check of other tools' downloads"],
    [/^(http\.(stall_seconds|segments|segment_min_bytes)|stremio\.(stall_seconds|metadata_timeout))$/, "for the next download that starts"],
    [/^qbittorrent\.prefer_for_magnets$/, "for the next magnet link you add"],
    [/^qbittorrent\.[a-z_]+$/, "for the next torrent that starts"],
    [/^(http|stremio)\.max_retries$/, "the next time a download fails and the hub decides on a retry"],
    // hub/daemon.py _prune_history re-reads the setting each time it runs: when the hub starts, then every PRUNE_EVERY (an hour)
    [/^retention\.keep_history_days$/, "at the hub's next History clean-up (about once an hour while the hub runs)"],
  ];
  function timingFor(path) {
    for (const [re, text] of TIMING_RULES) if (re.test(String(path || ""))) return text;
    return "";
  }

  function valuesEqual(a, b) {
    if (a === b) return true;
    if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && valuesEqual(a[k], b[k]));
  }

  // The hub's own default for a key (undefined when the hub did not report defaults, so the row shows none).
  function defaultOf(payload, path) { return getPath((payload && payload.defaults) || {}, path); }

  function setPath(obj, path, value) {
    const keys = String(path).split(".");
    let node = obj;
    keys.slice(0, -1).forEach((k) => { if (node[k] === null || typeof node[k] !== "object") node[k] = {}; node = node[k]; });
    node[keys[keys.length - 1]] = value;
  }

  // A value in the words a person would use.  `spec.type`: bool, int, float, text, hosts, choice, list (a schedule or a folder map).
  function formatValue(spec, v, payload) {
    if (v === undefined) return "";
    if (v === null) return /\.level$/.test(String(spec.path || "")) ? "follow Settings > Notifications" : "not set";
    if (spec.type === "bool") return v === true ? "On" : "Off";
    if (spec.type === "hosts") return Array.isArray(v) && v.length ? v.join(", ") : "none";
    if (spec.type === "choice" && !/\.level$/.test(String(spec.path || ""))) {
      const named = Array.isArray(spec.choices) ? spec.choices.find((c) => c[0] === v) : null;
      return named ? named[1] : (OPEN_LABELS[v] || String(v));
    }
    if (spec.type === "list") {
      const n = Array.isArray(v) ? v.length : (v && typeof v === "object" ? Object.keys(v).length : 0);
      const noun = spec.path === "schedule" ? ["window", "windows"] : ["file type", "file types"];
      return n === 0 ? "none" : n + " " + (n === 1 ? noun[0] : noun[1]);
    }
    if (typeof v === "number") return String(v) + (spec.unit ? " " + spec.unit : "");
    if (v === "") return spec.path === "library_root" ? "the default Jarvis downloads folder" : "empty";
    return String(v);
  }

  // ---- brief 7C: the two rows with a rule of their own (pure; no DOM) -----------------------------------------------------
  const RETENTION_PATH = "retention.keep_history_days";
  const OVERRIDE_TEXT = "Set in qbittorrent.json: that value is used instead of the one saved here.";

  // The file that sets a key, as the hub's `overridden_by` names it ("" = nothing overrides it).  Names only: the page never sees a value.
  function overrideOf(payload, path) {
    const map = payload && payload.overridden_by;
    if (!map || typeof map !== "object" || !Object.prototype.hasOwnProperty.call(map, path)) return "";
    return typeof map[path] === "string" ? map[path] : "";
  }

  // Would this change remove History rows the hub keeps today?  Yes when the number goes down, and when a number is set where
  // there was 0 (keep everything).  Going up, setting 0, or a value the hub will refuse anyway (it says so itself) asks nothing.
  function retentionNeedsConfirm(current, next) {
    if (next === null || next === undefined || typeof next === "boolean" || String(next).trim() === "") return false;
    const to = Number(next);
    if (!isFinite(to) || !(to > 0)) return false;
    if (current === null || current === undefined || typeof current === "boolean" || String(current).trim() === "") return true;
    const was = Number(current);
    if (!isFinite(was) || !(was > 0)) return true;
    return to < was;
  }

  // The finish time (epoch seconds) before which a History row would go, for the hub's history `until`.
  function retentionCutoff(nowMs, days) { return Math.floor(nowMs / 1000 - Number(days) * 86400); }

  // What the confirmation says.  `count` is the number of rows the History list can see that old (null = it could not be counted).
  function retentionWarning(days, count) {
    const d = Number(days);
    const unit = d === 1 ? "day" : "days";
    const tail = " Only the History rows go: the files on disk are never deleted. A removed row can't be brought back.";
    let first;
    if (typeof count === "number" && count > 0) {
      first = "Up to " + count + " finished download" + (count === 1 ? "" : "s") + " older than " + d + " " + unit + " will be removed from History the next time the hub tidies up (about once an hour).";
    } else if (count === 0) {
      first = "No finished download in History is older than " + d + " " + unit + " yet, so nothing goes now. From then on a row older than that is removed when the hub tidies up (about once an hour).";
    } else {
      first = "Finished downloads older than " + d + " " + unit + " will be removed from History the next time the hub tidies up (about once an hour). I couldn't count how many that is.";
    }
    return { title: "Remove older downloads from History?", body: first + tail };
  }

  // What a row says under its help text: the default, whether the value differs, and when a change takes effect.
  function rowInfo(spec, payload, current) {
    const def = defaultOf(payload, spec.path);
    const has = def !== undefined;
    const timing = timingFor(spec.path);
    return {
      hasDefault: has, differs: has && !valuesEqual(current, def),
      defaultText: has ? "Default: " + formatValue(spec, def, payload) : "",
      timing: timing ? "Takes effect " + timing : "",
    };
  }

  // Search: every word typed must be somewhere in the text (label, help, the source it belongs to).
  function matchQuery(text, query) {
    const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    const hay = String(text || "").toLowerCase();
    return words.every((w) => hay.indexOf(w) !== -1);
  }

  // ---- the per-source blocks
  const ENGINE_WORDS = { ready: "Ready", needs_setup: "Needs setup", not_running: "Not running", off: "Off", broken: "Broken" };
  const ENGINE_DOTS = { ready: "ok", needs_setup: "warn", not_running: "warn", off: "off", broken: "bad" };
  // an engine the hub's `sources` answer didn't deliver (older hub, or the call failed): the three that have settings here
  const FALLBACK_ENGINES = [{ id: "http", label: "HTTP" }, { id: "ytdlp", label: "yt-dlp" }, { id: "stremio", label: "Stremio (torrent)" }];
  // The block's scroll anchor: the Sources tab's Configure button lands on it (tool.js CONFIGURE_ANCHORS).
  const SOURCE_ANCHORS = { http: "http", ytdlp: "yt-dlp", stremio: "stremio", qbittorrent: "qbittorrent" };
  function sourceAnchor(id) { return SOURCE_ANCHORS[id] || anchorOf("source " + id); }

  function providerDot(p) {
    if (p && p.enabled === false) return { tone: "off", word: "Off" };
    if (p && p.last_error) return { tone: "bad", word: "Error" };
    if (p && typeof p.live === "number" && typeof p.stale === "number" && p.live > 0 && p.stale >= p.live) return { tone: "warn", word: "Not reporting" };
    return { tone: "ok", word: "Reporting" };
  }

  // The blocks of the Sources section, in order: engines (as the Sources tab lists them), then the programs that report downloads.
  // `rowSources`: engine ids that some writable row belongs to, so a setting for an engine the answer didn't list still gets a block.
  function planSources(payload, sources, rowSources) {
    const out = [];
    const seen = {};
    const engines = sources && Array.isArray(sources.engines) && sources.engines.length ? sources.engines : FALLBACK_ENGINES;
    engines.forEach((e) => {
      if (!e || typeof e.id !== "string") return;
      seen[e.id] = true;
      out.push({ kind: "engine", id: e.id, label: e.label || e.id, anchor: sourceAnchor(e.id), tone: ENGINE_DOTS[e.state] || "off",
        word: ENGINE_WORDS[e.state] || "", reason: e.reason || "", config_file: e.config_file || "" });
    });
    (rowSources || []).forEach((id) => {
      if (id && !seen[id]) { seen[id] = true; out.push({ kind: "engine", id: id, label: id, anchor: sourceAnchor(id), tone: "off", word: "", reason: "", config_file: "" }); }
    });
    (Array.isArray(payload && payload.providers) ? payload.providers : []).forEach((p) => {
      if (!p || typeof p.id !== "string") return;
      const d = providerDot(p);
      out.push({ kind: "provider", id: p.id, label: p.label || p.id, anchor: "tool-" + anchorOf(p.id), tone: d.tone, word: d.word, reason: "", config_file: "", provider: p });
    });
    return out;
  }

  // ---- the two list editors
  // Which row of a list a hub refusal is about, from the hub's own sentence ("schedule window 2: ...", ".iso: ...", "bad extension 'x'"),
  // or -1 when it says none.  The sentence itself is shown unchanged.
  function rowOfError(kind, message, model) {
    const text = String(message || "");
    if (kind === "schedule") {
      const m = /schedule window (\d+)/.exec(text);
      const n = m ? Number(m[1]) - 1 : -1;
      return n >= 0 && n < model.length ? n : -1;
    }
    const norm = (e) => { const t = String(e || "").trim().toLowerCase(); return t && t[0] !== "." ? "." + t : t; };
    const bad = /bad extension '([^']*)'/.exec(text) || /bad extension "([^"]*)"/.exec(text);
    for (let i = 0; i < model.length; i++) {
      const ext = norm(model[i].ext);
      if (bad && String(model[i].ext || "").slice(0, 30) === bad[1]) return i;
      if (ext && text.toLowerCase().indexOf(ext + ":") === 0) return i;
    }
    return -1;
  }

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
      : web.configured ? "not answering at " + web.url : "not set up: choose the stremio-web folder below"]);
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

  // ---- the Stremio card: the settings rows (brief 29, N18 second half + N22 part e) -------------------------------------
  // Pure helpers again: what each row shows, and the body each save sends.  The hub (hub/stremio/config.py) is the only validator;
  // nothing here checks an address, a folder or a command, and the hub's refusal is shown as it came.

  // The server-address row and the stremio-web rows, from the status answer.
  function stremioSetupModel(s) {
    const ok = !!(s && typeof s === "object" && s.ok !== false && s.state);
    const wc = (ok && s.web_config && typeof s.web_config === "object") ? s.web_config : null;
    const web = (ok && s.web && typeof s.web === "object") ? s.web : null;
    return {
      ready: ok && !!wc,
      server: { value: wc ? String(wc.server_url || "") : "", def: wc ? String(wc.default_server_url || "") : "" },
      web: {
        url: wc ? String(wc.url || "") : "", dir: wc ? String(wc.dir || "") : "", startCmd: wc ? String(wc.start_cmd || "") : "",
        defUrl: wc ? String(wc.default_url || "") : "", defCmd: wc ? String(wc.default_start_cmd || "") : "",
        dirMissing: !!(wc && wc.dir && wc.dir_exists === false), answers: !!(web && web.answers), configured: !!(web && web.configured),
        thisPc: !wc || wc.this_pc !== false,
      },
      // the "Allow addresses other than this PC" box and where playback links open (browser | native)
      network: { allowRemote: !!(wc && wc.allow_remote), siteUrl: wc ? String(wc.stremio_site_url || "") : "" },
      open: { with: wc && wc.open_with === "native" ? "native" : "browser" },
    };
  }

  // The box: nothing to send when it matches what is saved.
  function networkSaveBody(checked, saved) {
    return !!checked !== !!saved ? { network: { allow_remote: !!checked } } : null;
  }

  // Where links open: nothing to send when it matches what is saved.
  function openSaveBody(value, saved) {
    const v = String(value || "").trim().toLowerCase();
    return (v === "browser" || v === "native") && v !== String(saved || "browser") ? { open_with: v } : null;
  }

  // One request out of several optional bodies (null = nothing), or null when none has anything.  The hub applies the box first,
  // so an address and the box that allows it can travel together.
  function mergeBodies() {
    const out = {};
    Array.prototype.slice.call(arguments).forEach((b) => { if (b && typeof b === "object") Object.assign(out, b); });
    return Object.keys(out).length ? out : null;
  }

  // What to send for the address row: nothing when it didn't change.
  function serverSaveBody(value, saved) {
    const v = String(value || "").trim();
    return v && v !== String(saved || "") ? { server_url: v } : null;
  }

  // What to send for the stremio-web rows: only the boxes that differ from what the hub reports, or null when none does.
  function webSaveBody(fields, saved) {
    const out = {};
    ["url", "dir", "start_cmd"].forEach((k) => {
      const key = k === "start_cmd" ? "startCmd" : k;
      if (fields[key] === undefined) return;
      const v = String(fields[key]).trim();
      if (v !== String((saved || {})[key] || "")) out[k] = v;
    });
    return Object.keys(out).length ? { web: out } : null;
  }

  // ---- the Stremio card: account, player, addon list (brief 30) ---------------------------------------------------------
  // Pure again.  The hub never sends the account key or an addon address (a debrid key can sit in one): only "signed in", the
  // email, the host of each saved addon and its position.  Every body below is null when there is nothing to send.
  function accountModel(s) {
    const a = (s && s.ok !== false && s.account && typeof s.account === "object") ? s.account : null;
    if (!a) return { ready: false, signedIn: false, email: "", sentence: "The account status isn't available." };
    const email = String(a.email || "");
    return { ready: true, signedIn: !!a.signed_in, email: email,
      sentence: a.signed_in ? "Signed in" + (email ? " as " + email : "") + ". Jarvis can read your Stremio addons and library."
        : "Not signed in. Without an account Jarvis only uses the addon list below." };
  }
  function loginBody(email, password) {
    const e = String(email || "").trim(), p = String(password || "");
    return e && p ? { action: "login", email: e, password: p } : null;
  }
  function keyBody(key) {
    const k = String(key || "").trim();
    return k ? { action: "key", auth_key: k } : null;
  }
  function playerModel(s) {
    const p = (s && s.ok !== false && s.player_config && typeof s.player_config === "object") ? s.player_config : null;
    if (!p) return { ready: false, kind: "auto", exe: "", missing: false };
    return { ready: true, kind: String(p.kind || "auto"), exe: String(p.exe || ""), missing: !!(p.exe && p.exe_exists === false) };
  }
  function playerSaveBody(fields, saved) {
    const out = {}, kind = String(fields.kind || "").trim(), exe = String(fields.exe || "").trim();
    if (kind && kind !== String((saved || {}).kind || "auto")) out.kind = kind;
    if (exe !== String((saved || {}).exe || "")) out.exe = exe;
    return Object.keys(out).length ? { player: out } : null;
  }
  function addonsModel(s) {
    const rows = (s && s.ok !== false && Array.isArray(s.addon_hosts)) ? s.addon_hosts : null;
    if (!rows) return { ready: false, rows: [] };
    return { ready: true, rows: rows.map((r) => ({ index: Number(r.index), host: String(r.host || "?") })) };
  }
  function addonsBody(text, removeIndex) {
    const add = String(text || "").split(/\s+/).filter(Boolean);
    const remove = Number.isInteger(removeIndex) ? [removeIndex] : [];
    return add.length || remove.length ? { addon_urls: { add: add, remove: remove } } : null;
  }

  // The cache row: where it is (only the running server knows) and the request to hand to Jarvis, which makes the change with
  // its own stremio_prepare tool and asks first.  The page never changes the cache itself.
  function stremioCacheModel(s) {
    const c = (s && s.cache && typeof s.cache === "object") ? s.cache : null;
    if (!c) return { known: false, text: "Not reported by this hub.", root: "", canAsk: false };
    if (!c.known) return { known: false, text: String(c.note || "Not known: Stremio's server must be running."), root: "", canAsk: false };
    const gb = typeof c.size_bytes === "number" ? " (size limit " + (c.size_bytes / 1073741824).toFixed(c.size_bytes % 1073741824 ? 1 : 0) + " GB)" : "";
    return { known: true, text: String(c.root) + gb, root: String(c.root), canAsk: true };
  }
  function cacheRequestText(folder) {
    return "Please move Stremio's cache to " + String(folder || "").trim() + " and keep its current size limit "
      + "(use stremio_prepare with cache_root set and cache \"keep\"; it will ask me to confirm).";
  }

  // The repair offer (N22 part e): one sentence from the hub, one button.  A file that isn't JSON is only touched after a yes.
  function repairModel(s) {
    const c = (s && s.config && typeof s.config === "object") ? s.config : null;
    if (!c || c.state === "ok" || !c.can_repair) return { offer: false };
    const pair = c.repair_pair || {};
    const copy = c.repair_needs_copy === true;
    return {
      offer: true, needsCopy: copy, sentence: String(c.sentence || ""),
      label: copy ? "Keep a copy and save a fresh file" : "Re-detect and save",
      detail: "Found " + (pair.how || "on this PC") + ": " + (pair.runtime_exe || "?") + " and " + (pair.server_js || "?")
        + (copy ? ". Your unreadable file is moved to stremio.json.bad (nothing is deleted)." : ". Only the two paths are written; everything else in the file stays."),
    };
  }
  // A file that isn't JSON while no working Stremio is known: the hub's sentence is all the card can say (no button to press).
  function repairBrokenFileHint(s) {
    const c = (s && s.config && typeof s.config === "object") ? s.config : null;
    return c && c.state === "broken" && !c.can_repair ? String(c.sentence || "") : "";
  }

  // One save through the paths route.  `deps.save(body)` -> the hub's answer, or throws its refusal.
  async function settingsSaveFlow(deps, body) {
    if (!body) return { status: "unchanged" };
    try { return { status: "saved", result: await deps.save(body), body: body }; }
    catch (err) { return { status: "refused", error: String((err && err.message) || err || "The hub didn't accept that."), body: body }; }
  }

  // The repair: `deps.confirm(text)` -> true to go on (asked only when a file would be moved aside), `deps.save(body)` as above.
  async function repairFlow(deps, status) {
    const m = repairModel(status);
    if (!m.offer) return { status: "none" };
    if (m.needsCopy) {
      const yes = await deps.confirm("Your stremio.json can't be read as JSON. Move it to stremio.json.bad and write a fresh file with the Stremio paths Jarvis found? Nothing is deleted.");
      if (!yes) return { status: "cancelled" };
    }
    return settingsSaveFlow(deps, m.needsCopy ? { repair: true, keep_copy: true } : { repair: true });
  }

  // Start stremio-web: the same start route, with the literal "web" (the server's Start sends no body).
  async function startWebFlow(deps, status) {
    const ms = startTimeoutMs(status);
    try {
      const r = await deps.start(ms);
      return { status: "ok", text: String(r.sentence || r.note || "stremio-web is running."), result: r };
    } catch (err) {
      if (err && err.timedOut) return { status: "timeout", text: "Still no answer after " + Math.round(ms / 1000) + " seconds. stremio-web can take a while the first time: press Check again in a moment." };
      const data = (err && err.data) || {};
      return { status: "refused", text: String(data.error || (err && err.message) || "stremio-web didn't start."), result: data };
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

  // brief 24: every block carries data-settings-anchor (its heading, lower-cased, letters and digits joined by dashes) so the
  // Downloads panel's Sources tab can open Settings > Downloads scrolled to it: "stremio", "connections", "running-at-once".
  function anchorOf(title) { return String(title || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40); }
  function anchored(node, title) {
    if (node && typeof node.setAttribute === "function") node.setAttribute("data-settings-anchor", anchorOf(title));
    return node;
  }

  // ---- the section --------------------------------------------------------------------------------------
  // (build() and the page are below, after the Stremio card)

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
    // ---- brief 29: the setting rows (server address, stremio-web, the cache, the repair offer).  Saved through the same
    // paths route as Locate...; the hub (hub/stremio/config.py) validates and its refusal is shown as it came.
    const field = (label, hint) => h("input", { type: "text", class: "prefs-input prefs-input--wide", spellcheck: "false", "aria-label": label, placeholder: hint || "" });
    const inServer = field("Stremio server address", "http://127.0.0.1:11470");
    const inWebDir = field("stremio-web folder", "the folder where stremio-web is");
    const inWebCmd = field("stremio-web start command", "pnpm start");
    const inWebUrl = field("stremio-web address", "http://127.0.0.1:8080");
    const chkRemote = h("input", { type: "checkbox", "aria-label": "Allow addresses other than this PC" });
    const bSiteWeb = btn("Use Stremio's own website", () => useSite());
    const selOpen = h("select", { class: "prefs-select", "aria-label": "Open Stremio links in" });
    [["browser", "The browser (stremio-web or Stremio's website)"], ["native", "The Stremio app"]].forEach((o) => selOpen.append(h("option", { value: o[0], text: o[1] })));
    const bSaveOpen = btn("Save", () => saveOpen());
    const bSaveServer = btn("Save address", () => saveServer());
    const bBrowseWeb = btn("Browse\u2026", () => browseWeb());
    const bSaveWeb = btn("Save web settings", () => saveWeb());
    const bStartWeb = btn("Start stremio-web", () => startWeb());
    const bRepair = h("button", { class: "btn btn--sm", type: "button", text: "Re-detect and save", onclick: () => repair() });
    const repairNote = h("div", { class: "prefs-note" });
    const repairDetail = h("div", { class: "prefs-row__help" });
    const repairBox = h("div", {}, [repairNote, repairDetail, h("div", { class: "prefs-inline" }, [bRepair])]);
    const cacheText = h("div", { class: "prefs-note" });
    const bCache = btn("Copy a request for Jarvis\u2026", () => askCache());
    all.push(bSaveServer, bBrowseWeb, bSaveWeb, bStartWeb, bRepair, bCache, chkRemote, bSiteWeb, selOpen, bSaveOpen);
    let filledFor = null;
    // ---- brief 30: the account, the player and the addon list.  Same route family as above; the page only ever sends what the
    // owner typed, and never shows the key or an addon address (the hub sends hosts and positions).
    const inEmail = h("input", { type: "email", class: "prefs-input prefs-input--wide", spellcheck: "false", autocomplete: "off", "aria-label": "Stremio account email", placeholder: "email" });
    const inPass = h("input", { type: "password", class: "prefs-input prefs-input--wide", autocomplete: "off", "aria-label": "Stremio account password", placeholder: "password" });
    const inKey = h("input", { type: "password", class: "prefs-input prefs-input--wide", autocomplete: "off", spellcheck: "false", "aria-label": "Stremio login key", placeholder: "or paste a login key" });
    const accountNote = h("div", { class: "prefs-note" });
    const bLogin = h("button", { class: "btn btn--sm", type: "button", text: "Sign in", onclick: () => accountDo(loginBody(inEmail.value, inPass.value), true) });
    const bSaveKey = btn("Save key", () => accountDo(keyBody(inKey.value), true));
    const bCheckAcc = btn("Check account", () => accountDo({ action: "check" }, false));
    const bLogout = btn("Sign out", () => signOut());
    const selPlayer = h("select", { class: "prefs-select", "aria-label": "Video player" });
    [["auto", "Automatic (mpv, then VLC)"], ["mpv", "mpv"], ["vlc", "VLC"]].forEach((o) => selPlayer.append(h("option", { value: o[0], text: o[1] })));
    const inPlayerExe = field("Player program", "full path to mpv.exe or vlc.exe; empty = look for it");
    const bBrowsePlayer = btn("Browse\u2026", () => browsePlayer());
    const bSavePlayer = btn("Save player", () => savePlayer());
    const playerNote = h("div", { class: "prefs-row__help" });
    const addonList = h("div", { class: "prefs-list" });
    const inAddon = field("Addon address", "https://\u2026/manifest.json (one or more, separated by spaces)");
    const bAddAddon = btn("Add", () => runSave(addonsBody(inAddon.value, null), "Addon added").then(() => { inAddon.value = ""; }));
    all.push(inEmail, inPass, inKey, bLogin, bSaveKey, bCheckAcc, bLogout, selPlayer, inPlayerExe, bBrowsePlayer, bSavePlayer, bAddAddon);
    let extraFor = null;

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
      paintSetup();
    }

    // The rows are filled from the hub's answer once per answer, so a repaint (a button's busy state) never erases what is being typed.
    function paintSetup() {
      const set = stremioSetupModel(state.status);
      const rep = repairModel(state.status);
      repairBox.hidden = !rep.offer;
      repairNote.textContent = rep.offer ? rep.sentence : "";
      repairDetail.textContent = rep.offer ? rep.detail : "";
      bRepair.textContent = rep.offer ? rep.label : "Re-detect and save";
      const broken = repairBrokenFileHint(state.status);
      if (broken) { repairBox.hidden = false; repairNote.textContent = broken; repairDetail.textContent = "Rename or fix the file, or use Locate... after moving it aside."; }
      bRepair.hidden = !rep.offer;
      if (filledFor !== state.status && set.ready) {
        filledFor = state.status;
        inServer.value = set.server.value; inWebDir.value = set.web.dir; inWebCmd.value = set.web.startCmd; inWebUrl.value = set.web.url;
        chkRemote.checked = set.network.allowRemote; selOpen.value = set.open.with;
      }
      bSiteWeb.disabled = !!state.busy || !set.ready || !set.network.siteUrl;
      bSaveOpen.disabled = !!state.busy || !set.ready;
      selOpen.disabled = bSaveOpen.disabled; chkRemote.disabled = bSaveOpen.disabled;
      const cache = stremioCacheModel(state.status);
      cacheText.textContent = cache.text;
      bCache.disabled = !!state.busy || !cache.canAsk;
      bCache.title = cache.canAsk ? "Choose a folder; the request is copied for you to send to Jarvis, which asks before it changes anything."
        : "Stremio's server must be running for Jarvis to move its cache.";
      bSaveServer.disabled = !!state.busy || !set.ready;
      bSaveWeb.disabled = !!state.busy || !set.ready;
      bStartWeb.disabled = !!state.busy || !set.ready || set.web.answers || !set.web.configured;
      bStartWeb.title = set.web.answers ? "stremio-web is already answering." : !set.web.thisPc ? "That address isn't on this PC, so Jarvis can't start it there."
        : !set.web.configured ? "Choose the stremio-web folder and save first." : "Start stremio-web with the saved command, in the saved folder.";
      paintExtra();
    }

    // account / player / addon rows: the boxes are filled once per hub answer; the lists are redrawn every time
    function paintExtra() {
      const acc = accountModel(state.status), pl = playerModel(state.status), ad = addonsModel(state.status);
      accountNote.textContent = acc.sentence;
      accountNote.className = "prefs-note" + (acc.ready ? "" : " prefs-note--warn");
      bLogin.disabled = !!state.busy || !acc.ready; bSaveKey.disabled = bLogin.disabled;
      bCheckAcc.disabled = !!state.busy || !acc.signedIn; bLogout.disabled = !!state.busy || !acc.signedIn;
      if (extraFor !== state.status && pl.ready) { extraFor = state.status; selPlayer.value = pl.kind; inPlayerExe.value = pl.exe; if (acc.email && !inEmail.value) inEmail.value = acc.email; }
      playerNote.textContent = pl.missing ? "The saved player program no longer exists. Pick it again or clear the box." : "";
      bSavePlayer.disabled = !!state.busy || !pl.ready; bBrowsePlayer.disabled = bSavePlayer.disabled; bAddAddon.disabled = !!state.busy || !ad.ready;
      addonList.replaceChildren(...(ad.rows.length ? ad.rows.map((r) => h("div", { class: "prefs-inline" }, [h("span", { text: r.host }),
        btn("Remove", () => runSave(addonsBody("", r.index), "Addon removed"), { disabled: !!state.busy })]))
        : [h("div", { class: "prefs-empty", text: ad.ready ? "No addons saved here. Your account's addons are used when you are signed in." : "Not available." })]));
    }
    // one account call; `clearBoxes` empties the password and key boxes afterwards, whatever the answer was
    async function accountDo(body, clearBoxes) {
      if (!body) { say("Fill the box first.", "err"); return; }
      lock("saving");
      say(body.action === "login" ? "Signing in\u2026" : "");
      try {
        const r = await hubPost("/api/hub/stremio/account", body, 45000);
        state.status = r; say(String(r.account_note || "Done"), "ok");
      } catch (err) { say(String((err && err.message) || "The hub didn't accept that."), "err"); }
      if (clearBoxes) { inPass.value = ""; inKey.value = ""; }
      lock("");
      paint();
    }
    async function signOut() {
      const ui2 = global.JarvisUI;
      const yes = !!(ui2 && typeof ui2.confirm === "function" && await ui2.confirm({ title: "Sign out of Stremio?", level: "warn", focusCancel: true, confirmLabel: "Sign out",
        body: "Jarvis forgets your Stremio login key and tells Stremio it is finished. Your Stremio account itself is not changed." }));
      if (yes) await accountDo({ action: "logout" }, true);
    }
    function savePlayer() { const pl = playerModel(state.status); return runSave(playerSaveBody({ kind: selPlayer.value, exe: inPlayerExe.value }, pl), "Player saved"); }
    async function browsePlayer() {
      const picker = global.JarvisPathPicker;
      if (!picker || typeof picker.open !== "function") { say("The file picker isn't available on this page.", "err"); return; }
      const picked = await picker.open({ kind: "file", start: inPlayerExe.value || "", title: "Choose mpv or VLC" });
      if (picked) inPlayerExe.value = String(picked);
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

    // one save through the paths route (the hub's sentence is shown as it is)
    async function runSave(body, okText) {
      lock("saving");
      say("");
      const out = await settingsSaveFlow({ save: (b) => hubPost("/api/hub/stremio/paths", b, 30000) }, body);
      lock("");
      if (out.status === "saved") { state.status = out.result; say(okText || "Saved", "ok"); }
      else if (out.status === "refused") say(out.error, "err");
      else say("Nothing changed.");
      paint();
    }
    function saveServer() {
      const set = stremioSetupModel(state.status);
      return runSave(mergeBodies(serverSaveBody(inServer.value, set.server.value), networkSaveBody(chkRemote.checked, set.network.allowRemote)), "Address saved");
    }
    function saveWeb() {
      const set = stremioSetupModel(state.status);
      return runSave(mergeBodies(webSaveBody({ url: inWebUrl.value, dir: inWebDir.value, startCmd: inWebCmd.value }, set.web),
        networkSaveBody(chkRemote.checked, set.network.allowRemote)), "stremio-web settings saved");
    }
    // Stremio's own hosted web app as the web address: fills the box and ticks the "other addresses" box; Save web settings stores both.
    function useSite() {
      const set = stremioSetupModel(state.status);
      if (!set.network.siteUrl) return;
      inWebUrl.value = set.network.siteUrl;
      chkRemote.checked = true;
      say("Filled in " + set.network.siteUrl + ". Press Save web settings to keep it.");
    }
    function saveOpen() {
      const set = stremioSetupModel(state.status);
      return runSave(openSaveBody(selOpen.value, set.open.with), selOpen.value === "native" ? "Links will open in the Stremio app" : "Links will open in the browser");
    }
    async function browseWeb() {
      const picker = global.JarvisPathPicker;
      if (!picker || typeof picker.open !== "function") { say("The folder picker isn't available on this page.", "err"); return; }
      const picked = await picker.open({ kind: "folder", start: inWebDir.value || "", title: "Choose the stremio-web folder" });
      if (picked) inWebDir.value = String(picked);
    }
    async function startWeb() {
      lock("starting");
      say("Starting stremio-web\u2026");
      const out = await startWebFlow({ start: (ms) => hubPost("/api/hub/stremio/start", { what: "web" }, ms) }, state.status);
      lock("");
      say(out.text, out.status === "ok" ? "ok" : "err");
      await load2();
    }
    async function repair() {
      lock("saving");
      say("");
      const ui2 = global.JarvisUI;
      const out = await repairFlow({
        confirm: async (text) => !!(ui2 && typeof ui2.confirm === "function" && await ui2.confirm({ title: "Replace stremio.json?", level: "warn", focusCancel: true, confirmLabel: "Keep a copy and save", body: text })),
        save: (b) => hubPost("/api/hub/stremio/paths", b, 30000),
      }, state.status);
      lock("");
      if (out.status === "saved") { state.status = out.result; say(out.result && out.result.kept_copy ? "Saved. Your old file is " + out.result.kept_copy : "Saved", "ok"); }
      else if (out.status === "refused") say(out.error, "err");
      paint();
    }
    async function askCache() {
      const picker = global.JarvisPathPicker;
      if (!picker || typeof picker.open !== "function") { say("The folder picker isn't available on this page.", "err"); return; }
      const cache = stremioCacheModel(state.status);
      const picked = await picker.open({ kind: "folder", start: cache.root || "", title: "Choose the new cache folder" });
      if (!picked) return;
      const text = cacheRequestText(picked);
      let ok = false;
      try { await global.navigator.clipboard.writeText(text); ok = true; } catch (_) { /* shown below instead */ }
      say(ok ? "Copied. Paste it into the chat: Jarvis changes the cache with its own tool and asks you first." : text, ok ? "ok" : "err");
    }

    paint();
    load();
    return ui.group("Stremio", [
      ui.rowShell("Stremio's server", "What Jarvis found, whether it runs, and how to point it at Stremio. Locate... saves the two file paths for Jarvis only; it never changes Stremio.",
        h("div", {}, [title, sentence, next, facts]), msg),
      h("div", { class: "prefs-inline" }, [bCheck, bCopy, bProg, bJs, bStart]),
      ui.rowShell("Stremio's settings file", "Jarvis keeps what it knows about Stremio in a small file. If it is missing or out of date, one button repairs it.", repairBox, null),
      ui.rowShell("Allow addresses other than this PC", "Off: the server and stremio-web addresses must be http on 127.0.0.1 or localhost. On: http or https on any host is accepted (a Tailscale or network address, a name, Stremio's own website). Jarvis may then talk to that address, so turn it on only for hosts you trust. It is saved together with the address button you press.",
        chkRemote, null),
      ui.rowShell("Server address", "Where Stremio's server answers. A server must answer there before it is saved. Without the box above ticked only this PC (127.0.0.1 or localhost) is accepted.",
        h("div", { class: "prefs-inline" }, [inServer, bSaveServer]), null),
      ui.rowShell("stremio-web folder", "The folder Jarvis starts stremio-web from. Empty = not used.", h("div", { class: "prefs-inline" }, [inWebDir, bBrowseWeb]), null),
      ui.rowShell("stremio-web start command", "One program and its arguments, run in that folder. Empty = pnpm start.", inWebCmd, null),
      ui.rowShell("stremio-web address", "Where stremio-web answers, e.g. http://127.0.0.1:8080 or https://100.72.25.9:8080. Other addresses than this PC need the box above. Jarvis only starts stremio-web for an address on this PC.",
        h("div", { class: "prefs-inline" }, [inWebUrl, bSiteWeb]), null),
      h("div", { class: "prefs-inline" }, [bSaveWeb, bStartWeb]),
      ui.rowShell("Open Stremio links in", "Where a show's page or a stream opens when Jarvis plays or opens something in Stremio. The Stremio app needs to be installed; then no stremio-web is needed at all.",
        h("div", { class: "prefs-inline" }, [selOpen, bSaveOpen]), null),
      ui.rowShell("Stremio account", "Sign in so Jarvis can read your installed addons and library. The password is used once to get a login key and is never saved; only the key is kept, in Jarvis's own stremio.json, and this page never shows it.",
        h("div", {}, [accountNote, h("div", { class: "prefs-inline" }, [inEmail, inPass, bLogin]), h("div", { class: "prefs-inline" }, [inKey, bSaveKey]), h("div", { class: "prefs-inline" }, [bCheckAcc, bLogout])]), null),
      ui.rowShell("Video player", "Which player Jarvis uses for a finished file or a stream. Automatic looks for mpv, then VLC.",
        h("div", {}, [h("div", { class: "prefs-inline" }, [selPlayer, inPlayerExe, bBrowsePlayer, bSavePlayer]), playerNote]), null),
      ui.rowShell("Extra addons", "Addon addresses Jarvis uses besides your account's. Only the site is shown here, because an address can contain a private key.",
        h("div", {}, [addonList, h("div", { class: "prefs-inline" }, [inAddon, bAddAddon])]), null),
      ui.rowShell("Stremio's cache", "Where Stremio keeps what it is streaming. Changing it is a request to Jarvis, which asks before it changes the server.",
        h("div", {}, [cacheText, bCache]), null),
      h("details", {}, [h("summary", { text: "Diagnostic text" }), diag]),
    ]);
  }

  // ---- the page (DOM) -------------------------------------------------------------------------------------------------------
  // build() returns ONE element: a search bar, a rail of sections, and the sections.  Every row still goes through ctx.ui (the row
  // helpers settings.js hands over) and writes through dl_prefs / dl_settings / dl_provider_prefs; this file only decides what
  // goes where, what each row says about itself, and what the search and "Changed from default" hide.  The finished page is
  // reachable as `element.__dls` (rows, setQuery, setChangedOnly, visible ...) so a test can drive it without a browser.

  const PAGE_CSS = [
    ".dls{display:grid;grid-template-columns:168px minmax(0,1fr);gap:4px 18px;align-items:start}",
    ".dls-tools{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:center;gap:10px;position:sticky;top:-14px;z-index:3;padding:8px 0;background:var(--bg-0,var(--bg-1));border-bottom:1px solid var(--border)}",
    ".dls-tools .dls-search{width:280px;max-width:100%}.dls-count{font-size:11.5px;color:var(--text-dim)}",
    ".dls-rail{position:sticky;top:48px;display:flex;flex-direction:column;gap:2px}",
    ".dls-rail__item{display:flex;justify-content:space-between;gap:8px;text-align:left;padding:5px 8px;border:0;border-radius:var(--radius);background:none;color:var(--text-dim);font:inherit;font-size:12.5px;cursor:pointer}",
    ".dls-rail__item:hover,.dls-rail__item.is-active{background:var(--bg-1);color:var(--text)}.dls-rail__n{color:var(--text-dimmer);font-size:11px}",
    ".dls-main{min-width:0}.dls-section{margin:0 0 22px}.dls-section>.dls-lede{margin:0 0 8px;font-size:11.5px;color:var(--text-dim)}",
    ".dls [hidden]{display:none!important}.dls-locked{color:var(--text-dim)}",
    ".dls-meta{display:flex;flex-wrap:wrap;align-items:center;gap:2px 12px;margin-top:3px;font-size:11px;color:var(--text-dimmer)}",
    ".dls-reset{padding:0;border:0;background:none;color:var(--accent);font:inherit;font-size:11px;text-decoration:underline;cursor:pointer}",
    ".dls-source{margin:0 0 8px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-1)}",
    ".dls-source>summary{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;font-size:13px;color:var(--text);list-style:none}",
    ".dls-source>summary::-webkit-details-marker{display:none}.dls-source>summary::before{content:'\\25B8';color:var(--text-dimmer)}",
    ".dls-source[open]>summary::before{content:'\\25BE'}.dls-source__state{margin-left:auto;font-size:11.5px;color:var(--text-dim)}",
    ".dls-source__body{padding:0 12px 10px}.dls-dot{width:9px;height:9px;border-radius:50%;background:var(--text-dimmer);flex:none}",
    ".dls-dot--ok{background:var(--green,#3fb950)}.dls-dot--warn{background:var(--gold,#d29922)}.dls-dot--bad{background:var(--red,#f85149)}",
    ".dls-table{width:100%;border-collapse:collapse;margin:4px 0 8px}.dls-table th{font-size:11px;font-weight:400;text-align:left;color:var(--text-dimmer);padding:2px 6px 4px 0}",
    ".dls-table td{padding:3px 6px 3px 0;vertical-align:top}.dls-table .prefs-input,.dls-table .prefs-select{width:100%;box-sizing:border-box}",
    ".dls-table__err td{padding:0 0 6px;font-size:11.5px;color:var(--red)}.dls-none{padding:18px 0;color:var(--text-dim);font-size:13px}",
    "@media (max-width:760px){.dls{grid-template-columns:1fr}.dls-rail{position:static;flex-direction:row;flex-wrap:wrap}.dls-tools{position:static}}",
  ].join("\n");
  function injectStyle() {
    if (typeof document === "undefined" || !document.head || document.getElementById("dls-style")) return;
    const st = document.createElement("style");
    st.id = "dls-style";
    st.textContent = PAGE_CSS;
    document.head.appendChild(st);
  }

  function build(ctx) {
    return Promise.all([getJson("/api/hub/settings"), getJson("/api/hub/sources").catch(() => null)])
      .then(([payload, sources]) => [makePage(ctx, payload, sources)]);
  }

  // ---- the qBittorrent connection card (DOM) ---------------------------------------------------------------------------------
  // The top of the qBittorrent block under Sources: the switch, the Web UI address, the user name and the password.  Each row
  // saves as soon as it changes, like the settings rows under it; the password has its own box and buttons and is never drawn
  // back.  The behaviour options (category, tag, where it saves ...) are the ordinary settings rows below this card.
  function qbittorrentCard(ctx) {
    const ui = ctx.ui;
    const h = ui.h;
    const ROUTE = "/api/hub/qbittorrent";
    const note = h("div", { class: "prefs-note", text: "Reading qBittorrent's connection\u2026" });
    const where = h("div", { class: "prefs-row__help" });
    const overrideNote = h("div", { class: "prefs-note prefs-note--warn", hidden: true });
    const releaseStatus = h("span", { class: "prefs-row__status" });
    const bRelease = h("button", { class: "btn btn--sm", type: "button", text: "Let the rows below decide", onclick: async () => {
      try { applyStatus(await hubPost(ROUTE, qbtReleaseBody(), 30000)); saved(releaseStatus); }
      catch (err) { ui.flash(releaseStatus, err.message, "err"); }
    } });
    const releaseBox = h("div", { class: "prefs-inline", hidden: true }, [bRelease, releaseStatus]);
    const rows = h("div", {});
    const pwNote = h("div", { class: "prefs-row__help" });
    const bCheck = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Check again", onclick: () => load() });
    let drawn = false;

    function saved(status) { ui.flash(status, "Saved", "ok"); setTimeout(() => ui.flash(status, ""), 1800); }

    // the sentence, the file and the notes, from any answer; the rows are left alone so nothing being typed is lost
    function applyStatus(s) {
      const m = qbtModel(s);
      note.textContent = m.sentence;
      note.className = "prefs-note" + (m.tone === "bad" ? " prefs-note--warn" : "");
      where.textContent = m.loaded ? "Connection file: " + m.file + (m.exists ? "" : " (created when you first save)") + (m.problem && !m.sentence.includes(m.problem) ? ". " + m.problem : "") : "";
      const text = qbtOverrideText(m.overrides);
      overrideNote.textContent = text;
      overrideNote.hidden = !text;
      releaseBox.hidden = !text;
      pwNote.textContent = m.passwordSet ? "A password is saved." : "No password is saved (leave it so if qBittorrent asks for none).";
      return m;
    }

    function field(spec, value) {
      const status = h("span", { class: "prefs-row__status" });
      const ctl = ui.makeControl({ type: spec.type }, value, async (next) => {
        applyStatus(await hubPost(ROUTE, qbtBody(spec.key, next), 30000));
        saved(status);
      }, status);
      return ui.rowShell(spec.label, spec.help, ctl.el, status);
    }

    function passwordRow() {
      const status = h("span", { class: "prefs-row__status" });
      const box = h("input", { type: "password", class: "prefs-input prefs-input--wide", autocomplete: "new-password", spellcheck: "false",
        "aria-label": "qBittorrent password", placeholder: "Type the Web UI password" });
      const send = async (body) => {
        try { applyStatus(await hubPost(ROUTE, body, 30000)); box.value = ""; saved(status); }
        catch (err) { ui.flash(status, err.message, "err"); }
      };
      const save = h("button", { class: "btn btn--sm", type: "button", text: "Save password", onclick: () => {
        const body = qbtPasswordBody(box.value);
        if (!body) { ui.flash(status, "Type the password first.", "err"); return; }
        send(body);
      } });
      const forget = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Forget it", onclick: () => send(qbtForgetBody()) });
      return ui.rowShell("Password", "Kept only in qBittorrent's connection file on this PC, and never shown again, so the box stays empty.",
        h("div", {}, [h("div", { class: "prefs-inline" }, [box, save, forget]), pwNote]), status);
    }

    function draw(m) {
      const v = m.values;
      rows.replaceChildren(
        field({ key: "enabled", type: "bool", label: "Use qBittorrent",
          help: "Off = Jarvis doesn't use qBittorrent at all, even while it is running." }, v.enabled !== false),
        field({ key: "address", type: "text", label: "Web UI address",
          help: "Where qBittorrent's Web UI answers. Only this PC is accepted (127.0.0.1 or localhost). Default http://127.0.0.1:8080." }, v.address || ""),
        field({ key: "username", type: "text", label: "User name",
          help: "Leave empty if qBittorrent has \"Bypass authentication for clients on localhost\" switched on." }, v.username || ""),
        passwordRow());
      drawn = true;
    }

    async function load() {
      bCheck.disabled = true;
      let s;
      try { s = await getJson(ROUTE); }
      catch (err) { s = { ok: false, error: "Couldn't read qBittorrent's connection settings: " + err.message }; }
      bCheck.disabled = false;
      const m = applyStatus(s);
      if (m.loaded && !drawn) draw(m);
    }

    load();
    return ui.group("qBittorrent connection", [
      ui.rowShell("Connection", "qBittorrent's Web UI must be switched on (Tools > Options > Web UI) and kept on this PC. Jarvis asks it to download and watches the torrents it adds.",
        h("div", {}, [note, where, overrideNote]), null),
      rows,
      releaseBox,
      h("div", { class: "prefs-inline" }, [bCheck]),
    ]);
  }

  function makePage(ctx, payload, sources) {
    injectStyle();
    const ui = ctx.ui;
    const h = ui.h;
    if (!payload.settings || typeof payload.settings !== "object") payload.settings = {};
    const writable = new Set(payload.writable || []);
    const entries = [];                 // everything the search and the toggle can hide: settings, tables, the Stremio card, provider rows
    const sourceBlocks = [];            // { id, label, node, body, details, userOpen }
    const state = { query: "", changedOnly: false };

    function savedFlash(status) { ui.flash(status, "Saved", "ok"); setTimeout(() => ui.flash(status, ""), 1800); }

    // ---- one setting
    function addEntry(e) { entries.push(e); return e; }

    function paintMeta(e) {
      if (!e.meta) return;
      const info = e.info();
      const kids = [];
      if (info.defaultText) kids.push(h("span", { class: "dls-default", text: info.defaultText }));
      if (info.differs) kids.push(h("button", { class: "dls-reset", type: "button", text: "Reset", "aria-label": "Reset " + e.label + " to its default", onclick: () => e.reset() }));
      if (info.timing) kids.push(h("span", { class: "dls-timing", text: info.timing }));
      e.meta.replaceChildren(...kids);
      if (e.node && typeof e.node.setAttribute === "function") e.node.setAttribute("data-changed", info.differs ? "1" : "0");
    }

    // History retention: when the change would remove rows, say so, count them when the History list can, and ask.  Nothing is
    // saved before the answer; "no" puts the old number back (the row shows the refusal).
    async function countOlderThan(days) {
      try {
        const body = await getJson("/api/hub/history?limit=1&until=" + retentionCutoff(Date.now(), days));
        if (!body || body.summary_truncated || !body.summary || typeof body.summary.count !== "number") return null;
        return body.summary.count;
      } catch (_) { return null; }
    }
    async function confirmRetention(current, next) {
      if (!retentionNeedsConfirm(current, next)) return;
      const words = retentionWarning(Number(next), await countOlderThan(Number(next)));
      const dialog = global.JarvisUI;
      const ok = dialog && typeof dialog.confirm === "function"
        ? await dialog.confirm({ title: words.title, level: "warn", focusCancel: true, confirmLabel: "Remove old rows", body: words.body })
        : false;
      if (!ok) throw new Error("Not changed: you declined.");
    }

    // A key ~/.jarvis/qbittorrent.json also sets: the file wins, so this row only SHOWS what is saved here and says so.  No control,
    // no Reset, no default line: nothing on this page can change what is in force.
    function lockedRow(spec, where, file) {
      const status = h("span", { class: "prefs-row__status" });
      const help = (spec.help ? spec.help + " " : "") + OVERRIDE_TEXT;
      const shown = formatValue(spec, getPath(payload.settings, spec.path), payload);
      const ctlEl = h("span", { class: "prefs-note dls-locked", "data-readonly": "1", text: shown === "" ? "not set" : shown });
      const e = { kind: "setting", path: spec.path, label: spec.label, help: help, section: where.section, source: where.source, spec: spec,
        status: status, readonly: true, overriddenBy: file };
      e.node = ui.rowShell(spec.label, help, ctlEl, status);
      e.info = () => ({ hasDefault: false, differs: false, defaultText: "", timing: "" });
      e.reset = async () => {};
      return addEntry(e);
    }

    function settingRow(spec, where) {
      const file = overrideOf(payload, spec.path);
      if (file) return lockedRow(spec, where, file);
      const status = h("span", { class: "prefs-row__status" });
      const toCtl = spec.toCtl || ((v) => (spec.type === "hosts" ? (v || []).join(", ") : v));
      const fromCtl = spec.fromCtl || ((v) => (spec.type === "hosts" ? textToHosts(v) : v));
      let control = Object.assign({}, spec);
      if (spec.type === "choice" && !spec.choices) control.choices = (payload.open_modes || []).map((m) => [m, OPEN_LABELS[m] || m]);
      if (spec.type === "hosts") control.type = "text";
      const e = { kind: "setting", path: spec.path, label: spec.label, help: spec.help || "", section: where.section, source: where.source, spec: spec, status: status };
      const commit = async (next) => {
        const sent = fromCtl(next);
        if (spec.path === RETENTION_PATH) await confirmRetention(getPath(payload.settings, spec.path), sent);
        await writeSetting(spec.path, sent, payload);
        setPath(payload.settings, spec.path, sent);
        savedFlash(status);
        paintMeta(e);
        applyFilter();
      };
      const ctl = ui.makeControl(control, toCtl(getPath(payload.settings, spec.path)), commit, status);
      e.meta = h("div", { class: "dls-meta" });
      e.node = ui.rowShell(spec.label, spec.help || "", ctl.el, status, e.meta);
      e.info = () => rowInfo(spec, payload, getPath(payload.settings, spec.path));
      e.reset = async () => {
        const def = defaultOf(payload, spec.path);
        if (def === undefined || !e.info().differs) return;
        try {
          if (spec.path === RETENTION_PATH) await confirmRetention(getPath(payload.settings, spec.path), def);
          await writeSetting(spec.path, def, payload);
          setPath(payload.settings, spec.path, def);
          ctl.set(toCtl(def));
          savedFlash(status);
        } catch (err) { ui.flash(status, err.message, "err"); }
        paintMeta(e);
        applyFilter();
      };
      paintMeta(e);
      return addEntry(e);
    }

    // ---- a row that is not a hub setting (a program's own switch, its update size, its check interval): no default, no timing
    function plainRow(label, help, ctlEl, status, where, text) {
      const node = ui.rowShell(label, help, ctlEl, status);
      return addEntry({ kind: "plain", path: "", label: label, help: help || "", section: where.section, source: where.source, node: node, text: text || "", info: () => ({ differs: false }) });
    }

    // ---- the two list editors, as tables
    function tableEditor(opts) {
      // opts: { path, kind, title, help, model, columns:[{key,label,kind,choices?}], blank(), save(model), addLabel, max, empty }
      const model = opts.model;
      const errors = {};
      const status = h("span", { class: "prefs-row__status" });
      const body = h("tbody");
      const table = h("table", { class: "dls-table" }, [
        h("thead", {}, [h("tr", {}, opts.columns.map((c) => h("th", { scope: "col", text: c.label })).concat([h("th", { scope: "col", text: "" })]))]),
        body,
      ]);
      const emptyNote = h("div", { class: "prefs-empty", text: opts.empty || "None yet." });
      function paint() {
        const trs = [];
        model.forEach((row, i) => {
          const cells = opts.columns.map((c) => {
            let input;
            if (c.kind === "select") {
              input = h("select", { class: "prefs-select", "aria-label": c.label + ", row " + (i + 1) });
              c.choices(row).forEach(([v, label]) => input.append(h("option", { value: String(v), text: label })));
              input.value = String(row[c.key]);
            } else {
              input = h("input", { type: c.kind === "number" ? "number" : "text", class: "prefs-input", "aria-label": c.label + ", row " + (i + 1), spellcheck: "false" });
              if (c.kind === "number") input.min = "0";
              input.value = row[c.key] === undefined || row[c.key] === null ? "" : String(row[c.key]);
            }
            input.addEventListener("change", () => { row[c.key] = c.kind === "number" ? Number(input.value) : input.value; delete errors[i]; paint(); });
            return h("td", {}, [input]);
          });
          trs.push(h("tr", { "data-row": String(i) }, cells.concat([h("td", {}, [h("button", { class: "btn btn--ghost btn--sm", type: "button", text: "Remove", "aria-label": "Remove row " + (i + 1),
            onclick: () => { model.splice(i, 1); Object.keys(errors).forEach((k) => delete errors[k]); paint(); } })])])));
          if (errors[i]) trs.push(h("tr", { class: "dls-table__err", role: "alert" }, [h("td", { colspan: String(opts.columns.length + 1), text: errors[i] })]));
        });
        body.replaceChildren(...trs);
        e.view = trs;                       // the rows as drawn (a test reads these; the page never does)
        table.hidden = !model.length;
        emptyNote.hidden = !!model.length;
      }
      const spec = { path: opts.path, type: "list" };
      const e = { kind: "table", view: [], path: opts.path, label: opts.title, help: opts.help, section: opts.section, source: null, spec: spec, status: status };
      paint();
      e.meta = h("div", { class: "dls-meta" });
      e.info = () => rowInfo(spec, payload, getPath(payload.settings, opts.path));
      e.reset = async () => {
        const def = defaultOf(payload, opts.path);
        if (def === undefined || !e.info().differs) return;
        try {
          await writeSetting(opts.path, def, payload);
          setPath(payload.settings, opts.path, def);
          model.splice(0, model.length, ...opts.toModel(def));
          Object.keys(errors).forEach((k) => delete errors[k]);
          paint();
          savedFlash(status);
        } catch (err) { ui.flash(status, err.message, "err"); }
        paintMeta(e);
        applyFilter();
      };
      const add = h("button", { class: "btn btn--ghost btn--sm", type: "button", text: opts.addLabel, onclick: () => { if (model.length < (opts.max || 50)) { model.push(opts.blank()); paint(); } } });
      const save = h("button", { class: "btn btn--sm", type: "button", text: "Save", onclick: async () => {
        save.disabled = true;
        ui.flash(status, "");
        Object.keys(errors).forEach((k) => delete errors[k]);
        try {
          const written = opts.toWritten(model);
          await writeSetting(opts.path, written, payload);
          setPath(payload.settings, opts.path, written);
          paint();
          ui.flash(status, "Saved", "ok");
          paintMeta(e);
          applyFilter();
        } catch (err) {
          const at = rowOfError(opts.kind, err.message, model);
          if (at >= 0) errors[at] = err.message;
          paint();
          ui.flash(status, err.message, "err");     // the hub's own words, whole, whether or not one row could be named
        }
        save.disabled = false;
      } });
      e.node = h("div", { class: "prefs-group-table", "data-search": (opts.title + " " + opts.help).toLowerCase() }, [
        h("div", { class: "prefs-row__label", text: opts.title }),
        h("div", { class: "prefs-row__help", text: opts.help }),
        e.meta, table, emptyNote,
        h("div", { class: "prefs-inline" }, [add, save, status]),
      ]);
      paintMeta(e);
      return addEntry(e);
    }

    function scheduleTable(where) {
      const dayChoices = (w) => [["all", "Every day"], ["weekdays", "Weekdays"], ["weekends", "Weekends"]]
        .concat(["all", "weekdays", "weekends"].indexOf(w.days) === -1 ? [[w.days, "Chosen days (" + w.days + ")"]] : []);
      return tableEditor({
        path: "schedule", kind: "schedule", title: "Speed windows", section: where.section,
        help: "Inside a window its limit replaces the speed limit above (0 = unlimited there). The first window that matches wins; a window may cross midnight.",
        model: windowsToModel(getPath(payload.settings, "schedule")),
        columns: [
          { key: "days", label: "Days", kind: "select", choices: dayChoices },
          { key: "from", label: "From (hh:mm)", kind: "text" },
          { key: "to", label: "To (hh:mm)", kind: "text" },
          { key: "rate_limit_kbps", label: "Limit (KB/s)", kind: "number" },
        ],
        blank: blankWindow, addLabel: "Add a window", max: 10, empty: "No windows: the speed limit above always applies.",
        toModel: windowsToModel, toWritten: modelToWindows,
      });
    }

    function extTable(where) {
      return tableEditor({
        path: "ext_folders", kind: "ext", title: "Folder per file type", section: where.section,
        help: "Send a kind of file somewhere of its own, e.g. .iso to ISOs. A folder name is inside the download folder; a longer ending (.tar.gz) wins over a shorter one.",
        model: extToModel(getPath(payload.settings, "ext_folders")),
        columns: [{ key: "ext", label: ".ext", kind: "text" }, { key: "folder", label: "Folder", kind: "text" }],
        blank: () => ({ ext: "", folder: "" }), addLabel: "Add a file type", max: 50, empty: "None: files go by kind (video, audio, other).",
        toModel: extToModel, toWritten: modelToExt,
      });
    }

    // ---- which rows exist, and where they go
    const rowSpecs = planRows(payload).reduce((all, g) => all.concat(g.rows), []);
    const bySection = {};
    PAGE_SECTIONS.concat([OTHER_SECTION]).forEach((s) => { bySection[s.id] = { def: s, children: [], settings: [] }; });
    const bySource = {};
    const rowSources = [];
    rowSpecs.forEach((spec) => {
      const where = placeOf(spec.path);
      if (where.source) { (bySource[where.source] = bySource[where.source] || []).push(spec); if (rowSources.indexOf(where.source) === -1) rowSources.push(where.source); }
      else bySection[where.section].settings.push(spec);
    });

    PAGE_SECTIONS.concat([OTHER_SECTION]).forEach((s) => {
      const sec = bySection[s.id];
      sec.settings.forEach((spec) => sec.children.push(settingRow(spec, { section: s.id, source: null }).node));
    });

    // files and speed tables sit with their section's rows
    if (writable.has("ext_folders")) bySection.files.children.push(extTable({ section: "files" }).node);
    if (writable.has("schedule")) bySection.speed.children.push(scheduleTable({ section: "speed" }).node);

    // which events notify, and how important each is
    const N = global.JarvisNotifications;
    const levels = N && typeof N.pickerLevels === "function" ? N.pickerLevels() : { items: [] };
    (payload.events || []).forEach((ev) => {
      if (writable.has("notify." + ev + ".enabled")) {
        bySection.notifications.children.push(settingRow({ path: "notify." + ev + ".enabled", label: EVENT_LABELS[ev] || ev, type: "bool", help: "" },
          { section: "notifications", source: null }).node);
      }
      if (writable.has("notify." + ev + ".level") && levels.items.length) {
        const choices = [["default", "Follow Settings > Notifications"]].concat(levels.items.map((l) => [l.level, l.label]));
        bySection.notifications.children.push(settingRow({ path: "notify." + ev + ".level", label: (EVENT_LABELS[ev] || ev) + ": how important", type: "choice", choices: choices, help: "",
          toCtl: (v) => (v === null || v === undefined ? "default" : v), fromCtl: (v) => (v === "default" ? null : v) }, { section: "notifications", source: null }).node);
      }
    });

    // general rows of the Sources section come first, then one collapsed block per source
    const srcSec = bySection.sources;
    const srcChildren = srcSec.children.slice();
    const plan = planSources(payload, sources, rowSources);

    function sourceBlock(src) {
      const specs = src.kind === "engine" ? (bySource[src.id] || []) : [];
      const kids = [];
      const mark = { section: "sources", source: src.id, label: src.label };
      if (src.id === "qbittorrent") {
        const card = qbittorrentCard(ctx);
        kids.push(card);
        addEntry({ kind: "card", path: "", label: "qBittorrent connection", help: "", section: "sources", source: "qbittorrent", node: card, info: () => ({ differs: false }),
          text: "qbittorrent connection web ui address user name username password switch on off enabled torrent settings file" });
      }
      specs.forEach((spec) => kids.push(settingRow(spec, mark).node));
      if (src.id === "stremio") {
        const card = stremioBlock(ctx);
        kids.push(card);
        addEntry({ kind: "card", path: "", label: "Stremio server", help: "", section: "sources", source: "stremio", node: card, info: () => ({ differs: false }),
          text: "stremio server web folder start command address cache torrent settings file repair diagnostic locate" });
      }
      if (src.kind === "provider") {
        const p = src.provider;
        const onStatus = h("span", { class: "prefs-row__status" });
        const on = ui.makeControl({ type: "bool" }, p.enabled === true, async (v) => {
          const r = await runHubTool(PROVIDER_TOOL, { id: p.id, enabled: v });
          if (r && r.ok === false) throw new Error(r.error);
          ui.flash(onStatus, "Saved", "ok");
        }, onStatus);
        kids.push(plainRow(p.label || p.id, p.read_only ? "Shows its downloads here; has no controls." : "", on.el, onStatus, mark, "switch on off enabled").node);
        providerSettingRows(p).forEach((f) => {
          const stStatus = h("span", { class: "prefs-row__status" });
          const ctl = ui.makeControl({ type: f.type, min: f.min, unit: f.unit }, f.value, async (v) => {
            await runHubTool(PROVIDER_TOOL, providerSettingArgs(p.id, f.key, v));       // the hub's own error text reaches the row as it is
            ui.flash(stStatus, "Saved", "ok");
          }, stStatus);
          kids.push(plainRow(f.label, f.help, ctl.el, stStatus, mark, "").node);
        });
        if (typeof p.interval_s === "number") {
          const ivStatus = h("span", { class: "prefs-row__status" });
          const iv = ui.makeControl({ type: "float", min: 0.5, unit: "seconds" }, p.interval_s, async (v) => {
            await runHubTool(PROVIDER_TOOL, { id: p.id, interval_s: v });
            ui.flash(ivStatus, "Saved", "ok");
          }, ivStatus);
          kids.push(plainRow((p.label || p.id) + ": check every", "", iv.el, ivStatus, mark, "").node);
        }
      }
      const count = kids.length;       // 0 = nothing to set for this source
      if (!count) {
        kids.push(h("div", { class: "prefs-note", text: "This source has no settings here." + (src.config_file ? " It is set up in " + src.config_file + "." : "") }));
      }
      const body = h("div", { class: "dls-source__body" }, kids);
      const summary = h("summary", {}, [
        h("span", { class: "dls-dot dls-dot--" + src.tone, "aria-hidden": "true" }),
        h("span", { text: src.label }),
        h("span", { class: "dls-source__state", text: [src.word, count ? "" : "no settings"].filter(Boolean).join(" \u00b7 ") }),
      ]);
      const details = h("details", { class: "dls-source", "data-settings-anchor": src.anchor }, [summary, body]);
      const block = { id: src.id, label: src.label, node: details, details: details, userOpen: false };
      details.addEventListener("toggle", () => { if (!state.query && !state.changedOnly) block.userOpen = !!details.open; });
      // The Sources tab's Configure button scrolls to this node (settings.js scrollToAnchor); a closed block opens first, so the
      // person lands on the settings and not on a folded heading.
      const scroll = details.scrollIntoView;
      details.scrollIntoView = function (o) { details.open = true; block.userOpen = true; if (typeof scroll === "function") scroll.call(details, o); };
      sourceBlocks.push(block);
      return block;
    }

    const engineBlocks = plan.filter((s) => s.kind === "engine").map(sourceBlock);
    const providerBlocks = plan.filter((s) => s.kind === "provider").map(sourceBlock);
    srcSec.children = srcChildren.concat(engineBlocks.map((b) => b.node));
    if (providerBlocks.length) {
      const wrap = h("div", { "data-settings-anchor": "other-tools-downloads" }, [h("h4", { class: "dls-sub", text: "Other tools' downloads" })].concat(providerBlocks.map((b) => b.node)));
      srcSec.children.push(wrap);
    }

    // ---- sections and rail
    const secNodes = {};
    const railItems = {};
    const railKids = [];
    const mainKids = [];
    PAGE_SECTIONS.concat([OTHER_SECTION]).forEach((s) => {
      const sec = bySection[s.id];
      if (!sec.children.length) return;
      const node = h("section", { class: "prefs-group dls-section", id: "dls-" + s.id, "data-settings-anchor": anchorOf(s.title), "data-section": s.id },
        [h("h3", { text: s.title }), h("div", { class: "dls-lede", text: s.lede })].concat(sec.children));
      secNodes[s.id] = node;
      const n = h("span", { class: "dls-rail__n", text: "" });
      const item = h("button", { class: "dls-rail__item", type: "button", "data-target": s.id, onclick: () => {
        Object.keys(railItems).forEach((k) => railItems[k].btn.classList.remove("is-active"));
        item.classList.add("is-active");
        if (typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "start", behavior: "auto" });
      } }, [h("span", { text: s.title }), n]);
      railItems[s.id] = { btn: item, n: n, title: s.title };
      railKids.push(item);
      mainKids.push(node);
    });
    const none = h("div", { class: "dls-none", text: "No setting matches that.", hidden: true });
    mainKids.push(none);
    const rail = h("nav", { class: "dls-rail", "aria-label": "Download settings sections" }, railKids);
    const main = h("div", { class: "dls-main" }, mainKids);

    // ---- search and "Changed from default"
    const search = h("input", { type: "search", class: "prefs-input dls-search", placeholder: "Search download settings\u2026", "aria-label": "Search download settings", spellcheck: "false", autocomplete: "off" });
    const changedBox = h("input", { type: "checkbox", id: "dls-changed" });
    const changedLabel = h("label", { class: "check", for: "dls-changed" }, [changedBox, h("span", { text: "Changed from default" })]);
    const countEl = h("span", { class: "dls-count", "aria-live": "polite", text: "" });
    const tools = h("div", { class: "dls-tools" }, [search, changedLabel, countEl]);

    function entryText(e) {
      const block = e.source ? sourceBlocks.find((b) => b.id === e.source) : null;
      return [e.label, e.help, e.path, e.text || "", block ? block.label : ""].join(" ");
    }
    function entryVisible(e) {
      if (state.changedOnly && !(e.info && e.info().differs)) return false;
      return matchQuery(entryText(e), state.query);
    }
    function applyFilter() {
      const filtering = !!state.query.trim() || state.changedOnly;
      const perSection = {};
      let shown = 0;
      let total = 0;
      entries.forEach((e) => {
        const vis = entryVisible(e);
        e.visible = vis;
        if (e.node) e.node.hidden = !vis;
        total++;
        if (vis) { shown++; perSection[e.section] = (perSection[e.section] || 0) + 1; }
      });
      sourceBlocks.forEach((b) => {
        const inside = entries.filter((e) => e.source === b.id && e.section === "sources");
        const any = inside.some((e) => e.visible);
        const labelHit = !state.changedOnly && matchQuery(b.label, state.query);
        const vis = !filtering || any || (labelHit && !inside.length);
        b.node.hidden = !vis;
        b.details.open = filtering ? vis && (any || labelHit) : b.userOpen;
      });
      Object.keys(secNodes).forEach((id) => {
        const blockShown = id === "sources" && sourceBlocks.some((b) => !b.node.hidden);
        secNodes[id].hidden = filtering && !(perSection[id] > 0) && !blockShown;
        const c = perSection[id] || 0;
        railItems[id].n.textContent = filtering && c ? String(c) : "";
      });
      none.hidden = !(filtering && shown === 0 && !sourceBlocks.some((b) => !b.node.hidden));
      countEl.textContent = filtering ? shown + " of " + total + " settings shown" : "";
    }
    search.addEventListener("input", () => { state.query = search.value; applyFilter(); });
    changedBox.addEventListener("change", () => { state.changedOnly = !!changedBox.checked; applyFilter(); });
    applyFilter();

    const el = h("div", { class: "dls" }, [tools, rail, main]);
    el.__dls = {
      entries: entries, sourceBlocks: sourceBlocks, sections: secNodes,
      setQuery: (q) => { state.query = String(q || ""); applyFilter(); },
      setChangedOnly: (on) => { state.changedOnly = !!on; applyFilter(); },
      visiblePaths: () => entries.filter((e) => e.visible && e.path).map((e) => e.path),
      visibleLabels: () => entries.filter((e) => e.visible).map((e) => e.label),
      sectionIds: () => Object.keys(secNodes),
      rowFor: (path) => entries.find((e) => e.path === path) || null,
      state: state,
    };
    return el;
  }

  const section = {
    id: "downloads", label: "Downloads", title: "Downloads",
    lede: "Where downloads go, how fast and how many at once, what happens when one finishes, and which events notify you. The Downloads panel (Menu > Downloads) shows and controls the downloads themselves.",
    terms: "downloads download hub folder library speed limit bandwidth schedule window concurrency stremio yt-dlp video audio torrent steam provider notify notification finished after retry stall private host extension steam update size threshold gb unknown lost give up settle hours cancelled playnite stremio server start locate diagnostic qbittorrent web ui address user name password category tag magnet default reset changed from default rail section search parallel connections segments torrent metadata timeout speed windows folder per file type",
    build: build,
  };

  global.JarvisSettingsSections = (global.JarvisSettingsSections || []).concat([section]);
  global.JarvisDownloadsSettings = { _pure: {
    FIELDS: FIELDS, planRows: planRows, extraRows: extraRows, toolFor: toolFor, getPath: getPath, hostsToText: hostsToText,
    textToHosts: textToHosts, anchorOf: anchorOf, daysChoice: daysChoice, windowsToModel: windowsToModel, modelToWindows: modelToWindows,
    extToModel: extToModel, modelToExt: modelToExt, isEventPath: isEventPath,
    PROVIDER_SETTING_FIELDS: PROVIDER_SETTING_FIELDS, providerSettingRows: providerSettingRows, providerSettingArgs: providerSettingArgs,
    overrideOf: overrideOf, retentionNeedsConfirm: retentionNeedsConfirm, retentionCutoff: retentionCutoff, retentionWarning: retentionWarning,
    OVERRIDE_TEXT: OVERRIDE_TEXT, RETENTION_PATH: RETENTION_PATH,
    stremioModel: stremioModel, stremioFacts: stremioFacts, diagnosticFor: diagnosticFor, folderOf: folderOf, pickOptions: pickOptions,
    locateFlow: locateFlow, startFlow: startFlow, startTimeoutMs: startTimeoutMs, hubPost: hubPost, STREMIO_TONE: STREMIO_TONE,
    stremioSetupModel: stremioSetupModel, accountModel: accountModel, loginBody: loginBody, keyBody: keyBody, playerModel: playerModel, playerSaveBody: playerSaveBody, addonsModel: addonsModel, addonsBody: addonsBody, serverSaveBody: serverSaveBody, webSaveBody: webSaveBody, networkSaveBody: networkSaveBody, openSaveBody: openSaveBody, mergeBodies: mergeBodies, stremioCacheModel: stremioCacheModel,
    cacheRequestText: cacheRequestText, repairModel: repairModel, repairBrokenFileHint: repairBrokenFileHint,
    settingsSaveFlow: settingsSaveFlow, repairFlow: repairFlow, startWebFlow: startWebFlow,
    PAGE_SECTIONS: PAGE_SECTIONS, placeOf: placeOf, timingFor: timingFor, valuesEqual: valuesEqual, defaultOf: defaultOf, setPath: setPath, formatValue: formatValue,
    qbtModel: qbtModel, qbtOverrideNames: qbtOverrideNames, qbtOverrideText: qbtOverrideText, qbtBody: qbtBody, qbtPasswordBody: qbtPasswordBody,
    qbtForgetBody: qbtForgetBody, qbtReleaseBody: qbtReleaseBody,
    rowInfo: rowInfo, matchQuery: matchQuery, planSources: planSources, providerDot: providerDot, rowOfError: rowOfError, TIMING_RULES: TIMING_RULES,
  } };
  if (typeof module !== "undefined" && module.exports) module.exports = global.JarvisDownloadsSettings;
})(typeof window !== "undefined" ? window : globalThis);
