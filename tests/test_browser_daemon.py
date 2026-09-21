"""Tests for browser_daemon.py (master plan Part C, v2 warm daemon) and the
browser_tools.py client-side plumbing that talks to it.

    python3 tests/test_browser_daemon.py

No real Playwright/Chromium is used anywhere here: OPS' targets in
browser_daemon are monkeypatched to fake, deterministic functions, and the
real socket server IS exercised (bound to 127.0.0.1 on an OS-assigned free
port, same as production) since that costs nothing in a plain container and
is the actual thing worth testing — the wire protocol, not Playwright.

HOME is redirected to a temp dir before any jarvis import, per AGENTS.md.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

_TMP = tempfile.mkdtemp(prefix="jarvis-browser-daemon-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
Path(_TMP, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis import browser_daemon  # noqa: E402
from jarvis import browser_tools  # noqa: E402
from jarvis import daemons  # noqa: E402
from jarvis import ai_config  # noqa: E402


# --- built-in daemon registration -------------------------------------------

def test_registered_as_builtin():
    entries = {e["id"]: e for e in daemons.list_daemons()}
    assert "browser" in entries
    assert entries["browser"]["builtin"] is True
    assert entries["browser"]["enabled"] is True
    assert entries["browser"]["argv"] == [daemons.JARVIS_TOKEN, "browser-daemon"]


def test_builtin_cannot_be_removed_or_have_argv_edited():
    ok, _ = daemons.remove("browser")
    assert ok is False
    ok, _ = daemons.edit("browser", argv=["echo", "not the real worker"])
    assert ok is False
    entries = {e["id"]: e for e in daemons.list_daemons()}
    assert entries["browser"]["argv"] == [daemons.JARVIS_TOKEN, "browser-daemon"]


def test_no_ai_tool_registers_or_edits_the_browser_daemon():
    # Same invariant clipboard-watch is held to: no schema in
    # BROWSER_TOOL_SCHEMAS may let the model set an argv / register a
    # daemon of its own choosing. The daemon is only reachable through the
    # existing generic daemon_start/daemon_stop tools.
    names = {s["name"] for s in browser_tools.BROWSER_TOOL_SCHEMAS}
    assert not any("daemon" in n for n in names)


# --- helpers to run a real (but fake-backed) daemon in-process --------------

def _set_config(**defaults):
    ai_config.ensure_ai_config()
    raw = json.loads(ai_config.AI_CONFIG_FILE.read_text(encoding="utf-8"))
    d = raw.setdefault("defaults", {})
    d.update(defaults)
    from jarvis import atomic_io
    atomic_io.write_json(ai_config.AI_CONFIG_FILE, raw)


class _RunningDaemon:
    """Starts browser_daemon.run() on a background thread against fake
    OPS, and stops it cleanly on exit. One instance per test — module
    globals in browser_daemon (_stop, _last_activity) are reset here since
    the real module only expects one `run()` per process."""

    def __enter__(self):
        browser_daemon._stop.clear()
        browser_daemon._last_activity["t"] = time.monotonic()
        self._thread = threading.Thread(target=browser_daemon.run, daemon=True)
        self._thread.start()
        # Wait for the port file to appear rather than a fixed sleep — the
        # server binds and writes it before printing "running".
        for _ in range(200):
            if browser_daemon.PORT_FILE.exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("browser-daemon never wrote its port file")
        data = json.loads(browser_daemon.PORT_FILE.read_text(encoding="utf-8"))
        self.port = data["port"]
        return self

    def __exit__(self, *exc):
        browser_daemon._stop.set()
        self._thread.join(timeout=5)


def _raw_call(port, op, args=None):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8").splitlines()[0])


def test_daemon_serves_ping_and_writes_removes_port_file():
    with _RunningDaemon() as d:
        assert browser_daemon.PORT_FILE.exists()
        resp = _raw_call(d.port, "ping")
        assert resp == {"ok": True, "pong": True}
    assert not browser_daemon.PORT_FILE.exists()


def test_daemon_unknown_op_reported_cleanly():
    with _RunningDaemon() as d:
        resp = _raw_call(d.port, "not_a_real_op")
        assert resp["ok"] is False
        assert "unknown op" in resp["error"]


def test_daemon_bad_json_reported_cleanly_not_crashed():
    with _RunningDaemon() as d:
        with socket.create_connection(("127.0.0.1", d.port), timeout=2) as sock:
            sock.sendall(b"{not json\n")
            sock.shutdown(socket.SHUT_WR)
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        resp = json.loads(raw.decode("utf-8").splitlines()[0])
        assert resp["ok"] is False
        # Server must still be alive for the next call.
        assert _raw_call(d.port, "ping") == {"ok": True, "pong": True}


def test_daemon_dispatches_to_local_browser_tools_functions(monkeypatch):
    calls = []

    def fake_goto(args):
        calls.append(args)
        return {"ok": True, "url": args.get("url"), "title": "fake"}

    monkeypatch.setitem(browser_daemon.OPS, "goto", fake_goto)
    with _RunningDaemon() as d:
        resp = _raw_call(d.port, "goto", {"url": "example.com"})
    assert resp == {"ok": True, "url": "example.com", "title": "fake"}
    assert calls == [{"url": "example.com"}]


def test_daemon_op_exception_becomes_ok_false_not_a_crash(monkeypatch):
    def boom(args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(browser_daemon.OPS, "goto", boom)
    with _RunningDaemon() as d:
        resp = _raw_call(d.port, "goto", {"url": "x"})
        assert resp["ok"] is False
        assert "kaboom" in resp["error"]
        # Still alive afterwards.
        assert _raw_call(d.port, "ping")["ok"] is True


def test_idle_monitor_closes_browser_but_keeps_daemon_alive(monkeypatch):
    # Force a near-zero idle timeout and pretend a page is open, so the
    # monitor thread has something to close almost immediately.
    _set_config(browser_daemon_idle_seconds=30)  # will be clamped to >=30 anyway
    monkeypatch.setattr(browser_daemon, "_idle_seconds_setting", lambda: 0.01)
    closed = {"n": 0}
    monkeypatch.setattr(browser_tools, "close_browser", lambda: closed.__setitem__("n", closed["n"] + 1))
    monkeypatch.setitem(browser_tools._session, "page", object())
    try:
        with _RunningDaemon() as d:
            for _ in range(100):
                if closed["n"] > 0:
                    break
                time.sleep(0.05)
            assert closed["n"] >= 1
            # Daemon itself is still listening.
            assert _raw_call(d.port, "ping") == {"ok": True, "pong": True}
    finally:
        browser_tools._session["page"] = None


# --- browser_tools.py client-side: config flag + graceful fallback ---------

def test_warm_daemon_setting_defaults_off():
    _set_config()
    assert browser_tools._warm_daemon_setting() is False


def test_warm_daemon_setting_reads_config_flag():
    _set_config(browser_warm_daemon=True)
    assert browser_tools._warm_daemon_setting() is True
    _set_config(browser_warm_daemon=False)


def test_read_daemon_port_none_when_no_file():
    if browser_tools.DAEMON_PORT_FILE.exists():
        browser_tools.DAEMON_PORT_FILE.unlink()
    assert browser_tools._read_daemon_port() is None


def test_call_daemon_none_when_not_running():
    if browser_tools.DAEMON_PORT_FILE.exists():
        browser_tools.DAEMON_PORT_FILE.unlink()
    assert browser_tools._call_daemon("ping", {}) is None


def test_call_daemon_none_on_stale_port_file():
    from jarvis import atomic_io
    atomic_io.write_json(browser_tools.DAEMON_PORT_FILE, {"port": 1, "pid": 999999})
    # Port 1 is a privileged/likely-unbound port — connect should fail
    # fast and cleanly rather than raising.
    assert browser_tools._call_daemon("ping", {}) is None
    browser_tools.DAEMON_PORT_FILE.unlink()


def test_via_daemon_or_local_uses_local_when_flag_off():
    _set_config(browser_warm_daemon=False)
    sentinel = {"ok": True, "via": "local"}
    result = browser_tools._via_daemon_or_local("goto", {}, lambda args: sentinel)
    assert result is sentinel


def test_via_daemon_or_local_falls_back_when_daemon_unreachable():
    _set_config(browser_warm_daemon=True)
    if browser_tools.DAEMON_PORT_FILE.exists():
        browser_tools.DAEMON_PORT_FILE.unlink()
    sentinel = {"ok": True, "via": "local-fallback"}
    result = browser_tools._via_daemon_or_local("goto", {}, lambda args: sentinel)
    assert result is sentinel
    _set_config(browser_warm_daemon=False)


def test_via_daemon_or_local_uses_daemon_response_when_reachable(monkeypatch):
    _set_config(browser_warm_daemon=True)

    def fake_goto(args):
        return {"ok": True, "url": args.get("url"), "title": "fake"}

    monkeypatch.setitem(browser_daemon.OPS, "goto", fake_goto)
    with _RunningDaemon():
        result = browser_tools._via_daemon_or_local(
            "goto", {"url": "example.com"}, lambda args: {"ok": True, "via": "local"},
        )
    assert result == {"ok": True, "url": "example.com", "title": "fake"}
    _set_config(browser_warm_daemon=False)


def test_public_tool_browser_goto_routes_through_daemon_when_enabled(monkeypatch):
    _set_config(browser_warm_daemon=True)

    def fake_goto(args):
        return {"ok": True, "url": "https://example.com", "title": "fake-page"}

    monkeypatch.setitem(browser_daemon.OPS, "goto", fake_goto)
    with _RunningDaemon():
        result = browser_tools.tool_browser_goto({"url": "example.com"})
    assert result == {"ok": True, "url": "https://example.com", "title": "fake-page"}
    _set_config(browser_warm_daemon=False)


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                _MonkeyPatch().run(fn)
            else:
                fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


class _MonkeyPatch:
    """Minimal stand-in for pytest's monkeypatch fixture, since this suite
    (like its siblings) runs as a plain script, not under pytest."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((setattr, obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def setitem(self, mapping, key, value):
        had = key in mapping
        old = mapping.get(key)
        self._undo.append(("item", mapping, key, had, old))
        mapping[key] = value

    def run(self, fn):
        try:
            fn(self)
        finally:
            for entry in reversed(self._undo):
                if entry[0] is setattr:
                    _, obj, name, old = entry
                    setattr(obj, name, old)
                else:
                    _, mapping, key, had, old = entry
                    if had:
                        mapping[key] = old
                    else:
                        mapping.pop(key, None)


if __name__ == "__main__":
    sys.exit(_run())
