"""Playnite Bridge tools — HTTP API on localhost:19821.

Game *actions* (Play, mods, URLs, emulators) are first-class: list, resolve
by name, cache, and launch by stable action id — not just default Play.
"""

from . import playnite_config
from .playnite_http import DEFAULT_TIMEOUT, api as _api


def _compact_action(raw):
    if not isinstance(raw, dict):
        return {}
    out = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type"),
        "isPlayAction": bool(raw.get("isPlayAction")),
    }
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


from .playnite_http import DEFAULT_TIMEOUT, api as _api


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
    if not refresh and cached.get("actions"):
        return cached["actions"], None

    data = _api("GET", f"/api/games/{game_id}/actions")
    if "error" in data:
        return None, data

    actions = [_compact_action(a) for a in (data.get("actions") or []) if isinstance(a, dict)]
    game_name = data.get("game") or cached.get("name") or game_id
    play_action_id = None
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
        partial = [
            a for a in actions
            if norm in playnite_config.normalize_name(a.get("name", ""))
            or playnite_config.normalize_name(a.get("name", "")) in norm
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
        "playActionId": action_id if data.get("launchType") == "specific_action" else None,
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

    refresh = bool(args.get("refresh"))
    actions, err = _fetch_actions(game_id, refresh=refresh)
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
    if len(actions) > 1:
        out["message"] = (
            "Multiple actions — ask which one, or use playnite_launch_action with action_id/action_name."
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


def tool_playnite_launch_game(args):
    """Default play — if multiple actions exist, require explicit choice unless use_default_play."""
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

    actions, err = _fetch_actions(game_id)
    if err:
        return err

    if len(actions) > 1 and not args.get("use_default_play"):
        default_id = None
        cfg = playnite_config.load_config()
        default_id = (cfg.get("games") or {}).get(game_id, {}).get("play_action_id") or (
            cfg.get("games") or {}).get(game_id, {}).get("playActionId")
        return {
            "needs_clarification": True,
            "message": (
                "This game has multiple actions — ask which one, or use playnite_launch_action. "
                "Pass use_default_play=true only to run the default Play action."
            ),
            "game_id": game_id,
            "playActionId": default_id,
            "actions": actions,
        }

    if len(actions) == 1:
        return _launch_action_id(game_id, actions[0]["id"])

    cfg = playnite_config.load_config()
    default_id = (cfg.get("games") or {}).get(game_id, {}).get("play_action_id") or (
        cfg.get("games") or {}).get(game_id, {}).get("playActionId")
    if default_id:
        return _launch_action_id(game_id, default_id)

    data = _api("POST", f"/api/games/{game_id}/launch")
    if "error" in data:
        return data
    _remember_game_dict({"id": game_id, "name": data.get("game") or game_id})
    return {"ok": True, **{k: data[k] for k in ("game", "action", "actionId", "launchType") if k in data}}


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
            "List every launch action for a game (Play, mods, URLs, emulators). Each action has a "
            "stable action id. Call this before launching when the user might want a non-default action."
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
            "Launch a SPECIFIC game action by action_id or action_name. This is the correct tool when "
            "the user names a mod, alternate launch, URL action, etc. Confirm with the user first. "
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
            "Launch the default Play action ONLY when the game has one action or user confirmed default. "
            "If multiple actions exist, returns the action list — use playnite_launch_action instead. "
            "Pass action_id/action_name here too (delegates to launch_action). use_default_play=true skips prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "action_id": {"type": "string"},
                "action_name": {"type": "string"},
                "installed": {"type": "boolean"},
                "use_default_play": {"type": "boolean", "description": "Force default Play when multiple actions."},
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
        "description": "Update game metadata. gameActions replaces the full actions list (include ids to keep them).",
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
                        "Replace all launch actions. Each item: id, name, type, isPlayAction, path, etc."
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
