"""Phase 2 of the token-optimization enhancements doc: a small on-disk,
cross-process cache of recent search_tools/search_commands hits.

jarvis is a brand-new OS process on every `jarvis ...` invocation (see
history.py's module docstring), so anything a search_tools/search_commands
call discovered in one process is gone by the next — a user asking two
related things back-to-back re-triggers the exact same discovery round
twice. This module lets ai_client.py pre-seed a fresh process's
active_schemas from the last process's discovery, when the query looks
like a repeat.

Deliberately tiny and best-effort: a short TTL and a small entry cap mean
it never grows unbounded and never serves badly stale data, and every
call site treats a missing/corrupt/stale cache file exactly like a miss —
this is a pure optimization, never a correctness dependency. Same
directory conventions as commands_config.py's ~/.jarvis storage.
"""

import json
import time
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CACHE_FILE = JARVIS_DIR / "discovery_cache.json"
ENCODING = "utf-8"

# How long a cached hit stays usable, and how many recent entries are kept
# on disk. Small on purpose — this is meant to catch "two related asks in
# a row," not to become a durable index of every query ever made.
TTL_SECONDS = 5 * 60
MAX_ENTRIES = 20


def _normalize(query):
    return (query or "").strip().lower()


def _load_entries():
    """Return the on-disk entries list, or [] on any missing/corrupt file
    — a bad cache file should degrade to "no cache", never raise."""
    try:
        data = json.loads(CACHE_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _save_entries(entries):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({"entries": entries}, indent=2) + "\n", encoding=ENCODING
        )
    except OSError:
        # Best-effort only — a failed write just means the next process
        # doesn't get a cache hit, same as if the file didn't exist.
        pass


def cache_lookup(query, kind):
    """Return the previously-found name list for (normalized query, kind),
    or None on a miss / expired entry / unreadable cache file. Most-recent
    match wins if the same (query, kind) was stored more than once."""
    norm = _normalize(query)
    if not norm:
        return None
    now = time.time()
    best = None
    for entry in _load_entries():
        if entry.get("query") != norm or entry.get("kind") != kind:
            continue
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)) or now - ts > TTL_SECONDS:
            continue
        found = entry.get("found")
        if isinstance(found, list) and found:
            best = found
    return best


def cache_store(query, kind, found):
    """Record a successful discovery hit. Purely additive/best-effort —
    never raises, and a write failure just means no cache entry, not a
    broken caller."""
    norm = _normalize(query)
    found = [n for n in (found or []) if isinstance(n, str) and n]
    if not norm or not found:
        return

    entries = _load_entries()
    now = time.time()
    # Drop any now-stale entries and any prior entry for this exact
    # (query, kind) — the fresh one below replaces it — before appending
    # and capping, so the file never grows unbounded.
    entries = [
        e for e in entries
        if isinstance(e.get("ts"), (int, float))
        and now - e["ts"] <= TTL_SECONDS
        and not (e.get("query") == norm and e.get("kind") == kind)
    ]
    entries.append({"query": norm, "kind": kind, "found": found, "ts": now})
    entries = entries[-MAX_ENTRIES:]
    _save_entries(entries)
