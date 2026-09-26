"""Plain-language time parsing for the scheduler — "in 20 minutes", "tomorrow
at 9am", "every weekday at 08:30" -> a normalized trigger dict.

WHY THIS EXISTS AT ALL (instead of making the model emit ISO timestamps)
------------------------------------------------------------------------
The model *can* emit an ISO timestamp, and schedule_task still accepts one.
But asking it to is asking it to do date arithmetic in its head against a
"now" it only knows from a system-prompt string, and it gets that wrong in
exactly the cases that matter most: "in 20 minutes" at 23:52 (rolls the
date), "friday" on a Friday (this one or next?), DST boundaries. A 250-line
deterministic parser is both cheaper and more correct than a tool-call
round trip spent correcting a hallucinated timestamp.

So the contract is: the model passes through whatever the *user* said
("tomorrow morning", "every 30 min"), and this module resolves it against a
real `now`. If it can't, it says so plainly (returns an error the tool turns
into needs_clarification) rather than guessing a time the user never asked
for — a reminder that fires at the wrong hour is worse than one that was
never created, because the user believes it's set.

NO EXTERNAL DEPENDENCY, DELIBERATELY
------------------------------------
dateutil/pendulum would cover more phrasings, but scheduler.py is imported
on the CLI's hot path (every `jarvis ask` drains the notification inbox),
and jarvis's base dependency list is four packages precisely so a fresh
install is fast. stdlib datetime covers everything below.

Everything is NAIVE LOCAL TIME, on purpose. The user says "9am" meaning 9am
where they are; the machine running the tick is the same machine. Storing
UTC would mean converting twice for display and getting "every day at 9am"
subtly wrong across a DST change (a fixed UTC offset drifts by an hour; a
naive local wall-clock time does not, which is the behavior people actually
expect from a daily alarm).

PARSED FORMS
------------
One-shot (`{"type": "at", "at": "<iso>"}`):
    in 20 minutes / in 2h / in 1 hour 30 minutes / in 90s
    at 9am / at 09:00 / 9:30pm / 17:00
    tomorrow / tomorrow at 9am / today at 5pm / tonight
    monday / next friday at 14:00
    noon / midnight / in a minute / in an hour
    2026-09-16T09:00 / 2026-09-16 09:00 / 2026-09-16 (ISO passthrough)

Recurring (`{"type": "every", "every_seconds": N, "at": "<iso first run>"}`):
    every 30 minutes / every 2 hours / every hour
    every day at 9am / daily at 08:00
    every monday at 8 / every weekday at 09:15 / every weekend
    every week / every 3 days
"""

import re
from datetime import datetime, timedelta

# Accepted spellings per unit, longest-first so "minutes" is tried before
# "min" and "m" (a plain `in`-order scan would match "m" inside "minutes"
# and leave "inutes" behind).
_UNIT_SECONDS = [
    ("milliseconds", 0.001), ("millisecond", 0.001), ("ms", 0.001),
    ("seconds", 1), ("second", 1), ("secs", 1), ("sec", 1), ("s", 1),
    ("minutes", 60), ("minute", 60), ("mins", 60), ("min", 60), ("m", 60),
    ("hours", 3600), ("hour", 3600), ("hrs", 3600), ("hr", 3600), ("h", 3600),
    ("days", 86400), ("day", 86400), ("d", 86400),
    ("weeks", 604800), ("week", 604800), ("wks", 604800), ("wk", 604800), ("w", 604800),
    ("months", 2592000), ("month", 2592000),  # 30d — see note in parse_delay
    ("years", 31536000), ("year", 31536000),  # 365d
]

_UNIT_ALTERNATION = "|".join(u for u, _ in _UNIT_SECONDS)

# "2 hours", "2hours", "2 hrs" — one quantity + unit pair. Scanned
# repeatedly so "1 hour 30 minutes" accumulates instead of taking the first
# match and dropping the rest (a real failure mode: "in 1 hour 30 minutes"
# firing in one hour is wrong in a way the user won't notice until it's
# already fired).
_QTY_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + _UNIT_ALTERNATION + r")\b",
    re.IGNORECASE,
)

# Bare "in an hour" / "in a minute" — no digit at all. Mapped to 1 before
# the numeric scan runs.
_ARTICLE_UNIT_RE = re.compile(
    r"\ba[n]?\s+(" + _UNIT_ALTERNATION + r")\b",
    re.IGNORECASE,
)

# Long form first per weekday, so a reverse lookup (number -> name) for
# display picks "Thursday" rather than "Thurs".
_WEEKDAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
                  4: "Friday", 5: "Saturday", 6: "Sunday"}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4,
    "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

# "at 9am", "9:30 pm", "at 17:00", "@ 7". The leading at/@ is optional so a
# bare "tomorrow 9am" parses, but a bare number with no colon and no am/pm
# is rejected below (see _parse_clock) — otherwise "every 3 days" would read
# "3" as 3 o'clock.
_CLOCK_RE = re.compile(
    r"(?:\b(?:at|@)\s*)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b",
    re.IGNORECASE,
)

_NAMED_TIMES = {
    "midnight": (0, 0), "noon": (12, 0), "midday": (12, 0),
    "morning": (9, 0), "afternoon": (14, 0), "evening": (18, 0),
    "night": (21, 0), "tonight": (21, 0), "lunch": (12, 30),
    "lunchtime": (12, 30), "dinner": (19, 0), "dinnertime": (19, 0),
    "breakfast": (8, 0), "eod": (17, 0), "cob": (17, 0),
}

ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


class TimeSpecError(Exception):
    """Raised when a time expression can't be resolved. Callers turn this
    into a needs_clarification result rather than substituting a default —
    see this module's docstring for why guessing is the worse failure."""


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def to_iso(dt):
    """Seconds-precision local ISO. Sub-second precision is noise for a
    scheduler whose tick interval is measured in seconds, and it makes every
    stored record and log line harder to read."""
    return dt.replace(microsecond=0).isoformat()


def from_iso(text):
    """Parse anything to_iso() emits, plus the looser human formats in
    ISO_FORMATS. Tolerates a trailing 'Z' and a '+00:00'-style offset by
    dropping them: everything in this module is naive local time (see the
    module docstring), and datetime.fromisoformat() can't read 'Z' at all
    before Python 3.11, which this package still supports."""
    raw = (text or "").strip()
    if not raw:
        raise TimeSpecError("empty timestamp")
    cleaned = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", raw)
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    for fmt in ISO_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise TimeSpecError("not a recognizable timestamp: %r" % raw)


def parse_delay(text):
    """"2 hours 30 minutes" / "90s" / "an hour" -> float seconds, or None.

    Sums every quantity+unit pair it finds, so compound delays work. Months
    and years are approximated (30d / 365d) — a *delay* of "in 2 months" has
    no exact answer anyway, and anyone who needs a real calendar month wants
    an absolute date, which parse_when handles exactly.
    """
    norm = _norm(text)
    if not norm:
        return None
    # "an hour" -> "1 hour" so the numeric scan below sees it. Done as a
    # rewrite rather than a separate branch so "in an hour and 30 minutes"
    # still accumulates both parts.
    norm = _ARTICLE_UNIT_RE.sub(lambda m: "1 " + m.group(1), norm)
    total, found = 0.0, False
    for qty, unit in _QTY_UNIT_RE.findall(norm):
        for name, secs in _UNIT_SECONDS:
            if name == unit.lower():
                total += float(qty) * secs
                found = True
                break
    return total if found else None


def _parse_clock(text, allow_bare_hour=True):
    """Pull a wall-clock time out of a phrase -> (hour, minute) or None.

    allow_bare_hour=False rejects a lone number with no ':' and no am/pm.
    That guard is what keeps "every 3 days" from being read as "3 o'clock,
    daily" — in a recurring expression a bare integer is overwhelmingly a
    count, not an hour, and silently treating it as 3am would produce a job
    that looks fine in the list and fires at the wrong time forever.
    """
    for word, (hh, mm) in _NAMED_TIMES.items():
        if re.search(r"\b" + re.escape(word) + r"\b", text):
            return hh, mm
    for match in _CLOCK_RE.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        meridiem = (match.group(3) or "").replace(".", "").lower()
        explicit = bool(match.group(2)) or bool(meridiem) or bool(
            re.match(r"\s*(?:at|@)", match.group(0), re.IGNORECASE)
        )
        if not explicit and not allow_bare_hour:
            continue
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def _next_weekday(now, weekday, hour, minute, force_next=False):
    """The next occurrence of a weekday at a given time.

    force_next ("next friday") always skips to the following week, even on
    a Friday. Without it, "friday at 5pm" said on Friday morning means
    today — which is what people mean, and the reason this isn't just
    "always +7 if same day".
    """
    days_ahead = (weekday - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if force_next and days_ahead == 0:
        candidate += timedelta(days=7)
    elif candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def parse_when(text, now=None):
    """Resolve a one-shot time expression to an absolute datetime.

    Raises TimeSpecError if nothing recognizable is in `text`.
    """
    now = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        raise TimeSpecError("no time given")

    # ISO / explicit dates first: an exact timestamp should never be
    # reinterpreted by the fuzzy branches below.
    try:
        return from_iso(raw)
    except TimeSpecError:
        pass

    norm = _norm(raw)

    # Relative: "in 20 minutes". Requires the leading "in" (or a bare
    # duration like "20 minutes" / "5min") — but NOT if the phrase also
    # names a weekday or a day word, since "monday in 2 weeks" is a date
    # expression that happens to contain a duration.
    if not re.search(r"\b(today|tomorrow|tonight|yesterday|next|this)\b", norm) and \
            not any(re.search(r"\b" + d + r"\b", norm) for d in _WEEKDAYS):
        delay = parse_delay(norm)
        if delay is not None and (norm.startswith("in ") or _QTY_UNIT_RE.match(norm)):
            if delay <= 0:
                raise TimeSpecError("that delay is zero or negative")
            return (now + timedelta(seconds=delay)).replace(microsecond=0)

    clock = _parse_clock(norm)

    if re.search(r"\btomorrow\b", norm):
        hh, mm = clock if clock else (9, 0)
        return (now + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)

    if re.search(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", norm):
        hh, mm = clock if clock else (_NAMED_TIMES["tonight"] if "tonight" in norm else (9, 0))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            # "today at 9am" said at 3pm can't mean today. Rolling to
            # tomorrow is the only reading that produces a real future
            # time, and it's what an alarm clock does.
            candidate += timedelta(days=1)
        return candidate

    for name, weekday in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", norm):
            hh, mm = clock if clock else (9, 0)
            force_next = bool(re.search(r"\bnext\b", norm))
            return _next_weekday(now, weekday, hh, mm, force_next)

    if clock:
        candidate = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)  # "at 8" after 8 means tomorrow
        return candidate

    raise TimeSpecError(
        "couldn't read %r as a time — try 'in 20 minutes', 'tomorrow at 9am', "
        "or an exact '2026-09-16 14:30'" % raw
    )


def parse_every(text, now=None):
    """Resolve a recurring expression -> (interval_seconds, first_run dt).

    Returns None if `text` isn't a recurring expression at all, so callers
    can fall through to parse_when. Raises TimeSpecError only when it IS
    recurring but unreadable ("every purple").
    """
    now = now or datetime.now()
    norm = _norm(text)
    if not norm:
        return None

    # Ordinal ("first monday of the month") is checked before everything else
    # because it doesn't start with "every" and would otherwise fall straight
    # through to parse_when and be read as a one-off next-Monday.
    ordinal = parse_ordinal(norm, now)
    if ordinal:
        interval, first, rule = ordinal
        rule.update(parse_exceptions(norm))
        return interval, first, None, rule

    if re.match(r"^(daily|hourly|weekly|monthly|nightly)\b", norm):
        norm = {
            "daily": "every day", "hourly": "every hour", "weekly": "every week",
            "monthly": "every month", "nightly": "every day at 9pm",
        }[norm.split()[0]] + norm[len(norm.split()[0]):]

    if not re.match(r"^(every|each)\b", norm):
        return None

    body = re.sub(r"^(every|each)\s+", "", norm)
    exceptions = parse_exceptions(norm)
    # The exception clause is REMOVED from the body before any cadence
    # matching runs. Without this, "every day at 8 except weekends" sees the
    # word "weekends" in the body and matches the weekend branch — producing
    # a job that fires ONLY at weekends, i.e. the exact opposite of what was
    # asked for. A silently inverted schedule is the worst failure this
    # module can produce, since it looks like it worked.
    body = _EXCEPT_RE.sub("", body).strip()
    clock = _parse_clock(body, allow_bare_hour=False)

    # "every other tuesday" / "biweekly" — a weekly cadence with a phase.
    if _EVERY_OTHER_RE.search(norm):
        for name, weekday in _WEEKDAYS.items():
            if re.search(r"\b" + name + r"\b", body):
                hh, mm = clock if clock else (9, 0)
                first = _next_weekday(now, weekday, hh, mm)
                rule = {"every_n_weeks": 2}
                rule.update(exceptions)
                return 604800 * 2, first, None, rule
        # "every other day"
        if re.search(r"\bdays?\b", body):
            hh, mm = clock if clock else (9, 0)
            first = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if first <= now:
                first += timedelta(days=1)
            return 86400 * 2, first, None, exceptions or None

    # "every monday and thursday at 18:00" — more than one named weekday.
    named = sorted({wd for name, wd in _WEEKDAYS.items()
                    if re.search(r"\b" + name + r"\b", body)})
    if len(named) > 1:
        hh, mm = clock if clock else (9, 0)
        first = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while first <= now or first.weekday() not in named:
            first += timedelta(days=1)
        rule = {"only_days": named}
        rule.update(exceptions)
        return 86400, first, None, rule

    # "every weekday" / "every weekend" — a per-weekday recurrence the
    # fixed-interval model can't express (Mon-Fri isn't a constant gap), so
    # it's stored as daily and the skip is applied at fire time by
    # scheduler._weekday_filter. Flagged here via the returned marker.
    if re.search(r"\bweekday(s)?\b", body):
        hh, mm = clock if clock else (9, 0)
        first = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while first <= now or first.weekday() > 4:
            first += timedelta(days=1)
        return 86400, first, "weekday", exceptions or None
    if re.search(r"\bweekend(s)?\b", body):
        hh, mm = clock if clock else (10, 0)
        first = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while first <= now or first.weekday() < 5:
            first += timedelta(days=1)
        return 86400, first, "weekend", exceptions or None

    for name, weekday in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", body):
            hh, mm = clock if clock else (9, 0)
            return 604800, _next_weekday(now, weekday, hh, mm), None, exceptions or None

    interval = parse_delay(body)
    if interval is None:
        # "every day at 9am" — no quantity, just a bare unit.
        bare = re.match(r"^(" + _UNIT_ALTERNATION + r")\b", body)
        if bare:
            for name, secs in _UNIT_SECONDS:
                if name == bare.group(1):
                    interval = float(secs)
                    break
    if interval is None:
        raise TimeSpecError(
            "couldn't read %r as a repeat — try 'every 30 minutes' or "
            "'every day at 9am'" % text
        )
    if interval < 30:
        # A 5-second repeat would spawn faster than a tick can finish it and
        # is far more likely a typo ("every 5" meaning minutes) than intent.
        raise TimeSpecError("repeat interval must be at least 30 seconds")

    if clock and interval >= 86400:
        # "every day at 9am" — anchor the first run to that wall clock.
        first = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if first <= now:
            first += timedelta(seconds=interval)
    else:
        first = (now + timedelta(seconds=interval)).replace(microsecond=0)
    return interval, first, None, exceptions or None


def parse_trigger(text, now=None):
    """The one entry point scheduler.py actually calls.

    "every 30 minutes" -> {"type": "every", "every_seconds": 1800, "at": ...}
    "tomorrow at 9am"  -> {"type": "at", "at": "..."}

    Raises TimeSpecError on anything unreadable, so a caller never silently
    schedules something for a time nobody asked for.
    """
    now = now or datetime.now()
    recurring = parse_every(text, now)
    if recurring:
        # parse_every returns 4 values now (a `rule` was appended); the
        # 3-tuple form is still accepted so anything that calls it directly
        # and predates this keeps working.
        if len(recurring) == 4:
            interval, first, day_filter, rule = recurring
        else:
            interval, first, day_filter = recurring
            rule = None
        trigger = {"type": "every", "every_seconds": int(interval), "at": to_iso(first)}
        if day_filter:
            trigger["only_on"] = day_filter
        if rule:
            trigger["rule"] = rule
        return trigger
    return {"type": "at", "at": to_iso(parse_when(text, now))}


def describe(trigger, next_run=None):
    """Render a stored trigger back to a short human string, for `jarvis
    sched-list`, the web panel, and the model's own list_scheduled result —
    all three showed raw ISO before this existed, which reads terribly in a
    chat reply ("your reminder is set for 2026-09-16T09:00:00").

    `next_run`, if given, is the job's persisted next-fire time (the same
    value due_jobs()/tick() actually act on) and takes priority over the
    trigger's own `at` for anything that shows a concrete date/time.

    THIS PARAMETER IS THE L.4 FIX. `trigger["at"]` is written once at job
    creation (normalize_trigger) and, for a one-shot job, again by snooze() —
    but resume()'s roll-forward and every ordinary _advance() at fire time
    only ever update `next_run`, never `trigger["at"]`. Left to read
    `trigger["at"]` alone, a recurring job's displayed "next ..." time goes
    stale the moment it's paused/resumed or misses a run and catches up,
    while the job keeps firing correctly against the real `next_run` — the
    exact owner-reported symptom (displayed 3:00 AM, actually fires hours
    later) once a job has been through any of those paths. Passing next_run
    here, rather than chasing every future write site that changes it, is
    what keeps the display honest without touching any firing/mutation
    logic at all.
    """
    if not isinstance(trigger, dict):
        return str(trigger)
    ttype = trigger.get("type")
    if ttype == "at":
        return "at " + _friendly(next_run or trigger.get("at"))
    if ttype == "every":
        every = human_duration(trigger.get("every_seconds") or 0)
        only = trigger.get("only_on")
        at = next_run or trigger.get("at")
        base = "every " + every
        if only:
            base = "every %s" % ("weekday" if only == "weekday" else "weekend day")
        rule_text = describe_rule(trigger.get("rule"))
        if rule_text:
            base = rule_text if trigger.get("rule", {}).get("ordinal") is not None \
                else base + " (" + rule_text + ")"
        if at:
            try:
                base += ", next " + _friendly(at)
            except TimeSpecError:
                pass
        return base
    if ttype == "event":
        return "when '%s' happens" % trigger.get("event", "?")
    if ttype == "startup":
        return "on next startup"
    return str(ttype or "?")


def _friendly(iso):
    """'2026-09-16T09:00:00' -> 'tomorrow 09:00' / 'Tue 16 Sep 09:00'."""
    if not iso:
        return "?"
    try:
        dt = from_iso(iso)
    except TimeSpecError:
        return str(iso)
    now = datetime.now()
    today = now.date()
    delta_days = (dt.date() - today).days
    clock = dt.strftime("%H:%M")
    if delta_days == 0:
        return "today " + clock
    if delta_days == 1:
        return "tomorrow " + clock
    if 0 < delta_days < 7:
        return dt.strftime("%a ") + clock
    return dt.strftime("%a %d %b ") + clock


def human_duration(seconds):
    """3900 -> '1h 5m'. Used in every list/describe surface."""
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    if seconds < 0:
        return "overdue"
    if seconds < 60:
        return "%ds" % seconds
    parts, units = [], (("d", 86400), ("h", 3600), ("m", 60))
    for label, size in units:
        if seconds >= size:
            parts.append("%d%s" % (seconds // size, label))
            seconds %= size
        if len(parts) == 2:
            break
    return " ".join(parts) or "0m"


# ===========================================================================
# RICHER RECURRENCE — exceptions, ordinals, intervals
#
# parse_every() above covers the fixed-interval cases plus the two per-weekday
# ones (weekday/weekend) that a constant gap can't express. What it can't say:
#
#     every weekday at 9am except holidays
#     first monday of the month at 10:00
#     every other tuesday
#     every day at 8 except weekends
#     last friday of the month
#     every monday and thursday at 18:00
#
# All of these are the same shape: a base cadence plus a FILTER applied at
# fire time. The scheduler already does exactly that for weekday/weekend via
# trigger["only_on"], so this extends that mechanism rather than inventing a
# parallel one — a trigger gains an optional "rule" block, and
# scheduler._weekday_filter's call site consults it.
#
# WHY NOT RRULE
# -------------
# iCalendar's RRULE covers all of this and much more. It also needs
# dateutil, which this module deliberately doesn't have (see the module
# docstring), and it is unreadable — "FREQ=MONTHLY;BYDAY=1MO" is not
# something a user will recognise in `sched-list`. The set below is what
# people actually ask for, spelled the way they ask for it.
#
# HOLIDAYS ARE DATA
# -----------------
# "except holidays" needs a list of holidays, and there is no correct
# built-in answer — they differ by country, by company, and by person.
# So it reads ~/.jarvis/holidays.json, a plain list of YYYY-MM-DD strings,
# and a rule referencing holidays when that file is empty simply never skips
# anything. Failing open is right here: a reminder that fires on a holiday is
# a minor annoyance, one that silently never fires is a missed appointment.
# ===========================================================================

import json as _json
from pathlib import Path as _Path

HOLIDAYS_FILE = _Path.home() / ".jarvis" / "holidays.json"

_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1,
}

_EXCEPT_RE = re.compile(
    r"\b(?:except|excluding|but not|skip(?:ping)?|other than)\s+(?:on\s+)?(.+)$",
    re.IGNORECASE,
)

_ORDINAL_RE = re.compile(
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\s+"
    r"(monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|"
    r"friday|fri|saturday|sat|sunday|sun)\b(?:\s+(?:of|in)\s+(?:the\s+)?month)?",
    re.IGNORECASE,
)

_EVERY_OTHER_RE = re.compile(
    r"\b(?:every\s+other|alternate|biweekly|fortnightly)\b", re.IGNORECASE)


def load_holidays():
    """YYYY-MM-DD strings from ~/.jarvis/holidays.json. Never raises."""
    try:
        data = _json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return set()
    if isinstance(data, dict):
        data = data.get("holidays") or data.get("dates") or []
    if not isinstance(data, list):
        return set()
    out = set()
    for entry in data:
        if isinstance(entry, str) and len(entry) >= 10:
            out.add(entry[:10])
        elif isinstance(entry, dict) and entry.get("date"):
            out.add(str(entry["date"])[:10])
    return out


def parse_exceptions(text):
    """The 'except ...' half of a recurrence -> a filter dict.

    Returns {} when there's no exception clause, so a caller can merge
    unconditionally.
    """
    match = _EXCEPT_RE.search(text or "")
    if not match:
        return {}
    body = match.group(1).lower()
    rule = {}

    if re.search(r"\bholidays?\b|\bbank holidays?\b|\bpublic holidays?\b", body):
        rule["skip_holidays"] = True
    if re.search(r"\bweekends?\b", body):
        rule["skip_weekends"] = True
    if re.search(r"\bweekdays?\b", body):
        rule["skip_weekdays"] = True

    days = []
    for name, weekday in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", body) and weekday not in days:
            days.append(weekday)
    if days:
        rule["skip_days"] = sorted(days)

    # "except the 1st", "except 25 december" — explicit dates.
    dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", body)
    if dates:
        rule["skip_dates"] = sorted(set(dates))

    return rule


def parse_ordinal(text, now=None):
    """'first monday of the month at 10:00' -> (interval, first_run, rule).

    Returns None if the text isn't an ordinal expression. The interval is
    nominal (roughly a month); the rule is what actually decides whether a
    given fire is the right one, because "the first Monday" is 28-35 days
    apart depending on the month and no fixed interval expresses it.
    """
    now = now or datetime.now()
    match = _ORDINAL_RE.search(_norm(text))
    if not match:
        return None
    ordinal = _ORDINALS[match.group(1).lower()]
    weekday = _WEEKDAYS[match.group(2).lower()]
    clock = _parse_clock(_norm(text), allow_bare_hour=False) or (9, 0)

    first = _next_ordinal_weekday(now, ordinal, weekday, clock[0], clock[1])
    rule = {"ordinal": ordinal, "weekday": weekday}
    # Daily nominal interval: the scheduler advances a day at a time and the
    # rule rejects every day that isn't the nth weekday, which is both simpler
    # and more robust than trying to compute the next occurrence arithmetically
    # across month boundaries and DST.
    return 86400, first, rule


def _next_ordinal_weekday(now, ordinal, weekday, hour, minute):
    """Next datetime that is the nth <weekday> of its month, after `now`."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    # At most ~70 days covers this month and the next, which is always enough
    # for any ordinal from 1st to 5th or "last".
    for _ in range(70):
        if _is_ordinal_weekday(candidate, ordinal, weekday):
            return candidate
        candidate += timedelta(days=1)
    return candidate


def _is_ordinal_weekday(dt, ordinal, weekday):
    if dt.weekday() != weekday:
        return False
    if ordinal == -1:
        # "last": no same weekday left in this month.
        return (dt + timedelta(days=7)).month != dt.month
    return (dt.day - 1) // 7 + 1 == ordinal


def matches_rule(dt, rule, anchor=None):
    """Should a job whose trigger carries `rule` actually fire at `dt`?

    Called by the scheduler at fire time, alongside the existing only_on
    check. Returns True when there is no rule, so every existing job is
    unaffected.
    """
    if not isinstance(rule, dict) or not rule:
        return True

    if rule.get("skip_weekends") and dt.weekday() >= 5:
        return False
    if rule.get("skip_weekdays") and dt.weekday() < 5:
        return False
    if dt.weekday() in (rule.get("skip_days") or []):
        return False
    if dt.strftime("%Y-%m-%d") in set(rule.get("skip_dates") or []):
        return False
    if rule.get("skip_holidays") and dt.strftime("%Y-%m-%d") in load_holidays():
        return False

    only_days = rule.get("only_days")
    if only_days and dt.weekday() not in only_days:
        return False

    ordinal = rule.get("ordinal")
    if ordinal is not None:
        if not _is_ordinal_weekday(dt, ordinal, rule.get("weekday", dt.weekday())):
            return False

    interval_weeks = rule.get("every_n_weeks")
    if interval_weeks and interval_weeks > 1:
        # Counted from the anchor (the job's first run), so "every other
        # Tuesday" stays on the same fortnightly phase forever instead of
        # drifting whenever a run is skipped for another reason.
        if anchor:
            try:
                base = from_iso(anchor) if isinstance(anchor, str) else anchor
                weeks = (dt.date() - base.date()).days // 7
                if weeks % interval_weeks != 0:
                    return False
            except (TimeSpecError, AttributeError, TypeError):
                pass
    return True


def describe_rule(rule):
    """Plain-language rendering, for sched-list and the web panel."""
    if not isinstance(rule, dict) or not rule:
        return ""
    bits = []
    ordinal = rule.get("ordinal")
    if ordinal is not None:
        label = {1: "first", 2: "second", 3: "third", 4: "fourth",
                 5: "fifth", -1: "last"}.get(ordinal, str(ordinal))
        bits.append("%s %s of the month"
                    % (label, _WEEKDAY_NAMES.get(rule.get("weekday"), "day").lower()))
    if rule.get("every_n_weeks", 1) > 1:
        bits.append("every %d weeks" % rule["every_n_weeks"])
    only = rule.get("only_days")
    if only:
        bits.append("on " + ", ".join(_WEEKDAY_NAMES.get(d, "?") for d in only))
    skips = []
    if rule.get("skip_holidays"):
        skips.append("holidays")
    if rule.get("skip_weekends"):
        skips.append("weekends")
    if rule.get("skip_weekdays"):
        skips.append("weekdays")
    if rule.get("skip_days"):
        skips.extend(_WEEKDAY_NAMES.get(d, "?") for d in rule["skip_days"])
    if rule.get("skip_dates"):
        skips.append("%d specific date(s)" % len(rule["skip_dates"]))
    if skips:
        bits.append("except " + ", ".join(skips))
    return ", ".join(bits)
