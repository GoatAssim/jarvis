"""Tests for clipboard_tools.py: per-tool result shaping, truncation, the
Linux multi-binary fallback (FileNotFoundError vs a real failure vs no
display server at all), and clipboard_wait_for_change's poll loop.

No real clipboard is touched — every case monkeypatches either the
OS-dispatch functions (`_get`/`_set`) or `subprocess.run` directly, mirroring
how radio_tools.py's own PowerShell calls would be tested.

    python3 tests/test_clipboard_tools.py

HOME is redirected before any jarvis import, per AGENTS.md, even though
this module itself doesn't touch ~/.jarvis — tools.py's auto-discovery
(imported transitively via jarvis.tools) does resolve paths at import time.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

_TMP = tempfile.mkdtemp(prefix="jarvis-clipboard-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
Path(_TMP, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis import clipboard_tools as ct  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402
from jarvis import tool_registry as tr  # noqa: E402


# --- registration -----------------------------------------------------------

def test_registered_in_tools_and_schemas():
    for name in ("clipboard_get", "clipboard_set", "clipboard_clear", "clipboard_wait_for_change"):
        assert name in system_tools.TOOLS, name
    schema_names = {s["name"] for s in system_tools.TOOL_SCHEMAS}
    for name in ("clipboard_get", "clipboard_set", "clipboard_clear", "clipboard_wait_for_change"):
        assert name in schema_names, name


def test_registered_in_a_group_with_no_orphans():
    assert tr.group_of("clipboard_get") == "clipboard"
    assert tr.group_of("clipboard_set") == "clipboard"
    assert tr.group_of("clipboard_clear") == "clipboard"
    assert tr.group_of("clipboard_wait_for_change") == "clipboard"
    # The consistency check the rest of the suite relies on
    # (test_registry_every_tool_is_grouped) — verified locally too so this
    # file fails loudly on its own if clipboard tools are ever ungrouped.
    ungrouped = tr._ungrouped_tool_names()
    assert "clipboard_get" not in ungrouped
    assert "clipboard_set" not in ungrouped
    assert "clipboard_clear" not in ungrouped
    assert "clipboard_wait_for_change" not in ungrouped


# --- tool_clipboard_get / set / clear (monkeypatched backend) --------------

def _patch_backend(monkeypatch_get=None, monkeypatch_set=None):
    orig_get, orig_set = ct._get, ct._set
    if monkeypatch_get is not None:
        ct._get = monkeypatch_get
    if monkeypatch_set is not None:
        ct._set = monkeypatch_set
    return orig_get, orig_set


def test_clipboard_get_happy_path():
    orig_get, orig_set = _patch_backend(monkeypatch_get=lambda: ("hello", None))
    try:
        assert ct.tool_clipboard_get() == {"ok": True, "text": "hello", "truncated": False}
    finally:
        ct._get, ct._set = orig_get, orig_set


def test_clipboard_get_error_shape():
    orig_get, orig_set = _patch_backend(monkeypatch_get=lambda: (None, "boom"))
    try:
        assert ct.tool_clipboard_get() == {"ok": False, "error": "boom"}
    finally:
        ct._get, ct._set = orig_get, orig_set


def test_clipboard_get_truncates_long_text():
    long_text = "x" * (ct.CLIPBOARD_MAX_CHARS + 500)
    orig_get, orig_set = _patch_backend(monkeypatch_get=lambda: (long_text, None))
    try:
        result = ct.tool_clipboard_get()
        assert result["ok"] is True
        assert result["truncated"] is True
        assert len(result["text"]) == ct.CLIPBOARD_MAX_CHARS
    finally:
        ct._get, ct._set = orig_get, orig_set


def test_clipboard_set_round_trips_and_requires_text():
    store = {}
    def fake_set(text):
        store["v"] = text
        return True, None
    orig_get, orig_set = _patch_backend(monkeypatch_set=fake_set)
    try:
        assert ct.tool_clipboard_set({"text": "world"}) == {"ok": True}
        assert store["v"] == "world"
        assert ct.tool_clipboard_set({}) == {"ok": False, "error": "text is required."}
        assert ct.tool_clipboard_set(None) == {"ok": False, "error": "text is required."}
    finally:
        ct._get, ct._set = orig_get, orig_set


def test_clipboard_clear_sets_empty_string():
    store = {"v": "not empty"}
    def fake_set(text):
        store["v"] = text
        return True, None
    orig_get, orig_set = _patch_backend(monkeypatch_set=fake_set)
    try:
        assert ct.tool_clipboard_clear() == {"ok": True}
        assert store["v"] == ""
    finally:
        ct._get, ct._set = orig_get, orig_set


def test_clipboard_set_propagates_backend_error():
    orig_get, orig_set = _patch_backend(monkeypatch_set=lambda text: (False, "no backend"))
    try:
        assert ct.tool_clipboard_set({"text": "x"}) == {"ok": False, "error": "no backend"}
    finally:
        ct._get, ct._set = orig_get, orig_set


# --- clipboard_wait_for_change ----------------------------------------------

def test_wait_for_change_detects_new_value():
    seq = iter(["a", "a", "a", "b"])
    orig_get, orig_set = _patch_backend(monkeypatch_get=lambda: (next(seq), None))
    orig_sleep = ct.time.sleep
    ct.time.sleep = lambda s: None
    try:
        result = ct.tool_clipboard_wait_for_change({"timeout_seconds": 5})
        assert result == {"ok": True, "text": "b", "truncated": False}
    finally:
        ct._get, ct._set = orig_get, orig_set
        ct.time.sleep = orig_sleep


def test_wait_for_change_times_out():
    orig_get, orig_set = _patch_backend(monkeypatch_get=lambda: ("same", None))
    orig_sleep = ct.time.sleep
    orig_monotonic = ct.time.monotonic
    ct.time.sleep = lambda s: None
    # First call establishes the deadline in the past-relative sense; every
    # call after that reports "already past deadline" so the loop body
    # never actually runs and the timeout path is exercised deterministically.
    calls = {"n": 0}
    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 10_000.0
    ct.time.monotonic = fake_monotonic
    try:
        result = ct.tool_clipboard_wait_for_change({"timeout_seconds": 1})
        assert result == {"ok": False, "timed_out": True}
    finally:
        ct._get, ct._set = orig_get, orig_set
        ct.time.sleep = orig_sleep
        ct.time.monotonic = orig_monotonic


def test_wait_for_change_clamps_timeout():
    # Not a behavioral assert on timing (too slow/flaky for a unit test) —
    # just confirms bad/huge input doesn't raise and doesn't silently accept
    # an unbounded wait. Each sub-case gets its own ever-advancing fake
    # clock (jumping straight past any plausible deadline on its second
    # read) so a wrong clamp shows up as a hang, not a false pass.
    orig_get, orig_set = _patch_backend(monkeypatch_get=lambda: ("x", None))
    orig_sleep = ct.time.sleep
    orig_monotonic = ct.time.monotonic
    ct.time.sleep = lambda s: None

    def make_fake_monotonic():
        calls = {"n": 0}
        def fake_monotonic():
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else 1_000_000.0
        return fake_monotonic

    try:
        ct.time.monotonic = make_fake_monotonic()
        result = ct.tool_clipboard_wait_for_change({"timeout_seconds": 99999})
        assert result == {"ok": False, "timed_out": True}

        ct.time.monotonic = make_fake_monotonic()
        result2 = ct.tool_clipboard_wait_for_change({"timeout_seconds": "not a number"})
        assert result2 == {"ok": False, "timed_out": True}
    finally:
        ct._get, ct._set = orig_get, orig_set
        ct.time.sleep = orig_sleep
        ct.time.monotonic = orig_monotonic


# --- Linux multi-binary fallback (subprocess.run monkeypatched directly) ---

def test_linux_get_falls_through_missing_binaries():
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] in ("wl-paste", "xclip"):
            raise FileNotFoundError(argv[0])
        class Result:
            returncode = 0
            stdout = "from-xsel"
            stderr = ""
        return Result()
    orig_run = ct.subprocess.run
    orig_display = os.environ.get("DISPLAY")
    ct.subprocess.run = fake_run
    os.environ["DISPLAY"] = ":0"
    try:
        text, err = ct._get_linux()
        assert err is None, err
        assert text == "from-xsel"
        assert calls == ["wl-paste", "xclip", "xsel"], calls
    finally:
        ct.subprocess.run = orig_run
        if orig_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = orig_display


def test_linux_get_no_display_server_short_circuits():
    orig_display = os.environ.pop("DISPLAY", None)
    orig_wayland = os.environ.pop("WAYLAND_DISPLAY", None)
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        raise AssertionError("should never shell out with no display server")
    orig_run = ct.subprocess.run
    ct.subprocess.run = fake_run
    try:
        text, err = ct._get_linux()
        assert text is None
        assert "display server" in err
        assert calls == [], "should short-circuit before trying any binary"
    finally:
        ct.subprocess.run = orig_run
        if orig_display is not None:
            os.environ["DISPLAY"] = orig_display
        if orig_wayland is not None:
            os.environ["WAYLAND_DISPLAY"] = orig_wayland


def test_linux_get_all_binaries_missing_gives_actionable_error():
    orig_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = ":0"
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    orig_run = ct.subprocess.run
    ct.subprocess.run = fake_run
    try:
        text, err = ct._get_linux()
        assert text is None
        assert "No clipboard utility found" in err
        assert "xclip" in err  # names the fix, not just "failed"
    finally:
        ct.subprocess.run = orig_run
        if orig_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = orig_display


def test_linux_get_real_error_surfaces_not_swallowed():
    # A binary that exists but fails for a real reason (not "not installed")
    # must not be silently treated the same as FileNotFoundError.
    orig_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = ":0"
    def fake_run(argv, **kwargs):
        if argv[0] == "wl-paste":
            raise FileNotFoundError(argv[0])
        class Result:
            returncode = 1
            stdout = ""
            stderr = "clipboard is empty"
        return Result()
    orig_run = ct.subprocess.run
    ct.subprocess.run = fake_run
    try:
        text, err = ct._get_linux()
        assert text is None
        assert "clipboard is empty" in err
        assert "xclip failed" in err
    finally:
        ct.subprocess.run = orig_run
        if orig_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = orig_display


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
