"""E2 -- toast position, and the native OS notification renamed `desktop`.

Covers (backend, no network, no real OS notification, no sleeping):
  * normalize_position(): explicit > source > kind > global default > bottom-right;
    all eight positions accepted; spelling variants; a bad value falls to the next layer;
  * notify() puts `position` on the record and on the stream line, and the config file's
    `positions` / `position` are honoured;
  * the rename: CHANNELS has `desktop`, not `toast`; an explicit `toast` is read as
    `desktop` (the stub for the OS notification is called); an old notify_config.json
    that says "toast" is read as "desktop"; level 2+ delivers `desktop`;
  * the scheduler tools still accept `toast` and pass it through unchanged;
  * `jarvis notify-send` argument parsing (--position / --level, both spellings, errors);
  * static: Settings has the position row; the web stack code has one stack per position.

Run: python3 tests/test_e2_toast_position.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_e2_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import notifier  # noqa: E402  (after HOME is redirected)

# Redirect the notifier's own files into the temp home, whatever it computed at import.
notifier.JARVIS_DIR = Path(HOME) / ".jarvis"
notifier.INBOX_FILE = notifier.JARVIS_DIR / "notifications.json"
notifier.CONFIG_FILE = notifier.JARVIS_DIR / "notify_config.json"

PASS, FAIL = [], []
EIGHT = ("top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _write_config(data):
    notifier.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    notifier.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


def _reset():
    for f in (notifier.CONFIG_FILE, notifier.INBOX_FILE):
        try:
            f.unlink()
        except OSError:
            pass


def _isolate_delivery(calls):
    """Replace every live deliverer so nothing leaves the process."""
    def make(name):
        def fake(record, config):
            calls.append(name)
            return True
        return fake
    for name in list(notifier._DELIVERERS):
        if name != "inbox":
            notifier._DELIVERERS[name] = make(name)


# --- positions ---------------------------------------------------------------

def test_the_eight_positions():
    check("POSITIONS is the eight sides and corners", tuple(notifier.POSITIONS) == EIGHT, notifier.POSITIONS)
    check("default position is bottom-right (D-12)", notifier.DEFAULT_POSITION == "bottom-right")
    _reset()
    for pos in EIGHT:
        check(f"normalize_position accepts {pos}", notifier.normalize_position(pos) == pos)


def test_spelling_variants():
    _reset()
    for raw in ("Bottom Right", "bottom_right", "BOTTOM-RIGHT", "  bottom-right "):
        check(f"{raw!r} reads as bottom-right", notifier.normalize_position(raw) == "bottom-right")


def test_bad_values_fall_to_the_default():
    _reset()
    for bad in ("middle", "", None, 5, ["top"], "top-centre"):
        check(f"bad value {bad!r} -> default",
              notifier.normalize_position(bad) == "bottom-right")


def test_lookup_order():
    _reset()
    cfg = {"position": "top-left", "positions": {"reminder": "left", "my_tool": "top"}}
    check("explicit beats everything",
          notifier.normalize_position("right", kind="reminder", source="my_tool", config=cfg) == "right")
    check("source beats kind",
          notifier.normalize_position(None, kind="reminder", source="my_tool", config=cfg) == "top")
    check("kind beats the global default",
          notifier.normalize_position(None, kind="reminder", config=cfg) == "left")
    check("global default when nothing else says",
          notifier.normalize_position(None, kind="task", config=cfg) == "top-left")
    check("a bad explicit value falls through to the next layer",
          notifier.normalize_position("nowhere", kind="reminder", config=cfg) == "left")
    check("a bad kind value falls through to the global default",
          notifier.normalize_position(None, kind="x", config={"position": "bottom", "positions": {"x": "??"}}) == "bottom")
    check("a bad global default falls to bottom-right",
          notifier.normalize_position(None, config={"position": "??", "positions": {}}) == "bottom-right")
    check("a `positions` that is not a dict does not raise",
          notifier.normalize_position(None, kind="x", config={"position": "top", "positions": ["x"]}) == "top")


def test_notify_records_the_position():
    _reset()
    calls = []
    saved = dict(notifier._DELIVERERS)
    try:
        _isolate_delivery(calls)
        rec = notifier.notify("T", "m", channels=["inbox"], kind="notify")
        check("record carries the default position", rec.get("position") == "bottom-right", rec)
        rec = notifier.notify("T", "m", channels=["inbox"], kind="notify", position="top-right")
        check("explicit position lands on the record", rec.get("position") == "top-right", rec)
        rec = notifier.notify("T", "m", channels=["inbox"], kind="notify", position="bogus")
        check("a bad explicit position gives the default, never an error",
              rec.get("position") == "bottom-right", rec)
        _write_config({"position": "left", "positions": {"task": "top"}})
        check("config global default is used", notifier.notify("T", "m", channels=["inbox"], kind="notify").get("position") == "left")
        check("config per-kind value is used", notifier.notify("T", "m", channels=["inbox"], kind="task").get("position") == "top")
        check("source beats kind in the config",
              notifier.normalize_position(None, kind="task", source="s",
                                          config={"positions": {"s": "right", "task": "top"}}) == "right")
        check("the stored inbox record has the position",
              any(i.get("position") == "top" for i in notifier._load_inbox()))
    finally:
        notifier._DELIVERERS.update(saved)
        _reset()


# --- the rename --------------------------------------------------------------

def test_channels_renamed():
    check("CHANNELS has desktop", "desktop" in notifier.CHANNELS)
    check("CHANNELS no longer has toast", "toast" not in notifier.CHANNELS)
    check("the deliverer table has desktop, not toast",
          "desktop" in notifier._DELIVERERS and "toast" not in notifier._DELIVERERS)
    check("toast is a legacy alias for desktop", notifier.canonical_channel("toast") == "desktop")
    check("canonical_channel drops unknowns", notifier.canonical_channel("carrier-pigeon") is None)
    check("canonical_channels de-duplicates in order",
          notifier.canonical_channels(["Inbox", "toast", "desktop", "nope", "stream"]) == ["inbox", "desktop", "stream"])
    lvl2 = notifier._channels_for_level(2)
    check("level 2 delivers desktop", "desktop" in lvl2 and "toast" not in lvl2, lvl2)


def test_explicit_toast_still_reaches_the_os_notification():
    _reset()
    calls = []
    saved = dict(notifier._DELIVERERS)
    try:
        _isolate_delivery(calls)
        rec = notifier.notify("T", "m", channels=["inbox", "toast"], kind="notify")
        check("explicit 'toast' ran the desktop deliverer", "desktop" in calls, calls)
        check("and it is reported as delivered on `desktop`", "desktop" in rec["delivered"], rec)
    finally:
        notifier._DELIVERERS.update(saved)
        _reset()


def test_old_config_file_is_read_as_desktop():
    _reset()
    _write_config({"channels": {"reminder": ["inbox", "stream", "toast"], "task": ["inbox", "stream"]}})
    cfg = notifier._load_config()
    check("an old config saying toast now says desktop",
          cfg["channels"]["reminder"] == ["inbox", "stream", "desktop"], cfg["channels"])
    check("a list with no toast is untouched", cfg["channels"]["task"] == ["inbox", "stream"])
    check("the shipped default says desktop",
          notifier.DEFAULT_CONFIG["channels"]["notify"] == ["inbox", "stream", "desktop"])
    check("the shipped config has a position and an empty positions map",
          notifier.DEFAULT_CONFIG["position"] == "bottom-right" and notifier.DEFAULT_CONFIG["positions"] == {})
    _reset()


def test_scheduler_tools_still_accept_toast():
    from jarvis.actions import scheduler_tools as st
    check("toast is still an accepted channel name", "toast" in st.scheduler_channels())
    check("so is desktop", "desktop" in st.scheduler_channels())
    check("a channels argument is passed through unchanged",
          st._channels({"channels": ["inbox", "toast"]}) == ["inbox", "toast"])
    check("the model-facing help names desktop", "'desktop'" in st._CHANNEL_HELP)


# --- notify-send -------------------------------------------------------------

def test_notify_send_argument_parsing():
    from jarvis import cli
    f = cli.parse_notify_send_args
    check("plain words keep their meaning", f(["hi", "Title", "inbox,stream"]) == (["hi", "Title", "inbox,stream"], None, None, None))
    check("--position=P", f(["hi", "--position=top-left"])[:3] == (["hi"], "top-left", None))
    check("--position P", f(["hi", "--position", "Bottom Left"])[:3] == (["hi"], "bottom-left", None))
    check("--level N and --position together, any order",
          f(["--level", "3", "hi", "T", "--position=right"])[:3] == (["hi", "T"], "right", "3"))
    check("--level=N", f(["hi", "--level=4"])[:3] == (["hi"], None, "4"))
    err = f(["hi", "--position=middle"])[3]
    check("a bad position is an error that lists the choices", bool(err) and "bottom-right" in err, err)
    check("--position with no value is an error", bool(f(["hi", "--position"])[3]))
    check("--level with no value is an error", bool(f(["hi", "--level"])[3]))


# --- static: the web side ----------------------------------------------------

def test_web_side_is_wired():
    web = ROOT / "web" / "public"
    settings = (web / "settings.js").read_text(encoding="utf-8")
    check("Settings > Notifications has the position row",
          '"Where toasts appear"' in settings and 'path: ["position"]' in settings)
    for pos in EIGHT:
        check(f"Settings offers {pos}", f'["{pos}", ' in settings)
    js = (web / "notifications.js").read_text(encoding="utf-8")
    # E3: the stacks are the page's one toast host, made by ui-kit.js (JarvisUI.toastStack);
    # notifications.js asks it for the stack of a notification's position.
    check("the toast host makes a stack per position",
          "ensureStack(positionOf(n))" in js and "UI.toastStack(pos)" in js)
    css = (web / "ui-kit.css").read_text(encoding="utf-8")
    for pos in EIGHT:
        check(f"CSS has a stack for {pos}", f".jui-toasts--{pos}" in css)
    sched = (web / "schedules.js").read_text(encoding="utf-8")
    check("Schedules offers desktop, not toast", '["desktop", "Desktop"]' in sched and '["toast", "Toast"]' not in sched)
    check("Schedules maps an old stored `toast` to desktop", "legacyChannel" in sched)


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
