"""Tests for DEV_AGENT_KNOWN_BUGS.md BUG-7 and BUG-8.

BUG-7 (mojibake in Groq replies): `requests` decodes a `text/*` response that
states no charset as ISO-8859-1, and an SSE stream (`text/event-stream`) is
exactly that. `iter_lines(decode_unicode=True)` therefore turned each UTF-8
multi-byte character into 2-3 Latin-1 ones (a non-breaking hyphen came out as
three junk characters). `ai_providers._force_utf8` now fixes the encoding when
no charset was stated, in both `_post_stream` and `_post_json`; a charset the
server DID state is left alone.

BUG-8 (provider failures pushed agent work onto the weaker model): ask() will
no longer fail over, on an agent turn (dev_agent / code_agent already ran, or
the router offered one), to a provider whose entry says `"agent_safe": false`.
Nothing is flagged by default; `defaults.agent_fallback_guard: false` switches
the rule off; an explicit provider_override is never second-guessed.

Run: python3 tests/test_dev_agent_bug7_bug8.py
"""
import io
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

from jarvis import (  # noqa: E402
    ai_client, ai_config, ai_providers, conversations, key_health as kh, logs, tool_router,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# ---------------------------------------------------------------------------
# BUG-7
# ---------------------------------------------------------------------------

# A non-breaking hyphen (U+2011) is E2 80 91 in UTF-8; the box-drawing
# character U+2500 is E2 94 80. Both were garbled in the real conversation.
TEXT = "dev\u2011agent \u2500\u2500 caf\u00e9 \U0001F600"


def _sse_response(content_type, pieces):
    """A real requests.Response over fixed bytes, with `.encoding` set the way
    requests' adapter sets it: from the Content-Type header, which gives
    ISO-8859-1 for a text/* type that states no charset."""
    import json as _json
    body = b""
    for piece in pieces:
        chunk = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
        body += b"data: " + _json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"
    body += (b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
             b"data: [DONE]\n\n")
    resp = requests.Response()
    resp.status_code = 200
    resp.headers["Content-Type"] = content_type
    resp.raw = io.BytesIO(body)
    resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
    return resp


def _stream_text(resp):
    data, err = ai_providers._stream_openai_compatible_chat(resp, "https://example.invalid/v1/chat")
    assert err is None, err
    return data["choices"][0]["message"]["content"]


def test_default_requests_encoding_is_the_trap():
    resp = _sse_response("text/event-stream", [TEXT])
    check("requests reports ISO-8859-1 for an SSE type with no charset (the trap)",
          (resp.encoding or "").lower() == "iso-8859-1", resp.encoding)


def test_stream_is_garbled_without_the_fix_and_clean_with_it():
    garbled = _stream_text(_sse_response("text/event-stream", [TEXT]))
    check("without _force_utf8 the stream decodes as Latin-1 mojibake", garbled != TEXT, garbled)

    resp = ai_providers._force_utf8(_sse_response("text/event-stream", [TEXT]))
    check("_force_utf8 returns the same response object", resp is not None)
    check("with _force_utf8 the streamed text round-trips exactly", _stream_text(resp) == TEXT)


def test_multibyte_split_across_chunks_is_clean():
    # The decoder is incremental, so a character cut between two network
    # chunks must still come out whole. Split the JSON payload mid-character.
    resp = _sse_response("text/event-stream", ["dev\u2011", "agent"])
    ai_providers._force_utf8(resp)
    check("two deltas reassemble to one clean string", _stream_text(resp) == "dev\u2011agent")


def test_stated_charset_is_respected():
    resp = _sse_response("text/event-stream; charset=iso-8859-1", ["plain"])
    ai_providers._force_utf8(resp)
    check("a charset the server stated is not overridden",
          (resp.encoding or "").lower() == "iso-8859-1", resp.encoding)


def test_force_utf8_never_raises_on_odd_objects():
    class Bare:
        pass

    ok = True
    try:
        ai_providers._force_utf8(Bare())
        ai_providers._force_utf8(None)
    except Exception as e:  # noqa: BLE001
        ok = False
        print(e)
    check("a response-like object without headers/encoding is left alone", ok)


def test_post_stream_and_post_json_apply_it():
    seen = {}
    real_post = requests.post

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        resp = _sse_response("text/event-stream", [TEXT])
        seen[stream] = resp
        return resp

    requests.post = fake_post
    try:
        r1, e1 = ai_providers._post_stream("https://example.invalid", {}, {}, 5)
        r2, e2 = ai_providers._post_json("https://example.invalid", {}, {}, 5)
    finally:
        requests.post = real_post
    check("_post_stream returns a UTF-8 response", e1 is None and (r1.encoding or "").lower() == "utf-8", getattr(r1, "encoding", None))
    check("_post_json returns a UTF-8 response", e2 is None and (r2.encoding or "").lower() == "utf-8", getattr(r2, "encoding", None))


for fn in [
    test_default_requests_encoding_is_the_trap,
    test_stream_is_garbled_without_the_fix_and_clean_with_it,
    test_multibyte_split_across_chunks_is_clean,
    test_stated_charset_is_respected,
    test_force_utf8_never_raises_on_odd_objects,
    test_post_stream_and_post_json_apply_it,
]:
    fn()


# ---------------------------------------------------------------------------
# BUG-8 -- pure helpers
# ---------------------------------------------------------------------------

def test_provider_agent_safe_parsing():
    f = ai_client._provider_agent_safe
    check("absent flag -> safe (nothing is flagged by default)", f({"name": "x"}) is True)
    check("agent_safe true -> safe", f({"agent_safe": True}) is True)
    check("agent_safe false -> unsafe", f({"agent_safe": False}) is False)
    check('"false" / "No" / "off" strings -> unsafe',
          all(f({"agent_safe": v}) is False for v in ("false", "No", " off ", "0")))
    check("junk value -> safe (never silently locks a provider out)", f({"agent_safe": "maybe"}) is True)
    check("None provider -> safe", f(None) is True)


class _Exec:
    def __init__(self, names):
        self.runs = [{"name": n, "arguments": {}, "result": {"ok": True}} for n in names]

    def __call__(self, *a, **k):
        raise AssertionError("not used")


def test_is_agent_turn():
    f = ai_client._is_agent_turn
    check("dev_agent ran -> agent turn", f(_Exec(["list_dir", "dev_agent"]), None) is True)
    check("code_agent ran -> agent turn", f(_Exec(["code_agent"]), None) is True)
    check("only ordinary tools ran -> not an agent turn", f(_Exec(["read_file", "run_shell"]), None) is False)
    check("no executor, no route -> not an agent turn", f(None, None) is False)
    routed = tool_router.RouteResult(["dev_agent", "read_file"], ["dev_agent"])
    check("router offered dev_agent -> agent turn (first attempt too)", f(_Exec([]), routed) is True)
    plain = tool_router.RouteResult(["read_file"], ["files"])
    check("router offered only ordinary tools -> not an agent turn", f(_Exec([]), plain) is False)


test_provider_agent_safe_parsing()
test_is_agent_turn()


# ---------------------------------------------------------------------------
# BUG-8 -- through ask()'s real failover loop
# ---------------------------------------------------------------------------

def _reset_key_health():
    try:
        kh.HEALTH_FILE.unlink()
    except OSError:
        pass


@contextmanager
def _env(adapter, executor_runs, providers, defaults_extra=None, route=None):
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS), ai_client._make_tool_executor,
            tool_router.route, conversations.JARVIS_DIR, conversations.CONV_DIR,
            conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    defaults = {"tools_enabled": True, "prompt_mode": "full"}
    defaults.update(defaults_extra or {})
    cfg = {"persona": {}, "providers": providers, "defaults": defaults}
    ex = _Exec(executor_runs)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        ai_config.load_ai_config = lambda: cfg
        ai_providers.ADAPTERS["fake"] = adapter
        ai_client._make_tool_executor = lambda *a, **k: ex
        if route is not None:
            tool_router.route = lambda *a, **k: route
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, _, ai_client._make_tool_executor, tool_router.route,
             conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
             conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def _providers(groq_extra=None):
    return [
        {"name": "gem", "type": "fake", "enabled": True, "api_keys": ["kg"], "model": "m"},
        {"name": "groq", "type": "fake", "enabled": True, "api_keys": ["kq"], "model": "m",
         **(groq_extra or {})},
    ]


def _adapter(calls):
    """gem is overloaded (the real incident's 503); groq would answer."""
    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        if resolved["api_key"] == "kg":
            return ai_providers.AIResult(False, error="provider server error (HTTP 503) model overloaded",
                                         kind=ai_providers.KIND_OVERLOAD)
        return ai_providers.AIResult(True, text="groq answered")
    return adapter


def test_agent_turn_skips_unsafe_provider():
    _reset_key_health()
    calls = []
    with _env(_adapter(calls), ["dev_agent"], _providers({"agent_safe": False})) as conv:
        r = ai_client.ask("make me a small node app", commands=[], conversation_id=conv)
    check("the unsafe provider is never called on an agent turn", calls == ["kg"], calls)
    check("the turn does not succeed through the unsafe provider", r.provider != "groq", r.provider)
    reasons = " ".join(msg for _, msg in (r.attempts or []))
    check("attempts say why groq was skipped", "agent_safe=false" in reasons and "groq" in reasons, reasons)


def test_ordinary_turn_still_fails_over_to_the_flagged_provider():
    _reset_key_health()
    calls = []
    with _env(_adapter(calls), ["read_file"], _providers({"agent_safe": False})) as conv:
        r = ai_client.ask("what is in main.py", commands=[], conversation_id=conv)
    check("a plain turn still falls back to the flagged provider", calls == ["kg", "kq"], calls)
    check("and succeeds", r.ok and r.text == "groq answered", r)


def test_unflagged_provider_still_takes_over_an_agent_turn():
    _reset_key_health()
    calls = []
    with _env(_adapter(calls), ["dev_agent"], _providers()) as conv:
        r = ai_client.ask("make me a small node app", commands=[], conversation_id=conv)
    check("nothing is flagged by default, so behavior is unchanged", calls == ["kg", "kq"], calls)
    check("failover succeeds as before", r.ok and r.text == "groq answered", r)


def test_guard_can_be_switched_off():
    _reset_key_health()
    calls = []
    with _env(_adapter(calls), ["dev_agent"], _providers({"agent_safe": False}),
              {"agent_fallback_guard": False}) as conv:
        r = ai_client.ask("make me a small node app", commands=[], conversation_id=conv)
    check("defaults.agent_fallback_guard=false lets the flagged provider run", calls == ["kg", "kq"], calls)
    check("and it answers", r.ok, r)


def test_explicit_provider_override_is_respected():
    _reset_key_health()
    calls = []
    with _env(_adapter(calls), ["dev_agent"], _providers({"agent_safe": False})) as conv:
        r = ai_client.ask("make me a small node app", commands=[], conversation_id=conv,
                          provider_override=["gem", "groq"])
    check("a provider order picked for this ask is never second-guessed", calls == ["kg", "kq"], calls)
    check("and the picked provider answers", r.ok and r.text == "groq answered", r)


def test_router_offering_dev_agent_guards_the_first_attempt():
    _reset_key_health()
    calls = []
    routed = tool_router.RouteResult(["dev_agent"], ["dev_agent"])
    with _env(_adapter(calls), [], _providers({"agent_safe": False}), route=routed) as conv:
        r = ai_client.ask("go on agent mode and make me an app", commands=[], conversation_id=conv)
    check("no tool has run yet, but the router offered dev_agent: groq is skipped", calls == ["kg"], calls)
    check("the ask fails honestly instead of answering from the flagged provider", r.ok is False, r.ok)


for fn in [
    test_agent_turn_skips_unsafe_provider,
    test_ordinary_turn_still_fails_over_to_the_flagged_provider,
    test_unflagged_provider_still_takes_over_an_agent_turn,
    test_guard_can_be_switched_off,
    test_explicit_provider_override_is_respected,
    test_router_offering_dev_agent_guards_the_first_attempt,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
