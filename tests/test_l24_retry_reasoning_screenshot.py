"""L.24 / L.16 token work, third pass. Three gaps that were still open after
jarvis-l24-discovery-retry-interactive-cap.patch and the L.16 preflight.

  1. RETRY HINT      key_health.parse_retry_delay only understood whole seconds
                     ("retry in 26s", "2m3.5s"). OpenAI states a short wait in
                     MILLISECONDS ("try again in 20ms") and sends `retry-after-ms`;
                     Retry-After may be an HTTP date. Those read as "stated nothing",
                     so a 20 ms hint parked the key for the 60 s default. Also:
                     `[retry in %g s]` printed 1,234,567 s as "1.23457e+06", which
                     nothing parses back.
  2. REASONING       token_usage assumed every OpenAI-compatible host counts
                     reasoning_tokens INSIDE completion_tokens. xAI bills them on
                     top, so the ledger / scheduled-run budget under-counted them.
                     The host's own total_tokens now tells the two apart.
  3. take_screenshot an UNATTENDED ask is no longer offered take_screenshot unless
                     the job asks for a screenshot (the model never sees its picture
                     and nobody watches the UI card). preflight.unoffered_tools() is
                     the one rule; ask() filters the schemas with it and the prompt
                     builder drops the blurb / pack-instruction text that names it.
                     Interactive asks are untouched.

Deliberately NOT changed, and pinned so nobody "fixes" it by accident: D5's
same-key wait stays 429-only (a 503 with a stated delay is parked, never waited
out); the interactive token cap stays OFF by default.

Run: python3 tests/test_l24_retry_reasoning_screenshot.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (ai_client, ai_config, ai_providers, conversations, key_health,  # noqa: E402
                    preflight, token_usage, tool_registry)
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def close(a, b, eps=1e-9):
    return a is not None and abs(a - b) < eps


# ---------------------------------------------------------------------------
# 1. The retry hint
# ---------------------------------------------------------------------------
p = key_health.parse_retry_delay

# the forms that already worked still read the same
check("unchanged: gemini retryDelay", p('{"retryDelay": "26s"}') == 26.0)
check("unchanged: retry in 26.06s", close(p("Please retry in 26.06s."), 26.06))
check("unchanged: try again in 2m3.5s", p("Please try again in 2m3.5s") == 123.5)
check("unchanged: try again in 20s", p("try again in 20s") == 20.0)
check("unchanged: Retry-After header beats the body", p("nothing", {"Retry-After": "7"}) == 7.0)
check("unchanged: nothing stated is None", p("rate limited") is None and p(None) is None and p("") is None)

# the forms that did not
check("OpenAI: try again in 20ms is 0.02 s", close(p("Rate limit reached. Please try again in 20ms."), 0.02),
      p("Please try again in 20ms."))
check("a space before ms is fine", close(p("retry in 500 ms"), 0.5), p("retry in 500 ms"))
check("1h2m3s is 3723 s", p("Please try again in 1h2m3s.") == 3723.0, p("Please try again in 1h2m3s."))
check("2m alone is 120 s", p("try again in 2m") == 120.0, p("try again in 2m"))
check("a spelled-out unit reads (1.5 seconds)", close(p("Please retry in 1.5 seconds"), 1.5))
check("minutes then ms are not confused (1m500ms)", close(p("try again in 1m500ms"), 60.5), p("try again in 1m500ms"))
check("words after the anchor with no number state nothing", p("retry in soon") is None and p("try again later") is None)
check("retry-after-ms header is milliseconds", close(p("x", {"retry-after-ms": "250"}), 0.25))
check("retry-after-ms beats Retry-After (it is the finer one)",
      close(p("x", {"retry-after-ms": "1500", "Retry-After": "2"}), 1.5))
check("header names are case-insensitive on a plain dict", p("x", {"RETRY-AFTER": "3"}) == 3.0)
check("a fractional Retry-After still reads", p("x", {"Retry-After": "7.5"}) == 7.5)
_date = "Wed, 21 Oct 2015 07:28:30 GMT"      # epoch 1445412510
check("Retry-After as an HTTP date is the wait from now", close(p("x", {"Retry-After": _date}, now=1445412480.0), 30.0),
      p("x", {"Retry-After": _date}, now=1445412480.0))
check("an HTTP date already past is 0, never negative", p("x", {"Retry-After": _date}, now=1445412600.0) == 0.0)
check("a garbled Retry-After falls through to the body", p("try again in 9s", {"Retry-After": "soon"}) == 9.0)
check("a garbled header and no body states nothing", p("", {"Retry-After": "soon"}) is None)
check("a header object that raises on lookup states nothing instead of raising",
      p("rate limited", type("H", (), {"get": lambda self, k: 1 / 0})()) is None)


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


reason = ai_providers._status_reason
check("a 429 stating 20ms carries [retry in 0.02s]",
      reason(Resp(429, '{"error": {"message": "Please try again in 20ms."}}'))
      == "rate limited or quota exceeded (HTTP 429) [retry in 0.02s]",
      reason(Resp(429, '{"error": {"message": "Please try again in 20ms."}}')))
check("a 503 with retry-after-ms carries it too",
      reason(Resp(503, "", {"retry-after-ms": "1500"})) == "provider server error (HTTP 503) [retry in 1.5s]",
      reason(Resp(503, "", {"retry-after-ms": "1500"})))
check("whole seconds read exactly as before",
      reason(Resp(429, "", {"Retry-After": "9"})) == "rate limited or quota exceeded (HTTP 429) [retry in 9s]")
big = reason(Resp(429, "", {"Retry-After": "1234567"}))
check("a huge delay prints as plain digits (no exponent)", "e+" not in big and "[retry in 1234567s]" in big, big)
check("...and key_health reads the printed hint back unchanged", p(big) == 1234567.0, p(big))
for _v in (0.02, 1.5, 26.06, 7, 3723, 86400):
    _txt = "rate limited or quota exceeded (HTTP 429) [retry in %ss]" % ai_providers._fmt_delay(_v)
    check(f"round trip: {_v} survives _fmt_delay -> parse_retry_delay", close(p(_txt), float(_v), 1e-3), _txt)
check("a 2xx is still no reason", reason(Resp(200)) is None)
check("a stated nothing 503 reads exactly as before",
      reason(Resp(503, "overloaded")) == "provider server error (HTTP 503)")


def _fresh():
    try:
        key_health.HEALTH_FILE.unlink()
    except OSError:
        pass


_fresh()
t0 = 1_000_000.0
key_health.record_failure("oai", "m", "k1", ai_providers.KIND_KEY,
                          "rate limited or quota exceeded (HTTP 429) [retry in 0.02s]", now=t0)
check("a 20 ms hint parks the key for the 5 s floor, not the 60 s default",
      key_health.cooldown_until("oai", "k1", now=t0 + 4) > 0 and key_health.cooldown_until("oai", "k1", now=t0 + 6) == 0)
_fresh()
key_health.record_failure("oai", "m", "k2", ai_providers.KIND_KEY,
                          "rate limited or quota exceeded (HTTP 429)", now=t0)
check("no stated delay still parks 60 s",
      key_health.cooldown_until("oai", "k2", now=t0 + 59) > 0 and key_health.cooldown_until("oai", "k2", now=t0 + 61) == 0)
_fresh()
key_health.record_failure("oai", "m", "k3", ai_providers.KIND_OVERLOAD,
                          "provider server error (HTTP 503) [retry in 1.5s]", now=t0)
check("a 1.5 s overload hint parks the model for the 5 s floor",
      key_health.model_cooling("oai", "m", now=t0 + 4) and not key_health.model_cooling("oai", "m", now=t0 + 6))

w = ai_client._short_429_wait_seconds
check("the same-key 429 wait now sees a millisecond hint (waits ~0.02 s)",
      close(w(ai_providers.KIND_KEY, "rate limited or quota exceeded (HTTP 429) [retry in 0.02s]", cap=30), 0.02),
      w(ai_providers.KIND_KEY, "rate limited or quota exceeded (HTTP 429) [retry in 0.02s]", cap=30))
check("D5 unchanged: a hint above the cap still rotates",
      w(ai_providers.KIND_KEY, "rate limited or quota exceeded (HTTP 429) [retry in 99s]", cap=30) is None)
check("D5 unchanged: a 503 with a stated delay is NOT waited out on the same key",
      w(ai_providers.KIND_OVERLOAD, "provider server error (HTTP 503) [retry in 5s]", cap=30) is None)

# ---------------------------------------------------------------------------
# 2. reasoning_tokens on OpenAI-compatible hosts
# ---------------------------------------------------------------------------
ex = token_usage.extract_usage

groq = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 300, "total_tokens": 400,
                                          "completion_tokens_details": {"reasoning_tokens": 250}}})
check("in-output host (total == prompt + completion): reasoning flagged as inside output",
      groq.get("thinking_tokens") == 250 and groq.get("thinking_in_output") is True, groq)
check("...and the total is not inflated", token_usage.entry_total(groq) == 400, token_usage.entry_total(groq))

no_total = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 300,
                                              "completion_tokens_details": {"reasoning_tokens": 250}}})
check("no total_tokens: the in-output reading is kept (the pre-existing behaviour)",
      no_total.get("thinking_in_output") is True and token_usage.entry_total(no_total) == 400, no_total)

xai = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 350,
                                         "completion_tokens_details": {"reasoning_tokens": 200}}})
check("xAI style (total = prompt + completion + reasoning): reasoning is billed on top",
      xai.get("thinking_tokens") == 200 and not xai.get("thinking_in_output"), xai)
check("...and counted: the ledger total equals the host's own total_tokens",
      token_usage.entry_total(xai) == 350 and token_usage.extra_thinking(xai) == 200, token_usage.entry_total(xai))

partial = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 330,
                                             "completion_tokens_details": {"reasoning_tokens": 200}}})
check("a surplus smaller than reasoning_tokens adds only the surplus (never more than billed)",
      partial.get("thinking_tokens") == 180 and token_usage.entry_total(partial) == 330, partial)

odd = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 9999,
                                         "completion_tokens_details": {"reasoning_tokens": 40}}})
check("an odd huge total_tokens cannot add more than the reasoning figure the host reported",
      odd.get("thinking_tokens") == 40 and token_usage.entry_total(odd) == 190, odd)

short = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 120,
                                           "completion_tokens_details": {"reasoning_tokens": 40}}})
check("a total_tokens below prompt + completion is not a surplus (in-output reading)",
      short.get("thinking_in_output") is True and token_usage.entry_total(short) == 150, short)

plain = ex("openai_compatible", {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 190}})
check("no reasoning_tokens reported: total_tokens is never second-guessed (no thinking key)",
      "thinking_tokens" not in plain and token_usage.entry_total(plain) == 150, plain)

zero = ex("openai_compatible", {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                                          "completion_tokens_details": {"reasoning_tokens": 0}}})
check("reasoning_tokens of 0 adds nothing", "thinking_tokens" not in zero, zero)

# the over-budget stop sees it (a scheduled run must not slip past its limit on reasoning)
tb = ai_providers.TokenBudget(300)
tb.add({"round": 1, **xai})
check("TokenBudget counts the on-top reasoning", tb.used == 350 and tb.exceeded(), tb.used)
tb2 = ai_providers.TokenBudget(450)
tb2.add({"round": 1, **groq})
check("TokenBudget still does not double-count in-output reasoning", tb2.used == 400 and not tb2.exceeded(), tb2.used)

# get_usage_summary reads the same rule (thread-local rounds)
ai_providers.set_log_context(None, "t")
try:
    ai_providers._log_local.usage_rounds = [{"round": 1, **xai}, {"round": 2, **groq}]
    summ = ai_providers.get_usage_summary()
finally:
    ai_providers.clear_log_context()
check("get_usage_summary: total = both rounds' real spend (350 + 400)", summ["total_tokens"] == 750, summ)
check("get_usage_summary: on-top thinking reported as thinking, in-output kept separate",
      summ.get("thinking_tokens") == 200 and summ.get("thinking_in_output_tokens") == 250, summ)

# other providers are untouched
check("anthropic unchanged",
      ex("anthropic", {"usage": {"input_tokens": 10, "output_tokens": 5}})
      == {"input_tokens": 10, "output_tokens": 5, "source": "reported"})
check("gemini thoughtsTokenCount still adds on top",
      token_usage.extra_thinking(ex("gemini", {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                                                                 "thoughtsTokenCount": 30, "totalTokenCount": 45}})) == 30)

# ---------------------------------------------------------------------------
# 3. take_screenshot is not OFFERED to an unattended ask that did not ask for one
# ---------------------------------------------------------------------------
un = preflight.unoffered_tools
SHOT = preflight.SCREENSHOT_TOOL
check("the tool the rule names is take_screenshot", SHOT == "take_screenshot")
check("unattended, prompt never mentions a screenshot: not offered",
      un("use the hotkey ctrl+s then type my name", True) == frozenset({SHOT}))
check("unattended, asked to LOOK at the screen: still not offered (read_screen / look_at_screen do that)",
      un("check the screen and tell me what the dialog says", True) == frozenset({SHOT}))
for _ask in ("take a screenshot of the desktop", "Take a Screenshot", "grab a screen shot and save it",
             "save a screen-shot to the desktop", "capture the screen every hour", "snap my desktop",
             "hit print screen", "make a screen capture of this window", "photograph the display"):
    check(f"unattended and the job asks for the picture ({_ask!r}): offered", un(_ask, True) == frozenset(), un(_ask, True))
for _talk in ("use the hotkey ctrl+s", "check the screen", "shut the PC down if usage is exhausted", "", None):
    check(f"interactive is never filtered ({_talk!r})", un(_talk, False) == frozenset())
check("an empty unattended prompt: not offered (nothing asked for it)", un("", True) == frozenset({SHOT}) and
      un(None, True) == frozenset({SHOT}))
check("a prompt object that cannot be read as text offers everything instead of raising",
      un(type("Bad", (), {"__str__": lambda self: 1 / 0})(), True) == frozenset())
check("the result is deterministic (ask() and the prompt builder must agree)",
      un("press f5", True) == un("press f5", True))

# the pack instruction that names the tool
base = tool_registry.pack_instruction("vision")
with_it = tool_registry.pack_instruction("vision", frozenset({SHOT}))
check("vision pack instruction: unchanged without an unoffered set", "take_screenshot" in base)
check("vision pack instruction: the variant no longer names take_screenshot", "take_screenshot" not in with_it, with_it)
check("...but still tells the model how to look (look_at_screen, read_screen, force_vision)",
      all(t in with_it for t in ("look_at_screen", "read_screen", "force_vision")), with_it)
check("a group whose instruction names nothing unoffered is untouched",
      tool_registry.pack_instruction("audio", frozenset({SHOT})) == tool_registry.pack_instruction("audio"))
check("an empty unoffered set is the old behaviour",
      tool_registry.pack_instruction("vision", frozenset()) == base and tool_registry.pack_instruction("vision", ()) == base)

# through the real ask(): what the first request actually carries
SAVED_ENV = {k: os.environ.get(k) for k in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_JOB_APPROVED_KINDS",
                                            "JARVIS_TOKEN_BUDGET", "JARVIS_JOB_ACTION")}


class _R:
    def __init__(self, body):
        self.status_code, self._body = 200, body
        self.text, self.headers = json.dumps(body), {}

    def json(self):
        return self._body


def first_request(prompt, unattended):
    """(offered tool names, system-prompt text) of the first request ask() sends."""
    try:
        key_health.HEALTH_FILE.unlink()
    except OSError:
        pass
    cfg = json.loads(json.dumps(ai_config.DEFAULT_AI_CONFIG))
    cfg["providers"] = [{"name": "gemini", "type": "gemini", "enabled": True,
                         "model": "gemini-3.6-flash", "api_keys": ["placeholder-1"]}]
    d = cfg["defaults"]
    d["provider_priority"] = ["gemini"]
    d["prompt_mode"] = "compact"
    d["min_round_interval_seconds"] = 0
    ai_config.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    ai_config.AI_CONFIG_FILE.write_text(json.dumps(cfg), encoding=ai_config.ENCODING)
    conv_id = conversations.new_conversation(make_current=False)
    seen = []

    def fake_post_json(url, headers, payload, timeout):
        seen.append(payload)
        return _R({"candidates": [{"content": {"role": "model", "parts": [{"text": "Done, sir."}]},
                                   "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 5}}), None

    saved = (ai_providers._post_json, time.sleep)
    ai_providers._post_json, time.sleep = fake_post_json, (lambda s: None)
    for k in SAVED_ENV:
        os.environ.pop(k, None)
    if unattended:
        os.environ["JARVIS_SCHEDULED"] = "1"
    try:
        ai_client.ask(prompt, commands=[], conversation_id=conv_id,
                      on_confirm_request=lambda n, a, risk_note=None: True)
    finally:
        ai_providers._post_json, time.sleep = saved
        for k, v in SAVED_ENV.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if not seen:
        return [], ""
    names = [f["name"] for g in (seen[0].get("tools") or []) for f in g.get("function_declarations", [])]
    return names, json.dumps(seen[0].get("systemInstruction") or "")


NOT_ASKED = "use the hotkey ctrl+s then type my name"
ASKED = "take a screenshot of the desktop"

ctl_tools, ctl_sys = first_request(NOT_ASKED, unattended=False)
check("control (interactive): the desktop group is offered, take_screenshot included -- the premise of the next checks",
      "take_screenshot" in ctl_tools and "hotkey" in ctl_tools, ctl_tools)
check("control (interactive): the prompt carries the screenshot blurb", "take_screenshot" in ctl_sys, ctl_sys[:300])

un_tools, un_sys = first_request(NOT_ASKED, unattended=True)
check("unattended, no screenshot asked for: the desktop group is still offered", "hotkey" in un_tools, un_tools)
check("...without take_screenshot", "take_screenshot" not in un_tools, un_tools)
check("...and the other screen tools are untouched (read_screen is how it looks)",
      "read_screen" in ctl_tools and "read_screen" in un_tools, (ctl_tools, un_tools))
check("...and the prompt no longer names take_screenshot (a provider rejects a call to an unoffered tool)",
      "take_screenshot" not in un_sys, un_sys[:300])
check("one tool fewer than the interactive offering, nothing else removed",
      sorted(set(ctl_tools) - set(un_tools)) == ["take_screenshot"] and not (set(un_tools) - set(ctl_tools)),
      (sorted(set(ctl_tools) - set(un_tools)), sorted(set(un_tools) - set(ctl_tools))))

asked_tools, asked_sys = first_request(ASKED, unattended=True)
check("unattended and the job asks for a screenshot: take_screenshot is offered", "take_screenshot" in asked_tools, asked_tools)

# the executor is built from the FULL catalog, so a call the model makes anyway still runs
check("the full session catalog still contains take_screenshot (only the offering is trimmed)",
      any(s.get("name") == "take_screenshot" for s in system_tools.tool_schemas_for_session()))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  ")
    sys.exit(1)
