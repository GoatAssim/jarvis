"""Download Hub, phase 8: yt-dlp Level B (real progress, clean cancel).

Three layers:

  1. ProgressModel on synthetic events (no processes): ONE bar for video + audio, exact vs
     estimated totals, unknown sizes stay indeterminate, post-processing phases, garbage in.
  2. The engine against a scripted fake worker: what the row says while it runs (percent,
     speed, ETA, file name, "≈" flags, downloading -> postprocessing with a labelled phase), a
     polite stop that keeps the .part, a worker that ignores the stop being killed after the
     grace time, post-processing being killed at once, a stale stop file never stopping the next
     attempt, garbled lines being skipped.
  3. The REAL path, skipped when yt-dlp or ffmpeg is missing: the real engine, the real worker,
     real yt-dlp and a real ffmpeg merge, against a local server (tests/_hub_ytdlp_media.py).

Run: python tests/test_hub_ytdlp_level_b.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-levelb-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.hub import states  # noqa: E402
from jarvis.hub.engines import _ytdlp_worker as worker_mod  # noqa: E402
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


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def dl(file, done, total=None, est=None, speed=None, status="downloading", title=None):
    return {"k": "dl", "status": status, "file": file, "done": done, "total": total, "est": est,
            "speed": speed, "eta": None, "title": title}


def stream(i, size=None, approx=None, tbr=None, v=False, a=False):
    return {"id": i, "size": size, "approx": approx, "tbr": tbr, "v": v, "a": a}


# =========================================================================== 1. the model

def test_model():
    check("worker and engine speak the same markers",
          worker_mod.MARKER == ytdlp.MARKER and worker_mod.PROGRESS_MARKER == ytdlp.PROGRESS_MARKER
          and worker_mod.STOP_FILE == ytdlp.STOP_FILE)

    # one file, exact size
    clk = Clock()
    m = ytdlp.ProgressModel({}, clock=clk)
    m.feed(dl("a.mp4", 1000, total=4000, speed=2000.0, title="T"))
    s = m.snapshot()
    check("one exact file: bytes, total, speed and an exact ETA", s["bytes_done"] == 1000 and s["bytes_total"] == 4000
          and s["estimated"] is False and s["speed_bps"] == 2000.0 and s["eta_s"] == 1.5)
    check("a single plain file shows no phase", s["phase"] is None and s["phase_index"] is None)
    check("the file name and title come through", s["message"] == "a.mp4" and s["title"] == "T")

    # video + audio, sizes announced up front: one bar for both
    clk = Clock()
    m = ytdlp.ProgressModel({}, clock=clk)
    m.feed({"k": "plan", "streams": [stream("v", size=6000, v=True), stream("a", size=2000, a=True)],
            "title": "Plan title", "duration": 10})
    check("the plan gives the title before any bytes move", m.snapshot()["title"] == "Plan title")
    m.feed(dl("x.fv.mp4", 3000, total=6000, speed=1000.0))
    s = m.snapshot()
    check("video first: 3000 of 8000, phase 'downloading video 1/3' (merge counts as a step)",
          s["bytes_done"] == 3000 and s["bytes_total"] == 8000 and not s["estimated"]
          and s["phase"] == "downloading video" and (s["phase_index"], s["phase_count"]) == (1, 3))
    m.feed(dl("x.fv.mp4", 6000, total=6000, status="finished"))
    s = m.snapshot()
    check("the video finishing is 75 %, not 100 %", s["bytes_done"] == 6000 and s["bytes_total"] == 8000)
    m.feed(dl("x.fa.m4a", 500, total=2000, speed=800.0))
    s = m.snapshot()
    check("then the audio continues the same bar", s["bytes_done"] == 6500 and s["bytes_total"] == 8000
          and s["phase"] == "downloading audio" and s["phase_index"] == 2)
    m.feed(dl("x.fa.m4a", 2000, total=2000, status="finished"))
    m.feed({"k": "pp", "status": "started", "pp": "Merger"})
    s = m.snapshot()
    check("merging is its own phase at 100 % with a label and step number",
          s["state"] == "postprocessing" and s["phase"] == "merging video and audio"
          and (s["phase_index"], s["phase_count"]) == (3, 3) and s["bytes_done"] == s["bytes_total"] == 8000
          and s["speed_bps"] is None)
    m.feed({"k": "pp", "status": "started", "pp": "FFmpegMetadata"})
    s = m.snapshot()
    check("a step nobody predicted never shows 'step 4 of 3'", s["phase_index"] <= s["phase_count"]
          and s["phase"] == "writing metadata")

    # announced sizes that are estimates -> estimated flag
    m = ytdlp.ProgressModel({}, clock=Clock())
    m.feed({"k": "plan", "streams": [stream("v", size=6000, v=True), stream("a", approx=2000, a=True)]})
    m.feed(dl("x.fv.mp4", 100, total=6000))
    check("an announced size that is only approximate makes the whole total an estimate", m.snapshot()["estimated"] is True)
    check("...and an estimate gets no ETA of the engine's own", m.snapshot()["eta_s"] is None)

    # fragments: yt-dlp's own running estimate
    m = ytdlp.ProgressModel({}, clock=Clock())
    m.feed(dl("h.mp4", 500, est=5000, speed=100.0))
    s = m.snapshot()
    check("a fragmented download uses yt-dlp's estimate and says so", s["bytes_total"] == 5000 and s["estimated"] is True)
    m.feed(dl("h.mp4", 6000, est=5500))
    check("an estimate that turns out too low never shows done > total", m.snapshot()["bytes_total"] >= 6000)

    # bitrate x length as a last resort
    m = ytdlp.ProgressModel({}, clock=Clock())
    m.feed({"k": "plan", "streams": [stream("hls", tbr=800.0, v=True, a=True)], "duration": 100})
    m.feed(dl("h.mp4", 10))
    s = m.snapshot()
    check("with no size at all, bitrate x duration gives a flagged estimate", s["bytes_total"] == 800 * 125 * 100 and s["estimated"])

    # nothing known: no number is invented
    m = ytdlp.ProgressModel({}, clock=Clock())
    m.feed({"k": "plan", "streams": [stream("v", size=6000, v=True), stream("a", v=False, a=True)]})
    m.feed(dl("x.fv.mp4", 100, total=6000))
    s = m.snapshot()
    check("one stream of unknown size: the total stays unknown (indeterminate bar), bytes still shown",
          s["bytes_total"] is None and s["bytes_done"] == 100)

    # speed goes stale between streams
    clk = Clock()
    m = ytdlp.ProgressModel({}, clock=clk)
    m.feed(dl("a", 10, total=100, speed=50.0))
    clk.t += ytdlp.SPEED_FRESH_S + 1
    check("an old speed is dropped instead of shown as current", m.snapshot()["speed_bps"] is None)

    # options predict extra steps
    check("audio mode: extraction is one step", ytdlp.expected_pp_steps({"mode": "audio"}, 1) == 1)
    check("merge + thumbnail + metadata + subs + sponsorblock add up",
          ytdlp.expected_pp_steps({"embed_thumbnail": True, "embed_metadata": True, "embed_subs": True,
                                   "sponsorblock_remove": "sponsor"}, 2) == 5)
    check("a plain single file needs none", ytdlp.expected_pp_steps({}, 1) == 0)
    m = ytdlp.ProgressModel({"embed_thumbnail": True}, clock=Clock())
    m.feed(dl("a.mp4", 10, total=100))
    s = m.snapshot()
    check("a single file with a post step still shows 1/2 while downloading", (s["phase_index"], s["phase_count"]) == (1, 2))
    check("every known post-processor has a readable label", ytdlp.pp_label("FFmpegExtractAudio") == "extracting audio"
          and ytdlp.pp_label("Merger") == "merging video and audio" and ytdlp.pp_label("Whatever") == "post-processing"
          and ytdlp.pp_label(None) == "post-processing")

    # garbage from the pipe
    m = ytdlp.ProgressModel({}, clock=Clock())
    for ev in (None, "x", [], {"k": "dl"}, {"k": "dl", "file": "a", "done": -5, "total": float("nan"), "speed": "fast"},
               {"k": "dl", "file": "a", "done": True, "total": 10 ** 30}, {"k": "plan", "streams": "nope"},
               {"k": "pp", "status": "started", "pp": 5}, {"k": "zzz"}):
        m.feed(ev)
    s = m.snapshot()
    check("garbage events never raise and never produce negative or non-finite numbers",
          s["bytes_done"] >= 0 and (s["bytes_total"] is None or s["bytes_total"] >= 0)
          and (s["speed_bps"] is None or s["speed_bps"] > 0))
    check("a hostile file name is cleaned to one short line",
          (lambda mm: (mm.feed(dl("evil\n\x00" + "x" * 500, 1, total=2)), len(mm.snapshot()["message"]) <= 200
                       and "\n" not in mm.snapshot()["message"])[1])(ytdlp.ProgressModel({}, clock=Clock())))


# =========================================================================== 2. fake worker

FAKE = Path(HOME) / "fake_level_b.py"
FAKE.write_text(r'''
import json, os, sys, time
MARK, PMARK, STOP = "@@JARVIS_YTDL_RESULT@@", "@@JARVIS_YTDL_PROGRESS@@", ".jarvis-ytdl-stop"
args = json.loads(sys.stdin.read())
out, url = args["output_dir"], args["url"]

def prog(ev):
    sys.stdout.write("\r[download] noise from yt-dlp\n" + PMARK + " " + json.dumps(ev) + "\n"); sys.stdout.flush()

def result(payload):
    print(MARK + " " + json.dumps(payload)); sys.stdout.flush(); sys.exit(0)

def stop_asked():
    p = os.path.join(out, STOP)
    return open(p).read().strip() if os.path.exists(p) else None

def dl(file, done, total, status="downloading", speed=2000.0):
    return {"k": "dl", "status": status, "file": file, "done": done, "total": total, "est": None, "speed": speed, "eta": 1, "title": None}

if url.endswith("/merge"):
    prog({"k": "plan", "streams": [{"id": "v", "size": 6000, "approx": None, "tbr": None, "v": True, "a": False},
                                    {"id": "a", "size": None, "approx": 2000, "tbr": None, "v": False, "a": True}],
          "title": "Merged Title", "duration": 10})
    for done in (1000, 3000, 5000):
        prog(dl("M.fv.mp4", done, 6000)); time.sleep(0.4)
    prog(dl("M.fv.mp4", 6000, 6000, "finished"))
    for done in (500, 1500):
        prog(dl("M.fa.m4a", done, 2000)); time.sleep(0.4)
    prog(dl("M.fa.m4a", 2000, 2000, "finished"))
    prog({"k": "pp", "status": "started", "pp": "Merger"}); time.sleep(1.2)
    prog({"k": "pp", "status": "finished", "pp": "Merger"})
    open(os.path.join(out, "M.mp4"), "wb").write(b"m" * 800)
    result({"ok": True, "files": [{"file": "M.mp4", "title": "Merged Title", "bytes": 800}]})

if url.endswith("/exact"):
    for done in (1000, 2000, 3000):
        prog(dl("E.mp4", done, 4000)); time.sleep(0.4)
    open(os.path.join(out, "E.mp4"), "wb").write(b"e" * 400)
    result({"ok": True, "files": [{"file": "E.mp4", "title": "E"}]})

if url.endswith("/polite") or url.endswith("/deaf"):
    n = 0
    while True:
        n += 1000
        with open(os.path.join(out, "P.mp4.part"), "ab") as f:
            f.write(b"p" * 1000)
        prog(dl("P.mp4", n, 100000))
        kind = None if url.endswith("/deaf") else stop_asked()
        if kind:
            result({"ok": False, "stopped": kind, "error": "stopped (%s)" % kind})
        time.sleep(0.1)

if url.endswith("/ppstuck"):
    prog({"k": "plan", "streams": [{"id": "v", "size": 100, "approx": None, "tbr": None, "v": True, "a": True}]})
    prog(dl("S.mp4", 100, 100, "finished"))
    prog({"k": "pp", "status": "started", "pp": "FFmpegExtractAudio"})
    time.sleep(60)

if url.endswith("/stopfile-resume"):
    if os.path.exists(os.path.join(out, "seen-stop")):
        open(os.path.join(out, "R.mp4"), "wb").write(b"r")
        result({"ok": True, "files": [{"file": "R.mp4", "title": "R"}]})
    open(os.path.join(out, "R.mp4.part"), "wb").write(b"r" * 10)
    for _ in range(100):
        prog(dl("R.mp4", 10, 100))
        if stop_asked():
            open(os.path.join(out, "seen-stop"), "w").write("1")
            result({"ok": False, "stopped": stop_asked()})
        time.sleep(0.1)

if url.endswith("/garbled"):
    sys.stdout.write(PMARK + " {not json\n" + PMARK + " [1,2]\n" + PMARK + " 7\n"); sys.stdout.flush()
    prog(dl("G.mp4", 50, 100)); time.sleep(0.7)
    open(os.path.join(out, "G.mp4"), "wb").write(b"g" * 100)
    result({"ok": True, "files": [{"file": "G.mp4", "title": "G"}]})

if url.endswith("/selfstop"):
    result({"ok": False, "stopped": "cancel", "error": "stopped (cancel)"})
result({"ok": False, "error": "unknown fake url"})
''')
ytdlp.worker_command_override = [sys.executable, str(FAKE)]
ytdlp.ENGINE.available = lambda: (True, "")


def env():
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    store.set_settings({"library_root": str(lib)})
    return store, lib


def claim(store, url, **options):
    item = store.add_item(engine="ytdlp", source_url="https://video.example/" + url if "://" not in url else url,
                          title="t", options=options)[0]
    claimed = store.claim_next({"default": 100}, 100)
    assert claimed and claimed["id"] == item["id"]
    return claimed


def run(store, item, stop=None, sample=False, stall=None):
    settings = store.get_settings()
    if stall is not None:
        settings["ytdlp"] = {"stall_seconds": stall}
    ctx = EngineContext(store, item, settings, stop_reason=stop or (lambda: None), rate_limit_bps=lambda: 0)
    rows, done = [], threading.Event()

    def sampler():
        while not done.is_set():
            rows.append(store.get_item(item["id"]))
            time.sleep(0.12)

    th = threading.Thread(target=sampler, daemon=True)
    if sample:
        th.start()
    t0 = time.time()
    outcome = ytdlp.ENGINE.run(item, ctx)
    elapsed = time.time() - t0
    done.set()
    if sample:
        th.join()
    return outcome, rows, elapsed


STOPS = []
_orig_stop_run = ytdlp.stop_run


def _spy(run_, work_dir, kind, grace=None):
    t = time.time()
    how = _orig_stop_run(run_, work_dir, kind, grace)
    STOPS.append((kind, how, time.time() - t))
    return how


ytdlp.stop_run = _spy


def test_engine_progress():
    store, lib = env()
    item = claim(store, "merge")
    outcome, rows, _ = run(store, item, sample=True)
    row = store.get_item(item["id"])
    check("the fake download finishes", outcome.kind == Outcome.DONE)
    dl_rows = [r for r in rows if r["state"] == "downloading" and r["bytes_total"]]
    check("the row got a determinate percent while downloading", any(r["percent"] and 0 < r["percent"] < 100 for r in dl_rows))
    check("one bar for video + audio: the total is both streams (6000 + 2000)", dl_rows and all(r["bytes_total"] == 8000 for r in dl_rows))
    check("the bar never hits 100 % while still downloading (no double 100 %)",
          all((r["percent"] or 0) < 99.9 for r in dl_rows))
    check("percent is shown with a '≈' because one size was only announced approximately",
          any("percent" in r["derived"] for r in dl_rows))
    check("speed from yt-dlp is the engine's own (no '≈' on it)", any(r["speed_bps"] for r in dl_rows)
          and not any("speed" in r["derived"] for r in dl_rows if r["speed_bps"]))
    check("the title was known before the file was done", any(r["title"] == "Merged Title" for r in rows if r["state"] != "done"))
    check("the file being written is shown", any(r["message"] and r["message"].startswith("M.f") for r in dl_rows))
    check("the video phase is labelled with its step", any(r["phase"] == "downloading video" and r["phase_index"] == 1
                                                         and r["phase_count"] == 3 for r in dl_rows))
    check("then the audio phase", any(r["phase"] == "downloading audio" and r["phase_index"] == 2 for r in dl_rows))
    pp = [r for r in rows if r["state"] == "postprocessing" and r["phase"] == "merging video and audio"]
    check("merging shows as its own postprocessing phase 3/3", pp and all((r["phase_index"], r["phase_count"]) == (3, 3) for r in pp))
    check("...at 100 %, so it is the phase label that tells it is alive", all(r["percent"] == 100 for r in pp))
    check("percent goes up, never down, while downloading",
          [r["percent"] for r in dl_rows] == sorted(r["percent"] for r in dl_rows))
    check("when done the note and phase are cleared and the file is saved",
          row["message"] is None and row["phase"] is None and Path(row["dest_file"]).name == "M.mp4"
          and Path(row["dest_file"]).parent == lib / "Video")

    store, lib = env()
    item = claim(store, "exact")
    outcome, rows, _ = run(store, item, sample=True)
    dl_rows = [r for r in rows if r["state"] == "downloading" and r["bytes_total"]]
    check("an exact size gives an exact percent, no '≈'", dl_rows and not any("percent" in r["derived"] for r in dl_rows))
    check("...and a single plain file shows no phase", all(r["phase"] is None for r in dl_rows))

    store, lib = env()
    item = claim(store, "garbled")
    outcome, rows, _ = run(store, item, sample=True)
    check("garbled progress lines are skipped; the download still works",
          outcome.kind == Outcome.DONE and any(r["percent"] == 50 for r in rows))


def test_engine_stop():
    # polite pause: the worker answers the stop file, the .part stays
    store, lib = env()
    item = claim(store, "polite")
    t0 = time.time()
    STOPS.clear()
    outcome, _, elapsed = run(store, item, stop=lambda: "pause" if time.time() - t0 > 0.8 else None)
    row = store.get_item(item["id"])
    work = Path(row["source_ref"]["work_dir"])
    check("pause is asked politely and the worker goes within a moment",
          outcome.kind == Outcome.PAUSED and STOPS and STOPS[-1][:2] == ("pause", "polite") and STOPS[-1][2] < 2.0)
    check("the .part is kept", (work / "P.mp4.part").exists() and (work / "P.mp4.part").stat().st_size >= 1000)
    check("the stop request is cleaned up", not (work / ytdlp.STOP_FILE).exists())
    check("the row shows the real position after the stop, not the last tick",
          row["bytes_done"] == (work / "P.mp4.part").stat().st_size)

    # polite cancel
    item = claim(store, "polite")
    t0 = time.time()
    STOPS.clear()
    outcome, _, _ = run(store, item, stop=lambda: "cancel" if time.time() - t0 > 0.8 else None)
    row = store.get_item(item["id"])
    check("cancel stops politely and removes the private folder",
          outcome.kind == Outcome.CANCELLED and STOPS[-1][:2] == ("cancel", "polite")
          and not Path(row["source_ref"]["work_dir"]).exists())
    check("a cancelled download leaves no file in the library (only the paused one's .part is there)",
          sorted(p.name for p in lib.rglob("*") if p.is_file()) == ["P.mp4.part"])

    # a worker that ignores the stop is killed after the grace time
    saved = ytdlp.GRACE_S
    ytdlp.GRACE_S = 0.7
    store, lib = env()
    item = claim(store, "deaf")
    t0 = time.time()
    STOPS.clear()
    outcome, _, elapsed = run(store, item, stop=lambda: "cancel" if time.time() - t0 > 0.8 else None)
    check("a worker that ignores the stop is KILLED after the grace time (the kill-based fallback)",
          outcome.kind == Outcome.CANCELLED and STOPS[-1][:2] == ("cancel", "killed") and 0.6 < STOPS[-1][2] < 5
          and elapsed < 10)
    check("...and the folder is still removed", not Path(store.get_item(item["id"])["source_ref"]["work_dir"]).exists())
    ytdlp.GRACE_S = saved

    # during post-processing there is no hook: kill at once, never wait the grace out
    ytdlp.GRACE_S = 30
    store, lib = env()
    item = claim(store, "ppstuck")
    t0 = time.time()
    STOPS.clear()
    outcome, _, elapsed = run(store, item, stop=lambda: "cancel" if time.time() - t0 > 1.3 else None)
    check("cancel while post-processing kills at once (no waiting for a hook that can't run)",
          outcome.kind == Outcome.CANCELLED and STOPS[-1][:2] == ("cancel", "killed") and STOPS[-1][2] < 5 and elapsed < 8)
    # a daemon shutdown is never polite
    item = claim(store, "polite")
    t0 = time.time()
    STOPS.clear()
    outcome, _, _ = run(store, item, stop=lambda: "shutdown" if time.time() - t0 > 0.6 else None)
    check("a daemon shutdown kills at once and the row is requeued",
          outcome.kind == Outcome.REQUEUE and STOPS[-1][:2] == ("shutdown", "killed") and STOPS[-1][2] < 5)
    ytdlp.GRACE_S = saved

    # a stop file left over from a paused attempt must not stop the next attempt
    store, lib = env()
    item = claim(store, "stopfile-resume")
    t0 = time.time()
    outcome, _, _ = run(store, item, stop=lambda: "pause" if time.time() - t0 > 0.7 else None)
    work = Path(store.get_item(item["id"])["source_ref"]["work_dir"])
    (work / ytdlp.STOP_FILE).write_text("pause")                   # as if cleanup had failed
    store.transition(item["id"], "paused", expect=states.ACTIVE_STATES)
    store.transition(item["id"], "queued", expect="paused")
    again = store.claim_next({"default": 100}, 100)
    outcome, _, _ = run(store, again)
    check("a stale stop file is removed before the next attempt, which then completes",
          outcome.kind == Outcome.DONE)

    # a worker that says 'stopped' nobody asked for is a retryable failure, not a pause
    store, lib = env()
    outcome, _, _ = run(store, claim(store, "selfstop"))
    check("a 'stopped' result nobody asked for is a retryable failure",
          outcome.kind == Outcome.FAILED and outcome.retryable and "stopped on its own" in outcome.error)

    # the stall clock counts progress lines as life, even when the folder doesn't grow
    store, lib = env()
    item = claim(store, "merge")
    outcome, _, _ = run(store, item, stall=2)
    check("a run whose longest silence is 1.2 s is not a stall at stall_seconds=2", outcome.kind == Outcome.DONE)
    store, lib = env()
    item = claim(store, "ppstuck")
    outcome, _, elapsed = run(store, item, stall=1)
    check("...but a worker that goes silent for longer IS a stall, and is killed",
          outcome.kind == Outcome.STALLED and elapsed < 10)


# =========================================================================== 3. the real thing

def test_real():
    import _hub_ytdlp_media as media
    if importlib.util.find_spec("yt_dlp") is None or not media.have_ffmpeg():
        print("skip    real yt-dlp / ffmpeg end-to-end (yt-dlp or ffmpeg isn't installed)")
        return
    ytdlp.worker_command_override = None
    ytdlp.ENGINE.available = type(ytdlp.ENGINE).available.__get__(ytdlp.ENGINE)
    vsize, asize = media.make_media(os.path.join(HOME, "media"))
    old_pp = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = media.write_plugin(os.path.join(HOME, "plugin")) + (os.pathsep + old_pp if old_pp else "")
    server, base = media.start_media_server(os.path.join(HOME, "media"))
    models = []
    real_init = ytdlp._Run.__init__

    def keep(self, proc, model=None):
        real_init(self, proc, model)
        models.append(self.model)

    ytdlp._Run.__init__ = keep
    try:
        ok, why = ytdlp.ENGINE.available()
        check("the real engine reports itself ready", ok)
        url = "%s/hubtest/abc?v=%d&a=%d&approx=1&bps=300000" % (base, vsize, asize)

        store, lib = env()
        item = claim(store, url)
        outcome, rows, _ = run(store, item, sample=True)
        row = store.get_item(item["id"])
        check("REAL: a merged video+audio download finishes", outcome.kind == Outcome.DONE and Path(row["dest_file"]).exists())
        check("REAL: the file is in the library and nothing else is left behind",
              [p.name for p in lib.rglob("*") if p.is_file()] == [Path(row["dest_file"]).name]
              and not [p for p in lib.rglob("*") if p.is_dir() and p.name.startswith(ytdlp.WORK_PREFIX)])
        dl_rows = [r for r in rows if r["state"] == "downloading" and r["bytes_total"]]
        check("REAL: the total is video + audio from yt-dlp's own numbers", dl_rows and all(r["bytes_total"] == vsize + asize for r in dl_rows))
        check("REAL: live percent, speed and ETA while downloading",
              any(r["percent"] and 0 < r["percent"] < 100 and r["speed_bps"] and r["eta_s"] is not None for r in dl_rows))
        check("REAL: percent is marked '≈' (the audio size was only approximate)", any("percent" in r["derived"] for r in dl_rows))
        check("REAL: the file name and the title are shown", any((r["message"] or "").startswith("Hub_Test_abc") for r in dl_rows)
              and any(r["title"] == "Hub Test abc" for r in rows))
        check("REAL: the percent never reached 100 while still downloading", all((r["percent"] or 0) < 99.9 for r in dl_rows))
        m = models[-1]
        check("REAL: yt-dlp's post-processor hook reached the engine (the merge was seen)",
              any("Merger" in n for n in m.pp_names))
        check("REAL: the plan said two streams, video then audio", [(s["v"], s["a"]) for s in m.streams] == [(True, False), (False, True)])

        # pause + resume continues from the .part with a Range request
        store, lib = env()
        item = claim(store, url + "&x=resume")
        t0 = time.time()
        STOPS.clear()
        server.requests.clear()
        outcome, _, _ = run(store, item, stop=lambda: "pause" if time.time() - t0 > 1.6 else None)
        row = store.get_item(item["id"])
        work = Path(row["source_ref"]["work_dir"])
        part = next(work.glob("*.part"), None)
        check("REAL: pause is polite and keeps the .part", outcome.kind == Outcome.PAUSED and STOPS[-1][1] == "polite" and part is not None)
        paused_at = part.stat().st_size if part else 0
        store.transition(item["id"], "paused", expect=states.ACTIVE_STATES)
        store.transition(item["id"], "queued", expect="paused")
        again = store.claim_next({"default": 100}, 100)
        server.requests.clear()
        outcome, rows, _ = run(store, again, sample=True)
        ranges = [r for p, r in server.requests if p.endswith("video.mp4")]
        check("REAL: the resumed download completes", outcome.kind == Outcome.DONE)
        check("REAL: it asked the server to continue where the .part ended (a Range request)",
              paused_at > 0 and any(r and r.startswith("bytes=%d-" % paused_at) for r in ranges))
        first = next((r for r in rows if r["bytes_done"]), None)
        check("REAL: the row continues from where it was, not from 0", first is not None and first["bytes_done"] >= paused_at)

        # cancel is prompt and leaves nothing
        store, lib = env()
        item = claim(store, url + "&x=cancel")
        t0 = time.time()
        STOPS.clear()
        outcome, _, elapsed = run(store, item, stop=lambda: "cancel" if time.time() - t0 > 1.5 else None)
        row = store.get_item(item["id"])
        check("REAL: cancel is prompt (polite, well inside the grace time)",
              outcome.kind == Outcome.CANCELLED and STOPS[-1][1] == "polite" and STOPS[-1][2] < ytdlp.GRACE_S)
        check("REAL: a cancelled download leaves no half-written file anywhere",
              [p for p in lib.rglob("*") if p.is_file()] == [] and not Path(row["source_ref"]["work_dir"]).exists())

        # one stream only: no merge, no phases
        store, lib = env()
        item = claim(store, "%s/hubtest/solo?v=%d&only=video&bps=600000" % (base, vsize))
        outcome, rows, _ = run(store, item, sample=True)
        dl_rows = [r for r in rows if r["state"] == "downloading" and r["bytes_total"]]
        check("REAL: a single stream downloads with an exact total and no '≈'",
              outcome.kind == Outcome.DONE and dl_rows and all(r["bytes_total"] == vsize for r in dl_rows)
              and not any("percent" in r["derived"] for r in dl_rows))

        # audio extraction is its own labelled phase
        store, lib = env()
        item = claim(store, "%s/hubtest/aud?v=%d&a=%d" % (base, vsize, asize), mode="audio", audio_format="mp3")
        outcome, rows, _ = run(store, item, sample=True)
        m = models[-1]
        check("REAL: audio mode finishes and the extraction step was seen",
              outcome.kind == Outcome.DONE and Path(store.get_item(item["id"])["dest_file"]).suffix == ".mp3"
              and any("ExtractAudio" in n for n in m.pp_names))
    finally:
        ytdlp._Run.__init__ = real_init
        media.stop_media_server(server)
        if old_pp is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pp


def main():
    for fn in (test_model, test_engine_progress, test_engine_stop, test_real):
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
