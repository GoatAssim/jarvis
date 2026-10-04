// Verifies web/public/app.js's live-streaming render entry point (master
// plan §8 / K.3.1.5): applyAskStreamEvent() and the helpers around it —
// thinking segments, streamed text, interim-text commit on a tool round,
// the mid-stream-failover reset, the show/hide thinking toggle, and the
// wiring that hooks all of it into handleWsMessage / finalizeAskBubble /
// setThinkShow. Also asserts the server.js and CLI sides of the same
// marker protocol are still wired (a source check, not a server run — see
// the "wiring" section at the bottom for why).
//
// No JS test framework in this repo (see verify_math_rendering.js): plain
// asserts, run it directly:
//   node tests/verify_ask_stream_render.js
// The block between the "BEGIN"/"END" markers is sliced straight out of
// app.js and run against a tiny fake DOM, so it's the shipped code, not a
// copy that can drift.

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const appSrc = fs.readFileSync(path.join(ROOT, "web", "public", "app.js"), "utf8");
const serverSrc = fs.readFileSync(path.join(ROOT, "web", "server.js"), "utf8");
const cliSrc = fs.readFileSync(path.join(ROOT, "jarvis-cli", "jarvis", "cli.py"), "utf8");

const BEGIN = "// ---- §8 live streaming (K.3.1.5) BEGIN";
const END = "// ---- §8 live streaming (K.3.1.5) END";
const b = appSrc.indexOf(BEGIN);
const e = appSrc.indexOf(END);
if (b === -1 || e === -1 || e < b) {
  console.error("Could not find the live-streaming block in app.js — did it move?");
  process.exit(1);
}
const blockSrc = appSrc.slice(b, e);

let pass = 0;
let fail = 0;
function check(name, cond, detail) {
  if (cond) { pass += 1; console.log("ok      " + name); }
  else { fail += 1; console.log("FAILED  " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : "")); }
}

// ---------------------------------------------------------------------------
// A tiny fake DOM — just what the block touches.
// ---------------------------------------------------------------------------
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parent = null;
    this.className = ""; this.dataset = {}; this.attrs = {};
    this.hidden = false; this.open = false;
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
    this._text = ""; this._html = "";
    const self = this;
    this.classList = {
      add(c) { const s = new Set(self.className.split(/\s+/).filter(Boolean)); s.add(c); self.className = [...s].join(" "); },
      remove(c) { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  get textContent() { return this._text + this.children.map((c) => (c.isText ? c.value : c.textContent)).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; this._text = ""; }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    c.parent = this;
    if (i === -1) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; } }
  contains(n) { return n === this || this.children.some((c) => !c.isText && c.contains(n)); }
  descendants() { return this.children.filter((c) => !c.isText).flatMap((c) => [c, ...c.descendants()]); }
  querySelectorAll(sel) { return this.descendants().filter((n) => matches(n, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const matches = (n, sel) => sel.startsWith(".") && n.className.split(/\s+/).includes(sel.slice(1));
const textNode = (v) => ({ isText: true, value: v, parent: null });

function el(tag, attrs = {}, children = []) {
  const n = new FakeNode(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else n.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    n.appendChild(typeof c === "string" ? textNode(c) : c);
  }
  return n;
}
const qs = (sel, root) => root.querySelector(sel);

// ---------------------------------------------------------------------------
// Harness: one fresh world per test.
// ---------------------------------------------------------------------------
function makeWorld({ show = false, viewing = true } = {}) {
  const w = {};
  w.askThread = new FakeNode("div");
  w.state = { running: true, askPendingBubble: null, askReplyLines: [], askStream: null };
  w.thinkState = { level: "off", show, loaded: true };
  w.viewing = viewing;
  w.status = [];
  w.scrolls = 0;
  w.mdCalls = 0;
  w.richCalls = [];
  w.frames = [];

  const pending = el("div", { class: "ask-msg ask-msg--jarvis is-pending" }, [
    el("div", { class: "ask-msg__role" }, "Jarvis"),
    el("div", { class: "ask-msg__bubble" }, [el("span", { class: "ask-typing" })]),
  ]);
  w.askThread.appendChild(pending);
  w.state.askPendingBubble = pending;
  w.pendingBubble = qs(".ask-msg__bubble", pending);

  const deps = {
    el, qs,
    askThread: w.askThread,
    state: w.state,
    thinkState: w.thinkState,
    setAskStatus: (t) => w.status.push(t),
    isViewingAskThread: () => w.viewing,
    insertIntoAskThread: (msg) => {
      if (w.state.askPendingBubble && w.askThread.contains(w.state.askPendingBubble)) {
        w.askThread.insertBefore(msg, w.state.askPendingBubble);
      } else w.askThread.appendChild(msg);
    },
    // The block draws through app.js's single entry point, renderRich() (I-B10).
    // The stub records what it was asked to draw; renderRich itself (fence
    // closing, math, code-block chrome) lives outside this block.
    renderRich: (bubbleEl, t, opts) => {
      w.mdCalls += 1;
      w.richCalls.push({ text: t, opts: opts || {} });
      bubbleEl.innerHTML = "<md>" + t + "</md>";
    },
    addAskMsgActions: () => {},
    currentAssistantName: () => "Jarvis",
    askThreadScrollToEnd: () => { w.scrolls += 1; },
    requestAnimationFrame: (fn) => { w.frames.push(fn); return w.frames.length; },
    cancelAnimationFrame: (id) => { w.frames[id - 1] = null; },
  };
  const names = Object.keys(deps);
  const factory = new Function(...names, blockSrc + `
    return { balanceStreamingMarkdown, applyAskStreamEvent, takeAskStreamPartial,
             syncStreamThinkingVisibility, renderStreamText };`);
  w.api = factory(...names.map((n) => deps[n]));
  w.flush = () => { const fs_ = w.frames.splice(0); for (const f of fs_) if (f) f(); };
  w.ev = (k, extra = {}) => w.api.applyAskStreamEvent({ k, ...extra });
  w.children = () => w.askThread.children;
  w.thinkBlocks = () => w.askThread.querySelectorAll(".thinking-block--stream");
  w.interims = () => w.askThread.querySelectorAll(".ask-msg--interim");
  w.notes = () => w.askThread.querySelectorAll(".ask-stream-note");
  return w;
}

// ---------------------------------------------------------------------------
// balanceStreamingMarkdown (pure)
// ---------------------------------------------------------------------------
{
  const { api } = makeWorld();
  const bal = api.balanceStreamingMarkdown;
  check("plain text is untouched", bal("hello **world**") === "hello **world**");
  check("an unclosed ``` fence gets closed", bal("a\n```js\nconst x = 1;") === "a\n```js\nconst x = 1;\n```");
  check("a closed fence is left alone", bal("```\ncode\n```\nafter") === "```\ncode\n```\nafter");
  check("two fences, the second unclosed, closes only the second",
    bal("```\na\n```\ntext\n```py\nb") === "```\na\n```\ntext\n```py\nb\n```");
  check("~~~ fences are tracked separately from ```", bal("~~~\nx") === "~~~\nx\n~~~");
  check("a ``` line inside an open ~~~ fence does not close it", bal("~~~\n```\nx") === "~~~\n```\nx\n~~~");
  check("a longer fence needs an equally long closer",
    bal("````md\n```\ninner\n```\nmore") === "````md\n```\ninner\n```\nmore\n````");
  check("a fence line with an info string does not close an open fence",
    bal("```\nx\n```js").endsWith("\n```"));
  check("backticks mid-line are not a fence", bal("use ``` inline here") === "use ``` inline here");
  check("no double newline before the closer when text already ends in one", bal("```\nx\n") === "```\nx\n```");
  check("empty input is fine", bal("") === "");
}

// ---------------------------------------------------------------------------
// Text streaming
// ---------------------------------------------------------------------------
{
  const w = makeWorld();
  w.ev("text", { d: "Hel" }); w.ev("text", { d: "lo " }); w.ev("text", { d: "world" });
  check("no markdown render happens synchronously per delta", w.mdCalls === 0, w.mdCalls);
  check("exactly one frame is scheduled for three deltas", w.frames.length === 1, w.frames.length);
  w.flush();
  check("one render per frame, with everything so far", w.mdCalls === 1 && w.pendingBubble.innerHTML === "<md>Hello world</md>",
    [w.mdCalls, w.pendingBubble.innerHTML]);
  check("status line says writing…", w.status[w.status.length - 1] === "writing\u2026", w.status);
  w.ev("text", { d: "\n```js\nlet a" });
  w.flush();
  // renderRich(..., { final: false }) closes the open fence itself (and marks
  // that last block "writing…"); the block just has to hand it the real text
  // and flag the render as non-final, math-free.
  const last = w.richCalls[w.richCalls.length - 1];
  check("the preview render is non-final and hands over the real, unbalanced text",
    last.text === "Hello world\n```js\nlet a" && last.opts.final === false && last.opts.math === false, last);
  check("the stored text itself keeps the real, unbalanced content", w.state.askStream.text === "Hello world\n```js\nlet a");
}

// ---------------------------------------------------------------------------
// Thinking segments + the show/hide toggle
// ---------------------------------------------------------------------------
{
  const w = makeWorld({ show: false });
  w.ev("thinking", { d: "let me " }); w.ev("thinking", { d: "think" });
  const blocks = w.thinkBlocks();
  check("thinking deltas build ONE block", blocks.length === 1, blocks.length);
  check("the block sits above the pending bubble, in order",
    w.children().indexOf(blocks[0]) < w.children().indexOf(w.state.askPendingBubble));
  check("its body streams the raw thinking text", blocks[0].querySelector(".thinking-block__text").textContent === "let me think");
  check("with the toggle OFF it is hidden but still receiving text", blocks[0].hidden === true);
  check("status line says thinking… either way", w.status[w.status.length - 1] === "thinking\u2026");

  // turn the toggle ON mid-turn — instant, and reveals what already streamed
  w.thinkState.show = true;
  w.api.syncStreamThinkingVisibility();
  check("toggling ON mid-turn reveals the block, auto-expanded", blocks[0].hidden === false && blocks[0].open === true);
  check("…with everything streamed so far intact", blocks[0].querySelector(".thinking-block__text").textContent === "let me think");

  w.ev("text", { d: "answer" });
  const summary = blocks[0].querySelector(".thinking-block__summary").textContent;
  check("when the thinking segment ends the header becomes 'Thought for N s'", /^Thought for \d+ s$/.test(summary), summary);
  check("…and the block collapses", blocks[0].open === false);
  check("…and drops its live marker", !blocks[0].classList.contains("thinking-block--live"));
  check("a finished block stays visible while the toggle is ON", blocks[0].hidden === false);

  w.thinkState.show = false;
  w.api.syncStreamThinkingVisibility();
  check("toggling OFF hides finished blocks too (display switch only)", blocks[0].hidden === true);
}
{
  const w = makeWorld({ show: true });
  w.ev("thinking", { d: "x" });
  const blk = w.thinkBlocks()[0];
  check("with the toggle ON a live block starts visible and open", blk.hidden === false && blk.open === true);
  w.thinkState.show = false;
  w.api.syncStreamThinkingVisibility();
  check("turning OFF mid-stream hides the live block but keeps it live",
    blk.hidden === true && blk.classList.contains("thinking-block--live"));
}

// ---------------------------------------------------------------------------
// A tool round commits its text; the next round starts fresh
// ---------------------------------------------------------------------------
{
  const w = makeWorld();
  w.ev("thinking", { d: "plan" });
  w.ev("text", { d: "I'll check that now" });
  w.ev("tool", { name: "get_battery" });
  w.ev("round_end", { finish: "tool" });
  const interims = w.interims();
  check("text before a tool call is committed as its own bubble", interims.length === 1, interims.length);
  check("…with the exact text, as raw (for Copy)", interims[0].dataset.raw === "I'll check that now");
  check("…rendered as markdown", qs(".ask-msg__bubble", interims[0]).innerHTML === "<md>I'll check that now</md>");
  check("…above the pending bubble", w.children().indexOf(interims[0]) < w.children().indexOf(w.state.askPendingBubble));
  check("the pending bubble starts over for the next round", w.pendingBubble.children.length === 1 && w.pendingBubble.children[0].className === "ask-typing");
  check("status line names the tool", w.status[w.status.length - 1] === "running get_battery\u2026", w.status);
  check("the round's stream text is cleared", w.state.askStream.text === "");

  w.ev("text", { d: "It's 87%." });
  w.flush();
  check("next round's text goes to the pending bubble only", w.pendingBubble.innerHTML === "<md>It's 87%.</md>");
  check("the committed interim bubble is untouched", qs(".ask-msg__bubble", interims[0]).innerHTML === "<md>I'll check that now</md>");
  w.ev("round_end", { finish: "done" });
  check("round_end(done) does NOT commit — the final reply stays provisional in the pending bubble",
    w.interims().length === 1 && w.state.askStream.text === "It's 87%.");
}
{
  const w = makeWorld();
  w.ev("tool", { name: "x" });
  w.ev("round_end", { finish: "tool" });
  check("a tool round with no text commits nothing", w.interims().length === 0);
}

// ---------------------------------------------------------------------------
// reset (mid-stream failover)
// ---------------------------------------------------------------------------
{
  const w = makeWorld({ show: true });
  w.ev("text", { d: "first round, kept" });
  w.ev("round_end", { finish: "tool" });          // committed
  w.ev("thinking", { d: "round two thinking" });
  w.ev("text", { d: "half a sent" });
  w.flush();
  check("precondition: uncommitted output is on screen", w.pendingBubble.innerHTML.includes("half a sent") && w.thinkBlocks().length === 1);
  w.ev("reset");
  check("reset discards the uncommitted text", w.state.askStream.text === "" && w.pendingBubble.children[0].className === "ask-typing");
  check("reset removes the uncommitted thinking block", w.thinkBlocks().length === 0);
  check("reset leaves text committed by an earlier tool round alone", w.interims().length === 1);
  check("reset tells the user what happened", w.notes().length === 1 && /Switching provider/.test(w.notes()[0].textContent), w.notes().map((n) => n.textContent));
  w.ev("text", { d: "new provider's answer" });
  w.flush();
  check("the next attempt streams into a clean bubble", w.pendingBubble.innerHTML === "<md>new provider's answer</md>");
}
{
  const w = makeWorld();
  w.ev("reset");
  check("a reset with nothing streamed is silent (no stray note)", w.notes().length === 0);
}
{
  const w = makeWorld({ show: true });
  w.ev("thinking", { d: "committed thinking" });
  w.ev("round_end", { finish: "tool" });
  w.ev("reset");
  check("thinking committed with a tool round survives a later reset", w.thinkBlocks().length === 1);
}

// ---------------------------------------------------------------------------
// End of turn, Stop, and reconcile
// ---------------------------------------------------------------------------
{
  const w = makeWorld({ show: true });
  w.ev("thinking", { d: "pondering" });
  w.ev("text", { d: "partial reply" });
  w.ev("thinking", { d: "more" }); // a second thinking segment starts after text
  const blk = w.thinkBlocks()[1];
  const partial = w.api.takeAskStreamPartial();
  check("takeAskStreamPartial hands back text that was never superseded", partial === "partial reply", partial);
  check("it clears the stream state", w.state.askStream === null);
  check("it closes a thinking segment still open at Stop (no stuck 'Thinking…')",
    !blk.classList.contains("thinking-block--live") && /^Thought for/.test(blk.querySelector(".thinking-block__summary").textContent));
  check("calling it again is safe", w.api.takeAskStreamPartial() === "");
}
{
  const w = makeWorld();
  w.ev("text", { d: "streamed preview" });
  w.state.askReplyLines.push("Jarvis: final authoritative reply"); // the finished reply starts arriving
  w.flush();
  check("a frame that fires after the finished reply began does NOT paint over it",
    w.pendingBubble.innerHTML === "" || !w.pendingBubble.innerHTML.includes("streamed preview"), w.pendingBubble.innerHTML);
}
{
  const w = makeWorld();
  w.ev("text", { d: "to be cancelled" });
  w.api.takeAskStreamPartial();
  const before = w.mdCalls;
  w.flush();
  check("ending the turn cancels the pending frame", w.mdCalls === before);
}

// ---------------------------------------------------------------------------
// Robustness / scoping
// ---------------------------------------------------------------------------
{
  const w = makeWorld();
  w.state.running = false;
  w.ev("text", { d: "stray" });
  check("events after the turn ended are ignored", w.state.askStream === null);
}
{
  const w = makeWorld();
  w.state.askPendingBubble = null;
  w.ev("text", { d: "stray" });
  check("events with no bubble to land in are ignored", w.state.askStream === null);
}
{
  const w = makeWorld();
  let threw = false;
  try {
    w.api.applyAskStreamEvent(null);
    w.api.applyAskStreamEvent("nope");
    w.api.applyAskStreamEvent({ k: "from_the_future", d: "x" });
    w.api.applyAskStreamEvent({ k: "text" }); // no delta
  } catch (_) { threw = true; }
  check("malformed / unknown events never throw", threw === false);
  check("an unknown kind changes nothing", w.thinkBlocks().length === 0 && (!w.state.askStream || w.state.askStream.text === ""));
}
{
  const w = makeWorld({ viewing: false, show: true });
  w.ev("thinking", { d: "x" });
  w.ev("text", { d: "background reply" });
  w.ev("round_end", { finish: "tool" });
  w.flush();
  check("while the user is viewing another conversation nothing touches the thread DOM",
    w.thinkBlocks().length === 0 && w.interims().length === 0 && w.mdCalls === 0);
}
{
  // follow-the-stream only while at the bottom
  const w = makeWorld();
  w.askThread.scrollHeight = 1000; w.askThread.clientHeight = 300; w.askThread.scrollTop = 0; // scrolled up
  w.ev("text", { d: "hello" });
  w.flush();
  check("scrolled up: streaming does NOT yank the view down", w.scrolls === 0, w.scrolls);
  w.askThread.scrollTop = 700; // at the bottom
  w.ev("text", { d: " more" });
  w.flush();
  check("at the bottom: streaming follows", w.scrolls === 1, w.scrolls);
}

// ---------------------------------------------------------------------------
// Wiring (source checks) — the browser, server and CLI halves of the protocol
// ---------------------------------------------------------------------------
// server.js can't be started here (its npm deps aren't vendored), so its half
// is checked as source: a marker branch that whitelists the five kinds, and
// the env var that turns the CLI side on.
const has = (src, re) => re.test(src);
check("app.js: handleWsMessage routes ask-stream to applyAskStreamEvent",
  has(appSrc, /case "ask-stream":[\s\S]{0,400}applyAskStreamEvent\(msg\.ev\)/));
check("app.js: ask-start clears any stale stream state", has(appSrc, /case "ask-start":[\s\S]{0,200}state\.askStream = null/));
check("app.js: finalizeAskBubble collects the partial before anything else",
  has(appSrc, /function finalizeAskBubble\(overrideMessage\) \{\s*\/\/[^\n]*\n\s*const streamedPartial = takeAskStreamPartial\(\)/));
check("app.js: setThinkShow syncs live blocks both on set and on revert",
  (appSrc.match(/syncStreamThinkingVisibility\(\)/g) || []).length >= 3);
check("app.js: the repaint-after-switch path renders the stream preview",
  has(appSrc, /function rerenderAskPendingBubble\(\) \{[\s\S]{0,400}renderStreamText\(\)/));
check("server.js: has the JARVIS_STREAM marker branch forwarding ask-stream",
  has(serverSrc, /const STREAM_MARKER = "JARVIS_STREAM "/) && has(serverSrc, /type: "ask-stream", ev: out/));
check("server.js: whitelists exactly the five event kinds",
  has(serverSrc, /new Set\(\["text", "thinking", "tool", "round_end", "reset"\]\)/));
check("server.js: turns the CLI side on for web asks, with a JARVIS_WEB_STREAM=0 escape hatch",
  has(serverSrc, /JARVIS_WEB_STREAM !== "0"[\s\S]{0,120}JARVIS_STREAM_MARKERS = "1"/));
check("cli.py: only streams when server.js asked for it, and flushes before the reply",
  has(cliSrc, /JARVIS_STREAM_MARKERS"\) == "1"/) && has(cliSrc, /on_stream=stream_sink/) && has(cliSrc, /stream_sink\.close\(\)/));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
