"""L.40 -- "streaming only works with Groq".

No Groq-only gate exists in the code. What does exist are stream-parser gaps
that only bite hosts other than Groq, each of which ends in a confusing
"empty response" or a crash instead of the provider's real answer:

  * a host that fails AFTER sending 200 (OpenRouter, Gemini, Ollama) sends an
    `{"error": ...}` event the parsers skipped as an empty chunk;
  * OpenAI-compatible `content` as a list of typed parts (Mistral) raised
    TypeError; whole tool calls sharing one `index` (Gemini's OpenAI endpoint)
    were glued into one call with a joined name;
  * Ollama kept only the LAST line's tool_calls, so parallel calls sent on
    separate lines lost all but one.

Parsers are driven directly with recorded wire shapes; no network.
Run: python3 tests/test_l40_stream_parsers_other_hosts.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers as ap  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class FakeStreamResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.text = ""
        self.headers = {}
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


def sse(*objs):
    return [f"data: {json.dumps(o)}" for o in objs]


def ndjson(*objs):
    return [json.dumps(o) for o in objs]


def parse(fn, lines):
    events = []
    ap.set_log_context(None, "t", stream=True)
    ap.set_stream_sink(lambda kind, **data: events.append((kind, data)))
    try:
        data, err = fn(FakeStreamResp(lines), "http://x")
    finally:
        ap.clear_log_context()
    return data, err, events


# ---------------------------------------------------------------------------
# _stream_error_text
# ---------------------------------------------------------------------------
check("error text: nothing for a normal chunk and for a null error",
      ap._stream_error_text({"choices": []}) == "" and ap._stream_error_text({"error": None}) == ""
      and ap._stream_error_text("x") == "")
check("error text: an object error carries its message and code",
      "Provider disconnected" in ap._stream_error_text({"error": {"message": "Provider disconnected", "code": 502}})
      and "502" in ap._stream_error_text({"error": {"message": "Provider disconnected", "code": 502}}))
check("error text: a plain-string error (Ollama)", ap._stream_error_text({"error": "model crashed"}) == "model crashed")
check("error text: an {type: error} event", ap._stream_error_text({"type": "error", "message": "boom"}) == "boom")

# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------
data, err, events = parse(ap._stream_openai_compatible_chat, sse(
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"error": {"message": "Provider disconnected", "code": 502}},
))
check("openai: a mid-stream error event fails the round with the provider's own words",
      data is None and err and "Provider disconnected" in err and err.startswith("stream error"), (data, err))

data, err, events = parse(ap._stream_openai_compatible_chat, sse(
    {"choices": [{"delta": {"content": [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]}}]},
    {"choices": [{"delta": {"content": [{"type": "thinking",
                                          "thinking": [{"type": "text", "text": "hmm"}]}]}}]},
    {"choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}]},
))
msg = (data or {}).get("choices", [{}])[0].get("message", {})
check("openai: list-shaped content no longer raises and is assembled", err is None and msg.get("content") == "Hello!", (err, msg))
check("openai: a typed thinking part becomes the reasoning trace", msg.get("reasoning_content") == "hmm", msg)
check("openai: the text and thinking deltas were both streamed live",
      [d["delta"] for k, d in events if k == ap.EVENT_TEXT] == ["Hello", "!"]
      and [d["delta"] for k, d in events if k == ap.EVENT_THINKING] == ["hmm"], events)

data, err, events = parse(ap._stream_openai_compatible_chat, sse(
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "get_time", "arguments": "{}"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c2", "function": {"name": "get_battery", "arguments": "{\"a\": 1}"}}]},
                  "finish_reason": "tool_calls"}]},
))
calls = (data or {}).get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
check("openai: two whole calls on the same index stay two calls",
      [c["function"]["name"] for c in calls] == ["get_time", "get_battery"], calls)
check("openai: each keeps its own id and arguments",
      [c["id"] for c in calls] == ["c1", "c2"] and calls[1]["function"]["arguments"] == "{\"a\": 1}", calls)

data, err, events = parse(ap._stream_openai_compatible_chat, sse(
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "get_time", "arguments": ""}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"tz\":"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \"UTC\"}"}}]},
                  "finish_reason": "tool_calls"}]},
))
calls = (data or {}).get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
check("openai: ordinary fragments (id once, then arguments only) still make ONE call",
      len(calls) == 1 and calls[0]["function"]["arguments"] == "{\"tz\": \"UTC\"}", calls)

data, err, events = parse(ap._stream_openai_compatible_chat, sse(
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}},
        {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
))
calls = (data or {}).get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
check("openai: distinct indexes (the usual shape) are unchanged", [c["function"]["name"] for c in calls] == ["one", "two"], calls)

data, err, events = parse(ap._stream_openai_compatible_chat, sse(
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "a", "function": {"name": "one", "arguments": {"x": 1}}}]}, "finish_reason": "tool_calls"}]},
))
calls = (data or {}).get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
check("openai: arguments sent as an object by a proxy are serialised, not a crash",
      err is None and json.loads(calls[0]["function"]["arguments"]) == {"x": 1}, (err, calls))

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
data, err, events = parse(ap._stream_ollama_chat, ndjson(
    {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "one", "arguments": {}}}]}, "done": False},
    {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "two", "arguments": {}}}]}, "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
))
names = [c["function"]["name"] for c in (data or {}).get("message", {}).get("tool_calls", [])]
check("ollama: parallel calls sent on separate lines are ALL kept", names == ["one", "two"], (names, err))
check("ollama: each was reported live", [d["name"] for k, d in events if k == ap.EVENT_TOOL] == ["one", "two"], events)

data, err, events = parse(ap._stream_ollama_chat, ndjson(
    {"message": {"role": "assistant", "content": "Hi"}, "done": False},
    {"error": "llama runner process has terminated"},
))
check("ollama: a mid-stream error line fails the round with Ollama's own words",
      data is None and err and "llama runner process has terminated" in err, (data, err))

# ---------------------------------------------------------------------------
# Gemini / Cohere
# ---------------------------------------------------------------------------
data, err, events = parse(ap._stream_gemini_chat, sse(
    {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]},
    {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}},
))
check("gemini: a mid-stream error event fails the round with Google's own words",
      data is None and err and "overloaded" in err, (data, err))

data, err, events = parse(ap._stream_gemini_chat, sse(
    {"candidates": [{"content": {"parts": [{"thought": True, "text": "thinking"}, {"text": "Hi"}]}}]},
    {"candidates": [{"content": {"parts": [{"text": " there"}]}, "finishReason": "STOP"}]},
))
parts = (data or {}).get("candidates", [{}])[0].get("content", {}).get("parts", [])
check("gemini: a normal stream is unchanged (thought part + answer part)",
      err is None and parts[0] == {"thought": True, "text": "thinking"} and parts[1] == {"text": "Hi there"}, (err, parts))

data, err, events = parse(ap._stream_cohere_chat, sse(
    {"type": "content-delta", "delta": {"message": {"content": {"text": "Hi"}}}},
    {"type": "error", "message": "rate limited"},
))
check("cohere: an error event fails the round", data is None and err and "rate limited" in err, (data, err))

data, err, events = parse(ap._stream_cohere_chat, sse(
    {"type": "content-delta", "delta": {"message": {"content": {"text": "Hi"}}}},
    {"type": "message-end", "delta": {"finish_reason": "COMPLETE", "usage": {"tokens": {"input_tokens": 1}}}},
))
check("cohere: a normal stream is unchanged",
      err is None and data["message"]["content"][0]["text"] == "Hi" and data["finish_reason"] == "COMPLETE", (err, data))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
