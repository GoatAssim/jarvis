"""Tests for jarvis/conv_search.py — full-text search across conversations.

Run: python3 ../tests/test_conv_search.py   (from jarvis-cli/)

Seeds a temp ~/.jarvis/conversations with a few known transcripts, so every
assertion is about search behaviour rather than about whatever happens to be
in the real history.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import conv_search, conversations  # noqa: E402

PASS, FAIL = [], []

A, B, C = "a" * 16, "b" * 16, "c" * 16


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


SEED = [
    (A, "Postgres indexing chat", "2026-09-10T10:00:00+00:00", [
        ("2026-09-01T10:00:00+00:00", "how do I speed up a slow postgres query",
         "Add a B-tree index on the filtered column."),
        ("2026-09-02T10:00:00+00:00", "what about composite indexes",
         "Composite indexes help when you filter on both columns."),
    ]),
    (B, "Weekend plans", "2026-09-11T10:00:00+00:00", [
        ("2026-09-03T10:00:00+00:00", "suggest something for saturday", "How about a hike?"),
        ("2026-09-04T10:00:00+00:00", "any good postgres books",
         "'The Art of PostgreSQL' is excellent."),
    ]),
    (C, "Deploy notes", "2026-09-12T10:00:00+00:00", [
        ("2026-09-05T10:00:00+00:00", "deploy the staging box", "Done."),
    ]),
]


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_search_test_"))
    conversations.JARVIS_DIR = tmp
    conversations.CONV_DIR = tmp / "conversations"
    conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
    conversations.CURRENT_FILE = tmp / "current_conversation.json"
    conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)

    index = []
    for cid, title, updated, turns in SEED:
        record = {
            "id": cid, "title": title, "soft_context": "",
            "created_at": "2026-09-01T09:00:00+00:00", "updated_at": updated,
            "exchanges": [{"ts": ts, "user": u, "jarvis": j} for ts, u, j in turns],
        }
        (conversations.CONV_DIR / f"{cid}.json").write_text(json.dumps(record), encoding="utf-8")
        index.append({"id": cid, "title": title, "soft_context": "", "updated_at": updated})
    conversations.INDEX_FILE.write_text(json.dumps(index), encoding="utf-8")
    return tmp


def ids(results):
    return sorted(r["id"] for r in results)


def test_words_mode_is_order_independent():
    tmp = fresh()
    try:
        forward = conv_search.search("postgres index")
        backward = conv_search.search("index postgres")
        check("all terms must appear, in any order", ids(forward) == ids(backward) == [A])
        check("a term that appears nowhere kills the match",
              conv_search.search("postgres kangaroo") == [])
        check("matching is case-insensitive", ids(conv_search.search("POSTGRES QUERY")) == [A])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_it_searches_replies_not_just_prompts():
    tmp = fresh()
    try:
        # "B-tree" only ever appears in an assistant reply. Searching only
        # user messages (or only titles, as conv-list does) would miss it —
        # which is the gap this module exists to close.
        check("text from a reply is searchable", ids(conv_search.search("b-tree")) == [A])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phrase_mode():
    tmp = fresh()
    try:
        check("an exact phrase matches", ids(conv_search.search("composite indexes", mode="phrase")) == [A])
        check("the same words out of order do NOT match as a phrase",
              conv_search.search("indexes composite", mode="phrase") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phrase_does_not_match_across_the_turn_seam():
    tmp = fresh()
    try:
        # user ends "...staging box", reply is "Done." — joined with a
        # newline precisely so a phrase can't span the boundary.
        check("a phrase can't span user->reply", conv_search.search("box Done", mode="phrase") == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_regex_mode():
    tmp = fresh()
    try:
        check("a real regex works", ids(conv_search.search("B-?tree", mode="regex")) == [A])
        check("regex is case-insensitive by default", ids(conv_search.search("b-?TREE", mode="regex")) == [A])
        try:
            conv_search.search("(unclosed", mode="regex")
            check("a broken regex raises SearchError", False, "no error raised")
        except conv_search.SearchError:
            check("a broken regex raises SearchError rather than crashing", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_snippets_mark_the_match():
    tmp = fresh()
    try:
        result = conv_search.search("composite", mode="phrase")[0]
        snippet = result["matches"][0]["snippet"]
        check("the snippet marks the match for highlighting",
              "\u00abcomposite\u00bb" in snippet.lower(), snippet)
        check("the snippet carries surrounding context", len(snippet) > len("composite") + 10)
        check("the match count is reported", result["match_count"] >= 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scoping_and_ordering():
    tmp = fresh()
    try:
        both = conv_search.search("postgres")
        check("a term in two conversations finds both", ids(both) == [A, B])
        check("results are most-recently-updated first", both[0]["id"] == B, both[0]["id"])
        scoped = conv_search.search("postgres", conv_id=B)
        check("scoping to one conversation works", ids(scoped) == [B])
        check("limit is honoured", len(conv_search.search("postgres", limit=1)) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_date_bounds():
    tmp = fresh()
    try:
        # Bounds are naive dates compared against tz-aware stored timestamps;
        # normalizing both sides is what stops a TypeError mid-scan.
        check("since excludes older turns", conv_search.search("postgres", since="2026-09-04")[0]["id"] == B)
        check("until excludes newer turns", ids(conv_search.search("postgres", until="2026-09-02")) == [A])
        check("an unparseable bound is ignored rather than fatal",
              len(conv_search.search("postgres", since="not a date")) == 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tool_content_is_opt_in():
    tmp = fresh()
    try:
        record = json.loads((conversations.CONV_DIR / f"{C}.json").read_text(encoding="utf-8"))
        record["exchanges"][0]["extras"] = [
            {"type": "screenshot", "data": {"filename": "kangaroo.png"}}]
        (conversations.CONV_DIR / f"{C}.json").write_text(json.dumps(record), encoding="utf-8")

        # Default off: "find where I talked about X" shouldn't match a file
        # path that merely contained X.
        check("tool content is not searched by default", conv_search.search("kangaroo") == [])
        check("but can be opted into", ids(conv_search.search("kangaroo", include_tools=True)) == [C])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_guards():
    tmp = fresh()
    try:
        for bad in ("", "   "):
            try:
                conv_search.search(bad)
                check(f"{bad!r} is rejected", False, "searched anyway")
            except conv_search.SearchError:
                check(f"an empty query {bad!r} is rejected", True)
        try:
            conv_search.search("x" * (conv_search.MAX_PATTERN_CHARS + 1))
            check("an overlong query is rejected", False, "accepted")
        except conv_search.SearchError:
            check("an overlong query is rejected", True)
        try:
            conv_search.search("x", mode="telepathy")
            check("an unknown mode is rejected", False, "accepted")
        except conv_search.SearchError:
            check("an unknown mode is rejected", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rendering():
    tmp = fresh()
    try:
        results = conv_search.search("postgres")
        counts = conv_search.summarize(results)
        check("summarize counts conversations", counts["conversations"] == 2)
        check("summarize counts matches", counts["matches"] >= 2)
        text = conv_search.render_for_terminal(results, "postgres")
        check("terminal rendering names the conversations", "Postgres indexing chat" in text)
        check("empty results render a sentence, not a crash",
              "No conversations matched" in conv_search.render_for_terminal([], "zzz"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


for fn in [
    test_words_mode_is_order_independent, test_it_searches_replies_not_just_prompts,
    test_phrase_mode, test_phrase_does_not_match_across_the_turn_seam,
    test_regex_mode, test_snippets_mark_the_match, test_scoping_and_ordering,
    test_date_bounds, test_tool_content_is_opt_in, test_guards, test_rendering,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
