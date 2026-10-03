"""Tracks how often each command actually runs.

cli.py's run_command() bumps a counter here on every real invocation \u2014
whether typed directly ('jarvis updateSpotify') or, once the AI can run
commands itself, triggered by a natural-language ask. top() reads it back.
Nothing puts it in the AI's system prompt any more (L.41 removed the
"frequently used commands" block).
"""

import json
import sys
import threading
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
STATS_FILE = JARVIS_DIR / "usage_stats.json"
ENCODING = "utf-8"

# Chained commands can now run genuinely concurrently on separate threads
# within one jarvis process (see cli.py's "and" chain separator), and each
# one calls bump() for itself \u2014 this guards the read-modify-write below so
# two threads bumping different commands at the same moment can't clobber
# each other's update (a plain read-then-write has no other protection).
_LOCK = threading.Lock()


def _load():
    if not STATS_FILE.exists():
        return {}
    try:
        data = json.loads(STATS_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    counts = data.get("counts") if isinstance(data, dict) else None
    return counts if isinstance(counts, dict) else {}


def _save(counts):
    # Atomic — see atomic_io's module docstring. bump() is called on every
    # command run, including concurrently from chained commands, so this is
    # one of the most frequently rewritten files in the system and
    # correspondingly the most likely to be caught mid-write by a kill.
    from . import atomic_io
    if not atomic_io.write_json(STATS_FILE, {"counts": counts}):
        print("Warning: couldn't save usage stats", file=sys.stderr)


def bump(name):
    with _LOCK:
        counts = _load()
        counts[name] = counts.get(name, 0) + 1
        _save(counts)


def top(n=5):
    """[(name, count), ...] sorted most- to least-used."""
    counts = _load()
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:n]
