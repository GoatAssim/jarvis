"""Tests for the per-room "save this room" switch (channel spaces S7 / D4).

    python3 tests/test_channel_save_switch.py

A room (server, channel, thread or group chat) can be set to "don't save": nothing
from it is written to the transcript or to a conversation, no conversation id exists
for it, and its person summaries are not started. Everything else is unchanged: the
gate verdict, the reply, the usage counts, and every other room.

The switch can only turn saving OFF and only a literal false counts when it is read.

No bot token, no socket, no model, no real ~/.jarvis: HOME is redirected to a temp dir
BEFORE any jarvis module is imported (AGENTS.md).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-saveswitch-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
_CLI = str(Path(__file__).resolve().parent.parent / "jarvis-cli")
sys.path.insert(0, _CLI)

from jarvis import conversations                                               # noqa: E402
from jarvis.channels import (DISCORD, INSTAGRAM, base, changelog, permissions,  # noqa: E402
                             people, servers, spaces, transcript)
from jarvis.channels import config as channel_config                           # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND = "100000000000000001", "200000000000000002"
G1, G2 = "900000000000000001", "900000000000000002"
C1, C2, C3 = "800000000000000001", "800000000000000002", "800000000000000003"
THREAD = "700000000000000001"
IGGROUP = "ig-group-thread-1"


def wipe():
    root = Path(_HOME) / ".jarvis"
    for _ in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
    transcript.reset_cooldowns()
    spaces._cache.clear()


def reset(extra=None):
    wipe()
    people.touch(DISCORD, OWNER, handle="boss", is_owner=True)
    people.touch(DISCORD, FRIEND, handle="friend")
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [], "require_mention": True,
    })
    cfg[DISCORD].update(extra or {})
    cfg[INSTAGRAM].update({"enabled": True, "reply_allowlist": [FRIEND], "dm_allowlist": [FRIEND],
                           "require_mention": True, "cooldown_seconds": 0})
    channel_config.save_config(cfg)
    return channel_config.load_config()[DISCORD]


def room_msg(guild=G1, channel=C1, thread=None, text="hello", mid="m1", uid=FRIEND):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=uid, user_handle="friend", text=text,
        mentioned=True, guild_id=guild, channel_id=channel, message_id=mid,
        thread_id=thread or channel, guild_name="Srv", channel_name="general")


def ig_group_msg(text="hello group", mid="g1"):
    return permissions.IncomingMessage(
        INSTAGRAM, permissions.CTX_GROUP, user_id=FRIEND, user_handle="friend", text=text,
        mentioned=True, channel_id=IGGROUP, message_id=mid, thread_id=IGGROUP)


def dm_msg(text="hi", mid="d1"):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_DM, user_id=FRIEND, user_handle="friend", text=text,
        mentioned=True, channel_id="dm-1", message_id=mid, thread_id="dm-1")


class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
                            "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": []}


def drive(msg, platform=DISCORD):
    saved = base._ask_jarvis, base._notify_owner_new_sender
    calls, sent = [], []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return _Result()

    base._ask_jarvis = fake
    base._notify_owner_new_sender = lambda *a, **k: True
    try:
        decision = base.handle_message(platform, msg, lambda chunk: sent.append(chunk) or True)
    finally:
        base._ask_jarvis, base._notify_owner_new_sender = saved
    return decision, calls, sent


def conv_of(platform, thread_id):
    """The conversation id saved for a room, or None. no `create` callable: asking never makes one."""
    return transcript.conv_id_for(platform, thread_id)


def scopes_of(platform=DISCORD):
    return dict(channel_config.load_config()[platform].get("scopes") or {})


# ------------------------------------------------------------ the switch itself

def test_server_off_saves_nothing_there_and_only_there():
    reset()
    ok, err, _ = servers.set_switch(DISCORD, "guild", G1, "log", "off")
    check("switch written", ok, str(err))
    d, calls, sent = drive(room_msg(guild=G1, channel=C1, text="secret one"))
    check("the room is still answered", d.allowed and calls and sent)
    check("no transcript line for the muted server", transcript.read_thread(DISCORD, C1) == [])
    check("no conversation was created", conv_of(DISCORD, C1) is None, str(conv_of(DISCORD, C1)))
    check("no registry entry for the room", spaces.get(DISCORD, C1) is None)
    check("the text is on no disk", all("secret one" not in p.read_text(errors="ignore")
                                         for p in Path(_HOME).rglob("*") if p.is_file()))
    d, calls, sent = drive(room_msg(guild=G2, channel=C3, text="saved one", mid="m2"))
    check("another server is still saved", len(transcript.read_thread(DISCORD, C3)) >= 2,
          str(transcript.read_thread(DISCORD, C3)))
    check("and gets a conversation", conv_of(DISCORD, C3) is not None)
    check("and a registry entry", spaces.get(DISCORD, C3) is not None)


def test_channel_and_thread_scopes():
    reset()
    servers.set_switch(DISCORD, "channel", C1, "log", "off")
    drive(room_msg(channel=C1, text="muted channel"))
    drive(room_msg(channel=C2, text="open channel", mid="m2"))
    check("the muted channel saves nothing", transcript.read_thread(DISCORD, C1) == [])
    check("its sibling is saved", len(transcript.read_thread(DISCORD, C2)) >= 2)
    ok, err, detail = spaces.set_switch(DISCORD, THREAD, "log", "off", kind="thread")
    check("a thread can be muted", ok and detail["scope"] == f"thread:{THREAD}", str(detail))
    drive(room_msg(channel=C2, thread=THREAD, text="muted thread", mid="m3"))
    check("the thread saves nothing", transcript.read_thread(DISCORD, THREAD) == [])
    drive(room_msg(channel=C2, text="open again", mid="m4"))
    check("its parent channel is still saved", len(transcript.read_thread(DISCORD, C2)) >= 4)


def test_instagram_group_by_thread_id():
    reset()
    ok, err, detail = spaces.set_switch(INSTAGRAM, IGGROUP, "log", "off")
    check("an Instagram group can be muted by thread id", ok and detail["scope"] == f"channel:{IGGROUP}", str(detail))
    d, calls, sent = drive(ig_group_msg(), platform=INSTAGRAM)
    check("it is still answered", d.allowed and calls and sent, str(d.stage))
    check("nothing from it is saved", transcript.read_thread(INSTAGRAM, IGGROUP) == [])
    check("no conversation", conv_of(INSTAGRAM, IGGROUP) is None)
    spaces.set_switch(INSTAGRAM, IGGROUP, "log", "inherit")
    drive(ig_group_msg(text="again", mid="g2"), platform=INSTAGRAM)
    check("inherit puts it back", len(transcript.read_thread(INSTAGRAM, IGGROUP)) >= 2)


def test_platform_level_off_and_dms_unchanged():
    reset({"log_conversations": False})
    drive(room_msg(text="x"))
    check("platform-level off still saves nothing", transcript.read_thread(DISCORD, C1) == [])
    reset()
    servers.set_switch(DISCORD, "guild", G1, "log", "off")
    drive(dm_msg())
    check("a DM is not affected by a server's switch", len(transcript.read_thread(DISCORD, "dm-1")) >= 2)


def test_usage_counts_are_still_written():
    reset()
    servers.set_switch(DISCORD, "guild", G1, "log", "off")
    drive(room_msg())
    usage_file = Path(_HOME) / ".jarvis" / "channels" / "usage.jsonl"
    check("the counts-only usage line is still written", usage_file.exists() and usage_file.read_text().strip(),
          str(usage_file.exists()))
    check("and holds no text", "hello" not in usage_file.read_text())


# ------------------------------------------------------------ safety of the switch

def test_only_a_literal_false_counts():
    for bad in ("false", "off", 0, None, "no", "banana", True, 1):
        cfg = reset({"scopes": {f"guild:{G1}": {"log_conversations": bad}}})
        check(f"{bad!r} in a scope is ignored (saving stays on)", permissions.saving_on(cfg, room_msg()))
    cfg = reset({"scopes": {f"guild:{G1}": {"log_conversations": False}}})
    check("a literal false turns it off", not permissions.saving_on(cfg, room_msg()))
    check("another server is unaffected", permissions.saving_on(cfg, room_msg(guild=G2)))
    cfg = reset({"log_conversations": False, "scopes": {f"guild:{G1}": {"log_conversations": True}}})
    check("a scope can never switch saving back ON over a platform that is off",
          not permissions.saving_on(cfg, room_msg()))
    check("never raises on junk", permissions.saving_on(None, room_msg()) in (True, False)
          and permissions.saving_on({"scopes": 7}, room_msg()) in (True, False))
    cfg = reset({"scopes": {f"guild:{G1}": {"log_conversations": "false"}}})
    d, calls, sent = drive(room_msg(text="typed string"))
    check("a hand-typed string neither crashes nor mutes", d.allowed and len(transcript.read_thread(DISCORD, C1)) >= 2)


def test_the_switch_never_writes_true_and_never_touches_access():
    reset({"allowed_guilds": [G1], "allowed_channels": [C1]})
    before = channel_config.load_config()[DISCORD]
    servers.set_switch(DISCORD, "guild", G1, "log", "off")
    after = channel_config.load_config()[DISCORD]
    check("the switch is a literal false", scopes_of()[f"guild:{G1}"]["log_conversations"] is False)
    for key in ("allowed_guilds", "allowed_channels", "reply_allowlist", "dm_allowlist",
                "tool_allowlist", "allow_tools", "require_mention", "owner", "enabled"):
        check(f"{key} was not touched", before.get(key) == after.get(key), key)
    servers.set_switch(DISCORD, "guild", G1, "log", "on")
    check("on removes the override instead of writing true", f"guild:{G1}" not in scopes_of(), str(scopes_of()))
    ok, err, _ = servers.set_switch(DISCORD, "guild", G1, "log", "maybe")
    check("a bad value is refused", not ok)
    ok, err, _ = spaces.set_switch(INSTAGRAM, "../etc/passwd", "log", "off")
    check("a path-like room id is refused", not ok, err)
    ok, err, _ = spaces.set_switch(INSTAGRAM, "", "log", "off")
    check("an empty room id is refused", not ok)
    ok, err, _ = spaces.set_switch("myspace", IGGROUP, "log", "off")
    check("an unknown platform is refused", not ok)
    ok, err, _ = spaces.set_switch(INSTAGRAM, IGGROUP, "banana", "off")
    check("an unknown switch is refused", not ok)
    ok, err, _ = spaces.set_switch(INSTAGRAM, IGGROUP, "mention", "off")
    check("mention can still only be switched on", not ok)
    ok, err, _ = spaces.set_switch(INSTAGRAM, IGGROUP, "log", "off", kind="guild")
    check("a room kind other than channel/thread is refused", not ok)


def test_the_gate_verdict_does_not_depend_on_the_switch():
    cfg = reset()
    before = base.gate(DISCORD, room_msg(), channel_config.load_config()[DISCORD])
    servers.set_switch(DISCORD, "guild", G1, "log", "off")
    cfg = channel_config.load_config()[DISCORD]
    after = base.gate(DISCORD, room_msg(), cfg)
    check("same verdict", (before.allowed, before.stage) == (after.allowed, after.stage),
          f"{before.stage} {after.stage}")
    check("the real decide() agrees", permissions.decide(cfg, room_msg()).allowed is True)


def test_view_and_changelog():
    reset()
    servers.note_server(DISCORD, G1, "Srv") if hasattr(servers, "note_server") else None
    servers.set_switch(DISCORD, "guild", G1, "log", "off")
    view = servers.view(DISCORD)
    text = json.dumps(view)
    check("the read model reports saving", '"saving"' in text, text[:300])
    row = next((s for s in view.get("servers", []) if s.get("id") == G1), None)
    check("the muted server reads as not saving", row is not None and row.get("saving") is False
          and row["override"]["log"] == "off", str(row))
    servers.set_switch(DISCORD, "guild", G1, "log", "off")           # no change
    servers.set_switch(DISCORD, "guild", G1, "log", "on")
    lines = [e for e in changelog.read_all() if e.get("kind") == "scope" and e.get("switch") == "log"]
    check("each real change logs once", len(lines) == 2, str(lines))
    check("in words", all("Saving this room" in changelog.describe(e) for e in lines))
    check("the log holds no message text", "hello" not in json.dumps(lines))


def _cli(*args):
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=_CLI, JARVIS_RAW_ARCHIVE="0")
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True,
                          text=True, env=env, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout + proc.stderr}


def test_cli():
    reset()
    code, out = _cli("channels-server-set", DISCORD, "guild", G1, "log", "off")
    check("channels-server-set log off", code == 0 and out.get("ok"), str(out))
    code, out = _cli("channels-space-set", INSTAGRAM, IGGROUP, "log", "off")
    check("channels-space-set", code == 0 and out.get("ok") and out.get("known") is False, str(out))
    check("it wrote channel:<thread>", scopes_of(INSTAGRAM).get(f"channel:{IGGROUP}", {}).get("log_conversations") is False)
    code, out = _cli("channels-space-set", DISCORD, THREAD, "ambient", "off", "--thread")
    check("--thread writes a thread scope", code == 0 and f"thread:{THREAD}" in scopes_of(), str(out))
    code, out = _cli("channels-space-set", INSTAGRAM, IGGROUP, "log")
    check("missing arguments print usage", code != 0)
    code, out = _cli("channels-space-set", INSTAGRAM, "bad/id", "log", "off")
    check("a bad id is refused", code != 0 and not out.get("ok"), str(out))
    from jarvis import reserved_names
    names = getattr(reserved_names, "RESERVED", None) or getattr(reserved_names, "RESERVED_NAMES", None)
    src = Path(reserved_names.__file__).read_text()
    check("the command is registered as a reserved name", "channels-space-set" in src)


def test_server_js_route_is_static_and_narrow():
    src = (Path(__file__).resolve().parent.parent / "web" / "server.js").read_text()
    check("the log switch is allowed", '"ambient", "log"' in src)
    check("the room route exists and validates", "/api/channels/spaces/:platform/:threadId" in src
          and "CHANNEL_SPACE_ID" in src)
    check("the route only runs channels-space-set", '"channels-space-set", platform, threadId' in src)


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n"
                          + traceback.format_exc(limit=4))
    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    shutil.rmtree(_HOME, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
