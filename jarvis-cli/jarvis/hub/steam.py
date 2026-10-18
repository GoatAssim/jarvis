"""The Steam provider (hub plan 6.1 - 6.4): installs and updates that Steam is doing, shown in
Menu > Downloads however they were started (Steam's own button, Playnite, `playnite_install_game`).

Steam offers no supported external control and no progress API, so this READS its files:

  <Steam>/steamapps/libraryfolders.vdf        where the libraries are
  <library>/steamapps/appmanifest_<id>.acf    one per game: StateFlags, BytesToDownload,
                                              BytesDownloaded, BytesToStage, BytesStaged,
                                              SizeOnDisk, LastUpdated, name, installdir

Read-only and never touching Steam: no pause, cancel or priority (the plan never simulates one),
only a Reveal button for the install folder and an "Open in Steam" link.

Honesty about what is known (plan P2, P3).  These are Steam's undocumented files: the field
names are stable in practice but the `StateFlags` bits are community knowledge, so they are
HINTS.  The mapping below is one small table (`classify`) with fixtures in the tests; if a Steam
update changes the files, only this module is wrong, and the hub says "no manifests found"
rather than showing an empty list.  Speed and ETA are not stored by Steam: they are left out of
the report so the hub derives them from successive byte counts (shown with "≈").

  bits seen: 2 UpdateRequired, 4 FullyInstalled, 256 UpdateRunning, 512 UpdatePaused,
             1024 UpdateStarted, 1048576 Downloading, 2097152 Staging, 4194304 Committing

  paused        512 set
  installing    staging / committing set (a phase, not a download: the bar shows stage bytes)
  downloading   256 / 1024 / 1048576 set and the byte count moved since the last look
  stalled       ... and it did not move for 3 looks in a row
  queued        ... but nothing has been fetched yet (Steam's own queue)
  done           it was being fetched and Steam now says fully installed (4): reported once
  not listed    everything else, including a game that merely has an update PENDING (2 alone):
                a pending update is not a download

Not done here (plan 6.1 mentions it): taking the friendly name / cover from the Playnite game
record.  The manifest's own name is used.  Steam closed: rows go stale (the poll reports an
error and writes nothing), never failed.
"""

import os
import re
import sys
from pathlib import Path

from . import providers, states

FLAG_UPDATE_REQUIRED = 2
FLAG_UPDATE_RUNNING = 256
FLAG_UPDATE_PAUSED = 512
FLAG_UPDATE_STARTED = 1024
FLAG_DOWNLOADING = 1048576
FLAG_STAGING = 2097152
FLAG_COMMITTING = 4194304
_RUNNING = FLAG_UPDATE_RUNNING | FLAG_UPDATE_STARTED | FLAG_DOWNLOADING
_STAGING = FLAG_STAGING | FLAG_COMMITTING
STALL_LOOKS = 3
MAX_MANIFESTS = 400
MAX_FILE_BYTES = 256 * 1024


# --------------------------------------------------------------------------- the .acf / .vdf format

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text):
    """Valve's key/value text -> nested dict (keys as written).  Tolerant: garbage in, {} out."""
    root, stack = {}, []
    current, key = root, None
    stack = [root]
    for match in _TOKEN.finditer(text or ""):
        quoted, brace = match.group(1), match.group(2)
        if quoted is not None:
            value = re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), quoted)
            if key is None:
                key = value
            else:
                current[key] = value
                key = None
        elif brace == "{":
            child = {}
            if key is not None:
                current[key] = child
                key = None
            stack.append(child)
            current = child
        else:
            if len(stack) > 1:
                stack.pop()
            current = stack[-1]
            key = None
    return root


def _ci(mapping, name, default=None):
    """Case-insensitive lookup (Steam writes `StateFlags`, `appid`, `SizeOnDisk` ...)."""
    low = name.lower()
    for k, v in (mapping or {}).items():
        if str(k).lower() == low:
            return v
    return default


def _int(value, default=0):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_libraries(text):
    """Library folder paths out of libraryfolders.vdf (new nested format and the old flat one)."""
    tree = parse_vdf(text)
    node = _ci(tree, "libraryfolders", tree)
    out = []
    for key, value in (node or {}).items():
        if not str(key).isdigit():
            continue
        path = _ci(value, "path") if isinstance(value, dict) else value
        if isinstance(path, str) and path.strip():
            out.append(path.strip())
    return out


# --------------------------------------------------------------------------- one manifest -> one row

def classify(manifest, memory=None):
    """One parsed appmanifest -> a row dict for the hub, or None when nothing is being fetched.

    `memory` is this app's {"bytes": last BytesDownloaded, "still": looks without movement,
    "seen": True once looked at before}; the caller keeps it between polls.  It is updated here.
    """
    state = (manifest or {}).get("AppState") if isinstance(manifest, dict) and "AppState" in manifest else manifest
    state = state if isinstance(state, dict) else {}
    appid = str(_ci(state, "appid", "")).strip()
    if not appid.isdigit():
        return None
    flags = _int(_ci(state, "StateFlags"))
    to_dl, got = _int(_ci(state, "BytesToDownload")), _int(_ci(state, "BytesDownloaded"))
    to_stage, staged = _int(_ci(state, "BytesToStage")), _int(_ci(state, "BytesStaged"))
    memory = memory if memory is not None else {}
    previous = memory.get("bytes")
    moved = previous is None or got != previous or staged != memory.get("staged")
    memory["bytes"], memory["staged"] = got, staged
    memory["still"] = 0 if moved else memory.get("still", 0) + 1
    first = not memory.get("seen")
    memory["seen"] = True

    name = str(_ci(state, "name", "") or "App %s" % appid)
    row = {"id": appid, "title": name, "kind": "game"}
    update = _int(_ci(state, "LastUpdated")) > 0 or _int(_ci(state, "SizeOnDisk")) > 0
    if first:
        memory["update"] = update
    row["subtitle"] = "update" if memory.get("update") else "install"

    if flags & FLAG_UPDATE_PAUSED:
        row.update(state="paused", message="paused in Steam")
    elif flags & _STAGING:
        row.update(state="postprocessing", phase="installing", percent=_pct(staged, to_stage, 99.0))
    elif flags & _RUNNING:
        if to_dl <= 0 and got <= 0:
            row.update(state="queued", message="waiting in Steam's own queue")
        elif memory["still"] >= STALL_LOOKS and got < to_dl:
            row.update(state="stalled", message="no progress for %d looks; Steam may be waiting" % memory["still"])
        else:
            row.update(state="downloading", phase=None)
        if to_dl > 0:
            row["bytes_done"], row["bytes_total"] = min(got, to_dl), to_dl
            row["percent"] = _pct(got, to_dl, 99.0)
    else:
        # Not fetching anything now.  If we were watching it and Steam now calls it fully
        # installed, it finished: say so once (a later poll no longer lists it).  Anything else
        # that vanished (uninstalled, cancelled) is handled by the poll's `complete` rule.
        was_active, memory["active"] = memory.get("active"), False
        if was_active and flags & 4:
            row.update(state="done", percent=100, message=None, phase=None)
            row["detail"] = [{"label": "App id", "value": appid}, {"label": "Steam flags", "value": str(flags)}]
            return row
        return None
    memory["active"] = True
    row["detail"] = [{"label": "App id", "value": appid}, {"label": "Steam flags", "value": str(flags)}]
    return row


def _pct(done, total, ceiling):
    """Percent of a transfer, held under 100 until Steam says it is finished."""
    if total <= 0:
        return None
    return round(min(ceiling, 100.0 * done / total), 1)


# --------------------------------------------------------------------------- finding Steam

def default_roots():
    env = os.environ.get("JARVIS_STEAM_ROOT", "").strip()
    out = [env] if env else []
    home = Path.home()
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                out.append(winreg.QueryValueEx(key, "SteamPath")[0])
        except (ImportError, OSError):
            pass
        out += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    elif sys.platform == "darwin":
        out.append(str(home / "Library" / "Application Support" / "Steam"))
    else:
        out += [str(home / ".steam" / "steam"), str(home / ".local" / "share" / "Steam"),
                str(home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam")]
    return out


def find_libraries(root=None, candidates=None):
    """(steam root or None, [library paths]).  Every library in libraryfolders.vdf that exists,
    plus the Steam folder itself."""
    for cand in ([root] if root else (candidates if candidates is not None else default_roots())):
        if not cand:
            continue
        steam = Path(cand)
        apps = steam / "steamapps"
        if not apps.is_dir():
            continue
        libs = [str(steam)]
        try:
            text = (apps / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
            for extra in parse_libraries(text):
                path = Path(extra.replace("\\\\", "\\"))
                if (path / "steamapps").is_dir() and str(path) not in libs:
                    libs.append(str(path))
        except OSError:
            pass
        return str(steam), libs
    return None, []


def read_manifests(libraries, cache=None):
    """{appid: (library, install folder name, parsed manifest)} for every appmanifest found.
    `cache` ({path: (mtime, size, parsed)}) avoids re-parsing a file that did not change."""
    cache = cache if cache is not None else {}
    out, count = {}, 0
    for lib in libraries:
        folder = Path(lib) / "steamapps"
        try:
            names = sorted(n for n in os.listdir(folder) if n.startswith("appmanifest_") and n.endswith(".acf"))
        except OSError:
            continue
        for name in names:
            if count >= MAX_MANIFESTS:
                return out
            path = folder / name
            try:
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES:
                    continue
                hit = cache.get(str(path))
                if hit and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
                    parsed = hit[2]
                else:
                    parsed = parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
                    cache[str(path)] = (stat.st_mtime, stat.st_size, parsed)
            except OSError:
                continue
            count += 1
            state = _ci(parsed, "AppState", parsed)
            appid = str(_ci(state, "appid", ""))
            if appid.isdigit():
                out[appid] = (lib, str(_ci(state, "installdir", "") or ""), parsed)
    return out


def parse_manifests(files, memory=None):
    """The pure function the plan asks for: parsed manifests in, rows out.

    `files`: {appid: (library, installdir, parsed manifest)} as read_manifests returns.
    Returns [row] for what is being fetched; install folders are filled in as `dest`."""
    memory = memory if memory is not None else {}
    rows = []
    for appid, (lib, installdir, parsed) in sorted(files.items()):
        row = classify(parsed, memory.setdefault(appid, {}))
        if row is None:
            continue
        if installdir:
            row["dest"] = str(Path(lib) / "steamapps" / "common" / installdir)
        row["detail"].append({"label": "Library", "value": lib})
        row["links"] = [{"label": "Open in Steam", "kind": "url", "target": "steam://nav/downloads"}]
        rows.append(row)
    return rows


def steam_running():
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() in ("steam.exe", "steam", "steam_osx", "steamwebhelper.exe",
                                                         "steamwebhelper"):
                return True
    except Exception:  # noqa: BLE001 - can't tell: assume it is, so we never hide real progress
        return True
    return False


# --------------------------------------------------------------------------- the provider

class SteamProvider(providers.BuiltinProvider):
    id = "steam"

    def __init__(self):
        self.root_override = None          # tests
        self.candidates = None             # tests
        self.is_running = steam_running    # tests
        self._memory, self._cache = {}, {}
        self._active = False

    def _root(self, store):
        if self.root_override is not None:
            return self.root_override
        try:
            return ((store.get_provider("steam") or {}).get("settings") or {}).get("root") or None
        except Exception:  # noqa: BLE001
            return None

    def registration(self, store):
        steam, libs = find_libraries(self._root(store), self.candidates)
        if not steam:
            return None
        roots = [str(Path(lib) / "steamapps" / "common") for lib in libs]
        return {"label": "Steam", "mode": "poll", "kind": "game", "icon": "S", "capabilities": ["reveal"],
                "open_roots": roots, "interval_s": 3.0, "settings": {"root": ""}}

    def idle(self, store):
        return not self._active

    def poll(self, store, now):
        steam, libs = find_libraries(self._root(store), self.candidates)
        if not steam:
            return {"error": "Steam wasn't found (set its folder with dl_provider_prefs, or JARVIS_STEAM_ROOT)"}
        files = read_manifests(libs, self._cache)
        if not files:
            return {"error": "no appmanifest files found in %d Steam librar%s" % (len(libs), "y" if len(libs) == 1 else "ies")}
        for gone in set(self._memory) - set(files):
            self._memory.pop(gone, None)
        rows = parse_manifests(files, self._memory)
        self._active = bool(rows)
        if rows and not self.is_running():
            self._active = False
            return {"error": "Steam isn't running; its downloads are shown as not updating"}
        return {"items": rows, "complete": True}


PROVIDER = SteamProvider()


def doctor(store):
    """hub_doctor's lines for Steam (plan P2: say 'no manifests found', never an empty list)."""
    steam, libs = find_libraries(PROVIDER._root(store), PROVIDER.candidates)
    if not steam:
        return [{"name": "steam", "status": "ok", "detail": "Steam wasn't found on this PC; the Steam provider is idle"}]
    files = read_manifests(libs, {})
    if not files:
        return [{"name": "steam", "status": "warn", "detail": "Steam found at %s but no appmanifest files in its libraries" % steam}]
    return [{"name": "steam", "status": "ok", "detail": "%d librar%s, %d game manifest(s)"
             % (len(libs), "y" if len(libs) == 1 else "ies", len(files))}]
