"""Find the Stremio desktop app without any setup (brief 12 / N9, step 1).

Everything here is READ-ONLY: it lists processes, reads Windows' uninstall list, reads Start Menu
shortcut files and looks at folders.  It never starts, stops or writes anything.

The probes run in this order and each one says what it found and which probe found it:

    1. port       the configured server address answers like a Stremio server
    2. process    a Stremio program is running (`stremio.exe`, `stremio-runtime.exe`): its folder
    3. registry   Windows' "installed programs" list has a Stremio entry: its install folder
    4. shortcut   a Stremio shortcut in the Start Menu: where it points
    5. folder     the usual install folders (the list that used to be the only thing looked at)
    6. none       nothing found: the owner points at it (Locate...)

Probe 1 is about the SERVER ("is it up, on which port").  Probes 2 to 5 are about the FILES Jarvis
needs to start a server itself (`stremio-runtime` + `server.js` in the same folder).  The first of
2 to 5 that finds the pair wins; `tried` lists every probe that ran, in plain words, for
`stack_doctor` and for the "Copy diagnostic" button.

Every probe takes its INPUTS as arguments (the process list, the registry values, the shortcut
files, the folder tree), so the tests feed fakes.  The `list_*` / `read_*` functions below are the
real readers; they never raise (a failure is an empty list).  Paths are handled as plain strings
with either kind of separator, so a Windows path can be tested on any machine.

Nothing here imports another module of this package at import time (config.py imports this file),
and `psutil` / `winreg` are imported inside the readers that need them.
"""

import os
import re
import struct
import sys

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 11470
HTTPS_PORT = 12470        # the server's TLS port: a plain-HTTP probe would only hang on it

# a running program counts as Stremio when its file name (without .exe) is one of these
APP_PROCESS_NAMES = ("stremio", "stremio-runtime", "stremio-service")
RUNTIME_NAMES = ("stremio-runtime.exe", "stremio-runtime", "node.exe", "node")
SERVER_JS = "server.js"
# folders worth looking into when a probe names a folder that has no server.js directly in it
SUBFOLDERS = ("server", "resources", "app", "Stremio-4", "Stremio")

HOW = {
    "config": "from the paths saved for Jarvis",
    "port": "because it answers on its port",
    "process": "from the running Stremio program",
    "registry": "from Windows' list of installed programs",
    "shortcut": "from the Stremio shortcut in the Start Menu",
    "folder": "in one of Stremio's usual install folders",
    "": "",
}


# --------------------------------------------------------------------------- path helpers

def _sep(path):
    return "\\" if ("\\" in path or re.match(r"^[A-Za-z]:", path)) else "/"


def join(base, *parts):
    """base + parts with the separator `base` already uses (so a Windows path stays one)."""
    out = str(base or "")
    sep = _sep(out)
    for part in parts:
        part = str(part or "").strip("/\\")
        if part:
            out = out.rstrip("/\\") + sep + part
    return out


def dirname(path):
    path = str(path or "").rstrip("/\\")
    cut = max(path.rfind("/"), path.rfind("\\"))
    if cut < 0:
        return ""
    return path[:cut] if cut > 0 else path[:1]


def basename(path):
    path = str(path or "").rstrip("/\\")
    return path[max(path.rfind("/"), path.rfind("\\")) + 1:]


def stem(path):
    name = basename(path).lower()
    return name[:-4] if name.endswith(".exe") else name


def clean_command_path(text):
    """The file path inside a registry value such as `"C:\\x\\uninstall.exe" /S`, `C:\\x\\stremio.exe,0`
    or `C:\\x\\a.exe --flag`: quotes, an icon index and trailing arguments removed."""
    text = str(text or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        text = text[1:end] if end > 0 else text[1:]
    else:
        match = re.match(r"^(.+?\.(?:exe|lnk|ico|dll|js))(?=$|[\s,])", text, re.I)
        if match:
            text = match.group(1)
    return re.sub(r",\s*-?\d+$", "", text).strip()


class RealFs:
    """The three questions the probes ask of the disk."""

    def is_file(self, path):
        try:
            return os.path.isfile(path)
        except (OSError, ValueError):
            return False

    def is_dir(self, path):
        try:
            return os.path.isdir(path)
        except (OSError, ValueError):
            return False

    def listdir(self, path):
        try:
            return sorted(os.listdir(path))
        except (OSError, ValueError):
            return []


# --------------------------------------------------------------------------- finding the server files

def _folder_candidates(folder, fs):
    out = [folder]
    out += [join(folder, sub) for sub in SUBFOLDERS]
    out += [join(folder, name) for name in fs.listdir(folder) if name.lower().startswith("stremio")]
    seen, unique = set(), []
    for item in out:
        key = item.replace("\\", "/").rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def find_server_files(folder, fs=None):
    """{'runtime_exe', 'server_js', 'folder'} when `folder` (or a Stremio sub-folder of it) holds a
    runtime and `server.js` side by side, else None."""
    fs = fs or RealFs()
    folder = str(folder or "").strip()
    if not folder or not fs.is_dir(folder):
        return None
    for cand in _folder_candidates(folder, fs):
        js = join(cand, SERVER_JS)
        if not fs.is_file(js):
            continue
        for name in RUNTIME_NAMES:
            exe = join(cand, name)
            if fs.is_file(exe):
                return {"runtime_exe": exe, "server_js": js, "folder": cand}
    return None


def _result(probe, found, detail, **extra):
    out = {"probe": probe, "found": bool(found), "detail": detail}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- probe 1: the port

def _default_check(url):
    from . import server           # lazy: server.py imports config.py, which imports this file
    return server.StremioServer(url).is_up(timeout=1.0)


def url_for_port(port):
    return "http://%s:%d" % (LOOPBACK_HOST, int(port))


def probe_port(url, check=None, other_ports=()):
    """Does a Stremio server answer at `url`?  If not, does one answer on another loopback port?

    `check(url) -> (up, version_or_reason)`.  `other_ports` are extra loopback ports to try (the
    default 11470 and the ports a running Stremio program listens on): only 127.0.0.1 is ever
    asked, never another host."""
    check = check or _default_check
    up, info = check(url)
    if up:
        return _result("port", True, "Stremio's server answers at %s (version %s)" % (url, info),
                       url=url, version=str(info), answers=True)
    tried = [url]
    for port in other_ports:
        if port == HTTPS_PORT:
            continue
        try:
            other = url_for_port(port)
        except (TypeError, ValueError):
            continue
        if other in tried:
            continue
        tried.append(other)
        up2, info2 = check(other)
        if up2:
            return _result("port", False, "nothing answers at %s, but a Stremio server answers at %s (version %s)"
                           % (url, other, info2), url=url, answers=False, other_url=other, other_version=str(info2))
    return _result("port", False, "nothing answers like a Stremio server at %s" % url, url=url, answers=False,
                   reason=str(info))


# --------------------------------------------------------------------------- probe 2: running program

def is_stremio_process(proc):
    name = stem(proc.get("name") or proc.get("exe") or "")
    if name in APP_PROCESS_NAMES:
        return True
    # `node server.js` started from a Stremio folder (Linux packages, a hand-started server)
    for arg in proc.get("cmdline") or []:
        if basename(str(arg)).lower() == SERVER_JS and "stremio" in str(arg).lower():
            return True
    return False


def _server_js_in(cmdline):
    for arg in cmdline or []:
        text = str(arg)
        if basename(text).lower() == SERVER_JS:
            return text
    return ""


def probe_process(processes, fs=None):
    """`processes`: [{'pid', 'name', 'exe', 'cmdline': [..]}].  Finds the folder of a running Stremio."""
    fs = fs or RealFs()
    mine = [p for p in (processes or []) if isinstance(p, dict) and is_stremio_process(p)]
    if not mine:
        return _result("process", False, "no Stremio program is running", running=False, pids=[])
    pids = [p.get("pid") for p in mine if p.get("pid") is not None]
    names = ", ".join(sorted({basename(p.get("exe") or p.get("name") or "") or "?" for p in mine}))
    # the exact pair, when the runtime was started with the server's script as an argument
    for proc in mine:
        exe, js = str(proc.get("exe") or ""), _server_js_in(proc.get("cmdline"))
        if exe and js and fs.is_file(exe) and fs.is_file(js):
            return _result("process", True, "Stremio is running (%s) and started %s" % (names, js),
                           running=True, pids=pids, runtime_exe=exe, server_js=js, folder=dirname(js))
    folders = []
    for proc in mine:
        for source in (proc.get("exe"), _server_js_in(proc.get("cmdline"))):
            folder = dirname(source) if source else ""
            if folder and folder not in folders:
                folders.append(folder)
    for folder in folders:
        pair = find_server_files(folder, fs)
        if pair:
            return _result("process", True, "Stremio is running (%s) from %s" % (names, folder),
                           running=True, pids=pids, **pair)
    where = folders[0] if folders else ""
    return _result("process", False,
                   "Stremio is running (%s)%s but I found no server.js and runtime there"
                   % (names, (" from " + where) if where else ""),
                   running=True, pids=pids, folder=where)


# --------------------------------------------------------------------------- probe 3: Windows registry

def probe_registry(entries, fs=None):
    """`entries`: [{'name', 'location', 'icon', 'uninstall', 'version'}] taken from the uninstall list."""
    fs = fs or RealFs()
    mine = [e for e in (entries or []) if isinstance(e, dict) and "stremio" in str(e.get("name") or "").lower()]
    if not mine:
        return _result("registry", False, "Windows' list of installed programs has no Stremio entry")
    seen_folders = []
    for entry in mine:
        folders = [str(entry.get("location") or "").strip()]
        for key in ("icon", "uninstall"):
            path = clean_command_path(entry.get(key))
            if path:
                folders.append(dirname(path))
        for folder in folders:
            if not folder:
                continue
            if folder not in seen_folders:
                seen_folders.append(folder)
            pair = find_server_files(folder, fs)
            if pair:
                return _result("registry", True, "Windows lists %r, installed in %s" % (str(entry.get("name"))[:60], pair["folder"]),
                               version=str(entry.get("version") or ""), **pair)
    return _result("registry", False,
                   "Windows lists Stremio (installed in %s) but I found no server.js and runtime there"
                   % (seen_folders[0] if seen_folders else "an unknown folder"),
                   version=str(mine[0].get("version") or ""), folder=seen_folders[0] if seen_folders else "")


# --------------------------------------------------------------------------- probe 4: Start Menu shortcut

def parse_lnk(data):
    """The target, working folder and icon of a Windows .lnk file (MS-SHLLINK), or {} when it
    isn't one.  Only the fields the probe needs."""
    try:
        data = bytes(data)
        if len(data) < 76 or struct.unpack_from("<I", data, 0)[0] != 0x4C:
            return {}
        flags = struct.unpack_from("<I", data, 20)[0]
        pos = 76
        if flags & 0x01:                                    # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        target = ""
        if flags & 0x02:                                    # HasLinkInfo
            info = pos
            info_size, header_size, info_flags = struct.unpack_from("<III", data, info)
            if info_flags & 0x01:                           # VolumeIDAndLocalBasePath
                local_off = struct.unpack_from("<I", data, info + 16)[0]
                suffix_off = struct.unpack_from("<I", data, info + 24)[0]

                def cstr(offset, wide=False):
                    start = info + offset
                    if wide:
                        end = start
                        while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
                            end += 2
                        return data[start:end].decode("utf-16-le", "replace")
                    end = data.find(b"\x00", start)
                    return data[start:end if end >= 0 else len(data)].decode("cp1252", "replace")

                if header_size >= 0x24:
                    local_u = struct.unpack_from("<I", data, info + 28)[0]
                    suffix_u = struct.unpack_from("<I", data, info + 32)[0]
                    if local_u:
                        target = cstr(local_u, True) + (cstr(suffix_u, True) if suffix_u else "")
                if not target and local_off:
                    target = cstr(local_off) + (cstr(suffix_off) if suffix_off else "")
            pos = info + info_size
        unicode = bool(flags & 0x80)
        strings = {}
        for bit, name in ((0x04, "name"), (0x08, "relative"), (0x10, "workdir"), (0x20, "args"), (0x40, "icon")):
            if flags & bit:
                count = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                size = count * 2 if unicode else count
                raw = data[pos:pos + size]
                pos += size
                strings[name] = raw.decode("utf-16-le" if unicode else "cp1252", "replace")
        return {"target": target, "workdir": strings.get("workdir", ""), "icon": strings.get("icon", "")}
    except (struct.error, IndexError, ValueError):
        return {}


def probe_shortcuts(shortcuts, fs=None):
    """`shortcuts`: [{'path', 'target', 'workdir', 'icon'}] (already parsed)."""
    fs = fs or RealFs()
    mine = [s for s in (shortcuts or []) if isinstance(s, dict)]
    if not mine:
        return _result("shortcut", False, "no Stremio shortcut in the Start Menu")
    seen = []
    for item in mine:
        folders = [dirname(clean_command_path(item.get("target"))), str(item.get("workdir") or "").strip(),
                   dirname(clean_command_path(item.get("icon")))]
        for folder in folders:
            if not folder:
                continue
            if folder not in seen:
                seen.append(folder)
            pair = find_server_files(folder, fs)
            if pair:
                return _result("shortcut", True, "the Start Menu shortcut %s points into %s" % (basename(item.get("path") or "Stremio"), pair["folder"]), **pair)
    return _result("shortcut", False,
                   "the Start Menu has a Stremio shortcut (pointing into %s) but I found no server.js and runtime there"
                   % (seen[0] if seen else "an unknown folder"), folder=seen[0] if seen else "")


# --------------------------------------------------------------------------- probe 5: the usual folders

def fixed_folders(env=None, home=None):
    """The folders a Stremio desktop install usually lives in, most likely first."""
    env = os.environ if env is None else env
    out = []
    local = env.get("LOCALAPPDATA")
    if local:
        out.append(join(local, "Programs", "LNV", "Stremio-4"))
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = env.get(var)
        if base:
            out += [join(base, "Stremio-4"), join(base, "LNV", "Stremio-4"), join(base, "Stremio")]
    home = str(home) if home is not None else os.path.expanduser("~")
    out += [join(home, "AppData", "Local", "Programs", "LNV", "Stremio-4"),
            "/Applications/Stremio.app/Contents/MacOS", "/opt/stremio", "/usr/share/stremio", "/usr/libexec/stremio"]
    seen, unique = set(), []
    for item in out:
        key = item.replace("\\", "/").lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def probe_folders(folders, fs=None):
    fs = fs or RealFs()
    folders = list(folders or [])
    for folder in folders:
        pair = find_server_files(folder, fs)
        if pair:
            return _result("folder", True, "found in %s" % pair["folder"], **pair)
    return _result("folder", False, "looked in %d usual install folder(s); none has a Stremio runtime next to server.js" % len(folders),
                   looked=folders)


# --------------------------------------------------------------------------- the real readers (never raise)

def list_processes():
    """Stremio-looking running programs only (so no other program's command line is kept)."""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    try:
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                info = proc.info
                item = {"pid": info.get("pid"), "name": info.get("name") or "", "exe": info.get("exe") or "",
                        "cmdline": [str(a) for a in (info.get("cmdline") or [])][:12]}
                if is_stremio_process(item):
                    out.append(item)
            except Exception:  # noqa: BLE001 - a process that vanished or can't be read
                continue
    except Exception:  # noqa: BLE001
        return out
    return out


def listening_ports(pids):
    """Loopback / any-address TCP ports the given programs listen on (empty when psutil can't say)."""
    try:
        import psutil
    except ImportError:
        return []
    ports = []
    for pid in pids or []:
        try:
            proc = psutil.Process(int(pid))
            getter = getattr(proc, "net_connections", None) or proc.connections
            for conn in getter(kind="inet"):
                if str(conn.status).upper() == "LISTEN" and conn.laddr and conn.laddr.ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
                    if conn.laddr.port not in ports:
                        ports.append(conn.laddr.port)
        except Exception:  # noqa: BLE001
            continue
    return sorted(ports)


def read_uninstall_entries():
    """Stremio entries of Windows' uninstall list (all three places it lives).  [] off Windows."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import winreg
    except ImportError:
        return []
    out = []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            root = winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as key:
                        def value(name):
                            try:
                                return str(winreg.QueryValueEx(key, name)[0])
                            except OSError:
                                return ""
                        name = value("DisplayName")
                        if "stremio" in name.lower() or "stremio" in sub.lower():
                            out.append({"name": name or sub, "location": value("InstallLocation"), "icon": value("DisplayIcon"),
                                        "uninstall": value("UninstallString"), "version": value("DisplayVersion"), "key": sub})
                except OSError:
                    continue
        finally:
            try:
                winreg.CloseKey(root)
            except OSError:
                pass
    return out


def start_menu_folders(env=None):
    env = os.environ if env is None else env
    out = []
    for var in ("APPDATA", "PROGRAMDATA"):
        base = env.get(var)
        if base:
            out.append(join(base, "Microsoft", "Windows", "Start Menu", "Programs"))
    return out


def read_start_menu_shortcuts(folders=None, max_depth=2, max_found=12):
    """Parsed Stremio .lnk files found under the Start Menu folders.  [] off Windows or when none."""
    folders = start_menu_folders() if folders is None else folders
    out = []
    for top in folders:
        if not os.path.isdir(top):
            continue
        top_depth = top.rstrip("/\\").count(os.sep)
        for base, dirs, files in os.walk(top):
            if base.rstrip("/\\").count(os.sep) - top_depth >= max_depth:
                dirs[:] = []
            for name in files:
                if name.lower().endswith(".lnk") and "stremio" in name.lower():
                    path = os.path.join(base, name)
                    try:
                        with open(path, "rb") as handle:
                            parsed = parse_lnk(handle.read(65536))
                    except OSError:
                        continue
                    if parsed:
                        parsed["path"] = path
                        out.append(parsed)
                    if len(out) >= max_found:
                        return out
    return out


# --------------------------------------------------------------------------- putting it together

_ABSENT = object()


def find_files(processes=_ABSENT, registry=_ABSENT, shortcuts=_ABSENT, folders=_ABSENT, fs=None):
    """Probes 2 to 5, in order, until one finds the runtime + server.js pair.

    Each argument is the probe's input; leave it out and the real reader is used (only if that
    probe is reached).  Returns {'found', 'runtime_exe', 'server_js', 'folder', 'how', 'tried',
    'app_running', 'pids', 'install_folder', 'installed_version'}."""
    fs = fs or RealFs()
    tried = []
    out = {"found": False, "runtime_exe": "", "server_js": "", "folder": "", "how": "", "tried": tried,
           "app_running": False, "pids": [], "install_folder": "", "installed_version": ""}

    def note(result):
        tried.append(result)
        if result.get("folder") and not out["install_folder"]:
            out["install_folder"] = result["folder"]
        if result.get("version") and not out["installed_version"]:
            out["installed_version"] = result["version"]
        if result["found"]:
            out.update(found=True, runtime_exe=result["runtime_exe"], server_js=result["server_js"],
                       folder=result["folder"], how=result["probe"])
        return result["found"]

    procs = list_processes() if processes is _ABSENT else processes
    first = probe_process(procs, fs)
    out["app_running"] = bool(first.get("running"))
    out["pids"] = list(first.get("pids") or [])
    if note(first):
        return out
    reg = probe_registry(read_uninstall_entries() if registry is _ABSENT else registry, fs)
    if note(reg):
        return out
    short = probe_shortcuts(read_start_menu_shortcuts() if shortcuts is _ABSENT else shortcuts, fs)
    if note(short):
        return out
    note(probe_folders(fixed_folders() if folders is _ABSENT else folders, fs))
    return out


def detect(server_url, check=None, ports=_ABSENT, **inputs):
    """All the probes: the port first, then `find_files`.  See the module docstring.

    Returns {'server': {...the port probe...}, 'files': {...find_files...}, 'tried': [port probe,
    then the others], 'how': plain words for where the files were found}."""
    files = find_files(**inputs)
    if ports is _ABSENT:
        ports = [DEFAULT_PORT] + listening_ports(files.get("pids"))
    server = probe_port(server_url, check=check, other_ports=ports)
    tried = [server] + list(files["tried"])
    return {"server": server, "files": files, "tried": tried, "how": HOW.get(files["how"], "")}
