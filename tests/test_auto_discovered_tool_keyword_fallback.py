"""Tests for the §7 fix (master plan): an auto-discovered tool with no
TOOL_KEYWORDS of its own is no longer left with zero router coverage.

Background: tool_router.route() only ever activates a tool's group because
*some* tool in it scored a keyword hit (see tool_loader.py's module
docstring, "Why TOOL_KEYWORDS is not really optional"). TOOL_KEYWORDS is
optional per the actions/*.py contract, so a tool file that skips it was
either only reachable via search_tools's low-confidence fallback, or rode
along invisibly on a sibling's keyword hit in the same group. The fix:
tool_loader._validate() now derives a minimal fallback keyword set from a
keyword-less tool's own name, at exactly tool_router.MIN_SCORE, so it gets
*some* real router coverage — hand-written TOOL_KEYWORDS always still win
and are never overridden.

This exercises tool_loader.discover_actions() directly against temporary
actions directories, never the real jarvis/actions/ — so it can't be
affected by (or affect) any real shipped or user tool file.

Run: python3 tests/test_auto_discovered_tool_keyword_fallback.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tool_loader  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _make_actions_dir(files):
    """files: {filename: source_text}. Returns the temp dir Path."""
    d = Path(tempfile.mkdtemp(prefix="jarvis-test-actions-"))
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def _discover(files, reserved_names=None):
    d = _make_actions_dir(files)
    logs = []
    try:
        records = tool_loader.discover_actions(
            actions_dir=d, reserved_names=reserved_names, logger=logs.append
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return records, logs


_NO_KEYWORDS_SRC = '''
TOOL_SCHEMAS = [
    {
        "name": "spotify_search",
        "description": "Search Spotify for a track.",
        "parameters": {"type": "object", "properties": {}},
    },
]
TOOLS = {"spotify_search": lambda args: {"ok": True}}
TOOL_GROUP = "spotify_fallback_test"
'''

_ALL_STOPWORDS_SRC = '''
TOOL_SCHEMAS = [
    {
        "name": "get_all",
        "description": "A tool whose name is made entirely of stopwords.",
        "parameters": {"type": "object", "properties": {}},
    },
]
TOOLS = {"get_all": lambda args: {"ok": True}}
TOOL_GROUP = "stopword_only_test"
'''

_MIXED_SRC = '''
TOOL_KEYWORDS = {
    "spotify_play": {"play music": 8},
}
TOOL_SCHEMAS = [
    {
        "name": "spotify_play",
        "description": "Play a track on Spotify.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "spotify_pause",
        "description": "Pause Spotify playback.",
        "parameters": {"type": "object", "properties": {}},
    },
]
TOOLS = {
    "spotify_play": lambda args: {"ok": True},
    "spotify_pause": lambda args: {"ok": True},
}
TOOL_GROUP = "spotify_mixed_test"
'''

_EXPLICIT_ONLY_SRC = '''
TOOL_KEYWORDS = {
    "git_run": {"run git": 8, "git command": 8},
}
TOOL_SCHEMAS = [
    {
        "name": "git_run",
        "description": "Run an arbitrary git subcommand.",
        "parameters": {"type": "object", "properties": {}},
    },
]
TOOLS = {"git_run": lambda args: {"ok": True}}
TOOL_GROUP = "git_explicit_test"
'''


def test_derive_fallback_keywords_directly():
    derived = tool_loader._derive_fallback_keywords("spotify_search")
    check(
        "derives real words, drops short/stopword tokens",
        derived == {"spotify": 5, "search": 5},
        derived,
    )
    check(
        "every derived weight matches tool_router.MIN_SCORE (the real floor)",
        all(w == 5 for w in derived.values()) and derived,
        derived,
    )
    check(
        "a name made entirely of stopwords/short words derives nothing",
        tool_loader._derive_fallback_keywords("get_all") == {},
    )


def test_keywordless_tool_gets_fallback_keywords():
    records, logs = _discover({"spotify_fallback.py": _NO_KEYWORDS_SRC})
    check("file is valid", len(records) == 1 and records[0].valid, records)
    rec = records[0]
    check(
        "spotify_search got a derived fallback entry",
        rec.keywords.get("spotify_search") == {"spotify": 5, "search": 5},
        rec.keywords,
    )
    check(
        "an informational Note is logged, not the old alarming Warning",
        any("[tools] Note:" in line for line in logs) and not any("[tools] Warning:" in line for line in logs),
        logs,
    )


def test_all_stopword_name_still_gets_the_old_warning():
    records, logs = _discover({"stopword_only.py": _ALL_STOPWORDS_SRC})
    check("file is valid", len(records) == 1 and records[0].valid, records)
    rec = records[0]
    check("no fallback keywords could be derived", rec.keywords.get("get_all") in (None, {}), rec.keywords)
    check(
        "the original loud Warning still fires when even the fallback can't help",
        any("[tools] Warning:" in line and "no fallback keywords could be derived" in line for line in logs),
        logs,
    )


def test_explicit_keywords_are_never_overridden():
    records, logs = _discover({"git_explicit.py": _EXPLICIT_ONLY_SRC})
    rec = records[0]
    check(
        "git_run keeps its exact human-authored keywords, untouched",
        rec.keywords.get("git_run") == {"run git": 8, "git command": 8},
        rec.keywords,
    )
    check(
        "no Note/Warning about keywords logged when every tool already has real keywords",
        not any(line.startswith("[tools] Note:") or line.startswith("[tools] Warning:") for line in logs),
        logs,
    )


def test_mixed_file_only_derives_for_the_uncovered_tool():
    records, logs = _discover({"spotify_mixed.py": _MIXED_SRC})
    rec = records[0]
    check(
        "spotify_play's explicit keyword survives unchanged",
        rec.keywords.get("spotify_play") == {"play music": 8},
        rec.keywords,
    )
    check(
        "spotify_pause (no explicit keywords) gets a derived fallback",
        rec.keywords.get("spotify_pause") == {"spotify": 5, "pause": 5},
        rec.keywords,
    )
    check(
        "the per-tool Note names only the tool(s) that needed a fallback",
        any("['spotify_pause']" in line for line in logs),
        logs,
    )


def test_router_actually_activates_on_the_derived_keyword():
    # End-to-end: merge a fallback-only file's keywords the same way
    # tool_registry.py does, and confirm tool_router.route() picks it up —
    # not just that a dict entry exists, but that it clears MIN_SCORE and
    # activates the tool's group. TOOL_KEYWORDS/TOOL_GROUPS are fully
    # replaced (not just updated) for the duration of the call, so this
    # can't pick up an unrelated match from the real, fully-loaded catalog
    # (e.g. a real spotify_play or search_code keyword).
    from jarvis import tool_router

    records, _logs = _discover({"spotify_fallback.py": _NO_KEYWORDS_SRC})
    rec = records[0]

    orig_keywords = dict(tool_router.TOOL_KEYWORDS)
    orig_groups = dict(tool_router.TOOL_GROUPS)
    try:
        tool_router.TOOL_KEYWORDS.clear()
        tool_router.TOOL_KEYWORDS.update(rec.keywords)
        tool_router.TOOL_GROUPS.clear()
        tool_router.TOOL_GROUPS[rec.group] = list(rec.tools)
        result = tool_router.route("can you search for a song on spotify")
        check(
            "the derived 'spotify' keyword routes to the fallback tool's group",
            rec.group in result.groups and "spotify_search" in result.tools,
            result,
        )
    finally:
        tool_router.TOOL_KEYWORDS.clear()
        tool_router.TOOL_KEYWORDS.update(orig_keywords)
        tool_router.TOOL_GROUPS.clear()
        tool_router.TOOL_GROUPS.update(orig_groups)


for fn in [
    test_derive_fallback_keywords_directly,
    test_keywordless_tool_gets_fallback_keywords,
    test_all_stopword_name_still_gets_the_old_warning,
    test_explicit_keywords_are_never_overridden,
    test_mixed_file_only_derives_for_the_uncovered_tool,
    test_router_actually_activates_on_the_derived_keyword,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
