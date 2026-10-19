/* The live Download Hub status card in the chat (Download Hub phase 8, item 17.3-4).
 *
 * `present_download_status` (jarvis-cli/jarvis/actions/download_status_card.py) prints one `download_status`
 * card line; app.js hands its JSON payload to JarvisDownloadCard.render(), and a reloaded conversation hands
 * the saved payload to the same function (thread_extras.py keeps it).  The card follows the downloads over
 * /api/hub/stream (one shared `jarvis hub-admin watch` process feeds every open view) and falls back to
 * polling /api/hub/card.  Its buttons run the same dl_* tools as the Downloads panel, through the same
 * confirmation.  Every string that came from a download is put on the page with textContent only.
 *
 * The formatting half below (constants through rowActions) is copied from the panel's pure half
 * (jarvis-cli/jarvis/actions/hub_ui/tool.js) so the two look the same; tests/verify_hub_panel.js loads both and
 * checks they agree on every state, so the copies can't drift apart unnoticed.
 */
(function (global) {
  "use strict";

  const ACTIVE = new Set(["probing", "connecting", "downloading", "postprocessing"]);

  const FINAL = new Set(["done", "failed", "cancelled"]);

  const APPROX = "\u2248 ";            // "≈ ": the hub estimated this number itself
  const DOT = " \u00B7 ";

  const ACTION_DEFS = {
    pause: { label: "Pause", tool: "dl_pause" },
    resume: { label: "Resume", tool: "dl_resume" },
    retry: { label: "Retry", tool: "dl_retry" },
    open: { label: "Open", tool: "dl_open" },
    reveal: { label: "Show in folder", tool: "dl_open" },
    cancel: { label: "Cancel", tool: "dl_cancel", danger: true },
  };

  const ACTION_ORDER = ["pause", "resume", "retry", "open", "reveal", "cancel"];

  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : null;
  }

  function clampPct(v) {
    const n = num(v);
    return n === null ? null : Math.max(0, Math.min(100, n));
  }

  function fmtWhen(sec, nowMs) {
    const s = num(sec);
    if (s === null) return "";
    const d = new Date(s * 1000);
    const now = new Date(nowMs === undefined || nowMs === null ? Date.now() : nowMs);
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(d) - day(now)) / 86400000);
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (diff === 0) return "today " + time;
    if (diff === 1) return "tomorrow " + time;
    if (diff > 1 && diff < 7) return d.toLocaleDateString([], { weekday: "short" }) + " " + time;
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
  }

  function fmtBytes(n) {
    n = num(n);
    if (n === null || n < 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? String(Math.round(n)) : n.toFixed(n >= 100 ? 0 : 1)) + " " + units[i];
  }

  function fmtSpeed(bps) {
    const n = num(bps);
    return n === null || n <= 0 ? "" : fmtBytes(n) + "/s";
  }

  function fmtEta(s) {
    s = num(s);
    if (s === null || s < 0) return "";
    s = Math.round(s);
    if (s < 60) return s + " s";
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min";
    const m = Math.floor((s % 3600) / 60);
    return Math.floor(s / 3600) + " h " + (m < 10 ? "0" : "") + m + " min";
  }

  function fmtAgo(s) {
    s = num(s);
    if (s === null || s < 0) return "a while ago";
    if (s < 90) return Math.round(s) + " s ago";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }

  function derivedHas(row, key) {
    return Array.isArray(row.derived) && row.derived.indexOf(key) !== -1;
  }

  function isStale(row) {
    return !!row.stale && !FINAL.has(row.state);
  }

  function percentText(row) {
    const p = clampPct(row.percent);
    return p === null ? "" : (derivedHas(row, "percent") ? APPROX : "") + Math.round(p) + "%";
  }

  function speedText(row) {
    const t = fmtSpeed(row.speed_bps);
    return t ? (derivedHas(row, "speed") ? APPROX : "") + t : "";
  }

  function etaText(row) {
    const t = fmtEta(row.eta_s);
    return t ? (derivedHas(row, "eta") ? APPROX : "") + "ETA " + t : "";
  }

  function statusText(row) {
    const s = row.state;
    if (isStale(row)) return "No update " + fmtAgo(row.age_s);
    const parts = [];
    switch (s) {
      case "queued":
        return row.retry_in_s ? "Waiting to retry in " + row.retry_in_s + " s" : "Queued";
      case "probing": return "Checking\u2026";
      case "connecting": return "Connecting\u2026";
      case "downloading":
        [percentText(row), speedText(row), etaText(row)].forEach((t) => { if (t) parts.push(t); });
        return parts.length ? parts.join(DOT) : "Downloading";
      case "postprocessing": {
        const label = row.phase ? row.phase : "Processing";
        const idx = row.phase_index && row.phase_count ? " (" + row.phase_index + "/" + row.phase_count + ")" : "";
        return label.charAt(0).toUpperCase() + label.slice(1) + idx;
      }
      case "paused":
        if (row.scheduled_at) return "Scheduled" + DOT + "starts " + fmtWhen(row.scheduled_at);
        return "Paused" + (percentText(row) ? " at " + percentText(row) : "");
      case "stalled": return "Stalled" + (row.retry_in_s ? " \u2014 retry in " + row.retry_in_s + " s" : "");
      case "failed": return "Failed" + (percentText(row) ? " at " + percentText(row) : "");
      case "cancelled": return "Cancelled";
      case "done": return "Done";
      default: return "Unknown";
    }
  }

  function barLook(row) {
    const pct = clampPct(row.percent);
    if (isStale(row)) return { kind: "stale", pct: pct === null ? 100 : pct };
    switch (row.state) {
      case "done": return { kind: "done", pct: 100 };
      case "failed": return { kind: "failed", pct: pct === null ? 0 : pct };
      case "cancelled": return { kind: "cancelled", pct: 0 };
      case "queued": return { kind: "queued", pct: 0 };
      case "paused": return { kind: "paused", pct: pct === null ? 0 : pct };
      case "stalled": return { kind: "stalled", pct: pct === null ? 0 : pct };
      case "unknown": return { kind: "unknown", pct: pct === null ? 0 : pct };
      case "postprocessing": return { kind: "striped", pct: 100 };
      default: return pct === null ? { kind: "indeterminate", pct: 35 } : { kind: "determinate", pct: pct };
    }
  }

  function rowActions(row) {
    const caps = Array.isArray(row.capabilities) ? row.capabilities : [];
    return ACTION_ORDER.filter((v) => caps.indexOf(v) !== -1).map((v) => Object.assign({ verb: v }, ACTION_DEFS[v]));
  }


  // ======================================================================== the card (DOM, live updates)

  const CARD_VERBS = ["pause", "resume", "retry", "cancel"];
  const RUNNABLE = { dl_pause: 1, dl_resume: 1, dl_retry: 1, dl_cancel: 1, dl_open: 1 };
  const MAX_CARDS = 8;
  const MAX_STREAMS = 3;            // a browser allows ~6 connections per host; the panel needs one too
  const POLL_MS = 3000;
  let liveStreams = 0;

  const ID_RE = /^[A-Za-z0-9_.:\-]{1,120}$/;

  // The verbs a card offers: the row's own capabilities (so a read-only provider row has none), the
  // everyday controls while it is live, and Open / Show in folder once it is done.
  function cardVerbs(row) {
    if (!row || row.card === "group") return [];
    const verbs = rowActions(row).filter((a) => CARD_VERBS.indexOf(a.verb) !== -1 || row.state === "done");
    return verbs;
  }

  // Merge a fresh row over the last one the card knew (the stream sends whole rows; the poll too).
  function mergeRow(prev, next) { return Object.assign({}, prev || {}, next || {}); }

  // Keep only well-formed cards from a payload, in order, at most MAX_CARDS.
  function normalizeCards(payload) {
    const raw = payload && Array.isArray(payload.cards) ? payload.cards : [];
    const out = [];
    raw.forEach((c) => {
      if (!c || typeof c !== "object" || typeof c.id !== "string" || !ID_RE.test(c.id)) return;
      if (out.length < MAX_CARDS) out.push(Object.assign({ card: c.card === "group" ? "group" : "item" }, c));
    });
    return out;
  }

  function groupStatus(card) {
    const c = card.counts || {};
    const bits = [(c.done || 0) + " of " + (c.total || 0) + " done"];
    if (c.running) bits.push(c.running + " running");
    if (c.queued) bits.push(c.queued + " waiting");
    if (c.paused) bits.push(c.paused + " paused");
    if (c.failed) bits.push(c.failed + " failed");
    if (c.stalled) bits.push(c.stalled + " stalled");
    const sp = fmtSpeed(card.speed_bps);
    if (sp && !card.final) bits.push(sp);
    return bits.join(DOT);
  }

  function groupLook(card) {
    if (card.final) return { kind: card.state === "done" ? "done" : "failed", pct: card.state === "done" ? 100 : clampPct(card.percent) || 0 };
    return barLook(card);
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  async function postJson(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) throw new Error((data && (data.error || data.message)) || res.statusText || ("HTTP " + res.status));
    return data;
  }

  // Run one hub tool the way the Downloads panel does: ask the server first whether it needs a confirmation
  // and show it, then run.  Declining runs nothing.  Only the five download controls can be run from here.
  async function runHubTool(name, args) {
    if (!RUNNABLE[name]) throw new Error(name + " can't be run from this card");
    const preview = await postJson("/api/tools/preview", { name: name, arguments: args });
    if (preview && (preview.confirm_required || preview.ai_review)) {
      const ui = window.JarvisUI;
      const ok = ui && typeof ui.confirm === "function"
        ? await ui.confirm({ title: "Run " + name + "?", level: "warn", focusCancel: true, confirmLabel: "Run", body: JSON.stringify(args, null, 2) })
        : false;
      if (!ok) return { declined: true };
    }
    const r = await postJson("/api/tools/run", { name: name, arguments: args });
    return r && r.result ? r.result : r;
  }

  function buildItem(card, state) {
    const root = el("div", "dlc__item");
    const thumb = el("img", "dlc__thumb");
    thumb.alt = "";
    thumb.loading = "lazy";
    thumb.hidden = true;
    const body = el("div", "dlc__body");
    const title = el("div", "dlc__title");
    const meta = el("div", "dlc__meta");
    const track = el("div", "dlc-bar");
    const fill = el("div", "dlc-bar__fill");
    track.appendChild(fill);
    const status = el("div", "dlc__status");
    status.setAttribute("aria-live", "polite");
    const actions = el("div", "dlc__actions");
    const note = el("div", "dlc__note");
    body.append(title, meta, track, status, actions, note);
    root.append(thumb, body);
    const view = { root, thumb, title, meta, track, fill, status, actions, note, row: null, verbs: "" };

    function paint(row) {
      view.row = row;
      const group = row.card === "group";
      root.dataset.state = row.state || "unknown";
      title.textContent = row.title || (group ? "Batch" : "Download");
      title.title = title.textContent;
      const look = group ? groupLook(row) : barLook(row);
      track.className = "dlc-bar " + look.kind;
      fill.style.width = look.pct + "%";
      if (look.kind === "determinate" || look.kind === "done" || look.kind === "failed") {
        track.setAttribute("role", "progressbar");
        track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100");
        track.setAttribute("aria-valuenow", String(Math.round(look.pct)));
      } else track.removeAttribute("role");
      status.textContent = group ? groupStatus(row) : statusText(row);
      const metaBits = [];
      if (!group) {
        const src = row.owner === "hub" ? String(row.engine || "hub").toUpperCase() : (row.provider_label || row.provider || "");
        if (src) metaBits.push(src);
        const size = fmtBytes(row.bytes_total);
        if (size) metaBits.push(size);
      }
      meta.textContent = metaBits.join(DOT);
      if (!group && row.has_thumb && ID_RE.test(row.id)) {
        const url = "/api/hub/thumb/" + encodeURIComponent(row.id);
        if (thumb.getAttribute("src") !== url) thumb.src = url;
        thumb.hidden = false;
      } else thumb.hidden = true;
      const verbs = cardVerbs(row);
      const key = verbs.map((v) => v.verb).join(",");
      if (key !== view.verbs) {
        view.verbs = key;
        actions.textContent = "";
        verbs.forEach((a) => {
          const b = el("button", a.danger ? "is-danger" : "", a.label);
          b.type = "button";
          b.addEventListener("click", async () => {
            b.disabled = true;
            note.textContent = "";
            try {
              const args = a.tool === "dl_open" ? { id: view.row.id, action: a.verb } : { id: view.row.id };
              const r = await runHubTool(a.tool, args);
              if (r && r.declined) note.textContent = "Cancelled.";
              else if (r && r.ok === false) note.textContent = r.error || "That didn't work.";
              if (state && state.poke) state.poke();
            } catch (e) { note.textContent = e.message || "That didn't work."; }
            b.disabled = false;
          });
          actions.appendChild(b);
        });
      }
    }
    paint(card);
    view.paint = paint;
    return view;
  }

  // One live card (a payload of up to MAX_CARDS items / groups).  Returns the element; it follows the
  // downloads by itself, over the shared stream when there is room, else by polling, and stops when every
  // card is final or the element has left the page.
  function render(payload) {
    const cards = normalizeCards(payload);
    const root = el("div", "dlc");
    if (!cards.length) return root;
    const state = { poke: null };
    const views = new Map();
    cards.forEach((c) => {
      const v = buildItem(c, state);
      views.set(c.id, v);
      root.appendChild(v.root);
    });
    const ids = cards.filter((c) => c.card === "item").map((c) => c.id);
    const groups = cards.filter((c) => c.card === "group").map((c) => c.id);
    let stream = null, timer = null, dead = false, polling = false;

    function allFinal() {
      for (const v of views.values()) if (!v.row || !v.row.final && !FINAL.has(v.row.state)) return false;
      return true;
    }
    function apply(rows) {
      rows.forEach((row) => {
        const v = views.get(row.id);
        if (!v) return;
        const merged = mergeRow(v.row, row);
        if (merged.card !== "group") merged.card = "item";
        merged.final = merged.card === "group" ? !!merged.final : FINAL.has(merged.state);
        v.paint(merged);
      });
      if (allFinal()) stop();
    }
    function stop() {
      dead = true;
      if (stream) { stream.close(); stream = null; liveStreams = Math.max(0, liveStreams - 1); }
      clearTimeout(timer);
    }
    async function pollOnce() {
      if (dead) return;
      if (!root.isConnected && root.dataset.seen) return stop();
      if (root.isConnected) root.dataset.seen = "1";
      try {
        const q = [];
        if (ids.length) q.push("ids=" + encodeURIComponent(ids.join(",")));
        if (groups.length) q.push("group=" + encodeURIComponent(groups[0]));
        const res = await fetch("/api/hub/card?" + q.join("&"));
        const data = await res.json();
        if (data && data.ok && Array.isArray(data.cards)) apply(data.cards);
        else if (res.status === 400 || res.status === 404) { stop(); return; }
      } catch (_) { /* try again */ }
      if (!dead) timer = setTimeout(pollOnce, POLL_MS);
    }
    function startPolling() {
      if (polling || dead) return;
      polling = true;
      pollOnce();
    }
    function startStream() {
      if (typeof EventSource !== "function" || liveStreams >= MAX_STREAMS) return startPolling();
      const q = [];
      if (ids.length) q.push("ids=" + encodeURIComponent(ids.join(",")));
      if (groups.length) q.push("group=" + encodeURIComponent(groups[0]));
      stream = new EventSource("/api/hub/stream?" + q.join("&"));
      liveStreams += 1;
      const onRows = (ev) => {
        if (!root.isConnected && root.dataset.seen) return stop();
        if (root.isConnected) root.dataset.seen = "1";
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        apply((msg && (msg.items || msg.upsert)) || []);
        // a group card follows its members, which the stream filters by group id; refresh its counts by poll
        if (groups.length && !dead) pollGroups();
      };
      stream.addEventListener("snapshot", onRows);
      stream.addEventListener("delta", onRows);
      stream.addEventListener("down", () => { if (stream) { stream.close(); stream = null; liveStreams = Math.max(0, liveStreams - 1); } startPolling(); });
      stream.onerror = () => { if (stream && stream.readyState === 2) { stream = null; liveStreams = Math.max(0, liveStreams - 1); startPolling(); } };
    }
    let groupBusy = false;
    async function pollGroups() {
      if (groupBusy) return;
      groupBusy = true;
      try {
        const res = await fetch("/api/hub/card?group=" + encodeURIComponent(groups[0]));
        const data = await res.json();
        if (data && data.ok) apply(data.cards.filter((c) => c.card === "group"));
      } catch (_) { /* next tick */ } finally { setTimeout(() => { groupBusy = false; }, 1500); }
    }
    state.poke = () => { if (!dead) { if (stream) { /* the stream will say */ } else { clearTimeout(timer); timer = setTimeout(pollOnce, 300); } } };
    if (!allFinal()) {
      // the element is not in the page yet when render() returns; start once it is (or at the next tick)
      setTimeout(() => { if (!dead) startStream(); }, 0);
    }
    root._dlcStop = stop;
    return root;
  }

  const api = { render: render, restore: render, _pure: {
    statusText: statusText, barLook: barLook, rowActions: rowActions, cardVerbs: cardVerbs, mergeRow: mergeRow,
    normalizeCards: normalizeCards, groupStatus: groupStatus, groupLook: groupLook, fmtBytes: fmtBytes,
    fmtSpeed: fmtSpeed, fmtEta: fmtEta, fmtAgo: fmtAgo, percentText: percentText, ACTION_DEFS: ACTION_DEFS,
  } };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else global.JarvisDownloadCard = api;
})(typeof window !== "undefined" ? window : globalThis);
