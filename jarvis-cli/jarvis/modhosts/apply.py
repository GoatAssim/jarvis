"""Replace ONE mod file with its newer file, safely, and keep the way back (brief M2).

The Updates feature finds a newer file (modhosts/updates.py, `check_folder`); the Minecraft update source (M1) calls
`apply_update` to swap it in.  This is the one place in the Minecraft group that changes a file of the owner's, so it is
slow and exact on purpose.  A bad swap would break the game.

THE RULE (the owner's, new in this round)
  A mod file is replaced only here, only after the owner's confirmation (or the owner's own standing auto-update tick,
  which the hub honours, not this file), only inside a folder the owner put on the watched list, NEVER deleting anything
  (the old file is MOVED to a backup folder), and always verified by hash before and after.  One file per call.

THE STEPS, IN ORDER (each one is checked before the next; any failure stops there)
  1. Find the old file in `folder` by its SHA-1 (one level, no links, hidden files or sub-folders: `updates.scan_folder`).
     No file has that hash any more -> state "unchanged", nothing touched.  Two files have it -> refused (never guess).
  2. Ask the SAME adapter that found the update (modrinth.get_file / curseforge.get_file, through net.py) for the new
     file's URL, size and hashes.  `updates.check_folder` entries carry only the version id, name and size, so the real
     link and checksums are read again here and compared with what the entry said (a changed file is refused).  Modrinth:
     sha512 else sha1.  CurseForge: sha1.  A file with NO published hash is REFUSED (stricter than download_mc_file on
     purpose: here a bad file would replace a good one).  A file whose author blocks third-party downloads is refused with
     the project page; nothing else is tried.
  3. `dry_run`: say what would happen in ONE line (`plan`) and stop.  Nothing is created, not even a temp file or a folder.
  4. Download through `net.fetch` only (the vendor's own host), capped by the Minecraft `download_max_mb` setting (0 = no
     limit of the owner's) and by a hard ceiling of 512 MB, into an exclusively created `.part` file in
     ~/.jarvis/modhosts/replaced/_staging/ (NOT in the mods folder).  Size and hash are checked ON DISK; any mismatch deletes
     the `.part` (it is ours) and answers `verify_failed`.  The old file was never touched.
  5. Swap: a record (replaced.json) is written FIRST (no record, no swap), then the OLD file is MOVED into
     ~/.jarvis/modhosts/replaced/<YYYYmmdd-HHMMSS>/<old name> (a counter is added when that batch folder exists), then the
     verified new file is moved into `folder` under its official name WITHOUT replacing anything (outdir's no-clobber
     move).  If the second move fails the old file is moved back.  Same name for both: the old one is backed up first.
  6. The placed file is hashed again and compared with the published hash; a difference puts the old file back and fails.
  7. The record is finished (old name and hash, new name and hash, folder, source, project, when, `get_with`; no secret).
     Backups are then pruned (see PRUNING).
  8. Result: state "done", needs_restart true (a running game does not reload mods), installed = the new file's name.
  Required dependencies the new version lists are REPORTED in `notes` and never added.

WHERE THINGS LIVE (all under ~/.jarvis/modhosts/replaced/, owner-only permissions where the OS has them)
  <YYYYmmdd-HHMMSS>[-n]/   one batch per replaced file (UTC time): the old file, replaced.json
  _staging/                downloads in progress (*.part).  A `.part` left by a killed Jarvis is safe to delete by hand.
  undone/<batch id>/       the new file moved aside by `undo_mc_update`

PRUNING (the ONE place Jarvis deletes anything; `_remove_batch` is the only function that does it)
  After a replace SUCCEEDED, if there are more than MAX_BATCHES batches or they take more than MAX_BACKUP_BYTES, the OLDEST
  batch is removed (repeatedly, oldest first, never the batch just made).  It happens only after a newer apply succeeded,
  the result says so (`pruned`, and the message), and `_remove_batch` refuses anything that is not a batch folder directly
  inside ~/.jarvis/modhosts/replaced/ (a name check, a real-path check, never a link).  The same function removes the
  record-only batch of an apply that failed before it moved anything.

TWO THINGS A DISK CAN DO that a rename cannot
  * The mods folder may be on another drive than ~/.jarvis.  A plain rename then fails, so `_move` falls back to
    copy -> verify the copy by hash -> only then remove the source.  The old file is never left without a verified copy.
  * On Windows a jar the game holds open cannot be moved.  That is reported plainly ("close Minecraft and try again")
    and changes nothing.

No function here raises; a failure is {"ok": False, "state": "failed", "message", "error", "reason"} with a reason from
modhosts/__init__.py.  Names and text from the mod sites are untrusted: clipped, stripped, marked.  The CurseForge key is
never read here (the adapter and net.py do that); this file never names a key file.
"""
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from . import (AMBIGUOUS, BAD_DEST, EXISTS, NOT_DISTRIBUTABLE, OFFLINE, REASONS, TOO_BIG, UNSUPPORTED, VENDOR_CHANGED,
               VERIFY_FAILED, net, outdir, sources, untrusted, updates)

HARD_CEILING = 512 * 1024 * 1024          # a file bigger than this is never fetched, whatever the setting says
MAX_BATCHES = 50                          # backup batches kept (read at call time so a test can lower it)
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024
MSG_MAX = 200                             # the contract: one plain sentence of at most 200 characters
CHUNK = 1024 * 1024
RECORD_NAME = "replaced.json"
RECORD_MAX_BYTES = 64 * 1024
STAGING = "_staging"
UNDONE = "undone"
EXTENSIONS = (".jar", ".zip", ".mrpack")
HASH_ORDER = {"modrinth": ("sha512", "sha1"), "curseforge": ("sha1",)}
LABEL = {"modrinth": "Modrinth", "curseforge": "CurseForge"}
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_LEN = {"sha1": 40, "sha512": 128}
_BATCH_RE = re.compile(r"^(\d{8})-(\d{6})(?:-(\d{1,4}))?$")
_MR_ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
_STATUSES = ("pending", "done", "undone", "rolled_back")

CDN_NOTE = ("CurseForge's file server (edge.forgecdn.net) has not served a file to Jarvis in the owner's probe, so a "
            "CurseForge file may not be fetchable at all. Nothing was changed.")
LOCK_TEXT = ("The file looks to be in use (Windows won't move a jar the game has open). Close Minecraft and its launcher, "
             "then try again. Nothing was changed.")

NOW = None                                # test seam: a callable returning epoch seconds


# --------------------------------------------------------------------------- small helpers

def _now():
    try:
        return float(NOW()) if callable(NOW) else time.time()
    except Exception:  # noqa: BLE001
        return time.time()


def _clip(text, limit=60):
    return untrusted.clip(text, limit)


def _size_text(n):
    return "%.1f MB" % (n / 1048576.0) if n >= 1048576 else "%d KB" % max(1, round(n / 1024.0))


def _result(ok, state, message, **extra):
    out = {"ok": bool(ok), "state": state, "message": untrusted.clip(message, MSG_MAX)}
    out.update(extra)
    return out


def _failed(reason, message, **extra):
    """The failure shape: the contract's apply result (state failed, error) plus `reason`."""
    text = untrusted.clip(message, MSG_MAX)
    out = {"ok": False, "state": "failed", "message": text, "error": text,
           "reason": reason if reason in REASONS else VENDOR_CHANGED}
    out.update(extra)
    return out


def _guest():
    """True when this turn answers someone who is not the owner (the same rule as the Minecraft tools).  Unreadable = guest."""
    try:
        from ..actions import minecraft_updates
        return bool(minecraft_updates._is_chat_guest())
    except Exception:  # noqa: BLE001 - a permission check fails closed
        return True


def _setting(key, default):
    try:
        from .. import tool_settings
        return tool_settings.get("minecraft_lookup", key, default)
    except Exception:  # noqa: BLE001
        return default


def _cap_bytes():
    """The `download_max_mb` setting in bytes (read the way minecraft_download.py reads it); 0 = no cap of the owner's -> None."""
    try:
        mb = float(_setting("download_max_mb", 0) or 0)
    except (TypeError, ValueError):
        mb = 0
    return int(mb * 1024 * 1024) if mb > 0 else None


def _late(deadline):
    return deadline is not None and time.monotonic() > deadline


def _stamp(epoch):
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(epoch))


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --------------------------------------------------------------------------- the backup area

def replaced_root():
    """~/.jarvis/modhosts/replaced, resolved at call time so a test that redirects HOME first sees its own."""
    return Path.home() / ".jarvis" / "modhosts" / "replaced"


def batch_sort_key(batch_id):
    match = _BATCH_RE.match(str(batch_id or ""))
    if not match:
        return None
    return (match.group(1), match.group(2), int(match.group(3) or 1))


def batch_ids():
    """Every batch folder's id, OLDEST first.  Only names that look like a batch count (never _staging or undone)."""
    root = replaced_root()
    found = []
    try:
        if outdir.is_link(root):
            return []
        with os.scandir(str(root)) as it:
            for entry in it:
                key = batch_sort_key(entry.name)
                if key is not None and entry.is_dir(follow_symlinks=False):
                    found.append((key, entry.name))
    except OSError:
        return []
    return [name for _key, name in sorted(found)]


def _mkdir_private(path):
    """Create ONE folder with owner-only permissions where the OS has them; an existing folder is fine."""
    try:
        os.mkdir(str(path), 0o700)
    except FileExistsError:
        pass


def _ensure_root():
    """Make sure ~/.jarvis/modhosts/replaced exists (one level at a time, owner-only) -> (Path, None) | (None, failure)."""
    root = replaced_root()
    try:
        for part in (root.parent.parent, root.parent, root):
            if outdir.is_link(part):
                return None, _failed(BAD_DEST, "Jarvis's backup folder (%s) is a link, so nothing was changed." % _clip(str(part), 80))
            _mkdir_private(part)
        if not os.path.isdir(root):
            return None, _failed(BAD_DEST, "Jarvis's backup folder couldn't be created, so nothing was changed.")
    except OSError as exc:
        return None, _failed(BAD_DEST, "Jarvis's backup folder couldn't be created (%s). Nothing was changed." % _clip(exc, 60))
    return root, None


def _new_batch(root, epoch):
    """Create a fresh batch folder: the second-resolution stamp, then -2, -3 ... when that name is taken.  -> (id, Path)."""
    base = _stamp(epoch)
    for n in range(1, 10000):
        batch_id = base if n == 1 else "%s-%d" % (base, n)
        path = root / batch_id
        try:
            os.mkdir(str(path), 0o700)
            return batch_id, path
        except FileExistsError:
            continue
    raise OSError("no free backup folder name")


def _free_name(folder, name):
    """`name`, or `name` with a counter before the extension, so an existing backup is never overwritten."""
    if not os.path.lexists(os.path.join(str(folder), name)):
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(2, 10000):
        candidate = "%s (%d)%s" % (stem, n, ("." + ext) if ext else "")
        if not os.path.lexists(os.path.join(str(folder), candidate)):
            return candidate
    raise OSError("no free backup name")


def _dir_bytes(path):
    total = 0
    try:
        for dirpath, _dirs, names in os.walk(str(path), followlinks=False):
            for name in names:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------- hashing and moving

def _digest(path, *algos):
    """Hash one regular file in one pass -> ({algo: hex}, size) | (None, why).  Never follows a link; never raises."""
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not_a_file"
        digests = {a: hashlib.new(a) for a in algos}
        size = 0
        with os.fdopen(fd, "rb") as handle:
            fd = None
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                for digest in digests.values():
                    digest.update(chunk)
        return {a: d.hexdigest() for a, d in digests.items()}, size
    except Exception:  # noqa: BLE001 - locked, vanished, no permission
        return None, "unreadable"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _is_lock(exc):
    """True when a failed move looks like a file in use (a sharing violation on Windows) or one the OS won't let go of."""
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in (5, 32, 33):
        return True
    return getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM, errno.EBUSY, getattr(errno, "ETXTBSY", -1))


def _cross_device(exc):
    return getattr(exc, "errno", None) == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _drop_part(path):
    """Remove a `.part` file THIS module made (staging download or a cross-drive temp copy).  Anything else is left alone."""
    try:
        if str(path).endswith(".part"):
            os.unlink(str(path))
    except OSError:
        pass


def _copy_across(src, dst):
    """Move across drives: copy to a temp name beside `dst`, check the copy by hash, put it under `dst` WITHOUT replacing
    anything, and only then remove `src`.  Raises FileExistsError / OSError; leaves `src` whole on any failure."""
    import tempfile
    first, size = _digest(src, "sha1")
    if first is None:
        raise OSError(errno.EIO, "the file could not be read")
    folder = os.path.dirname(str(dst))
    fd, tmp = tempfile.mkstemp(prefix=".jarvis-move-", suffix=".part", dir=folder)
    placed = False
    try:
        with os.fdopen(fd, "wb") as out, open(str(src), "rb") as source:
            while True:
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            try:
                os.fsync(out.fileno())
            except OSError:
                pass
        again, copied = _digest(tmp, "sha1")
        if again is None or copied != size or again["sha1"] != first["sha1"]:
            raise OSError(errno.EIO, "the copy did not match the original")
        outdir._move_no_clobber(tmp, str(dst))        # same folder now: a rename; refuses an existing name
        placed = True
        try:
            os.unlink(str(src))
        except OSError:
            # The source can't be removed (in use): undo our copy so there is exactly one file, where it was.
            try:
                os.unlink(str(dst))
            except OSError:
                pass
            placed = False
            raise
    finally:
        if not placed:
            _drop_part(tmp)


def _move(src, dst):
    """Move one file so that an existing `dst` is NEVER replaced.  Raises FileExistsError or OSError (a lock is
    recognised by `_is_lock`).  Tests replace this one name to make a move fail."""
    try:
        outdir._move_no_clobber(str(src), str(dst))
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if not _cross_device(exc):
            raise
    _copy_across(src, dst)


def move_file(src, dst):
    """The shared door for the undo tool: always goes through the module's `_move` (so one test seam covers both)."""
    return _move(src, dst)


# --------------------------------------------------------------------------- the record (replaced.json)

def _good_name(name):
    return (isinstance(name, str) and outdir.safe_part(name) and not name.startswith(".")
            and name.lower().endswith(EXTENSIONS))


def write_record(batch_dir, record):
    """Write replaced.json atomically (temp file, then replace) -> True | False.  Never raises."""
    try:
        path = Path(batch_dir) / RECORD_NAME
        tmp = Path(batch_dir) / (RECORD_NAME + ".tmp")
        text = json.dumps(record, indent=1, ensure_ascii=False)
        with open(str(tmp), "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(path))
        return True
    except Exception:  # noqa: BLE001
        return False


def read_record(batch_id):
    """The validated record of one batch -> (record, None) | (None, why).  Everything in it is checked again: the file
    is on disk and could have been edited, so no name or path in it is trusted."""
    if batch_sort_key(batch_id) is None:
        return None, "that is not a backup id"
    path = replaced_root() / str(batch_id) / RECORD_NAME
    try:
        if outdir.is_link(path) or outdir.is_link(path.parent):
            return None, "the record is a link"
        if os.path.getsize(str(path)) > RECORD_MAX_BYTES:
            return None, "the record is too big"
        with open(str(path), "r", encoding="utf-8") as handle:
            rec = json.load(handle)
    except FileNotFoundError:
        return None, "there is no record for that backup"
    except Exception:  # noqa: BLE001
        return None, "the record can't be read"
    try:
        if not isinstance(rec, dict) or rec.get("v") != 1 or rec.get("id") != batch_id or rec.get("status") not in _STATUSES:
            return None, "the record is not in a form Jarvis knows"
        old, new = rec.get("old"), rec.get("new")
        if not (isinstance(old, dict) and isinstance(new, dict)):
            return None, "the record is incomplete"
        for side in (old, new):
            if not (_good_name(side.get("name")) and isinstance(side.get("sha1"), str) and _SHA1_RE.match(side["sha1"])):
                return None, "the record names a file Jarvis does not accept"
        folder = rec.get("folder")
        if not (isinstance(folder, str) and folder and os.path.isabs(folder) and ".." not in Path(folder).parts
                and "\x00" not in folder):
            return None, "the record names a folder Jarvis does not accept"
        return rec, None
    except Exception:  # noqa: BLE001
        return None, "the record can't be read"


# --------------------------------------------------------------------------- the ONE place that deletes (pruning)

def _remove_batch(batch_id):
    """THE ONLY FUNCTION IN THE MINECRAFT GROUP THAT DELETES ANYTHING THE OWNER COULD WANT BACK.

    Removes one backup batch (and the `undone/<id>` folder beside it) by ID.  It refuses, deleting nothing, unless the id
    looks like a batch id AND the folder is a real directory (not a link) whose real path sits directly inside the real
    ~/.jarvis/modhosts/replaced/ .  -> (True, None) | (False, why).  Used by `prune` and to discard the record-only batch
    of an apply that failed before it moved anything."""
    try:
        if batch_sort_key(batch_id) is None:
            return False, "not a backup id"
        root = replaced_root()
        real_root = os.path.realpath(str(root))
        if outdir.is_link(root) or not os.path.isdir(real_root):
            return False, "the backup folder is not a plain folder"
        for base in (root, root / UNDONE):
            path = base / str(batch_id)
            if not os.path.lexists(str(path)):
                continue
            if outdir.is_link(path) or not os.path.isdir(str(path)):
                return False, "a backup is not a plain folder"
            if os.path.realpath(str(path.parent)) != os.path.realpath(str(base)) \
                    or not outdir.inside(os.path.realpath(str(path)), real_root) \
                    or os.path.realpath(str(path)) == real_root:
                return False, "that folder is outside the backup area"
            shutil.rmtree(str(path))
        return True, None
    except Exception as exc:  # noqa: BLE001 - never raises
        return False, "the backup could not be removed (%s)" % type(exc).__name__


def _batch_bytes(batch_id):
    root = replaced_root()
    return _dir_bytes(root / batch_id) + _dir_bytes(root / UNDONE / batch_id)


def prune(keep_id):
    """Keep the backups within MAX_BATCHES and MAX_BACKUP_BYTES by removing the OLDEST batch, repeatedly, never `keep_id`
    (the batch of the apply that just succeeded).  Only called after a successful apply.  -> {"removed": [ids], "left": n}."""
    removed = []
    try:
        while True:
            ids = batch_ids()
            sizes = {i: _batch_bytes(i) for i in ids}
            over_count = len(ids) > int(MAX_BATCHES)
            over_bytes = sum(sizes.values()) > int(MAX_BACKUP_BYTES)
            if not (over_count or over_bytes):
                break
            oldest = ids[0] if ids else None
            if oldest is None or oldest == keep_id:
                break
            ok, _why = _remove_batch(oldest)
            if not ok:
                break
            removed.append(oldest)
    except Exception:  # noqa: BLE001
        pass
    return {"removed": removed, "left": len(batch_ids())}


# --------------------------------------------------------------------------- the checks before anything moves

def check_folder_rules(folder):
    """The folder rules as the scan has them, here once more -> (Path, None) | (None, failure): an absolute path with no
    '..' and no drive root, `outdir.protected_reason` as written AND as it really resolves, not a link, an existing folder."""
    try:
        if isinstance(folder, Path):
            text = str(folder)
        elif isinstance(folder, str) and folder.strip():
            text = folder.strip()
        else:
            return None, _failed(BAD_DEST, "No folder was named, so nothing was changed.")
        if "\x00" in text:
            return None, _failed(BAD_DEST, "That folder name has an invalid character. Nothing was changed.")
        path = Path(os.path.expanduser(text))
        if not path.is_absolute() or ".." in path.parts or len(path.parts) <= 1:
            return None, _failed(BAD_DEST, "'%s' isn't a usable mods folder (a full path, no '..', no drive root). "
                                           "Nothing was changed." % _clip(text, 60))
        why = outdir.protected_reason(path, writing=True)
        if why:
            return None, _failed(BAD_DEST, "Won't change files there: %s. Nothing was changed." % _clip(why, 100))
        if outdir.is_link(path):
            return None, _failed(BAD_DEST, "The folder is a link/shortcut, so nothing was changed.")
        if not os.path.isdir(path):
            return None, _failed(BAD_DEST, "That folder doesn't exist, so nothing was changed.")
        return path, None
    except Exception as exc:  # noqa: BLE001
        return None, _failed(BAD_DEST, "The folder could not be checked (%s). Nothing was changed." % type(exc).__name__)


def _find_old(folder, old_sha1, deadline):
    """Locate the file with `old_sha1` -> ({"name", "path", "size"}, None) | (None, result)."""
    state = updates.load_state()
    scan = updates.scan_folder(folder, cached_hashes=state.get("hashes"), deadline=deadline)
    if not scan.get("ok"):
        return None, _failed(scan.get("reason") or BAD_DEST, scan.get("error") or "The folder could not be read.")
    hits = [f for f in scan["files"] if f["sha1"] == old_sha1]
    if len(hits) > 1:
        names = ", ".join(_clip(h["name"], 40) for h in hits[:3])
        return None, _failed(AMBIGUOUS, "Two files in the folder are identical (%s), so Jarvis won't guess which to "
                                        "replace. Remove one and check again." % names)
    if not hits:
        if scan.get("partial"):
            return None, _failed(OFFLINE, "The time allowed ran out before the whole folder was read. Nothing was changed.")
        notes = []
        if scan.get("unreadable"):
            notes.append("%d file(s) in the folder could not be read (in use?)" % len(scan["unreadable"]))
        return None, _result(True, "unchanged", "the file changed or is gone; check again", notes=notes)
    return {"name": hits[0]["name"], "path": hits[0]["path"], "size": hits[0]["size"]}, None


def _info_from_entry(plan_entry):
    """The facts of the update entry that matter here -> (info, None) | (None, failure)."""
    if not isinstance(plan_entry, dict):
        return None, _failed(UNSUPPORTED, "The update entry is missing, so nothing was changed.")
    gw = plan_entry.get("get_with")
    if not isinstance(gw, dict):
        return None, _failed(UNSUPPORTED, "The update entry has no source and file id, so nothing was changed.")
    source = str(gw.get("source") or "").strip().lower()
    if source not in HASH_ORDER:
        return None, _failed(UNSUPPORTED, "The update entry names a source Jarvis doesn't use for mods. Nothing was changed.")
    top = plan_entry.get("source")
    if top not in (None, "") and str(top).strip().lower() != source:
        return None, _failed(UNSUPPORTED, "The update entry contradicts itself about its source. Nothing was changed.")
    project, file_id = str(gw.get("project") or "").strip(), str(gw.get("file") or "").strip()
    pattern = _MR_ID_RE if source == "modrinth" else re.compile(r"^[0-9]{1,12}$")
    if not (pattern.match(project) and pattern.match(file_id)):
        return None, _failed(UNSUPPORTED, "The update entry has a project or file id Jarvis can't use. Nothing was changed.")
    latest = plan_entry.get("latest") if isinstance(plan_entry.get("latest"), dict) else {}
    if latest.get("file_id") not in (None, "") and str(latest.get("file_id")) != file_id:
        return None, _failed(UNSUPPORTED, "The update entry names two different files. Nothing was changed.")
    block = plan_entry.get("project") if isinstance(plan_entry.get("project"), dict) else {}
    page = block.get("page_url") if isinstance(block.get("page_url"), str) else ""
    if not (page.startswith("https://") and len(page) <= 300 and "\n" not in page and " " not in page):
        page = "https://modrinth.com/project/%s" % project if source == "modrinth" else ""
    size = latest.get("size")
    return {"source": source, "project": project, "file_id": file_id,
            "name": untrusted.clean_file_name(latest.get("file_name") or ""),
            "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            "page": page, "project_name": untrusted.clean_name(block.get("name") or ""),
            "get_with": {"tool": _clip(gw.get("tool"), 40), "source": source, "project": project, "file": file_id}}, None


def _resolve_new(info):
    """Ask the adapter for the new file and vet it -> (new, None) | (None, failure).  `new` has url, name, size, algo, hash,
    notes.  Nothing is written; the only traffic is the vendor lookup (and the download later)."""
    source, label = info["source"], LABEL[info["source"]]
    adapter = sources.get(source)
    if adapter is None or not callable(getattr(adapter, "get_file", None)):
        return None, _failed(UNSUPPORTED, "%s is not available in this build, so nothing was changed." % label)
    got = adapter.get_file(info["project"], info["file_id"])
    if not isinstance(got, dict) or not got.get("ok") or not isinstance(got.get("file"), dict):
        why = got.get("reason") if isinstance(got, dict) else None
        text = got.get("error") if isinstance(got, dict) else None
        return None, _failed(why or VENDOR_CHANGED, "%s Nothing was changed." % _clip(text or "The file could not be looked up.", 140))
    file = got["file"]
    if str(file.get("file_id") or "") != info["file_id"] or str(file.get("project_id") or "") != info["project"]:
        return None, _failed(VENDOR_CHANGED, "%s answered about a different file than the update names. Nothing was changed." % label)
    url = file.get("download_url")
    if not url:
        why = str(file.get("why") or "")
        page = (" Open %s to get it yourself." % info["page"]) if info["page"] else ""
        if why == "host_not_allowed":
            return None, _failed(VENDOR_CHANGED, "%s gave a download link on a host Jarvis doesn't allow. Nothing was changed." % label)
        return None, _failed(NOT_DISTRIBUTABLE, "%s doesn't let Jarvis download this file (its author blocks it).%s Nothing was changed."
                             % (label, page), project_page=info["page"] or None)
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.username or parts.password or host not in net.allowed_hosts(source):
            return None, _failed(VENDOR_CHANGED, "%s gave a download link on a host Jarvis doesn't allow. Nothing was changed." % label)
    except ValueError:
        return None, _failed(VENDOR_CHANGED, "%s gave a download link that can't be read. Nothing was changed." % label)
    name = untrusted.clean_file_name(file.get("file_name") or "")
    if not _good_name(name) or name != str(file.get("file_name") or "").strip():
        return None, _failed(UNSUPPORTED, "The new file's name ('%s') isn't one Jarvis puts in a mods folder (only plain .jar, "
                                          ".zip or .mrpack names). Nothing was changed." % _clip(name, 50))
    size = file.get("size") if isinstance(file.get("size"), int) and not isinstance(file.get("size"), bool) else None
    if info["name"] and info["name"] != name:
        return None, _failed(VENDOR_CHANGED, "The published file is no longer the one the update found. Check again. Nothing was changed.")
    if info["size"] is not None and size is not None and info["size"] != size:
        return None, _failed(VENDOR_CHANGED, "The published file's size changed since the update was found. Check again. Nothing was changed.")
    if size is not None and size <= 0:
        return None, _failed(VERIFY_FAILED, "%s lists this file as empty. Nothing was changed." % label)
    cap = _cap_bytes()
    limit = HARD_CEILING if cap is None else min(cap, HARD_CEILING)
    if size is not None and size > limit:
        what = "the 512 MB limit" if limit == HARD_CEILING and (cap is None or cap >= HARD_CEILING) else "the download_max_mb limit (%s)" % _size_text(limit)
        return None, _failed(TOO_BIG, "The new file is %s, over %s. Nothing was changed." % (_size_text(size), what))
    hashes = file.get("hashes") if isinstance(file.get("hashes"), dict) else {}
    algo = value = None
    for candidate in HASH_ORDER[source]:
        raw = hashes.get(candidate)
        if isinstance(raw, str) and re.match(r"^[0-9a-f]{%d}$" % _HEX_LEN[candidate], raw.strip().lower()):
            algo, value = candidate, raw.strip().lower()
            break
    if algo is None:
        return None, _failed(VERIFY_FAILED, "no checksum to verify it against, so the file was not replaced. Nothing was changed.")
    deps = [d for d in (file.get("dependencies") or ()) if isinstance(d, dict) and d.get("type") == "required"]
    notes = []
    if deps:
        ids = ", ".join(_clip(d.get("project_id"), 20) for d in deps[:8] if d.get("project_id"))
        notes.append("This version lists %d required dependenc%s (%s). Jarvis never adds them: check they are in the folder."
                     % (len(deps), "y" if len(deps) == 1 else "ies", ids or "ids not given"))
    return {"url": str(url), "name": name, "size": size, "algo": algo, "hash": value, "limit": limit, "notes": notes}, None


# --------------------------------------------------------------------------- the download

def _download(info, new, deadline_seconds):
    """Fetch the file into _staging/ and check it ON DISK -> ({"path", "sha1", "size"}, None) | (None, failure)."""
    root, bad = _ensure_root()
    if bad:
        return None, bad
    staging = root / STAGING
    try:
        if outdir.is_link(staging):
            return None, _failed(BAD_DEST, "Jarvis's staging folder is a link, so nothing was changed.")
        _mkdir_private(staging)
    except OSError as exc:
        return None, _failed(BAD_DEST, "Jarvis's staging folder couldn't be created (%s). Nothing was changed." % _clip(exc, 60))
    label = LABEL[info["source"]]
    wanted = new["size"] if new["size"] is not None else new["limit"]
    try:
        timeout = min(120.0, max(1.0, float(deadline_seconds))) if deadline_seconds and float(deadline_seconds) > 0 else 120.0
    except (TypeError, ValueError):
        timeout = 120.0
    reply = net.fetch(info["source"], new["url"], want="bytes", max_bytes=wanted, timeout=timeout)
    if not reply.get("ok"):
        reason = reply.get("reason") or VENDOR_CHANGED
        text = _clip(reply.get("error") or "%s did not send the file." % label, 140)
        if reason == TOO_BIG and new["size"] is not None:
            return None, _failed(VERIFY_FAILED, "The download was bigger than the published size, so it was thrown away. Nothing was changed.")
        extra = {"notes": [CDN_NOTE]} if info["source"] == "curseforge" and reason not in ("rate_limited", "offline", "no_key", "bad_key") else {}
        return None, _failed(reason, "%s Nothing was changed." % text, **extra)
    data = reply.get("body")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None, _failed(VERIFY_FAILED, "%s sent an empty file, so nothing was changed." % label)
    part = staging / ("%s-%s.part" % (_stamp(_now()), os.urandom(4).hex()))
    fd = None
    try:
        fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(bytes(data))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _drop_part(part)
        return None, _failed(BAD_DEST, "The download couldn't be saved in Jarvis's staging folder (%s). Nothing was changed." % _clip(exc, 60))
    finally:
        data = None
    digests, size = _digest(part, new["algo"], "sha1")
    if digests is None:
        _drop_part(part)
        return None, _failed(VERIFY_FAILED, "The downloaded file couldn't be read back, so it was thrown away. Nothing was changed.")
    if new["size"] is not None and size != new["size"]:
        _drop_part(part)
        return None, _failed(VERIFY_FAILED, "The download is %d bytes but %s lists %d, so it was thrown away. Nothing was changed."
                             % (size, label, new["size"]))
    if digests[new["algo"]] != new["hash"]:
        _drop_part(part)
        return None, _failed(VERIFY_FAILED, "The download's %s checksum does not match the published one, so it was thrown away. "
                                            "Nothing was changed." % new["algo"].upper())
    return {"path": str(part), "sha1": digests["sha1"], "size": size}, None


# --------------------------------------------------------------------------- the swap

def _discard(batch_id):
    """Drop the record-only batch of an apply that stopped before moving anything."""
    _remove_batch(batch_id)


def _put_back(backup_path, old_path):
    """Move the old file back where it was -> None | the text to tell the owner when even that failed."""
    try:
        _move(backup_path, old_path)
        return None
    except Exception as exc:  # noqa: BLE001
        return "Couldn't put the old file back (%s). It is safe at %s; copy it back." % (_clip(exc, 40), backup_path)


def apply_update(folder, old_sha1, plan_entry, *, dry_run=False, deadline_seconds=0):
    """Replace the file whose SHA-1 is `old_sha1` in `folder` with the newer file `plan_entry` (an `updates` entry from
    `modhosts.updates.check_folder`) names.  See the module docstring.  One file per call.  Never raises.

    -> {"ok", "state": done | unchanged | failed, "message", "plan" (dry run), "installed", "needs_restart", "error",
        "backup" (where the old file went), "batch" (its id, for undo), "notes", "pruned", "reason" (failures)}"""
    try:
        return _apply(folder, old_sha1, plan_entry, bool(dry_run), deadline_seconds)
    except Exception as exc:  # noqa: BLE001 - this never raises
        return _failed(UNSUPPORTED, "The replace failed (%s). Check the folder before trying again." % type(exc).__name__)


def _apply(folder, old_sha1, plan_entry, dry_run, deadline_seconds):
    if _guest():
        return _failed(UNSUPPORTED, "Replacing a mod file is the owner's: someone in a chat can't do it. Nothing was changed.")
    sha = str(old_sha1 or "").strip().lower()
    if not _SHA1_RE.match(sha):
        return _failed(UNSUPPORTED, "old_sha1 must be the 40-character SHA-1 of the file to replace. Nothing was changed.")
    try:
        deadline = time.monotonic() + float(deadline_seconds) if deadline_seconds and float(deadline_seconds) > 0 else None
    except (TypeError, ValueError):
        deadline = None
    path, bad = check_folder_rules(folder)
    if bad:
        return bad
    old, bad = _find_old(path, sha, deadline)                     # step 1
    if bad:
        return bad
    info, bad = _info_from_entry(plan_entry)                       # step 2
    if bad:
        return bad
    new, bad = _resolve_new(info)
    if bad:
        return bad
    epoch = _now()
    target = path / new["name"]
    same_name = outdir.norm(target) == outdir.norm(old["path"])
    if not same_name and os.path.lexists(str(target)):
        return _failed(EXISTS, "A file named '%s' is already in the folder, so nothing was changed. Move it away first." % _clip(new["name"], 50))
    if os.name == "nt" and len(str(target)) > outdir.MAX_TARGET_PATH:
        return _failed(BAD_DEST, "The new file's full path is too long for Windows. Nothing was changed.")
    backup_preview = replaced_root() / _stamp(epoch) / old["name"]
    if dry_run:                                                    # step 3: nothing is created
        plan = "replace %s with %s (%s) in %s; the old file is moved to %s" % (
            _clip(old["name"], 80), _clip(new["name"], 80), _size_text(new["size"]) if new["size"] is not None else "size not listed",
            path, backup_preview)
        return _result(True, "unchanged", "dry run: nothing was changed", plan=plan, notes=new["notes"],
                       needs_restart=False)
    if _late(deadline):
        return _failed(OFFLINE, "The time allowed ran out before anything was downloaded. Nothing was changed.")
    got, bad = _download(info, new, deadline_seconds)              # step 4
    if bad:
        return bad
    staged = got["path"]
    if _late(deadline):
        _drop_part(staged)
        return _failed(OFFLINE, "The time allowed ran out after the download, so the file was not swapped in. Nothing was changed.")
    return _swap(path, old, info, new, got, staged, sha, same_name, target, epoch)


def _swap(folder, old, info, new, got, staged, old_sha1, same_name, target, epoch):
    """Steps 5 to 8: record first, old file out, new file in, verify, finish the record, prune."""
    root, bad = _ensure_root()
    if bad:
        _drop_part(staged)
        return bad
    # Just before the move: is the old file still the one we hashed (and still a plain file)?
    now_hash, _size = _digest(old["path"], "sha1")
    if now_hash is None or now_hash["sha1"] != old_sha1 or outdir.is_link(old["path"]):
        _drop_part(staged)
        return _result(True, "unchanged", "the file changed or is gone; check again")
    try:
        batch_id, batch_dir = _new_batch(root, epoch)
    except OSError as exc:
        _drop_part(staged)
        return _failed(BAD_DEST, "A backup folder couldn't be made (%s). Nothing was changed." % _clip(exc, 60))
    backup_path = batch_dir / _free_name(batch_dir, old["name"])
    record = {"v": 1, "id": batch_id, "status": "pending", "when": _iso(epoch), "folder": str(folder), "source": info["source"],
              "project": info["project"], "project_name": info["project_name"],
              "old": {"name": old["name"], "sha1": old_sha1, "size": old["size"]},
              "new": {"name": new["name"], "sha1": got["sha1"], "size": got["size"], "verified_with": new["algo"]},
              "backup_name": backup_path.name, "get_with": info["get_with"], "undone": None}
    if not write_record(batch_dir, record):                        # no record, no swap
        _discard(batch_id)
        _drop_part(staged)
        return _failed(BAD_DEST, "The undo record couldn't be written, so nothing was changed.")
    try:
        _move(old["path"], str(backup_path))                       # step 5: the old file OUT (moved, never deleted)
    except FileExistsError:
        _discard(batch_id)
        _drop_part(staged)
        return _failed(EXISTS, "The backup name was taken, so nothing was changed. Try again.")
    except OSError as exc:
        _discard(batch_id)
        _drop_part(staged)
        text = LOCK_TEXT if _is_lock(exc) else "The old file couldn't be moved to the backup folder (%s). Nothing was changed." % _clip(exc, 60)
        return _failed(BAD_DEST, text)
    try:
        _move(staged, str(target))                                 # the new file IN, replacing nothing
    except (FileExistsError, OSError) as exc:
        lost = _put_back(str(backup_path), old["path"])
        _drop_part(staged)
        if lost:
            record["status"] = "rolled_back"
            write_record(batch_dir, record)
            return _failed(BAD_DEST, lost, backup=str(backup_path), batch=batch_id)
        _discard(batch_id)
        why = LOCK_TEXT if (isinstance(exc, OSError) and not isinstance(exc, FileExistsError) and _is_lock(exc)) \
            else ("A file with the new name appeared in the folder, so nothing was replaced." if isinstance(exc, FileExistsError)
                  else "The new file couldn't be moved into the folder (%s); the old file is back." % _clip(exc, 60))
        return _failed(EXISTS if isinstance(exc, FileExistsError) else BAD_DEST, why)
    placed, _size = _digest(str(target), new["algo"], "sha1")      # step 6: again, from the folder
    if placed is None or placed[new["algo"]] != new["hash"]:
        record["status"] = "rolled_back"
        rejected = batch_dir / _free_name(batch_dir, "rejected-" + new["name"])
        try:
            _move(str(target), str(rejected))
            lost = _put_back(str(backup_path), old["path"])
        except Exception as exc:  # noqa: BLE001
            lost = "The new file did not check out and couldn't be moved aside (%s); the old file is safe at %s." % (_clip(exc, 30), backup_path)
        write_record(batch_dir, record)
        return _failed(VERIFY_FAILED, lost or "The placed file did not match its checksum, so the old file was put back.",
                       backup=str(backup_path), batch=batch_id)
    record["status"] = "done"                                       # step 7
    record["new"]["sha1"] = placed["sha1"]
    notes = list(new["notes"])
    if not write_record(batch_dir, record):
        notes.append("The undo record could not be finished; the old file is safe at %s." % backup_path)
    pruned = prune(batch_id)["removed"]
    message = "replaced %s with %s" % (_clip(old["name"], 55), _clip(new["name"], 55))
    if pruned:
        message += "; removed the oldest backup %s to stay within the limit" % pruned[0]
    return _result(True, "done", message, installed=new["name"], needs_restart=True, backup=str(backup_path), batch=batch_id,
                   notes=notes, pruned=pruned)
