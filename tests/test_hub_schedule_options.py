"""Download Hub: timed starts, the global concurrency number, and notifications wired live to the notifier.

Run: python tests/test_hub_schedule_options.py
"""
import datetime
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-sched-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import notifier  # noqa: E402
from jarvis.hub import service, settings as hs  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
URL = "http://93.184.216.34/file%d.zip"


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


def fresh(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / ("h%d.db" % _n[0]), clock=clock or time.time)


def test_parse():
    now = datetime.datetime(2026, 10, 18, 12, 0)
    got, err = service.parse_start("in 2 hours", now=now)
    check("'in 2 hours' parses to two hours ahead", err is None and got["iso"] == "2026-10-18T14:00:00")
    check("tomorrow at 3am", service.parse_start("tomorrow at 3am", now=now)[0]["iso"] == "2026-10-19T03:00:00")
    check("a past time is refused", "passed" in (service.parse_start("2026-10-01 09:00", now=now)[1] or ""))
    check("a repeat is refused", "once" in (service.parse_start("every night", now=now)[1] or ""))
    check("nonsense is refused", service.parse_start("whenever", now=now)[1] is not None)
    check("more than a year away is refused", service.parse_start("2029-01-01 09:00", now=now)[1] is not None)


def test_schedule_flow():
    t = [1000.0]
    store = fresh(clock=lambda: t[0])
    res = service.add_items([URL % 1], start_at="in 2 hours", store=store)
    check("dl_add with start_at queues it paused with a start time", res["ok"] and res["added"][0]["state"] == "paused" and res["added"][0]["start_at"])
    iid = res["added"][0]["id"]
    item = store.get_item(iid)
    check("the row carries the marker and the time", service.scheduled_at(item) is not None and item["options"].get("start_at"))
    row = service.present_item(item, {}, store.now())
    check("the panel row says scheduled, can be scheduled, has no retry countdown", row["scheduled_at"] and row["can_schedule"] and row["retry_in_s"] is None)
    check("it counts as scheduled in the summary", service.summary(store)["scheduled"]["count"] == 1)
    check("nothing is due yet", store.due_scheduled(store.now()) == [])
    # bulk resume and pause leave it alone
    r = service.control("resume", all=True, store=store)
    check("'resume all' does not undo a start time", store.get_item(iid)["state"] == "paused" and r["skipped"])
    check("resume on it alone starts it now and drops the schedule",
          service.control("resume", id=iid, store=store)["changed"] == [iid]
          and store.get_item(iid)["state"] == "queued" and not store.get_item(iid)["options"].get("start_at"))
    # schedule a queued one, then the daemon wakes it
    r = service.schedule_items("in 1 hour", id=iid, store=store)
    check("a queued download can be scheduled", r["ok"] and store.get_item(iid)["state"] == "paused")
    when = service.scheduled_at(store.get_item(iid))
    check("the wake-up does nothing before the time", service.wake_scheduled(store, when - 5) == [])
    check("the wake-up queues it once the time has come", service.wake_scheduled(store, when + 1) == [iid]
          and store.get_item(iid)["state"] == "queued" and not store.get_item(iid)["options"].get("start_at"))
    # a daemon that was down starts it on its next tick
    service.schedule_items("in 1 hour", id=iid, store=store)
    far = service.scheduled_at(store.get_item(iid)) + 86400
    d = HubDaemon(store=store, clock=lambda: far)
    d._wake_scheduled()
    check("the daemon's tick queues an overdue one", store.get_item(iid)["state"] == "queued")
    # clear, errors
    service.schedule_items("in 1 hour", id=iid, store=store)
    r = service.schedule_items("clear", id=iid, store=store)
    check("clear removes the time and leaves it paused", r["ok"] and store.get_item(iid)["state"] == "paused" and service.scheduled_at(store.get_item(iid)) is None)
    check("clearing a download with no time says so", service.schedule_items("clear", id=iid, store=store)["ok"] is False)
    check("'now' resumes it", service.schedule_items("now", id=iid, store=store)["ok"] and store.get_item(iid)["state"] == "queued")
    store.transition(iid, "probing")
    check("a running download can't be scheduled", service.schedule_items("in 1 hour", id=iid, store=store)["ok"] is False)
    check("bad wording is an error, not a guess", service.schedule_items("later-ish", id=iid, store=store)["ok"] is False)
    check("a missing selector is an error", service.schedule_items("in 1 hour", store=store)["ok"] is False)
    # cancelled scheduled item is never woken
    r2 = service.add_items([URL % 2], start_at="in 1 hour", store=store)["added"][0]["id"]
    service.control("cancel", id=r2, store=store)
    check("a cancelled scheduled download is not started", service.wake_scheduled(store, time.time() + 10 ** 7) == [] and store.get_item(r2)["state"] == "cancelled")
    # a paused row with only a retry time is not "scheduled"
    r3 = service.add_items([URL % 3], store=store)["added"][0]["id"]
    store.update_item(r3, {"next_attempt_at": 1.0}, touch=False)
    store.transition(r3, "paused")
    check("a paused row with just a retry time is not woken", service.wake_scheduled(store, time.time() + 10 ** 7) == [])


def test_concurrency_summary():
    store = fresh()
    s = service.summary(store)
    check("the summary reports the global limit and what runs", s["concurrency"]["total"] == 4 and s["concurrency"]["running"] == 0)
    r = service.update_prefs({"concurrency.total": 2}, store=store)
    check("the everyday tier can change the global limit", r["ok"] and service.summary(store)["concurrency"]["total"] == 2)
    check("zero is refused", service.update_prefs({"concurrency.total": 0}, store=store)["ok"] is False)
    a = service.add_items([URL % 10, URL % 11, URL % 12], store=store)
    caps, total = HubDaemon(store=store)._caps()
    started = []
    while True:
        it = store.claim_next({"http": 9, "default": 9}, total)
        if not it:
            break
        started.append(it["id"])
    check("no more than the global limit start at once", len(started) == 2 and store.running_hub() == 2)


def test_notifier_wiring():
    opts = service.notify_options()
    check("the hub offers exactly the notifier's channels", [c["id"] for c in opts["channels"]] == list(notifier.CHANNELS))
    check("and its levels", [l["value"] for l in opts["levels"]] == list(range(notifier.MIN_LEVEL, notifier.MAX_LEVEL + 1)))
    check("it is in the summary and the settings the panel reads", service.summary(fresh())["notify_options"]["channels"] and service.get_settings(fresh())["settings"]["notify_options"]["levels"])
    old = (notifier.MAX_LEVEL, dict(notifier.LEVEL_NAMES), notifier.CHANNELS, dict(notifier._DELIVERERS))
    try:
        notifier.MAX_LEVEL = 6
        notifier.LEVEL_NAMES[6] = "panic"
        notifier.CHANNELS = notifier.CHANNELS + ("telegram",)
        notifier._DELIVERERS["telegram"] = lambda record, config: True
        opts = service.notify_options()
        check("a new level appears with no hub change", opts["levels"][-1]["value"] == 6 and "Panic" in opts["levels"][-1]["label"])
        check("a new channel appears with no hub change", opts["channels"][-1] == {"id": "telegram", "label": "Telegram"})
        check("the hub accepts the new level", hs.validate_update({"notify.done.level": 6})[0] == {"notify.done.level": 6})
        check("and the new level by name", hs.validate_update({"notify.done.level": "panic"})[0] == {"notify.done.level": 6})
        check("and the new channel for one download", service.normalize_channels(["telegram"]) == (["telegram"], None))
        check("but not a level past the notifier's top", hs.validate_update({"notify.done.level": 7})[1])
    finally:
        notifier.MAX_LEVEL, notifier.LEVEL_NAMES, notifier.CHANNELS = old[0], old[1], old[2]
        notifier._DELIVERERS.clear()
        notifier._DELIVERERS.update(old[3])
    check("back to normal, 6 is refused again", hs.validate_update({"notify.done.level": 6})[1])
    check("a level by name works today", hs.validate_update({"notify.done.level": "persistent"})[0] == {"notify.done.level": 3})
    check("'default' clears it", hs.validate_update({"notify.done.level": "default"})[0] == {"notify.done.level": None})


for fn in (test_parse, test_schedule_flow, test_concurrency_summary, test_notifier_wiring):
    print("\n-- " + fn.__name__)
    fn()
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
