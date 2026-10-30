/* ============================================================================
 * updates-panel.js -- the "Updates" Menu panel (Download Hub, round 5, agent H2).
 *
 * It replaces the Updates tab that used to live inside Menu > Downloads. One screen for everything the hub knows is out of
 * date: mods, games, package managers (pip, Chocolatey, ...), apps and anything a tool declares as an update source.
 *
 *   - It is a thin face on the hub's own web routes and nothing else. Three that existed (GET /api/hub/updates, POST
 *     .../check, POST .../dismiss) and three new ones (POST .../apply, POST .../dry-run, POST .../set). The payloads are the
 *     v1 contract of this round: every finding carries `group`, `can_apply` and `apply` {state, message, at}; every provider
 *     `group`, `auto_update`, `can_apply`, `updates_only`; the top level `groups`, `settings` and `applying`.
 *   - The hub never compares versions, so neither does this file. "installed -> available" is the two texts the source gave.
 *     No "newer than", no ordering by version (rows are ordered by name), no price, no rating and no link: nothing the hub
 *     did not hand over is shown.
 *   - It never builds an install command. An update is the hub asking the provider's OWN updater to apply ONE finding. Every
 *     update by hand is: a dry run first (the `plan` line says exactly what will run), then ONE dialog showing it, then the
 *     real call. Only a yes goes through. "Update all" is one dialog too (how many, which groups).
 *   - Automatic updates are off by default. Switching the master switch ON shows one plain dialog first; the per-source
 *     tick is disabled (with the reason in words) when the source has no updater of its own or the master switch is off.
 *   - Everything another program said (names, versions, sources, messages, errors) is untrusted. It is put on the page with
 *     textContent / setAttribute only, capped and stripped of control characters. This file never touches innerHTML.
 *   - The panel's own switches live in the panel (nothing is added to settings.js).
 *   - Layout (round 10, DH-D): each source block can be folded and dragged. Only the person's layout is kept in this browser
 *     (localStorage, inside try/catch, under one key): the order of the sources by id, the blocks he folded or unfolded by hand and
 *     which blocks it already decided for. It is never sent to the hub. With storage empty, blocked or full, everything still works
 *     for as long as the panel's page lives.
 *
 * Public:  window.JarvisUpdates.open() / .close() / .refreshBadge()
 * Tested:  window.JarvisUpdates._pure and ._flows (tests/verify_updates_panel.js, with a fake hub built from the contract).
 * ========================================================================= */
(function (global) {
  "use strict";

  const ROUTES = {
    read: "/api/hub/updates",
    check: "/api/hub/updates/check",
    dismiss: "/api/hub/updates/dismiss",
    apply: "/api/hub/updates/apply",
    dryRun: "/api/hub/updates/dry-run",
    set: "/api/hub/updates/set",
  };
  const GROUPS = [["mods", "Mods"], ["games", "Games"], ["packages", "Packages"], ["apps", "Apps"], ["other", "Other"]];
  const GROUP_IDS = GROUPS.map((g) => g[0]);
  const GROUP_NAME = { mods: "Mods", games: "Games", packages: "Packages", apps: "Apps", other: "Other" };
  // The switch that leaves a whole group out: nothing is checked and nothing is shown for it.
  const INCLUDE_LABEL = {
    mods: "Include mods", games: "Include games", packages: "Include package managers", apps: "Include apps", other: "Include other sources",
  };
  const DOT = " \u00b7 ";
  // Layout (round 10, DH-D). A source with this many updates or more starts folded the first time it is shown with updates;
  // after the person folds or unfolds it himself his choice wins. 8 is what still fits one screen without the list clumping.
  const COLLAPSE_AT = 8;
  const LAYOUT_KEY = "jarvis.updates.layout";
  const LAYOUT_VERSION = 1;
  const MAX_IDS = 400;
  const DRAG_STALE_MS = 120000;
  const POLL = { slow: 20000, checking: 4000, applying: 3000, closed: 60000 };
  const FAST_AFTER_CHECK_MS = 90000;
  const FAST_AFTER_APPLY_MS = 120000;
  const STARTED_NOTE = "Handed to the program, which does the rest (for Steam, in its own dialog). This row stays until a later check stops listing it.";
  const NO_UPDATER = "No updater of its own: update it in the program itself.";
  const MASTER_OFF_REASON = "Switch on \u201cUpdate by itself\u201d on the left first.";
  const NO_APPLY_REASON = "This source has no updater of its own, so it cannot update by itself.";
  const CHECKS_OFF_REASON = "Switch on this source\u2019s update checks first.";
  const AUTO_DIALOG = {
    title: "Switch on \u201cUpdate by itself\u201d?",
    body: "With this on, Jarvis starts a source\u2019s own updater by itself, without asking each time, when that source reports something out of date. "
      + "It only touches sources that have their own \u201cUpdate by itself\u201d tick, and it only runs while the hub daemon is running. "
      + "An update that fails is not retried by itself: you see it here, with the message, and can try again by hand. "
      + "It is off by default, and you can switch it off here again at any time.",
    confirmLabel: "Switch on",
  };
  // What the panel may write through /api/hub/updates/set: scope -> allowed keys. Nothing else is ever sent.
  const SET_KEYS = { global: ["auto_update"], group: ["included"], provider: ["update_checks", "auto_update"] };

  // ------------------------------------------------------------------ pure helpers

  // Text from another program: no control or bidi characters, capped. It is only ever drawn with textContent.
  function text(v, max) {
    let s = v === null || v === undefined ? "" : String(v);
    s = s.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "");
    return max && s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
  }
  function trimmed(v, max) { return text(v, max).trim(); }
  function num(v) { return typeof v === "number" && isFinite(v) ? v : null; }
  function isObj(v) { return !!v && typeof v === "object" && !Array.isArray(v); }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function fmtBytes(n) {
    const v = num(n);
    if (v === null || v <= 0) return "";
    if (v >= 1073741824) return (v / 1073741824).toFixed(2) + " GB";
    if (v >= 1048576) return (v / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(v / 1024)) + " KB";
  }

  function fmtAgo(sec) {
    const s = num(sec);
    if (s === null || s < 0) return "";
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " min ago";
    const h = Math.floor(s / 3600);
    if (h < 48) return h + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }

  function groupOf(v) {
    const g = trimmed(v).toLowerCase();
    return GROUP_IDS.indexOf(g) >= 0 ? g : "other";
  }

  function rowsOf(data) {
    const d = isObj(data) ? data : {};
    return (Array.isArray(d.updates) ? d.updates : []).filter((f) => isObj(f) && trimmed(f.provider) && trimmed(f.id));
  }

  // `off` is the panel's own memory of sources whose update checks it switched off in this session: the hub leaves such a
  // source out of its answer, so without this the person could not find the switch to turn it back on.
  function providersOf(data, off) {
    const d = isObj(data) ? data : {};
    const out = (Array.isArray(d.providers) ? d.providers : []).filter((p) => isObj(p) && trimmed(p.provider));
    const have = new Set(out.map((p) => p.provider));
    Object.keys(isObj(off) ? off : {}).forEach((id) => {
      if (!have.has(id)) out.push({ provider: id, label: off[id].label || id, group: off[id].group || "other", update_checks: false, status: "off", can_check: false, count: 0, local: true });
    });
    return out;
  }

  function groupsOf(data) {
    const d = isObj(data) ? data : {};
    const given = Array.isArray(d.groups) ? d.groups.filter((g) => isObj(g) && GROUP_IDS.indexOf(trimmed(g.id)) >= 0) : [];
    return GROUP_IDS.map((id) => {
      const g = given.find((x) => trimmed(x.id) === id);
      if (g) return { id: id, label: trimmed(g.label, 40) || GROUP_NAME[id], included: g.included !== false, count: num(g.count) || 0 };
      const n = rowsOf(d).filter((f) => groupOf(f.group) === id && !f.hidden).length;   // an older hub: count them ourselves
      return { id: id, label: GROUP_NAME[id], included: true, count: n };
    });
  }

  function includedMap(data) {
    const out = {};
    groupsOf(data).forEach((g) => { out[g.id] = g.included; });
    return out;
  }

  function settingsOf(data) {
    const s = isObj(data) && isObj(data.settings) ? data.settings : {};
    return { auto_update: s.auto_update === true, auto_max_per_cycle: num(s.auto_max_per_cycle) };
  }

  function totalCount(data) {
    const d = isObj(data) ? data : {};
    const n = num(d.count);
    return n !== null ? n : rowsOf(d).filter((f) => !f.hidden).length;
  }

  // The chips: All plus the five groups, each with the count the hub gave.
  function chipsOf(data) {
    return [{ id: "all", label: "All", count: totalCount(data), included: true }].concat(groupsOf(data));
  }

  // ---- the apply state of one finding, in words

  function applyView(a) {
    if (!isObj(a)) return null;
    const state = trimmed(a.state);
    const msg = trimmed(a.message, 200);
    if (state === "queued") return { state: state, label: "Waiting its turn", detail: msg, tone: "", busy: true };
    if (state === "running") return { state: state, label: "Updating now\u2026", detail: msg, tone: "", busy: true };
    if (state === "done") return { state: state, label: "Done", detail: (msg ? msg + " " : "") + "The row goes when a check stops listing it.", tone: "ok", busy: false };
    if (state === "started") return { state: state, label: "Handed over", detail: (msg ? msg + " " : "") + STARTED_NOTE, tone: "warn", busy: false };
    if (state === "failed") return { state: state, label: "Failed", detail: msg || "No reason was given.", tone: "bad", busy: false };
    return null;
  }

  function isBusy(f) {
    const v = applyView(f && f.apply);
    return !!(v && v.busy);
  }

  function versionText(f) {
    return (trimmed(f && f.installed, 80) || "version not stated") + " \u2192 " + (trimmed(f && f.available, 80) || "version not stated");
  }

  function metaParts(f) {
    const out = [];
    const size = fmtBytes(f && f.size_bytes);
    if (size) out.push(size);
    const via = trimmed(f && f.source, 40);
    if (via) out.push("via " + via);
    const ago = fmtAgo(f && f.age_s);
    if (ago) out.push("checked " + ago);
    return out;
  }

  // The last check of one source and how it went, in words (the hub's `status` and `problems`).
  function healthText(p) {
    if (!isObj(p)) return { text: "", tone: "", lines: [] };
    const age = fmtAgo(p.age_s);
    const lines = (Array.isArray(p.problems) ? p.problems : []).filter(isObj).map((x) => {
      const src = trimmed(x.source, 40);
      return (src ? src + ": " : "") + (trimmed(x.error, 240) || "it failed");
    });
    if (p.update_checks === false) return { text: "Update checks are switched off for this source, so nothing is checked and nothing is shown for it.", tone: "", lines: [] };
    const st = trimmed(p.status);
    if (st === "checking") return { text: "Checking now\u2026", tone: "", lines: [] };
    if (st === "failed") {
      const why = trimmed(p.error, 240) || "it failed";
      return { text: "The last check failed: " + why + (num(p.count) && age ? ". The list below is from " + age + "." : "."), tone: "bad", lines: [] };
    }
    if (st === "partial") return { text: "Checked " + (age || "a while ago") + ", but not everything:", tone: "warn", lines: lines };
    if (st === "never") return { text: "Not checked yet. The first check runs a few minutes after the hub daemon starts, or press Check now.", tone: "", lines: [] };
    if (st === "pushed") return { text: "This program reports its own updates; the hub does not check it.", tone: "", lines: [] };
    const h = num(p.update_interval_h);
    return { text: "Checked " + (age || "a while ago") + (h !== null ? ". Looks again every " + (Math.round(h * 10) / 10) + " h." : "."), tone: "", lines: lines };
  }

  // The line in the header: how many, when it last checked, whether the daemon runs, what is being applied.
  function headerText(data, o) {
    const x = o || {};
    if (!isObj(data)) return x.error ? trimmed(x.error, 200) : "reading the updates\u2026";
    const n = totalCount(data);
    const parts = [n === 0 ? "No updates found" : plural(n, "update", "updates") + " available"];
    const ages = providersOf(data).map((p) => num(p.age_s)).filter((v) => v !== null);
    parts.push(ages.length ? "last checked " + fmtAgo(Math.min.apply(null, ages)) : "nothing checked yet");
    const ap = num(data.applying) || 0;
    if (ap > 0) parts.push(plural(ap, "update is", "updates are") + " running or waiting");
    parts.push(data.daemon_alive === false ? "the hub daemon is not running: start it in Menu > Daemons" : "hub daemon running");
    return parts.join(DOT);
  }

  function badgeText(data) {
    const n = isObj(data) ? num(data.count) : null;
    return n !== null && n > 0 ? (n > 99 ? "99+" : String(n)) : "";
  }

  // ---- the list: one block per source, in group order and then by name

  function blocksOf(data, filter, off) {
    const incl = includedMap(data);
    const map = new Map();
    providersOf(data, off).forEach((p) => {
      map.set(p.provider, { id: p.provider, label: trimmed(p.label, 60) || p.provider, group: groupOf(p.group), provider: p, rows: [] });
    });
    rowsOf(data).forEach((f) => {
      if (!map.has(f.provider)) map.set(f.provider, { id: f.provider, label: trimmed(f.provider_label, 60) || f.provider, group: groupOf(f.group), provider: null, rows: [] });
      map.get(f.provider).rows.push(f);
    });
    let blocks = Array.from(map.values()).filter((b) => incl[b.group] !== false);
    if (filter && filter !== "all") blocks = blocks.filter((b) => b.group === filter);
    const key = (v) => String(v || "").toLowerCase();
    blocks.forEach((b) => b.rows.sort((a, c) => key(a.title || a.id).localeCompare(key(c.title || c.id)) || key(a.id).localeCompare(key(c.id))));
    blocks.sort((a, b) => GROUP_IDS.indexOf(a.group) - GROUP_IDS.indexOf(b.group) || key(a.label).localeCompare(key(b.label)) || key(a.id).localeCompare(key(b.id)));
    return blocks;
  }

  // A source with nothing to say (checked, nothing found) is one name in a closing line, not a block of its own.
  function splitBlocks(blocks, drawerId) {
    const shown = [];
    const quiet = [];
    (blocks || []).forEach((b) => {
      const st = b.provider ? trimmed(b.provider.status) : "";
      const loud = b.rows.length > 0 || b.id === drawerId || ["failed", "partial", "checking", "never", "off"].indexOf(st) >= 0 || (b.provider && b.provider.update_checks === false);
      (loud ? shown : quiet).push(b);
    });
    return { shown: shown, quiet: quiet };
  }

  // ---- the layout: which sources are folded and in what order (round 10, DH-D). Pure; the storage is passed in.

  // A source id that is safe to keep: a clean string of sane length (anything else is not remembered).
  function idKey(v) {
    const s = typeof v === "string" ? v : "";
    return s && s.length <= 120 && s === text(s) ? s : "";
  }

  function newLayout() {
    return { order: [], choice: Object.create(null), seen: Object.create(null), auto: Object.create(null) };
  }

  // Whatever came out of storage (a string, an object, junk) becomes a layout that is safe to use. Never throws.
  function cleanLayout(raw) {
    const out = newLayout();
    let v = raw;
    if (typeof v === "string") { try { v = JSON.parse(v); } catch (_) { return out; } }
    if (!isObj(v) || v.v !== LAYOUT_VERSION) return out;
    const seen = new Set();
    (Array.isArray(v.order) ? v.order : []).forEach((x) => {
      const id = idKey(x);
      if (id && !seen.has(id) && out.order.length < MAX_IDS) { seen.add(id); out.order.push(id); }
    });
    const pick = (src, into, wantBool) => {
      if (!isObj(src)) return;
      Object.keys(src).slice(0, MAX_IDS).forEach((k) => {
        const id = idKey(k);
        if (!id) return;
        if (wantBool && typeof src[k] === "boolean") into[id] = src[k];
        else if (!wantBool && src[k] === true) into[id] = true;
      });
    };
    pick(v.choice, out.choice, true);
    pick(v.seen, out.seen, false);
    pick(v.auto, out.auto, false);
    return out;
  }

  function layoutToJson(layout) {
    const l = layout || newLayout();
    return JSON.stringify({ v: LAYOUT_VERSION, order: l.order, choice: l.choice, seen: l.seen, auto: l.auto });
  }

  // `storage` is anything with getItem/setItem (the page's localStorage, or a fake). null, empty or broken gives the default.
  function readLayout(storage) {
    try { return storage ? cleanLayout(storage.getItem(LAYOUT_KEY)) : newLayout(); } catch (_) { return newLayout(); }
  }
  // true when it was kept, false when the browser would not (the layout then lasts for this page only).
  function writeLayout(storage, layout) {
    try { if (!storage) return false; storage.setItem(LAYOUT_KEY, layoutToJson(layout)); return true; } catch (_) { return false; }
  }

  // Folded or not: his own choice if he made one, else what the first sight of a big source decided.
  function isCollapsed(layout, id) {
    const l = layout || newLayout();
    return typeof l.choice[id] === "boolean" ? l.choice[id] : l.auto[id] === true;
  }

  // The first time a source is shown WITH updates, a big one starts folded (unless he already chose). Returns true when it changed.
  function noteSeen(layout, blocks) {
    let changed = false;
    (blocks || []).forEach((b) => {
      const id = idKey(b && b.id);
      if (!id || !b.rows || !b.rows.length || layout.seen[id] === true || Object.keys(layout.seen).length >= MAX_IDS) return;
      layout.seen[id] = true;
      if (b.rows.length >= COLLAPSE_AT && typeof layout.choice[id] !== "boolean") layout.auto[id] = true;
      changed = true;
    });
    return changed;
  }

  // His choice for these sources (Collapse all / Expand all and the toggle). Returns true when something changed.
  function setCollapsed(layout, ids, value) {
    let changed = false;
    (ids || []).forEach((raw) => {
      const id = idKey(raw);
      if (!id || layout.choice[id] === !!value) return;
      if (Object.keys(layout.choice).length >= MAX_IDS && !(id in layout.choice)) return;
      layout.choice[id] = !!value;
      changed = true;
    });
    return changed;
  }

  // Saved places first, in the saved order; a source with no saved place after them, in the order it came (group, then name).
  function orderBlocks(blocks, order) {
    const list = Array.isArray(blocks) ? blocks : [];
    const place = new Map();
    (Array.isArray(order) ? order : []).forEach((id, i) => { if (!place.has(id)) place.set(id, i); });
    const placed = list.filter((b) => place.has(b.id)).sort((x, y) => place.get(x.id) - place.get(y.id));
    return placed.concat(list.filter((b) => !place.has(b.id)));
  }

  // The saved order with every source that has no place yet added at the end; ids of sources not reported now stay where they were.
  function materializeOrder(order, blocks) {
    const out = [];
    const seen = new Set();
    (Array.isArray(order) ? order : []).concat((blocks || []).map((b) => b.id)).forEach((id) => {
      if (idKey(id) && !seen.has(id)) { seen.add(id); out.push(id); }
    });
    return out.slice(0, MAX_IDS);
  }

  // `id` moved to just before or just after `target`. A new list; the same content when either is unknown.
  function moveRelative(list, id, target, side) {
    const a = Array.isArray(list) ? list : [];
    if (id === target || a.indexOf(id) < 0 || a.indexOf(target) < 0) return a.slice();
    const out = a.filter((x) => x !== id);
    out.splice(out.indexOf(target) + (side === "after" ? 1 : 0), 0, id);
    return out;
  }

  // Move up / Move down: next to the neighbour that is SHOWN above or below it now (`shownIds` is the list as drawn).
  function moveStep(list, shownIds, id, dir) {
    const at = (shownIds || []).indexOf(id);
    const neighbour = at < 0 ? undefined : shownIds[at + (dir < 0 ? -1 : 1)];
    return neighbour === undefined ? (Array.isArray(list) ? list.slice() : []) : moveRelative(list, id, neighbour, dir < 0 ? "before" : "after");
  }

  // Dropped on the upper half of a block: before it. On the lower half: after it.
  function dropSide(y, rect) {
    const top = num(rect && rect.top), h = num(rect && rect.height);
    if (num(y) === null || top === null || h === null) return "before";
    return y < top + h / 2 ? "before" : "after";
  }

  // What the layout changes on screen; part of the page's redraw signature so a fold or a move redraws and nothing else does.
  function layoutSignature(layout) {
    const l = layout || newLayout();
    return JSON.stringify([l.order, Object.keys(l.choice).sort().map((k) => [k, l.choice[k]]), Object.keys(l.auto).sort()]);
  }

  // A folded source still tells how its updates are going: the ones running or waiting and the ones that failed.
  function applySummary(rows) {
    let busy = 0, failed = 0;
    (rows || []).forEach((f) => { const v = applyView(f && f.apply); if (v && v.busy) busy += 1; else if (v && v.state === "failed") failed += 1; });
    const parts = [];
    if (busy) parts.push(plural(busy, "update", "updates") + " running or waiting");
    if (failed) parts.push(failed + " failed");
    return { text: parts.join(DOT), bad: failed > 0 };
  }

  // After a redraw the focused control may be gone (a button that is now disabled, a row that is folded away): where focus goes then.
  function focusFallbacks(key) {
    const k = String(key || "");
    const parts = k.split(":");
    if (parts[0] === "up" || parts[0] === "dn") return ["tog:" + parts.slice(1).join(":"), "chip:all"];
    if (parts[0] === "upd" || parts[0] === "ign") return ["tog:" + parts[1], "chip:all"];
    if (k === "tools:collapse") return ["tools:expand", "chip:all"];
    if (k === "tools:expand") return ["tools:collapse", "chip:all"];
    if (k === "tools:reset") return ["tools:collapse", "tools:expand", "chip:all"];
    return [];
  }

  // What to say when there is nothing to list (null when there is a list).
  function emptyState(data, filter, blocks) {
    if (!isObj(data)) return null;
    const rows = (blocks || []).reduce((n, b) => n + b.rows.length, 0);
    if (rows > 0) return null;
    const incl = includedMap(data);
    if (filter && filter !== "all" && incl[filter] === false) {
      return { kind: "group_off", text: "This group is switched off, so nothing is checked and nothing is shown for it. Switch it on on the left to bring it back.", check: false };
    }
    const provs = (blocks || []).map((b) => b.provider).filter(Boolean);
    if (!provs.length) return { kind: "none", text: "No program reports updates yet. Package managers, games and mods appear here once something can check them and the hub daemon runs.", check: false };
    const live = provs.filter((p) => p.update_checks !== false);
    if (!live.length) return { kind: "off", text: "Update checks are switched off for these sources, so nothing is checked. Switch them on under Details.", check: false };
    const bad = live.filter((p) => ["failed", "partial"].indexOf(trimmed(p.status)) >= 0);
    if (bad.length) {
      return { kind: "failed", text: "Nothing is listed, but " + (bad.length === 1 ? "a source" : bad.length + " sources") + " could not be checked completely (below), so this does not mean everything is up to date.", check: true };
    }
    if (live.every((p) => trimmed(p.status) === "never")) return { kind: "never", text: "Nothing has been checked yet.", check: true };
    const ages = live.map((p) => num(p.age_s)).filter((v) => v !== null);
    return { kind: "clean", text: "Nothing reported an update when it was last checked" + (ages.length ? " (" + fmtAgo(Math.min.apply(null, ages)) + ")" : "") + ".", check: true };
  }

  // ---- what "Update all" and a single Update would do

  function planAll(data, scope) {
    const sc = isObj(scope) ? scope : {};
    const provs = new Map(providersOf(data).map((p) => [p.provider, p]));
    const by = {};
    let count = 0, cant = 0, busy = 0;
    rowsOf(data).forEach((f) => {
      if (f.hidden) return;
      if (sc.provider && f.provider !== sc.provider) return;
      const g = groupOf(f.group || (provs.get(f.provider) || {}).group);
      if (sc.group && sc.group !== "all" && g !== sc.group) return;
      if (f.can_apply !== true) { cant += 1; return; }
      if (isBusy(f)) { busy += 1; return; }
      count += 1;
      by[g] = (by[g] || 0) + 1;
    });
    const args = sc.provider ? { provider: sc.provider, all: true }
      : (sc.group && sc.group !== "all" ? { all: true, group: sc.group } : { all: true });
    return { count: count, cant: cant, busy: busy, groups: GROUP_IDS.filter((g) => by[g]).map((g) => ({ id: g, label: GROUP_NAME[g], count: by[g] })), args: args };
  }

  function confirmAllText(plan, scopeName) {
    const groups = plan.groups.map((g) => g.label + " " + g.count).join(", ");
    let body = "This starts each program\u2019s own updater, one after another" + (scopeName ? " (" + scopeName + ")" : "") + ": " + groups + ". "
      + "Jarvis does not install anything itself. A program that cannot finish shows its message here.";
    if (plan.cant) body += " " + plan.cant + " more cannot be updated from here: their source has no updater of its own.";
    if (plan.busy) body += " " + plan.busy + " already queued or running " + (plan.busy === 1 ? "is" : "are") + " left alone.";
    return { title: "Update " + plural(plan.count, "thing", "things") + "?", body: body, confirmLabel: "Update " + plan.count, level: "warn", focusCancel: true };
  }

  function confirmOneText(f, plan) {
    const name = trimmed(f && (f.title || f.id), 120);
    const planLine = trimmed(plan, 400);
    return {
      title: "Update " + name + "?",
      body: "This runs the program\u2019s own updater for this one (" + versionText(f) + "). It starts now and can take a few minutes. "
        + (planLine ? "This is exactly what will run:" : "The program did not say what it will run."),
      pre: planLine || "",
      confirmLabel: "Update", level: "warn", focusCancel: true,
    };
  }

  // The tick of one source: on or off, and when it cannot be used, why (in words).
  function autoTick(p, master) {
    const on = !!(p && p.auto_update === true);
    if (!p || p.can_apply !== true) return { checked: on, disabled: true, reason: NO_APPLY_REASON };
    if (p.update_checks === false) return { checked: on, disabled: true, reason: CHECKS_OFF_REASON };
    if (!master) return { checked: on, disabled: true, reason: MASTER_OFF_REASON };
    return { checked: on, disabled: false, reason: "" };
  }

  // The body of a write to /api/hub/updates/set, or null when the panel is not allowed to send it.
  function setArgs(scope, id, key, value) {
    if (!Object.prototype.hasOwnProperty.call(SET_KEYS, scope) || SET_KEYS[scope].indexOf(key) < 0) return null;
    if (typeof value !== "boolean") return null;
    if (scope === "global") return { scope: scope, key: key, value: value };
    const ident = trimmed(id, 40);
    if (!ident || (scope === "group" && GROUP_IDS.indexOf(ident) < 0)) return null;
    return { scope: scope, id: ident, key: key, value: value };
  }

  // Quick while something runs or was just asked for, slow otherwise; slowest when the panel is closed (only the badge follows).
  function pollDelay(o) {
    const x = o || {};
    if (!x.open) return POLL.closed;
    if ((num(x.applying) || 0) > 0) return POLL.applying;
    return x.checking || x.recent ? POLL.checking : POLL.slow;
  }

  // What the screen shows, with ages in whole minutes, so an unchanged list is not drawn again on every read.
  function signature(data) {
    const d = isObj(data) ? data : {};
    const mins = (v) => (num(v) === null ? null : Math.round(v / 60));
    return JSON.stringify({
      u: (Array.isArray(d.updates) ? d.updates : []).map((f) => [f.provider, f.id, f.title, f.installed, f.available, f.size_bytes, f.source, f.group, f.can_apply, mins(f.age_s), isObj(f.apply) ? [f.apply.state, f.apply.message] : null]),
      p: (Array.isArray(d.providers) ? d.providers : []).map((p) => [p.provider, p.label, p.group, p.status, p.error, p.problems, p.count, p.can_check, p.can_apply, p.auto_update, p.update_checks, mins(p.age_s), p.update_interval_h]),
      g: Array.isArray(d.groups) ? d.groups.map((g) => [g.id, g.included, g.count]) : null,
      s: isObj(d.settings) ? [d.settings.auto_update, d.settings.auto_max_per_cycle] : null,
      a: num(d.applying) || 0,
      n: num(d.count),
      d: d.daemon_alive === true,
    });
  }

  // What to say after Check now. `started` is how many checks the daemon will begin.
  function checkNotice(r) {
    const res = isObj(r) && Array.isArray(r.results) ? r.results.filter(isObj) : [];
    if (!res.length) return { text: trimmed(isObj(r) ? (r.message || r.error) : "", 200) || "No source has an update check that is switched on.", level: "info", started: 0 };
    const started = res.filter((x) => x.status === "requested").length;
    if (started) return { text: started === 1 ? "Checking now. The answer appears here in a few seconds." : "Checking " + started + " sources now. The answers appear here in a few seconds.", level: "info", started: started };
    if (res.some((x) => x.status === "already_checking")) return { text: "Already checking. The answer appears here when it finishes.", level: "info", started: 0 };
    const first = res.find((x) => trimmed(x.message));
    return { text: first ? trimmed(first.message, 200) : "Nothing was started.", level: "warn", started: 0 };
  }

  // What to say after an apply request: how many were queued and why the others were not.
  function applyNotice(r) {
    const res = isObj(r) && Array.isArray(r.results) ? r.results.filter(isObj) : [];
    if (!res.length) return { text: trimmed(isObj(r) ? (r.error || r.message) : "", 200) || "Nothing was started.", level: "warn", queued: 0 };
    const count = (s) => res.filter((x) => x.status === s).length;
    const queued = count("queued");
    if (queued) {
      const rest = [];
      if (count("already_running")) rest.push(count("already_running") + " already running");
      if (count("not_applicable")) rest.push(count("not_applicable") + " cannot be updated from here");
      if (count("refused")) rest.push(count("refused") + " refused");
      return { text: "Queued " + plural(queued, "update", "updates") + (rest.length ? " (" + rest.join(", ") + ")" : "") + ". Each row shows its state as it goes.", level: "info", queued: queued };
    }
    const first = res.find((x) => trimmed(x.message));
    return { text: first ? trimmed(first.message, 200) : "Nothing was started.", level: "warn", queued: 0 };
  }

  // ------------------------------------------------------------------ talking to the hub (the three routes plus the three new ones)

  async function request(path, body) {
    const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Jarvis-Hub": "1" }, body: JSON.stringify(body) };
    const res = await global.fetch(path, opt);
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    return { ok: !!res.ok, status: res.status, data: data };
  }
  // Throws on anything but a clean ok, with the hub's own sentence.
  async function hubCall(path, body) {
    const r = await request(path, body);
    if (!r.ok || !isObj(r.data) || r.data.ok === false) throw new Error(trimmed(isObj(r.data) ? (r.data.error || r.data.message) : "", 240) || "HTTP " + r.status);
    return r.data;
  }
  // Returns the answer even when it says ok: false (an apply answer carries one line per finding either way).
  async function hubRaw(path, body) {
    const r = await request(path, body);
    if (!isObj(r.data)) throw new Error("HTTP " + r.status);
    return r.data;
  }

  function UI() { return global.JarvisUI || null; }
  function say(message, level) {
    const ui = UI();
    if (ui && typeof ui.toast === "function") ui.toast({ message: text(message, 240), level: level || "info" });
  }
  // The page's own dialog. With none, nothing runs: a yes must come from a person.
  async function ask(opts) {
    const ui = UI();
    if (!ui || typeof ui.confirm !== "function") return false;
    try { return (await ui.confirm(opts)) === true; } catch (_) { return false; }
  }
  const REAL = { call: hubCall, raw: hubRaw, ask: ask, say: say };

  // ------------------------------------------------------------------ the flows (the order and the content of every write)

  function makeFlows(deps) {
    const d = Object.assign({}, REAL, deps || {});

    // One update: a dry run (changes nothing), one dialog with the plan line, then the real call.
    async function updateOne(f) {
      const provider = trimmed(f && f.provider, 40);
      const id = trimmed(f && f.id, 120);
      if (!provider || !id) return { ok: false, stage: "input" };
      let plan = "";
      try {
        const dry = await d.call(ROUTES.dryRun, { provider: provider, id: id });
        plan = trimmed(dry && dry.plan, 400);
      } catch (err) {
        d.say("Could not work out what the update would run, so nothing was started: " + text(err && err.message, 160), "warn");
        return { ok: false, stage: "dry_run" };
      }
      if (!(await d.ask(confirmOneText(f, plan)))) return { ok: false, declined: true };
      return applyRequest({ provider: provider, id: id });
    }

    // Update all (every source, one group or one source): one dialog that says how many and which groups.
    async function updateMany(data, scope, scopeName) {
      const plan = planAll(data, scope);
      if (!plan.count) {
        d.say(plan.busy ? "Everything that can be updated is already queued or running." : "Nothing here can be updated from this panel.", "info");
        return { ok: false, stage: "nothing" };
      }
      if (!(await d.ask(confirmAllText(plan, scopeName)))) return { ok: false, declined: true };
      return applyRequest(plan.args);
    }

    async function applyRequest(body) {
      let r;
      try { r = await d.raw(ROUTES.apply, body); } catch (err) {
        d.say(text(err && err.message, 200), "warn");
        return { ok: false, stage: "apply" };
      }
      const n = applyNotice(r);
      d.say(n.text, n.level);
      return { ok: n.queued > 0, queued: n.queued };
    }

    // The master switch. Off -> on shows the one plain dialog first; a no leaves it off and writes nothing.
    async function switchAuto(on) {
      if (on && !(await d.ask(AUTO_DIALOG))) return { ok: false, declined: true };
      return setSetting("global", "", "auto_update", on);
    }

    async function setSetting(scope, id, key, value) {
      const body = setArgs(scope, id, key, value);
      if (!body) return { ok: false, stage: "input" };
      try { await d.call(ROUTES.set, body); } catch (err) {
        d.say(text(err && err.message, 200), "warn");
        return { ok: false, stage: "set" };
      }
      return { ok: true };
    }

    async function check(provider) {
      let r;
      try { r = await d.call(ROUTES.check, { provider: trimmed(provider, 40) }); } catch (err) {
        d.say(text(err && err.message, 200), "warn");
        return { ok: false, started: 0 };
      }
      const n = checkNotice(r);
      d.say(n.text, n.level);
      return { ok: true, started: n.started };
    }

    async function ignore(f) {
      const ok = await d.ask({
        title: "Ignore this version?", body: "It stays hidden until the program reports a different version. Nothing is installed, changed or deleted.",
        level: "warn", focusCancel: true, confirmLabel: "Ignore",
      });
      if (!ok) return { ok: false, declined: true };
      try { await d.call(ROUTES.dismiss, { provider: trimmed(f && f.provider, 40), id: trimmed(f && f.id, 120) }); } catch (err) {
        d.say(text(err && err.message, 200), "warn");
        return { ok: false, stage: "dismiss" };
      }
      return { ok: true };
    }

    return { updateOne, updateMany, switchAuto, setSetting, check, ignore };
  }

  // ------------------------------------------------------------------ DOM (only reached from open(); never at load)

  function el(tag, props, children) {
    const node = document.createElement(tag);
    const p = props || {};
    Object.keys(p).forEach((k) => {
      const v = p[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = text(v);
      else if (k.slice(0, 2) === "on" && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : text(v));
    });
    [].concat(children === undefined ? [] : children).forEach((c) => {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(text(c)) : c);
    });
    return node;
  }

  const S = {
    overlay: null, panel: null, onKey: null, prevFocus: null,
    data: null, loaded: false, error: "", busy: false, again: false, timer: 0, readAt: 0,
    filter: "all", drawer: "", off: {}, asking: false, working: false, fastUntil: 0, sig: "",
    ref: {},
    layout: null, saved: true, shown: [], drag: "", dragAt: 0, deferred: false,
  };
  const FLOWS = makeFlows();
  const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex=\"-1\"])";

  // ---- the layout in this browser (round 10, DH-D): one key in localStorage, always inside try/catch, never sent anywhere
  function browserStore() { try { return global.localStorage || null; } catch (_) { return null; } }
  function ensureLayout() { if (!S.layout) S.layout = readLayout(browserStore()); return S.layout; }
  function keepLayout() { S.saved = writeLayout(browserStore(), ensureLayout()); }
  function allBlocks() { return blocksOf(S.data, "all", S.off); }
  function dragging() { return !!S.drag && Date.now() - S.dragAt < DRAG_STALE_MS; }
  function announce(msg) { if (S.ref.live) S.ref.live.textContent = text(msg, 200); }

  function setBadge(data) {
    if (typeof document === "undefined") return;
    const node = document.getElementById("menu-item-updates-badge");
    if (!node) return;
    const t = badgeText(data);
    node.textContent = t;
    node.hidden = !t;
    if (t) node.setAttribute("aria-label", plural(totalCount(data), "update", "updates") + " available");
    else node.removeAttribute("aria-label");
  }

  function schedule() {
    clearTimeout(S.timer);
    const checking = !!(S.data && Array.isArray(S.data.providers) && S.data.providers.some((p) => p && p.status === "checking"));
    const delay = pollDelay({ open: !!S.overlay, applying: S.data && S.data.applying, checking: checking, recent: Date.now() < S.fastUntil });
    // A hidden browser tab reads nothing at all (the badge is only a number); it picks up again when the tab is shown.
    S.timer = setTimeout(() => { if (document.hidden) schedule(); else load(false); }, delay);
  }

  async function load(manual) {
    if (S.busy) { S.again = true; return; }
    S.busy = true;
    try {
      const r = await hubCall(ROUTES.read);
      S.data = r; S.loaded = true; S.error = ""; S.readAt = Date.now();
      setBadge(r);
      Object.keys(S.off).forEach((id) => { if ((r.providers || []).some((p) => p && p.provider === id)) delete S.off[id]; });
      if (S.overlay) render(false);
    } catch (err) {
      S.error = trimmed(err && err.message, 200) || "The updates couldn't be read.";
      if (S.overlay) { render(true); if (manual && S.loaded) say(S.error, "warn"); }
    } finally {
      S.busy = false;
      schedule();
      if (S.again) { S.again = false; setTimeout(() => load(false), 0); }
    }
  }

  function loadSoon(fastMs) {
    if (fastMs) S.fastUntil = Date.now() + fastMs;
    load(false);
  }

  // ---- drawing

  function switchRow(o) {
    const input = el("input", { type: "checkbox", role: "switch", class: "up-switch__box", "data-focus": o.key });
    input.checked = !!o.checked;
    input.disabled = !!o.disabled;
    input.addEventListener("change", () => o.onchange(input.checked, input));
    return el("div", { class: "up-switchrow" }, [
      el("label", { class: "up-switch" + (o.disabled ? " is-disabled" : "") }, [input, el("span", { class: "up-switch__label", text: o.label })]),
      o.hint ? el("div", { class: "up-hint", text: o.hint }) : null,
    ]);
  }

  function button(label, o) {
    return el("button", {
      type: "button", class: "btn btn--ghost btn--sm" + (o && o.cls ? " " + o.cls : ""), text: label,
      title: o && o.title, "aria-label": o && o.aria, "aria-pressed": o && o.pressed !== undefined ? String(!!o.pressed) : null,
      "aria-expanded": o && o.expanded !== undefined ? String(!!o.expanded) : null,
      "data-focus": o && o.key, disabled: o && o.disabled, onclick: o && o.onclick,
    });
  }

  function renderChips() {
    const box = S.ref.chips;
    box.textContent = "";
    chipsOf(S.data).forEach((c) => {
      box.appendChild(el("button", {
        type: "button", class: "up-chip" + (S.filter === c.id ? " is-active" : ""), "aria-pressed": String(S.filter === c.id), "data-focus": "chip:" + c.id,
        onclick: () => { S.filter = c.id; render(true); },
      }, [el("span", { text: c.label }), el("span", { class: "up-chip__n", text: c.included ? String(c.count) : "off" })]));
    });
  }

  function renderGroupSwitches() {
    const box = S.ref.groups;
    box.textContent = "";
    groupsOf(S.data).forEach((g) => {
      box.appendChild(switchRow({
        key: "group:" + g.id, label: INCLUDE_LABEL[g.id], checked: g.included, disabled: !S.loaded || S.working,
        hint: g.included ? "" : "Off: nothing is checked and nothing is shown.",
        onchange: (on) => changeSetting("group", g.id, "included", on),
      }));
    });
  }

  function renderAuto() {
    const box = S.ref.auto;
    box.textContent = "";
    const st = settingsOf(S.data);
    box.appendChild(switchRow({
      key: "auto:master", label: "Update by itself", checked: st.auto_update, disabled: !S.loaded || S.working,
      hint: st.auto_update
        ? "On. Only sources with their own tick (Details of a source) update by themselves" + (st.auto_max_per_cycle ? ", at most " + st.auto_max_per_cycle + " per round" : "") + ". A failed update is not retried by itself."
        : "Off. Nothing updates unless you press Update.",
      onchange: async (on, input) => {
        S.working = true;
        const r = await FLOWS.switchAuto(on);
        S.working = false;
        if (!r.ok) input.checked = !on;
        loadSoon(0);
        render(true);
      },
    }));
  }

  async function changeSetting(scope, id, key, value) {
    S.working = true;
    const r = await FLOWS.setSetting(scope, id, key, value);
    S.working = false;
    if (r.ok && scope === "provider" && key === "update_checks") {
      if (value === false) {
        const p = providersOf(S.data).find((x) => x.provider === id);
        S.off[id] = { label: p ? trimmed(p.label, 60) : id, group: p ? groupOf(p.group) : "other" };
      } else delete S.off[id];
    }
    loadSoon(0);
    render(true);
  }

  function renderBanner() {
    const box = S.ref.banner;
    box.textContent = "";
    const d = S.data;
    const add = (t, tone) => box.appendChild(el("div", { class: "up-banner" + (tone ? " is-" + tone : ""), role: "status", text: t }));
    if (isObj(d) && d.daemon_alive === false) add("The hub daemon is not running, so nothing is checked or updated and the list below may be old. Start it in Menu > Daemons.", "bad");
    if (isObj(d) && (num(d.applying) || 0) > 0) add(plural(d.applying, "update is", "updates are") + " running or waiting. This list refreshes by itself.", "info");
    if (S.error && S.loaded) add("The last read failed: " + S.error + " What you see is from earlier.", "warn");
    box.hidden = !box.childNodes.length;
  }

  function renderRow(f) {
    const meta = metaParts(f);
    const view = applyView(f.apply);
    const busy = !!(view && view.busy);
    const can = f.can_apply === true;
    const name = trimmed(f.title || f.id, 200);
    return el("div", { class: "up-row" + (view ? " is-" + view.state : ""), role: "listitem", "data-provider": f.provider, "data-id": f.id }, [
      el("div", { class: "up-row__main" }, [
        el("div", { class: "up-row__title", text: name, title: name }),
        el("div", { class: "up-row__ver", text: versionText(f) }),
        meta.length ? el("div", { class: "up-row__meta" }, meta.map((t) => el("span", { text: t }))) : null,
        view ? el("div", { class: "up-apply is-" + (view.tone || "plain"), role: "status" }, [el("b", { text: view.label }), view.detail ? " " + view.detail : null]) : null,
        !can ? el("div", { class: "up-hint", text: NO_UPDATER }) : null,
      ]),
      el("div", { class: "up-row__actions" }, [
        button(view && view.state === "failed" ? "Try again" : "Update", {
          key: "upd:" + f.provider + ":" + f.id, disabled: !can || busy, aria: "Update " + name,
          title: !can ? NO_UPDATER : busy ? "Already queued or running." : "Ask the program\u2019s own updater to update this one.",
          onclick: async () => { const r = await FLOWS.updateOne(f); if (r.ok) loadSoon(FAST_AFTER_APPLY_MS); },
        }),
        button("Ignore this version", {
          key: "ign:" + f.provider + ":" + f.id, aria: "Ignore this version of " + name, title: "Hide this one until the program reports a different version.",
          onclick: async () => { const r = await FLOWS.ignore(f); if (r.ok) loadSoon(0); },
        }),
      ]),
    ]);
  }

  // ---- the layout actions: fold, move, drag (round 10, DH-D)

  function toggleBlock(id) {
    const l = ensureLayout();
    setCollapsed(l, [id], !isCollapsed(l, id));
    keepLayout();
    render(true);
  }

  function foldAll(value) {
    setCollapsed(ensureLayout(), S.shown, value);
    keepLayout();
    announce(value ? "All sources in this list are folded." : "All sources in this list are open.");
    render(true);
  }

  function resetOrder() {
    ensureLayout().order = [];
    keepLayout();
    announce("Order reset: groups first, then by name.");
    render(true);
  }

  // Stores `next` as the saved order when it differs from `base`; always redraws. True when the order changed.
  function applyMove(base, next) {
    const changed = JSON.stringify(base) !== JSON.stringify(next);
    if (changed) { ensureLayout().order = next; keepLayout(); }
    render(true);
    return changed;
  }

  function moveBlock(id, dir) {
    const blocks = allBlocks();
    const base = materializeOrder(ensureLayout().order, blocks);
    const at = S.shown.indexOf(id);
    const found = blocks.find((b) => b.id === id);
    if (applyMove(base, moveStep(base, S.shown, id, dir))) announce((found ? found.label : id) + " moved " + (dir < 0 ? "up" : "down") + ", place " + (at + (dir < 0 ? 0 : 2)) + " of " + S.shown.length + ".");
  }

  function markDrop(node, side) {
    node.classList.remove("is-drop-before");
    node.classList.remove("is-drop-after");
    if (side) node.classList.add("is-drop-" + side);
  }

  function dropOn(targetId, side) {
    const id = S.drag;
    S.drag = ""; S.deferred = false;
    if (!id || id === targetId) { render(true); return; }
    const blocks = allBlocks();
    const base = materializeOrder(ensureLayout().order, blocks);
    const label = (x) => { const f = blocks.find((b) => b.id === x); return f ? f.label : x; };
    if (applyMove(base, moveRelative(base, id, targetId, side))) announce(label(id) + " moved " + (side === "after" ? "below " : "above ") + label(targetId) + ".");
  }

  function endDrag() {
    const redraw = !!S.drag || S.deferred;
    S.drag = ""; S.deferred = false;
    if (redraw) render(true);                                                   // a read that came in during the drag is drawn now
  }

  function renderBlock(b, at, count) {
    const p = b.provider;
    const health = healthText(p);
    const plan = planAll(S.data, { provider: b.id });
    const can = !!(p && p.can_check);
    const folded = isCollapsed(ensureLayout(), b.id);
    const sum = folded ? applySummary(b.rows) : { text: "", bad: false };
    const grip = el("span", { class: "up-grip", title: "Drag to move this source", "aria-hidden": "true", text: "\u22ee\u22ee" });
    const node = el("div", { class: "up-block" + (folded ? " is-collapsed" : ""), role: "group", "aria-label": b.label, "data-provider": b.id }, [
      el("div", { class: "up-block__head" }, [
        grip,
        button(folded ? "\u25b8" : "\u25be", {
          cls: "up-fold", key: "tog:" + b.id, expanded: !folded, aria: (folded ? "Expand " : "Collapse ") + b.label,
          title: folded ? "Show the updates of this source." : "Hide the updates of this source. Its name, count and any error stay.", onclick: () => toggleBlock(b.id),
        }),
        el("span", { class: "up-block__name", text: b.label }),
        el("span", { class: "up-tag", text: GROUP_NAME[b.group] }),
        el("span", { class: "up-block__n", text: plural(b.rows.length, "update", "updates") }),
        p && p.auto_update === true ? el("span", { class: "up-tag up-tag--on", text: "updates by itself" }) : null,
        sum.text ? el("span", { class: "up-summary" + (sum.bad ? " is-bad" : ""), text: sum.text }) : null,
        el("span", { class: "up-spacer" }),
        can ? button("Check now", { key: "chk:" + b.id, disabled: S.asking, aria: "Check " + b.label + " again", title: "Ask this source again. This only looks; nothing is installed.", onclick: () => doCheck(b.id) }) : null,
        plan.count ? button("Update all", { key: "all:" + b.id, aria: "Update all of " + b.label, title: "Update everything this source lists, one after another.", onclick: () => doUpdateMany({ provider: b.id }, b.label) }) : null,
        button("Details", { key: "det:" + b.id, pressed: S.drawer === b.id, aria: "Details of " + b.label, onclick: () => { S.drawer = S.drawer === b.id ? "" : b.id; render(true); } }),
        button("\u2191", { cls: "up-move", key: "up:" + b.id, disabled: at === 0, aria: "Move " + b.label + " up", title: "Move up", onclick: () => moveBlock(b.id, -1) }),
        button("\u2193", { cls: "up-move", key: "dn:" + b.id, disabled: at === count - 1, aria: "Move " + b.label + " down", title: "Move down", onclick: () => moveBlock(b.id, 1) }),
      ]),
      // Folded: only a problem is still told (the last check failed or was partial); the plain "checked 5 min ago" line goes with the rows.
      health.text && (!folded || health.tone) ? el("div", { class: "up-health" + (health.tone ? " is-" + health.tone : "") }, [text(health.text)].concat(health.lines.map((t) => el("div", { class: "up-health__line", text: t })))) : null,
      folded ? null : el("div", { class: "up-block__rows", role: "list" }, b.rows.map(renderRow)),
    ]);
    // Only the grip starts a drag, so the text and the buttons of the block stay selectable and clickable.
    grip.addEventListener("mousedown", () => { node.setAttribute("draggable", "true"); });
    grip.addEventListener("mouseup", () => { if (!S.drag) node.removeAttribute("draggable"); });
    node.addEventListener("dragstart", (e) => {
      if (node.getAttribute("draggable") !== "true") return;
      S.drag = b.id; S.dragAt = Date.now();
      node.classList.add("is-dragging");
      try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", b.id); } catch (_) { /* some browsers refuse; the drag still works */ }
    });
    node.addEventListener("dragover", (e) => {
      if (!dragging() || S.drag === b.id) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = "move"; } catch (_) { /* ignore */ }
      markDrop(node, dropSide(e.clientY, node.getBoundingClientRect()));
    });
    node.addEventListener("dragleave", () => markDrop(node, ""));
    node.addEventListener("drop", (e) => {
      if (!dragging() || S.drag === b.id) return;
      e.preventDefault();
      dropOn(b.id, dropSide(e.clientY, node.getBoundingClientRect()));
    });
    node.addEventListener("dragend", endDrag);
    return node;
  }

  // Above the list: fold or open every source in it, and bring the usual order back.
  function renderTools() {
    const box = S.ref.tools;
    box.textContent = "";
    const ids = S.loaded ? S.shown : [];
    box.hidden = !ids.length;
    if (!ids.length) return;
    const l = ensureLayout();
    const folded = ids.filter((id) => isCollapsed(l, id)).length;
    box.appendChild(button("Collapse all", { key: "tools:collapse", disabled: folded === ids.length, title: "Fold every source in this list. Names, counts and errors stay.", onclick: () => foldAll(true) }));
    box.appendChild(button("Expand all", { key: "tools:expand", disabled: folded === 0, title: "Open every source in this list.", onclick: () => foldAll(false) }));
    box.appendChild(button("Reset order", { key: "tools:reset", disabled: !l.order.length, title: "Back to the usual order: group, then name. Folded sources stay folded.", onclick: resetOrder }));
    if (!S.saved) box.appendChild(el("span", { class: "up-hint", text: "This browser would not keep the layout, so it lasts only until this page is closed." }));
  }

  function renderList() {
    const box = S.ref.list;
    box.textContent = "";
    S.shown = [];
    if (!S.loaded) {
      box.appendChild(el("div", { class: "up-empty", text: S.error || "Reading the updates\u2026" }));
      return;
    }
    const blocks = orderBlocks(blocksOf(S.data, S.filter, S.off), ensureLayout().order);
    const empty = emptyState(S.data, S.filter, blocks);
    if (empty) {
      box.appendChild(el("div", { class: "up-empty", "data-kind": empty.kind }, [
        el("div", { text: empty.text }),
        empty.check && !(S.data && S.data.daemon_alive === false) ? button(empty.kind === "never" ? "Check now" : "Check again", { key: "empty:check", disabled: S.asking, onclick: () => doCheck("") }) : null,
      ]));
    }
    const parts = splitBlocks(blocks, S.drawer);
    S.shown = parts.shown.map((b) => b.id);
    parts.shown.forEach((b, i) => box.appendChild(renderBlock(b, i, parts.shown.length)));
    if (parts.quiet.length) {
      box.appendChild(el("div", { class: "up-quiet", text: "No updates reported by: " + parts.quiet.map((b) => b.label).join(", ") + "." }));
    }
  }

  function renderDrawer() {
    const box = S.ref.drawer;
    box.textContent = "";
    const id = S.drawer;
    const title = S.ref.drawerTitle;
    if (!id) {
      title.textContent = "Source";
      box.appendChild(el("div", { class: "up-hint", text: "Press Details on a source to see how its last check went, how often it looks, and to switch its update checks or automatic updates on or off." }));
      return;
    }
    const p = providersOf(S.data, S.off).find((x) => x.provider === id);
    if (!p) {
      title.textContent = "Source";
      box.appendChild(el("div", { class: "up-hint", text: "This source is no longer reported." }));
      return;
    }
    const label = trimmed(p.label, 60) || id;
    title.textContent = label;
    const master = settingsOf(S.data).auto_update;
    const health = healthText(p);
    const tick = autoTick(p, master);
    const checksOn = p.update_checks !== false;
    const h = num(p.update_interval_h);
    box.appendChild(el("div", { class: "up-drawer__group", text: GROUP_NAME[groupOf(p.group)] + (p.updates_only ? " \u00b7 reports and applies updates only, supplies no downloads" : "") }));
    box.appendChild(el("h4", { class: "up-h", text: "Last check" }));
    box.appendChild(el("div", { class: "up-health" + (health.tone ? " is-" + health.tone : "") }, [text(health.text)].concat(health.lines.map((t) => el("div", { class: "up-health__line", text: t })))));
    box.appendChild(el("div", { class: "up-hint", text: (h !== null ? "Looks again every " + (Math.round(h * 10) / 10) + " h. " : "") + "The interval is set in Settings > Downloads." }));
    box.appendChild(el("h4", { class: "up-h", text: "Switches" }));
    box.appendChild(switchRow({
      key: "drawer:checks", label: "Update checks", checked: checksOn, disabled: S.working,
      hint: checksOn ? "On: this source is checked on its interval." : "Off: nothing is checked and nothing is shown for it.",
      onchange: (on) => changeSetting("provider", id, "update_checks", on),
    }));
    box.appendChild(switchRow({
      key: "drawer:auto", label: "Update by itself", checked: tick.checked, disabled: tick.disabled || S.working, hint: tick.reason,
      onchange: (on) => changeSetting("provider", id, "auto_update", on),
    }));
    const row = el("div", { class: "up-drawer__actions" });
    if (p.can_check) row.appendChild(button("Check now", { key: "drawer:check", disabled: S.asking, onclick: () => doCheck(id) }));
    if (planAll(S.data, { provider: id }).count) row.appendChild(button("Update all", { key: "drawer:all", onclick: () => doUpdateMany({ provider: id }, label) }));
    row.appendChild(button("Close", { key: "drawer:close", onclick: () => { S.drawer = ""; render(true); } }));
    box.appendChild(row);
  }

  function renderHeadButtons() {
    const down = !!(S.data && S.data.daemon_alive === false);
    const plan = planAll(S.data, { group: S.filter });
    S.ref.check.disabled = S.asking || !S.loaded;
    S.ref.check.textContent = S.asking ? "Asking\u2026" : "Check now";
    S.ref.all.disabled = !S.loaded || plan.count === 0;
    S.ref.all.title = plan.count ? "Ask each program\u2019s own updater to update " + plural(plan.count, "thing", "things") + "." : "Nothing here can be updated from this panel.";
    S.ref.check.title = down ? "The hub daemon is not running; start it in Menu > Daemons." : "Ask every source that has an update check to look again. This only looks; nothing is installed.";
  }

  function render(force) {
    if (!S.overlay) return;
    if (dragging()) { S.deferred = true; return; }                              // a redraw under the hand would kill the drag; it is drawn when the drag ends
    S.ref.sub.textContent = headerText(S.data, { error: S.error });
    S.ref.sub.className = "debug-panel__subtitle" + (S.data && S.data.daemon_alive === false ? " is-error" : "");
    ensureLayout();
    if (S.loaded && isObj(S.data) && noteSeen(S.layout, allBlocks())) keepLayout();
    const sig = signature(S.data) + "|" + layoutSignature(S.layout) + "|" + S.saved + "|" + S.filter + "|" + S.drawer + "|" + Object.keys(S.off).join(",") + "|" + S.asking + "|" + S.working + "|" + S.loaded + "|" + S.error;
    if (!force && sig === S.sig) return;
    S.sig = sig;
    // Redrawing must not cost the person their place: keep the focused control and the scroll positions.
    const active = document.activeElement && S.panel && S.panel.contains(document.activeElement) ? document.activeElement.getAttribute("data-focus") : "";
    const scrolls = [S.ref.list, S.ref.left, S.ref.drawer].map((n) => n.scrollTop);
    renderChips(); renderGroupSwitches(); renderAuto(); renderBanner(); renderList(); renderTools(); renderDrawer(); renderHeadButtons();
    S.ref.count.textContent = S.loaded ? String(totalCount(S.data)) : "";
    [S.ref.list, S.ref.left, S.ref.drawer].forEach((n, i) => { n.scrollTop = scrolls[i]; });
    if (active) {
      const find = (key) => Array.prototype.find.call(S.panel.querySelectorAll("[data-focus]"), (n) => n.getAttribute("data-focus") === key && !n.disabled);
      const again = [active].concat(focusFallbacks(active)).map(find).find(Boolean);
      if (again) again.focus();
    }
  }

  // ---- the buttons

  async function doCheck(provider) {
    if (S.asking) return;
    S.asking = true; render(true);
    const r = await FLOWS.check(provider);
    S.asking = false;
    loadSoon(r.started ? FAST_AFTER_CHECK_MS : 0);
    render(true);
  }

  async function doUpdateMany(scope, name) {
    const r = await FLOWS.updateMany(S.data, scope, name);
    if (r.ok) loadSoon(FAST_AFTER_APPLY_MS);
  }

  // ---- open / close

  function open() {
    if (typeof document === "undefined") return;
    if (S.overlay) { const first = S.panel.querySelector("[data-focus]"); if (first) first.focus(); return; }
    S.prevFocus = document.activeElement;
    const sub = el("div", { class: "debug-panel__subtitle", role: "status", "aria-live": "polite" });
    const checkBtn = button("Check now", { key: "head:check", onclick: () => doCheck("") });
    const allBtn = button("Update all", { key: "head:all", onclick: () => doUpdateMany({ group: S.filter }, S.filter === "all" ? "" : GROUP_NAME[S.filter]) });
    const refreshBtn = button("Refresh", { key: "head:refresh", title: "Read the list again now", onclick: () => load(true) });
    const chips = el("div", { class: "up-chips", role: "group", "aria-label": "Filter by group" });
    const groups = el("div", { class: "up-side" });
    const auto = el("div", { class: "up-side" });
    const left = el("div", { class: "up-left" }, [
      chips,
      el("h4", { class: "up-h", text: "Which groups are checked" }), groups,
      el("h4", { class: "up-h", text: "Automatic updates" }), auto,
    ]);
    const count = el("span", { class: "debug-tool-count" });
    const banner = el("div", { class: "up-banners" });
    const tools = el("div", { class: "up-tools", role: "toolbar", "aria-label": "List layout" });
    const live = el("div", { class: "up-live", role: "status", "aria-live": "polite" });
    const list = el("div", { class: "up-list" });
    const drawerTitle = el("h3", { text: "Source" });
    const drawer = el("div", { class: "up-drawer" });
    S.ref = { sub: sub, check: checkBtn, all: allBtn, chips: chips, groups: groups, auto: auto, left: left, count: count, banner: banner, tools: tools, live: live, list: list, drawer: drawer, drawerTitle: drawerTitle };
    S.panel = el("div", { class: "debug-panel up-panel", role: "dialog", "aria-modal": "true", "aria-label": "Updates" }, [
      el("div", { class: "panel__corner panel__corner--tl" }), el("div", { class: "panel__corner panel__corner--br" }),
      el("div", { class: "debug-panel__head" }, [
        el("div", { class: "debug-panel__titles" }, [el("div", { class: "debug-panel__title", text: "UPDATES" }), sub]),
        el("div", { class: "up-head-actions" }, [checkBtn, allBtn, refreshBtn]),
        el("button", { class: "modal__close", type: "button", "aria-label": "Close", text: "\u00d7", onclick: close }),
      ]),
      el("div", { class: "debug-body up-body" }, [
        el("section", { class: "debug-pane", "aria-label": "Filter" }, [el("div", { class: "debug-pane__head" }, [el("h3", { text: "Filter" })]), left]),
        el("section", { class: "debug-pane", "aria-label": "Updates" }, [el("div", { class: "debug-pane__head" }, [el("h3", { text: "Updates" }), count]), banner, tools, list, live]),
        el("section", { class: "debug-pane", "aria-label": "Source" }, [el("div", { class: "debug-pane__head" }, [drawerTitle]), drawer]),
      ]),
    ]);
    S.overlay = el("div", { class: "debug-overlay up-overlay" }, [S.panel]);
    S.overlay.addEventListener("mousedown", (e) => { if (e.target === S.overlay) close(); });
    S.onKey = (e) => {
      if (!S.overlay) return;
      if (document.querySelector("#jui-layer .jui-modal")) return;               // a confirmation owns the keys
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
    S.sig = "";
    render(true);
    const firstChip = S.panel.querySelector(".up-chip");
    if (firstChip) firstChip.focus();
    load(true);
  }

  function close() {
    if (!S.overlay) return;
    document.removeEventListener("keydown", S.onKey, true);
    if (S.overlay.parentNode) S.overlay.parentNode.removeChild(S.overlay);
    const back = S.prevFocus;
    S.overlay = null; S.panel = null; S.onKey = null; S.ref = {}; S.sig = ""; S.drawer = ""; S.filter = "all"; S.drag = ""; S.deferred = false;
    schedule();                                                                  // back to the slow badge poll
    if (back && back.focus && document.contains(back)) { try { back.focus(); } catch (_) { /* gone */ } }
  }

  // The menu badge: one slow read a little after the page loads, then on the slow timer; a hidden tab reads nothing.
  if (typeof document !== "undefined" && typeof document.addEventListener === "function" && document.getElementById) {
    S.timer = setTimeout(() => { if (document.hidden) schedule(); else load(false); }, 4000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !S.busy && Date.now() - S.readAt > POLL.slow) load(false); });
  }

  global.JarvisUpdates = {
    open, close, refreshBadge: () => load(false),
    _pure: {
      ROUTES, GROUPS, GROUP_IDS, INCLUDE_LABEL, SET_KEYS, STARTED_NOTE, NO_UPDATER, AUTO_DIALOG, POLL,
      text, trimmed, num, fmtBytes, fmtAgo, groupOf, rowsOf, providersOf, groupsOf, includedMap, settingsOf, totalCount, chipsOf,
      applyView, isBusy, versionText, metaParts, healthText, headerText, badgeText, blocksOf, splitBlocks, emptyState,
      planAll, confirmAllText, confirmOneText, autoTick, setArgs, pollDelay, signature, checkNotice, applyNotice,
      COLLAPSE_AT, LAYOUT_KEY, LAYOUT_VERSION, MAX_IDS, idKey, newLayout, cleanLayout, layoutToJson, readLayout, writeLayout, isCollapsed,
      noteSeen, setCollapsed, orderBlocks, materializeOrder, moveRelative, moveStep, dropSide, layoutSignature, applySummary, focusFallbacks,
    },
    _flows: makeFlows,
  };
})(typeof window !== "undefined" ? window : globalThis);
