"""Download Hub, DH-B (part 1): a finished hub download can be dismissed.

Covers the rule that moved: `service.dismiss` and `store.dismiss_items(hub_states=...)`.
  * a hub download that is done, failed or cancelled can be hidden; one that is waiting or running cannot;
  * `finished: true` (Clear finished) hides done and cancelled hub downloads and the provider rows it always hid,
    but leaves failed hub downloads (they may need a Retry);
  * hiding deletes nothing: the row stays in the database, History's own query can still read it
    (`include_dismissed`), and the file on disk is never touched (checked with a real temp file);
  * the store keeps its own guard: without `hub_states` it still refuses a finished hub row, and a `hub_states`
    entry that is not a final state is ignored;
  * a hidden failed hub row comes back when it is queued again (Retry).

Tested against a fake clock and a temporary database. No network, no Steam, no browser.
This file does NOT show that the History tab lists a hidden row: History's query lives in `hub_admin.history`, which
this brief does not own (see the report, "For the merge").

Run: python tests/test_hub_dismiss_finished.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-dismiss-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import service, states  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
HISTORY_STATES = ["done", "failed", "cancelled"]


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def new_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"hub-dismiss-{_n[0]}.db", clock=clock or time.time)


def sql(store, query, args=()):
    with store._write() as conn:
        conn.execute(query, args)


def hub_row(store, state, name, now):
    """A hub download put straight into `state` (the real paths take real time)."""
    item = store.add_item(engine="http", source_url="http://93.184.216.34/" + name, title=name)[0]
    finished = now if state in states.FINAL_STATES else None
    sql(store, "UPDATE items SET state=?, finished_at=? WHERE id=?", (state, finished, item["id"]))
    return item["id"]


def shown(store):
    return sorted(r["id"] for r in store.list_items(limit=100))


def test_store_rule():
    clock = Clock()
    store = new_store(clock)
    ids = {st: hub_row(store, st, st, clock()) for st in ("done", "failed", "cancelled", "queued", "downloading", "paused")}
    got = store.dismiss_items(list(ids.values()))
    check("the store's own rule is unchanged: with no hub_states only failed and cancelled hub rows are hidden",
          sorted(got) == sorted([ids["failed"], ids["cancelled"]]) and store.get_item(ids["done"])["dismissed_at"] is None)
    got = store.dismiss_items(list(ids.values()), hub_states=("done",))
    check("hub_states=('done',) lets a finished hub row go too, and only that one",
          got == [ids["done"]] and store.get_item(ids["done"])["dismissed_at"] is not None)
    check("a waiting, running or paused hub row stays", all(store.get_item(ids[s])["dismissed_at"] is None for s in ("queued", "downloading", "paused")))
    got = store.dismiss_items(list(ids.values()), hub_states=("queued", "downloading", "paused", "unknown", "bogus", None, 5))
    check("hub_states entries that are not final states are ignored", got == [])
    check("hiding it twice reports nothing the second time", store.dismiss_items([ids["done"]], hub_states=("done",)) == [])
    check("the row got the same event as any hidden row",
          any("dismissed" in e["message"] for e in store.list_events(ids["done"])))
    check("a bad hub_states value does not raise", store.dismiss_items([ids["queued"]], hub_states=None) == [])


def test_service_one_row():
    clock = Clock()
    store = new_store(clock)
    folder = Path(HOME) / "library"
    folder.mkdir()
    file = folder / "movie.mkv"
    file.write_bytes(b"the file the owner downloaded")
    stamp = file.stat().st_mtime_ns
    done_id = hub_row(store, "done", "movie", clock())
    sql(store, "UPDATE items SET dest_dir=?, dest_file=? WHERE id=?", (str(folder), "movie.mkv", done_id))
    failed_id = hub_row(store, "failed", "bad", clock())
    cancelled_id = hub_row(store, "cancelled", "gone", clock())
    queued_id = hub_row(store, "queued", "wait", clock())
    running_id = hub_row(store, "downloading", "run", clock())
    paused_id = hub_row(store, "paused", "pau", clock())

    counts_before = store.counts()
    res = service.dismiss(id=done_id, store=store)
    check("a finished hub download is dismissed", res["ok"] and res["dismissed"] == [done_id] and res["count"] == 1)
    check("the answer says no file was touched", "no file" in res["note"])
    check("it leaves the list", done_id not in shown(store))
    check("the row is kept: get_item finds it, hidden", store.get_item(done_id)["dismissed_at"] is not None)
    check("a store query for History's states still reads it with include_dismissed",
          done_id in [r["id"] for r in store.list_items(states_in=HISTORY_STATES, include_dismissed=True, limit=100)])
    check("and the default query hides it (so History needs include_dismissed: see the report)",
          done_id not in [r["id"] for r in store.list_items(states_in=HISTORY_STATES, limit=100)])
    check("Show hidden's query lists it", done_id in [r["id"] for r in store.list_items(limit=100, include_dismissed=True)])
    check("the file on disk is untouched (still there, same bytes, same modification time)",
          file.exists() and file.read_bytes() == b"the file the owner downloaded" and file.stat().st_mtime_ns == stamp)
    check("the status counts no longer count it", store.counts()["done"] == counts_before["done"] - 1)

    res = service.dismiss(id=done_id, store=store)
    check("dismissing it again is ok and says it was already hidden", res["ok"] and res["dismissed"] == [] and "already" in res["note"])
    res = service.dismiss(id=failed_id, store=store)
    check("a failed hub download can still be dismissed one by one", res["ok"] and res["dismissed"] == [failed_id])
    res = service.dismiss(id=cancelled_id, store=store)
    check("a cancelled one too", res["ok"] and res["dismissed"] == [cancelled_id])
    for name, rid in (("a waiting", queued_id), ("a running", running_id), ("a paused", paused_id)):
        res = service.dismiss(id=rid, store=store)
        check("%s hub download is refused and the answer points at dl_cancel" % name,
              res["ok"] is False and "dl_cancel" in res["error"] and store.get_item(rid)["dismissed_at"] is None)
    check("the refusal no longer says a finished download can't be hidden", "finished" in service.dismiss(id=queued_id, store=store)["error"])
    check("an unknown id is an error, not an exception", service.dismiss(id="h_nope", store=store)["ok"] is False)
    check("both an id and finished is still an error", service.dismiss(id=done_id, finished=True, store=store)["ok"] is False)
    check("neither is still an error", service.dismiss(store=store)["ok"] is False)


def test_service_clear_finished():
    clock = Clock()
    store = new_store(clock)
    store.register_provider("steam", label="Steam", mode="poll", poll_tool="get_x", capabilities=["reveal"])
    store.upsert_provider_row("steam", "fin", {"title": "Fin", "state": "done", "percent": 100})
    store.upsert_provider_row("steam", "run", {"title": "Run", "state": "downloading", "percent": 5})
    store.upsert_provider_row("steam", "unk", {"title": "Unk", "state": "unknown"})
    done_id = hub_row(store, "done", "d", clock())
    done2_id = hub_row(store, "done", "d2", clock())
    cancelled_id = hub_row(store, "cancelled", "c", clock())
    failed_id = hub_row(store, "failed", "f", clock())
    queued_id = hub_row(store, "queued", "q", clock())
    running_id = hub_row(store, "downloading", "r", clock())

    res = service.dismiss(finished=True, store=store)
    check("Clear finished hides done and cancelled hub downloads and the provider rows it always did",
          res["ok"] and sorted(res["dismissed"]) == sorted(["steam:fin", "steam:unk", done_id, done2_id, cancelled_id]) and res["count"] == 5)
    left = shown(store)
    check("a failed hub download, a waiting one, a running one and a running provider row stay",
          left == sorted([failed_id, queued_id, running_id, "steam:run"]))
    res = service.dismiss(finished=True, store=store)
    check("with nothing left to clear it says so", res["ok"] and res["count"] == 0 and "nothing" in res["note"])
    check("the failed one can still be hidden by itself afterwards", service.dismiss(id=failed_id, store=store)["dismissed"] == [failed_id])


def test_comes_back():
    clock = Clock()
    store = new_store(clock)
    failed_id = hub_row(store, "failed", "f", clock())
    done_id = hub_row(store, "done", "d", clock())
    service.dismiss(id=failed_id, store=store)
    service.dismiss(id=done_id, store=store)
    store.transition(failed_id, "queued")
    check("a hidden failed hub download comes back when it is queued again (Retry)", store.get_item(failed_id)["dismissed_at"] is None)
    check("a hidden finished one stays hidden meanwhile", store.get_item(done_id)["dismissed_at"] is not None)


def test_tool():
    default = Store()          # the default store under the test HOME, the one the tool uses
    row = default.add_item(engine="http", source_url="http://93.184.216.34/tool", title="tool")[0]
    sql(default, "UPDATE items SET state='done', finished_at=? WHERE id=?", (time.time(), row["id"]))
    out = dh.tool_dl_dismiss({"id": row["id"]})
    check("dl_dismiss hides a finished hub download for the owner (no chat context)", out["ok"] is True and out["dismissed"] == [row["id"]])
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
    row2 = default.add_item(engine="http", source_url="http://93.184.216.34/tool2", title="tool2")[0]
    sql(default, "UPDATE items SET state='done', finished_at=? WHERE id=?", (time.time(), row2["id"]))
    check("a guest still cannot", dh.tool_dl_dismiss({"id": row2["id"]})["ok"] is False and default.get_item(row2["id"])["dismissed_at"] is None)
    os.environ.pop("JARVIS_CHANNEL_SENDER", None)


def main():
    for fn in (test_store_rule, test_service_one_row, test_service_clear_finished, test_comes_back, test_tool):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s ran without raising (%s)" % (fn.__name__, exc), False)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
