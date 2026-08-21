"""Send a notification to the paired phone via KDE Connect (stderr marker)."""

import sys


def _clean(value, limit):
    text = " ".join(str(value or "").split())
    return text[:limit]


def tool_notify_phone(args):
    args = args or {}
    title = _clean(args.get("title") or "Jarvis", 80) or "Jarvis"
    body = _clean(args.get("body") or args.get("text") or args.get("message") or "", 400)
    if not body:
        return {"needs_clarification": True, "message": "What should the phone notification say?"}
    print(f"JARVIS_NOTIFY\t{title}\t{body}", file=sys.stderr, flush=True)
    return {"ok": True, "title": title, "body": body}


NOTIFY_TOOL_SCHEMAS = [
    {
        "name": "notify_phone",
        "description": (
            "Send a notification to the user's paired phone through KDE Connect. "
            "Use when they ask to ping/notify the phone or get their attention."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["body"],
        },
    },
]

NOTIFY_TOOLS = {
    "notify_phone": tool_notify_phone,
}
