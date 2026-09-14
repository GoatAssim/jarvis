"""Pluggable text-to-speech backends for Jarvis — jarvis-enhancement-plan.md
§3.5. Mirrors Mark LIII's core/tts.py swappable-provider shape, one local
option and two online options:

    kokoro       — local, offline. Neural TTS, ~330MB model, no network
                    needed after the first download.
    edge         — online, free. Microsoft Edge's TTS, no API key.
                    Default provider (works with zero config).
    elevenlabs   — online, paid. Cloud API, best quality, needs an API key.

synthesize() returns raw audio bytes + a mime type and never touches an
output device — that split is what lets the same function serve both
callers described in this package's __init__.py docstring: cli.py's
`jarvis speak` calls speak() (synthesize + audio_io.play on this machine),
while web/server.js's /api/voice/speak endpoint calls synthesize() alone
and streams the bytes to the browser to play through *its* speaker
instead — per jarvis-enhancement-plan.md §3a, CLI and web need different
playback paths even though the synthesis backend itself is shared.
"""

import asyncio
import sys


_KOKORO_INSTALL_NOTE = (
    'kokoro not installed. pip install "jarvis-cli[voice-tts-kokoro]" '
    "(pulls in kokoro + soundfile; first call downloads the ~330MB model, "
    "then runs fully offline)."
)
_EDGE_INSTALL_NOTE = 'edge-tts not installed. pip install "jarvis-cli[voice-tts-edge]"'
_ELEVENLABS_INSTALL_NOTE = (
    'elevenlabs needs the requests package (already a base jarvis-cli '
    "dependency) plus an API key — set tts.elevenlabs.api_key and "
    "voice_id via `jarvis voice-config`."
)

_kokoro_pipeline_cache = {}  # lang_code -> KPipeline


def _kokoro_synthesize(text, settings):
    try:
        from kokoro import KPipeline
    except ImportError:
        return None, {"error": _KOKORO_INSTALL_NOTE}
    try:
        import soundfile as sf
        import numpy as np
        import io
    except ImportError:
        return None, {"error": _KOKORO_INSTALL_NOTE}

    voice = settings.get("voice", "af_heart")
    speed = float(settings.get("speed", 1.0))
    # Kokoro's language code is the voice prefix's first letter ('a' =
    # American English, 'b' = British English, ...) — see kokoro's README.
    lang_code = voice[0] if voice else "a"

    pipeline = _kokoro_pipeline_cache.get(lang_code)
    if pipeline is None:
        try:
            pipeline = KPipeline(lang_code=lang_code)
        except Exception as e:
            return None, {"error": f"couldn't load kokoro pipeline: {e}"}
        _kokoro_pipeline_cache[lang_code] = pipeline

    try:
        chunks = [audio for _graphemes, _phonemes, audio in pipeline(text, voice=voice, speed=speed)]
        if not chunks:
            return None, {"error": "kokoro produced no audio for this text"}
        audio = np.concatenate(chunks)
        buf = io.BytesIO()
        sf.write(buf, audio, 24000, format="WAV")
        return {"audio": buf.getvalue(), "mime": "audio/wav"}, None
    except Exception as e:
        return None, {"error": f"kokoro synthesis failed: {e}"}


def _edge_synthesize(text, settings):
    try:
        import edge_tts
    except ImportError:
        return None, {"error": _EDGE_INSTALL_NOTE}

    voice = settings.get("voice", "en-US-GuyNeural")
    rate = settings.get("rate", "+0%")
    volume = settings.get("volume", "+0%")

    async def _run():
        communicate = edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume)
        chunks = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.extend(chunk["data"])
        return bytes(chunks)

    try:
        audio = asyncio.run(_run())
    except Exception as e:
        return None, {"error": f"edge-tts synthesis failed: {e}"}

    if not audio:
        return None, {"error": "edge-tts returned no audio (bad voice name, or no internet)"}
    return {"audio": audio, "mime": "audio/mpeg"}, None


def _elevenlabs_synthesize(text, settings):
    try:
        import requests
    except ImportError:
        return None, {"error": _ELEVENLABS_INSTALL_NOTE}

    api_key = (settings.get("api_key") or "").strip()
    voice_id = (settings.get("voice_id") or "").strip()
    model = settings.get("model", "eleven_turbo_v2_5")

    if not api_key or not voice_id:
        return None, {
            "error": "tts.elevenlabs.api_key and tts.elevenlabs.voice_id must both be set "
                     "via `jarvis voice-config` before using the elevenlabs provider."
        }

    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": text, "model_id": model},
            timeout=30,
        )
    except requests.RequestException as e:
        return None, {"error": f"elevenlabs request failed: {e}"}

    if resp.status_code != 200:
        return None, {"error": f"elevenlabs API error {resp.status_code}: {resp.text[:300]}"}
    if not resp.content:
        return None, {"error": "elevenlabs returned no audio"}
    return {"audio": resp.content, "mime": "audio/mpeg"}, None


_BACKENDS = {
    "kokoro": _kokoro_synthesize,
    "edge": _edge_synthesize,
    "elevenlabs": _elevenlabs_synthesize,
}


def synthesize(text, config=None):
    """Synthesizes `text` with the configured provider. Returns
    {"ok": True, "provider": "...", "audio": <bytes>, "mime": "..."} on
    success, or {"error": "..."} — never raises. Does NOT play anything;
    see speak() below for that, or hand "audio"/"mime" to a browser."""
    from . import config as voice_config

    text = (text or "").strip()
    if not text:
        return {"error": "nothing to say (empty text)"}

    cfg = config or voice_config.load_voice_config()
    provider, settings = voice_config.tts_provider_and_settings(cfg)

    backend = _BACKENDS.get(provider)
    if backend is None:
        return {"error": f"unknown TTS provider '{provider}'"}

    try:
        result, error = backend(text, settings)
    except Exception as e:
        return {"error": f"{provider} synthesis raised: {e}"}

    if error:
        return error
    return {"ok": True, "provider": provider, **result}


def speak(text, config=None, play=True, out_path=None):
    """Synthesizes `text` and, by default, plays it on this machine's
    default output device — the CLI path (`jarvis speak`). Pass play=False
    to just get the bytes back (that's all server.js's web path uses;
    web/server.js has no business touching the host's speaker for a
    request that came from someone's browser on another machine).

    out_path, if given, also writes the audio to disk (e.g. so the web
    endpoint doesn't have to hold the whole file in memory before
    streaming it) — this is independent of `play`.

    Returns synthesize()'s dict, plus "played": bool if play=True was
    requested (True/False depending on whether playback itself succeeded
    — synthesis can succeed while playback fails, e.g. no output device
    on a headless box)."""
    result = synthesize(text, config=config)
    if "error" in result:
        return result

    if out_path:
        try:
            from pathlib import Path
            Path(out_path).write_bytes(result["audio"])
            result["path"] = str(out_path)
        except OSError as e:
            result["write_error"] = f"couldn't write '{out_path}': {e}"

    if play:
        from . import audio_io
        play_result = audio_io.play(result["audio"], mime=result.get("mime"))
        result["played"] = "ok" in play_result
        if "error" in play_result:
            result["play_error"] = play_result["error"]

    return result