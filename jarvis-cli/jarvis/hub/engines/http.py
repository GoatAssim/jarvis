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

Everything from the network -- file names, headers, redirects -- is untrusted: names go
through naming.sanitize_filename, the destination is forced under the library root, and
free disk space is checked before the first byte is written (refuse, never truncate).
"""

import os
import re
import shutil
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


def stream_to_file(ctx, item, url, headers=None, resolver=None, session=None):
    """Download `url` for `item`; returns an Outcome.  The reusable core of this engine."""
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
    offset = os.path.getsize(part) if part and os.path.exists(part) else 0

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
            return stream_to_file(ctx, item, url)
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
