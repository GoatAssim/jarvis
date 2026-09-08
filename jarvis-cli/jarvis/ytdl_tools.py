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
    """Machine line for the web UI — see app.js's addAskPromptTrace, which
    watches for JARVIS_MEDIA the same way it already does for screenshots."""
    label = (title or filename or "").replace("\t", " ").replace("\n", " ")
    print(f"JARVIS_MEDIA\t{kind}\t{job_id}\t{filename}\t{label}", file=sys.stderr, flush=True)


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


def ytdl_download(arguments):
    arguments = arguments or {}
    url = arguments.get("url")
    if not url or not isinstance(url, str):
        return {"error": "url is required"}

    mode = (arguments.get("mode") or "video").strip().lower()
    if mode not in ("video", "audio"):
        return {"error": f"unknown mode: {mode!r} (expected 'video' or 'audio')"}

    quality = (arguments.get("quality") or "best").strip()
    container = (arguments.get("container") or "mp4").strip().lower()
    if container not in _CONTAINERS:
        return {"error": f"unknown container: {container!r} (expected one of {', '.join(_CONTAINERS)})"}
    video_codec = (arguments.get("video_codec") or "").strip().lower() or None
    if video_codec and video_codec not in _VIDEO_CODECS:
        return {"error": f"unknown video_codec: {video_codec!r} (expected one of {', '.join(_VIDEO_CODECS)})"}

    audio_format = (arguments.get("audio_format") or "mp3").strip().lower()
    if audio_format not in _AUDIO_FORMATS:
        return {"error": f"unknown audio_format: {audio_format!r} (expected one of {', '.join(_AUDIO_FORMATS)})"}
    audio_quality = str(arguments.get("audio_quality") or "192K").strip()

    embed_thumbnail = bool(arguments.get("embed_thumbnail", False))
    embed_metadata = bool(arguments.get("embed_metadata", False))
    embed_chapters = bool(arguments.get("embed_chapters", False))
    write_subs = bool(arguments.get("write_subs", False))
    embed_subs = bool(arguments.get("embed_subs", False))
    sub_langs = (arguments.get("sub_langs") or "en").strip()

    sponsorblock_remove = [c.strip() for c in (arguments.get("sponsorblock_remove") or "").split(",") if c.strip()]

    rate_limit_kbps = arguments.get("rate_limit_kbps")
    if rate_limit_kbps is not None:
        try:
            rate_limit_kbps = int(rate_limit_kbps)
        except (TypeError, ValueError):
            return {"error": "rate_limit_kbps must be a number"}

    playlist = bool(arguments.get("playlist", False))
    playlist_items = (arguments.get("playlist_items") or "").strip() or None
    skip_if_downloaded = bool(arguments.get("skip_if_downloaded", False))

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
    outtmpl = str(job_dir / "%(title).150B [%(id)s].%(ext)s")

    postprocessors = []
    if mode == "audio":
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": audio_quality,
        })

    opts = _base_opts(
        outtmpl=outtmpl,
        restrictfilenames=True,
        writethumbnail=embed_thumbnail,
        embedthumbnail=embed_thumbnail,
        addmetadata=embed_metadata,
        addchapters=embed_chapters,
        embedsubtitles=embed_subs and mode == "video",
        writesubtitles=write_subs,
        writeautomaticsub=write_subs,
        subtitleslangs=[l.strip() for l in sub_langs.split(",") if l.strip()] if write_subs else None,
        sponsorblock_remove=sponsorblock_remove or None,
        postprocessors=postprocessors or None,
        noplaylist=not playlist,
    )

    if mode == "audio":
        opts["format"] = "bestaudio/best"
    else:
        if quality.startswith("id:"):
            fmt_id = quality[3:].strip()
            opts["format"] = f"{fmt_id}+bestaudio/{fmt_id}/best"
        else:
            opts["format"] = _QUALITY_MAP.get(quality, quality)
        opts["merge_output_format"] = container
        if video_codec:
            opts["format_sort"] = [f"vcodec:{video_codec}"]

    if rate_limit_kbps:
        opts["ratelimit"] = rate_limit_kbps * 1024

    if playlist:
        opts["max_downloads"] = MAX_PLAYLIST_ITEMS
        if playlist_items:
            opts["playlist_items"] = playlist_items
        else:
            opts["playlist_items"] = f"1:{MAX_PLAYLIST_ITEMS}"

    if skip_if_downloaded:
        ensure_dir()
        opts["download_archive"] = str(ARCHIVE_FILE)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
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
