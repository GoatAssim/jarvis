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

WHAT THIS MODULE DOESN'T DO
---------------------------
It doesn't call a model. `describe_screen()` returns a *decision* plus the
image path when escalation is warranted; `ai_client` is what actually
attaches the image. Keeping the policy free of I/O is what lets the whole
escalation ladder be tested without a provider (see tests/test_vision.py).
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

# Provider types whose adapters can carry an image part. Mirrors the shape of
# reasoning.CAPABILITIES, and for the same reason: one table, consulted
# before building a request, rather than a per-adapter branch.
VISION_CAPABLE_TYPES = {
    "anthropic": True,
    "gemini": True,
    "openai_compatible": True,   # most, but see the model check below
    "ollama": True,              # only for multimodal local models
    "cohere": False,
}

# Model names that are known text-only despite living on a vision-capable
# provider type. Cheap guard against a guaranteed-400 round trip.
_TEXT_ONLY_MODEL_RE = re.compile(
    r"(whisper|embed|tts|moderation|rerank|instruct-text|-text-only)", re.I)


def vision_available(providers=None):
    """Is there at least one configured provider that could take an image?"""
    if providers is None:
        try:
            from . import ai_config, ai_client
            cfg = ai_config.load_ai_config()
            providers = ai_client._eligible_providers(
                cfg.get("providers") or [], cfg.get("defaults") or {})
        except Exception:
            return False
    for provider in providers or []:
        if not isinstance(provider, dict):
            continue
        if not VISION_CAPABLE_TYPES.get(provider.get("type")):
            continue
        if _TEXT_ONLY_MODEL_RE.search(str(provider.get("model") or "")):
            continue
        return True
    return False


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


def _ocr_file(path):
    """OCR an existing image. Returns text (possibly empty) — never raises.

    Empty text is a legitimate, meaningful result here (it's escalation
    signal #2), so every failure mode collapses to "" rather than an error
    shape. A missing Tesseract binary means the text tier is simply
    unavailable, which correctly pushes the decision toward vision.
    """
    from . import ocr_tools  # noqa: PLC0415 — heavy, lazy

    if ocr_tools.pytesseract is None or ocr_tools.Image is None:
        return ""
    try:
        words = ocr_tools._ocr_words(path)
        return "\n".join(ocr_tools._words_to_lines(words))
    except Exception:
        return ""


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


def tool_look_at_screen(args):
    """Answer a question about the screen, using the cheapest tier that can.

    Returns one of three shapes:

        tier="ocr"     -> answered from text; `text` holds the OCR output
        tier="cached"  -> this exact screen was described recently
        tier="vision"  -> `image_path` is set; the caller attaches the image

    The tool never *sends* the image itself. It hands back a decision, and
    ai_client attaches the file — so the escalation policy stays a pure
    function and this module needs no provider, no network and no key.
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

    ocr_text = _ocr_file(image_path)
    available = vision_available()

    if force:
        escalate = available
        reason = ("explicitly requested" if available
                  else "no vision-capable provider configured")
    else:
        escalate, reason = should_escalate(question, ocr_text, available)

    _prune_frames()

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

    fp = fingerprint(image_path)
    cached = cache_lookup(fp, question)
    if cached:
        return {
            "tier": "cached",
            "question": question,
            "description": cached,
            "text": ocr_text[:2000],
            "escalated": False,
            "reason": "this screen was already described recently (unchanged)",
        }

    return {
        "tier": "vision",
        "question": question,
        "image_path": str(image_path),
        "fingerprint": fp,
        "text": ocr_text[:2000],
        "escalated": True,
        "reason": reason,
        "note": "Look at the attached image to answer. The OCR text is "
                "included for context but did not answer the question.",
    }


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
            "pixels when the text can't answer it — e.g. colours, icons, "
            "greyed-out buttons, charts, layout. Prefer read_screen when you "
            "only need text."
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
