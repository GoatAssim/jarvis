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

A pipe protocol and not a thread: a hub download must be killable (see engines/ytdlp.py), and yt-dlp
runs in this process only.  Anything yt-dlp prints is moved to stderr so it can never be mistaken
for a protocol line.

This module starts with an underscore on purpose: engines/__init__.py skips it, so it is never
mistaken for an engine.  Nothing else imports it.
"""

import json
import os
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
        try:
            from jarvis import ytdl_tools
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
