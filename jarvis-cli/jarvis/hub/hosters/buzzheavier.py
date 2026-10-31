"""Buzzheavier (`buzzheavier.com/<id>`, `bzzhr.co`, `bzzhr.to`, `fuckingfast.co`, `fuckingfast.net`).

Method (plan 4.4, reverse-engineered, confidence high): `GET <page-url>/download` with `Referer: <page-url>`.  The
direct file URL comes back in the `hx-redirect` RESPONSE HEADER, not in the body (it is an htmx redirect).  The
BuzzCopy userscript and the `bhdownloader` package both do exactly this; the URL is time-limited, which is why the
engine resolves again on every attempt.

Two details the plan marks as unconfirmed, handled so a wrong guess costs nothing:
  * if the header is missing the request is repeated ONCE with `HX-Request: true` (an htmx request header: a guess);
  * a plain 3xx `Location` is accepted as the file address too.

A link that is not `<host>/<id>` (a folder, the front page) is refused by name rather than guessed at.
"""

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .base import ResolveFailed, Resolved, Resolver, Unsupported

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Buzzheavier(Resolver):
    id = "buzzheavier"
    label = "Buzzheavier"
    hosts = ("buzzheavier.com", "bzzhr.co", "bzzhr.to", "fuckingfast.co", "fuckingfast.net")
    method = "asks <page>/download and reads the hx-redirect header"
    confidence = "high"
    needs_human = "no"
    source = "BuzzCopy userscript (Greasy Fork 535478); bhdownloader"

    def resolve(self, url, ctx):
        page_url = self._page_url(url)
        download = page_url + "/download"
        direct = None
        for attempt in (0, 1):
            headers = {"Referer": page_url}
            if attempt:
                headers["HX-Request"] = "true"
            ctx.check_stop()
            answer = ctx.fetch.get(download, headers=headers, follow=False, max_bytes=65536)
            answer.require_ok(self.label)
            direct = (answer.headers.get("hx-redirect") or "").strip()
            if not direct and 300 <= answer.status < 400:
                direct = (answer.headers.get("Location") or "").strip()
            if direct:
                direct = urljoin(answer.url, direct)
                break
        if not direct:
            raise ResolveFailed("Buzzheavier's answer had no file address (no hx-redirect header); the site may "
                                "have changed")
        if urlsplit(direct).scheme.lower() not in ("http", "https"):
            raise ResolveFailed("Buzzheavier gave an address that isn't http or https")
        return Resolved(direct, headers={"Referer": page_url, "User-Agent": ctx.user_agent})

    @staticmethod
    def _page_url(url):
        try:
            parts = urlsplit(str(url).strip())
        except ValueError:
            raise Unsupported("that isn't a usable Buzzheavier link")
        path = parts.path.rstrip("/")
        if path.endswith("/download"):
            path = path[: -len("/download")]
        segments = [s for s in path.split("/") if s]
        if len(segments) != 1 or not _ID.match(segments[0]):
            raise Unsupported("that Buzzheavier address isn't a single-file link (<host>/<id>): a folder or another "
                              "page isn't supported")
        return urlunsplit((parts.scheme or "https", parts.netloc, "/" + segments[0], "", ""))


RESOLVER = Buzzheavier()
