"""Master plan §8.2 item 5 / K.3.1.4 — the CLI half of web streaming.

`ai_client.ask(on_stream=...)` already reports provider-neutral stream
events (`ai_providers.EVENT_*`) as an adapter parses them. This module turns
those into `JARVIS_STREAM {json}` marker lines on stdout — the same
marker-on-stdout protocol `JARVIS_CONFIRM_REQUEST` / `JARVIS_USAGE` already
use — which `web/server.js` peels off in its ask handler and forwards to the
browser as `ask-stream` WebSocket messages.

Wire shape, one physical line per event (json.dumps escapes every newline
inside a delta, so server.js's line buffer can never split an event):

    JARVIS_STREAM {"k":"text","d":"..."}        text delta (coalesced)
    JARVIS_STREAM {"k":"thinking","d":"..."}    thinking delta (coalesced)
    JARVIS_STREAM {"k":"tool","name":"..."}     a tool call is starting
    JARVIS_STREAM {"k":"round_end","finish":"tool"|"done"}
    JARVIS_STREAM {"k":"reset"}                 discard this attempt's
                                                uncommitted partial output

Deltas are coalesced (~40 ms) instead of one line per token: a fast local
model emits hundreds of tokens a second, and a WebSocket message plus a DOM
update per token buys nothing the eye can see. Every non-delta event flushes
the pending delta first, so ordering across kinds is always preserved.

Nothing here prints the final assembled reply — cli.py still does that
exactly as before, so the exit/persist path (and the browser's "final reply
is authoritative" reconcile) is untouched.
"""
import json
import sys
import threading

MARKER = "JARVIS_STREAM "

# How long a delta may sit in the buffer before it's flushed anyway. Short
# enough that streaming still reads as word-by-word, long enough to batch a
# burst of tokens into one line.
COALESCE_SECONDS = 0.04


class StreamMarkerSink:
    """Callable with ai_client.ask()'s `on_stream(kind, **data)` contract.

    Never raises: a broken pipe (the web server went away mid-turn) must not
    take down the provider attempt, same philosophy as
    ai_providers._emit_stream's own guard around the sink call.
    """

    def __init__(self, out=None, coalesce_seconds=COALESCE_SECONDS):
        self._out = out if out is not None else sys.stdout
        self._coalesce = coalesce_seconds
        self._lock = threading.Lock()
        self._kind = None      # "text" / "thinking" while a delta is buffered
        self._buf = []
        self._timer = None

    # -- internals (callers hold self._lock) ---------------------------------

    def _write(self, payload):
        try:
            self._out.write(MARKER + json.dumps(payload, ensure_ascii=False) + "\n")
            self._out.flush()
        except Exception:  # noqa: BLE001 — a stream hook must never break the turn
            pass

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _flush_locked(self):
        self._cancel_timer()
        if self._kind and self._buf:
            self._write({"k": self._kind, "d": "".join(self._buf)})
        self._kind = None
        self._buf = []

    def _timer_fired(self):
        with self._lock:
            self._timer = None
            self._flush_locked()

    # -- public ---------------------------------------------------------------

    def __call__(self, kind, **data):
        try:
            with self._lock:
                if kind in ("text", "thinking"):
                    delta = data.get("delta")
                    if not delta:
                        return
                    # A change of kind (thinking -> text) must not be merged
                    # into one line, or the UI would lose where one segment
                    # ended and the next began.
                    if self._kind is not None and self._kind != kind:
                        self._flush_locked()
                    self._kind = kind
                    self._buf.append(str(delta))
                    if self._timer is None:
                        self._timer = threading.Timer(self._coalesce, self._timer_fired)
                        self._timer.daemon = True
                        self._timer.start()
                    return
                self._flush_locked()
                if kind == "tool":
                    # Name only. Arguments are fragmented partial JSON on
                    # most adapters and are already shown, complete, by the
                    # existing stderr trace once the call actually runs
                    # (§8.3: never show arguments before they parse).
                    self._write({"k": "tool", "name": str(data.get("name") or "")})
                elif kind == "round_end":
                    self._write({"k": "round_end", "finish": str(data.get("finish") or "done")})
                elif kind == "reset":
                    self._write({"k": "reset"})
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        """Flush whatever is still buffered. Call once the ask() returns."""
        try:
            with self._lock:
                self._flush_locked()
        except Exception:  # noqa: BLE001
            pass


class TerminalStreamSink:
    """Plain-terminal (TTY) half of master plan 8.2 item 5.

    Same `on_stream(kind, **data)` contract as StreamMarkerSink, but instead
    of marker lines it prints for a human at a terminal: text deltas straight
    to stdout as they arrive, thinking deltas dimmed on stderr only when the
    show-thinking setting is on (reasoning `show`, the same setting as
    `jarvis think`).

    Text that streams before a tool call is a real interim message (8.4), so
    it stays on screen; the line is simply closed when the round ends. A
    `reset` (mid-stream failover, 8.6) can't un-print a terminal, so it ends
    the line and notes on stderr that the reply is restarting.

    cli.py still reconciles at the end: if `covers(final_text)` the streamed
    text already IS the reply and only a trailing newline is needed;
    otherwise the authoritative final reply is printed as before, so what is
    displayed and what is saved can never silently drift.
    """

    def __init__(self, out=None, err=None, prefix="", dim="", reset="", show_thinking=False):
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr
        self._prefix = prefix
        self._dim = dim
        self._reset = reset
        self._show_thinking = bool(show_thinking)
        self._text_open = False      # a stdout line is mid-sentence
        self._thinking_open = False  # a stderr line is mid-sentence
        self._segment = []           # text of the round currently streaming
        self.final_text = ""         # text of the last round that ended "done"
        self.streamed_any = False

    # -- internals -------------------------------------------------------------

    def _w(self, stream, s):
        try:
            stream.write(s)
            stream.flush()
        except Exception:  # noqa: BLE001 — a stream hook must never break the turn
            pass

    def _close_thinking(self):
        if self._thinking_open:
            self._w(self._err, self._reset + "\n")
            self._thinking_open = False

    def _close_text(self):
        if self._text_open:
            self._w(self._out, "\n")
            self._text_open = False

    # -- public ----------------------------------------------------------------

    def __call__(self, kind, **data):
        try:
            if kind == "text":
                delta = data.get("delta")
                if not delta:
                    return
                self._close_thinking()
                if not self._text_open:
                    self._w(self._out, self._prefix)
                    self._text_open = True
                self._w(self._out, str(delta))
                self._segment.append(str(delta))
                self.streamed_any = True
            elif kind == "thinking":
                delta = data.get("delta")
                if not delta or not self._show_thinking:
                    return
                self._close_text()
                if not self._thinking_open:
                    self._w(self._err, self._dim + "  thinking: ")
                    self._thinking_open = True
                self._w(self._err, str(delta).replace("\n", " "))
            elif kind == "tool":
                self._close_thinking()
                self._close_text()
            elif kind == "round_end":
                self._close_thinking()
                self._close_text()
                text = "".join(self._segment)
                self._segment = []
                if str(data.get("finish") or "done") == "done":
                    self.final_text = text
                else:
                    self.final_text = ""
            elif kind == "reset":
                self._close_thinking()
                had_text = self._text_open or bool(self._segment)
                self._close_text()
                self._segment = []
                self.final_text = ""
                if had_text:
                    self._w(self._err, f"{self._dim}  \u21ba switching provider \u2014 restarting the reply{self._reset}\n")
        except Exception:  # noqa: BLE001
            pass

    def covers(self, final_text):
        """True when what was streamed is the whole final reply (compared
        ignoring whitespace differences), so it must not be printed again."""
        norm = lambda s: " ".join(str(s or "").split())  # noqa: E731
        return bool(self.final_text) and norm(self.final_text) == norm(final_text)

    def close(self):
        try:
            self._close_thinking()
            self._close_text()
        except Exception:  # noqa: BLE001
            pass
