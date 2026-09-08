"""Lets Jarvis read and change its own prompt "capacity" mode (see
ai_client.PROMPT_MODE_DEFS) from inside an ask, the same way a person can
via the web UI's capacity switch or `jarvis mode-set`.

Imports ai_client lazily (inside the functions, not at module load) since
ai_client itself imports tools.py (which imports this module) at its own
top \u2014 a top-level `from . import ai_client` here would be a circular
import at the exact moment ai_client is still mid-initialization.
"""


def tool_get_capacity_mode(args=None):
    from . import ai_client, ai_config

    cfg = ai_config.load_ai_config()
    mode = ai_client.current_mode(cfg)
    return {
        "mode": mode,
        "label": ai_client.MODE_LABELS.get(mode, mode),
        "options": [
            {"mode": m, "label": ai_client.MODE_LABELS[m]} for m in ai_client.PROMPT_MODES
        ],
    }


def tool_set_capacity_mode(args):
    from . import ai_client, ai_config

    args = args or {}
    cfg = ai_config.load_ai_config()
    current = ai_client.current_mode(cfg)
    options = [{"mode": m, "label": ai_client.MODE_LABELS[m]} for m in ai_client.PROMPT_MODES]

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
            "right now is already using the new mode."
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