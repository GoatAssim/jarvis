"""Streaming completion checks: terminal (TTY) sink + stream_options retry.

No network. Run: python3 tests/test_streaming_tty_and_options.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers as ap  # noqa: E402
from jarvis.stream_markers import TerminalStreamSink  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def sink(show=False):
    o, e = io.StringIO(), io.StringIO()
    return TerminalStreamSink(out=o, err=e, prefix="J: ", dim="<d>", reset="</d>", show_thinking=show), o, e


# plain reply
s, o, e = sink()
s("text", delta="Hel"); s("text", delta="lo"); s("round_end", finish="done"); s.close()
check("text streams with one prefix", o.getvalue() == "J: Hello\n", repr(o.getvalue()))
check("covers identical final", s.covers("Hello"))
check("covers ignores whitespace", s.covers("  Hello \n"))
check("does not cover different final", not s.covers("Hello there"))

# thinking hidden vs shown
s, o, e = sink(show=False)
s("thinking", delta="hmm"); s("text", delta="ok"); s("round_end", finish="done")
check("thinking hidden when show is off", e.getvalue() == "" and o.getvalue() == "J: ok\n")
s, o, e = sink(show=True)
s("thinking", delta="hmm\nmore"); s("text", delta="ok"); s("round_end", finish="done")
check("thinking dimmed on stderr when shown", "thinking: hmm more" in e.getvalue() and "<d>" in e.getvalue())
check("thinking never lands on stdout", "hmm" not in o.getvalue())

# tool round keeps interim text, final is last round only
s, o, e = sink()
s("text", delta="Checking."); s("tool", name="x"); s("round_end", finish="tool")
s("text", delta="Done."); s("round_end", finish="done"); s.close()
check("interim text stays on screen", o.getvalue() == "J: Checking.\nJ: Done.\n", repr(o.getvalue()))
check("final_text is last round only", s.final_text == "Done." and s.covers("Done."))

# reset on failover
s, o, e = sink()
s("text", delta="half a sen"); s("reset")
s("text", delta="Full answer."); s("round_end", finish="done")
check("reset notes on stderr", "restarting the reply" in e.getvalue())
check("after reset only new text is final", s.final_text == "Full answer.")
s, o, e = sink(); s("reset")
check("reset with nothing streamed is silent", e.getvalue() == "")

# broken stream never raises
class Boom:
    def write(self, *_): raise OSError("closed")
    def flush(self): pass
s = TerminalStreamSink(out=Boom(), err=Boom(), show_thinking=True)
try:
    s("text", delta="x"); s("thinking", delta="y"); s("round_end", finish="done"); s.close(); ok = True
except Exception:
    ok = False
check("sink swallows write errors", ok)

# stream_options rejection detector
for msg, want in [
    ("HTTP 400: Unrecognized request argument supplied: stream_options", True),
    ("HTTP 422: extra fields not permitted: stream_options", True),
    ("HTTP 400: 'include_usage' is not supported", True),
    ("HTTP 400: Tool choice is none, but model called a tool", False),
    ("invalid or unauthorized API key (HTTP 401)", False),
    (None, False),
]:
    check(f"stream_options detector: {msg!r}", ap.looks_like_stream_options_rejected(msg) is want)


# end to end: host rejects stream_options once, then streams
class Resp:
    def __init__(self, code, lines=(), text=""):
        self.status_code, self._lines, self.text, self.headers = code, list(lines), text, {}
    def iter_lines(self, decode_unicode=True):
        yield from self._lines
    def close(self): pass


import json  # noqa: E402
chunks = [
    'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
    'data: ' + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    'data: [DONE]',
]
sent, events = [], []
def fake_post_stream(url, headers, payload, timeout):
    sent.append(json.loads(json.dumps(payload)))
    if "stream_options" in payload:
        return Resp(400, text='{"error":"Unrecognized request argument supplied: stream_options"}'), None
    return Resp(200, chunks), None
ap._post_stream = fake_post_stream
ap.set_log_context("t", stream=True)
ap.set_stream_sink(lambda kind, **d: events.append((kind, d)))
r = ap.call_openai_compatible(
    {"base_url": "https://x.test/v1/chat/completions", "api_key": "k", "model": "m"},
    [{"role": "user", "content": "hi"}], 5)
ap.clear_log_context()
check("rejected stream_options is retried, not failed", r.ok and r.text == "Hi", getattr(r, "error", None))
check("two requests: with then without stream_options",
      len(sent) == 2 and "stream_options" in sent[0] and "stream_options" not in sent[1], sent)
check("retry still streams", sent[-1].get("stream") is True and any(k == "text" for k, _ in events))

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
