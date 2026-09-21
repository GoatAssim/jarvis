"""Tests for master plan Part A §5 ("model can still call tools after
talking to the user"), Anthropic prototype only — per the plan's own staged
order ("prototype it on one adapter first... not done until all five
follow it").

Before this: every adapter's shape was

    if tool_calls_present and ...:
        # execute tools, append to history
        continue
    text = "".join(text parts)
    return AIResult(True, text=text, ...)

so `text = ...` was only ever reached when there were NO tool calls that
round. Any narration the model sent alongside a tool call ("I'll check
that now" + an actual tool_use block, in the same response) was silently
thrown away — not "not streamed", genuinely discarded; the user only ever
saw text from the final, tool-free round.

Now `call_anthropic` extracts text unconditionally, and when it co-occurs
with tool_use blocks, captures it (ai_providers.get_interim_text()) and
fires it live via the on_interim_text hook (set_log_context) BEFORE
running the tools that came with it, so:
  - it isn't lost from the model's/turn's record even if a later round's
    text ends up being the one returned as the final answer
  - a caller that wants to show it live (cli.py's stderr trace, which
    server.js forwards to the web UI) can

Run: python3 tests/test_interim_text.py
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_config, ai_providers, conversations, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


PROVIDER = {"name": "test-anthropic", "model": "claude-x", "api_key": "sk-test"}
_TOOL_SCHEMA = [{"name": "get_battery", "description": "d",
                 "parameters": {"type": "object", "properties": {}}}]


class _FakeResp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.text = ""
        self.headers = {}

    def json(self):
        return self._data


def _queue_post_json(responses):
    """Monkeypatch target: serves `responses` (one dict per call) in order.
    These tests are about call_anthropic's own text/tool-call handling, not
    the HTTP layer (covered elsewhere) — the request itself is ignored."""
    it = iter(responses)

    def fake(url, headers, payload, timeout):
        return _FakeResp(next(it)), None
    return fake


@contextmanager
def _fake_http(responses):
    orig = ai_providers._post_json
    ai_providers._post_json = _queue_post_json(responses)
    try:
        yield
    finally:
        ai_providers._post_json = orig


# ---------------------------------------------------------------------------
# Direct unit tests of call_anthropic.
# ---------------------------------------------------------------------------

def test_text_alongside_tool_call_is_captured_and_final_answer_is_clean():
    ai_providers.set_thinking("off")
    seen_live = []

    def on_interim(text, round_num):
        seen_live.append((text, round_num))

    ai_providers.set_log_context(None, "test", on_interim_text=on_interim)
    try:
        with _fake_http([
            {  # round 0: narration + a tool call, same response
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "I'll check that now."},
                    {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}},
                ],
            },
            {  # round 1: the real final answer, no tool calls
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Battery is fine."}],
            },
        ]):
            result = ai_providers.call_anthropic(
                PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
                tools=_TOOL_SCHEMA, tool_executor=lambda name, args: {"ok": True, "percent": 87},
            )
    finally:
        ai_providers.clear_log_context()

    check("ask succeeds", result.ok, result.error)
    check("final text is ONLY the last round's answer, not the narration",
          result.text == "Battery is fine.", result.text)
    check("interim narration was captured, not discarded",
          ai_providers.get_interim_text() == [{"round": 0, "text": "I'll check that now."}],
          ai_providers.get_interim_text())
    check("on_interim_text fired live, before the final answer",
          seen_live == [("I'll check that now.", 0)], seen_live)


def test_tool_use_with_no_text_collects_nothing():
    ai_providers.set_thinking("off")
    with _fake_http([
        {"stop_reason": "tool_use",
         "content": [{"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]},
    ]):
        result = ai_providers.call_anthropic(
            PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=_TOOL_SCHEMA, tool_executor=lambda name, args: {"ok": True},
        )
    check("ask succeeds", result.ok, result.error)
    check("no interim text when the tool-call round had no narration",
          ai_providers.get_interim_text() == [], ai_providers.get_interim_text())


def test_a_plain_final_answer_is_not_treated_as_interim():
    ai_providers.set_thinking("off")
    with _fake_http([
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Just an answer, no tools."}]},
    ]):
        result = ai_providers.call_anthropic(
            PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=None, tool_executor=None,
        )
    check("ask succeeds", result.ok, result.error)
    check("final answer text is correct", result.text == "Just an answer, no tools.", result.text)
    check("nothing logged as interim — it's the final answer, not narration",
          ai_providers.get_interim_text() == [], ai_providers.get_interim_text())


def test_multiple_tool_rounds_each_keep_their_own_interim_text():
    ai_providers.set_thinking("off")
    with _fake_http([
        {"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "First, let me look at A."},
            {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}},
        ]},
        {"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "Now let me check B."},
            {"type": "tool_use", "id": "t2", "name": "get_battery", "input": {}},
        ]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "All done."}]},
    ]):
        result = ai_providers.call_anthropic(
            PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=_TOOL_SCHEMA, tool_executor=lambda name, args: {"ok": True},
        )
    check("ask succeeds", result.ok, result.error)
    check("final text is the last round's answer only", result.text == "All done.", result.text)
    check("both rounds' narration captured, in order, tagged with their own round",
          ai_providers.get_interim_text() == [
              {"round": 0, "text": "First, let me look at A."},
              {"round": 1, "text": "Now let me check B."},
          ], ai_providers.get_interim_text())


def test_interim_text_resets_between_attempts():
    ai_providers.set_thinking("off")
    with _fake_http([
        {"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "leftover from a previous attempt"},
            {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}},
        ]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]},
    ]):
        ai_providers.call_anthropic(
            PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=_TOOL_SCHEMA, tool_executor=lambda name, args: {"ok": True},
        )
    check("interim text exists after the first attempt", len(ai_providers.get_interim_text()) == 1)
    ai_providers.set_thinking("off")  # what ai_client.ask() does at the start of every attempt
    check("...and is gone once the next attempt starts (set_thinking resets it)",
          ai_providers.get_interim_text() == [])


def test_on_interim_text_exception_never_breaks_the_turn():
    # A trace hook is a nice-to-have, not load-bearing — a caller's buggy
    # callback must never take the whole ask down.
    ai_providers.set_thinking("off")

    def bad_hook(text, round_num):
        raise RuntimeError("boom")

    ai_providers.set_log_context(None, "test", on_interim_text=bad_hook)
    try:
        with _fake_http([
            {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "narrating"},
                {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}},
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "fine"}]},
        ]):
            result = ai_providers.call_anthropic(
                PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
                tools=_TOOL_SCHEMA, tool_executor=lambda name, args: {"ok": True},
            )
    finally:
        ai_providers.clear_log_context()
    check("a raising on_interim_text hook doesn't break the turn", result.ok and result.text == "fine", result)


for fn in [
    test_text_alongside_tool_call_is_captured_and_final_answer_is_clean,
    test_tool_use_with_no_text_collects_nothing,
    test_a_plain_final_answer_is_not_treated_as_interim,
    test_multiple_tool_rounds_each_keep_their_own_interim_text,
    test_interim_text_resets_between_attempts,
    test_on_interim_text_exception_never_breaks_the_turn,
]:
    fn()


# ---------------------------------------------------------------------------
# Integration test through ai_client.ask(): the real "anthropic" adapter
# type, with only the HTTP layer faked, checking on_interim_text fires from
# ask() itself and the saved conversation carries an "interimText" extra.
# ---------------------------------------------------------------------------

@contextmanager
def _ask_env(providers, defaults_extra=None):
    orig = (ai_config.load_ai_config, conversations.JARVIS_DIR, conversations.CONV_DIR,
            conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    defaults = {"tools_enabled": True, "prompt_mode": "full"}
    defaults.update(defaults_extra or {})
    cfg = {"persona": {}, "providers": providers, "defaults": defaults}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        ai_config.load_ai_config = lambda: cfg
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, conversations.JARVIS_DIR, conversations.CONV_DIR,
             conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR) = orig


def _prov(name, keys, **extra):
    return {"name": name, "type": "anthropic", "enabled": True, "api_keys": keys, "model": "m", **extra}


def test_ask_wires_interim_text_live_and_into_the_saved_exchange():
    ai_providers.set_thinking("off")
    live = []

    def on_interim(text, round_num):
        live.append((text, round_num))

    with _ask_env([_prov("anthropic", ["k1"])]) as conv:
        with _fake_http([
            {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Let me check the battery."},
                {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}},
            ]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "It's at a healthy level."}]},
        ]):
            r = ai_client.ask("how's my battery", commands=[], conversation_id=conv,
                               on_interim_text=on_interim)
        saved = conversations.get_conversation(conv)

    check("ask succeeds", r.ok, r)
    check("final reply text is the clean final answer",
          r.text == "It's at a healthy level.", r.text)
    check("on_interim_text fired through ask(), same as cli.py would wire it",
          live == [("Let me check the battery.", 0)], live)
    exchanges = (saved or {}).get("exchanges") or []
    check("exactly one exchange was saved", len(exchanges) == 1, exchanges)
    extras = exchanges[0].get("extras") if exchanges else []
    interim_extras = [e for e in (extras or []) if e.get("type") == "interimText"]
    check("saved exchange carries an interimText extra",
          len(interim_extras) == 1 and
          interim_extras[0]["data"]["items"] == [{"round": 0, "text": "Let me check the battery."}],
          interim_extras)


test_ask_wires_interim_text_live_and_into_the_saved_exchange()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
