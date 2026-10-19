"""The HTTP(S) engine (plan section 7.2): plain `requests` streaming into a `.part` file.

  * one GET gives everything a probe would (size, Accept-Ranges, Content-Disposition, the
    final URL) -- many servers mishandle HEAD, and the real request is needed anyway;
  * writes `<name>.part`, renames on success after a size check; a `.part` is never taken as
    finished;
  * RESUME with `Range: bytes=<offset>-` (+ `If-Range` with the ETag when there is one).  A
    server that answers 200 instead of 206 gets the file restarted, never appended to;
  * redirects are followed by hand so the SSRF guard re-checks every hop, and credentials
    are dropped when a redirect changes host;
  * a stall (no data for `http.stall_seconds`) comes back as `Outcome.stalled`, a dropped
    connection or 5xx as a retryable failure; the daemon owns the retry policy;
  * `stream_to_file` is the reusable part: the Stremio engine (phase 4) points it at the
    server's `download=1` URL and adds its own registration and stats watchdog.

SEGMENTED download (plan 7.2 "later option", phase 9) -- OPT IN, never the default:
  * on when `http.segments` (or the item's `options.segments`) is above 1, the file is at least
    `http.segment_min_bytes`, the first answer was a plain 200 with a size and `Accept-Ranges:
    bytes`, and it is a fresh start.  The HTTP engine decides this; `stream_to_file` itself only
    segments when the caller passes `segments=` (the Stremio engine doesn't, and so never does);
  * the file is split into contiguous parts, each fetched by its own `Range` request on its own
    connection and written at its own offset in ONE preallocated `.part`;
  * every part request goes through `open_stream` like the first one: the SSRF guard checks every
    hop of every part, and credentials are dropped when a redirect changes host;
  * a server that doesn't really do Range (a 200 instead of a 206, a wrong Content-Range, a
    different ETag, a 429 for the parallel requests) makes the download carry on as ONE connection,
    in the same run, with nothing lost;
  * progress is saved in `source_ref` (`segments`: per part start / end / bytes done, `total`),
    so pause, a stall, a crash or a kill resume each part where it was; a table that doesn't match
    the file on disk is thrown away and the file starts over, never trusted;
  * pause / cancel / the rate limit act on the whole file (the limit is shared by the parts).

Everything from the network -- file names, headers, redirects -- is untrusted: names go
through naming.sanitize_filename, the destination is forced under the library root, and
free disk space is checked before the first byte is written (refuse, never truncate).
"""

import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import naming, netguard, states
from .. import settings as settings_mod
from .base import Engine, Outcome, RouteRule

CHUNK = 256 * 1024
MAX_REDIRECTS = 5
REPORT_EVERY = 0.5            # seconds between progress writes
FREE_SPACE_MARGIN = 1.10      # need 10 % more than the file (plan section 9.3)
USER_AGENT = "Jarvis-Hub/1"
SEGMENT_MAX = 16                          # parallel parts, at most
MIN_PART_BYTES = 1024 * 1024              # a part is never smaller than this (tests lower it)
DEFAULT_SEGMENT_MIN = 32 * 1024 * 1024    # files smaller than this are never segmented

_REDIRECTS = (301, 302, 303, 307, 308)
# Headers the item may carry (from an addon's proxyHeaders, phase 4).  Anything else is dropped.
_SAFE_HEADERS = {"referer": "Referer", "authorization": "Authorization", "user-agent": "User-Agent",
                 "accept": "Accept", "accept-language": "Accept-Language"}
_DROP_ON_NEW_HOST = ("authorization", "cookie")
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
_CONTENT_RANGE_UNSAT = re.compile(r"bytes\s+\*/(\d+)", re.I)


def clean_headers(raw):
    """Keep only the allow-listed request headers, with single-line values."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        name = _SAFE_HEADERS.get(str(key).lower())
        if name and isinstance(value, str) and len(value) <= 2000 and "\r" not in value and "\n" not in value:
            out[name] = value
    return out


def _retry_after(headers):
    value = (headers.get("Retry-After") or "").strip()
    if value.isdigit() and 0 < int(value) <= 300:
        return float(value)
    return None


def open_stream(session, url, headers, allow_hosts, timeout, resolver=None):
    """GET with manual redirects.  Returns (response, final_url).  Raises GuardError when a
    hop is refused.  Every hop is checked BEFORE it is requested."""
    current = url
    hdrs = dict(headers)
    for _ in range(MAX_REDIRECTS + 1):
        netguard.check_url(current, allow_hosts, resolver)
        resp = session.get(current, headers=hdrs, stream=True, allow_redirects=False, timeout=timeout)
        location = resp.headers.get("Location")
        if resp.status_code in _REDIRECTS and location:
            resp.close()
            nxt = urljoin(current, location)
            if urlsplit(nxt).netloc.lower() != urlsplit(current).netloc.lower():
                for key in list(hdrs):
                    if key.lower() in _DROP_ON_NEW_HOST:
                        del hdrs[key]
            current = nxt
            continue
        return resp, current
    raise netguard.GuardError("too many redirects")


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def stream_to_file(ctx, item, url, headers=None, resolver=None, session=None, segments=1):
    """Download `url` for `item`; returns an Outcome.  The reusable core of this engine.

    `segments` > 1 allows a segmented download (see the module docstring); the default, 1, is
    the single-connection download every caller got before, so existing callers (the Stremio
    engine) are untouched."""
    import requests

    opts = item.get("options") or {}
    stall = int(ctx.setting("http", "stall_seconds", default=60))
    connect_timeout = float(ctx.setting("http", "connect_timeout", default=15))
    allow = ctx.setting("allow_private_hosts", default=[])
    roots = settings_mod.write_roots(ctx.settings, opts)
    ref = dict(item.get("source_ref") or {})
    part, final = ref.get("part"), ref.get("final")
    if part and not (naming.under_any(roots, part) and naming.under_any(roots, final or part)):
        part = final = None                      # a recorded path outside the root is never trusted
        ref = {}
    seg_table = None
    if ref.get("segments"):
        seg_table = _load_table(ref, part)
        if seg_table is None:                   # a table that doesn't match the file is never trusted
            _remove(part)
            part = final = None
            ref = {}
            ctx.update(source_ref={}, bytes_done=0)
    offset = 0 if seg_table else (os.path.getsize(part) if part and os.path.exists(part) else 0)

    req = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
    req.update(clean_headers(headers if headers is not None else opts.get("headers")))
    if offset > 0:
        req["Range"] = "bytes=%d-" % offset
        validator = ref.get("etag") or ref.get("last_modified")
        if validator:
            req["If-Range"] = validator

    own_session = session is None
    session = session or requests.Session()
    resp = None
    try:
        if seg_table:                           # resuming a segmented download
            return _download_segments(ctx, session, url, req, allow, (connect_timeout, stall), resolver,
                                      part, final, ref, seg_table, None, stall)
        try:
            resp, final_url = open_stream(session, url, req, allow, (connect_timeout, stall), resolver)
        except netguard.GuardError as exc:
            return Outcome.failed(exc)
        code = resp.status_code
        hdr = resp.headers

        if code == 416:
            m = _CONTENT_RANGE_UNSAT.match(hdr.get("Content-Range") or "")
            if m and offset and int(m.group(1)) == offset:
                return _finish(ctx, part, final, offset, offset)       # we already had it all
            _remove(part)
            ctx.update(source_ref={}, bytes_done=0)
            return Outcome.failed("the server rejected the resume point; starting over", retryable=True, delay_s=1.0)
        if code in (408, 425, 429) or 500 <= code < 600:
            return Outcome.failed("server answered HTTP %d" % code, retryable=True, delay_s=_retry_after(hdr))
        if code not in (200, 206):
            return Outcome.failed("server answered HTTP %d" % code)

        total = None
        mode = "wb"
        if code == 206:
            m = _CONTENT_RANGE.match(hdr.get("Content-Range") or "")
            if not m or int(m.group(1)) != offset:
                _remove(part)
                ctx.update(source_ref={}, bytes_done=0)
                return Outcome.failed("the server resumed at the wrong place; starting over", retryable=True, delay_s=1.0)
            total = int(m.group(3)) if m.group(3).isdigit() else None
            mode = "ab"
        else:
            if offset:
                ctx.event("info", "the server ignored Range; restarting the file from 0")
                offset = 0
            length = (hdr.get("Content-Length") or "").strip()
            total = int(length) if length.isdigit() else None

        if not part:
            header_name = naming.filename_from_content_disposition(hdr.get("Content-Disposition"))
            raw = opts.get("filename") or header_name or naming.filename_from_url(final_url)
            name = naming.sanitize_filename(raw, default="download")
            if not os.path.splitext(name)[1]:
                name += naming.ext_from_content_type(hdr.get("Content-Type"))
            kind = naming.kind_for(name, hdr.get("Content-Type"))
            dest_dir, _source = settings_mod.resolve_dest_dir(ctx.settings, name, kind, opts)
            if dest_dir is None:
                return Outcome.failed("the destination folder isn't allowed (a sub-folder of the library root, or a full path you approved)")
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return Outcome.failed("can't create %s: %s" % (dest_dir, exc))
            final = str(naming.unique_path(dest_dir / name))
            part = final + ".part"
            ref = {"part": part, "final": final}
            fields = {"dest_dir": str(dest_dir), "dest_file": final, "kind": kind}
            if header_name or not item.get("title"):
                fields["title"] = Path(final).name
            ctx.update(**fields)
        etag, modified = hdr.get("ETag"), hdr.get("Last-Modified")
        if etag and not etag.startswith("W/"):          # a weak ETag is no good for If-Range
            ref["etag"] = etag
        elif modified:
            ref["last_modified"] = modified
        ref["accept_ranges"] = (hdr.get("Accept-Ranges") or "").lower() == "bytes"

        if total is not None:
            try:
                free = shutil.disk_usage(Path(part).parent).free
            except OSError:
                free = None
            need = int((total - offset) * FREE_SPACE_MARGIN)
            if free is not None and free < need:
                return Outcome.failed("not enough free space: needs %s, %s free"
                                      % (_fmt_size(need), _fmt_size(free)))
        if (segments > 1 and code == 200 and offset == 0 and total and ref.get("accept_ranges")
                and total >= int(ctx.setting("http", "segment_min_bytes", default=DEFAULT_SEGMENT_MIN))):
            plan = _plan_segments(total, segments)
            if plan:
                out = _download_segments(ctx, session, url, req, allow, (connect_timeout, stall), resolver,
                                         part, final, ref, plan, resp, stall, total=total)
                if out is not None:
                    return out
                # None: the server won't do parallel Range; carry on with this one connection
        ctx.set_state("downloading", source_ref=ref, bytes_done=offset,
                      **({"bytes_total": total} if total is not None else {}))

        return _transfer(ctx, resp, part, final, mode, offset, total, stall)
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError, requests.exceptions.Timeout) as exc:
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return Outcome.failed("couldn't connect in %d s" % connect_timeout, retryable=True)
        if _is_read_timeout(exc):
            return Outcome.stalled("no data for %d s" % stall)
        return Outcome.failed("connection problem: %s" % _short(exc), retryable=True)
    except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema, requests.exceptions.TooManyRedirects) as exc:
        return Outcome.failed("bad link: %s" % _short(exc))
    except OSError as exc:
        return Outcome.failed("disk problem: %s" % _short(exc))
    finally:
        if resp is not None:
            resp.close()
        if own_session:
            session.close()


def _is_read_timeout(exc):
    """A read timeout while streaming surfaces from `requests` as a plain ConnectionError
    wrapping urllib3's ReadTimeoutError, so look inside it rather than trusting the class."""
    import requests
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return True
    for arg in getattr(exc, "args", ()):
        if type(arg).__name__ == "ReadTimeoutError" or "read timed out" in str(arg).lower():
            return True
    return False


def _short(exc):
    return states.clean_text("%s: %s" % (type(exc).__name__, exc), 160)


def _transfer(ctx, resp, part, final, mode, offset, total, stall):
    """The read loop.  Returns an Outcome."""
    import requests

    done = offset
    written = 0
    tracker = states.RateTracker()
    started = time.monotonic()
    last_report = 0.0
    stop = None

    def report():
        speed = tracker.update(done)
        eta = states.RateTracker.eta(total, done, speed)
        fields = {"bytes_done": done, "speed_bps": speed, "eta_s": eta}
        if total is not None:
            fields["bytes_total"] = total
        else:
            fields["percent"] = None                 # length unknown: an indeterminate bar
        return ctx.update(**fields)

    os.makedirs(os.path.dirname(part) or ".", exist_ok=True)     # a resumed job's folder may be gone
    with open(part, mode) as handle:
        for chunk in resp.iter_content(chunk_size=CHUNK):
            if not chunk:
                continue
            handle.write(chunk)
            done += len(chunk)
            written += len(chunk)
            stop = ctx.stop_requested()
            if stop:
                break
            limit = ctx.rate_limit_bps()
            if limit > 0:
                wait = written / float(limit) - (time.monotonic() - started)
                while wait > 0 and not stop:
                    time.sleep(min(wait, 0.25))
                    wait -= 0.25
                    stop = ctx.stop_requested()
                if stop:
                    break
            now = time.monotonic()
            if now - last_report >= REPORT_EVERY:
                last_report = now
                if not report():                      # the row was deleted under us
                    stop = "cancel"
                    break
        handle.flush()

    if stop:
        report()
        if stop == "pause":
            return Outcome.paused()
        if stop == "cancel":
            _remove(part)
            return Outcome.cancelled()
        return Outcome.requeue()
    return _finish(ctx, part, final, done, total)


def _finish(ctx, part, final, done, total):
    """EOF reached: check the size, then move `.part` to its final name."""
    if total is not None and done < total:
        return Outcome.stalled("the connection ended at %s of %s" % (_fmt_size(done), _fmt_size(total)))
    if total is not None and done > total:
        _remove(part)
        ctx.update(source_ref={}, bytes_done=0)
        return Outcome.failed("received more data than announced; starting over", retryable=True, delay_s=1.0)
    target = Path(final)
    if target.exists():                               # something else took the name meanwhile
        target = naming.unique_path(target, ignore_part=True)
    try:
        os.replace(part, str(target))
    except OSError as exc:
        return Outcome.failed("couldn't finish the file: %s" % _short(exc))
    ctx.update(dest_file=str(target), dest_dir=str(target.parent), bytes_done=done, bytes_total=done)
    return Outcome.done(str(target))


# --------------------------------------------------------------------------- segmented download

class _Seg:
    """One part of a segmented download: bytes [s, e) of the file, `d` of them already written."""
    __slots__ = ("s", "e", "d")

    def __init__(self, s, e, d=0):
        self.s, self.e, self.d = s, e, d

    @property
    def length(self):
        return self.e - self.s

    @property
    def left(self):
        return self.e - self.s - self.d

    def as_dict(self):
        return {"s": self.s, "e": self.e, "d": self.d}


class _PartRefused(Exception):
    """A part request was not answered the way a real Range server answers."""

    def __init__(self, message, code=None, retry_after=None):
        Exception.__init__(self, message)
        self.code = code
        self.retry_after = retry_after


class _EndedEarly(Exception):
    pass


def _as_segments(raw):
    """A usable part count (1..SEGMENT_MAX) from a setting or option value, else None."""
    if raw in (None, "") or isinstance(raw, bool):
        return None
    try:
        return max(1, min(SEGMENT_MAX, int(raw)))
    except (TypeError, ValueError):
        return None


def wanted_segments(ctx, item):
    """How many parallel parts this download may use: the item's own `segments` option wins over
    the `http.segments` setting (1 turns it off for that item); an unusable value is ignored in
    favour of the next level, and with nothing usable it is 1 (off)."""
    own = _as_segments((item.get("options") or {}).get("segments"))
    if own is not None:
        return own
    return _as_segments(ctx.setting("http", "segments", default=1)) or 1


def _plan_segments(total, wanted):
    """The parts for a file of `total` bytes, or None when it isn't worth splitting."""
    count = min(int(wanted), SEGMENT_MAX, int(total // max(1, MIN_PART_BYTES)))
    if count < 2:
        return None
    size = total // count
    segs = []
    for i in range(count):
        start = i * size
        segs.append(_Seg(start, total if i == count - 1 else start + size))
    return segs


def _load_table(ref, part):
    """The saved parts of an interrupted segmented download, or None if they can't be trusted:
    they must tile the whole file with no gap, the counts must be sane and the preallocated
    `.part` must still be exactly as big as the file."""
    raw, total = ref.get("segments"), ref.get("total")
    if (not isinstance(raw, list) or isinstance(total, bool) or not isinstance(total, int) or total <= 0
            or not 1 < len(raw) <= SEGMENT_MAX or not part):
        return None
    segs, expect = [], 0
    for row in raw:
        if not isinstance(row, dict):
            return None
        s, e, d = row.get("s"), row.get("e"), row.get("d")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (s, e, d)):
            return None
        if s != expect or e <= s or d < 0 or d > e - s:
            return None
        segs.append(_Seg(s, e, d))
        expect = e
    if expect != total:
        return None
    try:
        if os.path.getsize(part) != total:
            return None
    except OSError:
        return None
    return segs


def _open_part(session, url, req, allow, timeouts, resolver, seg, validator):
    """Open one part: a Range request through `open_stream`, so the SSRF guard checks every hop
    and credentials drop on a host change, exactly like the first request.  Returns the response,
    or raises _PartRefused when the answer isn't a proper partial answer for this part."""
    hdrs = dict(req)
    hdrs["Range"] = "bytes=%d-%d" % (seg.s + seg.d, seg.e - 1)
    if validator.get("etag") or validator.get("last_modified"):
        hdrs["If-Range"] = validator.get("etag") or validator.get("last_modified")
    resp, _final = open_stream(session, url, hdrs, allow, timeouts, resolver)
    ok = False
    try:
        code = resp.status_code
        if code != 206:
            raise _PartRefused("HTTP %d for a part" % code, code=code, retry_after=_retry_after(resp.headers))
        m = _CONTENT_RANGE.match(resp.headers.get("Content-Range") or "")
        if not m or int(m.group(1)) != seg.s + seg.d or int(m.group(2)) != seg.e - 1:
            raise _PartRefused("a part came back with the wrong range")
        if validator.get("total") and m.group(3).isdigit() and int(m.group(3)) != validator["total"]:
            raise _PartRefused("the file changed size")
        etag = resp.headers.get("ETag")
        if validator.get("etag") and etag and etag != validator["etag"]:
            raise _PartRefused("the file changed on the server")
        length = (resp.headers.get("Content-Length") or "").strip()
        if length.isdigit() and int(length) != seg.left:
            raise _PartRefused("a part has the wrong length")
        ok = True
        return resp
    finally:
        if not ok:
            resp.close()


def _snapshot(ref, segs, total):
    out = dict(ref)
    out["segments"] = [seg.as_dict() for seg in segs]
    out["total"] = total
    return out


def _download_segments(ctx, session, url, req, allow, timeouts, resolver, part, final, ref, segs, first,
                       stall, total=None):
    """The segmented download.  `first` is the already-open plain 200 answer of a FRESH start (its
    stream becomes part 0); None means resuming from a saved table.

    Returns an Outcome, or None only for a fresh start where the server refused parallel parts
    (nothing has been created yet; the caller carries on with the one connection it has)."""
    import requests

    fresh = first is not None
    total = total or ref.get("total") or segs[-1].e
    validator = {"etag": ref.get("etag"), "last_modified": ref.get("last_modified"), "total": total}
    pending = [seg for seg in segs if seg.left > 0]
    opened = {}                                       # id(seg) -> response
    try:
        try:
            for seg in pending:
                if fresh and seg.s == 0:
                    opened[id(seg)] = first           # the stream we already have
                    continue
                opened[id(seg)] = _open_part(session, url, req, allow, timeouts, resolver, seg, validator)
        except netguard.GuardError as exc:        # a hop of a part points somewhere private: refuse the file
            return Outcome.failed(exc)
        except (_PartRefused, requests.exceptions.RequestException) as exc:
            for key, r in list(opened.items()):
                if r is not first:
                    r.close()
            opened.clear()
            if fresh:
                ctx.event("info", "the server won't serve parallel parts (%s); using one connection"
                          % states.clean_text(exc, 80))
                return None
            if isinstance(exc, _PartRefused):
                if exc.code and (exc.code in (408, 425, 429) or 500 <= exc.code < 600):
                    return Outcome.failed("server answered HTTP %d" % exc.code, retryable=True, delay_s=exc.retry_after)
                if exc.code and exc.code not in (200, 416):
                    return Outcome.failed("server answered HTTP %d" % exc.code)
                _remove(part)                          # it no longer does Range / the file changed
                ctx.update(source_ref={}, bytes_done=0)
                return Outcome.failed("the server won't resume the parts (%s); starting over"
                                      % states.clean_text(exc, 80), retryable=True, delay_s=1.0)
            raise

        if fresh:
            try:
                os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
                with open(part, "wb") as handle:
                    handle.truncate(total)             # the whole file's room, parts write at their offsets
            except OSError as exc:
                return Outcome.failed("disk problem: %s" % _short(exc))
            ctx.event("info", "downloading in %d parallel parts" % len(segs))
        ctx.set_state("downloading", source_ref=_snapshot(ref, segs, total), bytes_done=sum(x.d for x in segs),
                      bytes_total=total)
        return _run_segments(ctx, part, final, ref, segs, total, pending, opened, stall)
    finally:
        for r in opened.values():
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


def _run_segments(ctx, part, final, ref, segs, total, pending, opened, stall):
    """Run one thread per unfinished part and watch them.  Returns an Outcome."""
    import requests

    halt = threading.Event()
    errors = []                                       # [(seg, exception)] -- first one wins
    pace = {"limit": ctx.rate_limit_bps(), "free_at": time.monotonic()}
    pace_lock = threading.Lock()

    def throttle(n):
        limit = pace["limit"]
        if limit <= 0:
            return
        with pace_lock:                               # one shared budget for all the parts
            now = time.monotonic()
            start = max(pace["free_at"], now)
            pace["free_at"] = start + n / float(limit)
        wait = start - now
        while wait > 0 and not halt.is_set():
            time.sleep(min(wait, 0.1))
            wait -= 0.1

    def work(seg, resp):
        try:
            with open(part, "r+b") as handle:
                handle.seek(seg.s + seg.d)
                for chunk in resp.iter_content(chunk_size=CHUNK):
                    if halt.is_set():
                        return
                    if not chunk:
                        continue
                    if len(chunk) > seg.left:
                        chunk = chunk[:seg.left]      # never write past this part's end
                    handle.write(chunk)
                    handle.flush()                    # counted only once it is really handed to the OS
                    seg.d += len(chunk)
                    throttle(len(chunk))
                    if seg.left <= 0:
                        return
            if seg.left > 0 and not halt.is_set():
                raise _EndedEarly("the connection ended at %s of part %d" % (_fmt_size(seg.d), seg.s))
        except BaseException as exc:  # noqa: BLE001 - reported to the watcher, never raised in a thread
            if not halt.is_set():
                errors.append((seg, exc))
                halt.set()
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    threads = [threading.Thread(target=work, args=(seg, opened[id(seg)]), daemon=True) for seg in pending]
    for t in threads:
        t.start()

    tracker = states.RateTracker()
    stop, last_report = None, 0.0

    def done_bytes():
        return sum(x.d for x in segs)

    def report():
        done = done_bytes()
        speed = tracker.update(done)
        return ctx.progress(bytes_done=done, bytes_total=total, speed_bps=speed,
                            eta_s=states.RateTracker.eta(total, done, speed),
                            source_ref=_snapshot(ref, segs, total))

    while any(t.is_alive() for t in threads):
        stop = ctx.stop_requested()
        if stop or halt.is_set():
            break
        now = time.monotonic()
        pace["limit"] = ctx.rate_limit_bps()
        if now - last_report >= REPORT_EVERY:
            last_report = now
            if not report():                          # the row was deleted under us
                stop = "cancel"
                break
        time.sleep(0.05)

    halt.set()
    for r in opened.values():                         # unblock parts that are waiting on the network
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass
    for t in threads:
        t.join(5.0)
    report()                                          # the saved table is exactly what is on disk

    if stop == "cancel":
        _remove(part)
        return Outcome.cancelled()
    if stop == "pause":
        return Outcome.paused()
    if stop:
        return Outcome.requeue()
    if errors:
        exc = errors[0][1]
        if isinstance(exc, _EndedEarly):
            return Outcome.stalled(str(exc))
        if isinstance(exc, requests.exceptions.RequestException):
            if isinstance(exc, requests.exceptions.ConnectTimeout):
                return Outcome.failed("couldn't connect", retryable=True)
            if _is_read_timeout(exc):
                return Outcome.stalled("no data for %d s" % stall)
            return Outcome.failed("connection problem: %s" % _short(exc), retryable=True)
        if isinstance(exc, OSError):
            return Outcome.failed("disk problem: %s" % _short(exc))
        return Outcome.failed("unexpected error: %s" % _short(exc))
    if done_bytes() != total or any(seg.left > 0 for seg in segs):
        return Outcome.stalled("the parts ended at %s of %s" % (_fmt_size(done_bytes()), _fmt_size(total)))
    cleaned = {k: v for k, v in _snapshot(ref, segs, total).items() if k not in ("segments", "total")}
    ctx.update(source_ref=cleaned)
    return _finish(ctx, part, final, total, total)


class HttpEngine(Engine):
    name = "http"
    label = "HTTP"
    supports_pause = True

    def available(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            return False, "the `requests` package is missing"
        return True, ""

    def run(self, item, ctx):
        url = item.get("source_url") or ""
        try:
            return stream_to_file(ctx, item, url, segments=wanted_segments(ctx, item))
        except Exception as exc:  # noqa: BLE001 - an engine never raises past run
            return Outcome.failed("unexpected error: %s" % _short(exc))


def _match_http(text, settings):
    text = (text or "").strip()
    if re.match(r"^https?://", text, re.I):
        return {"reason": "direct link (HTTP engine)", "source": text}
    return None


ENGINE = HttpEngine()
# Low priority on purpose: a site-specific engine (yt-dlp, Stremio) claims its links first.
ROUTE_RULES = [RouteRule("http", 10, _match_http)]
