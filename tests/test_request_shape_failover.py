"""Tests for ask()'s key rotation when a failure is about the REQUEST, not the key.

Background (master plan, section 1c): one logged turn burned three Groq keys
on "Tool choice is none, but model called a tool" and a fourth on a
tool-argument schema mismatch. Both are rejections of the payload itself, so
the next key on the same provider gets the identical 400. ask() now skips a
provider's remaining keys for a whitelisted set of those message shapes and
moves on to the next provider; every other failure (dead key, 429, 5xx, an
unrecognized 400) still rotates keys exactly as before.

No network, no API keys: adapters are fakes registered in
ai_providers.ADAPTERS, and ~/.jarvis is redirected to a temp dir before any
jarvis module is imported (most modules resolve Path.home() at import time).

Run: python3 tests/test_request_shape_failover.py
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


# The exact strings _status_reason() produces for the two Groq 400s in the log.
TOOL_CHOICE_NONE = ("HTTP 400: {\"error\":{\"message\":\"Tool choice is none, but model called a tool\","
                    "\"type\":\"invalid_request_error\",\"code\":\"tool_use_failed\"}}")
SCHEMA_MISMATCH = ("HTTP 400: {\"error\":{\"message\":\"Tool call validation failed: parameters for tool "
                   "spawn_subagent did not match schema: errors: [/parent_id: expected string, but got null]\"}}")


def test_classifier_recognizes_the_logged_request_shape_errors():
    check("Groq 'Tool choice is none' is request-shaped", ai_providers.is_request_shape_error(TOOL_CHOICE_NONE))
    check("Groq tool-argument schema mismatch is request-shaped", ai_providers.is_request_shape_error(SCHEMA_MISMATCH))
    check("'not in request.tools' is request-shaped",
          ai_providers.is_request_shape_error("HTTP 400: attempted to call tool 'x' which was not in request.tools"))
    check("Gemini duplicate declaration is request-shaped",
          ai_providers.is_request_shape_error("HTTP 400: Duplicate function declaration found: web_search"))


def test_classifier_leaves_key_problems_alone():
    # Gemini reports an INVALID KEY as a 400 too - the next key fixes that one,
    # which is exactly why this is a whitelist and not "any HTTP 400".
    check("Gemini invalid-key 400 still rotates",
          not ai_providers.is_request_shape_error("HTTP 400: API key not valid. Please pass a valid API key."))
    check("429 still rotates", not ai_providers.is_request_shape_error("rate limited or quota exceeded (HTTP 429)"))
    check("401 still rotates", not ai_providers.is_request_shape_error("invalid or unauthorized API key (HTTP 401)"))
    check("5xx still rotates", not ai_providers.is_request_shape_error("provider server error (HTTP 503)"))
    check("timeouts still rotate", not ai_providers.is_request_shape_error("timed out after 30s"))
    check("an unrecognized 400 still rotates", not ai_providers.is_request_shape_error("HTTP 400: invalid model name"))
    check("None is not request-shaped", not ai_providers.is_request_shape_error(None))
    check("empty string is not request-shaped", not ai_providers.is_request_shape_error(""))


# ---------------------------------------------------------------------------
# End to end through ask()
# ---------------------------------------------------------------------------

@contextmanager
def _isolated_ask_env():
    orig = {
        "conv_dir": conversations.JARVIS_DIR, "conv_conv_dir": conversations.CONV_DIR,
        "conv_index": conversations.INDEX_FILE, "conv_current": conversations.CURRENT_FILE,
        "logs_dir": logs.JARVIS_DIR, "logs_log_dir": logs.LOG_DIR,
        "spawn": ai_client._spawn_title_update,
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
        # A successful ask() launches a detached retitle OS process; not wanted here.
        ai_client._spawn_title_update = lambda *a, **k: None
        try:
            yield conversations.get_current_id()
        finally:
            conversations.JARVIS_DIR = orig["conv_dir"]
            conversations.CONV_DIR = orig["conv_conv_dir"]
            conversations.INDEX_FILE = orig["conv_index"]
            conversations.CURRENT_FILE = orig["conv_current"]
            logs.JARVIS_DIR = orig["logs_dir"]
            logs.LOG_DIR = orig["logs_log_dir"]
            ai_client._spawn_title_update = orig["spawn"]


@contextmanager
def _fake_providers(first_error, first_keys=3):
    """Provider 'first' (N keys) always fails with `first_error`; provider
    'second' (1 key) answers. Yields the list of (provider, key) adapter calls."""
    orig_load = ai_config.load_ai_config
    orig_adapters = dict(ai_providers.ADAPTERS)
    calls = []
    cfg = {
        "persona": {},
        "providers": [
            {"name": "first", "type": "fake", "enabled": True,
             "api_keys": [f"k{i}" for i in range(1, first_keys + 1)], "model": "m"},
            {"name": "second", "type": "fake", "enabled": True, "api_keys": ["z1"], "model": "m"},
        ],
        "defaults": {"tools_enabled": False, "prompt_mode": "compact"},
    }

    def fake_adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append((resolved.get("name"), resolved.get("api_key")))
        if resolved.get("name") == "first":
            return ai_providers.AIResult(False, error=first_error)
        return ai_providers.AIResult(True, text="answered by second")

    ai_config.load_ai_config = lambda: cfg
    ai_providers.ADAPTERS["fake"] = fake_adapter
    try:
        yield calls
    finally:
        ai_config.load_ai_config = orig_load
        ai_providers.ADAPTERS.clear()
        ai_providers.ADAPTERS.update(orig_adapters)


def test_request_shape_error_skips_the_providers_remaining_keys():
    with _isolated_ask_env() as conv_id, _fake_providers(TOOL_CHOICE_NONE, first_keys=3) as calls:
        result = ai_client.ask("hello", commands=[], conversation_id=conv_id)
        first_calls = [c for c in calls if c[0] == "first"]
        check("the request-shaped provider was tried on ONE key, not three", len(first_calls) == 1, calls)
        check("ask() moved on to the next provider and it answered",
              result.ok is True and result.text == "answered by second", (result.ok, result.text))
        first_attempt = [a for a in result.attempts if a[0].startswith("first")]
        check("the skipped keys are recorded in the attempt note",
              len(first_attempt) == 1 and "skipping this provider's other 2 key(s)" in first_attempt[0][1],
              first_attempt)
        check("the original error text is still in the note", "Tool choice is none" in first_attempt[0][1])


def test_schema_mismatch_is_treated_the_same_way():
    with _isolated_ask_env() as conv_id, _fake_providers(SCHEMA_MISMATCH, first_keys=4) as calls:
        result = ai_client.ask("hello", commands=[], conversation_id=conv_id)
        check("four keys, one attempt", len([c for c in calls if c[0] == "first"]) == 1, calls)
        check("still answered by the next provider", result.ok is True)


def test_ordinary_failures_still_rotate_through_every_key():
    with _isolated_ask_env() as conv_id, _fake_providers("rate limited or quota exceeded (HTTP 429)",
                                                         first_keys=3) as calls:
        result = ai_client.ask("hello", commands=[], conversation_id=conv_id)
        check("a 429 tries all three keys before moving on",
              [c[1] for c in calls if c[0] == "first"] == ["k1", "k2", "k3"], calls)
        check("and then the next provider answers", result.ok is True)
        first_attempts = [a for a in result.attempts if a[0].startswith("first")]
        check("no skip note is attached to ordinary failures",
              all("skipping" not in a[1] for a in first_attempts), first_attempts)


def test_invalid_key_400_still_rotates():
    with _isolated_ask_env() as conv_id, _fake_providers("HTTP 400: API key not valid. Please pass a valid API key.",
                                                         first_keys=2) as calls:
        ai_client.ask("hello", commands=[], conversation_id=conv_id)
        check("an invalid-key 400 tries the second key", len([c for c in calls if c[0] == "first"]) == 2, calls)


def test_last_key_failure_gets_no_misleading_skip_note():
    # One key configured: there is nothing left to skip, so the note would be false.
    with _isolated_ask_env() as conv_id, _fake_providers(TOOL_CHOICE_NONE, first_keys=1) as calls:
        result = ai_client.ask("hello", commands=[], conversation_id=conv_id)
        first_attempt = [a for a in result.attempts if a[0].startswith("first")]
        check("single-key provider: error recorded unannotated",
              len(first_attempt) == 1 and "skipping" not in first_attempt[0][1], first_attempt)
        check("and the next provider still answers", result.ok is True)


for fn in [
    test_classifier_recognizes_the_logged_request_shape_errors,
    test_classifier_leaves_key_problems_alone,
    test_request_shape_error_skips_the_providers_remaining_keys,
    test_schema_mismatch_is_treated_the_same_way,
    test_ordinary_failures_still_rotate_through_every_key,
    test_invalid_key_400_still_rotates,
    test_last_key_failure_gets_no_misleading_skip_note,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
