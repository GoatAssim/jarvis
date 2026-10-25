"""A short memory of one stream search (N31c, brief 8D).

`discovery.streams_for` puts what it got into this memory; a second search for the same
`(type, video_id)` inside `TTL_S` (5 minutes) is answered from it and says so (`cached: True`,
`cached_age_s`, `cache_note` in the answer).  It holds the RAW answer, before any ranking, so the same entry
serves a different quality or audio asked a minute later.

Rules:

  * In memory only, per process.  Nothing is written to disk on purpose: a raw answer can hold a direct link
    with the owner's debrid key in it.  A one-shot `jarvis ask` process therefore starts empty; a warm worker
    keeps it for its lifetime.
  * Bounded: `MAX_ENTRIES` searches, and a search with more than `MAX_STREAMS` streams is not kept.
  * Only a search where at least one addon answered AND some stream came back is kept; an empty or failed
    search is never remembered (a release that is not out yet must be asked for again).
  * An entry knows whether it is `complete` (every addon asked had answered or failed) or partial (the play
    intent stopped waiting for slow ones).  `get(..., complete_only=True)` is for a caller that must see what
    a full search sees.
  * Whoever gets an answer gets a deep copy: changing it changes nothing in the memory.

The clock is `_clock`, which the tests replace.
"""

import copy
import threading
import time
from collections import OrderedDict

TTL_S = 300.0
MAX_ENTRIES = 24
MAX_STREAMS = 1500

_clock = time.time                 # replaced by the tests
_lock = threading.Lock()
_entries = OrderedDict()           # (type, video_id) -> {"at": seconds, "answer": dict, "complete": bool}
_VOLATILE = ("cached", "cached_age_s", "cache_note", "complete")


def _key(type_, video_id):
    return (str(type_), str(video_id))


def _purge(now):
    for key in [k for k, e in _entries.items() if now - e["at"] > TTL_S or now < e["at"]]:
        del _entries[key]


def put(type_, video_id, answer, *, complete=True):
    """Remember one search.  -> True when it was kept."""
    if not isinstance(answer, dict):
        return False
    streams = answer.get("streams")
    if not streams or not answer.get("answered") or len(streams) > MAX_STREAMS:
        return False
    stored = copy.deepcopy({k: v for k, v in answer.items() if k not in _VOLATILE})
    now = float(_clock())
    with _lock:
        _purge(now)
        key = _key(type_, video_id)
        _entries.pop(key, None)
        _entries[key] = {"at": now, "answer": stored, "complete": bool(complete)}
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
    return True


def get(type_, video_id, *, complete_only=False):
    """-> a copy of the remembered answer with `cached` True, or None (nothing kept, too old, or partial while
    `complete_only`)."""
    now = float(_clock())
    with _lock:
        _purge(now)
        key = _key(type_, video_id)
        entry = _entries.get(key)
        if entry is None or (complete_only and not entry["complete"]):
            return None
        _entries.move_to_end(key)
    out = copy.deepcopy(entry["answer"])
    age = max(0, int(now - entry["at"]))
    out["cached"] = True
    out["cached_age_s"] = age
    out["cache_note"] = ("the same answer as a search %d s ago (a search is kept %d min); no addon was asked again"
                         % (age, int(TTL_S // 60)))
    out["complete"] = entry["complete"]
    return out


def clear():
    """Forget everything.  -> how many searches were kept."""
    with _lock:
        count = len(_entries)
        _entries.clear()
    return count


def size():
    with _lock:
        _purge(float(_clock()))
        return len(_entries)
