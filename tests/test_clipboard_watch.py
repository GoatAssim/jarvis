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

def _install_fake_notifier(stop_after=1):
    """Install a fake in place of `jarvis.notifier`. Records every notify()
    call and raises SystemExit once `stop_after` calls have been captured,
    breaking run()'s infinite loop at a caller-chosen point instead of
    always after the very first call — lets a test observe a short burst
    of calls (K.2.7.2) instead of only ever the first one.

    Also exposes normalize_level(), delegating to the real implementation.
    run() calls `notifier.normalize_level()` directly (to decide
    push-vs-buffer *before* notify() is ever reached), so a fake that only
    stubs notify() breaks with an AttributeError the instant a matching
    change comes in — this is exactly what broke 3 tests here when K.2.7.2
    added that call (see the master plan's K.2.7 for the regression this
    fixes). normalize_level()'s own lookup logic already has dedicated
    coverage elsewhere (K.2.7.1, test_notification_levels.py), so this
    delegates rather than re-implementing it — this fixture's job is only
    to intercept delivery, not to re-test level resolution.
    """
    notified = []
    def fake_notify(title, message, **kw):
        notified.append((title, message, kw))
        if len(notified) >= stop_after:
            raise SystemExit(0)
    orig = cw.notifier
    cw.notifier = types.SimpleNamespace(notify=fake_notify, normalize_level=orig.normalize_level)
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


def test_run_coalesces_rapid_burst_into_bounded_live_pushes_with_full_inbox_record():
    # K.2.7.2: a rapid run of matching changes must not toast once per
    # change (flood) but must still keep a durable inbox record of every
    # individual change — "no lost events" (see clipboard_watch.py's
    # _burst_decision docstring / the master plan's K.8 acceptance
    # criterion). coalesce_seconds is set far longer than this test's real
    # wall-clock runtime so the burst can never age into a flush, keeping
    # the outcome independent of scheduling jitter between test runs.
    orig_get = ct._get
    notified, orig_notifier = _install_fake_notifier(stop_after=6)
    seq = iter(["a", "b", "c", "d", "e", "f", "g"])
    ct._get = lambda: (next(seq, "g"), None)
    cw.set_pattern(None)
    cw.save_config(poll_seconds=0.01, coalesce_seconds=5.0)
    try:
        try:
            cw.run()
        except SystemExit:
            pass
        assert len(notified) == 6, \
            "expected exactly 6 notify() calls: 1 live push + 5 buffered inbox records"
        first_title, first_message, first_kw = notified[0]
        assert first_message == "b"
        assert first_kw.get("channels") is None, \
            "the first, isolated change must still push live (no burst yet in progress)"
        buffered = notified[1:]
        assert [m for (_, m, _) in buffered] == ["c", "d", "e", "f", "g"], \
            "every individual buffered change must keep its own durable inbox record — none lost"
        assert all(kw.get("channels") == ["inbox"] for (_, _, kw) in buffered), \
            "changes during an active burst must be inbox-only, not re-toasted one per change"
    finally:
        ct._get = orig_get
        cw.notifier = orig_notifier
        cw.save_config(coalesce_seconds=3.0)


# --- K.2.7.2 burst-coalescing pure functions --------------------------------
# Dedicated coverage for the decision functions clipboard_watch.py's own
# docstring says were split out specifically so they're testable with a
# synthetic clock instead of run()'s real time.sleep loop. Previously these
# had zero tests of their own — only ever exercised indirectly through
# run() at real-clock speed (see the master plan's K.2.7.3).

def test_burst_decision_level_3_and_up_always_pushes():
    # D.2.1's "never suppressed" guarantee for levels 3+ — even mid-burst,
    # with a push a moment ago.
    assert cw._burst_decision(3, 100.0, 99.99, {"count": 1}, 3.0) == "push"
    assert cw._burst_decision(5, 100.0, 100.0, None, 3.0) == "push"


def test_burst_decision_pushes_isolated_change_after_idle_period():
    # No burst in progress, and the last live push was outside the
    # coalescing window — a lone change should still arrive promptly.
    assert cw._burst_decision(2, 100.0, 90.0, None, 3.0) == "push"


def test_burst_decision_buffers_when_burst_already_in_progress():
    assert cw._burst_decision(2, 100.0, 90.0, {"count": 1}, 3.0) == "buffer"


def test_burst_decision_buffers_when_last_push_too_recent():
    assert cw._burst_decision(2, 100.0, 99.0, None, 3.0) == "buffer"


def test_buffer_change_starts_new_buffer():
    pending = cw._buffer_change(None, "hello", False, 100.0)
    assert pending == {"count": 1, "preview": "hello", "truncated": False, "since": 100.0}


def test_buffer_change_accumulates_count_and_keeps_original_since():
    pending = cw._buffer_change(None, "first", False, 100.0)
    pending = cw._buffer_change(pending, "second", True, 100.5)
    assert pending["count"] == 2
    assert pending["preview"] == "second"  # latest preview wins
    assert pending["truncated"] is True
    assert pending["since"] == 100.0, "the burst's start time must not reset while it's still active"


def test_maybe_flush_none_when_nothing_pending():
    assert cw._maybe_flush(None, 100.0, 3.0) is None


def test_maybe_flush_none_before_window_elapses():
    pending = {"count": 1, "preview": "x", "truncated": False, "since": 100.0}
    assert cw._maybe_flush(pending, 101.0, 3.0) is None


def test_maybe_flush_returns_pending_once_window_elapses():
    pending = {"count": 2, "preview": "x", "truncated": False, "since": 100.0}
    assert cw._maybe_flush(pending, 103.0, 3.0) == pending


def test_flush_title_and_message_singular():
    title, message = cw._flush_title_and_message({"count": 1, "preview": "hi", "truncated": False})
    assert title == "Clipboard changed"
    assert message == "hi"


def test_flush_title_and_message_plural_and_truncated():
    title, message = cw._flush_title_and_message({"count": 4, "preview": "hi", "truncated": True})
    assert title == "Clipboard changed (4\u00d7)"
    assert message == "4 changes \u2014 latest: hi \u2026"


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


# --- D7: model-callable pattern get/set (clipboard_tools.py) ----------------
# The model can already start/stop/inspect this daemon for free via
# daemon_start/daemon_stop/daemon_status — no new tool needed for that half.
# These two are the one part of D7 that needed new code: letting the model
# read/change what the watcher filters ON.

def test_tool_get_pattern_reflects_config():
    cw.set_pattern(None)
    assert ct.tool_clipboard_watch_get_pattern()["pattern"] is None
    cw.set_pattern("invoice")
    assert ct.tool_clipboard_watch_get_pattern()["pattern"] == "invoice"


def test_tool_set_pattern_round_trips():
    result = ct.tool_clipboard_watch_set_pattern({"pattern": r"\d{3}-\d{4}"})
    assert result["ok"] is True
    assert result["pattern"] == r"\d{3}-\d{4}"
    assert cw.load_config()["pattern"] == r"\d{3}-\d{4}"


def test_tool_set_pattern_empty_clears():
    cw.set_pattern("something")
    result = ct.tool_clipboard_watch_set_pattern({"pattern": ""})
    assert result["ok"] is True
    assert result["pattern"] is None
    assert cw.load_config()["pattern"] is None


def test_tool_set_pattern_omitted_also_clears():
    cw.set_pattern("something")
    result = ct.tool_clipboard_watch_set_pattern({})
    assert result["ok"] is True
    assert cw.load_config()["pattern"] is None


def test_tool_set_pattern_rejects_bad_regex_without_raising():
    # A tool call must never let a re.error escape into the caller — the
    # model just gets ok:False back, same as any other tool failure.
    cw.set_pattern("keep-me")
    result = ct.tool_clipboard_watch_set_pattern({"pattern": "(unclosed"})
    assert result["ok"] is False
    assert "error" in result
    # And the bad attempt must not have clobbered the last-good pattern.
    assert cw.load_config()["pattern"] == "keep-me"


def test_tool_set_pattern_rejects_non_string():
    result = ct.tool_clipboard_watch_set_pattern({"pattern": 12345})
    assert result["ok"] is False


def test_tool_set_pattern_no_confirm_required():
    # D7 option A: model-callable with no confirmation gate, same footing
    # as daemon_start/daemon_stop already have for this daemon — a regex
    # string can only ever be matched against, never executed.
    from jarvis import tool_safety
    assert tool_safety.requires_confirmation("clipboard_watch_set_pattern") is False


def test_tool_registered_in_clipboard_group_and_schemas():
    from jarvis import tool_registry
    from jarvis import tools as system_tools
    assert "clipboard_watch_set_pattern" in tool_registry.TOOL_GROUPS["clipboard"]
    assert "clipboard_watch_get_pattern" in tool_registry.TOOL_GROUPS["clipboard"]
    assert "clipboard_watch_set_pattern" in system_tools.TOOLS
    schema_names = {s["name"] for s in ct.CLIPBOARD_TOOL_SCHEMAS}
    assert {"clipboard_watch_get_pattern", "clipboard_watch_set_pattern"} <= schema_names


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
