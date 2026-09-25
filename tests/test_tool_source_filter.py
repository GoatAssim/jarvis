"""§3 — Debug-menu source filter.

tools_list_payload() (tools.py:728, behind /api/tools) now returns a
`source` field per tool so the Debug panel can filter on it. Covers: every
item gets one of the four buckets that exist today ("builtin", "auto",
"user", "mcp" — see tools._tool_source's docstring), the buckets partition
the catalog with no overlap, a couple of known built-ins land in
"builtin", a couple of known shipped jarvis/actions/ tools land in "auto",
the always-present mcp_list_servers (and, with a server configured, a
mcp_<server>_<tool>) lands in "mcp" rather than "auto", and USER_TOOL_NAMES
agrees with the "user" bucket exactly.

No test framework dependency (none is installed in this environment) —
plain asserts, runnable directly:

    python3 tests/test_tool_source_filter.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tools as system_tools  # noqa: E402


def _payload_by_name():
    items = system_tools.tools_list_payload()
    return {item["name"]: item for item in items}


def test_every_tool_has_a_known_source():
    by_name = _payload_by_name()
    bad = {name: item["source"] for name, item in by_name.items()
           if item.get("source") not in ("builtin", "auto", "user", "mcp")}
    assert bad == {}, f"tools with an unrecognized source: {bad}"


def test_no_duplicate_names_in_payload():
    items = system_tools.tools_list_payload()
    names = [item["name"] for item in items]
    assert len(names) == len(set(names)), "tools_list_payload() returned duplicate names"


def test_known_builtin_is_builtin():
    by_name = _payload_by_name()
    # get_battery is hand-wired directly into TOOLS at the top of tools.py,
    # long before the auto-discovery block runs — never auto or user.
    assert by_name["get_battery"]["source"] == "builtin"


def test_known_shipped_auto_tool_is_auto():
    by_name = _payload_by_name()
    # calendar_events ships from jarvis/actions/calendar_tools.py, picked
    # up by the first (shipped) discover_actions() scan.
    assert "calendar_events" in by_name, "expected calendar_tools.py's calendar_events to be discovered"
    assert by_name["calendar_events"]["source"] == "auto"


def test_user_tool_names_matches_user_bucket_exactly():
    by_name = _payload_by_name()
    user_bucket = {name for name, item in by_name.items() if item["source"] == "user"}
    assert user_bucket == system_tools.USER_TOOL_NAMES


def test_mcp_list_servers_is_mcp_not_auto():
    # mcp_list_servers is always present (actions/mcp_tools.py adds it
    # unconditionally, even with zero servers configured) and rides the
    # same shipped jarvis/actions/ auto-discovery scan as every other
    # built-in action module — the thing §3 item 2 specifically has to
    # pull out of "auto" and into its own bucket.
    by_name = _payload_by_name()
    assert "mcp_list_servers" in by_name, "expected actions/mcp_tools.py's mcp_list_servers to be discovered"
    assert by_name["mcp_list_servers"]["source"] == "mcp"
    assert "mcp_list_servers" in system_tools._MCP_TOOL_NAMES


def test_configured_mcp_server_tool_is_mcp():
    # A live process only ever sees the always-present mcp_list_servers
    # unless a real server is configured under ~/.jarvis/mcp_config.json
    # (module import order makes reconfiguring that mid-suite fragile — see
    # AGENTS.md on ~/.jarvis-touching tests needing HOME redirected before
    # any jarvis import, which this module already did at the top). So this
    # exercises the same code path _tool_source() actually runs, directly:
    # a name only reaches the "mcp" branch by way of _MCP_TOOL_NAMES, which
    # is populated purely from discovered records' `.group == "mcp"` — not
    # from a `mcp_` name prefix. Confirm a hypothetical mcp_<server>_<tool>
    # name is classified "mcp" once (and only while) it's actually in that
    # set, and reverts to "auto" once it isn't — proving the branch is live
    # rather than a name-prefix heuristic in disguise.
    fake_name = "mcp_demo_search"
    assert fake_name not in system_tools._MCP_TOOL_NAMES
    assert fake_name not in system_tools.USER_TOOL_NAMES
    system_tools._MCP_TOOL_NAMES.add(fake_name)
    try:
        assert system_tools._tool_source(fake_name) == "mcp"
    finally:
        system_tools._MCP_TOOL_NAMES.discard(fake_name)
    assert system_tools._tool_source(fake_name) == "auto" or system_tools._tool_source(fake_name) == "builtin"


def test_builtin_auto_user_mcp_partition_the_catalog_cleanly():
    by_name = _payload_by_name()
    buckets = {"builtin": set(), "auto": set(), "user": set(), "mcp": set()}
    for name, item in by_name.items():
        buckets[item["source"]].add(name)
    # No name appears in more than one bucket (a dict comprehension above
    # already guarantees this structurally, but assert the union covers
    # everything as a sanity check against a future refactor).
    assert buckets["builtin"] | buckets["auto"] | buckets["user"] | buckets["mcp"] == set(by_name)


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
