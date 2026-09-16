"""Auto-discovered tool: let Jarvis DM the owner on Discord/Instagram.

This is the "tell me when it's done" half of the channels feature. A long
task (a download, a build, a scheduled job) finishes while the user is
somewhere else, and Jarvis reaches them on their phone instead of leaving a
toast on a PC nobody is looking at.

Kept deliberately narrow: it sends a short text message to the ONE account
configured as `owner` in ~/.jarvis/channels.json. It cannot pick a
recipient. That is the entire security model of this tool — a message tool
that took a target would let any prompt injection reachable from a chat
channel turn Jarvis into a spam relay against arbitrary users, which is a
much worse outcome than the tool simply not existing.

Delivery is best-effort by design, and the result says why when it isn't:
Instagram in particular can only be reached inside a 24-hour window the
owner has to open themselves (see channels/instagram_gateway.py). The
notifier inbox still records everything, so an undeliverable notification
is never a lost one.
"""


def tool_notify_owner(args):
    # Lazy import: anything reaching back into jarvis.tools during
    # discovery would hit a circular import (see actions/_template.py's
    # SAFE IMPORTS section). channels.* doesn't, but notifier does, so keep
    # the whole block inside the handler rather than reasoning per-module.
    from ..channels import outbound, PLATFORMS

    message = (args.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "message is required"}

    platform = (args.get("platform") or "").strip().lower()
    if platform in ("", "any", "auto"):
        wanted = None          # notify_owner() walks every enabled platform
    elif platform in PLATFORMS:
        wanted = [platform]
    else:
        return {"ok": False,
                "error": f"unknown platform '{platform}' — use discord, instagram, or leave blank"}

    # all=true sends everywhere rather than stopping at the first success.
    first_only = not bool(args.get("all"))

    outcome = outbound.notify_owner(message, platforms=wanted,
                                    first_success_only=first_only)

    delivered = [r["platform"] for r in outcome["results"] if r["ok"]]
    failed = [f"{r['platform']}: {r['detail']}"
              for r in outcome["results"] if not r["ok"]]

    if outcome["ok"]:
        return {"ok": True, "delivered_to": delivered,
                # Failures are still reported on success so the model can
                # tell the user "sent on Discord, Instagram's window was
                # closed" rather than implying everything worked.
                "not_delivered": failed}
    return {"ok": False,
            "error": "could not reach the owner on any channel",
            "detail": failed,
            "hint": ("Check `jarvis channels-status`. The notification is "
                     "still in the Jarvis inbox either way.")}


TOOL_SCHEMAS = [
    {
        "name": "notify_owner",
        "description": (
            "Send a short direct message to the owner on Discord or Instagram. "
            "Use this to report that a long-running task finished, or to flag "
            "something that needs attention when the user may not be at the PC. "
            "Only reaches the single configured owner account — it cannot "
            "message anyone else."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to tell the owner. Keep it to a sentence or two.",
                },
                "platform": {
                    "type": "string",
                    "enum": ["discord", "instagram", "any"],
                    "description": (
                        "Where to send it. Omit or use 'any' to try each "
                        "configured channel until one works."
                    ),
                },
                "all": {
                    "type": "boolean",
                    "description": (
                        "Send on every configured channel instead of stopping "
                        "at the first success. Default false."
                    ),
                },
            },
            "required": ["message"],
        },
    },
]

TOOLS = {"notify_owner": tool_notify_owner}

# Its own group. It isn't a sibling of anything existing: 'notify' in
# tool_registry terms today means the local scheduler's notification kinds,
# and folding an outbound chat message into system_control or commands
# would mean either group's keyword hit drags this along with it.
TOOL_GROUP = "channels"

TOOL_KEYWORDS = {
    "notify_owner": {
        "notify me": 10,
        "let me know": 9,
        "tell me when": 10,
        "dm me": 10,
        "message me": 9,
        "text me": 8,
        "ping me": 9,
        # Weighted low: "when you're done" is common phrasing in requests
        # that don't want a notification at all, so it nudges rather than
        # decides. Router weights are additive, so this still helps when it
        # co-occurs with one of the stronger phrases above.
        "when you're done": 5,
        "when it's finished": 5,
    },
}

TOOL_PACK_INSTRUCTION = (
    "notify_owner DMs the owner on a chat app. Call it when the user asked to "
    "be told once something finishes, or when you finish a long task they "
    "asked for and they may have walked away. Don't call it for an answer you "
    "are already giving them in this reply — they are reading it right now."
)
