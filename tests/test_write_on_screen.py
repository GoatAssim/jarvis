"""Tests for jarvis/desktop_tools.py's write_on_screen — type_text's
"send it" sibling (see its docstring). No real pyautogui is exercised;
a fake stand-in records calls so we can assert on typed text + Enter.

Runnable directly: python3 tests/test_write_on_screen.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import desktop_tools, tool_safety  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


class FakePyAutoGUI:
    def __init__(self):
        self.calls = []

    def write(self, text, interval=0.0):
        self.calls.append(("write", text, interval))

    def press(self, key):
        self.calls.append(("press", key))


def test_write_on_screen_types_and_submits_by_default():
    fake = FakePyAutoGUI()
    orig = desktop_tools.pyautogui
    desktop_tools.pyautogui = fake
    try:
        result = desktop_tools.tool_write_on_screen({"text": "continue"})
        check("reports ok", result.get("ok") is True, result)
        check("reports it typed the given text", result.get("typed") == "continue", result)
        check("reports it submitted", result.get("submitted") is True, result)
        check("pyautogui.write was called with the text",
              fake.calls[0] == ("write", "continue", 0.02), fake.calls)
        check("pyautogui.press('enter') was called afterward",
              fake.calls[1] == ("press", "enter"), fake.calls)
    finally:
        desktop_tools.pyautogui = orig


def test_write_on_screen_can_skip_submit():
    fake = FakePyAutoGUI()
    orig = desktop_tools.pyautogui
    desktop_tools.pyautogui = fake
    try:
        result = desktop_tools.tool_write_on_screen({"text": "draft only", "press_enter": False})
        check("reports not submitted", result.get("submitted") is False, result)
        check("only write was called, no press", fake.calls == [("write", "draft only", 0.02)], fake.calls)
    finally:
        desktop_tools.pyautogui = orig


def test_write_on_screen_requires_text():
    fake = FakePyAutoGUI()
    orig = desktop_tools.pyautogui
    desktop_tools.pyautogui = fake
    try:
        result = desktop_tools.tool_write_on_screen({})
        check("missing text is an error, nothing typed",
              "error" in result and fake.calls == [], result)
    finally:
        desktop_tools.pyautogui = orig


def test_write_on_screen_reports_missing_pyautogui():
    orig = desktop_tools.pyautogui
    desktop_tools.pyautogui = None
    try:
        result = desktop_tools.tool_write_on_screen({"text": "hi"})
        check("a clear error is returned when pyautogui isn't installed",
              "error" in result and "pyautogui" in result["error"], result)
    finally:
        desktop_tools.pyautogui = orig


def test_write_on_screen_clamps_interval():
    fake = FakePyAutoGUI()
    orig = desktop_tools.pyautogui
    desktop_tools.pyautogui = fake
    try:
        desktop_tools.tool_write_on_screen({"text": "x", "interval": 5})
        check("an out-of-range interval is clamped to 1.0", fake.calls[0][2] == 1.0, fake.calls)
    finally:
        desktop_tools.pyautogui = orig


def test_write_on_screen_is_registered_and_confirm_gated():
    check("write_on_screen is in the DESKTOP_TOOLS dispatch table",
          "write_on_screen" in desktop_tools.DESKTOP_TOOLS)
    check("write_on_screen has a schema entry",
          any(s["name"] == "write_on_screen" for s in desktop_tools.DESKTOP_TOOL_SCHEMAS))
    check("write_on_screen defaults to confirm_required, same as type_text",
          "write_on_screen" in tool_safety.DEFAULT_CONFIRM_REQUIRED)


for fn in [
    test_write_on_screen_types_and_submits_by_default,
    test_write_on_screen_can_skip_submit,
    test_write_on_screen_requires_text,
    test_write_on_screen_reports_missing_pyautogui,
    test_write_on_screen_clamps_interval,
    test_write_on_screen_is_registered_and_confirm_gated,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
