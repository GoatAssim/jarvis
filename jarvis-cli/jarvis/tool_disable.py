"""Tools and saved commands the owner has switched OFF in the Tool Manager.

WHAT "DISABLED" MEANS
---------------------
Owner's definition (2026-10-03): **the model cannot see the tool, and cannot use
it.** Concretely, for a disabled tool or saved command:

  * it is not offered to the model: not in the schemas a provider is sent, not
    in `search_tools` / `search_commands` results, not served by
    `get_tool_schema`, not suggested as a "did you mean";
  * if the model calls it anyway (a stale prompt, a guessed name), the call is
    refused and nothing runs;
  * scheduled jobs and unattended runs that name it fail clearly at fire time
    instead of silently running or silently doing nothing.

What it does NOT mean: the owner can still run it. The Debug panel, the Test
Checklist and `jarvis <saved-command>` typed at a terminal are the owner's own
hands, not the model's, so they keep working (that is how you test a tool you
are about to switch back on). This is the "hidden from the model, plus refused
for anything automatic" reading of master plan Q-L25f -- not "refused
everywhere".

THE AGENTS.md INVARIANT
-----------------------
AGENTS.md says `TOOLS` / `CORE_TOOL_SCHEMAS` stay fully loaded and locally
executable, and that real execution is never gated behind the router/discovery
layer. Both still hold: a disabled tool stays in `TOOLS` and stays executable by
the owner, and nothing here lives in the router. The model-facing refusal sits at
the choke points that already gate execution (`tools.execute_tool`, next to
JARVIS_ALLOWED_TOOLS, and `ai_client._make_tool_executor`).

WHERE IT LIVES, AND WHY THERE
-----------------------------
`~/.jarvis/disabled.json`, NOT inside a command's own spec in commands.json and
NOT in actions/. Two reasons:

  1. A disabled flag stored in a saved command's spec could be flipped back on
     by the model through `update_command`. This file has no model-facing
     writer (tests/test_tool_disable.py asserts it), so a model cannot undo the
     owner's switch.
  2. `script.bat` overwrites the install directory on every rebuild; ~/.jarvis
     never moves, so the switch survives a reinstall (master plan Q-L25a).

The file is re-read on every call, like tool_safety.json: each `jarvis` run is a
fresh OS process (AGENTS.md), and the web server and the daemons spawn those.
Nothing is cached in memory that a Tool Manager click would have to invalidate.

PROTECTED TOOLS
---------------
A few tools cannot be disabled, because switching them off would break Jarvis
rather than restrict it. The list is built from what the code actually hard-wires,
not guessed:

  search_tools, search_commands, get_tool_schema, load_skill
      The system prompt tells the model to call search_tools / search_commands
      when it can't see a tool, the catalog tier keeps get_tool_schema callable
      so demoted tools aren't stranded, and ai_providers.DISCOVERY_TOOL_NAMES
      treats all of them as the discovery set. Disable one and the prompt points
      the model at a tool it doesn't have, and every tool outside the routed set
      becomes unreachable.

There is deliberately nothing else. The confirmation gate, risk_review() and the
AI-review gating live in ai_client._make_tool_executor and tool_safety.py, not in
a model tool, so no tool is a "confirm-gate dependency". If that ever changes,
add the tool here and tests/test_tool_disable.py will pin it.

Protection is enforced where the list is READ (disabled_tools() subtracts it), not
only where the switch is flipped, so a hand-edited disabled.json, or one written
before a name joined this list, cannot disable a protected tool either.

Import-light on purpose: tools.py and ai_client.py import this at module level, so
it must not import either of them (or anything that does).
"""

import re
from pathlib import Path

from . import atomic_io

JARVIS_DIR = Path.home() / ".jarvis"
DISABLED_FILE = JARVIS_DIR / "disabled.json"

# name -> why it can't be switched off (shown in the Tool Manager).
PROTECTED_TOOLS = {
    "search_tools": "Discovery: without it the model can't find any tool outside the ones offered up front.",
    "search_commands": "Discovery: the prompt tells the model to call it to find your saved commands.",
    "get_tool_schema": "Discovery: it is how a tool the model has only seen by name becomes callable.",
    "load_skill": "Discovery: part of the discovery set the prompt and providers rely on.",
}

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProtectedToolError(ValueError):
    """Raised when someone tries to switch off a protected tool."""


def _empty():
    return {"tools": [], "commands": []}


def _read():
    """The raw file as {"tools": [str], "commands": [str]}. Never raises.

    Goes through atomic_io.read_json so a torn write falls back to the .bak
    rather than reading as empty -- an unreadable file reading as "nothing is
    disabled" would silently re-enable everything the owner switched off.
    """
    data = atomic_io.read_json(DISABLED_FILE, default=None, expect=dict)
    if not isinstance(data, dict):
        return _empty()
    out = _empty()
    for key in ("tools", "commands"):
        raw = data.get(key)
        if isinstance(raw, list):
            out[key] = sorted({n for n in raw if isinstance(n, str) and n.strip()})
    return out


def _write(data):
    # write_json never raises -- it returns False. Ignoring that would let the
    # Tool Manager show a switch as ON that was never saved, which for a
    # "the model cannot use this" setting is the worst way to fail.
    if not atomic_io.write_json(DISABLED_FILE, data):
        raise OSError("couldn't save %s" % DISABLED_FILE)


# --- reading ------------------------------------------------------------------


def protected_reason(name):
    """Why `name` can't be disabled, or None when it can."""
    return PROTECTED_TOOLS.get((name or "").strip())


def disabled_tools():
    """frozenset of tool names currently switched off (protected ones excluded)."""
    return frozenset(n for n in _read()["tools"] if n not in PROTECTED_TOOLS)


def disabled_commands():
    """frozenset of saved-command names currently switched off."""
    return frozenset(_read()["commands"])


def is_tool_disabled(name):
    name = (name or "").strip()
    return bool(name) and name in disabled_tools()


def is_command_disabled(name):
    name = (name or "").strip()
    return bool(name) and name in disabled_commands()


def visible_commands(commands):
    """`commands` without the disabled ones. Hands back the same object when
    nothing is disabled (the common case) and passes a non-dict through
    untouched, so it is safe to put at the top of any function that takes the
    saved-commands dict."""
    if not isinstance(commands, dict) or not commands:
        return commands
    off = disabled_commands()
    if not off:
        return commands
    return {n: s for n, s in commands.items() if n not in off}


def refusal_for_tool(name):
    """The result a refused call returns. One shape everywhere so the model, the
    scheduler and the Debug panel all see the same words."""
    return {
        "ok": False, "blocked": True, "retryable": False, "disabled": True,
        "error": "%s is switched off by the owner and cannot be used." % name,
        "hint": ("Do not call it again, and do not try another tool to get the same "
                 "effect. Tell the user it is switched off."),
    }


def refusal_for_command(name):
    return {
        "ok": False, "blocked": True, "retryable": False, "disabled": True,
        "command": name,
        "error": "The saved command %s is switched off by the owner and cannot be run." % name,
        "hint": ("Do not run it, and do not rebuild it from its steps with other tools. "
                 "Tell the user it is switched off."),
    }


# --- writing --------------------------------------------------------------------


def _set(key, name, disabled):
    data = _read()
    current = set(data[key])
    if disabled:
        current.add(name)
    else:
        current.discard(name)
    data[key] = sorted(current)
    _write(data)
    return name in current


def set_tool_disabled(name, disabled):
    """Switch a tool off (True) or on (False). Returns {"name", "disabled"}.

    Raises ProtectedToolError for a protected tool being switched off, ValueError
    for a malformed name, OSError if the file couldn't be saved. Switching a
    protected tool ON is always allowed -- it is how a stale entry is cleared.
    """
    name = (name or "").strip()
    if not _TOOL_NAME_RE.match(name):
        raise ValueError("not a valid tool name: %r" % name)
    if disabled and name in PROTECTED_TOOLS:
        raise ProtectedToolError("%s can't be switched off. %s" % (name, PROTECTED_TOOLS[name]))
    return {"name": name, "disabled": _set("tools", name, bool(disabled))}


def set_command_disabled(name, disabled):
    """Switch a saved command off or on. Same contract as set_tool_disabled."""
    name = (name or "").strip()
    if not name or any(ch.isspace() for ch in name) or any(ch in name for ch in "\r\n\0"):
        raise ValueError("not a valid command name: %r" % name)
    return {"name": name, "disabled": _set("commands", name, bool(disabled))}


def rename_command(old, new):
    """A renamed saved command keeps its switch. Without this a disabled command
    could be re-enabled just by renaming it in the commands panel: the new name
    isn't on the list. No-op when `old` wasn't disabled."""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return
    if old in _read()["commands"]:
        set_command_disabled(new, True)
        _set("commands", old, False)


def forget_tool(name):
    """Drop a tool's entry (its file was deleted, so a later tool with the same
    name must not inherit an old switch). Never raises."""
    try:
        if (name or "").strip() in _read()["tools"]:
            _set("tools", name.strip(), False)
    except Exception:  # noqa: BLE001 -- cleanup must never break a delete
        pass


def forget_command(name):
    try:
        if (name or "").strip() in _read()["commands"]:
            _set("commands", name.strip(), False)
    except Exception:  # noqa: BLE001
        pass
