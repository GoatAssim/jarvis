(() => {
  "use strict";

  // ===========================================================================
  // Utilities
  // ===========================================================================

  const qs = (sel, root = document) => root.querySelector(sel);
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function highlightTokens(runText) {
    const escaped = escapeHtml(runText);
    return escaped.replace(/\{\{|\}\}|\{[a-zA-Z0-9_]+\}/g, (m) => {
      if (m === "{{" || m === "}}") return m;
      return `<span class="tok">${m}</span>`;
    });
  }

  // Lightweight JSON syntax coloring for the confirmation prompts (the
  // "are you sure" popups shown before a flagged tool/command runs — see
  // showRunConfirmPopup, addAskConfirmBubble, renderResolvedConfirmBubble,
  // debugRenderConfirmPending). Those used to just dump a plain
  // JSON.stringify(..., null, 2) into a <pre>, the same undifferentiated
  // block of text as the Settings tab's read-only raw JSON viewer. This
  // colors keys/strings/numbers/booleans/null so the arguments and
  // resolved command content are actually easy to scan at a glance before
  // approving something. Only ever reads its own escaped output back into
  // innerHTML, so nothing here can inject anything the value itself didn't
  // already contain (already escaped first).
  function jsonSyntaxHtml(value) {
    // Match on the RAW JSON text, not an already-escaped copy — escaping
    // first turns every `"` into `&quot;`, which the string-matching part
    // of this regex would never see. Every match gets escaped individually
    // inside the callback instead; the only characters left untouched are
    // JSON's own structural punctuation/whitespace ({}[]:, and newlines),
    // none of which need HTML-escaping.
    const json = JSON.stringify(value, null, 2);
    return json.replace(
      /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      (match) => {
        let cls = "json-num";
        if (match.startsWith('"')) {
          cls = /:\s*$/.test(match) ? "json-key" : "json-str";
        } else if (match === "true" || match === "false") {
          cls = "json-bool";
        } else if (match === "null") {
          cls = "json-null";
        }
        return `<span class="${cls}">${escapeHtml(match)}</span>`;
      },
    );
  }

  // A confirmation prompt's "Command:" line is sometimes a plain shell
  // string (a saved command's `run`, not JSON) and sometimes a JSON value
  // (resolved_run_for_review's {command, vars, run} object, or a chain's
  // list of those) — color it as JSON only when it actually is some.
  function confirmValuePre(value, className) {
    const html = typeof value === "string" ? escapeHtml(value) : jsonSyntaxHtml(value);
    return el("pre", { class: className, html });
  }

  function describeCondition(cond) {
    if (cond == null) return null;
    if (typeof cond === "string") return cond;
    if (typeof cond === "object") {
      return Object.entries(cond)
        .map(([k, v]) => (Array.isArray(v) ? `${k} in [${v.join(", ")}]` : `${k} == ${v}`))
        .join(" and ");
    }
    return String(cond);
  }

  // ===========================================================================
  // Shared JSON tree component — collapsible, optionally editable.
  //
  // Used by (1) the "organize-json <path>" chat-bubble result and (2) the
  // Settings modal's Config tab. Renders straight from an already-parsed JS
  // value (never re-serializes to send anywhere), so browsing/editing a
  // config file here never touches the AI layer or costs a token.
  // ===========================================================================

  function jsonTreeTypeOf(v) {
    if (v === null) return "null";
    if (Array.isArray(v)) return "array";
    return typeof v; // "object" | "string" | "number" | "boolean"
  }

  function jsonTreeIsContainer(v) {
    return v !== null && typeof v === "object";
  }

  function jsonTreeCountLabel(v) {
    if (Array.isArray(v)) {
      const n = v.length;
      return `[ ]  ${n} item${n === 1 ? "" : "s"}`;
    }
    const n = Object.keys(v).length;
    return `{ }  ${n} key${n === 1 ? "" : "s"}`;
  }

  function jsonTreeScalarLabel(v) {
    if (v === null) return "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "string") return JSON.stringify(v);
    return String(v);
  }

  // Turns whatever text a person typed into an editable leaf back into a
  // proper JS value, the way JSON itself would read it — "true"/"false"/
  // "null"/numbers become their real types, anything else (including text
  // that merely looks numeric-ish but isn't, or fails to parse) stays a
  // plain string so nothing is silently misinterpreted.
  function jsonTreeCoerce(raw) {
    const t = String(raw);
    if (t === "null") return null;
    if (t === "true") return true;
    if (t === "false") return false;
    if (/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(t.trim()) && t.trim() !== "") return Number(t);
    return t;
  }

  // Builds a read-only OR editable collapsible tree for `rootValue` inside
  // `mount`. Editable mode mutates `rootValue` in place (objects/arrays are
  // references) and calls `onChange()` after every structural or leaf edit
  // so the caller can re-derive raw JSON text for saving/Raw-view.
  function buildJsonTree(mount, rootValue, { editable = false, onChange = () => {} } = {}) {
    const collapsedPaths = new Set(); // paths the user explicitly collapsed
    const expandedPaths = new Set();  // paths the user explicitly expanded

    function isOpen(pathKey, depth) {
      if (collapsedPaths.has(pathKey)) return false;
      if (expandedPaths.has(pathKey)) return true;
      return depth < 2; // default: first two levels open, rest start collapsed
    }

    function rerender() {
      mount.innerHTML = "";
      if (jsonTreeIsContainer(rootValue)) {
        mount.appendChild(el("div", { class: "json-tree__meta json-tree__meta--root" }, jsonTreeCountLabel(rootValue)));
        buildChildren(mount, rootValue, "$", 0);
      } else {
        mount.appendChild(el("div", { class: "json-tree__row" }, [
          el("span", { class: `json-tree__val json-tree__val--${jsonTreeTypeOf(rootValue)}` }, jsonTreeScalarLabel(rootValue)),
        ]));
      }
    }

    function startEditValue(container, key, valEl) {
      const current = container[key];
      const input = el("input", {
        class: "json-tree__edit-input",
        type: "text",
        value: jsonTreeTypeOf(current) === "string" ? current : jsonTreeScalarLabel(current),
      });
      valEl.replaceWith(input);
      input.focus();
      input.select();
      const commit = () => {
        container[key] = jsonTreeCoerce(input.value);
        onChange();
        rerender();
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        else if (e.key === "Escape") { e.preventDefault(); rerender(); }
      });
      input.addEventListener("blur", commit);
    }

    function startRenameKey(container, key, keyEl) {
      const input = el("input", { class: "json-tree__edit-input json-tree__edit-input--key", type: "text", value: key });
      keyEl.replaceWith(input);
      input.focus();
      input.select();
      const commit = () => {
        const newKey = input.value.trim();
        if (!newKey || newKey === key) { rerender(); return; }
        if (Object.prototype.hasOwnProperty.call(container, newKey)) {
          toast(`"${newKey}" already exists at this level.`);
          rerender();
          return;
        }
        // Rebuild the object to preserve key order with the rename in place.
        const rebuilt = {};
        for (const k of Object.keys(container)) {
          rebuilt[k === key ? newKey : k] = container[k];
        }
        for (const k of Object.keys(container)) delete container[k];
        Object.assign(container, rebuilt);
        onChange();
        rerender();
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        else if (e.key === "Escape") { e.preventDefault(); rerender(); }
      });
      input.addEventListener("blur", commit);
    }

    function buildAddControls(value, pathKey, depth) {
      const wrap = el("div", { class: "json-tree__row json-tree__add-row", style: `padding-left:${(depth + 1) * 16}px` });
      if (Array.isArray(value)) {
        wrap.appendChild(el("button", {
          type: "button", class: "json-tree__add",
          onclick: () => { value.push(""); expandedPaths.add(pathKey); onChange(); rerender(); },
        }, "+ add item"));
      } else {
        wrap.appendChild(el("button", {
          type: "button", class: "json-tree__add",
          onclick: () => {
            let base = "new_key", n = 1, name = base;
            while (Object.prototype.hasOwnProperty.call(value, name)) name = `${base}_${n++}`;
            value[name] = "";
            expandedPaths.add(pathKey);
            onChange();
            rerender();
          },
        }, "+ add key"));
      }
      return wrap;
    }

    function buildChildren(container, value, pathKey, depth) {
      const isArr = Array.isArray(value);
      const keys = isArr ? value.map((_, i) => i) : Object.keys(value);
      for (const key of keys) {
        const childPathKey = `${pathKey}.${key}`;
        const child = value[key];
        const childIsContainer = jsonTreeIsContainer(child);
        const open = childIsContainer ? isOpen(childPathKey, depth + 1) : false;

        const row = el("div", { class: "json-tree__row", style: `padding-left:${depth * 16}px` });

        const toggle = el("span", {
          class: "json-tree__toggle" + (childIsContainer ? "" : " json-tree__toggle--leaf"),
        }, childIsContainer ? (open ? "\u25be" : "\u25b8") : "\u00b7");
        if (childIsContainer) {
          toggle.addEventListener("click", () => {
            if (isOpen(childPathKey, depth + 1)) {
              expandedPaths.delete(childPathKey);
              collapsedPaths.add(childPathKey);
            } else {
              collapsedPaths.delete(childPathKey);
              expandedPaths.add(childPathKey);
            }
            rerender();
          });
        }
        row.appendChild(toggle);

        const keyEl = el("span", { class: "json-tree__key" }, isArr ? `[${key}]` : String(key));
        if (editable && !isArr) {
          keyEl.classList.add("json-tree__key--editable");
          keyEl.title = "Click to rename";
          keyEl.addEventListener("click", () => startRenameKey(value, key, keyEl));
        }
        row.appendChild(keyEl);
        row.appendChild(el("span", { class: "json-tree__colon" }, ":"));

        if (childIsContainer) {
          row.appendChild(el("span", { class: "json-tree__meta" }, jsonTreeCountLabel(child)));
        } else {
          const valEl = el("span", { class: `json-tree__val json-tree__val--${jsonTreeTypeOf(child)}` }, jsonTreeScalarLabel(child));
          if (editable) {
            valEl.classList.add("json-tree__val--editable");
            valEl.title = "Click to edit";
            valEl.addEventListener("click", () => startEditValue(value, key, valEl));
          }
          row.appendChild(valEl);
        }

        if (editable) {
          row.appendChild(el("button", {
            type: "button", class: "json-tree__del", title: isArr ? "Remove item" : "Remove key",
            onclick: () => {
              if (isArr) value.splice(Number(key), 1);
              else delete value[key];
              onChange();
              rerender();
            },
          }, "\u00d7"));
        }

        container.appendChild(row);

        if (childIsContainer && open) {
          buildChildren(container, child, childPathKey, depth + 1);
          if (editable) container.appendChild(buildAddControls(child, childPathKey, depth + 1));
        }
      }
    }

    rerender();
  }

  let toastTimer = null;
  function toast(message, kind = "error") {
    const t = qs("#toast");
    t.textContent = message;
    t.className = "toast" + (kind === "info" ? " is-info" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
  }

  // Bottom-left popup for a directly-run saved command that's paused on
  // its own confirm_required/ai_review flags (see server.js's
  // "confirm-request" message, fired from cli.py's confirm_direct_command
  // — completely separate from the in-thread ask-confirm bubble, since a
  // direct run has no chat thread to render into). Shows the actual
  // command content (risk_note.command_run) alongside any AI risk note
  // and the command's own safety flags, so the user has real information
  // before approving — not just the bare tool name and typed args.
  function showRunConfirmPopup(tool, args, riskNote) {
    const popup = qs("#run-confirm-popup");
    popup.innerHTML = "";
    popup.hidden = false;

    popup.appendChild(el("div", { class: "run-confirm-popup__title" }, [
      "\u26a0 Run ", el("span", { class: "run-confirm-popup__tool" }, tool || "(unknown command)"), "?",
    ]));

    popup.appendChild(confirmValuePre(args || {}, "run-confirm-popup__args"));

    if (riskNote && riskNote.command_run !== undefined && riskNote.command_run !== null) {
      popup.appendChild(el("div", { class: "run-confirm-popup__risk-label" }, "Command:"));
      popup.appendChild(confirmValuePre(riskNote.command_run, "run-confirm-popup__args"));
    }

    if (riskNote && riskNote.note) {
      const label = riskNote.provider ? `AI review \u2014 ${riskNote.provider}` : "AI review";
      popup.appendChild(el("div", { class: "run-confirm-popup__risk" }, [
        el("div", { class: "run-confirm-popup__risk-label" }, label),
        el("div", { class: "run-confirm-popup__risk-note" }, riskNote.note),
      ]));
    }

    if (riskNote && riskNote.command_flags) {
      const cf = riskNote.command_flags;
      popup.appendChild(el("div", { class: "run-confirm-popup__flags" },
        `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
    }

    const yesBtn = el("button", { class: "btn btn--primary", type: "button" }, "Yes, run it");
    const noBtn = el("button", { class: "btn btn--ghost", type: "button" }, "No, cancel");
    popup.appendChild(el("div", { class: "run-confirm-popup__actions" }, [noBtn, yesBtn]));

    function resolve(approved) {
      popup.hidden = true;
      popup.innerHTML = "";
      wsSend({ type: "confirm-response", approved });
    }
    yesBtn.addEventListener("click", () => resolve(true));
    noBtn.addEventListener("click", () => resolve(false));
  }

  function jarvisTabVisible() {
    return document.visibilityState === "visible";
  }

  function ensureNotifPermission() {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }

  function summarizeText(text) {
    const plain = String(text || "")
      .replace(/[#*_`>+-]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (!plain) return "Finished.";
    const sentence = (plain.match(/^[^.!?]+[.!?]?/) || [plain])[0];
    return sentence.slice(0, 120);
  }

  function notifyIfAway(title, body) {
    if (jarvisTabVisible()) return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title.slice(0, 88), {
        body: (body || "").slice(0, 140),
        tag: "jarvis-task",
        silent: true,
      });
      n.onclick = () => {
        window.focus();
        n.close();
      };
    } catch {
      /* private mode / unsupported */
    }
  }

  function notifyTaskDone(summary, failed) {
    const task = (state.lastTaskLabel || "that").replace(/\s+/g, " ").trim().slice(0, 42) || "that";
    const title = `Your task of doing ${task} is done sir`;
    notifyIfAway(title, failed ? (summary || "It didn't finish cleanly.") : (summary || "All set."));
  }

  // ===========================================================================
  // State
  // ===========================================================================

  const state = {
    commands: {},          // name -> spec
    selected: null,        // currently selected command name
    sequence: [],          // [{name, flags, label}]
    running: false,        // true while ANY child process is in flight (a run OR an ask \u2014 the
                            // server allows only one at a time per connection, see server.js)
    ws: null,
    wsBackoff: 1000,
    editingOriginalName: null, // set when the builder modal is editing an existing command

    // Ask Jarvis \u2014 state for the turn currently streaming in, if any.
    askPendingBubble: null,  // the DOM node for Jarvis's in-progress reply bubble
    askReplyLines: [],       // accumulated (post prefix-strip) lines of that reply, raw (untriaged)
    askTraceBubble: null,    // the DOM node for the current turn's console-dump bubble, if any
    askQuotes: [],           // highlighted excerpts attached to the next ask
    lastTaskLabel: "",       // user request / command name for away notifications
    cmdSearch: "",           // current text in the command-list search field

    // Debug dashboard \u2014 tool catalog is read live from /api/tools, never hardcoded.
    debugTools: [],          // [{name, description, parameters}] as returned by the CLI
    debugLoaded: false,
    debugSelected: null,     // name of the currently selected tool
    debugSearch: "",
    debugResponseMode: "organized", // "organized" | "raw"
    debugLastResult: null,   // last {ok, result, raw, stderr, error} from /api/tools/run
    debugLastResultError: false,
    debugPendingConfirm: null, // {name, arguments, risk_note} awaiting Yes/No before /api/tools/run
    debugLastUsage: null,    // Phase 0 (new_plan.md): last ask-usage payload (see handleWsMessage)
    debugMode: null,          // local-only capacity override for this panel, e.g. "compact" \u2014
                               // never sent to /api/mode, never affects the real global mode
    debugLastUsage: null,    // Phase 0 (new_plan.md): last ask-usage payload (see handleWsMessage)

    // Conversations — every saved chat lives in ~/.jarvis/conversations
    // (see conversations.py); this is just the in-memory mirror for the
    // sidebar list, refreshed from /api/conversations.
    conversations: [],           // [{id,title,soft_context,created_at,updated_at,exchange_count}]
    activeConversationId: null,  // which one the open thread + next ask belong to
    askConversationId: null,     // which conversation the in-flight ask/redo actually belongs to
    convoSearch: "",
    askTraceByConv: {},          // convId -> [{text, cls}] recorded "commands Jarvis runs" lines,
                                  // so switching away and back doesn't lose them (see askPromptLine)
    pendingConfirmByConv: {},    // convId -> {tool, arguments, risk_note, extraItem} for a confirm
                                  // request that arrived while that conversation wasn't being viewed
    threadExtrasByConv: {},      // convId -> [{bucket, type, data}] non-text thread items (screenshots,
                                  // downloads, organize-json results, console dumps, resolved confirms)
                                  // so they survive switching away and back — see pushThreadExtra() and
                                  // renderThreadExtra(). "bucket" is the exchange index they belong
                                  // after (see exchangeCountByConv).
    exchangeCountByConv: {},     // convId -> number of completed (saved) exchanges, used to bucket
                                  // threadExtrasByConv entries against loadConversationIntoThread's replay

    // Logs overlay — conversation-scoped raw model↔backend traffic, read
    // live from /api/logs (see logs.py). Independent of the Ask sidebar's
    // own conversation list/search state above.
    logsLoaded: false,
    logsConvos: [],           // [{id, title, updated_at, exists}] from /api/logs
    logsSearch: "",
    logsSelected: null,       // conv id currently shown in the middle pane
    logsEntries: [],          // entries for logsSelected, oldest first
    logsViewMode: "organized", // "organized" | "raw"
  };

  // ===========================================================================
  // API
  // ===========================================================================

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) {
      const message = (data && data.error) || `${method} ${url} failed (${res.status})`;
      const err = new Error(message);
      err.data = data; // preserves extra fields (e.g. organize-json's line/column/snippet)
      throw err;
    }
    return data;
  }

  const Api = {
    status: () => api("GET", "/api/status"),
    reconnect: () => api("POST", "/api/reconnect"),
    listCommands: () => api("GET", "/api/commands"),
    createCommand: (name, spec) => api("POST", "/api/commands", { name, spec }),
    updateCommand: (oldName, name, spec) => api("PUT", `/api/commands/${encodeURIComponent(oldName)}`, { name, spec }),
    deleteCommand: (name) => api("DELETE", `/api/commands/${encodeURIComponent(name)}`),
    clearAiHistory: (conversationId) => api("POST", "/api/ai/clear", conversationId ? { conversationId } : {}),
    configList: () => api("GET", "/api/config/list"),
    getConfigFile: (name) => api("GET", `/api/config/file/${encodeURIComponent(name)}/raw`),
    putConfigFile: (name, text) => api("PUT", `/api/config/file/${encodeURIComponent(name)}/raw`, { text }),
    organizeJson: (targetPath) => api("POST", "/api/json/organize", { path: targetPath }),
    listTools: () => api("GET", "/api/tools"),
    runTool: (name, arguments_, mode) => api("POST", "/api/tools/run", { name, arguments: arguments_, mode }),
    previewTool: (name, arguments_, mode) => api("POST", "/api/tools/preview", { name, arguments: arguments_, mode }),
    setToolSafety: (name, key, value) => api("POST", "/api/tools/safety", { name, key, value }),
    listConversations: (q) => api("GET", `/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`),
    createConversation: (title) => api("POST", "/api/conversations", title ? { title } : {}),
    getConversation: (id) => api("GET", `/api/conversations/${encodeURIComponent(id)}`),
    deleteConversation: (id) => api("DELETE", `/api/conversations/${encodeURIComponent(id)}`),
    getMode: () => api("GET", "/api/mode"),
    setMode: (mode) => api("POST", "/api/mode", { mode }),
    listLogs: () => api("GET", "/api/logs"),
    getLog: (id, limit) => api("GET", `/api/logs/${encodeURIComponent(id)}${limit ? `?limit=${limit}` : ""}`),
    clearLog: (id) => api("DELETE", `/api/logs/${encodeURIComponent(id)}`),
  };

  // ===========================================================================
  // Boot sequence
  // ===========================================================================

  async function bootLine(text, opts = {}) {
    const holder = qs("#boot-lines");
    const line = el("div", {}, text);
    if (opts.warn) line.style.color = "var(--red)";
    if (opts.ok) line.style.color = "var(--green)";
    holder.appendChild(line);
    await sleep(opts.delay ?? 260);
  }

  function hideBoot() {
    const boot = qs("#boot");
    if (boot.classList.contains("is-hidden")) return;
    boot.classList.add("is-hidden");
    setTimeout(() => { boot.hidden = true; }, 500);
    qs("#app").hidden = false;
  }

  async function runBoot() {
    let skipped = false;
    qs("#boot").addEventListener("click", () => { skipped = true; hideBoot(); }, { once: true });

    const step = async (text, opts) => { if (!skipped) await bootLine(text, opts); };

    await step("ESTABLISHING UPLINK\u2026");
    const statusPromise = Api.status().catch(() => ({ online: false }));
    await step("LOCATING JARVIS BINARY\u2026", { delay: 320 });
    const status = await statusPromise;

    if (status.online) {
      await step(`LINKED \u2014 ${status.invocation}`, { ok: true, delay: 260 });
      await step("INDEXING COMMAND SET\u2026", { delay: 260 });
      await step("ALL SYSTEMS NOMINAL.", { ok: true, delay: 420 });
    } else {
      await step("BINARY NOT FOUND.", { warn: true, delay: 260 });
      await step("ENTERING DEGRADED MODE.", { warn: true, delay: 420 });
    }

    await initApp(status);
    hideBoot(); // no-op if the click handler above already hid it early
  }

  // ===========================================================================
  // Clock
  // ===========================================================================

  function tickClock() {
    const now = new Date();
    qs("#clock").textContent = now.toLocaleTimeString([], { hour12: false });
  }

  // ===========================================================================
  // Status pill
  // ===========================================================================

  function renderStatus(status) {
    const pill = qs("#status-pill");
    const text = qs("#status-text");
    const meta = qs("#status-meta");
    pill.classList.remove("is-online", "is-offline");
    if (status.online) {
      pill.classList.add("is-online");
      text.textContent = "ONLINE";
      meta.textContent = `${status.invocation} \u00b7 ${status.configPath}`;
      pill.title = "";
      pill.style.cursor = "default";
    } else {
      pill.classList.add("is-offline");
      text.textContent = "OFFLINE \u2014 click to retry";
      meta.textContent = "jarvis CLI not found on PATH";
      pill.title = "Retry locating the jarvis binary";
      pill.style.cursor = "pointer";
    }
  }

  qs("#status-pill").addEventListener("click", async () => {
    const pill = qs("#status-pill");
    if (!pill.classList.contains("is-offline")) return;
    qs("#status-text").textContent = "RETRYING\u2026";
    const status = await Api.reconnect().catch(() => ({ online: false }));
    renderStatus(status);
    if (status.online) await loadCommands();
  });

  // ===========================================================================
  // Capacity switch — see ai_client.PROMPT_MODE_DEFS on the CLI side (today:
  // 400% / 100% / 50%, i.e. full / compact / ultra). Fully generic: the
  // cycle order, labels, and tooltip summaries all come from /api/mode's
  // `options` array (itself ai_client.mode_options(), one source of truth),
  // never hardcoded here — adding a mode to PROMPT_MODE_DEFS is enough for
  // it to show up in this switch with zero front-end changes. Click always
  // steps to the next mode in `options` order and persists it via
  // /api/mode, same "thin client over the CLI" pattern as everything else
  // in this file.
  // ===========================================================================

  let modeOptions = [];   // [{mode,label,summary}, ...] from the server, in cycle order
  const FALLBACK_MODE = { mode: "compact", label: "Capacity", summary: "" };

  function optionFor(mode) {
    return modeOptions.find((o) => o.mode === mode) || null;
  }

  function renderMode(mode) {
    const btn = qs("#btn-mode-switch");
    const current = optionFor(mode) || optionFor(FALLBACK_MODE.mode) || FALLBACK_MODE;
    const idx = modeOptions.indexOf(current);
    const next = modeOptions.length ? modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length] : null;
    btn.dataset.mode = current.mode;
    btn.title = current.summary
      ? `${current.label} — ${current.summary}${next ? ` Click to switch to ${next.label}.` : ""}`
      : current.label;
    qs("#mode-switch-label").textContent = current.label;
  }

  async function loadMode() {
    try {
      const data = await Api.getMode();
      if (Array.isArray(data.options) && data.options.length) modeOptions = data.options;
      renderMode(data.mode);
    } catch {
      // Non-fatal — leave the button on its default label rather than
      // blocking the rest of the app over a cosmetic switch.
    }
  }

  qs("#btn-mode-switch").addEventListener("click", async () => {
    const btn = qs("#btn-mode-switch");
    if (!modeOptions.length) { await loadMode(); if (!modeOptions.length) return; }
    const current = btn.dataset.mode || FALLBACK_MODE.mode;
    const idx = modeOptions.findIndex((o) => o.mode === current);
    const next = modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length];
    btn.disabled = true;
    try {
      const data = await Api.setMode(next.mode);
      if (Array.isArray(data.options) && data.options.length) modeOptions = data.options;
      renderMode(data.mode);
    } catch (e) {
      toast(e.message || "Couldn't switch capacity mode.", "error");
    } finally {
      btn.disabled = false;
    }
  });

  // ===========================================================================
  // Command list
  // ===========================================================================

  function stepCount(spec) {
    return Array.isArray(spec.run) ? spec.run.length : 1;
  }
  function hasConditions(spec) {
    if (!Array.isArray(spec.run)) return false;
    return spec.run.some((s) => typeof s === "object" && (s.if != null || s.unless != null));
  }

  // Default flag values for a quick run/queue straight from the list card
  // (no var-form on screen yet) — only vars with a default get filled in.
  function defaultFlagsFor(spec) {
    const flags = {};
    for (const [vname, vspec] of Object.entries(spec.vars || {})) {
      if (vspec && typeof vspec === "object" && "default" in vspec) flags[vname] = vspec.default;
    }
    return flags;
  }
  function hasRequiredVars(spec) {
    return Object.values(spec.vars || {}).some((v) => !(v && typeof v === "object" && "default" in v));
  }

  const ICON_RUN = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M6 4l14 8-14 8V4z" fill="currentColor"/></svg>';
  const ICON_ADD = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5z" fill="currentColor"/></svg>';

  function quickRunCommand(name) {
    const spec = state.commands[name];
    if (!spec) return;
    if (hasRequiredVars(spec)) {
      selectCommand(name);
      toast(`"${name}" needs input \u2014 fill in the required fields, then Execute.`, "info");
      return;
    }
    runSegments([{ name, flags: defaultFlagsFor(spec) }]);
  }

  function quickQueueCommand(name) {
    const spec = state.commands[name];
    if (!spec) return;
    const flags = defaultFlagsFor(spec);
    const bits = Object.entries(flags).map(([k, v]) => `--${k} ${v}`).join(" ");
    state.sequence.push({ name, flags, label: bits ? `${name} ${bits}` : name, mode: "then" });
    renderSequenceBar();
    toast(`Added "${name}" to sequence.`, "info");
  }

  function commandMatchesSearch(name, spec, query) {
    if (!query) return true;
    const haystack = `${name} ${spec.description || ""}`.toLowerCase();
    return haystack.includes(query);
  }

  function renderCommandList() {
    const list = qs("#cmd-list");
    const allNames = Object.keys(state.commands);
    if (allNames.length === 0) {
      list.innerHTML = "";
      list.appendChild(el("div", { class: "empty-hint" }, "No commands yet. Build your first one."));
      return;
    }
    const query = state.cmdSearch.trim().toLowerCase();
    const names = allNames.filter((name) => commandMatchesSearch(name, state.commands[name], query));
    list.innerHTML = "";
    if (names.length === 0) {
      list.appendChild(el("div", { class: "empty-hint" }, `No commands match \u201c${state.cmdSearch.trim()}\u201d.`));
      return;
    }
    for (const name of names) {
      const spec = state.commands[name];
      const card = el("div", {
        class: "cmd-card" + (name === state.selected ? " is-active" : ""),
        onclick: () => selectCommand(name),
      }, [
        el("div", { class: "cmd-card__quick" }, [
          el("button", {
            type: "button",
            class: "cmd-card__quick-btn cmd-card__quick-btn--run",
            title: `Run "${name}"`,
            "aria-label": `Run ${name}`,
            html: ICON_RUN,
            onclick: (e) => { e.stopPropagation(); quickRunCommand(name); },
          }),
          el("button", {
            type: "button",
            class: "cmd-card__quick-btn cmd-card__quick-btn--add",
            title: `Add "${name}" to sequence`,
            "aria-label": `Add ${name} to sequence`,
            html: ICON_ADD,
            onclick: (e) => { e.stopPropagation(); quickQueueCommand(name); },
          }),
        ]),
        el("div", { class: "cmd-card__name" }, name),
        el("div", { class: "cmd-card__desc" }, spec.description || ""),
        el("div", { class: "cmd-card__meta" }, [
          el("span", { class: "badge" }, `${stepCount(spec)} step${stepCount(spec) === 1 ? "" : "s"}`),
          el("span", { class: "badge" }, `${Object.keys(spec.vars || {}).length} var${Object.keys(spec.vars || {}).length === 1 ? "" : "s"}`),
          hasConditions(spec) ? el("span", { class: "badge badge--cond" }, "conditional") : null,
        ]),
      ]);
      list.appendChild(card);
    }
  }

  async function loadCommands({ silent = false } = {}) {
    try {
      state.commands = await Api.listCommands();
    } catch (e) {
      state.commands = {};
      if (!silent) toast(e.message);
    }
    applyCommandsToUi();
  }

  function applyCommandsToUi() {
    renderCommandList();
    if (state.selected && !state.commands[state.selected]) {
      state.selected = null;
    }
    renderDetail();
    // If the Settings modal has commands.json open right now, refresh that
    // tab's in-memory copy so an external change (another tab, another
    // client, the CLI itself) doesn't get clobbered by a stale Save.
    // syncCommandsIntoSettingsTab is defined further down (Settings modal
    // section) — function declarations are hoisted, so this call is safe.
    syncCommandsIntoSettingsTab(state.commands);
  }

  function applyCommandsFromServer(commands) {
    if (!commands || typeof commands !== "object" || Array.isArray(commands)) return;
    state.commands = commands;
    applyCommandsToUi();
  }

  // ===========================================================================
  // Detail / run panel
  // ===========================================================================

  function selectCommand(name) {
    state.selected = name;
    renderCommandList();
    renderDetail();
  }

  function currentVarValues() {
    const values = {};
    qsa("#var-form [data-var]").forEach((input) => { values[input.dataset.var] = input.value; });
    return values;
  }

  function validateVarForm() {
    let ok = true;
    qsa("#var-form [data-var]").forEach((input) => {
      const required = input.dataset.required === "1";
      const empty = input.value.trim() === "";
      input.classList.toggle("is-invalid", required && empty);
      if (required && empty) ok = false;
    });
    return ok;
  }

  function renderDetail() {
    const empty = qs("#detail-empty");
    const content = qs("#detail-content");
    if (!state.selected || !state.commands[state.selected]) {
      empty.hidden = false;
      content.hidden = true;
      return;
    }
    empty.hidden = true;
    content.hidden = false;

    const name = state.selected;
    const spec = state.commands[name];
    qs("#detail-name").textContent = name;
    qs("#detail-desc").textContent = spec.description || "";

    const form = qs("#var-form");
    form.innerHTML = "";
    const varNames = Object.keys(spec.vars || {});
    if (varNames.length === 0) {
      form.appendChild(el("div", { class: "empty-hint" }, "This command takes no variables."));
    }
    for (const vname of varNames) {
      const vspec = spec.vars[vname] || {};
      const required = !("default" in vspec);
      const field = el("div", { class: "var-field" }, [
        el("label", { class: "var-field__label" }, [
          `--${vname}`,
          required ? el("span", { class: "var-field__req" }, "REQUIRED") : null,
        ]),
        el("input", {
          type: "text",
          "data-var": vname,
          "data-required": required ? "1" : "0",
          placeholder: required ? "(no default \u2014 required)" : String(vspec.default ?? ""),
          value: vspec.default != null ? String(vspec.default) : "",
        }),
        vspec.description ? el("div", { class: "var-field__hint" }, vspec.description) : null,
      ]);
      form.appendChild(field);
    }

    const stepsList = qs("#steps-preview");
    stepsList.innerHTML = "";
    const steps = Array.isArray(spec.run) ? spec.run : [spec.run];
    steps.forEach((step, i) => {
      const isObj = typeof step === "object" && step !== null;
      const runText = isObj ? step.run : step;
      const tags = [];
      if (isObj && step.if != null) tags.push(el("span", { class: "tag tag--if" }, `IF ${describeCondition(step.if)}`));
      if (isObj && step.unless != null) tags.push(el("span", { class: "tag tag--if" }, `UNLESS ${describeCondition(step.unless)}`));
      if (isObj && step.continueOnError) tags.push(el("span", { class: "tag tag--coe" }, "continue on error"));
      if (isObj && step.parallel && i > 0) tags.push(el("span", { class: "tag tag--parallel" }, "\u2225 parallel with previous"));
      if (isObj && step.showCommand === false) tags.push(el("span", { class: "tag tag--hidden" }, "command hidden"));

      stepsList.appendChild(el("li", { class: "step-preview" }, [
        el("div", { class: "step-preview__num" }, String(i + 1).padStart(2, "0")),
        el("div", { class: "step-preview__body" }, [
          isObj && step.name ? el("div", { class: "step-preview__name" }, step.name) : null,
          el("div", { class: "step-preview__run", html: highlightTokens(runText) }),
          tags.length ? el("div", { class: "step-preview__tags" }, tags) : null,
        ]),
      ]));
    });
  }

  qs("#btn-execute").addEventListener("click", () => {
    if (!state.selected) return;
    if (!validateVarForm()) { toast("Fill in all required variables first."); return; }
    runSegments([{ name: state.selected, flags: currentVarValues() }]);
  });

  // #var-form is a real <form> so a lone text field (very common — most
  // commands take one var) implicitly submits on Enter, which would
  // otherwise reload the page and wipe all state. Route that into EXECUTE.
  qs("#var-form").addEventListener("submit", (e) => {
    e.preventDefault();
    qs("#btn-execute").click();
  });

  qs("#btn-queue").addEventListener("click", () => {
    if (!state.selected) return;
    if (!validateVarForm()) { toast("Fill in all required variables first."); return; }
    const flags = currentVarValues();
    const bits = Object.entries(flags).map(([k, v]) => `--${k} ${v}`).join(" ");
    state.sequence.push({ name: state.selected, flags, label: bits ? `${state.selected} ${bits}` : state.selected, mode: "then" });
    renderSequenceBar();
    toast(`Added "${state.selected}" to sequence.`, "info");
  });

  qs("#btn-edit").addEventListener("click", () => {
    if (state.selected) openBuilder("edit", state.selected);
  });

  qs("#btn-delete").addEventListener("click", async () => {
    if (!state.selected) return;
    const name = state.selected;
    if (!confirm(`Delete "${name}" from commands.json? This can't be undone.`)) return;
    try {
      await Api.deleteCommand(name);
      state.selected = null;
      pruneSequence(name);
      await loadCommands();
      toast(`Deleted "${name}".`, "info");
    } catch (e) {
      toast(e.message);
    }
  });

  // ===========================================================================
  // Sequence bar
  // ===========================================================================

  function renderSequenceBar() {
    const bar = qs("#sequence-bar");
    const items = qs("#sequence-items");
    if (state.sequence.length === 0) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    items.innerHTML = "";
    state.sequence.forEach((item, i) => {
      // A connector between this item and the previous one \u2014 click to
      // flip it between "then" (wait for the previous item) and "and"
      // (run alongside it), same then/and relationship as the CLI's own
      // chain syntax and a step's own "parallel" toggle, one level up.
      if (i > 0) {
        const isParallel = item.mode === "and";
        items.appendChild(el("button", {
          type: "button",
          class: "seq-connector" + (isParallel ? " is-parallel" : ""),
          title: isParallel
            ? "Runs together with the previous item \u2014 click to run after it instead"
            : "Runs after the previous item finishes \u2014 click to run them together instead",
          onclick: () => { item.mode = isParallel ? "then" : "and"; renderSequenceBar(); },
        }, isParallel ? "\u2225" : "\u2192"));
      }
      items.appendChild(el("div", { class: "seq-chip" }, [
        el("span", { class: "seq-chip__idx" }, `${i + 1}`),
        el("span", {}, item.name),
        el("span", { class: "seq-chip__x", onclick: () => { state.sequence.splice(i, 1); renderSequenceBar(); } }, "\u00d7"),
      ]));
    });
  }

  function pruneSequence(name) {
    const before = state.sequence.length;
    state.sequence = state.sequence.filter((s) => s.name !== name);
    if (state.sequence.length !== before) renderSequenceBar();
  }

  function renameInSequence(oldName, newName) {
    let changed = false;
    for (const item of state.sequence) {
      if (item.name === oldName) {
        item.name = newName;
        changed = true;
      }
    }
    if (changed) renderSequenceBar();
  }

  qs("#btn-seq-clear").addEventListener("click", () => { state.sequence = []; renderSequenceBar(); });
  qs("#btn-seq-run").addEventListener("click", () => {
    if (state.sequence.length === 0) return;
    runSegments(state.sequence.map((s) => ({ name: s.name, flags: s.flags, mode: s.mode })));
    state.sequence = [];
    renderSequenceBar();
  });

  // ===========================================================================
  // Console + WebSocket execution
  // ===========================================================================

  function consoleAppend(text, cls) {
    const c = qs("#console");
    const idle = qs(".console__idle", c);
    if (idle) idle.remove();
    c.appendChild(el("div", { class: `console-line console-line--${cls}` }, text));
    c.scrollTop = c.scrollHeight;
  }

  qs("#btn-clear-console").addEventListener("click", () => {
    qs("#console").innerHTML = '<div class="console__idle">Awaiting instructions.</div>';
  });

  qs("#btn-abort").addEventListener("click", () => {
    wsSend({ type: "cancel" });
  });

  function setRunning(running) {
    state.running = running;
    qs("#btn-execute").disabled = running;
    qs("#btn-seq-run").disabled = running;
    qs("#btn-abort").hidden = !running;
    refreshAskBusyUI();
  }

  // The ask panel's own busy indicators (input, send/stop buttons) reflect
  // whether *the conversation currently on screen* is the one an ask is
  // running for — not just "is anything running at all" — so switching to
  // an idle conversation while a background ask keeps going elsewhere
  // doesn't leave a stray Stop button (or a disabled input) behind in a
  // conversation where, as far as its own view is concerned, nothing is
  // happening. Call this both when state.running changes (setRunning) and
  // whenever the visible conversation changes (selectConversation et al).
  function refreshAskBusyUI() {
    const busyHere = state.running && isViewingAskThread();
    qs("#ask-input").disabled = busyHere;
    qs("#btn-ask-send").disabled = busyHere;
    qs("#btn-ask-stop").hidden = !busyHere;
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    state.ws = ws;

    ws.addEventListener("open", () => { state.wsBackoff = 1000; });
    ws.addEventListener("close", () => {
      state.ws = null;
      if (state.running) consoleAppend("\u26a0 uplink to server lost mid-run", "exit-bad");
      setRunning(false);
      setTimeout(connectWs, state.wsBackoff);
      state.wsBackoff = Math.min(state.wsBackoff * 1.6, 10000);
    });
    ws.addEventListener("error", () => {});
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleWsMessage(msg);
    });
  }

  function wsSend(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
    } else {
      toast("Not connected to the server yet \u2014 try again in a moment.");
    }
  }

  function handleWsMessage(msg) {
    switch (msg.type) {
      case "commands":
        applyCommandsFromServer(msg.commands);
        break;
      case "start":
        setRunning(true);
        consoleAppend(msg.cmdline, "cmd");
        break;
      case "stdout":
        consoleAppend(msg.line, "out");
        break;
      case "stderr":
        consoleAppend(msg.line, "err");
        break;
      case "exit":
        setRunning(false);
        qs("#run-confirm-popup").hidden = true;
        if (msg.signal) {
          consoleAppend(`\u25a0 stopped (${msg.signal})`, "exit-bad");
          notifyTaskDone(`Stopped (${msg.signal}).`, true);
        } else if (msg.code === 0) {
          consoleAppend("\u25a0 done \u2014 exit code 0", "exit-ok");
          notifyTaskDone("Command finished.");
        } else {
          consoleAppend(`\u25a0 exit code ${msg.code}`, "exit-bad");
          notifyTaskDone(`Exit code ${msg.code}.`, true);
        }
        break;
      case "error":
        setRunning(false);
        qs("#run-confirm-popup").hidden = true;
        consoleAppend(`\u26a0 ${msg.message}`, "exit-bad");
        toast(msg.message);
        notifyTaskDone(msg.message, true);
        break;

      case "confirm-request":
        showRunConfirmPopup(msg.tool, msg.arguments, msg.risk_note);
        break;

      case "ask-start":
        setRunning(true);
        state.askReplyLines = [];
        state.askTraceBubble = null;
        state.askPendingBubble = addJarvisBubblePending();
        setAskStatus("thinking\u2026", "busy");
        askPromptBegin();
        break;
      case "ask-stdout":
        appendAskReplyLine(msg.line);
        break;
      case "ask-stderr":
        addAskPromptTrace(msg.line);
        break;
      case "ask-usage":
        // Phase 0 (new_plan.md) baseline: {input_tokens, output_tokens,
        // total_tokens, rounds: [{round, input_tokens, output_tokens, source}],
        // tool_calls: [{name, round, input_tokens, output_tokens, source}]}
        // from ai_providers.get_usage_summary(), one per successful ask.
        // Shown two places: a line in this turn's trace, and the debug
        // menu's "Last ask" token panel (renderDebugUsage below).
        state.debugLastUsage = msg.usage || null;
        if (msg.usage) {
          const u = msg.usage;
          const rounds = u.rounds || [];
          const tools = u.tool_calls || [];
          askPromptLine(
            `$ tokens  in=${u.input_tokens || 0} out=${u.output_tokens || 0} ` +
            `total=${u.total_tokens || 0}  rounds=${rounds.length} tools=${tools.length}`,
            "tool"
          );
        }
        renderDebugUsage();
        break;
      case "ask-confirm-request": {
        const convId = state.askConversationId;
        const extraItem = pushThreadExtra(convId, "confirm", {
          tool: msg.tool, arguments: msg.arguments, risk_note: msg.risk_note, resolved: null,
        });
        if (convId != null) {
          state.pendingConfirmByConv[convId] = { tool: msg.tool, arguments: msg.arguments, risk_note: msg.risk_note, extraItem };
        }
        if (isViewingAskThread()) {
          addAskConfirmBubble(msg.tool, msg.arguments, msg.risk_note, convId, extraItem);
          setAskStatus("waiting for your confirmation\u2026", "busy");
        } else {
          toast("Jarvis needs your OK on something in another chat.", "info");
        }
        break;
      }
      case "ask-exit": {
        setRunning(false);
        const raw = state.askReplyLines.length ? state.askReplyLines.join("\n") : "";
        finalizeAskBubble();
        askPromptEnd(msg.code, msg.signal);
        setAskStatus(msg.code === 0 ? "online" : "last attempt failed", msg.code === 0 ? "" : "error");
        notifyTaskDone(summarizeText(raw), msg.code !== 0);
        // The reply may have just (re)titled this conversation — refresh
        // the sidebar so its card picks up the new title/gist.
        if (msg.code === 0) refreshConvoList();
        // A successful turn is now a saved exchange server-side — bump the
        // bucket counter so the NEXT turn's extras (screenshots, console
        // dumps, etc.) get filed after it rather than merged into this one.
        if (msg.code === 0 && state.askConversationId != null) {
          const convId = state.askConversationId;
          state.exchangeCountByConv[convId] = (state.exchangeCountByConv[convId] || 0) + 1;
        }
        state.askConversationId = null;
        break;
      }
      case "ask-error":
        setRunning(false);
        if (state.askPendingBubble) {
          finalizeAskBubble(msg.message);
        } else {
          toast(msg.message);
        }
        askPromptEnd(1, null, msg.message);
        setAskStatus("error", "error");
        notifyTaskDone(msg.message, true);
        state.askConversationId = null;
        break;
    }
  }

  function runSegments(segments) {
    if (state.running) { toast("A command is already running."); return; }
    ensureNotifPermission();
    state.lastTaskLabel = (segments || []).map((s) => s.name).filter(Boolean).join(" then ") || "that command";
    wsSend({ type: "run", segments });
  }

  // ===========================================================================
  // Markdown renderer (marked.js — loaded via CDN before this script)
  // ===========================================================================

  function renderMarkdown(text) {
    if (typeof marked === "undefined") {
      const d = document.createElement("div");
      d.textContent = text;
      return d.innerHTML.replace(/\n/g, "<br>");
    }
    const html = marked.parse(text, {
      breaks: true,
      gfm: true,
    });
    if (typeof DOMPurify !== "undefined") {
      return DOMPurify.sanitize(html);
    }
    return html;
  }

  // ===========================================================================
  // Path linkification — turn file/folder paths mentioned in a rendered
  // reply into clickable links that open them, using the same
  // reveal_in_explorer / open_file_location / open_file tools present_file
  // (see jarvis-cli/jarvis/present_tools.py) uses under the hood. Web
  // console only, no CLI equivalent — there's no clickable surface in a
  // terminal.
  // ===========================================================================

  // Matches Windows paths (`C:\Users\...`, `\\server\share\...`) and
  // Unix-ish absolute paths (`/home/user/...`), each optionally followed by
  // a trailing file extension segment. Deliberately conservative: requires
  // at least one path separator after the root so we don't snag bare words
  // or drive letters mentioned in passing (e.g. "the C: drive").
  const PATH_RE = /(?:[a-zA-Z]:\\(?:[^\s\\/:*?"<>|]+\\)*[^\s\\/:*?"<>|]+|\\\\[^\s\\/:*?"<>|]+(?:\\[^\s\\/:*?"<>|]+)+|\/(?:[^\s/]+\/)*[^\s/]+)/g;

  // Trailing punctuation that's almost always sentence structure, not part
  // of the path itself (closing parens/brackets are kept if they're
  // balanced against an opener earlier in the match).
  function trimTrailingPunctuation(str) {
    let end = str.length;
    while (end > 0 && /[.,;:!?]/.test(str[end - 1])) end--;
    while (end > 0 && ")]}".includes(str[end - 1])) {
      const closer = str[end - 1];
      const opener = closer === ")" ? "(" : closer === "]" ? "[" : "{";
      const opens = str.slice(0, end - 1).split(opener).length - 1;
      const closes = str.slice(0, end - 1).split(closer).length - 1;
      if (opens > closes) break; // balanced against something earlier — keep it
      end--;
    }
    return str.slice(0, end);
  }

  // Cheap upfront guess so we pick the right tool on the first try in the
  // common case: a trailing separator is unambiguous, otherwise assume a
  // file if the last path segment has a dot-extension, folder otherwise.
  // Not load-bearing — see the retry in openPathLink() below, which
  // corrects a wrong guess using the tool's own error message rather than
  // trying to perfect this heuristic (we have no filesystem access here).
  function guessIsFolder(path) {
    if (/[\\/]$/.test(path)) return true;
    const lastSegment = path.split(/[\\/]/).pop() || "";
    return !/\.[^.]+$/.test(lastSegment);
  }

  async function openPathLink(rawPath, linkEl) {
    let isFolder = linkEl.dataset.isFolder === "1";
    linkEl.classList.add("is-busy");
    try {
      let res = await Api.runTool(isFolder ? "open_file_location" : "open_file", { path: rawPath });
      let errMsg = res.result && res.result.error;
      // Our folder/file guess was wrong — the tool just told us so
      // ("X is a folder, not a file"). Flip and retry once rather than
      // surfacing an error the user has no way to act on.
      if (!isFolder && errMsg && /is a folder, not a file/i.test(errMsg)) {
        isFolder = true;
        linkEl.dataset.isFolder = "1";
        res = await Api.runTool("open_file_location", { path: rawPath });
        errMsg = res.result && res.result.error;
      }
      const failed = res.ok === false || errMsg;
      if (failed) {
        toast(errMsg || res.error || "Couldn't open that.");
      }
    } catch (e) {
      toast(e.message || "Couldn't open that.");
    } finally {
      linkEl.classList.remove("is-busy");
    }
  }

  // A looser check used only for inline `code` spans: the model chose to
  // mark this text as a literal, so we trust it even if it contains spaces
  // (real Windows paths routinely do, e.g. "C:\Program Files\...") — we
  // just need it to *look* like a path at all, rather than picking out a
  // path-shaped substring from a run of prose.
  const LOOKS_LIKE_PATH_RE = /^(?:[a-zA-Z]:[\\/]|\\\\|\/)[^\n]*[^\s]$/;

  function makePathLink(rawPath) {
    const trimmed = trimTrailingPunctuation(rawPath);
    const isFolder = guessIsFolder(trimmed);
    const link = el("span", {
      class: "path-link",
      "data-is-folder": isFolder ? "1" : "0",
      title: `Open ${isFolder ? "folder" : "file"}: ${trimmed}`,
      onclick: (e) => { e.preventDefault(); openPathLink(trimmed, link); },
    }, trimmed);
    return link;
  }

  // Walks the rendered bubble's DOM, skipping real code blocks (<pre>,
  // i.e. fenced ```code```) and existing <a>/.path-link nodes so we don't
  // mangle code or double-link things, and wraps any path-looking text in
  // a clickable span. Call this right after setting a bubble's innerHTML
  // to renderMarkdown(...).
  //
  // Inline `code` spans get special handling: markdown renders a
  // single-backtick path like `C:\Program Files\Jarvis\log.txt` as
  // <code>...</code>, and models mention paths this way constantly. If the
  // whole span's content looks like a path (LOOKS_LIKE_PATH_RE), we treat
  // it as one regardless of internal spaces, since the backticks are the
  // model's own signal that it's a literal, not prose to search inside.
  // Plain (non-code) text still goes through PATH_RE, which is
  // space-free/conservative since it has to pick a path out of a sentence.
  function linkifyPaths(container) {
    if (!container) return;
    const codeSpans = Array.from(container.querySelectorAll("code")).filter((c) => !c.closest("pre"));
    for (const span of codeSpans) {
      const text = span.textContent;
      if (LOOKS_LIKE_PATH_RE.test(text.trim())) {
        span.replaceWith(makePathLink(text.trim()));
      }
    }

    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (parent.closest("code, pre, a, .path-link")) return NodeFilter.FILTER_REJECT;
        return PATH_RE.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const textNodes = [];
    let n;
    while ((n = walker.nextNode())) textNodes.push(n);

    for (const node of textNodes) {
      const text = node.nodeValue;
      PATH_RE.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let lastIndex = 0;
      let match;
      let any = false;
      while ((match = PATH_RE.exec(text))) {
        const raw = match[0];
        const trimmed = trimTrailingPunctuation(raw);
        if (trimmed.length < 3) continue; // too short to be a meaningful path (e.g. stray "/x")
        any = true;
        const start = match.index;
        frag.appendChild(document.createTextNode(text.slice(lastIndex, start)));
        frag.appendChild(makePathLink(trimmed));
        lastIndex = start + raw.length; // resume after the *untrimmed* match so leftover punctuation is preserved as text
        PATH_RE.lastIndex = lastIndex;
      }
      if (!any) continue;
      frag.appendChild(document.createTextNode(text.slice(lastIndex)));
      node.parentNode.replaceChild(frag, node);
    }
  }

  // ===========================================================================
  // Ask Jarvis
  // ===========================================================================

  const askOverlay = qs("#ask-overlay");
  const askThread = qs("#ask-thread");

  function setAskStatus(text, kind) {
    if (!isViewingAskThread()) return;
    const el = qs("#ask-status-line");
    el.textContent = text;
    el.className = "ask-panel__subtitle" + (kind ? ` is-${kind}` : "");
  }

  function askThreadScrollToEnd() {
    askThread.scrollTop = askThread.scrollHeight;
  }

  function clearAskEmptyHint() {
    const hint = qs(".ask-empty", askThread);
    if (hint) hint.remove();
  }

  function addAskMsgActions(msg) {
    if (qs(".ask-msg__actions", msg)) return;
    const actions = el("div", { class: "ask-msg__actions" }, [
      el("button", {
        type: "button",
        class: "ask-msg__act",
        title: "Copy as raw text",
        onclick: () => copyAskRaw(msg),
      }, "Copy"),
      el("button", {
        type: "button",
        class: "ask-msg__act",
        title: "Redo this prompt",
        onclick: () => redoAskMessage(msg),
      }, "Redo"),
    ]);
    msg.appendChild(actions);
  }

  async function copyAskRaw(msg) {
    const text = msg.dataset.raw || "";
    if (!text) {
      toast("Nothing to copy.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied raw text.", "info");
    } catch {
      toast("Couldn't copy to clipboard.");
    }
  }

  function previousUserMessage(fromMsg) {
    let node = fromMsg.previousElementSibling;
    while (node) {
      if (node.classList.contains("ask-msg--user")) return node;
      node = node.previousElementSibling;
    }
    return null;
  }

  function redoAskMessage(msg) {
    if (state.running) {
      toast("Wait for the current reply to finish.");
      return;
    }
    const userMsg = msg.classList.contains("ask-msg--user") ? msg : previousUserMessage(msg);
    const text = (userMsg && userMsg.dataset.raw || "").trim();
    if (!text) {
      toast("No prompt to redo.");
      return;
    }
    let node = userMsg.nextSibling;
    while (node) {
      const next = node.nextSibling;
      node.remove();
      node = next;
    }
    ensureNotifPermission();
    state.lastTaskLabel = text;
    state.askConversationId = state.activeConversationId;
    wsSend({ type: "ask", text, redo: true, conversationId: state.activeConversationId });
  }

  function addUserBubble(text, quotes) {
    clearAskEmptyHint();
    const kids = [el("div", { class: "ask-msg__role" }, "You")];
    if (quotes && quotes.length) {
      kids.push(el("div", { class: "ask-msg__quotes" }, quotes.map((q) =>
        el("blockquote", { class: "ask-msg__quote" }, q)
      )));
    }
    kids.push(el("div", { class: "ask-msg__bubble" }, text || "About the quoted part"));
    const msg = el("div", { class: "ask-msg ask-msg--user" }, kids);
    msg.dataset.raw = text || quotes.join("\n\n") || "";
    addAskMsgActions(msg);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // A pending-bubble reference can go stale (its DOM node destroyed) if the
  // thread was rebuilt from scratch by loadConversationIntoThread while we
  // were viewing a different conversation (see selectConversation, which
  // re-creates a fresh pending bubble on switching back to a still-running
  // conversation — but this guard is here in case anything else races it,
  // since insertBefore throws outright on a detached reference node).
  function insertIntoAskThread(msg) {
    if (state.askPendingBubble && askThread.contains(state.askPendingBubble)) {
      askThread.insertBefore(msg, state.askPendingBubble);
    } else {
      askThread.appendChild(msg);
    }
  }

  // True while the conversation thread currently on screen is the same one
  // an in-flight ask/redo actually belongs to. If the user has switched to
  // a different conversation while a reply is still streaming in, we must
  // not paint that reply into the (now unrelated) visible thread — the
  // exchange is still being saved server-side regardless, and will show up
  // correctly next time this conversation is opened.
  function isViewingAskThread() {
    return state.askConversationId == null || state.askConversationId === state.activeConversationId;
  }

  function addJarvisBubblePending() {
    if (!isViewingAskThread()) return null;
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble" }, [
        el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
      ]),
    ]);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // ---------------------------------------------------------------------
  // Thread "extras" — the non-text items (screenshots, downloads,
  // organize-json results, console dumps, resolved confirmations) that get
  // inserted straight into the ask thread as they happen. Unlike the plain
  // user/jarvis text exchanges, these never round-tripped through the
  // server, so loadConversationIntoThread() had nothing to replay them
  // from — they simply vanished the moment a conversation was rebuilt
  // (switching away and back, or a page refresh landing back on it).
  // We mirror them here, bucketed by how many exchanges had completed in
  // that conversation when they occurred, so they can be replayed in the
  // right place relative to the text exchanges.
  // ---------------------------------------------------------------------

  function extraBucketFor(convId) {
    return state.exchangeCountByConv[convId] || 0;
  }

  function pushThreadExtra(convId, type, data) {
    if (convId == null) return null;
    if (!state.threadExtrasByConv[convId]) state.threadExtrasByConv[convId] = [];
    const item = { bucket: extraBucketFor(convId), type, data };
    state.threadExtrasByConv[convId].push(item);
    return item;
  }

  // Console dumps stream in incrementally (more lines keep arriving for the
  // same turn) — find-or-create the one extra for this conversation+bucket
  // instead of pushing a new one on every update.
  function upsertConsoleExtra(convId, dumpLines) {
    if (convId == null) return;
    const bucket = extraBucketFor(convId);
    if (!state.threadExtrasByConv[convId]) state.threadExtrasByConv[convId] = [];
    const arr = state.threadExtrasByConv[convId];
    let item = arr.find((it) => it.type === "console" && it.bucket === bucket);
    if (!item) {
      item = { bucket, type: "console", data: { dumpLines: [] } };
      arr.push(item);
    }
    item.data.dumpLines = dumpLines.slice();
  }

  // Renders one recorded extra into the (currently on-screen) thread —
  // used both for a freshly-arrived event and for replaying history when a
  // conversation is (re)loaded.
  function renderThreadExtra(item) {
    switch (item.type) {
      case "screenshot":
        renderScreenshotBubble(item.data.filename);
        break;
      case "download":
        renderDownloadBubble(item.data.jobId, item.data.filename, item.data.title);
        break;
      case "organizeJson":
        if (item.data.payload) {
          renderOrganizeJsonExtra(item.data.targetPath, item.data.payload);
        } else if (item.data.targetPath) {
          // Server-persisted extras only ever carry the path (the full
          // parsed JSON isn't saved to disk) — insert the placeholder now,
          // in its correct spot in the replay order, then refetch exactly
          // like a live organize_json call does and fill it in once ready.
          // Caching the payload on the item means a second replay in this
          // tab won't refetch.
          clearAskEmptyHint();
          const msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
            el("div", { class: "ask-msg__role" }, "Jarvis"),
            el("div", { class: "ask-msg__bubble" }, [
              el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
            ]),
          ]);
          insertIntoAskThread(msg);
          (async () => {
            let payload;
            try {
              payload = await Api.organizeJson(item.data.targetPath);
            } catch (err) {
              payload = err.data || { ok: false, error: err.message };
            }
            item.data.payload = payload;
            if (askThread.contains(msg)) renderOrganizeJsonResult(msg, payload);
          })();
        }
        break;
      case "console":
        renderConsoleDumpBubble(item.data.dumpLines);
        break;
      case "confirm":
        if (item.data.resolved !== null) renderResolvedConfirmBubble(item.data);
        break;
    }
  }

  function addAskPromptTrace(raw) {
    const line = stripAnsi(raw).trim();
    if (!line) return;
    if (line.startsWith("JARVIS_MEDIA\t")) {
      const parts = line.split("\t");
      if (parts[1] === "screenshot" && parts[2]) {
        showAskScreenshot(parts[2].trim());
        askPromptLine(`$ screenshot  ${parts[2].trim()}`, "tool");
        return;
      }
      if (parts[1] === "download" && parts[2] && parts[3]) {
        const jobId = parts[2].trim();
        const filename = parts[3].trim();
        const title = (parts[4] || filename).trim();
        showAskDownload(jobId, filename, title);
        askPromptLine(`$ download  ${title}`, "tool");
        return;
      }
      if (parts[1] === "organize_json" && parts[2]) {
        showAskOrganizeJson(parts[2].trim());
        askPromptLine(`$ organize_json  ${parts[2].trim()}`, "tool");
        return;
      }
      if (parts[1] === "present_file" && parts[6] !== undefined) {
        const jobId = (parts[2] || "-").trim();
        const filename = (parts[3] || "-").trim();
        const name = (parts[4] || "").trim();
        const ftype = (parts[5] || "file").trim();
        const sizeBytes = parts[6] && parts[6] !== "-" ? Number(parts[6]) : null;
        const fullPath = (parts[7] || "").trim();
        showAskPresentFile({
          jobId: jobId === "-" ? null : jobId,
          filename: filename === "-" ? null : filename,
          name, type: ftype, sizeBytes, path: fullPath,
        });
        askPromptLine(`$ present  ${name || fullPath}`, "tool");
        return;
      }
    }
    let cls = "sys";
    if (line.includes("\u2717")) cls = "fail";
    else if (line.includes("$")) cls = "tool";
    askPromptLine(line, cls);
  }

  function formatFileSize(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "\u2014";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes / 1024;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
  }

  // Shared by present_file's chat card — runs reveal_in_explorer /
  // open_file_location / open_file straight through /api/tools/run, no
  // model involved, disabling the row's other buttons while it's in flight.
  function runFileAction(name, path, btn, siblings) {
    siblings.forEach((b) => { b.disabled = true; });
    const prevLabel = btn.textContent;
    btn.textContent = "\u2026";
    Api.runTool(name, { path })
      .then((res) => {
        toast(res.ok !== false && !(res.result && res.result.error) ? "Done." : ((res.result && res.result.error) || res.error || "Failed."));
      })
      .catch((e) => toast(e.message || "Failed."))
      .finally(() => {
        siblings.forEach((b) => { b.disabled = false; });
        btn.textContent = prevLabel;
      });
  }

  // Fired when the AI itself calls the organize_json tool (as opposed to
  // the person typing "organize-json <path>" directly into the Ask box —
  // see handleOrganizeJsonCommand). The tool already validated the file and
  // told the model only a tiny ok/type/count summary; this re-runs
  // /api/json/organize itself (still zero tokens — a plain local REST call)
  // purely to get the full parsed data for the Organized/Raw JSON sheet,
  // and renders it with the exact same renderOrganizeJsonResult() used by
  // the typed shortcut, so both paths look identical to the user.
  // Renders the Organized/Raw JSON sheet bubble; reused for a fresh result
  // and for replaying an already-fetched one from threadExtrasByConv.
  function renderOrganizeJsonExtra(targetPath, payload) {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble" }),
    ]);
    insertIntoAskThread(msg);
    renderOrganizeJsonResult(msg, payload);
    askThreadScrollToEnd();
    return msg;
  }

  function showAskOrganizeJson(targetPath) {
    const convId = state.askConversationId;
    const item = pushThreadExtra(convId, "organizeJson", { targetPath, payload: null });
    let msg = null;
    if (isViewingAskThread()) {
      clearAskEmptyHint();
      msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
        el("div", { class: "ask-msg__role" }, "Jarvis"),
        el("div", { class: "ask-msg__bubble" }, [
          el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
        ]),
      ]);
      insertIntoAskThread(msg);
      askThreadScrollToEnd();
    }
    (async () => {
      let payload;
      try {
        payload = await Api.organizeJson(targetPath);
      } catch (err) {
        payload = err.data || { ok: false, error: err.message };
      }
      // Keep the fetched result even if the conversation was switched away
      // from mid-fetch, so it's there next time this conversation is opened.
      if (item) item.data.payload = payload;
      // Thread may have been rebuilt (conversation switch) while we waited.
      if (!msg || !askThread.contains(msg)) return;
      renderOrganizeJsonResult(msg, payload);
    })();
  }

  function renderScreenshotBubble(filename) {
    clearAskEmptyHint();
    const url = `/api/screenshots/${encodeURIComponent(filename)}`;
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--media" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--media" }, [
        el("a", { href: url, target: "_blank", rel: "noopener", class: "ask-shot-link" }, [
          el("img", {
            class: "ask-shot",
            src: url,
            alt: "Desktop screenshot",
            loading: "lazy",
          }),
        ]),
        el("div", { class: "ask-shot-cap" }, "Screenshot"),
      ]),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function showAskScreenshot(filename) {
    if (!/^ss_[A-Za-z0-9_.-]+\.png$/.test(filename)) return;
    pushThreadExtra(state.askConversationId, "screenshot", { filename });
    if (!isViewingAskThread()) return;
    renderScreenshotBubble(filename);
  }

  // A finished ytdl_download (see jarvis-cli/jarvis/ytdl_tools.py) — offers
  // inline playback plus a real download link, keyed by the job's own
  // folder so filenames (drawn from the video's title) never collide.
  const DOWNLOAD_EXT_RE = /\.([A-Za-z0-9]+)$/;
  const AUDIO_EXTS = new Set(["mp3", "m4a", "opus", "wav", "flac", "ogg"]);

  function renderDownloadBubble(jobId, filename, title) {
    const url = `/api/downloads/${encodeURIComponent(jobId)}/${encodeURIComponent(filename)}`;
    const extMatch = DOWNLOAD_EXT_RE.exec(filename);
    const ext = extMatch ? extMatch[1].toLowerCase() : "";
    const isAudio = AUDIO_EXTS.has(ext);

    const player = isAudio
      ? el("audio", { class: "ask-dl-player", src: url, controls: "true", preload: "none" })
      : el("video", { class: "ask-dl-player", src: url, controls: "true", preload: "none" });

    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--media" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--media" }, [
        player,
        el("div", { class: "ask-dl-footer" }, [
          el("div", { class: "ask-shot-cap" }, title || filename),
          el("a", { href: url, download: filename, class: "ask-dl-link" }, "Download"),
        ]),
      ]),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function showAskDownload(jobId, filename, title) {
    if (!/^dl_[A-Za-z0-9_-]+$/.test(jobId) || !filename) return;
    pushThreadExtra(state.askConversationId, "download", { jobId, filename, title });
    if (!isViewingAskThread()) return;
    renderDownloadBubble(jobId, filename, title);
  }

  // A present_file call (see jarvis-cli/jarvis/present_tools.py) — a card
  // with the file/folder's name, type, size and path, plus Open/Reveal
  // buttons (straight REST calls via runFileAction, no model round-trip)
  // and, when the tool prepared one, a Download link identical in shape to
  // showAskDownload's (same job-folder/route, just a different tool made it).
  function showAskPresentFile(info) {
    if (!isViewingAskThread()) return;
    clearAskEmptyHint();
    const isFolder = info.type === "folder";
    const icon = isFolder ? "\ud83d\udcc1" : "\ud83d\udcc4";

    const revealBtn = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, "Reveal in Explorer");
    const openBtn = el("button", { class: "btn btn--ghost btn--sm", type: "button" }, isFolder ? "Open" : "Open file");
    const actionButtons = [revealBtn, openBtn];
    revealBtn.addEventListener("click", () => runFileAction("reveal_in_explorer", info.path, revealBtn, actionButtons));
    openBtn.addEventListener("click", () => runFileAction(isFolder ? "open_file_location" : "open_file", info.path, openBtn, actionButtons));

    const actions = [revealBtn, openBtn];
    if (info.jobId && info.filename) {
      const url = `/api/downloads/${encodeURIComponent(info.jobId)}/${encodeURIComponent(info.filename)}`;
      actions.push(el("a", { href: url, download: info.filename, class: "ask-dl-link" }, "Download"));
    }

    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--media" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--media" }, [
        el("div", { class: "ask-file-card" }, [
          el("div", { class: "ask-file-card__icon" }, icon),
          el("div", { class: "ask-file-card__body" }, [
            el("div", { class: "ask-file-card__name" }, info.name || info.path || "(unnamed)"),
            el("div", { class: "ask-file-card__meta" }, `${isFolder ? "Folder" : "File"} \u00b7 ${formatFileSize(info.sizeBytes)}`),
            el("div", { class: "ask-file-card__path" }, info.path || ""),
            el("div", { class: "ask-file-card__actions" }, actions),
          ]),
        ]),
      ]),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
  }

  function stripAnsi(s) {
    return String(s || "").replace(/\u001b\[[0-9;]*[A-Za-z]/g, "").replace(/\u001b\][^\u0007]*\u0007/g, "");
  }

  function askPromptTerm() {
    return qs("#ask-prompt-term");
  }

  function setAskPromptState(text, live) {
    if (!isViewingAskThread()) return;
    const node = qs("#ask-prompt-state");
    node.textContent = text;
    node.classList.toggle("is-live", !!live);
  }

  function askPromptClearIdle() {
    const idle = qs(".ask-prompt__idle", askPromptTerm());
    if (idle) idle.remove();
  }

  function askPromptCursor(show) {
    if (!isViewingAskThread()) return;
    const term = askPromptTerm();
    let cur = qs(".ask-prompt__cursor", term);
    if (!show) {
      if (cur) cur.remove();
      return;
    }
    if (!cur) cur = el("span", { class: "ask-prompt__cursor" });
    term.appendChild(cur);
  }

  // The single place a trace line gets added. Always records it against
  // whichever conversation the running ask belongs to (state.askConversationId)
  // so switching away and back doesn't lose it (see renderAskTraceForConv) —
  // and only touches the actual DOM if that conversation happens to be the
  // one on screen right now, so a background ask can never paint into (or
  // clobber) an unrelated, currently-visible conversation's panel.
  function askPromptLine(text, cls) {
    const convId = state.askConversationId;
    if (convId != null) {
      if (!state.askTraceByConv[convId]) state.askTraceByConv[convId] = [];
      state.askTraceByConv[convId].push({ text, cls });
    }
    if (!isViewingAskThread()) return;
    const term = askPromptTerm();
    askPromptClearIdle();
    const cur = qs(".ask-prompt__cursor", term);
    const line = el("div", { class: `ask-prompt-line ask-prompt-line--${cls}` }, text);
    if (cur) term.insertBefore(line, cur);
    else term.appendChild(line);
    term.scrollTop = term.scrollHeight;
  }

  function askPromptBegin() {
    if (state.askConversationId != null) state.askTraceByConv[state.askConversationId] = [];
    setAskPromptState("live", true);
    askPromptLine("$ jarvis", "cmd");
    askPromptCursor(true);
  }

  function askPromptEnd(code, signal, errorMessage) {
    if (errorMessage) askPromptLine(errorMessage, "fail");
    else if (signal) askPromptLine(`stopped (${signal})`, "fail");
    else if (code === 0) askPromptLine("done", "done");
    else askPromptLine(`exit ${code}`, "fail");
    askPromptCursor(false);
    setAskPromptState("idle", false);
  }

  function askPromptReset() {
    askPromptTerm().innerHTML = '<div class="ask-prompt__idle">Commands Jarvis runs will show up here live.</div>';
    setAskPromptState("idle", false);
  }

  // Called instead of a blind askPromptReset() whenever the visible
  // conversation changes: replays that conversation's own recorded trace
  // lines (if any) rather than always wiping the panel to idle, so
  // switching away and back no longer loses "the console output on the
  // right". If that conversation's ask is still actively running, restores
  // the live cursor/state too.
  function renderAskTraceForConv(convId) {
    const lines = state.askTraceByConv[convId];
    const term = askPromptTerm();
    if (!lines || !lines.length) {
      askPromptReset();
      return;
    }
    term.innerHTML = "";
    for (const { text, cls } of lines) {
      term.appendChild(el("div", { class: `ask-prompt-line ask-prompt-line--${cls}` }, text));
    }
    const stillRunning = state.running && state.askConversationId === convId;
    if (stillRunning) {
      term.appendChild(el("span", { class: "ask-prompt__cursor" }));
      qs("#ask-prompt-state").textContent = "live";
      qs("#ask-prompt-state").classList.add("is-live");
    } else {
      qs("#ask-prompt-state").textContent = "idle";
      qs("#ask-prompt-state").classList.remove("is-live");
    }
    term.scrollTop = term.scrollHeight;
  }

  // Jarvis's own CLI output is plain text like "J.A.R.V.I.S: <reply>" (see
  // cli.py: handle_ai_prompt, which does `print(f"{prefix}{result.text}")`).
  // Normally that "Name: " prefix is on line 1 and we just lift it off (and
  // adopt Name as the role label, so a renamed persona in ai_config.json is
  // reflected automatically) instead of showing it twice.
  //
  // But when the model calls a tool like run_command, it sometimes echoes
  // the tool's raw output *verbatim as the start of its own answer*, and
  // only *then* writes its actual signed reply \u2014 e.g.:
  //   Hello, World! Jarvis at your service.
  //   J.A.R.V.I.S: Executed, sir. The hello command completed successfully.
  // Here line 1 is straight from the command's stdout, not from Jarvis "the
  // persona" \u2014 the "Name: " prefix only shows up on line 2. Stripping only
  // ever looked at line 1, so that raw output used to get glued into the
  // same bubble as the real reply. Now we scan every line for the first one
  // that looks like "<Name>: <text>" and treat everything *before* it as a
  // console dump (its own bubble), keeping only that line onward (prefix
  // stripped) as Jarvis's actual reply.
  const NAME_PREFIX_LINE = /^([^\n:]{1,40}):\s(.*)$/;
  // Some providers (mainly weaker/local ones, or a mid-stream fallback) don't
  // always route tool calls through the real function-calling API and instead
  // have the model echo its own tool-call/tool-result scaffolding as plain
  // text \u2014 e.g. a line like "[called run_command with {...}]" or
  // "[tool result ...]" (see ai_client.py's _TOOL_TRACE_LINE). Treat those as
  // console dump too, wherever they show up in the reply.
  const INLINE_TOOL_TRACE_LINE = /^\[(called\s|tool result\b)/i;

  function splitConsoleDump(lines) {
    let splitAt = -1;
    let name = null;
    let firstReplyLine = null;
    for (let i = 0; i < lines.length; i++) {
      const m = NAME_PREFIX_LINE.exec(String(lines[i]));
      if (m) {
        splitAt = i;
        name = m[1];
        firstReplyLine = m[2];
        break;
      }
    }

    const dump = splitAt === -1 ? [] : lines.slice(0, splitAt);
    const candidateReply = splitAt === -1 ? lines.slice() : [firstReplyLine, ...lines.slice(splitAt + 1)];

    const reply = [];
    for (const line of candidateReply) {
      if (INLINE_TOOL_TRACE_LINE.test(String(line).trim())) dump.push(String(line).trim());
      else reply.push(line);
    }
    return { name, dump, reply };
  }

  function ensureAskTraceBubble() {
    if (state.askTraceBubble && askThread.contains(state.askTraceBubble)) return state.askTraceBubble;
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--console" }, [
      el("div", { class: "ask-msg__role" }, "Console"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--console" }),
    ]);
    insertIntoAskThread(msg);
    state.askTraceBubble = msg;
    return msg;
  }

  function renderAskTrace(dumpLines) {
    if (!dumpLines.length) return;
    upsertConsoleExtra(state.askConversationId, dumpLines);
    if (!isViewingAskThread()) return;
    renderConsoleDumpBubble(dumpLines);
  }

  // Renders (or updates) the "Console" bubble with the given dump lines —
  // used both for a live-streaming reply and for replaying a saved one.
  function renderConsoleDumpBubble(dumpLines) {
    const msg = ensureAskTraceBubble();
    qs(".ask-msg__bubble", msg).textContent = dumpLines.join("\n");
    msg.dataset.raw = dumpLines.join("\n");
    askThreadScrollToEnd();
  }

  // A tool flagged confirm_required (see jarvis-cli/jarvis/tool_safety.py)
  // pauses the running ask and asks the browser to decide. cli.py's
  // on_confirm_request is blocked on a synchronous stdin read right now —
  // server.js relays the answer straight to it, so until Yes/No is
  // clicked here the ask genuinely cannot proceed.
  function addAskConfirmBubble(tool, args, riskNote, convId, extraItem) {
    clearAskEmptyHint();
    const bubbleChildren = [
      el("div", { class: "ask-confirm__tool" }, tool || "(unknown tool)"),
      confirmValuePre(args || {}, "ask-confirm__args"),
    ];
    if (riskNote && riskNote.command_run !== undefined && riskNote.command_run !== null) {
      bubbleChildren.push(el("div", { class: "ask-confirm__risk-label" }, "Command:"));
      bubbleChildren.push(confirmValuePre(riskNote.command_run, "ask-confirm__args"));
    }
    if (riskNote && riskNote.note) {
      const label = riskNote.provider ? `AI review \u2014 ${riskNote.provider}` : "AI review";
      bubbleChildren.push(el("div", { class: "ask-confirm__risk" }, [
        el("div", { class: "ask-confirm__risk-label" }, label),
        el("div", { class: "ask-confirm__risk-note" }, riskNote.note),
      ]));
    }
    if (riskNote && riskNote.command_flags) {
      const cf = riskNote.command_flags;
      bubbleChildren.push(el("div", { class: "ask-confirm__flags" },
        `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
    }
    const yesBtn = el("button", { class: "btn btn--primary ask-confirm__btn", type: "button" }, "Yes, run it");
    const noBtn = el("button", { class: "btn btn--ghost ask-confirm__btn", type: "button" }, "No, cancel");
    const actions = el("div", { class: "ask-confirm__actions" }, [yesBtn, noBtn]);
    bubbleChildren.push(actions);

    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--confirm" }, [
      el("div", { class: "ask-msg__role" }, "Confirm"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--confirm" }, bubbleChildren),
    ]);

    function resolve(approved) {
      yesBtn.disabled = true;
      noBtn.disabled = true;
      actions.appendChild(el("div", { class: `ask-confirm__result ${approved ? "is-yes" : "is-no"}` },
        approved ? "\u2713 Approved \u2014 running\u2026" : "\u2717 Declined"));
      if (convId != null) delete state.pendingConfirmByConv[convId];
      if (extraItem) extraItem.data.resolved = approved;
      wsSend({ type: "ask-confirm-response", approved });
      setAskStatus("thinking\u2026", "busy");
    }
    yesBtn.addEventListener("click", () => resolve(true));
    noBtn.addEventListener("click", () => resolve(false));

    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // A static, already-resolved confirm bubble — used to replay history
  // (see renderThreadExtra) instead of the live interactive one above.
  function renderResolvedConfirmBubble(data) {
    clearAskEmptyHint();
    const bubbleChildren = [
      el("div", { class: "ask-confirm__tool" }, data.tool || "(unknown tool)"),
      confirmValuePre(data.arguments || {}, "ask-confirm__args"),
    ];
    if (data.risk_note && data.risk_note.command_run !== undefined && data.risk_note.command_run !== null) {
      bubbleChildren.push(el("div", { class: "ask-confirm__risk-label" }, "Command:"));
      bubbleChildren.push(confirmValuePre(data.risk_note.command_run, "ask-confirm__args"));
    }
    if (data.risk_note && data.risk_note.note) {
      const label = data.risk_note.provider ? `AI review \u2014 ${data.risk_note.provider}` : "AI review";
      bubbleChildren.push(el("div", { class: "ask-confirm__risk" }, [
        el("div", { class: "ask-confirm__risk-label" }, label),
        el("div", { class: "ask-confirm__risk-note" }, data.risk_note.note),
      ]));
    }
    if (data.risk_note && data.risk_note.command_flags) {
      const cf = data.risk_note.command_flags;
      bubbleChildren.push(el("div", { class: "ask-confirm__flags" },
        `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
    }
    bubbleChildren.push(el("div", { class: "ask-confirm__actions" }, [
      el("div", { class: `ask-confirm__result ${data.resolved ? "is-yes" : "is-no"}` },
        data.resolved ? "\u2713 Approved \u2014 running\u2026" : "\u2717 Declined"),
    ]));
    const msg = el("div", { class: "ask-msg ask-msg--jarvis ask-msg--confirm" }, [
      el("div", { class: "ask-msg__role" }, "Confirm"),
      el("div", { class: "ask-msg__bubble ask-msg__bubble--confirm" }, bubbleChildren),
    ]);
    insertIntoAskThread(msg);
    askThreadScrollToEnd();
    return msg;
  }

  // Shared between a normal incoming reply line and re-painting whatever's
  // accumulated so far into a freshly (re)created pending bubble — e.g.
  // after switching back into a still-running conversation, where the old
  // bubble's DOM node was destroyed by loadConversationIntoThread rebuilding
  // the thread from scratch (see selectConversation).
  function rerenderAskPendingBubble() {
    if (!state.askPendingBubble || !state.askReplyLines.length) return;
    const { name, dump, reply } = splitConsoleDump(state.askReplyLines);
    if (name) qs(".ask-msg__role", state.askPendingBubble).textContent = name;
    renderAskTrace(dump);
    const bubble = qs(".ask-msg__bubble", state.askPendingBubble);
    bubble.innerHTML = renderMarkdown(reply.join("\n"));
    linkifyPaths(bubble);
  }

  function appendAskReplyLine(line) {
    if (!state.askPendingBubble) return;
    state.askReplyLines.push(line);
    if (!isViewingAskThread()) return;
    rerenderAskPendingBubble();
    askThreadScrollToEnd();
  }

  function finalizeAskBubble(overrideMessage) {
    if (!isViewingAskThread()) {
      state.askPendingBubble = null;
      state.askReplyLines = [];
      state.askTraceBubble = null;
      return;
    }
    const bubble = state.askPendingBubble;
    if (bubble) {
      bubble.classList.remove("is-pending");
      if (state.askReplyLines.length === 0) {
        const raw = overrideMessage || "(no response)";
        bubble.dataset.raw = raw;
        bubble.classList.add("is-error");
        qs(".ask-msg__bubble", bubble).textContent = raw;
      } else {
        const { name, dump, reply } = splitConsoleDump(state.askReplyLines);
        if (name) qs(".ask-msg__role", bubble).textContent = name;
        renderAskTrace(dump);
        // If the model's entire "reply" somehow turned out to be dump lines,
        // fall back to showing everything rather than leaving the bubble blank.
        const replyLines = reply.length ? reply : state.askReplyLines;
        const raw = replyLines.join("\n");
        bubble.dataset.raw = raw;
        const bubbleEl = qs(".ask-msg__bubble", bubble);
        bubbleEl.innerHTML = renderMarkdown(raw);
        linkifyPaths(bubbleEl);
      }
      addAskMsgActions(bubble);
    }
    if (state.askTraceBubble) addAskMsgActions(state.askTraceBubble);
    state.askPendingBubble = null;
    state.askReplyLines = [];
    state.askTraceBubble = null;
    askThreadScrollToEnd();
  }

  function openAsk() {
    askOverlay.hidden = false;
    ensureNotifPermission();
    qs("#ask-input").focus();
  }
  function closeAsk() {
    askOverlay.hidden = true;
  }

  qs("#btn-ask-jarvis").addEventListener("click", openAsk);
  qs("#ask-close").addEventListener("click", closeAsk);
  askOverlay.addEventListener("click", (e) => { if (e.target === askOverlay) closeAsk(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !askOverlay.hidden) closeAsk();
  });

  qs("#btn-ask-stop").addEventListener("click", () => wsSend({ type: "cancel" }));

  const askQuoteBar = qs("#ask-quote-bar");
  const askSelPop = qs("#ask-sel-pop");
  const askPanel = qs(".ask-panel");
  let pendingSelection = "";

  function hideSelPop() {
    askSelPop.hidden = true;
    pendingSelection = "";
  }

  function renderQuoteBar() {
    askQuoteBar.innerHTML = "";
    if (!state.askQuotes.length) {
      askQuoteBar.hidden = true;
      return;
    }
    askQuoteBar.hidden = false;
    state.askQuotes.forEach((q, i) => {
      askQuoteBar.appendChild(el("div", { class: "ask-quote-chip" }, [
        el("span", { class: "ask-quote-chip__text", title: q }, q),
        el("button", {
          type: "button",
          class: "ask-quote-chip__x",
          title: "Remove quote",
          onclick: () => {
            state.askQuotes.splice(i, 1);
            renderQuoteBar();
          },
        }, "\u00d7"),
      ]));
    });
  }

  function addQuote(text) {
    const clipped = text.replace(/\s+/g, " ").trim();
    if (!clipped) return;
    if (state.askQuotes.includes(clipped)) return;
    if (state.askQuotes.length >= 5) {
      toast("You can quote up to 5 excerpts.");
      return;
    }
    state.askQuotes.push(clipped.slice(0, 1200));
    renderQuoteBar();
    qs("#ask-input").focus();
  }

  function selectionInAskThread() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return "";
    const range = sel.getRangeAt(0);
    if (!askThread.contains(range.commonAncestorContainer)) return "";
    const bubble = range.commonAncestorContainer.nodeType === 1
      ? range.commonAncestorContainer.closest(".ask-msg__bubble, .ask-msg__quote")
      : range.commonAncestorContainer.parentElement &&
        range.commonAncestorContainer.parentElement.closest(".ask-msg__bubble, .ask-msg__quote");
    if (!bubble) return "";
    return sel.toString().trim();
  }

  function placeSelPop() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return hideSelPop();
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const panelRect = askPanel.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - panelRect.left;
    let top = rect.top - panelRect.top - 36;
    askSelPop.hidden = false;
    const popW = askSelPop.offsetWidth || 120;
    left = Math.max(8, Math.min(left - popW / 2, panelRect.width - popW - 8));
    if (top < 8) top = rect.bottom - panelRect.top + 6;
    askSelPop.style.left = `${left}px`;
    askSelPop.style.top = `${top}px`;
  }

  function onAskSelection() {
    if (askOverlay.hidden) return hideSelPop();
    const text = selectionInAskThread();
    if (!text) return hideSelPop();
    pendingSelection = text;
    placeSelPop();
  }

  document.addEventListener("selectionchange", () => {
    if (askOverlay.hidden) return;
    // wait a tick so mouseup can finish
    requestAnimationFrame(onAskSelection);
  });
  askThread.addEventListener("scroll", hideSelPop, { passive: true });

  qs("#ask-sel-quote").addEventListener("mousedown", (e) => e.preventDefault());
  qs("#ask-sel-copy").addEventListener("mousedown", (e) => e.preventDefault());
  qs("#ask-sel-quote").addEventListener("click", () => {
    if (pendingSelection) addQuote(pendingSelection);
    window.getSelection()?.removeAllRanges();
    hideSelPop();
  });
  qs("#ask-sel-copy").addEventListener("click", async () => {
    if (!pendingSelection) return;
    try {
      await navigator.clipboard.writeText(pendingSelection);
      toast("Copied selection.", "info");
    } catch {
      toast("Couldn't copy.");
    }
    hideSelPop();
  });

  // "organize-json <path>" typed into the Ask box is a local shortcut, not
  // an AI prompt — it's intercepted here and routed straight to
  // /api/json/organize (see Api.organizeJson) so it never goes near
  // wsSend/ask, never costs a token, and isn't blocked by state.running
  // (that flag only guards the one-active-child-per-socket ask/run path;
  // organize-json is an independent one-shot REST call on the server).
  const ORGANIZE_JSON_RE = /^organize-json\s+(.+)$/i;

  qs("#ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = qs("#ask-input");
    const text = input.value.trim();
    const quotes = state.askQuotes.slice();
    if (!text && quotes.length === 0) return;

    const organizeMatch = quotes.length === 0 ? ORGANIZE_JSON_RE.exec(text) : null;
    if (organizeMatch) {
      if (!state.activeConversationId) await startNewConversation();
      input.value = "";
      askInputAutoGrow(input);
      hideSelPop();
      handleOrganizeJsonCommand(text, organizeMatch[1].trim());
      return;
    }

    // There is only one active child process per websocket connection
    // (server.js: ws.activeChild), so an ask really can't run concurrently
    // with another ask (or a plain run) even across conversations — that
    // part is by design, not a bug (see JARVIS_CONTEXT.md).
    //
    // The bug: when the in-flight ask belongs to a DIFFERENT conversation
    // than the one on screen, refreshAskBusyUI()'s isViewingAskThread()
    // check (correctly) leaves #ask-input/#btn-ask-send enabled here, since
    // it's only scoping the Stop-button/spinner UI to the conversation
    // that's actually busy. That left this handler's bare `if (state.running)
    // return;` as the only thing standing in the way — and it returned
    // silently, with no toast, no shake, nothing. From the user's
    // perspective, switching to an idle conversation while another one was
    // still replying made the whole app look permanently stuck: typing did
    // nothing and there was no indication why or when it would recover.
    // Surface it instead of eating the submit silently.
    if (state.running) {
      toast(
        isViewingAskThread()
          ? "Still replying \u2014 hang tight."
          : "Jarvis is still replying in another chat \u2014 wait for it to finish."
      );
      return;
    }
    if (!state.activeConversationId) await startNewConversation();
    // Captured once, right after the conversation is guaranteed to exist —
    // not re-read from state.activeConversationId further down. Between
    // this await and wsSend below, the user could switch to a different
    // conversation; reading state.activeConversationId twice could then
    // send this ask under one id but mark askConversationId with another,
    // misattributing every ask-stdout/ask-exit line that follows.
    const conversationId = state.activeConversationId;
    input.value = "";
    askInputAutoGrow(input);
    state.askQuotes = [];
    renderQuoteBar();
    hideSelPop();
    addUserBubble(text, quotes);
    ensureNotifPermission();
    state.lastTaskLabel = text || (quotes[0] || "that");
    state.askConversationId = conversationId;
    wsSend({
      type: "ask",
      text,
      quote: quotes.length ? quotes.join("\n---\n") : undefined,
      conversationId,
    });
  });

  // ---- organize-json chat-bubble result --------------------------------

  // Echoes the typed command as a user-style bubble but deliberately
  // WITHOUT the shared addAskMsgActions() Redo button — Redo resends
  // through the AI ask pipeline (wsSend/ask), which would defeat the
  // entire point of this shortcut (zero API tokens, however big the file).
  function addOrganizeJsonUserBubble(rawText) {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--user" }, [
      el("div", { class: "ask-msg__role" }, "You"),
      el("div", { class: "ask-msg__bubble" }, rawText),
    ]);
    msg.dataset.raw = rawText;
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function addOrganizeJsonPendingBubble() {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble" }, [
        el("span", { class: "ask-typing" }, [el("span", {}), el("span", {}), el("span", {})]),
      ]),
    ]);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function renderOrganizeJsonResult(msg, payload) {
    msg.classList.remove("is-pending");
    const bubble = qs(".ask-msg__bubble", msg);
    bubble.innerHTML = "";
    bubble.classList.add("ask-msg__bubble--jsontree");

    if (!payload || payload.ok === false) {
      msg.classList.add("is-error");
      bubble.appendChild(el("div", { class: "json-org-error" },
        (payload && payload.error) || "organize-json failed."));
      if (payload && payload.snippet) {
        bubble.appendChild(el("pre", { class: "json-org-snippet" }, payload.snippet));
      }
      msg.dataset.raw = (payload && payload.error) || "organize-json failed.";
      askThreadScrollToEnd();
      return;
    }

    const btnOrganized = el("button", { type: "button", class: "json-org-toggle-btn is-active" }, "Organized");
    const btnRaw = el("button", { type: "button", class: "json-org-toggle-btn" }, "Raw JSON");
    const viewWrap = el("div", { class: "json-org-view" });

    function showOrganized() {
      btnOrganized.classList.add("is-active");
      btnRaw.classList.remove("is-active");
      viewWrap.innerHTML = "";
      const treeMount = el("div", { class: "json-tree" });
      viewWrap.appendChild(treeMount);
      buildJsonTree(treeMount, payload.data, { editable: false });
    }
    function showRaw() {
      btnRaw.classList.add("is-active");
      btnOrganized.classList.remove("is-active");
      viewWrap.innerHTML = "";
      viewWrap.appendChild(el("pre", { class: "json-org-raw" }, JSON.stringify(payload.data, null, 2)));
    }
    btnOrganized.addEventListener("click", showOrganized);
    btnRaw.addEventListener("click", showRaw);

    bubble.appendChild(el("div", { class: "json-org-path" }, payload.path));
    bubble.appendChild(el("div", { class: "json-org-toggle" }, [btnOrganized, btnRaw]));
    bubble.appendChild(viewWrap);
    showOrganized();

    msg.dataset.raw = JSON.stringify(payload.data, null, 2);
    bubble.appendChild(el("div", { class: "ask-msg__actions ask-msg__actions--static" }, [
      el("button", {
        type: "button", class: "ask-msg__act", title: "Copy the underlying JSON",
        onclick: () => copyAskRaw(msg),
      }, "Copy JSON"),
    ]));
    askThreadScrollToEnd();
  }

  async function handleOrganizeJsonCommand(rawText, targetPath) {
    addOrganizeJsonUserBubble(rawText);
    const pending = addOrganizeJsonPendingBubble();
    if (!targetPath) {
      renderOrganizeJsonResult(pending, { ok: false, error: "usage: organize-json <path>" });
      return;
    }
    try {
      const payload = await Api.organizeJson(targetPath);
      renderOrganizeJsonResult(pending, payload);
    } catch (err) {
      renderOrganizeJsonResult(pending, err.data || { ok: false, error: err.message });
    }
  }

  // #ask-input is a <textarea> so a message can span multiple lines (the
  // CLI already handles embedded "\n" in a prompt string fine — this is
  // just about letting the web UI type one in). Enter sends, same as the
  // old single-line <input> used to; Shift+Enter inserts a real newline
  // instead, the same convention as Slack/Discord/etc.
  qs("#ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      qs("#ask-form").requestSubmit();
    }
  });

  // Grows the textarea to fit its content (up to the CSS max-height, after
  // which it scrolls) instead of staying a fixed single line.
  function askInputAutoGrow(input) {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }
  qs("#ask-input").addEventListener("input", (e) => askInputAutoGrow(e.target));

  qs("#btn-ask-clear").addEventListener("click", async () => {
    try {
      await Api.clearAiHistory(state.activeConversationId);
    } catch (e) {
      toast(e.message);
      return;
    }
    state.askQuotes = [];
    renderQuoteBar();
    hideSelPop();
    askThread.innerHTML = "";
    askThread.appendChild(el("div", { class: "ask-empty" }, "Ask about anything, or tell me what you need done, sir."));
    askPromptReset();
    refreshConvoList();
    toast("Conversation cleared.", "info");
  });

  // ===========================================================================
  // Debug dashboard \u2014 every tool Jarvis can call, read live via /api/tools
  // (never hardcoded), with a form to run any of them and see the response.
  // ===========================================================================

  const debugOverlay = qs("#debug-overlay");
  const debugStatusLine = qs("#debug-status-line");
  const debugDocs = qs("#debug-docs");
  const debugUsage = qs("#debug-usage"); // Phase 0 (new_plan.md): see renderDebugUsage below
  const debugArgs = qs("#debug-args");
  const debugResponse = qs("#debug-response");
  const debugToolList = qs("#debug-tool-list");
  const debugToolCount = qs("#debug-tool-count");
  const btnDebugRun = qs("#btn-debug-run");

  // ---- Debug's own capacity switch ---------------------------------------
  // Looks and cycles just like the global #btn-mode-switch (same
  // modeOptions/optionFor/FALLBACK_MODE from the section above), but it
  // only ever writes to state.debugMode \u2014 it never calls Api.setMode, so
  // clicking it can't change what mode Jarvis is actually running in.
  // Nothing currently reads state.debugMode back into a tool call (tool-run
  // bypasses the model entirely), so today this is purely a local display
  // toggle, kept separate in case a debug-scoped mode override is wired up
  // to something later.
  const btnDebugModeSwitch = qs("#btn-debug-mode-switch");
  const debugModeSwitchLabel = qs("#debug-mode-switch-label");

  function renderDebugMode() {
    const current = optionFor(state.debugMode) || optionFor(FALLBACK_MODE.mode) || FALLBACK_MODE;
    const idx = modeOptions.indexOf(current);
    const next = modeOptions.length ? modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length] : null;
    btnDebugModeSwitch.dataset.mode = current.mode;
    const base = "Local override for this debug panel only \u2014 does not touch Jarvis's real capacity mode.";
    btnDebugModeSwitch.title = current.summary
      ? `${base} ${current.label} \u2014 ${current.summary}${next ? ` Click to switch to ${next.label}.` : ""}`
      : base;
    debugModeSwitchLabel.textContent = current.label;
  }

  btnDebugModeSwitch.addEventListener("click", async () => {
    if (!modeOptions.length) { await loadMode(); if (!modeOptions.length) return; }
    const current = state.debugMode || FALLBACK_MODE.mode;
    const idx = modeOptions.findIndex((o) => o.mode === current);
    const next = modeOptions[(Math.max(idx, 0) + 1) % modeOptions.length];
    state.debugMode = next.mode; // local only \u2014 never posted to /api/mode
    renderDebugMode();
  });

  function debugFindTool(name) {
    return state.debugTools.find((t) => t.name === name) || null;
  }

  function debugFilteredTools() {
    const q = state.debugSearch.trim().toLowerCase();
    if (!q) return state.debugTools;
    return state.debugTools.filter((t) =>
      t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q)
    );
  }

  // Human-readable type label for a JSON-schema property, e.g. "string",
  // "array<string>", "enum".
  function debugParamTypeLabel(prop) {
    if (!prop || typeof prop !== "object") return "any";
    if (Array.isArray(prop.enum)) return "enum";
    if (prop.type === "array") {
      const items = prop.items && prop.items.type ? prop.items.type : "any";
      return `array<${items}>`;
    }
    return prop.type || "any";
  }

  function debugParamEntries(schema) {
    const props = (schema && schema.properties) || {};
    const required = new Set((schema && schema.required) || []);
    return Object.entries(props).map(([name, prop]) => ({
      name,
      prop: prop || {},
      required: required.has(name),
    }));
  }

  // ---- LEFT pane: description + parameter docs -----------------------------

  // A single "Toggle warning:"/"Toggle AI review:" switch under a tool's
  // description. Flips jarvis-cli/jarvis/tool_safety.json's per-tool flags
  // immediately (no separate save step) via /api/tools/safety, and mutates
  // the in-memory tool object in place so the confirm flow below reads the
  // change right away without a full tool-list reload.
  function debugToggleRow(tool, key, label) {
    const input = el("input", { type: "checkbox", class: "safety-toggle__input" });
    input.checked = !!tool[key];
    const row = el("label", { class: "safety-toggle" }, [
      input,
      el("span", { class: "safety-toggle__track" }),
      el("span", { class: "safety-toggle__label" }, label),
    ]);
    input.addEventListener("change", async () => {
      const next = input.checked;
      input.disabled = true;
      try {
        const updated = await Api.setToolSafety(tool.name, key, next);
        tool[key] = !!(updated && key in updated ? updated[key] : next);
        input.checked = tool[key];
      } catch (e) {
        input.checked = !next; // revert on failure
        toast(`Couldn't update ${label.toLowerCase()} for ${tool.name}: ${e.message}`);
      } finally {
        input.disabled = false;
      }
    });
    return row;
  }

  // Phase 0 (new_plan.md) baseline: render the most recent ask's token/tool/
  // round breakdown into the debug panel. Called on "ask-usage" (handleWsMessage)
  // and once on debug-overlay open so a prior turn's numbers are still visible.
  function renderDebugUsage() {
    if (!debugUsage) return;
    debugUsage.innerHTML = "";
    const u = state.debugLastUsage;
    if (!u) {
      debugUsage.appendChild(el("div", { class: "debug-empty" }, "No ask measured yet this session."));
      return;
    }
    const rounds = u.rounds || [];
    const toolCalls = u.tool_calls || [];
    debugUsage.appendChild(el("div", { class: "debug-docs__desc" },
      `input=${u.input_tokens || 0}  output=${u.output_tokens || 0}  ` +
      `total=${u.total_tokens || 0}  rounds=${rounds.length}  tool calls=${toolCalls.length}`
    ));
    if (rounds.length) {
      debugUsage.appendChild(el("div", { class: "debug-docs__section-title" }, "Per round (reported)"));
      for (const r of rounds) {
        debugUsage.appendChild(el("div", { class: "debug-empty" },
          `round ${r.round}: in=${r.input_tokens || 0} out=${r.output_tokens || 0}`
        ));
      }
    }
    if (toolCalls.length) {
      debugUsage.appendChild(el("div", { class: "debug-docs__section-title" }, "Per tool call (estimated)"));
      for (const t of toolCalls) {
        debugUsage.appendChild(el("div", { class: "debug-empty" },
          `${t.name} (round ${t.round}): in\u2248${t.input_tokens || 0} out\u2248${t.output_tokens || 0}`
        ));
      }
    }
  }

  function renderDebugDocs(tool) {
    debugDocs.innerHTML = "";
    if (!tool) {
      debugDocs.appendChild(el("div", { class: "debug-empty" }, "Select a tool from the list on the right."));
      return;
    }
    debugDocs.appendChild(el("div", { class: "debug-docs__name" }, tool.name));
    debugDocs.appendChild(el("div", { class: "debug-docs__desc" },
      tool.description || "No description provided by this tool."));

    debugDocs.appendChild(el("div", { class: "debug-docs__safety" }, [
      debugToggleRow(tool, "confirm_required", "Toggle warning:"),
      debugToggleRow(tool, "ai_review", "Toggle AI review:"),
    ]));

    const entries = debugParamEntries(tool.parameters);
    if (!entries.length) {
      debugDocs.appendChild(el("div", { class: "debug-docs__section-title" }, "Parameters"));
      debugDocs.appendChild(el("div", { class: "debug-empty" }, "This tool takes no arguments."));
      return;
    }
    debugDocs.appendChild(el("div", { class: "debug-docs__section-title" }, "Parameters"));
    for (const { name, prop, required } of entries) {
      const head = el("div", { class: "debug-param-doc__name" }, [
        name,
        el("span", { class: "debug-param-doc__type" }, debugParamTypeLabel(prop)),
        required ? el("span", { class: "debug-param-doc__req" }, "required") : null,
      ]);
      const doc = el("div", { class: "debug-param-doc" }, [head]);
      if (prop.description) {
        doc.appendChild(el("div", { class: "debug-param-doc__desc" }, prop.description));
      }
      if (Array.isArray(prop.enum)) {
        doc.appendChild(el("div", { class: "debug-param-doc__enum" }, `one of: ${prop.enum.join(", ")}`));
      }
      debugDocs.appendChild(doc);
    }
  }

  // ---- MIDDLE pane: argument form -------------------------------------------

  // Builds one labeled input appropriate to the property's JSON-schema type.
  // Returns the input/textarea/select element so the run handler can read it.
  function debugBuildArgInput(name, prop, required) {
    const type = prop && prop.type;

    if (Array.isArray(prop && prop.enum)) {
      const select = el("select", { "data-arg": name });
      if (!required) select.appendChild(el("option", { value: "" }, "(omit)"));
      for (const v of prop.enum) select.appendChild(el("option", { value: v }, String(v)));
      if (prop.default !== undefined) select.value = String(prop.default);
      return select;
    }
    if (type === "boolean") {
      const wrap = el("label", { class: "check" });
      const input = el("input", { type: "checkbox", "data-arg": name, "data-argtype": "boolean" });
      if (prop.default === true) input.checked = true;
      wrap.appendChild(input);
      wrap.appendChild(document.createTextNode(" true"));
      wrap.__debugInput = input; // caller reads .__debugInput when the field itself is a <label>
      return wrap;
    }
    if (type === "number" || type === "integer") {
      const input = el("input", {
        type: "number", "data-arg": name, "data-argtype": type,
        placeholder: prop.default !== undefined ? String(prop.default) : "",
      });
      if (prop.minimum !== undefined) input.min = prop.minimum;
      if (prop.maximum !== undefined) input.max = prop.maximum;
      return input;
    }
    if (type === "array" || type === "object") {
      const input = el("textarea", {
        "data-arg": name, "data-argtype": type,
        placeholder: type === "array" ? "[\"item1\", \"item2\"]" : "{\"key\": \"value\"}",
      });
      return input;
    }
    // string, or unknown \u2014 default to a plain text input
    const input = el("input", {
      type: "text", "data-arg": name, "data-argtype": "string",
      placeholder: prop && prop.default !== undefined ? String(prop.default) : "",
    });
    return input;
  }

  function renderDebugArgs(tool) {
    debugArgs.innerHTML = "";
    if (!tool) {
      debugArgs.appendChild(el("div", { class: "debug-empty" }, "Nothing selected yet."));
      btnDebugRun.disabled = true;
      return;
    }
    btnDebugRun.disabled = false;
    const entries = debugParamEntries(tool.parameters);
    if (!entries.length) {
      debugArgs.appendChild(el("div", { class: "debug-empty" }, "This tool takes no arguments \u2014 just hit Run."));
      return;
    }
    for (const { name, prop, required } of entries) {
      const label = el("div", { class: "debug-arg-field__label" }, [
        name,
        required ? el("span", { class: "debug-arg-field__req" }, "required") : null,
      ]);
      const input = debugBuildArgInput(name, prop, required);
      const field = el("div", { class: "debug-arg-field" }, [label, input]);
      debugArgs.appendChild(field);
    }
  }

  // Reads every [data-arg] control under #debug-args back into a plain
  // object, coercing each value to the type the schema said it should be.
  // Throws with a friendly message if a field can't be coerced.
  function debugCollectArgs() {
    const out = {};
    for (const node of qsa("[data-arg]", debugArgs)) {
      const name = node.getAttribute("data-arg");
      const argtype = node.getAttribute("data-argtype");
      if (node.tagName === "SELECT") {
        if (node.value !== "") out[name] = node.value;
        continue;
      }
      if (argtype === "boolean") {
        out[name] = node.checked;
        continue;
      }
      const raw = node.value;
      if (raw === "" || raw == null) continue; // omit empty optional/required-but-blank fields
      if (argtype === "number" || argtype === "integer") {
        const n = Number(raw);
        if (Number.isNaN(n)) throw new Error(`"${name}" must be a number.`);
        out[name] = argtype === "integer" ? Math.trunc(n) : n;
        continue;
      }
      if (argtype === "array" || argtype === "object") {
        try {
          out[name] = JSON.parse(raw);
        } catch (e) {
          throw new Error(`"${name}" must be valid JSON (${e.message}).`);
        }
        continue;
      }
      out[name] = raw;
    }
    return out;
  }

  // ---- BOTTOM: response viewer (organized / raw JSON) -----------------------

  // Renders any JSON value as a plain indented key: value tree \u2014 the
  // "organized" view, easier to scan than a raw dump for typical tool
  // results (flat-ish objects, small arrays).
  function debugRenderOrganized(value, depth = 0) {
    if (value === null || value === undefined) {
      return el("span", { class: "debug-kv__val" }, String(value));
    }
    if (Array.isArray(value)) {
      if (!value.length) return el("span", { class: "debug-kv__val" }, "[]");
      const wrap = el("div", { class: "debug-kv" });
      value.forEach((item, i) => {
        const row = el("div", { class: "debug-kv__row" }, [
          el("span", { class: "debug-kv__key" }, `[${i}]`),
        ]);
        if (item !== null && typeof item === "object") {
          row.appendChild(debugRenderOrganized(item, depth + 1));
        } else {
          row.appendChild(el("span", { class: "debug-kv__val" }, String(item)));
        }
        wrap.appendChild(row);
      });
      return wrap;
    }
    if (typeof value === "object") {
      const keys = Object.keys(value);
      if (!keys.length) return el("span", { class: "debug-kv__val" }, "{}");
      const wrap = el("div", { class: "debug-kv" });
      for (const k of keys) {
        const v = value[k];
        const row = el("div", { class: "debug-kv__row" }, [el("span", { class: "debug-kv__key" }, `${k}:`)]);
        if (v !== null && typeof v === "object") {
          row.appendChild(debugRenderOrganized(v, depth + 1));
        } else {
          row.appendChild(el("span", { class: "debug-kv__val" }, String(v)));
        }
        wrap.appendChild(row);
      }
      return wrap;
    }
    return el("span", { class: "debug-kv__val" }, String(value));
  }

  function renderDebugResponse() {
    debugResponse.innerHTML = "";
    debugResponse.classList.remove("is-error");
    if (state.debugPendingConfirm) {
      debugResponse.appendChild(debugRenderConfirmPending(state.debugPendingConfirm));
      return;
    }
    const last = state.debugLastResult;
    if (!last) {
      debugResponse.appendChild(el("div", { class: "debug-empty" }, "Response will appear here after you run a tool."));
      return;
    }
    if (last.error) {
      debugResponse.classList.add("is-error");
      debugResponse.appendChild(el("pre", {}, last.error));
      return;
    }
    const payload = last.result;
    if (state.debugResponseMode === "raw") {
      debugResponse.appendChild(el("pre", {}, JSON.stringify(payload, null, 2)));
    } else {
      debugResponse.appendChild(debugRenderOrganized(payload));
    }
  }

  // Shown in the response pane, in place of a result, while a
  // confirm_required tool's run is waiting on Yes/No — mirrors the same
  // "raw JSON has the prompt + buttons" shape as the chat confirm bubble
  // (see addAskConfirmBubble), just scoped to a manual debug-dashboard run.
  function debugRenderConfirmPending(pending) {
    const wrap = el("div", { class: "debug-confirm" });
    if (state.debugResponseMode === "raw") {
      wrap.appendChild(el("pre", {}, JSON.stringify({
        tool: pending.name, arguments: pending.arguments, risk_note: pending.risk_note,
      }, null, 2)));
    } else {
      wrap.appendChild(el("div", { class: "debug-confirm__tool" }, pending.name));
      wrap.appendChild(confirmValuePre(pending.arguments || {}, "debug-confirm__args"));
      if (pending.risk_note && pending.risk_note.command_run !== undefined && pending.risk_note.command_run !== null) {
        wrap.appendChild(el("div", { class: "debug-confirm__risk-label" }, "Command:"));
        wrap.appendChild(confirmValuePre(pending.risk_note.command_run, "debug-confirm__args"));
      }
      if (pending.risk_note && pending.risk_note.note) {
        const label = pending.risk_note.provider ? `AI review \u2014 ${pending.risk_note.provider}` : "AI review";
        wrap.appendChild(el("div", { class: "debug-confirm__risk" }, [
          el("div", { class: "debug-confirm__risk-label" }, label),
          el("div", { class: "debug-confirm__risk-note" }, pending.risk_note.note),
        ]));
      }
      if (pending.risk_note && pending.risk_note.command_flags) {
        const cf = pending.risk_note.command_flags;
        wrap.appendChild(el("div", { class: "debug-confirm__flags" },
          `Flags: confirm_required=${!!cf.confirm_required}, ai_review=${!!cf.ai_review}`));
      }
    }
    const yesBtn = el("button", { class: "btn btn--primary", type: "button" }, "Y \u2014 run it");
    const noBtn = el("button", { class: "btn btn--ghost", type: "button" }, "N \u2014 cancel");
    wrap.appendChild(el("div", { class: "debug-confirm__actions" }, [yesBtn, noBtn]));

    yesBtn.addEventListener("click", async () => {
      yesBtn.disabled = true;
      noBtn.disabled = true;
      debugStatusLine.textContent = `running ${pending.name}\u2026`;
      debugStatusLine.classList.add("is-busy");
      debugStatusLine.classList.remove("is-error", "is-ok");
      try {
        await debugRunNow(pending.name, pending.arguments);
      } catch (e) {
        state.debugLastResult = { error: e.message };
        debugStatusLine.textContent = "request failed";
        debugStatusLine.classList.add("is-error");
      } finally {
        state.debugPendingConfirm = null;
        debugStatusLine.classList.remove("is-busy");
        renderDebugResponse();
      }
    });
    noBtn.addEventListener("click", () => {
      state.debugPendingConfirm = null;
      state.debugLastResult = { result: { ok: false, cancelled: true, message: `You declined to run '${pending.name}'.` } };
      debugStatusLine.textContent = "cancelled";
      debugStatusLine.classList.remove("is-error", "is-ok", "is-busy");
      renderDebugResponse();
    });
    return wrap;
  }

  qs("#debug-response-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest(".debug-toggle-btn");
    if (!btn) return;
    state.debugResponseMode = btn.dataset.mode;
    qsa(".debug-toggle-btn", debugOverlay).forEach((b) => b.classList.toggle("is-active", b === btn));
    renderDebugResponse();
  });

  // ---- RIGHT pane: tool list -------------------------------------------------

  function renderDebugToolList() {
    const tools = debugFilteredTools();
    debugToolCount.textContent = `${state.debugTools.length}`;
    debugToolList.innerHTML = "";
    if (!tools.length) {
      debugToolList.appendChild(el("div", { class: "debug-empty" },
        state.debugTools.length ? "No tools match your search." : "No tools reported by jarvis."));
      return;
    }
    for (const tool of tools) {
      const card = el("div", {
        class: "debug-tool-card" + (tool.name === state.debugSelected ? " is-active" : ""),
        onclick: () => debugSelectTool(tool.name),
      }, [
        el("div", { class: "debug-tool-card__name" }, tool.name),
        tool.description ? el("div", { class: "debug-tool-card__desc" }, tool.description) : null,
      ]);
      debugToolList.appendChild(card);
    }
  }

  function debugSelectTool(name) {
    state.debugSelected = name;
    state.debugLastResult = null;
    state.debugPendingConfirm = null;
    const tool = debugFindTool(name);
    renderDebugDocs(tool);
    renderDebugArgs(tool);
    renderDebugResponse();
    renderDebugToolList();
  }

  qs("#debug-search").addEventListener("input", (e) => {
    state.debugSearch = e.target.value;
    renderDebugToolList();
  });

  // ---- Run ------------------------------------------------------------------

  // Actually calls /api/tools/run and stashes the result — split out so
  // both the direct (non-sensitive) path below and the confirm dialog's
  // Yes button (debugRenderConfirmPending) can share it.
  async function debugRunNow(name, args) {
    const res = await Api.runTool(name, args, state.debugMode);
    state.debugLastResult = res.ok !== false ? { result: res.result } : { error: res.error || "Tool run failed." };
    debugStatusLine.textContent = res.ok !== false ? "done" : "tool run failed";
    debugStatusLine.classList.toggle("is-error", res.ok === false);
    debugStatusLine.classList.toggle("is-ok", res.ok !== false);
  }

  btnDebugRun.addEventListener("click", async () => {
    if (!state.debugSelected) return;
    let args;
    try {
      args = debugCollectArgs();
    } catch (e) {
      state.debugLastResult = { error: e.message };
      state.debugPendingConfirm = null;
      renderDebugResponse();
      return;
    }
    const tool = debugFindTool(state.debugSelected);
    btnDebugRun.disabled = true;
    debugStatusLine.classList.remove("is-error", "is-ok");
    try {
      if (tool && tool.confirm_required) {
        // Don't run anything yet — ask jarvis what this call would do (and,
        // if ai_review is on for this tool, a second provider's risk note),
        // then wait for Yes/No before ever calling /api/tools/run for real.
        debugStatusLine.textContent = `checking ${state.debugSelected}\u2026`;
        debugStatusLine.classList.add("is-busy");
        state.debugLastResult = null;
        const preview = await Api.previewTool(state.debugSelected, args, state.debugMode);
        state.debugPendingConfirm = {
          name: state.debugSelected,
          arguments: args,
          risk_note: (preview && preview.risk_note) || null,
        };
        debugStatusLine.textContent = "awaiting confirmation";
      } else {
        debugStatusLine.textContent = `running ${state.debugSelected}\u2026`;
        debugStatusLine.classList.add("is-busy");
        await debugRunNow(state.debugSelected, args);
      }
    } catch (e) {
      state.debugLastResult = { error: e.message };
      state.debugPendingConfirm = null;
      debugStatusLine.textContent = "request failed";
      debugStatusLine.classList.add("is-error");
    } finally {
      debugStatusLine.classList.remove("is-busy");
      btnDebugRun.disabled = false;
      renderDebugResponse();
    }
  });

  // ---- Open / close / load ----------------------------------------------------

  async function openDebug() {
    debugOverlay.hidden = false;
    if (!state.debugMode) {
      if (!modeOptions.length) await loadMode();
      // Seed from whatever the global switch currently shows, purely as a
      // starting point \u2014 from here the two are independent.
      state.debugMode = qs("#btn-mode-switch").dataset.mode || FALLBACK_MODE.mode;
    }
    renderDebugMode();
    renderDebugUsage();
    if (state.debugLoaded) return;
    debugStatusLine.textContent = "reading tool catalog\u2026";
    debugStatusLine.classList.add("is-busy");
    try {
      const tools = await Api.listTools();
      state.debugTools = Array.isArray(tools) ? tools : [];
      state.debugLoaded = true;
      debugStatusLine.textContent = `${state.debugTools.length} tools available`;
    } catch (e) {
      debugStatusLine.textContent = `couldn't load tools: ${e.message}`;
      debugStatusLine.classList.add("is-error");
    } finally {
      debugStatusLine.classList.remove("is-busy");
      renderDebugToolList();
    }
  }

  function closeDebug() {
    debugOverlay.hidden = true;
  }

  qs("#btn-debug").addEventListener("click", openDebug);
  qs("#debug-close").addEventListener("click", closeDebug);
  debugOverlay.addEventListener("click", (e) => { if (e.target === debugOverlay) closeDebug(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !debugOverlay.hidden) closeDebug();
  });

  // ===========================================================================
  // Logs overlay — conversation-scoped raw traffic between the model and
  // this backend (see logs.py). Left: every conversation that has at least
  // one logged entry. Middle: that conversation's entries (organized or raw
  // JSON), with refresh/clear. Right: settings, left empty for now.
  // ===========================================================================

  const logsOverlay = qs("#logs-overlay");
  const logsStatusLine = qs("#logs-status-line");
  const logsConvoList = qs("#logs-convo-list");
  const logsConvoCount = qs("#logs-convo-count");
  const logsEntriesEl = qs("#logs-entries");
  const logsEntriesTitle = qs("#logs-entries-title");
  const logsEntryCount = qs("#logs-entry-count");
  const btnLogsRefresh = qs("#btn-logs-refresh");
  const btnLogsClear = qs("#btn-logs-clear");

  function logsFilteredConvos() {
    const q = state.logsSearch.trim().toLowerCase();
    if (!q) return state.logsConvos;
    return state.logsConvos.filter((c) =>
      (c.title || "").toLowerCase().includes(q) || c.id.toLowerCase().includes(q)
    );
  }

  function renderLogsConvoList() {
    const items = logsFilteredConvos();
    logsConvoCount.textContent = `${state.logsConvos.length}`;
    logsConvoList.innerHTML = "";
    if (!items.length) {
      logsConvoList.appendChild(el("div", { class: "debug-empty" },
        state.logsConvos.length ? "No conversations match your search." : "No logs yet — logs are written as soon as you ask Jarvis something."));
      return;
    }
    for (const c of items) {
      const when = (c.updated_at || "").slice(0, 19).replace("T", " ");
      const card = el("div", {
        class: "logs-convo-card" + (c.id === state.logsSelected ? " is-active" : ""),
        onclick: () => logsSelectConvo(c.id),
      }, [
        el("div", { class: "logs-convo-card__title" + (c.exists ? "" : " is-deleted") }, c.title || c.id),
        el("div", { class: "logs-convo-card__meta" }, when ? `${when}  ·  ${c.id}` : c.id),
      ]);
      logsConvoList.appendChild(card);
    }
  }

  function logEntryDirClass(direction) {
    return `log-entry__dir--${(direction || "info").replace(/[^a-z_]/g, "")}`;
  }

  // Token count shown right next to the provider (key x/y) badge — see
  // logs.py (each entry's `data`). "usage" entries carry real,
  // provider-reported counts (token_usage.extract_usage); "tool_call" /
  // "tool_result" entries carry local ~estimates (token_usage.estimate_
  // tokens_for), since no provider reports per-tool-call token counts on
  // its own. Other directions (request/response/error/info) don't carry a
  // count of their own, so no badge is shown for them.
  function logEntryTokenLabel(entry) {
    const data = entry.data;
    if (!data || typeof data !== "object") return null;
    if (entry.direction === "usage") {
      const total = (data.input_tokens || 0) + (data.output_tokens || 0);
      return `${total} tok (in=${data.input_tokens || 0} out=${data.output_tokens || 0})`;
    }
    if (entry.direction === "tool_call") {
      return `~${data.input_tokens || 0} tok in`;
    }
    if (entry.direction === "tool_result") {
      return `~${data.output_tokens || 0} tok out`;
    }
    return null;
  }

  function renderLogsEntries() {
    logsEntriesEl.innerHTML = "";
    logsEntryCount.textContent = state.logsSelected ? `${state.logsEntries.length}` : "";
    if (!state.logsSelected) {
      logsEntriesEl.appendChild(el("div", { class: "debug-empty" }, "Select a conversation on the left."));
      return;
    }
    if (!state.logsEntries.length) {
      logsEntriesEl.appendChild(el("div", { class: "debug-empty" }, "(empty)"));
      return;
    }
    if (state.logsViewMode === "raw") {
      logsEntriesEl.appendChild(el("pre", { class: "log-entry__body" }, JSON.stringify(state.logsEntries, null, 2)));
      return;
    }
    for (const entry of state.logsEntries) {
      const tokenLabel = logEntryTokenLabel(entry);
      const head = el("div", { class: "log-entry__head" }, [
        el("span", { class: "log-entry__ts" }, (entry.ts || "").replace("T", " ").replace("Z", "") || "?"),
        el("span", { class: `log-entry__dir ${logEntryDirClass(entry.direction)}` }, entry.direction || "?"),
        entry.provider ? el("span", { class: "log-entry__provider" }, entry.provider) : null,
        tokenLabel ? el("span", { class: "log-entry__tokens" }, tokenLabel) : null,
        entry.round != null ? el("span", { class: "log-entry__round" }, `round ${entry.round}`) : null,
      ]);
      const body = el("pre", { class: "log-entry__body" }, JSON.stringify(entry.data, null, 2));
      logsEntriesEl.appendChild(el("div", { class: "log-entry" }, [head, body]));
    }
  }

  qs("#logs-view-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest(".debug-toggle-btn");
    if (!btn) return;
    state.logsViewMode = btn.dataset.mode;
    qsa(".debug-toggle-btn", qs("#logs-view-toggle")).forEach((b) => b.classList.toggle("is-active", b === btn));
    renderLogsEntries();
  });

  async function logsLoadEntries(convId) {
    logsStatusLine.textContent = "loading entries…";
    logsStatusLine.classList.add("is-busy");
    logsStatusLine.classList.remove("is-error", "is-ok");
    try {
      const res = await Api.getLog(convId, 50);
      state.logsEntries = (res && res.entries) || [];
      logsStatusLine.textContent = `${state.logsEntries.length} log line(s) for ${convId}`;
      logsStatusLine.classList.add("is-ok");
    } catch (e) {
      state.logsEntries = [];
      logsStatusLine.textContent = `couldn't load log: ${e.message}`;
      logsStatusLine.classList.add("is-error");
    } finally {
      logsStatusLine.classList.remove("is-busy");
      renderLogsEntries();
    }
  }

  function logsSelectConvo(id) {
    state.logsSelected = id;
    state.logsEntries = [];
    logsEntriesTitle.textContent = "Log";
    const convo = state.logsConvos.find((c) => c.id === id);
    if (convo) logsEntriesTitle.textContent = convo.title || id;
    btnLogsRefresh.disabled = false;
    btnLogsClear.disabled = false;
    renderLogsConvoList();
    renderLogsEntries();
    logsLoadEntries(id);
  }

  qs("#logs-convo-search").addEventListener("input", (e) => {
    state.logsSearch = e.target.value;
    renderLogsConvoList();
  });

  btnLogsRefresh.addEventListener("click", () => {
    if (state.logsSelected) logsLoadEntries(state.logsSelected);
  });

  btnLogsClear.addEventListener("click", async () => {
    if (!state.logsSelected) return;
    const id = state.logsSelected;
    if (!confirm(`Clear the log for "${logsEntriesTitle.textContent}"? This can't be undone.`)) return;
    btnLogsClear.disabled = true;
    try {
      await Api.clearLog(id);
      state.logsEntries = [];
      state.logsConvos = state.logsConvos.filter((c) => c.id !== id);
      state.logsSelected = null;
      logsEntriesTitle.textContent = "Log";
      btnLogsRefresh.disabled = true;
      renderLogsConvoList();
      renderLogsEntries();
      toast("Log cleared.", "info");
    } catch (e) {
      toast(`Couldn't clear log: ${e.message}`);
      btnLogsClear.disabled = false;
    }
  });

  async function openLogs() {
    logsOverlay.hidden = false;
    if (state.logsLoaded) { renderLogsConvoList(); renderLogsEntries(); return; }
    logsStatusLine.textContent = "reading conversations…";
    logsStatusLine.classList.add("is-busy");
    try {
      const items = await Api.listLogs();
      state.logsConvos = Array.isArray(items) ? items : [];
      state.logsLoaded = true;
      logsStatusLine.textContent = `${state.logsConvos.length} conversation(s) with logs`;
    } catch (e) {
      logsStatusLine.textContent = `couldn't load logs: ${e.message}`;
      logsStatusLine.classList.add("is-error");
    } finally {
      logsStatusLine.classList.remove("is-busy");
      renderLogsConvoList();
      renderLogsEntries();
    }
  }

  function closeLogs() {
    logsOverlay.hidden = true;
  }

  qs("#btn-logs-fab").addEventListener("click", openLogs);
  qs("#logs-close").addEventListener("click", closeLogs);
  logsOverlay.addEventListener("click", (e) => { if (e.target === logsOverlay) closeLogs(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !logsOverlay.hidden) closeLogs();
  });


  // ===========================================================================
  // Conversations — every chat with at least one message is saved (see
  // jarvis-cli's conversations.py), switchable and searchable from the
  // sidebar inside the Ask panel. Opening the page always starts a fresh
  // one; switching to an older one restores it with full context. A
  // conversation that never gets a message — opened and abandoned, e.g.
  // by just loading the page or clicking "+ New" and never sending
  // anything — is never written into that history in the first place.
  // ===========================================================================

  const convoListEl = qs("#convo-list");

  // A plain, already-finished reply bubble — used to replay a saved
  // conversation's history, as opposed to addJarvisBubblePending() +
  // appendAskReplyLine()/finalizeAskBubble(), which animate a live one in.
  function addJarvisStaticBubble(text) {
    clearAskEmptyHint();
    const msg = el("div", { class: "ask-msg ask-msg--jarvis" }, [
      el("div", { class: "ask-msg__role" }, "Jarvis"),
      el("div", { class: "ask-msg__bubble" }),
    ]);
    msg.dataset.raw = text || "";
    const bubbleEl = qs(".ask-msg__bubble", msg);
    bubbleEl.innerHTML = renderMarkdown(text || "");
    linkifyPaths(bubbleEl);
    addAskMsgActions(msg);
    askThread.appendChild(msg);
    return msg;
  }

  // The server now saves each turn's screenshots/downloads/organize_json
  // results/confirm decisions alongside it (see conversations.append_exchange
  // and ai_client._extras_from_runs) — this seeds state.threadExtrasByConv
  // from that saved data the first time a conversation is loaded in this
  // tab, so replay works after a genuine page reload and not just when
  // switching between conversations within the same session. Once seeded,
  // in-memory items pushed during a live ask (pushThreadExtra) take over
  // for the rest of the tab's lifetime.
  function seedThreadExtrasFromRecord(record, convId) {
    if (convId == null || state.threadExtrasByConv[convId]) return;
    const extras = [];
    (record.exchanges || []).forEach((ex, bucket) => {
      for (const e of ex.extras || []) {
        if (e && e.type) extras.push({ bucket, type: e.type, data: e.data || {} });
      }
    });
    state.threadExtrasByConv[convId] = extras;
  }

  function loadConversationIntoThread(record, convId) {
    seedThreadExtrasFromRecord(record, convId);
    askThread.innerHTML = "";
    const exchanges = (record && record.exchanges) || [];
    const extras = (convId != null && state.threadExtrasByConv[convId]) || [];
    const renderExtrasForBucket = (bucket) => {
      for (const item of extras) {
        if (item.bucket === bucket) renderThreadExtra(item);
      }
    };
    if (!exchanges.length) {
      renderExtrasForBucket(0);
      if (!askThread.children.length) {
        askThread.appendChild(el("div", { class: "ask-empty" }, "Ask about anything, or tell me what you need done, sir."));
      }
    } else {
      exchanges.forEach((ex, i) => {
        addUserBubble(ex.user || "");
        addJarvisStaticBubble(ex.jarvis || "");
        renderExtrasForBucket(i);
      });
      // Extras for the turn currently in flight (or one that failed after
      // producing media but before finishing) live past the last saved
      // exchange, at bucket === exchanges.length.
      renderExtrasForBucket(exchanges.length);
    }
    askThreadScrollToEnd();
  }

  function convoFiltered() {
    const q = state.convoSearch.trim().toLowerCase();
    if (!q) return state.conversations;
    return state.conversations.filter((c) =>
      (c.title || "").toLowerCase().includes(q) || (c.soft_context || "").toLowerCase().includes(q)
    );
  }

  function renderConvoList() {
    const items = convoFiltered();
    convoListEl.innerHTML = "";
    if (!items.length) {
      convoListEl.appendChild(el("div", { class: "ask-convos__empty" },
        state.conversations.length ? "No chats match your search." : "No conversations yet."));
      return;
    }
    for (const convo of items) {
      const gist = (convo.soft_context || "").trim();
      const card = el("div", {
        class: "convo-card" + (convo.id === state.activeConversationId ? " is-active" : ""),
        onclick: () => selectConversation(convo.id),
      }, [
        el("div", { class: "convo-card__title" }, convo.title || "New Conversation"),
        gist
          ? el("div", { class: "convo-card__gist" }, gist)
          : el("div", { class: "convo-card__empty-gist" }, convo.exchange_count ? "\u2026" : "No messages yet"),
        el("button", {
          type: "button", class: "convo-card__del", title: "Delete this conversation",
          onclick: (e) => { e.stopPropagation(); deleteConversationConfirm(convo.id); },
        }, "\u00d7"),
      ]);
      convoListEl.appendChild(card);
    }
  }

  async function refreshConvoList() {
    // A brand-new conversation isn't written into the server-side index
    // until it has a first real exchange (see conversations.py's
    // new_conversation/append_exchange) — specifically so an "opened but
    // never used" conversation doesn't clutter conversation history.
    // That means if we're currently sitting in one, the server plainly
    // won't include it in this fetch. Hang onto it locally so it doesn't
    // vanish out of the sidebar mid-session just because something else
    // triggered a list refresh — it only actually disappears (as
    // intended) once the session ends without ever sending a message.
    const activeIfStillEmpty = state.conversations.find((c) => c.id === state.activeConversationId);
    try {
      state.conversations = await Api.listConversations(state.convoSearch);
    } catch (e) {
      convoListEl.innerHTML = "";
      convoListEl.appendChild(el("div", { class: "ask-convos__empty" }, `Couldn't load chats: ${e.message}`));
      return;
    }
    if (
      activeIfStillEmpty &&
      !state.conversations.some((c) => c.id === state.activeConversationId)
    ) {
      state.conversations.unshift(activeIfStillEmpty);
    }
    renderConvoList();
  }

  async function selectConversation(id) {
    if (id === state.activeConversationId) return;
    // Switching is always allowed, even mid-reply: if an ask is still in
    // flight for the conversation we're leaving, it keeps running in the
    // background and saves normally — we just stop painting it into a
    // thread that isn't showing it anymore (see isViewingAskThread()).
    if (state.running && state.askConversationId && state.askConversationId === state.activeConversationId) {
      toast("Still replying in the other chat \u2014 it'll be saved there.", "info");
    }
    let record;
    try {
      record = await Api.getConversation(id);
    } catch (e) {
      toast(`Couldn't load that conversation: ${e.message}`);
      return;
    }
    state.activeConversationId = id;
    state.exchangeCountByConv[id] = (record.exchanges || []).length;
    loadConversationIntoThread(record, id);
    // If this conversation's ask is still running in the background, the
    // thread rebuild above just destroyed the old pending-bubble DOM node
    // (see insertIntoAskThread) — make a fresh one and repaint whatever's
    // accumulated so far, so live updates have somewhere valid to land.
    if (state.running && state.askConversationId === id) {
      state.askPendingBubble = addJarvisBubblePending();
      rerenderAskPendingBubble();
      setAskStatus("thinking\u2026", "busy");
    }
    renderAskTraceForConv(id);
    refreshAskBusyUI();
    const pending = state.pendingConfirmByConv[id];
    if (pending) {
      addAskConfirmBubble(pending.tool, pending.arguments, pending.risk_note, id, pending.extraItem);
      setAskStatus("waiting for your confirmation\u2026", "busy");
    }
    renderConvoList();
  }

  // Used both by the "+ New" button and automatically once on page load
  // (see initApp) — every fresh page load starts a brand-new conversation,
  // while older ones stay saved and reachable from the sidebar.
  async function startNewConversation({ select = true } = {}) {
    let record;
    try {
      record = await Api.createConversation();
    } catch (e) {
      toast(`Couldn't start a new conversation: ${e.message}`);
      return null;
    }
    state.conversations.unshift(record);
    if (select) {
      state.activeConversationId = record.id;
      state.exchangeCountByConv[record.id] = 0;
      loadConversationIntoThread({ exchanges: [] }, record.id);
      askPromptReset();
      refreshAskBusyUI();
    }
    renderConvoList();
    return record.id;
  }

  async function deleteConversationConfirm(id) {
    if (!confirm("Delete this conversation? This can't be undone.")) return;
    try {
      await Api.deleteConversation(id);
    } catch (e) {
      toast(`Couldn't delete: ${e.message}`);
      return;
    }
    delete state.askTraceByConv[id];
    delete state.pendingConfirmByConv[id];
    delete state.threadExtrasByConv[id];
    delete state.exchangeCountByConv[id];
    state.conversations = state.conversations.filter((c) => c.id !== id);
    if (id === state.activeConversationId) {
      // Land somewhere sane: the next most recent chat, or a brand-new one.
      if (state.conversations.length) {
        await selectConversation(state.conversations[0].id);
      } else {
        await startNewConversation();
      }
    } else {
      renderConvoList();
    }
    toast("Conversation deleted.", "info");
  }

  qs("#btn-convo-new").addEventListener("click", () => startNewConversation());
  qs("#convo-search").addEventListener("input", (e) => {
    state.convoSearch = e.target.value;
    refreshConvoList();
  });

  // ===========================================================================
  // Command builder modal
  // ===========================================================================

  const backdrop = qs("#modal-backdrop");

  function openBuilder(mode, name) {
    state.editingOriginalName = mode === "edit" ? name : null;
    qs("#modal-title").textContent = mode === "edit" ? `Edit \u201c${name}\u201d` : "New Command";
    qs("#modal-error").textContent = "";
    qs("#raw-json-error").textContent = "";
    setBuilderTab("builder");

    const spec = mode === "edit" ? state.commands[name] : { description: "", run: [""], vars: {} };
    qs("#f-name").value = mode === "edit" ? name : "";
    qs("#f-desc").value = spec.description || "";
    qs("#f-confirm-required").checked = !!spec.confirm_required;
    qs("#f-ai-review").checked = !!spec.ai_review;

    buildVarsEditor(spec.vars || {});
    buildStepsEditor(normalizeSteps(spec.run));
    refreshVarSync();
    syncBuilderToRaw();

    backdrop.hidden = false;
    qs("#f-name").focus();
  }

  function closeBuilder() { backdrop.hidden = true; }

  qs("#cmd-search").addEventListener("input", (e) => {
    state.cmdSearch = e.target.value;
    renderCommandList();
  });

  qs("#btn-new-command").addEventListener("click", () => openBuilder("new"));
  qs("#modal-close").addEventListener("click", closeBuilder);
  qs("#btn-cancel-modal").addEventListener("click", closeBuilder);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeBuilder(); });

  // ===========================================================================
  // Settings modal — every *.json file in the jarvis config dir, auto-
  // discovered from GET /api/config/list (see server.js's KNOWN_CONFIGS /
  // generic config-file browser). No hardcoded tab list: drop a new *.json
  // file in that directory and it shows up here with no HTML/JS change.
  // Each tab gets an editable collapsible tree (buildJsonTree, editable:true)
  // plus a Raw JSON toggle for bulk/paste edits — mirroring the organize-json
  // chat bubble's Organized/Raw split, just editable here.
  // ===========================================================================

  const settingsBackdrop = qs("#settings-backdrop");
  const settingsTabsEl = qs("#settings-tabs");
  const settingsBodyEl = qs("#settings-body");

  let settingsFiles = [];        // [{name, label, hint, path}] from /api/config/list
  let settingsActiveTab = null;  // active file name, e.g. "commands.json"
  const settingsFileState = {};  // name -> {path, text, data, parseError, mode, dirty}

  function settingsFileMeta(name) {
    return settingsFiles.find((f) => f.name === name);
  }

  function settingsPaneEl(name) {
    return qs(`.settings-pane[data-settings-pane="${name}"]`, settingsBodyEl);
  }

  function clearSettingsErrors() {
    qs("#settings-error-global").textContent = "";
    qsa(".settings-pane__error", settingsBodyEl).forEach((e) => { e.textContent = ""; });
  }

  function buildSettingsTabs() {
    settingsTabsEl.innerHTML = "";
    settingsFiles.forEach((f) => {
      settingsTabsEl.appendChild(el("button", {
        type: "button",
        class: "tab-btn settings-tab-btn",
        "data-settings-tab": f.name,
        title: f.hint || "",
        onclick: () => selectSettingsTab(f.name),
      }, f.label || f.name));
    });
    updateSettingsTabsFade();
  }

  // Keeps the scrollable tab strip's edge fades honest: only fades the
  // side(s) that actually have more tabs hidden off-screen, so a short
  // list (nothing to scroll) shows no fade at all, and a strip scrolled
  // all the way to one end doesn't fade the end it's already at.
  const SETTINGS_TABS_FADE_PX = 18;
  function updateSettingsTabsFade() {
    const maxScroll = settingsTabsEl.scrollWidth - settingsTabsEl.clientWidth;
    if (maxScroll <= 1) {
      settingsTabsEl.style.setProperty("--settings-tabs-fade-l", "0px");
      settingsTabsEl.style.setProperty("--settings-tabs-fade-r", "0px");
      return;
    }
    const atStart = settingsTabsEl.scrollLeft <= 1;
    const atEnd = settingsTabsEl.scrollLeft >= maxScroll - 1;
    settingsTabsEl.style.setProperty("--settings-tabs-fade-l", atStart ? "0px" : `${SETTINGS_TABS_FADE_PX}px`);
    settingsTabsEl.style.setProperty("--settings-tabs-fade-r", atEnd ? "0px" : `${SETTINGS_TABS_FADE_PX}px`);
  }
  settingsTabsEl.addEventListener("scroll", updateSettingsTabsFade, { passive: true });
  window.addEventListener("resize", () => {
    if (!settingsBackdrop.hidden) updateSettingsTabsFade();
  });
  // The strip only scrolls horizontally, so a plain vertical mouse-wheel
  // (no shift held) would otherwise do nothing over it — translate it to
  // horizontal scroll, same convention as most horizontal tab/chip rows.
  settingsTabsEl.addEventListener("wheel", (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    if (settingsTabsEl.scrollWidth <= settingsTabsEl.clientWidth) return;
    e.preventDefault();
    settingsTabsEl.scrollLeft += e.deltaY;
  }, { passive: false });

  function buildSettingsPanes() {
    settingsBodyEl.innerHTML = "";
    settingsFiles.forEach((f) => {
      const btnOrganized = el("button", {
        type: "button", class: "debug-toggle-btn is-active", "data-view": "organized",
        onclick: () => setSettingsPaneMode(f.name, "organized"),
      }, "Organized");
      const btnRaw = el("button", {
        type: "button", class: "debug-toggle-btn", "data-view": "raw",
        onclick: () => setSettingsPaneMode(f.name, "raw"),
      }, "Raw JSON");

      settingsBodyEl.appendChild(el("div", { class: "settings-pane", "data-settings-pane": f.name }, [
        el("div", { class: "settings-pane__hint" }, f.hint || ""),
        el("div", { class: "raw-config-path settings-pane__path" }, ""),
        el("div", { class: "settings-view-toggle" }, [btnOrganized, btnRaw]),
        el("div", { class: "settings-pane__view" }, [
          el("div", { class: "settings-empty" }, "Loading\u2026"),
        ]),
        el("div", { class: "modal__error settings-pane__error" }),
      ]));
    });
  }

  function setActiveSettingsTabUi(name) {
    qsa(".settings-tab-btn", settingsTabsEl).forEach((b) => {
      const isActive = b.dataset.settingsTab === name;
      b.classList.toggle("is-active", isActive);
      // "nearest" (not "center"/"start") so this never fights the user's
      // own scroll position when the tab is already fully visible — it
      // only moves the strip the minimum needed to bring a newly
      // selected, currently-clipped tab (e.g. picked via openSettings()
      // with an initialName, or a keyboard/programmatic switch) on screen.
      if (isActive) b.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    qsa(".settings-pane", settingsBodyEl).forEach((p) => {
      p.classList.toggle("is-active", p.dataset.settingsPane === name);
    });
    updateSettingsTabsFade();
  }

  // Renders whichever view (tree or raw textarea) matches the file's
  // current mode. Called after loading a file, after Save/Reload, and
  // whenever the Organized/Raw toggle is flipped.
  function renderSettingsPaneContent(name) {
    const st = settingsFileState[name];
    const pane = settingsPaneEl(name);
    if (!st || !pane) return;
    qsa(".debug-toggle-btn", pane).forEach((b) => b.classList.toggle("is-active", b.dataset.view === st.mode));
    const viewWrap = qs(".settings-pane__view", pane);
    viewWrap.innerHTML = "";

    if (st.mode === "raw") {
      const ta = el("textarea", { class: "settings-json", spellcheck: "false" });
      ta.value = st.text;
      ta.addEventListener("input", () => { st.text = ta.value; st.dirty = true; });
      viewWrap.appendChild(ta);
      return;
    }

    if (st.parseError) {
      viewWrap.appendChild(el("div", { class: "json-org-error" },
        `Can't show a tree \u2014 invalid JSON: ${st.parseError}`));
      return;
    }
    const treeMount = el("div", { class: "json-tree settings-tree" });
    viewWrap.appendChild(treeMount);
    buildJsonTree(treeMount, st.data, {
      editable: true,
      onChange: () => {
        st.text = JSON.stringify(st.data, null, 2) + "\n";
        st.dirty = true;
      },
    });
  }

  function setSettingsPaneMode(name, mode) {
    const st = settingsFileState[name];
    const pane = settingsPaneEl(name);
    if (!st || !pane) return;
    if (mode === "organized" && st.mode !== "organized") {
      // Coming from Raw — re-parse whatever text is sitting there now,
      // since the person may have hand-edited it directly.
      try {
        st.data = JSON.parse(st.text);
        st.parseError = null;
      } catch (e) {
        qs(".settings-pane__error", pane).textContent = `Can't switch to Organized \u2014 invalid JSON: ${e.message}`;
        return;
      }
    }
    qs(".settings-pane__error", pane).textContent = "";
    st.mode = mode;
    renderSettingsPaneContent(name);
  }

  async function selectSettingsTab(name, { force = false } = {}) {
    settingsActiveTab = name;
    setActiveSettingsTabUi(name);
    const pane = settingsPaneEl(name);
    if (!pane) return;
    qs(".settings-pane__error", pane).textContent = "";

    if (!settingsFileState[name] || force) {
      try {
        const data = await Api.getConfigFile(name);
        const st = { path: data.path, text: data.text, mode: "organized", dirty: false };
        try {
          st.data = JSON.parse(st.text);
          st.parseError = null;
        } catch (e) {
          st.data = null;
          st.parseError = e.message;
          st.mode = "raw"; // can't build a tree out of invalid JSON — show the text instead
        }
        settingsFileState[name] = st;
      } catch (e) {
        qs(".settings-pane__error", pane).textContent = e.message;
        return;
      }
    }
    qs(".settings-pane__path", pane).textContent = settingsFileState[name].path;
    renderSettingsPaneContent(name);
  }

  async function openSettings(initialName) {
    clearSettingsErrors();
    settingsBackdrop.hidden = false;
    Object.keys(settingsFileState).forEach((k) => delete settingsFileState[k]);
    try {
      const list = await Api.configList();
      settingsFiles = list.files || [];
    } catch (e) {
      settingsFiles = [];
      toast(e.message);
    }
    buildSettingsTabs();
    buildSettingsPanes();
    const target = (initialName && settingsFiles.some((f) => f.name === initialName))
      ? initialName
      : (settingsFiles[0] && settingsFiles[0].name);
    if (target) {
      await selectSettingsTab(target, { force: true });
    } else {
      settingsBodyEl.appendChild(el("div", { class: "settings-empty" }, "No config files found."));
    }
  }

  function closeSettings() {
    settingsBackdrop.hidden = true;
  }

  // Called from applyCommandsToUi whenever commands.json changes on the
  // server (another tab's edit, the CLI, a live broadcast) — if that tab is
  // open in Settings right now, refresh it in place instead of letting a
  // later Save silently clobber the newer version with stale in-memory state.
  function syncCommandsIntoSettingsTab(commands) {
    const st = settingsFileState["commands.json"];
    if (!st || settingsBackdrop.hidden) return;
    const text = JSON.stringify({ commands }, null, 2) + "\n";
    st.text = text;
    try {
      st.data = JSON.parse(text);
      st.parseError = null;
    } catch { /* JSON.stringify output is always valid JSON */ }
    if (settingsActiveTab === "commands.json") renderSettingsPaneContent("commands.json");
  }

  qs("#btn-settings").addEventListener("click", () => openSettings("commands.json"));
  qs("#settings-close").addEventListener("click", closeSettings);
  qs("#btn-settings-cancel").addEventListener("click", closeSettings);
  settingsBackdrop.addEventListener("click", (e) => { if (e.target === settingsBackdrop) closeSettings(); });

  qs("#btn-settings-reload").addEventListener("click", async () => {
    clearSettingsErrors();
    if (!settingsActiveTab) return;
    try {
      await selectSettingsTab(settingsActiveTab, { force: true });
      toast("Reloaded from disk.", "info");
    } catch (e) {
      toast(e.message);
    }
  });

  qs("#btn-settings-save").addEventListener("click", async () => {
    clearSettingsErrors();
    const name = settingsActiveTab;
    if (!name) return;
    const st = settingsFileState[name];
    const pane = settingsPaneEl(name);
    if (!st || !pane) return;
    const errEl = qs(".settings-pane__error", pane);

    // Organized mode keeps st.text in sync on every tree edit (onChange
    // above); Raw mode's textarea does the same on input — either way
    // st.text is the thing to validate and send.
    try {
      JSON.parse(st.text);
    } catch (e) {
      errEl.textContent = `Invalid JSON: ${e.message}`;
      return;
    }
    try {
      await Api.putConfigFile(name, st.text);
      st.dirty = false;
      if (name === "commands.json") await loadCommands({ silent: true });
      closeSettings();
      const meta = settingsFileMeta(name);
      toast(`${(meta && meta.label) || name} saved.`, "info");
    } catch (e) {
      errEl.textContent = e.message;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!settingsBackdrop.hidden) closeSettings();
    else if (!backdrop.hidden) closeBuilder();
  });

  function setBuilderTab(tab) {
    qsa("#modal-backdrop .tab-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === tab));
    qsa("#modal-backdrop .tab-pane").forEach((p) => p.classList.toggle("is-active", p.dataset.pane === tab));
  }

  qsa("#modal-backdrop .tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      if (target === "raw" && !qs('#modal-backdrop .tab-btn[data-tab="builder"]').disabled) {
        syncBuilderToRaw();
      } else if (target === "builder") {
        if (!applyRawToBuilder()) return;
      }
      setBuilderTab(target);
    });
  });

  // --- Vars editor -----------------------------------------------------------

  function buildVarsEditor(varsObj) {
    const holder = qs("#vars-editor");
    holder.innerHTML = "";
    const entries = Object.entries(varsObj || {});
    if (entries.length === 0) addVarRow();
    else entries.forEach(([name, v]) => addVarRow(name, v.default, v.description));
  }

  function addVarRow(name = "", def, desc = "") {
    const tmpl = qs("#tmpl-var").content.cloneNode(true);
    const row = tmpl.querySelector(".var-row");
    row.querySelector(".var-name").value = name;
    row.querySelector(".var-default").value = def == null ? "" : String(def);
    row.querySelector(".var-desc").value = desc || "";
    row.querySelector(".var-remove").addEventListener("click", () => { row.remove(); refreshVarSync(); });
    row.querySelector(".var-name").addEventListener("input", refreshVarSync);
    qs("#vars-editor").appendChild(row);
    refreshVarSync();
  }

  qs("#btn-add-var").addEventListener("click", () => addVarRow());

  function currentVarNames() {
    return qsa("#vars-editor .var-name").map((i) => i.value.trim()).filter(Boolean);
  }

  function refreshVarSync() {
    const names = currentVarNames();
    // condition-row <select> options
    qsa(".cond-var").forEach((sel) => {
      const prev = sel.value;
      sel.innerHTML = "";
      names.forEach((n) => sel.appendChild(el("option", { value: n }, n)));
      if (names.includes(prev)) sel.value = prev;
    });
    // insert-var chips per step
    qsa(".step-card").forEach((card) => {
      let chipRow = qs(".insert-chips", card);
      if (!chipRow) {
        chipRow = el("div", { class: "insert-chips" });
        chipRow.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;margin:-4px 0 10px;";
        card.querySelector(".step-run").closest(".field").after(chipRow);
      }
      chipRow.innerHTML = "";
      names.forEach((n) => {
        const chip = el("button", { type: "button", class: "btn btn--ghost btn--sm" }, `{${n}}`);
        chip.style.padding = "2px 8px";
        chip.addEventListener("click", () => insertAtCursor(card.querySelector(".step-run"), `{${n}}`));
        chipRow.appendChild(chip);
      });
    });
  }

  function insertAtCursor(input, text) {
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    input.focus();
    input.selectionStart = input.selectionEnd = start + text.length;
  }

  // --- Steps editor ------------------------------------------------------------

  function normalizeSteps(run) {
    const list = Array.isArray(run) ? run : [run];
    return list.map((s) => (typeof s === "string" ? { run: s } : { ...s }));
  }

  // Shared by both the Builder and Raw JSON tabs so neither path can save a
  // command with a blank step — jarvis would accept it and then fail
  // confusingly (or no-op) the moment someone actually ran it.
  function findSpecError(spec) {
    if (typeof spec !== "object" || spec === null || Array.isArray(spec)) {
      return "Command spec must be an object.";
    }
    if (spec.run === undefined || spec.run === null) {
      return "Command must have a 'run' (string or list of steps).";
    }
    const steps = normalizeSteps(spec.run);
    if (steps.length === 0) return "Add at least one step.";
    const blankIdx = steps.findIndex((s) => typeof s.run !== "string" || s.run.trim() === "");
    if (blankIdx !== -1) return `Step ${blankIdx + 1} needs a command to run.`;
    return null;
  }

  function buildStepsEditor(steps) {
    const holder = qs("#steps-editor");
    holder.innerHTML = "";
    if (steps.length === 0) steps = [{ run: "" }];
    steps.forEach((s) => addStepCard(s));
  }

  // The "parallel with previous" checkbox only makes sense from the second
  // step onward \u2014 there's nothing before the first step to run alongside.
  // Called after anything that can change step order (add, remove, move,
  // drag-drop) so the right card's checkbox is the one hidden.
  function refreshStepParallelVisibility() {
    qsa("#steps-editor .step-card").forEach((card, i) => {
      const wrap = qs(".step-parallel-wrap", card);
      if (!wrap) return;
      wrap.hidden = i === 0;
      if (i === 0) card.querySelector(".step-parallel").checked = false;
    });
  }

  function addStepCard(step = {}) {
    const tmpl = qs("#tmpl-step").content.cloneNode(true);
    const card = tmpl.querySelector(".step-card");

    card.querySelector(".step-name").value = step.name || "";
    card.querySelector(".step-run").value = step.run || "";
    card.querySelector(".step-coe").checked = !!step.continueOnError;
    card.querySelector(".step-parallel").checked = !!step.parallel;
    card.querySelector(".step-show-cmd").checked = step.showCommand !== false;

    card.querySelector(".step-remove").addEventListener("click", () => {
      if (qsa("#steps-editor .step-card").length <= 1) { toast("A command needs at least one step."); return; }
      card.remove();
      refreshStepParallelVisibility();
    });
    card.querySelector(".step-up").addEventListener("click", () => {
      const prev = card.previousElementSibling;
      if (prev) card.parentNode.insertBefore(card, prev);
      refreshStepParallelVisibility();
    });
    card.querySelector(".step-down").addEventListener("click", () => {
      const next = card.nextElementSibling;
      if (next) card.parentNode.insertBefore(next, card);
      refreshStepParallelVisibility();
    });

    setupCondBlock(card, "if", step.if);
    setupCondBlock(card, "unless", step.unless);

    // drag reorder
    card.addEventListener("dragstart", () => card.classList.add("is-dragging"));
    card.addEventListener("dragend", () => { card.classList.remove("is-dragging"); refreshStepParallelVisibility(); });
    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      const dragging = qs(".is-dragging", holderOf(card));
      if (!dragging || dragging === card) return;
      const rect = card.getBoundingClientRect();
      const after = e.clientY - rect.top > rect.height / 2;
      card.parentNode.insertBefore(dragging, after ? card.nextSibling : card);
    });

    qs("#steps-editor").appendChild(card);
    refreshVarSync();
    refreshStepParallelVisibility();
  }

  function holderOf(node) { return node.parentNode; }

  qs("#btn-add-step").addEventListener("click", () => addStepCard());

  function setupCondBlock(card, kind, initialValue) {
    const checkbox = card.querySelector(kind === "if" ? ".step-has-if" : ".step-has-unless");
    const rowsHolder = card.querySelector(kind === "if" ? ".step-card__if-rows" : ".step-card__unless-rows");
    let mode = "simple"; // or "advanced"

    function render() {
      rowsHolder.innerHTML = "";
      const modeBar = el("div", { style: "display:flex;justify-content:space-between;align-items:center;" }, [
        el("span", { class: "cond-group-label" }, kind.toUpperCase()),
        el("button", {
          type: "button", class: "btn btn--ghost btn--sm",
          onclick: () => { mode = mode === "simple" ? "advanced" : "simple"; render(); },
        }, mode === "simple" ? "use expression instead" : "use simple rows instead"),
      ]);
      rowsHolder.appendChild(modeBar);

      if (mode === "simple") {
        rowsHolder.dataset.rows = "";
        const addRow = (varName = "", val = "") => {
          const t = qs("#tmpl-cond-row").content.cloneNode(true);
          const row = t.querySelector(".cond-row");
          const names = currentVarNames();
          names.forEach((n) => row.querySelector(".cond-var").appendChild(el("option", { value: n }, n)));
          if (names.includes(varName)) row.querySelector(".cond-var").value = varName;
          row.querySelector(".cond-val").value = val;
          row.querySelector(".cond-remove").addEventListener("click", () => row.remove());
          rowsHolder.appendChild(row);
        };
        rowsHolder._addRow = addRow;
        rowsHolder._getValue = () => {
          const out = {};
          qsa(".cond-row", rowsHolder).forEach((row) => {
            const k = row.querySelector(".cond-var").value;
            const v = row.querySelector(".cond-val").value;
            if (k) out[k] = v;
          });
          return out;
        };
        const initial = initialValue && typeof initialValue === "object" ? initialValue : {};
        const entries = Object.entries(initial);
        if (entries.length === 0) addRow();
        else entries.forEach(([k, v]) => addRow(k, Array.isArray(v) ? v.join(",") : v));
        rowsHolder.appendChild(el("button", {
          type: "button", class: "btn btn--ghost btn--sm cond-add",
          onclick: () => addRow(),
        }, "+ AND condition"));
      } else {
        const ta = el("input", {
          type: "text", class: "cond-expr",
          placeholder: "env == 'prod' and (region == 'us' or region == 'eu')",
          value: typeof initialValue === "string" ? initialValue : "",
        });
        rowsHolder._getValue = () => ta.value.trim();
        rowsHolder.appendChild(ta);
      }
    }

    checkbox.checked = initialValue != null;
    rowsHolder.hidden = !checkbox.checked;
    if (typeof initialValue === "string") mode = "advanced";
    render();

    checkbox.addEventListener("change", () => { rowsHolder.hidden = !checkbox.checked; });

    card[`_get_${kind}`] = () => {
      if (!checkbox.checked) return undefined;
      const v = rowsHolder._getValue();
      if (mode === "simple" && Object.keys(v).length === 0) return undefined;
      if (mode === "advanced" && !v) return undefined;
      return v;
    };
  }

  // --- Builder <-> spec object -------------------------------------------------

  function collectVarsFromEditor() {
    const vars = {};
    qsa("#vars-editor .var-row").forEach((row) => {
      const name = row.querySelector(".var-name").value.trim();
      if (!name) return;
      const def = row.querySelector(".var-default").value;
      const desc = row.querySelector(".var-desc").value.trim();
      const entry = {};
      if (def !== "") entry.default = def;
      if (desc) entry.description = desc;
      vars[name] = entry;
    });
    return vars;
  }

  function collectStepsFromEditor() {
    return qsa("#steps-editor .step-card").map((card, i) => {
      const step = { run: card.querySelector(".step-run").value };
      const name = card.querySelector(".step-name").value.trim();
      if (name) step.name = name;
      const ifVal = card._get_if && card._get_if();
      const unlessVal = card._get_unless && card._get_unless();
      if (ifVal !== undefined) step.if = ifVal;
      if (unlessVal !== undefined) step.unless = unlessVal;
      if (card.querySelector(".step-coe").checked) step.continueOnError = true;
      // Meaningless (and never shown) on the first step \u2014 nothing precedes
      // it to run alongside \u2014 so it's never written even if the checkbox
      // somehow ended up checked before this card became the first one.
      if (i > 0 && card.querySelector(".step-parallel").checked) step.parallel = true;
      // Default is "shown"; only write the field when it's turned off, so
      // a plain spec with nothing toggled stays exactly as compact as before.
      if (!card.querySelector(".step-show-cmd").checked) step.showCommand = false;
      return step;
    });
  }

  function collectSpecFromBuilder() {
    const description = qs("#f-desc").value.trim();
    const vars = collectVarsFromEditor();
    const steps = collectStepsFromEditor();
    let run;
    if (steps.length === 1 && !steps[0].name && steps[0].if === undefined && steps[0].unless === undefined && !steps[0].continueOnError) {
      run = steps[0].run;
    } else {
      run = steps;
    }
    const spec = { description, run, vars };
    // Only write these when turned on, so a plain command with neither
    // toggled stays exactly as compact as before (mirrors the step-level
    // showCommand pattern above).
    if (qs("#f-confirm-required").checked) spec.confirm_required = true;
    if (qs("#f-ai-review").checked) spec.ai_review = true;
    return spec;
  }

  function syncBuilderToRaw() {
    const spec = collectSpecFromBuilder();
    qs("#raw-json").value = JSON.stringify(spec, null, 2);
    qs("#raw-json-error").textContent = "";
  }

  function applyRawToBuilder() {
    let parsed;
    try {
      parsed = JSON.parse(qs("#raw-json").value);
    } catch (e) {
      qs("#raw-json-error").textContent = `Invalid JSON: ${e.message}`;
      return false;
    }
    if (typeof parsed !== "object" || parsed === null || parsed.run === undefined) {
      qs("#raw-json-error").textContent = "Needs at least a 'run' field.";
      return false;
    }
    qs("#f-desc").value = parsed.description || "";
    qs("#f-confirm-required").checked = !!parsed.confirm_required;
    qs("#f-ai-review").checked = !!parsed.ai_review;
    buildVarsEditor(parsed.vars || {});
    buildStepsEditor(normalizeSteps(parsed.run));
    refreshVarSync();
    qs("#raw-json-error").textContent = "";
    return true;
  }

  qs("#btn-save-command").addEventListener("click", async () => {
    qs("#modal-error").textContent = "";
    let spec;
    if (qs('.tab-pane[data-pane="raw"]').classList.contains("is-active")) {
      try {
        spec = JSON.parse(qs("#raw-json").value);
      } catch (e) {
        qs("#modal-error").textContent = `Invalid JSON: ${e.message}`;
        return;
      }
    } else {
      spec = collectSpecFromBuilder();
    }
    const name = qs("#f-name").value.trim();
    if (!name) { qs("#modal-error").textContent = "Command name is required."; return; }
    const specErr = findSpecError(spec);
    if (specErr) { qs("#modal-error").textContent = specErr; return; }

    try {
      const originalName = state.editingOriginalName;
      if (originalName) {
        await Api.updateCommand(originalName, name, spec);
        if (originalName !== name) renameInSequence(originalName, name);
      } else {
        await Api.createCommand(name, spec);
      }
      closeBuilder();
      await loadCommands();
      state.selected = name;
      renderCommandList();
      renderDetail();
      toast(`Saved "${name}".`, "info");
    } catch (e) {
      qs("#modal-error").textContent = e.message;
    }
  });

  // ===========================================================================
  // Init
  // ===========================================================================

  async function initApp(status) {
    renderStatus(status);
    // Silent: the boot sequence + status pill already explain an offline
    // CLI on first load, so a third toast on top would just be noise.
    await loadCommands({ silent: !status.online });
    if (status.online) await loadMode();
    // Load every saved conversation into the sidebar first (they live on
    // disk under ~/.jarvis/conversations and are shared across every
    // browser/session that hits this server — this was previously never
    // called, so old conversations looked "lost" on a fresh page load even
    // though they were still on disk). Then start today's fresh thread on
    // top of that list, per the "opening the page always starts a new
    // conversation" design.
    if (status.online) {
      await refreshConvoList();
      await startNewConversation();
    }
    connectWs();
  }

  tickClock();
  setInterval(tickClock, 1000);
  runBoot();
})();