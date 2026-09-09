"""Everything SDK settings (~/.jarvis/everything.json).

The SDK is a thin ctypes wrapper around voidtools' Everything file-search
IPC interface (Windows only) \u2014 see DOCUMENTATION/everything_sdk_reference.md
and DOCUMENTATION/everything_sdk_python_reference.md. Everything (the app)
must already be running in the background; this file just tracks where the
SDK DLL lives and a few safe defaults, so everything_tools.py doesn't have
to re-guess the DLL path on every call.

`dll_path` is normally left blank \u2014 everything_tools._resolve_dll_path()
auto-detects the usual install locations first and only falls back to this
override. Set it by hand (or via `jarvis everything-config`, which just
prints this file's path) if auto-detection doesn't find your install.
"""

import json
import os
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "everything.json"
ENCODING = "utf-8"

DEFAULT_CONFIG = {
    "enabled": True,
    "dll_path": "",              # explicit override; blank = auto-detect
    "default_max_results": 30,
    "max_results_cap": 200,
    "match_path_default": False,
    # Friendly names -> real folders, so "search my projects folder" doesn't
    # require the user (or the model) to know/type the full path. Keys are
    # matched case-insensitively with spaces/underscores/hyphens ignored.
    # Values may use ~ and %ENV% / $ENV placeholders, expanded on read.
    "folder_aliases": {
        "desktop": r"~\Desktop",
        "documents": r"~\Documents",
        "downloads": r"~\Downloads",
        "pictures": r"~\Pictures",
        "music": r"~\Music",
        "videos": r"~\Videos",
    },
}


def _normalize_alias_key(name):
    return "".join(ch for ch in name.strip().lower() if ch.isalnum())


def resolve_folder_alias(name, cfg=None):
    """Look up a user-friendly folder name in folder_aliases (case/spacing
    insensitive) and expand ~ and %ENV%/$ENV vars. Returns the expanded path
    string, or None if `name` doesn't match any configured alias."""
    if not name or not isinstance(name, str):
        return None
    cfg = cfg if cfg is not None else load_config()
    aliases = cfg.get("folder_aliases") or {}
    target = _normalize_alias_key(name)
    for key, value in aliases.items():
        if _normalize_alias_key(key) == target:
            return str(Path(os.path.expandvars(value)).expanduser())
    return None


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


def is_enabled():
    return bool(load_config().get("enabled", True))
