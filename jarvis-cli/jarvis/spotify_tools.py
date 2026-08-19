"""Jarvis tools for the user's Spotify account (desktop app on this PC)."""

from . import spotify_api as sp


def _err(data):
    return isinstance(data, dict) and bool(data.get("error") or data.get("needs_setup"))


def tool_spotify_open(args=None):
    """Launch the local Spotify app (Windows account already logged in). No API needed."""
    ok = sp.start_desktop_app()
    if ok:
        return {
            "ok": True,
            "message": "Spotify desktop app was launched for the account logged in on this PC.",
        }
    return {
        "ok": False,
        "error": "Couldn't start the Spotify app. Is it installed?",
        "hint": "Install Spotify, or use spotify_play after jarvis spotify-login.",
    }


def tool_spotify_now(args=None):
    me = sp.api("GET", "/me")
    if _err(me):
        return me
    player = sp.api("GET", "/me/player")
    devices = sp.api("GET", "/me/player/devices")
    device_list = []
    if isinstance(devices, dict) and not _err(devices):
        for d in devices.get("devices") or []:
            device_list.append({
                "id": d.get("id"),
                "name": d.get("name"),
                "type": d.get("type"),
                "active": d.get("is_active"),
                "volume": d.get("volume_percent"),
            })
    now = None
    paused = None
    if isinstance(player, dict) and player.get("ok") and player.get("status") == 204:
        paused = True
    elif isinstance(player, dict) and not _err(player) and not player.get("ok"):
        item = player.get("item")
        now = sp._compact_track(item) if item else None
        paused = not player.get("is_playing", False)
        ctx = player.get("context") or {}
        if now is not None:
            now["context_uri"] = ctx.get("uri") or ""
            now["shuffle"] = player.get("shuffle_state")
            now["repeat"] = player.get("repeat_state")
            now["progress_ms"] = player.get("progress_ms")
    return {
        "account": me.get("display_name") or me.get("id"),
        "product": me.get("product"),
        "now": now,
        "paused": paused,
        "devices": device_list,
        "note": (
            "Free accounts: Jarvis opens tracks in the desktop app (click play if needed). "
            "Premium is only required for API remote-control."
            if (me.get("product") or "").lower() != "premium"
            else "Playback can use the Web API on this Premium account, or the desktop app."
        ),
    }


def tool_spotify_search(args):
    args = args or {}
    query = (args.get("query") or args.get("q") or "").strip()
    if len(query) < 2:
        return {"needs_clarification": True, "message": "What should I search for on Spotify?"}
    kind = (args.get("type") or "track,playlist,album,artist").replace(" ", "")
    data = sp.api("GET", "/search", params={"q": query, "type": kind, "limit": 6})
    if _err(data):
        opened = sp.open_spotify_uri(sp.search_uri(query))
        data = dict(data)
        data["opened_desktop_search"] = opened
        data["note"] = (
            "API search unavailable — opened Spotify's own search in the desktop app "
            "(works on Free). You can also jarvis spotify-login for catalog results here."
        )
        return data
    out = {"query": query, "tracks": [], "playlists": [], "albums": [], "artists": []}
    tracks = ((data.get("tracks") or {}).get("items") or [])
    for t in tracks:
        c = sp._compact_track(t)
        if c:
            out["tracks"].append(c)
    for p in ((data.get("playlists") or {}).get("items") or []):
        if not p:
            continue
        c = sp._compact_playlist(p)
        if c:
            out["playlists"].append(c)
    for a in ((data.get("albums") or {}).get("items") or []):
        c = sp._compact_album(a)
        if c:
            out["albums"].append(c)
    for a in ((data.get("artists") or {}).get("items") or []):
        c = sp._compact_artist(a)
        if c:
            out["artists"].append(c)
    out["note"] = (
        "Free: spotify_play opens this in the desktop app (may need one click to play). "
        "Pass the uri to spotify_play."
    )
    return out


def tool_spotify_play(args):
    args = args or {}
    uri = (args.get("uri") or "").strip()
    query = (args.get("query") or "").strip()
    kind = (args.get("type") or "").strip().lower()

    if not uri and query:
        search_type = kind if kind in ("track", "playlist", "album", "artist") else "track,playlist"
        found = tool_spotify_search({"query": query, "type": search_type})
        if _err(found) or found.get("needs_clarification"):
            return found
        pick = None
        if kind == "playlist" or (not kind and found.get("playlists") and "playlist" in query.lower()):
            pick = (found.get("playlists") or [None])[0]
        elif kind == "album":
            pick = (found.get("albums") or [None])[0]
        elif kind == "artist":
            pick = (found.get("artists") or [None])[0]
        else:
            pick = (found.get("tracks") or [None])[0] or (found.get("playlists") or [None])[0]
        if not pick or not pick.get("uri"):
            opened = sp.open_spotify_uri(sp.search_uri(query))
            opened["query"] = query
            opened["note"] = "No API match — opened Spotify search in the desktop app (Free-friendly)."
            return opened
        uri = pick["uri"]

    if not uri:
        return {"needs_clarification": True, "message": "Need a Spotify uri or a search query to play."}

    if uri.startswith("https://"):
        uri = sp._https_to_spotify_uri(uri)

    # Free (and unknown) accounts cannot use PUT /me/player/play — open the desktop app instead.
    if not sp.is_premium():
        sp.start_desktop_app()
        result = sp.open_spotify_uri(uri)
        result["playing"] = uri
        result["account"] = "free"
        result["note"] = (
            "Opened in the Spotify desktop app on this PC (Free). "
            "If it only shows the page, press play once — Spotify blocks remote start on Free."
        )
        return result

    device, derr = sp.ensure_device()
    body = {"uris": [uri]} if ":track:" in uri or ":episode:" in uri else {"context_uri": uri}

    if device:
        result = sp.api(
            "PUT",
            "/me/player/play",
            params={"device_id": device.get("id")},
            json_body=body,
        )
        if not _err(result):
            return {
                "ok": True,
                "playing": uri,
                "device": device.get("name"),
                "via": "web api",
            }
        fallback = sp.open_spotify_uri(uri)
        fallback["web_api"] = result
        return fallback

    fallback = sp.open_spotify_uri(uri)
    if derr:
        fallback["device_error"] = derr.get("error")
    return fallback


def tool_spotify_control(args):
    args = args or {}
    action = (args.get("action") or "").strip().lower()
    if not action:
        return {
            "needs_clarification": True,
            "message": "Which action? pause, resume, next, previous, shuffle_on, shuffle_off, "
            "repeat_track, repeat_context, repeat_off, volume.",
        }

    if action in ("pause", "stop", "resume", "play", "unpause", "next", "skip", "previous", "prev"):
        if not sp.is_premium():
            sp.start_desktop_app()
            ok, err = sp.send_media_key(action)
            if ok:
                return {
                    "ok": True,
                    "action": action,
                    "via": "windows media keys",
                    "note": "Free account — sent a media key. Spotify must be the current player.",
                }
            return {"ok": False, "error": err, "action": action}
    elif action in ("shuffle_on", "shuffle_off", "repeat_track", "repeat_context", "repeat_off", "volume"):
        if not sp.is_premium():
            return {
                "ok": False,
                "error": "Shuffle/repeat/volume remote control needs Spotify Premium. Use the Spotify app on Free.",
            }

    device, derr = sp.ensure_device()
    params = {"device_id": device["id"]} if device else None

    if action in ("pause", "stop"):
        result = sp.api("PUT", "/me/player/pause", params=params)
    elif action in ("resume", "play", "unpause"):
        result = sp.api("PUT", "/me/player/play", params=params)
    elif action in ("next", "skip"):
        result = sp.api("POST", "/me/player/next", params=params)
    elif action in ("previous", "prev"):
        result = sp.api("POST", "/me/player/previous", params=params)
    elif action == "shuffle_on":
        result = sp.api("PUT", "/me/player/shuffle", params={**(params or {}), "state": "true"})
    elif action == "shuffle_off":
        result = sp.api("PUT", "/me/player/shuffle", params={**(params or {}), "state": "false"})
    elif action == "repeat_track":
        result = sp.api("PUT", "/me/player/repeat", params={**(params or {}), "state": "track"})
    elif action == "repeat_context":
        result = sp.api("PUT", "/me/player/repeat", params={**(params or {}), "state": "context"})
    elif action == "repeat_off":
        result = sp.api("PUT", "/me/player/repeat", params={**(params or {}), "state": "off"})
    elif action == "volume":
        vol = args.get("volume")
        try:
            vol = int(vol)
        except (TypeError, ValueError):
            return {"needs_clarification": True, "message": "volume needs 0–100."}
        vol = max(0, min(100, vol))
        q = {**(params or {}), "volume_percent": vol}
        result = sp.api("PUT", "/me/player/volume", params=q)
    else:
        return {"error": f"Unknown action '{action}'."}

    if _err(result):
        if derr:
            result["device_hint"] = derr.get("error")
        return result
    return {"ok": True, "action": action, "device": (device or {}).get("name")}


def tool_spotify_queue(args):
    args = args or {}
    uri = (args.get("uri") or "").strip()
    query = (args.get("query") or "").strip()
    if not uri and query:
        found = tool_spotify_search({"query": query, "type": "track"})
        if _err(found) or found.get("needs_clarification"):
            return found
        pick = (found.get("tracks") or [None])[0]
        if not pick:
            return {"error": f"No track for '{query}'."}
        uri = pick["uri"]
    if not uri:
        return {"needs_clarification": True, "message": "Need uri or query to queue."}
    if not sp.is_premium():
        return {
            "ok": False,
            "error": "Queue via API needs Premium. On Free, open the track with spotify_play instead.",
            "uri": uri,
        }
    device, _ = sp.ensure_device()
    params = {"uri": uri}
    if device:
        params["device_id"] = device["id"]
    result = sp.api("POST", "/me/player/queue", params=params)
    if _err(result):
        return result
    return {"ok": True, "queued": uri}


def tool_spotify_playlists(args=None):
    args = args or {}
    limit = args.get("limit") or 20
    try:
        limit = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        limit = 20
    data = sp.api("GET", "/me/playlists", params={"limit": limit})
    if _err(data):
        return data
    items = []
    for p in data.get("items") or []:
        c = sp._compact_playlist(p)
        if c:
            items.append(c)
    return {"showing": len(items), "playlists": items}


def tool_spotify_suggest(args=None):
    """Taste snapshot: top tracks/artists, recently played, Made For You playlists."""
    args = args or {}
    rng = (args.get("time_range") or "medium_term").strip()
    if rng not in ("short_term", "medium_term", "long_term"):
        rng = "medium_term"

    tops_t = sp.api("GET", "/me/top/tracks", params={"time_range": rng, "limit": 8})
    tops_a = sp.api("GET", "/me/top/artists", params={"time_range": rng, "limit": 8})
    recent = sp.api("GET", "/me/player/recently-played", params={"limit": 8})
    playlists = sp.api("GET", "/me/playlists", params={"limit": 50})

    if _err(tops_t) and _err(tops_a):
        return tops_t if _err(tops_t) else tops_a

    def tracks_from(payload, key="items"):
        if _err(payload) or not isinstance(payload, dict):
            return []
        out = []
        for it in payload.get(key) or []:
            c = sp._compact_track(it)
            if c:
                out.append(c)
        return out

    made = []
    keywords = (
        "discover weekly", "release radar", "daily mix", "on repeat",
        "repeat rewind", "your time capsule", "liked from radio", "daylist",
        "smart shuffle", "mix",
    )
    if isinstance(playlists, dict) and not _err(playlists):
        for p in playlists.get("items") or []:
            name = (p.get("name") or "").lower()
            if any(k in name for k in keywords):
                c = sp._compact_playlist(p)
                if c:
                    made.append(c)

    top_tracks = tracks_from(tops_t)
    top_artists = []
    if isinstance(tops_a, dict) and not _err(tops_a):
        for a in tops_a.get("items") or []:
            c = sp._compact_artist(a)
            if c:
                top_artists.append(c)

    seeds = []
    if top_artists:
        seeds.append(f"more like {top_artists[0]['name']}")
    if len(top_artists) > 1:
        seeds.append(f"{top_artists[0]['name']} x {top_artists[1]['name']} mix")
    if made:
        seeds.append(f"play your '{made[0]['name']}' playlist")
    if top_tracks:
        seeds.append(f"queue {top_tracks[0]['name']} by {top_tracks[0]['artists']}")

    return {
        "time_range": rng,
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "recently_played": tracks_from(recent),
        "made_for_you": made[:8],
        "try_next": seeds[:6],
        "note": "These are THIS account's tastes. Play a made-for-you playlist with spotify_play uri=… "
                "or search similar to a top artist.",
    }


def tool_spotify_like(args=None):
    args = args or {}
    track_id = (args.get("id") or "").strip()
    uri = (args.get("uri") or "").strip()
    if not track_id and uri.startswith("spotify:track:"):
        track_id = uri.split(":")[-1]
    if not track_id:
        now = sp.api("GET", "/me/player/currently-playing")
        if _err(now):
            return now
        item = (now or {}).get("item") or {}
        track_id = item.get("id") or ""
        if not track_id:
            return {"needs_clarification": True, "message": "Nothing is playing — pass a track uri to like."}
    result = sp.api("PUT", "/me/tracks", params={"ids": track_id})
    if _err(result):
        return result
    return {"ok": True, "liked": track_id}


SPOTIFY_TOOL_SCHEMAS = [
    {
        "name": "spotify_open",
        "description": (
            "Launch the Spotify desktop app on this PC (the account already logged into the app). "
            "Use for 'open/start/spin up Spotify'. Does not play a song — follow with spotify_search + spotify_play. "
            "Do NOT use run_command to open Spotify."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "spotify_now",
        "description": "Current Spotify account, now playing, pause state, and devices on this PC.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "spotify_search",
        "description": "Search THIS user's Spotify catalog. Returns compact name/artists/uri. Then spotify_play with uri.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Song, artist, playlist, or album."},
                "type": {"type": "string", "description": "Comma types: track,playlist,album,artist. Default all."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "spotify_play",
        "description": (
            "Play a track/playlist/album/artist on the Spotify app for the logged-in PC account. "
            "Prefer uri from search/suggest. Or pass query to search then play. "
            "Never claim it started unless this tool returns ok."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "spotify:track:… / playlist:… / album:… / artist:…"},
                "query": {"type": "string", "description": "If no uri, search and play the best match."},
                "type": {"type": "string", "description": "Hint when using query: track|playlist|album|artist"},
            },
            "required": [],
        },
    },
    {
        "name": "spotify_control",
        "description": "pause, resume, next, previous, shuffle_on/off, repeat_track/context/off, volume (0-100).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "volume": {"type": "integer", "description": "For action=volume, 0–100."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "spotify_queue",
        "description": "Add a track to the queue (uri or search query).",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "spotify_playlists",
        "description": "List playlists in the logged-in Spotify account (name, uri, length).",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max 40, default 20."},
            },
            "required": [],
        },
    },
    {
        "name": "spotify_suggest",
        "description": (
            "Taste-based suggestions from THIS account: top tracks/artists, recently played, "
            "Made For You (Discover Weekly, Daily Mix, On Repeat, …) and try_next prompts. "
            "Use for 'what should I listen to' then spotify_play a uri."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string",
                    "description": "short_term (weeks), medium_term (months, default), long_term (years).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "spotify_like",
        "description": "Save the current track (or a track uri) to Liked Songs.",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "id": {"type": "string"},
            },
            "required": [],
        },
    },
]

SPOTIFY_TOOLS = {
    "spotify_open": tool_spotify_open,
    "spotify_now": tool_spotify_now,
    "spotify_search": tool_spotify_search,
    "spotify_play": tool_spotify_play,
    "spotify_control": tool_spotify_control,
    "spotify_queue": tool_spotify_queue,
    "spotify_playlists": tool_spotify_playlists,
    "spotify_suggest": tool_spotify_suggest,
    "spotify_like": tool_spotify_like,
}
