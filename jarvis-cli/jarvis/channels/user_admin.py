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

WHAT A SWITCH CANNOT DO
-----------------------
There is no deny-list in the gate. A person covered by a `"*"` entry cannot be
switched off individually — set_flag() refuses and says why instead of
removing an id that was never the reason they got through.
"""

from . import PLATFORMS, PERM_DM, PERM_REPLY, PERM_TOOLS
from . import config as channel_config
from . import people, permissions, transcript, usage, user_perms
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
        "linked": _linked_view(rec),
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
    }


def sync_listed(platform, cfg):
    """Make sure everyone an allow-list (or `owner`) names has a record.
    Never raises: a store that can't be written leaves the panel showing what
    it already had rather than failing to open. Returns how many were made."""
    entries = []
    for which in (PERM_DM, PERM_REPLY, PERM_TOOLS):
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
    for which in (PERM_DM, PERM_REPLY, PERM_TOOLS):
        ok, err = _remove_everywhere(platform, which, rec)
        if not ok:
            return False, err
    return people.remove_person(platform, rec["user_id"])


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
        elif scope is not None:
            why = "Their custom tool list is empty."
        elif not cfg.get("allow_tools"):
            why = (f"The {platform} master tools switch is off "
                   f"(jarvis channels-set {platform} allow_tools true).")
        else:
            why = "They are not on the tool list."
        tools = {"state": "none", "why": why, "allowed": None, "count": None,
                 "plumbing": []}

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
        "notes": notes,
        "saved": False,
        "model_called": False,
    }
