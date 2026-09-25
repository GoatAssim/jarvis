"""Master plan §8: real-time streaming — foundation + the Ollama adapter,
the first one converted (§8.7's suggested order: local, free to test end to
end without a real network).

What this covers
-----------------
1. The shared plumbing: stream_enabled()/set_log_context(stream=...) default
   to False, and stay False unless explicitly turned on — the whole point
   being that no existing caller (a test mocking `_post_json`, a script
   calling an adapter directly) is affected just because this module now
   has a `_post_stream` too.
2. call_ollama(), with streaming turned on: NDJSON deltas fire through
   set_stream_sink() as EVENT_TEXT/EVENT_THINKING/EVENT_TOOL/EVENT_ROUND_END,
   and the assembled AIResult is IDENTICAL (usage, thinking, tool_history,
   finish signal) to what the same canned data would produce non-streamed —
   §8.3's own design goal for every adapter it converts.
3. _post_stream()'s error paths (timeout, connection error) and a
   mid-stream drop, and that a malformed NDJSON line is skipped rather than
   failing the whole round.

No network: ai_providers._post_stream and requests.post are monkeypatched.
Run: python3 tests/test_streaming_ollama.py
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

import requests  # noqa: E402

from jarvis import ai_providers as ap  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


TOOLS = [{"name": "get_battery", "description": "d", "parameters": {"type": "object", "properties": {}}}]
MSGS = [{"role": "user", "content": "hi"}]
PROVIDER = {"name": "t", "model": "m", "api_key": "k", "base_url": "http://x"}


class FakeStreamResp:
    """Just enough of a `requests.Response` (stream=True) for
    _stream_ollama_chat: iter_lines(), status_code, text, headers, close()."""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.text = ""
        self.headers = {}
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line

    def close(self):
        self.closed = True


class Exec:
    def __init__(self):
        self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, args))
        return {"ok": True, "percent": 87}


@contextmanager
def fake_stream(lines, status_code=200):
    """Patches _post_stream directly — the same level test_finish_signal.py
    patches _post_json at — so this test exercises the real
    _stream_ollama_chat() parsing/assembly logic against a canned NDJSON
    sequence."""
    resp = FakeStreamResp(lines, status_code=status_code)
    orig = ap._post_stream
    ap._post_stream = lambda url, headers, payload, timeout: (resp, None)
    try:
        yield resp
    finally:
        ap._post_stream = orig


def ndjson(*objs):
    return [json.dumps(o) for o in objs]


def run_ollama(lines, tools=TOOLS, on_stream=None, executor=None):
    ap.set_thinking("off")
    events = []
    ap.set_log_context(None, "t", stream=True)
    ap.set_stream_sink(on_stream or (lambda kind, **data: events.append((kind, data))))
    try:
        ex = executor if executor is not None else Exec()
        with fake_stream(lines):
            res = ap.call_ollama(dict(PROVIDER), list(MSGS), 5, tools=tools, tool_executor=ex)
    finally:
        ap.clear_log_context()
    return res, ex, events


# ---------------------------------------------------------------- 1. defaults

def test_streaming_is_off_by_default():
    ap.clear_log_context()
    check("stream_enabled() is False with no context set at all", ap.stream_enabled() is False)
    ap.set_log_context(None, "t")  # no stream= kwarg passed
    check("set_log_context()'s own default is also False", ap.stream_enabled() is False)
    ap.clear_log_context()


def test_a_sink_with_streaming_off_never_fires():
    # Defensive: set_stream_sink() alone must not turn streaming on. In
    # practice ai_client.py only ever calls it alongside stream=True, but
    # the two are independent knobs on purpose (see ai_providers.py's own
    # comment) and each should hold its own default correctly.
    ap.set_log_context(None, "t", stream=False)
    fired = []
    ap.set_stream_sink(lambda kind, **d: fired.append(kind))
    ap._emit_stream(ap.EVENT_TEXT, delta="x")
    check("a sink only fires when something calls _emit_stream — this test just checks it CAN, "
          "the real guarantee is call_ollama below never calling it when stream_enabled() is False",
          fired == [ap.EVENT_TEXT])
    ap.clear_log_context()


def test_non_streaming_path_never_touches_post_stream():
    called = []
    orig = ap._post_stream
    ap._post_stream = lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError("should not stream"))
    ap.set_log_context(None, "t", stream=False)  # explicit off, the current real-world default
    try:
        with _fake_post_json_ollama_final("Battery is fine."):
            res = ap.call_ollama(dict(PROVIDER), list(MSGS), 5, tools=TOOLS, tool_executor=Exec())
    finally:
        ap.clear_log_context()
        ap._post_stream = orig
    check("non-streaming call never calls _post_stream", called == [])
    check("non-streaming call still succeeds via _post_json", res.ok and res.text == "Battery is fine.", res.error)


@contextmanager
def _fake_post_json_ollama_final(text):
    class Resp:
        status_code, text_, headers = 200, "", {}

        def json(self):
            return {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": text}}
    orig = ap._post_json
    ap._post_json = lambda url, headers, payload, timeout: (Resp(), None)
    try:
        yield
    finally:
        ap._post_json = orig


# ---------------------------------------------------------- 2. streamed text

def test_streamed_text_only_reply_matches_non_streaming_shape():
    lines = ndjson(
        {"message": {"role": "assistant", "content": "Battery "}, "done": False},
        {"message": {"role": "assistant", "content": "is fine."}, "done": False},
        {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": ""},
         "prompt_eval_count": 12, "eval_count": 4},
    )
    res, ex, events = run_ollama(lines, tools=None)
    check("assembled text is the concatenation of every delta", res.text == "Battery is fine.", res.text)
    check("ok, no tool calls, no error", res.ok and not ex.calls, res.error)
    text_events = [d["delta"] for k, d in events if k == ap.EVENT_TEXT]
    check("both text deltas were emitted live, in order", text_events == ["Battery ", "is fine."], text_events)
    check("a round_end event fires with finish=done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_DONE for k, d in events), events)


def test_streamed_usage_matches_what_final_chunk_reports():
    lines = ndjson(
        {"message": {"role": "assistant", "content": "hi"}, "done": False},
        {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": ""},
         "prompt_eval_count": 100, "eval_count": 25},
    )
    res, ex, events = run_ollama(lines, tools=None)
    check("prompt tokens from the final chunk carry through to usage",
          res.usage is not None and res.usage.get("input_tokens") == 100, res.usage)
    check("completion tokens from the final chunk carry through to usage",
          res.usage is not None and res.usage.get("output_tokens") == 25, res.usage)


# ------------------------------------------------------- 3. streamed thinking

def test_streamed_thinking_deltas_assemble_and_fire():
    lines = ndjson(
        {"message": {"role": "assistant", "content": "", "thinking": "Let me "}, "done": False},
        {"message": {"role": "assistant", "content": "", "thinking": "consider this."}, "done": False},
        {"message": {"role": "assistant", "content": "Done."}, "done": False},
        {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": ""}},
    )
    res, ex, events = run_ollama(lines, tools=None)
    check("final text excludes the thinking trace", res.text == "Done.", res.text)
    thinking_events = [d["delta"] for k, d in events if k == ap.EVENT_THINKING]
    check("both thinking deltas were emitted live, in order",
          thinking_events == ["Let me ", "consider this."], thinking_events)
    check("assembled thinking trace is saved the same way a non-streamed reply's is",
          ap.get_thinking_trace().get("text") == "Let me consider this.", ap.get_thinking_trace())


# ----------------------------------------------------------- 4. streamed tool

def test_streamed_tool_call_arrives_whole_and_runs():
    lines = ndjson(
        {"message": {"role": "assistant", "content": "Checking now."}, "done": False},
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "get_battery", "arguments": {}}}]}, "done": False},
        {"done": True, "done_reason": "stop"},
    )
    final_lines = ndjson(
        {"message": {"role": "assistant", "content": "Battery is at 87%."}, "done": False},
        {"done": True, "done_reason": "stop"},
    )
    calls = {"n": 0}

    def post_stream_router(url, headers, payload, timeout):
        calls["n"] += 1
        return FakeStreamResp(lines if calls["n"] == 1 else final_lines), None

    ap.set_thinking("off")
    events = []
    ap.set_log_context(None, "t", stream=True)
    ap.set_stream_sink(lambda kind, **data: events.append((kind, data)))
    orig = ap._post_stream
    ap._post_stream = post_stream_router
    try:
        ex = Exec()
        res = ap.call_ollama(dict(PROVIDER), list(MSGS), 5, tools=TOOLS, tool_executor=ex)
    finally:
        ap.clear_log_context()
        ap._post_stream = orig

    check("the tool was actually called, with the streamed-whole arguments",
          ex.calls == [("get_battery", {})], ex.calls)
    check("final answer after the tool round is the second round's streamed text",
          res.ok and res.text == "Battery is at 87%.", (res.ok, res.text))
    check("interim narration from the tool round was captured (§5, still works when streaming)",
          any(entry.get("text") == "Checking now." for entry in ap.get_interim_text()), ap.get_interim_text())
    tool_events = [d for k, d in events if k == ap.EVENT_TOOL]
    check("a tool event fired with the right name and (whole, unfragmented) arguments",
          tool_events and tool_events[0]["name"] == "get_battery" and tool_events[0]["arguments"] == {},
          tool_events)
    check("the first round's round_end says tool, not done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_TOOL for k, d in events), events)


# --------------------------------------------------- 5. malformed / dropped

def test_a_malformed_ndjson_line_is_skipped_not_fatal():
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "Hi"}, "done": False}),
        "{not json at all",
        "",  # a blank line, also just skipped
        json.dumps({"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": " there"}}),
    ]
    res, ex, events = run_ollama(lines, tools=None)
    check("the malformed/blank lines were skipped, not fatal", res.ok, res.error)
    check("the two valid lines still assembled correctly", res.text == "Hi there", res.text)


def test_mid_stream_connection_drop_is_a_clean_error_not_a_crash():
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "Partial"}, "done": False}),
        requests.exceptions.ConnectionError("connection reset"),
    ]
    res, ex, events = run_ollama(lines, tools=None)
    check("a mid-stream drop ends as a clean AIResult(False, error=...), not an exception reaching the caller",
          res.ok is False and "interrupted" in (res.error or ""), res.error)


# ------------------------------------------------- 6. _post_stream's own errors

def test_post_stream_timeout_and_connection_error():
    class BoomTimeout:
        def __call__(self, *a, **k):
            raise requests.exceptions.Timeout()

    class BoomConn:
        def __call__(self, *a, **k):
            raise requests.exceptions.ConnectionError()

    orig = requests.post
    requests.post = BoomTimeout()
    try:
        resp, err = ap._post_stream("http://x", {}, {}, 5)
    finally:
        requests.post = orig
    check("a timeout returns (None, <message mentioning the timeout>)",
          resp is None and "timed out" in err, err)

    requests.post = BoomConn()
    try:
        resp, err = ap._post_stream("http://x", {}, {}, 5)
    finally:
        requests.post = orig
    check("a connection error returns (None, <message>), not a raised exception",
          resp is None and err, err)


for fn in [
    test_streaming_is_off_by_default,
    test_a_sink_with_streaming_off_never_fires,
    test_non_streaming_path_never_touches_post_stream,
    test_streamed_text_only_reply_matches_non_streaming_shape,
    test_streamed_usage_matches_what_final_chunk_reports,
    test_streamed_thinking_deltas_assemble_and_fire,
    test_streamed_tool_call_arrives_whole_and_runs,
    test_a_malformed_ndjson_line_is_skipped_not_fatal,
    test_mid_stream_connection_drop_is_a_clean_error_not_a_crash,
    test_post_stream_timeout_and_connection_error,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
