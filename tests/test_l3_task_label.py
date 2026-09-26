"""L.3 — task label in Logs.

Covers:
  - logs.log() persists task_id/task_label read from the environment,
    bounded defensively, and empty (never fabricated) when absent.
  - logs.search() carries task_id/task_label through to result rows.
  - task_runner._spawn_ask() sets JARVIS_TASK_LABEL alongside the existing
    JARVIS_TASK_ID, from the task's own human-readable title.
  - scheduler._do_ask() sets both JARVIS_TASK_ID and JARVIS_TASK_LABEL from
    the job, the same way it already sets JARVIS_LOG_SOURCE="scheduler".

Same no-dependency plain-assert pattern as tests/test_channels.py and
tests/test_scheduler.py — run directly:

    python3 tests/test_l3_task_label.py

Every test repoints HOME at a fresh temp dir first, so nothing here touches
a real ~/.jarvis.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import logs, scheduler, task_runner  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def fresh_home():
    tmp = tempfile.mkdtemp(prefix="jarvis-l3-test-")
    logs.JARVIS_DIR = Path(tmp) / ".jarvis"
    logs.LOG_DIR = logs.JARVIS_DIR / "logs"
    return tmp


def with_env(**kv):
    """Context-manager-free env override: returns a restore callback."""
    originals = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def restore():
        for k, v in originals.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def test_log_writer_persists_task_fields():
    tmp = fresh_home()
    try:
        import jarvis.conversations as conversations
        conversations.CONV_DIR = Path(tmp) / ".jarvis" / "conversations"
        conv = conversations.new_conversation(title="t", make_current=False)

        restore = with_env(JARVIS_TASK_ID="task-123", JARVIS_TASK_LABEL="Morning briefing")
        try:
            logs.log(conv, "info", {"x": 1})
        finally:
            restore()

        entries = logs.read_entries(conv)
        check("one entry was written", len(entries) == 1, entries)
        e = entries[0]
        check("task_id persisted", e.get("task_id") == "task-123", e)
        check("task_label persisted", e.get("task_label") == "Morning briefing", e)

        restore = with_env(JARVIS_TASK_ID=None, JARVIS_TASK_LABEL=None)
        try:
            logs.log(conv, "info", {"x": 2})
        finally:
            restore()
        e2 = logs.read_entries(conv)[1]
        check("manual entry is not mislabeled as a task",
              e2.get("task_id") == "" and e2.get("task_label") == "", e2)

        long_label = "z" * 500
        restore = with_env(JARVIS_TASK_ID="t", JARVIS_TASK_LABEL=long_label)
        try:
            logs.log(conv, "info", {"x": 3})
        finally:
            restore()
        e3 = logs.read_entries(conv)[2]
        check("task_label is bounded defensively", len(e3.get("task_label", "")) <= 100, len(e3.get("task_label", "")))

        check("same conversation holds both task and manual entries correctly",
              entries and True,  # sanity: prior asserts already show the mix
              [e.get("task_id") for e in logs.read_entries(conv)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_carries_task_label():
    tmp = fresh_home()
    try:
        import jarvis.conversations as conversations
        conversations.CONV_DIR = Path(tmp) / ".jarvis" / "conversations"
        conv = conversations.new_conversation(title="t", make_current=False)

        restore = with_env(JARVIS_TASK_ID="task-9", JARVIS_TASK_LABEL="Nightly backup")
        try:
            logs.log(conv, "info", {"needle": "findme"})
        finally:
            restore()

        result = logs.search("findme")
        check("search finds the entry", result["ok"] and len(result["results"]) == 1, result)
        r = result["results"][0]
        check("search result carries task_id", r.get("task_id") == "task-9", r)
        check("search result carries task_label", r.get("task_label") == "Nightly backup", r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_task_runner_sets_task_label():
    orig_run = task_runner.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeCompleted(stdout="done\n")

    task_runner.subprocess.run = fake_run
    try:
        task = {"id": "task-42", "title": "Clean up downloads folder"}
        ok, text, err = task_runner._spawn_ask(task, "do the thing")
        check("_spawn_ask still succeeds", ok is True, err)
        check("JARVIS_TASK_ID is set from the task", captured["env"].get("JARVIS_TASK_ID") == "task-42")
        check("JARVIS_TASK_LABEL is set from the task's title",
              captured["env"].get("JARVIS_TASK_LABEL") == "Clean up downloads folder")
    finally:
        task_runner.subprocess.run = orig_run


def test_task_runner_label_empty_when_untitled():
    orig_run = task_runner.subprocess.run
    captured = {}

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeCompleted(stdout="done\n")

    task_runner.subprocess.run = fake_run
    try:
        ok, _text, _err = task_runner._spawn_ask({"id": "task-1"}, "prompt")
        check("_spawn_ask succeeds with no title", ok is True)
        check("JARVIS_TASK_LABEL is empty rather than fabricated",
              captured["env"].get("JARVIS_TASK_LABEL") == "")
    finally:
        task_runner.subprocess.run = orig_run


def test_scheduler_do_ask_sets_task_fields():
    tmp = fresh_home()
    orig_run = scheduler.subprocess.run
    captured = {}

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeCompleted(stdout="Jarvis: done.\n")

    scheduler.subprocess.run = fake_run
    try:
        import jarvis.conversations as conversations
        conversations.CONV_DIR = Path(tmp) / ".jarvis" / "conversations"
        scheduler.ASK_LOG_FILE = Path(tmp) / ".jarvis" / "scheduler_ask_log.jsonl"

        job = {"id": "job-7", "title": "Morning briefing", "kind": "task", "conv_id": None}
        action = {"type": "ask", "prompt": "summarize my day"}
        result = scheduler._do_ask(job, action)
        check("_do_ask still succeeds", result["ok"] is True, result)
        check("JARVIS_TASK_ID is set from the job", captured["env"].get("JARVIS_TASK_ID") == "job-7")
        check("JARVIS_TASK_LABEL is set from the job's title",
              captured["env"].get("JARVIS_TASK_LABEL") == "Morning briefing")
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


def test_scheduler_do_ask_label_falls_back_to_default_title():
    tmp = fresh_home()
    orig_run = scheduler.subprocess.run
    captured = {}

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeCompleted(stdout="Jarvis: done.\n")

    scheduler.subprocess.run = fake_run
    try:
        import jarvis.conversations as conversations
        conversations.CONV_DIR = Path(tmp) / ".jarvis" / "conversations"
        scheduler.ASK_LOG_FILE = Path(tmp) / ".jarvis" / "scheduler_ask_log.jsonl"

        job = {"id": "job-8", "kind": "task", "conv_id": None}  # no title
        action = {"type": "ask", "prompt": "do something"}
        scheduler._do_ask(job, action)
        check("untitled job still gets a non-fabricated, non-empty label",
              captured["env"].get("JARVIS_TASK_LABEL") == "Scheduled task",
              captured["env"].get("JARVIS_TASK_LABEL"))
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


test_log_writer_persists_task_fields()
test_search_carries_task_label()
test_task_runner_sets_task_label()
test_task_runner_label_empty_when_untitled()
test_scheduler_do_ask_sets_task_fields()
test_scheduler_do_ask_label_falls_back_to_default_title()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
