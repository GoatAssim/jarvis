"""Brief 6G: the Steam launcher lives in actions/steam_tools.py, never in hub/steam.py.

hub/steam.py is the watcher: it runs every few seconds and must stay read-only and inert (AGENTS.md invariant,
N5).  The one thing Jarvis can ask Steam to do (open its install dialog, steam://install/<appid>) is in
actions/steam_tools.py, which runs only when the owner asks for an install.  tests/test_hub_steam_alive.py checks
the imports and the steam:// count of hub/steam.py; this file checks WHERE the launcher is, by reading the source
(ast) and by calling it with every way out of the process replaced by something that raises or records.

  * hub/steam.py has no install_url, INSTALL_URL, _open_url, OPEN_URL or launch_install (as a name, an
    assignment, a def or an import);
  * actions/steam_tools.py has all five;
  * steam_tools.py imports subprocess only inside a function;
  * the URL it will open for "730" is steam://install/730, and for "1;calc", "../x", "" and 11 digits it is None;
  * launch_install with OPEN_URL swapped for a recorder opens exactly that URL once, and never touches the real
    os.startfile or subprocess.Popen (both are patched to raise if called);
  * the default launcher, against FAKE os.startfile / subprocess.Popen: Windows hands the URL to os.startfile,
    anywhere else it runs open / xdg-open with the three DEVNULLs.  (Fakes only: the real ones never run.)

No network, no Steam, no Windows.  Run: python tests/test_steam_launcher_placement.py
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis.actions import steam_tools as st  # noqa: E402
from jarvis.hub import steam  # noqa: E402

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


MOVED = ("INSTALL_URL", "install_url", "_open_url", "OPEN_URL", "launch_install")
STEAM_PY = ROOT / "jarvis-cli" / "jarvis" / "hub" / "steam.py"
TOOLS_PY = ROOT / "jarvis-cli" / "jarvis" / "actions" / "steam_tools.py"
steam_tree = ast.parse(STEAM_PY.read_text(encoding="utf-8"))
tools_tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))


def defined_names(tree):
    """Every name the module binds anywhere: def, class, assignment target, import alias, and any bare name used."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


# --------------------------------------------------------------------------- the watcher has none of it
steam_names = defined_names(steam_tree)
for name in MOVED:
    check(f"hub/steam.py has no {name}", name not in steam_names and not hasattr(steam, name))
check("hub/steam.py has no launch_install alias either (the file cannot launch anything)",
      not any(n for n in steam_names if "launch" in n.lower() and "install" in n.lower()))

# --------------------------------------------------------------------------- the install tool module has all of it
tools_top = set()
for node in tools_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        tools_top.add(node.name)
    elif isinstance(node, ast.Assign):
        tools_top |= {t.id for t in node.targets if isinstance(t, ast.Name)}
for name in MOVED:
    check(f"actions/steam_tools.py defines {name} at module level", name in tools_top and hasattr(st, name))
check("the tool calls the local launch_install, not one on the watcher module",
      any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "launch_install" for n in ast.walk(tools_tree))
      and not any(isinstance(n, ast.Attribute) and n.attr in MOVED and isinstance(n.value, ast.Name) and n.value.id == "steam"
                  for n in ast.walk(tools_tree)))

# --------------------------------------------------------------------------- subprocess only inside a function


def subprocess_imports(tree):
    """-> list of (inside_a_function, line) for every import of subprocess."""
    found = []

    def visit(node, in_func):
        for child in ast.iter_child_nodes(node):
            inner = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(child, ast.Import) and any(a.name.split(".")[0] == "subprocess" for a in child.names):
                found.append((in_func, child.lineno))
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and (child.module or "").split(".")[0] == "subprocess":
                found.append((in_func, child.lineno))
            visit(child, inner)

    visit(tree, False)
    return found


imports = subprocess_imports(tools_tree)
check("steam_tools.py imports subprocess exactly once, and only inside a function",
      len(imports) == 1 and all(inside for inside, _ in imports))
check("hub/steam.py imports subprocess nowhere, not even inside a function", subprocess_imports(steam_tree) == [])

# --------------------------------------------------------------------------- the URL it will open
check("730 -> steam://install/730", st.install_url("730") == "steam://install/730")
check("an app id given as a number works too", st.install_url(730) == "steam://install/730")
for bad in ("1;calc", "../x", "", "99999999999"):
    check(f"{bad!r} -> None (nothing would be opened)", st.install_url(bad) is None)
check("ten digits is the longest accepted", st.install_url("1234567890") == "steam://install/1234567890")

# --------------------------------------------------------------------------- launch_install: only OPEN_URL, only once


def _boom(*args, **kwargs):
    raise AssertionError("the real launcher was touched")


_MISSING = object()
real_startfile = getattr(os, "startfile", _MISSING)
real_popen = subprocess.Popen
real_open_url = st.OPEN_URL
opened = []
os.startfile = _boom
subprocess.Popen = _boom
try:
    st.OPEN_URL = lambda url: opened.append(url)
    result = st.launch_install("730")
    check("launch_install with OPEN_URL swapped opens exactly that URL, once",
          result == (True, "") and opened == ["steam://install/730"])
    del opened[:]
    result = st.launch_install("1;calc")
    check("a bad id opens nothing and says so", result == (False, "not a Steam app id") and opened == [])
    st.OPEN_URL = lambda url: (_ for _ in ()).throw(OSError("no handler"))
    ok, err = st.launch_install("730")
    check("a failing opener is a sentence, never an exception",
          ok is False and err == "could not hand steam://install/730 to Steam: no handler")
    # the swap is looked up at call time, on THIS module (the same way every test re-points it)
    st.OPEN_URL = real_open_url
    ok, err = st.launch_install("730")
    check("with the real opener, the patched os.startfile / Popen are what it reaches (and they refused): nothing started",
          ok is False and "the real launcher was touched" in err)
finally:
    st.OPEN_URL = real_open_url
    subprocess.Popen = real_popen
    if real_startfile is _MISSING:
        del os.startfile
    else:
        os.startfile = real_startfile
check("the module's own opener is the default again afterwards", st.OPEN_URL is st._open_url)

# --------------------------------------------------------------------------- the default opener, against FAKES
calls = {"startfile": [], "popen": []}


class _FakePopen:
    DEVNULL = subprocess.DEVNULL

    def __init__(self, argv, **kwargs):
        calls["popen"].append((argv, kwargs))


real_platform = sys.platform
try:
    os.startfile = lambda url: calls["startfile"].append(url)
    subprocess.Popen = _FakePopen
    sys.platform = "win32"
    st._open_url("steam://install/730")
    check("Windows: the URL goes to os.startfile (a fake) and nothing else", calls["startfile"] == ["steam://install/730"] and calls["popen"] == [])
    del calls["startfile"][:]
    sys.platform = "linux"
    st._open_url("steam://install/730")
    argv, kwargs = calls["popen"][0]
    check("Linux: xdg-open with the URL, all three streams DEVNULL (a fake Popen)",
          argv == ["xdg-open", "steam://install/730"] and calls["startfile"] == []
          and kwargs == {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})
    del calls["popen"][:]
    sys.platform = "darwin"
    st._open_url("steam://install/730")
    check("macOS: open with the URL (a fake Popen)", calls["popen"][0][0] == ["open", "steam://install/730"])
finally:
    sys.platform = real_platform
    subprocess.Popen = real_popen
    if real_startfile is _MISSING:
        del os.startfile
    else:
        os.startfile = real_startfile
check("the real subprocess.Popen and sys.platform are back after the fakes", subprocess.Popen is real_popen and sys.platform == real_platform)

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
