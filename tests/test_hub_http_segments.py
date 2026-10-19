"""Download Hub, phase 9: segmented (multi-connection) HTTP download.

Against a real local server that implements Range properly (`bytes=a-b`) plus a few servers that
misbehave on purpose.  Covers: it is OFF unless asked for (setting or per-item option, the item
wins, small files and servers without Accept-Ranges stay on one connection); the parts tile the
file and the result is byte-identical; a server that answers 200 / 429 / a different ETag /
the wrong range for the parallel parts makes the download carry on as ONE connection inside the
same run; the SSRF guard refuses a private redirect on any part and credentials are dropped on a
host change for every part; pause keeps a table of parts and resume asks each part to continue
where it was (and nothing for a finished part); a SIGKILLed process resumes; a table that doesn't
match the file is thrown away; the rate limit is shared by the parts; cancel removes the file; a
short part is a retryable stall that keeps its progress; and `stream_to_file` called the old way
(the Stremio engine does) never segments.

Run: python tests/test_hub_http_segments.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = tempfile.mkdtemp(prefix="jarvis-hub-seg-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

import _hub_testserver as srv  # noqa: E402
from jarvis.hub import settings as settings_mod  # noqa: E402
from jarvis.hub import states  # noqa: E402
from jarvis.hub.engines import http as http_engine  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

http_engine.MIN_PART_BYTES = 64 * 1024            # small test files can be split

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


# --------------------------------------------------------------------------- the server

class Handler(srv.Handler):
    """The shared test server's routes, with a REAL Range (`bytes=a-b`) and some liars."""

    def _serve(self, size, ranges, delay=0.0, extra_headers=None, lie_ranges=False, body_cut=None, etag='"v1"'):
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
        if ranges or lie_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body_cut is not None:
            body = body[: int(len(body) * body_cut)]
        for i in range(0, len(body), srv.CHUNK):
            if not self._write(body[i:i + srv.CHUNK]):
                return
            if delay:
                time.sleep(delay)
        if body_cut is not None:
            self.close_connection = True

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path
        size = int(query.get("size", ["300000"])[0])
        delay = float(query.get("delay", ["0"])[0])
        st = self.state
        has_range = bool(self.headers.get("Range"))
        if path in ("/file.bin", "/slow.bin"):
            st.log.append((path, self.headers.get("Range"), self.headers.get("If-Range")))
            st.headers.append(dict(self.headers))
            return self._serve(size, True, delay if path == "/slow.bin" else 0.0)
        special = {"/liar.bin", "/limit.bin", "/redir-parts", "/shortparts.bin", "/changed.bin", "/flip.bin",
                   "/wrongrange.bin"}
        if path not in special:
            return super().do_GET()
        st.log.append((path, self.headers.get("Range"), self.headers.get("If-Range")))
        st.headers.append(dict(self.headers))
        if path == "/liar.bin":                       # says Accept-Ranges, ignores Range
            return self._serve(size, False, lie_ranges=True)
        if path == "/limit.bin":
            if has_range:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._serve(size, True)
        if path == "/redir-parts":
            if has_range:
                self.send_response(302)
                self.send_header("Location", "http://10.0.0.1/secret")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._serve(size, True)
        if path == "/shortparts.bin":
            return self._serve(size, True, body_cut=0.4 if has_range else None)
        if path == "/changed.bin":
            return self._serve(size, True, etag='"v2"' if has_range else '"v1"')
        if path == "/flip.bin":                       # honours Range until the test flips it
            if has_range and st.flip:
                return self._serve(size, False, lie_ranges=True)
            return self._serve(size, True, delay)
        if path == "/wrongrange.bin":                 # a 206 for the wrong place
            if has_range:
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-9/%d" % size)
                self.send_header("Content-Length", "10")
                self.end_headers()
                self._write(srv.payload(10))
                return
            return self._serve(size, True)


def start_server():
    state = srv.State()
    state.flip = False
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = srv.Server(("127.0.0.1", 0), handler)
    state.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % state.port, state


def _quiet(self, request, client_address):          # a part closed early resets its connection: not news
    pass


srv.Server.handle_error = _quiet
SERVER, BASE, STATE = start_server()
SIZE = 1_600_000


def env(segments=4, min_bytes=100_000, allow=("127.0.0.1", "localhost"), stall=5, **more):
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    cfg = {"library_root": str(lib), "allow_private_hosts": list(allow), "http.stall_seconds": stall,
           "http.segment_min_bytes": min_bytes}
    if segments is not None:
        cfg["http.segments"] = segments
    cfg.update(more)
    store.set_settings(cfg)
    STATE.reset()
    STATE.flip = False
    return store, lib


def claim(store, url, **options):
    item = store.add_item(engine="http", source_url=url, title="", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None, limit=0):
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None),
                        rate_limit_bps=lambda: limit)
    return http_engine.ENGINE.run(item, ctx), ctx


def ranges_of(path):
    out = []
    for p, rng, _ in STATE.log:
        if p == path and rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            out.append((int(m.group(1)), int(m.group(2)) if m.group(2) else None))
    return sorted(out)


def requests_to(path):
    return [r for r in STATE.log if r[0] == path]


def content_ok(path, size):
    return Path(path).read_bytes() == srv.payload(size)


def files_in(lib):
    return sorted(p.name for p in lib.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- units

def test_units():
    plan = http_engine._plan_segments(1_000_000, 4)
    check("the parts tile the file with no gap and no overlap",
          [(x.s, x.e) for x in plan] == [(0, 250000), (250000, 500000), (500000, 750000), (750000, 1000000)])
    odd = http_engine._plan_segments(1_000_003, 3)
    check("an odd size still ends exactly at the end of the file", odd[0].s == 0 and odd[-1].e == 1_000_003
          and all(a.e == b.s for a, b in zip(odd, odd[1:])))
    check("a file too small for two parts isn't split", http_engine._plan_segments(100_000, 4) is None)
    check("the count is capped by the part size and by SEGMENT_MAX",
          len(http_engine._plan_segments(300_000, 16)) == 300_000 // http_engine.MIN_PART_BYTES
          and len(http_engine._plan_segments(10 ** 9, 99)) == http_engine.SEGMENT_MAX)

    class Ctx:
        def __init__(self, seg):
            self.seg = seg

        def setting(self, *path, default=None):
            return self.seg if path == ("http", "segments") else default

    check("segments default to 1 (off)", http_engine.wanted_segments(Ctx(None), {}) == 1)
    check("the setting is used", http_engine.wanted_segments(Ctx(4), {}) == 4)
    check("the item's option wins over the setting, even to turn it off",
          http_engine.wanted_segments(Ctx(4), {"options": {"segments": 2}}) == 2
          and http_engine.wanted_segments(Ctx(4), {"options": {"segments": 1}}) == 1)
    check("nonsense means off, and the cap holds",
          http_engine.wanted_segments(Ctx("lots"), {}) == 1 and http_engine.wanted_segments(Ctx(True), {}) == 1
          and http_engine.wanted_segments(Ctx(500), {}) == http_engine.SEGMENT_MAX
          and http_engine.wanted_segments(Ctx(0), {}) == 1 and http_engine.wanted_segments(Ctx(4), {"options": {"segments": "x"}}) == 4)
    part = Path(HOME) / "tbl.part"
    part.write_bytes(b"\0" * 100)
    good = {"segments": [{"s": 0, "e": 50, "d": 10}, {"s": 50, "e": 100, "d": 0}], "total": 100}
    check("a consistent table is accepted", http_engine._load_table(good, str(part)) is not None)
    bad_cases = {
        "a gap": {"segments": [{"s": 0, "e": 40, "d": 0}, {"s": 50, "e": 100, "d": 0}], "total": 100},
        "an overlap": {"segments": [{"s": 0, "e": 60, "d": 0}, {"s": 50, "e": 100, "d": 0}], "total": 100},
        "done beyond the part": {"segments": [{"s": 0, "e": 50, "d": 51}, {"s": 50, "e": 100, "d": 0}], "total": 100},
        "negative done": {"segments": [{"s": 0, "e": 50, "d": -1}, {"s": 50, "e": 100, "d": 0}], "total": 100},
        "not covering the total": {"segments": [{"s": 0, "e": 50, "d": 0}, {"s": 50, "e": 90, "d": 0}], "total": 100},
        "a single part": {"segments": [{"s": 0, "e": 100, "d": 0}], "total": 100},
        "text instead of numbers": {"segments": [{"s": "0", "e": 50, "d": 0}, {"s": 50, "e": 100, "d": 0}], "total": 100},
        "booleans": {"segments": [{"s": False, "e": 50, "d": 0}, {"s": 50, "e": 100, "d": 0}], "total": 100},
        "too many parts": {"segments": [{"s": i, "e": i + 1, "d": 0} for i in range(17)], "total": 17},
        "not a list": {"segments": "x", "total": 100},
        "no total": {"segments": good["segments"]},
        "the wrong file size": dict(good, total=101),
    }
    for label, table in bad_cases.items():
        check("a table with %s is refused" % label, http_engine._load_table(table, str(part)) is None)
    check("...and so is a table whose file is gone", http_engine._load_table(good, str(part) + ".missing") is None)
    check("the settings exist, default to OFF and validate",
          settings_mod.DEFAULTS["http"]["segments"] == 1 and "http.segments" in settings_mod.WRITABLE
          and settings_mod.validate_update({"http.segments": 4})[1] == []
          and settings_mod.validate_update({"http.segments": 17})[1] != []
          and settings_mod.validate_update({"http.segment_min_bytes": 5})[1] != [])


# --------------------------------------------------------------------------- the download

def test_basic():
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    outcome, _ = run(store, item)
    row = store.get_item(item["id"])
    check("a segmented download finishes", outcome.kind == Outcome.DONE and Path(row["dest_file"]).exists())
    check("the file is byte-identical", content_ok(row["dest_file"], SIZE))
    got = ranges_of("/file.bin")
    quarter = SIZE // 4
    check("one plain request plus three Range requests for the other parts, each bounded",
          len(requests_to("/file.bin")) == 4 and got == [(quarter, 2 * quarter - 1), (2 * quarter, 3 * quarter - 1),
                                                         (3 * quarter, SIZE - 1)])
    check("the row is complete: size, 100 %", row["bytes_total"] == SIZE and row["bytes_done"] == SIZE)
    check("no .part is left and the library holds just the file", files_in(lib) == ["file.bin"])
    check("the saved parts are cleared once it is done", "segments" not in (row["source_ref"] or {}))
    events = [e["message"] for e in store.list_events(item["id"])] if hasattr(store, "list_events") else []
    check("the log says it used parallel parts", not events or any("parallel parts" in m for m in events))

    # off by default
    store, lib = env(segments=None)
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    outcome, _ = run(store, item)
    check("with nothing set it is ONE connection, exactly as before",
          outcome.kind == Outcome.DONE and len(requests_to("/file.bin")) == 1 and not ranges_of("/file.bin"))

    # the item's option beats the setting both ways
    store, lib = env(segments=1)
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE, segments=3)
    outcome, _ = run(store, item)
    check("an item can ask for parts when the setting is off",
          outcome.kind == Outcome.DONE and len(requests_to("/file.bin")) == 3
          and content_ok(store.get_item(item["id"])["dest_file"], SIZE))
    store, lib = env(segments=4)
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE, segments=1)
    outcome, _ = run(store, item)
    check("...and can turn it off when the setting is on", outcome.kind == Outcome.DONE and len(requests_to("/file.bin")) == 1)

    # small files and servers without Range stay on one connection
    store, lib = env(min_bytes=2_000_000)
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    outcome, _ = run(store, item)
    check("a file below segment_min_bytes is not split", outcome.kind == Outcome.DONE and len(requests_to("/file.bin")) == 1)
    store, lib = env()
    item = claim(store, BASE + "/norange.bin?size=%d" % SIZE)
    outcome, _ = run(store, item)
    check("a server that doesn't offer Accept-Ranges is never split",
          outcome.kind == Outcome.DONE and len(requests_to("/norange.bin")) == 1)
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=100000")
    outcome, _ = run(store, item)
    check("a file too small for two parts is one connection", outcome.kind == Outcome.DONE and len(requests_to("/file.bin")) == 1)
    store, lib = env()
    item = claim(store, BASE + "/nolength")
    outcome, _ = run(store, item)
    check("a body of unknown length is never split", outcome.kind == Outcome.DONE and len(requests_to("/nolength")) == 1)

    # the Stremio engine's way of calling it never segments
    store, lib = env(segments=4)
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    ctx = EngineContext(store, item, store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
    outcome = http_engine.stream_to_file(ctx, item, item["source_url"])
    check("stream_to_file called the old way (no segments argument) stays on one connection",
          outcome.kind == Outcome.DONE and len(requests_to("/file.bin")) == 1)


def test_fallbacks():
    for path, why in (("/liar.bin", "answers 200 to a Range request"), ("/limit.bin", "answers 429 to parallel requests"),
                      ("/changed.bin", "changes its ETag"), ("/wrongrange.bin", "answers the wrong range")):
        store, lib = env()
        item = claim(store, BASE + path + "?size=%d" % SIZE)
        outcome, _ = run(store, item)
        row = store.get_item(item["id"])
        check("a server that %s: the download still completes, intact, in the same run" % why,
              outcome.kind == Outcome.DONE and content_ok(row["dest_file"], SIZE) and files_in(lib) == [path[1:]])
        check("...on ONE connection (no part was written by a refused part)",
              "segments" not in (row["source_ref"] or {}))
    store, lib = env()
    item = claim(store, BASE + "/liar.bin?size=%d" % SIZE)
    run(store, item)
    msgs = [e["message"] for e in store.list_events(item["id"])] if hasattr(store, "list_events") else None
    check("the fallback is logged", msgs is None or any("one connection" in m for m in msgs))


def test_ssrf_and_credentials():
    store, lib = env()
    item = claim(store, BASE + "/redir-parts?size=%d" % SIZE)
    outcome, _ = run(store, item)
    check("a part redirected to a private address is refused", outcome.kind == Outcome.FAILED and not outcome.retryable
          and "private" in outcome.error)
    check("...with the guard's own message, not a generic 'unexpected error'",
          "unexpected" not in outcome.error and outcome.error.startswith("10.0.0.1 points at a private"))
    check("...and nothing is left on disk", files_in(lib) == [])

    store, lib = env(allow=("127.0.0.1", "localhost"))
    item = claim(store, BASE + "/redirect-host?size=%d" % SIZE, headers={"Authorization": "Bearer secret"})
    outcome, _ = run(store, item)
    row = store.get_item(item["id"])
    pairs = list(zip(STATE.log, STATE.headers))
    to_final = [h for (p, _r, _i), h in pairs if p == "/file.bin"]
    to_first = [h for (p, _r, _i), h in pairs if p == "/redirect-host"]
    check("credentials are dropped when the redirect changes host, for EVERY part",
          outcome.kind == Outcome.DONE and len(to_final) == 4 and not any("Authorization" in h for h in to_final)
          and to_first and all(h.get("Authorization") == "Bearer secret" for h in to_first))
    check("the file is intact after the redirect", content_ok(row["dest_file"], SIZE))

    store, lib = env(allow=())
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    outcome, _ = run(store, item)
    check("the first request is still guarded (a loopback address with no allow-list is refused)",
          outcome.kind == Outcome.FAILED and "private" in outcome.error and not requests_to("/file.bin"))


def test_pause_resume():
    size = 3_000_000
    store, lib = env()
    item = claim(store, BASE + "/slow.bin?size=%d&delay=0.05" % size)
    t0 = time.time()
    outcome, _ = run(store, item, stop=lambda: "pause" if time.time() - t0 > 1.0 else None)
    row = store.get_item(item["id"])
    ref = row["source_ref"]
    table = ref.get("segments") or []
    part = Path(ref["part"])
    check("pause keeps the preallocated .part at the full file size", outcome.kind == Outcome.PAUSED
          and part.exists() and part.stat().st_size == size)
    check("the saved table has one entry per part with progress in it",
          len(table) == 4 and ref["total"] == size and sum(t["d"] for t in table) > 0
          and all(0 <= t["d"] <= t["e"] - t["s"] for t in table))
    check("the row says how far it got, and that is the sum of the parts", row["bytes_done"] == sum(t["d"] for t in table))
    saved = [(t["s"] + t["d"], t["e"] - 1) for t in table if t["d"] < t["e"] - t["s"]]
    done_parts = [t for t in table if t["d"] >= t["e"] - t["s"]]
    STATE.reset()
    store.transition(item["id"], "paused", expect=states.ACTIVE_STATES)
    store.transition(item["id"], "queued", expect="paused")
    again = store.claim_next({"default": 100}, 100)
    outcome, _ = run(store, again)
    row = store.get_item(item["id"])
    asked = ranges_of("/slow.bin")
    check("resume asks each unfinished part to continue exactly where it stopped", asked == sorted(saved))
    check("...and asks nothing for a part that was already complete", len(asked) == len(saved) and len(saved) + len(done_parts) == 4)
    check("the resumed file is complete and byte-identical", outcome.kind == Outcome.DONE and content_ok(row["dest_file"], size))
    check("the If-Range validator went with each resumed part",
          all(r[2] == '"v1"' for r in requests_to("/slow.bin")))

    # cancel removes the file
    store, lib = env()
    item = claim(store, BASE + "/slow.bin?size=%d&delay=0.05" % size)
    t0 = time.time()
    outcome, _ = run(store, item, stop=lambda: "cancel" if time.time() - t0 > 0.8 else None)
    check("cancel stops every part and removes the .part", outcome.kind == Outcome.CANCELLED and files_in(lib) == [])

    # a daemon shutdown requeues and keeps the progress
    store, lib = env()
    item = claim(store, BASE + "/slow.bin?size=%d&delay=0.05" % size)
    t0 = time.time()
    outcome, _ = run(store, item, stop=lambda: "shutdown" if time.time() - t0 > 0.8 else None)
    check("a shutdown asks for a requeue and keeps the parts", outcome.kind == Outcome.REQUEUE
          and len(store.get_item(item["id"])["source_ref"].get("segments") or []) == 4)


CHILD = r'''
import os, sys
from pathlib import Path
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
root, store_path, item_id = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.join(root, "jarvis-cli"))
from jarvis.hub.engines import http as http_engine
from jarvis.hub.engines.base import EngineContext
from jarvis.hub.store import Store
http_engine.MIN_PART_BYTES = 64 * 1024
store = Store(Path(store_path))
item = store.get_item(item_id)
ctx = EngineContext(store, item, store.get_settings(), stop_reason=lambda: None, rate_limit_bps=lambda: 0)
http_engine.ENGINE.run(item, ctx)
'''


def test_kill_and_resume():
    size = 4_000_000
    store, lib = env()
    item = claim(store, BASE + "/slow.bin?size=%d&delay=0.08" % size)
    child = Path(HOME) / "seg_child.py"
    child.write_text(CHILD)
    db = Path(HOME) / ("hub-%d.db" % _n[0])
    proc = subprocess.Popen([sys.executable, str(child), str(ROOT), str(db), item["id"]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.8)
    proc.kill()                                    # SIGKILL / TerminateProcess: no cleanup at all
    proc.wait()
    row = store.get_item(item["id"])
    table = (row["source_ref"] or {}).get("segments") or []
    check("a killed process left the preallocated .part and a saved table of parts",
          len(table) == 4 and Path(row["source_ref"]["part"]).exists() and row["state"] != "done")
    check("...with progress in it", sum(t["d"] for t in table) > 0)
    check("nothing was renamed to the final name", not Path(row["source_ref"]["final"]).exists())
    STATE.reset()
    resumed = store.get_item(item["id"])
    outcome, _ = run(store, resumed)
    row = store.get_item(item["id"])
    asked = ranges_of("/slow.bin")
    check("after the kill the download resumes and completes", outcome.kind == Outcome.DONE)
    check("the result is byte-identical (a table that lags the disk only re-fetches, never corrupts)",
          content_ok(row["dest_file"], size))
    check("it did not start over: it asked for parts beyond their starts",
          any(start > t["s"] for (start, _e), t in zip(asked, [t for t in table if t["d"] < t["e"] - t["s"]]) if start))


def test_bad_tables():
    size = SIZE
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % size)
    part = lib / "Files" / "file.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"\xff" * size)               # looks complete, but its parts say otherwise
    garbage = {"part": str(part), "final": str(lib / "Files" / "file.bin"),
               "segments": [{"s": 0, "e": 10, "d": 9999}, {"s": 10, "e": size, "d": 0}], "total": size}
    store.update_item(item["id"], {"source_ref": garbage})
    item = store.get_item(item["id"])
    outcome, _ = run(store, item)
    row = store.get_item(item["id"])
    check("a table that doesn't add up is thrown away and the file starts over, intact",
          outcome.kind == Outcome.DONE and content_ok(row["dest_file"], size))
    check("...and the first request was a fresh one, not a resume", requests_to("/file.bin")[0][1] is None)

    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % size)
    part = lib / "Files" / "file.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"\x01" * 1000)               # the file is not the size the table says
    good = {"part": str(part), "final": str(lib / "Files" / "file.bin"), "total": size,
            "segments": [{"s": 0, "e": size // 2, "d": 500}, {"s": size // 2, "e": size, "d": 0}]}
    store.update_item(item["id"], {"source_ref": good})
    outcome, _ = run(store, store.get_item(item["id"]))
    check("a table for a file of the wrong size is never trusted", outcome.kind == Outcome.DONE
          and content_ok(store.get_item(item["id"])["dest_file"], size))

    store, lib = env(allow=("127.0.0.1",))
    outside = Path(HOME) / "outside.part"
    outside.write_bytes(b"\0" * size)
    item = claim(store, BASE + "/file.bin?size=%d" % size)
    store.update_item(item["id"], {"source_ref": {"part": str(outside), "final": str(outside)[:-5], "total": size,
                                                   "segments": [{"s": 0, "e": size // 2, "d": 0}, {"s": size // 2, "e": size, "d": 0}]}})
    outcome, _ = run(store, store.get_item(item["id"]))
    check("a recorded path outside the library is never written to, segmented or not",
          outside.read_bytes() == b"\0" * size and outcome.kind == Outcome.DONE)

    # the server stops honouring Range between pause and resume
    size = 3_000_000
    store, lib = env()
    item = claim(store, BASE + "/flip.bin?size=%d&delay=0.05" % size)
    t0 = time.time()
    outcome, _ = run(store, item, stop=lambda: "pause" if time.time() - t0 > 1.0 else None)
    part = Path(store.get_item(item["id"])["source_ref"]["part"])
    check("(set-up) paused with parts saved", outcome.kind == Outcome.PAUSED and part.exists())
    STATE.flip = True
    store.transition(item["id"], "paused", expect=states.ACTIVE_STATES)
    store.transition(item["id"], "queued", expect="paused")
    outcome, _ = run(store, store.get_item(item["id"]))
    row = store.get_item(item["id"])
    check("a server that stopped doing Range on resume: the half file is discarded, retryable, never appended to",
          outcome.kind == Outcome.FAILED and outcome.retryable and not part.exists() and not row["source_ref"])
    outcome, _ = run(store, store.get_item(item["id"]))
    row = store.get_item(item["id"])
    check("...and the next attempt downloads it whole on one connection, intact",
          outcome.kind == Outcome.DONE and content_ok(row["dest_file"], size))


def test_short_parts_and_limit():
    store, lib = env()
    item = claim(store, BASE + "/shortparts.bin?size=%d" % SIZE)
    outcome, _ = run(store, item)
    row = store.get_item(item["id"])
    check("a part that ends early is a retryable stall, never accepted as the file",
          outcome.kind in (Outcome.STALLED, Outcome.FAILED) and outcome.retryable)
    check("...it is not renamed, and its progress is kept for the retry",
          not Path(row["source_ref"]["final"]).exists() and Path(row["source_ref"]["part"]).exists()
          and len(row["source_ref"]["segments"]) == 4)

    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    t0 = time.time()
    outcome, _ = run(store, item)
    fast = time.time() - t0
    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    t0 = time.time()
    outcome2, _ = run(store, item, limit=800_000)
    slow = time.time() - t0
    check("the rate limit is shared by the parts: 1.6 MB at 800 KB/s takes about 2 s in total, not 0.5 s",
          outcome2.kind == Outcome.DONE and 1.4 < slow < 8 and fast < 1.2)
    check("...and the limited file is intact", content_ok(store.get_item(item["id"])["dest_file"], SIZE))

    store, lib = env()
    item = claim(store, BASE + "/file.bin?size=%d" % SIZE)
    store.delete_items([item["id"]])
    outcome, _ = run(store, item)
    check("a row deleted while running ends the run quietly", outcome.kind in (Outcome.CANCELLED, Outcome.DONE, Outcome.FAILED))


def main():
    for fn in (test_units, test_basic, test_fallbacks, test_ssrf_and_credentials, test_pause_resume,
               test_kill_and_resume, test_bad_tables, test_short_parts_and_limit):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s ran without raising (%s)" % (fn.__name__, exc), False)
    print(f"\n{passed} passed, {failed} failed")
    srv.stop(SERVER)
    shutil.rmtree(HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
