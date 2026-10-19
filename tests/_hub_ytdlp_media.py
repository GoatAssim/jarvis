"""Helpers for the yt-dlp Level B end-to-end tests (not a test itself).

Everything runs on this machine, no network: a small HTTP server that serves real media files
(made with ffmpeg) with Range support and an optional speed limit, and a yt-dlp *plugin extractor*
that answers `http://127.0.0.1:<port>/hubtest/<id>` with SEPARATE video-only and audio-only
formats, the way YouTube does.  yt-dlp then downloads two files and merges them with ffmpeg, which
is exactly the flow that makes a naive per-file progress bar reach 100 % twice and then freeze.

The plugin is loaded by the worker process through PYTHONPATH (yt-dlp finds a `yt_dlp_plugins`
folder on sys.path), so the real engine, the real worker, real yt-dlp and real ffmpeg are all
in the loop.

URL query for /hubtest/<id>:
    v=<bytes>     the video stream's exact size (filesize)
    a=<bytes>     the audio stream's size
    approx=1      announce the audio size as `filesize_approx` (an estimate) instead
    bps=<n>       serve both media files at about n bytes per second
    only=video    offer just the video stream (one file, no merge)
"""

import http.server
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

PLUGIN_SOURCE = '''
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.common import InfoExtractor


class HubTestIE(InfoExtractor):
    IE_NAME = "hubtest"
    _VALID_URL = r"https?://[^/]+/hubtest/(?P<id>[\\w-]+)"

    def _real_extract(self, url):
        vid = self._match_id(url)
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        base = "%s://%s" % (parts.scheme, parts.netloc)
        bps = q.get("bps")
        suffix = ("?bps=%s" % bps) if bps else ""
        formats = [{
            "format_id": "v1", "url": base + "/media/video.mp4" + suffix, "ext": "mp4",
            "vcodec": "mpeg4", "acodec": "none", "height": 240, "width": 320,
            "filesize": int(q["v"]) if q.get("v") else None,
        }]
        if q.get("only") != "video":
            audio = {"format_id": "a1", "url": base + "/media/audio.m4a" + suffix, "ext": "m4a",
                     "vcodec": "none", "acodec": "aac", "abr": 128}
            if q.get("a"):
                audio["filesize_approx" if q.get("approx") else "filesize"] = int(q["a"])
            formats.append(audio)
        return {"id": vid, "title": "Hub Test " + vid, "formats": formats, "duration": 4}
'''


def have_ffmpeg():
    return bool(shutil.which("ffmpeg"))


def make_media(folder):
    """video.mp4 (video only) and audio.m4a (audio only) in `folder`; returns their sizes."""
    os.makedirs(folder, exist_ok=True)
    video, audio = os.path.join(folder, "video.mp4"), os.path.join(folder, "audio.m4a")
    if not os.path.exists(video):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                        "-t", "4", "-an", "-c:v", "mpeg4", "-b:v", "3M", "-pix_fmt", "yuv420p", video], check=True)
    if not os.path.exists(audio):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                        "-t", "4", "-vn", "-c:a", "aac", "-b:a", "256k", audio], check=True)
    return os.path.getsize(video), os.path.getsize(audio)


def write_plugin(root):
    """Create <root>/yt_dlp_plugins/extractor/hubtest.py; put `root` on PYTHONPATH."""
    folder = os.path.join(root, "yt_dlp_plugins", "extractor")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "hubtest.py"), "w", encoding="utf-8") as fh:
        fh.write(PLUGIN_SOURCE)
    return root


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    folder = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        self.server.requests.append((parts.path, self.headers.get("Range")))
        m = re.match(r"^/media/([\w.\-]+)$", parts.path)
        path = os.path.join(self.folder, m.group(1)) if m else None
        if not path or not os.path.isfile(path):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(path)
        start, status = 0, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        bps = float(q.get("bps", ["0"])[0] or 0)
        with open(path, "rb") as fh:
            fh.seek(start)
            body = fh.read()
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4" if path.endswith(".mp4") else "audio/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        self.end_headers()
        chunk = 16 * 1024
        for i in range(0, len(body), chunk):
            try:
                self.wfile.write(body[i:i + chunk])
                self.wfile.flush()
            except OSError:
                return
            if bps:
                time.sleep(chunk / bps)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_media_server(folder):
    """Serve /media/<file> from `folder`.  Returns (server, base_url); `server.requests` logs them."""
    handler = type("BoundHandler", (_Handler,), {"folder": folder})
    server = _Server(("127.0.0.1", 0), handler)
    server.requests = []                 # (path, Range header) of every request, for the tests
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def stop_media_server(server):
    try:
        server.shutdown()
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
