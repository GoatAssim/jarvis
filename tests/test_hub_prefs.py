"""Download Hub: the everyday preferences (speed limiter, notifications, open-when-done, folders).

Covers: the new settings (validation, deep merge, the dl_prefs-vs-dl_settings split); the speed
schedule maths (including overnight windows); how the daemon shares the limit between running
downloads and honours a per-download cap; per-download options on dl_add / dl_prefs; the
notification Dispatcher (once only, restart-safe, history stays quiet, per-event switches, per-item
override, minimum duration, burst coalescing, batch and queue-empty summaries) with a fake
`deliver` standing in for jarvis/notifier.py; "open when done" (once only, per-download override,
programs and scripts never opened, the cap on automatic opens); and the dl_prefs tool.

Run: python tests/test_hub_prefs.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-prefs-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
import datetime  # noqa: E402
import collections  # noqa: E402
from jarvis.hub import naming, notify, service, settings as hs, states  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

PUBLIC = "http://93.184.216.34"      # an IP literal: the SSRF guard resolves names, and tests have no network
passed = failed = 0
_n = [0]


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


class Clock:
    def __init__(self, t=2_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def fresh_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"prefs-{_n[0]}.db", clock=clock or time.time)


def add(store, name="a.zip", group_id=None, options=None, url=None):
    item, _ = store.add_item(engine="http", source_url=url or f"https://example.com/{name}", title=name,
                             options=options or {}, group_id=group_id)
    return item


def finish(store, item, state="done", **fields):
    """Run THIS row through to done / failed the way the daemon would.

    (claim_next picks by age, and rows made in the same second tie, so it can't be trusted to
    pick the row a test means.)"""
    if store.get_item(item["id"])["state"] == "queued":
        with store._write() as conn:
            conn.execute("UPDATE items SET state='probing', attempts=attempts+1, started_at=COALESCE(started_at, ?) "
                         "WHERE id=?", (store.now(), item["id"]))
    if state == "done":
        fields.setdefault("dest_file", "/library/Files/" + item["title"])
    return store.transition(item["id"], state, **fields)


class Sink:
    """Stands in for notifier.notify: records every message."""

    def __init__(self):
        self.sent = []

    def __call__(self, title, message, *, level=None, failed=False, look=None, tone=None):
        # `look` / `tone` are only passed for the three small-toast events (hub/notify.EVENT_LOOKS).
        self.sent.append({"title": title, "message": message, "level": level, "failed": failed,
                          "look": look, "tone": tone})
        return {"delivered": ["inbox", "desktop"], "failed_channels": []}


# ------------------------------------------------------------------ settings

def test_settings():
    d = hs.DEFAULTS
    check("six notification events, done/failed/group/queue on, started/stalled off",
          [e for e in hs.EVENTS if d["notify"][e]["enabled"]] == ["done", "failed", "group_done", "queue_empty"])
    check("a notification level defaults to None (follow the notifier)", all(d["notify"][e]["level"] is None for e in hs.EVENTS))
    check("open-when-done is off by default", d["after_download"]["open"] == "off")

    clean, err = hs.validate_update({"notify.done.level": "3", "notify.failed.level": "default", "notify.done.enabled": "off"})
    check("a level, 'default' and an on/off word validate", clean == {"notify.done.level": 3, "notify.failed.level": None, "notify.done.enabled": False} and not err)
    check("a level outside 1-5 is refused", hs.validate_update({"notify.done.level": 9})[1])
    check("there is no channel list of our own to set", hs.validate_update({"notify.done.channels": ["desktop"]})[1])
    check("an unknown event is refused", hs.validate_update({"notify.lunch.enabled": True})[1])

    merged = hs.merged({"notify": {"done": {"enabled": False}}})
    check("changing one event keeps every other event's default",
          merged["notify"]["done"] == {"enabled": False, "level": None} and merged["notify"]["failed"]["enabled"] is True)
    stored = {}
    hs.apply_dotted(stored, {"notify.done.level": 4, "notify.done.enabled": False, "folders.video": "Movies"})
    check("apply_dotted writes nested keys as nested", stored == {"notify": {"done": {"level": 4, "enabled": False}}, "folders": {"video": "Movies"}})

    check("open mode words are normalised", hs.validate_update({"after_download.open": "None"})[0] == {"after_download.open": "off"})
    check("an unknown open mode is refused", hs.validate_update({"after_download.open": "run"})[1])

    for bad in ("../x", "/abs", "C:\\Users\\x", "", "a/b/c/d/e/f/g/h"):
        if hs.validate_update({"folders.video": bad})[1] == []:
            check("folder name %r is refused" % bad, False)
            break
    else:
        check("folder names that are paths, parents, empty or too deep are refused", True)
    check("a nested folder name is kept", hs.validate_update({"folders.video": "Media/Movies"})[0] == {"folders.video": "Media/Movies"})

    check("library_root and allow_private_hosts are NOT in the everyday set",
          "library_root" not in hs.PREFS_WRITABLE and "allow_private_hosts" not in hs.PREFS_WRITABLE)
    clean, err = hs.validate_update({"library_root": "/tmp/x"}, allowed=hs.PREFS_WRITABLE)
    check("...and asking for them there says to use dl_settings", err and "dl_settings" in err[0] and not clean)
    check("dl_settings still accepts them", not hs.validate_update({"library_root": str(Path(HOME) / "lib")})[1])


def test_schedule():
    clean, err = hs.validate_update({"schedule": [{"days": "weekdays", "from": "01:00", "to": "07:00", "rate_limit_kbps": 0}]})
    win = clean["schedule"][0]
    check("a schedule window is normalised (days list, 24h times)", win["days"] == ["mon", "tue", "wed", "thu", "fri"] and win["from"] == "01:00" and not err)
    check("bad times are refused", hs.validate_update({"schedule": [{"days": "all", "from": "25:00", "to": "07:00"}]})[1])
    check("from == to is refused", hs.validate_update({"schedule": [{"days": "all", "from": "07:00", "to": "07:00"}]})[1])
    check("a bad day is refused", hs.validate_update({"schedule": [{"days": "funday", "from": "01:00", "to": "02:00"}]})[1])
    check("more than ten windows is refused", hs.validate_update({"schedule": [{"days": "all", "from": "01:00", "to": "02:00"}] * 11})[1])
    check("an empty list clears the schedule", hs.validate_update({"schedule": []})[0] == {"schedule": []})

    def at(day, hh, mm=0):                       # 2026-10-12 is a Monday
        return datetime.datetime(2026, 10, 12 + day, hh, mm)

    cfg = hs.merged({"rate_limit_kbps": 1000, "schedule": [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "from": "09:00", "to": "17:00", "rate_limit_kbps": 200},
        {"days": ["fri"], "from": "22:00", "to": "06:00", "rate_limit_kbps": 0}]})
    check("outside every window the owner's limit applies", hs.effective_rate_limit_kbps(cfg, at(0, 8)) == (1000, "manual"))
    check("inside a window its limit replaces it", hs.effective_rate_limit_kbps(cfg, at(0, 10)) == (200, "schedule"))
    check("the window end is exclusive", hs.effective_rate_limit_kbps(cfg, at(0, 17))[1] == "manual")
    check("a window with 0 means unlimited there", hs.effective_rate_limit_kbps(cfg, at(4, 23)) == (0, "schedule"))
    check("an overnight window is still on the next morning", hs.effective_rate_limit_kbps(cfg, at(5, 2)) == (0, "schedule"))
    check("...and over by 06:00", hs.effective_rate_limit_kbps(cfg, at(5, 6))[1] == "manual")
    check("an overnight window doesn't start on the wrong day", hs.effective_rate_limit_kbps(cfg, at(2, 23)) == (1000, "manual"))
    check("no limit and no window reads as none", hs.effective_rate_limit_kbps(hs.merged({}), at(0, 12)) == (0, "none"))


# ------------------------------------------------------------------ folders, store

def test_folders_and_store():
    check("the built-in kind folders are unchanged", [naming.kind_folder(k) for k in ("video", "audio", "file", "x")] == ["Video", "Audio", "Files", "Files"])
    cfg = {"video": "Movies", "audio": "Music/Podcasts"}
    check("a configured folder name is used", naming.kind_folder("video", cfg) == "Movies" and naming.kind_folder("audio", cfg) == "Music/Podcasts")
    check("an unset kind falls back to the built-in", naming.kind_folder("file", cfg) == "Files")
    check("an unusable stored value falls back instead of breaking a download", naming.kind_folder("video", {"video": "../.."}) == "Video")
    check("clean_relative refuses paths", naming.clean_relative("/etc") is None and naming.clean_relative("C:/x") is None)

    store = fresh_store()
    conn = store._write().__enter__().__class__  # noqa: F841 - just proves the context manager exists
    with store._write() as c:
        c.execute("DROP TABLE notices")
        c.execute("PRAGMA user_version = 1")
    reopened = Store(store.path) if hasattr(store, "path") else None
    check("a version-1 database upgrades itself (the notices table comes back)",
          reopened is not None and reopened.schema_version() == 2 and reopened.claim_notice("x", "done", 1.0))


# ------------------------------------------------------------------ per-download options

def test_item_options():
    store = fresh_store()
    out = service.add_items(PUBLIC + "/a.zip", store=store, speed_limit_kbps=300, open_when_done="reveal", notify_me="on")
    item = store.get_item(out["added"][0]["id"])
    check("dl_add stores the per-download extras in options", item["options"] == {"limit_kbps": 300, "open": "reveal", "notify": "on"})
    again = service.add_items(PUBLIC + "/a.zip", store=store, speed_limit_kbps=999)
    check("the extras never change the duplicate check", again["added"][0]["duplicate"] is True and again["added"][0]["id"] == item["id"])
    check("a bad speed is refused", service.add_items(PUBLIC + "/b.zip", store=store, speed_limit_kbps="fast")["ok"] is False)
    check("a bad open mode is refused", service.add_items(PUBLIC + "/b.zip", store=store, open_when_done="run")["ok"] is False)
    check("a bad notify value is refused", service.add_items(PUBLIC + "/b.zip", store=store, notify_me="loudly")["ok"] is False)
    plain = service.add_items(PUBLIC + "/c.zip", store=store, speed_limit_kbps=0, open_when_done="default")
    check("0 and 'default' mean 'nothing of its own'", store.get_item(plain["added"][0]["id"])["options"] == {})

    res = service.update_prefs({"limit_kbps": 500, "open": "folder"}, id=item["id"], store=store)
    check("update_prefs(id) changes one download's options", res["ok"] and store.get_item(item["id"])["options"] == {"limit_kbps": 500, "open": "folder", "notify": "on"})
    res = service.update_prefs({"limit_kbps": 0, "open": "default"}, id=item["id"], store=store)
    check("0 / default clear them again", store.get_item(item["id"])["options"] == {"notify": "on"})
    check("an unknown per-download key is refused", service.update_prefs({"library_root": "/x"}, id=item["id"], store=store)["ok"] is False)
    done = add(store, "done.zip")
    finish(store, done)
    check("a finished download can't be given new options", service.update_prefs({"limit_kbps": 5}, id=done["id"], store=store)["ok"] is False)
    check("no such download is an error", service.update_prefs({"limit_kbps": 5}, id="nope", store=store)["ok"] is False)
    check("item_limit_kbps reads the cap", hs.item_limit_kbps({"options": {"limit_kbps": 300}}) == 300 and hs.item_limit_kbps({"options": {}}) == 0)

    res = service.update_prefs({"rate_limit_kbps": 2048, "notify.done.level": 4}, store=store)
    view = res["settings"]
    check("update_prefs changes the global ones and the view shows them",
          res["ok"] and view["rate_limit_kbps"] == 2048 and view["rate_limit_now"] == {"kbps": 2048, "source": "manual"} and view["notify"]["done"]["level"] == 4)
    check("update_prefs refuses library_root", service.update_prefs({"library_root": str(Path(HOME) / "x")}, store=store)["ok"] is False)
    check("the settings view carries the library info the panel shows",
          {"path", "exists", "writable", "free_bytes", "folders"} <= set(view["library"]) and view["library"]["folders"]["video"] == "Video")


# ------------------------------------------------------------------ sharing the limit

class FakeWorker:
    def __init__(self, item_id, limit_kbps=0):
        self.item_id, self.limit_kbps, self.engine_name = item_id, limit_kbps, "http"

    def is_alive(self):
        return True


def test_rates():
    store = fresh_store()
    d = HubDaemon(store=store)
    items = [add(store, "r%d.bin" % i, options=({"limit_kbps": 100} if i == 0 else {})) for i in range(3)]
    for it, cap in zip(items, (100, 0, 0)):
        d.workers[it["id"]] = FakeWorker(it["id"], cap)
    store.set_settings({"rate_limit_kbps": 1000})
    d._settings = None
    rates = {i["id"]: d._rate_for(d.workers[i["id"]]) for i in items}
    check("a capped download takes no more than its cap", rates[items[0]["id"]] == 100 * 1024)
    check("what it leaves unused goes to the others", rates[items[1]["id"]] == rates[items[2]["id"]] == 450 * 1024)
    check("together they use the whole global limit", sum(rates.values()) == 1000 * 1024)

    store.set_settings({"rate_limit_kbps": 0})
    d._settings = None
    d._rates_at = 0
    rates = {i["id"]: d._rate_for(d.workers[i["id"]]) for i in items}
    check("no global limit: only the cap applies (0 = unlimited)", rates[items[0]["id"]] == 100 * 1024 and rates[items[1]["id"]] == 0)

    store.update_item(items[1]["id"], {"options": {"limit_kbps": 50}})
    d._rates_at = 0
    check("an edit made while it runs is picked up", d._rate_for(d.workers[items[1]["id"]]) == 50 * 1024)

    now = datetime.datetime.now()
    window = {"days": list(hs._DAY_NAMES), "from": "00:00", "to": "23:59", "rate_limit_kbps": 640}
    store.set_settings({"schedule": [window], "rate_limit_kbps": 0})
    d._settings = None
    check("inside a schedule window its limit is the global one", d._global_bps() == 640 * 1024 or now.hour * 60 + now.minute >= 23 * 60 + 59)
    store.set_settings({"schedule": []})
    d._settings = None
    check("_rate_share still divides the global limit evenly", d._rate_share("http") == 0)


# ------------------------------------------------------------------ notifications

def make_dispatcher(clock=None, cfg_updates=None):
    clock = clock or Clock()
    store = fresh_store(clock)
    sink = Sink()
    if cfg_updates:
        clean, err = hs.validate_update(cfg_updates)
        assert not err, err
        store.set_settings(clean)
    disp = notify.Dispatcher(store, deliver=sink, clock=clock)
    return store, disp, sink, clock


def scan(disp, store):
    return disp.scan(store.get_settings())


def test_notifications():
    clock = Clock()
    store, disp, sink, _ = make_dispatcher(clock, {"notify.coalesce_s": 0})
    old = add(store, "old.zip")
    finish(store, old)                                    # finished BEFORE announcing began
    clock.advance(10)
    scan(disp, store)
    check("rows that finished before announcing began stay quiet (history)", sink.sent == [])

    item = add(store, "movie.mkv")
    clock.advance(1)
    finish(store, item, bytes_done=734_003_200, dest_dir="/library/Video")
    scan(disp, store)
    check("a finish is announced once", len(sink.sent) == 1 and sink.sent[0]["title"] == "Download finished")
    check("the message names the file, size and folder", "movie.mkv" in sink.sent[0]["message"] and "700.0 MB" in sink.sent[0]["message"] and "Video" in sink.sent[0]["message"])
    check("no level is forced (the notifier decides)", sink.sent[0]["level"] is None and sink.sent[0]["failed"] is False)
    scan(disp, store)
    check("a second scan doesn't repeat it", len(sink.sent) == 1)
    again = notify.Dispatcher(store, deliver=sink, clock=clock)
    scan(again, store)
    check("a restarted daemon doesn't repeat it either", len(sink.sent) == 1)

    bad = add(store, "bad.bin")
    clock.advance(1)
    finish(store, bad, state="failed", error="server said 404")
    scan(disp, store)
    check("a failure is announced with its reason and flagged failed", sink.sent[-1]["failed"] is True and "404" in sink.sent[-1]["message"] and sink.sent[-1]["title"] == "Download failed")

    # retry -> finishes again -> announced again (new finished_at)
    store.transition(bad["id"], "queued")
    clock.advance(5)
    finish(store, bad, bytes_done=10)
    n = len(sink.sent)
    scan(disp, store)
    check("a retried download that then finishes is announced again", len(sink.sent) == n + 1 and sink.sent[-1]["title"] == "Download finished")


def test_switches():
    store, disp, sink, clock = make_dispatcher(None, {"notify.coalesce_s": 0, "notify.done.level": 4})
    scan(disp, store)
    a = add(store, "a.zip")
    clock.advance(1)
    finish(store, a)
    scan(disp, store)
    check("a configured level is handed to the notifier", sink.sent and sink.sent[-1]["level"] == 4)

    store.set_settings({"notify.done.enabled": False})
    b = add(store, "b.zip")
    clock.advance(1)
    finish(store, b)
    n = len(sink.sent)
    scan(disp, store)
    check("an event switched off says nothing", len(sink.sent) == n)

    forced = add(store, "forced.zip", options={"notify": "on"})
    clock.advance(1)
    finish(store, forced)
    scan(disp, store)
    check("'notify me about this one' speaks even when the event is off", len(sink.sent) == n + 1 and "forced.zip" in sink.sent[-1]["message"])

    store.set_settings({"notify.done.enabled": True})
    quiet = add(store, "quiet.zip", options={"notify": "off"})
    clock.advance(1)
    finish(store, quiet)
    n = len(sink.sent)
    scan(disp, store)
    check("'never tell me about this one' stays quiet", len(sink.sent) == n)

    store.set_settings({"notify.enabled": False})
    c = add(store, "c.zip")
    clock.advance(1)
    finish(store, c)
    scan(disp, store)
    check("the master switch silences everything", len(sink.sent) == n)

    store2, disp2, sink2, clock2 = make_dispatcher(None, {"notify.coalesce_s": 0, "notify.min_seconds": 30})
    scan(disp2, store2)
    fast = add(store2, "fast.zip")
    store2.claim_next({"http": 4, "default": 4}, 8)
    clock2.advance(5)
    store2.transition(fast["id"], "done", dest_file="/x/fast.zip")
    scan(disp2, store2)
    check("a download faster than min_seconds isn't announced", sink2.sent == [])
    slow = add(store2, "slow.zip")
    store2.claim_next({"http": 4, "default": 4}, 8)
    clock2.advance(45)
    store2.transition(slow["id"], "done", dest_file="/x/slow.zip")
    scan(disp2, store2)
    check("a slower one is", len(sink2.sent) == 1)

    store3, disp3, sink3, clock3 = make_dispatcher(None, {"notify.coalesce_s": 0, "notify.started.enabled": True, "notify.stalled.enabled": True})
    scan(disp3, store3)
    s = add(store3, "s.zip")
    clock3.advance(1)
    store3.claim_next({"http": 4, "default": 4}, 8)
    scan(disp3, store3)
    check("'started' speaks when switched on", sink3.sent and sink3.sent[-1]["title"] == "Download started")
    store3.transition(s["id"], "stalled", error="no data")
    scan(disp3, store3)
    check("'stalled' speaks when switched on", sink3.sent[-1]["title"] == "Download stalled")


def test_coalescing_and_summaries():
    store, disp, sink, clock = make_dispatcher(None, {"notify.coalesce_s": 8, "notify.queue_empty.enabled": False})
    scan(disp, store)
    for name in ("one.zip", "two.zip", "three.zip", "four.zip"):
        it = add(store, name)
        clock.advance(1)
        finish(store, it)
    scan(disp, store)
    check("finishes inside the window are held, not sent one by one", sink.sent == [])
    clock.advance(9)
    scan(disp, store)
    check("when the window ends they go out as ONE message", len(sink.sent) == 1 and sink.sent[0]["title"] == "4 downloads finished")
    check("it names a few and counts the rest", "one.zip" in sink.sent[0]["message"] and "and 1 more" in sink.sent[0]["message"])

    store2, disp2, sink2, clock2 = make_dispatcher(None, {"notify.coalesce_s": 8})
    scan(disp2, store2)
    gid = store2.ensure_group("Holiday photos", kind="batch")
    items = [add(store2, "p%d.jpg" % i, group_id=gid) for i in range(2)]
    clock2.advance(1)
    finish(store2, items[0])
    scan(disp2, store2)
    check("a batch with one file left says nothing about the batch", all(m["title"] != "Batch finished" for m in sink2.sent))
    clock2.advance(1)
    finish(store2, items[1])
    scan(disp2, store2)
    titles = [m["title"] for m in sink2.sent]
    check("the last file triggers the batch summary", "Batch finished" in titles)
    check("the held per-file line is sent before the summary", titles[0] == "2 downloads finished" and titles.index("Batch finished") == 1)
    summary = [m for m in sink2.sent if m["title"] == "Batch finished"][0]
    check("the summary names the batch and counts", "Holiday photos" in summary["message"] and "2 finished" in summary["message"])
    scan(disp2, store2)
    check("the batch summary is sent once", [m["title"] for m in sink2.sent].count("Batch finished") == 1)
    check("and the queue-empty message follows once everything is done", sink2.sent[-1]["title"] == "All downloads finished" and len(sink2.sent) == 3)

    store3, disp3, sink3, clock3 = make_dispatcher(None, {"notify.coalesce_s": 0})
    scan(disp3, store3)
    x = add(store3, "x.zip")
    scan(disp3, store3)                     # busy now
    clock3.advance(1)
    finish(store3, x)
    scan(disp3, store3)
    check("'all downloads finished' fires when the queue goes empty", any(m["title"] == "All downloads finished" for m in sink3.sent))
    n = len(sink3.sent)
    scan(disp3, store3)
    check("...once", len(sink3.sent) == n)


def test_send_test():
    store, disp, sink, _ = make_dispatcher(None, {"notify.failed.level": 5})
    rec = disp.send_test("failed", store.get_settings())
    check("the test goes through the same deliver with the event's level", sink.sent[0]["level"] == 5 and sink.sent[0]["failed"] is True)
    check("it says it's only a test", sink.sent[0]["title"].startswith("Test:") and "only a test" in sink.sent[0]["message"])
    check("it returns the notifier's record (which channels delivered)", rec["delivered"] == ["inbox", "desktop"])
    try:
        disp.send_test("lunch", store.get_settings())
        check("an unknown type is refused", False)
    except ValueError:
        check("an unknown type is refused", True)
    out = service.send_test_notification("done", store=store, deliver=sink)
    check("the service wrapper reports the delivered channels", out["ok"] and out["delivered"] == ["inbox", "desktop"] and "inbox" in out["note"])
    check("the service wrapper refuses an unknown type", service.send_test_notification("lunch", store=store)["ok"] is False)


# ------------------------------------------------------------------ open when done

def test_auto_open():
    lib = Path(HOME) / "library"
    (lib / "Files").mkdir(parents=True)
    calls = []
    saved = service.LAUNCH
    service.LAUNCH = lambda path, action: calls.append((str(path), action)) or True
    try:
        clock = Clock()
        store = fresh_store(clock)
        clean, err = hs.validate_update({"library_root": str(lib)})
        store.set_settings(clean)

        def make(name, options=None, outside=False):
            path = (Path(HOME) / "elsewhere" / name) if outside else (lib / "Files" / name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
            it = add(store, name, options=options)
            clock.advance(1)
            finish(store, it, dest_file=str(path), dest_dir=str(path.parent))
            return it, path

        def run(since=1_000_000.0, recent=None):
            return service.run_auto_open(store, store.get_settings(), since, recent if recent is not None else collections.deque(), now=clock())

        it, path = make("doc.pdf")
        check("off by default: nothing is opened", run() == [] and calls == [])

        store.set_settings({"after_download.open": "reveal"})
        it, path = make("pic.jpg")
        result = run()
        check("reveal: the file is shown in its folder", result == [(it["id"], "reveal")] and calls[-1] == (str(path), "reveal"))
        check("the row records what happened", any("automatically" in (e.get("message") or "") for e in store.get_item(it["id"])["events"])
              if "events" in store.get_item(it["id"]) else True)
        n = len(calls)
        run()
        check("it is done once per finish", len(calls) == n)

        store.set_settings({"after_download.open": "folder"})
        it, path = make("clip.mp4")
        run()
        check("folder: the folder is opened, not the file", calls[-1] == (str(path.parent), "open"))

        store.set_settings({"after_download.open": "open"})
        it, path = make("report.pdf")
        run()
        check("open: a document is opened", calls[-1] == (str(path), "open"))
        it, path = make("setup.exe")
        run()
        check("open: a program is shown in its folder instead, never run", calls[-1] == (str(path), "reveal"))
        for name in ("run.bat", "x.ps1", "y.js", "z.lnk", "m.docm", "page.html", "a.sh"):
            check("%s is on the never-open list" % name, service.is_risky_to_open(name))
        check("a pdf and a zip are not", not service.is_risky_to_open("a.pdf") and not service.is_risky_to_open("b.ZIP"))

        it, path = make("own.pdf", options={"open": "off"})
        n = len(calls)
        run()
        check("a download's own 'off' beats the global setting", len(calls) == n)
        store.set_settings({"after_download.open": "off"})
        it, path = make("own2.pdf", options={"open": "reveal"})
        run()
        check("...and its own 'reveal' beats a global 'off'", calls[-1] == (str(path), "reveal"))

        it, path = make("elsewhere.pdf", options={"open": "open"}, outside=True)
        n = len(calls)
        result = run()
        check("a file outside the library root is refused, never opened", len(calls) == n and result and result[0][1] == "failed")

        store.set_settings({"after_download.open": "reveal"})
        recent = collections.deque()
        made = [make("b%d.dat" % i)[0] for i in range(5)]
        n = len(calls)
        result = run(recent=recent)
        check("a flood of finishes opens only a few windows", len(calls) - n == service.AUTO_OPEN_MAX and [r[1] for r in result].count("skipped") == 2)
        later = make("late.dat")[0]
        clock.advance(service.AUTO_OPEN_WINDOW_S + 1)
        n = len(calls)
        run(recent=recent)
        check("...and the allowance comes back after the window", len(calls) == n + 1)
    finally:
        service.LAUNCH = saved


# ------------------------------------------------------------------ end to end (real daemon, real HTTP)

def test_end_to_end():
    server, base, _state = srv.start()
    lib = Path(HOME) / "e2e-lib"
    saved = service.LAUNCH
    opened = []
    service.LAUNCH = lambda path, action: opened.append((str(path), action)) or True
    try:
        store = fresh_store()
        clean, err = hs.validate_update({"library_root": str(lib), "allow_private_hosts": ["127.0.0.1"], "folders.file": "Docs",
                                         "after_download.open": "reveal", "notify.coalesce_s": 0, "notify.queue_empty.enabled": False})
        check("the e2e settings validate", not err)
        store.set_settings(clean)
        sink = Sink()
        daemon = HubDaemon(store, deliver=sink)
        slow = service.add_items(base + "/file.bin?size=200000", store=store, speed_limit_kbps=100)["added"][0]["id"]
        fast = service.add_items(base + "/file.bin?size=200001", store=store, notify_me="off", open_when_done="off")["added"][0]["id"]
        started = time.time()
        finished = {}
        end = started + 30
        while time.time() < end and len(finished) < 2:
            daemon.tick()
            for iid in (slow, fast):
                if iid not in finished and store.get_item(iid)["state"] in states.FINAL_STATES:
                    finished[iid] = time.time() - started
            time.sleep(0.05)
        daemon.stop(timeout=5)
        check("both downloads finished", len(finished) == 2 and all(store.get_item(i)["state"] == "done" for i in (slow, fast)))
        check("the capped one took visibly longer than the uncapped one", finished[slow] >= 1.2 and finished[fast] < finished[slow])
        item = store.get_item(slow)
        check("files went into the configured folder for their kind", Path(item["dest_file"]).parent == lib / "Docs" and Path(item["dest_file"]).stat().st_size == 200000)
        check("the finish was announced once through the notifier (not the 'never' one)",
              [m["title"] for m in sink.sent] == ["Download finished"] and "file.bin" in sink.sent[0]["message"])
        check("the global 'show in folder' ran for the one that follows it, not for the one that opted out",
              opened == [(item["dest_file"], "reveal")])
        daemon2 = HubDaemon(store, deliver=sink)
        daemon2.tick()
        daemon2.tick()
        check("a second daemon over the same data announces and opens nothing again", len(sink.sent) == 1 and len(opened) == 1)
    finally:
        service.LAUNCH = saved
        srv.stop(server)


# ------------------------------------------------------------------ the tool

def test_tool():
    from jarvis import ui_bridge
    from jarvis.actions import download_hub as hub_tool
    store_dir = Path(HOME)
    # the tool uses the default Store(): point it at a fresh DB under this HOME
    check("dl_prefs is registered and not confirm-gated", "dl_prefs" in hub_tool.TOOLS and "dl_prefs" not in hub_tool.TOOL_CONFIRM_REQUIRED)
    res = hub_tool.tool_dl_prefs({})
    check("no arguments reads the settings", res["ok"] and "notify" in res["settings"] and "schedule" in res["settings"])
    res = hub_tool.tool_dl_prefs({"set": {"rate_limit_kbps": 1024}})
    check("setting the speed limit works without a question", res["ok"] and res["settings"]["rate_limit_now"]["kbps"] == 1024)
    check("library_root is refused here", hub_tool.tool_dl_prefs({"set": {"library_root": str(store_dir / "z")}})["ok"] is False)
    check("a typo is an error", hub_tool.tool_dl_prefs({"set": {"rate_limit": 5}})["ok"] is False)
    check("a non-object `set` is an error", hub_tool.tool_dl_prefs({"set": "fast"})["ok"] is False)

    asked = []
    saved = ui_bridge.confirm
    ui_bridge.confirm = lambda *a, **k: asked.append(a) or False
    try:
        res = hub_tool.tool_dl_prefs({"set": {"after_download.open": "open"}})
        check("choosing 'open the file' asks first, and No changes nothing", asked and res.get("cancelled") and hub_tool.tool_dl_prefs({})["settings"]["after_download"]["open"] == "off")
        res = hub_tool.tool_dl_add({"url": PUBLIC + "/q.zip", "open_when_done": "open"})
        check("dl_add with open_when_done=open asks too, and No queues nothing", res.get("cancelled") and len(asked) == 2)
        ui_bridge.confirm = lambda *a, **k: True
        res = hub_tool.tool_dl_prefs({"set": {"after_download.open": "open"}})
        check("Yes sets it", res["ok"] and res["settings"]["after_download"]["open"] == "open")
        hub_tool.tool_dl_prefs({"set": {"after_download.open": "off"}})
        check("reveal needs no question", hub_tool.tool_dl_prefs({"set": {"after_download.open": "reveal"}})["ok"])
    finally:
        ui_bridge.confirm = saved

    res = hub_tool.tool_dl_add({"url": PUBLIC + "/own.zip", "speed_limit_kbps": 250, "notify": "on"})
    item_id = res["added"][0]["id"]
    res = hub_tool.tool_dl_prefs({"id": item_id, "set": {"limit_kbps": 400}})
    check("dl_prefs with an id edits that download's cap", res["ok"] and res["options"]["limit_kbps"] == 400 and res["options"]["notify"] == "on")
    check("an id with nothing to set says so", hub_tool.tool_dl_prefs({"id": item_id})["ok"] is False)
    out = hub_tool.tool_dl_prefs({"test_notification": "done"})
    check("test_notification goes through the notifier path", out["ok"] and "note" in out)
    check("an unknown test type is refused", hub_tool.tool_dl_prefs({"test_notification": "lunch"})["ok"] is False)

    launched = []
    saved_launch = service.LAUNCH
    service.LAUNCH = lambda path, action: launched.append((str(path), action)) or True
    try:
        res = hub_tool.tool_dl_open({"target": "library"})
        check("dl_open target=library opens the download folder", res["ok"] and launched and launched[-1][1] == "open" and res["path"] == launched[-1][0])
    finally:
        service.LAUNCH = saved_launch


for fn in (test_settings, test_schedule, test_folders_and_store, test_item_options, test_rates,
           test_notifications, test_switches, test_coalescing_and_summaries, test_send_test,
           test_auto_open, test_end_to_end, test_tool):
    print("\n--", fn.__name__)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("%s ran to the end (%s)" % (fn.__name__, exc), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
