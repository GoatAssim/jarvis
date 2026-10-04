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

REGISTERED ONLY
---------------
Every function refuses a person who has no people.json record (nobody who was
denied at the gate ever gets one — see permissions.py). That is the rule the
panel is built on, so it is enforced here and not left to the UI.

WHAT A SWITCH CANNOT DO
-----------------------
There is no deny-list in the gate. A person covered by a `"*"` entry cannot be
switched off individually — set_flag() refuses and says why instead of
removing an id that was never the reason they got through.
"""

from . import PLATFORMS, PERM_DM, PERM_REPLY, PERM_TOOLS
from . import config as channel_config
from . import people, permissions, user_perms
from .config import WILDCARD

FLAGS = ("dm", "reply", "tool", "owner", "send_dm", "blocked")
_SET_FOR = {"dm": PERM_DM, "reply": PERM_REPLY, "tool": PERM_TOOLS}

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
        "avatar": rec.get("avatar") or "",
        "first_seen": rec.get("first_seen"),
        "last_seen": rec.get("last_seen"),
        "messages": int(rec.get("messages") or 0),
        "notes": [n for n in (rec.get("notes") or []) if isinstance(n, str)],
        "follow": follow,
        "owner": bool(owner),
        "blocked": follow == people.FOLLOW_BLOCKED,
        "dm": membership(cfg, PERM_DM, rec),
        "reply": membership(cfg, PERM_REPLY, rec),
        "tool": membership(cfg, PERM_TOOLS, rec),
        "send_dm": bool(perms["can_dm"]),
        "tools": perms["tools"],
    }
    # A one-glance summary of what this person can do right now. It restates
    # the gate's own rule (permissions.decide: allow_tools AND tool_allowlist)
    # so the badge on a card can never promise more than the gate gives.
    tools_live = bool(cfg.get("allow_tools")) and view["tool"]["on"]
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
    cfg_all = channel_config.load_config()
    platforms = [platform] if platform in PLATFORMS else list(PLATFORMS)
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
    }


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
        if value:
            if blocked:
                return False, "they're blocked — unblock them first", ""
            ok, err = channel_config.add_to_set(platform, which, uid)
            return ok, err, ""
        member = membership(cfg, which, rec)
        if member["via"] == "wildcard":
            return False, (f"covered by \"*\" (everyone) in the {flag} list — "
                           f"a switch for one person can't undo that"), ""
        ok, err = _remove_everywhere(platform, which, rec)
        return ok, err, ""

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
        for which in (PERM_DM, PERM_REPLY, PERM_TOOLS):
            if membership(cfg, which, rec)["via"] == "wildcard":
                wildcard_note = ("a \"*\" (everyone) entry still covers them in "
                                 "at least one list — blocking can't override it")
                break
        else:
            wildcard_note = ""
        for which in (PERM_DM, PERM_REPLY, PERM_TOOLS):
            ok, err = _remove_everywhere(platform, which, rec)
            if not ok:
                return False, err, ""
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
