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
rather than showing an empty list.

Steam does NOT write BytesDownloaded while it downloads (N5).  A real run (2026-10-21, Windows,
one PC, 145 s) saw the manifest rewritten only every 20 to 50 s, while Steam's downloading folder
and its disk writes were changing all the time.  So:

  * a byte count from the manifest is "what Steam last reported, N s ago", never a live number:
    a running row carries no percent, no done-bytes, no speed and no ETA (speed_bps / eta_s are
    sent as None on purpose, so the hub does not derive a "speed" from the size of one burst);
  * a running row is never "stalled" from the manifest alone.  Two more signals are read, both
    read-only and cheap: the size / file count / newest-file time of steamapps/downloading/<id>
    (and temp/<id>) in the game's own library, and the disk-write counter of Steam's processes
    (psutil only).  An unreadable signal is "unavailable", never "flat";
  * "stalled" needs the manifest bytes AND every available second signal flat for STALL_SECONDS,
    and at least one second signal available.  A signal that says "moving" can only keep the row
    on "downloading" a little longer; it can never cause a stall.

  bits seen: 2 UpdateRequired, 4 FullyInstalled, 256 UpdateRunning, 512 UpdatePaused,
             1024 UpdateStarted, 1048576 Downloading, 2097152 Staging, 4194304 Committing

  paused        512 set and something is actually in flight (bytes known).  Steam also sets 512 on
                a pending update it has queued and not started, with every Bytes* field 0 or absent
                (seen on a real PC 2026-10-18: two Linux runtimes sat at 518 for minutes, then ran
                and finished with 0 bytes).  That is not a download: it is not listed, exactly like
                a bare 2, instead of a row stuck on "paused" with no percent that later "finishes"
                100% of nothing.  A download that was already listed and is then paused stays.
  installing    staging / committing set (a phase, not a download: the bar shows stage bytes)
  downloading   256 / 1024 / 1048576 set (the message says what Steam last reported and how old it is)
  stalled       ... and nothing moved for STALL_SECONDS: manifest bytes, downloading folder and Steam's
                disk writes (every one of them that can be read, at least one of them readable)
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
import time
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
# N5.  steam-checks.py (standard library only, imports nothing from the hub) keeps copies of
# STALL_SECONDS and of WRITES_MIN_BYTES_PER_S (as IO_NOISE_BYTES per 2 s tick);
# tests/test_hub_steam_alive.py fails if the copies drift apart.
STALL_SECONDS = 60.0              # manifest and every readable second signal flat this long = stalled
LOOK_GAP_SECONDS = 15.0           # a longer pause between two looks restarts every "flat since" timer
WRITES_MIN_BYTES_PER_S = 32 * 1024  # Steam's writes below this are noise, not a download
DIR_MAX_FILES = 4000              # downloading/<id> bigger than this: the folder signal is unavailable
DIR_MAX_DEPTH = 8                 # ... and so is one nested deeper than this (never a half-read "flat")
STEAM_PROCESS_NAMES = frozenset((
    "steam.exe", "steamwebhelper.exe", "steamservice.exe", "steam", "steam_osx", "steamwebhelper"))
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


# --------------------------------------------------------------------------- second signals (N5)

def read_folder_signal(library, appid, max_files=DIR_MAX_FILES, max_depth=DIR_MAX_DEPTH):
    """What is in steamapps/downloading/<appid> and steamapps/temp/<appid> of the game's own library.

    -> {"bytes", "files", "newest_mtime"} (summed over both folders), or None = UNAVAILABLE: neither
    folder exists, a read failed, or the tree is bigger than the caps (a half-read folder must never
    look "flat").  Read-only, never follows links, stops at the caps, so one look stays cheap."""
    total = {"bytes": 0, "files": 0, "newest_mtime": 0.0}
    found = False
    try:
        for sub in ("downloading", "temp"):
            root = Path(library) / "steamapps" / sub / str(appid)
            if not root.is_dir():
                continue
            found = True
            stack = [(str(root), 0)]
            while stack:
                here, depth = stack.pop()
                with os.scandir(here) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth + 1 > max_depth:
                                return None
                            stack.append((entry.path, depth + 1))
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        total["files"] += 1
                        if total["files"] > max_files:
                            return None
                        total["bytes"] += stat.st_size
                        total["newest_mtime"] = max(total["newest_mtime"], stat.st_mtime)
    except (OSError, ValueError):
        return None
    return total if found else None


def read_steam_writes():
    """Total bytes written so far by Steam's processes, read with psutil only (no PowerShell, no
    subprocess).  None = UNAVAILABLE: no psutil, no readable process, or the platform has no counter."""
    try:
        import psutil
    except ImportError:
        return None
    total, readable = 0, 0
    try:
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() not in STEAM_PROCESS_NAMES:
                continue
            try:
                total += int(proc.io_counters().write_bytes)
                readable += 1
            except (psutil.Error, AttributeError, OSError, ValueError):
                continue
    except Exception:  # noqa: BLE001 - whatever went wrong, "can't tell" is the honest answer
        return None
    return total if readable else None


def _signal_moved(name, before, now_value, seconds):
    """Did one second signal change between two looks?  (A signal that moved can only keep a row
    on "downloading", so every doubt here is answered "moved".)"""
    if name == "folder":
        return (now_value["bytes"] != before["bytes"] or now_value["files"] != before["files"]
                or now_value["newest_mtime"] > before["newest_mtime"])
    # writes: a counter that went DOWN (a Steam process ended) is a change too
    return now_value < before or now_value - before > WRITES_MIN_BYTES_PER_S * max(0.0, seconds)


def _look(memory, now, signals):
    """Record this look's second signals in the app's memory.

    `signals` is {"folder": snapshot or None, "writes": total or None}; None = unavailable.
    Returns {"folder": seconds since it last moved or None, "writes": same}.  The first look, a
    look after a gap longer than LOOK_GAP_SECONDS, a signal that was unavailable a moment ago and
    a clock that jumped back all count as "moved just now": flat is only ever PROVEN by watching."""
    signals = signals if isinstance(signals, dict) else {}
    before_at = memory.get("look_at")
    gap = None if before_at is None else now - before_at
    restart = gap is None or gap < 0 or gap > LOOK_GAP_SECONDS
    flat = {}
    for name in ("folder", "writes"):
        value = signals.get(name)
        before = memory.get(name)
        memory[name] = value
        if value is None:
            flat[name] = None
            continue
        if restart or before is None or _signal_moved(name, before, value, gap):
            memory[name + "_moved_at"] = now
        flat[name] = now - memory.get(name + "_moved_at", now)
    memory["look_at"] = now
    return flat


_SIGNAL_LABELS = (("folder", "downloading folder"), ("writes", "Steam disk writes"))


def _ago(seconds):
    seconds = max(0, int(seconds))
    return "%d s" % seconds if seconds < 90 else "%d min" % (seconds // 60)


def _size(count):
    value = float(max(0, count))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0


def _flags_of(manifest):
    state = (manifest or {}).get("AppState") if isinstance(manifest, dict) and "AppState" in manifest else manifest
    return _int(_ci(state if isinstance(state, dict) else {}, "StateFlags"))


def is_running_download(manifest):
    """True when classify would read this manifest's second signals (a transfer is running; not
    paused, not staging).  Lets the provider read the folder only for apps that need it."""
    flags = _flags_of(manifest)
    return bool(flags & _RUNNING) and not flags & (FLAG_UPDATE_PAUSED | _STAGING)


# --------------------------------------------------------------------------- one manifest -> one row

def classify(manifest, memory=None, now=None, signals=None):
    """One parsed appmanifest -> a row dict for the hub, or None when nothing is being fetched.

    `memory` is this app's dict, kept by the caller between polls and updated here ("bytes" and
    "staged": the last manifest numbers, "bytes_changed_at": when they last changed, plus what
    `_look` keeps).  `now` is the poll's clock (seconds); `signals` is {"folder": ..., "writes": ...}
    as read_folder_signal / read_steam_writes return them (None = unavailable).  Without signals a
    running row is never "stalled".
    """
    now = time.time() if now is None else float(now)
    state = (manifest or {}).get("AppState") if isinstance(manifest, dict) and "AppState" in manifest else manifest
    state = state if isinstance(state, dict) else {}
    appid = str(_ci(state, "appid", "")).strip()
    if not appid.isdigit():
        return None
    flags = _int(_ci(state, "StateFlags"))
    to_dl, got = _int(_ci(state, "BytesToDownload")), _int(_ci(state, "BytesDownloaded"))
    to_stage, staged = _int(_ci(state, "BytesToStage")), _int(_ci(state, "BytesStaged"))
    memory = memory if memory is not None else {}
    flat = None
    previous = memory.get("bytes")
    moved = previous is None or got != previous or staged != memory.get("staged")
    memory["bytes"], memory["staged"] = got, staged
    if moved:
        memory["bytes_changed_at"] = now
        if previous is not None:
            memory["bytes_seen_change"] = True      # Steam really rewrote them while we were looking
    first = not memory.get("seen")
    memory["seen"] = True

    name = str(_ci(state, "name", "") or "App %s" % appid)
    row = {"id": appid, "title": name, "kind": "game"}
    update = _int(_ci(state, "LastUpdated")) > 0 or _int(_ci(state, "SizeOnDisk")) > 0
    if first:
        memory["update"] = update
    row["subtitle"] = "update" if memory.get("update") else "install"

    if flags & FLAG_UPDATE_PAUSED:
        if to_dl <= 0 and got <= 0 and to_stage <= 0 and staged <= 0 and not memory.get("active"):
            memory["active"] = False         # a queued background update, not a download (see the module docstring)
            return None
        row.update(state="paused", message="paused in Steam")
    elif flags & _STAGING:
        row.update(state="postprocessing", phase="installing", percent=_pct(staged, to_stage, 99.0))
    elif flags & _RUNNING:
        flat = _look(memory, now, signals)
        if to_dl <= 0 and got <= 0:
            row.update(state="queued", message="waiting in Steam's own queue")
        else:
            # What Steam last wrote is a past fact, not a live number: no percent, no done-bytes, no
            # speed, no ETA.  The explicit Nones keep the hub from deriving a "speed" out of the size
            # of one burst of bytes (that measures when Steam writes the file, not how fast it goes).
            row.update(phase=None, percent=None, bytes_done=None, speed_bps=None, eta_s=None)
            if to_dl > 0:
                row["bytes_total"] = to_dl
            available = [v for v in flat.values() if v is not None]
            quiet = now - memory.get("bytes_changed_at", now)
            if got < to_dl and available and min([quiet] + available) >= STALL_SECONDS:
                checked = ["manifest"] + [label for key, label in _SIGNAL_LABELS if flat[key] is not None]
                row.update(state="stalled", percent=_pct(got, to_dl, 99.0), bytes_done=min(got, to_dl),
                           message="nothing moved for %s (%s); Steam may be waiting"
                                   % (_ago(min([quiet] + available)), ", ".join(checked)))
            else:
                row.update(state="downloading", message=_last_report(got, to_dl, memory, now, bool(available)))
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
    if flat is not None:
        row["detail"].append({"label": "Other signs of life", "value": "; ".join(
            "%s %s" % (label, "unavailable" if flat[key] is None else "last moved %s ago" % _ago(flat[key]))
            for key, label in _SIGNAL_LABELS)})
    return row


def _last_report(got, to_dl, memory, now, have_signals):
    """The honest sentence for a running row whose only numbers are the manifest's."""
    if got <= 0:
        text = "Steam hasn't reported any progress yet"
    else:
        age = now - memory.get("bytes_changed_at", now)
        if memory.get("bytes_seen_change"):
            when = "reported %s ago" % _ago(age)
        elif age >= 1:
            when = "reported at least %s ago" % _ago(age)
        else:
            when = "as Steam had written it when Jarvis first looked"
        sizes = "%.1f %% (%s of %s)" % (_pct(got, to_dl, 99.0), _size(got), _size(to_dl)) if to_dl > 0 else _size(got)
        text = "%s, %s" % (sizes, when)
    text += "; Steam isn't reporting live progress"
    if not have_signals:
        text += " and Jarvis can't see any other sign of activity"
    return text


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


def _path_key(path):
    """Two spellings of one folder compare equal (case on Windows, `..`, a trailing slash)."""
    return os.path.normcase(os.path.normpath(str(path)))


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
                if not (path / "steamapps").is_dir():
                    continue
                # Windows paths are case-blind and the registry says `c:/program files (x86)/steam`
                # while libraryfolders.vdf says `C:\Program Files (x86)\Steam`: one library, not two
                # (a real PC showed "3 libraries" for 2, scanned the Steam folder twice and listed
                # two open roots).  The first entry is the Steam folder itself: keep Steam's own
                # spelling of it from the vdf, so the paths shown in rows read properly.
                key = _path_key(path)
                same = [i for i, lib in enumerate(libs) if _path_key(lib) == key]
                if not same:
                    libs.append(str(path))
                elif same[0] == 0:
                    libs[0] = str(path)
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


def parse_manifests(files, memory=None, now=None, signals=None):
    """The pure function the plan asks for: parsed manifests in, rows out.

    `files`: {appid: (library, installdir, parsed manifest)} as read_manifests returns.
    `now`: the poll's clock; `signals`: {appid: {"folder": ..., "writes": ...}} (see classify).
    Returns [row] for what is being fetched; install folders are filled in as `dest`."""
    memory = memory if memory is not None else {}
    now = time.time() if now is None else now
    signals = signals if isinstance(signals, dict) else {}
    rows = []
    for appid, (lib, installdir, parsed) in sorted(files.items()):
        row = classify(parsed, memory.setdefault(appid, {}), now, signals.get(appid))
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
        self.read_folder = read_folder_signal   # tests: (library, appid) -> snapshot or None
        self.read_writes = read_steam_writes    # tests: () -> bytes written so far or None
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

    def _signals(self, files):
        """The second signals, read only for the apps that are running a transfer right now (the poll
        runs every ~3 s, so an idle Steam costs nothing).  Any failure = unavailable (None)."""
        out, writes, read = {}, None, False
        for appid, (lib, _installdir, parsed) in files.items():
            if not is_running_download(parsed):
                continue
            if not read:
                read = True
                try:
                    writes = self.read_writes()
                except Exception:  # noqa: BLE001
                    writes = None
            try:
                folder = self.read_folder(lib, appid)
            except Exception:  # noqa: BLE001
                folder = None
            out[appid] = {"folder": folder, "writes": writes}
        return out

    def poll(self, store, now=None):
        now = time.time() if now is None else now
        steam, libs = find_libraries(self._root(store), self.candidates)
        if not steam:
            return {"error": "Steam wasn't found (set its folder with dl_provider_prefs, or JARVIS_STEAM_ROOT)"}
        files = read_manifests(libs, self._cache)
        if not files:
            return {"error": "no appmanifest files found in %d Steam librar%s" % (len(libs), "y" if len(libs) == 1 else "ies")}
        for gone in set(self._memory) - set(files):
            self._memory.pop(gone, None)
        rows = parse_manifests(files, self._memory, now, self._signals(files))
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
