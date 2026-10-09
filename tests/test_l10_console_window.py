"""L.10 - the daemon console window: switches, launcher, viewer.

Covers: the two switches (`console_window_auto`, `console_window_stop_on_close`)
defaulting off, being stored/edited on custom daemons AND built-ins, surviving
the registry round trip; `open_console_window()` (Windows-only refusal, the
CREATE_NEW_CONSOLE launch with no inherited stdio, "already open" instead of a
second window); auto-open on start() without ever failing the start; the
viewer's Follower (backlog, appended lines, half-written lines, rotation,
truncation, missing file, UTF-8 split across reads); the viewer loop's stdin
relay and stop-on-close; request_stop(); and the CLI flags + slash coverage
names. The real window (a console on a real desktop) can only be checked by
hand - see the manual checklist in the master plan entry for L.10.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-l10-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import daemons, daemon_viewer  # noqa: E402

REG = Path(HOME) / ".jarvis" / "daemons.json"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def reset():
    if REG.exists():
        REG.unlink()
    shutil.rmtree(Path(HOME) / ".jarvis" / "daemons", ignore_errors=True)


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "jarvis-cli"), HOME=HOME, USERPROFILE=HOME)
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True, env=env, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout, "err": proc.stderr}


class FakePopen:
    calls = []

    def __init__(self, argv, **kwargs):
        FakePopen.calls.append((argv, kwargs))
        self.pid = 4242


def test_switches():
    reset()
    ok, _ = daemons.add("web", "python3 -c pass")
    e = daemons.get("web")
    check("a new daemon has both switches off", ok and e["console_window_auto"] is False and e["console_window_stop_on_close"] is False)
    ok, _ = daemons.add("watch", "python3 -c pass", console_window_auto=True, console_window_stop_on_close=True)
    e = daemons.get("watch")
    check("add can turn both on", ok and e["console_window_auto"] is True and e["console_window_stop_on_close"] is True)
    ok, err = daemons.edit("web", console_window_auto=True)
    check("edit turns one on and leaves the other", ok and daemons.get("web")["console_window_auto"] is True and daemons.get("web")["console_window_stop_on_close"] is False)
    daemons.edit("web", console_window_auto="")
    check("edit coerces to a real boolean", daemons.get("web")["console_window_auto"] is False)

    # an entry saved before this existed has neither key
    data = json.loads(REG.read_text(encoding="utf-8"))
    data["daemons"]["old"] = {"id": "old", "name": "old", "argv": ["python3", "-c", "pass"], "enabled": True}
    REG.write_text(json.dumps(data), encoding="utf-8")
    old = daemons.get("old")
    check("a daemon saved before L.10 reads as both off", old["console_window_auto"] is False and old["console_window_stop_on_close"] is False)

    d = daemons.describe(daemons.get("watch"))
    check("describe() exposes the switches and whether a window is open",
          d["console_window_auto"] is True and d["console_window_stop_on_close"] is True and d["console_window_open"] is False)


def test_builtin():
    reset()
    check("a built-in has both off by default",
          daemons.get("scheduler")["console_window_auto"] is False)
    ok, err = daemons.edit("scheduler", console_window_auto=True, console_window_stop_on_close=True)
    check("a built-in may turn them on (they change how you watch it, not what runs)", ok)
    e = daemons.get("scheduler")
    check("...and they stick across a reload", e["console_window_auto"] is True and e["console_window_stop_on_close"] is True)
    ok, _ = daemons.edit("scheduler", argv=["x"])
    check("the lock on a built-in's command is unchanged", not ok)
    stored = json.loads(REG.read_text(encoding="utf-8"))["daemons"]["scheduler"]
    check("only the overrides are written for a built-in", "argv" not in stored and stored.get("console_window_auto") is True)


def test_open_window():
    reset()
    daemons.add("web", "python3 -c pass")
    real_on_windows, real_popen = daemons._on_windows, daemons.subprocess.Popen
    try:
        daemons._on_windows = lambda: False
        ok, msg = daemons.open_console_window("web")
        check("refused off Windows, with a reason", not ok and "Windows" in msg)
        ok, msg = daemons.open_console_window("nope")
        check("an unknown daemon is refused", not ok and "no daemon" in msg)

        daemons._on_windows = lambda: True
        FakePopen.calls = []
        daemons.subprocess.Popen = FakePopen
        ok, msg = daemons.open_console_window("web")
        check("on Windows it launches the viewer", ok and len(FakePopen.calls) == 1)
        argv, kwargs = FakePopen.calls[0]
        check("the viewer command is `daemon-viewer <id>`", argv[-2:] == ["daemon-viewer", "web"])
        check("it gets its own console window", kwargs.get("creationflags", 0) & daemons.CREATE_NEW_CONSOLE)
        check("no stdio is passed down (the web server's pipes must not leak into the window)",
              "stdin" not in kwargs and "stdout" not in kwargs and "stderr" not in kwargs and kwargs.get("close_fds") is True)

        # already open -> not a second window
        daemons.note_console_window("web", os.getpid() + 100000)
        daemons.pid_alive_real = daemons.pid_alive
        daemons.pid_alive = lambda pid: True
        FakePopen.calls = []
        ok, msg = daemons.open_console_window("web")
        check("an open window is reported, not duplicated", ok and "already open" in msg and not FakePopen.calls)
        check("describe() says a window is open", daemons.describe(daemons.get("web"))["console_window_open"] is True)
        daemons.pid_alive = daemons.pid_alive_real
        ok, msg = daemons.open_console_window("web")
        check("a recorded pid that is dead no longer counts", ok and len(FakePopen.calls) == 1)

        def boom(argv, **kw):
            raise OSError("no console")
        daemons.subprocess.Popen = boom
        daemons.note_console_window("web", None)
        ok, msg = daemons.open_console_window("web")
        check("a launch failure is reported", not ok and "no console" in msg)
    finally:
        daemons._on_windows, daemons.subprocess.Popen = real_on_windows, real_popen
        if hasattr(daemons, "pid_alive_real"):
            daemons.pid_alive = daemons.pid_alive_real


def test_auto_open_on_start():
    reset()
    daemons.add("quiet", "python3 -c pass")
    daemons.add("loud", "python3 -c pass", console_window_auto=True)
    opened = []
    real_open, real_popen = daemons.open_console_window, daemons.subprocess.Popen
    started = []

    class P:
        pid = 4000001   # not a live process
    try:
        daemons.subprocess.Popen = lambda argv, **kw: (started.append(argv), P())[1]
        daemons.open_console_window = lambda did: (opened.append(did), (True, "opening"))[1]
        ok, msg = daemons.start("quiet")
        check("start() with the switch off opens no window", ok and opened == [])
        ok, msg = daemons.start("loud")
        check("start() with the switch on opens its window", ok and opened == ["loud"])
        daemons.stop("loud", timeout=1)
        daemons.open_console_window = lambda did: (False, "the console window is Windows-only")
        ok, msg = daemons.start("loud")
        check("a window that cannot open never fails the start, and says so",
              ok and "console window not opened" in msg and "Windows-only" in msg)
    finally:
        daemons.open_console_window, daemons.subprocess.Popen = real_open, real_popen


def test_request_stop():
    reset()
    daemons.add("web", "python3 -c pass")
    ok, msg = daemons.request_stop("web")
    check("stopping something that is not running is a quiet success", ok and "not running" in msg)
    check("request_stop on an unknown id is refused", daemons.request_stop("nope")[0] is False)
    daemons._write_status("web", supervisor_pid=os.getpid() + 100000, status="running")
    real = daemons.pid_alive
    daemons.pid_alive = lambda pid: True
    try:
        ok, msg = daemons.request_stop("web")
        check("a live daemon gets the stop flag", ok and daemons.stop_flag_path("web").exists())
    finally:
        daemons.pid_alive = real


def test_follower():
    d = Path(tempfile.mkdtemp(prefix="jarvis-l10-f-"))
    log = d / "console.log"
    f = daemon_viewer.Follower(log)
    check("a missing log is an empty backlog, not an error", f.start() == [] and f.poll() == [])

    log.write_bytes(b"one\ntwo\nthree\n")
    f = daemon_viewer.Follower(log)
    check("the backlog is the last lines already written", f.start(2) == ["two", "three"])
    check("nothing new yet", f.poll() == [])
    with open(log, "ab") as fh:
        fh.write(b"four\nfi")
    check("only COMPLETE lines are returned", f.poll() == ["four"])
    with open(log, "ab") as fh:
        fh.write(b"ve\n")
    check("a half-written line arrives whole, once", f.poll() == ["five"])

    with open(log, "ab") as fh:
        fh.write("caf\u00e9 \u2713\n".encode("utf-8")[:4])
    got = f.poll()
    with open(log, "ab") as fh:
        fh.write("caf\u00e9 \u2713\n".encode("utf-8")[4:])
    got += f.poll()
    check("a multi-byte character split across two reads is not mangled", got == ["caf\u00e9 \u2713"])

    with open(log, "ab") as fh:
        fh.write(b"win\r\n")
    check("CRLF line endings are stripped", f.poll() == ["win"])

    # rotation: console.log -> console.log.1, new console.log
    os.replace(log, d / "console.log.1")
    log.write_bytes(b"after-rotation\n")
    check("a rotated log is followed from the top of the new file", f.poll() == ["after-rotation"])
    # truncation in place
    log.write_bytes(b"x\n")
    check("a truncated log is followed from the top", f.poll() == ["x"])
    # gone, then back
    log.unlink()
    check("a vanished log is quiet", f.poll() == [])
    log.write_bytes(b"back\n")
    check("...and picked up again when it reappears", f.poll() == ["back"])

    big = d / "big.log"
    big.write_bytes(b"".join(f"line {i}\n".encode() for i in range(5000)))
    f2 = daemon_viewer.Follower(big)
    lines = f2.start(50)
    check("a huge log shows only the requested backlog, starting on a whole line",
          len(lines) == 50 and lines[-1] == "line 4999" and lines[0] == "line 4950")
    shutil.rmtree(d, ignore_errors=True)


class Lines:
    """A stdin that hands out queued lines, then blocks like a console until
    `release()`, then reports end of input."""

    def __init__(self, lines):
        self.q = list(lines)
        self.gate = threading.Event()

    def readline(self):
        if self.q:
            return self.q.pop(0)
        self.gate.wait(5)
        return ""


def run_viewer_in_thread(did, inp):
    out = io.StringIO()
    result = {}

    def go():
        result["code"] = daemon_viewer.run_viewer(did, out=out, inp=inp, poll=0.05, install_close_handler=False)

    t = threading.Thread(target=go, daemon=True)   # the read-only viewer never ends on its own
    t.start()
    return t, out, result


def test_viewer_loop():
    reset()
    daemons.add("echo", "python3 -c pass", supports_stdin=True)
    daemons.add("mute", "python3 -c pass")
    sent = []
    real_send = daemons.send_input
    daemons.send_input = lambda did, text: (sent.append((did, text)), (True, "sent"))[1]
    try:
        log = daemons.console_path("echo")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"hello\n")
        inp = Lines(["ping\n"])
        t, out, result = run_viewer_in_thread("echo", inp)
        deadline = time.time() + 5
        while time.time() < deadline and not sent:
            time.sleep(0.05)
        with open(log, "ab") as fh:
            fh.write(b"live line\n")
        deadline = time.time() + 5
        while time.time() < deadline and "live line" not in out.getvalue():
            time.sleep(0.05)
        check("the viewer shows what is already there", "hello" in out.getvalue())
        check("...and follows new output", "live line" in out.getvalue())
        check("a typed line is relayed to the daemon's stdin", sent == [("echo", "ping")])
        check("it records itself so a second open finds it", daemons._read_status("echo").get("console_window_pid") == os.getpid())
        inp.gate.set()
        t.join(10)
        check("the viewer ends when its input closes", not t.is_alive() and result.get("code") == 0)
        check("...and clears its pid", daemons._read_status("echo").get("console_window_pid") is None)

        # read-only daemon: input is never read, never relayed
        sent.clear()
        inp = Lines(["should not be read\n"])
        t, out, result = run_viewer_in_thread("mute", inp)
        time.sleep(0.4)
        check("a service that takes no input is announced read-only and nothing is relayed",
              "Read-only" in out.getvalue() and sent == [] and inp.q == ["should not be read\n"])
        check("the read-only viewer stays open", t.is_alive())
        # There is no portable way to inject Ctrl+C into a thread; the
        # stop-on-close path is exercised through _finish() below instead.
    finally:
        daemons.send_input = real_send


def test_stop_on_close():
    reset()
    daemons.add("keep", "python3 -c pass")
    daemons.add("kill", "python3 -c pass", console_window_stop_on_close=True)
    stops = []
    real = daemons.request_stop
    daemons.request_stop = lambda did: (stops.append(did), (True, "stop requested"))[1]
    out = io.StringIO()
    try:
        daemons.note_console_window("keep", os.getpid())
        daemon_viewer._finish("keep", os.getpid(), False, "window closed", out)
        check("closing the viewer leaves a daemon running when stop-on-close is off", stops == [])
        check("...and clears the pid", daemons._read_status("keep").get("console_window_pid") is None)
        daemons.note_console_window("kill", os.getpid())
        daemon_viewer._finish("kill", os.getpid(), True, "window closed", out)
        check("closing it asks the daemon to stop when stop-on-close is on", stops == ["kill"])
        check("...and says so in the window", "stop requested" in out.getvalue())
        # someone else's pid is not ours to clear
        daemons.note_console_window("keep", os.getpid() + 1)
        daemon_viewer._finish("keep", os.getpid(), False, "x", out)
        check("another window's pid is left alone", daemons._read_status("keep").get("console_window_pid") == os.getpid() + 1)
    finally:
        daemons.request_stop = real


def test_viewer_refuses():
    reset()
    out = io.StringIO()
    check("an unknown daemon is refused", daemon_viewer.run_viewer("nope", out=out, inp=Lines([]), install_close_handler=False) == 1)
    daemons.add("web", "python3 -c pass")
    daemons.note_console_window("web", os.getpid() + 100000)
    real = daemons.pid_alive
    daemons.pid_alive = lambda pid: True
    try:
        out = io.StringIO()
        code = daemon_viewer.run_viewer("web", out=out, inp=Lines([]), install_close_handler=False)
        check("a second viewer for the same daemon is refused", code == 1 and "already open" in out.getvalue())
    finally:
        daemons.pid_alive = real


def test_cli():
    reset()
    code, out = cli("daemon-add", "w", "python3 -c pass", "--console-window-auto", "--console-window-stop-on-close")
    e = daemons.get("w")
    check("daemon-add --console-window-auto/--console-window-stop-on-close", code == 0 and e["console_window_auto"] and e["console_window_stop_on_close"])
    code, out = cli("daemon-edit", "w", "--console-window-auto", "false")
    e = daemons.get("w")
    check("daemon-edit turns one off", code == 0 and e["console_window_auto"] is False and e["console_window_stop_on_close"] is True)
    code, out = cli("daemon-edit", "w", "--console-window-stop-on-close", "false")
    check("daemon-edit turns the other off", code == 0 and daemons.get("w")["console_window_stop_on_close"] is False)
    code, out = cli("daemon-window", "w")
    check("daemon-window refuses off Windows with a JSON error", (os.name == "nt") or (code != 0 and out.get("ok") is False and "Windows" in out.get("message", "")))
    code, out = cli("daemon-window")
    check("daemon-window needs an id", code != 0)
    code, out = cli("daemons")
    row = [d for d in out["daemons"] if d["id"] == "w"][0]
    check("`jarvis daemons` carries the switches and the open flag", "console_window_auto" in row and row.get("console_window_open") is False)


def test_names_and_wiring():
    from jarvis import reserved_names, workspace_cli
    for name in ("daemon-window", "daemon-viewer", "daemons-sync"):
        check(f"{name} is a workspace command", name in workspace_cli.COMMANDS)
        check(f"{name} is a reserved name", name in reserved_names.RESERVED_NAMES if hasattr(reserved_names, "RESERVED_NAMES") else True)
    data = (ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
    check("the slash data covers daemon-window and lists the viewer as not exposed",
          '"daemon-window"' in data and '"daemon-viewer": {' in data)
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("server.js has a `window` action that maps to daemon-window",
          'new Set(["start", "stop", "restart", "status", "window"])' in server)
    check("server.js forwards both switches on add and edit",
          "--console-window-auto" in server and "--console-window-stop-on-close" in server and "consoleWindowAuto" in server)


try:
    test_switches()
    test_builtin()
    test_open_window()
    test_auto_open_on_start()
    test_request_stop()
    test_follower()
    test_viewer_loop()
    test_stop_on_close()
    test_viewer_refuses()
    test_cli()
    test_names_and_wiring()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
