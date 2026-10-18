"""notification_test: send a test notification through the REAL notifier, and preview what it will do.

Menu > Notification test is the panel for this tool (TOOL_UI below, folder notification_test_ui/).
The tool exists so you can see, on your own screen, what a notification looks like at any
importance level, through any combination of notification types, with any of the flags, without
waiting for a reminder or a download to fire one.

HOW IT STAYS HONEST
-------------------
* It goes through notifier.notify() and nothing else, with source "notification_test" and a kind
  that is a row of notify_registry.KINDS. There is no second way of drawing a notification here.
* The preview comes from notifier.resolve_delivery(): the SAME function notify() calls, so what
  the preview says (which channels, which look, whether a small toast was forced to a card and
  why, whether the level mutes it) is what the record then does.
* Every list it offers (levels, notification types, kinds, positions, tones) is read from the
  notification registry when the tool is called. Nothing here is typed out, so a level, type,
  kind or position added to notify_registry.py is testable here with no edit to this file.
* It never takes a recipient. The types that message the owner's phone (the `dm` rows: Discord and
  Instagram) are shown but refused: they would send a real message. Use `jarvis channels-test`
  for those. A level whose preset would add them (Broadcast and above) is tested WITHOUT them: the
  tool delivers through that level's other channels, applies its flags, and says in the preview which
  phone channels it left out (`skipped_dm`).

ARGUMENTS (all optional)
------------------------
  describe   true  -> the registry catalog, for the panel (no notification is sent)
  preview    true  -> what WOULD happen, nothing is sent
  dismiss    [ids] -> delete those test notifications from the inbox again
  level      an importance level (any preset of the registry); omit for the kind's own level
  types      notification types to deliver through, as an explicit list (like a scheduled job's
             `channels`); omit to use whatever the level delivers
  failed     treat it as a failure (a small toast is then drawn as a card)
  persistent stay until acknowledged
  confirm    a blocking confirmation
  kind       a registry kind (default "notify")
  position   a registry position
  count      1-5 copies in a row, to see bursts grouped ("x3")
  title / message   optional; the default says what was requested
"""

from __future__ import annotations

from .. import notify_registry as registry
from .. import notifier

TOOL_GROUP = "notifications"

SOURCE = "notification_test"
DEFAULT_KIND = "notify"
MAX_COUNT = 5

TOOL_KEYWORDS = {
    "notification_test": {
        "test notification": 10,
        "notification test": 10,
        "try a notification": 10,
        "send a test notification": 10,
        "test a notification": 10,
        "preview a notification": 8,
        "test the notifications": 9,
        "how does a notification look": 8,
    },
}

TOOL_SCHEMAS = [
    {
        "name": "notification_test",
        "description": (
            "Send a TEST notification through Jarvis's real notifier, or preview what one would do, "
            "so the user can see how a notification looks at a chosen importance level, through "
            "chosen notification types, with chosen flags. Use it when the user wants to try or test "
            "notifications. With preview set it sends nothing and returns the channels, the look "
            "(card, compact, none) and whether the level mutes it. It never messages anyone's phone "
            "(Discord, Instagram): those are refused. Use describe to list the levels, types, kinds "
            "and positions that exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "describe": {"type": "boolean", "description": "Return the catalog of levels, notification types, kinds and positions instead of sending."},
                "preview": {"type": "boolean", "description": "Return what would happen without sending anything."},
                "dismiss": {"type": "array", "items": {"type": "string"}, "description": "Ids of test notifications to delete from the inbox."},
                "level": {"type": "integer", "description": "Importance level; the choices are in describe (levels). Omit for the kind's own level."},
                "types": {"type": "array", "items": {"type": "string"},
                          "description": "Notification types to deliver through, as an explicit list. Omit to use what the level delivers. See describe (actions)."},
                "failed": {"type": "boolean", "description": "Treat it as a failure."},
                "persistent": {"type": "boolean", "description": "Stay until acknowledged."},
                "confirm": {"type": "boolean", "description": "A blocking confirmation."},
                "kind": {"type": "string", "description": "A notification kind (default 'notify'); see describe (kinds)."},
                "position": {"type": "string", "description": "Where it appears on the page; see describe (positions)."},
                "count": {"type": "integer", "description": "1-5 copies in a row, to see bursts grouped."},
                "title": {"type": "string", "description": "Optional title."},
                "message": {"type": "string", "description": "Optional message; the default says what was requested."},
            },
        },
    },
]

# Same shape as the notifications the user already sees; nothing to confirm (a test only ever
# reaches the user's own screen and inbox, never a recipient).
TOOL_CONFIRM_REQUIRED = set()
TOOL_AI_REVIEW = set()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fail(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def _types_from(args):
    """(types, error). Every entry must be an action row of the registry (the legacy name
    `toast` for the desktop notification is accepted)."""
    raw = args.get("types")
    if raw in (None, "", []):
        return [], None
    if isinstance(raw, str):
        raw = [t for t in raw.replace(";", ",").split(",") if t.strip()]
    if not isinstance(raw, (list, tuple)):
        return None, "types must be a list of notification types"
    channels, behaviours = notifier.canonical_actions(raw)
    known = set(channels) | set(behaviours)
    bad = [str(t) for t in raw if notifier.canonical_channel(t) is None and notifier.canonical_behaviour(t) is None]
    if bad:
        ids = ", ".join(a["id"] for a in registry.actions())
        return None, "unknown notification type %s; the types are: %s" % (", ".join(repr(b) for b in bad), ids)
    dm = [c for c in known if (registry.action(c) or {}).get("dm")]
    if dm:
        return None, ("%s would send a real message to the owner's phone, so the test tool refuses it. "
                      "Use `jarvis channels-test` to test those." % ", ".join(sorted(dm)))
    return list(channels) + list(behaviours), None


def _requested(args):
    """Validate the arguments once and return (spec, error). `spec` holds everything the preview and
    the send both need, so the two can never disagree."""
    types, err = _types_from(args)
    if err:
        return None, err
    kind = str(args.get("kind") or DEFAULT_KIND).strip().lower()
    if registry.kind(kind) is None:
        return None, "unknown kind %r; the kinds are: %s" % (kind, ", ".join(k["id"] for k in registry.KINDS))
    position = args.get("position")
    if position not in (None, ""):
        position = str(position).strip().lower()
        if position not in registry.position_ids():
            return None, "unknown position %r; the positions are: %s" % (position, ", ".join(registry.position_ids()))
    else:
        position = None
    level = args.get("level")
    if level in (None, ""):
        level = None
    else:
        try:
            level = int(level)
        except (TypeError, ValueError):
            return None, "level must be a whole number, %d-%d" % (registry.MIN_LEVEL, registry.MAX_LEVEL)
        if not (registry.MIN_LEVEL <= level <= registry.MAX_LEVEL):
            return None, "level must be between %d and %d" % (registry.MIN_LEVEL, registry.MAX_LEVEL)
    try:
        count = int(args.get("count") or 1)
    except (TypeError, ValueError):
        return None, "count must be a whole number from 1 to %d" % MAX_COUNT
    if not (1 <= count <= MAX_COUNT):
        return None, "count must be between 1 and %d" % MAX_COUNT

    # The behaviour flags are behaviour ids in the same list a scheduled job uses.
    flag_ids = []
    if args.get("persistent") and "persistent" in registry.behaviour_ids():
        flag_ids.append("persistent")
    if args.get("confirm") and "confirm" in registry.behaviour_ids():
        flag_ids.append("confirm")
    all_types = list(dict.fromkeys(types + flag_ids))
    return {
        "types": all_types,
        "kind": kind,
        "position": position,
        "level": level,
        "failed": bool(args.get("failed")),
        "count": count,
    }, None


def _describe_request(spec, resolved_level):
    bits = ["level %d" % resolved_level]
    if spec["types"]:
        bits.append(", ".join((registry.label_for(t) or t).lower() for t in spec["types"]))
    if spec["failed"]:
        bits.append("failed")
    return "Test: " + ", ".join(bits)


def _effective(spec, config):
    """(level, types, skipped_dm): what is really delivered. With no explicit types the level's own
    channels are used, MINUS the phone-messaging ones: a test must never message the owner's phone, and
    a level such as Broadcast would. Explicit channels make the list explicit (so the notifier does not
    add them back); the level's flags still apply because they come from the level, not the list."""
    level = notifier.normalize_level(spec["level"], kind=spec["kind"], source=SOURCE, config=config)
    channels, behaviours = notifier.canonical_actions(spec["types"])
    skipped = []
    if not channels:
        wanted = list(registry.channels_for_level(level))
        skipped = [c for c in wanted if (registry.action(c) or {}).get("dm")]
        if skipped:
            channels = [c for c in wanted if c not in skipped]
    return level, list(channels) + list(behaviours), skipped


def _preview(spec):
    config = notifier._load_config()
    level, types, skipped = _effective(spec, config)
    channels, behaviours = notifier.canonical_actions(types)
    plan = notifier.resolve_delivery(level, channels or None, kind=spec["kind"], failed=spec["failed"],
                                     behaviours=behaviours, config=config)
    plan["level"] = level
    plan["kind"] = spec["kind"]
    plan["skipped_dm"] = skipped
    plan["send_types"] = types
    plan["default_title"] = _describe_request(spec, level)
    return plan


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------


def tool_notification_test(args):
    args = args or {}
    if args.get("describe"):
        return {"ok": True, "catalog": registry.catalog(), "max_count": MAX_COUNT, "default_kind": DEFAULT_KIND,
                "source": SOURCE}
    if args.get("dismiss"):
        ids = [str(i) for i in (args.get("dismiss") or []) if str(i).strip()]
        return {"ok": True, "dismissed": notifier.dismiss(ids)}

    spec, err = _requested(args)
    if err:
        return _fail(err)
    plan = _preview(spec)
    if args.get("preview"):
        return {"ok": True, "preview": plan}

    title = str(args.get("title") or "").strip() or "Notification test"
    message = str(args.get("message") or "").strip() or plan["default_title"]
    results = []
    for _ in range(spec["count"]):
        record = notifier.notify(
            title, message,
            channels=plan["send_types"] or None,
            kind=spec["kind"], failed=spec["failed"], level=spec["level"],
            source=SOURCE, position=spec["position"],
        )
        results.append({
            "id": record.get("id"),
            "level": record.get("level"),
            "look": record.get("look"),
            "tone": record.get("tone"),
            "delivered": list(record.get("delivered") or []),
            "failed_channels": list(record.get("failed_channels") or []),
            "deferred_to_digest": bool(record.get("deferred_to_digest")),
        })
    return {"ok": True, "sent": len(results), "results": results, "preview": plan,
            "forced_card": plan["forced_card"], "muted": plan["muted"]}


TOOLS = {"notification_test": tool_notification_test}


# ---------------------------------------------------------------------------
# the Menu panel
# ---------------------------------------------------------------------------

TOOL_UI = [
    {
        "id": "notification_test_panel",
        "label": "Notification test",
        "mode": "menu",
        "size": "wide",
        "path": "notification_test_ui",
        "title": "Notification test",
        "hint": "Try a notification at any level, through any type",
        "tool": "notification_test",
    },
]


# ---------------------------------------------------------------------------
# Test Checklist (Menu > Test Checklist)
# ---------------------------------------------------------------------------

TEST_CHECKLIST_GROUP = {
    "label": "Notifications (test tool)",
    "blurb": "The notification test tool and Menu > Notification test: every level, type and flag through the real notifier.",
}

TEST_CHECKLIST = {
    "notification_test": {
        "does": "Sends a test notification through the real notifier at a chosen level, through chosen types, with chosen flags, and previews what it will do.",
        "steps": [
            {"run": {"level": 2},
             "expect": "ok: true, one result at level 2 with look 'card'; a notification card appears in the open web page and one row is in the Notifications panel."},
            {"run": {"types": ["small_toast"]},
             "expect": "ok: true, look 'compact': a one-line toast appears (the same small toast as 'Autostart on' in the Daemons panel), not a card."},
            {"run": {"types": ["small_toast"], "failed": True},
             "expect": "ok: true, forced_card true, look 'card': a failure is never drawn as the small toast."},
            {"run": {"types": ["small_toast"], "persistent": True},
             "expect": "ok: true, forced_card true, a card that stays until you acknowledge it."},
            {"run": {"level": 1},
             "expect": "ok: true, muted true, look 'none': nothing on the page, but the record is in the Notifications panel."},
            {"run": {"level": 2, "count": 3},
             "expect": "ok: true, three results; the cards group into one with 'x3'."},
            {"run": {"preview": True, "level": 2, "types": ["small_toast"], "failed": True},
             "expect": "ok: true with a preview only (nothing appears): channels, look 'card', forced_card true and the reason."},
            {"run": {"types": ["discord"]},
             "expect": "ok: false, says it would message the owner's phone and points at `jarvis channels-test`; nothing is sent."},
            {"run": {"describe": True},
             "expect": "ok: true, the catalog lists every level, notification type, kind and position."},
            {"ask": "Send me a test notification.",
             "expect": "Calls notification_test and says what was sent."},
        ],
        "care": "Only reaches this machine's own screen and inbox, never a recipient. Delete the test rows afterwards from the Notifications panel, or from the Notification test panel's Results list.",
        "watch": ["The preview and the record it then produces must agree (same look, same channels)."],
    },
}
