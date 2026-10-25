"""Download Hub, brief 7C: `retention.keep_history_days` is writable from the Settings page.

Covers: the validator (refuses -1, 3651, "ten", True, 1.5, nan and a missing value; accepts 0, 90, 3650, "90",
90.0); the default is unchanged (90) and `retention.auto_delete_files` is still not writable by anyone;
dl_prefs REFUSES the key (it is not in PREFS_WRITABLE: the model can't change it without the owner), while
dl_settings (confirm-gated, the path the Settings page uses) takes it; the value is read back and the daemon's
pruning uses it; pruning itself is unchanged (History ROWS only, never a file; 0 or less keeps everything); no
other default, range or writable key moved.

Also (brief 7C, part D): the qBittorrent file override.  `qbittorrent.file_overridden_keys()` names the behaviour keys
~/.jarvis/qbittorrent.json sets and nothing else; the hub_admin `settings` op reports them as
overridden_by: "qbittorrent.json"; precedence is unchanged (the file still wins); and no secret (address, user
name, password, or a behaviour value) appears in any answer.

Run: python tests/test_hub_settings_retention.py   (Linux, no network)
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-retention-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import service, states  # noqa: E402
from jarvis.hub import settings as S  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
KEY = "retention.keep_history_days"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def saved():
    """The keep_history_days now in force, read from the store the tools write to."""
    return service._store(None).get_settings()["retention"]["keep_history_days"]


# ---------------------------------------------------------------- the validator
def verdict(value, allowed=None):
    return S.validate_update({KEY: value}) if allowed is None else S.validate_update({KEY: value}, allowed=allowed)


for bad in (-1, 3651, "ten", True, False, 1.5, float("nan"), float("inf"), None, [90], {"a": 1}, "1.5", "", 10 ** 9):
    clean, problems = verdict(bad)
    check("%r is refused, with a sentence, and nothing is kept" % (bad,), clean == {} and len(problems) == 1 and KEY in problems[0])
for good, kept in ((0, 0), (90, 90), (3650, 3650), (1, 1), ("90", 90), (90.0, 90)):
    clean, problems = verdict(good)
    check("%r is accepted as %r" % (good, kept), clean == {KEY: kept} and problems == [] and type(clean[KEY]) is int)
check("the refusal for 3651 names the range", "0 and 3650" in verdict(3651)[1][0])
check("the refusal for 1.5 says whole number (it is not quietly cut to 1)", "whole number" in verdict(1.5)[1][0])

# ---------------------------------------------------------------- who can write it
check("the key is in WRITABLE (dl_settings, which asks first)", KEY in S.WRITABLE)
check("the key is NOT in PREFS_WRITABLE (dl_prefs, which doesn't ask)", KEY not in S.PREFS_WRITABLE)
clean, problems = verdict(30, allowed=S.PREFS_WRITABLE)
check("validate_update with the dl_prefs list refuses it and says why", clean == {} and "dl_settings" in problems[0])
check("retention.auto_delete_files is not writable by any list", "retention.auto_delete_files" not in S.WRITABLE
      and "retention.auto_delete_files" not in S.PREFS_WRITABLE)
clean, problems = S.validate_update({"retention.auto_delete_files": True})
check("and an attempt to write it is refused", clean == {} and bool(problems))
clean, problems = S.validate_update({"retention": {"keep_history_days": 5, "auto_delete_files": True}})
check("a whole `retention` object can't carry it in either", clean == {} and bool(problems))

r = dh.tool_dl_prefs({"set": {KEY: 30}})
check("dl_prefs refuses the key through the real tool", r.get("ok") is False and "dl_settings" in str(r.get("error", r)))
check("...and nothing changed", saved() == 90)
r = dh.tool_dl_prefs({"set": {"retention.auto_delete_files": True}})
check("dl_prefs refuses auto_delete_files too", r.get("ok") is False)

r = dh.tool_dl_settings({"set": {KEY: 30}})
check("dl_settings takes the key", r.get("ok") is True)
check("...and reads it back", saved() == 30)
r = dh.tool_dl_settings({"set": {KEY: 3651}})
check("dl_settings refuses an out-of-range value and keeps the old one",
      r.get("ok") is False and saved() == 30)
r = dh.tool_dl_settings({"set": {KEY: 0}})
check("0 (keep everything) is accepted", r.get("ok") is True and saved() == 0)
check("the other retention key is untouched by all of this", service._store(None).get_settings()["retention"]["auto_delete_files"] is False)
dh.tool_dl_settings({"set": {KEY: 90}})

# ---------------------------------------------------------------- nothing else moved
check("the default is still 90 days and auto_delete_files False", S.DEFAULTS["retention"] == {"keep_history_days": 90, "auto_delete_files": False})
check("the default settings pass their own validator", verdict(S.DEFAULTS["retention"]["keep_history_days"])[1] == [])
check("exactly one retention key is writable", [k for k in S.WRITABLE if k.startswith("retention")] == [KEY])
check("no other setting changed list: dl_prefs still can't touch library_root or allow_private_hosts",
      "library_root" not in S.PREFS_WRITABLE and "allow_private_hosts" not in S.PREFS_WRITABLE
      and "library_root" in S.WRITABLE and "allow_private_hosts" in S.WRITABLE)
check("an existing validator is as before (concurrency.http 1..16)",
      S.validate_update({"concurrency.http": 16})[1] == [] and S.validate_update({"concurrency.http": 17})[1] != [])

# ---------------------------------------------------------------- pruning is as before, and follows the setting
DAY = 86400.0
store = Store(Path(HOME) / "prune.db")
now = 1_800_000_000.0
payload = Path(HOME) / "download.bin"
payload.write_bytes(b"data")


def final_row(title, age_days, state="done"):
    item = store.add_item(engine="http", source_url="http://93.184.216.34/" + title, title=title)[0]
    with store._write() as conn:           # place it directly: the lifecycle isn't what is tested here
        conn.execute("UPDATE items SET state=?, finished_at=?, updated_at=?, dest_file=? WHERE id=?",
                     (state, now - age_days * DAY, now - age_days * DAY, str(payload), item["id"]))
    return item["id"]


old_done, old_failed = final_row("old-done", 120), final_row("old-failed", 120, "failed")
young = final_row("young", 10)
live = store.add_item(engine="http", source_url="http://93.184.216.34/live", title="live")[0]["id"]
with store._write() as conn:
    conn.execute("UPDATE items SET created_at=?, updated_at=? WHERE id=?", (now - 500 * DAY, now - 500 * DAY, live))

check("0 keeps everything", store.prune_history(0, now) == 0 and store.get_item(old_done) is not None)
check("-1 keeps everything too (as before)", store.prune_history(-1, now) == 0)
removed = store.prune_history(90, now)
check("90 removes the two finished rows older than 90 days", removed == 2 and store.get_item(old_done) is None and store.get_item(old_failed) is None)
check("a younger row and a queued row stay", store.get_item(young) is not None and store.get_item(live) is not None)
check("pruning never touches a file", payload.exists() and payload.read_bytes() == b"data")

# the daemon takes the number from the saved setting (the path the Settings page writes)
from jarvis.hub.daemon import HubDaemon  # noqa: E402

store2 = Store(Path(HOME) / "prune2.db")
store2.set_settings({"retention": {"keep_history_days": 30, "auto_delete_files": False}})
item = store2.add_item(engine="http", source_url="http://93.184.216.34/x", title="x")[0]
with store2._write() as conn:
    conn.execute("UPDATE items SET state='done', finished_at=?, updated_at=? WHERE id=?", (now - 45 * DAY, now - 45 * DAY, item["id"]))
daemon = HubDaemon(store2, clock=lambda: now)
daemon._prune_history()
check("the daemon's pruning uses the saved number (30 days removes a 45-day-old row)", store2.get_item(item["id"]) is None)

# ---------------------------------------------------------------- part D: the qBittorrent file override (names only)
import io  # noqa: E402
import json  # noqa: E402

from jarvis import hub_admin  # noqa: E402
from jarvis.hub.engines import qbittorrent as qb  # noqa: E402

SECRETS = ("S3CRET-PASSWORD-77", "secret-user-name", "10.9.8.7", "9191", "movies-secret-category", "tag-secret-value", "SubfolderSecret")
cfg_file = qb.config_path()
cfg_file.parent.mkdir(parents=True, exist_ok=True)


def write_cfg(obj):
    cfg_file.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def op_settings():
    out = io.StringIO()
    hub_admin.run_cli(["settings"], stdin=io.StringIO("{}"), stdout=out)
    return json.loads(out.getvalue())


if cfg_file.exists():
    cfg_file.unlink()
check("no file: nothing is overridden", qb.file_overridden_keys() == [] and op_settings()["overridden_by"] == {})
check("the Settings answer is still a normal one (writable keys, defaults, providers)",
      op_settings()["ok"] is True and KEY in op_settings()["writable"] and "defaults" in op_settings())

write_cfg({"address": "http://10.9.8.7:9191", "username": "secret-user-name", "password": "S3CRET-PASSWORD-77"})
check("a file with only the connection (address, user, password) overrides no behaviour key", qb.file_overridden_keys() == [] and op_settings()["overridden_by"] == {})

write_cfg({"address": "http://10.9.8.7:9191", "username": "secret-user-name", "password": "S3CRET-PASSWORD-77",
           "category": "movies-secret-category", "tag": "tag-secret-value", "save_subfolder": "SubfolderSecret",
           "after_done": "pause", "poll_s": 5, "stall_minutes": 0, "metadata_timeout_s": 600, "cancel_deletes_data": True,
           "prefer_for_magnets": True})
names = qb.file_overridden_keys()
check("every behaviour key the file sets is named, in the engine's own order", names == [k for k in qb.SETTINGS_KEYS if k in names] and sorted(names) == sorted(qb.SETTINGS_KEYS))
admin = op_settings()
check("the hub_admin settings op reports each as overridden_by qbittorrent.json",
      admin["overridden_by"] == {"qbittorrent." + k: "qbittorrent.json" for k in qb.SETTINGS_KEYS})
check("the connection keys are never in overridden_by", not any(k in admin["overridden_by"] for k in ("qbittorrent.address", "qbittorrent.username", "qbittorrent.password")))
blob = json.dumps(admin) + json.dumps(names) + json.dumps(service.get_settings())
check("NO secret appears in the answer: no address, user name, password, or behaviour value from the file",
      not any(sec in blob for sec in SECRETS))
check("...nor in the payload function called directly", not any(sec in json.dumps(hub_admin.settings_payload()) for sec in SECRETS))

write_cfg({"category": "movies-secret-category", "password": "S3CRET-PASSWORD-77", "tag": 5, "poll_s": "fast", "after_done": "nonsense"})
check("a value the engine would not accept does not count as set (it follows load_config)", qb.file_overridden_keys() == ["category"])
write_cfg({"prefer_for_magnets": False, "stall_minutes": 0})
check("prefer_for_magnets false in the file is not an override (it is 'either says so'); stall_minutes 0 is", qb.file_overridden_keys() == ["stall_minutes"])
write_cfg({"prefer_for_magnets": True})
check("prefer_for_magnets true in the file is", qb.file_overridden_keys() == ["prefer_for_magnets"])
for label, text in (("not JSON", "{oops S3CRET-PASSWORD-77"), ("a list", "[1, 2]"), ("empty", ""), ("a string", '"S3CRET-PASSWORD-77"')):
    write_cfg(text)
    check("a file that is %s gives no names and no error" % label, qb.file_overridden_keys() == [] and op_settings()["overridden_by"] == {})
check("a missing file given by path gives no names", qb.file_overridden_keys(Path(HOME) / "nope.json") == [])

# precedence did not change: the file still wins over the saved settings
write_cfg({"address": "http://127.0.0.1:8080", "category": "from-file"})
loaded, problem = qb.load_config(settings={"qbittorrent": {"category": "from-settings", "tag": "from-settings"}})
check("precedence is unchanged: the file's category wins, a key the file lacks comes from the settings",
      problem is None and loaded["category"] == "from-file" and loaded["tag"] == "from-settings")
check("the function is read-only: the file is as it was", json.loads(cfg_file.read_text(encoding="utf-8")) == {"address": "http://127.0.0.1:8080", "category": "from-file"})
cfg_file.unlink()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
