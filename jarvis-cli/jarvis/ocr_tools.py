"""OCR-based screen interaction for Jarvis — the "sighted" tier that
desktop_tools.py deliberately left out.

Two tools live here: click_on_text() (find text, click it) and
read_screen() (find all text, return it as plain lines — no image, no
screenshot artifact, just what the screen currently says). Both share the
same capture-then-OCR pipeline below.

This is a separate module on purpose: desktop_tools.py's own docstring
says vision/OCR tools "cost a real screenshot + extra processing per call"
and "belong in a separate module if/when they're wanted", to keep the
blind (cheap, mouse/keyboard-only) and sighted (screenshot + recognition)
tools easy to tell apart and audit independently.

Screen capture is NOT reimplemented here — it reuses
screenshot_tools.capture_to(), the same DPI-aware Windows/mss capture
tool_take_screenshot uses, so there's exactly one place that knows how to
grab the desktop. The difference: this module captures to a throwaway
temp file, never calls ensure_dir()/_prune()/_emit_media(), and deletes
the file immediately after OCR — the image is processed locally and is
never saved to ~/.jarvis/screenshots, shown to the user, or sent to the
model, matching the same "pixels never leave this function" spirit as
take_screenshot's note.

Engine: pytesseract, which wraps the Tesseract OCR binary. That binary is
NOT a pip dependency — `pip install pytesseract` alone is not enough; the
Tesseract binary itself must also be installed and on PATH (a separate
installer on Windows: https://github.com/UB-Mannheim/tesseract/wiki).
Both pieces are checked at call time and reported separately so a missing
binary doesn't look like a missing pip package.
"""

import tempfile
from pathlib import Path

from . import screenshot_tools

try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None

try:
    import pyautogui
except ImportError:
    pyautogui = None


VALID_BUTTONS = {"left", "right", "middle"}

# Two OCR "misses" are reported differently: pip package missing vs. the
# actual Tesseract binary missing (the part people get stuck on).
_TESSERACT_INSTALL_NOTE = (
    "OCR needs both `pip install pytesseract Pillow` AND the Tesseract "
    "OCR binary itself installed and on PATH (pytesseract only wraps it, "
    "it doesn't bundle it) — on Windows: "
    "https://github.com/UB-Mannheim/tesseract/wiki, on macOS: `brew install "
    "tesseract`, on Linux: `apt install tesseract-ocr`."
)


def _no_pytesseract():
    return {"error": f"pytesseract/Pillow not installed. {_TESSERACT_INSTALL_NOTE}"}


def _no_tesseract_binary(detail):
    return {"error": f"Tesseract binary not found on PATH. {_TESSERACT_INSTALL_NOTE} ({detail})"}


def _no_pyautogui():
    return {"error": "pyautogui is not installed. Install it with: pip install pyautogui"}


def _ocr_words(image_path):
    """Run OCR and return a flat list of recognized words with boxes,
    grouped enough (block/par/line/word_num) to reconstruct multi-word
    phrases. Raises pytesseract.TesseractNotFoundError if the binary is
    missing — caller translates that into a friendly error.
    """
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        words.append({
            "text": text,
            "left": int(data["left"][i]),
            "top": int(data["top"][i]),
            "width": int(data["width"][i]),
            "height": int(data["height"][i]),
            "conf": conf,
            "block": data["block_num"][i],
            "par": data["par_num"][i],
            "line": data["line_num"][i],
            "word": data["word_num"][i],
        })
    return words


def _line_key(w):
    return (w["block"], w["par"], w["line"])


def _phrase_candidates(words, query, max_span=4):
    """Slide a window of 1..max_span consecutive same-line words and test
    each joined phrase against `query` (case-insensitive substring, either
    direction so both 'save' matching 'Save As' and 'save as' matching
    'Save As' work). Returns a list of {text, left, top, width, height,
    conf} boxes — one per matching span, box is the union of its words,
    conf is the mean of the matched words' confidences.
    """
    q = query.strip().lower()
    if not q:
        return []
    by_line = {}
    for w in words:
        by_line.setdefault(_line_key(w), []).append(w)
    for line_words in by_line.values():
        line_words.sort(key=lambda w: w["word"])

    candidates = []
    for line_words in by_line.values():
        n = len(line_words)
        for start in range(n):
            joined = ""
            for span in range(1, min(max_span, n - start) + 1):
                chunk = line_words[start:start + span]
                joined = " ".join(c["text"] for c in chunk)
                jl = joined.lower()
                if q in jl or jl in q:
                    left = min(c["left"] for c in chunk)
                    top = min(c["top"] for c in chunk)
                    right = max(c["left"] + c["width"] for c in chunk)
                    bottom = max(c["top"] + c["height"] for c in chunk)
                    conf = sum(c["conf"] for c in chunk) / len(chunk)
                    candidates.append({
                        "text": joined,
                        "left": left,
                        "top": top,
                        "width": right - left,
                        "height": bottom - top,
                        "conf": conf,
                    })
    # Dedup near-identical boxes (same text+position found via multiple spans),
    # keep the highest-confidence copy of each.
    best = {}
    for c in candidates:
        key = (c["text"].lower(), c["left"], c["top"])
        if key not in best or c["conf"] > best[key]["conf"]:
            best[key] = c
    ranked = sorted(best.values(), key=lambda c: c["conf"], reverse=True)
    return ranked


def _words_to_lines(words):
    """Reconstruct reading-order lines from _ocr_words()'s flat word list —
    same (block, par, line) grouping tool_click_on_text uses to find
    phrases, just joined into whole lines instead of matched against a
    query. Lines are ordered by their top-most word's vertical position,
    top of screen first, matching how a person reads it.
    """
    by_line = {}
    for w in words:
        by_line.setdefault(_line_key(w), []).append(w)
    lines = []
    for line_words in by_line.values():
        line_words.sort(key=lambda w: w["word"])
        text = " ".join(w["text"] for w in line_words)
        top = min(w["top"] for w in line_words)
        lines.append((top, text))
    lines.sort(key=lambda t: t[0])
    return [text for _top, text in lines]


def tool_read_screen(args=None):
    if pytesseract is None or Image is None:
        return _no_pytesseract()
    args = args or {}
    try:
        min_confidence = float(args.get("min_confidence", 0))
    except (TypeError, ValueError):
        min_confidence = 0.0

    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="jarvis_ocr_", suffix=".png")
        import os
        os.close(fd)
        tmp_path = Path(tmp_name)

        meta, err = screenshot_tools.capture_to(tmp_path)
        if err or meta is None:
            return {"error": f"screen capture failed: {err or 'unknown error'}"}

        try:
            words = _ocr_words(tmp_path)
        except pytesseract.TesseractNotFoundError as e:
            return _no_tesseract_binary(str(e))
        except Exception as e:
            return {"error": f"OCR failed: {e}"}
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    if min_confidence > 0:
        words = [w for w in words if w["conf"] >= min_confidence]
    lines = _words_to_lines(words)
    text = "\n".join(lines)
    return {
        "ok": True,
        "text": text,
        "line_count": len(lines),
        "word_count": len(words),
    }


def tool_click_on_text(args=None):
    if pytesseract is None or Image is None:
        return _no_pytesseract()
    args = args or {}
    text = args.get("text")
    if not text:
        return {"error": "text is required"}
    do_click = args.get("click", True)
    button = (args.get("button") or "left").lower()
    if button not in VALID_BUTTONS:
        return {"error": f"button must be one of {sorted(VALID_BUTTONS)}"}
    try:
        ambiguity_margin = float(args.get("ambiguity_margin", 8.0))
    except (TypeError, ValueError):
        ambiguity_margin = 8.0

    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="jarvis_ocr_", suffix=".png")
        import os
        os.close(fd)
        tmp_path = Path(tmp_name)

        meta, err = screenshot_tools.capture_to(tmp_path)
        if err or meta is None:
            return {"error": f"screen capture failed: {err or 'unknown error'}"}

        try:
            words = _ocr_words(tmp_path)
        except pytesseract.TesseractNotFoundError as e:
            return _no_tesseract_binary(str(e))
        except Exception as e:
            return {"error": f"OCR failed: {e}"}
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    candidates = _phrase_candidates(words, str(text))
    if not candidates:
        return {"ok": False, "error": "text not found on screen"}

    def center(c):
        return c["left"] + c["width"] // 2, c["top"] + c["height"] // 2

    top = candidates[0]
    close = [c for c in candidates if top["conf"] - c["conf"] <= ambiguity_margin]
    ambiguous = len(close) > 1

    def as_candidate_payload(c):
        x, y = center(c)
        return {"matched_text": c["text"], "x": x, "y": y, "confidence": round(c["conf"], 1)}

    if not do_click:
        return {
            "ok": True,
            "clicked": False,
            "candidates": [as_candidate_payload(c) for c in candidates[:8]],
        }

    if ambiguous:
        return {
            "ok": True,
            "clicked": False,
            "ambiguous": True,
            "note": "Multiple similarly-confident matches — pass click=false to inspect, or re-call with more specific text.",
            "candidates": [as_candidate_payload(c) for c in close[:8]],
        }

    if pyautogui is None:
        return _no_pyautogui()

    x, y = center(top)
    try:
        pyautogui.click(x=x, y=y, button=button)
    except Exception as e:
        return {"error": f"click_on_text found the text but the click failed: {e}"}

    return {
        "ok": True,
        "clicked": True,
        "matched_text": top["text"],
        "x": x,
        "y": y,
        "confidence": round(top["conf"], 1),
        "button": button,
    }


OCR_TOOL_SCHEMAS = [
    {
        "name": "read_screen",
        "description": (
            "Read the text currently visible on screen via local OCR — "
            "returns plain text lines, NOT an image or a screenshot; no "
            "pixels are ever sent to you or saved anywhere. Use this "
            "instead of take_screenshot when you just need to know what a "
            "window says (e.g. checking whether another program printed "
            "'done' or an error) rather than needing to actually see the "
            "screen's layout/visuals. Cheaper than a screenshot round-trip "
            "through you: the OCR happens locally and only the recognized "
            "text comes back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "min_confidence": {
                    "type": "number",
                    "description": (
                        "Drop recognized words below this OCR confidence "
                        "(0-100) before reconstructing lines. Default 0 "
                        "(keep everything Tesseract returned)."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "click_on_text",
        "description": (
            "Find text visible on screen (via a screenshot + local OCR — the "
            "image is processed locally and is NEVER sent to you or saved for "
            "the user) and click its center point. Use this to click a button "
            "or label by what it says (e.g. 'Save', 'Save As', 'Skip Ad') "
            "when there's no keyboard shortcut and you don't already know its "
            "coordinates. Costs a real screenshot + OCR pass, so prefer "
            "hotkey/press_key when a shortcut exists. If more than one "
            "similarly-confident match is found, nothing is clicked and the "
            "candidates are returned instead so you can disambiguate or "
            "re-call with more specific text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to find on screen, e.g. 'Save' or 'Save As'.",
                },
                "click": {
                    "type": "boolean",
                    "description": "If false, only locate and return candidates — don't click. Default true.",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button to click with (default 'left').",
                },
                "ambiguity_margin": {
                    "type": "number",
                    "description": (
                        "OCR-confidence gap (0-100) within which two matches are "
                        "treated as tied and neither is auto-clicked (default 8)."
                    ),
                },
            },
            "required": ["text"],
        },
    },
]

OCR_TOOLS = {
    "read_screen": tool_read_screen,
    "click_on_text": tool_click_on_text,
}