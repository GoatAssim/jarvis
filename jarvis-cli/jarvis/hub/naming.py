"""Filenames and destinations (plan sections 13 and 14).

Names come from places the owner doesn't control (a `Content-Disposition` header, a URL, a
video title), so they are cleaned here before they become part of a path: reserved Windows
characters and device names, trailing dots and spaces, `..`, path separators and over-long
names.  And every destination is checked to resolve INSIDE the library root -- the hub never
writes anywhere else (and `dl_add`'s `dest` is only ever a sub-folder of it).
"""

import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WIN_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)]
)
MAX_NAME = 120
MAX_EXT = 16
MAX_SUBDIR_DEPTH = 4

VIDEO_EXT = frozenset(".mp4 .mkv .avi .mov .webm .m4v .wmv .flv .ts .mpg .mpeg .m2ts".split())
AUDIO_EXT = frozenset(".mp3 .flac .wav .ogg .opus .m4a .aac .wma .oga".split())
_EXT_BY_TYPE = {
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/mpeg": ".mpg",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/flac": ".flac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac", "audio/webm": ".webm",
    "application/pdf": ".pdf", "application/zip": ".zip", "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz", "application/x-tar": ".tar", "text/plain": ".txt",
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
}


def sanitize_filename(name, default="download", max_len=MAX_NAME):
    """One safe file name (never a path).  Falls back to `default` when nothing is left."""
    text = unicodedata.normalize("NFC", str(name if name is not None else ""))
    text = text.replace("\\", "/").split("/")[-1]           # a path in a name -> its last part
    text = _BAD_CHARS.sub("_", text)
    text = re.sub(r"\.{2,}", ".", text)                      # no ".." survives
    text = text.strip(" .")                                  # Windows drops trailing dots/spaces
    if not text:
        text = default
    stem, ext = os.path.splitext(text)
    if not stem:                                             # e.g. ".gz" alone
        stem, ext = text.lstrip("."), ""
    if len(ext) > MAX_EXT:
        stem, ext = text, ""
    if stem.split(".")[0].lower() in _WIN_RESERVED:          # CON, NUL, COM1.txt ...
        stem = "_" + stem
    room = max(8, max_len - len(ext))
    if len(stem) > room:
        stem = stem[:room].rstrip(" .") or default
    return (stem + ext) or default


def filename_from_content_disposition(header):
    """The raw filename from a Content-Disposition header (RFC 6266 / 5987), or None.
    The caller still runs it through sanitize_filename."""
    if not header:
        return None
    text = str(header)
    m = re.search(r"filename\*\s*=\s*([^';]*)'[^']*'([^;]+)", text, re.I)
    if m:
        charset = (m.group(1) or "utf-8").strip() or "utf-8"
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=charset, errors="replace")
        except LookupError:
            return unquote(m.group(2).strip().strip('"'), errors="replace")
    m = re.search(r'(?<![\w*])filename\s*=\s*"((?:[^"\\]|\\.)*)"', text, re.I)
    if m:
        # Only \" and \\ are quoted-pairs here: unescaping every backslash would eat a path
        # separator ("..\\..\\x.exe") before sanitize_filename gets to see it.
        return re.sub(r'\\(["\\])', r"\1", m.group(1))
    m = re.search(r"(?<![\w*])filename\s*=\s*([^;]+)", text, re.I)
    if m:
        return m.group(1).strip()
    return None


def filename_from_url(url):
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return None
    last = unquote(path.rstrip("/").split("/")[-1]) if path else ""
    return last or None


def ext_from_content_type(content_type):
    base = str(content_type or "").split(";")[0].strip().lower()
    return _EXT_BY_TYPE.get(base, "")


def kind_for(name, content_type=None):
    ext = os.path.splitext(str(name or ""))[1].lower()
    base = str(content_type or "").split(";")[0].strip().lower()
    if ext in VIDEO_EXT or base.startswith("video/"):
        return "video"
    if ext in AUDIO_EXT or base.startswith("audio/"):
        return "audio"
    return "file"


def kind_folder(kind):
    return {"video": "Video", "audio": "Audio"}.get(kind, "Files")


def under(root, path):
    """Does `path` resolve to somewhere inside `root` (symlinks followed)?"""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def safe_subdir(root, subdir):
    """`root`/`subdir` as a Path, or None if `subdir` isn't a plain relative sub-folder.

    Refused (not silently repaired): absolute paths, drive letters, `..`, too deep.  The
    owner asked for a place; guessing a different one would hide the mistake.
    """
    text = str(subdir or "").strip().replace("\\", "/")
    if not text:
        return Path(root)
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or "\x00" in text:
        return None
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or len(parts) > MAX_SUBDIR_DEPTH or any(p == ".." for p in parts):
        return None
    cleaned = [sanitize_filename(p, default="_", max_len=60) for p in parts]
    target = Path(root).joinpath(*cleaned)
    return target if under(root, target) else None


def unique_path(path, ignore_part=False):
    """`path`, or `name (1).ext`, `name (2).ext` ... so nothing is overwritten.

    A leftover `.part` from another job counts as taken too, except when the caller is the
    job that OWNS that `.part` (`ignore_part=True`, used when its download finishes).
    """
    path = Path(path)

    def taken(p):
        return p.exists() or (not ignore_part and Path(str(p) + ".part").exists())

    if not taken(path):
        return path
    stem, ext = path.stem, path.suffix
    for n in range(1, 1000):
        candidate = path.with_name("%s (%d)%s" % (stem, n, ext))
        if not taken(candidate):
            return candidate
    return path.with_name("%s (%d)%s" % (stem, os.getpid(), ext))
