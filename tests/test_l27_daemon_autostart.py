"""Regression tests for L.27 — owner-reported, 2026-10-02: the Daemons panel's
Autostart switch was stored and shown, and nothing ever started an autostart
daemon. `daemons.autostart_all()` existed and had no caller.

The fix is one call, on the scheduler's STARTUP tick (scheduler.tick(
startup=True)) — the moment every long-lived driver already treats as "Jarvis
just started": the web server (`sched-tick --startup` on boot), `jarvis
sched-daemon`, and `jarvis sched-tick --startup` from Task Scheduler/cron.

These tests go through the REAL start path and not through autostart_all()
called by hand: each one runs `python -m jarvis sched-tick --startup` (or
`sched-daemon --once`) as its own process — exactly how web/server.js drives
it — against a REAL supervisor and a REAL child, then reads status from a
fresh process. (Calling daemons.start() in-process would leave the supervisor
as an unreaped zombie of the test process, which pid_alive() counts as alive;
see tests/test_l26_daemon_restart.py. That is a harness artifact, so this file
never does it.)

Run: python3 tests/test_l27_daemon_autostart.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "jarvis-cli"
sys.path.insert(0, str(PKG))

HOME = tempfile.mkdtemp(prefix="jarvis_l27_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")
os.environ["PYTHONPATH"] = str(PKG) + os.pathsep + os.environ.get("PYTHONPATH", "")

from jarvis import daemons  # noqa: E402  (after HOME is redirected)

ENV = dict(os.environ)

SLEEPER = f'"{sys.executable}" -c "import time; time.sleep(600)"'
CRASHER = f'"{sys.executable}" -c "import sys; sys.exit(3)"'


def run_py(code, timeout=60):
    proc = subprocess.run([sys.executable, "-c", code], env=ENV, cwd=str(PKG),
                          capture_output=True, text=True, timeout=timeout)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def cli(*args, timeout=90):
    """One `python -m jarvis ...` process; returns (parsed-json-or-None, proc)."""
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], env=ENV,
                          cwd=str(PKG), capture_output=True, text=True,
                          timeout=timeout)
    out = proc.stdout.strip()
    try:
        return json.loads(out), proc
    except ValueError:
        return None, proc


def startup_tick():
    """What the web server runs on boot."""
    result, proc = cli("sched-tick", "--startup")
    assert result is not None, (proc.stdout, proc.stderr)
    return result


def plain_tick():
    result, proc = cli("sched-tick")
    assert result is not None, (proc.stdout, proc.stderr)
    return result


def status_of(did):
    # A fresh process, so no zombie/in-memory state can colour the answer.
    return run_py("import json;from jarvis import daemons;"
                  f"print(json.dumps(daemons.status({did!r})))")


def stop_of(did):
    return run_py("import json;from jarvis import daemons;"
                  f"ok,msg=daemons.stop({did!r});"
                  "print(json.dumps({'ok':ok,'message':msg}))", timeout=90)


def wait_for(pred, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    return pred()


def _register(did, command, **kw):
    daemons.add(did, command, **kw)


def _cleanup(*dids):
    for did in dids:
        try:
            stop_of(did)
        except Exception:  # noqa: BLE001
            pass
        s = status_of(did)
        for pid in (s.get("pid"), s.get("supervisor_pid")):
            if pid and pid != os.getpid() and daemons.pid_alive(pid):
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass


def _autostarted(result, did):
    return [r for r in (result.get("daemons_autostarted") or []) if r["id"] == did]


# --- the bug --------------------------------------------------------------

def test_startup_tick_starts_an_autostart_daemon():
    did = "l27-basic"
    _register(did, SLEEPER, autostart=True)
    try:
        assert not status_of(did)["running"], "precondition: it starts down"
        result = startup_tick()
        entries = _autostarted(result, did)
        assert entries and entries[0]["ok"], result
        s = wait_for(lambda: (x := status_of(did))["running"] and x)
        assert s and s["running"], f"autostart daemon never came up: {status_of(did)}"
        assert s["supervisor_pid"] and s["pid"]
    finally:
        _cleanup(did)


def test_sched_daemon_startup_tick_also_autostarts():
    # The second long-lived driver. `--once` runs exactly its startup tick.
    did = "l27-sched-daemon"
    _register(did, SLEEPER, autostart=True)
    try:
        _, proc = cli("sched-daemon", "--once", "--quiet")
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        s = wait_for(lambda: (x := status_of(did))["running"] and x)
        assert s and s["running"], status_of(did)
    finally:
        _cleanup(did)


# --- what must NOT start --------------------------------------------------

def test_non_autostart_and_disabled_daemons_stay_down():
    plain, disabled = "l27-plain", "l27-disabled"
    _register(plain, SLEEPER)                    # autostart off
    _register(disabled, SLEEPER, autostart=True)
    daemons.edit(disabled, enabled=False)
    try:
        result = startup_tick()
        assert not _autostarted(result, plain), result
        assert not _autostarted(result, disabled), result
        time.sleep(1.0)
        assert not status_of(plain)["running"]
        assert not status_of(disabled)["running"]
    finally:
        _cleanup(plain, disabled)


def test_a_plain_tick_never_autostarts():
    # Autostart is a STARTUP behaviour. On the 30 s tick it would undo every
    # manual Stop within half a minute.
    did = "l27-plain-tick"
    _register(did, SLEEPER, autostart=True)
    try:
        result = plain_tick()
        assert not _autostarted(result, did), result
        assert "daemons_autostarted" in result and result["daemons_autostarted"] == []
        time.sleep(1.0)
        assert not status_of(did)["running"]
    finally:
        _cleanup(did)


def test_a_daemon_stopped_by_hand_stays_stopped_across_plain_ticks():
    did = "l27-stopped-by-hand"
    _register(did, SLEEPER, autostart=True)
    try:
        startup_tick()
        assert wait_for(lambda: status_of(did)["running"])
        ok = stop_of(did)
        assert ok["ok"], ok
        assert not status_of(did)["running"]
        for _ in range(3):
            plain_tick()
        time.sleep(1.0)
        assert not status_of(did)["running"], "a manual Stop must stick"
    finally:
        _cleanup(did)


# --- safe to call twice ---------------------------------------------------

def test_second_startup_tick_leaves_a_running_daemon_alone():
    did = "l27-twice"
    _register(did, SLEEPER, autostart=True)
    try:
        startup_tick()
        first = wait_for(lambda: (x := status_of(did))["running"] and x)
        assert first, status_of(did)

        result = startup_tick()
        assert not _autostarted(result, did), \
            f"a running daemon must not even be attempted: {result}"
        second = status_of(did)
        assert second["running"]
        assert second["pid"] == first["pid"], "child must not be replaced"
        assert second["supervisor_pid"] == first["supervisor_pid"], \
            "no second supervisor may appear"
    finally:
        _cleanup(did)


def test_startup_tick_does_not_fight_a_crash_looping_supervisor():
    # restart=always + an instantly failing command leaves the supervisor
    # alive in `restarting` with no running child. start() would refuse that
    # with the H.1.3 guard; autostart must treat it as "already being brought
    # up", not as a failure, and must not spawn a second supervisor (L.26).
    did = "l27-crashloop"
    _register(did, CRASHER, autostart=True, restart="always",
              restart_delay=30, max_restarts=0)
    try:
        startup_tick()
        looping = wait_for(
            lambda: (x := status_of(did))["status"] == "restarting" and x,
            seconds=20)
        assert looping and looping["supervisor_pid"], status_of(did)

        result = startup_tick()
        assert not _autostarted(result, did), result
        after = status_of(did)
        assert after["supervisor_pid"] == looping["supervisor_pid"], \
            "the live supervisor must be left alone"
        assert "autostart failed" not in (after.get("last_error") or ""), after
    finally:
        _cleanup(did)


# --- failures are visible -------------------------------------------------

def test_a_failed_autostart_is_reported_and_recorded_on_the_daemon():
    did = "l27-nocommand"
    _register(did, SLEEPER, autostart=True)
    # Blank the stored command, as a hand-edited registry could.
    registry = daemons._load_registry()
    registry[did]["argv"] = []
    daemons._save_registry(registry)
    try:
        result = startup_tick()
        entries = _autostarted(result, did)
        assert entries and entries[0]["ok"] is False, result
        assert "no command" in entries[0]["message"], entries
        s = status_of(did)
        assert not s["running"]
        assert "autostart failed" in (s.get("last_error") or ""), \
            f"the panel must be able to show why: {s}"
    finally:
        _cleanup(did)


def test_one_bad_daemon_does_not_stop_the_others_starting():
    bad, good = "l27-aaa-bad", "l27-zzz-good"
    _register(bad, SLEEPER, autostart=True)
    _register(good, SLEEPER, autostart=True)
    registry = daemons._load_registry()
    registry[bad]["argv"] = []
    daemons._save_registry(registry)
    try:
        result = startup_tick()
        assert _autostarted(result, bad)[0]["ok"] is False, result
        assert _autostarted(result, good)[0]["ok"] is True, result
        assert wait_for(lambda: status_of(good)["running"])
    finally:
        _cleanup(bad, good)


# --- the tick still does its real job ------------------------------------

def test_startup_tick_result_keeps_its_existing_shape():
    result = startup_tick()
    for key in ("ok", "ran", "notifications", "daemons_started", "at"):
        assert key in result, (key, result)
    assert result["ok"] is True
    assert isinstance(result["daemons_autostarted"], list)


# --- runner (keep BELOW every test: it reads globals() when it executes) ----

if __name__ == "__main__":
    failed = 0
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    for n in names:
        try:
            globals()[n]()
            print(f"  ok   {n}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL {n}: {exc!r}")
            traceback.print_exc()
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
