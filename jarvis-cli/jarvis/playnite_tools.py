"""Playnite Bridge tools — HTTP API on localhost:19821.

Game *actions* (Play, mods, URLs, emulators) are first-class: list, resolve
by name, cache, and launch by stable action id — not just default Play.
"""

from . import playnite_config
from .playnite_http import DEFAULT_TIMEOUT, api as _api


def _is_library_plugin_action(raw):
    if not isinstance(raw, dict):
        return False
    if raw.get("isLibraryPluginAction"):
        return True
    return (raw.get("type") or "") == "LibraryPlugin"


def _stored_game_actions(actions):
    """LibraryPlugin entries are virtual — never persist them via PUT."""
    if not isinstance(actions, list):
        return actions
    return [a for a in actions if isinstance(a, dict) and not _is_library_plugin_action(a)]


def _compact_action(raw):
    if not isinstance(raw, dict):
        return {}
    out = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type"),
        "isPlayAction": bool(raw.get("isPlayAction")),
    }
    if _is_library_plugin_action(raw):
        out["isLibraryPluginAction"] = True
        out["type"] = raw.get("type") or "LibraryPlugin"
        return out
    path = raw.get("path")
    if isinstance(path, str) and path.strip():
        out["path"] = path.strip()[:140]
    return out


def _compact_game(raw, *, include_actions=True):
    """Slim game dict for tool results. List/search never includes actions or paths."""
    if not isinstance(raw, dict):
        return {}
    out = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "installed": bool(raw.get("isInstalled", raw.get("installed"))),
        "favorite": bool(raw.get("favorite")),
    }
    if raw.get("source"):
        out["source"] = raw.get("source")
    if raw.get("completionStatus") or raw.get("status"):
        out["status"] = raw.get("completionStatus") or raw.get("status")
    pt = raw.get("playtime")
    if pt:
        out["hours"] = round(float(pt) / 3600, 1)
    elif raw.get("hours") is not None:
        out["hours"] = raw.get("hours")
    if include_actions:
        actions_raw = raw.get("gameActions") or raw.get("actions") or []
        if actions_raw:
            out["playActionId"] = raw.get("playActionId")
            out["actionCount"] = raw.get("actionCount") or len(actions_raw)
            out["actions"] = [_compact_action(a) for a in actions_raw[:12] if isinstance(a, dict)]
    return out


def _list_game(raw):
    """Name/playtime/status only — for library search and query results."""
    return _compact_game(raw, include_actions=False)


def _remember_game_dict(game):
    cfg = playnite_config.load_config()
    playnite_config.remember_game(cfg, game)
    playnite_config.save_config(cfg)


def _remember_many(games):
    cfg = playnite_config.load_config()
    for g in games:
        playnite_config.remember_game(cfg, g)
    playnite_config.save_config(cfg)


def _resolve_game_id(args):
    game_id = (args.get("game_id") or "").strip()
    name = (args.get("name") or "").strip()
    if game_id:
        return game_id, None
    if not name:
        return None, {"needs_clarification": True, "message": "Need game_id or game name."}
    found = tool_playnite_find_game({
        "name": name,
        "installed": args.get("installed"),
        "limit": 5,
        "use_cache": True,
    })
    if found.get("error"):
        return None, found
    games = found.get("games") or []
    if not games:
        return None, {"error": f"No game found matching '{name}'."}
    if len(games) > 1 or found.get("needs_clarification"):
        return None, {
            "needs_clarification": True,
            "message": "Several games match — which one?",
            "games": games,
        }
    return games[0].get("id"), None


def _fetch_actions(game_id, *, refresh=False):
    """Return (actions_list, error_dict). Uses cache unless refresh=True."""
    cfg = playnite_config.load_config()
    cached = (cfg.get("games") or {}).get(game_id) or {}
    cached_actions = cached.get("actions") or []
    if not refresh and cached_actions:
        return cached_actions, None

    data = _api("GET", f"/api/games/{game_id}/actions")
    if "error" in data:
        return None, data

    actions = [_compact_action(a) for a in (data.get("actions") or []) if isinstance(a, dict)]
    game_name = data.get("game") or cached.get("name") or game_id
    play_action_id = None
    for a in actions:
        if a.get("isLibraryPluginAction") and a.get("isPlayAction"):
            play_action_id = a.get("id")
            break
    if not play_action_id:
        for a in actions:
            if a.get("isPlayAction"):
                play_action_id = a.get("id")
                break

    _remember_game_dict({
        "id": game_id,
        "name": game_name,
        "actions": actions,
        "playActionId": play_action_id,
        "play_action_id": play_action_id,
    })
    return actions, None


def _match_action(actions, *, action_id=None, action_name=None):
    if not actions:
        return None, {"error": "This game has no configured actions."}

    if action_id:
        action_id = action_id.strip()
        for a in actions:
            if a.get("id") == action_id:
                return a, None
        return None, {"error": f"No action with id {action_id} on this game."}

    if action_name:
        norm = playnite_config.normalize_name(action_name)
        if not norm:
            return None, {"needs_clarification": True, "message": "Which action should I run?"}
        exact = [a for a in actions if playnite_config.normalize_name(a.get("name", "")) == norm]
        if len(exact) == 1:
            return exact[0], None
        if norm in ("play", "default", "default play"):
            lib = [a for a in actions if a.get("isLibraryPluginAction")]
            named_play = [
                a for a in actions
                if playnite_config.normalize_name(a.get("name", "")) == "play"
                and not a.get("isLibraryPluginAction")
            ]
            if named_play:
                return named_play[0], None
            if lib:
                return lib[0], None
        if "library" in norm:
            lib = [a for a in actions if a.get("isLibraryPluginAction")]
            if len(lib) == 1:
                return lib[0], None
        partial = [
            a for a in actions
            if norm in playnite_config.normalize_name(a.get("name", ""))
            or playnite_config.normalize_name(a.get("name", "")) in norm
        ]
        if norm == "play":
            partial = [
                a for a in partial
                if playnite_config.normalize_name(a.get("name", "")) == "play"
                or a.get("isLibraryPluginAction")
            ]
        if len(partial) == 1:
            return partial[0], None
        if len(partial) > 1:
            return None, {
                "needs_clarification": True,
                "message": f"Several actions match '{action_name}' — which one?",
                "actions": partial,
            }
        return None, {
            "needs_clarification": True,
            "message": f"No action matching '{action_name}'.",
            "actions": actions,
        }

    return None, None


def _launch_action_id(game_id, action_id, *, game_name="", action_name=""):
    data = _api("POST", f"/api/games/{game_id}/actions/{action_id}/launch")
    if "error" in data:
        return data
    _remember_game_dict({
        "id": game_id,
        "name": data.get("game") or game_name or game_id,
        "last_action_id": action_id,
        "last_action_name": data.get("action") or action_name,
        "playActionId": action_id if data.get("launchType") in (
            "specific_action", "library_plugin_action",
        ) else None,
    })
    return {
        "ok": True,
        "game_id": game_id,
        "action_id": action_id,
        **{k: data[k] for k in ("game", "action", "actionId", "launchType") if k in data},
    }


def tool_playnite_list_game_actions(args):
    args = args or {}
    game_id, err = _resolve_game_id(args)
    if err:
        return err
    if not game_id:
        return {"error": "Could not resolve game."}

    refresh = args.get("refresh")
    if refresh is None:
        refresh = True
    actions, err = _fetch_actions(game_id, refresh=bool(refresh))
    if err:
        return err

    cfg = playnite_config.load_config()
    cached = (cfg.get("games") or {}).get(game_id) or {}
    default_id = cached.get("play_action_id") or cached.get("playActionId")
    for a in actions:
        if a.get("id") == default_id:
            a["default"] = True

    out = {
        "game_id": game_id,
        "game": cached.get("name"),
        "total": len(actions),
        "actions": actions,
        "playActionId": default_id,
    }
    extras = [a for a in actions if not a.get("isLibraryPluginAction")]
    if any(a.get("isLibraryPluginAction") for a in actions):
        out["message"] = (
            "Includes a virtual LibraryPlugin play action (Steam/Epic/GOG/etc — same as Play in Playnite). "
            "playnite_launch_game uses that default. Use playnite_launch_action only for a named extra. "
            "Do not PUT LibraryPlugin entries back as stored actions."
        )
    elif len(extras) > 1:
        out["message"] = (
            "Multiple stored actions — ask which one, or use playnite_launch_action with action_id/action_name."
        )
    return out


def tool_playnite_launch_action(args):
    args = args or {}
    game_id, err = _resolve_game_id(args)
    if err:
        return err

    action_id = (args.get("action_id") or "").strip()
    action_name = (args.get("action_name") or "").strip()
    if not action_id and not action_name:
        return {
            "needs_clarification": True,
            "message": "Need action_id or action_name — call playnite_list_game_actions first.",
        }

    actions, err = _fetch_actions(game_id)
    if err:
        return err

    action, err = _match_action(actions, action_id=action_id, action_name=action_name)
    if err or not action:
        actions, retry_err = _fetch_actions(game_id, refresh=True)
        if retry_err:
            return err or retry_err
        action, err = _match_action(actions, action_id=action_id, action_name=action_name)
        if err:
            return err
    if not action:
        return {"error": "Could not resolve action."}

    return _launch_action_id(
        game_id,
        action["id"],
        game_name=(args.get("name") or ""),
        action_name=action.get("name") or "",
    )


def tool_playnite_find_game(args):
    args = args or {}
    name = (args.get("name") or "").strip()
    if not name:
        return {"needs_clarification": True, "message": "Which game name should I search for?"}
    if len(name) < 3:
        return {
            "needs_clarification": True,
            "message": "Name is too short — use a real title, or playnite_query_games with filters.",
        }

    cfg = playnite_config.load_config()
    use_cache = args.get("use_cache", True)
    if use_cache:
        cached = playnite_config.cache_lookup(name)
        if cached:
            for g in cached:
                playnite_config.remember_game(cfg, g)
            playnite_config.save_config(cfg)
            if len(cached) == 1:
                return {"from_cache": True, "games": cached}
            return {
                "from_cache": True,
                "needs_clarification": len(cached) > 1,
                "games": cached,
                "message": "Multiple cached matches — ask which one.",
            }

    limit = args.get("limit")
    if limit is None:
        limit = cfg.get("search_limit_default", 5)
    limit = min(int(limit), int(cfg.get("search_limit_max", 10)))

    params = {"q": name, "limit": limit}
    if args.get("installed") is True:
        params["installed"] = "true"
    elif args.get("installed") is False:
        params["installed"] = "false"
    if args.get("favorite") is True:
        params["favorite"] = "true"
    for key in ("source", "genre", "category", "tag", "feature", "platform", "completionStatus"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            params[key] = val.strip()
    if args.get("hidden") is True:
        params["hidden"] = "true"
    if args.get("uncategorized") is True:
        params["uncategorized"] = "true"
    if args.get("offset") is not None:
        params["offset"] = int(args["offset"])

    data = _api("GET", "/api/games", params=params)
    if "error" in data:
        return data

    games = [_list_game(g) for g in (data.get("games") or [])]
    _remember_many(games[:8])

    out = {
        "total": data.get("total"),
        "showing": len(games),
        "games": games,
    }
    if len(games) > 1:
        out["message"] = "Multiple matches — confirm with the user before acting."
    if not games:
        out["message"] = "No games matched. Try a shorter or more specific name, or different filters."
    return out


def _launch_default_play(game_id):
    """Same as clicking Play in Playnite (library plugin or stored play action)."""
    data = _api("POST", f"/api/games/{game_id}/launch")
    if "error" in data:
        return data
    _remember_game_dict({
        "id": game_id,
        "name": data.get("game") or game_id,
        "playActionId": data.get("actionId"),
        "play_action_id": data.get("actionId"),
        "last_action_id": data.get("actionId"),
        "last_action_name": data.get("action"),
    })
    return {"ok": True, "game_id": game_id, **{k: data[k] for k in ("game", "action", "actionId", "launchType") if k in data}}


def tool_playnite_launch_game(args):
    """Default Play in Playnite. Named extras go through playnite_launch_action."""
    args = args or {}
    game_id, err = _resolve_game_id(args)
    if err:
        return err

    action_id = (args.get("action_id") or "").strip()
    action_name = (args.get("action_name") or "").strip()
    if action_id or action_name:
        return tool_playnite_launch_action({
            "game_id": game_id,
            "action_id": action_id,
            "action_name": action_name,
        })

    return _launch_default_play(game_id)


def tool_playnite_library_stats(args):
    data = _api("GET", "/api/stats")
    if "error" in data:
        return data
    keep = (
        "totalGames", "installed", "favorites", "totalPlaytime", "recentlyPlayed",
        "bySource", "byCompletionStatus", "topGenres",
    )
    return {k: data[k] for k in keep if k in data}


def tool_playnite_get_game(args):
    args = args or {}
    game_id = (args.get("game_id") or "").strip()
    if not game_id:
        return {"needs_clarification": True, "message": "Need game_id (from find_game or frequent list)."}

    data = _api("GET", f"/api/games/{game_id}")
    if "error" in data:
        return data

    if args.get("detail") == "full":
        compact = _compact_game(data, include_actions=True)
        _remember_game_dict(compact)
        return data

    compact = _compact_game(data, include_actions=True)
    if data.get("genres"):
        compact["genres"] = data.get("genres")[:6]
    if data.get("tags"):
        compact["tags"] = data.get("tags")[:8]
    if data.get("categories"):
        compact["categories"] = data.get("categories")[:6]
    _remember_game_dict(compact)
    return compact


def tool_playnite_update_game(args):
    args = args or {}
    game_id = (args.get("game_id") or "").strip()
    if not game_id:
        return {"needs_clarification": True, "message": "Need game_id to update."}

    body = {}
    for key in (
        "name", "sortingName", "description", "notes", "version", "completionStatus",
        "hidden", "favorite", "userScore", "communityScore", "criticScore", "releaseDate",
        "categories", "tags", "features", "genres", "developers", "publishers", "series",
        "platforms", "ageRatings", "regions", "links", "gameActions",
    ):
        if key in args and args[key] is not None:
            body[key] = args[key]

    if "gameActions" in body:
        stored = _stored_game_actions(body["gameActions"])
        if stored != body["gameActions"]:
            body["gameActions"] = stored

    if not body:
        return {"needs_clarification": True, "message": "Nothing to update — pass fields to change."}

    data = _api("PUT", f"/api/games/{game_id}", body=body)
    if "error" in data:
        return data
    if "gameActions" in body:
        _fetch_actions(game_id, refresh=True)
    return {"ok": True, "game_id": game_id, "updated": list(body.keys())}


def tool_playnite_list_frequent(args):
    cfg = playnite_config.load_config()
    games, _ = playnite_config._cache_block(cfg)
    if not games:
        return {"games": [], "message": "No cached games yet — find or launch games to build the list."}
    ranked = sorted(
        games.values(),
        key=lambda g: (-int(g.get("use_count", 0)), g.get("last_used", "")),
    )
    limit = int((args or {}).get("limit") or cfg.get("frequent_list_size") or 12)
    return {"games": ranked[:limit]}


PLAYNITE_TOOL_SCHEMAS = [
    {
        "name": "playnite_list_game_actions",
        "description": (
            "List launch actions for a game. Includes a virtual type=LibraryPlugin action on "
            "Steam/Epic/GOG/etc (library integration play) plus stored File/URL/Emulator/Script actions. "
            "LibraryPlugin is not in the DB — never send it back in gameActions updates. "
            "For ordinary 'play this game' use playnite_launch_game."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string", "description": "Resolve game by name if game_id omitted."},
                "refresh": {"type": "boolean", "description": "Bypass cache and fetch fresh from Playnite."},
            },
        },
    },
    {
        "name": "playnite_launch_action",
        "description": (
            "Launch a SPECIFIC game action by action_id or action_name (mod, URL, emulator, or "
            "LibraryPlugin). Confirm first. For ordinary Play, use playnite_launch_game. "
            "Use playnite_list_game_actions if ids are unknown."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string", "description": "Game name if game_id omitted."},
                "action_id": {"type": "string", "description": "Stable action GUID from list_game_actions."},
                "action_name": {"type": "string", "description": "Match action by name, e.g. 'Play', 'Launch Modded'."},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_find_game",
        "description": (
            "Find ONE game by a specific name substring. Max ~5 results, compact fields only "
            "(name, hours, installed, source, status). NEVER use this to list the whole library "
            "or browse by genre — use playnite_query_games WITH filters instead. "
            "Do not pass a single letter or empty-ish query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Game name substring (be specific)."},
                "installed": {"type": "boolean"},
                "favorite": {"type": "boolean"},
                "source": {"type": "string"},
                "genre": {"type": "string"},
                "category": {"type": "string"},
                "tag": {"type": "string"},
                "feature": {"type": "string"},
                "platform": {"type": "string"},
                "completionStatus": {"type": "string"},
                "hidden": {"type": "boolean", "description": "Include hidden games."},
                "uncategorized": {"type": "boolean"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "use_cache": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "playnite_launch_game",
        "description": (
            "Launch a game the same way as Play in Playnite (Steam/Epic/GOG library play, or the "
            "stored play action). Use this for 'play X'. Extra named launchers: playnite_launch_action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "action_id": {"type": "string"},
                "action_name": {"type": "string"},
                "installed": {"type": "boolean"},
                "use_default_play": {"type": "boolean", "description": "Unused; default Play is used when no action_id/name."},
            },
        },
    },
    {
        "name": "playnite_library_stats",
        "description": "Library stats overview — not a full game list.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "playnite_get_game",
        "description": "One game by id — compact includes actions array with ids.",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "detail": {"type": "string", "enum": ["compact", "full"]},
            },
            "required": ["game_id"],
        },
    },
    {
        "name": "playnite_update_game",
        "description": (
            "Update game metadata. gameActions replaces stored File/URL/Emulator/Script actions "
            "(include ids to keep them). Never include type LibraryPlugin — those are virtual."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "favorite": {"type": "boolean"},
                "hidden": {"type": "boolean"},
                "completionStatus": {"type": "string"},
                "userScore": {"type": "integer"},
                "communityScore": {"type": "integer"},
                "criticScore": {"type": "integer"},
                "categories": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "features": {"type": "array", "items": {"type": "string"}},
                "genres": {"type": "array", "items": {"type": "string"}},
                "developers": {"type": "array", "items": {"type": "string"}},
                "publishers": {"type": "array", "items": {"type": "string"}},
                "series": {"type": "array", "items": {"type": "string"}},
                "platforms": {"type": "array", "items": {"type": "string"}},
                "links": {"type": "array", "items": {"type": "object"}},
                "gameActions": {
                    "type": "array",
                    "description": (
                        "Replace stored launch actions only. Do not include LibraryPlugin entries."
                    ),
                    "items": {"type": "object"},
                },
                "name": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["game_id"],
        },
    },
    {
        "name": "playnite_list_frequent",
        "description": "Cached frequent games with ids and known actions — use before library search.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
    },
]

PLAYNITE_TOOLS = {
    "playnite_list_game_actions": tool_playnite_list_game_actions,
    "playnite_launch_action": tool_playnite_launch_action,
    "playnite_find_game": tool_playnite_find_game,
    "playnite_launch_game": tool_playnite_launch_game,
    "playnite_library_stats": tool_playnite_library_stats,
    "playnite_get_game": tool_playnite_get_game,
    "playnite_update_game": tool_playnite_update_game,
    "playnite_list_frequent": tool_playnite_list_frequent,
}
