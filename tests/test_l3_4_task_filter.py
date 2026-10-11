"""L.3.4 -- filter log search by task.

logs.search(task=...) keeps only entries that belong to a task: its id exactly,
or any part of its label (case-insensitive). With a task given the query may be
empty. Entries with no task never match, so a manual entry is not mislabeled.
The `logs-search --task` CLI flag passes it through.

Run directly:  python3 tests/test_l3_4_task_filter.py
Repoints the log dir at a temp dir first; touches no real ~/.jarvis.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


CONV = "a1b2c3d4e5f60718"
OTHER = "0f1e2d3c4b5a6978"


def fresh_home():
    tmp = tempfile.mkdtemp(prefix="jarvis-l34-test-")
    logs.JARVIS_DIR = Path(tmp) / ".jarvis"
    logs.LOG_DIR = logs.JARVIS_DIR / "logs"
    return tmp


def seed():
    logs.log(CONV, "request", {"text": "hello there"})                       # manual entry
    logs.log(CONV, "info", {"text": "backup started"}, source="scheduler",
             task_id="9a8b7c6d", task_label="Nightly backup")
    logs.log(CONV, "response", {"text": "backup finished"}, source="scheduler",
             task_id="9a8b7c6d", task_label="Nightly backup")
    logs.log(OTHER, "info", {"text": "weather checked"}, source="scheduler",
             task_id="11223344", task_label="Morning weather")


def ids(result):
    return [(r["conv_id"], r["task_label"]) for r in result["results"]]


def test_filter_by_label_substring():
    tmp = fresh_home()
    try:
        seed()
        res = logs.search("", task="backup")
        check("a task filter alone (empty query) is a valid search", res["ok"] is True, res)
        check("it finds exactly that task's entries", len(res["results"]) == 2, res)
        check("every hit carries the task label", all(r["task_label"] == "Nightly backup" for r in res["results"]))
        check("the manual entry is not a hit", all(r["task_id"] for r in res["results"]))
        check("the result echoes the filter", res.get("task") == "backup")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_filter_is_case_insensitive_and_by_id():
    tmp = fresh_home()
    try:
        seed()
        check("label match ignores case", len(logs.search("", task="NIGHTLY")["results"]) == 2)
        check("an exact task id matches", len(logs.search("", task="9a8b7c6d")["results"]) == 2)
        check("a PART of an id does not match by id", len(logs.search("", task="9a8b")["results"]) == 0)
        none = logs.search("", task="no such task")
        check("an unknown task finds nothing, not an error", none["ok"] is True and none["results"] == [], none)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_filter_combines_with_query_and_other_filters():
    tmp = fresh_home()
    try:
        seed()
        res = logs.search("finished", task="backup")
        check("query AND task: only the matching entry of that task", len(res["results"]) == 1, res)
        check("...and it is the response", res["results"][0]["direction"] == "response")
        check("query that only another task has finds nothing under this task",
              logs.search("weather", task="backup")["results"] == [])
        res = logs.search("", task="backup", directions=["info"])
        check("task AND direction", len(res["results"]) == 1 and res["results"][0]["direction"] == "info", res)
        res = logs.search("", task="weather", conv_id=OTHER)
        check("task AND conv_id", ids(res) == [(OTHER, "Morning weather")], res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_task_filter_behaves_as_before():
    tmp = fresh_home()
    try:
        seed()
        check("an empty query with NO task is still an error", logs.search("")["ok"] is False)
        check("a normal query without a task still finds every match",
              len(logs.search("text")["results"]) == 4)
        check("a whitespace-only task is no filter", len(logs.search("text", task="   ")["results"]) == 4)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_query_snippet_is_not_blank():
    tmp = fresh_home()
    try:
        seed()
        res = logs.search("", task="backup")
        check("filter-only hits still carry a readable snippet", all(r["snippet"] for r in res["results"]), res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_passes_task_through():
    import io
    import contextlib
    from jarvis import channels_cli
    tmp = fresh_home()
    try:
        seed()
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                channels_cli.handle(["logs-search", "--task", "backup"])
            except SystemExit as e:
                code = e.code
        out = json.loads(buf.getvalue())
        check("`logs-search --task backup` exits 0", code == 0, code)
        check("...returns that task's two entries", len(out["results"]) == 2, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


test_filter_by_label_substring()
test_filter_is_case_insensitive_and_by_id()
test_filter_combines_with_query_and_other_filters()
test_no_task_filter_behaves_as_before()
test_empty_query_snippet_is_not_blank()
test_cli_passes_task_through()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
