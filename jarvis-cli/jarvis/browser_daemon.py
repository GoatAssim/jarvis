"""`jarvis browser-daemon` — the v2 "always-warm" browser session from the
master plan's Part C (Browser control), stretch goal.

WHAT THIS IS
------------
browser_tools.py's v1 session is "process lifetime = one ask": every
`jarvis ask` that touches a browser_* tool pays Chromium's ~1-2s startup
cost once and closes it when the process exits. That's fine for a single
ask, but a person doing several browser-touching asks back to back (or a
task with several steps, each its own `jarvis ask` subprocess — see
task_runner.py) pays that cost every single time.

This module is a small, long-lived control process — a `daemons.py`
BUILTIN, same footing as `clipboard-watch` — that holds ONE persistent
Playwright session open across every ask, and exposes it to `jarvis`
subprocesses over a local loopback socket instead of each one opening its
own. It reuses browser_tools.py's actual tool logic (`_local_tool_browser_*`,
`_ensure_page`, `close_browser`) unchanged; this module is transport +
lifecycle, not a second implementation of "how do I click a button".

WHY A LOOPBACK TCP SOCKET, NOT A UNIX DOMAIN SOCKET
------------------------------------------------------
This codebase's primary platform is Windows (see daemons.py's own
"append-only queue file, not a FIFO" reasoning for the same conclusion
elsewhere), and AF_UNIX support on Windows is new enough and inconsistent
enough across Python/OS versions to not be worth the platform branch. A
socket bound to 127.0.0.1 on an OS-assigned free port, with the port
number written to a small file under ~/.jarvis/, works identically
everywhere and is exactly as local — nothing outside this machine can
reach it.

PROTOCOL
--------
Newline-delimited JSON, one request per connection (no keep-alive — the
client (browser_tools._call_daemon) opens a fresh connection per call,
which is simpler and cheap enough at "a few calls per ask" volume):

    request:  {"op": "goto", "args": {"url": "..."}}\n
    response: {"ok": true, "url": "...", "title": "..."}\n

`op` is one of goto/click/fill/get_text/screenshot/wait_for/close/ping,
mapping onto browser_tools' _local_tool_browser_* functions (see OPS
below) — the exact same result shapes those already return, so a caller
that gets a daemon response and one from the v1 local path back to back
cannot tell them apart.

IDLE TIMEOUT
------------
A background thread checks, every few seconds, how long it has been since
the last request that actually touched the browser. Once that exceeds
`browser_daemon_idle_seconds` (ai_config.json `defaults`, default 600s)
AND a session is currently open, it closes the browser (browser_tools.
close_browser()) — freeing the Chromium process's memory — but the daemon
itself keeps running and listening. The next request just reopens the
session, paying the same ~1-2s startup a v1 ask always pays; nothing
breaks, it only stops being "warm" until used again. This is deliberately
NOT "the daemon exits after being idle": exiting would hand the job of
noticing and restarting it to something else, and daemons.py's registry
default restart policy is "never" (see daemons.py's own reasoning), so an
idle-exiting daemon would just go quietly stopped and stay that way.

STOPPING
--------
`daemon-stop browser` (or `daemon_stop` the AI tool) sends SIGTERM/SIGINT
the same way it stops every other daemon (see daemons.py's STOP_SIGNALS);
this module's own signal handler — same pattern as sched_daemon.py's
_handle_stop — just sets a flag the main loop checks, then the shutdown
path closes the browser, stops the socket server, and removes the port
file so a stale one is never mistaken for "still running" (browser_tools.
_call_daemon()'s connect attempt would fail anyway once nothing is
listening on that port, but removing the file makes `jarvis
browser-daemon-status`-style debugging honest too).
"""

import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

from . import atomic_io
from . import browser_tools

JARVIS_DIR = Path.home() / ".jarvis"
PORT_FILE = browser_tools.DAEMON_PORT_FILE
ENCODING = "utf-8"

DEFAULT_IDLE_SECONDS = 600
_IDLE_CHECK_SECONDS = 5

_stop = threading.Event()
_last_activity = {"t": time.monotonic()}
_activity_lock = threading.Lock()

# Maps the wire protocol's `op` directly onto browser_tools' LOCAL
# implementations (not the public tool_browser_* wrappers) — this process
# IS the warm session, so it must never turn around and call back into
# itself over the socket, which is what calling the public wrappers would
# risk if browser_warm_daemon happens to be on for this process too.
OPS = {
    "goto": browser_tools._local_tool_browser_goto,
    "click": browser_tools._local_tool_browser_click,
    "fill": browser_tools._local_tool_browser_fill,
    "get_text": browser_tools._local_tool_browser_get_text,
    "screenshot": browser_tools._local_tool_browser_screenshot,
    "wait_for": browser_tools._local_tool_browser_wait_for,
    "close": browser_tools._local_tool_browser_close,
    "ping": lambda args: {"ok": True, "pong": True},
}


def _idle_seconds_setting():
    try:
        from . import ai_config
        cfg = ai_config.load_ai_config()
        val = float(cfg.get("defaults", {}).get("browser_daemon_idle_seconds", DEFAULT_IDLE_SECONDS))
    except Exception:
        val = DEFAULT_IDLE_SECONDS
    return max(30.0, min(val, 3600.0))


def _touch_activity():
    with _activity_lock:
        _last_activity["t"] = time.monotonic()


def _handle_stop(signum, frame):
    _stop.set()


class _Handler(socketserver.StreamRequestHandler):
    # Short per-connection timeouts: this is a local, low-volume control
    # channel, not a real network service — a slow/wedged client should
    # never be able to tie up a handler thread indefinitely.
    timeout = 60

    def handle(self):
        try:
            raw = self.rfile.readline()
        except OSError:
            return
        if not raw:
            return
        try:
            request = json.loads(raw.decode(ENCODING, errors="replace"))
        except ValueError:
            self._reply({"ok": False, "error": "bad request (not JSON)"})
            return
        if not isinstance(request, dict):
            self._reply({"ok": False, "error": "bad request (not an object)"})
            return
        op = request.get("op")
        fn = OPS.get(op)
        if fn is None:
            self._reply({"ok": False, "error": f"unknown op: {op!r}"})
            return
        _touch_activity()
        try:
            result = fn(request.get("args") or {})
        except Exception as e:
            result = {"ok": False, "error": f"browser-daemon internal error: {e}"}
        if not isinstance(result, dict):
            result = {"ok": False, "error": "internal error: non-dict tool result"}
        self._reply(result)

    def _reply(self, result):
        try:
            self.wfile.write((json.dumps(result) + "\n").encode(ENCODING))
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _write_port_file(port):
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_io.write_json(PORT_FILE, {"port": port, "pid": os.getpid(), "started": time.time()})


def _remove_port_file():
    try:
        PORT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _idle_monitor():
    while not _stop.is_set():
        _stop.wait(_IDLE_CHECK_SECONDS)
        if _stop.is_set():
            break
        with _activity_lock:
            idle_for = time.monotonic() - _last_activity["t"]
        if browser_tools._session.get("page") is None:
            continue
        if idle_for >= _idle_seconds_setting():
            print(f"browser-daemon: idle for {idle_for:.0f}s, closing browser", flush=True)
            browser_tools.close_browser()


def run():
    """Entry point for `jarvis browser-daemon`. Blocks until stopped;
    returns an exit code, matching every other `jarvis <x>-daemon` entry
    point (see clipboard_watch.run(), channels/instagram_gateway.py)."""
    try:
        # Real usage always calls run() as the main thread of its own
        # `jarvis browser-daemon` process, where this always succeeds.
        # Python only refuses signal.signal() off the main thread — which
        # only happens in this suite's tests, run against a background
        # thread for convenience — so degrade quietly there rather than
        # crashing the daemon loop.
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
    except ValueError:
        pass

    try:
        server = _Server(("127.0.0.1", 0), _Handler)
    except OSError as e:
        print(f"browser-daemon: could not bind a loopback port: {e}", flush=True)
        return 1

    port = server.server_address[1]
    _write_port_file(port)
    _touch_activity()

    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    server_thread.start()

    idle_thread = threading.Thread(target=_idle_monitor, daemon=True)
    idle_thread.start()

    print(f"browser-daemon: running on 127.0.0.1:{port}", flush=True)
    try:
        while not _stop.is_set():
            _stop.wait(0.5)
    finally:
        server.shutdown()
        server.server_close()
        browser_tools.close_browser()
        _remove_port_file()
        print("browser-daemon: stopped", flush=True)
    return 0
