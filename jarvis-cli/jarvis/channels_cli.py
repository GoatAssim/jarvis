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
import re
import sys
import time

from .channels import PLATFORMS, PERM_SETS
from .channels import config as channel_config
from .channels import directory, outbound, people, permissions, transcript
from .channels import allowed_guilds, denied, master_tools, panel_dm
from .channels import preset_admin, presets, servers, user_admin

COMMANDS = (
    "channels-config", "channels-status", "channels-set", "channels-allow",
    "channels-deny", "channels-test", "channels-whoami", "channels-log",
    "channels-directory", "channels-people", "channels-follow",
    "channels-block", "channels-users", "channels-user", "channels-user-tools",
    "channels-add-person", "channels-rename", "channels-remove-person",
    "channels-note", "channels-forget",
    "channels-link", "channels-unlink",
    "channels-conversation", "channels-usage", "channels-user-test",
    "channels-history", "channels-handle",
    "channels-tools-for", "channels-instruction",
    "channels-presets", "channels-preset", "channels-bulk",
    "channels-servers", "channels-server-set", "channels-guilds",
    "channels-master-tools", "channels-send", "channels-denied",
    "discord-daemon", "instagram-serve", "logs-search",
)

USAGE = """channel commands:
  channels-config                       print the config file path
  channels-status                       show permissions + readiness (JSON)
  channels-set <platform> <key> <value> set one config key
  channels-allow <platform> <set> <id>  add to dm|reply|tool|image allowlist
  channels-deny  <platform> <set> <id>  remove from an allowlist
  channels-test  [platform] [message]   send a test DM to the owner
  channels-whoami                       how to find your stable user id
  channels-log   <platform> <thread>    print a thread's transcript
  channels-directory [platform]         list known @handle -> id mappings
  channels-people [platform] [--pending] who has messaged Jarvis, and what it knows about them
  channels-follow <platform> <id|@handle> approve someone: adds them to reply_allowlist
  channels-block  <platform> <id|@handle> refuse someone, and stop being asked about them
  channels-users [platform]             every registered person + every switch (JSON)
  channels-user  <platform> <id> <switch> <on|off>
                                        flip one switch for one person; <switch> is one of:
                                        dm, reply, tool, image, owner, send_dm, blocked
                                        (image = may send Jarvis pictures once that exists; the owner always may)
  channels-user-tools <platform> <id> inherit | custom [tool ...]
                                        which tools that person may run (custom = ONLY those)
  channels-tools-for <platform> <id|@handle> <30m|24h|7d>
                                        tool use for that long, then it ends by itself (1m to 30d).
                                        Running it again replaces the countdown; `channels-user ... tool
                                        on|off` ends it (on = for good). Refused for the owner
  channels-instruction <platform> <id|@handle> show | set <text ...> | clear
                                        your own line on HOW to talk to that person (style only,
                                        up to 240 chars; it grants nothing). Refused for the owner
  channels-add-person <platform> <id|@handle> [name ...]
                                        add someone who hasn't messaged yet (grants nothing)
  channels-rename <platform> <id|@handle> [name ...]
                                        set what they're called; no name clears it
  channels-remove-person <platform> <id|@handle>
                                        delete a hand-added person who never messaged
  channels-note <platform> <id|@handle> list | edit <note-id> <text ...> | delete <note-id>
                                        what Jarvis remembers about them; ids come from `list`
  channels-forget <platform> <id|@handle> [--history] [--yes]
                                        wipe someone: off every list, limits, record
                                        (--history also their DM logs). Without --yes it
                                        only PREVIEWS. Refuses the owner and anyone "*" covers
  channels-link <platform> <id|@handle> <other-platform> <id|@handle|name ...>
                                        same human on both platforms (identity only)
  channels-unlink <platform> <id|@handle>
                                        undo a link
  channels-conversation <platform> <id> [limit]
                                        what that person sent and how Jarvis answered (JSON, read-only)
  channels-usage <platform> <id> [days] their messages, tokens and tool calls (JSON, read-only)
  channels-history <platform> <id> [limit]
                                        every recorded change to their access: who/what/when,
                                        and whether it came from the panel or the terminal
                                        (JSON, read-only). Starts when the log does
  channels-handle <platform> <id|@handle> <new-handle>
                                        correct the handle of someone you added by hand who
                                        hasn't written yet; their access moves with it.
                                        Refused for the owner and for anyone who has written
  channels-user-test <platform> <id> dm | group [mentioned|unmentioned]
                                        dry run: what the gate would do with a message from them.
                                        Calls no model, sends nothing, saves nothing
  channels-presets                      the quick setups (JSON): what each one switches on
  channels-preset <platform> <id|@handle> <setup> [preview]
                                        apply a quick setup to one person; `preview` only shows
                                        what it would change. Setups: none, chat_only,
                                        chat_notify, trusted
  channels-bulk flag <switch> <on|off> <platform:id> ...
                                        one switch for several people (each refusal is reported
                                        per person). <switch>: dm, reply, tool (off only), send_dm,
                                        blocked
  channels-bulk preset|preview <setup> <platform:id> ...
                                        a quick setup for several people / what it would change
  channels-servers [platform]           Discord servers + channels Jarvis knows, with their switches (JSON)
  channels-server-set <platform> guild|channel <id> <switch> <on|off|inherit>
  channels-guilds <platform> [add|remove <id>] [--yes]
                                        the allowed_guilds filter ("only these servers"). No action = show it.
                                        add to an EMPTY list, or an id Jarvis hasn't seen, only PREVIEWS unless
                                        --yes. Removing the LAST entry is always refused (empty = every server)
                                        per-server / per-channel switch. <switch>: enabled (answer
                                        here), tools (allow tools here), mention (require @mention).
                                        These can only take access away: enabled/tools "on" means
                                        inherit; mention can be turned on or inherited, never off
  channels-master-tools <platform> [on|off] [--yes]
                                        the platform-wide tool switch (allow_tools). No argument shows
                                        who it would let run tools. `on` only PREVIEWS unless --yes;
                                        `off` applies at once. `on` is refused while the tool list has "*"
  channels-send <platform> <id|@handle> [--yes] <message ...>
                                        DM one known person on your behalf, through the same path, limits
                                        (5 per person / 20 per hour) and log as the send_dm tool. Without
                                        --yes it only PREVIEWS. Refused for the owner, someone blocked or
                                        with DMs switched off, and anyone known only by handle
  channels-denied [platform] [days]     senders the gate turned away recently, with counts and the gate's
                                        reason -- never what they wrote (JSON, read-only)
  discord-daemon                        run the Discord bot (foreground)
  instagram-serve                       run the Instagram webhook (foreground)
  logs-search <query> [--mode m] [--origin o] [--source s] [--direction d]

  <set> is one of: dm, reply, tool, image
  <platform> is one of: discord, instagram"""

# Short aliases, because "channels-allow discord dm_allowlist 123" is a lot
# of typing for something you do while setting up.
_SET_ALIASES = {
    "dm": "dm_allowlist", "reply": "reply_allowlist", "tool": "tool_allowlist",
    "tools": "tool_allowlist",
    "image": "image_allowlist", "images": "image_allowlist",
}


def _parse_duration(text):
    """'30m' / '24h' / '7d' -> whole minutes, or None. A bare number is not
    accepted: whether it means minutes or hours is exactly the kind of
    ambiguity that turns a one-day grant into a one-minute (or one-month) one."""
    m = re.match(r"^(\d{1,5})([mhd])$", str(text or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)]


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


def _person_id(platform, entry):
    """An id or @handle -> the stored id, looking only at people on file
    (never directory.py: these commands change who someone is, so a guess
    would be worse than an error)."""
    uid = str(entry or "").strip().lstrip("@")
    if uid.isdigit() and people.get(platform, uid):
        return uid
    for rec in people.all_people(platform if platform in PLATFORMS else None):
        if uid.lower() in ((rec.get("handle") or "").lower(),
                           str(rec.get("user_id") or "").lower()):
            return rec.get("user_id")
    return uid


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
            _fail(f"usage: {cmd} <platform> <dm|reply|tool|image> <id-or-@handle>")
        platform, which, entry = rest[0], rest[1], rest[2]
        which = _SET_ALIASES.get(which.lower(), which)
        if which not in PERM_SETS:
            _fail(f"unknown set '{rest[1]}' — use dm, reply, tool or image")

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
        if ok and which == "tool_allowlist":
            # A time limit set from the panel (L.36-P6) is a ceiling on the
            # tool list. Allowing or denying tools by hand is a decision
            # about the list itself, so it ends any countdown (allow = for
            # good, deny = revoked), same as the Tool use switch.
            _known = people.get(platform, str(actual_entry).lstrip("@"))
            if _known:
                _note = user_admin.end_countdown(platform, _known["user_id"])
                if _note:
                    result["note"] = _note
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

    if cmd == "channels-users":
        platform = rest[0] if rest and rest[0] in PLATFORMS else None
        print(json.dumps(user_admin.list_view(platform), indent=2))
        return

    if cmd == "channels-user":
        if len(rest) < 4:
            _fail("usage: channels-user <platform> <id> <switch> <on|off>  "
                  "(switch: " + ", ".join(user_admin.FLAGS) + ")")
        platform, entry, flag, raw = rest[0], rest[1], rest[2].lower(), rest[3]
        value = _coerce(raw)
        if not isinstance(value, bool):
            _fail(f"'{raw}' isn't on or off")
        # Accept an id or a @handle, resolved only against people who have
        # actually messaged in (people.py) — never a guess, never directory.py,
        # because this changes what someone may do.
        uid = entry.strip().lstrip("@")
        if not (uid.isdigit() and people.get(platform, uid)):
            found = None
            for rec in people.all_people(platform if platform in PLATFORMS else None):
                if uid.lower() in ((rec.get("handle") or "").lower(),):
                    found = rec.get("user_id")
                    break
            uid = found or uid
        ok, err, note = user_admin.set_flag(platform, uid, flag, value)
        print(json.dumps({"ok": ok, "error": err, "note": note,
                          "platform": platform, "user_id": uid,
                          "switch": flag, "value": value}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-user-tools":
        if len(rest) < 3:
            _fail("usage: channels-user-tools <platform> <id> inherit | custom [tool ...]")
        platform, uid, mode = rest[0], rest[1].strip().lstrip("@"), rest[2].lower()
        ok, err = user_admin.set_tools(platform, uid, mode, rest[3:])
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "user_id": uid, "mode": mode,
                          "tools": rest[3:] if mode == "custom" else []}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-tools-for":
        # L.36-P6. Same strict id resolution as channels-user: people on file.
        usage = "usage: channels-tools-for <platform> <id|@handle> <30m|24h|7d>"
        if len(rest) != 3:
            _fail(usage)
        platform, uid = rest[0], _person_id(rest[0], rest[1])
        minutes = _parse_duration(rest[2])
        if minutes is None:
            _fail(f"'{rest[2]}' isn't a time limit \u2014 write it like 30m, 24h or 7d")
        ok, err, note, until = user_admin.grant_tools_for(platform, uid, minutes)
        print(json.dumps({"ok": ok, "error": err, "note": note,
                          "platform": platform, "user_id": uid,
                          "minutes": minutes, "until": until,
                          "until_text": (time.strftime("%Y-%m-%d %H:%M",
                                                       time.localtime(until))
                                         if until else "")}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-instruction":
        # L.36-P12. Owner-typed only: nothing a chat guest can reach.
        usage = ("usage: channels-instruction <platform> <id|@handle> "
                 "show | set <text ...> | clear")
        if len(rest) < 3:
            _fail(usage)
        platform, uid, action = rest[0], _person_id(rest[0], rest[1]), rest[2].lower()
        if action == "show" and len(rest) == 3:
            rec = people.get(platform, uid)
            if rec is None:
                _fail(f"{uid} isn't registered on {platform}")
            print(json.dumps({"ok": True, "platform": platform, "user_id": uid,
                              "instruction": rec.get("instruction") or ""},
                             indent=2))
            return
        if action == "set" and len(rest) >= 4:
            ok, err, text = user_admin.set_instruction(
                platform, uid, " ".join(rest[3:]))
        elif action == "clear" and len(rest) == 3:
            ok, err, text = user_admin.set_instruction(platform, uid, "")
        else:
            _fail(usage)
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "user_id": uid, "instruction": text}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-presets":
        print(json.dumps({"ok": True, "presets": presets.public_view()}, indent=2))
        return

    if cmd == "channels-preset":
        # A quick setup for ONE person (L.36-P4). Same strict id resolution as
        # channels-user: people on file only.
        if len(rest) < 3:
            _fail("usage: channels-preset <platform> <id|@handle> <setup> [preview]  "
                  "(setup: " + ", ".join(presets.ids()) + ")")
        platform, uid, setup = rest[0], _person_id(rest[0], rest[1]), rest[2].lower()
        if len(rest) > 3 and rest[3].lower() == "preview":
            result = preset_admin.plan_preset(platform, uid, setup)
        else:
            result = preset_admin.apply_preset(platform, uid, setup)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-bulk":
        # Several people at once (L.36-P5). People are written <platform:id>
        # (an id or @handle after the colon), resolved one by one the same
        # strict way; an unknown one is reported as refused, not dropped.
        usage = ("usage: channels-bulk flag <switch> <on|off> <platform:id> ...  |  "
                 "channels-bulk preset|preview <setup> <platform:id> ...")
        if len(rest) < 3:
            _fail(usage)
        mode = rest[0].lower()
        if mode == "flag":
            if len(rest) < 4:
                _fail(usage)
            flag, value, entries = rest[1].lower(), _coerce(rest[2]), rest[3:]
            if not isinstance(value, bool):
                _fail(f"'{rest[2]}' isn't on or off")
        elif mode in ("preset", "preview"):
            setup, entries = rest[1].lower(), rest[2:]
        else:
            _fail(usage)
        refs = []
        for entry in entries:
            platform, _, ident = entry.partition(":")
            if not ident:
                _fail(f"'{entry}' isn't <platform:id>")
            refs.append((platform, _person_id(platform, ident)))
        if mode == "flag":
            result = preset_admin.bulk_flag(refs, flag, value)
        else:
            result = preset_admin.bulk_preset(refs, setup, preview=(mode == "preview"))
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-history":
        # Read-only. Same strict id resolution as the other per-person views.
        if len(rest) < 2:
            _fail("usage: channels-history <platform> <id> [limit]")
        platform, uid = rest[0], _person_id(rest[0], rest[1])
        number = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else 100
        result = user_admin.history_view(platform, uid, number)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-handle":
        if len(rest) < 3:
            _fail("usage: channels-handle <platform> <id|@handle> <new-handle>")
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        ok, err, note, rec = user_admin.set_handle(platform, uid, rest[2])
        rec = rec or {}
        print(json.dumps({"ok": ok, "error": err, "note": note,
                          "platform": platform,
                          "user_id": str(rec.get("user_id") or uid),
                          "handle": rec.get("handle") or ""}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd in ("channels-conversation", "channels-usage", "channels-user-test"):
        # Read-only (and, for -user-test, a dry run). The id is resolved the same
        # strict way channels-user resolves it: people on file only.
        if len(rest) < 2:
            _fail(f"usage: {cmd} <platform> <id>"
                  + (" [limit]" if cmd == "channels-conversation" else
                     " [days]" if cmd == "channels-usage" else
                     " dm | group [mentioned|unmentioned]"))
        platform, uid = rest[0], _person_id(rest[0], rest[1])
        extra = rest[2:]
        if cmd == "channels-conversation":
            number = int(extra[0]) if extra and extra[0].isdigit() else 100
            result = user_admin.conversation_view(platform, uid, number)
        elif cmd == "channels-usage":
            number = int(extra[0]) if extra and extra[0].isdigit() else 30
            result = user_admin.usage_view(platform, uid, number)
        else:
            context = (extra[0].lower() if extra else "dm")
            mentioned = not (len(extra) > 1 and extra[1].lower() == "unmentioned")
            result = user_admin.simulate(platform, uid, context, mentioned)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-add-person":
        if len(rest) < 2:
            _fail("usage: channels-add-person <platform> <id|@handle> [name ...]")
        platform, ident, name = rest[0], rest[1], " ".join(rest[2:])
        ok, err, rec, existing = user_admin.add_person(platform, ident, name)
        rec = rec or {}
        if not ok:
            note = ""
        elif existing:
            note = "already on file — nothing changed"
        elif rec.get("placeholder"):
            note = ("added — nothing is switched on for them yet. Only their "
                    "handle is known; the id fills in when they first message.")
        else:
            note = "added — nothing is switched on for them yet."
        print(json.dumps({
            "ok": ok, "error": err, "platform": platform,
            "user_id": rec.get("user_id", ""),
            "placeholder": bool(rec.get("placeholder")),
            "existing": existing, "note": note,
        }, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-rename":
        if len(rest) < 2:
            _fail("usage: channels-rename <platform> <id|@handle> [name ...]")
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        ok, err, now = user_admin.rename(platform, uid, " ".join(rest[2:]))
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "user_id": uid, "name": now}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-remove-person":
        if len(rest) < 2:
            _fail("usage: channels-remove-person <platform> <id|@handle>")
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        ok, err = user_admin.remove_person(platform, uid)
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "user_id": uid}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-note":
        usage = ("usage: channels-note <platform> <id|@handle> "
                 "list | edit <note-id> <text ...> | delete <note-id>")
        if len(rest) < 3:
            _fail(usage)
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        action = rest[2]
        if action == "list":
            rec = people.get(platform, uid)
            if rec is None:
                _fail(f"{uid} isn't registered on {platform}")
            print(json.dumps({"ok": True, "platform": platform, "user_id": uid,
                              "notes": people.list_notes(rec)}, indent=2))
            return
        if action == "edit" and len(rest) >= 5:
            ok, err, notes = user_admin.edit_note(
                platform, uid, rest[3], " ".join(rest[4:]))
        elif action == "delete" and len(rest) == 4:
            ok, err, notes = user_admin.delete_note(platform, uid, rest[3])
        else:
            _fail(usage)
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "user_id": uid, "notes": notes}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-forget":
        usage = "usage: channels-forget <platform> <id|@handle> [--history] [--yes]"
        flags = {a for a in rest[2:] if a.startswith("--")}
        if len(rest) < 2 or not flags <= {"--history", "--yes"} or \
                len(rest) - 2 != len(flags):
            _fail(usage)
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        ok, err, report = user_admin.forget_person(
            platform, uid, purge_history="--history" in flags,
            dry_run="--yes" not in flags)
        print(json.dumps({"ok": ok, "error": err, "report": report,
                          **({"preview": True,
                              "hint": "nothing was changed — add --yes to do it"}
                             if ok and report.get("dry_run") else {})},
                         indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-link":
        if len(rest) < 4:
            _fail("usage: channels-link <platform> <id|@handle> "
                  "<other-platform> <id|@handle|name ...>")
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        ok, err, note = user_admin.link_accounts(
            platform, uid, rest[2], " ".join(rest[3:]))
        print(json.dumps({"ok": ok, "error": err, "note": note,
                          "platform": platform, "user_id": uid,
                          "other_platform": rest[2]}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-unlink":
        if len(rest) < 2:
            _fail("usage: channels-unlink <platform> <id|@handle>")
        platform = rest[0]
        uid = _person_id(platform, rest[1])
        ok, err = user_admin.unlink_accounts(platform, uid)
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "user_id": uid}, indent=2))
        sys.exit(0 if ok else 1)

    if cmd == "channels-master-tools":
        # L.36-P9. Turning it ON widens access, so it only previews unless told
        # --yes; OFF takes access away and applies at once.
        usage = "usage: channels-master-tools <platform> [on|off] [--yes]"
        flags = [a for a in rest[1:] if a.startswith("--")]
        words = [a for a in rest[1:] if not a.startswith("--")]
        if not rest or rest[0] not in PLATFORMS or len(words) > 1 \
                or not set(flags) <= {"--yes"}:
            _fail(usage)
        platform = rest[0]
        if not words:
            print(json.dumps(master_tools.view(platform), indent=2))
            return
        if words[0].lower() not in ("on", "off"):
            _fail(usage)
        result = master_tools.set_master(platform, words[0].lower() == "on",
                                         confirm="--yes" in flags)
        if result.get("needs_confirm"):
            result["hint"] = "nothing was changed \u2014 add --yes to turn it on"
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-send":
        # L.36-P13. One known person, the owner's own words; same limits as the
        # send_dm tool because it IS that tool underneath.
        usage = "usage: channels-send <platform> <id|@handle> [--yes] <message ...>"
        if len(rest) < 3 or rest[0] not in PLATFORMS:
            _fail(usage)
        platform, uid = rest[0], _person_id(rest[0], rest[1])
        body = rest[2:]
        confirm = bool(body and body[0] == "--yes")
        if confirm:
            body = body[1:]
        text = " ".join(body).strip()
        if not text:
            _fail(usage)
        result = panel_dm.send(platform, uid, text, confirm=confirm)
        if result.get("needs_confirm"):
            result["hint"] = "nothing was sent \u2014 add --yes right after the id to send it"
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-denied":
        # L.36-P14. Read-only; never prints what a stranger wrote.
        platform = rest[0] if rest and rest[0] in PLATFORMS else None
        tail = rest[1:] if platform else rest
        if len(tail) > 1 or (tail and not tail[0].isdigit()):
            _fail("usage: channels-denied [platform] [days]")
        print(json.dumps(denied.denied_view(
            platform, int(tail[0]) if tail else denied.DEFAULT_DAYS), indent=2))
        return

    if cmd == "channels-servers":
        platform = rest[0] if rest else "discord"
        if platform not in PLATFORMS:
            _fail(f"unknown platform '{platform}'")
        print(json.dumps(servers.view(platform), indent=2))
        return

    if cmd == "channels-guilds":
        # L.36-P17. allowed_guilds from the panel. Narrowing only: see
        # channels/allowed_guilds.py for what needs --yes and what is refused.
        usage = "usage: channels-guilds <platform> [add|remove <id>] [--yes]"
        flags = [a for a in rest[1:] if a.startswith("--")]
        words = [a for a in rest[1:] if not a.startswith("--")]
        if not rest or rest[0] not in PLATFORMS or len(words) not in (0, 2) \
                or not set(flags) <= {"--yes"}:
            _fail(usage)
        platform = rest[0]
        if not words:
            result = allowed_guilds.view(platform)
            print(json.dumps(result, indent=2))
            sys.exit(0 if result.get("ok") else 1)
        if words[0].lower() not in ("add", "remove"):
            _fail(usage)
        result = allowed_guilds.set_allowed(
            platform, words[1], remove=words[0].lower() == "remove",
            confirm="--yes" in flags)
        if result.get("needs_confirm"):
            result["hint"] = "nothing was changed \u2014 add --yes to apply it"
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if cmd == "channels-server-set":
        if len(rest) < 5:
            _fail("usage: channels-server-set <platform> guild|channel <id> "
                  "<enabled|tools|mention> <on|off|inherit>")
        platform, kind, ident, switch, value = rest[:5]
        ok, err, detail = servers.set_switch(platform, kind, ident, switch, value)
        print(json.dumps({"ok": ok, "error": err, "platform": platform,
                          "kind": kind, "id": ident, **detail}, indent=2))
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
