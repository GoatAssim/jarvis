"""L.24 round 2: the three token-cost items that were still open after
l24-token-cost-fixes.patch (2026-10-09g).

  T2  discovery-round METRIC   RoundBudget.discovery_rounds -> AskUsage.to_dict()
                               ["discovery_rounds"] -> format_ask_total
  T4  provider RETRY HINT      a 5xx carries "[retry in Ns]" like a 429 always did,
                               and key_health parks an overloaded model for the
                               delay the provider STATED (not a fixed 45 s)
  T4  INTERACTIVE CAP          defaults.interactive_token_budget: the per-ask ledger
                               limit for an ask with a person watching (off by default)

What this file does NOT change, and pins so nobody "fixes" it by accident: the
same-key wait before rotating (D5, test_short_429_wait.py) is for a 429 only.
A 503 that states a delay is parked, never waited out on the same key.

Run: python3 tests/test_l24_discovery_retry_cap.py
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

from jarvis import (ai_client, ai_config, ai_providers, conversations, key_health,  # noqa: E402
                    logs, token_usage)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class Env:
    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


CLEAN = dict(JARVIS_SCHEDULED=None, JARVIS_CONTEXT=None, JARVIS_TOKEN_BUDGET=None)

# ---------------------------------------------------------------------------
# T2 -- the discovery-round metric
# ---------------------------------------------------------------------------
rb = ai_providers.RoundBudget(discovery_limit=2)
check("a fresh budget has counted no discovery round", rb.discovery_rounds == 0)
rb.take(["get_tool_schema"])
rb.take(["search_tools"])
rb.take(["load_skill"])            # the free pool (2) is spent: this one is charged, but still discovery
check("every discovery-only round is counted, whichever pool paid for it", rb.discovery_rounds == 3,
      (rb.discovery_rounds, rb.discovery_used, rb.used))
rb.take(["list_windows"])
check("a real tool round is not a discovery round", rb.discovery_rounds == 3, rb.discovery_rounds)
rb.take(["get_tool_schema", "list_windows"])
check("a round that mixes in real work is not counted (free calls never hide work)",
      rb.discovery_rounds == 3, rb.discovery_rounds)
rb.take(["definitely_not_a_tool"])
check("a made-up tool name is a discovery round, as RoundBudget already treats it",
      rb.discovery_rounds == 4, rb.discovery_rounds)
rb.take()
check("take() with no names (every older caller) never counts", rb.discovery_rounds == 4, rb.discovery_rounds)

small = ai_providers.RoundBudget(limit=1)
small.take(["list_windows"])
check("a refused round is not counted: nothing was sent",
      small.take(["get_tool_schema"]) is False and small.discovery_rounds == 0, small.discovery_rounds)

# metric is only a counter: the breaker's emptying of the pools does not touch it
before = rb.discovery_rounds
ai_client._stop_unattended_run(rb)
check("the circuit breaker leaves the metric alone", rb.discovery_rounds == before, rb.discovery_rounds)

au = token_usage.AskUsage()
au.add("gemini (key 1/2)", {"rounds": [{"round": 0, "input_tokens": 100, "output_tokens": 5}]}, ok=True)
check("an AskUsage nobody bound a budget to reports no discovery_rounds key (shape unchanged)",
      "discovery_rounds" not in au.to_dict(), au.to_dict())
au.bind_rounds(rb)
check("a bound AskUsage reports the live count", au.to_dict().get("discovery_rounds") == rb.discovery_rounds, au.to_dict())
rb.take(["search_tools"])
check("...and it follows the count while the ask runs", au.to_dict()["discovery_rounds"] == rb.discovery_rounds)
check("format_ask_total names it when there were some",
      "looking for a tool" in token_usage.format_ask_total(au.to_dict()), token_usage.format_ask_total(au.to_dict()))
quiet = token_usage.AskUsage()
quiet.add("x", {"rounds": [{"round": 0, "input_tokens": 1, "output_tokens": 1}]}, ok=True)
quiet.bind_rounds(ai_providers.RoundBudget())
check("...and stays quiet at zero", "looking for a tool" not in token_usage.format_ask_total(quiet.to_dict()))
_foreign = token_usage.AskUsage()
_foreign.add("x", {"rounds": [{"round": 0, "input_tokens": 1, "output_tokens": 1}]}, ok=True)
_foreign.bind_rounds(object())
check("a foreign object bound as the source never breaks to_dict() and adds no key",
      "discovery_rounds" not in _foreign.to_dict() and _foreign.to_dict()["input_tokens"] == 1, _foreign.to_dict())

# ---------------------------------------------------------------------------
# T4 -- the provider's retry hint on a 5xx
# ---------------------------------------------------------------------------
class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


reason = ai_providers._status_reason
check("a 503 with Retry-After carries the hint, like a 429",
      reason(Resp(503, "", {"Retry-After": "7"})) == "provider server error (HTTP 503) [retry in 7s]",
      reason(Resp(503, "", {"Retry-After": "7"})))
check("a 503 with Gemini's retryDelay in the body carries it",
      "[retry in 12s]" in reason(Resp(503, '{"error": {"details": [{"retryDelay": "12s"}]}}')),
      reason(Resp(503, '{"error": {"details": [{"retryDelay": "12s"}]}}')))
check("a 503 that states nothing reads exactly as before",
      reason(Resp(503, "overloaded")) == "provider server error (HTTP 503)", reason(Resp(503, "overloaded")))
check("a 529 (Anthropic's overloaded) is an overload too, hint included",
      reason(Resp(529, "", {"retry-after": "3"})) == "provider server error (HTTP 529) [retry in 3s]",
      reason(Resp(529, "", {"retry-after": "3"})))
check("the 429 text is untouched",
      reason(Resp(429, "", {"Retry-After": "9"})) == "rate limited or quota exceeded (HTTP 429) [retry in 9s]",
      reason(Resp(429, "", {"Retry-After": "9"})))
check("a 2xx is still no reason", reason(Resp(200)) is None)
check("a 4xx other than the named ones keeps its snippet form", reason(Resp(418, "teapot")) == "HTTP 418: teapot")
check("classify_failure still files the hinted text under overload",
      ai_providers.classify_failure(reason(Resp(503, "", {"Retry-After": "7"}))) == ai_providers.KIND_OVERLOAD)

# D5 is key-only, deliberately: pinned again here so a 5xx hint cannot start a same-key wait unnoticed
check("D5 unchanged: a 503 that states a delay is NOT waited out on the same key",
      ai_client._short_429_wait_seconds(ai_providers.KIND_OVERLOAD,
                                        "provider server error (HTTP 503) [retry in 5s]", cap=30) is None)

# key_health parks the model for the STATED delay
def _fresh():
    try:
        key_health.HEALTH_FILE.unlink()
    except OSError:
        pass


_fresh()
t0 = 1_000_000.0
key_health.record_failure("gem", "m1", "k", ai_providers.KIND_OVERLOAD,
                          "provider server error (HTTP 503) [retry in 120s]", now=t0)
check("an overloaded model is parked for the stated 120 s, not the fixed 45",
      key_health.model_cooling("gem", "m1", now=t0 + 100) and not key_health.model_cooling("gem", "m1", now=t0 + 121))
_fresh()
key_health.record_failure("gem", "m2", "k", ai_providers.KIND_OVERLOAD, "provider server error (HTTP 503)", now=t0)
check("no stated delay: the old fixed window (45 s) is kept",
      key_health.model_cooling("gem", "m2", now=t0 + 44) and not key_health.model_cooling("gem", "m2", now=t0 + 46))
_fresh()
key_health.record_failure("gem", "m3", "k", ai_providers.KIND_OVERLOAD,
                          "provider server error (HTTP 503) [retry in 1s]", now=t0)
check("a stated delay below 5 s still parks for 5 s (never hammer a busy host)",
      key_health.model_cooling("gem", "m3", now=t0 + 4) and not key_health.model_cooling("gem", "m3", now=t0 + 6))
_fresh()
key_health.record_failure("gem", "m4", "k", ai_providers.KIND_OVERLOAD,
                          "provider server error (HTTP 503) [retry in 99999s]", now=t0)
check("...and one above the ceiling is clamped to it",
      key_health.model_cooling("gem", "m4", now=t0 + key_health.MAX_COOLDOWN - 1)
      and not key_health.model_cooling("gem", "m4", now=t0 + key_health.MAX_COOLDOWN + 1))
_fresh()
key_health.record_failure("gem", "m5", "k", ai_providers.KIND_OVERLOAD, "provider server error (HTTP 503)",
                          retry_after=20, now=t0)
check("an explicit retry_after argument wins over the text",
      key_health.model_cooling("gem", "m5", now=t0 + 19) and not key_health.model_cooling("gem", "m5", now=t0 + 21))
_fresh()

# ---------------------------------------------------------------------------
# T4 -- the interactive cap
# ---------------------------------------------------------------------------
lim = ai_client._token_budget_limit
with Env(**CLEAN):
    check("interactive: no setting means no limit (the shipped default)", lim({}) is None)
    check("interactive: defaults.interactive_token_budget sets it", lim({"interactive_token_budget": 8000}) == 8000)
    check("interactive: 0 means off", lim({"interactive_token_budget": 0}) is None)
    check("interactive: a garbled value means off (its default), not some other number",
          lim({"interactive_token_budget": "lots"}) is None and lim({"interactive_token_budget": None}) is None
          and lim({"interactive_token_budget": -5}) is None)
    check("interactive: a bare true is not a limit of 1 token", lim({"interactive_token_budget": True}) is None)
    check("interactive: the scheduled setting does not leak into an interactive ask",
          lim({"scheduled_token_budget": 5}) is None)
with Env(**{**CLEAN, "JARVIS_TOKEN_BUDGET": "777"}):
    check("interactive: JARVIS_TOKEN_BUDGET is the scheduler's per-run knob and is not read here",
          lim({}) is None and lim({"interactive_token_budget": 500}) == 500)
with Env(**{**CLEAN, "JARVIS_SCHEDULED": "1"}):
    check("scheduled: the interactive setting does not change a scheduled run (still 30,000)",
          lim({"interactive_token_budget": 500}) == 30000)

tb = ai_providers.TokenBudget(8000)
tb.add({"input_tokens": 9100})
interactive_text = ai_client._token_budget_reply(tb, [{"name": "list_windows"}], interactive=True)
scheduled_text = ai_client._token_budget_reply(tb, [{"name": "list_windows"}])
check("the interactive stop reply says request, not scheduled run, and names ITS setting",
      "this request used 9,100 tokens" in interactive_text and "limit of 8,000" in interactive_text
      and "defaults.interactive_token_budget" in interactive_text and "scheduled" not in interactive_text
      and "\n" not in interactive_text, interactive_text)
check("the scheduled stop reply is byte-for-byte what it was",
      scheduled_text.startswith("Stopped: this scheduled run used 9,100 tokens, over its limit of 8,000")
      and scheduled_text.endswith("defaults.scheduled_token_budget in the AI config (0 turns the limit off)."),
      scheduled_text)


@contextmanager
def _env(adapter, providers, defaults_extra=None):
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS), conversations.JARVIS_DIR,
            conversations.CONV_DIR, conversations.INDEX_FILE, conversations.CURRENT_FILE,
            logs.JARVIS_DIR, logs.LOG_DIR, ai_client.time.sleep)
    defaults = {"tools_enabled": False, "prompt_mode": "full"}
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
        ai_providers.ADAPTERS["fake"] = adapter
        ai_client.time.sleep = lambda s: None
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, _, conversations.JARVIS_DIR, conversations.CONV_DIR,
             conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR,
             ai_client.time.sleep) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def _prov(name, keys):
    return {"name": name, "type": "fake", "enabled": True, "api_keys": keys, "model": "m"}


def _spend_then_fail(calls, tokens):
    """An adapter that burns `tokens` on its request, then fails over-quota."""
    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved["api_key"])
        ai_providers._record_usage("openai_compatible",
                                   {"usage": {"prompt_tokens": tokens, "completion_tokens": 1}}, 0)
        if len(calls) == 1:
            return ai_providers.AIResult(False, error="rate limited or quota exceeded (HTTP 429) [retry in 99s]")
        return ai_providers.AIResult(True, text="fine")
    return adapter


try:
    key_health.HEALTH_FILE.unlink()
except OSError:
    pass
calls = []
with Env(**CLEAN), _env(_spend_then_fail(calls, 6000), [_prov("p", ["k1", "k2"])],
                        {"interactive_token_budget": 5000}) as conv:
    r = ai_client.ask("hello", commands=[], conversation_id=conv)
check("interactive cap: the first attempt spent past the limit, so no second attempt starts",
      calls == ["k1"], calls)
check("interactive cap: the ask ends as a token-limit stop and says so in plain words",
      r.ok and r.ending == "token_budget" and "this request used" in (r.text or "")
      and "interactive_token_budget" in (r.text or ""), (r.ok, r.ending, r.text))
check("interactive cap: the ledger reports the limit and that it was exceeded",
      (r.usage_total or {}).get("limit") == 5000 and (r.usage_total or {}).get("exceeded") is True, r.usage_total)

try:
    key_health.HEALTH_FILE.unlink()
except OSError:
    pass
calls = []
with Env(**CLEAN), _env(_spend_then_fail(calls, 6000), [_prov("p", ["k1", "k2"])]) as conv:
    r = ai_client.ask("hello", commands=[], conversation_id=conv)
check("no setting: exactly the old behaviour -- the failover goes on and answers",
      calls == ["k1", "k2"] and r.ok and r.text == "fine" and r.ending != "token_budget", (calls, r.ending, r.text))
check("...and the ledger carries no limit", (r.usage_total or {}).get("limit") is None, r.usage_total)
check("...and the per-ask total counts both attempts' spend",
      ((r.usage or {}).get("ask_total") or {}).get("input_tokens") == 12000, r.usage)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
