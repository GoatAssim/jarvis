"""Search the raw log FILES on disk, line by line.

HOW THIS DIFFERS FROM logs.search()
-----------------------------------
`logs.search()` searches *parsed conversation entries*: it walks
`list_logged_conversations()`, JSON-decodes each line of each `.jsonl`, and
matches against structured records. That is the right tool for "find the
turn where I asked about X".

It is the wrong tool — and for two of these, no tool at all — for:

  * a line that failed to parse (a crash mid-write leaves exactly one such
    line, and it is frequently the most interesting line in the file),
  * a daemon's console output, which is plain text and has no conversation
    at all (see daemons.py),
  * a log file that isn't Jarvis's, which the user just wants grepped,
  * "which file, which line number" — logs.search returns an entry index
    into a parsed list, which you cannot open a file at.

So this is a plain, boring, line-oriented file search: give it a pattern,
it tells you file, line number, and the matching line. It is deliberately
NOT structured-aware, because the whole point is to see what is actually
written on disk.

WHAT IT SEARCHES BY DEFAULT
---------------------------
The named sets in SEARCH_ROOTS: conversation logs, every daemon console
(including rotated backups), and the scheduler's ask log. `paths=` takes
explicit files or globs instead, which is how "grep my app's log" works
without registering anything.

SAFETY
------
Binary files are skipped by a null-byte sniff rather than by extension, so
a `.log` that is actually a rotated gzip doesn't come back as mojibake.
Every file read is size-capped and the whole walk is capped, because a
search box that can be pointed at `/` needs a stopping condition that
isn't the user's patience.
"""

import os
import re
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
ENCODING = "utf-8"

MODES = ("words", "phrase", "regex")
DEFAULT_LIMIT = 100
MAX_PATTERN_CHARS = 500
# One pathological file (a 4GB rotated log, a core dump someone renamed)
# must not stall the whole search.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 400
MAX_LINE_CHARS = 2000
SNIFF_BYTES = 8192

# Named, safe-by-default roots. A caller asks for a set by name rather than
# passing a path, so the common cases need no path handling at all.
SEARCH_ROOTS = {
    "conversations": (JARVIS_DIR / "logs", ("*.jsonl",)),
    "daemons": (JARVIS_DIR / "daemons", ("*/console.log", "*/console.log.*")),
    "scheduler": (JARVIS_DIR, ("sched_ask_log.jsonl", "sched_ask_log.*")),
    "notifications": (JARVIS_DIR, ("notifications.json*",)),
}
DEFAULT_SETS = ("conversations", "daemons", "scheduler")


def _compile(query, mode):
    """Same three modes as logs.search, same meanings, so a user who has
    learned one search box has learned both."""
    query = (query or "").strip()
    if not query:
        return None, "empty query"
    if len(query) > MAX_PATTERN_CHARS:
        return None, f"query too long (max {MAX_PATTERN_CHARS})"
    mode = mode if mode in MODES else "words"
    try:
        if mode == "regex":
            return [re.compile(query, re.IGNORECASE)], ""
        if mode == "phrase":
            return [re.compile(re.escape(query), re.IGNORECASE)], ""
        terms = [t for t in query.split() if t]
        if not terms:
            return None, "empty query"
        return [re.compile(re.escape(t), re.IGNORECASE) for t in terms], ""
    except re.error as exc:
        return None, f"bad regex: {exc}"


def _looks_binary(path):
    try:
        with path.open("rb") as fh:
            chunk = fh.read(SNIFF_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def candidate_files(sets=None, paths=None):
    """Every file the search will actually open, deduped, newest first.

    Newest first matters: a limit-truncated search should show you the
    recent occurrences, which are almost always the ones you want, rather
    than whatever happened to sort first alphabetically.
    """
    found = []
    seen = set()

    for name in (sets if sets is not None else DEFAULT_SETS):
        root, patterns = SEARCH_ROOTS.get(name, (None, ()))
        if root is None or not root.exists():
            continue
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.is_file() and str(path) not in seen:
                    seen.add(str(path))
                    found.append(path)

    for raw in (paths or []):
        raw = str(raw or "").strip()
        if not raw:
            continue
        expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
        # A glob is anything with a magic character; everything else is a
        # literal path, so a filename containing '[' still works.
        if any(ch in raw for ch in "*?["):
            parent = expanded.parent
            try:
                matches = sorted(parent.glob(expanded.name))
            except (OSError, ValueError):
                matches = []
        elif expanded.is_dir():
            matches = [p for p in sorted(expanded.rglob("*")) if p.is_file()]
        else:
            matches = [expanded]
        for path in matches:
            if path.is_file() and str(path) not in seen:
                seen.add(str(path))
                found.append(path)

    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0

    found.sort(key=_mtime, reverse=True)
    return found[:MAX_FILES]


def _label(path):
    """Short, recognizable name for a result.

    A full path is unreadable in a list and a bare filename is ambiguous
    (every daemon's console is called console.log), so a Jarvis-owned file
    is shown relative to ~/.jarvis and anything else keeps its full path.
    """
    try:
        return str(path.relative_to(JARVIS_DIR))
    except ValueError:
        return str(path)


def search(query, mode="words", limit=DEFAULT_LIMIT, sets=None, paths=None,
           context=0, newest_first=True):
    """Grep the log files. Returns {"ok", "results"|"error", ...}.

    `context` includes N lines either side of each hit, the way `grep -C`
    does — usually what you want for a stack trace, where the line that
    matched "Traceback" is the least informative line of the block.
    """
    patterns, err = _compile(query, mode)
    if patterns is None:
        return {"ok": False, "error": err, "results": []}

    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        context = max(0, min(int(context), 10))
    except (TypeError, ValueError):
        context = 0

    files = candidate_files(sets=sets, paths=paths)
    results = []
    scanned_files = 0
    scanned_lines = 0
    skipped = []
    truncated = False

    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            skipped.append({"file": _label(path), "why": "too large"})
            continue
        if _looks_binary(path):
            skipped.append({"file": _label(path), "why": "binary"})
            continue

        try:
            with path.open("r", encoding=ENCODING, errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            skipped.append({"file": _label(path), "why": str(exc)})
            continue

        scanned_files += 1
        scanned_lines += len(lines)
        hits = []
        for number, line in enumerate(lines, start=1):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "…"
            # "words" is an AND across terms on a SINGLE line — the same
            # meaning logs.search gives it, scoped to a line because that
            # is the unit this search returns.
            if not all(p.search(line) for p in patterns):
                continue
            entry = {
                "file": _label(path),
                "path": str(path),
                "line": number,
                "text": line.strip(),
            }
            if context:
                lo = max(0, number - 1 - context)
                hi = min(len(lines), number + context)
                entry["context"] = [
                    {"line": lo + i + 1, "text": lines[lo + i][:MAX_LINE_CHARS]}
                    for i in range(hi - lo)
                ]
            hits.append(entry)

        # Within one file the newest lines are at the end, so a truncated
        # result should keep the tail rather than the head.
        if newest_first:
            hits.reverse()
        for hit in hits:
            results.append(hit)
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break

    return {
        "ok": True,
        "query": query,
        "mode": mode if mode in MODES else "words",
        "results": results,
        "truncated": truncated,
        "files_scanned": scanned_files,
        "files_available": len(files),
        "lines_scanned": scanned_lines,
        "skipped": skipped[:10],
    }


def tail(path, lines=200):
    """Last N lines of one file, without reading all of it."""
    target = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not target.is_file():
        return {"ok": False, "error": f"no such file: {target}", "lines": []}
    try:
        size = target.stat().st_size
        window = min(size, max(8192, int(lines) * 200))
        with target.open("rb") as fh:
            if size > window:
                fh.seek(size - window)
                fh.readline()
            data = fh.read()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "lines": []}
    text = data.decode(ENCODING, errors="replace")
    return {"ok": True, "file": _label(target), "path": str(target),
            "lines": text.splitlines()[-int(lines):]}


def available_sets():
    """Which named sets actually have files right now — for a UI dropdown
    that shouldn't offer an empty 'daemons' before any daemon has run."""
    out = []
    for name in SEARCH_ROOTS:
        count = len(candidate_files(sets=[name]))
        out.append({"name": name, "files": count})
    return out
