"""Download Hub, phase 9: the qBittorrent engine against a fake of qBittorrent's Web API.

Covers: the config file and `available()` (not set up, a Web UI that isn't on this PC, a wrong
password that never leaks, qBittorrent not running); magnet parsing (base32, tracker filter,
`xs=` dropped); a fresh add end to end (savepath under the library root, tag, live percent /
speed / ETA / peers / seeds / phase, files as children, the final path); attaching to a torrent
already in qBittorrent (no second add); pause and resume on both generations of qBittorrent
(stop/start and pause/resume); shutdown -> requeue, then a re-attach with no duplicate; cancel
(removes only a torrent the hub added, deletes partial data only inside the library root, only
pauses the owner's own torrent); every way a run can end badly (error state, vanished torrent,
qBittorrent going away, metadata timeout, stall, full disk, refused add, expired session);
`after_done`; the hub's rate limit; routing (never takes magnets by default, explicit override
works, opt-in falls through when qBittorrent is down); `hub_doctor`'s line; and that the password
appears nowhere a run writes.

Run: python tests/test_hub_qbittorrent_engine.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-qb-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_fake_qbittorrent as fake  # noqa: E402
from jarvis.hub import engines, router, states  # noqa: E402
from jarvis.hub.engines import qbittorrent as qbe  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

# Fast polls for the tests; a real config is never polled faster than MIN_POLL_S.
qbe.MIN_POLL_S = 0.02
qbe.MIN_METADATA_S = 0.05
qbe._sleep = lambda s: time.sleep(min(s, 0.02))

passed = failed = 0
_n = [0]
SERVERS = []


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


HASH = "ab" * 20
MAGNET = "magnet:?xt=urn:btih:%s&dn=Some%%20Show" % HASH
SIZE = 1_000_000


def new_qb(**kwargs):
    qb = fake.start(fake.FakeQb(**kwargs))
    SERVERS.append(qb)
    return qb


def config_file():
    return Path(HOME) / ".jarvis" / "qbittorrent.json"


def write_config(qb, **extra):
    cfg = {"address": qb.base, "username": qb.username, "password": qb.password, "poll_s": 0.02}
    cfg.update(extra)
    cfg = {k: v for k, v in cfg.items() if v is not None}
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps(cfg), encoding="utf-8")
    qbe.ENGINE._avail = None
    return cfg


def env():
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    store.set_settings({"library_root": str(lib)})
    return store, lib


def claim(store, source=MAGNET, **options):
    item = store.add_item(engine="qbittorrent", source_url=source, title="", kind="torrent", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None, limit=0):
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None),
                        rate_limit_bps=lambda: limit)
    return qbe.ENGINE.run(item, ctx)


def play(qb, frames):
    """Each torrents/info call that finds the torrent applies the next frame (a dict of fields, or a
    callable(torrent)); when the frames run out the torrent stays as it is."""
    queue = list(frames)

    def tick(torrent):
        if not queue:
            return
        frame = queue.pop(0)
        if callable(frame):
            frame(torrent)
        else:
            torrent.update(frame)
    qb.on_info = tick


def downloading(lib, progress=0.4, **extra):
    row = {"state": "downloading", "name": "Some Show", "size": SIZE, "total_size": SIZE,
           "progress": progress, "completed": int(SIZE * progress), "dlspeed": 250_000, "eta": 3,
           "num_seeds": 4, "num_leechs": 6, "save_path": str(lib / "Torrents"),
           "content_path": str(lib / "Torrents" / "Some Show")}
    row.update(extra)
    return row


def complete(lib, **extra):
    row = downloading(lib, 1.0, state="uploading", dlspeed=0, eta=8640000)
    row.update(extra)
    return row


def stop_after(calls, reason):
    """A `stop_requested` that says None for `calls` calls, then `reason` forever."""
    box = [0]

    def stop():
        box[0] += 1
        return reason if box[0] > calls else None
    return stop


def requeue_like_the_daemon(store):
    store.recover_after_restart()
    return store.claim_next({"default": 100}, 100)


# --------------------------------------------------------------------------- config / availability

def test_config_and_available():
    if config_file().exists():
        config_file().unlink()
    qbe.ENGINE._avail = None
    ok, why = qbe.ENGINE.available()
    check("without qbittorrent.json the engine says it is not set up", not ok and "not set up" in why)

    qb = new_qb()
    write_config(qb, address="http://192.168.1.20:8080")
    ok, why = qbe.ENGINE.available()
    check("a Web UI that isn't on this PC is refused", not ok and "on this PC" in why)
    write_config(qb, address="http://user:pw@127.0.0.1:8080")
    ok, why = qbe.ENGINE.available()
    check("credentials inside the address are refused", not ok and "pw@" not in why)

    write_config(qb)
    ok, why = qbe.ENGINE.available()
    check("with qBittorrent up and the right login the engine is available", ok and why == "")

    write_config(qb, password="s3cr3t-Pa55-wrong")
    ok, why = qbe.ENGINE.available()
    check("a wrong password says so, without repeating it", not ok and "rejected" in why and "s3cr3t" not in why)

    write_config(qb, enabled=False)
    ok, why = qbe.ENGINE.available()
    check("\"enabled\": false switches it off", not ok and "switched off" in why)

    config_file().write_text("\ufeff" + json.dumps({"address": qb.base, "username": qb.username,
                                                    "password": qb.password}), encoding="utf-8")
    qbe.ENGINE._avail = None
    check("a file saved with a BOM still loads", qbe.ENGINE.available()[0])

    config_file().write_text("{not json", encoding="utf-8")
    qbe.ENGINE._avail = None
    ok, why = qbe.ENGINE.available()
    check("a broken file is a one-line problem, not a crash", not ok and "can't read" in why)

    write_config(qb)
    qb.stop_server()
    qbe.ENGINE._avail = None
    ok, why = qbe.ENGINE.available()
    check("qBittorrent not running is reported as unreachable", not ok and "can't reach" in why)


def test_bypass_login():
    qb = new_qb(username="", password="")
    qb.bypass = True
    write_config(qb, username=None, password=None)
    check("with 'bypass authentication' no user name is needed", qbe.ENGINE.available()[0] and qb.logins == 0)


# --------------------------------------------------------------------------- magnets

def test_magnets():
    import base64
    raw = bytes(range(20))
    parsed = qbe.parse_magnet("magnet:?xt=urn:btih:%s" % base64.b32encode(raw).decode())
    check("a base32 info hash becomes hex", parsed and parsed["infohash"] == raw.hex())
    parsed = qbe.parse_magnet(
        "magnet:?xt=urn:btih:%s&dn=Some%%20Show&tr=udp://tracker.opentrackr.org:1337/announce"
        "&tr=http://192.168.1.5/announce&tr=http://localhost/a&tr=http://127.0.0.1:9/a&tr=ftp://x.example/a"
        "&xs=http://evil.example/x.torrent&ws=http://evil.example/w&as=http://evil.example/a" % HASH.upper())
    check("the hash is lower-cased and the name kept", parsed["infohash"] == HASH and parsed["name"] == "Some Show")
    check("only trackers that point somewhere public survive",
          parsed["trackers"] == ["udp://tracker.opentrackr.org:1337/announce"])
    check("xs / ws / as never reach qBittorrent", "evil" not in parsed["magnet"] and "xs=" not in parsed["magnet"])
    check("a bare 40-hex hash is a magnet", qbe.parse_magnet("c" * 40)["magnet"] == "magnet:?xt=urn:btih:" + "c" * 40)
    check("garbage and v2-only magnets are refused",
          qbe.parse_magnet("hello") is None and qbe.parse_magnet("magnet:?xt=urn:btmh:1220" + "a" * 64) is None
          and qbe.parse_magnet("http://example.com/a.torrent") is None and qbe.parse_magnet("") is None)
    many = "magnet:?xt=urn:btih:%s" % HASH + "".join("&tr=udp://t%d.example.com:80/a" % i for i in range(40))
    check("at most 20 trackers are passed on", len(qbe.parse_magnet(many)["trackers"]) == 20)


# --------------------------------------------------------------------------- a normal run

def test_basic_download():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    item = claim(store)
    snaps = {}

    def meta_look(t):
        snaps["meta"] = store.get_item(item["id"])
        t.update(downloading(lib))

    def mid_look(t):
        snaps["mid"] = store.get_item(item["id"])
        t.update(complete(lib))
    play(qb, [{}, meta_look, mid_look])
    out = run(store, item)
    row = store.get_item(item["id"])
    target = str(lib / "Torrents" / "Some Show")

    check("a magnet added with the engine finishes", out.kind == Outcome.DONE and out.dest_file == target)
    adds = qb.calls("torrents/add")
    check("qBittorrent was asked once, to save under <library>/Torrents with the hub's tag",
          len(adds) == 1 and adds[0]["savepath"] == str(lib / "Torrents") and adds[0]["tags"] == "jarvis"
          and adds[0]["autoTMM"] == "false" and adds[0]["paused"] == "false" and adds[0]["stopped"] == "false")
    check("what it received was the rebuilt magnet", HASH in adds[0]["urls"] and adds[0]["urls"].startswith("magnet:?xt=urn:btih:"))
    check("the save folder exists", (lib / "Torrents").is_dir())
    check("fetching metadata is an indeterminate bar with its own phase",
          snaps["meta"]["percent"] is None and snaps["meta"]["phase"] == "fetching metadata"
          and snaps["meta"]["state"] == "connecting")
    mid = snaps["mid"]
    check("progress is qBittorrent's own: percent, bytes, speed, ETA, peers, seeds",
          abs(mid["percent"] - 40.0) < 0.5 and mid["bytes_total"] == SIZE and mid["bytes_done"] == 400_000
          and mid["speed_bps"] == 250_000 and mid["eta_s"] == 3 and mid["seeds"] == 4 and mid["peers"] == 10)
    check("the row is downloading with no leftover phase", mid["state"] == "downloading" and mid["phase"] is None)
    check("the torrent's name replaces the placeholder title", row["title"] == "Some Show")
    check("its files are listed as children", [c["name"] for c in mid["children"]] == ["show/ep1.mkv", "show/ep2.mkv"])
    check("the info hash is remembered for a re-attach",
          row["source_ref"]["infohash"] == HASH and row["source_ref"]["added_by_hub"] is True)
    check("the drawer shows the info hash and qBittorrent's version",
          {"label": "Info hash", "value": HASH} in row["detail"]
          and any(d["label"] == "qBittorrent" and d["value"] == "v5.0.1" for d in row["detail"]))
    check("the row ends with the full size and the folder it lives in",
          row["bytes_done"] == SIZE and row["bytes_total"] == SIZE and row["dest_dir"] == str(lib / "Torrents")
          and row["dest_file"] == target and row["kind"] == "torrent")
    check("it keeps seeding in qBittorrent by default (nothing stopped or removed)",
          not qb.calls("torrents/stop") and not qb.deleted)

    item2 = claim(store, "magnet:?xt=urn:btih:%s" % ("cd" * 20), subdir="Shows/Kids")
    play(qb, [{}, complete(lib, name="Other")])
    out = run(store, item2)
    adds = qb.calls("torrents/add")
    check("a download's own sub-folder option decides where qBittorrent saves",
          out.kind == Outcome.DONE and adds[-1]["savepath"] == str(lib / "Shows" / "Kids"))
    item3 = claim(store, "magnet:?xt=urn:btih:%s" % ("ef" * 20), subdir="../outside")
    out = run(store, item3)
    check("a folder outside the library root is refused", out.kind == Outcome.FAILED and "destination" in out.error)
    item4 = claim(store, "https://example.com/file.torrent")
    out = run(store, item4)
    check("only magnets and info hashes are taken", out.kind == Outcome.FAILED and "magnet" in out.error)


def test_attach_existing():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    qb.torrent(HASH, **complete(lib))
    item = claim(store)
    out = run(store, item)
    row = store.get_item(item["id"])
    check("a torrent already in qBittorrent is attached to, never added again",
          out.kind == Outcome.DONE and not qb.calls("torrents/add"))
    check("and the hub knows it did not add it", row["source_ref"]["added_by_hub"] is False)


def test_after_done():
    for mode in ("keep", "pause", "remove"):
        qb = new_qb()
        write_config(qb, after_done=mode)
        store, lib = env()
        play(qb, [{}, complete(lib)])
        out = run(store, claim(store))
        if mode == "keep":
            check("after_done=keep leaves the torrent seeding", out.kind == Outcome.DONE and not qb.calls("torrents/stop") and not qb.deleted)
        elif mode == "pause":
            check("after_done=pause stops it in qBittorrent", out.kind == Outcome.DONE and qb.calls("torrents/stop"))
        else:
            check("after_done=remove drops the entry and keeps the files", out.kind == Outcome.DONE and qb.deleted == [(HASH, False)])
    qb = new_qb()
    write_config(qb, after_done="remove")
    store, lib = env()
    qb.torrent(HASH, **complete(lib))
    out = run(store, claim(store))
    check("after_done=remove never removes the owner's own torrent", out.kind == Outcome.DONE and not qb.deleted)


# --------------------------------------------------------------------------- pause / resume / restart

def test_pause_and_resume():
    for modern in (True, False):
        label = "5.x (stop/start)" if modern else "4.x (pause/resume)"
        qb = new_qb(modern=modern)
        write_config(qb)
        store, lib = env()
        item = claim(store)
        play(qb, [{}, downloading(lib)])
        out = run(store, item, stop=stop_after(6, "pause"))
        state = qb.torrents[HASH]["state"]
        check("pause on qBittorrent %s returns Outcome.paused and stops the torrent" % label,
              out.kind == Outcome.PAUSED and state == ("stoppedDL" if modern else "pausedDL"))
        if not modern:
            routes = [r for (_m, r, _p) in qb.log if r in ("torrents/stop", "torrents/pause")]
            check("a 4.x qBittorrent is found out by the 404 and pause is used", routes[:2] == ["torrents/stop", "torrents/pause"])
        store.transition(item["id"], "paused", expect=states.ACTIVE_STATES)
        store.transition(item["id"], "queued", expect="paused")
        again = store.claim_next({"default": 100}, 100)
        play(qb, [{}, complete(lib)])
        out = run(store, again)
        started = qb.calls("torrents/start") + qb.calls("torrents/resume")
        check("resume on %s starts the same torrent again and finishes" % label,
              out.kind == Outcome.DONE and started and len(qb.calls("torrents/add")) == 1)


def test_external_pause():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, downloading(lib), {"state": "pausedDL"}])
    out = run(store, claim(store))
    check("pausing it inside qBittorrent pauses the row", out.kind == Outcome.PAUSED and not qb.deleted)


def test_shutdown_and_reattach():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    item = claim(store)
    play(qb, [{}, downloading(lib)])
    out = run(store, item, stop=stop_after(6, "shutdown"))
    check("a daemon shutdown requeues the row and leaves the torrent running",
          out.kind == Outcome.REQUEUE and HASH in qb.torrents and not qb.deleted
          and not qb.calls("torrents/stop") and not qb.calls("torrents/pause"))
    check("the info hash is saved for the next start", store.get_item(item["id"])["source_ref"]["infohash"] == HASH)
    again = requeue_like_the_daemon(store)
    play(qb, [{}, complete(lib)])
    out = run(store, again)
    check("after a kill the next run re-attaches: no duplicate, no restart from zero",
          again and again["id"] == item["id"] and out.kind == Outcome.DONE and len(qb.calls("torrents/add")) == 1
          and store.get_item(item["id"])["source_ref"]["added_by_hub"] is True)


# --------------------------------------------------------------------------- cancel

def test_cancel():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, downloading(lib)])
    out = run(store, claim(store), stop=stop_after(6, "cancel"))
    check("cancel removes a torrent the hub added, with its partial data",
          out.kind == Outcome.CANCELLED and qb.deleted == [(HASH, True)] and HASH not in qb.torrents)

    qb = new_qb()
    write_config(qb, cancel_deletes_data=False)
    store, lib = env()
    out = run(store, claim(store), stop=stop_after(0, "cancel"))
    check("cancel_deletes_data=false keeps the data", out.kind == Outcome.CANCELLED and qb.deleted == [(HASH, False)])

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    elsewhere = Path(HOME) / "elsewhere"
    play(qb, [lambda t: t.update(save_path=str(elsewhere))])
    out = run(store, claim(store), stop=stop_after(0, "cancel"))
    check("data outside the library root is never deleted", out.kind == Outcome.CANCELLED and qb.deleted == [(HASH, False)])

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    qb.torrent(HASH, **downloading(lib, 0.3))
    out = run(store, claim(store), stop=stop_after(0, "cancel"))
    check("a torrent that was already in qBittorrent is only paused, never removed",
          out.kind == Outcome.CANCELLED and not qb.deleted and HASH in qb.torrents
          and qb.torrents[HASH]["state"] == "stoppedDL")


# --------------------------------------------------------------------------- ways a run ends badly

def test_failures():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, {"state": "error"}])
    out = run(store, claim(store))
    check("an error state fails the row, not retryable, and stops the torrent",
          out.kind == Outcome.FAILED and not out.retryable and qb.calls("torrents/stop"))

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, downloading(lib), {"state": "missingFiles"}])
    out = run(store, claim(store))
    check("missing files fail the row", out.kind == Outcome.FAILED and "error" in out.error)

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, lambda t: qb.torrents.pop(HASH, None)])
    out = run(store, claim(store))
    check("a torrent removed inside qBittorrent ends the row as cancelled", out.kind == Outcome.CANCELLED)

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, lambda t: threading.Thread(target=qb.stop_server).start()])
    out = run(store, claim(store))
    check("qBittorrent going away mid-run is a retryable failure", out.kind == Outcome.FAILED and out.retryable
          and "qBittorrent" in out.error)

    qb = new_qb()
    write_config(qb, metadata_timeout_s=0.1)
    store, lib = env()
    out = run(store, claim(store))
    check("a magnet whose file list never arrives times out, and the torrent is stopped",
          out.kind == Outcome.FAILED and not out.retryable and "file list" in out.error and qb.calls("torrents/stop"))

    qb = new_qb()
    write_config(qb, stall_minutes=0.001)
    store, lib = env()
    play(qb, [{}, downloading(lib, 0.2, state="stalledDL", dlspeed=0, eta=8640000)])
    out = run(store, claim(store))
    check("no bytes for stall_minutes fails the row", out.kind == Outcome.FAILED and "no data" in out.error)

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, downloading(lib, 0.1, size=10 ** 18, total_size=10 ** 18, completed=10 ** 17)])
    out = run(store, claim(store))
    check("a torrent bigger than the free space is refused before it fills the disk",
          out.kind == Outcome.FAILED and "free space" in out.error and qb.calls("torrents/stop"))

    qb = new_qb()
    write_config(qb)
    qb.add_answer, qb.add_creates = "Fails.", False
    store, lib = env()
    out = run(store, claim(store))
    check("a magnet qBittorrent refuses fails cleanly", out.kind == Outcome.FAILED and not out.retryable
          and "didn't accept" in out.error)

    qb = new_qb()
    write_config(qb, password="s3cr3t-Pa55-wrong")
    store, lib = env()
    item = claim(store)
    out = run(store, item)
    trail = json.dumps([out.error, store.get_item(item["id"]), store.list_events(item["id"])], default=str)
    check("a wrong password fails the row without retrying, and the password appears nowhere",
          out.kind == Outcome.FAILED and not out.retryable and "rejected" in out.error and "s3cr3t" not in trail)

    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, lambda t: (setattr(qb, "expire_session", True), t.update(downloading(lib)))[0], complete(lib)])
    out = run(store, claim(store))
    check("an expired session is renewed on the spot", out.kind == Outcome.DONE and qb.logins >= 2)

    qb = new_qb()
    write_config(qb)
    qb.fail_info = 1
    store, lib = env()
    out = run(store, claim(store))
    check("an HTTP 500 from qBittorrent right at the start is a clean failure, not a crash",
          out.kind == Outcome.FAILED and "HTTP 500" in out.error)


def test_rate_limit():
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, downloading(lib), complete(lib)])
    out = run(store, claim(store), limit=50_000)
    limits = qb.calls("torrents/setDownloadLimit")
    check("the hub's share of the rate limit becomes the torrent's download limit",
          out.kind == Outcome.DONE and limits and limits[0]["limit"] == "50000")
    qb = new_qb()
    write_config(qb)
    store, lib = env()
    play(qb, [{}, complete(lib)])
    run(store, claim(store), limit=0)
    check("with no limit nothing is changed in qBittorrent", not qb.calls("torrents/setDownloadLimit"))


# --------------------------------------------------------------------------- routing / doctor

def test_routing():
    qb = new_qb()
    write_config(qb)
    explicit = [r for r in engines.route_rules() if r.engine == "qbittorrent"]
    others = [r.priority for r in engines.route_rules() if r.engine != "qbittorrent"]
    check("its always-matching rule has the lowest priority there is", min(r.priority for r in explicit) < min(others))

    decision = router.route(MAGNET)
    check("by default a magnet is NOT claimed by this engine", decision["engine"] != "qbittorrent")

    decision = router.route(MAGNET, engine="qbittorrent")
    check("engine=\"qbittorrent\" always works for a magnet",
          decision["ok"] and decision["engine"] == "qbittorrent" and decision["kind"] == "torrent"
          and decision["source"].startswith("magnet:?xt=urn:btih:" + HASH))
    check("a bare info hash works too", router.route(HASH, engine="qbittorrent")["ok"])
    check("a web link is not a torrent", not router.route("https://example.com/a", engine="qbittorrent")["ok"])

    check("opt-in off: the preferred rule stays quiet", qbe._match_preferred(MAGNET, {}) is None)
    write_config(qb, prefer_for_magnets=True)
    hit = qbe._match_preferred(MAGNET, {})
    check("opt-in on and qBittorrent up: it claims magnets", hit and hit["source"].startswith("magnet:?"))
    check("it never claims a web link", qbe._match_preferred("https://example.com/a", {}) is None)
    write_config(qb)
    check("the same opt-in can come from the hub settings",
          qbe._match_preferred(MAGNET, {"qbittorrent": {"prefer_for_magnets": True}}) is not None)
    write_config(qb, prefer_for_magnets=True)
    qb.stop_server()
    qbe.ENGINE._avail = None
    check("opt-in on but qBittorrent down: the magnet falls through to the next engine",
          qbe._match_preferred(MAGNET, {}) is None)
    decision = router.route(MAGNET, engine="qbittorrent")
    check("and asking for the engine by name says it isn't ready", not decision["ok"] and "isn't ready" in decision["problem"])


def test_doctor():
    qb = new_qb()
    write_config(qb)
    qb.torrent(HASH, tags="jarvis")
    rows = qbe.ENGINE.doctor()
    check("hub_doctor shows the version, the address and how many torrents carry the tag",
          rows and rows[0]["status"] == "ok" and "v5.0.1" in rows[0]["detail"] and qb.base in rows[0]["detail"]
          and "1 torrent" in rows[0]["detail"] and qb.password not in rows[0]["detail"])
    qb.stop_server()
    rows = qbe.ENGINE.doctor()
    check("with qBittorrent down it warns", rows and rows[0]["status"] == "warn")
    if config_file().exists():
        config_file().unlink()
    check("with no config it adds nothing (available() already says it)", qbe.ENGINE.doctor() == [])
    check("the engine module loads through discovery", "qbittorrent" in engines.names()
          and "qbittorrent" not in engines.LOAD_ERRORS)


try:
    test_config_and_available()
    test_bypass_login()
    test_magnets()
    test_basic_download()
    test_attach_existing()
    test_after_done()
    test_pause_and_resume()
    test_external_pause()
    test_shutdown_and_reattach()
    test_cancel()
    test_failures()
    test_rate_limit()
    test_routing()
    test_doctor()
finally:
    for server in SERVERS:
        try:
            server.stop_server()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
