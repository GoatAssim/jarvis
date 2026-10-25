"""Which addons keep failing, so a stream search stops paying for them (N31b, brief 8D).

After `FAILS_TO_SKIP` failures in a row an addon is left out of stream searches for `SKIP_S` seconds
(6 hours).  One answer clears its count.  When the six hours are over the addon is asked again once: an
answer clears it, another failure skips it for another six hours.  Nothing here is permanent, and the
caller always names a skipped addon in the search's `failed` list (`skipped_sentence`).

What counts as a failure (`counts_as_failure`, read from `AddonError.kind` and `.status`, never from the
message text):

  * an HTTP answer of 404, or any other status from 400 up except 408 and 429 (a server that is dead,
    gone or broken; 408 and 429 are "try later"),
  * a timeout, a refused connection or a name that did not resolve (`kind` "network"),
  * any other refusal by the address guard.

What does not count: an address the SSRF guard refused because it is private or local
(`kind` "local_refused").  That is the owner's setting, not the addon's fault, and it costs no network
call.  `local_refused_sentence` names it with the sentence the code already uses ("add it to
allow_private_hosts").  An answer that is not JSON, a reply that is too big and a redirect loop are not
counted either.

The memory is one small JSON file, `addon_health.json` in the hub's data folder.  It is keyed by a short
hash of the addon's address, never the address itself (the address can hold a debrid key), and it holds
only counts and times.  A missing, damaged or unwritable file is an empty memory: this module never raises.
`reset()` forgets everything.  The clock is `_clock`, which the tests replace.
"""

import hashlib
import json
import math
import os
import threading
import time

from .. import paths

FAILS_TO_SKIP = 3
SKIP_S = 6 * 3600
STALE_S = 30 * 24 * 3600           # a count nobody has touched for this long is dropped
MAX_ENTRIES = 200
FILE_NAME = "addon_health.json"
NOT_COUNTED_STATUSES = (408, 429)

_lock = threading.Lock()
_clock = time.time                 # replaced by the tests


def _now(now=None):
    return float(now) if now is not None else float(_clock())


def path():
    return paths.hub_dir() / FILE_NAME


def key_of(addon):
    """A short hash of the addon's address.  The address itself is never written anywhere."""
    base = getattr(addon, "base", None)
    base = str(base if base is not None else addon)
    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- the file

def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _load():
    """-> {key: {"fails": int, "until": float, "last": float}}.  A missing or damaged file is {}."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing, unreadable, not JSON: an empty memory
        return {}
    items = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    out = {}
    for key, entry in items.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        fails = entry.get("fails")
        if isinstance(fails, bool) or not isinstance(fails, int) or fails < 0:
            continue
        out[key[:64]] = {"fails": min(fails, 1000), "until": _number(entry.get("until")), "last": _number(entry.get("last"))}
    return out


def _save(items):
    """Write the memory (a temp file, then a rename).  -> True when it was written; never raises."""
    try:
        folder = paths.hub_dir()
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / ("%s.%d.tmp" % (FILE_NAME, os.getpid()))
        tmp.write_text(json.dumps({"version": 1, "addons": items}, separators=(",", ":")), encoding="utf-8")
        os.replace(str(tmp), str(path()))
        return True
    except Exception:  # noqa: BLE001
        return False


def _prune(items, now):
    for key in [k for k, e in items.items() if e["until"] <= now and now - e["last"] > STALE_S]:
        del items[key]
    if len(items) > MAX_ENTRIES:
        newest = sorted(items, key=lambda k: items[k]["last"], reverse=True)[:MAX_ENTRIES]
        for key in [k for k in items if k not in newest]:
            del items[key]


# --------------------------------------------------------------------------- what the callers use

def counts_as_failure(exc):
    """Is this AddonError one of the failures that count toward a skip?  (See the module text.)"""
    kind = getattr(exc, "kind", None)
    if kind in ("network", "refused"):
        return True
    if kind == "http":
        status = getattr(exc, "status", None)
        return isinstance(status, int) and not isinstance(status, bool) and status >= 400 and status not in NOT_COUNTED_STATUSES
    return False


def is_local_refusal(exc):
    """The SSRF guard refused a private or local address: the owner's setting, not a failure."""
    return getattr(exc, "kind", None) == "local_refused"


def split(addons, now=None):
    """-> (active, skipped): `skipped` is [(addon, failures in a row)] for the addons that are skipped right now."""
    now = _now(now)
    with _lock:
        items = _load()
    active, skipped = [], []
    for addon in addons:
        entry = items.get(key_of(addon))
        if entry and entry["fails"] >= FAILS_TO_SKIP and entry["until"] > now:
            skipped.append((addon, entry["fails"]))
        else:
            active.append(addon)
    return active, skipped


def record(ok=(), failed=(), now=None):
    """`ok`: addons that answered (their count is cleared).  `failed`: addons whose failure counts (one more in a row).
    Never raises."""
    now = _now(now)
    try:
        with _lock:
            items = _load()
            changed = False
            for addon in ok:
                if items.pop(key_of(addon), None) is not None:
                    changed = True
            for addon in failed:
                key = key_of(addon)
                entry = items.get(key) or {"fails": 0, "until": 0.0, "last": 0.0}
                entry["fails"] = min(entry["fails"] + 1, 1000)
                entry["last"] = now
                if entry["fails"] >= FAILS_TO_SKIP:
                    entry["until"] = now + SKIP_S
                items[key] = entry
                changed = True
            if changed:
                _prune(items, now)
                _save(items)
    except Exception:  # noqa: BLE001 - health is a convenience: it must never break a search
        pass


def state(addon, now=None):
    """-> {"fails": n, "skipped": bool, "until": epoch seconds or 0} for one addon (the tests and a curious owner)."""
    now = _now(now)
    with _lock:
        entry = _load().get(key_of(addon))
    if not entry:
        return {"fails": 0, "skipped": False, "until": 0.0}
    return {"fails": entry["fails"], "skipped": entry["fails"] >= FAILS_TO_SKIP and entry["until"] > now, "until": entry["until"]}


def reset():
    """Forget every count and every skip.  -> how many addons were remembered.  Never raises."""
    with _lock:
        remembered = len(_load())
        try:
            path().unlink()
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - cannot delete it: overwrite it with an empty memory
            _save({})
    return remembered


# --------------------------------------------------------------------------- the sentences for `failed`

def _names(names, limit=6):
    clean = sorted({str(n) for n in names if n})
    text = ", ".join(clean[:limit])
    return text + (" and %d more" % (len(clean) - limit) if len(clean) > limit else "")


def skipped_sentence(names):
    """One sentence for every addon that was not asked because it keeps failing (it is named, never hidden)."""
    return "skipped: failed %d times in a row, not asked again for %d h: %s" % (FAILS_TO_SKIP, SKIP_S // 3600, _names(names))


def local_refused_sentence(names):
    """One sentence for the addons whose private or local address the guard refused."""
    return "refused: add it to allow_private_hosts if intended: %s" % _names(names)
