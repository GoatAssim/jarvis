"""Download Hub: two HTTP downloads of the same file name must never share one `.part` file.

The bug (brief 6D): the engine chose `final = unique_path(dest / name)` and `part = final + ".part"`,
but the `.part` file only came into being when the transfer started, so two items that started
together both saw the name as free and took the same pair; one then failed at the rename ("a
download's `.part` file vanished before the rename"), or both wrote into one file.

Covers: the reservation itself (many threads, one name); the natural race (two real downloads of
one name started at the same moment, repeated 50 times); the same with the interleaving FORCED
(both picks happen before either claim); two downloads finishing together and getting two final
names; an existing finished file never overwritten at the rename; a stale `.part` of a dead item
never adopted by a new download; a paused download resuming on its OWN `.part` (same name, Range,
with a second download of the same name running meanwhile); a resumed item whose `.part` vanished
(it never writes into a name another download holds); and the segmented path under the same race.

Run: python tests/test_hub_http_name_race.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-hub-race-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.hub import naming, states  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

http_engine.MIN_PART_BYTES = 64 * 1024            # small test files can be split

ROUNDS = 50                                       # the natural race is repeated this many times
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


# --------------------------------------------------------------------------- the server (real Range)

class Handler(srv.Handler):
    """The shared test server, with a real `bytes=a-b` Range so the segmented path can run."""

    def _serve(self, size, ranges, delay=0.0, extra_headers=None):
        data = srv.payload(size)
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if ranges and rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = min(int(b), size - 1) if b else size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        body = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"v1"')
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        for i in range(0, len(body), srv.CHUNK):
            if not self._write(body[i:i + srv.CHUNK]):
                return
            if delay:
                time.sleep(delay)


def start_server():
    state = srv.State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = srv.Server(("127.0.0.1", 0), handler)
    state.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % state.port, state


def _quiet(self, request, client_address):          # a part closed early resets its connection: not news
    pass


srv.Server.handle_error = _quiet
SERVER, BASE, STATE = start_server()


# --------------------------------------------------------------------------- helpers

def env(segments=None, **more):
    """A fresh store and library root, pointed at the local server."""
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    cfg = {"library_root": str(lib), "allow_private_hosts": ["127.0.0.1"], "http.stall_seconds": 5}
    if segments:
        cfg["http.segments"] = segments
        cfg["http.segment_min_bytes"] = 100_000
    cfg.update(more)
    store.set_settings(cfg)
    STATE.reset()
    return store, lib


def claim(store, url, **options):
    item = store.add_item(engine="http", source_url=url, title="", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def make_ctx(store, item, stop=None):
    return EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None),
                         rate_limit_bps=lambda: 0)


def run(store, item, stop=None):
    return http_engine.ENGINE.run(item, make_ctx(store, item, stop))


def requeue_like_the_daemon(store, item_id):
    store.transition(item_id, "paused", expect=states.ACTIVE_STATES)
    store.transition(item_id, "queued", expect="paused")
    return store.claim_next({"default": 100}, 100)


def pause_after_first_chunk():
    return lambda: "pause"


def run_together(store, items):
    """Run every item in its own thread, released at the same moment.  -> [Outcome or Exception]."""
    gate = threading.Barrier(len(items))
    results = [None] * len(items)

    def work(i, item):
        try:
            gate.wait(10)
            results[i] = run(store, item)
        except BaseException as exc:  # noqa: BLE001 - reported, not raised in a thread
            results[i] = exc

    threads = [threading.Thread(target=work, args=(i, it), daemon=True) for i, it in enumerate(items)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def forced_overlap(module, name):
    """Make the FIRST call of `module.name` in each thread wait for the other thread's first call.
    Used on two points of the engine's start: `naming.unique_path` (both threads have chosen a name
    before either has claimed one) and `shutil.disk_usage` (the free-space check, which runs AFTER the
    name is settled and BEFORE the first byte is written: both threads are held there, so on the old
    code both then write into one `.part`).  Returns an undo function."""
    real = getattr(module, name)
    gate = threading.Barrier(2)
    seen = threading.local()

    def wrapper(*args, **kwargs):
        if not getattr(seen, "done", False):
            seen.done = True
            try:
                gate.wait(2)
            except threading.BrokenBarrierError:
                pass
        return real(*args, **kwargs)

    setattr(module, name, wrapper)
    return lambda: setattr(module, name, real)


def finished_ok(results):
    return all(isinstance(r, Outcome) and r.kind == Outcome.DONE for r in results)


def files_in(folder):
    return sorted(p.name for p in Path(folder).glob("*")) if Path(folder).exists() else []


# --------------------------------------------------------------------------- the tests

def test_reservation_unit():
    folder = Path(HOME) / "unit"
    folder.mkdir()
    reserve = getattr(http_engine, "_reserve", None)
    check("the engine has one place that reserves a name", callable(reserve))
    if not callable(reserve):
        return
    got, gate = [], threading.Barrier(24)

    def work():
        gate.wait(10)
        got.append(reserve(folder, "a.bin"))

    threads = [threading.Thread(target=work, daemon=True) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    finals = [f for f, _p in got]
    parts = [p for _f, p in got]
    check("24 threads reserving one name get 24 different names", len(got) == 24 and len(set(finals)) == 24)
    check("...and 24 different .part files", len(set(parts)) == 24 and all(Path(p).exists() for p in parts))
    check("each .part is the final name plus .part, and empty", all(p == f + ".part" and Path(p).stat().st_size == 0
                                                                   for f, p in got))
    check("the first one keeps the plain name, the rest are 'name (n).ext'",
          sorted(Path(f).name for f in finals)[0] == "a (1).bin" and "a.bin" in [Path(f).name for f in finals])
    check("a reserved name is never a finished file", not any(Path(f).exists() for f in finals))


def test_natural_race():
    bad, why = 0, []
    for i in range(ROUNDS):
        store, lib = env()
        items = [claim(store, BASE + "/file.bin?size=300000"), claim(store, BASE + "/file.bin?size=300000")]
        results = run_together(store, items)
        names = files_in(lib / "Files")
        ok = (finished_ok(results) and names == ["file (1).bin", "file.bin"]
              and all((lib / "Files" / n).read_bytes() == srv.payload(300000) for n in names)
              and len({r.dest_file for r in results}) == 2)
        if not ok:
            bad += 1
            if len(why) < 2:
                why.append([getattr(r, "error", r) for r in results])
    check("two downloads of one name started together: all %d rounds end with two intact files" % ROUNDS, bad == 0)
    if bad:
        print("        (%d of %d rounds went wrong; first errors: %s)" % (bad, ROUNDS, why))


def test_forced_interleaving():
    undo = forced_overlap(naming, "unique_path"), forced_overlap(shutil, "disk_usage")
    try:
        store, lib = env()
        items = [claim(store, BASE + "/file.bin?size=200000"), claim(store, BASE + "/file.bin?size=200000")]
        results = run_together(store, items)
    finally:
        for fn in undo:
            fn()
    names = files_in(lib / "Files")
    check("both picked the same name before either claimed it: both still finish", finished_ok(results))
    check("...with two different final names and no .part left", names == ["file (1).bin", "file.bin"])
    check("...and both files hold the right bytes", all((lib / "Files" / n).read_bytes() == srv.payload(200000)
                                                       for n in names))


def _two_parts_one_final(store, lib, contents):
    """Two items that both ended up with the SAME final name (and each its own .part), as a legacy row
    or a hand-edited row could.  -> [(ctx, part)]"""
    folder = lib / "Files"
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "same.bin"
    out = []
    for i, data in enumerate(contents):
        item = claim(store, BASE + "/file.bin?size=10")
        part = folder / ("same%d.bin.part" % i)
        part.write_bytes(data)
        out.append((make_ctx(store, item), str(part), len(data)))
    return str(final), out


def test_finishing_together():
    bad = 0
    for _ in range(100):
        store, lib = env()
        a, b = b"A" * 5000, b"B" * 7000
        final, jobs = _two_parts_one_final(store, lib, [a, b])
        gate = threading.Barrier(2)
        outs = [None, None]

        def work(i, ctx, part, size):
            gate.wait(10)
            outs[i] = http_engine._finish(ctx, part, final, size, size)

        threads = [threading.Thread(target=work, args=(i,) + job, daemon=True) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        files = {p.name: p.read_bytes() for p in (lib / "Files").glob("*") if not p.name.endswith(".part")}
        if not (all(o is not None and o.kind == Outcome.DONE for o in outs) and len(files) == 2
                and sorted(files.values()) == sorted([a, b])
                and not list((lib / "Files").glob("*.part"))):
            bad += 1
    check("two downloads finishing together under one final name get two names (100 rounds, none lost)", bad == 0)

    undo_replace = forced_overlap(os, "replace")
    undo_link = forced_overlap(os, "link")
    try:
        store, lib = env()
        final, jobs = _two_parts_one_final(store, lib, [a, b])
        gate = threading.Barrier(2)
        outs = [None, None]

        def work2(i, ctx, part, size):
            gate.wait(10)
            outs[i] = http_engine._finish(ctx, part, final, size, size)

        threads = [threading.Thread(target=work2, args=(i,) + job, daemon=True) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        undo_replace()
        undo_link()
    files = {p.name: p.read_bytes() for p in (lib / "Files").glob("*")}
    check("...also when both reach the rename at the very same instant (forced)",
          all(o is not None and o.kind == Outcome.DONE for o in outs) and len(files) == 2
          and sorted(files.values()) == sorted([a, b]))


def test_existing_file_not_overwritten():
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1000")
    folder = lib / "Files"
    folder.mkdir(parents=True)
    part = folder / "file.bin.part"
    part.write_bytes(srv.payload(1000))
    mine = folder / "file.bin"
    mine.write_bytes(b"somebody else's finished file")      # appeared after our name was chosen
    out = http_engine._finish(make_ctx(store, item), str(part), str(mine), 1000, 1000)
    check("a finished file that took the name meanwhile is never overwritten",
          out.kind == Outcome.DONE and mine.read_bytes() == b"somebody else's finished file")
    check("...the download lands under the next free name instead",
          Path(out.dest_file).name == "file (1).bin" and Path(out.dest_file).read_bytes() == srv.payload(1000)
          and not part.exists())
    check("...and the row learned the name it really got", store.get_item(item["id"])["dest_file"] == out.dest_file)

    # another download's reserved name is not taken as a free final name either
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1000")
    folder = lib / "Files"
    folder.mkdir(parents=True)
    part = folder / "file.bin.part"
    part.write_bytes(srv.payload(1000))
    (folder / "file.bin").write_bytes(b"old")
    (folder / "file (1).bin.part").write_bytes(b"another download is writing here")
    out = http_engine._finish(make_ctx(store, item), str(part), str(folder / "file.bin"), 1000, 1000)
    check("a name another download has reserved ('(1)' with its own .part) is skipped",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "file (2).bin"
          and (folder / "file (1).bin.part").read_bytes() == b"another download is writing here")

    # a finish that was interrupted after the link but before the .part was removed is completed, not duplicated
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=1000")
    folder = lib / "Files"
    folder.mkdir(parents=True)
    part = folder / "file.bin.part"
    part.write_bytes(srv.payload(1000))
    os.link(str(part), str(folder / "file.bin"))
    out = http_engine._finish(make_ctx(store, item), str(part), str(folder / "file.bin"), 1000, 1000)
    check("a rename that was interrupted halfway is completed under the same name (no '(1)' copy)",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "file.bin" and not part.exists()
          and files_in(folder) == ["file.bin"])


def test_stale_part_not_adopted():
    store, lib = env()
    folder = lib / "Files"
    folder.mkdir(parents=True)
    stale = folder / "file.bin.part"
    stale.write_bytes(b"leftover of a dead download")
    item = claim(store, BASE + "/file.bin?size=50000")
    out = run(store, item)
    check("a stale .part (no item owns it) is never adopted: the new download takes the next name",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "file (1).bin")
    check("...its bytes are the server's, not appended to the leftover",
          Path(out.dest_file).read_bytes() == srv.payload(50000))
    check("...and the stale .part is left exactly as it was", stale.read_bytes() == b"leftover of a dead download")
    check("...the server was asked for the whole file (no Range)", STATE.log[0][1] is None)


def test_resume_keeps_its_own_part():
    store, lib = env()
    size = 1_048_576
    a = claim(store, BASE + "/file.bin?size=%d" % size)
    first = run(store, a, stop=pause_after_first_chunk())
    ref = store.get_item(a["id"])["source_ref"]
    part = Path(ref["part"])
    got = part.stat().st_size
    check("a paused download keeps its reserved .part and records it", first.kind == Outcome.PAUSED
          and part.name == "file.bin.part" and 0 < got < size)

    b = claim(store, BASE + "/file.bin?size=5000")           # same name, while the first one is paused
    out_b = run(store, b)
    check("a second download of the same name meanwhile gets the next name, never the paused one's",
          out_b.kind == Outcome.DONE and Path(out_b.dest_file).name == "file (1).bin")
    check("...and the paused download's .part is untouched", part.exists() and part.stat().st_size == got)

    STATE.reset()
    a = requeue_like_the_daemon(store, a["id"])
    second = run(store, a)
    check("the resume goes on with its OWN .part and its own name",
          second.kind == Outcome.DONE and Path(second.dest_file) == lib / "Files" / "file.bin"
          and STATE.log[0][1] == "bytes=%d-" % got and Path(second.dest_file).read_bytes() == srv.payload(size))
    check("...and the other download's file is untouched",
          (lib / "Files" / "file (1).bin").read_bytes() == srv.payload(5000) and not part.exists())


def test_resume_when_the_part_vanished():
    # (a) the name is free again: the item takes its own name back and starts from 0
    store, lib = env()
    a = claim(store, BASE + "/file.bin?size=1048576")
    run(store, a, stop=pause_after_first_chunk())
    part = Path(store.get_item(a["id"])["source_ref"]["part"])
    part.unlink()
    STATE.reset()
    a = requeue_like_the_daemon(store, a["id"])
    out = run(store, a)
    check("a resumed item whose .part vanished starts over under its own name (no Range)",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "file.bin" and STATE.log[0][1] is None
          and Path(out.dest_file).read_bytes() == srv.payload(1048576))

    # (b) another download took that name meanwhile and FINISHED: the resumed item never overwrites it
    store, lib = env()
    a = claim(store, BASE + "/file.bin?size=1048576")
    run(store, a, stop=pause_after_first_chunk())
    Path(store.get_item(a["id"])["source_ref"]["part"]).unlink()
    b = claim(store, BASE + "/file.bin?size=7000")
    out_b = run(store, b)
    check("the name was free again, so the other download took it", Path(out_b.dest_file).name == "file.bin")
    a = requeue_like_the_daemon(store, a["id"])
    out = run(store, a)
    check("the resumed item never overwrites that finished file: it ends under the next name",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "file (1).bin"
          and Path(out.dest_file).read_bytes() == srv.payload(1048576)
          and (lib / "Files" / "file.bin").read_bytes() == srv.payload(7000))
    check("...and no .part is left", not list((lib / "Files").glob("*.part")))

    # (c) the exclusive re-claim itself: a missing .part is taken back, a held one is refused
    folder = Path(HOME) / "reclaim"
    folder.mkdir()
    target = folder / "x.bin.part"
    check("a missing .part is taken back (created empty, exclusively)",
          http_engine._reclaim(str(target)) is True and target.exists() and target.stat().st_size == 0)
    target.write_bytes(b"held by someone")
    check("a .part that exists is refused and left exactly as it is",
          http_engine._reclaim(str(target)) is False and target.read_bytes() == b"held by someone")
    check("a missing folder is made first",
          http_engine._reclaim(str(folder / "sub" / "y.bin.part")) is True)


def test_segmented_race():
    size = 600_000
    bad, parallel = 0, 0
    for _ in range(15):
        store, lib = env(segments=4)
        items = [claim(store, BASE + "/file.bin?size=%d" % size), claim(store, BASE + "/file.bin?size=%d" % size)]
        results = run_together(store, items)
        names = files_in(lib / "Files")
        ok = (finished_ok(results) and names == ["file (1).bin", "file.bin"]
              and all((lib / "Files" / n).read_bytes() == srv.payload(size) for n in names))
        if not ok:
            bad += 1
        parallel += sum(1 for r in STATE.log if r[1] and r[1].startswith("bytes=") and not r[1].endswith("-"))
    check("two SEGMENTED downloads of one name started together: 15 rounds, two intact files each", bad == 0)
    check("...and the parts really were fetched in parallel (the test exercised the segmented path)", parallel > 0)

    undo = forced_overlap(naming, "unique_path"), forced_overlap(shutil, "disk_usage")
    try:
        store, lib = env(segments=4)
        items = [claim(store, BASE + "/file.bin?size=%d" % size), claim(store, BASE + "/file.bin?size=%d" % size)]
        results = run_together(store, items)
    finally:
        for fn in undo:
            fn()
    check("segmented, both picks before either claim (forced): still two names, two intact files",
          finished_ok(results) and files_in(lib / "Files") == ["file (1).bin", "file.bin"]
          and all((lib / "Files" / n).read_bytes() == srv.payload(size) for n in files_in(lib / "Files")))


def test_nothing_left_after_an_early_failure():
    # a refusal for lack of space happens after the name is reserved: the reservation must not stay behind
    import types
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=100000")
    real = shutil.disk_usage
    shutil.disk_usage = lambda p: types.SimpleNamespace(total=10, used=10, free=10)
    try:
        out = run(store, item)
    finally:
        shutil.disk_usage = real
    check("a refusal for lack of space leaves no reserved .part behind",
          out.kind == Outcome.FAILED and not out.retryable and not list((lib / "Files").glob("*.part")))
    check("...and the row keeps no resume handle for it", not store.get_item(item["id"])["source_ref"])
    item = claim(store, BASE + "/file.bin?size=1048576")
    out = run(store, item, stop=lambda: "cancel")
    check("a cancel removes the reserved .part", out.kind == Outcome.CANCELLED and not list((lib / "Files").glob("*.part")))


if __name__ == "__main__":
    for fn in (test_reservation_unit, test_natural_race, test_forced_interleaving, test_finishing_together,
               test_existing_file_not_overwritten, test_stale_part_not_adopted, test_resume_keeps_its_own_part,
               test_resume_when_the_part_vanished, test_segmented_race, test_nothing_left_after_an_early_failure):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a crash in one test is one failed check, not a lost run
            import traceback
            traceback.print_exc()
            check("%s ran without crashing (%s)" % (fn.__name__, type(exc).__name__), False)
    SERVER.shutdown()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
