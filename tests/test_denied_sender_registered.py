"""Senders who DM/mention the bot but are refused by the gate still get a
people.json record, so they show up in the Channels panel."""
import sys, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "jarvis-cli"))
from jarvis.channels import base, people, permissions, config as cc

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

tmp = pathlib.Path(tempfile.mkdtemp())
people.PEOPLE_FILE = tmp / "people.json"
base._log = lambda *a, **k: None
base.transcript.log_inbound = lambda *a, **k: None

cfg = {"enabled": True, "owner": "1", "dm_allowlist": [], "reply_allowlist": [], "tool_allowlist": []}
dm = permissions.IncomingMessage("discord", permissions.CTX_DM, user_id="42", user_handle="Stranger", text="hi", mentioned=True, message_id="m1")
d = base.handle_message("discord", dm, lambda t: True, cfg=cfg)
check("DM from non-allowlisted sender is denied", not d.allowed)
rec = people.get("discord", "42")
check("...but is registered", rec is not None and rec["handle"] == "stranger")
check("...with no owner ping state", rec is not None and not rec.get("notified_at"))

chatter = permissions.IncomingMessage("discord", permissions.CTX_GROUP, user_id="77", user_handle="rando", text="lol", mentioned=False, message_id="m2")
base.handle_message("discord", chatter, lambda t: True, cfg=cfg)
check("unaddressed server chatter is NOT registered", people.get("discord", "77") is None)
sys.exit(1 if fails else 0)
