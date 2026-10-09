"""Pictures a tool made during a CHAT ask, collected so the gateway can send them.

WHY THIS EXISTS (L.42.3, outbound half)
---------------------------------------
Tools that make an image (`take_screenshot`, `browser_screenshot`, `present_file`)
announce it with a `JARVIS_MEDIA` line on stderr. The web server reads that line;
a gateway process (Discord) never did, so the picture was made and nobody got it.

This module is the in-process equivalent: `begin()` opens a collector for ONE ask,
a tool calls `offer(path)`, the gateway calls `end()` and sends what was kept.

It is deliberately dumb. It decides nothing about WHO may receive a file:
`begin(allow=...)` is told by the gateway code (owner-only, see
channels/media_send.py) and `offer()` refuses when nobody is collecting, so in the
CLI and the web UI -- where no collector is open -- it does nothing at all.
Asks in a gateway are serialized by channels/base._ASK_LOCK, so one module-level
collector is enough; it is still guarded by its own lock.
"""

import threading

_LOCK = threading.Lock()
_ACTIVE = None   # {"allow": bool, "items": [(path_str, kind)]} while an ask runs
MAX_ITEMS = 8    # more than this in one ask is a loop, not a request


def begin(allow=True):
    """Start collecting for one ask. `allow=False` collects nothing (a guest's ask)."""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = {"allow": bool(allow), "items": []}


def end():
    """Stop collecting; return [(path, kind), ...] in the order they were offered."""
    global _ACTIVE
    with _LOCK:
        state, _ACTIVE = _ACTIVE, None
    return list(state["items"]) if state else []


def offer(path, kind="image"):
    """A tool says: 'this file is for the person who asked'. True if it was kept.

    False (and nothing kept) when no ask is collecting, the sender may not
    receive files, the same file was already offered, or the cap is reached --
    callers use that to tell the model whether the picture was really sent.
    """
    with _LOCK:
        if not _ACTIVE or not _ACTIVE["allow"]:
            return False
        p = str(path)
        if any(p == existing for existing, _ in _ACTIVE["items"]):
            return True
        if len(_ACTIVE["items"]) >= MAX_ITEMS:
            return False
        _ACTIVE["items"].append((p, str(kind or "image")))
        return True


def is_collecting():
    with _LOCK:
        return bool(_ACTIVE and _ACTIVE["allow"])
