"""THE guarded client for the Minecraft mod sources.  Nothing else in `jarvis/modhosts/` opens a connection.

Rules this file enforces (tests/test_modhosts_net.py pins each one):
  * Fixed host allow-list per vendor, held in hosts.py as constants.  Never read from config or env: a
    configurable host would let a hand-edit or a redirect send the CurseForge key somewhere else.
  * https only.  `netguard.check_url` on the first request and on EVERY redirect; redirects are followed by
    hand, at most `max_redirects`, only to allow-listed hosts (a redirect to plain http on an allowed host
    is upgraded to https, anything else is refused).
  * A per-request timeout (a dead connection must not hang forever) and a response size cap (a defensive
    parsing limit, not one of the owner's limits).
  * An honest User-Agent: `Jarvis/<build> (<contact>)`, `contact` being the tool setting
    minecraft_lookup.contact (default github.com/GoatAssim).
  * The CurseForge key is added ONLY for vendor "curseforge" AND host api.curseforge.com, read lazily
    through `config.get_key()` (a module another patch provides: missing module or missing key means
    reason `no_key`).  It is redacted from every error sentence and never returned.  A caller cannot pass
    its own key / authorization / cookie header: those are dropped.
  * 429: honour Retry-After once when it is short, otherwise `rate_limited` with the wait stated.  Never
    a retry loop.
  * Every exception is caught and rewritten: a library error can carry a URL, so only the kind of failure
    is ever said.

fetch() never raises.  Success: {"ok": True, "status", "headers" (lower-case names), "body", "final_url"};
failure: {"ok": False, "error", "reason"} plus `status` (and a short `body`) when the vendor answered.

Test seams (tests inject these; none of them can take a URL or a host):
  TRANSPORT(method, url, headers, json_body, timeout) -> Reply      the one place bytes move
  RESOLVER(host, port) -> [address]                                    handed to netguard.check_url
  SLEEP(seconds)                                                       the one wait (Retry-After)
  CONTACT_PROVIDER() -> str                                            skips the tool-setting lookup
"""
import importlib
import json
import re
import time
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from . import (BAD_KEY, NO_KEY, NOT_FOUND, OFFLINE, RATE_LIMITED, TOO_BIG, UNSUPPORTED, VENDOR_CHANGED, fail,
               hosts)
from ..hub import netguard

VENDORS = ("modrinth", "curseforge", "optifine", "loader_meta")
_HOST_BLOCKS = {
    "modrinth": "MODRINTH_HOSTS",
    "curseforge": "CURSEFORGE_HOSTS",
    "optifine": "OPTIFINE_HOSTS",
    "loader_meta": "LOADER_META_HOSTS",
}
_LABELS = {"modrinth": "Modrinth", "curseforge": "CurseForge", "optifine": "The OptiFine site",
           "loader_meta": "The loader site"}

# Per-request timeouts (read timeout, seconds).  The Fabric loader answer takes about 20 s, hence 90.
DEFAULT_TIMEOUTS = {"modrinth": 30, "curseforge": 30, "optifine": 60, "loader_meta": 90}
CONNECT_TIMEOUT = 10

# Defensive parsing limits.  Generous on purpose: the real answers seen were 0.2 to 0.6 MB.
DEFAULT_CAPS = {"json": 16 * 1024 * 1024, "text": 8 * 1024 * 1024, "bytes": 128 * 1024 * 1024}
DEFAULT_PREFIX_BYTES = 64
MAX_PREFIX_BYTES = 1024 * 1024
MAX_ERROR_BODY = 2000

KEY_VENDOR = "curseforge"
KEY_HOST = "api.curseforge.com"
KEY_HEADER = "x-api-key"

# A short Retry-After is honoured once; a long one is reported instead.
MAX_RETRY_WAIT = 10.0
DEFAULT_RETRY_WAIT = 5.0

DEFAULT_CONTACT = "github.com/GoatAssim"
_CONTACT_OK = re.compile(r"^[A-Za-z0-9 ._:/@+\-]{1,80}$")
_DROPPED_HEADERS = {"x-api-key", "authorization", "cookie", "host", "user-agent", "proxy-authorization"}
_WANTS = ("json", "text", "bytes", "head", "bytes_prefix")

# --------------------------------------------------------------------------- test seams
TRANSPORT = None
RESOLVER = None
SLEEP = time.sleep
CONTACT_PROVIDER = None


class Reply:
    """What a transport hands back.  `body` may be bytes (the fakes) or `chunks` an iterator of bytes
    (the real client, so the size cap is enforced while reading)."""

    def __init__(self, status, headers=None, body=b"", chunks=None, closer=None):
        self.status_code = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        self._chunks = chunks
        self._closer = closer

    def iter_bytes(self, size=64 * 1024):
        if self._chunks is not None:
            for chunk in self._chunks:
                if chunk:
                    yield chunk
            return
        data = bytes(self._body)
        for start in range(0, len(data), size):
            yield data[start:start + size]

    def close(self):
        try:
            if self._closer:
                self._closer()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- small helpers

def allowed_hosts(vendor):
    """The allow-list of one vendor (a frozenset of lower-case host names; empty for an unknown vendor)."""
    name = _HOST_BLOCKS.get(vendor)
    return frozenset(str(h).lower() for h in (getattr(hosts, name, ()) if name else ()))


def _contact():
    try:
        if CONTACT_PROVIDER is not None:
            value = CONTACT_PROVIDER()
        else:
            from .. import tool_settings
            value = tool_settings.get("minecraft_lookup", "contact", default=DEFAULT_CONTACT)
        raw = str(value or "")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):          # a header value never holds a control character
            return DEFAULT_CONTACT
        value = " ".join(raw.split())
        return value if _CONTACT_OK.match(value) else DEFAULT_CONTACT
    except Exception:  # noqa: BLE001
        return DEFAULT_CONTACT


def user_agent():
    """`Jarvis/<build> (<contact>)`: a uniquely identifying agent, as Modrinth requires."""
    try:
        from .. import build_info
        build = str(getattr(build_info, "BUILD_NUMBER", "") or "0")
    except Exception:  # noqa: BLE001
        build = "0"
    build = re.sub(r"[^0-9A-Za-z._-]", "", build) or "0"
    return "Jarvis/%s (%s)" % (build, _contact())


def _get_key():
    """The CurseForge key, or None.  Lazy on purpose: config.py is another patch's module."""
    try:
        config = importlib.import_module(__package__ + ".config")
        key = config.get_key()
    except Exception:  # noqa: BLE001 - module missing, or it could not read its file
        return None
    return key.strip() if isinstance(key, str) and key.strip() else None


def _scrub(text, key):
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return text


def _label(vendor):
    return _LABELS.get(vendor, "The site")


def _with_params(url, params):
    if not params:
        return url
    pairs = []
    for name, value in (params.items() if isinstance(params, dict) else params):
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        pairs.append((str(name), value if isinstance(value, (list, tuple)) else str(value)))
    if not pairs:
        return url
    query = urlencode(pairs, doseq=True, quote_via=quote)
    return url + ("&" if "?" in url else "?") + query


def _retry_after(headers):
    raw = (headers or {}).get("retry-after")
    try:
        value = float(str(raw).strip())
        return value if value >= 0 else DEFAULT_RETRY_WAIT
    except (TypeError, ValueError):
        return DEFAULT_RETRY_WAIT


def _short_body(raw):
    """A vendor error body, for the callers that tell apart a 400 `[]` from a 404 JSON (loader sites)."""
    try:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:  # noqa: BLE001
        return ""
    text = text.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        if len(text) <= MAX_ERROR_BODY:
            return parsed
    except ValueError:
        pass
    return text[:MAX_ERROR_BODY]


def _real_transport(method, url, headers, json_body, timeout):
    """The only code that really sends bytes.  Lazy import: tests never need `requests`."""
    import requests

    resp = requests.request(method, url, headers=headers, json=json_body, stream=True, allow_redirects=False,
                            timeout=(min(CONNECT_TIMEOUT, timeout), timeout))
    return Reply(resp.status_code, resp.headers, chunks=resp.iter_content(chunk_size=64 * 1024),
                 closer=resp.close)


def _read(reply, limit, deadline):
    """Read at most `limit` bytes.  Returns (bytes, truncated_by_cap).  A body that keeps trickling in past
    `deadline` ends the call (a stalled server must not hold the tool forever)."""
    data = bytearray()
    for chunk in reply.iter_bytes():
        data += chunk
        if limit is not None and len(data) > limit:
            return bytes(data[:limit]), True
        if time.monotonic() > deadline:
            raise TimeoutError("the answer took too long")
    return bytes(data), False


# --------------------------------------------------------------------------- fetch

def fetch(vendor, url, *, method="GET", params=None, json_body=None, headers=None,
          want="json", max_bytes=None, max_redirects=3, timeout=None):
    """One guarded request.  See the module docstring.  Never raises."""
    key = None
    try:
        return _fetch(vendor, url, method, params, json_body, headers, want, max_bytes, max_redirects, timeout)
    except Exception as exc:  # noqa: BLE001 - library errors can carry a URL or a header: say only the kind
        try:
            key = _get_key() if vendor == KEY_VENDOR else None
        except Exception:  # noqa: BLE001
            key = None
        return fail(OFFLINE, _scrub("%s did not answer (%s)." % (_label(vendor), type(exc).__name__), key))


def _fetch(vendor, url, method, params, json_body, headers, want, max_bytes, max_redirects, timeout):
    if vendor not in _HOST_BLOCKS:
        return fail(UNSUPPORTED, "That is not one of the sites Jarvis uses for Minecraft mods.")
    method = str(method or "GET").upper()
    if method not in ("GET", "POST"):
        return fail(UNSUPPORTED, "Only GET and POST are used for the mod sites.")
    if want not in _WANTS:
        return fail(UNSUPPORTED, "Unknown answer type %r." % (want,))
    allow = allowed_hosts(vendor)
    if not allow:
        return fail(UNSUPPORTED, "No allowed hosts are configured for %s." % _label(vendor))
    try:
        allowed_redirects = max(0, int(max_redirects))
    except (TypeError, ValueError):
        allowed_redirects = 3
    followed = 0
    try:
        limit_timeout = float(timeout) if timeout else float(DEFAULT_TIMEOUTS[vendor])
    except (TypeError, ValueError):
        limit_timeout = float(DEFAULT_TIMEOUTS[vendor])
    if limit_timeout <= 0:
        limit_timeout = float(DEFAULT_TIMEOUTS[vendor])
    if want == "bytes_prefix":
        try:
            cap = int(max_bytes) if max_bytes else DEFAULT_PREFIX_BYTES
        except (TypeError, ValueError):
            cap = DEFAULT_PREFIX_BYTES
        cap = max(1, min(cap, MAX_PREFIX_BYTES))
    elif want == "head":
        cap = 0
    else:
        default_cap = DEFAULT_CAPS["bytes" if want == "bytes" else want]
        try:
            cap = int(max_bytes) if max_bytes else default_cap
        except (TypeError, ValueError):
            cap = default_cap
        cap = max(1, cap)

    extra = {}
    for name, value in (headers or {}).items():
        lowered = str(name).strip().lower()
        if lowered and lowered not in _DROPPED_HEADERS and "\n" not in str(value) and "\r" not in str(value):
            extra[str(name).strip()] = str(value)

    current = _with_params(str(url or ""), params)
    key = None                       # the key in play, so every sentence below can be scrubbed
    waited = False
    first = True
    transport = TRANSPORT or _real_transport
    deadline = time.monotonic() + max(limit_timeout * 3, 60.0)

    while True:
        parts = urlsplit(current)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme != "https":
            return fail(UNSUPPORTED, "Only https links are used for the mod sites.")
        try:
            port = parts.port
        except ValueError:
            return fail(UNSUPPORTED, "That address has an invalid port.")
        if port not in (None, 443):
            return fail(UNSUPPORTED, "Addresses with a custom port are not used for the mod sites.")
        if host not in allow:
            if first:
                return fail(UNSUPPORTED, "%s is not one of the sites Jarvis may use for %s; refused."
                            % (host or "That address", _label(vendor)))
            return fail(UNSUPPORTED, "%s redirected to %s, which is not one of the sites Jarvis may use; refused."
                        % (_label(vendor), host or "an unknown host"))
        try:
            netguard.check_url(current, resolver=RESOLVER)
        except netguard.GuardError as exc:
            return fail(UNSUPPORTED, "%s was refused: %s" % (host, _scrub(str(exc).replace(current, "the address"), key)))

        send = {"User-Agent": user_agent(),
                "Accept": "application/json" if want == "json" else "*/*"}
        send.update(extra)
        if method == "POST" and json_body is not None:
            send["Content-Type"] = "application/json"
        key = None
        if vendor == KEY_VENDOR and host == KEY_HOST:
            key = _get_key()
            if not key:
                return fail(NO_KEY, "CurseForge needs a personal API key, and none is set. Get a free one at "
                                    "console.curseforge.com and put it in ~/.jarvis/curseforge.json; Modrinth "
                                    "and OptiFine work without it.")
            send[KEY_HEADER] = key

        try:
            reply = transport(method, current, send, json_body if method == "POST" else None, limit_timeout)
        except Exception as exc:  # noqa: BLE001 - say the kind of failure, never the URL or a header
            kind = type(exc).__name__
            slow = "Timeout" in kind or "timed out" in str(exc).lower()
            return fail(OFFLINE, _scrub("%s %s (%s)." % (
                _label(vendor), "took too long to answer" if slow else "could not be reached", kind), key))
        try:
            status = int(reply.status_code)
            lowered = {str(k).lower(): str(v) for k, v in (reply.headers or {}).items()}
            lowered.pop("set-cookie", None)

            if status == 429:
                wait = _retry_after(lowered)
                if waited or wait > MAX_RETRY_WAIT:
                    return fail(RATE_LIMITED, "%s is rate limiting requests. Wait about %d seconds and try again."
                                % (_label(vendor), int(wait + 0.999)), status=status)
                waited = True
                reply.close()
                SLEEP(wait)
                continue

            if 300 <= status < 400 and lowered.get("location"):
                if allowed_redirects == 0:
                    # max_redirects=0: hand the 3xx back with its Location (hop-by-hop work).
                    return {"ok": True, "status": status, "headers": lowered,
                            "body": b"" if want in ("bytes", "bytes_prefix") else ("" if want == "text" else None),
                            "final_url": current}
                if followed >= allowed_redirects:
                    return fail(VENDOR_CHANGED, "%s redirected too many times." % _label(vendor), status=status)
                if method == "POST":
                    return fail(VENDOR_CHANGED, "%s redirected a request that carries data; refused." % _label(vendor),
                                status=status)
                nxt = urljoin(current, lowered["location"])
                if nxt.lower().startswith("http://"):
                    nxt = "https://" + nxt[len("http://"):]
                current = nxt
                followed += 1
                first = False
                reply.close()
                continue

            if not 200 <= status < 300:
                raw, _ = _read(reply, MAX_ERROR_BODY * 4, deadline) if want != "head" else (b"", False)
                return _http_failure(vendor, status, lowered, raw, key)

            if want == "head":
                return {"ok": True, "status": status, "headers": lowered, "body": None, "final_url": current}
            raw, over = _read(reply, cap, deadline)
            if over and want != "bytes_prefix":
                return fail(TOO_BIG, "%s answered with more data than the %d MB limit." % (
                    _label(vendor), max(1, cap // (1024 * 1024))), status=status)
            if want == "bytes_prefix" or want == "bytes":
                body = raw
            elif want == "text":
                body = raw.decode(_charset(lowered), errors="replace")
            else:
                if status == 204 or not raw.strip():
                    body = None
                else:
                    try:
                        body = json.loads(raw.decode("utf-8-sig", errors="replace"))
                    except ValueError:
                        return fail(VENDOR_CHANGED, "%s answered, but not in the form Jarvis expects (it was not "
                                                    "JSON). The site may have changed." % _label(vendor),
                                    status=status)
            return {"ok": True, "status": status, "headers": lowered, "body": body, "final_url": current}
        except TimeoutError as exc:
            return fail(OFFLINE, "%s took too long to send its answer (%s)." % (_label(vendor), type(exc).__name__))
        finally:
            reply.close()


def _charset(headers):
    ctype = headers.get("content-type", "")
    match = re.search(r"charset=([A-Za-z0-9_\-]+)", ctype)
    if match:
        try:
            "".encode(match.group(1))
            return match.group(1)
        except LookupError:
            pass
    return "utf-8"


def _http_failure(vendor, status, headers, raw, key):
    label = _label(vendor)
    if key and isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).replace(key.encode("utf-8"), b"***")        # a vendor error page may echo what it was sent
    body = _short_body(raw)
    extra = {"status": status}
    if body not in ("", None):
        extra["body"] = body
    if status == 404:
        return fail(NOT_FOUND, "%s has no such page (HTTP 404)." % label, **extra)
    if status == 401 or (status == 403 and vendor == KEY_VENDOR):
        return fail(BAD_KEY, "%s refused the request (HTTP %d): the API key may be wrong, or this item is not "
                             "available to third-party tools." % (label, status), **extra)
    if 500 <= status < 600:
        return fail(OFFLINE, "%s is having trouble right now (HTTP %d). Try again later." % (label, status), **extra)
    return fail(VENDOR_CHANGED, _scrub("%s answered HTTP %d, which Jarvis does not expect here." % (label, status),
                                       key), **extra)
