"""A small memory of which show a name meant (brief 8C, item N29).  No network.

    lookup(query, type_)         -> the title this name was resolved to before, or None
    store(query, type_, result)  -> remember it (a clear winner, or the owner's pick after a question)
    forget(query, type_)         -> drop it (the owner said "look again", or the lookup no longer agrees)
    forget_id(item_id)           -> drop every entry that points at an id no addon knows any more
    collapse(results)            -> the same show listed by several addons becomes ONE result
    remember_offer(query, type_, shown) / confirm_id(item_id)
                                 -> after a "several titles match" question, the id the owner chose is stored

Why a file: every chat message is a NEW `jarvis ask` process (web/server.js spawns one per message), so an
in-memory dict would forget the title between "which one?" and the owner's answer.  The file is
`<hub dir>/stremio_titles.json`; `paths.hub_dir()` is resolved on every call (a test may move HOME).

What an entry holds: the id, type, name, year, id kind, and times (stored, last used, how often).  Nothing an
addon wrote beyond that: no description, no image address, no addon name.  A cache is not the data: a damaged,
unreadable or odd file is IGNORED (it reads as empty) and never raises; every public function swallows its own
failure and answers "nothing" rather than break the tool that called it.

Written atomically (a temp file, then os.replace) but without the `.bak` copy atomic_io.write_json makes: losing
this file costs one slower lookup, which is exactly the case atomic_io's header says should not use it.  Two
processes writing at the same moment: the last write wins and may lose the other's newest line.  That costs one
more lookup too.
"""

import json
import math
import os
import re
import threading
import time
import unicodedata

from .. import paths, states
from . import addons as A

FILE_NAME = "stremio_titles.json"
LIFETIME_S = 90 * 86400            # N29a: a show's IMDb id does not change
MAX_ENTRIES = 500                  # oldest (least recently used) out
OFFER_TTL_S = 600                  # how long a "which one?" question can still be answered
MAX_OFFERS = 10
MAX_OFFER_ITEMS = 12
QUERY_LIMIT = 120                  # discovery.find cuts the query to 120 characters too

_now = time.time                   # a test replaces this to move the clock
_lock = threading.RLock()
_ID_QUERY = re.compile(r"^(tt\d{5,10}|kitsu:\d+)$")
_EMPTY = {"entries": {}, "offers": []}


# --------------------------------------------------------------------------- keys

def norm_query(text):
    """\"  Rick  AND morty! \" -> \"rick and morty\".  Letters and digits of any script stay; accents, case,
    punctuation and control characters go.  At most QUERY_LIMIT characters, so a 5,000-character query is one
    short key."""
    raw = unicodedata.normalize("NFKD", str(text if text is not None else "")[:QUERY_LIMIT * 4])
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()
    raw = re.sub(r"[\W_]+", " ", raw, flags=re.UNICODE)
    return " ".join(raw.split())[:QUERY_LIMIT].strip()


def is_id_query(text):
    """An id is looked up by its meta, not searched, so it is never cached."""
    return bool(_ID_QUERY.match(str(text or "").strip()))


def _type_key(type_):
    return A.valid_type(type_) or "any"


def _key(type_key, query):
    return "%s|%s" % (type_key, query)


def _path():
    return paths.hub_dir() / FILE_NAME


# --------------------------------------------------------------------------- reading and cleaning the file

def _num(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def _year(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1800 <= number <= 2200 else None


def _show_id(value):
    """A SHOW id (not an episode id): `tt0000000`, `kitsu:5`, `mf123`.  Two colons would be an episode."""
    item_id = A.valid_id(value)
    return item_id if item_id and item_id.count(":") < 2 else None


def _title_fields(raw):
    """The five fields a stored title may hold, cleaned, or None when it is not a usable title."""
    if not isinstance(raw, dict):
        return None
    item_id, type_ = _show_id(raw.get("id")), A.valid_type(raw.get("type"))
    name = states.clean_text(raw.get("name"), 100) if isinstance(raw.get("name"), str) else None
    if not item_id or not type_ or not name:
        return None
    return {"id": item_id, "type": type_, "name": name, "year": _year(raw.get("year")), "id_kind": A.id_kind(item_id)}


def _clean_entry(raw):
    fields = _title_fields(raw)
    query = norm_query(raw.get("query")) if isinstance(raw, dict) and isinstance(raw.get("query"), str) else ""
    if fields is None or not query:
        return None
    type_key = raw.get("type_key")
    type_key = type_key if type_key == "any" or A.valid_type(type_key) == type_key else None
    if type_key is None:
        return None
    stored = _num(raw.get("stored"))
    last = _num(raw.get("last_used"), stored)
    uses = raw.get("uses")
    uses = uses if isinstance(uses, int) and not isinstance(uses, bool) and 0 <= uses < 10 ** 9 else 0
    return dict(fields, query=query, type_key=type_key, stored=stored, last_used=last, uses=uses)


def _clean_offer(raw):
    if not isinstance(raw, dict):
        return None
    query = norm_query(raw.get("query")) if isinstance(raw.get("query"), str) else ""
    type_key = raw.get("type_key")
    if not query or not (type_key == "any" or A.valid_type(type_key) == type_key):
        return None
    items = [f for f in (_title_fields(i) for i in (raw.get("items") or [])[:MAX_OFFER_ITEMS]) if f]
    if not items:
        return None
    shown = {i["id"] for i in items}
    alias = {}
    if isinstance(raw.get("alias"), dict):
        for hidden, target in list(raw["alias"].items())[:60]:
            hidden_id = _show_id(hidden)
            if hidden_id and target in shown and hidden_id not in shown:
                alias[hidden_id] = target
    return {"query": query, "type_key": type_key, "at": _num(raw.get("at")), "items": items, "alias": alias}


def _read():
    """The file as {"entries": {key: entry}, "offers": [offer]}.  Anything wrong with it reads as empty."""
    state = {"entries": {}, "offers": []}
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  (no file, bad JSON, bad bytes, a folder in the way: all the same)
        return state
    if not isinstance(data, dict):
        return state
    try:
        raw_entries = data.get("entries")
        for raw in (raw_entries.values() if isinstance(raw_entries, dict) else ()):
            entry = _clean_entry(raw)
            if entry:
                state["entries"][_key(entry["type_key"], entry["query"])] = entry
        raw_offers = data.get("offers")
        for raw in (raw_offers if isinstance(raw_offers, list) else ())[:MAX_OFFERS * 3]:
            offer = _clean_offer(raw)
            if offer:
                state["offers"].append(offer)
    except Exception:  # noqa: BLE001
        return {"entries": {}, "offers": []}
    return state


def _prune(state, now):
    entries = {k: e for k, e in state["entries"].items() if now - e["stored"] <= LIFETIME_S}
    if len(entries) > MAX_ENTRIES:
        keep = sorted(entries.items(), key=lambda kv: (kv[1]["last_used"], kv[1]["stored"]), reverse=True)[:MAX_ENTRIES]
        entries = dict(keep)
    offers = [o for o in state["offers"] if 0 <= now - o["at"] <= OFFER_TTL_S]
    state["entries"], state["offers"] = entries, offers[-MAX_OFFERS:]
    return state


def _write(state):
    """True when the file was replaced.  Never raises, and never leaves its temp file behind."""
    path = _path()
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        _prune(state, _now())
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"version": 1, "entries": state["entries"], "offers": state["offers"]}
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(str(tmp), str(path))
        return True
    except Exception:  # noqa: BLE001  (a full disk or a locked file: carry on without saving)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------- lookup / store / forget

def _result(entry):
    return {"id": entry["id"], "type": entry["type"], "name": entry["name"], "year": entry["year"], "id_kind": entry["id_kind"]}


def lookup(query, type_=None):
    """The stored title for this name, or None.  `type_` None means \"any type\": an entry stored for exactly one
    type answers it; two entries of different types for the same name do NOT (that is a real question).  With a
    type, an entry stored for \"any type\" answers it when that entry's type is the one asked."""
    try:
        query_key = norm_query(query)
        if not query_key or is_id_query(query):
            return None
        type_key = _type_key(type_)
        with _lock:
            state, now = _read(), _now()
            entries = state["entries"]
            live = {k: e for k, e in entries.items() if now - e["stored"] <= LIFETIME_S and e["query"] == query_key}
            if type_key == "any":
                hit = next(iter(live.values())) if len(live) == 1 else None
            else:
                hit = live.get(_key(type_key, query_key))
                if hit is None:
                    spare = live.get(_key("any", query_key))
                    hit = spare if spare is not None and spare["type"] == type_key else None
            if hit is None:
                return None
            hit["uses"] += 1
            hit["last_used"] = now
            _write(state)
            return _result(hit)
    except Exception:  # noqa: BLE001
        return None


def store(query, type_, result):
    """Remember `result` (a search result: id, type, name, year ...) for this name.  True when it was stored."""
    try:
        query_key = norm_query(query)
        fields = _title_fields(result)
        if not query_key or is_id_query(query) or fields is None:
            return False
        type_key = _type_key(type_)
        with _lock:
            state, now = _read(), _now()
            key = _key(type_key, query_key)
            old = state["entries"].get(key)
            uses = old["uses"] if old and old["id"] == fields["id"] else 0
            state["entries"][key] = dict(fields, query=query_key, type_key=type_key, stored=now, last_used=now, uses=uses)
            return _write(state)
    except Exception:  # noqa: BLE001
        return False


def forget(query, type_=None):
    """Drop what this name resolved to.  With no type, every type's entry for the name goes; with a type, that
    type's entry and an \"any\" entry of that type (the two `lookup` would have used).  True when something went."""
    try:
        query_key = norm_query(query)
        if not query_key:
            return False
        type_key = _type_key(type_)
        with _lock:
            state = _read()
            drop = [k for k, e in state["entries"].items() if e["query"] == query_key and (
                type_key == "any" or e["type_key"] == type_key or (e["type_key"] == "any" and e["type"] == type_key))]
            for key in drop:
                del state["entries"][key]
            if drop:
                _write(state)
            return bool(drop)
    except Exception:  # noqa: BLE001
        return False


def forget_id(item_id):
    """Drop every entry that points at this id (an id no addon knows).  -> how many went."""
    try:
        item_id = A.valid_id(item_id)
        if not item_id:
            return 0
        with _lock:
            state = _read()
            drop = [k for k, e in state["entries"].items() if e["id"] == item_id]
            for key in drop:
                del state["entries"][key]
            if drop:
                _write(state)
            return len(drop)
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------- the duplicate collapse

def _group_key(result):
    """(normalised name, type, year), or None when the result must stand alone: no year on it (N29b: a missing
    year never collapses), no type, or a name with nothing in it."""
    if not isinstance(result, dict):
        return None
    year, type_ = _year(result.get("year")), A.valid_type(result.get("type"))
    name = norm_query(result.get("name")) if isinstance(result.get("name"), str) else ""
    if year is None or not type_ or not name:
        return None
    return name, type_, year


def _rep_order(position, result):
    """Which duplicate stays visible: an IMDb id, then Cinemeta, then the one the search listed first."""
    cinemeta = str(result.get("addon") or "").strip().lower().startswith("cinemeta")
    return (0 if result.get("id_kind") == "imdb" else 1, 0 if cinemeta else 1, position)


def collapse(results):
    """The same show listed by several addons -> one result (N29).  The same normalised name, the same type and the
    same year, BOTH present.  The kept one is the IMDb result (Cinemeta before another addon), and it takes the place
    of the group's first member in the list, so the order is the search's order.  It gains `also`: the ids that were
    folded into it, so a pick of one of those can still be understood (`confirm_id`).  Nothing is mutated; entries
    that fold into nothing come back as the same objects."""
    out, groups = [], {}
    for position, result in enumerate(results or []):
        if not isinstance(result, dict):
            continue
        key = _group_key(result)
        if key is None:
            out.append(result)
        elif key in groups:
            groups[key][1].append((position, result))
        else:
            groups[key] = (len(out), [(position, result)])
            out.append(None)
    for slot, members in groups.values():
        keep = min(members, key=lambda m: _rep_order(*m))[1]
        also = [m[1]["id"] for m in members if m[1] is not keep and m[1].get("id") and m[1]["id"] != keep.get("id")]
        out[slot] = dict(keep, also=also) if also else keep
    return out


# --------------------------------------------------------------------------- learning from the owner's pick

def remember_offer(query, type_, shown):
    """A \"several titles match\" question was just asked with `shown` (what `collapse` left).  Keep the ids for
    OFFER_TTL_S so `confirm_id` can store the owner's choice.  Never stores an answer to the question itself."""
    try:
        query_key = norm_query(query)
        if not query_key or is_id_query(query):
            return False
        items, alias = [], {}
        for result in (shown or [])[:MAX_OFFER_ITEMS]:
            fields = _title_fields(result)
            if fields is None:
                continue
            items.append(fields)
            for hidden in (result.get("also") or [])[:20]:
                hidden_id = _show_id(hidden)
                if hidden_id:
                    alias[hidden_id] = fields["id"]
        if not items:
            return False
        type_key = _type_key(type_)
        with _lock:
            state, now = _read(), _now()
            state["offers"] = [o for o in state["offers"] if (o["query"], o["type_key"]) != (query_key, type_key)]
            state["offers"].append({"query": query_key, "type_key": type_key, "at": now, "items": items, "alias": alias})
            return _write(state)
    except Exception:  # noqa: BLE001
        return False


def confirm_id(item_id):
    """Contract C4.  The owner chose `item_id` out of the last \"several match\" list: store name -> that title.
    No effect (False) when the id was not offered, or the offer is older than OFFER_TTL_S.  An id that was folded
    into a shown one by `collapse` counts as that shown one.  An episode id (`tt...:1:3`) counts as its show."""
    try:
        wanted = A.valid_id(item_id)
        if not wanted:
            return False
        candidates = [wanted]
        if wanted.count(":") >= 2:
            candidates.append(wanted.split(":")[0] if A.id_kind(wanted) == "imdb" else ":".join(wanted.split(":")[:2]))
        with _lock:
            state, now = _read(), _now()
            for offer in reversed(state["offers"]):
                if not 0 <= now - offer["at"] <= OFFER_TTL_S:
                    continue
                for cand in candidates:
                    target = offer["alias"].get(cand, cand)
                    chosen = next((i for i in offer["items"] if i["id"] == target), None)
                    if chosen is None:
                        continue
                    key = _key(offer["type_key"], offer["query"])
                    old = state["entries"].get(key)
                    uses = old["uses"] if old and old["id"] == chosen["id"] else 0
                    state["entries"][key] = dict(chosen, query=offer["query"], type_key=offer["type_key"],
                                                 stored=now, last_used=now, uses=uses)
                    state["offers"].remove(offer)
                    return _write(state)
            return False
    except Exception:  # noqa: BLE001
        return False
