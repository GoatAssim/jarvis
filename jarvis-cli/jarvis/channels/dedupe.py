"""Has this exact message already been handled?

THE BUG THIS FIXES
------------------
Both platforms can deliver the same message more than once, and neither
gateway noticed.

Instagram is the clearer case. Meta retries a webhook delivery whenever it
doesn't receive a prompt 200 — and "didn't receive" includes the response
being lost on the way back, which the sender cannot distinguish from the
request never arriving. instagram_gateway responds 200 before doing any
work, which makes that window small, but small is not zero: a dropped
response means Meta re-POSTs a payload Jarvis has already fully processed.

Discord has the same shape for a different reason: on a resumed gateway
session discord.py can replay events the client already saw before the
connection dropped.

The consequence is worse than a duplicated reply. An allowlisted sender
with tools enabled saying "send that email" gets the email sent twice,
because nothing between the socket and the tool executor has any notion
that this is the same message. ai_client's per-ask cache dedupes a tool
call WITHIN one ask() (so a provider failover doesn't re-run a real
action), but a redelivery is a whole new ask in a whole new pipeline run,
and that cache has never seen it.

WHY ON DISK AND NOT A SET IN MEMORY
-----------------------------------
A gateway restart is the most likely moment for a redelivery: the process
dropped, so Meta's 200 never arrived, so Meta retries — at which point an
in-memory set is empty and the guard is useless in precisely the case it
exists for. So the store is a file.

WHY A RING AND NOT A FULL LOG
-----------------------------
Only recent ids can plausibly be redelivered; Meta gives up retrying within
hours. Keeping every message id Jarvis has ever seen would grow without
bound to defend against nothing. MAX_IDS is a few thousand — far more than
any retry window, far less than a problem.

WHAT THIS IS NOT
----------------
It is not a cooldown (permissions.py owns that, keyed on sender) and it is
not conversation history (transcript.py owns that). It answers exactly one
question: have I seen THIS message id before.
"""

import time
from pathlib import Path

from .. import atomic_io

CHANNELS_DIR = Path.home() / ".jarvis" / "channels"
SEEN_FILE = CHANNELS_DIR / "seen_messages.json"

# Comfortably larger than any plausible retry burst, small enough that the
# whole file is a cheap read/write on every inbound message.
MAX_IDS = 4000
# Ids older than this can't still be retried by anyone, so they're dropped
# on write even if the ring isn't full.
TTL_SECONDS = 24 * 3600


def _load():
    data = atomic_io.read_json(SEEN_FILE, default={}, expect=dict)
    seen = data.get("seen")
    return seen if isinstance(seen, dict) else {}


def _save(seen):
    atomic_io.write_json(SEEN_FILE, {"seen": seen})


def _key(platform, message_id):
    return f"{platform}:{message_id}"


def already_seen(platform, message_id, now=None):
    """True if this message has been handled before.

    Records it as seen on the way through, so the caller is one call rather
    than a check-then-mark pair that could be interleaved. Returns False for
    an empty message_id: a platform event with no id cannot be deduplicated,
    and treating "no id" as "already seen" would silently drop real
    messages — the far worse failure of the two.
    """
    message_id = str(message_id or "").strip()
    if not message_id:
        return False

    now = time.time() if now is None else now
    key = _key(platform, message_id)
    try:
        seen = _load()
    except Exception:  # noqa: BLE001 — an unreadable store must not stop a
        # reply; the worst case without it is the duplicate this prevents,
        # which is strictly better than dropping every message.
        return False

    if key in seen:
        return True

    seen[key] = now
    # Prune on write rather than on a timer: this is the only moment the
    # file is open anyway, and a store nobody writes to doesn't need pruning.
    if len(seen) > MAX_IDS:
        fresh = {k: v for k, v in seen.items() if now - (v or 0) < TTL_SECONDS}
        if len(fresh) > MAX_IDS:
            ordered = sorted(fresh.items(), key=lambda kv: kv[1] or 0)
            fresh = dict(ordered[-MAX_IDS:])
        seen = fresh
        seen[key] = now
    try:
        _save(seen)
    except Exception:  # noqa: BLE001
        pass
    return False


def forget_all():
    """Test hook, and an escape hatch if the store is ever corrupted."""
    try:
        _save({})
    except Exception:  # noqa: BLE001
        pass
