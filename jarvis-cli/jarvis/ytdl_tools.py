"""Video/audio download tools for Jarvis, built on yt-dlp.

Three tools:
  ytdl_info      — read-only metadata lookup (title, duration, uploader,
                    thumbnail, subtitle languages, whether ffmpeg is
                    available). No file written, no confirmation needed.
  ytdl_formats   — read-only: lists yt-dlp's raw per-format table (id,
                    ext, resolution, fps, codecs, size) so an exact
                    format_id can be picked via quality="id:<format_id>".
  ytdl_download  — actually fetches media to disk. Mutating + network, so
                    it defaults to confirm_required=True (see
                    tool_safety.DEFAULT_CONFIRM_REQUIRED) same as
                    write_file/run_custom_command.

Deliberately bounded, matching the rest of this codebase's "known
simplifications" philosophy (see file_tools.py's docstring):
  - Playlists are opt-in (playlist=True) and hard-capped at
    MAX_PLAYLIST_ITEMS regardless of what playlist_items asks for —
    never a way to point one tool call at an entire channel.
  - No cookies/auth, no proxy, no impersonation, no arbitrary yt-dlp CLI
    flags exposed — url/mode/quality/container/codec/audio/subtitles/
    SponsorBlock/output_dir/rate-limit/playlist, and that's the surface.
    Anything needing a login wall fails with a clear error instead of
    half-working.
  - output_dir follows the exact same convention as file_tools.py's
    write_file: relative resolves against the user's home directory,
    absolute is used as-is. The confirm_required prompt shows the
    resolved arguments before anything runs, same trust boundary as
    write_file already established.
  - The web UI's inline player/download card only appears for the
    *default* location (~/.jarvis/downloads/<job>/) — see
    web/server.js's /api/downloads/:jobId/:filename route. Passing a
    custom output_dir trades that inline card away for filesystem
    placement freedom; the tool result still reports the real path.
  - Every download (single or playlist) still lands in its own job
    folder (dl_<timestamp>_<hex>) so the web route can serve a file by
    (job_id, filename) with no path-traversal ambiguity, and old job
    folders are pruned (MAX_KEEP), same pattern as
    screenshot_tools.py's _prune().
"""

import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import wire

JARVIS_DIR = Path.home() / ".jarvis"
DOWNLOAD_DIR = JARVIS_DIR / "downloads"
ARCHIVE_FILE = DOWNLOAD_DIR / ".archive.txt"
MAX_KEEP = 15  # job folders, not files — a job can be several files
MAX_PLAYLIST_ITEMS = 10  # hard cap, independent of what playlist_items asks for
MAX_MEDIA_EMITTED = 5  # inline player cards per download call, rest just listed in the result

JOB_ID_PREFIX = "dl_"

_QUALITY_MAP = {
    "best": "bv*+ba/b",
    "2160p": "bv*[height<=2160]+ba/b[height<=2160]",
    "1440p": "bv*[height<=1440]+ba/b[height<=1440]",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
    "360p": "bv*[height<=360]+ba/b[height<=360]",
}
_VIDEO_CODECS = ("h264", "vp9", "av1")
_CONTAINERS = ("mp4", "mkv", "webm")
_AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav", "flac", "aac", "vorbis", "alac")
_MEDIA_EXTS = {
    "mp4", "mkv", "webm", "mov", "avi", "flv", "m4v",
    "mp3", "m4a", "opus", "wav", "flac", "aac", "ogg", "wma",
}


def _import_yt_dlp():
    try:
        import yt_dlp
        return yt_dlp, None
    except ImportError:
        return None, (
            "yt-dlp isn't installed — run: pip install yt-dlp "
            "(or 'pip install -e .[ytdl]' in jarvis-cli/)"
        )


def _ffmpeg_path():
    return shutil.which("ffmpeg")


def ensure_dir():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOAD_DIR


def _prune():
    """Keep only the most-recently-modified MAX_KEEP job folders under the
    default download dir. Never touches a caller-supplied output_dir —
    only jobs this module itself created land here to begin with."""
    jobs = sorted(
        (p for p in DOWNLOAD_DIR.glob(f"{JOB_ID_PREFIX}*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    while len(jobs) > MAX_KEEP:
        try:
            shutil.rmtree(jobs.pop(0), ignore_errors=True)
        except OSError:
            break


def _new_job_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{JOB_ID_PREFIX}{stamp}_{secrets.token_hex(3)}"


def _resolve_output_dir(raw):
    """Same convention as file_tools._resolve_path: relative → under the
    user's home dir, absolute → used as given."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    return p


def _emit_media(kind, job_id, filename, title=None):
    """Machine line for the web UI — announced through wire.media(), which
    app.js's handleAskMedia turns into the download card."""
    label = (title or filename or "").replace("\t", " ").replace("\n", " ")
    wire.media(kind, job_id, filename, label)


def _fmt_duration(seconds):
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    seconds = int(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _fmt_bytes(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return None


def _base_opts(**overrides):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "socket_timeout": 30,
    }
    for k, v in overrides.items():
        if v is not None:
            opts[k] = v
    return opts


def _sanitize_info(yt_dlp, info):
    try:
        return yt_dlp.YoutubeDL.sanitize_info(info) if info else {}
    except Exception:
        return info or {}


def ytdl_info(arguments):
    arguments = arguments or {}
    url = arguments.get("url")
    if not url or not isinstance(url, str):
        return {"error": "url is required"}

    yt_dlp, err = _import_yt_dlp()
    if err:
        return {"error": err}

    opts = _base_opts(skip_download=True, noplaylist=True)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = _sanitize_info(yt_dlp, info)
    except yt_dlp.utils.GeoRestrictedError:
        return {"error": "That content is geo-restricted here."}
    except yt_dlp.utils.UnsupportedError:
        return {"error": "URL isn't a site/format yt-dlp supports."}
    except yt_dlp.utils.DownloadError as e:
        return {"error": f"couldn't look up {url}: {e}"}
    except Exception as e:
        return {"error": f"couldn't look up {url}: {e}"}

    formats = info.get("formats") or []
    heights = sorted({f.get("height") for f in formats if f.get("height")}, reverse=True)
    subs = sorted((info.get("subtitles") or {}).keys())
    auto_subs = sorted((info.get("automatic_captions") or {}).keys())

    return {
        "ok": True,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "duration_str": _fmt_duration(info.get("duration")),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "is_live": bool(info.get("is_live")),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "available_qualities": [f"{h}p" for h in heights[:8]],
        "subtitle_langs": subs[:15],
        "auto_caption_langs": auto_subs[:15],
        "is_playlist": bool(info.get("_type") == "playlist" or info.get("entries")),
        "ffmpeg_available": _ffmpeg_path() is not None,
        "note": "Metadata only — nothing downloaded. Call ytdl_download to actually fetch it.",
    }


def ytdl_formats(arguments):
    arguments = arguments or {}
    url = arguments.get("url")
    if not url or not isinstance(url, str):
        return {"error": "url is required"}

    yt_dlp, err = _import_yt_dlp()
    if err:
        return {"error": err}

    opts = _base_opts(skip_download=True, noplaylist=True)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = _sanitize_info(yt_dlp, info)
    except Exception as e:
        return {"error": f"couldn't look up {url}: {e}"}

    formats = info.get("formats") or []
    rows = []
    for f in formats:
        rows.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": (f"{f.get('height')}p" if f.get("height") else "audio only" if f.get("vcodec") == "none" else f.get("resolution")),
            "fps": f.get("fps"),
            "vcodec": None if f.get("vcodec") == "none" else f.get("vcodec"),
            "acodec": None if f.get("acodec") == "none" else f.get("acodec"),
            "abr_kbps": f.get("abr"),
            "filesize": _fmt_bytes(f.get("filesize") or f.get("filesize_approx")),
            "note": f.get("format_note"),
        })
    # Best-quality first: video formats by height, then audio-only by bitrate.
    rows.sort(key=lambda r: (r["resolution"] == "audio only", -(r.get("fps") or 0)), reverse=False)

    return {
        "ok": True,
        "title": info.get("title"),
        "formats": rows[:40],
        "truncated": len(rows) > 40,
        "note": "Pick a format_id and pass quality=\"id:<format_id>\" to ytdl_download.",
    }


# Options `background` hands to the Download Hub's yt-dlp engine (the same names as above).
_HUB_PASS = ("mode", "quality", "container", "video_codec", "audio_format", "audio_quality",
             "embed_thumbnail", "embed_metadata", "embed_chapters", "write_subs", "embed_subs",
             "sub_langs", "sponsorblock_remove", "skip_if_downloaded")


def _ytdl_download_background(arguments):
    """`background: true`: queue the download in the Download Hub and return at once.

    The hub's yt-dlp engine does the transfer (queue, pause / resume / cancel, retries, history in
    Menu > Downloads), so nothing here waits.  The default stays the blocking download below: this
    only runs when the caller asks.  What background does NOT do: playlists (one video per link),
    a custom output_dir (a hub download goes to the hub library folders) and the inline playable
    card the blocking download makes -- each is refused with a message instead of being ignored.
    """
    url = arguments.get("url")
    if arguments.get("playlist"):
        return {"error": "background downloads are one video per link; for a playlist leave background off"}
    if arguments.get("output_dir"):
        return {"error": "background downloads save into the Download Hub library folders; "
                         "leave output_dir out, or leave background off to save in a custom folder"}
    try:
        from .hub import service
        options = {k: arguments.get(k) for k in _HUB_PASS if arguments.get(k) is not None}
        res = service.add_items([url], engine="ytdlp", engine_options=options,
                                speed_limit_kbps=arguments.get("rate_limit_kbps"))
    except Exception as exc:  # noqa: BLE001 - a tool never raises
        return {"error": f"couldn't queue it in the Download Hub: {exc}"}
    if not res.get("ok"):
        why = res.get("error") or "the hub refused it"
        return {"error": f"couldn't queue it in the Download Hub: {why}. Leave background off to download it now."}
    added = (res.get("added") or [{}])[0]
    out = {
        "ok": True,
        "queued": True,
        "background": True,
        "id": added.get("id"),
        "duplicate": bool(added.get("duplicate")),
        "message": ("Already in the Download Hub." if added.get("duplicate")
                    else "Queued in the Download Hub; it downloads in the background (Menu > Downloads). "
                         "Use dl_status or dl_list to check on it."),
    }
    if res.get("note"):
        out["note"] = res["note"]
    return out


def _parse_download_args(arguments):
    """Validate and normalise ytdl_download's arguments -> (params, error).

    `error` is the exact message `ytdl_download` has always answered with for a bad argument
    (it wraps it as {"error": message}).  Nothing is touched on disk here and yt-dlp is not
    imported: this is the part of the old `ytdl_download` that only looked at the arguments, so
    the tool and the Download Hub's worker (Level B, plan 7.1) share ONE option builder and
    cannot drift apart.  The order of the checks is the original order.
    """
    mode = (arguments.get("mode") or "video").strip().lower()
    if mode not in ("video", "audio"):
        return None, f"unknown mode: {mode!r} (expected 'video' or 'audio')"

    quality = (arguments.get("quality") or "best").strip()
    container = (arguments.get("container") or "mp4").strip().lower()
    if container not in _CONTAINERS:
        return None, f"unknown container: {container!r} (expected one of {', '.join(_CONTAINERS)})"
    video_codec = (arguments.get("video_codec") or "").strip().lower() or None
    if video_codec and video_codec not in _VIDEO_CODECS:
        return None, f"unknown video_codec: {video_codec!r} (expected one of {', '.join(_VIDEO_CODECS)})"

    audio_format = (arguments.get("audio_format") or "mp3").strip().lower()
    if audio_format not in _AUDIO_FORMATS:
        return None, f"unknown audio_format: {audio_format!r} (expected one of {', '.join(_AUDIO_FORMATS)})"
    audio_quality = str(arguments.get("audio_quality") or "192K").strip()

    sub_langs = (arguments.get("sub_langs") or "en").strip()
    sponsorblock_remove = [c.strip() for c in (arguments.get("sponsorblock_remove") or "").split(",") if c.strip()]

    rate_limit_kbps = arguments.get("rate_limit_kbps")
    if rate_limit_kbps is not None:
        try:
            rate_limit_kbps = int(rate_limit_kbps)
        except (TypeError, ValueError):
            return None, "rate_limit_kbps must be a number"

    return {
        "mode": mode,
        "quality": quality,
        "container": container,
        "video_codec": video_codec,
        "audio_format": audio_format,
        "audio_quality": audio_quality,
        "embed_thumbnail": bool(arguments.get("embed_thumbnail", False)),
        "embed_metadata": bool(arguments.get("embed_metadata", False)),
        "embed_chapters": bool(arguments.get("embed_chapters", False)),
        "write_subs": bool(arguments.get("write_subs", False)),
        "embed_subs": bool(arguments.get("embed_subs", False)),
        "sub_langs": sub_langs,
        "sponsorblock_remove": sponsorblock_remove,
        "rate_limit_kbps": rate_limit_kbps,
        "playlist": bool(arguments.get("playlist", False)),
        "playlist_items": (arguments.get("playlist_items") or "").strip() or None,
        "skip_if_downloaded": bool(arguments.get("skip_if_downloaded", False)),
    }, None


def _assemble_opts(params, job_dir):
    """The yt_dlp.YoutubeDL options for validated `params`, writing into `job_dir`."""
    mode = params["mode"]
    quality = params["quality"]
    outtmpl = str(Path(job_dir) / "%(title).150B [%(id)s].%(ext)s")

    postprocessors = []
    if mode == "audio":
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": params["audio_format"],
            "preferredquality": params["audio_quality"],
        })

    sub_langs = params["sub_langs"]
    write_subs = params["write_subs"]
    opts = _base_opts(
        outtmpl=outtmpl,
        restrictfilenames=True,
        writethumbnail=params["embed_thumbnail"],
        embedthumbnail=params["embed_thumbnail"],
        addmetadata=params["embed_metadata"],
        addchapters=params["embed_chapters"],
        embedsubtitles=params["embed_subs"] and mode == "video",
        writesubtitles=write_subs,
        writeautomaticsub=write_subs,
        subtitleslangs=[l.strip() for l in sub_langs.split(",") if l.strip()] if write_subs else None,
        sponsorblock_remove=params["sponsorblock_remove"] or None,
        postprocessors=postprocessors or None,
        noplaylist=not params["playlist"],
    )

    if mode == "audio":
        opts["format"] = "bestaudio/best"
    else:
        if quality.startswith("id:"):
            fmt_id = quality[3:].strip()
            opts["format"] = f"{fmt_id}+bestaudio/{fmt_id}/best"
        else:
            opts["format"] = _QUALITY_MAP.get(quality, quality)
        opts["merge_output_format"] = params["container"]
        if params["video_codec"]:
            opts["format_sort"] = [f"vcodec:{params['video_codec']}"]

    if params["rate_limit_kbps"]:
        opts["ratelimit"] = params["rate_limit_kbps"] * 1024

    if params["playlist"]:
        opts["max_downloads"] = MAX_PLAYLIST_ITEMS
        if params["playlist_items"]:
            opts["playlist_items"] = params["playlist_items"]
        else:
            opts["playlist_items"] = f"1:{MAX_PLAYLIST_ITEMS}"

    if params["skip_if_downloaded"]:
        ensure_dir()
        opts["download_archive"] = str(ARCHIVE_FILE)
    return opts


def build_ydl_opts(arguments, job_dir=None):
    """THE option builder (plan 7.1, Level B): ytdl_download's arguments -> (opts, error).

    `opts` are the options for `yt_dlp.YoutubeDL`, writing into `job_dir` (when it is not given,
    the arguments' `output_dir`).  `error` is the message for a bad argument (then `opts` is
    {}).  `ytdl_download` and the Download Hub's worker both build their options here, so a hub
    download and a tool call behave identically (presets, containers, codecs, subtitles,
    SponsorBlock, rate limit).  No progress hooks are added here; see `download_with_hooks`.
    """
    arguments = arguments or {}
    params, error = _parse_download_args(arguments)
    if error:
        return {}, error
    if job_dir is None:
        raw = arguments.get("output_dir")
        if not raw:
            return {}, "a folder to download into is required (output_dir)"
        job_dir = _resolve_output_dir(raw)
    return _assemble_opts(params, job_dir), None


def ytdl_download(arguments):
    arguments = arguments or {}
    url = arguments.get("url")
    if not url or not isinstance(url, str):
        return {"error": "url is required"}
    if arguments.get("background"):
        return _ytdl_download_background(arguments)
    return download_with_hooks(arguments)


PLAN_PP_NAME = "JarvisPlan"


def _add_plan_hook(yt_dlp, ydl, plan_hook):
    """Have yt-dlp call `plan_hook(info)` once format selection is done and before any bytes
    move (its `before_dl` stage).  `info["requested_formats"]` is then known: it says whether
    this is one file or video + audio, and how big each is announced to be, which the progress
    hooks themselves can't say.  Best effort: if this yt-dlp can't do it, the download simply
    goes ahead without a plan."""
    try:
        from yt_dlp.postprocessor.common import PostProcessor

        class JarvisPlanPP(PostProcessor):
            PP_NAME = PLAN_PP_NAME

            def run(self, info):
                try:
                    plan_hook(info)
                except Exception:  # noqa: BLE001 - reporting must never break the download
                    pass
                return [], info

        ydl.add_post_processor(JarvisPlanPP(ydl), when="before_dl")
    except Exception:  # noqa: BLE001
        pass


def download_with_hooks(arguments, progress_hooks=None, postprocessor_hooks=None, plan_hook=None):
    """The blocking download behind `ytdl_download`, optionally reporting through yt-dlp's hooks.

    With no hooks this is exactly what `ytdl_download` always did.  The Download Hub's worker
    process (engines/_ytdlp_worker.py) passes `progress_hooks` / `postprocessor_hooks`: that is
    how the hub gets a real percent, speed, ETA and file name, and how a hook that raises
    stops the download cleanly.  `plan_hook(info)` is called once, before the transfer, with the
    selected formats (see `_add_plan_hook`).  Never takes the `background` (queue it in the hub)
    path.
    """
    arguments = arguments or {}
    url = arguments.get("url")
    if not url or not isinstance(url, str):
        return {"error": "url is required"}

    params, error = _parse_download_args(arguments)
    if error:
        return {"error": error}
    mode = params["mode"]
    playlist = params["playlist"]

    yt_dlp, err = _import_yt_dlp()
    if err:
        return {"error": err}

    # Our default selectors virtually always require a merge/extract/embed
    # step, so check up front rather than fail midway through a download.
    if not _ffmpeg_path():
        return {
            "ok": False,
            "error": (
                "ffmpeg isn't installed or not on PATH — yt-dlp needs it to merge "
                "video+audio, extract audio, or embed thumbnails/subs/metadata. "
                "Install it (e.g. `apt install ffmpeg`, `brew install ffmpeg`, or "
                "from ffmpeg.org) and try again."
            ),
        }

    custom_location = "output_dir" in arguments and arguments.get("output_dir")
    if custom_location:
        job_dir = _resolve_output_dir(arguments["output_dir"])
        job_id = None
    else:
        ensure_dir()
        job_id = _new_job_id()
        job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    opts = _assemble_opts(params, job_dir)
    if progress_hooks:
        opts["progress_hooks"] = list(progress_hooks)
        opts["noprogress"] = True        # the hooks are the progress report; no console bar
    if postprocessor_hooks:
        opts["postprocessor_hooks"] = list(postprocessor_hooks)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if plan_hook is not None:
                _add_plan_hook(yt_dlp, ydl, plan_hook)
            info = ydl.extract_info(url, download=True)
            info = _sanitize_info(yt_dlp, info)
    except yt_dlp.utils.GeoRestrictedError:
        return {"ok": False, "error": "That content is geo-restricted here."}
    except yt_dlp.utils.UnsupportedError:
        return {"ok": False, "error": "URL isn't a site/format yt-dlp supports."}
    except yt_dlp.utils.PostProcessingError as e:
        return {"ok": False, "error": f"Post-processing failed — is ffmpeg installed and on PATH? ({e})"}
    except yt_dlp.utils.MaxDownloadsReached:
        pass  # expected once our playlist cap is hit — files up to the cap still landed
    except yt_dlp.utils.DownloadError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"download failed: {e}"}

    # filepath (post_process/after_move stage) is the authoritative final
    # name — merges/remuxes/extraction can diverge from the pre-download
    # prediction (see PART 15 of the yt-dlp reference).
    media_files = sorted(
        (p for p in job_dir.iterdir() if p.is_file() and p.suffix.lstrip(".").lower() in _MEDIA_EXTS),
        key=lambda p: p.stat().st_mtime,
    )
    if not media_files:
        return {"ok": False, "error": "yt-dlp reported success but no output file was found."}

    title = info.get("title") or (media_files[0].stem if len(media_files) == 1 else None)
    results = []
    for i, path in enumerate(media_files):
        entry_title = title if len(media_files) == 1 else path.stem
        results.append({
            "file": path.name,
            "title": entry_title,
            "bytes": path.stat().st_size,
        })
        if job_id and i < MAX_MEDIA_EMITTED:
            _emit_media("download", job_id, path.name, entry_title)

    if job_id:
        _prune()

    note = (
        "File(s) saved on disk and offered to the user for playback/download in the UI. "
        if job_id else
        f"File(s) saved to {job_dir} (custom output_dir — no inline UI card, path is the real location). "
    )
    note += (
        "Do NOT invent details about the content, and do NOT say more than a short "
        "one-sentence confirmation that it's ready."
    )

    return {
        "ok": True,
        "job_id": job_id,
        "output_dir": str(job_dir) if not job_id else None,
        "mode": mode,
        "count": len(results),
        "files": results,
        "duration": info.get("duration"),
        "duration_str": _fmt_duration(info.get("duration")),
        "note": note,
    }


YTDL_TOOL_SCHEMAS = [
    {
        "name": "ytdl_info",
        "description": (
            "Look up metadata (title, uploader, duration, available qualities, subtitle "
            "languages) for a video/audio URL without downloading anything. YouTube and "
            "~1800 other sites via yt-dlp. Use this first if the user just wants details, "
            "or to check ffmpeg_available before a download that needs it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The video/audio page URL."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "ytdl_formats",
        "description": (
            "List every raw format yt-dlp sees for a URL (format_id, ext, resolution, fps, "
            "codecs, approx filesize), best quality first. Use when the user wants an exact "
            "quality/codec/file-size choice beyond the simple presets — pass the chosen "
            "format_id back to ytdl_download as quality=\"id:<format_id>\"."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The video/audio page URL."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "ytdl_download",
        "description": (
            "Download a video or extract audio from a URL (YouTube + ~1800 other sites via "
            "yt-dlp) and offer the file to the user in the UI. Always confirmed with the "
            "user first. Single video by default — set playlist=true to fetch multiple "
            f"items from a playlist URL (hard-capped at {MAX_PLAYLIST_ITEMS} items)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The video/audio/playlist page URL."},
                "mode": {
                    "type": "string",
                    "enum": ["video", "audio"],
                    "description": "video (default): merged video+audio file. audio: extract audio only.",
                },
                "quality": {
                    "type": "string",
                    "description": (
                        "Only used when mode='video'. One of 'best' (default), '2160p', '1440p', "
                        "'1080p', '720p', '480p', '360p', a raw yt-dlp format-selector string, "
                        "or 'id:<format_id>' from ytdl_formats for an exact stream."
                    ),
                },
                "container": {
                    "type": "string",
                    "enum": list(_CONTAINERS),
                    "description": "Only used when mode='video'. Container to merge into. Defaults to mp4.",
                },
                "video_codec": {
                    "type": "string",
                    "enum": list(_VIDEO_CODECS),
                    "description": "Only used when mode='video'. Prefer this video codec when there's a choice. Omit for no preference.",
                },
                "audio_format": {
                    "type": "string",
                    "enum": list(_AUDIO_FORMATS),
                    "description": "Only used when mode='audio'. Defaults to mp3.",
                },
                "audio_quality": {
                    "type": "string",
                    "description": "Only used when mode='audio'. VBR '0' (best) to '10' (worst), or a literal bitrate like '192K' (default).",
                },
                "embed_thumbnail": {"type": "boolean", "description": "Embed the video's thumbnail as cover art / thumbnail track. Default false."},
                "embed_metadata": {"type": "boolean", "description": "Embed title/uploader/etc. metadata into the file. Default false."},
                "embed_chapters": {"type": "boolean", "description": "Embed chapter markers, when available. Default false."},
                "write_subs": {"type": "boolean", "description": "Download subtitles (manual + auto-generated) as separate files. Default false."},
                "embed_subs": {"type": "boolean", "description": "Also embed subtitles into the video file itself (mode='video' only, implies write_subs is useful). Default false."},
                "sub_langs": {"type": "string", "description": "Comma-separated subtitle language codes, e.g. 'en' (default) or 'en,es'. Only used when write_subs or embed_subs is true."},
                "sponsorblock_remove": {
                    "type": "string",
                    "description": "Comma-separated SponsorBlock categories to cut out (e.g. 'sponsor,selfpromo'). YouTube only, ignored elsewhere. Omit for none.",
                },
                "output_dir": {
                    "type": "string",
                    "description": (
                        "Custom save location. Relative paths resolve against the user's home "
                        "directory, absolute paths are used as-is (same rule as write_file). "
                        "Omit to use the default ~/.jarvis/downloads/, which also gets an inline "
                        "playable card in the UI — a custom output_dir does not."
                    ),
                },
                "rate_limit_kbps": {"type": "integer", "description": "Cap download speed in KB/s. Omit for no limit."},
                "playlist": {"type": "boolean", "description": f"Allow more than one item from a playlist URL, capped at {MAX_PLAYLIST_ITEMS}. Default false (single video only)."},
                "playlist_items": {
                    "type": "string",
                    "description": "Only used when playlist=true. yt-dlp item-spec, e.g. '1-5' or '1,3,5'. Defaults to the first items up to the cap.",
                },
                "skip_if_downloaded": {"type": "boolean", "description": "Skip (rather than re-download) URLs already fetched before, tracked in a persistent archive file. Default false."},
                "background": {
                    "type": "boolean",
                    "description": (
                        "true: don't wait. Queue it in the Download Hub and return at once; the hub downloads "
                        "it in the background (pause / retry / history in Menu > Downloads; check with dl_status). "
                        "Use for long or large downloads, or when the person doesn't need the file right now. "
                        "Not with playlist or output_dir. Default false: download now and offer the file."
                    ),
                },
            },
            "required": ["url"],
        },
    },
]

YTDL_TOOLS = {
    "ytdl_info": ytdl_info,
    "ytdl_formats": ytdl_formats,
    "ytdl_download": ytdl_download,
}
