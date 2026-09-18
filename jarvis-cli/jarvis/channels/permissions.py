"""The gate: may this message be ingested, answered, and may it run tools?

Pure functions over plain data. No network, no platform SDK, no filesystem,
no clock except the one you pass in. That is deliberate — this is the file
where a mistake means a stranger runs desktop automation on someone's PC,
so it has to be fully testable without a bot token, a Meta app, or an
internet connection. Every gateway in this package delegates here rather
than re-deriving "is this allowed" against its own SDK's objects.

THE PIPELINE
------------
    decide(cfg, msg) -> Decision

runs these stages in order and STOPS at the first denial:

    enabled      platform turned on at all
    self         never answer our own messages (infinite loop guard)
    reachable    was this even addressed to us? — a DM, or an @mention
    dm_allowed   DMs only: sender is in dm_allowlist
    where        group only: guild/channel filters
    reply        sender is in reply_allowlist
    cooldown     sender isn't flooding

and then, independently of the above, computes:

    may_use_tools = allow_tools AND sender in tool_allowlist

Note the "independently": tool permission is NOT a stage that can deny the
message. Someone in reply_allowlist but not tool_allowlist gets a normal
conversational answer with tools switched off, which is the single most
useful configuration this module supports and the reason the four sets are
separate in the first place.

WHY "reachable" COMES BEFORE "reply"
------------------------------------
So that a message we were never addressed in is dropped without ever
consulting an allowlist, and — more importantly — without being logged. A
bot sitting in a busy server should not be quietly writing a transcript of
every message in every channel to the owner's disk. If it wasn't for us, we
never saw it.
"""

import time

from . import PERM_DM, PERM_REPLY, PERM_TOOLS, PERM_SETS
from . import config
from .config import WILDCARD

# Context kinds. A gateway translates its own notion of "where" into one of
# these before calling decide(), so the gate itself never imports an SDK.
CTX_DM = "dm"
CTX_GROUP = "group"


def scope_keys(msg):
    """Scope identifiers for one message, MOST SPECIFIC FIRST.

    A Discord message in a server has all three (thread/channel/guild); an
    Instagram group thread has only a thread. Order matters — resolve_scope
    walks this list and the first override wins per key.
    """
    keys = []
    if msg.thread_id and msg.thread_id != msg.channel_id:
        keys.append(f"thread:{msg.thread_id}")
    if msg.channel_id:
        keys.append(f"channel:{msg.channel_id}")
    if msg.guild_id:
        keys.append(f"guild:{msg.guild_id}")
    return keys


def resolve_scope(cfg, msg):
    """Platform config with any matching per-scope overrides layered on.

    Only keys the scope actually declares are replaced — an override that
    sets `allow_tools: false` for one server leaves every allowlist, the
    cooldown and the owner exactly as configured at platform level. That
    partial-override behaviour is deliberate: a scope block that had to
    restate the whole config to change one field would drift out of sync
    with the defaults the moment either changed.

    Returns the config unchanged when there are no scopes, so the common
    case costs one dict lookup.

    THE BUG THIS FIXES
    ------------------
    load_config() normalizes every top-level dm/reply/tool_allowlist and
    owner into a clean list/string on every read (see config.py's
    _normalize_entries — "a bare string instead of a list is the single
    most common hand-edit mistake"). A scope override sits one level
    deeper in the same file and used to skip that normalization entirely
    — `merged[field] = value` copied whatever JSON was there verbatim. The
    same obvious-thing-to-type mistake at the top level

        "scopes": {"guild:123456789012345678": {"reply_allowlist": 987654321012345678}}

    (a bare id instead of `["987654321012345678"]`) used to reach
    permissions.matches() as a raw int, which crashed with `'int' object
    is not iterable` and denied the message with stage="error" — a
    confusing failure mode indistinguishable from "the gate itself is
    broken" (see base.py's handle_message). Normalizing here, the exact
    same way load_config() already does for the platform-level value,
    means a scope override is exactly as hand-edit-tolerant as everything
    else in this file.
    """
    scopes = (cfg or {}).get("scopes")
    if not isinstance(scopes, dict) or not scopes:
        return cfg
    merged = None
    # Walk least-specific first and let more specific overwrite, so the
    # final result has thread > channel > guild precedence.
    for key in reversed(scope_keys(msg)):
        override = scopes.get(key)
        if not isinstance(override, dict):
            continue
        if merged is None:
            merged = dict(cfg)
        for field, value in override.items():
            if field in PERM_SETS:
                value = config.normalize_entries(value)
            elif field == "owner":
                value = config.normalize_entry(value)
            merged[field] = value
    return merged if merged is not None else cfg


class IncomingMessage:
    """One inbound message, normalized across platforms.

    `user_id` should be the platform's stable id (Discord snowflake,
    Instagram IGSID) and `user_handle` the mutable display handle. Both are
    matched against the allowlists — see config.py's IDENTITY MATCHING note
    for why both, and why ids are the ones to actually use.
    """

    __slots__ = ("platform", "context", "user_id", "user_handle", "text",
                 "mentioned", "guild_id", "channel_id", "message_id",
                 "thread_id", "timestamp", "raw")

    def __init__(self, platform, context, user_id="", user_handle="", text="",
                 mentioned=False, guild_id="", channel_id="", message_id="",
                 thread_id="", timestamp=None, raw=None):
        self.platform = platform
        self.context = context
        self.user_id = str(user_id or "")
        self.user_handle = str(user_handle or "")
        self.text = text or ""
        self.mentioned = bool(mentioned)
        self.guild_id = str(guild_id or "")
        self.channel_id = str(channel_id or "")
        self.message_id = str(message_id or "")
        # Platform-native conversation key: Discord channel, Instagram thread.
        self.thread_id = str(thread_id or channel_id or "")
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.raw = raw

    def identities(self):
        """Every form of this sender an allowlist entry could name, in the
        same normalized shape config.py stores entries in."""
        out = []
        for value in (self.user_id, self.user_handle):
            text = str(value or "").strip().lstrip("@").lower()
            if text and text not in out:
                out.append(text)
        return out

    def __repr__(self):  # pragma: no cover — debugging convenience only
        who = self.user_handle or self.user_id or "?"
        return f"<IncomingMessage {self.platform}/{self.context} from {who}>"


class Decision:
    """The gate's verdict.

    `stage` names the check that produced this outcome — it is what the
    denial log line prints, and it is the difference between "the bot is
    broken" and "the bot is working and you are not on the reply list".
    """

    __slots__ = ("allowed", "may_use_tools", "stage", "reason")

    def __init__(self, allowed, stage, reason, may_use_tools=False):
        self.allowed = bool(allowed)
        self.may_use_tools = bool(may_use_tools)
        self.stage = stage
        self.reason = reason

    def __bool__(self):
        return self.allowed

    def __repr__(self):  # pragma: no cover
        verdict = "allow" if self.allowed else "deny"
        tools = "+tools" if self.may_use_tools else "-tools"
        return f"<Decision {verdict} {tools} at {self.stage}: {self.reason}>"


def matches(entries, identities):
    """Is any of `identities` covered by `entries`?

    Empty entries -> False (deny). The WILDCARD entry -> True. See
    config.py's EMPTY MEANS DENY note for why that asymmetry is the whole
    point rather than an oversight.

    `entries` SHOULD always be a list by the time it gets here — load_
    config() normalizes every top-level allowlist on every read, and
    resolve_scope() normalizes a scope override's before merging it in
    (see that function's own note). This function still tolerates a bare
    string/int/None (treated as a single entry, or none) rather than
    crashing on it, the same "never raises, fails closed" contract as the
    rest of this module — a config file is hand-editable, and the obvious
    thing to type for one person is `"reply_allowlist": 123456789012345`
    (no brackets), not a JSON list.
    """
    if not entries:
        return False
    if isinstance(entries, (str, int)):
        entries = [entries]
    if not isinstance(entries, (list, tuple, set)):
        return False
    entries = [str(e).strip().lstrip("@").lower() for e in entries if str(e).strip()]
    if WILDCARD in entries:
        return True
    return any(ident in entries for ident in identities)


def _is_self(cfg, msg):
    """Our own message coming back at us. Without this a bot that mentions
    itself in a reply answers its own reply, forever."""
    bot_id = str(cfg.get("bot_user_id") or cfg.get("ig_user_id") or "").strip().lower()
    if not bot_id:
        return False
    return msg.user_id.strip().lower() == bot_id


def decide(cfg, msg, last_seen_at=None, now=None):
    """Run the whole pipeline. `cfg` is one platform's block from
    config.load_config(); `last_seen_at` is when this sender was last
    *accepted* (a float epoch, or None) and drives the cooldown stage.

    Returns a Decision. Never raises — a malformed config denies.
    """
    cfg = cfg or {}
    # Per-scope overrides are applied before anything is checked, so every
    # stage below sees one already-resolved config and no stage has to know
    # scopes exist. `enabled` is read from the resolved config too, which
    # means a scope can switch the bot off for one server without touching
    # the platform.
    cfg = resolve_scope(cfg, msg)
    now = time.time() if now is None else now
    identities = msg.identities()

    if not cfg.get("enabled"):
        return Decision(False, "enabled", f"{msg.platform} channel is disabled")

    if _is_self(cfg, msg):
        return Decision(False, "self", "message is from the bot itself")

    # --- reachable: was this addressed to us at all? ----------------------
    if msg.context == CTX_DM:
        if not cfg.get("respond_in_dms", True):
            return Decision(False, "reachable", "DMs are disabled for this channel")
        # A DM is inherently addressed to us, so the mention requirement
        # does not apply here — see the module docstring.
        if not matches(cfg.get(PERM_DM), identities):
            return Decision(False, "dm_allowed",
                            "sender is not in dm_allowlist")
    else:
        if cfg.get("require_mention", True) and not msg.mentioned:
            return Decision(False, "reachable", "not mentioned")
        # Location filters. Unlike the allowlists these are empty=unrestricted:
        # they narrow *where* an already-authorized person may talk to the
        # bot, and the who-gate below still fails closed on its own.
        guilds = cfg.get("allowed_guilds") or []
        if guilds and msg.guild_id and str(msg.guild_id) not in [str(g) for g in guilds]:
            return Decision(False, "where", f"guild {msg.guild_id} not allowed")
        channels = cfg.get("allowed_channels") or []
        if channels and msg.channel_id and str(msg.channel_id) not in [str(c) for c in channels]:
            return Decision(False, "where", f"channel {msg.channel_id} not allowed")

    # --- reply: may this person get an answer? ---------------------------
    if not matches(cfg.get(PERM_REPLY), identities):
        return Decision(False, "reply", "sender is not in reply_allowlist")

    # --- cooldown --------------------------------------------------------
    cooldown = cfg.get("cooldown_seconds") or 0
    if cooldown and last_seen_at is not None:
        elapsed = now - last_seen_at
        if elapsed < cooldown:
            return Decision(False, "cooldown",
                            f"{elapsed:.1f}s since last message, cooldown is {cooldown}s")

    # --- tools: independent of everything above --------------------------
    may_use_tools = bool(cfg.get("allow_tools")) and matches(cfg.get(PERM_TOOLS), identities)

    return Decision(True, "allowed",
                    "tools enabled" if may_use_tools else "tools disabled for this sender",
                    may_use_tools=may_use_tools)


def is_owner(cfg, identities):
    """Is this sender the configured owner? Used for owner-only commands
    inside a chat (e.g. `@jarvis status`), never as a permission shortcut —
    the owner still has to appear in a set to be granted by it."""
    owner = str((cfg or {}).get("owner") or "").strip().lstrip("@").lower()
    if not owner:
        return False
    if isinstance(identities, IncomingMessage):
        identities = identities.identities()
    return owner in identities


def describe(cfg):
    """Human-readable summary of one platform's permission posture, for
    `jarvis channels-status` and the Guides panel. Written to make a
    fail-closed config obvious at a glance, because "I pasted my token and
    nothing happens" is the predictable first-run experience of a system
    that denies by default."""
    cfg = cfg or {}

    def render(which):
        entries = cfg.get(which) or []
        if not entries:
            return "nobody (empty)"
        if WILDCARD in entries:
            return "EVERYONE (*)"
        return f"{len(entries)}: " + ", ".join(entries[:6]) + ("…" if len(entries) > 6 else "")

    lines = [
        f"enabled:         {bool(cfg.get('enabled'))}",
        f"owner:           {cfg.get('owner') or '(unset)'}",
        f"dm_allowlist:    {render(PERM_DM)}",
        f"reply_allowlist: {render(PERM_REPLY)}",
        f"tool_allowlist:  {render(PERM_TOOLS)}",
        f"allow_tools:     {bool(cfg.get('allow_tools'))}",
        f"require_mention: {bool(cfg.get('require_mention', True))}",
    ]
    if not cfg.get("reply_allowlist"):
        lines.append(
            "  ! reply_allowlist is empty, so nobody will get an answer. "
            "Add an id, or \"*\" for everyone.")
    if cfg.get("allow_tools") and WILDCARD in (cfg.get(PERM_TOOLS) or []):
        lines.append(
            "  ! tool_allowlist is \"*\" with allow_tools on — anyone who can "
            "reach this bot can run tools on this machine.")
    return "\n".join(lines)
