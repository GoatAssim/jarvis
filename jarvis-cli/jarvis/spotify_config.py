"""Spotify app credentials and OAuth tokens (~/.jarvis/spotify.json).

Create a Spotify Developer app, set redirect URI to the value in this file,
paste client_id, then run: jarvis spotify-login
Log in with the SAME account as the Spotify desktop app on this PC.
"""

import json
import time
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "spotify.json"
ENCODING = "utf-8"

DEFAULT_CONFIG = {
    "enabled": True,
    "client_id": "",
    "client_secret": "",
    "redirect_uri": "http://127.0.0.1:19823/callback",
    "access_token": "",
    "refresh_token": "",
    "expires_at": 0,
    "display_name": "",
    "user_id": "",
    "product": "",
}

SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "user-top-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-library-modify",
    "user-read-email",
]


def ensure_config():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding=ENCODING
        )


def load_config():
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(data):
    ensure_config()
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)


def is_configured():
    cfg = load_config()
    if not cfg.get("enabled", True):
        return False
    return bool(
        (cfg.get("refresh_token") or cfg.get("access_token") or cfg.get("client_id") or "").strip()
    )


def token_valid(cfg=None):
    cfg = cfg or load_config()
    token = (cfg.get("access_token") or "").strip()
    if not token:
        return False
    expires = cfg.get("expires_at") or 0
    try:
        expires = float(expires)
    except (TypeError, ValueError):
        expires = 0
    return time.time() < (expires - 30)
