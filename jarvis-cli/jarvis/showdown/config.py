"""~/.jarvis/showdown.json: who the bot logs in as and who it may play.

Same footing as spotify.json: a plain JSON file outside the repo that the owner edits.
The password is read from here (or the JARVIS_SHOWDOWN_PASSWORD environment variable)
and is never returned by any tool, status call or log line.
"""

import json
import os
import re
from pathlib import Path

ENCODING = "utf-8"
JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "showdown.json"

SERVERS = {
    "public": "wss://sim3.psim.us/showdown/websocket",
    "local": "ws://localhost:8000/showdown/websocket",
}

DEFAULTS = {
    # The bot's own Showdown account. Empty password = an unregistered "guest" name.
    "username": "",
    "password": "",
    # "public", "local" (a pokemon-showdown server you run yourself), or a ws:// URL.
    "server": "public",
    # Only these accounts may challenge the bot (names, case and spaces ignored).
    # Empty = nobody: the bot rejects every challenge. YOUR account goes here.
    "allowed_users": [],
    "default_format": "gen9randombattle",
    # auto = the bot plays; manual = it only suggests and waits for showdown_choose.
    "mode": "auto",
    # False = heuristic only (no model call at all).
    "use_model": True,
    "auto_accept": True,
    # Local server run with --no-security: log in without the real login server.
    "auth": "normal",
}

_NAME = re.compile(r"^[A-Za-z0-9 _\-]{1,18}$")
_FORMAT = re.compile(r"^[a-z0-9]{3,40}$")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not cfg.get("password"):
        cfg["password"] = os.environ.get("JARVIS_SHOWDOWN_PASSWORD", "")
    users = cfg.get("allowed_users")
    cfg["allowed_users"] = [str(u) for u in users] if isinstance(users, list) else []
    if cfg.get("mode") not in ("auto", "manual"):
        cfg["mode"] = "auto"
    return cfg


def ensure_config():
    """Write the template file once so the owner has something to edit."""
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            CONFIG_FILE.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding=ENCODING)
    except OSError:
        pass
    return CONFIG_FILE


def server_url(cfg):
    s = str(cfg.get("server") or "public")
    if s in SERVERS:
        return SERVERS[s]
    return s if s.startswith(("ws://", "wss://")) else SERVERS["public"]


def valid_name(name):
    return bool(_NAME.match(str(name or "")))


def valid_format(fmt):
    return bool(_FORMAT.match(str(fmt or "")))
