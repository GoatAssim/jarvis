"""E0 -- one registry of notification actions, presets, kinds and positions.

jarvis/notify_registry.py is the single table; notifier.py, the scheduler tools, the digest, the
CLI, doctor, the Settings screen and the toast renderer all read it. This pins:

  * the table is consistent, and every channel row has a deliverer in the notifier (and the
    reverse), so adding a channel row without its `_deliver_<id>` is caught here, not in production;
  * `problems()` really catches the mistakes it claims to (duplicate ids, a gap in the levels,
    a preset adding an unknown action, a kind with a level that is not a preset, a channel with
    no deliverer, a deliverer with no channel);
  * nothing the notifier did before changed: CHANNELS, LEVEL_NAMES, POSITIONS (and their order),
    the level -> channel rule at every level, the record's `persistent` / `confirm_required`
    flags at every level, the default per-kind levels, the digest rule;
  * the catalog: shape, JSON-serialisable, a version that changes when the table changes, the
    owner's configured values under `configured`, and `jarvis notify-config --catalog`;
  * the text the model sees (`remind_me` / `notify_me` level and channel help) comes from the
    table, still names `desktop`, and still does not advertise the DM channels;
  * the web side reads the catalog and keeps its built-in fallbacks (static);
  * "how to add a new kind / level / action" actually works: a new row reaches the derived values.

Does not touch the Download Hub (jarvis/hub/) -- that code only reads notifier.CHANNELS and
notifier.LEVEL_NAMES, which keep their names and values (pinned below).

Run: python3 tests/test_notify_registry.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_e0_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import notify_registry as reg  # noqa: E402  (after HOME is redirected)
from jarvis import notifier  # noqa: E402

notifier.JARVIS_DIR = Path(HOME) / ".jarvis"
notifier.INBOX_FILE = notifier.JARVIS_DIR / "notifications.json"
notifier.CONFIG_FILE = notifier.JARVIS_DIR / "notify_config.json"

PASS, FAIL = [], []

EIGHT = ("top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right")
OLD_CHANNELS = ("inbox", "stream", "desktop", "voice", "playnite", "discord", "instagram")
OLD_LEVELS = {1: "silent", 2: "standard", 3: "persistent", 4: "broadcast", 5: "confirm"}
# "owner_dm" (silent, no Settings row) was added by E4 for the notify_owner tool's inbox record.
OLD_KIND_LEVELS = {"reminder": 2, "notify": 2, "task": 2, "clipboard_watch": 2, "ambient": 1,
                   "scheduled": 2, "daemon": 2, "owner_dm": 1}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _reset():
    for f in (notifier.CONFIG_FILE, notifier.INBOX_FILE):
        try:
            f.unlink()
        except OSError:
            pass


def _write_config(data):
    notifier.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    notifier.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


@contextlib.contextmanager
def _swapped(**tables):
    """Temporarily replace tables in notify_registry (ACTIONS=..., PRESETS=..., KINDS=...)."""
    saved = {k: getattr(reg, k) for k in tables}
    try:
        for k, v in tables.items():
            setattr(reg, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(reg, k, v)


@contextlib.contextmanager
def _stubbed_delivery(calls):
    saved = dict(notifier._DELIVERERS)
    try:
        for name in list(notifier._DELIVERERS):
            if name != "inbox":
                notifier._DELIVERERS[name] = (lambda n: lambda record, config: calls.append(n) or True)(name)
        yield
    finally:
        notifier._DELIVERERS.clear()
        notifier._DELIVERERS.update(saved)


# --- the table itself ---------------------------------------------------------------------------------

def test_the_table_is_consistent():
    check("no problems in the table", reg.problems() == [], reg.problems())
    check("no mismatch with the notifier's deliverers",
          reg.problems(notifier._DELIVERERS.keys()) == [], reg.problems(notifier._DELIVERERS.keys()))
    check("notifier.registry_problems() agrees", notifier.registry_problems() == [])
    check("every channel row has a deliverer",
          all(c in notifier._DELIVERERS for c in reg.channel_ids()))
    check("every deliverer has a channel row",
          all(c in reg.channel_ids() for c in notifier._DELIVERERS))


def test_problems_catches_mistakes():
    base_actions = reg.ACTIONS
    dup = base_actions + ({"id": "inbox", "type": "channel", "label": "again"},)
    with _swapped(ACTIONS=dup):
        check("a duplicate action id is reported", any("more than once" in p for p in reg.problems()), reg.problems())
    odd = base_actions + ({"id": "x", "type": "gizmo", "label": "x"},)
    with _swapped(ACTIONS=odd):
        check("an unknown action type is reported", any("unknown type" in p for p in reg.problems()), reg.problems())
    no_flag = base_actions + ({"id": "blink", "type": "behaviour", "label": "Blink"},)
    with _swapped(ACTIONS=no_flag):
        check("a behaviour with no record_flag is reported", any("record_flag" in p for p in reg.problems()), reg.problems())
    gap = tuple(p for p in reg.PRESETS if p["level"] != 3)
    with _swapped(PRESETS=gap):
        check("a gap in the levels is reported", any("1..N" in p for p in reg.problems()), reg.problems())
    bad_add = reg.PRESETS[:1] + (dict(reg.PRESETS[1], adds=("teleport",)),) + reg.PRESETS[2:]
    with _swapped(PRESETS=bad_add):
        check("a preset adding an unknown action is reported", any("unknown action" in p for p in reg.problems()), reg.problems())
    bad_kind = reg.KINDS + ({"id": "weird", "label": "Weird", "level": 9, "settings": True},)
    with _swapped(KINDS=bad_kind):
        check("a kind whose level is not a preset is reported", any("'weird'" in p for p in reg.problems()), reg.problems())
    with_new_channel = reg.ACTIONS + ({"id": "telegram", "type": "channel", "label": "Telegram"},)
    with _swapped(ACTIONS=with_new_channel):
        check("a channel row with no deliverer is reported",
              any("'telegram'" in p and "no deliverer" in p for p in reg.problems(notifier._DELIVERERS.keys())),
              reg.problems(notifier._DELIVERERS.keys()))
    check("a deliverer with no channel row is reported",
          any("'ghost'" in p for p in reg.problems(list(notifier._DELIVERERS) + ["ghost"])))


# --- nothing the notifier did before has changed ------------------------------------------------------

def test_values_the_rest_of_jarvis_imports_are_unchanged():
    check("CHANNELS is the same seven, same order", tuple(notifier.CHANNELS) == OLD_CHANNELS, notifier.CHANNELS)
    check("LEVEL_NAMES is the same", dict(notifier.LEVEL_NAMES) == OLD_LEVELS, notifier.LEVEL_NAMES)
    check("MIN/MAX/DEFAULT level", (notifier.MIN_LEVEL, notifier.MAX_LEVEL, notifier.DEFAULT_LEVEL) == (1, 5, 2))
    check("POSITIONS is the same eight, same order", tuple(notifier.POSITIONS) == EIGHT, notifier.POSITIONS)
    check("DEFAULT_POSITION is bottom-right", notifier.DEFAULT_POSITION == "bottom-right")
    check("the legacy `toast` alias still maps to desktop", notifier.canonical_channel("toast") == "desktop")
    check("default per-kind levels are the same (plus E4's silent `owner_dm`)", notifier.DEFAULT_CONFIG["levels"] == OLD_KIND_LEVELS,
          notifier.DEFAULT_CONFIG["levels"])
    check("DEFAULT_CONFIG per-kind channels are untouched",
          notifier.DEFAULT_CONFIG["channels"]["task"] == ["inbox", "stream"])


def test_level_to_channels_rule_is_unchanged():
    want = {1: ["inbox"], 2: ["inbox", "stream", "desktop"],
            3: ["inbox", "stream", "desktop"],
            4: ["inbox", "stream", "desktop", "discord", "instagram"],
            5: ["inbox", "stream", "desktop", "discord", "instagram"]}
    for level, channels in want.items():
        check(f"level {level} delivers {channels}", notifier._channels_for_level(level) == channels,
              notifier._channels_for_level(level))


def test_record_flags_at_every_level():
    _reset()
    calls = []
    with _stubbed_delivery(calls):
        for level in (1, 2, 3, 4, 5):
            rec = notifier.notify("T", "m", level=level, kind="notify")
            check(f"level {level}: persistent == (level >= 3)", rec["persistent"] is (level >= 3), rec)
            check(f"level {level}: confirm_required == (level >= 5)", rec["confirm_required"] is (level >= 5), rec)
            check(f"level {level}: level_name from the preset", rec["level_name"] == OLD_LEVELS[level], rec)
    _reset()


def test_digest_rule_is_unchanged():
    from jarvis import digest
    on, off = {"enabled": True}, {"enabled": False}
    check("levels 1-2 batch when the digest is on",
          digest.should_batch_level(1, on) is True and digest.should_batch_level(2, on) is True)
    check("levels 3-5 never batch",
          all(digest.should_batch_level(n, on) is False for n in (3, 4, 5)))
    check("nothing batches when the digest is off", digest.should_batch_level(1, off) is False)
    check("a junk level does not batch", digest.should_batch_level("banana", on) is False)


# --- the catalog --------------------------------------------------------------------------------------

def test_catalog_shape():
    _reset()
    cat = notifier.catalog()
    check("catalog is JSON-serialisable", bool(json.dumps(cat)))
    for key in ("actions", "presets", "kinds", "positions", "min_level", "max_level",
                "default_level", "default_position", "version", "configured"):
        check(f"catalog has {key}", key in cat, list(cat))
    check("nine actions: seven channels and two behaviours",
          [a["type"] for a in cat["actions"]].count("channel") == 7
          and [a["type"] for a in cat["actions"]].count("behaviour") == 2, cat["actions"])
    check("five presets, 1..5", [p["level"] for p in cat["presets"]] == [1, 2, 3, 4, 5])
    p4 = [p for p in cat["presets"] if p["level"] == 4][0]
    check("a preset lists what it delivers and its flags (cumulative)",
          p4["channels"] == ["inbox", "stream", "desktop", "discord", "instagram"]
          and p4["behaviours"] == ["persistent"]
          and p4["flags"] == {"persistent": True, "confirm_required": False}, p4)
    check("model_hint is not published (it is model-facing prose)",
          all("model_hint" not in a for a in cat["actions"]))
    check("positions are in picker order, bottom-right first",
          [p["id"] for p in cat["positions"]][0] == "bottom-right"
          and sorted(p["id"] for p in cat["positions"]) == sorted(EIGHT), cat["positions"])
    check("settings kinds are the six that had rows; `scheduled` has none",
          [k["id"] for k in cat["kinds"] if k["settings"]]
          == ["reminder", "notify", "task", "clipboard_watch", "ambient", "daemon"], cat["kinds"])
    check("a version string", isinstance(cat["version"], str) and len(cat["version"]) == 12)


def test_catalog_version_follows_the_table_not_the_config():
    _reset()
    v1 = notifier.catalog()["version"]
    _write_config({"levels": {"reminder": 4}, "position": "top"})
    check("changing the owner's config does not change the version", notifier.catalog()["version"] == v1)
    with _swapped(KINDS=reg.KINDS + ({"id": "download", "label": "Downloads", "level": 2, "settings": True},)):
        v2 = reg.catalog()["version"]
    check("changing the table does", v1 != v2, (v1, v2))
    _reset()


def test_catalog_carries_the_owners_configured_values():
    _reset()
    _write_config({"levels": {"reminder": 4}, "position": "top", "positions": {"task": "left"}, "enabled": False})
    conf = notifier.catalog()["configured"]
    check("levels (merged over the defaults)", conf["levels"].get("reminder") == 4 and conf["levels"].get("task") == 2, conf)
    check("position and per-kind positions", conf["position"] == "top" and conf["positions"] == {"task": "left"}, conf)
    check("enabled", conf["enabled"] is False, conf)
    check("a bare registry catalog (no config) has no `configured`", "configured" not in reg.catalog())
    _reset()


def test_cli_prints_the_catalog():
    from jarvis import cli
    _reset()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_scheduler_command(["notify-config", "--catalog"])
    try:
        doc = json.loads(buf.getvalue())
    except ValueError:
        doc = None
    check("`notify-config --catalog` exits 0 and prints the catalog as JSON",
          code == 0 and isinstance(doc, dict) and doc.get("version") == reg.catalog()["version"], buf.getvalue()[:200])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_scheduler_command(["notify-config"])
    check("`notify-config` alone still prints just the config path",
          code == 0 and buf.getvalue().strip().endswith("notify_config.json"), buf.getvalue())
    _reset()


# --- what the model sees ------------------------------------------------------------------------------

def test_tool_help_comes_from_the_table():
    from jarvis.actions import scheduler_tools as st
    check("_LEVEL_HELP is the registry's", st._LEVEL_HELP == reg.level_help())
    check("_CHANNEL_HELP is the registry's", st._CHANNEL_HELP == reg.channel_help())
    check("level help names every preset", all(p["id"] in st._LEVEL_HELP for p in reg.PRESETS), st._LEVEL_HELP)
    check("channel help still names desktop and says toast still works",
          "'desktop'" in st._CHANNEL_HELP and "'toast'" in st._CHANNEL_HELP)
    check("channel help does not advertise the DM channels",
          "discord" not in st._CHANNEL_HELP.lower() and "instagram" not in st._CHANNEL_HELP.lower(), st._CHANNEL_HELP)
    check("_level() accepts 1..5 and rejects 0 and 6",
          st._level({"level": 1}) == 1 and st._level({"level": 5}) == 5
          and st._level({"level": 0}) is None and st._level({"level": 6}) is None)
    check("scheduler_channels still lists toast and desktop",
          "toast" in st.scheduler_channels() and "desktop" in st.scheduler_channels())


# --- adding a new kind / level / action actually works -------------------------------------------------

def test_adding_a_kind_reaches_the_derived_values():
    with _swapped(KINDS=reg.KINDS + ({"id": "download", "label": "Downloads", "level": 3, "settings": True},)):
        check("default_levels has the new kind", reg.default_levels().get("download") == 3)
        cat = reg.catalog()
        check("the catalog lists it with a Settings row", any(k["id"] == "download" and k["settings"] for k in cat["kinds"]))
        check("the table is still consistent", reg.problems(notifier._DELIVERERS.keys()) == [], reg.problems())


def test_adding_a_level_reaches_the_help_the_catalog_and_the_flags():
    sixth = {"level": 6, "id": "emergency", "name": "Emergency", "description": "Everything, loudly.",
             "help": "every channel, until acknowledged", "adds": (), "digest_eligible": False}
    with _swapped(PRESETS=reg.PRESETS + (sixth,)):
        check("the table is still consistent", reg.problems() == [], reg.problems())
        check("level_names has it", reg.level_names()[6] == "emergency")
        check("the tool help mentions it", "6 emergency" in reg.level_help() and "1-5" not in reg.level_help(), reg.level_help())
        check("it inherits everything below it", reg.channels_for_level(6) == reg.channels_for_level(5))
        check("its flags are the lower levels'", reg.flags_for_level(6) == {"persistent": True, "confirm_required": True})
        check("the catalog's max_level moves",
              reg.catalog()["presets"][-1]["level"] == 6 and reg.catalog()["presets"][-1]["id"] == "emergency")


def test_adding_a_behaviour_adds_a_record_flag():
    flash = {"id": "flash", "type": "behaviour", "label": "Flash the taskbar", "record_flag": "flash_taskbar"}
    presets = reg.PRESETS[:3] + (dict(reg.PRESETS[3], adds=reg.PRESETS[3]["adds"] + ("flash",)),) + reg.PRESETS[4:]
    with _swapped(ACTIONS=reg.ACTIONS + (flash,), PRESETS=presets):
        check("consistent", reg.problems(notifier._DELIVERERS.keys()) == [], reg.problems(notifier._DELIVERERS.keys()))
        check("levels below 4 do not set it", reg.flags_for_level(3)["flash_taskbar"] is False)
        check("level 4 and up do", reg.flags_for_level(4)["flash_taskbar"] is True and reg.flags_for_level(5)["flash_taskbar"] is True)
        check("channels_for_level ignores a behaviour", "flash" not in reg.channels_for_level(4))


# --- static: the web side reads the catalog and keeps its fallbacks ---------------------------------------

def test_web_side_reads_the_catalog():
    web = ROOT / "web"
    server = (web / "server.js").read_text(encoding="utf-8")
    check("server.js serves /api/notifications/catalog from `notify-config --catalog`",
          '"/api/notifications/catalog"' in server and '["notify-config", "--catalog"]' in server)
    nj = (web / "public" / "notifications.js").read_text(encoding="utf-8")
    check("the toast renderer loads the catalog", "loadCatalog" in nj and "API.catalog" in nj)
    check("it keeps its built-in position list as the fallback", 'const POSITIONS = ["top", "bottom"' in nj)
    check("level 2 / level 5 are not hard-coded in show()", "level >= 2" not in nj.split("function show(")[1].split("function ")[0]
          if "function show(" in nj else True)
    sj = (web / "public" / "settings.js").read_text(encoding="utf-8")
    check("Settings > Notifications builds from the catalog", "notifyCatalog()" in sj and "cat.presets" in sj and "cat.kinds" in sj)
    check("Settings keeps the built-in lists as the fallback", "const LEVELS = [[1," in sj and "const NOTIFY_KINDS" in sj
          and '["daemon", "Daemon problems", 2]' in sj)
    check("Settings still never builds markup from a value", ".innerHTML" not in sj)


def test_doctor_reports_a_registry_mismatch():
    from jarvis import doctor
    src = (ROOT / "jarvis-cli" / "jarvis" / "doctor.py").read_text(encoding="utf-8")
    check("doctor has the registry check", "notify.registry" in src and "registry_problems" in src)
    saved = dict(notifier._DELIVERERS)
    try:
        notifier._DELIVERERS.pop("playnite", None)
        out = doctor.check_notifications()
        check("a missing deliverer shows up as a WARN in doctor",
              any(getattr(c, "id", getattr(c, "key", "")) == "notify.registry" for c in out)
              or any("notify.registry" in repr(c) for c in out), out)
    finally:
        notifier._DELIVERERS.clear()
        notifier._DELIVERERS.update(saved)
    _reset()


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
