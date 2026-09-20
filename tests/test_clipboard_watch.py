"""Tests for clipboard_watch.py and clipboard_cli.py: built-in daemon
registration, config round-trip (including bad-regex rejection), the
polling loop's change-detection and pattern-filtering, and the CLI dispatch
for `clipboard-watch-config`.

No real clipboard, daemon supervisor, or notification channel is touched —
`clipboard_tools._get` and `jarvis.notifier` are monkeypatched throughout.

    python3 tests/test_clipboard_watch.py

HOME is redirected before any jarvis import, per AGENTS.md — clipboard_watch's
config file and daemons.py's registry both live under ~/.jarvis.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

_TMP = tempfile.mkdtemp(prefix="jarvis-clipboard-watch-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
Path(_TMP, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis import clipboard_watch as cw  # noqa: E402
from jarvis import clipboard_tools as ct  # noqa: E402
from jarvis import clipboard_cli  # noqa: E402
from jarvis import daemons  # noqa: E402


# --- built-in daemon registration -------------------------------------------

def test_registered_as_builtin():
    entries = {e["id"]: e for e in daemons.list_daemons()}
    assert "clipboard-watch" in entries
    assert entries["clipboard-watch"]["builtin"] is True
    assert entries["clipboard-watch"]["enabled"] is True


def test_builtin_cannot_be_removed_or_have_argv_edited():
    # Same protection every other built-in gets — this is what makes it
    # safe for the model to daemon_start/daemon_stop this id without
    # granting it any ability to change what actually runs.
    ok, _ = daemons.remove("clipboard-watch")
    assert ok is False
    ok, _ = daemons.edit("clipboard-watch", argv=["echo", "not the real worker"])
    assert ok is False
    entries = {e["id"]: e for e in daemons.list_daemons()}
    assert entries["clipboard-watch"]["argv"] == [daemons.JARVIS_TOKEN, "clipboard-watch"]


def test_no_ai_tool_registers_or_edits_the_daemon():
    # Guards the actual design decision: clipboard_tools.py's registered
    # tools must not include a start/stop-with-argv style tool. The model
    # gets clipboard-watch only through the pre-existing, generic
    # daemon_start/daemon_stop tools (already covered by workspace tests).
    from jarvis import clipboard_tools as _ct
    assert "clipboard_watch_start" not in _ct.CLIPBOARD_TOOLS
    assert "clipboard_watch_stop" not in _ct.CLIPBOARD_TOOLS


# --- config -------------------------------------------------------------

def test_config_defaults():
    # Explicit reset first: this test asserts the *default* shape, and
    # must not depend on running before every other test that changes
    # config (tests run in sorted-name order, not declaration order).
    cw.save_config(pattern=None, poll_seconds=1.0)
    cfg = cw.load_config()
    assert cfg["pattern"] is None
    assert cfg["poll_seconds"] == 1.0


def test_set_pattern_round_trips_and_clears():
    cw.set_pattern(r"https?://\S+")
    assert cw.load_config()["pattern"] == r"https?://\S+"
    cw.set_pattern("")
    assert cw.load_config()["pattern"] is None
    cw.set_pattern(None)
    assert cw.load_config()["pattern"] is None


def test_set_pattern_rejects_bad_regex():
    cw.set_pattern("a known-good pattern")
    before = cw.load_config()
    raised = False
    try:
        cw.set_pattern("(unclosed")
    except Exception:
        raised = True
    assert raised, "an invalid regex must raise, not silently save"
    assert cw.load_config() == before, "a rejected pattern must not be persisted"


def test_save_config_updates_poll_seconds():
    cw.save_config(poll_seconds=5)
    assert cw.load_config()["poll_seconds"] == 5


# --- run() loop -----------------------------------------------------------

def _install_fake_notifier():
    notified = []
    def fake_notify(title, message, **kw):
        notified.append((title, message, kw))
        raise SystemExit(0)  # stop run()'s infinite loop after the first hit
    orig = cw.notifier
    cw.notifier = types.SimpleNamespace(notify=fake_notify)
    return notified, orig


def test_run_returns_error_code_when_backend_unavailable():
    orig_get = ct._get
    ct._get = lambda: (None, "no clipboard utility found")
    try:
        assert cw.run() == 1
    finally:
        ct._get = orig_get


def test_run_notifies_on_change_no_pattern():
    orig_get = ct._get
    notified, orig_notifier = _install_fake_notifier()
    seq = iter(["a", "a", "b", "b"])
    ct._get = lambda: (next(seq, "b"), None)
    cw.set_pattern(None)
    cw.save_config(poll_seconds=0.01)
    try:
        try:
            cw.run()
        except SystemExit:
            pass
        assert notified, "expected a notification on clipboard change"
        title, message, kw = notified[0]
        assert title == "Clipboard changed"
        assert message == "b"
        assert kw.get("kind") == "clipboard_watch"
    finally:
        ct._get = orig_get
        cw.notifier = orig_notifier


def test_run_skips_non_matching_changes():
    orig_get = ct._get
    notified, orig_notifier = _install_fake_notifier()
    # Two non-matching changes, then a matching one — the loop must not
    # fire (or exit) on the first two.
    seq = iter(["a", "no match here", "no match here", "still no", "still no", "has-URL http://x"])
    ct._get = lambda: (next(seq, "has-URL http://x"), None)
    cw.set_pattern(r"https?://")
    cw.save_config(poll_seconds=0.01)
    try:
        try:
            cw.run()
        except SystemExit:
            pass
        assert notified, "expected the eventual matching change to notify"
        assert "http://x" in notified[0][1]
    finally:
        ct._get = orig_get
        cw.notifier = orig_notifier


def test_run_truncates_long_clipboard_text_in_notification():
    orig_get = ct._get
    notified, orig_notifier = _install_fake_notifier()
    long_text = "y" * (ct.CLIPBOARD_MAX_CHARS + 200)
    seq = iter(["a", long_text, long_text])
    ct._get = lambda: (next(seq, long_text), None)
    cw.set_pattern(None)
    cw.save_config(poll_seconds=0.01)
    try:
        try:
            cw.run()
        except SystemExit:
            pass
        assert notified
        title, message, kw = notified[0]
        assert message.endswith(" …")
        assert len(message) <= ct.CLIPBOARD_MAX_CHARS + 2
    finally:
        ct._get = orig_get
        cw.notifier = orig_notifier


# --- CLI dispatch -----------------------------------------------------------

def test_cli_config_sets_pattern_and_poll_seconds(capsys=None):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        clipboard_cli.handle(["clipboard-watch-config", "--pattern", "foo.*bar", "--poll-seconds", "3"])
    assert "foo.*bar" in buf.getvalue()
    cfg = cw.load_config()
    assert cfg["pattern"] == "foo.*bar"
    assert cfg["poll_seconds"] == 3.0


def test_cli_config_clear_pattern():
    cw.set_pattern("something")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        clipboard_cli.handle(["clipboard-watch-config", "--clear-pattern"])
    assert cw.load_config()["pattern"] is None


def test_cli_config_rejects_bad_regex():
    import io, contextlib
    buf = io.StringIO()
    raised = False
    with contextlib.redirect_stdout(buf):
        try:
            clipboard_cli.handle(["clipboard-watch-config", "--pattern", "(unclosed"])
        except SystemExit:
            raised = True
    assert raised
    assert "invalid --pattern" in buf.getvalue() or "\"ok\": false" in buf.getvalue().lower()


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
