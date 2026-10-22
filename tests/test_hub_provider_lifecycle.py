"""Download Hub, Round 3 (agent B): the lifecycle of a provider's row.

Covers: a row that vanished from its provider's list says so in words; `unknown` settles into
`cancelled` after `unknown_settle_hours` (from the row's own timestamps, so a row that was already
unknown in an existing database settles too; a stale row, i.e. the provider is down, never does; a
provider reporting the row again first wins); dismiss (store rule, service rule, the tool, the guest
refusal, counts and lists hiding a dismissed row, a dismissed row coming back only when its provider
reports it live again); retention pruning (final rows older than keep_history_days, never active /
unknown / kept ones, never a file; 0 = forever); the setting; and the daemon wiring.

No network, no Steam, no real clock: a fake clock drives everything.

Run: python tests/test_hub_provider_lifecycle.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-life-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import tool_loader, tools as system_tools  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import providers, service, settings as hub_settings, states  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
H = 3600.0
DAY = 86400.0


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

    def advance(self, s):
        self.t += s


def new_store(clock=None):
    _n[0] += 1
    return Store(Path(HOME) / f"hub-life-{_n[0]}.db", clock=clock or time.time)


def provider_store(clock, label="Steam", pid="steam"):
    store = new_store(clock)
    store.register_provider(pid, label=label, mode="poll", poll_tool="get_x", capabilities=["reveal"])
    return store


def sql(store, query, args=()):
    with store._write() as conn:
        conn.execute(query, args)


def make_final(store, item_id, state, finished_at, owner="provider"):
    """Put a row straight into a final state with a chosen finish time (the real paths take real time)."""
    sql(store, "UPDATE items SET state=?, finished_at=?, updated_at=? WHERE id=?", (state, finished_at, finished_at, item_id))


def vanish(store, pid, local_id):
    """The provider's complete list no longer holds the row: it becomes unknown (below 100 %)."""
    providers.apply_poll(store, pid, {"items": [], "complete": True})


def guest_env(on):
    if on is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = on


GUEST = json.dumps({"platform": "discord", "user_id": "42", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "7", "is_owner": True})


# --------------------------------------------------------------------------- unknown says what happened

def test_vanished_wording():
    clock = Clock()
    store = provider_store(clock, label="Steam")
    store.upsert_provider_row("steam", "620", {"title": "Portal 2", "state": "downloading", "percent": 0.5,
                                               "message": "Downloading 12 MB/s"})
    vanish(store, "steam", "620")
    row = store.get_item("steam:620")
    check("a row that vanished below 100 % is unknown", row["state"] == "unknown")
    check("its message names the provider and says it stopped listing the row",
          "Steam stopped listing this" in (row.get("message") or "") and "Downloading" not in (row.get("message") or ""))
    check("and it is not flagged stale (the provider answered)", not row["stale"])
    store.upsert_provider_row("steam", "70", {"title": "Half-Life", "state": "downloading", "percent": 100})
    vanish(store, "steam", "70")
    check("one that vanished at 100 % is still done, as before", store.get_item("steam:70")["state"] == "done")


# --------------------------------------------------------------------------- settling

def test_settle():
    clock = Clock()
    store = provider_store(clock, label="Steam")
    store.upsert_provider_row("steam", "620", {"title": "Portal 2", "state": "downloading", "percent": 0.5})
    vanish(store, "steam", "620")
    became_unknown = clock()

    check("settle_hours defaults to 24 and survives junk",
          providers.settle_hours({}) == 24 and providers.settle_hours({"unknown_settle_hours": "x"}) == 24
          and providers.settle_hours({"unknown_settle_hours": 0}) == 24 and providers.settle_hours({"unknown_settle_hours": 6}) == 6
          and providers.settle_hours(None) == 24)

    clock.advance(23 * H)
    check("a row unknown for less than the limit is left alone", providers.settle_unknown(store, clock(), 24) == []
          and store.get_item("steam:620")["state"] == "unknown")
    clock.advance(2 * H)
    settled = providers.settle_unknown(store, clock(), 24)
    row = store.get_item("steam:620")
    check("after the limit it settles into cancelled", settled == ["steam:620"] and row["state"] == "cancelled")
    check("with the gave-up message (provider and hours)",
          row["message"] == "Steam never listed this again (gave up waiting after 24 h)")
    check("it has a finish time (so retention can date it) and the event says why",
          row["finished_at"] is not None and row["finished_at"] > became_unknown
          and any("gave up waiting" in e["message"] for e in store.list_events("steam:620")))
    check("settling twice does nothing", providers.settle_unknown(store, clock(), 24) == [])

    # the hours come from the caller (the setting): a shorter limit settles sooner
    store.upsert_provider_row("steam", "10", {"title": "Ten", "state": "downloading", "percent": 3})
    vanish(store, "steam", "10")
    clock.advance(3 * H)
    check("a 2 h limit settles a row unknown for 3 h", providers.settle_unknown(store, clock(), 2) == ["steam:10"]
          and "gave up waiting after 2 h" in store.get_item("steam:10")["message"])

    # a provider that reports the row again before the limit wins
    store.upsert_provider_row("steam", "11", {"title": "Eleven", "state": "downloading", "percent": 3})
    vanish(store, "steam", "11")
    clock.advance(20 * H)
    store.upsert_provider_row("steam", "11", {"title": "Eleven", "state": "downloading", "percent": 4})
    clock.advance(10 * H)
    check("a row the provider reported again is live again and never settles",
          providers.settle_unknown(store, clock(), 24) == [] and store.get_item("steam:11")["state"] == "downloading")

    # and a settled row comes back when the provider reports it live (a download restarted in Steam)
    store.upsert_provider_row("steam", "620", {"title": "Portal 2", "state": "downloading", "percent": 9})
    check("a settled row that the provider lists live again follows the provider", store.get_item("steam:620")["state"] == "downloading")


def test_settle_rules():
    clock = Clock()
    store = provider_store(clock, label="Steam")
    # a row already unknown in an existing database: only its own timestamps say how old it is
    store.upsert_provider_row("steam", "old", {"title": "Old", "state": "downloading", "percent": 1})
    sql(store, "UPDATE items SET state='unknown', stale=0, updated_at=? WHERE id='steam:old'", (clock() - 3 * DAY,))
    check("a row already unknown in an existing database settles (no new field needed)",
          providers.settle_unknown(store, clock(), 24) == ["steam:old"] and store.get_item("steam:old")["state"] == "cancelled")

    # Steam closed: the daemon's mark_stale flags it, then after a day moves it to unknown + stale = 1
    store.upsert_provider_row("steam", "closed", {"title": "Closed", "state": "downloading", "percent": 50})
    clock.advance(2 * DAY)
    flagged = store.mark_stale(now=clock())
    row = store.get_item("steam:closed")
    check("setup: a provider that went silent leaves an unknown row flagged stale", flagged["unknown"] == 1 and row["state"] == "unknown" and row["stale"])
    clock.advance(10 * DAY)
    check("a stale unknown row (the provider is down) is never settled", providers.settle_unknown(store, clock(), 24) == []
          and store.get_item("steam:closed")["state"] == "unknown")

    # a hub row, a finished row and a running row are none of its business
    hub = store.add_item(engine="http", source_url="http://93.184.216.34/f", title="h")[0]
    store.upsert_provider_row("steam", "run", {"title": "Run", "state": "downloading", "percent": 5})
    store.upsert_provider_row("steam", "fin", {"title": "Fin", "state": "done", "percent": 100})
    clock.advance(10 * DAY)
    providers.settle_unknown(store, clock(), 24)
    check("hub rows, running rows and finished rows are never settled",
          store.get_item(hub["id"])["state"] == "queued" and store.get_item("steam:run")["state"] == "downloading"
          and store.get_item("steam:fin")["state"] == "done")

    # a dismissed unknown row settles too (so it can be pruned later); a disabled provider's rows too
    store.upsert_provider_row("steam", "hid", {"title": "Hidden", "state": "downloading", "percent": 1})
    vanish(store, "steam", "hid")
    store.dismiss_items(["steam:hid"])
    store.register_provider("other", label="Other", mode="push")
    store.upsert_provider_row("other", "o", {"title": "O", "state": "unknown"})
    store.set_provider_enabled("other", False)
    clock.advance(2 * DAY)
    done = providers.settle_unknown(store, clock(), 24)
    check("a hidden row and a switched-off provider's row settle as well", "steam:hid" in done and "other:o" in done)
    check("and a hidden row stays hidden after it settles", store.get_item("steam:hid")["state"] == "cancelled"
          and store.get_item("steam:hid")["dismissed_at"] is not None)
    # the message uses the provider's own label
    check("the message names that row's own provider", store.get_item("other:o")["message"].startswith("Other never listed this again"))


def test_setting():
    check("the default is 24 hours", hub_settings.DEFAULTS["unknown_settle_hours"] == 24)
    check("the owner may change it (dl_prefs list)", "unknown_settle_hours" in hub_settings.PREFS_WRITABLE)
    clean, errors = hub_settings.validate_update({"unknown_settle_hours": 48}, allowed=hub_settings.PREFS_WRITABLE)
    check("48 is accepted through the dl_prefs path", clean == {"unknown_settle_hours": 48} and not errors)
    refused = []
    for value in (0, -3, 721, "soon", None, True):
        clean, errors = hub_settings.validate_update({"unknown_settle_hours": value}, allowed=hub_settings.PREFS_WRITABLE)
        refused.append(bool(errors) and not clean)
    check("0, a negative, over 720, a word, None and a bool are all refused", all(refused))
    clean, errors = hub_settings.validate_update({"unknown_settle_hours": 1}, allowed=hub_settings.PREFS_WRITABLE)
    clean2, errors2 = hub_settings.validate_update({"unknown_settle_hours": 720}, allowed=hub_settings.PREFS_WRITABLE)
    check("1 and 720 (the ends of the range) are accepted", not errors and not errors2)
    out = hub_settings.merged({"unknown_settle_hours": 12})
    check("a stored value is read back through merged()", out["unknown_settle_hours"] == 12 and hub_settings.merged({})["unknown_settle_hours"] == 24)
    check("the providers key is still there, as an empty block", hub_settings.merged({}).get("providers") == {})


# --------------------------------------------------------------------------- dismiss

def test_dismiss_store():
    clock = Clock()
    store = provider_store(clock)
    store.upsert_provider_row("steam", "run", {"title": "Run", "state": "downloading", "percent": 5})
    store.upsert_provider_row("steam", "fin", {"title": "Fin", "state": "done", "percent": 100})
    store.upsert_provider_row("steam", "unk", {"title": "Unk", "state": "downloading", "percent": 5})
    vanish(store, "steam", "unk")   # also finishes "run"? no: run is not in the (empty) complete list, so it goes unknown too
    store.upsert_provider_row("steam", "run", {"title": "Run", "state": "downloading", "percent": 5})
    hub = store.add_item(engine="http", source_url="http://93.184.216.34/f", title="h")[0]
    sql(store, "UPDATE items SET state='done', finished_at=? WHERE id=?", (clock(), hub["id"]))

    before = store.counts()
    done = store.dismiss_items(["steam:run", "steam:fin", "steam:unk", hub["id"], "nope:nope", ""])
    check("only unknown and finished provider rows are hidden (not a running row, not a hub row, not a missing id)",
          sorted(done) == ["steam:fin", "steam:unk"])
    check("the hub row, though finished, is not hidden", store.get_item(hub["id"])["dismissed_at"] is None)
    ids = [r["id"] for r in store.list_items(limit=50)]
    check("list_items hides dismissed rows by default", "steam:fin" not in ids and "steam:unk" not in ids and "steam:run" in ids and hub["id"] in ids)
    ids_all = [r["id"] for r in store.list_items(limit=50, include_dismissed=True)]
    check("and shows them when asked", "steam:fin" in ids_all and "steam:unk" in ids_all)
    after = store.counts()
    check("counts() no longer count them", after["total"] == before["total"] - 2 and after["done"] == before["done"] - 1)
    check("by_provider follows too", after["by_provider"]["steam"]["count"] == before["by_provider"]["steam"]["count"] - 2)
    check("get_item still finds a dismissed row by id", store.get_item("steam:fin")["dismissed_at"] is not None)
    check("hiding a row twice reports nothing the second time", store.dismiss_items(["steam:fin"]) == [])
    check("dismissing nothing is fine", store.dismiss_items([]) == [] and store.dismiss_items(None) == [])
    check("the row got an event saying nothing was deleted",
          any("dismissed" in e["message"] for e in store.list_events("steam:fin")))

    # coming back
    store.upsert_provider_row("steam", "fin", {"title": "Fin", "state": "done", "percent": 100})
    check("a provider that keeps saying 'done' does not bring a dismissed row back", store.get_item("steam:fin")["dismissed_at"] is not None)
    vanish(store, "steam", "none")
    check("a dismissed unknown row stays hidden through further polls", store.get_item("steam:unk")["dismissed_at"] is not None)
    store.upsert_provider_row("steam", "unk", {"title": "Unk", "state": "downloading", "percent": 6})
    back = store.get_item("steam:unk")
    check("a provider that reports the row live again brings it back", back["dismissed_at"] is None and back["state"] == "downloading")
    check("it is in the lists and the counts again", "steam:unk" in [r["id"] for r in store.list_items(limit=50)])
    check("and the event says so", any("shown again" in e["message"] for e in store.list_events("steam:unk")))
    store.upsert_provider_row("steam", "fin", {"title": "Fin", "state": "paused", "percent": 100})
    check("any live state brings it back (paused too)", store.get_item("steam:fin")["dismissed_at"] is None)


def test_dismiss_service_and_tool():
    clock = Clock()
    store = provider_store(clock)
    store.upsert_provider_row("steam", "run", {"title": "Run", "state": "downloading", "percent": 5})
    store.upsert_provider_row("steam", "fin", {"title": "Fin", "state": "done", "percent": 100})
    store.upsert_provider_row("steam", "fail", {"title": "Fail", "state": "failed"})
    store.upsert_provider_row("steam", "unk", {"title": "Unk", "state": "unknown"})
    store.upsert_provider_row("steam", "pau", {"title": "Pau", "state": "paused"})
    hub = store.add_item(engine="http", source_url="http://93.184.216.34/f", title="h")[0]

    res = service.dismiss(id="steam:fin", store=store)
    check("a finished provider row is dismissed", res["ok"] and res["dismissed"] == ["steam:fin"] and res["count"] == 1)
    check("the answer says nothing was deleted", "no file" in res["note"] or "nothing" in res["note"])
    res = service.dismiss(id="steam:fin", store=store)
    check("dismissing it again is ok and says it was already hidden", res["ok"] and res["dismissed"] == [] and "already" in res["note"])
    res = service.dismiss(id="steam:run", store=store)
    check("a running row is refused, with the reason", res["ok"] is False and "still running" in res["error"])
    res = service.dismiss(id="steam:pau", store=store)
    check("a paused row is refused too (it has not finished)", res["ok"] is False and "hasn't finished" in res["error"])
    res = service.dismiss(id=hub["id"], store=store)
    check("a hub download is refused and points at dl_cancel", res["ok"] is False and "dl_cancel" in res["error"]
          and store.get_item(hub["id"])["dismissed_at"] is None)
    check("an unknown id is an error, not an exception", service.dismiss(id="steam:nope", store=store)["ok"] is False)
    check("neither an id nor finished is an error", service.dismiss(store=store)["ok"] is False)
    check("both an id and finished is an error", service.dismiss(id="steam:fail", finished=True, store=store)["ok"] is False)

    res = service.dismiss(finished=True, store=store)
    check("finished=true hides every unknown or finished provider row that is left (failed, unknown)",
          res["ok"] and sorted(res["dismissed"]) == ["steam:fail", "steam:unk"] and res["count"] == 2)
    left = sorted(r["id"] for r in store.list_items(limit=50))
    check("a running row, a paused row and a hub row stay", left == sorted(["steam:run", "steam:pau", hub["id"]]))
    res = service.dismiss(finished=True, store=store)
    check("with nothing left to clear it says so", res["ok"] and res["count"] == 0 and "nothing" in res["note"])
    check("bulk control no longer sees hidden rows", all(r["id"] != "steam:fail" for r in store.list_items(provider="steam", limit=50)))

    # the tool: through the real tool functions, owner and guest
    schema = {s["name"]: s for s in dh.TOOL_SCHEMAS}
    check("dl_dismiss has a tiny schema (id, finished) and is a registered tool",
          "dl_dismiss" in dh.TOOLS and list(schema["dl_dismiss"]["parameters"]["properties"]) == ["id", "finished"]
          and len(json.dumps(schema["dl_dismiss"])) < 700)
    records = tool_loader.discover_actions(logger=lambda *a, **k: None)
    rec = [r for r in records if r.file.startswith("download_hub")][0]
    check("it is NOT confirm-gated (it hides, it deletes no file)", "dl_dismiss" not in rec.confirm_required)
    check("it has a checklist entry and keywords", "dl_dismiss" in rec.checklist and "dl_dismiss" in dh.TOOL_KEYWORDS)

    # the tool on the default store (the one under the test HOME)
    default = Store()
    default.register_provider("steam", label="Steam", mode="poll", poll_tool="get_x")
    default.upsert_provider_row("steam", "t1", {"title": "T1", "state": "done"})
    guest_env(GUEST)
    out = dh.tool_dl_dismiss({"id": "steam:t1"})
    check("a chat guest is refused in the handler", out["ok"] is False and "owner's" in out["error"]
          and default.get_item("steam:t1")["dismissed_at"] is None)
    check("so is finished=true", dh.tool_dl_dismiss({"finished": True})["ok"] is False and default.get_item("steam:t1")["dismissed_at"] is None)
    guest_env("{not json")
    check("an unreadable sender counts as a guest (fails closed)", dh.tool_dl_dismiss({"id": "steam:t1"})["ok"] is False)
    guest_env(OWNER)
    check("the owner in a chat is allowed", dh.tool_dl_dismiss({"id": "steam:t1"})["ok"] is True)
    guest_env(None)
    default.upsert_provider_row("steam", "t2", {"title": "T2", "state": "done"})
    check("no chat context (the PC) is the owner's own", dh.tool_dl_dismiss({"id": "steam:t2"})["ok"] is True)
    check("a non-dict argument is an error, not an exception", dh.tool_dl_dismiss(None)["ok"] is False)
    check("finished must be exactly true (the string 'true' is not)", dh.tool_dl_dismiss({"finished": "true"})["ok"] is False)


def test_owner_only():
    names = {"dl_add", "dl_add_ytdlp", "dl_add_stremio", "dl_dismiss", "bulk_plan", "bulk_run", "stremio_library", "stremio_mark_watched"}
    check("all eight names are in OWNER_ONLY_TOOLS", names <= set(system_tools.OWNER_ONLY_TOOLS))
    check("dl_list and dl_status stay open to a guest", not ({"dl_list", "dl_status"} & set(system_tools.OWNER_ONLY_TOOLS)))
    index = system_tools._tool_index()
    check("every one of them is a real tool", all(n in index for n in names))
    guest_env(GUEST)
    hidden = system_tools.hidden_from_sender()
    guest_env(None)
    check("a guest is not offered them", names <= set(hidden))
    check("the owner's own context hides none of them", not (names & set(system_tools.hidden_from_sender())))


# --------------------------------------------------------------------------- retention

def test_prune():
    clock = Clock()
    store = provider_store(clock)
    day = DAY
    now = clock()

    def row(local, state, age_days, **more):
        fields = {"title": local, "state": "downloading", "percent": 1}
        store.upsert_provider_row("steam", local, fields)
        make_final(store, "steam:" + local, state, now - age_days * day)
        if more.get("updated_only"):
            sql(store, "UPDATE items SET finished_at=NULL WHERE id=?", ("steam:" + local,))

    row("old_done", "done", 120)
    row("old_failed", "failed", 100)
    row("old_cancelled", "cancelled", 91)
    row("young_done", "done", 30)
    row("edge", "done", 89)
    row("no_finish_old", "done", 200, updated_only=True)
    row("no_finish_young", "done", 3, updated_only=True)
    store.upsert_provider_row("steam", "running", {"title": "r", "state": "downloading", "percent": 3})
    sql(store, "UPDATE items SET created_at=?, updated_at=? WHERE id='steam:running'", (now - 400 * day, now - 400 * day))
    store.upsert_provider_row("steam", "paused", {"title": "p", "state": "paused"})
    sql(store, "UPDATE items SET updated_at=? WHERE id='steam:paused'", (now - 400 * day,))
    store.upsert_provider_row("steam", "unk", {"title": "u", "state": "unknown"})
    sql(store, "UPDATE items SET updated_at=? WHERE id='steam:unk'", (now - 400 * day,))
    row("kept", "done", 300)
    sql(store, "UPDATE items SET keep=1 WHERE id='steam:kept'")
    row("hidden_old", "done", 150)
    store.dismiss_items(["steam:hidden_old"], now=now)
    queued = store.add_item(engine="http", source_url="http://93.184.216.34/q", title="queued")[0]
    sql(store, "UPDATE items SET updated_at=? WHERE id=?", (now - 400 * day, queued["id"]))
    old_hub = store.add_item(engine="http", source_url="http://93.184.216.34/h", title="old hub")[0]
    make_final(store, old_hub["id"], "done", now - 200 * day)

    f = Path(HOME) / "keepme.bin"
    f.write_bytes(b"data")
    sql(store, "UPDATE items SET dest_file=? WHERE id='steam:old_done'", (str(f),))

    # a group whose rows are partly pruned keeps its other members
    gid = store.ensure_group("a season") if hasattr(store, "ensure_group") else None
    if gid:
        sql(store, "UPDATE items SET group_id=? WHERE id IN ('steam:old_done','steam:young_done')", (gid,))

    check("keep_history_days <= 0 keeps everything", store.prune_history(0, now) == 0 and store.prune_history(-5, now) == 0)
    check("and so does a value that is not a number", store.prune_history(None, now) == 0 and store.prune_history("soon", now) == 0)
    n = store.prune_history(90, now)
    gone = [i for i in ("old_done", "old_failed", "old_cancelled", "no_finish_old", "hidden_old") if store.get_item("steam:" + i) is None]
    check("final rows older than the limit are deleted (done, failed, cancelled; a hidden one too)",
          sorted(gone) == sorted(["old_done", "old_failed", "old_cancelled", "no_finish_old", "hidden_old"]))
    check("a hub history row that old goes too", store.get_item(old_hub["id"]) is None)
    check("the count returned is what was deleted", n == 6)
    check("a row with no finish time is dated by its last update", store.get_item("steam:no_finish_young") is not None)
    check("younger rows stay (30 days, the edge at 89 days)", store.get_item("steam:young_done") is not None and store.get_item("steam:edge") is not None)
    check("running, paused, unknown and queued rows are never pruned, however old",
          all(store.get_item(i) is not None for i in ("steam:running", "steam:paused", "steam:unk", queued["id"])))
    check("a row flagged keep is never pruned", store.get_item("steam:kept") is not None)
    check("no file was touched", f.exists() and f.read_bytes() == b"data")
    check("the events of a pruned row went with it", store.list_events("steam:old_done") == [])
    if gid:
        check("a group's remaining rows are unaffected", store.get_item("steam:young_done")["group_id"] == gid)
    check("pruning again finds nothing", store.prune_history(90, now) == 0)
    clock.advance(70 * day)
    check("70 days later, the rows that were 30 / 89 days old are past the limit; a 3-day-old one is not",
          store.prune_history(90, clock()) == 2 and store.get_item("steam:young_done") is None
          and store.get_item("steam:edge") is None and store.get_item("steam:no_finish_young") is not None)
    check("a shorter limit takes shorter-lived rows", store.prune_history(1, clock()) >= 1 and store.get_item("steam:no_finish_young") is None)

    # delete_items: the state guard
    live = store.add_item(engine="http", source_url="http://93.184.216.34/z", title="z")[0]
    check("delete_items(only_states=...) leaves a row that is no longer final",
          store.delete_items([live["id"]], only_states=states.FINAL_STATES) == 0 and store.get_item(live["id"]) is not None)
    check("and without the guard it deletes as before", store.delete_items([live["id"]]) == 1)


def test_old_database():
    """An existing database (made before dismissed_at existed) gains the column and keeps working."""
    import sqlite3
    from jarvis.hub import store as store_mod
    path = Path(HOME) / "old-life.db"
    s1 = Store(path)
    s1.register_provider("steam", label="Steam", mode="poll", poll_tool="get_x")
    s1.upsert_provider_row("steam", "a", {"title": "A", "state": "done"})
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE items DROP COLUMN dismissed_at")
    conn.commit()
    names = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    conn.close()
    check("setup: the column is gone", "dismissed_at" not in names)
    store_mod._COLUMNS_CHECKED.discard(str(path))
    s2 = Store(path)
    check("opening it adds the column and the row is intact", s2.get_item("steam:a")["state"] == "done"
          and s2.get_item("steam:a")["dismissed_at"] is None)
    check("and dismiss works on it", s2.dismiss_items(["steam:a"]) == ["steam:a"] and s2.list_items() == [])


# --------------------------------------------------------------------------- the daemon

def test_daemon():
    clock = Clock()
    store = provider_store(clock)
    store.upsert_provider_row("steam", "gone", {"title": "Gone", "state": "downloading", "percent": 2})
    vanish(store, "steam", "gone")
    store.upsert_provider_row("steam", "ancient", {"title": "Ancient", "state": "done"})
    make_final(store, "steam:ancient", "done", clock() - 200 * DAY)
    store.upsert_provider_row("steam", "recent", {"title": "Recent", "state": "done"})
    make_final(store, "steam:recent", "done", clock() - 2 * DAY)

    daemon = HubDaemon(store, clock=clock)
    daemon.settings = lambda: dict(hub_settings.merged({}), unknown_settle_hours=24)
    daemon.start_up()
    check("start_up prunes: history past 90 days is gone, recent history stays",
          store.get_item("steam:ancient") is None and store.get_item("steam:recent") is not None)
    check("and does not settle a row that has only just gone unknown", store.get_item("steam:gone")["state"] == "unknown")

    clock.advance(25 * H)
    daemon._housekeeping()
    check("housekeeping settles an unknown row once the limit has passed", store.get_item("steam:gone")["state"] == "cancelled")

    # the hourly cadence (a monotonic clock in the daemon): not again within the hour, again after it
    calls = []
    real = store.prune_history
    store.prune_history = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    daemon._housekeeping()
    check("a second housekeeping inside the hour does not prune again", calls == [])
    daemon._last_prune -= 3601
    daemon._housekeeping()
    check("one more than an hour later does", calls == [1])

    # failures are logged and never stop the tick
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    store.prune_history = boom
    daemon._last_prune -= 3601
    try:
        daemon._housekeeping()
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    check("a pruning failure is swallowed by housekeeping", ok)
    store.prune_history = real
    real_settle = providers.settle_unknown
    providers.settle_unknown = boom
    try:
        daemon._housekeeping()
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    finally:
        providers.settle_unknown = real_settle
    check("and so is a settling failure", ok)

    # retention 0 keeps everything, through the real settings path
    store2 = provider_store(clock)
    store2.upsert_provider_row("steam", "old", {"title": "Old", "state": "done"})
    make_final(store2, "steam:old", "done", clock() - 500 * DAY)
    d2 = HubDaemon(store2, clock=clock)
    d2.settings = lambda: dict(hub_settings.merged({}), retention={"keep_history_days": 0, "auto_delete_files": False})
    d2.start_up()
    check("keep_history_days = 0 prunes nothing", store2.get_item("steam:old") is not None)


def main():
    for fn in (test_vanished_wording, test_settle, test_settle_rules, test_setting, test_dismiss_store,
               test_dismiss_service_and_tool, test_owner_only, test_prune, test_old_database, test_daemon):
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
