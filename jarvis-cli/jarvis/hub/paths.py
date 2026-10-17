"""Where the hub keeps things (plan section 13).

Resolved when called, not at import: the tests redirect HOME before importing and an
engine running in the daemon may outlive a HOME change in a test process.  (Most other
modules resolve `Path.home()` at import time; this one can afford not to.)
"""

from pathlib import Path


def jarvis_dir():
    return Path.home() / ".jarvis"


def hub_dir():
    return jarvis_dir() / "hub"


def db_path():
    return hub_dir() / "hub.db"


def inbox_dir():
    """Where hub_client spools a report when the store is busy (the daemon applies them)."""
    return hub_dir() / "inbox"


def log_path():
    return hub_dir() / "hub.log"


def default_library_root():
    """Where finished files go until the owner picks a place (plan decision 1).

    The plan's example is `D:/Downloads/Jarvis`; a drive that may not exist is a bad
    default, so this is the user's Downloads folder, and `dl_settings library_root=...`
    moves it.
    """
    return Path.home() / "Downloads" / "Jarvis"
