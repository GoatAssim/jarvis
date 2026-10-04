"""L.11 - user-defined daemon categories: registry, CLI and the server's argv helper.

Covers: the `categories` field on custom and built-in daemons, defaults,
replace-not-merge edits, `[]` meaning "remove them all", strict writes that
leave the registry untouched when refused, lenient reads of a hand-edited
file, `builtin` staying a separate lock, the CLI flags, and
`daemonCategoryArgs()` in web/server.js (the one place a category name becomes
an argv element). The panel itself is tested in verify_daemons_panel.js (pure
helpers) and verify_l11_daemon_categories.py (a real browser).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# HOME first: daemons.py resolves ~/.jarvis at import time.
HOME = tempfile.mkdtemp(prefix="jarvis-l11-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import daemons  # noqa: E402

REG = Path(HOME) / ".jarvis" / "daemons.json"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def cats(did):
    return (daemons.get(did) or {}).get("categories")


def reset():
    if REG.exists():
        REG.unlink()


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "jarvis-cli"), HOME=HOME, USERPROFILE=HOME)
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True, env=env, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout, "err": proc.stderr}


def test_registry():
    reset()
    ok, err = daemons.add("web", "python3 -c pass", categories=["Utility", " utility ", "Web   stuff"])
    check("add stores a cleaned, de-duplicated set", ok and cats("web") == ["Utility", "Web stuff"])
    daemons.add("plain", "python3 -c pass")
    check("a daemon added without categories has none (= Undefined)", cats("plain") == [])
    check("describe() exposes them to every caller", daemons.describe(daemons.get("web"))["categories"] == ["Utility", "Web stuff"])

    ok, err = daemons.add("toomany", "python3 -c pass", categories=[str(i) for i in range(9)])
    check("add refuses a ninth category and registers nothing", not ok and "at most 8" in err and daemons.get("toomany") is None)
    ok, err = daemons.add("longname", "python3 -c pass", categories=["x" * 25])
    check("add refuses an over-long name instead of renaming it", not ok and "longer than 24" in err and daemons.get("longname") is None)

    ok, _ = daemons.edit("web", categories=["a", "b"])
    check("edit replaces the whole set, it does not merge", ok and cats("web") == ["a", "b"])
    ok, _ = daemons.edit("web", categories=["b"])
    check("edit can remove one", ok and cats("web") == ["b"])
    ok, _ = daemons.edit("web", categories=[])
    check("[] is a real value: remove them all", ok and cats("web") == [])
    daemons.edit("web", categories=["keep"])
    ok, _ = daemons.edit("web", name="Renamed")
    check("an edit that does not mention categories leaves them alone", ok and cats("web") == ["keep"])

    before = REG.read_text()
    ok, err = daemons.edit("web", name="ShouldNotStick", categories=[str(i) for i in range(9)])
    check("a refused edit says why", not ok and "at most 8" in err)
    check("... and writes nothing, not even the valid field beside it", REG.read_text() == before and daemons.get("web")["name"] == "Renamed")
    ok, err = daemons.edit("web", categories=[3])
    check("non-text names are refused on a write", not ok)


def test_builtins():
    reset()
    check("built-ins ship with sensible default categories", cats("scheduler") == ["core"] and cats("discord") == ["chat"] and cats("instagram") == ["chat"] and cats("browser") == ["desktop"])
    ok, _ = daemons.edit("discord", categories=["chat", "gateway"])
    check("a built-in's categories are editable", ok and cats("discord") == ["chat", "gateway"])
    stored = json.loads(REG.read_text())["daemons"]["discord"]
    check("only the override is stored, never the whole built-in definition", set(stored) == {"categories"})
    ok, _ = daemons.edit("discord", categories=[])
    check("a built-in can be cleared to none, and that sticks", ok and cats("discord") == [])
    check("... and is not mistaken for 'use the default'", json.loads(REG.read_text())["daemons"]["discord"] == {"categories": []})
    ok, _ = daemons.edit("discord", categories=["chat"])
    check("setting a built-in back to its default stores nothing", ok and "discord" not in json.loads(REG.read_text())["daemons"])
    ok, err = daemons.edit("scheduler", argv=["evil"])
    check("categories do not unlock a built-in's locked fields (builtin stays a separate lock)", not ok and "can't be changed" in err)
    check("a built-in is still a built-in", daemons.get("scheduler")["builtin"] is True)


def test_lenient_read():
    reset()
    REG.parent.mkdir(parents=True, exist_ok=True)
    REG.write_text(json.dumps({"daemons": {
        "legacy": {"id": "legacy", "argv": ["x"]},
        "weird": {"id": "weird", "argv": ["x"], "categories": {"not": "a list"}},
        "messy": {"id": "messy", "argv": ["x"], "categories": ["A", "a", "", 7, " b ", "c", "d", "e", "f", "g", "h", "i"]},
        "scheduler": {"categories": {"oops": 1}},
    }}))
    check("a daemon saved before categories existed loads as none", cats("legacy") == [])
    check("a non-list is read as none, not an error", cats("weird") == [])
    check("a hand-edited list is fixed up on read, never refused", cats("messy") == ["A", "b", "c", "d", "e", "f", "g", "h"])
    check("a broken built-in override is read as none rather than failing the registry", cats("scheduler") == [])
    check("the other daemons still load", daemons.get("legacy") is not None and len(daemons.list_daemons()) >= 8)


def test_cli():
    reset()
    code, out = cli("daemon-add", "svc", "python3 -c pass", "--category", "Tools", "--category", "tools", "--category", "chat")
    check("daemon-add --category (repeatable) works", code == 0 and out.get("ok") and cats("svc") == ["Tools", "chat"])
    code, out = cli("daemon-edit", "svc", "--category", "x", "--category", "y")
    check("daemon-edit --category replaces the set", code == 0 and cats("svc") == ["x", "y"] and out["changed"] == ["categories"])
    code, out = cli("daemon-edit", "svc", "--category")
    check("a bare --category is an error, never 'clear'", code != 0 and "needs a name" in out.get("error", "") and cats("svc") == ["x", "y"])
    code, out = cli("daemon-edit", "svc", "--clear-categories")
    check("--clear-categories removes them all", code == 0 and cats("svc") == [])
    code, out = cli("daemon-edit", "svc", "--category", "keep", "--clear-categories")
    check("--clear-categories wins over a stray --category", code == 0 and cats("svc") == [])
    code, out = cli("daemon-edit", "svc", *sum([["--category", str(i)] for i in range(9)], []))
    check("the CLI reports a refused write with a non-zero exit", code != 0 and "at most 8" in out.get("error", "") and cats("svc") == [])
    code, out = cli("daemon-add", "bare", "python3 -c pass", "--category")
    check("daemon-add rejects a bare --category too", code != 0 and daemons.get("bare") is None)
    code, out = cli("daemons")
    check("`jarvis daemons` carries categories for every daemon", code == 0 and all(isinstance(d.get("categories"), list) for d in out["daemons"]))


NODE_HELPER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const m = src.match(/function daemonCategoryArgs\(value\) \{[\s\S]*?\n\}\n/);
if (!m) { console.log(JSON.stringify({ missing: true })); process.exit(0); }
const f = eval('(' + m[0].replace(/^function daemonCategoryArgs/, 'function') + ')');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map((c) => f(c === '__undefined__' ? undefined : c))));
"""


def test_server_helper():
    node = shutil.which("node")
    if not node:
        print("SKIP    server.js daemonCategoryArgs (node not installed)")
        return
    cases = ["__undefined__", [], ["a", "b"], ["  a  ", "", "  "], ["--shell"], [" --x"], ["-ok"], "oops", [1], list("a" * 51)]
    proc = subprocess.run([node, "-e", NODE_HELPER, str(ROOT / "web" / "server.js")], input=json.dumps(cases), capture_output=True, text=True, timeout=30)
    res = json.loads(proc.stdout) if proc.returncode == 0 else None
    check("the helper can be lifted out of server.js", res is not None and not (isinstance(res, dict) and res.get("missing")))
    if not isinstance(res, list):
        return
    check("undefined = leave categories alone (no args)", res[0] == {"args": None})
    check("an empty list = remove them all, as its own flag", res[1] == {"args": ["--clear-categories"]})
    check("names become --category pairs, in order", res[2] == {"args": ["--category", "a", "--category", "b"]})
    check("names are trimmed and blanks skipped; all-blank counts as empty", res[3] == {"args": ["--category", "a"]})
    check("a name starting with two dashes is refused (it would be parsed as a flag)", "error" in res[4] and "error" in res[5])
    check("one dash is fine", res[6] == {"args": ["--category", "-ok"]})
    check("a non-list is refused", "error" in res[7])
    check("a non-string name is refused", "error" in res[8])
    check("an absurd count is refused before it can build a huge argv", "error" in res[9])


try:
    test_registry()
    test_builtins()
    test_lenient_read()
    test_cli()
    test_server_helper()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
