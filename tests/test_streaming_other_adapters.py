"""Master plan \u00a78: real-time streaming \u2014 the four adapters converted after
Ollama (\u00a78.7's order: openai_compatible, anthropic, gemini, cohere).

Mirrors tests/test_streaming_ollama.py's own coverage and style for each
adapter: text assembly, usage, tool-call assembly (fragmented, unlike
Ollama's whole-line calls \u2014 the one thing genuinely different per adapter),
a malformed/dropped SSE line, and a mid-stream connection drop. Cohere has
no thinking coverage on purpose \u2014 see CAPABILITIES; it doesn't have a
reasoning-model member yet, so call_cohere's non-streamed path never calls
_collect_thinking either.

No network: ai_providers._post_stream and requests.post are monkeypatched.
Run: python3 tests/test_streaming_other_adapters.py
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


class Exec:
    def __init__(self):
        self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, args))
        return {"ok": True, "percent": 87}


class FakeStreamResp:
    """Just enough of a `requests.Response` (stream=True) for the various
    _stream_<provider>_chat parsers: iter_lines(), status_code, text,
    headers, close()."""

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


def sse(*objs):
    """SSE `data: {...}` lines, one per object, matching what every one of
    these four adapters' parsers actually reads (they all skip non-`data:`
    lines, so the paired `event: <type>` line each real API also sends
    doesn't need to be reproduced here)."""
    return [f"data: {json.dumps(o)}" for o in objs]


def _run(call_fn, provider, lines, tools=TOOLS, executor=None):
    ap.set_thinking("off")
    events = []
    ap.set_log_context(None, "t", stream=True)
    ap.set_stream_sink(lambda kind, **data: events.append((kind, data)))
    orig = ap._post_stream
    ap._post_stream = lambda url, headers, payload, timeout: (FakeStreamResp(lines), None)
    try:
        ex = executor if executor is not None else Exec()
        res = call_fn(dict(provider), list(MSGS), 5, tools=tools, tool_executor=ex)
    finally:
        ap.clear_log_context()
        ap._post_stream = orig
    return res, ex, events


def _run_with_router(call_fn, provider, router, tools=TOOLS, executor=None):
    """Like _run, but _post_stream is a caller-supplied router \u2014 needed for
    the multi-round (tool-call-then-final-answer) tests, same idea as
    test_streaming_ollama.py's post_stream_router."""
    ap.set_thinking("off")
    events = []
    ap.set_log_context(None, "t", stream=True)
    ap.set_stream_sink(lambda kind, **data: events.append((kind, data)))
    orig = ap._post_stream
    ap._post_stream = router
    try:
        ex = executor if executor is not None else Exec()
        res = call_fn(dict(provider), list(MSGS), 5, tools=tools, tool_executor=ex)
    finally:
        ap.clear_log_context()
        ap._post_stream = orig
    return res, ex, events


# ============================================================== openai_compatible

OAI_PROVIDER = {"name": "t", "model": "m", "api_key": "k", "base_url": "http://x"}


def test_oai_text_and_usage_match_non_streaming_shape():
    lines = sse(
        {"id": "r1", "choices": [{"delta": {"content": "Battery "}}]},
        {"choices": [{"delta": {"content": "is fine."}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4}},
    ) + ["data: [DONE]"]
    res, ex, events = _run(ap.call_openai_compatible, OAI_PROVIDER, lines, tools=None)
    check("oai: assembled text is the concatenation of every delta", res.text == "Battery is fine.", res.text)
    check("oai: ok, no tool calls, no error", res.ok and not ex.calls, res.error)
    text_events = [d["delta"] for k, d in events if k == ap.EVENT_TEXT]
    check("oai: both text deltas were emitted live, in order", text_events == ["Battery ", "is fine."], text_events)
    check("oai: usage from the trailing include_usage chunk carries through",
          res.usage is not None and res.usage.get("input_tokens") == 12 and res.usage.get("output_tokens") == 4,
          res.usage)
    check("oai: a round_end event fires with finish=done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_DONE for k, d in events), events)


def test_oai_thinking_deltas_assemble_and_fire():
    lines = sse(
        {"choices": [{"delta": {"reasoning_content": "Let me "}}]},
        {"choices": [{"delta": {"reasoning_content": "consider this."}}]},
        {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]},
    )
    res, ex, events = _run(ap.call_openai_compatible, OAI_PROVIDER, lines, tools=None)
    check("oai: final text excludes the reasoning trace", res.text == "Done.", res.text)
    thinking_events = [d["delta"] for k, d in events if k == ap.EVENT_THINKING]
    check("oai: both reasoning deltas were emitted live, in order",
          thinking_events == ["Let me ", "consider this."], thinking_events)
    check("oai: assembled thinking trace is saved the same way a non-streamed reply's is",
          ap.get_thinking_trace().get("text") == "Let me consider this.", ap.get_thinking_trace())


def test_oai_fragmented_tool_call_assembles_and_runs():
    round1 = sse(
        {"choices": [{"delta": {"content": "Checking now."}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "get_battery", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"unit\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"pct\"}"}}]}, "finish_reason": "tool_calls"}]},
    )
    round2 = sse({"choices": [{"delta": {"content": "Battery is at 87%."}, "finish_reason": "stop"}]})
    calls = {"n": 0}

    def router(url, headers, payload, timeout):
        calls["n"] += 1
        return FakeStreamResp(round1 if calls["n"] == 1 else round2), None

    res, ex, events = _run_with_router(ap.call_openai_compatible, OAI_PROVIDER, router)
    check("oai: the tool was called with the fully-reassembled arguments",
          ex.calls == [("get_battery", {"unit": "pct"})], ex.calls)
    check("oai: final answer after the tool round is the second round's streamed text",
          res.ok and res.text == "Battery is at 87%.", (res.ok, res.text))
    tool_events = [d for k, d in events if k == ap.EVENT_TOOL]
    check("oai: a tool event fired only once fragments were complete",
          tool_events and tool_events[0]["name"] == "get_battery", tool_events)
    check("oai: the first round's round_end says tool, not done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_TOOL for k, d in events), events)


def test_oai_malformed_line_skipped_and_mid_stream_drop_is_clean():
    lines = sse({"choices": [{"delta": {"content": "Hi"}}]}) + [
        "data: {not json at all", "", "data:",
    ] + sse({"choices": [{"delta": {"content": " there"}, "finish_reason": "stop"}]})
    res, ex, events = _run(ap.call_openai_compatible, OAI_PROVIDER, lines, tools=None)
    check("oai: malformed/blank data lines were skipped, not fatal", res.ok, res.error)
    check("oai: the valid lines still assembled correctly", res.text == "Hi there", res.text)

    drop_lines = sse({"choices": [{"delta": {"content": "Partial"}}]}) + [
        requests.exceptions.ConnectionError("connection reset")]
    res2, ex2, events2 = _run(ap.call_openai_compatible, OAI_PROVIDER, drop_lines, tools=None)
    check("oai: a mid-stream drop ends as a clean AIResult(False, error=...)",
          res2.ok is False and "interrupted" in (res2.error or ""), res2.error)


# ================================================================== anthropic

ANTH_PROVIDER = {"name": "t", "model": "m", "api_key": "k", "base_url": "http://x"}


def test_anthropic_text_and_usage_match_non_streaming_shape():
    lines = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Battery "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "is fine."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    )
    res, ex, events = _run(ap.call_anthropic, ANTH_PROVIDER, lines, tools=None)
    check("anthropic: assembled text is the concatenation of every delta", res.text == "Battery is fine.", res.text)
    check("anthropic: ok, no tool calls, no error", res.ok and not ex.calls, res.error)
    text_events = [d["delta"] for k, d in events if k == ap.EVENT_TEXT]
    check("anthropic: both text deltas were emitted live, in order",
          text_events == ["Battery ", "is fine."], text_events)
    check("anthropic: input_tokens from message_start and output_tokens from message_delta both carry through",
          res.usage is not None and res.usage.get("input_tokens") == 20 and res.usage.get("output_tokens") == 6,
          res.usage)
    check("anthropic: a round_end event fires with finish=done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_DONE for k, d in events), events)


def test_anthropic_thinking_deltas_assemble_with_signature():
    lines = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "consider this."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Done."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
    )
    res, ex, events = _run(ap.call_anthropic, ANTH_PROVIDER, lines, tools=None)
    check("anthropic: final text excludes the thinking trace", res.text == "Done.", res.text)
    thinking_events = [d["delta"] for k, d in events if k == ap.EVENT_THINKING]
    check("anthropic: both thinking deltas were emitted live, in order (signature_delta is not one of them)",
          thinking_events == ["Let me ", "consider this."], thinking_events)
    check("anthropic: assembled thinking trace is saved the same way a non-streamed reply's is",
          ap.get_thinking_trace().get("text") == "Let me consider this.", ap.get_thinking_trace())


def test_anthropic_tool_use_input_json_delta_assembles_and_runs():
    round1 = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking now."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_battery", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"un"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "it\": \"pct\"}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}},
    )
    round2 = sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Battery is at 87%."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
    )
    calls = {"n": 0}

    def router(url, headers, payload, timeout):
        calls["n"] += 1
        return FakeStreamResp(round1 if calls["n"] == 1 else round2), None

    res, ex, events = _run_with_router(ap.call_anthropic, ANTH_PROVIDER, router)
    check("anthropic: the tool was called with the fully-reassembled input",
          ex.calls == [("get_battery", {"unit": "pct"})], ex.calls)
    check("anthropic: final answer after the tool round is the second round's streamed text",
          res.ok and res.text == "Battery is at 87%.", (res.ok, res.text))
    tool_events = [d for k, d in events if k == ap.EVENT_TOOL]
    check("anthropic: a tool event fired only once input_json_delta fragments were complete",
          tool_events and tool_events[0]["name"] == "get_battery" and tool_events[0]["arguments"] == {"unit": "pct"},
          tool_events)
    check("anthropic: the first round's round_end says tool, not done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_TOOL for k, d in events), events)


def test_anthropic_malformed_line_skipped_and_mid_stream_drop_is_clean():
    lines = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
    ) + ["data: {not json", "", "event: ping"] + sse(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " there"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    )
    res, ex, events = _run(ap.call_anthropic, ANTH_PROVIDER, lines, tools=None)
    check("anthropic: malformed/blank/non-data lines were skipped, not fatal", res.ok, res.error)
    check("anthropic: the valid lines still assembled correctly", res.text == "Hi there", res.text)

    drop_lines = sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Partial"}},
    ) + [requests.exceptions.ConnectionError("connection reset")]
    res2, ex2, events2 = _run(ap.call_anthropic, ANTH_PROVIDER, drop_lines, tools=None)
    check("anthropic: a mid-stream drop ends as a clean AIResult(False, error=...)",
          res2.ok is False and "interrupted" in (res2.error or ""), res2.error)


# ===================================================================== gemini

GEMINI_PROVIDER = {"name": "t", "model": "m", "api_key": "k"}


def test_gemini_stream_url_swaps_the_endpoint():
    check("gemini: generateContent becomes streamGenerateContent?alt=sse",
          ap._gemini_stream_url("https://x/v1beta/models/m:generateContent")
          == "https://x/v1beta/models/m:streamGenerateContent?alt=sse")
    check("gemini: a custom URL with no matching suffix just gets alt=sse appended",
          ap._gemini_stream_url("https://proxy.example/chat") == "https://proxy.example/chat?alt=sse")


def test_gemini_text_and_usage_match_non_streaming_shape():
    lines = sse(
        {"candidates": [{"content": {"parts": [{"text": "Battery "}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "is fine."}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 15, "candidatesTokenCount": 5}},
    )
    res, ex, events = _run(ap.call_gemini, GEMINI_PROVIDER, lines, tools=None)
    check("gemini: assembled text is the concatenation of every delta", res.text == "Battery is fine.", res.text)
    check("gemini: ok, no tool calls, no error", res.ok and not ex.calls, res.error)
    text_events = [d["delta"] for k, d in events if k == ap.EVENT_TEXT]
    check("gemini: both text deltas were emitted live, in order", text_events == ["Battery ", "is fine."], text_events)
    check("gemini: the latest chunk's cumulative usageMetadata carries through",
          res.usage is not None and res.usage.get("input_tokens") == 15 and res.usage.get("output_tokens") == 5,
          res.usage)
    check("gemini: a round_end event fires with finish=done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_DONE for k, d in events), events)


def test_gemini_thought_parts_assemble_and_fire():
    lines = sse(
        {"candidates": [{"content": {"parts": [{"thought": True, "text": "Let me "}]}}]},
        {"candidates": [{"content": {"parts": [{"thought": True, "text": "consider this."}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "Done."}]}, "finishReason": "STOP"}]},
    )
    res, ex, events = _run(ap.call_gemini, GEMINI_PROVIDER, lines, tools=None)
    check("gemini: final text excludes the thought parts", res.text == "Done.", res.text)
    thinking_events = [d["delta"] for k, d in events if k == ap.EVENT_THINKING]
    check("gemini: both thought deltas were emitted live, in order",
          thinking_events == ["Let me ", "consider this."], thinking_events)
    check("gemini: assembled thinking trace is saved the same way a non-streamed reply's is",
          ap.get_thinking_trace().get("text") == "Let me consider this.", ap.get_thinking_trace())


def test_gemini_function_call_part_arrives_whole_and_runs():
    round1 = sse(
        {"candidates": [{"content": {"parts": [{"text": "Checking now."}]}}]},
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "get_battery", "args": {}}}]}, "finishReason": "STOP"}]},
    )
    round2 = sse({"candidates": [{"content": {"parts": [{"text": "Battery is at 87%."}]}, "finishReason": "STOP"}]})
    calls = {"n": 0}

    def router(url, headers, payload, timeout):
        calls["n"] += 1
        return FakeStreamResp(round1 if calls["n"] == 1 else round2), None

    res, ex, events = _run_with_router(ap.call_gemini, GEMINI_PROVIDER, router)
    check("gemini: the tool was called with the streamed-whole args",
          ex.calls == [("get_battery", {})], ex.calls)
    check("gemini: final answer after the tool round is the second round's streamed text",
          res.ok and res.text == "Battery is at 87%.", (res.ok, res.text))
    tool_events = [d for k, d in events if k == ap.EVENT_TOOL]
    check("gemini: a tool event fired with the right name and (whole) arguments",
          tool_events and tool_events[0]["name"] == "get_battery" and tool_events[0]["arguments"] == {},
          tool_events)
    check("gemini: the first round's round_end says tool, not done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_TOOL for k, d in events), events)


def test_gemini_malformed_line_skipped_and_mid_stream_drop_is_clean():
    lines = sse({"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]}) + [
        "data: {not json at all", "",
    ] + sse({"candidates": [{"content": {"parts": [{"text": " there"}]}, "finishReason": "STOP"}]})
    res, ex, events = _run(ap.call_gemini, GEMINI_PROVIDER, lines, tools=None)
    check("gemini: malformed/blank data lines were skipped, not fatal", res.ok, res.error)
    check("gemini: the valid lines still assembled correctly", res.text == "Hi there", res.text)

    drop_lines = sse({"candidates": [{"content": {"parts": [{"text": "Partial"}]}}]}) + [
        requests.exceptions.ConnectionError("connection reset")]
    res2, ex2, events2 = _run(ap.call_gemini, GEMINI_PROVIDER, drop_lines, tools=None)
    check("gemini: a mid-stream drop ends as a clean AIResult(False, error=...)",
          res2.ok is False and "interrupted" in (res2.error or ""), res2.error)


# ===================================================================== cohere

COHERE_PROVIDER = {"name": "t", "model": "m", "api_key": "k"}


def test_cohere_text_and_usage_match_non_streaming_shape():
    lines = sse(
        {"type": "content-delta", "delta": {"message": {"content": {"text": "Battery "}}}},
        {"type": "content-delta", "delta": {"message": {"content": {"text": "is fine."}}}},
        {"type": "message-end", "delta": {"finish_reason": "COMPLETE",
                                          "usage": {"tokens": {"input_tokens": 9, "output_tokens": 3}}}},
    )
    res, ex, events = _run(ap.call_cohere, COHERE_PROVIDER, lines, tools=None)
    check("cohere: assembled text is the concatenation of every delta", res.text == "Battery is fine.", res.text)
    check("cohere: ok, no tool calls, no error", res.ok and not ex.calls, res.error)
    text_events = [d["delta"] for k, d in events if k == ap.EVENT_TEXT]
    check("cohere: both text deltas were emitted live, in order", text_events == ["Battery ", "is fine."], text_events)
    check("cohere: usage from message-end carries through",
          res.usage is not None and res.usage.get("input_tokens") == 9 and res.usage.get("output_tokens") == 3,
          res.usage)
    check("cohere: a round_end event fires with finish=done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_DONE for k, d in events), events)
    check("cohere: no thinking event fired — this family has no reasoning-model member yet",
          not [d for k, d in events if k == ap.EVENT_THINKING], events)


def test_cohere_fragmented_tool_call_assembles_and_runs():
    round1 = sse(
        {"type": "tool-plan-delta", "delta": {"message": {"tool_plan": "I will check the battery."}}},
        {"type": "tool-call-start", "index": 0,
         "delta": {"message": {"tool_calls": {"id": "call_1", "function": {"name": "get_battery", "arguments": ""}}}}},
        {"type": "tool-call-delta", "index": 0,
         "delta": {"message": {"tool_calls": {"function": {"arguments": "{\"unit\":"}}}}},
        {"type": "tool-call-delta", "index": 0,
         "delta": {"message": {"tool_calls": {"function": {"arguments": "\"pct\"}"}}}}},
        {"type": "tool-call-end", "index": 0},
        {"type": "message-end", "delta": {"finish_reason": "TOOL_CALL"}},
    )
    round2 = sse(
        {"type": "content-delta", "delta": {"message": {"content": {"text": "Battery is at 87%."}}}},
        {"type": "message-end", "delta": {"finish_reason": "COMPLETE"}},
    )
    calls = {"n": 0}

    def router(url, headers, payload, timeout):
        calls["n"] += 1
        return FakeStreamResp(round1 if calls["n"] == 1 else round2), None

    res, ex, events = _run_with_router(ap.call_cohere, COHERE_PROVIDER, router)
    check("cohere: the tool was called with the fully-reassembled arguments",
          ex.calls == [("get_battery", {"unit": "pct"})], ex.calls)
    check("cohere: final answer after the tool round is the second round's streamed text",
          res.ok and res.text == "Battery is at 87%.", (res.ok, res.text))
    tool_events = [d for k, d in events if k == ap.EVENT_TOOL]
    check("cohere: a tool event fired only once tool-call-end confirmed the call complete",
          tool_events and tool_events[0]["name"] == "get_battery", tool_events)
    check("cohere: the first round's round_end says tool, not done",
          any(k == ap.EVENT_ROUND_END and d.get("finish") == ap.FINISH_ROUND_TOOL for k, d in events), events)


def test_cohere_malformed_line_skipped_and_mid_stream_drop_is_clean():
    lines = sse({"type": "content-delta", "delta": {"message": {"content": {"text": "Hi"}}}}) + [
        "data: {not json at all", "",
    ] + sse(
        {"type": "content-delta", "delta": {"message": {"content": {"text": " there"}}}},
        {"type": "message-end", "delta": {"finish_reason": "COMPLETE"}},
    )
    res, ex, events = _run(ap.call_cohere, COHERE_PROVIDER, lines, tools=None)
    check("cohere: malformed/blank data lines were skipped, not fatal", res.ok, res.error)
    check("cohere: the valid lines still assembled correctly", res.text == "Hi there", res.text)

    drop_lines = sse({"type": "content-delta", "delta": {"message": {"content": {"text": "Partial"}}}}) + [
        requests.exceptions.ConnectionError("connection reset")]
    res2, ex2, events2 = _run(ap.call_cohere, COHERE_PROVIDER, drop_lines, tools=None)
    check("cohere: a mid-stream drop ends as a clean AIResult(False, error=...)",
          res2.ok is False and "interrupted" in (res2.error or ""), res2.error)


for fn in [
    test_oai_text_and_usage_match_non_streaming_shape,
    test_oai_thinking_deltas_assemble_and_fire,
    test_oai_fragmented_tool_call_assembles_and_runs,
    test_oai_malformed_line_skipped_and_mid_stream_drop_is_clean,
    test_anthropic_text_and_usage_match_non_streaming_shape,
    test_anthropic_thinking_deltas_assemble_with_signature,
    test_anthropic_tool_use_input_json_delta_assembles_and_runs,
    test_anthropic_malformed_line_skipped_and_mid_stream_drop_is_clean,
    test_gemini_stream_url_swaps_the_endpoint,
    test_gemini_text_and_usage_match_non_streaming_shape,
    test_gemini_thought_parts_assemble_and_fire,
    test_gemini_function_call_part_arrives_whole_and_runs,
    test_gemini_malformed_line_skipped_and_mid_stream_drop_is_clean,
    test_cohere_text_and_usage_match_non_streaming_shape,
    test_cohere_fragmented_tool_call_assembles_and_runs,
    test_cohere_malformed_line_skipped_and_mid_stream_drop_is_clean,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
