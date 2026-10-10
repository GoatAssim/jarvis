/* ============================================================================
 * ask-embed.js -- an Ask Jarvis panel you can put inside another screen
 * (master plan L.53; first host: the Tool Manager's side panel).
 *
 * It is NOT a second chat client. It speaks the SAME websocket protocol the main
 * Ask panel does ("ask" -> ask-start / ask-stream / ask-stdout / ask-confirm-request
 * / ask-usage / ask-exit / ask-error, see web/server.js), so the host gets a normal
 * Jarvis conversation: the same providers, the same tools, a saved conversation, a
 * confirmation card for anything that asks first. What a host adds is passed in:
 *
 *   JarvisAskEmbed.create({
 *     size:            "auto" | "small" | "normal"   // auto = by the panel's own width
 *     placeholder, emptyHint,                         // texts
 *     conversationId:  "<id>" | "",                   // resume a saved one
 *     origin, originDetail: () => "",                 // stamped when it is created
 *     getAskExtras:    () => ({ toolMaker: {...} }),  // merged into every "ask" message
 *     beforeSend:      () => "",                      // a non-empty string vetoes the send and is shown
 *     fold:            (text, pending) => ({ text, chip }),   // hide code from the bubble
 *     onConversation:  (id) => {},                    // a conversation was created / reset
 *     onTurnStart:     () => {},
 *     onText:          (roundText) => {},             // the model's live text (this round)
 *     onTurnEnd:       ({ ok, stopped, text, partial, error, tools }) => {},
 *   }) -> { el, send, stop, busy, newConversation, loadConversation,
 *           conversationId, setSize, focus, destroy }
 *
 * One instance = one conversation = one DOM subtree. A host that has several
 * (one per tab) keeps them all alive and attaches the active one's `el`; a turn
 * keeps running while its panel is not on screen, because each ask owns its own
 * websocket (closing it is also what stops the child on the server).
 *
 * The pure helpers at the bottom (splitReply, applyStream, sizeFor, ...) carry
 * no DOM and are exported for tests/verify_ask_embed.js.
 * ========================================================================= */
(function (root) {
  "use strict";

  const SMALL_BELOW = 400;               // px: a panel narrower than this wears the small layout
  const STOP_GRACE_MS = 2500;            // after Stop: how long to wait for the child's own exit
  const RENDER_EVERY_MS = 70;            // the streaming bubble repaints at most this often
  const MAX_CONFIRM_ARGS = 700;

  // ---- pure helpers --------------------------------------------------------

  function stripAnsi(s) {
    return String(s == null ? "" : s)
      .replace(/\u001b\[[0-9;]*[A-Za-z]/g, "")
      .replace(/\u001b\][^\u0007]*\u0007/g, "");
  }

  // The CLI prints "<Name>: <reply>" and may print console scaffolding before it
  // (and "[called x ...]" echoes inside it). Same split app.js's splitConsoleDump
  // makes for the main panel: what precedes the first "Name: " line is a console
  // dump, the rest is the reply with the prefix removed.
  const NAME_PREFIX_LINE = /^([^\n:]{1,40}):\s(.*)$/;
  const INLINE_TOOL_TRACE_LINE = /^\[(called\s|tool result\b)/i;

  function splitReply(lines) {
    lines = (lines || []).map((l) => stripAnsi(l));
    let splitAt = -1;
    let name = null;
    let first = null;
    for (let i = 0; i < lines.length; i++) {
      const m = NAME_PREFIX_LINE.exec(lines[i]);
      if (m) { splitAt = i; name = m[1]; first = m[2]; break; }
    }
    const dump = splitAt === -1 ? [] : lines.slice(0, splitAt);
    const candidate = splitAt === -1 ? lines.slice() : [first].concat(lines.slice(splitAt + 1));
    const reply = [];
    for (const line of candidate) {
      if (INLINE_TOOL_TRACE_LINE.test(String(line).trim())) dump.push(String(line).trim());
      else reply.push(line);
    }
    return { name, dump, reply, text: reply.join("\n").trim() };
  }

  function sizeFor(widthPx) {
    return Number(widthPx) > 0 && Number(widthPx) < SMALL_BELOW ? "small" : "normal";
  }

  // The live stream: {k:"text", d} appends; {k:"reset"} drops this round's text (a
  // mid-stream fallback restarted it); {k:"tool", name} notes a tool; {k:"round_end",
  // finish:"tool"} closes a round that ended in a tool call, so the NEXT round's text
  // starts clean. {k:"thinking"} is not shown here. Returns what changed.
  function newStream() { return { text: "", tools: [], round: 1 }; }

  function applyStream(st, ev) {
    if (!st || !ev || typeof ev !== "object") return "";
    switch (ev.k) {
      case "text":
        st.text += typeof ev.d === "string" ? ev.d : "";
        return "text";
      case "reset":
        st.text = "";
        return "text";
      case "tool":
        if (typeof ev.name === "string" && ev.name) st.tools.push(ev.name);
        return "tool";
      case "round_end":
        if (ev.finish === "tool") { st.text = ""; st.round += 1; return "round"; }
        return "";
      default:
        return "";
    }
  }

  // Default fold: nothing hidden.
  function foldNone(text) { return { text: String(text || ""), chip: "" }; }

  // ---- DOM -----------------------------------------------------------------

  function h(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v == null || v === false) continue;
        if (k === "class") n.className = v;
        else if (k.slice(0, 2) === "on" && typeof v === "function") n.addEventListener(k.slice(2), v);
        else if (v === true) n.setAttribute(k, "");
        else n.setAttribute(k, String(v));
      }
    }
    (Array.isArray(kids) ? kids : kids == null ? [] : [kids]).forEach((c) => {
      if (c == null || c === false) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }

  function wsUrl() {
    const proto = root.location && root.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + root.location.host + "/ws";
  }

  function renderInto(node, text, streaming) {
    const md = root.JarvisMarkdown;
    if (md && typeof md.renderInto === "function") {
      node.classList.add("jv-md");
      md.renderInto(node, text, streaming ? { streaming: true } : undefined);
    } else {
      node.textContent = text;
    }
  }

  function create(opts) {
    opts = opts || {};
    const fold = typeof opts.fold === "function" ? opts.fold : foldNone;
    let convId = opts.conversationId || "";
    let sizeMode = opts.size || "auto";
    let busy = false;
    let sock = null;
    let turn = null;                    // the running ask's state
    let stopTimer = 0;
    let observer = null;
    let destroyed = false;

    const thread = h("div", { class: "jae__thread", role: "log", "aria-live": "polite" });
    const status = h("div", { class: "jae__status", "aria-live": "polite" });
    const input = h("textarea", {
      class: "jae__input", rows: "2",
      placeholder: opts.placeholder || "Ask Jarvis\u2026",
      "aria-label": opts.placeholder || "Ask Jarvis",
    });
    const sendBtn = h("button", { type: "button", class: "jae__btn jae__btn--send", onclick: () => submit() }, "Send");
    const stopBtn = h("button", { type: "button", class: "jae__btn jae__btn--stop", hidden: true, onclick: () => stop() }, "Stop");
    const hint = h("div", { class: "jae__hint" }, "Enter sends \u00b7 Shift+Enter for a new line");
    const el = h("div", { class: "jae jae--normal" }, [
      thread,
      status,
      h("div", { class: "jae__compose" }, [input, h("div", { class: "jae__btns" }, [sendBtn, stopBtn])]),
      hint,
    ]);

    function showEmpty() {
      if (thread.querySelector(".jae__msg")) return;
      if (!thread.querySelector(".jae__empty")) {
        thread.appendChild(h("div", { class: "jae__empty" }, opts.emptyHint || "Ask Jarvis anything about this."));
      }
    }
    function clearEmpty() {
      const e = thread.querySelector(".jae__empty");
      if (e) e.remove();
    }
    function scrollEnd(force) {
      const near = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
      if (force || near) thread.scrollTop = thread.scrollHeight;
    }
    function setStatus(text, kind) {
      status.textContent = text || "";
      status.className = "jae__status" + (kind ? " is-" + kind : "");
    }
    function setBusy(on) {
      busy = !!on;
      sendBtn.disabled = busy;
      stopBtn.hidden = !busy;
      input.disabled = busy;
      el.classList.toggle("is-busy", busy);
    }

    function addMsg(role, text, extraClass) {
      clearEmpty();
      const body = h("div", { class: "jae__body" });
      const node = h("div", { class: "jae__msg jae__msg--" + role + (extraClass ? " " + extraClass : "") }, [body]);
      if (text) body.textContent = text;
      thread.appendChild(node);
      scrollEnd(true);
      return { node, body };
    }

    // An assistant bubble whose text can be repainted (streaming) and finished.
    function paintAssistant(msg, text, pending) {
      const f = fold(text, pending);
      msg.body.textContent = "";
      if (f.text && f.text.trim()) renderInto(msg.body, f.text, pending);
      let chip = msg.node.querySelector(".jae__chip");
      if (f.chip) {
        if (!chip) { chip = h("div", { class: "jae__chip" }); msg.node.appendChild(chip); }
        chip.textContent = f.chip;
      } else if (chip) {
        chip.remove();
      }
      msg.node.classList.toggle("is-pending", !!pending);
      scrollEnd(false);
    }

    // ---- conversation ------------------------------------------------------

    async function ensureConversation() {
      if (convId) return convId;
      const body = {};
      if (opts.origin) {
        body.origin = opts.origin;
        const d = typeof opts.originDetail === "function" ? opts.originDetail() : opts.originDetail;
        if (d) body.originDetail = String(d);
      }
      const res = await root.fetch("/api/conversations", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.id) throw new Error(data.error || "Couldn't start a conversation.");
      convId = data.id;
      if (opts.onConversation) opts.onConversation(convId);
      return convId;
    }

    async function loadConversation(id) {
      if (!id || busy) return false;
      let data;
      try {
        const res = await root.fetch("/api/conversations/" + encodeURIComponent(id));
        if (!res.ok) {
          // The conversation is gone (deleted from Logs): forget it, so the next message
          // starts a new one instead of asking into an id that no longer exists.
          if (res.status === 404 && id === convId) { convId = ""; if (opts.onConversation) opts.onConversation(""); }
          return false;
        }
        data = await res.json();
      } catch (_e) { return false; }
      const list = data && Array.isArray(data.exchanges) ? data.exchanges : null;
      if (!list) return false;
      convId = id;
      thread.textContent = "";
      for (const ex of list) {
        if (ex && typeof ex.user === "string" && ex.user) addMsg("user", ex.user);
        if (ex && typeof ex.jarvis === "string" && ex.jarvis) {
          const m = addMsg("ai");
          paintAssistant(m, ex.jarvis, false);
        }
      }
      showEmpty();
      scrollEnd(true);
      return true;
    }

    function newConversation() {
      if (busy) return false;
      convId = "";
      thread.textContent = "";
      setStatus("");
      showEmpty();
      if (opts.onConversation) opts.onConversation("");
      return true;
    }

    // ---- one ask -----------------------------------------------------------

    function confirmCard(msg) {
      let args = "";
      try { args = JSON.stringify(msg.arguments || {}, null, 2); } catch (_e) { args = ""; }
      if (args.length > MAX_CONFIRM_ARGS) args = args.slice(0, MAX_CONFIRM_ARGS) + "\u2026";
      const card = h("div", { class: "jae__confirm" });
      const done = (approved) => {
        if (sock && sock.readyState === 1) sock.send(JSON.stringify({ type: "ask-confirm-response", approved }));
        card.classList.add("is-done");
        card.textContent = (approved ? "Allowed: " : "Declined: ") + String(msg.tool || "that");
        setStatus(approved ? "working\u2026" : "declined \u2014 carrying on\u2026", "busy");
      };
      card.appendChild(h("div", { class: "jae__confirm-title" }, "Jarvis wants to use " + String(msg.tool || "a tool")));
      if (msg.risk_note) card.appendChild(h("div", { class: "jae__confirm-note" }, String(msg.risk_note)));
      if (args && args !== "{}") card.appendChild(h("pre", { class: "jae__confirm-args" }, args));
      card.appendChild(h("div", { class: "jae__confirm-btns" }, [
        h("button", { type: "button", class: "jae__btn jae__btn--send", onclick: () => done(true) }, "Allow"),
        h("button", { type: "button", class: "jae__btn", onclick: () => done(false) }, "Decline"),
      ]));
      clearEmpty();
      thread.appendChild(h("div", { class: "jae__msg jae__msg--confirm" }, [card]));
      scrollEnd(true);
    }

    function finish(info) {
      if (!turn) return;
      const t = turn;
      turn = null;
      clearTimeout(stopTimer);
      clearTimeout(t.renderTimer);
      if (sock) { try { sock.close(); } catch (_e) { /* already closed */ } sock = null; }
      setBusy(false);
      const text = info.text || "";
      if (info.error) {
        t.msg.node.remove();
        addMsg("err", info.error, "jae__msg--error");
        setStatus("");
      } else if (info.stopped) {
        paintAssistant(t.msg, text || t.stream.text, false);
        t.msg.node.classList.add("is-stopped");
        if (!(text || t.stream.text).trim()) t.msg.node.remove();
        setStatus("stopped");
      } else {
        paintAssistant(t.msg, text, false);
        setStatus("");
      }
      if (t.stream.tools.length) {
        const uniq = Array.from(new Set(t.stream.tools));
        const meta = h("div", { class: "jae__meta" }, "used: " + uniq.join(", "));
        if (t.msg.node.isConnected) t.msg.node.appendChild(meta);
      }
      if (opts.onTurnEnd) {
        opts.onTurnEnd({
          ok: !info.error && !info.stopped, stopped: !!info.stopped, text,
          partial: !!info.stopped || !!info.error, error: info.error || "", tools: t.stream.tools.slice(),
        });
      }
      scrollEnd(true);
    }

    function paintSoon(t) {
      if (t.renderTimer) return;
      t.renderTimer = setTimeout(() => {
        t.renderTimer = 0;
        if (turn === t) paintAssistant(t.msg, t.stream.text, true);
      }, RENDER_EVERY_MS);
    }

    async function submit() {
      if (busy || destroyed) return false;
      const text = input.value.trim();
      if (!text) return false;
      return send(text);
    }

    async function send(text) {
      text = String(text || "").trim();
      if (!text || busy || destroyed) return false;
      // A host can say "not now" (e.g. a Save is still running): shown, nothing sent, text kept.
      const veto = opts.beforeSend ? opts.beforeSend() : "";
      if (veto) { setStatus(String(veto), "busy"); return false; }
      setBusy(true);
      setStatus("starting\u2026", "busy");
      try {
        await ensureConversation();
      } catch (e) {
        setBusy(false);
        addMsg("err", e.message || "Couldn't start a conversation.", "jae__msg--error");
        setStatus("");
        return false;
      }
      input.value = "";
      addMsg("user", text);
      const msg = addMsg("ai");
      msg.node.classList.add("is-pending");
      turn = { msg, stream: newStream(), lines: [], errLines: [], stopped: false, renderTimer: 0 };
      if (opts.onTurnStart) opts.onTurnStart();

      let extras = {};
      try { extras = (opts.getAskExtras && opts.getAskExtras()) || {}; } catch (_e) { extras = {}; }
      const t = turn;
      let ws;
      try { ws = new root.WebSocket(wsUrl()); } catch (e) {
        finish({ error: "Couldn't reach Jarvis: " + (e && e.message ? e.message : "no connection") });
        return false;
      }
      sock = ws;
      ws.addEventListener("open", () => {
        ws.send(JSON.stringify(Object.assign({ type: "ask", text, conversationId: convId }, extras)));
      });
      ws.addEventListener("message", (e) => {
        if (turn !== t) return;
        let m;
        try { m = JSON.parse(e.data); } catch (_e) { return; }
        switch (m.type) {
          case "ask-start":
            setStatus("thinking\u2026", "busy");
            break;
          case "ask-stream": {
            const what = applyStream(t.stream, m.ev);
            if (what === "text") {
              if (opts.onText) opts.onText(t.stream.text);
              if (t.stream.text.trim()) setStatus("writing\u2026", "busy");
              paintSoon(t);
            } else if (what === "tool") {
              const name = t.stream.tools[t.stream.tools.length - 1];
              setStatus("using " + name + "\u2026", "busy");
            } else if (what === "round") {
              if (opts.onText) opts.onText("");
              paintAssistant(t.msg, "", true);
            }
            break;
          }
          case "ask-stdout":
            t.lines.push(String(m.line == null ? "" : m.line));
            break;
          case "ask-stderr":
            if (typeof m.line === "string" && m.line.trim()) {
              t.errLines.push(stripAnsi(m.line).trim());
              if (t.errLines.length > 12) t.errLines.shift();
            }
            break;
          case "ask-confirm-request":
            setStatus("waiting for your OK\u2026", "busy");
            confirmCard(m);
            break;
          case "ask-error":
            finish({ error: String(m.message || "Jarvis couldn't answer."), stopped: t.stopped });
            break;
          case "ask-exit": {
            const parsed = splitReply(t.lines);
            if (t.stopped) return finish({ stopped: true, text: parsed.text });
            if (m.code === 0) return finish({ text: parsed.text });
            const why = t.errLines.length ? t.errLines[t.errLines.length - 1] : "";
            return finish({ error: "Jarvis couldn't answer" + (why ? ": " + why : " (the last attempt failed).") });
          }
          default:
            break;
        }
      });
      ws.addEventListener("error", () => {
        if (turn === t) finish({ error: "Lost the connection to Jarvis.", stopped: t.stopped });
      });
      ws.addEventListener("close", () => {
        if (turn === t) finish({ error: t.stopped ? "" : "Lost the connection to Jarvis.", stopped: t.stopped });
      });
      return true;
    }

    function stop() {
      if (!busy || !turn || turn.stopped) return;
      turn.stopped = true;
      setStatus("stopping\u2026", "busy");
      if (sock && sock.readyState === 1) {
        try { sock.send(JSON.stringify({ type: "cancel" })); } catch (_e) { /* closing */ }
      }
      // If the child does not report its exit, closing the socket makes the server kill it.
      const t = turn;
      stopTimer = setTimeout(() => {
        if (turn === t) finish({ stopped: true, text: splitReply(t.lines).text });
      }, STOP_GRACE_MS);
    }

    // ---- size --------------------------------------------------------------

    function applySize(width) {
      const mode = sizeMode === "auto" ? sizeFor(width) : sizeMode;
      el.classList.toggle("jae--small", mode === "small");
      el.classList.toggle("jae--normal", mode !== "small");
    }
    function setSize(mode) {
      sizeMode = mode === "small" || mode === "normal" ? mode : "auto";
      applySize(el.clientWidth);
    }
    if (typeof root.ResizeObserver === "function") {
      observer = new root.ResizeObserver((entries) => {
        const w = entries[0] && entries[0].contentRect ? entries[0].contentRect.width : 0;
        if (w > 0 && sizeMode === "auto") applySize(w);   // a hidden (0-wide) pane keeps its last layout
      });
      observer.observe(el);
    }
    applySize(0);

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        submit();
      }
    });
    showEmpty();

    function destroy() {
      destroyed = true;
      clearTimeout(stopTimer);
      // The host is going away (its tab closed): drop the running turn WITHOUT reporting it,
      // so no onTurnEnd fires into a screen that no longer exists. Closing the socket below
      // is what makes the server kill the child.
      if (turn) { clearTimeout(turn.renderTimer); turn = null; }
      if (observer) { observer.disconnect(); observer = null; }
      if (sock) { try { sock.close(); } catch (_e) { /* gone */ } sock = null; }
    }

    return {
      el, send, stop, newConversation, loadConversation, setSize, destroy,
      busy: () => busy,
      conversationId: () => convId,
      focus: () => { try { input.focus(); } catch (_e) { /* not attached */ } },
      setStatus,
    };
  }

  root.JarvisAskEmbed = {
    create, splitReply, stripAnsi, sizeFor, newStream, applyStream, foldNone, SMALL_BELOW,
  };
})(typeof window !== "undefined" ? window : globalThis);
