"""The back end of `jarvis hub-admin` (Download Hub, phase 8 item 17.3-2 and phase 9 option B).

One verb serves three front-end pieces, the way `calendar-admin` serves the Calendars panel:

  * the live status card in the chat (`card`),
  * the Downloads panel's push stream, thumbnails and files (`watch`, `thumb`, `file`),
  * the panel's History tab and the drag-to-reorder queue (`history`, `history_remove`,
    `history_clear`, `redownload`, `reorder`) plus the Settings tab's read of the hub settings
    (`settings`);
  * the Stremio status block (`stremio_status`, read-only), the Locate... button's save
    (`stremio_save`: the owner's paths into ~/.jarvis/stremio.json; N9, brief 12) and the Start button
    (`stremio_start`: the server only, through `tool_stack_start`; brief 19);
  * hiding a finished or lost provider row (`dismiss`; brief 19).  It hides, it deletes no row and no file;
  * the Updates tab (N23 part f): `updates` (read: the stored findings and how each provider's checks are going),
    `updates_check` (Check now: sets a flag the daemon's runner picks up; nothing is started here) and
    `updates_dismiss` (Ignore this version: hides one finding), and the Updates rework's three: `updates_apply`
    (queue one finding, or all of a group, for the daemon's apply runner), `updates_dry_run` (the plan the provider's own
    apply tool gives; it changes nothing) and `updates_set` (the auto-update switches and the group / provider
    settings).  The logic is `hub/updates_admin.py`; nothing here installs, upgrades or removes anything: only a
    provider's own apply tool changes the machine.

It is NOT a command to type.  The request is one JSON object on stdin, the answer is one JSON
object on stdout (`watch` prints one JSON object per line until it is stopped).  `{"ok": false,
"error": ...}` is a refusal, never a crash.

The rules this module keeps (they are the Download Hub invariants in AGENTS.md, not new ones):

  * every read goes through `hub.service` / `hub.store`; nothing here opens the sqlite file itself;
  * a path or a thumbnail is resolved from the database by item id, never taken from the caller,
    and a file must sit inside the library root / the folder the owner chose (hub rows) or the
    provider's `open_roots` (provider rows), exactly as `service.open_item` checks it;
  * the writes (`history_remove`, `history_clear`, `redownload`, `reorder`, `dismiss`) touch ROWS only.  No file
    is ever deleted here, a provider's rows are never removed (the provider would just report
    them again), and an unattended process (a chat reply, a scheduled job, a sub-agent) is refused;
  * no model-facing tool reaches this module.  `present_download_status` calls only `card_payload`,
    which is read-only.
"""

import base64
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

# Same guard as calendar_tools.unattended_reason(): a chat reply, a scheduled job and a subagent all
# run the same CLI, and the writes below are the owner's own button presses.
_UNATTENDED_VARS = ("JARVIS_CHANNEL", "JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_TASK_ID")
_UNATTENDED_MESSAGE = "The Download Hub's History and queue controls can only be used from the Jarvis page itself."

# `sources` (brief 24) is the Sources tab: every engine and every provider with an honest status.  Read-only.
READ_OPS = ("card", "list", "settings", "history", "file", "thumb", "stremio_status", "sources", "qbittorrent_status", "updates")
# `dismiss` (brief 19) hides a provider row that is unknown or finished; `stremio_start` (brief 19) is the Start button.
WRITE_OPS = ("history_remove", "history_clear", "redownload", "reorder", "stremio_save", "dismiss", "stremio_start", "stremio_account",
             "qbittorrent_save", "updates_check", "updates_dismiss", "updates_apply", "updates_dry_run", "updates_set")
OPS = READ_OPS + WRITE_OPS
# N14 (the path picker, jarvis/hub_browse.py): list one folder, and make one new folder.  Neither reads a file's
# contents.  Both are for the Jarvis page's own picker, so both refuse an unattended process (below).
PICKER_OPS = ("browse", "mkdir")
OPS = OPS + PICKER_OPS
# N24 (the torrent file picker): `torrent_files` reads one torrent's file list (for a magnet it asks Stremio's server
# to register the torrent, which is why it is not a plain read); `torrent_add` queues the chosen files as one group.
# Both are the Jarvis page's own buttons, so both refuse an unattended process (a chat reply, a scheduled job).
TORRENT_OPS = ("torrent_files", "torrent_add")
OPS = OPS + TORRENT_OPS

MAX_CARD_IDS = 8
MAX_IDS = 200
MAX_HISTORY = 500
MAX_THUMB_BYTES = 256 * 1024
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
THUMB_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=]+)$")
THUMB_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
LIVE_SECONDS = 1.0                     # the watcher's tick
SUMMARY_EVERY = 2                      # ticks between summaries
HEARTBEAT_S = 15.0


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def unattended_reason():
    for var in _UNATTENDED_VARS:
        if os.environ.get(var):
            return _UNATTENDED_MESSAGE
    if os.environ.get("JARVIS_CONTEXT") in ("scheduled", "unattended"):
        return _UNATTENDED_MESSAGE
    return None


def _modules():
    # lazy: the hub package is only needed once a request arrives (tool files must not import it
    # at module level, and this file may be imported by one)
    from .hub import naming, service, settings as settings_mod, states
    return service, settings_mod, states, naming


def _store(store):
    service = _modules()[0]
    return service._store(store)       # noqa: SLF001 - the one place a Store is made from the default path


def _clean_ids(value, limit):
    """A list of plausible item ids (strings), duplicates dropped, at most `limit`."""
    out = []
    for raw in value if isinstance(value, list) else []:
        text = str(raw or "").strip()
        if ITEM_ID_RE.match(text) and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- rows

def present_row(item, providers, now):
    """The same row the panel and `dl_list` show, plus `has_thumb` (a cover is stored)."""
    service = _modules()[0]
    row = service.present_item(item, providers, now)
    row["has_thumb"] = bool(item.get("thumb_ref"))
    # the order the queue will run in (the panel's drag-to-reorder needs it; dl_list rows don't carry it)
    row["priority"] = int(item.get("priority") or 0)
    # a row the owner hid (epoch seconds), None while it is shown; `service.present_item` doesn't carry it, and the
    # panel's "Show hidden" needs it (brief 19)
    row["dismissed_at"] = item.get("dismissed_at")
    # when the transfer began (History shows how long it took); `service.present_item` doesn't carry it
    row["started_at"] = item.get("started_at")
    return row


def _rows(store, items):
    service = _modules()[0]
    providers = service._provider_map(store)   # noqa: SLF001
    now = store.now()
    rows = [present_row(i, providers, now) for i in items]
    if any(i.get("group_id") for i in items):
        # N24: a file of a torrent group says which file it is and how many the torrent has, so the panel can
        # draw the group card ("7 of 23 files") and offer "Add more files" without a second request.
        try:
            groups = service.torrent_groups(store)
        except Exception:  # noqa: BLE001 - the list must still answer
            groups = {}
        if groups:
            for item, row in zip(items, rows):
                info = groups.get(item.get("group_id"))
                if info:
                    row["torrent"] = {"index": service._file_index_of(item), "files": info.get("files"),   # noqa: SLF001
                                      "name": info.get("name"), "infohash": info.get("infohash")}
    return rows


def _final(states, state):
    return state in states.FINAL_STATES


# --------------------------------------------------------------------------- the status card

def group_card(group_id, rows, title=""):
    """One card for a whole group: the counts, one overall bar and the speed.  Pure."""
    counts = {"total": len(rows), "done": 0, "running": 0, "queued": 0, "paused": 0, "failed": 0,
              "cancelled": 0, "stalled": 0, "unknown": 0}
    speed = 0.0
    parts = []
    stale = 0
    for r in rows:
        state = r.get("state")
        if state == "done":
            counts["done"] += 1
        elif state in ("probing", "connecting", "downloading", "postprocessing"):
            counts["running"] += 1
        elif state in counts:
            counts[state] += 1
        else:
            counts["unknown"] += 1
        if r.get("stale") and state not in ("done", "failed", "cancelled"):
            stale += 1
        if isinstance(r.get("speed_bps"), (int, float)) and state in ("downloading", "postprocessing"):
            speed += float(r["speed_bps"])
        if state == "done":
            pct = 100.0
        elif isinstance(r.get("percent"), (int, float)):
            pct = max(0.0, min(100.0, float(r["percent"])))
        else:
            pct = None
        weight = r.get("bytes_total") if isinstance(r.get("bytes_total"), (int, float)) and r["bytes_total"] > 0 else None
        parts.append((pct, weight))
    known = [p for p in parts if p[0] is not None]
    percent = None
    if known:
        if all(w for _, w in known) and len(known) == len(parts):
            total = sum(w for _, w in known)
            percent = sum(p * w for p, w in known) / total
        else:
            percent = sum(p for p, _ in known) / len(parts)
    finished = counts["total"] > 0 and counts["done"] + counts["failed"] + counts["cancelled"] == counts["total"]
    if not counts["total"]:
        state = "unknown"
    elif finished:
        state = "done" if counts["failed"] == 0 and counts["cancelled"] == 0 else "failed"
    elif counts["running"]:
        state = "downloading"
    elif counts["stalled"]:
        state = "stalled"
    elif counts["paused"] and not counts["queued"]:
        state = "paused"
    else:
        state = "queued"
    live = counts["total"] - counts["done"] - counts["failed"] - counts["cancelled"]
    return {"card": "group", "id": str(group_id), "title": title or "Batch", "state": state, "percent": percent,
            "speed_bps": speed or None, "counts": counts, "final": finished,
            "stale": bool(live) and stale == live, "attention": counts["failed"] + counts["stalled"]}


def card_payload(ids=None, group=None, query=None, store=None, limit=MAX_CARD_IDS):
    """What the in-chat status card shows, read-only.

    `ids`: download ids; `group`: a group id (one card with the counts); `query`: text matched
    against titles (several matches give several cards, newest first).  Returns
    {"ok": True, "cards": [...]} where a download card is the panel's row plus {"card": "item",
    "final": bool} and a group card is `group_card`'s dict ({"card": "group", ...}).  Nothing is written.
    """
    try:
        service, _, states, _ = _modules()
        store = _store(store)
        limit = max(1, min(int(limit or MAX_CARD_IDS), MAX_CARD_IDS))
        items = []
        seen = set()

        def take(item):
            if item and item["id"] not in seen and len(items) < limit:
                seen.add(item["id"])
                items.append(item)

        for item_id in _clean_ids(ids, limit):
            take(store.get_item(item_id))
        if query and not items and not group:
            for item in store.list_items(search=str(query)[:80], limit=limit, include_disabled=False):
                take(item)
        cards = []
        if group and ITEM_ID_RE.match(str(group)):
            members = store.list_items(group_id=str(group), limit=500)
            if members:
                rows = _rows(store, members)
                title = (rows[0].get("group_title") or "").strip() if rows else ""
                cards.append(group_card(group, rows, title))
        for row in _rows(store, items):
            row["card"] = "item"
            row["final"] = _final(states, row["state"])
            cards.append(row)
        if not cards:
            return _err("no such download" if (ids or group) else "nothing matches that")
        return {"ok": True, "cards": cards, "at": time.time()}
    except Exception as exc:  # noqa: BLE001 - read-only, must answer
        return _err("couldn't read the download status: %s" % exc)


# --------------------------------------------------------------------------- the push stream

def _quantised(row):
    """A row as it is compared between ticks: `age_s` only moves in 10 s steps so a silent
    provider row doesn't produce a message every second."""
    copy = dict(row)
    if isinstance(copy.get("age_s"), (int, float)):
        copy["age_s"] = int(copy["age_s"] // 10)
    return json.dumps(copy, sort_keys=True, default=str)


def diff_rows(previous, rows):
    """(upserts, removed_ids, new_map).  `previous` maps id -> the compared JSON of the last
    tick.  Pure, so the delta logic is tested without a database."""
    current = {}
    upserts = []
    for row in rows:
        key = _quantised(row)
        current[row["id"]] = key
        if previous.get(row["id"]) != key:
            upserts.append(row)
    removed = [i for i in previous if i not in current]
    return upserts, removed, current


def snapshot(store=None, limit=500, include_dismissed=False):
    """The rows and the summary.  `include_dismissed` also returns the rows the owner hid (they carry
    `dismissed_at`); the push stream never does, so the panel asks for them with the `list` op only while
    its "Show hidden" switch is on."""
    service = _modules()[0]
    store = _store(store)
    rows = _rows(store, store.list_items(limit=max(1, min(int(limit), 2000)),
                                         include_dismissed=bool(include_dismissed)))
    summary = service.summary(store)
    summary.pop("notify_options", None)      # the panel reads it with its own polls; it is big
    return {"ok": True, "items": rows, "summary": summary, "at": time.time()}


def watch_events(store=None, interval=LIVE_SECONDS, stop=None, clock=time.time, sleep=time.sleep, limit=500):
    """Yield the stream the Downloads panel and the chat card read: one `snapshot`, then a
    `delta` ({"upsert": [rows], "remove": [ids], "summary": {...}?}) whenever something changed,
    and a `beat` every HEARTBEAT_S so a client can tell a quiet hub from a dead stream.
    Reads only.  `stop()` returning True ends the generator."""
    service = _modules()[0]
    store = _store(store)
    first = snapshot(store, limit)
    yield dict(first, type="snapshot")
    previous = {r["id"]: _quantised(r) for r in first["items"]}
    last_summary = json.dumps(first["summary"], sort_keys=True, default=str)
    last_sent = clock()
    tick = 0
    while not (stop and stop()):
        sleep(interval)
        tick += 1
        try:
            rows = _rows(store, store.list_items(limit=limit))
            upserts, removed, previous = diff_rows(previous, rows)
            message = {"type": "delta", "upsert": upserts, "remove": removed}
            if tick % SUMMARY_EVERY == 0 or upserts or removed:
                summary = service.summary(store)
                summary.pop("notify_options", None)
                text = json.dumps(summary, sort_keys=True, default=str)
                if text != last_summary:
                    last_summary = text
                    message["summary"] = summary
            if upserts or removed or "summary" in message:
                message["at"] = clock()
                last_sent = message["at"]
                yield message
            elif clock() - last_sent >= HEARTBEAT_S:
                last_sent = clock()
                yield {"type": "beat", "at": last_sent}
        except Exception as exc:  # noqa: BLE001 - a bad tick must not end the stream
            yield {"type": "error", "error": str(exc)[:160], "at": clock()}


# --------------------------------------------------------------------------- files and thumbnails

def _openable_path(store, item):
    """The file a row points at, only if it sits where the hub may open it (the same test
    `service.open_item` applies before it launches anything).  Returns (path, error)."""
    _, settings_mod, _, naming = _modules()
    path = item.get("dest_file")
    if not path:
        return None, "that download has no file yet"
    if item["owner"] == "hub":
        roots = settings_mod.write_roots(store.get_settings(), item.get("options"))
    else:
        roots = [r for r in ((store.get_provider(item["provider"]) or {}).get("open_roots") or [])]
    if not roots or not any(naming.under(r, path) for r in roots):
        return None, "that file isn't inside a folder the hub may open"
    if not os.path.isfile(path):
        return None, "the file is no longer there"
    return str(path), None


def resolve_file(item_id, store=None):
    """{"ok": True, "path", "name", "size"} for `GET /api/hub/file/:itemId`, or a refusal."""
    try:
        store = _store(store)
        if not ITEM_ID_RE.match(str(item_id or "")):
            return _err("that isn't a download id")
        item = store.get_item(str(item_id))
        if not item:
            return _err("no such download")
        if item["state"] != "done":
            return _err("that download isn't finished")
        path, problem = _openable_path(store, item)
        if problem:
            return _err(problem)
        return {"ok": True, "path": path, "name": os.path.basename(path), "size": os.path.getsize(path)}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read that file: %s" % exc)


def resolve_thumb(item_id, store=None):
    """{"ok": True, "mime", "data"} (base64) for `GET /api/hub/thumb/:itemId`.  The store only
    keeps small image data-URIs (states.clean_thumb); this re-checks the type and the size so
    the route can never serve anything but a picture of a known type."""
    try:
        store = _store(store)
        if not ITEM_ID_RE.match(str(item_id or "")):
            return _err("that isn't a download id")
        item = store.get_item(str(item_id))
        if not item or not item.get("thumb_ref"):
            return _err("no thumbnail")
        match = THUMB_RE.match(str(item["thumb_ref"]))
        if not match:
            return _err("no thumbnail")
        data = match.group(2)
        raw_len = (len(data) * 3) // 4
        if raw_len > MAX_THUMB_BYTES:
            return _err("that thumbnail is too large")
        try:
            base64.b64decode(data, validate=True)
        except ValueError:
            return _err("no thumbnail")
        return {"ok": True, "mime": THUMB_MIME[match.group(1)], "data": data}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read that thumbnail: %s" % exc)


# --------------------------------------------------------------------------- settings (read)

def _overridden_by():
    """{"qbittorrent.tag": "qbittorrent.json", ...}: the hub settings whose saved value is NOT the one in force,
    because ~/.jarvis/qbittorrent.json sets the same key and the file wins (brief 7C).  Names only: the engine's
    `file_overridden_keys` never hands over a value, so no address, user name or password can reach this answer.
    {} when there is no file, it can't be read, or anything fails."""
    try:
        from .hub.engines import qbittorrent
        return {"qbittorrent." + key: qbittorrent.CONFIG_NAME for key in qbittorrent.file_overridden_keys()}
    except Exception:  # noqa: BLE001 - the Settings read never fails over this
        return {}


def _update_settings(store):
    from .hub import updates_admin
    return updates_admin.provider_update_settings(store)


def settings_payload(store=None):
    """What the Settings tab's Downloads section needs and nothing more: the merged settings, which
    keys `dl_prefs` / `dl_settings` may write (read from hub/settings.py, never copied), the
    library folder info and the providers.  The values are written back through those two tools, so
    their validators and confirmations stay the only ones."""
    try:
        service, settings_mod, _, _ = _modules()
        store = _store(store)
        settings = store.get_settings()
        view = service.settings_view(settings)
        view.pop("notify_options", None)         # the Settings tab reads the live notifier catalog itself
        # `defaults`: hub/settings.py's own DEFAULTS, copied as they are (brief 25, B2) so the page can say what a setting is
        # normally and offer Reset.  Read-only: nothing here adds or changes a default.
        return {"ok": True, "settings": settings, "view": view, "defaults": copy.deepcopy(settings_mod.DEFAULTS),
                "writable": sorted(settings_mod.WRITABLE), "prefs_writable": sorted(settings_mod.PREFS_WRITABLE),
                "events": list(settings_mod.EVENTS), "open_modes": list(settings_mod.OPEN_MODES),
                "overridden_by": _overridden_by(),
                "providers": service.list_providers(store).get("providers", []),
                # N23: the two update settings of the providers that have an update check (read-only here; they are
                # written with dl_provider_prefs like the other provider settings)
                "update_settings": _update_settings(store)}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the settings: %s" % exc)


# --------------------------------------------------------------------------- history

HISTORY_STATES = ("done", "failed", "cancelled")
HISTORY_SORTS = ("newest", "largest", "slowest", "name")
HISTORY_DEFAULT_LIMIT = 100          # a page when the caller names none
HISTORY_SCAN = 20000                 # most rows read to build the summary of a filtered set (flagged when it bites)
HISTORY_FILE_CHECKS = 200            # most rows on one page whose file is looked for (`check_files`)


def _row_time(item):
    return item.get("finished_at") or item.get("updated_at") or item.get("created_at") or 0


def _row_size(item):
    for key in ("bytes_total", "bytes_done"):
        if isinstance(item.get(key), (int, float)) and item[key] > 0:
            return int(item[key])
    return 0


def _row_speed(item):
    """Average bytes per second over the whole download, or None when the row doesn't say."""
    size = item.get("bytes_total") if isinstance(item.get("bytes_total"), (int, float)) else item.get("bytes_done")
    started, finished = item.get("started_at"), item.get("finished_at")
    if not (isinstance(size, (int, float)) and size > 0 and started and finished and finished > started):
        return None
    return size / float(finished - started)


def _sort_key(item, sort):
    """A tuple that sorts ASCENDING in the order the person asked for; the id ends every key so the order is total
    (a cursor can then say exactly where a page stopped)."""
    t, ident = -float(_row_time(item)), str(item.get("id") or "")
    if sort == "largest":
        return (-_row_size(item), t, ident)
    if sort == "slowest":
        speed = _row_speed(item)
        return (0 if speed is not None else 1, speed if speed is not None else 0.0, t, ident)
    if sort == "name":
        return (str(item.get("title") or ident).lower(), t, ident)
    return (t, ident)


def _cursor_make(sort, key):
    raw = json.dumps([sort, list(key)], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_read(token, sort):
    """The key a `before` cursor stands for, or None when it isn't one of ours or belongs to another sort."""
    try:
        text = str(token or "")
        if not text or len(text) > 600 or not re.match(r"^[A-Za-z0-9_\-]+$", text):
            return None
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8"))
        if not (isinstance(data, list) and len(data) == 2 and data[0] == sort and isinstance(data[1], list)):
            return None
        return tuple(data[1])
    except Exception:  # noqa: BLE001
        return None


def _stamp(value, name):
    """A request's date bound as epoch seconds (a number or a numeric string), or None."""
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a time in epoch seconds" % name)
    if out != out or out < 0 or out > 4102444800:
        raise ValueError("%s is out of range" % name)
    return out


def _file_exists(item):
    """True / False for a file or folder a row points at, None when it can't be told (no path, or the check itself
    failed).  It only looks; nothing is opened, read or touched."""
    path = item.get("dest_file") or item.get("dest_dir")
    if not path or not isinstance(path, str):
        return None
    try:
        return bool(os.path.exists(path))
    except (OSError, ValueError):
        return None


def history(request, store=None):
    """Finished, failed and cancelled rows for the History tab.

    Old callers that pass nothing still get `items`, `count`, `more` and `retention_days`, newest first.  A caller may also
    pass `sort` (newest, largest, slowest, name), `since` / `until` (epoch seconds, on when the row finished), `before` (the
    `next_cursor` of the page before) and `check_files`.  The answer's `summary` ({count, total_bytes, failed}) and
    `states` describe the WHOLE filtered set, not the page.  Nothing here deletes or opens a file."""
    try:
        service = _modules()[0]
        store = _store(store)
        req = request if isinstance(request, dict) else {}
        wanted = [s for s in (req.get("states") or HISTORY_STATES) if s in HISTORY_STATES] or list(HISTORY_STATES)
        limit = max(1, min(int(req.get("limit") or HISTORY_DEFAULT_LIMIT), MAX_HISTORY))
        offset = max(0, int(req.get("offset") or 0))
        sort = str(req.get("sort") or "newest")
        if sort not in HISTORY_SORTS:
            return _err("unknown sort %r (use one of: %s)" % (sort[:20], ", ".join(HISTORY_SORTS)))
        try:
            since, until = _stamp(req.get("since"), "since"), _stamp(req.get("until"), "until")
        except ValueError as exc:
            return _err(str(exc))
        if since is not None and until is not None and since > until:
            return _err("the date range ends before it starts")
        # every row of the filtered set, newest created first, read in pages of the store's own cap
        items, scanned, truncated = [], 0, False
        while True:
            chunk = store.list_items(states_in=list(HISTORY_STATES), kind=(str(req.get("kind") or "")[:20] or None),
                                     provider=(str(req.get("provider") or "")[:60] or None),
                                     search=(str(req.get("search") or "")[:80] or None), limit=2000, offset=scanned)
            items += chunk
            scanned += len(chunk)
            if len(chunk) < 2000:
                break
            if scanned >= HISTORY_SCAN:
                truncated = True
                break
        if since is not None:
            items = [i for i in items if _row_time(i) >= since]
        if until is not None:
            items = [i for i in items if _row_time(i) <= until]
        by_state = {s: 0 for s in HISTORY_STATES}
        for i in items:
            by_state[i["state"]] = by_state.get(i["state"], 0) + 1
        listed = [i for i in items if i["state"] in wanted]
        listed.sort(key=lambda i: _sort_key(i, sort))
        summary = {"count": len(listed), "total_bytes": sum(_row_size(i) for i in listed),
                   "failed": sum(1 for i in listed if i["state"] == "failed")}
        if req.get("before"):
            cursor = _cursor_read(req.get("before"), sort)
            if cursor is None:
                return _err("that page marker isn't one this list gave (or it is for another sort); load the list again")
            page = [i for i in listed if _sort_key(i, sort) > cursor][:limit + 1]
        else:
            page = listed[offset:offset + limit + 1]
        more = len(page) > limit
        page = page[:limit]
        rows = _rows(store, page)
        if req.get("check_files") is True:
            for n, (item, row) in enumerate(zip(page, rows)):
                row["file_exists"] = _file_exists(item) if n < HISTORY_FILE_CHECKS else None
        out = {"ok": True, "items": rows, "rows": rows, "count": len(rows), "more": more,
               "next_cursor": _cursor_make(sort, _sort_key(page[-1], sort)) if (more and page) else None,
               "summary": summary, "states": by_state, "sort": sort,
               "retention_days": int((store.get_settings().get("retention") or {}).get("keep_history_days") or 0)}
        if truncated:
            out["summary_truncated"] = True
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the history: %s" % exc)


def _removable(item, states):
    """Why a row may not be removed from history, or None.  Hub rows only, finished only."""
    if item["owner"] != "hub":
        return "that row belongs to %s, which would just report it again" % (item.get("provider") or "another tool")
    if item["state"] not in states.FINAL_STATES:
        return "it isn't finished (cancel it first)"
    return None


def history_remove(request, store=None):
    """Remove rows from the history.  Rows only: no file is touched, and `delete_files` is not a
    thing this function knows."""
    try:
        _, _, states, _ = _modules()
        store = _store(store)
        ids = _clean_ids((request or {}).get("ids"), MAX_IDS)
        if not ids:
            return _err("say which downloads to remove from the history")
        ok_ids, skipped = [], []
        for item_id in ids:
            item = store.get_item(item_id)
            if not item:
                skipped.append({"id": item_id, "why": "no such download"})
                continue
            why = _removable(item, states)
            if why:
                skipped.append({"id": item_id, "why": why})
            else:
                ok_ids.append(item_id)
        removed = store.delete_items(ok_ids) if ok_ids else 0
        return {"ok": True, "removed": removed, "skipped": skipped,
                "note": "Only the list entries were removed. Downloaded files were not touched."}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't remove those: %s" % exc)


def history_clear(request, store=None, now=None):
    """Remove every finished hub row older than `days`.  `dry_run: true` only counts them, so the
    confirmation can say how many."""
    try:
        _, _, states, _ = _modules()
        store = _store(store)
        req = request if isinstance(request, dict) else {}
        try:
            days = int(req.get("days"))
        except (TypeError, ValueError):
            return _err("say how many days: older than N days (1 to 3650)")
        if days < 1 or days > 3650:
            return _err("days must be between 1 and 3650")
        cutoff = (store.now() if now is None else now) - days * 86400
        old = []
        for item in store.list_items(states_in=list(HISTORY_STATES), owner="hub", limit=2000):
            when = item.get("finished_at") or item.get("updated_at") or item.get("created_at") or 0
            if when and when < cutoff and not _removable(item, states):
                old.append(item["id"])
        if req.get("dry_run") is True:
            return {"ok": True, "would_remove": len(old), "days": days}
        removed = store.delete_items(old) if old else 0
        return {"ok": True, "removed": removed, "days": days,
                "note": "Only the list entries were removed. Downloaded files were not touched."}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't clear the history: %s" % exc)


def redownload(request, store=None):
    """Queue a finished / failed / cancelled hub download again, from the link the hub kept (the
    panel only ever sees the masked link, so this has to be done here)."""
    try:
        service, _, states, _ = _modules()
        store = _store(store)
        item_id = str((request or {}).get("id") or "")
        item = store.get_item(item_id) if ITEM_ID_RE.match(item_id) else None
        if not item:
            return _err("no such download")
        if item["owner"] != "hub" or not item.get("source_url"):
            return _err("only a download the hub made itself can be downloaded again")
        if item["state"] not in states.FINAL_STATES:
            return _err("it is still in the list; use Retry or Resume")
        options = item.get("options") or {}
        sub = options.get("subdir") if isinstance(options.get("subdir"), str) else None
        out = service.add_items([item["source_url"]], engine=item.get("engine") or None, dest=sub or None, store=store)
        out["redownload_of"] = item_id
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't queue it again: %s" % exc)


def reorder(request, store=None):
    """Put queued hub downloads in the order given (the first runs first).  Only `queued`
    hub rows move; anything else in the list is refused by name."""
    try:
        store = _store(store)
        ids = _clean_ids((request or {}).get("ids"), MAX_IDS)
        if len(ids) < 2:
            return _err("drag at least two queued downloads to change their order")
        items = [store.get_item(i) for i in ids]
        bad = [i for i, it in zip(ids, items) if not it or it["owner"] != "hub" or it["state"] != "queued"]
        if bad:
            return _err("only downloads that are waiting in the hub's queue can be reordered",
                        refused=bad[:10])
        base = len(ids)
        for position, item_id in enumerate(ids):
            store.update_item(item_id, {"priority": base - position}, expect="queued", touch=False)
        return {"ok": True, "order": ids}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't reorder the queue: %s" % exc)


def dismiss(request, store=None):
    """The panel's Dismiss and Clear finished: hide one provider row (`{"id": ...}`) or every row that can be
    hidden (`{"finished": true}`).  The rules, the refusals (a hub row, a row that is still running, one that
    hasn't finished) and the answer are `service.dismiss`'s own and come back unchanged.  It hides: no row is
    deleted and no file is touched, so it needs no confirmation.  Only a well-formed id is passed on."""
    try:
        service = _modules()[0]
        store = _store(store)
        req = request if isinstance(request, dict) else {}
        item_id = str(req.get("id") or "").strip()
        if item_id and not ITEM_ID_RE.match(item_id):
            return _err("that isn't a download id")
        return service.dismiss(id=item_id or None, finished=req.get("finished") is True, store=store)
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't hide that: %s" % exc)


# --------------------------------------------------------------------------- torrents: choose the files (N24)

MAX_SOURCE = 4096
MAX_WAIT_S = 60.0


def _torrent_source(request):
    """(source text, error) from a request.  Text only; what it may hold is torrent_files.lookup's business."""
    source = (request or {}).get("source")
    if not isinstance(source, str) or not source.strip():
        return None, "give a magnet link, a torrent hash, a .torrent link or the path of a .torrent file"
    if len(source) > MAX_SOURCE or "\x00" in source:
        return None, "that is too long to be a torrent link"
    return source.strip(), None


def torrent_files(request, store=None):
    """The picker's list: one torrent's files, cleaned and bounded (MAX_LIST_PANEL), with the free space and, when the
    torrent already has a group, which files are in it.  Starts nothing.  Never raises."""
    try:
        service = _modules()[0]
        store = _store(store)
        source, problem = _torrent_source(request)
        if problem:
            return _err(problem)
        from .hub.stremio import torrent_files as tfiles
        try:
            wait = float((request or {}).get("wait_s") or 25.0)
        except (TypeError, ValueError):
            wait = 25.0
        look = tfiles.lookup(source, budget_s=max(1.0, min(wait, MAX_WAIT_S)))
        if not look.get("ok"):
            return _err(tfiles.clean_display(look.get("error") or "couldn't read that torrent", 300))
        view = tfiles.compact(look, limit=tfiles.MAX_LIST_PANEL, collapse=False)
        settings = store.get_settings()
        root = _modules()[1].library_root(settings)
        group, taken = None, {}
        for gid, info in service.torrent_groups(store).items():
            if info.get("infohash") == look.get("infohash"):
                group = gid
                for item in store.list_items(group_id=gid, limit=2000, include_dismissed=True, include_disabled=True):
                    if item["state"] not in ("failed", "cancelled"):
                        taken[service._file_index_of(item)] = item["state"]   # noqa: SLF001
        files = []
        for entry in view["files"]:
            entry = dict(entry)
            if entry["index"] in taken:
                entry["in_group"] = taken[entry["index"]]
            files.append(entry)
        real = tfiles.real_files(look)
        return {"ok": True, "name": tfiles.clean_display(look.get("name") or ""), "infohash": look.get("infohash"),
                "single": bool(look.get("single")), "count": look["count"], "size": look["total_size"],
                "files": files, "hidden": view["hidden"], "real_files": len(real),
                "needs_choice": tfiles.needs_choice(look), "max_pick": tfiles.MAX_PICK,
                "free_bytes": service._free_bytes(root), "group": group,           # noqa: SLF001
                "read_from": "server" if look.get("origin") == "server" else "file"}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read that torrent: %s" % str(exc)[:160])


def torrent_add(request, store=None):
    """The picker's Add button: queue the chosen files of one torrent as one group (`group` adds to an existing
    one: "Add more files").  The rules, the per-file refusals and the free-space check are
    `service.add_torrent_files`'s own and come back unchanged."""
    try:
        service = _modules()[0]
        store = _store(store)
        req = request if isinstance(request, dict) else {}
        source, problem = _torrent_source(req)
        if problem:
            return _err(problem)
        group = req.get("group")
        if group not in (None, ""):
            group = str(group).strip()
            if not ITEM_ID_RE.match(group):
                return _err("that isn't a group id")
        return service.add_torrent_files(source, req.get("indexes"), group_id=group or None,
                                         start_paused=req.get("start_paused") is True, store=store)
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't queue those files: %s" % str(exc)[:160])


# --------------------------------------------------------------------------- Stremio (N9, brief 12)

def _stremio_wait(store=None):
    """The `stremio.start_wait_seconds` hub setting (60 when it can't be read)."""
    try:
        value = (_store(store).get_settings().get("stremio") or {}).get("start_wait_seconds")
        return max(10, min(300, int(value)))
    except Exception:  # noqa: BLE001
        return 60


def _stremio_web():
    """stremio-web, for the card's "Web" line: its address, whether anything listens there (a bare connect, 1 s at
    most) and whether Jarvis has a folder to start it from (`web.dir`).  Reads only; never raises."""
    try:
        from .hub.stremio import config, server
        url = config.web_url()
        return {"url": url, "answers": bool(server.port_answers(url)), "configured": bool(config.web_dir())}
    except Exception:  # noqa: BLE001 - the card then says it doesn't know
        return None


def _stremio_web_config():
    """What the card's stremio-web rows show (brief 29): the saved folder and command and their defaults.  A folder the owner
    chose and a command, never a secret.  Separate from `_stremio_web()` so that block keeps its three keys."""
    try:
        from .hub.stremio import config
        cfg = config.load()
        folder = str((cfg.get("web") or {}).get("dir") or "")
        return {"url": config.web_url(cfg), "dir": folder[:300], "dir_exists": bool(config.web_dir(cfg)),
                "start_cmd": config.web_command(cfg), "default_url": config.DEFAULT_WEB_URL,
                "default_start_cmd": config.DEFAULTS["web"]["start_cmd"], "server_url": config.server_url(cfg),
                "default_server_url": config.DEFAULT_SERVER_URL,
                # addresses other than this PC (off by default) and where playback links open (browser or the Stremio app)
                "allow_remote": config.allow_remote(cfg), "open_with": config.open_with(cfg),
                "stremio_site_url": config.STREMIO_SITE_URL, "this_pc": bool(config.is_this_pc(config.web_url(cfg)))}
    except Exception:  # noqa: BLE001
        return None


def _stremio_extra():
    """The account, player and addon-list rows (brief 30).  The account key is never read here, only whether one is saved;
    the addon list is shown as hosts and positions, because a debrid key can sit in an address's path."""
    try:
        from .hub.stremio import config
        cfg = config.load()
        kind, exe = config.player_prefs(cfg)
        return {"account": config.account_state(cfg),
                "player_config": {"kind": kind, "exe": exe[:300], "exe_exists": bool(exe) and Path(exe).is_file()},
                "addon_hosts": config.addon_hosts(cfg)}
    except Exception:  # noqa: BLE001
        return {}


def _stremio_cache(snap):
    """Where Stremio's cache is, for the card's row.  Only the RUNNING server knows (its /settings `cacheRoot`), so this
    asks it (loopback, 2 s) and says plainly when it can't.  Reads only; never raises; no secret is in a server's settings
    answer and only the two keys below are kept.  Changing it is `stremio_prepare` (confirm-gated), not this module."""
    try:
        if snap.get("state") != "running":
            return {"known": False, "note": "Stremio's cache folder is read from its running server. Start the server to see it."}
        from .hub.stremio import config, server
        values = server.StremioServer(config.server_url()).values(timeout=2)
        root = str(values.get("cacheRoot") or "")
        size = values.get("cacheSize")
        out = {"known": bool(root), "root": root[:300]}
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
            out["size_bytes"] = int(size)
        if not root:
            out["note"] = "The server didn't say where its cache is."
        return out
    except Exception:  # noqa: BLE001
        return {"known": False, "note": "Couldn't ask the server where its cache is."}


def _stremio_config_block(snap):
    """The stremio.json file's own state (missing, broken, stale ...) and, when Jarvis is finding Stremio by itself and
    a working pair is known, the repair the card may offer (N22 part e).  Reads only."""
    from .hub.stremio import config, detect
    state = config.file_state()
    path = snap.get("path") or {}
    offer = bool(state["state"] != "ok" and path.get("found") and path.get("where") not in ("", "config"))
    out = {"state": state["state"], "file": state["file"], "sentence": state["sentence"], "can_repair": offer}
    if offer:
        out["repair_pair"] = {"runtime_exe": path.get("runtime_exe") or "", "server_js": path.get("server_js") or "",
                              "how": detect.HOW.get(path.get("where") or "", "")}
        out["repair_needs_copy"] = state["state"] == "broken"
    return out


def _stremio_payload(snap):
    """What the status card, Settings and the add form show.  Nothing secret: the account key is
    never read, and the paths are the ones the owner chose or Jarvis found."""
    from .hub.stremio import config, detect
    path = dict(snap.get("path") or {})
    path["how_text"] = detect.HOW.get(path.get("where") or "", "")
    out = {key: snap.get(key) for key in ("state", "title", "sentence", "next", "action", "step", "step_label",
                                           "address", "version", "managed_by", "problems", "can_start", "installed_version",
                                           "other_url")}
    out.update(ok=True, path=path, tried=snap.get("tried") or [], config_file=str(config.config_path()))
    return out


def stremio_status(request=None, store=None):
    """Read-only: the state of Stremio's server in words, its address and version, the program Jarvis
    would start and how it was found, the failed step, config problems, and a diagnostic text.
    `doctor: true` adds the whole `stack_doctor` result to the diagnostic (slower: it asks the server
    about its cache).  Starts, stops and writes nothing."""
    try:
        from .hub.stremio import status
        wait = _stremio_wait(store)
        snap = status.snapshot(wait_seconds=wait)
        out = _stremio_payload(snap)
        out["wait_seconds"] = wait          # how long Start may take (the card's own time-out follows it)
        out["web"] = _stremio_web()
        out["web_config"] = _stremio_web_config()
        out.update(_stremio_extra())
        out["cache"] = _stremio_cache(snap)
        out["config"] = _stremio_config_block(snap)
        out["diagnostic"] = status.diagnostic_text(snap)
        if (request or {}).get("doctor"):
            try:
                from .actions import stremio_stack
                doc = stremio_stack.tool_stack_doctor({})
                if doc.get("diagnostic"):
                    out["diagnostic"] = doc["diagnostic"]
            except Exception:  # noqa: BLE001 - the short diagnostic above stands
                pass
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read Stremio's status: %s" % str(exc)[:160])


def stremio_save(request, store=None):
    """The card's save button(s): the two paths (and, for 'answers on another port', the loopback address), the
    stremio-web block (`web`: url, dir, start_cmd), the server address alone, or `repair: true` (re-detect and save;
    `keep_copy: true` moves an unreadable stremio.json aside first).  All of them write ~/.jarvis/stremio.json through
    hub/stremio/config.py, which is the only validator.  The owner's own button only: this runs for a write op, so an
    unattended process is refused before it gets here, and config checks again.  Both path files must exist; a new
    server address must answer like a Stremio server.  `dry_run: true` only checks (and reports what is on the server's
    port) without writing."""
    try:
        from .hub.stremio import config, detect, status
        request = request if isinstance(request, dict) else {}
        exe, js = request.get("runtime_exe"), request.get("server_js")
        url = request.get("server_url") or None
        web = request.get("web") if isinstance(request.get("web"), dict) else None
        network = request.get("network") if isinstance(request.get("network"), dict) else None
        open_with = request.get("open_with") if isinstance(request.get("open_with"), str) else None
        player = request.get("player") if isinstance(request.get("player"), dict) else None
        addon_urls = request.get("addon_urls") if isinstance(request.get("addon_urls"), dict) else None
        if request.get("repair") is True:
            result = config.repair(keep_copy=request.get("keep_copy") is True)
        elif not (exe or js) and (url or web is not None or player is not None or addon_urls is not None
                                  or network is not None or open_with is not None):
            if request.get("dry_run") is True:
                remote_ok = network.get("allow_remote") if network is not None and isinstance(network.get("allow_remote"), bool) else None
                clean, found = config.check_web(web, remote_ok) if web is not None else ({}, [])
                if network is not None:
                    found = found + config.check_network(network)[1]
                if open_with is not None:
                    found = found + config.check_open(open_with)[1]
                if player is not None:
                    found = found + config.check_player(player)[1]
                return {"ok": not found, "dry_run": True, "problems": found, "web": clean,
                        **({} if not found else {"error": "; ".join(found)})}
            result = config.save_settings(server_url=url, web=web, player=player, addon_urls=addon_urls,
                                          network=network, open_with=open_with)
        elif request.get("dry_run") is True:
            checked = config.check_paths(exe, js)
            probe = detect.probe_port(config.server_url(), other_ports=(detect.DEFAULT_PORT,))
            return {"ok": checked["ok"], "dry_run": True, "problems": checked["problems"], "server": probe,
                    **({} if checked["ok"] else {"error": "; ".join(checked["problems"])})}
        else:
            result = config.save_paths(exe, js, server_url=url)
        if not result.get("ok"):
            return _err(result.get("error") or "couldn't save", **{k: v for k, v in result.items() if k in ("code", "problems")})
        out = stremio_status({}, store)
        out["saved"] = result.get("saved") or {}
        for key in ("kept_copy", "repaired"):
            if result.get(key):
                out[key] = result[key]
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't save the Stremio settings: %s" % str(exc)[:160])


def stremio_account(request, store=None):
    """The card's account buttons (brief 30).  `action`: "login" (email + password, signs in to Stremio's cloud and saves the
    key it returns), "key" (paste a login key), "check" (ask Stremio whose key it is), "logout" (tell Stremio, then forget the
    key).  The owner's own button only: a write op, so an unattended process is refused before it gets here and config checks
    again.  The password is used for one call and never stored or echoed; the key is never returned."""
    try:
        from .hub.stremio import addons, config
        request = request if isinstance(request, dict) else {}
        action = str(request.get("action") or "")
        refused = config._owner_only()
        if refused:
            return _err(refused["error"], code=refused.get("code"))
        net = addons.Net()
        note = ""
        if action == "login":
            key, email = addons.login(net, request.get("email"), request.get("password"))
            result = config.save_account(key, email)
            note = "Signed in as %s." % email
        elif action == "key":
            result = config.save_account(request.get("auth_key"), "")
            note = "Login key saved. Press Check account to see whose it is."
        elif action == "check":
            if not config.account_state()["signed_in"]:
                return _err("no Stremio account is saved yet")
            email = addons.account_email(net)
            result = config.save_account(addons.auth_key(), email) if email else {"ok": True}
            note = ("The saved key belongs to %s." % email) if email else "Stremio accepted the saved key."
        elif action == "logout":
            if config.account_state()["signed_in"]:
                addons.remote_logout(net)
            result = config.clear_account()
            note = "Signed out. The login key was removed from stremio.json."
        else:
            return _err("unknown account action")
        if not result.get("ok"):
            return _err(result.get("error") or "couldn't save the account")
        out = stremio_status({}, store)
        out["account_note"] = note
        return out
    except Exception as exc:  # noqa: BLE001 - AddonError text already has the password and key removed
        return _err("couldn't update the Stremio account: %s" % str(exc)[:200])


def stremio_start(request=None, store=None):
    """The card's Start buttons.  Without a body: bring Stremio's SERVER up, nothing else.  With `what: "web"` (that literal
    and nothing else is read; brief 29): start stremio-web from the folder and command in stremio.json.  Both are
    `stremio_stack.tool_stack_start`, so every rule is that function's: loopback only; the already-up case starts nothing;
    and when the Stremio app is open with a silent server it says so and starts nothing (N9 decision 4).  The server start
    may wait up to `stremio.start_wait_seconds`, so the web route gives it a long time-out and the page shows a working state
    and times out on its own.  The answer is the sentence the status uses; `ok` is false when it is not up afterwards.  A
    write op: an unattended process is refused."""
    try:
        from .actions import stremio_stack
        what = "web" if isinstance(request, dict) and request.get("what") == "web" else "server"
        raw = stremio_stack.tool_stack_start({"what": what})
        raw = raw if isinstance(raw, dict) else {}
        if what == "web":
            web = raw.get("web") if isinstance(raw.get("web"), dict) else {}
            state = str(web.get("state") or "")
            out = {"state": state, "url": web.get("url") or ""}
            if state == "already_up":
                out["sentence"] = "stremio-web is already running at %s." % out["url"]
            elif state == "started":
                out["sentence"] = "stremio-web is running at %s." % out["url"]
            elif state == "starting":
                out["sentence"] = "stremio-web was started and hasn't answered yet at %s." % out["url"]
            if web.get("note"):
                out["note"] = str(web["note"])[:300]
            if raw.get("ok") and state in ("already_up", "started", "starting"):
                out["ok"] = True
                return out
            out["ok"] = False
            out["error"] = str(raw.get("error") or web.get("error") or web.get("note") or "stremio-web did not start")[:400]
            return out
        server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
        out = {"state": server.get("state") or "", "outcome": server.get("outcome") or "",
               "sentence": server.get("sentence") or "", "url": server.get("url") or ""}
        for key in ("version", "note", "next"):
            if server.get(key):
                out[key] = server[key]
        if raw.get("config_problems"):
            out["config_problems"] = raw["config_problems"]
        if raw.get("ok"):
            out["ok"] = True
            return out
        out["ok"] = False
        out["error"] = str(raw.get("error") or server.get("error") or "Stremio's server did not start")[:400]
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't start Stremio: %s" % str(exc)[:160])


# --------------------------------------------------------------------------- qBittorrent's connection

def qbittorrent_status(request=None, store=None):
    """The connection form, read: the switch, the Web UI address and the user name as the file holds them, whether a
    password is saved (never the password), which behaviour keys the file also holds (they win over the settings rows),
    what is wrong with the file, and the engine's own answer in the words the Sources tab uses.  Writes nothing."""
    try:
        from .hub.engines import qbittorrent as qbe
        out = qbe.connection_state()
        out["file"] = _plain(out.get("file"), 300)
        out["problem"] = _plain(out["problem"], 300) if out.get("problem") else None
        ok, reason = qbe.ENGINE.available()
        out["engine"] = {"ok": bool(ok), "reason": _plain(reason, 300)}
        out["ok"] = True
        return out
    except Exception as exc:  # noqa: BLE001 - a status read must never take the page down
        return _err("couldn't read the qBittorrent connection: %s" % _plain(exc, 160))


def qbittorrent_save(request, store=None):
    """The connection form's save.  OWNER ONLY: `admin` refuses an unattended process before this runs.  Writes only the
    keys sent, through the engine's own validator (`save_connection`); a refusal names the field and writes nothing.
    The answer is the status again, plus `saved` (the key names).  A password is written here and never sent back."""
    try:
        from .hub.engines import qbittorrent as qbe
        result = qbe.save_connection(request if isinstance(request, dict) else {})
        if not result.get("ok"):
            return _err(result.get("error") or "couldn't save the qBittorrent connection", problems=result.get("problems") or [])
        out = qbittorrent_status({}, store)
        out["saved"] = result.get("saved") or []
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't save the qBittorrent connection: %s" % _plain(exc, 160))


# --------------------------------------------------------------------------- the Sources tab (brief 24)

# What each engine is for, in a short phrase.  An engine that is not here shows only its label.
ENGINE_HANDLES = {
    "http": "direct links and files",
    "ytdlp": "YouTube and other video sites",
    "stremio": "magnets, torrent files and Stremio streams",
    "qbittorrent": "magnets, when you ask for it",
    "hosters": "file-host links (MediaFire, Google Drive, Buzzheavier ...)",
}
# The file an engine is set up in, when it has no settings page.  A path in words, never its contents.
ENGINE_CONFIG_FILES = {"qbittorrent": "~/.jarvis/qbittorrent.json"}
# A provider the hub knows about that only registers when it is first used.  Playnite registers on its first install on
# purpose (hub/playnite_provider.py), so before that it is not a provider row and the page would show nothing at all.
KNOWN_PROVIDER_HINTS = {
    "playnite": ("Playnite", "Appears here after the first install Jarvis starts through Playnite."),
}
SOURCES_CHECK_SECONDS = 8.0            # one engine's own check may not hold the page up longer than this

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s)\]'\"]*[^\s)\]'\".,;:!?]")
_HOSTPORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b|\blocalhost(?::\d+)?\b", re.IGNORECASE)
_SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|token|api[_-]?key|secret|cookie|auth)\b(\s*[=:]\s*)\S+")


def _plain(text, limit=300):
    """A sentence from an engine or a provider, safe to show: no address (an addon or server URL, or host:port), and
    nothing after `password=` / `token:` and the like.  The Sources tab is a status page, not a settings dump."""
    out = str(text or "")
    try:                                              # the folder the person's home is in is not for a status page
        home = str(Path.home())
        for form in {home, home.replace("\\", "/")}:
            if len(form) > 3:
                out = out.replace(form, "~")
    except Exception:  # noqa: BLE001
        pass
    out = _URL_RE.sub("its address", out)
    out = _HOSTPORT_RE.sub("its address", out)
    out = _SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "(hidden)", out)
    out = " ".join(out.split())
    return out[:limit]


def _engine_state(ok, reason):
    """ready | needs_setup | not_running | off, from an engine's own (ok, reason)."""
    if ok:
        return "ready"
    low = str(reason or "").lower()
    if "switched off" in low or "turned off" in low or "disabled" in low:
        return "off"
    for word in ("isn't running", "is not running", "not running", "can't reach", "cannot reach", "couldn't reach",
                 "isn't answering", "didn't answer", "connection refused", "no answer"):
        if word in low:
            return "not_running"
    return "needs_setup"


def _probe_engine(engine):
    """(ok, reason, checks) for one engine.  Raises if the engine's own check does; the caller says Broken."""
    ok, reason = engine.available()
    checks = []
    doctor = getattr(engine, "doctor", None)
    if callable(doctor):
        for line in (doctor() or [])[:8]:
            if isinstance(line, dict):
                checks.append({"name": _plain(line.get("name"), 60), "status": _plain(line.get("status"), 12) or "info",
                               "detail": _plain(line.get("detail"), 300)})
    return bool(ok), str(reason or ""), checks


def sources(request=None, store=None):
    """Read-only, for the Sources tab: every download engine (ready, needs setup, not running, off or broken, with
    the one-sentence reason, what it is for, how many it has running and waiting, its own check lines and where to
    configure it) and every program that reports its own downloads (what `dl_providers` says, plus a hint for one the
    hub knows but that has not registered yet).  Starts nothing and changes nothing; an engine whose check raises is
    shown Broken, never a crash.  No key, password or address is in the answer."""
    try:
        import concurrent.futures
        from .hub import engines, service, settings as settings_mod, states
        st = _store(store)
        engines.load()
        counts = st.counts().get("by_engine") or {}
        waiting = {}
        for row in st.list_items(owner="hub", states_in=("queued",), limit=2000):
            waiting[row.get("engine") or "?"] = waiting.get(row.get("engine") or "?", 0) + 1
        writable = settings_mod.PREFS_WRITABLE

        names = sorted(engines.ENGINES)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(names) or 1)))
        futures = {name: pool.submit(_probe_engine, engines.ENGINES[name]) for name in names}
        out_engines = []
        deadline = time.monotonic() + SOURCES_CHECK_SECONDS
        for name in names:
            engine = engines.ENGINES[name]
            ok, reason, checks, state = False, "", [], "broken"
            try:
                ok, reason, checks = futures[name].result(timeout=max(0.1, deadline - time.monotonic()))
                state = _engine_state(ok, reason)
                if not ok and str(reason).startswith("check failed"):
                    state = "broken"
            except concurrent.futures.TimeoutError:
                reason = "its check did not finish in %d seconds" % int(SOURCES_CHECK_SECONDS)
            except Exception as exc:  # noqa: BLE001 - an engine's check must never take the page down
                reason = "its check failed: %s" % type(exc).__name__ + (": %s" % exc if str(exc) else "")
            by = counts.get(name) or {}
            has_settings = any(k == "concurrency." + name or k.startswith(name + ".") for k in writable)
            out_engines.append({
                "id": name, "label": getattr(engine, "label", "") or name, "state": state, "reason": _plain(reason),
                "handles": ENGINE_HANDLES.get(name, ""), "active": int(by.get("active") or 0), "queued": int(waiting.get(name, 0)),
                "checks": checks, "configure": name if has_settings else None,
                "config_file": ENGINE_CONFIG_FILES.get(name),
            })
        pool.shutdown(wait=False)
        for name, why in sorted(engines.LOAD_ERRORS.items()):
            out_engines.append({"id": name, "label": name, "state": "broken", "reason": _plain("didn't load: %s" % why),
                                "handles": "", "active": 0, "queued": 0, "checks": [], "configure": None, "config_file": None})

        provided = service.list_providers(st)
        if provided.get("ok") is False:
            return _err(provided.get("error") or "couldn't read the providers")
        out_providers = []
        seen = set()
        try:
            update_rows = st.update_check_state()
        except Exception:  # noqa: BLE001 - the Sources tab opens without it
            update_rows = {}
        for p in provided.get("providers") or []:
            seen.add(p.get("id"))
            row = dict(p)
            row["registered"] = True
            # The Updates rework: a provider that only reports / applies updates is not a download source; the panel
            # can hide it here (the same flag `updates` carries per provider).
            row["updates_only"] = bool((update_rows.get(p.get("id")) or {}).get("updates_only"))
            row["last_error"] = _plain(row.get("last_error")) or None
            out_providers.append(row)
        for pid, (label, hint) in sorted(KNOWN_PROVIDER_HINTS.items()):
            if pid not in seen:
                out_providers.append({"id": pid, "label": label, "registered": False, "hint": hint})
        return {"ok": True, "engines": out_engines, "providers": out_providers}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the sources: %s" % _plain(exc, 160))


# --------------------------------------------------------------------------- the entry point

def admin(op, request=None, store=None):
    """One request, one JSON-able answer.  Never raises."""
    request = request if isinstance(request, dict) else {}
    if op not in OPS:
        return _err("usage: jarvis hub-admin %s (one JSON object on stdin)" % "|".join(OPS + ("watch",)))
    if op in WRITE_OPS:
        why = unattended_reason()
        if why:
            return _err(why, code="unattended")
    if op in PICKER_OPS and unattended_reason():
        return _err("The folder picker can only be used from the Jarvis page itself.", code="unattended")
    if op in TORRENT_OPS and unattended_reason():
        return _err("The torrent file picker can only be used from the Jarvis page itself.", code="unattended")
    try:
        if op == "card":
            return card_payload(ids=request.get("ids"), group=request.get("group"),
                                query=request.get("query"), store=store)
        if op == "list":
            return snapshot(store, request.get("limit") or 500, include_dismissed=request.get("include_dismissed") is True)
        if op == "settings":
            return settings_payload(store)
        if op == "history":
            return history(request, store)
        if op == "file":
            return resolve_file(request.get("id"), store)
        if op == "thumb":
            return resolve_thumb(request.get("id"), store)
        if op == "history_remove":
            return history_remove(request, store)
        if op == "history_clear":
            return history_clear(request, store)
        if op == "redownload":
            return redownload(request, store)
        if op == "reorder":
            return reorder(request, store)
        if op == "dismiss":
            return dismiss(request, store)
        if op == "stremio_status":
            return stremio_status(request, store)
        if op == "sources":
            return sources(request, store)
        if op == "stremio_save":
            return stremio_save(request, store)
        if op == "stremio_start":
            return stremio_start(request, store)
        if op == "stremio_account":
            return stremio_account(request, store)
        if op == "qbittorrent_status":
            return qbittorrent_status(request, store)
        if op == "qbittorrent_save":
            return qbittorrent_save(request, store)
        if op == "torrent_files":
            return torrent_files(request, store)
        if op == "torrent_add":
            return torrent_add(request, store)
        if op in ("updates", "updates_check", "updates_dismiss", "updates_apply", "updates_dry_run", "updates_set"):
            from .hub import updates_admin
            return getattr(updates_admin, op)(request, store=store)
        if op == "browse":
            from . import hub_browse
            return hub_browse.browse(request, store=store)
        if op == "mkdir":
            from . import hub_browse
            return hub_browse.make_folder(request)
    except Exception as exc:  # noqa: BLE001 - a panel call must answer, not crash the page
        return _err("Download Hub error: %s" % str(exc)[:160])
    return _err("unknown op")


def run_cli(argv, stdin=None, stdout=None):
    """`jarvis hub-admin <op>`: read one JSON object from stdin, print one JSON object.  `watch`
    prints one JSON object per line, flushing each, until its stdout closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    op = argv[0] if argv else ""
    if op == "watch":
        try:
            for message in watch_events():
                stdout.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
                stdout.flush()
        except (BrokenPipeError, KeyboardInterrupt, OSError):
            pass
        return 0
    request = {}
    if op in OPS:
        try:
            raw = stdin.read()
            request = json.loads(raw) if raw.strip() else {}
        except (ValueError, OSError):
            request = None
        if not isinstance(request, dict):
            stdout.write(json.dumps(_err("The request must be one JSON object on stdin.")) + "\n")
            return 0
    stdout.write(json.dumps(admin(op, request), ensure_ascii=False, default=str) + "\n")
    return 0
