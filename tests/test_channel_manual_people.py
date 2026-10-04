"""Tests for hand-added people, renaming and linked accounts (L.36b).

    python3 tests/test_channel_manual_people.py

Covers channels/people.py (add_person, adopt_listed, remove_person, link /
unlink, set_name locking, placeholder adoption, prompt_block), the matching
channels/user_admin.py functions and list_view reconcile, remember_sender's
respect for a locked name, send_dm ignoring a handle-only person, and the
channels-* CLI commands. No bot token, no socket, no real ~/.jarvis: HOME is
redirected to a temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-manual-people-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import channels_cli                                   # noqa: E402
from jarvis.actions import channel_people, send_dm                # noqa: E402
from jarvis.channels import DISCORD, INSTAGRAM, people, user_admin, user_perms  # noqa: E402
from jarvis.channels import config as channel_config              # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND = "100000000000000001", "200000000000000002"
IGSID = "17841400000000001"


def reset(discord=None):
    for f in (user_perms.PERMS_FILE, people.PEOPLE_FILE, channel_config.CONFIG_FILE):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    cfg = channel_config.load_config()
    cfg[DISCORD].update({"enabled": True, "allow_tools": True})
    cfg[DISCORD].update(discord or {})
    channel_config.save_config(cfg)


def ids(view, platform=None):
    return sorted(p["user_id"] for p in view["people"] if platform in (None, p["platform"]))


def cli(*argv):
    """Run one channels command, return (exit_code, parsed_json)."""
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            channels_cli.handle(list(argv))
        except SystemExit as exc:
            code = exc.code or 0
    try:
        return code, json.loads(buf.getvalue())
    except ValueError:
        return code, {}


# ---------------------------------------------------------------- the bug --
reset({"owner": OWNER, "dm_allowlist": [FRIEND], "reply_allowlist": [FRIEND, "@maryem"],
       "tool_allowlist": ["*"]})
view = user_admin.list_view(DISCORD)
check("someone only named in an allow-list now has a row",
      FRIEND in ids(view) and OWNER in ids(view), str(ids(view)))
check("a handle in a list becomes a handle-only row",
      any(p["handle"] == "maryem" and p["placeholder"] for p in view["people"]))
check("'*' creates nobody", not any(p["user_id"] == "*" for p in view["people"]))
check("those rows are marked as added, with no messages",
      all(p["manual"] and p["messages"] == 0 for p in view["people"]))
check("the row reflects the list they were found in",
      [p for p in view["people"] if p["user_id"] == FRIEND][0]["reply"]["on"])
again = user_admin.list_view(DISCORD)
check("reading twice creates no duplicates", ids(again) == ids(view))
check("the owner row is flagged as the owner",
      [p for p in again["people"] if p["user_id"] == OWNER][0]["owner"])

# a message from a listed person continues the same row
people.touch(DISCORD, FRIEND, handle="friend")
rec = people.get(DISCORD, FRIEND)
check("first message continues the row", rec["messages"] == 1 and rec["manual"])
check("still one row for them", ids(user_admin.list_view(DISCORD)).count(FRIEND) == 1)

# ------------------------------------------------------------- add person --
reset()
ok, err, rec, existing = user_admin.add_person(DISCORD, FRIEND, "Friend")
check("add by id works", ok and not existing and rec["user_id"] == FRIEND, err)
check("adding grants nothing",
      channel_config.load_config()[DISCORD]["reply_allowlist"] == []
      and channel_config.load_config()[DISCORD]["tool_allowlist"] == [])
check("a name typed on add is locked", rec["name"] == "Friend" and rec["name_locked"])
ok, err, rec2, existing = user_admin.add_person(DISCORD, FRIEND)
check("adding the same id again selects them, no twin", ok and existing and rec2["user_id"] == FRIEND)
check("no duplicate stored", ids(user_admin.list_view(DISCORD)).count(FRIEND) == 1)
ok, err, *_ = user_admin.add_person(DISCORD, "has space")
check("an id with spaces is refused", not ok and "no spaces" in err)
ok, err, *_ = user_admin.add_person(DISCORD, "-x")
check("a leading dash is refused (it would read as a CLI flag)", not ok)
ok, err, *_ = user_admin.add_person("telegram", "123")
check("unknown platform refused", not ok)

ok, err, ig, _ = user_admin.add_person(INSTAGRAM, "@Bobby_Ig", "Bob")
check("handle-only add makes a placeholder keyed by the lower-cased handle",
      ok and ig["placeholder"] and ig["user_id"] == "bobby_ig" and ig["handle"] == "bobby_ig")

# the directory knows a handle -> a normal row with the real id
from jarvis.channels import directory                              # noqa: E402
directory.record(INSTAGRAM, "known_user", "17841499999999999")
ok, err, kn, _ = user_admin.add_person(INSTAGRAM, "known_user")
check("a handle the directory knows resolves to its id", ok and kn["user_id"] == "17841499999999999" and not kn["placeholder"])

# a switch works on a hand-added person (they are 'registered')
ok, err, note = user_admin.set_flag(DISCORD, FRIEND, "reply", True)
check("switches work on a hand-added person", ok, err)
check("...and write the real allow-list", FRIEND in channel_config.load_config()[DISCORD]["reply_allowlist"])

# ----------------------------------------------- placeholder -> real id ----
user_perms.set_tools(INSTAGRAM, "bobby_ig", "custom", ["read_file"])
user_perms.set_can_dm(INSTAGRAM, "bobby_ig", True)
channel_config.add_to_set(INSTAGRAM, "reply_allowlist", "bobby_ig")
people.touch(INSTAGRAM, IGSID, handle="bobby_ig")
adopted = people.get(INSTAGRAM, IGSID)
check("their first message adopts the placeholder under the real id",
      adopted and not adopted["placeholder"] and adopted["name"] == "Bob" and adopted["manual"])
check("the handle-keyed row is gone", people.get(INSTAGRAM, "bobby_ig") is None)
check("tool limits moved to the real id",
      user_perms.get(INSTAGRAM, IGSID)["tools"] == {"mode": "custom", "allow": ["read_file"]},
      str(user_perms.get(INSTAGRAM, IGSID)))
check("...and the DM switch", user_perms.get(INSTAGRAM, IGSID)["can_dm"] is True)
check("...and nothing is left under the handle",
      user_perms.get(INSTAGRAM, "bobby_ig")["tools"]["mode"] == "inherit")
check("the list entry by handle still lets them through",
      user_admin.membership(channel_config.load_config()[INSTAGRAM], "reply_allowlist", adopted)["on"])
check("no twin created", sum(1 for p in people.all_people(INSTAGRAM) if p.get("handle") == "bobby_ig") == 1)

# ----------------------------------------------------------------- rename --
reset()
people.touch(DISCORD, FRIEND, handle="friend")
ok, err, now = user_admin.rename(DISCORD, FRIEND, "  Fri\n end ")
check("rename cleans to one line", ok and now == "Fri end", now)
check("a manual name is locked", people.get(DISCORD, FRIEND)["name_locked"])
people.set_name(DISCORD, FRIEND, "Guest Typed This")
check("a guest's own name does not overwrite it", people.get(DISCORD, FRIEND)["name"] == "Fri end")
os.environ[channel_people.SENDER_ENV] = json.dumps({"platform": DISCORD, "user_id": FRIEND, "handle": "friend"})
out = channel_people.tool_remember_sender({"name": "Hijack"})
del os.environ[channel_people.SENDER_ENV]
check("remember_sender keeps the owner's name and says so",
      out["ok"] and out["name"] == "Fri end" and out.get("name_kept"), str(out))
ok, err, now = user_admin.rename(DISCORD, FRIEND, "")
check("clearing the name works and unlocks it", ok and now == "" and not people.get(DISCORD, FRIEND)["name_locked"])
people.set_name(DISCORD, FRIEND, "Self Named")
check("an unlocked name can be set by the person again", people.get(DISCORD, FRIEND)["name"] == "Self Named")
ok, err, _n = user_admin.rename(DISCORD, "999", "x")
check("rename refuses someone not on file", not ok)
ok, err, now = user_admin.rename(DISCORD, FRIEND, "x" * 200)
check("a long name is capped", ok and len(now) == people.MAX_NAME_LEN)

# ------------------------------------------------------------------ links --
reset()
people.touch(DISCORD, FRIEND, handle="sam_d")
people.touch(INSTAGRAM, IGSID, handle="sam_ig")
people.set_name(INSTAGRAM, IGSID, "Sam")
ok, err, note = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, "@sam_ig")
check("link by handle", ok, err)
d, i = people.get(DISCORD, FRIEND), people.get(INSTAGRAM, IGSID)
check("stored on both sides", d["linked"] == f"instagram:{IGSID}" and i["linked"] == f"discord:{FRIEND}")
check("name is shared in the view",
      [p for p in user_admin.list_view()["people"] if p["user_id"] == FRIEND][0]["name_effective"] == "Sam")
check("the link is shown with the other account",
      [p for p in user_admin.list_view()["people"] if p["user_id"] == FRIEND][0]["linked"]["handle"] == "sam_ig")
check("the person's own `name` field is untouched", d["name"] == "")
block = people.prompt_block(people.get(DISCORD, FRIEND), DISCORD)
check("the identity block uses the linked name and says it's the same person",
      "They asked to be called Sam" in block and "same person as Sam on instagram" in block, block)
check("a link grants nothing",
      channel_config.load_config()[DISCORD]["reply_allowlist"] == []
      and not [p for p in user_admin.list_view()["people"] if p["user_id"] == FRIEND][0]["reply"]["on"])

# link by id, by name
user_admin.unlink_accounts(DISCORD, FRIEND)
check("unlink clears both sides", not people.get(DISCORD, FRIEND)["linked"] and not people.get(INSTAGRAM, IGSID)["linked"])
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, IGSID)
check("link by id", ok, err)
user_admin.unlink_accounts(DISCORD, FRIEND)
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, "sam")
check("link by name", ok, err)
check("unlinking twice is fine", user_admin.unlink_accounts(DISCORD, FRIEND)[0] and user_admin.unlink_accounts(DISCORD, FRIEND)[0])

# refusals
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, DISCORD, "x")
check("two accounts on one platform can't be linked", not ok and "same platform" in err)
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, "Nobody Called This")
check("an unknown NAME is an error, not a new person", not ok and "Nobody Called This" in err)
check("...and created nothing", len(people.all_people(INSTAGRAM)) == 1)
ok, err, _n = user_admin.link_accounts(DISCORD, "424242", INSTAGRAM, "sam")
check("link from someone not on file refused", not ok)
people.touch(INSTAGRAM, "17841400000000002", handle="sam2")
people.set_name(INSTAGRAM, "17841400000000002", "Sam")
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, "sam")
check("an ambiguous name is refused", not ok and "more than one" in err, err)

# link to an id/handle nobody has used creates a hand-added row
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, "@brand_new")
check("linking an unseen handle adds them", ok and people.get(INSTAGRAM, "brand_new")["placeholder"], err)

# re-linking moves the link and leaves no dangling side
ok, err, _n = user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, IGSID)
check("relink moves it", ok and not people.get(INSTAGRAM, "brand_new")["linked"] and people.get(INSTAGRAM, IGSID)["linked"] == f"discord:{FRIEND}")
other_d = "300000000000000003"
people.touch(DISCORD, other_d, handle="third")
ok, err, _n = user_admin.link_accounts(DISCORD, other_d, INSTAGRAM, IGSID)
check("stealing an Instagram account for someone else unlinks the first", ok and not people.get(DISCORD, FRIEND)["linked"])

# the owner account: link never makes anyone the owner
reset({"owner": OWNER})
people.touch(DISCORD, OWNER, handle="boss", is_owner=True)
people.touch(INSTAGRAM, IGSID, handle="boss_ig")
ok, err, note = user_admin.link_accounts(INSTAGRAM, IGSID, DISCORD, OWNER)
check("linking to the owner account says it doesn't make this one the owner", ok and "doesn't make" in note, note)
v = {(p["platform"], p["user_id"]): p for p in user_admin.list_view()["people"]}
check("...and the Instagram account is not the owner", not v[(INSTAGRAM, IGSID)]["owner"])
block = people.prompt_block(people.get(INSTAGRAM, IGSID), INSTAGRAM)
check("the block still says NOT your owner and doesn't contradict itself",
      "NOT your owner" in block and "same person" not in block, block)

# a dangling link reads as no link
data = people._load()
del data[f"discord:{OWNER}"]
people._save(data)
check("a link to a deleted record reads as none", people.partner(people.get(INSTAGRAM, IGSID)) is None)

# notes from the linked account reach the model
reset()
people.touch(DISCORD, FRIEND, handle="f")
people.touch(INSTAGRAM, IGSID, handle="f_ig")
people.add_note(INSTAGRAM, IGSID, "likes chess")
user_admin.link_accounts(DISCORD, FRIEND, INSTAGRAM, IGSID)
check("a linked account's notes appear in the block",
      "likes chess" in people.prompt_block(people.get(DISCORD, FRIEND), DISCORD))

# ----------------------------------------------------------------- remove --
reset({"owner": OWNER})
user_admin.add_person(DISCORD, FRIEND, "Temp")
channel_config.add_to_set(DISCORD, "reply_allowlist", FRIEND)
ok, err = user_admin.remove_person(DISCORD, FRIEND)
check("a hand-added person who never wrote can be removed", ok, err)
check("...and leaves the lists", FRIEND not in channel_config.load_config()[DISCORD]["reply_allowlist"])
check("...and the sync does not bring them back", FRIEND not in ids(user_admin.list_view(DISCORD)))
people.touch(DISCORD, "400000000000000004", handle="talker")
ok, err = user_admin.remove_person(DISCORD, "400000000000000004")
check("someone who has messaged can't be removed", not ok)
user_admin.add_person(DISCORD, OWNER)
ok, err = user_admin.remove_person(DISCORD, OWNER)
check("the owner can't be removed", not ok and "owner" in err)

# ------------------------------------------------------------ the gate ----
# Nothing about a row or a link may change who is let through.
from jarvis.channels import permissions                                         # noqa: E402
reset({"reply_allowlist": [], "dm_allowlist": []})
user_admin.add_person(DISCORD, FRIEND, "Pal")
cfg = channel_config.load_config()[DISCORD]
msg = permissions.IncomingMessage(DISCORD, permissions.CTX_DM, user_id=FRIEND, user_handle="pal", text="hi")
check("a hand-added person is still denied until a switch is on",
      not permissions.decide(cfg, msg, now=1e9).allowed)

# --------------------------------------------------------------- send_dm --
reset()
user_admin.add_person(INSTAGRAM, "only_a_handle", "Handle Only")
res = send_dm.tool_send_dm({"person": "Handle Only", "message": "hi", "dry_run": True})
check("send_dm doesn't pick a handle-only person as a DM target",
      not res.get("ok") and not res.get("would_send_to"), str(res))
people.touch(INSTAGRAM, IGSID, handle="only_a_handle")  # their first message gives the id
res = send_dm.tool_send_dm({"person": "Handle Only", "message": "hi", "dry_run": True})
check("...but can be found once they have written", res.get("ok") and res.get("dry_run"), str(res))

# ------------------------------------------------------------------- CLI --
reset()
code, out = cli("channels-add-person", "instagram", "@cli_user", "Cli", "Person")
check("CLI add-person", code == 0 and out["ok"] and out["placeholder"] and not out["existing"], str(out))
code, out = cli("channels-add-person", "instagram", "cli_user")
check("CLI add-person on an existing one is not an error", code == 0 and out["existing"], str(out))
code, out = cli("channels-rename", "instagram", "@cli_user", "Renamed")
check("CLI rename by handle", code == 0 and out["name"] == "Renamed", str(out))
code, out = cli("channels-rename", "instagram", "cli_user")
check("CLI rename with no name clears it", code == 0 and out["name"] == "", str(out))
people.touch(DISCORD, FRIEND, handle="dfriend")
code, out = cli("channels-link", "discord", "@dfriend", "instagram", "@cli_user")
check("CLI link", code == 0 and out["ok"], str(out))
check("CLI link stored", people.get(DISCORD, FRIEND)["linked"] == "instagram:cli_user")
code, out = cli("channels-unlink", "discord", FRIEND)
check("CLI unlink", code == 0 and not people.get(DISCORD, FRIEND)["linked"], str(out))
code, out = cli("channels-remove-person", "instagram", "@cli_user")
check("CLI remove-person", code == 0 and people.get(INSTAGRAM, "cli_user") is None, str(out))
code, out = cli("channels-link", "discord")
check("CLI usage error exits non-zero", code != 0)

# ---------------------------------------------------- old records survive --
reset()
legacy = {"platform": DISCORD, "user_id": FRIEND, "handle": "old", "name": "Old", "avatar": "", "notes": [],
          "first_seen": 1.0, "last_seen": 2.0, "messages": 3, "follow": "approved", "is_owner": False, "notified_at": 5.0}
people._save({f"discord:{FRIEND}": legacy})
v = [p for p in user_admin.list_view(DISCORD)["people"] if p["user_id"] == FRIEND][0]
check("a record written before this change still reads",
      v["name"] == "Old" and v["linked"] is None and not v["manual"] and not v["placeholder"] and not v["name_locked"])
check("...and renders the same identity block as before",
      people.prompt_block(legacy, DISCORD).count("same person") == 0)

for name in FAILED:
    print("FAILED:", name)
print(f"{PASSED} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
