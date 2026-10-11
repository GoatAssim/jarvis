"""Switching a tool off lists the palette commands that go off with it (leftover from the
Tool Manager tool-command-switch patch).

A TOOL_COMMANDS command follows its tool live (tools.command_off_reason). The Tool Manager's
confirmation listed the screens and daemons that go off with a file's last tool and the
scheduled jobs that name the tool, but not these. `tools.switch_off_commands(name)` is the new
list, `jarvis disabled-dependents tool <n>` carries it as `commands`.

  1. switch_off_commands: the command that runs the tool is listed; its sibling (another tool)
     is not; a command that is already off (the owner's switch, or its tool already off) is
     not news; unknown / empty / protected names give []; it writes nothing and never raises;
     it agrees with command_off_reason (apply the switch, and exactly the listed ones go off).
  2. CLI: `disabled-dependents tool <n>` has `commands`; for a saved command it has none; the
     existing `jobs` and `cascade` blocks are unchanged.
  3. Static: tool-manager.js asks for it and lists it in the confirmation.

No network, no live model. Throwaway HOME. Written, not run, by the author: the owner runs it.

Run: python3 tests/test_tool_off_commands.py
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-offcmd-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

TOOLS_DIR = Path(_HOME) / ".jarvis" / "tools"
TOOLS_DIR.mkdir(parents=True, exist_ok=True)
(TOOLS_DIR / "toc_demo.py").write_text(textwrap.dedent('''
    def _a(args):
        return {"ok": True, "message": "a ran"}

    def _b(args):
        return {"ok": True, "message": "b ran"}

    def _c(args):
        return {"ok": True, "message": "c ran"}

    TOOL_SCHEMAS = [
        {"name": "toc_a", "description": "A.", "parameters": {"type": "object", "properties": {}}},
        {"name": "toc_b", "description": "B.", "parameters": {"type": "object", "properties": {}}},
        {"name": "toc_c", "description": "C (no command).", "parameters": {"type": "object", "properties": {}}},
    ]
    TOOLS = {"toc_a": _a, "toc_b": _b, "toc_c": _c}
    TOOL_GROUP = "demo_off_commands"
    TOOL_COMMANDS = [
        {"name": "toca", "tool": "toc_a", "summary": "Run A", "riskTier": "safe"},
        {"name": "toca-two", "tool": "toc_a", "summary": "Run A another way", "riskTier": "safe"},
        {"name": "tocb", "tool": "toc_b", "summary": "Run B", "riskTier": "caution"},
    ]
'''), encoding="utf-8")

from jarvis import tool_disable  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def reset():
    for path in (tool_disable.DISABLED_FILE, tool_disable.DISABLED_FILE.with_suffix(".json.bak")):
        if path.exists():
            path.unlink()


def cli(*args):
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=dict(os.environ),
                       capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    return p.returncode, out


def src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_function():
    reset()
    S = system_tools.switch_off_commands
    ent = {c["name"] for c in system_tools.AUTO_COMMANDS}
    check("the demo commands are registered", {"toca", "toca-two", "tocb"} <= ent, ent)
    check("the commands that run the tool are listed, sorted; the other tool's is not", S("toc_a") == ["toca", "toca-two"], S("toc_a"))
    check("a tool with one command lists that one", S("toc_b") == ["tocb"])
    check("a tool with no command lists none", S("toc_c") == [])
    check("an unknown name, an empty one and None list none and do not raise", S("nope_zz") == [] and S("") == [] and S(None) == [])
    protected = next(iter(tool_disable.PROTECTED_TOOLS), "search_tools")
    check("a protected tool can never be switched off, so it lists nothing", S(protected) == [])
    check("whitespace around the name is ignored", S("  toc_b ") == ["tocb"])
    # nothing was written by asking
    check("asking changes nothing on disk", tool_disable.disabled_tools() == frozenset() and tool_disable.disabled_tool_commands() == frozenset())
    # the owner's own switch: already off, not news
    tool_disable.set_tool_command_disabled("toca", True)
    check("a command the owner already switched off is left out", S("toc_a") == ["toca-two"], S("toc_a"))
    tool_disable.set_tool_command_disabled("toca", False)
    # the tool already off: nothing more to say
    tool_disable.set_tool_disabled("toc_a", True)
    check("a tool that is already off lists nothing", S("toc_a") == [])
    check("...and its sibling is unaffected by that", S("toc_b") == ["tocb"])
    # last tool of the file: still the commands that run it; commands already off for another reason are not repeated
    tool_disable.set_tool_disabled("toc_b", True)
    check("the last tool on in a file: its own commands (nothing else is still on to list)", S("toc_c") == [], S("toc_c"))
    reset()
    # agreement with the rule it is built on
    off = set(tool_disable.disabled_tools())
    own = tool_disable.disabled_tool_commands()
    for name in ("toc_a", "toc_b", "toc_c"):
        listed = set(S(name))
        tool_disable.set_tool_disabled(name, True)
        now_off = {c["name"] for c in system_tools.AUTO_COMMANDS if system_tools.command_off_reason(c)}
        was_off = {c["name"] for c in system_tools.AUTO_COMMANDS if system_tools.command_off_reason(c, off, own)}
        tool_disable.set_tool_disabled(name, False)
        check("applying the switch for %s takes off exactly the listed commands (and no others)" % name,
              now_off - was_off == listed, (now_off - was_off, listed))
    check("every command is back on afterwards", not any(system_tools.command_off_reason(c) for c in system_tools.AUTO_COMMANDS))


def test_cli():
    reset()
    code, out = cli("disabled-dependents", "tool", "toc_a")
    check("CLI: a tool answer carries `commands`", code == 0 and out.get("commands") == ["toca", "toca-two"], (code, out))
    check("CLI: `jobs` and `cascade` are still there, unchanged in shape",
          isinstance(out.get("jobs"), list) and set(out.get("cascade", {})) == {"file", "screens", "daemons"}, out)
    code, out = cli("disabled-dependents", "tool", "no_such_tool_zzz")
    check("CLI: an unknown tool answers with an empty list", code == 0 and out.get("commands") == [], (code, out))
    code, out = cli("disabled-dependents", "command", "whatever")
    check("CLI: a saved command has no `commands` block", code == 0 and "commands" not in out and "cascade" not in out, (code, out))
    code, out = cli("disabled-dependents", "bogus", "x")
    check("CLI: a bad kind is still refused", code == 1)
    reset()


def test_static():
    tm = src("web/public/tool-manager.js")
    check("tool-manager.js reads `commands` from the dependents answer", "commands = (res && res.commands) || [];" in tm)
    check("...lists them in the confirmation", "mLines.length" in tm and "These palette commands go off with it" in tm)
    check("...and a tool with only commands to list now asks (no longer silent)", "|| cLines.length || mLines.length)" in tm)
    check("commandLines is exposed for tests", "cascadeLines, commandLines," in tm)
    check("only the tool and saved-command kinds ask (an MCP / persona / screen switch is unchanged)",
          'if (!available && (kind === "tool" || kind === "command")) {' in tm)
    check("the route passes the CLI's answer through (no per-key filtering)", "relayJarvisJson(res, [\"disabled-dependents\", kind, name]" in src("web/server.js"))


test_function()
test_cli()
test_static()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
