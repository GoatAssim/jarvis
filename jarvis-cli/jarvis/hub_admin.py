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
  * hiding a finished or lost provider row (`dismiss`; brief 19).  It hides, it deletes no row and no file.

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
READ_OPS = ("card", "list", "settings", "history", "file", "thumb", "stremio_status", "sources")
# `dismiss` (brief 19) hides a provider row that is unknown or finished; `stremio_start` (brief 19) is the Start button.
WRITE_OPS = ("history_remove", "history_clear", "redownload", "reorder", "stremio_save", "dismiss", "stremio_start")
OPS = READ_OPS + WRITE_OPS
# N14 (the path picker, jarvis/hub_browse.py): list one folder, and make one new folder.  Neither reads a file's
# contents.  Both are for the Jarvis page's own picker, so both refuse an unattended process (below).
PICKER_OPS = ("browse", "mkdir")
OPS = OPS + PICKER_OPS

MAX_CARD_IDS = 8
MAX_IDS = 200
MAX_HISTORY = 1000
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
    return row


def _rows(store, items):
    service = _modules()[0]
    providers = service._provider_map(store)   # noqa: SLF001
    now = store.now()
    return [present_row(i, providers, now) for i in items]


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
        return {"ok": True, "settings": settings, "view": view,
                "writable": sorted(settings_mod.WRITABLE), "prefs_writable": sorted(settings_mod.PREFS_WRITABLE),
                "events": list(settings_mod.EVENTS), "open_modes": list(settings_mod.OPEN_MODES),
                "providers": service.list_providers(store).get("providers", [])}
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't read the settings: %s" % exc)


# --------------------------------------------------------------------------- history

HISTORY_STATES = ("done", "failed", "cancelled")


def history(request, store=None):
    """Finished, failed and cancelled rows, newest first, for the History tab."""
    try:
        service = _modules()[0]
        store = _store(store)
        req = request if isinstance(request, dict) else {}
        wanted = [s for s in (req.get("states") or HISTORY_STATES) if s in HISTORY_STATES] or list(HISTORY_STATES)
        limit = max(1, min(int(req.get("limit") or 300), MAX_HISTORY))
        offset = max(0, int(req.get("offset") or 0))
        items = store.list_items(states_in=wanted, kind=(str(req.get("kind") or "")[:20] or None),
                                 provider=(str(req.get("provider") or "")[:60] or None),
                                 search=(str(req.get("search") or "")[:80] or None),
                                 limit=limit + 1, offset=offset)
        items.sort(key=lambda i: (i.get("finished_at") or i.get("updated_at") or 0), reverse=True)
        more = len(items) > limit
        rows = _rows(store, items[:limit])
        return {"ok": True, "items": rows, "count": len(rows), "more": more, "retention_days":
                int((store.get_settings().get("retention") or {}).get("keep_history_days") or 0)}
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
    """The Locate... button: save the two paths (and, for 'answers on another port', the loopback
    address) into ~/.jarvis/stremio.json.  The owner's own button only: this runs for a write op, so an
    unattended process is refused before it gets here, and `config.save_paths` checks again.  Both
    files must exist; a new address must answer like a Stremio server.  `dry_run: true` only checks
    (and reports what is on the server's port) without writing."""
    try:
        from .hub.stremio import config, detect, status
        request = request if isinstance(request, dict) else {}
        exe, js = request.get("runtime_exe"), request.get("server_js")
        url = request.get("server_url") or None
        if request.get("dry_run") is True:
            checked = config.check_paths(exe, js)
            probe = detect.probe_port(config.server_url(), other_ports=(detect.DEFAULT_PORT,))
            return {"ok": checked["ok"], "dry_run": True, "problems": checked["problems"], "server": probe,
                    **({} if checked["ok"] else {"error": "; ".join(checked["problems"])})}
        result = config.save_paths(exe, js, server_url=url)
        if not result.get("ok"):
            return _err(result.get("error") or "couldn't save", **{k: v for k, v in result.items() if k in ("code", "problems")})
        out = stremio_status({}, store)
        out["saved"] = result["saved"]
        return out
    except Exception as exc:  # noqa: BLE001
        return _err("couldn't save the Stremio paths: %s" % str(exc)[:160])


def stremio_start(request=None, store=None):
    """The card's Start button: bring Stremio's SERVER up, nothing else (not stremio-web).  It is
    `stremio_stack.tool_stack_start` with what="server", so every rule is that function's: loopback only; the
    already-up case starts nothing; and when the Stremio app is open with a silent server it says so and starts
    nothing (N9 decision 4).  It may wait up to `stremio.start_wait_seconds`, so the web route gives it a long
    time-out and the page shows a working state and times out on its own.  The answer is the sentence the status
    uses; `ok` is false when the server is not up afterwards.  A write op: an unattended process is refused."""
    try:
        from .actions import stremio_stack
        raw = stremio_stack.tool_stack_start({"what": "server"})
        raw = raw if isinstance(raw, dict) else {}
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
        return _err("couldn't start Stremio's server: %s" % str(exc)[:160])


# --------------------------------------------------------------------------- the Sources tab (brief 24)

# What each engine is for, in a short phrase.  An engine that is not here shows only its label.
ENGINE_HANDLES = {
    "http": "direct links and files",
    "ytdlp": "YouTube and other video sites",
    "stremio": "magnets, torrent files and Stremio streams",
    "qbittorrent": "magnets, when you ask for it",
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
        for p in provided.get("providers") or []:
            seen.add(p.get("id"))
            row = dict(p)
            row["registered"] = True
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
