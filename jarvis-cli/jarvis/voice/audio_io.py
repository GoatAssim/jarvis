"""Microphone recording and local playback for Jarvis's voice module.

This is the one piece of voice/ that's genuinely CLI-only — a web browser
records through its own mic via getUserMedia and uploads the resulting
blob (see web/server.js's /api/voice/transcribe), it never calls anything
in this file. cli.py's `jarvis listen` is the only caller of record() /
play(), which is exactly the "terminal audio device vs. browser mic/
speaker" split jarvis-enhancement-plan.md §3a calls out for §3.5.

Backend: sounddevice + numpy for capture, soundfile for WAV encode/decode.
All three are optional (pip install "jarvis-cli[voice-audio]") — everything
in here degrades to a clear {"error": "..."} dict, same convention as
tts.py/stt.py, rather than an ImportError with no guidance.

Recording uses simple RMS-based silence detection rather than pulling in a
dedicated VAD library (webrtcvad, silero-vad, ...): good enough to auto-
stop a spoken question without an extra heavyweight dependency, tunable
via ~/.jarvis/voice_config.json's "audio.silence_rms_threshold" if a
particular mic/room needs it.
"""

import sys
import tempfile
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import soundfile as sf
except ImportError:
    sf = None

_AUDIO_INSTALL_NOTE = (
    'Recording/playback needs numpy, sounddevice, and soundfile: '
    'pip install "jarvis-cli[voice-audio]" (on Linux you may also need the '
    "system package 'libportaudio2')."
)


def _missing_audio_deps():
    if np is None or sd is None or sf is None:
        return {"error": _AUDIO_INSTALL_NOTE}
    return None


def record(config=None, max_seconds=None, on_status=None):
    """Records from the default input device until either a run of near-
    silence is detected (person stopped talking) or max_record_seconds
    elapses, whichever comes first. Returns {"ok": True, "path": "<temp
    wav path>", "seconds": <float>} on success, or {"error": "..."}.

    `on_status(msg)` is an optional callback for a "listening..."/"heard
    silence, stopping" style status line — cli.py's `jarvis listen` uses
    it the same way ai_client.ask()'s on_attempt/on_route callbacks work,
    so the caller controls whether/how that gets printed rather than this
    module printing directly.

    The caller owns the returned temp file and should delete it once
    done (stt.transcribe() does not delete its input).
    """
    missing = _missing_audio_deps()
    if missing:
        return missing

    from . import config as voice_config
    cfg = (config or voice_config.load_voice_config())["audio"]
    sample_rate = int(cfg.get("sample_rate", 16000))
    channels = int(cfg.get("channels", 1))
    silence_seconds = float(cfg.get("silence_seconds", 1.2))
    threshold = float(cfg.get("silence_rms_threshold", 500))
    ceiling = float(max_seconds if max_seconds is not None else cfg.get("max_record_seconds", 30))

    status = on_status or (lambda msg: None)
    block_seconds = 0.2
    block_frames = max(1, int(sample_rate * block_seconds))

    chunks = []
    silence_run = 0.0
    heard_any_voice = False
    elapsed = 0.0

    status("listening… (speak now)")
    try:
        with sd.InputStream(
            samplerate=sample_rate, channels=channels, dtype="int16"
        ) as stream:
            while elapsed < ceiling:
                block, _overflow = stream.read(block_frames)
                chunks.append(block.copy())
                elapsed += block_seconds

                rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))
                if rms >= threshold:
                    heard_any_voice = True
                    silence_run = 0.0
                elif heard_any_voice:
                    silence_run += block_seconds
                    if silence_run >= silence_seconds:
                        status("heard silence — stopping")
                        break
    except Exception as e:
        return {"error": f"recording failed: {e}"}

    if not chunks:
        return {"error": "no audio captured"}

    audio = np.concatenate(chunks, axis=0)
    if not heard_any_voice:
        status("no speech detected")
        return {"error": "no speech detected (mic may be muted or silent)"}

    fd_path = Path(tempfile.gettempdir()) / f"jarvis_rec_{int(time.time() * 1000)}.wav"
    try:
        sf.write(str(fd_path), audio, sample_rate, subtype="PCM_16")
    except Exception as e:
        return {"error": f"couldn't write recording: {e}"}

    return {"ok": True, "path": str(fd_path), "seconds": round(elapsed, 2)}


def play(path_or_bytes, mime=None):
    """Plays a WAV/MP3 file (path or raw bytes) through the default output
    device. Returns {"ok": True} or {"error": "..."}. Used only by cli.py
    (`jarvis speak`) — server.js's web path hands the synthesized bytes
    straight to the browser instead of calling this."""
    missing = _missing_audio_deps()
    if missing:
        return missing

    tmp_path = None
    try:
        if isinstance(path_or_bytes, (bytes, bytearray)):
            suffix = ".mp3" if (mime and "mpeg" in mime) else ".wav"
            tmp_path = Path(tempfile.gettempdir()) / f"jarvis_play_{int(time.time() * 1000)}{suffix}"
            tmp_path.write_bytes(path_or_bytes)
            target = tmp_path
        else:
            target = Path(path_or_bytes)

        data, samplerate = sf.read(str(target), dtype="float32")
        sd.play(data, samplerate)
        sd.wait()
        return {"ok": True}
    except Exception as e:
        return {"error": f"playback failed: {e}"}
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)