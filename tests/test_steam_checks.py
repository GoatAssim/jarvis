"""steam-checks.py (brief 06, step 0): the read-only Steam experiment script, tested on fixtures.

No network, no real Steam, no psutil needed.  Covers: the second signals (downloading folder, content_log
tail, process-write readers), the false-stall judgement (N5), the lifecycle / cancel / uninstall record (N6),
the operator markers and what Steam wrote after them, the step coverage, the steam:// string scan and the
SteamCMD lookup (N12), and one synthetic end-to-end run of main() against a fake Steam folder that is
"downloaded", then "cancelled", while the script watches.

Run: python tests/test_steam_checks.py
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

HOME = tempfile.mkdtemp(prefix="jarvis-steamchecks-")
os.environ["JARVIS_HUB_NO_DELIVER"] = "1"
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "steam-checks.py"
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAILED: %s %s" % (name, detail))


def load():
    spec = importlib.util.spec_from_file_location("steam_checks_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = load()
tmp = Path(tempfile.mkdtemp(prefix="jarvis-steamchecks-fx-"))

# ------------------------------------------------------------------ dir_signal / app_dir_signal
lib = tmp / "lib"
dl = lib / "steamapps" / "downloading" / "620"
(dl / "sub").mkdir(parents=True)
(dl / "a.bin").write_bytes(b"x" * 1000)
(dl / "sub" / "b.bin").write_bytes(b"y" * 500)
sig = sc.dir_signal(dl)
check("dir: exists", sig["exists"] is True)
check("dir: total bytes over sub folders", sig["bytes"] == 1500, sig)
check("dir: file count", sig["files"] == 2)
check("dir: newest mtime is set", sig["newest_mtime"] > 0)
check("dir: missing folder", sc.dir_signal(tmp / "nope") == {"exists": False, "bytes": 0, "files": 0, "newest_mtime": 0.0, "partial": False})
app = sc.app_dir_signal([str(lib), str(tmp / "other")], "620")
check("app dir: summed over libraries, downloading folder", app["exists"] and app["bytes"] == 1500 and app["newest_age_s"] is not None)
temp_dir = lib / "steamapps" / "temp" / "620"
temp_dir.mkdir(parents=True)
(temp_dir / "t.bin").write_bytes(b"z" * 100)
check("app dir: temp folder is added", sc.app_dir_signal([str(lib)], "620")["bytes"] == 1600)
check("app dir: another app is empty", sc.app_dir_signal([str(lib)], "999")["exists"] is False)
old_cap = sc.DIR_FILE_CAP
sc.DIR_FILE_CAP = 1
check("dir: file cap marks partial", sc.dir_signal(dl)["partial"] is True)
sc.DIR_FILE_CAP = old_cap

# ------------------------------------------------------------------ LogTail
log = tmp / "content_log.txt"
log.write_text("old line one\nold line two\n", encoding="utf-8")
tail = sc.LogTail(log)
first = tail.read_new(["620"])
check("log: first look starts at the end", first["present"] and first["new_lines"] == 0)
with open(log, "a", encoding="utf-8") as fh:
    fh.write("AppID 620 state changed : Fully Installed,Update Running\nunrelated chatter\nAppID 70 update started\n")
second = tail.read_new(["620"])
check("log: counts all new lines", second["new_lines"] == 3, second)
check("log: keeps lines naming the app or a download word, drops the rest", len(second["kept"]) == 2 and not any("unrelated" in k for k in second["kept"]), second)
check("log: nothing new", tail.read_new(["620"])["new_lines"] == 0)
log.write_text("fresh after rotation: download resumed\n", encoding="utf-8")
rot = tail.read_new([])
check("log: rotation (file got smaller) is read from the start", rot["new_lines"] == 1 and tail.rotations == 1, rot)
check("log: missing file", sc.LogTail(tmp / "no.txt").read_new([])["present"] is False)
with open(log, "a", encoding="utf-8") as fh:
    fh.write("".join("download line %d\n" % i for i in range(40)))
check("log: at most 6 lines kept per tick", len(tail.read_new([])["kept"]) == 6)

# ------------------------------------------------------------------ process writes (PowerShell JSON) and SignalProbe
cim = json.dumps([{"Name": "steam.exe", "ProcessId": 11, "WriteTransferCount": 1000},
                  {"Name": "steam.exe", "ProcessId": 12, "WriteTransferCount": 500},
                  {"Name": "steamwebhelper.exe", "ProcessId": 13, "WriteTransferCount": 7}])
parsed = sc.parse_cim_json(cim)
check("cim: grouped by process name", sorted(parsed) == ["steam.exe", "steamwebhelper.exe"] and len(parsed["steam.exe"]) == 2)
check("cim: a single object (not a list) is accepted", sc.parse_cim_json('{"Name":"Steam.exe","ProcessId":1,"WriteTransferCount":5}') == {"steam.exe": [(1, 5)]})
check("cim: garbage and empty give {}", sc.parse_cim_json("not json") == {} and sc.parse_cim_json("") == {})

writes = [{"steam.exe": 1000}, {"steam.exe": 1000 + 5 * 1024 * 1024}, {"steam.exe": 1000 + 5 * 1024 * 1024}]
probe = sc.SignalProbe([str(lib)], str(tmp), io_reader=lambda: (writes.pop(0), "fake"))
p0 = probe.sample(100.0, ["620"])
check("probe: first tick has no movement", p0["dir"]["620"]["moved"] is False and p0["io"]["moved"] is False)
(dl / "c.bin").write_bytes(b"w" * 4096)
p1 = probe.sample(102.0, ["620"])
check("probe: folder growth is seen", p1["dir"]["620"]["moved"] and p1["dir"]["620"]["d_bytes"] == 4096, p1["dir"])
check("probe: process writes are seen with a rate", p1["io"]["moved"] and p1["io"]["rate"] == round(5 * 1024 * 1024 / 2.0), p1["io"])
p2 = probe.sample(104.0, ["620"])
check("probe: a flat tick is flat", not p2["dir"]["620"]["moved"] and not p2["io"]["moved"])
check("moved(): reports (moved, available)", sc.second_signal_moved(p1, "620") == (True, True) and sc.second_signal_moved(p2, "620") == (False, True)
      and sc.second_signal_moved(None, "620") == (False, False) and sc.second_signal_moved({"dir": {}, "io": None}, "620") == (False, False))
check("probe: want_io=False skips the reader", sc.SignalProbe([], None, io_reader=lambda: 1 / 0).sample(1.0, [], want_io=False)["io"] is None)
check("io reader never raises here", isinstance(sc.read_process_writes()[0], dict))

# ------------------------------------------------------------------ judge_stalls (N5)


def entry(t, state, dir_moved=None, io_moved=None, app="620"):
    sig = None
    if dir_moved is not None or io_moved is not None:
        sig = {"dir": {app: {"exists": dir_moved is not None, "moved": bool(dir_moved)}} if dir_moved is not None else {},
               "io": {"moved": bool(io_moved)} if io_moved is not None else None}
    return {"t": t, "app": app, "flags": 1026, "raw": {}, "row": {"state": state}, **({"sig": sig} if sig else {})}


false_stall = [entry(0, "downloading", True), entry(2, "downloading", True)] + [entry(4 + 2 * i, "stalled", True) for i in range(4)] + [entry(12, "downloading", True)]
v = sc.judge_stalls(false_stall)
check("stall: second signal moving = FALSE stall = FAIL", len(v) == 1 and v[0]["verdict"] == "FAIL" and "FALSE" in v[0]["why"], v)
real_long = [entry(2 * i, "stalled", False, False) for i in range(40)] + [entry(80, "downloading", True)]
v = sc.judge_stalls(real_long)
check("stall: flat for 60 s or more = PASS", len(v) == 1 and v[0]["verdict"] == "PASS" and v[0]["seconds"] >= 60, v)
real_short = [entry(2 * i, "stalled", False, False) for i in range(5)]
v = sc.judge_stalls(real_short)
check("stall: flat but under 60 s = WARN (too early by the N5 rule)", len(v) == 1 and v[0]["verdict"] == "WARN" and "60" in v[0]["why"], v)
no_signal = [entry(2 * i, "stalled") for i in range(40)]
v = sc.judge_stalls(no_signal)
check("stall: no second signal = WARN, never PASS", len(v) == 1 and v[0]["verdict"] == "WARN", v)
check("stall: only IO available and flat for 70 s = PASS", sc.judge_stalls([entry(2 * i, "stalled", None, False) for i in range(36)])[0]["verdict"] == "PASS")
check("stall: IO moved but folder flat = FAIL", sc.judge_stalls([entry(2 * i, "stalled", False, i == 3) for i in range(10)])[0]["verdict"] == "FAIL")
check("stall: no stalled rows = nothing to judge", sc.judge_stalls([entry(0, "downloading", True)]) == [])
two = [entry(0, "stalled", True), entry(2, "downloading", True), entry(4, "stalled", False, False)]
check("stall: two separate stretches are judged separately", len(sc.judge_stalls(two)) == 2)
check("stall: apps are judged separately", len(sc.judge_stalls([entry(0, "stalled", True, app="1"), entry(0, "stalled", False, False, app="2")])) == 2)

# ------------------------------------------------------------------ lifecycle (N6)


def st(flags, **raw):
    return {"flags": flags, "raw": {k: str(v) for k, v in raw.items()}}


check("life: manifest appeared", sc.lifecycle_events(None, st(1026)) == ["manifest_appeared"])
check("life: manifest removed (a cancel on a new install)", sc.lifecycle_events(st(1026, BytesDownloaded=5), None) == ["manifest_removed"])
ev = sc.lifecycle_events(st(1026, BytesDownloaded=0), st(1538, BytesDownloaded=40))
check("life: pause bit set + bytes written", "pause_bit_set" in ev and any(e.startswith("BytesDownloaded") for e in ev), ev)
check("life: pause bit cleared (resume)", "pause_bit_cleared" in sc.lifecycle_events(st(1538), st(1026)))
ev = sc.lifecycle_events(st(1026, BytesDownloaded=40), st(2, BytesDownloaded=40))
check("life: flags back to 2 without 4 = running bits cleared (a cancel of an update)", "running_bits_cleared_without_fully_installed" in ev, ev)
check("life: finish = running bits to 4", "finished (fully installed)" in sc.lifecycle_events(st(1026), st(4)))
check("life: uninstalling bit", "uninstalling_bit_set" in sc.lifecycle_events(st(4), st(2052)))
check("life: no change, no event", sc.lifecycle_events(st(4, BytesDownloaded=1), st(4, BytesDownloaded=1)) == [])

hist_cancel_new = [(0, None), (2, st(1026, BytesDownloaded=0)), (4, st(1026, BytesDownloaded=40)), (6, None)]
rec = sc.cancel_record("620", hist_cancel_new, False, True)
check("cancel record: manifest removed", rec and rec["kind"] == "manifest removed" and rec["manifest_present_at_end"] is False and rec["last_bytes_downloaded"] == "40" and rec["downloading_dir_exists"] is True, rec)
rec = sc.cancel_record("620", [(0, st(1026, BytesDownloaded=40)), (2, st(2, BytesDownloaded=40))], True, False)
check("cancel record: manifest kept, flags back to 2, bytes kept", rec and "flags back to 2" in rec["kind"] and rec["last_bytes_downloaded"] == "40", rec)
rec = sc.cancel_record("620", [(0, st(1026)), (2, st(2052)), (4, None)], False, False)
check("cancel record: uninstall named", rec and "uninstalling" in rec["kind"], rec)
check("cancel record: a finished download is not a cancel", sc.cancel_record("620", [(0, st(1026)), (2, st(4))], True, False) is None)
check("cancel record: an app that never ran is not a record", sc.cancel_record("620", [(0, st(4)), (2, None)], False, False) is None)
rec = sc.cancel_record("620", [(0, st(1026, BytesDownloaded=40)), (2, st(1538, BytesDownloaded=40))], True, True)
check("cancel record: paused is not a cancel", rec and rec["kind"].startswith("paused"), rec)

# ------------------------------------------------------------------ markers, coverage
timeline = [
    {"t": 1.0, "app": "620", "flags": 1026, "raw": {"BytesDownloaded": "0"}, "row": {"state": "downloading", "subtitle": "install"}},
    {"t": 10.0, "marker": "pause"},
    {"t": 12.0, "app": "620", "flags": 1538, "raw": {"BytesDownloaded": "40"}, "row": {"state": "paused", "subtitle": "install"}},
    {"t": 12.0, "app": "620", "event": "pause_bit_set"},
    {"t": 30.0, "marker": "cancel"},
    {"t": 32.0, "app": "620", "event": "manifest_removed"},
]
eff = sc.marker_effects(timeline)
check("markers: both found", [e["marker"] for e in eff] == ["pause", "cancel"])
check("markers: what the manifest wrote after the pause key", eff[0]["app_changes"].get("620") and eff[0]["app_changes"]["620"][0][1] == 1538 and eff[0]["app_changes"]["620"][0][2] == "40", eff[0])
check("markers: the removal after the cancel key", any("manifest_removed" in e for e in eff[1]["events"]), eff[1])
check("markers: key table", sc.MARKER_KEYS["p"] == "pause" and sc.MARKER_KEYS["c"] == "cancel" and sc.MARKER_KEYS["u"] == "uninstall")
entries = [e for e in timeline if "flags" in e]
events = [e for e in timeline if "event" in e]
cov = dict((n, s) for n, s, _e in sc.coverage(entries, events, [e for e in timeline if "marker" in e], False, 600, 600))
check("coverage: start, pause and cancel seen", cov["start"] and cov["pause"] and cov["cancel"])
check("coverage: resume, finish, update, staging, uninstall, exit NOT seen", not any(cov[k] for k in ("resume", "finish", "update", "staging", "uninstall", "steam exit")), cov)
cov2 = dict((n, s) for n, s, _e in sc.coverage([{"flags": 2097152 | 1024, "app": "1", "row": None}], [], [], True, 60, 60))
check("coverage: staging bit and a vanished Steam are seen", cov2["staging"] and cov2["steam exit"])

# ------------------------------------------------------------------ N12 research: steam:// strings, SteamCMD, accounts
blob = (b"\x00\x01junk steam://nav/downloads\x00 steam://install/%u steam://open/console\x00 "
        + "steam://pausedownload/".encode("utf-16-le") + b"\x00\x00 steam://x")
found = sc.find_steam_uri_strings(blob)
check("strings: ASCII uris found", "steam://nav/downloads" in found and "steam://install/%u" in found, found)
check("strings: UTF-16 uri found", any(f.startswith("steam://pausedownload") for f in found), found)
check("verb: arguments are dropped", sc.verb_of("steam://install/%u") == "install/%u" and sc.verb_of("steam://install/12345") == "install" and sc.verb_of("steam://nav/downloads") == "nav/downloads")
fake_steam = tmp / "Steam"
(fake_steam / "steamui").mkdir(parents=True)
(fake_steam / "steam.exe").write_bytes(b"MZ steam://nav/downloads steam://validate/%u")
(fake_steam / "steamui" / "library.js").write_text('openURL("steam://pausedownload/1"); x="steam://cancel_install"', encoding="utf-8")
(fake_steam / "steamui" / "big.png").write_bytes(b"steam://ignored-because-png")
n, total, uris, notes = sc.scan_steam_files_for_uris(str(fake_steam))
check("scan: files read, png skipped", n == 2 and "steam://ignored-because-png" not in uris, (n, uris))
check("scan: names the file each string came from", uris.get("steam://pausedownload/1") == {"library.js"} and "steam.exe" in uris.get("steam://nav/downloads", set()))
check("scan: control words picked from verbs", any(w in "cancel_install" for w in sc.CONTROL_WORDS) and "cancel_install" in {sc.verb_of(u) for u in uris})
check("scan: nothing is written into Steam's folder", sorted(p.name for p in fake_steam.iterdir()) == ["steam.exe", "steamui"])

cmd_home = Path(HOME) / "steamcmd"
cmd_home.mkdir()
check("steamcmd: none found on a clean home", sc.steamcmd_candidates(None) == [] or all("steamcmd" in str(p).lower() for p in sc.steamcmd_candidates(None)))
(cmd_home / "steamcmd.exe").write_bytes(b"MZ")
(cmd_home / "config").mkdir()
(cmd_home / "config" / "config.vdf").write_text('"InstallConfigStore"{}', encoding="utf-8")
cands = sc.steamcmd_candidates(None)
check("steamcmd: found in ~/steamcmd", any(p == cmd_home / "steamcmd.exe" for p in cands), cands)
info = sc.steamcmd_report(cmd_home / "steamcmd.exe")
check("steamcmd: report says config.vdf exists without reading it", info["has_config_vdf"] is True and info["has_package_folder"] is False)
(fake_steam / "config").mkdir()
(fake_steam / "config" / "loginusers.vdf").write_text('"users"{"1"{"AccountName" "someone"}"2"{"AccountName" "else"}}', encoding="utf-8")
check("accounts: counted, never returned by name", sc.count_saved_steam_accounts(str(fake_steam)) == 2)
check("accounts: missing file = None", sc.count_saved_steam_accounts(str(tmp / "nowhere")) is None)

# ------------------------------------------------------------------ end to end: a fake Steam, a download that the script watches, then a cancel
steam_root = tmp / "e2e" / "Steam"
apps = steam_root / "steamapps"
(apps / "downloading" / "620").mkdir(parents=True)
(steam_root / "logs").mkdir()
(steam_root / "logs" / "content_log.txt").write_text("startup\n", encoding="utf-8")
(apps / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % str(steam_root).replace("\\", "\\\\"), encoding="utf-8")


def manifest(flags, got=0):
    return ('"AppState"\n{\n\t"appid"\t\t"620"\n\t"name"\t\t"Fixture Game"\n\t"StateFlags"\t\t"%d"\n\t"installdir"\t\t"Fixture Game"\n'
            '\t"SizeOnDisk"\t\t"0"\n\t"LastUpdated"\t\t"0"\n\t"BytesToDownload"\t\t"1000000"\n\t"BytesDownloaded"\t\t"%d"\n'
            '\t"BytesToStage"\t\t"2000000"\n\t"BytesStaged"\t\t"0"\n}\n' % (flags, got))


(apps / "appmanifest_620.acf").write_text(manifest(1026), encoding="utf-8")
(steam_root / "steamapps" / "common" / "Fixture Game").mkdir(parents=True)


def play():
    """Steam 'downloads' for ~3 s (folder grows, manifest never changes), then the owner cancels: manifest + folder go."""
    end = time.time() + 3.0
    n = 0
    while time.time() < end:
        n += 1
        with open(apps / "downloading" / "620" / ("chunk%d.bin" % n), "wb") as fh:
            fh.write(b"d" * 200000)
        with open(steam_root / "logs" / "content_log.txt", "a", encoding="utf-8") as fh:
            fh.write("AppID 620 update: downloading chunk %d\n" % n)
        time.sleep(0.3)
    (apps / "appmanifest_620.acf").unlink()
    time.sleep(0.2)
    shutil.rmtree(apps / "downloading" / "620")


out_dir = tmp / "e2e-out"
sys.path.insert(0, str(ROOT / "jarvis-cli"))
from jarvis.hub import steam as steam_mod  # noqa: E402

steam_mod.steam_running = lambda: True            # the fake Steam "is running"
sc2 = load()
sc2.steam_process_running = lambda: (True, "fake")
before_files = {str(p.relative_to(steam_root)) for p in steam_root.rglob("*") if p.is_file()}
vdf_mtime = (apps / "libraryfolders.vdf").stat().st_mtime
player = threading.Thread(target=play, daemon=True)
argv = sys.argv
sys.argv = ["steam-checks.py", "--root", str(steam_root), "--watch", "6", "--interval", "0.5", "--out", str(out_dir), "--no-hub", "--no-open"]
player.start()
try:
    code = sc2.main()
finally:
    sys.argv = argv
player.join()
report = (out_dir / "steam-checks-report.txt").read_text(encoding="utf-8")
timeline_rows = [json.loads(line) for line in (out_dir / "steam-checks-timeline.jsonl").read_text(encoding="utf-8").splitlines()]

# N5 (brief 16): the hub no longer says "stalled" from the manifest alone, so the same fake download that
# used to produce a false stall now produces none: the folder grows, the manifest never changes, the row
# stays "downloading" and the run has no FAIL.
check("e2e: no FAIL in the run (the old false stall is gone)", code == 0 and not any(s == "FAIL" for s, t in sc2.RESULTS), [r for r in sc2.RESULTS if r[0] == "FAIL"])
check("e2e: Jarvis never said 'stalled' while the folder grew", any("never said 'stalled'" in t for s, t in sc2.RESULTS)
      and not any("stalled" in str((r.get("row") or {}).get("state")) for r in [json.loads(l) for l in (out_dir / "steam-checks-timeline.jsonl").read_text(encoding="utf-8").splitlines()]), [r for r in sc2.RESULTS if "stall" in r[1]])
check("e2e: '[PASS] stall was recognised' is gone", "stall was recognised" not in report)
check("e2e: the watch ran its full length", any(s == "PASS" and "full length" in t for s, t in sc2.RESULTS), [r for r in sc2.RESULTS if "watch" in r[1]])
check("e2e: no percent while the folder grew is an INFO naming N5 (on purpose), not a FAIL", any(s == "INFO" and "on purpose since N5" in t for s, t in sc2.RESULTS) and not any(s == "FAIL" and "percent" in t for s, t in sc2.RESULTS), [r for r in sc2.RESULTS if "percent" in r[1]])
check("e2e: the downloading rows in the timeline carry the honest message and no percent", any(
    (r.get("row") or {}).get("state") == "downloading" and "live progress" in str((r.get("row") or {}).get("message")) and (r.get("row") or {}).get("percent") is None
    for r in timeline_rows), [r.get("row") for r in timeline_rows if r.get("row")][:4])
check("e2e: the manifest removal was seen as an event", any(r.get("event") == "manifest_removed" and r.get("app") == "620" for r in timeline_rows), [r for r in timeline_rows if "event" in r])
check("e2e: the N6 record says manifest removed and that Jarvis would mark it unknown", "manifest removed" in report and "'unknown'" in report)
check("e2e: second signals are in the timeline", any((r.get("sig") or {}).get("dir", {}).get("620", {}).get("moved") for r in timeline_rows))
check("e2e: log lines are in the timeline", any(((r.get("sig_tick") or {}).get("log") or {}).get("kept") for r in timeline_rows))
check("e2e: step coverage is printed, with cancel seen and resume not", "step cancel was seen" in report and "step resume was NOT seen" in report, "")
check("e2e: the control research part ran and printed the options table", "Options for the owner's decision S2" in report and "REJECTED, not tried" in report)
check("e2e: nothing was written into the fake Steam folder by the script", {str(p.relative_to(steam_root)) for p in steam_root.rglob("*") if p.is_file()} <= before_files | {"steamapps/downloading/620/chunk%d.bin" % i for i in range(1, 40)}
      and (apps / "libraryfolders.vdf").stat().st_mtime == vdf_mtime)
check("e2e: the strings file is written to --out, not to Steam", (out_dir / "steam-control-strings.txt").is_file())

# a download that is really flat for the whole watch is not a FALSE stall (no folder, no writes): WARN at most
check("judge: flat stall under the window never FAILs", all(v["verdict"] != "FAIL" for v in sc.judge_stalls(real_short)))

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(HOME, ignore_errors=True)
print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
