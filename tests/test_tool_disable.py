"""Tool Manager (L.25) -- switching a tool or saved command OFF.

"Disabled" means the MODEL cannot see it and cannot use it (owner, 2026-10-03).
The owner still can (Debug, `jarvis <command>`). Anything automatic -- the
model's executor, scheduled jobs -- is refused, and says so.

Covers:
  1. tool_disable store: round trip, protection (enforced on read too), bad
     names, torn/corrupt files, a failed save is reported not swallowed.
  2. Hidden from the model: session schemas, schemas_for_tools, search_tools,
     get_tool_schema, did_you_mean, the discovery group list, the payload.
  3. Refused if called anyway: execute_tool (default-deny, owner=True opt-in) and
     the model's executor -- before the cache and before the confirm gate, and it
     stops an unattended run.
  4. Saved commands: hidden from search/resolve/run_command/run_chain, the
     update_command exact-name bypass is closed, the prompt block excludes them,
     a rename keeps the switch.
  5. Scheduler: a job naming a disabled command or tool fails clearly (last_error,
     notification) and spawns nothing; jobs_using() lists dependents.
  6. CLI: the four commands, exit codes, protected refusal, delete cleans up.
  7. No model-facing tool can flip a switch.

No network, no live model. Runs against a throwaway HOME.

Run: python3 tests/test_tool_disable.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis import (ai_client, atomic_io, command_tools, commands_config,  # noqa: E402
                    scheduler, tool_disable, tool_safety)
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def reset():
    for path in (tool_disable.DISABLED_FILE, tool_disable.DISABLED_FILE.with_suffix(".json.bak")):
        if path.exists():
            path.unlink()


def names(schemas):
    return [s.get("name") for s in schemas]


# a harmless tool of our own, so "it ran" is observable
RAN = []
PROBE_SCHEMA = {"name": "zz_probe", "description": "probe", "parameters": {"type": "object", "properties": {}}}
system_tools.TOOLS["zz_probe"] = lambda args: (RAN.append(args), {"ok": True, "ran": True})[1]
system_tools.AUTO_TOOLS["zz_probe"] = system_tools.TOOLS["zz_probe"]


# --- 1. the store -------------------------------------------------------------------
reset()
check("nothing is disabled by default", not tool_disable.disabled_tools() and not tool_disable.disabled_commands())
r = tool_disable.set_tool_disabled("click", True)
check("switching a tool off is reported and visible", r == {"name": "click", "disabled": True} and tool_disable.is_tool_disabled("click"), r)
check("the file is real JSON on disk", json.loads(tool_disable.DISABLED_FILE.read_text())["tools"] == ["click"])
check("switching it on removes it", tool_disable.set_tool_disabled("click", False)["disabled"] is False and not tool_disable.is_tool_disabled("click"))
check("switching off twice is harmless", tool_disable.set_tool_disabled("click", True) and tool_disable.set_tool_disabled("click", True) and tool_disable.disabled_tools() == frozenset({"click"}))
reset()

for protected in ("search_tools", "search_commands", "get_tool_schema", "load_skill"):
    try:
        tool_disable.set_tool_disabled(protected, True)
        check("protected: %s cannot be switched off" % protected, False, "no error")
    except tool_disable.ProtectedToolError as e:
        check("protected: %s cannot be switched off" % protected, protected in str(e) and bool(tool_disable.protected_reason(protected)))
check("protected tools are not on the list after the refusals", not tool_disable.disabled_tools())
check("a protected tool can always be switched ON (clears a stale entry)", tool_disable.set_tool_disabled("search_tools", False)["disabled"] is False)
atomic_io.write_json(tool_disable.DISABLED_FILE, {"tools": ["search_tools", "click"], "commands": []})
check("a hand-edited file naming a protected tool does not disable it (enforced on READ)",
      tool_disable.disabled_tools() == frozenset({"click"}) and not tool_disable.is_tool_disabled("search_tools"))
check("PROTECTED_TOOLS is exactly the discovery set the code hard-wires",
      set(tool_disable.PROTECTED_TOOLS) == {"search_tools", "search_commands", "get_tool_schema", "load_skill"})
from jarvis import ai_providers  # noqa: E402
check("…and covers ai_providers.DISCOVERY_TOOL_NAMES", ai_providers.DISCOVERY_TOOL_NAMES <= set(tool_disable.PROTECTED_TOOLS))
reset()

for bad in ("", "Bad Name", "1x", "a" * 70, None):
    try:
        tool_disable.set_tool_disabled(bad, True)
        check("bad tool name %r is rejected" % (bad,), False)
    except ValueError:
        check("bad tool name %r is rejected" % (bad,), True)
try:
    tool_disable.set_command_disabled("has space", True)
    check("a command name with a space is rejected", False)
except ValueError:
    check("a command name with a space is rejected", True)

# a torn write falls back to the .bak instead of reading as "nothing is disabled"
tool_disable.set_tool_disabled("click", True)
tool_disable.set_tool_disabled("hotkey", True)             # second write leaves the first as .bak
tool_disable.DISABLED_FILE.write_text("{ torn", encoding="utf-8")
check("a torn disabled.json falls back to the .bak (not to 'everything on')",
      tool_disable.disabled_tools() == frozenset({"click"}), sorted(tool_disable.disabled_tools()))
reset()
tool_disable.DISABLED_FILE.write_text("[1, 2]", encoding="utf-8")
check("a wrong-shaped file reads as empty rather than raising", tool_disable.disabled_tools() == frozenset())
reset()

# a failed save must be an error, never a silent "it worked"
_real_write = atomic_io.write_json
atomic_io.write_json = lambda *a, **k: False
try:
    tool_disable.set_tool_disabled("click", True)
    check("a save that fails raises instead of pretending", False, "no error")
except OSError:
    check("a save that fails raises instead of pretending", True)
finally:
    atomic_io.write_json = _real_write
check("…and nothing was recorded", not tool_disable.is_tool_disabled("click"))
reset()

# --- 2. hidden from the model ----------------------------------------------------------
tool_disable.set_tool_disabled("click", True)
check("session schemas omit it", "click" not in names(system_tools.tool_schemas_for_session()))
check("session schemas still offer its siblings", "hotkey" in names(system_tools.tool_schemas_for_session()))
check("schemas_for_tools drops it and keeps order of the rest",
      names(system_tools.schemas_for_tools(["click", "hotkey"])) == ["hotkey"])
check("schemas_for_tools == tool_schemas_for_session's names (the existing invariant)",
      names(system_tools.schemas_for_tools(names(system_tools.tool_schemas_for_session()))) == names(system_tools.tool_schemas_for_session()))
hits = [m.get("name") for m in (system_tools.tool_search_tools({"query": "click"}).get("matches") or [])]
check("search_tools doesn't return it, even for its own name", "click" not in hits and "browser_click" in hits, hits)
group = system_tools.tool_search_tools({"query": "desktop"})
check("search_tools by group omits it", "click" not in [m.get("name") for m in (group.get("matches") or [])], group)
gs = system_tools.tool_get_tool_schema({"name": "click"})
check("get_tool_schema refuses it, by name", gs.get("disabled") is True and "schema" not in gs, gs)
check("the unknown-tool hint never suggests it", "click" not in (system_tools._unknown_tool_hint("clic").get("did_you_mean") or []))
check("execute_tool's unknown-name path never suggests it either",
      "click" not in (system_tools.execute_tool("clickk").get("did_you_mean") or []))
pay = {i["name"]: i for i in system_tools.tools_list_payload()}
check("the Tool Manager payload still LISTS it, marked disabled", pay["click"]["disabled"] is True)
check("…and an ordinary tool is not marked", pay["hotkey"]["disabled"] is False and pay["hotkey"]["protected"] == "")
check("the payload explains a protected tool", bool(pay["search_tools"]["protected"]) and pay["search_tools"]["disabled"] is False)
check("the discovery group list is unaffected while other tools in a group are on",
      "desktop" in system_tools.tool_search_tools({"query": ""})["groups"])
# every tool in a group off -> the group is not offered
members = list(system_tools.tool_registry.TOOL_GROUPS["desktop"]) if hasattr(system_tools, "tool_registry") else None
from jarvis import tool_registry  # noqa: E402
members = list(tool_registry.TOOL_GROUPS["desktop"])
for m in members:
    if m not in tool_disable.PROTECTED_TOOLS:
        tool_disable.set_tool_disabled(m, True)
check("a group with every tool switched off is not offered", "desktop" not in system_tools.tool_search_tools({"query": ""})["groups"])
reset()

# --- 3. refused if called anyway ----------------------------------------------------------
tool_disable.set_tool_disabled("zz_probe", True)
del RAN[:]
r = system_tools.execute_tool("zz_probe", {})
check("execute_tool refuses a disabled tool by default", r.get("disabled") is True and r.get("blocked") is True, r)
check("…and the handler never ran", RAN == [])
check("the refusal tells the model not to work around it", "same effect" in r["hint"])
r = system_tools.execute_tool("zz_probe", {}, owner=True)
check("owner=True (the Debug panel) still runs it", r.get("ran") is True and RAN == [{}], r)
tool_disable.set_tool_disabled("zz_probe", False)
check("switched back on, it runs for everyone again", system_tools.execute_tool("zz_probe", {}).get("ran") is True)
system_tools.TOOLS["zz_probe"]({})                                  # (keep RAN a plain list)
check("the Debug panel's code path passes owner=True",
      "execute_tool(tool_name, arguments, verbosity=verbosity, owner=True)" in (CLI_DIR / "jarvis" / "cli.py").read_text(encoding="utf-8"))
check("the scheduler's tool path does NOT pass owner (so it is refused)",
      "execute_tool(name, args)" in (CLI_DIR / "jarvis" / "scheduler.py").read_text(encoding="utf-8"))

# the model's executor
tool_disable.set_tool_disabled("zz_probe", True)
asked = []
executor = ai_client._make_tool_executor(None, schemas=[PROBE_SCHEMA], on_confirm_request=lambda *a: asked.append(a) or True)
del RAN[:]
r = executor("zz_probe", {})
check("the model's executor refuses a disabled tool", r.get("disabled") is True and RAN == [], r)
tool_disable.set_tool_disabled("zz_probe", False)
r = executor("zz_probe", {})
check("…and doesn't cache the refusal (switch it on and the next call runs)", r.get("ran") is True, r)
tool_disable.set_tool_disabled("zz_probe", True)
r = executor("zz_probe", {})
check("a result cached BEFORE it was switched off is not served afterwards", r.get("disabled") is True, r)
reset()

# before the confirm gate: a disabled, confirm-required tool must not even ask
tool_disable.set_tool_disabled("click", True)
asked = []
ex2 = ai_client._make_tool_executor(None, schemas=system_tools.schemas_for_tools(["hotkey"]) or [],
                                    on_confirm_request=lambda *a: asked.append(a) or True)
r = ex2("click", {"x": 1, "y": 1})
check("a disabled confirm-required tool is refused WITHOUT a confirmation prompt", r.get("disabled") is True and asked == [], (r, asked))
reset()

# unattended: stops the run instead of improvising
os.environ["JARVIS_SCHEDULED"] = "1"
try:
    tool_disable.set_tool_disabled("zz_probe", True)
    ex3 = ai_client._make_tool_executor(None, schemas=[PROBE_SCHEMA])
    r1 = ex3("zz_probe", {})
    check("unattended: the call is refused", r1.get("disabled") is True, r1)
    tool_disable.set_tool_disabled("zz_probe", False)
    del RAN[:]
    r2 = ex3("zz_probe", {"again": 1})
    check("unattended: the whole run is stopped (breaker), so it can't improvise around the switch",
          r2.get("blocked") is True and "Stopped" in (r2.get("error") or "") and RAN == [], r2)
finally:
    os.environ.pop("JARVIS_SCHEDULED", None)
    reset()
ex4 = ai_client._make_tool_executor(None, schemas=[PROBE_SCHEMA])
tool_disable.set_tool_disabled("zz_probe", True)
ex4("zz_probe", {})
tool_disable.set_tool_disabled("zz_probe", False)
check("interactive: a refusal does NOT stop the run (a person is there)", ex4("zz_probe", {"x": 2}).get("ran") is True)
reset()

# --- 4. saved commands --------------------------------------------------------------------------
commands_config.ensure_config()
CMDS = {
    "deploy-prod": {"description": "Ship it", "run": "echo deploy", "confirm_required": True},
    "list-files": {"description": "List files", "run": "echo files"},
    "nightly": {"description": "Nightly job", "run": "echo night"},
}
commands_config.save_commands_dict(CMDS)
check("baseline: search_commands lists all three", len(command_tools.tool_search_commands({"query": ""})["matches"]) == 3)
tool_disable.set_command_disabled("deploy-prod", True)
listed = [m["name"] for m in command_tools.tool_search_commands({"query": ""})["matches"]]
check("a disabled command is not listed", listed == ["list-files", "nightly"], listed)
r = command_tools.tool_search_commands({"query": "deploy"})
check("…nor found by name or by description", not r["matches"] and "deploy-prod" not in json.dumps(r), r)
r = command_tools.tool_search_commands({"query": "ship"})
check("…nor found by a word from its description", not r["matches"], r)
r = command_tools.tool_run_command({"name": "deploy-prod"})
check("run_command refuses it by exact name", r.get("disabled") is True and r.get("blocked") is True, r)
r = command_tools.tool_run_command({"name": "DEPLOY-PROD"})
check("…and by a different case", r.get("disabled") is True, r)
r = command_tools.tool_run_command({"name": "deploy"})
check("a partial name does NOT guess into it", "deploy-prod" not in json.dumps(r) and not r.get("ok"), r)
r = command_tools.tool_run_command({"name": "nonexistent"})
check("'available' in an error never lists it", "deploy-prod" not in json.dumps(r.get("available") or []), r)
r = command_tools.tool_run_chain({"segments": [{"name": "list-files"}, {"name": "deploy-prod"}]})
check("run_chain refuses a chain containing it, before running anything",
      r.get("disabled") is True and not r.get("results"), r)
before = json.dumps(commands_config.load_commands_dict()["deploy-prod"], sort_keys=True)
r = command_tools.tool_update_command({"name": "deploy-prod", "description": "pwned", "run": "echo evil", "confirm_required": False})
check("update_command by EXACT name is refused (the model can't rewrite a switched-off command)",
      r.get("disabled") is True, r)
check("…and the command is unchanged on disk", json.dumps(commands_config.load_commands_dict()["deploy-prod"], sort_keys=True) == before)
ctx = ai_client._commands_context(tool_disable.visible_commands(commands_config.load_commands_dict()))
check("the system prompt's 'Saved commands' block omits it", "deploy-prod" not in ctx and "list-files" in ctx, ctx)
check("visible_commands hands back the same object when nothing is off",
      (lambda d: tool_disable.visible_commands(d) is d)({"a": {}}) if not tool_disable.disabled_commands() else True)
ask_src = (CLI_DIR / "jarvis" / "ai_client.py").read_text(encoding="utf-8")
_body = ask_src[ask_src.index("def _ask_impl("):]
_filter_at = _body.find("commands = tool_disable.visible_commands(commands)")
_first_read = _body.find("cfg = ai_config.load_ai_config()")
check("_ask_impl filters the commands dict before anything reads it (it precedes the first real statement)",
      0 < _filter_at < _first_read and _body[_filter_at:_first_read].count("\n") < 4, (_filter_at, _first_read))
check("the owner's own run path is not gated (resolve_and_run has no disable check)",
      "tool_disable" not in re.search(r"def resolve_and_run[\s\S]*?\n\n\n", (CLI_DIR / "jarvis" / "cli.py").read_text(encoding="utf-8")).group(0))
tool_disable.rename_command("deploy-prod", "ship-it")
check("a RENAMED command keeps its switch (no re-enabling by renaming)",
      tool_disable.is_command_disabled("ship-it") and not tool_disable.is_command_disabled("deploy-prod"), sorted(tool_disable.disabled_commands()))
tool_disable.rename_command("list-files", "ls")
check("renaming a command that was ON leaves both names on", not tool_disable.disabled_commands() - {"ship-it"})
tool_disable.forget_command("ship-it")
check("forget drops the switch (a deleted command's name starts fresh)", not tool_disable.disabled_commands())
reset()

# --- 5. scheduler ---------------------------------------------------------------------------------
commands_config.save_commands_dict(CMDS)
notes = []
from jarvis import notifier  # noqa: E402
_real_notify = notifier.notify
notifier.notify = lambda **kw: notes.append(kw) or {"id": "n1"}
_real_run = scheduler.subprocess.run
scheduler.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a disabled command was spawned"))
try:
    tool_disable.set_command_disabled("nightly", True)
    job = scheduler.create(kind="task", title="nightly run", when="in 1 hour",
                           action={"type": "command", "command": "nightly"}, trusted=True)
    out = scheduler._do_command(job, job["action"])
    check("a job naming a disabled command fails clearly", out["ok"] is False and "switched off" in out["error"], out)
    check("…without spawning anything", True)          # the patched subprocess.run would have raised
    check("…and tells the owner (failed notification)", notes and notes[-1].get("failed") is True and "nightly" in notes[-1]["message"], notes)
    outcome = scheduler._execute_job(job) if hasattr(scheduler, "_execute_job") else scheduler._run_action(job)
    check("via the job runner too: the outcome is a failure with the reason", outcome and outcome.get("ok") is False and "switched off" in (outcome.get("error") or ""), outcome)
    tool_disable.set_command_disabled("nightly", False)
finally:
    scheduler.subprocess.run = _real_run
    notifier.notify = _real_notify

tool_disable.set_tool_disabled("zz_probe", True)
tjob = scheduler.create(kind="task", title="probe it", when="in 1 hour", action={"type": "tool", "tool": "zz_probe", "args": {}}, trusted=True)
del RAN[:]
notifier.notify = lambda **kw: notes.append(kw) or {"id": "n2"}
try:
    out = scheduler._do_tool(tjob, tjob["action"])
finally:
    notifier.notify = _real_notify
check("a scheduled TOOL action on a disabled tool fails clearly and does not run",
      out["ok"] is False and "switched off" in (out["error"] or "") and RAN == [], out)
deps = scheduler.jobs_using("tool", "zz_probe")
check("jobs_using lists the job that names a tool", [d["id"] for d in deps] == [tjob["id"]], deps)
check("…and the one that names a command", [d["id"] for d in scheduler.jobs_using("command", "nightly")] == [job["id"]])
check("…and nothing for an unused name or a bad kind", scheduler.jobs_using("tool", "never_used") == [] and scheduler.jobs_using("nope", "x") == [] and scheduler.jobs_using("tool", "") == [])
reset()


# --- 6. the CLI ------------------------------------------------------------------------------------
def cli(*argv):
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "jarvis", *argv], cwd=str(CLI_DIR), env=env, capture_output=True, text=True, timeout=120)
    try:
        return p.returncode, json.loads(p.stdout)
    except ValueError:
        return p.returncode, {"_raw": p.stdout[-200:], "_err": p.stderr[-200:]}


code, out = cli("tool-disable-set", "click", "true")
check("CLI: tool-disable-set switches a tool off", code == 0 and out.get("disabled") is True, out)
code, out = cli("disabled-list")
check("CLI: disabled-list shows it and the protected map", code == 0 and "click" in out["tools"] and "search_tools" in out["protected"], out)
code, out = cli("tools-list")
row = next((i for i in out if i["name"] == "click"), {})
check("CLI: tools-list reports disabled", code == 0 and row.get("disabled") is True and row.get("protected") == "", row)
code, out = cli("tool-disable-set", "search_tools", "true")
check("CLI: a protected tool is refused with exit 1 and protected=true", code == 1 and out.get("protected") is True, (code, out))
code, out = cli("tool-disable-set", "no_such_tool_xyz", "true")
check("CLI: an unknown tool is refused", code == 1 and "no such tool" in out.get("error", ""), (code, out))
code, out = cli("tool-disable-set", "click", "false")
check("CLI: switching back on works", code == 0 and out.get("disabled") is False, out)
code, out = cli("tool-disable-set", "click")
check("CLI: missing argument gives usage and exit 1", code == 1 and "usage" in out.get("error", ""), out)

code, out = cli("command-disable-set", "nightly", "true")
check("CLI: command-disable-set switches a saved command off", code == 0 and out.get("disabled") is True, out)
code, out = cli("command-disable-set", "ghost-command", "true")
check("CLI: switching off a command that doesn't exist is refused", code == 1 and "no saved command" in out.get("error", ""), (code, out))
code, out = cli("command-disable-set", "nightly", "rename", "nightly2")
check("CLI: rename moves the switch", code == 0 and out.get("disabled") is True and tool_disable.is_command_disabled("nightly2"), out)
code, out = cli("command-disable-set", "nightly2", "forget")
check("CLI: forget drops it (even though the command doesn't exist)", code == 0 and not tool_disable.disabled_commands(), out)
code, out = cli("disabled-dependents", "tool", "zz_probe")
check("CLI: disabled-dependents returns jobs", code == 0 and isinstance(out.get("jobs"), list) and out["jobs"][0]["id"] == tjob["id"], out)
code, out = cli("disabled-dependents", "bogus", "x")
check("CLI: disabled-dependents rejects a bad kind", code == 1, (code, out))
for reserved in ("tool-disable-set", "command-disable-set", "disabled-list", "disabled-dependents"):
    check("%s is a reserved name (a saved command can't shadow it)" % reserved,
          reserved in commands_config.RESERVED_NAMES)
reset()

# deleting a user tool file drops its switch
from jarvis import custom_tools_store as cts  # noqa: E402
cts.write_tool("probe_file_tool", cts.template_source("minimal"))
user_tools = [t for t in cts.list_tools() if t["name"] == "probe_file_tool"][0]["tools"]
tool_disable.set_tool_disabled(user_tools[0], True)
cts.delete_tool("probe_file_tool")
check("deleting a user tool file drops its switch", not tool_disable.is_tool_disabled(user_tools[0]), user_tools)
reset()

# --- 7. the model cannot flip a switch --------------------------------------------------------------
WRITERS = r"tool_disable\.(set_tool_disabled|set_command_disabled|rename_command|forget_tool|forget_command|_set|_write|DISABLED_FILE)|disabled\.json"
offenders = set()
for tool_name, fn in system_tools.TOOLS.items():
    try:
        text = Path(sys.modules[fn.__module__].__file__).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        continue
    if fn.__module__ in ("__main__", "jarvis.tool_disable"):
        continue                      # this file's own probe tool / the module itself
    if re.search(WRITERS, text):
        offenders.add("%s (%s)" % (tool_name, fn.__module__))
# custom_tools_store.delete_tool legitimately forgets a deleted tool's switch; no
# MODEL-facing tool is defined in it (its tool_* functions are example templates).
offenders = {o for o in offenders if "jarvis.custom_tools_store" not in o}
check("no model-facing tool module can write the disabled list", not offenders, sorted(offenders))
check("the model-facing command tools only READ it",
      not re.search(r"tool_disable\.(set_|rename_|forget_|_set|_write)", (CLI_DIR / "jarvis" / "command_tools.py").read_text(encoding="utf-8")))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
