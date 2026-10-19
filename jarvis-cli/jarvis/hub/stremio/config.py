"""~/.jarvis/stremio.json: where the Stremio server and stremio-web live (plan section 13).

    {
      "server": {"url": "http://127.0.0.1:11470", "runtime_exe": "...stremio-runtime.exe", "server_js": "...server.js"},
      "web":    {"url": "http://127.0.0.1:8080", "dir": "D:/dev/stremio-web", "start_cmd": "pnpm start"},
      "player": {"kind": "auto", "exe": ""},
      "account": {"auth_key": "..."},                 # secrets live ONLY here; nothing in this
      "prefs": {...}                                  #   package reads or prints them
    }

Only the owner writes this file (there is deliberately no tool that does).  Two rules are
enforced on READ, so a hand-edit can't weaken them:

  * the server and web URLs must be http on a loopback address (plan section 14: "keep the
    Stremio server and web UI bound to 127.0.0.1").  The hub's SSRF allow-list entry for the
    server is derived from `server.url`, so a non-loopback host here would open the guard;
    such a value is ignored and the default is used (and `problems()` says so);
  * `JARVIS_STREMIO_SERVER_URL` (the tests, or a second server on another port) overrides the
    server URL but obeys the same rule.
"""

import copy
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .. import paths

DEFAULT_SERVER_URL = "http://127.0.0.1:11470"
DEFAULT_WEB_URL = "http://127.0.0.1:8080"
ENV_SERVER_URL = "JARVIS_STREMIO_SERVER_URL"
LOOPBACK = ("127.0.0.1", "localhost", "::1")

DEFAULTS = {
    "server": {"url": DEFAULT_SERVER_URL, "runtime_exe": "", "server_js": ""},
    "web": {"url": DEFAULT_WEB_URL, "dir": "", "start_cmd": "pnpm start"},
    "player": {"kind": "auto", "exe": ""},
    "account": {},
    "prefs": {},
}
_SECRET_KEYS = ("auth_key", "authkey", "password", "token", "cookie")


def config_path():
    return paths.jarvis_dir() / "stremio.json"


def _merge(base, over):
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load():
    """The merged config.  Never raises: a missing or unreadable file is the defaults."""
    stored = {}
    try:
        from ... import atomic_io
        data = atomic_io.read_json(config_path(), default={}, expect=dict)
        if isinstance(data, dict):
            stored = data
    except Exception:  # noqa: BLE001 - config is optional
        stored = {}
    return _merge(DEFAULTS, stored)


def _loopback_url(url):
    """The URL without a trailing slash if it is http on a loopback host, else None."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme.lower() != "http" or host not in LOOPBACK or parts.username or parts.password:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return "http://%s%s" % ("[%s]" % host if ":" in host else host, ":%d" % parts.port if parts.port else "")


def server_url(cfg=None):
    env = _loopback_url(os.environ.get(ENV_SERVER_URL))
    if env:
        return env
    cfg = cfg or load()
    return _loopback_url((cfg.get("server") or {}).get("url")) or DEFAULT_SERVER_URL


def web_url(cfg=None):
    cfg = cfg or load()
    return _loopback_url((cfg.get("web") or {}).get("url")) or DEFAULT_WEB_URL


def hostport(url):
    """'127.0.0.1:11470' for a URL (the shape netguard's allow-list and parse_server_url use)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return "%s:%d" % (host, port)


def server_hostport(cfg=None):
    return hostport(server_url(cfg))


def problems(cfg=None):
    """What is wrong with the file as written (for stack_doctor); [] when it is fine or absent."""
    out = []
    path = config_path()
    if not path.exists():
        return out
    cfg = cfg or load()
    raw_server = (cfg.get("server") or {}).get("url")
    if raw_server and _loopback_url(raw_server) is None:
        out.append("server.url must be http on 127.0.0.1 / localhost (got %r); the default is used" % str(raw_server)[:80])
    raw_web = (cfg.get("web") or {}).get("url")
    if raw_web and _loopback_url(raw_web) is None:
        out.append("web.url must be http on 127.0.0.1 / localhost (got %r); the default is used" % str(raw_web)[:80])
    return out


# --------------------------------------------------------------------------- finding the runtime

def _candidates():
    """Likely homes of the desktop app's server (stremio-runtime + server.js), most likely first."""
    out = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Programs" / "LNV" / "Stremio-4")
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(var)
        if base:
            out.append(Path(base) / "Stremio-4")
            out.append(Path(base) / "LNV" / "Stremio-4")
            out.append(Path(base) / "Stremio")
    home = Path.home()
    out += [home / "AppData" / "Local" / "Programs" / "LNV" / "Stremio-4",
            Path("/Applications/Stremio.app/Contents/MacOS"),
            Path("/opt/stremio"), Path("/usr/share/stremio"), Path("/usr/libexec/stremio")]
    return out


def detect_runtime():
    """(runtime_exe, server_js) of an installed Stremio, or ('', '')."""
    for folder in _candidates():
        try:
            for name in ("stremio-runtime.exe", "stremio-runtime", "node.exe", "node"):
                exe = folder / name
                js = folder / "server.js"
                if exe.is_file() and js.is_file():
                    return str(exe), str(js)
        except OSError:
            continue
    return "", ""


def runtime(cfg=None):
    """(runtime_exe, server_js, where): the configured pair if both files exist, else the detected
    one.  `where` is 'config', 'detected' or '' (not found)."""
    cfg = cfg or load()
    block = cfg.get("server") or {}
    exe, js = str(block.get("runtime_exe") or ""), str(block.get("server_js") or "")
    if exe and js and Path(exe).is_file() and Path(js).is_file():
        return exe, js, "config"
    exe, js = detect_runtime()
    if exe:
        return exe, js, "detected"
    return "", "", ""


def web_dir(cfg=None):
    cfg = cfg or load()
    folder = str((cfg.get("web") or {}).get("dir") or "")
    return folder if folder and Path(folder).is_dir() else ""


def web_command(cfg=None):
    """The configured start command for stremio-web (a string; the daemon runs it with a shell)."""
    cfg = cfg or load()
    return str((cfg.get("web") or {}).get("start_cmd") or "pnpm start").strip()[:200]


def player_prefs(cfg=None):
    cfg = cfg or load()
    block = cfg.get("player") or {}
    kind = str(block.get("kind") or "auto").lower()
    return (kind if kind in ("auto", "mpv", "vlc") else "auto"), str(block.get("exe") or "")


def public_view(cfg=None):
    """The config with every secret blanked, for tools that show it."""
    cfg = copy.deepcopy(cfg or load())

    def scrub(node):
        if isinstance(node, dict):
            for key in list(node):
                if str(key).lower() in _SECRET_KEYS:
                    node[key] = "(set)" if node[key] else ""
                else:
                    scrub(node[key])
    scrub(cfg)
    return cfg


def which(name):
    return shutil.which(name)
