"""Regression tests for bugs found auditing the tree.

Each test here corresponds to a defect that existed before this patch set and
would have gone on silently failing. Run with
`python3 tests/test_robustness_fixes.py`.

The scheduler ones matter most: that loop is the process that has to still be
alive tomorrow morning for a 7am reminder to fire.
"""

import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import dev_agent_events, sched_daemon, scheduler  # noqa: E402


def _fresh_store():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-robust-"))
    scheduler.STORE_FILE = tmp / "sched.json"
    scheduler.LOCK_FILE = tmp / "sched.lock"
    return tmp


def _past():
    return scheduler.timespec.to_iso(datetime.now() - timedelta(minutes=5))


def _good_job(job_id="j_ok", title="innocent bystander"):
    return {
        "id": job_id, "kind": "reminder", "title": title, "status": "pending",
        "trigger": {"type": "at", "at": _past()},
        "action": {"type": "notify", "message": "y"},
        "next_run": _past(), "run_count": 0,
    }


# ---------------------------------------------------------------------------
# BUG 1 — a malformed job killed the whole tick, and with it the daemon.
#
# tick()'s docstring has always promised "a job that blows up is recorded on
# itself and the loop continues". _run_action() was guarded, but the code
# AROUND it wasn't: job["trigger"], job["kind"] and job["title"] were
# unchecked subscripts. A store missing any of them raised KeyError straight
# out of tick(), and sched_daemon called tick() unguarded.
# ---------------------------------------------------------------------------

MALFORMED = {
    "missing trigger": {"id": "j_bad", "kind": "reminder", "title": "t",
                        "status": "pending", "catch_up": True,
                        "action": {"type": "notify", "message": "x"},
                        "next_run": None, "run_count": 0},
    "missing title": {"id": "j_bad", "kind": "reminder", "status": "pending",
                      "trigger": {"type": "at"},
                      "action": {"type": "notify", "message": "x"},
                      "next_run": None, "run_count": 0},
    "missing kind": {"id": "j_bad", "title": "t", "status": "pending",
                     "trigger": {"type": "at"},
                     "action": {"type": "notify", "message": "x"},
                     "next_run": None, "run_count": 0},
    "trigger is a string": {"id": "j_bad", "kind": "reminder", "title": "t",
                            "status": "pending", "trigger": "at",
                            "catch_up": True,
                            "action": {"type": "notify", "message": "x"},
                            "next_run": None, "run_count": 0},
}


def test_malformed_job_does_not_abort_the_tick():
    for label, bad in MALFORMED.items():
        _fresh_store()
        bad = dict(bad)
        bad["next_run"] = _past()
        if isinstance(bad.get("trigger"), dict):
            bad["trigger"]["at"] = _past()
        scheduler.save_store({"jobs": [bad, _good_job()], "last_tick": None})

        out = scheduler.tick()   # must not raise

        fired = [r["id"] for r in out.get("ran", [])]
        assert "j_ok" in fired, (label, "unrelated reminder was stranded", fired)

        # The invariant is "it left the pending state", not "it errored".
        # After the defensive fixes most of these shapes are fully
        # recoverable and finish as `done` — which is a better outcome than
        # being parked. What must never happen is staying pending, because
        # that is the re-fire loop.
        store = scheduler.load_store()
        broken = next(j for j in store["jobs"] if j["id"] == "j_bad")
        assert broken["status"] != scheduler.STATUS_PENDING, (label, broken["status"])
        if broken["status"] == scheduler.STATUS_ERROR:
            assert broken.get("last_error"), label
    print("ok  malformed job is recorded, not fatal (%d shapes)" % len(MALFORMED))


def test_non_dict_trigger_does_not_kill_the_whole_scan():
    """A separate bug from the one above, and a worse one.

    due_jobs() used `job.get("trigger") or {}`, which defends against a
    MISSING trigger but not a wrong-typed one. A trigger that is a bare
    string hit .get() on a str and raised out of due_jobs() — which runs
    BEFORE tick()'s per-job loop, so the per-job guard never saw it. One bad
    job stopped the entire scan, so no reminder anywhere ever fired again.
    """
    _fresh_store()
    bad = {"id": "j_bad", "kind": "reminder", "title": "t", "status": "pending",
           "trigger": "at", "action": {"type": "notify", "message": "x"},
           "next_run": _past(), "run_count": 0}
    scheduler.save_store({"jobs": [bad, _good_job()], "last_tick": None})

    due = scheduler.due_jobs()          # must not raise
    assert any(j["id"] == "j_ok" for j in due)

    out = scheduler.tick()              # must not raise either
    assert "j_ok" in [r["id"] for r in out.get("ran", [])]

    # Junk that isn't even a dict is skipped rather than crashed on.
    _fresh_store()
    scheduler.save_store({"jobs": ["not a job", None, 42, _good_job()],
                          "last_tick": None})
    assert [j["id"] for j in scheduler.due_jobs()] == ["j_ok"]
    print("ok  non-dict trigger doesn't kill the scan")


def test_broken_job_does_not_refire_every_tick():
    """The nastier half of bug 1: the crash could land after the action had
    already fired but before save_store(), leaving status=pending. The next
    tick then re-fired the same notification and crashed again — a duplicate
    -notification crash loop. Parking it at STATUS_ERROR is what stops it,
    because due_jobs() only returns pending jobs."""
    _fresh_store()
    bad = dict(MALFORMED["missing kind"])
    bad["next_run"] = _past()
    bad["trigger"] = {"type": "at", "at": _past()}
    scheduler.save_store({"jobs": [bad], "last_tick": None})

    first = scheduler.tick()
    assert any(r["id"] == "j_bad" for r in first.get("ran", []))

    second = scheduler.tick()
    assert not any(r["id"] == "j_bad" for r in second.get("ran", [])), \
        "broken job fired a second time — the crash loop is back"
    print("ok  broken job is parked, not re-fired")


def test_daemon_survives_a_tick_that_raises():
    """Defence in depth: even if something under tick() raises anyway, the
    daemon must lose one pass, not every future pass."""
    real = scheduler.tick
    try:
        def exploding(*a, **kw):
            raise RuntimeError("something deep under tick blew up")
        scheduler.tick = exploding
        out = sched_daemon._tick_scheduler()
        assert out["ok"] is False
        assert "RuntimeError" in out["error"]
        assert out["ran"] == [], "caller's result.get('ran') contract must hold"
    finally:
        scheduler.tick = real
    print("ok  daemon survives a raising tick")


# ---------------------------------------------------------------------------
# BUG 2 — dev_agent_events.emit() violated its own "never raises" contract.
#
# It caught (TypeError, ValueError), but json.dumps(default=str) means the
# failure that actually arrives is str()/__repr__() itself raising, which can
# be any exception. A RuntimeError out of a field's __repr__ escaped emit()
# and aborted the dev_agent loop over a logging call.
# ---------------------------------------------------------------------------


def test_emit_survives_a_field_whose_repr_raises():
    class Hostile:
        def __repr__(self):
            raise RuntimeError("boom")

    event = dev_agent_events.emit("da_1", 1, "fix", "fail", target_file=Hostile())
    assert event["job_id"] == "da_1"
    assert event["seq"] == 1
    assert event["phase"] == "fix"
    assert event["status"] == "fail"
    assert "error" in event and "boom" in event["error"]
    print("ok  emit survives a hostile __repr__")


def test_emit_still_handles_circular_and_plain_objects():
    """The two cases that already worked, kept honest.

    A plain object is NOT an error case — default=str serializes it — which
    is why the old test asserting an "error" key here was wrong about what it
    was testing.
    """
    class Plain:
        def __repr__(self):
            return "<Plain>"

    event = dev_agent_events.emit("da_2", 1, "fix", "ok", target_file=Plain())
    assert "error" not in event
    # emit() returns the event dict holding the ORIGINAL object; only the
    # line printed to stderr is serialized. The distinction matters because
    # the return value is what dev_agent appends to its `steps` list.
    assert isinstance(event["target_file"], Plain)

    loop = {}
    loop["self"] = loop
    event = dev_agent_events.emit("da_3", 1, "fix", "fail", loop=loop)
    assert "error" in event and "Circular" in event["error"]
    print("ok  emit handles plain + circular")


def test_emit_keyboard_interrupt_still_propagates():
    """Exception, not BaseException: a Ctrl+C during serialization must still
    reach the top. Swallowing KeyboardInterrupt would make the agent loop
    un-interruptible."""
    class Interrupting:
        def __repr__(self):
            raise KeyboardInterrupt

    try:
        dev_agent_events.emit("da_4", 1, "fix", "fail", x=Interrupting())
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt was swallowed")
    print("ok  KeyboardInterrupt still propagates")


if __name__ == "__main__":
    test_malformed_job_does_not_abort_the_tick()
    test_non_dict_trigger_does_not_kill_the_whole_scan()
    test_broken_job_does_not_refire_every_tick()
    test_daemon_survives_a_tick_that_raises()
    test_emit_survives_a_field_whose_repr_raises()
    test_emit_still_handles_circular_and_plain_objects()
    test_emit_keyboard_interrupt_still_propagates()
    print("\nall robustness regression tests passed")
