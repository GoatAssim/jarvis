"""Tests for the chat confirmation channel (L.29 / L.42.1) and the thinking trace
in chat (L.42.2).

    python3 tests/test_chat_approvals.py

No model, no network, no real ~/.jarvis, no discord.py: HOME is redirected to a
temp dir BEFORE any jarvis module is imported (AGENTS.md), the ask is replaced,
and the Discord button view is driven with a tiny stand-in for the `discord`
module. These check what the gateway WOULD do, not what Discord or a provider says.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-approvals-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import (DISCORD, INSTAGRAM, approvals, base, discord_gateway,  # noqa: E402
                             people, permissions, thinking, transcript, user_perms)
from jarvis.channels import config as channel_config                              # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND = "100", "200"


def reset():
    for f in (user_perms.PERMS_FILE, people.PEOPLE_FILE, channel_config.CONFIG_FILE,
              thinking.THREADS_FILE, approvals.PENDING_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    for uid, handle in ((OWNER, "boss"), (FRIEND, "friend")):
        people.touch(DISCORD, uid, handle=handle, is_owner=(uid == OWNER))
    cfg = channel_config.load_config()
    cfg[DISCORD].update({"enabled": True, "owner": OWNER, "allow_tools": True,
                         "cooldown_seconds": 0, "dm_allowlist": [OWNER, FRIEND],
                         "reply_allowlist": [OWNER, FRIEND], "tool_allowlist": [OWNER],
                         "log_conversations": False})
    channel_config.save_config(cfg)
    with approvals._LOCK:
        approvals._PENDING.clear()


def only_pending():
    with approvals._LOCK:
        return list(approvals._PENDING.values())


def answer_soon(approve, user_id=OWNER, delay=0.05):
    """Answer the newest pending request from another thread, like a button press."""
    def go():
        deadline = time.time() + 5
        while time.time() < deadline:
            rows = only_pending()
            if rows:
                time.sleep(delay)
                answer_soon.result = approvals.resolve(rows[-1].id, user_id, approve)
                return
            time.sleep(0.01)
    t = threading.Thread(target=go)
    t.start()
    return t


# ------------------------------------------------------------ showing the action
reset()
text, complete = approvals.render_action("send_dm", {"person": "maryem", "message": "late"})
check("the action shows the tool and every argument", "send_dm" in text and "maryem" in text
      and "late" in text and complete, text)
text, complete = approvals.render_action("run_shell", {"command": "x" * 5000})
check("an over-long argument is flagged as not shown in full", not complete)
text, complete = approvals.render_action("a", {f"k{i}": "v" * 300 for i in range(20)}, limit=500)
check("a whole prompt over the limit is flagged too", not complete and len(text) <= 505)
sent = []
ok = approvals.ask_owner(DISCORD, OWNER, "run_shell", {"command": "x" * 5000}, None,
                         lambda *a: sent.append(a), seconds=1)
check("an action that cannot be shown in full is denied without asking", ok is False and not sent)

# ---------------------------------------------------------------- the main path
reset()
prompts = []


def prompter(request_id, text, seconds):
    prompts.append((request_id, text, seconds))
    return None


t = answer_soon(True)
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "maryem", "message": "late"},
                         None, prompter, seconds=5)
t.join()
check("the owner pressing approve lets the tool run", ok is True)
check("the prompt was sent once with the request id and the time limit",
      len(prompts) == 1 and prompts[0][2] == 5)
check("a settled request leaves nothing pending or on disk",
      not only_pending() and approvals._load_rows() == [])

reset()
t = answer_soon(False)
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, prompter, seconds=5)
t.join()
check("deny means the tool does not run", ok is False)

# ------------------------------------------------------------- who may answer
reset()
t = answer_soon(True, user_id=FRIEND)
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, prompter, seconds=0.6)
t.join()
check("a guest's approve does nothing: not the owner", answer_soon.result[0] is False)
check("...and the request then expires as denied", ok is False)

reset()
seen_rows = []


def spy_prompter(request_id, text, seconds):
    seen_rows.append(approvals._load_rows())
    return None


t = answer_soon(True)
approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "SECRETNAME", "message": "SECRETBODY"},
                    None, spy_prompter, seconds=5)
t.join()
check("while pending, the store has the tool and platform on disk",
      seen_rows and seen_rows[0] and seen_rows[0][0]["tool"] == "send_dm"
      and seen_rows[0][0]["platform"] == DISCORD)
check("the on-disk store never holds the arguments or message text",
      "SECRETNAME" not in json.dumps(seen_rows) and "SECRETBODY" not in json.dumps(seen_rows))

# --------------------------------------------------------- timeout, late, double
reset()
started = time.time()
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, prompter, seconds=0.3)
check("no answer in time counts as denied", ok is False and time.time() - started < 3)
check("a press after the timeout fails closed",
      approvals.resolve(prompts[-1][0], OWNER, True)[0] is False)

reset()
rid = {}


def grab(request_id, text, seconds):
    rid["id"] = request_id
    return None


t = answer_soon(True)
approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, grab, seconds=5)
t.join()
check("a second press on a settled request is refused",
      approvals.resolve(rid["id"], OWNER, True)[0] is False)
check("an unknown request id is refused", approvals.resolve("deadbeef", OWNER, True)[0] is False)


def broken(request_id, text, seconds):
    raise RuntimeError("DMs are closed")


reset()
logged = []
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, broken, seconds=5,
                         log=lambda d, r: logged.append((d, r)))
check("a prompt that could not be sent is a denial with the cause",
      ok is False and logged and logged[0][0] == "denied" and "DMs are closed" in logged[0][1])
check("...and leaves nothing pending", not only_pending() and approvals._load_rows() == [])

reset()
hooks = []
t = answer_soon(True)
approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None,
                    lambda *a: (lambda d, r: hooks.append(d)), seconds=5)
t.join()
check("the gateway's tidy-up hook is told the outcome", hooks == ["approved"], str(hooks))

# ------------------------------------------------------------------ restart
reset()
approvals._save_rows([{"id": "aaaa1111", "platform": DISCORD, "tool": "send_dm",
                       "created": 1, "expires": 2},
                      {"id": "bbbb2222", "platform": INSTAGRAM, "tool": "send_dm",
                       "created": 1, "expires": 2}])
n = approvals.cancel_pending(DISCORD)
rows = approvals._load_rows()
check("a gateway start drops that platform's pending rows and only those",
      n == 1 and len(rows) == 1 and rows[0]["platform"] == INSTAGRAM)
check("an answer to a request from before the restart fails closed",
      approvals.resolve("aaaa1111", OWNER, True)[0] is False)

# ------------------------------------------------------------ Instagram's Y / N
check("parse: whole-message y / yes / n / no",
      [approvals.parse_answer(x) for x in ("y", "Yes", " N ", "no.", "Y!")] == [True, True, False, False, True])
check("parse: anything else is not an answer",
      all(approvals.parse_answer(x) is None for x in ("yes please", "y n", "ok", "", "maybe", None)))


def pending_text(owner=OWNER):
    box = {}

    def go():
        box["ok"] = approvals.ask_owner(INSTAGRAM, owner, "send_dm", {"person": "x"}, None,
                                        lambda *a: None, accept_text=True, seconds=5)
    th = threading.Thread(target=go)
    th.start()
    deadline = time.time() + 5
    while not only_pending() and time.time() < deadline:
        time.sleep(0.01)
    return th, box


reset()
check("no pending request: a 'y' is just a message",
      approvals.answer_text(INSTAGRAM, OWNER, "y", is_dm=True, is_owner=True) is None)
th, box = pending_text()
check("a guest's 'y' never answers", approvals.answer_text(INSTAGRAM, FRIEND, "y", True, False) is None
      and len(only_pending()) == 1)
reply = approvals.answer_text(INSTAGRAM, OWNER, "Y", is_dm=True, is_owner=True)
th.join()
check("the owner's whole-message Y approves", reply == "Approved." and box["ok"] is True, str(reply))

reset()
th, box = pending_text()
reply = approvals.answer_text(INSTAGRAM, OWNER, "n", is_dm=True, is_owner=True)
th.join()
check("the owner's N denies", reply == "Denied." and box["ok"] is False)

reset()
th, box = pending_text()
reply = approvals.answer_text(INSTAGRAM, OWNER, "yes, and also tell her hi", is_dm=True, is_owner=True)
th.join()
check("another message from the owner denies the pending request and carries on",
      reply is None and box["ok"] is False)

reset()
th, box = pending_text()
reply = approvals.answer_text(INSTAGRAM, OWNER, "y", is_dm=False, is_owner=True)
th.join()
check("a 'y' from a group thread never approves", reply is None and box["ok"] is False)

# ------------------------------------------------- the Discord buttons (stand-in)
fake = types.SimpleNamespace()


class _View:
    def __init__(self, *a, **kw):
        pass


def _button(**kw):
    return lambda fn: fn


fake.ui = types.SimpleNamespace(View=_View, button=_button)
fake.ButtonStyle = types.SimpleNamespace(success=1, danger=2)


class _Resp:
    def __init__(self):
        self.calls = []

    async def send_message(self, text, ephemeral=False):
        self.calls.append(("send", text, ephemeral))

    async def defer(self):
        self.calls.append(("defer",))


def press(view, user_id, approve):
    inter = types.SimpleNamespace(user=types.SimpleNamespace(id=int(user_id)), response=_Resp())

    async def run():
        if await view.interaction_check(inter):
            await view._answer(inter, approve)
    asyncio.run(run())
    return inter.response.calls


reset()
rid = {}
view_box = {}


def view_prompter(request_id, text, seconds):
    view_box["view"] = discord_gateway.make_approval_view(fake, request_id, OWNER, seconds)
    rid["id"] = request_id
    return None


def press_soon(user_id, approve):
    def go():
        deadline = time.time() + 5
        while "view" not in view_box and time.time() < deadline:
            time.sleep(0.01)
        press_soon.calls = press(view_box["view"], user_id, approve)
    th = threading.Thread(target=go)
    th.start()
    return th


th = press_soon(FRIEND, True)
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, view_prompter, seconds=0.6)
th.join()
check("a guest pressing Approve gets a private refusal and nothing runs",
      ok is False and press_soon.calls and press_soon.calls[0][0] == "send" and press_soon.calls[0][2] is True,
      str(press_soon.calls))

reset()
view_box.clear()
th = press_soon(OWNER, True)
ok = approvals.ask_owner(DISCORD, OWNER, "send_dm", {"person": "x"}, None, view_prompter, seconds=5)
th.join()
check("the owner pressing Approve settles it", ok is True and press_soon.calls == [("defer",)],
      str(press_soon.calls))
late = press(view_box["view"], OWNER, True)
check("pressing again afterwards is told the request is closed",
      late and late[0][0] == "send" and "no longer open" in late[0][1], str(late))

# ------------------------------------------------ the wiring in handle_message
class _R:
    text = "ok"
    provider = "test"
    error = ""
    usage_total = None


def drive(user_id, text, mid, thread="t1", ask_approval=None, by_text=False, fake_ask=None,
          context=None):
    """handle_message with the ask replaced. Returns (kwargs the ask saw, sent texts)."""
    seen, sent = {}, []
    original = base._ask_jarvis

    def default_fake(text_, conv_id, may_use_tools, **kw):
        seen.update(kw)
        seen["text"] = text_
        return _R()
    base._ask_jarvis = fake_ask(seen) if fake_ask else default_fake
    try:
        msg = permissions.IncomingMessage(
            DISCORD, context or permissions.CTX_DM, user_id=user_id, user_handle="h",
            text=text, message_id=mid, thread_id=thread)
        base.handle_message(DISCORD, msg, lambda t: sent.append(t) or True,
                            ask_approval=ask_approval, approval_by_text=by_text)
    finally:
        base._ask_jarvis = original
    return seen, sent


reset()
noop = lambda *a: None  # noqa: E731
seen, _ = drive(OWNER, "hello", "m1", ask_approval=noop)
check("the owner's ask gets a confirmation callback", callable(seen.get("on_confirm_request")))
seen, _ = drive(FRIEND, "hello", "m2", ask_approval=noop)
check("a guest's ask NEVER gets one (run_shell / code_agent stay cancelled)",
      seen.get("on_confirm_request") is None, str(seen.get("on_confirm_request")))
seen, _ = drive(OWNER, "hello", "m3")
check("a gateway with no prompt support: chat behaves as before",
      seen.get("on_confirm_request") is None)

# end to end: the callback ai_client would call, answered from "another thread"
reset()
prompt_log = []


def ask_approval(request_id, text, seconds):
    prompt_log.append(text)
    return None


def fake_ask_calls_confirm(seen):
    def fake(text_, conv_id, may_use_tools, **kw):
        cb = kw.get("on_confirm_request")
        seen["approved"] = cb("send_dm", {"person": "maryem", "message": "late"}, None) if cb else None
        return _R()
    return fake


t = answer_soon(True)
seen, sent = drive(OWNER, "tell maryem I'm late", "m4", ask_approval=ask_approval,
                   fake_ask=fake_ask_calls_confirm)
t.join()
check("approve through handle_message runs the tool", seen.get("approved") is True)
check("the prompt showed the exact recipient and text",
      prompt_log and "maryem" in prompt_log[0] and "late" in prompt_log[0], str(prompt_log))

reset()
t = answer_soon(False)
seen, _ = drive(OWNER, "tell maryem I'm late", "m5", ask_approval=ask_approval,
                fake_ask=fake_ask_calls_confirm)
t.join()
check("deny through handle_message cancels it", seen.get("approved") is False)

# a typed Y on a text platform is answered by the gateway with no model call
reset()
box = {}


def hold_ask(seen):
    def fake(text_, conv_id, may_use_tools, **kw):
        cb = kw.get("on_confirm_request")
        box["approved"] = cb("send_dm", {"person": "maryem", "message": "late"}, None)
        return _R()
    return fake


first = threading.Thread(target=lambda: drive(OWNER, "tell maryem I'm late", "m6",
                                              ask_approval=ask_approval, by_text=True,
                                              fake_ask=hold_ask))
first.start()
deadline = time.time() + 5
while not only_pending() and time.time() < deadline:
    time.sleep(0.01)
seen, sent = drive(OWNER, "y", "m7", ask_approval=ask_approval, by_text=True)
first.join()
check("a typed 'y' from the owner approves and costs no model call",
      box.get("approved") is True and "text" not in seen and sent == ["Approved."], str((box, seen, sent)))

reset()
seen, sent = drive(OWNER, "y", "m8", ask_approval=ask_approval, by_text=True)
check("a 'y' with nothing pending is an ordinary message", seen.get("text") == "y")

# ------------------------------------------------------ the thinking trace
def fake_ask_with_trace(thought):
    def factory(seen):
        def fake(text_, conv_id, may_use_tools, **kw):
            seen.update(kw)
            cb = kw.get("on_trace")
            if cb is not None:
                cb(object(), thought)
            return _R()
        return fake
    return factory


THOUGHT = {"text": "first I check the memory block, then answer", "level": "medium", "rounds": 1}

reset()
seen, sent = drive(OWNER, "hi", "t1a", fake_ask=fake_ask_with_trace(THOUGHT))
check("default: no trace is asked for or sent", seen.get("on_trace") is None and sent == ["ok"])

reset()
thinking.set_thread(DISCORD, "t1", OWNER, "on")
seen, sent = drive(OWNER, "hi", "t1b", fake_ask=fake_ask_with_trace(THOUGHT))
check("owner with /thinking on: the trace is a separate message AFTER the reply",
      len(sent) == 2 and sent[0] == "ok" and sent[1].startswith("Thinking (medium):")
      and "||first I check" in sent[1], str(sent))

reset()
thinking.set_thread(DISCORD, "t1", OWNER, "on")
seen, sent = drive(OWNER, "hi", "t1c", fake_ask=fake_ask_with_trace({"text": "x", "level": "off"}))
check("a trace at level 'off' sends nothing", sent == ["ok"], str(sent))
seen, sent = drive(OWNER, "hi", "t1d", fake_ask=fake_ask_with_trace({"text": "", "level": "medium"}))
check("an empty trace sends nothing", sent == ["ok"], str(sent))

reset()
user_perms.set_thinking(DISCORD, FRIEND, "on")
seen, sent = drive(FRIEND, "hi", "t1e", fake_ask=fake_ask_with_trace(THOUGHT))
check("a guest whose switch is 'on' gets the level but NEVER the trace",
      seen.get("think_override") and seen.get("on_trace") is None and sent == ["ok"], str((seen, sent)))

reset()
thinking.set_thread(DISCORD, "t1", OWNER, "off")
seen, sent = drive(OWNER, "hi", "t1f", fake_ask=fake_ask_with_trace(THOUGHT))
check("owner with /thinking off: no trace", seen.get("on_trace") is None and sent == ["ok"])

# trace formatting, bounds
for plat, limit in ((DISCORD, 1900), (INSTAGRAM, 900)):
    for n in (5, 400, 6000):
        parts = thinking.format_trace(plat, "word " * n, "low", limit=limit)
        check(f"{plat}: {n} words fit the message limit", parts and all(len(p) <= limit for p in parts))
check("Discord hides the trace in spoilers, at most 3 messages, with a truncation note",
      (lambda ps: len(ps) == 3 and all("||" in p for p in ps) and "truncated" in ps[-1])
      (thinking.format_trace(DISCORD, "word " * 6000, "low")))
check("a literal || in the trace cannot end the spoiler early",
      thinking.format_trace(DISCORD, "a || b", "low")[0].count("||") == 2)
check("Instagram has no spoiler: one labelled plain message",
      thinking.format_trace(INSTAGRAM, "reason", "low")[0] == "Thinking (low):\nreason")
check("nothing to show -> no messages", thinking.format_trace(DISCORD, "  ", "low") == [])

print(f"{PASSED} passed, {len(FAILED)} failed")
for line in FAILED:
    print("FAILED:", line)
sys.exit(1 if FAILED else 0)
