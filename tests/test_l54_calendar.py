"""Master plan L.54 -- the Menu -> Calendars panel's back end (calendar_tools.admin).

The promise: the owner can see their events, link a calendar, manage Jarvis's own
events and set a reminder from a panel, and none of that makes the calendar less safe
than it was. Pinned here:

  1. masking: a linked calendar's link is hidden (host + a few characters, never the
     middle), and no response from the panel back end ever holds the real link;
  2. errors: a network error that quotes the address is scrubbed before it can reach the
     browser;
  3. checking a link: a real calendar reports what it found, a web page that is not a
     calendar, an empty reply, a bad scheme and an unreachable host are each refused with a
     plain reason, and nothing is saved by a check;
  4. linking: refuses a duplicate name, a link that is already linked, a thirteenth
     calendar and a link that fails its check; switching off / removing works and removing
     drops the cached copy;
  5. windows: events come from every source, sorted, with NO 40-event cap, all-day events
     are flagged, and only Jarvis's own events are editable (a feed named "local" is not
     mistaken for Jarvis's calendar); bad windows are refused;
  6. Jarvis's own events: add / edit / delete with validation, an all-day event, an edit
     keeps the id and merges only the fields sent, an old event is refused instead of being
     silently pruned, the 500 cap is refused instead of silently dropping, a linked or
     invalid id can never be edited or deleted;
  7. the model's calendar tools are exactly add / read / change / delete (never a feed), and
     nothing but cli.py reaches admin();
  8. a chat / scheduled / task process is refused outright, and the CLI verb prints one
     JSON object;
  9. an all-day event with no DTEND lasts one day (the panel's month view depends on it);
 10. the web layer: every /api/calendar route sits behind the same-origin gate, the link
     travels on stdin (never argv), and the page sends the header and never uses innerHTML.

Reminders are NOT part of this back end: "Remind me" uses the existing POST /api/scheduled
and only ever creates a job, because the scheduler has no operation that edits one. The
page-side planning of a reminder is pinned in tests/verify_calendar_panel.js.

Run: python3 tests/test_l54_calendar.py   (no key, no network)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-l54-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
_UNATTENDED = ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID", "JARVIS_CONTEXT")
for _var in _UNATTENDED:
    os.environ.pop(_var, None)

_ROOT = Path(__file__).resolve().parent.parent
_JARVIS_CLI = _ROOT / "jarvis-cli"
sys.path.insert(0, str(_JARVIS_CLI))

from jarvis.actions import calendar_tools as cal  # noqa: E402

PASS, FAIL = [], []
JDIR = Path(_HOME) / ".jarvis"

SECRET_PATH = "/calendar/ical/me%40example.com/private-9f8e7d6c5b4a39281706/basic.ics"
LINK = "https://calendar.example.com" + SECRET_PATH
LINK2 = "https://other.example.org/feeds/abcdef0123456789abcdef0123456789/work.ics"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)[:300]}")


def _reset():
    import shutil
    shutil.rmtree(JDIR, ignore_errors=True)
    JDIR.mkdir(parents=True, exist_ok=True)


def _ics(*events, extra=""):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN"]
    for ev in events:
        lines.append("BEGIN:VEVENT")
        lines.extend(ev)
        lines.append("END:VEVENT")
    lines.append(extra)
    lines.append("END:VCALENDAR")
    return "\r\n".join(l for l in lines if l is not None)


def _ev(uid, title, start, end=None, date_only=False):
    fmt = "%Y%m%d" if date_only else "%Y%m%dT%H%M%S"
    param = ";VALUE=DATE" if date_only else ""
    out = ["UID:%s" % uid, "SUMMARY:%s" % title, "DTSTART%s:%s" % (param, start.strftime(fmt))]
    if end is not None:
        out.append("DTEND%s:%s" % (param, end.strftime(fmt)))
    return out


class FakeNet:
    """Replaces calendar_tools.fetch_feed: a dict of link -> (body, error)."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, feed, force=False):
        url = (feed.get("url") or "").strip()
        self.calls.append((url, force))
        return self.table.get(url, ("", "connection refused"))


def _with_net(table):
    net = FakeNet(table)
    cal.fetch_feed = net
    return net


_REAL_FETCH = cal.fetch_feed


def _restore():
    cal.fetch_feed = _REAL_FETCH


def _future(days=1, hour=15, minute=0):
    d = datetime.now() + timedelta(days=days)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _dump(obj):
    return json.dumps(obj, default=str)


# ---- 1. masking ---------------------------------------------------------------------
def test_mask_url():
    masked = cal.mask_url(LINK)
    check("the host is kept", masked.startswith("https://calendar.example.com"), masked)
    check("the secret middle is not shown", "9f8e7d6c5b4a39281706" not in masked and "private" not in masked and "example.com/private" not in masked, masked)
    check("it is shorter than the link and shows an ellipsis", len(masked) < len(LINK) and "\u2026" in masked, masked)
    check("userinfo is dropped", "user:pw@" not in cal.mask_url("https://user:pw@host.example.com/" + "a" * 40))
    short = cal.mask_url("https://host.example.com/x.ics")
    check("a short path is hidden entirely", short == "https://host.example.com/\u2026", short)
    check("no path shows no tail", cal.mask_url("https://host.example.com") == "https://host.example.com")
    check("not a link is said so", cal.mask_url("hello") == "(not a link)")
    check("empty stays empty", cal.mask_url("") == "")
    check("the scheme is lower-cased", cal.mask_url("HTTPS://Host.example.com/" + "z" * 40).startswith("https://"))


def test_clean_link():
    check("webcal becomes https", cal._clean_link("webcal://h.example.com/a.ics") == "https://h.example.com/a.ics")
    check("quotes, spaces and brackets are removed", cal._clean_link('  "<https://h.example.com/a.ics>"  ') == "https://h.example.com/a.ics")
    check("the scheme is lower-cased", cal._clean_link("HTTPS://h.example.com/A.ics") == "https://h.example.com/A.ics")
    check("the path keeps its case", "/A.ics" in cal._clean_link("HTTPS://h.example.com/A.ics"))


# ---- 2. scrubbing ---------------------------------------------------------------------
def test_scrub():
    msg = ("HTTPSConnectionPool(host='calendar.example.com', port=443): Max retries exceeded with url: "
           + SECRET_PATH + " (Caused by NewConnectionError)")
    out = cal._scrub(msg, LINK)
    check("the secret path is gone from a requests-style error", "9f8e7d6c5b4a39281706" not in out and "private-" not in out, out)
    check("the useful part of the error stays", "Max retries exceeded" in out, out)
    out2 = cal._scrub("failed: " + LINK, LINK)
    check("a whole link is replaced", "calendar.example.com/calendar" not in out2 and "link hidden" in out2, out2)
    out3 = cal._scrub("see https://unrelated.example.net/secret/path now", LINK)
    check("any other link is replaced too", "secret/path" not in out3, out3)
    check("plain text is untouched", cal._scrub("HTTP 403", LINK) == "HTTP 403")


# ---- 3. checking ------------------------------------------------------------------------
def test_check_feed():
    _reset()
    body = _ics(_ev("a", "Standup", _future(1, 9)), _ev("b", "Lunch", _future(2, 12)))
    net = _with_net({LINK: (body, "")})
    try:
        ok = cal.check_feed(LINK)
        check("a real calendar is ok and counts its events", ok["ok"] and ok["events_found"] == 2, ok)
        check("it names the next event", ok["next_event"] and ok["next_event"]["title"] == "Standup", ok)
        check("a check always fetches fresh", net.calls and net.calls[-1][1] is True, net.calls)
        check("a check saves nothing", not cal.load_config()["feeds"])
        check("https is not flagged insecure", ok["insecure"] is False)
        http = "http://calendar.example.com/a.ics"
        net.table[http] = (body, "")
        check("an http link works but is flagged", cal.check_feed(http)["insecure"] is True)
        check("webcal:// is accepted (rewritten to https)", cal.check_feed("webcal://calendar.example.com" + SECRET_PATH)["ok"] is True)

        net.table["https://x.example.com/page"] = ("<html><body>Please sign in</body></html>", "")
        page = cal.check_feed("https://x.example.com/page")
        check("a web page is not a calendar", not page["ok"] and "not a calendar" in page["error"], page)
        net.table["https://x.example.com/empty"] = ("   ", "")
        check("an empty reply is refused", not cal.check_feed("https://x.example.com/empty")["ok"])
        net.table["https://x.example.com/bad"] = ("", "HTTP 403 for url: " + "/calendar/ical/secretthing/basic.ics")
        bad = cal.check_feed("https://x.example.com/bad")
        check("an unreachable link is refused with a reason", not bad["ok"] and "403" in bad["error"], bad)
        check("and the reason does not carry the address", "secretthing" not in bad["error"], bad)
        check("a missing link is asked for", "Paste" in cal.check_feed("")["error"])
        check("ftp:// is refused", not cal.check_feed("ftp://x.example.com/a.ics")["ok"])
        check("plain text is refused", not cal.check_feed("not a link")["ok"])
        none_upcoming = _ics(_ev("old", "Past", datetime.now() - timedelta(days=30)))
        net.table["https://x.example.com/old"] = (none_upcoming, "")
        old = cal.check_feed("https://x.example.com/old")
        check("a calendar with nothing coming up is still ok", old["ok"] and old["next_event"] is None and old["events_found"] == 1, old)
    finally:
        _restore()


# ---- 4. linking ---------------------------------------------------------------------------
def test_link_enable_remove():
    _reset()
    body = _ics(_ev("a", "Standup", _future(1, 9)))
    _with_net({LINK: (body, ""), LINK2: (body, "")})
    try:
        bad = cal.link_feed("", LINK)
        check("a name is required", not bad["ok"] and "name" in bad["error"].lower(), bad)
        done = cal.link_feed("  Work   cal ", LINK)
        check("linking works", done["ok"], done)
        check("the name is tidied", done["name"] == "Work cal", done)
        check("the result carries the masked link only", LINK not in _dump(done) and "9f8e7d6c5b4a39281706" not in _dump(done), done)
        check("it is saved enabled", cal.load_config()["feeds"] == [{"name": "Work cal", "url": LINK, "enabled": True}], cal.load_config()["feeds"])
        dup_name = cal.link_feed("work CAL", LINK2)
        check("a duplicate name is refused (case-insensitive)", not dup_name["ok"] and "already" in dup_name["error"], dup_name)
        dup_link = cal.link_feed("Other", "webcal://calendar.example.com" + SECRET_PATH)
        check("the same link under another name is refused", not dup_link["ok"] and "Work cal" in dup_link["error"], dup_link)
        check("a refused link is not saved", len(cal.load_config()["feeds"]) == 1)
        failing = cal.link_feed("Broken", "https://nowhere.example.com/a.ics")
        check("a link that fails its check is not saved", not failing["ok"] and len(cal.load_config()["feeds"]) == 1, failing)
        check("a second calendar links", cal.link_feed("Other", LINK2)["ok"] and len(cal.load_config()["feeds"]) == 2)

        off = cal.set_feed_enabled("Work cal", False)
        check("switching off works", off["ok"] and cal.load_config()["feeds"][0]["enabled"] is False, off)
        view = cal.feeds_view()
        check("the view reports it off", [f["enabled"] for f in view["feeds"]] == [False, True], view)
        check("switching a missing calendar off is refused", not cal.set_feed_enabled("Nope", True)["ok"])

        cal._save_cache({LINK: {"body": body, "at": "2026-01-01T00:00:00"}, LINK2: {"body": body, "at": "2026-01-01T00:00:00"}})
        gone = cal.remove_feed("Work cal")
        check("removing works", gone["ok"] and [f["name"] for f in cal.load_config()["feeds"]] == ["Other"], gone)
        check("removing drops that calendar's cached copy and keeps the other's", LINK not in cal._load_cache() and LINK2 in cal._load_cache(), list(cal._load_cache()))
        check("removing a missing calendar is refused", not cal.remove_feed("Work cal")["ok"])

        cfg = cal.load_config()
        cfg["feeds"] = [{"name": "F%d" % i, "url": "https://h%d.example.com/a.ics" % i, "enabled": True} for i in range(cal.MAX_FEEDS)]
        cal.save_config(cfg)
        full = cal.link_feed("One too many", LINK)
        check("the calendar cap is enforced", not full["ok"] and str(cal.MAX_FEEDS) in full["error"], full)
    finally:
        _restore()


def test_feeds_view_never_holds_the_link():
    _reset()
    cfg = cal.load_config()
    cfg["feeds"] = [{"name": "Work", "url": LINK, "enabled": True}]
    cal.save_config(cfg)
    view = cal.feeds_view()
    text = _dump(view)
    check("the link is not in the feeds view", LINK not in text and "9f8e7d6c5b4a39281706" not in text and "private-" not in text, text)
    check("the masked link is", view["feeds"][0]["url_masked"].startswith("https://calendar.example.com"), view)
    check("the limits are stated", view["retention_days"] == 60 and view["retention_max"] == 500 and view["max_feeds"] == cal.MAX_FEEDS, view)
    check("the default reminder lead is passed on", view["default_reminder_minutes"] == 60, view)


# ---- 5. windows ------------------------------------------------------------------------------
def test_window_view():
    _reset()
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    many = [_ev("m%d" % i, "Event %d" % i, start + timedelta(days=1, hours=i % 20, minutes=i)) for i in range(60)]
    allday = _ev("ad", "Holiday", start + timedelta(days=5), start + timedelta(days=6), date_only=True)
    feed_body = _ics(*many, allday)
    _with_net({LINK: (feed_body, "")})
    try:
        cfg = cal.load_config()
        cfg["feeds"] = [{"name": "local", "url": LINK, "enabled": True}]  # a feed that is NAMED local
        cal.save_config(cfg)
        added = cal.add_local_event({"title": "Mine", "date": (start + timedelta(days=2)).strftime("%Y-%m-%d"), "time": "10:00", "duration_minutes": 30})
        check("a local event is added", added["ok"], added)

        win = cal.window_view(start.strftime("%Y-%m-%d"), (start + timedelta(days=10)).strftime("%Y-%m-%d"))
        check("the window answers ok", win["ok"], win)
        titles = [e["title"] for e in win["events"]]
        check("there is no 40-event cap", len(win["events"]) >= 62, len(win["events"]))
        check("events are sorted by start", [e["start"] for e in win["events"]] == sorted(e["start"] for e in win["events"]))
        by_title = {e["title"]: e for e in win["events"]}
        check("an all-day event is flagged all-day", by_title["Holiday"]["all_day"] is True, by_title.get("Holiday"))
        check("a timed event is not", by_title["Event 0"]["all_day"] is False)
        check("Jarvis's own event is local, editable and labelled", by_title["Mine"]["local"] and by_title["Mine"]["editable"] and by_title["Mine"]["source"] == "Jarvis calendar", by_title["Mine"])
        check("a feed named 'local' is NOT treated as Jarvis's calendar", by_title["Event 0"]["local"] is False and by_title["Event 0"]["editable"] is False and by_title["Event 0"]["source"] == "local", by_title["Event 0"])
        check("a linked event id is not an event id", not cal._EVENT_ID_RE.match(by_title["Event 0"]["id"]), by_title["Event 0"]["id"])
        check("the window reply never holds the link", LINK not in _dump(win) and "9f8e7d6c5b4a39281706" not in _dump(win))
        check("it reports the enabled feed count", win["feed_count"] == 1, win)

        narrow = cal.window_view((start + timedelta(days=2)).strftime("%Y-%m-%d"), (start + timedelta(days=3)).strftime("%Y-%m-%d"))
        check("a narrow window holds only its day", [e["title"] for e in narrow["events"]] == ["Mine"], [e["title"] for e in narrow["events"]])

        cal.set_feed_enabled("local", False)
        off = cal.window_view(start.strftime("%Y-%m-%d"), (start + timedelta(days=10)).strftime("%Y-%m-%d"))
        check("a switched-off calendar contributes nothing", [e["title"] for e in off["events"]] == ["Mine"], [e["title"] for e in off["events"]])
        check("and is not counted", off["feed_count"] == 0)

        check("an end before the start is refused", not cal.window_view("2026-10-10", "2026-10-09")["ok"])
        check("a window over the limit is refused", not cal.window_view("2026-01-01", "2028-01-01")["ok"])
        check("a bad date is refused", not cal.window_view("soon", "later")["ok"])
        check("a missing bound is refused", not cal.window_view("", "2026-10-10")["ok"])
        check("a timestamp bound is accepted", cal.window_view("2026-10-10T00:00:00", "2026-10-11T00:00:00")["ok"])
    finally:
        _restore()


def test_window_problems_are_scrubbed():
    _reset()
    _with_net({LINK: ("", "HTTPSConnectionPool: Max retries exceeded with url: " + SECRET_PATH)})
    try:
        cfg = cal.load_config()
        cfg["feeds"] = [{"name": "Work", "url": LINK, "enabled": True}]
        cal.save_config(cfg)
        win = cal.window_view("2026-10-10", "2026-10-17")
        check("a feed problem is reported", win["ok"] and win["problems"] and win["problems"][0].startswith("Work:"), win)
        check("and carries no part of the secret address", "9f8e7d6c5b4a39281706" not in _dump(win) and "private-" not in _dump(win), win["problems"])
    finally:
        _restore()


def test_all_day_without_dtend_is_one_day():
    day = datetime(2026, 11, 3)
    body = _ics(_ev("x", "Birthday", day, None, date_only=True))
    parsed = cal.parse_ics(body)
    check("one event parsed", len(parsed) == 1, parsed)
    check("a date-only start with no end lasts one day", parsed[0]["end"] - parsed[0]["start"] == timedelta(days=1), parsed[0])
    timed = cal.parse_ics(_ics(_ev("y", "Call", datetime(2026, 11, 3, 14, 0), None)))
    check("a timed event with no end still lasts an hour", timed[0]["end"] - timed[0]["start"] == timedelta(hours=1), timed[0])
    check("a day-long event is flagged all-day", cal._is_all_day(parsed[0]["start"], parsed[0]["end"]))
    check("a midnight-to-1am event is not", not cal._is_all_day(datetime(2026, 11, 3), datetime(2026, 11, 3, 1)))


# ---- 6. Jarvis's own events ----------------------------------------------------------------------
def test_local_events():
    _reset()
    day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    for label, fields, word in [
        ("a title is required", {"date": day, "time": "10:00"}, "title"),
        ("a blank title is refused", {"title": "   ", "date": day, "time": "10:00"}, "title"),
        ("a date is required", {"title": "X", "time": "10:00"}, "date"),
        ("a bad date is refused", {"title": "X", "date": "12/10/2026", "time": "10:00"}, "date"),
        ("a time is required unless all-day", {"title": "X", "date": day}, "time"),
        ("a bad time is refused", {"title": "X", "date": day, "time": "25:99"}, "time"),
        ("a bad duration is refused", {"title": "X", "date": day, "time": "10:00", "duration_minutes": "lots"}, "duration"),
    ]:
        out = cal.add_local_event(fields)
        check(label, not out["ok"] and word in out["error"].lower(), out)
    check("nothing was saved by the refusals", cal.load_config()["local_events"] == [])

    one = cal.add_local_event({"title": "  Dentist ", "date": day, "time": "15:30", "duration_minutes": 45, "location": "Main St", "description": "bring forms"})
    check("a good event is added", one["ok"], one)
    ev = one["event"]
    check("it has an ev_ id and is editable", bool(cal._EVENT_ID_RE.match(ev["id"])) and ev["editable"], ev)
    check("title is trimmed, times are right", ev["title"] == "Dentist" and ev["start"] == day + "T15:30:00" and ev["end"] == day + "T16:15:00", ev)
    check("location and notes are kept", ev["location"] == "Main St" and ev["description"] == "bring forms", ev)

    clamped = cal.add_local_event({"title": "Long", "date": day, "time": "08:00", "duration_minutes": 99999})
    check("a huge duration is clamped to a day", clamped["ok"] and clamped["event"]["end"] == (datetime.fromisoformat(day + "T08:00:00") + timedelta(minutes=1440)).isoformat(), clamped)
    tiny = cal.add_local_event({"title": "Tiny", "date": day, "time": "08:00", "duration_minutes": 1})
    check("a tiny duration is raised to five minutes", tiny["ok"] and tiny["event"]["end"] == day + "T08:05:00", tiny)

    allday = cal.add_local_event({"title": "Trip", "date": day, "all_day": True})
    check("an all-day event needs no time", allday["ok"] and allday["event"]["all_day"] is True, allday)
    check("it runs midnight to the next midnight", allday["event"]["start"] == day + "T00:00:00" and allday["event"]["end"] == (datetime.fromisoformat(day) + timedelta(days=1)).isoformat(), allday)

    # edit: only the fields sent change
    upd = cal.update_local_event(ev["id"], {"title": "Dentist (moved)", "time": "16:00"})
    check("an edit works", upd["ok"], upd)
    check("the id is kept", upd["event"]["id"] == ev["id"])
    check("the fields sent changed", upd["event"]["title"] == "Dentist (moved)" and upd["event"]["start"] == day + "T16:00:00", upd["event"])
    check("the duration was kept", upd["event"]["end"] == day + "T16:45:00", upd["event"])
    check("location and notes were kept", upd["event"]["location"] == "Main St" and upd["event"]["description"] == "bring forms", upd["event"])
    check("an edit does not add a second event", len([e for e in cal.load_config()["local_events"] if e["id"] == ev["id"]]) == 1)
    to_all = cal.update_local_event(ev["id"], {"all_day": True})
    check("a timed event can become all-day", to_all["ok"] and to_all["event"]["all_day"] is True, to_all)
    back = cal.update_local_event(ev["id"], {"all_day": False, "time": "09:00", "duration_minutes": 30})
    check("and back again", back["ok"] and back["event"]["all_day"] is False and back["event"]["end"] == day + "T09:30:00", back)
    check("an edit that is invalid is refused and changes nothing", not cal.update_local_event(ev["id"], {"title": ""})["ok"] and cal.load_config()["local_events"][0]["title"] == "Dentist (moved)")

    # ids that must never work
    for bad in ["f_0123456789", "ev_zzzz", "../../etc", "", None, "ev_00000000"]:
        up = cal.update_local_event(bad, {"title": "x"})
        de = cal.delete_local_event(bad)
        check("edit and delete refuse the id %r" % (bad,), not up["ok"] and not de["ok"], (up, de))

    gone = cal.delete_local_event(ev["id"])
    check("delete works", gone["ok"] and gone["removed"] == ev["id"], gone)
    check("the event is gone", all(e["id"] != ev["id"] for e in cal.load_config()["local_events"]))
    check("deleting it again says so", not cal.delete_local_event(ev["id"])["ok"])


def test_retention_and_cap_are_refused_not_silent():
    _reset()
    old_day = (datetime.now() - timedelta(days=cal.LOCAL_RETENTION_DAYS + 5)).strftime("%Y-%m-%d")
    old = cal.add_local_event({"title": "Ancient", "date": old_day, "time": "10:00"})
    check("an event older than the retention is refused, not silently dropped", not old["ok"] and str(cal.LOCAL_RETENTION_DAYS) in old["error"], old)
    recent_day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    check("a recent past event is allowed", cal.add_local_event({"title": "Last week", "date": recent_day, "time": "10:00"})["ok"])

    cfg = cal.load_config()
    future = (datetime.now() + timedelta(days=5)).replace(microsecond=0)
    cfg["local_events"] = [{"id": "ev_%08x" % i, "title": "E%d" % i, "start": future.isoformat(), "end": (future + timedelta(hours=1)).isoformat()} for i in range(cal.LOCAL_MAX_EVENTS)]
    cal.save_config(cfg)
    full = cal.add_local_event({"title": "One more", "date": future.strftime("%Y-%m-%d"), "time": "10:00"})
    check("the 500 cap is refused with the number, not a silent drop", not full["ok"] and str(cal.LOCAL_MAX_EVENTS) in full["error"], full)
    check("nothing was dropped to make room", len(cal.load_config()["local_events"]) == cal.LOCAL_MAX_EVENTS)


def test_model_tool_behaviour_is_unchanged():
    _reset()
    out = cal.tool_calendar_add_event({"title": "Dentist", "when": "tomorrow at 3pm", "duration_minutes": 45, "location": "Main St"})
    check("the model tool still adds an event", out.get("ok") and out["id"].startswith("ev_"), out)
    check("and it is stored", cal.load_config()["local_events"][0]["title"] == "Dentist")
    check("and still prunes through the shared helper", callable(cal._prune_local))
    cfg = cal.load_config()
    cfg["local_events"].append({"id": "ev_deadbeef", "title": "Ancient", "start": "2020-01-01T10:00:00", "end": "2020-01-01T11:00:00"})
    cal._prune_local(cfg)
    check("prune drops an old event and keeps a recent one", [e["title"] for e in cfg["local_events"]] == ["Dentist"], cfg["local_events"])
    listed = cal.tool_calendar_events({"days": 7})
    check("the model's list still includes it", any(e["title"] == "Dentist" for e in listed["events"]), listed)


# ---- 7. the model cannot reach the panel back end ---------------------------------------------------------
def test_model_cannot_reach_admin():
    four = ["calendar_add_event", "calendar_delete_event", "calendar_events", "calendar_update_event"]
    check("exactly the four calendar tools (add, read, change, delete)", sorted(cal.TOOLS) == four, sorted(cal.TOOLS))
    check("and exactly those four schemas", sorted(s["name"] for s in cal.TOOL_SCHEMAS) == four)
    from jarvis import tools
    names = [n.lower() for n in tools.TOOLS if "calendar" in n.lower()]
    check("no other model tool is about calendars (none can link or remove a feed)", sorted(names) == four, names)
    offenders = []
    for path in sorted((_JARVIS_CLI / "jarvis").rglob("*.py")):
        if path.name in ("calendar_tools.py", "cli.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"calendar_tools\.(admin|link_feed|add_feed_unverified|set_settings|add_local_event|update_local_event|delete_local_event|remove_feed|set_feed_enabled|check_feed)", text) \
                or "calendar-admin" in text.replace("reserved_names", ""):
            offenders.append(path.name)
    check("only cli.py (and reserved_names.py, which just lists the name) reaches the panel back end", offenders in ([], ["reserved_names.py"]), offenders)
    from jarvis import reserved_names
    check("calendar-admin is a reserved command name", "calendar-admin" in reserved_names.RESERVED_NAMES)
    check("it is a registered op list", "event-delete" in cal.ADMIN_OPS and "link" in cal.ADMIN_OPS)


# ---- 8. unattended + the CLI -------------------------------------------------------------------------------------
def test_unattended_is_refused():
    _reset()
    for var, value in [("JARVIS_CHANNEL", "discord"), ("JARVIS_CHANNEL_SENDER", "123"), ("JARVIS_SCHEDULED", "1"),
                       ("JARVIS_TASK_ID", "t1"), ("JARVIS_CONTEXT", "scheduled"), ("JARVIS_CONTEXT", "unattended")]:
        os.environ[var] = value
        try:
            for op in cal.ADMIN_OPS:
                out = cal.admin(op, {"url": LINK, "name": "x", "event": {"title": "x"}, "id": "ev_00000000"})
                if not (out.get("ok") is False and out.get("code") == "unattended"):
                    check("%s=%s refuses %s" % (var, value, op), False, out)
                    break
            else:
                check("%s=%s refuses every op" % (var, value), True)
        finally:
            os.environ.pop(var, None)
    check("a refused call changed nothing", cal.load_config()["feeds"] == [] and cal.load_config()["local_events"] == [])
    check("an ordinary interactive context is allowed", cal.admin("feeds")["ok"] is True)
    check("JARVIS_CONTEXT=interactive is not mistaken for unattended", cal.unattended_reason() is None)


def test_admin_dispatch_is_safe():
    _reset()
    check("an unknown op says the usage", cal.admin("explode", {})["ok"] is False and "usage" in cal.admin("explode", {})["error"])
    check("a non-dict payload is treated as empty", cal.admin("feeds", "oops")["ok"] is True)
    check("a payload of the wrong shape never raises", cal.admin("event-add", {"event": "not a dict"})["ok"] is False)
    check("link with nothing is a refusal, not a crash", cal.admin("link", {})["ok"] is False)
    check("feed-enable needs the flag to be exactly true", cal.admin("feed-enable", {"name": "x", "enabled": "yes"})["ok"] is False)


def _env(**extra):
    env = dict(os.environ)
    env["HOME"] = _HOME
    env["USERPROFILE"] = _HOME
    for var in _UNATTENDED:
        env.pop(var, None)
    env.update(extra)
    return env


def _cli(op, payload=None, **env):
    proc = subprocess.run([sys.executable, "-m", "jarvis", "calendar-admin", op],
                          input=None if payload is None else json.dumps(payload),
                          capture_output=True, text=True, env=_env(**env), timeout=120, cwd=str(_JARVIS_CLI))
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"_raw": proc.stdout[-300:], "_err": proc.stderr[-300:]}


def test_cli_verb():
    _reset()
    code, out = _cli("feeds")
    check("feeds prints one JSON object", code == 0 and out.get("ok") is True and out.get("feeds") == [], (code, out))
    day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    code, added = _cli("event-add", {"event": {"title": "From the CLI", "date": day, "time": "11:00"}})
    check("event-add reads one JSON object on stdin", code == 0 and added.get("ok") is True, (code, added))
    code, win = _cli("window", {"start": day, "end": (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")})
    check("window returns it", code == 0 and [e["title"] for e in win.get("events", [])] == ["From the CLI"], (code, win))
    code, bad = _cli("event-delete", {"id": "ev_00000000"})
    check("a refusal is a JSON object, not a crash", bad.get("ok") is False and "error" in bad, (code, bad))
    proc = subprocess.run([sys.executable, "-m", "jarvis", "calendar-admin", "link"], input="not json", capture_output=True, text=True,
                          env=_env(), timeout=120, cwd=str(_JARVIS_CLI))
    check("a body that is not JSON is refused", '"ok": false' in proc.stdout and "JSON" in proc.stdout, proc.stdout[-200:])
    code, blocked = _cli("event-add", {"event": {"title": "Sneaky", "date": day, "time": "11:00"}}, JARVIS_CHANNEL="discord")
    check("the CLI refuses inside a chat reply", blocked.get("ok") is False and blocked.get("code") == "unattended", (code, blocked))
    code, still = _cli("window", {"start": day, "end": (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")})
    check("and the refused write did not happen", [e["title"] for e in still.get("events", [])] == ["From the CLI"], still)


# ---- 10. the web layer ------------------------------------------------------------------------------------------------
def test_web_layer_is_gated():
    server = (_ROOT / "web" / "server.js").read_text(encoding="utf-8")
    routes = re.findall(r'app\.(get|post|put|delete)\("(/api/calendar[^"]*)"([^\n]*)', server)
    check("the calendar routes exist", len(routes) >= 9, routes)
    for verb, path, rest in routes:
        check("%s %s is behind requireCalendarOrigin and requireJarvis" % (verb.upper(), path), "requireCalendarOrigin" in rest and "requireJarvis" in rest, rest[:120])
    check("the gate wants the loopback host, the same origin and the header",
          "LOOPBACK_HOST_RE.test(hostName)" in server and 'req.headers["x-jarvis-calendar"] !== "1"' in server and "String(PORT)" in server)
    helper = server[server.index("async function calendarAdmin"):server.index("function calendarText")]
    check("the link travels on stdin, as JSON", "JSON.stringify(payload)" in helper and 'runJarvisOnce(["calendar-admin", op]' in helper, helper)
    check("no calendar route puts a link or name in argv", not re.search(r'runJarvisOnce\(\[[^\]]*calendar-admin[^\]]*,\s*(req|url|name)', server))
    check("event ids are validated before use", "CALENDAR_EVENT_ID_RE" in server and server.count("CALENDAR_EVENT_ID_RE.test(id)") >= 2)
    check("an unattended refusal is a 403", "unattended: 403" in server)
    check("the event body is whitelisted", 'for (const key of ["title", "date", "time", "location", "description"])' in server)


def test_page_layer_is_safe():
    js = (_ROOT / "web" / "public" / "calendar.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    check("the page never uses innerHTML / insertAdjacentHTML / document.write", not re.search(r"innerHTML|insertAdjacentHTML|document\.write|outerHTML", code))
    check("every request carries the calendar header", '"X-Jarvis-Calendar": "1"' in js)
    check("the page offers no way to edit a reminder", "/api/scheduled/" not in code and 'put("/api/scheduled' not in code and "patch" not in code.lower().replace("dispatch", ""))
    check("a reminder is only ever created (POST /api/scheduled, kind reminder)", '"/api/scheduled", { when: plan.when, message: plan.message, kind: "reminder" }' in js)
    check("the pasted link is dropped when the guide closes", "state.wizard = null" in js and 'w.url = ""' in js)
    html = (_ROOT / "web" / "public" / "index.html").read_text(encoding="utf-8")
    check("the markup, the menu item and the script are present", all(x in html for x in ('id="calendar-overlay"', 'id="menu-item-calendar"', 'src="calendar.js"', 'href="calendar.css"')))
    appjs = (_ROOT / "web" / "public" / "app.js").read_text(encoding="utf-8")
    check("the menu item opens the panel", "menu-item-calendar" in appjs and "JarvisCalendar" in appjs)


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
            finally:
                _restore()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
