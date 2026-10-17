"""Download Hub, phase 1: the HTTP engine against a real local server.

Covers: a plain download (name, folder, size check, .part renamed away, nothing left behind);
RESUME with Range + If-Range after a pause; a server that ignores Range (restarts from 0,
never appends); redirects (followed; the name comes from the final URL; a hop to a private
address is refused; credentials are dropped when the host changes); the SSRF guard on the
first request; a hostile Content-Disposition staying inside the library root; HTTP errors
(404 permanent, 503 retryable with Retry-After); a truncated body and a stall (both
retryable); cancel deleting only the .part; a body with no length; the free-space check
refusing instead of truncating; the `subdir` option (and a bad one); which request headers an
item may carry; and the rate limit.

Run: python tests/test_hub_http_engine.py
"""
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-http-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.hub import states  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
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


SERVER, BASE, STATE = srv.start()


def env(allow=("127.0.0.1",), stall=5):
    """A fresh store and library root, pointed at the local server."""
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    store.set_settings({"library_root": str(lib), "allow_private_hosts": list(allow), "http.stall_seconds": stall})
    STATE.reset()
    return store, lib


def claim(store, url, **options):
    # Nothing here plays the daemon, so a finished row stays "downloading" in the store; the caps
    # are set high so the Nth claim in one store is never refused by them.
    item = store.add_item(engine="http", source_url=url, title="", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None, limit=0):
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None),
                        rate_limit_bps=lambda: limit)
    return http_engine.ENGINE.run(item, ctx)


def requeue_like_the_daemon(store, item_id):
    store.transition(item_id, "paused", expect=states.ACTIVE_STATES)
    store.transition(item_id, "queued", expect="paused")
    return store.claim_next({"default": 100}, 100)


def stop_after_first_chunk(kind):
    """A stop callable that asks for `kind` from the first check on (the engine checks after each chunk)."""
    return lambda: kind


def test_basic():
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=300000")
    outcome = run(store, item)
    row = store.get_item(item["id"])
    final = Path(row["dest_file"])
    check("a plain download finishes", outcome.kind == Outcome.DONE and outcome.dest_file == str(final))
    check("the file lands in <library>/Files/<name>", final == lib / "Files" / "file.bin" and final.exists())
    check("its bytes are exactly the server's", final.read_bytes() == srv.payload(300000))
    check("no .part is left behind", not Path(str(final) + ".part").exists())
    check("the row learned its size, folder and name", row["bytes_total"] == 300000 and row["bytes_done"] == 300000
          and row["title"] == "file.bin" and row["dest_dir"] == str(lib / "Files"))
    check("the server was asked for the whole file (no Range)", STATE.log[0][1] is None)
    check("the engine asks for identity encoding so lengths are honest", STATE.headers[0].get("Accept-Encoding") == "identity")

    item2 = claim(store, BASE + "/file.bin?size=1000")
    outcome2 = run(store, item2)
    check("a second download of the same name never overwrites", Path(outcome2.dest_file).name == "file (1).bin"
          and Path(outcome2.dest_file).read_bytes() == srv.payload(1000) and final.read_bytes() == srv.payload(300000))

    video = claim(store, BASE + "/file.bin?size=1000")
    # a video-looking name goes under Video/ (kind from the extension)
    store.update_item(video["id"], {"options": {"filename": "clip.mkv"}})
    video = store.get_item(video["id"])
    out = run(store, video)
    check("an .mkv goes under Video/", Path(out.dest_file) == lib / "Video" / "clip.mkv")


def test_resume():
    store, lib = env()
    size = 1_048_576
    item = claim(store, BASE + "/file.bin?size=%d" % size)
    first = run(store, item, stop=stop_after_first_chunk("pause"))
    ref = store.get_item(item["id"])["source_ref"]
    part = Path(ref["part"])
    got = part.stat().st_size
    check("a pause stops cleanly and keeps the partial file", first.kind == Outcome.PAUSED and part.exists() and 0 < got < size)
    check("the row recorded how far it got", store.get_item(item["id"])["bytes_done"] == got)
    check("the resume handle (part, final, etag) is stored on the row", ref["part"].endswith(".part") and ref.get("etag") == '"v1"' and ref["accept_ranges"])

    STATE.reset()
    item = requeue_like_the_daemon(store, item["id"])
    second = run(store, item)
    final = Path(second.dest_file)
    check("the second run finishes the file", second.kind == Outcome.DONE and final.read_bytes() == srv.payload(size))
    check("...by asking for bytes=<offset>- with If-Range", STATE.log[0][1] == "bytes=%d-" % got and STATE.log[0][2] == '"v1"')
    check("the .part is gone", not part.exists())

    # a hub row whose recorded .part sits OUTSIDE the library root is never trusted
    outside = Path(HOME) / "outside.part"
    outside.write_bytes(b"x" * 10)
    sneaky = claim(store, BASE + "/file.bin?size=2000")
    store.update_item(sneaky["id"], {"source_ref": {"part": str(outside), "final": str(Path(HOME) / "outside.bin")}})
    sneaky = store.get_item(sneaky["id"])
    STATE.reset()
    out = run(store, sneaky)
    check("a recorded path outside the library root is ignored (starts fresh inside it)",
          out.kind == Outcome.DONE and Path(out.dest_file).parent == lib / "Files" and STATE.log[0][1] is None
          and not (Path(HOME) / "outside.bin").exists())


def test_no_range_support():
    store, lib = env()
    size = 600_000
    item = claim(store, BASE + "/norange.bin?size=%d" % size)
    run(store, item, stop=stop_after_first_chunk("pause"))
    STATE.reset()
    item = requeue_like_the_daemon(store, item["id"])
    out = run(store, item)
    check("a server that ignores Range gets the file restarted from 0", out.kind == Outcome.DONE
          and Path(out.dest_file).read_bytes() == srv.payload(size))
    check("(and the engine did ask for a range)", STATE.log[0][1] is not None)
    check("the restart is logged", any("ignored Range" in e["message"] for e in store.list_events(item["id"])))


def test_redirects_and_guard():
    store, lib = env()
    item = claim(store, BASE + "/redirect?size=1000")
    out = run(store, item)
    check("a redirect is followed and the name comes from the final URL", out.kind == Outcome.DONE and Path(out.dest_file).name == "file.bin")

    item = claim(store, BASE + "/redir-private")
    out = run(store, item)
    check("a redirect to a private address is refused, not retried",
          out.kind == Outcome.FAILED and not out.retryable and "private" in out.error)

    store, lib = env(allow=("127.0.0.1", "localhost"))
    item = claim(store, BASE + "/redirect-host?size=1000", headers={"Authorization": "Bearer SECRET", "Referer": "https://x.example/"})
    out = run(store, item)
    check("a cross-host redirect still completes (the second host is allow-listed)", out.kind == Outcome.DONE)
    first, second = STATE.headers[0], STATE.headers[-1]
    check("credentials go to the first host", first.get("Authorization") == "Bearer SECRET")
    check("...and are dropped when the redirect changes host", "Authorization" not in second and second.get("Referer") == "https://x.example/")

    store, lib = env(allow=())
    item = claim(store, BASE + "/file.bin?size=1000")
    out = run(store, item)
    check("the first request is guarded too: loopback not on the allow-list is refused",
          out.kind == Outcome.FAILED and not out.retryable and "private or local" in out.error)
    check("and nothing was requested or written", STATE.log == [] and not lib.exists())

    store, lib = env()
    item = claim(store, "file:///etc/passwd")
    out = run(store, item)
    check("a file: link is refused by the engine as well", out.kind == Outcome.FAILED and not out.retryable)


def test_hostile_names_and_dirs():
    store, lib = env()
    item = claim(store, BASE + "/cd?size=100")
    out = run(store, item)
    final = Path(out.dest_file)
    check("a hostile Content-Disposition is reduced to a plain name inside the library",
          out.kind == Outcome.DONE and final.name == "evil.exe" and final.parent == lib / "Files")
    check("nothing was written outside the library root", not (Path(HOME) / "evil.exe").exists() and not (lib.parent / "evil.exe").exists())

    item = claim(store, BASE + "/file.bin?size=500", subdir="Manuals")
    out = run(store, item)
    check("the subdir option puts the file in <library>/Manuals", Path(out.dest_file).parent == lib / "Manuals")

    for bad in ("../escape", "/etc", "C:\\Windows", "a/b/c/d/e/f"):
        item = claim(store, BASE + "/file.bin?size=500", subdir=bad)
        out = run(store, item)
        check("a bad subdir (%s) is refused and nothing is written" % bad, out.kind == Outcome.FAILED and not out.retryable)
    check("...and nothing escaped", not (lib.parent / "escape").exists())


def test_errors_and_retries():
    store, lib = env()
    item = claim(store, BASE + "/missing")
    out = run(store, item)
    check("a 404 is permanent", out.kind == Outcome.FAILED and not out.retryable and "404" in out.error)

    item = claim(store, BASE + "/flaky?size=1000")
    out = run(store, item)
    check("a 503 is retryable and honours Retry-After", out.kind == Outcome.FAILED and out.retryable and out.delay_s == 2.0)
    item = requeue_like_the_daemon(store, item["id"])
    out = run(store, item)
    check("and the second try succeeds", out.kind == Outcome.DONE)

    item = claim(store, BASE + "/short")
    out = run(store, item)
    check("a body that ends early is retryable (never accepted as the file)",
          out.kind in (Outcome.STALLED, Outcome.FAILED) and out.retryable)
    check("and is not renamed to its final name", not list((lib / "Files").glob("*")) or all(p.name.endswith(".part") for p in (lib / "Files").glob("*")))

    store, lib = env(stall=1)
    item = claim(store, BASE + "/hang?size=200000")
    began = time.time()
    out = run(store, item)
    check("no data for stall_seconds is a stall, not a hang", out.kind == Outcome.STALLED and time.time() - began < 3.5)

    store2, _ = env()
    item = claim(store2, "http://127.0.0.1:1/never")
    out = run(store2, item)
    check("a refused connection is retryable", out.kind == Outcome.FAILED and out.retryable)


def test_cancel_and_nolength():
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1048576")
    out = run(store, item, stop=stop_after_first_chunk("cancel"))
    part = Path(store.get_item(item["id"])["source_ref"]["part"])
    check("cancel returns Cancelled and removes the .part", out.kind == Outcome.CANCELLED and not part.exists())
    check("and leaves no other file", not [p for p in (lib / "Files").glob("*")])

    item = claim(store, BASE + "/file.bin?size=1048576")
    out = run(store, item, stop=stop_after_first_chunk("shutdown"))
    check("a shutdown asks to be requeued, keeping the .part", out.kind == Outcome.REQUEUE
          and Path(store.get_item(item["id"])["source_ref"]["part"]).exists())

    item = claim(store, BASE + "/nolength?size=70000")
    out = run(store, item)
    row = store.get_item(item["id"])
    check("a body with no length finishes at end of stream", out.kind == Outcome.DONE and Path(out.dest_file).read_bytes() == srv.payload(70000))
    check("and its size is known afterwards", row["bytes_total"] == 70000)


def test_disk_space():
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=100000")
    real = shutil.disk_usage
    shutil.disk_usage = lambda p: types.SimpleNamespace(total=10, used=10, free=10)
    try:
        out = run(store, item)
    finally:
        shutil.disk_usage = real
    check("not enough free space refuses instead of truncating", out.kind == Outcome.FAILED and not out.retryable and "free space" in out.error)
    check("and no partial file was started", not list((lib / "Files").glob("*.part")))


def test_headers_and_rate_limit():
    check("only allow-listed headers pass", http_engine.clean_headers(
        {"Referer": "r", "authorization": "a", "Cookie": "c", "X-Other": "x", "User-Agent": "u\r\nInjected: 1"}) == {"Referer": "r", "Authorization": "a"})
    check("non-dict headers are ignored", http_engine.clean_headers("nope") == {})

    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1000", headers={"Referer": "https://ref.example/", "Cookie": "evil=1", "X-Other": "x"})
    run(store, item)
    sent = STATE.headers[0]
    check("an item's Referer is sent", sent.get("Referer") == "https://ref.example/")
    check("a Cookie or unknown header from the item is not", "Cookie" not in sent and "X-Other" not in sent)

    item = claim(store, BASE + "/file.bin?size=250000")
    began = time.time()
    out = run(store, item, limit=200_000)
    took = time.time() - began
    check("a rate limit slows the transfer down", out.kind == Outcome.DONE and took >= 0.9)


try:
    test_basic()
    test_resume()
    test_no_range_support()
    test_redirects_and_guard()
    test_hostile_names_and_dirs()
    test_errors_and_retries()
    test_cancel_and_nolength()
    test_disk_space()
    test_headers_and_rate_limit()
finally:
    srv.stop(SERVER)
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
