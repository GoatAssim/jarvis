"""Download Hub, phase 3: the yt-dlp engine, its routing, `dl_add`'s video options and
`dl_import_legacy`.  No network, no yt-dlp and no ffmpeg needed: the engine's worker process
is replaced by a small fake script that behaves like `ytdl_download` (writes files into the
`output_dir` it is given and prints the result line).

Covers: the router (YouTube and site-specific extractors go to ytdlp; a direct file link on such
a site and any other link stay with HTTP; forcing ytdlp on an unclaimed link is refused; the
engine not being ready is said plainly); option validation; the engine (files land in the right
Video / Audio / sub-folder and nowhere else, never overwrite, the private work folder is gone
afterwards, a stray work folder outside the library is ignored, errors are classified into
retryable and permanent, a crashed helper is retryable, a stall is a stall, pause keeps the
.part and resume continues from it, cancel removes only its own folder, the rate limit reaches
the helper); the daemon running a yt-dlp row end to end; service.add_items with video options
(stored only on yt-dlp rows, part of the dedupe key, bad values refused, ignored on file links);
cancelling a queued row cleans its folder; and the legacy import (read-only rows, idempotent,
Open / Reveal only).

Run: python tests/test_hub_ytdlp.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-ytdlp-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import engines, legacy, router, service, states  # noqa: E402
from jarvis.hub.daemon import HubDaemon  # noqa: E402
from jarvis.hub.engines import ytdlp  # noqa: E402
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


ARGS_LOG = Path(HOME) / "worker-args.jsonl"
FAKE = Path(HOME) / "fake_worker.py"
FAKE.write_text(r'''
import json, os, sys, time
MARK = "@@JARVIS_YTDL_RESULT@@"
args = json.loads(sys.stdin.read())
with open(os.environ["FAKE_ARGS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
out = args["output_dir"]
url = args["url"]

def result(payload):
    print("some noise yt-dlp would never print")
    print(MARK + " " + json.dumps(payload))
    sys.exit(0)

if url.endswith("/ok") or url.endswith("/audio"):
    audio = url.endswith("/audio")
    name = "Some Title [abc123]." + ("mp3" if audio else "mp4")
    with open(os.path.join(out, name), "wb") as f:
        f.write(b"x" * 5000)
    with open(os.path.join(out, "Some Title [abc123].en.srt"), "w") as f:
        f.write("1\n")
    with open(os.path.join(out, "leftover.f137.mp4.part"), "wb") as f:
        f.write(b"half")
    result({"ok": True, "files": [{"file": name, "title": "Some Title", "bytes": 5000}], "count": 1})
if url.endswith("/private"):
    result({"ok": False, "error": "ERROR: Video unavailable. This video is private"})
if url.endswith("/net"):
    result({"ok": False, "error": "Unable to download webpage: <urlopen error timed out>"})
if url.endswith("/limited"):
    result({"ok": False, "error": "HTTP Error 429: Too Many Requests"})
if url.endswith("/weird"):
    result({"ok": False, "error": "something nobody has seen before"})
if url.endswith("/crash"):
    sys.stderr.write("Traceback...\nSegfault-ish\n")
    sys.exit(3)
if url.endswith("/resume"):
    part = os.path.join(out, "Resumed [r1].mp4.part")
    if os.path.exists(part):
        os.replace(part, os.path.join(out, "Resumed [r1].mp4"))
        result({"ok": True, "files": [{"file": "Resumed [r1].mp4", "title": "Resumed"}]})
    with open(part, "wb") as f:
        f.write(b"y" * 4000)
    time.sleep(60)
if url.endswith("/slow"):
    n = 0
    while True:
        with open(os.path.join(out, "slow.mp4.part"), "ab") as f:
            f.write(b"z" * 2000)
        time.sleep(0.1)
if url.endswith("/hang"):
    time.sleep(60)
if url.endswith("/two"):
    for name in ("Part One [p1].mp4", "Part Two [p2].mp4"):
        with open(os.path.join(out, name), "wb") as f:
            f.write(b"q" * 100)
    result({"ok": True, "files": [{"file": "Part One [p1].mp4", "title": "Part One"}, {"file": "Part Two [p2].mp4", "title": "Part Two"}]})
result({"ok": False, "error": "unknown fake url"})
''')
os.environ["FAKE_ARGS_LOG"] = str(ARGS_LOG)
ytdlp.worker_command_override = [sys.executable, str(FAKE)]
ytdlp.ENGINE.available = lambda: (True, "")            # no yt-dlp / ffmpeg needed here


def worker_calls():
    if not ARGS_LOG.exists():
        return []
    return [json.loads(line) for line in ARGS_LOG.read_text().splitlines() if line.strip()]


def env(**more):
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    base = {"library_root": str(lib)}
    base.update(more)
    store.set_settings(base)
    return store, lib


def claim(store, url, **options):
    item = store.add_item(engine="ytdlp", source_url="https://video.example/" + url, title="t", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None, limit=0, stall=None):
    settings = store.get_settings()
    if stall is not None:
        settings["ytdlp"] = {"stall_seconds": stall}
    ctx = EngineContext(store, item, settings, stop_reason=stop or (lambda: None), rate_limit_bps=lambda: limit)
    return ytdlp.ENGINE.run(item, ctx)


def workdirs(lib):
    return [p for p in lib.rglob("*") if p.is_dir() and p.name.startswith(ytdlp.WORK_PREFIX)]


# --------------------------------------------------------------------------- routing

def test_routing():
    check("the yt-dlp engine is discovered", engines.get("ytdlp") is ytdlp.ENGINE and "ytdlp" not in engines.LOAD_ERRORS)
    check("the worker script is not mistaken for an engine", "_ytdlp_worker" not in engines.names()
          and "_ytdlp_worker" not in engines.LOAD_ERRORS)
    for url in ("https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://m.youtube.com/watch?v=abc",
                "https://music.youtube.com/watch?v=abc"):
        r = router.route(url)
        check("YouTube: %s -> ytdlp" % url, r["ok"] and r["engine"] == "ytdlp" and "YouTube" in r["reason"] and r["kind"] == "video")
    check("a direct file link is still the HTTP engine's", router.route("https://example.com/a.zip")["engine"] == "http")
    check("a plain page nothing claims is still the HTTP engine's (as before)", router.route("https://example.com/page")["engine"] == "http")

    class FakeIE:
        IE_NAME = "fakesite"

        @staticmethod
        def ie_key():
            return "FakeSite"

        @staticmethod
        def suitable(url):
            return "fakesite.example" in url

    ytdlp.EXTRACTOR_SOURCE = lambda: [FakeIE]
    r = router.route("https://fakesite.example/watch/123")
    check("a site yt-dlp has an extractor for goes to ytdlp", r["ok"] and r["engine"] == "ytdlp" and "fakesite" in r["reason"])
    check("...but a direct media file on that site stays with HTTP", router.route("https://fakesite.example/clip.mp4")["engine"] == "http")
    check("...and so does an archive", router.route("https://fakesite.example/pack.zip")["engine"] == "http")
    check("another site is untouched", router.route("https://other.example/watch/1")["engine"] == "http")
    forced = router.route("https://other.example/watch/1", engine="ytdlp")
    check("forcing ytdlp on a link it doesn't recognise is refused (no generic extractor)",
          not forced["ok"] and "can't take" in forced["problem"])
    check("forcing ytdlp on a claimed link works", router.route("https://fakesite.example/watch/1", engine="ytdlp")["ok"])
    check("an http link forced to http still works", router.route("https://fakesite.example/watch/1", engine="http")["engine"] == "http")
    ytdlp.EXTRACTOR_SOURCE = lambda: [type("Broken", (), {"ie_key": staticmethod(lambda: "Broken"),
                                                          "suitable": staticmethod(lambda u: 1 / 0)})]
    check("an extractor that raises never breaks routing", router.route("https://example.com/page")["engine"] == "http")
    ytdlp.EXTRACTOR_SOURCE = None

    ytdlp.ENGINE.available = lambda: (False, "yt-dlp isn't installed")
    r = router.route("https://www.youtube.com/watch?v=abc")
    check("with yt-dlp missing a YouTube link says what is missing, and isn't sent to HTTP",
          not r["ok"] and r["engine"] == "ytdlp" and "yt-dlp isn't installed" in r["problem"])
    ytdlp.ENGINE.available = lambda: (True, "")

    a = router.dedupe_key("ytdlp", "https://x/y", {"mode": "audio"})
    b = router.dedupe_key("ytdlp", "https://x/y", {"quality": "1080p"})
    c = router.dedupe_key("ytdlp", "https://x/y", {"mode": "audio", "limit_kbps": 50})
    check("the same video as audio and as 1080p video are two downloads", a != b)
    check("a speed cap or notification choice doesn't make a new download", a == c)


# --------------------------------------------------------------------------- options

def test_options():
    ok, err = ytdlp.normalize_options({"mode": "AUDIO", "audio_format": "FLAC", "quality": " 1080p ", "embed_subs": 1, "junk": 5})
    check("valid options are cleaned (case, spaces, booleans) and junk dropped",
          err is None and ok == {"mode": "audio", "audio_format": "flac", "quality": "1080p", "embed_subs": True})
    for bad, word in (({"mode": "movie"}, "mode"), ({"container": "gif"}, "container"), ({"video_codec": "x265"}, "video_codec"),
                      ({"audio_format": "wma"}, "audio_format"), ({"quality": "a\nb"}, "quality"), ({"sub_langs": "x" * 300}, "sub_langs")):
        out, err = ytdlp.normalize_options(bad)
        check("a bad value is an error, not silently dropped: %s" % list(bad)[0], out == {} and err and word in err)
    check("nothing at all is fine", ytdlp.normalize_options(None) == ({}, None))
    check("extract_options keeps only the yt-dlp keys", ytdlp.extract_options({"mode": "audio", "subdir": "X", "limit_kbps": 5}) == {"mode": "audio"})
    check("playlist / output_dir / cookies are not passable", not ({"playlist", "output_dir", "cookies"} & set(ytdlp.PASS_KEYS)))
    check("classify: private video is permanent", ytdlp.classify_error("ERROR: Video unavailable. This video is private") == (False, None))
    check("classify: a timeout is retryable", ytdlp.classify_error("Unable to download webpage: <urlopen error timed out>") == (True, None))
    check("classify: HTTP 429 is retryable with a longer wait", ytdlp.classify_error("HTTP Error 429: Too Many Requests") == (True, 60.0))
    check("classify: an unknown message isn't retried in a loop", ytdlp.classify_error("never seen") == (False, None))
    check("classify: ffmpeg trouble is permanent", ytdlp.classify_error("ffmpeg isn't installed or not on PATH")[0] is False)


# --------------------------------------------------------------------------- the engine

def test_engine_success():
    store, lib = env()
    item = claim(store, "ok")
    outcome = run(store, item)
    row = store.get_item(item["id"])
    final = Path(row["dest_file"])
    check("a download finishes", outcome.kind == Outcome.DONE and outcome.dest_file == str(final))
    check("a video lands in <library>/Video", final == lib / "Video" / "Some Title [abc123].mp4" and final.read_bytes() == b"x" * 5000)
    check("the subtitle sidecar came along", (lib / "Video" / "Some Title [abc123].en.srt").exists())
    check("a half-finished fragment is NOT moved into the library", not list(lib.rglob("*.part")))
    check("the private work folder is gone", workdirs(lib) == [])
    check("the row learned its real title and the size of what was saved", row["title"] == "Some Title" and row["bytes_total"] == 5000 + 2)
    check("the row's source_ref is cleared once it is finished", not row["source_ref"])
    call = worker_calls()[-1]
    check("the helper was given the url, the private output_dir and the options",
          call["url"] == "https://video.example/ok" and Path(call["output_dir"]).name.startswith(ytdlp.WORK_PREFIX))
    check("the playlist option is never passed", "playlist" not in call)

    item2 = claim(store, "ok")
    outcome2 = run(store, item2)
    check("a second download of the same name never overwrites",
          Path(outcome2.dest_file).name == "Some Title [abc123] (1).mp4" and final.read_bytes() == b"x" * 5000)

    store2, lib2 = env()
    audio = claim(store2, "audio", mode="audio", audio_format="mp3")
    run(store2, audio)
    row = store2.get_item(audio["id"])
    check("audio goes to <library>/Audio and the row is audio", Path(row["dest_file"]).parent == lib2 / "Audio" and row["kind"] == "audio")
    sub = claim(store2, "ok", subdir="Manuals/Cooking")
    run(store2, sub)
    check("a per-download sub-folder is honoured", Path(store2.get_item(sub["id"])["dest_file"]).parent == lib2 / "Manuals" / "Cooking")
    bad = claim(store2, "ok", subdir="../../etc")
    out = run(store2, bad)
    check("a sub-folder that escapes the library is refused", out.kind == Outcome.FAILED and "destination" in out.error and not out.retryable)

    two = claim(store2, "two")
    out = run(store2, two)
    row = store2.get_item(two["id"])
    check("several files: all are saved, the first is the row's file, the row says how many",
          out.kind == Outcome.DONE and Path(row["dest_file"]).name == "Part One [p1].mp4"
          and (lib2 / "Video" / "Part Two [p2].mp4").exists() and "2 files" in (row["message"] or ""))

    store3, lib3 = env()
    item = claim(store3, "ok", mode="audio", quality="id:140", container="mkv", limit_kbps=100, subdir="Z")
    run(store3, item, limit=2048 * 1024)
    call = worker_calls()[-1]
    check("the options reach the helper, the per-download extras don't",
          call["mode"] == "audio" and call["quality"] == "id:140" and call["container"] == "mkv"
          and "limit_kbps" not in call and "subdir" not in call)
    check("the rate limit reaches the helper in KB/s", call.get("rate_limit_kbps") == 2048)


def test_engine_errors():
    store, lib = env()
    out = run(store, claim(store, "private"))
    check("a private video fails for good, with yt-dlp's words", out.kind == Outcome.FAILED and not out.retryable and "private" in out.error)
    out = run(store, claim(store, "net"))
    check("a network error is retryable", out.kind == Outcome.FAILED and out.retryable)
    out = run(store, claim(store, "limited"))
    check("rate limiting by the site is retryable after a longer wait", out.retryable and out.delay_s == 60.0)
    out = run(store, claim(store, "weird"))
    check("an unknown error is not retried forever", out.kind == Outcome.FAILED and not out.retryable)
    out = run(store, claim(store, "crash"))
    check("a helper that dies without an answer is a retryable failure naming the exit code and the last stderr line",
          out.kind == Outcome.FAILED and out.retryable and "exit 3" in out.error and "Segfault-ish" in out.error)
    check("a failed download leaves no file in the library", [p for p in lib.rglob("*") if p.is_file()] == [])

    item = claim(store, "hang")
    t0 = time.time()
    out = run(store, item, stall=1)
    check("no growth for the stall time is a stall (and the helper is killed)",
          out.kind == Outcome.STALLED and out.retryable and time.time() - t0 < 15)

    ytdlp.ENGINE.available = lambda: (False, "ffmpeg isn't installed or not on PATH")
    out = run(store, claim(store, "ok"))
    check("with the engine not ready the run fails clearly and is not retried", out.kind == Outcome.FAILED and not out.retryable and "ffmpeg" in out.error)
    ytdlp.ENGINE.available = lambda: (True, "")

    out = run(store, claim(store, "ok", mode="movie"))
    check("a bad stored option fails the run instead of guessing", out.kind == Outcome.FAILED and "mode" in out.error)


def test_pause_resume_cancel():
    store, lib = env()
    item = claim(store, "resume")
    t0 = time.time()
    out = run(store, item, stop=lambda: "pause" if time.time() - t0 > 1.2 else None)
    row = store.get_item(item["id"])
    work = Path(row["source_ref"]["work_dir"])
    check("pause stops the helper and keeps the .part and the work folder",
          out.kind == Outcome.PAUSED and (work / "Resumed [r1].mp4.part").exists())
    check("the work folder is recorded so the next attempt finds it", work.parent == lib / "Video" and work.name.startswith(ytdlp.WORK_PREFIX))
    before = len(worker_calls())
    store.transition(item["id"], "paused", expect=states.ACTIVE_STATES)
    store.transition(item["id"], "queued", expect="paused")
    again = store.claim_next({"default": 100}, 100)
    out = run(store, again)
    calls = worker_calls()[before:]
    check("resuming runs the helper in the SAME folder, so yt-dlp continues the .part",
          out.kind == Outcome.DONE and len(calls) == 1 and calls[0]["output_dir"] == str(work))
    check("the resumed file is in the library and the work folder is gone",
          (lib / "Video" / "Resumed [r1].mp4").exists() and not work.exists())

    slow = claim(store, "slow")
    t0 = time.time()
    out = run(store, slow, stop=lambda: "cancel" if time.time() - t0 > 1.2 else None)
    row = store.get_item(slow["id"])
    work = Path(row["source_ref"]["work_dir"])
    check("cancel stops the helper", out.kind == Outcome.CANCELLED)
    check("while running it reported live size and speed (no percent)",
          row["bytes_done"] and row["bytes_done"] > 0 and row["percent"] is None)
    # the daemon's cancel path (service._discard_part) removes the work folder
    precious = lib / "Video" / "precious.mp4"
    precious.write_bytes(b"keep")
    service._discard_part(store, row)
    check("cancelling removes this download's own folder", not work.exists())
    check("...and nothing else in the library", precious.exists())

    outside = Path(HOME) / "elsewhere" / (ytdlp.WORK_PREFIX + "x")
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("keep")
    check("a work folder outside the library is never deleted", ytdlp.remove_work_dir(outside, [lib]) is False and outside.exists())
    rootish = lib / "Video"
    check("the destination folder itself is never deleted", ytdlp.remove_work_dir(rootish, [lib]) is False and rootish.exists())
    notmine = lib / "Video" / "Other folder"
    notmine.mkdir()
    check("a folder this engine didn't name is never deleted", ytdlp.remove_work_dir(notmine, [lib]) is False and notmine.exists())

    forged = claim(store, "ok")
    store.update_item(forged["id"], {"source_ref": {"work_dir": str(outside), "dest_dir": str(outside.parent)}})
    forged = store.get_item(forged["id"])
    out = run(store, forged)
    check("a recorded folder outside the library is not trusted: the file still lands inside it",
          out.kind == Outcome.DONE and Path(out.dest_file).parent == lib / "Video" and (outside / "keep.txt").exists())


# --------------------------------------------------------------------------- the daemon end to end

def test_daemon():
    store, lib = env()
    daemon = HubDaemon(store)
    daemon.start_up()
    a = store.add_item(engine="ytdlp", source_url="https://video.example/ok", title="t", options={})[0]
    b = store.add_item(engine="ytdlp", source_url="https://video.example/private", title="t", options={})[0]
    end = time.time() + 30
    while time.time() < end:
        daemon.tick()
        if all(store.get_item(i["id"])["state"] in ("done", "failed") for i in (a, b)):
            break
        time.sleep(0.05)
    ra, rb = store.get_item(a["id"]), store.get_item(b["id"])
    check("the daemon runs a yt-dlp row to done", ra["state"] == "done" and Path(ra["dest_file"]).exists() and ra["percent"] == 100)
    check("and a permanent error to failed without retrying", rb["state"] == "failed" and rb["attempts"] == 1 and "private" in rb["error"])
    daemon.stop(5)


# --------------------------------------------------------------------------- service

def test_service():
    store, lib = env()
    res = service.add_items(["https://www.youtube.com/watch?v=abc"], engine_options={"mode": "audio", "audio_format": "opus"}, store=store)
    row = store.get_item(res["added"][0]["id"])
    check("add_items queues a YouTube link on the ytdlp engine with the video options stored",
          res["ok"] and row["engine"] == "ytdlp" and row["options"]["mode"] == "audio" and row["options"]["audio_format"] == "opus")
    check("an audio request makes an audio row and the row has a readable title", row["kind"] == "audio" and row["title"] == "Video from www.youtube.com")
    again = service.add_items(["https://www.youtube.com/watch?v=abc"], engine_options={"mode": "audio", "audio_format": "opus"}, store=store)
    check("the same link with the same options is a duplicate", again["added"][0]["duplicate"] is True)
    other = service.add_items(["https://www.youtube.com/watch?v=abc"], engine_options={"quality": "720p"}, store=store)
    check("the same link with different options is a new download", other["added"][0]["duplicate"] is False)
    bad = service.add_items(["https://www.youtube.com/watch?v=abc"], engine_options={"container": "gif"}, store=store)
    check("a bad option queues nothing and says why", bad["ok"] is False and "container" in bad["error"])
    mixed = service.add_items(["http://93.184.216.34/a.zip"], engine_options={"quality": "720p"}, store=store)
    check("video options on a file link are ignored and the answer says so",
          mixed["ok"] and store.get_item(mixed["added"][0]["id"])["options"].get("quality") is None
          and "ignored" in mixed.get("note", ""))
    dry = service.preview("https://www.youtube.com/watch?v=abc", store=store)
    check("preview names the yt-dlp engine", dry["ok"] and dry["engine"] == "ytdlp")

    # a queued row with a leftover work folder: cancelling it cleans the folder
    queued = res["added"][0]["id"]
    work = lib / "Audio" / (ytdlp.WORK_PREFIX + queued)
    work.mkdir(parents=True)
    (work / "x.opus.part").write_bytes(b"zz")
    store.update_item(queued, {"source_ref": {"work_dir": str(work), "dest_dir": str(work.parent)}})
    out = service.control("cancel", id=queued, store=store)
    check("cancelling a waiting yt-dlp row removes its work folder", out["changed"] == [queued] and not work.exists())


def test_control_reports_failure():
    store, lib = env()
    item = store.add_item(engine="ytdlp", source_url="https://video.example/ok", title="t")[0]
    store.transition(item["id"], "probing")
    store.transition(item["id"], "done")
    res = service.control("cancel", id=item["id"], store=store)
    check("a control call that can't change a single named download says ok: false",
          res["ok"] is False and "already done" in res["error"] and res["skipped"])


# --------------------------------------------------------------------------- legacy import

def test_legacy():
    store, lib = env()
    root = Path(HOME) / ".jarvis" / "downloads"
    check("nothing to import is a clear, successful answer", legacy.import_legacy(store)["found"] == 0)
    for name, files in (("dl_20260101_120000_aaaaaa", ["First [a1].mp4"]),
                        ("dl_20260102_120000_bbbbbb", ["Song [b1].mp3", "Other [b2].mkv"]),
                        ("notours", ["Skip [c1].mp4"])):
        (root / name).mkdir(parents=True, exist_ok=True)
        for f in files:
            (root / name / f).write_bytes(b"m" * 300)
    (root / "dl_20260101_120000_aaaaaa" / "notes.txt").write_text("not media")
    res = legacy.import_legacy(store)
    check("it finds every media file in the dl_* folders and nothing else", res["ok"] and res["found"] == 3 and res["imported"] == 3)
    rows = store.list_items(provider="ytdl_legacy", limit=50)
    check("they are provider rows, done, with their size", len(rows) == 3 and all(r["state"] == "done" and r["bytes_total"] == 300 for r in rows))
    check("kinds follow the file", sorted(r["kind"] for r in rows) == ["audio", "video", "video"])
    again = legacy.import_legacy(store)
    check("running it again adds nothing", again["imported"] == 0 and again["already"] == 3 and len(store.list_items(provider="ytdl_legacy", limit=50)) == 3)
    shown = service.list_items(provider="ytdl_legacy", store=store)["items"]
    check("they are read-only: Open and Reveal only, no pause / cancel / retry",
          all(set(r["capabilities"]) == {"open", "reveal"} for r in shown))
    done_ids = {r["id"] for r in shown}
    check("a bulk pause / cancel never touches them", service.control("cancel", all=True, store=store)["ok"]
          and {r["id"] for r in store.list_items(provider="ytdl_legacy", limit=50)} == done_ids)
    opened = []
    service.LAUNCH = lambda path, action: opened.append((str(path), action)) or True
    res = service.open_item(shown[0]["id"], "reveal", store=store)
    check("Open works through the provider's open_roots", res["ok"] and opened and opened[0][0].startswith(str(root)))
    service.LAUNCH = service._default_launch
    shutil.rmtree(root / "dl_20260101_120000_aaaaaa")
    check("a folder the old tool pruned leaves a row that says the file is gone",
          "no longer there" in service.open_item(next(r["id"] for r in shown if "First" in r["title"]), "open", store=store).get("error", ""))
    old = legacy.import_legacy(store)
    check("re-importing after a prune doesn't remove history", old["ok"] and len(store.list_items(provider="ytdl_legacy", limit=50)) == 3)


def test_background():
    """ytdl_download(background=True) queues in the hub instead of downloading and waiting."""
    from jarvis import ytdl_tools
    hub = Store()                                   # the default store under the test HOME
    before = len(hub.list_items(limit=2000))
    res = ytdl_tools.ytdl_download({"url": "https://www.youtube.com/watch?v=bgtest1", "background": True,
                                    "mode": "audio", "audio_format": "opus", "rate_limit_kbps": 500})
    check("background=true queues at once and says so", res.get("ok") and res["queued"] and res["background"] and res["id"])
    row = hub.get_item(res["id"])
    check("it is a hub yt-dlp row carrying the options", row["engine"] == "ytdlp" and row["options"]["mode"] == "audio"
          and row["options"]["audio_format"] == "opus" and row["kind"] == "audio")
    check("the speed cap came along", row["options"].get("limit_kbps") == 500)
    check("the answer tells the model how to follow it", "dl_status" in res["message"])
    again = ytdl_tools.ytdl_download({"url": "https://www.youtube.com/watch?v=bgtest1", "background": True,
                                      "mode": "audio", "audio_format": "opus", "rate_limit_kbps": 500})
    check("the same request again is reported as already there", again.get("duplicate") is True and again["id"] == res["id"]
          and len(hub.list_items(limit=2000)) == before + 1)
    pl = ytdl_tools.ytdl_download({"url": "https://www.youtube.com/playlist?list=x", "background": True, "playlist": True})
    check("a playlist with background is refused, not silently shrunk", "error" in pl and "playlist" in pl["error"])
    od = ytdl_tools.ytdl_download({"url": "https://www.youtube.com/watch?v=bgtest2", "background": True, "output_dir": "Somewhere"})
    check("a custom output_dir with background is refused", "error" in od and "output_dir" in od["error"])
    bad = ytdl_tools.ytdl_download({"url": "https://www.youtube.com/watch?v=bgtest3", "background": True, "container": "gif"})
    check("a bad option is reported and nothing is queued", "error" in bad and "container" in bad["error"])
    nope = ytdl_tools.ytdl_download({"url": "https://nothing-claims-this.example/page", "background": True})
    check("a page yt-dlp can't take says so and suggests leaving background off", "error" in nope and "background off" in nope["error"])
    check("none of the refusals queued anything", len(hub.list_items(limit=2000)) == before + 1)
    plain = ytdl_tools.ytdl_download({"url": "https://www.youtube.com/watch?v=x", "mode": "movie"})
    check("without background the old blocking path is untouched", "error" in plain and "unknown mode" in plain["error"]
          and len(hub.list_items(limit=2000)) == before + 1)
    schema = next(t for t in ytdl_tools.YTDL_TOOL_SCHEMAS if t["name"] == "ytdl_download") \
        if hasattr(ytdl_tools, "YTDL_TOOL_SCHEMAS") else None
    if schema is not None:
        check("the schema offers background as a boolean", schema["parameters"]["properties"]["background"]["type"] == "boolean")


def main():
    for fn in (test_routing, test_options, test_engine_success, test_engine_errors, test_pause_resume_cancel,
               test_daemon, test_service, test_control_reports_failure, test_legacy, test_background):
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
