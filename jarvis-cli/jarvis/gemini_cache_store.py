"""Cross-process store for Gemini explicit cache names.

Why this file exists: jarvis is a fresh OS process on every `jarvis ...`
call. The previous Gemini caching code created a cachedContents resource at
the start of an ask and DELETED it at the end of that same ask, which meant
every invocation paid:

  - one extra HTTP round trip (POST /cachedContents) before the first
    generateContent, on the latency path
  - cache-creation billing at the standard input-token rate
  - storage billing for however long it lived

...in exchange for a discount it could only collect on rounds 2+ of that one
ask. Since most asks are a single round, that was usually a net loss, and it
could never be anything else, because the cache was thrown away before the
next process could reach it.

This store fixes the amortization. A cache name is written to disk keyed by
a fingerprint of exactly what went into it (model + system prefix + tool
schemas). The next jarvis process fingerprints its own prefix, finds the
same key, and reuses the existing cache name instead of creating a second
one. The cache is left to expire on its own TTL rather than deleted, so the
write is paid once and read many times — which is the only arrangement in
which explicit caching beats Gemini's free implicit caching.

Same philosophy as discovery_cache.py and route_stickiness.py: entirely
best-effort. A missing file, a corrupt file, a read-only home directory, a
stale entry pointing at an expired cache — all collapse to "no cached name
available", and the caller falls back to sending content inline exactly as
it would have anyway. Nothing here ever raises into an ask().
"""

import hashlib
import json
import time
from pathlib import Path

STORE_FILE = Path.home() / ".jarvis" / "gemini_cache.json"
ENCODING = "utf-8"

# Never let the file grow without bound: it's keyed by prefix fingerprint,
# and a user who switches models/router groups often would otherwise
# accumulate one dead entry per combination forever.
MAX_ENTRIES = 24

# Drop an entry this many seconds BEFORE its recorded expiry. A cache that
# expires mid-flight makes generateContent fail with a 400 referencing a
# missing cachedContent, which would burn a whole provider attempt; retiring
# it slightly early turns that failure into a normal inline request.
EXPIRY_MARGIN_SECONDS = 30


def fingerprint(model, system_text, tools_payload):
    """Stable key for one cacheable prefix.

    Must cover everything that actually went INTO the cache, because reusing
    a cache whose contents differ from what the caller now believes is in it
    produces a request that silently prompts the model with stale tools or a
    stale persona. json.dumps with sort_keys is what makes two equal tool
    lists hash equal regardless of dict ordering.
    """
    try:
        tools_repr = json.dumps(tools_payload or [], sort_keys=True, default=str)
    except (TypeError, ValueError):
        tools_repr = str(tools_payload)
    blob = "\x1f".join([model or "", system_text or "", tools_repr])
    return hashlib.sha256(blob.encode(ENCODING, "replace")).hexdigest()[:32]


def _read():
    try:
        with open(STORE_FILE, encoding=ENCODING) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    try:
        STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STORE_FILE, "w", encoding=ENCODING) as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def lookup(key):
    """Return a still-valid cache name for `key`, or None.

    Expired and malformed entries are treated as absent. They're not pruned
    here — lookup() runs on the latency path of an ask, and a disk write
    there buys nothing; store() prunes instead.
    """
    entry = _read().get(key)
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    expires = entry.get("expires_at")
    if not isinstance(name, str) or not name:
        return None
    try:
        expires = float(expires)
    except (TypeError, ValueError):
        return None
    if time.time() >= expires - EXPIRY_MARGIN_SECONDS:
        return None
    return name


def store(key, name, ttl_seconds):
    """Record a freshly created cache name and prune the store.

    Pruning drops expired entries first, then oldest-created, so the file
    stays bounded without ever evicting a live cache in favor of a dead one.
    """
    if not key or not name:
        return
    data = _read()
    now = time.time()
    data[key] = {
        "name": name,
        "created_at": now,
        "expires_at": now + max(0, int(ttl_seconds or 0)),
    }

    live = {
        k: v for k, v in data.items()
        if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
    }
    if len(live) > MAX_ENTRIES:
        ordered = sorted(live.items(), key=lambda kv: kv[1].get("created_at") or 0, reverse=True)
        live = dict(ordered[:MAX_ENTRIES])
    _write(live)


def forget(name):
    """Drop every entry pointing at `name`.

    Called when Gemini rejects a cache name (expired, deleted out from under
    us, wrong project) so the next ask creates a fresh one instead of
    retrying the same dead reference every time.
    """
    if not name:
        return
    data = _read()
    remaining = {
        k: v for k, v in data.items()
        if not (isinstance(v, dict) and v.get("name") == name)
    }
    if len(remaining) != len(data):
        _write(remaining)


def clear():
    """Wipe the store. Only used by the skills/debug surfaces and tests."""
    try:
        STORE_FILE.unlink()
    except OSError:
        pass
