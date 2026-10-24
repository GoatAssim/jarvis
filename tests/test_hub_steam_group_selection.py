"""Download Hub, brief 6F: the `steam` tool group is picked by the router, and a chat guest is not offered it.

Covers (against the REAL tool_router.route and tools.schemas_for_tools; no fakes for the router):
  * a message about Steam's library, installing a Steam game or updating one selects the `steam` group
    and the keyword hit that activated it belongs to the right tool (steam_library / steam_install_game /
    steam_update_game); the group brings all three tools along (the router offers whole groups);
  * messages that are about something else (a video download, a Stremio episode, a Playnite list, a greeting,
    an empty message) do not select the `steam` group and offer no Steam tool;
  * the three tools are in tools.OWNER_ONLY_TOOLS: a chat guest (and a sender with no is_owner flag) has all
    three in hidden_from_sender(), and none of them is in the schemas that route(...).tools turns into for a
    guest; the owner on a chat platform and a context with no chat at all have none hidden and get all three;
  * the confirm list still holds: steam_install_game and steam_update_game are in TOOL_CONFIRM_REQUIRED (and in
    tool_safety.DEFAULT_CONFIRM_REQUIRED, where tools.py folds them), steam_library is not.

This runs the keyword scorer only.  It never starts a Steam, never opens a steam:// link, never calls a model and
never talks to a real chat guest: what a guest or the model really does with the offer is not tried here.

Run: python tests/test_hub_steam_group_selection.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-steam-group-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER",
             "JARVIS_TOOL_MAKER_CONTEXT_FILE", "JARVIS_STEAM_ROOT"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_registry, tool_router, tool_safety, tools  # noqa: E402
from jarvis.actions import steam_tools  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}{': ' + str(detail) if detail else ''}")


STEAM_TOOLS = ("steam_library", "steam_install_game", "steam_update_game")

GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
GUEST_NO_FLAG = json.dumps({"platform": "discord", "user_id": "42"})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})


def set_sender(raw):
    if raw is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = raw


def steam_hits(result):
    """The (tool, phrase) pairs of the keyword hits that activated the steam group."""
    return [(name, phrase) for group, name, phrase in result.matches if group == "steam"]


# ------------------------------------------------------------------ the group as the registry knows it
check("the steam group holds exactly the three Steam tools",
      sorted(tool_registry.TOOL_GROUPS.get("steam", [])) == sorted(STEAM_TOOLS), tool_registry.TOOL_GROUPS.get("steam"))
check("each Steam tool has keywords in the registry and belongs to the steam group",
      all(tool_registry.TOOL_KEYWORDS.get(n) and tool_registry.group_of(n) == "steam" for n in STEAM_TOOLS))
check("the module declares the same group and the same three tools",
      steam_tools.TOOL_GROUP == "steam" and sorted(steam_tools.TOOLS) == sorted(STEAM_TOOLS))

# ------------------------------------------------------------------ messages about Steam select the group
SELECTS = (
    ("which steam games do I have installed", "steam_library"),
    ("install the steam game Portal 2", "steam_install_game"),
    ("update the steam game Portal 2", "steam_update_game"),
    ("show me my steam library", "steam_library"),
    ("update it on steam", "steam_update_game"),
    ("install on steam please", "steam_install_game"),
)
for message, want_tool in SELECTS:
    r = tool_router.route(message)
    hits = steam_hits(r)
    check(f"{message!r}: the steam group is selected", "steam" in r.groups and r.confident, r)
    check(f"{message!r}: the keyword that activated it is {want_tool}'s",
          bool(hits) and {name for name, _ in hits} == {want_tool}, hits)
    check(f"{message!r}: all three Steam tools are in the routed tools", set(STEAM_TOOLS) <= set(r.tools), r.tools)

# ------------------------------------------------------------------ messages about something else do not
NOT_STEAM = (
    "download this video",
    "play episode 3 of the show",
    "what's on my playnite list",
    "hi",
    "",
    "   ",
)
for message in NOT_STEAM:
    r = tool_router.route(message)
    check(f"{message!r}: the steam group is not selected", "steam" not in r.groups, r.groups)
    check(f"{message!r}: no Steam tool is routed", not (set(STEAM_TOOLS) & set(r.tools)), r.tools)
    check(f"{message!r}: no keyword hit belongs to the steam group", steam_hits(r) == [], steam_hits(r))

# ------------------------------------------------------------------ the owner-only gate: what a guest is offered
for name in STEAM_TOOLS:
    check(f"{name} is in OWNER_ONLY_TOOLS", name in tools.OWNER_ONLY_TOOLS)

ROUTED = tool_router.route("install the steam game Portal 2").tools


def offered_steam(routed):
    return {s["name"] for s in tools.schemas_for_tools(routed)} & set(STEAM_TOOLS)


try:
    for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
        set_sender(raw)
        hidden = tools.hidden_from_sender()
        check(f"{label}: all three Steam tools are hidden", set(STEAM_TOOLS) <= hidden, sorted(hidden))
        check(f"{label}: none of the routed Steam tools becomes a schema",
              set(STEAM_TOOLS) <= set(ROUTED) and offered_steam(ROUTED) == set(), offered_steam(ROUTED))
    for label, raw in (("the owner on a chat platform", OWNER), ("no chat context (the PC, a scheduled job)", None)):
        set_sender(raw)
        hidden = tools.hidden_from_sender()
        check(f"{label}: none of the Steam tools is hidden", not (set(STEAM_TOOLS) & hidden), sorted(hidden))
        check(f"{label}: all three routed Steam tools become schemas", offered_steam(ROUTED) == set(STEAM_TOOLS),
              offered_steam(ROUTED))
finally:
    set_sender(None)

# ------------------------------------------------------------------ the confirm list
check("steam_install_game and steam_update_game are confirmed with the owner before every call",
      {"steam_install_game", "steam_update_game"} <= set(steam_tools.TOOL_CONFIRM_REQUIRED))
check("steam_library (read-only) is not in the confirm list", "steam_library" not in steam_tools.TOOL_CONFIRM_REQUIRED)
check("the confirm list holds exactly those two", set(steam_tools.TOOL_CONFIRM_REQUIRED) == {"steam_install_game", "steam_update_game"})
check("tools.py folded them into tool_safety.DEFAULT_CONFIRM_REQUIRED the same way",
      {"steam_install_game", "steam_update_game"} <= tool_safety.DEFAULT_CONFIRM_REQUIRED
      and "steam_library" not in tool_safety.DEFAULT_CONFIRM_REQUIRED)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
