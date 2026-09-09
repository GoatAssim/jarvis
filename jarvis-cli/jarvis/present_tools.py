"""present_file — the one tool the model should reach for whenever it wants
to actually show the user a file or folder that already exists on disk
(a search_files hit, something write_file/ytdl_download just created,
etc.), instead of just typing the path out in prose.

Note this is deliberately different from app.js's path-linkification
(linkifyPaths/makePathLink) \u2014 that's a client-side convenience that turns
*any* path-looking text the model happens to type into a clickable "open"
link, no tool call involved. present_file is for when the model wants to
actively hand the user something \u2014 a name/type/size/path card, with
Open/Reveal *and* Download, not just a click-to-open span.

Replaces what used to be the *only* way to get Reveal/Open buttons on a
search_files result: the debug dashboard's bespoke per-row rendering (now
removed \u2014 the model never actually called reveal_in_explorer/
open_file_location/open_file itself, since nothing in a normal
conversation ever surfaced them). This tool is that surface. It still
calls into those same three tools under the hood (see everything_tools.py)
\u2014 present_file's job is presenting; the actions on its card each still
resolve to a plain reveal_in_explorer / open_file_location / open_file
call, same as if the model had invoked them directly.

Two very different personalities depending on JARVIS_UI (see
web/server.js, which sets JARVIS_UI=web on every subprocess it spawns for
the web console \u2014 ask, tool-run, tool-preview \u2014 a plain terminal
`jarvis ask`/`jarvis tool-run` has no such env var):

  JARVIS_UI=web (web console): prepares a download copy (files: straight
    copy; folders: zipped first, per the brief) into its own job folder
    under ~/.jarvis/downloads/<job_id>/ \u2014 the exact same folder + the
    exact same web/server.js `/api/downloads/:jobId/:filename` route
    ytdl_tools.py's download cards already use, just reusing it for a
    different kind of job. Then emits a JARVIS_MEDIA line (see
    screenshot_tools.py's docstring for the pattern this follows) so
    app.js can render the actual card: name/type/size/path plus Open/
    Reveal/Download buttons. Skips the copy (but still shows the card,
    just without a Download button) past DOWNLOAD_SIZE_CAP, so a huge
    file/folder can't block the ask on a slow synchronous copy.

  No JARVIS_UI (plain CLI): none of the above. No job folder, no zip, no
    JARVIS_MEDIA line \u2014 just the plain name/type/size/path info, because
    a terminal has no card to click.

Either way, the file's actual bytes never reach the model \u2014 same
"placeholder, not payload" rule as take_screenshot's pixels and
ytdl_download's media: the model gets metadata to talk about, the user
gets the real thing through the UI.
"""

import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
# Same directory (and job-id prefix) ytdl_tools.py's download jobs use —
# web/server.js's /api/downloads/:jobId/:filename route doesn't care which
# tool created a job folder, only that it matches dl_<...>.
DOWNLOAD_DIR = JARVIS_DIR / "downloads"
JOB_ID_PREFIX = "dl_"
MAX_KEEP = 15  # job folders, matching ytdl_tools.py's own cap on the shared dir
DOWNLOAD_SIZE_CAP = 300 * 1024 * 1024  # 300MB — beyond this, card shows but no Download button


def _is_web_ui():
    return os.environ.get("JARVIS_UI") == "web"


def ensure_dir():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOAD_DIR


def _prune():
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


def _dir_size(path):
    """Best-effort total size of a folder — unreadable entries are just
    skipped rather than failing the whole lookup."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _emit_media(job_id, filename, name, ftype, size_bytes, path_str):
    """Machine line for the web UI — see screenshot_tools.py's docstring
    for the general pattern app.js's addAskPromptTrace watches for."""
    clean = lambda s: str(s or "").replace("\t", " ").replace("\n", " ")
    size_field = str(size_bytes) if isinstance(size_bytes, int) else "-"
    print(
        "JARVIS_MEDIA\tpresent_file\t"
        f"{job_id or '-'}\t{filename or '-'}\t{clean(name)}\t{ftype}\t{size_field}\t{clean(path_str)}",
        file=sys.stderr, flush=True,
    )


def present_file(arguments):
    arguments = arguments or {}
    raw_path = arguments.get("path")
    if not raw_path or not isinstance(raw_path, str):
        return {"error": "path is required"}

    path = Path(raw_path).expanduser()
    if not path.exists():
        return {"error": f"path not found: {path}"}

    is_folder = path.is_dir()
    ftype = "folder" if is_folder else "file"
    try:
        size_bytes = _dir_size(path) if is_folder else path.stat().st_size
    except OSError as e:
        size_bytes = None
        size_err = str(e)
    else:
        size_err = None

    base_note = (
        "File already shown to the user in the UI with its own actions. "
        "Do NOT restate the full path back to them, do NOT describe file contents, "
        "and reply with at most a short one-sentence confirmation."
    )

    if not _is_web_ui():
        # Plain CLI: info only, nothing else — see module docstring.
        result = {
            "ok": True,
            "name": path.name,
            "type": ftype,
            "path": str(path),
            "size_bytes": size_bytes,
        }
        if size_err:
            result["note"] = f"size unavailable: {size_err}"
        return result

    job_id = None
    download_filename = None
    download_note = None
    if size_bytes is not None and size_bytes <= DOWNLOAD_SIZE_CAP:
        try:
            ensure_dir()
            job_id = _new_job_id()
            job_dir = DOWNLOAD_DIR / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            if is_folder:
                archive_base = str(job_dir / path.name)
                shutil.make_archive(archive_base, "zip", root_dir=str(path.parent), base_dir=path.name)
                download_filename = f"{path.name}.zip"
            else:
                shutil.copy2(path, job_dir / path.name)
                download_filename = path.name
            _prune()
        except OSError as e:
            job_id, download_filename = None, None
            download_note = f"download copy failed: {e}"
    elif size_bytes is not None:
        download_note = f"too large to prepare for download (over {DOWNLOAD_SIZE_CAP // (1024 * 1024)}MB)"

    _emit_media(job_id, download_filename, path.name, ftype, size_bytes, str(path))

    result = {
        "ok": True,
        "name": path.name,
        "type": ftype,
        "path": str(path),
        "size_bytes": size_bytes,
        "note": base_note,
    }
    if download_note:
        result["download_note"] = download_note
    return result


PRESENT_TOOL_SCHEMAS = [
    {
        "name": "present_file",
        "compact_description": (
            "Show a file/folder in the chat as a card: name, type, size, path, plus "
            "Open/Reveal (and, in the web UI, Download \u2014 folders zipped first) actions. "
            "Use right after search_files/write_file/ytdl_download etc. instead of just "
            "typing the path. One call per file the user actually cares about."
        ),
        "short_description": "Show a file/folder to the user as a card, instead of just stating its path.",
        "description": (
            "Show a file or folder to the user right in the chat, as a card with its name, "
            "type, size and path, plus Open / Reveal in Explorer (and, in the web UI, "
            "Download \u2014 folders are zipped first) actions. Use this instead of just typing "
            "the path out in text: right after search_files finds a specific result the user "
            "asked about, or right after write_file / ytdl_download / anything else that just "
            "created something. One path per call \u2014 for a handful of results, call it once "
            "per file the user actually cares about, not the whole result list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path to the file or folder to present."},
            },
            "required": ["path"],
        },
    },
]

PRESENT_TOOLS = {
    "present_file": present_file,
}
