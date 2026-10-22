#!/usr/bin/env python3
"""steam-checks.py -- does Jarvis's Steam support work against a REAL Steam?

The Stremio half was checked with stremio-checks.ps1 against the real server.  This is the same
idea for Steam (hub plan section 16, experiment 9, plus the phase-5 code in jarvis/hub/steam.py,
which so far has only been tested against fixture .acf files).

It never changes anything: it only READS Steam's folders and (optionally) the hub's database, and
it never touches steam.exe.  The only thing it can start is Steam's own Downloads page, and only
if you say yes.

Run it from anywhere (Python 3, no extra packages):

    python steam-checks.py                  # parts 1-5 and 7: static checks, about 10 seconds
    python steam-checks.py --watch 600      # + part 6: a 10-minute guided live watch (recommended; it runs its full length)
    python steam-checks.py --watch 600 --steam-speed 38   # + Steam's own speed (MB/s) to compare the second signals with

Put it in the repo root (next to jarvis-cli/) or pass --repo <folder with jarvis-cli>.

Parts
  1  Where Steam is, and is it running
  2  libraryfolders.vdf: an independent read vs Jarvis's parser
  3  Every appmanifest_*.acf: which fields exist, which StateFlags values are present, and an
     independent read of each field vs Jarvis's parser
  4  Jarvis's real Steam code on your files (find_libraries, read_manifests, parse_manifests, the
     provider's poll, hub doctor line)
  5  steam://nav/downloads: is the scheme registered; (asks) does it open the Downloads page
  6  Live watch (--watch N): you install / pause / resume / finish a small game in Steam while this
     samples Steam's files every --interval seconds and compares them with what Jarvis reports
  7  Through the real hub: dl_providers, dl_list(provider=steam), hub_doctor (is the Steam row in
     Menu > Downloads the same one this script sees?)
  8  Steam control research (N12): steam:// strings in Steam's own files, SteamCMD, a table of options.
     Read-only; nothing is started, stopped or driven.

Brief 06 (N5, N6, N12) added to part 6: while a download runs it records, side by side with the manifest,
the size and newest-file time of steamapps/downloading/<appid>/, the new lines of logs/content_log.txt and
the disk writes of the Steam processes; it records what the manifest does at a cancel and an uninstall; it
lets you press one key (s p r f c u g x n) to mark what you just did in Steam; and it no longer counts a
"stalled" row as a pass: a stall while a second signal still moves is a FAIL.

Output: a report (steam-checks-report.txt), a timeline (steam-checks-timeline.jsonl) and a list of
steam:// strings (steam-control-strings.txt) in --out (default ./steam-checks-out).  Send them back; they hold
game names, folder paths and the lines of content_log.txt that name an app or a download (CDN host names can be in
those: have a look before you send).  No account names, ids or tokens are read or written.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows consoles choke on a game called "Pokemon (with an accent) ...": never let printing crash the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

FIELDS = ["appid", "name", "StateFlags", "BytesToDownload", "BytesDownloaded", "BytesToStage",
          "BytesStaged", "SizeOnDisk", "LastUpdated", "installdir"]
FLAG_NAMES = {1: "Invalid", 2: "UpdateRequired", 4: "FullyInstalled", 8: "Encrypted", 16: "UpdateOptional",
              32: "FilesMissing", 64: "FilesCorrupt", 128: "UpdateRunningRaw", 256: "UpdateRunning",
              512: "UpdatePaused", 1024: "UpdateStarted", 2048: "Uninstalling", 4096: "BackupRunning",
              65536: "Reconfiguring", 131072: "Validating", 262144: "AddingFiles", 524288: "Preallocating",
              1048576: "Downloading", 2097152: "Staging", 4194304: "Committing", 8388608: "UpdateStopping"}
KNOWN_MASK = 2 | 4 | 256 | 512 | 1024 | 1048576 | 2097152 | 4194304      # the bits steam.py reasons about

REPORT = []          # lines of the report file
RESULTS = []         # (status, title) for the final tally


def say(text=""):
    print(text)
    REPORT.append(text)


def result(status, title, detail=""):
    """status: PASS | FAIL | WARN | INFO."""
    RESULTS.append((status, title))
    line = "[%-4s] %s" % (status, title)
    if detail:
        line += "  -- " + detail
    say(line)


def heading(text):
    say("")
    say("=" * 78)
    say(text)
    say("=" * 78)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def decode_flags(value):
    value = int(value or 0)
    names = [name for bit, name in sorted(FLAG_NAMES.items()) if value & bit]
    unknown = value & ~sum(FLAG_NAMES)
    if unknown:
        names.append("unknown(0x%x)" % unknown)
    return "+".join(names) or "0"


def ask_yes_no(question, default=False):
    if not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        answer = input("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith("y")


# --------------------------------------------------------------------------- repo + Jarvis import

def find_repo(arg):
    here = Path(__file__).resolve().parent
    for cand in ([Path(arg)] if arg else []) + [here, here.parent, Path.cwd(), Path.cwd().parent]:
        for base in (cand, cand / "jarvis-main"):
            if (base / "jarvis-cli" / "jarvis" / "hub" / "steam.py").is_file():
                return base
    return None


# --------------------------------------------------------------------------- independent readers
# Deliberately NOT the code under test: line regexes instead of Jarvis's tokenizer.

_KV = re.compile(r'^\s*"([^"]+)"\s+"((?:[^"\\]|\\.)*)"\s*$')


def raw_fields(text):
    """Top-level key/value pairs of an appmanifest (depth 1 only), straight from the lines."""
    out, depth = {}, 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "{":
            depth += 1
            continue
        if stripped == "}":
            depth -= 1
            continue
        match = _KV.match(line)
        if match and depth == 1:
            out.setdefault(match.group(1), match.group(2).replace("\\\\", "\\"))
    return out


def raw_library_paths(text):
    """Every "path" value in libraryfolders.vdf, plus old-format `"1" "D:\\\\Games"` lines."""
    paths = []
    for line in text.splitlines():
        match = _KV.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).replace("\\\\", "\\")
        if key.lower() == "path" or (key.isdigit() and re.match(r"^[A-Za-z]:[\\/]", value)):
            paths.append(value)
    return paths


def steam_process_running():
    """(running?, how we know).  Independent of jarvis's steam_running()."""
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True,
                                 text=True, timeout=15).stdout.lower()
            return "steam.exe" in out, "tasklist"
        out = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, text=True, timeout=10)
        return out.returncode == 0, "pgrep"
    except Exception as exc:  # noqa: BLE001
        return None, "could not tell (%s)" % exc


# --------------------------------------------------------------------------- second signals (brief 06, step 0)
# N5: Steam does not write BytesDownloaded into the manifest while it downloads, so the watch records
# three other signals SIDE BY SIDE with the manifest, to learn which one moves at Steam's own rate:
#   dir  -- total size and newest-file time of steamapps/downloading/<appid>/ (and steamapps/temp/<appid>/)
#   log  -- new lines of <Steam>/logs/content_log.txt (only lines that name the app or a download word)
#   io   -- disk-write counters of the Steam processes (psutil, else PowerShell CIM on Windows, else /proc)
# Everything here only READS.  Nothing starts, stops, freezes or drives Steam.

# The next two values are COPIES of the hub's rule (this script imports nothing from the hub at module
# level and stays standard-library only).  The originals are STALL_SECONDS and WRITES_MIN_BYTES_PER_S
# in jarvis-cli/jarvis/hub/steam.py; tests/test_hub_steam_alive.py fails if the copies drift apart.
STALL_REAL_S = 60.0          # == hub STALL_SECONDS: "stalled" only when the manifest and every readable second signal are flat this long
DIR_FILE_CAP = 20000         # a downloading folder with more files than this is sampled partially
IO_PROCESS_NAMES = ("steam.exe", "steamwebhelper.exe", "steamservice.exe", "steam", "steamwebhelper")
LOG_KEYWORDS = ("download", "paus", "resum", "stopp", "cancel", "update", "state changed", "app update",
                "uninstall", "staging", "commit", "queue", "suspend", "install")
LOG_LINE_CAP = 6             # lines kept per tick in the timeline
LOG_LINE_LEN = 200
IO_NOISE_BYTES = 64 * 1024   # == hub WRITES_MIN_BYTES_PER_S (32 KB/s) x this script's default 2 s tick: a smaller write delta is not a download


def dir_signal(folder):
    """{'exists','bytes','files','newest_mtime'} for a folder, walked without following links."""
    out = {"exists": False, "bytes": 0, "files": 0, "newest_mtime": 0.0, "partial": False}
    try:
        if not os.path.isdir(folder):
            return out
    except OSError:
        return out
    out["exists"] = True
    stack = [str(folder)]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    out["files"] += 1
                    out["bytes"] += stat.st_size
                    out["newest_mtime"] = max(out["newest_mtime"], stat.st_mtime)
                    if out["files"] >= DIR_FILE_CAP:
                        out["partial"] = True
                        return out
        except OSError:
            continue
    return out


def app_dir_signal(libraries, appid, now=None):
    """The downloading/ and temp/ folders of one app, summed over every library (an app lives in one)."""
    now = now if now is not None else time.time()
    total = {"exists": False, "bytes": 0, "files": 0, "newest_mtime": 0.0, "partial": False}
    for lib in libraries:
        for sub in ("downloading", "temp"):
            one = dir_signal(Path(lib) / "steamapps" / sub / str(appid))
            if one["exists"]:
                total["exists"] = True
                total["bytes"] += one["bytes"]
                total["files"] += one["files"]
                total["newest_mtime"] = max(total["newest_mtime"], one["newest_mtime"])
                total["partial"] = total["partial"] or one["partial"]
    total["newest_age_s"] = round(now - total["newest_mtime"], 1) if total["newest_mtime"] else None
    return total


class LogTail:
    """Reads what was appended to a log file since the last call.  Handles rotation (file got smaller)."""

    def __init__(self, path):
        self.path = str(path)
        self.offset = None
        self.rotations = 0

    def read_new(self, appids=()):
        """-> {'present','new_lines','kept':[...]}.  `kept` holds only lines naming an app id or a download word."""
        out = {"present": False, "new_lines": 0, "kept": []}
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return out
        out["present"] = True
        if self.offset is None:              # first look: start at the end, we only want what happens next
            self.offset = size
            return out
        if size < self.offset:
            self.offset = 0
            self.rotations += 1
        if size == self.offset:
            return out
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(min(size - self.offset, 512 * 1024))
                self.offset += len(data)
        except OSError:
            return out
        text = data.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        out["new_lines"] = len(lines)
        ids = [str(a) for a in appids if a]
        for ln in lines:
            low = ln.lower()
            if any(i in ln for i in ids) or any(k in low for k in LOG_KEYWORDS):
                out["kept"].append(ln.strip()[:LOG_LINE_LEN])
        out["kept"] = out["kept"][-LOG_LINE_CAP:]
        return out


def parse_cim_json(text):
    """PowerShell `Get-CimInstance Win32_Process | ConvertTo-Json` output -> {name: [(pid, write_bytes)]}."""
    try:
        data = json.loads(text) if text and text.strip() else []
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = [data]
    out = {}
    for item in data if isinstance(data, list) else []:
        try:
            name = str(item.get("Name") or "").lower()
            out.setdefault(name, []).append((int(item.get("ProcessId")), int(item.get("WriteTransferCount") or 0)))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def read_process_writes():
    """-> ({process name: total bytes written so far}, how) or ({}, reason).  Read-only."""
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        psutil = None
    if psutil is not None:
        totals = {}
        try:
            for proc in psutil.process_iter(["name"]):
                name = str(proc.info.get("name") or "").lower()
                if name in IO_PROCESS_NAMES:
                    try:
                        totals[name] = totals.get(name, 0) + int(proc.io_counters().write_bytes)
                    except Exception:  # noqa: BLE001 - access denied for one process is fine
                        continue
            return totals, ("psutil" if totals else "psutil: no Steam process was found or readable (is Steam running? access denied?)")
        except Exception as exc:  # noqa: BLE001
            return {}, "psutil failed (%s)" % exc
    if sys.platform.startswith("win"):
        names = " OR ".join("Name='%s'" % n for n in IO_PROCESS_NAMES if n.endswith(".exe"))
        cmd = ("Get-CimInstance Win32_Process -Filter \"%s\" | Select-Object Name,ProcessId,WriteTransferCount | "
               "ConvertTo-Json -Compress" % names)
        try:
            raw = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                                 text=True, timeout=15).stdout
        except Exception as exc:  # noqa: BLE001
            return {}, "PowerShell failed (%s)" % exc
        parsed = parse_cim_json(raw)
        return {n: sum(w for _p, w in rows) for n, rows in parsed.items()}, "PowerShell CIM"
    if os.path.isdir("/proc"):
        totals = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % pid, encoding="utf-8", errors="replace") as fh:
                    name = fh.read().strip().lower()
                if name not in IO_PROCESS_NAMES:
                    continue
                with open("/proc/%s/io" % pid, encoding="utf-8") as fh:
                    for ln in fh:
                        if ln.startswith("write_bytes:"):
                            totals[name] = totals.get(name, 0) + int(ln.split(":")[1])
            except (OSError, ValueError):
                continue
        return totals, "/proc"
    return {}, "no way to read process I/O here"


class SignalProbe:
    """One object per watch: gives every tick's second signals, with the change since the previous tick."""

    def __init__(self, libraries, steam_root, io_reader=None):
        self.libraries = list(libraries)
        self.log = LogTail(Path(steam_root) / "logs" / "content_log.txt") if steam_root else None
        self.io_reader = io_reader or read_process_writes
        self.prev_dir, self.prev_io, self.prev_t = {}, None, None
        self.io_how = None

    def sample(self, now, appids, want_io=True):
        sig = {"dir": {}, "log": None, "io": None}
        for appid in appids:
            cur = app_dir_signal(self.libraries, appid, now)
            prev = self.prev_dir.get(appid)
            cur["d_bytes"] = (cur["bytes"] - prev["bytes"]) if prev else 0
            cur["moved"] = bool(prev) and (cur["d_bytes"] != 0 or cur["newest_mtime"] > prev["newest_mtime"])
            self.prev_dir[appid] = cur
            sig["dir"][appid] = cur
        if self.log is not None:
            sig["log"] = self.log.read_new(appids)
        totals, how = self.io_reader() if want_io else ({}, self.io_how)
        self.io_how = how
        if totals:
            prev, dt = self.prev_io, (now - self.prev_t) if self.prev_t else 0
            delta = {n: max(0, totals[n] - prev.get(n, totals[n])) for n in totals} if prev else {n: 0 for n in totals}
            all_delta = sum(delta.values())
            sig["io"] = {"writes": totals, "delta": delta, "delta_total": all_delta,
                         "rate": round(all_delta / dt) if dt > 0 else 0, "moved": all_delta >= IO_NOISE_BYTES}
            self.prev_io = totals
            self.prev_t = now
        return sig


def second_signal_moved(sig, appid):
    """(moved?, available?) for one app: did any second signal move this tick."""
    if not sig:
        return False, False
    available, moved = False, False
    one = (sig.get("dir") or {}).get(appid)
    if one and one.get("exists"):
        available = True
        moved = moved or one.get("moved", False)
    if sig.get("io"):
        available = True
        moved = moved or sig["io"].get("moved", False)
    return moved, available


def judge_stalls(entries, window_s=STALL_REAL_S):
    """Judge every stretch of ticks where Jarvis said 'stalled', against the second signal.

    `entries` are the watch's per-app timeline entries: {'t', 'app', 'row': {'state'}, 'sig': {...}}.
    Returns a list of {'app','from_t','to_t','seconds','verdict','why'} where verdict is
    FAIL (second signal moved: a FALSE stall), PASS (flat for the whole stretch and for at least
    `window_s`), WARN (flat but shorter than the window, or no second signal at all).
    """
    by_app = {}
    for e in entries:
        if "app" in e and "flags" in e:           # manifest samples only, not lifecycle events
            by_app.setdefault(e["app"], []).append(e)
    out = []
    for app, rows in by_app.items():
        rows.sort(key=lambda r: r["t"])
        stretch = []
        for r in rows + [None]:
            stalled = bool(r) and (r.get("row") or {}).get("state") == "stalled"
            if stalled:
                stretch.append(r)
                continue
            if stretch:
                out.append(_judge_one(app, stretch, window_s))
                stretch = []
    return out


def _judge_one(app, stretch, window_s):
    first, last = stretch[0]["t"], stretch[-1]["t"]
    seconds = round(last - first, 1)
    moved_ticks, available = 0, False
    for r in stretch:
        moved, avail = second_signal_moved(r.get("sig"), app)
        available = available or avail
        moved_ticks += 1 if moved else 0
    info = {"app": app, "from_t": first, "to_t": last, "seconds": seconds}
    if not available:
        info.update(verdict="WARN", why="no second signal was available (no downloading folder, no I/O reading): the stall cannot be judged")
    elif moved_ticks:
        info.update(verdict="FAIL", why="FALSE stall: the second signal moved in %d of %d stalled ticks while Jarvis said 'stalled'" % (moved_ticks, len(stretch)))
    elif seconds < window_s:
        info.update(verdict="WARN", why="the second signal was flat, but 'stalled' was shown after only %.0f s; the N5 rule wants at least %.0f s" % (seconds, window_s))
    else:
        info.update(verdict="PASS", why="the second signal was flat for the whole %.0f s: a real stall" % seconds)
    return info


# --------------------------------------------------------------------------- cancel / uninstall evidence (N6)

def lifecycle_events(prev, cur):
    """Compare the previous and current manifest state of ONE app -> list of event strings.

    prev / cur are None (manifest absent) or {'flags': int, 'raw': {...}}.
    """
    events = []
    if prev is None and cur is not None:
        events.append("manifest_appeared")
    elif prev is not None and cur is None:
        events.append("manifest_removed")
    elif prev is not None and cur is not None:
        if prev["flags"] != cur["flags"]:
            events.append("flags %s -> %s" % (prev["flags"], cur["flags"]))
            if not (prev["flags"] & 512) and cur["flags"] & 512:
                events.append("pause_bit_set")
            if prev["flags"] & 512 and not (cur["flags"] & 512):
                events.append("pause_bit_cleared")
            if cur["flags"] & 2048:
                events.append("uninstalling_bit_set")
            if prev["flags"] & (256 | 1024 | 1048576) and not cur["flags"] & (256 | 1024 | 1048576 | 512 | 2097152 | 4194304) and not cur["flags"] & 4:
                events.append("running_bits_cleared_without_fully_installed")
            if prev["flags"] & (256 | 1024 | 1048576) and cur["flags"] & 4 and not cur["flags"] & (256 | 1024 | 1048576):
                events.append("finished (fully installed)")
        for field in ("BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged"):
            if prev["raw"].get(field) != cur["raw"].get(field):
                events.append("%s %s -> %s" % (field, prev["raw"].get(field), cur["raw"].get(field)))
    return events


def cancel_record(app, history, install_dir_exists, downloading_dir_exists):
    """One app's end-of-watch record for N6, from its manifest history.

    history: list of (t, state or None) where state is {'flags','raw'} or None, one per tick it was known.
    Returns a dict, or None when the app never ran or ended fully installed with its manifest in place.
    """
    ran = any(s and s["flags"] & (256 | 1024 | 1048576) for _t, s in history)
    if not ran:
        return None
    last_state = None
    for _t, s in history:
        if s is not None:
            last_state = s
    final = history[-1][1]
    if final is not None and final["flags"] & 4 and not final["flags"] & (256 | 1024 | 1048576 | 512):
        return None
    rec = {"app": app, "manifest_present_at_end": final is not None,
           "last_flags": last_state["flags"] if last_state else None,
           "last_bytes_downloaded": last_state["raw"].get("BytesDownloaded") if last_state else None,
           "last_bytes_to_download": last_state["raw"].get("BytesToDownload") if last_state else None,
           "install_dir_exists": install_dir_exists, "downloading_dir_exists": downloading_dir_exists}
    if final is None:
        rec["kind"] = "manifest removed" + (" after an uninstalling bit" if any(s and s["flags"] & 2048 for _t, s in history) else "")
    elif final["flags"] & 512:
        rec["kind"] = "paused (manifest kept, pause bit set)"
    elif final["flags"] & 2:
        rec["kind"] = "manifest kept, flags back to %s (update still required)" % final["flags"]
    else:
        rec["kind"] = "manifest kept, flags %s" % final["flags"]
    return rec


# --------------------------------------------------------------------------- what the owner did, and what Steam wrote next

MARKER_KEYS = {"s": "start", "p": "pause", "r": "resume", "f": "finished", "c": "cancel", "u": "uninstall",
               "x": "steam-exit", "g": "update", "n": "note"}


def marker_effects(timeline, after_s=15.0):
    """For every operator marker in the timeline: what the manifests did in the `after_s` seconds after it.

    Returns [{'marker','t','app_changes': {app: [(t_rel, flags, bytes_downloaded)]}, 'events': [...]}].
    """
    out = []
    marks = [e for e in timeline if "marker" in e]
    for m in marks:
        changes, events = {}, []
        before = {}
        for e in timeline:
            if "app" in e and "flags" in e and e["t"] <= m["t"]:
                before[e["app"]] = e
        for e in timeline:
            if "event" in e and m["t"] <= e["t"] <= m["t"] + after_s:
                events.append("+%.0fs app %s: %s" % (e["t"] - m["t"], e.get("app"), e["event"]))
            if "app" in e and "flags" in e and m["t"] < e["t"] <= m["t"] + after_s:
                prev = before.get(e["app"])
                if prev is None or prev["flags"] != e["flags"] or (prev["raw"] or {}).get("BytesDownloaded") != (e["raw"] or {}).get("BytesDownloaded"):
                    changes.setdefault(e["app"], []).append((round(e["t"] - m["t"], 1), e["flags"], (e["raw"] or {}).get("BytesDownloaded")))
                    before[e["app"]] = e
        out.append({"marker": m["marker"], "t": m["t"], "app_changes": changes, "events": events})
    return out


def coverage(entries, events, markers, steam_gone_seen, requested_s, ran_s):
    """Which of the steps the brief asks for were actually seen.  -> ordered [(step, seen_bool, evidence)]."""
    flags_seen = {e["flags"] for e in entries if "app" in e and "flags" in e}
    any_flag = lambda mask: any(f & mask for f in flags_seen)  # noqa: E731
    texts = [ev.get("event", "") for ev in events]
    marker_names = {m["marker"] for m in markers}
    steps = [
        ("start", any_flag(256 | 1024 | 1048576), "a running bit (256 / 1024 / 1048576) was seen"),
        ("pause", any(f & 512 and f & (256 | 1024 | 1048576) for f in flags_seen) or "pause" in marker_names,
         "the pause bit 512 with a running bit, or you pressed p"),
        ("resume", any("pause_bit_cleared" in t for t in texts) or "resume" in marker_names, "the pause bit went away again, or you pressed r"),
        ("finish", any("finished" in t for t in texts) or "finished" in marker_names, "running bits -> fully installed (4)"),
        ("update", "update" in marker_names or any((e.get("row") or {}).get("subtitle") == "update" for e in entries if "app" in e and "flags" in e),
         "a row labelled update, or you pressed g"),
        ("staging", any_flag(2097152 | 4194304), "the Staging / Committing bits (2097152 / 4194304) were seen"),
        ("cancel", any("manifest_removed" in t or "running_bits_cleared_without_fully_installed" in t for t in texts) or "cancel" in marker_names,
         "manifest removed or running bits cleared without 4, or you pressed c"),
        ("uninstall", any("uninstalling_bit_set" in t for t in texts) or "uninstall" in marker_names, "the Uninstalling bit 2048, or you pressed u"),
        ("steam exit", steam_gone_seen or "steam-exit" in marker_names, "the Steam process went away, or you pressed x"),
    ]
    return [(name, bool(seen), ev) for name, seen, ev in steps]


# --------------------------------------------------------------------------- operator markers (single keys during the watch)

class Markers:
    """Lets you press one key during the watch to say what you just did in Steam.

    Windows: any key (msvcrt).  Elsewhere: a letter then Enter, only when stdin is a terminal.
    No key is ever required; without a terminal this does nothing.
    """

    def __init__(self):
        self.ok = bool(sys.stdin) and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        self.win = sys.platform.startswith("win")

    def poll(self):
        """-> a marker name or None."""
        if not self.ok:
            return None
        try:
            if self.win:
                import msvcrt  # noqa: PLC0415
                if not msvcrt.kbhit():
                    return None
                ch = msvcrt.getwch().lower()
            else:
                import select  # noqa: PLC0415
                if not select.select([sys.stdin], [], [], 0)[0]:
                    return None
                line = sys.stdin.readline().strip().lower()
                ch = line[:1]
        except Exception:  # noqa: BLE001
            return None
        return MARKER_KEYS.get(ch)


# --------------------------------------------------------------------------- Steam-control research (N12): reads files only

CONTROL_WORDS = ("download", "pause", "resume", "cancel", "stop", "install", "uninstall", "validate", "update",
                 "abort", "queue", "priority", "suspend", "console")
_STR_ASCII = re.compile(rb"steam://[A-Za-z0-9_./%\-]{2,80}")
_STR_WIDE = re.compile(rb"(?:s\x00t\x00e\x00a\x00m\x00:\x00/\x00/\x00)(?:[A-Za-z0-9_/.\-]\x00){2,80}")
SCAN_TOTAL_CAP = 220 * 1024 * 1024
SCAN_FILE_CAP = 80 * 1024 * 1024
SCAN_SECONDS = 90


def find_steam_uri_strings(data):
    """Every distinct `steam://...` string in a block of bytes (ASCII and UTF-16LE).  Arguments after the verb are kept."""
    found = set()
    for m in _STR_ASCII.finditer(data):
        found.add(m.group(0).decode("ascii", "replace"))
    for m in _STR_WIDE.finditer(data):
        found.add(m.group(0).decode("utf-16-le", "replace"))
    return found


def verb_of(uri):
    """steam://nav/downloads -> 'nav/downloads' ; steam://install/12345 -> 'install'.  Numbers are arguments, not verbs."""
    parts = [p for p in uri[len("steam://"):].split("/") if p]
    keep = []
    for part in parts[:2]:
        if part.isdigit():
            break
        keep.append(part)
    return "/".join(keep) or "(none)"


def scan_steam_files_for_uris(steam_root, budget_s=SCAN_SECONDS):
    """Look through Steam's own program files for steam:// strings.  Returns (files_scanned, bytes, {uri: {files}}, notes).

    This only reads bytes.  A string being there is NOT proof that the command works or does what its name says.
    """
    root = Path(steam_root)
    candidates = [root / "steam.exe", root / "steamclient64.dll", root / "steamclient.dll", root / "steam.sh"]
    for sub in ("steamui", "resource", "clientui"):
        folder = root / sub
        if folder.is_dir():
            for dirpath, _dirs, files in os.walk(folder):
                for name in files:
                    if name.lower().endswith((".js", ".json", ".txt", ".res", ".html", ".dll", ".exe", ".css", ".map")):
                        candidates.append(Path(dirpath) / name)
    found, notes, scanned, total = {}, [], 0, 0
    started = time.time()
    for path in candidates:
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size > SCAN_FILE_CAP:
            notes.append("skipped %s (%s, over the per-file cap)" % (path.name, human(size)))
            continue
        if total + size > SCAN_TOTAL_CAP or time.time() - started > budget_s:
            notes.append("stopped after %d files: the size or time cap was reached" % scanned)
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        scanned += 1
        total += len(data)
        for uri in find_steam_uri_strings(data):
            found.setdefault(uri, set()).add(path.name)
    return scanned, total, found, notes


def steamcmd_candidates(steam_root=None, extra=()):
    """Places a SteamCMD could be, in order.  Nothing here runs it."""
    out = []
    which = shutil.which("steamcmd") or shutil.which("steamcmd.exe")
    if which:
        out.append(Path(which))
    names = ("steamcmd.exe", "steamcmd.sh", "steamcmd")
    homes = [Path.home(), Path.home() / "steamcmd", Path.home() / "Downloads" / "steamcmd", Path.home() / "Desktop" / "steamcmd"]
    roots = [Path(p) for p in (r"C:\steamcmd", r"D:\steamcmd", r"C:\SteamCMD", r"C:\Program Files\SteamCMD",
                               r"C:\Program Files (x86)\SteamCMD", r"C:\Tools\steamcmd")]
    if steam_root:
        roots += [Path(steam_root) / "steamcmd", Path(steam_root).parent / "steamcmd"]
    roots += [Path(p) for p in extra]
    for base in homes + roots:
        for name in names:
            cand = base / name
            if cand not in out:
                out.append(cand)
    return [p for p in out if p.is_file()]


def steamcmd_report(path):
    """What can be said about an installed SteamCMD without running it (running it would update itself)."""
    base = path.parent
    info = {"path": str(path), "has_config_vdf": (base / "config" / "config.vdf").is_file(),
            "has_package_folder": (base / "package").is_dir(), "has_logs": (base / "logs").is_dir()}
    return info


def count_saved_steam_accounts(steam_root):
    """How many accounts Steam has remembered on this PC.  Counts entries only; account names are never read out."""
    path = Path(steam_root) / "config" / "loginusers.vdf"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return len(re.findall(r'"AccountName"', text))


# --------------------------------------------------------------------------- the parts

class Ctx:
    steam_mod = None
    steam_root = None
    libraries = []
    manifest_files = []     # [(library, path)]


def part1_locate(args):
    heading("1. Where Steam is, and is it running")
    say("platform: %s   python: %s" % (sys.platform, sys.version.split()[0]))
    candidates = []
    if args.root:
        candidates.append(("--root", args.root))
    env = os.environ.get("JARVIS_STEAM_ROOT", "").strip()
    if env:
        candidates.append(("JARVIS_STEAM_ROOT", env))
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                reg = winreg.QueryValueEx(key, "SteamPath")[0]
            candidates.append(("registry HKCU\\Software\\Valve\\Steam\\SteamPath", reg))
            say("registry SteamPath = %s" % reg)
        except (ImportError, OSError) as exc:
            result("WARN", "Steam's registry key could not be read", str(exc))
        candidates += [("default", r"C:\Program Files (x86)\Steam"), ("default", r"C:\Program Files\Steam")]
    else:
        home = Path.home()
        candidates += [("default", str(home / ".steam" / "steam")), ("default", str(home / ".local" / "share" / "Steam"))]
    chosen = None
    for source, cand in candidates:
        if (Path(cand) / "steamapps").is_dir():
            chosen = (source, str(Path(cand)))
            break
    if not chosen:
        result("FAIL", "Steam folder not found", "tried: " + "; ".join(c for _s, c in candidates) +
               "   (pass --root <your Steam folder>)")
        return False
    Ctx.steam_root = chosen[1]
    result("PASS", "Steam folder found", "%s (from %s)" % (chosen[1], chosen[0]))
    running, how = steam_process_running()
    if running is None:
        result("INFO", "could not tell whether Steam is running", how)
    else:
        result("INFO", "Steam is %s" % ("running" if running else "NOT running"), "via " + how)
        if not running:
            say("       (Jarvis shows Steam rows as stale, not failed, when Steam is closed during a download.)")
    return True


def part2_libraries(args):
    heading("2. libraryfolders.vdf")
    vdf = Path(Ctx.steam_root) / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        result("FAIL", "libraryfolders.vdf is missing", str(vdf))
        Ctx.libraries = [Ctx.steam_root]
        return
    text = vdf.read_text(encoding="utf-8", errors="replace")
    result("INFO", "libraryfolders.vdf size", "%d bytes (Jarvis reads at most %d)" % (len(text.encode("utf-8")), Ctx.steam_mod.MAX_FILE_BYTES))
    mine = raw_library_paths(text)
    say("libraries named in the file (independent read): %s" % (mine or "none"))
    jarvis_root, jarvis_libs = Ctx.steam_mod.find_libraries(Ctx.steam_root)
    say("libraries Jarvis keeps (exist on disk):          %s" % jarvis_libs)
    norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
    on_disk = [p for p in mine if (Path(p) / "steamapps").is_dir()]
    expected = {norm(Ctx.steam_root)} | {norm(p) for p in on_disk}
    got = {norm(p) for p in jarvis_libs}
    if len(jarvis_libs) != len(got):
        result("FAIL", "Jarvis lists the same library more than once (%d entries, %d distinct)" % (len(jarvis_libs), len(got)),
               "usually the registry's lower-case Steam path vs libraryfolders.vdf's spelling; fixed by jarvis-steam-real-run-fixes.patch")
    elif expected == got:
        result("PASS", "Jarvis finds the same libraries as an independent read", "%d librar%s" % (len(got), "y" if len(got) == 1 else "ies"))
    else:
        result("FAIL", "Jarvis's library list differs from the independent read",
               "only Jarvis: %s | only independent: %s" % (sorted(got - expected), sorted(expected - got)))
    missing = [p for p in mine if not (Path(p) / "steamapps").is_dir()]
    if missing:
        result("INFO", "libraries named in the file but not on disk (unplugged drive?)", "; ".join(missing))
    Ctx.libraries = jarvis_libs or [Ctx.steam_root]


def part3_manifests(args):
    heading("3. appmanifest_*.acf files (field names, StateFlags, independent read)")
    total, flag_counts, field_missing, mismatches = 0, {}, {}, []
    rows = []
    for lib in Ctx.libraries:
        folder = Path(lib) / "steamapps"
        for name in sorted(os.listdir(folder)):
            if not (name.startswith("appmanifest_") and name.endswith(".acf")):
                continue
            path = folder / name
            Ctx.manifest_files.append((lib, path))
            total += 1
            size = path.stat().st_size
            text = path.read_text(encoding="utf-8", errors="replace")
            raw = raw_fields(text)
            for field in FIELDS:
                if field not in raw:
                    field_missing.setdefault(field, []).append(name)
            flags = int(raw.get("StateFlags", "0") or 0)
            flag_counts[flags] = flag_counts.get(flags, 0) + 1
            if size > Ctx.steam_mod.MAX_FILE_BYTES:
                result("WARN", "%s is %d bytes, over Jarvis's %d-byte cap: it would be skipped" % (name, size, Ctx.steam_mod.MAX_FILE_BYTES))
            # Jarvis's parser vs the independent read, field by field.
            tree = Ctx.steam_mod.parse_vdf(text)
            state = Ctx.steam_mod._ci(tree, "AppState", tree)
            for field in FIELDS:
                if field in raw:
                    theirs = str(Ctx.steam_mod._ci(state, field, ""))
                    if theirs != raw[field]:
                        mismatches.append("%s.%s: independent=%r jarvis=%r" % (name, field, raw[field], theirs))
            rows.append((raw.get("appid", "?"), raw.get("name", "?"), flags, raw.get("BytesToDownload", "-"),
                         raw.get("BytesDownloaded", "-"), raw.get("SizeOnDisk", "-"), lib))
    ids = [r[0] for r in rows]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        result("WARN", "the same manifest was listed twice (same app id in two library entries)", ", ".join(dup_ids[:5]))
    result("INFO", "manifests found", "%d across %d librar%s" % (total, len(Ctx.libraries), "y" if len(Ctx.libraries) == 1 else "ies"))
    if total == 0:
        result("WARN", "no appmanifest files: nothing to compare (install a small game and re-run)")
        return
    if total > Ctx.steam_mod.MAX_MANIFESTS:
        result("WARN", "more manifests than Jarvis's cap", "%d > %d: the extra ones are never read" % (total, Ctx.steam_mod.MAX_MANIFESTS))
    say("")
    say("%-9s %-34s %-10s %-34s %12s %12s" % ("appid", "name", "StateFlags", "meaning", "toDownload", "downloaded"))
    for appid, name, flags, to_dl, got, _size, _lib in rows[:60]:
        say("%-9s %-34s %-10s %-34s %12s %12s" % (appid, name[:34], flags, decode_flags(flags)[:34], to_dl, got))
    if len(rows) > 60:
        say("... and %d more" % (len(rows) - 60))
    say("")
    say("StateFlags values present: " + ", ".join("%s x%d (%s)" % (f, n, decode_flags(f)) for f, n in sorted(flag_counts.items())))
    plan_fields = [f for f in FIELDS if f not in field_missing]
    result("PASS" if len(plan_fields) >= 6 else "WARN", "field names the plan relies on exist",
           "present in every manifest: %s" % ", ".join(f for f in FIELDS if f not in field_missing))
    for field, names in field_missing.items():
        # Steam only writes the download fields while something is being fetched: that is normal.
        status = "INFO" if field in ("BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged") else "WARN"
        result(status, "field %r is absent from %d of %d manifests" % (field, len(names), total),
               "normal for the Bytes* fields of an idle game" if status == "INFO" else "e.g. " + ", ".join(names[:3]))
    if mismatches:
        result("FAIL", "Jarvis's parser disagrees with the independent read on %d value(s)" % len(mismatches), "; ".join(mismatches[:5]))
    else:
        result("PASS", "Jarvis's parser agrees with the independent read on every field of every manifest")
    odd = {f: n for f, n in flag_counts.items() if f & ~KNOWN_MASK and f not in (4, 6)}
    if odd:
        result("INFO", "StateFlags values with bits Jarvis doesn't reason about", ", ".join("%s (%s)" % (f, decode_flags(f)) for f in odd))


def part4_jarvis_code(args):
    heading("4. Jarvis's Steam code on your real files")
    steam = Ctx.steam_mod
    cache = {}
    files = steam.read_manifests(Ctx.libraries, cache)
    on_disk_ids = set()
    for _lib, path in Ctx.manifest_files:
        match = re.match(r"appmanifest_(\d+)\.acf$", path.name)
        if match:
            on_disk_ids.add(match.group(1))
    lost = on_disk_ids - set(files)
    if not on_disk_ids:
        result("INFO", "no manifests, so read_manifests has nothing to prove")
    elif lost:
        result("FAIL", "read_manifests dropped manifests that are on disk", "app ids: %s" % sorted(lost)[:10])
    else:
        result("PASS", "read_manifests returned every manifest on disk", "%d app(s)" % len(files))
    again = steam.read_manifests(Ctx.libraries, cache)
    result("PASS" if again.keys() == files.keys() else "FAIL", "second read through the mtime cache gives the same apps")

    memory = {}
    rows = steam.parse_manifests(files, memory)
    active_by_flags = []
    for appid, (_lib, _dir, parsed) in files.items():
        state = steam._ci(parsed, "AppState", parsed)
        flags = steam._int(steam._ci(state, "StateFlags"))
        has_bytes = any(steam._int(steam._ci(state, f)) > 0 for f in ("BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged"))
        if flags & (steam._RUNNING | steam._STAGING) or (flags & steam.FLAG_UPDATE_PAUSED and has_bytes):
            active_by_flags.append(appid)
    say("rows Jarvis would show right now: %d   (apps whose flags and bytes say 'being fetched': %d)" % (len(rows), len(active_by_flags)))
    for row in rows:
        say("   %-9s %-30s %-14s %5s%%  %s -> dest %s" % (row["id"], row["title"][:30], row["state"], row.get("percent"),
                                                        row["subtitle"], row.get("dest", "-")))
    shown = {r["id"] for r in rows}
    if set(active_by_flags) == shown:
        result("PASS", "the rows are exactly the apps Steam is fetching", "none shown for idle or merely-pending-update games")
    else:
        result("FAIL", "rows and flags disagree", "flags say %s, rows say %s" % (sorted(active_by_flags), sorted(shown)))
    for row in rows:
        dest = row.get("dest")
        if dest and not Path(dest).exists():
            result("WARN", "row %s's install folder does not exist yet" % row["id"], dest + " (normal for a brand-new install)")
        elif dest:
            result("PASS", "row %s's install folder exists" % row["id"], dest)

    # The provider itself, exactly as the hub daemon calls it, with a store that records nothing.
    class StubStore:
        def get_provider(self, _pid):
            return {"settings": {"root": Ctx.steam_root}}

    provider = steam.SteamProvider()
    reg = provider.registration(StubStore())
    result("PASS" if reg and reg.get("mode") == "poll" else "FAIL", "provider registration", json.dumps(reg)[:160] if reg else "None")
    report = provider.poll(StubStore(), time.time())
    if "error" in report:
        status = "INFO" if "isn't running" in report["error"] else "FAIL"
        result(status, "provider.poll returned an error", report["error"])
    else:
        result("PASS", "provider.poll returned a report", "%d item(s), complete=%s" % (len(report.get("items", [])), report.get("complete")))
    for line in steam.doctor(StubStore()):
        result("PASS" if line["status"] == "ok" else "WARN", "hub_doctor steam line", line["detail"])


def part5_link(args):
    heading("5. steam://nav/downloads")
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"steam\shell\open\command") as key:
                command = winreg.QueryValueEx(key, "")[0]
            result("PASS", "the steam:// scheme is registered with Windows", command[:120])
        except (ImportError, OSError) as exc:
            result("FAIL", "the steam:// scheme is not registered", str(exc))
            return
        if args.no_open:
            result("INFO", "not opening the link (--no-open)")
            return
        if ask_yes_no("Open steam://nav/downloads now? (it just shows Steam's Downloads page)"):
            os.startfile("steam://nav/downloads")      # noqa: S606 - the exact link Jarvis's row button uses
            worked = ask_yes_no("Did Steam's DOWNLOADS page open?", default=True)
            result("PASS" if worked else "FAIL", "steam://nav/downloads opens Steam's Downloads page", "answered by you")
        else:
            result("INFO", "link not opened (skipped)")
    else:
        result("INFO", "steam:// registration is only checked on Windows")


RUNNING_BITS = 256 | 1024 | 1048576
INTERESTING_BITS = RUNNING_BITS | 512 | 2097152 | 4194304 | 2048
PROBE_AFTER_TICKS = 30          # keep sampling an app's folders this many ticks after it stops looking active (cancel evidence)


def part6_watch(args):
    heading("6. Live watch (%d s, every %.1f s)" % (args.watch, args.interval))
    steam = Ctx.steam_mod
    say("Do these in Steam while this runs (use a SMALL game; any free one will do). The watch runs its FULL length,")
    say("so plan the steps before you start; a Ctrl+C has to be pressed twice within 5 s to stop it early:")
    say("  a) start an install                  -> expect: queued/downloading")
    say("  b) after ~20 s press PAUSE           -> expect: paused;   resume after ~20 s")
    say("  c) let it finish (staging is seen at the end)")
    say("  d) an UPDATE of an installed game (Library > game > Update)")
    say("  e) start another install and CANCEL it in Steam, then UNINSTALL a small game (N6: what is left on disk?)")
    say("  f) last: while a download is active choose Steam > Exit -> expect 'Steam isn't running'")
    say("Optional, and it helps a lot: right after each action press ONE key here to mark it (Windows: just the key; elsewhere")
    say("letter + Enter): s=start p=pause r=resume f=finished c=cancel u=uninstall g=update x=steam-exit n=note.")
    say("The marks are written into the timeline, next to what Steam did in the seconds after.")
    say("")

    class StubStore:
        def get_provider(self, _pid):
            return {"settings": {"root": Ctx.steam_root}}

    provider = steam.SteamProvider()
    probe = SignalProbe(Ctx.libraries, Ctx.steam_root)
    markers = Markers()
    timeline, seen_flags, change_fields = [], {}, {}
    last_flags, last_raw, last_state = {}, {}, {}
    states_seen, done_count, errors = set(), {}, []
    peak_speed, series = 0.0, {}
    hub_rows_seen, probe_active_ticks, hub_active_ticks = 0, 0, 0
    probe_first_t, hub_first_t = None, None
    empty_paused = set()        # rows 'paused' with no bytes at all: a queued background update, not a download
    labels = {}
    started, tick, prev_sample = time.time(), 0, {}
    hub_call = hub_steam_rows if not args.no_hub else None
    # --- brief 06: lifecycle, second signals, markers
    manifest_now = {}           # appid -> {'flags','raw'} of the previous tick (absent = no manifest)
    history = {}                # appid -> [(t, state or None)] for apps that ever looked interesting
    last_interesting = {}       # appid -> last tick it looked interesting
    install_dirs = {}           # appid -> installdir name, to check what is left after a cancel or uninstall
    lifecycle = []              # event entries
    steam_gone_seen, steam_was_up = False, None
    sig_stats = {}              # appid -> counters for the 6d table
    rates = {"dir": [], "io": []}
    last_int, last_now = None, None
    while time.time() - started < args.watch:
        try:
            tick += 1
            now = time.time()
            t_rel = round(now - started, 1)
            mark = markers.poll()
            while mark:
                timeline.append({"t": t_rel, "marker": mark})
                say("  %s  >>> you marked: %s" % (time.strftime("%H:%M:%S"), mark))
                mark = markers.poll()
            files = steam.read_manifests(Ctx.libraries, {})          # fresh each time: no mtime cache
            report = provider.poll(StubStore(), now)
            rows = {r["id"]: r for r in report.get("items", [])} if "error" not in report else {}
            if "error" in report and (not errors or errors[-1] != report["error"]):
                errors.append(report["error"])
                say("  %s  provider.poll error: %s" % (time.strftime("%H:%M:%S"), report["error"]))
            # Steam process up/down (the poll's error says the same, this is the independent look)
            if tick % max(1, int(5 / args.interval)) == 1:
                up, _how = steam_process_running()
                if up is not None:
                    if steam_was_up and not up:
                        steam_gone_seen = True
                        timeline.append({"t": t_rel, "event": "steam_process_gone"})
                        say("  %s  Steam's process is gone" % time.strftime("%H:%M:%S"))
                    steam_was_up = up
            cur_state = {}
            for appid, (_lib, _dir, parsed) in files.items():
                state = steam._ci(parsed, "AppState", parsed)
                raw_all = {f: str(steam._ci(state, f, "")) for f in FIELDS if steam._ci(state, f, None) is not None}
                cur_state[appid] = {"flags": steam._int(raw_all.get("StateFlags")), "raw": raw_all}
                if raw_all.get("installdir"):
                    install_dirs[appid] = (raw_all["installdir"], _lib)
            for appid, st in cur_state.items():
                if st["flags"] & INTERESTING_BITS:
                    last_interesting[appid] = tick
            watched = {a for a, last in last_interesting.items() if tick - last <= PROBE_AFTER_TICKS}
            sig = probe.sample(now, sorted(watched), want_io=bool(watched) or tick % 5 == 1)
            # lifecycle events: a manifest appearing, vanishing, changing flags or bytes
            for appid in sorted(set(cur_state) | set(manifest_now)):
                if appid not in last_interesting:
                    continue
                events = lifecycle_events(manifest_now.get(appid), cur_state.get(appid))
                if tick == 1:                    # manifests that were already there when the watch began are not news
                    events = [e for e in events if e != "manifest_appeared"]
                history.setdefault(appid, []).append((t_rel, cur_state.get(appid)))
                for ev in events:
                    entry = {"t": t_rel, "app": appid, "event": ev}
                    lifecycle.append(entry)
                    timeline.append(entry)
                    name = (cur_state.get(appid) or manifest_now.get(appid) or {"raw": {}})["raw"].get("name", "?")
                    say("  %s  EVENT %-9s %-26s %s" % (time.strftime("%H:%M:%S"), appid, str(name)[:26], ev))
            manifest_now = cur_state
            real = False                    # True when a row this tick is a real transfer (bytes known)
            for appid, (_lib, _dir, parsed) in files.items():
                state = steam._ci(parsed, "AppState", parsed)
                raw = {f: str(steam._ci(state, f, "")) for f in FIELDS if steam._ci(state, f, None) is not None}
                flags = steam._int(raw.get("StateFlags"))
                if appid in last_raw and last_raw[appid] != raw:
                    for field in raw:
                        if raw[field] != last_raw[appid].get(field):
                            change_fields.setdefault(flags, set()).add(field)
                seen_flags.setdefault(flags, set()).add(raw.get("name", appid))
                row = rows.get(appid)
                row_state = row["state"] if row else None
                if row:
                    if row_state == "paused" and not int(raw.get("BytesToDownload") or 0) and not int(raw.get("BytesDownloaded") or 0):
                        empty_paused.add(appid)
                    else:
                        real = True
                moved = flags != last_flags.get(appid) or row_state != last_state.get(appid)
                if row:
                    states_seen.add(row_state)
                    labels.setdefault(appid, row["subtitle"])
                    if row_state == "done":
                        done_count[appid] = done_count.get(appid, 0) + 1
                    got = int(raw.get("BytesDownloaded") or 0)
                    if appid in prev_sample and now > prev_sample[appid][0]:
                        speed = (got - prev_sample[appid][1]) / (now - prev_sample[appid][0])
                        peak_speed = max(peak_speed, speed)
                    prev_sample[appid] = (now, got)
                    if row.get("percent") is not None:
                        series.setdefault(appid, []).append(row["percent"])
                if moved or (row and tick % 5 == 0):
                    say("  %s  %-9s %-26s flags=%-9s %-28s -> %-12s %s" % (
                        time.strftime("%H:%M:%S"), appid, raw.get("name", "?")[:26], flags, decode_flags(flags)[:28],
                        row_state or "(not listed)",
                        ("%s%% %s/%s" % (row.get("percent"), human(raw.get("BytesDownloaded")), human(raw.get("BytesToDownload"))))
                        if row and row.get("percent") is not None else ""))
                entry = {"t": t_rel, "app": appid, "name": raw.get("name"), "flags": flags,
                         "raw": {k: raw.get(k) for k in ("BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged", "SizeOnDisk", "LastUpdated")},
                         "row": {k: row.get(k) for k in ("state", "percent", "subtitle", "message")} if row else None}
                if appid in sig["dir"]:
                    entry["sig"] = {"dir": {appid: sig["dir"][appid]},
                                    "io": ({k: sig["io"][k] for k in ("moved", "rate", "delta_total")} if sig["io"] else None)}
                    stats = sig_stats.setdefault(appid, {"active": 0, "manifest_moved": 0, "dir_moved": 0, "io_moved": 0})
                    if flags & RUNNING_BITS:
                        stats["active"] += 1
                        prev_raw = last_raw.get(appid) or {}
                        if prev_raw and (prev_raw.get("BytesDownloaded") != raw.get("BytesDownloaded") or prev_raw.get("BytesStaged") != raw.get("BytesStaged")):
                            stats["manifest_moved"] += 1
                        if sig["dir"][appid].get("moved"):
                            stats["dir_moved"] += 1
                        if sig["io"] and sig["io"].get("moved"):
                            stats["io_moved"] += 1
                timeline.append(entry)
                last_flags[appid], last_raw[appid], last_state[appid] = flags, raw, row_state
            # tick-level second signals (log lines, per-process writes) + one readable line
            tick_sig = {"t": t_rel, "sig_tick": {"log": sig["log"], "io": sig["io"]}}
            timeline.append(tick_sig)
            if sig["io"] and sig["io"].get("rate"):
                rates["io"].append(sig["io"]["rate"])
            for appid, d in sig["dir"].items():
                st = cur_state.get(appid)
                if st and st["flags"] & RUNNING_BITS and d.get("d_bytes", 0) > 0 and last_now:
                    rates["dir"].append(d["d_bytes"] / max(0.5, now - last_now))
            if sig["dir"] and any(d.get("moved") for d in sig["dir"].values()) or (sig["io"] and sig["io"].get("moved")) or tick % 5 == 0:
                bits = []
                for appid, d in sig["dir"].items():
                    bits.append("%s dir=%s (%s%s, newest %s)" % (appid, human(d["bytes"]), "+" if d.get("d_bytes", 0) >= 0 else "-", human(abs(d.get("d_bytes", 0))),
                                                           ("%ss ago" % d["newest_age_s"]) if d.get("newest_age_s") is not None else "no files"))
                if sig["io"]:
                    bits.append("io=%s/s (%s)" % (human(sig["io"]["rate"]), ", ".join("%s %s" % (n, human(v)) for n, v in sig["io"]["delta"].items() if v)))
                if sig["log"] and sig["log"].get("new_lines"):
                    bits.append("log +%d lines" % sig["log"]["new_lines"])
                    for ln in sig["log"]["kept"][-2:]:
                        bits.append("| " + ln[:110])
                if bits:
                    say("  %s  SIGNALS  %s" % (time.strftime("%H:%M:%S"), "   ".join(bits)))
            last_now = now
            if real:
                probe_active_ticks += 1
                if probe_first_t is None:
                    probe_first_t = now - started
            if hub_call and tick % max(1, int(5 / args.interval)) == 0:
                hub = hub_call()
                if hub is None:
                    pass
                else:
                    if hub:
                        hub_rows_seen += 1
                        if hub_first_t is None:
                            hub_first_t = now - started
                        if real:
                            hub_active_ticks += 1
                        timeline.append({"t": round(now - started, 1), "hub_steam_rows": hub})
            time.sleep(args.interval)
        except KeyboardInterrupt:
            if last_int is not None and time.time() - last_int < 5:
                say("  (stopped early by you: the watch did not run its full length)")
                break
            last_int = time.time()
            say("  Ctrl+C pressed: the watch should run its FULL length. Press it again within 5 s to stop anyway.")
    ran_s = time.time() - started

    heading("6b. What the watch showed")
    if not timeline:
        result("WARN", "nothing was sampled")
        return
    result("INFO", "samples", "%d ticks, %d timeline entries" % (tick, len(timeline)))
    if ran_s + 2 * args.interval < args.watch:
        result("WARN", "the watch stopped after %.0f s of the %d s asked for" % (ran_s, args.watch),
               "the earlier run (jarvis-main_155) was cut at 16 s and could not answer pause, resume, finish, cancel, update or staging: run it again to its end")
    else:
        result("PASS", "the watch ran its full length", "%.0f s" % ran_s)
    say("StateFlags values seen, with the games that had them:")
    for flags, names in sorted(seen_flags.items()):
        say("   %-9s %-48s %s" % (flags, decode_flags(flags), ", ".join(sorted(names))[:60]))
    for flags, fields in sorted(change_fields.items()):
        say("   while flags=%s these manifest fields changed between samples: %s" % (flags, ", ".join(sorted(fields))))
    unknown = [f for f in seen_flags if f & ~KNOWN_MASK and f not in (4, 6) and any(f & b for b in (256, 512, 1024))]
    if unknown:
        result("INFO", "active StateFlags with bits Jarvis doesn't name", ", ".join("%s (%s)" % (f, decode_flags(f)) for f in unknown))

    if empty_paused:
        result("WARN", "%d row(s) sat on 'paused' with no bytes at all" % len(empty_paused),
               "apps %s: Steam parks queued background updates with the pause flag; they are not downloads "
               "(jarvis-steam-real-run-fixes.patch stops listing them)" % ", ".join(sorted(empty_paused)))
    manifest_entries = [e for e in timeline if "app" in e and "flags" in e]
    if not states_seen:
        result("WARN", "Jarvis never listed a download during the watch",
               "did a download start? If one did and you saw no rows, that is a FAIL: send the timeline file")
    else:
        result("INFO", "Jarvis row states seen", ", ".join(sorted(states_seen)))
    if "downloading" in states_seen:
        result("PASS", "a download was reported as 'downloading'")
        rising = [a for a, s in series.items() if len(s) >= 3 and s[-1] > s[0]]
        dir_grew = [a for a, st in sig_stats.items() if st["dir_moved"]]
        stuck = [a for a in dir_grew if len(series.get(a, [])) >= 3 and series[a][-1] <= series[a][0]]
        if rising:
            result("PASS", "percent rose during the download", "; ".join("%s: %s%% -> %s%%" % (a, series[a][0], series[a][-1]) for a in rising))
        elif stuck:
            result("FAIL", "percent did not rise although the downloading folder grew (this is N5)",
                   "app(s) %s: Jarvis showed a percent that stood still while Steam was downloading" % ", ".join(sorted(stuck)))
        elif dir_grew and not series:
            result("INFO", "no percent was shown while the downloading folder grew: on purpose since N5",
                   "Steam does not write progress while it downloads, so a running row says what Steam last reported "
                   "(and how old that is) in its message instead of a percent that would stand still")
        else:
            result("FAIL", "percent rose during the download", "no app's percent went up (and no second signal moved either)")
        result("INFO", "peak speed derived from manifest byte deltas", "%s/s: NOT a real speed, it measures when Steam wrote the file (N5)" % human(peak_speed))
    else:
        result("WARN", "no 'downloading' state seen: percent, speed, stall and the hub comparison are NOT tested yet",
               "install a game of about 1 GB or more (the hub looks at an idle Steam every 30-60 s) and re-run with --watch 600")
    for appid, values in series.items():
        if any(v >= 100 for v in values[:-1]) or any(v > 99.0 for v in values if v != 100):
            result("FAIL", "percent reached 100 before Steam said finished", "app %s: %s" % (appid, values[-5:]))
    for wanted, label, meaning in (("paused", "pause", "paused in Steam"), ("queued", "queue", "waiting in Steam's own queue"),
                                   ("postprocessing", "staging", "the 'installing' phase")):
        if wanted in states_seen:
            result("PASS", "%s was recognised" % label, meaning)
        else:
            result("INFO", "%s not exercised" % label)
    # --- N5: the stall check. Never a pass on its own: it is compared with the second signal.
    verdicts = judge_stalls(manifest_entries)
    if not verdicts:
        if "stalled" in states_seen:
            result("WARN", "'stalled' was shown but no stretch could be judged")
        else:
            result("INFO", "Jarvis never said 'stalled' during the watch", "so the false-stall check was not exercised")
    for v in verdicts:
        result(v["verdict"], "stall of app %s at t=%.0f..%.0f s (%.0f s): %s" % (v["app"], v["from_t"], v["to_t"], v["seconds"], v["why"]))
    if done_count:
        for appid, count in done_count.items():
            result("PASS" if count == 1 else "FAIL", "app %s: 'done' reported %s time(s)" % (appid, count), "exactly once is the rule" if count != 1 else "")
    else:
        result("INFO", "no download finished during the watch", "so the 'finish reported once' rule was not exercised")
    for appid, label in labels.items():
        result("INFO", "app %s labelled %r" % (appid, label), "'update' only if the game already had SizeOnDisk/LastUpdated when first seen")
    if any("isn't running" in e for e in errors):
        result("PASS", "Steam closed mid-download was reported as 'Steam isn't running', not as a failure")
    elif errors:
        result("WARN", "poll errors during the watch", "; ".join(errors[:3]))

    # ---- 6c coverage
    heading("6c. Which steps the watch actually saw")
    steam_gone_seen = steam_gone_seen or any("isn't running" in e for e in errors)
    marks = [e for e in timeline if "marker" in e]
    cover = coverage(manifest_entries, lifecycle, marks, steam_gone_seen, args.watch, ran_s)
    for name, seen, evidence in cover:
        result("PASS" if seen else "WARN", "step %s %s" % (name, "was seen" if seen else "was NOT seen"), evidence)

    # ---- 6d second signals
    heading("6d. N5: the manifest and the three second signals, side by side")
    if not sig_stats:
        result("INFO", "no app looked active, so the second signals were not compared", "start an install while the watch runs")
    for appid, st in sig_stats.items():
        say("app %s: %d active ticks | manifest bytes changed in %d | downloading folder changed in %d | Steam's disk writes (>%s per tick) in %d" % (
            appid, st["active"], st["manifest_moved"], st["dir_moved"], human(IO_NOISE_BYTES), st["io_moved"]))
        if st["active"] >= 3:
            best = max((("downloading folder", st["dir_moved"]), ("disk writes of Steam's processes", st["io_moved"])), key=lambda x: x[1])
            if best[1] > st["manifest_moved"]:
                result("INFO", "app %s: %s moved in %d active ticks, the manifest in %d" % (appid, best[0], best[1], st["manifest_moved"]),
                       "a candidate second signal for N5")
            else:
                result("WARN", "app %s: no second signal beat the manifest" % appid, "folder moved %d, writes moved %d, manifest %d (active ticks %d)" % (st["dir_moved"], st["io_moved"], st["manifest_moved"], st["active"]))
    log_seen = [e for e in timeline if "sig_tick" in e and (e["sig_tick"].get("log") or {}).get("new_lines")]
    log_present = any((e["sig_tick"].get("log") or {}).get("present") for e in timeline if "sig_tick" in e)
    if not log_present:
        result("WARN", "content_log.txt was not found", "expected at <Steam>/logs/content_log.txt")
    else:
        result("INFO", "content_log.txt got new lines in %d of %d ticks" % (len(log_seen), tick), "the timeline holds the lines that name the app or a download word")
    how = probe.io_how
    if not any((e["sig_tick"].get("io")) for e in timeline if "sig_tick" in e):
        result("WARN", "Steam's disk writes could not be read", how or "no reader worked (try: pip install psutil)")
    else:
        result("INFO", "Steam's disk writes were read through %s" % how)
    steam_speed = args.steam_speed
    if steam_speed is None and sig_stats and sys.stdin and sys.stdin.isatty():
        try:
            text = input("What did Steam's own Downloads page show as the speed while it downloaded, in MB/s? (Enter to skip) ").strip()
            steam_speed = float(text) if text else None
        except (EOFError, ValueError):
            steam_speed = None
    median = lambda xs: sorted(xs)[len(xs) // 2] if xs else None  # noqa: E731
    for name, vals in (("downloading folder growth", rates["dir"]), ("disk writes of Steam's processes", rates["io"])):
        m = median([v for v in vals if v > IO_NOISE_BYTES])
        if m is None:
            continue
        if steam_speed:
            ratio = m / (steam_speed * 1024 * 1024)
            result("PASS" if 0.5 <= ratio <= 2.0 else "WARN", "%s: median %s/s = %.2f x the speed you read off Steam" % (name, human(m), ratio),
                   "within half to double of Steam's own number" if 0.5 <= ratio <= 2.0 else "not in the same range as Steam's own number")
        else:
            result("INFO", "%s: median %s/s while active" % (name, human(m)), "give Steam's own speed with --steam-speed MB/s to compare")

    # ---- 6e N6: cancel / uninstall
    heading("6e. N6: what was left after a cancel or an uninstall")
    records = []
    for appid, hist in history.items():
        inst = install_dirs.get(appid)
        install_exists = None
        if inst:
            install_exists = (Path(inst[1]) / "steamapps" / "common" / inst[0]).is_dir()
        dl_exists = any((Path(lib) / "steamapps" / sub / str(appid)).is_dir() for lib in Ctx.libraries for sub in ("downloading", "temp"))
        rec = cancel_record(appid, hist, install_exists, dl_exists)
        if rec:
            records.append(rec)
    if not records:
        result("INFO", "no cancel, uninstall or vanished manifest was seen", "press c / u after you cancel or uninstall in Steam, and run the watch long enough")
    for rec in records:
        result("INFO", "app %s: %s" % (rec["app"], rec["kind"]),
               "last flags %s, bytes %s of %s, manifest at the end: %s, install folder: %s, downloading/temp folder: %s" % (
                   rec["last_flags"], rec["last_bytes_downloaded"], rec["last_bytes_to_download"],
                   "yes" if rec["manifest_present_at_end"] else "NO", rec["install_dir_exists"], rec["downloading_dir_exists"]))
        if not rec["manifest_present_at_end"] or rec["last_flags"] is not None and not (rec["last_flags"] & RUNNING_BITS):
            say("       Jarvis: classify() lists nothing for it. While other manifests remain on disk the poll says complete=True and"
                " providers.apply_poll sets the row to 'unknown' (N6); if it was the last manifest the poll errors instead"
                " ('no appmanifest files found') and the row goes stale. Both are cases a fixture must cover.")
    # ---- 6f what Steam wrote after each key you pressed
    heading("6f. What Steam wrote after each key you pressed (N5 and N12)")
    effects = marker_effects(timeline)
    if not effects:
        result("INFO", "no keys were pressed", "next time press p / r / c / u right after the action: it makes pause, resume and cancel unambiguous")
    for eff in effects:
        say("at t=%.0f s you marked '%s':" % (eff["t"], eff["marker"]))
        if not eff["app_changes"] and not eff["events"]:
            say("     nothing changed in any manifest within 15 s")
        for app, ch in eff["app_changes"].items():
            say("     app %s: %s" % (app, "; ".join("+%.0fs flags=%s (%s) bytes=%s" % (dt, f, decode_flags(f), b) for dt, f, b in ch[:6])))
        for ev in eff["events"][:8]:
            say("     " + ev)

    path = Path(args.out) / "steam-checks-timeline.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in timeline:
            fh.write(json.dumps(entry) + "\n")
    say("timeline written to %s" % path)
    if hub_call:
        from jarvis.hub import providers as hub_providers
        idle_max = hub_providers.IDLE_MAX_S
        active_s = probe_active_ticks * args.interval
        if probe_first_t is None:
            result("INFO", "end-to-end hub comparison skipped", "no download was active")
        elif hub_first_t is not None:
            lag = max(0.0, hub_first_t - probe_first_t)
            result("PASS", "the real hub showed the Steam download in Menu > Downloads' data (dl_list)", "%d sampled moments" % hub_rows_seen)
            result("PASS" if lag <= idle_max + 10 else "WARN", "the hub noticed the download %.0f s after Steam started it" % lag,
                   "its idle poll is %d-%d s by design (providers.IDLE_MIN_S/IDLE_MAX_S); once it sees a row it polls every ~3 s"
                   % (hub_providers.IDLE_MIN_S, idle_max))
        elif active_s < idle_max + 5:
            result("INFO", "the hub showed nothing, but the download lasted only %d s" % active_s,
                   "the hub looks at an idle Steam only every %d-%d s, so a short download can be missed entirely; use a bigger game to test this" % (hub_providers.IDLE_MIN_S, idle_max))
        else:
            alive = hub_steam_alive()
            if alive:
                result("FAIL", "Steam downloaded for %d s but the hub listed no Steam rows" % active_s,
                       "the hub polled Steam %d s ago, so it was running: send the timeline file" % alive)
            else:
                result("WARN", "Steam downloaded for %d s but the hub listed no Steam rows" % active_s,
                       "the hub isn't getting good Steam polls (daemon stopped, provider off, or it thinks Steam is closed): part 7 shows which")
    Ctx.watch_summary = {"ran_s": ran_s, "records": records, "effects": effects, "cover": cover}


def part8_control(args):
    heading("8. Steam control research (N12): can a download be paused or cancelled from outside?")
    say("Read-only. Nothing here starts, stops, freezes or drives Steam. No steam:// command is run (only nav/downloads in part 5,")
    say("and only if you said yes). Killing, suspending or window-automating Steam is not tried and never will be.")
    seen = {"strings": "not run (no Steam folder)", "manifest": None, "steamcmd": None}
    # (a) steam:// strings in Steam's own files
    if Ctx.steam_root:
        scanned, total, found, notes = scan_steam_files_for_uris(Ctx.steam_root)
        for n in notes:
            say("   note: " + n)
        verbs = {}
        for uri, files in found.items():
            verbs.setdefault(verb_of(uri), set()).update(files)
        control = {v: f for v, f in verbs.items() if any(w in v.lower() for w in CONTROL_WORDS)}
        out_path = Path(args.out) / "steam-control-strings.txt"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("# steam:// strings found in Steam's own files (a string is not proof that a command works)\n")
            for uri in sorted(found):
                fh.write("%s\t%s\n" % (uri, ",".join(sorted(found[uri]))[:80]))
        result("INFO", "scanned %d Steam file(s), %s: %d distinct steam:// strings, %d verbs" % (scanned, human(total), len(found), len(verbs)),
               "full list: %s" % out_path)
        say("verbs whose name mentions %s:" % ", ".join(CONTROL_WORDS[:7]) + " ...")
        for verb in sorted(control):
            say("   steam://%-28s in %s" % (verb, ", ".join(sorted(control[verb]))[:60]))
        wanted = [v for v in control if any(w in v.lower() for w in ("pause", "resume", "cancel", "stop", "abort", "suspend"))]
        if wanted:
            result("WARN", "strings that LOOK like a pause / cancel command exist", "%s: read the list above; they were NOT run, so what they do is unknown" % ", ".join("steam://" + w for w in sorted(wanted)))
            seen["strings"] = "found strings: " + ", ".join(sorted(wanted)) + " (not run)"
        else:
            result("INFO", "no steam:// string with a pause, resume, cancel, stop, abort or suspend verb was found")
            seen["strings"] = "no pause/cancel/stop verb among %d steam:// verbs in %d files" % (len(verbs), scanned)
    else:
        result("INFO", "Steam's program files were not scanned", "no Steam folder")
    # (b) manifest at pause / resume / cancel comes from the watch
    summary = getattr(Ctx, "watch_summary", None)
    if summary:
        pause_effects = [e for e in summary["effects"] if e["marker"] in ("pause", "resume", "cancel")]
        seen["manifest"] = "%d key mark(s) for pause/resume/cancel recorded in 6f" % len(pause_effects) if pause_effects else \
            "the watch ran but you pressed no pause/resume/cancel key: see 6b for the flags seen"
    else:
        seen["manifest"] = "not run: use --watch"
        result("WARN", "what Steam writes into the manifest at pause, resume and cancel was not recorded", "run with --watch 600 and press p / r / c")
    # (c) SteamCMD
    found_cmd = steamcmd_candidates(Ctx.steam_root)
    if found_cmd:
        info = steamcmd_report(found_cmd[0])
        result("INFO", "SteamCMD found", "%s (config\\config.vdf present: %s; not run, running it would make it update itself)" % (info["path"], info["has_config_vdf"]))
        seen["steamcmd"] = "installed at %s" % info["path"]
    else:
        result("INFO", "SteamCMD was not found", "looked on PATH and in the usual folders; it is a separate download from Valve")
        seen["steamcmd"] = "not installed here"
    if Ctx.steam_root:
        accounts = count_saved_steam_accounts(Ctx.steam_root)
        if accounts is not None:
            result("INFO", "Steam remembers %d account(s) on this PC" % accounts, "count only; names are not read or printed")
    # the options table
    say("")
    say("Options for the owner's decision S2 (what was seen on THIS PC; costs are from the plan, (unverified) = not tested here):")
    say("  %-3s %-44s %-52s %s" % ("", "option", "cost", "seen here"))
    rows = [
        ("a", "a steam:// command for pause / cancel", "none if one exists; (unverified) none is known", seen["strings"]),
        ("b", "set Steam's own flags in the manifest", "writing Steam's files while it runs: not safe; read-only research", seen["manifest"] or ""),
        ("c", "the hub downloads with SteamCMD itself", "SteamCMD + a Steam login (Steam Guard) for owned games: Jarvis would hold credentials (unverified)", seen["steamcmd"] or ""),
        ("d", "suspend or kill steam.exe / its threads", "freezes or breaks the whole client: REJECTED, not tried", "not tried, by design"),
        ("e", "automate Steam's window", "fragile: not recommended, not tried", "not tried, by design"),
    ]
    for key, option, cost, saw in rows:
        say("  %-3s %-44s %-52s %s" % (key, option, cost, saw))
    result("INFO", "Steam control: nothing was started, stopped or changed", "the table above goes in the report for decision S2")


def hub_steam_rows():
    """The Steam rows the REAL hub currently holds (what Menu > Downloads shows), or None if unreadable."""
    try:
        from jarvis.actions import download_hub as dh
        out = dh.tool_dl_list({"provider": "steam", "limit": 50})
        if not out or not out.get("ok"):
            return None
        return [{"id": i.get("id"), "title": i.get("title"), "state": i.get("state"), "percent": i.get("percent"),
                 "stale": i.get("stale")} for i in out.get("items", [])]
    except Exception:  # noqa: BLE001
        return None


def hub_steam_alive():
    """Seconds since the real hub last polled Steam successfully, when it is enabled; else 0."""
    try:
        from jarvis.actions import download_hub as dh
        for p in dh.tool_dl_providers({}).get("providers", []):
            if p.get("id") == "steam" and p.get("enabled") and p.get("last_ok_age_s") is not None and p["last_ok_age_s"] <= 30:
                return max(1, int(p["last_ok_age_s"]))
    except Exception:  # noqa: BLE001
        pass
    return 0


def part7_hub(args):
    heading("7. Through the real hub (dl_providers, dl_list, hub_doctor)")
    try:
        from jarvis.actions import download_hub as dh
    except Exception as exc:  # noqa: BLE001
        result("FAIL", "could not import the hub tools", "%s: %s" % (type(exc).__name__, exc))
        return
    try:
        out = dh.tool_dl_providers({})
    except Exception as exc:  # noqa: BLE001
        result("FAIL", "dl_providers raised", "%s: %s" % (type(exc).__name__, exc))
        return
    if not out.get("ok"):
        result("FAIL", "dl_providers failed", str(out.get("error") or out)[:200])
        return
    steam_p = next((p for p in out.get("providers", []) if p.get("id") == "steam"), None)
    say("providers registered: %s" % (", ".join(p["id"] for p in out.get("providers", [])) or "none"))
    if not steam_p:
        result("WARN" if not Ctx.steam_root else "FAIL", "no 'steam' provider is registered in the hub",
               "the daemon registers Steam within 60 s of starting: if hub_doctor says it is running, it was most likely started "
               "BEFORE the Steam code was in this repo. Restart it (Menu > Daemons > Download hub), wait a minute, re-run. "
               "If it is still missing, look in the daemon's log for 'built-in provider steam not registered'")
    else:
        result("PASS", "the 'steam' provider is registered", "mode=%s enabled=%s interval=%ss" % (steam_p["mode"], steam_p["enabled"], steam_p["interval_s"]))
        result("PASS" if steam_p["enabled"] else "WARN", "Steam provider is switched on" if steam_p["enabled"] else "Steam provider is switched OFF (dl_provider_prefs)")
        age = steam_p.get("last_ok_age_s")
        if age is None:
            result("WARN", "the hub has never had a good Steam poll", "last error: %s" % steam_p.get("last_error"))
        elif age <= 30:
            result("PASS", "the hub polled Steam successfully %d s ago" % age)
        else:
            err = steam_p.get("last_error")
            result("WARN", "the hub's last good Steam poll was %d s ago" % age,
                   ("its last poll said: %s" % err) if err else "the hub daemon may have stopped (hub_doctor daemon line below)")
        if steam_p.get("last_error"):
            result("INFO", "Steam provider's last error", steam_p["last_error"])
        extra = [c for c in steam_p["capabilities"] if c != "reveal"]
        result("PASS" if not extra and not steam_p["controls"] else "FAIL",
               "Steam rows are read-only (only a Reveal-folder button, no pause/cancel)",
               "capabilities=%s controls=%s" % (steam_p["capabilities"], steam_p["controls"]))
    lst = dh.tool_dl_list({"provider": "steam", "limit": 50})
    items = lst.get("items", []) if lst.get("ok") else []
    result("INFO", "dl_list(provider=steam)", "%d row(s)" % len(items))
    for item in items:
        say("   %-9s %-30s %-14s %5s%%  stale=%s" % (item.get("id"), str(item.get("title"))[:30], item.get("state"), item.get("percent"), item.get("stale")))
    doc = dh.tool_hub_doctor({})
    for check in doc.get("checks", []):
        if check.get("name") in ("daemon", "steam") or "steam" in str(check.get("name")).lower():
            detail = check.get("detail", "")
            if check.get("name") == "steam" and Ctx.steam_root and "wasn't found" in detail:
                result("WARN" if args.root else "FAIL", "hub_doctor says Steam isn't found, but this script found it at %s" % Ctx.steam_root,
                       "give the hub the folder (dl_provider_prefs id=steam settings={root: ...} or JARVIS_STEAM_ROOT)"
                       if not args.root else "expected: you passed --root, the hub only looks in the usual places")
                continue
            result("PASS" if check.get("status") == "ok" else "WARN", "hub_doctor %s" % check.get("name"), detail)


# --------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Test Jarvis's Steam support against a real Steam install.")
    parser.add_argument("--repo", help="folder that contains jarvis-cli/ (default: next to this script)")
    parser.add_argument("--root", help="Steam folder (default: registry / JARVIS_STEAM_ROOT / usual places)")
    parser.add_argument("--watch", type=int, default=0, help="seconds of live watch (part 6); 0 = skip")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between samples in the live watch")
    parser.add_argument("--out", default="steam-checks-out", help="folder for the report and timeline")
    parser.add_argument("--no-open", action="store_true", help="do not offer to open steam://nav/downloads")
    parser.add_argument("--no-hub", action="store_true", help="skip part 7 (the real hub database) and the live hub comparison")
    parser.add_argument("--no-control", action="store_true", help="skip part 8 (Steam control research)")
    parser.add_argument("--steam-speed", type=float, default=None, help="the speed (MB/s) Steam's own Downloads page showed, to compare with the second signals")
    args = parser.parse_args()
    args.interval = max(0.5, args.interval)
    Path(args.out).mkdir(parents=True, exist_ok=True)

    repo = find_repo(args.repo)
    if not repo:
        print("Can't find jarvis-cli/jarvis/hub/steam.py. Put this script in the repo root or pass --repo <folder>.")
        return 2
    sys.path.insert(0, str(repo / "jarvis-cli"))
    os.environ.setdefault("JARVIS_HUB_NO_DELIVER", "1")      # this script never fires a real notification
    from jarvis.hub import steam as steam_mod               # noqa: E402
    Ctx.steam_mod = steam_mod

    say("steam-checks  %s   repo: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), repo))
    if part1_locate(args):
        part2_libraries(args)
        part3_manifests(args)
        part4_jarvis_code(args)
        part5_link(args)
        if args.watch > 0:
            part6_watch(args)
        else:
            say("")
            say("(part 6, the live watch, was skipped: run with --watch 300 and install a small game)")
    if not args.no_control:
        part8_control(args)
    if not args.no_hub:
        part7_hub(args)

    heading("Summary")
    tally = {}
    for status, _title in RESULTS:
        tally[status] = tally.get(status, 0) + 1
    say("   ".join("%s: %d" % (k, tally.get(k, 0)) for k in ("PASS", "FAIL", "WARN", "INFO")))
    for status, title in RESULTS:
        if status == "FAIL":
            say("  FAIL  " + title)
    report_path = Path(args.out) / "steam-checks-report.txt"
    report_path.write_text("\n".join(REPORT) + "\n", encoding="utf-8")
    say("report written to %s" % report_path)
    return 1 if tally.get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
