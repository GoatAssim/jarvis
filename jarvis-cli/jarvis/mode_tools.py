"""Lets Jarvis read and change its own prompt "capacity" mode (see
ai_client.PROMPT_MODE_DEFS) from inside an ask, the same way a person can
via the web UI's capacity switch or `jarvis mode-set`.

Imports ai_client lazily (inside the functions, not at module load) since
ai_client itself imports tools.py (which imports this module) at its own
top \u2014 a top-level `from . import ai_client` here would be a circular
import at the exact moment ai_client is still mid-initialization.
"""


def _mode_cost_pct(mode):
    """Relative token cost of a mode, read from its "N% Capacity" label
    (the registry has no numeric cost field, and its ORDER is not cost order:
    full comes first). None for a label with no percentage -- a custom mode
    that doesn't follow the convention is never treated as "more expensive"."""
    import re
    from . import ai_client

    m = re.match(r"\s*(\d+)\s*%", ai_client.MODE_LABELS.get(mode, "") or "")
    return int(m.group(1)) if m else None


def _active_provider_is_local(cfg):
    """True when the provider/key attempt driving THIS tool call is a local
    model: type "ollama", or any provider pointed at this machine (covers a
    second Ollama entry reached through its OpenAI-compatible /v1 endpoint).
    False outside an ask() (a direct `jarvis tool ...` run, a test), so a
    person driving this by hand is never restricted."""
    from urllib.parse import urlparse

    from . import ai_client, ai_providers

    label = ai_providers.active_provider_label()
    if not label:
        return False
    name = label.split(" (key ", 1)[0]  # "gemini (key 2/10)" -> "gemini"
    for provider in cfg.get("providers") or []:
        if ai_client._provider_label(provider) != name:
            continue
        if provider.get("type") == "ollama":
            return True
        host = (urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    return False


def tool_get_capacity_mode(args=None):
    from . import ai_client, ai_config

    cfg = ai_config.load_ai_config()
    mode = ai_client.current_mode(cfg)
    return {
        "mode": mode,
        "label": ai_client.MODE_LABELS.get(mode, mode),
        "options": ai_client.mode_options(),
    }


def tool_set_capacity_mode(args):
    from . import ai_client, ai_config

    args = args or {}
    cfg = ai_config.load_ai_config()
    current = ai_client.current_mode(cfg)
    options = ai_client.mode_options()

    requested = (args.get("mode") or "").strip().lower()
    if not requested:
        if not args.get("next"):
            return {
                "error": "pass either 'mode' (an exact name) or next=true",
                "current_mode": current,
                "options": options,
            }
        requested = ai_client.next_mode(current)

    if requested not in ai_client.PROMPT_MODES:
        return {
            "error": f"unknown mode '{requested}'",
            "current_mode": current,
            "options": options,
        }

    # A model must not raise the token budget on its own while a LOCAL model
    # is the one answering. mode is one global setting, so a raise made here
    # also becomes what every later ask runs at -- and on local hardware a
    # bigger prompt/answer budget is minutes of extra generation, not extra
    # quota. One logged turn showed the setting climbing (100% -> 150% ->
    # 400%) with two multi-minute gaps right after. Nothing in jarvis raises
    # it automatically on failure or retry; the only writers are a person
    # (web UI / `jarvis mode-set`, neither of which goes through this tool)
    # and this tool. Lowering is always allowed.
    requested_cost, current_cost = _mode_cost_pct(requested), _mode_cost_pct(current)
    if (requested_cost is not None and current_cost is not None
            and requested_cost > current_cost and _active_provider_is_local(cfg)):
        return {
            "error": (
                f"not changed: a local model is answering right now, and raising capacity "
                f"({ai_client.MODE_LABELS.get(current, current)} -> "
                f"{ai_client.MODE_LABELS.get(requested, requested)}) would make every reply much "
                f"slower on local hardware. Tell the user; they can raise it themselves from "
                f"the web UI's capacity switch or with `jarvis mode-set {requested}`."
            ),
            "refused": True,
            "current_mode": current,
            "options": options,
        }

    new_mode = ai_client.set_mode(requested)
    return {
        "ok": True,
        "mode": new_mode,
        "label": ai_client.MODE_LABELS[new_mode],
        "previous_mode": current,
        "previous_label": ai_client.MODE_LABELS.get(current, current),
        "note": "Takes effect starting with the user's next message, not this reply.",
    }


CAPACITY_TOOL_SCHEMAS = [
    {
        "name": "get_capacity_mode",
        "description": (
            "Read Jarvis's current prompt 'capacity' mode (how much history/commands/tool-"
            "schema detail is sent per ask) and every mode currently available."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_capacity_mode",
        "description": (
            "Change Jarvis's own prompt capacity mode \u2014 trades context/answer richness "
            "for tokens per ask (e.g. 400% Capacity=full, 100%=balanced default, "
            "50%=ultra compact). Call get_capacity_mode first if unsure which mode names "
            "are valid \u2014 the set of modes can grow over time. Use if the user asks to "
            "save tokens / go more compact, or wants deeper context / fuller answers. "
            "Takes effect starting with the user's NEXT message \u2014 don't claim your reply "
            "right now is already using the new mode. Only change it when the user asks; "
            "while a local model is answering, raising capacity is refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Exact mode name from get_capacity_mode's options, e.g. 'full', 'compact', 'ultra'. Omit if using next=true.",
                },
                "next": {
                    "type": "boolean",
                    "description": "true to step to the next mode in the cycle without knowing exact names.",
                },
            },
            "required": [],
        },
    },
]

CAPACITY_TOOLS = {
    "get_capacity_mode": tool_get_capacity_mode,
    "set_capacity_mode": tool_set_capacity_mode,
}