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

  * by default the server and web URLs must be http on a loopback address (plan section 14: "keep
    the Stremio server and web UI bound to 127.0.0.1").  The hub's SSRF allow-list entry for the
    server is derived from `server.url`, so a non-loopback host here would open the guard;
    such a value is ignored and the default is used (and `problems()` says so).  The owner can
    switch that rule off with `network.allow_remote` (the "Allow addresses other than this PC" box,
    saved only by the owner's own button): then http AND https addresses on any host are accepted
    (a Tailscale / LAN address, a name, Stremio's own web.stremio.com), still with no user name,
    password, path or query in them;
  * `JARVIS_STREMIO_SERVER_URL` (the tests, or a second server on another port) overrides the
    server URL but obeys the same rule.

    "network": {"allow_remote": false},               # off unless the owner ticked the box
    "open":    {"with": "browser"}                    # "browser" or "native" (the Stremio app, stremio:// links)
"""

import copy
import ipaddress
import json
import os
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

from .. import paths
from . import detect

DEFAULT_SERVER_URL = "http://127.0.0.1:11470"
DEFAULT_WEB_URL = "http://127.0.0.1:8080"
ENV_SERVER_URL = "JARVIS_STREMIO_SERVER_URL"
STREMIO_SITE_URL = "https://web.stremio.com"       # Stremio's own hosted web app; a preset for the web address (needs allow_remote)
LOOPBACK = ("127.0.0.1", "localhost", "::1")

DEFAULTS = {
    "server": {"url": DEFAULT_SERVER_URL, "runtime_exe": "", "server_js": ""},
    "web": {"url": DEFAULT_WEB_URL, "dir": "", "start_cmd": "pnpm start"},
    "player": {"kind": "auto", "exe": ""},
    "account": {},
    "prefs": {},
    "network": {"allow_remote": False},
    "open": {"with": "browser"},
}
OPEN_WITH = ("browser", "native")
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


_HOST_CHARS = re.compile(r"^[a-z0-9._:\-]+$")


def _remote_url(url):
    """Like `_loopback_url` but for any host and http or https (only used when the owner allowed it): the URL without
    a trailing slash, or None.  Still no user name, password, path, query or fragment."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host or not _HOST_CHARS.match(host) or parts.username or parts.password:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return "%s://%s%s" % (scheme, "[%s]" % host if ":" in host else host, ":%d" % parts.port if parts.port else "")


def allow_remote(cfg=None):
    """True only when the owner ticked "Allow addresses other than this PC" (network.allow_remote is exactly true)."""
    cfg = cfg if isinstance(cfg, dict) else load()
    block = cfg.get("network")
    return isinstance(block, dict) and block.get("allow_remote") is True


def _address_url(url, remote_ok=False):
    """The one URL check for the server and web addresses: loopback http, or (remote_ok) any http/https host."""
    return _remote_url(url) if remote_ok else _loopback_url(url)


def _address_error(label, got, remote_ok):
    """The refusal sentence for a bad address.  Keeps the old wording when remote addresses are off and says how to
    allow them; says what IS accepted when they are on."""
    if remote_ok:
        return "the %s must be an http or https address with just a host and a port, like https://100.72.25.9:8080 (got %r)" % (label, str(got)[:80])
    return ("the %s must be http on 127.0.0.1 or localhost (got %r). To use another address, tick 'Allow addresses other than "
            "this PC' and save again" % (label, str(got)[:80]))


def is_this_pc(url):
    """True when the URL's host is this PC: a loopback name or an address that belongs to one of this PC's own network
    cards (a Tailscale or LAN address included).  Found by trying to bind that address, so a name is resolved but nothing is
    ever contacted.  A host that is somewhere else (web.stremio.com, another machine) is False."""
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in LOOPBACK:
        return True
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})[:8]
        except (OSError, UnicodeError):
            return False
    for ip in addresses:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback:
            return True
        sock = socket.socket(socket.AF_INET6 if addr.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((ip, 0))
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def open_with_value(cfg=None):
    """'browser' (default) or 'native': where playback links open (the default browser, or the Stremio app)."""
    cfg = cfg if isinstance(cfg, dict) else load()
    block = cfg.get("open")
    value = str(block.get("with") or "").strip().lower() if isinstance(block, dict) else ""
    return value if value in OPEN_WITH else "browser"


open_with = open_with_value


def server_url(cfg=None):
    cfg = cfg or load()
    remote = allow_remote(cfg)
    env = _address_url(os.environ.get(ENV_SERVER_URL), remote)
    if env:
        return env
    return _address_url((cfg.get("server") or {}).get("url"), remote) or DEFAULT_SERVER_URL


def web_url(cfg=None):
    cfg = cfg or load()
    return _address_url((cfg.get("web") or {}).get("url"), allow_remote(cfg)) or DEFAULT_WEB_URL


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
    remote = allow_remote(cfg)
    raw_server = (cfg.get("server") or {}).get("url")
    if raw_server and _address_url(raw_server, remote) is None:
        out.append(("server.url must be an http or https host and port (got %r); the default is used" if remote else
                    "server.url must be http on 127.0.0.1 / localhost unless network.allow_remote is on (got %r); the default is used")
                   % str(raw_server)[:80])
    raw_web = (cfg.get("web") or {}).get("url")
    if raw_web and _address_url(raw_web, remote) is None:
        out.append(("web.url must be an http or https host and port (got %r); the default is used" if remote else
                    "web.url must be http on 127.0.0.1 / localhost unless network.allow_remote is on (got %r); the default is used")
                   % str(raw_web)[:80])
    block = cfg.get("server") or {}
    for key, label in (("runtime_exe", "the Stremio program"), ("server_js", "server.js")):
        saved = str(block.get(key) or "")
        if saved and not Path(saved).is_file():
            out.append("the saved path for %s no longer exists (%s); Jarvis looks for Stremio by itself instead. "
                       "Use Locate... to save the right one" % (label, saved[:120]))
    if bool(block.get("runtime_exe")) != bool(block.get("server_js")):
        out.append("only one of server.runtime_exe and server.server_js is saved; both are needed, so Jarvis looks for Stremio by itself")
    return out


# --------------------------------------------------------------------------- finding the runtime

def detect_runtime(**inputs):
    """(runtime_exe, server_js) of an installed Stremio found without any saved path, or ('', '').
    Runs the probes in `detect.py` (a running Stremio, Windows' install list, the Start Menu
    shortcut, the usual folders); `inputs` lets a test feed fake processes, registry values,
    shortcuts and folders."""
    found = detect.find_files(**inputs)
    return (found["runtime_exe"], found["server_js"]) if found["found"] else ("", "")


def configured_pair(cfg=None):
    """(runtime_exe, server_js) when both saved paths are set and both files exist, else None."""
    cfg = cfg or load()
    block = cfg.get("server") or {}
    exe, js = str(block.get("runtime_exe") or ""), str(block.get("server_js") or "")
    if exe and js and Path(exe).is_file() and Path(js).is_file():
        return exe, js
    return None


def runtime_info(cfg=None, **inputs):
    """What `runtime()` knows, in full: {'found', 'runtime_exe', 'server_js', 'folder', 'where',
    'how', 'tried', 'app_running', 'install_folder', ...}.  `where` is 'config' (both saved paths
    exist), the probe that found it ('process', 'registry', 'shortcut', 'folder') or '' (not found)."""
    cfg = cfg or load()
    pair = configured_pair(cfg)
    if pair:
        exe, js = pair
        return {"found": True, "runtime_exe": exe, "server_js": js, "folder": detect.dirname(js), "where": "config",
                "how": detect.HOW["config"], "tried": [], "app_running": False, "pids": [], "install_folder": detect.dirname(js),
                "installed_version": ""}
    info = detect.find_files(**inputs)
    info["where"] = info["how"]
    info["how"] = detect.HOW.get(info["where"], "")
    return info


def runtime(cfg=None):
    """(runtime_exe, server_js, where): the saved pair if both files exist, else the one the probes
    found.  `where` is 'config', 'process', 'registry', 'shortcut', 'folder' or '' (not found)."""
    info = runtime_info(cfg)
    if info["found"]:
        return info["runtime_exe"], info["server_js"], info["where"]
    return "", "", ""


# --------------------------------------------------------------------------- saving the paths (owner only)

MAX_SAVED_PATH = 500


def _plain_path(value, label):
    """(cleaned absolute path, None) or (None, why).  A program on this PC's own disk: no UNC share,
    no relative path, no control characters."""
    text = str(value or "").strip().strip('"').strip()
    if not text:
        return None, "%s is empty" % label
    if len(text) > MAX_SAVED_PATH or any(c in text for c in "\r\n\x00"):
        return None, "%s isn't a usable path" % label
    if text.startswith("\\\\") or text.startswith("//"):
        return None, "%s is on a network share; point at a copy on this PC's own disk" % label
    if not (os.path.isabs(text) or re.match(r"^[A-Za-z]:[\\/]", text)):
        return None, "%s must be a full path (like C:/Users/you/AppData/Local/Programs/LNV/Stremio-4/server.js)" % label
    return os.path.normpath(text), None


def check_paths(runtime_exe, server_js):
    """Is this pair something Jarvis can start?  {'ok', 'problems': [plain sentences], 'runtime_exe',
    'server_js'}.  Read-only: it looks at the two files and nothing else."""
    problems_out, clean = [], {}
    for key, value, label in (("runtime_exe", runtime_exe, "the Stremio program"), ("server_js", server_js, "server.js")):
        path, why = _plain_path(value, label)
        if why:
            problems_out.append(why)
        elif not Path(path).is_file():
            problems_out.append("%s doesn't exist: %s" % (label, path))
        else:
            clean[key] = path
    if clean.get("server_js") and not clean["server_js"].lower().endswith(".js"):
        problems_out.append("server.js must be the .js file Stremio's server runs (got %s)" % detect.basename(clean["server_js"]))
    if clean.get("runtime_exe") and clean["runtime_exe"].lower().endswith((".js", ".lnk", ".txt", ".json")):
        problems_out.append("the Stremio program must be the runtime (stremio-runtime.exe), not %s" % detect.basename(clean["runtime_exe"]))
    return {"ok": not problems_out, "problems": problems_out, **clean}


def save_paths(runtime_exe="", server_js="", server_url=None, check=None):
    """Write `server.runtime_exe`, `server.server_js` (and, when given, `server.url`) into
    ~/.jarvis/stremio.json.  OWNER ONLY: the only caller is `hub_admin.stremio_save`, the owner's
    own button (loopback, `X-Jarvis-Hub` header).  There is deliberately no model tool for this
    (plan decision 14 f): `tests/test_hub_stremio_detect.py` fails if a tool file names it, and
    an unattended process (chat reply, scheduled job, sub-agent) is refused here too.

    Checked BEFORE anything is written: both files exist; a new `server_url` must be http on
    loopback AND answer like a Stremio server right now (a dead address is never saved).  Only the
    keys named above change; every other key (the account key included) is carried over as it was,
    and a file that isn't valid JSON is left alone rather than overwritten.
    Returns {'ok', 'error'?, 'saved': {...}, 'server': {...what is on the port...}}."""
    try:
        from ... import hub_admin
        why = hub_admin.unattended_reason()
        if why:
            return {"ok": False, "error": why, "code": "unattended"}
    except Exception:  # noqa: BLE001 - the guard module being unimportable must not open the door
        return {"ok": False, "error": "couldn't check who is asking, so nothing was saved"}
    checked = check_paths(runtime_exe, server_js)
    if not checked["ok"]:
        return {"ok": False, "error": "; ".join(checked["problems"]), "problems": checked["problems"]}
    new_url = None
    if server_url:
        remote_ok = allow_remote()
        new_url = _address_url(server_url, remote_ok)
        if new_url is None:
            return {"ok": False, "error": _address_error("server address", server_url, remote_ok)}
        probe = detect.probe_port(new_url, check=check, other_ports=())
        if not probe["answers"]:
            return {"ok": False, "error": "nothing answers like a Stremio server at %s, so I didn't save that address" % new_url}
    path = config_path()
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return {"ok": False, "error": "%s exists but can't be read as JSON, so I didn't touch it. Fix or rename it first" % path}
        if not isinstance(raw, dict) or not isinstance(raw.get("server", {}), dict):
            return {"ok": False, "error": "%s isn't shaped like Jarvis expects (no 'server' object), so I didn't touch it" % path}
    server_block = dict(raw.get("server") or {})
    server_block["runtime_exe"], server_block["server_js"] = checked["runtime_exe"], checked["server_js"]
    if new_url:
        server_block["url"] = new_url
    raw["server"] = server_block
    from ... import atomic_io
    if not atomic_io.write_json(path, raw):
        return {"ok": False, "error": "couldn't write %s (is the folder writable?)" % path}
    after = load().get("server") or {}
    if after.get("runtime_exe") != checked["runtime_exe"] or after.get("server_js") != checked["server_js"]:
        return {"ok": False, "error": "I wrote %s but reading it back didn't give the same paths" % path}
    saved = {"runtime_exe": checked["runtime_exe"], "server_js": checked["server_js"]}
    if new_url:
        saved["url"] = new_url
    return {"ok": True, "saved": saved, "file": str(path)}


# --------------------------------------------------------------------------- the web side + repair (owner only)
# Brief 29 (N18 second half, N22 part e).  Same rules as save_paths: the owner's own button, loopback, an unattended
# process is refused, only the keys named here change, every other key (the account key included) is carried over,
# and a file that isn't valid JSON is never overwritten.  This module stays the ONLY validator of these values.

MAX_WEB_COMMAND = 200
# stremio-web's start command is run with a shell (actions/_stremio_web.py), so what the PAGE may save is one plain
# program and its arguments.  Chaining, redirection and substitution characters are refused here; a person who needs
# them can still edit the file by hand, exactly as before.
_COMMAND_FORBIDDEN = re.compile(r"[&|;<>`$%^\r\n\x00]")
_WEB_KEYS = ("url", "dir", "start_cmd")


def _owner_only():
    """None when the caller is the owner's own page, else the refusal dict (same guard as save_paths)."""
    try:
        from ... import hub_admin
        why = hub_admin.unattended_reason()
        if why:
            return {"ok": False, "error": why, "code": "unattended"}
    except Exception:  # noqa: BLE001 - the guard module being unimportable must not open the door
        return {"ok": False, "error": "couldn't check who is asking, so nothing was saved"}
    return None


def check_web(web, remote_ok=None):
    """(clean, problems) for the stremio-web block the page sends: {'url', 'dir', 'start_cmd'}.  Only the keys
    present are checked.  Read-only: it looks at the folder and nothing else.  `dir` '' turns stremio-web off;
    `start_cmd` '' goes back to the default command.  `remote_ok` (None = what is saved now) says whether an address
    other than this PC is accepted."""
    clean, problems_out = {}, []
    remote_ok = allow_remote() if remote_ok is None else bool(remote_ok)
    if not isinstance(web, dict):
        return clean, ["the stremio-web settings must be an object with url, dir and start_cmd"]
    for key in web:
        if key not in _WEB_KEYS:
            problems_out.append("%s isn't a stremio-web setting Jarvis can save" % str(key)[:40])
    if "url" in web:
        url = _address_url(web["url"], remote_ok)
        if url is None:
            problems_out.append(_address_error("stremio-web address", web["url"], remote_ok))
        else:
            clean["url"] = url
    if "dir" in web:
        text = str(web["dir"] or "").strip().strip('"').strip()
        if not text:
            clean["dir"] = ""
        else:
            folder, why = _plain_path(text, "the stremio-web folder")
            if why:
                problems_out.append(why)
            elif not Path(folder).is_dir():
                problems_out.append("the stremio-web folder doesn't exist: %s" % folder)
            else:
                clean["dir"] = folder
    if "start_cmd" in web:
        text = str(web["start_cmd"] or "").strip()
        if not text:
            clean["start_cmd"] = DEFAULTS["web"]["start_cmd"]
        elif len(text) > MAX_WEB_COMMAND:
            problems_out.append("the start command is longer than %d characters" % MAX_WEB_COMMAND)
        elif _COMMAND_FORBIDDEN.search(text):
            problems_out.append("the start command can't contain & | ; < > ` $ % ^ or a line break: give one program and its "
                                "arguments, like `pnpm start`")
        else:
            clean["start_cmd"] = text
    return clean, problems_out


def _raw_for_write():
    """(raw dict, None) of the file as it is (or {} when absent), else (None, why).  Never touches the file."""
    path = config_path()
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return None, "%s exists but can't be read as JSON, so I didn't touch it. Fix or rename it first" % path
        if not isinstance(raw, dict) or not isinstance(raw.get("server", {}), dict) or not isinstance(raw.get("web", {}), dict) \
                or any(not isinstance(raw.get(k, {}), dict) for k in ("player", "addons", "account", "network", "open")):
            return None, "%s isn't shaped like Jarvis expects (no 'server' / 'web' / 'player' / 'addons' / 'account' / 'network' / 'open' object), so I didn't touch it" % path
    return raw, None


def check_network(network):
    """(clean, problems) for {'allow_remote': true|false}: whether addresses other than this PC are accepted."""
    clean, found = {}, []
    if not isinstance(network, dict):
        return clean, ["the network settings must be an object with allow_remote"]
    for key in network:
        if key != "allow_remote":
            found.append("%s isn't a network setting Jarvis can save" % str(key)[:40])
    if "allow_remote" in network:
        if isinstance(network["allow_remote"], bool):
            clean["allow_remote"] = network["allow_remote"]
        else:
            found.append("allow_remote must be true or false")
    return clean, found


def check_open(value):
    """(clean value, problems) for where playback links open: 'browser' or 'native' (the Stremio app)."""
    text = str(value or "").strip().lower() if isinstance(value, str) else ""
    if text in OPEN_WITH:
        return text, []
    return None, ["links can open in the browser or in the Stremio app (browser or native)"]


def save_settings(server_url=None, web=None, player=None, addon_urls=None, check=None, network=None, open_with=None):
    """Write `server.url`, the `web` block, `network.allow_remote` and/or `open.with` into ~/.jarvis/stremio.json.  OWNER ONLY
    (hub_admin.stremio_save, the card's own buttons); there is no model tool for it.  A new `server_url` must be http on
    loopback (or, when `network.allow_remote` is on, now or in this same save, any http/https host) AND answer like a Stremio
    server right now (a dead address is never saved); the web address only has to pass the same address rule (stremio-web
    may not be running yet).  Turning `allow_remote` off is refused while a saved address is not on this PC.
    Returns {'ok', 'error'?, 'problems'?, 'saved': {...}, 'file'}."""
    refused = _owner_only()
    if refused:
        return refused
    if not server_url and web is None and player is None and addon_urls is None and network is None and open_with is None:
        return {"ok": False, "error": "nothing to save"}
    new_url, clean_web, clean_player, new_urls, clean_net, new_open = None, {}, {}, None, {}, None
    if network is not None:
        clean_net, found = check_network(network)
        if found:
            return {"ok": False, "error": "; ".join(found), "problems": found}
        if not clean_net:
            return {"ok": False, "error": "nothing to save"}
    if open_with is not None:
        new_open, found = check_open(open_with)
        if found:
            return {"ok": False, "error": "; ".join(found), "problems": found}
    remote_ok = clean_net["allow_remote"] if "allow_remote" in clean_net else allow_remote()
    if server_url:
        new_url = _address_url(server_url, remote_ok)
        if new_url is None:
            return {"ok": False, "error": _address_error("server address", server_url, remote_ok)}
        if not detect.probe_port(new_url, check=check, other_ports=())["answers"]:
            return {"ok": False, "error": "nothing answers like a Stremio server at %s, so I didn't save that address" % new_url}
    if web is not None:
        clean_web, found = check_web(web, remote_ok)
        if found:
            return {"ok": False, "error": "; ".join(found), "problems": found}
        if not clean_web:
            return {"ok": False, "error": "nothing to save"}
    if player is not None:
        clean_player, found = check_player(player)
        if found:
            return {"ok": False, "error": "; ".join(found), "problems": found}
        if not clean_player:
            return {"ok": False, "error": "nothing to save"}
    raw, why = _raw_for_write()
    if why:
        return {"ok": False, "error": why}
    if clean_net.get("allow_remote") is False:
        # the box can only be cleared when nothing saved points away from this PC (else the address would silently fall back)
        left = [(label, value) for label, value in (
            ("server address", new_url or (raw.get("server") or {}).get("url")),
            ("stremio-web address", clean_web.get("url") or (raw.get("web") or {}).get("url"))) if value and _loopback_url(value) is None]
        if left:
            return {"ok": False, "error": "the %s is %s, which isn't on this PC. Save an address on this PC (or empty the box) in the same save, "
                                          "then clear 'Allow addresses other than this PC'" % (left[0][0], str(left[0][1])[:80])}
    if addon_urls is not None:
        new_urls, found = check_addon_urls(addon_urls, (raw.get("addons") or {}).get("urls") if isinstance(raw.get("addons"), dict) else [])
        if found:
            return {"ok": False, "error": "; ".join(found), "problems": found}
    if clean_net:
        raw["network"] = dict(raw.get("network") or {}, **clean_net)
    if new_open:
        raw["open"] = dict(raw.get("open") or {}, **{"with": new_open})
    if new_url:
        raw["server"] = dict(raw.get("server") or {}, url=new_url)
    if clean_player:
        raw["player"] = dict(raw.get("player") or {}, **clean_player)
    if new_urls is not None:
        raw["addons"] = dict(raw.get("addons") or {}, urls=new_urls)
    if clean_web:
        raw["web"] = dict(raw.get("web") or {}, **clean_web)
    from ... import atomic_io
    path = config_path()
    if not atomic_io.write_json(path, raw):
        return {"ok": False, "error": "couldn't write %s (is the folder writable?)" % path}
    after = load()
    saved = {}
    if clean_net:
        saved["network"] = clean_net
        if allow_remote(after) != clean_net.get("allow_remote", allow_remote(after)):
            return {"ok": False, "error": "I wrote %s but reading it back didn't give the same network setting" % path}
    if new_open:
        saved["open"] = new_open
        if open_with_value(after) != new_open:
            return {"ok": False, "error": "I wrote %s but reading it back didn't give the same 'open with' setting" % path}
    if new_url:
        saved["url"] = new_url
        if (after.get("server") or {}).get("url") != new_url:
            return {"ok": False, "error": "I wrote %s but reading it back didn't give the same address" % path}
    if clean_web:
        if any((after.get("web") or {}).get(k) != v for k, v in clean_web.items()):
            return {"ok": False, "error": "I wrote %s but reading it back didn't give the same stremio-web settings" % path}
        saved["web"] = clean_web
    if clean_player:
        if any((after.get("player") or {}).get(k) != v for k, v in clean_player.items()):
            return {"ok": False, "error": "I wrote %s but reading it back didn't give the same player settings" % path}
        saved["player"] = clean_player
    if new_urls is not None:
        saved["addon_urls"] = len(new_urls)
    return {"ok": True, "saved": saved, "file": str(path)}


# --------------------------------------------------------------------------- player, addon list, account (owner only)
# Same rules as above: the owner's own buttons, an unattended process is refused, every other key is carried over.  The
# secrets (the account key, a debrid key inside an addon address) are written here and never read back out by the page.

_PLAYER_KINDS = ("auto", "mpv", "vlc")
MAX_ADDON_URLS = 30
MAX_ADDON_URL = 300
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-+=/.]{8,200}$")


def check_player(player):
    """(clean, problems) for {'kind': auto|mpv|vlc, 'exe': full path or ''}.  Only the keys present are checked."""
    clean, found = {}, []
    if not isinstance(player, dict):
        return clean, ["the player settings must be an object with kind and exe"]
    for key in player:
        if key not in ("kind", "exe"):
            found.append("%s isn't a player setting Jarvis can save" % str(key)[:40])
    if "kind" in player:
        kind = str(player["kind"] or "").strip().lower()
        if kind in _PLAYER_KINDS:
            clean["kind"] = kind
        else:
            found.append("the player must be auto, mpv or vlc")
    if "exe" in player:
        text = str(player["exe"] or "").strip().strip('"').strip()
        if not text:
            clean["exe"] = ""
        else:
            exe, why = _plain_path(text, "the player program")
            if why:
                found.append(why)
            elif not Path(exe).is_file():
                found.append("the player program doesn't exist: %s" % exe)
            elif exe.lower().endswith((".js", ".lnk", ".txt", ".json")):
                found.append("the player must be the program itself (mpv.exe or vlc.exe), not %s" % detect.basename(exe))
            else:
                clean["exe"] = exe
    return clean, found


def _addon_url(text):
    text = str(text or "").strip()
    if not text or len(text) > MAX_ADDON_URL or re.search(r"\s", text):
        return None
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
        return None
    return text


def check_addon_urls(change, current):
    """(new list, problems) for {'add': [address ...], 'remove': [position ...]} applied to the saved list.  Positions are the
    ones `addon_hosts()` showed, so the page never needs the addresses (a debrid key can sit in one)."""
    current = [str(u) for u in (current or []) if isinstance(u, str)]
    if not isinstance(change, dict):
        return None, ["the addon list change must be an object with add and remove"]
    found, out = [], list(current)
    drop = set()
    for pos in change.get("remove") or []:
        if isinstance(pos, int) and not isinstance(pos, bool) and 0 <= pos < len(current):
            drop.add(pos)
        else:
            found.append("there is no addon at position %s" % str(pos)[:10])
    out = [u for i, u in enumerate(current) if i not in drop]
    for raw in change.get("add") or []:
        url = _addon_url(raw)
        if url is None:
            found.append("that isn't an addon address (http or https, no spaces, up to %d characters)" % MAX_ADDON_URL)
        elif url not in out:
            out.append(url)
    if len(out) > MAX_ADDON_URLS:
        found.append("at most %d addons can be saved" % MAX_ADDON_URLS)
    return (None if found else out), found


def addon_hosts(cfg=None):
    """The saved addon list as [{'index', 'host'}]: the host only, never the path (a debrid key can be in it)."""
    cfg = cfg or load()
    block = cfg.get("addons")
    urls = block.get("urls") if isinstance(block, dict) else None
    out = []
    for i, url in enumerate(urls if isinstance(urls, list) else []):
        try:
            parts = urlsplit(str(url))
            out.append({"index": i, "host": ("%s%s" % (parts.hostname or "?", ":%d" % parts.port if parts.port else ""))[:80]})
        except ValueError:
            out.append({"index": i, "host": "?"})
    return out[:MAX_ADDON_URLS]


def account_state(cfg=None):
    """{'signed_in', 'email'}: whether a key is saved and the email it was saved with.  Never the key."""
    block = (cfg or load()).get("account")
    block = block if isinstance(block, dict) else {}
    key = str(block.get("auth_key") or "").strip()
    return {"signed_in": bool(_KEY_RE.match(key)), "email": str(block.get("email") or "")[:150]}


def save_account(auth_key, email=""):
    """Write `account.auth_key` (and `account.email`, which is not a secret) into stremio.json.  OWNER ONLY.  Every other key
    is carried over; a file that isn't valid JSON is never overwritten.  {'ok', 'error'?, 'file'}; never echoes the key."""
    refused = _owner_only()
    if refused:
        return refused
    key = str(auth_key or "").strip()
    if not _KEY_RE.match(key):
        return {"ok": False, "error": "that doesn't look like a Stremio login key (letters, digits and - _ + = / . only)"}
    raw, why = _raw_for_write()
    if why:
        return {"ok": False, "error": why}
    block = dict(raw.get("account") or {}, auth_key=key)
    email = str(email or "").strip()[:150]
    if email:
        block["email"] = email
    else:
        block.pop("email", None)
    raw["account"] = block
    from ... import atomic_io
    if not atomic_io.write_json(config_path(), raw):
        return {"ok": False, "error": "couldn't write %s (is the folder writable?)" % config_path()}
    if not account_state()["signed_in"]:
        return {"ok": False, "error": "I wrote %s but reading it back didn't show the account" % config_path()}
    return {"ok": True, "file": str(config_path())}


def clear_account():
    """Sign out: remove `account.auth_key` and `account.email` (nothing else).  OWNER ONLY."""
    refused = _owner_only()
    if refused:
        return refused
    raw, why = _raw_for_write()
    if why:
        return {"ok": False, "error": why}
    block = dict(raw.get("account") or {})
    if not (block.get("auth_key") or block.get("email")):
        return {"ok": True, "file": str(config_path()), "was_signed_in": False}
    block.pop("auth_key", None)
    block.pop("email", None)
    raw["account"] = block
    from ... import atomic_io
    if not atomic_io.write_json(config_path(), raw):
        return {"ok": False, "error": "couldn't write %s (is the folder writable?)" % config_path()}
    return {"ok": True, "file": str(config_path()), "was_signed_in": True}


def file_state(cfg=None):
    """What is wrong with ~/.jarvis/stremio.json as a FILE (N22 part e).  Read-only; never raises.
    {'state', 'file', 'sentence'}; state is 'ok', 'missing', 'broken' (not JSON), 'stale' (a saved path no longer
    exists), 'partial' (only one of the two paths is saved) or 'no_paths' (the file exists, no Stremio paths in it)."""
    path = config_path()
    out = {"file": str(path), "state": "ok", "sentence": ""}
    try:
        if not path.exists():
            out.update(state="missing", sentence="There is no stremio.json yet, so Jarvis looks for Stremio by itself every time.")
            return out
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict) or not isinstance(raw.get("server", {}), dict):
            out.update(state="broken", sentence="%s can't be read as JSON, so Jarvis ignores it and will not overwrite it." % path)
            return out
        block = raw.get("server") or {}
        exe, js = str(block.get("runtime_exe") or ""), str(block.get("server_js") or "")
        if bool(exe) != bool(js):
            out.update(state="partial", sentence="Only one of the two Stremio paths is saved in stremio.json, and both are needed.")
        elif exe and not (Path(exe).is_file() and Path(js).is_file()):
            out.update(state="stale", sentence="A Stremio path saved in stremio.json no longer exists (Stremio may have moved).")
        elif not exe:
            out.update(state="no_paths", sentence="stremio.json has no Stremio paths saved, so Jarvis looks for Stremio by itself every time.")
    except Exception:  # noqa: BLE001 - a status read must not raise
        out.update(state="ok", sentence="")
    return out


def _bad_copy_path(path):
    """stremio.json.bad, or .bad2, .bad3 ... the first that is free (never overwrites an earlier copy)."""
    for n in range(1, 30):
        candidate = path.with_name(path.name + (".bad" if n == 1 else ".bad%d" % n))
        if not candidate.exists():
            return candidate
    return None


def repair(keep_copy=False, check=None, **inputs):
    """The Stremio card's one repair button (N22 part e): find Stremio again and save the pair, through `save_paths`
    (so every guard of the Locate... button applies: loopback, owner only, both files must exist).  Only the `server`
    paths change; `account`, `player`, `prefs` and every other key are carried over.  A file that isn't valid JSON is
    NOT overwritten unless `keep_copy` is true: then it is MOVED aside to stremio.json.bad (never deleted) and a fresh
    file is written.  Nothing is written when Stremio is not found.  `inputs` feed `detect_runtime` (tests)."""
    refused = _owner_only()
    if refused:
        return refused
    state = file_state()
    if state["state"] == "ok":
        return {"ok": False, "error": "stremio.json is fine; there is nothing to repair"}
    exe, js = detect_runtime(**inputs)
    if not (exe and js):
        return {"ok": False, "error": "I couldn't find Stremio on this PC, so there is nothing to save. Use Locate... to point Jarvis at it"}
    kept = None
    if state["state"] == "broken":
        if not keep_copy:
            return {"ok": False, "code": "needs_copy",
                    "error": "%s can't be read as JSON, so I won't overwrite it. Agree to keep a copy (stremio.json.bad) and I'll write a fresh file"
                             % state["file"]}
        path = config_path()
        target = _bad_copy_path(path)
        if target is None:
            return {"ok": False, "error": "too many stremio.json.bad copies already; remove some and try again"}
        try:
            os.replace(str(path), str(target))
        except OSError as exc:
            return {"ok": False, "error": "couldn't keep a copy of %s: %s" % (path, str(exc)[:100])}
        kept = str(target)
    result = save_paths(exe, js, check=check)
    if kept:
        result["kept_copy"] = kept
    if result.get("ok"):
        result["repaired"] = state["state"]
    return result


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
