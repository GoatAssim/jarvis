"""Master plan §8 / K.3.1.4 — the CLI half of web streaming.

Covers jarvis/stream_markers.py (the `JARVIS_STREAM {json}` marker-line sink
cli.py hands to ai_client.ask(on_stream=...) when web/server.js asks for it)
and the two ai_client.ask() changes that make it usable:

  * passing on_stream= opts the ask() into the streaming transport, with an
    explicit `defaults.stream: false` still forcing the blocking path;
  * EVENT_RESET fires when an attempt fails (or its finished reply is
    rejected) after uncommitted text/thinking already streamed — never for
    text streamed before a tool call, which is a kept interim message (§8.4).

No network: the provider adapter is a fake that calls ai_providers._emit_stream
the way every real adapter does.
Run: python3 tests/test_stream_markers.py
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_config, ai_providers, conversations, logs  # noqa: E402
from jarvis.stream_markers import MARKER, StreamMarkerSink  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def parse(buf):
    """The marker lines a sink wrote, as dicts. Asserts the wire contract on
    the way: every line is one physical line starting with the marker."""
    out = []
    for line in buf.getvalue().split("\n"):
        if not line:
            continue
        assert line.startswith(MARKER), line
        out.append(json.loads(line[len(MARKER):]))
    return out


# ---------------------------------------------------------------------------
# StreamMarkerSink
# ---------------------------------------------------------------------------

def test_sink_coalesces_deltas_into_one_line():
    buf = io.StringIO()
    sink = StreamMarkerSink(out=buf, coalesce_seconds=5)  # never fires on its own here
    for piece in ["Hel", "lo ", "wor", "ld"]:
        sink("text", delta=piece)
    check("nothing is written while a delta is still coalescing", buf.getvalue() == "")
    sink.close()
    evs = parse(buf)
    check("one line carries the whole coalesced delta", evs == [{"k": "text", "d": "Hello world"}], evs)


def test_sink_timer_flushes_without_a_following_event():
    buf = io.StringIO()
    sink = StreamMarkerSink(out=buf, coalesce_seconds=0.03)
    sink("text", delta="slow model, no more tokens for a while")
    deadline = time.time() + 2
    while not buf.getvalue() and time.time() < deadline:
        time.sleep(0.01)
    evs = parse(buf)
    check("a held delta is flushed by the timer, not stuck until the next event",
          evs == [{"k": "text", "d": "slow model, no more tokens for a while"}], evs)
    sink.close()


def test_sink_never_merges_across_kinds_and_keeps_order():
    buf = io.StringIO()
    sink = StreamMarkerSink(out=buf, coalesce_seconds=5)
    sink("thinking", delta="let me ")
    sink("thinking", delta="think")
    sink("text", delta="answer")
    sink("tool", name="get_battery", arguments={"a": 1})
    sink("round_end", finish="tool")
    sink("text", delta="done")
    sink("round_end", finish="done")
    sink.close()
    evs = parse(buf)
    expect = [
        {"k": "thinking", "d": "let me think"},
        {"k": "text", "d": "answer"},
        {"k": "tool", "name": "get_battery"},
        {"k": "round_end", "finish": "tool"},
        {"k": "text", "d": "done"},
        {"k": "round_end", "finish": "done"},
    ]
    check("thinking -> text -> tool -> round_end keeps order and never merges kinds", evs == expect, evs)
    check("tool events carry the name only, never partial arguments",
          all("arguments" not in e for e in evs))


def test_sink_escapes_newlines_so_an_event_is_one_physical_line():
    buf = io.StringIO()
    sink = StreamMarkerSink(out=buf, coalesce_seconds=5)
    sink("text", delta="line one\nline two\r\n```py\nprint('é — ✓')\n```")
    sink.close()
    raw = buf.getvalue()
    check("exactly one physical line for a multi-line delta", raw.count("\n") == 1, repr(raw))
    check("the delta round-trips intact, unicode included",
          parse(buf)[0]["d"] == "line one\nline two\r\n```py\nprint('é — ✓')\n```")


def test_sink_reset_flushes_pending_text_first_then_resets():
    buf = io.StringIO()
    sink = StreamMarkerSink(out=buf, coalesce_seconds=5)
    sink("text", delta="half a sent")
    sink("reset")
    sink.close()
    check("pending text is emitted before the reset so the UI can discard it",
          parse(buf) == [{"k": "text", "d": "half a sent"}, {"k": "reset"}], parse(buf))


def test_sink_ignores_empty_deltas_and_unknown_kinds():
    buf = io.StringIO()
    sink = StreamMarkerSink(out=buf, coalesce_seconds=5)
    sink("text", delta="")
    sink("text")
    sink("something_new", delta="x")
    sink.close()
    check("empty deltas and unknown kinds write nothing", buf.getvalue() == "", buf.getvalue())


def test_sink_never_raises_on_a_broken_pipe():
    class Broken:
        def write(self, _):
            raise BrokenPipeError("web server went away")

        def flush(self):
            raise BrokenPipeError("web server went away")

    sink = StreamMarkerSink(out=Broken(), coalesce_seconds=0.01)
    ok = True
    try:
        sink("text", delta="hi")
        sink("tool", name="x")
        sink("round_end", finish="done")
        time.sleep(0.05)
        sink.close()
    except Exception as e:  # noqa: BLE001
        ok = False
        print("raised:", e)
    check("a dead stdout never takes down the provider attempt", ok)


# ---------------------------------------------------------------------------
# ai_client.ask(): gating + reset
# ---------------------------------------------------------------------------

@contextmanager
def _isolated_ask_env():
    orig = {
        "conv_dir": conversations.JARVIS_DIR, "conv_conv_dir": conversations.CONV_DIR,
        "conv_index": conversations.INDEX_FILE, "conv_current": conversations.CURRENT_FILE,
        "logs_dir": logs.JARVIS_DIR, "logs_log_dir": logs.LOG_DIR,
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR = tmp
        logs.LOG_DIR = tmp / "logs"
        try:
            yield conversations.get_current_id()
        finally:
            conversations.JARVIS_DIR = orig["conv_dir"]
            conversations.CONV_DIR = orig["conv_conv_dir"]
            conversations.INDEX_FILE = orig["conv_index"]
            conversations.CURRENT_FILE = orig["conv_current"]
            logs.JARVIS_DIR = orig["logs_dir"]
            logs.LOG_DIR = orig["logs_log_dir"]


@contextmanager
def _fake_providers(script, stream_default="unset"):
    """Two fake providers. `script` is a list of per-attempt callables
    (one per adapter call, in order); each gets no args, may call
    ai_providers._emit_stream(...) and returns an AIResult. Records, per
    call, what ai_providers.stream_enabled() said."""
    orig_load = ai_config.load_ai_config
    orig_adapters = dict(ai_providers.ADAPTERS)
    seen_enabled = []
    calls = {"n": 0}

    defaults = {"tools_enabled": False, "prompt_mode": "full"}
    if stream_default != "unset":
        defaults["stream"] = stream_default
    cfg = {
        "persona": {},
        "providers": [
            {"name": "fakeprov1", "type": "fake", "enabled": True, "api_keys": ["k1"], "model": "m"},
            {"name": "fakeprov2", "type": "fake", "enabled": True, "api_keys": ["k2"], "model": "m"},
        ],
        "defaults": defaults,
    }

    def fake_adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        seen_enabled.append(ai_providers.stream_enabled())
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return step()

    ai_config.load_ai_config = lambda: cfg
    ai_providers.ADAPTERS["fake"] = fake_adapter
    try:
        yield seen_enabled
    finally:
        ai_config.load_ai_config = orig_load
        ai_providers.ADAPTERS.clear()
        ai_providers.ADAPTERS.update(orig_adapters)


def _ok(text):
    return ai_providers.AIResult(True, text=text)


def _fail(msg):
    return ai_providers.AIResult(False, error=msg)


def test_passing_on_stream_opts_in_and_forwards_events():
    got = []

    def step():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="hi")
        ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish="done")
        return _ok("hi")

    with _isolated_ask_env() as conv, _fake_providers([step]) as seen:
        r = ai_client.ask("hello", commands=[], conversation_id=conv,
                          on_stream=lambda kind, **d: got.append((kind, d)))
    check("ask() still succeeds", r.ok is True, getattr(r, "error", ""))
    check("the adapter saw stream_enabled() == True with no defaults.stream set", seen == [True], seen)
    check("events reach the caller's callback",
          got == [("text", {"delta": "hi"}), ("round_end", {"finish": "done"})], got)


def test_without_on_stream_the_blocking_path_is_unchanged():
    def step():
        return _ok("hi")

    with _isolated_ask_env() as conv, _fake_providers([step]) as seen:
        ai_client.ask("hello", commands=[], conversation_id=conv)
    check("no on_stream and no defaults.stream -> adapters stay on the blocking transport",
          seen == [False], seen)


def test_defaults_stream_false_overrides_on_stream():
    got = []

    def step():
        return _ok("hi")

    with _isolated_ask_env() as conv, _fake_providers([step], stream_default=False) as seen:
        r = ai_client.ask("hello", commands=[], conversation_id=conv,
                          on_stream=lambda kind, **d: got.append(kind))
    check("explicit defaults.stream=false forces the blocking path even with on_stream", seen == [False], seen)
    check("and the ask still completes normally", r.ok is True)


def test_defaults_stream_true_still_streams_with_nobody_listening():
    def step():
        return _ok("hi")

    with _isolated_ask_env() as conv, _fake_providers([step], stream_default=True) as seen:
        ai_client.ask("hello", commands=[], conversation_id=conv)
    check("defaults.stream=true keeps its old meaning (stream, no sink)", seen == [True], seen)


def test_reset_fires_when_an_attempt_fails_mid_round():
    got = []

    def failing():
        ai_providers._emit_stream(ai_providers.EVENT_THINKING, delta="hmm")
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="half a sent")
        return _fail("connection dropped mid-stream")

    def good():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="full answer")
        ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish="done")
        return _ok("full answer")

    with _isolated_ask_env() as conv, _fake_providers([failing, good]):
        r = ai_client.ask("hello", commands=[], conversation_id=conv,
                          on_stream=lambda kind, **d: got.append(kind))
    check("ask() fails over to the second provider and succeeds", r.ok is True, getattr(r, "error", ""))
    check("a reset sits between the failed partial output and the next attempt's output",
          got == ["thinking", "text", "reset", "text", "round_end"], got)


def test_reset_does_not_touch_text_committed_before_a_tool_call():
    got = []

    def failing_after_tool_round():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="I'll check that now")
        ai_providers._emit_stream(ai_providers.EVENT_TOOL, name="get_battery")
        ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish="tool")
        # the tool ran; the NEXT round dies before it streams anything
        return _fail("503 from provider")

    def good():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="87%")
        ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish="done")
        return _ok("87%")

    with _isolated_ask_env() as conv, _fake_providers([failing_after_tool_round, good]):
        ai_client.ask("battery?", commands=[], conversation_id=conv,
                      on_stream=lambda kind, **d: got.append(kind))
    check("no reset when everything streamed was already committed by a tool round",
          "reset" not in got, got)


def test_reset_fires_for_a_finished_reply_rejected_after_the_fact():
    got = []
    echo = '[called get_battery with {"x": 1}]'

    def echoes_traces():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta=echo)
        ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish="done")
        return _ok(echo)

    def good():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="87%")
        ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish="done")
        return _ok("87%")

    check("fixture is something ask()'s post-hoc check really rejects",
          ai_client._is_tool_trace_reply(echo) is True)
    with _isolated_ask_env() as conv, _fake_providers([echoes_traces, good]):
        r = ai_client.ask("battery?", commands=[], conversation_id=conv,
                          on_stream=lambda kind, **d: got.append(kind))
    check("a finished round (round_end done) stays provisional, so rejecting it resets it",
          got == ["text", "round_end", "reset", "text", "round_end"] and r.ok is True, got)


def test_a_failure_before_any_output_emits_no_reset():
    got = []

    def dead_key():
        return _fail("401 bad key")

    def good():
        ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="ok")
        return _ok("ok")

    with _isolated_ask_env() as conv, _fake_providers([dead_key, good]):
        ai_client.ask("hi", commands=[], conversation_id=conv,
                      on_stream=lambda kind, **d: got.append(kind))
    check("nothing streamed, nothing to discard -> no reset noise", got == ["text"], got)


for fn in [
    test_sink_coalesces_deltas_into_one_line,
    test_sink_timer_flushes_without_a_following_event,
    test_sink_never_merges_across_kinds_and_keeps_order,
    test_sink_escapes_newlines_so_an_event_is_one_physical_line,
    test_sink_reset_flushes_pending_text_first_then_resets,
    test_sink_ignores_empty_deltas_and_unknown_kinds,
    test_sink_never_raises_on_a_broken_pipe,
    test_passing_on_stream_opts_in_and_forwards_events,
    test_without_on_stream_the_blocking_path_is_unchanged,
    test_defaults_stream_false_overrides_on_stream,
    test_defaults_stream_true_still_streams_with_nobody_listening,
    test_reset_fires_when_an_attempt_fails_mid_round,
    test_reset_does_not_touch_text_committed_before_a_tool_call,
    test_reset_fires_for_a_finished_reply_rejected_after_the_fact,
    test_a_failure_before_any_output_emits_no_reset,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
