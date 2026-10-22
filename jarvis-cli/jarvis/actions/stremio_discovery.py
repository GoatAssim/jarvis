"""Auto-discovered tool file: finding shows and streams through the owner's Stremio addons
(Download Hub plan, phase 6 and the discovery half of phase 8; brief 07, N13).

  media_find            a title ("jujutsu kaisen") -> a short list of candidates with their ids; asks which one
                        when it is ambiguous.  Works with whatever id scheme the owner's addons use
                        (IMDb `tt...` or Kitsu `kitsu:N`): each result says which, and every later tool follows it.
  media_episodes        a title's episodes, season by season (which season is the latest, which are out).
  stremio_addons        the addons that are installed (account, or the list in ~/.jarvis/stremio.json).
  stream_search         ask every addon for the streams of ONE episode (or film) and rank them: quality
                        (1080p over 720p) and audio (dub over sub, read from the stream's text).
  stremio_library       the owner's Stremio library: continue watching, watched.
  stremio_mark_watched  mark a MOVIE watched / unwatched in the account (preview first; confirm-gated).

Season-sized work (the plan, then the queue) is in bulk_download.py.  Everything here is read-only
except stremio_mark_watched.  All network goes through the SSRF guard (hub/stremio/addons.py).
Text an addon wrote is data: it is cleaned and truncated, never obeyed.
"""


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, exc)}


def _net():
    from ..hub.store import Store
    from ..hub.stremio import addons as A
    return A.Net(allow=Store().get_settings().get("allow_private_hosts") or [])


def _addons(net, need=None):
    """-> ([Addon], notes).  `need`: only addons that offer this resource."""
    from ..hub.stremio import addons as A
    found, notes = A.installed(net)
    if need:
        found = [a for a in found if a.supports(need) or (need == "meta" and a.search_catalogs())]
    return found, notes


# --------------------------------------------------------------------------- media_find

def resolve_title(net, found, query, type_=None):
    """Shared with bulk_plan.  -> ("one", result) | ("ask", [results]) | ("none", errors)."""
    from ..hub.stremio import discovery
    answer = discovery.find(net, found, query, type_)
    results = answer["results"]
    if not results:
        return "none", answer["errors"] or ["no addon found anything for that"]
    if discovery.is_clear_winner(results):
        return "one", results[0]
    return "ask", results


def _public_result(r):
    return {k: r.get(k) for k in ("id", "type", "name", "year", "id_kind", "addon", "description", "images") if r.get(k) not in (None, "", {})}


def tool_media_find(args):
    try:
        args = args or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"needs_clarification": True, "message": "Which show or film?"}
        from ..hub.stremio import addons as A
        type_ = A.valid_type(args.get("type")) if args.get("type") else None
        net = _net()
        found, notes = _addons(net)
        if not found:
            return {"ok": False, "error": "no addon can look titles up. " + " ".join(notes)}
        kind, payload = resolve_title(net, found, query, type_)
        if kind == "none":
            return {"ok": True, "results": [], "message": "Nothing found for %r. If that is an abbreviation, try the full title." % query[:60],
                    "problems": payload[:4], "note": " ".join(notes) or None}
        if kind == "one":
            return {"ok": True, "resolved": True, "match": _public_result(payload), "note": " ".join(notes) or None}
        return {"ok": True, "resolved": False, "needs_clarification": True,
                "message": "Several titles match %r; which one?" % query[:60],
                "matches": [_public_result(r) for r in payload[:8]], "note": " ".join(notes) or None}
    except Exception as exc:  # noqa: BLE001
        return _fail("media_find", exc)


# --------------------------------------------------------------------------- media_episodes

def tool_media_episodes(args):
    try:
        args = args or {}
        from ..hub.stremio import addons as A, discovery
        item_id, type_ = A.valid_id(args.get("id")), A.valid_type(args.get("type") or "series")
        if not item_id:
            return {"needs_clarification": True, "message": "Which title? Give its id from media_find (tt... or kitsu:...)."}
        net = _net()
        found, notes = _addons(net)
        try:
            ep = discovery.episodes(net, found, type_, item_id)
        except A.AddonError as exc:
            return {"ok": False, "error": str(exc)}
        videos = ep["videos"]
        seasons = discovery.seasons_of(videos)
        season, assumed = args.get("season"), False
        if season in (None, "", "latest"):
            season = discovery.latest_season(videos)
            assumed = len([s for s in seasons if s["season"] > 0]) > 1
        try:
            season = int(season) if season is not None else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "season must be a number (or 'latest')"}
        rows = [v for v in videos if v["season"] == season]
        out = {"ok": True, "title": ep["title"], "id": ep["id"], "type": ep["type"], "seasons": seasons[:40],
               "season": season, "season_assumed": assumed, "images": ep["images"],
               "episodes": [{"episode": v["episode"], "title": v["title"], "released": v["out"]} for v in rows[:150]]}
        if not rows:
            out["note"] = "that title has no season %s" % season
        elif assumed:
            out["note"] = "No season was named, so this is the latest one; say so if another was meant."
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("media_episodes", exc)


# --------------------------------------------------------------------------- stremio_addons

def tool_stremio_addons(args):
    try:
        net = _net()
        found, notes = _addons(net)
        from ..hub.stremio import addons as A
        out = {"ok": True, "count": len(found), "addons": [a.public() for a in found],
               "account": "set up" if A.account_ready() else "no account key (addons come from the list in ~/.jarvis/stremio.json)"}
        if notes:
            out["note"] = " ".join(notes)
        if not found:
            out["message"] = "No addons are known. Add account.auth_key or an addons.urls list to ~/.jarvis/stremio.json."
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("stremio_addons", exc)


# --------------------------------------------------------------------------- stream_search

def _video_id(net, found, type_, item_id, season, episode):
    """The id the stream request needs.  A film is its own id.  An episode's id is the one the
    meta addon gave it (so Kitsu's scheme is used when that's what the addon says)."""
    from ..hub.stremio import addons as A, discovery
    if type_ == "movie":
        return item_id, None
    if A.id_kind(item_id) == "imdb" and item_id.count(":") >= 2:
        return item_id, None
    ep = discovery.episodes(net, found, type_, item_id)
    for v in ep["videos"]:
        if v["season"] == season and v["episode"] == episode:
            return v["id"], ep
    raise A.AddonError("that title has no season %s episode %s in the addons' lists" % (season, episode))


def tool_stream_search(args):
    try:
        args = args or {}
        from ..hub.stremio import addons as A, discovery, links, ranking
        item_id, type_ = A.valid_id(args.get("id")), A.valid_type(args.get("type") or "series")
        if not item_id:
            return {"needs_clarification": True, "message": "Which title? Give its id from media_find."}
        season, episode = args.get("season"), args.get("episode")
        if type_ != "movie" and item_id.count(":") < 2 and (season is None or episode is None):
            return {"needs_clarification": True, "message": "Which season and episode?"}
        want, problem = ranking.normalize_want(args.get("quality"), args.get("audio"), args.get("max_gb"), args.get("min_seeders"))
        if problem:
            return {"ok": False, "error": problem}
        try:
            limit = max(1, min(int(args.get("limit") or 10), 25))
        except (TypeError, ValueError):
            limit = 10
        net = _net()
        found, notes = _addons(net)
        if not discovery.offers_streams(found, type_, item_id):
            return {"ok": False, "error": discovery.no_streams_sentence(type_, item_id), "note": " ".join(notes) or None}
        try:
            vid, _ep = _video_id(net, found, type_, item_id, None if season is None else int(season),
                                 None if episode is None else int(episode))
            answer = discovery.streams_for(net, found, type_, vid)
        except A.AddonError as exc:
            return {"ok": False, "error": str(exc)}
        except (TypeError, ValueError):
            return {"ok": False, "error": "season and episode must be numbers"}
        releases = []
        for stream, addon_name in answer["streams"]:
            release = ranking.parse_release(stream)
            if release is not None:
                release["addon"] = addon_name
                releases.append(release)
        kept, rejected = ranking.rank(releases, want, torrents_only=False)
        rows = []
        for c in kept[:limit]:
            row = {"rank": len(rows) + 1, "label": c["label"], "tier": c["tier"], "resolution": c["resolution"], "audio": c["audio"],
                   "size_gb": ranking.fmt_gb(c["size"]), "seeders": c["seeders"], "codec": c["codec"], "source_type": c["source"],
                   "addon": c["addon"], "name": c["name"], "torrent": c["torrent"], "direct_link": c["direct"]}
            if c["torrent"]:
                parsed = links.parse_stream(c["stream"])
                row["magnet"] = links.build_magnet(parsed["infohash"], parsed["file_idx"], None, parsed["trackers"][:4])
            rows.append(row)
        exact = sum(1 for c in kept if c["tier"] == "exact")
        out = {"ok": True, "video_id": vid, "wants": {k: v for k, v in want.items() if v}, "asked": answer["asked"],
               "answered": answer["answered"], "found": len(kept), "exact": exact, "streams": rows,
               "rejected": dict(rejected)}
        if answer["failed"]:
            out["problems"] = answer["failed"][:5]
        if want["audio"] != "any" and any(r["audio"] == "unknown" for r in rows):
            out["note"] = "Some releases don't say their language; they are never counted as a %s." % want["audio"]
        if notes:
            out["note"] = ((out.get("note") or "") + " " + " ".join(notes)).strip()
        if not kept:
            out["message"] = "No usable release for that episode from the addons that answered."
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("stream_search", exc)


# --------------------------------------------------------------------------- the account library

def tool_stremio_library(args):
    try:
        args = args or {}
        from ..hub.stremio import addons as A, discovery
        kind = str(args.get("show") or "all").strip().lower()
        if kind not in ("all", "continue_watching", "watched", "unwatched"):
            return {"ok": False, "error": "show must be all, continue_watching, watched or unwatched"}
        try:
            rows = discovery.library(_net(), kind=kind, search=args.get("search"), limit=args.get("limit") or 20)
        except A.AddonError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "count": len(rows), "items": rows,
                "note": "read from the Stremio cloud; not every field is filled for every title"}
    except Exception as exc:  # noqa: BLE001
        return _fail("stremio_library", exc)


def tool_stremio_mark_watched(args):
    try:
        args = args or {}
        from ..hub.stremio import addons as A, discovery
        if not args.get("id"):
            return {"needs_clarification": True, "message": "Which library title? Give its id (stremio_library)."}
        try:
            return discovery.mark_watched(_net(), args["id"], watched=args.get("watched") is not False,
                                          apply=args.get("apply") is True)
        except A.AddonError as exc:
            return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _fail("stremio_mark_watched", exc)


# --------------------------------------------------------------------------- declarations

TOOL_SCHEMAS = [
    {"name": "media_find",
     "description": ("Find a show or film by name through the owner's Stremio addons. Returns its id and type (an IMDb id like tt0000000, "
                     "or kitsu:N for anime, whichever the addons use) for the other tools. If several titles match it asks which one; "
                     "if nothing is found and the name is an abbreviation, retry with the full title."),
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The title as the owner said it, or an id (tt... / kitsu:N)."},
         "type": {"type": "string", "description": "Narrow to one type the addons use (series, movie, anime ...). Omit to search all."}},
         "required": ["query"]}},
    {"name": "media_episodes",
     "description": ("List a title's episodes by season, using an id from media_find. Without a season it shows the latest released one "
                     "and says so. Use it to see how many episodes a season has."),
     "parameters": {"type": "object", "properties": {
         "id": {"type": "string", "description": "From media_find."},
         "type": {"type": "string", "description": "From media_find (default series)."},
         "season": {"type": "integer", "description": "Omit for the latest season."}},
         "required": ["id"]}},
    {"name": "stremio_addons",
     "description": "List the Stremio addons that are installed (names, what each offers). Addresses are not shown: they can hold keys.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "stream_search",
     "description": ("Ask every installed addon for the streams of ONE episode or film and rank them: the quality asked for first "
                     "(1080p over 720p), then audio (dub over sub; read from the release's text, so a release that does not say is "
                     "'language not stated' and never counts as a dub). For a whole season use bulk_plan instead."),
     "parameters": {"type": "object", "properties": {
         "id": {"type": "string", "description": "From media_find."},
         "type": {"type": "string", "description": "From media_find (default series; movie for films)."},
         "season": {"type": "integer"}, "episode": {"type": "integer"},
         "quality": {"type": "string", "description": "2160, 1080, 720 or 480 (also 4k, 1080p ...). Omit for any."},
         "audio": {"type": "string", "enum": ["dub", "sub", "any"], "description": "Default any."},
         "max_gb": {"type": "number", "description": "Largest acceptable file, in GB."},
         "min_seeders": {"type": "integer"}, "limit": {"type": "integer", "description": "How many to return (default 10)."}},
         "required": ["id"]}},
    {"name": "stremio_library",
     "description": "The owner's Stremio library from their account: continue watching, watched, or everything.",
     "parameters": {"type": "object", "properties": {
         "show": {"type": "string", "enum": ["all", "continue_watching", "watched", "unwatched"]},
         "search": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "stremio_mark_watched",
     "description": ("Mark a MOVIE in the owner's Stremio library watched or unwatched (episodes of a series can't be marked from here). "
                     "It asks the owner before every call. Without apply=true it only previews; with apply=true it changes their account (the old state is backed up first)."),
     "parameters": {"type": "object", "properties": {
         "id": {"type": "string", "description": "A library id from stremio_library."},
         "watched": {"type": "boolean", "description": "Default true; false marks it unwatched."},
         "apply": {"type": "boolean", "description": "true to write it; omit to preview. Either way the owner is asked first."}},
         "required": ["id"]}},
]

TOOLS = {
    "media_find": tool_media_find, "media_episodes": tool_media_episodes, "stremio_addons": tool_stremio_addons,
    "stream_search": tool_stream_search, "stremio_library": tool_stremio_library,
    "stremio_mark_watched": tool_stremio_mark_watched,
}
TOOL_GROUP = "stremio"

TOOL_KEYWORDS = {
    "media_find": {"find the show": 10, "find a show": 10, "look up the show": 10, "find the series": 9, "find the movie": 9,
                   "what show is": 6, "search stremio": 9, "stremio search": 9},
    "media_episodes": {"list the episodes": 10, "how many episodes": 10, "episodes of": 8, "what seasons": 9, "how many seasons": 10},
    "stremio_addons": {"stremio addons": 10, "installed addons": 10, "my addons": 8, "which addons": 9},
    "stream_search": {"find streams": 10, "search streams": 10, "which releases": 9, "dubbed release": 9, "find a release": 9,
                      "streams for": 8},
    "stremio_library": {"stremio library": 10, "continue watching": 10, "what have i watched": 9, "my library": 6},
    "stremio_mark_watched": {"mark as watched": 10, "mark watched": 10, "mark it watched": 10, "mark unwatched": 10},
}

# The group's one instruction text lives in stremio_stack.py (one declarer per group).

# Writing the owner's Stremio account is confirmed out of band, like every other write to something that is theirs.
TOOL_CONFIRM_REQUIRED = {"stremio_mark_watched"}
TOOL_AI_REVIEW = set()

TOOL_RESULT_SPECS = {
    "stream_search": {
        "list_item_drop": {"streams": {
            "medium": ["codec", "source_type", "torrent", "direct_link", "tier"],
            "low": ["codec", "source_type", "torrent", "direct_link", "tier", "magnet", "name", "resolution", "audio"]}},
        "list_item_truncate": {"streams": {"name": {"medium": 80, "low": 40}}},
    },
    "media_find": {"list_item_drop": {"matches": {"medium": ["description", "images"], "low": ["description", "images", "addon"]}}},
}

TEST_CHECKLIST = {
    "media_find": {
        "does": "Finds a show or film by name through your Stremio addons and gives its id (IMDb or Kitsu).",
        "steps": [
            {"run": {"query": "<a show you know, full title>"}, "expect": "ok: true, resolved with an id and type; or needs_clarification with a short list when several match."},
            {"run": {"query": "tt0944947"}, "expect": "resolved: the title for that IMDb id."},
            {"run": {"query": "<an anime title>", "type": "anime"}, "expect": "results carry id_kind kitsu if your addons use Kitsu ids (⚠️ unconfirmed which type name they use: try without type too)."},
            {"ask": "Find the show jjk on Stremio", "expect": "Calls media_find; if the abbreviation finds nothing it retries with the full title or asks."},
        ],
        "needs": ["at least one addon that can search (an account key or an addons.urls list in ~/.jarvis/stremio.json; otherwise Cinemeta is used)"],
        "watch": ["Network only through the SSRF guard: an addon on your own network needs its host in the allow list (dl_settings)."],
    },
    "media_episodes": {
        "does": "Lists a title's seasons and one season's episodes (the latest when none is named).",
        "steps": [
            {"run": {"id": "<id from media_find>", "type": "series"}, "expect": "ok: true, seasons with episode counts, the latest season's episodes, season_assumed true when there are several."},
            {"run": {"id": "<id from media_find>", "type": "series", "season": 1}, "expect": "that season's episodes."},
            {"run": {"id": "<id from media_find>", "season": 99}, "expect": "ok: true, no episodes, a note that the season doesn't exist."},
        ],
    },
    "stremio_addons": {
        "does": "Lists the installed Stremio addons and what each offers (no addresses: they can hold keys).",
        "steps": [{"run": {}, "expect": "ok: true, a count and one row per addon (name, host, types, streams/meta). With no account key, only the addons.urls list (and Cinemeta for titles)."}],
        "watch": ["No result may contain an addon's full address or your account key."],
    },
    "stream_search": {
        "does": "Asks every addon for one episode's streams and ranks them by quality and audio language.",
        "steps": [
            {"run": {"id": "<id from media_find>", "type": "series", "season": 1, "episode": 1, "quality": "1080", "audio": "dub"},
             "expect": "ok: true, streams ranked, exact ones (1080p dub) first, each with a size, seeders and the addon's name. A release with no language marker reads 'language not stated'."},
            {"run": {"id": "<id from media_find>", "type": "series", "season": 1, "episode": 1}, "expect": "no audio preference: ranked by quality and seeders."},
            {"run": {"id": "<id from media_find>", "type": "series", "season": 1}, "expect": "needs_clarification: which episode?"},
        ],
        "care": "Asks every addon you have installed (8 at a time, 8 s each).",
        "watch": ["⚠️ The dub / sub / 1080p markers are guesses until checked against your addons' real answers: run tests/addon_checks.py and compare."],
    },
    "stremio_library": {
        "does": "Reads your Stremio library (continue watching, watched) from your account.",
        "steps": [
            {"run": {"show": "continue_watching"}, "expect": "ok: true, titles with a progress percentage. ⚠️ the cloud answer's shape was never checked: if the list is empty or odd, say what it looked like."},
            {"run": {}, "expect": "your library, newest first."},
        ],
        "needs": ["account.auth_key in ~/.jarvis/stremio.json"],
    },
    "stremio_mark_watched": {
        "does": "Marks a movie in your Stremio library watched / unwatched (preview first).",
        "steps": [
            {"run": {"id": "<a movie's id from stremio_library>"}, "expect": "it asks first (every call does); then a preview: before / after, 'nothing was written'."},
            {"run": {"id": "<a movie's id>", "apply": True}, "expect": "asks first; after confirming: applied, a backup file path; check the movie in Stremio."},
            {"run": {"id": "<a series' id>"}, "expect": "ok: false, refused (episodes can't be marked from here)."},
        ],
        "needs": ["account.auth_key in ~/.jarvis/stremio.json"],
        "care": "Changes your Stremio account. ⚠️ the write was never run against a real account; the old state is saved in ~/.jarvis/hub/stremio_backups/ first.",
    },
}
