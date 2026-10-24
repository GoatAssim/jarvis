"""Download Hub, brief 29 part 1 (N18 second half, N22 part e): the Stremio configuration back end.

Covers `hub/stremio/config.py` (`check_web`, `save_settings`, `file_state`, `repair`) and the `hub_admin` ops that carry them
(`stremio_save` with a `web` block, a standalone address and `repair`; `stremio_start` with the literal "web"; the new
`web_config`, `cache` and `config` blocks of `stremio_status`).  Everything runs against a temp HOME, the loopback fake Stremio
server and a patched `tool_stack_start`: no real Stremio, no real process, nothing leaves this machine.

Run: python tests/test_hub_stremio_config.py
"""
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-config-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT", "JARVIS_STREMIO_SERVER_URL"):
    os.environ.pop(var, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_stremio as fake  # noqa: E402
from jarvis import hub_admin  # noqa: E402
from jarvis.hub.stremio import config, detect  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


REAL = Path(HOME) / "Stremio-Custom"
REAL.mkdir()
(REAL / "stremio-runtime.exe").write_bytes(b"")
(REAL / "server.js").write_bytes(b"")
EXE, JS = str(REAL / "stremio-runtime.exe"), str(REAL / "server.js")
WEB = Path(HOME) / "stremio-web"
WEB.mkdir()
FAKE, BASE, ST = fake.start()
cfg_path = config.config_path()
cfg_path.parent.mkdir(parents=True, exist_ok=True)
SECRET = {"account": {"auth_key": "SECRETKEY"}, "player": {"x": 1}}

# a machine on which detection finds exactly the pair in REAL (and the file reader fakes everything else out)
FOLDERS = [str(REAL)]
detect.list_processes = lambda: []
detect.read_uninstall_entries = lambda: []
detect.read_start_menu_shortcuts = lambda *a, **k: []
detect.fixed_folders = lambda *a, **k: list(FOLDERS)
detect.listening_ports = lambda pids: []


def write(data):
    cfg_path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def read():
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def reset():
    if cfg_path.exists():
        cfg_path.unlink()
    for extra in cfg_path.parent.glob("stremio.json.bad*"):
        extra.unlink()


# ===================================================================== check_web
clean, probs = config.check_web({"url": "http://localhost:8080/", "dir": str(WEB), "start_cmd": "pnpm start"})
check("check_web: a loopback address, an existing folder and a plain command are accepted", not probs and clean["dir"] == str(WEB)
      and clean["start_cmd"] == "pnpm start" and clean["url"].startswith("http://localhost:8080"))
clean, probs = config.check_web({"url": "http://example.com:8080"})
check("check_web: a non-loopback web address is refused, in words", probs and "127.0.0.1" in probs[0] and "url" not in clean)
clean, probs = config.check_web({"dir": str(WEB / "nope")})
check("check_web: a folder that doesn't exist is refused and named", probs and "doesn't exist" in probs[0])
for cmd in ("pnpm start && calc", "a | b", "x; y", "x > f", "x `y`", "x $HOME", "x %PATH%", "x\ny"):
    clean, probs = config.check_web({"start_cmd": cmd})
    check("check_web: the start command %r is refused" % cmd, probs and "start_cmd" not in clean)
clean, probs = config.check_web({"start_cmd": "x" * 201})
check("check_web: a command over 200 characters is refused", probs and "200" in probs[0])
clean, probs = config.check_web({"start_cmd": "   "})
check("check_web: an empty command means the default one", not probs and clean["start_cmd"] == config.DEFAULTS["web"]["start_cmd"])
clean, probs = config.check_web({"dir": ""})
check("check_web: an empty folder is allowed (turns stremio-web off)", not probs and clean == {"dir": ""})
clean, probs = config.check_web({"password": "x"})
check("check_web: a key that isn't a stremio-web setting is refused", probs and "password" in probs[0])
check("check_web: a non-object is refused", config.check_web("x")[1] != [])

# ===================================================================== save_settings
reset()
res = config.save_settings(web={"dir": str(WEB), "start_cmd": "npm start", "url": "http://127.0.0.1:8081"})
check("save_settings: the web block is saved to a new file", res["ok"] and read()["web"] == {"dir": str(WEB), "start_cmd": "npm start", "url": "http://127.0.0.1:8081/"}
      or (res["ok"] and read()["web"]["dir"] == str(WEB) and read()["web"]["start_cmd"] == "npm start"))
write(dict(SECRET, server={"runtime_exe": EXE, "server_js": JS}, web={"dir": "keepme", "other": "x"}, env={"A": "B"}, unknown=7))
res = config.save_settings(web={"start_cmd": "yarn dev"})
data = read()
check("save_settings: only the named key changes; every other key (account key, server paths, env, unknown) is carried over",
      res["ok"] and data["web"] == {"dir": "keepme", "other": "x", "start_cmd": "yarn dev"} and data["account"] == SECRET["account"]
      and data["server"] == {"runtime_exe": EXE, "server_js": JS} and data["env"] == {"A": "B"} and data["unknown"] == 7)
check("save_settings: the answer never carries the account key", "SECRETKEY" not in json.dumps(res))
res = config.save_settings(web={"dir": str(WEB / "nope")})
check("save_settings: a bad folder is refused and the file is unchanged", not res["ok"] and "doesn't exist" in res["error"] and read()["web"]["dir"] == "keepme")
res = config.save_settings(server_url=BASE)
check("save_settings: a loopback address that answers like Stremio is saved", res["ok"] and read()["server"]["url"] == BASE and read()["server"]["runtime_exe"] == EXE)
res = config.save_settings(server_url="http://example.com:11470")
check("save_settings: a non-loopback server address is refused", not res["ok"] and "127.0.0.1" in res["error"])
res = config.save_settings(server_url="http://127.0.0.1:%d" % free_port())
check("save_settings: an address nothing answers at is not saved", not res["ok"] and "nothing answers" in res["error"] and read()["server"]["url"] == BASE)
check("save_settings: nothing to save is an error", not config.save_settings()["ok"] and not config.save_settings(web={})["ok"])
write("{not json")
res = config.save_settings(web={"start_cmd": "x"})
check("save_settings: a file that isn't JSON is left alone, not overwritten", not res["ok"] and cfg_path.read_text() == "{not json")
write({"server": "oops"})
check("save_settings: a file with a non-object 'server' is left alone", not config.save_settings(web={"start_cmd": "x"})["ok"])
for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
    reset()
    os.environ[var] = "1"
    res = config.save_settings(web={"start_cmd": "x"})
    del os.environ[var]
    check("save_settings: refused for an unattended process (%s) and writes nothing" % var, not res["ok"] and res.get("code") == "unattended" and not cfg_path.exists())

# ===================================================================== file_state
reset()
check("file_state: no file -> missing", config.file_state()["state"] == "missing")
write("{nope")
check("file_state: not JSON -> broken, and the sentence names the file", config.file_state()["state"] == "broken" and str(cfg_path) in config.file_state()["sentence"])
write({"server": {"runtime_exe": EXE}})
check("file_state: one of two paths -> partial", config.file_state()["state"] == "partial")
write({"server": {"runtime_exe": str(REAL / "gone.exe"), "server_js": JS}})
check("file_state: a saved path that is gone -> stale", config.file_state()["state"] == "stale")
write({"web": {"dir": str(WEB)}})
check("file_state: a file with no Stremio paths -> no_paths", config.file_state()["state"] == "no_paths")
write({"server": {"runtime_exe": EXE, "server_js": JS}})
check("file_state: a good pair -> ok", config.file_state()["state"] == "ok" and config.file_state()["sentence"] == "")

# ===================================================================== repair
reset()
res = config.repair()
check("repair: a missing file is written with the pair detection found", res["ok"] and res["repaired"] == "missing" and read()["server"] == {"runtime_exe": EXE, "server_js": JS})
check("repair: a file that is fine is not touched", not config.repair()["ok"] and "nothing to repair" in config.repair()["error"])
write(dict(SECRET, server={"runtime_exe": str(REAL / "gone.exe"), "server_js": JS}, web={"dir": str(WEB)}))
res = config.repair()
data = read()
check("repair: a stale pair is replaced and every other key (account key, web) is kept",
      res["ok"] and res["repaired"] == "stale" and data["server"]["runtime_exe"] == EXE and data["account"] == SECRET["account"] and data["web"] == {"dir": str(WEB)})
check("repair: the answer never carries the account key", "SECRETKEY" not in json.dumps(res))
write("{broken json ]")
res = config.repair()
check("repair: a file that isn't JSON is NOT touched without keep_copy, and the answer says what to agree to",
      not res["ok"] and res.get("code") == "needs_copy" and cfg_path.read_text() == "{broken json ]")
res = config.repair(keep_copy=True)
bad = cfg_path.parent / "stremio.json.bad"
check("repair: with keep_copy the unreadable file is MOVED to stremio.json.bad (kept, not deleted) and a fresh file is written",
      res["ok"] and res["kept_copy"] == str(bad) and bad.read_text() == "{broken json ]" and read()["server"]["runtime_exe"] == EXE)
write("{broken again")
res = config.repair(keep_copy=True)
check("repair: a second copy never overwrites the first", res["ok"] and (cfg_path.parent / "stremio.json.bad2").exists() and bad.read_text() == "{broken json ]")
reset()
FOLDERS[:] = []
res = config.repair()
check("repair: when Stremio is not found nothing is written", not res["ok"] and "couldn't find Stremio" in res["error"] and not cfg_path.exists())
write("{broken")
res = config.repair(keep_copy=True)
check("repair: a broken file is not moved aside when there is nothing to save", not res["ok"] and cfg_path.read_text() == "{broken" and not (cfg_path.parent / "stremio.json.bad").exists())
FOLDERS[:] = [str(REAL)]
reset()
os.environ["JARVIS_SCHEDULED"] = "1"
res = config.repair()
os.environ.pop("JARVIS_SCHEDULED")
check("repair: refused for an unattended process and writes nothing", not res["ok"] and res.get("code") == "unattended" and not cfg_path.exists())

# ===================================================================== hub_admin: stremio_save
reset()
out = hub_admin.admin("stremio_save", {"web": {"dir": str(WEB), "start_cmd": "pnpm start"}})
check("hub-admin stremio_save: a web block alone saves, then answers with the fresh status (web_config shows it)",
      out["ok"] and out["saved"]["web"]["dir"] == str(WEB) and out["web_config"]["dir"] == str(WEB) and out["web_config"]["dir_exists"] is True
      and out["web_config"]["start_cmd"] == "pnpm start" and out["web"]["configured"] is True)
out = hub_admin.admin("stremio_save", {"web": {"start_cmd": "a && b"}})
check("hub-admin stremio_save: a refused web setting is ok: false with the hub's sentence and the problems", not out["ok"] and "can't contain" in out["error"] and out.get("problems"))
out = hub_admin.admin("stremio_save", {"web": {"start_cmd": "a && b"}, "dry_run": True})
check("hub-admin stremio_save dry_run on the web block: checks only, writes nothing", not out["ok"] and out["dry_run"] is True and read()["web"]["start_cmd"] == "pnpm start")
out = hub_admin.admin("stremio_save", {"web": {"start_cmd": "ok go"}, "dry_run": True})
check("hub-admin stremio_save dry_run: a good web block is ok and unchanged on disk", out["ok"] and out["dry_run"] and read()["web"]["start_cmd"] == "pnpm start")
out = hub_admin.admin("stremio_save", {"server_url": BASE})
check("hub-admin stremio_save: the server address alone is saved (no paths needed)", out["ok"] and read()["server"]["url"] == BASE)
out = hub_admin.admin("stremio_save", {})
check("hub-admin stremio_save: an empty request is still the old refusal", not out["ok"])
out = hub_admin.admin("stremio_save", {"web": "not an object"})
check("hub-admin stremio_save: a web value that isn't an object is ignored, not saved", not out["ok"])
reset()
out = hub_admin.admin("stremio_save", {"repair": True})
check("hub-admin stremio_save repair: writes the detected pair and says what it repaired", out["ok"] and out["repaired"] == "missing" and read()["server"]["server_js"] == JS)
write("{broken")
out = hub_admin.admin("stremio_save", {"repair": True})
check("hub-admin stremio_save repair: a broken file needs keep_copy and the code says so", not out["ok"] and out.get("code") == "needs_copy")
out = hub_admin.admin("stremio_save", {"repair": True, "keep_copy": True})
check("hub-admin stremio_save repair + keep_copy: the old file is kept and the answer names it", out["ok"] and out["kept_copy"].endswith("stremio.json.bad"))
for var in ("JARVIS_CHANNEL", "JARVIS_SCHEDULED"):
    reset()
    os.environ[var] = "1"
    a = hub_admin.admin("stremio_save", {"web": {"start_cmd": "x"}})
    b = hub_admin.admin("stremio_save", {"repair": True})
    del os.environ[var]
    check("hub-admin: web save and repair are refused when %s is set, and nothing is written" % var,
          not a["ok"] and not b["ok"] and a.get("code") == "unattended" and b.get("code") == "unattended" and not cfg_path.exists())

# ===================================================================== hub_admin: the new status blocks
reset()
snap_found = {"path": {"found": True, "runtime_exe": EXE, "server_js": JS, "where": "folder"}}
block = hub_admin._stremio_config_block(snap_found)
check("status config block: a missing file with Stremio found offers a repair with the pair", block["state"] == "missing" and block["can_repair"] is True
      and block["repair_pair"]["runtime_exe"] == EXE and block["repair_needs_copy"] is False)
write("{broken")
block = hub_admin._stremio_config_block(snap_found)
check("status config block: a broken file offers the repair only with keep_copy flagged", block["state"] == "broken" and block["can_repair"] and block["repair_needs_copy"] is True)
block = hub_admin._stremio_config_block({"path": {"found": False}})
check("status config block: nothing found -> no button (the sentence still says what is wrong)", block["can_repair"] is False and block["sentence"])
write({"server": {"runtime_exe": EXE, "server_js": JS}})
block = hub_admin._stremio_config_block({"path": {"found": True, "runtime_exe": EXE, "server_js": JS, "where": "config"}})
check("status config block: a good file offers nothing", block["state"] == "ok" and block["can_repair"] is False and "repair_pair" not in block)
cache = hub_admin._stremio_cache({"state": "not_running"})
check("status cache block: a server that isn't running -> not known, with a sentence", cache["known"] is False and "running server" in cache["note"])
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
ST.settings["cacheRoot"] = str(Path(HOME) / "cache-here")
ST.settings["cacheSize"] = 4294967296
cache = hub_admin._stremio_cache({"state": "running"})
check("status cache block: a running server's cacheRoot and size are reported", cache["known"] is True and cache["root"].endswith("cache-here") and cache["size_bytes"] == 4294967296)
ST.settings["cacheRoot"] = ""
check("status cache block: a server that doesn't say -> not known", hub_admin._stremio_cache({"state": "running"})["known"] is False)
os.environ.pop("JARVIS_STREMIO_SERVER_URL")
web_cfg = hub_admin._stremio_web_config()
check("web_config: carries the defaults the card shows next to each box", web_cfg["default_url"] == config.DEFAULT_WEB_URL and web_cfg["default_start_cmd"] == "pnpm start")
check("status: stremio_status carries web_config, cache and config, and no secret", all(k in hub_admin.admin("stremio_status", {}) for k in ("web_config", "cache", "config"))
      and "SECRETKEY" not in json.dumps(hub_admin.admin("stremio_status", {})))

# ===================================================================== hub_admin: stremio_start with "web"
from jarvis.actions import stremio_stack  # noqa: E402
real_start = stremio_stack.tool_stack_start
calls = []


def fake_start(answer):
    def fn(args):
        calls.append(dict(args))
        return answer
    return fn


try:
    stremio_stack.tool_stack_start = fake_start({"ok": True, "web": {"state": "started", "url": "http://127.0.0.1:8080"}})
    out = hub_admin.admin("stremio_start", {"what": "web"})
    check("stremio_start web: calls tool_stack_start for web only", calls == [{"what": "web"}] and out["ok"] is True and out["state"] == "started" and "running" in out["sentence"])
    stremio_stack.tool_stack_start = fake_start({"ok": True, "web": {"state": "already_up", "url": "http://127.0.0.1:8080"}})
    out = hub_admin.admin("stremio_start", {"what": "web"})
    check("stremio_start web: already up is ok and starts nothing", out["ok"] and out["state"] == "already_up" and "already" in out["sentence"])
    stremio_stack.tool_stack_start = fake_start({"ok": True, "web": {"state": "not_configured", "note": "set web.dir first"}})
    out = hub_admin.admin("stremio_start", {"what": "web"})
    check("stremio_start web: not configured is ok: false with the note as the error", out["ok"] is False and "web.dir" in out["error"])
    stremio_stack.tool_stack_start = fake_start({"ok": False, "error": "no node", "web": {"state": "error", "error": "no node"}})
    check("stremio_start web: an error is ok: false with its text", hub_admin.admin("stremio_start", {"what": "web"})["error"] == "no node")
    calls.clear()
    stremio_stack.tool_stack_start = fake_start({"ok": True, "server": {"state": "started", "sentence": "up"}})
    for body in ({}, {"what": "server"}, {"what": "both"}, {"what": "WEB"}, {"what": ["web"]}, None):
        hub_admin.admin("stremio_start", body if body is not None else {})
    check("stremio_start: anything but the literal \"web\" starts the server only", calls and all(c == {"what": "server"} for c in calls))
finally:
    stremio_stack.tool_stack_start = real_start

# ===================================================================== the route and the source guards
server_js = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
check("server.js: the paths route passes web (three string keys), repair and keep_copy; nothing else new",
      "hubStremioWeb(req.body?.web)" in server_js and "repair: req.body?.repair === true" in server_js and "keep_copy: req.body?.keep_copy === true" in server_js
      and 'for (const key of ["url", "dir", "start_cmd"])' in server_js)
config_src = (ROOT / "jarvis-cli" / "jarvis" / "hub" / "stremio" / "config.py").read_text(encoding="utf-8")
check("no model tool was added: nothing under actions/ names the new functions",
      not any(name in py.read_text(encoding="utf-8", errors="replace") for py in (ROOT / "jarvis-cli" / "jarvis" / "actions").glob("*.py")
              for name in ("save_settings", "check_web", "config.repair")))
check("config.py is still the one validator: hub_admin has no loopback or path check of its own",
      "_loopback_url" not in (ROOT / "jarvis-cli" / "jarvis" / "hub_admin.py").read_text(encoding="utf-8") and "def check_web" in config_src)

fake.stop(FAKE) if hasattr(fake, "stop") else None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
