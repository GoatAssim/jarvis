"""A small on-disk cache of PUBLIC vendor answers (~/.jarvis/modhosts/cache.json).

Each `jarvis` command is a new process, so a memory cache does nothing; this file is the cache.
Time to live about 10 minutes by default, size capped, written atomically (temp file then replace).
It holds public answers only, NEVER the key: put() refuses a value (or a cache key name) that contains the
CurseForge key if one is configured, in any spelling the file could carry: as it is, and JSON-escaped (a key
with a backslash or a double quote is written with a backslash in front of it), once or twice (a JSON text
held in a string).  The guard has no minimum key length: a refused write is only a cache miss, so a short or
odd key costs a little caching and never a leak.  (Belt and braces: net.py never returns the key anyway, and
nothing from curseforge.py is cached at all; tests/test_minecraft_hardening.py pins both.)

The file is listed in Settings as cache state by another patch (its entry is not part of this file).  Every
function returns quietly on any problem: a cache that fails is a cache miss, never an error.
"""
import json
import os
import threading
import time
from pathlib import Path

DEFAULT_TTL = 600
MAX_FILE_BYTES = 8 * 1024 * 1024         # whole file
MAX_ENTRY_BYTES = 1024 * 1024            # one value
MAX_ENTRIES = 400
MAX_KEY_LEN = 400

_lock = threading.Lock()


def cache_path():
    """Resolved at call time so a test that redirects HOME first sees its own folder."""
    return Path.home() / ".jarvis" / "modhosts" / "cache.json"


def _now():
    return time.time()


def _load():
    try:
        with cache_path().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}
    except Exception:  # noqa: BLE001 - missing, torn or unreadable: an empty cache
        return {}


def _save(entries):
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"v": 1, "entries": entries}, separators=(",", ":"), ensure_ascii=False)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
        return True
    except Exception:  # noqa: BLE001
        return False


def _secret():
    """The configured CurseForge key, or None.  Lazy: config.py belongs to another patch.  Any length."""
    try:
        import importlib
        config = importlib.import_module(__package__ + ".config")
        key = config.get_key()
        return key.strip() if isinstance(key, str) and key.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _spellings(secret):
    """Every way `secret` can read inside the cache file: as it is, and JSON-escaped once or twice, with
    and without ASCII escapes.  json.dumps escapes a backslash and a double quote, so a key holding either
    is NOT found by a plain substring search of the serialised value."""
    forms = {secret}
    for ascii_only in (True, False):
        once = json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
        forms.add(once)
        forms.add(json.dumps(once, ensure_ascii=ascii_only)[1:-1])
    return forms


def get(key):
    """The cached value, or None (missing, expired, unreadable)."""
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN:
        return None
    try:
        with _lock:
            entry = _load().get(key)
        if isinstance(entry, dict) and float(entry.get("t", 0)) > _now():
            return entry.get("v")
    except Exception:  # noqa: BLE001
        pass
    return None


def put(key, value, ttl_seconds=DEFAULT_TTL):
    """Remember `value` (any JSON value) for `ttl_seconds`.  Returns True when stored."""
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN or value is None:
        return False
    try:
        ttl = float(ttl_seconds)
        if ttl <= 0:
            return False
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_ENTRY_BYTES:
            return False
        secret = _secret()
        if secret:
            forms = _spellings(secret)
            if any(form in text or form in key for form in forms):
                return False
        with _lock:
            entries = _load()
            now = _now()
            entries = {k: v for k, v in entries.items() if isinstance(v, dict) and float(v.get("t", 0)) > now}
            entries[key] = {"t": now + ttl, "v": value}
            # Over a cap: drop what expires soonest first (never the entry just written).
            while len(entries) > 1 and (len(entries) > MAX_ENTRIES or _size(entries) > MAX_FILE_BYTES):
                victim = min((k for k in entries if k != key), key=lambda k: float(entries[k].get("t", 0)))
                entries.pop(victim)
            return _save(entries)
    except Exception:  # noqa: BLE001
        return False


def _size(entries):
    try:
        return len(json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def clear():
    """Forget everything (tests; and the one place a stale answer could be unwelcome)."""
    try:
        with _lock:
            _save({})
    except Exception:  # noqa: BLE001
        pass
