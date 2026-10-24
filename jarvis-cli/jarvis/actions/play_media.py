"""Auto-discovered tool file: playback (Download Hub plan, phase 7).

  play_episode   plays an episode (or movie) in three tiers, most direct first:
                   3. mpv / VLC on a finished file in the downloads library, or on the streaming
                      server's own /<hash>/<index> link (the only tier Jarvis can control afterwards)
                   2. stremio-web's player link with the stream pre-selected
                   1. stremio-web's stream list for the episode (always works; one click short)
                 Tier 1 needs only an IMDb id (or a title, looked up first) + season + episode; tier 2 a stream object; tier 3 an
                 infohash (+ file index), a stream object, or a finished hub download.  Nothing here
                 depends on discovery tools: when a stream is missing it opens the stream list.  A `title` is
                 resolved (stremio_discovery.resolve_title) only when there is no imdb_id and no item_id.
  play_local     plays a FINISHED hub download (by id or title) in mpv / VLC.
  play_control   pause / resume / toggle / seek / stop / volume / status of the mpv Jarvis itself started (hub/stremio/mpv.py,
                 over mpv's JSON IPC endpoint).  Owner only.  Never any other process, never a raw command.

What may be played is decided in hub/stremio/player.py: files under the downloads folders with a
video or audio extension, and the Stremio server's own stream links.  No tool argument is a path
or a URL.
"""


def _is_chat_guest():
    """True when this turn answers a chat message from someone who is NOT the owner.  No chat context (the PC, a
    scheduled job) is the owner's own.  An unreadable value counts as a guest: a permission check fails closed."""
    import json
    import os
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001  (bad JSON, or nested too deep to read -- a RecursionError: either way, not the owner)
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def tool_play_control(args):
    """Control the mpv Jarvis started.  Owner only: tools.py's OWNER_ONLY_TOOLS keeps it from being OFFERED to a chat
    guest (brief 31), and a guest who calls it anyway is refused here, before anything is sent."""
    try:
        if _is_chat_guest():
            return {"ok": False, "error": "controlling the player is the owner's: someone in a chat can't pause or stop it"}
        args = args or {}
        if not str(args.get("action") or "").strip():
            return {"needs_clarification": True, "message": "What should the player do: pause, resume, toggle, seek, stop, volume or status?"}
        from ..hub.stremio import mpv
        return mpv.run(args.get("action"), seconds=args.get("seconds"), volume=args.get("volume"))
    except Exception as exc:  # noqa: BLE001
        return _fail("play_control", exc)


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


def _imdb_from_title(title, type_arg):
    """A title the owner said -> (imdb_id, type, matched text, None) for ONE clear match, else
    (None, None, None, answer) where `answer` is what the tool returns: a question (several matches), or a
    plain sentence (nothing found, a Kitsu-only title).  Never picks among several.  The lookup is
    resolve_title from stremio_discovery, so every request goes through its guarded client (netguard)."""
    from ..hub.stremio import addons as A
    from . import stremio_discovery as disc
    try:
        net = disc._net()
        found, notes = disc._addons(net)
    except Exception as exc:  # noqa: BLE001  (the addon list could not even be read: say so in a sentence, never the exception's own text)
        return None, None, None, {"ok": False, "error": "the title lookup couldn't start (%s), so %r can't be found; give its IMDb id (tt...)."
                                                          % (type(exc).__name__, title[:60])}
    if not found:
        return None, None, None, {"ok": False, "error": ("no addon can look titles up, so %r can't be found; give its IMDb id (tt...). %s"
                                                           % (title[:60], " ".join(str(n) for n in notes))).strip()}
    kind, payload = disc.resolve_title(net, found, title, A.valid_type(type_arg) if type_arg else None)
    if kind == "none":
        return None, None, None, {"ok": False, "error": "Nothing was found for the title %r. If that is an abbreviation, try the full title, "
                                                          "or give its IMDb id (tt...)." % title[:60], "problems": payload[:3]}
    if kind == "ask":
        return None, None, None, {"ok": False, "needs_clarification": True,
                                  "message": "Several titles match %r; which one? Ask the owner, then call play_episode again with that imdb_id." % title[:60],
                                  "matches": [{k: r.get(k) for k in ("id", "type", "name", "year")} for r in payload[:8]]}
    found_text = "%s%s, %s %s" % (payload["name"], (" (%s)" % payload["year"]) if payload.get("year") else "", payload["type"], payload["id"])
    if payload.get("id_kind") != "imdb":
        return None, None, None, {"ok": False, "error": "Found %s, but stremio-web links need an IMDb id and that title only has %s. "
                                                          "Give its IMDb id (tt...) if you know it." % (found_text, payload["id"])}
    if payload["type"] not in ("series", "movie"):
        return None, None, None, {"ok": False, "error": "Found %s, but stremio-web links only exist for series and movies." % found_text}
    return payload["id"], payload["type"], found_text, None


def tool_play_episode(args):
    try:
        ctx = {}
        res = _play_episode(dict(args or {}), ctx)
        if ctx.get("matched") and isinstance(res, dict) and res.get("ok"):
            res["matched"] = ctx["matched"]            # what the title was taken to be, so the owner can see a wrong guess
        return res
    except Exception as exc:  # noqa: BLE001
        return _fail("play_episode", exc)


def _play_episode(args, ctx):
    from ..hub.stremio import config, links
    via = str(args.get("via") or "auto").strip().lower()
    if via not in ("auto", "stremio", "player"):
        return {"ok": False, "error": "via must be auto, stremio or player"}
    kind = str(args.get("type") or "series").strip().lower()
    season, episode = args.get("season"), args.get("episode")
    title = str(args.get("title") or "").strip()[:120]

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
    if not args.get("imdb_id") and parsed is None and not title:
        return {"needs_clarification": True,
                "message": "Which show? Give its title (or its IMDb id, like tt0944947) plus the season and episode; "
                           "or the id of a finished download (dl_list)."}
    # A title alone (no type, no season, no episode, no imdb_id, no known stream) may be a film, which needs no season
    # or episode: so for that one case the title is looked up FIRST and the question comes after, only for a series.
    # Any other case asks at once, before any lookup (type=series with a missing season, a half-given season/episode).
    lookup_first = bool(title and not args.get("imdb_id") and parsed is None and not str(args.get("type") or "").strip()
                        and season is None and episode is None)
    if kind == "series" and (season is None or episode is None) and (args.get("imdb_id") or title) and not lookup_first:
        return {"needs_clarification": True, "message": "Which season and episode?"}      # asked before any lookup

    def look_up_title():
        """Only when there is no imdb_id.  Sets args['imdb_id'] and kind; returns an answer to stop with, or None."""
        imdb, found_kind, found_text, answer = _imdb_from_title(title, args.get("type"))
        if answer is not None:
            return answer
        args["imdb_id"], ctx["matched"], ctx["kind"] = imdb, found_text, found_kind
        return None

    if not args.get("imdb_id") and title and parsed is None:
        answer = look_up_title()
        if answer is not None:
            return answer
        kind = ctx.get("kind") or kind
        if lookup_first and kind == "series":
            # a clear series match: now the season and episode are needed.  Hand back the id so the next call needs no lookup.
            return {"needs_clarification": True, "imdb_id": args["imdb_id"], "type": "series", "matched": ctx.get("matched"),
                    "message": "That is %s. Which season and episode? Ask the owner, then call play_episode again with this imdb_id, "
                               "the season and the episode." % ctx.get("matched")}
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
    if not args.get("imdb_id") and title:
        # a stream is known but the player route did not work: only the two web tiers need an IMDb id
        answer = look_up_title()
        if answer is not None:
            answer = dict(answer)
            answer["tried"] = tried
            return answer
        kind = ctx.get("kind") or kind
        try:
            links.deep_link_detail(config.web_url(), args["imdb_id"], kind, season, episode)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "tried": tried}
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


TOOL_SCHEMAS = [
    {"name": "play_episode",
     "description": ("Play an episode or movie. With a finished download's id (item_id) it plays that file in mpv/VLC. With a torrent "
                     "stream (stream object, or infohash + file_idx) it plays through the Stremio server in mpv/VLC, or in stremio-web "
                     "with via=stremio. With only a title or an IMDb id + season + episode it opens that episode's stream list in stremio-web "
                     "(one click short of playing). A title is looked up through the owner's addons: one clear match is used, several "
                     "make it ask which one, nothing is guessed. play_local plays a finished download by title."),
     "parameters": {"type": "object", "properties": {
         "imdb_id": {"type": "string", "description": "IMDb id like tt0944947. Wins over title."},
         "title": {"type": "string", "description": "The show or film as the owner said it, when there is no imdb_id (or media_find id). With no type, season or episode the title is looked up first: a film plays, a series asks for season and episode, several matches ask which."},
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
    {"name": "play_control",
     "description": ("Control the mpv player that Jarvis itself opened (play_episode / play_local): pause, resume, toggle, seek (relative "
                     "seconds), stop (closes it), volume (0-100) or status (title, position, duration, paused). Only that player; with "
                     "none running it says so. Owner only."),
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["pause", "resume", "toggle", "seek", "stop", "volume", "status"]},
         "seconds": {"type": "number", "description": "For seek: seconds to skip, negative to go back (e.g. -30)."},
         "volume": {"type": "integer", "description": "For volume: 0 to 100."}},
         "required": ["action"]}},
]

TOOLS = {"play_episode": tool_play_episode, "play_local": tool_play_local, "play_control": tool_play_control}
TOOL_GROUP = "stremio"

TOOL_KEYWORDS = {
    "play_episode": {"play episode": 10, "play the episode": 10, "play s0": 9, "watch episode": 10, "watch the episode": 10,
                     "play season": 9, "watch s0": 9},
    "play_control": {"pause the video": 10, "pause the player": 10, "resume the video": 10, "resume playback": 10, "skip ahead": 8,
                     "skip back": 8, "rewind the video": 9, "turn the volume": 8, "stop the player": 10, "what is playing": 9,
                     "what's playing": 9, "pause the episode": 9, "pause the movie": 9},
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
             "expect": "ok: true, tier 1, a link ending #/detail/series/tt0944947/tt0944947:1:1, opened: false; nothing is opened and nothing needs to be running."},
            {"run": {"imdb_id": "tt0944947", "season": 1, "episode": 1},
             "expect": "The browser opens stremio-web on that episode's streams (one click short of playing)."},
            {"run": {"item_id": "<a finished download's id from dl_list>"},
             "expect": "ok: true, tier 3: mpv or VLC opens the downloaded file."},
            {"run": {"infohash": "08ada5a7a6183aae1e09d831df6748d566095a10", "imdb_id": "tt1727587", "type": "movie"},
             "expect": "With the server up and mpv/VLC installed: tier 3, the player opens streaming Sintel from the server."},
            {"run": {"imdb_id": "tt0944947"}, "expect": "needs_clarification: which season and episode?"},
            {"run": {"title": "<a show you know, full title>", "season": 1, "episode": 1, "open": False},
             "expect": "Same result as with its IMDb id: ok: true, tier 1, a link ending #/detail/series/<id>/<id>:1:1, and a 'matched' line naming the title it was taken to be."},
            {"run": {"title": "<a name that fits several shows>", "season": 1, "episode": 1}, "expect": "needs_clarification with a short list (id, type, name, year); nothing opens."},
            {"run": {"title": "<a film you know, full title>"},
             "expect": "A clear film match opens the film's page in stremio-web with no question, and the answer says what it was taken to be (a 'matched' line). (⚠️ Seen against a fake addon only: whether your real addons have a searchable movie catalog is unknown.)"},
            {"run": {"title": "<a series you know, full title>"},
             "expect": "needs_clarification 'Which season and episode?' naming the show; the answer carries imdb_id, type and matched so the next call needs no lookup; nothing opens. (⚠️ Seen against a fake addon only.)"},
            {"run": {"title": "zzzz no such show", "season": 1, "episode": 1}, "expect": "ok: false, one sentence: 'Nothing was found for the title ... try the full title, or give its IMDb id (tt...)'; nothing opens."},
            {"run": {"item_id": "no-such-id"}, "expect": "ok: false, 'no download with that id'; nothing opens."},
            {"run": {}, "expect": "No error, a question: needs_clarification 'Which show? Give its title (or its IMDb id, like tt0944947) plus the season and episode; or the id of a finished download (dl_list).'"},
            {"ask": "Play S01E01 of Game of Thrones", "expect": "Calls play_episode with the title (no IMDb id needed) and says what opened."},
        ],
        "needs": ["stremio-web running for tiers 1-2 (stack_start what=web)", "mpv or VLC installed for tier 3",
                  "the Stremio server running for streaming (stack_start)",
                  "for a title: an addon that can look titles up (Cinemeta is used when none is set)"],
        "care": "Opens a browser tab or a player window. A title is looked up on the network (only through the addon guard); an IMDb id needs no lookup.",
        "watch": ["A title with NO addon able to look it up (network cut, Cinemeta refused, no addons.urls) answers ok: false, 'no addon can look titles up, so ... can't be found; give its IMDb id (tt...)', and nothing opens or starts. (Brief 31 fixed an earlier crash here, \"'tuple' object has no attribute 'strip'\"; checked against a fake addon setup only, never with a real network cut. The zzzz step above needs an addon that answers; its sentence is read from the code, not run.)"],
    },
    "play_control": {
        "does": "Pauses, resumes, seeks, stops, sets the volume of, or reports on the mpv player that Jarvis itself started.",
        "steps": [
            {"run": {"action": "status"}, "expect": "With nothing started by Jarvis playing: ok: false, one sentence saying nothing is playing that Jarvis started. While mpv plays: ok: true with title, position, duration, paused."},
            {"run": {"action": "pause"}, "expect": "ok: true, done: paused; the video freezes. action resume continues it."},
            {"run": {"action": "seek", "seconds": -30}, "expect": "ok: true, the video jumps back 30 seconds."},
            {"run": {"action": "volume", "volume": 40}, "expect": "ok: true, the volume drops to 40."},
            {"run": {"action": "volume", "volume": 300}, "expect": "ok: false, says volume must be from 0 to 100; nothing is sent to mpv."},
            {"run": {"action": "stop"}, "expect": "ok: true, the mpv window closes."},
            {"run": {"action": "toggle"}, "expect": "ok: true, 'toggled pause': a playing video freezes, a paused one continues."},
            {"run": {"action": "seek"}, "expect": "ok: false, 'seek needs seconds (positive to skip ahead, negative to go back)'; nothing is sent to mpv."},
            {"run": {"action": "bogus"}, "expect": "ok: false, 'action must be one of: pause, resume, toggle, seek, stop, volume, status.'"},
            {"ask": "Pause the video", "expect": "Calls play_control with action pause and says it paused."},
        ],
        "needs": ["mpv (not VLC) started by play_episode or play_local; Linux and macOS use a socket, Windows a named pipe (not tested)"],
        "watch": ["Only the mpv Jarvis started is ever controlled: a player you opened yourself is never touched.",
                  "Owner only: a remote chat guest is not offered play_control (tools.OWNER_ONLY_TOOLS), and if one calls it anyway the handler refuses: controlling the player is the owner's.",
                  "⚠️ 'stop' sends mpv's quit command, so the window closes (not just the playback). Real mpv's answers have never been seen by any report."],
    },
    "play_local": {
        "does": "Plays a finished Download Hub file in mpv or VLC.",
        "steps": [
            {"run": {"query": "<part of a finished download's title>"}, "expect": "ok: true, the player opens the file. Several matches: needs_clarification with a short list."},
            {"run": {"id": "<an unfinished download's id>"}, "expect": "ok: false, says it isn't finished."},
            {"run": {"id": "no-such-id"}, "expect": "ok: false, 'no download with that id'; no player starts."},
            {"run": {"query": "<text that matches no finished download>"}, "expect": "ok: false, 'no finished download matches that'; no player starts."},
            {"run": {}, "expect": "No error, a question: needs_clarification 'Which download? Give its id (dl_list) or part of its title.'"},
        ],
        "needs": ["mpv or VLC installed"],
        "watch": ["Only video / audio files inside the downloads library ever play: a .exe or a file outside it is refused."],
    },
}
