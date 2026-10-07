"""Recently turned-away senders, for the Channels panel (L.36-P14).

The question this answers: "someone says they messaged Jarvis and got no
answer -- who was it?". The gate already writes every refusal to the transcript
(`transcript.log_inbound`, denied lines included), so this only READS it.

WHAT IT SHOWS, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------
A stranger's handle, id, how many times, when last, which kind of chat, and the
gate's own fixed reason ("sender is not in reply_allowlist"). NEVER the text of
what they wrote: that is a stranger's words and the panel has no need of them to
decide whether to add someone. (The Conversation tab already shows messages for
people the owner has chosen to look at.)

Only refusals about WHO the person is count -- the `dm_allowed` and `reply`
stages. "Not mentioned" is group chatter nobody addressed to Jarvis, `cooldown`
is an allowed person pacing, `where` is a server or channel filter and `self` is
the bot's own echo; none of those is somebody being turned away.

It reverses nothing and grants nothing. L.36 read "registered" as "has a record
in people.json"; since the 2026-10-05l fix an addressed sender is registered
even when refused, so most of these people are already in the People list --
they are marked `registered` and the panel offers Open instead of Add. The rest
(people turned away before that fix, or whose records were forgotten) get Add,
which is the ordinary add-a-person path: a row and NOTHING else, no list entry,
no tool, no ownership. Someone the lists NOW let through is left out.

Read-only, bounded (newest threads, last lines of each) and never writes,
rotates or creates a file.
"""

import time
from datetime import datetime, timedelta

from . import PLATFORMS, PERM_DM, PERM_REPLY
from . import config as channel_config
from . import people, transcript, user_admin

# Gate stages that mean "this PERSON is not allowed", in the gate's own words.
WHO_STAGES = {"dm_allowed": PERM_DM, "reply": PERM_REPLY}

MAX_THREADS = 60            # newest threads read per platform
LINES_PER_THREAD = 400      # newest lines read per thread
DEFAULT_DAYS = 14
MAX_DAYS = 90
MAX_LISTED = 25


def _when(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _scan(platform, since):
    """{user_id: aggregate} for one platform, from the newest threads."""
    found = {}
    for item in transcript.list_threads(platform=platform)[:MAX_THREADS]:
        for rec in transcript.read_thread(platform, item["thread_id"],
                                          limit=LINES_PER_THREAD):
            if not isinstance(rec, dict) or rec.get("dir") != "in":
                continue
            if rec.get("allowed") is not False or rec.get("stage") not in WHO_STAGES:
                continue
            uid = str(rec.get("user_id") or "").strip()
            when = _when(rec.get("at"))
            if not uid or when is None or when < since:
                continue
            row = found.setdefault(uid, {
                "user_id": uid, "handle": "", "count": 0, "last_at": None,
                "contexts": set(), "stage": "", "reason": "", "_when": when})
            row["count"] += 1
            row["contexts"].add("dm" if rec.get("context") == "dm" else "group")
            if when >= row["_when"]:
                row["_when"], row["last_at"] = when, str(rec.get("at"))
                row["stage"] = str(rec.get("stage") or "")
                row["reason"] = str(rec.get("reason") or "")
                if str(rec.get("user_handle") or "").strip():
                    row["handle"] = str(rec["user_handle"]).strip().lstrip("@")
    return found


def denied_view(platform=None, days=DEFAULT_DAYS, now=None):
    """The panel's list: {ok, days, senders: [...], hidden}. `hidden` counts
    people left out because the lists now let them through."""
    try:
        days = max(1, min(MAX_DAYS, int(days)))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    stamp = datetime.fromtimestamp(time.time() if now is None else now)
    since = stamp - timedelta(days=days)
    platforms = [platform] if platform in PLATFORMS else list(PLATFORMS)
    cfg_all = channel_config.load_config()
    out, hidden = [], 0
    for p in platforms:
        cfg = cfg_all.get(p) or {}
        for uid, row in _scan(p, since).items():
            rec = people.get(p, uid)
            probe = rec or {"user_id": uid, "handle": row["handle"]}
            # Let in since? Then the refusal is history, not a to-do.
            which = WHO_STAGES.get(row["stage"])
            if which and user_admin.membership(cfg, which, probe)["on"]:
                hidden += 1
                continue
            out.append({
                "platform": p,
                "user_id": uid,
                "handle": row["handle"] or ((rec or {}).get("handle") or ""),
                "name": people.effective_name(rec) if rec else "",
                "count": row["count"],
                "last_at": row["last_at"],
                "contexts": sorted(row["contexts"]),
                "stage": row["stage"],
                "reason": row["reason"],
                "registered": bool(rec),
                "blocked": bool(rec and (rec.get("follow") or "") == people.FOLLOW_BLOCKED),
                "_sort": row["_when"],
            })
    out.sort(key=lambda r: r["_sort"], reverse=True)
    for r in out:
        r.pop("_sort", None)
    return {"ok": True, "days": days, "senders": out[:MAX_LISTED],
            "total": len(out), "hidden": hidden}
