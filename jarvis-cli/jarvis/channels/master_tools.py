"""The platform-wide tool switch (`allow_tools`) from the panel (L.36-P9).

`allow_tools` is ANDed with each person's tool_allowlist entry: with it off,
nobody can make Jarvis touch the PC from this platform no matter who is on the
list. Until now it was terminal-only (`jarvis channels-set <platform>
allow_tools true`), so a person could read "Waiting" in the panel with no way
to fix it from there.

THE DECISION (the plan left it to the owner; this is the conservative reading)
-----------------------------------------------------------------------------
Turning it ON widens access from a browser, so it is the one switch in the
panel that can only be flipped on in TWO steps, and that two-step lives here,
not in the page:

    1. `view()` / a call without `confirm` shows who would be able to run tools
       right after (named people who are also answered, are not blocked and
       whose time limit has not run out) and writes nothing.
    2. `set_master(..., confirm=True)` applies it.

Turning it OFF takes access away, so it applies at once.

It is REFUSED outright while the tool list contains `"*"` (everyone): with the
master switch on, that would let anyone who can reach the bot run tools on this
PC, and a click is not the place to decide that. Take the `"*"` out first, or
use the terminal deliberately. It never edits who is on a list, a person's
limits, or any token -- it writes the one `allow_tools` value through
config.set_value, which the change log already watches, so the flip appears in
History as "Tool use for everyone the lists allow switched on".

No model tool reaches this module (AGENTS.md).
"""

from . import PLATFORMS, PERM_REPLY, PERM_TOOLS
from . import config as channel_config
from . import people, permissions, user_admin, user_perms
from .config import WILDCARD

MAX_LISTED = 50


def _rows(platform, cfg):
    """(rows, perms_error): everyone the tool list lets through, and whether
    each would really be able to run a tool once the master switch is on."""
    perms_error = ""
    try:
        store = user_perms._load()
    except user_perms.PermsUnreadable as exc:
        store, perms_error = {}, str(exc)
    rows = []
    for rec in people.all_people(platform):
        if not isinstance(rec, dict) or not rec.get("user_id"):
            continue
        if not user_admin.membership(cfg, PERM_TOOLS, rec)["on"]:
            continue
        perms = user_perms.normalize(store.get(user_perms.key(platform, rec["user_id"])))
        answered = user_admin.membership(cfg, PERM_REPLY, rec)["on"]
        blocked = (rec.get("follow") or "") == people.FOLLOW_BLOCKED
        expired = user_perms.is_expired(perms.get("tools_until"))
        custom = perms["tools"]["mode"] == user_perms.TOOLS_CUSTOM
        rows.append({
            "user_id": str(rec["user_id"]),
            "name": people.effective_name(rec) or rec.get("handle") or str(rec["user_id"]),
            "handle": rec.get("handle") or "",
            "owner": bool(permissions.is_owner(cfg, user_admin._idents(rec))),
            "scope": "custom" if custom else "all",
            "tool_count": len(perms["tools"]["allow"]) if custom else None,
            "answered": bool(answered),
            "blocked": blocked,
            "expired": bool(expired),
            # The honest answer to "who gets tools when I switch this on".
            "reachable": bool(answered and not blocked and not expired),
        })
    # Unreadable limits fail closed in the gate (no tools), so nobody is
    # counted as reachable on the strength of limits we cannot see.
    if perms_error:
        for row in rows:
            row["reachable"] = False
    return rows, perms_error


def view(platform):
    """What switching the master switch on would do. Writes nothing."""
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"unknown platform '{platform}'"}
    cfg = channel_config.platform_config(platform)
    entries = channel_config.normalize_entries(cfg.get(PERM_TOOLS))
    rows, perms_error = _rows(platform, cfg)
    reachable = [r for r in rows if r["reachable"]]
    return {
        "ok": True,
        "platform": platform,
        "allow_tools": bool(cfg.get("allow_tools")),
        "wildcard": WILDCARD in entries,
        "people": rows[:MAX_LISTED],
        "people_total": len(rows),
        "reachable_count": len(reachable),
        "reachable_others": len([r for r in reachable if not r["owner"]]),
        "perms_error": perms_error,
    }


def set_master(platform, value, confirm=False):
    """Flip the platform-wide switch. Returns a dict:

        ok            the call did what was asked, or only previewed
        applied       the value was written (False for a preview / no-op)
        dry_run       a preview of turning it ON (needs `confirm`)
        needs_confirm the same thing, for a caller that wants the flag
        error         why it was refused
        view          view(platform), as it was BEFORE the write
    """
    report = view(platform)
    if not report.get("ok"):
        return report
    value = bool(value)
    out = {"ok": True, "error": "", "applied": False, "dry_run": False,
           "needs_confirm": False, "platform": platform, "view": report,
           "allow_tools": report["allow_tools"], "note": ""}
    if value == report["allow_tools"]:
        out["note"] = ("already " + ("on" if value else "off")
                       + " \u2014 nothing changed")
        return out

    if value and report["wildcard"]:
        out.update(ok=False, error=(
            'the tool list contains "*" (everyone), so turning this on would '
            "let anyone who can reach this bot run tools on this PC. The panel "
            "won't do that: take the \"*\" out of the tool list first"))
        return out
    if value and not confirm:
        out.update(dry_run=True, needs_confirm=True,
                   note="preview only \u2014 nothing was changed")
        return out

    ok, err = channel_config.set_value(platform, "allow_tools", value)
    if not ok:
        out.update(ok=False, error=err or "could not write the config")
        return out
    out.update(applied=True, allow_tools=value)
    if value:
        out["note"] = ("tools can now run from this platform for the people "
                       "who are on the tool list")
        if report["perms_error"]:
            out["note"] += ("; their limits could not be read, so every "
                            "message runs without tools until that is fixed")
    else:
        out["note"] = ("nobody can run tools from this platform now; the "
                       "lists and limits are unchanged")
    return out
