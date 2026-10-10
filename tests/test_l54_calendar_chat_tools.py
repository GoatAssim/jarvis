"""Master plan L.54 / Q-L54m -- change and delete events on Jarvis's own calendar from chat.

OPTIONAL patch (jarvis-L54-chat-event-tools.patch). Pins:

  1. the tools exist with schemas, only delete asks first, the loader registers that, and
     none of the four is about a feed;
  2. calendar_events gives an id to events on Jarvis's own calendar and to nothing else;
  3. calendar_update_event: a title, a move (length kept), a new length, clearing a
     location, an all-day event moved stays all-day (a length makes it timed), nothing to
     change / no id / a time it can't read all ask instead of guessing, a linked-calendar
     id is refused with the reason;
  4. calendar_delete_event: removes the event, refuses a second delete and a linked id;
  5. both refuse (and leave the file alone) when calendar.json cannot be read.

Run: python3 tests/test_l54_calendar_chat_tools.py   (no key, no network)
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l54c-home-")
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


def _stored():
    return json.loads(CONFIG.read_text(encoding="utf-8")).get("local_events", [])


def _day(days=3):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _add(title="Dentist", when=None, minutes=60):
    out = cal.tool_calendar_add_event({"title": title, "when": when or (_day() + "T15:00"), "duration_minutes": minutes})
    assert out.get("ok"), out
    return out["id"]


def test_registration():
    names = ["calendar_add_event", "calendar_delete_event", "calendar_events", "calendar_update_event"]
    check("exactly the four calendar tools", sorted(cal.TOOLS) == names, sorted(cal.TOOLS))
    check("a schema for each", sorted(s["name"] for s in cal.TOOL_SCHEMAS) == names)
    check("none of them is about a feed", not [n for n in cal.TOOLS if "feed" in n or "link" in n])
    check("the module declares only delete as confirm-required", cal.TOOL_CONFIRM_REQUIRED == {"calendar_delete_event"}, cal.TOOL_CONFIRM_REQUIRED)
    from jarvis import tools, tool_safety  # noqa: F401  (importing tools registers the module's flags)
    check("the loader registered delete as confirm-required", "calendar_delete_event" in tool_safety.DEFAULT_CONFIRM_REQUIRED)
    check("update and add are not", not {"calendar_update_event", "calendar_add_event"} & set(tool_safety.DEFAULT_CONFIRM_REQUIRED))
    check("both new tools have keywords", all(cal.TOOL_KEYWORDS.get(n) for n in ("calendar_update_event", "calendar_delete_event")))
    check("both are in the calendar group and named in tools.TOOLS",
          cal.TOOL_GROUP == "calendar" and {"calendar_update_event", "calendar_delete_event"} <= set(tools.TOOLS))


def test_ids_in_the_list():
    _reset()
    eid = _add()
    original = cal.fetch_feed
    cal.fetch_feed = lambda feed, force=False: ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:feed-1\nDTSTART:%sT100000\nDTEND:%sT110000\nSUMMARY:Linked\nEND:VEVENT\nEND:VCALENDAR\n"
                                                % (_day(2).replace("-", ""), _day(2).replace("-", "")), "")
    try:
        cfg = cal.load_config()
        cfg["feeds"] = [{"name": "work", "url": "https://x.example/a.ics", "enabled": True}]
        cal.save_config(cfg)
        listed = cal.tool_calendar_events({"days": 14})
    finally:
        cal.fetch_feed = original
    by = {e["title"]: e for e in listed["events"]}
    check("Jarvis's own event carries its id", by["Dentist"].get("id") == eid, by)
    check("a linked event carries none", "id" not in by["Linked"], by)


def test_update():
    _reset()
    day = _day()
    eid = _add("Dentist", day + "T15:00", 60)
    out = cal.tool_calendar_update_event({"id": eid, "title": "Dentist (check-up)"})
    check("a rename works and nothing else moves", out.get("ok") and _stored()[0]["title"] == "Dentist (check-up)"
          and _stored()[0]["start"] == day + "T15:00:00" and _stored()[0]["end"] == day + "T16:00:00", out)
    out = cal.tool_calendar_update_event({"id": eid, "when": day + "T16:30"})
    check("moving keeps the length", out.get("ok") and _stored()[0]["start"] == day + "T16:30:00" and _stored()[0]["end"] == day + "T17:30:00", (out, _stored()))
    check("the result says when", "when" in out and out["id"] == eid, out)
    out = cal.tool_calendar_update_event({"id": eid, "duration_minutes": 30})
    check("a new length keeps the start", out.get("ok") and _stored()[0]["start"] == day + "T16:30:00" and _stored()[0]["end"] == day + "T17:00:00", _stored())
    cal.tool_calendar_update_event({"id": eid, "location": "Main St"})
    out = cal.tool_calendar_update_event({"id": eid, "location": ""})
    check("an empty location clears it", out.get("ok") and _stored()[0]["location"] == "", _stored())

    check("no id asks which event", cal.tool_calendar_update_event({"title": "x"}).get("needs_clarification") is True)
    check("nothing to change asks", cal.tool_calendar_update_event({"id": eid}).get("needs_clarification") is True)
    check("a time it can't read asks, and changes nothing",
          cal.tool_calendar_update_event({"id": eid, "when": "when the cows come home"}).get("needs_clarification") is True
          and _stored()[0]["start"] == day + "T16:30:00")
    out = cal.tool_calendar_update_event({"id": "feed-uid-123", "title": "x"})
    check("a linked event's id is refused with the reason", out.get("ok") is False and "Only Jarvis's own" in out["error"], out)
    out = cal.tool_calendar_update_event({"id": "ev_deadbeef", "title": "x"})
    check("an unknown ev_ id is refused", out.get("ok") is False, out)

    # all-day
    created = cal.add_local_event({"title": "Trip", "date": day, "all_day": True})
    tid = created["event"]["id"]
    other = _day(6)
    out = cal.tool_calendar_update_event({"id": tid, "when": other + "T09:00"})
    trip = [e for e in _stored() if e["id"] == tid][0]
    check("an all-day event moved to another day stays all-day",
          out.get("ok") and out.get("all_day") is True and trip["start"] == other + "T00:00:00" and trip["end"].startswith(_day(7)), (out, trip))
    out = cal.tool_calendar_update_event({"id": tid, "when": other + "T09:00", "duration_minutes": 90})
    trip = [e for e in _stored() if e["id"] == tid][0]
    check("giving a length makes it a timed event at that time",
          out.get("ok") and not out.get("all_day") and trip["start"] == other + "T09:00:00" and trip["end"] == other + "T10:30:00", (out, trip))


def test_delete():
    _reset()
    eid = _add()
    other = _add("Keep me")
    out = cal.tool_calendar_delete_event({"id": eid})
    check("delete removes it and says what", out.get("ok") and out["deleted"] == "Dentist" and out["id"] == eid, out)
    check("the other event is untouched", [e["title"] for e in _stored()] == ["Keep me"] and _stored()[0]["id"] == other)
    check("deleting twice is a refusal", cal.tool_calendar_delete_event({"id": eid}).get("ok") is False)
    out = cal.tool_calendar_delete_event({"id": "feed-uid-123"})
    check("a linked event's id is refused with the reason", out.get("ok") is False and "Only Jarvis's own" in out["error"], out)
    check("no id asks which event", cal.tool_calendar_delete_event({}).get("needs_clarification") is True)


def test_unreadable_config():
    _reset()
    eid = _add()
    CONFIG.write_text("{ nope", encoding="utf-8")
    before = CONFIG.read_bytes()
    up = cal.tool_calendar_update_event({"id": eid, "title": "x"})
    de = cal.tool_calendar_delete_event({"id": eid})
    check("update refuses on an unreadable file", up.get("ok") is False and "nothing was changed" in up.get("error", ""), up)
    check("delete refuses on an unreadable file", de.get("ok") is False and "nothing was changed" in de.get("error", ""), de)
    check("the file is untouched", CONFIG.read_bytes() == before)


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
