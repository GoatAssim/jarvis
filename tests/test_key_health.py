"""Tests for F.9: nothing remembered which keys / models were unhealthy.

Every `jarvis` call is a new process, and provider_keys() returned keys in a
fixed order — so the next ask started on the key that had just returned a 429
(or the model that had just returned a 503). key_health.py keeps that memory on
disk; ask() uses it to reorder (never remove) keys and providers, to stop
rotating keys on a model-wide 503, and to stop trying a sibling provider whose
endpoint just refused a connection.

Run: python3 tests/test_key_health.py
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

from jarvis import ai_client, ai_config, ai_providers, conversations, key_health as kh, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _reset():
    try:
        kh.HEALTH_FILE.unlink()
    except OSError:
        pass


T = 1_000_000.0


def test_parse_retry_delay():
    p = kh.parse_retry_delay
    check("gemini body: retryDelay", p('{"retryDelay": "26s"}') == 26.0)
    check("gemini message: retry in 26.06s", abs(p("Please retry in 26.06s.") - 26.06) < 1e-9)
    check("openai/groq: try again in 2m3.5s", p("Please try again in 2m3.5s") == 123.5)
    check("openai/groq: try again in 20s", p("try again in 20s") == 20.0)
    check("Retry-After header wins", p("nothing", {"Retry-After": "7"}) == 7.0)
    check("nothing stated -> None", p("rate limited") is None and p(None) is None)


def test_status_reason_carries_the_stated_delay():
    class R:
        status_code = 429
        text = '{"error": {"message": "quota. Please retry in 26.06s."}}'
        headers = {}

    reason = ai_providers._status_reason(R())
    check("429 message includes the delay", "[retry in 26.06s]" in reason, reason)
    check("...and is still classified as a key failure", ai_providers.classify_failure(reason) == ai_providers.KIND_KEY)

    class R2(R):
        text = "nope"

    check("no stated delay -> the old message, unchanged", ai_providers._status_reason(R2()) == "rate limited or quota exceeded (HTTP 429)")


def test_cooldown_ordering_and_readmission():
    _reset()
    keys = ["k1", "k2", "k3"]
    kh.record_failure("gem", "m", "k1", "key", "rate limited or quota exceeded (HTTP 429) [retry in 26s]", now=T)
    order = kh.order_keys("gem", keys, now=T + 1)
    check("the 429'd key goes last, others keep their order", order == ["k2", "k3", "k1"], order)
    check("before its deadline it is still cooling", kh.cooldown_until("gem", "k1", now=T + 25) > 0)
    check("a retryDelay past its deadline re-admits it", kh.order_keys("gem", keys, now=T + 27) == keys)
    kh.record_failure("gem", "m", "k2", "key", "rate limited or quota exceeded (HTTP 429)", now=T)
    check("no stated delay -> default cooldown", 55 < kh.cooldown_until("gem", "k2", now=T) - T <= 60)
    kh.record_failure("gem", "m", "k3", "key", "invalid or unauthorized API key (HTTP 401)", now=T)
    check("a rejected key cools for an hour", kh.cooldown_until("gem", "k3", now=T) - T >= 3599)
    all_cool = kh.order_keys("gem", keys, now=T + 1)
    check("if everything is cooling the keys are still all returned, soonest first",
          sorted(all_cool) == sorted(keys) and all_cool[0] == "k1", all_cool)


def test_last_good_leads_and_success_clears():
    _reset()
    kh.record_failure("gem", "m", "k1", "key", "rate limited or quota exceeded (HTTP 429)", now=T)
    kh.record_success("gem", "m", "k2", now=T + 1)
    check("start at the key that last worked", kh.order_keys("gem", ["k1", "k2", "k3"], now=T + 2)[0] == "k2")
    kh.record_success("gem", "m", "k1", now=T + 3)
    check("a success clears the key's cooldown", kh.cooldown_until("gem", "k1", now=T + 4) == 0)


def test_overload_is_per_model_not_per_key():
    _reset()
    kh.record_failure("gem", "flash", "k1", "overload", "provider server error (HTTP 503)", now=T)
    check("503 cools the MODEL", kh.model_cooling("gem", "flash", now=T + 1) and not kh.model_cooling("gem", "flash", now=T + 60))
    check("...and leaves every key alone", kh.order_keys("gem", ["k1", "k2"], now=T + 1) == ["k1", "k2"])
    check("other models are unaffected", not kh.model_cooling("gem", "pro", now=T + 1))


def test_file_hygiene():
    _reset()
    kh.record_failure("gem", "m", "SECRET-KEY-VALUE", "key", "rate limited or quota exceeded (HTTP 429)", now=T)
    raw = kh.HEALTH_FILE.read_text()
    check("raw keys are never written", "SECRET-KEY-VALUE" not in raw and "gem:" in raw)
    kh.HEALTH_FILE.write_text("{not json")
    check("a corrupt file means no memory, not a crash", kh.order_keys("gem", ["a", "b"]) == ["a", "b"] and kh.cooldown_until("gem", "a") == 0)
    kh.record_failure("gem", "m", "a", "key", "rate limited or quota exceeded (HTTP 429)", now=T)
    check("...and it heals on the next write", json.loads(kh.HEALTH_FILE.read_text())["keys"])
    check("a single key / keyless provider is left alone", kh.order_keys("gem", ["only"]) == ["only"] and kh.order_keys("ollama", [None]) == [None])


@contextmanager
def _env(adapter, providers):
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS), ai_client._make_tool_executor,
            conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
            conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    cfg = {"persona": {}, "providers": providers, "defaults": {"tools_enabled": False, "prompt_mode": "full"}}
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
        ai_providers.ADAPTERS["ollama"] = adapter
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, _, ai_client._make_tool_executor, conversations.JARVIS_DIR,
             conversations.CONV_DIR, conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def _prov(name, keys, **extra):
    return {"name": name, "type": "fake", "enabled": True, "api_keys": keys, "model": "m", **extra}


def test_two_asks_second_skips_the_key_that_returned_429():
    _reset()
    used = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        used.append(resolved["api_key"])
        if resolved["api_key"] == "k1":
            # Delay stated here is deliberately > defaults.max_429_wait_seconds
            # (30s, see D5 / _short_429_wait_seconds in ai_client.py) so this
            # rotates immediately instead of retrying k1 in place — this test
            # is about cross-ask cooldown bookkeeping, not the D5 retry path
            # (that's covered by tests/test_short_429_wait.py, which mocks
            # time.sleep; this file's _env doesn't, so a qualifying delay
            # here would make the suite actually sleep it out).
            return ai_providers.AIResult(False, error="rate limited or quota exceeded (HTTP 429) [retry in 45s]")
        return ai_providers.AIResult(True, text="hi")

    with _env(adapter, [_prov("gem", ["k1", "k2"])]) as conv:
        r1 = ai_client.ask("hello", commands=[], conversation_id=conv)
        first_ask = list(used)
        used.clear()
        r2 = ai_client.ask("hello again", commands=[], conversation_id=conv)
    check("ask 1: k1 429'd, k2 answered", r1.ok and first_ask == ["k1", "k2"], first_ask)
    check("ask 2: starts on k2, never touches the cooling k1", r2.ok and used == ["k2"], used)


def test_overload_stops_key_rotation_and_demotes_the_provider():
    _reset()
    used = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        used.append((resolved["name"], resolved["api_key"]))
        if resolved["name"] == "busy":
            return ai_providers.AIResult(False, error="provider server error (HTTP 503)")
        return ai_providers.AIResult(True, text="ok")

    with _env(adapter, [_prov("busy", ["a", "b", "c"]), _prov("calm", ["x"])]) as conv:
        r1 = ai_client.ask("q1", commands=[], conversation_id=conv)
        first = list(used)
        used.clear()
        r2 = ai_client.ask("q2", commands=[], conversation_id=conv)
    check("503: one attempt on the busy provider, not three", first == [("busy", "a"), ("calm", "x")], first)
    check("the skipped keys are explained in the attempt log", any("overloaded" in e for _l, e in r1.attempts), r1.attempts)
    check("next ask tries the healthy provider FIRST (busy is only demoted)", r2.ok and used == [("calm", "x")], used)


def test_refused_connection_skips_siblings_on_the_same_host():
    _reset()
    used = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        used.append(resolved["name"])
        if "11434" in resolved.get("base_url", ""):
            return ai_providers.AIResult(False, error="couldn't connect (network issue, or the service is down) (is Ollama installed and running?)")
        return ai_providers.AIResult(True, text="cloud")

    url = "http://localhost:11434/api/chat"
    provs = [_prov("ollama-a", [], type="ollama", base_url=url), _prov("ollama-b", [], type="ollama", base_url=url), _prov("cloud", ["k"], base_url="https://x.example/v1")]
    with _env(adapter, provs) as conv:
        r = ai_client.ask("hi", commands=[], conversation_id=conv)
    check("the second Ollama entry is never tried", used == ["ollama-a", "cloud"], used)
    check("and the attempt log says why", any("skipped" in e and "11434" in e for _l, e in r.attempts), r.attempts)


def test_inner_agent_uses_a_different_key_and_reports_failures():
    _reset()
    check("spread: two ready keys -> the second first", kh.spread_keys("gem", ["a", "b", "c"]) == ["b", "a", "c"])
    check("spread: one ready key -> unchanged", kh.spread_keys("gem", ["a"]) == ["a"] and kh.spread_keys("gem", [None]) == [None])
    kh.record_failure("gem", "m", "a", "key", "rate limited or quota exceeded (HTTP 429)", now=T)
    check("spread: a cooling key is not promoted", kh.spread_keys("gem", ["a", "b"], now=T + 1) == ["b", "a"])
    _reset()
    from jarvis.actions import code_agent
    used = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        used.append(resolved["api_key"])
        if resolved["api_key"] == "b":
            return ai_providers.AIResult(False, error="rate limited or quota exceeded (HTTP 429) [retry in 30s]")
        return ai_providers.AIResult(True, text="edited")

    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS))
    ai_config.load_ai_config = lambda: {"persona": {}, "providers": [_prov("gem", ["a", "b"])],
                                        "defaults": {"tools_enabled": True}}
    ai_providers.ADAPTERS["fake"] = adapter
    try:
        text, err = code_agent._run_agent_loop("do it", [], lambda n, a: {}, 4)
    finally:
        ai_config.load_ai_config = orig[0]
        ai_providers.ADAPTERS.clear()
        ai_providers.ADAPTERS.update(orig[1])
    check("inner loop started on the SECOND key, fell back to the first", used == ["b", "a"] and text == "edited", (used, text, err))
    check("the key the inner loop burned is parked for the outer ask", kh.cooldown_until("gem", "b") > 0)


for fn in (test_parse_retry_delay, test_status_reason_carries_the_stated_delay, test_cooldown_ordering_and_readmission,
           test_last_good_leads_and_success_clears, test_overload_is_per_model_not_per_key, test_file_hygiene,
           test_two_asks_second_skips_the_key_that_returned_429,
           test_overload_stops_key_rotation_and_demotes_the_provider,
           test_refused_connection_skips_siblings_on_the_same_host,
           test_inner_agent_uses_a_different_key_and_reports_failures):
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
