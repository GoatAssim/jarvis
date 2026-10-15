"""Tests for Instagram thread keying: DMs, group threads and comment mentions.

    python3 tests/test_instagram_groups.py

  S1.3  a comment / story @mention is stored in its own thread (not the
        sender's DM file and conversation) while the 24h window and the send
        target stay exactly what they were

No network, no model, no real ~/.jarvis: HOME is redirected to a temp dir
BEFORE any jarvis module is imported (AGENTS.md).
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-ig-groups-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import INSTAGRAM, base, people, permissions, transcript  # noqa: E402
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


SENDER = "17841400000000001"
GROUP = "t9-group-thread"
BOT = "17841499999999999"

SENT = []          # (user_id, text, thread_key) per outbound call


class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
                            "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": []}


def _wipe():
    root = Path(_HOME) / ".jarvis"
    for _ in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
    transcript.reset_cooldowns()
    SENT.clear()


def _setup():
    _wipe()
    people.touch(INSTAGRAM, SENDER, handle="alice")
    cfg = channel_config.load_config()
    cfg[INSTAGRAM].update({
        "enabled": True, "ig_user_id": BOT, "bot_handle": "jarvis",
        "access_token": "x", "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [SENDER], "reply_allowlist": [SENDER], "tool_allowlist": [],
        "require_mention": True,
    })
    channel_config.save_config(cfg)
    return channel_config.platform_config(INSTAGRAM)


def _run(payload, cfg):
    """Real ig._process_payload -> real base.handle_message. Only the model,
    the profile fetches, the owner ping and the HTTP send are faked."""
    saved = (base._ask_jarvis, base._notify_owner_new_sender, ig._fetch_username,
             ig._fetch_profile_pic, ig.send_message)
    base._ask_jarvis = lambda *a, **k: _Result()
    base._notify_owner_new_sender = lambda *a, **k: True
    ig._fetch_username = lambda *a, **k: "alice"
    ig._fetch_profile_pic = lambda *a, **k: ""

    def fake_send(user_id, text, cfg=None, thread_key=""):
        SENT.append((str(user_id), text, str(thread_key)))
        return True, ""
    ig.send_message = fake_send
    try:
        ig._process_payload(payload, cfg)
    finally:
        (base._ask_jarvis, base._notify_owner_new_sender, ig._fetch_username,
         ig._fetch_profile_pic, ig.send_message) = saved


def _dm(mid="dm1", text="hello from a DM"):
    return {"entry": [{"messaging": [
        {"sender": {"id": SENDER}, "message": {"mid": mid, "text": text}}]}]}


def _mention(mid="c1", text="@jarvis look at this"):
    return {"entry": [{"changes": [{"field": "mentions", "value": {
        "from": {"id": SENDER, "username": "alice"}, "text": text, "comment_id": mid}}]}]}


def _group(mid="g1", text="hey @jarvis help"):
    return {"entry": [{"messaging": [
        {"sender": {"id": SENDER}, "thread_key": GROUP,
         "message": {"mid": mid, "text": text}}]}]}


def _in_lines(thread_id):
    return [r for r in transcript.read_thread(INSTAGRAM, thread_id) if r.get("dir") == "in"]


# ---------------------------------------------------------------------------


def test_mention_event_has_its_own_storage_key():
    out = ig.parse_events(_mention(), bot_id=BOT, bot_handle="jarvis")
    check("mention event parsed", len(out) == 1)
    check("mention keeps group context", out[0].context == permissions.CTX_GROUP)
    check("mention is addressed", out[0].mentioned is True)
    check("mention thread is not the sender id", out[0].thread_id != SENDER)
    check("mention thread id carries the sender",
          out[0].thread_id == ig.MENTION_THREAD_PREFIX + SENDER, out[0].thread_id)
    dm = ig.parse_events(_dm(), bot_id=BOT, bot_handle="jarvis")[0]
    check("a DM still stores under the sender id", dm.thread_id == SENDER)
    grp = ig.parse_events(_group(), bot_id=BOT, bot_handle="jarvis")[0]
    check("a group still stores under its thread key", grp.thread_id == GROUP)


def test_window_and_target_helper():
    dm = ig.parse_events(_dm(), bot_id=BOT)[0]
    check("DM: window on the person, no thread_key", ig._window_and_target(dm) == (SENDER, ""))
    grp = ig.parse_events(_group(), bot_id=BOT, bot_handle="jarvis")[0]
    check("group: window and target are the thread",
          ig._window_and_target(grp) == (GROUP, GROUP))
    men = ig.parse_events(_mention(), bot_id=BOT, bot_handle="jarvis")[0]
    check("mention: window and target stay the sender (as before)",
          ig._window_and_target(men) == (SENDER, SENDER))
    # a real group whose id merely starts with the prefix is still a group:
    # it has a channel id, a mention event never does.
    odd = permissions.IncomingMessage(
        platform=INSTAGRAM, context=permissions.CTX_GROUP, user_id=SENDER,
        text="x", channel_id="mention-77", thread_id="mention-77")
    check("a group id that looks like the prefix is not a mention event",
          ig._window_and_target(odd) == ("mention-77", "mention-77"))


def test_dm_and_mention_land_in_different_files_and_conversations():
    cfg = _setup()
    _run(_dm(), cfg)
    _run(_mention(), cfg)
    dm_lines = _in_lines(SENDER)
    men_lines = _in_lines(ig.MENTION_THREAD_PREFIX + SENDER)
    check("the DM is in the sender's file", [r["text"] for r in dm_lines] == ["hello from a DM"],
          str([r["text"] for r in dm_lines]))
    check("the mention is in its own file",
          [r["text"] for r in men_lines] == ["@jarvis look at this"],
          str([r["text"] for r in men_lines]))
    check("the DM file has no group-context line",
          all(r.get("context") == permissions.CTX_DM for r in dm_lines))
    dm_conv = dm_lines[0].get("conv_id") if dm_lines else None
    men_conv = men_lines[0].get("conv_id") if men_lines else None
    check("both got a conversation", bool(dm_conv) and bool(men_conv),
          f"{dm_conv!r} {men_conv!r}")
    check("different conversations", dm_conv != men_conv)


def test_outbound_target_and_window_are_unchanged():
    cfg = _setup()
    _run(_dm(), cfg)
    check("DM reply goes by id, no thread_key", SENT == [(SENDER, "the answer", "")], str(SENT))
    check("DM opened the person's window", ig.window_open_for(SENDER)[0] is True)

    cfg = _setup()
    _run(_mention(), cfg)
    check("mention reply still targets the sender",
          SENT == [(SENDER, "the answer", SENDER)], str(SENT))
    check("mention window is still keyed on the sender", ig.window_open_for(SENDER)[0] is True)
    check("no window was opened under the storage-only id",
          ig.window_open_for(ig.MENTION_THREAD_PREFIX + SENDER)[0] is False)

    cfg = _setup()
    _run(_group(), cfg)
    check("group reply targets the thread", SENT == [(SENDER, "the answer", GROUP)], str(SENT))
    check("group window is keyed on the thread", ig.window_open_for(GROUP)[0] is True)
    check("group message did not open the person's window",
          ig.window_open_for(SENDER)[0] is False)


def test_group_shape_negative_cases_fall_back_to_dm():
    """Pins what parse_events accepts today: nothing new."""
    same = {"entry": [{"messaging": [
        {"sender": {"id": SENDER}, "thread_key": SENDER,
         "message": {"mid": "m", "text": "hi"}}]}]}
    out = ig.parse_events(same, bot_id=BOT)
    check("thread_key equal to the sender is a DM", out[0].context == permissions.CTX_DM)
    nested = {"entry": [{"messaging": [
        {"sender": {"id": SENDER}, "thread": {"id": GROUP},
         "message": {"mid": "m", "text": "hi"}}]}]}
    out = ig.parse_events(nested, bot_id=BOT)
    check("nested thread.id is read as a group", out[0].context == permissions.CTX_GROUP
          and out[0].thread_id == GROUP)
    plain = ig.parse_events(_dm(), bot_id=BOT)
    check("no thread key at all is a DM", plain[0].context == permissions.CTX_DM)


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
