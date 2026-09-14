"""Pluggable TTS/STT for Jarvis — jarvis-enhancement-plan.md §3.5.

Ported from Mark LIII's core/tts.py + core/stt.py idea (swappable local/
online speech backends), adapted to jarvis's REST request/response
architecture instead of a persistent Live session. See §2 of the plan for
why that distinction matters: Mark LIII's voice I/O is wired *into* one
open bidirectional audio stream, but jarvis calls a provider's
generateContent/chat-completion endpoint once per turn, so voice here is
necessarily a *pre/post* step around a normal ask() call, not part of the
request loop itself:

    record (audio_io.record) -> transcribe (stt.transcribe) ->
    ai_client.ask(text) -> speak (tts.speak)

Nothing in this package is a TOOL_SCHEMAS/TOOLS entry and nothing here
should ever be imported from actions/ or tools.py — nothing in jarvis's
tool-calling loop should be able to make the model trigger a recording or
a playback on its own. The only callers are cli.py (terminal mic/speaker)
and, indirectly, web/server.js (spawns `jarvis speak`/`jarvis transcribe`
as subprocesses so it can hand raw bytes back to the browser's own
mic/speaker instead of the host machine's — see voice/tts.py and
voice/stt.py's module docstrings for why CLI and web use the same
synthesize()/transcribe() functions but different playback paths).

Every submodule here does lazy, try/except imports of its optional
dependencies (numpy/soundfile/sounddevice, kokoro, edge-tts, faster-
whisper, vosk, ...) and returns a plain {"error": "..."} dict naming the
exact `pip install` extra to run when one is missing, the same convention
ocr_tools.py already uses for pytesseract/Pillow. None of these packages
are in jarvis-cli's base `dependencies` in pyproject.toml — voice is 100%
opt-in.
"""