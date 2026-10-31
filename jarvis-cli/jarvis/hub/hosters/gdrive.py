"""Google Drive files (`drive.google.com/file/d/<id>/...`, `open?id=`, `uc?id=`, `drive.usercontent.google.com/download?id=`).

Method (plan 4.3, semi-official, confidence high): request
`https://drive.usercontent.google.com/download?id=<id>&export=download&authuser=0&confirm=t`.  For a big file Google
may still answer with an HTML "virus scan warning" page.  That page has a form `id='download-form'` pointing at
`drive.usercontent.google.com/download` with hidden inputs (`id`, `export`, `confirm`, `uuid`); submit it as a GET
(gdown pull request 308).  The page is recognised by its CONTENT TYPE, never by its status code.

Each probe reads only the headers when the answer is the file (the fetcher never reads a non-text body), so
nothing is downloaded here: the engine fetches the file itself, from the same address.

Limits, said plainly:
  * "too many users viewed or downloaded this file" has no free fix.  It is a `QuotaExceeded`: the hub tries
    again later.  The phrases it is recognised by are plain text matches (the plan names the condition, not the
    wording), so a rewording by Google would show as "couldn't understand the page" instead.
  * Folders are not handled (the plan's route is the optional `gdown` package, not built); they are refused by name.
  * The Drive API-key route the plan mentions as optional is not built.
"""

import html
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .. import naming
from .base import Dead, QuotaExceeded, ResolveFailed, Resolved, Resolver, Transient, Unsupported

# Module level so the tests can point it at a local fake; the real value is the one in the plan.
DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
_ID = r"[A-Za-z0-9_-]{6,128}"
_FILE_PATH = re.compile(r"/file/(?:u/\d+/)?d/(%s)(?:/|$)" % _ID)
_FORM = re.compile(r"<form\b([^>]*\bid\s*=\s*[\"']download-form[\"'][^>]*)>(.*?)</form>", re.I | re.S)
_INPUT = re.compile(r"<input\b[^>]*>", re.I)
_QUOTA = re.compile(r"too many (?:users|people)|viewed or downloaded this file recently|"
                    r"download quota (?:for this file )?(?:has been )?exceeded", re.I)
_PROBE_BYTES = 262144


def _attr(tag, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)')""" % re.escape(name), tag, re.I)
    if not m:
        return None
    return html.unescape(m.group(1) if m.group(1) is not None else m.group(2))


def file_id_of(url):
    """The file id in any of the link shapes, or raises Unsupported (folders, Docs, the Drive home page)."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        raise Unsupported("that isn't a usable Google Drive link")
    path = parts.path
    if "/folders/" in path or path.startswith("/drive/folders") or path.startswith("/folders"):
        raise Unsupported("Google Drive folders aren't supported yet (single files only); give me the file links")
    m = _FILE_PATH.search(path)
    if m:
        return m.group(1)
    query = parse_qs(parts.query)
    if path in ("/open", "/uc", "/download") and query.get("id"):
        value = query["id"][0]
        if re.fullmatch(_ID, value):
            return value
    raise Unsupported("that Google Drive address isn't a file link I know (file/d/<id>, open?id=, uc?id=)")


def parse_confirm_form(page_html, page_url):
    """The warning page's `download-form` -> the address to GET, or None.  The action must stay on the page's own host and scheme."""
    m = _FORM.search(page_html)
    if not m:
        return None
    action = _attr(m.group(1), "action")
    if not action:
        return None
    target = urljoin(page_url, action)
    there, here = urlsplit(target), urlsplit(page_url)
    # Same host and same scheme as the page the form came from: never a hop to another site, never a downgrade.
    if there.scheme.lower() not in ("http", "https") or there.scheme.lower() != here.scheme.lower() \
            or (there.hostname or "").lower() != (here.hostname or "").lower():
        return None
    fields = []
    for tag in _INPUT.findall(m.group(2)):
        name = _attr(tag, "name")
        if name and (_attr(tag, "type") or "hidden").lower() == "hidden":
            fields.append((name, _attr(tag, "value") or ""))
    if not fields:
        return None
    base = target.split("?", 1)[0]
    return base + "?" + urlencode(fields)


class GoogleDrive(Resolver):
    id = "gdrive"
    label = "Google Drive"
    hosts = ("drive.google.com", "drive.usercontent.google.com")
    method = "downloads through drive.usercontent.google.com and answers its confirm form"
    confidence = "high"
    needs_human = "no"
    notes = "single files only; a 'too many users' limit can only be waited out"
    source = "gdown (pull request 308, README); modde-sources (gdrive module)"

    def resolve(self, url, ctx):
        file_id = file_id_of(url)
        probe = DOWNLOAD_URL + "?" + urlencode([("id", file_id), ("export", "download"), ("authuser", "0"),
                                                ("confirm", "t")])
        found = self._probe(ctx, probe)
        if isinstance(found, str):                       # the warning page: answer its form, once
            found = self._probe(ctx, found, allow_form=False)
        return found

    def _probe(self, ctx, address, allow_form=True):
        ctx.check_stop()
        page = ctx.fetch.get(address, max_bytes=_PROBE_BYTES)
        if not page.is_html:
            if page.status in (200, 206):
                return self._as_file(address, page)
            page.require_ok(self.label)                   # 404 -> Dead, 429 -> Quota, 5xx -> Transient ...
            raise ResolveFailed("Google Drive answered HTTP %d without a file" % page.status)
        text = page.text
        if _QUOTA.search(text):
            raise QuotaExceeded(3600, "Google Drive says too many people viewed or downloaded this file recently; "
                                      "the hub tries again later")
        if allow_form:
            nxt = parse_confirm_form(text, page.url)
            if nxt:
                return nxt
        if page.status in (404, 410):
            raise Dead("Google Drive says the file isn't there (HTTP %d): it was removed or the link is wrong"
                       % page.status)
        if page.status in (401, 403):
            raise Dead("Google Drive refuses the file (HTTP %d): it isn't shared with 'anyone with the link', "
                       "or it was removed" % page.status)
        if page.status == 429:
            raise QuotaExceeded(page.retry_after(900), "Google Drive is rate-limiting this PC (HTTP 429)")
        if page.status >= 500:
            raise Transient("Google Drive answered HTTP %d" % page.status)
        raise ResolveFailed("couldn't understand Google Drive's page: no download form and no file; "
                            "the page layout may have changed")

    @staticmethod
    def _as_file(address, page):
        name = naming.filename_from_content_disposition(page.headers.get("Content-Disposition"))
        length = (page.headers.get("Content-Length") or "").strip()
        return Resolved(address, filename=name, size=int(length) if length.isdigit() else None)


RESOLVER = GoogleDrive()
