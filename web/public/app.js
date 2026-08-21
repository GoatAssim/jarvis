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

  let toastTimer = null;
  function toast(message, kind = "error") {
    const t = qs("#toast");
    t.textContent = message;
    t.className = "toast" + (kind === "info" ? " is-info" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
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
    running: false,        // true while a command/sequence is in flight
    asking: false,         // true while an Ask Jarvis reply is streaming
    ws: null,
    wsBackoff: 1000,
    editingOriginalName: null, // set when the builder modal is editing an existing command

    // Ask Jarvis \u2014 state for the turn currently streaming in, if any.
    askPendingBubble: null,  // the DOM node for Jarvis's in-progress reply bubble
    askReplyLines: [],       // accumulated (post prefix-strip) lines of that reply
    askPrefixStripped: false,// whether we've already tried stripping "Name: " off line 1
    askQuotes: [],           // highlighted excerpts attached to the next ask
    lastTaskLabel: "",       // user request / command name for away notifications
    muted: localStorage.getItem("jarvis-muted") === "1",
    askSpokenLen: 0,
    cmdQuery: "",
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
      throw new Error(message);
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
    getRaw: () => api("GET", "/api/raw"),
    putRaw: (text) => api("PUT", "/api/raw", { text }),
    clearAiHistory: () => api("POST", "/api/ai/clear"),
    getAiRaw: () => api("GET", "/api/ai/raw"),
    putAiRaw: (text) => api("PUT", "/api/ai/raw", { text }),
    getPlayniteRaw: () => api("GET", "/api/playnite/raw"),
    putPlayniteRaw: (text) => api("PUT", "/api/playnite/raw", { text }),
    getSpotifyRaw: () => api("GET", "/api/spotify/raw"),
    putSpotifyRaw: (text) => api("PUT", "/api/spotify/raw", { text }),
    getMemoryRaw: () => api("GET", "/api/memory/raw"),
    putMemoryRaw: (text) => api("PUT", "/api/memory/raw", { text }),
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
  // Command list
  // ===========================================================================

  function stepCount(spec) {
    return Array.isArray(spec.run) ? spec.run.length : 1;
  }
  function hasConditions(spec) {
    if (!Array.isArray(spec.run)) return false;
    return spec.run.some((s) => typeof s === "object" && (s.if != null || s.unless != null));
  }

  function renderCommandList() {
    const list = qs("#cmd-list");
    const q = (state.cmdQuery || "").trim().toLowerCase();
    const names = Object.keys(state.commands).filter((name) => {
      if (!q) return true;
      const spec = state.commands[name] || {};
      const desc = (spec.description || "").toLowerCase();
      return name.toLowerCase().includes(q) || desc.includes(q);
    });
    if (Object.keys(state.commands).length === 0) {
      list.innerHTML = "";
      list.appendChild(el("div", { class: "empty-hint" }, "No commands yet. Build your first one."));
      return;
    }
    if (names.length === 0) {
      list.innerHTML = "";
      list.appendChild(el("div", { class: "empty-hint" }, "No commands match that search."));
      return;
    }
    list.innerHTML = "";
    for (const name of names) {
      const spec = state.commands[name];
      const card = el("div", {
        class: "cmd-card" + (name === state.selected ? " is-active" : ""),
        onclick: () => selectCommand(name),
      }, [
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

  qs("#cmd-search").addEventListener("input", () => {
    state.cmdQuery = qs("#cmd-search").value;
    renderCommandList();
  });

  function applyCommandsToUi() {
    renderCommandList();
    if (state.selected && !state.commands[state.selected]) {
      state.selected = null;
    }
    renderDetail();
    const settingsBackdrop = qs("#settings-backdrop");
    const commandsPane = qs('#settings-backdrop .settings-pane[data-settings-pane="commands"]');
    if (settingsBackdrop && !settingsBackdrop.hidden && commandsPane?.classList.contains("is-active")) {
      qs("#settings-json-commands").value = JSON.stringify({ commands: state.commands }, null, 2) + "\n";
    }
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
    wsSend({ type: "cancel", kind: "run" });
  });

  function syncBusyUi() {
    qs("#btn-execute").disabled = state.running;
    qs("#btn-seq-run").disabled = state.running;
    qs("#btn-abort").hidden = !state.running;
    qs("#btn-ask-send").disabled = state.asking;
    qs("#btn-ask-stop").hidden = !state.asking;
    syncMuteButton();
  }

  function setRunning(running) {
    state.running = running;
    syncBusyUi();
  }

  function setAsking(asking) {
    state.asking = asking;
    syncBusyUi();
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    state.ws = ws;

    ws.addEventListener("open", () => { state.wsBackoff = 1000; });
    ws.addEventListener("close", () => {
      state.ws = null;
      if (state.running) consoleAppend("\u26a0 uplink to server lost mid-run", "exit-bad");
      if (state.asking) setAskStatus("uplink lost", "error");
      setRunning(false);
      setAsking(false);
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
        consoleAppend(`\u26a0 ${msg.message}`, "exit-bad");
        toast(msg.message);
        notifyTaskDone(msg.message, true);
        break;

      case "ask-start":
        setAsking(true);
        state.askReplyLines = [];
        state.askPrefixStripped = false;
        state.askSpokenLen = 0;
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
      case "ask-exit": {
        setAsking(false);
        const raw = state.askReplyLines.length ? state.askReplyLines.join("\n") : "";
        finalizeAskBubble();
        speakJarvisRemainder(raw);
        askPromptEnd(msg.code, msg.signal);
        setAskStatus(msg.code === 0 ? "online" : "last attempt failed", msg.code === 0 ? "" : "error");
        notifyTaskDone(summarizeText(raw), msg.code !== 0);
        break;
      }
      case "ask-error":
        setAsking(false);
        if (state.askPendingBubble) {
          finalizeAskBubble(msg.message);
        } else {
          toast(msg.message);
        }
        askPromptEnd(1, null, msg.message);
        setAskStatus("error", "error");
        notifyTaskDone(msg.message, true);
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

  const MUTE_RE = /\b(mute(?:\s+yourself)?|be quiet|silence(?:\s+yourself)?|stop (?:talking|speaking)|hush|voice off)\b/i;
  const UNMUTE_RE = /\b(unmute|speak again|you (?:may|can) talk|voice on|unmute yourself)\b/i;

  function speechText(raw) {
    return String(raw || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/!\[[^\]]*]\([^)]+\)/g, " ")
      .replace(/\[([^\]]+)]\([^)]+\)/g, "$1")
      .replace(/^#{1,6}\s+/gm, "")
      .replace(/[*_~]+/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function pickJarvisVoice() {
    const voices = window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
    const english = voices.filter((v) => {
      const n = `${v.name} ${v.lang}`.toLowerCase();
      if (!/^en/i.test(v.lang || "")) return false;
      if (/fr-|french|fran[cç]ais/.test(n)) return false;
      return true;
    });
    const score = (v) => {
      const n = `${v.name} ${v.lang}`.toLowerCase();
      if (/female|woman|hazel|zira|samantha|susan|fable|aria|jenny|salli/.test(n)) return -100;
      let s = 0;
      if (/en-gb|en_gb/.test(n)) s += 12;
      if (/google uk english male|daniel|george/.test(n)) s += 16;
      if (/male|david|mark|brian|ryan/.test(n)) s += 8;
      if (/rishi|en-in|india/.test(n)) s -= 12;
      if (/irish|scottish|australian|en-au/.test(n)) s -= 6;
      if (v.localService) s += 1;
      return s;
    };
    const ranked = english.slice().sort((a, b) => score(b) - score(a));
    const best = ranked[0];
    if (!best || score(best) <= 0) {
      return english.find((v) => /male/i.test(v.name)) || null;
    }
    return best;
  }

  function speakJarvis(text) {
    if (state.muted || !text || !window.speechSynthesis) return;
    const u = new SpeechSynthesisUtterance(text);
    const voice = pickJarvisVoice();
    if (voice) u.voice = voice;
    u.lang = "en-GB";
    u.rate = 0.94;
    u.pitch = 0.72;
    u.volume = 1;
    window.speechSynthesis.speak(u);
  }

  function speakJarvisCatchup(full) {
    if (state.muted) return;
    const plain = speechText(full);
    if (plain.length <= state.askSpokenLen) return;
    const unread = plain.slice(state.askSpokenLen);
    const parts = unread.split(/(?<=[.!?])\s+/);
    if (parts.length < 2) return;
    const ready = parts.slice(0, -1).join(" ").trim();
    if (!ready) return;
    speakJarvis(ready);
    state.askSpokenLen += ready.length;
    while (plain[state.askSpokenLen] === " ") state.askSpokenLen += 1;
  }

  function speakJarvisRemainder(full) {
    if (state.muted) return;
    const plain = speechText(full);
    const rest = plain.slice(state.askSpokenLen).trim();
    if (rest) speakJarvis(rest);
    state.askSpokenLen = plain.length;
  }

  function setMuted(muted) {
    state.muted = !!muted;
    localStorage.setItem("jarvis-muted", state.muted ? "1" : "0");
    if (state.muted && window.speechSynthesis) window.speechSynthesis.cancel();
    syncMuteButton();
  }

  function syncMuteButton() {
    const btn = qs("#btn-ask-mute");
    if (!btn) return;
    btn.classList.toggle("is-muted", state.muted);
    btn.textContent = state.muted ? "Unmute" : "Mute";
    btn.title = state.muted ? "Voice muted" : "Mute Jarvis";
  }

  function applyVoiceCommand(text) {
    if (MUTE_RE.test(text)) setMuted(true);
    else if (UNMUTE_RE.test(text)) {
      setMuted(false);
      speakJarvis("Online, sir.");
    }
  }

  function bindBubbleHoldCopy(msg) {
    let timer = null;
    const start = (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      timer = setTimeout(() => {
        timer = null;
        copyAskRaw(msg);
      }, 480);
    };
    const clear = () => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };
    msg.addEventListener("pointerdown", start);
    msg.addEventListener("pointerup", clear);
    msg.addEventListener("pointerleave", clear);
    msg.addEventListener("pointercancel", clear);
  }

  // ===========================================================================
  // Ask Jarvis
  // ===========================================================================

  const askOverlay = qs("#ask-overlay");
  const askThread = qs("#ask-thread");

  function setAskStatus(text, kind) {
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
    if (state.asking) {
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
    wsSend({ type: "ask", text, redo: true });
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
    bindBubbleHoldCopy(msg);
    askThread.appendChild(msg);
    askThreadScrollToEnd();
    return msg;
  }

  function addJarvisBubblePending() {
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
    }
    let cls = "sys";
    if (line.includes("\u2717")) cls = "fail";
    else if (line.includes("$")) cls = "tool";
    askPromptLine(line, cls);
  }

  function showAskScreenshot(filename) {
    if (!/^ss_[A-Za-z0-9_.-]+\.png$/.test(filename)) return;
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
    if (state.askPendingBubble) askThread.insertBefore(msg, state.askPendingBubble);
    else askThread.appendChild(msg);
    askThreadScrollToEnd();
  }

  function stripAnsi(s) {
    return String(s || "").replace(/\u001b\[[0-9;]*[A-Za-z]/g, "").replace(/\u001b\][^\u0007]*\u0007/g, "");
  }

  function askPromptTerm() {
    return qs("#ask-prompt-term");
  }

  function setAskPromptState(text, live) {
    const node = qs("#ask-prompt-state");
    node.textContent = text;
    node.classList.toggle("is-live", !!live);
  }

  function askPromptClearIdle() {
    const idle = qs(".ask-prompt__idle", askPromptTerm());
    if (idle) idle.remove();
  }

  function askPromptCursor(show) {
    const term = askPromptTerm();
    let cur = qs(".ask-prompt__cursor", term);
    if (!show) {
      if (cur) cur.remove();
      return;
    }
    if (!cur) cur = el("span", { class: "ask-prompt__cursor" });
    term.appendChild(cur);
  }

  function askPromptLine(text, cls) {
    const term = askPromptTerm();
    askPromptClearIdle();
    const cur = qs(".ask-prompt__cursor", term);
    const line = el("div", { class: `ask-prompt-line ask-prompt-line--${cls}` }, text);
    if (cur) term.insertBefore(line, cur);
    else term.appendChild(line);
    term.scrollTop = term.scrollHeight;
  }

  function askPromptBegin() {
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

  // Jarvis's own CLI output is plain text like "J.A.R.V.I.S: <reply>" (see
  // cli.py: handle_ai_prompt) \u2014 the ask panel already shows a "Jarvis" role
  // label above the bubble, so on the first line of a reply we lift that
  // leading "Name: " off (and adopt Name as the role label, so a renamed
  // persona in ai_config.json is reflected automatically) instead of
  // showing it twice.
  function appendAskReplyLine(line) {
    if (!state.askPendingBubble) return;
    if (!state.askPrefixStripped) {
      state.askPrefixStripped = true;
      const m = /^([^\n:]{1,40}):\s(.*)$/.exec(line);
      if (m) {
        qs(".ask-msg__role", state.askPendingBubble).textContent = m[1];
        line = m[2];
      }
    }
    state.askReplyLines.push(line);
    const bubble = qs(".ask-msg__bubble", state.askPendingBubble);
    bubble.innerHTML = renderMarkdown(state.askReplyLines.join("\n"));
    speakJarvisCatchup(state.askReplyLines.join("\n"));
    askThreadScrollToEnd();
  }

  function finalizeAskBubble(overrideMessage) {
    const bubble = state.askPendingBubble;
    if (bubble) {
      bubble.classList.remove("is-pending");
      const raw = state.askReplyLines.length
        ? state.askReplyLines.join("\n")
        : (overrideMessage || "(no response)");
      bubble.dataset.raw = raw;
      if (state.askReplyLines.length === 0) {
        bubble.classList.add("is-error");
        qs(".ask-msg__bubble", bubble).textContent = raw;
      } else {
        qs(".ask-msg__bubble", bubble).innerHTML = renderMarkdown(raw);
      }
      addAskMsgActions(bubble);
      bindBubbleHoldCopy(bubble);
    }
    state.askPendingBubble = null;
    state.askReplyLines = [];
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
  qs("#btn-ask-mute").addEventListener("click", () => setMuted(!state.muted));
  syncMuteButton();
  if (window.speechSynthesis) {
    window.speechSynthesis.onvoiceschanged = () => {};
    window.speechSynthesis.getVoices();
  }
  askOverlay.addEventListener("click", (e) => { if (e.target === askOverlay) closeAsk(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !askOverlay.hidden) closeAsk();
  });

  qs("#btn-ask-stop").addEventListener("click", () => wsSend({ type: "cancel", kind: "ask" }));

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

  qs("#ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.asking) return;
    const input = qs("#ask-input");
    const text = input.value.trim();
    const quotes = state.askQuotes.slice();
    if (!text && quotes.length === 0) return;
    applyVoiceCommand(text);
    input.value = "";
    state.askQuotes = [];
    renderQuoteBar();
    hideSelPop();
    addUserBubble(text, quotes);
    ensureNotifPermission();
    state.lastTaskLabel = text || (quotes[0] || "that");
    wsSend({
      type: "ask",
      text,
      quote: quotes.length ? quotes.join("\n---\n") : undefined,
    });
  });

  qs("#btn-ask-clear").addEventListener("click", async () => {
    try {
      await Api.clearAiHistory();
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
    toast("Conversation cleared.", "info");
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

    buildVarsEditor(spec.vars || {});
    buildStepsEditor(normalizeSteps(spec.run));
    refreshVarSync();
    syncBuilderToRaw();

    backdrop.hidden = false;
    qs("#f-name").focus();
  }

  function closeBuilder() { backdrop.hidden = true; }

  qs("#btn-new-command").addEventListener("click", () => openBuilder("new"));
  qs("#modal-close").addEventListener("click", closeBuilder);
  qs("#btn-cancel-modal").addEventListener("click", closeBuilder);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeBuilder(); });

  // ===========================================================================
  // Settings modal — commands.json, ai_config.json, playnite.json in one place.
  // ===========================================================================

  const settingsBackdrop = qs("#settings-backdrop");
  let settingsActiveTab = "commands";

  const SETTINGS = {
    commands: {
      get: () => Api.getRaw(),
      put: (text) => Api.putRaw(text),
      pathId: "settings-path-commands",
      jsonId: "settings-json-commands",
      errorId: "settings-error-commands",
      saveToast: "commands.json saved.",
      afterSave: () => loadCommands(),
    },
    ai: {
      get: () => Api.getAiRaw(),
      put: (text) => Api.putAiRaw(text),
      pathId: "settings-path-ai",
      jsonId: "settings-json-ai",
      errorId: "settings-error-ai",
      saveToast: "ai_config.json saved.",
    },
    playnite: {
      get: () => Api.getPlayniteRaw(),
      put: (text) => Api.putPlayniteRaw(text),
      pathId: "settings-path-playnite",
      jsonId: "settings-json-playnite",
      errorId: "settings-error-playnite",
      saveToast: "playnite.json saved.",
    },
    spotify: {
      get: () => Api.getSpotifyRaw(),
      put: (text) => Api.putSpotifyRaw(text),
      pathId: "settings-path-spotify",
      jsonId: "settings-json-spotify",
      errorId: "settings-error-spotify",
      saveToast: "spotify.json saved.",
    },
    memory: {
      get: () => Api.getMemoryRaw(),
      put: (text) => Api.putMemoryRaw(text),
      pathId: "settings-path-memory",
      jsonId: "settings-json-memory",
      errorId: "settings-error-memory",
      saveToast: "memory.json saved.",
    },
  };

  function clearSettingsErrors() {
    qs("#settings-error-global").textContent = "";
    for (const tab of Object.keys(SETTINGS)) {
      qs(`#${SETTINGS[tab].errorId}`).textContent = "";
    }
  }

  function setSettingsTab(tab) {
    settingsActiveTab = tab;
    qsa("#settings-backdrop .settings-tab").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.settingsTab === tab);
    });
    qsa("#settings-backdrop .settings-pane").forEach((p) => {
      p.classList.toggle("is-active", p.dataset.settingsPane === tab);
    });
    const jsonEl = qs(`#${SETTINGS[tab].jsonId}`);
    if (jsonEl) jsonEl.focus();
  }

  async function loadSettingsTab(tab, { force = false } = {}) {
    const cfg = SETTINGS[tab];
    const jsonEl = qs(`#${cfg.jsonId}`);
    if (!force && jsonEl.dataset.loaded === "1") return;
    const data = await cfg.get();
    qs(`#${cfg.pathId}`).textContent = data.path;
    jsonEl.value = data.text;
    jsonEl.dataset.loaded = "1";
  }

  async function openSettings(initialTab = "commands") {
    clearSettingsErrors();
    setSettingsTab(initialTab);
    settingsBackdrop.hidden = false;
    try {
      await loadSettingsTab(initialTab, { force: true });
    } catch (e) {
      toast(e.message);
    }
  }

  function closeSettings() {
    settingsBackdrop.hidden = true;
    for (const tab of Object.keys(SETTINGS)) {
      qs(`#${SETTINGS[tab].jsonId}`).dataset.loaded = "";
    }
  }

  qs("#btn-settings").addEventListener("click", () => openSettings("commands"));
  qs("#settings-close").addEventListener("click", closeSettings);
  qs("#btn-settings-cancel").addEventListener("click", closeSettings);
  settingsBackdrop.addEventListener("click", (e) => { if (e.target === settingsBackdrop) closeSettings(); });

  qsa("#settings-backdrop .settings-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.settingsTab;
      setSettingsTab(tab);
      loadSettingsTab(tab).catch((e) => {
        qs(`#${SETTINGS[tab].errorId}`).textContent = e.message;
      });
    });
  });

  qs("#btn-settings-reload").addEventListener("click", async () => {
    clearSettingsErrors();
    const tab = settingsActiveTab;
    try {
      await loadSettingsTab(tab, { force: true });
      toast("Reloaded from disk.", "info");
    } catch (e) {
      qs(`#${SETTINGS[tab].errorId}`).textContent = e.message;
    }
  });

  qs("#btn-settings-save").addEventListener("click", async () => {
    clearSettingsErrors();
    const tab = settingsActiveTab;
    const cfg = SETTINGS[tab];
    const text = qs(`#${cfg.jsonId}`).value;
    try {
      JSON.parse(text);
    } catch (e) {
      qs(`#${cfg.errorId}`).textContent = `Invalid JSON: ${e.message}`;
      return;
    }
    try {
      await cfg.put(text);
      if (cfg.afterSave) await cfg.afterSave();
      closeSettings();
      toast(cfg.saveToast, "info");
    } catch (e) {
      qs(`#${cfg.errorId}`).textContent = e.message;
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
    return { description, run, vars };
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
    connectWs();
  }

  tickClock();
  setInterval(tickClock, 1000);
  runBoot();
})();
