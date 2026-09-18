"""CLI handlers for the channels subsystem and log search.

Kept in its own module rather than inlined into cli.py, which is already
2000 lines: cli.py gains one dispatch branch that delegates here, and every
command's actual body lives next to the code it drives.

Same JSON-on-stdout convention the conv-search / mcp-* family already uses
(see cli.py's `_handle_json_command`), because web/server.js shells out to
these rather than reimplementing them in Node — one source of truth for
what a permission check or a log search actually does.
"""

import json
import sys

from .channels import PLATFORMS, PERM_SETS
from .channels import config as channel_config
from .channels import directory, outbound, people, permissions, transcript

COMMANDS = (
    "channels-config", "channels-status", "channels-set", "channels-allow",
    "channels-deny", "channels-test", "channels-whoami", "channels-log",
    "channels-directory", "channels-people", "channels-follow",
    "channels-block",
    "discord-daemon", "instagram-serve", "logs-search",
)

USAGE = """channel commands:
  channels-config                       print the config file path
  channels-status                       show permissions + readiness (JSON)
  channels-set <platform> <key> <value> set one config key
  channels-allow <platform> <set> <id>  add to dm|reply|tool allowlist
  channels-deny  <platform> <set> <id>  remove from an allowlist
  channels-test  [platform] [message]   send a test DM to the owner
  channels-whoami                       how to find your stable user id
  channels-log   <platform> <thread>    print a thread's transcript
  channels-directory [platform]         list known @handle -> id mappings
  channels-people [platform] [--pending] who has messaged Jarvis, and what it knows about them
  channels-follow <platform> <id|@handle> approve someone: adds them to reply_allowlist
  channels-block  <platform> <id|@handle> refuse someone, and stop being asked about them
  discord-daemon                        run the Discord bot (foreground)
  instagram-serve                       run the Instagram webhook (foreground)
  logs-search <query> [--mode m] [--origin o] [--source s] [--direction d]

  <set> is one of: dm, reply, tool
  <platform> is one of: discord, instagram"""

# Short aliases, because "channels-allow discord dm_allowlist 123" is a lot
# of typing for something you do while setting up.
_SET_ALIASES = {
    "dm": "dm_allowlist", "reply": "reply_allowlist", "tool": "tool_allowlist",
    "tools": "tool_allowlist",
}


def _coerce(value):
    """Turn a command-line string into the JSON type the config expects.

    Without this, `channels-set discord enabled true` writes the *string*
    "true", which is truthy in Python but obviously wrong in the file and
    confusing to anyone who opens it. Lists are accepted as JSON so an
    allowlist can be set wholesale.
    """
    text = (value or "").strip()
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return ""
    if text.lstrip("-").isdigit():
        return int(text)
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _fail(message, code=1):
    print(json.dumps({"ok": False, "error": message}, indent=2))
    sys.exit(code)


def handle(argv):
    """Dispatch one channels/logs command. argv[0] is the command name."""
    cmd = argv[0]
    rest = argv[1:]

    if cmd == "channels-config":
        path = channel_config.ensure_config()
        print(json.dumps({"ok": True, "path": str(path)}, indent=2))
        return

    if cmd == "channels-status":
        cfg = channel_config.load_config()
        print(json.dumps({
            "ok": True,
            "config": channel_config.redacted(cfg),
            "delivery": outbound.status(),
            "summary": {p: permissions.describe(cfg.get(p)) for p in PLATFORMS},
        }, indent=2))
        return

    if cmd == "channels-set":
        if len(rest) < 3:
            _fail("usage: channels-set <platform> <key> <value>")
        platform, key, value = rest[0], rest[1], " ".join(rest[2:])
        ok, err = channel_config.set_value(platform, key, _coerce(value))
        print(json.dumps({"ok": ok, "error": err,
                          "platform": platform, "key": key}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd in ("channels-allow", "channels-deny"):
        if len(rest) < 3:
            _fail(f"usage: {cmd} <platform> <dm|reply|tool> <id-or-@handle>")
        platform, which, entry = rest[0], rest[1], rest[2]
        which = _SET_ALIASES.get(which.lower(), which)
        if which not in PERM_SETS:
            _fail(f"unknown set '{rest[1]}' — use dm, reply or tool")

        # @handle resolution. Only on channels-allow: a channels-deny for a
        # handle nobody has heard of yet is ambiguous (remove what, from
        # what?) whereas allow has one unambiguous failure mode ("not known
        # yet — have them message the bot first"). For deny, resolve too
        # when possible so `channels-deny discord reply @name` still works
        # once the directory knows them, but fall through to removing the
        # literal string otherwise — a deny is safe to attempt even if it
        # turns out to be a no-op, unlike an allow which would silently
        # grant the WRONG id if resolution guessed instead of looking up.
        resolved, err = directory.resolve(platform, entry)
        if cmd == "channels-allow" and resolved is None:
            print(json.dumps({"ok": False, "error": err, "platform": platform,
                              "set": which, "entry": entry}, indent=2))
            sys.exit(1)
        actual_entry = resolved if resolved is not None else entry

        action = (channel_config.add_to_set if cmd == "channels-allow"
                  else channel_config.remove_from_set)
        ok, err = action(platform, which, actual_entry)
        result = {"ok": ok, "error": err, "platform": platform,
                  "set": which, "entry": actual_entry}
        if actual_entry != entry:
            result["resolved_from"] = entry
        print(json.dumps(result, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-test":
        platform = rest[0] if rest and rest[0] in PLATFORMS else None
        offset = 1 if platform else 0
        message = " ".join(rest[offset:]) or "Test message from Jarvis."
        result = outbound.notify_owner(
            message, platforms=[platform] if platform else None,
            first_success_only=False)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["ok"] else 1)

    if cmd == "channels-whoami":
        print(json.dumps({
            "ok": True,
            "discord": (
                "Settings > Advanced > Developer Mode ON, then right-click "
                "your name > Copy User ID. That 17-19 digit number is your "
                "stable id — use it, not your username."),
            "instagram": (
                "Your IGSID is assigned per-app and only exists once you've "
                "messaged the bot. DM the bot account once, then run "
                "`jarvis channels-log instagram <your-igsid>` — or check the "
                "webhook trace, which prints the sender id of every message "
                "it receives."),
        }, indent=2))
        return

    if cmd == "channels-directory":
        platform = rest[0] if rest and rest[0] in PLATFORMS else None
        entries = directory.all_entries(platform)
        print(json.dumps({"ok": True, "count": len(entries),
                          "entries": entries}, indent=2))
        return

    if cmd == "channels-people":
        platform = rest[0] if rest and rest[0] in PLATFORMS else None
        follow = people.FOLLOW_PENDING if "--pending" in rest else None
        entries = people.all_people(platform, follow=follow)
        print(json.dumps({
            "ok": True, "count": len(entries),
            "people": entries,
            "summary": [people.describe(e) for e in entries],
        }, indent=2))
        return

    if cmd in ("channels-follow", "channels-block"):
        if len(rest) < 2:
            _fail(f"usage: {cmd} <platform> <id-or-@handle>")
        platform, entry = rest[0], rest[1]
        if platform not in PLATFORMS:
            _fail(f"unknown platform '{platform}' — use "
                  + " or ".join(PLATFORMS))
        user_id = people.resolve_user_id(platform, entry)
        if not user_id:
            _fail(f"don't know anyone called '{entry}' on {platform} yet. "
                  f"Run `jarvis channels-people {platform}` to see who has "
                  f"actually messaged in.")

        if cmd == "channels-block":
            # No allowlist write. Blocking is the ABSENCE of permission,
            # and the allowlists already fail closed — so "blocked" only
            # has to mean "stop asking me about this person". Actively
            # removing them would silently undo a deliberate
            # channels-allow, which is a surprising thing for a command
            # whose job is to answer a notification prompt.
            record = people.set_follow(platform, user_id,
                                       people.FOLLOW_BLOCKED)
            print(json.dumps({"ok": True, "platform": platform,
                              "user_id": user_id, "follow": record["follow"],
                              "note": ("recorded — they stay off every "
                                       "allowlist, and you won't be asked "
                                       "about them again")}, indent=2))
            return

        # Approving is a real permission change, so it goes through the
        # same add_to_set() that channels-allow uses rather than a second
        # path into the config — permissions.py must keep exactly one
        # source of truth for who may be answered (see people.py).
        ok, err = channel_config.add_to_set(platform, "reply_allowlist",
                                            str(user_id))
        record = people.set_follow(platform, user_id, people.FOLLOW_APPROVED)
        print(json.dumps({
            "ok": ok, "error": err, "platform": platform,
            "user_id": user_id, "follow": record["follow"],
            "added_to": "reply_allowlist",
            "note": ("they can be answered now. Tools stay off until you "
                     "also run: jarvis channels-allow "
                     f"{platform} tool {user_id}"),
        }, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-log":
        if len(rest) < 2:
            _fail("usage: channels-log <platform> <thread_id>")
        entries = transcript.read_thread(rest[0], rest[1])
        print(json.dumps({"ok": True, "platform": rest[0],
                          "thread_id": rest[1], "entries": entries}, indent=2))
        return

    if cmd == "discord-daemon":
        from .channels import discord_gateway
        sys.exit(discord_gateway.run())

    if cmd == "instagram-serve":
        from .channels import instagram_gateway
        sys.exit(instagram_gateway.run())

    if cmd == "logs-search":
        from . import logs as logs_mod
        if not rest:
            _fail("usage: logs-search <query> [--mode words|phrase|regex] "
                  "[--origin discord] [--source scheduler] [--direction error]")
        query_parts, opts = [], {}
        i = 0
        while i < len(rest):
            token = rest[i]
            if token.startswith("--") and i + 1 < len(rest):
                opts.setdefault(token[2:], []).append(rest[i + 1])
                i += 2
                continue
            query_parts.append(token)
            i += 1
        result = logs_mod.search(
            " ".join(query_parts),
            mode=(opts.get("mode") or ["words"])[0],
            limit=int((opts.get("limit") or [50])[0]),
            conv_id=(opts.get("conv") or [None])[0],
            directions=opts.get("direction"),
            origins=opts.get("origin"),
            sources=opts.get("source"),
        )
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    _fail(f"unknown command '{cmd}'")
