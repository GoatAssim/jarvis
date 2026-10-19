#!/usr/bin/env python3
"""Download Hub, phase 9: what do Epic's and GOG's launchers leave on disk during a download?

The sibling of `steam-checks.py` (plan section 0.1).  Nothing here parses a download yet: the
master plan calls non-Steam stores "unverified", and field names must not be guessed.  This script
READS the launchers' local files and WATCHES them while you install, pause and update a game, so the
real parser can be written from what actually changes.

It is read-only: it never writes to a launcher's folder and never contacts the network.  GOG's
database is copied to a temp folder and read from the copy.  Passwords, tokens, cookies, e-mail and
session-looking values are masked in the output.  The only file it may write is the one you name
with --out.

The paths below come from memory of how the two launchers lay out their files on Windows; they are
starting points to confirm, which is the whole point of the script.  A path that doesn't exist is
reported as missing, not as an error.

Usage (Windows, Python 3):

    python tests/store_checks.py                         # inventory of what exists, then exit
    python tests/store_checks.py --store epic            # only Epic
    python tests/store_checks.py --watch 600             # inventory, then watch 10 minutes of changes
    python tests/store_checks.py --watch 900 --interval 3 --out store-report.txt --redact

While it watches: start a game install (about 1 GB or more), pause it, resume it, let it finish, then
trigger an update of an installed game, in EACH launcher you want covered.  Send back the report
(--redact replaces your Windows user name with <user> in paths).
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SECRET_WORDS = ("token", "secret", "password", "passwd", "auth", "cookie", "session", "email", "login", "user")
INTERESTING_TABLE = re.compile(r"install|download|progress|queue|task|state|product|update", re.I)
LOG_WORDS = re.compile(r"download|progress|install|update|chunk|bytes|patch|queue|pause|resum|cancel", re.I)
MAX_KEYS = 200            # per JSON file
MAX_FILES = 300           # per folder listing
MAX_ROWS = 50             # per small database table
MAX_LOG_LINES = 40        # per file per look
MAX_DB_BYTES = 300 * 1024 * 1024


def is_secret(name):
    text = str(name).lower()
    return any(word in text for word in SECRET_WORDS)


def short(value, limit=160):
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- pure helpers (tested)

def flat(obj, prefix="", out=None, limit=MAX_KEYS):
    """A JSON value -> {dotted.key: short value}.  Secret-looking keys are masked, lists summarised."""
    out = {} if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = "%s.%s" % (prefix, key) if prefix else str(key)
            if is_secret(key):
                out[name] = "<masked>"
            else:
                flat(value, name, out, limit)
    elif isinstance(obj, list):
        out[(prefix or "$") + "[]"] = "list of %d" % len(obj)
        for index, value in enumerate(obj[:5]):
            flat(value, "%s[%d]" % (prefix, index), out, limit)
    else:
        out[prefix or "$"] = short(obj)
    return out


def diff_maps(old, new):
    """Readable change lines between two snapshots: + new key, - key gone, ~ value changed."""
    lines = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            lines.append("+ %s = %s" % (key, new[key]))
        elif key not in new:
            lines.append("- %s" % key)
        elif old[key] != new[key]:
            lines.append("~ %s: %s -> %s" % (key, old[key], new[key]))
    return lines


def redact(text, user):
    if not user:
        return text
    return re.sub(re.escape(user), "<user>", text, flags=re.I)


def list_dir(path, prefix, depth=2):
    """{prefix:rel/path: 'size=N mtime=ISO'} for a folder, two levels deep, capped."""
    out = {}
    root = Path(path)
    if not root.is_dir():
        return out

    def walk(folder, level):
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if len(out) >= MAX_FILES:
                return
            rel = os.path.relpath(entry.path, root).replace("\\", "/")
            try:
                stat = entry.stat()
            except OSError:
                continue
            if entry.is_dir(follow_symlinks=False):
                out["%s:%s/" % (prefix, rel)] = "dir"
                if level < depth:
                    walk(entry.path, level + 1)
            else:
                stamp = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                out["%s:%s" % (prefix, rel)] = "size=%d mtime=%s" % (stat.st_size, stamp)
    walk(root, 1)
    return out


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def db_inventory(db_path, prefix="db"):
    """Read a copy of a SQLite file: every table's columns and row count; the rows of small tables
    whose name looks download-related, with secret-looking columns masked."""
    out = {}
    path = Path(db_path)
    if not path.is_file():
        return out
    if path.stat().st_size > MAX_DB_BYTES:
        out["%s:skipped" % prefix] = "file is over %d MB" % (MAX_DB_BYTES // 1024 // 1024)
        return out
    work = Path(tempfile.mkdtemp(prefix="store-checks-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(path) + suffix)
            if source.exists():
                shutil.copy2(source, work / (path.name + suffix))
        conn = sqlite3.connect(str(work / path.name))
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for table in tables:
                quoted = '"%s"' % table.replace('"', '""')
                columns = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % quoted)]
                count = conn.execute("SELECT COUNT(*) FROM %s" % quoted).fetchone()[0]
                out["%s:%s:columns" % (prefix, table)] = ", ".join(columns)
                out["%s:%s:rows" % (prefix, table)] = str(count)
                if INTERESTING_TABLE.search(table) and 0 < count <= MAX_ROWS:
                    for index, row in enumerate(conn.execute("SELECT * FROM %s" % quoted)):
                        shown = ["%s=%s" % (col, "<masked>" if is_secret(col) else short(val, 80))
                                 for col, val in zip(columns, row)]
                        out["%s:%s:row%02d" % (prefix, table, index)] = short("; ".join(shown), 400)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["%s:error" % prefix] = short(exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


class LogTail:
    """New lines of log files since the last look, only the ones that mention downloads."""

    def __init__(self):
        self.offsets = {}

    def look(self, folder, prefix, first=False):
        found = {}
        root = Path(folder)
        if not root.is_dir():
            return found
        files = sorted([p for p in root.glob("*.log")], key=lambda p: p.stat().st_mtime)[-6:]
        for path in files:
            try:
                size = path.stat().st_size
                found["%s:%s" % (prefix, path.name)] = "size=%d" % size
                start = self.offsets.get(path, max(0, size - 65536) if first else size)
                if size < start:
                    start = 0
                with open(path, "rb") as handle:
                    handle.seek(start)
                    text = handle.read(262144).decode("utf-8", "replace")
                self.offsets[path] = start + len(text.encode("utf-8", "replace"))
            except OSError:
                continue
            hits = [line for line in text.splitlines() if LOG_WORDS.search(line)][-MAX_LOG_LINES:]
            for index, line in enumerate(hits):
                found["%s:%s:line@%d:%02d" % (prefix, path.name, self.offsets[path], index)] = short(line, 240)
        return found


# --------------------------------------------------------------------------- Epic

def epic_roots(env=None):
    env = env or os.environ
    pdata = Path(env.get("PROGRAMDATA") or r"C:\ProgramData")
    local = env.get("LOCALAPPDATA")
    return {
        "data": pdata / "Epic" / "EpicGamesLauncher" / "Data",
        "manifests": pdata / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests",
        "installed": pdata / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat",
        "logs": (Path(local) / "EpicGamesLauncher" / "Saved" / "Logs") if local else None,
    }


def snapshot_epic(roots, tail, first=False):
    snap = {}
    for name, path in roots.items():
        if path is None or not Path(path).exists():
            snap["epic:missing:%s" % name] = str(path)
    snap.update(list_dir(roots["data"], "epic:file"))
    install_dirs = {}
    manifests = roots["manifests"]
    if manifests.is_dir():
        for item in sorted(manifests.glob("*.item")):
            data = read_json(item)
            if data is None:
                snap["epic:item:%s" % item.name] = "not JSON"
                continue
            for key, value in flat(data).items():
                snap["epic:item:%s:%s" % (item.name, key)] = value
            if isinstance(data, dict) and data.get("InstallLocation"):
                install_dirs[item.name] = data["InstallLocation"]
    data = read_json(roots["installed"]) if roots["installed"].is_file() else None
    if data is not None:
        for key, value in flat(data).items():
            snap["epic:installed:%s" % key] = value
    for name, folder in install_dirs.items():
        snap.update(list_dir(Path(folder) / ".egstore", "epic:egstore:%s" % name))
    if roots["logs"] is not None:
        snap.update(tail.look(roots["logs"], "epic:log", first))
    return snap


# --------------------------------------------------------------------------- GOG

def gog_roots(env=None):
    env = env or os.environ
    pdata = Path(env.get("PROGRAMDATA") or r"C:\ProgramData")
    base = pdata / "GOG.com" / "Galaxy"
    return {"galaxy": base, "db": base / "storage" / "galaxy-2.0.db", "logs": base / "logs"}


def gog_registry():
    """HKLM ...\\GOG.com\\Games\\<id> values (path, ver, gameName ...).  Windows only."""
    out = {}
    try:
        import winreg
    except ImportError:
        return {"gog:registry": "not Windows"}
    for hive_path in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    game = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, game) as key:
                        value_index = 0
                        while True:
                            try:
                                name, value, _kind = winreg.EnumValue(key, value_index)
                            except OSError:
                                break
                            value_index += 1
                            out["gog:reg:%s:%s" % (game, name)] = "<masked>" if is_secret(name) else short(value)
                except OSError:
                    continue
    return out


def snapshot_gog(roots, tail, first=False):
    snap = {}
    for name, path in roots.items():
        if not Path(path).exists():
            snap["gog:missing:%s" % name] = str(path)
    snap.update(list_dir(roots["galaxy"], "gog:file", depth=1))
    snap.update(db_inventory(roots["db"], "gog:db"))
    snap.update(gog_registry())
    for key, value in list(snap.items()):
        if key.startswith("gog:reg:") and key.endswith(":path"):
            for info in sorted(Path(value).glob("goggame-*.info")) if Path(value).is_dir() else []:
                data = read_json(info)
                if data is not None:
                    for k, v in flat(data).items():
                        snap["gog:info:%s:%s" % (info.name, k)] = v
    snap.update(tail.look(roots["logs"], "gog:log", first))
    return snap


# --------------------------------------------------------------------------- run

def take(stores, tail, first=False):
    snap = {}
    if stores in ("epic", "all"):
        snap.update(snapshot_epic(epic_roots(), tail, first))
    if stores in ("gog", "all"):
        snap.update(snapshot_gog(gog_roots(), tail, first))
    return snap


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only look at Epic's and GOG's local launcher files.")
    parser.add_argument("--store", choices=("epic", "gog", "all"), default="all")
    parser.add_argument("--watch", type=float, default=0, help="keep watching for this many seconds")
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between looks (default 3)")
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--redact", action="store_true", help="replace your user name with <user>")
    args = parser.parse_args(argv)

    sink = open(args.out, "w", encoding="utf-8") if args.out else None
    user = os.environ.get("USERNAME") or os.environ.get("USER") if args.redact else None

    def say(text=""):
        text = redact(text, user)
        print(text)
        if sink:
            sink.write(text + "\n")

    try:
        tail = LogTail()
        say("store-checks %s  (read-only; python %s)" % (datetime.datetime.now().isoformat(timespec="seconds"),
                                                         sys.version.split()[0]))
        before = take(args.store, tail, first=True)
        families = {}
        for key in before:
            parts = key.split(":")
            families[":".join(parts[:2])] = families.get(":".join(parts[:2]), 0) + 1
        say("\n== inventory: %d entries" % len(before))
        for family, count in sorted(families.items()):
            say("  %-28s %d" % (family, count))
        say("\n== entries")
        for key in sorted(before):
            say("  %s = %s" % (key, before[key]))
        if args.watch > 0:
            say("\n== watching for %d s every %g s; install / pause / resume / update a game now" % (args.watch, args.interval))
            started = time.monotonic()
            changes = 0
            while time.monotonic() - started < args.watch:
                time.sleep(args.interval)
                after = take(args.store, tail)
                # old log lines drop out of each look by design; only their arrival matters
                lines = [l for l in diff_maps(before, after) if not (l.startswith("- ") and ":line@" in l)]
                if lines:
                    changes += len(lines)
                    say("\n-- +%.0f s: %d change(s)" % (time.monotonic() - started, len(lines)))
                    for line in lines[:120]:
                        say("   " + line)
                    if len(lines) > 120:
                        say("   ... and %d more" % (len(lines) - 120))
                before = after
            say("\n== done: %d changes seen in %.0f s" % (changes, time.monotonic() - started))
        return 0
    finally:
        if sink:
            sink.close()


if __name__ == "__main__":
    sys.exit(main())
