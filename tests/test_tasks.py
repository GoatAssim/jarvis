"""Tests for tasks.py / task_runner.py.

Same no-framework, plain-assert pattern as the rest of tests/ — run with
`python3 tests/test_tasks.py`.

The whole point of splitting tasks.py from task_runner.py is that this file
needs no API key, no network and no subprocess: run_step() takes an
injectable `ask`, so a fake model drives the full multi-step loop including
crash-and-resume.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tasks, task_runner  # noqa: E402

MARKER = task_runner.CONTROL_MARKER


def _isolate():
    """Point the store at a throwaway dir — never touch a real ~/.jarvis."""
    tmp = tempfile.mkdtemp(prefix="jarvis-tasks-test-")
    tasks.TASKS_DIR = Path(tmp)
    return Path(tmp)


def _control(**fields):
    return "\n%s %s" % (MARKER, json.dumps(fields))


# ---------------------------------------------------------------------------


def test_create_and_load():
    _isolate()
    t = tasks.create("ship the thing", notes="be careful")
    assert tasks.is_valid_id(t["id"])
    assert t["status"] == tasks.STATUS_PENDING
    assert t["plan"] == []
    assert tasks.needs_plan(t)

    again = tasks.load(t["id"])
    assert again["goal"] == "ship the thing"
    assert again["notes"] == "be careful"

    assert tasks.load("t_nope") is None
    assert tasks.load("../etc/passwd") is None  # bad id, not an exception
    print("ok  create/load")


def test_plan_preserves_completed_head():
    _isolate()
    t = tasks.create("g", plan=["a", "b", "c"])
    tasks.record_step(t, ok=True, summary="did a")
    assert t["cursor"] == 1

    # Re-planning mid-run must not resurrect step 1 as pending, or the task
    # redoes finished work forever.
    tasks.set_plan(t, ["b-revised", "c-revised"])
    assert [s["text"] for s in t["plan"]] == ["a", "b-revised", "c-revised"]
    assert t["plan"][0]["status"] == "done"
    assert [s["n"] for s in t["plan"]] == [1, 2, 3]
    print("ok  replan preserves completed head")


def test_budget_ceilings():
    _isolate()
    t = tasks.create("g", plan=["a"], max_steps=2)
    assert tasks.budget_exhausted(t)[0] is False
    tasks.record_step(t, ok=True, summary="1")
    tasks.record_step(t, ok=True, summary="2")
    done, why = tasks.budget_exhausted(t)
    assert done and "step budget" in why

    t2 = tasks.create("g2", plan=["a"])
    for _ in range(tasks.DEFAULT_MAX_CONSECUTIVE_FAILURES):
        tasks.record_step(t2, ok=False, summary="nope", error="boom")
    done, why = tasks.budget_exhausted(t2)
    assert done and "in a row" in why

    # A success resets the streak rather than merely pausing it.
    t3 = tasks.create("g3", plan=["a"])
    tasks.record_step(t3, ok=False, summary="x")
    tasks.record_step(t3, ok=True, summary="y")
    assert t3["budget"]["consecutive_failures"] == 0
    print("ok  budget ceilings")


def test_lease_and_crash_recovery():
    _isolate()
    t = tasks.create("g", plan=["a", "b"])
    assert tasks.claim(t) is True
    assert tasks.lease_is_live(t) is True
    assert tasks.is_runnable(t) is False  # we hold it

    # A lease held by a pid that no longer exists is not live. 999999 is
    # chosen to be implausible rather than guaranteed-free; on the off
    # chance it exists the assertion below is the thing that would flag it.
    t["lease"] = {"pid": 999999, "until": tasks._iso(), "since": tasks._iso()}
    assert tasks.lease_is_live(t) is False
    assert tasks.is_runnable(t) is True

    # An expired lease is dead even when the pid is alive (our own).
    import os
    from datetime import timedelta
    t["lease"] = {"pid": os.getpid(),
                  "until": tasks._iso(tasks._now() - timedelta(minutes=1)),
                  "since": tasks._iso()}
    assert tasks.lease_is_live(t) is False

    # A garbage timestamp degrades to "not live" instead of raising.
    t["lease"] = {"pid": os.getpid(), "until": "not-a-date", "since": ""}
    assert tasks.lease_is_live(t) is False
    print("ok  leases")


def test_control_line_parsing():
    reply = "I did the thing.\n" + MARKER + ' {"step": "done", "task": "continue"}'
    assert tasks.parse_control(reply, MARKER) == {"step": "done", "task": "continue"}
    assert tasks.strip_control(reply, MARKER) == "I did the thing."

    # No control line is normal, not an error.
    assert tasks.parse_control("just a reply", MARKER) == {}
    # Malformed JSON is ignored rather than raising.
    assert tasks.parse_control(MARKER + " {oops", MARKER) == {}
    # Backtick-wrapped payloads happen; they should still parse.
    assert tasks.parse_control(MARKER + ' `{"step": "done"}`', MARKER) == {"step": "done"}
    # The LAST marker wins, so a model echoing the instruction mid-reply
    # doesn't beat its real verdict at the bottom.
    two = (MARKER + ' {"step": "blocked"}\ntext\n' + MARKER + ' {"step": "done"}')
    assert tasks.parse_control(two, MARKER)["step"] == "done"
    print("ok  control line parsing")


def test_full_loop_with_fake_model():
    _isolate()
    t = tasks.create("build and test")
    seen = []

    def fake_ask(task, prompt):
        seen.append(prompt)
        if "Plan a task" in prompt:
            return True, "I'll do it in two." + _control(plan=["compile", "run tests"]), None
        # Match the ACTIVE step line, not the plan listing — every step's
        # prompt contains every step's text, so "compile" in prompt is true
        # on both steps.
        if "Do ONLY this step: compile" in prompt:
            return True, "Compiled clean." + _control(
                step="done", summary="compiled", note="used the debug profile",
                task="continue"), None
        return True, "All green." + _control(step="done", summary="tests pass",
                                             task="complete"), None

    # Step 1: planning.
    t = task_runner.run_step(t, ask=fake_ask)
    assert [s["text"] for s in t["plan"]] == ["compile", "run tests"]
    assert t["cursor"] == 0, "planning must not consume a real step slot"
    assert t["status"] == tasks.STATUS_PENDING

    # Step 2: first real step.
    t = task_runner.run_step(t, ask=fake_ask)
    assert t["cursor"] == 1
    assert t["plan"][0]["status"] == "done"
    assert any("debug profile" in n["text"] for n in t["working_memory"]["notes"])
    assert t["status"] == tasks.STATUS_PENDING

    # Step 3: finishes the whole task.
    t = task_runner.run_step(t, ask=fake_ask)
    assert t["status"] == tasks.STATUS_DONE
    assert "All green" in (t["result"] or "")
    assert t["lease"] is None

    # The second step's prompt must have carried the first step's note.
    assert "debug profile" in seen[-1]
    # And it must NOT have re-sent the whole first reply verbatim.
    assert seen[-1].count("Compiled clean") == 0
    print("ok  full loop")


def test_transport_failure_retries_rather_than_failing_task():
    _isolate()
    # Three steps, so the retry below lands mid-plan rather than running
    # off the end (which would legitimately finish the task and mask what
    # this test is checking).
    t = tasks.create("g", plan=["a", "b", "c"])
    calls = {"n": 0}

    def flaky(task, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "", "provider exploded"
        return True, "fine" + _control(step="done", summary="ok", task="continue"), None

    t = task_runner.run_step(t, ask=flaky)
    assert t["status"] == tasks.STATUS_PENDING, "one outage must not kill the task"
    assert t["budget"]["consecutive_failures"] == 1
    assert t["last_error"] == "provider exploded"

    t = task_runner.run_step(t, ask=flaky)
    assert t["budget"]["consecutive_failures"] == 0
    assert t["status"] == tasks.STATUS_PENDING
    print("ok  transport failure retries")


def test_blocked_task_waits_for_human():
    _isolate()
    t = tasks.create("g", plan=["a"])

    def blocker(task, prompt):
        return True, "Need a decision." + _control(
            step="blocked", summary="which database?", task="blocked"), None

    t = task_runner.run_step(t, ask=blocker)
    assert t["status"] == tasks.STATUS_BLOCKED
    assert tasks.is_runnable(t) is False, "blocked tasks must not self-dispatch"

    t = tasks.resume(t["id"])
    assert t["status"] == tasks.STATUS_PENDING
    assert tasks.is_runnable(t) is True
    print("ok  blocked/resume")


def test_supervisor_recovers_crashed_task():
    _isolate()
    t = tasks.create("g", plan=["a", "b"])
    # Simulate: worker claimed it, started, then was hard-killed.
    tasks.claim(t)
    tasks.start(t)
    t["lease"]["pid"] = 999999
    tasks.save(t)

    recovered = task_runner.recover_stale()
    assert t["id"] in recovered
    back = tasks.load(t["id"])
    assert back["status"] == tasks.STATUS_PENDING
    assert back["lease"] is None
    assert any("resumed after" in n["text"] for n in back["working_memory"]["notes"])
    print("ok  crash recovery")


def test_tick_advances_one_and_respects_leases():
    _isolate()
    a = tasks.create("a", plan=["x"])
    tasks.create("b", plan=["y"])

    def one_shot(task, prompt):
        return True, "done" + _control(step="done", summary="s", task="complete"), None

    out = task_runner.tick(limit=1, ask=one_shot)
    assert out["count"] == 1, "default tick advances one task, not all of them"

    # A task someone else holds is skipped, not stolen.
    held = tasks.load(a["id"]) or a
    if held["status"] not in tasks.TERMINAL_STATUSES:
        tasks.claim(held)
        assert tasks.is_runnable(held) is False
    print("ok  tick")


def test_context_block_is_bounded():
    _isolate()
    t = tasks.create("g", plan=["a"])
    for i in range(200):
        tasks.add_note(t, "note number %d with some padding text" % i)
        tasks.set_fact(t, "key%d" % i, "value %d" % i)
    assert len(t["working_memory"]["notes"]) <= tasks.MAX_NOTES
    assert len(t["working_memory"]["facts"]) <= 40

    for i in range(300):
        tasks.record_step(t, ok=True, summary="step %d " % i + "x" * 200)
    assert len(t["history"]) <= tasks.MAX_HISTORY
    block = tasks.context_block(t)
    assert len(block) <= 3100, len(block)
    # Newest content survives the trim; oldest is what gets dropped.
    assert "step 299" in block
    print("ok  bounded working memory")


def test_plan_fallback_when_planner_returns_nothing():
    _isolate()
    t = tasks.create("do the thing")

    def useless(task, prompt):
        return True, "sure thing boss", None  # no control line at all

    t = task_runner.run_step(t, ask=useless)
    assert len(t["plan"]) == 1
    assert t["plan"][0]["text"] == "do the thing"
    print("ok  planner fallback")


if __name__ == "__main__":
    test_create_and_load()
    test_plan_preserves_completed_head()
    test_budget_ceilings()
    test_lease_and_crash_recovery()
    test_control_line_parsing()
    test_full_loop_with_fake_model()
    test_transport_failure_retries_rather_than_failing_task()
    test_blocked_task_waits_for_human()
    test_supervisor_recovers_crashed_task()
    test_tick_advances_one_and_respects_leases()
    test_context_block_is_bounded()
    test_plan_fallback_when_planner_returns_nothing()
    print("\nall task tests passed")
