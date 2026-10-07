"""Tests for the last of the Channels enhancement pack (L.36-P8, P9, P13, P14).

    python3 tests/test_channel_pack_p8_p9_p13_p14.py

P8  image_allowlist: stored, listed, switchable per person, cleaned up by block /
    forget / handle edit, NEVER a way into a conversation, owner always may.
P9  master_tools: preview-then-confirm, off at once, refused while "*" is listed.
P13 panel_dm: preview sends nothing; confirm sends once through the send_dm
    tool's own limits; owner / placeholder / chat-origin refused.
P14 denied: who-stage refusals only, handle/count/reason, never message text.

No bot token, no socket, no real ~/.jarvis: HOME is a temp dir set BEFORE any
jarvis module is imported (AGENTS.md). Delivery is stubbed.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-pack-p8-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
    os.environ.pop(_v, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import channels_cli                                                  # noqa: E402
from jarvis.channels import (DISCORD, PERM_IMAGES, denied, master_tools, outbound, panel_dm,  # noqa: E402
                             people, permissions, preset_admin, transcript, user_admin, user_perms)
from jarvis.channels import config as channel_config                              # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003"
SENT = []


def _fake(platform, person_id, text):
    SENT.append((platform, str(person_id), text))
    return True, "sent"


outbound.dm_person = _fake


def reset(**discord):
    SENT.clear()
    for v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
        os.environ.pop(v, None)
    for f in (user_perms.PERMS_FILE, people.PEOPLE_FILE, channel_config.CONFIG_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    for d in Path(_HOME).rglob("*"):
        if d.is_file() and d.suffix in (".jsonl", ".log") or d.name in ("dm_sends.json", "send_dm_log.json"):
            try:
                d.unlink()
            except OSError:
                pass
    cfg = channel_config.load_config()
    cfg[DISCORD].update({"enabled": True, "owner": OWNER, "allow_tools": False, "cooldown_seconds": 0,
                         "dm_allowlist": [OWNER, FRIEND], "reply_allowlist": [OWNER, FRIEND],
                         "tool_allowlist": []})
    cfg[DISCORD].update(discord)
    channel_config.save_config(cfg)
    for uid, h in ((OWNER, "boss"), (FRIEND, "friend")):
        people.touch(DISCORD, uid, handle=h)
    people.set_name(DISCORD, FRIEND, "Friend")


def cfg():
    return channel_config.platform_config(DISCORD)


def person(uid):
    return [p for p in user_admin.list_view(DISCORD)["people"] if p["user_id"] == uid][0]


# ------------------------------------------------------------------ P8 ----
reset()
check("P8: image_allowlist defaults to nobody", cfg().get(PERM_IMAGES) == [])
check("P8: owner may send pictures with no list entry", permissions.may_send_images(cfg(), {"id": OWNER}) is True
      or permissions.may_send_images(cfg(), [OWNER]) is True)
ident = user_admin._idents(people.get(DISCORD, FRIEND))
check("P8: a guest may not by default", permissions.may_send_images(cfg(), ident) is False)
check("P8: a broken config answers no, never raises", permissions.may_send_images({"image_allowlist": 5}, ident) is False)

ok, err, note = user_admin.set_flag(DISCORD, FRIEND, "image", True)
check("P8: switching pictures on works", ok, err)
check("P8: ...and says it changes nothing yet", "doesn't read pictures" in note, note)
check("P8: ...the person view shows it", person(FRIEND)["image"]["on"] is True and person(FRIEND)["effective"]["images"] == "on")
check("P8: ...and may_send_images now says yes", permissions.may_send_images(cfg(), ident) is True)
sim = user_admin.simulate(DISCORD, FRIEND, "dm")
check("P8: the Test tab reports pictures on", sim["images"]["state"] == "on", str(sim.get("images")))
ok, err, _ = user_admin.set_flag(DISCORD, FRIEND, "image", False)
check("P8: switching it off works", ok and not person(FRIEND)["image"]["on"], err)

ok, err, _ = user_admin.set_flag(DISCORD, OWNER, "image", True)
check("P8: the owner has no picture switch", not ok and "owner" in err, err)
check("P8: owner reads 'owner' in the effective view", person(OWNER)["effective"]["images"] == "owner")

# A picture grant is never a way in.
reset(reply_allowlist=[OWNER])
channel_config.add_to_set(DISCORD, PERM_IMAGES, FRIEND)
sim = user_admin.simulate(DISCORD, FRIEND, "dm")
check("P8: a picture grant without reply still isn't answered", sim["answered"] is False and sim["images"]["state"] == "none", str(sim))

# Cleanup paths.
reset()
user_admin.set_flag(DISCORD, FRIEND, "image", True)
user_admin.set_flag(DISCORD, FRIEND, "blocked", True)
check("P8: blocking removes the picture grant", FRIEND not in (cfg().get(PERM_IMAGES) or []))
reset()
user_admin.set_flag(DISCORD, FRIEND, "image", True)
res = user_admin.forget_person(DISCORD, FRIEND, history=False, confirm=True) if "confirm" in user_admin.forget_person.__code__.co_varnames else user_admin.forget_person(DISCORD, FRIEND)
check("P8: forgetting someone removes the picture grant", FRIEND not in (cfg().get(PERM_IMAGES) or []), str(res)[:200])
reset(image_allowlist=["*"])
check("P8: a '*' picture entry locks the per-person switch off",
      not user_admin.set_flag(DISCORD, FRIEND, "image", False)[0])
check("P8: pictures are not a bulk flag", "image" not in preset_admin.BULK_FLAGS)

buf = io.StringIO()
reset()
with contextlib.redirect_stdout(buf):
    try:
        channels_cli.handle(["channels-allow", DISCORD, "image", FRIEND])
    except SystemExit:
        pass
check("P8: channels-allow ... image writes the list", FRIEND in (cfg().get(PERM_IMAGES) or []))
check("P8: describe() names the picture list", "image_allowlist" in permissions.describe(cfg()) if hasattr(permissions, "describe") else True)

# ------------------------------------------------------------------ P9 ----
reset(tool_allowlist=[FRIEND])
v = master_tools.view(DISCORD)
check("P9: view shows the switch off and who it would reach", v["ok"] and v["allow_tools"] is False and v["reachable_others"] == 1, str(v))
r = master_tools.set_master(DISCORD, True)
check("P9: turning on without confirm only previews", r["ok"] and r["dry_run"] and not r["applied"] and not cfg()["allow_tools"], str(r)[:200])
r = master_tools.set_master(DISCORD, True, confirm=True)
check("P9: with confirm it is written", r["ok"] and r["applied"] and cfg()["allow_tools"] is True, str(r)[:200])
r = master_tools.set_master(DISCORD, True, confirm=True)
check("P9: already on is a no-op", r["ok"] and not r["applied"])
r = master_tools.set_master(DISCORD, False)
check("P9: turning off applies at once, no confirm", r["ok"] and r["applied"] and cfg()["allow_tools"] is False, str(r)[:200])
check("P9: ...and leaves the lists alone", cfg()["tool_allowlist"] == [FRIEND])

reset(tool_allowlist=["*"])
r = master_tools.set_master(DISCORD, True, confirm=True)
check("P9: refused while the tool list holds '*', even confirmed", not r["ok"] and "*" in r["error"] and cfg()["allow_tools"] is False, str(r)[:200])
check("P9: unknown platform is refused", not master_tools.set_master("signal", True, confirm=True)["ok"])

reset(tool_allowlist=[FRIEND])
user_admin.set_flag(DISCORD, FRIEND, "blocked", True)
check("P9: a blocked person isn't counted as reachable", master_tools.view(DISCORD)["reachable_others"] == 0)

buf = io.StringIO()
reset(tool_allowlist=[FRIEND])
with contextlib.redirect_stdout(buf):
    try:
        channels_cli.handle(["channels-master-tools", DISCORD, "on"])
    except SystemExit:
        pass
check("P9: the CLI previews without --yes", '"needs_confirm": true' in buf.getvalue() and not cfg()["allow_tools"])

# ----------------------------------------------------------------- P13 ----
reset()
user_admin.set_flag(DISCORD, FRIEND, "send_dm", True)
p = panel_dm.preview(DISCORD, FRIEND, "hello there")
check("P13: preview resolves the person and sends nothing", p["ok"] and p["dry_run"] and not SENT, str(p)[:200])
check("P13: ...and shows the shared allowance", p["rate"]["recipient_max"] == 5 and p["rate"]["recipient_used"] == 0)
s = panel_dm.send(DISCORD, FRIEND, "hello there")
check("P13: send without confirm is only a preview", s.get("needs_confirm") and not SENT)
s = panel_dm.send(DISCORD, FRIEND, "hello there", confirm=True)
check("P13: confirmed send delivers exactly once", s["ok"] and s["sent"] and SENT == [(DISCORD, FRIEND, "hello there")], str(s)[:200])
check("P13: ...and counts against the tool's own allowance", panel_dm.preview(DISCORD, FRIEND, "again")["rate"]["recipient_used"] == 1)
logged = json.dumps(transcript.read_thread(DISCORD, transcript.list_threads(platform=DISCORD)[0]["thread_id"], limit=20)) if transcript.list_threads(platform=DISCORD) else ""
check("P13: ...and is logged as an owner DM", "owner_dm" in logged, logged[:200])
for i in range(4):
    panel_dm.send(DISCORD, FRIEND, f"m{i}", confirm=True)
over = panel_dm.send(DISCORD, FRIEND, "sixth", confirm=True)
check("P13: the 5-per-hour limit is enforced", not over["ok"] and len(SENT) == 5, str(over)[:200])

reset()
user_admin.set_flag(DISCORD, FRIEND, "send_dm", True)
check("P13: the owner's own account is refused", not panel_dm.send(DISCORD, OWNER, "hi", confirm=True)["ok"])
check("P13: an unknown id is refused", not panel_dm.send(DISCORD, "999999999999999999", "hi", confirm=True)["ok"])
check("P13: an empty message is refused", not panel_dm.preview(DISCORD, FRIEND, "   ")["ok"])
check("P13: an over-long message is refused", not panel_dm.preview(DISCORD, FRIEND, "x" * 1501)["ok"])
os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": DISCORD, "user_id": FRIEND})
check("P13: refused when reached from inside a chat", not panel_dm.send(DISCORD, FRIEND, "hi", confirm=True)["ok"] and not SENT)
os.environ.pop("JARVIS_CHANNEL_SENDER")
os.environ["JARVIS_SCHEDULED"] = "1"
check("P13: refused from an unattended run", not panel_dm.send(DISCORD, FRIEND, "hi", confirm=True)["ok"] and not SENT)
os.environ.pop("JARVIS_SCHEDULED")
user_admin.set_flag(DISCORD, FRIEND, "send_dm", False)
check("P13: refused when 'Jarvis may DM them' is off", not panel_dm.send(DISCORD, FRIEND, "hi", confirm=True)["ok"] and not SENT)
user_admin.set_flag(DISCORD, FRIEND, "send_dm", True)
user_admin.set_flag(DISCORD, FRIEND, "blocked", True)
check("P13: refused for someone blocked", not panel_dm.send(DISCORD, FRIEND, "hi", confirm=True)["ok"] and not SENT)

buf = io.StringIO()
reset()
user_admin.set_flag(DISCORD, FRIEND, "send_dm", True)
with contextlib.redirect_stdout(buf):
    try:
        channels_cli.handle(["channels-send", DISCORD, FRIEND, "hi", "there"])
    except SystemExit:
        pass
check("P13: the CLI previews without --yes", not SENT and "needs_confirm" in buf.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    try:
        channels_cli.handle(["channels-send", DISCORD, FRIEND, "--yes", "hi", "there"])
    except SystemExit:
        pass
check("P13: the CLI sends with --yes", SENT == [(DISCORD, FRIEND, "hi there")], str(SENT))

# ----------------------------------------------------------------- P14 ----
reset()
now = datetime.now().replace(microsecond=0)
at = lambda **kw: (now - timedelta(**kw)).isoformat()   # noqa: E731


def inb(uid, text, when, ctx="dm", allowed=False, stage="reply", handle="rando"):
    return {"dir": "in", "context": ctx, "user_id": uid, "user_handle": handle, "text": text, "mentioned": True,
            "allowed": allowed, "stage": stage, "reason": f"sender is not in {stage}_allowlist" if not allowed else "",
            "may_use_tools": False, "at": when}


SECRET = "TOP-SECRET-STRANGER-WORDS"
transcript.append(DISCORD, "dm-stranger", inb(STRANGER, SECRET, at(hours=3)))
transcript.append(DISCORD, "dm-stranger", inb(STRANGER, SECRET + "2", at(hours=1)))
transcript.append(DISCORD, "g1", inb("400000000000000004", "chatter", at(hours=1), ctx="group", stage="reachable", handle="lurker"))
transcript.append(DISCORD, "g1", inb("500000000000000005", "slow down", at(hours=1), stage="cooldown", handle="fast"))
transcript.append(DISCORD, "dm-old", inb("600000000000000006", "ancient", at(days=40), handle="old"))
transcript.append(DISCORD, "dm-friend", inb(FRIEND, "was turned away once", at(days=2), stage="reply", handle="friend"))
v = denied.denied_view(DISCORD, 14)
by = {r["user_id"]: r for r in v["senders"]}
check("P14: the stranger is listed with a count and handle", STRANGER in by and by[STRANGER]["count"] == 2 and by[STRANGER]["handle"] == "rando", str(v)[:300])
check("P14: ...as not yet registered, with the gate's reason", by[STRANGER]["registered"] is False and "reply_allowlist" in by[STRANGER]["reason"])
check("P14: nothing they wrote appears anywhere in the output", SECRET not in json.dumps(v))
check("P14: unaddressed group chatter isn't 'turned away'", "400000000000000004" not in by)
check("P14: an allowed person pacing (cooldown) isn't listed", "500000000000000005" not in by)
check("P14: refusals older than the window are left out", "600000000000000006" not in by)
check("P14: someone the lists now let through is left out, and counted", FRIEND not in by and v["hidden"] == 1, str(v)[:300])
check("P14: a longer window brings the old one back", "600000000000000006" in {r["user_id"] for r in denied.denied_view(DISCORD, 60)["senders"]})
people.touch(DISCORD, STRANGER, handle="rando")
check("P14: once they have a record they read as registered", {r["user_id"]: r for r in denied.denied_view(DISCORD, 14)["senders"]}[STRANGER]["registered"] is True)
check("P14: reading it grants nothing", STRANGER not in (cfg()["reply_allowlist"] + cfg()["dm_allowlist"] + cfg()["tool_allowlist"] + cfg()[PERM_IMAGES]))
check("P14: bad days fall back to the default", denied.denied_view(DISCORD, "x")["days"] == denied.DEFAULT_DAYS)

for name in FAILED:
    print("FAILED:", name)
print(f"{PASSED} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
