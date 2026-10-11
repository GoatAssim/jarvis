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

THE CALENDARS PANEL (L.54)
--------------------------
Menu -> Calendars reads and edits the same two stores through ``admin()`` at the
bottom of this file (``jarvis calendar-admin <op>``, one JSON object on stdin, one
on stdout). It is a human-only path: no model tool reaches ``admin()``, the feed
links are secrets and are only ever sent back MASKED, and it refuses to run inside
a chat reply, a scheduled job or a background task (``unattended_reason``).
Linked (ICS) calendars stay read-only: the panel can link, switch off, refresh and
remove one, but it never writes an event to one. Editing and deleting is for
Jarvis's own events only.

PAIRING WITH THE SCHEDULER
--------------------------
`calendar_events` returns each event with a `starts_in_seconds`, which is
what makes "remind me an hour before my next meeting" a single ordinary
schedule_task call against a real timestamp rather than a new subsystem.
"""

import calendar
import hashlib
import json
import os
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

# Jarvis's own events are kept for this long after they end, and at most this many
# (the panel states both, because deleting old events silently would surprise).
LOCAL_RETENTION_DAYS = 60
LOCAL_RETENTION_MAX_DAYS = 3650
LOCAL_MAX_EVENTS = 500
# Calendars panel limits.
MAX_FEEDS = 12
FEED_NAME_MAX = 40
PANEL_MAX_RANGE_DAYS = 400
PANEL_MAX_EVENTS = 2000

DEFAULT_CONFIG = {
    "feeds": [],          # [{"name": "work", "url": "https://...ics", "enabled": true}]
    "local_events": [],
    "default_reminder_minutes": 60,
    # How long Jarvis's own events are kept after they end (1..3650). The default is
    # LOCAL_RETENTION_DAYS; a number outside the range is ignored. Edited in
    # Settings > Advanced (the panel only states it).
    "keep_days": LOCAL_RETENTION_DAYS,
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


def load_config_strict():
    """(cfg, None), or (None, reason) when calendar.json exists but cannot be read.

    ``load_config()`` answers "the defaults" for a file it cannot parse, which is right
    for a READ and wrong for a WRITE: saving those defaults back would replace the
    owner's linked calendars and events with an empty calendar. Every writer (the
    panel's and the model tool's) loads through this instead. A missing or empty file
    is a fresh install, not a problem.
    """
    try:
        text = CONFIG_FILE.read_text(encoding=ENCODING)
    except FileNotFoundError:
        return load_config(), None
    except OSError as exc:
        return None, "calendar.json can't be read (%s), so nothing was changed." % str(exc)[:100]
    if not text.strip():
        return load_config(), None
    try:
        data = json.loads(text)
    except ValueError:
        return None, ("calendar.json isn't valid JSON, so nothing was changed. Fix it in "
                      "Settings > Advanced, or restore calendar.json.bak next to it.")
    if not isinstance(data, dict):
        return None, "calendar.json isn't a JSON object, so nothing was changed."
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update({k: v for k, v in data.items() if k in cfg or k.startswith("_")})
    return cfg, None


def retention_days(cfg=None):
    """Days Jarvis's own events are kept after they end: ``keep_days`` from calendar.json
    when it is a whole number from 1 to 3650, else LOCAL_RETENTION_DAYS."""
    cfg = cfg if cfg is not None else load_config()
    value = cfg.get("keep_days", LOCAL_RETENTION_DAYS)
    if isinstance(value, bool) or not isinstance(value, int):
        return LOCAL_RETENTION_DAYS
    if 1 <= value <= LOCAL_RETENTION_MAX_DAYS:
        return value
    return LOCAL_RETENTION_DAYS


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
_PROP_RE = re.compile(r"^(?P<name>[A-Za-z\-]+)(?P<params>;[^:]*)?:(?P<value>.*)$")

# Occurrence expansion limits: a malformed or huge rule must not hang a tool call.
MAX_RRULE_STEPS = 20000
MAX_OCCURRENCES_PER_SERIES = 1500
# A zone's offset from the PC's own can reach a day and a bit, so a window is padded by
# this much when it is applied to a series still in its own zone's wall-clock time.
_ZONE_PAD = timedelta(days=2)


def parse_ics(text, window_start=None, window_end=None):
    """ICS text -> [event dicts]. Never raises; a malformed block is skipped.

    A repeating event is expanded across the window. An occurrence the calendar crossed
    out (EXDATE) or replaced (a VEVENT with the same UID and a RECURRENCE-ID) is not shown
    as well as its replacement."""
    if not text:
        return []
    # Unfold: ICS wraps long lines with a CRLF + single space/tab.
    text = _UNFOLD_RE.sub("", text)
    raws = []
    current = None
    alarm_depth = 0
    for line in text.splitlines():
        line = line.strip()
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            alarm_depth = 0
            continue
        if upper == "END:VEVENT":
            if current:
                raws.append(current)
            current = None
            alarm_depth = 0
            continue
        if current is None:
            continue
        # A VALARM inside an event has its own DESCRIPTION (\"Reminder\") that must not
        # overwrite the event's.
        if upper == "BEGIN:VALARM":
            alarm_depth += 1
            continue
        if upper == "END:VALARM":
            alarm_depth = max(0, alarm_depth - 1)
            continue
        if alarm_depth:
            continue
        match = _PROP_RE.match(line)
        if not match:
            continue
        name = match.group("name").upper()
        params = match.group("params") or ""
        value = match.group("value")
        if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            current[name] = (value, params)
        elif name == "EXDATE":
            current.setdefault("EXDATE", []).append((value, params))
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "STATUS"):
            current[name] = value

    # Occurrences of a series that another VEVENT (same UID) replaced.
    replaced = {}
    for raw in raws:
        uid = (raw.get("UID") or "").strip()
        if uid and raw.get("RECURRENCE-ID"):
            _add_skip(replaced.setdefault(uid, _new_skip()), *raw["RECURRENCE-ID"])

    events = []
    for raw in raws:
        skip = _new_skip()
        for value, params in raw.get("EXDATE") or []:
            _add_skip(skip, value, params)
        if raw.get("RRULE") and not raw.get("RECURRENCE-ID"):
            other = replaced.get((raw.get("UID") or "").strip())
            if other:
                skip["datetimes"] |= other["datetimes"]
                skip["dates"] |= other["dates"]
        try:
            events.extend(_finish_event(raw, window_start, window_end, skip))
        except Exception:  # noqa: BLE001 -- one bad block must not lose the whole calendar
            continue
    return events


def _new_skip():
    return {"datetimes": set(), "dates": set()}


def _add_skip(skip, value, params):
    """Record the occurrence(s) an EXDATE / RECURRENCE-ID names: a date-only value crosses
    out that day, a time crosses out that moment (compared as the PC's local time)."""
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        wall, zone = _parse_ics_dt_wall(part, params)
        if wall is None:
            continue
        if _is_date_value(part, params):
            skip["dates"].add(wall.date())
        else:
            skip["datetimes"].add(_to_local(wall, zone))


def _finish_event(raw, window_start, window_end, skip=None):
    start_wall, zone = _parse_ics_dt_wall(*raw.get("DTSTART", ("", "")))
    if start_wall is None:
        return []
    end_wall, end_zone = _parse_ics_dt_wall(*raw.get("DTEND", ("", "")))
    if end_wall is None:
        # A date-only DTSTART with no DTEND is an all-day event: one day long
        # (RFC 5545 section 3.6.1). Anything else defaults to an hour, as before.
        dtstart_value, dtstart_params = raw.get("DTSTART", ("", ""))
        all_day = _is_date_value(dtstart_value, dtstart_params)
        end_wall = start_wall + (timedelta(days=1) if all_day else timedelta(hours=1))
        end_zone = zone
    if (raw.get("STATUS") or "").upper() == "CANCELLED":
        return []

    # Length, measured on the PC's clock; a series keeps it for every occurrence.
    duration = _to_local(end_wall, end_zone) - _to_local(start_wall, zone)
    if duration < timedelta(0):
        duration = timedelta(0)
    wall_duration = end_wall - start_wall if zone == end_zone else duration
    if wall_duration < timedelta(0):
        wall_duration = duration

    base = {
        "title": _unescape(raw.get("SUMMARY") or "(no title)"),
        "location": _unescape(raw.get("LOCATION") or ""),
        "description": _unescape(raw.get("DESCRIPTION") or "")[:400],
        "uid": raw.get("UID") or "",
    }

    # A series is expanded in its own zone's wall-clock time (a 09:00 meeting in Paris stays
    # 09:00 in Paris across a clock change) and each occurrence is then put on the PC's clock.
    pad_start = window_start - _ZONE_PAD if window_start else None
    pad_end = window_end + _ZONE_PAD if window_end else None
    occurrences = _expand_rrule(start_wall, start_wall + wall_duration, raw.get("RRULE"),
                                pad_start, pad_end, zone=zone)
    skip = skip or _new_skip()
    out = []
    for occ_wall_start, _occ_wall_end in occurrences:
        occ_start = _to_local(occ_wall_start, zone)
        occ_end = occ_start + duration
        if occ_start in skip["datetimes"] or occ_start.date() in skip["dates"]:
            continue
        if window_start and occ_end < window_start:
            continue
        if window_end and occ_start > window_end:
            continue
        out.append(dict(base, start=occ_start, end=occ_end))
    return out


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_RRULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH",
               "BYSETPOS", "WKST"}
_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")


def _parse_byday(text):
    """\"MO,1FR,-1SU\" -> [(None, 0), (1, 4), (-1, 6)]; None when any item is not understood."""
    out = []
    for item in (text or "").split(","):
        item = item.strip().upper()
        if not item:
            continue
        match = _BYDAY_RE.match(item)
        if not match:
            return None
        out.append((int(match.group(1)) if match.group(1) else None, _WEEKDAYS[match.group(2)]))
    return out


def _parse_ints(text, low, high):
    """\"1,15,-1\" -> [1, 15, -1] (each non-zero, within +-high); None when anything is off."""
    out = []
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            n = int(item)
        except ValueError:
            return None
        if n == 0 or n < low or n > high:
            return None
        out.append(n)
    return out


def _month_candidates(year, month, bymonthday, byday, bysetpos, default_day):
    """The days of one month a MONTHLY / YEARLY rule lands on, ascending."""
    last = calendar.monthrange(year, month)[1]
    by_date = None
    if bymonthday:
        by_date = set()
        for n in bymonthday:
            day = n if n > 0 else last + 1 + n
            if 1 <= day <= last:
                by_date.add(day)
    by_weekday = None
    if byday:
        by_weekday = set()
        for ordinal, weekday in byday:
            matches = [d for d in range(1, last + 1) if datetime(year, month, d).weekday() == weekday]
            if ordinal is None:
                by_weekday.update(matches)
            else:
                index = ordinal - 1 if ordinal > 0 else ordinal
                if -len(matches) <= index < len(matches):
                    by_weekday.add(matches[index])
    if by_date is not None and by_weekday is not None:
        days = by_date & by_weekday
    elif by_date is not None:
        days = by_date
    elif by_weekday is not None:
        days = by_weekday
    else:
        days = {default_day} if default_day <= last else set()
    days = sorted(days)
    if bysetpos:
        picked = []
        for pos in bysetpos:
            index = pos - 1 if pos > 0 else pos
            if -len(days) <= index < len(days):
                picked.append(days[index])
        days = sorted(set(picked))
    return days


def _rrule_until(raw, zone):
    """UNTIL as a wall-clock datetime in the series' own zone, or None. A date-only UNTIL
    includes that whole day."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 8:
            return datetime.strptime(raw, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
        if raw.endswith("Z"):
            utc = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if zone is not None:
                return utc.astimezone(zone).replace(tzinfo=None)
            return utc.astimezone(_local_tz()).replace(tzinfo=None)
        return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
    except (ValueError, OverflowError, OSError):
        return None


def _expand_rrule(start, end, rrule, window_start, window_end, zone=None):
    """Expand a repeat rule across the window. No RRULE (or no window) -> the one original
    occurrence. ``start``/``end``/the window are wall-clock times in the series' own zone.

    Handles FREQ=DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT and UNTIL; BYDAY for
    daily and weekly; BYMONTHDAY, BYDAY (also \"first Monday\" / \"last Friday\") and BYSETPOS
    for monthly; BYMONTH for yearly. A months-long step is a real calendar month (a
    repeat on the 15th stays on the 15th; one on the 31st skips the months without a 31st,
    as RFC 5545 says). Anything else falls back to the single original occurrence, which is
    the safe direction to be wrong in: showing one real event beats inventing a series that
    doesn't exist.

    Only the occurrences overlapping the window are returned, but COUNT is counted from the
    start of the series, so a series years old still ends where it should."""
    if not rrule or not window_end:
        return [(start, end)]
    parts = {}
    for chunk in rrule.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            parts[key.strip().upper()] = value.strip()
    freq = (parts.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or not set(parts) <= _RRULE_KEYS:
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
            return [(start, end)]
        if count_cap < 1:
            return [(start, end)]
    until = _rrule_until(parts.get("UNTIL"), zone) if parts.get("UNTIL") else None
    if parts.get("UNTIL") and until is None:
        return [(start, end)]

    byday = _parse_byday(parts["BYDAY"]) if parts.get("BYDAY") else []
    bymonthday = _parse_ints(parts["BYMONTHDAY"], -31, 31) if parts.get("BYMONTHDAY") else []
    bymonth = _parse_ints(parts["BYMONTH"], 1, 12) if parts.get("BYMONTH") else []
    bysetpos = _parse_ints(parts["BYSETPOS"], -366, 366) if parts.get("BYSETPOS") else []
    if byday is None or bymonthday is None or bymonth is None or bysetpos is None:
        return [(start, end)]
    ordinal_days = any(o is not None for o, _ in byday)

    # Which combinations are understood; the rest keep the one original occurrence.
    if freq in ("DAILY", "WEEKLY"):
        if ordinal_days or bymonthday or bymonth or bysetpos:
            return [(start, end)]
    elif freq == "MONTHLY":
        if bymonth:
            return [(start, end)]
    elif freq == "YEARLY":
        if (byday or bymonthday) and not bymonth:
            return [(start, end)]
        if bysetpos and not (byday or bymonthday):
            return [(start, end)]

    duration = end - start
    # Without COUNT a series can be jumped close to the window instead of walked from its
    # first day (a daily event from years ago must still be found).
    jump_from = window_start if count_cap is None else None

    def occurrences():
        if freq == "DAILY":
            wanted = {w for _, w in byday}
            k = 0
            if jump_from and jump_from > start:
                k = max(0, (jump_from - start).days // interval - 1)
            while True:
                occ = start + timedelta(days=k * interval)
                k += 1
                if wanted and occ.weekday() not in wanted:
                    continue
                yield occ
        elif freq == "WEEKLY" and byday:
            wanted = sorted({w for _, w in byday})
            week0 = start - timedelta(days=start.weekday())
            k = 0
            if jump_from and jump_from > start:
                k = max(0, (jump_from - week0).days // (7 * interval) - 1)
            while True:
                base = week0 + timedelta(weeks=k * interval)
                k += 1
                for offset in wanted:
                    occ = base + timedelta(days=offset)
                    if occ >= start:
                        yield occ
        elif freq == "WEEKLY":
            k = 0
            if jump_from and jump_from > start:
                k = max(0, (jump_from - start).days // (7 * interval) - 1)
            while True:
                yield start + timedelta(weeks=k * interval)
                k += 1
        elif freq == "MONTHLY":
            k = 0
            if jump_from and jump_from > start:
                months = (jump_from.year - start.year) * 12 + jump_from.month - start.month
                k = max(0, months // interval - 1)
            while True:
                total = start.year * 12 + (start.month - 1) + k * interval
                year, month = divmod(total, 12)
                month += 1
                k += 1
                if year > 9998:
                    return
                for day in _month_candidates(year, month, bymonthday, byday, bysetpos, start.day):
                    occ = start.replace(year=year, month=month, day=day)
                    if occ >= start:
                        yield occ
        else:  # YEARLY
            months = sorted(set(bymonth)) or [start.month]
            k = 0
            if jump_from and jump_from > start:
                k = max(0, (jump_from.year - start.year) // interval - 1)
            while True:
                year = start.year + k * interval
                k += 1
                if year > 9998:
                    return
                for month in months:
                    for day in _month_candidates(year, month, bymonthday, byday, bysetpos, start.day):
                        occ = start.replace(year=year, month=month, day=day)
                        if occ >= start:
                            yield occ

    out = []
    counted = 0
    for steps, occ in enumerate(occurrences(), 1):
        if steps > MAX_RRULE_STEPS:
            break
        if until and occ > until:
            break
        if occ > window_end:
            break
        counted += 1
        if count_cap is not None and counted > count_cap:
            break
        occ_end = occ + duration
        if window_start and occ_end < window_start:
            continue
        out.append((occ, occ_end))
        if len(out) >= MAX_OCCURRENCES_PER_SERIES:
            break
    return out


def _is_date_value(value, params):
    """True for a date-only stamp (``DTSTART;VALUE=DATE:20261012``). ``VALUE=DATE-TIME``
    is NOT one: a plain substring test on ``VALUE=DATE`` matched it and dropped the time."""
    return bool(re.search(r"VALUE=DATE(?!-)", params or "", re.I)) or len((value or "").strip()) == 8


# Windows calendars (Outlook) name zones the Windows way; these are the common ones.
_WINDOWS_ZONES = {
    "UTC": "UTC", "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau", "GTB Standard Time": "Europe/Bucharest",
    "FLE Standard Time": "Europe/Kiev", "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul", "Egypt Standard Time": "Africa/Cairo",
    "South Africa Standard Time": "Africa/Johannesburg", "W. Central Africa Standard Time": "Africa/Lagos",
    "Arab Standard Time": "Asia/Riyadh", "Arabian Standard Time": "Asia/Dubai",
    "Israel Standard Time": "Asia/Jerusalem", "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
    "Singapore Standard Time": "Asia/Singapore", "AUS Eastern Standard Time": "Australia/Sydney",
    "Pacific Standard Time": "America/Los_Angeles", "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago", "Eastern Standard Time": "America/New_York",
    "Atlantic Standard Time": "America/Halifax", "SA Pacific Standard Time": "America/Bogota",
    "E. South America Standard Time": "America/Sao_Paulo",
}
_TZID_RE = re.compile(r'TZID=("[^"]*"|[^;:]*)', re.I)
_ZONES = {}


def _zone_for(params):
    """The tzinfo a ``TZID=`` parameter names, or None when there is none or this PC cannot
    resolve it (the time is then taken at face value, as before)."""
    match = _TZID_RE.search(params or "")
    if not match:
        return None
    name = match.group(1).strip().strip('"').strip()
    if not name:
        return None
    if name in _ZONES:
        return _ZONES[name]
    zone = None
    if name.upper() in ("UTC", "GMT", "Z"):
        zone = timezone.utc
    else:
        try:
            from zoneinfo import ZoneInfo
            for candidate in (name, _WINDOWS_ZONES.get(name)):
                if not candidate:
                    continue
                try:
                    zone = ZoneInfo(candidate)
                    break
                except Exception:  # noqa: BLE001 -- unknown name, or no tz database here
                    continue
        except ImportError:
            zone = None
    _ZONES[name] = zone
    return zone


def _local_tz():
    """The PC's own zone (None = the system's). A function so a test can pin one."""
    return None


def _to_local(naive, zone):
    """A wall-clock time in ``zone`` -> naive time on this PC's clock. No zone -> as is."""
    if zone is None:
        return naive
    try:
        return naive.replace(tzinfo=zone).astimezone(_local_tz()).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return naive


def _parse_ics_dt_wall(value, params):
    """ICS timestamp -> (naive wall-clock time, tzinfo or None), or (None, None).

    A UTC ('Z') stamp is UTC; a ``TZID=`` stamp is in that zone when this PC can resolve
    it; a floating or date-only stamp has no zone (the calendar owner meant the clock on
    the wall where they typed it)."""
    value = (value or "").strip()
    if not value:
        return None, None
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ"), timezone.utc
        if _is_date_value(value, params):
            return datetime.strptime(value[:8], "%Y%m%d"), None
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S"), _zone_for(params)
    except ValueError:
        return None, None


def _parse_ics_dt(value, params):
    """ICS timestamp -> naive local datetime (everything in this project is naive local
    time on purpose, see timespec.py). UTC and resolvable TZID stamps are converted to
    this PC's clock; a floating or unresolvable one is taken at face value."""
    wall, zone = _parse_ics_dt_wall(value, params)
    if wall is None:
        return None
    return _to_local(wall, zone)


_UNESCAPE_RE = re.compile(r"\\(.)", re.S)


def _unescape(text):
    """RFC 5545 TEXT: \\n and \\N are a line break; \\, \\; \\\\ are the plain character."""
    return _UNESCAPE_RE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), text or "").strip()


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


def _decode_body(resp):
    """The reply's text. ``requests`` decodes a ``text/*`` reply that names no charset as
    Latin-1 (an old HTTP rule), which turns accents and Arabic titles in a calendar into
    mojibake; an iCalendar file with no charset is UTF-8 (RFC 5545), so decode it that way."""
    headers = getattr(resp, "headers", None) or {}
    try:
        content_type = str(headers.get("Content-Type") or "").lower()
    except AttributeError:
        content_type = ""
    raw = getattr(resp, "content", None)
    if "charset=" not in content_type and isinstance(raw, (bytes, bytearray)):
        try:
            return bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    return resp.text


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
        body = _decode_body(resp)[:MAX_ICS_BYTES]
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
            "start": start, "end": end, "source": "local", "local": True,
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
    out = {
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
    if event.get("local") and event.get("uid"):
        # Only Jarvis's own events can be changed, and this id is how
        # calendar_update_event / calendar_delete_event name one.
        out["id"] = event["uid"]
    return out


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
# Calendars panel back end (L.54)
#
# Everything below is for the Menu -> Calendars panel, reached only through
# ``admin()`` (``jarvis calendar-admin <op>``). None of it is a model tool, and
# none of it is added to TOOLS / TOOL_SCHEMAS: a feed link is a secret and an
# event written here is the owner's own, so a chat guest or a fetched page must
# not be able to steer it. The model's calendar tools are add, read, change and delete
# events on JARVIS'S OWN calendar -- none of them can link, switch or remove a feed.
# ---------------------------------------------------------------------------

_UNATTENDED_MESSAGE = ("The Calendars panel can only be used from the web page or your own "
                       "terminal, not from a chat message, a scheduled job or a background task.")

_EVENT_ID_RE = re.compile(r"^ev_[0-9a-f]{4,16}$")


def unattended_reason():
    """A reason string if this process must not touch the calendar admin, else None.

    A chat reply, a scheduled job and a subagent all run the same CLI. Anything the
    panel can do (link a secret URL, fetch a URL it was handed, write an event) could
    otherwise be done by whoever can steer that conversation.
    """
    for var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID"):
        if os.environ.get(var):
            return _UNATTENDED_MESSAGE
    if os.environ.get("JARVIS_CONTEXT") in ("scheduled", "unattended"):
        return _UNATTENDED_MESSAGE
    return None


def mask_url(url):
    """A feed link with its secret hidden: scheme + host, then the first and last few
    characters of a long path, never the middle. A short path is hidden entirely
    (a short secret would otherwise be mostly on show). Userinfo is dropped."""
    url = (url or "").strip()
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://([^/?#]*)(.*)$", url)
    if not match:
        return "(not a link)" if url else ""
    scheme, authority, rest = match.groups()
    host = authority.rsplit("@", 1)[-1]
    if not rest:
        tail = ""
    elif len(rest) <= 24:
        tail = "/\u2026"
    else:
        tail = rest[:5] + "\u2026" + rest[-4:]
    return "%s://%s%s" % (scheme.lower(), host, tail)


def _clean_link(url):
    """What the owner pasted -> what gets fetched and saved: whitespace, quotes and
    angle brackets off, the scheme lower-cased, ``webcal://`` rewritten to https."""
    url = (url or "").strip().strip("\"'<>").strip()
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", url, re.S)
    if match:
        scheme = match.group(1).lower()
        if scheme == "webcal":
            scheme = "https"
        url = scheme + "://" + match.group(2)
    return url


def _fetch_key(url):
    """The cache key fetch_feed uses for this link."""
    return (url or "").strip().replace("webcal://", "https://", 1)


def _scrub(text, *links):
    """Take any link out of an error message before it goes to the browser.

    ``requests`` puts the whole address in its errors ("...Max retries exceeded with url:
    /calendar/ical/<secret>/basic.ics"), and the address IS the secret. The links being
    fetched are removed by value (whole, and the path/query part alone), then anything
    that still looks like a link or a ``url: /path``."""
    text = str(text or "")
    for link in links:
        for form in {(link or "").strip(), _fetch_key(link)}:
            if form:
                text = text.replace(form, "(link hidden)")
        match = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/?#]*(.*)$", (link or "").strip())
        tail = match.group(1) if match else ""
        if len(tail) > 3:
            text = text.replace(tail, "(hidden)")
    text = re.sub(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+", "(link hidden)", text)
    text = re.sub(r"(\burl:?\s*)/\S+", r"\1(hidden)", text, flags=re.I)
    return text


def _is_all_day(start, end):
    return (start.hour, start.minute, start.second) == (0, 0, 0) \
        and (end.hour, end.minute, end.second) == (0, 0, 0) and end > start


def _feed_view(feed, cache):
    entry = cache.get(_fetch_key(feed.get("url"))) or {}
    return {
        "name": feed.get("name") or "calendar",
        "url_masked": mask_url(feed.get("url")),
        "enabled": feed.get("enabled") is not False,
        "last_fetched": entry.get("at") or None,
    }


def feeds_view():
    """The Calendars tab: every linked calendar with its link MASKED, plus the limits
    the panel states. The raw link never leaves this module."""
    cfg = load_config()
    cache = _load_cache()
    feeds = [_feed_view(f, cache) for f in (cfg.get("feeds") or []) if isinstance(f, dict)]
    try:
        lead = int(cfg.get("default_reminder_minutes") or 60)
    except (TypeError, ValueError):
        lead = 60
    return {
        "ok": True,
        "feeds": feeds,
        "max_feeds": MAX_FEEDS,
        "local_event_count": len([e for e in cfg.get("local_events") or [] if isinstance(e, dict)]),
        "default_reminder_minutes": max(1, min(lead, 10080)),
        "retention_days": retention_days(cfg),
        "retention_max": LOCAL_MAX_EVENTS,
        "cache_minutes": CACHE_TTL_SECONDS // 60,
    }


def check_feed(url):
    """Fetch a link the owner pasted and say what is on the other end, WITHOUT saving it.

    The guide's "check before saving" step. Always fetches fresh (a cached body would
    make a mistyped-then-fixed link look fine). A page that is not a calendar (a login
    wall, an HTML error) is reported as such, not as "0 events".
    """
    clean = _clean_link(url)
    if not clean:
        return {"ok": False, "error": "Paste the calendar link first."}
    if not clean.startswith(("http://", "https://")):
        return {"ok": False,
                "error": "That doesn't look like a web link. It should start with https:// "
                         "(or webcal://)."}
    body, err = fetch_feed({"url": clean}, force=True)
    if err:
        return {"ok": False, "error": "Couldn't open that link: %s" % _scrub(err, clean)}
    if not body or not body.strip():
        return {"ok": False, "error": "The link opened but was empty."}
    if "BEGIN:VCALENDAR" not in body.upper():
        return {"ok": False,
                "error": "That link opened a web page, not a calendar. Use the secret iCal / "
                         "ICS address (it usually ends in .ics), not the calendar's normal page."}
    now = datetime.now()
    found = len(parse_ics(body))
    upcoming = [e for e in parse_ics(body, now - timedelta(hours=12), now + timedelta(days=366))
                if e["end"] >= now]
    upcoming.sort(key=lambda e: e["start"])
    nxt = upcoming[0] if upcoming else None
    return {
        "ok": True,
        "events_found": found,
        "upcoming_found": len(upcoming),
        "next_event": ({"title": nxt["title"], "start": nxt["start"].replace(microsecond=0).isoformat()}
                       if nxt else None),
        "insecure": clean.startswith("http://"),
        "link": clean,
    }


def _clean_name(name):
    name = re.sub(r"\s+", " ", (name or "").strip())
    name = "".join(ch for ch in name if ch.isprintable())
    return name[:FEED_NAME_MAX].strip()


def link_feed(name, url):
    """Check, then save, a new linked calendar. Refuses a duplicate name, a link that is
    already linked, or a twelfth calendar. Returns the new feed MASKED."""
    name = _clean_name(name)
    if not name:
        return {"ok": False, "error": "Give the calendar a name."}
    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
    feeds = [f for f in (cfg.get("feeds") or []) if isinstance(f, dict)]
    if len(feeds) >= MAX_FEEDS:
        return {"ok": False, "error": "You already have %d linked calendars, the most the panel keeps. "
                                      "Remove one first." % MAX_FEEDS}
    if any((f.get("name") or "").strip().lower() == name.lower() for f in feeds):
        return {"ok": False, "error": "There is already a calendar called \"%s\". Pick another name." % name}
    clean = _clean_link(url)
    for feed in feeds:
        if _fetch_key(_clean_link(feed.get("url"))) == _fetch_key(clean):
            return {"ok": False, "error": "That link is already linked as \"%s\"." % (feed.get("name") or "calendar")}
    checked = check_feed(clean)
    if not checked.get("ok"):
        return checked
    feeds.append({"name": name, "url": clean, "enabled": True})
    cfg["feeds"] = feeds
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}
    return {"ok": True, "name": name, "events_found": checked["events_found"],
            "upcoming_found": checked["upcoming_found"], "next_event": checked["next_event"],
            "feed": _feed_view(feeds[-1], _load_cache())}


def set_feed_enabled(name, enabled):
    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
    hit = 0
    for feed in cfg.get("feeds") or []:
        if isinstance(feed, dict) and (feed.get("name") or "") == name:
            feed["enabled"] = bool(enabled)
            hit += 1
    if not hit:
        return {"ok": False, "error": "There is no linked calendar called \"%s\"." % name}
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}
    return {"ok": True, "name": name, "enabled": bool(enabled)}


def remove_feed(name):
    """Unlink a calendar (every linked calendar with that name) and drop its cached copy.
    Jarvis's own events are not touched."""
    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
    keep, gone = [], []
    for feed in cfg.get("feeds") or []:
        if isinstance(feed, dict) and (feed.get("name") or "") == name:
            gone.append(feed)
        else:
            keep.append(feed)
    if not gone:
        return {"ok": False, "error": "There is no linked calendar called \"%s\"." % name}
    cfg["feeds"] = keep
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}
    still_used = {_fetch_key(f.get("url")) for f in keep if isinstance(f, dict)}
    cache = _load_cache()
    changed = False
    for feed in gone:
        key = _fetch_key(feed.get("url"))
        if key in cache and key not in still_used:
            del cache[key]
            changed = True
    if changed:
        _save_cache(cache)
    return {"ok": True, "removed": name}


def collect_range(start, end, force=False):
    """Every event overlapping [start, end], from every enabled feed and Jarvis's own
    store, sorted. Unlike the model tool this has no 40-event cap. Never raises."""
    cfg = load_config()
    events, problems = [], []
    for feed in cfg.get("feeds") or []:
        if not isinstance(feed, dict) or feed.get("enabled") is False:
            continue
        body, err = fetch_feed(feed, force=force)
        name = feed.get("name") or "calendar"
        if err:
            problems.append("%s: %s" % (name, _scrub(err, feed.get("url"))))
        if not body:
            continue
        for event in parse_ics(body, start, end):
            event["source"] = name
            events.append(event)
    events.extend(e for e in local_events(cfg) if e["end"] >= start and e["start"] <= end)
    events.sort(key=lambda e: (e["start"], e["title"]))
    return events, problems


def _panel_event(ev):
    start, end = ev["start"], ev["end"]
    local = bool(ev.get("local"))
    if local:
        ident = ev.get("uid") or ""
    else:
        seed = "%s|%s|%s" % (ev.get("source") or "", ev.get("uid") or "", start.isoformat())
        ident = "f_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return {
        "id": ident,
        "title": ev.get("title") or "(no title)",
        "start": start.replace(microsecond=0).isoformat(),
        "end": end.replace(microsecond=0).isoformat(),
        "all_day": _is_all_day(start, end),
        "location": ev.get("location") or "",
        "description": ev.get("description") or "",
        "source": "Jarvis calendar" if local else (ev.get("source") or "calendar"),
        "local": local,
        # Only Jarvis's own events can be edited or deleted: a linked calendar is a
        # read-only ICS link, and a hand-edited event with no id has nothing to address.
        "editable": bool(local and _EVENT_ID_RE.match(ident or "")),
    }


def _parse_window_bound(text, label):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("%s is missing." % label)
    try:
        return datetime.fromisoformat(re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", raw) if "T" in raw else raw)
    except ValueError:
        raise ValueError("%s is not a date (use YYYY-MM-DD)." % label)


def window_view(start_text, end_text, force=False):
    """The Agenda / Week / Month views: events overlapping the window, serialized."""
    try:
        start = _parse_window_bound(start_text, "The start")
        end = _parse_window_bound(end_text, "The end")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if end <= start:
        return {"ok": False, "error": "The end of the window must be after its start."}
    if (end - start).days > PANEL_MAX_RANGE_DAYS:
        return {"ok": False, "error": "That is more than %d days; ask for a shorter window." % PANEL_MAX_RANGE_DAYS}
    events, problems = collect_range(start, end, force=force)
    truncated = len(events) > PANEL_MAX_EVENTS
    return {
        "ok": True,
        "start": start.replace(microsecond=0).isoformat(),
        "end": end.replace(microsecond=0).isoformat(),
        "now": datetime.now().replace(microsecond=0).isoformat(),
        "events": [_panel_event(e) for e in events[:PANEL_MAX_EVENTS]],
        "problems": problems,
        "truncated": truncated,
        "feed_count": len([f for f in (load_config().get("feeds") or [])
                           if isinstance(f, dict) and f.get("enabled") is not False]),
    }


def _prune_local(cfg):
    """Drop events that ended more than ``retention_days(cfg)`` ago and keep the newest
    LOCAL_MAX_EVENTS. Shared by the model tool and the panel."""
    cutoff = _retention_cutoff(cfg)
    cfg["local_events"] = [e for e in cfg.get("local_events") or []
                           if isinstance(e, dict)
                           and str(e.get("end") or e.get("start") or "") >= cutoff][-LOCAL_MAX_EVENTS:]
    return cfg


def _retention_cutoff(cfg):
    """ISO time before which a Jarvis event that has ended is dropped."""
    return (datetime.now() - timedelta(days=retention_days(cfg))).isoformat()


def _event_from_fields(fields, base=None):
    """Panel fields -> (stored entry without an id, None) or (None, error message).

    ``base`` is the stored entry being edited: a field the request leaves out keeps its
    old value. A date is YYYY-MM-DD and a time HH:MM; an all-day event is the date at
    midnight to the next midnight (how a linked calendar writes one)."""
    fields = fields if isinstance(fields, dict) else {}
    base = base or {}
    try:
        base_start = datetime.fromisoformat(base["start"]) if base.get("start") else None
        base_end = datetime.fromisoformat(base["end"]) if base.get("end") else None
    except (ValueError, TypeError):
        base_start = base_end = None

    title = (fields["title"] if "title" in fields else base.get("title") or "")
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not title:
        return None, "Give the event a title."

    if "date" in fields:
        try:
            day = datetime.strptime(str(fields["date"]).strip(), "%Y-%m-%d")
        except ValueError:
            return None, "Pick a date (YYYY-MM-DD)."
    elif base_start:
        day = base_start.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        return None, "Pick a date."

    if "all_day" in fields:
        all_day = bool(fields["all_day"])
    else:
        all_day = bool(base_start and base_end and _is_all_day(base_start, base_end))

    if all_day:
        start = day
        end = day + timedelta(days=1)
    else:
        if "time" in fields:
            try:
                clock = datetime.strptime(str(fields["time"]).strip(), "%H:%M")
            except ValueError:
                return None, "Pick a start time (HH:MM)."
        elif base_start and not (base_end and _is_all_day(base_start, base_end)):
            clock = base_start
        else:
            return None, "Pick a start time, or tick All day."
        start = day.replace(hour=clock.hour, minute=clock.minute)
        if "duration_minutes" in fields:
            try:
                minutes = int(fields["duration_minutes"])
            except (TypeError, ValueError):
                return None, "The duration must be a number of minutes."
        elif base_start and base_end and not _is_all_day(base_start, base_end):
            minutes = int((base_end - base_start).total_seconds() // 60)
        else:
            minutes = 60
        end = start + timedelta(minutes=max(5, min(minutes, 1440)))

    location = str(fields["location"] if "location" in fields else base.get("location") or "")[:200]
    description = str(fields["description"] if "description" in fields else base.get("description") or "")[:500]
    keep = retention_days()
    if end < datetime.now() - timedelta(days=keep):
        return None, ("Events older than %d days are removed automatically, so this one "
                      "wouldn't be kept." % keep)
    return {
        "title": title[:200],
        "start": start.replace(microsecond=0).isoformat(),
        "end": end.replace(microsecond=0).isoformat(),
        "location": location.strip(),
        "description": description.strip(),
    }, None


def _event_by_id(cfg, event_id):
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        return None
    for index, entry in enumerate(cfg.get("local_events") or []):
        if isinstance(entry, dict) and entry.get("id") == event_id:
            return index
    return None


def add_local_event(fields):
    entry, error = _event_from_fields(fields)
    if error:
        return {"ok": False, "error": error}
    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
    # Events past their keep time are removed by this very add, so they do not count
    # towards the limit (otherwise a full calendar of old events refuses a new one).
    cutoff = _retention_cutoff(cfg)
    existing = [e for e in cfg.get("local_events") or [] if isinstance(e, dict)]
    live = [e for e in existing if str(e.get("end") or e.get("start") or "") >= cutoff]
    if len(live) >= LOCAL_MAX_EVENTS:
        return {"ok": False, "error": "Jarvis's calendar already holds %d events, the most it keeps. "
                                      "Delete some first." % LOCAL_MAX_EVENTS}
    taken = {e.get("id") for e in existing}
    new_id = "ev_" + secrets.token_hex(4)
    while new_id in taken:
        new_id = "ev_" + secrets.token_hex(4)
    entry["id"] = new_id
    cfg.setdefault("local_events", []).append(entry)
    held = len(cfg["local_events"])
    _prune_local(cfg)
    pruned = held - len(cfg["local_events"])
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}
    out = {"ok": True, "event": _panel_event(_as_event(entry))}
    if pruned:
        # Said, not silent: past events leave when a new one is added.
        out["pruned"] = pruned
    return out


def update_local_event(event_id, fields):
    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
    index = _event_by_id(cfg, event_id)
    if index is None:
        return {"ok": False, "error": "That event isn't in Jarvis's calendar any more "
                                      "(it may have been deleted). Only Jarvis's own events can be edited."}
    old = cfg["local_events"][index]
    entry, error = _event_from_fields(fields, base=old)
    if error:
        return {"ok": False, "error": error}
    entry["id"] = old["id"]
    cfg["local_events"][index] = entry
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}
    return {"ok": True, "event": _panel_event(_as_event(entry))}


def delete_local_event(event_id):
    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
    index = _event_by_id(cfg, event_id)
    if index is None:
        return {"ok": False, "error": "That event isn't in Jarvis's calendar any more "
                                      "(it may have been deleted). Only Jarvis's own events can be deleted."}
    removed = cfg["local_events"].pop(index)
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}
    return {"ok": True, "removed": removed.get("id"), "title": removed.get("title") or ""}


def _as_event(entry):
    """A stored local entry -> the event shape collect_range yields."""
    start = datetime.fromisoformat(entry["start"])
    end = datetime.fromisoformat(entry["end"])
    return {"title": entry.get("title") or "(no title)", "location": entry.get("location") or "",
            "description": entry.get("description") or "", "uid": entry.get("id") or "",
            "start": start, "end": end, "source": "local", "local": True}


ADMIN_OPS = ("feeds", "window", "check", "link", "feed-enable", "feed-remove",
             "event-add", "event-update", "event-delete")


def admin(op, payload=None):
    """The one entry point for the Calendars panel. Always returns a dict with ``ok``;
    a refusal is ``{"ok": False, "error": ...}``, never an exception."""
    why = unattended_reason()
    if why:
        return {"ok": False, "error": why, "code": "unattended"}
    payload = payload if isinstance(payload, dict) else {}
    try:
        if op == "feeds":
            return feeds_view()
        if op == "window":
            return window_view(payload.get("start"), payload.get("end"), force=payload.get("refresh") is True)
        if op == "check":
            return check_feed(payload.get("url"))
        if op == "link":
            return link_feed(payload.get("name"), payload.get("url"))
        if op == "feed-enable":
            return set_feed_enabled(str(payload.get("name") or ""), payload.get("enabled") is True)
        if op == "feed-remove":
            return remove_feed(str(payload.get("name") or ""))
        if op == "event-add":
            return add_local_event(payload.get("event"))
        if op == "event-update":
            return update_local_event(payload.get("id"), payload.get("event"))
        if op == "event-delete":
            return delete_local_event(payload.get("id"))
    except Exception as exc:  # noqa: BLE001 -- a panel call must answer, not crash the page
        return {"ok": False, "error": "Calendar error: %s" % str(exc)[:160]}
    return {"ok": False, "error": "usage: jarvis calendar-admin %s (one JSON object on stdin)" % "|".join(ADMIN_OPS)}


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

    cfg, problem = load_config_strict()
    if cfg is None:
        return {"ok": False, "error": problem}
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
    held = len(cfg["local_events"])
    _prune_local(cfg)
    pruned = held - len(cfg["local_events"])
    if not save_config(cfg):
        return {"ok": False, "error": "Couldn't save calendar.json."}

    out = {"ok": True, "id": entry["id"], "title": title,
           "start": entry["start"], "when": _friendly(start, datetime.now()),
           "note": "Saved to Jarvis's own calendar. Subscribed feeds are read-only."}
    if pruned:
        out["pruned"] = pruned
        out["note"] += " %d older event(s) past the keep limit were removed." % pruned
    return out


def tool_calendar_update_event(args):
    """Change an event on Jarvis's own calendar, through the same function the panel uses."""
    args = args or {}
    event_id = (args.get("id") or "").strip()
    if not event_id:
        return {"needs_clarification": True,
                "message": "Which event? Call calendar_events first: events on Jarvis's own "
                           "calendar carry an id."}
    fields = {}
    for key in ("title", "location", "description"):
        if args.get(key) is not None:
            fields[key] = str(args[key])
    if args.get("duration_minutes") not in (None, ""):
        fields["duration_minutes"] = args["duration_minutes"]
    when = args.get("when") if args.get("when") is not None else args.get("start")
    if when is not None and str(when).strip():
        from .. import timespec
        try:
            start = timespec.parse_when(str(when).strip())
        except timespec.TimeSpecError as exc:
            return {"needs_clarification": True, "message": str(exc)}
        fields["date"] = start.strftime("%Y-%m-%d")
        fields["time"] = start.strftime("%H:%M")
        # An all-day event moved to another day stays all-day; giving a length makes
        # it a timed event at that time. (parse_when has no "no time" answer.)
        cfg = load_config()
        index = _event_by_id(cfg, event_id)
        if index is not None:
            old = cfg["local_events"][index]
            try:
                was_all_day = _is_all_day(datetime.fromisoformat(old["start"]),
                                          datetime.fromisoformat(old["end"]))
            except (KeyError, ValueError, TypeError):
                was_all_day = False
            if was_all_day:
                if "duration_minutes" in fields:
                    fields["all_day"] = False
                else:
                    fields.pop("time")
    if not fields:
        return {"needs_clarification": True, "message": "What should change about it?"}
    result = update_local_event(event_id, fields)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "Couldn't change the event."}
    event = result["event"]
    out = {"ok": True, "id": event["id"], "title": event["title"], "start": event["start"],
           "when": _friendly(datetime.fromisoformat(event["start"]), datetime.now())}
    if event.get("all_day"):
        out["all_day"] = True
    return out


def tool_calendar_delete_event(args):
    """Delete an event from Jarvis's own calendar (the user is asked first: see
    TOOL_CONFIRM_REQUIRED)."""
    args = args or {}
    event_id = (args.get("id") or "").strip()
    if not event_id:
        return {"needs_clarification": True,
                "message": "Which event? Call calendar_events first: events on Jarvis's own "
                           "calendar carry an id."}
    result = delete_local_event(event_id)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "Couldn't delete the event."}
    return {"ok": True, "deleted": result.get("title") or "", "id": result.get("removed")}


TOOL_SCHEMAS = [
    {
        "name": "calendar_events",
        "description": (
            "List upcoming calendar events from the user's subscribed calendars "
            "and Jarvis's own. Use for 'what's on today', 'when is my next "
            "meeting', 'am I free Thursday'. Each event includes "
            "starts_in_seconds, so to set a reminder before one, call this "
            "first and then schedule_task with the computed time. Events on "
            "Jarvis's own calendar (source \"local\") also carry an id."
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
    {
        "name": "calendar_update_event",
        "description": (
            "Change an event on Jarvis's own calendar: its title, day or time, length, "
            "location or description. Needs the event's id from calendar_events (only "
            "events with source \"local\" have one). Send only what changes; moving an "
            "event keeps its length unless duration_minutes is given, and an all-day event "
            "moved to another day stays all-day (give duration_minutes to make it timed). "
            "Events from a subscribed calendar (Google/Outlook/Apple) are read-only and "
            "cannot be changed -- say so instead of trying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The event's id from calendar_events."},
                "title": {"type": "string"},
                "when": {"type": "string", "description": "New start, plain language: 'friday 09:00', 'tomorrow at 3pm'."},
                "duration_minutes": {"type": "number"},
                "location": {"type": "string", "description": "Empty string clears it."},
                "description": {"type": "string", "description": "Empty string clears it."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "calendar_delete_event",
        "description": (
            "Delete an event from Jarvis's own calendar, by the id calendar_events gave it. "
            "Cannot delete an event from a subscribed calendar. The user is asked to confirm "
            "before it runs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The event's id from calendar_events."},
            },
            "required": ["id"],
        },
    },
]

TOOLS = {
    "calendar_events": tool_calendar_events,
    "calendar_add_event": tool_calendar_add_event,
    "calendar_update_event": tool_calendar_update_event,
    "calendar_delete_event": tool_calendar_delete_event,
}

# Deleting is the one step with no undo, so it goes through the ordinary confirmation
# prompt (declared here, not in tool_safety.py -- see tool_loader.py). Adding and changing
# an event on Jarvis's own calendar are easy to put back.
TOOL_CONFIRM_REQUIRED = {"calendar_delete_event"}

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
    "calendar_update_event": {
        "reschedule": 9, "move my meeting": 10, "move the meeting": 10,
        "change my appointment": 10, "change the event": 9, "rename the event": 9,
        "update my calendar": 10, "move my appointment": 10,
    },
    "calendar_delete_event": {
        "delete the event": 10, "remove from my calendar": 10, "delete from my calendar": 10,
        "cancel my appointment": 9, "cancel the meeting": 8, "take it off my calendar": 9,
    },
}

TOOL_PACK_INSTRUCTION = (
    "calendar_events reads subscribed calendars; calendar_add_event writes only to "
    "Jarvis's own. For 'remind me N before my next meeting', call calendar_events "
    "with next_only first, then schedule_task using that event's start time minus N "
    "— don't guess a time. Subscribed feeds cache for 15 minutes; pass refresh only "
    "if the user says something just changed. To change or delete an event, call "
    "calendar_events (use query for the title) to get its id, then calendar_update_event "
    "/ calendar_delete_event. Only events with source \"local\" have an id; an event on a "
    "subscribed calendar can't be changed or deleted, so say that."
)
