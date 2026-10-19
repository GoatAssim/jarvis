"""A tiny fake of qBittorrent's Web API v2 for the Download Hub engine tests (not a test itself).

It speaks just enough of the real API for `hub/engines/qbittorrent.py`:

    POST auth/login                  user + password -> "Ok." and a SID cookie, or "Fails."
    GET  app/version, app/webapiVersion
    GET  torrents/info               ?hashes=...  or  ?tag=...
    GET  torrents/files              ?hash=...
    POST torrents/add                multipart: urls, savepath, tags, ...
    POST torrents/stop | start       (5.x)   404 on a 4.x fake
    POST torrents/pause | resume     (4.x)   404 on a 5.x fake
    POST torrents/delete             hashes, deleteFiles
    POST torrents/setDownloadLimit   hashes, limit

Like the real one it answers 403 without a session cookie and 401 when the Referer / Origin
doesn't match its own address (qBittorrent's CSRF check).

Knobs a test can turn on the `FakeQb` object:
    modern            True: stop/start exist (5.x); False: only pause/resume (4.x)
    bypass            True: no login needed ("bypass authentication for clients on localhost")
    add_answer        what torrents/add says ("Ok." or "Fails.");  add_creates: whether it really adds
    expire_session    the next authenticated call gets a 403 once (the session timed out)
    fail_info         answer HTTP 500 to the next N torrents/info calls
    on_info           callable(torrent) run for each torrent on every torrents/info (advances the simulation)
    log               [(method, route, params)] of every request;  deleted: [(hash, deleteFiles)]
"""

import http.server
import json
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

_BTIH = re.compile(r"urn:btih:([0-9a-fA-F]{40})")


def parse_multipart(body, content_type):
    match = re.search(r"boundary=([^;]+)", content_type or "")
    if not match:
        return {}
    boundary = match.group(1).strip().strip('"').encode()
    out = {}
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, value = chunk.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name:
            out[name.group(1).decode()] = value.decode("utf-8", "replace")
    return out


class FakeQb:
    def __init__(self, username="admin", password="secret", modern=True, version="v5.0.1"):
        self.username = username
        self.password = password
        self.modern = modern
        self.version = version
        self.bypass = False
        self.add_answer = "Ok."
        self.add_creates = True
        self.expire_session = False
        self.fail_info = 0
        self.on_info = None
        self.torrents = {}
        self.log = []
        self.deleted = []
        self.logins = 0
        self.session_ok = False
        self.sid = "SID-FAKE"
        self.port = 0
        self.server = None

    # -- helpers for tests -------------------------------------------------------
    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def calls(self, route):
        return [params for (_method, r, params) in self.log if r == route]

    def torrent(self, infohash, **fields):
        """Create (or update) a torrent the way qBittorrent would list it."""
        row = self.torrents.setdefault(infohash, {
            "hash": infohash, "name": infohash, "size": 0, "total_size": 0, "progress": 0.0,
            "completed": 0, "amount_left": 0, "dlspeed": 0, "eta": 8640000, "num_seeds": 0,
            "num_leechs": 0, "state": "metaDL", "save_path": "", "content_path": "", "tags": "",
            "category": "",
        })
        row.update(fields)
        return row

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def start(qb):
    """Serve `qb` on a free loopback port in a thread.  Returns qb (its `base` is then valid)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"      # one request per connection: a stopped server is really gone

        def log_message(self, *args):          # keep the test output quiet
            pass

        def _send(self, code, body="", ctype="text/plain; charset=utf-8", headers=None):
            data = body if isinstance(body, bytes) else str(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload):
            self._send(200, json.dumps(payload), "application/json")

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            parts = urlsplit(self.path)
            if not parts.path.startswith("/api/v2/"):
                return self._send(404, "Not Found")
            route = parts.path[len("/api/v2/"):]
            params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" in ctype:
                    params.update(parse_multipart(body, ctype))
                else:
                    params.update({k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()})
            qb.log.append((method, route, params))

            # the CSRF check: a Referer or Origin that doesn't match this server is refused
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if not origin.startswith(qb.base):
                return self._send(401, "Unauthorized")

            if route == "auth/login":
                if params.get("username") == qb.username and params.get("password") == qb.password:
                    qb.logins += 1
                    qb.session_ok = True
                    return self._send(200, "Ok.", headers={"Set-Cookie": "SID=%s; path=/" % qb.sid})
                return self._send(200, "Fails.")

            if not qb.bypass:
                if qb.expire_session:
                    qb.expire_session = False
                    qb.session_ok = False
                cookie = self.headers.get("Cookie", "")
                if not (qb.session_ok and ("SID=%s" % qb.sid) in cookie):
                    return self._send(403, "Forbidden")
            return self._route(route, params)

        def _route(self, route, params):
            if route == "app/version":
                return self._send(200, qb.version)
            if route == "app/webapiVersion":
                return self._send(200, "2.11.2")
            if route == "torrents/info":
                if qb.fail_info > 0:
                    qb.fail_info -= 1
                    return self._send(500, "boom")
                wanted = [h.lower() for h in params.get("hashes", "").split("|") if h] if "hashes" in params else None
                tag = params.get("tag")
                rows = []
                for infohash, row in list(qb.torrents.items()):
                    if wanted is not None and infohash not in wanted:
                        continue
                    if tag and tag not in [t.strip() for t in row.get("tags", "").split(",")]:
                        continue
                    if qb.on_info:
                        qb.on_info(row)
                    if infohash in qb.torrents:             # a tick may have removed it
                        rows.append(dict(row))
                return self._json(rows)
            if route == "torrents/files":
                row = qb.torrents.get(params.get("hash", "").lower())
                if not row:
                    return self._json([])
                return self._json([{"name": "show/ep1.mkv", "size": 600, "progress": row["progress"]},
                                   {"name": "show/ep2.mkv", "size": 400, "progress": row["progress"]}])
            if route == "torrents/add":
                match = _BTIH.search(params.get("urls", ""))
                if qb.add_creates and qb.add_answer == "Ok." and match:
                    infohash = match.group(1).lower()
                    dn = re.search(r"dn=([^&]+)", params.get("urls", ""))
                    qb.torrent(infohash, name=unquote(dn.group(1)) if dn else infohash, state="metaDL",
                               save_path=params.get("savepath", ""), tags=params.get("tags", ""))
                return self._send(200, qb.add_answer)
            if route in ("torrents/stop", "torrents/pause", "torrents/start", "torrents/resume"):
                if route in ("torrents/stop", "torrents/start") and not qb.modern:
                    return self._send(404, "Not Found")
                if route in ("torrents/pause", "torrents/resume") and qb.modern:
                    return self._send(404, "Not Found")
                stopping = route in ("torrents/stop", "torrents/pause")
                for infohash in params.get("hashes", "").lower().split("|"):
                    row = qb.torrents.get(infohash)
                    if not row:
                        continue
                    complete = row["progress"] >= 1.0
                    if stopping:
                        row["state"] = ("stoppedUP" if complete else "stoppedDL") if qb.modern \
                            else ("pausedUP" if complete else "pausedDL")
                    else:
                        row["state"] = "uploading" if complete else "downloading"
                return self._send(200, "")
            if route == "torrents/delete":
                for infohash in params.get("hashes", "").lower().split("|"):
                    if infohash in qb.torrents:
                        del qb.torrents[infohash]
                        qb.deleted.append((infohash, params.get("deleteFiles") == "true"))
                return self._send(200, "")
            if route == "torrents/setDownloadLimit":
                return self._send(200, "")
            return self._send(404, "Not Found")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    qb.server = server
    qb.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return qb
