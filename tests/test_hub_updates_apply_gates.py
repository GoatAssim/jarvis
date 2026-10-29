"""Download Hub, the Updates rework (H1): who can reach an apply, and what the hub never does.

The rule this file holds: the hub never builds an install command; only a provider's apply tool changes the machine; a guest
reaches no apply path; auto-update is off by default.

Covers:
  * the three new admin ops (and the two old writes) refuse an unattended process (a chat reply, a scheduled job, a
    sub-agent) BEFORE anything is queued, planned, called or changed;
  * `dl_updates_apply` refuses a chat guest, a sender with no is_owner flag and every unreadable sender before it touches the
    store or the updates code, for every argument shape; the owner and a no-chat context get through; junk arguments from the
    owner are an answer, not an exception; with the daemon down it queues nothing;
  * `dl_updates_apply` and the demo's apply tool are confirm-gated by name; a declared apply tool (the demo's, a custom tool's)
    and the twelve pre-allocated Updates names are hidden from a guest and offered to the owner;
  * nothing in the hub's update code can start a program or build a command (no subprocess, os.system, shell=True);
  * auto-update is off in a fresh store, and the model's paths cannot switch it on.

Run: python tests/test_hub_updates_apply_gates.py
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-upd-apply-gates-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["JARVIS_HUB_NO_UPDATES"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
UNATTENDED_VARS = ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT")
for _var in UNATTENDED_VARS + ("JARVIS_ALLOWED_TOOLS", "JARVIS_TOOL_MAKER_CONTEXT_FILE"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_registry, tool_safety, tools  # noqa: E402
from jarvis.actions import download_updates as du  # noqa: E402
from jarvis.hub import settings as settings_mod, updates, updates_admin  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0


def check(name, cond, detail=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}" + (f"   [{detail!r}]" if detail is not None else ""))


GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
GUEST_IG = json.dumps({"platform": "instagram", "user_id": "7", "is_owner": False})
GUEST_NO_FLAG = json.dumps({"platform": "discord", "user_id": "42"})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "is_owner": True})
UNREADABLE = ["{not json", "[1, 2]", '"just a string"', "42", "null", "[" * 200000]
GUESTS = [("a Discord guest", GUEST), ("an Instagram guest", GUEST_IG), ("a sender with no is_owner flag", GUEST_NO_FLAG)] + \
         [("an unreadable sender (%r...)" % raw[:12], raw) for raw in UNREADABLE]

TWELVE = ("dl_updates_apply", "check_pkg_updates", "apply_pkg_update", "check_steam_updates", "apply_steam_update",
          "check_mc_watched", "apply_mc_update", "mc_watch", "list_mc_replaced", "undo_mc_update", "inspect_modpack",
          "update_modpack_file")


def set_sender(raw):
    if raw is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = raw


reached = []


class Spy:
    def __init__(self, label, result):
        self.label, self.result = label, result

    def __call__(self, *a, **k):
        reached.append(self.label)
        return self.result


# --------------------------------------------------------------------------- the admin ops

def test_admin_ops_refuse_unattended():
    store = Store(Path(HOME) / "hub-gates-1.db", clock=time.time)
    store.register_provider("fetch", label="Fetch", mode="push")
    store.update_offer("fetch", "a", {"title": "Alpha", "installed": "1", "available": "2"})
    store.daemon_beat(os.getpid(), time.time())
    store.update_check_put("fetch", update_tool="check_fetch_updates", apply_tool="apply_fetch_update")
    saved = {name: getattr(updates, name) for name in ("request_apply", "dry_run", "request_check", "dismiss_finding")}
    for name in saved:
        setattr(updates, name, Spy("updates." + name, {"ok": True}))
    requests = {
        "updates_apply": [{"provider": "fetch", "id": "a"}, {"all": True}, {"all": True, "group": "packages"}],
        "updates_dry_run": [{"provider": "fetch", "id": "a"}],
        "updates_set": [{"scope": "global", "key": "auto_update", "value": True},
                        {"scope": "provider", "id": "fetch", "key": "auto_update", "value": True},
                        {"scope": "group", "id": "packages", "key": "auto_update", "value": True}],
        "updates_check": [{"provider": "fetch"}],
        "updates_dismiss": [{"provider": "fetch", "id": "a"}],
    }
    try:
        for var in UNATTENDED_VARS:
            os.environ[var] = "scheduled" if var == "JARVIS_CONTEXT" else "1"
            try:
                for op, shapes in requests.items():
                    for request in shapes:
                        del reached[:]
                        out = getattr(updates_admin, op)(dict(request), store=store)
                        check(f"{op} {sorted(request)} refuses an unattended process ({var})",
                              out.get("ok") is False and out.get("code") == "unattended" and reached == [], out)
            finally:
                os.environ.pop(var, None)
    finally:
        for name, fn in saved.items():
            setattr(updates, name, fn)
    check("nothing was queued by any of them", store.applying_info() == {})
    check("and the master switch is still off, no provider tick was set",
          settings_mod.update_global_settings(store.get_settings())["auto_update"] is False
          and settings_mod.provider_auto_update(store.get_provider("fetch")["settings"]) is False)

    for op in ("updates_apply", "updates_dry_run", "updates_set"):
        check(f"{op} is registered as a WRITE op of updates_admin and of hub_admin", op in updates_admin.WRITE_OPS)
    from jarvis import hub_admin
    check("hub_admin lists the three as writes (so its own unattended check covers them too)",
          {"updates_apply", "updates_dry_run", "updates_set"} <= set(hub_admin.WRITE_OPS))
    out = updates_admin.updates_apply({"provider": "fetch", "id": "a"}, store=store)
    check("an attended request is not refused for the wrong reason", out.get("code") != "unattended", out)


# --------------------------------------------------------------------------- the chat tool

SHAPES = [{"provider": "steam", "id": "x"}, {"all": True}, {"all": True, "group": "mods"},
          {"provider": "steam", "id": "x", "dry_run": True}, {}]


def test_chat_tool_guest_gate():
    orig = {"request_apply": updates.request_apply, "dry_run": updates.dry_run, "_store": du._store}
    updates.request_apply = Spy("updates.request_apply", {"ok": True, "queued": 0, "results": []})
    updates.dry_run = Spy("updates.dry_run", {"ok": True, "plan": "p"})
    du._store = Spy("du._store", object())
    try:
        for label, raw in GUESTS:
            set_sender(raw)
            refused = nothing = sentence = True
            for args in SHAPES:
                del reached[:]
                r = du.tool_dl_updates_apply(dict(args))
                refused = refused and isinstance(r, dict) and r.get("ok") is False and isinstance(r.get("error"), str)
                nothing = nothing and reached == []
                err = r.get("error") or ""
                sentence = sentence and err.startswith("dl_updates_apply ") and "owner's" in err and "\n" not in err
            check(f"dl_updates_apply: {label} is refused for every argument shape", refused)
            check(f"dl_updates_apply: {label}: nothing behind the gate ran (no store, no queue, no plan)", nothing)
            check(f"dl_updates_apply: {label}: one plain sentence naming the tool", sentence)
        set_sender(GUEST)
        for junk in (None, "nonsense", [], 7):
            del reached[:]
            try:
                r = du.tool_dl_updates_apply(junk)
            except Exception:  # noqa: BLE001
                r = None
            check(f"junk arguments from a guest are a refusal, not an exception ({junk!r})",
                  isinstance(r, dict) and r.get("ok") is False and reached == [])
        for label, raw in (("the owner on a chat platform", OWNER), ("no chat context", None)):
            set_sender(raw)
            del reached[:]
            r = du.tool_dl_updates_apply({"provider": "steam", "id": "x"})
            check(f"{label} gets past the gate and the work is reached", "updates.request_apply" in reached and "owner's" not in json.dumps(r))
            del reached[:]
            r = du.tool_dl_updates_apply({"provider": "steam", "id": "x", "dry_run": True})
            check(f"{label}: a plan is reached too", "updates.dry_run" in reached and r.get("ok") is True and "note" in r)
        set_sender(None)
        del reached[:]
        r = du.tool_dl_updates_apply({"all": True, "group": "nonsense"})
        check("an unknown group from the owner is refused before anything is queued", r.get("ok") is False and "group" in r["error"] and
              "updates.request_apply" not in reached, r)
        r = du.tool_dl_updates_apply({"dry_run": True})
        check("a plan needs a provider and an id", r.get("ok") is False and "provider and id" in r["error"], r)
        for junk in (None, "nonsense", [], 7):
            try:
                r = du.tool_dl_updates_apply(junk)
            except Exception:  # noqa: BLE001
                r = None
            check(f"junk arguments from the owner are an answer, not an exception ({junk!r})", isinstance(r, dict))
    finally:
        set_sender(None)
        updates.request_apply, updates.dry_run, du._store = orig["request_apply"], orig["dry_run"], orig["_store"]


def test_chat_tool_with_the_daemon_down_queues_nothing():
    set_sender(None)
    r = du.tool_dl_updates_apply({"all": True})
    check("with no hub daemon running the answer says so and queues nothing", r.get("ok") is False and r.get("code") == "daemon_down"
          and Store().applying_info() == {}, r)
    r = du.tool_dl_updates_apply({"provider": "steam", "id": "x"})
    check("same for one update", r.get("ok") is False and r.get("code") == "daemon_down", r)


# --------------------------------------------------------------------------- names, confirmation, hiding

def test_names_confirmation_and_hiding():
    check("dl_updates_apply is confirm-gated by name in its own file", "dl_updates_apply" in du.TOOL_CONFIRM_REQUIRED)
    check("and the safety policy asks before running it", tool_safety.requires_confirmation("dl_updates_apply"))
    check("dl_updates and dl_updates_check are still not confirm-gated",
          not tool_safety.requires_confirmation("dl_updates") and not tool_safety.requires_confirmation("dl_updates_check"))
    check("the demo's apply tool is confirm-gated too", tool_safety.requires_confirmation("apply_demo_update"))
    check("the demo's check tool is a read", not tool_safety.requires_confirmation("check_demo_updates"))
    check("all twelve pre-allocated Updates names are in OWNER_ONLY_TOOLS", set(TWELVE) <= set(tools.OWNER_ONLY_TOOLS),
          sorted(set(TWELVE) - set(tools.OWNER_ONLY_TOOLS)))
    check("dl_updates_apply is a real tool in the downloads group",
          "dl_updates_apply" in tools.TOOLS and "dl_updates_apply" in tool_registry.TOOL_INDEX
          and "dl_updates_apply" in tool_registry.TOOL_GROUPS.get("downloads", []))
    declared = {e["id"]: e for e in tools.AUTO_DOWNLOADS}
    check("the demo provider is declared with its apply tool and group", "demo_updates" in declared
          and declared["demo_updates"]["apply_tool"] == "apply_demo_update" and declared["demo_updates"]["updates_only"] is True)
    check("_declared_apply_tools() lists it", "apply_demo_update" in tools._declared_apply_tools())

    for label, raw in GUESTS[:3]:
        set_sender(raw)
        hidden = tools.hidden_from_sender()
        check(f"{label}: dl_updates_apply and every pre-allocated name are hidden", set(TWELVE) <= hidden)
        check(f"{label}: a provider file's declared apply tool is hidden too (not in OWNER_ONLY_TOOLS by name)",
              "apply_demo_update" in hidden and "apply_demo_update" not in tools.OWNER_ONLY_TOOLS)
        offered = [s["name"] for s in tools.schemas_for_tools(["dl_updates_apply", "apply_demo_update", "dl_list"])]
        check(f"{label}: schemas_for_tools offers the guest none of the apply tools", offered == ["dl_list"], offered)
    set_sender(None)
    for label, raw in (("the owner on a chat platform", OWNER), ("no chat context", None)):
        set_sender(raw)
        hidden = tools.hidden_from_sender()
        check(f"{label}: no apply tool is hidden", not ({"dl_updates_apply", "apply_demo_update"} & hidden))
        offered = [s["name"] for s in tools.schemas_for_tools(["dl_updates_apply", "apply_demo_update"])]
        check(f"{label}: both are offered", sorted(offered) == ["apply_demo_update", "dl_updates_apply"], offered)
    set_sender(None)
    check("hidden_from_sender stays a plain frozenset", isinstance(tools.hidden_from_sender(), frozenset))


# --------------------------------------------------------------------------- what the hub never does

def test_the_hub_builds_no_install_command():
    hub = ROOT / "jarvis-cli" / "jarvis" / "hub"
    forbidden = re.compile(r"\bsubprocess\b|os\.system|os\.popen|shell\s*=\s*True|\bPopen\b|\bpexpect\b|check_output")
    for name in ("updates.py", "updates_admin.py"):
        text = (hub / name).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        # strip the docstrings (they may say what the file never does)
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        check(f"hub/{name} has no way to start a program or build a command line", not forbidden.search(code),
              forbidden.findall(code))
    text = (ROOT / "jarvis-cli" / "jarvis" / "actions" / "download_updates.py").read_text(encoding="utf-8")
    code = re.sub(r'"""(?:.|\n)*?"""', "", text)
    check("actions/download_updates.py has none either", not forbidden.search(code), forbidden.findall(code))
    text = (hub / "updates.py").read_text(encoding="utf-8")
    check("the apply call is the provider's own tool: ApplyRunner reaches it through the tool executor only",
          "tools.execute_tool" in text and "apply_tool" in text)


def test_auto_update_is_off_by_default_and_not_settable_by_the_model():
    store = Store(Path(HOME) / "hub-gates-2.db", clock=time.time)
    cfg = settings_mod.update_global_settings(store.get_settings())
    check("a fresh store: auto-update is off", cfg["auto_update"] is False)
    check("dl_settings' own writable set does not hold the three update keys",
          not (settings_mod.UPDATES_WRITABLE & set(settings_mod.WRITABLE)))
    check("nor does dl_prefs'", not (settings_mod.UPDATES_WRITABLE & set(settings_mod.PREFS_WRITABLE)))
    for key in sorted(settings_mod.UPDATES_WRITABLE):
        clean, errors = settings_mod.validate_update({key: True})
        check(f"validate_update refuses {key} for the model's path", errors and not clean, errors)
    clean, errors = settings_mod.validate_provider_settings("steam", {"auto_update": True})
    check("and a provider's auto_update cannot be set through dl_provider_prefs", errors and "auto_update" not in clean)
    set_sender(None)
    check("dl_settings' schema does not advertise auto-update",
          "auto_update" not in json.dumps([s for s in tool_registry.TOOL_INDEX.get("dl_settings", {}).get("parameters", {}).items()]))


if __name__ == "__main__":
    for fn in (test_admin_ops_refuse_unattended, test_chat_tool_guest_gate, test_chat_tool_with_the_daemon_down_queues_nothing,
               test_names_confirmation_and_hiding, test_the_hub_builds_no_install_command,
               test_auto_update_is_off_by_default_and_not_settable_by_the_model):
        fn()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
