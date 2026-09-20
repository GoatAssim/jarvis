"""Tests for the F.2 fix (master plan Part F): a model that guesses a tool
name gets a bare `{"error": "no such tool: X"}` and no way to recover
except guessing again — burning real round budget on nothing (Case 2a: 4 of
6 rounds; Case 2b: 6 discovery calls before the one that mattered).

Two independent pieces, matching F.2's suggested fix items 1 and 2 (item 3,
a separate discovery-call budget, is owner decision D1 — not done here, see
master plan):

1. `execute_tool`'s unknown-name path (`tools.py`) now reuses the same
   did_you_mean/search_tools hint `get_tool_schema` already had, via a
   shared `_unknown_tool_hint` helper — so a model that calls a made-up
   name directly (not through get_tool_schema first) still gets pointed
   at the real name or at search_tools.
2. The compact and ultra-compact system-prompt blurbs (`ai_client.py`,
   `_tools_blurb`) now say "never invent a tool name — call search_tools
   first", matching what the full (non-compact) blurb already said. Those
   two compact variants are exactly the ones active when tool schemas are
   name-only (Case 2a/2b's actual condition) — the ones that used to read
   as tacit permission to probe.

Run: python3 tests/test_unknown_tool_hint.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tools, ai_client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def test_execute_tool_unknown_name_carries_did_you_mean():
    # "move_file" doesn't exist, but "move_mouse" does — a plausible
    # near-miss the old bare error gave no hint about.
    result = tools.execute_tool("move_file", {})
    check("error text unchanged in shape", result.get("error") == "no such tool: move_file", result)
    check("did_you_mean is present and non-empty", bool(result.get("did_you_mean")), result)


def test_execute_tool_unknown_name_falls_back_to_search_tools_hint():
    result = tools.execute_tool("zzzz_definitely_not_a_real_tool_zzzz", {})
    check("error text present", result.get("error", "").startswith("no such tool:"), result)
    check("no did_you_mean for a name with no near matches", "did_you_mean" not in result, result)
    check(
        "hint points at search_tools",
        result.get("hint") == "Call search_tools with a keyword to find the right name.",
        result,
    )


def test_get_tool_schema_and_execute_tool_agree_on_unknown_name():
    """Both entry points a model can hit an unknown name through now give
    the same shape of answer — this was previously get_tool_schema-only."""
    via_schema = tools.tool_get_tool_schema({"name": "move_file"})
    via_execute = tools.execute_tool("move_file", {})
    check(
        "same error text",
        via_schema.get("error") == via_execute.get("error") == "no such tool: move_file",
        (via_schema, via_execute),
    )
    check(
        "same did_you_mean list",
        via_schema.get("did_you_mean") == via_execute.get("did_you_mean"),
        (via_schema.get("did_you_mean"), via_execute.get("did_you_mean")),
    )


def test_known_tool_names_are_unaffected():
    # A real tool call must not be touched by any of this.
    result = tools.execute_tool("search_tools", {"query": "shell"})
    check("known tool still runs normally", "error" not in result or "no such tool" not in result.get("error", ""), result)


def test_compact_blurb_tells_model_not_to_invent_names():
    blurb = ai_client._tools_blurb(compact=True, ultra=False)
    check("compact blurb says never invent a tool name", "invent" in blurb.lower(), blurb)
    check("compact blurb points at search_tools", "search_tools" in blurb, blurb)


def test_ultra_compact_blurb_tells_model_not_to_invent_names():
    blurb = ai_client._tools_blurb(compact=True, ultra=True)
    check("ultra blurb says never invent a tool name", "invent" in blurb.lower(), blurb)
    check("ultra blurb points at search_tools", "search_tools" in blurb, blurb)


def test_full_noncompact_blurb_still_says_it_too():
    # Regression guard: this one already had the line before the fix.
    blurb = ai_client._tools_blurb(compact=False, ultra=False)
    check("full blurb still says never invent a tool name", "invent" in blurb.lower(), blurb)


for fn in [
    test_execute_tool_unknown_name_carries_did_you_mean,
    test_execute_tool_unknown_name_falls_back_to_search_tools_hint,
    test_get_tool_schema_and_execute_tool_agree_on_unknown_name,
    test_known_tool_names_are_unaffected,
    test_compact_blurb_tells_model_not_to_invent_names,
    test_ultra_compact_blurb_tells_model_not_to_invent_names,
    test_full_noncompact_blurb_still_says_it_too,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
