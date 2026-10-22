"""Finding things through the owner's Stremio addons (plan 9.1 and 9.3, brief 07, N13).

    find(net, addons, query)                 -> candidates for a title ("jjk" -> a short list)
    episodes(net, addons, type, id)          -> the show's episodes, season by season
    streams_for(net, addons, type, video_id) -> every addon's streams for one episode, in parallel
    library(net, ...) / mark_watched(...)    -> the owner's Stremio library (cloud account)

Nothing here is tied to one id scheme.  A result carries the id and the `type` the addon gave
(`tt0000000` with `series`, `kitsu:123` with whatever type the Kitsu addon uses), and every later
call asks the addons that SAY they handle that type and id prefix, so whichever scheme the
owner's addons use is the one used.

All network goes through addons.Net (the SSRF guard).  Text from an addon is data: it is cleaned
to one short line and only ever shown or ranked, never executed or passed on as an instruction.
"""

import difflib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import states
from . import addons as A

FAN_OUT = 8                    # parallel addon calls (plan 9.3)
MAX_SEARCH_CALLS = 12
MAX_RESULTS = 12
MAX_VIDEOS = 3000
STREAM_BUDGET_S = 40.0         # one stream_search never waits longer than this in total


def _clean(value, limit):
    return states.clean_text(value, limit) or ""


# --------------------------------------------------------------------------- the title

def _poster_fields(meta):
    """Which of the addon meta's image fields are present (brief 05 chooses; this only passes them on)."""
    return {k: meta.get(k) for k in ("poster", "background", "logo") if isinstance(meta.get(k), str) and meta.get(k)}


def _year_of(meta):
    for key in ("year", "releaseInfo", "released"):
        found = re.search(r"(19|20)\d{2}", str(meta.get(key) or ""))
        if found:
            return int(found.group(0))
    return None


def _candidate(meta, addon, cat_type):
    item_id = A.valid_id(meta.get("id") or meta.get("imdb_id"))
    name = _clean(meta.get("name"), 100)
    if not item_id or not name:
        return None
    return {"id": item_id, "type": A.valid_type(meta.get("type")) or cat_type, "name": name, "year": _year_of(meta),
            "id_kind": A.id_kind(item_id), "addon": addon.name,
            "description": _clean(meta.get("description"), 160), "images": _poster_fields(meta)}


def _similarity(query, name):
    q, n = re.sub(r"[^a-z0-9 ]+", " ", query.lower()).split(), re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    if not q or not n:
        return 0.0
    qs, ns = " ".join(q), " ".join(n)
    if qs == ns:
        return 1.0
    ratio = difflib.SequenceMatcher(None, qs, ns).ratio()
    if ns.startswith(qs) or all(w in n for w in q):
        ratio = max(ratio, 0.85)
    return ratio


def find(net, found_addons, query, type_=None):
    """-> {"results": [...], "searched": N, "errors": [...]}  Best match first.

    A direct id (tt... or kitsu:...) is looked up by its meta instead of searched."""
    query = _clean(query, 120)
    type_ = A.valid_type(type_) if type_ else None
    errors = []
    if re.match(r"^tt\d{5,10}$", query) or re.match(r"^kitsu:\d+$", query):
        for kind in ([type_] if type_ else ["series", "movie", "anime"]):
            try:
                meta, addon = _meta(net, found_addons, kind, query)
            except A.AddonError:
                continue
            cand = _candidate(dict(meta, id=meta.get("id") or query), addon, kind)
            if cand:
                cand["score"] = 1.0
                return {"results": [cand], "searched": 1, "errors": errors}
        return {"results": [], "searched": 1, "errors": ["no addon knows that id"]}

    calls = []
    for addon in found_addons:
        if not (addon.supports("catalog") or addon.supports("meta") or addon.search_catalogs()):
            continue
        for cat_type, cat_id in addon.search_catalogs(type_):
            calls.append((addon, cat_type, cat_id))
    calls = calls[:MAX_SEARCH_CALLS]
    results = {}
    if calls:
        with ThreadPoolExecutor(max_workers=min(FAN_OUT, len(calls))) as pool:
            futures = {}
            for addon, cat_type, cat_id in calls:
                try:
                    url = A.resource_url(addon.base, "catalog", cat_type, cat_id, "search=%s" % query)
                except A.AddonError:
                    continue
                futures[pool.submit(net.get_json, url, addon.name)] = (addon, cat_type)
            for future in as_completed(futures):
                addon, cat_type = futures[future]
                try:
                    data = future.result()
                except A.AddonError as exc:
                    errors.append(str(exc))
                    continue
                for meta in (data.get("metas") if isinstance(data, dict) else None) or []:
                    cand = _candidate(meta, addon, cat_type) if isinstance(meta, dict) else None
                    if cand and cand["id"] not in results:
                        results[cand["id"]] = cand
    ranked = []
    for cand in results.values():
        cand["score"] = round(_similarity(query, cand["name"]), 3)
        ranked.append(cand)
    ranked.sort(key=lambda c: (-c["score"], -(c["year"] or 0)))
    return {"results": ranked[:MAX_RESULTS], "searched": len(calls), "errors": errors[:5]}


def is_clear_winner(results):
    """One result, or a top result that clearly beats the next (so no question is needed).

    A result whose normalised name equals the query (score 1.0, see `_similarity`) and which is the
    ONLY such result wins outright: the real Cinemeta answer for "Jujutsu Kaisen" has the series at
    1.0 and the film "Jujutsu Kaisen 0" at 0.933, a gap of 0.067, which the gap rule alone reads as
    "ask".  Two exact matches (two shows with the same name) still ask."""
    if not results:
        return False
    if len(results) == 1:
        return True
    first, second = results[0], results[1]
    exact = [r for r in results if r["score"] >= 1.0]
    if len(exact) == 1 and exact[0] is first:
        return True
    return first["score"] >= 0.9 and first["score"] - second["score"] >= 0.15


# --------------------------------------------------------------------------- the episodes

def _meta(net, found_addons, type_, item_id):
    """-> (meta dict, addon) from the first addon that answers for this type and id."""
    last = None
    for addon in found_addons:
        if not addon.supports("meta", type_, item_id):
            continue
        try:
            data = net.get_json(A.resource_url(addon.base, "meta", type_, item_id), addon.name)
        except A.AddonError as exc:
            last = exc
            continue
        meta = data.get("meta") if isinstance(data, dict) else None
        if isinstance(meta, dict) and (meta.get("videos") or meta.get("name")):
            return meta, addon
    raise last or A.AddonError("no installed addon has details for that title")


def _released(video):
    """True when the episode is out (a missing date counts as out; an unreadable one too)."""
    text = str(video.get("released") or "")
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return True
    try:
        stamp = time.mktime((int(match.group(1)), int(match.group(2)), int(match.group(3)), 0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return True
    return stamp <= time.time() + 86400


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < 10000 else None


def _big(value):
    """A non-negative whole number of any size (milliseconds, counts), else None."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def episodes(net, found_addons, type_, item_id):
    """-> {"title", "id", "type", "images", "videos": [{season, episode, id, title, released_ok}], "addon"}

    `id` of a video is what the stream request needs (`tt...:S:E`, `kitsu:N:E`): taken from the
    addon's own video id, and only built from the show id when the addon left it out."""
    type_, item_id = A.valid_type(type_), A.valid_id(item_id)
    if not type_ or not item_id:
        raise A.AddonError("that isn't a title id I can use")
    meta, addon = _meta(net, found_addons, type_, item_id)
    videos = []
    for raw in (meta.get("videos") or [])[:MAX_VIDEOS]:
        if not isinstance(raw, dict):
            continue
        season = _number(raw.get("season"))
        episode = _number(raw.get("episode") if raw.get("episode") is not None else raw.get("number"))
        vid = A.valid_id(raw.get("id"))
        if (season is None or episode is None) and vid and vid.count(":") >= 2:
            tail = vid.split(":")
            season = _number(tail[-2]) if season is None else season
            episode = _number(tail[-1]) if episode is None else episode
        if season is None or episode is None:
            continue
        if not vid:
            vid = "%s:%d:%d" % (item_id, season, episode)
        videos.append({"season": season, "episode": episode, "id": vid,
                       "title": _clean(raw.get("title") or raw.get("name"), 80), "out": _released(raw)})
    videos.sort(key=lambda v: (v["season"], v["episode"]))
    return {"title": _clean(meta.get("name"), 100), "id": item_id, "type": type_, "images": _poster_fields(meta),
            "videos": videos, "addon": addon.name, "year": _year_of(meta)}


def seasons_of(videos):
    """-> [{season, episodes, released}] in order.  Season 0 (specials) is listed but never chosen
    on its own."""
    out = {}
    for v in videos:
        row = out.setdefault(v["season"], {"season": v["season"], "episodes": 0, "released": 0})
        row["episodes"] += 1
        row["released"] += 1 if v["out"] else 0
    return [out[k] for k in sorted(out)]


def latest_season(videos):
    """The newest season that has at least one released episode (season 0 only if it is all there is)."""
    rows = [r for r in seasons_of(videos) if r["released"]]
    real = [r for r in rows if r["season"] > 0]
    pick = real or rows
    return pick[-1]["season"] if pick else None


# --------------------------------------------------------------------------- the streams

def offers_streams(found_addons, type_=None, item_id=None):
    """True when at least one installed addon offers streams (for this type and id, when given)."""
    return any(a.supports("stream", type_, item_id) for a in found_addons)


def no_streams_sentence(type_=None, item_id=None):
    """The plain sentence for "nothing here can give a stream" (the real Cinemeta-only answer).  Shared by
    stream_search and bulk_plan so neither returns an empty result that looks like an answer."""
    what = (" for %s ids (type %s)" % (A.id_kind(item_id), type_ or "series")) if item_id else ""
    return ("no installed addon offers streams%s: the addons found can only look titles up, so there is nothing to plan "
            "or queue. Add an addon that offers streams (an account key or an addons.urls list in ~/.jarvis/stremio.json), "
            "then ask again." % what)


def streams_for(net, found_addons, type_, video_id, *, budget_s=STREAM_BUDGET_S):
    """Ask every stream-capable addon for one episode, in parallel.

    -> {"streams": [(stream dict, addon name)], "answered": [names], "failed": [sentences]}"""
    type_, video_id = A.valid_type(type_), A.valid_id(video_id)
    if not type_ or not video_id:
        raise A.AddonError("that isn't an episode id I can use")
    targets = [a for a in found_addons if a.supports("stream", type_, video_id)]
    out = {"streams": [], "answered": [], "failed": [], "asked": len(targets)}
    if not targets:
        out["failed"].append("no installed addon offers streams for %s ids (type %s)" % (A.id_kind(video_id), type_))
        return out
    pool = ThreadPoolExecutor(max_workers=min(FAN_OUT, len(targets)))
    try:
        futures = {}
        for addon in targets:
            futures[pool.submit(net.get_json, A.resource_url(addon.base, "stream", type_, video_id), addon.name)] = addon
        try:
            for future in as_completed(futures, timeout=budget_s):
                addon = futures[future]
                try:
                    data = future.result()
                except A.AddonError as exc:
                    out["failed"].append(str(exc))
                    continue
                items = data.get("streams") if isinstance(data, dict) else None
                out["answered"].append(addon.name)
                for stream in (items or [])[:200]:
                    if isinstance(stream, dict):
                        out["streams"].append((stream, addon.name))
        except Exception:  # noqa: BLE001 - as_completed's TimeoutError: the slow ones are reported, not waited for
            for future, addon in futures.items():
                if not future.done():
                    out["failed"].append("%s was too slow" % addon.name)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


# --------------------------------------------------------------------------- the account library

LIBRARY_COLLECTION = "libraryItem"


def library(net, *, kind="all", search=None, limit=20):
    """-> [row] from the owner's Stremio library (cloud `DatastoreGet`).  Raises AddonError.

    Field names (`state.timeOffset`, `state.duration`, `state.lastWatched`, `state.video_id`,
    `state.timesWatched`, `removed`) follow stremio-core's LibraryItem as far as the plan records
    it (9.1); NOT run against a real account, so every field is read defensively."""
    result = A.cloud_call(net, "datastoreGet", {"collection": LIBRARY_COLLECTION, "ids": [], "all": True})
    rows = []
    needle = _clean(search, 60).lower()
    for raw in result if isinstance(result, list) else []:
        if not isinstance(raw, dict) or raw.get("removed"):
            continue
        state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        offset, duration = _big(state.get("timeOffset")) or 0, _big(state.get("duration")) or 0
        times = _big(state.get("timesWatched")) or 0
        name = _clean(raw.get("name"), 100)
        if needle and needle not in name.lower():
            continue
        progress = (100.0 * offset / duration) if duration else 0.0
        row = {"id": A.valid_id(raw.get("_id")) or "", "name": name, "type": A.valid_type(raw.get("type")) or "",
               "watched": bool(times or state.get("flaggedWatched")), "times_watched": times,
               "progress_pct": round(min(100.0, progress), 1),
               "last_watched": _clean(state.get("lastWatched"), 40), "video_id": A.valid_id(state.get("video_id")) or "",
               "modified": _clean(raw.get("_mtime"), 40)}
        if kind == "continue_watching" and not (0 < progress < 95):
            continue
        if kind == "watched" and not row["watched"]:
            continue
        if kind == "unwatched" and row["watched"]:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["last_watched"] or r["modified"]), reverse=True)
    return rows[:max(1, min(int(limit or 20), 100))]


# --------------------------------------------------------------------------- watched state (a WRITE)

def _utc_stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def mark_watched(net, item_id, watched=True, *, apply=False):
    """Mark a whole library title (a movie) watched or unwatched.  -> a dict for the tool.

    Safe by construction, because this edits the owner's Stremio account and the write format is
    NOT confirmed against a real account:
      * a preview unless `apply` is true;
      * the item is saved to ~/.jarvis/hub/stremio_backups/ BEFORE anything is sent;
      * only an item the library already holds is changed, only these `state` fields
        (`timesWatched`, `flaggedWatched`, `lastWatched`) and `_mtime`, on a copy of what the
        account itself sent;
      * a series is refused: its per-episode watched state is a packed bit field whose layout
        isn't confirmed, so a wrong write could damage the whole title's progress.
    """
    import json
    from .. import paths
    item_id = A.valid_id(item_id)
    if not item_id:
        return {"ok": False, "error": "that isn't a library id (see stremio_library)"}
    result = A.cloud_call(net, "datastoreGet", {"collection": LIBRARY_COLLECTION, "ids": [item_id], "all": False})
    item = next((r for r in (result if isinstance(result, list) else []) if isinstance(r, dict) and r.get("_id") == item_id), None)
    if item is None or item.get("removed"):
        return {"ok": False, "error": "that title isn't in your Stremio library"}
    kind = A.valid_type(item.get("type")) or ""
    if kind != "movie":
        return {"ok": False, "error": "only movies can be marked from here: a series' episode-by-episode watched state is a packed "
                                      "bit field I could not confirm, so mark episodes in Stremio itself"}
    state = dict(item.get("state") if isinstance(item.get("state"), dict) else {})
    now = _utc_stamp()
    before = {"timesWatched": state.get("timesWatched"), "flaggedWatched": state.get("flaggedWatched")}
    if watched:
        state["timesWatched"] = max(1, _big(state.get("timesWatched")) or 0)
        state["flaggedWatched"] = 1
        state["lastWatched"] = now
    else:
        state["timesWatched"] = 0
        state["flaggedWatched"] = 0
    changed = dict(item, state=state, _mtime=now)
    after = {"timesWatched": state.get("timesWatched"), "flaggedWatched": state.get("flaggedWatched")}
    out = {"ok": True, "applied": False, "title": _clean(item.get("name"), 100), "id": item_id,
           "watched": bool(watched), "before": before, "after": after}
    if not apply:
        out["note"] = "preview only: nothing was written. Run again with apply=true (it asks first) to save it."
        return out
    folder = paths.hub_dir() / "stremio_backups"
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / ("%s-%s.json" % (re.sub(r"[^A-Za-z0-9_.-]", "_", item_id)[:40], time.strftime("%Y%m%d-%H%M%S")))
    backup.write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")
    A.cloud_call(net, "datastorePut", {"collection": LIBRARY_COLLECTION, "changes": [changed]})
    out.update(applied=True, backup=str(backup), note="saved; the previous state of this title is kept in the backup file")
    return out
