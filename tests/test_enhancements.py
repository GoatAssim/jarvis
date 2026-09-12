"""Automated tests for the three enhancements built on top of
tests/test_schemas_for_tools.py's baseline (see jarvis-codebase-
orientation.md's "Outstanding ideas" section for which is which):

  #1 weighted multi-group confidence in tool_router.route()
  #2 cross-process discovery cache (discovery_cache.py)
  #3 exclusion keywords in TOOL_KEYWORDS

Same no-framework, plain-assert convention as test_schemas_for_tools.py
(none is installed in this environment) — runnable directly:

    python3 tests/test_enhancements.py

Or point pytest at the whole tests/ dir if it's available in your
environment; these functions are also valid pytest test functions as-is.

Tests that touch discovery_cache.py isolate it to a throwaway temp file
(monkeypatching discovery_cache.CACHE_FILE for the duration of the test,
then restoring it) so running this suite never reads/writes a real
~/.jarvis/discovery_cache.json.
"""

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client  # noqa: E402
from jarvis import discovery_cache  # noqa: E402
from jarvis import tool_registry  # noqa: E402
from jarvis import tool_router  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402


@contextmanager
def _isolated_cache():
    """Point discovery_cache at a throwaway file for the duration of the
    `with` block, then restore it — same idea as commands_config.py's
    storage, just redirected so tests never touch the real ~/.jarvis."""
    original_dir = discovery_cache.JARVIS_DIR
    original_file = discovery_cache.CACHE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        discovery_cache.JARVIS_DIR = Path(tmp)
        discovery_cache.CACHE_FILE = Path(tmp) / "discovery_cache.json"
        try:
            yield
        finally:
            discovery_cache.JARVIS_DIR = original_dir
            discovery_cache.CACHE_FILE = original_file


# ---------------------------------------------------------------------------
# #1 — weighted multi-group confidence
# ---------------------------------------------------------------------------

def test_router_near_tie_keeps_both_groups():
    # battery (core, weight 10) and commit (system_control, weight 7) —
    # scores are close (diff 3, well under ROUTER_GROUP_MARGIN=10) and
    # there are only two of them (under ROUTER_MAX_GROUPS=2), so both
    # should survive trimming, matching the design doc's "download this
    # spotify track" near-tie example.
    result = tool_router.route("what's my battery status, can I also commit this")
    assert result.confident is True
    assert set(result.groups) == {"core", "system_control"}


def test_router_drops_weak_group_beyond_margin():
    # A strong, multi-keyword core hit (battery + wifi + disk) alongside a
    # single bare-MIN_SCORE desktop hit ("press") — the desktop score ends
    # up more than ROUTER_GROUP_MARGIN below core's, so it should be
    # trimmed even though desktop matched at all.
    result = tool_router.route(
        "what is my battery, wifi status, disk space, and please press a key for me"
    )
    assert result.confident is True
    assert result.groups == ["core"]
    assert "press_key" not in result.tools
    assert "type_text" not in result.tools


def test_router_caps_at_max_groups_even_within_margin():
    # Three groups all match (core/battery, system_control/commit,
    # youtube), and youtube's score is close enough to core's to survive
    # the margin check — but ROUTER_MAX_GROUPS=2 caps the result to the
    # top two by score regardless, dropping system_control even though it
    # would have passed the margin test on its own.
    result = tool_router.route(
        "what's my battery, commit my changes, and download this youtube video"
    )
    assert result.confident is True
    assert len(result.groups) <= tool_router.ROUTER_MAX_GROUPS
    assert "system_control" not in result.groups


def test_router_single_group_message_unaffected():
    # The common case — one matched group — must be byte-for-byte the
    # same behavior as before enhancement #1 (no trimming logic engages
    # when there's nothing to trim).
    result = tool_router.route("commit my changes")
    assert result.confident is True
    assert result.groups == ["system_control"]


# ---------------------------------------------------------------------------
# #2 — cross-process discovery cache
# ---------------------------------------------------------------------------

def test_discovery_cache_miss_on_empty_store():
    with _isolated_cache():
        assert discovery_cache.cache_lookup("deploy", "tools") is None


def test_discovery_cache_round_trip_and_normalization():
    with _isolated_cache():
        discovery_cache.cache_store("  Deploy Prod  ", "tools", ["run_command"])
        # normalized (stripped/lowercased) lookup should hit
        assert discovery_cache.cache_lookup("deploy prod", "tools") == ["run_command"]


def test_discovery_cache_kind_isolation():
    with _isolated_cache():
        discovery_cache.cache_store("deploy", "tools", ["run_command"])
        # a "commands"-kind lookup for the same query string must not see
        # the "tools"-kind entry — the two are tracked independently so a
        # tool hit is never handed back as a commands-group activation.
        assert discovery_cache.cache_lookup("deploy", "commands") is None


def test_discovery_cache_expired_entry_is_a_miss():
    with _isolated_cache():
        discovery_cache.cache_store("deploy", "tools", ["run_command"])
        entries = discovery_cache._load_entries()
        entries[0]["ts"] -= discovery_cache.TTL_SECONDS + 1
        discovery_cache._save_entries(entries)
        assert discovery_cache.cache_lookup("deploy", "tools") is None


def test_discovery_cache_caps_entry_count():
    with _isolated_cache():
        for i in range(discovery_cache.MAX_ENTRIES + 10):
            discovery_cache.cache_store(f"query {i}", "tools", ["get_battery"])
        assert len(discovery_cache._load_entries()) == discovery_cache.MAX_ENTRIES


def test_discovery_cache_ignores_empty_or_corrupt_file():
    with _isolated_cache():
        discovery_cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        discovery_cache.CACHE_FILE.write_text("not valid json{{{")
        assert discovery_cache.cache_lookup("anything", "tools") is None
        # a store afterward should still succeed (corrupt file treated as
        # empty, not as a fatal error)
        discovery_cache.cache_store("anything", "tools", ["get_battery"])
        assert discovery_cache.cache_lookup("anything", "tools") == ["get_battery"]


def test_make_tool_executor_stores_search_tools_hit_in_cache():
    # End-to-end through the real wiring: _make_tool_executor(cache_query=...)
    # should call discovery_cache.cache_store() when a real search_tools
    # call (run against the actual catalog, deterministic/no network) finds
    # something — this is the exact call site described in the enhancement
    # #2 design ("On an actual search_tools/search_commands hit inside
    # _make_tool_executor, also call cache_store()").
    with _isolated_cache():
        discovered = []
        executor = ai_client._make_tool_executor(
            on_tool_call=None,
            schemas=None,
            discover_sink=lambda names: discovered.extend(names),
            cache_query="find me a spotify tool",
        )
        result = executor("search_tools", {"query": "spotify"})
        assert result.get("matches"), "expected search_tools to find spotify tools"
        assert discovered, "discover_sink should have been called with matches"
        cached = discovery_cache.cache_lookup("find me a spotify tool", "tools")
        assert cached, "a successful search_tools hit should be cache_store()'d"


def test_make_tool_executor_stores_search_commands_hit_in_cache():
    # Same idea for search_commands, whose own matches are saved-command
    # names (not tool names) — cache_store() should record the tool name
    # (["run_command"]) that discover_sink was actually handed, under
    # kind="commands", not the saved-command names themselves.
    original_execute_tool = system_tools.execute_tool

    def fake_execute_tool(name, arguments=None, verbosity=None):
        if name == "search_commands":
            return {"matches": [{"name": "deploy-prod"}], "total_commands": 1}
        return original_execute_tool(name, arguments, verbosity)

    with _isolated_cache():
        system_tools.execute_tool = fake_execute_tool
        try:
            discovered = []
            executor = ai_client._make_tool_executor(
                on_tool_call=None,
                schemas=None,
                discover_sink=lambda names: discovered.extend(names),
                cache_query="run the deploy thing",
            )
            result = executor("search_commands", {"query": "deploy"})
            assert result.get("matches")
            assert "run_command" in discovered
            cached = discovery_cache.cache_lookup("run the deploy thing", "commands")
            assert cached == ["run_command"]
        finally:
            system_tools.execute_tool = original_execute_tool


def test_cache_query_none_stores_nothing():
    # cache_query is optional — with it left None (matches tools_enabled
    # being False in ask()), nothing should be written, and nothing should
    # raise either.
    with _isolated_cache():
        executor = ai_client._make_tool_executor(
            on_tool_call=None, schemas=None,
            discover_sink=lambda names: None,
            cache_query=None,
        )
        executor("search_tools", {"query": "spotify"})
        assert discovery_cache.cache_lookup("spotify", "tools") is None


# ---------------------------------------------------------------------------
# #3 — exclusion keywords in TOOL_KEYWORDS
# ---------------------------------------------------------------------------

def test_exclusion_keyword_cancels_the_phrase_match():
    # "screen shot" is excluded by "recording"/"record" on take_screenshot
    # (see tool_registry.TOOL_KEYWORDS) — with both present and no other
    # keyword in the message, the router should have no opinion at all.
    result = tool_router.route("please take a screen shot recording")
    assert result.confident is False
    assert result.tools == []


def test_exclusion_keyword_does_not_affect_plain_match():
    # Without the excluding term present, the phrase still matches
    # normally — enhancement #3 must not regress the common case.
    result = tool_router.route("take a screenshot")
    assert result.confident is True
    assert "take_screenshot" in result.tools

    result = tool_router.route("can you take a screen shot please")
    assert result.confident is True
    assert "take_screenshot" in result.tools


def test_keyword_weight_and_exclusions_helpers_unwrap_both_shapes():
    # Plain int form (most entries)
    assert tool_registry.keyword_weight(10) == 10
    assert tool_registry.keyword_exclusions(10) == []

    # Dict form with an exclusion list
    value = {"weight": 10, "not_with": ["recording", "record"]}
    assert tool_registry.keyword_weight(value) == 10
    assert tool_registry.keyword_exclusions(value) == ["recording", "record"]

    # Dict form with no not_with key at all should degrade to []
    assert tool_registry.keyword_exclusions({"weight": 10}) == []


def test_keywords_for_haystack_is_still_just_phrases():
    # tools.py's tool_search_tools() haystack building only ever needs the
    # phrase strings (see tools.py, " ".join(keywords_for(name).keys())) —
    # confirm a dict-valued entry doesn't leak its weight/not_with into
    # that join, which would happen if keywords_for() ever stopped simply
    # passing the raw values through.
    phrases = set(tool_registry.keywords_for("take_screenshot").keys())
    assert phrases == {"screenshot", "screen shot", "capture screen"}


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
