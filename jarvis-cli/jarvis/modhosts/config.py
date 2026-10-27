"""The CurseForge API key (``~/.jarvis/curseforge.json``) and nothing else.

CurseForge only answers a request that carries a personal API key, issued per developer at
console.curseforge.com and **non-transferable**: Jarvis never ships one and never shares one.
The owner pastes their own, either in Settings > Integrations > Minecraft mods or straight
into the file::

    {"api_key": "<your key>"}

The environment variable ``CURSEFORGE_API_KEY`` is the fallback (the file wins when both are
set). Without a key every CurseForge path answers with ``no_key_message()``; Modrinth and
OptiFine keep working.

Rules this file exists to keep (the plan's D5, and the P3 acceptance line "with a fake key no
result, error, log line or ``public_view()`` contains it"):

* ``get_key()`` is the ONLY way the key leaves this module, and it has four callers (the guard
  test, tests/test_minecraft_guards.py, lists them): ``modhosts/net.py`` (puts it in the
  ``x-api-key`` header, for ``api.curseforge.com`` alone, and scrubs it from every error),
  ``modhosts/cache.py`` (refuses to store anything that holds it), ``modhosts/sources.py``
  (only asks whether there is one) and ``modhosts/curseforge.py`` (asks whether there is one
  before any request, and scrubs it from messages). A new caller is a decision to make on
  purpose: add it to that list and say why in AGENTS.md.
* The key is never printed, logged, put in an error string, a result, a hub row or a prompt.
  Every message below is built without the value, including the "damaged key" one.
* ``public_view()`` is what everything else (doctor, tests, the Settings page through
  ``settings_admin``) may look at: it says "set" or "not set" and where from, never the key.

Paths are resolved on every call (tests move HOME, and a long-lived worker should not keep a
stale one). Nothing here raises, and nothing here touches the network.
"""

import json
import os
import re
from pathlib import Path

CONFIG_NAME = "curseforge.json"
ENV_NAME = "CURSEFORGE_API_KEY"
ENCODING = "utf-8"

# What a freshly created file holds, and what settings_admin treats as the built-in default.
DEFAULT_CONFIG = {"api_key": ""}

# The key travels in an HTTP header, so it must be plain printable ASCII with no space, quote
# or line break in it. The probe's first CurseForge run failed because the key had been read
# from a damaged environment variable and the vendor answered 403 "key missing or invalid";
# a key that cannot even be a header value is reported as DAMAGED (doctor says so) instead of
# being sent and rejected. Real keys are about 60 characters (they start with "$2a$10$").
MAX_KEY_LENGTH = 200
_KEY_OK_RE = re.compile(r"^[\x21-\x7e]{1,%d}$" % MAX_KEY_LENGTH)
_QUOTES = ("\"", "'")


def config_path():
    """~/.jarvis/curseforge.json (resolved now, so a moved HOME is honoured)."""
    return Path.home() / ".jarvis" / CONFIG_NAME


def clean(raw):
    """A pasted key as it should be stored: surrounding whitespace and one pair of matching
    quotes removed (a quote or a trailing newline is the usual paste accident). Not a string
    (or blank) gives ''. The result is NOT yet known to be valid: see ``looks_valid``."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:
        text = text[1:-1].strip()
    return text


def looks_valid(text):
    """True when `text` could be sent as a header value (printable ASCII, no spaces)."""
    return isinstance(text, str) and bool(_KEY_OK_RE.match(text))


def _read_file():
    """(value, state) where state is 'missing' | 'unreadable' | 'bad_shape' | 'ok'. `value`
    is the cleaned key text ('' when none). Never raises, never keeps the file's text."""
    path = config_path()
    try:
        text = path.read_text(encoding=ENCODING)
    except FileNotFoundError:
        return "", "missing"
    except (OSError, UnicodeDecodeError):
        return "", "unreadable"
    try:
        doc = json.loads(text)
    except ValueError:
        return "", "unreadable"
    if not isinstance(doc, dict):
        return "", "bad_shape"
    return clean(doc.get("api_key")), "ok"


def _read_env():
    return clean(os.environ.get(ENV_NAME, ""))


def _pick():
    """(key, source, problem). `key` is only ever a VALID key or None. `source` is 'file' or
    'env' (where a valid key came from) or None. `problem` is a short sentence about a key
    that was found but cannot be used (damaged), or ''."""
    problem = ""
    file_key, _state = _read_file()
    if file_key:
        if looks_valid(file_key):
            return file_key, "file", ""
        problem = "The key in %s has spaces or characters a key cannot contain." % CONFIG_NAME
    env_key = _read_env()
    if env_key:
        if looks_valid(env_key):
            return env_key, "env", problem
        problem = problem or "The %s variable has spaces or characters a key cannot contain." % ENV_NAME
    return None, None, problem


def get_key():
    """The key (file first, then the environment), or None when there is no usable one.
    The ONLY accessor; see the module docstring for who may call it."""
    try:
        return _pick()[0]
    except Exception:  # noqa: BLE001 -- a config problem must read as "no key", never crash a lookup
        return None


def key_problem():
    """One plain sentence when a key IS present but unusable (damaged), else ''. Built
    without the key's value."""
    try:
        key, _source, problem = _pick()
    except Exception:  # noqa: BLE001
        return ""
    return "" if key else problem


def public_view():
    """What may be shown or logged: whether a key is set, where it comes from, whether the
    file can be read. ``api_key`` is always blank (the same shape as the file, key removed)."""
    try:
        key, source, problem = _pick()
        _value, file_state = _read_file()
    except Exception:  # noqa: BLE001
        key, source, problem, file_state = None, None, "", "unreadable"
    return {
        "api_key": "",
        "api_key_set": key is not None,
        "source": source,
        "file": str(config_path()),
        "file_state": file_state,
        "problem": problem,
    }


def no_key_message():
    """The one sentence every CurseForge path answers with when there is no key."""
    return ("CurseForge needs your own personal API key (they are free, issued per developer at "
            "console.curseforge.com, and cannot be shared). Paste it in Settings > Integrations > "
            "Minecraft mods, or put it in ~/.jarvis/%s as {\"api_key\": \"...\"} (or set %s). "
            "Without one, Modrinth and OptiFine still work." % (CONFIG_NAME, ENV_NAME))
