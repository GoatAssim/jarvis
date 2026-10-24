"""The yt-dlp engine (plan section 7.1): video and audio from YouTube and the ~1800 other sites
yt-dlp has a specific extractor for.

The download itself is `ytdl_tools.download_with_hooks`, the same code and the same option
builder (`ytdl_tools.build_ydl_opts`) as the `ytdl_download` tool, so a hub download and a tool
call behave identically (same quality presets, containers, codecs, subtitles, SponsorBlock,
ffmpeg check).  What this file adds around it:

  * it runs in its OWN PROCESS (engines/_ytdlp_worker.py), so pause, cancel and a daemon
    shutdown can really stop it; yt-dlp keeps its `.part` files and the next attempt
    continues from them (the plan's "yt-dlp .part continuation");
  * LEVEL B, real progress: the worker forwards yt-dlp's own progress hooks as lines on stdout;
    `ProgressModel` turns them into ONE bar for the whole download (video + audio streams
    together, not two bars that each reach 100 %), with bytes, speed, ETA and the file being
    written.  A size that is only yt-dlp's estimate is reported as an estimate, so the panel shows
    "≈" next to the percent and ETA; a size that is exact is not marked.  When nothing is known
    the bar stays indeterminate with the live size;
  * merge / extract audio / embed show as their own `postprocessing` phase with a label ("merging
    video and audio (3/3)"), so a finished transfer never sits frozen at 100 %;
  * CANCEL and PAUSE ask the worker to stop (a control file in the private folder; its progress
    hook raises, yt-dlp stops at once and leaves the `.part`).  If the worker hasn't gone within
    `GRACE_S`, or it is post-processing / not downloading, the process tree is killed instead:
    the kill-based stop is the fallback and is never skipped;
  * every download works in its own hidden folder, `<destination>/.jarvis-ytdl-<item id>/`.
    ytdl_download lists every media file in the folder it was given, so a shared "Video" folder
    would be reported wrongly; a private folder is also what lets cancel delete exactly this
    download's leftovers and nothing else, and what guarantees a cancelled download never leaves
    a half-written FINAL file (finished files only move out of it after a success);
  * when it finishes, the files move into the destination folder (a rename on the same disk),
    never overwriting, and the private folder goes away;
  * no activity for `ytdlp.stall_seconds` (default 300; progress lines and folder growth both
    count) comes back as a stall, a network error as a retryable failure and an unsupported /
    private / geo-blocked video as a permanent one.

If a worker sends no progress lines at all (an older helper, a site whose hooks never fire), the
row falls back to the size of the folder: live bytes and speed, no percent.

What is deliberately NOT here: cookies and a proxy.  A hub download is still ONE video:
`ytdl_download`'s `playlist` option is never passed.  A playlist becomes a group of single-video
rows made by `engines/_ytdlp_playlist.py` (it looks first, asks, caps at 50).  The router only
claims a link when it is a YouTube page or a site yt-dlp has a SPECIFIC extractor for, so this
engine is not pointed at an arbitrary address by default (the HTTP engine's SSRF guard doesn't
apply to yt-dlp, which does its own fetching).  The one exception is the generic extractor,
asked once per link (`options.generic`, see GENERIC_ENABLED below, OFF in this version).
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .. import naming, netguard, states
from .. import settings as settings_mod
from .base import Engine, Outcome, RouteRule

WORK_PREFIX = ".jarvis-ytdl-"
MARKER = "@@JARVIS_YTDL_RESULT@@"                 # same strings as engines/_ytdlp_worker.py
PROGRESS_MARKER = "@@JARVIS_YTDL_PROGRESS@@"
STOP_FILE = ".jarvis-ytdl-stop"
REPORT_EVERY = 0.5
POLL_EVERY = 0.1
KILL_WAIT = 8.0
GRACE_S = 2.5            # how long a polite stop may take before the process tree is killed
DEFAULT_STALL_S = 300
SPEED_FRESH_S = 5.0      # a speed older than this is not shown (between two streams, say)

# Options a hub item may carry through to ytdl_download (everything else is ignored).
PASS_KEYS = ("mode", "quality", "container", "video_codec", "audio_format", "audio_quality",
             "embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs",
             "sub_langs", "sponsorblock_remove", "skip_if_downloaded")
_BOOL_KEYS = ("embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs",
              "skip_if_downloaded")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

_YOUTUBE_HOSTS = frozenset(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                            "youtu.be", "www.youtu.be"])

# The generic extractor (a link no specific extractor claims), allowed by the owner for ONE link.
# OFF: yt-dlp does its own fetching and follows redirects itself, which netguard cannot see.  The
# worker has a connect-level guard for it (see _ytdlp_worker.py) but it could not be tried against
# real yt-dlp, and ffmpeg / rtmpdump connect outside any Python guard, so the owner decides.
GENERIC_ENABLED = False
GENERIC_KEY = "generic"

# Tests swap these.
worker_command_override = None
EXTRACTOR_SOURCE = None            # callable -> list of extractor classes; None = ask yt-dlp
_extractors = None


# --------------------------------------------------------------------------- options

def normalize_options(raw):
    """The yt-dlp options of one download -> ({clean}, error).  Unknown keys are ignored
    here (the item also carries subdir / limit_kbps / notify ...); a bad value is an error,
    never silently dropped, because the owner asked for it."""
    from ... import ytdl_tools
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    mode = raw.get("mode")
    if mode not in (None, ""):
        mode = str(mode).strip().lower()
        if mode not in ("video", "audio"):
            return {}, "mode must be 'video' or 'audio'"
        out["mode"] = mode
    quality = raw.get("quality")
    if quality not in (None, ""):
        quality = str(quality).strip()
        if len(quality) > 200 or _CTRL.search(quality):
            return {}, "quality is not a usable format selector"
        out["quality"] = quality
    for key, allowed in (("container", ytdl_tools._CONTAINERS), ("video_codec", ytdl_tools._VIDEO_CODECS),
                         ("audio_format", ytdl_tools._AUDIO_FORMATS)):
        value = raw.get(key)
        if value not in (None, ""):
            value = str(value).strip().lower()
            if value not in allowed:
                return {}, "%s must be one of: %s" % (key, ", ".join(allowed))
            out[key] = value
    for key in ("audio_quality", "sub_langs", "sponsorblock_remove"):
        value = raw.get(key)
        if value not in (None, ""):
            value = str(value).strip()
            if len(value) > 120 or _CTRL.search(value):
                return {}, "%s is not usable" % key
            out[key] = value
    for key in _BOOL_KEYS:
        if raw.get(key) is not None:
            out[key] = bool(raw[key])
    if raw.get(GENERIC_KEY):                           # stored only when true: "this link, asked once"
        if not GENERIC_ENABLED:
            return {}, "yt-dlp's generic extractor is switched off in this version, so nothing was queued"
        out[GENERIC_KEY] = True
    return out, None


def extract_options(options):
    """Just the yt-dlp keys of an item's options (what gets stored under `ytdl`)."""
    return {k: (options or {}).get(k) for k in PASS_KEYS if (options or {}).get(k) is not None}


# --------------------------------------------------------------------------- routing

def _is_youtube(text):
    try:
        return (urlsplit(text).hostname or "").lower() in _YOUTUBE_HOSTS
    except ValueError:
        return False


def _is_http(text):
    return bool(re.match(r"^https?://", text or "", re.I))


def _extractor_classes():
    global _extractors
    if EXTRACTOR_SOURCE is not None:
        try:
            return list(EXTRACTOR_SOURCE())
        except Exception:  # noqa: BLE001
            return []
    if _extractors is None:
        try:
            from yt_dlp.extractor import gen_extractor_classes
            _extractors = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
        except Exception:  # noqa: BLE001 - yt-dlp missing or too old: claim nothing
            _extractors = []
    return _extractors


def _claimed_by(url):
    """Name of the site-specific yt-dlp extractor that takes `url`, or None."""
    for ie in _extractor_classes():
        try:
            if ie.suitable(url):
                return getattr(ie, "IE_NAME", None) or ie.ie_key()
        except Exception:  # noqa: BLE001 - one odd extractor must not break routing
            continue
    return None


_FILE_EXT = frozenset(naming.VIDEO_EXT) | frozenset(naming.AUDIO_EXT) | frozenset((
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".exe", ".msi", ".dmg", ".pkg",
    ".deb", ".rpm", ".apk", ".pdf", ".epub", ".torrent", ".bin", ".img"))


def _looks_like_file(url):
    name = naming.filename_from_url(url) or ""
    return os.path.splitext(name)[1].lower() in _FILE_EXT


def _match_youtube(text, settings):
    text = (text or "").strip()
    if _is_http(text) and _is_youtube(text):
        return {"reason": "YouTube page (yt-dlp engine)", "kind": "video", "source": text}
    return None


def _match_site(text, settings):
    text = (text or "").strip()
    if not _is_http(text) or _is_youtube(text) or _looks_like_file(text):
        return None
    name = _claimed_by(text)
    if name:
        return {"reason": "supported site: %s (yt-dlp engine)" % states.clean_text(name, 40),
                "kind": "video", "source": text}
    return None


def generic_problem(text):
    """Why the generic extractor may NOT take this link, or None when it may.  Checked before
    queueing and again when the download starts: http / https only, no credentials, and every
    address the host resolves to must be PUBLIC (the owner's allow_private_hosts list is
    deliberately not consulted: it was made for the HTTP engine)."""
    if not GENERIC_ENABLED:
        return "the generic extractor is switched off in this version"
    text = (text or "").strip()
    if not _is_http(text):
        return "only http and https links can go to the generic extractor"
    if _looks_like_file(text):
        return "that is a file link, not a page: the HTTP engine takes it (dl_add)"
    try:
        netguard.check_url(text, ())
    except netguard.GuardError as exc:
        return str(exc)
    return None


def _match_generic(text, settings):
    """A route hit for a link the owner allowed the generic extractor for, else None."""
    if generic_problem(text) is not None:
        return None
    return {"reason": "generic extractor, allowed by you for this link", "kind": "video",
            "source": (text or "").strip()}


# --------------------------------------------------------------------------- progress (Level B)

_PP_LABELS = {
    "merger": "merging video and audio", "ffmpegmerger": "merging video and audio",
    "ffmpegextractaudio": "extracting audio", "extractaudio": "extracting audio",
    "ffmpegvideoconvertor": "converting video", "ffmpegvideoremuxer": "remuxing video",
    "ffmpegembedsubtitle": "embedding subtitles", "embedthumbnail": "embedding thumbnail",
    "ffmpegthumbnailsconvertor": "converting the thumbnail", "ffmpegmetadata": "writing metadata",
    "modifychapters": "removing sponsor segments", "sponsorblock": "looking up sponsor segments",
    "ffmpegsubtitlesconvertor": "converting subtitles", "movefiles": "saving",
    "ffmpegfixupm3u8": "fixing the stream", "ffmpegfixupm4a": "fixing the audio",
    "ffmpegfixupstretched": "fixing the video", "ffmpegfixuptimestamp": "fixing timestamps",
    "ffmpegconcat": "joining parts",
}


def pp_label(name):
    key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    return _PP_LABELS.get(key) or "post-processing"


def expected_pp_steps(args, n_streams):
    """How many post-processing steps these options will usually need after the transfer.  Only
    used to draw "(3/4)": a guess is clamped, so it can never show a step beyond the total."""
    args = args or {}
    steps = 0
    if args.get("mode") == "audio":
        steps += 1
    elif n_streams > 1:
        steps += 1                                    # merging video and audio
    if args.get("embed_thumbnail"):
        steps += 1
    if args.get("embed_metadata") or args.get("embed_chapters"):
        steps += 1
    if args.get("embed_subs") and args.get("mode") != "audio":
        steps += 1
    if args.get("sponsorblock_remove"):
        steps += 1
    return steps


def _num(value):
    """A non-negative finite number from a worker line, else None (never trust the pipe)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


class ProgressModel:
    """What yt-dlp's hooks (forwarded by the worker) add up to: ONE row's progress.

    yt-dlp reports per FILE: a normal "video + audio" download is two files, each going 0 to 100 %,
    and then a merge.  Shown as it comes, the bar would reach 100 % twice and then freeze.  This
    adds the files up instead: finished files count with their real size, the current file with
    what it has so far, and files still to come with the size yt-dlp announced for them.

    `snapshot()` says whether the total is EXACT (every size is a real byte count) or ESTIMATED
    (any size is a guess: yt-dlp's `filesize_approx` / `total_bytes_estimate`).  When any size is
    unknown the total is None and the bar is indeterminate: no number is made up.
    """

    def __init__(self, args=None, clock=time.monotonic):
        self.args = args or {}
        self._clock = clock
        self.streams = []                 # [{"id", "size", "approx", "tbr", "v", "a"}] from the plan line
        self.duration = None
        self.files = []                   # file names in the order they first appeared
        self.finished = {}                # file -> its real size
        self.cur_file = None
        self.cur_done = 0
        self.cur_total = None             # exact size of the current file
        self.cur_est = None               # yt-dlp's estimate when it has no exact size
        self.speed = None
        self._speed_at = 0.0
        self.title = None
        self.state = "downloading"
        self.pp_started = 0
        self.pp_name = None
        self.pp_names = []                # every post-processor seen, in order (for the log and tests)
        self.events = 0                   # progress lines seen (0 = fall back to folder size)
        self.download_events = 0

    # ---- input ---------------------------------------------------------------------------------
    def feed(self, ev):
        if not isinstance(ev, dict):
            return
        self.events += 1
        if ev.get("k") == "pp":
            status = ev.get("status")
            if status in ("started", "processing"):
                self.state = "postprocessing"
                self.pp_name = ev.get("pp")
                if status == "started":
                    self.pp_started += 1
                    if len(self.pp_names) < 20:
                        self.pp_names.append(states.clean_text(self.pp_name, 40))
            return
        if ev.get("k") == "plan":
            streams = ev.get("streams")
            if isinstance(streams, list) and streams:
                self.streams = [{"id": st.get("id"), "size": _num(st.get("size")), "approx": _num(st.get("approx")),
                                 "tbr": _num(st.get("tbr")), "v": bool(st.get("v")), "a": bool(st.get("a"))}
                                for st in streams[:8] if isinstance(st, dict)]
            self.duration = _num(ev.get("duration"))
            title = states.clean_text(ev.get("title"), 300)
            if title:
                self.title = title
            return
        if ev.get("k") != "dl":
            return
        self.download_events += 1
        self.state = "downloading"
        name = states.clean_text(ev.get("file"), 200) or ""
        if name and name not in self.files:
            self.files.append(name)
        done, total, est = _num(ev.get("done")), _num(ev.get("total")), _num(ev.get("est"))
        if ev.get("status") == "finished":
            self.finished[name] = int(done or total or self.cur_done or 0)
            if self.cur_file == name:
                self.cur_file, self.cur_done, self.cur_total, self.cur_est = None, 0, None, None
        else:
            self.cur_file = name
            self.cur_done = int(done or 0)
            self.cur_total = int(total) if total else None
            self.cur_est = int(est) if est and not total else None
        speed = _num(ev.get("speed"))
        if speed:
            self.speed, self._speed_at = speed, self._clock()
        title = states.clean_text(ev.get("title"), 300)
        if title:
            self.title = title

    # ---- output --------------------------------------------------------------------------------
    def _plan(self, k):
        """(size, exact) announced for stream k, or (None, False)."""
        if k < len(self.streams):
            st = self.streams[k]
            if st["size"]:
                return int(st["size"]), True
            if st["approx"]:
                return int(st["approx"]), False
            if st["tbr"] and self.duration:               # bitrate x length: a rough size
                return int(st["tbr"] * 125 * self.duration), False
        return None, False

    def _stream_kind(self, k):
        if k < len(self.streams):
            st = self.streams[k]
            if st["v"] and not st["a"]:
                return "video"
            if st["a"] and not st["v"]:
                return "audio"
        return "stream"

    def snapshot(self):
        n = max(len(self.streams), len(self.files), 1)
        idx = min(max(len(self.files), 1), n)
        done = sum(self.finished.values()) + (self.cur_done if self.cur_file else 0)

        total, exact = 0, True
        for k in range(n):
            name = self.files[k] if k < len(self.files) else None
            if name is not None and name in self.finished:
                total += self.finished[name]
            elif name is not None and name == self.cur_file and self.cur_total:
                total += self.cur_total
            elif name is not None and name == self.cur_file and self.cur_est:
                total += self.cur_est
                exact = False
            else:
                size, size_exact = self._plan(k)
                if size is None:
                    total = None
                    break
                total += size
                exact = exact and size_exact
        if total is not None and total < done:
            total = done                                # an estimate that was too low

        speed = self.speed if self._clock() - self._speed_at <= SPEED_FRESH_S else None
        pp_expected = expected_pp_steps(self.args, n)
        snap = {"state": self.state, "bytes_done": int(done), "bytes_total": int(total) if total else None,
                "estimated": not exact, "speed_bps": speed, "eta_s": None,
                "phase": None, "phase_index": None, "phase_count": None,
                "message": self.cur_file or (self.files[-1] if self.files else None), "title": self.title}
        if self.state == "postprocessing":
            if snap["bytes_total"] is None and done:
                snap["bytes_total"], snap["estimated"] = int(done), False
            snap["bytes_done"] = snap["bytes_total"] or snap["bytes_done"]
            snap["speed_bps"] = None
            snap["estimated"] = False                    # all bytes are in; 100 % is simply true
            step = n + max(self.pp_started, 1)
            count = max(n + pp_expected, step)
            snap.update(phase=pp_label(self.pp_name), phase_index=step, phase_count=count)
            return snap
        if total and speed and exact:
            snap["eta_s"] = max(0.0, (total - done) / speed)
        if n > 1:
            snap.update(phase="downloading %s" % self._stream_kind(idx - 1), phase_index=idx,
                        phase_count=n + pp_expected)
        elif pp_expected:
            snap.update(phase="downloading", phase_index=1, phase_count=1 + pp_expected)
        return snap


# --------------------------------------------------------------------------- the process

def worker_command():
    if worker_command_override is not None:
        return list(worker_command_override)
    return [sys.executable, "-m", "jarvis.hub.engines._ytdlp_worker"]


def _package_parent():
    """The folder that contains the `jarvis` package, so the worker can import it."""
    return str(Path(__file__).resolve().parents[3])


def _spawn(arguments, cwd):
    env = dict(os.environ)
    parent = _package_parent()
    env["PYTHONPATH"] = parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            cwd=str(cwd), env=env, **kwargs)
    try:
        proc.stdin.write(json.dumps(arguments))
        proc.stdin.close()
    except OSError:
        pass
    return proc


class _Run:
    """One worker process plus the threads draining its pipes.  The stdout thread feeds the
    worker's progress lines into `model` (see ProgressModel)."""

    def __init__(self, proc, model=None):
        self.proc = proc
        self.model = model if model is not None else ProgressModel()
        self.result = None
        self.stderr_tail = ""
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._read_out, daemon=True),
                         threading.Thread(target=self._read_err, daemon=True)]
        for t in self._threads:
            t.start()

    def _read_out(self):
        try:
            for line in self.proc.stdout:
                if line.startswith(MARKER):
                    try:
                        self.result = json.loads(line[len(MARKER):].strip())
                    except ValueError:
                        self.result = {"ok": False, "error": "the download helper answered garbled"}
                elif line.startswith(PROGRESS_MARKER):
                    try:
                        ev = json.loads(line[len(PROGRESS_MARKER):].strip())
                    except ValueError:
                        continue                    # a garbled progress line is just skipped
                    with self._lock:
                        self.model.feed(ev)
        except (OSError, ValueError):
            pass

    def _read_err(self):
        try:
            for line in self.proc.stderr:
                self.stderr_tail = (self.stderr_tail + line)[-2000:]
        except (OSError, ValueError):
            pass

    def snapshot(self):
        """(what the model says now, how many progress lines have arrived)."""
        with self._lock:
            return self.model.snapshot(), self.model.events

    def downloading(self):
        """True while the worker is in the transfer itself, where its hook can stop it."""
        with self._lock:
            return self.model.state == "downloading" and self.model.download_events > 0

    def running(self):
        return self.proc.poll() is None

    def finish(self, timeout=2.0):
        for t in self._threads:
            t.join(timeout)

    def request_stop(self, work_dir, kind):
        """Ask the worker to stop politely.  False if the request couldn't be written."""
        try:
            Path(work_dir, STOP_FILE).write_text(kind, encoding="utf-8")
            return True
        except OSError:
            return False

    def kill(self):
        _kill_tree(self.proc)
        try:
            self.proc.wait(KILL_WAIT)
        except Exception:  # noqa: BLE001
            pass
        self.finish(1.0)


def stop_run(run, work_dir, kind, grace=None):
    """Stop a worker for `kind` ("pause" / "cancel" / "shutdown").

    Pause and cancel while the worker is transferring are asked politely first: its progress
    hook raises and yt-dlp stops, leaving the `.part` for the next attempt.  If it doesn't exit in
    `grace` seconds, or it is post-processing (no hook runs while ffmpeg works), or this is a
    shutdown (the daemon is waiting), the process tree is killed: the fallback is always there.
    Returns "polite" or "killed".
    """
    grace = GRACE_S if grace is None else grace
    how = "killed"
    if kind in ("pause", "cancel") and run.running() and run.downloading() and run.request_stop(work_dir, kind):
        end = time.monotonic() + grace
        while run.running() and time.monotonic() < end:
            time.sleep(0.05)
        if not run.running():
            run.finish(1.0)
            how = "polite"
    if run.running():
        run.kill()
    try:
        os.remove(os.path.join(str(work_dir), STOP_FILE))
    except OSError:
        pass
    return how


def _kill_tree(proc):
    """Stop the worker and what it started (ffmpeg) -- psutil when we have it."""
    try:
        import psutil
        try:
            parent = psutil.Process(proc.pid)
            kids = parent.children(recursive=True)
        except psutil.Error:
            kids = []
        for child in kids:
            try:
                child.kill()
            except psutil.Error:
                pass
    except ImportError:
        pass
    try:
        proc.kill()
    except OSError:
        pass


# --------------------------------------------------------------------------- results

_PERMANENT = ("unsupported url", "geo-restricted", "private video", "video unavailable", "sign in",
              "members-only", "age-restricted", "age restricted", "removed", "not available", "drm",
              "ffmpeg", "isn't installed", "no output file", "requested format is not available",
              "not a valid url", "unable to extract", "this video is", "has been terminated",
              "private or local address", "generic extractor")
_TRANSIENT = ("timed out", "timeout", "connection", "temporarily", "http error 5", "http error 429",
              "reset by peer", "urlopen error", "incompleteread", "remote end closed",
              "network is unreachable", "name resolution", "try again", "too many requests")


def classify_error(text):
    """(retryable, delay_s or None) for an error message from yt-dlp."""
    low = str(text or "").lower()
    if any(p in low for p in _PERMANENT):
        return False, None
    if any(p in low for p in _TRANSIENT):
        return True, (60.0 if ("429" in low or "too many requests" in low) else None)
    return False, None


def _is_partial(name):
    """A leftover of an unfinished transfer (yt-dlp's .part / .ytdl / fragment files)."""
    low = name.lower()
    return low == STOP_FILE or low.endswith((".part", ".ytdl", ".temp", ".tmp")) or ".part-frag" in low


def folder_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _short(exc):
    return states.clean_text("%s: %s" % (type(exc).__name__, exc), 160)


def remove_work_dir(work_dir, roots):
    """Delete a download's private folder -- only a folder this engine named, only inside a root."""
    try:
        path = Path(work_dir)
        if (path.name.startswith(WORK_PREFIX) and path.is_dir() and naming.under_any(roots, path)
                and not any(Path(r).resolve() == path.resolve() for r in roots)):
            shutil.rmtree(path, ignore_errors=True)
            return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------- the engine

class YtdlpEngine(Engine):
    name = "ytdlp"
    label = "yt-dlp"
    supports_pause = True

    def available(self):
        if importlib.util.find_spec("yt_dlp") is None:
            return False, "yt-dlp isn't installed (pip install yt-dlp, or pip install -e .[ytdl] in jarvis-cli/)"
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg isn't installed or not on PATH (yt-dlp needs it to merge video and audio)"
        return True, ""

    def generic_match(self, text, settings):
        """The router asks this only for a link the owner allowed the generic extractor for."""
        return _match_generic(text, settings)

    def run(self, item, ctx):
        try:
            return self._run(item, ctx)
        except Exception as exc:  # noqa: BLE001 - an engine never raises past run
            return Outcome.failed("unexpected error: %s" % _short(exc))

    def _prepare(self, item, ctx, args):
        """(work_dir, dest_dir, error): where this attempt works; made once, reused on resume."""
        opts = item.get("options") or {}
        roots = settings_mod.write_roots(ctx.settings, opts)
        ref = dict(item.get("source_ref") or {})
        work, dest = ref.get("work_dir"), ref.get("dest_dir")
        if work and dest and naming.under_any(roots, work) and naming.under_any(roots, dest):
            Path(work).mkdir(parents=True, exist_ok=True)
            return Path(work), Path(dest), None
        audio = args.get("mode") == "audio"
        kind = "audio" if audio else "video"
        sample = "x." + (args.get("audio_format", "mp3") if audio else args.get("container", "mp4"))
        dest_dir, _source = settings_mod.resolve_dest_dir(ctx.settings, sample, kind, opts)
        if dest_dir is None:
            return None, None, ("the destination folder isn't allowed (a sub-folder of the library root, "
                                "or a full path you approved)")
        work_dir = dest_dir / (WORK_PREFIX + item["id"])
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, None, "can't create %s: %s" % (work_dir, exc)
        ctx.update(kind=kind, dest_dir=str(dest_dir), source_ref={"work_dir": str(work_dir), "dest_dir": str(dest_dir)})
        return work_dir, dest_dir, None

    def _run(self, item, ctx):
        args, problem = normalize_options(item.get("options"))
        if problem:
            return Outcome.failed(problem)
        ok, why = self.available()
        if not ok:
            return Outcome.failed("the ytdlp engine isn't ready: %s" % why)
        work_dir, dest_dir, problem = self._prepare(item, ctx, args)
        if problem:
            return Outcome.failed(problem)
        roots = settings_mod.write_roots(ctx.settings, item.get("options"))

        call = dict(args)
        call["url"] = item.get("source_url") or ""
        if args.get(GENERIC_KEY):
            # Asked once, remembered on the row.  Looked at again NOW: the switch may have been
            # turned off since, and a name can point somewhere else than it did at queue time.
            why = generic_problem(call["url"])
            if why:
                return Outcome.failed("the generic extractor was refused for this link: %s" % states.clean_text(why, 160))
        call["output_dir"] = str(work_dir)
        rate = ctx.rate_limit_bps()
        if rate > 0:
            call["rate_limit_kbps"] = max(1, int(rate // 1024))
        stall = float(ctx.setting("ytdlp", "stall_seconds", default=DEFAULT_STALL_S))

        try:
            os.remove(os.path.join(str(work_dir), STOP_FILE))     # a stop left from an earlier attempt
        except OSError:
            pass
        ctx.set_state("downloading", percent=None, bytes_done=folder_size(work_dir))
        model = ProgressModel(args)
        try:
            run = _Run(_spawn(call, work_dir), model)
        except OSError as exc:
            return Outcome.failed("couldn't start the yt-dlp helper: %s" % _short(exc), retryable=True)

        tracker = states.RateTracker()
        now = time.monotonic()
        last_size, last_events, last_activity, last_report = folder_size(work_dir), 0, now, 0.0
        shown_state = "downloading"
        stop = None
        while run.running():
            stop = ctx.stop_requested()
            if stop:
                stop_run(run, work_dir, stop)
                break
            now = time.monotonic()
            if now - last_report >= REPORT_EVERY:
                last_report = now
                size = folder_size(work_dir)
                snap, events = run.snapshot()
                if size != last_size or events != last_events:
                    last_activity = now                  # bytes on disk or a progress line: alive
                last_size, last_events = size, events
                if events:
                    alive, shown_state = self._report(ctx, snap, shown_state)
                else:                                    # no progress lines: the folder is all we know
                    alive = ctx.update(bytes_done=size, speed_bps=tracker.update(size))
                if not alive:
                    stop = "cancel"                      # the row was deleted under us
                    run.kill()
                    break
                if now - last_activity > stall:
                    run.kill()
                    return Outcome.stalled("no data for %d s" % int(stall))
            time.sleep(POLL_EVERY)
        run.finish()

        if stop == "pause":
            # yt-dlp kept writing until it stopped: show where it really is, not the last tick
            snap, events = run.snapshot()
            if events:
                self._report(ctx, snap, shown_state)
            else:
                ctx.update(bytes_done=folder_size(work_dir))
            return Outcome.paused()
        if stop == "cancel":
            remove_work_dir(work_dir, roots)
            return Outcome.cancelled()
        if stop:
            return Outcome.requeue()

        result = run.result
        if isinstance(result, dict) and result.get("stopped"):
            return Outcome.failed("the yt-dlp helper stopped on its own (%s)" % states.clean_text(result.get("stopped"), 20),
                                  retryable=True)
        if result is None:
            tail = states.clean_text(run.stderr_tail.strip().splitlines()[-1] if run.stderr_tail.strip() else "", 160)
            return Outcome.failed("the yt-dlp helper stopped unexpectedly (exit %s)%s"
                                  % (run.proc.returncode, ": " + tail if tail else ""), retryable=True)
        if not isinstance(result, dict) or not result.get("ok"):
            error = states.clean_text((result or {}).get("error") if isinstance(result, dict) else "", 300) \
                or "yt-dlp failed"
            retryable, delay = classify_error(error)
            return Outcome.failed(error, retryable=retryable, delay_s=delay)
        return self._finalize(ctx, result, work_dir, dest_dir, roots)

    @staticmethod
    def _report(ctx, snap, shown_state):
        """Write one ProgressModel snapshot to the row.  Returns (row still there, state shown)."""
        if snap["state"] != shown_state and ctx.set_state(snap["state"]):
            shown_state = snap["state"]
        fields = {key: snap[key] for key in ("phase", "phase_index", "phase_count", "message")}
        if snap["title"]:
            fields["title"] = snap["title"]
        alive = ctx.progress(bytes_done=snap["bytes_done"], bytes_total=snap["bytes_total"],
                             speed_bps=snap["speed_bps"], eta_s=snap["eta_s"],
                             estimated=snap["estimated"], **fields)
        return alive, shown_state

    def _finalize(self, ctx, result, work_dir, dest_dir, roots):
        if not naming.under_any(roots, dest_dir):
            return Outcome.failed("the destination folder isn't allowed")
        ctx.set_state("postprocessing", phase="saving into the library", phase_index=None, phase_count=None,
                      message=None, speed_bps=None, eta_s=None)
        try:
            names = sorted(p.name for p in Path(work_dir).iterdir() if p.is_file() and not _is_partial(p.name))
        except OSError as exc:
            return Outcome.failed("couldn't read the finished files: %s" % _short(exc))
        if not names:
            return Outcome.failed("yt-dlp reported success but left no file")
        moved, total = {}, 0
        try:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            for name in names:
                source = Path(work_dir) / name
                size = source.stat().st_size
                # The final name is CLAIMED, not just chosen (brief 7B): `unique_path` then `os.replace` let two
                # downloads of one name that finish together both pick it, and the second replaced the first.
                # `move_unique` links the file to the name (fails if it exists), then goes on to `name (1).ext`;
                # nothing is ever replaced.  Another disk is handled inside it.
                target = naming.move_unique(source, Path(dest_dir) / name)
                moved[name] = target
                total += size
        except OSError as exc:
            return Outcome.failed("couldn't save the file: %s" % _short(exc))
        remove_work_dir(work_dir, roots)
        listed = [f for f in (result.get("files") or []) if isinstance(f, dict)]
        primary = None
        for entry in listed:
            if entry.get("file") in moved:
                primary = moved[entry["file"]]
                break
        primary = primary or moved[names[0]]
        fields = {"dest_dir": str(dest_dir), "dest_file": str(primary), "bytes_done": total, "bytes_total": total,
                  "source_ref": {}, "phase": None, "phase_index": None, "phase_count": None, "message": None}
        title = listed[0].get("title") if listed else None
        if title:
            fields["title"] = title
        if len(moved) > 1:
            fields["message"] = "%d files" % len(moved)
        ctx.update(**fields)
        return Outcome.done(str(primary))


ENGINE = YtdlpEngine()
# Above the HTTP engine's 10: a video page is claimed here, a plain file link is not.
ROUTE_RULES = [RouteRule("ytdlp", 70, _match_youtube), RouteRule("ytdlp", 40, _match_site)]
