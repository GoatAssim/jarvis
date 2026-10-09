/* ============================================================================
 * log-conversation.js — the "Conversation" view inside Menu → Logs (L.2).
 *
 * WHAT IT IS
 * ----------
 * The Logs overlay used to show one box per raw log line (the "Organized"
 * view). That is a faithful dump but not something you can *read*: a single
 * ask is 30–150 lines of request / response / usage / prompt_cache /
 * tool_call / tool_result. This module reconstructs what happened as a
 * conversation: a user bubble, the assistant's bubbles, one expandable card
 * per tool call, and slim timeline rows for the things that are not chat
 * (provider attempts, key failover, errors, side requests such as the risk
 * review). Raw JSON stays one click away on every event, and the separate
 * "Raw JSON" view still shows the exact underlying entries.
 *
 * TWO HALVES
 * ----------
 *  1. build(entries)   PURE. Flat log entries in, a timeline of turns out.
 *                      No DOM; tests/verify_log_conversation.js loads this
 *                      file into a bare `window` with `vm` and exercises it.
 *  2. render(...)      DOM. Draws a timeline with the SAME bubble classes the
 *                      Ask panel uses (.ask-msg, .ask-msg__bubble, …) and the
 *                      app's own markdown renderer (passed in as `deps`).
 *
 * RULES IT FOLLOWS (master plan L.2)
 * ----------------------------------
 *  - Never invent. A timestamp, status, duration, provider or round is shown
 *    only when the log carries it (or it is derived from timestamps that ARE
 *    logged, and then it says so). A thing the log does not record — retry
 *    cooldowns, forced endings, confirmation prompts, task start/finish —
 *    simply does not appear as an event.
 *  - Never falsely merge. A tool_result is paired with a tool_call only by
 *    order + name inside one turn. A model-requested tool call that has no
 *    tool_call entry stays visible as "requested — no execution logged".
 *  - Never crash on a bad entry. Non-objects, missing fields and unknown
 *    shapes degrade to a generic row; every event renders inside try/catch.
 *  - No new backend. Reads the same entries /api/logs/:id already returns.
 *
 * PROVIDER SHAPES it understands (request payload / response body):
 *   Gemini            contents[].parts[] / candidates[0].content.parts[]
 *   OpenAI-compatible messages[] / choices[0].message   (Groq, Ollama /v1, …)
 *   Anthropic         messages[] with content blocks / content[] blocks
 *   Ollama native     messages[] / message{content,thinking,tool_calls}
 * ========================================================================== */
(function (global) {
  "use strict";

  const MAX_RAW_CHARS = 12000;   // per JSON block until "Show all" is clicked
  // ai_providers._TOOLS_WITHHELD_NOTICE is merged into the last user message
  // when tools are switched off for the rest of a reply; it must never be
  // mistaken for something the person typed.
  const NOTICE_MARK = "You can't use tools for the rest of this reply.";
  const RISK_REVIEW_MARK = "is about to run this tool call";

  // ---------------------------------------------------------------------------
  // Small pure helpers
  // ---------------------------------------------------------------------------

  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const str = (v) => (typeof v === "string" ? v : "");
  const normName = (s) => String(s == null ? "" : s).toLowerCase().replace(/[^a-z0-9]/g, "");
  const pad = (n, w) => String(n).padStart(w || 2, "0");

  function clip(s, n) {
    const t = String(s == null ? "" : s);
    return t.length > n ? t.slice(0, Math.max(0, n - 1)) + "\u2026" : t;
  }

  function tsMs(ts) {
    if (typeof ts !== "string" || !ts) return null;
    const ms = Date.parse(ts);
    return Number.isFinite(ms) ? ms : null;
  }

  // The log stores UTC ISO strings; the person reads local time.
  function fmtLocal(ts) {
    const ms = tsMs(ts);
    if (ms == null) return typeof ts === "string" && ts ? ts : "?";
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }

  function fmtClock(ts) {
    const full = fmtLocal(ts);
    return full.length > 11 ? full.slice(11) : full;
  }

  function fmtDuration(ms) {
    if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
    const total = Math.round(ms / 1000);
    return `${Math.floor(total / 60)} m ${pad(total % 60)} s`;
  }

  function hostOf(url) {
    if (typeof url !== "string" || !url) return "";
    const m = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/i.exec(url);
    return m ? m[1].toLowerCase() : "";
  }

  function entryKey(e) {
    return (isObj(e) ? str(e.ts) : "") + "|" + (isObj(e) ? str(e.direction) : "");
  }

  function valuePreview(v) {
    if (v == null) return String(v);
    if (typeof v === "string") return JSON.stringify(clip(v, 40));
    if (typeof v === "number" || typeof v === "boolean") return String(v);
    if (Array.isArray(v)) return `[${v.length}]`;
    return "{\u2026}";
  }

  // "path="main.py", mode="r"" — what a tool was asked, in one short line.
  function summarizeArgs(args, max) {
    const limit = max || 90;
    if (args == null) return "(no arguments)";
    if (!isObj(args)) return clip(typeof args === "string" ? args : JSON.stringify(args), limit);
    const keys = Object.keys(args);
    if (!keys.length) return "(no arguments)";
    return clip(keys.map((k) => `${k}=${valuePreview(args[k])}`).join(", "), limit);
  }

  // ok / error / cancelled from what the result itself says. A result that
  // says nothing either way is "ok" (it ran and returned); it is never
  // upgraded to a success the log doesn't show, only not flagged as failure.
  function classifyResult(result) {
    if (!isObj(result)) return { status: "ok", note: "" };
    if (result.cancelled || result.canceled || result.denied ||
        result.status === "cancelled" || result.status === "canceled" || result.status === "denied") {
      return { status: "cancelled", note: clip(str(result.reason) || str(result.error) || "", 120) };
    }
    if (result.error || result.ok === false) {
      return { status: "error", note: clip(typeof result.error === "string" ? result.error : "", 160) };
    }
    return { status: "ok", note: "" };
  }

  function errorMessage(body) {
    if (isObj(body)) {
      if (typeof body.error === "string") return body.error;
      if (isObj(body.error)) return str(body.error.message) || clip(JSON.stringify(body.error), 240);
      if (typeof body.message === "string") return body.message;
    }
    if (typeof body === "string") return clip(body, 240);
    try { return clip(JSON.stringify(body), 240); } catch (_) { return "unreadable error body"; }
  }

  // ---------------------------------------------------------------------------
  // Pulling the human text out of provider-shaped payloads
  // ---------------------------------------------------------------------------

  function textOfContent(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";
    const out = [];
    for (const b of content) {
      if (typeof b === "string") out.push(b);
      else if (isObj(b) && (b.type === "text" || b.type === undefined) && typeof b.text === "string") out.push(b.text);
    }
    return out.join("\n");
  }

  function stripNotice(text) {
    const i = text.indexOf(NOTICE_MARK);
    return i === -1 ? text : text.slice(0, i).replace(/\s+$/, "");
  }

  function isSyntheticUserText(text) {
    return /^Result of [^\n]*?:/.test(text) || text.startsWith("[tool result]");
  }

  // The latest thing a PERSON typed in this request's history. Walks back
  // over tool results and the "tools withheld" notice, which providers send
  // under the user role too.
  function extractUserText(payload) {
    if (!isObj(payload)) return null;
    if (Array.isArray(payload.messages)) {
      for (let i = payload.messages.length - 1; i >= 0; i--) {
        const m = payload.messages[i];
        if (!isObj(m) || m.role !== "user") continue;
        const t = stripNotice(textOfContent(m.content)).trim();
        if (t && !isSyntheticUserText(t)) return t;
      }
    }
    if (Array.isArray(payload.contents)) {
      for (let i = payload.contents.length - 1; i >= 0; i--) {
        const m = payload.contents[i];
        if (!isObj(m) || m.role !== "user" || !Array.isArray(m.parts)) continue;
        const t = stripNotice(m.parts.map((p) => (isObj(p) && typeof p.text === "string" ? p.text : "")).join("")).trim();
        if (t && !isSyntheticUserText(t)) return t;
      }
    }
    return null;
  }

  function parseJsonLoose(v) {
    if (typeof v !== "string") return v;
    try { return JSON.parse(v); } catch (_) { return v; }
  }

  // One normalized view of a response body: { text, thinking, calls[], finish }.
  function parseResponseBody(body) {
    const out = { text: "", thinking: "", calls: [], finish: "" };
    if (!isObj(body)) return out;
    if (Array.isArray(body.candidates)) {                       // Gemini
      const c = isObj(body.candidates[0]) ? body.candidates[0] : {};
      const parts = isObj(c.content) && Array.isArray(c.content.parts) ? c.content.parts : [];
      for (const p of parts) {
        if (!isObj(p)) continue;
        if (isObj(p.functionCall)) out.calls.push({ name: str(p.functionCall.name), args: p.functionCall.args });
        else if (typeof p.text === "string") { if (p.thought) out.thinking += p.text; else out.text += p.text; }
      }
      out.finish = str(c.finishReason);
    } else if (Array.isArray(body.choices)) {                   // OpenAI-compatible
      const ch = isObj(body.choices[0]) ? body.choices[0] : {};
      const m = isObj(ch.message) ? ch.message : {};
      out.text = textOfContent(m.content);
      out.thinking = str(m.reasoning) || str(m.reasoning_content) || str(m.thinking);
      for (const tc of Array.isArray(m.tool_calls) ? m.tool_calls : []) {
        const fn = isObj(tc) && isObj(tc.function) ? tc.function : {};
        out.calls.push({ name: str(fn.name), args: parseJsonLoose(fn.arguments) });
      }
      out.finish = str(ch.finish_reason);
    } else if (Array.isArray(body.content)) {                   // Anthropic
      for (const b of body.content) {
        if (!isObj(b)) continue;
        if (b.type === "text" && typeof b.text === "string") out.text += b.text;
        else if (b.type === "thinking" && typeof b.thinking === "string") out.thinking += b.thinking;
        else if (b.type === "tool_use") out.calls.push({ name: str(b.name), args: b.input });
      }
      out.finish = str(body.stop_reason);
    } else if (isObj(body.message)) {                           // Ollama native
      const m = body.message;
      out.text = textOfContent(m.content);
      out.thinking = str(m.thinking);
      for (const tc of Array.isArray(m.tool_calls) ? m.tool_calls : []) {
        const fn = isObj(tc) && isObj(tc.function) ? tc.function : {};
        out.calls.push({ name: str(fn.name), args: parseJsonLoose(fn.arguments) });
      }
      out.finish = str(body.done_reason);
    }
    return out;
  }

  // ---------------------------------------------------------------------------
  // build(): flat entries -> turns
  // ---------------------------------------------------------------------------
  //
  // timeline = { turns: Turn[], stats: { entries, malformed } }
  // Turn     = { index, firstTs, lastTs, user, sources[], taskLabels[], events[],
  //              tokensIn, tokensOut, askTotal, closed, toolCount, errorCount }
  // Event    = { type, ts, provider, round, entries[], ... } where type is one of
  //   user | assistant | tool | attempt | error | side | console | command | empty | info | other

  function buildTimeline(entries) {
    const list = Array.isArray(entries) ? entries : [];
    const turns = [];
    const stats = { entries: list.length, malformed: 0 };

    let cur = null;
    let attemptHost = "";
    let pendingTools = [];    // tool events with a call and no result yet
    let lastModel = null;     // { kind: "main", call } | { kind: "side", ev } — where usage lines attach
    let openMain = null;      // the main model call still waiting for its response (a side request can sit between)
    let pendingCache = null;  // prompt_cache entry waiting for its request
    let prevMs = null;        // timestamp of the previous well-formed entry

    function startTurn(entry) {
      cur = {
        index: turns.length, firstTs: str(entry.ts) || null, lastTs: str(entry.ts) || null,
        user: null, sources: [], taskLabels: [], events: [], calls: [],
        tokensIn: 0, tokensOut: 0, askTotal: null, closed: false,
        toolCount: 0, errorCount: 0, attemptLabel: null,
      };
      turns.push(cur);
      pendingTools = [];
      lastModel = null;
      openMain = null;
      attemptHost = "";
      return cur;
    }

    function touch(entry) {
      if (!cur) startTurn(entry);
      if (str(entry.ts)) { cur.lastTs = entry.ts; if (!cur.firstTs) cur.firstTs = entry.ts; }
      const s = str(entry.source);
      if (s && !cur.sources.includes(s)) cur.sources.push(s);
      const t = str(entry.task_label);
      if (t && !cur.taskLabels.includes(t)) cur.taskLabels.push(t);
    }

    function addEvent(type, entry, extra) {
      const ev = Object.assign({
        type, ts: str(entry.ts) || null,
        provider: entry.provider == null ? null : String(entry.provider),
        round: typeof entry.round === "number" ? entry.round : null,
        taskLabel: str(entry.task_label), source: str(entry.source),
        entries: [entry],
      }, extra || {});
      cur.events.push(ev);
      return ev;
    }

    function setUser(text, entry) {
      cur.user = { text, ts: str(entry.ts) || null };
      cur.events.unshift({
        type: "user", ts: str(entry.ts) || null, provider: null, round: null,
        taskLabel: str(entry.task_label), source: str(entry.source), text, entries: [entry],
      });
    }

    // A request that begins a new ask. Attempt rows logged just BEFORE that
    // request (the capacity "info" line comes first) belong to the new ask,
    // not the tail of the previous one when it never wrote an end marker.
    function startTurnFromRequest(entry) {
      const prev = cur;
      startTurn(entry);
      if (prev && !prev.closed) {
        const moved = [];
        while (prev.events.length && prev.events[prev.events.length - 1].type === "attempt") {
          moved.unshift(prev.events.pop());
        }
        for (const ev of moved) { ev.switched = false; ev.from = null; cur.events.push(ev); cur.attemptLabel = ev.provider; }
      }
    }

    list.forEach((entry, idx) => {
      if (!isObj(entry)) { stats.malformed++; return; }
      try {
        const direction = str(entry.direction) || "?";
        const d = entry.data;
        const ms = tsMs(entry.ts);
        const gapMs = ms != null && prevMs != null ? ms - prevMs : null;
        if (ms != null) prevMs = ms;

        switch (direction) {
          case "info": {
            if (isObj(d) && d.capacity_mode !== undefined) {
              if (cur && cur.closed) startTurn(entry);
              touch(entry);
              const label = entry.provider == null ? "" : String(entry.provider);
              const switched = !!(cur.attemptLabel && cur.attemptLabel !== label);
              addEvent("attempt", entry, {
                switched, from: switched ? cur.attemptLabel : null,
                capacity: str(d.capacity_label) || str(d.capacity_mode),
              });
              cur.attemptLabel = label;
              attemptHost = "";
            } else if (isObj(d) && d.ask_usage !== undefined) {
              touch(entry);
              cur.askTotal = isObj(d.ask_usage) ? d.ask_usage : null;
              cur.closed = true;
            } else if (isObj(d) && Array.isArray(d.console_dump)) {
              touch(entry);
              addEvent("console", entry, { lines: d.console_dump.map((l) => String(l)) });
            } else {
              touch(entry);
              addEvent("info", entry, {});
            }
            break;
          }
          case "prompt_cache": {
            touch(entry);
            pendingCache = entry;
            break;
          }
          case "request": {
            const payload = isObj(d) ? d.payload : null;
            const url = isObj(d) ? str(d.url) : "";
            const host = hostOf(url);
            const label = entry.provider == null ? "" : String(entry.provider);
            const side = label.includes("(side request during") || !!(attemptHost && host && host !== attemptHost);
            if (side) {
              touch(entry);
              const prompt = extractUserText(payload) || "";
              const ev = addEvent("side", entry, {
                host, model: isObj(payload) ? str(payload.model) : "",
                riskReview: prompt.includes(RISK_REVIEW_MARK),
                prompt: clip(prompt, 300), response: null, usage: null,
              });
              lastModel = { kind: "side", ev };
              break;
            }
            if (!attemptHost && host) attemptHost = host;
            const userText = extractUserText(payload);
            if (!cur || cur.closed || (userText && cur.user && userText !== cur.user.text)) startTurnFromRequest(entry);
            touch(entry);
            if (userText && !cur.user) setUser(userText, entry);
            const call = {
              request: entry, response: null, usage: null, cache: pendingCache, parsed: null,
              primary: null, placeholders: [], round: null, entries: pendingCache ? [pendingCache, entry] : [entry],
            };
            pendingCache = null;
            cur.calls.push(call);
            openMain = call;
            lastModel = { kind: "main", call };
            break;
          }
          case "response": {
            touch(entry);
            if (lastModel && lastModel.kind === "side" && !lastModel.ev.response) {
              lastModel.ev.response = entry;
              lastModel.ev.entries.push(entry);
              break;
            }
            let call;
            if (openMain && !openMain.response) {
              call = openMain;
            } else {                       // a response with no request in the log
              call = { request: null, response: null, usage: null, cache: null, parsed: null, primary: null, placeholders: [], round: null, entries: [] };
              cur.calls.push(call);
            }
            openMain = null;
            lastModel = { kind: "main", call };
            call.response = entry;
            call.entries.push(entry);
            const status = isObj(d) && typeof d.status === "number" ? d.status : null;
            const body = isObj(d) ? d.body : undefined;
            if ((status != null && status >= 400) || (isObj(body) && body.error && !Array.isArray(body.candidates) && !Array.isArray(body.choices))) {
              cur.errorCount++;
              call.primary = addEvent("error", entry, { message: errorMessage(body), httpStatus: status, call });
              call.primary.entries = call.entries.slice();
              break;
            }
            const parsed = parseResponseBody(body);
            call.parsed = parsed;
            if (parsed.text.trim() || parsed.thinking.trim()) {
              call.primary = addEvent("assistant", entry, {
                text: parsed.text, thinking: parsed.thinking, finish: parsed.finish, final: false, call,
              });
              call.primary.entries = call.entries.slice();
            }
            for (const c of parsed.calls) {
              const ph = addEvent("tool", entry, {
                name: c.name, args: c.args, requestedOnly: true, callEntry: null, resultEntry: null,
                status: "requested", note: "", durationMs: null, call, requestedTs: str(entry.ts) || null,
              });
              ph.entries = [];
              call.placeholders.push(ph);
              if (!call.primary) { call.primary = ph; }
            }
            if (!call.primary) {
              call.primary = addEvent("empty", entry, { call });
              call.primary.entries = call.entries.slice();
            }
            if (call.primary.type === "tool") call.primary.entries = call.entries.slice();
            break;
          }
          case "usage": {
            touch(entry);
            const inTok = isObj(d) ? Number(d.input_tokens) || 0 : 0;
            const outTok = isObj(d) ? Number(d.output_tokens) || 0 : 0;
            cur.tokensIn += inTok; cur.tokensOut += outTok;
            if (lastModel && lastModel.kind === "side") {
              lastModel.ev.usage = isObj(d) ? d : null;
              lastModel.ev.entries.push(entry);
            } else if (lastModel && lastModel.kind === "main") {
              const call = lastModel.call;
              call.usage = isObj(d) ? d : null;
              if (isObj(d) && typeof d.round === "number") call.round = d.round;
              call.entries.push(entry);
              if (call.primary) call.primary.entries.push(entry);
            }
            break;
          }
          case "tool_call": {
            touch(entry);
            const name = isObj(d) ? str(d.name) : "";
            const lastCall = cur.calls[cur.calls.length - 1];
            let ev = lastCall ? lastCall.placeholders.find((p) => p.requestedOnly && normName(p.name) === normName(name)) : null;
            if (ev) {
              ev.requestedOnly = false;
              ev.status = "running";
              ev.entries.push(entry);
              ev.ts = str(entry.ts) || ev.ts;
            } else {
              ev = addEvent("tool", entry, {
                name, args: undefined, requestedOnly: false, resultEntry: null, status: "running", note: "",
                durationMs: null, call: lastCall || null, requestedTs: null,
              });
            }
            ev.callEntry = entry;
            ev.name = name || ev.name;
            ev.args = isObj(d) ? d.arguments : undefined;
            ev.tokensIn = isObj(d) && d.input_tokens != null ? Number(d.input_tokens) : null;
            if (typeof entry.round === "number") ev.round = entry.round;
            ev.taskLabel = str(entry.task_label) || ev.taskLabel;
            ev.source = str(entry.source) || ev.source;
            ev.provider = entry.provider == null ? ev.provider : String(entry.provider);
            // The log line is written AFTER the tool finishes, so the time
            // since the previous log line is the closest honest "how long it
            // took" (it includes any approval / risk-review wait).
            ev.durationMs = gapMs != null && gapMs >= 0 ? gapMs : null;
            cur.toolCount++;
            pendingTools.push(ev);
            break;
          }
          case "tool_result": {
            touch(entry);
            const name = isObj(d) ? str(d.name) : "";
            const at = pendingTools.findIndex((t) => normName(t.name) === normName(name));
            const result = isObj(d) ? d.result : undefined;
            const cls = classifyResult(result);
            let ev;
            if (at !== -1) {
              ev = pendingTools.splice(at, 1)[0];
              ev.entries.push(entry);
            } else {
              ev = addEvent("tool", entry, {
                name, args: undefined, requestedOnly: false, callEntry: null, status: "ok", note: "",
                durationMs: null, call: null, requestedTs: null, callMissing: true,
              });
              cur.toolCount++;
            }
            ev.resultEntry = entry;
            ev.result = result;
            ev.status = cls.status;
            ev.note = cls.note;
            ev.tokensOut = isObj(d) && d.output_tokens != null ? Number(d.output_tokens) : null;
            if (cls.status === "error") cur.errorCount++;
            break;
          }
          case "error": {
            touch(entry);
            cur.errorCount++;
            addEvent("error", entry, { message: errorMessage(d), httpStatus: null });
            break;
          }
          case "command_run": {
            touch(entry);
            addEvent("command", entry, {
              cmdline: isObj(d) ? str(d.cmdline) : "", exitCode: isObj(d) ? d.exit_code : null,
              lines: isObj(d) && Array.isArray(d.lines) ? d.lines : [],
            });
            break;
          }
          default: {
            touch(entry);
            addEvent("other", entry, { direction });
          }
        }
      } catch (err) {
        stats.malformed++;
        if (!cur) startTurn({ ts: null });
        cur.events.push({
          type: "other", ts: str(entry.ts) || null, provider: null, round: null, taskLabel: "", source: "",
          direction: str(entry.direction) || "?", entries: [entry], unreadable: String(err && err.message || err),
        });
      }
    });

    // ---- finalize ----------------------------------------------------------
    for (const turn of turns) {
      let lastText = -1;
      turn.events.forEach((ev, i) => {
        if (ev.type === "assistant" && ev.text.trim()) lastText = i;
        if (ev.type === "tool") {
          if (ev.requestedOnly) { ev.status = "no_run"; }
          else if (ev.status === "running") { ev.status = "no_result"; }
          if (ev.round == null && ev.call && ev.call.round != null) ev.round = ev.call.round;
          if (!ev.entries.length && ev.call) ev.entries = ev.call.entries.slice();
        } else if ((ev.type === "assistant" || ev.type === "error" || ev.type === "empty") && ev.call) {
          if (ev.round == null && ev.call.round != null) ev.round = ev.call.round;
        }
      });
      if (lastText !== -1) {
        const ev = turn.events[lastText];
        const callsOwn = ev.call && ev.call.parsed ? ev.call.parsed.calls.length : 0;
        const toolAfter = turn.events.slice(lastText + 1).some((e) => e.type === "tool");
        ev.final = callsOwn === 0 && !toolAfter;
      }
    }
    return { turns, stats };
  }

  // ---------------------------------------------------------------------------
  // Filtering (direction / source / search hits) — pure
  // ---------------------------------------------------------------------------

  function eventMatches(ev, f) {
    if (!f) return true;
    const es = Array.isArray(ev.entries) ? ev.entries : [];
    if (f.direction && !es.some((e) => isObj(e) && e.direction === f.direction)) return false;
    if (f.source && !es.some((e) => isObj(e) && str(e.source) === f.source)) return false;
    if (f.hits && !es.some((e) => f.hits.has(entryKey(e)))) return false;
    return true;
  }

  function filterIsActive(f) {
    return !!(f && (f.direction || f.source || f.hits));
  }

  // Which events to draw. Matches + one neighbour either side stay visible
  // (so a hit is never shown without the thing just before and after it); a
  // run of the rest becomes one fold. The user message of a turn is always
  // visible. `forceKeep` is the focused event, which must never be folded.
  function planVisibility(turn, f, showAll, forceKeep) {
    const evs = turn.events;
    const n = evs.length;
    const match = evs.map((ev) => eventMatches(ev, f));
    if (!filterIsActive(f) || showAll) return { match, keep: evs.map(() => true), matched: match.filter(Boolean).length };
    const keep = new Array(n).fill(false);
    for (let i = 0; i < n; i++) {
      if (evs[i].type === "user" || evs[i] === forceKeep) keep[i] = true;
      if (match[i]) { keep[i] = true; if (i > 0) keep[i - 1] = true; if (i < n - 1) keep[i + 1] = true; }
    }
    return { match, keep, matched: match.filter(Boolean).length };
  }

  function findEventForEntry(timeline, ref) {
    if (!timeline || !ref) return null;
    const want = (str(ref.ts)) + "|" + str(ref.direction);
    for (const turn of timeline.turns) {
      for (const ev of turn.events) {
        if (ev.entries.some((e) => entryKey(e) === want)) return ev;
      }
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------------

  function appendKids(node, kids) {
    if (kids == null) return node;
    for (const k of Array.isArray(kids) ? kids : [kids]) {
      if (k == null || k === false) continue;
      node.appendChild(typeof k === "object" ? k : document.createTextNode(String(k)));
    }
    return node;
  }

  function h(tag, cls, kids, attrs) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (attrs) for (const k of Object.keys(attrs)) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    return appendKids(n, kids);
  }

  function chip(text, cls, title) {
    return h("span", "lc-chip" + (cls ? " " + cls : ""), String(text), title ? { title } : null);
  }

  function jsonText(value) {
    if (typeof value === "string") return value;
    try {
      const t = JSON.stringify(value, null, 2);
      return t === undefined ? String(value) : t;
    } catch (_) { return String(value); }
  }

  // A pre that never dumps a giant payload into the page: capped until asked.
  function jsonBlock(value, label) {
    const text = jsonText(value);
    const wrap = h("div", "lc-json");
    const over = text.length > MAX_RAW_CHARS;
    const pre = h("pre", "lc-json__pre");
    pre.textContent = over ? text.slice(0, MAX_RAW_CHARS) : text;
    const head = h("div", "lc-json__head", label ? h("span", "lc-json__label", label) : null);
    const copy = h("button", "lc-linkbtn", "Copy", { type: "button" });
    copy.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      const done = () => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1200); };
      try { navigator.clipboard.writeText(text).then(done, () => {}); } catch (_) { /* no clipboard */ }
    });
    head.appendChild(copy);
    wrap.appendChild(head);
    wrap.appendChild(pre);
    if (over) {
      const more = h("button", "lc-linkbtn", `Show all (${text.length.toLocaleString()} chars)`, { type: "button" });
      more.addEventListener("click", (e) => { e.preventDefault(); pre.textContent = text; more.remove(); });
      wrap.appendChild(more);
    }
    return wrap;
  }

  // The path from any rendered event to its exact log lines, built only when
  // opened so a long turn stays cheap to draw.
  function rawDetails(entries, label) {
    const real = (entries || []).filter(isObj);
    const d = h("details", "lc-raw", h("summary", null, `${label || "Raw log entries"} (${real.length})`));
    let built = false;
    d.addEventListener("toggle", () => {
      if (!d.open || built) return;
      built = true;
      for (const e of real) {
        d.appendChild(h("div", "lc-raw__head", [
          h("span", "lc-ts", fmtLocal(e.ts), { title: str(e.ts) }),
          chip(str(e.direction) || "?", "lc-chip--dir"),
          e.provider ? chip(String(e.provider)) : null,
          typeof e.round === "number" ? chip(`round ${e.round}`) : null,
        ]));
        d.appendChild(jsonBlock(e));
      }
    });
    return d;
  }

  function metaRow(parts) {
    return h("div", "lc-meta", parts.filter(Boolean));
  }

  function tokenText(call) {
    if (!call || !isObj(call.usage)) return "";
    return `${Number(call.usage.input_tokens) || 0} in / ${Number(call.usage.output_tokens) || 0} out`;
  }

  function providerChips(ev) {
    return [
      ev.provider ? chip(ev.provider, "lc-chip--provider") : null,
      ev.round != null ? chip(`round ${ev.round}`, null, ev.callEntry && typeof ev.callEntry.round === "number" ? "" : "Round taken from the model call this belongs to") : null,
    ];
  }

  function renderUser(ev, deps) {
    const bubble = h("div", "ask-msg__bubble");
    bubble.textContent = ev.text;
    if (deps.renderUserText) { try { deps.renderUserText(bubble, ev.text); } catch (_) { bubble.textContent = ev.text; } }
    return h("div", "ask-msg ask-msg--user lc-ev", [
      h("div", "ask-msg__role", "You"),
      bubble,
      metaRow([h("span", "lc-ts", fmtLocal(ev.ts), { title: str(ev.ts) }), ev.source ? chip(ev.source, "lc-chip--origin") : null,
        ev.taskLabel ? chip(`Task: ${ev.taskLabel}`, "lc-chip--task") : null]),
      rawDetails(ev.entries.slice(0, 1), "Raw request"),
    ]);
  }

  function renderAssistant(ev, deps) {
    const text = ev.text || "";
    const children = [];
    children.push(h("div", "ask-msg__role", (deps.assistantName || "Jarvis") + (ev.final ? "" : " \u00b7 before tool call")));
    if (text.trim()) {
      const bubble = h("div", "ask-msg__bubble");
      let ok = false;
      if (deps.renderRich) { try { deps.renderRich(bubble, text); ok = true; } catch (_) { ok = false; } }
      if (!ok) bubble.textContent = text;
      children.push(bubble);
    }
    if (ev.thinking && ev.thinking.trim()) {
      children.push(h("details", "lc-think", [
        h("summary", null, text.trim() ? "Thinking" : "Thinking (no reply text in this response)"),
        h("pre", "lc-think__text", ev.thinking),
      ]));
    }
    children.push(metaRow([
      h("span", "lc-ts", fmtLocal(ev.ts), { title: str(ev.ts) }),
      ...providerChips(ev),
      tokenText(ev.call) ? chip(tokenText(ev.call) + " tok") : null,
      ev.finish ? chip(ev.finish, null, "finish reason") : null,
      ev.taskLabel ? chip(`Task: ${ev.taskLabel}`, "lc-chip--task") : null,
    ]));
    children.push(rawDetails(ev.entries, "Raw model call"));
    return h("div", "ask-msg ask-msg--jarvis lc-ev" + (ev.final ? "" : " ask-msg--interim"), children);
  }

  const STATUS_LABEL = {
    ok: "ok", error: "error", cancelled: "cancelled", running: "no result logged",
    no_result: "no result logged", no_run: "no execution logged", requested: "requested",
  };

  function renderTool(ev) {
    const summary = h("summary", "lc-tool__sum", [
      h("span", "lc-dot lc-dot--" + ev.status),
      h("span", "lc-tool__name", ev.name || "(unnamed tool)"),
      h("span", "lc-tool__args", summarizeArgs(ev.args)),
      ev.durationMs != null && ev.callEntry ? chip("\u2248 " + fmtDuration(ev.durationMs), "lc-chip--dur",
        "Time since the previous log line. The log is written after the tool finishes, so this is the closest honest run time (it includes any approval or risk-review wait).") : null,
      chip(STATUS_LABEL[ev.status] || ev.status, "lc-chip--status-" + ev.status, ev.note || ""),
      h("span", "lc-ts", ev.ts ? fmtClock(ev.ts) : "", { title: ev.ts ? `${fmtLocal(ev.ts)} (${ev.ts})` : "" }),
    ]);
    const body = h("div", "lc-tool__body");
    body.appendChild(metaRow([
      ...providerChips(ev),
      ev.tokensIn != null ? chip(`~${ev.tokensIn} tok in`) : null,
      ev.tokensOut != null ? chip(`~${ev.tokensOut} tok out`) : null,
      ev.taskLabel ? chip(`Task: ${ev.taskLabel}`, "lc-chip--task") : null,
      ev.source ? chip(ev.source, "lc-chip--origin") : null,
      ev.requestedTs && ev.callEntry ? chip(`requested ${fmtClock(ev.requestedTs)}`, null, "When the model's response asking for this call was logged") : null,
    ]));
    if (ev.requestedOnly) {
      body.appendChild(h("div", "lc-note",
        "The model asked for this call, but no tool_call line was logged for it \u2014 for example it was replayed from the cache after a failover, or never approved."));
    }
    if (ev.callMissing) body.appendChild(h("div", "lc-note", "A result was logged with no matching call before it."));
    if (ev.note) body.appendChild(h("div", "lc-note", ev.note));
    body.appendChild(jsonBlock(ev.args === undefined ? "(not logged)" : ev.args, "Arguments"));
    if (ev.resultEntry) body.appendChild(jsonBlock(ev.result === undefined ? "(not logged)" : ev.result, "Result"));
    body.appendChild(rawDetails(ev.entries, "Raw log entries"));
    return h("details", "lc-tool lc-ev is-" + ev.status, [summary, body]);
  }

  function sysRow(kind, icon, title, ev, bodyKids) {
    const summary = h("summary", "lc-sys__sum", [
      h("span", "lc-sys__icon", icon),
      h("span", "lc-sys__title", title),
      ev.provider && kind !== "attempt" && kind !== "switch" ? chip(ev.provider, "lc-chip--provider") : null,
      h("span", "lc-ts", ev.ts ? fmtClock(ev.ts) : "", { title: ev.ts ? `${fmtLocal(ev.ts)} (${ev.ts})` : "" }),
    ]);
    const body = h("div", "lc-sys__body", bodyKids || []);
    body.appendChild(rawDetails(ev.entries, "Raw log entries"));
    return h("details", `lc-sys lc-sys--${kind} lc-ev`, [summary, body]);
  }

  function renderSystem(ev) {
    switch (ev.type) {
      case "attempt":
        return ev.switched
          ? sysRow("switch", "\u21c4", `Switched to ${ev.provider || "another key"}${ev.from ? ` (was ${ev.from})` : ""}${ev.capacity ? ` \u00b7 ${ev.capacity}` : ""}`, ev)
          : sysRow("attempt", "\u25b6", `Provider attempt: ${ev.provider || "unknown"}${ev.capacity ? ` \u00b7 ${ev.capacity}` : ""}`, ev);
      case "error":
        return sysRow("error", "!", (ev.httpStatus ? `HTTP ${ev.httpStatus} \u00b7 ` : "Error \u00b7 ") + clip(ev.message, 200), ev,
          [h("div", "lc-note", ev.message)]);
      case "side": {
        const title = (ev.riskReview ? "Risk review request" : "Side request") + (ev.host ? ` \u2192 ${ev.host}` : "") + (ev.model ? ` \u00b7 ${ev.model}` : "");
        const kids = [];
        if (ev.prompt) kids.push(jsonBlock(ev.prompt, "Prompt"));
        if (ev.response && isObj(ev.response.data)) {
          const parsed = parseResponseBody(ev.response.data.body);
          kids.push(jsonBlock(parsed.text || ev.response.data.body, "Reply"));
        }
        return sysRow("side", "\u21aa", title, ev, kids);
      }
      case "console":
        return sysRow("console", "\u2263", `Console output (${ev.lines.length} line${ev.lines.length === 1 ? "" : "s"})`, ev,
          [h("pre", "lc-json__pre", ev.lines.join("\n"))]);
      case "command":
        return sysRow("command", "$", `Command run${ev.exitCode != null ? ` \u00b7 exit ${ev.exitCode}` : ""} \u00b7 ${clip(ev.cmdline, 140)}`, ev,
          [h("pre", "lc-json__pre", ev.lines.map((l) => (isObj(l) ? str(l.text) : String(l))).join("\n"))]);
      case "empty":
        return sysRow("empty", "\u2205", "Model returned an empty response (no text, no tool call)", ev);
      case "info":
        return sysRow("info", "i", "Info", ev, [jsonBlock(ev.entries[0] && ev.entries[0].data, "Data")]);
      default:
        return sysRow("other", "?", ev.unreadable ? `Couldn't interpret this entry (${clip(ev.unreadable, 80)})` : `Entry: ${ev.direction || "?"}`, ev);
    }
  }

  function renderEvent(ev, deps) {
    try {
      if (ev.type === "user") return renderUser(ev, deps);
      if (ev.type === "assistant") return renderAssistant(ev, deps);
      if (ev.type === "tool") return renderTool(ev);
      return renderSystem(ev);
    } catch (err) {
      return h("div", "lc-sys lc-sys--other lc-ev", [
        h("div", "lc-sys__sum", [h("span", "lc-sys__icon", "?"), h("span", "lc-sys__title", `Couldn't draw this event (${clip(err && err.message, 80)})`)]),
        rawDetails(ev.entries, "Raw log entries"),
      ]);
    }
  }

  function renderTurnHead(turn, nTurns) {
    const spanMs = tsMs(turn.lastTs) != null && tsMs(turn.firstTs) != null ? tsMs(turn.lastTs) - tsMs(turn.firstTs) : null;
    return h("div", "lc-turn__head", [
      h("span", "lc-turn__title", `Turn ${turn.index + 1} of ${nTurns}`),
      h("span", "lc-ts", turn.firstTs ? fmtLocal(turn.firstTs) : "?", { title: str(turn.firstTs) }),
      spanMs != null && spanMs > 0 ? chip(fmtDuration(spanMs), null, "first to last log line of this turn") : null,
      turn.toolCount ? chip(`${turn.toolCount} tool call${turn.toolCount === 1 ? "" : "s"}`) : null,
      turn.errorCount ? chip(`${turn.errorCount} error${turn.errorCount === 1 ? "" : "s"}`, "lc-chip--bad") : null,
      (turn.tokensIn || turn.tokensOut) ? chip(`${turn.tokensIn + turn.tokensOut} tok`, null, `in ${turn.tokensIn} / out ${turn.tokensOut} (summed from usage lines)`) : null,
      ...turn.sources.map((s) => chip(s, "lc-chip--origin")),
      ...turn.taskLabels.map((t) => chip(`Task: ${t}`, "lc-chip--task")),
    ]);
  }

  // render(container, timeline, deps, opts)
  //   deps  { renderRich(el, md), renderUserText(el, text)?, assistantName }
  //   opts  { filter: {direction, source, hits:Set, origin, convOrigin}|null,
  //           showAll, onShowAll(bool), focus: {ts, direction}|null }
  function render(container, timeline, deps, opts) {
    const o = opts || {};
    const f = o.filter || null;
    const active = filterIsActive(f);
    container.innerHTML = "";
    const root = h("div", "lc-root");
    container.appendChild(root);

    const focusEv = o.focus ? findEventForEntry(timeline, o.focus) : null;
    const plans = timeline.turns.map((t) => planVisibility(t, f, !!o.showAll, focusEv));
    const totalEvents = timeline.turns.reduce((n, t) => n + t.events.length, 0);
    const matchedEvents = plans.reduce((n, p) => n + p.matched, 0);

    if (timeline.stats.malformed) {
      root.appendChild(h("div", "lc-banner lc-banner--warn",
        `${timeline.stats.malformed} log line(s) couldn't be read and are skipped here. Switch to Raw JSON to see them.`));
    }
    if (active) {
      const bits = [];
      if (f.direction) bits.push(`direction = ${f.direction}`);
      if (f.source) bits.push(`source = ${f.source}`);
      if (f.hits) bits.push("search matches");
      const banner = h("div", "lc-banner", [
        h("span", null, `Filtered by ${bits.join(" \u00b7 ")}: ${matchedEvents} of ${totalEvents} events match. Matches keep their neighbours; the rest are folded.`),
      ]);
      if (typeof o.onShowAll === "function") {
        const b = h("button", "lc-linkbtn", o.showAll ? "Fold non-matching" : "Show all events", { type: "button" });
        b.addEventListener("click", () => o.onShowAll(!o.showAll));
        banner.appendChild(b);
      }
      root.appendChild(banner);
    }
    // Origin is a property of the whole conversation, not of one event, so it
    // can't fold anything — it can only tell you the filter and this log disagree.
    if (f && f.origin && f.convOrigin !== f.origin) {
      root.appendChild(h("div", "lc-banner lc-banner--warn",
        `Origin filter "${f.origin}" doesn't match this conversation's origin (${f.convOrigin || "typed"}).`));
    }
    if (!timeline.turns.length) {
      root.appendChild(h("div", "debug-empty", "Nothing readable in this log yet."));
      return { totalEvents, matchedEvents };
    }

    let focusEl = null;
    timeline.turns.forEach((turn, ti) => {
      const plan = plans[ti];
      const box = h("section", "lc-turn");
      box.appendChild(renderTurnHead(turn, timeline.turns.length));
      if (!turn.user) box.appendChild(h("div", "lc-note lc-note--turn", "No user message could be found in this turn's logged requests."));
      let i = 0;
      while (i < turn.events.length) {
        if (plan.keep[i]) {
          const ev = turn.events[i];
          const node = renderEvent(ev, deps);
          if (plan.match[i] && active) node.classList.add("is-match");
          if (ev === focusEv) { node.classList.add("is-focus"); focusEl = node; if (node.tagName === "DETAILS") node.open = true; }
          box.appendChild(node);
          i++;
          continue;
        }
        let j = i;
        while (j < turn.events.length && !plan.keep[j]) j++;
        const hidden = turn.events.slice(i, j);
        const fold = h("button", "lc-fold", `\u2026 ${hidden.length} event${hidden.length === 1 ? "" : "s"} hidden by the filter \u2014 show`, { type: "button" });
        fold.addEventListener("click", () => {
          const frag = document.createDocumentFragment();
          for (const ev of hidden) frag.appendChild(renderEvent(ev, deps));
          fold.replaceWith(frag);
        });
        box.appendChild(fold);
        i = j;
      }
      root.appendChild(box);
    });

    if (focusEl) {
      const target = focusEl;
      const go = () => { try { target.scrollIntoView({ block: "center" }); } catch (_) { /* detached */ } };
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(go); else go();
    }
    return { totalEvents, matchedEvents };
  }

  global.JarvisLogConversation = {
    build: buildTimeline,
    render,
    // Pure helpers, exposed for tests/verify_log_conversation.js only.
    _pure: {
      extractUserText, parseResponseBody, summarizeArgs, classifyResult, errorMessage, fmtDuration,
      fmtLocal, hostOf, eventMatches, filterIsActive, planVisibility, findEventForEntry, entryKey,
      stripNotice, isSyntheticUserText,
    },
  };
})(window);
