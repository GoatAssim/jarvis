"""Tests for decision D5 (master plan F.9/F.16): on a 429 that states a
SHORT retry delay, wait it out once on the SAME key before rotating,
instead of abandoning the attempt immediately. F.9's evidence: Case 2b
started on the key that had just 503'd/429'd in the previous case, with no
memory of it — key_health.py fixed the "no memory" half; this is the "is
rotating immediately even the right move" half that F.9 left as D5.

Only a genuine rate-limit/quota failure (still ai_providers.KIND_KEY) with
a STATED delay of at most defaults.max_429_wait_seconds (default 30)
qualifies. A bad/rejected key (401/403 — also KIND_KEY, but nothing to wait
out), an unstated delay, or a delay longer than the cap all rotate to the
next key immediately, exactly as before this change. At most one retry per
key: if the retry also 429s, that key is done and rotation proceeds as
usual.

Run: python3 tests/test_short_429_wait.py
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

from jarvis import ai_client, ai_config, ai_providers, conversations, key_health as kh, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _reset_key_health():
    try:
        kh.HEALTH_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Pure unit tests of _short_429_wait_seconds — no ask()/adapter machinery.
# ---------------------------------------------------------------------------

def test_short_stated_delay_qualifies():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_KEY, "rate limited or quota exceeded (HTTP 429) [retry in 5s]", cap=30)
    check("5s stated delay, cap 30 -> waits 5s", wait == 5.0, wait)


def test_delay_longer_than_cap_does_not_qualify():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_KEY, "rate limited (HTTP 429) [retry in 45s]", cap=30)
    check("45s stated delay, cap 30 -> no wait", wait is None, wait)


def test_unstated_delay_does_not_qualify():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_KEY, "rate limited or quota exceeded (HTTP 429)", cap=30)
    check("no stated delay -> no wait", wait is None, wait)


def test_bad_key_does_not_qualify_even_with_a_number_nearby():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_KEY, "invalid or unauthorized api key (HTTP 401)", cap=30)
    check("bad key (not rate-limit/quota text) -> no wait", wait is None, wait)


def test_non_key_kind_never_qualifies():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_OVERLOAD, "provider server error (HTTP 503) [retry in 5s]", cap=30)
    check("KIND_OVERLOAD -> no wait, D5 is key-only", wait is None, wait)


def test_cap_zero_disables_it():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_KEY, "rate limited (HTTP 429) [retry in 1s]", cap=0)
    check("cap=0 -> disabled even for a 1s delay", wait is None, wait)


def test_quota_wording_also_qualifies():
    wait = ai_client._short_429_wait_seconds(
        ai_providers.KIND_KEY, "quota exceeded, retry in 10s", cap=30)
    check("'quota' wording qualifies same as 'rate limited'", wait == 10.0, wait)


for fn in [
    test_short_stated_delay_qualifies,
    test_delay_longer_than_cap_does_not_qualify,
    test_unstated_delay_does_not_qualify,
    test_bad_key_does_not_qualify_even_with_a_number_nearby,
    test_non_key_kind_never_qualifies,
    test_cap_zero_disables_it,
    test_quota_wording_also_qualifies,
]:
    fn()


# ---------------------------------------------------------------------------
# Integration tests through ask()'s real failover loop, with a fake adapter
# and ai_client.time.sleep monkeypatched so nothing actually waits.
# ---------------------------------------------------------------------------

@contextmanager
def _env(adapter, providers, defaults_extra=None):
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS),
            conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
            conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR, ai_client.time.sleep)
    defaults = {"tools_enabled": False, "prompt_mode": "full"}
    defaults.update(defaults_extra or {})
    cfg = {"persona": {}, "providers": providers, "defaults": defaults}
    sleeps = []
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
        ai_client.time.sleep = lambda s: sleeps.append(s)
        try:
            yield conversations.get_current_id(), sleeps
        finally:
            (ai_config.load_ai_config, _, conversations.JARVIS_DIR,
             conversations.CONV_DIR, conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR, ai_client.time.sleep) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def _prov(name, keys, **extra):
    return {"name": name, "type": "fake", "enabled": True, "api_keys": keys, "model": "m", **extra}


def test_short_429_retries_same_key_and_succeeds():
    _reset_key_health()
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        if len(calls) == 1:
            return ai_providers.AIResult(False, error="rate limited or quota exceeded (HTTP 429) [retry in 3s]")
        return ai_providers.AIResult(True, text="hi")

    with _env(adapter, [_prov("gem", ["k1"])]) as (conv, sleeps):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("ask succeeds after the retry", r.ok, r)
    check("same key used both times", calls == ["k1", "k1"], calls)
    check("slept for the stated 3s", sleeps == [3.0], sleeps)


def test_long_429_delay_rotates_immediately_without_waiting():
    _reset_key_health()
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        if resolved["api_key"] == "k1":
            return ai_providers.AIResult(False, error="rate limited (HTTP 429) [retry in 45s]")
        return ai_providers.AIResult(True, text="hi")

    with _env(adapter, [_prov("gem", ["k1", "k2"])]) as (conv, sleeps):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("ask succeeds on the second key", r.ok, r)
    check("each key tried exactly once, no retry on k1", calls == ["k1", "k2"], calls)
    check("never slept — delay exceeded the cap", sleeps == [], sleeps)


def test_bad_key_rotates_immediately_without_waiting():
    _reset_key_health()
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        if resolved["api_key"] == "k1":
            return ai_providers.AIResult(False, error="invalid or unauthorized api key (HTTP 401)")
        return ai_providers.AIResult(True, text="hi")

    with _env(adapter, [_prov("gem", ["k1", "k2"])]) as (conv, sleeps):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("ask succeeds on the second key", r.ok, r)
    check("no retry on a bad key", calls == ["k1", "k2"], calls)
    check("never slept", sleeps == [], sleeps)


def test_max_429_wait_seconds_zero_disables_the_retry():
    _reset_key_health()
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        return ai_providers.AIResult(False, error="rate limited (HTTP 429) [retry in 2s]")

    with _env(adapter, [_prov("gem", ["k1"])], {"max_429_wait_seconds": 0}) as (conv, sleeps):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("ask fails (only key, retry disabled)", r.ok is False)
    check("adapter called exactly once, no retry", calls == ["k1"], calls)
    check("never slept", sleeps == [], sleeps)


def test_at_most_one_retry_then_rotates():
    _reset_key_health()
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        if resolved["api_key"] == "k1":
            return ai_providers.AIResult(False, error="rate limited (HTTP 429) [retry in 3s]")
        return ai_providers.AIResult(True, text="hi")

    with _env(adapter, [_prov("gem", ["k1", "k2"])]) as (conv, sleeps):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("ask succeeds on k2 after k1's retry also 429'd", r.ok, r)
    check("k1 tried twice (initial + one retry), then k2 once", calls == ["k1", "k1", "k2"], calls)
    check("slept exactly once, not twice", sleeps == [3.0], sleeps)


def test_default_cap_is_thirty_seconds():
    _reset_key_health()
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        if len(calls) == 1:
            return ai_providers.AIResult(False, error="rate limited (HTTP 429) [retry in 30s]")
        return ai_providers.AIResult(True, text="hi")

    with _env(adapter, [_prov("gem", ["k1"])]) as (conv, sleeps):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("a delay exactly at the default 30s cap still retries", r.ok, r)
    check("slept for the full 30s", sleeps == [30.0], sleeps)


for fn in [
    test_short_429_retries_same_key_and_succeeds,
    test_long_429_delay_rotates_immediately_without_waiting,
    test_bad_key_rotates_immediately_without_waiting,
    test_max_429_wait_seconds_zero_disables_the_retry,
    test_at_most_one_retry_then_rotates,
    test_default_cap_is_thirty_seconds,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)