"""Thinking at Off, and the interactive token cap's default (2026-10-11).

Three leftovers from the L.39 / L.24 work:

  * Gemini at Off sent no `thinkingBudget: 0`, so a Gemini 2.5 model kept
    thinking (and billing) on its own. Now 2.5 Flash / Flash-Lite get a zero
    budget at Off; 2.5 Pro (cannot be off), Gemini 3 (thinkingLevel) and
    anything unknown get exactly the request they got before. A provider block
    can send nothing (`gemini_off_budget: false`) or name a budget.
  * gpt-oss on Groq cannot be switched fully off (per memory of Groq's docs,
    not checked live). It stays at its lowest effort with the text hidden;
    the provider block's `off_reasoning_effort` can replace "low" at Off only,
    and one info line per ask says that Off is the floor there.
  * The interactive token cap exists and is now ON at 150,000 tokens
    (0 turns it off; an unreadable value falls back to the default).

Run: python3 tests/test_thinking_off_and_interactive_cap.py
"""
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_providers, reasoning  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


ZERO = {"_generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}

# ---------------------------------------------------------------------------
# reasoning.gemini_off_patch
# ---------------------------------------------------------------------------
gop = reasoning.gemini_off_patch
for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "models/gemini-2.5-flash",
              "gemini-2.5-flash-preview-09-2025", "gemini-2.5-flash-lite-preview-06-17",
              "Gemini-2.5-Flash"):
    check(f"automatic: {model} gets a zero budget", gop(model) == ZERO, gop(model))
for model in ("gemini-2.5-pro", "gemini-2.0-flash", "gemini-2.5-flash-image",
              "gemini-2.5-flash-preview-tts", "gemini-3-flash-preview", "gemini-3.6-flash",
              "gemini-3-pro-preview", "gemini-flash-latest", "", None):
    check(f"automatic: {model!r} gets nothing", gop(model) == {}, gop(model))
check("includeThoughts is never part of the Off patch",
      "includeThoughts" not in gop("gemini-2.5-flash")["_generationConfig"]["thinkingConfig"])

check("gemini_off_budget false: nothing, even on 2.5 Flash", gop("gemini-2.5-flash", False) == {})
check("gemini_off_budget 128 on 2.5 Pro: that budget",
      gop("gemini-2.5-pro", 128) == {"_generationConfig": {"thinkingConfig": {"thinkingBudget": 128}}})
check("gemini_off_budget 0 on an alias: a zero budget", gop("gemini-flash-latest", 0) == ZERO)
check("gemini_off_budget on a Gemini 3 model: still nothing (its lowest level is not off)",
      gop("gemini-3-flash", 0) == {} and gop("gemini-3.6-flash", 128) == {})
check("gemini_off_budget true is the same as absent",
      gop("gemini-2.5-flash", True) == ZERO and gop("gemini-2.5-pro", True) == {})
check("gemini_off_budget unreadable (text, negative, a list): treated as absent, so automatic",
      gop("gemini-2.5-flash", "lots") == ZERO and gop("gemini-2.5-flash", -1) == ZERO
      and gop("gemini-2.5-flash", []) == ZERO and gop("gemini-2.5-pro", "lots") == {}
      and gop("gemini-2.5-pro", -1) == {})

# Gemini 2.5 Pro's wording for a zero budget is recognised as a thinking rejection
check("'Budget 0 is invalid. This model only works in thinking mode.' counts as a thinking rejection",
      reasoning.looks_like_thinking_rejected("Budget 0 is invalid. This model only works in thinking mode."))
check("an unrelated 400 still does not",
      not reasoning.looks_like_thinking_rejected("API key not valid. Please pass a valid API key."))

# ---------------------------------------------------------------------------
# reasoning.off_patch off_effort, host_off_floor_note
# ---------------------------------------------------------------------------
G = dict(provider_type="openai_compatible", provider_name="groq",
         base_url="https://api.groq.com/openai/v1/chat/completions")
check("gpt-oss: default Off effort is still low, reasoning text hidden",
      reasoning.off_patch(model="openai/gpt-oss-120b", **G) == {"reasoning_effort": "low", "include_reasoning": False})
check("gpt-oss: off_effort replaces low",
      reasoning.off_patch(model="openai/gpt-oss-120b", off_effort="none", **G)
      == {"reasoning_effort": "none", "include_reasoning": False})
check("gpt-oss: a blank or non-text off_effort is ignored",
      reasoning.off_patch(model="openai/gpt-oss-120b", off_effort="  ", **G)["reasoning_effort"] == "low"
      and reasoning.off_patch(model="openai/gpt-oss-120b", off_effort=5, **G)["reasoning_effort"] == "low")
check("off_effort does not touch qwen3 or other models",
      reasoning.off_patch(model="qwen/qwen3-32b", off_effort="minimal", **G) == {"reasoning_effort": "none"}
      and reasoning.off_patch(model="llama-3.3-70b-versatile", off_effort="none", **G) == {})
check("the floor note exists for gpt-oss on Groq and only there",
      "gpt-oss" in reasoning.host_off_floor_note(model="openai/gpt-oss-20b", **G)
      and reasoning.host_off_floor_note(model="qwen/qwen3-32b", **G) == ""
      and reasoning.host_off_floor_note("gemini", provider_name="gemini", model="gemini-2.5-flash") == ""
      and reasoning.host_off_floor_note("openai_compatible", provider_name="openai", model="gpt-oss-120b") == "")

# ---------------------------------------------------------------------------
# ai_providers._apply_thinking
# ---------------------------------------------------------------------------
GEM_FLASH = {"name": "gemini", "model": "gemini-2.5-flash"}
GEM_PRO = {"name": "gemini", "model": "gemini-2.5-pro"}
GEM_3 = {"name": "gemini", "model": "gemini-3-flash-preview"}
GROQ = {"name": "groq", "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1/chat/completions"}


def apply(provider, ptype, level, payload, round_num=0, ran_tools=False):
    ai_providers.set_thinking(level)
    try:
        ret = ai_providers._apply_thinking(payload, provider, ptype, round_num, ran_tools, 0)
    finally:
        requested = ai_providers.get_thinking_trace()["requested"]
        ai_providers.set_thinking("off")
    return ret, payload, requested


def G3(n=3072):
    return {"generationConfig": {"maxOutputTokens": n}}


ret, pl, req = apply(GEM_FLASH, "gemini", "off", G3())
check("off + 2.5 flash: a zero budget is merged, the output cap is untouched, not counted as a thinking request",
      ret is False and req == 0 and pl == {"generationConfig": {"maxOutputTokens": 3072, "thinkingConfig": {"thinkingBudget": 0}}}, pl)

ret, pl, req = apply(GEM_PRO, "gemini", "off", G3())
check("off + 2.5 pro: left exactly as it was", ret is False and pl == G3(), pl)
ret, pl, req = apply(GEM_3, "gemini", "off", G3())
check("off + gemini 3: left exactly as it was", ret is False and pl == G3(), pl)
ret, pl, req = apply(dict(GEM_FLASH, gemini_off_budget=False), "gemini", "off", G3())
check("off + gemini_off_budget false: left exactly as it was", pl == G3(), pl)
ret, pl, req = apply(dict(GEM_PRO, gemini_off_budget=128), "gemini", "off", G3())
check("off + gemini_off_budget 128 on 2.5 pro: that budget",
      pl["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 128}, pl)

ret, pl, req = apply(GEM_FLASH, "gemini", "off",
                     {"generationConfig": {"maxOutputTokens": 3072, "thinkingConfig": {"thinkingBudget": 2048}}})
check("a thinkingConfig already in the request is never overwritten",
      pl["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 2048}, pl)

ret, pl, req = apply(GEM_FLASH, "gemini", "medium", G3())
tc = pl["generationConfig"]["thinkingConfig"]
check("medium + 2.5 flash: unchanged -- the level's budget and includeThoughts, no zero",
      ret is True and req == 1 and tc == {"thinkingBudget": 4096, "includeThoughts": True}, tc)

ret, pl, req = apply(GEM_FLASH, "gemini", "medium", G3(), round_num=1, ran_tools=False)
check("a medium turn's unthought round is NOT zeroed on Gemini (only a real Off is)", ret is False and pl == G3(), pl)

ai_providers.set_thinking("off")
ai_providers._log_local.forced = {"round_base": 0, "captured": [], "text": ""}
try:
    pl = G3()
    ai_providers._apply_thinking(pl, GEM_FLASH, "gemini", 0, False, 0)
finally:
    ai_providers._log_local.forced = None
check("a forced ending at level off is zeroed too",
      pl["generationConfig"].get("thinkingConfig") == {"thinkingBudget": 0}, pl)

# a provider block's extra_params still wins at Off (merged key by key)
gen = {"maxOutputTokens": 3072, "thinkingConfig": {"thinkingBudget": 0}}
ai_providers._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingBudget": 512}})
check("extra_params.thinkingConfig still overrides the Off budget", gen["thinkingConfig"] == {"thinkingBudget": 512}, gen)

# gpt-oss
ret, pl, req = apply(GROQ, "openai_compatible", "off", {"model": "x"})
check("off + gpt-oss: unchanged -- low effort, text hidden, not counted",
      pl.get("reasoning_effort") == "low" and pl.get("include_reasoning") is False and req == 0, pl)
ret, pl, req = apply(dict(GROQ, off_reasoning_effort="none"), "openai_compatible", "off", {"model": "x"})
check("off + off_reasoning_effort none: that value replaces low", pl.get("reasoning_effort") == "none", pl)
ret, pl, req = apply(dict(GROQ, off_reasoning_effort="none"), "openai_compatible", "medium", {"model": "x"})
check("medium + off_reasoning_effort: the level wins, the Off override is not used",
      pl.get("reasoning_effort") == "medium" and "include_reasoning" not in pl, pl)
ret, pl, req = apply(dict(GROQ, off_reasoning_effort="none"), "openai_compatible", "medium", {"model": "x"},
                     round_num=1, ran_tools=False)
check("medium unthought round: the quiet floor 'low' is used, never the Off override",
      pl.get("reasoning_effort") == "low", pl)

# the floor note: once per ask, in the log, never on a host that can be fully off
logged = []
orig_log, orig_conv, orig_prov = ai_providers.logs.log, ai_providers._log_conv_id, ai_providers._log_provider
ai_providers.logs.log = lambda conv, kind, data, **kw: logged.append((conv, kind, data))
ai_providers._log_conv_id = lambda: "conv-x"
ai_providers._log_provider = lambda: "groq"
try:
    ai_providers.set_thinking("off")
    for _ in range(3):
        ai_providers._apply_thinking({"model": "x"}, GROQ, "openai_compatible", 0, False, 0)
    check("gpt-oss at Off: the floor note is logged once per ask, not once per round",
          len(logged) == 1 and logged[0][1] == "info" and "gpt-oss" in logged[0][2].get("thinking", ""), logged)
    ai_providers.set_thinking("off")
    ai_providers._apply_thinking({"model": "x"}, GROQ, "openai_compatible", 0, False, 0)
    check("...and again for the next ask", len(logged) == 2, logged)
    logged.clear()
    ai_providers._apply_thinking(G3(), GEM_FLASH, "gemini", 0, False, 0)
    ai_providers._apply_thinking({"model": "x"}, {"name": "groq", "model": "llama-3.3-70b-versatile",
                                                  "base_url": "https://api.groq.com/openai/v1"},
                                 "openai_compatible", 0, False, 0)
    check("a host that can be fully off logs no floor note", logged == [], logged)
    ai_providers.set_thinking("medium")
    ai_providers._apply_thinking({"model": "x"}, GROQ, "openai_compatible", 0, False, 0)
    check("a level above off logs no floor note", logged == [], logged)
finally:
    ai_providers.logs.log, ai_providers._log_conv_id, ai_providers._log_provider = orig_log, orig_conv, orig_prov
    ai_providers.set_thinking("off")

# ---------------------------------------------------------------------------
# the interactive token cap default
# ---------------------------------------------------------------------------
for var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_TOKEN_BUDGET"):
    os.environ.pop(var, None)
lim = ai_client._token_budget_limit
check("interactive default is 150,000 and is what an empty config gets",
      ai_client.INTERACTIVE_TOKEN_BUDGET_DEFAULT == 150000 and lim({}) == 150000 and lim(None) == 150000)
check("interactive: a set number wins", lim({"interactive_token_budget": 40000}) == 40000)
check("interactive: 0 is the only way to switch it off", lim({"interactive_token_budget": 0}) is None)
check("interactive: unreadable values fall back to the default, never to no limit",
      all(lim({"interactive_token_budget": bad}) == 150000 for bad in ("lots", None, -1, True, False, [], {})))
check("interactive: a numeric string is read", lim({"interactive_token_budget": "5000"}) == 5000)
os.environ["JARVIS_SCHEDULED"] = "1"
try:
    check("a scheduled run still gets its own limit (30,000), not the interactive one",
          lim({}) == 30000 and lim({"interactive_token_budget": 500}) == 30000)
finally:
    os.environ.pop("JARVIS_SCHEDULED", None)

# the Settings row shows the same default and says how to switch it off
settings_js = (Path(__file__).resolve().parent.parent / "web" / "public" / "settings.js").read_text(encoding="utf-8")
row = [ln for ln in settings_js.splitlines() if '"interactive_token_budget"' in ln]
check("Settings > Advanced: the row's fallback is 150000 and its help says 0 turns the limit off",
      len(row) == 1 and "fallback: 150000" in row[0] and "0 turns the limit off" in row[0], row)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
