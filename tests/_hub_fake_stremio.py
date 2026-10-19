"""A fake Stremio streaming server for the hub tests (not a test itself).

Speaks the parts of the real server the owner's 2026-10-18 experiments confirmed:

    GET  /settings                          {"options": [], "values": {...serverVersion, cacheRoot, cacheSize ...}}
    POST /settings                          replaces `values` (unless state.refuse_settings)
    POST /<hash>/create                     registers; answers the stats ("dead" torrents answer files: null)
    GET  /<hash>/stats.json                 the stats, or JSON null when the hash isn't registered
    GET  /stats.json                        {hash: stats} for every registered torrent
    GET  /<hash>/<idx>?external=1&download=1   307 -> /<hash>/<urlquoted name>?download=1
    GET  /<hash>/<name>?download=1          200 / 206 with Content-Disposition: attachment; filename="..";
    GET  /<hash>/<idx>                      the streaming URL: 200 / 206, no Content-Disposition
    POST /create {"blob": hex}              registers a .torrent (answers infoHash)

Torrents are described by `state.torrents[hash] = {"files": [(path, size), ...], "dead": bool}`.  File
bytes are `payload(size)` from _hub_testserver, so a test can check a download without storing it.
`state.cut_after` (bytes) makes the first download request close the connection after that many
bytes (a dropped connection); `state.stall` makes it send half the body and then stall.
`state.log` records (method, path, Range) for every request, `state.registered` the hashes created.
"""

import http.server
import json
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from _hub_testserver import payload


class State:
    def __init__(self):
        self.torrents = {}
        self.registered = []
        self.log = []
        self.settings = {"serverVersion": "4.21.2", "cacheRoot": "", "cacheSize": 2147483648,
                         "btMaxConnections": 55, "btDownloadSpeedSoftLimit": 2621440,
                         "btDownloadSpeedHardLimit": 3670016}
        self.refuse_settings = False
        self.cut_after = None
        self.stall = False
        self.blobs = []
        self.blob_hash = "ab" * 20
        self.forget_all = False

    def reset(self):
        self.__init__()

    def stats_for(self, digest):
        spec = self.torrents.get(digest)
        if digest not in self.registered or spec is None:
            return None
        if spec.get("dead"):
            return {"infoHash": digest, "name": None, "files": None, "peers": 0, "downloaded": 0,
                    "downloadSpeed": 0, "peerSearchRunning": True}
        files, offset = [], 0
        for path, size in spec["files"]:
            files.append({"path": path, "name": path.split("/")[-1], "length": size, "offset": offset})
            offset += size
        return {"infoHash": digest, "name": spec.get("name", "Torrent"), "files": files, "peers": 7,
                "downloaded": 0, "downloadSpeed": spec.get("speed", 1000), "uploadSpeed": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_POST(self):
        st = self.state
        st.log.append(("POST", self.path, None))
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if parts == ["settings"]:
            data = self._body()
            if not st.refuse_settings:
                st.settings = dict(data)
            return self._json({"success": True})
        if parts == ["create"]:
            body = self._body()
            st.blobs.append(body.get("blob"))
            digest = st.blob_hash
            st.torrents.setdefault(digest, {"files": [("blob/file.mp4", 5000)]})
            st.registered.append(digest)
            return self._json(st.stats_for(digest))
        if len(parts) == 2 and parts[1] == "create":
            digest = parts[0]
            self._body()
            if digest in st.torrents:
                st.registered.append(digest)
            return self._json(st.stats_for(digest) or {"infoHash": digest, "files": None})
        self.send_error(404)

    def do_GET(self):
        st = self.state
        split = urlsplit(self.path)
        parts = [unquote(p) for p in split.path.split("/") if p]
        st.log.append(("GET", self.path, self.headers.get("Range")))
        if parts == ["settings"]:
            return self._json({"options": [], "values": dict(st.settings), "baseUrl": "http://192.168.1.5:11470"})
        if parts == ["t.torrent"]:
            body = b"d4:infod6:lengthi5000eee"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts == ["stats.json"]:
            return self._json({h: st.stats_for(h) for h in set(st.registered) if st.stats_for(h)})
        if len(parts) == 2 and parts[1] == "stats.json":
            return self._json(st.stats_for(parts[0]))
        if len(parts) == 2 and parts[0] in st.torrents and parts[0] in st.registered:
            spec = st.torrents[parts[0]]
            if spec.get("dead"):
                time.sleep(3)
                return
            files = spec["files"]
            token = parts[1]
            if token.lstrip("-").isdigit():
                idx = int(token)
                if idx == -1:
                    idx = max(range(len(files)), key=lambda i: files[i][1])
                if "download" in split.query:
                    self.send_response(307)
                    self.send_header("Location", "/%s/%s?download=1" % (parts[0], quote(files[idx][0].split("/")[-1])))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._serve(files[idx], attachment=False)
            for entry in files:
                if entry[0].split("/")[-1] == token:
                    return self._serve(entry, attachment=True)
        self.send_error(404)

    def _serve(self, entry, attachment):
        st = self.state
        name, size = entry[0].split("/")[-1], entry[1]
        data = payload(size)
        rng = self.headers.get("Range")
        start, status = 0, 200
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        body = data[start:]
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="%s";' % name)
        self.end_headers()
        try:
            if st.cut_after is not None and start == 0:
                cut, st.cut_after = st.cut_after, None
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.close_connection = True
                self.connection.close()
                return
            if st.stall and start == 0:
                half = len(body) // 2
                self.wfile.write(body[:half])
                self.wfile.flush()
                time.sleep(4)
                return
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def start():
    state = State()
    handler = type("H", (Handler,), {"state": state})
    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):      # dropped connections are the point of some tests
            pass

    server = Quiet(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1], state


def stop(server):
    server.shutdown()
    server.server_close()
