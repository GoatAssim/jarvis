"""Loading and defaults for ~/.jarvis/voice_config.json.

Same philosophy as ai_config.py (see that module's docstring for the full
rationale): a plain, hand-editable JSON file, created with a sensible
starter template on first use, re-read fresh on every invocation — no
"apply"/reload step, no code change needed to switch providers or tweak a
voice/model/rate.

Two independent provider choices, matching jarvis-enhancement-plan.md's
§3.5 table:

    "tts": {"provider": "edge", ...}          # kokoro | edge | elevenlabs | xtts
    "stt": {"provider": "faster_whisper", ...} # faster_whisper | vosk

"edge" (Edge TTS) is the default TTS provider because it needs no API key
and no local model download — the file works out of the box the same way
ai_config.json's Ollama entry needs no key. Swap "provider" to "kokoro" for
fully offline TTS (first call downloads the ~330MB model, then no network
needed), "elevenlabs" for paid cloud quality (needs api_key + voice_id
filled in under "elevenlabs"), or "xtts" to clone a specific voice from a
short reference clip you have the rights to (first call downloads the
~2GB XTTS-v2 checkpoint, then runs fully offline — needs
speaker_wav_path filled in under "xtts"; see `jarvis voice-config`). Same
idea for STT: "faster_whisper" is the
default (offline after its one-time, much smaller model download); "vosk"
is a lighter offline alternative for constrained machines, but needs
"model_path" pointed at a downloaded Vosk model directory since Vosk (unlike
faster-whisper) doesn't fetch one for you.

Each per-provider block is complete on its own so switching "provider" is
the only edit needed for a normal switch — the other blocks' settings are
simply ignored while inactive, not deleted, so flipping back and forth
during evaluation doesn't lose any tuning.
"""

import json
import sys
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
VOICE_CONFIG_FILE = JARVIS_DIR / "voice_config.json"
ENCODING = "utf-8"

KNOWN_TTS_PROVIDERS = {"kokoro", "edge", "elevenlabs", "xtts"}
KNOWN_STT_PROVIDERS = {"faster_whisper", "vosk"}

DEFAULT_VOICE_CONFIG = {
    # Master switch for the whole voice feature (TTS + STT), independent of
    # which providers are picked below. Flip to false to turn voice off
    # everywhere at once — the CLI's `speak`/`listen`/`transcribe`
    # subcommands and synthesize()/transcribe() both refuse to run (see
    # voice_enabled() below and the checks at the top of tts.synthesize()/
    # stt.transcribe()), and web/server.js's /api/status reports it via
    # "voiceEnabled" so public/app.js can hide the mic button and every
    # "Speak" button on the reply bubbles. No provider/model settings are
    # touched or lost — this only gates whether they're used.
    "enabled": True,
    "tts": {
        "provider": "edge",
        "kokoro": {
            # Kokoro voice names are short codes, not free text — see
            # https://github.com/hexgrad/kokoro for the current list.
            "voice": "af_heart",
            "speed": 1.0,
        },
        "edge": {
            # Any voice from `edge-tts --list-voices`.
            "voice": "en-US-GuyNeural",
            "rate": "+0%",
            "volume": "+0%",
        },
        "elevenlabs": {
            "api_key": "",
            "voice_id": "",
            "model": "eleven_turbo_v2_5",
        },
        "xtts": {
            # Path to a short (roughly 6-30s), clean, single-speaker
            # reference recording of the voice to clone. You need the
            # rights to this recording (your own voice, a hired voice
            # actor, a public-domain source, etc.) — this is a general-
            # purpose voice cloner, not a licensed source of any specific
            # copyrighted character's voice. Left blank on purpose: xtts
            # refuses to run without one rather than silently falling
            # back to a stock voice.
            "speaker_wav_path": "",
            "language": "en",
            "model_name": "tts_models/multilingual/multi-dataset/xtts_v2",
            "device": "cpu",  # "cuda" if you have a supported GPU — much faster
        },
    },
    "stt": {
        "provider": "faster_whisper",
        "faster_whisper": {
            # tiny|base|small|medium|large-v3 (or a distil-* variant) — see
            # https://github.com/SYSTRAN/faster-whisper. "small" is a
            # reasonable default: noticeably more accurate than "base",
            # still comfortable on CPU.
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "language": "",  # "" = auto-detect
        },
        "vosk": {
            # No default — Vosk needs a model downloaded and pointed at
            # explicitly (https://alphacephei.com/vosk/models); unlike
            # faster-whisper it won't fetch one on first use.
            "model_path": "",
        },
    },
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        # Auto-stop-recording tuning for voice.audio_io.record(): how long
        # a run of near-silence has to last before we consider the person
        # done talking, and the hard ceiling regardless of silence, so a
        # noisy room can't pin the mic open forever.
        "silence_seconds": 1.2,
        "silence_rms_threshold": 500,
        "max_record_seconds": 30,
    },
}


def ensure_voice_config():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    if not VOICE_CONFIG_FILE.exists():
        VOICE_CONFIG_FILE.write_text(
            json.dumps(DEFAULT_VOICE_CONFIG, indent=2) + "\n", encoding=ENCODING
        )


def _merge_defaults(section_name, loaded, defaults):
    """Shallow-merge one top-level section (tts/stt/audio) so a config file
    written before a new sub-key was added still picks up that sub-key's
    default instead of a KeyError deep in tts.py/stt.py/audio_io.py."""
    section = loaded.get(section_name)
    if not isinstance(section, dict):
        return dict(defaults[section_name])
    merged = dict(defaults[section_name])
    for key, value in section.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            sub = dict(merged[key])
            sub.update(value)
            merged[key] = sub
        else:
            merged[key] = value
    return merged


def load_voice_config():
    """Always returns a dict with 'enabled', 'tts', 'stt', and 'audio'
    keys, each fully populated (missing sub-keys filled from
    DEFAULT_VOICE_CONFIG) — callers never need to guard against a
    half-shaped or stale config."""
    ensure_voice_config()
    try:
        data = json.loads(VOICE_CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(
            f"Warning: couldn't read {VOICE_CONFIG_FILE} ({e}) — using built-in voice "
            f"defaults for this run. Fix the JSON, or delete the file to get a fresh "
            f"starter template.",
            file=sys.stderr,
        )
        data = {}

    if not isinstance(data, dict):
        data = {}

    enabled = data.get("enabled", DEFAULT_VOICE_CONFIG["enabled"])
    if not isinstance(enabled, bool):
        enabled = DEFAULT_VOICE_CONFIG["enabled"]

    return {
        "enabled": enabled,
        "tts": _merge_defaults("tts", data, DEFAULT_VOICE_CONFIG),
        "stt": _merge_defaults("stt", data, DEFAULT_VOICE_CONFIG),
        "audio": _merge_defaults("audio", data, DEFAULT_VOICE_CONFIG),
    }


def voice_enabled(cfg=None):
    """True unless voice_config.json has \"enabled\": false at the top
    level — the one switch that turns TTS + STT off everywhere (CLI and
    web) without touching any provider settings."""
    cfg = cfg or load_voice_config()
    return bool(cfg.get("enabled", True))


def tts_provider_and_settings(cfg=None):
    """(provider_name, that provider's settings dict), falling back to
    'edge' if the configured provider name is unrecognized (e.g. a typo
    hand-edited into the file) rather than raising deep inside tts.py."""
    cfg = cfg or load_voice_config()
    provider = cfg["tts"].get("provider", "edge")
    if provider not in KNOWN_TTS_PROVIDERS:
        provider = "edge"
    return provider, cfg["tts"].get(provider, {})


def stt_provider_and_settings(cfg=None):
    """Same shape as tts_provider_and_settings(), falling back to
    'faster_whisper'."""
    cfg = cfg or load_voice_config()
    provider = cfg["stt"].get("provider", "faster_whisper")
    if provider not in KNOWN_STT_PROVIDERS:
        provider = "faster_whisper"
    return provider, cfg["stt"].get(provider, {})