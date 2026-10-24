"""Downloads made with the Jarvis browser go to the Download Hub (N22 part a).

Before this, a click on a download link in the Jarvis browser (Playwright, `browser_tools.py`) ended in
Playwright's own temporary file, deleted when the browser closed, and the hub never heard of it.  Now:

  * every page of the browser's context is watched for a `download` (see `attach`; pop-ups and new tabs
    are pages too, and Chromium reports a pop-up's download on its opener, so both are covered);
  * the file is saved with `download.save_as()` into the hub's library folder, named by `hub/naming.py`
    (the same rules as every hub download: a clean name, the kind's folder or an extension folder, never
    overwriting), through a `.part` file that becomes the real name only when it is whole;
  * the download is a row in Downloads and History, written through `hub_client` in PUSH mode under the
    provider id `browser` ("Browser downloads").  The browser owns the transfer and Playwright reports no
    progress, so the row says "downloading" with no percent until the file lands, then done (with its
    path, Open and Show in folder) or failed;
  * these rows have NO Pause, Resume or Cancel: the provider declares no such capability and no control
    tool (the same rule as Steam rows, decision S2).  The hub does not simulate a control it does not have;
  * the browser tool's result says the file was handed to the hub (`annotate`), so "did it download?" can be
    answered from `dl_status`.

What this does NOT do, on purpose:

  * No cookie is read, copied or passed anywhere.  The browser stays the only thing that holds the login;
    the HTTP and yt-dlp engines keep dropping cookies.  (`tests/test_hub_browser_intake.py` greps this file.)
  * No new way to start a download.  The confirmation on `browser_click` / `browser_fill` is unchanged.
  * The page's address is never stored: a signed link can carry a token in its query.  The row keeps the
    host name only.

How a save progresses (measured on Playwright 1.56 with its headless Chromium): the sync API delivers events
and finishes `save_as` only while a Playwright call is running.  So the browser tools do not return the
moment the click is done: `annotate` keeps calling `page.wait_for_timeout` (`settle`) until every save has
landed, up to SETTLE_MAX_S, and says so when it stops earlier.  Any later browser tool call lets a save that
is still running continue.  A save the browser closes on is a failed row, never a row left "downloading".
"""

import atexit
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import hub_client
from . import naming
from . import settings as hub_settings

PROVIDER_ID = "browser"
LABEL = "Browser downloads"
SETTLE_GRACE_MS = 500        # after a click: how long to wait for a download to START (the event comes with the headers)
SETTLE_MAX_S = 600.0         # how long one browser tool call may wait for saves that are running
SETTLE_POLL_MS = 200
DRAIN_AT_EXIT_S = 30.0       # when the process ends with a save running, wait this long before the browser closes
MAX_NOTES = 20
REPLACE_TRIES = 6            # putting the finished file in place can fail for a moment (a virus scanner holds it on Windows)
REPLACE_WAIT_S = 0.5

_lock = threading.RLock()
_saving = {}                 # local id -> {"item", "title", "part", "started", "done"}
_notes = []                  # outcomes the model has not been told about yet
_contexts = set()            # id() of contexts already watched
_pages = set()               # id() of pages already watched
_last_page = [None]
_exit_hook = [False]


# --------------------------------------------------------------------------- the hub side

def _settings():
    """The hub's settings, or the defaults when the store can't be read (a download must not be lost
    because the hub's database is busy)."""
    try:
        return hub_client._store().get_settings()
    except Exception:  # noqa: BLE001
        return hub_settings.merged({})


def _roots(settings):
    try:
        return [str(r) for r in hub_settings.write_roots(settings)]
    except Exception:  # noqa: BLE001
        return [str(hub_settings.library_root(settings))]


def register(settings=None):
    """Tell the hub this provider exists (push mode, no controls, Open and Show in folder only).
    Refreshed on every call, so a moved library root is followed.  Never raises."""
    try:
        settings = settings if settings is not None else _settings()
        return hub_client.register_provider(
            PROVIDER_ID, label=LABEL, mode="push", kind="file", icon="B",
            capabilities=["open", "reveal"], controls=None, open_roots=_roots(settings)[:8],
            source_file="hub/browser_intake.py")
    except Exception:  # noqa: BLE001
        return False


def _host(url):
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host[:80]


def _reserve(folder, name):
    """Pick a name nothing else uses and claim it with an empty `.part` file, so two downloads of the
    same name started together never share one.  -> (final path, part path)."""
    with _lock:
        for _ in range(50):
            target = naming.unique_path(Path(folder) / name)
            part = Path(str(target) + ".part")
            try:
                with open(part, "xb"):
                    pass
                return target, part
            except FileExistsError:
                continue
        raise OSError("could not reserve a file name in %s" % folder)


def _remember(note):
    with _lock:
        _notes.append(note)
        del _notes[:-MAX_NOTES]


def _finish_failed(local_id, entry, error):
    """The one place a saving row turns failed (a second caller finds it already settled)."""
    with _lock:
        if entry.get("done"):
            return
        entry["done"] = True
        _saving.pop(local_id, None)
    try:
        if entry.get("part"):
            Path(entry["part"]).unlink()
    except OSError:
        pass
    try:
        entry["item"].fail(error)
    except Exception:  # noqa: BLE001
        pass
    _remember({"id": "%s:%s" % (PROVIDER_ID, local_id), "title": entry.get("title", ""), "state": "failed",
               "error": str(error)[:200]})


def _on_download(download):
    """One download event.  Runs inside Playwright's event dispatch and blocks until the file is saved
    (the save only moves while the browser tool is pumping, see the module docstring).  Never raises."""
    local_id = "b" + uuid.uuid4().hex[:10]
    entry = {"item": None, "title": "download", "part": None, "started": time.monotonic(), "done": False}
    try:
        try:
            suggested = download.suggested_filename
        except Exception:  # noqa: BLE001
            suggested = ""
        name = naming.sanitize_filename(suggested, default="download")
        entry["title"] = name
        kind = naming.kind_for(name)
        settings = _settings()
        register(settings)
        host = _host(getattr(download, "url", ""))
        item = hub_client.begin(
            PROVIDER_ID, local_id, title=name, kind=kind, state="downloading",
            subtitle=("from " + host) if host else None,
            message="the browser is downloading it; Jarvis saves it into the library when it lands")
        entry["item"] = item
        item.progress(percent=None)
        with _lock:
            _saving[local_id] = entry

        folder, _source = hub_settings.resolve_dest_dir(settings, name, kind)
        if folder is None or not naming.under_any(hub_settings.write_roots(settings), folder):
            _finish_failed(local_id, entry, "no safe folder to save it in (check the library folder in Downloads settings)")
            return
        Path(folder).mkdir(parents=True, exist_ok=True)
        target, part = _reserve(folder, name)
        entry["part"] = str(part)
        try:
            download.save_as(str(part))
        except Exception as exc:  # noqa: BLE001 - a failed or cancelled transfer, or the browser closing
            _finish_failed(local_id, entry, "the browser could not finish the download: %s" % _first_line(exc))
            return
        if entry.get("done"):                       # the browser closed while it was saving
            return
        try:
            size = os.path.getsize(part)
            for attempt in range(REPLACE_TRIES):
                if target.exists():                # someone took the name while we were saving
                    target = naming.unique_path(target, ignore_part=True)
                try:
                    os.replace(part, target)
                    break
                except OSError:
                    if attempt == REPLACE_TRIES - 1:
                        raise
                    time.sleep(REPLACE_WAIT_S)
        except OSError as exc:
            # The download itself is whole: never delete it.  The row fails and says where the file is.
            kept = entry.pop("part", None)
            _finish_failed(local_id, entry, "the file is downloaded but could not be renamed (%s); it is kept as %s"
                           % (_first_line(exc), kept))
            return
        with _lock:
            if entry.get("done"):
                return
            entry["done"] = True
            _saving.pop(local_id, None)
        item.progress(bytes_done=size, bytes_total=size, percent=100.0)
        item.done(path=target)
        _remember({"id": item.id, "title": name, "state": "done", "path": str(target), "bytes": size})
    except Exception as exc:  # noqa: BLE001 - nothing in here may break the browser
        try:
            if entry.get("item") is not None:
                _finish_failed(local_id, entry, "browser download failed: %s" % _first_line(exc))
        except Exception:  # noqa: BLE001
            pass


def _first_line(exc):
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:160]


# --------------------------------------------------------------------------- watching the browser

def _watch_page(page):
    try:
        if id(page) in _pages:
            return
        _pages.add(id(page))
        _last_page[0] = page
        page.on("download", _on_download)
    except Exception:  # noqa: BLE001
        pass


def _on_context_closed(*_args):
    """The browser closed: a save still running can't finish.  Fail those rows now, so nothing is left
    "downloading" for ever."""
    with _lock:
        running = list(_saving.items())
    for local_id, entry in running:
        _finish_failed(local_id, entry, "the browser was closed before the file was saved")
    _last_page[0] = None


def attach(context):
    """Watch every page of this browser context (the ones open now and every later one, pop-ups and
    new tabs included).  Playwright has no context-level `download` event (`context.on("download")` is
    accepted and never fires: measured on 1.56), so each page is subscribed as it appears.
    Idempotent; never raises; returns True when the context is being watched."""
    try:
        if id(context) in _contexts:
            return True
        _contexts.add(id(context))
        for page in list(getattr(context, "pages", None) or []):
            _watch_page(page)
        context.on("page", _watch_page)
        context.on("close", _on_context_closed)
        if not _exit_hook[0]:
            _exit_hook[0] = True
            atexit.register(_drain_at_exit)   # runs BEFORE browser_tools' own atexit (last in, first out)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- waiting, and telling the model

def saving_now():
    with _lock:
        return [{"id": "%s:%s" % (PROVIDER_ID, k), "title": v["title"],
                 "seconds": int(time.monotonic() - v["started"])} for k, v in _saving.items()]


def _pump(page, ms):
    """Let Playwright run for `ms` (events and saves only move while it does).  False when it can't."""
    for candidate in (page, _last_page[0]):
        if candidate is None:
            continue
        try:
            candidate.wait_for_timeout(ms)
            return True
        except Exception:  # noqa: BLE001 - that page is gone; try the last one we saw
            continue
    return False


def settle(page=None, max_s=None):
    """Keep the browser running until every save that has started has landed, or `max_s` ran out.
    Returns True when nothing is still saving."""
    limit = SETTLE_MAX_S if max_s is None else float(max_s)
    started = time.monotonic()
    while _saving_count():
        if time.monotonic() - started >= limit:
            return False
        if not _pump(page, SETTLE_POLL_MS):
            return False
        _tick_messages()
    return True


def _saving_count():
    with _lock:
        return len(_saving)


def _tick_messages():
    with _lock:
        running = list(_saving.values())
    for entry in running:
        try:
            seconds = int(time.monotonic() - entry["started"])
            entry["item"].progress(message="the browser is downloading it (%d s); Jarvis saves it when it lands" % seconds)
        except Exception:  # noqa: BLE001
            pass


def take_notes():
    with _lock:
        out = list(_notes)
        del _notes[:]
    return out


def _drain_at_exit():
    try:
        if _saving_count():
            settle(None, DRAIN_AT_EXIT_S)
    except Exception:  # noqa: BLE001
        pass


def sentence(notes, still):
    """What the model is told.  Plain, short, and honest about what is and is not done."""
    parts = []
    for note in notes:
        if note.get("state") == "done":
            parts.append("The file '%s' was handed to the Download Hub and saved as %s (id %s; dl_status shows it)."
                         % (note.get("title"), note.get("path"), note.get("id")))
        else:
            parts.append("A browser download '%s' failed: %s (id %s; it is a failed row in the Download Hub)."
                         % (note.get("title"), note.get("error"), note.get("id")))
    for run in still:
        parts.append("The file '%s' is still being downloaded by the browser (id %s, %d s so far); it goes to the Download Hub "
                     "when it lands. Any further browser tool call lets it continue; closing the browser first would lose it."
                     % (run["title"], run["id"], run["seconds"]))
    if parts:
        parts.append("Browser downloads have no pause or cancel.")
    return " ".join(parts)


def annotate(result, page=None, grace_ms=0, max_s=None):
    """Add what became of any download to a browser tool's result.

    `grace_ms`: wait that long first for a download to start (a click).  Then wait for every running
    save (`settle`).  A result with nothing to say comes back untouched.  A `goto` that Chromium refused
    with "Download is starting" is turned into a success, because that is exactly what happened.
    Never raises."""
    try:
        if page is not None:
            _last_page[0] = page
        if not grace_ms and isinstance(result, dict) and "Download is starting" in str(result.get("error") or ""):
            grace_ms = SETTLE_GRACE_MS      # Chromium refused the navigation: the download event is right behind it
        if grace_ms:
            _pump(page, grace_ms)
        settle(page, max_s)
        notes, still = take_notes(), saving_now()
        if not notes and not still:
            return result
        if not isinstance(result, dict):
            return result
        out = dict(result)
        if not out.get("ok") and "Download is starting" in str(out.get("error") or ""):
            out.pop("error", None)
            out["ok"] = True
            try:
                out["url"] = page.url
            except Exception:  # noqa: BLE001
                pass
        out["downloads"] = [dict(n) for n in notes] + [dict(r, state="downloading") for r in still]
        out["download_note"] = sentence(notes, still)
        return out
    except Exception:  # noqa: BLE001
        return result
