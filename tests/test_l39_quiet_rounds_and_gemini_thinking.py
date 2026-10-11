"""L.39 + Gemini thinking (reported 2026-10-11).

  * Groq gpt-oss only got its quieting keys (low effort, reasoning hidden) when
    the level was `off`. A forced ending, and a tool round that does not carry
    a thinking request at level low/medium/high, went out with no effort key,
    so the host ran them at ITS default (medium).
  * Gemini counts thinking tokens against maxOutputTokens (default 3072) while
    a medium budget is 4096 and high is 16384, so thinking ate the cap, the
    call came back empty, and the ask failed over. The cap is now raised the
    way Anthropic's already was.
  * An `extra_params.thinkingConfig.thinkingBudget: 0` in a Gemini provider
    block (what the max_tokens error tells people to add) cancelled every
    thinking level picked. Thinking ON now beats a budget of 0; OFF is unchanged.
  * call_gemini retries once without the thinking keys when the host rejects
    them (the OpenAI-compatible adapter always did; Gemini burned the key).

Run: python3 tests/test_l39_quiet_rounds_and_gemini_thinking.py
No network: requests.post is replaced by a queue. Touches no real ~/.jarvis.
"""
import copy
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers, reasoning  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


GROQ = {"name": "groq", "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1/chat/completions"}
GROQ_LLAMA = dict(GROQ, model="llama-3.3-70b-versatile")
GEM25 = {"name": "gemini", "model": "gemini-2.5-flash"}
GEM3 = {"name": "gemini", "model": "gemini-3-pro-preview"}
QUIET = {"reasoning_effort": "low", "include_reasoning": False}


def _apply(provider, ptype, level, round_num=0, ran_tools=False, thought_rounds=0, payload=None):
    ai_providers.set_thinking(level)
    pl = payload if payload is not None else {}
    ret = ai_providers._apply_thinking(pl, provider, ptype, round_num, ran_tools, thought_rounds)
    return ret, pl


# ---------------------------------------------------------------------------
# Groq: rounds that do not think are quiet too
# ---------------------------------------------------------------------------
ret, pl = _apply(GROQ, "openai_compatible", "medium", round_num=0)
check("medium round 0 on gpt-oss: asks for medium effort, reasoning not hidden",
      ret is True and pl.get("reasoning_effort") == "medium" and "include_reasoning" not in pl, pl)

ret, pl = _apply(GROQ, "openai_compatible", "medium", round_num=1, ran_tools=False)
check("a later round with no tools run is quiet (low + hidden), not host-default",
      ret is False and {k: pl.get(k) for k in QUIET} == QUIET, pl)

ret, pl = _apply(GROQ, "openai_compatible", "low", round_num=3, ran_tools=True, thought_rounds=2)
check("past the level's round cap the round is quiet, not host-default",
      ret is False and {k: pl.get(k) for k in QUIET} == QUIET, pl)

ai_providers.set_thinking("high")
ai_providers._log_local.forced = {"round_base": 0, "captured": [], "text": ""}
try:
    pl = {}
    ret = ai_providers._apply_thinking(pl, GROQ, "openai_compatible", 0, False, 0)
finally:
    ai_providers._log_local.forced = None
check("a forced ending (even at level high) is quiet on gpt-oss",
      ret is False and {k: pl.get(k) for k in QUIET} == QUIET, pl)

ai_providers.set_thinking("high")
ai_providers._log_local.forced = {"round_base": 0, "captured": [], "text": ""}
try:
    pl = {}
    ai_providers._apply_thinking(pl, GROQ_LLAMA, "openai_compatible", 0, False, 0)
    gem = {}
    ai_providers._apply_thinking(gem, GEM25, "gemini", 0, False, 0)
finally:
    ai_providers._log_local.forced = None
check("a forced ending on a model with no reasoning default sends nothing extra",
      pl == {} and gem == {}, (pl, gem))

ret, pl = _apply(GROQ, "openai_compatible", "off")
check("level off on gpt-oss is unchanged: quiet keys, not counted",
      ret is False and {k: pl.get(k) for k in QUIET} == QUIET
      and ai_providers.get_thinking_trace()["requested"] == 0, pl)

ret, pl = _apply(GROQ, "openai_compatible", "medium", round_num=1, ran_tools=False,
                 payload={"reasoning_effort": "high"})
check("a key already in the payload is still never overwritten", pl["reasoning_effort"] == "high", pl)

ret, pl = _apply(GEM25, "gemini", "medium", round_num=1, ran_tools=False,
                 payload={"generationConfig": {"maxOutputTokens": 100}})
check("a non-thinking Gemini round is left exactly as it was",
      ret is False and pl == {"generationConfig": {"maxOutputTokens": 100}}, pl)

# ---------------------------------------------------------------------------
# Gemini: the output cap leaves room for the thinking that was asked for
# ---------------------------------------------------------------------------
G = lambda n: {"generationConfig": {"maxOutputTokens": n}}  # noqa: E731

ret, pl = _apply(GEM25, "gemini", "medium", payload=G(3072))
check("medium (4096 budget) raises a 3072 cap to budget + 1024",
      ret is True and pl["generationConfig"]["maxOutputTokens"] == 4096 + 1024, pl)
ret, pl = _apply(GEM25, "gemini", "high", payload=G(3072))
check("high (16384 budget) raises the cap to 16384 + 1024",
      pl["generationConfig"]["maxOutputTokens"] == 16384 + 1024, pl)
ret, pl = _apply(GEM25, "gemini", "low", payload=G(3072))
check("low (1024 budget) fits already: 3072 stays 3072",
      pl["generationConfig"]["maxOutputTokens"] == 3072, pl)
ret, pl = _apply(GEM25, "gemini", "high", payload=G(60000))
check("a cap that is already larger is never lowered",
      pl["generationConfig"]["maxOutputTokens"] == 60000, pl)
ret, pl = _apply(GEM25, "gemini", "off", payload=G(3072))
check("level off leaves the cap alone", pl == G(3072), pl)
ret, pl = _apply(GEM3, "gemini", "high", payload=G(3072))
tc = pl["generationConfig"].get("thinkingConfig") or {}
check("Gemini 3 (thinkingLevel) also gets room: level's budget + 1024",
      tc.get("thinkingLevel") == "high"
      and pl["generationConfig"]["maxOutputTokens"] == 16384 + 1024, pl)

check("gemini_output_floor: dynamic budget (-1) and no config ask for nothing",
      reasoning.gemini_output_floor("high", {"thinkingBudget": -1}) == 0
      and reasoning.gemini_output_floor("high", None) == 0
      and reasoning.gemini_output_floor("off", {"thinkingLevel": "low"}) == 0)
check("gemini_output_floor: an explicit budget is used as given",
      reasoning.gemini_output_floor("low", {"thinkingBudget": 2000}) == 3024)
check("gemini_output_floor: True is not a budget",
      reasoning.gemini_output_floor("low", {"thinkingBudget": True}) == 0)

# ---------------------------------------------------------------------------
# _merge_gemini_extra: thinking ON beats a budget of 0
# ---------------------------------------------------------------------------
gen = {"maxOutputTokens": 5120, "thinkingConfig": {"thinkingBudget": 4096, "includeThoughts": True}}
ai_providers._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingBudget": 0}}, thinking_requested=True)
check("thinking requested: a provider budget of 0 no longer cancels it",
      gen["thinkingConfig"] == {"thinkingBudget": 4096, "includeThoughts": True}, gen)

gen = {"maxOutputTokens": 10}
ai_providers._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingBudget": 0}})
check("thinking not requested: the provider's 0 applies as written (off stays off)",
      gen["thinkingConfig"] == {"thinkingBudget": 0}, gen)

gen = {"thinkingConfig": {"thinkingBudget": 4096, "includeThoughts": True}}
ai_providers._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingBudget": 2048}}, thinking_requested=True)
check("a non-zero provider budget still wins, as before",
      gen["thinkingConfig"]["thinkingBudget"] == 2048 and gen["thinkingConfig"]["includeThoughts"], gen)

# ---------------------------------------------------------------------------
# Rejection matcher
# ---------------------------------------------------------------------------
for text in ("HTTP 400: Thinking level medium is not supported for this model",
             "HTTP 400: thinking_budget must be between 128 and 32768",
             "HTTP 400: thinkingBudget is out of range for this model"):
    check(f"counts as a thinking rejection: {text[:48]}", reasoning.looks_like_thinking_rejected(text))
for text in ("HTTP 429: quota exceeded", "HTTP 400: invalid model name", "HTTP 503: overloaded", None):
    check(f"does NOT count: {text}", not reasoning.looks_like_thinking_rejected(text))


# ---------------------------------------------------------------------------
# call_gemini end to end (queued fake HTTP)
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = __import__("json").dumps(body)
        self.headers = {}

    def json(self):
        return self._body


class Queue:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None, **_kw):
        self.calls.append({"url": url, "payload": copy.deepcopy(json)})
        if not self.responses:
            raise AssertionError("queue exhausted: more requests than expected")
        return self.responses.pop(0)


OK_BODY = {"candidates": [{"content": {"parts": [
    {"thought": True, "text": "weighing it"}, {"text": "hello"}]}, "finishReason": "STOP"}]}
PROVIDER = {"name": "gemini", "type": "gemini", "api_key": "k", "model": "gemini-3-pro-preview",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"}
MSGS = [{"role": "user", "content": "hi"}]


LAST = {"trace": ""}


def run_gemini(provider, level, responses):
    orig = ai_providers.requests.post
    q = Queue(responses)
    ai_providers.requests.post = q
    try:
        ai_providers.set_thinking(level)
        result = ai_providers.call_gemini(provider, MSGS, timeout=5)
        LAST["trace"] = ai_providers.get_thinking_trace()["text"]
    finally:
        ai_providers.requests.post = orig
        ai_providers.set_thinking("off")
    return result, q


rejected = FakeResponse(400, {"error": {"code": 400,
                                        "message": "Thinking level medium is not supported for this model."}})
result, q = run_gemini(PROVIDER, "medium", [rejected, FakeResponse(200, OK_BODY)])
first = q.calls[0]["payload"]["generationConfig"] if q.calls else {}
second = q.calls[1]["payload"]["generationConfig"] if len(q.calls) > 1 else {}
check("a rejected thinkingLevel costs one retry, not the key",
      result.ok and result.text == "hello" and len(q.calls) == 2, (result.ok, result.error, len(q.calls)))
check("the first request carried the thinking config, the retry did not",
      "thinkingConfig" in first and "thinkingConfig" not in second, (first, second))

other = FakeResponse(400, {"error": {"code": 400, "message": "API key not valid."}})
result, q = run_gemini(PROVIDER, "medium", [other])
check("an unrelated 400 is NOT retried and still fails the attempt",
      result.ok is False and len(q.calls) == 1, (result.ok, len(q.calls)))

provider = dict(PROVIDER, model="gemini-2.5-flash", extra_params={"thinkingConfig": {"thinkingBudget": 0}})
result, q = run_gemini(provider, "medium", [FakeResponse(200, OK_BODY)])
tc = q.calls[0]["payload"]["generationConfig"].get("thinkingConfig") or {}
check("call_gemini: level medium + provider budget 0 still asks for thinking and thoughts",
      tc.get("thinkingBudget") == 4096 and tc.get("includeThoughts") is True, tc)
check("call_gemini: the thought summary that comes back is collected as the trace",
      LAST["trace"] == "weighing it", LAST)

result, q = run_gemini(provider, "off", [FakeResponse(200, OK_BODY)])
tc = q.calls[0]["payload"]["generationConfig"].get("thinkingConfig")
check("call_gemini: level off + provider budget 0 sends the 0 untouched", tc == {"thinkingBudget": 0}, tc)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
