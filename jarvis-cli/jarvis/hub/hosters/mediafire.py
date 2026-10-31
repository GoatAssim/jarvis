"""MediaFire (`mediafire.com/file/<key>/<name>/file`).

Method (plan 4.2, scraping, confidence high): GET the page with a normal browser User-Agent and read the download
button, `<a id='downloadButton' href='https://download####.mediafire.com/...'>` (it also carries
`aria-label='Download file'`).  Community scrapers add two fallbacks, both kept: a regex for
`https://download<digits>.mediafire.com/...`, and a JavaScript variable named `khtmlurl` (the plan doesn't give its
syntax; the pattern here, `khtmlurl = "<url>"`, is a guess that only ever runs after the first two found nothing).

The file request must carry `Referer: <page url>` (one scraper reports 500 errors without it), and the link is
time-limited, so the engine resolving again on every attempt is the design, not a patch.

Not handled: folder links (`/folder/<key>`).  No source in the plan covers them, so they are refused by name rather
than guessed at.  Only the page host is claimed: `download123.mediafire.com` is already a file address and goes
straight to the HTTP engine.
"""

import html
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .base import ResolveFailed, Resolved, Resolver, Unsupported

_TAG = re.compile(r"<a\b[^>]*>", re.I)
_DOWNLOAD_HOST = re.compile(r"https?://download\d+\.mediafire\.com/[^\s\"'<>\\]+", re.I)
_KHTMLURL = re.compile(r"khtmlurl\s*[=:]\s*[\"'](https?:[^\"']+)[\"']", re.I)


def _attr(tag, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)')""" % re.escape(name), tag, re.I)
    if not m:
        return None
    return html.unescape(m.group(1) if m.group(1) is not None else m.group(2))


def _usable(href, base):
    """An href worth downloading: http(s) or protocol-relative, never javascript: or a bare #."""
    href = (href or "").strip()
    if not href or href.startswith("#") or href.lower().startswith(("javascript:", "data:", "mailto:")):
        return None
    if href.startswith("//"):
        href = "https:" + href
    href = urljoin(base, href)
    return href if urlsplit(href).scheme.lower() in ("http", "https") else None


def find_download_link(page_html, base_url):
    """The file address on a MediaFire page, or None.  Pure text work: no script is run."""
    tags = _TAG.findall(page_html)
    for wanted in (lambda t: (_attr(t, "id") or "") == "downloadButton",
                   lambda t: (_attr(t, "aria-label") or "").strip().lower() == "download file"):
        for tag in tags:
            if wanted(tag):
                link = _usable(_attr(tag, "href"), base_url)
                if link:
                    return link
    m = _DOWNLOAD_HOST.search(page_html)
    if m:
        return _usable(html.unescape(m.group(0)), base_url)
    m = _KHTMLURL.search(page_html)
    if m:
        return _usable(html.unescape(m.group(1)), base_url)
    return None


class MediaFire(Resolver):
    id = "mediafire"
    label = "MediaFire"
    hosts = ("mediafire.com",)
    method = "reads the download button on the page"
    confidence = "high"
    needs_human = "no"
    notes = "single files only; folder links aren't supported"
    source = "modde-sources (mediafire module); community scrapers"

    def resolve(self, url, ctx):
        parts = urlsplit(str(url).strip())
        if parts.path.lower().startswith("/folder"):
            raise Unsupported("MediaFire folder links aren't supported: the plan has no verified method for them. "
                              "Open the folder and give me the file links.")
        page_url = urlunsplit((parts.scheme or "https", parts.netloc, parts.path, parts.query, ""))
        ctx.check_stop()
        page = ctx.fetch.get(page_url)
        page.require_ok(self.label)
        file_headers = {"Referer": page_url, "User-Agent": ctx.user_agent}
        if not page.is_text:                     # the address was already the file
            return Resolved(page.url, headers=file_headers)
        link = find_download_link(page.text, page.url)
        if not link:
            raise ResolveFailed("couldn't find the download button on the MediaFire page: the file may have been "
                                "removed, or the page layout changed")
        return Resolved(link, headers=file_headers)


RESOLVER = MediaFire()
