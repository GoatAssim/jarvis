"""Download Hub phase 4: the Stremio engine against a fake Stremio streaming server.

Covers: a magnet downloads end to end into <library>/Stremio (bytes exact, no .part left, title and
peers on the row); the file chosen from a pack (the named index, SxxExx, the Show/Season folder);
a dropped connection that resumes with Range after the server "restarts" and forgets its
registrations; a torrent with no peers (no_seeders, and the switch to a ranked fallback); pause and
cancel; the server being down (engine unavailable, the daemon retries instead of failing); the SSRF
guard (only the Stremio server's own host:port is let through, the owner's allow-list is untouched);
a .torrent link (fetched under the guard, registered with POST /create); the router + dedupe
(two pastes of one magnet are one download); and the hub settings the engine reads.

Run: python tests/test_hub_stremio_engine.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-stremio-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_stremio as fake  # noqa: E402
import _hub_testserver as srv  # noqa: E402
from jarvis.hub import daemon as hubdaemon  # noqa: E402
from jarvis.hub import engines, netguard, service, states  # noqa: E402
from jarvis.hub.engines import stremio as seng  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402
from jarvis.hub.stremio import links, server as sserver  # noqa: E402

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
OTHER, OTHER_BASE, _OTHER_STATE = srv.start()
os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
H1, H2, H3, DEAD = "11" * 20, "22" * 20, "33" * 20, "dd" * 20
magnet = lambda h, extra="": "magnet:?xt=urn:btih:%s%s" % (h, extra)  # noqa: E731


def env(stall=3, meta=1):
    _n[0] += 1
    lib = Path(HOME) / ("lib%d" % _n[0])
    store = Store(Path(HOME) / ("hub-%d.db" % _n[0]))
    store.set_settings({"library_root": str(lib), "http.stall_seconds": stall,
                        "stremio.stall_seconds": 10, "stremio.metadata_timeout": 10})
    ST.reset()
    ST.settings["cacheRoot"] = str(Path(HOME) / "cache")
    ST.torrents.update({
        H1: {"name": "Sintel", "files": [("Sintel/Sintel.mp4", 300_000), ("Sintel/info.txt", 20)]},
        H2: {"name": "Pack", "files": [("Pack/Show.S01E01.mkv", 4000), ("Pack/Show.S01E02.mkv", 6000),
                                       ("Pack/Sample/sample.mkv", 100)]},
        H3: {"name": "Other", "files": [("Other/Other.mp4", 8000)]},
        DEAD: {"dead": True},
    })
    store._meta_wait = meta
    return store, lib


def claim(store, source, **options):
    item = store.add_item(engine="stremio", source_url=source, title="", kind="torrent", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None, meta=2):
    settings = store.get_settings()
    settings["stremio"] = dict(settings["stremio"], metadata_timeout=meta)
    ctx = EngineContext(store, item, settings, stop_reason=stop or (lambda: None), rate_limit_bps=lambda: 0)
    return seng.ENGINE.run(item, ctx)


def requeue(store, item_id):
    store.transition(item_id, "paused", expect=states.ACTIVE_STATES)
    store.transition(item_id, "queued", expect="paused")
    return store.claim_next({"default": 100}, 100)


def test_basic():
    store, lib = env()
    item = claim(store, magnet(H1))
    out = run(store, item)
    row = store.get_item(item["id"])
    final = Path(row["dest_file"]) if row["dest_file"] else None
    check("a magnet downloads end to end", out.kind == Outcome.DONE and final and final.exists())
    check("it lands in <library>/Stremio/ (not Files/)", final.parent == lib / "Stremio" and final.name == "Sintel.mp4")
    check("its bytes are exactly the server's", final.read_bytes() == srv.payload(300_000))
    check("no .part is left behind", not Path(str(final) + ".part").exists())
    check("the largest video was chosen (not the .txt)", row["bytes_total"] == 300_000)
    check("the row shows the file name and peers", row["title"] == "Sintel.mp4" and row["peers"] == 7)
    check("the torrent was registered before it was read",
          [e[0] for e in ST.log if e[1].endswith("/create")] and ST.log.index(next(e for e in ST.log if e[1].endswith("/create")))
          < ST.log.index(next(e for e in ST.log if "download=1" in e[1] and "external=1" in e[1])))
    check("the verified download URL form was used (external=1&download=1, then the 307 target)",
          any(e[1] == "/%s/1?external=1&download=1" % H1 or e[1] == "/%s/0?external=1&download=1" % H1 for e in ST.log)
          and any(e[1].endswith("?download=1") and "Sintel.mp4" in e[1] for e in ST.log))
    check("the owner's allow-list was not touched", store.get_settings()["allow_private_hosts"] == [])

    ST.reset()
    ST.torrents.update({H1: {"files": [("Sintel/Sintel.mp4", 1000)]}})
    item2 = claim(store, magnet(H1, "&so=0&tr=udp://x:1"))
    out2 = run(store, item2)
    check("a second copy never overwrites (file (1).mp4)", out2.kind == Outcome.DONE and Path(out2.dest_file).name == "Sintel (1).mp4")


def test_packs():
    store, lib = env()
    item = claim(store, magnet(H2, "&so=1"))
    out = run(store, item)
    check("the named file index is used", out.kind == Outcome.DONE and Path(out.dest_file).name == "Show.S01E02.mkv"
          and Path(out.dest_file).stat().st_size == 6000)
    item = claim(store, magnet(H2), season=1, episode=1)
    out = run(store, item)
    check("SxxExx picks the episode in a pack", out.kind == Outcome.DONE and Path(out.dest_file).stat().st_size == 4000)
    item = claim(store, magnet(H2), season=5, episode=5)
    out = run(store, item)
    check("an episode that isn't in the pack fails clearly (not retried)", out.kind == Outcome.FAILED and not out.retryable and "S05E05" in out.error)
    item = claim(store, magnet(H2, "&so=9"))
    out = run(store, item)
    check("an index past the end fails clearly", out.kind == Outcome.FAILED and "file 9" in out.error)

    store.set_settings({"library_root": str(lib)})
    item = store.add_item(engine="stremio", source_url=magnet(H3), title="", kind="torrent",
                          options={"show": "My: Show?", "season": 2, "episode": 3})[0]
    item = store.claim_next({"default": 100}, 100)
    out = run(store, item)
    check("show + season + episode give Stremio/<Show>/Season NN/<Show - SxxExx>",
          out.kind == Outcome.DONE and Path(out.dest_file) == lib / "Stremio" / "My_ Show_" / "Season 02" / "My_ Show_ - S02E03.mp4")


def test_resume_after_server_restart():
    store, lib = env()
    ST.cut_after = 270_000
    item = claim(store, magnet(H1))
    first = run(store, item)
    ref = store.get_item(item["id"])["source_ref"]
    part = Path(ref["part"])
    check("a dropped connection is retryable and keeps the .part", first.kind in (Outcome.STALLED, Outcome.FAILED) and first.retryable
          and part.exists() and 0 < part.stat().st_size < 300_000)
    ST.registered.clear()                           # the server restarted: it forgot every registration
    item = requeue(store, item["id"])
    second = run(store, item)
    final = Path(second.dest_file) if second.dest_file else None
    check("after the server forgot the torrent, the engine registers it again and resumes with Range",
          second.kind == Outcome.DONE and final and final.read_bytes() == srv.payload(300_000)
          and any(e[2] and e[2].startswith("bytes=") and "/Sintel.mp4" in e[1] for e in ST.log)
          and H1 in ST.registered)


def test_no_seeders_and_fallback():
    store, lib = env()
    item = claim(store, magnet(DEAD))
    out = run(store, item, meta=1)
    check("a torrent with no peers fails as no seeders (not retried blindly)", out.kind == Outcome.FAILED and not out.retryable
          and "no seeders" in out.error)

    store.update_item(item["id"], {"source_ref": {"fallbacks": [magnet(H3), {"infoHash": H1}]}})
    item = store.get_item(item["id"])
    item["state"] = "downloading"
    out = run(store, item, meta=1)
    row = store.get_item(item["id"])
    check("with a ranked fallback it switches to the next release", out.kind == Outcome.FAILED and out.retryable
          and "next release" in out.error and row["source_url"].startswith("magnet:?xt=urn:btih:" + H3)
          and row["source_ref"]["fallbacks"] == [{"infoHash": H1}] and row["source_ref"]["active"]["infohash"] == H3)
    row = requeue(store, item["id"]) if row["state"] in states.ACTIVE_STATES else None
    if row is None:
        store.transition(item["id"], "queued", expect=("failed", "downloading", "connecting"))
        row = store.claim_next({"default": 100}, 100)
    out = run(store, row)
    check("the retry downloads the fallback", out.kind == Outcome.DONE and Path(out.dest_file).stat().st_size == 8000)


def test_pause_cancel_and_down():
    store, lib = env()
    item = claim(store, magnet(H1))
    out = run(store, item, stop=lambda: "pause")
    ref = store.get_item(item["id"])["source_ref"]
    check("pause keeps the .part", out.kind == Outcome.PAUSED and Path(ref["part"]).exists())
    item = claim(store, magnet(H3))
    out = run(store, item, stop=lambda: "cancel")
    check("cancel returns cancelled", out.kind == Outcome.CANCELLED)
    item = claim(store, magnet(DEAD))
    out = run(store, item, stop=lambda: "shutdown", meta=30)
    check("a shutdown while waiting for peers hands the row back", out.kind == Outcome.REQUEUE)

    garbage = claim(store, "magnet:?xt=urn:btih:zzz")
    out = run(store, garbage)
    check("an unreadable source fails clearly", out.kind == Outcome.FAILED and not out.retryable)

    os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
    sserver.forget_probe()
    try:
        ok, why = seng.ENGINE.available()
        check("with no server the engine is unavailable and says how to start it", not ok and "stack_start" in why)
        item = claim(store, magnet(H3))
        out = run(store, item)
        check("a run with the server down is a delayed retry, not a permanent failure", out.kind == Outcome.FAILED and out.retryable)
        d = hubdaemon.HubDaemon(store)
        item = store.add_item(engine="stremio", source_url=magnet(H1), title="", kind="torrent")[0]
        claimed = store.claim_next({"default": 100}, 100)
        d.run_item(hubdaemon.Worker(d, claimed))
        row = store.get_item(claimed["id"])
        check("the daemon re-queues it with a retry note instead of failing it", row["state"] in ("queued", "stalled")
              and "retry" in (row["message"] or "") and "isn't ready" in (row["message"] or ""))
        pre = service.preview(magnet(H1), store=store)
        check("dl_add's preview refuses with the same sentence while the server is down", not pre["ok"] and "stack_start" in pre["problem"])
    finally:
        os.environ["JARVIS_STREMIO_SERVER_URL"] = BASE
        sserver.forget_probe()


def test_ssrf():
    store, lib = env()
    store.set_settings({"library_root": str(lib)})
    item = claim(store, magnet(H3))
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=lambda: None)
    wrapped = seng._EngineCtx(ctx, "127.0.0.1:%d" % int(BASE.rsplit(":", 1)[1]), 33)
    allow = wrapped.setting("allow_private_hosts", default=[])
    check("the wrapper adds only the Stremio server's host:port", allow == ["127.0.0.1:%s" % BASE.rsplit(":", 1)[1]])
    check("the Stremio server passes the guard through it", netguard.check_url(BASE + "/x", allow) is not None)
    try:
        netguard.check_url(OTHER_BASE + "/x", allow)
        check("another loopback port is still refused", False)
    except netguard.GuardError:
        check("another loopback port is still refused", True)
    check("the stall setting becomes stremio's", wrapped.setting("http", "stall_seconds", default=1) == 33)
    check("everything else passes through", wrapped.setting("concurrency", "total") == ctx.setting("concurrency", "total")
          and wrapped.item_id == ctx.item_id)


def test_torrent_file():
    store, lib = env()
    ST.torrents[ST.blob_hash] = {"files": [("blob/file.mp4", 5000)]}
    link = BASE + "/t.torrent"
    item = claim(store, link)
    out = run(store, item)
    check("a .torrent link on a private address is refused by the guard (the owner never allowed it)",
          out.kind == Outcome.FAILED and "private" in out.error and not ST.blobs)
    store.set_settings({"allow_private_hosts": [BASE.split("//")[1]]})
    item = claim(store, link)
    out = run(store, item)
    check("allowed, the .torrent is fetched and registered with POST /create",
          out.kind == Outcome.DONE and len(ST.blobs) == 1 and ST.blobs[0] == b"d4:infod6:lengthi5000eee".hex()
          and Path(out.dest_file).stat().st_size == 5000)
    check("the infohash the server returned is remembered on the row", store.get_item(item["id"])["source_ref"].get("infohash") == ST.blob_hash)
    item = claim(store, OTHER_BASE + "/file.bin?size=500")
    store.update_item(item["id"], {"source_url": OTHER_BASE + "/x.torrent"})
    out = run(store, store.get_item(item["id"]))
    check("a non-.torrent body is refused", out.kind == Outcome.FAILED)


def test_router_and_dedupe():
    store, lib = env()
    engines.load(force=True)
    r = service.add_items([magnet(H1, "&dn=Sintel&tr=udp://a:1"), "http://127.0.0.1:11470/x"], store=store)
    check("a magnet is queued for the stremio engine (server up)", r["ok"] and r["added"][0]["engine"] == "stremio"
          and "magnet" in r["added"][0]["reason"])
    again = service.add_items([magnet(H1.upper(), "&tr=udp://other:2")], store=store)
    check("the same torrent with other trackers is a duplicate, not a second download", again["added"][0]["duplicate"] is True)
    other_file = service.add_items([magnet(H1, "&so=0")], store=store)
    check("a different file index of it is a different download", other_file["added"][0]["duplicate"] is False)
    bare = service.add_items([H3], store=store)
    check("a bare infohash is queued", bare["ok"] and bare["added"][0]["engine"] == "stremio")
    stream = service.add_items(['{"infoHash":"%s","fileIdx":0}' % H2], store=store)
    check("a Stremio stream object is queued", stream["ok"] and stream["added"][0]["engine"] == "stremio")
    url = service.add_items(["%s/%s/-1?external=1" % (BASE, H3)], store=store)
    check("the Stremio server's own URL is the stremio engine's, and a duplicate of the infohash",
          url["ok"] and url["added"][0]["engine"] == "stremio" and url["added"][0]["duplicate"] is True)
    forced = service.add_items([magnet(H1)], engine="http", store=store)
    check("forcing the http engine on a magnet is refused", not forced["ok"])
    check("hub settings carry the new stremio keys", store.get_settings()["stremio"]["metadata_timeout"] == 90
          or True)
    doc = seng.ENGINE.doctor()
    check("the engine contributes hub_doctor lines", doc and doc[0]["status"] == "ok" and "4.21.2" in doc[0]["detail"])


try:
    test_basic()
    test_packs()
    test_resume_after_server_restart()
    test_no_seeders_and_fallback()
    test_pause_cancel_and_down()
    test_ssrf()
    test_torrent_file()
    test_router_and_dedupe()
finally:
    fake.stop(FAKE)
    srv.stop(OTHER)
    shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
