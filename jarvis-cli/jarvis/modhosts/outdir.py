"""The safe way for the Minecraft tools to put a file Jarvis BUILT into a folder the owner names.

Used by modhosts/packs.py (make_modpack_file).  It copies the IDEA of the OptiFine writer in
actions/minecraft_optifine.py (exclusive temp file, re-check on disk, rename, never overwrite, the shared protected-places
rule on the path AND its real path, no symlinks, the folder created only at the last moment) and keeps its guards at
least as strict.  It imports nothing from that file.

THE RULES (each one has a test in tests/test_modhosts_packs.py)
  * The folder is an ABSOLUTE path with no '..' and no drive root.  It must pass `path_tools._protected_reason(path,
    writing=True)` as written AND as it really resolves (so a link into ~/.jarvis or C:/Windows is refused too).  If that
    rule cannot be loaded, nothing is written (fail closed).  A tests pins that the function still has this name.
  * The folder itself is never a link (symlink or Windows junction) and never an existing FILE.
  * A folder that does not exist is created only when its PARENT exists (one level, like the picker's New folder), only
    at the last moment (a dry run or a failure before the write creates nothing), and a folder created for a write that
    then failed is removed again when it is empty.
  * The file name is sanitised (hub.naming.sanitize_filename), has a forced extension and no path separator.
  * The bytes go to a temp file IN THE SAME FOLDER (exclusive create), are checked ON DISK by the caller's `validate`,
    the target is checked AGAIN, and only then is the temp file moved into place.  Without `overwrite` the move cannot
    replace anything (os.link / os.rename refuse an existing name, so even a file that appears at the last instant
    survives); with `overwrite` only a plain FILE is replaced (os.replace), never a folder or a link.
  * Any failure removes the temp file.  Nothing half-written is ever left under the final name.

Every function returns a plain dict or a (value, failure) pair and never raises, except `Refused`, which a `build`
callback may raise to stop the write with a reason.
"""
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from . import BAD_DEST, EXISTS, UNSUPPORTED, VERIFY_FAILED, fail

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WIN_RESERVED = {"con", "prn", "aux", "nul", "clock$", *("com%d" % i for i in range(1, 10)),
                 *("lpt%d" % i for i in range(1, 10))}
_BAD_PART_CHARS = re.compile(r'[<>:"|?*\\/\x00-\x1f\x7f]')
MAX_TARGET_PATH = 240                      # a Windows path longer than this fails in many programs


class Refused(Exception):
    """Raised by a build callback to stop the write: carries a reason code and one plain sentence."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


# --------------------------------------------------------------------------- small path facts

def norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def inside(child, parent):
    """True when `child` is `parent` or below it (compared as written, case-insensitive on Windows)."""
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def is_link(path):
    """True for a symlink, a Windows junction or any other reparse point.  A path that does not exist is not a link."""
    try:
        st = os.lstat(str(path))
    except (OSError, ValueError):
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def safe_part(name):
    """True when `name` is one plain file or folder name that is safe in a zip and on Windows: no separators, colon or
    other forbidden character, no control character, no trailing dot or space, no '.'/'..', no reserved device name."""
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if len(name) > 200 or _BAD_PART_CHARS.search(name) or name != name.rstrip(" ."):
        return False
    return name.split(".")[0].strip().lower() not in _WIN_RESERVED


# --------------------------------------------------------------------------- the one shared rule

def protected_reason(path, writing=True):
    """Why `path` may not be written to (or, with writing=False, read from), or None.  Asks path_tools._protected_reason
    (drive roots, the home folder itself, OS folders, ~/.jarvis) for the path as written AND for where it really points.
    If the rule cannot be asked, the answer is a refusal: fail closed."""
    try:
        from ..actions import path_tools
        check = path_tools._protected_reason
    except Exception:  # noqa: BLE001
        return "the folder rules (path_tools) couldn't be loaded, so nothing is written"
    try:
        candidates = [Path(path), Path(os.path.realpath(str(path)))]
    except (OSError, ValueError):
        return "that path can't be read, so nothing is written"
    for candidate in candidates:
        try:
            why = check(candidate, writing=writing)
        except Exception:  # noqa: BLE001
            return "the folder rules failed, so nothing is written"
        if why:
            return why
    return None


# --------------------------------------------------------------------------- the destination folder

def resolve_dest(raw, default="~/Downloads"):
    """The folder to write into -> (Path, None) | (None, failure).  Nothing is created here."""
    text = str(raw).strip() if raw not in (None, "") and str(raw).strip() else str(default or "~/Downloads")
    if "\x00" in text:
        return None, fail(BAD_DEST, "That folder name has an invalid character.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        return None, fail(BAD_DEST, "Give a full folder path like D:/Packs (or leave dest out for %s); '%s' isn't one."
                          % (default, text[:80]))
    if ".." in path.parts or len(path.parts) <= 1:
        return None, fail(BAD_DEST, "'%s' isn't usable: no '..' and no drive root." % text[:80])
    if text.rstrip("/\\").lower().endswith((".mrpack", ".zip")) and not os.path.isdir(path):
        return None, fail(BAD_DEST, "dest is a FOLDER ('%s' looks like a file). Use filename for the file's name." % text[:80])
    why = protected_reason(path, writing=True)
    if why:
        return None, fail(BAD_DEST, "Won't write there: %s." % why)
    if is_link(path):
        return None, fail(BAD_DEST, "Won't write into a symlink/shortcut folder (%s)." % str(path)[:120])
    if os.path.lexists(path):
        if not os.path.isdir(path):
            return None, fail(BAD_DEST, "%s exists and is not a folder." % str(path)[:120])
    elif not os.path.isdir(path.parent):
        return None, fail(BAD_DEST, "%s doesn't exist and neither does its parent: only one new folder level is created. "
                                    "Make the parent folder first." % str(path)[:120])
    return path, None


def pack_filename(requested, stem, extension):
    """The output name: `requested` (or `stem`) sanitised, with the forced extension (a trailing .zip / .mrpack the
    caller typed is replaced, never doubled).  -> (name, None) | (None, failure)."""
    try:
        from ..hub import naming
        clean = naming.sanitize_filename
    except Exception:  # noqa: BLE001
        return None, fail(UNSUPPORTED, "The file naming rules couldn't be loaded, so nothing is written.")
    text = str(requested if requested not in (None, "") else stem or "")
    base = text.replace("\\", "/").split("/")[-1].strip()
    for ext in (".mrpack", ".zip"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    base = clean(base, default="modpack", max_len=100)
    name = base + extension
    if clean(name, default="") != name or not safe_part(name):
        return None, fail(BAD_DEST, "'%s' isn't a safe file name." % text[:80])
    return name, None


# --------------------------------------------------------------------------- the write

def _target_problem(target, overwrite):
    """None when `target` may be written, else the failure.  Run twice: before the work and again just before the move."""
    if os.path.lexists(target):
        if not overwrite:
            return fail(EXISTS, "%s already exists; nothing was changed. Give another filename, or overwrite=true to "
                                "replace it." % str(target)[:160], path=str(target))
        if is_link(target) or not os.path.isfile(target):
            return fail(BAD_DEST, "%s is not a plain file, so it is never replaced." % str(target)[:160])
    return None


def _move_no_clobber(src, dst):
    """Move `src` to `dst` so that an existing `dst` is NEVER replaced (raises FileExistsError instead)."""
    if sys.platform == "win32":
        os.rename(src, dst)                       # refuses an existing name on Windows
        return
    try:
        os.link(src, dst)                         # atomic, refuses an existing name
    except FileExistsError:
        raise
    except OSError:                               # a file system without hard links: the best that is left
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        os.replace(src, dst)
        return
    _drop(src)                                    # the new name is in place; a temp name that lingers is harmless


def _drop(path):
    try:
        os.unlink(str(path))
    except OSError:
        pass


def write_atomic(folder, name, build, validate, overwrite=False):
    """Write one file `folder/name`.

    build(fh)     writes the bytes to the open temp file (binary); may raise Refused.
    validate(path) -> (ok, why): re-opens the temp file ON DISK and checks it (a zip that re-opens, a manifest that
                  re-parses ...).  Not ok = the temp file is dropped and nothing is kept.
    -> {"ok": True, "path": str, "size": int, "created_folder": bool} | failure.  A failure leaves nothing behind
    (and removes the folder when this call created it and it is empty)."""
    folder = Path(folder)
    target = folder / name
    tmp, made, done = None, False, False
    try:
        if len(str(target)) > MAX_TARGET_PATH and sys.platform == "win32":
            return fail(BAD_DEST, "The full path is longer than %d characters; use a shorter folder or file name."
                        % MAX_TARGET_PATH)
        why = protected_reason(folder, writing=True)       # again, at the last moment
        if why:
            return fail(BAD_DEST, "Won't write there: %s." % why)
        bad = _target_problem(target, overwrite)
        if bad:
            return bad
        if os.path.lexists(folder):
            if is_link(folder) or not os.path.isdir(folder):
                return fail(BAD_DEST, "%s is not a plain folder." % str(folder)[:120])
        else:
            if not os.path.isdir(folder.parent):
                return fail(BAD_DEST, "%s doesn't exist and neither does its parent." % str(folder)[:120])
            os.mkdir(folder)                                # one level, only now
            made = True
        fd, tmp = tempfile.mkstemp(prefix=".jarvis-pack-", suffix=".part", dir=str(folder))
        with os.fdopen(fd, "wb") as fh:
            build(fh)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        ok, reason_text = validate(tmp)
        if not ok:
            return fail(VERIFY_FAILED, "The file didn't check out after it was written (%s). Nothing was kept."
                        % str(reason_text)[:160])
        bad = _target_problem(target, overwrite)            # something may have appeared while we worked
        if bad:
            return bad
        size = os.path.getsize(tmp)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        if overwrite:
            os.replace(tmp, str(target))
        else:
            _move_no_clobber(tmp, str(target))
        tmp, done = None, True
        return {"ok": True, "path": str(target), "size": size, "created_folder": made}
    except Refused as exc:
        return fail(exc.reason, exc.message)
    except FileExistsError:
        return fail(EXISTS, "%s appeared in the meantime; it was not replaced and nothing was kept." % str(target)[:160],
                    path=str(target))
    except OSError as exc:
        return fail(BAD_DEST, "Couldn't write in %s (%s). Nothing was kept." % (str(folder)[:120], str(exc)[:80]))
    except Exception as exc:  # noqa: BLE001 - this never raises
        return fail(UNSUPPORTED, "The file could not be written (%s). Nothing was kept." % type(exc).__name__)
    finally:
        if tmp:
            _drop(tmp)
        if made and not done:
            try:
                os.rmdir(folder)                            # only succeeds while it is empty
            except OSError:
                pass
