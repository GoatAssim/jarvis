"""The files inside a torrent: read the list, keep it safe, choose some (brief N24).

WHAT THIS MODULE IS
  * `lookup(text)`        the file list of a torrent, as one dict.  Read-only and bounded: it starts no download.
  * `check_path(raw)`     is ONE file name from the torrent safe to write?  (a sentence when it is not)
  * `layout(...)`         where a chosen file goes: `<torrent folder>/<folders inside the torrent>/<file>`.
  * `choose(listing, ...)` the chosen indexes -> the files to queue, and the ones refused (per file).
  * `compact(listing, ...)` the bounded, cleaned copy that the model and the panel see.
  * `needs_choice(listing)` the ask-first rule (N24b).

WHERE THE LIST COMES FROM (in this order)
  1. a .torrent file on this PC          `torrentfile.read_files`, no server at all
  2. a .torrent link                     fetched with the SSRF guard, then read the same way, no server
  3. a magnet / hash the hub already has a copy of (`torrentfile.staged_blob`), no server
  4. anything else                       the Stremio server's stats.json (`server.wait_for_files`)

SAFETY (a rule, not a nicety)
  File names from a swarm are written by strangers.  Every name is checked here, PER FILE, before anything is
  queued and again when the engine is about to write.  A name is REFUSED (not silently repaired) when it
  has `..` or an empty / `.` segment, is an absolute path (`/x`, `\\\\x`), starts with a drive letter (`C:`),
  holds a NUL, has a Windows reserved name (CON, NUL, COM1 ...) as a segment, has a segment longer than the cap
  (folders 60 characters, the file name 120: the limits the destination layer can hold without cutting), or sits
  deeper than the destination layer allows.  The other characters Windows dislikes (`: ? * "`) are not hostile:
  they go through `naming.sanitize_filename`, the same as every other download's name.  A refused file is
  listed with its reason; the others of the same torrent are unaffected; nothing is written for it.

  Names shown to the model or the panel are DATA: control characters, bidi marks and other invisible format
  characters are stripped and the text is cut at NAME_CUT characters (`clean_display`).  They never become an
  instruction.

NOT HERE: the queue, the notifications and the panel (service.py, notify.py, hub_ui).
"""

import os
import re
import unicodedata

from .. import naming
from . import config as scfg
from . import links, torrentfile
from . import server as sserver

MAX_PICK = 100                    # files chosen in one call (N24c)
MAX_LIST_MODEL = 200              # entries the model sees; folders collapse to a count beyond it
MAX_LIST_PANEL = 1000             # entries the panel's picker holds; the rest is only counted
NAME_CUT = 120                    # characters of a name shown to the model or the panel
FILE_SEGMENT_CAP = naming.MAX_NAME
DIR_SEGMENT_CAP = 60
SAMPLE_WORDS = ("sample",)

_DRIVE = re.compile(r"^[A-Za-z]:")
_SEP = re.compile(r"[\\/]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --------------------------------------------------------------------------- names as DATA

def clean_display(text, limit=NAME_CUT):
    """A name from the swarm made safe to SHOW: no control, bidi or invisible format characters, one line, cut."""
    value = unicodedata.normalize("NFC", str(text if text is not None else ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp"))
    value = _CONTROL.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[:max(1, limit - 1)].rstrip() + "…"
    return value


# --------------------------------------------------------------------------- one name: safe or refused

def check_path(raw):
    """(segments, None) when a torrent's file name is safe to write under a folder, else (None, reason).

    `raw` is the path inside the torrent, as the swarm wrote it.  Pure.  The reason is ONE short sentence that
    never repeats the hostile text itself."""
    text = raw if isinstance(raw, str) else str(raw if raw is not None else "")
    if "\x00" in text:
        return None, "its name contains a NUL character"
    if not text.strip():
        return None, "it has no name"
    if text[0] in "/\\":
        return None, "its name is an absolute path"
    if _DRIVE.match(text):
        return None, "its name starts with a drive letter"
    segments = _SEP.split(text)
    for number, seg in enumerate(segments):
        last = number == len(segments) - 1
        if seg in ("", ".", ".."):
            return None, "its name has an empty or relative (. / ..) part"
        if _DRIVE.match(seg):
            return None, "a part of its name looks like a drive letter"
        if seg.split(".")[0].strip().lower() in naming._WIN_RESERVED:
            return None, "a part of its name is a reserved Windows name"
        cap = FILE_SEGMENT_CAP if last else DIR_SEGMENT_CAP
        if len(seg) > cap:
            return None, "a part of its name is longer than %d characters" % cap
    return segments, None


def folder_name(name, fallback="torrent"):
    """The folder a torrent's files go into: its own name, made into one safe folder name (never refused:
    it is a label for the torrent, not a path the swarm chose for a file)."""
    return naming.sanitize_filename(clean_display(name, DIR_SEGMENT_CAP * 2), default=fallback, max_len=DIR_SEGMENT_CAP)


def strip_root(raw, root):
    """A stats.json path normally starts with the torrent's own folder name; the list wants the part inside."""
    text = str(raw or "")
    root = str(root or "")
    if root:
        for sep in ("/", "\\"):
            if text.startswith(root + sep) and len(text) > len(root) + 1:
                return text[len(root) + 1:]
    return text


def layout(raw, *, root="", folder="", single=False, base="", base_depth=0):
    """Where one chosen file goes.  -> {"ok": True, "subdir", "filename", "relative"} or {"ok": False, "reason"}.

    `subdir` is relative to the library root (or to the owner's chosen folder): `<base>/<folder>/<dirs>`;
    `filename` is the sanitized last segment.  A single-file torrent has no folder of its own.
    `base_depth` counts the folder levels the owner's destination already uses (the destination layer allows
    MAX_SUBDIR_DEPTH levels in all)."""
    segments, reason = check_path(strip_root(raw, root))
    if segments is None:
        return {"ok": False, "reason": reason}
    dirs, last = segments[:-1], segments[-1]
    levels = ([] if single else [folder or folder_name(root)]) + [naming.sanitize_filename(d, default="_", max_len=DIR_SEGMENT_CAP)
                                                                  for d in dirs]
    if len(levels) + int(base_depth) > naming.MAX_SUBDIR_DEPTH:
        return {"ok": False, "reason": "it sits in folders nested deeper than %d levels" % naming.MAX_SUBDIR_DEPTH}
    filename = naming.sanitize_filename(last, default="file")
    parts = ([base] if base else []) + levels
    subdir = "/".join(p for p in parts if p)
    return {"ok": True, "subdir": subdir, "filename": filename,
            "relative": "/".join(levels + [filename]) if levels else filename}


# --------------------------------------------------------------------------- the listing

def _is_video(path):
    return os.path.splitext(str(path))[1].lower() in naming.VIDEO_EXT


def _is_sample(path):
    low = str(path).lower()
    return any(word in low for word in SAMPLE_WORDS)


def _entry(index, raw, length, root, pad=False):
    inside = strip_root(raw, root)
    segments, reason = check_path(inside)
    return {"index": index, "raw": inside, "length": max(0, int(length or 0)), "pad": bool(pad),
            "video": _is_video(inside), "sample": _is_sample(inside), "refused": reason or ""}


def from_torrent(info, origin="file"):
    """A listing from `torrentfile.read_files()`'s answer."""
    name = str(info.get("name") or "")
    single = bool(info.get("single"))
    # a .torrent file's paths are already INSIDE the torrent's folder: nothing to strip (a folder that happens to
    # share the torrent's name must stay)
    files = [_entry(i, f["path"], f["length"], "", f.get("pad")) for i, f in enumerate(info["files"])]
    return _listing(files, name, info.get("infohash") or "", single, info.get("count", len(files)), origin)


def from_stats(stats, infohash=""):
    """A listing from the Stremio server's stats.json (`files` entries have `path`, `name`, `length`)."""
    name = str((stats or {}).get("name") or "")
    raw_files = [f for f in ((stats or {}).get("files") or []) if isinstance(f, dict)]
    files = [_entry(i, f.get("path") or f.get("name") or "", f.get("length"), name) for i, f in enumerate(raw_files)]
    single = len(files) == 1 and files[0]["raw"] == name
    return _listing(files, name, infohash or str((stats or {}).get("infoHash") or ""), single, len(files), "server")


def _listing(files, name, infohash, single, count, origin):
    return {"ok": True, "origin": origin, "name": name, "infohash": infohash, "single": single, "files": files,
            "count": int(count), "truncated": int(count) > len(files),
            "total_size": sum(f["length"] for f in files), "folder": folder_name(name, fallback=(infohash or "torrent")[:12])}


def real_files(listing):
    """The files that count as "a real file": not empty, not a padding file, not a sample clip."""
    return [f for f in listing["files"] if f["length"] > 0 and not f["pad"] and not f["sample"]]


def needs_choice(listing):
    """N24b, the ask-first rule: True when there is more than one real file and not exactly one real video.
    Exactly one real video (with its subtitles and notes around it) keeps today's behaviour: the engine picks it."""
    real = real_files(listing)
    if len(real) <= 1:
        return False
    return len([f for f in real if f["video"]]) != 1


def _ranges(numbers, limit=NAME_CUT):
    """[3,4,5,9] -> "3-5,9" (cut)."""
    out, nums = [], sorted(set(numbers))
    start = prev = None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(str(start) if start == prev else "%d-%d" % (start, prev))
            start = prev = n
    if start is not None:
        out.append(str(start) if start == prev else "%d-%d" % (start, prev))
    text = ",".join(out)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _view(entry):
    out = {"index": entry["index"], "path": clean_display(entry["raw"]), "size": entry["length"]}
    if entry["video"]:
        out["video"] = True
    if entry["sample"]:
        out["sample"] = True
    if entry["refused"]:
        out["refused"] = entry["refused"]
    return out


def compact(listing, limit=MAX_LIST_MODEL, collapse=True):
    """The cleaned, bounded copy for the model (collapse=True) or the panel (collapse=False).

    -> {"files": [...], "folders": [...], "shown", "hidden"}.  `folders` are runs of files folded into one
    line ({"folder", "files", "size", "indexes": "10-34"}) when there are more than `limit` entries; with
    collapse=False the list is simply cut at `limit` and `hidden` says how many are not in it."""
    visible = [f for f in listing["files"] if not f["pad"]]
    if len(visible) <= limit:
        return {"files": [_view(f) for f in visible], "folders": [], "shown": len(visible),
                "hidden": max(0, listing["count"] - len(listing["files"]))}
    if not collapse:
        return {"files": [_view(f) for f in visible[:limit]], "folders": [], "shown": limit,
                "hidden": listing["count"] - limit}
    by_dir = {}
    for f in visible:
        inside = clean_display(f["raw"], 400)
        by_dir.setdefault(inside.rsplit("/", 1)[0] if "/" in inside else "", []).append(f)
    entries = len(visible)
    folded = set()
    for key, members in sorted(by_dir.items(), key=lambda kv: -len(kv[1])):
        if entries <= limit or not key:
            break
        folded.add(key)
        entries -= len(members) - 1
    files, folders = [], []
    for key, members in by_dir.items():
        if key in folded:
            folders.append({"folder": clean_display(key), "files": len(members),
                            "size": sum(m["length"] for m in members),
                            "indexes": _ranges(m["index"] for m in members)})
        else:
            files.extend(_view(m) for m in members)
    files.sort(key=lambda v: v["index"])
    folders.sort(key=lambda v: v["folder"])
    hidden = 0
    if len(files) + len(folders) > limit:
        hidden = len(files) + len(folders) - limit
        files = files[:max(0, limit - len(folders))]
    return {"files": files, "folders": folders, "shown": len(files) + len(folders),
            "hidden": hidden + max(0, listing["count"] - len(listing["files"]))}


# --------------------------------------------------------------------------- choosing files

def parse_indexes(value):
    """What the caller sent as file_indexes -> ([ints], problem).  Accepts a list of ints (or digit strings).
    Duplicates are dropped, the order is kept.  More than MAX_PICK is a problem, not a silent cut."""
    if value in (None, ""):
        return [], None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [], "file_indexes must be a list of file numbers"
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            return [], "file_indexes must be whole numbers"
        try:
            number = int(str(item).strip())
        except ValueError:
            return [], "file_indexes must be whole numbers"
        if number < 0 or number >= links.MAX_FILE_INDEX:
            return [], "file number %d is not a valid file index" % number
        if number not in out:
            out.append(number)
    if len(out) > MAX_PICK:
        return [], "that is %d files; the limit is %d per call (choose fewer, or add the rest in a second call)" % (
            len(out), MAX_PICK)
    return out, None


def choose(listing, indexes, *, base="", base_depth=0):
    """The chosen file numbers -> ({"accepted": [...], "refused": [...]}).

    accepted: {"index", "length", "subdir", "filename", "relative", "display"}  ready to queue.
    refused:  {"index", "path" (cleaned, cut), "reason"}  that one file only; the rest are unaffected."""
    by_index = {f["index"]: f for f in listing["files"]}
    accepted, refused = [], []
    for number in indexes:
        entry = by_index.get(number)
        if entry is None:
            refused.append({"index": number, "path": "", "reason": "the torrent has no file number %d" % number})
            continue
        shown = clean_display(entry["raw"])
        if entry["pad"] or entry["length"] <= 0:
            refused.append({"index": number, "path": shown, "reason": "it is an empty file"})
            continue
        if entry["refused"]:
            refused.append({"index": number, "path": shown, "reason": entry["refused"]})
            continue
        placed = layout(entry["raw"], folder=listing["folder"], single=listing["single"], base=base,
                        base_depth=base_depth)
        if not placed["ok"]:
            refused.append({"index": number, "path": shown, "reason": placed["reason"]})
            continue
        accepted.append({"index": number, "length": entry["length"], "subdir": placed["subdir"],
                         "filename": placed["filename"], "relative": placed["relative"], "display": shown})
    return {"accepted": accepted, "refused": refused}


# --------------------------------------------------------------------------- finding the list

def _fail(message):
    return {"ok": False, "error": message}


def _settings_view():
    """A tiny stand-in for the engine's ctx: only `setting(name, default=)`, from the hub's own settings."""
    class _Ctx:
        @staticmethod
        def setting(name, default=None):
            try:
                from .. import service
                value = service._store(None).get_settings().get("stremio", {}).get(name)
            except Exception:  # noqa: BLE001
                value = None
            return default if value is None else value
    return _Ctx()


def lookup(text, *, budget_s=20.0, server=None, fetch=None):
    """The file list of whatever `text` names.  Never raises.  Starts no download.

    -> {"ok": True, **listing, "kind": "file"|"link"|"magnet"|"stream"..., "file_idx": the file the source already
    names or None, "source": what the caller passes back to add the chosen files}  or  {"ok": False, "error"}.

    `server` and `fetch` are for the tests (a StremioServer-like object; a function url -> (bytes, problem))."""
    try:
        return _lookup(text, budget_s, server, fetch)
    except Exception as exc:  # noqa: BLE001 - tools never raise
        return _fail("couldn't read that torrent: %s" % clean_display(exc, 160))


def _from_blob(blob, kind, source, file_idx=None):
    info, why = torrentfile.read_files(blob)
    if info is None:
        return _fail("that isn't a usable .torrent file (%s)" % clean_display(why, 80))
    listing = from_torrent(info)
    # `blob` (the .torrent bytes) is for the caller that queues files, so it can keep a copy for the engine
    # (torrentfile.stage); it is never shown to the model or the panel.
    return dict(listing, kind=kind, file_idx=file_idx, source=source, blob=blob)


def _lookup(text, budget_s, server, fetch):
    value = str(text or "").strip()
    if not value:
        return _fail("give me a magnet link, a torrent hash, a .torrent link or the path of a .torrent file")
    found, problem = torrentfile.local_torrent(value)
    if problem:
        return _fail(problem)
    if found is not None:
        return _from_blob(found["blob"], "file", value)
    parsed = links.parse_input(value, scfg.server_hostport())
    if not parsed:
        return _fail("that is not a magnet link, a 40-letter torrent hash, a .torrent link or a .torrent file I can read")
    if parsed["kind"] == "torrent_url":
        grab = fetch
        if grab is None:
            from ..engines import stremio as engine
            ctx = _settings_view()
            grab = lambda url: engine._fetch_torrent(url, ctx)  # noqa: E731
        blob, why = grab(parsed["url"])
        if blob is None:
            return _fail(why or "couldn't fetch that .torrent link")
        return _from_blob(blob, "link", value)
    digest = parsed["infohash"]
    source = links.build_magnet(digest, None, parsed.get("name"), parsed.get("trackers"))
    kept = torrentfile.staged_blob(digest)
    if kept is not None:
        return _from_blob(kept, "magnet", source, parsed.get("file_idx"))
    box = server
    if box is None:
        up, why = sserver.probe()
        if not up:
            return _fail("the Stremio server isn't running (%s), so I can't read that torrent's file list; "
                         "start it with stack_start and ask again" % clean_display(why, 100))
        box = sserver.default()
    try:
        got = sserver.wait_for_files(box, digest, parsed.get("trackers") or (), budget_s=budget_s)
    finally:
        if server is None:
            box.close()
    if got["state"] == "files":
        listing = from_stats(got["stats"], digest)
        return dict(listing, kind=parsed["kind"], file_idx=parsed.get("file_idx"), source=source)
    if got["state"] == "error":
        return _fail(clean_display(got["error"], 200))
    return _fail("the torrent gave no file list in %d s (no peers yet?); try again later" % int(round(budget_s)))
