"""Download Hub, brief 24 (N15): the queue arrows and the drag MEAN something to the daemon.

The panel's arrows end in hub_admin.reorder, which writes priorities.  This runs the real HubDaemon (tick() by
hand) with ONE slot (`concurrency.total` = 1) and checks that the row moved to the top is the row that starts
next, that moving it back changes who starts, and that a row moved down waits its turn.  Together with
tests/browser_hub_panel.py (the click in a real browser ends in the same stored order) this is the whole chain.

Run: python tests/test_hub_reorder_start_order.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-reorder-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import hub_admin  # noqa: E402
from jarvis.hub import engines, states  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.engines.base import Engine, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


class Holder(Engine):
    """Takes its slot and keeps it until the test lets go (options['mode'] is not used: every row blocks)."""
    name = "holder"
    label = "Holder"

    def run(self, item, ctx):
        ctx.set_state("downloading")
        while True:
            if ctx.stop_requested():
                return Outcome.requeue()
            time.sleep(0.02)


engines.load()
engines.ENGINES["holder"] = Holder()


_n = [0]


def env():
    _n[0] += 1
    store = Store(Path(HOME) / ("hub-%d.db" % _n[0]))
    store.set_settings({"library_root": str(Path(HOME) / "lib"), "concurrency.total": 1, "concurrency.holder": 1})
    rows = []
    for title in ("Alpha", "Bravo", "Charlie"):
        rows.append(store.add_item(engine="holder", source_url="holder://x/" + title, title=title)[0])
        time.sleep(0.02)
    return store, HubDaemon(store), rows


def pump_until_active(daemon, store, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        daemon.tick()
        act = [r["title"] for r in store.list_items(limit=50) if r["state"] in states.ACTIVE_STATES]
        if act:
            return act
        time.sleep(0.02)
    return []


def stop(daemon, store):
    try:
        daemon.stop()
    except Exception:  # noqa: BLE001
        pass


def test_top_row_starts_first():
    store, daemon, (a, b, c) = env()
    check("with no reorder the oldest row is the one the next slot goes to",
          [r["title"] for r in store.list_items(order="queue", states_in=("queued",))] == ["Alpha", "Bravo", "Charlie"])
    res = hub_admin.reorder({"ids": [c["id"], a["id"], b["id"]]}, store=store)
    check("reorder accepts Charlie, Alpha, Bravo", res.get("ok") is True)
    check("the stored queue is Charlie, Alpha, Bravo",
          [r["title"] for r in store.list_items(order="queue", states_in=("queued",))] == ["Charlie", "Alpha", "Bravo"])
    act = pump_until_active(daemon, store)
    check("one slot: the row moved to the top (Charlie) starts, and only it", act == ["Charlie"])
    stop(daemon, store)


def test_move_down_waits():
    store, daemon, (a, b, c) = env()
    # Alpha was first; the down arrow puts it second
    hub_admin.reorder({"ids": [b["id"], a["id"], c["id"]]}, store=store)
    act = pump_until_active(daemon, store)
    check("one slot: after Alpha moved down, Bravo starts, not Alpha", act == ["Bravo"])
    # the row that is waiting can still be reordered while another runs
    res = hub_admin.reorder({"ids": [c["id"], a["id"]]}, store=store)
    check("the rows still waiting can be reordered while one runs", res.get("ok") is True)
    check("... and the running row is not part of the waiting queue", [r["title"] for r in store.list_items(order="queue", states_in=("queued",))] == ["Charlie", "Alpha"])
    stop(daemon, store)


def test_running_row_is_refused():
    store, daemon, (a, b, c) = env()
    act = pump_until_active(daemon, store)
    check("one slot: Alpha (oldest) started", act == ["Alpha"])
    res = hub_admin.reorder({"ids": [c["id"], a["id"], b["id"]]}, store=store)
    check("a running row in the list makes reorder refuse (the panel never offers it)", res.get("ok") is False)
    stop(daemon, store)


for t in (test_top_row_starts_first, test_move_down_waits, test_running_row_is_refused):
    t()

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
