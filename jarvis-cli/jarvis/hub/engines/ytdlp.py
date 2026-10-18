"""The yt-dlp engine (plan section 7.1, Level A): video and audio from YouTube and the ~1800
other sites yt-dlp has a specific extractor for.

Level A means the existing `ytdl_tools.ytdl_download` is reused unchanged, so a hub download
and a `ytdl_download` tool call behave identically (same quality presets, containers, codecs,
subtitles, SponsorBlock, ffmpeg check).  What this file adds around it:

  * it runs in its OWN PROCESS (engines/_ytdlp_worker.py), so pause, cancel and a daemon
    shutdown can really stop it; yt-dlp keeps its `.part` files and the next attempt
    continues from them (the plan's "yt-dlp .part continuation");
  * every download works in its own hidden folder, `<destination>/.jarvis-ytdl-<item id>/`.
    ytdl_download lists every media file in the folder it was given, so a shared "Video" folder
    would be reported wrongly; a private folder is also what lets cancel delete exactly this
    download's leftovers and nothing else;
  * when it finishes, the files move into the destination folder (a rename on the same disk),
    never overwriting, and the private folder goes away;
  * progress is the size of that folder (bytes and speed, no percent: yt-dlp's own numbers
    arrive with Level B in phase 8, so the bar is indeterminate until then);
  * no growth for `ytdlp.stall_seconds` (default 300) comes back as a stall, a network error
    as a retryable failure and an unsupported / private / geo-blocked video as a permanent one.

What is deliberately NOT here: playlists (a hub download is one video; `ytdl_download`'s
`playlist` option is not passed), cookies, a proxy, and the generic extractor.  The router only
claims a link when it is a YouTube page or a site yt-dlp has a SPECIFIC extractor for, so this
engine can never be pointed at an arbitrary address (the HTTP engine's SSRF guard doesn't apply
to yt-dlp, which does its own fetching).
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

from .. import naming, states
from .. import settings as settings_mod
from .base import Engine, Outcome, RouteRule

WORK_PREFIX = ".jarvis-ytdl-"
MARKER = "@@JARVIS_YTDL_RESULT@@"
REPORT_EVERY = 1.0
POLL_EVERY = 0.25
KILL_WAIT = 8.0
DEFAULT_STALL_S = 300

# Options a hub item may carry through to ytdl_download (everything else is ignored).
PASS_KEYS = ("mode", "quality", "container", "video_codec", "audio_format", "audio_quality",
             "embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs",
             "sub_langs", "sponsorblock_remove", "skip_if_downloaded")
_BOOL_KEYS = ("embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs",
              "skip_if_downloaded")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

_YOUTUBE_HOSTS = frozenset(["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                            "youtu.be", "www.youtu.be"])

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
    """One worker process plus the threads draining its pipes."""

    def __init__(self, proc):
        self.proc = proc
        self.result = None
        self.stderr_tail = ""
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
        except (OSError, ValueError):
            pass

    def _read_err(self):
        try:
            for line in self.proc.stderr:
                self.stderr_tail = (self.stderr_tail + line)[-2000:]
        except (OSError, ValueError):
            pass

    def running(self):
        return self.proc.poll() is None

    def finish(self, timeout=2.0):
        for t in self._threads:
            t.join(timeout)

    def kill(self):
        _kill_tree(self.proc)
        try:
            self.proc.wait(KILL_WAIT)
        except Exception:  # noqa: BLE001
            pass
        self.finish(1.0)


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
              "not a valid url", "unable to extract", "this video is", "has been terminated")
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
    return low.endswith((".part", ".ytdl", ".temp", ".tmp")) or ".part-frag" in low


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
        call["output_dir"] = str(work_dir)
        rate = ctx.rate_limit_bps()
        if rate > 0:
            call["rate_limit_kbps"] = max(1, int(rate // 1024))
        stall = float(ctx.setting("ytdlp", "stall_seconds", default=DEFAULT_STALL_S))

        ctx.set_state("downloading", percent=None, bytes_done=folder_size(work_dir))
        try:
            run = _Run(_spawn(call, work_dir))
        except OSError as exc:
            return Outcome.failed("couldn't start the yt-dlp helper: %s" % _short(exc), retryable=True)

        tracker = states.RateTracker()
        last_size, last_growth, last_report = folder_size(work_dir), time.monotonic(), 0.0
        stop = None
        while run.running():
            stop = ctx.stop_requested()
            if stop:
                run.kill()
                break
            now = time.monotonic()
            if now - last_report >= REPORT_EVERY:
                last_report = now
                size = folder_size(work_dir)
                if size != last_size:
                    last_size, last_growth = size, now
                speed = tracker.update(size)
                if not ctx.update(bytes_done=size, speed_bps=speed):
                    stop = "cancel"                    # the row was deleted under us
                    run.kill()
                    break
                if now - last_growth > stall:
                    run.kill()
                    return Outcome.stalled("no data for %d s" % int(stall))
            time.sleep(POLL_EVERY)
        run.finish()

        if stop == "pause":
            return Outcome.paused()
        if stop == "cancel":
            remove_work_dir(work_dir, roots)
            return Outcome.cancelled()
        if stop:
            return Outcome.requeue()

        result = run.result
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

    def _finalize(self, ctx, result, work_dir, dest_dir, roots):
        if not naming.under_any(roots, dest_dir):
            return Outcome.failed("the destination folder isn't allowed")
        ctx.set_state("postprocessing", phase="saving into the library")
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
                target = naming.unique_path(Path(dest_dir) / name)
                size = source.stat().st_size
                try:
                    os.replace(str(source), str(target))
                except OSError:
                    shutil.move(str(source), str(target))        # another disk: copy then delete
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
                  "source_ref": {}}
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
