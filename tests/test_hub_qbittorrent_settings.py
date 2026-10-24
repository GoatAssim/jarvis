"""Download Hub, brief 31 (H12, TRACK_E): the qBittorrent settings block and concurrency.qbittorrent.

Covers: the block's defaults match the engine's own (decision 11 as proposed); concurrency.qbittorrent is
2 (what the engine got as the fallback before, so nothing changes), writable, 1..16, and in the daemon's
caps; which keys the everyday tier (dl_prefs) may set and which need dl_settings; every value's range;
the connection (address, user name, PASSWORD) and the on/off switch are NOT settings (refused, never
stored); the settings answer, the dl_settings / dl_prefs answers and the doctor lines never contain the
password (the JSON is grepped), not even when a stray `password` key sits in the stored block; the
engine takes a behaviour key from the file when the file sets it, else from the block, else the
default, and prefer_for_magnets is on when either says so.

Run: python tests/test_hub_qbittorrent_settings.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-qbset-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_qbittorrent as fake  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import doctor, engines, service, settings as hs  # noqa: E402
from jarvis.hub.engines import qbittorrent as qbe  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

qbe.MIN_POLL_S = 0.02

passed = failed = 0
SECRET = "S3cret-Pa55-qbit-9x"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def config_file():
    return Path(HOME) / ".jarvis" / "qbittorrent.json"


def write_config(**cfg):
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps(cfg), encoding="utf-8")


# ---------------------------------------------------------------- defaults
merged = hs.merged({})
block = merged["qbittorrent"]
check("every key in the block is one of the engine's behaviour keys", set(block) == set(qbe.SETTINGS_KEYS))
check("the block's defaults are the engine's own defaults (decision 11 as proposed)",
      all(block[k] == qbe.DEFAULT_CONFIG[k] for k in block))
check("the block is off-by-nothing: prefer_for_magnets False, cancel_deletes_data True",
      block["prefer_for_magnets"] is False and block["cancel_deletes_data"] is True)
check("concurrency.qbittorrent defaults to 2 (what the fallback gave before)", merged["concurrency"]["qbittorrent"] == 2)
check("the other three concurrency defaults are untouched",
      {k: merged["concurrency"][k] for k in ("total", "http", "ytdlp", "stremio")} == {"total": 4, "http": 3, "ytdlp": 2, "stremio": 3})

# ---------------------------------------------------------------- writable keys and tiers
check("concurrency.qbittorrent is an everyday key", "concurrency.qbittorrent" in hs.PREFS_WRITABLE)
for key in ("prefer_for_magnets", "category", "metadata_timeout_s", "stall_minutes", "poll_s"):
    check("qbittorrent.%s is an everyday key (dl_prefs)" % key, "qbittorrent." + key in hs.PREFS_WRITABLE)
for key in ("cancel_deletes_data", "after_done", "save_subfolder", "tag"):
    check("qbittorrent.%s needs dl_settings (it deletes data, moves files or changes what counts as the hub's)" % key,
          "qbittorrent." + key in hs.WRITABLE and "qbittorrent." + key not in hs.PREFS_WRITABLE)
for key in ("address", "username", "password", "enabled", "base"):
    clean, errors = hs.validate_update({"qbittorrent." + key: "x"})
    check("qbittorrent.%s is not a setting at all (the file's alone)" % key, not clean and errors and "can't be changed here" in errors[0])
check("a whole `qbittorrent` object can't be written either", not hs.validate_update({"qbittorrent": {"password": "x"}})[0])

# ---------------------------------------------------------------- validators
def ok(key, value):
    clean, errors = hs.validate_update({key: value})
    return clean.get(key) if clean and not errors else None


def bad(key, value):
    clean, errors = hs.validate_update({key: value})
    return not clean and bool(errors)


check("concurrency.qbittorrent 1..16", ok("concurrency.qbittorrent", 5) == 5 and bad("concurrency.qbittorrent", 0) and bad("concurrency.qbittorrent", 17))
check("after_done keep / pause / remove only", ok("qbittorrent.after_done", "pause") == "pause" and bad("qbittorrent.after_done", "delete"))
check("cancel_deletes_data and prefer_for_magnets are booleans (the same words every other switch takes)",
      ok("qbittorrent.cancel_deletes_data", False) is False and bad("qbittorrent.cancel_deletes_data", "maybe")
      and ok("qbittorrent.prefer_for_magnets", "on") is True and bad("qbittorrent.prefer_for_magnets", 7))
check("metadata_timeout_s 60..86400", ok("qbittorrent.metadata_timeout_s", 600) == 600 and bad("qbittorrent.metadata_timeout_s", 59) and bad("qbittorrent.metadata_timeout_s", 86401))
check("stall_minutes 0..10080", ok("qbittorrent.stall_minutes", 0) == 0 and bad("qbittorrent.stall_minutes", -1) and bad("qbittorrent.stall_minutes", 10081))
check("poll_s 0.5..30", ok("qbittorrent.poll_s", 1) == 1.0 and bad("qbittorrent.poll_s", 0.1) and bad("qbittorrent.poll_s", 31) and bad("qbittorrent.poll_s", "fast"))
check("tag and category: plain text, no commas / backslashes / control characters",
      ok("qbittorrent.tag", " mine ") == "mine" and bad("qbittorrent.tag", "a,b") and bad("qbittorrent.tag", "a\\b") and bad("qbittorrent.tag", "a\nb")
      and ok("qbittorrent.category", "TV") == "TV" and bad("qbittorrent.category", 5) and bad("qbittorrent.tag", "x" * 61))
check("save_subfolder is a plain sub-folder (not a path out of the library)",
      ok("qbittorrent.save_subfolder", "Torrents/TV") == "Torrents/TV" and bad("qbittorrent.save_subfolder", "../x")
      and bad("qbittorrent.save_subfolder", "D:/x") and bad("qbittorrent.save_subfolder", "/etc"))

# ---------------------------------------------------------------- through the service
store = Store(Path(HOME) / "qb.db")
r = service.update_prefs({"qbittorrent.poll_s": 1.5, "concurrency.qbittorrent": 3}, store=store)
check("dl_prefs sets an everyday qBittorrent key and the connection count", r["ok"] and store.get_settings()["qbittorrent"]["poll_s"] == 1.5
      and store.get_settings()["concurrency"]["qbittorrent"] == 3)
r = service.update_prefs({"qbittorrent.cancel_deletes_data": False}, store=store)
check("dl_prefs refuses a dl_settings key and changes nothing", r["ok"] is False and "dl_settings" in json.dumps(r)
      and store.get_settings()["qbittorrent"]["cancel_deletes_data"] is True)
r = service.update_settings({"qbittorrent.cancel_deletes_data": False, "qbittorrent.after_done": "pause"}, store=store)
check("dl_settings sets those", r["ok"] and store.get_settings()["qbittorrent"]["cancel_deletes_data"] is False
      and store.get_settings()["qbittorrent"]["after_done"] == "pause")
view = service.get_settings(store=store)["settings"]
check("the settings answer shows the block with exactly the known keys",
      set(view["qbittorrent"]) == set(qbe.SETTINGS_KEYS) and view["qbittorrent"]["after_done"] == "pause" and view["concurrency"]["qbittorrent"] == 3)

# the daemon's caps include the new key
from jarvis.hub import daemon  # noqa: E402
d = daemon.Daemon(store=store) if hasattr(daemon, "Daemon") else None
if d is not None and hasattr(d, "_caps"):
    conc, total = d._caps()
    check("the daemon's caps have qbittorrent at 3 now (not the fallback of 2)", conc.get("qbittorrent") == 3 and conc["default"] == 2)

# ---------------------------------------------------------------- no secret anywhere
qb = fake.start(fake.FakeQb(username="admin", password=SECRET))
write_config(address=qb.base, username="admin", password=SECRET, poll_s=0.02, tag="jarvis")
# a stray password sitting in the stored block (hand-edited database) must not leak either
store.set_setting_raw("qbittorrent", dict(store.get_settings()["qbittorrent"], password=SECRET, username="admin", address="http://x"))
answers = {
    "settings answer": service.get_settings(store=store),
    "dl_settings read": dh.tool_dl_settings({}),
    "dl_prefs read": dh.tool_dl_prefs({}),
    "update answer": service.update_prefs({"qbittorrent.poll_s": 2}, store=store),
    "doctor": doctor.run(store=store),
    "engine availability": engines.availability(),
    "engine doctor rows": qbe.ENGINE.doctor(settings=store.get_settings()),
}
for label, answer in answers.items():
    check("%s never contains the password" % label, SECRET not in json.dumps(answer, default=str))
check("...and the stray keys are not in the settings answer either",
      "admin" not in json.dumps(answers["settings answer"]["settings"]["qbittorrent"]) and "http://x" not in json.dumps(answers["settings answer"]["settings"]["qbittorrent"]))
rows = answers["engine doctor rows"]
check("the doctor line is there and says it reached qBittorrent", rows and rows[0]["status"] == "ok" and "tagged" in rows[0]["detail"])

write_config(address=qb.base, username="admin", password=SECRET + "-wrong", poll_s=0.02)
qbe.ENGINE._avail = None
wrong = [doctor.run(store=store), engines.availability(), qbe.ENGINE.doctor(settings=store.get_settings())]
check("a failed login (wrong password) names neither password", SECRET not in json.dumps(wrong, default=str) and SECRET + "-wrong" not in json.dumps(wrong, default=str))
write_config(address="http://127.0.0.1:9", username="admin", password=SECRET, poll_s=0.02)
qbe.ENGINE._avail = None
down = [doctor.run(store=store), engines.availability(), qbe.ENGINE.doctor(settings=store.get_settings())]
check("qBittorrent not running leaks no password either", SECRET not in json.dumps(down, default=str))
cfg, problem = qbe.load_config(settings=store.get_settings())
check("load_config's problem text never has it", SECRET not in str(problem))

# ---------------------------------------------------------------- precedence: file > block > default
write_config(address=qb.base, username="admin", password=SECRET)
cfg, problem = qbe.load_config(settings={"qbittorrent": {"tag": "fromblock", "after_done": "remove", "stall_minutes": 30}})
check("a key the file leaves out comes from the block", problem is None and cfg["tag"] == "fromblock" and cfg["after_done"] == "remove" and cfg["stall_minutes"] == 30)
write_config(address=qb.base, username="admin", password=SECRET, tag="fromfile")
cfg, _ = qbe.load_config(settings={"qbittorrent": {"tag": "fromblock", "poll_s": 5}})
check("a key the file sets is the file's", cfg["tag"] == "fromfile" and cfg["poll_s"] == 5.0)
cfg, _ = qbe.load_config()
check("with no settings it is the file, then the defaults (as before)", cfg["tag"] == "fromfile" and cfg["after_done"] == "keep")
cfg, _ = qbe.load_config(settings={"qbittorrent": {"after_done": "explode", "poll_s": "x", "metadata_timeout_s": 1}})
check("a bad stored value falls back, never breaks a run", cfg["after_done"] == "keep" and cfg["poll_s"] == 2.0 and cfg["metadata_timeout_s"] >= qbe.MIN_METADATA_S)
cfg, _ = qbe.load_config(settings={"qbittorrent": {"password": "other", "username": "other", "address": "http://10.0.0.5:1"}})
check("the block cannot carry the connection: the file's address, user and password stand",
      cfg["password"] == SECRET and cfg["username"] == "admin" and cfg["base"] == qb.base)
write_config(address=qb.base, username="admin", password=SECRET, enabled=False)
cfg, problem = qbe.load_config(settings={"qbittorrent": {}})
check("`enabled: false` in the file still switches it off", cfg is None and "switched off" in problem)

# prefer_for_magnets: either one is enough
write_config(address=qb.base, username="admin", password=SECRET, prefer_for_magnets=False)
check("block True wins over a file that says False (as prefers_magnets always did)", qbe.prefers_magnets({"qbittorrent": {"prefer_for_magnets": True}}) is True
      and qbe.load_config(settings={"qbittorrent": {"prefer_for_magnets": True}})[0]["prefer_for_magnets"] is True)
write_config(address=qb.base, username="admin", password=SECRET, prefer_for_magnets=True)
check("a file that says True is enough", qbe.prefers_magnets({"qbittorrent": {"prefer_for_magnets": False}}) is True)
write_config(address=qb.base, username="admin", password=SECRET)
check("neither says it: not preferred", qbe.prefers_magnets({"qbittorrent": {"prefer_for_magnets": False}}) is False and qbe.prefers_magnets({}) is False)

fake_servers = [qb]
for s in fake_servers:
    try:
        s.stop()
    except Exception:  # noqa: BLE001
        pass

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
