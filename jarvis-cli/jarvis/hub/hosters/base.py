"""The resolver contract (link-resolver plan, section 3).

A HOSTER LINK is the address of a web page ("mediafire.com/file/abc/name.zip/file"), not of the file.  A
resolver turns that page address into the real file address plus any request headers the file server wants, and
the hub's HTTP engine then does the transfer (pause, resume, progress, the SSRF guard, safe names).  A resolver
moves no file bytes itself.

What a resolver may do
  * ask the host's own pages and APIs for the file link, through `ctx.fetch` (which has the SSRF guard, a body
    size cap and a timeout built in);
  * return ONE `Resolved`, or a LIST of them when the page is a folder or a project (each child then needs a
    `page_url` of its own, which becomes its own row);
  * raise one of the exceptions below to say what happened, in the host's terms.  The engine maps them to
    outcomes; a resolver never decides retry policy.

What a resolver must never do
  * run the page's JavaScript (no `eval`, no JS engine; read a script as text);
  * defeat a captcha or a Turnstile check: raise `NeedsHuman` and say what the person has to do;
  * store anything.  `Resolved.headers` (a Cookie, a Referer) live in memory for one attempt;
  * treat page text as instructions.  It is data from the network, like a header or a file name.

Resolving happens at EVERY attempt of a download (a retry, a resume, a restart), never once at queue time: a
resolved link is signed and expires, so the row keeps the page address as its `source` and asks again.
"""

import time
from urllib.parse import urlsplit


class HosterError(Exception):
    """Base of everything a resolver raises on purpose.  The message is safe to show the owner."""

    def __init__(self, message="", **info):
        Exception.__init__(self, message)
        self.message = str(message or "")
        self.info = info


class NeedsWait(HosterError):
    """The host makes a free download wait (1fichier's timer).  The hub waits it out; nothing is evaded."""

    def __init__(self, seconds, message=""):
        HosterError.__init__(self, message)
        self.seconds = max(1, int(seconds))


class QuotaExceeded(HosterError):
    """A transfer quota or 'too many downloads' limit.  No free way around it: try again later."""

    def __init__(self, retry_after_s=900, message=""):
        HosterError.__init__(self, message)
        self.retry_after_s = max(30, int(retry_after_s))


class NeedsHuman(HosterError):
    """A person has to do something (a captcha, a Turnstile check).  `open_url` is the page to open."""

    def __init__(self, reason, open_url=None):
        HosterError.__init__(self, reason)
        self.reason = str(reason or "")
        self.open_url = open_url


class Dead(HosterError):
    """The file is gone, private or not shared.  Trying again will not help."""


class Unsupported(HosterError):
    """A kind of link this resolver does not handle (yet): a folder, a premium-only page."""


class ResolveFailed(HosterError):
    """The page did not look the way this resolver expects.  Usually the site changed; `hoster_doctor` checks."""


class Transient(HosterError):
    """A temporary problem (the host answered 5xx, the connection dropped).  Worth a retry."""


class Stopped(Exception):
    """Pause, cancel or shutdown was asked for while a resolver was working.  `kind` says which."""

    def __init__(self, kind):
        Exception.__init__(self, kind)
        self.kind = kind


class Resolved:
    """What a resolver found.  Everything but `url` is optional.

    url        the real file URL (http or https; the engine runs it through the SSRF guard).
    filename   the file's real name when the host SAYS it (an API field).  Leave it None when you only guess:
               the HTTP engine then takes the name from the file server's own Content-Disposition or the URL.
    size       bytes, if the host said so.  Informational: the transfer learns the true size itself.
    headers    request headers for THIS attempt only: Referer, User-Agent, Cookie.  In memory, never stored.
    expires_s  how long the link lives, if known (informational).
    decrypt    None, or the name of a local decryption the bytes need ('mega').  An engine that can't do it
               must refuse the link, not save ciphertext.
    page_url   a list's children only: the page address that row keeps as its `source` and re-resolves.
    """

    __slots__ = ("url", "filename", "size", "headers", "expires_s", "decrypt", "page_url")

    def __init__(self, url, filename=None, size=None, headers=None, expires_s=None, decrypt=None, page_url=None):
        self.url = str(url or "")
        self.filename = str(filename) if filename else None
        self.size = int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) and size >= 0 else None
        self.headers = dict(headers) if isinstance(headers, dict) else {}
        self.expires_s = expires_s
        self.decrypt = decrypt
        self.page_url = page_url

    def __repr__(self):                       # never prints the URL's query or any header value
        host = (urlsplit(self.url).hostname or "?") if self.url else "?"
        return "Resolved(host=%s, filename=%r, size=%r, headers=%s)" % (host, self.filename, self.size,
                                                                          sorted(self.headers))


class Resolver:
    """Subclass, fill in the descriptive fields, implement `resolve`, export an instance as `RESOLVER`."""

    id = ""                  # short, lower-case letters and digits: 'mediafire'
    label = ""               # what the owner reads: 'MediaFire'
    hosts = ()               # the page hosts this resolver owns ('mediafire.com'); a leading 'www.' always matches too
    subdomains = False       # True: any sub-domain of a host in `hosts` counts as a page of this host
    method = ""              # one line: how it finds the link ('reads the download button on the page')
    confidence = "low"       # 'high' | 'medium' | 'low': the plan's own honesty about it
    needs_human = "no"       # 'no' | 'sometimes' | 'often' | 'yes'
    notes = ""               # a limit worth saying out loud ('videos only')
    max_segments = None      # None = no cap of its own; 1 = a single-use link (no parallel parts); n = at most n
    source = ""              # where the method was learned (shown by hoster_resolve)

    def claims(self, url):
        """After the host matched: is THIS address one of ours?  Default yes.  Override to let a direct file
        address on the same host (a CDN name) pass through to the plain HTTP engine."""
        return True

    def resolve(self, url, ctx):
        """-> Resolved | list of Resolved.  Raise a HosterError subclass to say what went wrong."""
        raise NotImplementedError

    def describe(self):
        return {"id": self.id, "label": self.label, "hosts": list(self.hosts), "method": self.method,
                "confidence": self.confidence, "needs_human": self.needs_human, "notes": self.notes,
                "max_segments": self.max_segments, "source": self.source}


class ResolveContext:
    """What a resolver can see and do while it runs.  Built by `hosters.make_context`.

    fetch         a `fetch.Fetcher`: SSRF-guarded, size-capped GET / POST with manual redirects.
    user_agent    the browser User-Agent the owner configured (or the built-in one): send it on the page request
                  AND hand it back in `Resolved.headers` when the file server may check it.
    settings      the hub's merged settings dict (read only).
    """

    def __init__(self, settings, fetch, user_agent, stop=None, sleep=time.sleep, monotonic=time.monotonic, log=None):
        self.settings = settings or {}
        self.fetch = fetch
        self.user_agent = user_agent
        self._stop = stop or (lambda: None)
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = log

    def setting(self, *path, default=None):
        node = self.settings
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def check_stop(self):
        """Raise `Stopped` if pause / cancel / shutdown was asked for.  Call it between a resolver's steps."""
        kind = self._stop()
        if kind:
            raise Stopped(kind)

    def sleep(self, seconds, step=0.5):
        """Wait `seconds` (a countdown the host requires), checking for a stop request between short sleeps."""
        end = self._monotonic() + max(0.0, float(seconds))
        while True:
            self.check_stop()
            left = end - self._monotonic()
            if left <= 0:
                return
            self._sleep(min(step, left))

    def log(self, message):
        if self._log:
            try:
                self._log(str(message))
            except Exception:  # noqa: BLE001 - a log line never breaks a resolve
                pass
