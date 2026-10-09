"""L.14 - command categories.

Covers: `categories` as an optional key on a command spec (Python validation,
the same rules as daemons: at most 8, 24 characters, text only, strict on
write); a spec without it behaving exactly as before; the AI's update tool
leaving categories alone; web/server.js's validateSpec (extracted and run in
Node: stored cleaned, empty list dropped, bad input refused with a message);
JarvisCategories.fitCount (how many chips fit beside the fixed badges); and a
static check that the editor, card and info view are wired up. Layout itself -
chips really omitted at a narrow width - needs a browser: manual checklist.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-l14-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import commands_config, command_tools  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def spec(**extra):
    return {"description": "d", "run": "echo hi", "vars": {}, **extra}


def test_python_validation():
    check("a command with no categories is valid, exactly as before", commands_config.validate_command_spec(spec()) is None)
    check("categories: null is the same as none", commands_config.validate_command_spec(spec(categories=None)) is None)
    check("an empty list is valid", commands_config.validate_command_spec(spec(categories=[])) is None)
    check("a normal list is valid", commands_config.validate_command_spec(spec(categories=["deploy", "git"])) is None)
    check("exactly eight is valid", commands_config.validate_command_spec(spec(categories=[str(i) for i in range(8)])) is None)
    err = commands_config.validate_command_spec(spec(categories=[str(i) for i in range(9)]))
    check("a ninth category is refused, with the rule in the message", err and "at most 8" in err and err.startswith("Categories:"))
    err = commands_config.validate_command_spec(spec(categories=["x" * 25]))
    check("a 25-character name is refused", err and "longer than 24" in err)
    check("exactly 24 characters is valid", commands_config.validate_command_spec(spec(categories=["x" * 24])) is None)
    err = commands_config.validate_command_spec(spec(categories=["ok", 5]))
    check("a non-text name is refused", err and "must be text" in err)
    err = commands_config.validate_command_spec(spec(categories={"a": 1}))
    check("a non-list is refused", err and "list" in err)
    check("the existing checks still run first", commands_config.validate_command_spec({"description": "x", "categories": ["a"]}) == "Command must have a 'run' (string or list of steps).")


def test_ai_update_keeps_categories():
    commands_config.save_commands_dict({"deploy": spec(categories=["ops", "git"])})
    result = command_tools.tool_update_command({"name": "deploy", "description": "new words"})
    after = commands_config.load_commands_dict()["deploy"]
    check("the AI's update tool keeps a command's categories", "error" not in result and after["categories"] == ["ops", "git"] and after["description"] == "new words")
    result = command_tools.tool_update_command({"name": "deploy", "run": "echo two"})
    check("...even when it rewrites the steps", commands_config.load_commands_dict()["deploy"]["categories"] == ["ops", "git"])
    created = command_tools.tool_create_command({"name": "fresh", "description": "x", "run": "echo 1"})
    check("a command the AI creates simply has none", "error" not in created and "categories" not in commands_config.load_commands_dict()["fresh"])


NODE_SERVER = r"""
const fs = require('fs');
const path = require('path');
// category-input.js sets globalThis.JarvisCategories when there is no window
eval(fs.readFileSync(process.argv[1], 'utf8'));
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/function validateSpec\(spec\) \{[\s\S]*?\n\}\n/);
if (!m) { console.log(JSON.stringify({ missing: true })); process.exit(0); }
const validateSpec = eval('(' + m[0].replace(/^function validateSpec/, 'function') + ')');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map((c) => {
  const spec = JSON.parse(JSON.stringify(c));
  const error = validateSpec(spec);
  return { error, spec };
})));
"""

NODE_FIT = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const f = globalThis.JarvisCategories.fitCount;
console.log(JSON.stringify([
  f([50, 50, 60, 40, 40], 400, 8, 2),        // everything fits
  f([50, 50, 60, 40, 40], 190, 8, 2),        // two chips: 50+8+50+8+60=176 ok, +8+40=224 no
  f([50, 50, 60, 40, 40], 176, 8, 2),        // exactly full still fits
  f([50, 50, 60, 40, 40], 175, 8, 2),        // one pixel short: the chip is left out
  f([50, 50, 60, 40, 40], 10, 8, 2),         // fixed badges alone overflow: no chips, they stay
  f([50, 50], 400, 8, 2),                    // no chips at all
  f([50, 50, 200, 10], 300, 8, 2),           // the wide chip does not fit: later, smaller ones are NOT slipped in
  f([], 100, 8, 0),
  f([30, 30], 100, 0, 0),                    // no gap, no fixed
  f(null, 100, 8, 2),
]));
"""


def node():
    return shutil.which("node")


def test_server_validate_spec():
    if not node():
        print("SKIP    server.js validateSpec (node not installed)")
        return
    cases = [
        {"run": "x"},
        {"run": "x", "categories": ["  Deploy ", "deploy", "Git"]},
        {"run": "x", "categories": []},
        {"run": "x", "categories": [str(i) for i in range(9)]},
        {"run": "x", "categories": ["y" * 25]},
        {"run": "x", "categories": ["ok", 3]},
        {"run": "x", "categories": {"a": 1}},
        {"run": "x", "categories": "single"},
        {"run": "x", "categories": None},
        {"categories": ["a"]},
    ]
    proc = subprocess.run([node(), "-e", NODE_SERVER, str(ROOT / "web" / "public" / "category-input.js"), str(ROOT / "web" / "server.js")],
                          input=json.dumps(cases), capture_output=True, text=True, timeout=30)
    res = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None
    check("validateSpec can be lifted out of server.js and run", isinstance(res, list))
    if not isinstance(res, list):
        print(proc.stderr[:400])
        return
    check("no categories: unchanged and valid", res[0]["error"] is None and "categories" not in res[0]["spec"])
    check("categories are stored cleaned, de-duplicated, in order", res[1]["error"] is None and res[1]["spec"]["categories"] == ["Deploy", "Git"])
    check("an empty list is not stored at all", res[2]["error"] is None and "categories" not in res[2]["spec"])
    check("nine is refused with the rule", res[3]["error"] and "at most 8" in res[3]["error"] and res[3]["error"].startswith("Categories:"))
    check("25 characters is refused", res[4]["error"] and "longer than 24" in res[4]["error"])
    check("a non-text name is refused", res[5]["error"] and "must be text" in res[5]["error"])
    check("a non-list is refused", res[6]["error"] and "list" in res[6]["error"])
    check("a bare string is read as one category, like the daemon CLI", res[7]["error"] is None and res[7]["spec"]["categories"] == ["single"])
    check("null is the same as none", res[8]["error"] is None and "categories" not in res[8]["spec"])
    check("the existing 'run' check still comes first", res[9]["error"] and "'run'" in res[9]["error"])
    # Python and JS agree on the message for the same bad input
    py = commands_config.validate_command_spec(spec(categories=[str(i) for i in range(9)]))
    check("Python and the server give the same message", py == res[3]["error"])
    py = commands_config.validate_command_spec(spec(categories=["y" * 25]))
    check("...for the long name too", py == res[4]["error"])


def test_fit_count():
    if not node():
        print("SKIP    fitCount (node not installed)")
        return
    proc = subprocess.run([node(), "-e", NODE_FIT, str(ROOT / "web" / "public" / "category-input.js")], capture_output=True, text=True, timeout=30)
    res = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None
    check("fitCount runs in Node (it needs no browser)", isinstance(res, list))
    if not isinstance(res, list):
        print(proc.stderr[:400])
        return
    check("everything fits: all three chips", res[0] == 3)
    check("two of three fit beside two fixed badges", res[1] == 2)
    check("a row that is exactly full still fits", res[2] == 2)
    check("one pixel short leaves the chip out", res[3] == 1)
    check("fixed badges that overflow on their own show no chips", res[4] == 0)
    check("no chips to show", res[5] == 0)
    check("a chip that does not fit also ends the row - no smaller one jumps ahead", res[6] == 0)
    check("nothing at all", res[7] == 0)
    check("no gap, no fixed items", res[8] == 2)
    check("garbage in is not an error", res[9] == 0)


def test_wiring():
    app = (ROOT / "web" / "public" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "public" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "public" / "style.css").read_text(encoding="utf-8")
    check("the editor has a Categories field backed by the shared chip box", 'id="f-categories"' in html and "commandCategoryInput()" in app)
    check("suggestions come from commands only", "Object.values(state.commands).map(commandCategories)" in app)
    check("the builder writes categories only when there are some", "if (categories.length) spec.categories = categories;" in app)
    check("the Raw JSON tab round-trips them", "commandCategoryInput().setValue(commandCategories(parsed))" in app)
    check("cards show chips after the badges and fit them to the row", "...commandCategories(spec).map(categoryChip)" in app and "fitCommandChips" in app and "scheduleChipFit();" in app)
    check("the info view lists every category", 'id="detail-cats"' in html and 'qs("#detail-cats")' in app)
    check("chips are drawn like the conditional badge", ".badge--cat{ color: var(--accent-secondary)" in css)
    check("the meta row is one line", "flex-wrap: nowrap" in css)
    check("category-input.js loads before app.js", html.index('src="category-input.js"') < html.index('src="app.js"'))
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    check("the server shares the one JS copy of the rules", 'import "./public/category-input.js";' in server and "globalThis.JarvisCategories.normalizeList(spec.categories, true)" in server)


try:
    test_python_validation()
    test_ai_update_keeps_categories()
    test_server_validate_spec()
    test_fit_count()
    test_wiring()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
