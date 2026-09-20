"""Tests for the set_capacity_mode guard (master plan, section 1b).

The logged turn showed capacity_mode climbing 100% -> 150% -> 400% while a
local Ollama model was answering, followed by multi-minute generation gaps.
Reading the source: nothing in jarvis raises the mode on failure or retry.
The only writers of defaults.prompt_mode are a person (web UI / `jarvis
mode-set`, neither of which goes through the tool) and the model-callable
set_capacity_mode tool. So the guard lives in the tool: while a LOCAL model is
the one answering, the tool refuses to move to a costlier mode. Lowering is
always allowed, cloud providers are unaffected, and a call made outside an
ask() (direct `jarvis tool ...`, tests) is never restricted.

No config on disk is touched: load_ai_config and set_mode are stubbed, and
~/.jarvis is redirected to a temp dir before any jarvis import.

Run: python3 tests/test_capacity_mode_local_guard.py
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

from jarvis import ai_client, ai_config, ai_providers, mode_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


PROVIDERS = [
    {"name": "gemini", "type": "gemini", "enabled": True, "api_keys": ["a", "b"], "model": "m"},
    {"name": "ollama", "type": "ollama", "enabled": True, "base_url": "http://localhost:11434/api/chat", "model": "qwen"},
    # A second local entry reached through Ollama's OpenAI-compatible endpoint:
    # its TYPE is not "ollama", but it is just as local.
    {"name": "ollama1", "type": "openai_compatible", "enabled": True,
     "base_url": "http://127.0.0.1:11434/v1/chat/completions", "model": "deepseek-r1"},
    {"name": "groq", "type": "openai_compatible", "enabled": True,
     "base_url": "https://api.groq.com/openai/v1/chat/completions", "api_keys": ["g"], "model": "m"},
]


@contextmanager
def _env(current_mode, active_label):
    """Config says `current_mode`; the tool runs 'inside' an attempt labeled
    `active_label` (None = outside any ask). Yields the list of set_mode calls."""
    orig_load, orig_set = ai_config.load_ai_config, ai_client.set_mode
    calls = []
    cfg = {"persona": {}, "providers": PROVIDERS, "defaults": {"prompt_mode": current_mode}}
    ai_config.load_ai_config = lambda: cfg
    ai_client.set_mode = lambda mode: calls.append(mode) or mode
    if active_label:
        ai_providers.set_log_context("conv-x", active_label)
    try:
        yield calls
    finally:
        ai_providers.clear_log_context()
        ai_config.load_ai_config, ai_client.set_mode = orig_load, orig_set


def test_cost_ranking_comes_from_the_capacity_labels():
    cost = mode_tools._mode_cost_pct
    check("50% < 100% < 150% < 400%",
          cost("ultra") < cost("compact") < cost("precise") < cost("full"),
          [cost(m) for m in ("ultra", "compact", "precise", "full")])
    check("exact values", (cost("ultra"), cost("compact"), cost("precise"), cost("full")) == (50, 100, 150, 400))
    check("an unknown mode has no cost (never treated as an escalation)", cost("nope") is None)


def test_local_provider_cannot_be_escalated_by_the_model():
    with _env("compact", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "full"})
        check("compact -> full is refused", r.get("refused") is True and "error" in r, r)
        check("nothing was persisted", calls == [], calls)
        check("the refusal tells the model to hand this to the user",
              "Tell the user" in r["error"] and "mode-set full" in r["error"], r.get("error"))
        check("and reports what the mode still is", r.get("current_mode") == "compact")
    with _env("compact", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "precise"})
        check("compact -> precise (150%) is refused too", r.get("refused") is True and calls == [], (r, calls))


def test_blind_next_cycling_is_caught_by_the_same_guard():
    # next=true from compact lands on precise, a raise - the walk seen in the log.
    with _env("compact", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"next": True})
        check("next=true onto a costlier mode is refused", r.get("refused") is True and calls == [], (r, calls))


def test_a_local_provider_behind_an_openai_compatible_entry_is_local_too():
    with _env("compact", "ollama1") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "full"})
        check("localhost base_url counts as local even though type isn't 'ollama'",
              r.get("refused") is True and calls == [], (r, calls))


def test_lowering_is_always_allowed():
    with _env("full", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "compact"})
        check("400% -> 100% goes through on a local model", r.get("ok") is True and calls == ["compact"], (r, calls))
    with _env("compact", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "ultra"})
        check("100% -> 50% goes through on a local model", r.get("ok") is True and calls == ["ultra"], (r, calls))
    with _env("compact", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "compact"})
        check("re-setting the same mode is not an escalation", r.get("ok") is True, r)


def test_cloud_providers_are_unaffected():
    for label in ("gemini (key 2/2)", "groq"):
        with _env("compact", label) as calls:
            r = mode_tools.tool_set_capacity_mode({"mode": "full"})
            check(f"escalation still works while {label!r} is answering",
                  r.get("ok") is True and calls == ["full"], (r, calls))


def test_calls_outside_an_ask_are_never_restricted():
    with _env("compact", None) as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "full"})
        check("no active provider -> allowed (a person running the tool directly)",
              r.get("ok") is True and calls == ["full"], (r, calls))


def test_an_unrecognized_active_label_fails_open():
    with _env("compact", "some-provider-not-in-config") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "full"})
        check("can't prove it's local -> allowed", r.get("ok") is True and calls == ["full"], (r, calls))


def test_existing_error_paths_are_unchanged():
    with _env("compact", "ollama") as calls:
        r = mode_tools.tool_set_capacity_mode({"mode": "warp-speed"})
        check("unknown mode still reports as unknown", "unknown mode" in r.get("error", "") and "refused" not in r, r)
        r = mode_tools.tool_set_capacity_mode({})
        check("no mode and no next still asks for one", "pass either" in r.get("error", ""), r)
        check("get_capacity_mode is untouched", mode_tools.tool_get_capacity_mode()["mode"] == "compact")


for fn in [
    test_cost_ranking_comes_from_the_capacity_labels,
    test_local_provider_cannot_be_escalated_by_the_model,
    test_blind_next_cycling_is_caught_by_the_same_guard,
    test_a_local_provider_behind_an_openai_compatible_entry_is_local_too,
    test_lowering_is_always_allowed,
    test_cloud_providers_are_unaffected,
    test_calls_outside_an_ask_are_never_restricted,
    test_an_unrecognized_active_label_fails_open,
    test_existing_error_paths_are_unchanged,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
