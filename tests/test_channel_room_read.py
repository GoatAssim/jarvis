"""Tests for the room readers in channels/transcript.py (channel spaces S3.2-S3.4).

    python3 tests/test_channel_room_read.py

  S3.2  read_space          rotation-aware, newest `limit`, participants from the
                            whole history, denied counted (not a participant),
                            replies with no `to_user` stay unattributed, tool calls
                            cleaned, text clipped, read-only (directory snapshot)
  S3.3  list_threads        the default listing keeps its four keys; details=True
                            adds `space` and `title` (registry first, newest
                            inbound line as the fallback)
  S3.4  attribution audit   every transcript.log_outbound call in base.py passes
                            `to_user`, so a room's replies can be attributed

No bot token, no socket, no model, no real ~/.jarvis: HOME is redirected to a temp
dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-roomread-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import (DISCORD, base, people, permissions, space,       # noqa: E402
                             spaces, transcript)
from jarvis.channels import config as channel_config                          # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003"
G1, C1, T1 = "900000000000000001", "800000000000000001", "700000000000000001"


def wipe():
    root = Path(_HOME) / ".jarvis"
    for _ in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
    spaces._cache.clear()
    transcript.reset_cooldowns()


def reset():
    wipe()
    for uid, handle in ((OWNER, "boss"), (FRIEND, "friend")):
        people.touch(DISCORD, uid, handle=handle)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
        "tool_allowlist": [], "require_mention": True,
    })
    channel_config.save_config(cfg)


def room_msg(uid=FRIEND, guild=G1, channel=C1, thread=None, text="hello", mid="m1",
             mentioned=True, guild_name="My Server", channel_name="general",
             thread_name="", handle="friend"):
    return permissions.IncomingMessage(
        DISCORD, permissions.CTX_GROUP, user_id=uid, user_handle=handle, text=text,
        mentioned=mentioned, guild_id=guild, channel_id=channel, message_id=mid,
        thread_id=thread or channel, guild_name=guild_name, channel_name=channel_name,
        thread_name=thread_name)


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


def drive(msg):
    """Real base.handle_message; only the model and the owner ping are faked."""
    saved = base._ask_jarvis, base._notify_owner_new_sender
    base._ask_jarvis = lambda *a, **k: _Result()
    base._notify_owner_new_sender = lambda *a, **k: True
    sent = []
    try:
        decision = base.handle_message(DISCORD, msg, lambda chunk: sent.append(chunk) or True)
    finally:
        base._ask_jarvis, base._notify_owner_new_sender = saved
    return decision, sent


def folder():
    path = transcript.CHANNELS_DIR / DISCORD
    path.mkdir(parents=True, exist_ok=True)
    return path


def put(name, records):
    (folder() / name).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def snapshot():
    root = Path(_HOME) / ".jarvis"
    out = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            out[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return out


# ===================================================================== S3.2

def test_read_space_shape():
    reset()
    drive(room_msg(mid="r1", text="hello"))
    drive(room_msg(uid=OWNER, handle="boss", mid="r2", text="hi there"))
    out = transcript.read_space(DISCORD, C1)
    check("kind comes from the registry", out["kind"] == space.KIND_CHANNEL, out["kind"])
    check("title comes from the registry", out["title"] == "#general · My Server", out["title"])
    check("two exchanges are four lines", out["total"] == 4 and out["shown"] == 4,
          f"{out['total']}/{out['shown']}")
    check("oldest first, in/out alternating",
          [e["dir"] for e in out["entries"]] == ["in", "out", "in", "out"])
    check("both senders are participants",
          {p["user_id"] for p in out["participants"]} == {FRIEND, OWNER})
    check("one message each",
          all(p["messages"] == 1 for p in out["participants"]), str(out["participants"]))
    check("participant carries the handle",
          {p["handle"] for p in out["participants"]} == {"friend", "boss"})
    replies = [e for e in out["entries"] if e["dir"] == "out"]
    check("every reply says who it was for (S3.4)",
          [r["to_user"] for r in replies] == [FRIEND, OWNER], str([r["to_user"] for r in replies]))
    check("the reply text is there", replies[0]["text"] == "the answer")
    check("nothing is unattributed", out["unattributed_replies"] == 0)
    check("nothing denied", out["denied"] == 0)
    check("an inbound entry has its message id",
          out["entries"][0]["message_id"] == "r1" and out["entries"][0]["user_id"] == FRIEND)


def test_denied_is_counted_not_a_participant():
    reset()
    drive(room_msg(mid="d1", text="hello"))
    decision, sent = drive(room_msg(uid=STRANGER, handle="rando", mid="d2", text="let me in"))
    check("the stranger really was turned away", not decision.allowed, str(decision))
    out = transcript.read_space(DISCORD, C1)
    check("one denied line counted", out["denied"] == 1, str(out["denied"]))
    check("the stranger is not a participant",
          STRANGER not in {p["user_id"] for p in out["participants"]})
    shown = [e for e in out["entries"] if e["dir"] == "in" and e["user_id"] == STRANGER]
    check("the denied line is still shown, marked allowed=false",
          len(shown) == 1 and shown[0]["allowed"] is False, str(shown))


def test_replies_without_to_user_are_not_guessed():
    reset()
    drive(room_msg(mid="u1"))
    transcript.append(DISCORD, C1, {"dir": "out", "text": "old reply", "ok": True})
    out = transcript.read_space(DISCORD, C1)
    check("an old reply with no to_user is counted", out["unattributed_replies"] == 1,
          str(out["unattributed_replies"]))
    old = [e for e in out["entries"] if e.get("text") == "old reply"][0]
    check("and attributed to nobody", old["to_user"] == "")


def test_rotation_limit_and_participants_use_the_whole_history():
    reset()
    put("rot1.20261001-101010.jsonl", [
        {"dir": "in", "user_id": "u1", "user_handle": "ann", "text": "a1",
         "context": "group", "at": "2026-10-01T10:00:00"},
        {"dir": "in", "user_id": "u2", "user_handle": "bob", "author_name": "Bob D.",
         "text": "a2", "context": "group", "at": "2026-10-01T10:01:00"}])
    put("rot1.jsonl", [
        {"dir": "in", "user_id": "u1", "user_handle": "ann", "text": "b1",
         "context": "group", "at": "2026-10-02T09:00:00"},
        {"dir": "out", "text": "r1", "to_user": "u1", "ok": True, "at": "2026-10-02T09:00:05"}])
    (folder() / "rot1.jsonl").open("a").write("{torn line")       # crash mid-write
    out = transcript.read_space(DISCORD, "rot1")
    check("spans the rotation, oldest first",
          [e["text"] for e in out["entries"]] == ["a1", "a2", "b1", "r1"],
          str([e["text"] for e in out["entries"]]))
    check("a torn line is skipped", out["total"] == 4, str(out["total"]))
    by_id = {p["user_id"]: p for p in out["participants"]}
    check("participants count the rotated copy too",
          by_id["u1"]["messages"] == 2 and by_id["u2"]["messages"] == 1, str(by_id))
    check("last_at is the newest line", by_id["u1"]["last_at"] == "2026-10-02T09:00:00")
    check("the display name is kept", by_id["u2"]["name"] == "Bob D.")
    check("most recent speaker first", out["participants"][0]["user_id"] == "u1")
    small = transcript.read_space(DISCORD, "rot1", limit=2)
    check("limit keeps the newest lines", [e["text"] for e in small["entries"]] == ["b1", "r1"])
    check("total and participants still cover everything",
          small["total"] == 4 and small["shown"] == 2 and len(small["participants"]) == 2)


def test_limits_and_unknowns():
    reset()
    check("an unknown thread reads empty",
          transcript.read_space(DISCORD, "nope")["entries"] == []
          and transcript.read_space(DISCORD, "nope")["total"] == 0)
    check("an unknown platform reads empty", transcript.read_space("telegram", "x")["total"] == 0)
    check("an empty thread id reads empty", transcript.read_space(DISCORD, "")["total"] == 0)
    put("lim.jsonl", [{"dir": "in", "user_id": "u", "text": str(i), "context": "group",
                       "at": f"2026-10-01T10:00:{i:02d}"} for i in range(5)])
    check("limit below 1 is raised to 1", transcript.read_space(DISCORD, "lim", limit=0)["shown"] == 1)
    check("a bad limit falls back to 100",
          transcript.read_space(DISCORD, "lim", limit="abc")["shown"] == 5)
    check("a huge limit is capped, not an error",
          transcript.read_space(DISCORD, "lim", limit=10 ** 9)["shown"] == 5)


def test_text_is_clipped_and_tool_calls_cleaned():
    reset()
    big = "x" * (transcript.MAX_TEXT_CHARS + 500)
    put("clip.jsonl", [
        {"dir": "in", "user_id": "u", "text": big, "context": "group", "at": "2026-10-01T10:00:00"},
        {"dir": "out", "text": "done", "to_user": "u", "ok": True, "at": "2026-10-01T10:00:05",
         "tool_calls": [{"name": "t", "args": "a" * 5000, "ok": True, "result": "r" * 5000},
                        "not a dict"]}])
    out = transcript.read_space(DISCORD, "clip")
    inbound, reply = out["entries"]
    check("long text is clipped and flagged",
          len(inbound["text"]) == transcript.MAX_TEXT_CHARS and inbound["clipped"] is True)
    calls = reply["tool_calls"]
    check("tool calls are cleaned, bad entries dropped", len(calls) == 1 and calls[0]["name"] == "t")
    check("tool call strings are bounded",
          len(calls[0]["args"]) <= transcript.MAX_TOOL_ARG_CHARS + 1
          and len(calls[0]["result"]) <= transcript.MAX_TOOL_RESULT_CHARS + 1)


def test_reply_to_and_attachments_are_passed_through_bounded():
    reset()
    put("rt.jsonl", [{"dir": "in", "user_id": "u", "text": "t", "context": "group",
                      "attachments": 2, "at": "2026-10-01T10:00:00",
                      "reply_to": {"message_id": "6", "user_id": "v", "user_name": "Bob",
                                   "snippet": "s" * 500, "junk": "dropped"}},
                     {"dir": "in", "user_id": "u", "text": "t2", "context": "group",
                      "reply_to": "not a dict", "attachments": "x", "at": "2026-10-01T10:00:01"}])
    first, second = transcript.read_space(DISCORD, "rt")["entries"]
    check("reply_to keeps only its four fields, clipped",
          set(first["reply_to"]) == {"message_id", "user_id", "user_name", "snippet"}
          and len(first["reply_to"]["snippet"]) == 120, str(first.get("reply_to")))
    check("attachments is a count", first["attachments"] == 2)
    check("a bad reply_to is left out, a bad count is 0",
          "reply_to" not in second and second["attachments"] == 0, str(second))


def test_reading_changes_nothing():
    reset()
    drive(room_msg(mid="ro1"))
    drive(dm_msg(mid="ro2"))
    before = snapshot()
    transcript.read_space(DISCORD, C1)
    transcript.read_space(DISCORD, "dm-chan-1")
    transcript.list_threads(DISCORD, details=True)
    transcript.list_threads()
    check("no file created, changed or rotated by reading", snapshot() == before)


# ===================================================================== S3.3

def test_list_threads_default_shape_is_unchanged():
    reset()
    drive(room_msg(mid="l1"))
    row = transcript.list_threads(platform=DISCORD)[0]
    check("the default listing keeps its four keys",
          set(row) == {"platform", "thread_id", "bytes", "modified"}, str(set(row)))


def test_list_threads_details():
    reset()
    drive(room_msg(mid="l1"))
    drive(room_msg(thread=T1, thread_name="deploy talk", mid="l2"))
    drive(dm_msg(mid="l3"))
    put("old1.jsonl", [{"dir": "in", "context": "group", "user_id": "u1", "guild_id": G1,
                        "channel_id": "old1", "text": "legacy", "at": "2026-10-01T10:00:00"}])
    put("outonly.jsonl", [{"dir": "out", "text": "x", "at": "2026-10-01T10:00:00"}])

    def rows():
        return {r["thread_id"]: r for r in transcript.list_threads(DISCORD, details=True)}

    got = rows()
    check("every entry gains space and title",
          all("space" in r and "title" in r for r in got.values()))
    check("a channel", got[C1]["space"] == space.KIND_CHANNEL and got[C1]["title"] == "#general · My Server",
          str(got[C1]))
    check("a thread", got[T1]["space"] == space.KIND_THREAD
          and got[T1]["title"] == "Thread 'deploy talk' · #general · My Server", str(got[T1]))
    check("a DM has no title", got["dm-chan-1"]["space"] == space.KIND_DM
          and got["dm-chan-1"]["title"] == "", str(got["dm-chan-1"]))
    check("a legacy thread is classified from its own line",
          got["old1"]["space"] == space.KIND_CHANNEL and got["old1"]["title"] == "Channel old1",
          str(got["old1"]))
    check("a thread with no inbound line is neither", got["outonly"]["space"] == ""
          and got["outonly"]["title"] == "")

    spaces.registry_file().unlink()                    # the registry is only a cache
    again = rows()
    check("without the registry the same kind and title come from the log",
          all(again[k]["space"] == got[k]["space"] and again[k]["title"] == got[k]["title"]
              for k in got), str({k: (again[k]["space"], again[k]["title"]) for k in again}))


def test_list_threads_details_reads_only_the_tail():
    reset()
    lines = [json.dumps({"dir": "in", "context": "group", "user_id": "u", "guild_id": G1,
                         "channel_id": "big1", "channel_name": "old-name",
                         "text": "y" * 500, "at": "2026-10-01T10:00:00"}) for _ in range(400)]
    lines.append(json.dumps({"dir": "in", "context": "group", "user_id": "u", "guild_id": G1,
                             "channel_id": "big1", "channel_name": "new-name", "text": "last",
                             "at": "2026-10-02T10:00:00"}))
    (folder() / "big1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    check("the file is bigger than the tail that is read",
          (folder() / "big1.jsonl").stat().st_size > transcript._TAIL_BYTES)
    got = {r["thread_id"]: r for r in transcript.list_threads(DISCORD, details=True)}
    check("the newest line names the room", got["big1"]["title"] == "#new-name", got["big1"]["title"])


# ===================================================================== S3.4

def test_every_outbound_log_call_passes_to_user():
    tree = ast.parse(Path(base.__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "log_outbound"]
    check("base.py logs replies in several places", len(calls) >= 6, str(len(calls)))
    missing = [n.lineno for n in calls if "to_user" not in {k.arg for k in n.keywords}]
    check("every one of them says who the reply was for", not missing, f"lines {missing}")


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
