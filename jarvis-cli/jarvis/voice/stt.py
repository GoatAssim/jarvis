"""Pluggable speech-to-text backends for Jarvis — jarvis-enhancement-plan.md
§3.5. Mirrors Mark LIII's core/stt.py swappable-provider shape (both its
options are local/offline, matching the STT half of that plan's table):

    faster_whisper  — VAD-buffered, downloads its model once (~75-290MB
                       depending on size) then runs fully offline. Default.
    vosk             — lighter, smaller footprint, needs a model manually
                       downloaded and pointed at via voice_config.json.

Both backends take a WAV file path and return plain text — no streaming,
no partial-result callbacks, because jarvis's REST request/response loop
has nowhere to put a partial transcript anyway (see this package's
__init__.py docstring): transcribe() runs to completion, then its result
becomes the literal `text` argument to ai_client.ask(), same as if the
person had typed it.

Used identically by cli.py (`jarvis listen`, on a mic recording from
audio_io.record()) and by web/server.js's /api/voice/transcribe endpoint
(on a browser-recorded upload saved to a temp file) — one backend, two
callers, per §3a.
"""

import sys

_WHISPER_INSTALL_NOTE = (
    'faster-whisper not installed. pip install "jarvis-cli[voice-stt-whisper]" '
    "(first transcription downloads the configured model size once, then "
    "runs fully offline)."
)
_VOSK_INSTALL_NOTE = (
    'vosk not installed. pip install "jarvis-cli[voice-stt-vosk]", then '
    "download a model from https://alphacephei.com/vosk/models and set "
    "stt.vosk.model_path in `jarvis voice-config` to its extracted folder."
)

_whisper_model_cache = {}  # (model_size, device, compute_type) -> model
_vosk_model_cache = {}     # model_path -> vosk.Model


def _transcribe_faster_whisper(path, settings):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None, {"error": _WHISPER_INSTALL_NOTE}

    model_size = settings.get("model", "small")
    device = settings.get("device", "cpu")
    compute_type = settings.get("compute_type", "int8")
    language = (settings.get("language") or "").strip() or None

    cache_key = (model_size, device, compute_type)
    model = _whisper_model_cache.get(cache_key)
    if model is None:
        try:
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as e:
            return None, {"error": f"couldn't load faster-whisper model '{model_size}': {e}"}
        _whisper_model_cache[cache_key] = model

    try:
        segments, info = model.transcribe(path, language=language, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:
        return None, {"error": f"faster-whisper transcription failed: {e}"}

    if not text:
        return None, {"error": "no speech recognized"}
    return {"text": text, "language": getattr(info, "language", language)}, None


def _transcribe_vosk(path, settings):
    try:
        import vosk
    except ImportError:
        return None, {"error": _VOSK_INSTALL_NOTE}
    try:
        import wave
    except ImportError:
        return None, {"error": "vosk backend needs the stdlib 'wave' module (unexpected)."}
    import json as _json

    model_path = (settings.get("model_path") or "").strip()
    if not model_path:
        return None, {
            "error": "stt.vosk.model_path is empty — set it via `jarvis voice-config` to "
                     "a downloaded Vosk model folder (https://alphacephei.com/vosk/models)."
        }

    model = _vosk_model_cache.get(model_path)
    if model is None:
        try:
            model = vosk.Model(model_path)
        except Exception as e:
            return None, {"error": f"couldn't load vosk model at '{model_path}': {e}"}
        _vosk_model_cache[model_path] = model

    try:
        wf = wave.open(path, "rb")
    except Exception as e:
        return None, {"error": f"couldn't open '{path}' as WAV: {e}"}

    try:
        rec = vosk.KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(False)
        pieces = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                pieces.append(_json.loads(rec.Result()).get("text", ""))
        pieces.append(_json.loads(rec.FinalResult()).get("text", ""))
    except Exception as e:
        return None, {"error": f"vosk transcription failed: {e}"}
    finally:
        wf.close()

    text = " ".join(p for p in pieces if p).strip()
    if not text:
        return None, {"error": "no speech recognized"}
    return {"text": text, "language": None}, None


_BACKENDS = {
    "faster_whisper": _transcribe_faster_whisper,
    "vosk": _transcribe_vosk,
}


def transcribe(path, config=None):
    """Transcribes a WAV file at `path`. Returns {"ok": True, "text":
    "...", "provider": "...", "language": "..."} on success, or
    {"error": "..."} — never raises."""
    from . import config as voice_config

    cfg = config or voice_config.load_voice_config()
    if not voice_config.voice_enabled(cfg):
        return {"error": "Voice is disabled (voice_config.json: \"enabled\": false).", "disabled": True}

    provider, settings = voice_config.stt_provider_and_settings(cfg)

    backend = _BACKENDS.get(provider)
    if backend is None:
        return {"error": f"unknown STT provider '{provider}'"}

    try:
        result, error = backend(path, settings)
    except Exception as e:
        return {"error": f"{provider} transcription raised: {e}"}

    if error:
        return error
    return {"ok": True, "provider": provider, **result}