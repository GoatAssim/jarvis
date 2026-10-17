"""A small local HTTP server the Download Hub tests download from (not a test itself).

Every file's bytes are a pure function of its size (`payload(n)`), so a test can check a
downloaded file without storing it.  Routes (query: size=N, delay=seconds per 32 KB chunk):

    /file.bin      Range-capable (206 + Content-Range), ETag "v1", Accept-Ranges: bytes
    /norange.bin   ignores Range and always answers 200 with the whole file
    /slow.bin      like file.bin but throttled by `delay` (for kill-and-resume tests)
    /redirect      302 -> /file.bin (same size)
    /redirect-host 302 -> http://localhost:<port>/file.bin  (a DIFFERENT host: credentials must drop)
    /redir-private 302 -> http://10.0.0.1/secret             (the guard must refuse the second hop)
    /cd            file.bin with a hostile Content-Disposition filename
    /missing       404
    /flaky         503 with Retry-After: 2 the first time, then 200
    /short         Content-Length 1000 but closes after 400 bytes
    /hang          sends half the body, then stalls for 3 s
    /nolength      200 with no Content-Length (the body ends when the connection closes)

`state.log` records (path, Range header, If-Range header) per request and `state.headers`
the full header dict, so tests can assert what the engine sent.
"""

import http.server
import threading
import time
from urllib.parse import parse_qs, urlsplit

CHUNK = 32 * 1024


def payload(n):
    return (bytes(range(256)) * (n // 256 + 1))[:n]


class State:
    def __init__(self):
        self.log = []
        self.headers = []
        self.flaky_calls = 0
        self.port = 0

    def reset(self):
        self.log.clear()
        self.headers.clear()
        self.flaky_calls = 0


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None

    def log_message(self, *args):
        pass

    def _write(self, data):
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _serve(self, size, ranges, delay=0.0, extra_headers=None):
        data = payload(size)
        rng = self.headers.get("Range")
        start, status = 0, 200
        if ranges and rng and rng.startswith("bytes="):
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
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"v1"')
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        for i in range(0, len(body), CHUNK):
            if not self._write(body[i:i + CHUNK]):
                return
            if delay:
                time.sleep(delay)

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path
        size = int(query.get("size", ["300000"])[0])
        delay = float(query.get("delay", ["0"])[0])
        st = self.state
        st.log.append((path, self.headers.get("Range"), self.headers.get("If-Range")))
        st.headers.append(dict(self.headers))

        if path == "/file.bin":
            return self._serve(size, True)
        if path == "/slow.bin":
            return self._serve(size, True, delay)
        if path == "/norange.bin":
            return self._serve(size, False)
        if path in ("/redirect", "/redirect-host", "/redir-private"):
            target = {"/redirect": "/file.bin?size=%d" % size,
                      "/redirect-host": "http://localhost:%d/file.bin?size=%d" % (st.port, size),
                      "/redir-private": "http://10.0.0.1/secret"}[path]
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/cd":
            return self._serve(size, True, extra_headers={
                "Content-Disposition": 'attachment; filename="..\\..\\evil.exe"'})
        if path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/flaky":
            st.flaky_calls += 1
            if st.flaky_calls == 1:
                self.send_response(503)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._serve(size, True)
        if path == "/short":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self._write(payload(400))
            self.close_connection = True
            return
        if path == "/hang":
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self._write(payload(size)[: size // 2])
            time.sleep(3)
            self.close_connection = True
            return
        if path == "/nolength":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self._write(payload(size))
            self.close_connection = True
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start():
    """Start a server on a free loopback port.  Returns (server, base_url, state)."""
    state = State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = Server(("127.0.0.1", 0), handler)
    state.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % state.port, state


def stop(server):
    try:
        server.shutdown()
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
