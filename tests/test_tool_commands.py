"""TOOL_COMMANDS -- palette commands a tool file contributes (master plan I.2.11 / K.6.7).

Covers:
  1. tool_commands.validate_commands: a good entry; every way an entry is dropped (bad
     name, reserved name, duplicate, unknown tool, no summary, bad args / fixed, a key
     that is not a parameter of the tool); the fail-closed risk tier (missing and
     unknown both become `dangerous`, and say so); caps.
  2. tool_commands.build_arguments: positional mapping, optional args, required args,
     coercion (integer / number / boolean), `rest`, too many words, `fixed` merging and
     never being overridden, NULs, and that nothing typed can add an undeclared key.
  3. tool_loader: a module's TOOL_COMMANDS is attached to its record, a bad entry never
     rejects the file, a file with no tools gets none.
  4. tools.py: a command follows ITS tool being switched off (and the whole-file rule),
     is listed with `disabled` under include_disabled, and tool_command_prepare refuses
     an off or unknown command.
  5. CLI: tool-commands list / prepare / run against a real user tool file in a
     throwaway HOME, including every refusal; run really runs the declared tool with
     the built arguments.
  6. Static: nothing model-facing can write a TOOL_COMMANDS entry (the same rule as
     TOOL_UI), the CLI name is reserved, and the server routes keep their guards.

No network, no live model. Runs against a throwaway HOME.

Run: python3 tests/test_tool_commands.py
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

# The throwaway HOME gets a real user tool file BEFORE jarvis.tools is imported, so the
# normal discovery pass finds it exactly as it would a file the owner dropped in.
TOOLS_DIR = Path(_HOME) / ".jarvis" / "tools"
TOOLS_DIR.mkdir(parents=True, exist_ok=True)
(TOOLS_DIR / "disk_demo.py").write_text(textwrap.dedent('''
    def _report(args):
        return {"ok": True, "message": "drive=%r verbose=%r count=%r" % (
            args.get("drive"), args.get("verbose"), args.get("count"))}

    def _wipe(args):
        return {"ok": True, "message": "wiped %s" % args.get("target")}

    def _other(args):
        return {"ok": True, "message": "other"}

    TOOL_SCHEMAS = [
        {"name": "disk_report", "description": "Report disk usage.",
         "parameters": {"type": "object", "properties": {
             "drive": {"type": "string"}, "verbose": {"type": "boolean"},
             "count": {"type": "integer"}}}},
        {"name": "disk_wipe", "description": "Wipe a thing.",
         "parameters": {"type": "object", "properties": {"target": {"type": "string"}},
                        "required": ["target"]}},
    ]
    TOOLS = {"disk_report": _report, "disk_wipe": _wipe}
    TOOL_GROUP = "demo_disk"
    TOOL_COMMANDS = [
        {"name": "disk", "tool": "disk_report", "summary": "How full is each drive",
         "riskTier": "safe", "description": "Longer hover text.", "example": "/disk C:",
         "args": [{"name": "drive", "hint": "e.g. C:"}, {"name": "count", "type": "integer"}],
         "fixed": {"verbose": False}},
        {"name": "wipe", "tool": "disk_wipe", "summary": "Wipe a target",
         "args": [{"name": "target", "required": True, "rest": True}]},
        {"name": "doctor", "tool": "disk_report", "summary": "clashes with a CLI name", "riskTier": "safe"},
        {"name": "bad", "tool": "nope", "summary": "unknown tool", "riskTier": "safe"},
        {"name": "typo", "tool": "disk_report", "summary": "bad param", "riskTier": "safe",
         "args": [{"name": "nope"}]},
    ]
'''), encoding="utf-8")
# A second file with no tools: its TOOL_COMMANDS must be refused, and a persona-only
# file is still a valid, tool-less module.
(TOOLS_DIR / "two_tools.py").write_text(textwrap.dedent('''
    TOOL_SCHEMAS = [
        {"name": "two_a", "description": "A.", "parameters": {"type": "object", "properties": {}}},
        {"name": "two_b", "description": "B.", "parameters": {"type": "object", "properties": {}}},
    ]
    TOOLS = {"two_a": lambda a: {"ok": True, "message": "a"}, "two_b": lambda a: {"ok": True, "message": "b"}}
    TOOL_GROUP = "demo_two"
    TOOL_COMMANDS = [
        {"name": "two-a", "tool": "two_a", "summary": "Run A", "riskTier": "caution"},
        {"name": "disk", "tool": "two_b", "summary": "duplicate of the other file's", "riskTier": "safe"},
    ]
'''), encoding="utf-8")

from jarvis import tool_commands as tc  # noqa: E402
from jarvis import tool_disable, tool_loader  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def reset_disabled():
    for path in (tool_disable.DISABLED_FILE, tool_disable.DISABLED_FILE.with_suffix(".json.bak")):
        if path.exists():
            path.unlink()


def entry(**kw):
    base = {"name": "mycmd", "tool": "my_tool", "summary": "Does a thing", "riskTier": "safe"}
    base.update(kw)
    return base


TOOLS = {"my_tool", "other_tool"}
SCHEMAS = {"my_tool": {"name": "my_tool", "parameters": {"type": "object", "properties": {
    "path": {"type": "string"}, "count": {"type": "integer"}, "flag": {"type": "boolean"}}}}}


def one(raw, **kw):
    kw.setdefault("reserved", {"doctor", "ask"})
    return tc.validate_commands(raw if isinstance(raw, list) else [raw], "f.py", TOOLS, SCHEMAS, **kw)


# --- 1. validate_commands ---------------------------------------------------------

ents, errs = one(entry())
check("a minimal good entry validates with no errors", len(ents) == 1 and not errs, errs)
e = ents[0]
check("it carries name, tool, summary, tier and the file", (e["name"], e["tool"], e["riskTier"], e["file"]) == ("mycmd", "my_tool", "safe", "f.py"))
check("an explicit tier is remembered as declared", e["declaredTier"] == "safe")

ents, errs = one(entry(riskTier=None))
check("no riskTier -> dangerous (fail closed)", ents and ents[0]["riskTier"] == "dangerous", ents)
check("...and the author is told why", any("dangerous" in x and "riskTier" in x for x in errs), errs)
ents, errs = one(entry(riskTier="harmless"))
check("an unknown riskTier -> dangerous, entry kept", ents and ents[0]["riskTier"] == "dangerous" and ents[0]["declaredTier"] == "", (ents, errs))
ents, errs = one({k: v for k, v in entry().items() if k != "riskTier"})
check("a missing key behaves like None", ents and ents[0]["riskTier"] == "dangerous")
for tier in ("safe", "caution", "dangerous"):
    ents, _ = one(entry(riskTier=tier))
    check("tier %s is accepted as-is" % tier, ents and ents[0]["riskTier"] == tier)
check("'SAFE' (wrong case) is not accepted as safe", one(entry(riskTier="SAFE"))[0][0]["riskTier"] == "dangerous")

for bad in ("Disk", "d", "9disk", "has space", "a" * 33, "under_score", "", None, 5):
    ents, errs = one(entry(name=bad))
    check("name %r is dropped" % (bad,), not ents and errs, (ents, errs))
ents, errs = one(entry(name="doctor"))
check("a reserved CLI name is dropped (the built-in wins)", not ents and any("jarvis command name" in x for x in errs), errs)
ents, errs = one([entry(), entry(summary="again")])
check("a duplicate name in one file keeps the first", len(ents) == 1 and ents[0]["summary"] == "Does a thing" and any("duplicate" in x for x in errs), errs)
ents, errs = one(entry(tool="not_mine"))
check("a tool that isn't this file's is dropped", not ents and any("not one of this file's tools" in x for x in errs), errs)
ents, errs = one(entry(tool=None))
check("no tool is dropped", not ents and errs)
ents, errs = one(entry(summary="   "))
check("a blank summary is dropped", not ents and any("summary" in x for x in errs), errs)
ents, errs = one(entry(summary="x" * 500))
check("a long summary is trimmed, not dropped", ents and len(ents[0]["summary"]) == tc.MAX_SUMMARY)
ents, errs = tc.validate_commands([entry()], "f.py", set(), None, reserved=set())
check("a file with no tools can't have commands", not ents and any("defines tools" in x for x in errs), errs)
check("None means none, quietly", tc.validate_commands(None, "f.py", TOOLS) == ([], []))
check("a non-list is an error, not a crash", tc.validate_commands("nope", "f.py", TOOLS)[1])
check("a single dict is accepted as a one-entry list", len(tc.validate_commands(entry(), "f.py", TOOLS, SCHEMAS, reserved=set())[0]) == 1)
ents, errs = one([entry(name="cmd%02d" % i) for i in range(20)])
check("only the first MAX_ENTRIES_PER_FILE are used", len(ents) == tc.MAX_ENTRIES_PER_FILE and any("only the first" in x for x in errs), (len(ents), errs))
ents, errs = one(["not a dict", entry()])
check("a non-dict entry is skipped, the next one stays", len(ents) == 1 and errs)

# args / fixed
ents, errs = one(entry(args=[{"name": "path", "required": True, "hint": "a path"}, {"name": "n", "arg": "count", "type": "integer"}]))
check("args are normalised (arg defaults to name, type to string)", ents and ents[0]["args"][0]["arg"] == "path" and ents[0]["args"][0]["type"] == "string" and ents[0]["args"][1]["arg"] == "count", errs)
check("usage shows <required> and [optional]", ents[0]["usage"] == "<path> [n]", ents[0]["usage"])
for label, args in (
    ("args that isn't a list", "x"),
    ("an arg that isn't a dict", ["x"]),
    ("an arg with a bad name", [{"name": "1bad"}]),
    ("an arg with a bad target key", [{"name": "ok", "arg": "has space"}]),
    ("a duplicate arg name", [{"name": "path"}, {"name": "path"}]),
    ("two args for one parameter", [{"name": "a", "arg": "path"}, {"name": "b", "arg": "path"}]),
    ("an unknown type", [{"name": "path", "type": "object"}]),
    ("rest that isn't last", [{"name": "path", "rest": True}, {"name": "count"}]),
    ("rest that isn't a string", [{"name": "count", "rest": True, "type": "integer"}]),
    ("too many args", [{"name": "a%d" % i, "arg": "path"} for i in range(tc.MAX_ARGS + 1)]),
):
    ents, errs = one(entry(args=args))
    check("%s drops the entry" % label, not ents and errs, (ents, errs))
ents, errs = one(entry(args=[{"name": "nope"}]))
check("an arg that isn't a parameter of the tool is dropped, naming what it takes", not ents and any("count, flag, path" in x for x in errs), errs)
ents, errs = one(entry(fixed={"nope": 1}))
check("a fixed key that isn't a parameter is dropped", not ents and errs, errs)
ents, errs = one(entry(fixed={"flag": True}, args=[{"name": "flag", "type": "boolean"}]))
check("a key both fixed and typed is dropped", not ents and any("both fixed and typed" in x for x in errs), errs)
ents, errs = one(entry(fixed=["x"]))
check("fixed must be a dict", not ents and errs)
ents, errs = one(entry(fixed={"path": "x" * (tc.MAX_FIXED_CHARS + 1)}))
check("an oversize fixed is dropped", not ents and errs)
ents, errs = one(entry(fixed={"path": object()}))
check("a fixed value that isn't JSON is dropped", not ents and errs)
ents, errs = tc.validate_commands([entry(args=[{"name": "whatever"}])], "f.py", TOOLS, None, reserved=set())
check("with no schema to check against, an arg is accepted", len(ents) == 1, errs)
ents, errs = tc.validate_commands([entry(args=[{"name": "whatever"}])], "f.py", TOOLS,
                                  {"my_tool": {"parameters": {"type": "object", "properties": {}}}}, reserved=set())
check("a schema with no properties isn't used to reject", len(ents) == 1, errs)

pub = tc.public(one(entry())[0][0], disabled=True, reason="why")
check("public() carries disabled and the reason", pub["disabled"] is True and pub["disabled_reason"] == "why")

# --- 2. build_arguments -------------------------------------------------------------

def built(spec_args, words, **kw):
    ent = one(entry(args=spec_args, **kw))[0]
    assert ent, spec_args
    return tc.build_arguments(ent[0], words)


A = [{"name": "path", "required": True}, {"name": "n", "arg": "count", "type": "integer"}, {"name": "f", "arg": "flag", "type": "boolean"}]
check("typed words fill the declared keys", built(A, ["x", "3", "yes"]) == ({"path": "x", "count": 3, "flag": True}, None))
check("an optional arg may be left off", built(A, ["x"]) == ({"path": "x"}, None))
check("a required arg missing is an error naming it", built(A, [])[1] and "path" in built(A, [])[1], built(A, []))
check("a blank required arg counts as missing", built(A, ["  "])[1])
check("a hint is used in the 'needs' message", "a path" in (built([{"name": "path", "required": True, "hint": "a path"}], [])[1] or ""))
check("more words than args is an error that shows usage", "at most 3" in (built(A, ["a", "1", "no", "extra"])[1] or ""), built(A, ["a", "1", "no", "extra"]))
check("a command with no args refuses words", "no arguments" in (built([], ["x"])[1] or ""))
check("a bad integer is refused, not guessed", built(A, ["x", "three"])[1] and "whole number" in built(A, ["x", "three"])[1])
check("an integer typed as a float is refused", built(A, ["x", "3.5"])[1])
check("a bad boolean is refused", built(A, ["x", "1", "maybe"])[1])
for word, want in (("true", True), ("YES", True), ("on", True), ("1", True), ("false", False), ("No", False), ("off", False), ("0", False)):
    check("boolean %r -> %r" % (word, want), built(A, ["x", "1", word])[0]["flag"] is want)
N = [{"name": "n", "arg": "count", "type": "number"}]
check("number accepts a float", built(N, ["2.5"]) == ({"count": 2.5}, None))
check("number refuses nan", built(N, ["nan"])[1])
check("number refuses inf", built(N, ["inf"])[1])
R = [{"name": "a", "arg": "flag"}, {"name": "target", "arg": "path", "rest": True}]
check("rest takes the remaining words joined by single spaces", built(R, ["one", "two", "three", "four"]) == ({"flag": "one", "path": "two three four"}, None))
check("rest with exactly its own word is that word", built(R, ["one", "two"]) == ({"flag": "one", "path": "two"}, None))
check("a rest arg may be empty if optional", built(R, ["one"]) == ({"flag": "one"}, None))
check("a word that looks like a flag is just a value", built(R, ["--evil", "--x=1"]) == ({"flag": "--evil", "path": "--x=1"}, None))
check("fixed is merged in", built([{"name": "path"}], ["p"], fixed={"flag": True}) == ({"path": "p", "flag": True}, None))
check("fixed applies even with no words", built([], [], fixed={"flag": True}) == ({"flag": True}, None))
check("a NUL in a word is refused", built([{"name": "path"}], ["a\x00b"])[1])
check("an over-long word is refused", built([{"name": "path"}], ["x" * (tc.MAX_ARG_CHARS + 1)])[1])
check("non-string words are refused", tc.build_arguments(one(entry())[0][0], [1, 2])[1])
check("a non-list is refused", tc.build_arguments(one(entry())[0][0], "x y")[1])
out, _ = built([{"name": "path"}], ["p"])
check("only declared keys can appear, whatever was typed", set(out) == {"path"})
out, _ = built([{"name": "path"}], ["p"], fixed={"flag": False})
check("a typed value never overrides a fixed one (they can't share a key)", out == {"path": "p", "flag": False})

# --- 3. the loader ---------------------------------------------------------------------

class Mod:
    pass


def make_module(**attrs):
    m = Mod()
    m.TOOL_SCHEMAS = [{"name": "my_tool", "description": "d", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    m.TOOLS = {"my_tool": lambda a: {"ok": True}}
    m.TOOL_GROUP = "g"
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


logs = []
rec = tool_loader._validate(make_module(TOOL_COMMANDS=[
    {"name": "good-one", "tool": "my_tool", "summary": "ok", "riskTier": "safe"},
    {"name": "bad one", "tool": "my_tool", "summary": "ok", "riskTier": "safe"}]), "m.py", logs.append)
check("loader: the record carries the valid command", rec.valid and [c["name"] for c in rec.commands] == ["good-one"], rec)
check("loader: the bad one is logged, not fatal", any("[tool-commands]" in l for l in logs), logs)
rec = tool_loader._validate(make_module(), "m.py", logs.append)
check("loader: no TOOL_COMMANDS -> []", rec.valid and rec.commands == [])
rec = tool_loader._validate(make_module(TOOL_COMMANDS="garbage"), "m.py", logs.append)
check("loader: a garbage TOOL_COMMANDS never rejects the file", rec.valid and rec.commands == [])
rec = tool_loader._validate(make_module(TOOL_COMMANDS=[{"name": "x1", "tool": "my_tool", "summary": "s", "args": [{"name": "zzz"}]}]), "m.py", logs.append)
check("loader: an arg that isn't in the tool's schema is dropped", rec.valid and rec.commands == [])

# --- 4. tools.py: aggregation and the follow-the-tool rule -----------------------------

names = [c["name"] for c in system_tools.AUTO_COMMANDS]
check("AUTO_COMMANDS has the good commands from both files", {"disk", "wipe", "two-a"} <= set(names), names)
check("a CLI-reserved name never got in", "doctor" not in names)
check("a command with an unknown tool never got in", "bad" not in names)
check("a command with a bad parameter never got in", "typo" not in names)
check("the first file wins a duplicate name", names.count("disk") == 1 and next(c for c in system_tools.AUTO_COMMANDS if c["name"] == "disk")["file"] == "disk_demo.py")
check("wipe, declaring no tier, is dangerous", next(c for c in system_tools.AUTO_COMMANDS if c["name"] == "wipe")["riskTier"] == "dangerous")

reset_disabled()
listing = system_tools.tool_commands_payload()["commands"]
check("payload lists them, none disabled", {"disk", "wipe", "two-a"} <= {c["name"] for c in listing} and not any(c["disabled"] for c in listing))
tool_disable.set_tool_disabled("disk_wipe", True)
listing = system_tools.tool_commands_payload()["commands"]
check("switching a command's tool off removes the command from the default list", "wipe" not in {c["name"] for c in listing} and "disk" in {c["name"] for c in listing})
full = {c["name"]: c for c in system_tools.tool_commands_payload(include_disabled=True)["commands"]}
check("...and include_disabled keeps it, flagged, with the reason", full["wipe"]["disabled"] and "disk_wipe" in full["wipe"]["disabled_reason"], full["wipe"])
ent, tool, args, err = system_tools.tool_command_prepare("wipe", ["x"])
check("prepare refuses a command whose tool is off", ent is None and err and "off" in err, err)
tool_disable.set_tool_disabled("disk_wipe", False)
ent, tool, args, err = system_tools.tool_command_prepare("wipe", ["a", "b"])
check("...and works again the moment the tool is back on (nothing stored)", err is None and tool == "disk_wipe" and args == {"target": "a b"}, (err, args))
tool_disable.set_tool_disabled("two_a", True)
check("one tool of several off leaves a command on the other on", "two-a" not in {c["name"] for c in system_tools.tool_commands_payload()["commands"]}
      and system_tools.tool_command_prepare("disk", [])[3] is None)
tool_disable.set_tool_disabled("two_b", True)
check("a file whose tools are ALL off takes its commands with it", system_tools.command_off_reason(next(c for c in system_tools.AUTO_COMMANDS if c["name"] == "two-a")) != "")
reset_disabled()
check("prepare refuses an unknown name", system_tools.tool_command_prepare("nope", [])[3] and "no palette command" in system_tools.tool_command_prepare("nope", [])[3])
check("prepare refuses words that don't fit", system_tools.tool_command_prepare("disk", ["a", "1", "z"])[3])
check("prepare returns the declared tool, never one typed", system_tools.tool_command_prepare("disk", ["C:"])[1] == "disk_report")
check("prepare merges fixed", system_tools.tool_command_prepare("disk", ["C:"])[2] == {"drive": "C:", "verbose": False})

# --- 5. CLI ------------------------------------------------------------------------------

def cli(*args):
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=dict(os.environ),
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def jload(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


rc, out, err = cli("tool-commands", "list")
listing = jload(out)
check("tool-commands list prints a commands list", rc == 0 and {"disk", "wipe", "two-a"} <= {c["name"] for c in listing.get("commands", [])}, (rc, out[:200], err[-300:]))
check("list entries have no absolute paths", "_dir" not in out and _HOME not in out)
check("the discovery problems went to stderr, not stdout", "[tool-commands]" in err and "[tool-commands]" not in out)
check("stderr names the missing riskTier", "no riskTier declared" in err and "wipe" in err, err[-400:])
rc, out, err = cli("tool-commands", "prepare", "disk", '["C:","3"]')
check("prepare builds the arguments", rc == 0 and jload(out).get("arguments") == {"drive": "C:", "count": 3, "verbose": False} and jload(out).get("tool") == "disk_report" and jload(out).get("riskTier") == "safe", (rc, out))
rc, out, err = cli("tool-commands", "prepare", "wipe", '["x"]')
check("prepare reports the fail-closed tier", rc == 0 and jload(out).get("riskTier") == "dangerous", out)
rc, out, err = cli("tool-commands", "prepare", "disk", '["C:","x"]')
check("prepare refuses a bad integer (exit 1, error JSON)", rc == 1 and "whole number" in jload(out).get("error", ""), (rc, out))
rc, out, err = cli("tool-commands", "run", "disk", '["C:"]')
res = jload(out)
check("run really runs the declared tool with the built arguments", rc == 0 and res.get("ok") is True and "drive='C:'" in res.get("message", "") and "verbose=False" in res.get("message", ""), (rc, out, err[-200:]))
rc, out, err = cli("tool-commands", "run", "wipe", '["a","b","--c"]')
check("run: a rest arg with a flag-looking word is one value", rc == 0 and "wiped a b --c" in out, (rc, out))
rc, out, err = cli("tool-commands", "run", "wipe", "[]")
check("run: a missing required arg is refused and nothing runs", rc == 1 and "needs" in jload(out).get("error", "") and "wiped" not in out, (rc, out))
rc, out, err = cli("tool-commands", "run", "disk", '["a","1","z"]')
check("run: too many words is refused", rc == 1 and "at most 2" in jload(out).get("error", ""), (rc, out))
rc, out, err = cli("tool-commands", "run", "nope", "[]")
check("run: an unknown command is refused", rc == 1 and "no palette command" in jload(out).get("error", ""), (rc, out))
rc, out, err = cli("tool-commands", "run", "disk_report", "[]")
check("run: a TOOL name is not a command name", rc == 1, (rc, out))
rc, out, err = cli("tool-commands", "run", "disk", "not json")
check("run: the words must be JSON", rc == 1 and "JSON array" in jload(out).get("error", ""), (rc, out))
rc, out, err = cli("tool-commands", "run", "disk", '{"drive":"C:"}')
check("run: an object instead of an array is refused", rc == 1, (rc, out))
rc, out, err = cli("tool-commands", "run", "disk", '["a"]', "extra-ignored")
check("run: trailing junk argv can't smuggle arguments", rc == 0 and "drive='a'" in out and "extra" not in out, (rc, out))
rc, out, err = cli("tool-disable-set", "disk_wipe", "true")
rc, out, err = cli("tool-commands", "run", "wipe", '["x"]')
check("run: refused while its tool is switched off", rc == 1 and "off" in jload(out).get("error", ""), (rc, out))
rc, out, err = cli("tool-commands", "list")
check("list: ...and it is gone from the default list", "wipe" not in {c["name"] for c in jload(out).get("commands", [])})
rc, out, err = cli("tool-commands", "list", "--all")
check("list --all: ...but kept, flagged disabled", any(c["name"] == "wipe" and c["disabled"] for c in jload(out).get("commands", [])), out[:200])
cli("tool-disable-set", "disk_wipe", "false")
rc, out, err = cli("tool-commands", "bogus")
check("an unknown sub-command prints usage", rc == 1 and "usage" in jload(out).get("error", ""), (rc, out))

# --- 6. static rules ---------------------------------------------------------------------

from jarvis import reserved_names  # noqa: E402
check("tool-commands is a reserved CLI name", "tool-commands" in reserved_names.RESERVED_NAMES)
src = (CLI_DIR / "jarvis")
writers = []
for path in src.rglob("*.py"):
    if path.name in ("tool_commands.py", "tool_loader.py", "custom_tools_store.py", "tools.py", "cli.py", "_template.py", "reserved_names.py",
                     "tool_disable.py"):    # tool_disable.py: the owner-only switch store (2026-10-11j) -- it names the
        continue                            # list but has no model-facing tool; test_tool_disable.py pins that
    if "TOOL_COMMANDS" in path.read_text(encoding="utf-8", errors="replace"):
        writers.append(path.name)
check("no other module so much as names TOOL_COMMANDS (nothing model-facing writes one)", not writers, writers)
check("the tool-commands module imports nothing heavy at module level",
      not re.search(r"^(?:from \.\s+import|from \. import|import)\s+(?:ai_client|tool_router|tool_registry|tools)\b",
                    (src / "tool_commands.py").read_text(encoding="utf-8"), re.M))
server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
for route in ("/api/tool-commands\"", "/api/tool-commands/:name/prepare", "/api/tool-commands/:name/run"):
    check("server.js has %s" % route.strip('"'), route in server)
check("server.js validates the command name on both POSTs", server.count("TOOL_COMMAND_NAME_RE.test(req.params.name)") == 2)
check("server.js never builds a shell string for it", "spawn(" not in server[server.index("// Tool commands (TOOL_COMMANDS"):server.index("// Custom tools — thin proxy")])
check("the listing fails closed if the registry can't be read", "if (!SLASH_BUILTIN_WORDS) return res.json({ commands: [], dropped: [] })" in server)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
