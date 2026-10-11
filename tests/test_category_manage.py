"""Renaming, merging and deleting a category as a whole (L.12 follow-up).

A category is only a name inside items' `categories` lists, so these are edits to
those lists. Covers: the pure list helpers in categories.py; the daemon registry
(usage, rename, merge, delete, built-ins, tool-supplied daemons, a refused rename
leaving the file untouched); the `daemon-category` CLI verb; that the verb is
registered everywhere a verb has to be; and, statically, the server routes and the
manager dialog's wiring. The JavaScript twin of the list helpers is checked against
these in test_categories.py, and the dialog's pure helpers in
verify_category_manager.js.

    python3 tests/test_category_manage.py
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
HOME = tempfile.mkdtemp(prefix="jarvis-catmanage-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import categories as C  # noqa: E402
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
    # atomic_io.read_json falls back to the .bak when the main file is missing, so
    # a reset has to remove both or the previous test's registry comes back.
    for f in (REG, REG.with_suffix(REG.suffix + ".bak")):
        if f.exists():
            f.unlink()


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "jarvis-cli"), HOME=HOME, USERPROFILE=HOME)
    proc = subprocess.run([sys.executable, "-m", "jarvis", *args], capture_output=True, text=True, env=env, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"raw": proc.stdout, "err": proc.stderr}


def test_list_helpers():
    r = C.rename_in_list
    check("rename replaces the name in place, keeping order", r(["a", "Utility", "b"], "utility", "Tools") == (["a", "Tools", "b"], True))
    check("rename matches case-insensitively", r(["UTILITY"], "utility", "x") == (["x"], True))
    check("an item without the name is returned unchanged", r(["a"], "zzz", "x") == (["a"], False))
    check("renaming to a name the item already holds merges (its spelling stays)", r(["Utility", "tools"], "Utility", "Tools") == (["tools"], True))
    check("a case-only rename changes the spelling", r(["utility"], "utility", "Utility") == (["Utility"], True))
    check("renaming to the same text changes nothing", r(["a"], "a", "a") == (["a"], False))
    check("a blank old or new name changes nothing", r(["a"], "", "x") == (["a"], False) and r(["a"], "a", "  ") == (["a"], False))
    check("junk input is read leniently", r(None, "a", "b") == ([], False) and r("solo", "solo", "x") == (["x"], True))
    d = C.remove_from_list
    check("remove takes the name out, case-insensitively", d(["a", "B", "c"], "b") == (["a", "c"], True))
    check("remove of an absent name changes nothing", d(["a"], "z") == (["a"], False))
    check("remove of the last name leaves an empty list", d(["a"], "a") == ([], True))
    n = C.check_new_name
    check("check_new_name cleans a good name", n("  Web   stuff ") == ("Web stuff", ""))
    check("check_new_name refuses blank, non-text and over-long names", n("  ")[1] != "" and n(5)[1] != "" and "longer than 24" in n("x" * 25)[1])


def test_registry():
    reset()
    daemons.add("a", "python3 -c pass", categories=["Utility", "Web"])
    daemons.add("b", "python3 -c pass", categories=["utility"])
    daemons.add("c", "python3 -c pass", categories=["Tools", "Utility"])
    daemons.add("d", "python3 -c pass")
    usage = {u["key"]: u["count"] for u in daemons.category_usage()}
    check("usage counts a category once however it is spelt", usage.get("utility") == 3 and usage.get("web") == 1 and usage.get("tools") == 1)
    check("usage lists the most used first", daemons.category_usage()[0]["key"] == "utility")

    ok, res = daemons.rename_category("Utility", "Helpers")
    check("rename succeeds and reports how many daemons changed", ok and res == {"changed": 3, "merged": False})
    check("every spelling was renamed", cats("a") == ["Helpers", "Web"] and cats("b") == ["Helpers"] and cats("c") == ["Tools", "Helpers"])
    check("a daemon without it is untouched", cats("d") == [])

    ok, res = daemons.rename_category("helpers", "Tools")
    check("renaming to a name in use is a merge and says so", ok and res["merged"] is True and res["changed"] == 3)
    check("a daemon holding both ends up with it once", cats("c") == ["Tools"] and cats("a") == ["Tools", "Web"] and cats("b") == ["Tools"])

    before = REG.read_text()
    ok, err = daemons.rename_category("nothere", "x")
    check("renaming a category nobody has is an error", not ok and "no daemon has" in err)
    ok, err = daemons.rename_category("Tools", "x" * 25)
    check("an over-long new name is refused", not ok and "longer than 24" in err)
    ok, err = daemons.rename_category("Tools", "   ")
    check("a blank new name is refused", not ok)
    ok, err = daemons.rename_category("", "x")
    check("a blank old name is refused", not ok)
    check("refusals write nothing", REG.read_text() == before)

    ok, res = daemons.rename_category("Tools", "tools")
    check("a case-only rename is not a merge", ok and res["merged"] is False and cats("b") == ["tools"])

    ok, res = daemons.delete_category("WEB")
    check("delete takes the label off and counts the daemons", ok and res == {"changed": 1} and cats("a") == ["tools"])
    ok, res = daemons.delete_category("tools")
    check("deleting the last label leaves an empty list (Undefined)", ok and cats("a") == [] and cats("b") == [] and cats("c") == [])
    ok, err = daemons.delete_category("tools")
    check("deleting a category nobody has is an error", not ok and "no daemon has" in err)
    check("the daemons themselves are all still there", all(daemons.get(x) for x in "abcd"))


def test_builtins_and_owned():
    reset()
    check("a built-in carries its own category to begin with", "chat" in (cats("discord") or []))
    ok, res = daemons.rename_category("chat", "Messaging")
    check("a built-in is renamed too (it is a label, not a locked field)", ok and cats("discord") == ["Messaging"] and cats("instagram") == ["Messaging"])
    stored = json.loads(REG.read_text())["daemons"]
    check("only the category override is stored for a built-in", set(stored["discord"]) == {"categories"})
    ok, res = daemons.delete_category("Messaging")
    check("deleting it from built-ins stores an empty override, not the default again", ok and cats("discord") == [] and cats("instagram") == [])
    after = daemons._load_registry()
    check("built-in commands were never touched", after["discord"]["argv"] == daemons.BUILTINS["discord"]["argv"])

    reset()
    daemons.add("tooly", "python3 -c pass", categories=["Mine"])
    merged = daemons._load_registry()
    merged["tooly"]["owner"] = "sometool"
    daemons._save_registry(merged)
    ok, res = daemons.rename_category("Mine", "Theirs")
    check("a tool-supplied daemon's categories are the owner's to rename", ok and cats("tooly") == ["Theirs"] and daemons.get("tooly").get("owner") == "sometool")


def test_cli():
    reset()
    daemons.add("a", "python3 -c pass", categories=["One", "Two"])
    daemons.add("b", "python3 -c pass", categories=["one"])
    code, out = cli("daemon-category", "list")
    names = {c["key"]: c["count"] for c in out.get("categories", [])}
    check("daemon-category list shows each category with its count", code == 0 and out.get("ok") is True and names.get("one") == 2 and names.get("two") == 1)
    code, out = cli("daemon-category", "rename", "one", "Uno")
    check("rename over the CLI reports changed and merged", code == 0 and out.get("ok") is True and out.get("changed") == 2 and out.get("merged") is False)
    check("... and the registry has it", cats("a") == ["Uno", "Two"] and cats("b") == ["Uno"])
    code, out = cli("daemon-category", "delete", "Uno")
    check("delete over the CLI", code == 0 and out.get("changed") == 2 and cats("b") == [])
    code, out = cli("daemon-category", "delete", "Uno")
    check("deleting again fails with a message, exit code non-zero", code != 0 and out.get("ok") is False and "no daemon has" in out.get("error", ""))
    code, out = cli("daemon-category", "rename", "Two")
    check("a rename with a missing argument prints usage", code != 0 and "usage" in out.get("error", ""))
    code, out = cli("daemon-category")
    check("no sub-command prints usage", code != 0 and "usage" in out.get("error", ""))


def test_registered_everywhere():
    sys.path.insert(0, str(ROOT / "jarvis-cli"))
    from jarvis import reserved_names, workspace_cli
    check("daemon-category is a workspace command", "daemon-category" in workspace_cli.COMMANDS)
    check("... and a reserved name (a saved command may not shadow it)", "daemon-category" in reserved_names.RESERVED_NAMES)
    data = (ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
    check("the palette registry covers it under the daemons verb", re.search(r'"covers": \[[^\]]*"daemon-category"', data) is not None)
    check("the usage text documents it", "daemon-category rename" in workspace_cli.USAGE)


def test_server_and_ui_wiring():
    server = (ROOT / "web" / "server.js").read_text(encoding="utf-8")
    for route in ("/api/daemon-categories", "/api/daemon-categories/rename", "/api/daemon-categories/delete",
                  "/api/command-categories", "/api/command-categories/rename", "/api/command-categories/delete"):
        check(f"server.js has {route}", f'"{route}"' in server)
    check("daemon category routes cannot be mistaken for /api/daemons/:id", "/api/daemons/categories" not in server)
    check("a category name starting with two dashes is refused before it reaches the CLI", "can't start with two dashes" in server and "categoryNameArg" in server)
    check("commands are renamed with the shared JS rules, not a second copy", "C.renameInList" in server and "C.removeFromList" in server)
    check("an emptied command list is dropped, never stored", "delete spec.categories" in server)
    html = (ROOT / "web" / "public" / "index.html").read_text(encoding="utf-8")
    check("index.html loads category-manager.js after category-input.js", html.index("category-input.js") < html.index("category-manager.js") < html.index('src="app.js"'))
    # The Commands panel no longer has a header button that opens the dialog chain: its
    # categories are a dropdown under the search box (category-menu.js) that filters and
    # manages in place, through the same two routes. The Daemons panel keeps the manager.
    check("the Daemons panel keeps its Manage categories button", 'id="btn-daemon-categories"' in html)
    check("the Commands panel has the dropdown slot and no dialog-chain button",
          'id="cmd-cat-filter"' in html and 'id="btn-command-categories"' not in html)
    check("index.html loads category-menu.js after category-input.js and before app.js",
          html.index('<script src="category-input.js">') < html.index('<script src="category-menu.js">') < html.index('src="app.js"'))
    check("the Daemons panel opens the manager; the Commands panel builds the dropdown",
          "JarvisCategoryManager" in (ROOT / "web" / "public" / "daemons.js").read_text(encoding="utf-8")
          and "JarvisCategoryMenu" in (ROOT / "web" / "public" / "app.js").read_text(encoding="utf-8"))
    menu_js = (ROOT / "web" / "public" / "category-menu.js").read_text(encoding="utf-8")
    check("the dropdown talks to the same two routes the server already has",
          "/api/command-categories" in menu_js and '"/rename"' in menu_js and '"/delete"' in menu_js)
    check("the dropdown never builds markup from a name", ".innerHTML" not in menu_js)
    mgr = (ROOT / "web" / "public" / "category-manager.js").read_text(encoding="utf-8")
    check("the manager never builds markup from a name", ".innerHTML" not in mgr)


try:
    test_list_helpers()
    test_registry()
    test_builtins_and_owned()
    test_cli()
    test_registered_everywhere()
    test_server_and_ui_wiring()
finally:
    shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
