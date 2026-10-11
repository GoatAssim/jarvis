"""Tool Manager row + switch for TOOL_COMMANDS (master plan I.2.11 follow-up, 10-11g).

A palette command a tool file contributes (`/disk`, ...) already followed its TOOL being
switched off. It now also has a switch of its own, the owner's, stored in
~/.jarvis/disabled.json under "tool_commands" and shown as a row in the Tool Manager.

Covers:
  1. tool_disable store: round trip, bad names refused, the list survives alongside the
     other kinds, switching off one kind doesn't touch another, forget_tool_command,
     a hand-edited / corrupt entry is ignored rather than crashing.
  2. tools.command_off_reason / tool_commands_payload: the owner's switch alone takes the
     command off (the tool keeps working); the tool's switch alone still does; both at
     once; clearing one never clears the other; `switched_off` / `disabled_by_tool` mean
     what the Tool Manager needs them to mean; the default list leaves it out.
  3. tool_command_prepare refuses a command the owner switched off, and says why.
  4. CLI: `tool-disable-set <name> true --kind tool-command` (and false), an unknown name
     is refused when switching OFF, a malformed name is refused, `--kind` still rejects
     junk, `disabled-list` carries "tool_commands", `tool-commands run` is refused while
     off and works again after, and a SAVED command and a TOOL command with the same name
     don't share a switch.
  5. Deleting the tool file forgets its commands' switches (a new command with the same
     name must not inherit an old "off").
  6. Static: server.js allows the kind (and nothing wider), tool-manager.js talks to the
     routes it should, and the palette is told to reload.

No network, no live model. Runs against a throwaway HOME. Written, not run, by the author
of the patch: the owner runs it.

Run: python3 tests/test_tool_command_switch.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

TOOLS_DIR = Path(_HOME) / ".jarvis" / "tools"
TOOLS_DIR.mkdir(parents=True, exist_ok=True)
(TOOLS_DIR / "sw_demo.py").write_text(textwrap.dedent('''
    def _a(args):
        return {"ok": True, "message": "a ran"}

    def _b(args):
        return {"ok": True, "message": "b ran"}

    TOOL_SCHEMAS = [
        {"name": "sw_a", "description": "A.", "parameters": {"type": "object", "properties": {}}},
        {"name": "sw_b", "description": "B.", "parameters": {"type": "object", "properties": {}}},
    ]
    TOOLS = {"sw_a": _a, "sw_b": _b}
    TOOL_GROUP = "demo_switch"
    TOOL_COMMANDS = [
        {"name": "swa", "tool": "sw_a", "summary": "Run A", "riskTier": "safe"},
        {"name": "swb", "tool": "sw_b", "summary": "Run B", "riskTier": "caution"},
    ]
'''), encoding="utf-8")

from jarvis import atomic_io, tool_disable  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def reset():
    for path in (tool_disable.DISABLED_FILE, tool_disable.DISABLED_FILE.with_suffix(".json.bak")):
        if path.exists():
            path.unlink()


def cli(*args):
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=dict(os.environ),
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def jload(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


def full():
    return {c["name"]: c for c in system_tools.tool_commands_payload(include_disabled=True)["commands"]}


# --- 1. the store ------------------------------------------------------------------------

reset()
check("the file carries a tool_commands list", "tool_commands" in tool_disable._KINDS)
check("nothing is off to begin with", tool_disable.disabled_tool_commands() == frozenset())
check("set returns the name and the new state", tool_disable.set_tool_command_disabled("swa", True) == {"name": "swa", "disabled": True})
check("...and the reader sees it", tool_disable.disabled_tool_commands() == frozenset({"swa"}) and tool_disable.is_tool_command_disabled("swa"))
check("a command's switch is not a tool's", not tool_disable.is_tool_disabled("swa") and "swa" not in tool_disable.disabled_tools())
check("...nor a saved command's", not tool_disable.is_command_disabled("swa") and "swa" not in tool_disable.disabled_commands())
check("...nor a screen's", "swa" not in tool_disable.disabled_ui())
tool_disable.set_tool_disabled("sw_b", True)
tool_disable.set_ui_disabled("some_screen", True)
check("the other kinds sit beside it untouched",
      tool_disable.disabled_tools() == frozenset({"sw_b"}) and tool_disable.disabled_ui() == frozenset({"some_screen"})
      and tool_disable.disabled_tool_commands() == frozenset({"swa"}))
check("switching it back on", tool_disable.set_tool_command_disabled("swa", False) == {"name": "swa", "disabled": False}
      and tool_disable.disabled_tool_commands() == frozenset())
for bad in ("", "A", "has space", "x", "under_score", "9lives", "a" * 40, "../etc", "sw\na"):
    try:
        tool_disable.set_tool_command_disabled(bad, True)
        check("a malformed name is refused: %r" % bad, False, "no error")
    except ValueError:
        check("a malformed name is refused: %r" % bad, True)
check("the refused names were not stored", tool_disable.disabled_tool_commands() == frozenset())
tool_disable.set_tool_command_disabled("swa", True)
tool_disable.forget_tool_command("swa")
check("forget_tool_command drops it", tool_disable.disabled_tool_commands() == frozenset())
tool_disable.forget_tool_command("never-there")
tool_disable.forget_tool_command("")
tool_disable.forget_tool_command(None)
check("forget never raises, for a name that isn't there or isn't a name", True)
# a hand-edited file with junk in the list reads as the good entries only
atomic_io.write_json(tool_disable.DISABLED_FILE, {"tool_commands": ["swa", 5, None, "", "  "], "tools": []})
check("junk entries in the file are ignored", tool_disable.disabled_tool_commands() == frozenset({"swa"}), tool_disable.disabled_tool_commands())
# an older file with no such list reads as nothing off
atomic_io.write_json(tool_disable.DISABLED_FILE, {"tools": ["sw_b"], "commands": []})
check("a file written before this list existed reads as none off", tool_disable.disabled_tool_commands() == frozenset())
check("...and writing keeps the other lists", tool_disable.set_tool_command_disabled("swb", True)["disabled"]
      and tool_disable.disabled_tools() == frozenset({"sw_b"}))

# --- 2. off reason and payload -----------------------------------------------------------

reset()
ent = {c["name"]: c for c in system_tools.AUTO_COMMANDS}
check("both commands are registered", {"swa", "swb"} <= set(ent), list(ent))
check("on by default", system_tools.command_off_reason(ent["swa"]) == "")
tool_disable.set_tool_command_disabled("swa", True)
reason = system_tools.command_off_reason(ent["swa"])
check("the owner's switch alone takes it off, and says so", "switched off in the Tool Manager" in reason and "tool sw_a" not in reason, reason)
check("...while the tool itself is still on", not tool_disable.is_tool_disabled("sw_a") and "sw_a" in system_tools.TOOLS)
p = full()
check("payload: switched_off and disabled, not by the tool", p["swa"]["switched_off"] and p["swa"]["disabled"] and not p["swa"]["disabled_by_tool"], p["swa"])
check("payload: the sibling is untouched", not p["swb"]["disabled"] and not p["swb"]["switched_off"] and not p["swb"]["disabled_by_tool"])
check("the default list leaves it out", "swa" not in {c["name"] for c in system_tools.tool_commands_payload()["commands"]}
      and "swb" in {c["name"] for c in system_tools.tool_commands_payload()["commands"]})
tool_disable.set_tool_command_disabled("swa", False)
tool_disable.set_tool_disabled("sw_a", True)
p = full()
check("the tool's switch alone still takes it off", p["swa"]["disabled"] and "sw_a" in p["swa"]["disabled_reason"], p["swa"])
check("...and that is `disabled_by_tool`, not the owner's switch", p["swa"]["disabled_by_tool"] and not p["swa"]["switched_off"])
tool_disable.set_tool_command_disabled("swa", True)
p = full()
check("both at once: the owner's switch is the one reported", p["swa"]["switched_off"] and not p["swa"]["disabled_by_tool"] and p["swa"]["disabled"], p["swa"])
tool_disable.set_tool_disabled("sw_a", False)
check("clearing the tool's switch leaves the owner's", full()["swa"]["switched_off"] and full()["swa"]["disabled"])
tool_disable.set_tool_command_disabled("swa", False)
tool_disable.set_tool_disabled("sw_b", True)
tool_disable.set_tool_disabled("sw_a", True)
p = full()
check("every tool of the file off takes both commands, as before", p["swa"]["disabled_by_tool"] and p["swb"]["disabled_by_tool"])
reset()
check("a payload entry has no absolute path", "_dir" not in json.dumps(full()) and _HOME not in json.dumps(full()))

# --- 3. prepare ---------------------------------------------------------------------------

tool_disable.set_tool_command_disabled("swb", True)
entry, tool, args, err = system_tools.tool_command_prepare("swb", [])
check("prepare refuses a command the owner switched off", entry is None and tool is None and err and "swb" in err and "switched off" in err, err)
entry, tool, args, err = system_tools.tool_command_prepare("swa", [])
check("...and still prepares its sibling", err is None and tool == "sw_a", err)
tool_disable.set_tool_command_disabled("swb", False)
entry, tool, args, err = system_tools.tool_command_prepare("swb", [])
check("...and prepares it again once switched on", err is None and tool == "sw_b", err)

# --- 4. CLI -------------------------------------------------------------------------------

reset()
rc, out, err = cli("tool-disable-set", "swa", "true", "--kind", "tool-command")
check("CLI: switch a tool command off", rc == 0 and jload(out) == {"name": "swa", "disabled": True}, (rc, out, err[-200:]))
check("CLI: ...stored in the file", tool_disable.disabled_tool_commands() == frozenset({"swa"}))
rc, out, err = cli("disabled-list")
dl = jload(out)
check("CLI: disabled-list carries tool_commands", rc == 0 and dl.get("tool_commands") == ["swa"], (rc, out[:300]))
check("CLI: ...and still the other lists", all(k in dl for k in ("tools", "commands", "personas", "skins", "ui", "ui_by_tool", "protected")))
rc, out, err = cli("tool-commands", "run", "swa", "[]")
check("CLI: tool-commands run is refused while off", rc == 1 and "switched off" in jload(out).get("error", "") and "a ran" not in out, (rc, out))
rc, out, err = cli("tool-commands", "run", "swb", "[]")
check("CLI: ...the sibling still runs", rc == 0 and "b ran" in out, (rc, out, err[-200:]))
rc, out, err = cli("tool-commands", "list")
check("CLI: list leaves it out", "swa" not in {c["name"] for c in jload(out).get("commands", [])}, out[:300])
rc, out, err = cli("tool-commands", "list", "--all")
row = next((c for c in jload(out).get("commands", []) if c["name"] == "swa"), {})
check("CLI: list --all keeps it, flagged", row.get("disabled") is True and row.get("switched_off") is True and row.get("disabled_by_tool") is False, row)
rc, out, err = cli("tool-disable-set", "swa", "false", "--kind", "tool-command")
check("CLI: switch it back on", rc == 0 and jload(out) == {"name": "swa", "disabled": False}, (rc, out))
rc, out, err = cli("tool-commands", "run", "swa", "[]")
check("CLI: ...and it runs again", rc == 0 and "a ran" in out, (rc, out, err[-200:]))
rc, out, err = cli("tool-disable-set", "no-such-cmd", "true", "--kind", "tool-command")
check("CLI: an unknown name is refused when switching OFF", rc == 1 and "no such tool command" in jload(out).get("error", ""), (rc, out))
check("CLI: ...and nothing was stored", tool_disable.disabled_tool_commands() == frozenset())
rc, out, err = cli("tool-disable-set", "no-such-cmd", "false", "--kind", "tool-command")
check("CLI: a stale entry can always be cleared (switching ON needs no lookup)", rc == 0, (rc, out))
rc, out, err = cli("tool-disable-set", "Bad Name", "true", "--kind", "tool-command")
check("CLI: a malformed name is refused", rc == 1 and jload(out).get("error"), (rc, out))
rc, out, err = cli("tool-disable-set", "swa", "true", "--kind", "bogus")
check("CLI: an unknown --kind is refused and names the new one", rc == 1 and "tool-command" in jload(out).get("error", ""), (rc, out))
check("CLI: ...and stored nothing", tool_disable.disabled_tool_commands() == frozenset() and tool_disable.disabled_tools() == frozenset())
rc, out, err = cli("tool-disable-set", "swa", "true", "--kind", "tool")
check("CLI: kind tool still means a TOOL (swa is not one)", rc == 1 and "no such tool" in jload(out).get("error", ""), (rc, out))
rc, out, err = cli("command-disable-set", "swa", "true")
check("CLI: a saved command with that name doesn't exist, so it can't be switched off either", rc == 1, (rc, out))
check("CLI: and the tool command's switch was not touched by that", tool_disable.disabled_tool_commands() == frozenset())
# the saved-command list and the tool-command list are separate files' lists
tool_disable.set_command_disabled("swa", True)
check("a saved command called swa does not switch the tool command swa", "swa" not in {n for n, c in full().items() if c["switched_off"]}
      and not system_tools.command_off_reason(ent["swa"]))
tool_disable.set_command_disabled("swa", False)

# --- 5. deleting the file ------------------------------------------------------------------

from jarvis import custom_tools_store as store  # noqa: E402
reset()
tool_disable.set_tool_command_disabled("swa", True)
tool_disable.set_tool_command_disabled("unrelated", True)
res = store.delete_tool("sw_demo")
check("delete_tool reports ok", res.get("ok") is True, res)
check("deleting the file forgets ITS commands' switches", "swa" not in tool_disable.disabled_tool_commands(), tool_disable.disabled_tool_commands())
check("...and nobody else's", "unrelated" in tool_disable.disabled_tool_commands())

# --- 6. static -----------------------------------------------------------------------------

server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
m = re.search(r"const DISABLE_KINDS = new Set\(\[([^\]]*)\]\)", server)
kinds = re.findall(r'"([^"]+)"', m.group(1)) if m else []
check("server.js: the allowed kinds are exactly the five", sorted(kinds) == ["persona", "skin", "tool", "tool-command", "ui"], kinds)
check("server.js: a request can't pass the kind straight into argv beyond that list", "DISABLE_KINDS.has(kind)" in server)
tm = (ROOT / "web" / "public" / "tool-manager.js").read_text(encoding="utf-8")
check("tool-manager.js reads the full command list", '/api/tool-commands?all=1' in tm and tm.count('/api/tool-commands?all=1') >= 2)
check("tool-manager.js switches it with kind tool-command", 'kind === "tcmd" ? "tool-command" : kind' in tm)
check("tool-manager.js tells the palette to reload", "JarvisSlash.loadToolCommands(true)" in tm)
check("tool-manager.js locks a command that is off only for its tool", '(row.kind === "ui" || row.kind === "tcmd") && !!(row.extra && row.extra.disabled_by_tool)' in tm)
check("no model-facing module writes the switch",
      not [p.name for p in (CLI_DIR / "jarvis").rglob("*.py")
           if p.name not in ("tool_disable.py", "cli.py", "custom_tools_store.py", "tools.py")
           and "set_tool_command_disabled" in p.read_text(encoding="utf-8", errors="replace")])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
