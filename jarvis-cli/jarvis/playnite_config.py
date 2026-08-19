"""Playnite Bridge settings and frequent-game cache (~/.jarvis/playnite.json).

Token comes from Playnite: Main Menu > Playnite Bridge > copy token / skill.
The cache stores game ids AND action ids so Jarvis can launch a specific
play config without re-querying the library.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "playnite.json"
ENCODING = "utf-8"

DEFAULT_CONFIG = {
    "enabled": True,
    "base_url": "http://127.0.0.1:19821",
    "token": "",
    "search_limit_default": 5,
    "search_limit_max": 10,
    "cache_max_games": 80,
    "frequent_list_size": 12,
}

_norm_re = re.compile(r"[^a-z0-9]+")


def normalize_name(name):
    if not isinstance(name, str):
        return ""
    return _norm_re.sub(" ", name.lower()).strip()


def ensure_config():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding=ENCODING
        )


def load_config():
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(data):
    ensure_config()
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)


def is_configured():
    cfg = load_config()
    if not cfg.get("enabled", True):
        return False
    return bool((cfg.get("token") or "").strip())


def _cache_block(cfg):
    games = cfg.get("games")
    if not isinstance(games, dict):
        games = {}
        cfg["games"] = games
    by_name = cfg.get("by_name")
    if not isinstance(by_name, dict):
        by_name = {}
        cfg["by_name"] = by_name
    return games, by_name


def remember_game(cfg, game):
    """Store a compact game entry, optional actions, and bump use count."""
    if not isinstance(game, dict):
        return
    gid = game.get("id")
    name = game.get("name")
    if not gid or not name:
        return

    games, by_name = _cache_block(cfg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    norm = normalize_name(name)

    prev = games.get(gid, {})
    entry = {
        "id": gid,
        "name": name,
        "normalized_name": norm,
        "installed": bool(game.get("installed", game.get("isInstalled", prev.get("installed")))),
        "favorite": bool(game.get("favorite", prev.get("favorite"))),
        "source": game.get("source") or prev.get("source"),
        "status": game.get("status") or game.get("completionStatus") or prev.get("status"),
        "use_count": int(prev.get("use_count", 0)) + 1,
        "last_used": now,
    }
    if game.get("hours") is not None:
        entry["hours"] = game.get("hours")
    elif game.get("playtime"):
        entry["hours"] = round(float(game["playtime"]) / 3600, 1)

    if game.get("play_action_id"):
        entry["play_action_id"] = game["play_action_id"]
    elif game.get("playActionId"):
        entry["play_action_id"] = game["playActionId"]
    elif prev.get("play_action_id"):
        entry["play_action_id"] = prev["play_action_id"]

    actions = game.get("actions")
    if isinstance(actions, list) and actions:
        entry["actions"] = actions[:12]
    elif prev.get("actions"):
        entry["actions"] = prev["actions"]

    if game.get("last_action_id"):
        entry["last_action_id"] = game["last_action_id"]
        entry["last_action_name"] = game.get("last_action_name") or prev.get("last_action_name")

    games[gid] = entry
    if norm:
        by_name[norm] = gid

    max_games = int(cfg.get("cache_max_games") or DEFAULT_CONFIG["cache_max_games"])
    if len(games) > max_games:
        victims = sorted(
            games.values(),
            key=lambda g: (g.get("use_count", 0), g.get("last_used", "")),
        )
        for old in victims[: len(games) - max_games]:
            old_id = old.get("id")
            old_norm = old.get("normalized_name")
            if old_id:
                games.pop(old_id, None)
            if old_norm and by_name.get(old_norm) == old_id:
                by_name.pop(old_norm, None)

    save_config(cfg)


def cache_lookup(name):
    """Return cached game dict(s) matching name, or None if no hit."""
    cfg = load_config()
    games, by_name = _cache_block(cfg)
    norm = normalize_name(name)
    if not norm:
        return None

    if norm in by_name:
        hit = games.get(by_name[norm])
        return [hit] if hit else None

    partial = []
    for g in games.values():
        gn = g.get("normalized_name") or normalize_name(g.get("name", ""))
        if norm == gn or norm in gn or gn in norm:
            partial.append(g)
    if partial:
        partial.sort(key=lambda g: (-g.get("use_count", 0), g.get("name", "")))
        return partial[:5]
    return None


def cache_get_game(game_id):
    if not game_id:
        return None
    cfg = load_config()
    games, _ = _cache_block(cfg)
    return games.get(game_id)


def frequent_games_context(max_games=None):
    """One-line-per-game hint for the system prompt (ids + actions, no library query)."""
    if not is_configured():
        return ""
    cfg = load_config()
    games, _ = _cache_block(cfg)
    if not games:
        return ""

    limit = max_games or int(cfg.get("frequent_list_size") or DEFAULT_CONFIG["frequent_list_size"])
    ranked = sorted(
        games.values(),
        key=lambda g: (-int(g.get("use_count", 0)), g.get("last_used", "")),
    )[:limit]
    lines = [
        "Playnite frequent games (game_id + action ids — use playnite_launch_action):"
    ]
    for g in ranked:
        bits = [g.get("name", "?"), f"game={g.get('id', '')}"]
        if g.get("play_action_id"):
            bits.append(f"default_play={g['play_action_id']}")
        if g.get("last_action_id"):
            bits.append(f"last_action={g['last_action_id']}")
        actions = g.get("actions") or []
        if len(actions) > 1:
            bits.append(f"{len(actions)} actions cached")
        elif len(actions) == 1:
            bits.append(f"action={actions[0].get('id')}")
        if g.get("installed") is False:
            bits.append("not installed")
        lines.append(" | ".join(str(b) for b in bits if b))
    return "\n".join(lines)
