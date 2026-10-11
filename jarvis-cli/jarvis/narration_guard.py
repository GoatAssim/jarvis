"""Catch a local model describing a tool call instead of making it (A.1d).

On Ollama, small or heavily quantised models often answer a tool-worthy
request with a sentence of intent -- "I will now search for cli.py. Let's
proceed." -- and `finish_reason: stop` with no tool_calls at all. The adapter
used to return that sentence as the finished answer, so the user had to type
"yes go ahead, call the tool" by hand, round after round (ten times in the
logged run this came from).

This module is the detector and the nudge, and nothing else: pure functions,
no I/O, so the rule can be tested without a model. The retry itself lives in
ai_providers.call_ollama. It errs hard on the side of NOT firing -- a wrong
fire costs one extra round on a real answer; a miss is only the old behaviour.
"""

import re

# Rounds of the nudge allowed per ask. Two, because the logged run needed more
# than one reminder but a model that ignores two will ignore ten.
MAX_NUDGES = 2

# Longer than this is an answer, not an announcement.
MAX_ANNOUNCEMENT_CHARS = 400

NUDGE = (
    "You described what you are about to do instead of doing it. Call the tool "
    "now, using the tool-calling mechanism rather than prose. If you already "
    "have everything you need, answer the user in plain text instead."
)

# A statement of future intent: "I will now", "I'll", "Let me", "Let's",
# "I am going to", "I need to" -- followed, within the same breath, by an
# action verb a tool would perform. "Let me know" is the one common trap.
_INTENT = re.compile(
    r"\b(?:i\s*(?:will|'ll|shall|am\s+going\s+to|'m\s+going\s+to|need\s+to|should|can\s+now)"
    r"|let\s+me(?!\s+know)|let(?:'s|\s+us)|going\s+to|now\s+i)\b"
    r".{0,90}?"
    r"\b(?:search|look|use|call|run|read|check|open|find|inspect|fetch|list|execute|"
    r"try|query|scan|locate|retrieve|get|launch|start|invoke|examine|browse|analy[sz]e)\b",
    re.I | re.S,
)

# Signs the text is a finished reply rather than a plan: it reports a result,
# asks the user something, or hands the turn back.
_REPORTS_RESULT = re.compile(
    r"\b(?:i\s+(?:found|checked|searched|read|ran|opened|looked|couldn't|could\s+not|wasn't|was\s+not|"
    r"did\s+not|didn't)|here\s+(?:is|are)|the\s+result|it\s+(?:shows|contains|says)|"
    r"done|finished|completed|unfortunately)\b",
    re.I,
)


def looks_like_narrated_call(text, tool_names=()):
    """True when `text` reads as the model announcing a tool call it did not
    make. `tool_names` are the tools offered this round; naming one verbatim
    next to an intent phrase is the strongest signal, but the intent phrase
    alone is enough for a short text."""
    if not isinstance(text, str):
        return False
    body = text.strip()
    if not body or len(body) > MAX_ANNOUNCEMENT_CHARS:
        return False
    if body.endswith("?"):
        return False
    if _REPORTS_RESULT.search(body):
        return False
    if not _INTENT.search(body):
        return False
    lowered = body.lower()
    names = [n for n in (tool_names or ()) if isinstance(n, str) and n]
    if any(n.lower() in lowered for n in names):
        return True
    # No tool named: still an announcement if the last sentence is the
    # intent (a reply that merely MENTIONS a plan mid-way is left alone).
    sentences = [x for x in re.split(r"(?<=[.!])\s+", body) if x.strip()]
    if not sentences:
        return False
    # Two sentences or fewer is an announcement however it is split; a longer
    # text counts only when its LAST sentence is the intent.
    return len(sentences) <= 2 or bool(_INTENT.search(sentences[-1]))
