"""Tests for ask_output.py (master plan Part D.2 — notification summaries)
and its wiring into notifier.py, scheduler.py and task_runner.py.

Run: python3 tests/test_ask_output.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ask_output, notifier, scheduler, task_runner  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_ask_output_test_"))
    notifier.JARVIS_DIR = tmp
    notifier.INBOX_FILE = tmp / "notifications.json"
    notifier.CONFIG_FILE = tmp / "notify_config.json"
    return tmp


# --- strip_protocol_lines ----------------------------------------------

def test_strips_usage_and_confirm_markers():
    raw = 'Here is your answer.\nJARVIS_USAGE {"input_tokens": 5}\nJARVIS_CONFIRM_REQUEST {"tool": "x"}\nAll done.'
    cleaned = ask_output.strip_protocol_lines(raw)
    check("usage marker gone", "JARVIS_USAGE" not in cleaned, cleaned)
    check("confirm marker gone", "JARVIS_CONFIRM_REQUEST" not in cleaned, cleaned)
    check("real text kept", "Here is your answer." in cleaned and "All done." in cleaned, cleaned)


def test_strip_is_a_noop_on_ordinary_text():
    text = "Take out the trash\nand water the plants"
    check("unaffected", ask_output.strip_protocol_lines(text) == text)


def test_strip_handles_falsy_input():
    check("None passes through", ask_output.strip_protocol_lines(None) is None)
    check("empty string passes through", ask_output.strip_protocol_lines("") == "")


def test_scheduler_alias_is_the_same_function():
    check("scheduler re-exports the shared implementation",
          scheduler._strip_protocol_lines is ask_output.strip_protocol_lines)


# --- summarize -----------------------------------------------------------

def test_summarize_short_single_line():
    result = ask_output.summarize("Done: sent the email.")
    check("summary is the whole line", result["summary"] == "Done: sent the email.")
    check("not truncated", result["truncated"] is False)
    check("full matches", result["full"] == "Done: sent the email.")


def test_summarize_multi_line_flags_truncated():
    result = ask_output.summarize("First line here.\nSecond line with more detail.")
    check("summary is first line only", result["summary"] == "First line here.")
    check("truncated because more follows", result["truncated"] is True)
    check("full keeps everything", "Second line" in result["full"])


def test_summarize_long_first_line_gets_cut_with_ellipsis():
    long_line = "x" * 250
    result = ask_output.summarize(long_line, max_chars=50)
    check("summary capped", len(result["summary"]) <= 51)
    check("ellipsis added", result["summary"].endswith("…"))
    check("truncated flagged", result["truncated"] is True)


def test_summarize_strips_markers_first():
    raw = 'JARVIS_USAGE {"a": 1}\nActual reply text.'
    result = ask_output.summarize(raw)
    check("marker never leaks into summary", "JARVIS_USAGE" not in result["summary"])
    check("marker never leaks into full", "JARVIS_USAGE" not in result["full"])
    check("real text surfaces", result["summary"] == "Actual reply text.")


def test_summarize_empty_text():
    result = ask_output.summarize("")
    check("empty summary", result == {"summary": "", "truncated": False, "full": ""})


def test_summarize_blank_lines_dont_count_as_more_content():
    result = ask_output.summarize("Only line here.\n\n   \n")
    check("not truncated when rest is blank", result["truncated"] is False, result)


# --- notifier.notify() integration --------------------------------------

def test_notify_strips_markers_from_message():
    tmp = fresh()
    try:
        record = notifier.notify(
            title="Job done",
            message='JARVIS_USAGE {"input_tokens": 12}\nThe report is ready.',
            channels=["inbox"], kind="task",
        )
        check("message cleaned", "JARVIS_USAGE" not in record["message"], record["message"])
        check("real content kept", "The report is ready." in record["message"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notify_populates_summary_fields():
    tmp = fresh()
    try:
        record = notifier.notify(
            title="Job done",
            message="Step one complete.\nStep two also complete.\nStep three too.",
            channels=["inbox"], kind="task",
        )
        check("summary is first line", record["summary"] == "Step one complete.")
        check("flagged as truncated", record["summary_truncated"] is True)
        check("full message still there for expand", "Step three too." in record["message"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notify_short_message_not_flagged_truncated():
    tmp = fresh()
    try:
        record = notifier.notify(title="Hi", message="quick note", channels=["inbox"])
        check("summary equals message", record["summary"] == "quick note")
        check("not truncated", record["summary_truncated"] is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pending_and_history_heal_old_records_missing_summary():
    tmp = fresh()
    try:
        # Simulate a notification written by a pre-D.2 build: no summary
        # field, and a raw marker line still sitting in the message.
        import json
        old_record = {
            "id": "abc123", "title": "Old", "kind": "task",
            "message": 'JARVIS_USAGE {"x": 1}\nOld task finished fine.',
            "job_id": None, "conv_id": None, "failed": False, "actions": [],
            "created_at": "2026-01-01T00:00:00", "seen_by": [],
            "delivered": ["inbox"], "failed_channels": [],
        }
        notifier.INBOX_FILE.write_text(json.dumps([old_record]), encoding="utf-8")

        healed = notifier.history(limit=10)[0]
        check("old record gets a summary on read", healed.get("summary") == "Old task finished fine.", healed)
        check("old record's message is cleaned on read", "JARVIS_USAGE" not in healed["message"])

        healed_pending = notifier.pending("web")[0]
        check("pending() heals too", healed_pending.get("summary") == "Old task finished fine.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_toast_prefers_summary_over_full_message(monkeypatch):
    tmp = fresh()
    try:
        seen = {}

        def fake_linux(title, text, record):
            seen["text"] = text
            return True

        monkeypatch.setattr(notifier, "_toast_linux", fake_linux)
        monkeypatch.setattr(sys, "platform", "linux")
        notifier.notify(
            title="T",
            message="Short headline.\nA lot more detail that shouldn't show in a toast.",
            channels=["toast"],
        )
        check("toast used the summary, not the full body",
              seen.get("text") == "Short headline.", seen)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- task_runner.py integration -----------------------------------------

def test_spawn_ask_output_is_stripped_before_control_parsing(monkeypatch):
    raw_stdout = (
        'JARVIS_USAGE {"input_tokens": 3}\n'
        'The task step finished.\n'
        'JARVIS_TASK {"step": "done", "task": "continue", "summary": "did a thing"}\n'
    )

    class FakeProc:
        stdout = raw_stdout
        stderr = ""
        returncode = 0

    monkeypatch.setattr(task_runner.subprocess, "run", lambda *a, **k: FakeProc())
    ok, text, error = task_runner._spawn_ask({"id": "t1"}, "prompt")
    check("spawn succeeded", ok is True)
    check("usage marker stripped from captured text", "JARVIS_USAGE" not in text, text)
    check("task control line still present for tasks.parse_control", "JARVIS_TASK" in text)

    control = task_runner.tasks.parse_control(text, task_runner.CONTROL_MARKER)
    check("control still parses correctly after stripping", control.get("summary") == "did a thing", control)
    body = task_runner.tasks.strip_control(text, task_runner.CONTROL_MARKER)
    check("body shown to a human has no leaked marker JSON", "JARVIS_USAGE" not in body and "JARVIS_TASK" not in body, body)
    check("body keeps the real reply", "The task step finished." in body)


class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def run(self, fn):
        try:
            fn(self)
        finally:
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            _MonkeyPatch().run(fn)
        else:
            fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(_run())
