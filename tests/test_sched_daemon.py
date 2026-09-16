"""Tests for jarvis/sched_daemon.py — the standing tick-loop driver that
fixes the "NO DAEMON" gap in KNOWN-ISSUES-AND-GAPS.md.

Run: python3 ../tests/test_sched_daemon.py   (from jarvis-cli/, like the others)

Nothing here actually sleeps for the default 30s interval or backgrounds a
real OS process: `run(once=True)` exercises the startup tick + shutdown
path synchronously, and the loop itself is exercised with a 1s interval
in a background thread so the test suite stays fast.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis import scheduler, sched_daemon  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_sched_daemon_test_"))
    scheduler.JARVIS_DIR = tmp
    scheduler.STORE_FILE = tmp / "scheduled.json"
    scheduler.LOCK_FILE = tmp / "scheduled.lock"
    sched_daemon.PID_FILE = tmp / "sched_daemon.pid"
    return tmp


def test_status_when_nothing_running():
    tmp = fresh()
    try:
        s = sched_daemon.status()
        check("status reports not running when no pid file", s == {"running": False}, s)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_once_writes_and_clears_pid_file():
    tmp = fresh()
    try:
        rc = sched_daemon.run(once=True, quiet=True)
        check("run(once=True) returns 0", rc == 0, rc)
        check("pid file cleaned up after once-run", not sched_daemon.PID_FILE.exists())
        check("status is not-running again", sched_daemon.status() == {"running": False})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_DAEMON_SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, {cli_dir!r})
from jarvis import scheduler, sched_daemon
tmp = Path({tmp_str!r})
scheduler.JARVIS_DIR = tmp
scheduler.STORE_FILE = tmp / "scheduled.json"
scheduler.LOCK_FILE = tmp / "scheduled.lock"
sched_daemon.PID_FILE = tmp / "sched_daemon.pid"
sched_daemon.run(interval=1, quiet=True)
"""


def test_refuses_second_instance_while_running():
    # Real subprocess, not a thread: signal.signal only installs handlers
    # in the main thread of the main interpreter, and a real daemon is
    # always its own process — this is what actually exercises SIGTERM.
    tmp = fresh()
    proc = None
    try:
        script = _DAEMON_SCRIPT.format(cli_dir=str(CLI_DIR), tmp_str=str(tmp))
        proc = subprocess.Popen([sys.executable, "-c", script])

        for _ in range(100):
            if sched_daemon.status().get("running"):
                break
            time.sleep(0.05)

        st = sched_daemon.status()
        check("daemon reports running once started", st.get("running") is True, st)

        rc = sched_daemon.run(once=True, quiet=True)
        check("a second invocation refuses to start", rc == 1)

        stopped = sched_daemon.stop_running()
        check("stop_running() signals the live daemon", stopped is True)
        proc.wait(timeout=5)
        check("daemon process exits after stop signal", proc.returncode == 0, proc.returncode)
        check("pid file cleared after stop", not sched_daemon.PID_FILE.exists())
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def test_stop_running_with_nothing_running():
    tmp = fresh()
    try:
        check("stop_running() is False when nothing is running",
              sched_daemon.stop_running() is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in [v for k, v in list(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)
