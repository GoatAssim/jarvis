"""Tests for the space model (channels/space.py) and registry (channels/spaces.py).

    python3 tests/test_channel_spaces.py

  S2.1  kind classification         every case, incl. Discord thread vs channel
  S2.2  titles and speaker labels   pure helpers, clipped, never empty
  S2.3  the registry                names only, no write when nothing changed,
                                    a rename is reported once, an empty name
                                    never wipes a stored one, corrupt file ok
  S2.4  privacy                     an unaddressed message is not registered;
                                    DMs are not registered; saving off -> none
  S2.5  rebuild()                   the registry comes back from transcripts
  S2.6  conversation labels         a room's conversation is titled by its
                                    name; a DM's title is unchanged; a rename
                                    refreshes the origin label

No bot token, no socket, no model, no real ~/.jarvis: HOME is redirected to a
temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-spaces-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import conversations                                              # noqa: E402
from jarvis.channels import (DISCORD, INSTAGRAM, base, people, permissions,   # noqa: E402
                             space, spaces, transcript)
from jarvis.channels import config as channel_config                          # noqa: E402
from jarvis.channels import instagram_gateway as ig                           # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003"
G1, C1, C2, T1 = "900000000000000001", "800000000000000001", "800000000000000002", "700000000000000001"
IG_USER, IG_GROUP = "17841400000000001", "t9-group-thread"


def wipe():
    root = Path(_HOME) / ".jarvis"
    for _ in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
    spaces._cache.clear()
    transcript.reset_cooldowns()


def reset(extra=None):
    wipe()
    for uid, handle in ((OWNER, "boss"), (FRIEND, "friend")):
        people.touch(DISCORD, uid, handle=handle)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [], "require_mention": True,
    })
    cfg[DISCORD].update(extra or {})
    channel_config.save_config(cfg)


def room_msg(uid=FRIEND, guild=G1, channel=C1, thread=None, text="hello", mid="m1",
             mentioned=True, guild_name="My Server", channel_name="general",
             thread_name="", author_name="", handle="friend"):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=uid, user_handle=handle, text=text,
        mentioned=mentioned, guild_id=guild, channel_id=channel, message_id=mid,
        thread_id=thread or channel, guild_name=guild_name, channel_name=channel_name,
        thread_name=thread_name, author_name=author_name)


def dm_msg(uid=FRIEND, thread="dm-chan-1", mid="d1", text="hi"):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_DM, user_id=uid, user_handle="friend", text=text,
        mentioned=True, channel_id=thread, message_id=mid, thread_id=thread)


class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
                            "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": []}


def drive(msg, cfg=None):
    """Real base.handle_message and real _conv_id_for; only the model and the
    owner ping are faked."""
    saved = base._ask_jarvis, base._notify_owner_new_sender
    base._ask_jarvis = lambda *a, **k: _Result()
    base._notify_owner_new_sender = lambda *a, **k: True
    sent = []
    try:
        decision = base.handle_message(DISCORD, msg, lambda chunk: sent.append(chunk) or True,
                                       cfg=cfg)
    finally:
        base._ask_jarvis, base._notify_owner_new_sender = saved
    return decision, sent


def spaces_file():
    return spaces.registry_file()


# ===================================================================== S2.1

def test_kind_classification():
    check("a Discord DM is a dm", space.kind_of(dm_msg()) == space.KIND_DM)
    check("a Discord server channel", space.kind_of(room_msg()) == space.KIND_CHANNEL)
    check("a Discord thread (own id, parent as channel)",
          space.kind_of(room_msg(thread=T1)) == space.KIND_THREAD)
    check("a thread whose id equals its channel is a channel, not a thread",
          space.kind_of(room_msg(thread=C1)) == space.KIND_CHANNEL)
    no_guild = permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=FRIEND, channel_id=C1, thread_id=C1)
    check("a Discord non-DM with no guild falls to group", space.kind_of(no_guild) == space.KIND_GROUP)
    igg = permissions.IncomingMessage(
        INSTAGRAM, permissions.CTX_GROUP, user_id=IG_USER, channel_id=IG_GROUP,
        thread_id=IG_GROUP)
    check("an Instagram group", space.kind_of(igg) == space.KIND_GROUP)
    igd = permissions.IncomingMessage(
        INSTAGRAM, permissions.CTX_DM, user_id=IG_USER, thread_id=IG_USER)
    check("an Instagram DM", space.kind_of(igd) == space.KIND_DM)
    mention = ig.parse_events(
        {"entry": [{"changes": [{"field": "mentions", "value": {
            "from": {"id": IG_USER, "username": "alice"}, "text": "@jarvis hi",
            "comment_id": "c1"}}]}]}, bot_id="9", bot_handle="jarvis")[0]
    check("an Instagram comment mention is a group-kind space",
          space.kind_of(mention) == space.KIND_GROUP)
    check("is_room: rooms yes, DMs no",
          space.is_room(room_msg()) and not space.is_room(dm_msg()))
    check("the mention prefix matches the gateway's",
          space.MENTION_THREAD_PREFIX == ig.MENTION_THREAD_PREFIX)


# ===================================================================== S2.2

def test_titles():
    check("channel title", space.title_for(room_msg()) == "#general · My Server",
          space.title_for(room_msg()))
    check("thread title", space.title_for(room_msg(thread=T1, thread_name="deploy talk"))
          == "Thread 'deploy talk' · #general · My Server",
          space.title_for(room_msg(thread=T1, thread_name="deploy talk")))
    check("unnamed thread falls back to its id tail",
          space.title_for(room_msg(thread=T1)).startswith("Thread 000001"),
          space.title_for(room_msg(thread=T1)))
    check("no guild name: channel only",
          space.title_for(room_msg(guild_name="")) == "#general")
    check("no names at all: channel id tail",
          space.title_for(room_msg(guild_name="", channel_name="")) == "Channel 000001",
          space.title_for(room_msg(guild_name="", channel_name="")))
    igg = permissions.IncomingMessage(
        INSTAGRAM, permissions.CTX_GROUP, user_id=IG_USER, channel_id=IG_GROUP,
        thread_id=IG_GROUP)
    check("Instagram group with no platform title: id tail",
          space.title_for(igg) == "Group thread", space.title_for(igg))
    nothing = permissions.IncomingMessage(INSTAGRAM, permissions.CTX_GROUP, user_id=IG_USER)
    check("a group with no id at all is still titled", space.title_for(nothing) == "Group chat")
    check("a DM has no room title (the caller keeps its own)", space.title_for(dm_msg()) == "")
    men = ig.parse_events(
        {"entry": [{"changes": [{"field": "mentions", "value": {
            "from": {"id": IG_USER, "username": "alice"}, "text": "@jarvis hi",
            "comment_id": "c1"}}]}]}, bot_id="9", bot_handle="jarvis")[0]
    check("mention threads are titled by who mentioned", space.title_for(men) == "Mentions · @alice",
          space.title_for(men))
    long = room_msg(guild_name="G" * 200, channel_name="c" * 200)
    check("titles are clipped to what a conversation can hold",
          len(space.title_for(long)) <= space.MAX_TITLE_CHARS)
    messy = room_msg(channel_name="gen\neral [x]", guild_name="My\tServer")
    check("control characters and brackets are cleaned",
          "\n" not in space.title_for(messy) and "[" not in space.title_for(messy)
          and "\t" not in space.title_for(messy), space.title_for(messy))


def test_speaker_record_and_label():
    entry = {"user_id": FRIEND, "handle": "friend", "name": "Alice"}
    rec = space.speaker_record(entry, room_msg(), False)
    check("record has exactly the four fields",
          set(rec) == {"id", "name", "handle", "owner"}, str(rec))
    check("name comes from what Jarvis knows them as", rec["name"] == "Alice")
    check("owner flag stored", space.speaker_record(entry, room_msg(), True)["owner"] is True)
    rec = space.speaker_record({"user_id": FRIEND}, room_msg(author_name="Bob D."), False)
    check("falls back to the platform display name", rec["name"] == "Bob D.", str(rec))
    rec = space.speaker_record({}, room_msg(handle="@zed"), False)
    check("handle is stored without the @", rec["handle"] == "zed", str(rec))
    check("an oversized name is clipped when stored",
          len(space.speaker_record({"name": "N" * 500}, room_msg(), False)["name"])
          == space.MAX_NAME_CHARS)

    label = space.speaker_label
    check("name wins", label({"name": "Alice", "handle": "al", "id": "1234567"}) == "Alice")
    check("then @handle", label({"handle": "al", "id": "1234567"}) == "@al")
    check("then user…last4", label({"id": "123456789"}) == "user…6789", label({"id": "123456789"}))
    check("a missing speaker is still labelled", label(None) == "someone")
    check("owner suffix", label({"name": "Alice", "owner": True}) == "Alice (owner)")
    check("is_owner overrides the stored flag",
          label({"name": "Alice", "owner": True}, is_owner=False) == "Alice")
    check("brackets and newlines never reach the label",
          label({"name": "A[li]\nce"}) == "A(li) ce", label({"name": "A[li]\nce"}))
    long = label({"name": "x" * 100, "owner": True})
    check("the whole label, owner suffix included, fits the cap",
          len(long) <= space.MAX_LABEL_CHARS and long.endswith("(owner)"), long)
    check("an unowned long label is capped too",
          len(label({"name": "x" * 100})) == space.MAX_LABEL_CHARS)


# ===================================================================== S2.3

def test_registry_records_names_once():
    reset()
    out = spaces.note(DISCORD, room_msg(), is_owner=False)
    check("first sighting is written", out and out["written"] is True, str(out))
    rec = spaces.get(DISCORD, C1)
    check("kind, names and ids stored",
          rec["kind"] == space.KIND_CHANNEL and rec["guild_name"] == "My Server"
          and rec["channel_name"] == "general" and rec["guild_id"] == G1
          and rec["channel_id"] == C1, str(rec))
    check("title stored", rec["title"] == "#general · My Server")
    check("the speaker is recorded by id", rec["speakers"] == [FRIEND])
    check("no message text anywhere in the file",
          "hello" not in spaces_file().read_text(encoding="utf-8"))
    out = spaces.note(DISCORD, room_msg(), is_owner=False)
    check("the same facts again are not a write", out["written"] is False and not out["renamed"], str(out))
    out = spaces.note(DISCORD, room_msg(uid=OWNER, handle="boss"))
    check("a new speaker IS a write", out["written"] is True)
    check("both speakers kept in order", spaces.get(DISCORD, C1)["speakers"] == [FRIEND, OWNER])
    out = spaces.note(DISCORD, room_msg(channel_name="chat"))
    check("a channel rename is written and reported",
          out["written"] is True and out["renamed"] is True, str(out))
    check("the title follows the rename", spaces.get(DISCORD, C1)["title"] == "#chat · My Server")
    out = spaces.note(DISCORD, room_msg(channel_name=""))
    check("a message that arrives without a name has not renamed the room",
          out["renamed"] is False and spaces.get(DISCORD, C1)["channel_name"] == "chat", str(out))
    check("the stored title survived the empty name",
          spaces.get(DISCORD, C1)["title"] == "#chat · My Server", spaces.get(DISCORD, C1)["title"])
    out = spaces.note(DISCORD, room_msg(channel_name="", guild_name=""))
    check("neither name given: nothing downgraded, nothing written",
          out["written"] is False and spaces.get(DISCORD, C1)["title"] == "#chat · My Server", str(out))


def test_threads_and_instagram_groups_are_rooms_of_their_own():
    reset()
    spaces.note(DISCORD, room_msg(thread=T1, thread_name="deploy talk"))
    rec = spaces.get(DISCORD, T1)
    check("a thread is its own entry", rec and rec["kind"] == space.KIND_THREAD, str(rec))
    check("the thread keeps its parent channel id and name",
          rec["channel_id"] == C1 and rec["channel_name"] == "general"
          and rec["thread_name"] == "deploy talk")
    check("the parent channel is not registered by its thread", spaces.get(DISCORD, C1) is None)
    igg = permissions.IncomingMessage(
        INSTAGRAM, permissions.CTX_GROUP, user_id=IG_USER, user_handle="alice",
        channel_id=IG_GROUP, thread_id=IG_GROUP, mentioned=True)
    check("an Instagram group registers", spaces.note(INSTAGRAM, igg)["written"] is True)
    check("under its own platform", spaces.get(INSTAGRAM, IG_GROUP)["kind"] == space.KIND_GROUP
          and spaces.get(DISCORD, IG_GROUP) is None)
    check("read() is per platform",
          list(spaces.read(DISCORD)) == [T1] and list(spaces.read(INSTAGRAM)) == [IG_GROUP])
    check("get() finds an id as it appears in a file name too",
          spaces.get(INSTAGRAM, transcript._safe_name(IG_GROUP)) is not None)


def test_speakers_are_capped():
    reset()
    for i in range(spaces.MAX_SPEAKERS + 10):
        spaces.note(DISCORD, room_msg(uid=str(5000 + i), handle=f"u{i}"))
    speakers = spaces.get(DISCORD, C1)["speakers"]
    check("at most the cap is kept", len(speakers) == spaces.MAX_SPEAKERS, str(len(speakers)))
    check("the oldest are dropped, the newest kept",
          speakers[-1] == str(5000 + spaces.MAX_SPEAKERS + 9) and "5000" not in speakers)


def test_corrupt_registry_degrades_to_empty():
    reset()
    spaces.note(DISCORD, room_msg())
    spaces_file().write_text("{not json", encoding="utf-8")
    check("a corrupt file reads as empty", spaces.read(DISCORD) == {})
    check("get() on a corrupt file is None, not an error", spaces.get(DISCORD, C1) is None)
    spaces._cache.clear()
    out = spaces.note(DISCORD, room_msg())
    check("noting again repairs it", out and out["written"] is True
          and spaces.get(DISCORD, C1) is not None)
    spaces_file().write_text("[1, 2]", encoding="utf-8")
    check("a wrong-shaped file reads as empty too", spaces.read(DISCORD) == {})
    spaces_file().write_text(json.dumps({DISCORD: "nope"}), encoding="utf-8")
    check("a wrong-shaped platform block reads as empty", spaces.read(DISCORD) == {})
    spaces._cache.clear()
    check("and does not stop the next note", spaces.note(DISCORD, room_msg())["written"] is True)


# ===================================================================== S3.1

def _in_line(thread):
    return [r for r in transcript.read_thread(DISCORD, thread) if r.get("dir") == "in"][-1]


def test_inbound_lines_gain_room_fields_only_when_set():
    reset()
    drive(room_msg(mid="v1"))
    line = _in_line(C1)
    check("a room line records its kind", line.get("space") == space.KIND_CHANNEL, str(line))
    for key in ("author_name", "thread_name", "reply_to", "attachments"):
        check(f"{key} absent when unset", key not in line)
    drive(room_msg(thread=T1, thread_name="deploy talk", author_name="Alice D.", mid="v2"))
    line = _in_line(T1)
    check("a thread line records kind, thread name and author name",
          line.get("space") == space.KIND_THREAD and line.get("thread_name") == "deploy talk"
          and line.get("author_name") == "Alice D.", str(line))
    m = room_msg(mid="v3")
    m.reply_to = {"message_id": "6999", "user_id": OWNER, "user_name": "Bob",
                  "snippet": "x" * 500, "junk": "dropped"}
    m.attachments = ("a.png", "b.png")
    drive(m)
    line = _in_line(C1)
    check("reply_to is stored with exactly its four keys",
          set(line["reply_to"]) == {"message_id", "user_id", "user_name", "snippet"}, str(line))
    check("the snippet is clipped to 120", len(line["reply_to"]["snippet"]) == 120)
    check("attachments is a count, not names", line.get("attachments") == 2, str(line))
    m = room_msg(mid="v4")
    m.reply_to = {"message_id": "", "snippet": ""}
    drive(m)
    check("an empty reply_to is left out", "reply_to" not in _in_line(C1))


def test_dm_lines_keep_their_exact_old_shape():
    reset()
    drive(dm_msg())
    line = _in_line("dm-chan-1")
    check("a DM line has no space field", "space" not in line, str(line))
    check("a DM line has none of the new keys",
          not ({"author_name", "thread_name", "reply_to", "attachments"} & set(line)), str(line))
    check("its old keys are all still there",
          {"dir", "context", "user_id", "user_handle", "text", "mentioned", "guild_id",
           "channel_id", "message_id", "allowed", "stage", "reason", "may_use_tools",
           "conv_id"} <= set(line))


def test_a_denied_room_line_carries_the_fields_too():
    reset()
    drive(room_msg(uid=STRANGER, handle="stranger", mid="z1"))
    line = _in_line(C1)
    check("denied and still described", line.get("allowed") is False
          and line.get("space") == space.KIND_CHANNEL, str(line))


def test_rebuild_trusts_the_logged_kind():
    reset()
    drive(room_msg(thread=T1, thread_name="deploy", mid="k1"))
    spaces_file().unlink()
    spaces._cache.clear()
    check("kind comes back from the line's own space field",
          spaces.rebuild(DISCORD)[T1]["kind"] == space.KIND_THREAD)
    check("and its thread name", spaces.read(DISCORD)[T1]["thread_name"] == "deploy")


# ===================================================================== S2.4

def test_dms_and_bad_input_are_not_registered():
    reset()
    check("a DM is not registered", spaces.note(DISCORD, dm_msg()) is None)
    check("an unknown platform is not registered", spaces.note("signal", room_msg()) is None)
    nothing = permissions.IncomingMessage(DISCORD, permissions.CTX_GROUP, user_id=FRIEND)
    check("a room with no id is not registered", spaces.note(DISCORD, nothing) is None)
    check("nothing was written", not spaces_file().exists())
    check("note() never raises on garbage", spaces.note(DISCORD, None) is None)


def test_unaddressed_message_is_never_registered():
    reset()
    quiet = room_msg(mentioned=False, text="just chatting", mid="q1")
    decision, sent = drive(quiet)
    check("the gate still drops it unseen", decision.allowed is False
          and decision.stage == "reachable", f"{decision.stage}")
    check("no reply", sent == [])
    check("not registered", not spaces_file().exists())
    check("not logged", not transcript.thread_path(DISCORD, C1).exists())
    drive(room_msg(mentioned=True, text="hello", mid="a1"))
    check("an addressed one in the same room is registered",
          spaces.get(DISCORD, C1) is not None)
    check("the unmentioned author is not on the speaker list",
          STRANGER not in spaces.get(DISCORD, C1)["speakers"])
    drive(room_msg(uid=STRANGER, mentioned=False, text="psst", mid="q2", handle="stranger"))
    check("an unaddressed stranger never joins the speaker list",
          spaces.get(DISCORD, C1)["speakers"] == [FRIEND], str(spaces.get(DISCORD, C1)))


def test_a_denied_but_addressed_message_registers_the_room():
    """Addressed means mentioned or DMed, allowed or not: the transcript logs
    it too, so the room is a room Jarvis was addressed in."""
    reset()
    drive(room_msg(uid=STRANGER, handle="stranger", mentioned=True, mid="d9"))
    check("the room is registered", spaces.get(DISCORD, C1) is not None)
    check("with the stranger as its speaker",
          spaces.get(DISCORD, C1)["speakers"] == [STRANGER])


def test_saving_off_means_no_registration():
    reset({"log_conversations": False})
    drive(room_msg(mid="s1"))
    check("nothing registered when conversations are not saved", not spaces_file().exists())
    check("nothing logged either", not transcript.thread_path(DISCORD, C1).exists())


def test_dm_through_the_pipeline_is_not_registered():
    reset()
    drive(dm_msg())
    check("a DM leaves no registry file", not spaces_file().exists())


# ===================================================================== S2.5

def test_rebuild_reproduces_the_registry():
    reset()
    drive(room_msg(mid="r1", text="one"))
    drive(room_msg(uid=OWNER, handle="boss", mid="r2", text="two"))
    drive(room_msg(channel=C2, channel_name="random", mid="r3", text="three"))
    drive(room_msg(thread=T1, thread_name="deploy", mid="r4", text="four"))
    drive(dm_msg(mid="r5"))
    before = spaces.read(DISCORD)
    check("three rooms registered, no DM", sorted(before) == sorted([C1, C2, T1]), str(sorted(before)))
    spaces_file().unlink()
    spaces._cache.clear()
    check("gone", spaces.read(DISCORD) == {})
    rebuilt = spaces.rebuild(DISCORD)
    check("rebuild finds the same rooms", sorted(rebuilt) == sorted(before), str(sorted(rebuilt)))
    for tid in (C1, C2):
        a, b = before[tid], spaces.read(DISCORD)[tid]
        check(f"{tid}: kind, title and speakers match",
              (a["kind"], a["title"], a["speakers"]) == (b["kind"], b["title"], b["speakers"]),
              f"{a} vs {b}")
    check("the thread comes back as a thread", spaces.read(DISCORD)[T1]["kind"] == space.KIND_THREAD)
    check("the DM thread is not in it", "dm-chan-1" not in spaces.read(DISCORD))
    check("rebuild wrote the file", spaces_file().exists())
    check("first/last seen come from the log lines",
          spaces.read(DISCORD)[C1]["first_seen"] and spaces.read(DISCORD)[C1]["last_seen"])


def test_rebuild_keeps_imported_entries_and_survives_nothing_to_read():
    reset()
    check("rebuild with no transcripts is an empty block", spaces.rebuild(DISCORD) == {})
    data = {DISCORD: {"import-abc": {"kind": "group", "title": "Old export", "imported": True,
                                      "speakers": []}}}
    spaces_file().write_text(json.dumps(data), encoding="utf-8")
    drive(room_msg(mid="x1"))
    rebuilt = spaces.rebuild(DISCORD)
    check("an imported entry survives a rebuild", "import-abc" in rebuilt and C1 in rebuilt, str(sorted(rebuilt)))
    check("an unknown platform rebuilds to nothing", spaces.rebuild("signal") == {})


# ===================================================================== S2.6

def _conv_of(thread):
    lines = [r for r in transcript.read_thread(DISCORD, thread) if r.get("dir") == "in"]
    return lines[-1].get("conv_id") if lines else None


def test_room_conversations_are_titled_by_name_and_dms_are_not():
    reset()
    drive(room_msg(mid="t1"))
    conv = conversations.get_conversation(_conv_of(C1))
    check("a room's conversation carries its name", conv and conv["title"] == "#general · My Server",
          str(conv and conv.get("title")))
    drive(dm_msg(mid="t2"))
    dm_conv = conversations.get_conversation(_conv_of("dm-chan-1"))
    check("a DM's conversation title is exactly what it always was",
          dm_conv and dm_conv["title"] == "discord:dm-chan-1", str(dm_conv and dm_conv.get("title")))


def test_a_rename_refreshes_the_origin_label_once():
    reset()
    drive(room_msg(mid="n1"))
    conv_id = _conv_of(C1)
    before = conversations.get_conversation(conv_id)["origin_detail"]
    check("created with the first name", "#general" in before, before)
    drive(room_msg(mid="n2", channel_name="chat"))
    after = conversations.get_conversation(conv_id)["origin_detail"]
    check("the origin label follows the rename", "#chat" in after and "#general" not in after, after)
    check("still the same conversation", _conv_of(C1) == conv_id)
    drive(room_msg(mid="n3", channel_name="chat"))
    check("an unchanged name changes nothing",
          conversations.get_conversation(conv_id)["origin_detail"] == after)


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
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
