/* ============================================================================
 * slash-palette.js — the "/" command palette in the Ask composer.
 * Master plan Part I.2. Engine + UI over slash-commands-data.js; this file
 * hardcodes no verb names, aliases, or argument lists of its own beyond the
 * handler table (verb -> what it does), which the coverage test can't see
 * but a missing entry surfaces immediately as "no handler for /x" in the
 * console at load time.
 *
 * HOW IT PLUGS IN
 *   - app.js's ask-form submit handler calls JarvisSlash.handleSubmit()
 *     first; it returns {status: "handled" | "blocked" | "passthrough"}.
 *   - app.js's #ask-input keydown handler calls
 *     JarvisSlash.handleComposerKeydown() first; it returns true when it
 *     fully handled the key itself.
 *   - Everything stateful (which chat is open, the websocket, opening
 *     panels) goes through window.JarvisHost, defined at the bottom of
 *     app.js. Read-only lists (skills, daemons, providers) are fetched
 *     directly here, the same "own small fetch helper" pattern daemons.js
 *     and test-checklist.js use.
 *
 * GRAMMAR (I.2.6)
 *   - Only a "/" at column 0 of a single-line, quote-free message starts a
 *     command. "/etc/hosts is ..." (a second slash in the first token) and
 *     anything multi-line are prose. "//" is the escape hatch: it sends the
 *     text with one slash stripped.
 *   - Exact verb or alias only; a prefix never auto-resolves on submit.
 *   - Unknown-but-close (edit distance <= 2, 3+ chars) is BLOCKED once with
 *     a "did you mean" — an identical second Enter sends it as a message.
 *   - Unknown and not close, it's just a message.
 *
 * KEYS (popover open)
 *   Up/Down/PageUp/PageDown/Home/End move · Tab completes the highlighted
 *   row · Enter completes it if the text isn't a finished command yet,
 *   runs it if it is · Esc closes (a second Esc does whatever it did before)
 * ============================================================================
 */
(function (global) {
  "use strict";

  const REG = global.JARVIS_SLASH_COMMANDS;
  if (!REG || !Array.isArray(REG.verbs)) {
    console.warn("slash-palette.js: slash-commands-data.js didn't load; the / palette is off.");
    return;
  }

  // -------------------------------------------------------------------------
  // Registry index
  // -------------------------------------------------------------------------
  const VERBS = REG.verbs;
  const GROUPS = REG.groups;
  const NOT_EXPOSED = REG.notExposed || {};
  const PASSTHROUGH = REG.passthrough || {};   // runnable as /<cli-name> [args]
  const BY_TOKEN = new Map();            // verb or alias (lowercase) -> spec
  for (const spec of VERBS) {
    BY_TOKEN.set(spec.verb.toLowerCase(), spec);
    for (const a of spec.aliases || []) BY_TOKEN.set(a.toLowerCase(), spec);
  }
  const ALL_TOKENS = Array.from(BY_TOKEN.keys()).concat(Object.keys(PASSTHROUGH));

  // The first Enter only COMPLETES (a second Enter runs) for the chat group
  // and for anything that isn't plain "safe", so a half-typed "/s" can never
  // fire /stop and "/cl" can never wipe a conversation. Everything else that
  // takes no argument (panel openers, /skills, /help, ...) runs on the first
  // Enter, matching the plan's "instant" column.
  function runsOnFirstEnter(spec) {
    return spec.instant && spec.riskTier === "safe" && !spec.confirm && spec.group !== "chat";
  }

  const STATIC_LISTS = {
    thinkLevels: [
      { value: "off", detail: "no extended thinking" },
      { value: "low", detail: "a little" },
      { value: "medium", detail: "a fair amount" },
      { value: "high", detail: "as much as it takes" },
      { value: "show", detail: "show the reasoning trace" },
      { value: "hide", detail: "hide the reasoning trace" },
    ],
    layouts: [
      { value: "classic", detail: "the full multi-panel layout" },
      { value: "focus", detail: "just the chat" },
    ],
    daemonActions: [
      { value: "start", detail: "start a stopped service" },
      { value: "stop", detail: "stop it (asks first)" },
      { value: "restart", detail: "stop, then start (asks first)" },
      { value: "status", detail: "show whether it's up" },
    ],
  };

  // -------------------------------------------------------------------------
  // Tiny helpers (own copies, same as daemons.js / test-checklist.js)
  // -------------------------------------------------------------------------
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children || [])) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  async function apiGet(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }

  async function apiSend(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }

  const host = () => global.JarvisHost;
  const toast = (msg, kind) => { const h = host(); if (h) h.toast(msg, kind || "error"); };
  const info = (msg) => toast(msg, "info");

  // -------------------------------------------------------------------------
  // Matching: fuzzy rank + edit distance
  // -------------------------------------------------------------------------
  // exact 100 > prefix 80 > word-prefix 60 > substring 40 > subsequence 20.
  function score(query, text) {
    const q = query.toLowerCase();
    const t = String(text || "").toLowerCase();
    if (!q) return 1;
    if (t === q) return 100;
    if (t.startsWith(q)) return 80;
    if (t.split(/[\s\-_/.]+/).some((w) => w.startsWith(q))) return 60;
    if (t.includes(q)) return 40;
    let i = 0;
    for (const ch of t) if (ch === q[i] && ++i === q.length) return 20;
    return 0;
  }

  function editDistance(a, b) {
    const m = a.length, n = b.length;
    if (!m) return n;
    if (!n) return m;
    let prev = Array.from({ length: n + 1 }, (_, j) => j);
    for (let i = 1; i <= m; i++) {
      const cur = [i];
      for (let j = 1; j <= n; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      prev = cur;
    }
    return prev[n];
  }

  function nearestVerb(token) {
    if (token.length < 3) return null;
    let best = null, bestD = 3;
    for (const t of ALL_TOKENS) {
      const d = editDistance(token, t);
      if (d < bestD) { bestD = d; best = t; }
    }
    return best;
  }

  // Recent use breaks ranking ties (I.2.5): a small MRU of verb ids and
  // skill / saved-command names — never chat titles — in localStorage.
  const MRU_KEY = "jarvis.slash.mru";
  function mruLoad() {
    try {
      const a = JSON.parse(global.localStorage.getItem(MRU_KEY) || "[]");
      return Array.isArray(a) ? a : [];
    } catch { return []; }
  }
  function mruBump(id) {
    try {
      const a = mruLoad().filter((x) => x !== id);
      a.unshift(id);
      global.localStorage.setItem(MRU_KEY, JSON.stringify(a.slice(0, 30)));
    } catch { /* storage blocked: ranking just loses recency */ }
  }
  function mruRank(id) {
    const i = mruLoad().indexOf(id);
    return i === -1 ? 1e6 : i;
  }

  // The characters of `text` that matched `query`, wrapped in .slash-hl.
  // Real DOM nodes, never innerHTML (I.2.5).
  function emphasise(text, query) {
    const t = String(text);
    const q = (query || "").toLowerCase();
    if (!q) return [document.createTextNode(t)];
    const lc = t.toLowerCase();
    let marks = null;
    const at = lc.indexOf(q);
    if (at !== -1) marks = [[at, at + q.length]];
    else if (q.length >= 3) {                                 // subsequence
      const idx = [];
      let i = 0;
      for (let k = 0; k < lc.length && i < q.length; k++) if (lc[k] === q[i]) { idx.push(k); i++; }
      if (i === q.length) marks = idx.map((k) => [k, k + 1]);
    }
    if (!marks) return [document.createTextNode(t)];
    const out = [];
    let pos = 0;
    for (const [a, b] of marks) {
      if (a > pos) out.push(document.createTextNode(t.slice(pos, a)));
      out.push(el("span", { class: "slash-hl", text: t.slice(a, b) }));
      pos = b;
    }
    if (pos < t.length) out.push(document.createTextNode(t.slice(pos)));
    return out;
  }

  // Whitespace split that keeps "quoted phrases" and --k="v w" together.
  // Used for passthrough arguments, which go to the CLI as an argv array.
  function shellSplit(text) {
    const toks = String(text).match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
    return toks.map((t) => t.replace(/"([^"]*)"|'([^']*)'/g, (_, a, b) => (a !== undefined ? a : b)));
  }

  function ago(ts) {
    if (ts == null || ts === "") return "";
    const ms = typeof ts === "number" ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts);
    if (!Number.isFinite(ms)) return "";
    const sec = Math.max(0, (Date.now() - ms) / 1000);
    if (sec < 60) return "just now";
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    if (sec < 7 * 86400) return `${Math.floor(sec / 86400)}d ago`;
    return new Date(ms).toLocaleDateString();
  }

  // -------------------------------------------------------------------------
  // Parsing
  // -------------------------------------------------------------------------
  // Returns null when the text isn't a command at all (prose).
  function parse(raw) {
    if (typeof raw !== "string" || !raw.startsWith("/")) return null;
    if (raw.startsWith("//") || raw.includes("\n")) return null;
    const body = raw.slice(1);
    const m = /^(\S*)(\s*)([\s\S]*)$/.exec(body);
    const token = m[1];
    if (token.includes("/")) return null;                  // a path, not a command
    const hasSpace = m[2].length > 0;
    if (!token && hasSpace) return null;                   // "/ foo" is prose
    return { token, tokenLc: token.toLowerCase(), hasSpace, rest: m[3] };
  }

  // Split the text after the verb into per-argument values. Leading args are
  // single whitespace-delimited tokens; the last arg takes the remainder
  // (so a chat title or file path can contain spaces).
  function splitArgs(spec, rest) {
    const n = spec.args.length;
    if (n <= 1) return { values: [rest], active: 0 };
    const values = [];
    let remaining = rest;
    for (let i = 0; i < n - 1; i++) {
      const m = /^(\S*)(\s+)?([\s\S]*)$/.exec(remaining);
      values.push(m[1]);
      if (!m[2]) return { values, active: i };            // still typing this one
      remaining = m[3];
    }
    values.push(remaining);
    return { values, active: n - 1 };
  }

  // -------------------------------------------------------------------------
  // Argument sources -> rows
  // Row: {value, label, detail, tag, disabled, why, current}
  // -------------------------------------------------------------------------
  const cache = {};   // source -> {at, rows}
  const CACHE_MS = 4000;

  async function fetchRows(source, fresh) {
    const hit = cache[source];
    if (!fresh && hit && Date.now() - hit.at < CACHE_MS) return hit.rows;
    const h = host();
    const st = h ? h.state() : {};
    let rows = [];
    let failed = false;
    try {
      switch (source) {
        case "chats": {
          const convs = (h && (await h.loadConversations())) || st.conversations || [];
          const titles = {};
          for (const c of convs) titles[(c.title || "").toLowerCase()] = (titles[(c.title || "").toLowerCase()] || 0) + 1;
          rows = convs.map((c) => {
            const title = c.title || "(untitled)";
            const dupe = titles[title.toLowerCase()] > 1;
            const open = c.id === st.activeConversationId;
            return {
              value: dupe ? c.id : title,
              label: title,
              detail: ago(c.updated_at),
              id: c.id,
              current: open,
              tag: open ? "open" : ((h && h.originLabel && h.originLabel(c.origin)) || ""),
            };
          });
          break;
        }
        case "commands": {
          const cmds = st.commands || {};
          rows = Object.keys(cmds).sort().map((name) => {
            const vars = (cmds[name] && cmds[name].vars) || {};
            // Same rule as app.js's hasRequiredVars(): a var with no
            // {default: ...} must be supplied.
            const names = Object.keys(vars);
            return {
              value: name, label: name,
              detail: names.map((v) => (hasDefault(vars[v]) ? `--${v}=${vars[v].default}` : `--${v} REQUIRED`)).join("  "),
              tag: names.some((v) => !hasDefault(vars[v])) ? "needs args" : "",
            };
          });
          break;
        }
        case "providers": {
          let list = st.aiProviders || [];
          if (!list.length) {
            const data = await apiGet("/api/ai/providers");
            list = Array.isArray(data.providers) ? data.providers : [];
          }
          rows = [{ value: "auto", label: "auto", detail: "try every eligible provider, as usual", current: !(st.providerOverride || []).length }]
            .concat(list.map((pr) => ({
              value: pr.name, label: pr.name,
              detail: pr.model || pr.summary || "",
              current: (st.providerOverride || []).includes(pr.name),
              disabled: pr.enabled === false || pr.available === false,
              why: "not configured",
            })));
          break;
        }
        case "installedSkills": {
          const q = st.activeConversationId ? `?conversationId=${encodeURIComponent(st.activeConversationId)}` : "";
          const [data, ld] = await Promise.all([
            apiGet("/api/skills"),
            apiGet("/api/skills/loaded" + q).catch(() => ({ loaded: [] })),
          ]);
          const loaded = new Set(ld.loaded || []);
          rows = (data.skills || []).map((sk) => ({
            value: sk.name, label: sk.name,
            detail: sk.valid ? (sk.description || "") : (sk.error || "invalid skill"),
            tag: loaded.has(sk.name) ? "loaded" : (sk.references && sk.references.length ? `${sk.references.length} ref` : ""),
            current: loaded.has(sk.name),
            disabled: !sk.valid,
            why: sk.error || "invalid skill",
          }));
          break;
        }
        case "loadedSkills": {
          const q = st.activeConversationId ? `?conversationId=${encodeURIComponent(st.activeConversationId)}` : "";
          const data = await apiGet("/api/skills/loaded" + q);
          rows = (data.loaded || []).map((n) => ({ value: n, label: n, detail: "loaded" }));
          rows.push({ value: "--all", label: "--all", detail: "drop every manually-loaded skill", tag: "bulk" });
          break;
        }
        case "daemonIds": {
          const data = await apiGet("/api/daemons");
          rows = (data.daemons || []).map((d) => ({
            value: d.id, label: d.id,
            detail: d.name && d.name !== d.id ? d.name : "",
            tag: d.running ? "running" : (d.status || "stopped"),
            dot: d.running ? "up" : "down",
            // Same "is it actually up" test daemons.js's isStoppable() uses.
            live: Boolean(d.running || d.supervisor_pid),
          }));
          break;
        }
        case "capacityModes": {
          let opts = st.modeOptions || [];
          if (!opts.length) {
            const data = await apiGet("/api/mode");
            opts = Array.isArray(data.options) ? data.options : [];
          }
          rows = opts.map((o) => ({ value: o.mode, label: o.mode, detail: o.summary || o.label || "", current: o.mode === st.capacityMode }));
          break;
        }
        case "helpVerbs":
          rows = VERBS.map((sp) => ({ value: sp.verb, label: sp.verb, detail: sp.summary }));
          break;
        case "thinkLevels":
          rows = STATIC_LISTS.thinkLevels.map((r) => ({
            label: r.value, ...r,
            current: r.value === st.thinkLevel || (r.value === "show" && st.thinkShow) || (r.value === "hide" && !st.thinkShow),
          }));
          break;
        case "layouts":
        case "daemonActions":
          rows = STATIC_LISTS[source].map((r) => ({ label: r.value, ...r, current: source === "layouts" && r.value === st.layout }));
          break;
        default:
          rows = [];
      }
    } catch (e) {
      failed = true;
      // Selectable, not disabled: Enter / click retries (I.2.5 "Error" state).
      rows = [{ value: "", label: "Couldn't load the list", detail: e.message, tag: "retry", retrySource: source }];
    }
    if (!failed) cache[source] = { at: Date.now(), rows };
    return rows;
  }

  function hasDefault(v) {
    return Boolean(v && typeof v === "object" && "default" in v);
  }

  function cachedRows(source) {
    return cache[source] ? cache[source].rows : null;
  }

  function matchRow(rows, text) {
    const t = text.trim().toLowerCase();
    if (!t) return null;
    return rows.find((r) => !r.disabled && !r.retrySource && (String(r.value).toLowerCase() === t || String(r.label).toLowerCase() === t
      || (r.id && String(r.id).toLowerCase() === t))) || null;
  }

  // -------------------------------------------------------------------------
  // Completeness (synchronous, from cached lists) — drives Enter's
  // "complete vs. run" decision. handleSubmit() re-resolves everything
  // itself with fresh data, so a stale/missing cache can only ever make
  // Enter complete instead of run, never run the wrong thing.
  // -------------------------------------------------------------------------
  function isComplete(raw) {
    const p = parse(raw);
    if (!p) return false;
    const spec = BY_TOKEN.get(p.tokenLc);
    if (!spec) return Boolean(PASSTHROUGH[p.tokenLc]);   // free-text args, none required
    const required = spec.args.filter((a) => a.required);
    if (!required.length) return true;
    if (!p.hasSpace) return false;
    const { values } = splitArgs(spec, p.rest);
    for (let i = 0; i < spec.args.length; i++) {
      const a = spec.args[i];
      const v = (values[i] || "").trim();
      if (!a.required) continue;
      if (!v) return false;
      if (a.source === "freeText") continue;
      if (a.source === "providers") {
        const rows = cachedRows("providers");
        if (!rows) return false;
        if (!v.split(",").map((s) => s.trim()).filter(Boolean).every((n) => matchRow(rows, n))) return false;
        continue;
      }
      const rows = STATIC_LISTS[a.source]
        ? STATIC_LISTS[a.source].map((r) => ({ value: r.value, label: r.value }))
        : cachedRows(a.source);
      if (!rows || !matchRow(rows, v)) return false;
    }
    return true;
  }

  // -------------------------------------------------------------------------
  // Popover state + rendering
  // -------------------------------------------------------------------------
  const ui = {
    root: null, list: null, foot: null, input: null,
    open: false, rows: [], active: 0, mode: "verbs", requestId: 0,
    lastNearMiss: null, showAll: false, sr: null, srText: "",
  };

  function ensureDom() {
    if (ui.root) return true;
    ui.root = document.getElementById("slash-palette");
    ui.input = document.getElementById("ask-input");
    if (!ui.root || !ui.input) return false;
    ui.list = el("div", { class: "slash-palette__list", role: "listbox", id: "slash-palette-list", "aria-label": "Commands" });
    ui.foot = el("div", { class: "slash-palette__foot", "aria-live": "polite" });
    // A polite live region that says "N commands" / "no matches" (I-B8); the
    // footer's own aria-live announces the highlighted row's description.
    ui.sr = el("div", { class: "slash-sr", role: "status", "aria-live": "polite" });
    ui.root.appendChild(ui.list);
    ui.root.appendChild(ui.foot);
    ui.root.appendChild(ui.sr);
    // pointerdown + preventDefault so the textarea keeps focus and blur can't
    // close the popover out from under the pick (mouse and touch alike).
    // Picking a row only ever FILLS the input; it never runs anything.
    ui.list.addEventListener("pointerdown", (e) => {
      const rowEl = e.target.closest(".slash-row");
      if (!rowEl) return;
      e.preventDefault();
      const idx = Number(rowEl.dataset.index);
      if (Number.isInteger(idx) && ui.rows[idx] && !ui.rows[idx].disabled) {
        ui.active = idx;
        applyActive();
      }
    });
    // Chrome fires a synthetic mousemove when a scroll (e.g. our own
    // scrollIntoView on an arrow key) moves rows under a stationary pointer;
    // without this guard that would snap the highlight back to whatever is
    // under the cursor and fight the keyboard.
    let lastX = -1, lastY = -1;
    ui.list.addEventListener("mousemove", (e) => {
      if (e.clientX === lastX && e.clientY === lastY) return;
      lastX = e.clientX; lastY = e.clientY;
      const rowEl = e.target.closest(".slash-row");
      if (!rowEl) return;
      const idx = Number(rowEl.dataset.index);
      if (idx !== ui.active && ui.rows[idx] && !ui.rows[idx].heading) setActive(idx, false);
    });
    return true;
  }

  function riskBadge(tier) {
    return el("span", { class: `slash-badge slash-badge--${tier}`, text: tier, title: `${tier} risk` });
  }

  function setOpen(open) {
    if (!ensureDom()) return;
    ui.open = open;
    ui.root.hidden = !open;
    ui.input.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) {
      ui.input.removeAttribute("aria-activedescendant");
      ui.requestId++;                                       // drop any in-flight fetch
      ui.showAll = false;
    }
  }

  function setActive(idx, scroll) {
    ui.active = idx;
    ui.list.querySelectorAll(".slash-row").forEach((node) => {
      const on = Number(node.dataset.index) === idx;
      node.classList.toggle("is-active", on);
      node.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        ui.input.setAttribute("aria-activedescendant", node.id);
        if (scroll !== false) node.scrollIntoView({ block: "nearest" });
      }
    });
    renderFoot();
  }

  function selectable(i) {
    const r = ui.rows[i];
    return r && !r.heading;
  }

  function move(delta) {
    if (!ui.rows.length) return;
    let i = ui.active;
    for (let step = 0; step < ui.rows.length; step++) {
      i = (i + delta + ui.rows.length) % ui.rows.length;
      if (selectable(i)) break;
    }
    setActive(i, true);
  }

  function edge(last) {
    const order = ui.rows.map((_, i) => i).filter(selectable);
    if (order.length) setActive(last ? order[order.length - 1] : order[0], true);
  }

  function firstSelectable() {
    const i = ui.rows.findIndex((r) => !r.heading && !r.disabled);
    return i === -1 ? ui.rows.findIndex((r) => !r.heading) : i;
  }

  function renderRows() {
    ui.list.innerHTML = "";
    ui.rows.forEach((r, i) => {
      if (r.heading) {
        ui.list.appendChild(el("div", { class: "slash-heading", role: "presentation", text: r.heading }));
        return;
      }
      const node = el("div", {
        class: "slash-row" + (r.disabled ? " is-disabled" : "") + (r.current ? " is-current" : "") + (r.toggle ? " is-toggle" : ""),
        role: "option", id: `slash-row-${i}`, "data-index": String(i),
        "aria-selected": "false", "aria-disabled": r.disabled ? "true" : null,
        title: r.title || (r.disabled && r.why ? r.why : null),
      }, [
        r.dot ? el("span", { class: `slash-dot slash-dot--${r.dot}`, "aria-hidden": "true" }) : null,
        el("span", { class: "slash-row__name" }, r.hl ? emphasise(r.label, r.hl) : [r.label]),
        r.alias ? el("span", { class: "slash-row__alias", text: r.alias }) : null,
        r.detail ? el("span", { class: "slash-row__detail", text: r.detail }) : null,
        r.tag ? el("span", { class: "slash-row__tag", text: r.tag }) : null,
        r.tier ? riskBadge(r.tier) : null,
      ]);
      ui.list.appendChild(node);
    });
  }

  // "Loads pdf for this chat until you unload it." — what Enter will do,
  // with the arguments typed so far filled in ({0}, {1} in the registry).
  function fillPreview(spec, values) {
    return (spec.preview || spec.summary).replace(/\{(\d)\}/g, (_, i) => {
      const v = (values[i] || "").trim();
      return v || `<${(spec.args[i] || {}).name || "arg"}>`;
    });
  }

  function previewFor(r) {
    if (!r || r.heading) return "";
    const p = parse(ui.input.value);
    if (r.pass) {
      const typed = p && p.tokenLc === r.pass ? p.rest.trim() : "";
      const e = PASSTHROUGH[r.pass];
      return `Runs "jarvis ${r.pass}${typed ? " " + typed : ""}" and shows what it prints.` +
        (e.riskTier === "dangerous" ? " Asks first; it can't be undone." : "");
    }
    if (r.spec) {
      const values = [];
      if (r.argIndex != null && p) {
        splitArgs(r.spec, p.rest).values.forEach((v, i) => { values[i] = v; });
        values[r.argIndex] = r.value;
      } else if (p && BY_TOKEN.get(p.tokenLc) === r.spec) {
        splitArgs(r.spec, p.rest).values.forEach((v, i) => { values[i] = v; });
      }
      return fillPreview(r.spec, values);
    }
    if (r.toggle) return "Lists the commands that aren't verbs: runnable CLI ones, and ones that can't run from chat.";
    return "";
  }

  function renderFoot() {
    const r = ui.rows[ui.active];
    ui.foot.innerHTML = "";
    if (ui.mode === "help" || !r || r.heading) {
      ui.foot.appendChild(el("span", { class: "slash-foot__keys", text: "\u2191\u2193 move \u00b7 Tab complete \u00b7 Esc close" }));
      ui.foot.appendChild(el("span", { class: "slash-foot__local", text: "local \u00b7 no tokens" }));
      return;
    }
    const preview = previewFor(r);
    if (preview) ui.foot.appendChild(el("div", { class: "slash-foot__preview", text: preview }));
    const kids = [];
    if (r.spec && r.argIndex == null) {
      kids.push(riskBadge(r.spec.riskTier));
      if (r.spec.example) kids.push(el("code", { class: "slash-foot__example", text: r.spec.example }));
      if (r.spec.confirm) kids.push(el("span", { class: "slash-foot__note", text: "asks before running" }));
      if (r.spec.covers && r.spec.covers.length) {
        kids.push(el("span", { class: "slash-foot__note", text: "covers: " + r.spec.covers.slice(0, 6).join(", ") + (r.spec.covers.length > 6 ? ` +${r.spec.covers.length - 6}` : "") }));
      }
    } else if (r.pass) {
      const e = PASSTHROUGH[r.pass];
      kids.push(el("span", { class: "slash-foot__text" }, [el("strong", { text: "/" + r.pass }), " \u2014 " + e.summary]));
      kids.push(riskBadge(e.riskTier));
      kids.push(el("code", { class: "slash-foot__example", text: e.usage ? `/${r.pass} ${e.usage}` : `/${r.pass}` }));
    } else if (r.notExposedName) {
      const ne = NOT_EXPOSED[r.notExposedName];
      kids.push(el("span", { class: "slash-foot__text" }, [el("strong", { text: r.notExposedName }), " \u2014 can't run from chat: " + ne.reason]));
      kids.push(riskBadge(ne.riskTier));
    } else if (r.disabled) {
      kids.push(el("span", { class: "slash-foot__text", text: r.why || "not available" }));
    } else if (r.detail && !r.toggle) {
      kids.push(el("span", { class: "slash-foot__text", text: r.detail }));
    }
    const runs = (r.spec && runsOnFirstEnter(r.spec) && ui.mode === "verbs") ? "run" : (r.retrySource ? "retry" : (r.toggle ? "toggle" : "complete"));
    kids.push(el("span", { class: "slash-foot__keys", text: `Enter ${runs} \u00b7 Esc close` }));
    kids.push(el("span", { class: "slash-foot__local", text: "local \u00b7 no tokens" }));
    kids.forEach((k) => ui.foot.appendChild(k));
  }

  function announce(rows, mode) {
    const n = rows.filter((r) => !r.heading && !r.disabled && !r.toggle && !r.retrySource).length;
    const text = n
      ? `${n} ${mode === "verbs" ? "command" : "match"}${n === 1 ? "" : (mode === "verbs" ? "s" : "es")}`
      : (mode === "verbs" ? "No matching commands" : "No matches");
    if (text !== ui.srText) { ui.srText = text; ui.sr.textContent = text; }
  }

  function show(rows, mode, keepActive) {
    ui.rows = rows;
    ui.mode = mode;
    renderRows();
    setOpen(true);
    announce(rows, mode);
    const start = keepActive && selectable(ui.active) ? ui.active : firstSelectable();
    setActive(start === -1 ? 0 : start, false);
  }

  // ---- Level 1: verbs ------------------------------------------------------
  function argSummary(spec) {
    return spec.args.length
      ? "args: " + spec.args.map((a) => `<${a.name}>${a.required ? "" : "?"}`).join(" ")
      : "no arguments";
  }

  function verbRow(spec, viaAlias, query) {
    return {
      label: "/" + spec.verb, spec, tier: spec.riskTier, hl: query || "",
      alias: viaAlias ? `alias /${viaAlias}` : (spec.aliases.length ? spec.aliases.map((a) => "/" + a).join(" ") : ""),
      detail: spec.summary,
      title: `${spec.summary}\n${argSummary(spec)}`,
      disabled: false,
    };
  }

  function passRow(name, query) {
    const e = PASSTHROUGH[name];
    return {
      label: "/" + name, pass: name, tier: e.riskTier, tag: "cli", hl: query || "",
      detail: e.summary,
      title: `${e.summary}\n${e.usage ? "args: " + e.usage : "no arguments"}`,
    };
  }

  function blockedRow(name, query) {
    return {
      label: name, notExposedName: name, tier: NOT_EXPOSED[name].riskTier, disabled: true, hl: query || "",
      detail: NOT_EXPOSED[name].reason, why: NOT_EXPOSED[name].reason,
    };
  }

  function verbRows(query) {
    const q = query.toLowerCase();
    const running = (host() && host().state().running) || false;
    const rows = [];
    const passNames = Object.keys(PASSTHROUGH).sort();
    const blockedNames = Object.keys(NOT_EXPOSED).sort();
    if (!q) {
      for (const g of GROUPS) {
        const inGroup = VERBS.filter((sp) => sp.group === g.id);
        if (!inGroup.length) continue;
        rows.push({ heading: g.label });
        for (const sp of inGroup) rows.push(withRunning(verbRow(sp), sp, running));
      }
      const others = passNames.length + blockedNames.length;
      rows.push({ heading: "More" });
      rows.push({
        toggle: "all",
        label: ui.showAll ? "Hide the other commands" : `Show the other ${others} commands`,
        detail: `${passNames.length} run from here, ${blockedNames.length} can't`,
        tag: ui.showAll ? "\u25B4" : "\u25BE",
      });
      if (ui.showAll) {
        rows.push({ heading: "CLI commands" });
        for (const n of passNames) rows.push(passRow(n));
        rows.push({ heading: "Can't run from chat" });
        for (const n of blockedNames) rows.push(blockedRow(n));
      }
      return rows;
    }
    const scored = [];
    for (const sp of VERBS) {
      let best = score(q, sp.verb), via = null;
      for (const a of sp.aliases) {
        const sc = score(q, a);
        if (sc > best) { best = sc; via = a; }
      }
      // A weak match in the description, but only a real substring / word
      // start: the subsequence rule is fine for a short name, and useless on
      // a whole sentence (any three letters occur in order somewhere in it),
      // where it would also stop a typo like /nwe from reaching the
      // "did you mean /new?" check.
      if (best === 0) { const d = score(q, sp.summary); if (d >= 40) best = Math.floor(d / 4); }
      if (best > 0) scored.push({ sp, best, via });
    }
    scored.sort((a, b) => b.best - a.best
      || mruRank("v:" + a.sp.verb) - mruRank("v:" + b.sp.verb)
      || VERBS.indexOf(a.sp) - VERBS.indexOf(b.sp));
    for (const { sp, via } of scored) rows.push(withRunning(verbRow(sp, via, q), sp, running));

    // CLI passthrough: a real name match only (not a description match), so a
    // short query doesn't drown the verbs in plumbing.
    const passHits = passNames.map((n) => ({ n, sc: score(q, n) })).filter((x) => x.sc >= 40)
      .sort((a, b) => b.sc - a.sc || a.n.localeCompare(b.n)).slice(0, 8);
    if (passHits.length) {
      rows.push({ heading: "CLI commands" });
      for (const { n } of passHits) rows.push(passRow(n, q));
    }
    // Commands that genuinely can't run from a chat box, only on a specific match.
    if (q.length >= 2) {
      const extra = blockedNames.filter((n) => score(q, n) >= 60).slice(0, 6);
      if (extra.length) {
        rows.push({ heading: "Can't run from chat" });
        for (const n of extra) rows.push(blockedRow(n, q));
      }
    }
    if (!rows.length) {
      rows.push({ label: `No command "/${query}" matches`, detail: "Enter sends it as a message", disabled: true, why: "", empty: true });
    }
    return rows;
  }

  function withRunning(row, spec, running) {
    if (running && !spec.whileReplying) {
      row.disabled = true;
      row.why = "can't run while Jarvis is replying";
    }
    return row;
  }

  // ---- Level 2: arguments --------------------------------------------------
  // `override` lets refresh() paint the last good list immediately and
  // revalidate behind it (I.2.5 "Loading").
  async function argRows(spec, p, override) {
    const { values, active } = splitArgs(spec, p.rest);
    const arg = spec.args[active];
    if (!arg) return [];
    let query = values[active] || "";
    let prefix = "";
    if (arg.source === "providers" && query.includes(",")) {
      const i = query.lastIndexOf(",");
      prefix = query.slice(0, i + 1);
      query = query.slice(i + 1);
    }
    if (arg.source === "freeText") {
      return [{
        label: arg.hint || arg.name, detail: arg.required ? "required" : "optional",
        disabled: true, why: arg.hint || "", freeText: true, spec,
      }];
    }
    const all = override || (await fetchRows(arg.source, false));
    const q = query.trim();
    const mruPrefix = arg.source === "installedSkills" ? "s:" : (arg.source === "commands" ? "c:" : null);
    const ranked = all
      .map((r) => ({ r, s: r.retrySource ? 1 : Math.max(score(q, r.label), score(q, r.value), r.id ? score(q, r.id) : 0, (score(q, r.detail || "") >= 40 ? Math.floor(score(q, r.detail || "") / 4) : 0)) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s || (mruPrefix ? mruRank(mruPrefix + a.r.value) - mruRank(mruPrefix + b.r.value) : 0))
      .map((x) => ({ ...x.r, prefix, argIndex: active, spec, hl: q }));
    if (!ranked.length) {
      const empty = [{ label: q ? `Nothing matches "${q}"` : "Nothing to pick from", detail: arg.hint || "", disabled: true, why: arg.hint || "" }];
      return empty;
    }
    if (arg.source === "loadedSkills" && ranked.length === 1 && ranked[0].value === "--all" && !q) {
      ranked.unshift({ label: "No skills are loaded", detail: "nothing to unload", disabled: true, why: "nothing to unload" });
    }
    ranked.total = all.length;
    ranked.arg = arg;
    return ranked;
  }

  // "/skillload › pick a skill · 3 of 24" (I.2.5)
  function withChip(spec, rows) {
    if (!rows.arg) return rows;
    const shown = rows.filter((r) => !r.heading && !r.disabled && !r.retrySource).length;
    if (!shown) return rows;
    const out = [{ heading: `/${spec.verb} \u203a pick a ${rows.arg.name} \u00b7 ${shown} of ${rows.total}` }].concat(rows);
    return out;
  }

  // Re-evaluate the popover from the textarea's current value.
  async function refresh(keepActive) {
    if (!ensureDom()) return;
    const raw = ui.input.value;
    const p = parse(raw);
    const h = host();
    if (!p || !h) { setOpen(false); return; }
    const id = ++ui.requestId;
    const spec = BY_TOKEN.get(p.tokenLc);
    const pass = spec ? null : PASSTHROUGH[p.tokenLc];

    if ((!spec && !pass) || !p.hasSpace) {
      // Level 1 — the verb is still being typed (or isn't a verb).
      show(verbRows(p.token), "verbs", keepActive);
      return;
    }
    if (pass) {
      show([{
        label: pass.summary, detail: pass.usage ? "args: " + pass.usage : "no arguments",
        pass: p.tokenLc, tier: pass.riskTier, noFill: true,
        title: `${pass.summary}\n${pass.usage ? "args: " + pass.usage : "no arguments"}`,
      }], "args");
      return;
    }
    // Level 2 — the verb is resolved; complete its argument(s).
    if (!spec.args.length) {
      show([{ label: "Press Enter to run", detail: spec.summary, spec, disabled: false, tier: spec.riskTier, noFill: true }], "args");
      return;
    }
    const { active } = splitArgs(spec, p.rest);
    const arg = spec.args[active];
    const stale = arg && cache[arg.source] ? cache[arg.source].rows : null;
    if (stale) show(withChip(spec, await argRows(spec, p, stale)), "args", keepActive);
    else show([{ label: "Loading\u2026", disabled: true, why: "" }], "args");
    if (id !== ui.requestId || !ui.open) return;
    const rows = withChip(spec, await argRows(spec, p));
    if (id !== ui.requestId || !ui.open) return;              // typed past it, or closed
    show(rows, "args", Boolean(stale) || keepActive);
  }

  // ---- Applying a row (Tab / Enter / click) -------------------------------
  function setInputValue(text) {
    ui.input.value = text;
    ui.input.setSelectionRange(text.length, text.length);
    ui.input.dispatchEvent(new Event("input", { bubbles: true }));   // autogrow + refresh
  }

  // Tab and a click only ever FILL. Enter may also run, but only for a verb
  // that takes no argument and is plain-safe (runsOnFirstEnter) — the plan's
  // "instant" column.
  function applyActive(opts) {
    const r = ui.rows[ui.active];
    if (!r || r.heading) return false;
    if (r.toggle) { ui.showAll = !ui.showAll; refresh(true); return true; }
    if (r.retrySource) { delete cache[r.retrySource]; refresh(); return true; }
    if (r.disabled) {
      if (r.why) info(r.why.charAt(0).toUpperCase() + r.why.slice(1));
      return true;
    }
    if (r.pass && ui.mode === "verbs") { setInputValue("/" + r.pass + " "); return true; }
    if (r.spec && ui.mode === "verbs") {
      const spec = r.spec;
      // "Instant" = nothing REQUIRED to fill in. /help has one optional
      // argument, and is still instant: Enter runs it, Tab leaves room to
      // type the optional verb.
      const needsArg = spec.args.some((a) => a.required);
      if (!needsArg && opts && opts.enter && runsOnFirstEnter(spec)) {
        setInputValue("/" + spec.verb);
        document.getElementById("ask-form").requestSubmit();
        return true;
      }
      if (!spec.args.length) {
        setInputValue("/" + spec.verb);
        return true;
      }
      setInputValue("/" + spec.verb + " ");                    // -> Level 2
      return true;
    }
    if (r.noFill) return true;
    if (r.argIndex != null && r.spec) {
      const spec = r.spec;
      const p = parse(ui.input.value);
      const { values } = splitArgs(spec, p ? p.rest : "");
      const before = values.slice(0, r.argIndex).join(" ");
      const lead = "/" + spec.verb + " " + (before ? before + " " : "");
      const hasNext = r.argIndex < spec.args.length - 1;
      setInputValue(lead + (r.prefix || "") + r.value + (hasNext ? " " : ""));
      return true;
    }
    return true;
  }

  // -------------------------------------------------------------------------
  // Keyboard
  // -------------------------------------------------------------------------
  // Returns true when the key was fully handled here.
  function handleComposerKeydown(e) {
    // IME composition (I.2.5): Enter is ignored, and while the palette is up
    // that means app.js's own Enter-to-submit must not fire on it either.
    if (e.isComposing || e.keyCode === 229) return ui.open && e.key === "Enter";
    if (!ui.open) return false;
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); move(1); return true;
      case "ArrowUp": e.preventDefault(); move(-1); return true;
      case "PageDown": e.preventDefault(); for (let i = 0; i < 6; i++) move(1); return true;
      case "PageUp": e.preventDefault(); for (let i = 0; i < 6; i++) move(-1); return true;
      case "Home": if (!ui.rows.length) return false; e.preventDefault(); edge(false); return true;
      case "End": if (!ui.rows.length) return false; e.preventDefault(); edge(true); return true;
      case "Escape":
        e.preventDefault();
        // Stop it here: app.js has a document-level Escape handler that closes
        // the whole Ask panel, and dismissing the palette must not take the
        // panel down with it. (A second Esc, with the palette already closed,
        // never gets this far and closes the panel exactly as before.)
        e.stopPropagation();
        setOpen(false);
        return true;
      case "Tab":
        if (e.shiftKey) { e.preventDefault(); move(-1); return true; }
        if (!ui.rows.some((r) => !r.heading)) return false;
        e.preventDefault();
        applyActive();                                       // Tab never runs anything
        return true;
      case "Enter": {
        if (e.shiftKey) return false;
        if (isComplete(ui.input.value)) {
          const p = parse(ui.input.value);
          const spec = p && BY_TOKEN.get(p.tokenLc);
          // A finished command runs. Exception: a highlighted row that is a
          // *different*, first-Enter verb than what's typed still wins below.
          if (!(ui.mode === "verbs" && ui.rows[ui.active] && ui.rows[ui.active].spec !== spec && runsOnFirstEnter(ui.rows[ui.active].spec))) {
            setOpen(false);
            return false;
          }
        }
        const r = ui.rows[ui.active];
        if (!r || r.heading || (r.disabled && !r.why)) return false;
        e.preventDefault();
        applyActive({ enter: true });
        return true;
      }
      default: return false;
    }
  }

  // -------------------------------------------------------------------------
  // Submit
  // -------------------------------------------------------------------------
  const PASS = { status: "passthrough" };
  const HANDLED = { status: "handled" };
  const BLOCKED = { status: "blocked" };

  async function resolveFromList(source, text, fresh) {
    const rows = await fetchRows(source, fresh);
    return { rows, row: matchRow(rows, text) };
  }

  function parseVarFlags(text) {
    // --name value  /  --name=value ; values may be "quoted".
    const out = {};
    const re = /--([A-Za-z_][\w-]*)(?:=|\s+)("([^"]*)"|'([^']*)'|(\S+))/g;
    let m;
    while ((m = re.exec(text))) out[m[1]] = m[3] ?? m[4] ?? m[5];
    return out;
  }

  const PANEL_VERBS = new Set(["guides", "debug", "checklist", "schedule", "mcp", "ctools", "channels",
    "daemons", "backlog", "logsearch", "setup", "subagents", "notifications", "logs", "config", "skin"]);

  const HANDLERS = {
    async new() { await host().newChat(); },
    async chat({ argText }) {
      const { rows, row } = await resolveFromList("chats", argText, true);
      let hit = row;
      if (!hit) {
        const partial = rows.filter((r) => score(argText, r.label) >= 60);
        if (partial.length === 1) hit = partial[0];
        else if (partial.length > 1) { toast(`"${argText}" matches ${partial.length} chats \u2014 pick one from the list.`); return false; }
      }
      if (!hit) { toast(`No chat called "${argText}".`); return false; }
      await host().selectChat(hit.id);
    },
    async clear() { await host().clearChat(); },
    stop() {
      if (!host().state().running) { info("Nothing is running."); return; }
      host().stop();
    },
    redo() { host().redo(); },
    copy() { host().copyLastReply(); },
    async provider({ argText }) {
      const { rows } = await resolveFromList("providers", "", true);
      const names = argText.split(",").map((s) => s.trim()).filter(Boolean);
      if (names.includes("auto") && names.length > 1) { toast('"auto" can\'t be combined with named providers.'); return false; }
      const bad = names.filter((n) => !matchRow(rows, n));
      if (bad.length) { toast(`Unknown provider${bad.length > 1 ? "s" : ""}: ${bad.join(", ")}.`); return false; }
      host().setProvider(names.join(","));
      info(names[0] === "auto" ? "Provider: auto." : `Provider order: ${names.join(" \u2192 ")}.`);
    },
    async think({ argText }) {
      const level = argText.trim().toLowerCase();
      if (!STATIC_LISTS.thinkLevels.some((r) => r.value === level)) {
        toast("Thinking level must be off, low, medium, high, show, or hide."); return false;
      }
      await host().setThink(level);
      info(level === "show" || level === "hide" ? `Reasoning trace ${level === "show" ? "shown" : "hidden"}.` : `Thinking: ${level}.`);
    },
    async capacity({ argText }) {
      const { rows, row } = await resolveFromList("capacityModes", argText, true);
      if (!row) { toast(`Unknown capacity mode "${argText}". Try: ${rows.map((r) => r.value).join(", ")}.`); return false; }
      await host().setCapacity(row.value);
      info(`Capacity: ${row.value}.`);
    },
    async skillload({ argText }) {
      const { row } = await resolveFromList("installedSkills", argText, true);
      if (!row) { toast(`No installed skill called "${argText}".`); return false; }
      await host().skillSlash("load", row.value);
    },
    async skillunload({ argText }) {
      const t = argText.trim();
      if (t === "--all") { await host().skillSlash("unload", "--all"); return; }
      const { row } = await resolveFromList("loadedSkills", t, true);
      if (!row) { toast(`"${t}" isn't loaded, so there's nothing to unload.`); return false; }
      await host().skillSlash("unload", row.value);
    },
    skillmake() { return host().skillSlash("make", ""); },
    skilladd() { return host().skillSlash("add", ""); },
    skills() { host().openPanel("skills"); },
    async run({ argText }) {
      const st = host().state();
      const names = Object.keys(st.commands || {}).sort((a, b) => b.length - a.length);
      const name = names.find((n) => argText === n || argText.startsWith(n + " "));
      if (!name) { toast(`No saved command called "${argText.split(/\s+/)[0]}".`); return false; }
      const spec = st.commands[name] || {};
      const given = parseVarFlags(argText.slice(name.length));
      const flags = {};
      const missing = [];
      for (const [v, d] of Object.entries(spec.vars || {})) {
        if (given[v] !== undefined) flags[v] = given[v];
        else if (hasDefault(d)) flags[v] = d.default;
        else missing.push(v);
      }
      if (missing.length) { toast(`/run ${name} needs ${missing.map((m) => "--" + m).join(" ")}.`); return false; }
      const unknown = Object.keys(given).filter((k) => !(spec.vars || {})[k]);
      if (unknown.length) { toast(`${name} has no ${unknown.map((u) => "--" + u).join(", ")} option.`); return false; }
      host().runSegments([{ name, flags }]);
    },
    async daemon({ argText, spec }) {
      const m = /^(\S+)\s+(\S+)$/.exec(argText.trim());
      if (!m) { toast("Usage: /daemon <start|stop|restart|status> <id>"); return false; }
      const action = m[1].toLowerCase();
      if (!STATIC_LISTS.daemonActions.some((r) => r.value === action)) {
        toast(`"${m[1]}" isn't a daemon action \u2014 use start, stop, restart, or status.`); return false;
      }
      const { rows, row } = await resolveFromList("daemonIds", m[2], true);
      if (!row) { toast(`No service called "${m[2]}"${rows.length ? "" : " (none are registered)"}.`); return false; }
      // Same rule the Daemons panel itself applies (daemons.js runAction):
      // stop always asks; restart only asks when the service is actually
      // up — restarting one that's already down just starts it.
      if ((spec.confirm || []).includes(action) && (action === "stop" || row.live)) {
        const ok = await host().confirm({
          title: `${action === "stop" ? "Stop" : "Restart"} ${row.value}?`,
          body: action === "stop"
            ? "The service will stop and stay stopped until you start it again."
            : "The service will stop and start again; anything it's doing right now is interrupted.",
          confirmLabel: action === "stop" ? "Stop" : "Restart",
          level: "warn",
        });
        if (!ok) return false;
      }
      try {
        const data = await apiSend("POST", `/api/daemons/${encodeURIComponent(row.value)}/${action}`, {});
        const d = data && (data.daemon || data);
        info((data && data.message) || (action === "status"
          ? `${row.value}: ${d && (d.status || (d.running ? "running" : "stopped")) || "ok"}.`
          : `${row.value}: ${action} sent.`));
      } catch (e) {
        toast(e.message || `Couldn't ${action} ${row.value}.`);
        return false;
      }
    },
    async layout({ argText }) {
      const mode = argText.trim().toLowerCase();
      if (!STATIC_LISTS.layouts.some((r) => r.value === mode)) { toast("Layout must be classic or focus."); return false; }
      await host().setLayout(mode);
    },
    help({ argText }) { showHelp(argText.trim().toLowerCase()); return "keep-open"; },
  };
  for (const v of PANEL_VERBS) HANDLERS[v] = () => host().openPanel(v);
  // organize-json is deliberately NOT here: its rendering is tied to app.js
  // internals, so handleSubmit() validates and then passes it through to the
  // ORGANIZE_JSON_RE branch that already exists there.
  for (const s of VERBS) {
    if (s.verb !== "organize-json" && !HANDLERS[s.verb]) console.warn(`slash-palette.js: no handler for /${s.verb}`);
  }

  function showHelp(token) {
    const input = ui.input;
    const spec = token ? BY_TOKEN.get(token.replace(/^\//, "")) : null;
    if (!spec) {
      input.value = "/";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
      if (token) info(`No command called "${token}".`);
      return;
    }
    input.value = "/" + spec.verb;
    show([
      { heading: `/${spec.verb}` },
      { label: spec.summary, detail: spec.example, spec, tier: spec.riskTier, disabled: false, noFill: true },
      ...spec.args.map((a) => ({ label: `<${a.name}>${a.required ? "" : " (optional)"}`, detail: a.hint || a.source, disabled: false, noFill: true })),
    ], "help");
    input.focus();
  }

  // ---- CLI passthrough (I.2.10) --------------------------------------------
  // `/memory-recall foo`, `/doctor`, `/logs-clear`... Any subcommand in the
  // registry's `passthrough` table. The server re-checks the name against the
  // same table and runs it as an argv array (no shell), so nothing typed here
  // can become anything but arguments to that one command.
  //
  // A non-zero exit is not always "it failed": `jarvis doctor` exits 1 for
  // warnings and 2 for something broken (I-B13). A registry entry can name
  // such codes in `exitCodes: {"1": {label, level}}`. A code with a level
  // other than "error" is shown as a result with that level, not as a
  // failure; a code with level "error" is still a failure, just named.
  function exitMeaning(entry, d) {
    const codes = entry && entry.exitCodes;
    if (!codes || d.code == null) return null;
    const m = codes[String(d.code)];
    return m && typeof m.label === "string" ? { label: m.label, level: m.level || "error" } : null;
  }

  function showResult(name, d, entry) {
    const out = [d.stdout, d.stderr, d.error].filter(Boolean).join("\n").trim();
    const meaning = exitMeaning(entry, d);
    const soft = !d.ok && !!meaning && meaning.level !== "error";   // non-zero, but not a failure
    const failed = !d.ok && !soft;
    const level = soft ? meaning.level : (failed ? "error" : "info");
    if (!out) {
      if (meaning) toast(`/${name}: ${meaning.label} (exit ${d.code}).`, level);
      else info(failed ? `/${name} failed (exit ${d.code == null ? "?" : d.code}).` : `/${name} done.`);
      return;
    }
    if (!failed && out.length <= 120 && !out.includes("\n")) { toast(out, level); return; }
    const h = host();
    const title = `/${name}` + (meaning
      ? ` \u2014 ${meaning.label} (exit ${d.code})`
      : (failed ? ` \u2014 failed${d.code != null ? ` (exit ${d.code})` : ""}` : ""));
    const text = out.length > 20000 ? out.slice(0, 20000) + "\n\u2026 (truncated)" : out;
    if (h && h.dialog) h.dialog({ title, pre: text, level });
    else toast(out.slice(0, 300), level);
  }

  async function runPassthrough(name, entry, argText) {
    const h = host();
    const args = shellSplit(argText);
    // Some commands are deliberately argument-less from the palette (the
    // registry's `maxArgs`): /subagent-keys is list-only so API keys are never
    // typed into a chat box, /sched-clear has no options. The server enforces
    // the same cap; this is the friendly version of that refusal.
    if (Number.isInteger(entry.maxArgs) && args.length > entry.maxArgs) {
      toast(entry.noArgsHint || `/${name} takes ${entry.maxArgs === 0 ? "no arguments" : "at most " + entry.maxArgs}.`, "warn");
      return false;
    }
    if (name === "conv-delete" && args[0] && args[0] === h.state().activeConversationId) {
      toast("That's the chat you have open. Switch to another one first.", "warn");
      return false;
    }
    if (entry.riskTier === "dangerous") {
      const ok = await h.confirm({
        title: `Run /${name}?`,
        body: `${entry.summary} This is a dangerous-tier command.`,
        pre: `jarvis ${name}${args.length ? " " + args.join(" ") : ""}`,
        confirmLabel: "Run", level: "error", focusCancel: true,
      });
      if (!ok) return false;
    }
    const data = await apiSend("POST", "/api/slash/run", { name, args });
    showResult(name, data, entry);
    if (name.startsWith("conv-") && h.refreshChats) h.refreshChats();
    return true;
  }

  async function handleSubmit(input, quotes) {
    if (!ensureDom()) return PASS;
    const raw = input.value;
    if (quotes && quotes.length) return PASS;
    if (raw.startsWith("//") && !raw.includes("\n")) {
      input.value = raw.slice(1);                              // escape hatch
      setOpen(false);
      return PASS;
    }
    const p = parse(raw);
    if (!p) return PASS;
    const spec = BY_TOKEN.get(p.tokenLc);
    const pass = spec ? null : PASSTHROUGH[p.tokenLc];

    if (!spec && !pass) {
      const trimmed = raw.trim();
      const known = NOT_EXPOSED[p.tokenLc];
      const near = known ? null : nearestVerb(p.tokenLc);
      if (!known && !near) return PASS;
      // A near-miss of a command whose arguments are secrets (/subagent-keyz
      // role provider KEY...) must not get the usual "press Enter again to
      // send it as a message": that second Enter would hand the keys to the
      // model and write them into the chat history. Drop the arguments instead.
      const nearEntry = near ? PASSTHROUGH[near] : null;
      if (nearEntry && nearEntry.secret && p.rest.trim()) {
        ui.lastNearMiss = null;
        input.value = "/" + p.token;
        setOpen(false);
        toast(`No command "/${p.token}" \u2014 did you mean /${near}? Its arguments can be secrets, so this wasn't sent to the model. Use \`//${p.token} ...\` if you really mean to send it as a message.`, "warn");
        return BLOCKED;
      }
      if (ui.lastNearMiss === trimmed) { ui.lastNearMiss = null; setOpen(false); return PASS; }   // "Send anyway"
      ui.lastNearMiss = trimmed;
      setOpen(false);
      toast(known
        ? `/${p.token} can't run from chat (${known.reason}). Press Enter again to send it as a message.`
        : `No command "/${p.token}" \u2014 did you mean /${near}? Press Enter again to send it as a message.`, "warn");
      return BLOCKED;
    }
    ui.lastNearMiss = null;
    const h = host();

    if (pass) {
      setOpen(false);
      input.value = "";
      let ran;
      try { ran = await runPassthrough(p.tokenLc, pass, p.rest.trim()); }
      catch (e) { toast(e && e.message ? e.message : `/${p.tokenLc} failed.`); ran = false; }
      if (ran === false) {
        // For a command whose arguments are secrets, never hand them back:
        // leave just the verb in the box.
        input.value = pass.secret && p.rest.trim() ? "/" + p.token : raw;
        refresh();
        return BLOCKED;
      }
      mruBump("v:" + p.tokenLc);
      return HANDLED;
    }

    if (h.state().running && !spec.whileReplying) {
      toast(`/${spec.verb} can't run while Jarvis is replying. Try /stop first.`, "warn");
      return BLOCKED;
    }
    const argText = p.rest.trim();
    const missing = spec.args.find((a, i) => a.required && !(splitArgs(spec, p.rest).values[i] || "").trim());
    if (missing) {
      toast(`/${spec.verb} needs ${missing.hint || missing.name}.`, "warn");
      refresh();
      return BLOCKED;
    }

    if (spec.verb === "organize-json") { setOpen(false); return PASS; }

    setOpen(false);
    input.value = "";
    let result;
    try {
      result = await HANDLERS[spec.verb]({ argText, spec, raw });
    } catch (e) {
      toast(e && e.message ? e.message : `/${spec.verb} failed.`);
      result = false;
    }
    if (result === false) {                                    // didn't run: give the text back
      input.value = raw;
      refresh();
      return BLOCKED;
    }
    mruBump("v:" + spec.verb);
    if (spec.verb === "skillload") mruBump("s:" + argText);
    if (spec.verb === "run") mruBump("c:" + argText.split(/\s+/)[0]);
    return HANDLED;
  }

  // -------------------------------------------------------------------------
  // Wiring
  // -------------------------------------------------------------------------
  function init() {
    if (!ensureDom()) return;
    ui.input.setAttribute("role", "combobox");
    ui.input.setAttribute("aria-autocomplete", "list");
    ui.input.setAttribute("aria-controls", "slash-palette-list");
    ui.input.setAttribute("aria-expanded", "false");
    ui.input.addEventListener("input", () => {
      ui.lastNearMiss = null;
      refresh();
    });
    ui.input.addEventListener("blur", () => setTimeout(() => setOpen(false), 120));
    ui.input.addEventListener("focus", () => { if (parse(ui.input.value)) refresh(); });
    document.addEventListener("pointerdown", (e) => {
      if (ui.open && !ui.root.contains(e.target) && e.target !== ui.input) setOpen(false);
    });
  }

  global.JarvisSlash = { handleSubmit, handleComposerKeydown, parse, score, editDistance, isComplete };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(window);
