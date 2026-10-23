"""L.27 follow-up (C5) -- a daemon that fails to autostart now raises ONE notification.

Before: `daemons.autostart_all()` recorded `last_error` on the daemon and nothing else, so
the only way to learn that a daemon stayed down after startup was to open the Daemons
panel. Now one `daemon` notification (inbox + the level's live channels) is raised per
autostart pass, however many daemons failed.

Covers:
  * the text builder: one / several / more than the line cap / name equal to id;
  * autostart_all() with a failing start(): exactly one notify(), kind `daemon`,
    failed=True, naming every failed daemon, and the return value keeps its shape;
  * no failure -> no notification; an already-running daemon is not a failure;
  * a notifier that raises must not change what autostart_all() returns;
  * the REAL notifier: the record lands in the inbox, the default level for kind `daemon`
    is 2, and a level of 1 in notify_config.json keeps it inbox-only;
  * Settings > Notifications has a "Daemon problems" row (static read of settings.js).

No network, no live model, no real supervisor (start() is replaced), no sleeping.

Run: python3 tests/test_l27c_autostart_notice.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_l27c_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import daemons, notifier, tool_daemons  # noqa: E402  (after HOME is redirected)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class _Calls:
    """Replaces notifier.notify and records every call."""

    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def __call__(self, title, message, **kw):
        self.calls.append({"title": title, "message": message, **kw})
        if self.raises:
            raise RuntimeError("notifier is broken")
        return {"delivered": ["inbox"]}


def _register(did, name=""):
    ok, msg = daemons.add(did, [sys.executable, "-c", "pass"], name=name, autostart=True)
    assert ok, msg


def _reset_registry():
    for did in list(daemons._load_registry()):
        if did not in daemons.BUILTINS:
            daemons.remove(did)


class _Patched:
    """Context manager: swap daemons.start and notifier.notify, always restore."""

    def __init__(self, start, notify):
        self.start, self.notify = start, notify

    def __enter__(self):
        self._old = (daemons.start, notifier.notify, tool_daemons.sync_if_stale)
        daemons.start, notifier.notify = self.start, self.notify
        # autostart_all() first registers the daemons installed tools declare. Since the Download Hub ships
        # `download_hub-hub` with autostart on, that call added a third failure (a fake start() fails for every
        # daemon) to a test that counts only the daemons IT registered. Keep the registry to the test's own.
        tool_daemons.sync_if_stale = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        daemons.start, notifier.notify, tool_daemons.sync_if_stale = self._old
        return False


def _fail_all(did):
    return False, f"could not start '{did}'"


# --- the text builder -----------------------------------------------------

def test_text_for_one_failure():
    title, body = daemons._autostart_failure_text(
        [{"id": "web", "name": "Web console", "message": "no command configured"}])
    check("one failure: singular title", title == "A daemon did not start", title)
    check("one failure: shows name and id", "Web console (web)" in body, body)
    check("one failure: shows the reason", "no command configured" in body, body)
    check("one failure: points at the Daemons panel", "Daemons panel" in body, body)


def test_text_when_name_equals_id():
    _, body = daemons._autostart_failure_text([{"id": "web", "name": "web", "message": "x"}])
    check("name equal to id is not repeated", "web (web)" not in body and "- web: x" in body, body)


def test_text_for_several_and_the_line_cap():
    many = [{"id": f"d{i}", "name": f"d{i}", "message": "boom"} for i in range(12)]
    title, body = daemons._autostart_failure_text(many)
    check("several failures: count in the title", title == "12 daemons did not start", title)
    listed = [ln for ln in body.splitlines() if ln.startswith("- d")]
    check("only AUTOSTART_NOTICE_MAX_LINES daemons are listed",
          len(listed) == daemons.AUTOSTART_NOTICE_MAX_LINES, listed)
    check("the rest are counted, not dropped silently", "- and 4 more" in body, body)


def test_text_survives_a_missing_reason():
    _, body = daemons._autostart_failure_text([{"id": "a", "name": "a", "message": ""}])
    check("an empty reason gets a placeholder", "no reason given" in body, body)


# --- autostart_all() ------------------------------------------------------

def test_one_notification_for_a_failed_pass():
    _reset_registry()
    _register("l27c-aaa", name="Alpha")
    _register("l27c-bbb")
    spy = _Calls()
    with _Patched(_fail_all, spy):
        results = daemons.autostart_all()
    mine = [r for r in results if r["id"].startswith("l27c-")]
    check("both failures are still returned, same shape",
          len(mine) == 2 and all(r["ok"] is False and "message" in r for r in mine), results)
    check("exactly ONE notification for the whole pass", len(spy.calls) == 1, spy.calls)
    call = spy.calls[0] if spy.calls else {}
    check("kind is `daemon`", call.get("kind") == "daemon", call)
    check("flagged as failed", call.get("failed") is True, call)
    check("title carries the count", call.get("title") == "2 daemons did not start", call)
    check("body names both daemons",
          "Alpha (l27c-aaa)" in call.get("message", "") and "l27c-bbb" in call.get("message", ""),
          call.get("message"))
    check("last_error is still written for the panel",
          "autostart failed" in (daemons.status("l27c-aaa").get("last_error") or ""))
    _reset_registry()


def test_no_failure_no_notification():
    _reset_registry()
    _register("l27c-good")
    spy = _Calls()
    with _Patched(lambda did: (True, "started"), spy):
        results = daemons.autostart_all()
    check("a clean pass raises nothing", spy.calls == [], spy.calls)
    check("and still reports the start",
          any(r["id"] == "l27c-good" and r["ok"] for r in results), results)
    _reset_registry()


def test_a_broken_notifier_does_not_change_the_result():
    _reset_registry()
    _register("l27c-one")
    spy = _Calls(raises=True)
    with _Patched(_fail_all, spy):
        try:
            results = daemons.autostart_all()
            raised = None
        except Exception as exc:  # noqa: BLE001
            results, raised = None, exc
    check("autostart_all() does not raise when notify() does", raised is None, raised)
    check("it still returns the failed entry",
          bool(results) and any(r["id"] == "l27c-one" and not r["ok"] for r in results), results)
    _reset_registry()


def test_empty_failure_list_is_a_noop():
    spy = _Calls()
    with _Patched(_fail_all, spy):
        out = daemons._notify_autostart_failures([])
    check("no failures: helper returns None and notifies nobody", out is None and not spy.calls)


# --- the real notifier ----------------------------------------------------

def test_default_level_for_kind_daemon_is_standard():
    check("default level for `daemon` is 2",
          notifier.normalize_level(None, kind="daemon") == 2)
    check("`daemon` is listed in the default levels so Settings can tune it",
          notifier.DEFAULT_CONFIG["levels"].get("daemon") == 2)


def test_real_notifier_writes_the_inbox_record():
    _reset_registry()
    # Level 1 (inbox only) so this test never fires an OS toast.
    Path(notifier.CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(notifier.CONFIG_FILE).write_text(json.dumps({"levels": {"daemon": 1}}), encoding="utf-8")
    _register("l27c-real", name="Real one")
    with _Patched(_fail_all, notifier.notify):
        daemons.autostart_all()
    items = [i for i in notifier._load_inbox() if i.get("kind") == "daemon"]
    check("one `daemon` record is in the inbox", len(items) == 1, items)
    if items:
        rec = items[0]
        check("record is marked failed", rec.get("failed") is True, rec)
        check("level 1 from notify_config.json is honoured", rec.get("level") == 1, rec)
        check("level 1 delivers to the inbox only", rec.get("delivered") == ["inbox"], rec)
        check("record names the daemon", "Real one (l27c-real)" in rec.get("message", ""), rec)
    _reset_registry()


# --- Settings row ----------------------------------------------------------

def test_settings_has_a_daemon_problems_row():
    src = (ROOT / "web" / "public" / "settings.js").read_text(encoding="utf-8")
    # Since L.30 settings.js keeps no list of kinds: it draws a row for every registry kind with `settings`
    # true (pickerKinds), so the row exists when the registry says so.
    from jarvis import notify_registry as reg
    check("Settings > Notifications has the `daemon` row",
          any(k["id"] == "daemon" and k["label"] == "Daemon problems" and k["level"] == 2 and k["settings"]
              for k in reg.catalog()["kinds"]) and "pickerKinds()" in src)


if __name__ == "__main__":
    for name in [n for n in sorted(globals()) if n.startswith("test_")]:
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAIL.append((name, repr(exc)))
            print(f"FAILED  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
