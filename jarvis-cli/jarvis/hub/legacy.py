"""`dl_import_legacy` (plan 7.1 / 10.1): index what the old `ytdl_download` tool already saved.

The old tool writes every download into `~/.jarvis/downloads/dl_<timestamp>_<hex>/` and prunes
the oldest folders beyond 15.  Those files are not hub downloads, so they are shown as rows of a
small read-only provider (`ytdl_legacy`): finished, with an Open / Reveal button each, and
nothing else.  The hub never moves, renames or deletes them; the old tool keeps pruning its
own folder as before (a pruned file's row just says the file is gone when opened).

Idempotent: a row's id is made from the folder name and the file name, so running it again
updates the same rows instead of adding more.
"""

import hashlib
import os
from pathlib import Path

from . import naming, states

PROVIDER = "ytdl_legacy"
LABEL = "Earlier video downloads"
MAX_FOLDERS = 200
_MEDIA_EXT = frozenset((".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v",
                        ".mp3", ".m4a", ".opus", ".wav", ".flac", ".aac", ".ogg", ".wma"))


def legacy_dir():
    return Path.home() / ".jarvis" / "downloads"


def local_id(folder_name, file_name):
    return "%s-%s" % (folder_name, hashlib.sha1(file_name.encode("utf-8", "replace")).hexdigest()[:8])


def scan(root=None):
    """[(folder name, file path, size, mtime)] newest first.  Pure reading; never raises."""
    root = Path(root) if root else legacy_dir()
    out = []
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("dl_")]
    except OSError:
        return out
    folders.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for folder in folders[:MAX_FOLDERS]:
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _MEDIA_EXT)
        except OSError:
            continue
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append((folder.name, path, stat.st_size, stat.st_mtime))
    return out


def import_legacy(store, root=None):
    """Make rows for the old downloads.  Returns {ok, folder, found, imported, already}."""
    try:
        root = Path(root) if root else legacy_dir()
        found = scan(root)
        if not found:
            return {"ok": True, "folder": str(root), "found": 0, "imported": 0, "already": 0,
                    "note": "No earlier ytdl_download folders were found."}
        provider, err = store.register_provider(
            PROVIDER, label=LABEL, mode="push", kind="video", icon="V", capabilities=["open", "reveal"],
            open_roots=[str(root)], source_file="hub.legacy")
        if provider is None:
            return {"ok": False, "error": "couldn't register the provider: %s" % err}
        imported = already = 0
        for folder, path, size, mtime in found:
            lid = local_id(folder, path.name)
            existed = store.get_item("%s:%s" % (PROVIDER, lid)) is not None
            kind = naming.kind_for(path.name)
            fields = {"title": path.stem, "subtitle": "earlier download", "kind": kind, "state": "done",
                      "percent": 100, "bytes_done": size, "bytes_total": size, "dest": str(path),
                      "message": None, "error": None}
            item, _problems = store.upsert_provider_row(PROVIDER, lid, fields, create=True, now=mtime)
            if item is None:
                continue
            if existed:
                already += 1
            else:
                imported += 1
        return {"ok": True, "folder": str(root), "found": len(found), "imported": imported, "already": already}
    except Exception as exc:  # noqa: BLE001 - tools never raise
        return {"ok": False, "error": "couldn't index the earlier downloads: %s" % states.clean_text(exc, 160)}
