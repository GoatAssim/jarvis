"""L.16 item 2: capability preflight for an unattended ask.

WHY THIS EXISTS
---------------
The 1a99e1e3f0d3af0e incident ("check the screen; if usage is exhausted shut
the PC down, otherwise type continue") spent 12 model calls and ~56K tokens on
a PC that could not read a screen at all. The two screen tools were taught to
fail once, early (L.16 item 1) -- but only AFTER a model round had been paid for
and a tool called. A job that needs a capability the PC does not have should
not start: nobody is watching, so the only useful outcomes are "done" and "a
one-line reason, before anything was spent".

WHAT IT CHECKS (and what it deliberately does not)
--------------------------------------------------
Two needs, both only for an UNATTENDED ask, both derived the way job_risk
derives what a job can do -- from the tools the router could offer -- plus one
wording cue where the tool list cannot tell the difference:

  power       the router could offer `power_action` (the prompt is about
              shutting down / restarting / sleeping / locking). Blocked when
              that tool is missing or switched off in the Tool Manager.
  see_screen  the router could offer a screen reader (read_screen /
              take_screenshot) AND the prompt asks to LOOK at the screen
              ("check the screen", "read what the window says"). Blocked when
              neither OCR (pytesseract + Pillow + the Tesseract binary) nor a
              vision-capable provider exists. The cue matters: "open Notepad
              and type hello" reaches the same desktop tools and needs no eyes.

It never blocks on a guess about WHAT a model will do. It blocks only when a
need is stated AND the capability is certainly absent. Everything else passes
and the existing guards (the screen tools' own one-shot failure, the circuit
breaker, the token budget) still apply.

FAILS OPEN. Unlike job_risk (which fails closed because a silent "nothing
risky here" is dangerous) this is a cost optimisation, not a safety control: an
exception, an unreadable config or an import error returns ok=True and the run
proceeds exactly as it did before this module existed.

Pure functions, injectable probes (ocr_ready / vision_ready / power_tool_ready)
so tests need no Tesseract, no keys and no desktop.
"""
import re

NEED_SEE = "see_screen"
NEED_POWER = "power"

_SCREEN_READERS = ("read_screen", "take_screenshot", "look_at_screen")

# A verb of looking within a short distance of a screen-ish noun, or the
# unambiguous idioms. Anchored on both so "type the screen name" does not match.
_SEE_CUE = re.compile(
    r"\b(?:check|read|look(?:\s+at)?|see|watch|inspect|verify|find\s+out|tell\s+me|whether|if)\b"
    r"[^.?!\n]{0,60}?\b(?:screen|window|display|monitor)\b"
    r"|\bon[\s-]?screen\b|\bscreen\s+(?:says|shows|reads|text)\b",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Probes -- each returns True/False and never raises
# ---------------------------------------------------------------------------
def ocr_ready():
    """pytesseract + Pillow importable AND the Tesseract binary runs."""
    try:
        from . import ocr_tools
        if ocr_tools.pytesseract is None or ocr_tools.Image is None:
            return False
        ocr_tools.pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 -- includes TesseractNotFoundError
        return False


def vision_ready():
    """At least one configured, enabled provider that can take an image."""
    try:
        from . import vision_tools
        return bool(vision_tools.vision_available())
    except Exception:  # noqa: BLE001
        return False


def power_tool_ready():
    """`power_action` exists on this build and the owner has not switched it off."""
    try:
        from . import tools, tool_disable
        return "power_action" in tools.TOOLS and not tool_disable.is_tool_disabled("power_action")
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def needs_of(prompt):
    """(needs, tools): which capabilities the prompt states it needs, and the
    router's (untrimmed) tool list they were derived from. Raises on a router
    error -- check() turns that into fail-open."""
    from . import job_risk
    tools = job_risk._ask_tools(str(prompt or ""))
    needs = []
    if "power_action" in tools:
        needs.append(NEED_POWER)
    if any(t in tools for t in _SCREEN_READERS) and _SEE_CUE.search(str(prompt or "")):
        needs.append(NEED_SEE)
    return needs, tools


def check(prompt):
    """{"ok": bool, "needs": [...], "blockers": [{"need", "why"}]}. Never raises."""
    try:
        needs, _tools = needs_of(prompt)
        blockers = []
        if NEED_SEE in needs and not ocr_ready() and not vision_ready():
            blockers.append({
                "need": NEED_SEE,
                "why": ("it has to read the screen, but the OCR engine (Tesseract) isn't "
                        "installed and no configured provider can look at an image")})
        if NEED_POWER in needs and not power_tool_ready():
            blockers.append({
                "need": NEED_POWER,
                "why": ("it has to shut down, restart, sleep or lock this PC, but the "
                        "power_action tool is missing or switched off in the Tool Manager")})
        return {"ok": not blockers, "needs": needs, "blockers": blockers}
    except Exception as exc:  # noqa: BLE001 -- fail OPEN, see the module docstring
        return {"ok": True, "needs": [], "blockers": [], "error": str(exc)}


def reply(result):
    """The one line an unattended run reports instead of starting."""
    parts = [b["why"] for b in (result or {}).get("blockers") or []]
    if not parts:
        return ""
    return ("Not started: this job can't run on this PC right now -- %s. "
            "Nothing was run and no model was called. Fix that and it will run next time."
            % "; and ".join(parts))
