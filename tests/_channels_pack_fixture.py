"""Prints REAL backend output (JSON) for tests/verify_l36_pack_ui.py.

    python3 tests/_channels_pack_fixture.py

A throwaway HOME (never the real ~/.jarvis); calls the same functions the CLI
calls, so the browser test renders what the backend really produces for the
last pack items (pictures, the platform tools switch, panel DM, turned-away
senders) rather than a hand-written guess that could drift from it.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

home = tempfile.mkdtemp(prefix="jarvis-pack-fixture-")
os.environ["HOME"] = home
os.environ["USERPROFILE"] = home
for v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
    os.environ.pop(v, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import DISCORD, denied, master_tools, panel_dm, people, transcript, user_admin  # noqa: E402
from jarvis.channels import config as channel_config                                             # noqa: E402

OWNER, FRIEND, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003"
for uid, handle in ((OWNER, "boss"), (FRIEND, "friend")):
    people.touch(DISCORD, uid, handle=handle)
people.set_name(DISCORD, FRIEND, "Friend")
cfg = channel_config.load_config()
cfg[DISCORD].update({"enabled": True, "owner": OWNER, "allow_tools": False, "cooldown_seconds": 0,
                     "bot_token": "dummy-fixture-token",
                     "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
                     "tool_allowlist": [FRIEND]})
channel_config.save_config(cfg)
user_admin.set_flag(DISCORD, FRIEND, "send_dm", True)

now = datetime.now().replace(microsecond=0)
SECRET = "STRANGER-PRIVATE-WORDS"
for hours in (5, 2):
    transcript.append(DISCORD, "dm-stranger", {
        "dir": "in", "context": "dm", "user_id": STRANGER, "user_handle": "rando", "text": SECRET,
        "mentioned": True, "allowed": False, "stage": "reply", "reason": "sender is not in reply_allowlist",
        "may_use_tools": False, "at": (now - timedelta(hours=hours)).isoformat()})

print(json.dumps({
    "people": user_admin.list_view(DISCORD),
    "master": master_tools.view(DISCORD),
    "denied": denied.denied_view(None, 14),
    "dm_preview": panel_dm.preview(DISCORD, FRIEND, "Hello from the panel"),
    "test": {f"{FRIEND}|dm": user_admin.simulate(DISCORD, FRIEND, "dm")},
    "ids": {"owner": OWNER, "friend": FRIEND, "stranger": STRANGER},
    "secret": SECRET,
}))
