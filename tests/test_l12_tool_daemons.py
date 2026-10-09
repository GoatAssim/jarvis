"""L.12 - daemons a tool supplies.

Covers: validate_daemons() (namespacing, placeholders, defaults, every way an
entry can be wrong, one bad entry not taking the others down); the loader
carrying a file's DAEMONS on its record and still loading the tools when an
entry is bad; sync() (register, idempotent, re-apply what the tool controls,
never overwrite what the owner edited, conflicts, removal on uninstall and on
being dropped from the file, switched off - not deleted - while the file is
disabled/broken and restored as it was); the locks (a tool-supplied daemon's
command/cwd/env/description cannot be edited and it cannot be removed by hand);
the autostart preset; sync_if_stale()'s cheap fingerprint check; and the CLI.
The Tool Manager's own switch is deliberately NOT part of this (Q-L46m).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-l12-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import daemons, tool_daemons, tool_loader  # noqa: E402

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


TOOL_DIR = Path(tempfile.mkdtemp(prefix="jarvis-l12-tool-"))


def decl(**over):
    base = {"id": "worker", "command": ["{python}", "{tool_dir}/worker.py"]}
    base.update(over)
    return base


def validated(file="my_tool.py", **over):
    entries, errors = tool_daemons.validate_daemons([decl(**over)], file, TOOL_DIR)
    return entries, errors


def record(file="my_tool.py", entries=None, valid=True):
    return types.SimpleNamespace(valid=valid, file=file, daemons=entries or [])


def test_validate():
    entries, errors = validated()
    e = entries[0] if entries else {}
    check("a minimal entry is accepted", len(entries) == 1 and errors == [])
    check("its registry id is namespaced by the file: <file>-<id>", e.get("did") == "my_tool-worker" and e.get("owner") == "my_tool")
    check("{python} and {tool_dir} are filled in", e.get("argv") == [tool_daemons.python_executable(), f"{TOOL_DIR}/worker.py"])
    check("cwd defaults to the tool's own folder", e.get("cwd") == str(TOOL_DIR))
    check("every other field gets its default", e.get("autostart") is False and e.get("restart") == "never" and e.get("shell") is False
          and e.get("supports_stdin") is False and e.get("categories") == [] and e.get("env") == {}
          and e.get("max_restarts") == daemons.DEFAULT_MAX_RESTARTS and e.get("stop_timeout") == daemons.DEFAULT_STOP_TIMEOUT)

    entries, _ = validated(command="python worker.py --fast")
    check("a string command is split like daemon-add", entries and entries[0]["argv"] == ["python", "worker.py", "--fast"])
    entries, _ = validated(command="echo a | findstr a", shell=True)
    check("a shell command is kept verbatim as one element", entries and entries[0]["argv"] == ["echo a | findstr a"])
    entries, _ = validated(cwd="sub")
    check("a relative cwd is relative to the tool's folder", entries and entries[0]["cwd"] == str(TOOL_DIR / "sub"))
    entries, _ = validated(cwd="{tool_dir}/data")
    check("cwd takes {tool_dir} too", entries and entries[0]["cwd"] == f"{TOOL_DIR}/data")

    entries, _ = validated(name="  Pretty  ", description=" does x ", categories=["Mine", "mine", " Other "], autostart=True,
                           restart="on-failure", restart_delay=2, max_restarts=0, stop_signal="INT", stop_timeout=3,
                           env={"A": "1"}, supports_stdin=True, notes="n")
    e = entries[0] if entries else {}
    check("every declared field is carried (trimmed, categories cleaned)",
          e.get("name") == "Pretty" and e.get("description") == "does x" and e.get("categories") == ["Mine", "Other"]
          and e.get("autostart") is True and e.get("restart") == "on-failure" and e.get("restart_delay") == 2
          and e.get("max_restarts") == 0 and e.get("stop_signal") == "INT" and e.get("stop_timeout") == 3
          and e.get("env") == {"A": "1"} and e.get("supports_stdin") is True and e.get("notes") == "n")

    bad = {
        "unknown key": decl(colour="red"),
        "no id": {"command": ["x"]},
        "no command": {"id": "w"},
        "empty command": decl(command="   "),
        "command of non-text": decl(command=[1, 2]),
        "bad restart": decl(restart="sometimes"),
        "bad signal": decl(stop_signal="HUP"),
        "bool as number": decl(restart_delay=True),
        "zero delay": decl(restart_delay=0),
        "negative max": decl(max_restarts=-1),
        "autostart not a bool": decl(autostart="yes"),
        "env not a dict": decl(env=["A=1"]),
        "env bad key": decl(env={"A B": "1"}),
        "env non-text value": decl(env={"A": 1}),
        "too many categories": decl(categories=[str(i) for i in range(9)]),
        "category too long": decl(categories=["x" * 25]),
        "name not text": decl(name=5),
        "not a dict": "worker",
    }
    for label, item in bad.items():
        entries, errors = tool_daemons.validate_daemons([item], "my_tool.py", TOOL_DIR)
        check(f"rejected: {label}", entries == [] and len(errors) == 1)
    entries, errors = tool_daemons.validate_daemons([decl(id="a"), {"id": "b"}, decl(id="c")], "my_tool.py", TOOL_DIR)
    check("one bad entry drops only itself", [e["id"] for e in entries] == ["a", "c"] and len(errors) == 1)
    entries, errors = tool_daemons.validate_daemons([decl(), decl()], "my_tool.py", TOOL_DIR)
    check("a repeated id is dropped", len(entries) == 1 and len(errors) == 1)
    entries, errors = tool_daemons.validate_daemons([decl(id="x" * 30)], "a_rather_long_tool_name.py", TOOL_DIR)
    check("a file name + id that cannot fit in 40 characters is refused", entries == [] and "40" in errors[0])
    check("DAEMONS that is not a list is refused", tool_daemons.validate_daemons({"id": "x"}, "t.py", TOOL_DIR)[0] == [])
    many = [decl(id=f"d{i}") for i in range(tool_daemons.MAX_PER_FILE + 3)]
    entries, errors = tool_daemons.validate_daemons(many, "t.py", TOOL_DIR)
    check("more than the per-file cap is cut, with a note", len(entries) == tool_daemons.MAX_PER_FILE and errors)


TOOL_SOURCE = '''
TOOL_SCHEMAS = [{"name": "l12_ping", "description": "ping", "parameters": {"type": "object", "properties": {}}}]
TOOLS = {"l12_ping": lambda: "pong"}
TOOL_GROUP = "l12test"
TOOL_KEYWORDS = {"l12_ping": ["l12 ping"]}
DAEMONS = %s
'''


def test_loader():
    d = Path(tempfile.mkdtemp(prefix="jarvis-l12-actions-"))
    good = [{"id": "worker", "command": ["{python}", "{tool_dir}/w.py"], "categories": ["Mine"], "autostart": True}]
    (d / "l12_tool.py").write_text(TOOL_SOURCE % repr(good), encoding="utf-8")
    (d / "l12_mixed.py").write_text(TOOL_SOURCE.replace('"l12_ping"', '"l12_pong"').replace("l12 ping", "l12 pong") % repr([{"id": "ok", "command": ["x"]}, {"id": "bad"}]), encoding="utf-8")
    (d / "l12_none.py").write_text(TOOL_SOURCE.replace('"l12_ping"', '"l12_nada"').replace("l12 ping", "l12 nada").replace("DAEMONS = %s\n", "") , encoding="utf-8")
    logs = []
    records = {r.file: r for r in tool_loader.discover_actions(actions_dir=d, logger=logs.append)}
    r = records.get("l12_tool.py")
    check("a tool file with DAEMONS loads", r is not None and r.valid)
    check("its record carries the validated daemons", r is not None and len(r.daemons) == 1 and r.daemons[0]["did"] == "l12_tool-worker")
    check("{tool_dir} resolved to the file's own folder", r is not None and r.daemons and r.daemons[0]["argv"][1] == f"{d.resolve()}/w.py")
    m = records.get("l12_mixed.py")
    check("a bad entry is logged and dropped; the tools and the good entry still load",
          m is not None and m.valid and [x["id"] for x in m.daemons] == ["ok"] and any("[tool-daemons]" in l for l in logs))
    n = records.get("l12_none.py")
    check("a file with no DAEMONS has an empty list", n is not None and n.valid and n.daemons == [])
    shutil.rmtree(d, ignore_errors=True)


def spec(**over):
    entries, errors = validated(**over)
    assert entries, errors
    return entries[0]


def test_sync_lifecycle():
    reset()
    r = tool_daemons.sync([record(entries=[spec(categories=["Mine"], autostart=True, name="My worker", description="d")])], {"my_tool"})
    check("installing a tool registers its daemon", r["added"] == ["my_tool-worker"])
    e = daemons.get("my_tool-worker")
    check("it is owned by the tool and not a built-in", e["owner"] == "my_tool" and e["builtin"] is False)
    check("the preset autostart and suggested categories are applied", e["autostart"] is True and e["categories"] == ["Mine"])
    check("it starts enabled with the console window switches off", e["enabled"] is True and e["console_window_auto"] is False)
    check("it appears in the normal daemon list", any(x["id"] == "my_tool-worker" for x in daemons.list_daemons()))

    mtime = REG.stat().st_mtime_ns
    r = tool_daemons.sync([record(entries=[spec(categories=["Mine"], autostart=True, name="My worker", description="d")])], {"my_tool"})
    check("syncing again changes nothing", not any(r[k] for k in ("added", "updated", "removed", "dormant", "woken", "conflicts")))
    check("...and does not even rewrite the registry", REG.stat().st_mtime_ns == mtime)

    # the owner edits what is theirs
    ok1, _ = daemons.edit("my_tool-worker", categories=["Elsewhere"], autostart=False, name="Renamed", notes="mine",
                          restart="always", console_window_auto=True)
    ok2, _ = daemons.edit("my_tool-worker", enabled=False)
    check("the owner can change name, categories, autostart, notes, restart policy, enabled and the window", ok1 and ok2)
    # the tool changes what it controls, and its own presets
    new = spec(command=["{python}", "{tool_dir}/worker2.py"], categories=["Tool's idea"], autostart=True, name="Tool's name",
               description="new", env={"K": "v"}, shell=False, supports_stdin=True)
    r = tool_daemons.sync([record(entries=[new])], {"my_tool"})
    e = daemons.get("my_tool-worker")
    check("what the tool controls is re-applied (command, env, stdin, description)",
          r["updated"] == ["my_tool-worker"] and e["argv"][1].endswith("worker2.py") and e["env"] == {"K": "v"}
          and e["supports_stdin"] is True and e["description"] == "new")
    check("the owner's edits survive (categories, autostart, name, enabled, notes, restart, window)",
          e["categories"] == ["Elsewhere"] and e["autostart"] is False and e["name"] == "Renamed" and e["enabled"] is False
          and e["notes"] == "mine" and e["restart"] == "always" and e["console_window_auto"] is True)

    # locks
    for field, value in (("argv", ["x"]), ("cwd", "/tmp"), ("env", {"A": "1"}), ("description", "x"),
                         ("shell", True), ("supports_stdin", False)):
        ok, err = daemons.edit("my_tool-worker", **{field: value})
        check(f"'{field}' is locked on a tool-supplied daemon", not ok and "my_tool" in err)
    ok, err = daemons.remove("my_tool-worker")
    check("a tool-supplied daemon cannot be removed by hand", not ok and "my_tool" in err and daemons.get("my_tool-worker"))

    # dropped from the file while the file still loads
    r = tool_daemons.sync([record(entries=[])], {"my_tool"})
    check("an entry dropped from DAEMONS is removed", r["removed"] == ["my_tool-worker"] and daemons.get("my_tool-worker") is None)


def test_uninstall_and_dormant():
    reset()
    stopped = []
    real_stop, real_status = daemons.stop, daemons.status
    daemons.stop = lambda did, timeout=None: (stopped.append(did), (True, "stopped"))[1]
    try:
        tool_daemons.sync([record(entries=[spec()])], {"my_tool"})
        daemons._write_status("my_tool-worker", supervisor_pid=os.getpid() + 100000, status="running")
        daemons.status = lambda did: {"running": True, "supervisor_pid": 1, "pid": 1}
        r = tool_daemons.sync([], set())
        check("uninstalling the tool (file gone) removes its daemon", r["removed"] == ["my_tool-worker"] and daemons.get("my_tool-worker") is None)
        check("...after stopping it if it was running", stopped == ["my_tool-worker"])
        daemons.status = real_status

        # disabled / broken file: present on disk, not loaded
        tool_daemons.sync([record(entries=[spec()])], {"my_tool"})
        daemons.edit("my_tool-worker", enabled=True)
        r = tool_daemons.sync([], {"my_tool"})
        e = daemons.get("my_tool-worker")
        check("a tool that is present but not loading switches its daemon OFF instead of deleting it",
              r["dormant"] == ["my_tool-worker"] and e is not None and e["enabled"] is False and e.get("dormant") is True)
        check("a dormant daemon cannot be started", daemons.start("my_tool-worker")[0] is False)
        r = tool_daemons.sync([], {"my_tool"})
        check("syncing a dormant daemon again changes nothing", not r["dormant"] and not r["removed"])
        r = tool_daemons.sync([record(entries=[spec()])], {"my_tool"})
        e = daemons.get("my_tool-worker")
        check("when the tool loads again its daemon is back on, as it was", r["woken"] == ["my_tool-worker"] and e["enabled"] is True and not e.get("dormant"))

        # the owner had switched it off BEFORE the tool went away: it stays off
        daemons.edit("my_tool-worker", enabled=False)
        tool_daemons.sync([], {"my_tool"})
        tool_daemons.sync([record(entries=[spec()])], {"my_tool"})
        check("...and one the owner had switched off stays off", daemons.get("my_tool-worker")["enabled"] is False)
    finally:
        daemons.stop, daemons.status = real_stop, real_status


def test_conflicts():
    reset()
    daemons.add("my_tool-worker", "python3 -c pass")
    r = tool_daemons.sync([record(entries=[spec()])], {"my_tool"})
    check("a tool cannot take over a daemon the owner made", r["added"] == [] and len(r["conflicts"]) == 1)
    e = daemons.get("my_tool-worker")
    check("...the owner's daemon is untouched", e.get("owner") is None and e["argv"][0] == "python3")
    check("...and it is still the owner's to remove", daemons.remove("my_tool-worker")[0] is True)

    reset()
    a = record(file="a_tool.py", entries=[{**spec(), "did": "shared-x", "owner": "a_tool"}])
    b = record(file="b_tool.py", entries=[{**spec(), "did": "shared-x", "owner": "b_tool"}])
    r = tool_daemons.sync([a, b], {"a_tool", "b_tool"})
    check("two tools claiming one id: the first wins and the clash is reported",
          r["added"] == ["shared-x"] and daemons.get("shared-x")["owner"] == "a_tool" and len(r["conflicts"]) == 1)

    reset()
    r = tool_daemons.sync([record(valid=False, entries=[spec()])], {"my_tool"})
    check("an invalid record contributes nothing", r["added"] == [])


def test_autostart_preset():
    reset()
    tool_daemons.sync([record(entries=[spec(autostart=True), spec(id="idle")])], {"my_tool"})
    started = []
    real_start, real_sync = daemons.start, tool_daemons.sync_if_stale
    daemons.start = lambda did: (started.append(did), (True, "ok"))[1]
    tool_daemons.sync_if_stale = lambda *a, **k: None
    try:
        daemons.autostart_all()
    finally:
        daemons.start, tool_daemons.sync_if_stale = real_start, real_sync
    check("autostart brings up a tool's daemon whose preset says so, and only that one", started == ["my_tool-worker"])


def test_stale_check():
    reset()
    recs = [record(entries=[spec()])]
    r = tool_daemons.sync_if_stale(records=recs, scan=("stamp-1", {"my_tool"}))
    check("the first look reconciles", r is not None and r["added"] == ["my_tool-worker"])
    check("...and remembers the fingerprint", json.loads(STATE.read_text(encoding="utf-8"))["stamp"] == "stamp-1")
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
    real = tool_daemons.sync
    tool_daemons.sync = boom
    try:
        check("an unchanged fingerprint does no work at all", tool_daemons.sync_if_stale(records=recs, scan=("stamp-1", {"my_tool"})) is None)
    finally:
        tool_daemons.sync = real
    r = tool_daemons.sync_if_stale(records=[], scan=("stamp-2", set()))
    check("a changed fingerprint reconciles again", r is not None and r["removed"] == ["my_tool-worker"])
    r = tool_daemons.sync_if_stale(records=[], scan=("stamp-2", set()), force=True)
    check("force reconciles regardless", r is not None)

    def failing(*a, **k):
        raise RuntimeError("tools would not import")
    tool_daemons.sync = failing
    try:
        r = tool_daemons.sync_if_stale(records=[], scan=("stamp-3", set()))
    finally:
        tool_daemons.sync = real
    st = json.loads(STATE.read_text(encoding="utf-8"))
    check("a failure is recorded against the fingerprint, not raised, and not retried until something changes",
          r is None and st["stamp"] == "stamp-3" and "would not import" in st.get("error", ""))

    # the real scan: names + sizes + mtimes of the two tool folders
    stamp, present = tool_daemons.scan_tool_folders()
    check("the real folder scan returns a fingerprint and the owners present on disk", isinstance(stamp, str) and len(stamp) == 64 and isinstance(present, set))
    from jarvis import custom_tools_store
    custom_tools_store.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    (custom_tools_store.TOOLS_DIR / "l12_scan.py.disabled").write_text("x", encoding="utf-8")
    stamp2, present2 = tool_daemons.scan_tool_folders()
    check("a disabled tool file still counts as present, and changes the fingerprint", "l12_scan" in present2 and stamp2 != stamp)
    (custom_tools_store.TOOLS_DIR / "l12_scan.py.disabled").unlink()


def test_cli_and_panel_fields():
    reset()
    tool_daemons.sync([record(entries=[spec(name="W", categories=["Mine"])])], {"my_tool"})
    code, out = cli("daemon-edit", "my_tool-worker", "--command", "evil")
    check("the CLI refuses to edit what the tool controls", code != 0 and "my_tool" in json.dumps(out))
    code, out = cli("daemon-remove", "my_tool-worker")
    check("the CLI refuses to remove it", code != 0 and out.get("ok") is False)
    code, out = cli("daemon-edit", "my_tool-worker", "--category", "Mine2")
    check("the CLI still edits what is the owner's", code == 0 and daemons.get("my_tool-worker")["categories"] == ["Mine2"])
    check("the listing row exposes the owner (the panel's tag and locks read it)",
          daemons.describe(daemons.get("my_tool-worker")).get("owner") == "my_tool")
    # The CLI process reconciles against the REAL installed tools, and a fake
    # owner is not one of them - so this is also the "tool uninstalled" path.
    code, out = cli("daemons-sync", "--force")
    check("daemons-sync reports what it changed",
          code == 0 and out.get("ok") is True and out.get("changed") is True
          and out["result"]["removed"] == ["my_tool-worker"])
    code, out = cli("daemons", "--no-usage")
    check("and the daemon is gone from the listing", code == 0 and all(d["id"] != "my_tool-worker" for d in out.get("daemons", [])))
    js = (ROOT / "web" / "public" / "daemons.js").read_text(encoding="utf-8")
    check("the panel locks command/cwd/env/description and hides Remove for a tool-supplied daemon",
          "owned: Boolean(entry && entry.owner)" in js and "!entry.builtin && !entry.owner" in js and "Set by the tool" in js)


try:
    test_validate()
    test_loader()
    test_sync_lifecycle()
    test_uninstall_and_dormant()
    test_conflicts()
    test_autostart_preset()
    test_stale_check()
    test_cli_and_panel_fields()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
    shutil.rmtree(TOOL_DIR, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
