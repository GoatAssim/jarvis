"""Download Hub, round 4 (brief 20): guest gates that fail closed, one pack instruction, the Steam doctor line.

Covers:
  * bulk_plan, bulk_run, stremio_library and stremio_mark_watched refuse a chat guest BEFORE doing
    anything (a recording stand-in for the work proves nothing downstream ran), also for an unreadable
    sender (bad JSON, JSON that is not an object, JSON nested too deep to read); the refusal is one plain
    sentence naming the tool and saying it is the owner's; the owner (on a chat platform, and with no chat
    context at all) is let through, as before.
  * the two copies of the guest rule behave exactly like download_hub._is_chat_guest on the same inputs.
  * (brief 31) play_control is in OWNER_ONLY_TOOLS too, hidden from a guest, and its handler still refuses one.
  * (brief 6F) steam_library, steam_install_game and steam_update_game are in OWNER_ONLY_TOOLS too, real tools, hidden
    from a guest by hidden_from_sender() (not from the owner, not with no chat context), and their handlers still refuse one.
  * the four names are in tools.OWNER_ONLY_TOOLS; tools.hidden_from_sender() hides them, and every loaded
    dl_add_<id> tool, from a guest (still a plain frozenset, nothing for the owner, nothing for no chat).
  * the "downloads" group has exactly ONE pack instruction, in download_hub.py, not longer than the two old
    ones together, with every rule of both.
  * hub_doctor's Steam lines add "steam disk writes" only when psutil is missing and Steam is here (or has rows).

No network, no Steam, no Stremio.  Run: python tests/test_hub_guest_gates.py
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-gates-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_registry, tools  # noqa: E402
from jarvis.actions import bulk_download as bulk_tools  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.actions import stremio_discovery as disc  # noqa: E402
from jarvis.actions import play_media  # noqa: E402
from jarvis.actions import steam_tools  # noqa: E402
from jarvis.hub import steam  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import bulk as bulk_engine  # noqa: E402
from jarvis.hub.stremio import mpv  # noqa: E402
from jarvis.hub.stremio import discovery, ranking  # noqa: E402

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
UNREADABLE = ["{not json", "[1, 2]", '"just a string"', "42", "null", "[" * 200000]   # the last one is too deep to parse
FOUR = ("bulk_plan", "bulk_run", "stremio_library", "stremio_mark_watched")


def set_sender(raw):
    if raw is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = raw


# ------------------------------------------------------------------ a recording stand-in for "the work"
reached = []


class Spy:
    """Replaces what a handler would do after its gate; records that it was reached."""

    def __init__(self, label, result=None):
        self.label, self.result = label, result if result is not None else {"ok": True}

    def __call__(self, *a, **k):
        reached.append(self.label)
        return self.result


_orig = {
    "run_plan": bulk_engine.run_plan, "normalize_want": ranking.normalize_want,
    "net": disc._net, "library": discovery.library, "mark_watched": discovery.mark_watched,
}
bulk_engine.run_plan = Spy("run_plan")
ranking.normalize_want = Spy("normalize_want", (None, "stop here"))      # first thing bulk_plan does after the title check
disc._net = Spy("net", object())
discovery.library = Spy("library", [])
discovery.mark_watched = Spy("mark_watched")

CALLS = {
    "bulk_plan": lambda: bulk_tools.tool_bulk_plan({"title": "Some Show"}),
    "bulk_run": lambda: bulk_tools.tool_bulk_run({"plan_id": "plan-1"}),
    "stremio_library": lambda: disc.tool_stremio_library({"show": "all"}),
    "stremio_mark_watched": lambda: disc.tool_stremio_mark_watched({"id": "tt0000001", "apply": True}),
}
REACHES = {"bulk_plan": "normalize_want", "bulk_run": "run_plan", "stremio_library": "library",
           "stremio_mark_watched": "mark_watched"}

# ------------------------------------------------------------------ the four handlers refuse a guest
for tool, call in CALLS.items():
    for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
        set_sender(raw)
        reached.clear()
        r = call()
        check(f"{tool}: {label} is refused", r.get("ok") is False and isinstance(r.get("error"), str))
        check(f"{tool}: {label}: nothing behind the gate ran", reached == [])
    r = None
    set_sender(GUEST)
    r = call()
    err = r.get("error") or ""
    check(f"{tool}: the refusal names the tool, says it is the owner's, and is one plain sentence",
          err.startswith(tool) and "owner's" in err and "\n" not in err and err.count(".") == 0 and len(err) < 100)
    for raw in UNREADABLE:
        set_sender(raw)
        reached.clear()
        r = call()
        check(f"{tool}: an unreadable sender ({raw[:12]!r}...) is refused (fails closed)",
              r.get("ok") is False and "owner's" in r.get("error", "") and reached == [])

# ------------------------------------------------------------------ and the owner is not affected
for tool, call in CALLS.items():
    for label, raw in (("the owner on a chat platform", OWNER), ("no chat context (the PC, a scheduled job)", None)):
        set_sender(raw)
        reached.clear()
        r = call()
        check(f"{tool}: {label} gets past the gate", REACHES[tool] in reached and "owner's" not in json.dumps(r))
set_sender(None)
check("bulk_plan with nothing to go on still asks which show (owner, before any work)",
      bulk_tools.tool_bulk_plan({}).get("needs_clarification") is True)
check("bulk_run with no plan still asks which plan (owner)", bulk_tools.tool_bulk_run({}).get("needs_clarification") is True)
check("stremio_mark_watched with no id still asks which title (owner)",
      disc.tool_stremio_mark_watched({}).get("needs_clarification") is True)
check("stremio_library still checks its `show` argument (owner)",
      "show must be" in disc.tool_stremio_library({"show": "bogus"}).get("error", ""))

# restore the real functions
bulk_engine.run_plan, ranking.normalize_want = _orig["run_plan"], _orig["normalize_want"]
disc._net, discovery.library, discovery.mark_watched = _orig["net"], _orig["library"], _orig["mark_watched"]

# ------------------------------------------------------------------ the three copies of the rule agree
matrix = [None, "", "   ", GUEST, GUEST_NO_FLAG, OWNER, json.dumps({"platform": "discord", "user_id": "9", "is_owner": None}),
          json.dumps({"is_owner": False}), json.dumps({"platform": "x"}), json.dumps({"user_id": "x"}),
          "{not json", "[1]", '"s"', "42", "null", "true", json.dumps({})]
for raw in matrix:
    set_sender(raw)
    want = dh._is_chat_guest()
    check(f"the gate copies in bulk_download and stremio_discovery agree with download_hub on {str(raw)[:40]!r}",
          bulk_tools._is_chat_guest() == want and disc._is_chat_guest() == want)
set_sender(None)

# ------------------------------------------------------------------ the names, and what a guest is offered
for name in FOUR:
    check(f"{name} is in OWNER_ONLY_TOOLS", name in tools.OWNER_ONLY_TOOLS)
    check(f"{name} is a real tool", name in tools.TOOLS and name in tool_registry.TOOL_INDEX)

GEN = "dl_add_zzgenerated"
tool_registry.TOOL_INDEX[GEN] = {"name": GEN, "description": "zzgenerated fetch for the gate test",
                                 "parameters": {"type": "object", "properties": {}}}
tools.TOOLS[GEN] = lambda args: {"ok": True, "ran": True}
try:
    set_sender(GUEST)
    hidden = tools.hidden_from_sender()
    check("a guest does not get the four tools offered", set(FOUR) <= hidden)
    check("...nor a generated dl_add_<id> tool", GEN in hidden)
    check("...but keeps the read-only ones", "dl_list" not in hidden and "dl_status" not in hidden
          and "present_download_status" not in hidden)
    check("hidden_from_sender still returns a plain frozenset", type(hidden) is frozenset)
    check("get_tool_schema hands a guest no schema for a generated add tool (it says owner-only)",
          "owner-only" in json.dumps(tools.tool_get_tool_schema({"name": GEN})))
    found = [m["name"] for m in tools.tool_search_tools({"query": "zzgenerated"}).get("matches", [])]
    check("...and search_tools does not list it", GEN not in found)
    set_sender(OWNER)
    hidden = tools.hidden_from_sender()
    check("the owner on a chat platform has nothing hidden by it", not (set(FOUR) & hidden) and GEN not in hidden)
    found = [m["name"] for m in tools.tool_search_tools({"query": "zzgenerated"}).get("matches", [])]
    check("...and search_tools lists the generated tool", GEN in found)
    set_sender(None)
    check("no chat context hides nothing", GEN not in tools.hidden_from_sender() and not (set(FOUR) & tools.hidden_from_sender()))
    set_sender("{not json")
    check("tools._chat_sender_is_guest still treats an unreadable sender as the owner (pinned by test_l28b_followups; "
          "the handlers are the gate)", tools._chat_sender_is_guest() is False)
finally:
    set_sender(None)
    del tool_registry.TOOL_INDEX[GEN]
    del tools.TOOLS[GEN]

# ------------------------------------------------------------------ play_control (brief 31): owner-only, hidden from a guest, handler still refuses
check("play_control is in OWNER_ONLY_TOOLS", "play_control" in tools.OWNER_ONLY_TOOLS)
check("play_control is a real tool", "play_control" in tools.TOOLS and "play_control" in tool_registry.TOOL_INDEX)
check("play_episode and play_local are NOT added with it (brief 31 leaves them; see its report)",
      not ({"play_episode", "play_local"} & tools.OWNER_ONLY_TOOLS))
for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
    set_sender(raw)
    check(f"{label}: play_control is hidden by hidden_from_sender()", "play_control" in tools.hidden_from_sender())
for label, raw in (("the owner on a chat platform", OWNER), ("no chat context", None)):
    set_sender(raw)
    check(f"{label}: play_control is not hidden", "play_control" not in tools.hidden_from_sender())
set_sender(None)

_orig_mpv_run = mpv.run
mpv.run = Spy("mpv_run", {"ok": True, "done": "paused"})
try:
    for label, raw in [("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)] + [("an unreadable sender (%r...)" % r[:12], r) for r in UNREADABLE]:
        set_sender(raw)
        reached.clear()
        r = play_media.tool_play_control({"action": "pause"})
        check(f"play_control handler still refuses {label}, before anything is sent to mpv",
              r.get("ok") is False and "owner" in r.get("error", "") and reached == [])
    for label, raw in (("the owner on a chat platform", OWNER), ("no chat context", None)):
        set_sender(raw)
        reached.clear()
        r = play_media.tool_play_control({"action": "pause"})
        check(f"play_control handler lets {label} through", r.get("ok") is True and reached == ["mpv_run"])
finally:
    mpv.run = _orig_mpv_run
    set_sender(None)

# ------------------------------------------------------------------ the three Steam tools (brief 6F): owner-only, hidden from a guest, handlers still refuse
STEAM_TOOLS = ("steam_library", "steam_install_game", "steam_update_game")
STEAM_CALLS = {"steam_library": {}, "steam_install_game": {"game": "105600"}, "steam_update_game": {"game": "105600"}}
_steam_opened = []
_orig_open_url = steam.OPEN_URL
steam.OPEN_URL = lambda url: _steam_opened.append(url)     # nothing here ever starts a real Steam
try:
    for name in STEAM_TOOLS:
        check(f"{name} is in OWNER_ONLY_TOOLS", name in tools.OWNER_ONLY_TOOLS)
        check(f"{name} is a real tool", name in tools.TOOLS and name in tool_registry.TOOL_INDEX)
        for label, raw in (("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
            set_sender(raw)
            check(f"{label}: {name} is hidden by hidden_from_sender()", name in tools.hidden_from_sender())
        for label, raw in (("the owner on a chat platform", OWNER), ("no chat context", None)):
            set_sender(raw)
            check(f"{label}: {name} is not hidden", name not in tools.hidden_from_sender())
        for label, raw in [("a chat guest", GUEST), ("a sender with no is_owner flag", GUEST_NO_FLAG)] + [("an unreadable sender (%r...)" % r[:12], r) for r in UNREADABLE]:
            set_sender(raw)
            del _steam_opened[:]
            r = steam_tools.TOOLS[name](STEAM_CALLS[name])
            check(f"{name} handler still refuses {label}, before anything is opened in Steam",
                  r.get("ok") is False and "owner's" in r.get("error", "") and _steam_opened == [])
finally:
    steam.OPEN_URL = _orig_open_url
    set_sender(None)

# ------------------------------------------------------------------ one pack instruction for "downloads"
declarers = sorted(r.file for r in tools._AUTO_VALID if r.group == "downloads" and r.pack_instruction)
check("exactly one file declares a pack instruction for downloads: download_hub.py", declarers == ["download_hub.py"])
text = tool_registry.pack_instruction("downloads")
check("tool_registry.pack_instruction('downloads') is that one text", text == dh.TOOL_PACK_INSTRUCTION.strip())
OLD_HUB, OLD_CARD = 932, 360          # the lengths of the two texts this replaces (round 3)
check("it is not longer than the two old ones together (%d <= %d)" % (len(text), OLD_HUB + OLD_CARD), len(text) <= OLD_HUB + OLD_CARD)
src = (ROOT / "jarvis-cli" / "jarvis" / "actions" / "download_status_card.py").read_text(encoding="utf-8")
check("download_status_card.py has no TOOL_PACK_INSTRUCTION assignment", "TOOL_PACK_INSTRUCTION =" not in src and "TOOL_PACK_INSTRUCTION=" not in src)
RULES = {
    "hub: dl_add queues and returns at once": "returns at once",
    "hub: report progress with dl_list / dl_status": "dl_list / dl_status",
    "hub: start_at for a later start": "start_at",
    "hub: dl_schedule for one already queued": "dl_schedule",
    "hub: dl_add is the router and names the add tool": "router",
    "hub: dl_add_ytdlp for a video or audio page": "dl_add_ytdlp",
    "hub: one video per link": "one video per link",
    "hub: dl_add_stremio for a torrent, file_index picks a file": "file_index picks a file",
    "hub: no video options on dl_add": "Do not put video options on dl_add",
    "hub: ytdl_download for 'download this video now'": "ytdl_download",
    "hub: background=true queues here too": "background=true",
    "hub: torrents go through the Stremio server (stack_status, stremio_prepare)": "stack_status",
    "hub: dl_prefs with the id for per-download choices": "dl_prefs",
    "card: present_download_status(ids|group|query)": "present_download_status(ids|group|query)",
    "card: after dl_add": "after you queue with dl_add",
    "card: call it ONCE": "ONCE",
    "card: pass the ids dl_add returned, or the group id": "group id",
    "card: a live card with bar, speed, Pause/Cancel, Open when done": "Pause/Cancel, Open when done",
    "card: never call it again to refresh": "never call it again to 'refresh'",
    "card: read-only, changes nothing": "read-only",
}
for label, needle in RULES.items():
    check(f"the one text keeps a rule: {label}", needle in text)
check("the 'stremio_prepare' hint survived too", "stremio_prepare" in text)

# ------------------------------------------------------------------ the Steam doctor line (psutil missing)
steam_dir = Path(HOME) / "Steam"
(steam_dir / "steamapps").mkdir(parents=True)
(steam_dir / "steamapps" / "appmanifest_1.acf").write_text(
    '"AppState"\n{\n\t"appid"\t\t"1"\n\t"name"\t\t"Fixture"\n\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"Fixture"\n}\n', encoding="utf-8")
prov = steam.PROVIDER
real_psutil = sys.modules.get("psutil", "absent")


def with_psutil(present):
    if present:
        sys.modules["psutil"] = types.ModuleType("psutil")     # a stand-in: only its import matters to the doctor
    else:
        sys.modules["psutil"] = None                           # `import psutil` now raises ImportError


def names(lines):
    return [ln["name"] for ln in lines]


try:
    store = Store(Path(HOME) / "doctor.db")
    prov.root_override, prov.candidates = str(steam_dir), []
    with_psutil(False)
    lines = steam.doctor(store)
    extra = [ln for ln in lines if ln["name"] == "steam disk writes"]
    check("Steam here and psutil missing: one extra line", len(extra) == 1 and names(lines)[0] == "steam" and len(lines) == 2)
    check("...it says the disk-writes signal is unavailable, that a stall rests on the downloading folder alone, "
          "and what to install",
          extra and "disk writes" in extra[0]["detail"] and "unavailable" in extra[0]["detail"]
          and "downloading folder alone" in extra[0]["detail"] and "pip install psutil" in extra[0]["detail"]
          and "Python Jarvis uses" in extra[0]["detail"])
    check("...as a warning, after the normal line (which is unchanged)", extra[0]["status"] == "warn" and lines[0]["status"] == "ok"
          and "manifest" in lines[0]["detail"])
    with_psutil(True)
    check("psutil present: no extra line", names(steam.doctor(store)) == ["steam"])
    with_psutil(False)
    prov.root_override = str(Path(HOME) / "nowhere")
    check("no Steam on this PC and no Steam rows: no psutil line (the provider is idle)",
          names(steam.doctor(store)) == ["steam"] and "wasn't found" in steam.doctor(store)[0]["detail"])
    store.upsert_provider_row("steam", "620", {"title": "Portal 2", "state": "downloading"})
    check("no Steam found but a Steam row exists: the psutil line shows", "steam disk writes" in names(steam.doctor(store)))
    with_psutil(True)
    check("...and not when psutil is there", names(steam.doctor(store)) == ["steam"])
    # through hub_doctor
    from jarvis.hub import doctor as hub_doctor
    prov.root_override = str(steam_dir)
    with_psutil(False)
    rep = hub_doctor.run(store)
    check("hub_doctor carries the line and counts it as a warning (the hub still reports ok)",
          any(c["name"] == "steam disk writes" and c["status"] == "warn" for c in rep["checks"])
          and "steam disk writes" in rep["summary"] and rep["ok"] is True)
    with_psutil(True)
    check("hub_doctor has no such line with psutil", not any(c["name"] == "steam disk writes" for c in hub_doctor.run(store)["checks"]))
finally:
    prov.root_override = None
    prov.candidates = None
    if real_psutil == "absent":
        sys.modules.pop("psutil", None)
    else:
        sys.modules["psutil"] = real_psutil

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
