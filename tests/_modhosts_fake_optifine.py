"""A fake optifine.net for the OptiFine tests (agent P6; not a test itself).  No sockets, no network.

It is a `fetch` with the interface contract's signature

    fetch(vendor, url, *, method="GET", params=None, json_body=None, headers=None,
          want="json", max_bytes=None, max_redirects=3)

so a test hands `fake.fetch` to the code under test exactly where `jarvis.modhosts.net.fetch` goes.  It behaves
like the real `net.fetch` in the ways that matter here:

  * vendor must be "optifine"; https only (an http URL is a failure, as in net.py: this is what proves the code
    upgrades the page's http links); only the host optifine.net (any other host is a failure AND is written to
    `STATE.bad_hosts`, so a test can assert that adfoc.us was never even attempted);
  * `max_redirects=0` returns a 3xx with its Location header instead of following it;
  * `want="bytes_prefix"` returns only the first `max_bytes` bytes, `want="bytes"` fails with reason `too_big` past
    `max_bytes`, `want="head"` returns no body;
  * success is `{"ok": True, "status", "headers" (lower-case names), "body", "final_url"}`, failure is
    `{"ok": False, "error", "reason"}`.

What it serves (the markup is the probe's, recorded 25 Oct 2026 against the live site; the rows are invented):

    /downloads   tests/data/modhosts/hand_p6_downloads.html (ASCII)   or `STATE.page` (bytes) when a test sets it
    /adloadx     the interstitial page hand_p6_adloadx.html with the tokenised anchor `downloadx?f=<file>&x=<TOKEN>`
    /downloadx   the jar (a real, valid ZIP built by make_jar) with Content-Disposition, no Content-Length

`STATE.log` has one entry per request (url, want, max_bytes, max_redirects, the fake time `t`); `STATE.modes` is a
set of switches a test turns on to make the site misbehave:

    hop302            the first /adloadx answers 302 to a relative Location (the redirect path is exercised)
    hop302_http       ... to an absolute http:// Location (must be upgraded to https)
    offsite_redirect  /adloadx answers 302 to adfoc.us (must be refused, never contacted)
    loop              /adloadx redirects to itself forever
    no_link           the interstitial has no downloadx anchor
    two_links         it has two DIFFERENT valid downloadx anchors
    bad_token         the anchor's x is not 32 hex characters
    other_file_link   the anchor's f names another file
    offsite_link      the anchor points at another host
    adloadx_is_jar    /adloadx itself answers with the jar
    html_jar          /downloadx answers 200 text/html
    jar_redirect_offsite   /downloadx answers 302 to adfoc.us
    bad_zip / tiny_jar / truncated   the bytes are not a good jar
    wrong_name        Content-Disposition names another file
    text_jar          the body comes back as str (a broken fetch)

`STATE.status` {path: code} forces a status; `STATE.fail` {path: failure dict} makes net fail; `STATE.raise_on` {path}
raises inside fetch; `STATE.errors_as_ok` True returns HTTP >= 400 as an ok dict with that status (net.py's
behaviour is not pinned by the contract, so the code under test is run both ways).
"""
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "modhosts"
PAGE_FILE = DATA / "hand_p6_downloads.html"
ADLOADX_FILE = DATA / "hand_p6_adloadx.html"
TOKEN = "8a585ba0511fc2f426b7805ecce211fd"
HOST = "optifine.net"


def make_jar(size=120_000, entries=None):
    """A real ZIP (stored entries, fixed timestamps: deterministic) of at least `size` bytes, shaped like an
    OptiFine jar (META-INF/MANIFEST.MF, optifine/Installer.class, padding)."""
    entries = entries or [("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\nMain-Class: optifine.Installer\r\n"),
                          ("optifine/Installer.class", b"\xca\xfe\xba\xbe" + b"\x00" * 64)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 25, 0, 0, 0))
            zf.writestr(info, data)
        pad = max(0, size - buf.tell() - 400)
        if pad:
            info = zipfile.ZipInfo("optifine/pad.bin", date_time=(2026, 10, 25, 0, 0, 0))
            zf.writestr(info, bytes((i * 7 + 3) % 251 for i in range(pad)))
    return buf.getvalue()


class Clock:
    """A fake clock: `sleep` advances it, `time` reads it.  Plug into optifine._clock / optifine._sleep."""

    def __init__(self, start=1000.0, per_request=0.01):
        self.now = float(start)
        self.slept = []
        self.per_request = per_request

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.log = []
        self.bad_hosts = []
        self.insecure = []
        self.page = PAGE_FILE.read_bytes()
        self.jar = make_jar()
        self.modes = set()
        self.status = {}
        self.fail = {}
        self.raise_on = set()
        self.errors_as_ok = False
        self.served_name = None
        self.clock = None
        self.hits = {}

    # ---- what the site knows
    def known(self):
        return {m.decode("ascii") for m in re.findall(rb"adloadx\?f=([A-Za-z0-9._-]+)", self.page)}

    def paths(self):
        return [urlsplit(e["url"]).path for e in self.log]

    def count(self, path):
        return sum(1 for p in self.paths() if p == path)

    # ---- the request
    def handle(self, vendor, url, want, max_bytes, max_redirects, depth=0):
        entry = {"vendor": vendor, "url": url, "want": want, "max_bytes": max_bytes, "max_redirects": max_redirects,
                 "t": self.clock.time() if self.clock else None}
        self.log.append(entry)
        if self.clock:
            self.clock.now += self.clock.per_request
        if vendor != "optifine":
            return {"ok": False, "error": "unknown vendor", "reason": "unsupported"}
        parts = urlsplit(url)
        if parts.scheme != "https":
            self.insecure.append(url)
            return {"ok": False, "error": "https only", "reason": "unsupported"}
        if (parts.hostname or "") != HOST:
            self.bad_hosts.append(url)
            return {"ok": False, "error": "host not on the allow-list", "reason": "unsupported"}
        path = parts.path
        if path in self.raise_on:
            raise RuntimeError("the fake connection blew up")
        if path in self.fail:
            return dict(self.fail[path])
        status, headers, body = self.route(parts)
        if path in self.status:
            status = self.status[path]
        if 300 <= status < 400 and max_redirects > 0 and depth < 5:
            from urllib.parse import urljoin
            return self.handle(vendor, urljoin(url, headers.get("location", "")), want, max_bytes, max_redirects - 1, depth + 1)
        if status >= 400 and not self.errors_as_ok:
            reason = "rate_limited" if status == 429 else "not_found" if status == 404 else "offline"
            return {"ok": False, "error": "HTTP %d" % status, "reason": reason}
        if want == "head":
            body = None
        elif want == "bytes_prefix":
            if isinstance(body, (bytes, bytearray)) and max_bytes:
                body = body[:max_bytes]
        elif want == "bytes":
            if max_bytes is not None and isinstance(body, (bytes, bytearray)) and len(body) > max_bytes:
                return {"ok": False, "error": "too big", "reason": "too_big"}
        elif want == "text":
            body = body.decode("latin-1") if isinstance(body, (bytes, bytearray)) else body
        else:
            return {"ok": False, "error": "want %r is not supported by this fake" % (want,), "reason": "unsupported"}
        if "text_jar" in self.modes and path == "/downloadx" and isinstance(body, (bytes, bytearray)):
            body = body.decode("latin-1")
        return {"ok": True, "status": status, "headers": headers, "body": body, "final_url": url}

    # ---- the routes
    def route(self, parts):
        path = parts.path
        q = parse_qs(parts.query)
        file_name = (q.get("f") or [""])[0]
        if path == "/downloads":
            return 200, {"content-type": "text/html;charset=ISO-8859-1"}, self.page
        if path == "/adloadx":
            hits = self.hits[file_name] = self.hits.get(file_name, 0) + 1
            if "offsite_redirect" in self.modes:
                return 302, {"location": "https://adfoc.us/serve/sitelinks/?id=475250&url=http://optifine.net/adloadx?f=%s" % file_name}, b""
            if "loop" in self.modes:
                return 302, {"location": "https://optifine.net/adloadx?f=%s" % file_name}, b""
            if "hop302" in self.modes and hits == 1:
                return 302, {"location": "/adloadx?f=%s&hop=1" % file_name}, b""
            if "hop302_http" in self.modes and hits == 1:
                return 302, {"location": "http://optifine.net/adloadx?f=%s&hop=1" % file_name}, b""
            if file_name not in self.known():
                return 404, {"content-type": "text/html"}, b"not found"
            if "adloadx_is_jar" in self.modes:
                return 200, {"content-type": "application/java-archive"}, self.jar
            return 200, {"content-type": "text/html;charset=ISO-8859-1"}, self.interstitial(file_name)
        if path == "/downloadx":
            if file_name not in self.known() or (q.get("x") or [""])[0] != TOKEN:
                return 404, {"content-type": "text/html"}, b"not found"
            if "jar_redirect_offsite" in self.modes:
                return 302, {"location": "https://adfoc.us/serve/download"}, b""
            if "html_jar" in self.modes:
                return 200, {"content-type": "text/html"}, b"<html><body>Please wait for your ad</body></html>" * 3000
            body = self.jar
            if "bad_zip" in self.modes:
                body = b"PK\x03\x04" + b"\x00" * 120_000
            elif "tiny_jar" in self.modes:
                body = make_jar(size=0)
            elif "truncated" in self.modes:
                body = self.jar[:-100]
            name = self.served_name or ("OptiFine_9.9.9_HD_U_X1.jar" if "wrong_name" in self.modes else file_name)
            return 200, {"content-type": "application/java-archive",
                         "content-disposition": 'attachment; filename="%s"' % name}, body
        return 404, {"content-type": "text/html"}, b"not found"

    def interstitial(self, file_name):
        text = ADLOADX_FILE.read_bytes().decode("latin-1")
        label = "OptiFine " + file_name[:-4].split("_", 2)[-1].replace("_", " ")
        x = "zz" * 16 if "bad_token" in self.modes else TOKEN
        f = file_name
        if "other_file_link" in self.modes:
            f = next(n for n in sorted(self.known()) if n != file_name and re.match(r"^[A-Za-z0-9._-]+$", n) and n.endswith(".jar"))
        page = text.replace("@FILE@", quote(f, safe="._-")).replace("@X@", x).replace("@LABEL@", label)
        if "no_link" in self.modes:
            page = page.replace("downloadx", "nothing-here")
        if "two_links" in self.modes:
            page = page.replace("</body>", "<a href='downloadx?f=%s&x=%s'>again</a></body>" % (quote(file_name, safe="._-"), "0" * 32))
        if "offsite_link" in self.modes:
            page = page.replace("href='downloadx?", "href='https://adfoc.us/downloadx?")
        return page.encode("latin-1")


STATE = State()


def reset():
    STATE.reset()
    return STATE


def fetch(vendor, url, *, method="GET", params=None, json_body=None, headers=None, want="json", max_bytes=None,
          max_redirects=3):
    """The contract's net.fetch, served by STATE."""
    return STATE.handle(vendor, url, want, max_bytes, max_redirects)
