"""The one table of what a notification can be and what it can do (Phase E, E0).

Before this file the same facts were typed out by hand in six places: the
channel names and the level -> channel rule in notifier.py, the model-facing
help in actions/scheduler_tools.py, the position list in notifications.js, the
level and kind rows in settings.js, the `--position` text in cli.py and the
"valid channels" message in doctor.py. Adding a new kind of notification meant
finding all six.

Now they all read THIS table. It is plain data with no imports from the rest of
Jarvis, so anything (the notifier, a tool, the CLI, the web page through
`jarvis notify-config --catalog`) can load it without a circular import.

THE TABLES
----------
ACTIONS   Everything a notification can DO. Two types:
            channel    a place it is delivered (inbox, notification card, small
                       toast, desktop, voice, Playnite, Discord DM, Instagram
                       DM). Each needs a deliverer in notifier._DELIVERERS under
                       the same id. A channel row can also carry:
                         look     it draws on the open web page ("card" or
                                  "compact"; see LOOKS)
                         page_ok  a click on the web page may use it (the page
                                  must never reach desktop, voice or a DM)
                         dm       it messages the owner's phone
                       The row's `label` is the ONE name every screen shows.
            behaviour  a change in how it behaves on screen (stay until
                       acknowledged, blocking confirm). It becomes a flag on
                       the record (`persistent`, `confirm_required`).
PRESETS   The importance levels (MIN_LEVEL..MAX_LEVEL). Each one lists what it ADDS; a level
          includes everything below it, so level 4 is levels 1-3 plus its own.
KINDS     What a notification is about (reminder, task, daemon...). A kind has a
          label and a default level; Settings > Notifications builds its rows
          from this.
POSITIONS The eight places an in-app toast can appear.
LOOKS     How a notification is DRAWN on the open web page: a full `card`, a
          one-line `compact` toast (the same small toast the rest of the
          interface uses, e.g. "Autostart on") or `none`. Each look that draws
          is carried by exactly one action row (`stream`, `small_toast`).
TONES     The mark and colour of the compact toast (`info`, `success`). Not the
          importance level and not the toast's severity.

TO ADD ...
----------
a new KIND         one row in KINDS. Its default level reaches notify_config.json's
                   defaults and Settings picks up its row on its own.
a new ACTION       one row in ACTIONS plus a `_deliver_<id>` function in
                   notifier.py and its entry in `_DELIVERERS`. If a preset should
                   include it, add its id to that preset's `adds`.
a new PRESET/level one row in PRESETS (levels must stay 1..N with no gaps).
                   The tool help, CLI limits, Settings rows and badge text follow.
a new POSITION     one row in POSITIONS.
a new LOOK         one row in LOOKS, plus the ACTIONS row that carries it (and
                   its `_deliver_<id>`). The page needs a draw branch for it.
a new TONE         one name in TONES (the page maps it to a mark and colour).

Nothing else keeps its own list: no web page, no tool and no panel types out a
level name, a channel id or a position. They read this table (through
`catalog()`), and tests/test_no_hardcoded_notify_lists.py fails if one does.
tests/test_notify_registry.py fails when the table and the notifier disagree.
"""

import hashlib
import json

# ---------------------------------------------------------------------------
# ACTIONS
# ---------------------------------------------------------------------------

CHANNEL = "channel"
BEHAVIOUR = "behaviour"

# `model_hint`: the phrase put in the help text of the tools a model calls
# (remind_me, notify_me, ...). None keeps the action out of that text -- the DM
# channels are reached through level 4, which the owner chooses, so the model is
# not invited to pick them by name. (They are still ACCEPTED if named.)
ACTIONS = (
    {"id": "inbox", "type": CHANNEL, "label": "Inbox",
     "description": "The durable queue. Always written; it is the only action that "
                    "survives nothing being open.",
     "always": True, "page_ok": True,
     "model_hint": "'inbox' (always on \u2014 the durable queue the web console and "
                   "terminal both drain)"},
    # The id `stream` never changes (stored configs, scheduled jobs and tests use it);
    # only the label does, so two rows are not both called "toast".
    {"id": "stream", "type": CHANNEL, "label": "Notification card",
     "description": "A full notification card in the open web page: source, time, "
                    "buttons.",
     "page_ok": True, "look": "card",
     "model_hint": "'stream' (live in an open web chat)"},
    {"id": "small_toast", "type": CHANNEL, "label": "Small toast",
     "description": "A one-line notice in the corner of the open web page, the same "
                    "small toast the rest of the interface uses (for example "
                    "\"Autostart on\"). Never used for failures or anything that "
                    "needs acknowledging.",
     "page_ok": True, "look": "compact",
     "model_hint": "'small_toast' (a one-line notice in the open web page)"},
    {"id": "desktop", "type": CHANNEL, "label": "Desktop notification",
     "description": "The operating system's own notification (called `toast` "
                    "before E2; the old name still works).",
     "model_hint": "'desktop' (native OS notification; the old name 'toast' still works)"},
    {"id": "voice", "type": CHANNEL, "label": "Spoken aloud",
     "description": "Text-to-speech. Off unless voice is enabled in notify_config.json.",
     "model_hint": "'voice' (spoken aloud)"},
    {"id": "playnite", "type": CHANNEL, "label": "Playnite",
     "description": "A message inside Playnite, for when a game is full-screen.",
     "model_hint": "'playnite'"},
    {"id": "discord", "type": CHANNEL, "label": "Discord DM", "dm": True,
     "description": "A direct message to the owner on Discord.",
     "model_hint": None},
    {"id": "instagram", "type": CHANNEL, "label": "Instagram DM", "dm": True,
     "description": "A direct message to the owner on Instagram (only reachable "
                    "inside the 24-hour window the owner opens).",
     "model_hint": None},
    {"id": "persistent", "type": BEHAVIOUR, "label": "Stay until acknowledged",
     "description": "The toast does not auto-dismiss and re-surfaces until the "
                    "owner acknowledges it.",
     "record_flag": "persistent"},
    {"id": "confirm", "type": BEHAVIOUR, "label": "Blocking confirmation",
     "description": "A dialog that needs an explicit acknowledgment, not just a toast.",
     "record_flag": "confirm_required"},
)

# ---------------------------------------------------------------------------
# PRESETS (the importance levels)
# ---------------------------------------------------------------------------

# Cumulative: a level delivers its own `adds` AND everything the levels below it
# add. `help` is the short phrase used in the model-facing text.
PRESETS = (
    {"level": 1, "id": "silent", "name": "Silent",
     "description": "Inbox entry only \u2014 no toast, no interruption.",
     "help": "inbox only",
     "adds": (), "digest_eligible": True},
    {"level": 2, "id": "standard", "name": "Standard",
     "description": "Adds a notice in the open web page, small or card depending on "
                    "the notification, and a desktop notification. The default.",
     "help": "adds a native toast \u2014 the default",
     "adds": ("stream", "desktop"), "digest_eligible": True},
    {"level": 3, "id": "persistent", "name": "Persistent",
     "description": "The toast stays until acknowledged and re-surfaces every 10 minutes.",
     "help": "the toast stays until acknowledged",
     "adds": ("persistent",), "digest_eligible": False},
    {"level": 4, "id": "broadcast", "name": "Broadcast",
     "description": "Also sent as a DM to the owner on every connected chat platform.",
     "help": "also DMs the owner on every connected channel",
     "adds": ("discord", "instagram"), "digest_eligible": False},
    {"level": 5, "id": "confirm", "name": "Confirm",
     "description": "Needs an explicit acknowledgment in a blocking dialog.",
     "help": "requires an explicit acknowledgment, not just a toast",
     "adds": ("confirm",), "digest_eligible": False},
)

DEFAULT_LEVEL = 2

# ---------------------------------------------------------------------------
# KINDS
# ---------------------------------------------------------------------------

# `level` is the default importance when nothing else sets one. `look` / `tone`
# (both optional) are how it draws on the open page when the caller does not say
# (see LOOKS / TONES). `settings` says whether Settings > Notifications shows a
# row for it. A kind that is not listed
# here still works (it falls back to DEFAULT_LEVEL); listing it is what gives it
# a label, a default and a Settings row.
KINDS = (
    {"id": "reminder", "label": "Reminders", "level": 2, "settings": True},
    {"id": "notify", "label": "Messages from Jarvis", "level": 2, "settings": True},
    {"id": "task", "label": "Finished tasks", "level": 2, "settings": True},
    {"id": "clipboard_watch", "label": "Clipboard watcher", "level": 2, "settings": True},
    {"id": "ambient", "label": "Ambient suggestions", "level": 1, "settings": True},
    # H.3: the "reminder set / task scheduled" confirmation. Its own kind so it
    # can be turned down without touching the reminder that fires later.
    # Draws as the small toast (look "compact", tone "success"): it is only a
    # confirmation. At a level that needs acknowledging it still becomes a card.
    {"id": "scheduled", "label": "Reminder set / task scheduled", "level": 2,
     "settings": False, "look": "compact", "tone": "success"},
    # L.27 follow-up: a daemon that failed to autostart.
    {"id": "daemon", "label": "Daemon problems", "level": 2, "settings": True},
    # E4: the inbox record of a DM Jarvis sent to the owner (the notify_owner
    # tool). Silent by default: the person was already told, on their phone;
    # this is the record of it. No Settings row (file-only, like `scheduled`).
    {"id": "owner_dm", "label": "Messages Jarvis sent to your phone", "level": 1,
     "settings": False},
    # E4b: a reply from someone you are waiting on that was not urgent enough to
    # DM you (channels/awaiting.py). Silent: it is the record, not the alert. An
    # urgent one is recorded as `owner_dm` at the standard level instead.
    {"id": "reply_digest", "label": "Replies from people you are waiting on", "level": 1,
     "settings": False},
    # The Download Hub: what the hub daemon announces (started / finished / failed /
    # stalled) AND the one-line answers its panel gives to a click ("Demo downloads
    # started", "Scheduled", "That didn't work"). One kind, so the owner turns the
    # whole hub up or down in one Settings row.
    {"id": "download", "label": "Downloads", "level": 2, "settings": True},
)

# ---------------------------------------------------------------------------
# POSITIONS (E2 / D-11 / D-12)
# ---------------------------------------------------------------------------

# Listed in notifier.POSITIONS order (tests pin it). `menu` is the order a
# picker shows them in, bottom-right first because it is the default.
POSITIONS = (
    {"id": "top", "label": "Top", "menu": 5},
    {"id": "bottom", "label": "Bottom", "menu": 6},
    {"id": "left", "label": "Left", "menu": 7},
    {"id": "right", "label": "Right", "menu": 8},
    {"id": "top-left", "label": "Top left", "menu": 4},
    {"id": "top-right", "label": "Top right", "menu": 3},
    {"id": "bottom-left", "label": "Bottom left", "menu": 2},
    {"id": "bottom-right", "label": "Bottom right", "menu": 1},
)
DEFAULT_POSITION = "bottom-right"

# ---------------------------------------------------------------------------
# LOOKS and TONES
# ---------------------------------------------------------------------------

# How a notification is drawn on the open web page. `action` is the channel row that
# carries the look (that row's own `look` names it back; problems() checks they agree).
# `none` has no action: nothing is drawn, the record is still in the inbox.
LOOKS = (
    {"id": "card", "label": "Card", "action": "stream",
     "description": "A full notification card: source, time, title, body, buttons."},
    {"id": "compact", "label": "Small toast", "action": "small_toast",
     "description": "One line in the same small toast the rest of the interface uses."},
    {"id": "none", "label": "Nothing on the page", "action": None,
     "description": "Not drawn on the page. Still in the inbox."},
)
DEFAULT_LOOK = "card"
LOOK_NONE = "none"

# The mark and colour of the compact toast. `info` is the accent "i"; `success` is the
# green tick. Not the importance level (a number, see PRESETS) and not a severity.
TONES = ("info", "success")
DEFAULT_TONE = "info"

# ---------------------------------------------------------------------------
# Lookups (pure; the notifier and every other consumer use these)
# ---------------------------------------------------------------------------

def min_level():
    """The lowest level in PRESETS, read when asked (a test or a live edit may swap the table)."""
    return min(p["level"] for p in PRESETS)


def max_level():
    """The highest level in PRESETS, read when asked."""
    return max(p["level"] for p in PRESETS)


# Kept for importers (notifier.MIN_LEVEL / MAX_LEVEL and anything that read these names): the same
# values as before, computed once at import. Anything that VALIDATES or PRINTS a bound calls
# min_level() / max_level() instead, so it follows the table.
MIN_LEVEL = min_level()
MAX_LEVEL = max_level()


def actions(type_=None):
    """Every action, or only the `channel` / `behaviour` ones."""
    return tuple(a for a in ACTIONS if type_ is None or a["type"] == type_)


def action(action_id):
    for item in ACTIONS:
        if item["id"] == action_id:
            return item
    return None


def channel_ids():
    """The channel action ids, in table order. notifier.CHANNELS is this."""
    return tuple(a["id"] for a in ACTIONS if a["type"] == CHANNEL)


def behaviour_ids():
    return tuple(a["id"] for a in ACTIONS if a["type"] == BEHAVIOUR)


def page_ok_ids():
    """The actions a click on the web page may use. server.js allows exactly these."""
    return tuple(a["id"] for a in ACTIONS if a.get("page_ok"))


def dm_channel_ids():
    """The channels that message the owner's phone. notifier._DM_CHANNELS is this."""
    return tuple(a["id"] for a in ACTIONS if a["type"] == CHANNEL and a.get("dm"))


def label_for(action_id):
    """The one name for an action, or None if it is not in the table."""
    item = action(action_id)
    return item["label"] if item else None


def look_ids():
    return tuple(l["id"] for l in LOOKS)


def look(look_id):
    for item in LOOKS:
        if item["id"] == look_id:
            return item
    return None


def look_for_action(action_id):
    """The look an action draws, or None for an action that draws nothing."""
    item = action(action_id)
    return item.get("look") if item else None


def action_for_look(look_id):
    """The channel action id that draws a look, or None (`none` has no action)."""
    item = look(look_id)
    return item.get("action") if item else None


def look_action_ids():
    """Every action that draws on the page, in table order."""
    return tuple(a["id"] for a in ACTIONS if a.get("look"))


def look_actions_for_level(level):
    """The on-page actions a bare level delivers (level 2 -> ["stream"]; level 1 -> [])."""
    on = set(look_action_ids())
    return [i for i in channels_for_level(level) if i in on]


def level_look(level):
    """The look a level implies when nothing else asks: the first on-page action its
    preset delivers, else `none`."""
    for action_id in look_actions_for_level(level):
        return look_for_action(action_id)
    return LOOK_NONE


def tone_ids():
    return tuple(TONES)


def presets():
    return PRESETS


def preset(level):
    """The preset for a level number, or None."""
    for item in PRESETS:
        if item["level"] == level:
            return item
    return None


def level_names():
    """{1: "silent", ...}. notifier.LEVEL_NAMES is this."""
    return {p["level"]: p["id"] for p in PRESETS}


def _adds_up_to(level):
    out = []
    for item in PRESETS:
        if item["level"] <= level:
            for action_id in item["adds"]:
                if action_id not in out:
                    out.append(action_id)
    return out


def channels_for_level(level):
    """The channels a bare level implies, `inbox` first, before any explicit
    `channels` override. notifier._channels_for_level() is this."""
    always = [a["id"] for a in ACTIONS if a["type"] == CHANNEL and a.get("always")]
    added = [i for i in _adds_up_to(level) if (action(i) or {}).get("type") == CHANNEL]
    return always + [i for i in added if i not in always]


def behaviours_for_level(level):
    """The behaviour action ids a level includes, e.g. 3 -> ["persistent"]."""
    return [i for i in _adds_up_to(level) if (action(i) or {}).get("type") == BEHAVIOUR]


def flags_for_level(level):
    """{record flag: bool} for EVERY behaviour, so a record always carries the
    same keys: {"persistent": True, "confirm_required": False} at level 3."""
    on = set(behaviours_for_level(level))
    return {a["record_flag"]: a["id"] in on for a in ACTIONS if a["type"] == BEHAVIOUR}


def digest_eligible(level):
    item = preset(level)
    return bool(item and item.get("digest_eligible"))


def kinds():
    return KINDS


def kind(kind_id):
    for item in KINDS:
        if item["id"] == kind_id:
            return item
    return None


def default_levels():
    """{kind id: default level}. notifier.DEFAULT_CONFIG["levels"] starts as this."""
    return {k["id"]: k["level"] for k in KINDS}


def position_ids():
    return tuple(p["id"] for p in POSITIONS)


def positions_menu():
    """The positions in picker order (default first)."""
    return tuple(sorted(POSITIONS, key=lambda p: p["menu"]))


# ---------------------------------------------------------------------------
# Text the model sees (built from the table, so it cannot drift from it)
# ---------------------------------------------------------------------------


def level_help():
    """The `level` parameter's description for the scheduler tools."""
    parts = ["%d %s (%s)" % (p["level"], p["id"], p["help"]) for p in PRESETS]
    return ("How important this is, %d-%d (K.2.7): %s. Omit to use the configured default."
            % (min_level(), max_level(), ", ".join(parts)))


def channel_help():
    """The `channels` parameter's description for the scheduler tools."""
    hints = [a["model_hint"] for a in ACTIONS if a["type"] == CHANNEL and a.get("model_hint")]
    return ("Where to deliver it: %s. Omit to use the user's configured defaults, "
            "which is almost always right." % ", ".join(hints))


# ---------------------------------------------------------------------------
# The catalog handed to everything that uses the notification system
# ---------------------------------------------------------------------------


def _public(item, drop=("model_hint",)):
    return {k: v for k, v in item.items() if k not in drop}


def catalog(config=None):
    """One JSON-able document describing everything above, plus (when `config`
    is given) the owner's current defaults. This is what `jarvis notify-config --catalog`
    prints, what `/api/notifications/catalog` serves and what the web page, the
    Settings screen and the tools read instead of keeping their own lists.

    `version` is a short hash of the table itself (not of `config`), so a
    consumer can tell whether its cached copy is stale.
    """
    body = {
        "actions": [_public(a) for a in ACTIONS],
        "presets": [dict(p, adds=list(p["adds"]), channels=channels_for_level(p["level"]),
                         behaviours=behaviours_for_level(p["level"]),
                         flags=flags_for_level(p["level"]))
                    for p in PRESETS],
        "kinds": [dict(k) for k in KINDS],
        "looks": [dict(l) for l in LOOKS],
        "tones": list(TONES),
        "default_look": DEFAULT_LOOK,
        "default_tone": DEFAULT_TONE,
        "positions": [dict(p) for p in positions_menu()],
        "min_level": min_level(),
        "max_level": max_level(),
        "default_level": DEFAULT_LEVEL,
        "default_position": DEFAULT_POSITION,
    }
    blob = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    body["version"] = hashlib.sha1(blob).hexdigest()[:12]
    if isinstance(config, dict):
        body["configured"] = {
            "enabled": bool(config.get("enabled", True)),
            "levels": dict(config.get("levels") or {}),
            "position": config.get("position") or DEFAULT_POSITION,
            "positions": dict(config.get("positions") or {}),
        }
    return body


def problems(deliverer_ids=None):
    """Mistakes in the table itself, as a list of sentences (empty = fine).
    With `deliverer_ids` (notifier._DELIVERERS' keys) it also checks that every
    channel has a deliverer and no deliverer is orphaned. Used by the test and
    by `jarvis doctor`; nothing here raises."""
    out = []
    ids = [a["id"] for a in ACTIONS]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        out.append("action id %r appears more than once" % dup)
    for a in ACTIONS:
        if a["type"] not in (CHANNEL, BEHAVIOUR):
            out.append("action %r has unknown type %r" % (a["id"], a["type"]))
        if a["type"] == BEHAVIOUR and not a.get("record_flag"):
            out.append("behaviour %r has no record_flag" % a["id"])
    levels = [p["level"] for p in PRESETS]
    if levels != list(range(1, len(levels) + 1)):
        out.append("preset levels must run 1..N in order, got %r" % levels)
    for p in PRESETS:
        for action_id in p["adds"]:
            if action(action_id) is None:
                out.append("preset %d adds unknown action %r" % (p["level"], action_id))
    if preset(DEFAULT_LEVEL) is None:
        out.append("DEFAULT_LEVEL %r is not a preset" % DEFAULT_LEVEL)
    for k in KINDS:
        if preset(k["level"]) is None:
            out.append("kind %r default level %r is not a preset" % (k["id"], k["level"]))
    if DEFAULT_POSITION not in position_ids():
        out.append("DEFAULT_POSITION %r is not a position" % DEFAULT_POSITION)
    look_list = [l["id"] for l in LOOKS]
    for dup in sorted({i for i in look_list if look_list.count(i) > 1}):
        out.append("look id %r appears more than once" % dup)
    if DEFAULT_LOOK not in look_list:
        out.append("DEFAULT_LOOK %r is not a look" % DEFAULT_LOOK)
    if LOOK_NONE not in look_list:
        out.append("there must be a %r look" % LOOK_NONE)
    if DEFAULT_TONE not in TONES:
        out.append("DEFAULT_TONE %r is not a tone" % DEFAULT_TONE)
    for l in LOOKS:
        if l.get("action") is None:
            continue
        carrier = action(l["action"])
        if carrier is None:
            out.append("look %r names the unknown action %r" % (l["id"], l["action"]))
        elif carrier.get("look") != l["id"]:
            out.append("look %r names action %r, but that action's look is %r"
                       % (l["id"], l["action"], carrier.get("look")))
    for a in ACTIONS:
        if a.get("look") is None:
            continue
        if a["type"] != CHANNEL:
            out.append("action %r has a look but is not a channel" % a["id"])
        if a["look"] not in look_list:
            out.append("action %r has the unknown look %r" % (a["id"], a["look"]))
        elif (look(a["look"]) or {}).get("action") != a["id"]:
            out.append("look %r is carried by more than one action (%r is one)"
                       % (a["look"], a["id"]))
    for k in KINDS:
        if k.get("look") is not None and k["look"] not in look_list:
            out.append("kind %r has the unknown look %r" % (k["id"], k["look"]))
        if k.get("tone") is not None and k["tone"] not in TONES:
            out.append("kind %r has the unknown tone %r" % (k["id"], k["tone"]))
    if deliverer_ids is not None:
        have = set(deliverer_ids)
        for cid in channel_ids():
            if cid not in have:
                out.append("channel %r has no deliverer in notifier._DELIVERERS" % cid)
        for extra in sorted(have - set(channel_ids())):
            out.append("deliverer %r has no channel row in notify_registry.ACTIONS" % extra)
    return out
