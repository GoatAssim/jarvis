"""The Stremio engine (plan sections 7.3 and 8): torrents through Stremio's streaming server.

A Stremio "download" is Jarvis holding an HTTP request open on the server and reading it to the
end; the server fetches the pieces.  This engine is the HTTP engine plus two extras:

  1. REGISTER   POST /<hash>/create (a torrent with no peers keeps that request open, yet it IS
                registered, so we poll /<hash>/stats.json for `files[]` instead of waiting on it);
                a .torrent URL is fetched (SSRF-guarded) and registered with POST /create.  A .torrent FILE
                on this PC (the owner gave its path) is claimed by `_match`, checked and copied into the hub's
                own folder by stremio/torrentfile.py, and queued as a magnet; this step then registers the kept
                copy with POST /create exactly as it does a fetched one.  The row never holds the path.
  2. CHOOSE     the file: the index the source named, else SxxExx in a pack, else the largest video.
  3. DOWNLOAD   engines.http.stream_to_file on  /<hash>/<idx>?external=1&download=1  (the server
                answers 307 -> /<hash>/<name>?download=1; the HTTP engine follows it with Range
                kept) into `<library>/Stremio/...`, resumable from the `.part` file's size.
  4. WATCHDOG   a thread reads stats.json every 10 s for peers / downloaded / speed, so the row
                shows peers and the final message can say "no peers" instead of just "stalled".
  5. DONE       EOF + the file's size equals `files[idx].length` from stats.json.

Storage mode is `save` (plan decision 6): the hub owns an ordinary video file.  `warm` is not built.

The server forgets every registration when it restarts, so each attempt starts with `create`
again -- the hub store is the source of truth, and a daemon restart or a dropped connection
resumes from the `.part` size with Range.

Optional hints another phase may leave on the row (all handled when absent):
    options / source_ref  show, season, episode      -> `Stremio/<Show>/Season NN/<Show> - SxxExx.<ext>`
    source_ref["fallbacks"]  [magnet text | stream object, ...]  ranked alternatives: on "no
                          seeders" or a second stall the engine switches to the next one.

The SSRF guard still applies to everything this engine fetches from the network.  The one entry
it adds to `allow_private_hosts` is the configured streaming server's own host:port (loopback,
enforced by stremio/config.py), and only for the calls this engine makes: no other loopback
port is opened, and the owner's allow-list is not modified.
"""

import os
import threading
import time
from pathlib import Path

from .. import naming, netguard, states
from .. import settings as settings_mod
from ..stremio import config as scfg
from ..stremio import links
from ..stremio import server as sserver
from ..stremio import torrentfile
from .base import Engine, Outcome, RouteRule

NAME = "stremio"
STATS_EVERY = 10.0          # watchdog period (plan 7.3 step 4)
MAX_TORRENT_FILE = torrentfile.MAX_BYTES        # one cap for a fetched link and a local file: 5 MB
FREE_SPACE_MARGIN = 1.10
USER_AGENT = "Jarvis-Hub/1"


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def _short(exc):
    return states.clean_text("%s: %s" % (type(exc).__name__, exc), 160)


def _free_bytes(path):
    import shutil
    probe = Path(path)
    for _ in range(12):
        if probe.exists():
            try:
                return shutil.disk_usage(str(probe)).free
            except OSError:
                return None
        if probe.parent == probe:
            break
        probe = probe.parent
    return None


# --------------------------------------------------------------------------- the ctx the HTTP engine sees

class _EngineCtx(object):
    """The EngineContext with two answers changed, for the HTTP engine's benefit only:

      allow_private_hosts  gains the streaming server's host:port (and nothing else)
      http.stall_seconds   becomes stremio.stall_seconds (a torrent is slower to start than a file)
    Everything else (update, set_state, stop_requested, rate_limit_bps, settings ...) is the real one.
    """

    def __init__(self, ctx, allow_entry, stall_seconds):
        self._ctx = ctx
        self._allow = allow_entry
        self._stall = stall_seconds

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def setting(self, *path, **kwargs):
        if path == ("allow_private_hosts",):
            base = self._ctx.setting("allow_private_hosts", default=[]) or []
            if isinstance(base, str):
                base = [base]
            return list(base) + [self._allow]
        if path == ("http", "stall_seconds"):
            return self._stall
        return self._ctx.setting(*path, **kwargs)


# --------------------------------------------------------------------------- the stats watchdog

class _Watchdog(threading.Thread):
    """Reads /<hash>/stats.json every few seconds while the transfer runs."""

    def __init__(self, server, infohash, ctx, interval=STATS_EVERY):
        super().__init__(name="stremio-watch-%s" % infohash[:8], daemon=True)
        self.server, self.infohash, self.ctx, self.interval = server, infohash, ctx, interval
        self._halt = threading.Event()
        self.last = {}                      # the latest {peers, downloaded, speed}
        self._quiet = 0

    def run(self):
        while not self._halt.wait(self.interval):
            try:
                stats = self.server.stats(self.infohash, timeout=5)
            except sserver.ServerError:
                continue
            if not stats:
                continue
            peers = _int(stats.get("peers"))
            self.last = {"peers": peers, "downloaded": _int(stats.get("downloaded")),
                         "speed": _int(stats.get("downloadSpeed"))}
            self._quiet = self._quiet + 1 if (peers == 0 and not self.last["speed"]) else 0
            fields = {"peers": peers}
            fields["phase"] = "waiting for peers" if self._quiet >= 2 else ""
            if not self.ctx.update(**fields):          # the row is gone
                return

    def stop(self):
        self._halt.set()
        self.join(2.0)


def _int(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- the engine

class StremioEngine(Engine):
    name = NAME
    label = "Stremio (torrent)"
    supports_pause = True
    # The daemon asks `available()` before every attempt.  A stopped Stremio server is a problem
    # that goes away (the owner starts it, or it restarts), so that refusal is retried with a
    # delay instead of failing the row for good (see HubDaemon.run_item).
    unavailable_is_retryable = True
    unavailable_delay_s = 20.0

    def available(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            return False, "the `requests` package is missing"
        up, info = sserver.probe()
        if not up:
            return False, ("the Stremio streaming server isn't running at %s. Start it with stack_start "
                           "(or Menu > Daemons); if it is already open in the Stremio app, check its address "
                           "in ~/.jarvis/stremio.json" % scfg.server_url())
        return True, ""

    def doctor(self):
        """Extra hub_doctor lines: the server's version and cache."""
        up, info = sserver.probe()
        if not up:
            return [{"name": "stremio server", "status": "warn", "detail": info}]
        out = [{"name": "stremio server", "status": "ok", "detail": "version %s at %s" % (info, scfg.server_url())}]
        try:
            values = sserver.StremioServer().values(timeout=4)
            unlimited = values.get("cacheSize") is None
            out.append({"name": "stremio cache", "status": "ok" if unlimited else "warn",
                        "detail": "unlimited" if unlimited else "finite: a finished file can be evicted; stremio_prepare sets unlimited"})
        except sserver.ServerError:
            pass
        return out

    def run(self, item, ctx):
        try:
            return _run(item, ctx)
        except Exception as exc:  # noqa: BLE001 - an engine never raises past run
            return Outcome.failed("unexpected error: %s" % _short(exc))


def _stop_outcome(stop):
    if stop == "pause":
        return Outcome.paused()
    if stop == "cancel":
        return Outcome.cancelled()
    return Outcome.requeue()


def _wait(ctx, seconds):
    """Sleep up to `seconds`, waking on a stop request.  Returns the stop reason or None."""
    end = time.monotonic() + seconds
    while True:
        stop = ctx.stop_requested()
        if stop:
            return stop
        left = end - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(0.25, left))


def _hint(item, ref, key):
    value = (item.get("options") or {}).get(key)
    if value in (None, ""):
        value = ref.get(key)
    return value


def _int_hint(item, ref, key):
    try:
        number = int(_hint(item, ref, key))
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _candidate(entry, hostport):
    """A fallback entry (magnet text or stream object) -> parsed dict, or None."""
    parsed = links.parse_stream(entry) if isinstance(entry, dict) else links.parse_input(entry, hostport)
    if parsed and parsed.get("infohash"):
        return parsed
    return None


def _target(item, ref, hostport):
    """What to download: the active fallback if one was chosen, else the row's own source."""
    active = ref.get("active")
    if isinstance(active, dict) and links.normalize_infohash(active.get("infohash")):
        return {"infohash": links.normalize_infohash(active["infohash"]), "file_idx": active.get("file_idx"),
                "trackers": links._clean_trackers(active.get("trackers")), "kind": "active"}
    parsed = links.parse_input(item.get("source_url"), hostport)
    if parsed is None:
        return None
    if parsed["kind"] == "torrent_url" and links.normalize_infohash(ref.get("infohash")):
        parsed = dict(parsed, infohash=links.normalize_infohash(ref["infohash"]))
    return parsed


def _fetch_torrent(url, ctx):
    """Download a .torrent file's bytes.  Every hop goes through the SSRF guard; the size is capped."""
    import requests
    from . import http as http_engine
    allow = list(ctx.setting("allow_private_hosts", default=[]) or [])
    session = requests.Session()
    resp = None
    try:
        resp, _final = http_engine.open_stream(
            session, url, {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}, allow, (15, 30))
        if resp.status_code != 200:
            return None, "the .torrent link answered HTTP %d" % resp.status_code
        data = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            data += chunk
            if len(data) > MAX_TORRENT_FILE:
                return None, "that .torrent file is larger than %s" % _fmt_size(MAX_TORRENT_FILE)
        if not data.startswith(b"d"):
            return None, "that link isn't a .torrent file"
        return bytes(data), None
    except netguard.GuardError as exc:
        return None, str(exc)
    except requests.exceptions.RequestException as exc:
        return None, "couldn't fetch the .torrent file: %s" % _short(exc)
    finally:
        if resp is not None:
            resp.close()
        session.close()


def _register(server, target, ctx, wait_s):
    """create + wait for the file list.  Returns (stats, None) or (None, Outcome)."""
    infohash, trackers = target["infohash"], target.get("trackers") or []
    stats = None
    try:
        stats = server.create(infohash, trackers)
    except sserver.ServerBusy:
        stats = None                          # registered, but no peers yet: poll for the metadata
    except sserver.ServerError as exc:
        return None, Outcome.failed(str(exc), retryable=True, delay_s=10.0)
    deadline = time.monotonic() + wait_s
    last_create = time.monotonic()
    errors = 0
    while not (stats and stats.get("files")):
        stop = ctx.stop_requested()
        if stop:
            return None, _stop_outcome(stop)
        if time.monotonic() >= deadline:
            return None, Outcome.failed("no_seeders", retryable=False)
        ctx.update(phase="waiting for metadata", peers=_int((stats or {}).get("peers")))
        stop = _wait(ctx, 2.0)
        if stop:
            return None, _stop_outcome(stop)
        try:
            stats = server.stats(infohash, timeout=8)
            errors = 0
            if stats is None and time.monotonic() - last_create >= 15:
                last_create = time.monotonic()
                try:
                    stats = server.create(infohash, trackers, timeout=(5, 8))
                except sserver.ServerBusy:
                    stats = None
        except sserver.ServerError as exc:
            errors += 1
            if errors >= 5:
                return None, Outcome.failed(str(exc), retryable=True, delay_s=10.0)
    return stats, None


def _layout(item, ref, ctx, entry, idx):
    """(options for the HTTP engine, a human note).  Chooses the file name and folder; never a path."""
    opts = dict(item.get("options") or {})
    raw = links.file_name(entry)
    ext = os.path.splitext(raw)[1]
    show = _hint(item, ref, "show")
    season, episode = _int_hint(item, ref, "season"), _int_hint(item, ref, "episode")
    name = naming.sanitize_filename(opts.get("filename") or raw, default="video%s" % ext)
    folder = None
    if show and season is not None:
        show_clean = naming.sanitize_filename(show, default="Show", max_len=60)
        folder = "Stremio/%s/Season %02d" % (show_clean, season)
        if episode is not None and not opts.get("filename"):
            name = naming.sanitize_filename("%s - S%02dE%02d%s" % (show_clean, season, episode, ext), default=name)
    if not opts.get("filename"):
        opts["filename"] = name
    if not opts.get("subdir") and not opts.get("dest_abs"):
        if folder:
            opts["subdir"] = folder
        elif not settings_mod.ext_folder_spec(ctx.settings, name):
            opts["subdir"] = "Stremio"
    return opts


def _cache_problem(server, length):
    """A sentence when the Stremio cache drive can't hold this file, else None.  Only checked for
    an unlimited cache (a finite one evicts instead of filling the disk)."""
    try:
        values = server.values(timeout=5)
    except sserver.ServerError:
        return None
    if values.get("cacheSize") is not None:
        return None
    root = values.get("cacheRoot")
    free = _free_bytes(root) if root else None
    if free is not None and length and free < int(length * FREE_SPACE_MARGIN):
        return ("not enough free space on the Stremio cache drive (%s): the file is %s, %s free. "
                "Change the cache folder with stremio_prepare or free some space"
                % (str(root)[:80], _fmt_size(length), _fmt_size(free)))
    return None


def _remember(ctx, ref):
    """stream_to_file starts a fresh source_ref the first time (part / final / etag); put our own keys
    (infohash, file index, hints, fallbacks) back so a retry, a restart and playback can find them."""
    try:
        row = ctx.store.get_item(ctx.item_id) or {}
        merged = dict(row.get("source_ref") or {})
        for key, value in ref.items():
            if key not in ("part", "final", "etag", "last_modified", "accept_ranges"):
                merged[key] = value
        ctx.update(source_ref=merged)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail a download
        pass


def _switch_candidate(ctx, item, ref, hostport, why):
    """Move to the next ranked fallback.  Returns an Outcome (a quick retry) or None when there is none."""
    pending = list(ref.get("fallbacks") or [])
    while pending:
        parsed = _candidate(pending.pop(0), hostport)
        if not parsed:
            continue
        part = ref.get("part")
        if part:
            try:
                os.remove(part)
            except OSError:
                pass
        fresh = {"fallbacks": pending, "switched": int(ref.get("switched") or 0) + 1,
                 "active": {"infohash": parsed["infohash"], "file_idx": parsed.get("file_idx"),
                            "trackers": parsed.get("trackers") or []}}
        for key in ("show", "season", "episode"):
            if key in ref:
                fresh[key] = ref[key]
        ctx.update(source_ref=fresh, bytes_done=0, bytes_total=None, source_url=links.canonical_source(parsed),
                   message=None)
        ctx.event("warn", "%s; switching to the next release (%s)" % (why, parsed["infohash"][:10]))
        return Outcome.failed("%s; trying the next release" % why, retryable=True, delay_s=1.0)
    return None


def _run(item, ctx):
    ref = dict(item.get("source_ref") or {})
    hostport = scfg.server_hostport()
    target = _target(item, ref, hostport)
    if target is None:
        return Outcome.failed("that isn't a magnet link, infohash or Stremio stream I can read")
    stremio_cfg = ctx.setting("stremio", default={}) or {}
    stall = max(10, _int(stremio_cfg.get("stall_seconds") or 90))
    meta_wait = max(1, _int(stremio_cfg.get("metadata_timeout") or 90))

    if torrentfile.looks_like_path(item.get("title")):
        # A path typed into dl_add can reach the row as its first title (the add code titles a row with the text it
        # was given).  Never keep a folder name on a row: say the file's own name instead.
        ctx.update(title=(str(target.get("name") or "") + ".torrent") if target.get("name") else "torrent file")

    server = sserver.StremioServer()
    watch = None
    try:
        ctx.set_state("connecting")
        up, why = server.is_up(timeout=3)
        if not up:
            return Outcome.failed(why, retryable=True, delay_s=20.0)

        # --- 1. register (a .torrent link is fetched and registered first; so is the kept copy of a .torrent FILE)
        blob = None
        if target["kind"] == "torrent_url":
            ctx.update(phase="fetching the .torrent file")
            blob, problem = _fetch_torrent(target["url"], ctx)
            if blob is None:
                return Outcome.failed(problem)
        elif target["kind"] in ("magnet", "infohash"):
            blob = torrentfile.staged_blob(target["infohash"])
            if blob is not None:
                # file name and size and infohash only: the folder the file came from was never stored
                ref["torrent_file"] = {"name": (str(target.get("name") or "") + ".torrent") if target.get("name") else "",
                                       "size": len(blob), "infohash": target["infohash"]}
            elif ref.get("torrent_file"):
                ctx.event("warn", "the kept copy of the .torrent file is gone; asking the swarm for the file list instead")
        if blob is not None:
            try:
                created = server.create_from_blob(blob)
            except sserver.ServerError as exc:
                return Outcome.failed(str(exc), retryable=True, delay_s=10.0)
            digest = links.normalize_infohash(created.get("infoHash"))
            if not digest:
                return Outcome.failed("the Stremio server didn't accept that .torrent file")
            target = dict(target, infohash=digest)
            ref["infohash"] = digest
        ctx.update(phase="registering the torrent", source_ref=ref)
        stats, outcome = _register(server, target, ctx, meta_wait)
        if outcome is not None:
            if outcome.kind == Outcome.FAILED and outcome.error == "no_seeders":
                msg = "no seeders: the torrent gave no file list in %d s" % meta_wait
                return _switch_candidate(ctx, item, ref, hostport, msg) or Outcome.failed(
                    msg + ". Try again later, or another release")
            return outcome

        # --- 2. choose the file
        files = stats.get("files") or []
        season, episode = _int_hint(item, ref, "season"), _int_hint(item, ref, "episode")
        idx, why_file = links.pick_file(files, target.get("file_idx"), season, episode)
        if idx is None:
            return Outcome.failed(why_file[:200])
        entry = files[idx]
        length = _int(entry.get("length"))
        ctx.event("info", "file %d of %d: %s (%s; %s)" % (idx, len(files), links.file_name(entry)[:80],
                                                         _fmt_size(length), why_file))
        problem = _cache_problem(server, length)
        if problem:
            return Outcome.failed(problem)

        # --- 3. download through the HTTP engine
        opts = _layout(item, ref, ctx, entry, idx)
        ref.update({"infohash": target["infohash"], "file_idx": idx, "length": length})
        title = str(item.get("title") or "")
        if (not title or title.lower().startswith(("magnet:", "{")) or title == str(item.get("source_url") or "")[:len(title)]
                or torrentfile.looks_like_path(title)):
            ctx.update(title=Path(opts["filename"]).stem or opts["filename"])
        run_item = dict(item, options=opts, source_ref=ref)
        url = links.download_url(server.base, target["infohash"], idx)
        wrapped = _EngineCtx(ctx, hostport, stall)
        watch = _Watchdog(server, target["infohash"], ctx)
        watch.start()
        ctx.update(phase="", peers=_int(stats.get("peers")))
        from . import http as http_engine
        outcome = http_engine.stream_to_file(wrapped, run_item, url, headers={})
        _remember(ctx, ref)

        # --- 4. what happened
        if outcome.kind == Outcome.DONE and outcome.dest_file and length:
            try:
                size = os.path.getsize(outcome.dest_file)
            except OSError:
                size = None
            if size is not None and size != length:
                try:
                    os.remove(outcome.dest_file)
                except OSError:
                    pass
                ctx.update(source_ref={}, bytes_done=0)
                return Outcome.failed("the saved file is %s but the torrent says %s; starting over"
                                      % (_fmt_size(size), _fmt_size(length)), retryable=True, delay_s=2.0)
        if outcome.kind in (Outcome.STALLED, Outcome.FAILED) and outcome.retryable:
            peers = watch.last.get("peers") if watch.last else None
            note = outcome.error or "stalled"
            if peers == 0:
                note += " (the torrent has no peers right now)"
            if outcome.kind == Outcome.STALLED and int(item.get("attempts") or 1) >= 2:
                switched = _switch_candidate(ctx, item, ref, hostport, note)
                if switched is not None:
                    return switched
            outcome.error = note
        return outcome
    except sserver.ServerError as exc:
        return Outcome.failed(str(exc), retryable=True, delay_s=15.0)
    finally:
        if watch is not None:
            watch.stop()
        server.close()


# --------------------------------------------------------------------------- routing

_REASONS = {
    "magnet": "magnet link (Stremio engine)",
    "infohash": "torrent infohash (Stremio engine)",
    "stream": "Stremio stream (Stremio engine)",
    "server_url": "Stremio server link (Stremio engine)",
    "torrent_url": ".torrent file (Stremio engine)",
}


def _match(text, settings):
    parsed = links.parse_input(text, scfg.server_hostport())
    if parsed:
        return {"reason": _REASONS.get(parsed["kind"], "torrent (Stremio engine)"), "kind": "torrent",
                "source": links.canonical_source(parsed)}
    # A path to a .torrent file on this PC.  Only a file that passes every check is claimed (a bad path is not: the
    # router then says what it says for anything it can't take; the one-sentence reasons are
    # torrentfile.local_torrent's second answer).  The row gets a magnet, never the path.
    found, _problem = torrentfile.local_torrent(text)
    if found is None or not torrentfile.stage(found):
        return None
    return {"reason": ".torrent file on this PC (Stremio engine)", "kind": "torrent",
            "source": links.build_magnet(found["infohash"], None, found["stem"])}


ENGINE = StremioEngine()
# Above the HTTP engine (10) so a Stremio server URL or a .torrent link is ours, not "a file".
ROUTE_RULES = [RouteRule("stremio", 50, _match)]
