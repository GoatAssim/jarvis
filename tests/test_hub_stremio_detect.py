"""Download Hub, brief 12 / N9 steps 1 and 2: finding Stremio without setup, and saying what is wrong.

Step 1, `hub/stremio/detect.py`: every probe is fed FAKE inputs (process lists, registry values, a
hand-built .lnk file, folder trees), so no real Stremio, registry or Start Menu is needed.
Step 2: `hub/stremio/status.py` (one test per state sentence), the reworked stack_start / stack_status /
stack_doctor against a fake server and a fake daemon layer, `config.save_paths` (the owner-only save),
the `hub-admin stremio_status` / `stremio_save` operations and their web routes, the new
`stremio.start_wait_seconds` setting, and the guards that keep the save from ever becoming a tool.

Nothing here touches the network (only loopback fakes) and nothing starts a real Stremio.

Run: python tests/test_hub_stremio_detect.py
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-stremio-detect-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT"):
    os.environ.pop(var, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_stremio as fake  # noqa: E402
from jarvis import daemons, hub_admin, tool_daemons  # noqa: E402
from jarvis.actions import stremio_stack  # noqa: E402
from jarvis.hub import settings as hub_settings  # noqa: E402
from jarvis.hub.stremio import config, detect, server as sserver, status  # noqa: E402

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


class FakeFs:
    """A folder tree for the probes: a set of file paths (either kind of separator, any case)."""

    def __init__(self, files):
        self.orig, self.files, self.dirs = {}, set(), set()
        for f in files:
            n = self._n(f)
            self.files.add(n)
            self.orig[n] = f.replace("\\", "/")
            parts = n.split("/")
            oparts = f.replace("\\", "/").split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                self.dirs.add(d)
                self.orig.setdefault(d, "/".join(oparts[:i]))

    @staticmethod
    def _n(path):
        return path.replace("\\", "/").rstrip("/").lower()

    def is_file(self, path):
        return self._n(path) in self.files

    def is_dir(self, path):
        return self._n(path) in self.dirs

    def listdir(self, path):
        prefix = self._n(path) + "/"
        out = set()
        for item in self.files | self.dirs:
            if item.startswith(prefix) and "/" not in item[len(prefix):]:
                out.add(self.orig[item].rsplit("/", 1)[-1])
        return sorted(out)


def build_lnk(target, workdir="", icon="", with_info=True):
    """A minimal valid Windows shortcut (MS-SHLLINK): LinkInfo with a local base path + string data."""
    header = bytearray(76)
    struct.pack_into("<I", header, 0, 0x4C)
    flags = 0x80 | (0x02 if with_info else 0) | (0x10 if workdir else 0) | (0x40 if icon else 0)
    struct.pack_into("<I", header, 20, flags)
    body = b""
    if with_info:
        volume = struct.pack("<IIII", 17, 3, 0, 16) + b"\x00"
        base = target.encode("cp1252") + b"\x00"
        local_off = 0x1C + len(volume)
        suffix_off = local_off + len(base)
        info_size = suffix_off + 1
        body += struct.pack("<IIIIIII", info_size, 0x1C, 1, 0x1C, local_off, 0, suffix_off) + volume + base + b"\x00"
    for text in (workdir, icon):
        if text:
            body += struct.pack("<H", len(text)) + text.encode("utf-16-le")
    return bytes(header) + body


# ===================================================================== step 1: detect.py

check("join keeps a Windows path Windows", detect.join("C:\\a\\b", "c", "d.exe") == "C:\\a\\b\\c\\d.exe")
check("join keeps a posix path posix", detect.join("/opt/x/", "y") == "/opt/x/y")
check("dirname / basename / stem handle both separators",
      detect.dirname("C:\\a\\b\\s.exe") == "C:\\a\\b" and detect.basename("/x/y/z.js") == "z.js" and detect.stem("C:\\A\\Stremio.EXE") == "stremio")
check("clean_command_path: quoted uninstall string with arguments",
      detect.clean_command_path('"C:\\Stremio\\Uninstall Stremio.exe" /allusers') == "C:\\Stremio\\Uninstall Stremio.exe")
check("clean_command_path: icon with an index", detect.clean_command_path("C:\\Stremio\\stremio.exe,0") == "C:\\Stremio\\stremio.exe")
check("clean_command_path: unquoted exe followed by arguments", detect.clean_command_path("C:\\S\\un.exe --silent") == "C:\\S\\un.exe")
check("clean_command_path: empty stays empty", detect.clean_command_path(None) == "")

NONDEFAULT = "D:\\Apps\\Stremio-4"
TREE = FakeFs([NONDEFAULT + "\\stremio-runtime.exe", NONDEFAULT + "\\server.js", NONDEFAULT + "\\stremio.exe"])

pair = detect.find_server_files(NONDEFAULT, TREE)
check("find_server_files: runtime and server.js side by side",
      pair and pair["runtime_exe"] == NONDEFAULT + "\\stremio-runtime.exe" and pair["server_js"] == NONDEFAULT + "\\server.js")
check("find_server_files: a parent folder finds the Stremio-* child",
      (detect.find_server_files("D:\\Apps", TREE) or {}).get("folder", "").lower().endswith("stremio-4"))
check("find_server_files: a folder without server.js is not a pair",
      detect.find_server_files("D:\\Nope", FakeFs(["D:\\Nope\\stremio-runtime.exe"])) is None)
check("find_server_files: server.js without a runtime is not a pair",
      detect.find_server_files("D:\\Nope", FakeFs(["D:\\Nope\\server.js"])) is None)
check("find_server_files: node next to server.js counts",
      (detect.find_server_files("/opt/stremio", FakeFs(["/opt/stremio/node", "/opt/stremio/server.js"])) or {}).get("runtime_exe") == "/opt/stremio/node")
check("find_server_files: a folder that does not exist is None", detect.find_server_files("Z:\\gone", TREE) is None)
check("find_server_files: a 'server' sub-folder is looked into",
      (detect.find_server_files("C:\\S", FakeFs(["C:\\S\\server\\server.js", "C:\\S\\server\\stremio-runtime.exe"])) or {}).get("server_js") == "C:\\S\\server\\server.js")

# ---- probe 1: the port (only loopback is ever asked)
asked = []


def check_fn(answers):
    def fn(url):
        asked.append(url)
        return (url in answers, answers.get(url, "connection refused"))
    return fn


r = detect.probe_port("http://127.0.0.1:11470", check_fn({"http://127.0.0.1:11470": "4.21.2"}))
check("port probe: answers -> found, with the version", r["found"] and r["answers"] and r["version"] == "4.21.2")
r = detect.probe_port("http://127.0.0.1:11470", check_fn({}), other_ports=(11470, 12470, 11999))
check("port probe: nothing answers -> not found, says so", not r["found"] and not r["answers"] and "nothing answers" in r["detail"])
check("port probe: the TLS port 12470 is never asked over plain http", "http://127.0.0.1:12470" not in asked)
asked.clear()
r = detect.probe_port("http://127.0.0.1:11999", check_fn({"http://127.0.0.1:11470": "4.20.1"}), other_ports=(11470,))
check("port probe: answered on another port -> reported with that address",
      not r["answers"] and r["other_url"] == "http://127.0.0.1:11470" and "answers at http://127.0.0.1:11470" in r["detail"])
check("port probe: only 127.0.0.1 is ever asked", all(u.startswith("http://127.0.0.1:") for u in asked))

# ---- probe 2: a running Stremio
r = detect.probe_process([], TREE)
check("process probe: nothing running -> not found", not r["found"] and not r["running"])
r = detect.probe_process([{"pid": 5, "name": "chrome.exe", "exe": "C:\\x\\chrome.exe", "cmdline": ["chrome", "--password=hunter2"]}], TREE)
check("process probe: other programs are ignored", not r["found"] and not r["running"])
r = detect.probe_process([{"pid": 77, "name": "stremio.exe", "exe": NONDEFAULT + "\\stremio.exe", "cmdline": []}], TREE)
check("process probe: stremio.exe in a non-default folder -> its server files",
      r["found"] and r["running"] and r["folder"] == NONDEFAULT and r["pids"] == [77] and r["probe"] == "process")
r = detect.probe_process([{"pid": 78, "name": "stremio-runtime.exe", "exe": NONDEFAULT + "\\stremio-runtime.exe",
                           "cmdline": [NONDEFAULT + "\\stremio-runtime.exe", NONDEFAULT + "\\server.js"]}], TREE)
check("process probe: the runtime's own command line gives the exact pair", r["found"] and r["server_js"] == NONDEFAULT + "\\server.js")
r = detect.probe_process([{"pid": 79, "name": "stremio.exe", "exe": "E:\\Odd\\stremio.exe", "cmdline": []}], TREE)
check("process probe: running but no server files there -> running, not found, says where",
      not r["found"] and r["running"] and "E:\\Odd" in r["detail"] and "no server.js" in r["detail"])
r = detect.probe_process([{"pid": 80, "name": "node", "exe": "/usr/bin/node", "cmdline": ["node", "/opt/stremio/server.js"]}],
                         FakeFs(["/opt/stremio/node", "/opt/stremio/server.js", "/usr/bin/node"]))
check("process probe: `node /opt/stremio/server.js` counts as Stremio", r["running"])
check("process probe: a node program that isn't Stremio does not",
      not detect.probe_process([{"pid": 81, "name": "node", "exe": "/usr/bin/node", "cmdline": ["node", "/srv/app/index.js"]}], TREE)["running"])

# ---- probe 3: Windows' installed-programs list
ENTRY = {"name": "Stremio 4.4.168", "location": NONDEFAULT, "icon": "", "uninstall": "", "version": "4.4.168"}
r = detect.probe_registry([ENTRY], TREE)
check("registry probe: InstallLocation -> found, version carried", r["found"] and r["folder"] == NONDEFAULT and r["version"] == "4.4.168")
r = detect.probe_registry([{"name": "Stremio", "location": "", "icon": NONDEFAULT + "\\stremio.exe,0", "uninstall": ""}], TREE)
check("registry probe: DisplayIcon (with ',0') gives the folder", r["found"] and r["folder"] == NONDEFAULT)
r = detect.probe_registry([{"name": "Stremio", "location": "", "icon": "", "uninstall": '"%s\\Uninstall Stremio.exe" /allusers' % NONDEFAULT}], TREE)
check("registry probe: the uninstaller's folder gives the install folder", r["found"] and r["folder"] == NONDEFAULT)
r = detect.probe_registry([{"name": "7-Zip", "location": NONDEFAULT}], TREE)
check("registry probe: other programs are ignored", not r["found"] and "no Stremio entry" in r["detail"])
r = detect.probe_registry([{"name": "Stremio", "location": "C:\\Emptied", "version": "5.0"}], TREE)
check("registry probe: listed but its folder has no server files -> says so, keeps the folder",
      not r["found"] and r["folder"] == "C:\\Emptied" and "no server.js" in r["detail"])

# ---- probe 4: the Start Menu shortcut
lnk = detect.parse_lnk(build_lnk(NONDEFAULT + "\\stremio.exe", workdir=NONDEFAULT, icon=NONDEFAULT + "\\stremio.exe"))
check("parse_lnk reads the target, working folder and icon",
      lnk.get("target") == NONDEFAULT + "\\stremio.exe" and lnk.get("workdir") == NONDEFAULT and lnk.get("icon") == NONDEFAULT + "\\stremio.exe")
check("parse_lnk without LinkInfo still gives the working folder", detect.parse_lnk(build_lnk("", workdir=NONDEFAULT, with_info=False)).get("workdir") == NONDEFAULT)
check("parse_lnk: garbage, short and empty input give {} (never raise)",
      detect.parse_lnk(b"not a shortcut") == {} and detect.parse_lnk(b"") == {} and detect.parse_lnk(b"\x4c\x00\x00\x00" + b"\x00" * 10) == {})
r = detect.probe_shortcuts([dict(lnk, path="C:\\Menu\\Stremio.lnk")], TREE)
check("shortcut probe: the shortcut's target gives the folder", r["found"] and r["folder"] == NONDEFAULT and "Stremio.lnk" in r["detail"])
r = detect.probe_shortcuts([{"path": "C:\\Menu\\Stremio.lnk", "target": "", "workdir": NONDEFAULT, "icon": ""}], TREE)
check("shortcut probe: the working folder alone is enough", r["found"])
check("shortcut probe: none -> not found", not detect.probe_shortcuts([], TREE)["found"])
r = detect.probe_shortcuts([{"path": "C:\\Menu\\Stremio.lnk", "target": "C:\\Gone\\stremio.exe"}], TREE)
check("shortcut probe: a shortcut pointing at nothing useful says so", not r["found"] and "C:\\Gone" in r["detail"])

# ---- probe 5: the usual folders
env = {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local", "PROGRAMFILES": "C:\\Program Files", "PROGRAMFILES(X86)": "C:\\Program Files (x86)"}
folders = detect.fixed_folders(env, "C:\\Users\\me")
check("fixed_folders: the per-user LNV folder first, no duplicates",
      folders[0] == "C:\\Users\\me\\AppData\\Local\\Programs\\LNV\\Stremio-4" and len(folders) == len({f.lower() for f in folders}))
r = detect.probe_folders([folders[0], NONDEFAULT], TREE)
check("folder probe: the first folder that has the pair wins", r["found"] and r["folder"] == NONDEFAULT)
r = detect.probe_folders(folders, TREE)
check("folder probe: none -> not found, and it says how many it looked in", not r["found"] and "%d usual" % len(folders) in r["detail"])

# ---- the order
inputs = dict(
    processes=[{"pid": 1, "name": "stremio.exe", "exe": NONDEFAULT + "\\stremio.exe", "cmdline": []}],
    registry=[{"name": "Stremio", "location": "D:\\Apps2\\Stremio-4"}],
    shortcuts=[], folders=[],
)
two = FakeFs([NONDEFAULT + "\\stremio-runtime.exe", NONDEFAULT + "\\server.js", "D:\\Apps2\\Stremio-4\\stremio-runtime.exe", "D:\\Apps2\\Stremio-4\\server.js"])
found = detect.find_files(fs=two, **inputs)
check("order: a running Stremio wins over the registry", found["found"] and found["how"] == "process" and found["folder"] == NONDEFAULT)
check("order: it stops at the first probe that finds the pair", [t["probe"] for t in found["tried"]] == ["process"])
found = detect.find_files(fs=two, **dict(inputs, processes=[]))
check("order: no process -> the registry", found["how"] == "registry" and [t["probe"] for t in found["tried"]] == ["process", "registry"])
found = detect.find_files(fs=two, processes=[], registry=[], shortcuts=[dict(lnk, path="C:\\Menu\\Stremio.lnk")], folders=[])
check("order: then the shortcut", found["how"] == "shortcut")
found = detect.find_files(fs=two, processes=[], registry=[], shortcuts=[], folders=[NONDEFAULT])
check("order: then the usual folders", found["how"] == "folder")
found = detect.find_files(fs=FakeFs([]), processes=[], registry=[], shortcuts=[], folders=["C:\\x"])
check("order: nothing -> not found, every probe ran and said what it saw",
      not found["found"] and found["how"] == "" and [t["probe"] for t in found["tried"]] == ["process", "registry", "shortcut", "folder"])
found = detect.find_files(fs=FakeFs([]), processes=[{"pid": 9, "name": "Stremio.exe", "exe": "E:\\Odd\\Stremio.exe", "cmdline": []}],
                          registry=[], shortcuts=[], folders=[])
check("find_files: a running app with no server files is app_running with its folder as install_folder",
      found["app_running"] and not found["found"] and found["install_folder"] == "E:\\Odd")
d = detect.detect("http://127.0.0.1:11470", check=check_fn({}), ports=[], fs=two, processes=[], registry=[], shortcuts=[], folders=[NONDEFAULT])
check("detect(): the port probe comes first in `tried`, and `how` says in words where the files were found",
      d["tried"][0]["probe"] == "port" and d["how"] == detect.HOW["folder"] and d["files"]["found"] and not d["server"]["answers"])
check("the real readers never raise and return lists",
      isinstance(detect.list_processes(), list) and isinstance(detect.read_uninstall_entries(), list)
      and isinstance(detect.read_start_menu_shortcuts([]), list) and isinstance(detect.listening_ports([999999999]), list))

# ===================================================================== config: runtime(), problems(), save_paths()

REAL = Path(HOME) / "Stremio-Custom"
REAL.mkdir()
(REAL / "stremio-runtime.exe").write_bytes(b"")
(REAL / "server.js").write_bytes(b"")
EXE, JS = str(REAL / "stremio-runtime.exe"), str(REAL / "server.js")

CLOSED = free_port()
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % CLOSED
FAKE, BASE, ST = fake.start()
cfg_path = config.config_path()
cfg_path.parent.mkdir(parents=True, exist_ok=True)

# nothing on this machine that the real readers could find: every reader is replaced by the fakes below
PROCS, REG, LNKS, FOLDERS, PORTS = [], [], [], [], []
detect.list_processes = lambda: list(PROCS)
detect.read_uninstall_entries = lambda: list(REG)
detect.read_start_menu_shortcuts = lambda *a, **k: list(LNKS)
detect.fixed_folders = lambda *a, **k: list(FOLDERS)
detect.listening_ports = lambda pids: list(PORTS)

check("runtime(): nothing saved, nothing found -> ('', '', '')", config.runtime() == ("", "", ""))
FOLDERS[:] = [str(REAL)]
check("runtime(): the usual-folder probe finds it, `where` says which probe", config.runtime() == (EXE, JS, "folder"))
FOLDERS[:] = []
REG[:] = [{"name": "Stremio", "location": str(REAL), "version": "4.9"}]
exe, js, where = config.runtime()
check("runtime(): a non-default install is found through the registry with no saved path", (exe, js, where) == (EXE, JS, "registry"))
check("detect_runtime() keeps its old (exe, js) shape", config.detect_runtime(processes=[], registry=REG, shortcuts=[], folders=[]) == (EXE, JS))
REG[:] = []

check("problems(): no file -> none", config.problems() == [])
cfg_path.write_text(json.dumps({"server": {"runtime_exe": str(REAL / "gone.exe"), "server_js": JS}, "account": {"auth_key": "SECRETKEY"}}))
probs = config.problems()
check("problems(): a saved path that no longer exists is reported in words, with the path",
      any("no longer exists" in p and "gone.exe" in p for p in probs))
check("runtime(): a stale saved path is ignored (probes used instead)", config.runtime() == ("", "", ""))
cfg_path.write_text(json.dumps({"server": {"runtime_exe": EXE}}))
check("problems(): only one of the two paths saved is reported", any("only one of" in p for p in config.problems()))
cfg_path.write_text(json.dumps({"server": {"runtime_exe": EXE, "server_js": JS}}))
check("runtime(): both saved paths exist -> 'config'", config.runtime() == (EXE, JS, "config") and config.problems() == [])
check("runtime_info(): a saved pair needs no probe", config.runtime_info()["tried"] == [] and config.runtime_info()["where"] == "config")
cfg_path.unlink()

# ---- save_paths (the owner's button): checks first, writes only its keys
r = config.save_paths(EXE, JS)
check("save_paths: both files exist -> saved", r["ok"] and r["saved"] == {"runtime_exe": EXE, "server_js": JS})
data = json.loads(cfg_path.read_text())
check("save_paths: wrote the two keys", data["server"]["runtime_exe"] == EXE and data["server"]["server_js"] == JS)
cfg_path.write_text(json.dumps({"server": {"url": "http://127.0.0.1:11470", "env": {"A": "1"}}, "web": {"dir": "D:/w"},
                                "account": {"auth_key": "SECRETKEY"}, "prefs": {"min_seeders": 5}, "extra": [1, 2]}))
r = config.save_paths(EXE, JS)
data = json.loads(cfg_path.read_text())
check("save_paths: every other key (account key, prefs, web, env, unknown keys) is carried over unchanged",
      data["account"] == {"auth_key": "SECRETKEY"} and data["prefs"] == {"min_seeders": 5} and data["web"] == {"dir": "D:/w"}
      and data["server"]["env"] == {"A": "1"} and data["extra"] == [1, 2] and data["server"]["url"] == "http://127.0.0.1:11470")
check("save_paths: the result never echoes the account key", "SECRETKEY" not in json.dumps(r))
before = cfg_path.read_text()
for label, args in (("a missing runtime", (str(REAL / "nope.exe"), JS)), ("a missing server.js", (EXE, str(REAL / "nope.js"))),
                    ("a relative path", ("stremio-runtime.exe", JS)), ("an empty path", ("", JS)),
                    ("a network share", ("\\\\server\\share\\stremio-runtime.exe", JS)), ("a path with a newline inside", (EXE + "\nx", JS)),
                    ("server.js that isn't a .js file", (EXE, EXE))):
    res = config.save_paths(*args)
    check("save_paths refuses %s, with a sentence, and writes nothing" % label,
          not res["ok"] and res["error"] and cfg_path.read_text() == before)
check("save_paths names the missing file in the sentence", "doesn't exist" in config.save_paths(str(REAL / "nope.exe"), JS)["error"])
cfg_path.write_text("{ this is not json")
res = config.save_paths(EXE, JS)
check("save_paths: a stremio.json that isn't JSON is left alone, not overwritten",
      not res["ok"] and "can't be read" in res["error"] and cfg_path.read_text() == "{ this is not json")
cfg_path.write_text(json.dumps({"server": "oops"}))
check("save_paths: a file with a non-object 'server' is left alone", not config.save_paths(EXE, JS)["ok"])
cfg_path.unlink()
res = config.save_paths(EXE, JS, server_url="http://example.com:11470")
check("save_paths: a non-loopback address is refused", not res["ok"] and "127.0.0.1" in res["error"] and not cfg_path.exists())
res = config.save_paths(EXE, JS, server_url="http://127.0.0.1:%d" % free_port())
check("save_paths: an address nothing answers at is not saved", not res["ok"] and "nothing answers" in res["error"] and not cfg_path.exists())
res = config.save_paths(EXE, JS, server_url=BASE)
check("save_paths: a loopback address that answers like Stremio is saved with the paths",
      res["ok"] and json.loads(cfg_path.read_text())["server"]["url"] == BASE)
cfg_path.unlink()
os.environ["JARVIS_SCHEDULED"] = "1"
res = config.save_paths(EXE, JS)
check("save_paths: refused for an unattended process (a scheduled job, a chat reply)", not res["ok"] and res.get("code") == "unattended" and not cfg_path.exists())
os.environ.pop("JARVIS_SCHEDULED")

# ===================================================================== step 2: status.describe, one sentence per state

URL = "http://127.0.0.1:11470"


def facts(**kw):
    base = {"url": URL, "up": False, "version": "", "other_url": "", "other_version": "", "port_busy": False,
            "path": {"found": False, "runtime_exe": "", "server_js": "", "folder": "", "where": ""}, "app_running": False,
            "install_folder": "", "daemon": {"registered": True, "running": False, "status": "stopped"}, "problems": [],
            "tried": [], "wait_seconds": 60, "now": 1000.0, "waited": None, "exited": False}
    base.update(kw)
    return base


FOUND = {"found": True, "runtime_exe": EXE, "server_js": JS, "folder": str(REAL), "where": "registry"}
d = status.describe(facts(up=True, version="4.21.2"))
check("state running (started outside): the sentence says so and that nothing is started or stopped",
      d["state"] == "running" and d["managed_by"] == "outside" and "4.21.2" in d["sentence"] and "outside Jarvis" in d["sentence"])
d = status.describe(facts(up=True, version="4.21.2", daemon={"registered": True, "running": True, "status": "running"}))
check("state running (started by Jarvis)", d["state"] == "running" and d["managed_by"] == "jarvis" and "Jarvis started it" in d["sentence"])
d = status.describe(facts(other_url="http://127.0.0.1:11999", other_version="4.20"))
check("state other_port: names both addresses and offers to save the right one",
      d["state"] == "other_port" and "http://127.0.0.1:11999" in d["sentence"] and URL in d["sentence"] and d["action"] == "save_port")
d = status.describe(facts(port_busy=True))
check("state port_busy: says something else uses the port and it is not Stremio",
      d["state"] == "port_busy" and "already using" in d["sentence"] and "doesn't answer like a Stremio server" in d["sentence"])
d = status.describe(facts(path=FOUND, daemon={"registered": True, "running": True, "status": "running", "started_at": 990.0}))
check("state starting: 10 s into a 60 s wait", d["state"] == "starting" and "10 second(s) ago" in d["sentence"] and "60 seconds" in d["sentence"])
d = status.describe(facts(path=FOUND, daemon={"registered": True, "running": True, "status": "running", "started_at": 900.0}))
check("state silent: running 100 s and still not answering", d["state"] == "silent" and "100 seconds ago" in d["sentence"] and "still doesn't answer" in d["sentence"])
d = status.describe(facts(path=FOUND, waited=60, daemon={"registered": True, "running": True, "status": "running"}))
check("state silent: after Jarvis' own full wait, even without a start time", d["state"] == "silent" and "60 seconds" in d["sentence"])
d = status.describe(facts(path=FOUND, daemon={"registered": True, "running": False, "status": "crashed", "exit_code": 3,
                                              "console_tail": "couldn't start x.exe: access denied"}))
check("state start_failed: exit code and the console's last line are in the sentence",
      d["state"] == "start_failed" and "stopped again" in d["sentence"] and "exit code 3" in d["sentence"] and "access denied" in d["sentence"])
d = status.describe(facts(path=FOUND, exited=True))
check("state start_failed: also when Jarvis saw it stop while waiting", d["state"] == "start_failed")
d = status.describe(facts(path=FOUND, daemon={"registered": True, "running": False, "status": "restarting"}))
check("state start_failed: a crash loop counts", d["state"] == "start_failed")
d = status.describe(facts(app_running=True, path=FOUND))
check("state app_open: the app is open, its server silent: wait; Jarvis starts nothing",
      d["state"] == "app_open" and "won't start a second server" in d["sentence"] and not d["can_start"])
d = status.describe(facts(install_folder="E:\\Odd"))
check("state files_missing: Stremio found, its server files not; says where and what to do",
      d["state"] == "files_missing" and "E:\\Odd" in d["sentence"] and "server.js" in d["sentence"] and "Locate..." in d["sentence"] and d["action"] == "locate")
d = status.describe(facts())
check("state not_found: says what was looked at and how to fix it",
      d["state"] == "not_found" and "couldn't find Stremio" in d["sentence"] and "Windows' list of installed programs" in d["sentence"]
      and "Locate..." in d["sentence"] and str(config.config_path()) in d["sentence"] and d["action"] == "locate" and d["step"] == "find")
d = status.describe(facts(path=FOUND))
check("state ready: installed, not running, with how it was found and a Start action",
      d["state"] == "ready" and d["can_start"] and d["action"] == "start" and "Windows' list of installed programs" in d["sentence"] and str(REAL) in d["sentence"])
d = status.describe(facts(path=FOUND, problems=["server.url must be http on 127.0.0.1"]))
check("config problems travel with every state", d["problems"] == ["server.url must be http on 127.0.0.1"])
titles = {status.describe(f)["title"] for f in (facts(up=True), facts(), facts(path=FOUND), facts(app_running=True), facts(port_busy=True))}
check("every state has its own short title for the card", len(titles) == 5 and "Not found" in titles and "Installed, not running" in titles)
text = status.diagnostic_text(dict(status.describe(facts()), tried=[{"probe": "port", "detail": "nothing answers"}]))
check("diagnostic_text lists the state, the address and what each probe saw",
      "Not found" in text and URL in text and "port: nothing answers" in text and "SECRETKEY" not in text)

# ===================================================================== step 2: the tools against fakes

FOLDERS[:] = []
cfg_path.write_text(json.dumps({"web": {"url": BASE}, "account": {"auth_key": "SECRETKEY"}}))
saved_start, saved_status, saved_sleep = daemons.start, daemons.status, time.sleep
saved_wait, saved_read = stremio_stack._start_wait, daemons.read_console
stremio_stack._start_wait = lambda: 2
time.sleep = lambda s: None
for did in ("stremio_stack-server", "stremio_stack-web"):
    pass


def start_stub(result=(True, "started"), flip=None):
    calls = []

    def fn(did):
        calls.append(did)
        if flip:
            os.environ["JARVIS_STREMIO_SERVER_URL"] = flip
        return result
    fn.calls = calls
    return fn


def status_stub(**kw):
    state = {"id": "x", "status": "running", "running": True, "pid": 4242, "started_at": time.time() - 1, "exit_code": None, "last_error": ""}
    state.update(kw)
    return lambda did: dict(state)


# --- 1. not found anywhere
res = stremio_stack.tool_stack_start({"what": "server"})
srv = res["server"]
check("stack_start, Stremio not installed: ok false, outcome not_found, one plain sentence",
      not res["ok"] and srv["outcome"] == "not_found" and "couldn't find Stremio" in srv["error"] and srv["state"] == "error")

# --- 2. the daemon registration: the FIRST start works without a restart
check("before any start the daemons are not registered (a fresh data folder)", daemons.get("stremio_stack-server") is None)
ok, why = stremio_stack._ensure_daemon("stremio_stack-server")
check("_ensure_daemon: registers the tool's daemons on the spot, so the first start needs no restart",
      ok and why == "" and daemons.get("stremio_stack-server") is not None)

# --- 3. found by the registry; started; the server answers
REG[:] = [{"name": "Stremio", "location": str(REAL), "version": "4.9"}]
daemons.start = start_stub(flip=BASE)
res = stremio_stack.tool_stack_start({"what": "server"})
srv = res["server"]
check("stack_start: found via the registry, started, answered -> started with the version",
      res["ok"] and srv["state"] == "started" and srv["version"] == "4.21.2" and daemons.start.calls == ["stremio_stack-server"])
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % CLOSED

# --- 4. started but silent
daemons.start = start_stub()
daemons.status = status_stub()
res = stremio_stack.tool_stack_start({"what": "server"})
srv = res["server"]
check("stack_start: started but silent -> its own sentence with the wait, not 'check again'",
      not res["ok"] and srv["outcome"] == "silent" and "seconds ago" in srv["error"] and "still doesn't answer" in srv["error"])

# --- 5. could not start the program
daemons.start = start_stub()
daemons.status = status_stub(status="crashed", running=False, pid=None, exit_code=2)
daemons.read_console = lambda did, lines=200, path=None: ["", "couldn't start x: [WinError 5] Access is denied"]
res = stremio_stack.tool_stack_start({"what": "server"})
srv = res["server"]
check("stack_start: the program stops again -> 'could not start', exit code and the console's last line",
      not res["ok"] and srv["outcome"] == "start_failed" and "stopped again" in srv["error"] and "exit code 2" in srv["error"]
      and "Access is denied" in srv["error"])
daemons.read_console = saved_read

# --- 6. the daemon is switched off / refuses
daemons.status = saved_status
daemons.start = start_stub(result=(False, "'stremio_stack-server' is disabled — enable it first"))
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: a switched-off daemon is its own sentence", res["server"]["outcome"] == "disabled" and "switched off" in res["server"]["error"])
daemons.start = start_stub(result=(False, "no such thing"))
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: any other refusal from the daemon layer is passed on plainly", res["server"]["outcome"] == "launch_refused" and "no such thing" in res["server"]["error"])

# --- 7. daemon not registered and registering fails
saved_get, saved_sync = daemons.get, tool_daemons.sync_if_stale
daemons.get = lambda did: None
tool_daemons.sync_if_stale = lambda force=False, **k: None
daemons.start = start_stub()
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: 'daemon not registered' says so clearly when registering really fails",
      res["server"]["outcome"] == "not_registered" and "couldn't set up the Stremio server task" in res["server"]["error"]
      and not daemons.start.calls)
daemons.get, tool_daemons.sync_if_stale = saved_get, saved_sync

# --- 8. the app is open but its server is silent: nothing is started
PROCS[:] = [{"pid": 31, "name": "stremio.exe", "exe": str(REAL / "stremio.exe"), "cmdline": []}]
daemons.start = start_stub()
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: app open, server silent -> says so and starts nothing",
      not res["ok"] and res["server"]["outcome"] == "app_open" and "won't start a second server" in res["server"]["error"] and not daemons.start.calls)
PROCS[:] = []

# --- 9. answered on another port
PORTS[:] = [int(BASE.rsplit(":", 1)[1])]
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: a server answers on another port -> reported, nothing started",
      not res["ok"] and res["server"]["outcome"] == "other_port" and BASE in res["server"]["error"] and not daemons.start.calls)
PORTS[:] = []

# --- 10. a port clash: something listens that isn't Stremio
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(5)
busy_port = listener.getsockname()[1]


def accept_and_drop():
    while True:
        try:
            conn, _ = listener.accept()
            conn.close()
        except OSError:
            return


threading.Thread(target=accept_and_drop, daemon=True).start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % busy_port
sserver.forget_probe()
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: something else on the port -> 'blocked' with the port-in-use sentence",
      not res["ok"] and res["server"]["state"] == "blocked" and res["server"]["outcome"] == "port_busy" and "already using" in res["server"]["error"])
listener.close()
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % CLOSED

# --- 11. a config problem is shown by stack_start too
cfg_path.write_text(json.dumps({"server": {"url": "http://10.0.0.5:11470"}, "web": {"url": BASE}, "account": {"auth_key": "SECRETKEY"}}))
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
res = stremio_stack.tool_stack_start({"what": "server"})
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % CLOSED
check("stack_start: config.problems() is shown (not only by stack_doctor)",
      res["server"]["state"] == "already_up" and any("server.url must be http on 127.0.0.1" in p for p in res.get("config_problems") or []))
cfg_path.write_text(json.dumps({"web": {"url": BASE}, "account": {"auth_key": "SECRETKEY"}}))

# --- 12. the app already runs the server: already up, nothing started
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
daemons.start = start_stub()
res = stremio_stack.tool_stack_start({"what": "server"})
check("stack_start: a server that already answers is 'already_up'; nothing is started", res["ok"] and res["server"]["state"] == "already_up" and not daemons.start.calls)
st = stremio_stack.tool_stack_status({})
check("stack_status: the server block carries the plain-words state",
      st["ok"] and st["server"]["state"] == "running" and st["server"]["up"] and "outside Jarvis" in st["server"]["sentence"] and "already up" in st["server"]["note"])
doc = stremio_stack.tool_stack_doctor({})
names = {c["name"]: c for c in doc["checks"]}
check("stack_doctor: a 'looked: <probe>' row for each probe that ran, a 'state' row and the start wait",
      {"looked: port", "looked: process", "looked: registry"} <= set(names)
      and names["state"]["status"] == "ok" and "stremio.start_wait_seconds" in names["start wait"]["detail"])
check("stack_doctor: a copyable diagnostic that never carries the account key",
      isinstance(doc.get("diagnostic"), str) and "Stremio status" in doc["diagnostic"] and "SECRETKEY" not in json.dumps(doc))
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % CLOSED
REG[:] = []
st = stremio_stack.tool_stack_status({})
check("stack_status: server down and Stremio not found -> says Not found in the summary",
      st["server"]["state"] == "not_found" and "not found" in st["summary"] and not st["server"]["can_start"])
doc = stremio_stack.tool_stack_doctor({})
names = {c["name"]: c for c in doc["checks"]}
check("stack_doctor: server down + not found -> 'server' fails with the plain sentence, 'server files' warns",
      names["server"]["status"] == "fail" and "couldn't find Stremio" in names["server"]["detail"] and names["server files"]["status"] == "warn" and not doc["ok"])

daemons.start, daemons.status, time.sleep, stremio_stack._start_wait = saved_start, saved_status, saved_sleep, saved_wait

# ===================================================================== hub-admin: stremio_status / stremio_save

check("the two operations are registered, the read one as a read and the save as a write",
      "stremio_status" in hub_admin.READ_OPS and "stremio_save" in hub_admin.WRITE_OPS and "stremio_save" not in hub_admin.READ_OPS)
REG[:] = []
got = hub_admin.admin("stremio_status", {})
check("hub-admin stremio_status: state in words, address, path, tried probes, a diagnostic, nothing secret",
      got["ok"] and got["state"] == "not_found" and got["address"] and got["sentence"] and got["title"] == "Not found"
      and isinstance(got["tried"], list) and got["tried"] and "diagnostic" in got and got["config_file"].endswith("stremio.json")
      and "SECRETKEY" not in json.dumps(got) and "can_start" in got and "action" in got)
dry = hub_admin.admin("stremio_save", {"runtime_exe": EXE, "server_js": JS, "dry_run": True})
check("hub-admin stremio_save dry_run: checks both files and says what is on the port, writes nothing",
      dry["ok"] and dry["dry_run"] and "server" in dry and json.loads(cfg_path.read_text()).get("server", {}).get("runtime_exe") is None)
bad = hub_admin.admin("stremio_save", {"runtime_exe": str(REAL / "nope.exe"), "server_js": JS})
check("hub-admin stremio_save: a missing file is refused with a sentence", not bad["ok"] and "doesn't exist" in bad["error"])
saved = hub_admin.admin("stremio_save", {"runtime_exe": EXE, "server_js": JS})
check("hub-admin stremio_save: saves, then answers with the fresh status",
      saved["ok"] and saved["saved"] == {"runtime_exe": EXE, "server_js": JS} and saved["path"]["where"] == "config" and saved["state"] == "ready"
      and saved["can_start"] and json.loads(cfg_path.read_text())["account"] == {"auth_key": "SECRETKEY"})
for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID"):
    os.environ[var] = "1"
    refused = hub_admin.admin("stremio_save", {"runtime_exe": EXE, "server_js": JS})
    check("hub-admin stremio_save is refused when %s is set (a chat reply, a scheduled job, a sub-agent)" % var,
          not refused["ok"] and refused.get("code") == "unattended")
    os.environ.pop(var)
check("hub-admin stremio_status stays readable for an unattended process (it changes nothing)",
      (os.environ.__setitem__("JARVIS_SCHEDULED", "1") or True) and hub_admin.admin("stremio_status", {})["ok"])
os.environ.pop("JARVIS_SCHEDULED", None)
before = cfg_path.read_text()
os.environ["JARVIS_CHANNEL"] = "x"
direct = config.save_paths(EXE, JS)
os.environ.pop("JARVIS_CHANNEL")
check("config.save_paths checks for an unattended caller itself too", not direct["ok"] and cfg_path.read_text() == before)

# the web routes: a read route and a header-guarded write route, and no path ever comes from the query string
server_js = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
check("web/server.js: GET /api/hub/stremio is a read route", 'app.get("/api/hub/stremio", requireHubRead, requireJarvis' in server_js)
check("web/server.js: POST /api/hub/stremio/paths needs the X-Jarvis-Hub header (requireHubWrite)",
      'app.post("/api/hub/stremio/paths", requireHubWrite, requireJarvis' in server_js)
check("web/server.js: the paths come from the POST body only, never the query string",
      "req.query.runtime_exe" not in server_js and "req.query.server_js" not in server_js and "req.body?.runtime_exe" in server_js)

# ===================================================================== never a model tool (decision 14 f)

import re  # noqa: E402

offenders = []
for py in sorted((ROOT / "jarvis-cli" / "jarvis").rglob("*.py")):
    rel = py.relative_to(ROOT / "jarvis-cli" / "jarvis").as_posix()
    if rel in ("hub/stremio/config.py", "hub_admin.py"):
        continue
    if re.search(r"\bsave_paths\b|\bstremio_save\b", py.read_text(encoding="utf-8", errors="replace")):
        offenders.append(rel)
check("nothing but hub_admin.py and config.py names save_paths / stremio_save (no tool, no other module): %s" % (offenders or "clean"), not offenders)
check("the Stremio tool file declares no tool that writes the paths",
      not any(re.search(r"save|locate|set_path|write_config", name) for name in stremio_stack.TOOLS))
check("no Stremio tool schema takes a path to the runtime or server.js",
      not any(k in json.dumps(stremio_stack.TOOL_SCHEMAS) for k in ("runtime_exe", "server_js")))

# ===================================================================== the setting

check("hub setting stremio.start_wait_seconds: default 60 (longer than the old 25), writable by dl_prefs",
      hub_settings.DEFAULTS["stremio"]["start_wait_seconds"] == 60 and "stremio.start_wait_seconds" in hub_settings.PREFS_WRITABLE)
validator = hub_settings.VALIDATORS["stremio.start_wait_seconds"] if hasattr(hub_settings, "VALIDATORS") else None
if validator is None:
    for name in dir(hub_settings):
        value = getattr(hub_settings, name)
        if isinstance(value, dict) and "stremio.metadata_timeout" in value:
            validator = value["stremio.start_wait_seconds"]
check("start_wait_seconds accepts 10 and 300, refuses 9, 301, text and a boolean",
      validator(10)[1] is None and validator(300)[1] is None and validator(9)[1] and validator(301)[1] and validator("x")[1] and validator(True)[1])
check("stack_start reads the wait from the hub setting (default 60 when unset)", stremio_stack._start_wait() == 60)

# ===================================================================== the daemon runner

runner = ROOT / "jarvis-cli" / "jarvis" / "actions" / "_stremio_server.py"
env = dict(os.environ, HOME=HOME, USERPROFILE=HOME)
env.pop("JARVIS_STREMIO_SERVER_URL", None)
env["JARVIS_STREMIO_SERVER_URL"] = BASE
done = subprocess.run([sys.executable, str(runner)], env=env, capture_output=True, text=True, timeout=60)
check("runner: a server that already answers -> exits 0, starts nothing", done.returncode == 0 and "already answers" in done.stdout)
env["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:%d" % CLOSED
cfg_path.write_text(json.dumps({"web": {"url": BASE}}))
env["LOCALAPPDATA"] = ""
done = subprocess.run([sys.executable, str(runner)], env=env, capture_output=True, text=True, timeout=60)
check("runner: nothing to start -> exits 2 and says what it looked at and how to fix it",
      done.returncode == 2 and "I looked at:" in done.stderr and "process:" in done.stderr and "Locate..." in done.stderr)

# ===================================================================== brief 19: the Stremio card's needs

check("LOCATE_HINT no longer promises a button that doesn't exist yet, and points at the card",
      "until that button exists" not in status.LOCATE_HINT.lower() and "Locate..." in status.LOCATE_HINT and "Settings > Downloads > Stremio" in status.LOCATE_HINT)
d = status.describe(facts())
check("the not_found sentence still names Locate..., the card and the config file (so the manual way stays documented)",
      "Locate..." in d["sentence"] and "Settings > Downloads > Stremio" in d["sentence"] and str(config.config_path()) in d["sentence"]
      and "until that button" not in d["sentence"].lower())
d = status.describe(facts(other_url="http://127.0.0.1:11471", other_version="4.20.8"))
check("describe() hands the other address on (state other_port) so the card's Locate... can save it; empty otherwise",
      d["state"] == "other_port" and d["other_url"] == "http://127.0.0.1:11471" and status.describe(facts())["other_url"] == "")
REG[:] = []
got = hub_admin.admin("stremio_status", {})
check("hub-admin stremio_status also reports other_url, the wait Start may take, and stremio-web (url, answers, configured)",
      got["ok"] and "other_url" in got and isinstance(got["wait_seconds"], int) and 10 <= got["wait_seconds"] <= 300
      and set(got["web"]) == {"url", "answers", "configured"} and isinstance(got["web"]["answers"], bool) and isinstance(got["web"]["configured"], bool))
hub_src = (ROOT / "jarvis-cli" / "jarvis" / "hub_admin.py").read_text(encoding="utf-8")
start_src = hub_src[hub_src.index("def stremio_start"):hub_src.index("# ----", hub_src.index("def stremio_start"))]
check("stremio_start starts the SERVER only, through tool_stack_start, and takes nothing from the request",
      'tool_stack_start({"what": "server"})' in start_src and "request.get" not in start_src and "what\": \"both" not in start_src)
offenders = []
for py in sorted((ROOT / "jarvis-cli" / "jarvis").rglob("*.py")):
    rel = py.relative_to(ROOT / "jarvis-cli" / "jarvis").as_posix()
    if rel == "hub_admin.py":
        continue
    if re.search(r"\bstremio_start\b", py.read_text(encoding="utf-8", errors="replace")):
        offenders.append(rel)
check("nothing but hub_admin.py names the stremio_start op (no tool, no other module): %s" % (offenders or "clean"), not offenders)
check("no Stremio tool is named stremio_start", "stremio_start" not in stremio_stack.TOOLS)

shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
