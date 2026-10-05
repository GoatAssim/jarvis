"""Per-person permissions that the three global allow-lists cannot express.

WHAT THIS FILE IS, AND WHAT IT IS NOT
-------------------------------------
`config.py` already answers three yes/no questions per person, as allow-lists:
may they DM the bot, do they get a reply, may their message cause tools to
run. Those lists stay the single source of truth for those three — this file
does not copy them and the Channels panel reads and writes the very same
lists the CLI does, so the two can never disagree (master plan L.36).

What the lists cannot say is anything finer than "tools: yes/no":

    tools    WHICH tools this one person may cause to run. Without this a
             person in tool_allowlist gets the whole catalogue (minus the
             owner-only DM tools), which is the broadest grant the system has.
    can_dm   whether Jarvis itself may message this person on the owner's
             behalf (the `send_dm` tool). `people.follow == blocked` already
             refuses that, but blocking is a blunt instrument: it also means
             "stop asking me about them".

Identity (name, handle, picture, first seen) stays in people.py. This file is
permission only, keyed the same way: "<platform>:<user_id>".

FAILS CLOSED
------------
`atomic_io.read_json` cannot tell "no file" from "unreadable file" — both give
the default. For a store of RESTRICTIONS that is the wrong default: a torn
user_perms.json that reads as "empty" would silently hand a person who was
limited to two tools the whole catalogue. So `_load()` here separates the two
cases, and an unreadable file raises `PermsUnreadable`; the gateway answers
that one message with tools off (see base.py) instead of guessing.

STORAGE
-------
    ~/.jarvis/channels/user_perms.json

Only non-default values are stored: a person with no restrictions has no
record, so the file stays as small as the number of people actually limited.
"""

import re

from .. import atomic_io
from .directory import CHANNELS_DIR

PERMS_FILE = CHANNELS_DIR / "user_perms.json"

# Tool scope modes. "inherit" = no per-person restriction (what everybody had
# before this file existed). "custom" = ONLY the tools in `allow`. An
# allow-list rather than a deny-list on purpose: a tool added next month must
# not be silently available to a person who was deliberately limited today.
TOOLS_INHERIT = "inherit"
TOOLS_CUSTOM = "custom"
TOOLS_MODES = (TOOLS_INHERIT, TOOLS_CUSTOM)

# Tools the machinery itself needs in order to function for someone who has
# at least one real tool. Without `search_tools` a person limited to three
# tools could never discover them (the model is offered tools by name through
# discovery), and the identity prompt tells the model to call
# `remember_sender` for a stranger whose name it does not know. None of these
# can act on the PC. They are added to the effective list in
# effective_tool_scope(), never stored, so the UI shows exactly what the
# owner ticked.
PLUMBING_TOOLS = (
    "search_tools", "get_tool_schema", "search_commands",
    "remember_sender", "who_am_i_talking_to",
)

# A tool name is typed by the web UI and ends up joined into a comma list in
# an environment variable, so it is strictly validated: a comma in a name
# would smuggle in a second tool.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_TOOLS = 400


class PermsUnreadable(Exception):
    """user_perms.json exists but cannot be read. Callers fail closed."""


def key(platform, user_id):
    return f"{platform}:{str(user_id or '').strip()}"


def _load():
    """Whole store. {} when there is genuinely no file; raises
    PermsUnreadable when a file is present but unusable (see FAILS CLOSED)."""
    present = PERMS_FILE.exists() or PERMS_FILE.with_suffix(
        PERMS_FILE.suffix + ".bak").exists()
    data = atomic_io.read_json(PERMS_FILE, default=None, expect=dict)
    if data is None:
        if present:
            raise PermsUnreadable(f"{PERMS_FILE.name} is present but unreadable")
        return {}
    return data


def _save(data):
    return atomic_io.write_json(PERMS_FILE, data)


def clean_tool_names(names):
    """Validated, de-duplicated, order-preserving tool names. Raises
    ValueError on a name that is not a plausible tool name rather than
    dropping it, so a typo is reported instead of silently granting less (or,
    worse, being 'fixed' into something else)."""
    out = []
    for raw in names or []:
        name = str(raw or "").strip()
        if not name:
            continue
        if not _NAME_RE.match(name):
            raise ValueError(f"'{name[:40]}' is not a valid tool name")
        if name not in out:
            out.append(name)
    if len(out) > MAX_TOOLS:
        raise ValueError(f"too many tools (max {MAX_TOOLS})")
    return out


def _log_change(platform, user_id, kind, **fields):
    """One line in the permission change log (changelog.py), after the change
    was saved. Never raises: the log must not be why a limit fails to apply."""
    try:
        from . import changelog
        changelog.record(platform, kind, user_id, **fields)
    except Exception:  # noqa: BLE001
        pass


def _defaults():
    return {"tools": {"mode": TOOLS_INHERIT, "allow": []}, "can_dm": True}


def normalize(entry):
    """A stored entry -> the full, clean shape. Tolerant of a hand-edited
    file (reads never raise), strict on writes (see set_tools)."""
    out = _defaults()
    if not isinstance(entry, dict):
        return out
    tools = entry.get("tools")
    if isinstance(tools, dict):
        mode = tools.get("mode")
        if mode in TOOLS_MODES:
            out["tools"]["mode"] = mode
        allow = tools.get("allow")
        if isinstance(allow, list):
            try:
                out["tools"]["allow"] = clean_tool_names(allow)
            except ValueError:
                # A hand-edit with a bad name in a RESTRICTION list: keep the
                # valid names and carry on. Dropping the whole list would turn
                # "limited" into "no tools"; keeping a bad name is harmless
                # (it matches nothing).
                out["tools"]["allow"] = [
                    n for n in (str(x).strip() for x in allow)
                    if _NAME_RE.match(n)][:MAX_TOOLS]
    if entry.get("can_dm") is False:
        out["can_dm"] = False
    return out


def get(platform, user_id):
    """Normalized permissions for one person (defaults when none stored).
    Raises PermsUnreadable like _load()."""
    return normalize(_load().get(key(platform, user_id)))


def _is_default(entry):
    return entry == _defaults()


def _put(platform, user_id, entry):
    data = _load()
    k = key(platform, user_id)
    if _is_default(entry):
        data.pop(k, None)
    else:
        data[k] = entry
    if not _save(data):
        raise OSError("could not write user_perms.json")
    return entry


def set_tools(platform, user_id, mode, allow=None):
    """Set one person's tool scope. `allow` is only meaningful in custom mode.

    Switching to inherit clears the list, so flipping back to custom later
    starts from nothing rather than from a stale grant nobody remembers."""
    if mode not in TOOLS_MODES:
        raise ValueError(f"mode must be one of: {', '.join(TOOLS_MODES)}")
    entry = get(platform, user_id)
    before = dict(entry["tools"])
    if mode == TOOLS_INHERIT:
        entry["tools"] = {"mode": TOOLS_INHERIT, "allow": []}
    else:
        entry["tools"] = {"mode": TOOLS_CUSTOM,
                          "allow": clean_tool_names(allow or [])}
    saved = _put(platform, user_id, entry)
    if entry["tools"] != before:
        _log_change(platform, user_id, "tools", mode=entry["tools"]["mode"],
                    tools=entry["tools"]["allow"])
    return saved


def forget(platform, user_id):
    """Drop one person's stored limits entirely. Returns True if there was an
    entry. Raises PermsUnreadable / OSError like the other writers.

    An absent entry reads as the defaults (tools inherit the platform's, DMs
    allowed), which is MORE access than a custom tool list or a switched-off
    send_dm. That is why this is not a general "reset": its one caller,
    user_admin.forget_person, has already taken the person off every
    allow-list and refused anyone a "*" entry covers, so the defaults it leaves
    behind belong to nobody the gate lets through. If they write again they
    start as anyone new does."""
    data = _load()
    if data.pop(key(platform, user_id), None) is None:
        return False
    if not _save(data):
        raise OSError("could not write user_perms.json")
    return True


def copy_entry(platform, old_id, new_id):
    """Give `new_id` the same stored limits `old_id` has, leaving the old
    entry in place. Returns True if there was something to copy. Raises
    PermsUnreadable / OSError like the other writers.

    Used when a hand-added person known only by handle is re-keyed to a
    corrected handle (user_admin.set_handle): the limits must exist under the
    new key BEFORE the record moves, or the person would be let through with
    the defaults for as long as it takes to finish."""
    data = _load()
    found = data.get(key(platform, old_id))
    if found is None:
        return False
    data[key(platform, new_id)] = found
    if not _save(data):
        raise OSError("could not write user_perms.json")
    return True


def drop_entry(platform, user_id):
    """Remove one stored entry. The second half of a re-key, after the new
    key holds the limits. Returns True if one was removed."""
    return forget(platform, user_id)


def set_can_dm(platform, user_id, value):
    entry = get(platform, user_id)
    before = entry["can_dm"]
    entry["can_dm"] = bool(value)
    saved = _put(platform, user_id, entry)
    if entry["can_dm"] != before:
        _log_change(platform, user_id, "send_dm", on=entry["can_dm"])
    return saved


def effective_tool_scope(platform, user_id):
    """What the gateway should put in JARVIS_ALLOWED_TOOLS for this sender.

    None            no per-person restriction (inherit) — leave the ask exactly
                    as it was before this file existed.
    frozenset()     custom with nothing ticked: run the ask with NO tools, which
                    ai_client already treats as a plain no-tools ask (L.28 #2)
                    instead of offering discovery tools that are refused.
    frozenset(...)  only these, plus PLUMBING_TOOLS.

    Raises PermsUnreadable; the caller fails closed.
    """
    entry = get(platform, user_id)
    tools = entry["tools"]
    if tools["mode"] != TOOLS_CUSTOM:
        return None
    if not tools["allow"]:
        return frozenset()
    return frozenset(tools["allow"]) | frozenset(PLUMBING_TOOLS)


def resolve_tool_access(platform, user_id, may_use_tools):
    """The gateway's per-message tool decision, in ONE place.

    `may_use_tools` is what the gate said (permissions.Decision). Returns
    (may_use_tools, tool_scope, label, problem):

      may_use_tools  the final answer; this can only be False where the gate
                     said True, never the other way round
      tool_scope     None (no per-person limit) or the frozenset to put in
                     JARVIS_ALLOWED_TOOLS for this ask
      label          the short form the gateway log line prints
      problem        non-empty when the answer is "off" BECAUSE something went
                     wrong reading the limits, for the caller to log

    base.handle_message and the Channels panel's "Test as this person" both
    call this, so the dry run cannot drift from what a real message gets. It
    fails closed exactly as before: limits that are unreadable or fail to load
    mean this message runs with tools off."""
    if not may_use_tools:
        return False, None, "off", ""
    try:
        scope = effective_tool_scope(platform, user_id)
    except PermsUnreadable as exc:
        return (False, None, "off (limits unreadable)",
                f"per-person tool limits unreadable ({exc}); "
                f"answering without tools")
    except Exception as exc:  # noqa: BLE001 -- unknown limits mean none
        return (False, None, "off (limits failed)",
                f"per-person tool limits failed ({exc}); "
                f"answering without tools")
    if scope is None:
        return True, None, "on", ""
    if not scope:
        return False, scope, "off (person's list is empty)", ""
    return True, scope, f"custom ({len(scope)})", ""


def dm_allowed(platform, user_id):
    """May Jarvis message this person on the owner's behalf? Read by send_dm.
    An unreadable file answers False: a restriction we cannot read is treated
    as present."""
    try:
        return get(platform, user_id)["can_dm"]
    except PermsUnreadable:
        return False
