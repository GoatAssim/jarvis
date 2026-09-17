"""Notification digest — batch the low-priority noise into one summary DM.

THE PROBLEM
-----------
notify_owner fires per event. That's exactly right for "the 40-minute build
finished" and exactly wrong for "the nightly backup ran, again, like it does
every night". Once a few recurring jobs are set up, the useful notification
is buried in a column of routine ones, and the honest response is to mute the
bot — at which point the one that mattered is lost too.

The usual fix is a per-job mute, but that's the wrong granularity: you don't
want to *never* hear about the backup, you want to hear about it once a day,
together with everything else routine.

HOW IT WORKS
------------
Every notification now carries a priority:

    high    always delivered immediately, never batched (failures, anything
            the user explicitly asked to be told about)
    normal  delivered immediately (the previous behavior for everything)
    low     appended to the digest queue instead of being delivered

A scheduled job (created by `jarvis digest-on`) flushes the queue on the
chosen cadence and sends ONE message summarizing it, grouped by source with
repeats collapsed ("nightly backup — 7 times, all ok"). The durable inbox
still records every individual item exactly as before, so nothing is lost
and the web console still shows them one by one; the digest only changes
what gets *pushed* to a phone.

WHY THE QUEUE IS SEPARATE FROM THE INBOX
----------------------------------------
The inbox (notifier.py) is a queue with acknowledging consumers — the web UI
and the CLI both drain it, and once drained an item is gone from `pending`.
The digest needs items to survive that drain, because the whole point is
that you were *not* at the machine. Reusing the inbox would mean a digest
that silently omits everything you happened to be online for, which is the
opposite of what a digest is. So this is its own small file with its own
lifecycle: appended on notify, cleared only when a digest is actually sent.

FAILING SAFE
------------
If the digest can't be delivered (no owner configured, Discord down, the
Instagram 24-hour window closed), the queue is NOT cleared — the items roll
into the next digest rather than evaporating. A notification system that can
lose messages while appearing to work is worse than one that doesn't exist,
which is the same reasoning behind the durable inbox itself.
"""

import json
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from . import atomic_io

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "digest_config.json"
QUEUE_FILE = JARVIS_DIR / "digest_queue.json"
ENCODING = "utf-8"

PRIORITIES = ("low", "normal", "high")
DEFAULT_PRIORITY = "normal"

SCHEDULES = ("daily", "weekly", "hourly")

# Title used for the scheduled job that flushes this, so `digest-on` can
# find and replace its own previous job instead of stacking up duplicates
# every time someone changes the cadence.
JOB_TITLE = "Jarvis notification digest"
JOB_MARKER = "__jarvis_digest__"

MAX_QUEUE = 500
MAX_DIGEST_CHARS = 1800   # one Discord message, with headroom for chunking

DEFAULT_CONFIG = {
    "enabled": False,
    "schedule": "daily",
    # When the digest goes out. Parsed by timespec, so "9am", "18:30" and
    # "evening" all work.
    "at": "9am",
    # Kinds whose notifications default to low priority (i.e. batched)
    # unless the caller says otherwise. "task" is the routine one: a
    # scheduled job completing is exactly the thing nobody needs a phone
    # buzz for.
    "batch_kinds": ["task"],
    # Send the digest even when nothing happened. Off by default — a daily
    # "nothing to report" DM is itself notification noise.
    "send_when_empty": False,
    # Where the digest goes. Empty = wherever notify_owner would send it.
    "platform": "",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return cfg
    if isinstance(data, dict):
        for key in cfg:
            if key in data:
                cfg[key] = data[key]
    cfg["schedule"] = cfg.get("schedule") if cfg.get("schedule") in SCHEDULES else "daily"
    cfg["batch_kinds"] = [str(k) for k in (cfg.get("batch_kinds") or []) if str(k).strip()]
    return cfg


def save_config(cfg):
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg or {})
    return atomic_io.write_json(CONFIG_FILE, merged)


def ensure_config():
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
    return CONFIG_FILE


def normalize_priority(value, kind=None, cfg=None):
    """Resolve a priority, applying the per-kind default when unspecified.

    An explicit priority always wins — `batch_kinds` only decides what an
    *unspecified* priority means, so a caller that genuinely wants a `task`
    delivered now can still say so.
    """
    text = (str(value).strip().lower() if value is not None else "")
    if text in PRIORITIES:
        return text
    aliases = {
        "urgent": "high", "important": "high", "critical": "high", "now": "high",
        "routine": "low", "quiet": "low", "batch": "low", "digest": "low",
        "fyi": "low", "info": "low",
    }
    if text in aliases:
        return aliases[text]
    cfg = cfg if cfg is not None else load_config()
    if kind and kind in (cfg.get("batch_kinds") or []):
        return "low"
    return DEFAULT_PRIORITY


def should_batch(priority, cfg=None):
    """Only `low` batches, and only while the digest is switched on.

    With the digest off, a `low` notification is delivered normally rather
    than queued — otherwise turning the feature off would quietly start
    swallowing messages into a file nobody flushes.
    """
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("enabled")) and priority == "low"


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _load_queue():
    data = atomic_io.read_json(QUEUE_FILE, default=None, expect=dict)
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _save_queue(items):
    return atomic_io.write_json(QUEUE_FILE, {"items": items[-MAX_QUEUE:]})


def enqueue(record):
    """Add one notification record (notifier.notify()'s shape) to the digest.

    Stores only what the summary needs. The full record already lives in the
    durable inbox, and duplicating it here would mean two copies that can
    disagree after an inbox prune.
    """
    items = _load_queue()
    items.append({
        "id": record.get("id"),
        "title": (record.get("title") or "Jarvis")[:120],
        "message": (record.get("message") or "")[:400],
        "kind": record.get("kind") or "notify",
        "job_id": record.get("job_id"),
        "failed": bool(record.get("failed")),
        "at": record.get("created_at") or _now_iso(),
    })
    return _save_queue(items)


def pending_count():
    return len(_load_queue())


def peek(limit=50):
    return _load_queue()[-limit:]


def clear():
    return _save_queue([])


def _now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def build_summary(items, since=None, now=None):
    """Turn a queue into one readable message.

    Collapsing is the whole value here. Seven identical "backup finished"
    lines is the noise the digest exists to remove, so repeats are grouped
    by (title, failed) and counted, newest timestamp shown. Failures are
    listed first and never collapsed away silently — a digest that buries
    the one failure among six successes has recreated the original problem.
    """
    now = now or datetime.now()
    if not items:
        return "Nothing to report since the last digest."

    groups = OrderedDict()
    for item in items:
        key = (item.get("title") or "Jarvis", bool(item.get("failed")))
        entry = groups.setdefault(key, {"count": 0, "last": "", "messages": []})
        entry["count"] += 1
        at = item.get("at") or ""
        if at > entry["last"]:
            entry["last"] = at
        msg = (item.get("message") or "").strip()
        if msg and msg not in entry["messages"]:
            entry["messages"].append(msg)

    failed = [(k, v) for k, v in groups.items() if k[1]]
    ok = [(k, v) for k, v in groups.items() if not k[1]]

    span = _describe_span(since, now)
    total = len(items)
    header = "Jarvis digest — %d notification%s%s" % (
        total, "" if total == 1 else "s", (" " + span) if span else "")

    lines = [header, ""]

    if failed:
        lines.append("Needs attention:")
        for (title, _f), entry in failed:
            lines.append("  " + _render_group(title, entry, show_messages=True))
        lines.append("")

    if ok:
        lines.append("Routine:" if failed else "")
        for (title, _f), entry in ok:
            lines.append("  " + _render_group(title, entry, show_messages=len(ok) <= 5))

    text = "\n".join(line for line in lines if line is not None).strip()
    if len(text) > MAX_DIGEST_CHARS:
        text = text[:MAX_DIGEST_CHARS - 40].rstrip() + "\n… (see the Jarvis inbox for the rest)"
    return text


def _render_group(title, entry, show_messages=False):
    count = entry["count"]
    line = title
    if count > 1:
        line += " ×%d" % count
    when = _clock(entry.get("last"))
    if when:
        line += " (%s)" % when
    if show_messages and entry["messages"]:
        first = entry["messages"][0]
        if len(first) > 120:
            first = first[:119].rstrip() + "…"
        line += " — " + first
        if len(entry["messages"]) > 1:
            line += " (+%d more)" % (len(entry["messages"]) - 1)
    return line


def _clock(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _describe_span(since, now):
    if not since:
        return ""
    try:
        start = datetime.fromisoformat(since) if isinstance(since, str) else since
    except (TypeError, ValueError):
        return ""
    delta = now - start
    if delta < timedelta(hours=2):
        return "in the last hour"
    if delta < timedelta(days=1, hours=6):
        return "since yesterday"
    days = max(1, delta.days)
    return "over the last %d days" % days


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def flush(force=False, dry_run=False):
    """Send the digest. Returns a result dict; never raises.

    On a delivery failure the queue is deliberately left intact — see the
    module docstring. `force` sends even an empty digest (what `jarvis
    digest-now` passes, since asking for it explicitly and getting silence
    is confusing); the scheduled path honors send_when_empty instead.
    """
    cfg = load_config()
    items = _load_queue()

    if not items and not force and not cfg.get("send_when_empty"):
        return {"ok": True, "sent": False, "count": 0, "reason": "nothing queued"}

    oldest = items[0].get("at") if items else None
    summary = build_summary(items, since=oldest)

    if dry_run:
        return {"ok": True, "sent": False, "count": len(items),
                "preview": summary, "reason": "dry run"}

    from .channels import outbound

    platform = (cfg.get("platform") or "").strip().lower()
    platforms = [platform] if platform else None
    try:
        outcome = outbound.notify_owner(summary, platforms=platforms,
                                        first_success_only=True)
    except Exception as exc:  # noqa: BLE001 — a dead channel must not lose the queue
        return {"ok": False, "sent": False, "count": len(items),
                "error": str(exc),
                "hint": "The queue was kept; it'll go out with the next digest."}

    if not outcome.get("ok"):
        detail = [f"{r['platform']}: {r['detail']}"
                  for r in outcome.get("results", []) if not r.get("ok")]
        return {"ok": False, "sent": False, "count": len(items),
                "error": "couldn't reach the owner on any channel",
                "detail": detail,
                "hint": "The queue was kept; it'll go out with the next digest. "
                        "Check `jarvis channels-status`."}

    clear()
    delivered = [r["platform"] for r in outcome.get("results", []) if r.get("ok")]
    return {"ok": True, "sent": True, "count": len(items),
            "delivered_to": delivered, "summary": summary}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _digest_jobs():
    """Every scheduled job this module owns, found by marker rather than by
    title — a user renaming the job in the web UI shouldn't orphan it."""
    from . import scheduler

    out = []
    try:
        jobs = scheduler.list_jobs(include_finished=True)
    except Exception:  # noqa: BLE001
        return out
    for job in jobs:
        action = job.get("action") or {}
        if action.get("marker") == JOB_MARKER or job.get("title") == JOB_TITLE:
            out.append(job)
    return out


def is_scheduled():
    from . import scheduler
    return any(j.get("status") == scheduler.STATUS_PENDING for j in _digest_jobs())


def trigger_text(schedule, at):
    """The timespec string for a cadence. Kept here rather than inline so
    `digest-on` and the doctor's "nothing is scheduled" check agree."""
    at = (at or "9am").strip()
    if schedule == "hourly":
        return "every hour"
    if schedule == "weekly":
        return "every monday at %s" % at
    return "every day at %s" % at


def schedule_digest(schedule=None, at=None):
    """(Re)register the flush job. Replaces any previous one.

    Implemented as a normal scheduled command job so it's visible in
    `sched-list` and the web UI's Scheduled panel like everything else —
    a background timer nobody can see is a background timer nobody can
    debug.
    """
    from . import scheduler

    cfg = load_config()
    schedule = schedule or cfg.get("schedule") or "daily"
    at = at or cfg.get("at") or "9am"
    if schedule not in SCHEDULES:
        return {"ok": False, "error": "schedule must be one of: %s" % ", ".join(SCHEDULES)}

    for job in _digest_jobs():
        try:
            scheduler.cancel(job.get("id"))
        except Exception:  # noqa: BLE001 — a stale job is not worth failing over
            pass

    cfg["enabled"] = True
    cfg["schedule"] = schedule
    cfg["at"] = at
    save_config(cfg)

    try:
        job = scheduler.create(
            kind="task",
            title=JOB_TITLE,
            when=trigger_text(schedule, at),
            action={"type": "tool", "tool": "send_digest", "arguments": {},
                    "marker": JOB_MARKER},
            # trusted: the user typed `jarvis digest-on` (or clicked it in
            # the web UI) — this is never reached from a model tool call, so
            # it shouldn't sit in the approval queue.
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "couldn't schedule the digest: %s" % exc,
                "hint": "Batching is on, but you'll need `jarvis digest-now` to send it."}

    return {"ok": True, "schedule": schedule, "at": at,
            "job_id": job.get("id") if isinstance(job, dict) else None,
            "next_run": job.get("next_run") if isinstance(job, dict) else None}


def unschedule():
    """Turn batching off and remove the job. Anything already queued is
    flushed rather than stranded — turning a feature off should not lose
    messages that were only held back because it was on."""
    from . import scheduler

    removed = 0
    for job in _digest_jobs():
        try:
            scheduler.cancel(job.get("id"))
            removed += 1
        except Exception:  # noqa: BLE001
            pass

    cfg = load_config()
    cfg["enabled"] = False
    save_config(cfg)

    pending = pending_count()
    flushed = None
    if pending:
        flushed = flush(force=True)
    return {"ok": True, "removed_jobs": removed, "flushed": flushed,
            "pending_was": pending}


def status():
    cfg = load_config()
    items = _load_queue()
    jobs = _digest_jobs()
    next_run = None
    for job in jobs:
        if job.get("next_run"):
            next_run = job["next_run"] if next_run is None else min(next_run, job["next_run"])
    return {
        "enabled": bool(cfg.get("enabled")),
        "schedule": cfg.get("schedule"),
        "at": cfg.get("at"),
        "batch_kinds": cfg.get("batch_kinds"),
        "pending": len(items),
        "oldest": items[0].get("at") if items else None,
        "scheduled": is_scheduled(),
        "next_run": next_run,
        "preview": build_summary(items) if items else "",
    }
