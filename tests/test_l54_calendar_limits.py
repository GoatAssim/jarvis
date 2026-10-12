"""Master plan L.54 -- the seven limits left open after the audit, now fixed.

  1. several repeat rules were not understood: hourly and finer, BYWEEKNO, BYYEARDAY, BYHOUR
     (and BYMINUTE / BYSECOND), RDATE, and combinations such as DAILY with BYMONTHDAY;
  2. weeks always started on Monday (the panel's Week / Month views, and WKST in a repeat rule);
  3. a time zone this PC could not resolve was shown as typed, with no word about it;
  4. two writers could race on calendar.json (read, change, write the whole file back);
  5. the chat tool calendar_add_event silently dropped a live event past 500;
  6. check_feed left the fetched text in the cache for a link that was never saved;
  7. there was no panel control for keep_days.

Run: python3 tests/test_l54_calendar_limits.py   (no key, no network)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l54lim-home-")
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
CACHE = JDIR / "calendar_cache.json"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok      " if cond else "FAILED  ") + name + ("" if cond else (f"  -- {detail}" if detail else "")))


def ics(*events, extra=""):
    return ("BEGIN:VCALENDAR\r\n" + extra
            + "".join("BEGIN:VEVENT\r\n" + e + "END:VEVENT\r\n" for e in events)
            + "END:VCALENDAR\r\n")


def stamps(events):
    return [e["start"].strftime("%Y-%m-%d %H:%M") for e in events]


def days(events):
    return [e["start"].strftime("%Y-%m-%d") for e in events]


def parse(text, ws, we):
    return cal.parse_ics(text, ws, we)


def series(rule, start="20261012T090000", end="20261012T093000", extra=""):
    return ics("UID:s\r\nSUMMARY:S\r\nDTSTART:%s\r\nDTEND:%s\r\nRRULE:%s\r\n%s" % (start, end, rule, extra))


def zone_available(name):
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 -- no zoneinfo / no tz database on this machine
        return False


def _reset(cfg=None):
    import shutil
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)
    if cfg is not None:
        document = dict(cal.DEFAULT_CONFIG)
        document.update(cfg)
        cal.save_config(document)


def _live(i, days_ahead=5):
    soon = (datetime.now() + timedelta(days=days_ahead)).replace(microsecond=0)
    return {"id": "ev_%08x" % i, "title": "E%d" % i, "start": soon.isoformat(),
            "end": (soon + timedelta(hours=1)).isoformat()}


# ---- 1. repeat rules -------------------------------------------------------------------------------------------------
def test_hourly_and_finer():
    ws, we = datetime(2026, 10, 12), datetime(2026, 10, 14)
    got = parse(series("FREQ=HOURLY;INTERVAL=2;COUNT=4"), ws, we)
    check("every 2 hours, 4 times", stamps(got) == ["2026-10-12 09:00", "2026-10-12 11:00", "2026-10-12 13:00", "2026-10-12 15:00"], stamps(got))
    check("...each keeps its own 30-minute length", all(e["end"] - e["start"] == timedelta(minutes=30) for e in got))
    got = parse(series("FREQ=MINUTELY;INTERVAL=15;COUNT=3", "20261012T100000", "20261012T101000"), ws, we)
    check("every 15 minutes, 3 times", stamps(got) == ["2026-10-12 10:00", "2026-10-12 10:15", "2026-10-12 10:30"], stamps(got))
    got = parse(series("FREQ=SECONDLY;INTERVAL=30;COUNT=3", "20261012T100000", "20261012T100001"), ws, we)
    check("every 30 seconds works too", [e["start"].strftime("%H:%M:%S") for e in got] == ["10:00:00", "10:00:30", "10:01:00"], stamps(got))
    got = parse(series("FREQ=HOURLY;BYHOUR=9,10;COUNT=4"), ws, datetime(2026, 10, 16))
    check("HOURLY with BYHOUR only keeps those hours", stamps(got) == ["2026-10-12 09:00", "2026-10-12 10:00", "2026-10-13 09:00", "2026-10-13 10:00"], stamps(got))
    got = parse(series("FREQ=HOURLY;BYMINUTE=0,30;COUNT=4"), ws, we)
    check("HOURLY with BYMINUTE adds those minutes to each hour", stamps(got) == ["2026-10-12 09:00", "2026-10-12 09:30", "2026-10-12 10:00", "2026-10-12 10:30"], stamps(got))
    old = series("FREQ=HOURLY", "20240101T000000", "20240101T003000")
    got = parse(old, datetime(2026, 10, 12), datetime(2026, 10, 12, 3))
    check("an hourly series from years ago is still found near the window (no COUNT: it jumps)",
          stamps(got)[:2] == ["2026-10-12 00:00", "2026-10-12 01:00"], stamps(got))
    flood = parse(series("FREQ=SECONDLY", "20260101T000000", "20260101T000001"), datetime(2026, 1, 1), datetime(2026, 1, 3))
    check("a runaway SECONDLY series is capped", len(flood) <= cal.MAX_OCCURRENCES_PER_SERIES, len(flood))


def test_by_parts():
    got = parse(series("FREQ=DAILY;BYHOUR=9,17;COUNT=4"), datetime(2026, 10, 12), datetime(2026, 10, 16))
    check("DAILY with BYHOUR", stamps(got) == ["2026-10-12 09:00", "2026-10-12 17:00", "2026-10-13 09:00", "2026-10-13 17:00"], stamps(got))
    got = parse(series("FREQ=DAILY;BYMONTHDAY=1,15", "20261001T090000", "20261001T100000"), datetime(2026, 10, 1), datetime(2026, 11, 2))
    check("DAILY with BYMONTHDAY (the 1st and the 15th)", days(got) == ["2026-10-01", "2026-10-15", "2026-11-01"], days(got))
    got = parse(series("FREQ=MONTHLY;BYMONTH=1,7;BYMONTHDAY=4", "20260104T090000", "20260104T100000"), datetime(2026, 1, 1), datetime(2027, 2, 1))
    check("MONTHLY with BYMONTH", days(got) == ["2026-01-04", "2026-07-04", "2027-01-04"], days(got))
    got = parse(series("FREQ=WEEKLY;BYDAY=MO,TU,WE;BYSETPOS=-1;COUNT=2", "20261012T090000", "20261012T100000"), datetime(2026, 10, 1), datetime(2026, 11, 30))
    check("WEEKLY with BYSETPOS (the last of Mon/Tue/Wed each week)", days(got) == ["2026-10-14", "2026-10-21"], days(got))
    got = parse(series("FREQ=YEARLY;BYYEARDAY=1,100,-1", "20260101T090000", "20260101T100000"), datetime(2026, 1, 1), datetime(2026, 12, 31, 23, 59))
    check("BYYEARDAY (day 1, day 100, the last day)", days(got) == ["2026-01-01", "2026-04-10", "2026-12-31"], days(got))
    got = parse(series("FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO", "20260511T090000", "20260511T100000"), datetime(2026, 1, 1), datetime(2027, 12, 31))
    check("BYWEEKNO with BYDAY (Monday of ISO week 20)", days(got) == ["2026-05-11", "2027-05-17"], days(got))
    got = parse(series("FREQ=YEARLY;BYWEEKNO=20", "20260511T090000", "20260511T100000"), datetime(2026, 1, 1), datetime(2027, 12, 31))
    check("BYWEEKNO alone uses the start's own weekday", days(got) == ["2026-05-11", "2027-05-17"], days(got))
    got = parse(series("FREQ=YEARLY;BYWEEKNO=53", "20261231T090000", "20261231T100000"), datetime(2026, 1, 1), datetime(2032, 12, 31))
    check("week 53 exists only in some years (2026 and 2032 here), never invented", days(got) == ["2026-12-31", "2032-12-30"], days(got))
    got = parse(series("FREQ=YEARLY;BYDAY=20MO", "20260511T090000", "20260511T100000"), datetime(2026, 1, 1), datetime(2027, 12, 31))
    check("a numbered weekday of the YEAR (20th Monday) needs no BYMONTH any more", days(got) == ["2026-05-18", "2027-05-17"], days(got))


def test_wkst():
    base = "FREQ=WEEKLY;INTERVAL=2;BYDAY=SU,MO"
    ws, we = datetime(2026, 10, 1), datetime(2026, 11, 1)
    got = parse(series(base + ";WKST=SU", "20261011T090000", "20261011T100000"), ws, we)
    check("WKST=SU pairs each Sunday with the Monday after it", days(got) == ["2026-10-11", "2026-10-12", "2026-10-25", "2026-10-26"], days(got))
    got = parse(series(base, "20261011T090000", "20261011T100000"), ws, we)
    check("the default week starts on Monday, so the same rule pairs a Sunday with the Monday before it",
          days(got) == ["2026-10-11", "2026-10-19", "2026-10-25"], days(got))


def test_rdate():
    ws, we = datetime(2026, 10, 1), datetime(2026, 10, 31)
    head = "UID:r\r\nSUMMARY:R\r\nDTSTART:20261001T090000\r\nDTEND:20261001T100000\r\n"
    got = parse(ics(head + "RDATE:20261005T090000,20261007T090000\r\n"), ws, we)
    check("RDATE adds dates to a single event", days(got) == ["2026-10-01", "2026-10-05", "2026-10-07"], days(got))
    got = parse(ics(head + "RDATE;VALUE=PERIOD:20261009T090000/20261009T120000\r\n"), ws, we)
    check("a PERIOD RDATE brings its own length", days(got) == ["2026-10-01", "2026-10-09"] and got[1]["end"] - got[1]["start"] == timedelta(hours=3), stamps(got))
    got = parse(ics(head + "RDATE;VALUE=PERIOD:20261009T090000/PT90M\r\n"), ws, we)
    check("...a PERIOD can give a length instead of an end", got[1]["end"] - got[1]["start"] == timedelta(minutes=90), stamps(got))
    got = parse(ics(head + "RDATE:20261005T090000\r\nEXDATE:20261005T090000\r\n"), ws, we)
    check("an EXDATE crosses out an RDATE as well", days(got) == ["2026-10-01"], days(got))
    got = parse(ics(head + "RRULE:FREQ=DAILY;COUNT=3\r\nRDATE:20261002T090000,20261020T090000\r\n"), ws, we)
    check("an RDATE on a moment the rule already has is not drawn twice", days(got) == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-20"], days(got))
    check("with no window (the 'how many events' count) an RDATE is not expanded",
          len(cal.parse_ics(ics(head + "RDATE:20261005T090000\r\n"))) == 1)


# ---- 3. time zones ---------------------------------------------------------------------------------------------------------
_CUSTOM_ZONE = (
    "BEGIN:VTIMEZONE\r\nTZID:Custom Zone One\r\n"
    "BEGIN:STANDARD\r\nDTSTART:19701025T030000\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\nEND:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\nDTSTART:19700329T020000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\nEND:DAYLIGHT\r\n"
    "END:VTIMEZONE\r\n")


def test_time_zones():
    real = cal._local_tz
    ws, we = datetime(2026, 10, 1), datetime(2026, 12, 1)
    try:
        cal._local_tz = lambda: timezone(timedelta(hours=1))  # a PC on UTC+1
        oct_ev = "UID:c1\r\nSUMMARY:Oct\r\nDTSTART;TZID=Custom Zone One:20261015T150000\r\nDTEND;TZID=Custom Zone One:20261015T160000\r\n"
        nov_ev = "UID:c2\r\nSUMMARY:Nov\r\nDTSTART;TZID=Custom Zone One:20261105T150000\r\nDTEND;TZID=Custom Zone One:20261105T160000\r\n"
        got = parse(ics(oct_ev, nov_ev, extra=_CUSTOM_ZONE), ws, we)
        by = {e["title"]: e for e in got}
        check("a zone the file defines itself (VTIMEZONE) is used: 15:00 in summer time (+02:00) is 14:00 on a UTC+1 PC",
              by.get("Oct") and stamps([by["Oct"]]) == ["2026-10-15 14:00"], stamps(got))
        check("...and after its own clock change (last Sunday of October) 15:00 is +01:00, so 15:00 here",
              by.get("Nov") and stamps([by["Nov"]]) == ["2026-11-05 15:00"], stamps(got))
        check("...neither is flagged as unconverted", not any(e.get("tz_unresolved") for e in got), got)
        check("a zone defined in one file does not leak into the next file read",
              stamps(parse(ics(oct_ev), ws, we)) == ["2026-10-15 15:00"] and parse(ics(oct_ev), ws, we)[0].get("tz_unresolved") == "Custom Zone One")

        mars = ics("UID:m\r\nSUMMARY:Mars\r\nDTSTART;TZID=Mars/Olympus:20261015T140000\r\nDTEND;TZID=Mars/Olympus:20261015T150000\r\n")
        got = parse(mars, ws, we)
        check("a zone nothing can resolve is still shown as written...", stamps(got) == ["2026-10-15 14:00"], stamps(got))
        check("...but the event now says so", got[0].get("tz_unresolved") == "Mars/Olympus", got)
        panel = cal._panel_event(got[0])
        check("the panel's copy carries the note", panel.get("tz_unresolved") == "Mars/Olympus" and "can't convert" in panel.get("tz_note", ""), panel)
        check("the model's copy carries it too", "can't convert" in cal._serialize(got[0], datetime.now()).get("timezone_note", ""))
        plain = cal._panel_event(parse(ics("UID:p\r\nSUMMARY:P\r\nDTSTART:20261015T140000\r\nDTEND:20261015T150000\r\n"), ws, we)[0])
        check("an ordinary event has no zone keys at all", "tz_unresolved" not in plain and "tz_note" not in plain, plain)
        check("a zone name from a calendar is cut and cleaned before it is shown",
              cal._clean_zone_name("A" * 200 + "\x00") == "A" * 60)

        if zone_available("America/New_York"):
            path = ics("UID:pp\r\nSUMMARY:Path\r\nDTSTART;TZID=/mozilla.org/20050126_1/America/New_York:20261015T090000\r\n"
                       "DTEND;TZID=/mozilla.org/20050126_1/America/New_York:20261015T100000\r\n")
            check("a path-style zone id (/mozilla.org/.../America/New_York) is understood", stamps(parse(path, ws, we)) == ["2026-10-15 14:00"], stamps(parse(path, ws, we)))
            low = ics("UID:lw\r\nSUMMARY:Low\r\nDTSTART;TZID=eastern standard time:20261015T090000\r\nDTEND;TZID=eastern standard time:20261015T100000\r\n")
            check("a Windows zone name is matched without regard to case", stamps(parse(low, ws, we)) == ["2026-10-15 14:00"], stamps(parse(low, ws, we)))
        if zone_available("Australia/Hobart"):
            hob = ics("UID:hb\r\nSUMMARY:Hobart\r\nDTSTART;TZID=Tasmania Standard Time:20261015T100000\r\nDTEND;TZID=Tasmania Standard Time:20261015T110000\r\n")
            check("a Windows name that was missing before (Tasmania) now converts (10:00 AEDT = 23:00 UTC the day before = 00:00 on UTC+1)",
                  stamps(parse(hob, ws, we)) == ["2026-10-15 00:00"], stamps(parse(hob, ws, we)))
    finally:
        cal._local_tz = real


# ---- 6. a probe leaves nothing in the cache ------------------------------------------------------------------------------------
class _Resp:
    status_code = 200
    headers = {"Content-Type": "text/calendar; charset=utf-8"}

    def __init__(self, body):
        self.content = body.encode("utf-8")
        self.text = body


def _stub_requests(body):
    class _Requests:
        calls = []

        @staticmethod
        def get(url, timeout=None, headers=None):
            _Requests.calls.append(url)
            return _Resp(body)
    return _Requests


def test_check_feed_leaves_no_cache():
    _reset()
    link = "https://calendar.example.com/ical/private-abc123def456/basic.ics"
    other = "https://other.example.org/feeds/zzzz9999/old.ics"
    body = ics("UID:a\r\nSUMMARY:Standup\r\nDTSTART:%s\r\nDTEND:%s\r\n" % (
        (datetime.now() + timedelta(days=1)).strftime("%Y%m%dT090000"), (datetime.now() + timedelta(days=1)).strftime("%Y%m%dT093000")))
    saved = sys.modules.get("requests")
    stub = _stub_requests(body)
    sys.modules["requests"] = stub
    try:
        out = cal.check_feed(link)
        check("a check succeeds", out.get("ok") is True and out.get("events_found") == 1, out)
        check("...it really fetched", stub.calls == [link], stub.calls)
        check("...and stored nothing: no cache file, and no text for that link", not CACHE.exists() or link not in cal._load_cache(), cal._load_cache())
        cal.check_feed(link)
        check("...a second check also leaves nothing", not CACHE.exists() or cal._load_cache() == {}, cal._load_cache())

        # An older version cached text for a link that was only ever checked: it is cleared the next
        # time a calendar is linked or removed.
        cal._save_cache({other: {"body": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "at": "2026-01-01T00:00:00"}})
        done = cal.link_feed("Work", link)
        check("linking works", done.get("ok") is True, done)
        cache = cal._load_cache()
        check("once SAVED, the fetched copy is its cache, so the first read needs no second download", link in cache and cache[link]["body"] == body, list(cache))
        check("...the panel then shows when it was last read", done["feed"]["last_fetched"], done)
        check("...and the stray copy for a link that is not linked was cleared", other not in cache, list(cache))
        check("the link itself is not in the answer", link not in json.dumps(done) and "private-abc123" not in json.dumps(done))
        gone = cal.remove_feed("Work")
        check("removing it clears its copy", gone.get("ok") is True and link not in cal._load_cache(), gone)
    finally:
        if saved is not None:
            sys.modules["requests"] = saved
        else:
            sys.modules.pop("requests", None)


def test_cache_entries_survive_two_writers():
    _reset()
    cal._save_cache({"https://a.example/a.ics": {"body": "A", "at": "2026-01-01T00:00:00"}})
    cal._store_cache("https://b.example/b.ics", "B")
    cache = cal._load_cache()
    check("storing one feed's copy keeps the other's (the cache is re-read under its lock first)",
          set(cache) == {"https://a.example/a.ics", "https://b.example/b.ics"}, list(cache))


# ---- 4. two writers ---------------------------------------------------------------------------------------------------------------
def test_lock_basics():
    _reset()
    lock = Path(str(CONFIG) + ".lock")
    with cal._config_lock() as held:
        check("the lock is taken", held is True and lock.exists())
    check("...and released", not lock.exists())
    try:
        with cal._config_lock():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("...even when the work inside it fails", not lock.exists())

    saved_wait = cal.LOCK_WAIT_SECONDS
    cal.LOCK_WAIT_SECONDS = 0.2
    try:
        lock.write_text("someone else", encoding="utf-8")  # a fresh lock another process holds
        out = cal.add_local_event({"title": "Blocked", "date": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"), "time": "10:00"})
        check("a write that cannot get the lock refuses, saying why, and changes nothing",
              out.get("ok") is False and "Another Jarvis" in out.get("error", "") and cal.load_config()["local_events"] == [], out)
        check("...the other process's lock is left alone", lock.exists() and lock.read_text(encoding="utf-8") == "someone else")
        tool = cal.tool_calendar_add_event({"title": "Blocked", "when": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d") + "T10:00"})
        check("the chat tool refuses the same way", tool.get("ok") is False and "Another Jarvis" in tool.get("error", ""), tool)
        for label, result in (("link", cal.set_feed_enabled("x", True)), ("remove", cal.remove_feed("x")),
                              ("update", cal.update_local_event("ev_00000000", {"title": "x"})),
                              ("delete", cal.delete_local_event("ev_00000000")), ("settings", cal.set_settings({"keep_days": 30}))):
            check("%s refuses while the file is locked" % label, result.get("ok") is False and "Another Jarvis" in result.get("error", ""), result)
        old = time.time() - (cal.LOCK_STALE_SECONDS + 5)
        os.utime(str(lock), (old, old))
        out = cal.add_local_event({"title": "After a crash", "date": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"), "time": "10:00"})
        check("a lock left by a process that died (older than LOCK_STALE_SECONDS) is taken over", out.get("ok") is True, out)
        check("...and gone afterwards", not lock.exists())
    finally:
        cal.LOCK_WAIT_SECONDS = saved_wait


def test_parallel_writers_do_not_lose_events():
    _reset()
    day = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    code = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "from jarvis.actions import calendar_tools as cal\n"
        "print(json.dumps(cal.add_local_event({'title': sys.argv[1], 'date': %r, 'time': '10:00'})))\n"
    ) % (str(_JARVIS_CLI), day)
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME)
    workers = 8
    procs = [subprocess.Popen([sys.executable, "-c", code, "Event %d" % i], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for i in range(workers)]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        try:
            results.append(json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            results.append({"ok": False, "error": (out + err)[-200:]})
    check("all %d simultaneous adds report success" % workers, all(r.get("ok") for r in results), results)
    titles = sorted(e["title"] for e in cal.load_config()["local_events"])
    check("all %d events are in calendar.json (none lost to a late writer)" % workers, titles == sorted("Event %d" % i for i in range(workers)), titles)
    ids = [e["id"] for e in cal.load_config()["local_events"]]
    check("...each with its own id", len(set(ids)) == len(ids) == workers, ids)
    check("no lock file is left behind", not Path(str(CONFIG) + ".lock").exists())
    check("and no half-written temp file", not (JDIR / "calendar.json.tmp").exists())


# ---- 5. the 500 limit ---------------------------------------------------------------------------------------------------------------
def test_chat_tool_never_drops_a_live_event():
    _reset({"local_events": [_live(i) for i in range(cal.LOCAL_MAX_EVENTS)]})
    before = cal.load_config()["local_events"]
    day = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    out = cal.tool_calendar_add_event({"title": "Number 501", "when": day + "T10:00"})
    check("at 500 live events the chat tool REFUSES, naming the number", out.get("ok") is False and str(cal.LOCAL_MAX_EVENTS) in out.get("error", ""), out)
    check("...and says nothing was added or removed (no 'pruned' claim)", "pruned" not in out and "id" not in out, out)
    after = cal.load_config()["local_events"]
    check("...every one of the 500 is still there, in the same order", after == before, len(after))
    panel = cal.add_local_event({"title": "Number 501", "date": day, "time": "10:00"})
    check("the panel refuses the same way (one rule for both)", panel.get("ok") is False and str(cal.LOCAL_MAX_EVENTS) in panel.get("error", ""), panel)

    old = (datetime.now() - timedelta(days=200)).replace(microsecond=0).isoformat()
    _reset({"local_events": [{"id": "ev_old%05d" % i, "title": "old", "start": old, "end": old} for i in range(cal.LOCAL_MAX_EVENTS)]})
    out = cal.tool_calendar_add_event({"title": "Fresh", "when": day + "T10:00"})
    check("a calendar full of EXPIRED events still takes a new one, and says how many went",
          out.get("ok") is True and out.get("pruned", 0) >= cal.LOCAL_MAX_EVENTS and "older event" in out.get("note", ""), out)
    check("...only the new one is left", [e["title"] for e in cal.load_config()["local_events"]] == ["Fresh"])

    _reset()
    ancient = (datetime.now() - timedelta(days=cal.LOCAL_RETENTION_DAYS + 40)).replace(microsecond=0)
    saved, pruned, error = cal._save_new_event({"title": "Ancient", "start": ancient.isoformat(), "end": (ancient + timedelta(hours=1)).isoformat()})
    check("an event that would be removed at once is refused, not saved and dropped in the same breath",
          saved is None and error and str(cal.LOCAL_RETENTION_DAYS) in error, (saved, pruned, error))
    check("...and the file holds nothing", cal.load_config()["local_events"] == [])


# ---- 7 and 2. the settings the panel owns ------------------------------------------------------------------------------------
def test_settings():
    _reset()
    check("the defaults: 60 days, Monday", cal.retention_days() == 60 and cal.week_start() == "mon")
    check("the panel's view reports both", cal.feeds_view()["retention_days"] == 60 and cal.feeds_view()["week_start"] == "mon"
          and cal.feeds_view()["retention_days_max"] == cal.LOCAL_RETENTION_MAX_DAYS)
    out = cal.set_settings({"keep_days": "90", "week_start": "SUN"})
    check("both can be set at once (a number as text, a name in any case)", out.get("ok") is True and out["retention_days"] == 90 and out["week_start"] == "sun", out)
    stored = json.loads(CONFIG.read_text(encoding="utf-8"))
    check("...they are in calendar.json", stored["keep_days"] == 90 and stored["week_start"] == "sun", stored)
    check("...and the rest of the file is untouched", stored["feeds"] == [] and stored["local_events"] == [] and stored["default_reminder_minutes"] == 60)
    check("the view follows", cal.retention_days() == 90 and cal.week_start() == "sun" and cal.feeds_view()["week_start"] == "sun")
    out = cal.set_settings({"week_start": "sat"})
    check("one setting alone leaves the other as it was", out.get("ok") is True and cal.retention_days() == 90 and cal.week_start() == "sat", out)
    for label, payload in (("zero days", {"keep_days": 0}), ("too many days", {"keep_days": cal.LOCAL_RETENTION_MAX_DAYS + 1}),
                           ("text", {"keep_days": "soon"}), ("a fraction", {"keep_days": 1.5}), ("true", {"keep_days": True}),
                           ("nothing", {"keep_days": None}), ("a bad weekday", {"week_start": "friday"}), ("an empty request", {}),
                           ("a stray key only", {"default_reminder_minutes": 5})):
        before = CONFIG.read_text(encoding="utf-8")
        result = cal.set_settings(payload)
        check("%s is refused with a reason and nothing is written" % label, result.get("ok") is False and result.get("error") and CONFIG.read_text(encoding="utf-8") == before, result)
    out = cal.set_settings({"keep_days": 365.0})
    check("a whole float is fine", out.get("ok") is True and cal.retention_days() == 365, out)
    check("a non-dict is a refusal, never a crash", cal.set_settings("oops").get("ok") is False)
    CONFIG.write_text("{ not json", encoding="utf-8")
    broken = CONFIG.read_bytes()
    out = cal.set_settings({"keep_days": 30})
    check("an unreadable calendar.json is never overwritten", out.get("ok") is False and CONFIG.read_bytes() == broken, out)
    CONFIG.write_text(json.dumps({"week_start": "friday", "keep_days": 99999}), encoding="utf-8")
    check("a hand-edited bad value reads as the default", cal.week_start() == "mon" and cal.retention_days() == 60)


def test_changing_keep_days_deletes_nothing_and_says_what_will_go():
    ancient = (datetime.now() - timedelta(days=100)).replace(microsecond=0).isoformat()
    _reset({"local_events": [{"id": "ev_aaaa0001", "title": "Ancient", "start": ancient, "end": ancient}, _live(1)]})
    check("with the default 60 days, one saved event is past its keep time", cal.feeds_view()["expiring_count"] == 1, cal.feeds_view())
    out = cal.set_settings({"keep_days": 365})
    check("raising it says none will go now", out.get("ok") is True and out["expiring_count"] == 0, out)
    check("...and the settings call deleted nothing", len(cal.load_config()["local_events"]) == 2)
    out = cal.set_settings({"keep_days": 30})
    check("lowering it reports how many will go the next time one is added", out["expiring_count"] == 1, out)
    check("...but still deletes nothing itself", len(cal.load_config()["local_events"]) == 2)
    day = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    added = cal.add_local_event({"title": "New", "date": day, "time": "10:00"})
    check("an add then removes it, and says so", added.get("ok") is True and added.get("pruned") == 1, added)


def test_admin_settings_op():
    _reset()
    check("'settings' is a registered op", "settings" in cal.ADMIN_OPS)
    out = cal.admin("settings", {"settings": {"keep_days": 45, "week_start": "sun"}})
    check("admin('settings') applies it", out.get("ok") is True and cal.retention_days() == 45 and cal.week_start() == "sun", out)
    check("a missing 'settings' object is a refusal, not a crash", cal.admin("settings", {}).get("ok") is False)
    os.environ["JARVIS_SCHEDULED"] = "1"
    try:
        out = cal.admin("settings", {"settings": {"keep_days": 5}})
        check("it is refused inside a scheduled job", out.get("ok") is False and out.get("code") == "unattended" and cal.retention_days() == 45, out)
    finally:
        os.environ.pop("JARVIS_SCHEDULED", None)
    check("no model tool can reach it", all("set_settings" not in json.dumps(s) and "settings" not in s["name"] for s in cal.TOOL_SCHEMAS))


def test_legacy_cli_add_uses_the_same_refusals():
    _reset()
    first = cal.add_feed_unverified("  Work  ", "webcal://calendar.example.com/ical/secret-123456/basic.ics")
    check("the unverified add saves, tidies the name and rewrites webcal://", first.get("ok") is True and first["name"] == "Work" and first["link"].startswith("https://"), first)
    check("...a second name in another case is refused", cal.add_feed_unverified("WORK", "https://other.example.com/o.ics").get("ok") is False)
    check("...the same link twice is refused", cal.add_feed_unverified("Other", "https://calendar.example.com/ical/secret-123456/basic.ics").get("ok") is False)
    check("...something that is not a link is refused", cal.add_feed_unverified("X", "not a link").get("ok") is False)
    CONFIG.write_text("{ nope", encoding="utf-8")
    broken = CONFIG.read_bytes()
    check("...an unreadable calendar.json is never overwritten", cal.add_feed_unverified("Y", "https://y.example.com/y.ics").get("ok") is False and CONFIG.read_bytes() == broken)


# ---- the web layer and the page ---------------------------------------------------------------------------------------------------------
def test_web_layer_and_page():
    server = (_ROOT / "web" / "server.js").read_text(encoding="utf-8")
    routes = re.findall(r'app\.(post)\("(/api/calendar/settings)"([^\n]*)', server)
    check("the settings route exists and sits behind the same gate as every other calendar route",
          len(routes) == 1 and "requireCalendarOrigin" in routes[0][2] and "requireJarvis" in routes[0][2], routes)
    block = server[server.index('"/api/calendar/settings"'):server.index("// Ambient monitoring.")]
    check("it passes on only keep_days and week_start", "keep_days" in block and "week_start" in block and 'calendarAdmin(res, "settings"' in block, block[:300])
    check("it is not given the request body wholesale", "calendarAdmin(res, \"settings\", req.body" not in block)
    js = (_ROOT / "web" / "public" / "calendar.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))
    check("the page has the keep-days and week-start controls and posts them to that route",
          'id: "cal-set-keep"' in js and 'id: "cal-set-week"' in js and '"/api/calendar/settings"' in js)
    check("...it still never uses innerHTML", not re.search(r"innerHTML|insertAdjacentHTML|document\.write|outerHTML", code))
    check("...it reads the settings before it asks for events (the window depends on the week start)",
          re.search(r"async function refreshAll\(refresh\) \{\s*(//[^\n]*\n\s*)?await loadFeeds\(\);\s*await loadEvents\(refresh\);", js) is not None)
    check("...and shows the zone note on an event", "ev.tz_note" in js)
    css = (_ROOT / "web" / "public" / "calendar.css").read_text(encoding="utf-8")
    check("the new classes exist in calendar.css", all(c in css for c in (".cal-settings__row", ".cal-pane__warn", ".cal-input--short")))
    agents = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    check("AGENTS.md states the lock, the refusal at 500 and the probe rule", all(x in agents for x in ("_config_lock", "never drops", "_no_store")))


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
