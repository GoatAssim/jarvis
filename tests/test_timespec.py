"""Tests for jarvis/timespec.py — the scheduler's time-expression parser.

Run: python3 ../tests/test_timespec.py   (from jarvis-cli/, like the others)

Everything is evaluated against a FIXED `now` (Tuesday 2026-09-15 14:30) so
these never depend on when they're run — a scheduler test suite that passes
in the morning and fails at 23:55 is worse than no suite at all.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import timespec  # noqa: E402

PASS, FAIL = [], []

# Tuesday.
NOW = datetime(2026, 9, 15, 14, 30, 0)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def at(expr):
    """Resolve a one-shot expression to a datetime."""
    trigger = timespec.parse_trigger(expr, NOW)
    assert trigger["type"] == "at", trigger
    return timespec.from_iso(trigger["at"])


def every(expr):
    trigger = timespec.parse_trigger(expr, NOW)
    assert trigger["type"] == "every", trigger
    return trigger


def test_relative_delays():
    check("'in 20 minutes' is 20 minutes out", at("in 20 minutes") == datetime(2026, 9, 15, 14, 50))
    check("'in 2h' understands the short unit", at("in 2h") == datetime(2026, 9, 15, 16, 30))
    check("'in 90s' understands seconds", at("in 90s") == datetime(2026, 9, 15, 14, 31, 30))
    # The compound case is the one that silently half-works if the parser
    # takes the first match and stops.
    check("'in 1 hour 30 minutes' sums BOTH parts",
          at("in 1 hour 30 minutes") == datetime(2026, 9, 15, 16, 0),
          str(at("in 1 hour 30 minutes")))
    check("'in an hour' handles the bare article", at("in an hour") == datetime(2026, 9, 15, 15, 30))
    check("parse_delay returns seconds", timespec.parse_delay("2 hours") == 7200)
    check("parse_delay on non-duration text returns None", timespec.parse_delay("banana") is None)


def test_clock_times():
    check("'at 9am' is tomorrow, since 9am already passed today",
          at("at 9am") == datetime(2026, 9, 16, 9, 0))
    check("'9:30pm' is still today", at("9:30pm") == datetime(2026, 9, 15, 21, 30))
    check("'17:00' 24-hour form works", at("17:00") == datetime(2026, 9, 15, 17, 0))
    check("'noon' rolls to tomorrow (12:00 < 14:30)", at("noon") == datetime(2026, 9, 16, 12, 0))
    check("'midnight' means 00:00 tomorrow", at("midnight") == datetime(2026, 9, 16, 0, 0))
    # 12am/12pm are the classic off-by-twelve bug.
    check("12am is midnight, not noon", at("12am").hour == 0)
    check("12pm is noon, not midnight", at("12pm").hour == 12)


def test_day_words():
    check("'tomorrow at 9am' is the next day", at("tomorrow at 9am") == datetime(2026, 9, 16, 9, 0))
    check("bare 'tomorrow' defaults to 9am", at("tomorrow") == datetime(2026, 9, 16, 9, 0))
    check("'today at 5pm' stays today", at("today at 5pm") == datetime(2026, 9, 15, 17, 0))
    # "today at 9am" said at 14:30 can't mean today; rolling forward is the
    # only reading that yields a real future time.
    check("'today at 9am' in the afternoon rolls to tomorrow",
          at("today at 9am") == datetime(2026, 9, 16, 9, 0))
    check("'tonight' is 21:00 today", at("tonight") == datetime(2026, 9, 15, 21, 0))


def test_weekdays():
    # NOW is a Tuesday (weekday 1).
    check("'monday at 8am' is next Monday", at("monday at 8am") == datetime(2026, 9, 21, 8, 0))
    check("'friday at 14:00' is this Friday", at("friday at 14:00") == datetime(2026, 9, 18, 14, 0))
    check("a weekday expression lands on that weekday", at("saturday").weekday() == 5)


def test_iso_passthrough():
    check("ISO with T parses exactly", at("2026-09-16T09:00") == datetime(2026, 9, 16, 9, 0))
    check("ISO with a space parses exactly", at("2026-09-16 09:00") == datetime(2026, 9, 16, 9, 0))
    check("a bare date is midnight", at("2026-09-20") == datetime(2026, 9, 20, 0, 0))
    # An exact timestamp must never be reinterpreted by the fuzzy branches.
    check("ISO wins over fuzzy parsing", at("2026-12-25 07:00") == datetime(2026, 12, 25, 7, 0))
    # from_iso has to tolerate the offsets to_iso never writes but other
    # sources do, since Python < 3.11 can't read a trailing Z at all.
    check("trailing Z is tolerated", timespec.from_iso("2026-09-16T09:00:00Z") == datetime(2026, 9, 16, 9, 0))
    check("a +00:00 offset is tolerated",
          timespec.from_iso("2026-09-16T09:00:00+00:00") == datetime(2026, 9, 16, 9, 0))


def test_recurring():
    check("'every 30 minutes' is a 1800s interval", every("every 30 minutes")["every_seconds"] == 1800)
    check("'every hour' with no quantity still parses", every("every hour")["every_seconds"] == 3600)
    check("'every 2 hours' first run is 2h out",
          timespec.from_iso(every("every 2 hours")["at"]) == datetime(2026, 9, 15, 16, 30))
    daily = every("every day at 9am")
    check("'every day at 9am' is a daily interval", daily["every_seconds"] == 86400)
    check("'every day at 9am' anchors to 09:00 tomorrow",
          timespec.from_iso(daily["at"]) == datetime(2026, 9, 16, 9, 0))
    check("'daily at 08:00' is the same thing said differently",
          every("daily at 08:00")["every_seconds"] == 86400)
    check("'every monday at 8' is weekly", every("every monday at 8")["every_seconds"] == 604800)
    check("'every 3 days' is three days, NOT 3 o'clock daily",
          every("every 3 days")["every_seconds"] == 259200,
          str(every("every 3 days")))


def test_weekday_filters():
    weekday = every("every weekday at 09:15")
    check("'every weekday' carries an only_on filter", weekday.get("only_on") == "weekday")
    check("'every weekday' first run is a Mon-Fri day",
          timespec.from_iso(weekday["at"]).weekday() <= 4)
    weekend = every("every weekend")
    check("'every weekend' carries its own filter", weekend.get("only_on") == "weekend")
    check("'every weekend' first run is Sat or Sun",
          timespec.from_iso(weekend["at"]).weekday() >= 5)


def test_rejections():
    # Guessing a time nobody asked for is the worse failure — a reminder
    # that fires at the wrong hour beats one that was never created only in
    # the sense that the user doesn't know either happened.
    for bad in ("", "banana", "when the cows come home", "every purple"):
        try:
            timespec.parse_trigger(bad, NOW)
            check(f"{bad!r} is rejected", False, "parsed instead of raising")
        except timespec.TimeSpecError:
            check(f"{bad!r} is rejected rather than guessed", True)
    try:
        timespec.parse_trigger("every 5 seconds", NOW)
        check("a sub-30s repeat is rejected", False, "accepted")
    except timespec.TimeSpecError:
        check("a sub-30s repeat is rejected", True)


def test_presentation():
    check("human_duration renders compound", timespec.human_duration(3900) == "1h 5m",
          timespec.human_duration(3900))
    check("human_duration handles sub-minute", timespec.human_duration(45) == "45s")
    check("describe renders a repeat readably",
          "every" in timespec.describe({"type": "every", "every_seconds": 1800, "at": None}))
    check("describe renders an event trigger",
          timespec.describe({"type": "event", "event": "backup_done"}) == "when 'backup_done' happens")
    check("describe renders a startup trigger",
          timespec.describe({"type": "startup"}) == "on next startup")


for fn in [
    test_relative_delays, test_clock_times, test_day_words, test_weekdays,
    test_iso_passthrough, test_recurring, test_weekday_filters,
    test_rejections, test_presentation,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
