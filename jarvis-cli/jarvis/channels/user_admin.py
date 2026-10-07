"""One person, every switch — the logic behind Menu > Channels > People.

WHY A MODULE OF ITS OWN
-----------------------
The Channels panel (web) and `jarvis channels-user` (terminal) must do exactly
the same thing when a switch is flipped. server.js shells out to the CLI rather
than re-implementing any of it (same convention as every other channels
route), and the CLI delegates here, so there is one implementation and
tests/test_user_admin.py can pin it without a browser, a bot token or a socket.

NOTHING HERE DECIDES ACCESS
---------------------------
permissions.decide() still does that, from the three allow-lists in config.py.
The first three switches below (dm / reply / tool) simply add or remove one
person's stable id in those same lists via config.add_to_set / remove_from_set
— the call `channels-allow` already makes — so the CLI, the panel and a
hand-edit of channels.json can never disagree about who is allowed. The other
switches are a different kind of thing and live elsewhere on purpose:

    owner     config.py's single `owner` string (who receives DMs and is trusted
              as the owner inside a chat). ONE per platform, so switching it
              on for someone moves it off whoever had it.
    send_dm   user_perms.can_dm — may Jarvis DM this person for the owner.
    blocked   people.follow == blocked, PLUS removal from all three lists. The
              older `channels-block` only records "stop asking me"; a panel
              Block that left someone in reply_allowlist would still answer
              them, which is not what a Block button means.
    tools     user_perms.tools — which tools, when `tool` is on.

REGISTERED ONLY — AND WHO COUNTS AS REGISTERED
----------------------------------------------
Every function refuses a person who has no people.json record. That is the
rule the panel is built on, so it is enforced here and not left to the UI.
A record comes from one of three places: a message (people.touch), the owner
adding someone by hand (add_person), or an allow-list that already names
someone who hasn't written yet. The third is what list_view() reconciles on
every read: anyone in dm/reply/tool_allowlist or `owner` gets a record, so a
person added through "Global lists" or `channels-allow` shows up in the panel
instead of being invisible until their first message.

READ-ONLY VIEWS  (L.36-P1 / P2 / P3)
------------------------------------
conversation_view, usage_view and simulate change nothing. The first two read
the transcript and the usage ledger; simulate is a dry run of the gate for one
person ("Test as this person") that calls no model, sends nothing, and writes
NOTHING -- no transcript line, no usage line, no people.json touch, no
cooldown mark, no conversation. tests/test_channel_insights.py snapshots the
whole ~/.jarvis tree around a run to hold it to that.

NOTES AND "FORGET THIS PERSON"  (L.36-P11)
-----------------------------------------
`edit_note` / `delete_note` change one line of what Jarvis remembers about a
person, by note id (people.note_id), never by position.

`forget_person` is the one real wipe, and it is deliberately the opposite of
a switch: it can only ever take things away. In this order -- so that a
failure part-way leaves the person LESS able to reach Jarvis, never more, and
the record in place so it can simply be run again:

    1. off every dm / reply / tool list, under every spelling they use
    2. their stored limits (user_perms) deleted
    3. optionally, the logs of threads that are only their DMs, and the
       model-facing conversations those logs point at
    4. their handle -> id entries in the directory
    5. the people.json record itself, and the other half of a link

It REFUSES the owner (hand ownership over first), anyone a `"*"` entry covers
in any list (step 2 would delete a tool limit they would then be let through
without -- the same reason a switch cannot undo a wildcard), and a person
whose limits file cannot be read (unknown limits are never wiped). It leaves
the usage ledger (counts only), group-thread lines other people also wrote in,
and a linked account on the other platform alone, and says so in its report.
No model tool reaches it (AGENTS.md); the panel and `jarvis channels-forget`
are the only callers, and the CLI only acts when told `--yes`.

TIME-LIMITED TOOLS  (L.36-P6)
-----------------------------
`grant_tools_for` switches tool use on for one person until a deadline. The
grant itself is the ordinary tool_allowlist entry; the deadline lives in
user_perms.tools_until and is only a ceiling on it. Two things end it:

    every message   user_perms.resolve_tool_access() answers "no tools" for a
                    person whose deadline has passed, whatever the list says
                    (this is the enforcement; it reads the clock itself and
                    needs no sweeper to be running)
    a sweep         expire_due() takes the person out of the tool list, so the
                    panel and `channels-users` stop showing tools as on. It
                    runs when the panel loads the people, when a message
                    arrives from someone whose time ran out, and from the
                    terminal; never from the "Test as this person" dry run.

Writes are ordered so a failure part-way leaves LESS access, never more: the
deadline is stored BEFORE the list entry is added, and removed AFTER the entry
is. Setting `tool` on or off by hand, blocking, or forgetting someone ends the
countdown (on = for good, off = revoked). The deadline is ignored for
the owner (they cannot be given one) and for anyone a "*" entry covers in the
tool list (a switch for one person cannot undo that, so a deadline for one
person cannot either).

THE OWNER'S INSTRUCTION FOR ONE PERSON  (L.36-P12)
--------------------------------------------------
`set_instruction` stores one owner-typed line about HOW to talk to a person
(people.py explains the rules and why it is safe in the prompt). It is style
only and never touches a permission, so it is not written to the change log.

WHAT A SWITCH CANNOT DO
-----------------------
There is no deny-list in the gate. A person covered by a `"*"` entry cannot be
switched off individually — set_flag() refuses and says why instead of
removing an id that was never the reason they got through.
"""

import time

from . import PLATFORMS, PERM_DM, PERM_REPLY, PERM_TOOLS, PERM_IMAGES
from . import config as channel_config
from . import changelog, people, permissions, presets, transcript, usage, user_perms
from .config import WILDCARD

FLAGS = ("dm", "reply", "tool", "image", "owner", "send_dm", "blocked")
_SET_FOR = {"dm": PERM_DM, "reply": PERM_REPLY, "tool": PERM_TOOLS,
            "image": PERM_IMAGES}
# Every allow-list a person can be named in. ONE tuple, used by every place that
# walks "all of someone's lists" (block, forget, handle edit, listing), so a list
# added later cannot be forgotten in one of them and leave a grant behind.
LISTS = (PERM_DM, PERM_REPLY, PERM_TOOLS, PERM_IMAGES)

# Platform fields that carry a secret. Mirrors config.redacted(): the view
# built here only ever says whether one is set.
_TOKEN_FIELDS = ("bot_token", "access_token")


def _idents(rec):
    out = []
    for value in (rec.get("user_id"), rec.get("handle")):
        text = channel_config.normalize_entry(value)
        if text and text not in out:
            out.append(text)
    return out


def membership(cfg, which, rec):
    """Is this person in one of the three lists, and by what route?

    `via` is "wildcard" when a "*" entry covers them (which a per-person switch
    cannot undo), "list" when their id or handle is named, "none" otherwise.
    `explicit` says whether they are ALSO named, so the panel can tell "named
    and covered" from "only covered"."""
    entries = channel_config.normalize_entries((cfg or {}).get(which))
    ids = _idents(rec)
    wildcard = WILDCARD in entries
    explicit = any(i in entries for i in ids)
    via = "wildcard" if wildcard else ("list" if explicit else "none")
    return {"on": wildcard or explicit, "via": via, "explicit": explicit}


def _linked_view(rec):
    """The other half of a link, as the panel shows it, or None."""
    other = people.partner(rec)
    if not other:
        return None
    return {
        "platform": other.get("platform") or "",
        "user_id": str(other.get("user_id") or ""),
        "handle": other.get("handle") or "",
        "name": other.get("name") or "",
        "avatar": other.get("avatar") or "",
        "owner": bool(other.get("is_owner")),
    }


def person_view(platform, rec, cfg, perms):
    """One person as the panel renders them: identity plus every switch."""
    cfg = cfg or {}
    ids = _idents(rec)
    follow = rec.get("follow") or people.FOLLOW_UNKNOWN
    owner = permissions.is_owner(cfg, ids)
    view = {
        "platform": platform,
        "user_id": str(rec.get("user_id") or ""),
        "handle": rec.get("handle") or "",
        "name": rec.get("name") or "",
        # What the panel should call them: their own name, else the linked
        # account's. `name_from` says which, so the Details row can be honest.
        "name_effective": people.effective_name(rec),
        "name_from": ("own" if rec.get("name") else
                      "linked" if people.effective_name(rec) else ""),
        "name_locked": bool(rec.get("name_locked")),
        "manual": bool(rec.get("manual")),
        # Only a handle is known; the id arrives with their first message.
        "placeholder": bool(rec.get("placeholder")),
        # The handle can be hand-edited only before they have written and when
        # they are not the owner (set_handle enforces the same rule).
        "handle_editable": handle_edit_refusal(rec, owner) == "",
        "linked": _linked_view(rec),
        "avatar": rec.get("avatar") or "",
        "first_seen": rec.get("first_seen"),
        "last_seen": rec.get("last_seen"),
        "messages": int(rec.get("messages") or 0),
        "notes": [n for n in (rec.get("notes") or []) if isinstance(n, str)],
        # The same notes with their ids, which the edit / delete controls use.
        "note_items": people.list_notes(rec),
        "follow": follow,
        "owner": bool(owner),
        "blocked": follow == people.FOLLOW_BLOCKED,
        "dm": membership(cfg, PERM_DM, rec),
        "reply": membership(cfg, PERM_REPLY, rec),
        "tool": membership(cfg, PERM_TOOLS, rec),
        # L.36-P8: may their pictures be read (once inbound images exist). The
        # owner always may, whether or not they are named in the list.
        "image": membership(cfg, PERM_IMAGES, rec),
        "send_dm": bool(perms["can_dm"]),
        "tools": perms["tools"],
        # L.36-P6: when their tool access ends by itself (epoch seconds, 0 =
        # it doesn't). `tool_expired` is a deadline that has passed but that
        # nothing has swept yet; list_view sweeps first, so it is only ever
        # true in the instant between the two.
        "tool_until": int(perms.get("tools_until") or 0),
        "tool_expired": user_perms.is_expired(perms.get("tools_until")),
        # L.36-P12: the owner's own line on how to talk to them.
        "instruction": rec.get("instruction") or "",
        "instruction_max": people.MAX_INSTRUCTION_LEN,
    }
    # A one-glance summary of what this person can do right now. It restates
    # the gate's own rule (permissions.decide: allow_tools AND tool_allowlist)
    # so the badge on a card can never promise more than the gate gives.
    tools_live = (bool(cfg.get("allow_tools")) and view["tool"]["on"]
                  and not view["tool_expired"])
    if not (view["reply"]["on"] and tools_live):
        tools_state = "none"
    elif perms["tools"]["mode"] == user_perms.TOOLS_CUSTOM:
        tools_state = "custom"
    else:
        tools_state = "all"
    view["effective"] = {
        "answered": bool(view["reply"]["on"]),
        "tools": tools_state,
        "tool_count": (len(perms["tools"]["allow"])
                       if tools_state == "custom" else None),
        # The platform-wide master switch is off, so the person's own tool
        # permission has no effect yet. The panel says so rather than showing
        # a green switch that does nothing.
        "tools_blocked_by_platform": (view["tool"]["on"]
                                      and not cfg.get("allow_tools")),
        # owner = always; on = named (or covered by "*"); off = not. Only
        # meaningful for someone who is answered at all.
        "images": ("owner" if owner else
                   "on" if view["image"]["on"] else "off"),
    }
    return view


def platform_view(platform, cfg):
    cfg = cfg or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "token_set": any(str(cfg.get(f) or "").strip() for f in _TOKEN_FIELDS),
        "owner": cfg.get("owner") or "",
        "allow_tools": bool(cfg.get("allow_tools")),
        # Keyed by the short switch names (dm / reply / tool), the same words
        # the panel and `channels-user` use.
        "wildcard": {
            short: WILDCARD in channel_config.normalize_entries(cfg.get(which))
            for short, which in _SET_FOR.items()
        },
    }


def list_view(platform=None):
    """Everything the panel needs in one call: a header per platform and one
    card per registered person (most recently seen first). `perms_error` is set
    instead of guessing when user_perms.json is unreadable."""
    # L.36-P6: first take out anyone whose time-limited tool access has run
    # out, so the lists read below are the truth.
    expired_now = expire_due(platform if platform in PLATFORMS else None)
    cfg_all = channel_config.load_config()
    platforms = [platform] if platform in PLATFORMS else list(PLATFORMS)
    for p in platforms:
        sync_listed(p, cfg_all.get(p) or {})
    perms_error = ""
    try:
        store = user_perms._load()
    except user_perms.PermsUnreadable as exc:
        store, perms_error = {}, str(exc)
    out_people = []
    for p in platforms:
        for rec in people.all_people(p):
            if not isinstance(rec, dict) or not rec.get("user_id"):
                continue
            perms = user_perms.normalize(store.get(user_perms.key(p, rec["user_id"])))
            out_people.append(person_view(p, rec, cfg_all.get(p) or {}, perms))
    return {
        "ok": True,
        "platforms": {p: platform_view(p, cfg_all.get(p) or {}) for p in platforms},
        "people": out_people,
        "perms_error": perms_error,
        # People whose time limit ended during THIS call (the panel says so
        # once). The permanent record is the History tab.
        "expired_now": expired_now,
        # Quick setups (L.36-P4): fixed in code, so the panel needs no call of
        # its own to know what to offer.
        "presets": presets.public_view(),
    }


def sync_listed(platform, cfg):
    """Make sure everyone an allow-list (or `owner`) names has a record.
    Never raises: a store that can't be written leaves the panel showing what
    it already had rather than failing to open. Returns how many were made."""
    entries = []
    for which in LISTS:
        entries.extend(channel_config.normalize_entries((cfg or {}).get(which)))
    owner = channel_config.normalize_entry((cfg or {}).get("owner"))
    if owner:
        entries.append(owner)
    try:
        return people.adopt_listed(platform, [e for e in entries if e != WILDCARD])
    except Exception:  # noqa: BLE001
        return 0


def _registered(platform, user_id):
    if platform not in PLATFORMS:
        return None, f"unknown platform '{platform}'"
    uid = str(user_id or "").strip()
    rec = people.get(platform, uid) if uid else None
    if not rec:
        return None, (f"{uid or 'that person'} isn't registered on {platform} "
                      f"— only people who have messaged Jarvis appear here")
    return rec, ""


def _remove_everywhere(platform, which, rec):
    """Take a person out of one list under every spelling the list might use
    (stable id, bare handle, @handle all normalize to the same entry)."""
    ok, err = True, ""
    for ident in _idents(rec):
        ok, err = channel_config.remove_from_set(platform, which, ident)
        if not ok:
            break
    return ok, err


def list_flag_refusal(flag, value, rec, cfg):
    """Why a dm / reply / tool switch cannot be set to `value` for this person,
    or "" when it can.

    ONE place for the two refusals, used by set_flag() and by the quick-setup
    preview (preset_admin), so a preview can never promise what the switch
    would then refuse: turning one ON for someone who is blocked, and turning
    one OFF for someone a "*" (everyone) entry still covers."""
    if flag not in _SET_FOR:
        return ""
    if flag == "image" and permissions.is_owner(cfg, _idents(rec)):
        return ("that's the owner \u2014 your own pictures are always read, "
                "so there is no switch for you")
    if value:
        if (rec.get("follow") or "") == people.FOLLOW_BLOCKED:
            return "they're blocked — unblock them first"
        return ""
    if membership(cfg, _SET_FOR[flag], rec)["via"] == "wildcard":
        return (f"covered by \"*\" (everyone) in the {flag} list — "
                f"a switch for one person can't undo that")
    return ""


def _image_note(cfg, rec):
    """The heads-up after switching pictures ON for someone. Honest about the
    two things the switch does not do: it changes nothing today (inbound
    pictures are not built, L.42), and it opens no conversation."""
    note = ("recorded \u2014 Jarvis doesn't read pictures sent in chat yet, so "
            "this changes nothing until that feature exists")
    if not membership(cfg, PERM_REPLY, rec)["on"]:
        note += "; they also need Reply switched on to be answered at all"
    return note


def set_flag(platform, user_id, flag, value):
    """Flip one switch. Returns (ok, error, note); `note` is a heads-up the
    panel shows after a SUCCESSFUL change (something still true that the person
    flipping the switch might not expect)."""
    if flag not in FLAGS:
        return False, f"unknown switch '{flag}' — use one of: {', '.join(FLAGS)}", ""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, ""
    uid = str(rec["user_id"])
    value = bool(value)
    cfg = channel_config.platform_config(platform)
    blocked = (rec.get("follow") or "") == people.FOLLOW_BLOCKED

    if flag in _SET_FOR:
        which = _SET_FOR[flag]
        refusal = list_flag_refusal(flag, value, rec, cfg)
        if refusal:
            return False, refusal, ""
        if value:
            ok, err = channel_config.add_to_set(platform, which, uid)
            if ok and flag == "image":
                return True, "", _image_note(cfg, rec)
            # Switching tools on by hand means "for good": any countdown from
            # an earlier time-limited grant ends here. Done AFTER the list
            # write, so a failure leaves the person with a deadline still
            # running, which is less access, not more.
            return ok, err, (end_countdown(platform, uid)
                             if ok and flag == "tool" else "")
        ok, err = _remove_everywhere(platform, which, rec)
        # Off means revoked: the list entry is gone first, then the countdown.
        return ok, err, (end_countdown(platform, uid)
                         if ok and flag == "tool" else "")

    if flag == "send_dm":
        try:
            user_perms.set_can_dm(platform, uid, value)
        except (OSError, user_perms.PermsUnreadable) as exc:
            return False, str(exc), ""
        return True, "", ""

    if flag == "owner":
        is_now_owner = permissions.is_owner(cfg, _idents(rec))
        if value:
            if blocked:
                return False, "they're blocked — unblock them first", ""
            ok, err = channel_config.set_value(platform, "owner", uid)
            if not ok:
                return False, err, ""
            # One owner per platform: whoever held it is no longer the owner.
            for other in people.all_people(platform):
                if str(other.get("user_id")) != uid and other.get("is_owner"):
                    people.set_owner_flag(platform, other["user_id"], False)
            people.set_owner_flag(platform, uid, True)
            people.set_follow(platform, uid, people.FOLLOW_APPROVED)
            return True, "", ("owner status grants no replies or tools by itself "
                              "— set those switches for them too")
        if not is_now_owner:
            return True, "", ""  # already not the owner: idempotent
        ok, err = channel_config.set_value(platform, "owner", "")
        if not ok:
            return False, err, ""
        people.set_owner_flag(platform, uid, False)
        return True, "", (f"{platform} has no owner now — Jarvis has nobody to "
                          f"notify there until you pick one")

    # flag == "blocked"
    if value:
        if permissions.is_owner(cfg, _idents(rec)):
            return False, "that's the owner — switch ownership to someone else first", ""
        for which in LISTS:
            if membership(cfg, which, rec)["via"] == "wildcard":
                wildcard_note = ("a \"*\" (everyone) entry still covers them in "
                                 "at least one list — blocking can't override it")
                break
        else:
            wildcard_note = ""
        for which in LISTS:
            ok, err = _remove_everywhere(platform, which, rec)
            if not ok:
                return False, err, ""
        end_countdown(platform, uid)
        people.set_follow(platform, uid, people.FOLLOW_BLOCKED)
        return True, "", wildcard_note
    # Unblocking goes back to "unknown", not "approved": a panel Block removed
    # them from every list, so approving them would claim a permission they no
    # longer have. "unknown" grants nothing and (notified_at is already set)
    # asks nobody anything; the owner switches things back on from zero.
    people.set_follow(platform, uid, people.FOLLOW_UNKNOWN)
    return True, "", "unblocked — they start with nothing switched on"


def set_tools(platform, user_id, mode, names=None):
    """Set one person's tool scope. Returns (ok, error)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err
    try:
        user_perms.set_tools(platform, rec["user_id"], mode, names or [])
    except (ValueError, OSError, user_perms.PermsUnreadable) as exc:
        return False, str(exc)
    return True, ""


# --------------------------------------------------------------------------
# Time-limited tool access  (L.36-P6)
# --------------------------------------------------------------------------

def end_countdown(platform, uid):
    """Drop a stored tool deadline. Returns "" when done (or when there was
    none), else a heads-up for the person flipping the switch: the deadline
    could not be cleared, so it will still end their tool use when it comes.
    Never raises, and never widens anything -- a deadline left in place only
    ever means less access."""
    try:
        user_perms.set_tools_until(platform, uid, 0)
    except (OSError, ValueError, user_perms.PermsUnreadable) as exc:
        return (f"the earlier time limit could not be cleared ({exc}) \u2014 "
                f"it will still end their tool use when it runs out")
    return ""


def grant_refusal(platform, rec, cfg):
    """Why this person cannot be given a time-limited grant, or "". One place
    for the preview text and the real thing."""
    if permissions.is_owner(cfg, _idents(rec)):
        return ("that's the owner \u2014 a time limit would cut off your own "
                "account inside chats. Use the Tool use switch instead")
    if (rec.get("follow") or "") == people.FOLLOW_BLOCKED:
        return "they're blocked \u2014 unblock them first"
    if membership(cfg, PERM_TOOLS, rec)["via"] == "wildcard":
        return ("covered by \"*\" (everyone) in the tool list \u2014 a time "
                "limit for one person can't undo that. Take the \"*\" out first")
    return ""


def grant_tools_for(platform, user_id, minutes, now=None):
    """Switch tool use on for one person until `minutes` from now.
    Returns (ok, error, note, until) where `until` is epoch seconds.

    Running it again for someone who already has a countdown replaces it
    (extends or shortens). The tool LIST (which tools) is untouched: a custom
    list the owner set stays, and a person with none gets whatever tool use
    already meant for them. Writes the deadline first, then the list entry;
    see the module docstring."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, "", 0
    try:
        minutes = user_perms.grant_minutes_ok(minutes)
    except ValueError as exc:
        return False, str(exc), "", 0
    cfg = channel_config.platform_config(platform)
    refusal = grant_refusal(platform, rec, cfg)
    if refusal:
        return False, refusal, "", 0
    uid = str(rec["user_id"])
    until = int((time.time() if now is None else now) + minutes * 60)
    try:
        user_perms.set_tools_until(platform, uid, until)
    except (OSError, ValueError, user_perms.PermsUnreadable) as exc:
        return False, str(exc), "", 0
    ok, err = channel_config.add_to_set(platform, PERM_TOOLS, uid)
    if not ok:
        end_countdown(platform, uid)
        return False, err or "could not switch tool use on", "", 0
    changelog.record(platform, changelog.K_TIMED, uid,
                     until=time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(until)),
                     minutes=minutes)
    notes = []
    if not cfg.get("allow_tools"):
        notes.append(f"The {platform} master tools switch is off, so this has "
                     f"no effect until it is on (jarvis channels-set "
                     f"{platform} allow_tools true).")
    return True, "", " ".join(notes), until


def expire_due(platform=None, user_id=None, now=None):
    """Take everyone whose time-limited tool access has run out off the tool
    list and clear their deadline. Returns [{platform, user_id, name}] for the
    ones ended by THIS call. Never raises.

    Safe to call from anywhere that is allowed to write (the panel's people
    load, the gateway after it refused a message, the terminal) and NOT from
    a dry run. Idempotent. If removing someone from the list fails, the
    deadline is left in place: resolve_tool_access keeps refusing them, and
    the next call tries again. Each removal is tagged why="time limit ended"
    in the change log."""
    ended = []
    try:
        store = user_perms._load()
    except user_perms.PermsUnreadable:
        return ended          # unreadable limits are handled by the callers
    except Exception:  # noqa: BLE001
        return ended
    clock = time.time() if now is None else now
    for k in list(store):
        plat, _, uid = k.partition(":")
        if plat not in PLATFORMS or not uid:
            continue
        if platform and plat != platform:
            continue
        if user_id and uid != str(user_id):
            continue
        entry = user_perms.normalize(store.get(k))
        if not user_perms.is_expired(entry["tools_until"], clock):
            continue
        try:
            rec = people.get(plat, uid)
            idents = _idents(rec) if rec else [uid]
            with changelog.reason("time limit ended"):
                done = True
                for ident in idents:
                    ok, _err = channel_config.remove_from_set(
                        plat, PERM_TOOLS, ident)
                    done = done and ok
            if not done:
                continue
            user_perms.set_tools_until(plat, uid, 0)
            ended.append({"platform": plat, "user_id": uid,
                          "name": (people.effective_name(rec) if rec else "")
                          or (rec or {}).get("handle") or uid})
        except Exception:  # noqa: BLE001 -- the deadline stays; retried later
            continue
    return ended


def set_instruction(platform, user_id, text):
    """Set (or, with empty text, clear) the owner's line on how to talk to
    this person. Returns (ok, error, instruction).

    Refuses the owner's own account (its prompt block is fixed text, so an
    instruction there would be stored and never used) and anyone not on file.
    Style only: nothing here touches a permission, and the model is told so."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, ""
    cfg = channel_config.platform_config(platform)
    if permissions.is_owner(cfg, _idents(rec)):
        return False, ("that's the owner \u2014 Jarvis treats your own "
                       "account like you at the PC, so there is nothing to "
                       "tell it about how to talk to you"), ""
    try:
        out = people.set_instruction(platform, rec["user_id"], text)
    except ValueError as exc:
        return False, str(exc), rec.get("instruction") or ""
    return True, "", out.get("instruction") or ""


# --------------------------------------------------------------------------
# Name, add, remove, link  (the panel's Profile tab and "Add person")
# --------------------------------------------------------------------------

def add_person(platform, ident, name=""):
    """Add someone who hasn't messaged yet. Returns (ok, error, record,
    existing).

    Creates a row and nothing else: no list entry, no tool, no ownership. The
    owner then flips switches on the row like on anyone else. Adding someone
    who is already on file is not a failure: `existing` is True and the
    record returned is theirs, so the panel can simply select them."""
    rec, err = people.add_person(platform, ident, name)
    if rec is not None and err:
        return True, "", rec, True
    return (rec is not None), err, rec, False


def rename(platform, user_id, name):
    """Set (or, with an empty name, clear) what this person is called. The
    owner's word outranks a guest's own remember_sender — see
    people.set_name. Returns (ok, error, name_now)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, ""
    out = people.set_name(platform, rec["user_id"], name, manual=True)
    return True, "", out.get("name") or ""


def remove_person(platform, user_id):
    """Remove a hand-added person who never messaged, and take them out of
    every list that named them (otherwise sync_listed would just re-create
    the row). Returns (ok, error)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err
    cfg = channel_config.platform_config(platform)
    if permissions.is_owner(cfg, _idents(rec)):
        return False, "that's the owner — hand ownership to someone else first"
    if not rec.get("manual") or int(rec.get("messages") or 0) > 0:
        return False, ("only someone you added by hand who has never "
                       "messaged can be removed — block anyone else")
    for which in LISTS:
        ok, err = _remove_everywhere(platform, which, rec)
        if not ok:
            return False, err
    return people.remove_person(platform, rec["user_id"])


def edit_note(platform, user_id, note_id, text):
    """Rewrite one of what Jarvis remembers about this person. Returns
    (ok, error, notes). Only the owner reaches this (panel / CLI)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, []
    return people.edit_note(platform, rec["user_id"], note_id, text)


def delete_note(platform, user_id, note_id):
    """Delete one remembered note. Returns (ok, error, notes)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, []
    return people.delete_note(platform, rec["user_id"], note_id)


def forget_refusal(platform, rec):
    """Why this person cannot be forgotten, or "" when they can. One place, so
    the preview and the real thing cannot disagree."""
    cfg = channel_config.platform_config(platform)
    if permissions.is_owner(cfg, _idents(rec)):
        return "that's the owner — hand ownership to someone else first"
    for which, label in ((PERM_DM, "dm"), (PERM_REPLY, "reply"), (PERM_TOOLS, "tool"),
                         (PERM_IMAGES, "image")):
        if membership(cfg, which, rec)["via"] == "wildcard":
            return (f"covered by \"*\" (everyone) in the {label} list — "
                    f"forgetting them would also delete their limits and "
                    f"they'd still be let through. Take the \"*\" out first")
    try:
        user_perms._load()
    except user_perms.PermsUnreadable as exc:
        return (f"their limits can't be read ({exc}) — Jarvis won't delete "
                f"limits it can't see")
    return ""


def forget_person(platform, user_id, purge_history=False, dry_run=False):
    """Wipe one person. Returns (ok, error, report).

    `dry_run` reports what would go and changes nothing. `purge_history` adds
    step 3 of the module docstring; without it their logs and conversations
    stay on disk as an archive nobody is shown. The report is the same shape
    either way, so a preview cannot promise something the real run then does
    differently."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, {}
    refusal = forget_refusal(platform, rec)
    if refusal:
        return False, refusal, {}
    cfg = channel_config.platform_config(platform)
    uid = str(rec["user_id"])
    other = people.partner(rec)
    report = {
        "platform": platform,
        "user_id": uid,
        "name": people.effective_name(rec) or rec.get("handle") or uid,
        "dry_run": bool(dry_run),
        "lists": [w for w in LISTS
                  if membership(cfg, w, rec)["explicit"]],
        "limits": False,
        "notes": len([n for n in (rec.get("notes") or []) if isinstance(n, str)]),
        "linked": ({"platform": other.get("platform") or "",
                    "user_id": str(other.get("user_id") or ""),
                    "name": other.get("name") or ""} if other else None),
        # What is on disk for them. Deleted only when `history_purged`; the
        # panel shows these counts next to the tick-box so the choice is made
        # knowing how much it is.
        "history": transcript.history_summary(platform, uid),
        "history_purged": bool(purge_history),
        "usage_kept": True,
    }
    try:
        report["limits"] = user_perms.normalize(
            user_perms._load().get(user_perms.key(platform, uid))) != user_perms._defaults()
    except user_perms.PermsUnreadable as exc:
        return False, str(exc), {}
    if dry_run:
        return True, "", report

    # 1. access first -- everything after this only tidies up.
    for which in LISTS:
        ok, err = _remove_everywhere(platform, which, rec)
        if not ok:
            return False, err, report
    # 2. their limits.
    try:
        user_perms.forget(platform, uid)
    except (OSError, user_perms.PermsUnreadable) as exc:
        return False, str(exc), report
    # 3. history, only when asked.
    if purge_history:
        purged = transcript.purge_person(platform, uid)
        if purged["errors"]:
            return False, ("couldn't delete every log file — "
                           + "; ".join(purged["errors"][:3])), report
        from .. import conversations
        for conv_id in purged["conv_ids"]:
            conversations.delete_conversation(conv_id)
        transcript.forget_conv_ids(purged["conv_ids"])
        report["history_purged_threads"] = purged["threads"]
    # 4. handles.
    from . import directory
    directory.forget(platform, uid)
    # 5. the record, last.
    ok, err = people.delete_record(platform, uid)
    if not ok:
        return False, err, report
    changelog.record(platform, changelog.K_FORGOT, uid,
                     history=bool(purge_history))
    return True, "", report


def link_accounts(platform, user_id, other_platform, ident):
    """Say this account and one on the other platform are the same person.
    Identity only: no permission is shared or granted. Returns
    (ok, error, note)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, ""
    ok, err, other = people.link(platform, rec["user_id"], other_platform, ident)
    if not ok:
        return False, err, ""
    note = ""
    if other and other.get("is_owner"):
        note = (f"that's the {other_platform} owner. Linking doesn't make "
                f"this account the owner — switch Owner on for it too if "
                f"it's you")
    return True, "", note


def unlink_accounts(platform, user_id):
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err
    return people.unlink(platform, rec["user_id"])


# --------------------------------------------------------------------------
# Read-only views: Conversation (P1), Usage (P2), Test as this person (P3)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Edit a handle by hand  (L.36-P10)
# --------------------------------------------------------------------------

def handle_edit_refusal(rec, is_owner=False):
    """Why this person's handle cannot be edited, or "" when it can.

    Only someone the owner added by hand who has never written: the moment a
    person messages, people.touch() overwrites the stored handle with the one
    the platform reports, so an edit would silently revert -- and for someone
    who has written, the platform's handle is the truth. The owner is refused
    because the config's `owner` entry may name the old handle."""
    if is_owner:
        return "that's the owner — hand ownership to someone else first"
    if not rec.get("manual") or int(rec.get("messages") or 0) > 0:
        return ("only a person you added by hand who hasn't written yet can "
                "have their handle edited — once they write, the platform's "
                "own handle is used and would replace an edit")
    return ""


def set_handle(platform, user_id, handle):
    """Correct the handle of a hand-added person who has never written.
    Returns (ok, error, note, record).

    The handle is more than a label: allow-lists can name someone by it, and a
    person known ONLY by handle (a placeholder) is keyed by it. So an edit
    carries their access with them -- anywhere the old handle was listed the
    new one is listed instead -- and never leaves the old string behind, where
    whoever really owns it would inherit the owner's grant.

    ORDER (a failure part-way must never leave someone with more access than
    they had, nor their limits behind):

        1. limits copied to the new key          (placeholder only)
        2. new handle added to the lists the old one was in
        3. the people.json record moved / relabelled
        4. old handle taken out of those lists
        5. old limits entry removed              (placeholder only)

    A failure at 2-3 leaves the old entries in place and reports; running it
    again finishes the job. It grants nothing the old handle did not."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return False, err, "", None
    cfg = channel_config.platform_config(platform)
    refusal = handle_edit_refusal(rec, permissions.is_owner(cfg, _idents(rec)))
    if refusal:
        return False, refusal, "", None
    new = people.clean_handle(handle)
    if not new:
        return False, ("a handle has no spaces, starts with a letter or digit "
                       "and isn't only digits (that would read as an id)"), "", None
    old = rec.get("handle") or ""
    if new == old:
        return True, "", "that's already their handle — nothing changed", rec
    if people.ident_taken(platform, new, rec["user_id"]):
        return False, f"{platform} already has someone with @{new}", "", None

    uid = str(rec["user_id"])
    placeholder = bool(rec.get("placeholder"))
    new_uid = new if placeholder else uid
    on_lists = [w for w in LISTS
                if old and old in channel_config.normalize_entries(cfg.get(w))]
    if placeholder:
        on_lists = [w for w in LISTS
                    if uid.lower() in channel_config.normalize_entries(cfg.get(w))]

    try:
        if placeholder:
            user_perms.copy_entry(platform, uid, new_uid)            # 1
    except (OSError, user_perms.PermsUnreadable) as exc:
        return False, f"couldn't move their limits — {exc}", "", None
    for which in on_lists:                                           # 2
        ok, err = channel_config.add_to_set(platform, which, new)
        if not ok:
            return False, err, "", None
    out, err = people.change_handle(platform, uid, new)              # 3
    if out is None:
        return False, err, "", None
    # Only the OLD HANDLE comes out. A person with a real id keeps their id
    # entry; only a placeholder's id *was* the handle.
    stale = ({old.lower()} if old else set()) | ({uid.lower()} if placeholder else set())
    for which in on_lists:                                           # 4
        for ident in stale:
            ok, err = channel_config.remove_from_set(platform, which, ident)
            if not ok:
                return False, err, "", out
    if placeholder:
        try:
            user_perms.drop_entry(platform, uid)                     # 5
        except (OSError, user_perms.PermsUnreadable):
            pass  # a leftover limits entry under an id nobody has is harmless
    changelog.record(platform, changelog.K_HANDLE, new_uid,
                     old=(old or (uid if placeholder else "")) or None)
    note = ("their access moved to the new handle" if on_lists
            else "nothing is switched on for them, so no list changed")
    return True, "", note, out


# --------------------------------------------------------------------------
# Change log view  (L.36-P15)
# --------------------------------------------------------------------------

def history_view(platform, user_id, limit=100):
    """Every recorded change to this person's access, oldest first, plus the
    platform-wide ones that reached them too. Read-only. Registered people
    only. Each line already carries its `text`; the panel prints it as text."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return {"ok": False, "error": err}
    lines, total = changelog.for_person(platform, _idents(rec), limit)
    return {"ok": True, "platform": platform, "user_id": str(rec["user_id"]),
            "entries": lines, "total": total,
            "truncated": total > len(lines),
            "tracking_since": changelog.started()}


def conversation_view(platform, user_id, limit=100):
    """What this person sent and how Jarvis answered, newest `limit` messages
    oldest-first. Registered people only. See transcript.person_records for
    how a reply is attributed (and why some older group replies are not)."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return {"ok": False, "error": err}
    out = transcript.read_person(platform, rec["user_id"], limit)
    out.update({"ok": True, "platform": platform,
                "user_id": str(rec["user_id"])})
    return out


def usage_view(platform, user_id, days=30):
    """Usage for one registered person -- see channels/usage.py."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return {"ok": False, "error": err}
    return usage.summary(platform, rec["user_id"], days)


# The gate's stages in the order permissions.decide() runs them, per kind of
# message. tests/test_channel_insights.py forces a denial at every one of these
# against the real decide(), so a stage added or renamed there fails here
# instead of silently showing a wrong walkthrough.
GATE_STAGES = {
    permissions.CTX_DM: ("enabled", "self", "reachable", "dm_allowed",
                         "reply", "cooldown"),
    permissions.CTX_GROUP: ("enabled", "self", "reachable", "where",
                            "reply", "cooldown"),
}
STAGE_LABELS = {
    "enabled": "Platform is switched on",
    "self": "Not Jarvis's own message",
    "reachable": "Addressed to Jarvis",
    "dm_allowed": "On the DM list",
    "where": "Server and channel allowed",
    "reply": "On the reply list",
    "cooldown": "Not sending too fast",
}


def _stage_rows(context, decision):
    """Every stage with pass / fail / skipped, ending at the one that decided.
    A stage after the denial was never reached, so it is `skipped`, not
    `pass` -- the gate stops at the first no."""
    rows, stopped = [], False
    for stage in GATE_STAGES[context]:
        if stopped:
            state, detail = "skipped", ""
        elif not decision.allowed and decision.stage == stage:
            state, detail, stopped = "fail", decision.reason, True
        else:
            state, detail = "pass", ""
        rows.append({"id": stage, "label": STAGE_LABELS[stage],
                     "state": state, "detail": detail})
    return rows


def simulate(platform, user_id, context=permissions.CTX_DM, mentioned=True):
    """What the gate would do with a message from this person -- a DRY RUN.

    Builds an in-memory message and asks permissions.decide() and
    user_perms.resolve_tool_access(), the very functions a real message goes
    through. It never calls a model, never sends, and writes nothing: no
    transcript, no usage line, no people.json record, no cooldown mark, no
    conversation. `saved` and `model_called` are in the result so a caller can
    say so without having to know.

    The message TEXT is deliberately not an input: no stage of the gate reads
    it (who and where decide everything), so a text box would only suggest
    that wording matters. Returns a dict; {"ok": False, ...} for an
    unregistered person or a bad argument."""
    rec, err = _registered(platform, user_id)
    if rec is None:
        return {"ok": False, "error": err}
    if context not in GATE_STAGES:
        return {"ok": False, "error": "context must be dm or group"}
    cfg = channel_config.platform_config(platform)
    msg = permissions.IncomingMessage(
        platform, context, user_id=rec["user_id"],
        user_handle=rec.get("handle") or "", text="",
        mentioned=bool(mentioned) if context == permissions.CTX_GROUP else False)
    # No guild / channel / thread id on purpose: decide() skips its location
    # filters for a message that names no place, whereas an invented id would
    # be refused by any allowed_channels list and show a denial that no real
    # message gets. The note below says the filters are not applied.
    # last_seen_at stays None: cooldown is gateway memory, which this process
    # does not have, so that stage can only ever pass here (noted below).
    decision = permissions.decide(cfg, msg, last_seen_at=None)
    answered = bool(decision.allowed)

    may_use, scope, label, problem = user_perms.resolve_tool_access(
        platform, rec["user_id"], decision.may_use_tools)
    if not answered:
        tools = {"state": "none", "why": "They would not be answered at all.",
                 "allowed": None, "count": None, "plumbing": []}
    elif may_use and scope is None:
        tools = {"state": "all", "allowed": None, "count": None, "plumbing": [],
                 "why": "Every tool the platform allows (owner-only tools "
                        "such as send_dm stay owner-only)."}
    elif may_use:
        ticked = sorted(set(scope) - set(user_perms.PLUMBING_TOOLS))
        tools = {"state": "custom", "allowed": ticked, "count": len(ticked),
                 "plumbing": sorted(user_perms.PLUMBING_TOOLS),
                 "why": "Only the tools ticked for them, plus the helpers "
                        "Jarvis needs to find them."}
    else:
        if problem:
            why = ("Their tool limits could not be read, so this message "
                   "runs with no tools until that is fixed.")
        elif label == user_perms.LABEL_EXPIRED:
            why = ("Their time-limited tool access has run out. Switch Tool "
                   "use on again, or give them another time limit.")
        elif scope is not None:
            why = "Their custom tool list is empty."
        elif not cfg.get("allow_tools"):
            why = (f"The {platform} master tools switch is off "
                   f"(jarvis channels-set {platform} allow_tools true).")
        else:
            why = "They are not on the tool list."
        tools = {"state": "none", "why": why, "allowed": None, "count": None,
                 "plumbing": []}

    # L.36-P8: would their pictures be read? Reads the same function the
    # feature will call (permissions.may_send_images), after the gate said yes.
    if not answered:
        images = {"state": "none", "why": "They would not be answered at all."}
    elif permissions.is_owner(cfg, _idents(rec)):
        images = {"state": "owner", "why": "The owner's pictures are always read."}
    elif permissions.may_send_images(cfg, msg):
        images = {"state": "on", "why": "You switched pictures on for them."}
    else:
        images = {"state": "off", "why": "Pictures are off for them (default)."}
    if answered:
        images["why"] += " Jarvis doesn't read pictures from chat yet, so nothing changes today."

    notes = []
    if int(cfg.get("cooldown_seconds") or 0) > 0:
        notes.append(f"Cooldown ({int(cfg['cooldown_seconds'])}s between "
                     f"messages) is not simulated: it depends on when they "
                     f"last wrote, which only the running bot knows.")
    if context == permissions.CTX_GROUP and (cfg.get("allowed_guilds")
                                              or cfg.get("allowed_channels")):
        notes.append("This platform limits which servers or channels Jarvis "
                     "answers in. That filter is not applied here, because "
                     "the test names no particular server or channel.")
    if context == permissions.CTX_GROUP and cfg.get("scopes"):
        notes.append("Per-server and per-channel overrides exist in the config "
                     "and are not applied here, because this test has no "
                     "particular server or channel.")
    if (rec.get("follow") or "") == people.FOLLOW_BLOCKED and answered:
        notes.append("They are marked blocked but still get through: a "
                     "\"*\" (everyone) entry covers them.")
    try:
        until = user_perms.tools_until(platform, rec["user_id"])
    except user_perms.PermsUnreadable:
        until = 0
    if answered and may_use and until and not user_perms.is_expired(until):
        notes.append("Tool use is time-limited for them: it ends at "
                     + time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
                     + ".")
    if answered and not user_perms.dm_allowed(platform, rec["user_id"]):
        notes.append("Jarvis would not DM them on your behalf (send_dm is off "
                     "for them).")

    return {
        "ok": True,
        "platform": platform,
        "user_id": str(rec["user_id"]),
        "context": context,
        "mentioned": bool(mentioned) if context == permissions.CTX_GROUP else None,
        "answered": answered,
        "stage": decision.stage,
        "reason": decision.reason,
        "stages": _stage_rows(context, decision),
        "tools": tools,
        "images": images,
        "notes": notes,
        "saved": False,
        "model_called": False,
    }
