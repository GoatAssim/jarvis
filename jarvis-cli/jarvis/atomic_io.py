"""Crash-safe file writing, shared.

Every persistent store in this project writes the same way: serialize the
whole thing, replace the file. Done with a plain write_text() that is a
truncate-then-write, which means a process killed mid-write leaves a file
that is half its old contents and parses as nothing at all.

That is not a theoretical risk here. The web UI's Stop button calls
killTree(), which on Windows is `taskkill /T /F` — an unconditional force
kill that no signal handler can intercept. Every Stop press races whatever
write happens to be in flight, and the loaders all catch JSONDecodeError
and return a default, so a torn file doesn't look corrupt, it looks empty.
A conversation that vanished and a conversation that never existed are
indistinguishable to the reader.

write_json() replaces that with: write a temp file, fsync it, move the
current file aside as .bak, then os.replace the temp into place.
os.replace is atomic on POSIX and on Windows (MoveFileEx with
REPLACE_EXISTING), so a concurrent reader sees the old file or the new
file and never a half of either. read_json() completes the pair by falling
back to the .bak when the main file won't parse.

Caches (discovery_cache, route_stickiness, skill_stickiness) deliberately
do NOT use this: losing a cache costs a recomputation, the backup file
would double their disk footprint for no benefit, and every one of them
already degrades cleanly to "no cache" on a parse failure. This is for
stores where the data is the point and cannot be regenerated.
"""

import json
import os

ENCODING = "utf-8"


def write_json(path, data, indent=2):
    """Atomically write `data` as JSON. Returns True on success.

    Never raises: callers are persistence paths where the correct response
    to a full disk is to carry on without saving, not to take down whatever
    the user was actually doing.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding=ENCODING) as handle:
            json.dump(data, handle, indent=indent, default=str, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            # Without fsync, os.replace can land before the data does, so a
            # power loss leaves an atomically-renamed empty file — atomic
            # and useless.
            os.fsync(handle.fileno())
        if path.exists():
            try:
                os.replace(str(path), str(path.with_suffix(path.suffix + ".bak")))
            except OSError:
                pass
        os.replace(str(tmp), str(path))
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_json(path, default=None, expect=None):
    """Read JSON, falling back to the .bak written by write_json.

    `expect` is an optional required type — data of the wrong shape counts
    as corruption, since a facts file that deserializes to a string is no
    more usable than one that doesn't parse.
    """
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            if not candidate.exists():
                continue
            data = json.loads(candidate.read_text(encoding=ENCODING))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if expect is not None and not isinstance(data, expect):
            continue
        return data
    return default
