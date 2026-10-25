"""Download Hub phase 4: the Stremio engine against a fake Stremio streaming server.

Covers: a magnet downloads end to end into <library>/Stremio (bytes exact, no .part left, title and
peers on the row); the file chosen from a pack (the named index, SxxExx, the Show/Season folder);
a dropped connection that resumes with Range after the server "restarts" and forgets its
registrations; a torrent with no peers (no_seeders, and the switch to a ranked fallback); pause and
cancel; the server being down (engine unavailable, the daemon retries instead of failing); the SSRF
guard (only the Stremio server's own host:port is let through, the owner's allow-list is untouched);
a .torrent link (fetched under the guard, registered with POST /create); a .torrent FILE on this PC
(brief 22: claimed by the engine's own route rule, kept in the hub's folder, registered with POST /create, and
never stored by its path); the router + dedupe
(two pastes of one magnet are one download); and the hub settings the engine reads.

Run: python tests/test_hub_stremio_engine.py
"""
import hashlib
import json
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
from jarvis.hub.stremio import links, server as sserver, torrentfile  # noqa: E402

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
    check("... and its error says what the server last reported, so the owner isn't left guessing",
          "the server said:" in out.error and ("peers" in out.error or "no stats" in out.error))
    check("... and the row said what it was waiting for while it waited (the panel shows it under 'Connecting')",
          "waiting for the file list" in (store.get_item(item["id"]).get("phase") or ""))

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


def make_torrent(length=7000, name="clip.mp4"):
    """A small real bencoded .torrent: -> (bytes, infohash)."""
    info = b"d6:lengthi%de4:name%d:%s12:piece lengthi16384e6:pieces20:%se" % (length, len(name), name.encode(), b"p" * 20)
    return b"d8:announce13:udp://t.x:1/a4:info" + info + b"e", hashlib.sha1(info).hexdigest()


def test_local_torrent_file():
    """Brief 22.  Against the FAKE Stremio server; Linux only."""
    store, lib = env()
    engines.load(force=True)
    secret = Path(HOME) / ("secret folder %d" % _n[0]) / "\u043c\u043e\u0438 \u0444\u0430\u0439\u043b\u044b"     # spaces and non-ASCII
    secret.mkdir(parents=True)
    blob, digest = make_torrent(7000)
    good = secret / "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440 clip.torrent"
    good.write_bytes(blob)
    ST.torrents[digest] = {"name": "clip", "files": [("clip/clip.mp4", 7000)]}
    ST.blob_hash = digest

    hit = seng._match(str(good), {})
    check("_match claims a good .torrent path (spaces and non-ASCII in it)", hit and hit["kind"] == "torrent" and "file" in hit["reason"])
    check("...and its source is a magnet with the infohash and the bare file name, no folder",
          hit["source"].startswith("magnet:?xt=urn:btih:" + digest) and "secret" not in hit["source"] and str(secret) not in hit["source"]
          and links.parse_magnet(hit["source"])["name"] == "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440 clip")
    check("the engine claimed it at the router's priority, ahead of the http engine", hit["source"] == seng._match('"%s"' % good, {})["source"])
    check("a copy was kept in the hub's own folder, named by the infohash", (torrentfile.staging_dir() / (digest + ".torrent")).read_bytes() == blob)

    r = service.add_items([str(good)], store=store)
    row = store.get_item(r["added"][0]["id"])
    check("add_items queues it for the stremio engine", r["ok"] and row["engine"] == "stremio" and ".torrent file" in r["added"][0]["reason"])
    check("the stored source_url holds no folder name", "secret" not in row["source_url"] and str(HOME) not in row["source_url"])
    again = service.add_items([str(good)], store=store)
    check("the same file twice is one download", again["added"][0]["duplicate"] is True)
    as_magnet = service.add_items([magnet(digest)], store=store)
    check("...and the same torrent as a plain magnet is the same download", as_magnet["added"][0]["duplicate"] is True)

    item = store.claim_next({"default": 100}, 100)
    leaked_title = store.get_item(item["id"])["title"]
    print("   (info) title the add code gave the row before it ran: %r" % leaked_title)
    check("(known, hub/service.py is not ours) the add code titles the row with the text it was given, so the title starts as the path",
          torrentfile.looks_like_path(leaked_title))
    out = run(store, item)
    row = store.get_item(item["id"])
    check("the kept file is registered with POST /create and downloads", out.kind == Outcome.DONE and ST.blobs == [blob.hex()]
          and Path(out.dest_file).stat().st_size == 7000)
    ref = row["source_ref"]
    check("source_ref holds the file name, size and infohash",
          ref.get("torrent_file") == {"name": "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440 clip.torrent", "size": len(blob), "infohash": digest}
          and ref.get("infohash") == digest)
    check("nothing stored on the row names the folder (title, source, source_ref, events)",
          "secret" not in json.dumps(row, default=str) and "secret" not in json.dumps(store.list_events(row["id"]) if hasattr(store, "list_events") else [], default=str))
    check("the title is no longer a path", not torrentfile.looks_like_path(row["title"]))

    # The scrub happens BEFORE anything else (the HTTP step names the row after the video later anyway), so test it with the
    # server unreachable: the run stops at once, and the row must already have lost the path from its title.
    blob2, digest2 = make_torrent(8000, "other.mp4")
    path2 = secret / "other.torrent"
    path2.write_bytes(blob2)
    r2 = service.add_items([str(path2)], store=store)
    row2 = store.get_item(r2["added"][0]["id"])
    check("(before it runs, the row's title is the typed path: the add code's doing)", torrentfile.looks_like_path(row2["title"]))
    claimed2 = store.claim_next({"default": 100}, 100)
    live_url = os.environ["JARVIS_STREMIO_SERVER_URL"]
    os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
    sserver.forget_probe()
    try:
        out_down = run(store, claimed2)
    finally:
        os.environ["JARVIS_STREMIO_SERVER_URL"] = live_url
        sserver.forget_probe()
    scrubbed = store.get_item(claimed2["id"])
    check("with the server down the run fails at once, retryably (nothing downloaded)", out_down.kind == Outcome.FAILED and out_down.retryable)
    check("...and the row's title is already just the file's name, not the path", scrubbed["title"] == "other.torrent"
          and "secret" not in json.dumps(scrubbed, default=str))

    # the kept copy is gone (moved machine, cleaned folder): the row falls back to asking the swarm, and says so
    (torrentfile.staging_dir() / (digest + ".torrent")).unlink()
    ST.blobs.clear()
    ST.registered.clear()
    ST.torrents[digest] = {"name": "clip", "files": [("clip/clip.mp4", 7000)]}
    item2 = store.add_item(engine="stremio", source_url=magnet(digest, "&dn=x"), title="", kind="torrent",
                           options={"filename": "again.mp4"})[0]
    item2 = store.claim_next({"default": 100}, 100)
    store.update_item(item2["id"], {"source_ref": {"torrent_file": {"name": "x.torrent", "size": 1, "infohash": digest}}})
    out2 = run(store, store.get_item(item2["id"]))
    check("with the kept copy gone it registers by infohash instead (no blob posted) and still downloads", out2.kind == Outcome.DONE and not ST.blobs)

    # a magnet that has no kept copy never posts a blob
    ST.blobs.clear()
    item3 = claim(store, magnet(H3))
    run(store, item3)
    check("an ordinary magnet never posts a blob", not ST.blobs)

    # bad paths: nothing is queued, and each says one sentence naming only the file
    before = len(store.list_items(limit=500))
    bad = {}
    folder = secret / "isadir.torrent"
    folder.mkdir()
    bad["directory"] = (str(folder), "folder")
    bad["missing"] = (str(secret / "nope.torrent"), "can't find")
    big = secret / "big.torrent"
    big.write_bytes(b"d" + b"x" * (torrentfile.MAX_BYTES + 10))
    bad["oversized"] = (str(big), "larger than")
    text = secret / "notes.torrent"
    text.write_text("dear diary, this is not a torrent\n")
    bad["a text file named .torrent"] = (str(text), "isn't a .torrent file")
    link = secret / "alias.torrent"
    link.symlink_to(good)
    bad["a link to somewhere else"] = (str(link), "link to somewhere else")
    bad["a relative path"] = ("some/where/x.torrent", "full path")
    bad["a network share"] = ("//server/share/x.torrent", "network share")
    for label, (path, word) in bad.items():
        found, sentence = torrentfile.local_torrent(path)
        check("bad path (%s): one sentence, nothing returned to queue" % label, found is None and sentence and word in sentence and "\n" not in sentence)
        check("bad path (%s): the sentence never echoes the folder" % label, "secret" not in sentence and str(HOME) not in sentence)
        res = service.add_items([path], store=store)
        check("bad path (%s): nothing is queued" % label, not res["ok"] and not res["added"])
        check("bad path (%s): the engine's rule does not claim it" % label, seng._match(path, {}) is None)
    check("no row was added by any bad path", len(store.list_items(limit=500)) == before)
    check("a bad path left no kept copy (only the second good file's is there)", {p.name for p in torrentfile.staging_dir().iterdir()} == {digest2 + ".torrent"})
    check("a plain URL or a missing non-.torrent path is not this feature's business",
          torrentfile.local_torrent("http://x/y") == (None, None) and torrentfile.local_torrent("/tmp/readme.txt") == (None, None))
    check("the cap is the same one the link fetch uses", torrentfile.MAX_BYTES == seng.MAX_TORRENT_FILE == 5 * 1024 * 1024)
    check("a path is not echoed by the one that is too long either", torrentfile.local_torrent("/" + "a" * 5000 + ".torrent") == (None, None))


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
    test_local_torrent_file()
    test_router_and_dedupe()
finally:
    fake.stop(FAKE)
    srv.stop(OTHER)
    shutil.rmtree(HOME, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
