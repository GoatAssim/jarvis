"""Downloads made with the Jarvis browser go to the Download Hub (N22 part a; hardened by brief 6E).

Before N22a, a click on a download link in the Jarvis browser (Playwright, `browser_tools.py`) ended in
Playwright's own temporary file, deleted when the browser closed, and the hub never heard of it.  Now:

  * every page of the browser's context is watched for a `download` (see `attach`; pop-ups and new tabs
    are pages too, and Chromium reports a pop-up's download on its opener, so both are covered);
  * a download is saved only when it was ASKED FOR (brief 6E, problem 3, see "Asked for" below);
  * the file is saved with `download.save_as()` into the hub's library folder, named by `hub/naming.py`
    (the same rules as every hub download: a clean name, the kind's folder or an extension folder, never
    overwriting), through a `.part` file that becomes the real name only when it is whole;
  * the download is a row in Downloads and History, written through `hub_client` in PUSH mode under the
    provider id `browser` ("Browser downloads").  The browser owns the transfer and Playwright reports no
    progress, so the row says "downloading" with no percent until the file lands, then done (with its
    path, Open and Show in folder) or failed;
  * these rows have NO Pause, Resume or Cancel: the provider declares no such capability and no control
    tool (the same rule as Steam rows, decision S2).  The hub does not simulate a control it does not have;
  * the browser tool's result says what became of the download (`annotate`), so "did it download?" can be
    answered from `dl_status`.

HOOKS: every place `browser_tools.py` calls into this module (brief 6E, problem 1).  There are three, each
inside try / except so that a bug here never breaks a browser tool, and each falls back to what the tool
did before N22a:

  1. `_ensure_page`        -> `attach(context)`   where the browser context is made: put the listeners on.
  2. `@_via_intake(kind)`  -> `run_tool(...)`     on each of the seven `_local_tool_browser_*` functions (goto, click,
                                                  fill, get_text, screenshot, wait_for, close).  One helper in
                                                  browser_tools, one decorator line per tool.  It runs the tool on the
                                                  browser's own thread (see "The browser's thread"), marks a tool that can
                                                  cause a download (click, goto), waits the bounded time and adds the
                                                  download note to the result.  It replaced `_hub_downloads` and its
                                                  four call sites in goto / click / wait_for and `_DOWNLOAD_GRACE_MS`.
  3. `close_browser`       -> `closing(fn, wait_s)` runs the teardown on the browser's thread, lets saves that are
                                                  running finish (only when asked to wait), then fails any that are left.

Asked for (brief 6E, problem 3).  Playwright delivers every download the browser makes, including one a page
starts by itself (a script, a pop-up, a redirect).  A download counts as asked for only when it starts
  * while a browser tool call that can cause one is running (`CAUSAL_TOOLS`: click and goto; there is no key
    press or submit tool, a form is submitted by a click), on the page that tool works on or a page that page
    opened (pop-ups; a page opened by a page that was not asked does not count), or
  * within `ASKED_WINDOW_S` after such a call ends, on the same page or one it opened.
Anything else is cancelled (`download.cancel()`), nothing is saved, no row is made, and the next browser tool
result says "a page tried to download <name>; it was not saved" so the owner can say yes and the model can
click it.  The window is above the measured start of a download (see the comment on ASKED_WINDOW_S: a link's download starts
inside the click; a landing page's countdown of 3 s starts it after the call returned).  There is no hub setting.

The browser's thread (brief 6E, problem 2).  Playwright's sync API can be used only from the thread that
started it (measured: another thread gets "cannot switch to a different thread"), and a download only moves
(its event, its `save_as`) while some Playwright call is running.  So one thread, `_Owner`, runs every browser
tool call and, between calls, keeps the browser pumping (`IDLE_PUMP_MS`).  A tool call therefore returns
after at most `SETTLE_GRACE_MS` + `SETTLE_BOUND_S` of waiting for the downloads it started, with what it
knows ("still downloading", the row id); the save carries on in that thread and the row ends done or failed
by itself.  The same thread is what lets the warm browser daemon (a thread per request) call Playwright at
all.  If the thread cannot be started a tool runs on the caller's thread, as before N22a.

What this does NOT do, on purpose:

  * No cookie is read, copied or passed anywhere.  The browser stays the only thing that holds the login;
    the HTTP and yt-dlp engines keep dropping cookies.  (`tests/test_hub_browser_intake.py` greps this file.)
  * No new way to start a download.  The confirmation on `browser_click` / `browser_fill` is unchanged.
  * The page's address is never stored: a signed link can carry a token in its query.  The row keeps the
    host name only.

A save the browser closes on is a failed row, never a row left "downloading".  A browser that closes
with a save running (`close_browser` at the end of an ask, the daemon's idle close, `browser_close`) fails it
with "the browser was closed before the file was saved"; a file that already landed is never lost.
"""

import functools
import os
import queue
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
SETTLE_BOUND_S = 8.0         # how long one browser tool call waits for the saves IT started (see below)
ASKED_WINDOW_S = 5.0         # after a click / goto ends: a download that starts within this is still "asked for"
SETTLE_POLL_MS = 200
IDLE_PUMP_MS = 100           # between tool calls the browser thread lets Playwright run this long at a time
DRAIN_AT_CLOSE_S = 30.0      # when an ask ends (or the daemon idles out) with a save running, wait this long before closing
MAX_NOTES = 20
TICK_EVERY_S = 3.0
REPLACE_TRIES = 6            # putting the finished file in place can fail for a moment (a virus scanner holds it on Windows)
REPLACE_WAIT_S = 0.5

# Tool kinds that can cause a download.  A click follows a link or submits a form; goto navigates.  `fill`
# only types, `wait_for`, `get_text` and `screenshot` only look.  (No key-press tool exists today: if one is
# added, its kind goes here.)
CAUSAL_TOOLS = frozenset({"click", "goto"})
START_WAIT_MS = {"click": SETTLE_GRACE_MS}   # a click waits this long for a download to begin; goto waits only when Chromium
                                             # refused the navigation with "Download is starting"

# Why SETTLE_BOUND_S = 8: a small file (a PDF, a patch, a few tens of MB on an ordinary line) lands inside it
# and the model gets the path in the same result; a big installer does not hold the model's turn for minutes.
# It is also far under the warm daemon's 30 s per call (browser_tools._DAEMON_CALL_TIMEOUT), which the tests check.
# Why ASKED_WINDOW_S = 5: Playwright's click waits until a link's navigation has started, so a link's download
# (even from a server that waits 3 s before its headers) begins inside the click call itself: measured 0.2 s,
# 1.6 s and 3.0 s after the click was issued for a server delay of 0, 1.5 and 3 s.  What starts AFTER the call
# has returned is a page's own timer: a landing page that says "your download starts in 3 seconds", a
# redirect after a script.  The test page's countdown starts it 3 s after load; the window is above that and
# above the click's own 0.5 s wait.  A longer window would let a script that waits for a click slip in.

_lock = threading.RLock()
_saving = {}                 # local id -> {"item", "title", "part", "started", "done"}
_notes = []                  # outcomes the model has not been told about yet (done, failed, refused)
_contexts = set()            # id() of contexts already watched
_pages = set()               # id() of pages already watched
_live_pages = []             # the pages that can still be pumped, oldest first
_last_page = [None]
_parent = {}                 # id(page) -> id(the page that opened it)
_asked_until = {}            # id(page) or "*" -> monotonic time until which a download there still counts as asked for
_active = [None]             # {"kind", "page_of"} while a tool that can cause a download is running
_last_tick = [0.0]
_closing = [False]            # the browser is being closed on purpose: a save that fails now says so


def _now():
    return time.monotonic()


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


def _save_download(download):
    """Save one download that was asked for.  Runs inside Playwright's event dispatch and blocks until the file
    is saved (the save only moves while the browser's thread is pumping, see the module docstring).  Never raises."""
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
            if _closing[0]:
                _finish_failed(local_id, entry, "the browser was closed before the file was saved")
            else:
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


# --------------------------------------------------------------------------- asked for (problem 3)

def _chain(page):
    """The page and the pages above it (the one that opened it, and so on), as ids."""
    ids = []
    cur = id(page) if page is not None else None
    while cur is not None and cur not in ids and len(ids) < 8:
        ids.append(cur)
        cur = _parent.get(cur)
    return ids


def _safe_page(page_of):
    try:
        return page_of() if page_of else None
    except Exception:  # noqa: BLE001
        return None


def _asked_for(page):
    """True when a download that just started on `page` was asked for: a tool call that can cause one is running
    on that page (or a page it opened), or one ended on it a moment ago.  See the module docstring."""
    now = _now()
    with _lock:
        chain = _chain(page)
        act = _active[0]
        if act is not None:
            acted = _safe_page(act.get("page_of"))
            if acted is None or id(acted) in chain:      # no page known (a fake): the tool call is enough
                return True
        return any(_asked_until.get(key, 0.0) > now for key in ["*"] + chain)


def _refuse(download):
    """A download nobody asked for: cancel it, save nothing, and remember to tell the model."""
    try:
        suggested = download.suggested_filename
    except Exception:  # noqa: BLE001
        suggested = ""
    name = naming.sanitize_filename(suggested, default="download")[:80]
    try:
        host = _host(getattr(download, "url", ""))
    except Exception:  # noqa: BLE001
        host = ""
    try:
        download.cancel()
    except Exception:  # noqa: BLE001 - already finished or the page is gone: nothing was saved either way
        pass
    with _lock:
        for note in _notes:
            if note.get("state") == "refused" and note.get("title") == name:
                note["times"] = note.get("times", 1) + 1
                return
        _notes.append({"state": "refused", "title": name, "host": host, "times": 1})
        del _notes[:-MAX_NOTES]


def _on_download(download, page=None):
    """One download event: save it when it was asked for, cancel it when it was not.  Never raises."""
    try:
        if _asked_for(page):
            _save_download(download)
        else:
            _refuse(download)
    except Exception:  # noqa: BLE001 - nothing in here may break the browser
        pass


def _opened(parent, child):
    """`parent` opened the page `child` (a pop-up or a target=_blank link)."""
    try:
        with _lock:
            _parent[id(child)] = id(parent)
    except Exception:  # noqa: BLE001
        pass


def _begin(kind, page_of):
    """A tool call starts (on the browser's thread).  Returns the saves already running, so the call waits only
    for the ones it starts."""
    _pump(None, 1)                     # judge what already happened with the old state, not this call's
    with _lock:
        before = set(_saving)
        if kind in CAUSAL_TOOLS:
            _active[0] = {"kind": kind, "page_of": page_of}
    return before


def _end(kind, page_of):
    with _lock:
        _active[0] = None
        if kind in CAUSAL_TOOLS:
            page = _safe_page(page_of)
            now = _now()
            _asked_until["*" if page is None else id(page)] = now + ASKED_WINDOW_S
            for key in [k for k, until in _asked_until.items() if until <= now]:
                del _asked_until[key]


# --------------------------------------------------------------------------- watching the browser

def _page_closed(page):
    with _lock:
        _pages.discard(id(page))
        _parent.pop(id(page), None)
        _asked_until.pop(id(page), None)
        if page in _live_pages:
            _live_pages.remove(page)
        if _last_page[0] is page:
            _last_page[0] = _live_pages[-1] if _live_pages else None


def _watch_page(page):
    try:
        if id(page) in _pages:
            return
        _pages.add(id(page))
        _live_pages.append(page)
        _last_page[0] = page
        page.on("download", lambda download, _page=page: _on_download(download, _page))
        page.on("popup", lambda child, _page=page: _opened(_page, child))
        page.on("close", lambda *_args, _page=page: _page_closed(_page))
    except Exception:  # noqa: BLE001
        pass


def _on_context_closed(context=None, *_args):
    """The browser closed: a save still running can't finish.  Fail those rows now, so nothing is left
    "downloading" for ever."""
    with _lock:
        running = list(_saving.items())
    for local_id, entry in running:
        _finish_failed(local_id, entry, "the browser was closed before the file was saved")
    with _lock:
        if context is not None:
            _contexts.discard(id(context))
        else:
            _contexts.clear()
        _pages.clear()
        _live_pages[:] = []
        _parent.clear()
        _asked_until.clear()
        _active[0] = None
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
        context.on("close", lambda *_args, _context=context: _on_context_closed(_context))
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- the browser's thread (problem 2)

class _Owner:
    """The one thread that uses Playwright.  Tool calls are handed to it and it keeps the browser pumping
    in between, so a save that outlives its tool call carries on and ends by itself."""

    def __init__(self):
        self.queue = queue.Queue()
        self.thread = None
        self.ident = None
        self._start_lock = threading.Lock()

    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def ensure(self):
        with self._start_lock:
            if self.alive():
                return True
            ready = threading.Event()
            self.thread = threading.Thread(target=self._loop, args=(ready,), name="jarvis-browser", daemon=True)
            self.thread.start()
            ready.wait(5.0)
            return self.alive()

    def call(self, fn, *args):
        if threading.get_ident() == self.ident:
            return fn(*args)
        box = {"done": threading.Event()}
        self.queue.put((fn, args, box))
        while not box["done"].wait(1.0):
            if not self.alive():
                raise RuntimeError("the browser thread stopped")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _loop(self, ready):
        self.ident = threading.get_ident()
        ready.set()
        while True:
            watching = bool(_live_pages)
            try:
                item = self.queue.get(timeout=IDLE_PUMP_MS / 1000.0 if watching else None)
            except queue.Empty:
                _idle_pump()
                continue
            fn, args, box = item
            try:
                box["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller, who raises it there
                box["error"] = exc
            box["done"].set()


_owner = _Owner()


def _idle_pump():
    """Nobody is using the browser: let Playwright run, so downloads are judged when they start and saves go on."""
    try:
        if not _pump(None, IDLE_PUMP_MS):
            _on_context_closed()            # every page is gone: whatever was still saving can't finish
        _tick_messages()
    except Exception:  # noqa: BLE001
        pass


def run_on_owner(fn, *args):
    """Run `fn` on the browser's thread (started on first use).  If that thread can't be had, run it here."""
    try:
        started = _owner.ensure()
    except Exception:  # noqa: BLE001
        started = False
    if not started:
        return fn(*args)
    return _owner.call(fn, *args)


def run_tool(fn, kind, args, page_of):
    """One browser tool call (hook 2).  Runs `fn(args)` on the browser's thread, then adds to its result what
    became of any download.  `fn` is the tool as it was before N22a: its own errors are its own.  Whatever goes
    wrong in this module's part, the tool still runs once and its result comes back."""
    try:
        started = _owner.ensure()
    except Exception:  # noqa: BLE001
        started = False
    if not started:
        return fn(args)
    return _owner.call(_run_tool_here, fn, kind, args, page_of)


def _run_tool_here(fn, kind, args, page_of):
    try:
        before = _begin(kind, page_of)
    except Exception:  # noqa: BLE001
        before = None
    try:
        result = fn(args)
    finally:
        try:
            _end(kind, page_of)
        except Exception:  # noqa: BLE001
            pass
    try:
        grace = START_WAIT_MS.get(kind, 0)
        bound = SETTLE_BOUND_S if kind in CAUSAL_TOOLS else 0.0
        return annotate(result, _safe_page(page_of), grace_ms=grace, max_s=bound, before=before)
    except Exception:  # noqa: BLE001 - the tool's own result is what matters
        return result


def closing(fn, wait_s=0.0):
    """The browser is being closed (hook 3): run its teardown `fn` on the browser's thread, after letting saves
    that are running finish for up to `wait_s`, and fail any that are left.  A browser that was never used
    here just runs `fn`.  Never raises of its own."""
    try:
        if _owner.alive() and wait_s and _saving_count() and threading.get_ident() != _owner.ident:
            deadline = _now() + float(wait_s)
            while _saving_count() and _now() < deadline and _owner.alive():
                time.sleep(0.1)                 # the browser's thread is pumping: the saves move
        elif wait_s and _saving_count():
            settle(None, wait_s)                # no such thread (a fake, or a browser set up by hand): pump here
    except Exception:  # noqa: BLE001
        pass
    _closing[0] = True
    try:
        if _owner.alive():
            return _owner.call(fn)
        return fn()
    finally:
        try:
            _on_context_closed()
        except Exception:  # noqa: BLE001
            pass
        _closing[0] = False


# --------------------------------------------------------------------------- waiting, and telling the model

def saving_now(only=None):
    with _lock:
        return [{"id": "%s:%s" % (PROVIDER_ID, k), "title": v["title"],
                 "seconds": int(time.monotonic() - v["started"])}
                for k, v in _saving.items() if only is None or k in only]


def _pump(page, ms):
    """Let Playwright run for `ms` (events and saves only move while it does).  False when it can't."""
    candidates = [page] + list(reversed(_live_pages)) + [_last_page[0]]
    seen = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        try:
            candidate.wait_for_timeout(ms)
            return True
        except Exception:  # noqa: BLE001 - that page is gone; try the next one
            continue
    return False


def settle(page=None, max_s=None, only=None):
    """Keep the browser running until the saves that have started have landed (all of them, or only the ids in
    `only`), or `max_s` ran out.  Returns True when none of them is still saving."""
    limit = SETTLE_BOUND_S if max_s is None else float(max_s)
    started = time.monotonic()
    while _saving_count(only):
        if time.monotonic() - started >= limit:
            return False
        if not _pump(page, SETTLE_POLL_MS):
            return False
        _tick_messages()
    return True


def _saving_count(only=None):
    with _lock:
        if only is None:
            return len(_saving)
        return len([k for k in _saving if k in only])


def _tick_messages():
    now = time.monotonic()
    if now - _last_tick[0] < TICK_EVERY_S:
        return
    _last_tick[0] = now
    with _lock:
        running = list(_saving.values())
    for entry in running:
        try:
            seconds = int(now - entry["started"])
            entry["item"].progress(message="the browser is downloading it (%d s); Jarvis saves it when it lands" % seconds)
        except Exception:  # noqa: BLE001
            pass


def take_notes():
    with _lock:
        out = list(_notes)
        del _notes[:]
    return out


def sentence(notes, still):
    """What the model is told.  Plain, short, and honest about what is and is not done."""
    parts = []
    saved = False
    for note in notes:
        state = note.get("state")
        if state == "done":
            saved = True
            parts.append("The file '%s' was handed to the Download Hub and saved as %s (id %s; dl_status shows it)."
                         % (note.get("title"), note.get("path"), note.get("id")))
        elif state == "refused":
            times = " (%d times)" % note["times"] if note.get("times", 1) > 1 else ""
            parts.append("A page tried to download '%s'%s; it was not saved, because no click or navigation of yours started it. "
                         "If the owner wants that file, they can say so and you can click its download link."
                         % (note.get("title"), times))
        else:
            saved = True
            parts.append("A browser download '%s' failed: %s (id %s; it is a failed row in the Download Hub)."
                         % (note.get("title"), note.get("error"), note.get("id")))
    for run in still:
        saved = True
        parts.append("The file '%s' is still being downloaded by the browser (id %s, %d s so far). It carries on by itself and "
                     "goes to the Download Hub when it lands; dl_status shows the row. Closing the browser first makes it fail."
                     % (run["title"], run["id"], run["seconds"]))
    if saved:
        parts.append("Browser downloads have no pause or cancel.")
    return " ".join(parts)


def annotate(result, page=None, grace_ms=0, max_s=None, before=None):
    """Add what became of any download to a browser tool's result.

    `grace_ms`: wait that long first for a download to start (a click).  Then wait, for at most `max_s`
    (default SETTLE_BOUND_S), for the saves this call started (`before` = the saves running when it began; none
    given = every save).  What has landed is reported as done, what has not as "still downloading" with its
    row id.  A result with nothing to say comes back untouched.  A `goto` that Chromium refused with "Download
    is starting" is turned into a success, because that is exactly what happened.  Never raises."""
    try:
        if page is not None:
            _last_page[0] = page
        if not grace_ms and isinstance(result, dict) and "Download is starting" in str(result.get("error") or ""):
            grace_ms = SETTLE_GRACE_MS      # Chromium refused the navigation: the download event is right behind it
        if grace_ms:
            _pump(page, grace_ms)
        with _lock:
            mine = None if before is None else set(_saving) - set(before)
        settle(page, max_s, only=mine)
        notes, still = take_notes(), saving_now(only=mine)
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
