"""`jarvis doctor` must understand both pid-file formats the daemons write.

sched_daemon.py writes its pid file as JSON ({"pid": N, "started": ...});
channels/discord_gateway.py writes a bare integer. doctor.check_daemons() and
_pid_alive_from() only ever parsed the bare integer, so a healthy scheduler
daemon always showed up as

    [warn] scheduler daemon - pid file is unreadable
           fix: Delete the sched_daemon.pid file and start it again.

(advice that, followed, deletes the live daemon's single-instance guard), and
a running scheduler with an overdue job was reported as "nothing is ticking".

Run: python3 tests/test_doctor_daemon_pid_formats.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# HOME first: jarvis modules resolve Path.home() at import time.
os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["USERPROFILE"] = os.environ["HOME"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import doctor  # noqa: E402


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _daemon_checks(files):
    """check_daemons() against a temp ~/.jarvis holding `files` ({name: text})."""
    d = Path(tempfile.mkdtemp(prefix="jarvis-dir-"))
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    saved = doctor.JARVIS_DIR
    doctor.JARVIS_DIR = d
    try:
        return {c.id: c for c in doctor.check_daemons()}, d
    finally:
        doctor.JARVIS_DIR = saved


def test_json_pid_file_of_a_live_scheduler_is_running_not_unreadable():
    body = json.dumps({"pid": os.getpid(), "started": "2026-10-04T09:00:00"})
    checks, _ = _daemon_checks({"sched_daemon.pid": body})
    c = checks["daemon.scheduler"]
    assert c.status == doctor.OK and str(os.getpid()) in c.detail, (c.status, c.detail)


def test_bare_int_pid_file_still_works():
    checks, _ = _daemon_checks({"discord_daemon.pid": str(os.getpid())})
    c = checks["daemon.discord"]
    assert c.status == doctor.OK, (c.status, c.detail)


def test_dead_pid_is_still_stale_in_either_format():
    dead = _dead_pid()
    checks, _ = _daemon_checks({
        "sched_daemon.pid": json.dumps({"pid": dead, "started": "x"}),
        "discord_daemon.pid": str(dead),
    })
    for key in ("daemon.scheduler", "daemon.discord"):
        assert checks[key].status == doctor.FAIL and str(dead) in checks[key].detail, (key, checks[key].detail)


def test_genuinely_unreadable_pid_file_still_warns():
    for junk in ("", "not a pid", "{}", '{"pid": "abc"}', "[1, 2]", "null"):
        checks, _ = _daemon_checks({"sched_daemon.pid": junk})
        c = checks["daemon.scheduler"]
        assert c.status == doctor.WARN and "unreadable" in c.detail, (junk, c.status, c.detail)


def test_pid_alive_from_reads_json_too():
    d = Path(tempfile.mkdtemp(prefix="jarvis-dir-"))
    f = d / "sched_daemon.pid"
    f.write_text(json.dumps({"pid": os.getpid(), "started": "x"}), encoding="utf-8")
    assert doctor._pid_alive_from(f) is True
    f.write_text(json.dumps({"pid": _dead_pid(), "started": "x"}), encoding="utf-8")
    assert doctor._pid_alive_from(f) is False
    f.write_text("garbage", encoding="utf-8")
    assert doctor._pid_alive_from(f) is False


# --- runner (keep BELOW every test: it reads globals() when it executes) -----
if __name__ == "__main__":
    failed = 0
    names = sorted(n for n in globals() if n.startswith("test_"))
    for name in names:
        try:
            globals()[name]()
            print("ok      %s" % name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAILED  %s: %r" % (name, exc))
    print("%d passed, %d failed" % (len(names) - failed, failed))
    sys.exit(1 if failed else 0)
