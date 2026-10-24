#!/usr/bin/env python3
"""Download Hub, N22 part c / N23: what does Steam write for a game that HAS an update waiting?

The sibling of `steam-checks.py` and `tests/store_checks.py`.  `steam_library` can only say "unknown"
about updates today, because Steam's UpdateRequired flag was seen set on idle games and nothing else has
been measured.  This script RECORDS what Steam's files say, raw, so the real rule can be written from
what actually differs between a game with an update waiting and one without.  It does NOT parse any of it
into "update available / not available", and it does not guess a version.

It is read-only: it never writes to Steam's folders, never starts Steam or a game, never opens a
steam:// link and never contacts the network.  The only file it may write is the one you name with --out.
Steam account ids are masked in the output (17-digit numbers); --redact also replaces your Windows user
name in paths.

Usage (Windows, Python 3), with Steam OPEN:

    python tests/steam_update_checks.py --out steam-update-report.txt --redact
    python tests/steam_update_checks.py --watch 600 --out steam-update-report.txt --redact \\
        --note "Terraria: Steam shows an Update button" --note "Hades: no update shown"

What to do while it runs: in Steam's Library, look at which games show an update waiting (a blue Update
button, or "Update queued" / "Update required").  Pass them with --note so the report can be read against
Steam's own screen.  Then, with --watch, start ONE small update, pause it, resume it, let it finish.  Send
back the report.
"""

import argparse
import datetime
import hashlib
import os
import re
import sys
import time
from pathlib import Path

MAX_MANIFESTS = 400
MAX_RAW = 6000            # characters of one manifest kept in the report
MAX_LIST = 120            # entries of one folder listing
STEAM_ID = re.compile(r"\b7656119\d{10}\b")
LINE = re.compile(r'^\s*"([^"]+)"\s+"([^"]*)"\s*$')


def now_text():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_roots(env=None):
    env = os.environ if env is None else env
    out = [env["JARVIS_STEAM_ROOT"]] if env.get("JARVIS_STEAM_ROOT") else []
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                out.append(winreg.QueryValueEx(key, "SteamPath")[0])
        except (ImportError, OSError):
            pass
        out += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    else:
        home = Path.home()
        out += [str(home / ".steam" / "steam"), str(home / ".local" / "share" / "Steam"),
                str(home / "Library" / "Application Support" / "Steam")]
    return out


def library_paths(root):
    """The Steam folder plus every library listed in libraryfolders.vdf (paths only, found by a plain pattern)."""
    libs = [str(root)]
    try:
        text = (Path(root) / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")[:200000]
    except OSError:
        return libs
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        path = match.group(1).replace("\\\\", "\\")
        if os.path.normcase(os.path.normpath(path)) not in [os.path.normcase(os.path.normpath(x)) for x in libs]:
            libs.append(path)
    return libs


def mask(text, user=None):
    text = STEAM_ID.sub("<steam-id>", text)
    if user:
        text = re.sub(re.escape(user), "<user>", text, flags=re.I)
    return text


def read_raw(path):
    """The manifest as Steam wrote it (capped).  Never parsed here."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:MAX_RAW]
    except OSError as exc:
        return "(unreadable: %s)" % exc


def top_pairs(raw):
    """Only to print the simple `"key" "value"` lines on their own, in file order.  Nothing is interpreted."""
    return [m.groups() for m in (LINE.match(line) for line in raw.splitlines()) if m]


def manifests(libs):
    found = []
    for lib in libs:
        folder = Path(lib) / "steamapps"
        try:
            names = sorted(n for n in os.listdir(folder) if n.startswith("appmanifest_") and n.endswith(".acf"))
        except OSError:
            continue
        for name in names:
            found.append(folder / name)
            if len(found) >= MAX_MANIFESTS:
                return found
    return found


def listing(folder):
    try:
        names = sorted(os.listdir(folder))[:MAX_LIST]
    except OSError:
        return None
    out = []
    for name in names:
        try:
            stat = (Path(folder) / name).stat()
            out.append("%s  %s  %d bytes" % (name, datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), stat.st_size))
        except OSError:
            out.append(name + "  (cannot stat)")
    return out


def inventory(libs, notes):
    lines = ["== inventory %s ==" % now_text()]
    for note in notes:
        lines.append("OWNER NOTE (what Steam's own screen showed): %s" % note)
    for lib in libs:
        lines.append("library: %s" % lib)
        for label, sub in (("steamapps", "steamapps"), ("downloading", "steamapps/downloading"), ("temp", "steamapps/temp")):
            items = listing(Path(lib) / sub)
            lines.append("  %s: %s" % (label, "missing" if items is None else "%d entries" % len(items)))
            for item in (items or [])[:40 if label == "steamapps" else MAX_LIST]:
                if label != "steamapps" or not item.startswith("appmanifest_"):
                    lines.append("    " + item)
    for path in manifests(libs):
        raw = read_raw(path)
        lines.append("--- %s  (%d chars shown, sha1 %s)" % (path, len(raw), hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]))
        lines.append(raw.rstrip())
    return lines


def snapshot(libs):
    return {str(p): read_raw(p) for p in manifests(libs)}


def diff(old, new):
    lines = []
    for path in sorted(set(old) | set(new)):
        if old.get(path) == new.get(path):
            continue
        if path not in old:
            lines.append("NEW manifest %s" % path)
            lines.extend("    " + k + " = " + v for k, v in top_pairs(new[path]))
        elif path not in new:
            lines.append("GONE manifest %s" % path)
        else:
            before, after = dict(top_pairs(old[path])), dict(top_pairs(new[path]))
            changes = ["    %s: %s -> %s" % (k, before.get(k), after.get(k)) for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
            lines.append("CHANGED manifest %s%s" % (path, "" if changes else "  (only inside nested sections: see the next full dump)"))
            lines.extend(changes)
    return lines


def main(argv=None, out=None, sleep=time.sleep, clock=time.monotonic):
    parser = argparse.ArgumentParser(description="Record what Steam's files say about game updates (read-only).")
    parser.add_argument("--watch", type=float, default=0, help="keep watching for this many seconds")
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between looks (default 3)")
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--redact", action="store_true", help="replace your user name with <user>")
    parser.add_argument("--note", action="append", default=[], help="what Steam's screen shows (repeat)")
    parser.add_argument("--root", help="Steam folder (default: look for it)")
    args = parser.parse_args(argv)
    user = (os.environ.get("USERNAME") or os.environ.get("USER")) if args.redact else None
    report = []

    def emit(text):
        text = mask(text, user)
        report.append(text)
        print(text, file=out or sys.stdout, flush=True)

    emit("steam_update_checks: read-only; nothing is written to Steam, nothing is started, no network.")
    roots = [args.root] if args.root else default_roots()
    root = next((r for r in roots if r and (Path(r) / "steamapps").is_dir()), None)
    if root is None:
        emit("Steam folder not found (tried: %s). Use --root." % ", ".join(str(r) for r in roots if r))
    else:
        libs = library_paths(root)
        emit("steam folder: %s  (%d libraries)" % (root, len(libs)))
        for line in inventory(libs, args.note):
            emit(line)
        if args.watch > 0:
            emit("== watching %d s every %s s; start, pause, resume and finish ONE small update now ==" % (args.watch, args.interval))
            last, end = snapshot(libs), clock() + args.watch
            while clock() < end:
                sleep(args.interval)
                current = snapshot(libs)
                changes = diff(last, current)
                if changes:
                    emit("-- %s" % now_text())
                    for line in changes:
                        emit(line)
                last = current
            emit("== watch over ==")
    if args.out:
        Path(args.out).write_text("\n".join(report) + "\n", encoding="utf-8")
        print("report written to %s" % args.out, file=out or sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
