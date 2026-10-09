"""Which collected files may leave the PC for a chat, and in what shape (L.42.3).

Sending a file to a chat is exfiltration if the wrong person can trigger it: a
screenshot shows the owner's desktop and `present_file` takes any path. So:

  * OWNER ONLY. `prepare()` returns nothing for anyone else. (A guest who is
    allowed tools still never receives a file; this is checked here AND by
    handle_message not opening a collector for them.)
  * ONLY THE ASKING THREAD. Nothing here takes a recipient; the gateway's own
    `send_file` is bound to the thread the message came from.
  * AN ALLOW-LIST OF WHAT: image types, plus PDF and plain text through
    `present_file`; never a folder, never a file that is not a regular file,
    never more than MAX_FILES per reply, never over the size cap.
  * RATE LIMITED, ON DISK, like `send_dm`: at most RATE_PER_THREAD files per
    thread and RATE_OVERALL in all per hour (`channels-set discord
    max_files_per_hour N` changes the per-thread figure). A restart does not
    reset it. Files over the limit are not sent and the reply says so.
  * TOO BIG IS SAID, NOT SILENT. A PNG over the cap is re-encoded as a JPEG
    (and shrunk) when Pillow is installed; otherwise it is reported.

The size cap is deliberately under what Discord documents for free uploads (the
limit has changed more than once); `channels-set discord max_upload_mb N`
overrides it.
"""

import os
import tempfile
import time
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
FILE_EXT = IMAGE_EXT | {".pdf", ".txt", ".md", ".csv", ".json", ".log"}
DEFAULT_MAX_MB = 8
MAX_FILES = 4
RATE_WINDOW_SECONDS = 3600
RATE_PER_THREAD = 20
RATE_OVERALL = 60


def _rate_file():
    from . import directory
    return directory.CHANNELS_DIR / "media_sends.json"


def _load_sends(now):
    """{key: [timestamps within the window]}; an unreadable file reads as empty
    (the same choice send_dm makes -- a rate limit is not a permission)."""
    from .. import atomic_io
    data = atomic_io.read_json(_rate_file(), default={}, expect=dict)
    cutoff = now - RATE_WINDOW_SECONDS
    clean = {}
    for k, stamps in data.items():
        if isinstance(stamps, list):
            kept = [t for t in stamps if isinstance(t, (int, float)) and t > cutoff]
            if kept:
                clean[k] = kept
    return clean


def _per_thread_limit(cfg):
    try:
        return int(max(1, min(int((cfg or {}).get("max_files_per_hour") or RATE_PER_THREAD), 200)))
    except (TypeError, ValueError):
        return RATE_PER_THREAD


def take_allowance(platform, thread_id, wanted, cfg=None, now=None):
    """How many of `wanted` files may go now; records that many. 0 when limited."""
    if wanted <= 0:
        return 0
    from .. import atomic_io
    now = time.time() if now is None else now
    sends = _load_sends(now)
    key = "%s:%s" % (platform, thread_id)
    mine, everyone = sends.get(key, []), sends.get("_all", [])
    room = min(_per_thread_limit(cfg) - len(mine), RATE_OVERALL - len(everyone))
    n = max(0, min(wanted, room))
    if n:
        for k in (key, "_all"):
            sends.setdefault(k, []).extend([now] * n)
        atomic_io.write_json(_rate_file(), sends)
    return n


def _cap_bytes(cfg):
    try:
        mb = float((cfg or {}).get("max_upload_mb") or DEFAULT_MAX_MB)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_MB
    return int(max(1.0, min(mb, 100.0)) * 1024 * 1024)


def _shrink(path, cap):
    """A copy of an image under `cap` bytes (JPEG, scaled down), or None."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 -- Pillow is optional
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            scale = 1.0
            for _ in range(6):
                work = im if scale >= 1.0 else im.resize(
                    (max(1, int(im.width * scale)), max(1, int(im.height * scale))))
                fd, out = tempfile.mkstemp(prefix="jarvis-send-", suffix=".jpg")
                os.close(fd)
                work.save(out, "JPEG", quality=85, optimize=True)
                if os.path.getsize(out) <= cap:
                    return out
                os.unlink(out)
                scale *= 0.7
    except Exception:  # noqa: BLE001 -- a bad image is reported, not raised
        return None
    return None


def prepare(items, is_owner, cfg=None, platform="", thread_id=""):
    """items: [(path, kind)] from media_out.end().

    Returns (files, notes, temp_paths): `files` is [(path_to_send, display_name)],
    `notes` is plain sentences about anything that was NOT sent, and
    `temp_paths` are re-encoded copies the caller must delete after sending.
    When `platform` is given the hourly rate limit applies (a caller that passes
    neither, such as a unit test of the type rules, is not rate limited).
    """
    if not is_owner or not items:
        return [], [], []
    cap = _cap_bytes(cfg)
    files, notes, temps = [], [], []
    for raw, kind in items:
        if len(files) >= MAX_FILES:
            notes.append("More than %d files in one reply -- the rest were not sent." % MAX_FILES)
            break
        p = Path(raw)
        try:
            real = p.resolve()
            if not real.is_file():
                notes.append("%s is not a regular file, so it was not sent." % p.name)
                continue
            ext = real.suffix.lower()
            allowed = IMAGE_EXT if kind == "image" else FILE_EXT
            if ext not in allowed:
                notes.append("%s (%s) is not a file type Jarvis sends to chat." % (p.name, ext or "no extension"))
                continue
            size = real.stat().st_size
            if size > cap:
                small = _shrink(real, cap) if ext in IMAGE_EXT else None
                if small:
                    temps.append(small)
                    files.append((small, real.stem + ".jpg"))
                else:
                    notes.append("%s is %.1f MB, over the %.0f MB limit, so it was not sent."
                                 % (p.name, size / 1048576.0, cap / 1048576.0))
                continue
            files.append((str(real), real.name))
        except OSError as exc:
            notes.append("%s could not be read (%s)." % (p.name, exc))
    if platform and files:
        allowed = take_allowance(platform, thread_id, len(files), cfg)
        if allowed < len(files):
            for path, _name in files[allowed:]:
                if path in temps:
                    temps.remove(path)
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            files = files[:allowed]
            notes.append("Hourly file limit reached for this chat, so some pictures were not sent. Try again later.")
    return files, notes, temps


def cleanup(temps):
    for t in temps or []:
        try:
            os.unlink(t)
        except OSError:
            pass
