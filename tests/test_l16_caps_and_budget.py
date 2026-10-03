"""L.16 items 7 and 8, at unit level: the list_windows cap and the per-ask
token ledger / budget. The end-to-end behaviour through the real ask() loop is
tests/test_l16_replay.py; this file pins the pieces it relies on.

Run: python3 tests/test_l16_caps_and_budget.py
"""
import inspect
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_providers, desktop_tools, token_usage  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
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


# ---------------------------------------------------------------------------
# Item 7 -- list_windows is bounded
# ---------------------------------------------------------------------------
class FakeWin:
    def __init__(self, title, active=False, minimized=False):
        self.title, self.left, self.top, self.width, self.height = title, 0, 0, 800, 600
        self.isActive, self.isMinimized = active, minimized


class FakeGW:
    def __init__(self, wins):
        self.wins = wins

    def getAllWindows(self):
        return self.wins


def list_with(wins):
    saved = desktop_tools.gw
    desktop_tools.gw = FakeGW(wins)
    try:
        return desktop_tools.tool_list_windows({})
    finally:
        desktop_tools.gw = saved


small = list_with([FakeWin("Notepad", active=True), FakeWin("Settings"), FakeWin("  "), FakeWin("Steam", minimized=True)])
check("a short list is returned whole, with no truncation fields",
      small["count"] == 3 and len(small["windows"]) == 3
      and not any(k in small for k in ("truncated", "not_shown", "hint", "titles_cut")), small)

many = [FakeWin("Window %02d " % i + "x" * 200, minimized=(i % 3 == 0)) for i in range(40)]
many[27].isActive = True
many[27].isMinimized = False
many[27].title = "The active one " + "y" * 200
big = list_with(many)
n = desktop_tools.LIST_WINDOWS_MAX
check("a long list is capped at LIST_WINDOWS_MAX", len(big["windows"]) == n == 15, len(big["windows"]))
check("count still reports the true total, and the omission is stated",
      big["count"] == 40 and big["truncated"] is True and big["not_shown"] == 40 - n and "focus_window" in big["hint"], big)
check("the active window is listed first", big["windows"][0]["isActive"] is True
      and big["windows"][0]["title"].startswith("The active one"), big["windows"][0])
check("visible windows come before minimized ones when the cap bites",
      all(not w["isMinimized"] for w in big["windows"][:n]) or
      [w["isMinimized"] for w in big["windows"]] == sorted(w["isMinimized"] for w in big["windows"]),
      [w["isMinimized"] for w in big["windows"]])
check("every title is cut to LIST_WINDOW_TITLE_MAX and the cut is flagged",
      all(len(w["title"]) <= desktop_tools.LIST_WINDOW_TITLE_MAX for w in big["windows"]) and big["titles_cut"] is True,
      max(len(w["title"]) for w in big["windows"]))
check("a cut title is a plain prefix (no ellipsis), so focus_window's partial match still works",
      "\u2026" not in big["windows"][0]["title"] and "..." not in big["windows"][0]["title"]
      and big["windows"][0]["title"] in many[27].title, big["windows"][0]["title"])
check("the result is bounded: 40 windows with 200-char titles stay under 4,000 characters",
      len(json.dumps(big)) < 4000, len(json.dumps(big)))
short_title = list_with([FakeWin("A" * 80)])
check("a title of exactly the maximum is not flagged as cut",
      "titles_cut" not in short_title and short_title["windows"][0]["title"] == "A" * 80, short_title)
check("with pygetwindow missing the tool still reports that, unchanged",
      (lambda: (setattr(desktop_tools, "gw", None), desktop_tools.tool_list_windows({}))[1])().get("error"), None)
desktop_tools.gw = desktop_tools.gw  # (restored by list_with; the line above only probes)

# ---------------------------------------------------------------------------
# Item 8 -- the token ledger
# ---------------------------------------------------------------------------
TB = ai_providers.TokenBudget
b = TB(100)
b.add({"input_tokens": 60, "output_tokens": 10, "thinking_tokens": 30})
check("input + output + thinking all count", b.used == 100 and b.summary()["thinking_tokens"] == 30, b.summary())
check("exactly on the limit is not over it", b.exceeded() is False)
b.add({"input_tokens": 1})
check("one token past the limit is over it", b.exceeded() is True and b.summary()["exceeded"] is True)
check("no limit means count only, never exceeded",
      (lambda t: (t.add({"input_tokens": 10 ** 9}), t.exceeded())[1])(TB(None)) is False)
check("a zero or negative limit means no limit", TB(0).limit is None and TB(-5).limit is None and TB("x").limit is None)
check("add() ignores junk without raising", (lambda t: (t.add(None), t.add({"input_tokens": "abc"}), t.used)[2])(TB(5)) == 0)

# thread-local scoping
prev = ai_providers.set_token_budget(TB(10))
check("set_token_budget returns the previous ledger", prev is None or isinstance(prev, TB))
seen = {}
t = threading.Thread(target=lambda: seen.setdefault("other", ai_providers.get_token_budget()))
t.start(); t.join()
check("another thread does not see this thread's ledger", seen["other"] is None)
ai_providers.set_token_budget(None)

# _record_usage feeds it across attempt boundaries (set_log_context resets the
# per-attempt list, never the ledger)
ledger = TB(1000)
ai_providers.set_token_budget(ledger)
ai_providers.set_log_context(None, "attempt 1")
ai_providers._record_usage("gemini", {"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 5,
                                                         "thoughtsTokenCount": 40}}, 0)
ai_providers.set_log_context(None, "attempt 2")           # a failover: per-attempt list resets
ai_providers._record_usage("openai_compatible", {"usage": {"prompt_tokens": 200, "completion_tokens": 10}}, 0)
per_attempt = ai_providers.get_usage_summary()["total_tokens"]
ai_providers.clear_log_context()
check("the per-attempt summary only sees the latest attempt (the gap the ledger closes)", per_attempt == 210, per_attempt)
check("the ledger spans both attempts, thinking included", ledger.used == 355 and ledger.requests == 2, ledger.summary())
check("Gemini thinking tokens are reported as their own field",
      token_usage.extract_usage("gemini", {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2,
                                                             "thoughtsTokenCount": 9}})["thinking_tokens"] == 9)
check("...and absent when there are none, so existing entries keep their shape",
      "thinking_tokens" not in token_usage.extract_usage("gemini", {"usageMetadata": {"promptTokenCount": 1,
                                                                                      "candidatesTokenCount": 2}}))

# enforcement points
ai_providers.set_token_budget(TB(10))
ai_providers.get_token_budget().add({"input_tokens": 11})
rb = ai_providers.RoundBudget(discovery_limit=3, project_discovery_limit=2)
check("take() refuses every kind of round once over the limit",
      rb.take(["read_screen"]) is False and rb.take(["get_tool_schema"]) is False
      and rb.take(["search_files"]) is False and rb.take() is False and rb.used == 0, (rb.used, rb.discovery_used))
ai_providers.set_token_budget(TB(10))
check("take() behaves exactly as before while under the limit",
      ai_providers.RoundBudget().take() is True)

calls = []


def stub_adapter(provider, messages, timeout, **kw):
    calls.append(1)
    return ai_providers.AIResult(True, text="done")


ai_providers.set_token_budget(TB(10))
ai_providers.get_token_budget().add({"input_tokens": 11})
r = ai_providers._forced_ending(stub_adapter, {}, [{"role": "user", "content": "hi"}], 5, [], None,
                                ai_providers.RoundBudget(grace=True), {}, 3)
check("a forced ending makes NO closing call once over the limit",
      calls == [] and r.ok is False and r.kind == ai_providers.KIND_BUDGET, (calls, r.kind))
ai_providers.set_token_budget(TB(10))
r = ai_providers._forced_ending(stub_adapter, {}, [{"role": "user", "content": "hi"}], 5, [], None,
                                ai_providers.RoundBudget(grace=False), {}, 3)
check("...and still makes its one closing call while under the limit", calls == [1] and r.ok is True, (calls, r.ok))
ai_providers.set_token_budget(None)

# ---------------------------------------------------------------------------
# ai_client: the limit, its sources, and the ask() wrapper
# ---------------------------------------------------------------------------
lim = ai_client._token_budget_limit
clean = dict(JARVIS_SCHEDULED=None, JARVIS_CONTEXT=None, JARVIS_TOKEN_BUDGET=None)
with Env(**clean):
    check("an interactive ask has no limit, whatever the config says",
          lim({}) is None and lim({"scheduled_token_budget": 5}) is None)
with Env(**{**clean, "JARVIS_SCHEDULED": "1"}):
    check("a scheduled run defaults to 30,000", lim({}) == ai_client.SCHEDULED_TOKEN_BUDGET_DEFAULT == 30000)
    check("defaults.scheduled_token_budget sets it", lim({"scheduled_token_budget": 12345}) == 12345)
    check("0 in the config turns it off", lim({"scheduled_token_budget": 0}) is None)
    check("a garbled config value falls back to the default, not to 'no limit'",
          lim({"scheduled_token_budget": "lots"}) == 30000 and lim({"scheduled_token_budget": None}) == 30000)
with Env(**{**clean, "JARVIS_SCHEDULED": "1", "JARVIS_TOKEN_BUDGET": "777"}):
    check("JARVIS_TOKEN_BUDGET beats the config", lim({"scheduled_token_budget": 12345}) == 777)
with Env(**{**clean, "JARVIS_SCHEDULED": "1", "JARVIS_TOKEN_BUDGET": "0"}):
    check("JARVIS_TOKEN_BUDGET=0 turns it off for one run", lim({}) is None)
with Env(**{**clean, "JARVIS_SCHEDULED": "1", "JARVIS_TOKEN_BUDGET": "not a number"}):
    check("a garbled JARVIS_TOKEN_BUDGET falls back to the default", lim({}) == 30000)
with Env(**{**clean, "JARVIS_CONTEXT": "unattended"}):
    check("an unattended JARVIS_CONTEXT is limited too", lim({}) == 30000)

tb = TB(30000)
tb.add({"input_tokens": 31234})
text = ai_client._token_budget_reply(tb, [{"name": "get_tool_schema"}, {"name": "read_screen"}, {"name": "read_screen"}])
check("the stop reply: cause, numbers, what ran (once each, no discovery calls), the setting",
      "31,234 tokens" in text and "limit of 30,000" in text and "It had run: read_screen." in text
      and "get_tool_schema" not in text and "defaults.scheduled_token_budget" in text and "\n" not in text, text)
check("the stop reply says so when nothing had run", "Nothing had run yet." in ai_client._token_budget_reply(tb, []))

check("ask() keeps its public signature through the wrapper",
      "user_text" in inspect.signature(ai_client.ask).parameters
      and "sender_context" in inspect.signature(ai_client.ask).parameters
      and inspect.signature(ai_client.ask) == inspect.signature(ai_client._ask_impl))

outer = TB(50)
ai_providers.set_token_budget(outer)
orig_impl = ai_client._ask_impl
try:
    ai_client._ask_impl = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")) if k.get("explode") else \
        (ai_providers.set_token_budget(TB(1)), "inner result")[1]
    # (the wrapper looks _ask_impl up at call time, so the patch above is what it runs)
    out = ai_client.ask("x")
    check("ask() puts the caller's ledger back when it returns", out == "inner result"
          and ai_providers.get_token_budget() is outer)
    try:
        ai_client.ask("x", explode=True)
    except RuntimeError:
        pass
    check("...and when it raises", ai_providers.get_token_budget() is outer)
finally:
    ai_client._ask_impl = orig_impl
    ai_providers.set_token_budget(None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
