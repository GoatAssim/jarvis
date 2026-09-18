"""Tests for vision_tools.py — the escalation policy above all.

The policy is a pure function, so the entire escalation ladder is testable
with no screen, no Tesseract, no provider and no key. Run with
`python3 tests/test_vision.py`.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import vision_tools as v  # noqa: E402

# A realistic OCR dump: enough text that the "near-empty" rule doesn't fire,
# so the other rules are what's actually being tested.
SCREEN_TEXT = """
File  Edit  View  Run  Terminal  Help
main.py  settings.json  README.md
def build_payload(request):
    return {"ok": True}
Problems  Output  Debug Console  Terminal
Submit  Cancel
"""


def _isolate():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-vision-test-"))
    v.CACHE_FILE = tmp / "vision_cache.json"
    return tmp


# ---------------------------------------------------------------------------


def test_text_questions_do_not_escalate():
    """The default must be "don't send pixels" — that's the whole point."""
    for q in ["what's the error message",
              "which file is open",
              "what does the terminal say",
              "read me the submit button label"]:
        escalate, reason = v.should_escalate(q, SCREEN_TEXT, vision_available=True)
        assert escalate is False, (q, reason)
    print("ok  text questions stay on OCR")


def test_inherently_visual_questions_escalate():
    for q in ["is the submit button greyed out",
              "what colour is the header",
              "does this chart show an upward trend",
              "what does that icon mean",
              "is the layout cut off"]:
        escalate, reason = v.should_escalate(q, SCREEN_TEXT, vision_available=True)
        assert escalate is True, (q, reason)
        assert "inherently visual" in reason, (q, reason)
    print("ok  visual questions escalate")


def test_empty_ocr_escalates():
    escalate, reason = v.should_escalate("what is this", "", vision_available=True)
    assert escalate is True and "almost no text" in reason

    # A couple of stray glyphs is still effectively nothing.
    escalate, reason = v.should_escalate("what is this", "x  y", vision_available=True)
    assert escalate is True and "almost no text" in reason
    print("ok  empty OCR escalates")


def test_missing_terms_escalate():
    """Asked about something OCR never saw — text has nothing to offer."""
    escalate, reason = v.should_escalate(
        "where is the sharepoint widget", SCREEN_TEXT, vision_available=True)
    assert escalate is True, reason
    assert "appear in the OCR text" in reason

    # But a question whose terms ARE present stays cheap.
    escalate, _ = v.should_escalate("what does terminal show", SCREEN_TEXT, True)
    assert escalate is False

    # And asking about CONTENT that isn't on screen must NOT escalate: the
    # right answer is "there isn't one", which OCR already supports. Paying
    # for pixels to confirm an absence is the bug this qualifier fixes.
    for q in ["what's the error message", "is there a warning", "any exceptions"]:
        escalate, reason = v.should_escalate(q, SCREEN_TEXT, True)
        assert escalate is False, (q, reason)

    # An element word alone, with its subject present, also stays cheap.
    escalate, _ = v.should_escalate("where is the submit button", SCREEN_TEXT, True)
    assert escalate is False
    print("ok  missing-term rule")


def test_never_escalate_without_a_capable_provider():
    """A vision call to a text-only model is a guaranteed 400 plus a wasted
    round trip — the capability check must beat every other rule."""
    for q in ["is the button greyed out", "what colour is it", "anything at all"]:
        escalate, reason = v.should_escalate(q, "", vision_available=False)
        assert escalate is False, q
        assert "no vision-capable provider" in reason
    print("ok  capability gate wins")


def test_word_boundary_matching():
    """Substring matching is the bug tool_router already had once: 'red'
    must not match inside 'required', 'disabled' not inside 'disabledness'."""
    score, hits = v.visual_score("the field is required and the form is fine")
    assert "red" not in hits, hits
    assert score < v.ESCALATE_THRESHOLD, (score, hits)

    score, hits = v.visual_score("the text is red")
    assert "red" in hits
    print("ok  word-boundary matching")


def test_stopwords_do_not_fake_coverage():
    """If 'the/is/what' counted as found terms, every question would look
    answered by any OCR text and nothing would ever escalate."""
    words = v._content_words("what is the thing on the screen right now")
    assert "what" not in words and "the" not in words and "screen" not in words
    assert "thing" in words
    print("ok  stopword filtering")


def test_vision_available_probe():
    assert v.vision_available([{"type": "anthropic", "model": "claude-x"}]) is True
    assert v.vision_available([{"type": "gemini", "model": "gemini-x"}]) is True
    assert v.vision_available([{"type": "cohere", "model": "command"}]) is False
    assert v.vision_available([]) is False
    # A vision-capable provider type running a known text-only model.
    assert v.vision_available([{"type": "openai_compatible",
                                "model": "whisper-large-v3"}]) is False
    assert v.vision_available([{"type": "openai_compatible",
                                "model": "llama-3.2-90b-vision"}]) is True
    # Junk entries are skipped, not crashed on.
    assert v.vision_available([None, "nope", {"type": "anthropic", "model": "m"}]) is True
    print("ok  capability probe")


def test_cache_roundtrip_and_ttl():
    _isolate()
    assert v.cache_lookup("abc") is None
    v.cache_store("abc", "what colour", "The header is dark blue.")
    assert v.cache_lookup("abc") == "The header is dark blue."

    # A different screen is a different key.
    assert v.cache_lookup("def") is None

    # Expired entries are ignored rather than served stale.
    import json
    data = json.loads(v.CACHE_FILE.read_text())
    data["abc"]["at"] = 0
    v.CACHE_FILE.write_text(json.dumps(data))
    assert v.cache_lookup("abc") is None

    # No fingerprint means no cache, not a crash.
    assert v.cache_lookup(None) is None
    assert v.cache_store(None, "q", "d") is False
    print("ok  cache")


def test_cache_is_bounded():
    _isolate()
    for i in range(v.MAX_ENTRIES + 10):
        v.cache_store("fp%03d" % i, "q", "description %d" % i)
    import json
    data = json.loads(v.CACHE_FILE.read_text())
    assert len(data) <= v.MAX_ENTRIES, len(data)
    # Newest survive, oldest are evicted.
    assert "fp%03d" % (v.MAX_ENTRIES + 9) in data
    assert "fp000" not in data
    print("ok  cache bounded")


def test_corrupt_cache_degrades():
    tmp = _isolate()
    v.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    v.CACHE_FILE.write_text("{not json at all")
    assert v.cache_lookup("abc") is None      # no raise
    assert v.cache_store("abc", "q", "d") is True  # overwrites the junk
    assert v.cache_lookup("abc") == "d"
    print("ok  corrupt cache degrades")


def test_fingerprint_is_stable_and_safe():
    _isolate()
    missing = Path(tempfile.mkdtemp()) / "nope.png"
    assert v.fingerprint(missing) is None, "a missing file must not raise"

    # Two identical files fingerprint the same; different content differs.
    d = Path(tempfile.mkdtemp())
    a, b, c = d / "a.bin", d / "b.bin", d / "c.bin"
    a.write_bytes(b"x" * 5000)
    b.write_bytes(b"x" * 5000)
    c.write_bytes(b"y" * 9000)
    fa, fb, fc = v.fingerprint(a), v.fingerprint(b), v.fingerprint(c)
    assert fa and fa == fb, (fa, fb)
    assert fa != fc
    print("ok  fingerprint")


def test_tool_requires_a_question():
    out = v.tool_look_at_screen({})
    assert out.get("needs_clarification") is True
    out = v.tool_look_at_screen({"question": "   "})
    assert out.get("needs_clarification") is True
    print("ok  question required")


def test_tool_degrades_without_a_screen():
    """No display in CI — the tool must return an error shape, not raise."""
    out = v.tool_look_at_screen({"question": "what is on screen"})
    assert isinstance(out, dict)
    assert "error" in out or out.get("tier") in ("ocr", "vision", "cached"), out
    print("ok  headless degradation")


if __name__ == "__main__":
    test_text_questions_do_not_escalate()
    test_inherently_visual_questions_escalate()
    test_empty_ocr_escalates()
    test_missing_terms_escalate()
    test_never_escalate_without_a_capable_provider()
    test_word_boundary_matching()
    test_stopwords_do_not_fake_coverage()
    test_vision_available_probe()
    test_cache_roundtrip_and_ttl()
    test_cache_is_bounded()
    test_corrupt_cache_degrades()
    test_fingerprint_is_stable_and_safe()
    test_tool_requires_a_question()
    test_tool_degrades_without_a_screen()
    print("\nall vision tests passed")
