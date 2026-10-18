"""Notification delivery — the "how it reaches you" half of the scheduler.

scheduler.py decides WHEN something happens. This module decides how the
user actually finds out, across however many surfaces are in play (a
browser tab, a terminal, the OS notification centre, Playnite, a speaker).

THE DURABLE INBOX IS THE POINT
------------------------------
Every other channel here is fire-and-forget: a toast pops whether or not
anyone is at the machine, and a WebSocket broadcast reaches only the tabs
open *right now*. For a reminder, that's not good enough — "remind me at 9"
firing into a closed browser is indistinguishable from never having fired.

So delivery is split in two:

  * The INBOX (~/.jarvis/notifications.json) is durable and always written.
    It's a queue, not a log: the web console drains it on connect and on an
    interval, `jarvis ask` drains it at the top of a reply, and each
    consumer acknowledges what it consumed. A notification raised while
    nothing was running is still waiting the next time anything is.

  * LIVE channels (stream/desktop/voice/playnite) are best-effort extras on
    top. Each is wrapped so an unavailable one (no Playnite running, no TTS
    installed, no notify-send on this distro) degrades to "not delivered
    that way" and never takes down the tick that raised it.

CHANNEL SELECTION
-----------------
Per-notification `channels` wins; otherwise the defaults in
~/.jarvis/notify_config.json apply, which are per-KIND — a reminder
probably wants a toast, a routine background task probably doesn't. Same
plain-JSON, created-on-first-use, re-read-every-call approach as
tool_safety.py and ai_config.py, so there's no reload step and the file is
hand-editable.
"""

import json
import os
import shutil
import subprocess
import sys
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import ask_output
from . import notify_registry as registry
from . import tunables
from . import wire

JARVIS_DIR = Path.home() / ".jarvis"
INBOX_FILE = JARVIS_DIR / "notifications.json"
CONFIG_FILE = JARVIS_DIR / "notify_config.json"
ENCODING = "utf-8"

# E2 / D-10: the native OS notification is called `desktop`. Its old name,
# `toast`, now belongs to the in-app toast (which the web page draws from the
# `stream` record, at the record's `position`). Everything already stored under
# the old name -- notify_config.json, a scheduled job's `channels`, a model's
# `channels` argument -- is read as `desktop`, so nothing changes for it.
#
# E0: the channel list is the `channel` rows of notify_registry.ACTIONS -- add a
# channel there (and its `_deliver_<id>` + `_DELIVERERS` entry below), not here.
CHANNELS = registry.channel_ids()
LEGACY_CHANNEL_ALIASES = {"toast": "desktop"}


def canonical_channel(name):
    """One channel name as notify() understands it: trimmed, lower-cased, the
    legacy alias resolved. Returns None for anything that is not a channel."""
    text = str(name or "").strip().lower()
    text = LEGACY_CHANNEL_ALIASES.get(text, text)
    return text if text in CHANNELS else None


def canonical_channels(names):
    """canonical_channel() over a list, dropping unknowns and duplicates,
    keeping order."""
    out = []
    for name in (names or []):
        canon = canonical_channel(name)
        if canon and canon not in out:
            out.append(canon)
    return out


# E2 / D-11: where an in-app toast appears. Eight places: four sides and four
# corners. D-12: bottom-right unless something says otherwise.
# E0: from notify_registry.POSITIONS (same order as before).
POSITIONS = registry.position_ids()
DEFAULT_POSITION = registry.DEFAULT_POSITION

# Cap on retained notifications. The inbox is a queue — unacknowledged items
# are kept, but a consumer that never acknowledges (a browser nobody opens
# again) shouldn't grow the file without bound.
#
# L.30: raised from 200 and the pruning made state-aware (see _prune). The
# Notifications panel promises "everything ever sent"; a flat newest-200
# window quietly threw away an unread reminder the moment a clipboard burst
# pushed it out of the window. Now the oldest READ items go first, and an item
# that still needs an acknowledgment is the last thing ever dropped.
MAX_INBOX = tunables.const("MAX_INBOX", 500)  # L.43: Settings > Advanced
MAX_MESSAGE_CHARS = 2000
TOAST_TIMEOUT = tunables.const("TOAST_TIMEOUT", 15)  # L.43: Settings > Advanced

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_CONFIG = {
    "enabled": True,
    # Per-kind default channels. "inbox" is in every one of these on
    # purpose: it's the only channel that survives nothing being open.
    # Superseded as the primary knob by "levels" below (D.2.1/K.2.7) —
    # kept, unchanged, as the explicit-`channels`-override path notify()
    # still honors, so nothing that already passes channels= breaks.
    "channels": {
        "reminder": ["inbox", "stream", "desktop"],
        "notify": ["inbox", "stream", "desktop"],
        "task": ["inbox", "stream"],
    },
    # D.2.1 — per-kind (or per custom-tool `source`) importance level
    # default, 1-5, looked up when a caller doesn't pass an explicit
    # `level`. See normalize_level()'s docstring for the full lookup order
    # and LEVEL_NAMES for what each number means. clipboard_watch defaults
    # to 2 (Standard) — a deliberate choice, not a carry-over from the old
    # channels default above: it keeps the previous toast-on-change
    # behavior while making flood control (K.2.7.2) apply to it by default
    # instead of every burst toasting individually.
    # E0: the per-kind defaults live in notify_registry.KINDS (one row per kind,
    # with its label and default level). notify_config.json's own "levels" still
    # overrides them, key by key, exactly as before.
    "levels": registry.default_levels(),
    # E2 -- where an in-app toast appears (see POSITIONS). "position" is the
    # global default; "positions" overrides it per kind (or per custom
    # tool/action `source`), the same shape as "levels". An explicit
    # `position=` on notify() beats both. A value that is not one of
    # POSITIONS is ignored and the next layer down is used.
    "position": DEFAULT_POSITION,
    "positions": {},
    # Windows toasts go through PowerShell's BurntToast module when it's
    # installed (much nicer looking), falling back to a plain balloon via
    # .NET's NotifyIcon, which needs nothing installed at all.
    "prefer_burnt_toast": True,
    "voice_enabled": False,
}

# D.2.1 — five cumulative importance levels, each including everything
# below it:
#   1 Silent      inbox entry only — no toast, no interruption.
#   2 Standard     + a native/live toast (this module's existing default
#                  behavior before this level system existed).
#   3 Persistent   + the toast doesn't auto-dismiss; it re-surfaces until
#                  acknowledged. A delivery-BEHAVIOR change on the same
#                  toast, not a new channel — carried as record["persistent"]
#                  for the frontend to act on, not a CHANNELS entry.
#   4 Broadcast    + pushed as a DM to the owner on every currently
#                  connected channel (discord/instagram), not just this
#                  session's own surface.
#   5 Confirm      + a blocking confirm surface requiring an explicit
#                  acknowledgment, not an ambient toast — record
#                  ["confirm_required"], again a frontend behavior flag
#                  rather than a channel.
# E0: the levels are notify_registry.PRESETS; these three names stay for callers.
LEVEL_NAMES = registry.level_names()
MIN_LEVEL, MAX_LEVEL = registry.MIN_LEVEL, registry.MAX_LEVEL
DEFAULT_LEVEL = registry.DEFAULT_LEVEL

# Accepted for backward/forward compatibility: anything still speaking the
# old three-tier digest.py vocabulary (low/normal/high), or a plain English
# importance word, maps onto the new 1-5 scale. Never the primary interface
# going forward — normalize_level()'s numeric/per-kind paths are — but a
# caller that passes a familiar word instead of memorizing "3" still works.
_LEVEL_ALIASES = {
    "silent": 1, "standard": 2, "persistent": 3, "broadcast": 4, "confirm": 5,
    "low": 1, "normal": 2, "high": 3,
    "routine": 1, "quiet": 1, "batch": 1, "digest": 1, "fyi": 1, "info": 1,
    "urgent": 3, "important": 3, "critical": 3, "now": 3,
}


def normalize_level(value, kind=None, source=None, config=None):
    """Resolve an explicit level to an int 1-5.

    Lookup order:
      1. `value` itself, if it's a valid level (1-5, or one of
         _LEVEL_ALIASES's legacy words/priorities).
      2. `config["levels"][source]` — lets a custom tool/action (K.2.7.1a)
         declare its own default the same way a built-in kind does, keyed
         by its own name instead of a shared "kind".
      3. `config["levels"][kind]`.
      4. DEFAULT_LEVEL.
    """
    if value is not None:
        try:
            lvl = int(value)
            if MIN_LEVEL <= lvl <= MAX_LEVEL:
                return lvl
        except (TypeError, ValueError):
            pass
        text = str(value).strip().lower()
        if text in _LEVEL_ALIASES:
            return _LEVEL_ALIASES[text]

    config = config if config is not None else _load_config()
    levels = config.get("levels") or {}
    for candidate_key in (source, kind):
        if not candidate_key:
            continue
        try:
            lvl = int(levels[candidate_key])
            if MIN_LEVEL <= lvl <= MAX_LEVEL:
                return lvl
        except (KeyError, TypeError, ValueError):
            continue
    return DEFAULT_LEVEL


def normalize_position(value, kind=None, source=None, config=None):
    """Resolve where an in-app toast goes.

    Lookup order (same shape as normalize_level):
      1. `value` itself, if it is one of POSITIONS.
      2. `config["positions"][source]`.
      3. `config["positions"][kind]`.
      4. `config["position"]`, the global default.
      5. DEFAULT_POSITION (bottom-right).
    A value that is not a position is skipped, never an error.
    """
    canon = _canon_position(value)
    if canon:
        return canon
    config = config if config is not None else _load_config()
    positions = config.get("positions") or {}
    if isinstance(positions, dict):
        for candidate_key in (source, kind):
            if not candidate_key:
                continue
            canon = _canon_position(positions.get(candidate_key))
            if canon:
                return canon
    return _canon_position(config.get("position")) or DEFAULT_POSITION


def _canon_position(value):
    """'Bottom Right', 'bottom_right' and 'BOTTOM-RIGHT' are all bottom-right;
    anything else that is not in POSITIONS gives None."""
    if not isinstance(value, str):
        return None
    text = "-".join(value.lower().replace("_", " ").replace("-", " ").split())
    return text if text in POSITIONS else None


def _channels_for_level(level):
    """The channel set implied by a bare level, before any explicit
    `channels` override (which still wins outright — see notify())."""
    return registry.channels_for_level(level)


def _load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        data = {}
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("channels", "levels", "positions") and isinstance(value, dict):
                config[key].update(value)
            else:
                config[key] = value
    # E2 / D-10: a config file written before the rename says "toast" for the
    # native notification. Read it as "desktop" so behaviour is unchanged.
    channels = config.get("channels")
    if isinstance(channels, dict):
        for kind_name, names in list(channels.items()):
            if isinstance(names, list):
                channels[kind_name] = [
                    LEGACY_CHANNEL_ALIASES.get(str(n).strip().lower(), n) for n in names]
    return config


def ensure_config():
    """Write the default config out if it doesn't exist yet, and return its
    path — mirrors ai_config.ensure_ai_config() so `jarvis notify-config`
    can print a path the user can immediately open and edit."""
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            CONFIG_FILE.write_text(
                json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding=ENCODING)
    except OSError:
        pass
    return CONFIG_FILE


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def catalog():
    """E0: everything a notification can be and do, as one JSON-able dict --
    the actions (channels and behaviours), the level presets, the kinds, the
    positions, and (under `configured`) this owner's current defaults. The
    single list that `jarvis notify-config --catalog`, /api/notifications/catalog, the
    Settings screen, the toast renderer and the tool help all read. See
    notify_registry.py for how to add a new kind, action or level."""
    return registry.catalog(_load_config())


def registry_problems():
    """Sentences describing any mismatch between notify_registry's table and
    this module's deliverers (empty list = consistent). Never raises."""
    try:
        return registry.problems(_DELIVERERS.keys())
    except Exception as exc:  # noqa: BLE001
        return ["could not check the notification registry: %s" % exc]


def _load_inbox():
    try:
        data = json.loads(INBOX_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _needs_ack(item):
    """True while a persistent / confirm-level notification has not been
    acknowledged by the owner (L.30). Level 1-2 items never need one."""
    if not isinstance(item, dict):
        return False
    if not (item.get("persistent") or item.get("confirm_required")):
        return False
    return not item.get("acked_at")


def _is_unread(item):
    return isinstance(item, dict) and not item.get("read_at")


def _prune(items):
    """Trim to MAX_INBOX, oldest first, but never drop what the owner still
    has to deal with if something less important can go instead.

    Drop order: (1) read items, (2) unread items that need no acknowledgment,
    (3) items still awaiting an acknowledgment — only if nothing else is left.
    Within a class the oldest goes first; the survivors keep their original
    order, so the file stays chronological.
    """
    if len(items) <= MAX_INBOX:
        return items
    excess = len(items) - MAX_INBOX

    def rank(item):
        if _needs_ack(item):
            return 2
        return 1 if _is_unread(item) else 0

    doomed = set()
    for want in (0, 1, 2):
        for idx, item in enumerate(items):
            if excess <= 0:
                break
            if idx not in doomed and rank(item) == want:
                doomed.add(idx)
                excess -= 1
    return [item for idx, item in enumerate(items) if idx not in doomed]


def _save_inbox(items):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INBOX_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_prune(list(items)), indent=2, default=str) + "\n",
                       encoding=ENCODING)
        os.replace(str(tmp), str(INBOX_FILE))
        return True
    except OSError:
        return False


# A lock around every load-modify-save of the inbox (L.30). The inbox is now
# written from more places at once — a scheduler tick appending, the web
# server marking things read, a terminal draining — and each of those is a
# read-modify-write of one whole file, so two overlapping ones used to lose
# the later writer's change: a notification could vanish, or a "read" mark
# could be undone. Same O_CREAT|O_EXCL approach as subagents.py's spawn lock
# (no fcntl/msvcrt, so identical on Windows and POSIX). Best effort on
# purpose: if the lock cannot be taken in time, proceed without it — a
# notification that arrives is better than one that waits on a stuck lockfile.
_LOCK_STALE_AFTER = 10.0
_LOCK_WAIT = 3.0


@contextmanager
def _inbox_lock():
    lock_path = str(INBOX_FILE.with_suffix(".lock"))
    held = False
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + _LOCK_WAIT
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                held = True
                break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lock_path) > _LOCK_STALE_AFTER:
                        os.remove(lock_path)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    break
                time.sleep(0.03)
    except OSError:
        pass
    try:
        yield held
    finally:
        if held:
            try:
                os.remove(lock_path)
            except OSError:
                pass


def _hydrate(item):
    """Heal a notification record read from disk that predates the
    summary/cleaning fields above (master plan D.2 "existing data" note):
    re-clean `message` and backfill `summary`/`summary_truncated` if
    missing, without rewriting the file — this runs on every read, is
    cheap, and means old data self-heals the next time it's viewed rather
    than needing a migration script. A record that already has a
    `summary` is trusted as-is and left untouched (no repeated work).
    """
    if not isinstance(item, dict):
        return item
    if "summary" in item and not (item.get("message") or "").startswith(ask_output.PROTOCOL_LINE_PREFIXES):
        return _decorate(item)
    item = dict(item)
    cleaned = ask_output.strip_protocol_lines(item.get("message") or "") or ""
    item["message"] = cleaned[:MAX_MESSAGE_CHARS]
    if "summary" not in item:
        summarized = ask_output.summarize(cleaned)
        item["summary"] = summarized["summary"][:MAX_MESSAGE_CHARS]
        item["summary_truncated"] = summarized["truncated"] or len(cleaned) > MAX_MESSAGE_CHARS
    return _decorate(item)


def _decorate(item):
    """Add the derived, never-stored fields the panel reads (L.30): `unread`
    and `needs_ack`. Returns a copy; the stored record is not touched."""
    out = dict(item)
    out["unread"] = _is_unread(item)
    out["needs_ack"] = _needs_ack(item)
    return out


def pending(consumer="web", limit=50):
    """Undelivered notifications for one consumer, oldest first.

    Acknowledgement is tracked PER CONSUMER (`seen_by`), not as a single
    delivered flag, because the web console and a terminal are genuinely
    different places a person might be looking — a reminder the browser
    already showed should still print in a terminal session that hasn't
    seen it, and vice versa.
    """
    consumer = (consumer or "web").strip() or "web"
    out = []
    for item in _load_inbox():
        if consumer in (item.get("seen_by") or []):
            continue
        out.append(_hydrate(item))
        if len(out) >= limit:
            break
    return out


def acknowledge(ids, consumer="web"):
    """Mark notifications as seen by one consumer. Fully-acknowledged old
    items are pruned here rather than on a timer — this is the only place
    that knows every consumer is done with them."""
    consumer = (consumer or "web").strip() or "web"
    wanted = set(ids or [])
    with _inbox_lock():
        items = _load_inbox()
        touched = 0
        for item in items:
            if item.get("id") in wanted:
                seen = list(item.get("seen_by") or [])
                if consumer not in seen:
                    seen.append(consumer)
                    item["seen_by"] = seen
                    touched += 1
        _save_inbox(items)
    return touched


def clear(consumer=None):
    """Drop the inbox. With a consumer, only marks everything seen by them."""
    if consumer:
        items = _load_inbox()
        return acknowledge([i.get("id") for i in items], consumer)
    with _inbox_lock():
        count = len(_load_inbox())
        _save_inbox([])
    return count


def _matches(item, unread_only=False, failed_only=False, needs_ack_only=False,
             kind=None, source=None, query=None):
    if unread_only and not _is_unread(item):
        return False
    if failed_only and not item.get("failed"):
        return False
    if needs_ack_only and not _needs_ack(item):
        return False
    if kind and (item.get("kind") or "notify") != kind:
        return False
    if source and (item.get("source") or "") != source:
        return False
    if query:
        hay = " ".join(str(item.get(k) or "") for k in
                       ("title", "message", "summary", "kind", "source")).lower()
        if not all(word in hay for word in str(query).lower().split()):
            return False
    return True


def history(limit=50, **filters):
    """Newest first, regardless of delivery state. `filters` (all optional):
    unread_only, failed_only, needs_ack_only, kind, source, query — see
    _matches(). With no filters this is exactly what it always was."""
    out = []
    for item in reversed(_load_inbox()):
        if filters and not _matches(item, **filters):
            continue
        out.append(_hydrate(item))
        if len(out) >= limit:
            break
    return out


def summary():
    """Counts for the panel and the badge, computed from the durable inbox so
    they survive a page reload (the old badge was a browser-side counter that
    reset to zero every time the page loaded).

    `needs_ack` lists the persistent / confirm-level items still awaiting the
    owner, newest first — what the page re-surfaces when it (re)connects."""
    items = _load_inbox()
    kinds = {}
    unread = failed_unread = 0
    awaiting = []
    for item in items:
        kinds[item.get("kind") or "notify"] = kinds.get(item.get("kind") or "notify", 0) + 1
        if _is_unread(item):
            unread += 1
            if item.get("failed"):
                failed_unread += 1
        if _needs_ack(item):
            awaiting.append(_hydrate(item))
    awaiting.reverse()
    return {
        "total": len(items),
        "unread": unread,
        "failed_unread": failed_unread,
        "needs_ack": awaiting,
        "kinds": kinds,
    }


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def mark_read(ids, ack=True):
    """The owner has seen these (L.30). Sets `read_at`; with `ack` (the
    default) it also sets `acked_at` on a persistent / confirm-level item, so
    reading it in the panel or clicking Acknowledge on its toast stops it
    from re-surfacing. Returns how many records changed.

    This is the OWNER's read state. It is deliberately separate from
    `seen_by` (acknowledge() above), which only records which consumer a
    notification was *delivered* to — delivery is not reading."""
    wanted = set(ids or [])
    if not wanted:
        return 0
    changed = 0
    stamp = _now()
    with _inbox_lock():
        items = _load_inbox()
        for item in items:
            if item.get("id") not in wanted:
                continue
            touched = False
            if not item.get("read_at"):
                item["read_at"] = stamp
                touched = True
            if ack and (item.get("persistent") or item.get("confirm_required")) \
                    and not item.get("acked_at"):
                item["acked_at"] = stamp
                touched = True
            changed += 1 if touched else 0
        if changed:
            _save_inbox(items)
    return changed


def mark_all_read():
    """Mark everything read — and acknowledged, since "mark all read" is an
    explicit act by the owner."""
    stamp = _now()
    changed = 0
    with _inbox_lock():
        items = _load_inbox()
        for item in items:
            touched = False
            if not item.get("read_at"):
                item["read_at"] = stamp
                touched = True
            if (item.get("persistent") or item.get("confirm_required")) \
                    and not item.get("acked_at"):
                item["acked_at"] = stamp
                touched = True
            changed += 1 if touched else 0
        if changed:
            _save_inbox(items)
    return changed


def dismiss(ids):
    """Delete specific notifications from the inbox for good."""
    wanted = set(ids or [])
    if not wanted:
        return 0
    with _inbox_lock():
        items = _load_inbox()
        kept = [i for i in items if i.get("id") not in wanted]
        removed = len(items) - len(kept)
        if removed:
            _save_inbox(kept)
    return removed


def clear_read():
    """Delete every notification that has been read, keeping the unread."""
    with _inbox_lock():
        items = _load_inbox()
        kept = [i for i in items if _is_unread(i)]
        removed = len(items) - len(kept)
        if removed:
            _save_inbox(kept)
    return removed


# ---------------------------------------------------------------------------
# The one public entry point
# ---------------------------------------------------------------------------


def notify(title, message, channels=None, kind="notify", job_id=None,
           conv_id=None, failed=False, actions=None, level=None, source=None,
           position=None):
    """Deliver one notification across every resolved channel.

    Never raises. Returns the notification record, with a `delivered` list
    of the channels that actually worked and `failed_channels` for the ones
    that didn't — visible in `jarvis sched-tick` output, because a toast
    that silently isn't appearing is otherwise very hard to diagnose.

    `message` is run through ask_output.strip_protocol_lines() before
    anything else — a defense-in-depth backstop (master plan D.2), not the
    only place this happens: scheduler.py already cleans a scheduled ask/
    command's captured stdout before it ever reaches here. This catches
    any OTHER call site that passes raw captured stdout straight through,
    now or in the future, without every one of them having to remember to
    strip it themselves. It's a no-op for ordinary text (a reminder body,
    a plain notify-send message) since that never starts with a marker
    prefix in the first place.

    The record also carries a `summary` (first line / first ~200 chars)
    and `summary_truncated` flag alongside the full `message`, so a list
    view (the web Notifications panel, `jarvis notify-list`) can show the
    short form with the full text available on expand, instead of dumping
    an entire reply — including, for a task/command notification, output
    that can run to paragraphs — into every entry in the list.

    `level` (1-5, D.2.1/K.2.7) resolves via normalize_level() — an explicit
    value here, else `source`'s or `kind`'s configured default, else
    DEFAULT_LEVEL. `source`, if given, tags where this came from (a custom
    tool/action's own name, K.2.7.1a) and can also carry its own configured
    default level the same way a built-in `kind` does.

    `position` (E2) is where the in-app toast is drawn: one of POSITIONS.
    It resolves via normalize_position() -- an explicit value here, else
    `source`'s or `kind`'s configured position, else the global default,
    else bottom-right -- and rides on the record as `record["position"]`,
    which the web page reads. It changes nothing about delivery.

    An explicit `channels` list still wins outright over whatever the
    resolved level implies — this is the same "explicit beats default"
    contract the old channels-only model already had, just with the level
    as the new default source instead of `kind` alone.

    Levels 1-2 are digest-eligible: when digest.py's batching is enabled,
    delivery beyond the durable inbox is deferred into the batched summary
    instead of firing now (record["deferred_to_digest"] = True). Levels 3-5
    never batch or get suppressed, matching the old "high" priority's
    guarantee.
    """
    config = _load_config()
    resolved_level = normalize_level(level, kind=kind, source=source, config=config)
    resolved_position = normalize_position(position, kind=kind, source=source, config=config)
    clean_message = ask_output.strip_protocol_lines((message or "").strip()) or ""
    summarized = ask_output.summarize(clean_message)
    record = {
        "id": secrets.token_hex(6),
        "title": (title or "Jarvis").strip()[:200],
        "message": clean_message[:MAX_MESSAGE_CHARS],
        "summary": summarized["summary"][:MAX_MESSAGE_CHARS],
        "summary_truncated": summarized["truncated"] or len(clean_message) > MAX_MESSAGE_CHARS,
        "kind": kind or "notify",
        "job_id": job_id,
        "conv_id": conv_id,
        "failed": bool(failed),
        "actions": list(actions or []),
        "created_at": datetime.now().replace(microsecond=0).isoformat(),
        "seen_by": [],
        "delivered": [],
        "failed_channels": [],
        "level": resolved_level,
        "level_name": LEVEL_NAMES.get(resolved_level, "standard"),
        "position": resolved_position,
        # E0: `persistent` and `confirm_required`, one key per behaviour action in
        # notify_registry.ACTIONS, set by what the level's preset includes.
        **registry.flags_for_level(resolved_level),
        # L.30 — the owner's read/acknowledge state, stamped later by
        # mark_read()/mark_all_read(). Absent == not read yet.
        "read_at": None,
        "acked_at": None,
    }
    if source:
        record["source"] = str(source)[:100]

    if not config.get("enabled", True):
        record["delivered"] = []
        record["failed_channels"] = ["disabled"]
        return record

    if channels:
        wanted = canonical_channels(channels)
        if "inbox" not in wanted:
            wanted = ["inbox"] + wanted
    else:
        wanted = _channels_for_level(resolved_level)

    # K.2.7 digest interaction: levels 1-2 defer their non-inbox delivery to
    # the batched summary when digest is on, exactly like the old low/normal
    # priorities did — just keyed off the new resolved level, the single
    # source of truth digest.py now reads too (see digest.should_batch_level).
    from . import digest as digest_mod
    if (not channels and registry.digest_eligible(resolved_level)
            and digest_mod.should_batch_level(resolved_level)):
        try:
            ok = _deliver_inbox(record, config)
        except Exception:  # noqa: BLE001
            ok = False
        (record["delivered"] if ok else record["failed_channels"]).append("inbox")
        try:
            digest_mod.enqueue(record)
            record["deferred_to_digest"] = True
        except Exception:  # noqa: BLE001 — digest is advice, never a reason to fail delivery
            record["deferred_to_digest"] = False
        return record

    for channel in dict.fromkeys(wanted):
        try:
            ok = _DELIVERERS[channel](record, config)
        except Exception:  # noqa: BLE001 — a broken channel is not fatal
            ok = False
        (record["delivered"] if ok else record["failed_channels"]).append(channel)

    return record


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def _deliver_inbox(record, config):
    with _inbox_lock():
        items = _load_inbox()
        items.append(record)
        return _save_inbox(items)


def _deliver_stream(record, config):
    """Announce the notification as a `notification` media card
    (wire.media) so a web console watching this process renders it
    immediately.

    Same envelope every other producer uses (see present_tools.py /
    dev_agent_events.py): the whole payload as one JSON field. This may run
    in a daemon or a plain terminal with no host, where wire.media prints
    the legacy `JARVIS_MEDIA` line exactly as this function always did.
    """
    # seen_by/delivered/failed_channels are deliberately excluded: this runs
    # *during* the delivery loop, so those three are a half-filled snapshot
    # that would be actively misleading in the browser ("delivered: inbox"
    # on a notification that also toasted). The UI doesn't need them.
    skip = ("seen_by", "delivered", "failed_channels")
    try:
        payload = json.dumps(
            {k: v for k, v in record.items() if k not in skip},
            default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    try:
        wire.media("notification", payload)
    except Exception:  # noqa: BLE001
        return False
    return True


def _deliver_desktop(record, config):
    """Native OS notification (called `toast` before E2). Best-effort by
    design — there is no portable way to do this, so each platform gets its
    own attempt and a failure just means the inbox carries the notification
    instead."""
    text = (record.get("summary") or record["message"]).replace("\r", " ").replace("\n", " ")[:250]
    title = record["title"][:64]
    if sys.platform.startswith("win"):
        return _toast_windows(title, text, config)
    if sys.platform == "darwin":
        return _toast_macos(title, text)
    return _toast_linux(title, text, record)


def _toast_windows(title, text, config):
    # BurntToast produces a real Windows 10/11 action-centre toast, but it's
    # a third-party module that may not be installed. The NotifyIcon
    # fallback below needs nothing beyond stock .NET, so there's always a
    # path that works.
    if config.get("prefer_burnt_toast", True):
        script = (
            "if (Get-Module -ListAvailable -Name BurntToast) {"
            " Import-Module BurntToast;"
            " New-BurntToastNotification -Text %s,%s; exit 0 } else { exit 3 }"
            % (_ps_quote(title), _ps_quote(text))
        )
        if _run_powershell(script):
            return True
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = %s;"
        "$n.BalloonTipText = %s;"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(10000);"
        "Start-Sleep -Seconds 6;"
        "$n.Dispose()" % (_ps_quote(title), _ps_quote(text))
    )
    return _run_powershell(script)


def _ps_quote(text):
    """Single-quoted PowerShell literal — the only quoting style where the
    sole escape needed is doubling the quote itself, so a notification
    containing $, backticks, or quotes can't turn into an injected command."""
    return "'" + str(text).replace("'", "''") + "'"


def _run_powershell(script):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=TOAST_TIMEOUT, creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _toast_macos(title, text):
    exe = shutil.which("osascript")
    if not exe:
        return False
    script = 'display notification %s with title %s' % (
        json.dumps(text), json.dumps(title))
    try:
        return subprocess.run([exe, "-e", script], capture_output=True,
                              timeout=TOAST_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _toast_linux(title, text, record):
    exe = shutil.which("notify-send")
    if not exe:
        return False
    urgency = "critical" if record.get("failed") else "normal"
    try:
        return subprocess.run([exe, "-u", urgency, "-a", "Jarvis", title, text],
                              capture_output=True, timeout=TOAST_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _deliver_voice(record, config):
    """Speak it. Off by default (voice_enabled) — a scheduled job talking out
    loud in a quiet room is a genuinely unwelcome surprise if nobody asked
    for it, so this one channel opts in rather than out."""
    if not config.get("voice_enabled"):
        return False
    try:
        from .voice import config as voice_config, tts as voice_tts
    except ImportError:
        return False
    if not voice_config.voice_enabled():
        return False
    spoken = "%s. %s" % (record["title"], record["message"])
    try:
        voice_tts.speak(spoken[:500])
        return True
    except Exception:  # noqa: BLE001 — TTS backends fail in many ways
        return False


def _deliver_playnite(record, config):
    """Reuse the existing playnite_notify tool rather than reimplementing its
    HTTP call — if the bridge's API changes, this follows automatically."""
    try:
        from . import playnite_api_tools
    except ImportError:
        return False
    try:
        result = playnite_api_tools.tool_playnite_notify({
            "text": "%s: %s" % (record["title"], record["message"][:180]),
            "type": "Error" if record.get("failed") else "Info",
        })
    except Exception:  # noqa: BLE001
        return False
    return not (isinstance(result, dict) and result.get("error"))


def _deliver_chat(record, config, platform):
    """Deliver one notification as a DM to the owner on a chat platform.

    Best-effort in the strongest sense: Instagram can only be reached
    inside a 24-hour window the owner opens themselves, and Discord needs
    a shared server. Both refuse as a matter of normal operation, not as
    an error — which is exactly why the inbox channel is always written
    too (see this module's docstring). Returning False here marks the
    channel as failed in `failed_channels` and changes nothing else.
    """
    try:
        from .channels import outbound
    except ImportError:
        return False
    title = (record.get("title") or "Jarvis").strip()
    body = (record.get("message") or "").strip()
    text = f"**{title}**\n{body}" if platform == "discord" else f"{title}\n{body}"
    ok, _detail = outbound.dm_owner(platform, text.strip())
    return bool(ok)


def _deliver_discord(record, config):
    return _deliver_chat(record, config, "discord")


def _deliver_instagram(record, config):
    return _deliver_chat(record, config, "instagram")


_DELIVERERS = {
    "inbox": _deliver_inbox,
    "stream": _deliver_stream,
    "desktop": _deliver_desktop,
    "voice": _deliver_voice,
    "playnite": _deliver_playnite,
    "discord": _deliver_discord,
    "instagram": _deliver_instagram,
}


# ---------------------------------------------------------------------------
# CLI-side rendering
# ---------------------------------------------------------------------------


def render_for_terminal(items):
    """Format pending notifications for a plain terminal. Returns "" when
    there's nothing, so callers can `if text: print(text)` without having to
    special-case an empty run."""
    if not items:
        return ""
    lines = []
    for item in items:
        mark = "!" if item.get("failed") else "*"
        head = "%s %s" % (mark, item.get("title") or "Jarvis")
        when = (item.get("created_at") or "")[11:16]
        if when:
            head += "  (%s)" % when
        lines.append(head)
        for line in (item.get("message") or "").splitlines():
            lines.append("  " + line)
    return "\n".join(lines)


def drain_for_cli(limit=10):
    """Fetch and immediately acknowledge everything waiting for a terminal
    session. One call, because every CLI caller wants both halves and a
    fetch without an ack would reprint the same reminder on every command
    until the end of time."""
    items = pending(consumer="cli", limit=limit)
    if items:
        acknowledge([i.get("id") for i in items], consumer="cli")
    return items
