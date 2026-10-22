"""Download Hub, N5 (brief 16): a running Steam download is never "stalled" from the manifest alone.

Steam does not write BytesDownloaded while it downloads: the manifest is rewritten in bursts 20 to 50 s
apart.  The old rule (3 looks without movement = stalled) therefore said "stalled" about 6 s after every
write.  The new rule: a running row is "downloading" (its message says what Steam last reported and how old
that is; no live percent, no speed, no ETA), and "stalled" needs the manifest bytes AND every readable second
signal (the downloading folder, Steam's disk writes) flat for STALL_SECONDS, with at least one readable.

Covers: the real run's fixture (tests/fixtures/steam_n5_real_run.jsonl, an excerpt of one Windows PC's
145-second run) replayed through the new code, tick by tick, against what the OLD code said; the 59 s / 61 s
boundary; each signal moving, flat, unavailable; gaps and restarts of the "flat since" timers; the pause bit,
staging, a finished game reported once, a queued game; the folder reader (caps, links, missing folder); the
writes reader (psutil faked: filtered by name, a refused process, no psutil); the provider (readers only for
running apps, a reader that raises, Steam not running); the hub store (a burst of bytes never becomes a
speed); the copies of the thresholds in steam-checks.py; and that hub/steam.py imports nothing that could
start a process or open the network.

Tested against fixtures and fakes on Linux.  Not run on Windows, with a real Steam or a real psutil reading
of Steam's processes.

Run: python tests/test_hub_steam_alive.py
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-hub-steamalive-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ.pop("JARVIS_STEAM_ROOT", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.hub import providers, steam  # noqa: E402
from jarvis.hub.store import Store  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def man(appid, flags, to_dl=0, got=0, to_stage=0, staged=0, size=0, updated=0, name="Game"):
    """A parsed manifest, as parse_vdf gives it."""
    return {"AppState": {"appid": str(appid), "name": name, "StateFlags": str(flags), "installdir": name,
                         "LastUpdated": str(updated), "SizeOnDisk": str(size), "BytesToDownload": str(to_dl),
                         "BytesDownloaded": str(got), "BytesToStage": str(to_stage), "BytesStaged": str(staged)}}


def folder(nbytes=1000, files=10, mtime=100.0):
    return {"bytes": nbytes, "files": files, "newest_mtime": mtime}


T0 = 5_000.0
KB = 1024
MB = 1024 * 1024


def run_looks(seconds, step=1.0, flags=1026, to_dl=1000, got=300, signals=None, mem=None, start=0.0, manifest=None):
    """Look every `step` s from `start` to `seconds`; `signals` is a function t -> signals dict.
    Returns (states per look, the last row, memory)."""
    mem = mem if mem is not None else {}
    out, row, t = [], None, start
    while t <= seconds + 1e-9:
        sig = signals(t) if callable(signals) else signals
        m = manifest(t) if callable(manifest) else (manifest or man(1, flags, to_dl, got))
        row = steam.classify(m, mem, now=T0 + t, signals=sig)
        out.append(row["state"] if row else None)
        t += step
    return out, row, mem


FLAT = {"folder": folder(), "writes": 50 * MB}


def sigs_text(row):
    return next(d["value"] for d in row["detail"] if d["label"] == "Other signs of life")

# ------------------------------------------------------------------------------- the thresholds
check("STALL_SECONDS is 60", steam.STALL_SECONDS == 60.0)
check("STALL_LOOKS is gone (nothing imports it)", not hasattr(steam, "STALL_LOOKS"))
spec = importlib.util.spec_from_file_location("steam_checks_copy", ROOT / "steam-checks.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)
check("steam-checks.py keeps the same stall window", sc.STALL_REAL_S == steam.STALL_SECONDS)
check("steam-checks.py keeps the same write-noise limit (per 2 s tick)", sc.IO_NOISE_BYTES == steam.WRITES_MIN_BYTES_PER_S * 2)

# ------------------------------------------------------------------------------- the real run
fixture = [json.loads(line) for line in (ROOT / "tests" / "fixtures" / "steam_n5_real_run.jsonl").read_text(encoding="utf-8").splitlines()]
meta, ticks = fixture[0], fixture[1:]
check("the fixture is an excerpt (small) of the real timeline, with both games", len(ticks) == 70 and set(meta["apps"]) == {"980610", "291550"}
      and (ROOT / "tests" / "fixtures" / "steam_n5_real_run.jsonl").stat().st_size < 20000)


def replay(with_signals=True):
    """Feed the real run through the new code, one look per recorded tick."""
    mem = {a: {} for a in meta["apps"]}
    seen = []
    for tick in ticks:
        for appid, rec in tick["a"].items():
            to_dl, got, to_stage, staged, size, updated = rec["m"]
            m = man(appid, rec["f"], to_dl, got, to_stage, staged, size, updated, name=meta["apps"][appid])
            sig = None
            if with_signals:
                d = rec["d"]
                sig = {"folder": {"bytes": d[0], "files": d[1], "newest_mtime": d[2]} if d else None, "writes": tick["w"]}
            row = steam.classify(m, mem[appid], now=T0 + tick["t"], signals=sig)
            seen.append((tick["t"], appid, rec["old"], row["state"] if row else None, row))
    return seen


real = replay()
old_stalled = [x for x in real if x[2] == "stalled"]
check("the real run has the stalled ticks the report lists (7 + 21 for Quaver, 19 + 1 for Brawlhalla)",
      len([x for x in old_stalled if x[1] == "980610"]) == 28 and len([x for x in old_stalled if x[1] == "291550"]) == 20)
check("EVERY tick where the old code said 'stalled' is now 'downloading'", old_stalled and all(x[3] == "downloading" for x in old_stalled))
check("the new code never says 'stalled' anywhere in the real run", not any(x[3] == "stalled" for x in real))
running_rows = [x[4] for x in real if x[3] == "downloading"]
check("a running real row has no live percent, no done-bytes, no speed, no ETA (sent as None on purpose)",
      running_rows and all(r["percent"] is None and r["bytes_done"] is None and r["speed_bps"] is None and r["eta_s"] is None for r in running_rows))
check("...but it knows the total and says that Steam isn't reporting live progress", all(r["bytes_total"] > 0 and "live progress" in r["message"] for r in running_rows))
quaver_after_write = [x[4] for x in real if x[1] == "980610" and 45.9 < x[0] < 60]
check("20 s after a manifest write the message says what Steam reported and how old it is",
      quaver_after_write and any("reported 1" in r["message"] or "reported 2" in r["message"] for r in quaver_after_write)
      and any("30.6 %" in r["message"] and "37.4 MB of 122.3 MB" in r["message"] for r in quaver_after_write))
check("the early 'waiting in Steam's own queue' tick is still queued (existing behaviour)", any(x[1] == "980610" and x[3] == "queued" for x in real))
done = [x for x in real if x[3] == "done"]
check("the finished game is reported 'done' exactly once", len(done) == 1 and done[0][1] == "980610" and done[0][4]["percent"] == 100)
check("the real signals were read as moving at some looks and the row says so in its detail",
      any(any(d["label"] == "Other signs of life" and "downloading folder last moved" in d["value"] for d in r["detail"]) for r in running_rows))

blind = replay(with_signals=False)
check("with NO second signal at all the real run is never stalled either", not any(x[3] == "stalled" for x in blind))
check("...and the message says Jarvis can't see any other sign of activity", all("can't see any other sign" in x[4]["message"] for x in blind if x[3] == "downloading"))

# ------------------------------------------------------------------------------- the stall rule, in time
states, row, _ = run_looks(59, signals=FLAT)
check("flat folder + flat writes for 59 s: still downloading", set(states) == {"downloading"})
states, row, _ = run_looks(61, signals=FLAT)
check("...for 61 s: stalled", states[-1] == "stalled" and states[-2] == "stalled" and states[58] == "downloading")
check("a stalled row says how long, and which signals were checked",
      "nothing moved for 61 s" in row["message"] and "manifest" in row["message"] and "downloading folder" in row["message"] and "Steam disk writes" in row["message"])
check("a stalled row keeps what Steam last reported (30.0 %) and still no speed", row["percent"] == 30.0 and row["bytes_done"] == 300 and row["speed_bps"] is None)
states, _, _ = run_looks(70, step=5.0, signals=FLAT)
check("the rule is time-based: looks every 5 s stall at the same moment (60 s), not after N looks", states.index("stalled") == 12 and states[11] == "downloading")

moving_folder = lambda t: {"folder": folder(1000 + int(t) * 5000, 10, 100.0 + t), "writes": 50 * MB}
states, _, _ = run_looks(300, signals=moving_folder)
check("a folder that keeps growing keeps the row downloading (writes flat)", set(states) == {"downloading"})
moving_writes = lambda t: {"folder": folder(), "writes": 50 * MB + int(t) * 2 * MB}
states, _, _ = run_looks(300, signals=moving_writes)
check("Steam's writes that keep moving keep it downloading (folder flat)", set(states) == {"downloading"})
tiny_writes = lambda t: {"folder": folder(), "writes": 50 * MB + int(t) * 10 * KB}
states, _, _ = run_looks(61, signals=tiny_writes)
check("writes under the noise limit (10 KB/s) count as flat", states[-1] == "stalled")
only_folder = lambda t: {"folder": folder(), "writes": None}
states, _, _ = run_looks(61, signals=only_folder)
check("writes unavailable, folder flat 61 s: stalled (one readable signal is enough)", states[-1] == "stalled")
states, _, _ = run_looks(61, signals=lambda t: {"folder": None, "writes": 50 * MB})
check("folder unavailable, writes flat 61 s: stalled", states[-1] == "stalled")
states, row, _ = run_looks(600, signals={"folder": None, "writes": None})
check("both unavailable: never stalled, even after 10 minutes", set(states) == {"downloading"} and "can't see any other sign" in row["message"])
states, row, _ = run_looks(600, signals=None)
check("no signals given at all (a stateless caller) is the same", set(states) == {"downloading"})
states, _, _ = run_looks(300, signals=lambda t: {"folder": None, "writes": 50 * MB + int(t) * 3 * MB})
check("one signal unavailable, the other moving: downloading", set(states) == {"downloading"})

_, row, _ = run_looks(5, signals={"folder": None, "writes": 1 * MB})
check("...the detail line says 'unavailable' for it", "downloading folder unavailable" in sigs_text(row) and "Steam disk writes last moved" in sigs_text(row))

# a flat signal that disappears for one look is not "flat" across the hole
def hole(t):
    return {"folder": None if t == 40 else folder(), "writes": 50 * MB}


states, _, _ = run_looks(61, signals=hole)
check("a signal that was unreadable for one look restarts its timer (not stalled at 61 s)", states[-1] == "downloading")
states, _, _ = run_looks(101, signals=hole)
check("...and stalls once it has been flat for 60 s again", states[-1] == "stalled")

# a gap between looks restarts the timers: we cannot vouch for time we did not watch
mem = {}
run_looks(30, signals=FLAT, mem=mem)
row = steam.classify(man(1, 1026, 1000, 300), mem, now=T0 + 400, signals=FLAT)
check("after a gap longer than LOOK_GAP_SECONDS (a sleeping PC, a stopped daemon) flat is not assumed", row["state"] == "downloading")
row = steam.classify(man(1, 1026, 1000, 300), mem, now=T0 + 400 + steam.LOOK_GAP_SECONDS - 1, signals=FLAT)
check("...and the timer then runs from the new look", row["state"] == "downloading")
row = steam.classify(man(1, 1026, 1000, 300), mem, now=T0 + 10, signals=FLAT)
check("a clock that jumps backwards also restarts them", row["state"] == "downloading")

# the manifest itself: a recent write (a burst) means Steam is alive even if both signals are flat
def burst(t):
    return man(1, 1026, 1000, 300 if t < 50 else 400)


states, _, _ = run_looks(61, signals=FLAT, manifest=burst)
check("a manifest write 11 s ago keeps it downloading although both signals were flat for 61 s", states[-1] == "downloading")
states, _, _ = run_looks(111, signals=FLAT, manifest=burst)
check("...and 60 s after that write, with both still flat, it is stalled", states[-1] == "stalled")
_, row, _ = run_looks(100, signals=moving_folder, manifest=burst)
check("a burst is not a rate: the row carries no speed or ETA after one", row["speed_bps"] is None and row["eta_s"] is None and row["percent"] is None)
check("...and the message gives the age of the last write", "reported" in row["message"])

# the first look says honestly that its age is unknown
row = steam.classify(man(2, 1026, 1000, 400), {}, now=T0, signals=FLAT)
check("first look: 'as Steam had written it when Jarvis first looked'", "40.0 %" in row["message"] and "first looked" in row["message"])
mem = {}
steam.classify(man(2, 1026, 1000, 400), mem, now=T0, signals=FLAT)
row = steam.classify(man(2, 1026, 1000, 400), mem, now=T0 + 20, signals=FLAT)
check("...later 'reported at least 20 s ago' (it was never seen to change)", "reported at least 20 s ago" in row["message"])
row = steam.classify(man(2, 1026, 1000, 0), {}, now=T0, signals=FLAT)
check("nothing reported yet: says so", row["state"] == "downloading" and "hasn't reported any progress yet" in row["message"] and row["bytes_total"] == 1000)
row = steam.classify(man(2, 1026, 1000, 5000), {}, now=T0, signals=FLAT)
check("bytes past the total: the percent in the message stays under 100", "99.0 %" in row["message"] and row["state"] == "downloading")
mem = {}
for i in range(100):
    row = steam.classify(man(2, 1026, 1000, 1000), mem, now=T0 + i, signals=FLAT)
check("a complete download waiting for Steam to finish is never called stalled", row["state"] == "downloading")

# ------------------------------------------------------------------------------- the other states, unchanged
row = steam.classify(man(3, 1026 | 512, 500, 100), {}, now=T0)
check("the pause bit means paused", row["state"] == "paused" and row["message"] == "paused in Steam")
check("a paused-flag update with no bytes is still not listed", steam.classify(man(3, 518), {}, now=T0) is None)
row = steam.classify(man(4, 1026 | 2097152, 1000, 1000, 400, 100), {}, now=T0)
check("staging is still the 'installing' phase with its own percent", row["state"] == "postprocessing" and row["percent"] == 25.0)
row = steam.classify(man(5, 1026), {}, now=T0)
check("started but nothing fetched is still queued", row["state"] == "queued" and "queue" in row["message"])
mem = {}
steam.classify(man(6, 1026, 100, 60), mem, now=T0, signals=FLAT)
row = steam.classify(man(6, 4, size=100), mem, now=T0 + 3)
check("a finished game is reported done once, then not listed", row["state"] == "done" and steam.classify(man(6, 4, size=100), mem, now=T0 + 6) is None)
check("a pending update (bit 2) is not a download", steam.classify(man(7, 6, 500), {}, now=T0) is None)
check("is_running_download: only a transfer that is not paused or staging",
      steam.is_running_download(man(8, 1026)) and not steam.is_running_download(man(8, 1026 | 512))
      and not steam.is_running_download(man(8, 1026 | 2097152)) and not steam.is_running_download(man(8, 4)))
row = steam.classify(man(9, 1026 | 512, 500, 100), {}, now=T0, signals=FLAT)
check("a signal handed to a paused row changes nothing", row["state"] == "paused")

# ------------------------------------------------------------------------------- the folder reader
base = Path(HOME) / "lib"
(base / "steamapps" / "downloading" / "77" / "a" / "b").mkdir(parents=True)
(base / "steamapps" / "temp" / "77").mkdir(parents=True)
(base / "steamapps" / "downloading" / "77" / "one.bin").write_bytes(b"x" * 1000)
(base / "steamapps" / "downloading" / "77" / "a" / "b" / "two.bin").write_bytes(b"x" * 500)
(base / "steamapps" / "temp" / "77" / "three.bin").write_bytes(b"x" * 250)
snap = steam.read_folder_signal(str(base), 77)
check("the folder reader sums downloading/ and temp/ of the game's own library", snap and snap["bytes"] == 1750 and snap["files"] == 3 and snap["newest_mtime"] > 0)
check("a game with neither folder is UNAVAILABLE (None), not flat", steam.read_folder_signal(str(base), 78) is None)
check("a missing library is unavailable, not an error", steam.read_folder_signal(str(base / "nope"), 77) is None)
check("over the file cap: unavailable (never a half-read 'flat')", steam.read_folder_signal(str(base), 77, max_files=2) is None
      and steam.read_folder_signal(str(base), 77, max_files=3) is not None)
check("deeper than the depth cap: unavailable", steam.read_folder_signal(str(base), 77, max_depth=1) is None
      and steam.read_folder_signal(str(base), 77, max_depth=2) is not None)
deep = Path(HOME) / "lib2" / "steamapps" / "downloading" / "5"
deep.mkdir(parents=True)
for i in range(300):
    (deep / ("f%d" % i)).write_bytes(b"y")
started = time.time()
snap = steam.read_folder_signal(str(Path(HOME) / "lib2"), 5)
check("300 files read in well under a second (the poll runs every ~3 s)", snap and snap["files"] == 300 and time.time() - started < 1.0)
try:
    os.symlink(str(base / "steamapps" / "temp" / "77"), str(deep / "link"))
    check("a link is not followed", steam.read_folder_signal(str(Path(HOME) / "lib2"), 5)["files"] == 300)
except (OSError, NotImplementedError):
    print("skip    (symlinks not available here)")
before = snap
(deep / "f0").write_bytes(b"y" * 10)
after = steam.read_folder_signal(str(Path(HOME) / "lib2"), 5)
check("a grown file shows as moved to the rule", steam._signal_moved("folder", before, after, 3.0))
check("an untouched folder shows as flat", not steam._signal_moved("folder", after, dict(after), 3.0))

# ------------------------------------------------------------------------------- the writes reader (psutil faked)


class Proc:
    def __init__(self, name, written=None, error=None):
        self.info = {"name": name}
        self._w, self._e = written, error

    def io_counters(self):
        if self._e:
            raise self._e
        return types.SimpleNamespace(write_bytes=self._w)


def with_psutil(procs=None, missing=False, iter_error=None):
    fake = types.ModuleType("psutil")

    class Error(Exception):
        pass

    class AccessDenied(Error):
        pass

    def process_iter(attrs=None):
        if iter_error:
            raise iter_error
        return iter(procs or [])

    fake.Error, fake.AccessDenied, fake.process_iter = Error, AccessDenied, process_iter
    saved = sys.modules.get("psutil", "absent")
    sys.modules["psutil"] = None if missing else fake
    try:
        return steam.read_steam_writes(), fake
    finally:
        if saved == "absent":
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = saved


total, _ = with_psutil([Proc("Steam.exe", 100), Proc("steamwebhelper.exe", 200), Proc("steamwebhelper.exe", 50), Proc("chrome.exe", 10 ** 9)])
check("writes: only Steam's processes are summed (by name, any case)", total == 350)

def denied_case():
    fk = types.ModuleType("psutil")

    class Error(Exception):
        pass

    class AccessDenied(Error):
        pass

    fk.Error, fk.AccessDenied = Error, AccessDenied
    fk.process_iter = lambda attrs=None: iter([Proc("steam.exe", 100), Proc("steamservice.exe", error=AccessDenied())])
    saved = sys.modules.get("psutil", "absent")
    sys.modules["psutil"] = fk
    try:
        a = steam.read_steam_writes()
        fk.process_iter = lambda attrs=None: iter([Proc("steamservice.exe", error=AccessDenied())])
        b = steam.read_steam_writes()
        fk.process_iter = lambda attrs=None: iter([Proc("chrome.exe", 5)])
        c = steam.read_steam_writes()
        return a, b, c
    finally:
        if saved == "absent":
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = saved


a, b, c = denied_case()
check("writes: a refused process is skipped and the rest still count", a == 100)
check("writes: only refused processes = unavailable", b is None)
check("writes: no Steam process at all = unavailable (not 'flat at 0')", c is None)
check("writes: no psutil = unavailable", with_psutil(missing=True)[0] is None)
check("writes: psutil blowing up = unavailable", with_psutil(iter_error=RuntimeError("boom"))[0] is None)
check("writes: a counter that goes down (a Steam process ended) counts as movement", steam._signal_moved("writes", 100 * MB, 90 * MB, 3.0))
check("writes: 100 KB in 3 s is movement, 50 KB in 3 s is not", steam._signal_moved("writes", MB, MB + 100 * KB, 3.0) and not steam._signal_moved("writes", MB, MB + 50 * KB, 3.0))

# ------------------------------------------------------------------------------- the provider
steam_dir = Path(HOME) / "Steam"
(steam_dir / "steamapps" / "common").mkdir(parents=True)
(steam_dir / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % str(steam_dir).replace("\\", "\\\\"), encoding="utf-8")


def write_acf(appid, flags, to_dl=0, got=0, size=0, updated=0):
    (steam_dir / "steamapps" / ("appmanifest_%s.acf" % appid)).write_text(
        '"AppState"\n{\n\t"appid"\t\t"%s"\n\t"name"\t\t"G%s"\n\t"StateFlags"\t\t"%d"\n\t"installdir"\t\t"G%s"\n\t"LastUpdated"\t\t"%d"\n'
        '\t"SizeOnDisk"\t\t"%d"\n\t"BytesToDownload"\t\t"%d"\n\t"BytesDownloaded"\t\t"%d"\n\t"BytesToStage"\t\t"0"\n\t"BytesStaged"\t\t"0"\n}\n'
        % (appid, appid, flags, appid, updated, size, to_dl, got), encoding="utf-8")


prov = steam.SteamProvider()
prov.root_override = str(steam_dir)
prov.is_running = lambda: True
calls = {"folder": [], "writes": 0}


def fake_folder(lib, appid):
    calls["folder"].append(str(appid))
    return folder()


def fake_writes():
    calls["writes"] += 1
    return 50 * MB


prov.read_folder, prov.read_writes = fake_folder, fake_writes
write_acf(1, 4, size=100)
write_acf(2, 6, to_dl=500)
res = prov.poll(None, T0)
check("an idle Steam costs no signal reads at all", res["items"] == [] and calls["folder"] == [] and calls["writes"] == 0)
write_acf(10, 1026, 1000, 300)
write_acf(11, 1026 | 512, 1000, 300)
write_acf(12, 1026 | 2097152, 1000, 1000)
res = prov.poll(None, T0 + 3)
check("signals are read only for the app that is running a transfer (not paused, not staging)", calls["folder"] == ["10"] and calls["writes"] == 1)
write_acf(13, 1026, 1000, 100)
calls["folder"].clear(); calls["writes"] = 0
res = prov.poll(None, T0 + 6)
check("two running apps: two folder reads, ONE writes read", sorted(calls["folder"]) == ["10", "13"] and calls["writes"] == 1)
for i in range(1, 19):                         # every 3 s up to 60 s after the first sight of app 10 (at +3 s)
    res = prov.poll(None, T0 + 6 + 3 * i)
check("polled every 3 s with flat signals: still downloading 57 s after the first look at it",
      {r["id"]: r["state"] for r in res["items"] if r["id"] in ("10", "13")} == {"10": "downloading", "13": "downloading"})
res = prov.poll(None, T0 + 63)
check("...app 10 is stalled at 60 s (flat folder, flat writes, frozen manifest), app 13 (first seen 3 s later) is not yet",
      {r["id"]: r["state"] for r in res["items"] if r["id"] in ("10", "13")} == {"10": "stalled", "13": "downloading"})
res = prov.poll(None, T0 + 66)
check("...and app 13 follows 3 s later", {r["id"]: r["state"] for r in res["items"] if r["id"] in ("10", "13")} == {"10": "stalled", "13": "stalled"})


def boom(*a):
    raise OSError("denied")


prov2 = steam.SteamProvider()
prov2.root_override, prov2.is_running = str(steam_dir), (lambda: True)
prov2.read_folder, prov2.read_writes = boom, boom
res = prov2.poll(None, T0)
check("a reader that raises is 'unavailable', the poll still answers", "items" in res and all(r["state"] != "stalled" for r in res["items"]))
res = prov2.poll(None, T0 + 500)
check("...and with both readers broken it is never stalled", all(r["state"] != "stalled" for r in res["items"]))
prov2.is_running = lambda: False
res = prov2.poll(None, T0 + 510)
check("Steam not running: the poll reports it and writes nothing (rows go stale)", "isn't running" in res.get("error", "") and "items" not in res)
res = steam.SteamProvider().poll(None)
check("poll() without a clock still works (uses the real one)", "items" in res or "error" in res)

# ------------------------------------------------------------------------------- the hub store: a burst is not a speed


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


clock = Clock()
store = Store(Path(HOME) / "hub.db", clock=clock)
files = {"100": (str(steam_dir), "G", man(100, 1026, 1000, 200, name="Burst"))}
memory = {}
providers.apply_poll(store, "steam", {"items": steam.parse_manifests(files, memory, clock(), {"100": {"folder": folder(), "writes": 1 * MB}}), "complete": True}, now=clock())
clock.advance(4)
files = {"100": (str(steam_dir), "G", man(100, 1026, 1000, 600, name="Burst"))}
providers.apply_poll(store, "steam", {"items": steam.parse_manifests(files, memory, clock(), {"100": {"folder": folder(), "writes": 1 * MB}}), "complete": True}, now=clock())
row = store.get_item("steam:100")
check("through the hub: bytes jumping 200 -> 600 in 4 s gives NO derived speed or ETA", row["state"] == "downloading" and not row["speed_bps"] and not row["eta_s"] and "speed" not in row["derived"])
check("...and no percent or done-bytes, but the total", row["percent"] is None and row["bytes_done"] is None and row["bytes_total"] == 1000)
check("...and the message (shown in the detail) says what Steam last reported", "60.0 %" in (row["message"] or "") and "live progress" in row["message"])
clock.advance(4)
files = {"100": (str(steam_dir), "G", man(100, 4, size=1000, updated=9, name="Burst"))}
providers.apply_poll(store, "steam", {"items": steam.parse_manifests(files, memory, clock(), None), "complete": True}, now=clock())
row = store.get_item("steam:100")
check("through the hub: when Steam says installed the row is done at 100 %", row["state"] == "done" and row["percent"] == 100)

# ------------------------------------------------------------------------------- hub/steam.py must stay inert
tree = ast.parse((ROOT / "jarvis-cli" / "jarvis" / "hub" / "steam.py").read_text(encoding="utf-8"))
imported = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imported |= {a.name.split(".")[0] for a in node.names}
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        imported.add((node.module or "").split(".")[0])
check("hub/steam.py imports no subprocess, socket, network or shell module", not (imported & {"subprocess", "socket", "urllib", "http", "requests", "ctypes", "shutil", "asyncio"}))
check("...only the standard modules it needs, and psutil lazily", imported <= {"os", "re", "sys", "time", "pathlib", "psutil", "winreg"})
src = (ROOT / "jarvis-cli" / "jarvis" / "hub" / "steam.py").read_text(encoding="utf-8")
check("...and it has no steam:// command except the Open in Steam link target", src.count("steam://") == 1 and "steam://nav/downloads" in src)

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
