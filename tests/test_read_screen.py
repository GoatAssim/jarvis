"""Tests for jarvis/ocr_tools.py's read_screen — the "MCP schema
translation is best-effort" gap's sibling ask, closing the "read screen
(not screenshot)" gap: a tool that returns plain OCR'd text instead of an
image. No real screen capture or Tesseract binary is exercised; both the
capture step and the OCR step are monkeypatched.

Runnable directly: python3 tests/test_read_screen.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ocr_tools, screenshot_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


class _FakeTesseractNotFoundError(Exception):
    pass


class _FakePytesseract:
    TesseractNotFoundError = _FakeTesseractNotFoundError


def _word(text, block=1, par=1, line=1, word=1, top=0, left=0, conf=90.0):
    return {"text": text, "left": left, "top": top, "width": 10, "height": 10,
            "conf": conf, "block": block, "par": par, "line": line, "word": word}


def _patched(monkeypatch_words=None, capture_error=None):
    """Context-manager-free patch/restore helper — simpler than importing
    contextlib for this small a set of patches."""
    orig_pytesseract = ocr_tools.pytesseract
    orig_image = ocr_tools.Image
    orig_ocr_words = ocr_tools._ocr_words
    orig_capture = screenshot_tools.capture_to

    ocr_tools.pytesseract = _FakePytesseract()
    ocr_tools.Image = object()  # just needs to be non-None

    def fake_capture_to(path):
        if capture_error:
            return None, capture_error
        return {"path": str(path)}, None
    screenshot_tools.capture_to = fake_capture_to

    def fake_ocr_words(image_path):
        if monkeypatch_words is None:
            return []
        if isinstance(monkeypatch_words, Exception):
            raise monkeypatch_words
        return monkeypatch_words
    ocr_tools._ocr_words = fake_ocr_words

    def restore():
        ocr_tools.pytesseract = orig_pytesseract
        ocr_tools.Image = orig_image
        ocr_tools._ocr_words = orig_ocr_words
        screenshot_tools.capture_to = orig_capture
    return restore


def test_read_screen_returns_reconstructed_lines_in_reading_order():
    words = [
        _word("world", line=1, word=2, top=0, left=60),
        _word("hello", line=1, word=1, top=0, left=0),
        _word("second", line=2, word=1, top=30, left=0),
        _word("line", line=2, word=2, top=30, left=50),
    ]
    restore = _patched(monkeypatch_words=words)
    try:
        result = ocr_tools.tool_read_screen({})
        check("reports ok", result.get("ok") is True, result)
        check("words within a line are joined in reading order (left-to-right by word_num)",
              result["text"].splitlines()[0] == "hello world", result)
        check("lines are ordered top-to-bottom",
              result["text"].splitlines() == ["hello world", "second line"], result)
        check("word_count matches the OCR'd words", result["word_count"] == 4, result)
        check("line_count matches the reconstructed lines", result["line_count"] == 2, result)
    finally:
        restore()


def test_read_screen_never_returns_an_image_field():
    words = [_word("hi")]
    restore = _patched(monkeypatch_words=words)
    try:
        result = ocr_tools.tool_read_screen({})
        check("no image/screenshot/base64 field is ever present",
              not any(k in result for k in ("image", "screenshot", "base64", "png")), result)
    finally:
        restore()


def test_read_screen_filters_by_min_confidence():
    words = [
        _word("keep", line=1, word=1, conf=95),
        _word("drop", line=1, word=2, conf=10, left=60),
    ]
    restore = _patched(monkeypatch_words=words)
    try:
        result = ocr_tools.tool_read_screen({"min_confidence": 50})
        check("low-confidence words are dropped", result["text"] == "keep", result)
        check("word_count reflects the filtered set", result["word_count"] == 1, result)
    finally:
        restore()


def test_read_screen_handles_empty_screen():
    restore = _patched(monkeypatch_words=[])
    try:
        result = ocr_tools.tool_read_screen({})
        check("an empty screen is still ok, just empty text", result == {
            "ok": True, "text": "", "line_count": 0, "word_count": 0,
        }, result)
    finally:
        restore()


def test_read_screen_reports_capture_failure():
    restore = _patched(capture_error="no display available")
    try:
        result = ocr_tools.tool_read_screen({})
        check("a capture failure is reported as an error, not a crash",
              "error" in result and "no display available" in result["error"], result)
    finally:
        restore()


def test_read_screen_reports_missing_tesseract_binary():
    restore = _patched(monkeypatch_words=_FakeTesseractNotFoundError("not found"))
    try:
        result = ocr_tools.tool_read_screen({})
        check("a missing Tesseract binary gets the friendly install-note error",
              "error" in result and "tesseract" in result["error"].lower(), result)
    finally:
        restore()


def test_read_screen_reports_missing_pytesseract_package():
    orig = ocr_tools.pytesseract
    ocr_tools.pytesseract = None
    try:
        result = ocr_tools.tool_read_screen({})
        check("a missing pytesseract package is a clear error",
              "error" in result and "pytesseract" in result["error"].lower(), result)
    finally:
        ocr_tools.pytesseract = orig


def test_read_screen_is_registered():
    check("read_screen is in OCR_TOOLS", "read_screen" in ocr_tools.OCR_TOOLS)
    check("read_screen has a schema entry",
          any(s["name"] == "read_screen" for s in ocr_tools.OCR_TOOL_SCHEMAS))
    schema = next(s for s in ocr_tools.OCR_TOOL_SCHEMAS if s["name"] == "read_screen")
    check("the schema description clarifies this isn't a screenshot",
          "screenshot" in schema["description"].lower() and "not" in schema["description"].lower(),
          schema["description"])


for fn in [
    test_read_screen_returns_reconstructed_lines_in_reading_order,
    test_read_screen_never_returns_an_image_field,
    test_read_screen_filters_by_min_confidence,
    test_read_screen_handles_empty_screen,
    test_read_screen_reports_capture_failure,
    test_read_screen_reports_missing_tesseract_binary,
    test_read_screen_reports_missing_pytesseract_package,
    test_read_screen_is_registered,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
