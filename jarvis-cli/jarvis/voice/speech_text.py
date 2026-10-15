"""What `jarvis speak` / `jarvis listen` actually say out loud of a reply.

Fenced code read aloud is symbol-by-symbol noise ("backtick backtick backtick,
open brace, ..."). So each fenced block becomes ONE short spoken marker and the
prose around it is kept. This is the CLI half of I-B11: the web UI's Speak
button already did it in `web/public/rich-text.js` (`JarvisRich.speechText`),
and the two are meant to agree -- `tests/test_cli_speech_text.py` runs one
corpus through both (it needs `node`, and prints SKIP without it). If you change
the rule here, change it there, and the other way round.

Rules (identical to the JavaScript):
  * A fence is a line of up to 3 spaces then 3+ backticks or 3+ tildes. A
    backtick fence's info string may not contain a backtick ("```x``` like this"
    is inline code, not a fence). It closes on a line of the same character, at
    least as long, and nothing else; an unclosed fence runs to the end.
  * Each fenced block is replaced by SPOKEN_CODE_MARKER; two blocks with nothing
    between them give one marker; prose around them is kept, trimmed, and
    joined with a blank line.
  * Inline `code` is left alone: it is usually a name that reads fine.
  * Text with no fence in it comes back UNCHANGED (the very same string), and
    None / "" give "" -- so calling it twice is the same as calling it once,
    which is what lets the web path (the browser already stripped the text, then
    posts it to `jarvis speak`) go through here again harmlessly.

One deliberate difference: line endings. Text from a Windows file or a pasted
reply can be CRLF, and a "\\r" left on a fence line would stop it being
recognised as a fence (the JavaScript regex has the same blind spot, but a model
reply there never has one). Here CRLF / CR are read as LF for the purpose of
FINDING fences; a text with no fence is still returned as it came.

Pure: imports nothing but `re`, no I/O, never raises on any input.
"""
import re

SPOKEN_CODE_MARKER = "Code block omitted."

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
# Quick "is there any fence line at all" test; same shape as the JavaScript's.
_ANY_FENCE = re.compile(r"(^|\n) {0,3}(`{3,}|~{3,})")


def _normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_fences(text):
    """[(kind, text), ...] with kind "text" (prose lines joined by "\\n") or
    "code" (the lines between a fence's own lines). `text` must already use
    "\\n" line endings; speech_text() takes care of that."""
    out = []
    prose = []
    opened = None  # (fence character, fence length) while inside a block
    body = []

    def flush_prose():
        nonlocal prose
        if prose:
            out.append(("text", "\n".join(prose)))
        prose = []

    for line in text.split("\n"):
        if opened is None:
            m = _FENCE_OPEN.match(line)
            # A backtick fence's info string may not contain a backtick: that is
            # inline code, not a fence.
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                flush_prose()
                opened = (m.group(1)[0], len(m.group(1)))
                body = []
            else:
                prose.append(line)
        else:
            m = _FENCE_CLOSE.match(line)
            if m and m.group(1)[0] == opened[0] and len(m.group(1)) >= opened[1]:
                out.append(("code", "\n".join(body)))
                opened = None
                body = []
            else:
                body.append(line)
    if opened is not None:
        out.append(("code", "\n".join(body)))
    else:
        flush_prose()
    return out


def speech_text(text):
    """The text to hand to the speaker for `text`: fenced code replaced by
    SPOKEN_CODE_MARKER, the prose around it kept. See the module docstring."""
    raw = "" if text is None else str(text)
    norm = _normalize_newlines(raw)
    if not _ANY_FENCE.search(norm):
        return raw
    parts = []
    for kind, body in split_fences(norm):
        if kind == "code":
            if not parts or parts[-1] != SPOKEN_CODE_MARKER:
                parts.append(SPOKEN_CODE_MARKER)
        elif body.strip():
            parts.append(body.strip())
    return "\n\n".join(parts)
