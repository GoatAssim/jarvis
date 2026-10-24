"""Download Hub: the qBittorrent CONNECTION form of Settings > Downloads > Sources > qBittorrent.

The form edits ~/.jarvis/qbittorrent.json: the on/off switch, the Web UI address, the user name and the password.  The behaviour
options are the hub's own settings rows (tests/test_hub_qbittorrent_settings.py) and are never written to this file by the form.

Covers: the read (defaults, a password never returned, the file's own problem named, the behaviour keys the file also holds);
every value the form may send, accepted or refused with a sentence that names the field; a refused save writes nothing; only the
keys sent change and keys the form doesn't know stay; the password is written, kept, forgotten; release_settings removes the
behaviour keys and nothing else; a broken file is refused and left alone; the engine's cached availability is dropped by a save;
the two hub_admin ops (the status is readable by anyone, the save is refused for an unattended process); no answer carries the
password.

Run: python tests/test_hub_qbittorrent_connection.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-qbc-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import hub_admin  # noqa: E402
from jarvis.hub.engines import qbittorrent as qbe  # noqa: E402

passed = failed = 0
SECRET = "hunter2-SECRET-pw"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def target():
    return qbe.config_path()


def read_file():
    return json.loads(target().read_text(encoding="utf-8"))


def write_file(data):
    target().parent.mkdir(parents=True, exist_ok=True)
    target().write_text(json.dumps(data), encoding="utf-8")


def reset():
    if target().exists():
        target().unlink()
    qbe.ENGINE._avail = None


def test_read():
    reset()
    st = qbe.connection_state()
    check("no file: the form reads the defaults, the switch on, and no password",
          st["exists"] is False and st["values"] == {"enabled": True, "address": "http://127.0.0.1:8080", "username": ""}
          and st["password_set"] is False and st["file_overrides"] == [] and st["problem"] is None)
    write_file({"address": "http://localhost:9090", "username": "me", "password": SECRET, "enabled": False})
    st = qbe.connection_state()
    check("a saved file is read as it is, the switch off", st["values"] == {"enabled": False, "address": "http://localhost:9090", "username": "me"})
    check("a saved password shows as saved and appears nowhere in the read", st["password_set"] is True and SECRET not in json.dumps(st))
    write_file({"address": "http://127.0.0.1:8080", "tag": "mine", "poll_s": 2, "cancel_deletes_data": False})
    st = qbe.connection_state()
    check("behaviour keys the file also holds are named (the file wins over the settings rows), in the engine's order",
          st["file_overrides"] == [k for k in qbe.SETTINGS_KEYS if k in ("tag", "poll_s", "cancel_deletes_data")])
    target().write_text("{not json", encoding="utf-8")
    st = qbe.connection_state()
    check("a file that isn't JSON is named, and the values fall back to the defaults",
          st["problem"] and "isn't readable JSON" in st["problem"] and st["values"]["address"] == "http://127.0.0.1:8080")
    target().write_text("[1]", encoding="utf-8")
    check("a file that is JSON but not an object is named", "one JSON object" in (qbe.connection_state()["problem"] or ""))


def test_saves():
    reset()
    res = qbe.save_connection({"address": " http://127.0.0.1:8080 ", "username": " admin ", "enabled": True})
    check("a valid connection is saved", res.get("ok") is True and res["saved"] == ["address", "enabled", "username"])
    data = read_file()
    check("values are stored stripped, and nothing else is written (no behaviour key, no password)",
          data == {"address": "http://127.0.0.1:8080", "username": "admin", "enabled": True})
    check("the file is owner-only where the OS allows it", os.name == "nt" or (target().stat().st_mode & 0o777) == 0o600)
    check("the engine now reads it", qbe.load_config()[1] is None and qbe.load_config()[0]["username"] == "admin")

    write_file({"address": "http://127.0.0.1:8080", "tag": "mine", "future_key": "keep me"})
    qbe.save_connection({"username": "someone"})
    data = read_file()
    check("saving one key changes that key only: the others, known or not, stay", data["username"] == "someone" and data["tag"] == "mine" and data["future_key"] == "keep me")

    qbe.save_connection({"enabled": False})
    check("the switch is stored as off, and the engine says so", read_file()["enabled"] is False and "switched off" in qbe.load_config()[1])
    qbe.save_connection({"enabled": True})
    check("and on again", qbe.load_config()[1] is None)

    qbe.save_connection({"username": ""})
    check("an empty user name is allowed (qBittorrent may bypass authentication on localhost)", read_file()["username"] == "")


def test_refusals():
    reset()
    qbe.save_connection({"username": "before"})
    before = target().read_text(encoding="utf-8")
    cases = [
        ({"address": "http://192.168.1.20:8080"}, "a Web UI on another PC is refused", "127.0.0.1"),
        ({"address": "http://user:pw@127.0.0.1:8080"}, "credentials inside the address are refused", "own fields"),
        ({"address": "ftp://127.0.0.1"}, "a non-http address is refused", "http"),
        ({"address": ""}, "an empty address is refused", ""),
        ({"address": 5}, "an address that isn't text is refused", "must be text"),
        ({"address": "http://127.0.0.1:8080/" + "x" * 400}, "an address that is too long is refused", "too long"),
        ({"username": 12}, "a user name that isn't text is refused", "must be text"),
        ({"enabled": "yes"}, "the switch sent as text is refused", "on or off"),
        ({"password": "x" * 201}, "a password over 200 characters is refused", "at most 200"),
        ({"password": 5}, "a password that isn't text is refused", "must be text"),
        ({"clear_password": "yes"}, "clear_password must be on or off", "on or off"),
        ({"release_settings": 1}, "release_settings must be on or off", "on or off"),
        ({"tag": "x"}, "a behaviour key is not part of the connection form (it would override the settings rows)", "isn't a qBittorrent connection setting"),
        ({"surprise": 1}, "an unknown key is refused, never ignored", "isn't a qBittorrent connection setting"),
    ]
    for values, name, expect in cases:
        res = qbe.save_connection(values)
        check(name, res.get("ok") is False and expect in (res.get("error") or ""))
    check("a refused save writes nothing", target().read_text(encoding="utf-8") == before)
    res = qbe.save_connection({"address": "http://10.0.0.5:8080", "username": "changed"})
    check("one bad value refuses the whole form: the good one is not written either", res.get("ok") is False and read_file()["username"] == "before")
    check("a refused address is named by the engine's own sentence (an address is not a secret); a refused password is never repeated",
          "10.0.0.5" in res["error"] and SECRET not in json.dumps(qbe.save_connection({"password": SECRET * 20})))
    check("an empty form is nothing to save", qbe.save_connection({}).get("error") == "nothing to save")
    check("a body that isn't an object is refused", qbe.save_connection([1]).get("ok") is False)
    target().write_text("[1, 2, 3]", encoding="utf-8")
    res = qbe.save_connection({"username": "x"})
    check("a file that is JSON but not an object is refused and left as it is",
          res.get("ok") is False and "one JSON object" in res["error"] and target().read_text(encoding="utf-8") == "[1, 2, 3]")
    target().write_text("{broken", encoding="utf-8")
    res = qbe.save_connection({"username": "x"})
    check("an unreadable file is refused and left as it is", res.get("ok") is False and "isn't readable JSON" in res["error"] and target().read_text(encoding="utf-8") == "{broken")


def test_password_and_release():
    reset()
    qbe.save_connection({"address": "http://127.0.0.1:8080", "password": SECRET})
    check("a typed password is written to the file", read_file()["password"] == SECRET)
    res = qbe.save_connection({"password": ""})
    check("an empty password on its own is nothing to save (the form never sends one)", res.get("error") == "nothing to save" and read_file()["password"] == SECRET)
    qbe.save_connection({"username": "admin"})
    check("a save that doesn't mention the password leaves it alone", read_file()["password"] == SECRET)
    res = qbe.save_connection({"clear_password": True})
    check("clear_password forgets it, and the form says none is saved",
          res.get("ok") is True and "password" in res["saved"] and read_file()["password"] == "" and qbe.connection_state()["password_set"] is False)
    check("a password with spaces keeps them", qbe.save_connection({"password": " a b "}).get("ok") is True and read_file()["password"] == " a b ")
    check("no save result carries the password", SECRET not in json.dumps(qbe.save_connection({"password": SECRET})))

    write_file({"address": "http://127.0.0.1:8080", "username": "u", "password": SECRET, "tag": "mine", "poll_s": 3, "after_done": "pause", "future_key": 1})
    res = qbe.save_connection({"release_settings": True})
    data = read_file()
    check("release_settings removes the behaviour keys and nothing else",
          res.get("ok") is True and res["saved"] == ["after_done", "poll_s", "tag"]
          and data == {"address": "http://127.0.0.1:8080", "username": "u", "password": SECRET, "future_key": 1})
    check("after it the form names no override", qbe.connection_state()["file_overrides"] == [])
    check("release with nothing to release is nothing to save", qbe.save_connection({"release_settings": True}).get("error") == "nothing to save")


def test_availability_refreshes():
    reset()
    qbe.save_connection({"address": "http://127.0.0.1:8080"})
    qbe.ENGINE._avail = (10 ** 9, None, (True, ""))             # a stale "connected" answer from before the save
    qbe.save_connection({"address": "http://127.0.0.1:8081"})
    check("a save drops the engine's cached availability, so the next check reads the new file", qbe.ENGINE._avail is None)


def test_hub_admin_ops():
    reset()
    original = qbe.ENGINE.available
    qbe.ENGINE.available = lambda: (False, "can't reach qBittorrent at http://127.0.0.1:8080 (is it running?)")
    try:
        st = hub_admin.admin("qbittorrent_status", {})
        check("the status op answers ok with the engine's own reason", st.get("ok") is True and st["engine"]["ok"] is False and "can't reach" in st["engine"]["reason"])
        check("the status op never shows a host:port in the reason, nor the home folder", "127.0.0.1:8080" not in st["engine"]["reason"] and HOME not in json.dumps(st))
        check("the status op shows the connection file with ~", st["file"].startswith("~"))

        res = hub_admin.admin("qbittorrent_save", {"username": "owner", "password": SECRET})
        check("the save op writes through the validator and answers the status, with the keys it saved",
              res.get("ok") is True and res["saved"] == ["password", "username"] and res["password_set"] is True and res["values"]["username"] == "owner")
        check("the save op never sends the password back", SECRET not in json.dumps(res))
        check("the file holds the password", read_file()["password"] == SECRET)
        bad = hub_admin.admin("qbittorrent_save", {"address": "http://10.0.0.5:8080"})
        check("a refusal is a sentence plus the list of problems", bad.get("ok") is False and bad.get("problems") and SECRET not in json.dumps(bad))
        check("a body that isn't an object is nothing to save, not a traceback", hub_admin.admin("qbittorrent_save", []).get("ok") is False)

        for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
            os.environ[var] = "1"
            try:
                refused = hub_admin.admin("qbittorrent_save", {"username": "nobody"})
                check("%s: an unattended process is refused the save, and nothing changes" % var, refused.get("ok") is False and read_file()["username"] == "owner")
                check("%s: the status is still readable" % var, hub_admin.admin("qbittorrent_status", {}).get("ok") is True)
            finally:
                del os.environ[var]

        check("the save is a write op and the status a read op", "qbittorrent_save" in hub_admin.WRITE_OPS and "qbittorrent_save" not in hub_admin.READ_OPS
              and "qbittorrent_status" in hub_admin.READ_OPS and "qbittorrent_status" not in hub_admin.WRITE_OPS)
    finally:
        qbe.ENGINE.available = original

    # the server only passes on the form's own keys
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("the routes: status is a read route, save a guarded write route that passes only the form's keys",
          'app.get("/api/hub/qbittorrent", requireHubRead, requireJarvis' in server and 'app.post("/api/hub/qbittorrent", requireHubWrite, requireJarvis' in server
          and "function hubQbtForm" in server and "hubQbtForm(req.body)" in server)


try:
    test_read()
    test_saves()
    test_refusals()
    test_password_and_release()
    test_availability_refreshes()
    test_hub_admin_ops()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
