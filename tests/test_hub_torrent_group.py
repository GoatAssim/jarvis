"""Download Hub, N24 (part 2): choosing files of a torrent, and what the hub does with them.

Covers, against a FAKE Stremio server with a torrent of 30 nested files and a hand-made hostile one:
  * dl_add_stremio: the ask-first answer (nothing queued) for a pack, a torrent with exactly one real video queued as before,
    a named file queued with no look, the server down (queued the old way, with a note), file_index + file_indexes together,
    101 files, a dry run, and a .torrent on disk chosen by path (the path is never stored);
  * one group per torrent, one child row per chosen file, the layout <torrent>/<folders>/<file>, "Add more files" joining the
    same group, a file already in it skipped, the free-space check over the SUM of the chosen files, a chosen folder, a paused
    start, and a hostile name refused for that file alone (nothing queued for it, nothing written);
  * the engine: a child lands at <library>/<torrent>/<folders>/<file> with the server's exact bytes, and a hostile name that
    was forced into a row anyway is refused AGAIN there (no request is sent, nothing is written outside the library);
  * Pause / Resume / Cancel on the group touch only the unfinished files; the finished file stays;
  * ONE notification when the group ends (no line per file, no "all finished" echo), and an ordinary batch is unchanged;
  * hub_admin's two ops (list, add), refused for an unattended process, and the rows' torrent info.

Tested against fakes only.  Run: python tests/test_hub_torrent_group.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-tgroup-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER", "JARVIS_CHANNEL", "JARVIS_TASK_ID"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_stremio as fake  # noqa: E402
import _hub_testserver as srv  # noqa: E402
from jarvis import hub_admin  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.hub import notify, service, states  # noqa: E402
from jarvis.hub.engines import stremio as seng  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import server as sserver, torrentfile  # noqa: E402
from _hub_torrent_helpers import benc, episode_files, info_hash, torrent  # noqa: E402,F401

passed = failed = 0
_n = [0]


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("ok      " + name)
    else:
        failed += 1
        print("FAILED  " + name)


FAKE, BASE, ST = fake.start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
H30, HEVIL, HONE, HNEW = "30" * 20, "e1" * 20, "01" * 20, "77" * 20
magnet = lambda h, extra="": "magnet:?xt=urn:btih:%s%s" % (h, extra)  # noqa: E731
EVIL_FILES = [("Evil/ok.mkv", 100), ("Evil/../../escape.txt", 50), ("Evil/C:/win.txt", 5), ("Evil/CON.txt", 5), ("Evil/ok2.bin", 7)]


def env():
    _n[0] += 1
    lib = Path(HOME) / ("lib%d" % _n[0])
    store = Store(Path(HOME) / ("hub-%d.db" % _n[0]))
    store.set_settings({"library_root": str(lib), "http.stall_seconds": 3, "stremio.stall_seconds": 10, "stremio.metadata_timeout": 10})
    ST.reset()
    ST.settings["cacheRoot"] = str(Path(HOME) / "cache")
    ST.torrents.update({H30: {"name": "BigPack", "files": episode_files()}, HEVIL: {"name": "Evil", "files": EVIL_FILES},
                        HONE: {"name": "Movie", "files": [("Movie/Movie.mkv", 5000), ("Movie/Movie.srt", 5)]}})
    sserver.forget_probe()
    return store, lib


def children(store, gid):
    return [i for i in store.list_items(group_id=gid, limit=500, include_dismissed=True, include_disabled=True)]


def run(store, item, meta=2):
    settings = store.get_settings()
    settings["stremio"] = dict(settings["stremio"], metadata_timeout=meta)
    ctx = EngineContext(store, item, settings, stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    return seng.ENGINE.run(item, ctx)


def claim(store, item_id):
    """Claim ONE named row (the queue hands out the oldest first; the tests add rows one at a time, or claim in order)."""
    store.update_item(item_id, {"priority": 1000}, touch=False)         # the queue hands out the highest priority first
    got = store.claim_next({"default": 100}, 100)
    assert got is not None and got["id"] == item_id, (got and got["id"], item_id)
    return got


# --------------------------------------------------------------------------- 1. dl_add_stremio: ask first, or take the choice

def test_tool_ask_first():
    store, lib = env()
    default = Store()
    res = dh.tool_dl_add_stremio({"magnet": magnet(H30)})
    check("a pack with many files queues NOTHING and asks which (needs_clarification, queued 0)", res.get("needs_clarification") is True and res["queued"] == 0 and default.list_items(limit=10) == [])
    check("the question names the torrent, how many files and how big, and carries the whole list", "BigPack" in res["message"] and "30 files" in res["message"] and len(res["files"]) == 30
          and res["torrent"]["files"] == 30 and res["torrent"]["real_files"] == 29)
    check("it says how to answer: the same tool with file_indexes, at most 100", "file_indexes" in res["message"] and res["call"]["tool"] == "dl_add_stremio" and "100" in res["message"])
    check("the question reads the list and requests no file", not [e for e in ST.log if "download=1" in e[1]])

    one = dh.tool_dl_add_stremio({"magnet": magnet(HONE)})
    check("a torrent with exactly one real video (and a subtitle) is queued at once, as before", one["ok"] and one["count"] == 1 and one["added"][0]["engine"] == "stremio" and not one.get("needs_clarification"))
    check("...with no group", not default.list_groups())

    ST.reset()
    ST.torrents.update({H30: {"name": "BigPack", "files": episode_files()}})
    sserver.forget_probe()
    named = dh.tool_dl_add_stremio({"magnet": magnet(H30, "&so=3")})
    check("a file the source already names is queued with no look at the file list (no question)", named["ok"] and named["count"] == 1 and H30 not in ST.registered)
    named2 = dh.tool_dl_add_stremio({"magnet": magnet(H30), "file_index": 4})
    check("file_index (one file) still works the old way", named2["ok"] and named2["count"] == 1)

    os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
    sserver.forget_probe()
    try:
        down = dh.tool_dl_add_stremio({"magnet": magnet(HNEW)})
        check("the list can't be read (server stopped): queued the old way, and the note says so", down["ok"] and down["count"] == 1 and "couldn't read" in down["note"])
    finally:
        os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
        sserver.forget_probe()

    both = dh.tool_dl_add_stremio({"magnet": magnet(H30), "file_index": 1, "file_indexes": [2, 3]})
    check("file_index and file_indexes together are refused in a sentence", both["ok"] is False and "not both" in both["error"])
    many = dh.tool_dl_add_stremio({"magnet": magnet(H30), "file_indexes": list(range(101))})
    check("101 files are refused in a sentence (nothing is cut silently)", many["ok"] is False and "100" in many["error"])
    before = len(default.list_items(limit=50))
    dry = dh.tool_dl_add_stremio({"magnet": magnet(H30), "file_indexes": [0, 1], "dry_run": True})
    check("a dry run names the engine and queues nothing", dry["ok"] and dry["dry_run"] and dry["files"] == 2 and dry["decisions"][0]["engine"] == "stremio" and len(default.list_items(limit=50)) == before)
    junk = dh.tool_dl_add_stremio({"magnet": magnet(H30), "file_indexes": ["x"]})
    check("junk in file_indexes is refused", junk["ok"] is False)


# --------------------------------------------------------------------------- 2. one group, one child per file

def test_group_add():
    store, lib = env()
    res = service.add_torrent_files(magnet(H30), [0, 1, 2, 24], store=store, budget_s=3)
    check("four chosen files: ok, one group named after the torrent, four rows", res["ok"] and res["count"] == 4 and res["group_title"] == "BigPack" and len(res["added"]) == 4)
    gid = res["group"]
    groups = store.list_groups()
    check("the group is a torrent group and remembers which torrent and how many files it has", groups[gid]["kind"] == "torrent" and groups[gid]["meta"]["infohash"] == H30 and groups[gid]["meta"]["files"] == 30)
    rows = children(store, gid)
    check("every child is an ordinary waiting stremio row of that group", len(rows) == 4 and all(r["engine"] == "stremio" and r["state"] == "queued" and r["group_id"] == gid for r in rows))
    by_title = {r["title"]: r for r in rows}
    check("titles are the file names; the source is a magnet with the file number (so=)", "E01.mkv" in by_title and "making of.mp4" in by_title and "so=0" in by_title["E01.mkv"]["source_url"] and "so=24" in by_title["making of.mp4"]["source_url"])
    check("the layout is <torrent>/<folders inside it>", by_title["E01.mkv"]["options"]["subdir"] == "BigPack/Season 1" and by_title["making of.mp4"]["options"]["subdir"] == "BigPack/Extras"
          and by_title["E01.mkv"]["options"]["filename"] == "E01.mkv")
    check("the total is the sum of the chosen files", res["total_size"] == 1011 + 1012 + 1013 + 500)

    more = service.add_torrent_files(magnet(H30), [3, 4], store=store, budget_s=3)
    check("the same torrent again joins the SAME group (one group per torrent)", more["ok"] and more["group"] == gid and len(children(store, gid)) == 6 and len(store.list_groups()) == 1)
    again = service.add_torrent_files(magnet(H30), [0, 1], store=store, budget_s=3)
    check("a file already in the group is skipped, said in words, and nothing is added twice", again["ok"] is False and len(again["skipped"]) == 2 and "already in this group" in again["skipped"][0]["reason"]
          and len(children(store, gid)) == 6)
    mixed = service.add_torrent_files(magnet(H30), [0, 5], group_id=gid, store=store, budget_s=3)
    check("\"Add more files\" with the group id: the new one is added, the old one skipped", mixed["ok"] and mixed["count"] == 1 and len(mixed["skipped"]) == 1 and mixed["group"] == gid)
    other = service.add_torrent_files(magnet(HONE), [0], group_id=gid, store=store, budget_s=3)
    check("a group of another torrent is refused", other["ok"] is False and "different torrent" in other["error"])
    check("a group id that isn't a torrent group is refused", service.add_torrent_files(magnet(H30), [9], group_id="nope", store=store, budget_s=3)["ok"] is False)
    check("nothing chosen, and junk, are refused in a sentence", service.add_torrent_files(magnet(H30), [], store=store)["ok"] is False and service.add_torrent_files(None, None, store=store)["ok"] is False
          and service.add_torrent_files(magnet(H30), list(range(101)), store=store)["ok"] is False)

    # the free-space check covers the SUM, not each file
    old = service._free_bytes
    service._free_bytes = lambda path: 2500
    try:
        one = service.add_torrent_files(magnet(H30), [7], store=store, budget_s=3)
        three = service.add_torrent_files(magnet(H30), [8, 9, 10], store=store, budget_s=3)
        check("one file that fits is added; three that fit one by one but not together are refused as a whole", one["ok"] and three["ok"] is False and "not enough free space" in three["error"]
              and three["total_size"] == 1021 + 1022 + 1023)
        check("the refusal names the sum and the free space, and queues nothing", "3 chosen file" in three["error"] and len(children(store, gid)) == 8)
    finally:
        service._free_bytes = old

    store2, lib2 = env()
    paused = service.add_torrent_files(magnet(H30), [0, 1], start_paused=True, store=store2, budget_s=3)
    check("start_paused queues the children paused", paused["ok"] and all(a["state"] == "paused" for a in paused["added"]))
    store3, lib3 = env()
    moved = service.add_torrent_files(magnet(H30), [0], dest="Series", store=store3, budget_s=3)
    row = children(store3, moved["group"])[0]
    check("a chosen sub-folder goes in front of the torrent's folder", row["options"]["subdir"] == "Series/BigPack/Season 1")
    absolute = service.add_torrent_files(magnet(H30), [0], dest=str(Path(HOME) / "elsewhere"), store=store3, budget_s=3)
    check("a full path outside the library needs the owner's approval first (refused here)", absolute["ok"] is False and "approved" in absolute["error"])
    deep = service.add_torrent_files(magnet(H30), [0], dest="a/b/c", store=store3, budget_s=3)
    check("a chosen folder that leaves no room for the torrent's own folders refuses that file", deep["ok"] is False and ("deeper" in json.dumps(deep) or "nested" in json.dumps(deep)))

    # a hostile torrent: refused per file, the rest queued
    store4, lib4 = env()
    evil = service.add_torrent_files(magnet(HEVIL), [0, 1, 2, 3, 4], store=store4, budget_s=3)
    check("hostile names are refused PER FILE; the safe files of the same torrent are queued", evil["ok"] and evil["count"] == 2 and sorted(r["index"] for r in evil["refused"]) == [1, 2, 3]
          and sorted(a["index"] for a in evil["added"]) == [0, 4])
    rows = children(store4, evil["group"])
    flat = json.dumps([[r["source_url"], r["title"], r["options"], r["subtitle"]] for r in rows])
    check("nothing about a refused name reaches a row", "escape" not in flat and "win.txt" not in flat and "CON" not in flat and ".." not in flat)
    check("the answer lists the refused files with a reason, cleaned", all(r["reason"] and "\x00" not in r["path"] for r in evil["refused"]) and "note" in evil)
    only_bad = service.add_torrent_files(magnet(HEVIL), [1, 2], store=store4, budget_s=3)
    check("if every chosen file is refused, nothing is queued and no new group appears", only_bad["ok"] is False and len(store4.list_groups()) == 1 and len(children(store4, evil["group"])) == 2)

    # a .torrent on disk, chosen by path
    blob = torrent("Local Pack", files=[("a/one.mkv", 400, None), ("b/two.mkv", 500, None), ("b/three.mkv", 600, None)])
    path = Path(HOME) / "pick-me.torrent"
    path.write_bytes(blob)
    os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
    sserver.forget_probe()
    store5, lib5 = env()
    os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
    sserver.forget_probe()
    try:
        local = service.add_torrent_files(str(path), [0, 2], store=store5, budget_s=1)
        check("a .torrent file on this PC: the chosen files are queued with no server", local["ok"] and local["count"] == 2)
        rows = children(store5, local["group"])
        check("the rows are magnets built from the file, and the PATH is stored nowhere", all(r["source_url"].startswith("magnet:?xt=urn:btih:%s" % info_hash(blob)) for r in rows)
              and "pick-me" not in json.dumps([[r["source_url"], r["title"], r["options"], r["subtitle"]] for r in rows]) and "pick-me" not in json.dumps(store5.list_groups()))
        check("a copy was kept for the engine to find by hash", torrentfile.staged_blob(info_hash(blob)) == blob)
        via_tool = dh.tool_dl_add_stremio({"magnet": str(path), "file_indexes": [0, 1]})
        check("through dl_add_stremio the path is taken together with file_indexes (and the ask-first question is not asked)", via_tool["ok"] and via_tool["count"] == 2 and not via_tool.get("needs_clarification"))
        check("the folder is the torrent's name, the layout is kept", sorted(r["options"]["subdir"] for r in rows) == ["Local Pack/a", "Local Pack/b"])
    finally:
        os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
        sserver.forget_probe()

    # through the tool, a path is taken only together with a choice
    nopath = dh.tool_dl_add_stremio({"magnet": str(path)})
    check("a path without a choice goes on being refused, as before", nopath["ok"] is False and "not a magnet" in nopath["error"])


# --------------------------------------------------------------------------- 3. the engine

def test_engine():
    store, lib = env()
    res = service.add_torrent_files(magnet(H30), [0, 24], store=store, budget_s=3)
    first = claim(store, res["added"][0]["id"])
    out = run(store, first)
    final = Path(out.dest_file) if out.dest_file else None
    check("a child lands at <library>/<torrent>/<folders>/<file>", out.kind == Outcome.DONE and final and final == lib / "BigPack" / "Season 1" / "E01.mkv" and final.exists())
    check("with exactly the server's bytes and no .part left", final.read_bytes() == srv.payload(1011) and not Path(str(final) + ".part").exists())
    second = claim(store, res["added"][1]["id"])
    out2 = run(store, second)
    check("a file in another folder of the torrent goes to that folder", out2.kind == Outcome.DONE and Path(out2.dest_file) == lib / "BigPack" / "Extras" / "making of.mp4")

    # the engine checks the name AGAIN: a hostile name forced into a row is refused for that file only
    store, lib = env()
    ok_item = store.add_item(engine="stremio", source_url=magnet(HEVIL, "&so=0"), title="ok.mkv", kind="video",
                             options={"subdir": "Evil", "filename": "ok.mkv", "torrent": {"folder": "Evil", "root": "Evil", "single": False, "base": "", "base_depth": 0}})[0]
    bad = [store.add_item(engine="stremio", source_url=magnet(HEVIL, "&so=%d" % i), title="x", kind="file",
                          options={"subdir": "Evil", "filename": "x", "torrent": {"folder": "Evil", "root": "Evil", "single": False, "base": "", "base_depth": 0}})[0] for i in (1, 2, 3)]
    outcomes = {}
    for item in [ok_item] + bad:
        outcomes[item["id"]] = run(store, claim(store, item["id"]))
    check("the safe file of the hostile torrent is downloaded", outcomes[ok_item["id"]].kind == Outcome.DONE and (lib / "Evil" / "ok.mkv").read_bytes() == srv.payload(100))
    check("each hostile one fails with a sentence that says nothing was saved", all(outcomes[i["id"]].kind == Outcome.FAILED and "refused" in outcomes[i["id"]].error and "nothing was saved" in outcomes[i["id"]].error for i in bad))
    check("no hostile file was requested from the server", not [e for e in ST.log if "escape" in e[1] or "win.txt" in e[1] or "CON" in e[1]] and len([e for e in ST.log if "download=1" in e[1] and "external=1" in e[1]]) == 1)
    written = [p for p in Path(HOME).rglob("*") if p.is_file() and p.name in ("escape.txt", "win.txt", "CON.txt")]
    check("nothing was written anywhere, inside the library or outside it", written == [] and not (lib.parent / "escape.txt").exists())
    check("the failed rows are not retried (a hostile name does not get better)", all(not outcomes[i["id"]].retryable for i in bad))


# --------------------------------------------------------------------------- 4. pause / resume / cancel on the group

def test_group_control():
    store, lib = env()
    res = service.add_torrent_files(magnet(H30), [0, 1, 2], store=store, budget_s=3)
    gid = res["group"]
    first = claim(store, res["added"][0]["id"])
    out = run(store, first)
    store.transition(first["id"], "done", dest_file=out.dest_file, finished_at=time.time())
    done_file = Path(out.dest_file)
    paused = service.control("pause", group=gid, store=store)
    states_now = {r["id"]: r["state"] for r in children(store, gid)}
    check("Pause on the group pauses the unfinished files and leaves the finished one", sorted(paused["changed"]) == sorted(i["id"] for i in res["added"][1:]) and states_now[first["id"]] == "done"
          and all(states_now[i["id"]] == "paused" for i in res["added"][1:]))
    check("the finished file is reported as skipped, with the reason", any(s["id"] == first["id"] for s in paused["skipped"]))
    resumed = service.control("resume", group=gid, store=store)
    check("Resume wakes only the paused ones", sorted(resumed["changed"]) == sorted(i["id"] for i in res["added"][1:]) and {r["id"]: r["state"] for r in children(store, gid)}[first["id"]] == "done")
    cancelled = service.control("cancel", group=gid, store=store)
    final_states = {r["id"]: r["state"] for r in children(store, gid)}
    check("Cancel on the group cancels the unfinished files; the finished one stays done", sorted(cancelled["changed"]) == sorted(i["id"] for i in res["added"][1:])
          and final_states[first["id"]] == "done" and all(final_states[i["id"]] == "cancelled" for i in res["added"][1:]))
    check("and the finished file is still on disk", done_file.exists() and done_file.read_bytes() == srv.payload(1011))


# --------------------------------------------------------------------------- 5. ONE notification when the group ends

class Clock:
    def __init__(self):
        self.t = 2_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, title, message, *, level=None, failed=False, channels=None, look=None, tone=None):
        self.sent.append({"title": title, "message": message, "failed": failed, "channels": channels})
        return {"delivered": ["inbox"], "failed_channels": []}


def settle(store, item, state="done"):
    if store.get_item(item["id"])["state"] == "queued":
        with store._write() as conn:
            conn.execute("UPDATE items SET state='probing', attempts=attempts+1, started_at=COALESCE(started_at, ?) WHERE id=?", (store.now(), item["id"]))
    fields = {"dest_file": "/library/BigPack/" + item["title"]} if state == "done" else {"error": "no peers"}
    return store.transition(item["id"], state, **fields)


def test_notify():
    store, lib = env()
    clock, sink = Clock(), Sink()
    disp = notify.Dispatcher(store, deliver=sink, clock=clock)
    disp.since()
    clock.advance(1)
    res = service.add_torrent_files(magnet(H30), [0, 1, 2], store=store, budget_s=3)
    rows = [store.get_item(a["id"]) for a in res["added"]]
    settle(store, rows[0])
    disp.scan(store.get_settings())
    clock.advance(30)
    disp.scan(store.get_settings())
    check("while a file is still unfinished the group says nothing", sink.sent == [])
    settle(store, rows[1])
    settle(store, rows[2], "failed")
    disp.scan(store.get_settings())
    clock.advance(30)
    disp.scan(store.get_settings())
    clock.advance(30)
    disp.scan(store.get_settings())
    check("when the group ends there is exactly ONE message", len(sink.sent) == 1)
    msg = sink.sent[0]
    check("it says how many files finished, out of how many, and what failed", msg["title"] == "Torrent finished" and "BigPack" in msg["message"] and "2 of 3 files finished" in msg["message"] and "1 failed" in msg["message"])
    check("no line per file, and no 'all downloads finished' echo", not [m for m in sink.sent if m["title"].startswith("Download") or m["title"] == "All downloads finished"])

    # a torrent group of ONE chosen file still speaks once
    store1, _ = env()
    clock1, sink1 = Clock(), Sink()
    d1 = notify.Dispatcher(store1, deliver=sink1, clock=clock1)
    d1.since()
    clock1.advance(1)
    one = service.add_torrent_files(magnet(H30), [5], store=store1, budget_s=3)
    settle(store1, store1.get_item(one["added"][0]["id"]))
    for _ in range(3):
        d1.scan(store1.get_settings())
        clock1.advance(30)
    check("one chosen file: one message, in the group's words", len(sink1.sent) == 1 and "1 of 1 files finished" in sink1.sent[0]["message"])

    # an ordinary batch keeps its wording and its per-file lines
    store2, _ = env()
    clock2, sink2 = Clock(), Sink()
    d2 = notify.Dispatcher(store2, deliver=sink2, clock=clock2)
    d2.since()
    clock2.advance(1)
    gid = store2.ensure_group("Plain batch")
    plain = [store2.add_item(engine="http", source_url="https://example.com/f%d.zip" % i, title="f%d.zip" % i, group_id=gid)[0] for i in range(2)]
    for item in plain:
        settle(store2, item)
    for _ in range(3):
        d2.scan(store2.get_settings())
        clock2.advance(30)
    check("an ordinary batch still says 'Batch finished' and still announces its files", [m for m in sink2.sent if m["title"] == "Batch finished" and "2 finished" in m["message"]]
          and [m for m in sink2.sent if "finished" in m["title"] and m["title"] != "Batch finished"])


# --------------------------------------------------------------------------- 6. hub_admin: the two ops and the rows

def test_hub_admin():
    store, lib = env()
    check("the two ops exist and are listed", set(hub_admin.TORRENT_OPS) == {"torrent_files", "torrent_add"} and set(hub_admin.TORRENT_OPS) <= set(hub_admin.OPS))
    listed = hub_admin.admin("torrent_files", {"source": magnet(H30), "wait_s": 3}, store=store)
    check("torrent_files: the list for the picker, with the free space, the limit and no group yet", listed["ok"] and listed["count"] == 30 and len(listed["files"]) == 30 and listed["max_pick"] == 100
          and isinstance(listed["free_bytes"], int) and listed["group"] is None and listed["needs_choice"] is True and listed["name"] == "BigPack"
          and {"index", "path", "size"} <= set(listed["files"][0]))
    check("it started nothing", store.list_items(limit=10) == [] and not [e for e in ST.log if "download=1" in e[1]])
    added = hub_admin.admin("torrent_add", {"source": magnet(H30), "indexes": [0, 1, 2]}, store=store)
    check("torrent_add queues the chosen files as one group", added["ok"] and added["count"] == 3 and added["group"])
    again = hub_admin.admin("torrent_files", {"source": magnet(H30), "wait_s": 3}, store=store)
    check("listing the torrent again says which files are already in its group", again["group"] == added["group"] and [f.get("in_group") for f in again["files"][:3]] == ["queued"] * 3
          and "in_group" not in again["files"][3])
    more = hub_admin.admin("torrent_add", {"source": magnet(H30), "indexes": [3], "group": added["group"]}, store=store)
    check("\"Add more files\": the new file joins the group", more["ok"] and more["group"] == added["group"] and len(children(store, added["group"])) == 4)
    snap = hub_admin.admin("list", {}, store=store)
    tors = [r for r in snap["items"] if r.get("group_id") == added["group"]]
    check("the rows say which file of which torrent they are, and how many files it has", len(tors) == 4 and all(r["torrent"]["files"] == 30 and r["torrent"]["infohash"] == H30 and r["torrent"]["name"] == "BigPack" for r in tors)
          and sorted(r["torrent"]["index"] for r in tors) == [0, 1, 2, 3])
    plain = store.add_item(engine="http", source_url="https://example.com/p.zip", title="p.zip")[0]
    snap = hub_admin.admin("list", {}, store=store)
    check("an ordinary row carries no torrent info", "torrent" not in [r for r in snap["items"] if r["id"] == plain["id"]][0])
    check("bad requests are plain errors", hub_admin.admin("torrent_files", {}, store=store)["ok"] is False and hub_admin.admin("torrent_files", {"source": "a\x00b"}, store=store)["ok"] is False
          and hub_admin.admin("torrent_files", {"source": "x" * 5000}, store=store)["ok"] is False and hub_admin.admin("torrent_add", {"source": magnet(H30), "indexes": [1], "group": "../x"}, store=store)["ok"] is False
          and hub_admin.admin("torrent_add", {"source": magnet(H30), "indexes": list(range(101))}, store=store)["ok"] is False)
    check("a hostile torrent through the panel's add: refused per file, answered in words", (lambda r: r["ok"] and r["count"] == 2 and len(r["refused"]) == 3)(
        hub_admin.admin("torrent_add", {"source": magnet(HEVIL), "indexes": [0, 1, 2, 3, 4]}, store=store)))
    for var in ("JARVIS_SCHEDULED", "JARVIS_CHANNEL", "JARVIS_TASK_ID", "JARVIS_CHANNEL_SENDER"):
        os.environ[var] = "1"
        blocked = [hub_admin.admin(op, {"source": magnet(H30), "indexes": [5]}, store=store) for op in hub_admin.TORRENT_OPS]
        os.environ.pop(var)
        check("an unattended process (%s) is refused for both ops, and nothing is queued" % var, all(b["ok"] is False and b.get("code") == "unattended" for b in blocked) and len(children(store, added["group"])) == 4)


try:
    test_tool_ask_first()
    test_group_add()
    test_engine()
    test_group_control()
    test_notify()
    test_hub_admin()
finally:
    fake.stop(FAKE)
    shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
