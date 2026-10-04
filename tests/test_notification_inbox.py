"""Regression tests for L.30 — the notification inbox rework.

Covers the backend half of the rework (notifier.py + the notify-* CLI verbs):

  * the OWNER's read / acknowledge state (`read_at`, `acked_at`) is separate
    from delivery (`seen_by`) — delivering to the web console does not make a
    notification "read"
  * `needs_ack` is true only for persistent / confirm-level items that have not
    been acknowledged; reading one acknowledges it
  * mark_read / mark_all_read / dismiss / clear_read
  * summary(): the durable counts the badge reads, `kinds`, and the
    awaiting-acknowledgment list (newest first)
  * history() filters (unread, failed, needs-ack, kind, source, multi-word
    query) and that with no filter it still behaves as it always did
  * pruning drops READ items first and an item still awaiting acknowledgment
    last, keeping the file chronological
  * the inbox lock: concurrent writers lose nothing, a stale lock is
    broken, and a lock that can't be taken never blocks delivery
  * the notify-summary / notify-read / notify-dismiss / notify-history verbs

No network, no live model.  HOME is pointed at a temp dir BEFORE jarvis is
imported, so nothing here touches the real ~/.jarvis.

Run: python3 tests/test_notification_inbox.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis_l30_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME          # Windows spelling of the same thing

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import notifier  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def fresh():
    """An empty inbox and a clean lock for the next test."""
    notifier._save_inbox([])
    try:
        os.remove(str(notifier.INBOX_FILE.with_suffix(".lock")))
    except OSError:
        pass


def send(title="t", message="m", level=2, **kw):
    kw.setdefault("channels", ["inbox"])        # keep OS toasts / stderr out of the test
    return notifier.notify(title, message, level=level, **kw)


def ids_of(items):
    return [i["id"] for i in items]


# --- the record --------------------------------------------------------------

def test_new_record_is_unread_and_unacked():
    fresh()
    r = send(level=3)
    check("a new record has no read_at", r["read_at"] is None)
    check("a new record has no acked_at", r["acked_at"] is None)
    h = notifier.history(5)[0]
    check("history decorates it unread", h["unread"] is True)
    check("a level-3 record needs acknowledgment", h["needs_ack"] is True)


def test_levels_one_and_two_never_need_ack():
    fresh()
    send(level=1)
    send(level=2)
    check("level 1 and 2 never need acknowledgment",
          not any(i["needs_ack"] for i in notifier.history(10)))
    send(level=5)
    check("level 5 needs acknowledgment", notifier.history(1)[0]["needs_ack"] is True)


def test_delivery_is_not_reading():
    fresh()
    r = send(level=3)
    notifier.acknowledge([r["id"]], consumer="web")     # the web console was handed it
    h = notifier.history(1)[0]
    check("delivered to a consumer is still unread", h["unread"] is True and "web" in h["seen_by"])
    check("and still needs acknowledgment", h["needs_ack"] is True)
    check("pending() no longer offers it to that consumer",
          r["id"] not in ids_of(notifier.pending("web")))


# --- mark_read / mark_all_read ----------------------------------------------

def test_mark_read_sets_read_and_acks_persistent():
    fresh()
    std = send("std", level=2)
    per = send("per", level=3)
    check("mark_read reports both changed", notifier.mark_read([std["id"], per["id"]]) == 2)
    by_id = {i["id"]: i for i in notifier.history(10)}
    check("the standard one is read", by_id[std["id"]]["unread"] is False)
    check("a standard one gets no acked_at", by_id[std["id"]]["acked_at"] is None)
    check("the persistent one is read AND acknowledged",
          by_id[per["id"]]["unread"] is False and by_id[per["id"]]["acked_at"])
    check("the persistent one stops needing acknowledgment", by_id[per["id"]]["needs_ack"] is False)


def test_mark_read_is_idempotent_and_ignores_unknown_ids():
    fresh()
    r = send()
    check("first mark_read changes one", notifier.mark_read([r["id"]]) == 1)
    first = notifier.history(1)[0]["read_at"]
    check("second mark_read changes nothing", notifier.mark_read([r["id"]]) == 0)
    check("read_at is not rewritten", notifier.history(1)[0]["read_at"] == first)
    check("an unknown id changes nothing", notifier.mark_read(["nope"]) == 0)
    check("an empty list changes nothing", notifier.mark_read([]) == 0)


def test_mark_all_read():
    fresh()
    for lvl in (1, 2, 3, 5):
        send(level=lvl)
    check("mark_all_read changes all four", notifier.mark_all_read() == 4)
    s = notifier.summary()
    check("nothing unread afterwards", s["unread"] == 0)
    check("and nothing awaits acknowledgment", s["needs_ack"] == [])
    check("a second call changes nothing", notifier.mark_all_read() == 0)


# --- dismiss / clear_read ----------------------------------------------------

def test_dismiss_removes_only_the_given_ids():
    fresh()
    a, b, c = send("a"), send("b"), send("c")
    check("dismiss reports two removed", notifier.dismiss([a["id"], c["id"]]) == 2)
    left = notifier.history(10)
    check("only b is left", ids_of(left) == [b["id"]])
    check("dismissing nothing is a no-op", notifier.dismiss([]) == 0)
    check("dismissing an unknown id is a no-op", notifier.dismiss(["nope"]) == 0)


def test_clear_read_keeps_the_unread():
    fresh()
    a, b, c = send("a"), send("b"), send("c")
    notifier.mark_read([a["id"], b["id"]])
    check("clear_read removes the two read ones", notifier.clear_read() == 2)
    check("the unread one survives", ids_of(notifier.history(10)) == [c["id"]])


# --- summary -----------------------------------------------------------------

def test_summary_counts_and_awaiting_list():
    fresh()
    send("one", kind="reminder", level=2)
    send("two", kind="task", level=2, failed=True)
    p1 = send("ack-old", kind="notify", level=3)
    p2 = send("ack-new", kind="notify", level=5)
    notifier.mark_read([ids_of(notifier.history(10))[-1]])      # the oldest ("one")
    s = notifier.summary()
    check("total counts everything", s["total"] == 4)
    check("unread excludes the one just read", s["unread"] == 3)
    check("failed_unread counts the failed unread one", s["failed_unread"] == 1)
    check("kinds are counted across the whole inbox",
          s["kinds"] == {"reminder": 1, "task": 1, "notify": 2}, s["kinds"])
    check("needs_ack is newest first", ids_of(s["needs_ack"]) == [p2["id"], p1["id"]])
    notifier.mark_read([p2["id"]])
    check("an acknowledged item leaves the awaiting list",
          ids_of(notifier.summary()["needs_ack"]) == [p1["id"]])


def test_summary_of_an_empty_inbox():
    fresh()
    s = notifier.summary()
    check("empty inbox summary is all zero",
          s["total"] == 0 and s["unread"] == 0 and s["failed_unread"] == 0
          and s["needs_ack"] == [] and s["kinds"] == {})


# --- history filters ---------------------------------------------------------

def test_history_without_filters_is_unchanged():
    fresh()
    a, b, c = send("a"), send("b"), send("c")
    check("newest first", ids_of(notifier.history(10)) == [c["id"], b["id"], a["id"]])
    check("the limit applies", ids_of(notifier.history(2)) == [c["id"], b["id"]])
    notifier.mark_read([a["id"]])
    check("read items are still listed with no filter", len(notifier.history(10)) == 3)


def test_history_filters():
    fresh()
    r1 = send("Backup finished", "all 14 files copied", kind="task", source="backup_job")
    r2 = send("Backup failed", "disk full", kind="task", source="backup_job", failed=True)
    r3 = send("Stand up", "daily meeting", kind="reminder")
    r4 = send("Deploy approval", "needs a human", kind="notify", level=5)
    notifier.mark_read([r1["id"]])
    check("unread_only drops the read one",
          set(ids_of(notifier.history(10, unread_only=True))) == {r2["id"], r3["id"], r4["id"]})
    check("failed_only", ids_of(notifier.history(10, failed_only=True)) == [r2["id"]])
    check("needs_ack_only", ids_of(notifier.history(10, needs_ack_only=True)) == [r4["id"]])
    check("kind", set(ids_of(notifier.history(10, kind="task"))) == {r1["id"], r2["id"]})
    check("source", set(ids_of(notifier.history(10, source="backup_job"))) == {r1["id"], r2["id"]})
    check("query is case-insensitive", ids_of(notifier.history(10, query="STAND")) == [r3["id"]])
    check("query matches the message too", ids_of(notifier.history(10, query="disk full")) == [r2["id"]])
    check("a multi-word query needs every word",
          ids_of(notifier.history(10, query="backup copied")) == [r1["id"]])
    check("a query matching nothing returns nothing", notifier.history(10, query="zzz") == [])
    check("filters combine (AND)",
          ids_of(notifier.history(10, kind="task", unread_only=True)) == [r2["id"]])
    check("the limit is applied after filtering",
          len(notifier.history(1, kind="task")) == 1)


# --- pruning -----------------------------------------------------------------

def test_prune_drops_read_items_first():
    fresh()
    saved = notifier.MAX_INBOX
    notifier.MAX_INBOX = 5
    try:
        recs = [send(f"n{i}") for i in range(5)]
        notifier.mark_read([recs[1]["id"], recs[3]["id"]])       # two read, in the middle
        send("n5")
        send("n6")
        titles = [i["title"] for i in notifier._load_inbox()]
        check("never more than the cap", len(titles) == 5, titles)
        check("the two read ones were dropped first", "n1" not in titles and "n3" not in titles, titles)
        check("the unread ones all survive", {"n0", "n2", "n4", "n5", "n6"} == set(titles), titles)
        check("the file stays chronological", titles == sorted(titles), titles)
    finally:
        notifier.MAX_INBOX = saved


def test_prune_keeps_unacknowledged_items_longest():
    fresh()
    saved = notifier.MAX_INBOX
    notifier.MAX_INBOX = 4
    try:
        keep = send("must-keep", level=3)               # oldest, awaiting acknowledgment
        for i in range(5):
            send(f"plain{i}", level=2)                   # a flood of ordinary unread ones
        titles = [i["title"] for i in notifier._load_inbox()]
        check("the oldest unacknowledged item outlives newer plain ones", "must-keep" in titles, titles)
        check("the cap still holds", len(titles) == 4, titles)
        check("the oldest plain ones went", "plain0" not in titles and "plain1" not in titles, titles)
        notifier.mark_read([keep["id"]])
        send("one-more")
        check("once acknowledged it is an ordinary item and can be pruned",
              "must-keep" not in [i["title"] for i in notifier._load_inbox()])
    finally:
        notifier.MAX_INBOX = saved


def test_default_cap_is_raised():
    check("MAX_INBOX is no longer the old 200", notifier.MAX_INBOX >= 500)


# --- the lock ----------------------------------------------------------------

def test_concurrent_writers_lose_nothing():
    fresh()
    errors = []

    def worker(n):
        try:
            send(f"w{n}", level=1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = {i["title"] for i in notifier._load_inbox()}
    check("no exceptions", not errors, errors)
    check("all 16 concurrent notifications are in the inbox", got == {f"w{n}" for n in range(16)}, sorted(got))


def test_mark_read_does_not_clobber_a_concurrent_append():
    fresh()
    first = send("first")
    stop = threading.Event()

    def appender():
        n = 0
        while not stop.is_set() and n < 25:
            send(f"x{n}", level=1)
            n += 1

    t = threading.Thread(target=appender)
    t.start()
    for _ in range(10):
        notifier.mark_read([first["id"]])
        notifier.mark_all_read()
    t.join()
    stop.set()
    titles = [i["title"] for i in notifier._load_inbox()]
    check("every appended notification survived the read marks",
          {f"x{n}" for n in range(25)} <= set(titles), len(titles))


def test_stale_lock_is_broken():
    fresh()
    lock = notifier.INBOX_FILE.with_suffix(".lock")
    lock.write_text("")
    old = time.time() - 60
    os.utime(str(lock), (old, old))
    t0 = time.time()
    send("after-stale-lock")
    check("a stale lockfile does not block delivery", time.time() - t0 < 2.0)
    check("the notification arrived", [i["title"] for i in notifier._load_inbox()] == ["after-stale-lock"])
    check("and the lock was released", not lock.exists())


def test_a_held_lock_never_blocks_delivery_forever():
    fresh()
    saved = notifier._LOCK_WAIT
    notifier._LOCK_WAIT = 0.25
    lock = notifier.INBOX_FILE.with_suffix(".lock")
    lock.write_text("")                                   # fresh: not stale
    try:
        t0 = time.time()
        send("despite-lock")
        check("delivery proceeds after the wait", time.time() - t0 < 3.0)
        check("the notification still arrived", [i["title"] for i in notifier._load_inbox()] == ["despite-lock"])
        check("a lock we did not take is left alone", lock.exists())
    finally:
        notifier._LOCK_WAIT = saved
        try:
            os.remove(str(lock))
        except OSError:
            pass


# --- legacy data -------------------------------------------------------------

def test_a_record_from_before_l30_still_reads():
    fresh()
    legacy = {"id": "legacy000001", "title": "old", "message": "from before", "kind": "notify",
              "created_at": "2026-09-01T10:00:00", "seen_by": ["web"], "failed": False}
    notifier._save_inbox([legacy])
    h = notifier.history(5)[0]
    check("a legacy record is unread", h["unread"] is True)
    check("and needs no acknowledgment", h["needs_ack"] is False)
    check("its summary is still backfilled", h.get("summary") == "from before", h.get("summary"))
    check("mark_read works on it", notifier.mark_read(["legacy000001"]) == 1)


# --- the CLI verbs -----------------------------------------------------------

def cli(*args):
    env = dict(os.environ, HOME=HOME, USERPROFILE=HOME)
    out = subprocess.run([sys.executable, "-m", "jarvis.cli", *args], capture_output=True, text=True,
                         cwd=str(ROOT / "jarvis-cli"), env=env, timeout=60)
    try:
        return json.loads(out.stdout), out.returncode
    except json.JSONDecodeError:
        return {"_raw": out.stdout, "_err": out.stderr}, out.returncode


def test_cli_verbs():
    fresh()
    a = send("Alpha", kind="task", level=2)
    b = send("Bravo failed", kind="task", level=3, failed=True)
    c = send("Charlie", kind="reminder", level=2)

    s, rc = cli("notify-summary")
    check("notify-summary exits 0", rc == 0, s)
    check("notify-summary counts", s.get("total") == 3 and s.get("unread") == 3, s)

    h, _ = cli("notify-history", "10", "--failed")
    check("notify-history --failed", ids_of(h["notifications"]) == [b["id"]], h)
    check("notify-history also carries the counts", h.get("total") == 3 and "kinds" in h, h)
    h, _ = cli("notify-history", "10", "--kind=reminder")
    check("notify-history --kind", ids_of(h["notifications"]) == [c["id"]], h)
    h, _ = cli("notify-history", "10", "--q=bravo failed")
    check("notify-history --q with spaces", ids_of(h["notifications"]) == [b["id"]], h)
    h, _ = cli("notify-history", "10", "--needs-ack")
    check("notify-history --needs-ack", ids_of(h["notifications"]) == [b["id"]], h)
    h, _ = cli("notify-history", "2")
    check("notify-history keeps the plain limit form", len(h["notifications"]) == 2, h)

    r, rc = cli("notify-read", a["id"])
    check("notify-read marks one", rc == 0 and r.get("read") == 1, r)
    h, _ = cli("notify-history", "10", "--unread")
    check("and it leaves the unread filter", a["id"] not in ids_of(h["notifications"]), h)
    r, _ = cli("notify-read", "all")
    check("notify-read all", r.get("read") == 2, r)
    r, rc = cli("notify-read")
    check("notify-read with no argument is a usage error", rc == 1 and "usage" in json.dumps(r), r)

    r, _ = cli("notify-dismiss", c["id"])
    check("notify-dismiss one", r.get("dismissed") == 1, r)
    r, _ = cli("notify-dismiss", "read")
    check("notify-dismiss read removes the rest (all are read)", r.get("dismissed") == 2, r)
    s, _ = cli("notify-summary")
    check("the inbox is empty afterwards", s.get("total") == 0, s)
    send("Delta")
    r, _ = cli("notify-dismiss", "all")
    check("notify-dismiss all", r.get("dismissed") == 1, r)
    r, rc = cli("notify-dismiss")
    check("notify-dismiss with no argument is a usage error", rc == 1 and "usage" in json.dumps(r), r)


def test_cli_ack_verb_is_still_delivery_only():
    fresh()
    r0 = send("Echo", level=3)
    out, _ = cli("notify-ack", r0["id"], "web")
    check("notify-ack still works", out.get("acknowledged") == 1, out)
    s, _ = cli("notify-summary")
    check("but the item is still unread and awaiting acknowledgment",
          s.get("unread") == 1 and len(s.get("needs_ack", [])) == 1, s)


# --- run ---------------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    crashed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            crashed += 1
            FAIL.append((name, f"crashed: {type(exc).__name__}: {exc}"))
            print(f"FAILED  {name}: crashed: {type(exc).__name__}: {exc}")
    print(f"\n{len(PASS)} checks passed, {len(FAIL)} failed across {len(tests)} tests")
    sys.exit(1 if FAIL else 0)
