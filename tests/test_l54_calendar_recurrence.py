"""Master plan L.54 -- repeating events and time zones in a linked calendar feed.

actions/calendar_tools.py expands a repeating event (RRULE) across the window being drawn.
tests/test_l54_calendar.py never exercised that, and these things were wrong:

  1. a daily event older than ~800 days (or a weekly Mon/Wed one older than ~400 weeks)
     vanished completely;
  2. monthly and yearly repeats stepped a fixed 30 / 365 days, so "the 15th" drifted and a
     birthday on 20 April was drawn on the 19th;
  3. "first Monday" / "last Friday" repeats landed on the wrong days;
  4. a crossed-out occurrence (EXDATE) was still drawn, and a moved one (a VEVENT with the
     same UID and a RECURRENCE-ID) was drawn twice;
  5. a time with a time-zone name (TZID=) was shown at face value, so it was wrong whenever the
     feed's zone differed from the PC's; Outlook's Windows zone names were not understood;
  6. a date-only UNTIL dropped the last day;
  7. a VALARM's own DESCRIPTION overwrote the event's;
  8. a feed with no declared charset came out as mojibake;
  9. the legacy `calendar-add` / `calendar-list` / `calendar-remove` commands skipped every
     safeguard the panel has.

Run: python3 tests/test_l54_calendar_recurrence.py   (no key, no network)
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l54r-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID",
             "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)

_ROOT = Path(__file__).resolve().parent.parent
_JARVIS_CLI = _ROOT / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis.actions import calendar_tools as cal  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
CONFIG = JDIR / "calendar.json"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else (f"  -- {detail}" if detail else "")))


def ics(*events):
    return ("BEGIN:VCALENDAR\r\n"
            + "".join("BEGIN:VEVENT\r\n" + e + "END:VEVENT\r\n" for e in events)
            + "END:VCALENDAR\r\n")


def days(events, fmt="%Y-%m-%d"):
    return [e["start"].strftime(fmt) for e in events]


def stamps(events):
    return [e["start"].strftime("%Y-%m-%d %H:%M") for e in events]


def parse(text, ws, we):
    return cal.parse_ics(text, ws, we)


def zone_available(name):
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 -- no zoneinfo, or no tz database on this machine
        return False


def test_old_series_are_found():
    ws, we = datetime(2026, 10, 1), datetime(2026, 10, 8)
    daily = ics("UID:a\r\nSUMMARY:Daily\r\nDTSTART:20230101T090000\r\nDTEND:20230101T093000\r\nRRULE:FREQ=DAILY\r\n")
    got = parse(daily, ws, we)
    check("a daily event from 2023 is still drawn", len(got) == 7 and days(got)[0] == "2026-10-01", days(got))
    check("...at its own time of day", all(e["start"].hour == 9 and e["start"].minute == 0 for e in got))
    check("...with its own length", all(e["end"] - e["start"] == timedelta(minutes=30) for e in got))
    weekly = ics("UID:b\r\nSUMMARY:Old\r\nDTSTART:20170102T090000\r\nDTEND:20170102T100000\r\n"
                 "RRULE:FREQ=WEEKLY;BYDAY=MO,WE\r\n")
    got = parse(weekly, ws, we)
    check("a Mon/Wed event from 2017 is still drawn", days(got) == ["2026-10-05", "2026-10-07"], days(got))
    every2 = ics("UID:c\r\nSUMMARY:Fortnight\r\nDTSTART:20200106T090000\r\nDTEND:20200106T100000\r\n"
                 "RRULE:FREQ=WEEKLY;INTERVAL=2\r\n")
    got = parse(every2, datetime(2026, 10, 1), datetime(2026, 11, 1))
    gaps = {(b["start"] - a["start"]).days for a, b in zip(got, got[1:])}
    check("an every-other-week series keeps its rhythm after years", gaps == {14} and len(got) >= 2, days(got))
    check("...and lands on the series' own weekday", all(e["start"].weekday() == 0 for e in got))
    count = ics("UID:d\r\nSUMMARY:Ten\r\nDTSTART:20250101T090000\r\nDTEND:20250101T100000\r\nRRULE:FREQ=DAILY;COUNT=10\r\n")
    check("COUNT is counted from the start of the series, so a finished one stays finished",
          parse(count, ws, we) == [])
    check("...and an unfinished one still shows", len(parse(count, datetime(2025, 1, 1), datetime(2025, 1, 31))) == 10)


def test_months_and_years_are_real_months_and_years():
    ws, we = datetime(2026, 10, 1), datetime(2026, 12, 31, 23, 59)
    monthly = ics("UID:m\r\nSUMMARY:Rent\r\nDTSTART:20260115T100000\r\nDTEND:20260115T110000\r\nRRULE:FREQ=MONTHLY\r\n")
    check("a monthly repeat on the 15th stays on the 15th",
          days(parse(monthly, ws, we)) == ["2026-10-15", "2026-11-15", "2026-12-15"], days(parse(monthly, ws, we)))
    on31 = ics("UID:m31\r\nSUMMARY:End\r\nDTSTART:20260131T100000\r\nDTEND:20260131T110000\r\nRRULE:FREQ=MONTHLY\r\n")
    check("one on the 31st skips the months that have no 31st (RFC 5545)",
          days(parse(on31, ws, we)) == ["2026-10-31", "2026-12-31"], days(parse(on31, ws, we)))
    yearly = ics("UID:y\r\nSUMMARY:Birthday\r\nDTSTART;VALUE=DATE:20200420\r\nRRULE:FREQ=YEARLY\r\n")
    got = parse(yearly, datetime(2026, 4, 1), datetime(2026, 5, 1))
    check("a yearly all-day repeat stays on its date (it drifted to the 19th)", days(got) == ["2026-04-20"], days(got))
    check("...and is still an all-day event, one day long",
          got and got[0]["end"] - got[0]["start"] == timedelta(days=1))
    leap = ics("UID:l\r\nSUMMARY:Leap\r\nDTSTART;VALUE=DATE:20240229\r\nRRULE:FREQ=YEARLY\r\n")
    check("a 29 February repeat is only drawn in leap years",
          parse(leap, datetime(2026, 2, 1), datetime(2026, 3, 31)) == []
          and days(parse(leap, datetime(2028, 2, 1), datetime(2028, 3, 31))) == ["2028-02-29"])
    every3 = ics("UID:q\r\nSUMMARY:Quarter\r\nDTSTART:20260105T090000\r\nDTEND:20260105T100000\r\n"
                 "RRULE:FREQ=MONTHLY;INTERVAL=3\r\n")
    check("INTERVAL counts months", days(parse(every3, datetime(2026, 1, 1), datetime(2026, 12, 31, 23, 59)))
          == ["2026-01-05", "2026-04-05", "2026-07-05", "2026-10-05"])


def test_nth_weekday_rules():
    ws, we = datetime(2026, 10, 1), datetime(2026, 12, 31, 23, 59)
    first_mon = ics("UID:f\r\nSUMMARY:First\r\nDTSTART:20260105T090000\r\nDTEND:20260105T100000\r\n"
                    "RRULE:FREQ=MONTHLY;BYDAY=1MO\r\n")
    check("'first Monday' lands on the first Monday",
          days(parse(first_mon, ws, we)) == ["2026-10-05", "2026-11-02", "2026-12-07"], days(parse(first_mon, ws, we)))
    last_fri = ics("UID:g\r\nSUMMARY:Last\r\nDTSTART:20260130T090000\r\nDTEND:20260130T100000\r\n"
                   "RRULE:FREQ=MONTHLY;BYDAY=-1FR\r\n")
    check("'last Friday' lands on the last Friday",
          days(parse(last_fri, ws, we)) == ["2026-10-30", "2026-11-27", "2026-12-25"], days(parse(last_fri, ws, we)))
    last_weekday = ics("UID:h\r\nSUMMARY:LW\r\nDTSTART:20260130T090000\r\nDTEND:20260130T100000\r\n"
                       "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1\r\n")
    check("'last weekday of the month' works (BYSETPOS)",
          days(parse(last_weekday, ws, we)) == ["2026-10-30", "2026-11-30", "2026-12-31"],
          days(parse(last_weekday, ws, we)))
    day_list = ics("UID:i\r\nSUMMARY:Pay\r\nDTSTART:20260101T090000\r\nDTEND:20260101T100000\r\n"
                   "RRULE:FREQ=MONTHLY;BYMONTHDAY=1,-1\r\n")
    check("BYMONTHDAY with a list and a negative day (1st and last)",
          days(parse(day_list, datetime(2026, 10, 1), datetime(2026, 11, 30, 23, 59)))
          == ["2026-10-01", "2026-10-31", "2026-11-01", "2026-11-30"])
    mother = ics("UID:j\r\nSUMMARY:Second Sunday of May\r\nDTSTART:20200510T090000\r\nDTEND:20200510T100000\r\n"
                 "RRULE:FREQ=YEARLY;BYMONTH=5;BYDAY=2SU\r\n")
    check("a yearly 'second Sunday of May'", days(parse(mother, datetime(2026, 5, 1), datetime(2026, 5, 31))) == ["2026-05-10"])
    weekdays = ics("UID:k\r\nSUMMARY:Work\r\nDTSTART:20260105T090000\r\nDTEND:20260105T100000\r\n"
                   "RRULE:FREQ=DAILY;BYDAY=MO,TU\r\n")
    check("DAILY with BYDAY only keeps those weekdays",
          days(parse(weekdays, datetime(2026, 10, 5), datetime(2026, 10, 14)))
          == ["2026-10-05", "2026-10-06", "2026-10-12", "2026-10-13"])


def test_rules_it_cannot_follow_fall_back_to_the_one_event():
    ws, we = datetime(2026, 1, 1), datetime(2026, 12, 31)
    for label, rule in (("BYWEEKNO", "FREQ=YEARLY;BYWEEKNO=20"), ("BYHOUR", "FREQ=DAILY;BYHOUR=9,17"),
                        ("HOURLY", "FREQ=HOURLY"), ("a bad BYDAY", "FREQ=WEEKLY;BYDAY=XX"),
                        ("COUNT=0", "FREQ=DAILY;COUNT=0"), ("a bad UNTIL", "FREQ=DAILY;UNTIL=soon"),
                        ("an ordinal on a weekly rule", "FREQ=WEEKLY;BYDAY=1MO")):
        got = parse(ics("UID:z\r\nSUMMARY:Odd\r\nDTSTART:20260310T090000\r\nDTEND:20260310T100000\r\nRRULE:%s\r\n" % rule), ws, we)
        check("%s: one real event, not an invented series" % label, days(got) == ["2026-03-10"], days(got))
    check("a runaway window does not hang or flood (series output is capped)",
          len(parse(ics("UID:r\r\nSUMMARY:Daily\r\nDTSTART:20000101T090000\r\nDTEND:20000101T100000\r\nRRULE:FREQ=DAILY\r\n"),
                    datetime(2000, 1, 1), datetime(2030, 1, 1))) <= cal.MAX_OCCURRENCES_PER_SERIES)


def test_crossed_out_and_moved_occurrences():
    ws, we = datetime(2026, 10, 1), datetime(2026, 11, 12)
    base = "UID:w\r\nSUMMARY:Stand-up\r\nDTSTART:20260930T090000\r\nDTEND:20260930T100000\r\nRRULE:FREQ=WEEKLY\r\n"
    got = parse(ics(base + "EXDATE:20261014T090000\r\n"), ws, we)
    check("an EXDATE occurrence is not drawn", "2026-10-14" not in days(got) and "2026-10-07" in days(got), days(got))
    got = parse(ics(base + "EXDATE:20261014T090000,20261021T090000\r\nEXDATE:20261028T090000\r\n"), ws, we)
    check("a comma list and repeated EXDATE lines are all honoured",
          not {"2026-10-14", "2026-10-21", "2026-10-28"} & set(days(got)), days(got))
    got = parse(ics("UID:ad\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261001\r\nDTEND;VALUE=DATE:20261002\r\n"
                    "RRULE:FREQ=DAILY;COUNT=4\r\nEXDATE;VALUE=DATE:20261002\r\n"), ws, we)
    check("a date-only EXDATE crosses out that day", days(got) == ["2026-10-01", "2026-10-03", "2026-10-04"], days(got))
    moved = ics(base,
                "UID:w\r\nRECURRENCE-ID:20261007T090000\r\nSUMMARY:Stand-up (moved)\r\n"
                "DTSTART:20261007T150000\r\nDTEND:20261007T160000\r\n")
    got = parse(moved, ws, we)
    check("a moved occurrence is drawn once, at its new time",
          stamps(got).count("2026-10-07 15:00") == 1 and "2026-10-07 09:00" not in stamps(got), stamps(got))
    check("...and the others of the series are untouched", "2026-10-14 09:00" in stamps(got) and "2026-10-21 09:00" in stamps(got))
    cancelled = ics(base,
                    "UID:w\r\nRECURRENCE-ID:20261007T090000\r\nSTATUS:CANCELLED\r\nSUMMARY:Stand-up\r\n"
                    "DTSTART:20261007T090000\r\nDTEND:20261007T100000\r\n")
    got = parse(cancelled, ws, we)
    check("a cancelled single occurrence disappears", "2026-10-07" not in days(got) and "2026-10-14" in days(got), days(got))
    other = ics(base, "UID:other\r\nRECURRENCE-ID:20261007T090000\r\nSUMMARY:Different series\r\n"
                      "DTSTART:20261007T150000\r\nDTEND:20261007T160000\r\n")
    check("a RECURRENCE-ID only replaces occurrences of ITS OWN UID", "2026-10-07 09:00" in stamps(parse(other, ws, we)))


def test_until():
    got = parse(ics("UID:u\r\nSUMMARY:U\r\nDTSTART:20261001T090000\r\nDTEND:20261001T100000\r\nRRULE:FREQ=DAILY;UNTIL=20261005\r\n"),
                datetime(2026, 10, 1), datetime(2026, 11, 1))
    check("a date-only UNTIL includes that whole day", days(got)[-1] == "2026-10-05" and len(got) == 5, days(got))
    got = parse(ics("UID:u2\r\nSUMMARY:U\r\nDTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
                    "RRULE:FREQ=DAILY;UNTIL=20261003T090000Z\r\n"), datetime(2026, 9, 30), datetime(2026, 11, 1))
    check("a UTC UNTIL includes its own instant", len(got) == 3, stamps(got))


def test_time_zones():
    real = cal._local_tz
    try:
        cal._local_tz = lambda: timezone(timedelta(hours=1))  # a PC on UTC+1, like Tunisia
        z = ics("UID:z\r\nSUMMARY:UTC\r\nDTSTART:20261015T140000Z\r\nDTEND:20261015T150000Z\r\n")
        check("a UTC time is shown on the PC's clock", stamps(parse(z, datetime(2026, 10, 1), datetime(2026, 11, 1))) == ["2026-10-15 15:00"])
        floating = ics("UID:f\r\nSUMMARY:Floating\r\nDTSTART:20261015T140000\r\nDTEND:20261015T150000\r\n")
        check("a floating time is taken as typed", stamps(parse(floating, datetime(2026, 10, 1), datetime(2026, 11, 1))) == ["2026-10-15 14:00"])
        check("an unknown TZID is taken at face value, not dropped",
              stamps(parse(ics("UID:n\r\nSUMMARY:N\r\nDTSTART;TZID=Mars/Olympus:20261015T140000\r\n"
                               "DTEND;TZID=Mars/Olympus:20261015T150000\r\n"), datetime(2026, 10, 1), datetime(2026, 11, 1)))
              == ["2026-10-15 14:00"])
        if zone_available("Europe/London") and zone_available("America/New_York"):
            lon = ics("UID:l\r\nSUMMARY:London\r\nDTSTART;TZID=Europe/London:20261015T150000\r\n"
                      "DTEND;TZID=Europe/London:20261015T160000\r\n")
            got = parse(lon, datetime(2026, 10, 1), datetime(2026, 11, 1))
            check("a TZID time is converted to the PC's clock (15:00 BST = 14:00 UTC = 15:00 on UTC+1)",
                  stamps(got) == ["2026-10-15 15:00"], stamps(got))
            ny = ics("UID:ny\r\nSUMMARY:NY\r\nDTSTART;TZID=America/New_York:20261015T090000\r\n"
                     "DTEND;TZID=America/New_York:20261015T100000\r\n")
            check("...09:00 in New York (EDT, UTC-4) is 13:00 UTC = 14:00 here",
                  stamps(parse(ny, datetime(2026, 10, 1), datetime(2026, 11, 1))) == ["2026-10-15 14:00"])
            check("...and the event keeps its length",
                  parse(ny, datetime(2026, 10, 1), datetime(2026, 11, 1))[0]["end"] - parse(ny, datetime(2026, 10, 1), datetime(2026, 11, 1))[0]["start"] == timedelta(hours=1))
            quoted = ics('UID:qt\r\nSUMMARY:Q\r\nDTSTART;TZID="America/New_York":20261015T090000\r\n'
                         'DTEND;TZID="America/New_York":20261015T100000\r\n')
            check("a quoted TZID works", stamps(parse(quoted, datetime(2026, 10, 1), datetime(2026, 11, 1))) == ["2026-10-15 14:00"])
            outlook = ics("UID:ol\r\nSUMMARY:Outlook\r\nDTSTART;TZID=Eastern Standard Time:20261015T090000\r\n"
                          "DTEND;TZID=Eastern Standard Time:20261015T100000\r\n")
            check("an Outlook (Windows) zone name works",
                  stamps(parse(outlook, datetime(2026, 10, 1), datetime(2026, 11, 1))) == ["2026-10-15 14:00"])
            series = ics("UID:s\r\nSUMMARY:Weekly NY\r\nDTSTART;TZID=America/New_York:20261022T090000\r\n"
                         "DTEND;TZID=America/New_York:20261022T100000\r\nRRULE:FREQ=WEEKLY\r\n")
            got = parse(series, datetime(2026, 10, 20), datetime(2026, 11, 20))
            check("a weekly 09:00 New York series keeps 09:00 there across the US clock change on 1 Nov "
                  "(14:00 before, 15:00 after on a fixed UTC+1 PC)",
                  stamps(got) == ["2026-10-22 14:00", "2026-10-29 14:00", "2026-11-05 15:00", "2026-11-12 15:00", "2026-11-19 15:00"],
                  stamps(got))
        else:
            print("SKIP    named-zone checks (no zoneinfo / tz database here; `tzdata` is a Windows dependency)")
    finally:
        cal._local_tz = real


def test_text_handling():
    ws, we = datetime(2026, 10, 1), datetime(2026, 11, 1)
    alarm = ics("UID:v\r\nSUMMARY:Dentist\r\nDESCRIPTION:Bring the form\r\nDTSTART:20261015T090000\r\nDTEND:20261015T100000\r\n"
                "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n")
    got = parse(alarm, ws, we)
    check("a VALARM's DESCRIPTION does not replace the event's", got and got[0]["description"] == "Bring the form", got)
    check("...and the event after the alarm block is still read", got and got[0]["title"] == "Dentist")
    low = ics("uid:lc\r\nsummary:Lowercase names\r\ndtstart:20261015T090000\r\ndtend:20261015T100000\r\n")
    check("property names are case-insensitive", days(parse(low, ws, we)) == ["2026-10-15"])
    esc = ics("UID:e\r\nSUMMARY:A\\, B\\; C\\\\ D\\nE\r\nDTSTART:20261015T090000\r\nDTEND:20261015T100000\r\n")
    check("TEXT escapes are read once (comma, semicolon, backslash, line break)",
          parse(esc, ws, we)[0]["title"] == "A, B; C\\ D\nE", repr(parse(esc, ws, we)[0]["title"]))
    broken = ics("UID:bad\r\nSUMMARY:No start\r\n",
                 "UID:ok\r\nSUMMARY:Fine\r\nDTSTART:20261015T090000\r\nDTEND:20261015T100000\r\n")
    check("one bad block does not lose the calendar", days(parse(broken, ws, we)) == ["2026-10-15"])
    neg = parse(ics("UID:n\r\nSUMMARY:Backwards\r\nDTSTART:20261015T100000\r\nDTEND:20261015T090000\r\n"), ws, we)
    check("an end before the start is a zero-length event, not a negative one",
          neg and neg[0]["end"] == neg[0]["start"])
    check("a multi-day all-day event keeps its span",
          parse(ics("UID:m\r\nSUMMARY:Trip\r\nDTSTART;VALUE=DATE:20261010\r\nDTEND;VALUE=DATE:20261013\r\n"), ws, we)[0]["end"]
          - datetime(2026, 10, 10) == timedelta(days=3))
    check("an unwindowed parse still returns one event per VEVENT (used to count a feed)",
          len(cal.parse_ics(ics("UID:a\r\nSUMMARY:A\r\nDTSTART:20230101T090000\r\nRRULE:FREQ=DAILY\r\n",
                                "UID:b\r\nSUMMARY:B\r\nDTSTART:20230101T090000\r\n"))) == 2)


class _Resp:
    def __init__(self, content, content_type=None):
        self.content = content
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.text = content.decode("latin-1")  # what requests does for text/* with no charset


def test_charset():
    arabic = "Réunion — اجتماع".encode("utf-8")
    check("no charset: decoded as UTF-8, not Latin-1", cal._decode_body(_Resp(arabic, "text/calendar")) == "Réunion — اجتماع")
    check("a BOM does not leak into the text", cal._decode_body(_Resp(b"\xef\xbb\xbfBEGIN:VCALENDAR", None)) == "BEGIN:VCALENDAR")
    latin = _Resp("Café".encode("latin-1"), "text/calendar; charset=ISO-8859-1")
    latin.text = "Café"
    check("a declared charset is the server's word", cal._decode_body(latin) == "Café")
    odd = _Resp(b"\xff\xfe caf\xe9", "text/calendar")
    check("bytes that are not UTF-8 fall back to what requests decoded", cal._decode_body(odd) == odd.text)

    class Bare:  # a test double with only .text
        text = "BEGIN:VCALENDAR"

    check("a reply object with only .text still works", cal._decode_body(Bare()) == "BEGIN:VCALENDAR")


def test_pruning_and_limits():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().replace(microsecond=0)
    old = (now - timedelta(days=200)).isoformat()
    cfg = dict(cal.DEFAULT_CONFIG)
    cfg["local_events"] = (["junk", 7, None]
                           + [{"id": "ev_old%d" % i, "title": "old", "start": old, "end": old} for i in range(cal.LOCAL_MAX_EVENTS)])
    cal.save_config(cfg)
    when = (now + timedelta(days=3)).replace(hour=15, minute=0)
    result = cal.add_local_event({"title": "New", "date": when.strftime("%Y-%m-%d"), "time": "15:00"})
    check("a full calendar of EXPIRED events does not refuse a new one", result.get("ok") is True, result)
    check("...the add reports what it removed", result.get("pruned", 0) >= cal.LOCAL_MAX_EVENTS, result)
    kept = json.loads(CONFIG.read_text(encoding="utf-8"))["local_events"]
    check("...only the new event is left, and the junk entries are gone", [e.get("title") for e in kept] == ["New"], kept)
    cfg = dict(cal.DEFAULT_CONFIG)
    soon = (now + timedelta(days=5)).isoformat()
    cfg["local_events"] = [{"id": "ev_live%d" % i, "title": "live", "start": soon, "end": soon} for i in range(cal.LOCAL_MAX_EVENTS)]
    cal.save_config(cfg)
    result = cal.add_local_event({"title": "One too many", "date": when.strftime("%Y-%m-%d"), "time": "16:00"})
    check("500 LIVE events still refuse another (the limit is unchanged)", result.get("ok") is False, result)


def _cli(*args):
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=str(_JARVIS_CLI))
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True, env=env, timeout=120)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout, "err": proc.stderr}


def test_legacy_cli_commands():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        CONFIG.unlink()
    real = cal.fetch_feed  # the CLI runs in another process: it must not need the network to be refused
    link = "https://example.invalid/private-abc123/basic.ics"
    CONFIG.write_text("{ nope", encoding="utf-8")
    before = CONFIG.read_bytes()
    code, out = _cli("calendar-add", "Work", link)
    check("calendar-add refuses an unreadable calendar.json", code == 1 and out.get("ok") is False, out)
    check("...and leaves the file exactly as it was", CONFIG.read_bytes() == before)
    CONFIG.unlink()
    code, out = _cli("calendar-add", "Work", "not a link")
    check("calendar-add refuses something that is not a web link", code == 1 and out.get("ok") is False, out)
    check("...and saved nothing", not CONFIG.exists() or not json.loads(CONFIG.read_text(encoding="utf-8")).get("feeds"))
    code, out = _cli("calendar-add", "Work", link)
    check("calendar-add links a calendar (offline: it says it could not be reached)", code == 0 and out.get("ok") is True and out.get("reachable") is False, out)
    check("...and the answer does not echo the secret address", "private-abc123" not in json.dumps(out), out)
    code, out = _cli("calendar-add", "work", "https://example.invalid/other.ics")
    check("a second calendar with the same name (any case) is refused", code == 1 and out.get("ok") is False, out)
    code, out = _cli("calendar-add", "Other", link)
    check("the same link twice is refused", code == 1 and out.get("ok") is False, out)
    for i in range(cal.MAX_FEEDS):
        _cli("calendar-add", "F%d" % i, "https://example.invalid/f%d.ics" % i)
    code, out = _cli("calendar-add", "OneMore", "https://example.invalid/more.ics")
    check("the 12-calendar limit applies here too", code == 1 and out.get("ok") is False and "12" in str(out.get("error")), out)
    code, out = _cli("calendar-remove", "nobody")
    check("calendar-remove says so when there is no such calendar", code == 1 and out.get("ok") is False, out)
    code, out = _cli("calendar-remove", "Work")
    check("calendar-remove removes one", code == 0 and out.get("ok") is True, out)
    unattended = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=str(_JARVIS_CLI), JARVIS_SCHEDULED="1")
    for cmd in (("calendar-add", "Sneaky", "https://example.invalid/s.ics"), ("calendar-list",), ("calendar-remove", "F0")):
        proc = subprocess.run([sys.executable, "-m", "jarvis", *cmd], capture_output=True, text=True, env=unattended, timeout=120)
        check("%s is refused inside a scheduled job" % cmd[0], proc.returncode == 1 and '"unattended"' in proc.stdout, proc.stdout[:200])
    cal.fetch_feed = real


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n== {name}")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(f"{name} ran without raising", False, e)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
