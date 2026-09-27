"""Regression tests for H.1.1 — the Daemons panel bug (owner-reported,
2026-09-25): a daemon shows as "crashed" regardless of its real state, and
starting one opens a visible empty cmd window.

Two independent root causes, confirmed by reading daemons.py directly:

  False crash status — status() treated ANY gap between start() writing
  STATUS_STARTING and the supervisor later writing STATUS_RUNNING with a
  real child_pid as an immediate crash. That gap exists on every single
  start (start() returns "as soon as the supervisor is spawned... it does
  not wait for the child to be healthy" — its own docstring), so this was a
  100%-reproducible false positive, not a flaky one. Fixed with a bounded
  grace window for STATUS_STARTING specifically, while a supervisor that
  has already exited is still caught immediately (no grace period needed
  when we already know for certain something failed).

  Empty cmd window — start()'s Windows creationflags combined
  CREATE_NO_WINDOW with DETACHED_PROCESS. The two express contradictory
  intent (no console at all, vs. a hidden one) and is a documented Windows
  gotcha for producing exactly the reported symptom. Fixed by using
  CREATE_NO_WINDOW alone, matching what run_supervisor()'s own child spawn
  already did correctly (and was never reported as showing a window).

No real subprocess spawning, no actual Windows required — the creationflags
fix is verified through _detached_creationflags(is_windows=...), a small
pure function factored out specifically so this is testable from any host.

Run: python3 tests/test_h11_daemon_status.py
"""

import sys
import tempfile
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis_h11_"))

from jarvis import daemons, atomic_io  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _fresh_daemon(name_suffix):
    did = f"h11test_{name_suffix}"
    if daemons.get(did):
        daemons.remove(did)
    daemons.add(did, "echo hi", name=f"H11 Test {name_suffix}")
    return daemons.normalize_id(did)


# --- False crash status -----------------------------------------------------

def test_starting_with_live_supervisor_is_not_a_false_crash():
    did = _fresh_daemon("live")
    daemons._write_status(did, status=daemons.STATUS_STARTING,
                           supervisor_pid=os.getpid(),  # our own pid: guaranteed alive
                           stop_requested=False, last_error="")
    s = daemons.status(did)
    check("a daemon mid-start, supervisor alive, reads as STARTING (not crashed)",
          s["status"] == daemons.STATUS_STARTING, f"got {s['status']!r}")
    check("running flag is correctly False during the starting gap",
          s["running"] is False)


def test_starting_with_dead_supervisor_is_a_real_crash():
    did = _fresh_daemon("dead_supervisor")
    # A pid essentially guaranteed not to exist.
    daemons._write_status(did, status=daemons.STATUS_STARTING,
                           supervisor_pid=999999, stop_requested=False)
    s = daemons.status(did)
    check("a supervisor that already exited is detected as crashed immediately",
          s["status"] == daemons.STATUS_CRASHED, f"got {s['status']!r}")


def test_starting_stuck_past_grace_window_eventually_reports_crashed():
    did = _fresh_daemon("stuck")
    daemons._write_status(did, status=daemons.STATUS_STARTING,
                           supervisor_pid=os.getpid(), stop_requested=False)
    rec = daemons._read_status(did)
    rec["updated"] = time.time() - daemons.STARTING_GRACE_SECONDS - 1
    atomic_io.write_json(daemons.status_path(did), rec)
    s = daemons.status(did)
    check("a supervisor alive but silent past the grace window is eventually crashed",
          s["status"] == daemons.STATUS_CRASHED, f"got {s['status']!r}")


def test_starting_within_grace_window_is_still_starting():
    did = _fresh_daemon("within_grace")
    daemons._write_status(did, status=daemons.STATUS_STARTING,
                           supervisor_pid=os.getpid(), stop_requested=False)
    rec = daemons._read_status(did)
    rec["updated"] = time.time() - (daemons.STARTING_GRACE_SECONDS - 2)
    atomic_io.write_json(daemons.status_path(did), rec)
    s = daemons.status(did)
    check("still inside the grace window reads as STARTING, not crashed",
          s["status"] == daemons.STATUS_STARTING, f"got {s['status']!r}")


def test_running_to_gone_is_still_an_immediate_crash_no_regression():
    """The grace period must be scoped to STATUS_STARTING only — a daemon
    that WAS genuinely running and has since disappeared must still be
    reported as crashed immediately, with no new delay introduced."""
    did = _fresh_daemon("was_running")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=999998, supervisor_pid=os.getpid(),
                           stop_requested=False, started_at=time.time())
    s = daemons.status(did)
    check("a running daemon whose child pid is gone is still an immediate crash",
          s["status"] == daemons.STATUS_CRASHED, f"got {s['status']!r}")


def test_running_to_gone_after_stop_request_is_stopped_not_crashed():
    did = _fresh_daemon("clean_stop")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=999997, supervisor_pid=os.getpid(),
                           stop_requested=True, started_at=time.time())
    s = daemons.status(did)
    check("a clean, requested stop is reported as stopped, not crashed",
          s["status"] == daemons.STATUS_STOPPED, f"got {s['status']!r}")


def test_actually_running_daemon_reads_as_running():
    """The core acceptance criterion: a start that succeeds shows
    'running,' not 'crashed.'"""
    did = _fresh_daemon("actually_running")
    daemons._write_status(did, status=daemons.STATUS_RUNNING,
                           child_pid=os.getpid(),  # alive: it's this test process
                           supervisor_pid=os.getpid(), stop_requested=False,
                           started_at=time.time())
    s = daemons.status(did)
    check("a genuinely running daemon (live child pid) reports running",
          s["status"] == daemons.STATUS_RUNNING and s["running"] is True,
          f"got {s}")


# --- Empty cmd window --------------------------------------------------------

def test_windows_creationflags_never_combine_detached_with_no_window():
    # CREATE_NO_WINDOW/DETACHED_PROCESS are both 0 on a non-Windows host
    # (getattr(subprocess, ..., 0) fallback), which would make this
    # assertion trivially pass for the wrong reason there. Patch in
    # distinct nonzero sentinel bits for the duration of this check so the
    # bitwise-OR comparison is meaningful on any host, Windows or not.
    original_no_window = daemons.CREATE_NO_WINDOW
    original_detached = daemons.DETACHED_PROCESS
    daemons.CREATE_NO_WINDOW = 0x08000000
    daemons.DETACHED_PROCESS = 0x00000008
    try:
        flags = daemons._detached_creationflags(is_windows=True)
        check("Windows creationflags is CREATE_NO_WINDOW alone",
              flags == daemons.CREATE_NO_WINDOW, f"got {flags!r}")
        check("Windows creationflags does NOT also OR in DETACHED_PROCESS",
              flags != (daemons.CREATE_NO_WINDOW | daemons.DETACHED_PROCESS),
              f"got {flags!r}")
    finally:
        daemons.CREATE_NO_WINDOW = original_no_window
        daemons.DETACHED_PROCESS = original_detached


def test_non_windows_creationflags_is_a_no_op():
    check("non-Windows creationflags is 0 (no-op)",
          daemons._detached_creationflags(is_windows=False) == 0)


def test_start_uses_the_shared_creationflags_helper():
    """A source-level guard: start()'s own Windows branch must call the
    single shared helper rather than recomputing (and potentially
    re-combining) the flags inline — that inline recomputation is exactly
    how the DETACHED_PROCESS regression happened in the first place."""
    import inspect
    src = inspect.getsource(daemons.start)
    check("start() computes Windows creationflags via _detached_creationflags(...)",
          "_detached_creationflags(" in src)
    check("start() does not inline CREATE_NO_WINDOW | DETACHED_PROCESS",
          "CREATE_NO_WINDOW | DETACHED_PROCESS" not in src
          and "DETACHED_PROCESS | CREATE_NO_WINDOW" not in src)


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
