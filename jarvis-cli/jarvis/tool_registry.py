"""Static registry of what Jarvis's tools are, grouped and keyworded.

This is Phase 1 of the token-optimization plan (see new_plan.md): a single
source of truth for tool grouping/keywords/workflow-instructions, built
directly from tools.TOOL_SCHEMAS so it can't silently drift out of sync
with the real tool catalog.

Nothing in ai_client.py's prompt-building path reads this module yet —
today, every tool schema still goes into every prompt exactly as before.
This module only makes the following possible for later phases:

    Phase 2 (tools.schemas_for_tools) — filter the full schema list down
             to a chosen set of names.
    Phase 3 (tool_router.py)          — score a user message against
             TOOL_KEYWORDS to guess which TOOL_GROUPS are relevant.
    Phase 4+                          — actually shrink what gets sent to
             the model based on that guess, and inject TOOL_PACK_INSTRUCTIONS
             only for active groups instead of _tools_blurb() covering
             every subsystem unconditionally.

Grouping is by *workflow*, not by "one Python file = one group" (see plan
section 13/14) — e.g. every desktop-automation tool (mouse, keyboard,
windows, screenshots, OCR) is one "desktop" group even though it's spread
across desktop_tools.py, screenshot_tools.py, and ocr_tools.py.
"""

from .tools import TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# TOOL_GROUPS — workflow groups. Every tool name in TOOL_SCHEMAS should
# appear in exactly one group; anything left over lands in "misc" (see the
# consistency check at the bottom of this module) rather than being
# silently dropped.
# ---------------------------------------------------------------------------

TOOL_GROUPS = {
    "core": [
        "get_datetime",
        "get_battery",
        "get_wifi_info",
        "get_location",
        "get_system_info",
        "get_disk_usage",
        "get_memory_usage",
    ],
    "memory": [
        "memory_save",
        "memory_forget",
        "memory_search",
    ],
    "capacity": [
        "get_capacity_mode",
        "set_capacity_mode",
    ],
    "commands": [
        "search_commands",
        "run_command",
        "run_chain",
        "create_command",
        "update_command",
        "run_custom_command",
    ],
    "desktop": [
        "type_text",
        "press_key",
        "hotkey",
        "scroll",
        "move_mouse",
        "click",
        "drag",
        "get_screen_size",
        "get_mouse_position",
        "list_windows",
        "focus_window",
        "get_active_window",
        "get_window_size",
        "get_window_info",
        "take_screenshot",
        "click_on_text",
    ],
    "files": [
        "search_files",
        "reveal_in_explorer",
        "open_file_location",
        "open_file",
        "write_file",
        "organize_json",
        "present_file",
    ],
    "web": [
        "web_search",
        "web_fetch",
    ],
    "youtube": [
        "ytdl_info",
        "ytdl_formats",
        "ytdl_download",
    ],
    "spotify": [
        "spotify_open",
        "spotify_now",
        "spotify_search",
        "spotify_play",
        "spotify_control",
        "spotify_queue",
        "spotify_playlists",
        "spotify_suggest",
        "spotify_like",
    ],
    "system_control": [
        "radio_status",
        "wifi_set",
        "bluetooth_set",
        "package_managers",
        "package_search",
        "package_info",
        "package_list",
        "package_install",
        "package_uninstall",
        "git_run",
    ],
    "playnite": [
        "playnite_list_game_actions",
        "playnite_launch_action",
        "playnite_find_game",
        "playnite_launch_game",
        "playnite_library_stats",
        "playnite_get_game",
        "playnite_update_game",
        "playnite_list_frequent",
        "playnite_delete_game",
        "playnite_get_action",
        "playnite_install_game",
        "playnite_uninstall_game",
        "playnite_manage_game_lists",
        "playnite_fetch_game_art",
        "playnite_list_missing_art",
        "playnite_query_games",
        "playnite_list_collections",
        "playnite_create_collection",
        "playnite_view",
        "playnite_app_info",
        "playnite_list_addons",
        "playnite_list_plugins",
        "playnite_notify",
        "playnite_auto_categorize",
        "playnite_fetch_all_art",
        "playnite_get_achievements",
        "playnite_get_activity",
        "playnite_get_cover",
        "playnite_eval",
        "playnite_rotate_token",
        "playnite_get_skill",
    ],
    # search_tools is the Phase 5 discovery tool (see tools.py) — it's
    # deliberately not offered alongside a normal workflow group, but it
    # still needs a home here so the consistency check below (and the
    # every-tool-is-grouped test) doesn't flag it as an orphaned tool that
    # someone forgot to register.
    "discovery": [
        "search_tools",
    ],
}

# ---------------------------------------------------------------------------
# TOOL_KEYWORDS — per-tool weighted keywords for the (future) local router.
# Not every tool needs entries here to start: the router only needs enough
# signal to place a message into the right *group* (see tool_router.py) —
# grouped tools without their own explicit keywords still get activated
# whenever a sibling tool in the same group scores highly enough.
#
# A value is normally a plain int weight. A phrase that's prone to false-
# positive overlap with an unrelated phrase (the same class of bug as
# "commanded" matching "command" — see tool_router.py's word-boundary fix,
# but for two *whole* phrases that can legitimately co-occur) can instead
# use {"weight": w, "not_with": [phrase, ...]}: the phrase still needs
# word-boundary weight >= MIN_SCORE to be considered, but tool_router.route()
# treats it as a non-match if any of its not_with terms also appear in the
# message. Use keyword_weight()/keyword_exclusions() to read either shape.
# ---------------------------------------------------------------------------

TOOL_KEYWORDS = {
    "get_battery": {"battery": 10, "charging": 8, "charge": 8, "power": 4},
    "get_wifi_info": {"wifi": 10, "wi-fi": 10, "network": 5, "wireless": 8, "ssid": 8},
    "get_location": {"location": 9, "where am i": 10, "gps": 6},
    "get_system_info": {"system info": 9, "hostname": 6, "os version": 6, "uptime": 6},
    "get_disk_usage": {"disk": 9, "storage": 7, "disk space": 10, "free space": 8},
    "get_memory_usage": {"ram": 9, "memory usage": 10, "memory": 4},
    "get_datetime": {"time": 6, "date": 6, "what time": 10, "today's date": 10},

    "memory_search": {"remember": 8, "memory": 6, "recall": 8, "what did i tell you": 10},
    "memory_save": {"remember this": 10, "save this": 6, "note that": 6},

    "search_commands": {"command": 6, "saved command": 10, "run my": 6},
    "run_command": {"run": 4, "run my": 8, "start server": 6},
    "run_chain": {"chain": 6, "run these commands": 8},

    "type_text": {"type": 6, "keyboard": 5},
    "press_key": {"press": 5, "key": 4},
    "hotkey": {"hotkey": 9, "shortcut": 7},
    "take_screenshot": {"screenshot": 10, "screen shot": {"weight": 10, "not_with": ["recording", "record"]}, "capture screen": 8},
    "click": {"discord": 8, "click": 6},
    "click_on_text": {"click on": 6, "ocr": 8},
    "list_windows": {"windows": 5, "open windows": 8},
    "focus_window": {"focus": 5, "switch to window": 8, "window": 5, "chrome": 6, "firefox": 6},

    "search_files": {
        "find file": 10, "search file": 10, "locate file": 8,
        "find files": 10, "search files": 10, "locate files": 8,
        "find a file": 10, "find the file": 10, "where is": 6,
        "files in": 7, "list files": 8, "list of files": 8,
        "folder": 5, "directory": 5, ".exe": 6, "ext:": 8,
    },
    "open_file": {"open file": 9},
    "write_file": {"write file": 9, "create file": 7, "save file": 6},

    "web_search": {"search the web": 10, "google": 6, "look up": 6},
    "web_fetch": {"fetch": 5, "open url": 7, "read this page": 7, "this link": 6},

    "ytdl_info": {"youtube": 8, "video info": 6},
    "ytdl_formats": {"youtube formats": 9, "video format": 6},
    "ytdl_download": {"download video": 10, "download youtube": 10, "download this video": 10},

    "spotify_search": {"spotify": 10, "song": 6, "track": 5, "artist": 5, "playlist": 6, "music": 5},
    "spotify_play": {"play": 4, "spotify": 8, "play this song": 8},
    "spotify_open": {"open spotify": 10},
    "spotify_control": {"pause": 6, "skip": 6, "next song": 6, "volume": 4},
    "spotify_queue": {"queue": 8, "queue up": 8},
    "spotify_playlists": {"my playlists": 9, "playlists": 6},

    "radio_status": {"radio": 6, "bluetooth": 6, "wifi status": 6},
    "wifi_set": {"turn on wifi": 10, "turn off wifi": 10, "enable wifi": 9, "disable wifi": 9},
    "bluetooth_set": {"turn on bluetooth": 10, "turn off bluetooth": 10, "enable bluetooth": 9},
    "package_search": {"package": 6, "install": 4, "is there a package": 8},
    "package_install": {"install": 6, "install package": 10},
    "git_run": {"git": 10, "repository": 7, "repo": 6, "commit": 7, "branch": 6, "push": 5, "pull": 4, "diff": 5},

    "playnite_find_game": {"playnite": 8, "game": 5},
    "playnite_launch_game": {"launch": 6, "play game": 8, "start game": 8},
    "playnite_library_stats": {"game library": 8, "how many games": 8},
    "playnite_list_frequent": {"frequent games": 9, "games i play most": 8},
}

# ---------------------------------------------------------------------------
# TOOL_PACK_INSTRUCTIONS — short, group-specific workflow guidance. This is
# exactly the content _tools_blurb() currently bakes into *every* prompt
# regardless of whether the group is relevant (see plan section 26/27) —
# defined here now so a later phase can inject only the active group's
# instructions instead. _tools_blurb() is untouched for now.
# ---------------------------------------------------------------------------

TOOL_PACK_INSTRUCTIONS = {
    "web": (
        "Web: for anything that may have changed (prices, news, best-X, "
        "how-tos), web_search then web_fetch 1-3 URLs, then summarize with "
        "markdown source links."
    ),
    "spotify": (
        "Spotify: search before play unless a URI is already known. Never claim "
        "playback started unless spotify_play/spotify_control actually succeeded."
    ),
    "youtube": (
        "Video: use ytdl_info first; use ytdl_formats when an exact format is "
        "needed; downloads should be confirmed with the user first."
    ),
    "system_control": (
        "Git: git_run only, destructive operations need confirmation. "
        "Packages: search/info before install; confirm the exact manager and "
        "package name before installing or uninstalling anything."
    ),
    "files": (
        "Search before opening/writing when the exact path isn't already known."
    ),
    "desktop": (
        "Desktop automation acts on the real mouse/keyboard/screen — confirm "
        "before anything destructive or hard to undo. get_active_window, "
        "get_window_info, and get_window_size all overlap — call ONE of "
        "them (or none, if list_windows/focus_window's own result already "
        "told you what you need), never several back-to-back on the same "
        "window. To click a button/label by what it says (e.g. 'Call', "
        "'Send'), use click_on_text directly — it screenshots, finds the "
        "text, and clicks it in one call. Don't get a window's size/"
        "position just to guess x/y for a plain click; that's slower and "
        "more brittle than click_on_text."
    ),
    "commands": (
        "Prefer search_commands over guessing a saved command's exact name."
    ),
    "playnite": (
        "Look a game up (playnite_find_game / playnite_query_games) before "
        "acting on it rather than guessing an id."
    ),
}

# ---------------------------------------------------------------------------
# TOOL_INDEX — name -> schema, O(1) lookup. Built once from the real schema
# list so it's always exactly what the model would actually be offered.
# ---------------------------------------------------------------------------

TOOL_INDEX = {schema["name"]: schema for schema in TOOL_SCHEMAS if schema.get("name")}


def group_of(name):
    """Which TOOL_GROUPS key a tool name belongs to, or None."""
    for group, names in TOOL_GROUPS.items():
        if name in names:
            return group
    return None


def tools_in_group(group):
    """Tool names in a group, or [] for an unknown group."""
    return list(TOOL_GROUPS.get(group, []))


def keywords_for(name):
    """The {keyword: weight} dict for a tool, or {} if it has none defined."""
    return dict(TOOL_KEYWORDS.get(name, {}))


def keyword_weight(value):
    """Unwrap a TOOL_KEYWORDS entry's value into its weight. Most entries
    are a plain int; a phrase that also carries an exclusion list uses
    {"weight": w, "not_with": [...]} instead — see keyword_exclusions()."""
    if isinstance(value, dict):
        return value.get("weight", 0)
    return value


def keyword_exclusions(value):
    """The phrase's not_with list (terms that, if also present, cancel
    this phrase's match), or [] for a plain-int entry with none."""
    if isinstance(value, dict):
        not_with = value.get("not_with")
        if isinstance(not_with, list):
            return not_with
    return []


def pack_instruction(group):
    """The short workflow instruction for a group, or '' if it has none."""
    return TOOL_PACK_INSTRUCTIONS.get(group, "")


def _ungrouped_tool_names():
    """Tool names present in TOOL_INDEX but missing from every TOOL_GROUPS
    list — used only by tests/consistency checks, not at runtime, so a new
    tool file that forgets to register itself here fails loudly instead of
    just silently never being routable."""
    grouped = {name for names in TOOL_GROUPS.values() for name in names}
    return sorted(set(TOOL_INDEX) - grouped)