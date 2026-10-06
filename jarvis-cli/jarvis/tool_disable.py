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

PERSONAS AND UI ELEMENTS (Tool Manager rows beyond tools and commands)
----------------------------------------------------------------------
The Tool Manager also lists the other things a tool file can contribute, and gives
each the same owner switch:

  * a PERSONA a file registered (`PERSONAS`, persona_registry.py): OFF hides it
    from the Skin modal's picker (`jarvis personas-list` stops returning it unless
    `--all`). Nothing about the model's own tools changes.
  * a UI ELEMENT a file shipped (`TOOL_UI`, tool_ui.py -- a button that opens the
    tool's own page, or a Menu entry that opens it in a panel): OFF removes the
    button / Menu entry and refuses to serve its files. The tool that goes with it
    keeps working; the owner is only hiding its screen.
  * a DAEMON is NOT stored here. daemons.json already has an `enabled` flag with
    exactly the right meaning (can't be started by hand, by a schedule, by
    autostart or by the model) and the Daemons panel already edits it, so the Tool
    Manager edits the same flag rather than keeping a second copy that could
    disagree with it.

  * a SKIN (`"skins": [...]`): the Skin modal's accent swatches (`preset:<id>`) and
    its theme gallery entries that ship with the app (`theme:<id>`). OFF hides the
    swatch / theme card from the picker. Skins are defined in the browser
    (web/public/app.js SKIN_PRESETS, ui-kit.js BUILTIN_THEMES), so unlike every other
    kind the Python side cannot check that an id exists -- only that it is
    well-formed; an id nothing draws is harmless. The one currently applied keeps
    applying: switching a skin off only takes it out of the picker, it never
    repaints the page.
  * a BUILT-IN PERSONA (Verity, J.A.R.V.I.S, F.R.I.D.A.Y., E.D.I.T.H., K.A.R.E.N.)
    uses the same `"personas"` list as a tool-registered one; their ids are exactly
    persona_registry.RESERVED_PERSONA_IDS, so the two can never collide.

Personas, skins and UI elements live in the SAME `disabled.json` as tools and commands
(`"personas": [...]`, `"skins": [...]`, `"ui": [...]`), for the same two reasons: no model-facing
writer, and it survives a reinstall. Nothing here is protected: switching either
off never breaks Jarvis.

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
# A persona id may also contain "-" (persona_registry._ID_RE); a UI id is a tool-style name.
_PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,48}$")
_UI_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# A skin id is "<what>:<id>": `preset:` = an accent swatch, `theme:` = a built-in
# theme card. Ids come from the browser (SKIN_PRESETS has "rose-gold", BUILTIN_THEMES
# has "mark_i"), so both separators are allowed.
_SKIN_ID_RE = re.compile(r"^(preset|theme):[a-z0-9][a-z0-9_-]{0,48}$")

# Every list the file carries. Kept in one place so _read/_empty/_set can't drift.
_KINDS = ("tools", "commands", "personas", "skins", "ui")


class ProtectedToolError(ValueError):
    """Raised when someone tries to switch off a protected tool."""


def _empty():
    return {k: [] for k in _KINDS}


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
    for key in _KINDS:
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


def disabled_personas():
    """frozenset of persona ids the owner has hidden from the Skin modal."""
    return frozenset(_read()["personas"])


def disabled_skins():
    """frozenset of skin ids (`preset:<id>` / `theme:<id>`) hidden from the Skin modal."""
    return frozenset(_read()["skins"])


def disabled_ui():
    """frozenset of TOOL_UI element ids the owner has switched off."""
    return frozenset(_read()["ui"])


def is_persona_disabled(persona_id):
    persona_id = (persona_id or "").strip()
    return bool(persona_id) and persona_id in disabled_personas()


def is_skin_disabled(skin_id):
    skin_id = (skin_id or "").strip()
    return bool(skin_id) and skin_id in disabled_skins()


def is_ui_disabled(ui_id):
    ui_id = (ui_id or "").strip()
    return bool(ui_id) and ui_id in disabled_ui()


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


def set_persona_disabled(persona_id, disabled):
    """Hide (True) or show (False) a registered persona in the Skin modal.
    Same contract as set_tool_disabled: ValueError for a malformed id, OSError if
    the file couldn't be saved. Returns {"id", "disabled"}."""
    persona_id = (persona_id or "").strip()
    if not _PERSONA_ID_RE.match(persona_id):
        raise ValueError("not a valid persona id: %r" % persona_id)
    return {"id": persona_id, "disabled": _set("personas", persona_id, bool(disabled))}


def set_skin_disabled(skin_id, disabled):
    """Hide (True) or show (False) one accent swatch (`preset:<id>`) or built-in
    theme (`theme:<id>`) in the Skin modal. Same contract as set_persona_disabled.
    Returns {"id", "disabled"}."""
    skin_id = (skin_id or "").strip()
    if not _SKIN_ID_RE.match(skin_id):
        raise ValueError("not a valid skin id (expected preset:<id> or theme:<id>): %r" % skin_id)
    return {"id": skin_id, "disabled": _set("skins", skin_id, bool(disabled))}


def set_ui_disabled(ui_id, disabled):
    """Switch a tool-shipped UI element (button / Menu entry) off or on."""
    ui_id = (ui_id or "").strip()
    if not _UI_ID_RE.match(ui_id):
        raise ValueError("not a valid UI element id: %r" % ui_id)
    return {"id": ui_id, "disabled": _set("ui", ui_id, bool(disabled))}


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


def forget_persona(persona_id):
    try:
        if (persona_id or "").strip() in _read()["personas"]:
            _set("personas", persona_id.strip(), False)
    except Exception:  # noqa: BLE001
        pass


def forget_skin(skin_id):
    try:
        if (skin_id or "").strip() in _read()["skins"]:
            _set("skins", skin_id.strip(), False)
    except Exception:  # noqa: BLE001
        pass


def forget_ui(ui_id):
    """Drop a UI element's entry (its tool file was deleted, so a later element with
    the same id must not inherit an old switch). Never raises."""
    try:
        if (ui_id or "").strip() in _read()["ui"]:
            _set("ui", ui_id.strip(), False)
    except Exception:  # noqa: BLE001
        pass
