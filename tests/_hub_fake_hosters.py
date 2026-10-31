"""A local stand-in for the file hosts the resolver tests talk to (not a test itself).

It extends the Download Hub's shared test server (`_hub_testserver`: Range, ETag, payloads that are a pure function
of their size) with the routes of three hosts, behaving the way the link-resolver plan DESCRIBES them.  It is a
description, not a recording: see tests/fixtures/hosters/README.md.  All three live on one host (127.0.0.1), so a
test names the resolver it wants with `hosters.extra_hosts` or a row's `options.resolver`.

Buzzheavier  (page = /<id>, answer = a header)
    /<id>/download   needs `Referer: <base>/<id>`; answers 200 with `hx-redirect: <file url>` and no body.
      goodfile  relative hx-redirect                 absolute  absolute hx-redirect
      needhx    header only when `HX-Request: true`  nohdr     never a header (the layout changed)
      locmode   302 + Location instead of the header gone      404
      busy      429 with Retry-After: 120            err500    503
    /fdl/<id>        the file (Range-capable); needs a Referer naming /<id>, else 403.
      flaky     403 on its first request only (an expired link), then fine.

MediaFire  (page = /file/<key>/<name>/file)
    keys: good (a downloadButton anchor), nobutton, scrambled (a dead #-button first), gone (404)
    /mfdl/<key>/<name>   the file; needs `Referer: <base>/file/<key>/`, else 500 (as one scraper reports).

Google Drive  (/download?id=<id>&export=download&authuser=0&confirm=t)
    smallfileid1  the file at once, with a Content-Disposition name
    bigfileid001  an HTML warning page with the download-form; the form's GET (with a uuid) gives the file
    loopformid01  the warning page again after the form: the resolver must give up, not loop
    quotaid0001   HTML 200 saying too many users            forbidden001  HTML 403
    missing0001   HTML 404                                  surprise0001  HTML 200 nobody expects

Plus /needcookie.bin (the file only with `Cookie: sid=s3cret`) and /echo-headers (a JSON dump of what it was sent).
`state.reqs` is [(path+query, headers)] for every request, so a test can assert exactly what a resolver sent.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import _hub_testserver as base

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hosters"
SIZES = {"goodfile": 200_000, "absolute": 120_000, "needhx": 90_000, "locmode": 80_000, "flaky": 150_000,
         "smallfileid1": 150_000, "bigfileid001": 700_000}
MF_FILES = {"good": 220_000, "scrambled": 60_000}


def fixture(rel, **subs):
    text = (FIXTURES / rel).read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace("{{%s}}" % key, str(value))
    return text


class HosterState(base.State):
    def __init__(self):
        base.State.__init__(self)
        self.reqs = []
        self.fdl_hits = {}

    def reset(self):
        base.State.reset(self)
        self.reqs.clear()
        self.fdl_hits.clear()

    def paths(self):
        return [p for p, _ in self.reqs]


class Handler(base.Handler):
    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self._write(body)

    def _base(self):
        return "http://127.0.0.1:%d" % self.state.port

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        st, root = self.state, self._base()
        st.reqs.append((self.path, dict(self.headers)))
        referer = self.headers.get("Referer") or ""

        m = re.fullmatch(r"/([A-Za-z0-9_-]+)/download", path)
        if m:                                                       # ---- Buzzheavier answer
            fid = m.group(1)
            if fid == "gone":
                return self._send(404, "not found")
            if fid == "busy":
                return self._send(429, "slow down", headers={"Retry-After": "120"})
            if fid == "err500":
                return self._send(503, "try later")
            if referer != "%s/%s" % (root, fid):
                return self._send(403, "bad referer")
            if fid == "nohdr":
                return self._send(200, "<html>ok</html>")
            if fid == "needhx" and self.headers.get("HX-Request") != "true":
                return self._send(200, "<html>ok</html>")
            target = "/fdl/%s?t=%d" % (fid, len(st.reqs))
            if fid == "locmode":
                return self._send(302, "", headers={"Location": target})
            if fid == "absolute":
                target = root + target
            return self._send(200, "", headers={"hx-redirect": target})
        m = re.fullmatch(r"/fdl/([A-Za-z0-9_-]+)", path)
        if m:                                                       # ---- Buzzheavier file
            fid = m.group(1)
            if "/%s" % fid not in referer:
                return self._send(403, "no referer")
            st.fdl_hits[fid] = st.fdl_hits.get(fid, 0) + 1
            if fid == "flaky" and st.fdl_hits[fid] == 1:
                return self._send(403, "link expired")
            return self._serve(SIZES.get(fid, 100_000), True)

        m = re.fullmatch(r"/file/([a-z]+)/([^/]+)/file", path)
        if m:                                                       # ---- MediaFire page
            key, name = m.groups()
            if key == "gone":
                return self._send(404, "gone")
            page = {"good": "mediafire/page_button.html", "nobutton": "mediafire/page_nobutton.html",
                    "scrambled": "mediafire/page_scrambled_button.html"}.get(key)
            if not page:
                return self._send(404, "unknown key")
            return self._send(200, fixture(page, BASE=root, KEY=key, NAME=name))
        m = re.fullmatch(r"/mfdl/([a-z]+)/([^/]+)", path)
        if m:                                                       # ---- MediaFire file
            key = m.group(1)
            if not referer.startswith("%s/file/%s/" % (root, key)):
                return self._send(500, "no referer")
            return self._serve(MF_FILES.get(key, 100_000), True)

        if path == "/download":                                     # ---- Google Drive
            fid = (query.get("id") or [""])[0]
            if fid == "quotaid0001":
                return self._send(200, fixture("gdrive/quota.html"))
            if fid == "forbidden001":
                return self._send(403, fixture("gdrive/notfound.html"))
            if fid == "missing0001":
                return self._send(404, fixture("gdrive/notfound.html"))
            if fid == "surprise0001":
                return self._send(200, fixture("gdrive/surprise.html"))
            if fid == "smallfileid1":
                return self._serve(SIZES[fid], True, extra_headers={
                    "Content-Disposition": 'attachment; filename="gdrive file.bin"'})
            if fid in ("bigfileid001", "loopformid01"):
                if fid == "bigfileid001" and (query.get("uuid") or [""])[0] == "u-123":
                    return self._serve(SIZES[fid], True, extra_headers={
                        "Content-Disposition": "attachment; filename*=UTF-8''big%20archive.bin"})
                return self._send(200, fixture("gdrive/warning.html", BASE=root, ID=fid, UUID="u-123"))
            return self._send(404, fixture("gdrive/notfound.html"))

        if path == "/needcookie.bin":
            if "sid=s3cret" not in (self.headers.get("Cookie") or ""):
                return self._send(403, "cookie please")
            return self._serve(int((query.get("size") or ["70000"])[0]), True)
        if path == "/echo-headers":
            return self._send(200, json.dumps(dict(self.headers)), ctype="application/json")
        return base.Handler.do_GET(self)


class Server(base.Server):
    def handle_error(self, request, client_address):
        pass            # a resolver that only wants the headers closes the connection early: not an error here


def start():
    """Start on a free loopback port.  Returns (server, base_url, state)."""
    import threading
    state = HosterState()
    handler = type("BoundHosterHandler", (Handler,), {"state": state})
    server = Server(("127.0.0.1", 0), handler)
    state.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % state.port, state


stop = base.stop
