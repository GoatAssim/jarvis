"""Download Hub, phase 1: the daemon's scheduler, retry policy and control handling.

Runs the real HubDaemon in this process (tick() by hand, a clock the test moves) against a fake
engine whose behaviour each item picks, plus one real HTTP download through the real engine.

Covers: an Outcome becoming a state (done / failed / crash / unknown engine); THE retry policy
(retryable failures requeue with 5, 10, 20 s backoff and give up after max_retries; a stall stays
`stalled` while it waits); the concurrency limits (per engine and total, a freed slot is used);
pause / resume / cancel through service.control with a live worker (applied by the daemon) and
without one (applied directly), including the .part being deleted on cancel and only that;
orphan control requests; restart recovery; a clean shutdown handing work back; provider rows
read-only; the rate-limit share; stale marking and the spool being applied by housekeeping; and
the single-instance guard.

Run: python tests/test_hub_daemon.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-daemon-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.hub import daemon as daemon_mod, engines, paths, service, states  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.engines.base import Engine, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

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


class FakeEngine(Engine):
    """Each item chooses what happens through options["mode"]."""
    name = "fake"
    label = "Fake"

    def __init__(self):
        self.calls = {}

    def run(self, item, ctx):
        mode = (item.get("options") or {}).get("mode", "ok")
        n = self.calls[item["id"]] = self.calls.get(item["id"], 0) + 1
        if mode == "ok":
            ctx.update(bytes_done=10, bytes_total=10)
            return Outcome.done("fake-result")
        if mode == "flaky":
            return Outcome.failed("boom %d" % n, retryable=True)
        if mode == "fatal":
            return Outcome.failed("nope")
        if mode == "stall":
            return Outcome.stalled("no data")
        if mode == "crash":
            raise RuntimeError("kaboom")
        if mode == "block":
            ctx.set_state("downloading")
            while True:
                reason = ctx.stop_requested()
                if reason == "pause":
                    return Outcome.paused()
                if reason == "cancel":
                    return Outcome.cancelled()
                if reason == "shutdown":
                    return Outcome.requeue()
                time.sleep(0.02)
        return Outcome.failed("unknown mode")


FAKE = FakeEngine()
engines.load()
engines.ENGINES["fake"] = FAKE


def new_env(clock=None, **settings):
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db", clock=clock or time.time)
    base = {"library_root": str(lib), "allow_private_hosts": ["127.0.0.1"], "concurrency.total": 4, "concurrency.fake": 2}
    base.update(settings)
    store.set_settings(base)
    return store, lib, HubDaemon(store, clock=clock or time.time)


def add_fake(store, mode="ok"):
    return store.add_item(engine="fake", source_url="fake://x/" + mode, title=mode, options={"mode": mode})[0]


def pump(daemon, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        daemon.tick()
        if cond():
            return True
        time.sleep(0.02)
    return False


def state_of(store, item):
    return store.get_item(item["id"])["state"]


def active_ids(store):
    return [r["id"] for r in store.list_items(limit=100) if r["state"] in states.ACTIVE_STATES]


def test_outcomes():
    store, lib, d = new_env()
    d.start_up()
    ok = add_fake(store, "ok")
    check("a finished run becomes done with 100 percent", pump(d, lambda: state_of(store, ok) == "done"))
    row = store.get_item(ok["id"])
    check("...and the daemon stamped the result", row["percent"] == 100.0 and row["dest_file"] == "fake-result" and row["finished_at"] and row["attempts"] == 1)

    fatal = add_fake(store, "fatal")
    pump(d, lambda: state_of(store, fatal) == "failed")
    row = store.get_item(fatal["id"])
    check("a non-retryable failure fails at once with its message", row["state"] == "failed" and row["error"] == "nope" and row["attempts"] == 1)

    crash = add_fake(store, "crash")
    pump(d, lambda: state_of(store, crash) == "failed")
    check("an engine that raises fails the item, not the daemon", "crashed" in store.get_item(crash["id"])["error"] and "kaboom" in store.get_item(crash["id"])["error"])

    ghost = store.add_item(engine="no_such_engine", source_url="x://y", title="ghost")[0]
    pump(d, lambda: state_of(store, ghost) == "failed")
    check("an unknown engine fails with a clear message", "no engine called" in store.get_item(ghost["id"])["error"])

    server, base, st = srv.start()
    try:
        item = store.add_item(engine="http", source_url=base + "/file.bin?size=200000", title="")[0]
        check("a real download runs through the daemon to done", pump(d, lambda: state_of(store, item) == "done", 20))
        row = store.get_item(item["id"])
        check("...with the file in the library", Path(row["dest_file"]).read_bytes() == srv.payload(200000) and row["percent"] == 100.0)
        check("...and a readable event trail", [e["message"] for e in store.list_events(item["id"])][-1].startswith("probing -> done")
              or any("finished" in e["message"] for e in store.list_events(item["id"])))
    finally:
        srv.stop(server)
    d.stop(5)


def test_retry_policy():
    clock = Clock()
    store, lib, d = new_env(clock)
    d.start_up()
    item = add_fake(store, "flaky")
    messages = []
    for attempt in range(1, 5):
        ok = pump(d, lambda: FAKE.calls.get(item["id"], 0) == attempt and state_of(store, item) in ("queued", "failed"))
        row = store.get_item(item["id"])
        messages.append((ok, row["state"], row["message"], row["error"], row["attempts"]))
        if attempt < 4:
            before = FAKE.calls[item["id"]]
            for _ in range(8):
                d.tick()
                time.sleep(0.01)
            check("attempt %d: not retried before its backoff has passed" % attempt, FAKE.calls[item["id"]] == before)
            clock.advance(400)
    check("the first three failures requeue with 5, 10, 20 s backoff",
          all(m[0] and m[1] == "queued" for m in messages[:3])
          and "retry 1 of 3 in 5 s" in messages[0][2] and "retry 2 of 3 in 10 s" in messages[1][2] and "retry 3 of 3 in 20 s" in messages[2][2])
    check("the fourth failure gives up (one try plus max_retries)", messages[3][0] and messages[3][1] == "failed" and messages[3][3] == "boom 4" and messages[3][4] == 4)
    check("a failed row can be retried with a fresh budget", service.control("retry", id=item["id"], store=store)["changed"] == [item["id"]]
          and store.get_item(item["id"])["attempts"] == 0 and store.get_item(item["id"])["error"] is None)
    d.stop(5)

    clock = Clock()
    store, lib, d = new_env(clock)
    d.start_up()
    stalled = add_fake(store, "stall")
    pump(d, lambda: FAKE.calls.get(stalled["id"], 0) == 1 and state_of(store, stalled) == "stalled")
    row = store.get_item(stalled["id"])
    check("a stall stays visible as `stalled` while it waits for its retry", row["state"] == "stalled" and "retry 1 of 3" in row["message"] and row["next_attempt_at"] > clock())
    listed = service.list_items(store=store)["items"][0]
    check("and the list says how long is left", listed["state"] == "stalled" and listed["retry_in_s"] == 5)
    clock.advance(6)
    check("then it is picked up again", pump(d, lambda: FAKE.calls.get(stalled["id"], 0) == 2))
    d.stop(5)

    store, lib, d = new_env(clock, **{"http.max_retries": 0})
    check("max_retries is read per engine (the block for http is separate from fake's default of 3)", d._max_retries("http") == 0 and d._max_retries("fake") == 3)


def test_concurrency():
    store, lib, d = new_env()
    d.start_up()
    items = [add_fake(store, "block") for _ in range(5)]
    check("the per-engine cap (2) is reached", pump(d, lambda: len(active_ids(store)) == 2))
    peak = 0
    for _ in range(25):
        d.tick()
        peak = max(peak, len(active_ids(store)))
        time.sleep(0.01)
    check("and never exceeded", peak == 2)
    first = active_ids(store)[0]
    res = service.control("cancel", id=first, store=store)
    check("cancelling a running item is applied by the daemon (pending, then cancelled)", res["pending"] == [first]
          and pump(d, lambda: state_of(store, {"id": first}) == "cancelled"))
    check("its slot is given to the next waiting item", pump(d, lambda: len(active_ids(store)) == 2 and first not in active_ids(store)))
    d.stop(5)

    store, lib, d = new_env(**{"concurrency.total": 1})
    d.start_up()
    for _ in range(3):
        add_fake(store, "block")
    pump(d, lambda: len(active_ids(store)) == 1)
    for _ in range(20):
        d.tick()
        time.sleep(0.01)
    check("the total cap holds across engines", len(active_ids(store)) == 1)
    d.stop(5)


def test_controls_with_a_live_worker():
    store, lib, d = new_env()
    d.start_up()
    item = add_fake(store, "block")
    pump(d, lambda: state_of(store, item) == "downloading")
    part = lib / "Files" / "x.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"partial")
    store.update_item(item["id"], {"source_ref": {"part": str(part)}})

    res = service.control("pause", id=item["id"], store=store)
    check("pause on a running item is pending (the daemon owns the worker)", res["ok"] and res["pending"] == [item["id"]] and not res["changed"])
    check("the daemon pauses it", pump(d, lambda: state_of(store, item) == "paused"))
    check("pause keeps the partial file", part.exists())
    check("a paused row offers resume and cancel, not pause",
          service.list_items(store=store)["items"][0]["capabilities"] == ["resume", "cancel"])
    res = service.control("resume", id=item["id"], store=store)
    check("resume requeues it and the worker starts again", res["changed"] == [item["id"]]
          and pump(d, lambda: state_of(store, item) == "downloading" and FAKE.calls[item["id"]] == 2))
    other = lib / "Files" / "keep.bin"
    other.write_bytes(b"done earlier")
    res = service.control("cancel", id=item["id"], store=store)
    check("cancel is pending, then the daemon cancels", res["pending"] == [item["id"]] and pump(d, lambda: state_of(store, item) == "cancelled"))
    check("cancel deletes the .part", not part.exists())
    check("and only that: another file in the library is untouched", other.exists())
    check("a cancelled row can be retried", service.control("retry", id=item["id"], store=store)["changed"] == [item["id"]])
    d.stop(5)


def test_controls_without_a_worker():
    store, lib, d = new_env()                      # no start_up(): no heartbeat, the daemon is "down"
    item = add_fake(store, "ok")
    res = service.control("pause", id=item["id"], store=store)
    check("with no daemon a queued row is paused directly", res["changed"] == [item["id"]] and state_of(store, item) == "paused")
    check("pausing twice says so", "already paused" in service.control("pause", id=item["id"], store=store)["error"])
    check("resume works", service.control("resume", id=item["id"], store=store)["changed"] == [item["id"]] and state_of(store, item) == "queued")
    part = lib / "Files" / "q.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x")
    store.update_item(item["id"], {"source_ref": {"part": str(part)}})
    check("cancel works and removes the .part", service.control("cancel", id=item["id"], store=store)["changed"] == [item["id"]]
          and state_of(store, item) == "cancelled" and not part.exists())
    outside = Path(HOME) / "precious.part"
    outside.write_bytes(b"keep me")
    second = add_fake(store, "ok")
    store.update_item(second["id"], {"source_ref": {"part": str(outside)}})
    service.control("cancel", id=second["id"], store=store)
    check("...really: cancel left a file outside the library alone", outside.exists())

    done = add_fake(store, "ok")
    store.transition(done["id"], "probing")
    store.transition(done["id"], "done")
    res = service.control("cancel", id=done["id"], store=store)
    check("cancelling a finished download is refused and the file row stays done", res["ok"] is False and "already done" in res["error"] and state_of(store, done) == "done")
    check("retry on a running/queued row is refused", service.control("retry", id=add_fake(store)["id"], store=store)["ok"] is False)
    check("no selector is a clear error", "which download" in service.control("pause", store=store)["error"])
    check("an unknown id is a clear error", service.control("pause", id="h_nope", store=store)["error"] == "no such download")

    for _ in range(3):
        add_fake(store, "ok")
    bulk = service.control("pause", all=True, store=store)
    check("a bulk call lists what it skipped instead of failing", bulk["ok"] and len(bulk["changed"]) >= 3 and bulk["skipped"])
    check("pause by engine", service.control("resume", engine="fake", store=store)["ok"])

    store.upsert_provider_row("demo", "r1", {"state": "downloading"})
    res = service.control("pause", id="demo:r1", store=store)
    check("a read-only provider row can't be paused, and says why", res["ok"] is False and "read-only" in res["error"])
    store.register_provider("capable", label="Capable", capabilities=["pause"], controls={"pause": "cap_pause"})
    store.upsert_provider_row("capable", "r1", {"state": "downloading"})
    res = service.control("pause", id="capable:r1", store=store)
    check("a provider that declares pause isn't wired until phase 5, and says so", res["ok"] is False and "phase 5" in res["error"])
    check("bulk calls never touch provider rows' state", store.get_item("demo:r1")["state"] == "downloading"
          and service.control("cancel", all=True, store=store)["ok"] and store.get_item("demo:r1")["state"] == "downloading")


def test_orphans_recovery_and_shutdown():
    store, lib, d = new_env()
    queued = add_fake(store, "ok")
    store.set_control(queued["id"], "pause")
    d.tick()
    row = store.get_item(queued["id"])
    check("a pause request nobody holds is applied by the daemon", row["state"] == "paused" and row["control"] == "")

    running = add_fake(store, "ok")
    store.transition(running["id"], "probing", expect="queued")
    store.transition(running["id"], "downloading", expect="probing")
    store.set_control(running["id"], "cancel")
    d.tick()
    check("a cancel for an item with no worker (a crash left it) is applied too", state_of(store, running) == "cancelled")

    left = add_fake(store, "ok")
    store.transition(left["id"], "probing", expect="queued")
    store.transition(left["id"], "downloading", expect="probing")
    d.start_up()
    check("on start, work a previous daemon left running goes back to the queue", state_of(store, left) == "queued")
    check("start_up writes the heartbeat", store.daemon_info()["alive"] and store.daemon_info()["pid"] == os.getpid())
    d.stop(5)

    store, lib, d = new_env()
    d.start_up()
    blocker = add_fake(store, "block")
    pump(d, lambda: state_of(store, blocker) == "downloading")
    d.stop(timeout=5)
    row = store.get_item(blocker["id"])
    check("a clean stop hands running work back as queued", row["state"] == "queued" and "restart" in (row["message"] or ""))
    check("and marks the heartbeat dead", not store.daemon_info()["alive"])
    check("the worker thread has ended", not any(t.name.startswith("hub-") and t.is_alive() for t in threading.enumerate()))


def test_rate_share_housekeeping_and_lock():
    store, lib, d = new_env(**{"rate_limit_kbps": 400})
    check("the global rate limit is shared out as bytes per second", d._rate_share("http") == 400 * 1024)
    store2, lib2, d2 = new_env()
    check("no limit means 0 (unlimited)", d2._rate_share("http") == 0)

    clock = Clock()
    store, lib, d = new_env(clock)
    store.upsert_provider_row("quiet", "a", {"state": "downloading", "percent": 5}, now=clock())
    clock.advance(60)
    d.tick()
    check("housekeeping flags a silent provider row stale", store.get_item("quiet:a")["stale"])

    inbox = paths.inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "0000000000000001-test.json").write_text(json.dumps(
        {"provider": "spooltool", "local_id": "s1", "fields": {"title": "Spooled", "state": "downloading"}, "create": True, "ts": time.time()}), encoding="utf-8")
    (inbox / "0000000000000002-bad.json").write_text("{not json", encoding="utf-8")
    d._last_housekeeping = 0.0
    d.tick()
    check("housekeeping applies spooled reports", store.get_item("spooltool:s1") is not None and store.get_item("spooltool:s1")["title"] == "Spooled")
    check("and clears the inbox, even of unreadable files", list(inbox.glob("*.json")) == [])

    check("pid_alive knows this process", daemon_mod.pid_alive(os.getpid()))
    check("pid_alive is False for junk", not daemon_mod.pid_alive("x") and not daemon_mod.pid_alive(None) and not daemon_mod.pid_alive(2_000_000_000))

    default_store = Store()
    default_store.daemon_beat(os.getppid(), time.time())
    code = daemon_mod.main()
    check("a second daemon refuses to start while another is alive", code == daemon_mod.EXIT_ALREADY_RUNNING)


try:
    test_outcomes()
    test_retry_policy()
    test_concurrency()
    test_controls_with_a_live_worker()
    test_controls_without_a_worker()
    test_orphans_recovery_and_shutdown()
    test_rate_share_housekeeping_and_lock()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
