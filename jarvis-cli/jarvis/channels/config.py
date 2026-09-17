"""Channel configuration (~/.jarvis/channels.json).

One file, both platforms, every knob. Same plain-JSON, created-on-first-use,
re-read-every-call approach as notifier.py / tool_safety.py / ai_config.py,
so there is no reload step and the file stays hand-editable.

THE FOUR SETS ARE GENUINELY INDEPENDENT
---------------------------------------
    dm_allowlist    who may open a DM conversation at all
    reply_allowlist who gets an actual answer back
    tool_allowlist  whose request may cause a tool to run
    owner           the single account that receives outbound DMs

They are checked in that order and nothing is inherited between them. That
is the point: "my friend can ping Jarvis in the server and get an answer,
but only I can make it touch my PC" is exactly one entry in reply_allowlist
and zero in tool_allowlist. The owner is NOT auto-added to the other three
either — an owner who can receive notifications but not issue commands is a
legitimate configuration, and silently granting permissions the user didn't
write down is how a permission system stops being one.

EMPTY MEANS DENY, "*" MEANS EVERYONE
------------------------------------
An empty allowlist denies everyone. It does NOT mean "unrestricted".

This is the single most important decision in this file, so it is worth
being explicit about the alternative: if empty meant "allow all", then a
fresh install with a bot token pasted in would answer any stranger who
pinged it, and — with allow_tools on — let that stranger run desktop
automation on the owner's machine. A config file the user hasn't finished
filling in must fail closed.

To actually open a set up, put the literal string "*" in it. That way
"everyone may ping me" is a thing the user typed on purpose, not a thing
that happened because they left a list blank.

IDENTITY MATCHING
-----------------
Entries are matched against BOTH the platform's stable numeric id and its
human-readable handle, case-insensitively, with a leading @ stripped:

    "1234567890"     Discord snowflake / Instagram IGSID  (stable, preferred)
    "someuser"       Discord username / Instagram handle  (convenient, mutable)
    "@someuser"      same thing, @ is stripped

Prefer ids. Discord usernames are changeable and Instagram handles are
changeable, so a handle-based allowlist is one rename away from either
locking out the owner or — worse — handing their slot to whoever claimed
the freed-up name. The handle form exists because asking someone to find a
snowflake before they can test their own bot is a bad first-run experience,
not because it's the right long-term answer. `jarvis channels-whoami`
prints the stable id for exactly this reason.
"""

import json
import os
from pathlib import Path

from . import DISCORD, INSTAGRAM, PLATFORMS, PERM_SETS

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "channels.json"
ENCODING = "utf-8"

# Wildcard entry. Deliberately a string the user has to type, see above.
WILDCARD = "*"

# Per-platform block shared by both platforms. Anything platform-specific
# is layered on top in _PLATFORM_EXTRA below, so the common permission
# shape stays literally identical between them — permissions.py can then be
# written once against this shape with no per-platform branching.
_COMMON_DEFAULTS = {
    "enabled": False,

    # --- the four sets -----------------------------------------------
    "owner": "",
    "dm_allowlist": [],
    "reply_allowlist": [],
    "tool_allowlist": [],

    # --- gating ------------------------------------------------------
    # require_mention applies to group contexts (a Discord guild channel,
    # an Instagram comment thread). DMs are gated by dm_allowlist instead —
    # requiring someone to @mention the bot inside a 1:1 DM with that same
    # bot is pointless ceremony.
    "require_mention": True,
    "respond_in_dms": True,

    # Master switch for tool execution on this platform, ANDed with the
    # per-user tool_allowlist. Off means "answer questions, touch nothing" —
    # a safe way to expose Jarvis to a server without exposing the PC.
    "allow_tools": False,

    # --- behaviour ---------------------------------------------------
    "log_conversations": True,
    # Platform hard caps: Discord 2000 chars/message, Instagram 1000 BYTES.
    # Overridden per platform below; the value here is just a floor.
    "max_reply_chars": 1900,
    # Silently ignore rather than reply "you're not allowed". A denial
    # notice is useful while setting up and noise forever after, so it is a
    # knob rather than a hardcoded choice.
    "notify_on_denied": False,
    # Seconds a single user must wait between accepted messages. Cheap
    # protection against someone discovering that pinging the bot costs the
    # owner real API tokens.
    "cooldown_seconds": 3,

    # --- per-scope overrides -----------------------------------------
    # Narrow the rules for one server / group / channel without changing
    # the platform defaults. Keys are "guild:<id>", "channel:<id>" or
    # "thread:<id>"; values are a partial platform block — only the keys
    # you actually write are overridden, everything else inherits.
    #
    #   "scopes": {
    #     "guild:1234": {"reply_allowlist": ["*"], "allow_tools": false}
    #   }
    #
    # More specific wins: thread > channel > guild > platform default.
    # This is what makes "answer anyone in my friends' server, but never
    # run tools there, while my own DMs keep full access" expressible.
    "scopes": {},

    # The bot's own @handle, for platforms where a mention is plain text
    # rather than a structured entity (Instagram). Matched case-insensitively
    # against the message body. Unused on Discord, which sends real mention
    # objects and needs no text matching.
    "bot_handle": "",
}

_PLATFORM_EXTRA = {
    DISCORD: {
        "bot_token": "",
        # Filled in automatically on first successful connect — used to
        # detect "was I mentioned" without a privileged intent and to skip
        # the bot's own messages.
        "bot_user_id": "",
        # Restrict to specific servers/channels. Empty = no restriction
        # (unlike the allowlists — these are filters on *where*, not *who*,
        # and the who-gate already fails closed).
        "allowed_guilds": [],
        "allowed_channels": [],
        # Discord marks message content privileged, BUT explicitly exempts
        # DMs to the bot and messages that mention it — which is exactly
        # and only what this integration reads. So the default is off and
        # the bot works without ticking anything in the Developer Portal.
        # See the Guides panel for the full explanation.
        "message_content_intent": False,
        "max_reply_chars": 1900,
        # --- acknowledgement ------------------------------------------
        # A reaction on the user's own message, as a read receipt. The
        # typing indicator already covers "working on it", but it only
        # starts once handle_message is underway and it says nothing about
        # whether the message was ACCEPTED — a message rejected by the
        # allowlist and a message being thought about look identical
        # (silence) for the several seconds a tool-calling turn takes.
        #
        # So: 👀 the instant it's picked up, replaced by ✅ when the reply
        # lands or ⚠️ if it failed. Three states, no text, no extra message
        # in the channel.
        "read_receipts": True,
        "reaction_seen": "\U0001F440",     # 👀 received
        "reaction_done": "\u2705",          # ✅ answered
        "reaction_failed": "\u26a0\ufe0f",  # ⚠️ something went wrong
        # Reacting to a denied message tells a stranger the bot is watching
        # and that they're on a list. Off by default for that reason.
        "react_when_denied": False,
    },
    INSTAGRAM: {
        # Instagram API with Instagram Login (no Facebook Page needed since
        # July 2024) OR the older Messenger-Platform-via-Page route. Both
        # end up POSTing to the same graph endpoint with a token.
        "access_token": "",
        "ig_user_id": "",
        "app_secret": "",          # verifies X-Hub-Signature-256 on webhooks
        "verify_token": "",        # echoed back during webhook subscription
        "graph_version": "v21.0",
        "webhook_host": "127.0.0.1",
        "webhook_port": 19824,
        "webhook_path": "/webhook",
        # Instagram counts BYTES, not characters, and caps at 1000.
        "max_reply_chars": 900,
    },
}


def _platform_defaults(platform):
    merged = json.loads(json.dumps(_COMMON_DEFAULTS))  # deep copy
    merged.update(json.loads(json.dumps(_PLATFORM_EXTRA.get(platform, {}))))
    return merged


def default_config():
    cfg = {p: _platform_defaults(p) for p in PLATFORMS}
    cfg["_comment"] = (
        "Allowlists: empty = nobody, \"*\" = everyone. Entries match a user id "
        "or a handle (case-insensitive, leading @ optional). Run "
        "`jarvis channels-whoami` to print stable ids."
    )
    return cfg


def ensure_config():
    """Write defaults out if absent and return the path, so a CLI command
    can print something the user can immediately open. Mirrors
    notifier.ensure_config()/ai_config.ensure_ai_config()."""
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            _atomic_write(default_config())
    except OSError:
        pass
    return CONFIG_FILE


def _atomic_write(data):
    """Write via temp-file + os.replace. The gateways are long-running and
    the web UI edits this file underneath them; a half-written config read
    mid-save would fail closed on every permission check until the next
    save, which looks exactly like "the bot randomly stopped answering"."""
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)
    os.replace(str(tmp), str(CONFIG_FILE))


def load_config():
    """Full config, defaults filled in for anything missing. Never raises —
    a corrupt file degrades to defaults, which are deny-everything, so a
    JSON typo disables the bots rather than opening them up."""
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    cfg = {}
    for platform in PLATFORMS:
        block = _platform_defaults(platform)
        incoming = data.get(platform)
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                block[key] = value
        # Normalize the four sets to lists of clean strings regardless of
        # what the user actually typed — a bare string instead of a list is
        # the single most common hand-edit mistake, and silently treating
        # "me" as ["m","e"] would be a baffling way to fail.
        for key in PERM_SETS:
            block[key] = _normalize_entries(block.get(key))
        block["owner"] = _normalize_entry(block.get("owner"))
        _coerce_text_fields(block)
        cfg[platform] = block
    return cfg


# Fields every consumer treats as text — `(cfg.get(k) or "").strip()` is the
# idiom used in roughly twenty places across channels/, spotify_*, playnite_*
# and doctor.py. Listed explicitly rather than coercing everything, because
# the booleans (enabled, allow_tools) and the real integers (webhook_port,
# cooldown_seconds, max_reply_chars) are genuinely non-text and must keep
# their types.
_TEXT_FIELDS = (
    "bot_token", "bot_user_id", "bot_handle",
    "access_token", "ig_user_id", "app_secret", "verify_token",
    "graph_version", "webhook_host", "webhook_path",
)


def _coerce_text_fields(block):
    """Force the id/token fields to strings.

    THE BUG THIS FIXES
    ------------------
        AttributeError: 'int' object has no attribute 'strip'
          ... if not (cfg.get(key) or "").strip()]     # instagram_gateway.run()

    An Instagram user id is a 17-digit number and a Discord snowflake is an
    18-digit number. This file is documented as hand-editable, so writing

        "ig_user_id": 17841400000000000

    is the obvious thing to type — JSON has a number type, the value is a
    number, no quotes needed. It parses fine, it saves fine, and then it
    crashes the gateway on startup with a traceback that names neither the
    file nor the field.

    Fixing it here rather than at the ~20 call sites is deliberate: those
    sites are spread across five modules, a fix at each one is a fix that
    the next `(cfg.get(x) or "").strip()` someone writes will reintroduce,
    and two sites (permissions.py, outbound.py) had already independently
    grown a defensive `str()` — which is the clearest possible signal that
    the coercion belongs at the boundary, once, instead of at every reader.

    A value of 0 or False coerces to "" rather than "0"/"False": for these
    fields those are "unset" spellings, and "0" would read as a configured
    id that happens to be zero.
    """
    for key in _TEXT_FIELDS:
        if key not in block:
            continue
        value = block[key]
        if value is None or value is False or value == 0:
            block[key] = ""
        elif not isinstance(value, str):
            block[key] = str(value).strip()


def save_config(cfg):
    try:
        _atomic_write(cfg)
        return True
    except OSError:
        return False


def platform_config(platform):
    return load_config().get(platform, _platform_defaults(platform))


def _normalize_entry(value):
    """One allowlist entry -> comparable form: str, stripped, lowercased,
    leading @ removed. Ids are digits so lowercasing is a no-op on them."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()


def _normalize_entries(value):
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        entry = _normalize_entry(item)
        if entry and entry not in out:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Mutation helpers — used by the CLI and the web UI so neither has to
# hand-roll read/modify/write against the file (and get the normalization
# subtly different from load_config()'s).
# ---------------------------------------------------------------------------


def set_value(platform, key, value):
    """Set one key on one platform. Returns (ok, error)."""
    if platform not in PLATFORMS:
        return False, f"unknown platform '{platform}'"
    cfg = load_config()
    cfg[platform][key] = value
    return (True, "") if save_config(cfg) else (False, "could not write config")


def add_to_set(platform, which, entry):
    """Add one entry to one of the four sets. Returns (ok, error)."""
    if which not in PERM_SETS:
        return False, f"unknown permission set '{which}'"
    if platform not in PLATFORMS:
        return False, f"unknown platform '{platform}'"
    entry = _normalize_entry(entry)
    if not entry:
        return False, "empty entry"
    cfg = load_config()
    current = cfg[platform].get(which) or []
    if entry in current:
        return True, ""  # idempotent — re-adding is not an error
    current.append(entry)
    cfg[platform][which] = current
    return (True, "") if save_config(cfg) else (False, "could not write config")


def remove_from_set(platform, which, entry):
    if which not in PERM_SETS:
        return False, f"unknown permission set '{which}'"
    if platform not in PLATFORMS:
        return False, f"unknown platform '{platform}'"
    entry = _normalize_entry(entry)
    cfg = load_config()
    current = [e for e in (cfg[platform].get(which) or []) if e != entry]
    cfg[platform][which] = current
    return (True, "") if save_config(cfg) else (False, "could not write config")


def redacted(cfg=None):
    """Config safe to hand to the web UI / print in a terminal: every
    secret replaced by a presence flag. The UI needs to show "a token is
    set" without ever receiving the token itself — same reasoning as
    server.js's /api/ai/providers returning {name, model, type} and never
    the API keys."""
    cfg = cfg or load_config()
    secret_keys = ("bot_token", "access_token", "app_secret", "verify_token")
    out = {}
    for platform in PLATFORMS:
        block = dict(cfg.get(platform) or {})
        for key in secret_keys:
            if key in block:
                block[key] = "set" if str(block[key]).strip() else ""
        out[platform] = block
    return out
