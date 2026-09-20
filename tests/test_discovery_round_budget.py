"""Tests for F.2 item 3 / decision D1 (master plan Part F): discovery-only
tool calls — search_tools, get_tool_schema, load_skill, and calls to a name
that doesn't exist in the catalog at all — used to burn the same shared
GLOBAL_MAX_TOOL_ROUNDS budget as real work. F.1/F.2's evidence showed this
biting hard: one case spent 4 of 6 rounds on made-up tool names before
giving up; another spent all 6 rounds on pure discovery (get_tool_schema x5,
search_tools x1) before the one real call, which F.1 then discarded anyway
for arriving after the budget was already spent.

RoundBudget.take(names=None) now gives discovery-only rounds their own
small, separate pool (ai_client.ask() wires it from
defaults.discovery_call_budget, default 3) so a model that's looking a name
up — or that guessed wrong — doesn't lose the ability to actually do the
work. `names=None` (every pre-existing caller: code_agent's inner loop,
every other test, every adapter call site before this change) reproduces
the exact old behavior: always draw from the main pool.

Run: python3 tests/test_discovery_round_budget.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers, tool_registry  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# A couple of tool names guaranteed to exist in the real catalog, and one
# guaranteed not to — used throughout instead of hardcoding brittle
# assumptions about which tools exist.
_REAL_TOOL = "get_battery" if "get_battery" in tool_registry.TOOL_INDEX else next(iter(tool_registry.TOOL_INDEX))
_MADE_UP_NAME = "definitely_not_a_real_tool_xyz"
assert _MADE_UP_NAME not in tool_registry.TOOL_INDEX


def test_names_none_reproduces_old_behavior():
    # No discovery_limit configured (the default for any RoundBudget built
    # without one) and no names passed: take() behaves exactly like the
    # pre-D1 version — every call, discovery or not, costs the main budget.
    rb = ai_providers.RoundBudget(limit=2)
    check("take() with no names charges the main pool (1/2)", rb.take() is True)
    check("take() with no names charges the main pool (2/2)", rb.take() is True)
    check("main pool exhausted", rb.take() is False)
    check("discovery pool untouched", rb.discovery_used == 0, rb.discovery_used)


def test_discovery_only_round_draws_from_its_own_pool():
    rb = ai_providers.RoundBudget(limit=2, discovery_limit=3)
    check("search_tools round is free", rb.take(["search_tools"]) is True)
    check("main pool untouched by it", rb.used == 0, rb.used)
    check("discovery pool charged", rb.discovery_used == 1, rb.discovery_used)
    check("get_tool_schema round is also free", rb.take(["get_tool_schema"]) is True)
    check("load_skill round is also free", rb.take(["load_skill"]) is True)
    check("discovery pool now spent", rb.discovery_used == 3, rb.discovery_used)
    check("main pool STILL untouched after 3 discovery rounds", rb.used == 0, rb.used)


def test_unknown_tool_name_is_also_free():
    # A made-up name costs nothing to actually call (execute_tool just
    # returns a hint) — F.2's whole point. It should draw from the same
    # discovery pool as a real discovery tool, not the work budget.
    rb = ai_providers.RoundBudget(limit=2, discovery_limit=3)
    check("unknown name round is free", rb.take([_MADE_UP_NAME]) is True)
    check("main pool untouched", rb.used == 0, rb.used)
    check("discovery pool charged", rb.discovery_used == 1, rb.discovery_used)


def test_mixed_round_with_a_real_call_is_charged_normally():
    # A round that mixes a real tool call in alongside a discovery one must
    # NOT hide behind the cheap pool — otherwise a model could pair
    # search_tools with real work every round and never pay for any of it.
    rb = ai_providers.RoundBudget(limit=2, discovery_limit=3)
    check("mixed round is charged", rb.take(["search_tools", _REAL_TOOL]) is True)
    check("main pool charged, not discovery", rb.used == 1 and rb.discovery_used == 0,
          (rb.used, rb.discovery_used))


def test_real_tool_alone_is_charged_normally():
    rb = ai_providers.RoundBudget(limit=2, discovery_limit=3)
    check("real tool round is charged", rb.take([_REAL_TOOL]) is True)
    check("main pool charged", rb.used == 1, rb.used)
    check("discovery pool untouched", rb.discovery_used == 0, rb.discovery_used)


def test_discovery_pool_exhaustion_falls_back_to_main_budget():
    # F.1/F.2's worst case: six straight discovery calls. With a discovery
    # cap of 3, the 4th+ discovery-only round should still be allowed —
    # just charged to the main budget instead of being an automatic cutoff,
    # so a model that burned its free lookups can still make (and be
    # charged for) one more attempt rather than being cut off mid-round.
    rb = ai_providers.RoundBudget(limit=6, discovery_limit=3)
    for i in range(3):
        check(f"discovery round {i + 1}/3 is free", rb.take(["search_tools"]) is True)
    check("discovery pool now spent", rb.discovery_used == 3)
    check("main pool still untouched", rb.used == 0, rb.used)
    check("4th discovery round falls back to the main pool",
          rb.take(["search_tools"]) is True)
    check("main pool now charged for it", rb.used == 1, rb.used)


def test_main_budget_can_still_be_exhausted_after_discovery_calls():
    rb = ai_providers.RoundBudget(limit=2, discovery_limit=3)
    check("discovery round is free", rb.take(["search_tools"]) is True)
    check("real round 1/2 charged", rb.take([_REAL_TOOL]) is True)
    check("real round 2/2 charged", rb.take([_REAL_TOOL]) is True)
    check("main budget now exhausted", rb.take([_REAL_TOOL]) is False)
    check("discovery pool has room left, but isn't consulted for a real call",
          rb.discovery_used == 1 and rb.discovery_limit - rb.discovery_used == 2)


def test_discovery_limit_zero_disables_the_pool_even_with_names():
    # A caller that builds its own RoundBudget without asking for a
    # discovery pool (code_agent's inner loop; any pre-existing test) keeps
    # exactly the old behavior even if it happens to pass names.
    rb = ai_providers.RoundBudget(limit=2)  # discovery_limit defaults to 0
    check("discovery_limit defaults to 0", rb.discovery_limit == 0)
    check("search_tools round is charged to the main pool when no discovery budget exists",
          rb.take(["search_tools"]) is True)
    check("main pool was actually charged", rb.used == 1, rb.used)
    check("discovery pool never used", rb.discovery_used == 0)


def test_empty_names_list_is_not_a_discovery_round():
    # Defensive: an empty list is falsy, same as None — take([]) must not
    # be silently free.
    rb = ai_providers.RoundBudget(limit=2, discovery_limit=3)
    check("take([]) charges the main pool", rb.take([]) is True)
    check("main pool charged", rb.used == 1, rb.used)
    check("discovery pool untouched", rb.discovery_used == 0)


def test_ai_client_reads_discovery_call_budget_from_config_defaults():
    # ai_client.ask() wires discovery_limit from
    # cfg["defaults"].get("discovery_call_budget", 3) — check the plumbing
    # directly rather than the whole ask() pipeline, matching how nearby
    # config knobs (grace_call) are already unit-tested at this level.
    import inspect
    from jarvis import ai_client

    src = inspect.getsource(ai_client.ask)
    check("ask() constructs RoundBudget with discovery_limit=", "discovery_limit=" in src)
    check("ask() reads discovery_call_budget from defaults",
          "discovery_call_budget" in src)
    # Parse just enough to be sure it isn't hardcoded to 0/disabled by
    # accident — the literal default fallback used in .get(...).
    check("default fallback is 3, matching F.2's evidence (six discovery calls)",
          'discovery_call_budget", 3)' in src or "discovery_call_budget', 3)" in src,
          src)


for fn in [
    test_names_none_reproduces_old_behavior,
    test_discovery_only_round_draws_from_its_own_pool,
    test_unknown_tool_name_is_also_free,
    test_mixed_round_with_a_real_call_is_charged_normally,
    test_real_tool_alone_is_charged_normally,
    test_discovery_pool_exhaustion_falls_back_to_main_budget,
    test_main_budget_can_still_be_exhausted_after_discovery_calls,
    test_discovery_limit_zero_disables_the_pool_even_with_names,
    test_empty_names_list_is_not_a_discovery_round,
    test_ai_client_reads_discovery_call_budget_from_config_defaults,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
