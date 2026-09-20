"""File-system path tools: move, copy, rename, make a folder, delete (to the
Recycle Bin).

WHY THIS FILE EXISTS (master plan F.3)
--------------------------------------
Before this file the `files` router group held seven tools and not one of
them could move anything: search_files, reveal_in_explorer, open_file_location,
open_file, write_file, organize_json, present_file. "Move report.pdf to the
studying folder" — the most ordinary file request there is — was only
reachable through `run_custom_command` (a shell string, in the `commands`
group the router never loaded for that message), so the model burned its
whole round budget probing made-up tool names (`move_file`, `run_command {}`,
`python_eval {}`) and never moved the file.

DESIGN
------
* Argument names are `src` / `dest` / `path` on purpose. policy.py's
  `_paths_in()` only recognises a fixed list of argument names when it scores
  a call for "touches ~/.ssh" / "writes outside your usual folders"; `dest`
  is on that list, the plan's original `dst` is not, and a path the policy
  can't see is a path it can't score.
* Safety is declared the ordinary way: TOOL_CONFIRM_REQUIRED at the bottom of
  this file, which tools.py folds into tool_safety.DEFAULT_CONFIRM_REQUIRED at
  import time (exactly what actions/code_agent.py and workspace_tools.py do).
  tool_safety.py itself is NOT modified — the plan (F.3) assumed it would
  have to be, but the action-file mechanism already covers this, and
  AGENTS.md forbids touching that file as a drive-by.
* Nothing here overwrites by accident. An existing destination is an error
  unless `overwrite=true`, and even then only a *file* is ever replaced —
  never a folder (that would silently merge or destroy a tree).
* Every mutating tool VERIFIES afterwards (the destination exists, the source
  is gone for a move) and reports `{ok, from, to}` — so a model can say "done"
  because it is, not because a call returned.
* A few places are refused outright regardless of confirmation: a drive/
  filesystem root, the home folder itself, OS directories, and Jarvis's own
  state folder (~/.jarvis). The last one matters: `copy_path(evil.json,
  ~/.jarvis/tool_safety.json, overwrite=true)` would otherwise be a way to
  rewrite the confirm-gate's own config through the very tool it gates, and
  reading ~/.jarvis/ai_config.json out to a public folder would leak API keys.
* Deleting goes to the Recycle Bin / Trash via the optional `send2trash`
  package and refuses (touching nothing) when it isn't installed. There is no
  permanent-delete path in this file, deliberately.

Never raises: every handler returns {"error": ...} (with a machine-readable
`reason`) instead, like every other tool file.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Windows filenames can't contain these; checked only for rename_path's
# `new_name` (a bare name), and only on Windows, where it is a real error
# rather than a stylistic one.
_WIN_BAD_NAME_CHARS = '<>:"/\\|?*'

_POSIX_SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys")
_WIN_SYSTEM_DIRS = ("windows", "program files", "program files (x86)", "programdata")


def _err(reason, message, **extra):
    out = {"error": message, "reason": reason}
    out.update(extra)
    return out


def _truthy(value):
    """Models send explicit null / "true" strings; both are normal."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _text(args, key):
    value = (args or {}).get(key)
    return value.strip() if isinstance(value, str) else ""


def _resolve(raw):
    """Absolute, normalised path. `~` expands; a relative path resolves
    against the home folder (same convention as write_file). No symlink
    resolution on purpose: moving a symlink should move the link."""
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path.home() / p
    return Path(os.path.abspath(str(p)))


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p)))


def _is_inside(child, parent):
    c, par = _norm(child), _norm(parent)
    return c == par or c.startswith(par.rstrip("\\/") + os.sep)


def _protected_reason(path, *, writing):
    """Why this path must never be touched by these tools, or None.

    `writing` is True for anything that changes/removes/creates at the path
    (a move's source, a delete, every destination). A plain copy SOURCE is
    only read, so OS directories are fine to copy *from*; ~/.jarvis is
    refused for every role (see module docstring).
    """
    p = Path(path)
    if _is_inside(p, Path.home() / ".jarvis"):
        return "that is Jarvis's own settings/state folder (~/.jarvis) — these tools never touch it"
    if not writing:
        return None
    if p.parent == p:
        return "that is a drive/filesystem root"
    if _norm(p) == _norm(Path.home()):
        return "that is your home folder itself"
    if sys.platform == "win32":
        parts = [x.lower() for x in p.parts]
        # ("C:\\", "Windows", ...) — only the top level of the drive counts.
        if len(parts) >= 2 and parts[1] in _WIN_SYSTEM_DIRS:
            return "that is inside a Windows system folder"
    else:
        for d in _POSIX_SYSTEM_DIRS:
            if _is_inside(p, d):
                return "that is inside an operating-system folder"
    return None


def _kind(p):
    return "dir" if os.path.isdir(p) and not os.path.islink(p) else "file"


def _ends_with_sep(raw):
    return raw.endswith(("/", "\\")) or raw.endswith(os.sep)


def _plan_target(src, dest_raw, *, make_parents):
    """Work out the final path for a move/copy and validate its folder.

    Returns (target, None) or (None, error_dict). `dest` may be an existing
    folder (the item goes *into* it, keeping its name), a trailing-slash
    folder that doesn't exist yet, or a full new path.
    """
    dest = _resolve(dest_raw)
    if os.path.isdir(dest):
        return Path(os.path.join(str(dest), src.name)), None
    if _ends_with_sep(dest_raw):
        # "…/studying/" that doesn't exist: the person named a folder.
        if not make_parents:
            return None, _err(
                "dest_folder_missing",
                f"destination folder does not exist: {dest}",
                hint="Check the folder name (search_files can find it), or pass make_parents=true to create it.",
            )
        return Path(os.path.join(str(dest), src.name)), None
    parent = dest.parent
    if not os.path.isdir(parent) and not make_parents:
        return None, _err(
            "dest_parent_missing",
            f"the folder that would contain the destination does not exist: {parent}",
            hint="Check the path (search_files can find it), or pass make_parents=true to create it.",
        )
    return dest, None


def _check_target_free(src, target, overwrite):
    """None if `target` can be written, else an error dict. Only a FILE may
    ever be overwritten, and only by another file."""
    if not os.path.lexists(target):
        return None
    if not overwrite:
        return _err("dest_exists", f"destination already exists: {target}",
                    hint="Pass overwrite=true to replace it (files only), or choose another name.")
    if os.path.isdir(target) and not os.path.islink(target):
        return _err("dest_is_dir", f"destination is an existing folder, which is never overwritten or merged: {target}")
    if os.path.isdir(src) and not os.path.islink(src):
        return _err("dest_is_file", f"can't replace a file with a folder: {target}")
    return None


def _replace_file(src, target, *, keep_source):
    """Put `src` at `target`, replacing what is there, without ever leaving
    the destination half-written: copy to a temp file beside the target, then
    os.replace() it into place (atomic on the same volume)."""
    if not keep_source:
        try:
            os.replace(src, target)   # same volume: one atomic step
            return
        except OSError:
            pass                       # cross-volume: fall through to copy
    fd, tmp = tempfile.mkstemp(prefix=".jarvis-", dir=os.path.dirname(str(target)) or ".")
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if not keep_source:
        os.unlink(src)


def _common_checks(src_raw, dest_raw, *, mutates_src):
    if not src_raw:
        return None, None, _err("bad_args", "src is required")
    if not dest_raw:
        return None, None, _err("bad_args", "dest is required")
    src = _resolve(src_raw)
    if not os.path.lexists(src):
        return None, None, _err(
            "src_not_found", f"source does not exist: {src}",
            hint="Use search_files to find the real path before retrying; don't guess.",
        )
    why = _protected_reason(src, writing=mutates_src)
    if why:
        return None, None, _err("protected_path", f"refusing: {why} ({src})")
    return src, dest_raw, None


def _transfer(args, *, keep_source):
    src_raw, dest_raw = _text(args, "src"), _text(args, "dest")
    src, dest_raw, err = _common_checks(src_raw, dest_raw, mutates_src=not keep_source)
    if err:
        return err
    overwrite = _truthy(args.get("overwrite"))
    make_parents = _truthy(args.get("make_parents"))

    target, err = _plan_target(src, dest_raw, make_parents=make_parents)
    if err:
        return err
    why = _protected_reason(target, writing=True)
    if why:
        return _err("protected_path", f"refusing: {why} ({target})")

    same = _norm(src) == _norm(target)
    if same and str(src) == str(target):
        return {"ok": True, "noop": True, "from": str(src), "to": str(target), "kind": _kind(src),
                "note": "source and destination are the same path; nothing to do"}
    is_dir = _kind(src) == "dir"
    if not same:
        # A folder can't go into (or be copied into) itself.
        if is_dir and _is_inside(target, src):
            return _err("dest_inside_src", f"can't put a folder inside itself: {src} -> {target}")
        err = _check_target_free(src, target, overwrite)
        if err:
            return err

    try:
        if make_parents:
            os.makedirs(os.path.dirname(str(target)) or ".", exist_ok=True)
        if same:
            # Case-only difference (Windows/macOS: report.PDF -> report.pdf).
            os.rename(src, target)
        elif keep_source:
            if is_dir:
                shutil.copytree(src, target, symlinks=True)
            elif overwrite and os.path.lexists(target):
                _replace_file(src, target, keep_source=True)
            else:
                shutil.copy2(src, target)
        else:
            if not is_dir and overwrite and os.path.lexists(target):
                _replace_file(src, target, keep_source=False)
            else:
                shutil.move(str(src), str(target))
    except (OSError, shutil.Error) as e:
        return _err("os_error", f"{'copy' if keep_source else 'move'} failed: {e}",
                    **{"from": str(src), "to": str(target)},
                    hint=("If this was a folder on another drive it may be partly copied; check the destination "
                          "before retrying." if is_dir else "Nothing was changed at the source."))

    # Verify. A call that returned is not the same thing as a file that moved.
    if not os.path.lexists(target):
        return _err("verify_failed", f"finished without error but {target} does not exist",
                    **{"from": str(src), "to": str(target)})
    if not keep_source and not same and os.path.lexists(src):
        return _err("verify_failed", f"copied to {target} but the source still exists at {src}",
                    **{"from": str(src), "to": str(target)})
    out = {"ok": True, "from": str(src), "to": str(target), "kind": "dir" if is_dir else "file"}
    if not is_dir:
        try:
            out["bytes"] = os.path.getsize(target)
        except OSError:
            pass
    return out


def tool_move_path(args):
    return _transfer(args or {}, keep_source=False)


def tool_copy_path(args):
    return _transfer(args or {}, keep_source=True)


def tool_rename_path(args):
    args = args or {}
    raw, new_name = _text(args, "path"), _text(args, "new_name")
    if not raw:
        return _err("bad_args", "path is required")
    if not new_name:
        return _err("bad_args", "new_name is required")
    if new_name in (".", "..") or any(sep in new_name for sep in ("/", "\\")):
        return _err("bad_name", "new_name must be a bare name with no folder in it",
                    hint="To put the item in another folder use move_path.")
    if sys.platform == "win32" and (any(c in new_name for c in _WIN_BAD_NAME_CHARS) or new_name.endswith((" ", "."))):
        return _err("bad_name", f"not a valid Windows file name: {new_name!r}")
    src = _resolve(raw)
    if not os.path.lexists(src):
        return _err("src_not_found", f"does not exist: {src}",
                    hint="Use search_files to find the real path before retrying; don't guess.")
    why = _protected_reason(src, writing=True)
    if why:
        return _err("protected_path", f"refusing: {why} ({src})")
    target = src.with_name(new_name)
    if src.name == new_name:
        return {"ok": True, "noop": True, "from": str(src), "to": str(target), "note": "already has that name"}
    case_only = _norm(src) == _norm(target)
    if not case_only and os.path.lexists(target):
        return _err("dest_exists", f"something named {new_name!r} already exists in {src.parent}")
    try:
        os.rename(src, target)
    except OSError as e:
        return _err("os_error", f"rename failed: {e}", **{"from": str(src), "to": str(target)})
    if not os.path.lexists(target) or (not case_only and os.path.lexists(src)):
        return _err("verify_failed", f"rename finished without error but the result isn't as expected ({target})")
    return {"ok": True, "from": str(src), "to": str(target), "kind": _kind(target)}


def tool_make_dir(args):
    args = args or {}
    raw = _text(args, "path")
    if not raw:
        return _err("bad_args", "path is required")
    p = _resolve(raw)
    why = _protected_reason(p, writing=True)
    if why:
        return _err("protected_path", f"refusing: {why} ({p})")
    if os.path.isdir(p):
        return {"ok": True, "path": str(p), "created": False, "note": "folder already exists"}
    if os.path.lexists(p):
        return _err("path_is_file", f"a file (not a folder) already exists there: {p}")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        return _err("os_error", f"couldn't create {p}: {e}")
    if not os.path.isdir(p):
        return _err("verify_failed", f"finished without error but {p} is not a folder")
    return {"ok": True, "path": str(p), "created": True}


def _send2trash():
    """Lazy: send2trash is optional, and importing it at module level would
    make a missing package a discovery-time failure for everyone."""
    try:
        import send2trash  # type: ignore
        return send2trash.send2trash
    except Exception:  # noqa: BLE001 — ImportError, or a broken install
        return None


def tool_delete_path(args):
    args = args or {}
    raw = _text(args, "path")
    if not raw:
        return _err("bad_args", "path is required")
    p = _resolve(raw)
    if not os.path.lexists(p):
        return _err("src_not_found", f"does not exist: {p}",
                    hint="Use search_files to find the real path before retrying; don't guess.")
    why = _protected_reason(p, writing=True)
    if why:
        return _err("protected_path", f"refusing: {why} ({p})")
    trash = _send2trash()
    if trash is None:
        return _err(
            "trash_unavailable",
            "can't move to the Recycle Bin: the optional 'send2trash' package isn't installed. "
            "Nothing was deleted (this tool never deletes permanently).",
            hint="pip install send2trash",
        )
    kind = _kind(p)
    try:
        trash(str(p))
    except Exception as e:  # noqa: BLE001 — send2trash raises platform-specific errors
        return _err("os_error", f"couldn't move to the Recycle Bin: {e}", path=str(p))
    if os.path.lexists(p):
        return _err("verify_failed", f"still exists after deleting: {p}")
    return {"ok": True, "path": str(p), "kind": kind, "recycled": True}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_MOVE_COPY_PROPS = {
    "src": {"type": "string", "description": "Full path of the file or folder to act on. If you only have a name, call search_files first."},
    "dest": {
        "type": "string",
        "description": (
            "Where it goes. An existing folder (the item keeps its name and lands inside it), or a full new path "
            "to give it a different name. Windows paths and ~ are fine."
        ),
    },
    "overwrite": {"type": "boolean", "description": "Replace an existing FILE at the destination. Default false. Folders are never overwritten."},
    "make_parents": {"type": "boolean", "description": "Create the destination folder if it doesn't exist. Default false."},
}

TOOL_SCHEMAS = [
    {
        "name": "move_path",
        "description": (
            "Move a file or folder to another folder (or to a new path). This is THE tool for 'move X to Y' — "
            "don't look for a shell command. Refuses to overwrite unless told to, and checks the result. "
            "Confirmed with the user first."
        ),
        "parameters": {"type": "object", "properties": dict(_MOVE_COPY_PROPS), "required": ["src", "dest"]},
    },
    {
        "name": "copy_path",
        "description": (
            "Copy a file or folder (with its contents) to another folder or path, leaving the original in place. "
            "Confirmed with the user first."
        ),
        "parameters": {"type": "object", "properties": dict(_MOVE_COPY_PROPS), "required": ["src", "dest"]},
    },
    {
        "name": "rename_path",
        "description": (
            "Rename a file or folder in place (same folder, new name). To change folders use move_path. "
            "Never overwrites. Confirmed with the user first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path of the file or folder to rename."},
                "new_name": {"type": "string", "description": "The new bare name, e.g. 'notes-final.txt' — no folder part."},
            },
            "required": ["path", "new_name"],
        },
    },
    {
        "name": "make_dir",
        "description": "Create a folder (and any missing parent folders). Succeeds quietly if it already exists. Confirmed with the user first.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Full path of the folder to create."}},
            "required": ["path"],
        },
    },
    {
        "name": "delete_path",
        "description": (
            "Delete a file or folder by sending it to the Recycle Bin/Trash (recoverable — there is no permanent "
            "delete). Confirmed with the user first."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Full path of the file or folder to delete."}},
            "required": ["path"],
        },
    },
]

TOOLS = {
    "move_path": tool_move_path,
    "copy_path": tool_copy_path,
    "rename_path": tool_rename_path,
    "make_dir": tool_make_dir,
    "delete_path": tool_delete_path,
}

# Joins the existing `files` group, so a "move … to the … folder" message —
# which already routes there on the word "folder" — now also gets these.
TOOL_GROUP = "files"

# "move" alone is also how people talk about the mouse, windows and sliders,
# so the bare word carries not_with exclusions (see tool_registry's
# "screen shot" entry for the same mechanism). The multi-word phrases are
# unambiguous and score at the top of the scale.
_NOT_FILES = ["mouse", "cursor", "pointer", "window", "slider", "volume", "brightness"]
TOOL_KEYWORDS = {
    "move_path": {
        "move": {"weight": 8, "not_with": _NOT_FILES},
        "move file": 10, "move the file": 10, "move this file": 10, "move files": 10,
        "move folder": 10, "move the folder": 10, "move it to": 6,
    },
    "copy_path": {
        "copy file": 10, "copy the file": 10, "copy this file": 10, "copy files": 10,
        "copy folder": 10, "copy the folder": 10, "duplicate file": 9, "duplicate the file": 9,
    },
    "rename_path": {
        "rename": 9, "rename file": 10, "rename the file": 10, "rename folder": 10, "rename the folder": 10,
    },
    "make_dir": {
        "new folder": 8, "create a folder": 9, "create folder": 9, "make a folder": 9,
        "make folder": 9, "make a directory": 9, "mkdir": 9,
    },
    "delete_path": {
        "delete file": 9, "delete the file": 9, "delete this file": 9, "delete folder": 9,
        "delete the folder": 9, "recycle bin": 9, "send to trash": 9, "move to trash": 9,
    },
}

TOOL_PACK_INSTRUCTION = ""  # `files` already has a curated instruction; this can't override it

# Everything here changes the disk. Declared the ordinary action-file way —
# tools.py merges this into tool_safety.DEFAULT_CONFIRM_REQUIRED, and an
# explicit entry in ~/.jarvis/tool_safety.json still overrides it either way.
TOOL_CONFIRM_REQUIRED = {"move_path", "copy_path", "rename_path", "make_dir", "delete_path"}
TOOL_AI_REVIEW = set()   # a second-provider review per move is exactly the confirm churn F.12 complains about
TOOL_RESULT_SPECS = {}
