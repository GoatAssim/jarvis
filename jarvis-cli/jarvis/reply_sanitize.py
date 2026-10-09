"""Guard against degenerate model replies (a wall of zero-width characters,
non-breaking spaces and ellipses).

Seen in the wild: gpt-oss-120b on Groq, right after a tool call succeeded,
produced a final reply that was ~380 characters of U+200B / U+00A0 / U+202F /
"…" / "**" noise with one real sentence at the very end. The text passed the
adapters' ``if not text`` check (it isn't empty) and was posted to the
channel verbatim, which looks like a hijacked bot.

Backward compatible by design: a normal reply is returned UNCHANGED. Only a
reply the detector flags is touched, and then either the complete sentences
inside it are kept, or — when there are none — "" is returned so the adapter's
existing "empty response content" path runs (kind=KIND_EMPTY, tool_history
preserved, so the next provider writes the closing reply without re-running
the tools that already succeeded).
"""

import re

# Invisible characters that carry no meaning in a chat reply. U+200C / U+200D
# are deliberately NOT here: they are load-bearing in emoji ZWJ sequences and
# in Persian/Indic scripts, so stripping them would corrupt legitimate text.
_INVISIBLE = "\u200b\u2060\ufeff"
_SPACEY = "\u00a0\u202f"          # NBSP / narrow NBSP — look like spaces, aren't
_NOISE = set(_INVISIBLE + _SPACEY + "…*")

# Thresholds. A real reply with markdown bold/ellipses is nowhere near these:
# the observed bad reply was ~70% noise, with 37 zero-width characters.
MIN_NOISE_CHARS = 12
MIN_NOISE_RATIO = 0.30
MIN_INVISIBLE_CHARS = 8

_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")
_INVISIBLE_RE = re.compile("[" + _INVISIBLE + "]")
_SPACEY_RE = re.compile("[" + _SPACEY + "]")


def is_degenerate(text):
    """True when ``text`` is dominated by invisible / ellipsis / asterisk noise."""
    if not text:
        return False
    invisible = sum(1 for ch in text if ch in _INVISIBLE)
    if invisible >= MIN_INVISIBLE_CHARS:
        return True
    noise = sum(1 for ch in text if ch in _NOISE)
    return noise >= MIN_NOISE_CHARS and noise / len(text) >= MIN_NOISE_RATIO


def _tidy(line):
    line = _INVISIBLE_RE.sub("", line)
    line = _SPACEY_RE.sub(" ", line)
    return re.sub(r"[ \t]+", " ", line).strip()


def sanitize_reply_text(text):
    """Return ``text`` unchanged unless it is degenerate; see module docstring.

    For a degenerate reply, keep only lines that read as a finished sentence
    (>= 3 words, ends in terminal punctuation) after the noise is removed.
    Fragments like "It appears ……" are residue of the glitch, not an answer.
    Returns "" when nothing survives.
    """
    if not is_degenerate(text):
        return text
    kept = []
    for raw in str(text).splitlines():
        line = _tidy(raw)
        if len(line.split()) >= 3 and _SENTENCE_END.search(line) and not set(line) <= _NOISE | set(". "):
            kept.append(line)
    return "\n".join(kept).strip()
