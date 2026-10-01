"""Tests for tiered vision (master plan L.16, Q-L16c): OCR first, then a real
multimodal call, so a missing Tesseract no longer makes "check the screen"
impossible.

Run: python3 tests/test_tiered_vision.py

No screen, no Tesseract, no network and no key is used. The capture, the OCR
status and the HTTP layer are all replaced; every test says which.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ocr_tools, screenshot_tools, tool_router, vision_call, vision_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


# 1x1 PNG, valid enough to read and base64.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc00000030101009ec8d2a50000000049454e44ae426082")

GEMINI = {"name": "gemini", "type": "gemini", "model": "gemini-2.5-flash", "api_keys": ["g1", "g2", "g3"]}
GROQ_TEXT = {"name": "groq", "type": "openai_compatible", "model": "llama-3.3-70b-versatile",
             "base_url": "https://api.groq.com/openai/v1/chat/completions", "api_keys": ["q1"]}
CLAUDE = {"name": "claude", "type": "anthropic", "model": "claude-sonnet-4-5", "api_keys": ["c1"]}


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.headers, self.text = status, body or {}, {}, ""

    def json(self):
        return self._body


def _gem_ok(text):
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


class Env:
    """Isolates every file and every collaborator these tests touch."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis_tv_test_"))
        self.png = self.tmp / "frame.png"
        self.png.write_bytes(PNG)
        self.posts = []          # every HTTP request attempted
        self.script = []         # responses handed out in order
        self.saved = {}

    def patch(self, obj, name, value):
        self.saved[(id(obj), name)] = (obj, name, getattr(obj, name))
        setattr(obj, name, value)

    def __enter__(self):
        self.patch(vision_tools, "CACHE_FILE", self.tmp / "vision_cache.json")
        self.patch(vision_tools, "_capture_once", lambda: (self.png, None))
        self.patch(vision_tools, "_prune_frames", lambda keep=8: None)

        def fake_post(url, headers, payload, timeout):
            self.posts.append((url, headers, payload))
            item = self.script.pop(0) if self.script else _Resp(500)
            return (None, item) if isinstance(item, str) else (item, None)
        self.patch(vision_call, "_post", fake_post)
        # key_health writes ~/.jarvis; keep it in the temp dir.
        from jarvis import key_health
        self.patch(key_health, "record_success", lambda *a, **k: None)
        self.patch(key_health, "record_failure", lambda *a, **k: None)
        self.patch(key_health, "order_keys", lambda name, keys, now=None: list(keys))
        return self

    def set_screen(self, ocr_text, ocr_status, providers):
        self.patch(vision_tools, "_ocr_file_status", lambda p: (ocr_text, ocr_status))
        self.patch(vision_tools, "vision_available", lambda providers_=None, _p=providers: bool(vision_call.vision_providers(_p)))
        self.patch(vision_call, "_load_providers", lambda: providers)

    def __exit__(self, *exc):
        for obj, name, val in self.saved.values():
            setattr(obj, name, val)
        shutil.rmtree(self.tmp, ignore_errors=True)


LONG_TEXT = "Usage limit reached. Your limit will reset at 5:00 PM.\n" * 3


# --------------------------------------------------------------------------- providers


def test_provider_support_matrix():
    s = vision_call.provider_supports_images
    check("gemini is vision capable", s({"type": "gemini", "model": "gemini-2.5-flash"}))
    check("claude is vision capable", s({"type": "anthropic", "model": "claude-sonnet-4-5"}))
    check("a text-only Groq llama is NOT (would 400)", not s(GROQ_TEXT))
    check("a Groq llama-4 scout is", s({"type": "openai_compatible", "model": "meta-llama/llama-4-scout-17b-16e-instruct"}))
    check("llama-3.2-90b-vision is", s({"type": "openai_compatible", "model": "llama-3.2-90b-vision-preview"}))
    check("gpt-4o via openai_compatible is", s({"type": "openai_compatible", "model": "gpt-4o-mini"}))
    check("ollama llava is", s({"type": "ollama", "model": "llava:13b"}))
    check("ollama llama3 (text) is NOT", not s({"type": "ollama", "model": "llama3.1:8b"}))
    check("cohere is NOT", not s({"type": "cohere", "model": "command-r"}))
    check("a whisper model on a vision type is NOT", not s({"type": "openai_compatible", "model": "whisper-large-v3"}))
    check("an embedding model on gemini is NOT", not s({"type": "gemini", "model": "gemini-embedding-001"}))
    check("junk is NOT", not s(None) and not s("x") and not s({}))
    check("vision_available follows the same table",
          vision_tools.vision_available([GROQ_TEXT]) is False
          and vision_tools.vision_available([GROQ_TEXT, GEMINI]) is True)


def test_privacy_switch():
    check("vision is on by default", vision_call.vision_enabled({"defaults": {}}) is True)
    check("defaults.vision_enabled=false turns it off",
          vision_call.vision_enabled({"defaults": {"vision_enabled": False}}) is False)
    check("an unreadable config fails CLOSED (no screenshot is sent)",
          vision_call.vision_enabled(object()) is False)
    from jarvis import ai_config
    saved = ai_config.load_ai_config
    ai_config.load_ai_config = lambda: {"providers": [GEMINI], "defaults": {"vision_enabled": False}}
    try:
        check("vision_available() is False when switched off, even with a Gemini key",
              vision_tools.vision_available() is False)
        check("and describe_image() makes no request", vision_call._load_providers() == [])
    finally:
        ai_config.load_ai_config = saved


# --------------------------------------------------------------------------- request / response shapes


def test_request_shapes():
    url, h, p = vision_call.build_request(GEMINI, "KEY", "what?", "B64", "image/png")
    check("gemini: model in url, key in header (not url), image inline",
          "gemini-2.5-flash:generateContent" in url and "KEY" not in url
          and h["x-goog-api-key"] == "KEY"
          and p["contents"][0]["parts"][1]["inline_data"] == {"mime_type": "image/png", "data": "B64"}, (url, p))
    url, h, p = vision_call.build_request(CLAUDE, "K", "what?", "B64", "image/png")
    blocks = p["messages"][0]["content"]
    check("anthropic: x-api-key, image block base64 + text block",
          h["x-api-key"] == "K" and blocks[0]["type"] == "image"
          and blocks[0]["source"]["data"] == "B64" and blocks[1]["text"] == "what?", p)
    prov = {"type": "openai_compatible", "model": "gpt-4o", "base_url": "https://x/v1/chat/completions"}
    url, h, p = vision_call.build_request(prov, "K", "what?", "B64", "image/png")
    check("openai-compatible: Bearer and a data: URI image_url",
          h["Authorization"] == "Bearer K"
          and p["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,B64", p)
    url, h, p = vision_call.build_request({"type": "ollama", "model": "llava"}, None, "what?", "B64", "image/png")
    check("ollama: images list, non-streaming", p["messages"][0]["images"] == ["B64"] and p["stream"] is False, p)
    check("an unsupported type returns an error, not a request",
          vision_call.build_request({"type": "cohere"}, "k", "q", "b", "m")[0] is None)
    check("openai-compatible without a base_url is an error",
          vision_call.build_request({"type": "openai_compatible", "model": "gpt-4o"}, "k", "q", "b", "m")[0] is None)


def test_response_parsing():
    pr = vision_call.parse_response
    check("gemini text parts joined, hidden thought parts skipped",
          pr("gemini", {"candidates": [{"content": {"parts": [
              {"text": "secret reasoning", "thought": True}, {"text": "Yes."}]}}]}) == "Yes.")
    check("anthropic text blocks", pr("anthropic", {"content": [{"type": "text", "text": "No."}]}) == "No.")
    check("openai-compatible message", pr("openai_compatible", {"choices": [{"message": {"content": " Blue "}}]}) == "Blue")
    check("ollama message", pr("ollama", {"message": {"content": "ok"}}) == "ok")
    for typ in ("gemini", "anthropic", "openai_compatible", "ollama"):
        check("%s: garbage body -> '' not an exception" % typ, pr(typ, {"nonsense": 1}) == "" and pr(typ, None) == "")


# --------------------------------------------------------------------------- the call


def test_describe_image_success_and_key_failover():
    with Env() as e:
        e.script = [_gem_ok("Usage is exhausted.")]
        out = vision_call.describe_image(e.png, "q", providers=[GEMINI])
        check("success returns the text and which provider answered",
              out["ok"] and out["text"] == "Usage is exhausted." and out["provider"] == "gemini", out)
        check("exactly one request was made", len(e.posts) == 1)

    with Env() as e:
        e.script = [_Resp(429, {}), _gem_ok("fine")]
        out = vision_call.describe_image(e.png, "q", providers=[GEMINI])
        check("a 429 on key 1 fails over to key 2",
              out["ok"] and len(e.posts) == 2
              and e.posts[0][1]["x-goog-api-key"] == "g1" and e.posts[1][1]["x-goog-api-key"] == "g2", out)

    with Env() as e:
        e.script = [_Resp(429, {}), _Resp(429, {}), _gem_ok("never reached")]
        out = vision_call.describe_image(e.png, "q", providers=[GEMINI])
        check("only MAX_KEYS_PER_PROVIDER keys are tried — a 10-key pool is not walked",
              not out["ok"] and len(e.posts) == vision_call.MAX_KEYS_PER_PROVIDER, (out, len(e.posts)))


def test_describe_image_provider_selection():
    with Env() as e:
        out = vision_call.describe_image(e.png, "q", providers=[GROQ_TEXT])
        check("a text-only provider is never sent an image",
              not out["ok"] and e.posts == [] and "no configured provider" in out["error"], out)

    with Env() as e:
        e.script = [_gem_ok("seen")]
        out = vision_call.describe_image(e.png, "q", providers=[GROQ_TEXT, GEMINI])
        check("the text-only provider is skipped, the vision one used",
              out["ok"] and out["provider"] == "gemini" and len(e.posts) == 1, out)

    with Env() as e:
        rejected = _Resp(400, {})
        rejected.text = "This model does not support image input"
        e.script = [rejected, _Resp(200, {"content": [{"type": "text", "text": "claude saw it"}]})]
        out = vision_call.describe_image(e.png, "q", providers=[GEMINI, CLAUDE])
        check("'model can't take images' skips the provider's other keys and moves on",
              out["ok"] and out["provider"] == "claude" and len(e.posts) == 2, (out, len(e.posts)))

    with Env() as e:
        bad_key = _Resp(400, {})
        bad_key.text = "API key not valid. Please pass a valid API key."
        e.script = [bad_key, _gem_ok("second key worked")]
        out = vision_call.describe_image(e.png, "q", providers=[GEMINI])
        check("a bare 400 (Gemini's bad-key shape) still tries the next key",
              out["ok"] and out["text"] == "second key worked" and len(e.posts) == 2, (out, len(e.posts)))

    with Env() as e:
        e.script = [_Resp(200, {"candidates": []})]
        out = vision_call.describe_image(e.png, "q", providers=[{**GEMINI, "api_keys": ["only"]}])
        check("an empty reply is a failure, not an empty success", not out["ok"] and "empty" in out["error"], out)

    with Env() as e:
        out = vision_call.describe_image(e.tmp / "missing.png", "q", providers=[GEMINI])
        check("a missing screenshot file is a clean error", not out["ok"] and "couldn't read" in out["error"], out)

    with Env() as e:
        e.script = ["timed out after 45s"] * 4
        out = vision_call.describe_image(e.png, "q", providers=[GEMINI])
        check("network errors are reported, never raised", not out["ok"] and "timed out" in out["error"], out)


def test_image_bytes_never_hit_the_conversation_log():
    """_post_json (used by every other adapter) logs the whole payload to the
    per-conversation request log. This module must not use it."""
    src = Path(vision_call.__file__).read_text(encoding="utf-8")
    check("vision_call does not call ai_providers._post_json", "_post_json(" not in src)


# --------------------------------------------------------------------------- look_at_screen


def test_tier1_answers_text_questions_without_pixels():
    with Env() as e:
        e.set_screen(LONG_TEXT, "ok", [GEMINI])
        out = vision_tools.tool_look_at_screen({"question": "what does the message say"})
        check("OCR ok + text question -> tier ocr, zero HTTP", out["tier"] == "ocr" and e.posts == [], out)


def test_missing_tesseract_falls_through_to_vision():
    """The L.16 failure: OCR missing made the task impossible."""
    with Env() as e:
        e.set_screen("", "unavailable", [GEMINI])
        e.script = [_gem_ok("Yes, the usage limit has been reached.")]
        out = vision_tools.tool_look_at_screen({"question": "is usage exhausted"})
        check("no Tesseract -> tier vision with a text answer",
              out.get("tier") == "vision" and "limit" in out["answer"] and "OCR is unavailable" in out["reason"], out)
        check("exactly one image request", len(e.posts) == 1)
        check("the result carries no image path to send anywhere", "image_path" not in out, out)


def test_neither_tier_fails_once_and_clearly():
    with Env() as e:
        e.set_screen("", "unavailable", [GROQ_TEXT])
        out = vision_tools.tool_look_at_screen({"question": "is usage exhausted"})
        check("both tiers missing -> error, no request made", "error" in out and e.posts == [], out)
        check("and it is marked non-retryable and 'can't see'",
              out["can_see_screen"] is False and out["retryable"] is False, out)
        check("it names both what is missing and what to do",
              "Tesseract" in out["error"] and "Gemini or Claude" in out["error"]
              and "don't" in out["hint"].lower(), out)


def test_vision_failure_degrades_honestly():
    with Env() as e:
        e.set_screen(LONG_TEXT, "ok", [GEMINI])
        e.script = [_Resp(503, {})] * 3
        out = vision_tools.tool_look_at_screen({"question": "is the submit button greyed out"})
        check("vision down but OCR has text -> OCR text, labelled as such, not a fake answer",
              out["tier"] == "ocr" and "vision failed" in out["reason"] and "do not guess" in out["note"], out)

    with Env() as e:
        e.set_screen("", "unavailable", [GEMINI])
        e.script = [_Resp(503, {})] * 3
        out = vision_tools.tool_look_at_screen({"question": "is usage exhausted"})
        check("vision down and no OCR -> can't-see error, non-retryable",
              "error" in out and out["retryable"] is False and out["can_see_screen"] is False, out)


def test_visual_question_with_working_ocr_still_escalates():
    with Env() as e:
        e.set_screen(LONG_TEXT, "ok", [GEMINI])
        e.script = [_gem_ok("It is grey.")]
        out = vision_tools.tool_look_at_screen({"question": "what colour is the header"})
        check("inherently visual question -> tier vision even though OCR works",
              out["tier"] == "vision" and "inherently visual" in out["reason"], out)


def test_answer_cache_is_per_question():
    with Env() as e:
        e.set_screen("", "unavailable", [GEMINI])
        e.script = [_gem_ok("A1"), _gem_ok("A2")]
        a = vision_tools.tool_look_at_screen({"question": "is usage exhausted"})
        b = vision_tools.tool_look_at_screen({"question": "Is   usage EXHAUSTED"})
        check("same screen + same question (case/space-insensitive) is served from cache, one request",
              a["tier"] == "vision" and b["tier"] == "cached" and b["answer"] == "A1" and len(e.posts) == 1, (a, b))
        c = vision_tools.tool_look_at_screen({"question": "what is the window title"})
        check("same screen, DIFFERENT question is NOT answered from the old answer",
              c["tier"] == "vision" and c["answer"] == "A2" and len(e.posts) == 2, c)


def test_force_vision_without_a_provider_does_not_call_out():
    with Env() as e:
        e.set_screen(LONG_TEXT, "ok", [GROQ_TEXT])
        out = vision_tools.tool_look_at_screen({"question": "what does it say", "force_vision": True})
        check("force_vision with no capable provider falls back to OCR, zero HTTP",
              out["tier"] == "ocr" and e.posts == [], out)


# --------------------------------------------------------------------------- read_screen fallback


class _FakeTess:
    class TesseractNotFoundError(Exception):
        pass


def _read_screen_env(e, pytess, vision_text=None, vision_err=None, vision_avail=True):
    e.patch(screenshot_tools, "capture_to", lambda path: ({"path": str(path)}, None))
    e.patch(ocr_tools, "pytesseract", pytess)
    e.patch(ocr_tools, "Image", object() if pytess is not None else None)
    e.patch(vision_tools, "vision_available", lambda providers=None: vision_avail)
    e.patch(ocr_tools, "_vision_fallback",
            lambda p: (vision_text, vision_err) if vision_text is not None else (None, vision_err or "no vision"))


def test_read_screen_falls_back_when_tesseract_missing():
    with Env() as e:
        _read_screen_env(e, None, vision_text="Usage limit reached\n\nResets 5 PM")
        out = ocr_tools.tool_read_screen({})
        check("pytesseract missing + vision available -> text via vision",
              out.get("ok") and out["source"] == "vision" and out["line_count"] == 2
              and "mistakes" in out["note"], out)

    with Env() as e:
        _read_screen_env(e, _FakeTess, vision_text="Hello")
        e.patch(ocr_tools, "_ocr_words", lambda p: (_ for _ in ()).throw(_FakeTess.TesseractNotFoundError("x")))
        out = ocr_tools.tool_read_screen({})
        check("Tesseract BINARY missing + vision available -> text via vision",
              out.get("ok") and out["source"] == "vision", out)


def test_read_screen_with_no_route_fails_fast_without_capturing():
    with Env() as e:
        e.patch(ocr_tools, "pytesseract", None)
        e.patch(vision_tools, "vision_available", lambda providers=None: False)

        def boom(path):
            raise AssertionError("must not capture when there is no tier at all")
        e.patch(screenshot_tools, "capture_to", boom)
        out = ocr_tools.tool_read_screen({})
        check("no OCR and no vision -> the original friendly error, before any capture",
              "pytesseract" in out["error"].lower(), out)


def test_read_screen_when_vision_also_fails_is_non_retryable():
    with Env() as e:
        _read_screen_env(e, None, vision_text=None, vision_err="no vision provider answered (gemini: HTTP 503)")
        out = ocr_tools.tool_read_screen({})
        check("both tiers failed -> original install note kept, vision reason added",
              "pytesseract" in out["error"].lower() and "503" in out["error"], out)
        check("and flagged non-retryable so a job stops instead of improvising",
              out["can_see_screen"] is False and out["retryable"] is False, out)


def test_read_screen_normal_ocr_path_is_unchanged():
    with Env() as e:
        e.patch(screenshot_tools, "capture_to", lambda path: ({"path": str(path)}, None))
        e.patch(ocr_tools, "pytesseract", _FakeTess)
        e.patch(ocr_tools, "Image", object())
        e.patch(ocr_tools, "_ocr_words", lambda p: [
            {"text": "Done", "left": 0, "top": 0, "width": 10, "height": 10, "conf": 90.0,
             "block": 1, "par": 1, "line": 1, "word": 1}])

        def boom(p):
            raise AssertionError("vision must not be touched when OCR works")
        e.patch(ocr_tools, "_vision_fallback", boom)
        out = ocr_tools.tool_read_screen({})
        check("OCR works -> same result shape, vision untouched",
              out == {"ok": True, "text": "Done", "line_count": 1, "word_count": 1}, out)


# --------------------------------------------------------------------------- routing (L.16 item 9)


def test_routing_for_the_incident_prompt():
    prompt = ("Check the screen to see if usage is exhausted. If usage is exhausted, do not type "
              "anything and shutdown the PC. If usage is not exhausted, type 'continue' and press "
              "Enter, then shutdown the PC.")
    names = {m[1] for m in tool_router.route(prompt).matches}
    check("the incident prompt no longer routes to schedule_watch", "schedule_watch" not in names, names)
    check("it reaches look_at_screen and power_action", {"look_at_screen", "power_action"} <= names, names)
    names = {m[1] for m in tool_router.route("check the screen every 5 minutes and tell me when it says done").matches}
    check("a genuine recurring watch still routes to schedule_watch", "schedule_watch" in names, names)


for fn in [
    test_provider_support_matrix, test_privacy_switch, test_request_shapes, test_response_parsing,
    test_describe_image_success_and_key_failover, test_describe_image_provider_selection,
    test_image_bytes_never_hit_the_conversation_log,
    test_tier1_answers_text_questions_without_pixels,
    test_missing_tesseract_falls_through_to_vision,
    test_neither_tier_fails_once_and_clearly, test_vision_failure_degrades_honestly,
    test_visual_question_with_working_ocr_still_escalates,
    test_answer_cache_is_per_question,
    test_force_vision_without_a_provider_does_not_call_out,
    test_read_screen_falls_back_when_tesseract_missing,
    test_read_screen_with_no_route_fails_fast_without_capturing,
    test_read_screen_when_vision_also_fails_is_non_retryable,
    test_read_screen_normal_ocr_path_is_unchanged,
    test_routing_for_the_incident_prompt,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
