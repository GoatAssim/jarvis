"""The memory of the last stream search, by video id (brief 8C, contract C1, item N26).  No network.

    remember(video_id, candidates)  -> store the ranked list a stream search just produced
    recall(video_id, rank)          -> the candidate at 1-based `rank` of the LAST search for that video id, or None

`video_id` is the id `stream_search` returns as `video_id` / `search_for` (`tt2861424:1:3` for an episode, the IMDb
id for a film).  `candidates` is the list `ranking.rank` returned: dicts with `stream`, `infohash`, `file_idx`,
`name`, `label`, `tier`, `size`, `seeders`, `addon` and whatever else ranking adds (`pack`, `file_known`,
`languages`, `language_match` after 8B).  A search is kept for TTL_S seconds, at most MAX_SEARCHES video ids and
MAX_CANDIDATES candidates each (a `stream_search` shows at most 25 rows, so rank 50 is far past anything shown).

One process or two?  Tool calls of ONE chat turn share a process, but every chat message starts a new
`jarvis ask` (web/server.js spawns one per message), and "play number 2" is normally the NEXT message.  So the
memory is mirrored to `<hub dir>/stremio_picks.json` and `recall` reads the newer of the two copies.  The file is a
cache: a damaged one reads as empty, a failed write is ignored, nothing here raises.

What is kept of a candidate: its numbers, labels and text, cleaned, and a SLIM `stream` holding only what playing
needs (`infoHash`, `fileIdx`, tracker `sources`, `name`/`title`, the file name and size).  A stream's `url`
(a direct link can hold the addon's key) is never kept, in memory or in the file; a candidate that is a direct link
is kept without its address and cannot be played from here.
"""

import copy
import json
import math
import os
import threading
import time

from .. import paths, states
from . import links

FILE_NAME = "stremio_picks.json"
TTL_S = 600
MAX_SEARCHES = 12
MAX_CANDIDATES = 50

_now = time.time                   # a test replaces this to move the clock
_lock = threading.RLock()
_mem = {}                          # video_id -> {"at": float, "candidates": [slim candidate]}

_KEEP = ("infohash", "file_idx", "name", "label", "tier", "size", "seeders", "addon", "resolution", "audio", "codec",
         "source", "torrent", "direct", "binge", "score", "pack", "file_known", "language_match")


def _path():
    return paths.hub_dir() / FILE_NAME


def _video_id(value):
    from . import addons as A
    return A.valid_id(value)


# --------------------------------------------------------------------------- cleaning what is kept

def _scalar(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return states.clean_text(value, 200)
    return None


def _slim_stream(stream):
    """The parts of a Stremio stream that playing needs, and nothing that could hold a secret."""
    if not isinstance(stream, dict):
        return {}
    out = {}
    digest = links.normalize_infohash(stream.get("infoHash") or stream.get("infohash"))
    if digest:
        out["infoHash"] = digest
        idx = stream.get("fileIdx")
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx <= links.MAX_FILE_INDEX:
            out["fileIdx"] = idx
        trackers = []
        for source in stream.get("sources") or []:
            if isinstance(source, str) and source.lower().startswith("tracker:"):
                trackers.append(source)
        out["sources"] = ["tracker:" + t for t in links._clean_trackers(trackers)[:12]]
    for key in ("name", "title"):
        text = states.clean_text(stream.get(key), 160) if isinstance(stream.get(key), str) else None
        if text:
            out[key] = text
    hints = stream.get("behaviorHints") if isinstance(stream.get("behaviorHints"), dict) else {}
    kept = {}
    if isinstance(hints.get("filename"), str) and hints["filename"].strip():
        kept["filename"] = states.clean_text(hints["filename"], 200)
    size = hints.get("videoSize")
    if isinstance(size, int) and not isinstance(size, bool) and size > 0:
        kept["videoSize"] = size
    if kept:
        out["behaviorHints"] = kept
    return out


def _slim(candidate):
    out = {key: _scalar(candidate.get(key)) for key in _KEEP if key in candidate}
    languages = candidate.get("languages")
    if isinstance(languages, (list, tuple)):
        out["languages"] = [states.clean_text(x, 20) for x in list(languages)[:8] if isinstance(x, str)]
    out["stream"] = _slim_stream(candidate.get("stream"))
    return out


def _clean_search(raw):
    """One stored search from the file -> {"at", "candidates"}, or None."""
    if not isinstance(raw, dict) or isinstance(raw.get("at"), bool) or not isinstance(raw.get("at"), (int, float)):
        return None
    if not math.isfinite(raw["at"]) or not isinstance(raw.get("candidates"), list):
        return None
    candidates = [_slim(c) for c in raw["candidates"][:MAX_CANDIDATES] if isinstance(c, dict)]
    return {"at": float(raw["at"]), "candidates": candidates}


# --------------------------------------------------------------------------- the file mirror

def _read_file():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  (no file, bad JSON, bad bytes: nothing remembered)
        return {}
    out = {}
    try:
        searches = data.get("searches") if isinstance(data, dict) else None
        for video_id, raw in (searches.items() if isinstance(searches, dict) else ()):
            vid, search = _video_id(video_id), _clean_search(raw)
            if vid and search is not None:
                out[vid] = search
    except Exception:  # noqa: BLE001
        return {}
    return out


def _prune(table, now):
    live = {v: s for v, s in table.items() if 0 <= now - s["at"] <= TTL_S}
    if len(live) > MAX_SEARCHES:
        newest = sorted(live.items(), key=lambda kv: kv[1]["at"], reverse=True)[:MAX_SEARCHES]
        live = dict(newest)
    return live


def _write_file(table):
    path = _path()
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "searches": table}, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(str(tmp), str(path))
        return True
    except Exception:  # noqa: BLE001  (a full disk or a locked file: the in-process copy still works)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _newer(*searches):
    found = [s for s in searches if s is not None]
    return max(found, key=lambda s: s["at"]) if found else None


# --------------------------------------------------------------------------- the two functions of the contract

def remember(video_id, candidates):
    """Store `candidates` (the ranked list, best first) as the last search for `video_id`.  True when kept
    (in this process; the file mirror is best effort).  An empty list is kept too: the last search found nothing."""
    try:
        vid = _video_id(video_id)
        if not vid:
            return False
        slim = [_slim(c) for c in list(candidates or [])[:MAX_CANDIDATES] if isinstance(c, dict)]
        with _lock:
            now = _now()
            _mem[vid] = {"at": now, "candidates": slim}
            live = _prune(_mem, now)
            _mem.clear()
            _mem.update(live)
            # the file may hold another process' newer searches: keep those, and ours where ours is newer
            merged = _read_file()
            for key, search in _mem.items():
                merged[key] = _newer(merged.get(key), search)
            _write_file(_prune(merged, now))
        return True
    except Exception:  # noqa: BLE001
        return False


def recall(video_id, rank):
    """The candidate at 1-based `rank` of the last search for `video_id`, or None (nothing remembered, expired,
    or no such rank).  A copy: changing it changes nothing stored."""
    try:
        vid = _video_id(video_id)
        if not vid or isinstance(rank, bool):
            return None
        number = int(rank)
        if number < 1:
            return None
        with _lock:
            now = _now()
            search = _newer(_mem.get(vid), _read_file().get(vid))
            if search is None or not 0 <= now - search["at"] <= TTL_S:
                return None
            candidates = search["candidates"]
            return copy.deepcopy(candidates[number - 1]) if number <= len(candidates) else None
    except Exception:  # noqa: BLE001
        return None


def clear():
    """Forget everything (this process and the file).  For tests and for a reset; nothing calls it in normal use."""
    try:
        with _lock:
            _mem.clear()
            try:
                _path().unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
