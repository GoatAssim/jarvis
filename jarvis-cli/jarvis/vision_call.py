"""Tier 3 of tiered vision: ask a multimodal model about ONE image (L.16, Q-L16c).

WHY THIS FILE EXISTS
--------------------
vision_tools.py had the escalation *policy* (OCR first, pixels only when
OCR can't answer) and said "ai_client attaches the image". Nothing did. No
provider adapter in ai_providers.py carries an image part, so the vision tier
returned an `image_path` that went nowhere and a PC without Tesseract simply
could not see its own screen.

THE DESIGN: A SIDE CALL, NOT A CHANGE TO THE MAIN LOOP
------------------------------------------------------
The main tool loop stays text-only. When look_at_screen (or read_screen, when
Tesseract is missing) needs pixels understood, it calls describe_image()
here: one request, one image, one question, plain text back. The tool result
the main model sees is that text.

That is deliberate:

  * No adapter in ai_providers.py changes, so the cache-prefix invariant, the
    round caps and every provider's tool loop are untouched (AGENTS.md).
  * The image is sent exactly once. Putting it into the main conversation
    would resend it on every later round (ai_providers documents rounds as
    roughly quadratic) — the L.16 trace already showed 47% of input tokens
    going on repeated resends.
  * It can use a DIFFERENT provider than the main loop. A scheduled ask that
    ends up on a text-only Groq model can still see the screen through a
    Gemini key.
  * The bytes never touch the conversation log: this module POSTs with
    `requests` directly instead of ai_providers._post_json, which would write
    the whole base64 payload into the per-conversation request log.

WHICH PROVIDERS COUNT
---------------------
provider_supports_images() is deliberately conservative for
openai_compatible and ollama: a text-only model there answers an image
request with an HTTP 400, which costs a round trip and a cooldown mark for a
healthy key. So those two types need a model name that is positively known
to take images. Gemini and Anthropic models all do (embedding/audio-only
names excepted).

NEVER RAISES. Every failure comes back as {"ok": False, "error": "..."}.
"""

import base64
import io
import re

import requests

DEFAULT_TIMEOUT = 45
# Providers tried at most; keys per provider tried at most. A screen question
# is not worth walking a 10-key pool.
MAX_PROVIDERS = 3
MAX_KEYS_PER_PROVIDER = 2
# Longest edge before downscaling. Plenty to read UI text; keeps the image's
# token cost bounded on a 4K display.
MAX_EDGE_PX = 1600
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ANSWER_CHARS = 4000

_TEXT_ONLY_RE = re.compile(
    r"(whisper|embed|tts|moderation|rerank|instruct-text|-text-only|"
    r"guard|transcribe|imagen|veo)", re.I)

# Wording providers use when the model itself can't take an image.
_IMAGE_REJECTED_RE = re.compile(
    r"(image|multimodal|vision|media|modalit)[^.]{0,60}(not supported|unsupported|"
    r"not support|invalid|cannot|can't)|"
    r"(does not|doesn't|do not) support[^.]{0,40}(image|vision|multimodal)", re.I)

# Models on openai_compatible / ollama endpoints known to accept images.
_IMAGE_MODEL_RE = re.compile(
    r"(vision|\bvl\b|[-_.]vl[-_.:0-9]|llava|bakllava|moondream|minicpm-v|"
    r"pixtral|llama-?4|scout|maverick|gemma-?3|gpt-4o|gpt-4\.1|gpt-4-turbo|"
    r"gpt-5|\bo[134]\b|o[134]-|gemini|claude|qwen2?\.?5?-?vl|qwen3-vl|"
    r"mistral-(small|medium)-3|internvl|granite3\.2-vision)", re.I)


def provider_supports_images(provider):
    """True if this configured provider can plausibly take an image."""
    if not isinstance(provider, dict):
        return False
    ptype = provider.get("type")
    model = str(provider.get("model") or "")
    if _TEXT_ONLY_RE.search(model):
        return False
    if ptype in ("gemini", "anthropic"):
        return True
    if ptype in ("openai_compatible", "ollama"):
        return bool(_IMAGE_MODEL_RE.search(model))
    return False


def vision_providers(providers):
    return [p for p in (providers or []) if provider_supports_images(p)]


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------


def load_image(path):
    """(bytes, mime, error). Downscales large screens when Pillow is there."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, None, "couldn't read the screenshot: %s" % exc
    if not raw:
        return None, None, "the screenshot file is empty"
    mime = "image/png"
    try:
        from PIL import Image  # noqa: PLC0415 — optional
        img = Image.open(io.BytesIO(raw))
        if max(img.size) > MAX_EDGE_PX:
            scale = MAX_EDGE_PX / float(max(img.size))
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 — a decode problem shouldn't hide the file; send as-is
        pass
    if len(raw) > MAX_IMAGE_BYTES:
        return None, None, ("the screenshot is %.1f MB, over the %d MB limit "
                            "(install Pillow so it can be downscaled)"
                            % (len(raw) / 1048576.0, MAX_IMAGE_BYTES // 1048576))
    return raw, mime, None


# ---------------------------------------------------------------------------
# Per-provider request/response shapes. Pure — easy to test.
# ---------------------------------------------------------------------------


def build_request(provider, key, prompt, b64, mime):
    """(url, headers, payload) for one provider type, or (None, None, error)."""
    ptype = provider.get("type")
    model = provider.get("model") or ""
    if ptype == "gemini":
        base = provider.get("base_url") or (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
        url = base.format(model=model) if "{model}" in base else base
        return url, {"x-goog-api-key": key or "", "Content-Type": "application/json"}, {
            "contents": [{"role": "user", "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": b64}}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1500},
        }
    if ptype == "anthropic":
        url = provider.get("base_url") or "https://api.anthropic.com/v1/messages"
        return url, {"x-api-key": key or "", "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"}, {
            "model": model, "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt}]}],
        }
    if ptype == "openai_compatible":
        url = provider.get("base_url") or ""
        if not url:
            return None, None, "no base_url configured"
        return url, {"Authorization": "Bearer %s" % (key or ""),
                     "Content-Type": "application/json"}, {
            "model": model, "temperature": 0, "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}}]}],
        }
    if ptype == "ollama":
        url = provider.get("base_url") or "http://localhost:11434/api/chat"
        return url, {"Content-Type": "application/json"}, {
            "model": model, "stream": False,
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        }
    return None, None, "provider type %r can't take images" % ptype


def parse_response(provider_type, data):
    """Answer text from a decoded response body, or ''. Never raises."""
    try:
        if provider_type == "gemini":
            parts = (((data.get("candidates") or [{}])[0].get("content") or {})
                     .get("parts") or [])
            # Skip hidden "thought" parts; they aren't the answer.
            return "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict) and not p.get("thought")).strip()
        if provider_type == "anthropic":
            return "".join(b.get("text", "") for b in (data.get("content") or [])
                           if isinstance(b, dict) and b.get("type") == "text").strip()
        if provider_type == "openai_compatible":
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            return (content or "").strip()
        if provider_type == "ollama":
            return ((data.get("message") or {}).get("content") or "").strip()
    except (AttributeError, IndexError, TypeError):
        pass
    return ""


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def _post(url, headers, payload, timeout):
    """(response, error). Never raises, never logs the payload."""
    try:
        return requests.post(url, headers=headers, json=payload, timeout=timeout), None
    except requests.exceptions.Timeout:
        return None, "timed out after %ss" % timeout
    except requests.exceptions.ConnectionError:
        return None, "couldn't connect (network issue, or the service is down)"
    except requests.exceptions.RequestException as exc:
        return None, "request failed: %s" % exc


def vision_enabled(cfg=None):
    """Privacy switch. A screenshot goes to a cloud provider when tier 3 runs,
    which "OCR stays on this PC" never did. `defaults.vision_enabled: false`
    in ai_config.json turns tier 3 off everywhere (default: on)."""
    try:
        if cfg is None:
            from . import ai_config  # noqa: PLC0415
            cfg = ai_config.load_ai_config()
        return (cfg.get("defaults") or {}).get("vision_enabled", True) is not False
    except Exception:  # noqa: BLE001 -- an unreadable config must not silently send screenshots
        return False


def _load_providers():
    from . import ai_config, ai_client  # noqa: PLC0415 — import cycle, see actions/_template.py
    cfg = ai_config.load_ai_config()
    if not vision_enabled(cfg):
        return []
    return ai_client._eligible_providers(cfg.get("providers") or [], cfg.get("defaults") or {})


def describe_image(image_path, prompt, providers=None, timeout=DEFAULT_TIMEOUT):
    """Ask a vision model one question about one image.

    Returns {"ok": True, "text", "provider", "model"} or
            {"ok": False, "error", "attempts": [...]}.
    """
    try:
        if providers is None:
            providers = _load_providers()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "couldn't load the provider config: %s" % exc, "attempts": []}

    candidates = vision_providers(providers)[:MAX_PROVIDERS]
    if not candidates:
        return {"ok": False, "attempts": [],
                "error": "no configured provider can take an image (needs a Gemini or "
                         "Claude key, or an OpenAI-compatible/Ollama model with vision), "
                         "or vision is switched off (defaults.vision_enabled)"}

    raw, mime, err = load_image(image_path)
    if err:
        return {"ok": False, "error": err, "attempts": []}
    b64 = base64.b64encode(raw).decode("ascii")

    from . import ai_config, ai_providers, key_health  # noqa: PLC0415

    attempts = []
    for provider in candidates:
        name = provider.get("name") or provider.get("type")
        keys = ai_config.provider_keys(provider)
        if provider.get("type") == "ollama":
            keys = keys or [None]
        else:
            try:
                keys = key_health.order_keys(name, keys)
            except Exception:  # noqa: BLE001 — ordering is a nicety
                pass
        for key in keys[:MAX_KEYS_PER_PROVIDER]:
            url, headers, payload = build_request(provider, key, prompt, b64, mime)
            if url is None:
                attempts.append("%s: %s" % (name, payload))
                break
            resp, post_err = _post(url, headers, payload, timeout)
            reason = post_err
            if resp is not None:
                reason = ai_providers._status_reason(resp)
            if reason is None:
                try:
                    text = parse_response(provider.get("type"), resp.json())
                except ValueError:
                    text = ""
                if text:
                    try:
                        key_health.record_success(name, provider.get("model"), key)
                    except Exception:  # noqa: BLE001
                        pass
                    return {"ok": True, "text": text[:MAX_ANSWER_CHARS],
                            "provider": name, "model": provider.get("model")}
                reason = "empty response"
            attempts.append("%s: %s" % (name, reason))
            try:
                key_health.record_failure(name, provider.get("model"), key,
                                          ai_providers.classify_failure(reason), reason)
            except Exception:  # noqa: BLE001
                pass
            # This model rejecting the IMAGE itself will fail identically on
            # the provider's other keys, so move on to the next provider. A
            # bare HTTP 400 deliberately does not count (Gemini reports a bad
            # key as a 400 too; ai_providers' shape check is a whitelist for
            # the same reason).
            if (ai_providers.classify_failure(reason) == ai_providers.KIND_SHAPE
                    or _IMAGE_REJECTED_RE.search(str(reason))):
                break
    return {"ok": False, "attempts": attempts,
            "error": "no vision provider answered (%s)" % "; ".join(attempts[-3:])}


TRANSCRIBE_PROMPT = (
    "Transcribe all the readable text on this screenshot, top to bottom, left to "
    "right, one line per visual line. Output only the text. Do not describe the "
    "image and do not guess at text you cannot read."
)


def answer_prompt(question):
    return (
        "This is a screenshot of a computer screen. Answer the question about it "
        "briefly and factually. If the answer is not visible on screen, say exactly "
        "that — do not guess.\n\nQuestion: %s" % question
    )

