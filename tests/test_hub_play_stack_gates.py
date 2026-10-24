"""Brief 7A: play_local, play_episode, stack_start, stack_stop and server_cache_clear are owner-only.

A chat guest who is allowed tools could otherwise ask them to open a player or a browser, or to start and
stop programs, on the owner's PC.  Covers:
  * the five names are in tools.OWNER_ONLY_TOOLS, and they are real tools (loaded, in the registry);
  * a chat guest (and a sender with no is_owner flag) is not OFFERED them: hidden_from_sender() lists them,
    schemas_for_tools hands no schema, get_tool_schema says owner-only, search_tools does not list them;
  * the owner on a chat platform, and no chat context at all (the PC, a scheduled job), keep all five;
  * nothing else of play_media.py / stremio_stack.py was gated: stack_status, stack_doctor, server_cache and
    stremio_prepare are NOT in the set, and a guest is still offered them (read-only, or confirm-gated by
    name); play_control stays in the set as before;
  * the Test Checklist entries of the five say owner only, and none says a guest is offered them.

What this file does NOT check, on purpose: that the handlers of the five refuse a guest.  They do not.  Only
play_control's handler checks who is asking (play_media._is_chat_guest); the five are hidden from a guest, not
refused.  The report (docs/reports/SMALL_FIXES_GATES_AND_LEVELS.md, Open points) gives the one-line fix.  A
pin that "the handler refuses" would fail today; a pin that "it does not" would block that fix.  The one
handler check kept here is play_control's, to show the gate that does exist still works.

Run on Linux against the tool registry only; nothing is started, opened or sent.
Run: python tests/test_hub_play_stack_gates.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-play-stack-gates-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis import tool_registry, tools  # noqa: E402
from jarvis.actions import play_media, stremio_stack  # noqa: E402
from jarvis.hub.stremio import mpv  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
GUEST_NO_FLAG = json.dumps({"platform": "discord", "user_id": "42"})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})

FIVE = ("play_local", "play_episode", "stack_start", "stack_stop", "server_cache_clear")
NOT_GATED = ("stack_status", "stack_doctor", "server_cache", "stremio_prepare")


def set_sender(raw):
    if raw is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = raw


def offered(name):
    """The tool names search_tools lists for `name`."""
    return [m["name"] for m in tools.tool_search_tools({"query": name}).get("matches", [])]


# ------------------------------------------------------------------ the set
for name in FIVE:
    check(f"{name} is in OWNER_ONLY_TOOLS", name in tools.OWNER_ONLY_TOOLS)
    check(f"{name} is a real tool (loaded and in the registry)", name in tools.TOOLS and name in tool_registry.TOOL_INDEX)
check("play_control is still in the set", "play_control" in tools.OWNER_ONLY_TOOLS)
for name in NOT_GATED:
    check(f"{name} is a real tool", name in tools.TOOLS and name in tool_registry.TOOL_INDEX)
    check(f"{name} is NOT in OWNER_ONLY_TOOLS (read-only, or confirm-gated by name)", name not in tools.OWNER_ONLY_TOOLS)
check("every tool of play_media.py and stremio_stack.py is either in the set or listed here as not gated",
      set(play_media.TOOLS) | set(stremio_stack.TOOLS) == set(FIVE) | set(NOT_GATED) | {"play_control"})
check("the read-only hub tools are still open to a guest", not ({"dl_list", "dl_status"} & tools.OWNER_ONLY_TOOLS))

# ------------------------------------------------------------------ a guest is not offered the five
for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
    set_sender(raw)
    hidden = tools.hidden_from_sender()
    for name in FIVE:
        check(f"{label}: hidden_from_sender() lists {name}", name in hidden)
        check(f"{label}: schemas_for_tools hands no {name} schema", tools.schemas_for_tools([name]) == [])
        check(f"{label}: get_tool_schema says {name} is owner-only", "owner-only" in json.dumps(tools.tool_get_tool_schema({"name": name})))
        check(f"{label}: search_tools does not list {name}", name not in offered(name))
    for name in NOT_GATED:
        check(f"{label}: {name} is still offered (not hidden)", name not in hidden)
        got = tools.schemas_for_tools([name])
        check(f"{label}: schemas_for_tools still hands the {name} schema", [s.get("name") for s in got] == [name])
set_sender(None)

# ------------------------------------------------------------------ the owner and a no-chat context keep them
for label, raw in (("the owner on a chat platform", OWNER), ("no chat context (the PC, a scheduled job)", None)):
    set_sender(raw)
    hidden = tools.hidden_from_sender()
    for name in FIVE:
        check(f"{label}: {name} is not hidden", name not in hidden)
        got = tools.schemas_for_tools([name])
        check(f"{label}: schemas_for_tools hands the {name} schema", [s.get("name") for s in got] == [name])
        check(f"{label}: get_tool_schema does not call {name} owner-only", "owner-only" not in json.dumps(tools.tool_get_tool_schema({"name": name})))
        check(f"{label}: search_tools lists {name}", name in offered(name))
set_sender(None)

# ------------------------------------------------------------------ the one handler gate that exists still works
sent = []
_orig_mpv_run = mpv.run
mpv.run = lambda *a, **k: (sent.append(a) or {"ok": True, "done": "paused"})
try:
    set_sender(GUEST)
    res = play_media.tool_play_control({"action": "pause"})
    check("play_control's handler still refuses a guest and sends nothing to mpv",
          res.get("ok") is False and "owner" in res.get("error", "") and sent == [])
finally:
    mpv.run = _orig_mpv_run
    set_sender(None)

# ------------------------------------------------------------------ the checklist says so
for module, name in ((play_media, "play_local"), (play_media, "play_episode"), (stremio_stack, "stack_start"),
                     (stremio_stack, "stack_stop"), (stremio_stack, "server_cache_clear")):
    text = json.dumps(module.TEST_CHECKLIST[name])
    check(f"the {name} checklist entry says owner only (OWNER_ONLY_TOOLS)", "Owner only" in text and "OWNER_ONLY_TOOLS" in text)
    check(f"the {name} checklist entry does not say a guest is offered it", "OFFERED" not in text and "guest is offered" not in text)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
