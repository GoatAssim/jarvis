"""A .torrent file on this PC, for the Stremio engine (brief 22).

The owner may point `dl_add` at the path of a .torrent file.  The Stremio engine's own route rule
(`engines/stremio.py: _match`) claims it, through `local_torrent()` below, and this module is the whole of
what happens to the file:

  1. READ     one regular file whose name ends `.torrent`, at most MAX_BYTES (the cap the engine's link fetch
              uses).  Never a folder, never a link to somewhere else, never a network share, never a path that is
              not absolute.  The file is opened without following links and re-checked on the open handle.
  2. CHECK    the bytes must be a bencoded dictionary with an `info` dictionary that has `pieces`.  The whole
              structure is walked (depth and size bounded), so a text file that merely starts with "d" is refused.
  3. NAME     the infohash is the SHA-1 of the `info` dictionary's own bytes.  The row stores a magnet built from
              it (`magnet:?xt=urn:btih:<hash>&dn=<file name without .torrent>`), so the folder the file was in is
              never stored, never shown and never reaches a notification, and two ways of adding one torrent are
              one download.
  4. KEEP     a copy goes into the hub's own folder, `<hub dir>/torrents/<infohash>.torrent` (content-addressed:
              the same torrent is one file), so the engine can register the real file with the server later, when
              the daemon runs the row, even if the original was moved.  Staging happens when the input is routed,
              which is also when a dry run or the panel's preview routes it: it writes at most one file of at most
              MAX_BYTES per distinct torrent the owner names, and nothing else.  No clean-up is built.

Every sentence this module returns about a bad file names the file by its own name only, never by its path.
The reading rules are the owner's own files only: the add tools already refuse a chat guest before any path
reaches here.

5. LIST     `read_files()` (brief N24) reads the file list of those same bytes with no server, for the
              `torrent_files` tool and the panel's file picker.  It returns names exactly as the file holds them:
              they are untrusted, and `hub/stremio/torrent_files.py` decides what is safe.

Linux-tested only.  The Windows branches (a mapped network drive, reparse points) are written but have not run on
Windows; macOS network volumes are not detected.
"""

import hashlib
import os
import re
import stat
import sys

from .. import paths, states
from . import links

MAX_BYTES = 5 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 2000000
_REPARSE = 0x400                 # FILE_ATTRIBUTE_REPARSE_POINT (a Windows link or junction)
_URL_START = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_NETWORK_FS = frozenset("nfs nfs4 cifs smb3 smbfs afs ncpfs 9p fuse.sshfs sshfs davfs".split())


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


# --------------------------------------------------------------------------- bencode (just enough)

class _Bad(ValueError):
    pass


_INT = re.compile(rb"-?[0-9]+")


def _is_digit(byte):
    return 0x30 <= byte <= 0x39


def _end_of(data, i, depth, budget):
    """The index just after the bencoded value that starts at `i`.  Raises _Bad on anything malformed."""
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        raise _Bad("too complex")
    if i >= len(data):
        raise _Bad("cut off")
    byte = data[i]
    if byte == 0x69:                                           # i<digits>e
        j = data.find(b"e", i + 1)
        if j < 0 or not _INT.fullmatch(data, i + 1, j):
            raise _Bad("bad integer")
        return j + 1
    if _is_digit(byte):                                        # <length>:<bytes>
        j = data.find(b":", i, i + 12)
        if j < 0 or not data[i:j].isdigit():
            raise _Bad("bad string")
        end = j + 1 + int(data[i:j])
        if end > len(data):
            raise _Bad("cut off")
        return end
    if byte in (0x6C, 0x64):                                   # l...e  or  d...e
        is_dict = byte == 0x64
        i += 1
        while True:
            if i >= len(data):
                raise _Bad("cut off")
            if data[i] == 0x65:
                return i + 1
            if is_dict:
                if not _is_digit(data[i]):
                    raise _Bad("bad key")
                i = _end_of(data, i, depth + 1, budget)
            i = _end_of(data, i, depth + 1, budget)
    raise _Bad("not bencode")


def _dict_items(data, start, budget):
    """{key bytes: (value start, value end)} of the dictionary that starts at `start`."""
    if data[start:start + 1] != b"d":
        raise _Bad("not a dictionary")
    i, out = start + 1, {}
    while True:
        if i >= len(data):
            raise _Bad("cut off")
        if data[i] == 0x65:
            return out
        if not _is_digit(data[i]):
            raise _Bad("bad key")
        key_end = _end_of(data, i, 1, budget)
        key = data[data.index(b":", i, key_end) + 1:key_end]
        value_end = _end_of(data, key_end, 1, budget)
        out.setdefault(key, (key_end, value_end))
        i = value_end


def parse(data):
    """The bytes of a .torrent file -> ({infohash, size}, None), or (None, why-not).  Pure: no files."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None, "empty"
    data = bytes(data)
    try:
        budget = [MAX_NODES]
        top = _dict_items(data, 0, budget)
        if b"info" not in top:
            return None, "no info section"
        start, end = top[b"info"]
        info = _dict_items(data, start, budget)
        if b"pieces" not in info:
            return None, "no pieces (a v2-only torrent, or not a torrent)"
    except _Bad as exc:
        return None, str(exc)
    return {"infohash": hashlib.sha1(data[start:end]).hexdigest(), "size": len(data)}, None


# --------------------------------------------------------------------------- the file list (N24)

MAX_FILES_READ = 20000           # file entries kept from one .torrent; the rest are only counted


def _decode(data, i, depth, budget):
    """One bencode value at data[i] -> (python value, next index).  Bytes stay bytes.  Bounded like _end_of()."""
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH or i >= len(data):
        raise _Bad("too big or too deep")
    ch = data[i]
    if ch == 0x69:                                           # i<digits>e
        end = data.find(b"e", i)
        if end < 0 or not _INT.fullmatch(data[i + 1:end]):
            raise _Bad("bad integer")
        return int(data[i + 1:end]), end + 1
    if _is_digit(ch):                                        # <len>:<bytes>
        colon = data.find(b":", i, i + 12)
        if colon < 0:
            raise _Bad("bad string")
        n = int(data[i:colon])
        if colon + 1 + n > len(data):
            raise _Bad("cut off")
        return data[colon + 1:colon + 1 + n], colon + 1 + n
    if ch == 0x6C:                                           # l ... e
        out, i = [], i + 1
        while True:
            if i >= len(data):
                raise _Bad("cut off")
            if data[i] == 0x65:
                return out, i + 1
            val, i = _decode(data, i, depth + 1, budget)
            out.append(val)
    if ch == 0x64:                                           # d ... e
        out, i = {}, i + 1
        while True:
            if i >= len(data):
                raise _Bad("cut off")
            if data[i] == 0x65:
                return out, i + 1
            key, i = _decode(data, i, depth + 1, budget)
            if not isinstance(key, bytes):
                raise _Bad("bad key")
            val, i = _decode(data, i, depth + 1, budget)
            out.setdefault(key, val)
    raise _Bad("not bencode")


def _text(value, limit=4096):
    """A name out of the file as text.  Kept RAW (NUL, slashes and all) so the caller can refuse it: this module
    never decides what is safe, torrent_files.check_path() does."""
    if isinstance(value, bytes):
        return value[:limit].decode("utf-8", "replace")
    return ""


def read_files(data):
    """The file list of a .torrent, from its own bytes, with NO server.  Pure: no files, no network.

    Returns ({name, infohash, single, files: [{path, length, pad}], count, truncated}, None) or (None, why-not).
    `path` is the segments joined with "/" and is UNTRUSTED (it may hold "..", a drive letter, a NUL ...);
    `files` keeps the torrent's own order, which is the file index the Stremio server uses.  At most
    MAX_FILES_READ entries are kept; `count` is the real number.  A v2-only torrent has no list: why-not."""
    head, why = parse(data)
    if head is None:
        return None, why
    data = bytes(data)
    try:
        budget = [MAX_NODES]
        top = _dict_items(data, 0, budget)
        start, _end = top[b"info"]
        tree, _next = _decode(data, start, 0, budget)
    except _Bad as exc:
        return None, str(exc)
    if not isinstance(tree, dict):
        return None, "no info section"
    name = _text(tree.get(b"name.utf-8") or tree.get(b"name"))
    listed, files, count = tree.get(b"files"), [], 0
    if isinstance(listed, list):
        for ent in listed:
            if not isinstance(ent, dict) or not isinstance(ent.get(b"length"), int):
                return None, "a file entry without a length"
            parts = ent.get(b"path.utf-8") if isinstance(ent.get(b"path.utf-8"), list) else ent.get(b"path")
            if not isinstance(parts, list) or not parts:
                return None, "a file entry without a path"
            count += 1
            if len(files) < MAX_FILES_READ:
                attr = ent.get(b"attr") if isinstance(ent.get(b"attr"), bytes) else b""
                files.append({"path": "/".join(_text(p, 1024) for p in parts), "length": max(0, ent[b"length"]),
                              "pad": b"p" in attr})
        single = False
    elif isinstance(tree.get(b"length"), int):
        files, count, single = [{"path": name, "length": max(0, tree[b"length"]), "pad": False}], 1, True
    else:
        return None, "no file list in it (a v2-only torrent)"
    return {"name": name, "infohash": head["infohash"], "single": single, "files": files, "count": count,
            "truncated": count > len(files)}, None


# --------------------------------------------------------------------------- where the hub keeps its copies

def staging_dir():
    return paths.hub_dir() / "torrents"


def _staged_path(infohash):
    digest = links.normalize_infohash(infohash)         # lower-case hex only: no way to name another folder
    return (staging_dir() / ("%s.torrent" % digest)) if digest else None


def stage(found):
    """Keep a copy of a checked file (see KEEP above).  True when the copy is there afterwards."""
    dest = _staged_path(found["infohash"])
    if dest is None:
        return False
    blob = found["blob"]
    try:
        if dest.is_file() and dest.stat().st_size == len(blob):
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name("%s.tmp%d" % (dest.name, os.getpid()))
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, dest)
        return True
    except OSError:
        return False


def staged_blob(infohash):
    """The kept copy for this infohash, or None.  The bytes must still hash to the name they are filed under."""
    dest = _staged_path(infohash)
    if dest is None:
        return None
    try:
        st = os.lstat(dest)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        with open(dest, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        return None
    info, _why = parse(data)
    return data if info and info["infohash"] == links.normalize_infohash(infohash) else None


# --------------------------------------------------------------------------- a path the owner typed

def _unquote(text):
    value = str(text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def looks_like_path(text):
    """True for text that starts like an absolute path (a drive, a root, a share or ~).  Used to keep one out of a title."""
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|//|/|~[\\/])", _unquote(text)))


def _mount_table():
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def on_network(path, mounts_text=None):
    """True when the path is on a network share.  Decided from the TEXT of the path and local tables only: the
    share itself is never touched (it could hang).  A UNC path counts on every system; Windows asks the drive type
    of a lettered drive (written, not run); Linux reads /proc/mounts (the path's own text, so a symlinked folder
    that leads to a share is not seen); macOS volumes are not detected."""
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if sys.platform.startswith("win"):
        drive = os.path.splitdrive(text)[0]
        if len(drive) == 2 and drive[1] == ":":
            try:
                import ctypes
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4      # DRIVE_REMOTE
            except Exception:  # noqa: BLE001 - unknown: not a share
                return False
        return False
    table = mounts_text if mounts_text is not None else _mount_table()
    full, best, best_type = os.path.abspath(text), -1, ""
    for line in table.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1]).rstrip("/") or "/"
        inside = full == mount or full.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) > best:
            best, best_type = len(mount), parts[2]
    return best_type in _NETWORK_FS


def _name_of(path):
    return states.clean_text(os.path.basename(str(path).replace("\\", "/")) or "that file", 60)


def local_torrent(text, mounts_text=None):
    """What does this text mean as a .torrent FILE on this PC?

    Returns (found, problem):
      (None, None)      not a path to a .torrent at all: not this module's business (a URL, a magnet, anything else)
      (None, sentence)  it is meant as one but can't be used: ONE plain sentence, naming the file by its name only
      (found, None)     found = {name, stem, size, infohash, blob} (no path: it is never kept or shown)

    Cheap for everything that is not a .torrent path: one string test, no file is touched."""
    raw = _unquote(text)
    if not raw or len(raw) > 4096 or "\n" in raw or "\r" in raw or "\x00" in raw:
        return None, None
    if _URL_START.match(raw) or not raw.lower().endswith(".torrent"):
        return None, None
    path = os.path.expanduser(raw)
    name = _name_of(path)
    if not os.path.isabs(path) and not path.startswith(("\\\\", "//")):
        return None, "To use a .torrent file I need its full path, starting at the drive or the root folder."
    if on_network(path, mounts_text):
        return None, "%s is on a network share; copy it to this PC and give me that path." % name
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None, "I can't find a file called %s at that path." % name
    except (OSError, ValueError):
        return None, "I can't read %s." % name
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE:
        return None, "%s is a link to somewhere else, so I won't follow it; give me the real file." % name
    if stat.S_ISDIR(st.st_mode):
        return None, "%s is a folder, not a .torrent file." % name
    if not stat.S_ISREG(st.st_mode):
        return None, "%s isn't a regular file." % name
    if st.st_size > MAX_BYTES:
        return None, "%s is larger than %s, too big to be a .torrent file." % (name, _fmt_size(MAX_BYTES))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            again = os.fstat(fd)                       # the handle, not the name: nothing swapped in since the check
            if not stat.S_ISREG(again.st_mode) or again.st_size > MAX_BYTES:
                return None, "%s isn't a regular .torrent file of a sensible size." % name
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None, "I can't read %s." % name
    if len(data) > MAX_BYTES:
        return None, "%s is larger than %s, too big to be a .torrent file." % (name, _fmt_size(MAX_BYTES))
    info, _why = parse(data)
    if info is None:
        return None, "%s isn't a .torrent file (it doesn't look like one inside)." % name
    stem = states.clean_text(name[:-len(".torrent")], links.MAX_NAME)
    return {"name": name, "stem": stem, "size": len(data), "infohash": info["infohash"], "blob": data}, None
