"""A real standing daemon for the scheduler.

Fixes the "NO DAEMON" gap described in scheduler.py: until now the only
drivers of scheduler.tick() were (a) the web server's setInterval, which
only exists while a browser tab/server process happens to be running,
(b) OS-level Task Scheduler/cron, which the user has to configure by hand
outside Jarvis entirely, and (c) `jarvis ask`, which only drains
notifications rather than firing jobs. Someone who just wants scheduled
tasks to fire, without leaving the web UI open and without hand-editing
Task Scheduler/crontab, had no first-class option.

This module is that option: `jarvis sched-daemon` is a long-running
foreground process (backgrounding is the caller's job — nohup/systemd/a
Windows service wrapper/etc., same as any other unix-style daemon) that
calls scheduler.tick() on a fixed interval until it's told to stop.

It deliberately does NOT replace the existing drivers:
  * web/server.js's interval still runs while the web UI is open — two
    tickers is fine, scheduler._claim_lock() already makes concurrent
    ticks a no-op (see scheduler.py's module docstring).
  * `jarvis sched-tick` (single-shot) is unchanged, still what Task
    Scheduler/cron should point at for people who prefer that route.
  * `jarvis ask`'s notification drain is unchanged.

Only one sched-daemon should run per machine (that's the whole point —
one long-lived process instead of an external scheduler). A PID file
enforces that: a second `jarvis sched-daemon` invocation refuses to start
if a live one is already running, rather than silently doubling tick
frequency.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import scheduler

PID_FILE = scheduler.JARVIS_DIR / "sched_daemon.pid"
DEFAULT_INTERVAL = 30  # seconds — matches web/server.js's TICK_INTERVAL_MS default

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _read_pid_file():
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        return int(data.get("pid"))
    except Exception:
        return None


def _write_pid_file():
    scheduler.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(
        json.dumps({"pid": os.getpid(), "started": datetime.now().isoformat()}),
        encoding="utf-8",
    )


def _clear_pid_file(expected_pid):
    # Only clear if it's still ours — never blow away a newer daemon's
    # pid file just because our shutdown path happened to run late.
    if _read_pid_file() == expected_pid:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def status():
    """Is a daemon already running, and since when? Used by `--status`
    and by anything else (CLI, web panel) that wants to show whether a
    standing daemon is watching before telling the user their reminder
    is safe."""
    pid = _read_pid_file()
    if pid is None:
        return {"running": False}
    if not _pid_alive(pid):
        return {"running": False, "stale_pid": pid}
    try:
        started = json.loads(PID_FILE.read_text(encoding="utf-8")).get("started")
    except Exception:
        started = None
    return {"running": True, "pid": pid, "started": started}


def stop_running():
    """Ask a running daemon (if any) to shut down. Returns True if a
    signal was sent, False if nothing was running."""
    pid = _read_pid_file()
    if pid is None or not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def run(interval=DEFAULT_INTERVAL, once=False, quiet=False):
    """Block, ticking every `interval` seconds, until SIGTERM/SIGINT.

    `once=True` runs a single startup tick and returns — used by tests and
    by anyone who wants "just fire what's due right now, then exit"
    without the sleep loop (that's still plain `jarvis sched-tick`, this
    flag exists so the daemon's own startup pass is exercised the same
    way the loop uses it).
    """
    existing = status()
    if existing.get("running"):
        if not quiet:
            print(json.dumps({
                "error": "a sched-daemon is already running",
                "pid": existing["pid"], "started": existing.get("started"),
            }, indent=2))
        return 1

    _write_pid_file()
    try:
        # Only installable in the main thread of the main interpreter —
        # true for every real invocation (`jarvis sched-daemon` is its own
        # process). Guarded so embedding this in an unusual host doesn't
        # crash the daemon outright; it just loses graceful-shutdown.
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
    except ValueError:
        pass

    if not quiet:
        print(json.dumps({
            "ok": True, "daemon_started": True, "pid": os.getpid(),
            "interval_seconds": interval,
        }, indent=2))
        sys.stdout.flush()

    try:
        result = scheduler.tick(startup=True)
        if not quiet and result.get("ran"):
            print(json.dumps({"tick": result}, indent=2, default=str))
            sys.stdout.flush()

        if once:
            return 0

        while not _stop:
            # Sleep in short slices rather than one long time.sleep(interval)
            # so a SIGTERM/SIGINT during the wait is honoured within ~1s
            # instead of up to `interval` seconds.
            slept = 0
            while slept < interval and not _stop:
                time.sleep(min(1, interval - slept))
                slept += 1
            if _stop:
                break
            result = scheduler.tick()
            if not quiet and result.get("ran"):
                print(json.dumps({"tick": result}, indent=2, default=str))
                sys.stdout.flush()
        return 0
    finally:
        _clear_pid_file(os.getpid())
        if not quiet:
            print(json.dumps({"ok": True, "daemon_stopped": True}, indent=2))
