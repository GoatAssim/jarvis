"""Tests for the per-person thinking switch (master plan L.44) and the
gateway wiring around it (/thinking, think_override, the tools-off
remember_sender offer, per-person context).

    python3 tests/test_thinking_switch.py

No model, no network, no real ~/.jarvis: HOME is redirected to a temp dir
BEFORE any jarvis module is imported (AGENTS.md). The ask itself is replaced,
so these check what the gateway WOULD ask for, not what a provider says.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-thinking-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import channels_cli                                                # noqa: E402
from jarvis.channels import (DISCORD, base, changelog, people, permissions,    # noqa: E402
                             person_memory, thinking, user_admin, user_perms)
from jarvis.channels import config as channel_config                           # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND = "100", "200"


def reset(**discord):
    for f in (user_perms.PERMS_FILE, people.PEOPLE_FILE, channel_config.CONFIG_FILE,
              changelog.LOG, thinking.THREADS_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    import shutil
    shutil.rmtree(person_memory.PEOPLE_DIR, ignore_errors=True)
    for uid, handle, name in ((OWNER, "boss", "Boss"), (FRIEND, "friend", "Friend")):
        people.touch(DISCORD, uid, handle=handle, is_owner=(uid == OWNER))
        people.set_name(DISCORD, uid, name)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({"enabled": True, "owner": OWNER, "allow_tools": False,
                         "cooldown_seconds": 0, "dm_allowlist": [OWNER, FRIEND],
                         "reply_allowlist": [OWNER, FRIEND], "tool_allowlist": [OWNER]})
    cfg[DISCORD].update(discord)
    channel_config.save_config(cfg)


def cli(*argv):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            channels_cli.handle(list(argv))
        except SystemExit as exc:
            code = exc.code or 0
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text.startswith("{") else {"raw": text})


# ----------------------------------------------------------- the stored value
reset()
check("a person starts with no setting and no lock",
      user_perms.thinking_setting(DISCORD, FRIEND) == (None, False))
user_perms.set_thinking(DISCORD, FRIEND, "off", lock=True)
check("the owner can set and lock it", user_perms.thinking_setting(DISCORD, FRIEND) == ("off", True))
check("it is in the permission store with their other limits",
      user_perms.key(DISCORD, FRIEND) in user_perms._load())
user_perms.set_thinking(DISCORD, FRIEND, "on")
check("changing the value keeps the lock unless told otherwise", user_perms.thinking_setting(DISCORD, FRIEND) == ("on", True))
user_perms.set_thinking(DISCORD, FRIEND, "on", lock=False)
check("a lock can be taken off", user_perms.thinking_setting(DISCORD, FRIEND) == ("on", False))
user_perms.set_thinking(DISCORD, FRIEND, None)
check("clearing the value clears the lock",
      user_perms.thinking_setting(DISCORD, FRIEND) == (None, False)
      and user_perms.key(DISCORD, FRIEND) not in user_perms._load())
try:
    user_perms.set_thinking(DISCORD, FRIEND, "maybe")
    check("a bad value is refused", False)
except ValueError:
    check("a bad value is refused", True)
user_perms.set_thinking(DISCORD, FRIEND, "off", lock=True)
check("a change is written to the permission log",
      any(e.get("kind") == "thinking" for e in changelog.read_all()))
check("...in plain words", "Thinking set to off (locked)" in [changelog.describe(e) for e in changelog.read_all()
                                                                if e.get("kind") == "thinking"][-1])

reset()
user_perms.PERMS_FILE.write_text("{ this is not json", encoding="utf-8")
check("an unreadable permission file fails CLOSED: off and locked",
      user_perms.thinking_setting(DISCORD, FRIEND) == ("off", True))
reset()
user_perms._save({user_perms.key(DISCORD, FRIEND): {"thinking": "turbo"}})
check("a hand-edited bad value reads as off and locked",
      user_perms.thinking_setting(DISCORD, FRIEND) == ("off", True))
reset()
check("the new fields did not change what a plain person looks like",
      user_perms.get(DISCORD, FRIEND)["can_dm"] is True and user_perms.get(DISCORD, FRIEND)["tools"]["mode"] == user_perms.TOOLS_INHERIT)

# ------------------------------------------------------------ what applies
reset()
check("no setting -> the default, which changes nothing",
      thinking.effective(DISCORD, "t1", FRIEND) == {"value": None, "locked": False, "source": "default"}
      and thinking.override_for(thinking.effective(DISCORD, "t1", FRIEND)) is None)
user_perms.set_thinking(DISCORD, FRIEND, "off")
check("'off' asks for no reasoning", thinking.override_for(thinking.effective(DISCORD, "t1", FRIEND)) == "off")
original_level = thinking._configured_level
try:
    thinking._configured_level = lambda: "off"
    user_perms.set_thinking(DISCORD, FRIEND, "on")
    check("'on' with nothing configured still turns something on",
          thinking.override_for(thinking.effective(DISCORD, "t1", FRIEND)) == "medium")
    thinking._configured_level = lambda: "high"
    check("'on' uses the level the owner configured",
          thinking.override_for(thinking.effective(DISCORD, "t1", FRIEND)) == "high")
finally:
    thinking._configured_level = original_level
check("the owner is never limited by a stored value",
      thinking.effective(DISCORD, "t1", OWNER, is_owner=True)["value"] is None)

# ------------------------------------------------------------ /thinking
reset()
check("ordinary text is not a command", thinking.handle_command(DISCORD, "t1", FRIEND, "thinking about lunch") is None)
check("an unknown word is not a command", thinking.handle_command(DISCORD, "t1", FRIEND, "/thinking maybe") is None)
check("it is case-insensitive and tolerates spaces", thinking.parse_command("  /THINKING   On ") == "on")
check("bare /thinking asks for status", thinking.parse_command("/thinking") == "status")
reply = thinking.handle_command(DISCORD, "t1", FRIEND, "/thinking off")
check("a person can switch it for their own thread", "off" in reply
      and thinking.effective(DISCORD, "t1", FRIEND)["value"] == "off"
      and thinking.effective(DISCORD, "t1", FRIEND)["source"] == "their /thinking choice")
check("...and it does not change another thread of theirs", thinking.effective(DISCORD, "t2", FRIEND)["value"] is None)
check("...or someone else", thinking.effective(DISCORD, "t1", "999")["value"] is None)
check("status answers without changing anything", "off" in thinking.handle_command(DISCORD, "t1", FRIEND, "/thinking status"))
user_perms.set_thinking(DISCORD, FRIEND, "off", lock=True)
check("a locked person gets the owner-set notice",
      thinking.handle_command(DISCORD, "t1", FRIEND, "/thinking on") == thinking.LOCKED_NOTICE)
check("...and nothing changed", thinking.effective(DISCORD, "t1", FRIEND)["value"] == "off"
      and thinking.effective(DISCORD, "t1", FRIEND)["locked"])
check("a locked value beats their old per-thread choice",
      thinking.effective(DISCORD, "t1", FRIEND)["source"] == "owner lock")
check("the owner can always use it", "on" in thinking.handle_command(DISCORD, "t1", OWNER, "/thinking on", is_owner=True)
      and thinking.effective(DISCORD, "t1", OWNER, is_owner=True)["value"] == "on")
check("forgetting a person drops their thread choices",
      thinking.forget_person(DISCORD, FRIEND) >= 1 and thinking.thread_value(DISCORD, "t1", FRIEND) is None)

# ------------------------------------------------------------ admin + CLI
reset()
ok, err, _ = user_admin.set_thinking(DISCORD, FRIEND, "off", True)
check("user_admin sets it", ok and user_perms.thinking_setting(DISCORD, FRIEND) == ("off", True), err)
ok, err, _ = user_admin.set_thinking(DISCORD, OWNER, "off", True)
check("the owner is refused rather than silently ignored", not ok and "owner" in err, err)
ok, err, _ = user_admin.set_thinking(DISCORD, "424242", "off")
check("someone who never messaged is refused", not ok)
view = [p for p in user_admin.list_view(DISCORD)["people"] if p["user_id"] == FRIEND][0]
check("the panel view carries the setting", view["thinking"] == "off" and view["thinking_lock"] is True)
code, out = cli("channels-thinking", DISCORD, FRIEND, "default")
check("CLI default clears it", code == 0 and user_perms.thinking_setting(DISCORD, FRIEND) == (None, False), str(out))
code, out = cli("channels-thinking", DISCORD, FRIEND, "on", "lock")
check("CLI on + lock", code == 0 and user_perms.thinking_setting(DISCORD, FRIEND) == ("on", True), str(out))
code, _ = cli("channels-thinking", DISCORD, FRIEND, "sideways")
check("CLI refuses a bad word", code != 0)
code, _ = cli("channels-thinking", DISCORD, FRIEND, "on", "forever")
check("CLI refuses a bad lock word", code != 0)

# ----------------------------------------------------- the gateway, end to end
class _R:
    text, provider, error = "ok", "fake", ""


def run_gateway(user_id, text, mid, thread="dm-x"):
    """handle_message with the ask replaced. Returns (seen kwargs, sent texts)."""
    seen, sent = {}, []
    original = base._ask_jarvis

    def fake(text_, conv_id, may_use_tools, **kw):
        seen.update(kw)
        seen["text"], seen["tools"] = text_, may_use_tools
        return _R()
    base._ask_jarvis = fake
    try:
        msg = permissions.IncomingMessage(DISCORD, permissions.CTX_DM, user_id=user_id,
                                          user_handle="h", text=text, message_id=mid, thread_id=thread)
        base.handle_message(DISCORD, msg, lambda t: sent.append(t) or True)
    finally:
        base._ask_jarvis = original
    return seen, sent


reset()
seen, sent = run_gateway(FRIEND, "hello there", "g1")
check("no setting: the ask is called exactly as before (no override)", seen.get("think_override") is None and sent == ["ok"])
user_perms.set_thinking(DISCORD, FRIEND, "off")
seen, sent = run_gateway(FRIEND, "hello again", "g2")
check("'off' reaches the ask as an override", seen.get("think_override") == "off", str(seen))
seen, sent = run_gateway(FRIEND, "/thinking on", "g3")
check("a /thinking message is answered by the gateway with NO model call", "text" not in seen and len(sent) == 1, str((seen, sent)))
check("...and it took effect (not locked)", thinking.effective(DISCORD, "dm-x", FRIEND)["value"] == "on")
user_perms.set_thinking(DISCORD, FRIEND, "off", lock=True)
seen, sent = run_gateway(FRIEND, "/thinking on", "g4")
check("a locked person is told so, with no model call", "text" not in seen and sent == [thinking.LOCKED_NOTICE], str((seen, sent)))
seen, sent = run_gateway(OWNER, "hi", "g5")
check("the owner's ask carries no override", seen.get("think_override") is None)

# tools-off offer for remember_sender (D-I10)
reset()
seen, _ = run_gateway(FRIEND, "my name is Fred", "o1")
check("a tools-off person introducing themselves is offered the identity tools",
      seen.get("tools") is False and seen.get("offer_tools") and "remember_sender" in seen["offer_tools"], str(seen))
seen, _ = run_gateway(FRIEND, "what is the capital of france", "o2")
check("an ordinary tools-off turn is offered nothing", not seen.get("offer_tools"), str(seen))
seen, _ = run_gateway(OWNER, "my name is Boss", "o3")
check("the owner is not affected", not seen.get("offer_tools"))

# per-person context reaches the model, and only for the right person
reset()
person_memory.add(people.key(DISCORD, FRIEND), "fact", "has a parrot named Kiwi")
person_memory.add(people.key(DISCORD, OWNER), "fact", "secret owner fact about a parrot")
seen, _ = run_gateway(FRIEND, "how is my parrot", "c1")
ctx = seen.get("sender_context") or ""
check("the person's own saved fact is in what the model is told", "Kiwi" in ctx, ctx)
check("nobody else's facts are", "secret owner fact" not in ctx)
seen, _ = run_gateway(OWNER, "how is my parrot", "c2")
check("the owner's turn gets no per-person block", "Kiwi" not in (seen.get("sender_context") or ""))
w = "qwertzk"
person_memory.add_instruction(people.key(DISCORD, FRIEND), w + " " + "plmnbvk" + " " + "zxcvbnk", "don_t_discuss")
seen, _ = run_gateway(FRIEND, f"tell me about {w} plmnbvk zxcvbnk", "c3")
ctx = seen.get("sender_context") or ""
check("a matching standing rule reaches the model", "STANDING RULES" in ctx and w in ctx, ctx)
seen, _ = run_gateway(FRIEND, "tell me about the weather", "c4")
check("an unrelated message gets no rule", "STANDING RULES" not in (seen.get("sender_context") or ""))
check("the rule firing was logged without the message",
      person_memory.read_applied() and "weather" not in json.dumps(person_memory.read_applied()))

print(f"{PASSED} passed, {len(FAILED)} failed")
for line in FAILED:
    print("FAILED:", line)
sys.exit(1 if FAILED else 0)
