"""Download Hub, brief 30: the Stremio card's account, player and addon-list back end.

Covers `addons.login` / `account_email` / `remote_logout` (a fake `net`; no real Stremio cloud), `config.save_account`,
`clear_account`, `check_player`, `check_addon_urls`, `addon_hosts`, `save_settings(player=, addon_urls=)` and the `hub_admin`
ops (`stremio_account`, the new blocks of `stremio_status`).  Temp HOME, nothing leaves this machine.

Run: python tests/test_hub_stremio_account.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-account-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT", "JARVIS_STREMIO_SERVER_URL"):
    os.environ.pop(var, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import hub_admin  # noqa: E402
from jarvis.hub.stremio import addons, config  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


def stored():
    return json.loads(config.config_path().read_text(encoding="utf-8"))


class FakeNet:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def request(self, method, url, label, **kw):
        self.calls.append((method, url, kw.get("json_body")))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


KEY = "abcDEF0123456789"
ok_reply = {"result": {"authKey": KEY, "user": {"email": "me@example.com"}}}

# ---- login
net = FakeNet(ok_reply)
check("login: returns the key and the email, posts to /login", addons.login(net, " me@example.com ", "hunter22") == (KEY, "me@example.com")
      and net.calls[0][1].endswith("/api/login") and net.calls[0][2]["type"] == "Login")
for email, pw in (("", "x"), ("nope", "x"), ("a@b.c", "")):
    try:
        addons.login(FakeNet(ok_reply), email, pw)
        check("login: refuses %r / %r before any call" % (email, pw), False)
    except addons.AddonError:
        check("login: refuses %r / %r before any call" % (email, pw), True)
try:
    addons.login(FakeNet({"error": {"message": "wrong hunter22 for me"}}), "me@example.com", "hunter22")
    check("login: a refusal is raised", False)
except addons.AddonError as exc:
    check("login: a refusal never contains the password", "hunter22" not in str(exc) and "***" in str(exc))
try:
    addons.login(FakeNet({"result": {"user": {}}}), "me@example.com", "hunter22")
    check("login: an answer without a key is an error", False)
except addons.AddonError:
    check("login: an answer without a key is an error", True)

# ---- save / clear the account
check("no account at first", config.account_state() == {"signed_in": False, "email": ""})
check("save_account refuses a key with odd characters", not config.save_account("bad key!!")["ok"] and not config.config_path().exists())
check("save_account saves the key and the email", config.save_account(KEY, "me@example.com")["ok"] and stored()["account"] == {"auth_key": KEY, "email": "me@example.com"})
state = config.account_state()
check("account_state never carries the key", state == {"signed_in": True, "email": "me@example.com"} and KEY not in json.dumps(state))
raw = stored()
raw["prefs"] = {"keep": 1}
config.config_path().write_text(json.dumps(raw), encoding="utf-8")
check("clear_account removes only the key and email", config.clear_account()["ok"] and stored()["account"] == {} and stored()["prefs"] == {"keep": 1}
      and not config.account_state()["signed_in"])
config.config_path().write_text("{not json", encoding="utf-8")
check("a file that isn't JSON is never overwritten by the account", not config.save_account(KEY)["ok"] and config.config_path().read_text(encoding="utf-8") == "{not json")
config.config_path().unlink()

# ---- player
exe = Path(HOME) / "mpv.exe"
exe.write_text("x", encoding="utf-8")
check("check_player: kind and an existing program", config.check_player({"kind": "MPV", "exe": str(exe)})[0] == {"kind": "mpv", "exe": str(exe)})
check("check_player: refuses an unknown kind, a missing file, a .js and an unknown key",
      all(config.check_player(p)[1] for p in ({"kind": "winamp"}, {"exe": str(Path(HOME) / "gone.exe")}, {"exe": str(exe.with_suffix(".js"))}, {"zzz": 1})))
check("check_player: an empty program clears it", config.check_player({"exe": ""})[0] == {"exe": ""})
check("save_settings(player=) writes the player block", config.save_settings(player={"kind": "vlc", "exe": str(exe)})["ok"] and stored()["player"] == {"kind": "vlc", "exe": str(exe)}
      and config.player_prefs() == ("vlc", str(exe)))
check("save_settings(player=) refuses a bad kind and writes nothing", not config.save_settings(player={"kind": "x"})["ok"] and stored()["player"]["kind"] == "vlc")

# ---- addon list
secret = "https://debrid.example/SECRETKEY/manifest.json"
check("addon list: add saves it, a bad address saves nothing", config.save_settings(addon_urls={"add": [secret], "remove": []})["ok"]
      and not config.save_settings(addon_urls={"add": ["ftp://x", "javascript:1", "http://a b/"], "remove": []})["ok"] and stored()["addons"]["urls"] == [secret])
check("addon_hosts shows the host and position, never the path", config.addon_hosts() == [{"index": 0, "host": "debrid.example"}] and "SECRETKEY" not in json.dumps(config.addon_hosts()))
check("addon list: adding the same address twice keeps one", config.save_settings(addon_urls={"add": [secret], "remove": []})["ok"] and len(stored()["addons"]["urls"]) == 1)
check("addon list: a position that isn't there is refused", not config.save_settings(addon_urls={"add": [], "remove": [5]})["ok"])
check("addon list: remove by position", config.save_settings(addon_urls={"add": [], "remove": [0]})["ok"] and stored()["addons"]["urls"] == [])
check("addon list: more than the cap is refused", not config.save_settings(addon_urls={"add": ["https://h%d.example/m.json" % i for i in range(31)], "remove": []})["ok"])

# ---- hub_admin
status = hub_admin.stremio_status({})
check("status carries account, player_config and addon_hosts, and no secret", status.get("account") == {"signed_in": False, "email": ""}
      and status["player_config"]["kind"] == "vlc" and status["addon_hosts"] == [] and KEY not in json.dumps(status))
real_login, real_net = addons.login, addons.Net
addons.Net = lambda *a, **k: FakeNet(ok_reply)
out = hub_admin.stremio_account({"action": "login", "email": "me@example.com", "password": "hunter22"})
check("stremio_account login: saves the key, answers the status, never the key or the password",
      out.get("ok") is not False and out["account"]["signed_in"] and "me@example.com" in out["account_note"]
      and KEY not in json.dumps(out) and "hunter22" not in json.dumps(out) and "hunter22" not in config.config_path().read_text(encoding="utf-8"))
out = hub_admin.stremio_account({"action": "check"})
check("stremio_account check: names the account", out.get("ok") is not False and "me@example.com" in out["account_note"])
out = hub_admin.stremio_account({"action": "logout"})
check("stremio_account logout: forgets the key", out.get("ok") is not False and not out["account"]["signed_in"] and "auth_key" not in stored()["account"])
check("stremio_account: unknown action and check without an account are refused", hub_admin.stremio_account({"action": "x"})["ok"] is False
      and hub_admin.stremio_account({"action": "check"})["ok"] is False)
out = hub_admin.stremio_account({"action": "key", "auth_key": KEY})
check("stremio_account key: saves a pasted key", out.get("ok") is not False and out["account"]["signed_in"])
addons.Net = real_net
os.environ["JARVIS_CONTEXT"] = "scheduled"
check("an unattended process is refused for every account action", all(hub_admin.stremio_account({"action": a, "email": "a@b.c", "password": "x", "auth_key": KEY})["ok"] is False
      for a in ("login", "key", "check", "logout")))
os.environ.pop("JARVIS_CONTEXT")
check("stremio_account is a write op, so the read routes can't reach it", "stremio_account" in hub_admin.WRITE_OPS and "stremio_account" not in hub_admin.READ_OPS)

# ---- no model tool can touch the account
tools = "\n".join(py.read_text(encoding="utf-8", errors="replace") for py in (ROOT / "jarvis-cli" / "jarvis" / "actions").glob("*.py"))
check("no tool file names save_account, clear_account or addons.login", not any(n in tools for n in ("save_account", "clear_account", "addons.login", "A.login(")))
check("server.js sends the password only to the account route", (ROOT / "web" / "server.js").read_text(encoding="utf-8").count("req.body?.password") == 1)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
