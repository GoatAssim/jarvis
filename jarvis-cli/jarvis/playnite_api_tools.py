"""Additional Playnite Bridge API tools — every endpoint not in playnite_tools core."""

from pathlib import Path

from . import playnite_config
from .playnite_http import api as _api, api_binary as _api_binary, api_text as _api_text
from .playnite_tools import (
    _compact_game,
    _list_game,
    _fetch_actions,
    _remember_game_dict,
    _remember_many,
    _resolve_game_id,
)

QUERY_LIST_MAX = 25
_QUERY_FILTER_KEYS = (
    "q", "favorite", "hidden", "uncategorized", "untagged",
    "playtimeMin", "playtimeMax", "releaseYearMin", "releaseYearMax",
    "source", "completionStatus",
    "genres", "categories", "tags", "features", "developers",
    "publishers", "platforms",
)

_EMPTY = {"type": "object", "properties": {}, "required": []}

_COLLECTION_LIST = {
    "categories": "/api/categories",
    "genres": "/api/genres",
    "tags": "/api/tags",
    "features": "/api/features",
    "platforms": "/api/platforms",
    "sources": "/api/sources",
    "companies": "/api/companies",
    "series": "/api/series",
    "completion_statuses": "/api/completion-statuses",
    "age_ratings": "/api/age-ratings",
    "regions": "/api/regions",
    "filter_presets": "/api/filter-presets",
    "emulators": "/api/emulators",
}

_COLLECTION_CREATE = {
    "categories": "/api/categories",
    "genres": "/api/genres",
    "tags": "/api/tags",
    "features": "/api/features",
    "series": "/api/series",
    "completion_statuses": "/api/completion-statuses",
}


def _game_id(args):
    game_id, err = _resolve_game_id(args or {})
    if err:
        return None, err
    if not game_id:
        return None, {"error": "Could not resolve game."}
    return game_id, None


def tool_playnite_delete_game(args):
    game_id, err = _game_id(args)
    if err:
        return err
    return _api("DELETE", f"/api/games/{game_id}")


def tool_playnite_get_action(args):
    args = args or {}
    game_id, err = _game_id(args)
    if err:
        return err
    action_id = (args.get("action_id") or "").strip()
    if not action_id:
        return {"needs_clarification": True, "message": "Need action_id from list_game_actions."}
    return _api("GET", f"/api/games/{game_id}/actions/{action_id}")


def tool_playnite_install_game(args):
    game_id, err = _game_id(args)
    if err:
        return err
    return _api("POST", f"/api/games/{game_id}/install")


def tool_playnite_uninstall_game(args):
    game_id, err = _game_id(args)
    if err:
        return err
    return _api("POST", f"/api/games/{game_id}/uninstall")


def tool_playnite_manage_game_lists(args):
    """Replace or append categories/tags/features/genres, or set completion status."""
    args = args or {}
    game_id, err = _game_id(args)
    if err:
        return err

    field = (args.get("field") or "").strip().lower()
    if field == "status":
        status = (args.get("status") or args.get("completion_status") or "").strip()
        if not status:
            return {"needs_clarification": True, "message": "Need status name for completion status."}
        return _api("PUT", f"/api/games/{game_id}/status", body={"status": status})

    if field not in ("categories", "tags", "features", "genres"):
        return {
            "error": "field must be categories, tags, features, genres, or status.",
        }

    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"needs_clarification": True, "message": f"Need items array for {field}."}

    mode = (args.get("mode") or "set").strip().lower()
    if mode == "add":
        return _api("POST", f"/api/games/{game_id}/{field}", body={field: items})
    return _api("PUT", f"/api/games/{game_id}/{field}", body={field: items})


def tool_playnite_fetch_game_art(args):
    game_id, err = _game_id(args)
    if err:
        return err
    return _api("POST", f"/api/games/{game_id}/fetch-art")


def tool_playnite_list_missing_art(args):
    data = _api("GET", "/api/games/missing-art")
    if "error" in data:
        return data
    games = data.get("games") or data if isinstance(data, list) else (data.get("games") or [])
    if isinstance(games, list):
        slim = [_list_game(g) if isinstance(g, dict) else g for g in games[:40]]
        return {"showing": len(slim), "total": len(games), "games": slim}
    return data


def tool_playnite_query_games(args):
    args = args or {}
    body = {}
    for key in (
        "q", "installed", "favorite", "hidden", "uncategorized", "untagged",
        "playtimeMin", "playtimeMax", "releaseYearMin", "releaseYearMax",
        "source", "completionStatus", "sort", "descending", "groupBy",
        "limit", "offset",
    ):
        if key in args and args[key] is not None:
            body[key] = args[key]
    for key in (
        "genres", "categories", "tags", "features", "developers",
        "publishers", "platforms",
    ):
        if key in args and args[key] is not None:
            body[key] = args[key]

    if not body:
        return {
            "needs_clarification": True,
            "message": (
                "Must pass FILTERS (genre, source, installed+playtime, favorite, etc.) or groupBy. "
                "Never dump the whole library."
            ),
        }

    has_group = bool(body.get("groupBy"))
    has_filter = any(k in body and body[k] not in (None, "", [], False) for k in _QUERY_FILTER_KEYS)
    # installed=true alone is still most of the library — not a real filter
    if not has_group and not has_filter:
        return {
            "needs_clarification": True,
            "message": (
                "Too broad. Add filters: genres, source, playtimeMin, favorite, completionStatus, "
                "or use groupBy for library-wide stats. Do not list every game."
            ),
        }

    if has_group:
        body.pop("limit", None)
        data = _api("POST", "/api/games/query", body=body)
        if "error" in data:
            return data
        return data

    limit = body.get("limit")
    if limit is None:
        limit = 15
    body["limit"] = min(int(limit), QUERY_LIST_MAX)

    data = _api("POST", "/api/games/query", body=body)
    if "error" in data:
        return data

    if "games" in data:
        games = [_list_game(g) for g in (data.get("games") or [])]
        _remember_many(games[:8])
        total = data.get("total")
        out = {
            "total": total,
            "showing": len(games),
            "limit": body["limit"],
            "games": games,
            "fields": "name, id, hours, installed, favorite, source, status only",
        }
        if total and int(total) > len(games):
            out["message"] = (
                f"{total} matches — showing {len(games)}. Narrow with more filters "
                "(genre, source, playtime, status) instead of paging the whole library."
            )
        return out
    return data


def tool_playnite_list_collections(args):
    args = args or {}
    kind = (args.get("kind") or "").strip().lower()
    path = _COLLECTION_LIST.get(kind)
    if not path:
        return {"error": f"Unknown collection kind '{kind}'."}
    return _api("GET", path)


def tool_playnite_create_collection(args):
    args = args or {}
    kind = (args.get("kind") or "").strip().lower()
    path = _COLLECTION_CREATE.get(kind)
    if not path:
        return {"error": f"Cannot create items for collection kind '{kind}'."}
    name = (args.get("name") or "").strip()
    if not name:
        return {"needs_clarification": True, "message": "Need name for the new collection item."}
    return _api("POST", path, body={"name": name})


def tool_playnite_view(args):
    args = args or {}
    action = (args.get("action") or "").strip().lower()
    if action == "state":
        return _api("GET", "/api/view/state")
    if action == "selected":
        data = _api("GET", "/api/view/selected")
        if "error" in data:
            return data
        games = data.get("games") or data.get("selected") or []
        if isinstance(games, list):
            compact = [_list_game(g) for g in games if isinstance(g, dict)]
            _remember_many(compact)
            return {**data, "games": compact}
        return data
    if action == "select":
        ids = args.get("game_ids")
        if not isinstance(ids, list) or not ids:
            return {"needs_clarification": True, "message": "Need game_ids array to select in UI."}
        return _api("POST", "/api/view/select", body={"gameIds": ids})
    if action == "filter":
        preset_id = (args.get("preset_id") or "").strip()
        if not preset_id:
            return {"needs_clarification": True, "message": "Need preset_id from filter_presets list."}
        return _api("POST", "/api/view/filter", body={"presetId": preset_id})
    return {"error": "action must be state, selected, select, or filter."}


def tool_playnite_app_info(args):
    return _api("GET", "/api/app/info")


def tool_playnite_list_addons(args):
    return _api("GET", "/api/app/addons")


def tool_playnite_list_plugins(args):
    return _api("GET", "/api/plugins")


def tool_playnite_notify(args):
    args = args or {}
    text = (args.get("text") or "").strip()
    if not text:
        return {"needs_clarification": True, "message": "Need notification text."}
    ntype = (args.get("type") or "info").strip().lower()
    if ntype not in ("info", "error"):
        ntype = "info"
    return _api("POST", "/api/notifications", body={"text": text, "type": ntype})


def tool_playnite_auto_categorize(args):
    return _api("POST", "/api/auto-categorize")


def tool_playnite_fetch_all_art(args):
    return _api("POST", "/api/fetch-all-art")


def tool_playnite_get_achievements(args):
    game_id, err = _game_id(args)
    if err:
        return err
    return _api("GET", f"/api/games/{game_id}/achievements")


def tool_playnite_get_activity(args):
    game_id, err = _game_id(args)
    if err:
        return err
    return _api("GET", f"/api/games/{game_id}/activity")


def tool_playnite_get_cover(args):
    args = args or {}
    game_id, err = _game_id(args)
    if err:
        return err

    cover_type = (args.get("type") or "cover").strip().lower()
    params = {}
    if cover_type in ("icon", "background"):
        params["type"] = cover_type

    resp, err = _api_binary("GET", f"/api/games/{game_id}/cover", params=params or None)
    if err:
        return err

    ext = ".jpg"
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "png" in ctype:
        ext = ".png"
    elif "webp" in ctype:
        ext = ".webp"

    out_dir = Path.home() / ".jarvis" / "playnite-covers"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{game_id}-{cover_type}{ext}"
    out_path = out_dir / filename
    out_path.write_bytes(resp.content)

    return {
        "ok": True,
        "game_id": game_id,
        "type": cover_type,
        "path": str(out_path),
        "bytes": len(resp.content),
        "contentType": resp.headers.get("Content-Type"),
    }


def tool_playnite_eval(args):
    args = args or {}
    code = (args.get("code") or "").strip()
    if not code:
        return {"needs_clarification": True, "message": "Need C# code to evaluate in Playnite."}
    body = {"code": code}
    if args.get("timeout_ms") is not None:
        body["timeoutMs"] = int(args["timeout_ms"])
    if args.get("on_ui_thread") is not None:
        body["onUiThread"] = bool(args["on_ui_thread"])
    return _api("POST", "/api/eval", body=body, timeout=min(int(body.get("timeoutMs", 10000)) / 1000 + 2, 32))


def tool_playnite_rotate_token(args):
    data = _api("POST", "/api/auth/rotate")
    if "error" in data:
        return data
    token = (data.get("token") or "").strip()
    if token:
        cfg = playnite_config.load_config()
        cfg["token"] = token
        playnite_config.save_config(cfg)
        return {
            "ok": True,
            "message": "Token rotated. New token saved to ~/.jarvis/playnite.json.",
        }
    return data


def tool_playnite_get_skill(args):
    return _api_text("GET", "/api/skill.md")


PLAYNITE_API_TOOL_SCHEMAS = [
    {
        "name": "playnite_delete_game",
        "description": "Permanently delete a game from the Playnite library. Confirm with the user first.",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string", "description": "Resolve game by name if game_id omitted."},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_get_action",
        "description": "Full metadata for one game action by action_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "action_id": {"type": "string"},
                "installed": {"type": "boolean"},
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "playnite_install_game",
        "description": "Start installing a game via its library source. Confirm with the user first.",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_uninstall_game",
        "description": "Uninstall a game. Confirm with the user first.",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_manage_game_lists",
        "description": (
            "Set or append categories/tags/features/genres on a game, or set completion status. "
            "mode=set replaces; mode=add appends. field=status uses status param."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "field": {
                    "type": "string",
                    "enum": ["categories", "tags", "features", "genres", "status"],
                },
                "mode": {"type": "string", "enum": ["set", "add"], "description": "Ignored for status."},
                "items": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "description": "Completion status name when field=status."},
                "installed": {"type": "boolean"},
            },
            "required": ["field"],
        },
    },
    {
        "name": "playnite_fetch_game_art",
        "description": "Fetch missing cover/art for one game (Steam CDN + IGDB fallback).",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_list_missing_art",
        "description": "List games that are missing artwork in the library.",
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_query_games",
        "description": (
            "REQUIRED for library browsing. ALWAYS pass filters (genres, source, playtimeMin, "
            "favorite, completionStatus, etc.) or groupBy. NEVER query with no filters or only "
            "installed=true — that dumps thousands of games. Returns compact rows "
            "(name, hours, installed, source, status) capped at 25. "
            "For 'whole library' questions use groupBy (genre/source/developer) instead of listing games. "
            "playnite_find_game is only for looking up a specific title."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "installed": {"type": "boolean"},
                "favorite": {"type": "boolean"},
                "hidden": {"type": "boolean"},
                "uncategorized": {"type": "boolean"},
                "untagged": {"type": "boolean"},
                "playtimeMin": {"type": "integer", "description": "Seconds."},
                "playtimeMax": {"type": "integer", "description": "Seconds."},
                "releaseYearMin": {"type": "integer"},
                "releaseYearMax": {"type": "integer"},
                "source": {"type": "string"},
                "completionStatus": {"type": "string"},
                "genres": {"type": "array", "items": {"type": "string"}},
                "categories": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "features": {"type": "array", "items": {"type": "string"}},
                "developers": {"type": "array", "items": {"type": "string"}},
                "publishers": {"type": "array", "items": {"type": "string"}},
                "platforms": {"type": "array", "items": {"type": "string"}},
                "sort": {
                    "type": "string",
                    "enum": ["name", "playtime", "added", "release", "lastplayed"],
                },
                "descending": {"type": "boolean"},
                "groupBy": {
                    "type": "string",
                    "enum": ["genre", "developer", "publisher", "source", "platform", "year", "completionStatus"],
                },
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        },
    },
    {
        "name": "playnite_list_collections",
        "description": "List a Playnite database collection (categories, genres, tags, sources, emulators, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(_COLLECTION_LIST.keys()),
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "playnite_create_collection",
        "description": "Create a category, genre, tag, feature, series, or completion status by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(_COLLECTION_CREATE.keys()),
                },
                "name": {"type": "string"},
            },
            "required": ["kind", "name"],
        },
    },
    {
        "name": "playnite_view",
        "description": (
            "Control or read Playnite UI: state (view mode/sort), selected games, select games, apply filter preset."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["state", "selected", "select", "filter"]},
                "game_ids": {"type": "array", "items": {"type": "string"}},
                "preset_id": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "playnite_app_info",
        "description": "Playnite app version, desktop/fullscreen mode, and paths.",
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_list_addons",
        "description": "Installed and disabled Playnite addon IDs.",
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_list_plugins",
        "description": "Loaded, installed, and disabled Playnite plugins.",
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_notify",
        "description": "Show an in-Playnite notification toast.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "type": {"type": "string", "enum": ["info", "error"]},
            },
            "required": ["text"],
        },
    },
    {
        "name": "playnite_auto_categorize",
        "description": "Auto-categorize all uncategorized games by primary genre.",
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_fetch_all_art",
        "description": "Fetch missing artwork for all games in the library.",
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_get_achievements",
        "description": "Achievements for a game (requires SuccessStory plugin).",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_get_activity",
        "description": "Play sessions for a game (requires GameActivity plugin).",
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_get_cover",
        "description": (
            "Download game cover/icon/background image to ~/.jarvis/playnite-covers/ and return the file path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "name": {"type": "string"},
                "type": {"type": "string", "enum": ["cover", "icon", "background"]},
                "installed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "playnite_eval",
        "description": (
            "Run C# inside Playnite (PlayniteApi, Plugin). Use for complex queries only. "
            "Dangerous — confirm intent with user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "timeout_ms": {"type": "integer", "description": "1-30000, default 10000."},
                "on_ui_thread": {"type": "boolean", "description": "Required for UI operations."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "playnite_rotate_token",
        "description": (
            "Rotate the Playnite Bridge API token. Saves the new token to playnite.json; old token stops working."
        ),
        "parameters": _EMPTY,
    },
    {
        "name": "playnite_get_skill",
        "description": "Fetch the Playnite Bridge skill.md file (includes current API token).",
        "parameters": _EMPTY,
    },
]

PLAYNITE_API_TOOLS = {
    "playnite_delete_game": tool_playnite_delete_game,
    "playnite_get_action": tool_playnite_get_action,
    "playnite_install_game": tool_playnite_install_game,
    "playnite_uninstall_game": tool_playnite_uninstall_game,
    "playnite_manage_game_lists": tool_playnite_manage_game_lists,
    "playnite_fetch_game_art": tool_playnite_fetch_game_art,
    "playnite_list_missing_art": tool_playnite_list_missing_art,
    "playnite_query_games": tool_playnite_query_games,
    "playnite_list_collections": tool_playnite_list_collections,
    "playnite_create_collection": tool_playnite_create_collection,
    "playnite_view": tool_playnite_view,
    "playnite_app_info": tool_playnite_app_info,
    "playnite_list_addons": tool_playnite_list_addons,
    "playnite_list_plugins": tool_playnite_list_plugins,
    "playnite_notify": tool_playnite_notify,
    "playnite_auto_categorize": tool_playnite_auto_categorize,
    "playnite_fetch_all_art": tool_playnite_fetch_all_art,
    "playnite_get_achievements": tool_playnite_get_achievements,
    "playnite_get_activity": tool_playnite_get_activity,
    "playnite_get_cover": tool_playnite_get_cover,
    "playnite_eval": tool_playnite_eval,
    "playnite_rotate_token": tool_playnite_rotate_token,
    "playnite_get_skill": tool_playnite_get_skill,
}
