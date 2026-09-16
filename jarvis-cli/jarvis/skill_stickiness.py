"""Manual skill loading — separate from, and complementary to, the model's
own on-demand `load_skill` tool (see skills.py / skill_tools.py).

`load_skill` is the model deciding, mid-ask, that a skill matches the task
and pulling its body in for that one ask. This module is the human's
equivalent: force a skill's instructions into every ask for a conversation
(or globally, across every conversation) until explicitly unloaded — the
"/skillload <name>" chat command and `jarvis skillload` CLI command, for
when the user already knows exactly which skill this conversation needs and
doesn't want to depend on the model noticing on its own.

Storage: one JSON file, one entry per SCOPE. A scope is either a real
conversation id (loaded only for that chat) or the literal key "*" (loaded
for every ask regardless of conversation — the plain `jarvis skillload
<name>` CLI form with no conversation in play, e.g. a scripted one-shot
`jarvis ask "..."`). get_loaded() always unions the two, conversation-scoped
first, so a skill loaded globally still shows up inside a specific web chat
and vice versa.

Deliberately best-effort and TTL-less, unlike route_stickiness.py (which
this otherwise mirrors): a manually loaded skill is an explicit choice, so
it stays loaded until the user explicitly unloads it — there is no "a few
turns then decay" behavior here, because decaying something the user asked
for on purpose would be surprising, not helpful.
"""

import json
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
STICKY_FILE = JARVIS_DIR / "skill_stickiness.json"
ENCODING = "utf-8"
GLOBAL_SCOPE = "*"


def _scope_key(conv_id):
    key = (conv_id or "").strip()
    return key if key else GLOBAL_SCOPE


def _load():
    try:
        data = json.loads(STICKY_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        STICKY_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)
    except OSError:
        # Best-effort only, same as discovery_cache.py/route_stickiness.py —
        # a failed write just means the next ask() doesn't see this load,
        # same as if it had never been called.
        pass


def load(conv_id, name):
    """Force `name` into context for `conv_id` (or every conversation, if
    conv_id is falsy) until unload() is called. Idempotent."""
    name = (name or "").strip()
    if not name:
        return
    scope = _scope_key(conv_id)
    data = _load()
    names = data.get(scope) or []
    if name not in names:
        names.append(name)
    data[scope] = names
    _save(data)


def unload(conv_id, name):
    """Drop one skill from one scope. No-op if it wasn't loaded there."""
    name = (name or "").strip()
    if not name:
        return
    scope = _scope_key(conv_id)
    data = _load()
    names = [n for n in (data.get(scope) or []) if n != name]
    if names:
        data[scope] = names
    else:
        data.pop(scope, None)
    _save(data)


def unload_all(conv_id):
    """Clear every manually-loaded skill for one scope."""
    scope = _scope_key(conv_id)
    data = _load()
    if data.pop(scope, None) is not None:
        _save(data)


def get_loaded(conv_id):
    """Every skill name manually loaded for this conversation, plus every
    globally-loaded one — deduped, conversation-scoped entries first since
    they're the more specific, more recently deliberate choice."""
    data = _load()
    scope = _scope_key(conv_id)
    names, seen = [], set()
    for n in (data.get(scope) or []) + (data.get(GLOBAL_SCOPE) or []):
        if n not in seen:
            seen.add(n)
            names.append(n)
    return names


def clear_conv(conv_id):
    """Drop a conversation's manually-loaded skills outright — mirrors
    route_stickiness.clear_sticky(), called from the same place, so a fresh
    conversation never inherits an old one's force-loaded skill. Only ever
    touches the conversation-specific scope; a globally-loaded skill is a
    deliberate cross-conversation choice and survives this."""
    if not conv_id:
        return
    data = _load()
    if data.pop(str(conv_id), None) is not None:
        _save(data)


def loaded_context(conv_id):
    """The system-prompt text for every manually-loaded skill, or "".

    Self-healing: a skill deleted after being loaded is silently dropped
    from whichever scope(s) it was loaded in, rather than surfaced as an
    error on every subsequent ask — an ask() call, deep in building a
    prompt, is not the place to make the user deal with stale bookkeeping.
    """
    from . import skills

    names = get_loaded(conv_id)
    if not names:
        return ""
    blocks, stale = [], []
    for name in names:
        result = skills.load_skill(name)
        if result.get("error"):
            stale.append(name)
            continue
        blocks.append(f"[Manually loaded skill: {result['name']}]\n{result['instructions']}")
    for name in stale:
        # get_loaded() unions both scopes, so a caller here can't tell which
        # one a given stale name came from — clear it from both.
        unload(conv_id, name)
        unload(None, name)
    return "\n\n".join(blocks)
