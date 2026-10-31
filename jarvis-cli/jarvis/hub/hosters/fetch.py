"""The one way a resolver talks to the network: guarded, capped, and never following blindly.

  * every request AND every redirect hop goes through `netguard.check_url` first, so a host that answers with a
    redirect to `http://192.168.1.1/` (or a page that names such a link) is refused, not fetched;
  * redirects are followed by hand (and can be switched off: Buzzheavier answers in a header, not a Location);
    an Authorization or Cookie header is dropped when a hop changes host;
  * a body is read only up to `max_bytes`, and only when the response looks like text (HTML, JSON ...).  A
    resolver that probes a URL which turns out to be the FILE therefore never downloads it;
  * the session keeps cookies between the requests of ONE resolve (a flow needs them) but that jar is this
    object's own: nothing from it reaches the download unless the resolver puts it in `Resolved.headers`;
  * a dropped connection or a 5xx is a `Transient`, a layout surprise is a `ResolveFailed`; neither leaks the
    request's query string (tokens live there) into a message.
"""

import re
from urllib.parse import urljoin, urlsplit

from .. import netguard
from .base import Dead, NeedsHuman, QuotaExceeded, ResolveFailed, Transient

DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/140.0.0.0 Safari/537.36")
DEFAULT_MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)
_TEXTLIKE = ("text/", "application/json", "application/xhtml", "application/javascript",
             "application/x-javascript", "application/xml")
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.\-]+)", re.I)


def _textlike(ctype):
    return any(ctype.startswith(p) for p in _TEXTLIKE) or ctype.endswith(("+json", "+xml"))


def _host(url):
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


class Page:
    """One answer.  `body` is what was read (empty for a non-text response); `truncated` says it was cut or skipped."""

    def __init__(self, status, url, headers, body, truncated, method="GET"):
        self.status = int(status)
        self.url = url
        self.headers = headers
        self.body = body
        self.truncated = bool(truncated)
        self.method = method
        self._text = None

    @property
    def content_type(self):
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    @property
    def is_html(self):
        # Detect by content type, never by status code: a Drive 'virus scan warning' page is a 200.
        return self.content_type in ("text/html", "application/xhtml+xml")

    @property
    def is_text(self):
        """False for a response that is the FILE (an archive, a video): its body was not read."""
        ctype = self.content_type
        return not ctype or _textlike(ctype)

    @property
    def text(self):
        if self._text is None:
            charset = "utf-8"
            m = _CHARSET.search(self.headers.get("Content-Type") or "")
            if m:
                charset = m.group(1)
            try:
                self._text = self.body.decode(charset, errors="replace")
            except LookupError:
                self._text = self.body.decode("utf-8", errors="replace")
        return self._text

    def json(self):
        import json
        try:
            return json.loads(self.text)
        except ValueError:
            raise ResolveFailed("the answer was not the JSON this resolver expects")

    def retry_after(self, default=60):
        value = (self.headers.get("Retry-After") or "").strip()
        return int(value) if value.isdigit() and 0 < int(value) <= 86400 else default

    def require_ok(self, label):
        """The status mapping every resolver shares.  Returns self when the status is a success (2xx / 3xx)."""
        code = self.status
        if (self.headers.get("cf-mitigated") or "").lower() == "challenge":
            raise NeedsHuman("%s is showing a browser check (Cloudflare) before the page" % label, open_url=None)
        if code in (404, 410):
            raise Dead("%s says the file isn't there (HTTP %d): it was removed or the link is wrong" % (label, code))
        if code == 429:
            raise QuotaExceeded(self.retry_after(), "%s is rate-limiting this PC (HTTP 429)" % label)
        if code in (408, 425) or 500 <= code < 600:
            raise Transient("%s answered HTTP %d" % (label, code))
        if code >= 400:
            raise ResolveFailed("%s refused the request (HTTP %d)" % (label, code))
        return self


class Fetcher:
    def __init__(self, allow_private_hosts=(), user_agent=None, timeout=15.0, session=None, dns=None):
        self.allow = list(allow_private_hosts or [])
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = float(timeout)
        self._session = session
        self._dns = dns                     # (host, port) -> [addresses]; tests inject it so nothing touches DNS

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass

    def get(self, url, *, headers=None, follow=True, max_bytes=DEFAULT_MAX_BYTES):
        return self.request("GET", url, headers=headers, follow=follow, max_bytes=max_bytes)

    def post(self, url, *, data=None, json_body=None, headers=None, follow=True, max_bytes=DEFAULT_MAX_BYTES):
        return self.request("POST", url, headers=headers, data=data, json_body=json_body, follow=follow,
                            max_bytes=max_bytes)

    def request(self, method, url, *, headers=None, data=None, json_body=None, follow=True,
                max_bytes=DEFAULT_MAX_BYTES):
        import requests
        base = {"User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}
        hdrs = dict(base)
        for key, value in (headers or {}).items():
            if value is None:
                hdrs.pop(key, None)
            else:
                hdrs[key] = value
        verb, current = method.upper(), url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                netguard.check_url(current, self.allow, self._dns)
                resp = self.session.request(verb, current, headers=hdrs, data=data, json=json_body, stream=True,
                                            allow_redirects=False, timeout=self.timeout)
                location = resp.headers.get("Location")
                if follow and resp.status_code in _REDIRECTS and location:
                    resp.close()
                    nxt = urljoin(current, location)
                    if resp.status_code in (301, 302, 303) and verb == "POST":
                        verb, data, json_body = "GET", None, None
                    if _host(nxt) != _host(current):
                        for key in list(hdrs):
                            if key.lower() in ("authorization", "cookie"):
                                del hdrs[key]
                    current = nxt
                    continue
                return self._page(resp, current, verb, max_bytes)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as exc:
            # The message names the host only: a request's query string can hold a token.
            raise Transient("couldn't reach %s (%s)" % (_host(current) or "the host", type(exc).__name__))
        except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema):
            raise ResolveFailed("the link has a bad address")
        raise ResolveFailed("too many redirects from %s" % (_host(url) or "the host"))

    def _page(self, resp, url, method, max_bytes):
        try:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body, truncated = b"", False
            if not ctype or _textlike(ctype):
                chunks, total = [], 0
                for chunk in resp.iter_content(16384):
                    if not chunk:
                        continue
                    room = int(max_bytes) - total
                    if len(chunk) > room:
                        chunks.append(chunk[:max(0, room)])
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                body = b"".join(chunks)
            else:
                truncated = True            # a file: nothing of it is read, only its headers matter
            return Page(resp.status_code, url, resp.headers, body, truncated, method)
        finally:
            resp.close()
