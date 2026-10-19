"""Local players for playback tier 3 (plan 9.4): mpv or VLC on a finished file or on the
streaming server's URL.  This is the one tier where Jarvis can control playback afterwards, so
mpv is started with a JSON IPC endpoint (`--input-ipc-server`); nothing in phase 7 talks to it
yet (mpv IPC control is not built), but the endpoint is where a later phase will.

What may be played is decided HERE, not by the caller:

    * a file that exists, is under a folder the hub may write (library root and the like), and
      has a video or audio extension -- never a program, script or document;
    * `http://<the configured streaming server>/<40-hex infohash>/<file index or -1>` and nothing
      else (no query string, no other host), built by `links.stream_url`.

The player is started with an argument LIST (no shell), the target is never an option, and for
mpv a `--` precedes it.  Addon-supplied text never reaches a command line.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .. import naming, paths
from . import config as scfg
from . import links

PLAYER_FILE = "player.json"
_MEDIA_EXT = naming.VIDEO_EXT | naming.AUDIO_EXT


def state_path():
    return paths.hub_dir() / PLAYER_FILE


def ipc_endpoint():
    if sys.platform.startswith("win"):
        return r"\\.\pipe\jarvis-mpv"
    return str(paths.hub_dir() / "mpv.sock")


def _windows_candidates(kind):
    out = []
    pf = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")]
    local = os.environ.get("LOCALAPPDATA")
    home = Path.home()
    if kind == "mpv":
        out += [Path(p) / "mpv" / "mpv.exe" for p in pf if p]
        if local:
            out.append(Path(local) / "Programs" / "mpv" / "mpv.exe")
        out += [home / "scoop" / "apps" / "mpv" / "current" / "mpv.exe",
                Path("C:/ProgramData/chocolatey/bin/mpv.exe")]
    else:
        out += [Path(p) / "VideoLAN" / "VLC" / "vlc.exe" for p in pf if p]
    return out


def _mac_candidates(kind):
    if kind == "mpv":
        return [Path("/opt/homebrew/bin/mpv"), Path("/usr/local/bin/mpv"),
                Path("/Applications/mpv.app/Contents/MacOS/mpv")]
    return [Path("/Applications/VLC.app/Contents/MacOS/VLC")]


def find_one(kind):
    """The path of an installed mpv or vlc, or ''."""
    found = shutil.which(kind)
    if found:
        return found
    candidates = _windows_candidates(kind) if sys.platform.startswith("win") else _mac_candidates(kind)
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return ""


def kind_of(exe):
    name = os.path.basename(str(exe)).lower()
    if "vlc" in name:
        return "vlc"
    if "mpv" in name:
        return "mpv"
    return ""


def find(kind=None, configured=None):
    """(kind, exe, problem).  `kind` is auto / mpv / vlc; `configured` an exe path from config."""
    cfg_kind, cfg_exe = scfg.player_prefs()
    kind = str(kind or cfg_kind or "auto").lower()
    configured = configured if configured is not None else cfg_exe
    if kind not in ("auto", "mpv", "vlc"):
        return None, "", "player must be auto, mpv or vlc"
    if configured:
        try:
            ok = Path(configured).is_file()
        except OSError:
            ok = False
        if ok and (kind in ("auto", kind_of(configured)) or not kind_of(configured)):
            return (kind_of(configured) or (kind if kind != "auto" else "mpv")), str(configured), ""
    for candidate in (("mpv", "vlc") if kind == "auto" else (kind,)):
        exe = find_one(candidate)
        if exe:
            return candidate, exe, ""
    wanted = "mpv or VLC" if kind == "auto" else kind
    return None, "", ("I couldn't find %s on this PC. Install it (or set player.exe in ~/.jarvis/stremio.json) "
                      "and try again" % wanted)


def check_target(target, roots=(), server_url=None):
    """(ok, problem).  See the module docstring for what may be played."""
    text = str(target or "")
    if text.lower().startswith(("http://", "https://")):
        server_url = server_url or scfg.server_url()
        try:
            parts = urlsplit(text)
            same = scfg.hostport(text) == scfg.hostport(server_url)
        except ValueError:
            return False, "that isn't a valid link"
        pieces = [p for p in parts.path.split("/") if p]
        if not same or parts.scheme != "http" or parts.query or parts.fragment or len(pieces) != 2:
            return False, "only the Stremio server's own /<hash>/<file> links can be played this way"
        if len(pieces[0]) != 40 or not links.normalize_infohash(pieces[0]):
            return False, "that isn't a Stremio server stream link"
        try:
            int(pieces[1])
        except ValueError:
            return False, "that isn't a Stremio server stream link"
        return True, ""
    path = Path(text)
    try:
        if not path.is_file():
            return False, "that file isn't there any more"
    except OSError:
        return False, "that file can't be read"
    if path.suffix.lower() not in _MEDIA_EXT:
        return False, "only video and audio files are played (this one is %s)" % (path.suffix or "a file with no type")
    if roots and not naming.under_any(list(roots), str(path)):
        return False, "that file is outside the downloads folders, so I won't play it"
    return True, ""


def build_command(kind, exe, target, fullscreen=False, ipc=None):
    """The argv list.  Pure, so the tests can pin it."""
    if kind == "vlc":
        args = [exe]
        if fullscreen:
            args.append("--fullscreen")
        args.append(target)
        return args
    args = [exe, "--no-terminal", "--force-window=yes"]
    if ipc:
        args.append("--input-ipc-server=%s" % ipc)
    if fullscreen:
        args.append("--fs")
    return args + ["--", target]


def launch(kind, exe, target, fullscreen=False, popen=None):
    """Start the player and return at once.  {ok, pid, player, ipc} or {ok: False, error}."""
    popen = popen or subprocess.Popen
    ipc = ipc_endpoint() if kind == "mpv" else None
    argv = build_command(kind, exe, target, fullscreen=fullscreen, ipc=ipc)
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = popen(argv, **kwargs)
    except OSError as exc:
        return {"ok": False, "error": "couldn't start %s: %s" % (kind, exc)}
    pid = getattr(proc, "pid", None)
    record = {"pid": pid, "player": kind, "ipc": ipc, "started_at": time.time(),
              "target": "stream" if str(target).lower().startswith("http") else "file"}
    try:
        from ... import atomic_io
        paths.hub_dir().mkdir(parents=True, exist_ok=True)
        atomic_io.write_json(state_path(), record)
    except Exception:  # noqa: BLE001 - the record is a convenience
        pass
    return {"ok": True, "pid": pid, "player": kind, "ipc": ipc}


def open_in_browser(url, opener=None):
    """Open a stremio-web link in the default browser.  Only a link on the configured stremio-web
    address is ever opened, and only one this package built (the callers pass nothing the model or an
    addon wrote)."""
    base = scfg.web_url()
    if not str(url).startswith(base + "/"):
        return False, "refusing to open a link that isn't on stremio-web (%s)" % base
    try:
        if opener is None:
            import webbrowser
            opener = webbrowser.open
        return bool(opener(url)), ""
    except Exception as exc:  # noqa: BLE001
        return False, "couldn't open the browser: %s" % exc
