"""Master plan L.54 -- hardening of the Calendars back end (actions/calendar_tools.py).

tests/test_l54_calendar.py pins what the panel back end does. This file pins four things
it does not:

  1. an unreadable calendar.json is NEVER overwritten: every writer (the panel's link /
     enable / remove / event add / edit / delete, and the model's calendar_add_event)
     refuses and leaves the file exactly as it was; a missing or empty file is a fresh
     install, not a problem;
  2. DTSTART;VALUE=DATE-TIME keeps its time of day (a substring test on "VALUE=DATE"
     used to read it as midnight), a date-only stamp is still an all-day event;
  3. keep_days (calendar.json) sets how long Jarvis's own events are kept: 60 by default,
     1..3650 accepted, anything else ignored; the panel's stated limit, the pruning and
     the "too old" refusal all follow it;
  4. adding an event says how many old ones it removed (no key when none), and a failed
     save is reported instead of claimed.

Run: python3 tests/test_l54_calendar_hardening.py   (no key, no network)
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l54h-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID",
             "JARVIS_CONTEXT"):
    os.environ.pop(_var, None)

_JARVIS_CLI = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis.actions import calendar_tools as cal  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"
CONFIG = JDIR / "calendar.json"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _reset(config=None):
    import shutil
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)
    if config is not None:
        CONFIG.write_text(json.dumps(config), encoding="utf-8")


def _cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _stored(title, days_ago, hours=1):
    start = (datetime.now() - timedelta(days=days_ago)).replace(microsecond=0)
    return {"id": "ev_%08x" % (abs(hash(title)) % 0xFFFFFFFF), "title": title,
            "start": start.isoformat(), "end": (start + timedelta(hours=hours)).isoformat()}


def _future_day(days=3):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


# ---- 1. an unreadable calendar.json is never overwritten ----------------------------------
def test_unreadable_config_is_left_alone():
    day = _future_day()
    for label, content in (("not JSON", "{ nope"), ("a JSON list", "[1, 2]")):
        _reset()
        CONFIG.write_text(content, encoding="utf-8")
        before = CONFIG.read_bytes()
        results = {
            "link": cal.link_feed("work", "https://x.example/a.ics"),
            "enable": cal.set_feed_enabled("work", False),
            "remove": cal.remove_feed("work"),
            "add": cal.add_local_event({"title": "X", "date": day, "time": "10:00"}),
            "update": cal.update_local_event("ev_aaaa1111", {"title": "Y"}),
            "delete": cal.delete_local_event("ev_aaaa1111"),
            "tool": cal.tool_calendar_add_event({"title": "X", "when": day + "T10:00"}),
            "admin add": cal.admin("event-add", {"event": {"title": "X", "date": day, "time": "10:00"}}),
        }
        for name, out in results.items():
            check("%s: %s refuses" % (label, name), out.get("ok") is False and "nothing was changed" in str(out.get("error")), out)
        check("%s: the file is byte-for-byte untouched" % label, CONFIG.read_bytes() == before)
        check("%s: a plain READ still falls back to defaults" % label, cal.load_config()["feeds"] == [])

    _reset()
    check("a missing calendar.json is a fresh install", cal.load_config_strict()[1] is None)
    CONFIG.write_text("", encoding="utf-8")
    check("an empty calendar.json is a fresh install", cal.load_config_strict()[1] is None)
    out = cal.add_local_event({"title": "First", "date": day, "time": "10:00"})
    check("and a write then works", out.get("ok") is True and _cfg()["local_events"][0]["title"] == "First", out)

    _reset({"feeds": [{"name": "f", "url": "https://x.example/a.ics", "enabled": True}], "_note": "keep me"})
    cal.add_local_event({"title": "Keeps the rest", "date": day, "time": "10:00"})
    after = _cfg()
    check("a good write keeps feeds and underscore keys", after["feeds"][0]["name"] == "f" and after.get("_note") == "keep me", after)


# ---- 2. DATE vs DATE-TIME ----------------------------------------------------------------------
ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:a1
DTSTART;VALUE=DATE:20261012
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
UID:t1
DTSTART;VALUE=DATE-TIME:20261012T143000
DTEND;VALUE=DATE-TIME:20261012T153000
SUMMARY:Dentist
END:VEVENT
BEGIN:VEVENT
UID:t2
DTSTART;VALUE=DATE-TIME:20261013T090000
SUMMARY:No end
END:VEVENT
END:VCALENDAR
"""


def test_date_vs_date_time():
    events = cal.parse_ics(ICS, datetime(2026, 10, 1), datetime(2026, 10, 31))
    by = {e["uid"]: e for e in events}
    check("DATE-TIME keeps its time of day", by["t1"]["start"] == datetime(2026, 10, 12, 14, 30), by["t1"])
    check("and its own end", by["t1"]["end"] == datetime(2026, 10, 12, 15, 30), by["t1"])
    check("a DATE-TIME event with no end lasts an hour (not a day)", by["t2"]["end"] - by["t2"]["start"] == timedelta(hours=1), by["t2"])
    check("a date-only event is midnight and one day long",
          by["a1"]["start"] == datetime(2026, 10, 12) and by["a1"]["end"] - by["a1"]["start"] == timedelta(days=1), by["a1"])


# ---- 3. keep_days ---------------------------------------------------------------------------------
def test_keep_days():
    _reset()
    check("the default is 60", cal.retention_days() == 60)
    check("a fresh config carries the key", cal.load_config()["keep_days"] == 60)
    for label, value in (("zero", 0), ("negative", -5), ("text", "30"), ("a float", 30.5), ("a bool", True),
                         ("too big", 5000), ("none", None)):
        _reset({"keep_days": value})
        check("%s is ignored (60)" % label, cal.retention_days() == 60, value)
    for value in (1, 30, 120, 3650):
        _reset({"keep_days": value})
        check("%d is accepted" % value, cal.retention_days() == value)

    _reset({"keep_days": 30})
    check("the panel's stated limit follows it", cal.feeds_view()["retention_days"] == 30)
    day = _future_day()
    old = cal.add_local_event({"title": "Old", "date": (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d"), "time": "10:00"})
    check("an event older than keep_days is refused, naming the number", old["ok"] is False and "30" in old["error"], old)
    cfg = _cfg()
    cfg["local_events"] = [_stored("Ended 45 days ago", 45), _stored("Ended 10 days ago", 10)]
    CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
    out = cal.add_local_event({"title": "New", "date": day, "time": "10:00"})
    titles = [e["title"] for e in _cfg()["local_events"]]
    check("adding prunes by the setting", out["ok"] and titles == ["Ended 10 days ago", "New"], titles)

    cfg = _cfg()
    cfg["keep_days"] = 120
    cfg["local_events"] = [_stored("Ended 45 days ago", 45)]
    CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
    cal.add_local_event({"title": "New 2", "date": day, "time": "10:00"})
    check("a larger keep_days keeps it", "Ended 45 days ago" in [e["title"] for e in _cfg()["local_events"]])


# ---- 4. adding says what it removed -------------------------------------------------------------
def test_add_reports_pruning():
    day = _future_day()
    _reset({"local_events": [_stored("Ancient A", 100), _stored("Ancient B", 200), _stored("Recent", 5)]})
    out = cal.add_local_event({"title": "New", "date": day, "time": "10:00"})
    check("the panel's add reports how many old events went", out["ok"] and out.get("pruned") == 2, out)

    _reset({"local_events": [_stored("Recent", 5)]})
    out = cal.add_local_event({"title": "New", "date": day, "time": "10:00"})
    check("no 'pruned' key when nothing was removed", out["ok"] and "pruned" not in out, out)

    _reset({"local_events": [_stored("Ancient", 100)]})
    tool = cal.tool_calendar_add_event({"title": "Via chat", "when": day + "T10:00"})
    check("the model tool reports it too, in the result and in the note",
          tool.get("ok") and tool.get("pruned") == 1 and "1 older event" in tool["note"], tool)
    _reset()
    tool = cal.tool_calendar_add_event({"title": "Via chat", "when": day + "T10:00"})
    check("the tool's result is unchanged when nothing was removed",
          tool.get("ok") and "pruned" not in tool and tool["note"] == "Saved to Jarvis's own calendar. Subscribed feeds are read-only.", tool)


# ---- 5. a failed save is reported ------------------------------------------------------------------
def test_failed_save_is_reported():
    _reset()
    day = _future_day()
    real = cal.save_config
    cal.save_config = lambda cfg: False
    try:
        out = cal.tool_calendar_add_event({"title": "X", "when": day + "T10:00"})
        check("the model tool says the save failed instead of claiming success",
              out.get("ok") is False and "save" in out.get("error", "").lower(), out)
        out = cal.add_local_event({"title": "X", "date": day, "time": "10:00"})
        check("the panel add says so too", out.get("ok") is False and "save" in out.get("error", "").lower(), out)
    finally:
        cal.save_config = real


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
