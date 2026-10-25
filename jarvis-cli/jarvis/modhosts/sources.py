"""The registry of source adapters.

    get("modrinth")   -> the modrinth module (same functions as every adapter)
    get("curseforge") -> the curseforge module when another patch has added it, else None

curseforge is imported lazily with importlib so that plugging it in needs no edit to any file here.  A missing
key is NOT reported by get(): the adapter's own functions answer `no_key` (with the "how to get a personal key"
sentence).  An adapter must expose: search, get_project, list_files, get_file, bulk_projects, bulk_list_files,
bulk_get_files (see the interface contract).
"""
import importlib

KINDS = ("mod", "modpack", "resourcepack", "shader", "datapack")
LOADERS = ("fabric", "forge", "neoforge", "quilt")
SOURCES = ("modrinth", "curseforge")

ADAPTER_FUNCTIONS = ("search", "get_project", "list_files", "get_file", "bulk_projects", "bulk_list_files",
                     "bulk_get_files")


def get(source):
    """The adapter module for `source`, or None (unknown name, or not installed).  Never raises."""
    name = str(source or "").strip().lower()
    if name not in SOURCES:
        return None
    try:
        return importlib.import_module("." + name, __package__)
    except Exception:  # noqa: BLE001 - not installed yet, or it failed to import: treated as absent
        return None


def has_curseforge_key():
    """True when a CurseForge key is configured.  Asks config.py (another patch's module) and never sees the
    key's value beyond `bool(...)`; False when config.py is not there."""
    try:
        config = importlib.import_module(__package__ + ".config")
        return bool(config.get_key())
    except Exception:  # noqa: BLE001
        return False
