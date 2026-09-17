"""Calendar — read ICS feeds (Google, Outlook, Fastmail, anything) + a local store.

WHY ICS AND NOT THE GOOGLE CALENDAR API
---------------------------------------
The Google Calendar API means an OAuth app, a consent screen, a client
secret, a refresh-token dance, and a `google-api-python-client` dependency
that pulls in a dozen transitive packages. For *reading your own calendar*,
every one of those calendars already exposes a secret ICS URL — Google calls
it "Secret address in iCal format", Outlook "Publish a calendar", Fastmail
and Apple the same. One URL, no OAuth, no dependency beyond `requests`,
which is already a base dependency.

The trade is real and worth stating: ICS is read-only and cached, typically
refreshed by the provider every few hours, so "did my 3pm move ten minutes
ago" is a question this cannot answer. Writing goes to the local store
instead. If someone needs true two-way sync with Google specifically, that's
an MCP server's job — not a reason to make every Jarvis install carry an
OAuth flow it will never use.

WHY A LOCAL STORE AS WELL
-------------------------
So `calendar_add_event` has somewhere to write, and so someone with no
calendar service at all still gets something useful. Local events live in
~/.jarvis/calendar.json and are merged with feed events on every read, sorted
together — from the model's point of view there is one calendar.

PAIRING WITH THE SCHEDULER
--------------------------
`calendar_events` returns each event with a `starts_in_seconds`, which is
what makes "remind me an hour before my next meeting" a single ordinary
schedule_task call against a real timestamp rather than a new subsystem.
"""

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "calendar.json"
CACHE_FILE = JARVIS_DIR / "calendar_cache.json"
ENCODING = "utf-8"

# A feed is fetched at most this often. Provider-side ICS is regenerated on
# their schedule (Google: roughly every few hours), so polling harder buys
# nothing but latency and rate limits.
CACHE_TTL_SECONDS = 900
FETCH_TIMEOUT = 15
MAX_ICS_BYTES = 4_000_000
MAX_EVENTS_RETURNED = 40
DEFAULT_LOOKAHEAD_DAYS = 7

DEFAULT_CONFIG = {
    "feeds": [],          # [{"name": "work", "url": "https://...ics", "enabled": true}]
    "local_events": [],
    "default_reminder_minutes": 60,
}


def ensure_config():
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            from .. import atomic_io
            atomic_io.write_json(CONFIG_FILE, DEFAULT_CONFIG)
    except (OSError, ImportError):
        pass
    return CONFIG_FILE


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in cfg or k.startswith("_")})
    except (FileNotFoundError, ValueError, OSError):
        pass
    return cfg


def save_config(cfg):
    from .. import atomic_io
    ensure_config()
    return atomic_io.write_json(CONFIG_FILE, cfg)


# ---------------------------------------------------------------------------
# ICS parsing
#
# A deliberately small subset: VEVENT, SUMMARY, DTSTART, DTEND, LOCATION,
# DESCRIPTION, plus enough RRULE to expand a simple daily/weekly repeat inside
# the window being asked about. Full RFC 5545 (RDATE, EXDATE, BYSETPOS,
# VTIMEZONE with custom offsets) is a library's job; this handles what a
# personal calendar actually contains and ignores the rest rather than
# failing on it.
# ---------------------------------------------------------------------------

_UNFOLD_RE = re.compile(r"\r?\n[ \t]")
_PROP_RE = re.compile(r"^(?P<name>[A-Z\-]+)(?P<params>;[^:]*)?:(?P<value>.*)$")


def parse_ics(text, window_start=None, window_end=None):
    """ICS text -> [event dicts]. Never raises; a malformed block is skipped."""
    if not text:
        return []
    # Unfold: ICS wraps long lines with a CRLF + single space/tab.
    text = _UNFOLD_RE.sub("", text)
    events = []
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                parsed = _finish_event(current, window_start, window_end)
                events.extend(parsed)
            current = None
            continue
        if current is None:
            continue
        match = _PROP_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        params = match.group("params") or ""
        value = match.group("value")
        if name in ("DTSTART", "DTEND"):
            current[name] = (value, params)
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "STATUS"):
            current[name] = value
    return events


def _finish_event(raw, window_start, window_end):
    start = _parse_ics_dt(*raw.get("DTSTART", ("", "")))
    if not start:
        return []
    end = _parse_ics_dt(*raw.get("DTEND", ("", ""))) or (start + timedelta(hours=1))
    if (raw.get("STATUS") or "").upper() == "CANCELLED":
        return []

    base = {
        "title": _unescape(raw.get("SUMMARY") or "(no title)"),
        "location": _unescape(raw.get("LOCATION") or ""),
        "description": _unescape(raw.get("DESCRIPTION") or "")[:400],
        "uid": raw.get("UID") or "",
    }

    occurrences = _expand_rrule(start, end, raw.get("RRULE"), window_start, window_end)
    out = []
    for occ_start, occ_end in occurrences:
        if window_start and occ_end < window_start:
            continue
        if window_end and occ_start > window_end:
            continue
        out.append(dict(base, start=occ_start, end=occ_end))
    return out


def _expand_rrule(start, end, rrule, window_start, window_end):
    """Expand a simple RRULE across the window. No RRULE -> one occurrence.

    Handles FREQ=DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT and UNTIL,
    plus BYDAY for weekly. Anything else falls back to the single original
    occurrence, which is the safe direction to be wrong in: showing one real
    event beats inventing a series that doesn't exist.
    """
    if not rrule or not window_end:
        return [(start, end)]
    parts = {}
    for chunk in rrule.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            parts[key.upper()] = value
    freq = (parts.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [(start, end)]

    try:
        interval = max(1, int(parts.get("INTERVAL") or 1))
    except ValueError:
        interval = 1
    count_cap = None
    if parts.get("COUNT"):
        try:
            count_cap = int(parts["COUNT"])
        except ValueError:
            count_cap = None
    until = None
    if parts.get("UNTIL"):
        until = _parse_ics_dt(parts["UNTIL"], "")

    byday = [d.strip()[-2:].upper() for d in (parts.get("BYDAY") or "").split(",") if d.strip()]
    day_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    wanted_days = {day_map[d] for d in byday if d in day_map}

    duration = end - start
    step = {"DAILY": timedelta(days=interval),
            "WEEKLY": timedelta(weeks=interval),
            "MONTHLY": timedelta(days=30 * interval),
            "YEARLY": timedelta(days=365 * interval)}[freq]

    out = []
    guard = 0

    if freq == "WEEKLY" and wanted_days:
        # Iterate by WEEK, and terminate on the week's start rather than on a
        # cursor pinned to the series' original weekday. Terminating on the
        # cursor drops the final week entirely whenever the series started
        # late in the week: a Mon-Fri standup beginning on a Friday would
        # emit that one Friday and then stop, because the next cursor (the
        # following Friday) was already past the window even though Monday
        # through Thursday of that week were inside it.
        week = start - timedelta(days=start.weekday())
        while guard < 400:
            guard += 1
            if window_end and week > window_end:
                break
            if count_cap is not None and len(out) >= count_cap:
                break
            for offset in sorted(wanted_days):
                occ = (week + timedelta(days=offset)).replace(
                    hour=start.hour, minute=start.minute, second=0, microsecond=0)
                if occ < start:
                    continue
                if window_end and occ > window_end:
                    continue
                if until and occ > until:
                    continue
                if count_cap is not None and len(out) >= count_cap:
                    break
                out.append((occ, occ + duration))
            week += timedelta(weeks=interval)
        return out or [(start, end)]

    cursor = start
    # Hard iteration cap: a malformed infinite RRULE must not hang a tool
    # call. 800 covers daily events across any realistic lookahead window.
    while guard < 800:
        guard += 1
        if until and cursor > until:
            break
        if window_end and cursor > window_end:
            break
        if count_cap is not None and len(out) >= count_cap:
            break
        out.append((cursor, cursor + duration))
        cursor += step
    return out or [(start, end)]


def _parse_ics_dt(value, params):
    """ICS timestamp -> naive local datetime.

    Everything in this project is naive local time on purpose (see
    timespec.py). A UTC ('Z') stamp is converted; a floating or TZID stamp is
    taken at face value, which is what the calendar owner meant when they
    typed it.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        if "VALUE=DATE" in (params or "") or len(value) == 8:
            return datetime.strptime(value[:8], "%Y%m%d")
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def _unescape(text):
    return (text or "").replace("\\n", "\n").replace("\\,", ",") \
                       .replace("\\;", ";").replace("\\\\", "\\").strip()


# ---------------------------------------------------------------------------
# Feeds + cache
# ---------------------------------------------------------------------------


def _load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_cache(data):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data), encoding=ENCODING)
    except OSError:
        pass  # a cache that can't be written just means refetching


def fetch_feed(feed, force=False):
    """Raw ICS for one feed, from cache when fresh. ("", reason) on failure."""
    url = (feed.get("url") or "").strip()
    if not url.startswith(("http://", "https://", "webcal://")):
        return "", "not an http(s) URL"
    url = url.replace("webcal://", "https://", 1)

    cache = _load_cache()
    entry = cache.get(url) or {}
    age = None
    if entry.get("at"):
        try:
            age = (datetime.now() - datetime.fromisoformat(entry["at"])).total_seconds()
        except ValueError:
            age = None
    if not force and entry.get("body") and age is not None and age < CACHE_TTL_SECONDS:
        return entry["body"], ""

    try:
        import requests
    except ImportError:
        return entry.get("body", ""), "requests is not installed"

    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT,
                            headers={"Accept": "text/calendar"})
        if resp.status_code >= 400:
            # Serve stale rather than nothing: a calendar from an hour ago is
            # far more useful than an error, and the caller is told it's stale.
            return entry.get("body", ""), "HTTP %d" % resp.status_code
        body = resp.text[:MAX_ICS_BYTES]
    except Exception as exc:  # noqa: BLE001 — a dead feed is a finding, not a crash
        return entry.get("body", ""), str(exc)[:120]

    cache[url] = {"body": body, "at": datetime.now().replace(microsecond=0).isoformat()}
    _save_cache(cache)
    return body, ""


def local_events(cfg=None):
    cfg = cfg or load_config()
    out = []
    for entry in cfg.get("local_events") or []:
        if not isinstance(entry, dict):
            continue
        try:
            start = datetime.fromisoformat(entry["start"])
        except (KeyError, ValueError, TypeError):
            continue
        try:
            end = datetime.fromisoformat(entry["end"]) if entry.get("end") else start + timedelta(hours=1)
        except (ValueError, TypeError):
            end = start + timedelta(hours=1)
        out.append({
            "title": entry.get("title") or "(no title)",
            "location": entry.get("location") or "",
            "description": entry.get("description") or "",
            "uid": entry.get("id") or "",
            "start": start, "end": end, "source": "local",
        })
    return out


def collect_events(days=DEFAULT_LOOKAHEAD_DAYS, force=False, now=None):
    """Every event in the window, from every source, sorted. Never raises."""
    now = now or datetime.now()
    window_end = now + timedelta(days=max(1, int(days or 1)))
    cfg = load_config()

    events, problems = [], []
    for feed in cfg.get("feeds") or []:
        if not isinstance(feed, dict) or feed.get("enabled") is False:
            continue
        body, err = fetch_feed(feed, force=force)
        name = feed.get("name") or "calendar"
        if err:
            problems.append("%s: %s" % (name, err))
        if not body:
            continue
        for event in parse_ics(body, now - timedelta(hours=12), window_end):
            event["source"] = name
            events.append(event)

    events.extend(e for e in local_events(cfg)
                  if e["end"] >= now - timedelta(hours=12) and e["start"] <= window_end)
    events.sort(key=lambda e: e["start"])
    return events, problems


def _serialize(event, now):
    start, end = event["start"], event["end"]
    return {
        "title": event["title"],
        "start": start.replace(microsecond=0).isoformat(),
        "end": end.replace(microsecond=0).isoformat(),
        "when": _friendly(start, now),
        "location": event.get("location") or None,
        "source": event.get("source") or "calendar",
        # The field that makes "remind me an hour before" one schedule_task
        # call instead of a new subsystem: a real number the model can
        # subtract from without doing date arithmetic in its head.
        "starts_in_seconds": int((start - now).total_seconds()),
        "in_progress": start <= now <= end,
    }


def _friendly(dt, now):
    delta_days = (dt.date() - now.date()).days
    clock = dt.strftime("%H:%M")
    if delta_days == 0:
        return "today " + clock
    if delta_days == 1:
        return "tomorrow " + clock
    if 0 < delta_days < 7:
        return dt.strftime("%a ") + clock
    return dt.strftime("%a %d %b ") + clock


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def tool_calendar_events(args):
    args = args or {}
    now = datetime.now()
    try:
        days = int(args.get("days") or DEFAULT_LOOKAHEAD_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_LOOKAHEAD_DAYS
    days = max(1, min(days, 90))

    events, problems = collect_events(days=days, force=bool(args.get("refresh")), now=now)

    query = (args.get("query") or "").strip().lower()
    if query:
        events = [e for e in events
                  if query in e["title"].lower() or query in (e.get("location") or "").lower()]

    if args.get("next_only"):
        upcoming = [e for e in events if e["end"] >= now]
        events = upcoming[:1]

    serialized = [_serialize(e, now) for e in events[:MAX_EVENTS_RETURNED]]
    result = {"ok": True, "count": len(serialized), "days": days, "events": serialized}
    if problems:
        # Surfaced rather than swallowed: "your calendar is empty" and "your
        # calendar feed is returning 403" look identical to a user otherwise,
        # and only one of them is worth doing something about.
        result["feed_problems"] = problems
    if not serialized and not problems and not (load_config().get("feeds")):
        result["hint"] = ("No calendar feeds configured. Add one with "
                          "`jarvis calendar-add <name> <secret-ics-url>` — in Google "
                          "Calendar it's Settings > your calendar > 'Secret address "
                          "in iCal format'.")
    return result


def tool_calendar_add_event(args):
    args = args or {}
    title = (args.get("title") or "").strip()
    if not title:
        return {"needs_clarification": True, "message": "What's the event called?"}

    from .. import timespec

    when = (args.get("when") or args.get("start") or "").strip()
    if not when:
        return {"needs_clarification": True, "message": "When is it?"}
    try:
        start = timespec.parse_when(when)
    except timespec.TimeSpecError as exc:
        return {"needs_clarification": True, "message": str(exc)}

    try:
        minutes = int(args.get("duration_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    end = start + timedelta(minutes=max(5, min(minutes, 1440)))

    cfg = load_config()
    entry = {
        "id": "ev_" + secrets.token_hex(4),
        "title": title[:200],
        "start": start.replace(microsecond=0).isoformat(),
        "end": end.replace(microsecond=0).isoformat(),
        "location": (args.get("location") or "")[:200],
        "description": (args.get("description") or "")[:500],
    }
    cfg.setdefault("local_events", []).append(entry)
    # Keep the local store bounded; past events are pruned rather than kept
    # forever, since this is a calendar, not an archive.
    cutoff = (datetime.now() - timedelta(days=60)).isoformat()
    cfg["local_events"] = [e for e in cfg["local_events"]
                           if (e.get("end") or e.get("start") or "") >= cutoff][-500:]
    save_config(cfg)

    return {"ok": True, "id": entry["id"], "title": title,
            "start": entry["start"], "when": _friendly(start, datetime.now()),
            "note": "Saved to Jarvis's own calendar. Subscribed feeds are read-only."}


TOOL_SCHEMAS = [
    {
        "name": "calendar_events",
        "description": (
            "List upcoming calendar events from the user's subscribed calendars "
            "and Jarvis's own. Use for 'what's on today', 'when is my next "
            "meeting', 'am I free Thursday'. Each event includes "
            "starts_in_seconds, so to set a reminder before one, call this "
            "first and then schedule_task with the computed time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "number", "description": "How far ahead to look. Default 7."},
                "query": {"type": "string", "description": "Only events matching this text."},
                "next_only": {"type": "boolean", "description": "Just the single next event."},
                "refresh": {"type": "boolean", "description": "Bypass the 15-minute feed cache."},
            },
            "required": [],
        },
    },
    {
        "name": "calendar_add_event",
        "description": (
            "Add an event to Jarvis's own calendar. Subscribed feeds are "
            "read-only, so this never writes to Google/Outlook. Use when the "
            "user asks to note something down rather than to be reminded — for "
            "a reminder use schedule_task instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What the event is."},
                "when": {"type": "string", "description": "Plain language: 'tomorrow at 3pm', 'friday 09:00'."},
                "duration_minutes": {"type": "number", "description": "Default 60."},
                "location": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title", "when"],
        },
    },
]

TOOLS = {
    "calendar_events": tool_calendar_events,
    "calendar_add_event": tool_calendar_add_event,
}

TOOL_GROUP = "calendar"

TOOL_KEYWORDS = {
    "calendar_events": {
        "calendar": 10, "my schedule": 10, "next meeting": 10, "what's on": 8,
        "whats on": 8, "am i free": 10, "appointment": 9, "appointments": 9,
        "meeting": 7, "meetings": 7, "agenda": 8, "booked": 6,
    },
    "calendar_add_event": {
        "add to my calendar": 10, "put in my calendar": 10,
        "schedule a meeting": 9, "book": 6, "new event": 9,
    },
}

TOOL_PACK_INSTRUCTION = (
    "calendar_events reads subscribed calendars; calendar_add_event writes only to "
    "Jarvis's own. For 'remind me N before my next meeting', call calendar_events "
    "with next_only first, then schedule_task using that event's start time minus N "
    "— don't guess a time. Subscribed feeds cache for 15 minutes; pass refresh only "
    "if the user says something just changed."
)
