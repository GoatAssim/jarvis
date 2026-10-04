"""Prints REAL backend output (JSON) for tests/verify_channels_tabs.js.

    python3 tests/_channels_fixture.py

Seeds a throwaway HOME (never the real ~/.jarvis), then calls the same
functions the CLI calls, so the Node test renders what the backend actually
produces instead of a hand-written guess that could drift from it.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

home = tempfile.mkdtemp(prefix="jarvis-fixture-")
os.environ["HOME"] = home
os.environ["USERPROFILE"] = home
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import DISCORD, people, transcript, usage, user_admin, user_perms  # noqa: E402
from jarvis.channels import config as channel_config                                    # noqa: E402

OWNER, FRIEND, OTHER = "100000000000000001", "200000000000000002", "300000000000000003"
for uid, handle in ((OWNER, "boss"), (FRIEND, "friend"), (OTHER, "other")):
    people.touch(DISCORD, uid, handle=handle)
people.set_name(DISCORD, FRIEND, "Friend")
cfg = channel_config.load_config()
cfg[DISCORD].update({"enabled": True, "owner": OWNER, "allow_tools": True, "cooldown_seconds": 0,
                     "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
                     "tool_allowlist": [OWNER, FRIEND]})
channel_config.save_config(cfg)
user_perms.set_tools(DISCORD, FRIEND, "custom", ["web_search", "get_datetime"])

now = datetime.now().replace(microsecond=0)
at = lambda **kw: (now - timedelta(**kw)).isoformat()   # noqa: E731


def inb(uid, text, when, ctx="dm", allowed=True, stage="allowed", reason=""):
    return {"dir": "in", "context": ctx, "user_id": uid, "user_handle": "h", "text": text, "mentioned": True,
            "allowed": allowed, "stage": stage, "reason": reason, "may_use_tools": allowed, "at": when}


transcript.append(DISCORD, "dm-friend", inb(FRIEND, "what's the weather?", at(days=1)))
transcript.append(DISCORD, "dm-friend", {"dir": "out", "kind": "reply", "text": "Sunny.", "ok": True, "error": "", "provider": "gemini", "at": at(days=1)})
transcript.append(DISCORD, "dm-friend", inb(FRIEND, "<img src=x onerror=alert(1)> **not bold**\nsecond line", at(hours=2)))
transcript.append(DISCORD, "dm-friend", {"dir": "out", "kind": "reply", "text": "I won't render that.", "ok": False, "error": "send failed: 403", "provider": "gemini", "at": at(hours=2), "to_user": FRIEND})
transcript.append(DISCORD, "dm-other", inb(OTHER, "let me in", at(minutes=5), allowed=False, stage="reply", reason="sender is not in reply_allowlist"))
transcript.append(DISCORD, "general", inb(FRIEND, "group hello", at(hours=1), ctx="group"))
transcript.append(DISCORD, "general", {"dir": "out", "kind": "reply", "text": "legacy group reply", "ok": True, "error": "", "provider": "gemini", "at": at(hours=1)})

for d, tokens, tools in ((0, 1200, ["web_search"]), (1, 800, []), (3, 15000, ["web_search", "get_datetime"])):
    usage.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with usage.LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": at(days=d), "platform": DISCORD, "user_id": FRIEND, "thread_id": "dm-friend",
                             "provider": "gemini", "ok": True, "input_tokens": tokens * 3 // 4, "output_tokens": tokens // 4,
                             "thinking_tokens": 0, "total_tokens": tokens, "requests": 1, "tool_calls": len(tools), "tools": tools}) + "\n")

out = {
    "people": user_admin.list_view(DISCORD),
    "conversation": {FRIEND: user_admin.conversation_view(DISCORD, FRIEND), OTHER: user_admin.conversation_view(DISCORD, OTHER)},
    "usage": {"30": user_admin.usage_view(DISCORD, FRIEND, 30), "7": user_admin.usage_view(DISCORD, FRIEND, 7),
              OTHER: user_admin.usage_view(DISCORD, OTHER, 30)},
    "test": {
        f"{FRIEND}|dm": user_admin.simulate(DISCORD, FRIEND, "dm"),
        f"{FRIEND}|group|True": user_admin.simulate(DISCORD, FRIEND, "group", True),
        f"{FRIEND}|group|False": user_admin.simulate(DISCORD, FRIEND, "group", False),
        f"{OTHER}|dm": user_admin.simulate(DISCORD, OTHER, "dm"),
    },
    "ids": {"owner": OWNER, "friend": FRIEND, "other": OTHER},
}
print(json.dumps(out))
