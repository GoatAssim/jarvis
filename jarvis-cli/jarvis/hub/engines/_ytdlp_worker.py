"""The process the yt-dlp engine starts for one download (plan 7.1, Level B).

    python -m jarvis.hub.engines._ytdlp_worker        (arguments as one JSON object on stdin)

It runs the download through `ytdl_tools.download_with_hooks`, i.e. the SAME option builder and
the same code as the `ytdl_download` tool, and talks to the engine on stdout, one line each:

    @@JARVIS_YTDL_PROGRESS@@ {json}     any number of times: what yt-dlp's hooks report
    @@JARVIS_YTDL_RESULT@@ {json}       exactly once, last: `ytdl_download`'s result

Progress lines are throttled (yt-dlp's hook fires every few KB) except for changes of file and of
status, which always go out.  They carry the file name, bytes done, the file's exact or estimated
size, speed and ETA.  One `plan` line goes out first, before any bytes move: the files yt-dlp
selected and their announced sizes, so the engine can show ONE bar for "video + audio" instead of
two that each reach 100 %.  Post-processing (merge / extract audio / embed ...) is reported
separately, so the row can say what it is doing instead of sitting at 100 %.

STOPPING.  The engine asks for a clean stop by creating `.jarvis-ytdl-stop` in this download's
private folder (the file says `pause` or `cancel`).  The progress hook sees it and raises, which
makes yt-dlp stop at once and leave its `.part` file for the next attempt.  That is the polite
way; the engine still kills the process tree when the worker doesn't answer in time (a hook only
runs while bytes move), and it never relies on this for post-processing.

TWO OTHER JOBS, chosen by the arguments (brief 21):

  * `{"op": "probe", "url": ..., "max_entries": N}` LOOKS at a playlist and downloads nothing: flat
    extraction (titles and links only), at most N entries (never more than 100), answered as one
    result line: `{"ok", "is_playlist", "title", "count", "more", "entries": [{"title", "url"}]}`;
  * `"generic": true` in a download's arguments means the owner allowed yt-dlp's generic extractor
    for this one link.  yt-dlp then fetches pages it knows nothing about and follows their
    redirects itself, which `hub/netguard.py` cannot see, so in this process only (it dies with
    the download): every socket connection to a private, loopback, link-local or otherwise
    non-public address is refused, on every hop, and a stream that needs another program to
    fetch it (live HLS, RTSP, MMS, RTMP: ffmpeg or rtmpdump connect on their own, outside this
    guard) is refused before any byte moves.

A pipe protocol and not a thread: a hub download must be killable (see engines/ytdlp.py), and yt-dlp
runs in this process only.  Anything yt-dlp prints is moved to stderr so it can never be mistaken
for a protocol line.

This module starts with an underscore on purpose: engines/__init__.py skips it, so it is never
mistaken for an engine.  Nothing else imports it.
"""

import json
import os
import re
import sys
import threading
import time

MARKER = "@@JARVIS_YTDL_RESULT@@"
PROGRESS_MARKER = "@@JARVIS_YTDL_PROGRESS@@"
STOP_FILE = ".jarvis-ytdl-stop"
EMIT_EVERY = 0.4            # seconds between progress lines for the same file and status
STOP_CHECK_EVERY = 0.1      # seconds between looks for the stop file

_proto = sys.stdout         # the real stdout; yt-dlp's own output is moved to stderr in main()
_lock = threading.Lock()


def _emit(marker, payload):
    try:
        line = json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001 - a report that can't be encoded is simply dropped
        return
    with _lock:
        try:
            # the leading newline ends any half line yt-dlp left behind (it draws with "\r")
            _proto.write("\n%s %s\n" % (marker, line))
            _proto.flush()
        except (OSError, ValueError):
            pass


PLAN_PP_NAME = "JarvisPlan"      # the reporting-only post-processor in ytdl_tools; never shown


def _stream_list(info):
    """The files yt-dlp is going to fetch for this video: `requested_formats` (video + audio),
    or the video itself when it is one file."""
    formats = (info or {}).get("requested_formats") or [info or {}]
    out = []
    for f in formats[:8]:
        if not isinstance(f, dict):
            continue
        out.append({
            "id": f.get("format_id"),
            "size": f.get("filesize"),
            "approx": f.get("filesize_approx"),
            "tbr": f.get("tbr"),
            "v": (f.get("vcodec") or "none") != "none",
            "a": (f.get("acodec") or "none") != "none",
        })
    return out


class _Reporter:
    """The two yt-dlp hooks, the throttle and the stop check."""

    def __init__(self, work_dir, stop_class):
        self.stop_path = os.path.join(work_dir, STOP_FILE)
        self.stop_class = stop_class
        self.stopped = None                 # "pause" / "cancel" once a stop was requested
        self._last_emit = {}                # (file, status) -> time
        self._last_stop_check = 0.0

    def _check_stop(self):
        if self.stopped:
            raise self.stop_class("stop requested (%s)" % self.stopped)
        now = time.monotonic()
        if now - self._last_stop_check < STOP_CHECK_EVERY:
            return
        self._last_stop_check = now
        try:
            if os.path.exists(self.stop_path):
                with open(self.stop_path, "r", encoding="utf-8", errors="replace") as fh:
                    kind = (fh.read(20) or "").strip().lower()
                self.stopped = kind if kind in ("pause", "cancel") else "cancel"
        except OSError:
            return
        if self.stopped:
            raise self.stop_class("stop requested (%s)" % self.stopped)

    def progress(self, d):
        """yt-dlp's progress hook."""
        try:
            status = d.get("status")
            name = os.path.basename(str(d.get("filename") or ""))
            now = time.monotonic()
            key = (name, status)
            if status != "downloading" or now - self._last_emit.get(key, 0.0) >= EMIT_EVERY:
                self._last_emit[key] = now
                info = d.get("info_dict") or {}
                _emit(PROGRESS_MARKER, {
                    "k": "dl", "status": status, "file": name,
                    "done": d.get("downloaded_bytes"), "total": d.get("total_bytes"),
                    "est": d.get("total_bytes_estimate"), "speed": d.get("speed"), "eta": d.get("eta"),
                    "frag": d.get("fragment_index"), "frags": d.get("fragment_count"),
                    "title": info.get("title"),
                })
        except Exception:  # noqa: BLE001 - reporting must never break the download
            pass
        self._check_stop()

    def plan(self, info):
        """Called once before the transfer: which files are coming and how big they are said to be."""
        _emit(PROGRESS_MARKER, {"k": "plan", "streams": _stream_list(info), "title": (info or {}).get("title"),
                                "duration": (info or {}).get("duration")})

    def postprocess(self, d):
        """yt-dlp's postprocessor hook: which step started or finished."""
        try:
            name = d.get("postprocessor")
            if str(name or "").startswith(PLAN_PP_NAME):
                return                      # our own reporting step is not post-processing
            _emit(PROGRESS_MARKER, {"k": "pp", "status": d.get("status"), "pp": name})
        except Exception:  # noqa: BLE001
            pass


def _stop_class():
    """The exception the progress hook raises.  A DownloadCancelled subclass, so yt-dlp lets it
    through untouched instead of wrapping it as a download error."""
    try:
        from yt_dlp.utils import DownloadCancelled as base
    except Exception:  # noqa: BLE001 - yt-dlp missing: the download answers with its own error
        base = Exception
    return type("StopRequested", (base,), {})


# --------------------------------------------------------------------------- probe

PROBE_MAX = 100             # the hard ceiling, whatever the caller asks for
_UNAVAILABLE_TITLES = frozenset(["[private video]", "[deleted video]"])
_ID_RE = re.compile(r"^[\w-]{6,20}$")


def _entry_url(entry):
    """The http(s) link of one flat playlist entry, or ''."""
    url = str(entry.get("url") or entry.get("webpage_url") or "").strip()
    if re.match(r"^https?://", url, re.I):
        return url[:2048]
    if url and str(entry.get("ie_key") or "").lower() == "youtube" and _ID_RE.match(url):
        return "https://www.youtube.com/watch?v=" + url      # YouTube sometimes gives a bare id
    return ""


def probe(arguments):
    """Look at `arguments["url"]` without downloading: a playlist's title, size and first entries."""
    url = str(arguments.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url is required"}
    try:
        cap = int(arguments.get("max_entries") or PROBE_MAX)
    except (TypeError, ValueError):
        cap = PROBE_MAX
    cap = max(1, min(cap, PROBE_MAX))
    try:
        import yt_dlp
    except ImportError:
        return {"ok": False, "error": "yt-dlp isn't installed (pip install yt-dlp)"}
    opts = {"quiet": True, "no_warnings": True, "ignoreconfig": True, "socket_timeout": 30,
            "skip_download": True, "extract_flat": "in_playlist", "noplaylist": False,
            "playlistend": cap, "lazy_playlist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - always answer with a result
        return {"ok": False, "error": "couldn't look at that link: %s" % exc}
    if not isinstance(info, dict):
        return {"ok": False, "error": "yt-dlp found nothing at that link"}
    raw = info.get("entries")
    if raw is None:
        return {"ok": True, "is_playlist": False, "title": info.get("title")}
    entries, unavailable, unusable = [], 0, 0
    try:
        for entry in raw:
            if len(entries) + unavailable + unusable >= cap:
                break
            if not isinstance(entry, dict):
                unusable += 1
                continue
            title = str(entry.get("title") or "").strip()
            if title.lower() in _UNAVAILABLE_TITLES:
                unavailable += 1
                continue
            link = _entry_url(entry)
            if not link:
                unusable += 1
                continue
            entries.append({"title": title[:200], "url": link})
    except Exception as exc:  # noqa: BLE001 - a listing that breaks half way is still an answer
        if not entries:
            return {"ok": False, "error": "couldn't read the playlist: %s" % exc}
    total = info.get("playlist_count")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    seen = len(entries) + unavailable + unusable
    return {"ok": True, "is_playlist": True, "title": str(info.get("title") or "").strip()[:200],
            "count": total if total is not None else seen,
            "more": bool(total is not None and total > seen) or (total is None and seen >= cap),
            "entries": entries, "unavailable": unavailable, "unusable": unusable}


# --------------------------------------------------------------------------- generic guard

OK_PROTOCOLS = frozenset(["http", "https", "m3u8_native", "http_dash_segments"])
REFUSED = "private or local address"        # engines/ytdlp.py reads this as a permanent failure


def _peer_check(sock, address, is_public=None):
    """The address a connect() may really use, or an OSError.  A host NAME is resolved here and
    every answer must be public; the first one is what gets used, so the name is not looked up
    a second time by the connect (no rebinding window)."""
    import ipaddress
    import socket
    if is_public is None:
        from jarvis.hub import netguard
        is_public = netguard._is_public
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple) or len(address) < 2:
        return address                                   # a local (unix) socket is not the network
    host, port = str(address[0]), address[1]
    bare = host.split("%")[0]
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, sock.family, socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            raise OSError("couldn't resolve %s: %s" % (host[:80], exc))
        if not infos or any(not is_public(i[4][0]) for i in infos):
            raise OSError("%s is a %s: refused for the generic extractor" % (host[:80], REFUSED))
        return infos[0][4]
    if not is_public(bare):
        raise OSError("%s is a %s: refused for the generic extractor" % (host[:80], REFUSED))
    return address


def install_network_guard(is_public=None):
    """Refuse non-public peers for every connection this process makes.  Returns a function that
    puts things back (the tests use it; the worker never does: it exits after the download)."""
    import socket
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address):
        return real_connect(self, _peer_check(self, address, is_public))

    def connect_ex(self, address):
        try:
            address = _peer_check(self, address, is_public)
        except OSError:
            return 13                                    # EACCES: connect_ex reports, it doesn't raise
        return real_connect_ex(self, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex

    def restore():
        socket.socket.connect, socket.socket.connect_ex = real_connect, real_connect_ex
    return restore


def check_generic_format(info):
    """Raise unless `info` (one file yt-dlp is about to fetch) is fetched by yt-dlp itself."""
    info = info or {}
    protocol = str(info.get("protocol") or "https")
    if info.get("is_live") or protocol not in OK_PROTOCOLS:
        raise RuntimeError(
            "the generic extractor found a %s that another program would have to fetch (%s); that "
            "can't be kept inside the %s guard, so it was refused"
            % ("live stream" if info.get("is_live") else "stream", protocol, REFUSED))


def apply_generic_guard(yt_dlp):
    """Everything `"generic": true` turns on, in this process: the network guard and a YoutubeDL
    that checks each file before fetching it."""
    restore = install_network_guard()
    base = yt_dlp.YoutubeDL

    class GuardedYoutubeDL(base):
        def dl(self, name, info, *args, **kwargs):
            check_generic_format(info)
            return super().dl(name, info, *args, **kwargs)

    yt_dlp.YoutubeDL = GuardedYoutubeDL

    def undo():
        yt_dlp.YoutubeDL = base
        restore()
    return undo


def main():
    global _proto
    _proto = sys.stdout
    sys.stdout = sys.stderr          # whatever yt-dlp prints can't corrupt the protocol
    reporter = None
    try:
        arguments = json.loads(sys.stdin.read() or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": "bad worker input: %s" % exc}
    else:
        if arguments.get("op") == "probe":
            try:
                result = probe(arguments)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": "probe failed: %s" % exc}
        else:
            try:
                from jarvis import ytdl_tools
                if arguments.get("generic") is True:
                    import yt_dlp
                    apply_generic_guard(yt_dlp)
                work_dir = str(arguments.get("output_dir") or ".")
                reporter = _Reporter(work_dir, _stop_class())
                result = ytdl_tools.download_with_hooks(
                    arguments, progress_hooks=[reporter.progress], postprocessor_hooks=[reporter.postprocess],
                    plan_hook=reporter.plan)
            except Exception as exc:  # noqa: BLE001 - always answer with a result line
                result = {"ok": False, "error": "download failed: %s" % exc}
    if reporter is not None and reporter.stopped:
        # yt-dlp may have turned our exception into an error message; what happened is "stopped".
        result = {"ok": False, "stopped": reporter.stopped, "error": "stopped (%s)" % reporter.stopped}
    try:
        line = json.dumps(result, default=str)
    except Exception:  # noqa: BLE001
        line = json.dumps({"ok": False, "error": "the result could not be encoded"})
    with _lock:
        _proto.write("\n%s %s\n" % (MARKER, line))
        _proto.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
