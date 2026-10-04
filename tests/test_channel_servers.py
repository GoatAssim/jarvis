"""Tests for Discord server support and tool calls in the Conversation view.

    python3 tests/test_channel_servers.py

  S1  channels/servers.py registry         note_guild / note_channel / forget / read
  S2  switches only take access away       set_switch + the REAL permissions.decide()
  S3  the read model                       servers.view (effective state, reasons)
  S4  tool calls in the transcript         scrubbing, clipping, read-back, old lines
  S5  base.handle_message end to end       tools captured -> first reply line, and
                                           NOT logged when log_conversations is off
  S6  the gateway's thread routing         discord_gateway._where with fake objects
  S7  the CLI                              channels-servers / channels-server-set

No bot token, no socket, no model, no real ~/.jarvis: HOME is redirected to a
temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-servers-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME  # Windows spelling of the same thing
os.environ["JARVIS_RAW_ARCHIVE"] = "0"   # S5 must not depend on an event log
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import (DISCORD, INSTAGRAM, base, discord_gateway, people,  # noqa: E402
                             permissions, servers, transcript, user_admin)
from jarvis.channels import config as channel_config                              # noqa: E402

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


def wipe():
    root = Path(_HOME) / ".jarvis"
    if root.exists():
        import shutil
        shutil.rmtree(root)
    servers._cache.clear()
    transcript.reset_cooldowns()


def reset(extra=None):
    wipe()
    for uid, handle in ((OWNER, "boss"), (FRIEND, "friend")):
        people.touch(DISCORD, uid, handle=handle)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": True, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [OWNER, FRIEND], "require_mention": True,
    })
    cfg[DISCORD].update(extra or {})
    channel_config.save_config(cfg)


def group_msg(uid=FRIEND, guild=G1, channel=C1, thread=None, text="hello", mid="m1"):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=uid, user_handle="h", text=text,
        mentioned=True, guild_id=guild, channel_id=channel, message_id=mid,
        thread_id=thread or channel)


def scopes():
    return channel_config.platform_config(DISCORD).get("scopes") or {}


# ===================================================================== S1

def test_registry_records_names_once():
    reset()
    check("a new server is written", servers.note_guild(DISCORD, G1, "My Server",
          channels=[(C1, "general", "text"), (C2, "jarvis", "text")]) is True)
    reg = servers.read(DISCORD)
    check("name and channels stored", reg[G1]["name"] == "My Server"
          and reg[G1]["channels"][C2]["name"] == "jarvis", str(reg))
    check("announcing the same facts again is not a write",
          servers.note_guild(DISCORD, G1, "My Server",
                             channels=[(C1, "general", "text"), (C2, "jarvis", "text")]) is False)
    check("a rename IS a write", servers.note_guild(DISCORD, G1, "Renamed") is True)
    check("the rename kept the channels", len(servers.read(DISCORD)[G1]["channels"]) == 2)
    check("a replaced channel list drops a deleted channel",
          servers.note_guild(DISCORD, G1, "Renamed", channels=[(C1, "general", "text")]) is True
          and list(servers.read(DISCORD)[G1]["channels"]) == [C1])


def test_registry_channel_notes_and_cache():
    reset()
    check("first sighting writes", servers.note_channel(DISCORD, G1, C1, "general", guild_name="S") is True)
    check("same names again: no write (cache)", servers.note_channel(DISCORD, G1, C1, "general", guild_name="S") is False)
    check("a channel rename writes", servers.note_channel(DISCORD, G1, C1, "chat", guild_name="S") is True)
    check("forget removes it", servers.forget_channel(DISCORD, G1, C1) is True
          and C1 not in servers.read(DISCORD)[G1]["channels"])
    servers.note_guild(DISCORD, G1, "S", left=True)
    check("left is remembered", servers.read(DISCORD)[G1]["left"] is True)
    servers.note_channel(DISCORD, G1, C2, "x", guild_name="S")
    check("a message in a 'left' server means we are back", servers.read(DISCORD)[G1]["left"] is False)


def test_registry_never_raises_and_rejects_junk():
    reset()
    check("non-numeric guild id is ignored", servers.note_guild(DISCORD, "../etc", "x") is False)
    check("instagram has no servers", servers.note_guild(INSTAGRAM, G1, "x") is False)
    check("garbage channel id ignored", servers.note_channel(DISCORD, G1, "nope", "x") is False)
    servers.registry_file().parent.mkdir(parents=True, exist_ok=True)
    servers.registry_file().write_text("{ not json", encoding="utf-8")
    check("a corrupt registry reads as empty", servers.read(DISCORD) == {})
    check("and is repaired by the next write", servers.note_guild(DISCORD, G1, "S") is True
          and servers.read(DISCORD)[G1]["name"] == "S")


# ===================================================================== S2

def test_switches_write_only_restrictive_values():
    reset()
    ok, err, detail = servers.set_switch(DISCORD, "guild", G1, "enabled", "off")
    check("server off is saved", ok and scopes()[f"guild:{G1}"] == {"enabled": False}, f"{err} {scopes()}")
    ok, *_ = servers.set_switch(DISCORD, "guild", G1, "enabled", "on")
    check("'on' removes the override instead of writing true", ok and f"guild:{G1}" not in scopes(), str(scopes()))
    ok, *_ = servers.set_switch(DISCORD, "guild", G1, "tools", "on")
    check("tools 'on' never writes allow_tools true", ok and f"guild:{G1}" not in scopes(), str(scopes()))
    ok, *_ = servers.set_switch(DISCORD, "guild", G1, "tools", "off")
    check("tools off -> allow_tools false", ok and scopes()[f"guild:{G1}"] == {"allow_tools": False})
    ok, err, _ = servers.set_switch(DISCORD, "guild", G1, "mention", "off")
    check("mention off is REFUSED (it would widen reach)", ok is False and "only be switched on" in err, err)
    ok, *_ = servers.set_switch(DISCORD, "guild", G1, "mention", "on")
    check("mention on -> require_mention true, other fields kept",
          ok and scopes()[f"guild:{G1}"] == {"allow_tools": False, "require_mention": True}, str(scopes()))
    for sw in ("tools", "mention"):
        servers.set_switch(DISCORD, "guild", G1, sw, "inherit")
    check("inherit on everything leaves no empty scope behind", f"guild:{G1}" not in scopes(), str(scopes()))


def test_switches_preserve_hand_written_scope_fields():
    reset({"scopes": {f"guild:{G1}": {"reply_allowlist": ["*"], "allow_tools": False}}})
    servers.set_switch(DISCORD, "guild", G1, "enabled", "off")
    entry = scopes()[f"guild:{G1}"]
    check("a hand-written reply_allowlist survives", entry.get("reply_allowlist") == ["*"], str(entry))
    servers.set_switch(DISCORD, "guild", G1, "enabled", "inherit")
    check("removing our key keeps theirs", scopes()[f"guild:{G1}"] == {"reply_allowlist": ["*"], "allow_tools": False})


def test_switches_validate_before_writing():
    reset()
    before = json.dumps(channel_config.load_config(), sort_keys=True)
    bad = [
        (DISCORD, "guild", "12ab", "enabled", "off"),
        (DISCORD, "guild", "-1", "enabled", "off"),
        (DISCORD, "server", G1, "enabled", "off"),
        (DISCORD, "guild", G1, "owner", "off"),
        (DISCORD, "guild", G1, "enabled", "true"),
        (INSTAGRAM, "guild", G1, "enabled", "off"),
        ("irc", "guild", G1, "enabled", "off"),
    ]
    for args in bad:
        ok, err, _ = servers.set_switch(*args)
        check(f"refused: {args}", ok is False and err, err)
    check("nothing was written by any refusal",
          json.dumps(channel_config.load_config(), sort_keys=True) == before)


def test_gate_honours_switches_and_siblings_are_unaffected():
    reset()
    check("baseline: friend is answered in server 1",
          permissions.decide(channel_config.platform_config(DISCORD), group_msg(guild=G1, channel=C1)).allowed)
    servers.set_switch(DISCORD, "guild", G1, "enabled", "off")
    cfg = channel_config.platform_config(DISCORD)
    d1 = permissions.decide(cfg, group_msg(guild=G1, channel=C1))
    check("server off -> denied at 'enabled'", not d1.allowed and d1.stage == "enabled", repr(d1))
    check("server 2 is untouched", permissions.decide(cfg, group_msg(guild=G2, channel=C3)).allowed)
    check("a DM is untouched", permissions.decide(cfg, permissions.IncomingMessage(
        DISCORD, permissions.CTX_DM, user_id=FRIEND, text="hi")).allowed)
    check("server-off denial is silent (never 'addressed to us')",
          base.addressed_to_us(d1) is False)


def test_channel_switch_and_thread_inherit_from_channel():
    reset()
    servers.set_switch(DISCORD, "channel", C1, "enabled", "off")
    cfg = channel_config.platform_config(DISCORD)
    check("channel off -> denied there", not permissions.decide(cfg, group_msg(channel=C1)).allowed)
    check("sibling channel still answers", permissions.decide(cfg, group_msg(channel=C2)).allowed)
    thread = permissions.decide(cfg, group_msg(channel=C1, thread="700000000000000007"))
    check("a thread under a switched-off channel is off too", not thread.allowed and thread.stage == "enabled", repr(thread))


def test_tools_switch_turns_tools_off_not_the_reply():
    reset()
    servers.set_switch(DISCORD, "guild", G1, "tools", "off")
    d = permissions.decide(channel_config.platform_config(DISCORD), group_msg())
    check("still answered, tools off", d.allowed and d.may_use_tools is False, repr(d))
    reset({"allow_tools": False})
    servers.set_switch(DISCORD, "guild", G1, "tools", "on")
    d = permissions.decide(channel_config.platform_config(DISCORD), group_msg())
    check("'on' can never switch tools ON over a platform that has them off", d.allowed and d.may_use_tools is False, repr(d))


# ===================================================================== S3

def test_view_effective_state():
    reset()
    servers.note_guild(DISCORD, G1, "Alpha", channels=[(C1, "general", "text"), (C2, "jarvis", "text")])
    servers.note_guild(DISCORD, G2, "Beta", channels=[(C3, "chat", "text")])
    v = servers.view(DISCORD)
    names = [s["name"] for s in v["servers"]]
    check("both servers listed, alphabetical", names == ["Alpha", "Beta"], str(names))
    alpha = v["servers"][0]
    check("answering by default", alpha["answering"] and alpha["tools"] and alpha["mention"])
    check("channels sorted by name", [c["name"] for c in alpha["channels"]] == ["general", "jarvis"])
    servers.set_switch(DISCORD, "guild", G1, "enabled", "off")
    servers.set_switch(DISCORD, "channel", C3, "enabled", "off")
    v = servers.view(DISCORD)
    alpha, beta = v["servers"]
    check("server off shown, with a reason", not alpha["answering"] and "switched off here" in alpha["why_not"], str(alpha["why_not"]))
    check("its channels are off too", all(not c["answering"] for c in alpha["channels"]))
    check("beta answers but its channel is off", beta["answering"] and not beta["channels"][0]["answering"])
    check("override is reported", alpha["override"]["enabled"] == "off" and beta["channels"][0]["override"]["enabled"] == "off")


def test_view_reports_allowed_lists_and_unknown_servers():
    reset({"allowed_guilds": [G1], "allowed_channels": [C1]})
    servers.note_guild(DISCORD, G1, "Alpha", channels=[(C1, "general", "text"), (C2, "other", "text")])
    servers.note_guild(DISCORD, G2, "Beta", channels=[(C3, "chat", "text")])
    v = servers.view(DISCORD)
    by = {s["id"]: s for s in v["servers"]}
    check("a server outside allowed_guilds is not answering",
          not by[G2]["answering"] and "not in allowed_guilds" in by[G2]["why_not"])
    ch = {c["id"]: c for c in by[G1]["channels"]}
    check("a channel outside allowed_channels is not answering", ch[C1]["answering"] and not ch[C2]["answering"])
    reset({"allowed_guilds": [G2], "scopes": {f"channel:{C3}": {"enabled": False}}})
    v = servers.view(DISCORD)
    check("a server only the config mentions is listed, unnamed", any(s["id"] == G2 and not s["known"] for s in v["servers"]))
    check("a config channel in no known server is an orphan", any(o["id"] == C3 for o in v["orphans"]))
    check("instagram reports unsupported", servers.view(INSTAGRAM)["supported"] is False)


def test_view_never_leaks_the_token():
    reset({"bot_token": "SECRET-TOKEN-123"})
    check("view carries no token", "SECRET-TOKEN-123" not in json.dumps(servers.view(DISCORD)))


# ===================================================================== S4

def test_tool_call_record_scrubs_and_clips():
    rec = transcript.tool_call_record("fetch_url", {"url": "http://x", "api_key": "abc", "headers": {"Authorization": "Bearer zzz"}},
                                      {"ok": True, "body": "y" * 5000})
    blob = json.dumps(rec)
    check("secret-looking keys are hidden", "abc" not in blob and "zzz" not in blob and "(hidden)" in blob, blob[:200])
    check("result clipped", len(rec["result"]) <= transcript.MAX_TOOL_RESULT_CHARS + 1 and rec["clipped"] is True)
    check("ok true", rec["ok"] is True)
    bad = transcript.tool_call_record("run_shell", {"cmd": "x"}, {"ok": False, "error": "nope: exit 2"})
    check("failure -> ok false and the error is the shown result", bad["ok"] is False and bad["result"] == "nope: exit 2", str(bad))
    declined = transcript.tool_call_record("delete_file", {"p": "a"}, {"ok": False, "cancelled": True}, {"approved": False})
    check("declined confirm is recorded", declined["ok"] is False and declined["confirm"] == "declined", str(declined))
    unknown = transcript.tool_call_record("web_search", {"q": "x"})
    check("no result -> outcome unknown, not 'ok'", unknown["ok"] is None and unknown["result"] == "")
    check("never raises on odd input", transcript.tool_call_record(None, object(), object())["name"] == "?")


def test_clean_tool_calls_bounds_and_types():
    check("non-list -> []", transcript.clean_tool_calls("x") == [] and transcript.clean_tool_calls(None) == [])
    messy = [{"name": "a", "args": 5, "ok": "yes", "result": None}, "junk", 7, {"name": "b" * 200}]
    out = transcript.clean_tool_calls(messy)
    check("junk items dropped, names bounded, non-bool ok -> None",
          len(out) == 2 and out[0]["ok"] is None and out[0]["args"] == "5" and len(out[1]["name"]) == 64, str(out))
    many = [{"name": f"t{i}"} for i in range(100)]
    check("capped per reply", len(transcript.clean_tool_calls(many)) == transcript.MAX_TOOL_CALLS)


def test_conversation_view_returns_tool_calls_and_old_lines_still_work():
    reset()
    transcript.append(DISCORD, "dm-f", {"dir": "in", "context": "dm", "user_id": FRIEND, "user_handle": "h", "text": "q",
                                        "mentioned": True, "allowed": True, "stage": "allowed", "may_use_tools": True,
                                        "at": "2026-10-05T10:00:00"})
    transcript.log_outbound(DISCORD, "dm-f", "answer", to_user=FRIEND, tool_calls=[
        transcript.tool_call_record("web_search", {"q": "weather"}, {"ok": True, "results": ["a"]})])
    transcript.append(DISCORD, "dm-f", {"dir": "out", "kind": "reply", "text": "old reply", "ok": True,
                                        "to_user": FRIEND, "at": "2026-10-05T10:05:00"})
    out = user_admin.conversation_view(DISCORD, FRIEND)
    by_text = {e["text"]: e for e in out["entries"] if e["dir"] == "out"}   # not by position: log_outbound stamps "now"
    check("the new reply carries its tool calls", by_text["answer"]["tool_calls"][0]["name"] == "web_search"
          and by_text["answer"]["tool_calls"][0]["ok"] is True, str(by_text))
    check("an older reply has an empty list, not a missing key", by_text["old reply"]["tool_calls"] == [])
    transcript.log_outbound(DISCORD, "dm-g", "no tools here", to_user=FRIEND, tool_calls=[])
    last = json.loads(transcript.thread_path(DISCORD, "dm-g").read_text(encoding="utf-8").splitlines()[-1])
    check("a reply with no tools writes no tool_calls key at all", "tool_calls" not in last, str(last))


def test_inbound_keeps_server_and_channel_names():
    reset()
    msg = group_msg()
    msg.guild_name, msg.channel_name = "Alpha", "general"
    transcript.log_inbound(DISCORD, msg, permissions.Decision(True, "allowed", "ok", may_use_tools=True))
    people.touch(DISCORD, FRIEND, handle="friend")
    e = user_admin.conversation_view(DISCORD, FRIEND)["entries"][0]
    check("names ride along on the inbound entry", e["guild_name"] == "Alpha" and e["channel_name"] == "general", str(e))
    check("ids too", e["guild_id"] == G1 and e["channel_id"] == C1)
    plain = group_msg(mid="m2")
    transcript.log_inbound(DISCORD, plain, None)
    line = json.loads(transcript.thread_path(DISCORD, C1).read_text(encoding="utf-8").splitlines()[-1])
    check("no names -> no name keys written", "guild_name" not in line and "channel_name" not in line)


# ===================================================================== S5

class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0, "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": [{"name": "web_search"}]}


def drive(uid, calls, text="hi", thread=C1, cfg_extra=None):
    """Real base.handle_message; the model is a fake that 'calls' `calls`
    through the same tool_log list the real _ask_jarvis fills."""
    orig = base._ask_jarvis, base._conv_id_for, base._notify_owner_new_sender
    sent = []

    def fake_ask(text_, conv_id, may_use_tools, **kw):
        log = kw.get("tool_log")
        if log is not None and may_use_tools:
            for name, args in calls:
                log.append({"name": name, "arguments": args})
        return _Result()
    base._ask_jarvis = fake_ask
    base._conv_id_for = lambda *a, **k: None
    base._notify_owner_new_sender = lambda *a, **k: True
    try:
        msg = group_msg(uid=uid, channel=thread, text=text, mid=f"mid-{len(sent)}-{text}-{thread}")
        base.handle_message(DISCORD, msg, lambda chunk: sent.append(chunk) or True)
    finally:
        base._ask_jarvis, base._conv_id_for, base._notify_owner_new_sender = orig
    return sent


def test_handle_message_logs_tool_calls_on_the_reply():
    reset()
    drive(FRIEND, [("web_search", {"q": "x", "token": "SECRET"}), ("get_datetime", {})], text="one")
    out = [e for e in user_admin.conversation_view(DISCORD, FRIEND)["entries"] if e["dir"] == "out"]
    check("one reply, tool calls attached", len(out) == 1 and [c["name"] for c in out[0]["tool_calls"]] == ["web_search", "get_datetime"], str(out))
    check("captured-only calls have an unknown outcome", out[0]["tool_calls"][0]["ok"] is None)
    check("secrets are scrubbed before they hit disk",
          "SECRET" not in transcript.thread_path(DISCORD, C1).read_text(encoding="utf-8"))


def test_long_reply_puts_tools_on_the_first_chunk_only():
    reset({"max_reply_chars": 50})
    orig = base._ask_jarvis, base._conv_id_for, base._notify_owner_new_sender
    base._ask_jarvis = lambda *a, **k: (k["tool_log"].append({"name": "t", "arguments": {}}), _Result("word " * 60))[1]
    base._conv_id_for = lambda *a, **k: None
    base._notify_owner_new_sender = lambda *a, **k: True
    try:
        base.handle_message(DISCORD, group_msg(mid="long"), lambda c: True)
    finally:
        base._ask_jarvis, base._conv_id_for, base._notify_owner_new_sender = orig
    lines = [json.loads(x) for x in transcript.thread_path(DISCORD, C1).read_text(encoding="utf-8").splitlines()]
    outs = [x for x in lines if x.get("dir") == "out"]
    check("several chunks", len(outs) > 1, str(len(outs)))
    check("only the first carries the tools", "tool_calls" in outs[0] and all("tool_calls" not in x for x in outs[1:]))


def test_tools_not_logged_when_conversations_are_not_logged():
    reset({"log_conversations": False})
    drive(FRIEND, [("web_search", {"q": "x"})], text="two")
    check("nothing written to the transcript at all", not transcript.thread_path(DISCORD, C1).exists())


def test_a_person_without_tools_has_no_tool_calls():
    reset({"tool_allowlist": [OWNER]})
    drive(FRIEND, [("web_search", {"q": "x"})], text="three")
    out = [e for e in user_admin.conversation_view(DISCORD, FRIEND)["entries"] if e["dir"] == "out"]
    check("answered, with no tool calls", len(out) == 1 and out[0]["tool_calls"] == [], str(out))


def test_raw_archive_supplies_outcomes_when_present():
    """_tool_calls_for_log prefers raw_archive tool_run events since the ask
    started, because only they know the result."""
    from jarvis import raw_archive
    reset()
    since = base._utc_stamp()
    orig_read = raw_archive.read
    raw_archive.read = lambda conv_id, kinds=None: [
        {"kind": "tool_run", "ts": "2000-01-01T00:00:00.000+00:00", "name": "old", "arguments": {}, "result": {"ok": True}},
        {"kind": "tool_run", "ts": base._utc_stamp(), "name": "web_search", "arguments": {"q": "x"}, "result": {"ok": False, "error": "boom"}},
    ]
    try:
        calls = base._tool_calls_for_log([{"name": "web_search", "arguments": {"q": "x"}}], "conv123456", since)
    finally:
        raw_archive.read = orig_read
    check("old events are ignored, the new one is used",
          len(calls) == 1 and calls[0]["name"] == "web_search" and calls[0]["ok"] is False and calls[0]["result"] == "boom", str(calls))
    check("no conversation id -> falls back to captured calls",
          base._tool_calls_for_log([{"name": "a", "arguments": {}}], None, since)[0]["name"] == "a")
    check("never raises", base._tool_calls_for_log(None, None, since) == [])


# ===================================================================== S6

class _Chan:
    def __init__(self, cid, name="general", parent=None):
        self.id, self.name = cid, name
        self.parent_id = parent.id if parent else None
        self.parent = parent


class _Thread(_Chan):
    pass


class _FakeDiscord:
    Thread = _Thread


class _Guild:
    def __init__(self, gid, name):
        self.id, self.name = gid, name


class _Msg:
    def __init__(self, channel, guild):
        self.channel, self.guild = channel, guild


def test_gateway_routes_a_thread_to_its_parent_channel():
    parent = _Chan(int(C1), "general")
    thread = _Thread(777, "side-chat", parent=parent)
    guild = _Guild(int(G1), "Alpha")
    g, channel_id, thread_id, name = discord_gateway._where(_FakeDiscord, _Msg(thread, guild))
    check("a thread reports its parent as the channel", channel_id == C1 and thread_id == "777" and name == "general" and g == "Alpha",
          str((g, channel_id, thread_id, name)))
    g, channel_id, thread_id, name = discord_gateway._where(_FakeDiscord, _Msg(parent, guild))
    check("a plain channel is unchanged: thread_id == channel_id", channel_id == C1 and thread_id == C1)
    g, channel_id, thread_id, name = discord_gateway._where(_FakeDiscord, _Msg(_Chan(5, "dm"), None))
    check("a DM has no guild name", g == "" and channel_id == "5")
    orphan = _Thread(778, "t", parent=None)
    _, channel_id, thread_id, _ = discord_gateway._where(_FakeDiscord, _Msg(orphan, guild))
    check("a thread with no parent id falls back to itself", channel_id == "778" and thread_id == "778")


def test_thread_inside_allowed_channel_is_allowed_and_outside_is_not():
    reset({"allowed_channels": [C1]})
    cfg = channel_config.platform_config(DISCORD)
    inside = permissions.decide(cfg, group_msg(channel=C1, thread="777"))
    outside = permissions.decide(cfg, group_msg(channel=C2, thread="778"))
    check("thread under an allowed channel passes the where-gate", inside.allowed, repr(inside))
    check("thread under another channel does not", not outside.allowed and outside.stage == "where", repr(outside))


# ===================================================================== S7

def cli(*argv):
    from jarvis import channels_cli
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            channels_cli.handle(list(argv))
        except SystemExit as exc:
            code = exc.code or 0
    try:
        return code, json.loads(buf.getvalue())
    except ValueError:
        return code, {"raw": buf.getvalue()}


def test_cli_commands():
    reset()
    servers.note_guild(DISCORD, G1, "Alpha", channels=[(C1, "general", "text")])
    code, out = cli("channels-servers")
    check("channels-servers lists", code == 0 and out["servers"][0]["name"] == "Alpha", str(out))
    code, out = cli("channels-server-set", DISCORD, "guild", G1, "enabled", "off")
    check("set off", code == 0 and out["ok"] and out["override"] is False, str(out))
    code, out = cli("channels-servers", DISCORD)
    check("and the list reflects it", out["servers"][0]["answering"] is False)
    code, out = cli("channels-server-set", DISCORD, "guild", G1, "mention", "off")
    check("refusal exits 1 with ok false", code == 1 and out["ok"] is False, str(out))
    code, out = cli("channels-server-set", DISCORD, "guild")
    check("usage error", code == 1 and out["ok"] is False)
    code, out = cli("channels-servers", "irc")
    check("unknown platform", code == 1 and out["ok"] is False)
    from jarvis import channels_cli, reserved_names
    names = getattr(reserved_names, "RESERVED_COMMAND_NAMES", None) or getattr(reserved_names, "RESERVED", None) or set()
    for cmd in ("channels-servers", "channels-server-set"):
        check(f"{cmd} is dispatched", cmd in channels_cli.COMMANDS)
        check(f"{cmd} is reserved", (cmd in names) if names else True)


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
