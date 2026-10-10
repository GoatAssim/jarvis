"""One tiny, history-free model call, for the places that need a yes/no or a
small JSON verdict about a chat message.

WHO USES IT
-----------
  * channels/sender_gate.py   -- D-I10: "is this detail worth remembering?"
  * channels/person_memory.py -- L.22: an instruction's topic matched a message
                                 only weakly; is it really about that topic?
  * channels/awaiting.py      -- L.23: is this reply to something the owner is
                                 waiting on important enough to interrupt them?

WHY ONE MODULE
--------------
All three need the same thing -- a few dozen tokens in, a few out, never on
the path of the person's own answer, and never allowed to break anything --
and `ai_client._quick_title_completion` already shows how to do that without a
second provider-selection path. This reuses its building blocks (eligible
providers, per-key failover, the adapter table) with a different prompt.

`_complete` is the one function that touches a provider. Tests replace it, so
no test needs a key or a network.

FAILS TOWARD "NO ANSWER"
------------------------
Every public function returns None when nothing usable came back. What None
MEANS is each caller's decision (the memory gate says "do not remember", the
reply classifier says "digest quietly", an instruction match says "apply it"),
and each documents it. Nothing here raises.
"""

import json
import re

# Short, because the question is short. A slow provider must not stall a
# gateway thread for the 60+ seconds a full answer is allowed.
TIMEOUT_SECONDS = 15
MAX_PROMPT_CHARS = 1800


def _complete(prompt):
    """The text of one cheap completion, or None. Replaced in tests."""
    try:
        from .. import ai_client, ai_config, ai_providers
        cfg = ai_config.load_ai_config()
        providers = ai_client._eligible_providers(cfg["providers"], cfg["defaults"])
    except Exception:  # noqa: BLE001 -- no config / no provider: no answer
        return None
    messages = [{"role": "user", "content": prompt[:MAX_PROMPT_CHARS]}]
    for provider in providers[:2]:
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            continue
        for key in ai_config.provider_keys(provider) or [None]:
            try:
                resolved = ai_client._resolve(provider, cfg["defaults"])
                if key is not None:
                    resolved["api_key"] = key
                result = adapter(resolved, messages,
                                 min(resolved["timeout"], TIMEOUT_SECONDS),
                                 tools=None, tool_executor=None)
            except Exception:  # noqa: BLE001
                continue
            if result.ok and result.text:
                return result.text
    return None


_OBJECT = re.compile(r"\{.*\}", re.S)


def ask_json(prompt):
    """The JSON object the model answered with, or None. Tolerates a code
    fence or a sentence around the object; a non-object answer is None."""
    try:
        raw = _complete(prompt)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    match = _OBJECT.search(str(raw))
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def ask_yes_no(prompt, field="answer"):
    """True / False from {"<field>": true|false}, or None when unclear."""
    obj = ask_json(prompt)
    if obj is None:
        return None
    value = obj.get(field)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no"):
        return False
    return None


def quote(text, limit=300):
    """Message text made safe to place inside a prompt: one line, clipped,
    with the characters that could close our own quotes removed. The model is
    told the text is DATA; this keeps it from also being formatting."""
    one = " ".join(str(text or "").split())[:limit]
    return one.replace('"', "'")
