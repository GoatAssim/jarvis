"""Send one DM from the Channels panel (L.36-P13).

This is the OWNER typing a message to ONE known person and clicking Send. It
is not a second sender: everything goes through actions/send_dm.tool_send_dm,
the very function the `send_dm` tool runs, so the limits are shared, not
copied --

    * known contacts only (a registered person, never a bare id)
    * not the owner's own account, not someone blocked, not someone whose
      "Jarvis may DM them" switch is off
    * 1500 characters
    * 5 per recipient / 20 overall per hour, from the same on-disk counter, so
      a message sent here and one sent by the tool count against each other
    * logged to the person's transcript as an `owner_dm` line
    * Discord/Instagram delivery through channels/outbound.dm_person (and so
      Instagram's 24-hour window and Discord's shared-server rule)

THE DECISION (left to the owner in the plan; the conservative reading)
---------------------------------------------------------------------
The `send_dm` tool is confirm-gated because a MODEL asks for it. Here a human
asks, so the confirmation is the click, but it is still two steps: `preview()`
shows exactly who gets exactly what and how much of the hourly allowance is
left, and `send(..., confirm=True)` is the only thing that delivers. A person
known only by handle (a placeholder) has no id to address, so they cannot be
messaged until they write once.

It refuses to run if it was reached from inside a chat (JARVIS_CHANNEL_SENDER
set) or an unattended run: a guest's message must never be able to cause this.
No model tool reaches this module (AGENTS.md); only the panel and
`jarvis channels-send` do, and the CLI only delivers when told `--yes`.
"""

import os
import time

from . import config as channel_config
from . import permissions, user_admin


def _tool():
    # Lazy: actions/ is imported during tool discovery and imports back into
    # channels/; importing it at module load would risk a cycle.
    from ..actions import send_dm
    return send_dm


def _refuse(message, **extra):
    return dict({"ok": False, "error": message, "sent": False}, **extra)


def _context_refusal(tool):
    if os.environ.get(tool.SENDER_ENV, "").strip():
        return ("this can only be done from the owner's own console, never "
                "from inside a chat")
    if tool._is_unattended():
        return "this does not run from a scheduled or unattended job"
    return ""


def _rate(tool, platform, uid, now):
    sends = tool._load_sends(now)
    return {
        "recipient_used": len(sends.get(f"{platform}:{uid}", [])),
        "recipient_max": tool.MAX_PER_RECIPIENT,
        "overall_used": len(sends.get("_all", [])),
        "overall_max": tool.MAX_OVERALL,
        "window_minutes": tool.RATE_WINDOW_SECONDS // 60,
    }


def _resolve(platform, user_id, text, tool):
    """(rec, who, error). Dry-runs the tool's own resolver for exactly this
    person and refuses if it comes back with anyone else."""
    rec, err = user_admin._registered(platform, user_id)
    if rec is None:
        return None, None, err
    if rec.get("placeholder"):
        return None, None, ("they're known only by handle, so there is no id to "
                            "address yet \u2014 they can be messaged after their "
                            "first message")
    if permissions.is_owner(channel_config.platform_config(platform),
                            user_admin._idents(rec)):
        return None, None, ("that's your own account \u2014 Jarvis reaches you "
                            "with notifications, not DMs")
    uid = str(rec["user_id"])
    clean = str(text or "").strip()
    if not clean:
        return None, None, "type a message first"
    if len(clean) > tool.MAX_MESSAGE_CHARS:
        return None, None, (f"the message is {len(clean)} characters; the limit "
                            f"is {tool.MAX_MESSAGE_CHARS}")
    res = tool.tool_send_dm({"person": f"{platform}:{uid}", "message": clean,
                             "dry_run": True})
    if not res.get("ok"):
        return None, None, str(res.get("error") or "can't message them")
    who = res.get("would_send_to") or {}
    if (str(who.get("platform")) != platform or str(who.get("user_id")) != uid
            or res.get("known_contact") is False):
        return None, None, "that didn't resolve to exactly this person \u2014 nothing sent"
    return rec, who, ""


def preview(platform, user_id, text, now=None):
    """Who would get what, and the allowance left. Writes nothing, sends
    nothing. Returns a dict with ok / error."""
    tool = _tool()
    why = _context_refusal(tool)
    if why:
        return _refuse(why)
    rec, who, err = _resolve(platform, user_id, text, tool)
    if rec is None:
        return _refuse(err)
    uid = str(rec["user_id"])
    now = time.time() if now is None else now
    limited = tool._rate_check(platform, uid, now)
    return {
        "ok": True, "dry_run": True, "sent": False,
        "to": who, "message": str(text).strip(), "chars": len(str(text).strip()),
        "limit": tool.MAX_MESSAGE_CHARS, "rate": _rate(tool, platform, uid, now),
        "limited": limited or "",
        "note": "preview only \u2014 nothing was sent",
    }


def send(platform, user_id, text, confirm=False, now=None):
    """Deliver it. Without `confirm` this is `preview`. Returns a dict: ok,
    sent, error, to, detail."""
    if not confirm:
        out = preview(platform, user_id, text, now=now)
        if out.get("ok"):
            out["needs_confirm"] = True
        return out
    tool = _tool()
    why = _context_refusal(tool)
    if why:
        return _refuse(why)
    rec, who, err = _resolve(platform, user_id, text, tool)
    if rec is None:
        return _refuse(err)
    # tool_send_dm re-resolves, rate-limits, delivers, logs and records the
    # send; its refusals (owner, blocked, DMs off, rate limit) come back as
    # ok False with the reason, and are passed on as they are.
    res = tool.tool_send_dm({"person": f"{platform}:{rec['user_id']}",
                             "message": str(text).strip()})
    if not res.get("ok"):
        return _refuse(str(res.get("error") or "could not send"), to=who,
                       hint=str(res.get("hint") or ""))
    return {"ok": True, "sent": True, "error": "", "to": who,
            "detail": str(res.get("detail") or ""),
            "chars": int(res.get("chars") or 0)}
