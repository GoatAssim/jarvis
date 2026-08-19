"""Tracks how often each command actually runs.

cli.py's run_command() bumps a counter here on every real invocation \u2014
whether typed directly ('jarvis updateSpotify') or, once the AI can run
commands itself, triggered by a natural-language ask. That data powers the
"frequently used commands" context handed to the AI on every 'jarvis <text>'
call (see frequent_commands_context() below, used by ai_client.py).
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
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATS_FILE.write_text(
            json.dumps({"counts": counts}, indent=2) + "\n", encoding=ENCODING
        )
    except OSError as e:
        print(f"Warning: couldn't save usage stats: {e}", file=sys.stderr)


def bump(name):
    with _LOCK:
        counts = _load()
        counts[name] = counts.get(name, 0) + 1
        _save(counts)


def top(n=5):
    """[(name, count), ...] sorted most- to least-used."""
    counts = _load()
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:n]


def frequent_commands_context(commands, n=5):
    """A short text block naming the user's most-used commands, meant to be
    folded into the system prompt. Skips names no longer in commands.json
    (renamed or deleted since), and returns "" once nothing qualifies."""
    ranked = [(name, count) for name, count in top(n * 2) if name in (commands or {})][:n]
    if not ranked:
        return ""
    bits = ", ".join(f"{name} ({count}x)" for name, count in ranked)
    return f"The user's most frequently used jarvis commands are: {bits}."
