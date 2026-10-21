"""The folder / file listing behind the Download Hub's path picker (N14, brief 14).

A web page cannot learn the full path of a file or folder the person picks, so the picker asks THIS
module (through `jarvis hub-admin browse` and `POST /api/hub/browse`) to list a folder, and the page
only draws the answer.

What it does:

  * `browse(request)`  - list one folder: its sub-folders and, for a file pick, the files with the
    wanted extensions.  Names and kinds only (a size and a modified time for files), never contents.
    No path -> the roots (Windows drives, home, Desktop, Documents, Downloads, the library root;
    macOS /Volumes and home; Linux / and home).  A path that does not exist answers with the nearest
    folder that does, with a note saying so.
  * `make_folder(request)` - create ONE new folder, one level, inside a folder that exists
    (the picker's "New folder" button).

What it keeps (the same spirit as the other hub-admin operations):

  * read-only listing: it opens no file, reads no content, follows no link into a loop and lists one
    folder at a time (it never walks a tree), so nothing here can run long on a big disk;
  * hidden and system entries are left out unless the caller asks (`hidden: true`);
  * the answer is capped (`MAX_ENTRIES`) with a count of what was left out, and the scan itself is
    capped (`MAX_SCAN`) so a folder with a million files cannot hang the call;
  * network (UNC) paths are not browsed: a dead share could block the call for a long time;
  * `make_folder` makes one folder with a plain name (the hub's own file-name rules), never a path,
    never over something that exists; it is a write, so `hub_admin` refuses it, like `browse`, for an
    unattended process (a chat reply, a scheduled job, a sub-agent);
  * nothing here decides whether a place is ALLOWED for downloads.  The hub still validates the
    chosen path (library root rules, the confirmation outside the library) when the field is saved.

The platform, the home folder, the drive list and the library root can all be passed in, so the tests
can check Windows and macOS answers on any machine.
"""

import os
import re
import sys
from pathlib import Path

MAX_ENTRIES = 500                       # entries returned in one answer
MAX_SCAN = 20000                        # directory entries looked at before giving up counting
MAX_PATH_CHARS = 1024
MAX_PATH_PARTS = 64
MAX_NAME_CHARS = 255
MAX_EXTENSIONS = 24
MAX_NEW_NAME = 100

KINDS = ("folder", "file")

# Windows file attributes (st_file_attributes): hidden and system entries stay out of the list.
_WIN_HIDDEN = 0x2
_WIN_SYSTEM = 0x4

# Folders that are system plumbing even when the platform does not flag them.
_SYSTEM_NAMES = frozenset(n.lower() for n in (
    "$Recycle.Bin", "$RECYCLE.BIN", "System Volume Information", "$WinREAgent", "Config.Msi",
    "Recovery", "$SysReset", "$Windows.~BT", "$Windows.~WS", "found.000", "lost+found",
))
# Pseudo file systems directly under "/" on Linux and friends (not hidden, but never somewhere to save).
_POSIX_TOP_SYSTEM = frozenset(("proc", "sys", "dev"))

_UNC_RE = re.compile(r"^[\\/]{2}[^\\/]")
_DRIVE_ONLY_RE = re.compile(r"^[A-Za-z]:$")
_EXT_RE = re.compile(r"^[a-z0-9][a-z0-9_+\-]{0,15}$")
_NUM_RE = re.compile(r"(\d+)")


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- pure helpers

def natural_key(name):
    """Sort key: case-insensitive, and digits compare as numbers ("Disc 2" before "Disc 10")."""
    parts = _NUM_RE.split(str(name).casefold())
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p != ""]


def is_hidden(name, attrs=None, parent_is_root=False, platform=None):
    """Is this entry hidden or system by default?  `attrs` is `st_file_attributes` (Windows only)."""
    text = str(name)
    if text.startswith("."):
        return True
    if text.lower() in _SYSTEM_NAMES:
        return True
    if attrs and (attrs & (_WIN_HIDDEN | _WIN_SYSTEM)):
        return True
    plat = platform or sys.platform
    if parent_is_root and not plat.startswith("win") and text in _POSIX_TOP_SYSTEM:
        return True
    return False


def norm_extensions(value):
    """[".EXE", "iso", "*.msi", 5, "bad name"] -> [".exe", ".iso", ".msi"] (lower case, with a dot, at most 24)."""
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    out = []
    for raw in value if isinstance(value, (list, tuple)) else []:
        if not isinstance(raw, str):
            continue
        text = raw.strip().lower().lstrip("*").lstrip(".")
        if _EXT_RE.match(text) and "." + text not in out:
            out.append("." + text)
        if len(out) >= MAX_EXTENSIONS:
            break
    return out


def wanted_file(name, extensions):
    """A file is listed when no extension filter is set, or its name ends in one of them."""
    if not extensions:
        return True
    lower = str(name).lower()
    return any(lower.endswith(ext) for ext in extensions)


def clean_path_text(value):
    """What the caller typed -> (text, error).  Refuses NUL, an overlong path and network paths."""
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, "That isn't a path."
    text = value.strip().strip('"')
    if not text:
        return "", None
    if "\x00" in text:
        return None, "That path has an invalid character."
    if len(text) > MAX_PATH_CHARS:
        return None, "That path is too long."
    if _UNC_RE.match(text):
        return None, "Network paths (\\\\server\\share) aren't browsed here. Use a mapped drive letter, or paste the path into the box."
    return text, None


def breadcrumbs(path):
    """An absolute folder path -> [{"name", "path"}, ...] from the drive / root down to the folder.
    The first crumb is the root itself ("C:\\" or "/")."""
    p = Path(path)
    parts = p.parts
    if not parts:
        return []
    out = []
    current = Path(parts[0])
    out.append({"name": parts[0], "path": str(current)})
    for part in parts[1:]:
        current = current / part
        out.append({"name": part, "path": str(current)})
    return out


def parent_of(path):
    """The folder above `path`, or None at a root (a drive or "/")."""
    p = Path(path)
    return None if p.parent == p else str(p.parent)


def _windows_drives():
    """Drive letters that exist, without touching removable or network drives (no probing)."""
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()       # type: ignore[attr-defined]
        return ["%s:\\" % chr(ord("A") + i) for i in range(26) if mask & (1 << i)]
    except Exception:  # noqa: BLE001 - not Windows, or ctypes unavailable
        return []


def _first_existing(candidates):
    for c in candidates:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return None


def roots(platform=None, home=None, library_root=None, drives=None):
    """The places the picker offers on the left, and what it lists when no path is given.

    -> [{"name", "path", "kind"}]   kind: drive | home | desktop | documents | downloads | library | volumes | root
    Only places that exist are returned."""
    plat = platform or sys.platform
    home_path = Path(home) if home else Path.home()
    out = []
    seen = set()

    def add(name, path, kind):
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        out.append({"name": name, "path": str(path), "kind": kind})

    if _first_existing([home_path]):
        add("Home", home_path, "home")
    desk = _first_existing([home_path / "Desktop", home_path / "OneDrive" / "Desktop"])
    if desk:
        add("Desktop", desk, "desktop")
    docs = _first_existing([home_path / "Documents", home_path / "OneDrive" / "Documents"])
    if docs:
        add("Documents", docs, "documents")
    down = _first_existing([home_path / "Downloads"])
    if down:
        add("Downloads", down, "downloads")
    if library_root:
        lib = Path(library_root)
        if _first_existing([lib]):
            add("Download library", lib, "library")
    if plat.startswith("win"):
        for drive in (drives if drives is not None else _windows_drives()):
            add(drive, drive, "drive")
    elif plat == "darwin":
        if _first_existing([Path("/Volumes")]):
            add("Volumes", "/Volumes", "volumes")
    else:
        add("Computer (/)", "/", "root")
    return out


# --------------------------------------------------------------------------- listing

def _real(path):
    """The fully resolved path, or None if it cannot be resolved (a link loop, a vanished folder)."""
    try:
        return os.path.realpath(str(path), strict=True) if sys.version_info >= (3, 10) else os.path.realpath(str(path))
    except (OSError, RuntimeError, ValueError):
        return None


def _is_ancestor_or_same(candidate_real, folder_real):
    """Is `candidate_real` the folder itself or one of its parents?  (A link to that leads nowhere new.)"""
    if not candidate_real or not folder_real:
        return False
    a = os.path.normcase(candidate_real)
    b = os.path.normcase(folder_real)
    if a == b:
        return True
    prefix = a if a.endswith(os.sep) else a + os.sep
    return b.startswith(prefix)


def _normalise(text):
    """Typed text -> an absolute normalised path string, or (None, message)."""
    if _DRIVE_ONLY_RE.match(text) and os.name == "nt":
        text = text + "\\"
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return None, "Type a full path, like D:\\Games or /home/me/Games."
    full = os.path.normpath(expanded)
    if len(Path(full).parts) > MAX_PATH_PARTS:
        return None, "That path is nested too deeply."
    return full, None


def _scan(folder, kind, extensions, hidden, platform):
    """Read one folder.  -> (dirs, files, scan_capped).  Raises OSError when it cannot be read."""
    folder_real = _real(folder)
    parent_is_root = parent_of(folder) is None
    dirs, files = [], []
    scanned = 0
    capped = False
    with os.scandir(folder) as it:
        for entry in it:
            scanned += 1
            if scanned > MAX_SCAN:
                capped = True
                break
            name = entry.name
            if not name or len(name) > MAX_NAME_CHARS:
                continue
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir()                  # follows a link; False for a broken or looping one
                attrs = None
                if not hidden and os.name == "nt":
                    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", None)
            except OSError:
                continue
            if not hidden and is_hidden(name, attrs, parent_is_root, platform):
                continue
            if is_dir:
                if is_link and _is_ancestor_or_same(_real(entry.path), folder_real):
                    continue                              # a link back up the tree: a loop, never new ground
                dirs.append((name, entry.path, is_link))
            elif kind == "file" and wanted_file(name, extensions):
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                files.append((name, entry.path, is_link))
    return dirs, files, capped


def _file_facts(path):
    try:
        st = os.stat(path)
        return int(st.st_size), int(st.st_mtime)
    except OSError:
        return None, None


def browse(request=None, store=None, platform=None, home=None, library_root=None, drives=None):
    """List one folder for the picker.  Never raises; a refusal is {"ok": False, "error": ...}.

    request: {"path": str | None, "kind": "folder" | "file", "extensions": [".exe", ...], "hidden": bool}

    -> {"ok": True, "path": "<folder shown, or '' for the roots>", "requested": "<what was asked>",
        "note": "<why it differs, or ''>", "parent": path | None, "crumbs": [...], "places": [...],
        "entries": [{"name", "path", "kind": "folder"|"file", "link": bool, "size"?, "modified"?}],
        "shown": n, "more": n, "scan_capped": bool, "kind": ..., "extensions": [...],
        "selected": "<file name the typed path pointed at, or ''>"}"""
    req = request if isinstance(request, dict) else {}
    kind = req.get("kind") or "folder"
    if kind not in KINDS:
        return _err("kind must be 'folder' or 'file'")
    extensions = norm_extensions(req.get("extensions")) if kind == "file" else []
    hidden = req.get("hidden") is True
    plat = platform or sys.platform

    text, problem = clean_path_text(req.get("path"))
    if problem:
        return _err(problem)

    if library_root is None:
        library_root = _library_root(store)
    places = roots(platform=plat, home=home, library_root=library_root, drives=drives)

    base = {"ok": True, "kind": kind, "extensions": extensions, "places": places,
            "requested": text, "note": "", "selected": ""}

    if not text:
        return _roots_answer(base, places, "")

    full, problem = _normalise(text)
    if problem:
        return _err(problem)

    # Walk up from the asked path to the nearest folder that exists AND can be read.
    note = ""
    selected = ""
    file_case = False
    candidate = full
    first = True
    while True:
        folder_real = _real(candidate)
        try:
            is_dir = folder_real is not None and os.path.isdir(candidate)
            is_file = folder_real is not None and os.path.isfile(candidate)
        except OSError:
            is_dir = is_file = False
        if is_file and first:
            # a file where a folder is wanted (or a file already chosen): show the folder it sits in
            selected = os.path.basename(candidate)
            file_case = True
            note = ("That is a file, not a folder. Showing the folder it is in." if kind == "folder"
                    else "")
            candidate = os.path.dirname(candidate)
            first = False
            continue
        if is_dir:
            try:
                dirs, files, capped = _scan(candidate, kind, extensions, hidden, plat)
            except OSError:
                if candidate == full:
                    note = "Jarvis can't open that folder. Showing the nearest one it can."
                nxt = parent_of(candidate)
                if nxt is None:
                    return _roots_answer(base, places, note or "Jarvis can't open that place.")
                candidate = nxt
                first = False
                continue
            if candidate != full and not note and not file_case:
                note = "That folder doesn't exist. Showing the nearest one that does."
            return _folder_answer(base, candidate, dirs, files, capped, note, selected)
        nxt = parent_of(candidate)
        if nxt is None:
            return _roots_answer(base, places, "That place doesn't exist. Pick one from the list.")
        candidate = nxt
        first = False


def _library_root(store):
    try:
        from .hub import service, settings as settings_mod
        return str(settings_mod.library_root(service._store(store).get_settings()))   # noqa: SLF001
    except Exception:  # noqa: BLE001 - no hub database yet is not a reason to refuse a listing
        return None


def _roots_answer(base, places, note):
    out = dict(base)
    entries = [{"name": p["name"], "path": p["path"], "kind": "folder", "link": False, "place": p["kind"]} for p in places]
    out.update({"path": "", "parent": None, "crumbs": [], "entries": entries, "shown": len(entries),
                "more": 0, "scan_capped": False})
    if note:
        out["note"] = note
    return out


def _folder_answer(base, folder, dirs, files, capped, note, selected):
    dirs.sort(key=lambda e: natural_key(e[0]))
    files.sort(key=lambda e: natural_key(e[0]))
    total = len(dirs) + len(files)
    keep_dirs = dirs[:MAX_ENTRIES]
    keep_files = files[:max(0, MAX_ENTRIES - len(keep_dirs))]
    entries = [{"name": n, "path": p, "kind": "folder", "link": bool(link)} for n, p, link in keep_dirs]
    for name, path, link in keep_files:
        size, modified = _file_facts(path)
        entry = {"name": name, "path": path, "kind": "file", "link": bool(link)}
        if size is not None:
            entry["size"] = size
            entry["modified"] = modified
        entries.append(entry)
    out = dict(base)
    out.update({"path": str(folder), "parent": parent_of(folder), "crumbs": breadcrumbs(folder),
                "entries": entries, "shown": len(entries), "more": total - len(entries),
                "scan_capped": bool(capped), "selected": selected})
    if note:
        out["note"] = note
    return out


# --------------------------------------------------------------------------- new folder

def check_new_name(name):
    """A new folder's name -> (clean name, error).  The hub's own file-name rules; a bad name is
    refused, not quietly repaired (the owner asked for THAT name)."""
    if not isinstance(name, str):
        return None, "Give the new folder a name."
    import unicodedata
    text = unicodedata.normalize("NFC", name.strip())
    if not text:
        return None, "Give the new folder a name."
    if len(text) > MAX_NEW_NAME:
        return None, "That name is too long (at most %d characters)." % MAX_NEW_NAME
    if "/" in text or "\\" in text:
        return None, "A folder name can't contain / or \\. Make one folder at a time."
    from .hub import naming
    if naming.sanitize_filename(text, default="") != text:
        return None, "A folder can't be called that. Avoid  < > : \" | ? *  a leading dot, a trailing dot or space, and names like CON or NUL."
    return text, None


def make_folder(request=None):
    """Create ONE folder `name` inside the existing folder `parent`.  -> {"ok": True, "path"} or a refusal."""
    req = request if isinstance(request, dict) else {}
    name, problem = check_new_name(req.get("name"))
    if problem:
        return _err(problem)
    text, problem = clean_path_text(req.get("parent"))
    if problem:
        return _err(problem)
    if not text:
        return _err("Open a folder first, then make the new folder in it.")
    full, problem = _normalise(text)
    if problem:
        return _err(problem)
    try:
        if _real(full) is None or not os.path.isdir(full):
            return _err("The folder to make it in no longer exists.")
        target = os.path.join(full, name)
        if os.path.lexists(target):
            return _err("There is already something called '%s' there." % name)
        os.mkdir(target)
        return {"ok": True, "path": target, "name": name, "parent": full}
    except PermissionError:
        return _err("Jarvis isn't allowed to make a folder there.")
    except OSError as exc:
        return _err("Couldn't make the folder: %s" % (exc.strerror or exc))
