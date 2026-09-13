"""Derives the CLI's own command/exe name from persona.assistant_name.

The config directory stays ~/.jarvis no matter what (see ai_config.py's
JARVIS_DIR) — that's a fixed, backwards-compatible storage path, totally
separate from what you type on the command line to invoke the tool. This
module is only about the latter: turning whatever name a persona is
equipped with into something Windows (and setuptools' console_scripts
generator) will accept as a command name, e.g.:

    "J.A.R.V.I.S"  -> "jarvis"
    "Friday"       -> "friday"
    "L.O.L"        -> "lol"
    "F.R.I.D.A.Y!" -> "friday"
    ""             -> "jarvis"   (falls back to the default)

Kept dependency-free (stdlib only, and only imports ai_config — itself
stdlib-only) so it's safe to import/run standalone from a fresh checkout
*before* `pip install .` has ever run, which is exactly when the build
script needs it: to decide what name to bake into pyproject.toml's
[project.scripts] entry before setuptools generates the actual exe.

The exact same sanitize_cli_name() logic is mirrored in web/public/app.js
(see sanitizeCliName there) so the "Skin" settings modal can show an
accurate live preview of the resulting command name without a round trip
to the backend. If you change the rules here, change them there too.
"""

import re
import sys

from .ai_config import load_ai_config

DEFAULT_CLI_NAME = "jarvis"

# Command names that would be actively confusing or dangerous to shadow.
# Sanitizing to one of these falls back to the default instead.
_RESERVED = {
    "cli", "cmd", "com", "con", "exe", "nul", "prn", "aux",
    "python", "python3", "py", "pip", "git", "npm", "node",
}

# Anything that isn't a plain ASCII letter/digit is stripped outright —
# this is what turns "J.A.R.V.I.S" into "jarvis" and "L.O.L" into "lol":
# dots, spaces, hyphens, emoji, punctuation, all of it just disappears
# rather than being converted to underscores/hyphens, so the result reads
# as one clean word instead of "j-a-r-v-i-s" or "l_o_l".
_ILLEGAL_CHARS_RE = re.compile(r"[^A-Za-z0-9]+")


def sanitize_cli_name(raw):
    """Turn an arbitrary persona display name into a safe, lowercase,
    alnum-only command name. Always returns a non-empty, usable name —
    falls back to DEFAULT_CLI_NAME for empty/all-illegal/reserved input."""
    cleaned = _ILLEGAL_CHARS_RE.sub("", str(raw or "")).lower()
    if not cleaned:
        return DEFAULT_CLI_NAME
    # A command starting with a digit is legal on Windows but awkward/easy
    # to mistype as a number in scripts, so nudge it into letter-led form.
    if cleaned[0].isdigit():
        cleaned = "cli" + cleaned
    if cleaned in _RESERVED:
        return DEFAULT_CLI_NAME
    return cleaned


def current_cli_name():
    """Reads ~/.jarvis/ai_config.json and returns the sanitized CLI name
    for whatever persona is currently equipped. Never raises — any read/
    parse failure (missing file, bad JSON, etc.) just yields the default,
    same as every other ai_config.py consumer."""
    try:
        cfg = load_ai_config()
    except Exception:
        return DEFAULT_CLI_NAME
    persona = cfg.get("persona") or {}
    return sanitize_cli_name(persona.get("assistant_name"))


DEFAULT_DISPLAY_NAME = "J.A.R.V.I.S"


def current_display_name():
    """The persona's actual display name (dots, spacing, and all — e.g.
    "F.R.I.D.A.Y.") straight out of ai_config.json, NOT the sanitized
    command name. Used for the CLI's own in-character chrome text (the
    confirm-before-running prompt, "ask X something" hints, etc.) so the
    terminal talks about "Friday" doing something rather than the fixed
    word "Jarvis" or the lowercase command "friday" — same source of truth
    ai_client.py already uses for the AI persona's system prompt."""
    try:
        cfg = load_ai_config()
    except Exception:
        return DEFAULT_DISPLAY_NAME
    persona = cfg.get("persona") or {}
    name = persona.get("assistant_name")
    return name if isinstance(name, str) and name.strip() else DEFAULT_DISPLAY_NAME


def banner_letters():
    """Letter-spaced, all-caps rendering of the sanitized CLI name for the
    startup banner — e.g. "friday" -> "F R I D A Y", mirroring the
    original hardcoded "J A R V I S" art but generated from whatever
    persona is actually equipped instead of frozen as one name forever."""
    return " ".join(current_cli_name().upper())


def main():
    # Deliberately prints ONLY the name, nothing else, so build scripts can
    # capture stdout directly (see script.bat's `for /f` around this call).
    sys.stdout.write(current_cli_name())


if __name__ == "__main__":
    main()
