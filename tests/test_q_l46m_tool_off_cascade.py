"""Q-L46m -- switching a tool off in the Tool Manager takes its file's daemons and screens with it.

The rule under test: when EVERY tool a file defines is switched off, the daemons
(DAEMONS) and screens (TOOL_UI) the same file ships go off too. One tool of
several off alone changes nothing. Screens follow it live (derived, nothing
stored); daemons are applied by tool_daemons.sync() on the CHANGE, so an owner's
later choice is not undone. A daemon that is running keeps running.

Covers:
  1. off_owners(): all-off vs some-off, no-tools files, invalid records.
  2. sync(): off, restore, idempotence, the owner's own "off" surviving a round
     trip, the owner switching it on by hand while the tool is off, a daemon
     registered while its tool is already off, a file that stops loading and comes
     back while still off, a running daemon never being stopped by the cascade.
  3. The fingerprint changes when the switches change (so the sync re-runs).
  4. tools.py: files_with_all_tools_off, ui_ids_off_with_tool, ui_is_off,
     switch_off_cascade (last tool lists screens + daemons; not-last, protected,
     already-off and unknown names list nothing), tool_ui_payload flags.
  5. CLI: disabled-list carries ui_by_tool; disabled-dependents carries cascade.

No network, no live model. Runs against a throwaway HOME.

Run: python3 tests/test_q_l46m_tool_off_cascade.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-q-l46m-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import daemons, tool_daemons, tool_disable  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

REG = Path(HOME) / ".jarvis" / "daemons.json"
STATE = Path(HOME) / ".jarvis" / "daemons_sync.json"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def reset():
    for f in (REG, STATE):
        if f.exists():
            f.unlink()
    shutil.rmtree(Path(HOME) / ".jarvis" / "daemons", ignore_errors=True)


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "jarvis-cli"), HOME=HOME, USERPROFILE=HOME)
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True, env=env, timeout=120)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout, "err": proc.stderr}


def daemon_spec(did="worker", file="cas_tool.py", **over):
    entries, errors = tool_daemons.validate_daemons(
        [dict({"id": did, "command": ["{python}", "{tool_dir}/w.py"]}, **over)], file, Path(HOME))
    assert entries, errors
    return entries[0]


def record(file="cas_tool.py", tools=("cas_a",), entries=None, valid=True):
    return types.SimpleNamespace(valid=valid, file=file, tools={t: object() for t in tools}, daemons=entries or [])


def entry(did="cas_tool-worker"):
    return daemons._load_registry().get(did) or {}


# --- 1. off_owners ------------------------------------------------------------

def test_off_owners():
    two = record(tools=("cas_a", "cas_b"))
    check("some of a file's tools off: not all-off", tool_daemons.off_owners([two], {"cas_a"}) == set())
    check("every tool of the file off: its owner id is returned", tool_daemons.off_owners([two], {"cas_a", "cas_b"}) == {"cas_tool"})
    check("an unrelated off tool changes nothing", tool_daemons.off_owners([two], {"zzz"}) == set())
    check("a file with no tools has no switch to follow", tool_daemons.off_owners([record(tools=())], {"cas_a"}) == set())
    check("an invalid record never counts", tool_daemons.off_owners([record(valid=False)], {"cas_a"}) == set())
    check("nothing to look at gives an empty set", tool_daemons.off_owners([], {"cas_a"}) == set() and tool_daemons.off_owners(None, {"x"}) == set())


# --- 2. sync -------------------------------------------------------------------

def test_sync_cascade():
    reset()
    rec = record(entries=[daemon_spec()])
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    check("registered on, as before", entry().get("enabled") is True and not entry().get("dormant"))

    r = tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    e = entry()
    check("tool off: the daemon is switched off and marked dormant for the tool", e.get("enabled") is False and e.get("dormant") is True and e.get("dormant_reason") == "tool-off")
    check("the sync reports it under tool_off", r["tool_off"] == ["cas_tool-worker"] and r["tool_on"] == [])
    check("and remembers the old enabled value", e.get("enabled_before_dormant") is True)

    r = tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    check("a second sync with the tool still off changes nothing", r["tool_off"] == [] and r["tool_on"] == [] and entry().get("dormant") is True)

    r = tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    e = entry()
    check("tool back on: enabled restored, dormant markers gone", e.get("enabled") is True and "dormant" not in e and "dormant_reason" not in e and "tool_off_seen" not in e)
    check("the sync reports it under tool_on", r["tool_on"] == ["cas_tool-worker"])


def test_owner_choices_survive():
    reset()
    rec = record(entries=[daemon_spec()])
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    reg = daemons._load_registry()
    reg["cas_tool-worker"]["enabled"] = False            # the owner switched it off by hand
    daemons._save_registry(reg)
    tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    check("a daemon the owner had switched off stays off after the tool comes back", entry().get("enabled") is False and not entry().get("dormant"))

    reset()
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    reg = daemons._load_registry()
    reg["cas_tool-worker"]["enabled"] = True             # switched on by hand while its tool is off
    daemons._save_registry(reg)
    r = tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    e = entry()
    check("switched on by hand while its tool is off: the owner wins, it is not re-dormant",
          e.get("enabled") is True and not e.get("dormant") and r["tool_off"] == [])
    tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    check("and the next sync still leaves it alone", entry().get("enabled") is True and not entry().get("dormant"))


def test_new_and_reloaded():
    reset()
    rec = record(entries=[daemon_spec()])
    r = tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    check("a daemon first registered while its tool is already off starts off", entry().get("enabled") is False and entry().get("dormant") is True and r["added"] == ["cas_tool-worker"])
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    check("and comes on with the tool", entry().get("enabled") is True)

    reset()
    tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    tool_daemons.sync([], {"cas_tool"}, tools_off={"cas_tool"})         # the file stops loading
    tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})      # and loads again, tool still off
    check("a file that stops loading and comes back while its tool is still off stays off", entry().get("enabled") is False and entry().get("dormant_reason") == "tool-off")
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    check("and returns when the tool is switched on", entry().get("enabled") is True and not entry().get("dormant"))


def test_running_daemon_is_not_stopped():
    reset()
    rec = record(entries=[daemon_spec()])
    tool_daemons.sync([rec], {"cas_tool"}, tools_off=set())
    stopped = []
    real = tool_daemons._stop_if_up
    tool_daemons._stop_if_up = lambda did: stopped.append(did)
    try:
        tool_daemons.sync([rec], {"cas_tool"}, tools_off={"cas_tool"})
    finally:
        tool_daemons._stop_if_up = real
    check("switching the tool off never stops a daemon (same rule as the daemon's own switch)", stopped == [])


# --- 3. fingerprint ------------------------------------------------------------

def test_fingerprint_follows_the_switches():
    real = tool_disable.disabled_tools
    try:
        tool_disable.disabled_tools = lambda: frozenset()
        fp1, _ = tool_daemons.scan_tool_folders()
        tool_disable.disabled_tools = lambda: frozenset({"cas_a"})
        fp2, _ = tool_daemons.scan_tool_folders()
        tool_disable.disabled_tools = lambda: frozenset({"cas_a"})
        fp3, _ = tool_daemons.scan_tool_folders()
    finally:
        tool_disable.disabled_tools = real
    check("changing which tools are off changes the fingerprint (so sync_if_stale re-runs)", fp1 != fp2)
    check("the same switches give the same fingerprint", fp2 == fp3)


# --- 4. tools.py: screens + the preview ---------------------------------------

def test_screens_and_preview():
    protected = next(iter(tool_disable.PROTECTED_TOOLS))
    saved = (system_tools._FILE_TOOLS, system_tools._FILE_DAEMONS, system_tools.AUTO_UI,
             tool_disable.disabled_tools, tool_disable.disabled_ui)
    off = {"cas_a"}
    off_ui = set()
    try:
        system_tools._FILE_TOOLS = {"cas_tool.py": ["cas_a", "cas_b"], "solo.py": ["solo_a"], "keep.py": [protected, "keep_b"]}
        system_tools._FILE_DAEMONS = {"cas_tool.py": ["cas_tool-worker"]}
        system_tools.AUTO_UI = [
            {"id": "cas_tool-page", "file": "cas_tool.py", "mode": "button", "title": "P", "label": "P", "tool": "cas_a", "tools": ["cas_a"]},
            {"id": "solo-page", "file": "solo.py", "mode": "menu", "title": "S", "label": "S", "tool": "solo_a", "tools": ["solo_a"]},
        ]
        tool_disable.disabled_tools = lambda: frozenset(n for n in off if n != protected)
        tool_disable.disabled_ui = lambda: frozenset(off_ui)

        check("one tool of two off: the file is not all-off", system_tools.files_with_all_tools_off() == set())
        check("so its screen is not cascaded", system_tools.ui_ids_off_with_tool() == set() and not system_tools.ui_is_off("cas_tool-page"))
        off.add("cas_b")
        check("both tools off: the file is all-off", system_tools.files_with_all_tools_off() == {"cas_tool.py"})
        check("its screen is off by its tool, the other file's is not",
              system_tools.ui_ids_off_with_tool() == {"cas_tool-page"} and system_tools.ui_is_off("cas_tool-page") and not system_tools.ui_is_off("solo-page"))
        payload = {u["id"]: u for u in system_tools.tool_ui_payload(include_disabled=True)["ui"]}
        check("the payload marks it disabled and disabled_by_tool", payload["cas_tool-page"]["disabled"] is True and payload["cas_tool-page"]["disabled_by_tool"] is True)
        check("an element that follows nothing is not marked", payload["solo-page"]["disabled"] is False and payload["solo-page"]["disabled_by_tool"] is False)
        check("the default payload (the browser's buttons) leaves the cascaded one out",
              [u["id"] for u in system_tools.tool_ui_payload()["ui"]] == ["solo-page"])
        bundle = system_tools.tool_ui_bundle("cas_tool-page")
        check("its page is no longer served", bundle.get("ok") is False and bundle.get("disabled") is True)

        off.discard("cas_b")
        off.discard("cas_a")
        c = system_tools.switch_off_cascade("cas_a")
        check("switching off the first of two lists nothing", c["screens"] == [] and c["daemons"] == [] and c["file"] == "cas_tool.py")
        off.add("cas_a")
        c = system_tools.switch_off_cascade("cas_b")
        check("switching off the last one lists its screens and daemons", c["screens"] == ["cas_tool-page"] and c["daemons"] == ["cas_tool-worker"])
        off_ui.add("cas_tool-page")
        c = system_tools.switch_off_cascade("cas_b")
        check("a screen the owner already switched off is not listed again", c["screens"] == [])
        c = system_tools.switch_off_cascade("cas_a")
        check("a tool that is already off lists nothing", c["screens"] == [] and c["daemons"] == [])
        check("a protected tool never cascades", system_tools.switch_off_cascade(protected) == {"file": "", "screens": [], "daemons": []})
        check("an unknown name lists nothing and does not raise", system_tools.switch_off_cascade("nope") == {"file": "", "screens": [], "daemons": []})
        check("an empty name lists nothing", system_tools.switch_off_cascade("") == {"file": "", "screens": [], "daemons": []})
        off.clear()
        check("a file that carries a protected tool can never be all-off", "keep.py" not in system_tools.files_with_all_tools_off(off={protected, "keep_b"}))
    finally:
        (system_tools._FILE_TOOLS, system_tools._FILE_DAEMONS, system_tools.AUTO_UI,
         tool_disable.disabled_tools, tool_disable.disabled_ui) = saved


# --- 5. CLI --------------------------------------------------------------------

def test_cli():
    code, out = cli("disabled-list")
    check("disabled-list carries ui_by_tool", code == 0 and out.get("ui_by_tool") == [])
    code, out = cli("disabled-dependents", "tool", "no_such_tool_zzz")
    check("disabled-dependents for a tool carries a cascade block", code == 0 and out.get("cascade") == {"file": "", "screens": [], "daemons": []} and out.get("jobs") == [])
    code, out = cli("disabled-dependents", "command", "no_such_command_zzz")
    check("and not for a saved command", code == 0 and "cascade" not in out)
    code, out = cli("tool-disable-set", "no_such_tool_zzz", "on")
    check("tool-disable-set still answers, now with the cascade fields",
          "cascade" in out or "error" in out)


# --- 6. front end (source checks: app.js is not loadable under node without a DOM) ---

def test_front_end_markers():
    pub = ROOT / "web" / "public"
    app = (pub / "app.js").read_text(encoding="utf-8")
    css = (pub / "style.css").read_text(encoding="utf-8")
    tm = (pub / "tool-manager.js").read_text(encoding="utf-8")
    check("Debug: a switched-off tool's card gets the is-off class and an 'off' badge",
          'tool.disabled ? " is-off" : ""' in app and "debug-tool-card__off" in app)
    check("Debug: the docs pane says it is switched off and still runnable", "debug-docs__off" in app and "Switched off in the Tool Manager" in app)
    check("Debug: the styles exist", ".debug-tool-card.is-off" in css and ".debug-tool-card__off" in css and ".debug-docs__off" in css)
    check("Tool Manager: reads /api/mcp and switches a server through the MCP panel's own route",
          '"/api/mcp"' in tm and '"/api/mcp/server/"' in tm and '"enable" : "disable"' in tm)
    check("Tool Manager: re-reads daemons and screens after a tool switch", "refreshExtraRows" in tm and '"/api/daemons"' in tm)
    check("Tool Manager: a screen that follows its tool has a locked switch", "disabled_by_tool" in tm)


try:
    test_off_owners()
    test_sync_cascade()
    test_owner_choices_survive()
    test_new_and_reloaded()
    test_running_daemon_is_not_stopped()
    test_fingerprint_follows_the_switches()
    test_screens_and_preview()
    test_cli()
    test_front_end_markers()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
