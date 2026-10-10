"""Pictures kept WITH a conversation, so the owner can see them again later.

WHY THIS EXISTS
---------------
A picture sent to Jarvis -- from the Ask Jarvis panel, or by someone on Discord or
Instagram -- reaches the model only as a DESCRIPTION (channels/media_in.py; the main tool
loop is text-only on purpose). The conversation record used to hold that description text
and nothing else, so the picture itself was gone the moment the vision call finished and
the owner could never look at what had actually been sent. This module is where the bytes
now go: one small file per picture under ``~/.jarvis/conv_images/``, referenced from the
exchange that carried it (``exchange["images"]``) and served back to the browser by
web/server.js (``/api/conv-images/<id>``).

WHAT IS STORED, AND WHAT IS NOT
-------------------------------
* Only bytes whose first bytes really are png / jpeg / gif / webp (``media_in.sniff_image``).
  The file's extension comes from what the bytes ARE, never from the sender's file name, so
  a name can neither choose the extension nor smuggle a path.
* The id is ``ci_<24 hex>.<ext>`` and is the ONLY thing that ever leaves this module toward
  the browser. ``path_for`` re-validates it against a strict pattern before it touches the
  disk, so a stray ``../`` can't escape the folder.
* A size cap per file (``MAX_BYTES``, 25 MB, the same ceiling ``media_in._max_bytes`` clamps
  to) and at most ``MAX_PER_MESSAGE`` per message.
* A reference carries ``id``, a display ``name`` (clipped, control characters removed) and
  ``size``. Never a path, never a URL the sender gave.

LIFETIME
--------
Deleting or clearing a conversation deletes its pictures (``delete_many``): the owner
asking for a conversation to go away must not leave its pictures behind, the same rule the
raw event log follows. Nothing here ever raises into an ask -- a picture that can't be kept
is simply not kept, and the ask carries on.
"""

import re
import secrets
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
IMAGES_DIR = JARVIS_DIR / "conv_images"

MAX_BYTES = 25 * 1048576
MAX_PER_MESSAGE = 3
MAX_NAME = 80

_ID_RE = re.compile(r"^ci_[0-9a-f]{24}\.(png|jpg|gif|webp)$")

_CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def kind_of(raw):
    """'png' / 'jpg' / 'gif' / 'webp' by the bytes' own signature, or '' when it is none of them."""
    head = bytes(raw[:16]) if raw else b""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return ""


def is_valid_id(image_id):
    return isinstance(image_id, str) and bool(_ID_RE.match(image_id))


def content_type(image_id):
    """The Content-Type for a valid id, '' for anything else."""
    if not is_valid_id(image_id):
        return ""
    return _CONTENT_TYPES.get(image_id.rsplit(".", 1)[1], "")


def path_for(image_id):
    """The file for a valid id, or None. Never a path built from unchecked text."""
    if not is_valid_id(image_id):
        return None
    return IMAGES_DIR / image_id


def _clean_name(name):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(name or "")).strip()
    return text[:MAX_NAME] or "picture"


def save(raw, name=""):
    """Keep one picture. Returns ``{"id", "name", "size"}`` or None when it can't be kept.

    Never raises."""
    try:
        if not raw or len(raw) > MAX_BYTES:
            return None
        kind = kind_of(raw)
        if not kind:
            return None
        image_id = "ci_%s.%s" % (secrets.token_hex(12), kind)
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        target = IMAGES_DIR / image_id
        tmp = IMAGES_DIR / (image_id + ".tmp")
        tmp.write_bytes(bytes(raw))
        tmp.replace(target)
        return {"id": image_id, "name": _clean_name(name), "size": len(raw)}
    except Exception:  # noqa: BLE001 -- keeping a picture must never fail an ask
        return None


def clean_refs(refs):
    """The references from a record (or a caller) reduced to the three safe fields, valid ids
    only, at most MAX_PER_MESSAGE. Anything else in a ref is dropped."""
    out = []
    for r in refs if isinstance(refs, (list, tuple)) else []:
        if not isinstance(r, dict) or not is_valid_id(r.get("id")):
            continue
        try:
            size = int(r.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"id": r["id"], "name": _clean_name(r.get("name")), "size": max(0, size)})
        if len(out) >= MAX_PER_MESSAGE:
            break
    return out


def delete_many(refs):
    """Remove the files the references name. Returns how many were removed. Never raises."""
    removed = 0
    for r in refs if isinstance(refs, (list, tuple)) else []:
        try:
            p = path_for(r.get("id") if isinstance(r, dict) else None)
            if p is not None and p.exists():
                p.unlink()
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    return removed


def refs_in_exchanges(exchanges):
    """Every picture reference carried by a list of exchanges (a conversation file's or a
    rebuilt one), in order."""
    out = []
    for ex in exchanges if isinstance(exchanges, (list, tuple)) else []:
        if isinstance(ex, dict):
            out.extend(clean_refs(ex.get("images")))
    return out
