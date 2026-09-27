"""Tests for L.1's recent_dms tool (actions/recent_dms.py).

Plain asserts, no framework, same convention as every other test here.
Everything that touches disk is redirected at ~/.jarvis via a temp HOME so
a test run never reads or writes the real store.

    python3 tests/test_recent_dms.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

# Must be set before the modules that resolve Path.home() at import time.
_TMP = tempfile.mkdtemp(prefix="jarvis-recent-dms-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP

from jarvis.channels import people, permissions, transcript  # noqa: E402
from jarvis.actions import recent_dms as rd  # noqa: E402

DISCORD = "discord"
INSTAGRAM = "instagram"


def _decision(allowed=True, may_use_tools=True):
    return permissions.Decision(allowed, "allowed", "", may_use_tools=may_use_tools)


def _log_dm(platform, thread_id, user_id, handle, text, conv_id="c1", reply=None):
    msg = permissions.IncomingMessage(
        platform=platform, context=permissions.CTX_DM, user_id=user_id,
        user_handle=handle, text=text, thread_id=thread_id,
        message_id=f"{thread_id}-{user_id}-{text[:8]}")
    transcript.log_inbound(platform, msg, _decision(), conv_id=conv_id)
    if reply:
        transcript.log_outbound(platform, thread_id, reply, conv_id=conv_id)


def _log_group(platform, thread_id, user_id, handle, text):
    msg = permissions.IncomingMessage(
        platform=platform, context=permissions.CTX_GROUP, user_id=user_id,
        user_handle=handle, text=text, thread_id=thread_id, mentioned=True,
        message_id=f"{thread_id}-{user_id}-{text[:8]}")
    transcript.log_inbound(platform, msg, _decision(), conv_id="cg")


def _clear_sender():
    os.environ.pop(rd.SENDER_ENV, None)


def _set_sender(platform, user_id, is_owner):
    os.environ[rd.SENDER_ENV] = json.dumps(
        {"platform": platform, "user_id": user_id, "is_owner": is_owner})


# --- basic shape -------------------------------------------------------

def test_a0_empty_history_is_ok_not_an_exception():
    """Named to sort first — _run() executes tests alphabetically against
    one shared temp store (same convention as test_channel_people.py), so
    this needs to run before any other test writes a transcript line."""
    _clear_sender()
    out = rd.tool_recent_dms({"platform": DISCORD})
    assert out == {"ok": True, "threads": []}, out


def test_lists_a_dm_thread_newest_first():
    _clear_sender()
    _log_dm(DISCORD, "dm-a", "u-a", "alice", "hello", reply="hi alice")
    _log_dm(DISCORD, "dm-b", "u-b", "bob", "yo")
    out = rd.tool_recent_dms({"platform": DISCORD})
    assert out["ok"] is True
    ids = [t["thread_id"] for t in out["threads"]]
    # "b" was touched last, so it must lead.
    assert ids[0] == "dm-b", ids


def test_excludes_group_channels():
    _clear_sender()
    _log_dm(DISCORD, "dm-c", "u-c", "cara", "hi there")
    _log_group(DISCORD, "guild-general", "u-d", "dave", "hello everyone")
    out = rd.tool_recent_dms({"platform": DISCORD})
    ids = [t["thread_id"] for t in out["threads"]]
    assert "guild-general" not in ids, ids
    assert "dm-c" in ids, ids


def test_person_prefers_saved_name_over_handle():
    _clear_sender()
    _log_dm(DISCORD, "dm-e", "u-e", "handle-e", "hey")
    people.set_name(DISCORD, "u-e", "Eve R.")
    out = rd.tool_recent_dms({"platform": DISCORD})
    entry = next(t for t in out["threads"] if t["thread_id"] == "dm-e")
    assert entry["person"] == "Eve R.", entry


def test_person_falls_back_to_handle_with_no_saved_name():
    _clear_sender()
    _log_dm(DISCORD, "dm-f", "u-f", "handle-f", "hey")
    out = rd.tool_recent_dms({"platform": DISCORD})
    entry = next(t for t in out["threads"] if t["thread_id"] == "dm-f")
    assert entry["person"] == "handle-f", entry


def test_unknown_platform_is_rejected():
    _clear_sender()
    out = rd.tool_recent_dms({"platform": "telegram"})
    assert out["ok"] is False
    assert "telegram" in out["error"]


def test_platform_filter_is_respected():
    _clear_sender()
    _log_dm(DISCORD, "dm-g", "u-g", "gina", "hi")
    _log_dm(INSTAGRAM, "dm-h", "u-h", "hank", "hi")
    out = rd.tool_recent_dms({"platform": INSTAGRAM})
    platforms = {t["platform"] for t in out["threads"]}
    assert platforms == {INSTAGRAM}, platforms


# --- L.1.4 truncation ---------------------------------------------------

def test_limit_is_clamped_to_max_threads():
    _clear_sender()
    for i in range(rd.MAX_THREADS + 5):
        _log_dm(DISCORD, f"dm-bulk-{i}", f"u-bulk-{i}", f"h{i}", "hi")
    out = rd.tool_recent_dms({"platform": DISCORD, "limit": 999})
    assert len(out["threads"]) <= rd.MAX_THREADS, len(out["threads"])


def test_messages_per_thread_is_clamped():
    _clear_sender()
    thread = "dm-many-msgs"
    for i in range(rd.MAX_MESSAGES_PER_THREAD + 5):
        _log_dm(DISCORD, thread, "u-many", "many", f"message {i}")
    out = rd.tool_recent_dms(
        {"platform": DISCORD, "limit": 1, "messages_per_thread": 999})
    assert len(out["threads"][0]["messages"]) <= rd.MAX_MESSAGES_PER_THREAD, \
        out["threads"][0]["messages"]


def test_long_message_text_is_trimmed():
    _clear_sender()
    long_text = "x" * (rd.MAX_MESSAGE_CHARS * 2)
    _log_dm(DISCORD, "dm-long", "u-long", "long", long_text)
    out = rd.tool_recent_dms({"platform": DISCORD, "limit": 1})
    text = out["threads"][0]["messages"][-1]["text"]
    assert len(text) <= rd.MAX_MESSAGE_CHARS + 1, len(text)  # +1 for the ellipsis char
    assert text.endswith("\u2026")


# --- corrupt data --------------------------------------------------------

def test_a_torn_trailing_line_does_not_crash():
    _clear_sender()
    _log_dm(DISCORD, "dm-torn", "u-torn", "torn", "hello")
    path = transcript.thread_path(DISCORD, "dm-torn")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    out = rd.tool_recent_dms({"platform": DISCORD, "limit": 5})
    assert out["ok"] is True
    ids = [t["thread_id"] for t in out["threads"]]
    assert "dm-torn" in ids, ids


# --- L.1.2 owner-only gate ------------------------------------------------

def test_pc_context_is_always_allowed():
    """No JARVIS_CHANNEL_SENDER at all = a local/PC call, not a chat
    message on someone else's behalf — always the owner's own context."""
    _clear_sender()
    out = rd.tool_recent_dms({})
    assert out["ok"] is True


def test_owner_chat_context_is_allowed():
    _set_sender(DISCORD, "owner-1", is_owner=True)
    try:
        out = rd.tool_recent_dms({})
        assert out["ok"] is True
    finally:
        _clear_sender()


def test_non_owner_chat_context_is_refused():
    _log_dm(DISCORD, "dm-secret", "u-secret", "secret", "private stuff")
    _set_sender(DISCORD, "u-someone-else", is_owner=False)
    try:
        out = rd.tool_recent_dms({})
        assert out["ok"] is False
        assert "owner" in out["error"]
        # Must not leak any thread data alongside the refusal.
        assert "threads" not in out
    finally:
        _clear_sender()


def test_malformed_sender_env_does_not_crash_or_leak():
    for junk in ("not json", "[]", '{"platform": "discord"}', ""):
        os.environ[rd.SENDER_ENV] = junk
        # A malformed/incomplete blob means _current_sender() can't tell
        # who's asking, so it must NOT be treated as "definitely the
        # owner" — see _current_sender()'s own None-on-anything-off rule.
        out = rd.tool_recent_dms({})
        assert out["ok"] is True, junk  # falls back to the PC-context path
    _clear_sender()


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
