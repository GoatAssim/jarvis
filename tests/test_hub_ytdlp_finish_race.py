"""Download Hub, brief 7B: two yt-dlp downloads that end with the SAME file name and finish together must
not overwrite each other.  No network, no yt-dlp, no ffmpeg: the engine's worker process is a small fake
script (the same way test_hub_ytdlp.py does it), so the real engine, the real `_finalize` and the real file
system are in the loop and only the video is fake.

Before the fix `_finalize` did `naming.unique_path(dest / name)` and then `os.replace(source, target)`: "check,
then replace".  Two items that both pass the check before either has replaced get the same target, and the
second replaces the first.  The work happens in a per-item folder (WORK_PREFIX + id), so only the last move
collides.  A playlist group (many videos, similar names) is the realistic trigger.

What is pinned here:
  1. the natural race: two items (and a group of four) that reach `_finalize` together, repeated 50 times
     (10 for the group); every round both files survive with their own bytes;
  2. the same with the window forced open (both items wait right before the final move, then go together), so
     the test does not depend on thread timing; repeated 50 times;
  3. an existing file is never overwritten: one that was there before, one that appears between the choice of
     the name and the move, a `.part` that belongs to another download, several taken names in a row;
  4. several files from one item against several from another;
  5. pause / resume and the per-item work folder behave as before (two running items have two folders, pause
     keeps the folder and the .part, resume continues in the SAME folder, the folder is gone afterwards, and a
     resumed item whose name was taken meanwhile lands under the next free name).

Run: python tests/test_hub_ytdlp_finish_race.py        (Linux; not run on Windows)
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-ytdlp-race-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"      # tests never fire real notifications
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub.engines import ytdlp  # noqa: E402
from jarvis.hub.engines.base import EngineContext, Outcome  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

ROUNDS = 50            # the race is repeated this many times
GROUP_ROUNDS = 10
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


# --------------------------------------------------------------------------- the fake worker
# /race/<tag>      one file "Same Name [race].mp4" holding <tag> * 400 bytes
# /pair/<tag>      two files "Clip A [pair].mp4" and "Clip B [pair].mp4", both holding <tag>
# /resume/<tag>    first run: writes "Same Name [race].mp4.part" and waits; second run: finishes it
FAKE = Path(HOME) / "fake_race_worker.py"
FAKE.write_text(r'''
import json, os, sys, time
MARK = "@@JARVIS_YTDL_RESULT@@"
args = json.loads(sys.stdin.read())
out = args["output_dir"]
kind, tag = args["url"].rstrip("/").split("/")[-2:]

def result(payload):
    print(MARK + " " + json.dumps(payload))
    sys.exit(0)

def write(name, data):
    with open(os.path.join(out, name), "wb") as f:
        f.write(data)

if kind == "race":
    write("Same Name [race].mp4", tag.encode() * 400)
    result({"ok": True, "files": [{"file": "Same Name [race].mp4", "title": "T " + tag}]})
if kind == "pair":
    write("Clip A [pair].mp4", ("A-" + tag).encode() * 100)
    write("Clip B [pair].mp4", ("B-" + tag).encode() * 100)
    result({"ok": True, "files": [{"file": "Clip A [pair].mp4", "title": "T " + tag}]})
if kind == "resume":
    part = os.path.join(out, "Same Name [race].mp4.part")
    if os.path.exists(part):
        os.replace(part, os.path.join(out, "Same Name [race].mp4"))
        result({"ok": True, "files": [{"file": "Same Name [race].mp4", "title": "T " + tag}]})
    write("Same Name [race].mp4.part", tag.encode() * 400)
    time.sleep(60)
result({"ok": False, "error": "unknown fake url"})
''')
ytdlp.worker_command_override = [sys.executable, str(FAKE)]
ytdlp.ENGINE.available = lambda: (True, "")            # no yt-dlp / ffmpeg needed here


def env():
    _n[0] += 1
    lib = Path(HOME) / f"lib{_n[0]}"
    store = Store(Path(HOME) / f"hub-{_n[0]}.db")
    store.set_settings({"library_root": str(lib)})
    return store, lib


def queue(store, kind, tags):
    """Add one row per tag, claim them all: -> {tag: claimed item}."""
    ids = {}
    for tag in tags:
        row = store.add_item(engine="ytdlp", source_url="https://video.example/%s/%s" % (kind, tag),
                             title="t", options={})[0]
        ids[row["id"]] = tag
    claimed = {}
    for _ in tags:
        item = store.claim_next({"default": 100}, 100)
        assert item and item["id"] in ids, "could not claim every queued item"
        claimed[ids[item["id"]]] = item
    return claimed


def make_ctx(store, item, stop=None):
    return EngineContext(store, item, store.get_settings(), stop_reason=stop or (lambda: None),
                         rate_limit_bps=lambda: 0)


def run_together(store, items):
    """Run every item's engine in its own thread; -> {tag: Outcome}."""
    out, errors = {}, []

    def one(tag, item):
        try:
            out[tag] = ytdlp.ENGINE.run(item, make_ctx(store, item))
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=one, args=(t, i)) for t, i in items.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    out["__errors__"] = errors
    return out


def files_of(folder):
    return {p.name: p.read_bytes() for p in Path(folder).iterdir() if p.is_file()}


def workdirs(lib):
    return [p for p in lib.rglob("*") if p.is_dir() and p.name.startswith(ytdlp.WORK_PREFIX)]


# --------------------------------------------------------------------------- how the two items are made to meet
_ORIG_FINALIZE = ytdlp.YtdlpEngine._finalize
_ORIG_REPLACE, _ORIG_LINK = os.replace, os.link


class Meeting:
    """`before_finalize(n)`: n items wait until all have reached `_finalize`, then go together.
    `before_move(n)`: n items wait right before their final move (the `os.replace` of the old code, the
    `os.link` of the new), after each has chosen its name, then go together.  Only the first move of a
    thread counts, and only a move out of a per-item work folder (never the database's own files)."""

    def __init__(self):
        self.mode = None
        self.barrier = None
        self.seen = threading.local()
        self.rival = None                 # (name, bytes): created right before the first move of any thread
        self.rival_done = False
        self.lock = threading.Lock()

    def arm(self, mode, n=2, rival=None):
        self.mode = mode
        self.barrier = threading.Barrier(n) if n > 1 else None
        self.seen = threading.local()
        self.rival, self.rival_done = rival, False

    def disarm(self):
        self.mode = self.barrier = self.rival = None

    def _wait(self):
        if self.barrier is not None:
            try:
                self.barrier.wait(10)
            except threading.BrokenBarrierError:
                pass

    def finalize(self, engine, ctx, result, work_dir, dest_dir, roots):
        if self.mode == "finalize":
            self._wait()
        return _ORIG_FINALIZE(engine, ctx, result, work_dir, dest_dir, roots)

    def _is_final_move(self, source):
        return Path(str(source)).parent.name.startswith(ytdlp.WORK_PREFIX)

    def before_move(self, source, target):
        if not self._is_final_move(source):
            return
        if self.mode == "move" and not getattr(self.seen, "moved", False):
            self.seen.moved = True
            self._wait()
        if self.rival is not None:
            with self.lock:
                if not self.rival_done:
                    self.rival_done = True
                    name, data = self.rival
                    folder = Path(str(target)).parent
                    (folder / name).write_bytes(data)       # somebody took the name after it was chosen


MEET = Meeting()
ytdlp.YtdlpEngine._finalize = lambda self, ctx, result, work_dir, dest_dir, roots: MEET.finalize(
    self, ctx, result, work_dir, dest_dir, roots)


def _replace(src, dst, *a, **k):
    MEET.before_move(src, dst)
    return _ORIG_REPLACE(src, dst, *a, **k)


def _link(src, dst, *a, **k):
    MEET.before_move(src, dst)
    return _ORIG_LINK(src, dst, *a, **k)


os.replace, os.link = _replace, _link


# --------------------------------------------------------------------------- 1 and 2: the race

def race_rounds(mode, label, rounds, n):
    lost = errors = wrong_names = leftovers = 0
    first_bad = None
    for i in range(rounds):
        store, lib = env()
        tags = ["t%d" % k for k in range(n)]
        items = queue(store, "race", tags)
        MEET.arm(mode, n)
        res = run_together(store, items)
        MEET.disarm()
        errors += len(res.pop("__errors__"))
        folder = lib / "Video"
        got = files_of(folder) if folder.exists() else {}
        want = {tag.encode() * 400 for tag in tags}
        if set(got.values()) != want or len(got) != n:
            lost += 1
            first_bad = first_bad or (i, sorted(got), [(t, o.kind) for t, o in res.items()])
        expected_names = {"Same Name [race].mp4"} | {"Same Name [race] (%d).mp4" % k for k in range(1, n)}
        if set(got) != expected_names:
            wrong_names += 1
        if workdirs(lib) or list(lib.rglob("*.part")):
            leftovers += 1
        if not all(o.kind == Outcome.DONE for o in res.values()):
            errors += 1
    print("info    %s: %d of %d rounds lost a file, %d had wrong names" % (label, lost, rounds, wrong_names))
    check("%s: no round lost a file (%d rounds, %d items each)%s"
          % (label, rounds, n, "" if not lost else "  <- first bad round: %r" % (first_bad,)), lost == 0)
    check("%s: the files are named 'name', 'name (1)', ... and nothing else" % label, wrong_names == 0)
    check("%s: every item ended DONE and no run raised" % label, errors == 0)
    check("%s: no work folder and no .part left behind" % label, leftovers == 0)


def test_race():
    race_rounds("finalize", "natural race, 2 items", ROUNDS, 2)
    race_rounds("move", "window forced open, 2 items", ROUNDS, 2)
    race_rounds("finalize", "natural race, group of 4", GROUP_ROUNDS, 4)
    race_rounds("move", "window forced open, group of 4", GROUP_ROUNDS, 4)


# --------------------------------------------------------------------------- 3: never overwrite

def finish_one(store, tag="new", kind="race"):
    item = queue(store, kind, [tag])[tag]
    return ytdlp.ENGINE.run(item, make_ctx(store, item))


def test_never_overwrite():
    # a file that was there before
    store, lib = env()
    (lib / "Video").mkdir(parents=True)
    old = lib / "Video" / "Same Name [race].mp4"
    old.write_bytes(b"OLD")
    out = finish_one(store)
    check("a file that already exists is not overwritten", old.read_bytes() == b"OLD")
    check("...the new one lands as 'name (1)' with its own bytes",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "Same Name [race] (1).mp4"
          and Path(out.dest_file).read_bytes() == b"new" * 400)

    # several taken names in a row
    store, lib = env()
    (lib / "Video").mkdir(parents=True)
    for suffix, data in ((".mp4", b"ZERO"), (" (1).mp4", b"ONE"), (" (2).mp4", b"TWO")):
        (lib / "Video" / ("Same Name [race]" + suffix)).write_bytes(data)
    out = finish_one(store)
    check("three taken names in a row: the new file is 'name (3)' and the three old ones are intact",
          Path(out.dest_file).name == "Same Name [race] (3).mp4"
          and (lib / "Video" / "Same Name [race].mp4").read_bytes() == b"ZERO"
          and (lib / "Video" / "Same Name [race] (1).mp4").read_bytes() == b"ONE"
          and (lib / "Video" / "Same Name [race] (2).mp4").read_bytes() == b"TWO")

    # a .part that belongs to another download counts as taken
    store, lib = env()
    (lib / "Video").mkdir(parents=True)
    other = lib / "Video" / "Same Name [race].mp4.part"
    other.write_bytes(b"ANOTHER DOWNLOAD")
    out = finish_one(store)
    check("another download's .part keeps its name: the new file goes to 'name (1)'",
          Path(out.dest_file).name == "Same Name [race] (1).mp4" and other.read_bytes() == b"ANOTHER DOWNLOAD")

    # somebody takes the name AFTER it was chosen and before the move (the narrow window of the old code)
    store, lib = env()
    (lib / "Video").mkdir(parents=True)
    MEET.arm(None, 1, rival=("Same Name [race].mp4", b"RIVAL"))
    out = finish_one(store)
    MEET.disarm()
    check("a file that appears between choosing the name and the move is not overwritten",
          (lib / "Video" / "Same Name [race].mp4").read_bytes() == b"RIVAL")
    check("...and the new file is kept under the next free name",
          out.kind == Outcome.DONE and Path(out.dest_file).name == "Same Name [race] (1).mp4"
          and Path(out.dest_file).read_bytes() == b"new" * 400)

    # nothing else changed for the plain case
    store, lib = env()
    item = queue(store, "race", ["solo"])["solo"]
    out = ytdlp.ENGINE.run(item, make_ctx(store, item))
    row = store.get_item(item["id"])
    check("the plain case is as before: <library>/Video/<name>, the row knows it, no work folder, no source_ref",
          Path(out.dest_file) == lib / "Video" / "Same Name [race].mp4" and row["dest_file"] == out.dest_file
          and workdirs(lib) == [] and not row["source_ref"])


# --------------------------------------------------------------------------- 4: several files per item

def test_pairs():
    ok = True
    for mode in ("finalize", "move"):
        store, lib = env()
        items = queue(store, "pair", ["x", "y"])
        MEET.arm(mode, 2)
        res = run_together(store, items)
        MEET.disarm()
        got = files_of(lib / "Video")
        want = {("A-x").encode() * 100, ("A-y").encode() * 100, ("B-x").encode() * 100, ("B-y").encode() * 100}
        ok = ok and set(got.values()) == want and len(got) == 4 and not res["__errors__"] \
            and all(res[t].kind == Outcome.DONE for t in ("x", "y"))
    check("two items with two files each, same names, finishing together: all four files survive", ok)


# --------------------------------------------------------------------------- 5: pause / resume, work folders

def test_work_folders_and_resume():
    store, lib = env()
    items = queue(store, "resume", ["a", "b"])
    stop = {"on": False}
    results = {}

    def go(tag):
        item = items[tag]
        results[tag] = ytdlp.ENGINE.run(item, make_ctx(store, item, stop=lambda: "pause" if stop["on"] else None))

    threads = [threading.Thread(target=go, args=(t,)) for t in items]
    for t in threads:
        t.start()
    deadline = time.time() + 30
    while time.time() < deadline and len(workdirs(lib)) < 2:
        time.sleep(0.05)
    while time.time() < deadline and len(list(lib.rglob("*.mp4.part"))) < 2:
        time.sleep(0.05)
    dirs = {p.name for p in workdirs(lib)}
    check("two running items work in two different per-item folders, named WORK_PREFIX + id",
          dirs == {ytdlp.WORK_PREFIX + it["id"] for it in items.values()})
    stop["on"] = True
    for t in threads:
        t.join(30)
    check("pause stops both and keeps both work folders with their .part",
          all(results.get(t) is not None and results[t].kind == Outcome.PAUSED for t in items)
          and {p.name for p in workdirs(lib)} == dirs and len(list(lib.rglob("*.mp4.part"))) == 2)
    check("nothing reached the library folder itself",
          not [p for p in (lib / "Video").iterdir() if p.is_file()])

    # item b resumes first and finishes; item a resumes later and must not replace it
    from jarvis.hub import states
    folder_of = {t: Path(store.get_item(items[t]["id"])["source_ref"]["work_dir"]) for t in items}
    seen = {}
    for tag in ("b", "a"):
        store.transition(items[tag]["id"], "paused", expect=states.ACTIVE_STATES)
        store.transition(items[tag]["id"], "queued", expect="paused")
        again = store.claim_next({"default": 100}, 100)
        assert again and again["id"] == items[tag]["id"]
        before = folder_of[tag].exists()
        out = ytdlp.ENGINE.run(again, make_ctx(store, again))
        seen[tag] = (before, out)
    final = lib / "Video" / "Same Name [race].mp4"
    check("a resumed item continues in the SAME folder it paused in and finishes",
          all(seen[t][0] and seen[t][1].kind == Outcome.DONE for t in seen))
    check("the first to finish owns 'name', the second got 'name (1)', both with their own bytes",
          final.read_bytes() == b"b" * 400
          and (lib / "Video" / "Same Name [race] (1).mp4").read_bytes() == b"a" * 400)
    check("the per-item folders are gone afterwards", workdirs(lib) == [] and not list(lib.rglob("*.part")))


def main():
    test_race()
    test_never_overwrite()
    test_pairs()
    test_work_folders_and_resume()
    print()
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
