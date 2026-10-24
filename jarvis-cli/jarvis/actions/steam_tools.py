"""Steam from Jarvis (N22 parts b and c): see the library, install a game, start an update.

What this is, and is not:
  * READ-ONLY facts first: `steam_library` lists the games Steam has installed (name, size, last update,
    Steam's own raw flags and build numbers) from the manifests Steam writes.  It changes nothing.
  * `steam_install_game` and `steam_update_game` ask STEAM to open its own install dialog
    (`steam://install/<appid>`).  Steam does the download and shows its own "Install" window where the
    owner chooses the library and confirms; the progress row in Downloads already exists (the Steam
    provider, hub/steam.py).  Both tools are confirmed with the owner before every call
    (TOOL_CONFIRM_REQUIRED) and refuse a chat guest before doing anything.
  * Steam stays READ-ONLY for pause and cancel (decision S2): there is no tool here, and no row control,
    that pauses, cancels, deletes or edits anything of Steam's.
  * "Which games have an update" is NOT answered yet: no signal has been measured against a real Steam
    (tests/steam_update_checks.py is the probe that will measure it).  Until then a game's
    `update_pending` is "yes" only while Steam is fetching its update right now, and "unknown" otherwise,
    with the raw numbers beside it.  The tools say so; they never guess.
  * No new setting: nothing here needs one.  A game that is not owned makes Steam show a dialog or do
    nothing; Jarvis can only report that it saw no change.
"""
import json
import os

from ..hub import steam


def _is_chat_guest():
    """True when this turn answers a chat message from someone who is NOT the owner.  No chat context
    (the PC, a scheduled job) is the owner's own.  Same rule as download_hub._is_chat_guest: an unreadable
    value counts as a guest, because a permission check fails closed."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001  (bad JSON, or nested too deep to read: either way, not the owner)
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def _guest_refusal(tool):
    return {"ok": False, "error": "%s is the owner's: someone in a chat can't use it" % tool}


def _fail(label, exc):
    return {"ok": False, "error": "%s failed: %s" % (label, str(exc)[:200])}


def _library():
    """(root, libraries, files) or an error dict."""
    root, libs = steam.find_libraries()
    if not libs:
        return {"ok": False, "error": ("Steam's folder wasn't found on this PC, so there is no library to read "
                                       "(Steam not installed, or in an unusual place: set JARVIS_STEAM_ROOT)")}
    return root, libs, steam.read_manifests(libs)


def _games(files):
    return steam.game_facts(files)


def _brief(game):
    keep = ("appid", "name", "library", "size_on_disk", "last_updated", "fully_installed", "update_pending",
            "update_reason", "transfer", "state_flags", "build_id", "target_build_id", "bytes_to_download",
            "bytes_downloaded")
    return {k: game[k] for k in keep}


def _ask_which(what, query, candidates):
    names = ["%s (app id %s)" % (g["name"], g["appid"]) for g in candidates[:8]]
    if candidates:
        question = "Which game do you mean by '%s'? %s" % (str(query)[:60], "; ".join(names))
    else:
        question = ("'%s' isn't among the games Steam has installed here. Give its Steam app id (the number in its store "
                    "page address, store.steampowered.com/app/<number>)." % str(query)[:60])
    return {"ok": True, "needs_clarification": True, "question": question, "message": question}


# --------------------------------------------------------------------------- the tools

def tool_steam_library(args):
    if _is_chat_guest():
        return _guest_refusal("steam_library")
    try:
        args = args or {}
        found = _library()
        if isinstance(found, dict):
            return found
        _root, libs, files = found
        games = _games(files)
        query = str(args.get("search") or "").strip().casefold()
        if query:
            games = [g for g in games if query in g["name"].casefold() or query == g["appid"]]
        if args.get("updating"):
            games = [g for g in games if g["update_pending"] == "yes"]
        try:
            limit = max(1, min(int(args.get("limit") or 50), steam.LIBRARY_LIMIT))
        except (TypeError, ValueError):
            limit = 50
        out = {"ok": True, "libraries": len(libs), "count": len(games), "games": [_brief(g) for g in games[:limit]],
               "steam_running": steam.steam_running(),
               "note": ("update_pending is 'yes' only while Steam is fetching the update right now; 'unknown' means Jarvis "
                        "cannot tell yet (the signal has not been measured), not that there is none. state_flags, "
                        "build_id and target_build_id are what Steam wrote, unread.")}
        if len(games) > limit:
            out["truncated"] = len(games) - limit
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail("steam_library", exc)


def _start(tool, args, update):
    """The shared body of install and update: resolve, check, hand one URL to Steam, look, say what was seen."""
    if _is_chat_guest():
        return _guest_refusal(tool)
    try:
        args = args or {}
        query = str(args.get("game") or "").strip()
        if not query:
            return {"ok": True, "needs_clarification": True, "question": "Which game? Give its name (if it is in your Steam "
                    "library here) or its Steam app id.", "message": "Which game? Give its name or its Steam app id."}
        if update and query.casefold() in ("all", "everything", "all games", "every game"):
            return {"ok": False, "error": ("'update all' isn't available: Jarvis can't yet tell which games need an update (that "
                                           "signal has not been measured). Name one game, or update them in Steam.")}
        found = _library()
        if isinstance(found, dict):
            return found
        _root, libs, files = found
        games = _games(files)
        game, candidates = steam.find_game(games, query)
        if game is None and not (query.isdigit() and not update):
            return _ask_which(tool, query, candidates)
        appid = game["appid"] if game else query
        name = game["name"] if game else None
        if update and game is None:
            return _ask_which(tool, query, candidates)
        if game and game["transfer"]:
            return {"ok": True, "launched": False, "appid": appid, "name": name, "transfer": game["transfer"],
                    "message": "%s is already being fetched by Steam now (%s); nothing was started. Pause and cancel are done in Steam."
                               % (name, game["transfer"]["state"])}
        if game and game["fully_installed"] and not update:
            return {"ok": True, "launched": False, "appid": appid, "name": name, "already_installed": True,
                    "message": "%s is already installed (Steam marks it fully installed); nothing was started. "
                               "To look for an update use steam_update_game." % name}
        if not steam.steam_running():
            return {"ok": False, "error": "Steam isn't running. Start Steam and sign in, then ask again; nothing was started."}
        before = steam.manifest_signature(libs, appid)      # taken first: a change the launch causes must show as a change
        ok, err = steam.launch_install(appid)
        if not ok:
            return {"ok": False, "error": err}
        seen = steam.observe_start(libs, appid, seconds=steam.OBSERVE_SECONDS, step=steam.OBSERVE_STEP, before=before)
        label = name or "app %s" % appid
        if seen == "started":
            message = ("Steam started fetching %s; it is a Steam row in Downloads now. Steam rows are read-only: pause and "
                       "cancel are done in Steam." % label)
        else:
            message = ("Asked Steam to %s %s. Steam normally shows its own window for this (pick the library and confirm "
                       "there); if it is waiting, that window is where to answer. %s The Steam row appears in Downloads once "
                       "Steam starts fetching. If Steam says nothing at all, the game may not be on this account."
                       % ("update" if update else "install", label,
                          "Steam's files changed but it is not fetching yet." if seen == "changed"
                          else "Jarvis saw no change in Steam's files within %d s." % int(steam.OBSERVE_SECONDS)))
        return {"ok": True, "launched": True, "appid": appid, "name": name, "observed": seen, "message": message}
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, exc)


def tool_steam_install_game(args):
    return _start("steam_install_game", args, update=False)


def tool_steam_update_game(args):
    return _start("steam_update_game", args, update=True)


# --------------------------------------------------------------------------- registry

TOOL_SCHEMAS = [
    {"name": "steam_library",
     "description": ("List the games Steam has installed on this PC (name, size, last update, Steam's raw flags and build numbers). "
                     "Read-only. 'update_pending' is yes only while Steam is fetching an update right now and unknown otherwise: say "
                     "that plainly, never claim a game has no update."),
     "parameters": {"type": "object", "properties": {
         "search": {"type": "string", "description": "Part of a game's name, or its app id."},
         "updating": {"type": "boolean", "description": "Only games Steam is updating right now."},
         "limit": {"type": "integer", "description": "How many to list (default 50, most 200)."}}}},
    {"name": "steam_install_game",
     "description": ("Ask Steam to install a game: opens Steam's own install dialog for it (the owner confirms there; this tool "
                     "is confirmed with the owner first). Give the game's name if it is already in the Steam library here, or its "
                     "Steam app id. Steam must be running. Progress shows as a read-only Steam row in Downloads."),
     "parameters": {"type": "object", "properties": {
         "game": {"type": "string", "description": "Game name (an installed game) or Steam app id (any game on the account)."}},
         "required": ["game"]}},
    {"name": "steam_update_game",
     "description": ("Ask Steam to look for and fetch an update for ONE installed game (opens Steam's own dialog; confirmed with the "
                     "owner first). Not 'update all': name the game."),
     "parameters": {"type": "object", "properties": {
         "game": {"type": "string", "description": "An installed game's name or app id."}},
         "required": ["game"]}},
]

TOOLS = {"steam_library": tool_steam_library, "steam_install_game": tool_steam_install_game,
         "steam_update_game": tool_steam_update_game}
TOOL_GROUP = "steam"

TOOL_KEYWORDS = {
    "steam_library": {"steam library": 10, "steam games": 10, "installed steam games": 10, "which steam games": 10,
                      "steam updates": 8, "steam game updates": 9},
    "steam_install_game": {"install on steam": 10, "install the steam game": 10, "install a steam game": 10,
                           "download on steam": 9, "install game from steam": 9, "get it on steam": 6},
    "steam_update_game": {"update steam game": 10, "update the steam game": 10, "update it on steam": 9,
                          "steam update for": 8},
}

# Both change what the owner's PC downloads, and both open a window in Steam: asked first, every time.
TOOL_CONFIRM_REQUIRED = {"steam_install_game", "steam_update_game"}
TOOL_AI_REVIEW = set()

TEST_CHECKLIST_GROUP = {
    "label": "Steam",
    "blurb": "See the Steam library, install a game, update one: steam_library, steam_install_game, steam_update_game. Steam stays read-only for pause and cancel.",
}

TEST_CHECKLIST = {
    "steam_library": {
        "does": "Lists the games Steam has installed here, from Steam's own files. Read-only.",
        "steps": [
            {"run": {}, "expect": "ok: true, your installed games with name, size, last update, Steam's flags. Compare 3 of them with Steam's own Library. Every game says update_pending 'unknown' unless Steam is fetching its update right now."},
            {"run": {"search": "<part of a game's name>"}, "expect": "only the matching games."},
            {"run": {"updating": True}, "expect": "empty, or only the games Steam is updating at this moment (start one in Steam, run it again)."},
            {"ask": "Which of my Steam games have updates?", "expect": "Calls steam_library and says honestly that it can't tell yet; it must NOT claim the others are up to date."},
        ],
        "needs": ["Steam installed on this PC"],
        "watch": ["Nothing in Steam's files is changed.", "From a remote chat guest (Discord / Instagram) it is refused before anything is read: 'steam_library is the owner's: someone in a chat can't use it'; a guest is not even offered the tool."],
    },
    "steam_install_game": {
        "does": "Asks Steam to open its install dialog for a game (name of an installed game, or any app id on your account).",
        "steps": [
            {"run": {"game": "<app id of a game you own and do NOT have installed>"}, "expect": "asks first (every call does); after confirming: ok: true, launched: true; Steam shows its own Install window: confirm it there. When Steam starts fetching, a Steam row appears in Downloads (read-only: no Pause / Cancel buttons)."},
            {"run": {"game": "<name of a fully installed game>"}, "expect": "ok: true, already_installed: true, nothing started, no Steam window."},
            {"run": {"game": "<a word that matches two of your games>"}, "expect": "a question listing both with their app ids; nothing started."},
            {"run": {"game": "999999999"}, "expect": "launched, but Steam shows nothing useful or an error of its own; Jarvis says it saw no change. No row appears."},
            {"run": {"game": "<an app id>"}, "expect": "With Steam closed: ok: false, \"Steam isn't running. Start Steam and sign in, then ask again; nothing was started.\""},
        ],
        "needs": ["Steam running and signed in", "a game on your account that is not installed"],
        "watch": ["⚠️ Never run against a real Steam by the agent that wrote it: tell what Steam really did.", "Pause / cancel stay in Steam: the row has no such buttons.", "From a remote chat guest it is refused before anything happens: 'steam_install_game is the owner's: someone in a chat can't use it'; a guest is not even offered the tool."],
        "care": "Starts a download of possibly many GB (Steam decides the library and asks you).",
    },
    "steam_update_game": {
        "does": "Asks Steam to look for and fetch an update for ONE installed game.",
        "steps": [
            {"run": {"game": "<name of an installed game>"}, "expect": "asks first; after confirming: launched. Steam either starts an update (a Steam row, subtitle 'update') or tells you in its own window that it is up to date. Write down which."},
            {"run": {"game": "all"}, "expect": "ok: false, \"'update all' isn't available\": nothing started."},
            {"run": {"game": "<a game that is not installed>"}, "expect": "a question: it isn't among the installed games; nothing started."},
        ],
        "needs": ["Steam running and signed in", "one installed game"],
        "watch": ["⚠️ What Steam does on steam://install/ for an already installed game was never measured: report it.", "From a remote chat guest it is refused before anything happens: 'steam_update_game is the owner's: someone in a chat can't use it'; a guest is not even offered the tool."],
        "care": "May start a large download.",
    },
}
