"""Chat-platform channels — Discord and Instagram.

This package is the "Jarvis reachable from a chat app" half of the system.
It is deliberately split from `notifier.py`, which is the "Jarvis reaches
out to you" half: notifier owns one-way delivery to wherever you happen to
be, while this package owns a two-way conversation with a platform that has
its own users, its own identity model, and its own idea of who is allowed
to say what.

WHY A SHARED CORE INSTEAD OF TWO BOTS
-------------------------------------
Discord and Instagram look nothing alike at the wire level — one is a
persistent WebSocket gateway you hold open, the other is an HTTPS webhook
Meta POSTs to. But everything *above* the wire is identical: decide whether
this message is even for us, decide whether this sender may be answered,
decide whether this sender may cause tools to run, log it, ask Jarvis, send
the reply back.

So the split is:

  * `config.py`      — one JSON file, both platforms, all four allowlists.
  * `permissions.py` — the gate. Pure functions over (config, message).
                       No network, no platform SDK, no IO. This is the part
                       that must be right, so it's the part that's testable
                       without a bot token.
  * `transcript.py`  — durable per-platform conversation logs.
  * `base.py`        — the platform-agnostic pipeline every gateway runs.
  * `discord_gateway.py` / `instagram_gateway.py` — the wire adapters, and
                       the ONLY files that import a platform SDK.
  * `outbound.py`    — sending a DM to the owner, used by the `notify_owner`
                       tool and by notifier.py's new channels.

A gateway is a long-running process, same shape as `sched_daemon.py`: the
CLI starts it in the foreground and backgrounding is the caller's problem.
That matters because `jarvis` is otherwise a brand-new OS process per
invocation (see history.py) — these two are the only long-lived things in
the system besides the scheduler daemon, so they follow its conventions
rather than inventing new ones.
"""

# Platform identifiers. Used as dict keys in the config, as the folder name
# under ~/.jarvis/channels/, and as notifier channel names — keeping them
# identical across all three means there is never a mapping table to keep
# in sync.
DISCORD = "discord"
INSTAGRAM = "instagram"
PLATFORMS = (DISCORD, INSTAGRAM)

# The four independent permission sets, in the order the gate applies them.
# Named here rather than as bare strings at each call site so a typo is an
# ImportError instead of a silently-always-denied check.
PERM_DM = "dm_allowlist"
PERM_REPLY = "reply_allowlist"
PERM_TOOLS = "tool_allowlist"
PERM_SETS = (PERM_DM, PERM_REPLY, PERM_TOOLS)

__all__ = [
    "DISCORD", "INSTAGRAM", "PLATFORMS",
    "PERM_DM", "PERM_REPLY", "PERM_TOOLS", "PERM_SETS",
]
