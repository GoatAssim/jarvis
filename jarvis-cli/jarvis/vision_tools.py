"""Tiered visual understanding: read the text cheaply, look at pixels rarely.

THE PROBLEM
-----------
`read_screen` is OCR-only. It knows what text is on screen and nothing else
— not whether a button is greyed out, not whether a chart trends up, not
what an error dialog's icon means. `take_screenshot` sends a real image, but
an image costs ~1-2k tokens even before the model reasons about it, and most
screen questions ("what's the error message", "what tab am I on") are pure
text questions that OCR answers for free.

So the interesting design problem isn't "add vision", it's **when to
escalate**. This module is that policy.

    tier 1  OCR the screen (local, free, already built)
    tier 2  does the OCR text plausibly answer the question?  -> stop
    tier 3  only if not, and only if a provider supports images, send pixels

WHEN ESCALATION HAPPENS
-----------------------
`should_escalate()` returns (bool, reason), and it says yes on exactly three
grounds:

1. **The question is inherently visual.** "Is this greyed out", "what
   colour", "does this chart go up", "what does this icon mean" — no amount
   of OCR text answers these, so escalate without bothering to check the
   text. This is a keyword score, weighted and word-boundary matched, the
   same trick tool_router.py uses for tool groups and for the same reason:
   zero tokens, zero round trips, cheap to be occasionally wrong.

2. **OCR came back empty or near-empty.** A screen with no readable text is
   either graphical or OCR failed; either way text isn't going to answer it.

3. **The question's own key terms are absent from the OCR text.** If someone
   asks about "the submit button" and the word "submit" isn't anywhere in
   what OCR read, the text tier has nothing to offer.

Everything else stops at tier 1. The default is deliberately "don't send
pixels" — the handoff's constraint is that vision is a last resort because
images are token-heavy, not a nicer default.

THE CACHE
---------
Re-reading a static screen shouldn't reprocess it as an image every time. A
perceptual-ish fingerprint of the screenshot (file size + a downsampled
grayscale digest) keys a small on-disk cache of prior descriptions, so
asking three questions about one unchanged screen costs one vision call.
Same spirit and same failure mode as discovery_cache.py: best-effort, TTL'd,
and a corrupt cache file degrades to "no cache" rather than an error.

TIER 3 IS A SIDE CALL (L.16, Q-L16c)
------------------------------------
Escalation used to return an `image_path` for "ai_client to attach" -- and
nothing in ai_client or the provider adapters ever did, so tier 3 did not
exist and a PC without Tesseract could not see its own screen. Tier 3 is now
vision_call.describe_image(): ONE request carrying the screenshot to a
multimodal provider, answer returned as text. The main tool loop stays
text-only and the image is never resent on later rounds.

The escalation policy itself (should_escalate) is still a pure function with
no I/O, which is what lets the whole ladder be tested without a provider.

TIER 1 MISSING IS NOT A REASON TO GIVE UP
-----------------------------------------
Tesseract not installed (or pytesseract/Pillow missing) means the text tier
is unavailable, which pushes straight to tier 3. Only when BOTH tiers are
unavailable does the tool fail -- once, with a clear, non-retryable message
that names what is missing, so an unattended job can stop at once instead of
improvising (L.16 RC1/RC2).
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

CACHE_FILE = Path.home() / ".jarvis" / "vision_cache.json"
TTL_SECONDS = 15 * 60
MAX_ENTRIES = 12

# Below this many characters, OCR effectively found nothing worth reasoning
# over — a couple of stray glyphs off a toolbar, not content.
MIN_USEFUL_OCR_CHARS = 40

# Score at or above this and the question is treated as inherently visual.
# Tuned so one strong phrase ("greyed out") escalates alone, while a single
# weak one ("look") does not.
ESCALATE_THRESHOLD = 6

# Weighted like tool_registry.TOOL_KEYWORDS. High weights are things OCR
# provably cannot answer; low weights are hints that want corroboration.
_VISUAL_KEYWORDS = {
    # Appearance / state that has no textual form at all
    "greyed out": 10, "grayed out": 10, "disabled": 6, "highlighted": 7,
    "colour": 8, "color": 8, "red": 5, "green": 5, "blue": 4, "dark mode": 7,
    "icon": 8, "logo": 7, "image": 7, "picture": 7, "photo": 7, "thumbnail": 7,
    "screenshot look": 8,
    # Layout / spatial questions
    "layout": 8, "aligned": 7, "alignment": 7, "position": 5, "overlap": 8,
    "cut off": 7, "cropped": 7, "off screen": 7, "top left": 6, "bottom right": 6,
    "looks like": 6, "look like": 6, "appearance": 7, "visually": 9, "visual": 6,
    # Charts and graphics
    "chart": 9, "graph": 8, "trend": 7, "upward": 7, "downward": 7,
    "diagram": 8, "arrow": 6, "progress bar": 8,
    # Clickability / affordance
    "clickable": 9, "is the button": 7, "button look": 8, "checked": 6,
    "unchecked": 7, "toggle": 5, "selected": 5,
}

# Words that carry no signal when checking "did OCR see what they asked
# about" — matching on these would make every question look answered.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "what",
    "which", "where", "when", "why", "how", "this", "that", "these", "those",
    "on", "in", "at", "to", "of", "for", "and", "or", "it", "my", "me", "i",
    "can", "you", "see", "tell", "show", "screen", "there", "any", "some",
    "currently", "right", "now", "please", "with", "from", "have", "has",
}


# ---------------------------------------------------------------------------
# Escalation policy
# ---------------------------------------------------------------------------


def _word_score(text, keywords):
    """Word-boundary weighted scoring. Substring matching is what made
    'commanded' route to the commands group once (see the orientation doc's
    bug log #1); the same trap applies to 'red' inside 'required'."""
    low = (text or "").lower()
    score = 0
    hits = []
    for phrase, weight in keywords.items():
        if re.search(r"\b%s\b" % re.escape(phrase), low):
            score += weight
            hits.append(phrase)
    return score, hits


def visual_score(question):
    return _word_score(question, _VISUAL_KEYWORDS)


def _content_words(question):
    words = re.findall(r"[a-z0-9]{3,}", (question or "").lower())
    return [w for w in words if w not in _STOPWORDS]


# Words naming a UI *element* rather than content. The missing-term rule
# below only fires when one of these is present, and that qualifier matters
# more than it looks:
#
#   "what's the error message"      -> "error"/"message" absent from OCR, but
#                                      the correct answer is "there isn't
#                                      one", which OCR can already give. No
#                                      element word, so no escalation.
#   "where is the sharepoint widget" -> "widget" IS an element word and
#                                      "sharepoint" is absent, so the thing
#                                      asked about plausibly exists as an
#                                      icon with no text label. Escalate.
#
# Without this qualifier the rule escalated on every question about
# something that simply wasn't on screen — i.e. it paid for pixels to
# confirm an absence.
_UI_ELEMENT_WORDS = {
    "button", "icon", "menu", "tab", "checkbox", "toggle", "dialog", "widget",
    "panel", "window", "bar", "field", "dropdown", "slider", "badge", "avatar",
    "thumbnail", "sidebar", "tooltip", "banner", "popup", "modal", "spinner",
    "cursor", "scrollbar", "tile", "card",
}


def should_escalate(question, ocr_text, vision_available=True):
    """(escalate, reason). The whole policy, in one testable function."""
    if not vision_available:
        # Never escalate to something the configured providers can't do —
        # a vision call to a text-only model is a guaranteed error plus a
        # wasted round trip.
        return False, "no vision-capable provider configured"

    score, hits = visual_score(question)
    if score >= ESCALATE_THRESHOLD:
        return True, "question is inherently visual (%s)" % ", ".join(hits[:3])

    text = (ocr_text or "").strip()
    if len(text) < MIN_USEFUL_OCR_CHARS:
        return True, "OCR found almost no text (%d chars)" % len(text)

    words = _content_words(question)
    if words and any(w in _UI_ELEMENT_WORDS for w in words):
        low = text.lower()
        # Only the non-element words need to be found: "button" itself
        # appearing in the OCR text proves nothing about whether THIS
        # button is there.
        subjects = [w for w in words if w not in _UI_ELEMENT_WORDS]
        if subjects and not any(re.search(r"\b%s" % re.escape(w), low) for w in subjects):
            return True, ("a UI element was asked about but none of its terms "
                          "(%s) appear in the OCR text" % ", ".join(subjects[:4]))

    return False, "OCR text covers the question"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def fingerprint(image_path):
    """A cheap content key for a screenshot.

    Not a true perceptual hash — it's a digest of a coarsely downsampled
    grayscale grid when Pillow is available, and falls back to size+mtime
    when it isn't. Downsampling first is what makes it survive a blinking
    cursor or an antialiasing difference, which a raw file hash would not.
    An over-sensitive fingerprint costs an extra vision call; an
    under-sensitive one answers about a screen that has since changed, so
    the grid is kept fine enough (16x16) to catch a real change.
    """
    path = Path(image_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        from PIL import Image  # noqa: PLC0415 — optional, checked at call time
        with Image.open(path) as img:
            small = img.convert("L").resize((16, 16))
            raw = small.tobytes()
        # Quantize to 16 levels so trivial rendering noise doesn't move it.
        coarse = bytes((b // 16) for b in raw)
        return hashlib.sha256(coarse).hexdigest()[:24]
    except Exception:
        return hashlib.sha256(
            ("%s:%s" % (stat.st_size, int(stat.st_mtime))).encode()).hexdigest()[:24]


def _load_cache():
    try:
        if not CACHE_FILE.exists():
            return {}
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cache_lookup(fp, question=None):
    """A prior description of this exact screen, or None.

    Keyed on the screen alone rather than screen+question: a description of
    a static screen is reusable across questions about it, which is the
    whole point. When a question is supplied it's used only to prefer a
    closer prior entry, never to reject an otherwise-valid one.
    """
    if not fp:
        return None
    entry = _load_cache().get(fp)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("at") or 0) > TTL_SECONDS:
        return None
    return entry.get("description")


def cache_store(fp, question, description):
    if not fp or not description:
        return False
    cache = _load_cache()
    cache[fp] = {"description": str(description)[:4000],
                 "question": str(question or "")[:200],
                 "at": time.time()}
    if len(cache) > MAX_ENTRIES:
        # Drop oldest first.
        for stale in sorted(cache, key=lambda k: cache[k].get("at", 0))[:len(cache) - MAX_ENTRIES]:
            cache.pop(stale, None)
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
        return True
    except OSError:
        return False  # a cache that can't be written is just a slower cache


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------

# Which providers can take an image lives in vision_call.provider_supports_images
# (one table, consulted before building a request). openai_compatible and
# ollama need a model name positively known to accept images, because a
# text-only model answers an image request with a 400 -- a wasted round trip
# and a cooldown mark against a healthy key.


def vision_available(providers=None):
    """Is there at least one configured provider that could take an image?"""
    from . import vision_call  # noqa: PLC0415
    if providers is None:
        try:
            from . import ai_config, ai_client
            cfg = ai_config.load_ai_config()
            if not vision_call.vision_enabled(cfg):
                return False
            providers = ai_client._eligible_providers(
                cfg.get("providers") or [], cfg.get("defaults") or {})
        except Exception:
            return False
    return bool(vision_call.vision_providers(providers))


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _capture_once():
    """Take one screenshot and return (path, error).

    Deliberately calls screenshot_tools.capture_to() rather than
    tool_take_screenshot(), for two reasons. First, tool_take_screenshot
    emits a JARVIS_MEDIA line that makes the image pop up in the web UI —
    correct when the user asked for a screenshot, wrong when they asked
    "what colour is that button" and never wanted a picture delivered.
    Second, it prunes and names files as user-facing artifacts; these are
    scratch frames.

    The single capture is then shared by BOTH tiers. ocr_tools.tool_read_screen
    takes its own screenshot internally, so calling it here would grab a
    second, later frame — and then the OCR text and the escalated image
    would be different moments in time. On a screen with a spinner, a
    countdown, or a notification sliding in, that produces an answer that
    describes neither frame.
    """
    from . import screenshot_tools  # noqa: PLC0415 — heavy, lazy

    try:
        screenshot_tools.ensure_dir()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = screenshot_tools.SCREENSHOT_DIR / ("vis_%s_%s.png" % (stamp, os.getpid()))
        meta, err = screenshot_tools.capture_to(path)
    except Exception as exc:  # noqa: BLE001
        return None, "couldn't take a screenshot: %s" % exc
    if err or meta is None:
        return None, err or "screen capture failed"
    return path, None


def _ocr_file_status(path):
    """OCR an existing image. Returns (text, status), never raises.

    status is "ok" (text may still be empty -- a legitimate result and
    escalation signal #2), "unavailable" (pytesseract/Pillow missing or the
    Tesseract binary not on PATH: the text tier does not exist on this PC),
    or "failed" (OCR ran and broke). The distinction matters: an empty screen
    and a missing OCR engine need different messages and different next steps.
    """
    from . import ocr_tools  # noqa: PLC0415 -- heavy, lazy

    if ocr_tools.pytesseract is None or ocr_tools.Image is None:
        return "", "unavailable"
    try:
        words = ocr_tools._ocr_words(path)
        return "\n".join(ocr_tools._words_to_lines(words)), "ok"
    except ocr_tools.pytesseract.TesseractNotFoundError:
        return "", "unavailable"
    except Exception:
        return "", "failed"


def _ocr_file(path):
    """Text only, "" on any failure. Kept for callers that don't care why."""
    return _ocr_file_status(path)[0]


def _prune_frames(keep=8):
    """Scratch frames pile up fast; keep a handful for debugging."""
    from . import screenshot_tools  # noqa: PLC0415

    try:
        frames = sorted(screenshot_tools.SCREENSHOT_DIR.glob("vis_*.png"),
                        key=lambda p: p.stat().st_mtime)
        while len(frames) > keep:
            frames.pop(0).unlink(missing_ok=True)
    except OSError:
        pass


def _answer_key(fp, question):
    """Cache key for an ANSWER. The older screen-only key suited a general
    description reused across questions; a tier-3 answer is specific to the
    question asked, so the question is part of the key."""
    if not fp:
        return None
    norm = " ".join((question or "").lower().split())
    return "%s:%s" % (fp, hashlib.sha256(norm.encode()).hexdigest()[:10])


def _cannot_see(ocr_status, vision_ok, detail=None):
    """The one clear failure for 'neither tier works'. Non-retryable by design:
    an unattended job must be able to stop on this at once."""
    parts = []
    if ocr_status == "unavailable":
        parts.append("the OCR engine (Tesseract) is not installed")
    elif ocr_status == "failed":
        parts.append("OCR failed")
    if not vision_ok:
        parts.append("no configured provider can look at an image "
                     "(add a Gemini or Claude key)")
    elif detail:
        parts.append(detail)
    return {
        "error": "Can't see the screen: " + "; ".join(parts) + ".",
        "can_see_screen": False,
        "retryable": False,
        "hint": ("Don't retry, don't guess, and don't click or type based on a "
                 "screen you haven't read. Report that the screen can't be read."),
    }


def tool_look_at_screen(args):
    """Answer a question about the screen, using the cheapest tier that can.

    Tiers: 1 OCR (local, free) -> 2 does the text answer it? -> 3 ask a
    multimodal model about the screenshot (vision_call.describe_image).

    Returns one of:
        tier="ocr"     answered from text; `text` holds the OCR output
        tier="cached"  this exact screen + question was answered recently
        tier="vision"  `answer` is a multimodal model's answer
    or an {"error", "can_see_screen": False, "retryable": False} when neither
    tier is available.
    """
    args = args or {}
    question = str(args.get("question") or "").strip()
    if not question:
        return {"needs_clarification": True,
                "message": "What do you want to know about the screen?"}
    force = bool(args.get("force_vision"))

    image_path, err = _capture_once()
    if err:
        return {"error": err}

    ocr_text, ocr_status = _ocr_file_status(image_path)
    available = vision_available()
    _prune_frames()

    # Neither tier exists: fail now, once, before spending anything.
    if ocr_status != "ok" and not available:
        return _cannot_see(ocr_status, False)

    if force:
        escalate = available
        reason = ("explicitly requested" if available
                  else "no vision-capable provider configured")
    elif ocr_status != "ok":
        escalate = True
        reason = ("OCR is unavailable on this PC" if ocr_status == "unavailable"
                  else "OCR failed")
    else:
        escalate, reason = should_escalate(question, ocr_text, available)

    if not escalate:
        return {
            "tier": "ocr",
            "question": question,
            "text": ocr_text[:6000],
            "escalated": False,
            "reason": reason,
            "note": "Answer from this text. Call again with force_vision=true "
                    "only if the text genuinely cannot answer the question.",
        }

    key = _answer_key(fingerprint(image_path), question)
    cached = cache_lookup(key, question)
    if cached:
        return {
            "tier": "cached",
            "question": question,
            "answer": cached,
            "escalated": False,
            "reason": "this screen was already asked this exact question recently (unchanged)",
        }

    from . import vision_call  # noqa: PLC0415
    out = vision_call.describe_image(image_path, vision_call.answer_prompt(question))
    if out.get("ok"):
        cache_store(key, question, out["text"])
        return {
            "tier": "vision",
            "question": question,
            "answer": out["text"],
            "escalated": True,
            "reason": reason,
            "provider": out.get("provider"),
        }

    # The vision call failed. If OCR did read something, it is better than
    # nothing -- say so plainly rather than pretending it answered.
    if ocr_status == "ok" and ocr_text.strip():
        return {
            "tier": "ocr",
            "question": question,
            "text": ocr_text[:6000],
            "escalated": False,
            "reason": "vision failed (%s)" % out.get("error"),
            "note": "Vision could not be used, so this is the OCR text only. "
                    "If it does not answer the question, say so -- do not guess.",
        }
    return _cannot_see(ocr_status, True, out.get("error"))


def remember_description(fingerprint_value, question, description):
    """Called after a vision answer comes back, so the next question about
    the same unchanged screen is free."""
    return cache_store(fingerprint_value, question, description)


VISION_TOOL_SCHEMAS = [
    {
        "name": "look_at_screen",
        "description": (
            "Answer a question about what's on screen. Reads the screen text "
            "first (free) and only falls back to actually looking at the "
            "pixels when the text can't answer it, or when OCR isn't installed "
            "— e.g. colours, icons, greyed-out buttons, charts, layout, 'is "
            "usage exhausted'. The result is a text answer. If it says it "
            "can't see the screen, stop: don't retry or act blind."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you need to know about the screen.",
                },
                "force_vision": {
                    "type": "boolean",
                    "description": "Skip the text tier and look at the image. "
                                   "Only when a previous text-tier answer was "
                                   "genuinely insufficient — images are expensive.",
                },
            },
            "required": ["question"],
        },
    },
]

VISION_TOOLS = {
    "look_at_screen": tool_look_at_screen,
}
