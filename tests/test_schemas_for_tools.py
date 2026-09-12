"""Phase 2 compatibility check (see new_plan.md and tools.schemas_for_tools's
docstring): schemas_for_tools() must be a pure filter over the same catalog
tool_schemas_for_session() already returns — asking it for exactly the
names tool_schemas_for_session() offered must hand back the exact same
schemas, same order, nothing added or dropped. That's what makes it safe
for ai_client.ask() (Phase 4) to route through schemas_for_tools() instead
of using tool_schemas_for_session()'s list directly.

No test framework dependency (none is installed in this environment) —
plain asserts, runnable directly:

    python3 tests/test_schemas_for_tools.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tools as system_tools  # noqa: E402
from jarvis import tool_registry  # noqa: E402
from jarvis import tool_router  # noqa: E402


def test_schemas_for_tools_matches_session_schemas():
    session_schemas = system_tools.tool_schemas_for_session()
    names = [s["name"] for s in session_schemas]
    filtered = system_tools.schemas_for_tools(names)
    assert filtered == session_schemas, (
        "schemas_for_tools(names) must exactly reproduce "
        "tool_schemas_for_session() when given its own names back"
    )


def test_schemas_for_tools_drops_unknown_and_dupes():
    out = system_tools.schemas_for_tools(["get_battery", "not_a_real_tool", "get_battery"])
    assert [s["name"] for s in out] == ["get_battery"]


def test_schemas_for_tools_empty_input():
    assert system_tools.schemas_for_tools([]) == []
    assert system_tools.schemas_for_tools(None) == []


def test_schemas_for_tools_preserves_requested_order():
    out = system_tools.schemas_for_tools(["spotify_play", "get_battery"])
    assert [s["name"] for s in out] == ["spotify_play", "get_battery"]


def test_registry_every_tool_is_grouped():
    ungrouped = tool_registry._ungrouped_tool_names()
    assert ungrouped == [], f"tools missing from TOOL_GROUPS: {ungrouped}"


def test_registry_no_tool_in_two_groups():
    seen = {}
    dupes = []
    for group, names in tool_registry.TOOL_GROUPS.items():
        for name in names:
            if name in seen:
                dupes.append((name, seen[name], group))
            seen[name] = group
    assert dupes == [], f"tools listed in more than one group: {dupes}"


def test_registry_no_ghost_tools():
    real_names = set(tool_registry.TOOL_INDEX)
    ghosts = []
    for group, names in tool_registry.TOOL_GROUPS.items():
        for name in names:
            if name not in real_names:
                ghosts.append((name, group))
    assert ghosts == [], f"TOOL_GROUPS names not in the real tool catalog: {ghosts}"


def test_router_no_match_on_ambiguous_greeting():
    result = tool_router.route("hi")
    assert result.confident is False
    assert result.tools == []


def test_router_empty_input():
    for text in ("", "   ", None):
        result = tool_router.route(text)
        assert result.confident is False
        assert result.tools == []


def test_router_matches_battery_into_core_group():
    result = tool_router.route("what's my battery at?")
    assert result.confident is True
    assert "get_battery" in result.tools
    assert "core" in result.groups


def test_router_matches_git_into_system_control_group():
    result = tool_router.route("can you commit my changes")
    assert result.confident is True
    assert "git_run" in result.tools


def test_router_matches_spotify_group_not_just_one_tool():
    result = tool_router.route("open spotify and play World is Mine")
    assert result.confident is True
    # Whole group activates, not just the one matched tool — see module
    # docstring on why a follow-up call almost always needs a sibling.
    assert "spotify_play" in result.tools
    assert "spotify_search" in result.tools
    assert "spotify_open" in result.tools


def test_router_matches_youtube_download_group():
    result = tool_router.route("download this youtube video as mp4")
    assert result.confident is True
    assert set(result.tools) >= {"ytdl_info", "ytdl_formats", "ytdl_download"}


def test_router_returned_tools_are_all_real():
    real_names = set(tool_registry.TOOL_INDEX)
    for text in ("what's my battery", "commit my changes", "download this video",
                 "open spotify and play something", "install a package"):
        result = tool_router.route(text)
        unknown = [n for n in result.tools if n not in real_names]
        assert unknown == [], f"router returned unknown tool(s) {unknown} for {text!r}"


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    failures = []
    for test in _TESTS:
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
        else:
            print(f"ok       {test.__name__}")
    for name, msg in failures:
        print(f"FAILED   {name}: {msg}")
    print(f"\n{len(_TESTS) - len(failures)}/{len(_TESTS)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
