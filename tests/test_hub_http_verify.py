"""Download Hub, brief P1: the `verify` item option (a checksum the finished file must match) and the
protected-folder refusal for `dest_abs`.

Covers: the checker (`service._check_verify`: shape, algorithm, length, characters, size; what is stored is the
clean form); the option reaching a row through `add_items` and through the shared `_queue` of the add tools, and
NOT being stored on a row another engine takes (the answer's `note` says so); the HTTP engine hashing the finished
`.part` (sha1, sha256, sha512): a match publishes the file, a mismatch fails the row for good
(`retryable=False`), deletes only that row's own `.part` and leaves other files alone; a resumed `.part` is
checked as a whole (a damaged earlier part is caught); the size cap (declared length over `size` + the slack is
refused, with no length the transfer is stopped); a pause or a cancel while the file is being hashed; a stored
option that is not usable failing before any request; the daemon NOT retrying a mismatch; rows without the option
behaving exactly as before (the hashing code is never reached); and `dest_abs` refusing the same places as
`path_tools._protected_reason(path, writing=True)` (drive roots, the home folder itself, OS folders, ~/.jarvis).

Run: python tests/test_hub_http_verify.py   (Linux, a local fake server, no network)
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-verify-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_STREMIO_SERVER_URL"] = "http://127.0.0.1:9"
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.actions import download_hub as dh  # noqa: E402
from jarvis.actions import path_tools  # noqa: E402
from jarvis.hub import engines, service, states  # noqa: E402
from jarvis.hub import settings as settings_mod  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines import stremio, ytdlp  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0
_n = [0]
PUBLIC = "http://93.184.216.34"
YT = "https://youtu.be/abcdefghijk"
SERVER, BASE, STATE = srv.start()
ytdlp.ENGINE.available = lambda: (True, "")
stremio.ENGINE.available = lambda: (True, "")


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def digest(algo, n):
    """The hex hash of the fake server's n-byte file."""
    return hashlib.new(algo, srv.payload(n)).hexdigest()


def vopt(algo, n, **extra):
    out = {"algo": algo, "value": digest(algo, n)}
    out.update(extra)
    return out


def env():
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hubv-{_n[0]}.db")
    store.set_settings({"library_root": str(lib), "allow_private_hosts": ["127.0.0.1", "localhost"], "http.stall_seconds": 5})
    STATE.reset()
    return store, lib


def claim(store, url, **options):
    item = store.add_item(engine="http", source_url=url, title="", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None):
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None), rate_limit_bps=lambda: 0)
    return http_engine.ENGINE.run(item, ctx)


def requeue_like_the_daemon(store, item_id):
    store.transition(item_id, "paused", expect=states.ACTIVE_STATES)
    store.transition(item_id, "queued", expect="paused")
    return store.claim_next({"default": 100}, 100)


def files_under(lib):
    return sorted(str(p.relative_to(lib)) for p in Path(lib).rglob("*") if p.is_file())


def default_rows():
    return service._store(None).list_items(limit=2000, include_disabled=True, include_dismissed=True)


def stored_options(item_id):
    return (service._store(None).get_item(item_id) or {}).get("options") or {}


# ---------------------------------------------------------------- the checker
def test_checker():
    fn = service._check_verify
    for algo, n in (("sha1", 40), ("sha256", 64), ("sha512", 128)):
        out, problem = fn({"algo": algo, "value": "ab" * (n // 2)})
        check("%s with %d hex characters is accepted" % (algo, n), problem is None and out == {"algo": algo, "value": "ab" * (n // 2)})
    out, problem = fn({"algo": " SHA-256 ", "value": " " + "AB" * 32 + " ", "size": "1234"})
    check("the algorithm name and the hash are cleaned (case, a dash, spaces) and a text size becomes a number",
          problem is None and out == {"algo": "sha256", "value": "ab" * 32, "size": 1234})
    out, problem = fn({"algo": "sha1", "value": "0" * 40, "size": 0})
    check("a size of 0 is a size (an empty file), not 'no size'", problem is None and out.get("size") == 0)
    out, problem = fn({"algo": "sha1", "value": "0" * 40, "size": 2048.0})
    check("a whole float is a whole number", problem is None and out["size"] == 2048)
    out, problem = fn({"algo": "sha1", "value": "0" * 40, "size": None})
    check("size None means no size", problem is None and "size" not in out)

    h40, h64, h128 = "a" * 40, "b" * 64, "c" * 128
    refusals = [
        ("not an object (text)", "abc", "object"),
        ("not an object (a list)", [h40], "object"),
        ("a bool", True, "object"),
        ("an unknown key", {"algo": "sha1", "value": h40, "extra": 1}, "extra"),
        ("no algorithm", {"value": h40}, "algo"),
        ("an algorithm that is a number", {"algo": 1, "value": h40}, "algo"),
        ("md5 (CurseForge-style, not supported)", {"algo": "md5", "value": "a" * 32}, "sha1, sha256 or sha512"),
        ("sha384", {"algo": "sha384", "value": "a" * 96}, "sha1, sha256 or sha512"),
        ("no value", {"algo": "sha1"}, "value"),
        ("a value that is a number", {"algo": "sha1", "value": 12345}, "value"),
        ("a sha1 hash that is too short", {"algo": "sha1", "value": h40[:-1]}, "40"),
        ("a sha1 hash that is too long", {"algo": "sha1", "value": h40 + "a"}, "40"),
        ("a sha256 hash given for sha1", {"algo": "sha1", "value": h64}, "40"),
        ("a sha1 hash given for sha256", {"algo": "sha256", "value": h40}, "64"),
        ("a sha256 hash given for sha512", {"algo": "sha512", "value": h64}, "128"),
        ("a sha512 hash of 129 characters", {"algo": "sha512", "value": h128 + "c"}, "128"),
        ("a letter outside a-f", {"algo": "sha1", "value": "g" * 40}, "0-9 and a-f"),
        ("a space inside", {"algo": "sha1", "value": "a" * 20 + " " + "a" * 19}, "0-9 and a-f"),
        ("a newline inside", {"algo": "sha1", "value": "a" * 20 + "\n" + "a" * 19}, "0-9 and a-f"),
        ("0x in front", {"algo": "sha1", "value": "0x" + "a" * 38}, "0-9 and a-f"),
        ("a negative size", {"algo": "sha1", "value": h40, "size": -1}, "size"),
        ("a size that is a bool", {"algo": "sha1", "value": h40, "size": True}, "size"),
        ("a size that is 2.5", {"algo": "sha1", "value": h40, "size": 2.5}, "size"),
        ("a size that is text", {"algo": "sha1", "value": h40, "size": "big"}, "size"),
        ("a size that is a list", {"algo": "sha1", "value": h40, "size": [1]}, "size"),
        ("a size of 2**60", {"algo": "sha1", "value": h40, "size": 1 << 60}, "size"),
    ]
    for label, bad, word in refusals:
        out, problem = fn(bad)
        check("verify refuses %s, in words (%r)" % (label, word), out is None and isinstance(problem, str) and word in problem)
    check("the engine and the hub agree on the algorithms and the hash lengths",
          http_engine._VERIFY_ALGOS == service.VERIFY_ALGOS == {"sha1": 40, "sha256": 64, "sha512": 128})

    clean, problem = service.normalize_engine_item_options({"verify": {"algo": "SHA1", "value": h40.upper()}, "segments": 2})
    check("normalize_engine_item_options takes it next to the other options",
          problem is None and clean == {"verify": {"algo": "sha1", "value": h40}, "segments": 2})
    clean, problem = service.normalize_engine_item_options({"verify": None})
    check("verify None leaves it unset", problem is None and clean == {})
    clean, problem = service.normalize_engine_item_options({"verify": {"algo": "md5", "value": "a" * 32}})
    check("a bad verify is the whole call's error", clean == {} and "sha1" in (problem or ""))
    clean, problem = service.normalize_engine_item_options({"hash": "abc"})
    check("the list of known options names verify", clean == {} and "verify" in (problem or ""))
    check("verify is for the http engine only", service.ITEM_OPTION_CHECKS["verify"][0] == ("http",))


# ---------------------------------------------------------------- reaching a row
def test_add_path():
    v = {"algo": "sha256", "value": digest("sha256", 1000), "size": 1000}
    res = service.add_items([PUBLIC + "/a.iso"], item_options={"verify": v}, start_paused=True)
    check("add_items stores the clean option on an http row", res["ok"] and stored_options(res["added"][0]["id"]).get("verify") == v)

    res = dh._queue({"start_paused": True}, [PUBLIC + "/q.iso"], "http", None, "dl_add_http", item_options={"verify": dict(v, algo="SHA-256")})
    check("the shared _queue of the add tools hands verify on untouched (no change to actions/download_hub.py is needed)",
          res["ok"] and stored_options(res["added"][0]["id"]).get("verify") == v)

    before = len(default_rows())
    res = service.add_items([PUBLIC + "/bad.iso"], item_options={"verify": {"algo": "sha256", "value": "zz"}}, start_paused=True)
    check("a bad verify is refused and nothing is queued", res.get("ok") is False and "64" in res.get("error", "") and len(default_rows()) == before)
    res = dh._queue({"start_paused": True}, [PUBLIC + "/bad2.iso"], "http", None, "dl_add_http", item_options={"verify": {"algo": "md5", "value": "a" * 32}})
    check("...the same through _queue", res.get("ok") is False and len(default_rows()) == before)

    res = service.add_items([PUBLIC + "/mix.iso", YT], item_options={"verify": v}, start_paused=True)
    by_engine = {a["engine"]: a for a in res["added"]}
    check("on a mixed batch the http row carries verify", stored_options(by_engine["http"]["id"]).get("verify") == v)
    check("...a row another engine takes does NOT (it would never be checked)", "verify" not in stored_options(by_engine["ytdlp"]["id"]))
    check("...and the answer says so in words", "verify only applies to links the http engine takes" in res.get("note", ""))
    res = service.add_items(["https://youtu.be/bbbbbbbbbbb"], item_options={"verify": v}, start_paused=True)
    check("a video link alone: queued without verify, with the note", res["ok"] and "verify" not in stored_options(res["added"][0]["id"])
          and "verify only applies" in res.get("note", ""))

    res = service.add_items([PUBLIC + "/plain.iso"], start_paused=True)
    check("a row queued without the option never gets one", res["ok"] and "verify" not in stored_options(res["added"][0]["id"]))
    res = service.add_items([PUBLIC + "/a.iso"], item_options={"verify": dict(v, size=1000)}, start_paused=True)
    check("a link that is already queued keeps the options it has (existing rule), and says so",
          res["added"][0]["duplicate"] and "verify was not changed" in res.get("note", ""))


# ---------------------------------------------------------------- the engine: match and mismatch
def test_match():
    for algo in ("sha1", "sha256", "sha512"):
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=300000", verify=vopt(algo, 300000))
        out = run(store, item)
        row = store.get_item(item["id"])
        check("%s: a matching file is published" % algo, out.kind == Outcome.DONE and Path(out.dest_file).read_bytes() == srv.payload(300000))
        check("%s: ...and no .part is left" % algo, files_under(lib) == ["Files/file.bin"])
        check("%s: ...it showed the checking step (postprocessing, with its words)" % algo,
              row["state"] == "postprocessing" and row["phase"] == "checking the checksum")
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1000", verify=vopt("sha256", 1000, size=1000))
    out = run(store, item)
    check("a verify with the right size and hash passes", out.kind == Outcome.DONE)


def test_mismatch():
    store, lib = env()
    (lib / "Files").mkdir(parents=True)
    (lib / "Files" / "file.bin").write_bytes(b"somebody else's finished file")
    (lib / "Files" / "other.bin.part").write_bytes(b"another download's partial")
    item = claim(store, BASE + "/file.bin?size=300000", verify=vopt("sha256", 299999))
    out = run(store, item)
    row = store.get_item(item["id"])
    check("a wrong hash fails the row, and for good (retryable=False)", out.kind == Outcome.FAILED and out.retryable is False)
    check("...saying it is a checksum mismatch, with the algorithm and the start of both hashes",
          "checksum mismatch" in out.error and "sha256" in out.error and digest("sha256", 299999)[:12] in out.error
          and digest("sha256", 300000)[:12] in out.error)
    check("...the file this row downloaded is gone (no .part, no half-published name)", not (lib / "Files" / "file (1).bin").exists()
          and not (lib / "Files" / "file (1).bin.part").exists())
    check("...an unrelated finished file and another download's .part are untouched",
          (lib / "Files" / "file.bin").read_bytes() == b"somebody else's finished file"
          and (lib / "Files" / "other.bin.part").read_bytes() == b"another download's partial")
    check("...and the row forgot its .part, so a retry starts from nothing", row["source_ref"] == {} and row["bytes_done"] == 0)
    check("...the library holds only what was there before", files_under(lib) == ["Files/file.bin", "Files/other.bin.part"])

    for algo in ("sha1", "sha512"):
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=5000", verify={"algo": algo, "value": "0" * http_engine._VERIFY_ALGOS[algo]})
        out = run(store, item)
        check("%s: a wrong hash fails and leaves nothing behind" % algo, out.kind == Outcome.FAILED and not out.retryable and files_under(lib) == [])

    # a retry (the owner presses Retry on the failed row) downloads again from byte 0, and can pass this time
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=5000", verify=vopt("sha1", 4999))
    run(store, item)
    store.update_item(item["id"], {"options": dict(item["options"], verify=vopt("sha1", 5000))})
    store.transition(item["id"], "failed", expect=states.ACTIVE_STATES, error="x")
    store.transition(item["id"], "queued", expect="failed")
    again = store.claim_next({"default": 100}, 100)
    STATE.reset()
    out = run(store, again)
    check("a retried row starts at byte 0 (no Range) and finishes once the hash is right",
          out.kind == Outcome.DONE and STATE.log and STATE.log[0][1] is None)


def test_resume():
    size = 1_048_576
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % size, verify=vopt("sha256", size))
    first = run(store, item, stop=lambda: "pause")
    part = Path(store.get_item(item["id"])["source_ref"]["part"])
    got = part.stat().st_size
    check("a pause keeps the partial file and does not check it", first.kind == Outcome.PAUSED and 0 < got < size)
    STATE.reset()
    item = requeue_like_the_daemon(store, item["id"])
    out = run(store, item)
    check("the resumed run asks for bytes=<offset>- and the WHOLE file is checked", STATE.log[0][1] == "bytes=%d-" % got
          and out.kind == Outcome.DONE and Path(out.dest_file).read_bytes() == srv.payload(size) and not part.exists())

    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % size, verify=vopt("sha256", size))
    run(store, item, stop=lambda: "pause")
    part = Path(store.get_item(item["id"])["source_ref"]["part"])
    data = bytearray(part.read_bytes())
    data[0] ^= 0xFF                                  # damage the part that was written BEFORE the pause
    part.write_bytes(bytes(data))
    STATE.reset()
    item = requeue_like_the_daemon(store, item["id"])
    out = run(store, item)
    check("a damaged earlier part is caught by the final check (the resumed bytes alone would look fine)",
          out.kind == Outcome.FAILED and not out.retryable and "checksum mismatch" in out.error)
    check("...and the damaged .part is removed", not part.exists() and files_under(lib) == []
          and store.get_item(item["id"])["source_ref"] == {})


# ---------------------------------------------------------------- the size cap
def test_size_cap():
    slack = http_engine.VERIFY_SIZE_SLACK
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=300000", verify=vopt("sha256", 300000, size=1000))
    out = run(store, item)
    check("a response that announces far more than `size` is refused for good", out.kind == Outcome.FAILED and not out.retryable
          and "announced" in out.error and "expected" in out.error)
    check("...before any name or .part was taken", files_under(lib) == [] and not (lib / "Files").exists()
          and store.get_item(item["id"])["source_ref"] in ({}, None))

    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % (1000 + slack), verify=vopt("sha256", 1000 + slack, size=1000))
    out = run(store, item)
    check("exactly size + slack is inside the allowance", out.kind == Outcome.DONE)
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % (1000 + slack + 1), verify=vopt("sha256", 1000 + slack + 1, size=1000))
    out = run(store, item)
    check("one byte more is refused", out.kind == Outcome.FAILED and "announced" in out.error and files_under(lib) == [])
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1000", verify=vopt("sha256", 1000, size=10_000_000))
    check("a file smaller than `size` is not a size problem (the hash decides)", run(store, item).kind == Outcome.DONE)

    store, lib = env()
    item = claim(store, BASE + "/nolength?size=300000", verify=vopt("sha256", 300000, size=1000))
    out = run(store, item)
    check("with no announced length the transfer is stopped once it passes size + slack, and the file removed",
          out.kind == Outcome.FAILED and not out.retryable and "more data" in out.error and files_under(lib) == [])
    store, lib = env()
    item = claim(store, BASE + "/nolength?size=3000", verify=vopt("sha256", 3000, size=3000))
    check("...a body with no length that is the right size passes", run(store, item).kind == Outcome.DONE)

    # a resume whose server now announces too much: the row's OWN earlier .part goes, nothing else
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1048576")
    run(store, item, stop=lambda: "pause")
    part = Path(store.get_item(item["id"])["source_ref"]["part"])
    (lib / "Files" / "keep.txt").write_text("mine")
    options = dict(item["options"], verify=vopt("sha256", 1048576, size=1000))
    store.update_item(item["id"], {"options": options})
    item = requeue_like_the_daemon(store, item["id"])
    out = run(store, item)
    check("a resumed row that now exceeds `size` is refused and only its own .part is removed",
          out.kind == Outcome.FAILED and "announced" in out.error and not part.exists()
          and (lib / "Files" / "keep.txt").read_text() == "mine")


# ---------------------------------------------------------------- pause / cancel while hashing
def test_stop_while_hashing():
    flag = {"on": False}
    original = http_engine._verify_part

    def wrapped(ctx, part, verify):
        flag["on"] = True
        try:
            return original(ctx, part, verify)
        finally:
            flag["on"] = False

    http_engine._verify_part = wrapped
    try:
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=700000", verify=vopt("sha256", 700000))
        out = run(store, item, stop=lambda: "cancel" if flag["on"] else None)
        check("a cancel while the file is being checked cancels the row and removes the .part",
              out.kind == Outcome.CANCELLED and files_under(lib) == [])

        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=700000", verify=vopt("sha256", 700000))
        out = run(store, item, stop=lambda: "pause" if flag["on"] else None)
        part = Path(store.get_item(item["id"])["source_ref"]["part"])
        check("a pause while it is checked keeps the finished .part", out.kind == Outcome.PAUSED and part.exists()
              and part.stat().st_size == 700000)
        STATE.reset()
        item = requeue_like_the_daemon(store, item["id"])
        out = run(store, item)
        check("...and the resume (the server says 416: nothing left to fetch) checks it again and publishes",
              out.kind == Outcome.DONE and STATE.log[0][1] == "bytes=700000-" and Path(out.dest_file).read_bytes() == srv.payload(700000))

        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=700000", verify=vopt("sha256", 700000))
        out = run(store, item, stop=lambda: "shutdown" if flag["on"] else None)
        check("a shutdown while it is checked puts the row back (requeue) and keeps the .part",
              out.kind == Outcome.REQUEUE and len(files_under(lib)) == 1 and files_under(lib)[0].endswith(".part"))
    finally:
        http_engine._verify_part = original


# ---------------------------------------------------------------- rows without the option, and bad stored options
def test_no_option_unchanged():
    original = (http_engine._hash_file, http_engine._verify_part)

    def boom(*args, **kwargs):
        raise AssertionError("the checking code ran for a row with no verify option")

    http_engine._hash_file = boom
    http_engine._verify_part = boom
    try:
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=300000")
        out = run(store, item)
        row = store.get_item(item["id"])
        check("a row without verify downloads exactly as before and never reaches the hashing code",
              out.kind == Outcome.DONE and Path(out.dest_file).read_bytes() == srv.payload(300000))
        check("...it never shows the checking step (the row stays in downloading, no phase)", row["state"] == "downloading" and not row["phase"])
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=300000", segments=1, referer="https://example.com/p")
        check("...also with other item options", run(store, item).kind == Outcome.DONE)
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=1000", verify=None)
        check("...and with verify stored as None (no option)", run(store, item).kind == Outcome.DONE)
        store, lib = env()
        item = claim(store, BASE + "/nolength?size=300000")
        check("...a body with no length and no option is not capped", run(store, item).kind == Outcome.DONE)
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=1000")
        ctx = EngineContext(store, item, store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
        out = http_engine.stream_to_file(ctx, item, BASE + "/file.bin?size=1000")
        check("...and stream_to_file called directly (the Stremio engine's way) is the same", out.kind == Outcome.DONE)
        check("...a context with no .item at all has no options (nothing to break)", http_engine._row_options(object()) is None)
    finally:
        http_engine._hash_file, http_engine._verify_part = original

    for label, bad in (("an unknown algorithm", {"algo": "md5", "value": "a" * 32}), ("a hash of the wrong length", {"algo": "sha256", "value": "a" * 40}),
                       ("an upper-case hash (the hub stores lower case)", {"algo": "sha1", "value": "A" * 40}), ("text", "deadbeef"),
                       ("a list", ["sha1", "a" * 40]), ("a negative size", {"algo": "sha1", "value": "a" * 40, "size": -5}),
                       ("a size that is a bool", {"algo": "sha1", "value": "a" * 40, "size": True}), ("an empty object", {})):
        store, lib = env()
        item = claim(store, BASE + "/file.bin?size=1000", verify=bad)
        out = run(store, item)
        check("a stored verify that is %s fails the row before any request (never downloaded unchecked)" % label,
              out.kind == Outcome.FAILED and not out.retryable and "checksum" in out.error and STATE.log == [] and files_under(lib) == [])


def test_finish_directly():
    """What the segmented path (and a 416 'already complete') calls: `_finish` on a whole `.part`."""
    store, lib = env()
    (lib / "Files").mkdir(parents=True)
    part, final = lib / "Files" / "big.bin.part", lib / "Files" / "big.bin"
    part.write_bytes(srv.payload(500000))
    item = claim(store, BASE + "/file.bin?size=500000", verify=vopt("sha512", 500000))
    store.update_item(item["id"], {"source_ref": {"part": str(part), "final": str(final)}})
    ctx = EngineContext(store, store.get_item(item["id"]), store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    out = http_engine._finish(ctx, str(part), str(final), 500000, 500000)
    check("_finish publishes a whole .part whose hash matches", out.kind == Outcome.DONE and final.read_bytes() == srv.payload(500000) and not part.exists())

    store, lib = env()
    (lib / "Files").mkdir(parents=True)
    part, final = lib / "Files" / "big.bin.part", lib / "Files" / "big.bin"
    part.write_bytes(srv.payload(500000))
    item = claim(store, BASE + "/file.bin?size=500000", verify=vopt("sha512", 499999))
    store.update_item(item["id"], {"source_ref": {"part": str(part), "final": str(final)}})
    ctx = EngineContext(store, store.get_item(item["id"]), store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    out = http_engine._finish(ctx, str(part), str(final), 500000, 500000)
    check("_finish fails a whole .part whose hash differs and removes it (the one this row recorded)",
          out.kind == Outcome.FAILED and not out.retryable and not part.exists() and not final.exists())

    store, lib = env()
    (lib / "Files").mkdir(parents=True)
    stranger = lib / "Files" / "stranger.part"
    stranger.write_bytes(srv.payload(500000))
    item = claim(store, BASE + "/file.bin?size=500000", verify=vopt("sha512", 499999))
    store.update_item(item["id"], {"source_ref": {"part": str(lib / "Files" / "mine.part"), "final": str(lib / "Files" / "mine")}})
    ctx = EngineContext(store, store.get_item(item["id"]), store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    out = http_engine._finish(ctx, str(stranger), str(lib / "Files" / "stranger"), 500000, 500000)
    check("a file the row did NOT record is never removed, even on a mismatch", out.kind == Outcome.FAILED and stranger.exists())


def test_daemon_does_not_retry():
    engines.load()
    store, lib = env()
    d = HubDaemon(store, clock=time.time)
    d.start_up()
    try:
        bad = store.add_item(engine="http", source_url=BASE + "/file.bin?size=200000", title="", options={"verify": vopt("sha256", 199999)})[0]
        good = store.add_item(engine="http", source_url=BASE + "/file.bin?size=200001", title="", options={"verify": vopt("sha1", 200001)})[0]
        end = time.time() + 25
        while time.time() < end:
            d.tick()
            if all(store.get_item(i["id"])["state"] in ("done", "failed") for i in (bad, good)):
                break
            time.sleep(0.02)
        row, ok = store.get_item(bad["id"]), store.get_item(good["id"])
        check("the daemon turns a mismatch into a failed row at once: one attempt, no retry scheduled",
              row["state"] == "failed" and row["attempts"] == 1 and not row.get("next_attempt_at") and "checksum mismatch" in (row["error"] or ""))
        check("...and a matching one through the same daemon is done", ok["state"] == "done" and Path(ok["dest_file"]).read_bytes() == srv.payload(200001))
        check("...only the good file is in the library (the bad one was deleted)", files_under(lib) == ["Files/" + Path(ok["dest_file"]).name])
    finally:
        d.stop(5)


# ---------------------------------------------------------------- dest_abs refuses the protected places
def _protected_cases():
    home = Path.home()
    if sys.platform == "win32":
        refused = ["C:\\Windows", "C:\\Windows\\System32\\x", "C:\\Program Files\\App", "C:\\ProgramData\\x", str(home), str(home / ".jarvis"),
                   str(home / ".jarvis" / "hub" / "x")]
        allowed = [str(home / "Downloads" / "x"), "D:\\Games", "C:\\Users\\Public\\Downloads"]
    else:
        refused = ["/etc", "/etc/cron.d", "/usr/lib/x", "/bin", "/sbin/x", "/lib/x", "/lib64", "/boot/x", "/dev/shm", "/proc/1", "/sys/x",
                   str(home), str(home / ".jarvis"), str(home / ".jarvis" / "hub" / "x")]
        allowed = [str(home / "Downloads" / "x"), str(home / "Elsewhere"), "/mnt/elsewhere", "/tmp/jarvis-x", "/opt/games", "/usrx/not-usr"]
    return refused, allowed


def test_protected_dest_abs():
    check("path_tools._protected_reason still exists (this test follows it; if it was renamed, fix the hub's list too)",
          callable(getattr(path_tools, "_protected_reason", None)))
    refused, allowed = _protected_cases()
    for p in refused:
        check("abs_folder refuses %s" % p, settings_mod.abs_folder(p) is None)
        check("...and so does path_tools for writing (the two lists agree on it)", path_tools._protected_reason(Path(p), writing=True) is not None)
        check("...with a reason in words", bool(settings_mod.protected_folder_reason(p)))
    for p in allowed:
        check("abs_folder keeps %s" % p, settings_mod.abs_folder(p) == str(Path(p)))
        check("...path_tools agrees it is fine", path_tools._protected_reason(Path(p), writing=True) is None)
    check("the old refusals stay: a drive / filesystem root", settings_mod.abs_folder(Path(HOME).anchor) is None)
    check("...a relative path", settings_mod.abs_folder("Downloads/x") is None)
    check("...'..' anywhere", settings_mod.abs_folder(str(Path(HOME) / "a" / ".." / "b")) is None)
    check("...an empty string and a NUL", settings_mod.abs_folder("") is None and settings_mod.abs_folder("/mnt/a\x00b") is None)
    check("~ is expanded before the check (bare ~ is the home folder itself, ~/.jarvis is the state folder)",
          settings_mod.abs_folder("~") is None and settings_mod.abs_folder("~/.jarvis") is None
          and settings_mod.abs_folder("~/Downloads/x") == str(Path(HOME) / "Downloads" / "x"))
    check("a folder that merely STARTS like a protected name is fine (.jarvis-backup, /etcetera)",
          settings_mod.abs_folder(str(Path(HOME) / ".jarvis-backup")) is not None
          and (sys.platform == "win32" or settings_mod.abs_folder("/etcetera/x") is not None))

    # the whole list, both ways, against the other function (any change to one list must be made in the other)
    both = refused + allowed + [str(Path(HOME) / "Downloads"), "/", "C:\\", "/home", "/var/tmp/x", "/opt", "/root/x", "/srv/x"]
    drift = [p for p in both if (settings_mod.protected_folder_reason(p) is None) != (path_tools._protected_reason(Path(p), writing=True) is None)]
    check("protected_folder_reason and path_tools._protected_reason give the same yes / no for every case (%s)" % ", ".join(drift[:3]), not drift)

    # where it matters: every way a dest_abs gets in
    store = service._store(None)
    before = len(default_rows())
    for p in refused:
        where, problem = service.split_dest(p, Path(HOME) / "lib", allow_absolute=True)
        check("split_dest refuses %s even after the owner said yes" % p, where == {} and bool(problem))
    for p in (HOME, "~", "/etc/jarvis-x", str(Path(HOME) / ".jarvis" / "dl")) if sys.platform != "win32" else (HOME, "~", "C:\\Windows\\x"):
        res = service.add_items([PUBLIC + "/zz.iso"], dest=p, allow_absolute_dest=True, start_paused=True)
        check("add_items(dest=%s) with approval is refused and nothing is queued" % p, res.get("ok") is False and len(default_rows()) == before)
    res = service.add_items([PUBLIC + "/zz.iso"], dest=str(Path(HOME) / "Elsewhere"), allow_absolute_dest=True, start_paused=True)
    check("a normal full path with approval still works",
          res["ok"] and stored_options(res["added"][0]["id"]).get("dest_abs") == str(Path(HOME) / "Elsewhere"))
    res = service.add_items([PUBLIC + "/zz2.iso"], dest=str(Path(HOME) / "Elsewhere2"), start_paused=True)
    check("without approval a full path is still refused first (needs_absolute, unchanged)", res.get("ok") is False and "approved" in res.get("error", ""))

    cfg = store.get_settings()
    bad_dir = refused[0]
    d, src = settings_mod.resolve_dest_dir(cfg, "a.bin", "file", {"dest_abs": bad_dir})
    check("resolve_dest_dir gives no folder for a protected dest_abs (the engine then refuses the download)", d is None)
    d, src = settings_mod.resolve_dest_dir(cfg, "a.bin", "file", {"dest_abs": HOME})
    check("...nor for the home folder itself", d is None)
    roots = settings_mod.write_roots(cfg, {"dest_abs": HOME})
    check("write_roots never lists a protected dest_abs as a place the hub may write to",
          Path(HOME) not in [Path(r) for r in roots] and len(roots) == 1)
    out, problem = settings_mod._check_ext_folders({".iso": refused[0]}, allow_abs=True)
    check("an extension folder in a protected place is refused too", out is None and bool(problem))
    out, problem = settings_mod._check_ext_folders({".iso": allowed[0]}, allow_abs=True)
    check("...and a normal full path is still taken", out == {".iso": str(Path(allowed[0]))} and problem is None)

    # a row written around the hub's check (a stored dest_abs) never writes there either
    store2, lib = env()
    target = Path(HOME) / ".jarvis" / "hub-dest-test"
    item = claim(store2, BASE + "/file.bin?size=1000", dest_abs=str(target))
    out = run(store2, item)
    check("a stored dest_abs inside ~/.jarvis fails the download and creates nothing there",
          out.kind == Outcome.FAILED and not target.exists() and STATE.log and "isn't allowed" in out.error)
    store2, lib = env()
    item = claim(store2, BASE + "/file.bin?size=1000", dest_abs=HOME)
    out = run(store2, item)
    check("a stored dest_abs that is the home folder itself fails the download", out.kind == Outcome.FAILED and "isn't allowed" in out.error
          and not (Path(HOME) / "file.bin").exists())
    ok_dir = Path(HOME) / "FullPathOK"
    item = claim(store2, BASE + "/file.bin?size=1000", dest_abs=str(ok_dir), verify=vopt("sha256", 1000))
    out = run(store2, item)
    check("an allowed full path still works end to end, with verify", out.kind == Outcome.DONE and Path(out.dest_file) == ok_dir / "file.bin")


try:
    test_checker()
    test_add_path()
    test_match()
    test_mismatch()
    test_resume()
    test_size_cap()
    test_stop_while_hashing()
    test_no_option_unchanged()
    test_finish_directly()
    test_daemon_does_not_retry()
    test_protected_dest_abs()
finally:
    srv.stop(SERVER)
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
