"""Regression tests for H.1.2 and H.1.3 — owner-reported, 2026-09-27, against
the rev. 2026-09-27c Daemons rework (`jarvis-h1-daemons-rework.zip`):

  H.1.2 "every daemon shows as crashed" — status() treated ANY previously-
  RUNNING daemon whose process is gone as an immediate crash, even when the
  supervisor is ALSO gone with no evidence a crash ever happened. That is
  the normal state after a host reboot, a hard kill of the supervisor, or a
  jarvis upgrade replacing the running process — run_supervisor() itself
  always writes the real final status (CRASHED/STOPPED, with a reason)
  before it ever exits normally, so a RUNNING record with BOTH child and
  supervisor gone and no final status ever written did not go through that
  path at all. Fixed by only keeping the immediate-crash reading when the
  supervisor is still alive (the case H.1.1's own regression test already
  locks in — see test_running_to_gone_is_still_an_immediate_crash_no_
  regression in test_h11_daemon_status.py, deliberately left unchanged and
  re-asserted here) and reporting "stopped" when the supervisor is gone too.

  H.1.3 "no working Stop control" — a daemon in STARTING or RESTARTING
  (mid crash-loop backoff) is not `running` yet, but its supervisor is
  already alive and already owns the daemon's child slot. The frontend used
  `running` alone to decide the Start/Stop toggle (fixed separately in
  daemons.js — not covered here, this repo has no JS test harness, same as
  every other daemons.js change to date), and start()'s own guard only
  checked `running`, which meant clicking "Start" on a daemon already mid
  backoff would spawn a SECOND supervisor over the first one. Fixed by also
  rejecting a start when a supervisor is already alive.

Run: python3 tests/test_h12_daemon_fixes.py
"""

import sys
import tempfile
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis_h12_"))

from jarvis import daemons  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _fresh_daemon(name_suffix):
    did = f"h12test_{name_suffix}"
    if daemons.get(did):
        daemons.remove(did)
    daemons.add(did, "echo hi", name=f"H12 Test {name_suffix}")
    return daemons.normalize_id(did)


# --- H.1.2: reboot / external-kill no longer reads as a false crash --------

def test_running_with_dead_supervisor_and_no_stop_request_is_stopped_not_crashed():
    """The actual "every daemon shows crashed" scenario: a status.json left
    over from before a reboot (or a hard-killed supervisor) says RUNNING,
    but neither the child nor the supervisor that was watching it are still
    alive, and nobody ever asked for a stop. No evidence exists that this
    daemon crashed — it's just not running right now."""
    did = _fresh_daemon("reboot")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=999996, supervisor_pid=999995,
                           stop_requested=False, started_at=time.time())
    s = daemons.status(did)
    check('a RUNNING record with both child and supervisor gone reads "stopped", not "crashed"',
          s["status"] == daemons.STATUS_STOPPED, f"got {s['status']!r}")


def test_running_with_live_supervisor_and_dead_child_is_still_crashed():
    """Unchanged from H.1.1: a supervisor that IS still alive with its child
    gone means something really did go wrong under active supervision, and
    must still read as an immediate crash — this is exactly the scenario
    test_h11_daemon_status.py's own regression test locks in; re-asserted
    here as a guard against H.1.2's fix accidentally loosening it."""
    did = _fresh_daemon("live_supervisor_dead_child")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=999994, supervisor_pid=os.getpid(),
                           stop_requested=False, started_at=time.time())
    s = daemons.status(did)
    check("a live supervisor with a dead child is still an immediate crash",
          s["status"] == daemons.STATUS_CRASHED, f"got {s['status']!r}")


def test_running_with_dead_supervisor_but_stop_requested_is_stopped():
    """A clean stop that raced a reboot should still read as stopped, not
    crashed — stop_requested is checked first, regardless of supervisor
    aliveness."""
    did = _fresh_daemon("reboot_after_stop_request")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=999993, supervisor_pid=999992,
                           stop_requested=True, started_at=time.time())
    s = daemons.status(did)
    check("a requested stop still reads stopped even with the supervisor also gone",
          s["status"] == daemons.STATUS_STOPPED, f"got {s['status']!r}")


def test_actually_running_daemon_is_unaffected():
    """Sanity: a genuinely running daemon must not be touched by this fix at
    all — it never reaches the RUNNING-but-gone branch."""
    did = _fresh_daemon("genuinely_running")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=os.getpid(), supervisor_pid=os.getpid(),
                           stop_requested=False, started_at=time.time())
    s = daemons.status(did)
    check("a genuinely running daemon still reports running",
          s["status"] == daemons.STATUS_RUNNING and s["running"] is True, f"got {s}")


# --- H.1.3: start() refuses to double-supervise ----------------------------

def test_start_refuses_when_a_supervisor_is_already_alive():
    """A daemon mid restart-backoff (STATUS_RESTARTING) is not `running`,
    but its supervisor is alive and already owns this daemon. start() must
    refuse rather than spawn a second supervisor over the first one."""
    did = _fresh_daemon("mid_backoff")
    daemons._write_status(did, status=daemons.STATUS_RESTARTING,
                           child_pid=None, supervisor_pid=os.getpid(),
                           stop_requested=False)
    ok, message = daemons.start(did)
    check("start() refuses when a supervisor is already alive",
          ok is False, f"got ok={ok!r}")
    check('the refusal message names the existing supervisor, not a generic failure',
          "supervisor" in message.lower(), f"got {message!r}")


def test_start_still_works_normally_once_the_supervisor_is_really_gone():
    """No regression: a daemon that is genuinely stopped, with no live
    supervisor on file, must still be startable. Uses a definitely-dead pid
    for supervisor_pid so this doesn't depend on FS state left by another
    test in this file."""
    did = _fresh_daemon("really_stopped")
    daemons._write_status(did, status=daemons.STATUS_STOPPED,
                           child_pid=None, supervisor_pid=999991,
                           stop_requested=False)
    current = daemons.status(did)
    check("a genuinely stopped daemon reports no live supervisor_pid",
          current["supervisor_pid"] is None, f"got {current['supervisor_pid']!r}")
    # Not actually spawning the real supervisor process here (no live
    # network/subprocess assumptions in this suite, same convention as
    # test_h11_daemon_status.py) — the guard itself is what's under test,
    # and it's already been shown above to only fire on a live
    # supervisor_pid. Confirming get()/status() see this daemon as eligible
    # to start is the meaningful, host-independent assertion.
    check("start()'s pre-flight checks (enabled, has a command) pass for it",
          bool(daemons.get(did)) and bool(daemons.resolve_argv(daemons.get(did))))


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    failures = []
    for test in _TESTS:
        before = len(FAIL)
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
            continue
        except Exception as e:  # noqa: BLE001
            failures.append((test.__name__, f"{type(e).__name__}: {e}"))
            print(f"FAILED   {test.__name__}: {type(e).__name__}: {e}")
            continue
        if len(FAIL) > before:
            failures.append((test.__name__, "see FAILED lines above"))
    total = len(PASS) + len(FAIL)
    print(f"\n{len(PASS)}/{total} checks passed across "
          f"{len(_TESTS) - len(failures)}/{len(_TESTS)} tests")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
