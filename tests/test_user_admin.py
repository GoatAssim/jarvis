"""Tests for the per-person Channels permissions (L.36).

    python3 tests/test_user_admin.py

Covers channels/user_perms.py (the store), channels/user_admin.py (the one
implementation behind the panel and `jarvis channels-user`), the enforcement
point in channels/base._ask_jarvis, the send_dm refusal, and the avatar field
on people.py. No bot token, no socket, no real ~/.jarvis: HOME is redirected to
a temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-user-admin-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME  # Windows spelling of the same thing
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import DISCORD, INSTAGRAM, base, people, user_admin, user_perms  # noqa: E402
from jarvis.channels import config as channel_config                                   # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND, STRANGER, GHOST = "100000000000000001", "200000000000000002", "300000000000000003", "999999999999999999"


def reset(cfg_discord=None):
    """Fresh store: three registered people, a sane Discord config."""
    for f in (user_perms.PERMS_FILE, people.PEOPLE_FILE, channel_config.CONFIG_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    for uid, handle, name in ((OWNER, "boss", "Boss"), (FRIEND, "friend", "Friend"), (STRANGER, "rando", "")):
        people.touch(DISCORD, uid, handle=handle)
    people.set_name(DISCORD, FRIEND, "Friend")
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": True,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [OWNER, FRIEND],
    })
    cfg[DISCORD].update(cfg_discord or {})
    channel_config.save_config(cfg)


def view(uid, platform=DISCORD):
    for p in user_admin.list_view(platform)["people"]:
        if p["user_id"] == uid:
            return p
    return None


# --------------------------------------------------------------- the store

def test_defaults_are_no_restriction():
    reset()
    check("no record -> inherit", user_perms.get(DISCORD, FRIEND)["tools"]["mode"] == "inherit")
    check("no record -> can_dm", user_perms.get(DISCORD, FRIEND)["can_dm"] is True)
    check("inherit -> scope None (ask untouched)", user_perms.effective_tool_scope(DISCORD, FRIEND) is None)


def test_custom_scope_is_allowlist_plus_plumbing():
    reset()
    user_perms.set_tools(DISCORD, FRIEND, "custom", ["get_datetime", "web_search"])
    scope = user_perms.effective_tool_scope(DISCORD, FRIEND)
    check("custom scope has the ticked tools", {"get_datetime", "web_search"} <= scope)
    check("custom scope adds discovery plumbing", set(user_perms.PLUMBING_TOOLS) <= scope)
    check("custom scope has nothing else", scope == {"get_datetime", "web_search"} | set(user_perms.PLUMBING_TOOLS))
    check("plumbing is not stored (UI shows what was ticked)",
          user_perms.get(DISCORD, FRIEND)["tools"]["allow"] == ["get_datetime", "web_search"])


def test_empty_custom_means_no_tools_at_all():
    reset()
    user_perms.set_tools(DISCORD, FRIEND, "custom", [])
    scope = user_perms.effective_tool_scope(DISCORD, FRIEND)
    check("empty custom -> empty frozenset (no plumbing either)", scope == frozenset(), repr(scope))


def test_only_non_defaults_are_stored():
    reset()
    user_perms.set_tools(DISCORD, FRIEND, "custom", ["a_tool"])
    check("a limited person has a record", f"discord:{FRIEND}" in json.loads(user_perms.PERMS_FILE.read_text()))
    user_perms.set_tools(DISCORD, FRIEND, "inherit")
    check("back to default removes the record", f"discord:{FRIEND}" not in json.loads(user_perms.PERMS_FILE.read_text()))
    user_perms.set_tools(DISCORD, FRIEND, "custom", ["a_tool"])
    user_perms.set_tools(DISCORD, FRIEND, "inherit")
    user_perms.set_tools(DISCORD, FRIEND, "custom", [])
    check("inherit clears the old list (no stale grant)", user_perms.get(DISCORD, FRIEND)["tools"]["allow"] == [])


def test_tool_names_validated():
    reset()
    for bad in ("a,b", "rm -rf /", "x" * 70, "tool name", "a;b", "../etc"):
        try:
            user_perms.set_tools(DISCORD, FRIEND, "custom", [bad])
            check(f"rejects {bad[:20]!r}", False)
        except ValueError:
            check(f"rejects {bad[:20]!r}", True)
    ok, err = user_admin.set_tools(DISCORD, FRIEND, "custom", ["good", "bad,name"])
    check("admin layer reports bad name, saves nothing", not ok and not user_perms.PERMS_FILE.exists(), err)
    ok, err = user_admin.set_tools(DISCORD, FRIEND, "weird", [])
    check("unknown mode refused", not ok)


def test_unreadable_file_fails_closed():
    reset()
    user_perms.set_tools(DISCORD, FRIEND, "custom", ["a_tool"])
    user_perms.PERMS_FILE.write_text("{ torn", encoding="utf-8")
    bak = user_perms.PERMS_FILE.with_suffix(user_perms.PERMS_FILE.suffix + ".bak")
    if bak.exists():
        bak.unlink()
    try:
        user_perms.effective_tool_scope(DISCORD, FRIEND)
        check("unreadable file raises", False)
    except user_perms.PermsUnreadable:
        check("unreadable file raises", True)
    check("dm_allowed is False when unreadable", user_perms.dm_allowed(DISCORD, FRIEND) is False)
    check("list_view reports instead of guessing", user_admin.list_view(DISCORD)["perms_error"] != "")
    check("absent file is NOT an error", (user_perms.PERMS_FILE.unlink() or True) and user_perms.dm_allowed(DISCORD, FRIEND) is True)


def test_hand_edited_garbage_is_tolerated_on_read():
    reset()
    user_perms.PERMS_FILE.write_text(json.dumps({
        f"discord:{FRIEND}": {"tools": {"mode": "custom", "allow": ["ok_tool", "bad,one", 5]}, "can_dm": "maybe"}}))
    got = user_perms.get(DISCORD, FRIEND)
    check("valid names kept, bad dropped, list still limits", got["tools"]["mode"] == "custom" and "ok_tool" in got["tools"]["allow"] and "bad,one" not in got["tools"]["allow"], str(got))
    check("non-False can_dm stays default True", got["can_dm"] is True)


# ------------------------------------------- enforcement: JARVIS_ALLOWED_TOOLS

class _FakeResult:
    text, provider, error, ok = "hi", "fake", "", True


def _run_ask(may_use_tools, tool_scope):
    """Run base._ask_jarvis against a fake ai_client.ask and report what the
    environment looked like DURING the ask and AFTER it."""
    from jarvis import ai_client
    seen = {}
    original = ai_client.ask

    def fake_ask(*a, **k):
        seen["during"] = os.environ.get("JARVIS_ALLOWED_TOOLS", "<unset>")
        return _FakeResult()
    ai_client.ask = fake_ask
    os.environ.pop("JARVIS_ALLOWED_TOOLS", None)
    try:
        base._ask_jarvis("hello", "conv123456", may_use_tools, platform=DISCORD, tool_scope=tool_scope)
    finally:
        ai_client.ask = original
    seen["after"] = os.environ.get("JARVIS_ALLOWED_TOOLS", "<unset>")
    return seen


def test_ask_environment_enforcement():
    reset()
    s = _run_ask(True, None)
    check("no scope: allowlist untouched during", s["during"] == "<unset>", str(s))
    s = _run_ask(True, frozenset({"b_tool", "a_tool"}))
    check("scope becomes the allowlist (sorted, comma list)", s["during"] == "a_tool,b_tool", str(s))
    check("allowlist restored to unset afterwards", s["after"] == "<unset>", str(s))
    s = _run_ask(False, frozenset({"a_tool"}))
    check("tools OFF beats any scope (a scope can only shrink)", s["during"] == "", str(s))
    check("restored after tools-off ask", s["after"] == "<unset>", str(s))
    os.environ["JARVIS_ALLOWED_TOOLS"] = "preexisting"
    try:
        from jarvis import ai_client
        original = ai_client.ask
        ai_client.ask = lambda *a, **k: _FakeResult()
        try:
            base._ask_jarvis("x", "conv123456", True, platform=DISCORD, tool_scope=frozenset({"z"}))
        finally:
            ai_client.ask = original
        check("a pre-existing allowlist is restored exactly", os.environ.get("JARVIS_ALLOWED_TOOLS") == "preexisting")
    finally:
        os.environ.pop("JARVIS_ALLOWED_TOOLS", None)


def test_tools_py_really_refuses_outside_the_scope():
    """The env var is only useful if the executor honours it. Pin that the real
    run_tool refuses a tool outside the allowlist."""
    from jarvis import tools as system_tools
    os.environ["JARVIS_ALLOWED_TOOLS"] = "get_datetime"
    try:
        out = system_tools.execute_tool("list_dir", {"path": "."})
        check("tool outside scope refused by the real executor", out == {"error": "tool not permitted"}, str(out))
        os.environ["JARVIS_ALLOWED_TOOLS"] = ""
        check("empty allowlist refuses everything", system_tools.execute_tool("get_datetime", {}) == {"error": "tool not permitted"})
    finally:
        os.environ.pop("JARVIS_ALLOWED_TOOLS", None)


# ------------------------------------------------------------ the switches

def test_access_switches_use_the_real_lists():
    reset()
    ok, err, _ = user_admin.set_flag(DISCORD, STRANGER, "reply", True)
    check("reply on adds the stable id", ok and STRANGER in channel_config.platform_config(DISCORD)["reply_allowlist"], err)
    ok, err, _ = user_admin.set_flag(DISCORD, STRANGER, "reply", False)
    check("reply off removes it", ok and STRANGER not in channel_config.platform_config(DISCORD)["reply_allowlist"])
    # a handle spelling in the list is removed too
    channel_config.add_to_set(DISCORD, "dm_allowlist", "@Rando")
    ok, _, _ = user_admin.set_flag(DISCORD, STRANGER, "dm", False)
    check("a @handle entry for them is removed as well", ok and "rando" not in channel_config.platform_config(DISCORD)["dm_allowlist"])
    v = view(STRANGER)
    check("view agrees with the gate", v["dm"]["on"] is False and v["reply"]["on"] is False)


def test_wildcard_cannot_be_switched_off_per_person():
    reset({"reply_allowlist": ["*"]})
    v = view(FRIEND)
    check("wildcard reported as via=wildcard", v["reply"]["via"] == "wildcard" and v["reply"]["on"])
    ok, err, _ = user_admin.set_flag(DISCORD, FRIEND, "reply", False)
    check("per-person off refused with a reason", not ok and "*" in err, err)
    check("the wildcard is untouched", channel_config.platform_config(DISCORD)["reply_allowlist"] == ["*"])


def test_registered_only():
    reset()
    ok, err, _ = user_admin.set_flag(DISCORD, GHOST, "reply", True)
    check("unregistered person refused", not ok and "registered" in err, err)
    check("nothing was written for them", GHOST not in channel_config.platform_config(DISCORD)["reply_allowlist"])
    ok, err = user_admin.set_tools(DISCORD, GHOST, "custom", [])
    check("tools for an unregistered person refused", not ok)
    check("list_view only lists registered people", GHOST not in [p["user_id"] for p in user_admin.list_view()["people"]])
    ok, err, _ = user_admin.set_flag("myspace", FRIEND, "reply", True)
    check("unknown platform refused", not ok)
    ok, err, _ = user_admin.set_flag(DISCORD, FRIEND, "sudo", True)
    check("unknown switch refused", not ok)


def test_owner_moves_and_is_single():
    reset()
    ok, err, note = user_admin.set_flag(DISCORD, FRIEND, "owner", True)
    check("owner on succeeds", ok, err)
    check("config owner is now FRIEND", channel_config.platform_config(DISCORD)["owner"] == FRIEND)
    check("previous owner flag cleared", people.get(DISCORD, OWNER)["is_owner"] is False)
    check("view: exactly one owner", sum(1 for p in user_admin.list_view(DISCORD)["people"] if p["owner"]) == 1)
    check("the note says owner grants nothing by itself", "grants no replies or tools" in note, note)
    cfg = channel_config.platform_config(DISCORD)
    check("ownership added NO list entry", cfg["tool_allowlist"] == [OWNER, FRIEND] and cfg["reply_allowlist"] == [OWNER, FRIEND])
    ok, _, note = user_admin.set_flag(DISCORD, FRIEND, "owner", False)
    check("owner off clears the config", ok and channel_config.platform_config(DISCORD)["owner"] == "")
    ok, _, _ = user_admin.set_flag(DISCORD, FRIEND, "owner", False)
    check("owner off again is idempotent", ok)


def test_block_is_a_real_block():
    reset()
    ok, err, _ = user_admin.set_flag(DISCORD, FRIEND, "blocked", True)
    cfg = channel_config.platform_config(DISCORD)
    check("block succeeds", ok, err)
    check("blocked person is on NO list", all(FRIEND not in cfg[k] for k in ("dm_allowlist", "reply_allowlist", "tool_allowlist")))
    check("follow recorded as blocked", people.get(DISCORD, FRIEND)["follow"] == "blocked")
    for flag in ("dm", "reply", "tool", "owner"):
        ok, err, _ = user_admin.set_flag(DISCORD, FRIEND, flag, True)
        check(f"cannot switch {flag} on while blocked", not ok and "blocked" in err, err)
    ok, err, _ = user_admin.set_flag(DISCORD, OWNER, "blocked", True)
    check("the owner cannot be blocked", not ok and "owner" in err, err)
    ok, _, note = user_admin.set_flag(DISCORD, FRIEND, "blocked", False)
    check("unblock starts from nothing (no list entries)", ok and FRIEND not in channel_config.platform_config(DISCORD)["reply_allowlist"])
    check("unblock does not mark them approved", people.get(DISCORD, FRIEND)["follow"] == "unknown")


def test_block_with_wildcard_says_so():
    reset({"reply_allowlist": ["*"]})
    ok, _, note = user_admin.set_flag(DISCORD, STRANGER, "blocked", True)
    check("block works but warns the wildcard still covers them", ok and "*" in note, note)


def test_effective_summary_matches_gate_rule():
    reset()
    check("tool on, allow_tools on, inherit -> all", view(FRIEND)["effective"]["tools"] == "all")
    user_perms.set_tools(DISCORD, FRIEND, "custom", ["a", "b", "c"])
    e = view(FRIEND)["effective"]
    check("custom -> count", e["tools"] == "custom" and e["tool_count"] == 3, str(e))
    reset({"allow_tools": False})
    e = view(FRIEND)["effective"]
    check("platform master switch off -> none, and says why", e["tools"] == "none" and e["tools_blocked_by_platform"] is True, str(e))
    reset()
    user_admin.set_flag(DISCORD, FRIEND, "reply", False)
    check("not answered -> no tools shown", view(FRIEND)["effective"]["tools"] == "none")


def test_tokens_never_in_the_view():
    reset({"bot_token": "SECRET-TOKEN-VALUE", "app_secret": "SECRET2"})
    blob = json.dumps(user_admin.list_view())
    check("no token in the panel payload", "SECRET-TOKEN-VALUE" not in blob and "SECRET2" not in blob)
    check("only a presence flag", user_admin.list_view(DISCORD)["platforms"][DISCORD]["token_set"] is True)


# --------------------------------------------------------------- send_dm

def test_send_dm_refuses_when_switched_off():
    reset()
    from jarvis.actions import send_dm
    rec, err = send_dm._resolve("Friend", DISCORD)
    check("resolves normally by default", rec is not None, str(err))
    user_admin.set_flag(DISCORD, FRIEND, "send_dm", False)
    rec, err = send_dm._resolve("Friend", DISCORD)
    check("refuses once switched off", rec is None and "switched off" in (err or {}).get("error", ""), str(err))
    check("the refusal points at the panel", "Permissions" in (err or {}).get("hint", ""))
    user_admin.set_flag(DISCORD, FRIEND, "send_dm", True)
    rec, err = send_dm._resolve("Friend", DISCORD)
    check("switching back on restores it", rec is not None)
    user_perms.PERMS_FILE.write_text("{ torn")
    rec, err = send_dm._resolve("Friend", DISCORD)
    check("an unreadable permissions file refuses (never guesses)", rec is None, str(err))


def test_send_dm_owner_only_invariants_still_hold():
    """Nothing in this change loosened the tool itself."""
    from jarvis.actions import send_dm
    from jarvis import tools as system_tools
    check("send_dm still needs confirmation", "send_dm" in send_dm.TOOL_CONFIRM_REQUIRED)
    check("send_dm still owner-only hidden from guests", "send_dm" in system_tools.OWNER_ONLY_TOOLS)
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": DISCORD, "user_id": FRIEND, "is_owner": False})
    try:
        out = send_dm.tool_send_dm({"person": "Boss", "message": "hi", "dry_run": True})
        check("a guest still can't send even with every switch on", out.get("ok") is False and "owner-only" in out.get("error", ""), str(out))
    finally:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)


# ------------------------------------------------------------------ avatar

def test_avatar_field():
    reset()
    good = "https://cdn.discordapp.com/avatars/1/abc.png?size=128"
    rec = people.touch(DISCORD, FRIEND, handle="friend", avatar=good)
    check("a CDN avatar is stored", rec["avatar"] == good)
    rec = people.touch(DISCORD, FRIEND, handle="friend", avatar="")
    check("an empty avatar never wipes a stored one (Instagram has none)", rec["avatar"] == good)
    for bad in ("http://cdn.discordapp.com/a.png", "https://evil.example/a.png", "https://cdn.discordapp.com.evil.io/a.png",
                "javascript:alert(1)", "https://cdn.discordapp.com/a.png\"onerror=\"x", "https://" + "a" * 400 + ".discordapp.com/x"):
        rec = people.touch(DISCORD, STRANGER, handle="rando", avatar=bad)
        check(f"rejects {bad[:35]!r}", rec["avatar"] == "", rec["avatar"])
    check("list_view carries the avatar", view(FRIEND)["avatar"] == good)
    check("old records without the field still load", people._blank(DISCORD, "1")["avatar"] == "")


def test_discord_avatar_helper_never_raises():
    from jarvis.channels import discord_gateway as dg

    class A:
        class display_avatar:
            url = "https://cdn.discordapp.com/avatars/1/a.png"
    check("size param appended", dg._avatar_url(A).endswith("a.png?size=128"))
    check("no display_avatar -> empty", dg._avatar_url(object()) == "")

    class Boom:
        @property
        def display_avatar(self):
            raise RuntimeError("library changed")
    check("a raising user object -> empty, not a crash", dg._avatar_url(Boom()) == "")


def test_cli_roundtrip():
    """channels-user / channels-user-tools through the real CLI dispatcher."""
    import io
    import contextlib
    from jarvis import channels_cli
    reset()

    def run(*argv):
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                channels_cli.handle(list(argv))
            except SystemExit as exc:
                code = exc.code or 0
        return code, json.loads(buf.getvalue())
    code, out = run("channels-user", DISCORD, "@rando", "reply", "on")
    check("CLI resolves a @handle of a REGISTERED person", code == 0 and out["user_id"] == STRANGER, str(out))
    code, out = run("channels-user", DISCORD, "nobody", "reply", "on")
    check("CLI refuses an unknown name", code != 0 and out["ok"] is False)
    code, out = run("channels-user", DISCORD, FRIEND, "reply", "maybe")
    check("CLI refuses a non on/off value", code != 0)
    code, out = run("channels-user-tools", DISCORD, FRIEND, "custom", "get_datetime", "web_search")
    check("CLI sets a custom list", code == 0 and user_perms.get(DISCORD, FRIEND)["tools"]["allow"] == ["get_datetime", "web_search"])
    code, out = run("channels-users", DISCORD)
    check("channels-users lists people", code == 0 and len(out["people"]) == 3)


# -------------------------------------------------------------- runner
# (Defined ABOVE the runner on purpose: AGENTS.md - a test appended below it
# would silently never run.)

if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{name} raised {exc!r}\n{traceback.format_exc()}")
    for line in FAILED:
        print("FAILED:", line)
    print(f"{PASSED} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
