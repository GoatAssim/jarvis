"""Video/audio download tool for Jarvis, built on yt-dlp.

Two tools:
  ytdl_info      — read-only metadata lookup (title, duration, uploader,
                    thumbnail, a short list of available qualities). No
                    file written, no confirmation needed.
  ytdl_download   — actually fetches media to disk. Mutating + network, so
                    it defaults to confirm_required=True (see
                    tool_safety.DEFAULT_CONFIRM_REQUIRED) same as
                    write_file/run_custom_command.

Deliberately narrow, matching the rest of this codebase's "known
simplifications" philosophy (see file_tools.py's docstring):
  - noplaylist is always on — one URL, one file, never a whole channel
    or playlist dumped to disk from a single tool call.
  - No cookies/auth, no proxy, no arbitrary yt-dlp CLI flags exposed —
    just url / mode / quality. Anything needing a login wall or
    geo-bypass fails with a clear error instead of half-working.
  - Downloads land under ~/.jarvis/downloads/<job>/ and old job folders
    are pruned (MAX_KEEP), same pattern as screenshot_tools.py's
    _prune() — this is about not filling the disk unattended, not a
    real archive.

Each downloaded job gets its own folder (dl_<timestamp>_<hex>) rather
than one shared directory, so the web UI can serve a file back by
(job_id, filename) without any path-traversal ambiguity — see
web/server.js's /api/downloads/:jobId/:filename route, which mirrors
/api/screenshots/:name.
"""

import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
DOWNLOAD_DIR = JARVIS_DIR / "downloads"
MAX_KEEP = 15  # job folders, not files — a video+thumbnail+subs can be several files

JOB_ID_RE_PREFIX = "dl_"

_QUALITY_MAP = {
    "best": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
}

_AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav", "flac", "vorbis")


def _import_yt_dlp():
    try:
        import yt_dlp
        return yt_dlp, None
    except ImportError:
        return None, (
            "yt-dlp isn't installed — run: pip install yt-dlp "
            "(or 'pip install -e .[ytdl]' in jarvis-cli/)"
        )


def ensure_dir():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOAD_DIR


def _prune():
    """Keep only the most-recently-modified MAX_KEEP job folders. Whole
    folders are removed (a job can be more than one file: video + subs +
    thumbnail), never raises on individual failures."""
    import shutil

    jobs = sorted(
        (p for p in DOWNLOAD_DIR.glob(f"{JOB_ID_RE_PREFIX}*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    while len(jobs) > MAX_KEEP:
        try:
            shutil.rmtree(jobs.pop(0), ignore_errors=True)
        except OSError:
            break


def _new_job_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{JOB_ID_RE_PREFIX}{stamp}_{secrets.token_hex(3)}"


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


def _base_opts(**overrides):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    opts.update(overrides)
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

    opts = _base_opts(skip_download=True)
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
    quality_hint = [f"{h}p" for h in heights[:6]]

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
        "available_qualities": quality_hint,
        "note": "Metadata only — nothing downloaded. Call ytdl_download to actually fetch it.",
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
    audio_format = (arguments.get("audio_format") or "mp3").strip().lower()
    if audio_format not in _AUDIO_FORMATS:
        return {"error": f"unknown audio_format: {audio_format!r} (expected one of {', '.join(_AUDIO_FORMATS)})"}

    yt_dlp, err = _import_yt_dlp()
    if err:
        return {"error": err}

    ensure_dir()
    job_id = _new_job_id()
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(job_dir / "%(title).150B [%(id)s].%(ext)s")

    if mode == "audio":
        opts = _base_opts(
            outtmpl=outtmpl,
            restrictfilenames=True,
            format="bestaudio/best",
            postprocessors=[{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "192",
            }],
        )
    else:
        opts = _base_opts(
            outtmpl=outtmpl,
            restrictfilenames=True,
            format=_QUALITY_MAP.get(quality, quality),
            merge_output_format="mp4",
        )

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
    except yt_dlp.utils.DownloadError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"download failed: {e}"}

    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    if not filepath:
        # Fall back to whatever landed in the job folder — post-processing
        # (merge/extract-audio) can rename the extension after the fact,
        # see PART 15 of the yt-dlp reference: predicted filename != final one.
        candidates = [p for p in job_dir.iterdir() if p.is_file()]
        filepath = str(candidates[0]) if candidates else None

    if not filepath or not Path(filepath).exists():
        return {"ok": False, "error": "yt-dlp reported success but no output file was found."}

    path = Path(filepath)
    title = info.get("title") or path.stem
    _prune()
    _emit_media("download", job_id, path.name, title)

    return {
        "ok": True,
        "job_id": job_id,
        "file": path.name,
        "mode": mode,
        "title": title,
        "duration": info.get("duration"),
        "duration_str": _fmt_duration(info.get("duration")),
        "bytes": path.stat().st_size,
        "note": (
            "File saved on disk and offered to the user for playback/download in the UI. "
            "Do NOT invent details about the content, and do NOT say more than a short "
            "one-sentence confirmation that it's ready."
        ),
    }


YTDL_TOOL_SCHEMAS = [
    {
        "name": "ytdl_info",
        "description": (
            "Look up metadata (title, uploader, duration, available qualities) for a "
            "video/audio URL without downloading anything. YouTube and ~1800 other "
            "sites via yt-dlp. Use this first if the user just wants details."
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
            "Download a video or extract audio from a URL (YouTube + ~1800 other sites "
            "via yt-dlp) and offer the file to the user in the UI. Always confirmed with "
            "the user first. One URL at a time — never a whole playlist/channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The video/audio page URL."},
                "mode": {
                    "type": "string",
                    "enum": ["video", "audio"],
                    "description": "video (default): merged video+audio file. audio: extract audio only.",
                },
                "quality": {
                    "type": "string",
                    "description": (
                        "Only used when mode='video'. One of 'best' (default), '1080p', "
                        "'720p', '480p', or a raw yt-dlp format-selector string."
                    ),
                },
                "audio_format": {
                    "type": "string",
                    "enum": list(_AUDIO_FORMATS),
                    "description": "Only used when mode='audio'. Defaults to mp3.",
                },
            },
            "required": ["url"],
        },
    },
]

YTDL_TOOLS = {
    "ytdl_info": ytdl_info,
    "ytdl_download": ytdl_download,
}
