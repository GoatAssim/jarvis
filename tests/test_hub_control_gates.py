"""Download Hub, round 9 (agent 9B): the guest gate on every hub tool except dl_list, dl_status and the status card.

Until build 168 a Discord / Instagram chat guest was OFFERED dl_pause, dl_resume, dl_cancel, dl_schedule, dl_settings and
dl_prefs (and dl_retry, dl_open, dl_providers, dl_provider_prefs, dl_import_legacy, hub_doctor, hub_demo_provider), and their
handlers did not refuse it.  What this file holds:

  * every one of the 16 handlers (the 13 of actions/download_hub.py plus dl_updates, dl_updates_check and dl_updates_apply of
    actions/download_updates.py) refuses a chat guest, a sender with no is_owner flag and every unreadable sender
    (bad JSON, JSON that is not an object, JSON nested too deep to read) BEFORE it touches the service, the store,
    a provider, the doctor, the demo or the updates code.  A recording stand-in for each of those proves nothing ran
    (the call list stays empty), for every argument shape that could do something (a read, a write, a test notification);
  * the refusal is one plain sentence that starts with the tool name and says it is the owner's;
  * the owner (on a chat platform) and no chat context at all (the PC, a scheduled job) get past the gate;
  * the 16 names are in tools.OWNER_ONLY_TOOLS, are real tools, are hidden from a guest by tools.hidden_from_sender() and
    not offered by tools.schemas_for_tools(), get no schema from get_tool_schema for a guest, and are all offered to the owner;
  * a guest keeps dl_list, dl_status and present_download_status (nothing else of the group), and those three are not in the set;
  * the guest rule of download_hub and of download_updates agree on every input, JSON nested too deep included;
  * the Test Checklist entries no longer say "Known gap" and each of the 16 says "Owner only".

No network, no daemon, no Steam, no Stremio.  Run: python tests/test_hub_control_gates.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-control-gates-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_CHANNEL",
             "JARVIS_TASK_ID", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_registry, tools  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.actions import download_updates as du  # noqa: E402
from jarvis.hub import demo, doctor, legacy, updates  # noqa: E402
from jarvis.hub import store as store_mod  # noqa: E402

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
GUEST_IG = json.dumps({"platform": "instagram", "user_id": "7", "is_owner": False})
GUEST_NO_FLAG = json.dumps({"platform": "discord", "user_id": "42"})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
UNREADABLE = ["{not json", "[1, 2]", '"just a string"', "42", "null", "[" * 200000]   # the last one is too deep to parse
GUESTS = [("a Discord guest", GUEST), ("an Instagram guest", GUEST_IG), ("a sender with no is_owner flag", GUEST_NO_FLAG)] + \
         [("an unreadable sender (%r...)" % raw[:12], raw) for raw in UNREADABLE]
OWNERS = [("the owner on a chat platform", OWNER), ("no chat context (the PC, a scheduled job)", None)]

HUB_NAMES = ("dl_pause", "dl_resume", "dl_cancel", "dl_retry", "dl_schedule", "dl_open", "dl_settings", "dl_prefs",
             "dl_providers", "dl_provider_prefs", "dl_import_legacy", "hub_doctor", "hub_demo_provider")
UPDATE_NAMES = ("dl_updates", "dl_updates_check", "dl_updates_apply")
ALL_NAMES = HUB_NAMES + UPDATE_NAMES
GUEST_KEEPS = ("dl_list", "dl_status", "present_download_status")


def set_sender(raw):
    if raw is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = raw


# ------------------------------------------------------------------ a recording stand-in for "everything behind the gate"
reached = []


class Rec:
    """Stands in for the service: ANY method records its name and answers ok."""

    def __getattr__(self, name):
        def call(*a, **k):
            reached.append("svc." + name)
            return {"ok": True, "alive": True, "via": name}
        return call


REC = Rec()


class Spy:
    def __init__(self, label, result=None):
        self.label, self.result = label, result if result is not None else {"ok": True, "via": label}

    def __call__(self, *a, **k):
        reached.append(self.label)
        return self.result


class SpyStore:
    """Replaces hub.store.Store: building one is already 'touching the store'."""

    def __new__(cls, *a, **k):
        reached.append("Store()")
        return REC


_orig = {
    "svc": dh._svc, "Store": store_mod.Store, "doctor": doctor.run, "legacy": legacy.import_legacy,
    "demo": (demo.start, demo.stop, demo.clear, demo.status),
    "present": updates.present, "request_check": updates.request_check, "du_store": du._store,
    "request_apply": updates.request_apply, "dry_run": updates.dry_run,
}
dh._svc = lambda: (reached.append("_svc()"), REC)[1]
store_mod.Store = SpyStore
doctor.run = Spy("doctor.run")
legacy.import_legacy = Spy("legacy.import_legacy")
demo.start, demo.stop, demo.clear, demo.status = (Spy("demo.start"), Spy("demo.stop"), Spy("demo.clear"), Spy("demo.status"))
updates.present = Spy("updates.present", {"updates": [], "count": 0, "checked_at": None, "providers": [], "problems": [],
                                          "daemon_alive": True})
updates.request_check = Spy("updates.request_check", {"ok": True, "results": []})
updates.request_apply = Spy("updates.request_apply", {"ok": True, "queued": 0, "results": []})
updates.dry_run = Spy("updates.dry_run", {"ok": True, "plan": "a plan"})
du._store = Spy("du._store", REC)

# Every argument shape that could DO something, per tool (a guest must be refused for each of them).
SHAPES = {
    "dl_pause": [{"id": "abc"}, {"all": True}, {}],
    "dl_resume": [{"id": "abc"}, {"all": True}],
    "dl_cancel": [{"id": "abc"}, {"all": True}],
    "dl_retry": [{"id": "abc"}, {"provider": "steam"}],
    "dl_schedule": [{"id": "abc", "when": "tomorrow at 3am"}, {"all": True, "when": "clear"}, {"id": "abc"}],
    "dl_open": [{"id": "abc"}, {"id": "abc", "action": "reveal"}, {"target": "library"}],
    "dl_settings": [{}, {"set": {"concurrency.http": 2}}, {"set": {"library_root": "D:/Anywhere"}}],
    "dl_prefs": [{}, {"set": {"rate_limit_kbps": 2048}}, {"id": "abc", "set": {"limit_kbps": 500}}, {"test_notification": "done"}],
    "dl_providers": [{}],
    "dl_provider_prefs": [{"id": "steam", "enabled": False}, {"id": "steam", "settings": {"notify_min_update_gb": 2}}],
    "dl_import_legacy": [{}],
    "hub_doctor": [{}],
    "hub_demo_provider": [{"action": "start"}, {"action": "stop"}, {"action": "clear"}, {"action": "status"}],
    "dl_updates": [{}, {"provider": "steam"}],
    "dl_updates_check": [{}, {"provider": "steam"}],
    "dl_updates_apply": [{"provider": "steam", "id": "x"}, {"all": True}, {"all": True, "group": "mods"},
                         {"provider": "steam", "id": "x", "dry_run": True}, {}],
}
HANDLERS = {name: dh.TOOLS[name] for name in HUB_NAMES}
HANDLERS.update({name: du.TOOLS[name] for name in UPDATE_NAMES})
check("the 13 hub tools and the three update tools all have a handler to test", len(HANDLERS) == 16 and set(SHAPES) == set(ALL_NAMES))

# ------------------------------------------------------------------ a guest is refused by EVERY handler, before anything runs
try:
    for name in ALL_NAMES:
        for label, raw in GUESTS:
            set_sender(raw)
            refused_all = True
            nothing_ran = True
            sentence_ok = True
            for args in SHAPES[name]:
                del reached[:]
                r = HANDLERS[name](dict(args))
                refused_all = refused_all and isinstance(r, dict) and r.get("ok") is False and isinstance(r.get("error"), str)
                nothing_ran = nothing_ran and reached == []
                err = r.get("error") or ""
                sentence_ok = sentence_ok and err.startswith(name + " ") and "owner's" in err and "\n" not in err \
                    and err.count(".") == 0 and len(err) < 110
            check(f"{name}: {label} is refused (every argument shape)", refused_all)
            check(f"{name}: {label}: nothing behind the gate ran (no service, store, doctor, demo or updates call)", nothing_ran)
            check(f"{name}: {label}: the refusal is one plain sentence naming the tool and saying it is the owner's", sentence_ok)
        set_sender(GUEST)
        for junk in (None, "nonsense", [], 7):
            del reached[:]
            try:
                r = HANDLERS[name](junk)
            except Exception:  # noqa: BLE001
                r = None
            check(f"{name}: junk arguments from a guest are a refusal, not an exception ({str(junk)!r})",
                  isinstance(r, dict) and r.get("ok") is False and reached == [])
    set_sender(None)

    # -------------------------------------------------------------- and the owner is not affected
    OWNER_ARGS = {
        "dl_pause": {"id": "abc"}, "dl_resume": {"id": "abc"}, "dl_cancel": {"id": "abc"}, "dl_retry": {"id": "abc"},
        "dl_schedule": {"id": "abc", "when": "clear"}, "dl_open": {"id": "abc"}, "dl_settings": {},
        "dl_prefs": {"set": {"rate_limit_kbps": 2048}}, "dl_providers": {},
        "dl_provider_prefs": {"id": "steam", "enabled": True}, "dl_import_legacy": {}, "hub_doctor": {},
        "hub_demo_provider": {"action": "status"}, "dl_updates": {}, "dl_updates_check": {},
        "dl_updates_apply": {"provider": "steam", "id": "x"},
    }
    for name in ALL_NAMES:
        for label, raw in OWNERS:
            set_sender(raw)
            del reached[:]
            r = HANDLERS[name](dict(OWNER_ARGS[name]))
            check(f"{name}: {label} gets past the gate and the work is reached",
                  bool(reached) and "owner's" not in json.dumps(r))
    set_sender(None)
finally:
    set_sender(None)
    dh._svc = _orig["svc"]
    store_mod.Store = _orig["Store"]
    doctor.run = _orig["doctor"]
    legacy.import_legacy = _orig["legacy"]
    demo.start, demo.stop, demo.clear, demo.status = _orig["demo"]
    updates.present, updates.request_check, du._store = _orig["present"], _orig["request_check"], _orig["du_store"]
    updates.request_apply, updates.dry_run = _orig["request_apply"], _orig["dry_run"]

# ------------------------------------------------------------------ the two copies of the guest rule agree, deep JSON included
matrix = [None, "", "   ", GUEST, GUEST_IG, GUEST_NO_FLAG, OWNER, json.dumps({"platform": "discord", "user_id": "9", "is_owner": None}),
          json.dumps({"is_owner": False}), json.dumps({"platform": "x"}), json.dumps({"user_id": "x"}), json.dumps({}),
          "true", *UNREADABLE]
for raw in matrix:
    set_sender(raw)
    a, b = dh._is_chat_guest(), du._is_chat_guest()
    check(f"download_hub and download_updates agree on {str(raw)[:30]!r}", a == b)
set_sender("[" * 200000)
check("JSON nested too deep to parse counts as a guest in download_hub (it used to raise)", dh._is_chat_guest() is True)
set_sender(None)

# ------------------------------------------------------------------ the names: owner-only, real, hidden, not offered
for name in ALL_NAMES:
    check(f"{name} is in OWNER_ONLY_TOOLS", name in tools.OWNER_ONLY_TOOLS)
    check(f"{name} is a real tool", name in tools.TOOLS and name in tool_registry.TOOL_INDEX)
for name in GUEST_KEEPS:
    check(f"{name} is NOT in OWNER_ONLY_TOOLS (a guest may look at the queue)", name not in tools.OWNER_ONLY_TOOLS)
    check(f"{name} is a real tool", name in tools.TOOLS and name in tool_registry.TOOL_INDEX)

for label, raw in (("a Discord guest", GUEST), ("an Instagram guest", GUEST_IG), ("a sender with no is_owner flag", GUEST_NO_FLAG)):
    set_sender(raw)
    hidden = tools.hidden_from_sender()
    check(f"{label}: all 16 are hidden by hidden_from_sender()", set(ALL_NAMES) <= hidden)
    check(f"{label}: dl_list, dl_status and present_download_status are not", not (set(GUEST_KEEPS) & hidden))
    offered = [s["name"] for s in tools.schemas_for_tools(list(ALL_NAMES) + list(GUEST_KEEPS))]
    check(f"{label}: schemas_for_tools offers the three read-only tools and none of the 16", sorted(offered) == sorted(GUEST_KEEPS))
    schema = tools.tool_get_tool_schema({"name": "dl_pause"})
    check(f"{label}: get_tool_schema('dl_pause') says owner-only and hands back no schema",
          "owner-only" in (schema.get("error") or "") and "schema" not in schema)
    found = [m["name"] for m in tools.tool_search_tools({"query": "pause the download"}).get("matches", [])]
    check(f"{label}: search_tools('pause the download') does not list the owner-only tools", not (set(ALL_NAMES) & set(found)))
for label, raw in OWNERS:
    set_sender(raw)
    check(f"{label}: nothing of the hub is hidden", not (set(ALL_NAMES + GUEST_KEEPS) & tools.hidden_from_sender()))
    offered = [s["name"] for s in tools.schemas_for_tools(list(ALL_NAMES) + list(GUEST_KEEPS))]
    check(f"{label}: all 19 are offered", sorted(offered) == sorted(ALL_NAMES + GUEST_KEEPS))
    found = [m["name"] for m in tools.tool_search_tools({"query": "pause the download"}).get("matches", [])]
    check(f"{label}: search_tools finds dl_pause", "dl_pause" in found)
set_sender("{not json")
check("tools._chat_sender_is_guest still treats an unreadable sender as the owner on the OFFER side (pinned by "
      "test_l28b_followups; the handlers above are the gate and they fail closed)", tools._chat_sender_is_guest() is False)
set_sender(None)

# ------------------------------------------------------------------ the checklist text
for name in HUB_NAMES:
    entry = dh.TEST_CHECKLIST.get(name) or {}
    text = json.dumps(entry, ensure_ascii=False)
    check(f"the {name} checklist entry says 'Owner only' and has no 'Known gap'", "Owner only" in text and "Known gap" not in text)
    check(f"...and its 'watch' is a list of strings", isinstance(entry.get("watch"), list) and all(isinstance(w, str) for w in entry["watch"]))
for name in UPDATE_NAMES:
    text = json.dumps(du.TEST_CHECKLIST.get(name) or {}, ensure_ascii=False)
    check(f"the {name} checklist entry says 'Owner only' and has no 'Known gap'", "Owner only" in text and "Known gap" not in text)
for path in ("download_hub.py", "download_updates.py", "download_add_http.py"):
    src = (ROOT / "jarvis-cli" / "jarvis" / "actions" / path).read_text(encoding="utf-8")
    check(f"{path} has no 'Known gap' text left", "Known gap" not in src)

# the neighbours that were already gated keep their gates (dl_add_http's handler uses the same rule)
from jarvis.actions import download_add_http as add_http  # noqa: E402
set_sender(GUEST)
check("dl_add_http still refuses a guest (it was gated before; the same rule)",
      add_http.tool_dl_add_http({"url": "https://example.com/a.bin"}).get("ok") is False)
set_sender("[" * 200000)
check("...also for JSON nested too deep to read", add_http.tool_dl_add_http({"url": "https://example.com/a.bin"}).get("ok") is False)
set_sender(None)
check("dl_add_http's checklist 'watch' is a list of strings (7D's first red check)",
      isinstance(add_http.TEST_CHECKLIST["dl_add_http"]["watch"], list)
      and all(isinstance(w, str) for w in add_http.TEST_CHECKLIST["dl_add_http"]["watch"]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
