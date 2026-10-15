"""C29 (second half): the desktop tools' two dependencies, pyautogui and
pygetwindow, are now named by `jarvis doctor` and by a failed tool's
diagnosis, and a missing / unsupported one no longer stops the whole
desktop module from loading.

Before this patch:
  * `desktop_tools.py` only caught ImportError around `import pygetwindow`.
    On macOS / Linux that import raises NotImplementedError, which escaped
    and broke every desktop tool, not just the five window tools.
  * `jarvis doctor` listed pyautogui but never pygetwindow.
  * `tool_diagnosis` knew four of the ten pyautogui tools and none of the
    five window tools, so the other eleven failed with no "what to install".

No real pyautogui / pygetwindow is needed: imports are faked.

Run: python3 tests/test_c29b_desktop_deps.py
"""

import builtins
import importlib
import os
import platform
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["USERPROFILE"] = os.environ["HOME"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import desktop_tools, doctor, tool_diagnosis  # noqa: E402

PASS, FAIL = [], []

WINDOW_TOOLS = ["list_windows", "focus_window", "get_active_window",
                "get_window_size", "get_window_info"]
INPUT_TOOLS = ["type_text", "write_on_screen", "press_key", "hotkey", "scroll",
               "move_mouse", "click", "drag", "get_screen_size",
               "get_mouse_position"]


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


class _FakeImport:
    """Make `import <name>` raise `exc` for the listed module names."""

    def __init__(self, names, exc):
        self.names, self.exc = set(names), exc
        self._real = builtins.__import__

    def __enter__(self):
        real, names, exc = self._real, self.names, self.exc

        def fake(name, *a, **k):
            if name.split(".")[0] in names:
                raise exc("fake: %s unavailable" % name)
            return real(name, *a, **k)

        builtins.__import__ = fake
        return self

    def __exit__(self, *_):
        builtins.__import__ = self._real


class _FakeSystem:
    """Pretend platform.system() says `name`."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self._real = platform.system
        platform.system = lambda: self.name

    def __exit__(self, *_):
        platform.system = self._real


def _row(results, import_name):
    for r in results:
        if r.id == "py.%s" % import_name:
            return r
    return None


# --- desktop_tools survives a package that cannot be imported ---------------

def test_module_loads_when_pygetwindow_raises_notimplemented():
    try:
        with _FakeImport(["pygetwindow"], NotImplementedError):
            mod = importlib.reload(desktop_tools)
            check("gw is None, module still loaded", mod.gw is None)
            out = mod.tool_list_windows({})
            check("window tool answers with the not-installed error",
                  isinstance(out, dict) and "pygetwindow" in out.get("error", ""), out)
            check("a non-window tool is still defined", callable(mod.DESKTOP_TOOLS["get_screen_size"]))
    finally:
        importlib.reload(desktop_tools)


def test_module_loads_when_pyautogui_raises_something_odd():
    try:
        with _FakeImport(["pyautogui"], KeyError):  # e.g. no DISPLAY on Linux
            mod = importlib.reload(desktop_tools)
            check("pyautogui is None, module still loaded", mod.pyautogui is None)
            out = mod.tool_get_screen_size({})
            check("input tool answers with the not-installed error",
                  isinstance(out, dict) and "pyautogui" in out.get("error", ""), out)
    finally:
        importlib.reload(desktop_tools)


# --- doctor -----------------------------------------------------------------

def test_doctor_table_has_pygetwindow():
    entry = doctor._PY_PACKAGES.get("pygetwindow") or {}
    check("pygetwindow is in the packages table", bool(entry))
    check("fixed by the desktop extra", entry.get("extra") == "desktop", entry)
    check("marked Windows-only", entry.get("windows_only") is True, entry)
    for t in WINDOW_TOOLS:
        check("feature text names %s" % t, t in entry.get("feature", ""), entry.get("feature"))


def test_doctor_missing_on_windows():
    with _FakeImport(["pygetwindow"], ImportError), _FakeSystem("Windows"):
        row = _row(doctor.check_python_packages(), "pygetwindow")
    check("row exists", row is not None)
    if row:
        check("skip, like the other optional packages", row.status == doctor.SKIP, row.to_dict())
        check("detail says the window tools are unavailable",
              "window tools" in row.detail and "unavailable" in row.detail, row.detail)
        check("fix is the desktop extra", "jarvis-cli[desktop]" in row.fix, row.fix)


def test_doctor_unsupported_platform_gives_no_pip_advice():
    with _FakeImport(["pygetwindow"], NotImplementedError), _FakeSystem("Linux"):
        row = _row(doctor.check_python_packages(), "pygetwindow")
    check("row exists", row is not None)
    if row:
        check("skip", row.status == doctor.SKIP, row.to_dict())
        check("says Windows only", "Windows" in row.detail, row.detail)
        check("no pip command that could not help", row.fix == "", row.fix)


def test_doctor_installed_is_ok():
    class _Fake:
        pass

    sys.modules["pygetwindow"] = _Fake()
    try:
        with _FakeSystem("Linux"):  # installed beats the platform note
            row = _row(doctor.check_python_packages(), "pygetwindow")
    finally:
        del sys.modules["pygetwindow"]
    check("installed -> ok", row is not None and row.status == doctor.OK, row and row.to_dict())


def test_doctor_other_rows_unchanged():
    with _FakeImport(["pyautogui"], ImportError), _FakeSystem("Windows"):
        row = _row(doctor.check_python_packages(), "pyautogui")
    check("pyautogui row still skip with the extra",
          row is not None and row.status == doctor.SKIP and "jarvis-cli[desktop]" in row.fix,
          row and row.to_dict())


# --- failed-tool diagnosis --------------------------------------------------

def test_every_desktop_tool_has_a_dependency_entry():
    for name in desktop_tools.DESKTOP_TOOLS:
        check("%s is in _TOOL_DEPS" % name, name in tool_diagnosis._TOOL_DEPS)
    for name in WINDOW_TOOLS:
        check("%s needs pygetwindow" % name,
              tool_diagnosis._TOOL_DEPS.get(name) == {"packages": ["pygetwindow"]})
    for name in INPUT_TOOLS:
        check("%s needs pyautogui" % name,
              tool_diagnosis._TOOL_DEPS.get(name) == {"packages": ["pyautogui"]})


def test_window_tool_failure_names_the_package():
    err = desktop_tools._no_pygetwindow()
    for name in WINDOW_TOOLS:
        d = tool_diagnosis.diagnose(name, dict(err))
        names = [m["name"] for m in (d or {}).get("missing", [])]
        check("%s: diagnosis names PyGetWindow" % name, names == ["PyGetWindow"], d)
        check("%s: fix is a pip command" % name, "pip install PyGetWindow" in (d or {}).get("fix", ""), d)


def test_input_tool_failure_names_the_package():
    err = desktop_tools._no_pyautogui()
    for name in INPUT_TOOLS:
        d = tool_diagnosis.diagnose(name, dict(err))
        names = [m["name"] for m in (d or {}).get("missing", [])]
        check("%s: diagnosis names pyautogui" % name, names == ["pyautogui"], d)


def test_unrelated_error_gets_no_package_blame():
    d = tool_diagnosis.diagnose("list_windows", {"error": "list_windows failed: access denied by the OS"})
    check("no `missing` list when the error does not name the package",
          not (d or {}).get("missing"), d)


def test_annotate_attaches_diagnosis():
    out = tool_diagnosis.annotate("focus_window", desktop_tools._no_pygetwindow())
    check("annotate adds a diagnosis", "diagnosis" in out, out)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]:
        print("--", fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(fn.__name__ + " ran without raising", False, repr(exc))
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
