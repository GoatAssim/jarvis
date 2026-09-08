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
}


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
