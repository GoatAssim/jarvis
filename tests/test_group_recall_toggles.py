"""Tests for recalled notes and memories in shared rooms (channel spaces S6 / D2).

    python3 tests/test_group_recall_toggles.py

Rule: in a server channel / thread / group chat a saved NOTE (people.json) or
remembered FACT (person_memory) is used only if its own flag says on, or it is
inherit and the platform's `group_recall` switch is on. off always wins; on wins over a
master that is off. The name, the owner's style note and standing rules always apply.
DMs behave exactly as before.

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

_HOME = tempfile.mkdtemp(prefix="jarvis-grouprecall-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
_CLI = str(Path(__file__).resolve().parent.parent / "jarvis-cli")
sys.path.insert(0, _CLI)

from jarvis.actions import channel_people                                    # noqa: E402
from jarvis.channels import (DISCORD, INSTAGRAM, base, changelog, people,    # noqa: E402
                             permissions, person_memory, transcript, user_admin)
from jarvis.channels import config as channel_config                         # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND, IG = "100000000000000001", "200000000000000002", "igfriend1"
G1, C1 = "900000000000000001", "800000000000000001"
NOTE = "likes chess"
FACT = "plays the cello in an orchestra"
ASK = "what do you know about my chess and my cello?"


def wipe():
    root = Path(_HOME) / ".jarvis"
    for _ in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
    transcript.reset_cooldowns()


def reset(extra=None):
    """Owner + a friend with one note and one remembered fact."""
    wipe()
    people.touch(DISCORD, OWNER, handle="boss", is_owner=True)
    people.touch(DISCORD, FRIEND, handle="friend")
    people.set_name(DISCORD, FRIEND, "Fred")
    people.add_note(DISCORD, FRIEND, NOTE)
    rec, err = person_memory.add(people.key(DISCORD, FRIEND), "fact", FACT)
    assert rec and not err, err
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [], "require_mention": True,
    })
    cfg[DISCORD].update(extra or {})
    channel_config.save_config(cfg)
    return rec["id"]


def note_id():
    return people.note_id(NOTE)


def room_msg(text=ASK, mid="m1", uid=FRIEND):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=uid, user_handle="friend", text=text,
        mentioned=True, guild_id=G1, channel_id=C1, message_id=mid, thread_id=C1,
        guild_name="My Server", channel_name="general")


def dm_msg(text=ASK, mid="d1"):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_DM, user_id=FRIEND, user_handle="friend", text=text,
        mentioned=True, channel_id="dm-1", message_id=mid, thread_id="dm-1")


class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
                            "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": []}


def drive(msg, draft="the answer"):
    """Real base.handle_message; only the model and the owner ping are faked.
    Returns (sender_context, calls, sent_chunks)."""
    saved = base._ask_jarvis, base._notify_owner_new_sender
    calls, sent = [], []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return _Result(draft)

    base._ask_jarvis = fake
    base._notify_owner_new_sender = lambda *a, **k: True
    try:
        base.handle_message(DISCORD, msg, lambda chunk: sent.append(chunk) or True)
    finally:
        base._ask_jarvis, base._notify_owner_new_sender = saved
    ctx = calls[0][1].get("sender_context", "") if calls else ""
    return ctx, calls, sent


def set_master(on):
    ok, err = channel_config.set_value(DISCORD, "group_recall", on)
    assert ok, err


# ------------------------------------------------------------ the master switch

def test_default_room_uses_no_notes_or_memory():
    reset()
    ctx, calls, _ = drive(room_msg())
    check("a message was handled", bool(calls))
    check("the note stays out of a room by default", NOTE not in ctx, ctx)
    check("the remembered fact stays out by default", "cello" not in ctx, ctx)
    check("the name is still used", "Fred" in ctx, ctx)
    check("the room framing is there", "WHERE: this is a shared" in ctx, ctx)


def test_master_on_uses_inherit_items():
    reset()
    set_master(True)
    ctx, _, _ = drive(room_msg())
    check("note used with the master on", NOTE in ctx, ctx)
    check("fact used with the master on", "cello" in ctx, ctx)


def test_per_item_on_wins_over_master_off():
    rid = reset()
    check("note flag set", user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "on")[0])
    ctx, _, _ = drive(room_msg())
    check("an `on` note is used with the master off", NOTE in ctx, ctx)
    check("the unflagged fact is still out", "cello" not in ctx, ctx)
    check("memory flag set", user_admin.set_memory_groups(DISCORD, FRIEND, rid, "on")[0])
    ctx, _, _ = drive(room_msg(mid="m2"))
    check("an `on` fact is used with the master off", "cello" in ctx, ctx)


def test_per_item_off_wins_over_master_on():
    rid = reset()
    set_master(True)
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "off")
    ctx, _, _ = drive(room_msg())
    check("an `off` note is never used", NOTE not in ctx, ctx)
    check("the inherit fact still follows the master", "cello" in ctx, ctx)
    user_admin.set_memory_groups(DISCORD, FRIEND, rid, "off")
    ctx, _, _ = drive(room_msg(mid="m2"))
    check("an `off` fact is never used", "cello" not in ctx, ctx)


def test_inherit_resets():
    rid = reset()
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "on")
    user_admin.set_memory_groups(DISCORD, FRIEND, rid, "on")
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "inherit")
    user_admin.set_memory_groups(DISCORD, FRIEND, rid, "inherit")
    ctx, _, _ = drive(room_msg())
    check("back to following the master (off)", NOTE not in ctx and "cello" not in ctx, ctx)
    check("the memory field is removed, not stored as inherit",
          "groups" not in person_memory.get(people.key(DISCORD, FRIEND), rid))


def test_dm_is_unchanged():
    reset()
    ctx, calls, _ = drive(dm_msg())
    check("a DM still gets the note with the master off", NOTE in ctx, ctx)
    check("a DM still gets the fact", "cello" in ctx, ctx)
    check("a DM has no room framing", "WHERE" not in ctx, ctx)
    env = json.loads(calls[0][1].get("sender_env") or "{}") if "sender_env" in calls[0][1] else {}
    check("a DM sender env carries no room_recall key", "room_recall" not in env, str(env))
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "off")
    ctx, _, _ = drive(dm_msg(mid="d2"))
    check("an `off` flag does not affect a DM", NOTE in ctx, ctx)


def test_identical_prompt_block_for_dms():
    reset()
    entry = people.get(DISCORD, FRIEND)
    check("prompt_block default equals room_recall=None",
          people.prompt_block(entry, DISCORD) == people.prompt_block(entry, DISCORD, room_recall=None))
    check("a room with the master on equals the DM text",
          people.prompt_block(entry, DISCORD, room_recall=True) == people.prompt_block(entry, DISCORD))


# ------------------------------------------------------------ what is always allowed

def test_rules_and_style_note_always_apply_in_a_room():
    reset()
    key = people.key(DISCORD, FRIEND)
    rec, err = person_memory.add_instruction(key, "", "tone", text="keep it short")
    check("rule created", rec is not None, str(err))
    people.set_instruction(DISCORD, FRIEND, "be formal with them")
    ctx, _, _ = drive(room_msg(text="hello there"))
    check("the standing rule is used in a room with the master off", "keep it short" in ctx, ctx)
    check("the owner's style note is used", "be formal with them" in ctx, ctx)


def test_hard_rule_still_withholds_a_leaking_draft_in_a_room():
    reset()
    key = people.key(DISCORD, FRIEND)
    rec, err = person_memory.add_instruction(key, "birthday surprise", "don_t_discuss", hard=True)
    check("hard rule created", rec is not None, str(err))
    from jarvis.channels import cheap_call
    saved = cheap_call.ask_yes_no
    cheap_call.ask_yes_no = lambda *a, **k: True        # the model would say "yes, it leaks"
    try:
        _ctx, _calls, sent = drive(room_msg(text="is there a birthday surprise planned?"),
                                   draft="Yes, the birthday surprise is on Friday.")
    finally:
        cheap_call.ask_yes_no = saved
    check("the leaking draft was not posted", sent and "Friday" not in " ".join(sent), str(sent))


# ------------------------------------------------------------ linked accounts, bookkeeping

def test_linked_account_notes_obey_their_own_flags():
    reset()
    people.touch(INSTAGRAM, IG, handle="fredig")
    people.add_note(INSTAGRAM, IG, "owns a red bicycle")
    ok, err, _ = people.link(DISCORD, FRIEND, INSTAGRAM, IG)
    check("linked", ok, str(err))
    ctx, _, _ = drive(room_msg(text="hello"))
    check("the other account's note is also withheld in a room", "red bicycle" not in ctx, ctx)
    people.set_note_groups(INSTAGRAM, IG, people.note_id("owns a red bicycle"), "on")
    ctx, _, _ = drive(room_msg(text="hello", mid="m2"))
    check("an `on` note on the linked account is used", "red bicycle" in ctx, ctx)
    ctx, _, _ = drive(dm_msg(text="hello"))
    check("a DM still gets the linked account's notes", "red bicycle" in ctx, ctx)


def test_memory_ids_count_only_injected_items():
    rid = reset()
    key = people.key(DISCORD, FRIEND)
    out = person_memory.context_block([key], ASK, room_recall=False)
    check("nothing injected, nothing counted", out["ids"] == [] and out["block"] == "", str(out))
    check("the withheld count is reported", out["withheld"] == 1, str(out))
    out = person_memory.context_block([key], ASK, room_recall=True)
    check("injected, counted", out["ids"] == [rid], str(out))
    out = person_memory.context_block([key], ASK)
    check("no room argument behaves as before", out["ids"] == [rid] and out["withheld"] == 0, str(out))


def test_asking_about_the_past_in_a_room_does_not_invite_a_made_up_memory():
    reset()
    ctx, _, _ = drive(room_msg(text="do you remember me?"))
    check("told not to claim a memory here", "do not recall saved details" in ctx, ctx)
    check("it does not reveal that anything is saved", "saved facts" not in ctx.lower()
          and "cello" not in ctx, ctx)
    ctx, _, _ = drive(room_msg(text="do you remember me?", mid="m2"), draft="Hi")
    set_master(True)
    out = person_memory.context_block([people.key(DISCORD, FRIEND)], "do you remember me?",
                                      room_recall=True)
    check("with recall on the normal path applies", out["block"] != person_memory.ROOM_NO_RECALL)


# ------------------------------------------------------------ pruning and edits

def test_flags_are_pruned_and_reset_on_edit():
    reset()
    people.add_note(DISCORD, FRIEND, "has a dog")
    dog = people.note_id("has a dog")
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "on")
    user_admin.set_note_groups(DISCORD, FRIEND, dog, "off")
    ok, err, notes = user_admin.edit_note(DISCORD, FRIEND, note_id(), "likes draughts")
    check("edit ok", ok, str(err))
    rec = people.get(DISCORD, FRIEND)
    groups = {n["text"]: n["groups"] for n in people.list_notes(rec)}
    check("an edited note goes back to inherit", groups.get("likes draughts") == "inherit", str(groups))
    check("the other note keeps its flag", groups.get("has a dog") == "off", str(groups))
    check("the old key is gone", note_id() not in (rec.get("note_groups") or {}), str(rec.get("note_groups")))
    user_admin.delete_note(DISCORD, FRIEND, dog)
    rec = people.get(DISCORD, FRIEND)
    check("deleting a note drops its flag", (rec.get("note_groups") or {}) == {}, str(rec.get("note_groups")))
    for i in range(8):                      # the ring buffer pushes old notes out
        people.add_note(DISCORD, FRIEND, f"fact number {i}")
    rec = people.get(DISCORD, FRIEND)
    live = {people.note_id(n) for n in rec["notes"]}
    check("flags never outlive their note", set(rec.get("note_groups") or {}) <= live)


def test_a_missing_note_or_bad_value_is_refused():
    reset()
    ok, err, _ = user_admin.set_note_groups(DISCORD, FRIEND, "deadbeef", "on")
    check("unknown note refused", not ok and "isn't there" in err, err)
    ok, err, _ = user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "yes please")
    check("bad value refused", not ok and "on, off or inherit" in err, err)
    ok, err, rec = user_admin.set_memory_groups(DISCORD, FRIEND, "nope", "on")
    check("unknown memory refused", not ok and rec is None, err)
    ok, err, _ = user_admin.set_note_groups(DISCORD, "999", note_id(), "on")
    check("unregistered person refused", not ok, err)
    key = people.key(DISCORD, FRIEND)
    rule, _ = person_memory.add_instruction(key, "", "tone", text="be brief")
    out, err = person_memory.set_groups(key, rule["id"], "on")
    check("a standing rule has no room flag", out is None and "always apply" in err, err)


# ------------------------------------------------------------ who can change it

def test_model_created_items_start_as_inherit():
    reset()
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": DISCORD, "user_id": FRIEND,
                                                      "is_owner": False,
                                                      "text": "my name is Fred and I love green tea"})
    from jarvis.channels import cheap_call
    saved = cheap_call.ask_yes_no
    cheap_call.ask_yes_no = lambda *a, **k: True      # the gate's tiny model call: "worth keeping"
    try:
        res = channel_people.tool_remember_sender({"note": "loves green tea"})
    finally:
        cheap_call.ask_yes_no = saved
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    check("the tool saved a note", res.get("ok"), str(res))
    rec = people.get(DISCORD, FRIEND)
    flags = {n["text"]: n["groups"] for n in people.list_notes(rec)}
    check("a model-created note is inherit", flags.get("loves green tea") == "inherit", str(flags))
    proposed = [r for r in person_memory.list_records(people.key(DISCORD, FRIEND),
                                                     statuses=("proposed",))]
    check("its proposed memory mirror has no flag", all("groups" not in r for r in proposed), str(proposed))


def test_no_model_tool_can_touch_the_flags():
    from jarvis.actions import person_memory_tools
    schemas = list(channel_people.TOOL_SCHEMAS) + list(person_memory_tools.TOOL_SCHEMAS)
    text = json.dumps(schemas).lower()
    check("no tool schema mentions the room flags", "groups" not in text and "note_groups" not in text
          and "group_recall" not in text)


def test_tools_that_echo_notes_follow_the_same_gate():
    reset()
    base_env = {"platform": DISCORD, "user_id": FRIEND, "is_owner": False}

    def who(extra):
        os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({**base_env, **extra})
        try:
            return channel_people.tool_who_am_i_talking_to()
        finally:
            os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    check("outside a room: all notes", NOTE in who({}).get("notes", []))
    check("in a room, master off: none", who({"room_recall": False}).get("notes") == [])
    check("in a room, master on: shown", NOTE in who({"room_recall": True}).get("notes", []))
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "on")
    check("in a room, `on` note shown with master off", NOTE in who({"room_recall": False}).get("notes", []))
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "inherit")
    check("a garbled room_recall value is treated as off", who({"room_recall": "true"}).get("notes") == [])


# ------------------------------------------------------------ settings, log, CLI

def test_setting_validation_and_changelog():
    reset()
    ok, err = channel_config.set_value(DISCORD, "group_recall", "maybe")
    check("a bad value is refused", not ok and "on or off" in err, err)
    check("a bad ambient number is refused",
          not channel_config.set_value(DISCORD, "ambient_messages", "500")[0])
    check("a bad ambient mode is refused",
          not channel_config.set_value(DISCORD, "ambient_senders", "everybody")[0])
    check("ambient_messages 0 is allowed", channel_config.set_value(DISCORD, "ambient_messages", "0")[0])
    check("the stored value is a real int", channel_config.platform_config(DISCORD)["ambient_messages"] == 0)
    set_master(True)
    set_master(False)
    lines = [e for e in changelog.read_all() if e.get("kind") == "platform"]
    keys = [(e.get("key"), e.get("on", e.get("value"))) for e in lines]
    check("group_recall changes are logged", ("group_recall", True) in keys and
          ("group_recall", False) in keys, str(keys))
    check("the log says it in words",
          any("group replies" in changelog.describe(e) for e in lines))


def test_item_flag_changes_are_logged_without_text():
    rid = reset()
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "on")
    user_admin.set_note_groups(DISCORD, FRIEND, note_id(), "on")        # no change: no line
    user_admin.set_memory_groups(DISCORD, FRIEND, rid, "off")
    lines = [e for e in changelog.read_all() if e.get("kind") == "recall"]
    check("one line per real change", len(lines) == 2, str(lines))
    blob = json.dumps(lines)
    check("never the note or the fact", NOTE not in blob and "cello" not in blob, blob)
    check("describes itself", all(changelog.describe(e) for e in lines))


def _cli(*args):
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=_CLI, JARVIS_RAW_ARCHIVE="0")
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True,
                          text=True, env=env, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout + proc.stderr}


def test_cli_commands():
    rid = reset()
    code, out = _cli("channels-note", DISCORD, FRIEND, "groups", note_id(), "on")
    check("channels-note groups ok", code == 0 and out.get("ok"), str(out))
    check("the listing shows the flag", any(n["groups"] == "on" for n in out.get("notes", [])), str(out))
    code, out = _cli("channels-note", DISCORD, FRIEND, "groups", note_id(), "sometimes")
    check("channels-note groups refuses a bad value", code != 0 and not out.get("ok"), str(out))
    code, out = _cli("channels-memory", DISCORD, FRIEND, "groups", rid, "off")
    check("channels-memory groups ok", code == 0 and out.get("ok") and out["record"]["groups"] == "off", str(out))
    code, out = _cli("channels-memory", DISCORD, FRIEND, "list")
    check("the memory list shows the flag", any(i.get("groups") == "off" for i in out.get("items", [])), str(out))
    code, out = _cli("channels-set", DISCORD, "group_recall", "on")
    check("channels-set group_recall on", code == 0 and channel_config.platform_config(DISCORD)["group_recall"] is True, str(out))
    code, out = _cli("channels-set", DISCORD, "group_recall", "perhaps")
    check("channels-set rejects a bad value", code != 0 and not out.get("ok"), str(out))
    check("and left the old value", channel_config.platform_config(DISCORD)["group_recall"] is True)


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
