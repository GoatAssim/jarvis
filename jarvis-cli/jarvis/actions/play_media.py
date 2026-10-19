"""Auto-discovered tool file: playback (Download Hub plan, phase 7).

  play_episode   plays an episode (or movie) in three tiers, most direct first:
                   3. mpv / VLC on a finished file in the downloads library, or on the streaming
                      server's own /<hash>/<index> link (the only tier Jarvis can control afterwards)
                   2. stremio-web's player link with the stream pre-selected
                   1. stremio-web's stream list for the episode (always works; one click short)
                 Tier 1 needs only an IMDb id + season + episode; tier 2 a stream object; tier 3 an
                 infohash (+ file index), a stream object, or a finished hub download.  Nothing here
                 depends on discovery tools: when a stream is missing it opens the stream list.
  play_local     plays a FINISHED hub download (by id or title) in mpv / VLC.

What may be played is decided in hub/stremio/player.py: files under the downloads folders with a
video or audio extension, and the Stremio server's own stream links.  No tool argument is a path
or a URL.
"""


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, exc)}


def _roots(settings, item):
    from ..hub import settings as settings_mod
    return settings_mod.write_roots(settings, item.get("options"))


def _local_file(item):
    """(path, problem) of a finished row's file."""
    if item.get("state") != "done":
        return None, "that download isn't finished (it is %s)" % item.get("state")
    path = item.get("dest_file")
    if not path:
        return None, "that download has no file recorded"
    return path, None


def _launch_file(path, item, settings, args):
    from ..hub.stremio import player
    ok, problem = player.check_target(path, roots=_roots(settings, item))
    if not ok:
        return {"ok": False, "error": problem}
    kind, exe, why = player.find(args.get("player"))
    if not kind:
        return {"ok": False, "error": why}
    res = player.launch(kind, exe, path, fullscreen=bool(args.get("fullscreen")))
    if not res["ok"]:
        return res
    return {"ok": True, "tier": 3, "how": "%s on the downloaded file" % kind, "player": kind,
            "title": (item.get("title") or "")[:100]}


def tool_play_local(args):
    try:
        args = args or {}
        from ..hub.store import Store
        store = Store()
        settings = store.get_settings()
        item = None
        if args.get("id"):
            item = store.get_item(str(args["id"]).strip())
            if item is None:
                return {"ok": False, "error": "no download with that id"}
        elif str(args.get("query") or "").strip():
            rows = [r for r in store.list_items(states_in=("done",), search=str(args["query"]).strip()[:100], limit=20)
                    if r.get("dest_file")]
            if not rows:
                return {"ok": False, "error": "no finished download matches that"}
            if len(rows) > 1:
                return {"ok": False, "needs_clarification": True,
                        "message": "Several finished downloads match; which one?",
                        "matches": [{"id": r["id"], "title": (r.get("title") or "")[:100]} for r in rows[:8]]}
            item = rows[0]
        else:
            return {"needs_clarification": True, "message": "Which download? Give its id (dl_list) or part of its title."}
        path, problem = _local_file(item)
        if problem:
            return {"ok": False, "error": problem}
        return _launch_file(path, item, settings, args)
    except Exception as exc:  # noqa: BLE001
        return _fail("play_local", exc)


def _matching_done_item(store, infohash, file_idx):
    """A finished Stremio row for this torrent (and file), or None."""
    from ..hub.stremio import links
    for row in store.list_items(engine="stremio", states_in=("done",), limit=500):
        ident = links.parse_magnet(row.get("source_url") or "") or {}
        ref = row.get("source_ref") or {}
        digest = ident.get("infohash") or ref.get("infohash")
        if digest != infohash:
            continue
        idx = ref.get("file_idx") if ref.get("file_idx") is not None else ident.get("file_idx")
        if file_idx is None or idx is None or int(idx) == int(file_idx):
            if row.get("dest_file"):
                return row
    return None


def _stream_from_server(parsed, season, episode, args):
    """Tier 3 on the server's link: register the torrent, pick the file, launch the player."""
    from ..hub.stremio import config, links, player, server as sserver
    kind, exe, why = player.find(args.get("player"))
    if not kind:
        return {"ok": False, "error": why}
    srv = sserver.StremioServer()
    if not srv.is_up()[0]:
        from .stremio_stack import _start_server
        started = _start_server(config, sserver)
        if started["state"] not in ("started", "already_up"):
            return {"ok": False, "error": started.get("error") or started.get("note") or "the Stremio server isn't running"}
    try:
        stats = None
        try:
            stats = srv.create(parsed["infohash"], parsed.get("trackers") or [], timeout=(5, 20))
        except sserver.ServerBusy:
            stats = srv.stats(parsed["infohash"])
        files = (stats or {}).get("files") or []
        idx, why_file = links.pick_file(files, parsed.get("file_idx"), season, episode) if files else (None, "no file list yet")
        if idx is None and not files and parsed.get("file_idx") is not None:
            idx = parsed["file_idx"]               # metadata is slow: the server resolves the index itself when it has it
        if idx is None:
            return {"ok": False, "error": "couldn't choose a file: %s (the torrent may have no seeders)" % why_file}
    except sserver.ServerError as exc:
        return {"ok": False, "error": str(exc)}
    url = links.stream_url(srv.base, parsed["infohash"], idx)
    ok, problem = player.check_target(url)
    if not ok:
        return {"ok": False, "error": problem}
    res = player.launch(kind, exe, url, fullscreen=bool(args.get("fullscreen")))
    if not res["ok"]:
        return res
    return {"ok": True, "tier": 3, "how": "%s streaming from the Stremio server (file %d)" % (kind, idx), "player": kind}


def _open_web(link, args, tier, how):
    from ..hub.stremio import config, player, server as sserver
    if args.get("open") is False:                  # just the link: nothing needs to be running
        return {"ok": True, "tier": tier, "how": how, "link": link, "opened": False}
    if not sserver.port_answers(config.web_url()):
        from .stremio_stack import _start_web
        started = _start_web(config, sserver)
        if started["state"] not in ("started", "already_up"):
            return {"ok": False, "tier": tier, "link": link,
                    "error": "stremio-web isn't running (%s); start it with stack_start what=web" % (started.get("note") or started.get("error") or "not set up")}
    opened, problem = player.open_in_browser(link)
    if not opened:
        return {"ok": False, "tier": tier, "link": link, "error": problem or "the browser didn't open"}
    return {"ok": True, "tier": tier, "how": how, "link": link, "opened": True}


def tool_play_episode(args):
    try:
        args = args or {}
        from ..hub.stremio import config, links
        via = str(args.get("via") or "auto").strip().lower()
        if via not in ("auto", "stremio", "player"):
            return {"ok": False, "error": "via must be auto, stremio or player"}
        kind = str(args.get("type") or "series").strip().lower()
        season, episode = args.get("season"), args.get("episode")

        # a finished download named by id needs no IMDb id at all
        from ..hub.store import Store
        store = Store()
        settings = store.get_settings()
        if args.get("item_id"):
            item = store.get_item(str(args["item_id"]).strip())
            if item is None:
                return {"ok": False, "error": "no download with that id"}
            path, problem = _local_file(item)
            if problem:
                return {"ok": False, "error": problem}
            return _launch_file(path, item, settings, args)

        parsed = None
        if args.get("stream") is not None:
            parsed = links.parse_stream(args["stream"])
            if parsed is None:
                return {"ok": False, "error": "that stream has no infoHash; only torrent streams can be played here"}
        elif args.get("infohash"):
            digest = links.normalize_infohash(args["infohash"])
            if not digest:
                return {"ok": False, "error": "that isn't an infohash"}
            parsed = {"infohash": digest, "file_idx": args.get("file_idx") if isinstance(args.get("file_idx"), int) else None,
                      "trackers": [], "name": ""}
        if not args.get("imdb_id") and parsed is None:
            return {"needs_clarification": True,
                    "message": "Which show? I need its IMDb id (like tt0944947) plus the season and episode; "
                               "or the id of a finished download (dl_list)."}
        if kind == "series" and (season is None or episode is None) and args.get("imdb_id"):
            return {"needs_clarification": True, "message": "Which season and episode?"}
        try:
            if args.get("imdb_id"):
                links.deep_link_detail(config.web_url(), args["imdb_id"], kind, season, episode)   # validates
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        tried = []
        if parsed is not None and via in ("auto", "player"):
            done = _matching_done_item(store, parsed["infohash"], parsed.get("file_idx"))
            if done is not None:
                path, problem = _local_file(done)
                res = _launch_file(path, done, settings, args) if not problem else {"ok": False, "error": problem}
                if res.get("ok"):
                    return res
                tried.append("downloaded file: %s" % res.get("error"))
            res = _stream_from_server(parsed, season if kind == "series" else None, episode if kind == "series" else None, args)
            if res.get("ok"):
                return res
            tried.append("player: %s" % res.get("error"))
            if via == "player":
                return {"ok": False, "error": res.get("error"), "tried": tried}
        if not args.get("imdb_id"):
            return {"ok": False, "error": "couldn't play it directly and there is no IMDb id to open its page", "tried": tried}
        if parsed is not None and isinstance(args.get("stream"), dict):
            meta = str(args.get("meta_addon_url") or "https://v3-cinemeta.strem.io/manifest.json")
            link = links.player_link(config.web_url(), args["stream"], args["imdb_id"], season, episode, meta,
                                     args.get("stream_addon_url"), kind)
            res = _open_web(link, args, 2, "stremio-web's player with the stream pre-selected")
            if res.get("ok"):
                res["note"] = "this opens Stremio's own player; if it only shows the episode page, pick the stream there"
                return res
            tried.append("player link: %s" % res.get("error"))
        link = links.deep_link_detail(config.web_url(), args["imdb_id"], kind, season, episode)
        res = _open_web(link, args, 1, "stremio-web's stream list for the episode (pick a stream; one click)")
        if tried:
            res["tried"] = tried
        return res
    except Exception as exc:  # noqa: BLE001
        return _fail("play_episode", exc)


TOOL_SCHEMAS = [
    {"name": "play_episode",
     "description": ("Play an episode or movie. With a finished download's id (item_id) it plays that file in mpv/VLC. With a torrent "
                     "stream (stream object, or infohash + file_idx) it plays through the Stremio server in mpv/VLC, or in stremio-web "
                     "with via=stremio. With only an IMDb id + season + episode it opens that episode's stream list in stremio-web "
                     "(one click short of playing). Needs the IMDb id (tt...); play_local plays a finished download by title."),
     "parameters": {"type": "object", "properties": {
         "imdb_id": {"type": "string", "description": "IMDb id like tt0944947."},
         "season": {"type": "integer"}, "episode": {"type": "integer"},
         "type": {"type": "string", "enum": ["series", "movie"], "description": "Default series."},
         "item_id": {"type": "string", "description": "A finished hub download to play (from dl_list)."},
         "stream": {"type": "object", "description": "A Stremio stream object with infoHash (and fileIdx)."},
         "infohash": {"type": "string", "description": "40-hex infohash when there is no stream object."},
         "file_idx": {"type": "integer", "description": "File index inside the torrent (omit for the right episode / largest video)."},
         "via": {"type": "string", "enum": ["auto", "stremio", "player"], "description": "auto (default): a local player when a file or stream is known, else stremio-web. stremio: stremio-web only. player: mpv/VLC only."},
         "player": {"type": "string", "enum": ["auto", "mpv", "vlc"]},
         "fullscreen": {"type": "boolean"},
         "meta_addon_url": {"type": "string", "description": "Manifest URL of the metadata addon (default Cinemeta); stremio-web player link only."},
         "stream_addon_url": {"type": "string", "description": "Manifest URL of the addon the stream came from; stremio-web player link only."},
         "open": {"type": "boolean", "description": "false = build the stremio-web link without opening it."}}}},
    {"name": "play_local",
     "description": "Play a FINISHED download from the Download Hub in mpv or VLC, by its id (dl_list) or part of its title.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "string", "description": "The download's id."},
         "query": {"type": "string", "description": "Part of its title, when there is no id."},
         "player": {"type": "string", "enum": ["auto", "mpv", "vlc"]},
         "fullscreen": {"type": "boolean"}}}},
]

TOOLS = {"play_episode": tool_play_episode, "play_local": tool_play_local}
TOOL_GROUP = "stremio"

TOOL_KEYWORDS = {
    "play_episode": {"play episode": 10, "play the episode": 10, "play s0": 9, "watch episode": 10, "watch the episode": 10,
                     "play season": 9, "watch s0": 9},
    "play_local": {"play the download": 10, "play the downloaded": 10, "play it in mpv": 10, "play it in vlc": 10,
                   "play the file": 8, "play the finished": 10},
}

# The group's one instruction text lives in stremio_stack.py (one declarer per group).

TOOL_COMMANDS = [
    {"name": "play", "tool": "play_episode", "riskTier": "caution",
     "summary": "Play an episode: /play <imdb id> <season> <episode>",
     "args": [{"name": "imdb", "arg": "imdb_id", "hint": "tt0944947", "required": True},
              {"name": "season", "arg": "season", "type": "integer", "hint": "1", "required": True},
              {"name": "episode", "arg": "episode", "type": "integer", "hint": "1", "required": True}],
     "example": "/play tt0944947 1 1"},
]

TEST_CHECKLIST = {
    "play_episode": {
        "does": "Plays an episode: a finished download or a torrent stream in mpv/VLC, else the episode's stream list in stremio-web.",
        "steps": [
            {"run": {"imdb_id": "tt0944947", "season": 1, "episode": 1, "open": False},
             "expect": "ok: true, tier 1, a link ending #/detail/series/tt0944947/tt0944947:1:1 (stremio-web running); nothing opened."},
            {"run": {"imdb_id": "tt0944947", "season": 1, "episode": 1},
             "expect": "The browser opens stremio-web on that episode's streams (one click short of playing)."},
            {"run": {"item_id": "<a finished download's id from dl_list>"},
             "expect": "ok: true, tier 3: mpv or VLC opens the downloaded file."},
            {"run": {"infohash": "08ada5a7a6183aae1e09d831df6748d566095a10", "imdb_id": "tt1727587", "type": "movie"},
             "expect": "With the server up and mpv/VLC installed: tier 3, the player opens streaming Sintel from the server."},
            {"run": {"imdb_id": "tt0944947"}, "expect": "needs_clarification: which season and episode?"},
            {"ask": "Play S01E01 of Game of Thrones", "expect": "Asks for / uses the IMDb id, calls play_episode, and says what opened."},
        ],
        "needs": ["stremio-web running for tiers 1-2 (stack_start what=web)", "mpv or VLC installed for tier 3",
                  "the Stremio server running for streaming (stack_start)"],
        "care": "Opens a browser tab or a player window.",
    },
    "play_local": {
        "does": "Plays a finished Download Hub file in mpv or VLC.",
        "steps": [
            {"run": {"query": "<part of a finished download's title>"}, "expect": "ok: true, the player opens the file. Several matches: needs_clarification with a short list."},
            {"run": {"id": "<an unfinished download's id>"}, "expect": "ok: false, says it isn't finished."},
        ],
        "needs": ["mpv or VLC installed"],
        "watch": ["Only video / audio files inside the downloads library ever play: a .exe or a file outside it is refused."],
    },
}
