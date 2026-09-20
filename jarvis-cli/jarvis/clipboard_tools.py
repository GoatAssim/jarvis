"""Cross-platform clipboard access for Jarvis.

Windows/macOS each have one obvious way to read/write the clipboard.
Linux does not: there's no universal binary, and a headless box may have
none at all (no display server). That case is handled as a clean
capability check, not a crash-and-timeout — see `_linux_precheck()`.

Follows the same shape as every other tool module (`radio_tools.py`,
`audio_tools.py`): a dispatch function per OS, plus the two module-level
exports `tools.py` splices into `CORE_TOOL_SCHEMAS`/`TOOLS`.
"""

import os
import platform
import subprocess
import time

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Mirrors web_tools.py's FETCH_MAX_CHARS pattern: cap what a tool hands
# back to the model rather than dumping an arbitrarily large blob into
# context because someone copied a whole log file. Clipboard contents are
# usually short, so this is generous, not a real limit in practice.
CLIPBOARD_MAX_CHARS = 8000

_SUBPROCESS_TIMEOUT = 5

# Linux: try in this order, since there's no single universal binary.
# Wayland compositors ship wl-clipboard; X11 desktops usually have xclip,
# and some minimal ones ship only xsel. Each entry is (get_argv, set_argv).
_LINUX_CANDIDATES = [
    (["wl-paste", "--no-newline"], ["wl-copy"]),
    (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]),
    (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]),
]


# ── per-OS backends ─────────────────────────────────────────────────────


def _get_windows():
    # -Raw preserves newlines; without it PowerShell returns a line array
    # and quietly drops blank lines.
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        creationflags=CREATE_NO_WINDOW,
    )


def _set_windows(text):
    # Value comes in via stdin, not argv, to sidestep argv length limits
    # and quoting hell for anything with quotes/newlines in it.
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
        input=text, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        creationflags=CREATE_NO_WINDOW,
    )


def _get_macos():
    return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)


def _set_macos(text):
    return subprocess.run(["pbcopy"], input=text, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)


def _linux_precheck():
    """None if a display server looks present; else the clear error to
    return immediately instead of spending 15s timing out on three
    missing binaries (SSH session, headless server, etc)."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return (
            "No display server detected (DISPLAY and WAYLAND_DISPLAY are both "
            "unset) — clipboard tools don't apply here, e.g. an SSH session or "
            "headless server."
        )
    return None


def _get_linux():
    precheck = _linux_precheck()
    if precheck:
        return None, precheck
    tried = []
    for get_argv, _set_argv in _LINUX_CANDIDATES:
        tried.append(get_argv[0])
        try:
            result = subprocess.run(get_argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
        except FileNotFoundError:
            continue
        if result.returncode != 0:
            err = (result.stderr or "").strip() or f"exit {result.returncode}"
            return None, f"{get_argv[0]} failed: {err}"
        return result.stdout, None
    return None, (
        f"No clipboard utility found (tried {', '.join(tried)}). "
        "Install one, e.g.: sudo apt install xclip"
    )


def _set_linux(text):
    precheck = _linux_precheck()
    if precheck:
        return False, precheck
    tried = []
    for _get_argv, set_argv in _LINUX_CANDIDATES:
        tried.append(set_argv[0])
        try:
            result = subprocess.run(set_argv, input=text, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
        except FileNotFoundError:
            continue
        if result.returncode != 0:
            err = (result.stderr or "").strip() or f"exit {result.returncode}"
            return False, f"{set_argv[0]} failed: {err}"
        return True, None
    return False, (
        f"No clipboard utility found (tried {', '.join(tried)}). "
        "Install one, e.g.: sudo apt install xclip"
    )


# ── OS dispatch ──────────────────────────────────────────────────────────


def _get():
    """Returns (text, error). Exactly one is non-None."""
    system = platform.system()
    if system == "Windows":
        try:
            result = _get_windows()
        except FileNotFoundError:
            return None, "PowerShell not found."
        except subprocess.TimeoutExpired:
            return None, f"Timed out after {_SUBPROCESS_TIMEOUT}s"
        if result.returncode != 0:
            return None, (result.stderr or "").strip() or f"exit {result.returncode}"
        return result.stdout, None
    if system == "Darwin":
        try:
            result = _get_macos()
        except FileNotFoundError:
            return None, "pbpaste not found."
        except subprocess.TimeoutExpired:
            return None, f"Timed out after {_SUBPROCESS_TIMEOUT}s"
        if result.returncode != 0:
            return None, (result.stderr or "").strip() or f"exit {result.returncode}"
        return result.stdout, None
    if system == "Linux":
        try:
            return _get_linux()
        except subprocess.TimeoutExpired:
            return None, f"Timed out after {_SUBPROCESS_TIMEOUT}s"
    return None, f"Clipboard access isn't wired up for {system!r}."


def _set(text):
    """Returns (ok, error)."""
    system = platform.system()
    if system == "Windows":
        try:
            result = _set_windows(text)
        except FileNotFoundError:
            return False, "PowerShell not found."
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {_SUBPROCESS_TIMEOUT}s"
        if result.returncode != 0:
            return False, (result.stderr or "").strip() or f"exit {result.returncode}"
        return True, None
    if system == "Darwin":
        try:
            result = _set_macos(text)
        except FileNotFoundError:
            return False, "pbcopy not found."
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {_SUBPROCESS_TIMEOUT}s"
        if result.returncode != 0:
            return False, (result.stderr or "").strip() or f"exit {result.returncode}"
        return True, None
    if system == "Linux":
        try:
            return _set_linux(text)
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {_SUBPROCESS_TIMEOUT}s"
    return False, f"Clipboard access isn't wired up for {system!r}."


# ── tool functions ───────────────────────────────────────────────────────


def tool_clipboard_get(args=None):
    text, err = _get()
    if err is not None:
        return {"ok": False, "error": err}
    text = text or ""
    truncated = len(text) > CLIPBOARD_MAX_CHARS
    return {"ok": True, "text": text[:CLIPBOARD_MAX_CHARS], "truncated": truncated}


def tool_clipboard_set(args):
    args = args or {}
    text = args.get("text")
    if text is None:
        return {"ok": False, "error": "text is required."}
    ok, err = _set(str(text))
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


def tool_clipboard_clear(args=None):
    ok, err = _set("")
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}


def tool_clipboard_wait_for_change(args=None):
    args = args or {}
    try:
        timeout_seconds = float(args.get("timeout_seconds", 20))
    except (TypeError, ValueError):
        timeout_seconds = 20.0
    timeout_seconds = max(1.0, min(timeout_seconds, 120.0))

    baseline, err = _get()
    if err is not None:
        return {"ok": False, "error": err}
    baseline = baseline or ""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        current, err = _get()
        if err is not None:
            return {"ok": False, "error": err}
        current = current or ""
        if current != baseline:
            truncated = len(current) > CLIPBOARD_MAX_CHARS
            return {"ok": True, "text": current[:CLIPBOARD_MAX_CHARS], "truncated": truncated}
    return {"ok": False, "timed_out": True}


CLIPBOARD_TOOL_SCHEMAS = [
    {
        "name": "clipboard_get",
        "description": (
            "Read the current text clipboard contents. Note: the clipboard is "
            "exactly where a password manager puts a password right before "
            "someone pastes it — don't restate its contents back verbatim in "
            "chat unless asked to."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clipboard_set",
        "description": "Set the text clipboard contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to place on the clipboard."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "clipboard_clear",
        "description": "Clear the clipboard (sets it to empty text).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clipboard_wait_for_change",
        "description": (
            "Block until the clipboard contents change from what they are right "
            "now, then return the new text. Use for 'copy that, then tell me "
            "when' style requests. Returns timed_out=true if nothing changed in "
            "time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait, in seconds. Default ~20, max 120.",
                },
            },
            "required": [],
        },
    },
]

CLIPBOARD_TOOLS = {
    "clipboard_get": tool_clipboard_get,
    "clipboard_set": tool_clipboard_set,
    "clipboard_clear": tool_clipboard_clear,
    "clipboard_wait_for_change": tool_clipboard_wait_for_change,
}
