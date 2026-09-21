"""Tests for master plan Part A §6 ("thinking between tool calls").

Before this: `_apply_thinking()` capped every level at 2 total thinking
rounds per turn (round 0, plus at most one more right after the first tool
batch) — `if round_num != 0 and not (ran_tools and thought_rounds < 2)`.
A longer tool-calling sequence got no thinking at all past the second
round, regardless of level.

Now the cap comes from `reasoning.max_thinking_rounds(level)` (see
reasoning._LEVELS): 'low' keeps the old cap of 2 so its token/latency cost
is unchanged; 'medium' allows more (4) before capping; 'high' is
uncapped — every round that ran tools gets a thinking request, since a
level chosen specifically for maximum deliberation shouldn't be silently
cut off. This is a default policy choice (the finding itself just flagged
the old cap as wrong and said "worth deciding deliberately"), not a fixed
requirement — easy to retune via reasoning._LEVELS if this default doesn't
fit.

Run: python3 tests/test_thinking_round_cap.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers, reasoning  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


_PROVIDER = {"name": "test", "model": "claude-x"}


def _simulate(level, rounds, ran_tools_from_round=1):
    """Run `rounds` simulated rounds through _apply_thinking exactly the way
    an adapter loop does (payload rebuilt fresh each round; thought_rounds
    only increments when _apply_thinking said yes) and return the list of
    per-round True/False results.

    reasoning.round_patch is monkeypatched to always return a non-empty
    patch whenever it's called at all — round_patch has its own, separate,
    pre-existing budget-floor logic (budget_for_round/MIN_USEFUL_BUDGET)
    that can independently zero out a round's *content* regardless of
    whether it should run at all. That's orthogonal to §6 (which is only
    about whether a round gets a thinking attempt in the first place, via
    _apply_thinking's own round_num/ran_tools/cap gate) and isolating it
    out here means this test exercises exactly the cap logic §6 changed,
    not round_patch's unrelated budget scaling.
    """
    orig_round_patch = reasoning.round_patch
    reasoning.round_patch = lambda *a, **kw: {"thinking": {"type": "enabled", "budget_tokens": 999}}
    try:
        ai_providers.set_thinking(level)
        thought_rounds = 0
        ran_tools = False
        results = []
        for round_num in range(rounds):
            applied = ai_providers._apply_thinking(
                {}, _PROVIDER, "anthropic", round_num, ran_tools, thought_rounds)
            results.append(applied)
            if applied:
                thought_rounds += 1
            if round_num >= ran_tools_from_round - 1:
                ran_tools = True  # from here on, every round "ran tools" last round
        return results
    finally:
        reasoning.round_patch = orig_round_patch


def test_off_never_applies():
    results = _simulate("off", 5)
    check("off: never applies, any round", results == [False] * 5, results)


def test_low_keeps_pre_section6_cap_of_two():
    # round 0 (True), then at most ONE more (round 1, since ran_tools is
    # True starting round 1 here) -> True, then every round after: False.
    results = _simulate("low", 6, ran_tools_from_round=1)
    check("low: round 0 applies", results[0] is True)
    check("low: exactly 2 rounds total get thinking, same as before §6",
          results == [True, True, False, False, False, False], results)


def test_medium_allows_more_before_capping():
    results = _simulate("medium", 8, ran_tools_from_round=1)
    check("medium: round 0 applies", results[0] is True)
    check("medium: 4 rounds total get thinking, then capped",
          results == [True, True, True, True, False, False, False, False], results)


def test_high_is_uncapped():
    # A long tool-calling sequence: every round from round 0 on should get
    # a thinking request, with no cutoff — this is the actual §6 fix.
    results = _simulate("high", 10, ran_tools_from_round=1)
    check("high: every single round applies, no cap", all(results), results)


def test_a_round_with_no_tools_yet_never_applies_past_round_zero():
    # Regression guard: the ran_tools gate itself (independent of the cap)
    # must still hold — a round that never ran a tool shouldn't get a
    # thinking request just because the level is uncapped.
    ai_providers.set_thinking("high")
    applied = ai_providers._apply_thinking({}, _PROVIDER, "anthropic", round_num=1,
                                            ran_tools=False, thought_rounds=1)
    check("round 1 with ran_tools=False never applies, even at 'high'", applied is False)


def test_reasoning_max_thinking_rounds_values():
    check("off -> 0", reasoning.max_thinking_rounds("off") == 0)
    check("low -> 2 (pre-§6 behavior preserved)", reasoning.max_thinking_rounds("low") == 2)
    check("medium -> 4", reasoning.max_thinking_rounds("medium") == 4)
    check("high -> None (uncapped)", reasoning.max_thinking_rounds("high") is None)
    check("unknown level falls back to off's cap (0)",
          reasoning.max_thinking_rounds("not-a-real-level") == 0)


for fn in [
    test_off_never_applies,
    test_low_keeps_pre_section6_cap_of_two,
    test_medium_allows_more_before_capping,
    test_high_is_uncapped,
    test_a_round_with_no_tools_yet_never_applies_past_round_zero,
    test_reasoning_max_thinking_rounds_values,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
