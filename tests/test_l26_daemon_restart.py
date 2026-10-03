"""Regression tests for L.26 — owner-reported, 2026-10-02: the Daemons
"Restart" button (and `daemon-restart`, and the `daemon_restart` AI tool)
refused with "already has a supervisor running" and, on a running daemon,
left it STOPPED.

Root cause: stop() declared success as soon as the CHILD was gone, but the
supervisor outlives its child (it still joins the pump threads, writes the
exit line and the final status). restart() called start() immediately and
lost the race to the H.1.3 "a supervisor is already alive" guard. And a
daemon that was `starting` / `restarting` (crash-loop backoff) has no running
child, so restart() skipped stop() entirely and start() refused forever.

These tests use a REAL supervisor and a REAL child, and run every operation
as its own `python -m jarvis` process — exactly how the web server drives
them. (Calling daemons.start() in-process leaves the supervisor as an unreaped
zombie of the test process, which pid_alive() still counts as alive; that is
a harness artifact, not behaviour, so this file does not do it.)

Run: python3 tests/test_l26_daemon_restart.py
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

HOME = tempfile.mkdtemp(prefix="jarvis_l26_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import daemons  # noqa: E402  (after HOME is redirected)

# Set on os.environ itself (not just the copy handed to subprocesses): the
# in-process start() below spawns `python -m jarvis daemon-run` and that child
# must be able to import the package without being installed.
os.environ["PYTHONPATH"] = str(PKG) + os.pathsep + os.environ.get("PYTHONPATH", "")
ENV = dict(os.environ)


def cli(*args, timeout=60):
    """One `python -m jarvis ...` process; returns the parsed JSON (or text)."""
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], env=ENV,
                          cwd=str(PKG), capture_output=True, text=True,
                          timeout=timeout)
    out = proc.stdout.strip()
    try:
        return json.loads(out)
    except ValueError:
        return {"_raw": out, "_err": proc.stderr.strip()}


def status_of(did):
    # A fresh process, so no zombie/in-memory state can colour the answer.
    code = ("import json,sys;from jarvis import daemons;"
            f"print(json.dumps(daemons.status({did!r})))")
    proc = subprocess.run([sys.executable, "-c", code], env=ENV, cwd=str(PKG),
                          capture_output=True, text=True, timeout=30)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def restart_of(did):
    code = ("import json;from jarvis import daemons;"
            f"ok,msg=daemons.restart({did!r});"
            "print(json.dumps({'ok':ok,'message':msg}))")
    proc = subprocess.run([sys.executable, "-c", code], env=ENV, cwd=str(PKG),
                          capture_output=True, text=True, timeout=90)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def start_of(did):
    code = ("import json;from jarvis import daemons;"
            f"ok,msg=daemons.start({did!r});"
            "print(json.dumps({'ok':ok,'message':msg}))")
    proc = subprocess.run([sys.executable, "-c", code], env=ENV, cwd=str(PKG),
                          capture_output=True, text=True, timeout=30)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def stop_of(did):
    code = ("import json;from jarvis import daemons;"
            f"ok,msg=daemons.stop({did!r});"
            "print(json.dumps({'ok':ok,'message':msg}))")
    proc = subprocess.run([sys.executable, "-c", code], env=ENV, cwd=str(PKG),
                          capture_output=True, text=True, timeout=60)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def wait_for(pred, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    return pred()


def _read_supervisor_pid(did):
    try:
        return daemons._read_status(did).get("supervisor_pid")
    except Exception:  # noqa: BLE001
        return None


def _register(did, command, **kw):
    daemons.add(did, command, **kw)


def _cleanup(did):
    # A supervisor that ran as a thread of THIS process records our own pid;
    # a separate `stop` process would (correctly) treat that as a foreign
    # supervisor and kill us. Only stop through a subprocess when it isn't.
    if _read_supervisor_pid(did) != os.getpid():
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


SLEEPER = f'"{sys.executable}" -c "import time; time.sleep(600)"'
CRASHER = f'"{sys.executable}" -c "import sys; sys.exit(3)"'


def test_restart_on_running_daemon_ends_running_with_new_pid():
    did = "l26-running"
    _register(did, SLEEPER)
    try:
        assert start_of(did)["ok"]
        first = wait_for(lambda: (s := status_of(did))["running"] and s)
        assert first and first["running"], first
        old_pid, old_sup = first["pid"], first["supervisor_pid"]

        r = restart_of(did)
        assert r["ok"], r
        second = wait_for(lambda: (s := status_of(did))["running"] and
                          s["pid"] != old_pid and s)
        assert second and second["running"], second
        assert second["pid"] != old_pid, "child pid must change on restart"
        assert second["supervisor_pid"] != old_sup, "a NEW supervisor owns it"
        assert not daemons.pid_alive(old_sup), "old supervisor must be gone"
    finally:
        _cleanup(did)


def test_restart_twice_in_a_row_both_work():
    # The owner's report: it failed "always"; the repro failed every other
    # click. Two consecutive restarts must both succeed.
    did = "l26-twice"
    _register(did, SLEEPER)
    try:
        assert start_of(did)["ok"]
        assert wait_for(lambda: status_of(did)["running"])
        for n in (1, 2):
            r = restart_of(did)
            assert r["ok"], (n, r)
            assert wait_for(lambda: status_of(did)["running"]), (n, r)
    finally:
        _cleanup(did)


def test_restart_unsticks_a_crash_looping_daemon():
    # restart=always + an instantly-failing command: status cycles through
    # `restarting`, the supervisor is alive, the child is not running. Before
    # the fix restart() skipped stop() and start() refused every time.
    did = "l26-crashloop"
    _register(did, CRASHER, restart="always", restart_delay=30,
              max_restarts=0)
    try:
        assert start_of(did)["ok"]
        looping = wait_for(
            lambda: (s := status_of(did))["status"] == "restarting" and s,
            seconds=20)
        assert looping, status_of(did)
        assert looping["supervisor_pid"] and not looping["running"]

        r = restart_of(did)
        assert r["ok"], r
        # It was handed a fresh supervisor (the old one is gone) — it will of
        # course begin crash-looping again, but the restart itself worked.
        after = status_of(did)
        assert after["supervisor_pid"] != looping["supervisor_pid"], after
        assert not daemons.pid_alive(looping["supervisor_pid"])
    finally:
        _cleanup(did)


def test_restart_on_stopped_daemon_just_starts_it():
    did = "l26-stopped"
    _register(did, SLEEPER)
    try:
        r = restart_of(did)
        assert r["ok"], r
        assert wait_for(lambda: status_of(did)["running"])
    finally:
        _cleanup(did)


def test_stop_returns_only_after_supervisor_is_gone():
    did = "l26-stop"
    _register(did, SLEEPER)
    try:
        assert start_of(did)["ok"]
        up = wait_for(lambda: (s := status_of(did))["running"] and s)
        assert up
        sup = up["supervisor_pid"]
        r = stop_of(did)
        assert r["ok"], r
        # The moment stop() returns, nothing may still be alive — that gap
        # is exactly what restart() used to lose its race in.
        assert not daemons.pid_alive(sup), "supervisor outlived stop()"
        assert not status_of(did)["supervisor_pid"]
    finally:
        _cleanup(did)


def test_a_genuine_second_supervisor_is_still_refused():
    # H.1.3 stays: start() on a daemon that is already up must refuse.
    did = "l26-guard"
    _register(did, SLEEPER)
    try:
        assert start_of(did)["ok"]
        assert wait_for(lambda: status_of(did)["running"])
        r = start_of(did)
        assert not r["ok"], r
        assert "already" in r["message"], r
    finally:
        _cleanup(did)


def test_failed_start_after_stop_says_what_state_it_was_left_in():
    # (c) never silently leave it stopped: if start() refuses after the stop,
    # the message must say so. Simulated by disabling the daemon mid-restart
    # through the registry before restart() reaches start().
    did = "l26-msg"
    _register(did, SLEEPER)
    try:
        assert start_of(did)["ok"]
        assert wait_for(lambda: status_of(did)["running"])
        daemons.edit(did, enabled=False)
        r = restart_of(did)
        assert not r["ok"], r
        assert "stopped" in r["message"] and "could not start" in r["message"], r
    finally:
        daemons.edit(did, enabled=True)
        _cleanup(did)


def test_in_process_start_then_stop_does_not_stall_on_a_zombie_supervisor():
    # A long-lived caller (Discord gateway, web tool runner) that start()s a
    # daemon IN-PROCESS owns the supervisor as a child. When it exits it is a
    # zombie, which pid_alive() counts as alive; stop() must reap it rather
    # than wait out the whole stop timeout for a supervisor that is gone.
    if os.name == "nt":
        return
    did = "l26-zombie"
    _register(did, SLEEPER)
    try:
        ok, msg = daemons.start(did)
        assert ok, msg
        assert wait_for(lambda: daemons.status(did)["running"])
        t0 = time.time()
        ok, msg = daemons.stop(did, timeout=20)
        took = time.time() - t0
        assert ok, msg
        assert "force-stopped" not in msg, msg
        assert took < 8, f"stop() stalled {took:.1f}s on a zombie supervisor"
        # ...and restart() from the same long-lived process works too.
        ok, msg = daemons.start(did)
        assert ok, msg
        assert wait_for(lambda: daemons.status(did)["running"])
        ok, msg = daemons.restart(did)
        assert ok, msg
        assert wait_for(lambda: daemons.status(did)["running"])
    finally:
        _cleanup(did)


def test_a_supervisor_thread_in_this_process_is_never_signalled():
    # run_supervisor() in a thread records OUR pid as supervisor_pid. stop()
    # must neither wait for that pid to vanish nor fall back to killing it —
    # that would SIGTERM the caller itself (this was a real hang + exit 143 in
    # tests/test_workspace.py while L.26 was being written).
    import threading
    did = "l26-selfpid"
    _register(did, SLEEPER)
    thread = threading.Thread(target=daemons.run_supervisor, args=(did,), daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: daemons.status(did)["running"])
        assert daemons.status(did)["supervisor_pid"] == os.getpid()
        t0 = time.time()
        ok, msg = daemons.stop(did, timeout=8)
        assert ok, msg
        assert time.time() - t0 < 6
        thread.join(timeout=10)
        assert not thread.is_alive(), "supervisor thread should have exited"
    finally:
        _cleanup(did)


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
