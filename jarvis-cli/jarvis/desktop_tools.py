"""Desktop input/window automation for Jarvis, via pyautogui (+ pygetwindow,
which ships with pyautogui).

Deliberately scoped to the cheap, blind-action tier plus basic window
awareness — no OCR/vision (click_on_text, locate_image_on_screen). Those
cost a real screenshot + extra processing per call and belong in a
separate module if/when they're wanted; keeping this one blind-only means
every tool here is fast, free of vision tokens, and safe to default
confirm_required=True on the ones that actually move the mouse or type.

Each tool takes a single `args` dict (never raises) and returns a plain
JSON-serializable dict — either the real result or {"error": "..."}.
"""

import platform

try:
    import pyautogui
    pyautogui.FAILSAFE = True
except ImportError:
    pyautogui = None

try:
    import pygetwindow as gw
except ImportError:
    gw = None


def _no_pyautogui():
    return {
        "error": (
            "pyautogui is not installed. Install it with: "
            "pip install pyautogui"
        )
    }


def _no_pygetwindow():
    return {
        "error": (
            "pygetwindow is not installed (it ships with pyautogui, so a "
            "plain `pip install pyautogui` should also fix this)."
        )
    }


VALID_BUTTONS = {"left", "right", "middle"}
VALID_DIRECTIONS = {"up", "down", "left", "right"}


# ---------------------------------------------------------------------------
# Cheap tier — keyboard, mouse, raw coordinates
# ---------------------------------------------------------------------------

def tool_type_text(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    text = args.get("text")
    if not text:
        return {"error": "text is required"}
    interval = args.get("interval", 0.02)
    try:
        interval = max(0.0, min(1.0, float(interval)))
    except (TypeError, ValueError):
        interval = 0.02
    try:
        pyautogui.write(str(text), interval=interval)
    except Exception as e:
        return {"error": f"type_text failed: {e}"}
    return {"ok": True, "typed": text}


def tool_press_key(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    key = args.get("key")
    if not key:
        return {"error": "key is required"}
    presses = args.get("presses", 1)
    try:
        presses = max(1, min(20, int(presses)))
    except (TypeError, ValueError):
        presses = 1
    try:
        pyautogui.press(str(key), presses=presses)
    except Exception as e:
        return {"error": f"press_key failed: {e}"}
    return {"ok": True, "key": key, "presses": presses}


def tool_hotkey(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    keys = args.get("keys")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split("+") if k.strip()]
    if not keys or not isinstance(keys, list):
        return {"error": "keys is required (list of key names, or 'ctrl+s' style string)"}
    try:
        pyautogui.hotkey(*keys)
    except Exception as e:
        return {"error": f"hotkey failed: {e}"}
    return {"ok": True, "keys": keys}


def tool_scroll(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    try:
        amount = int(args.get("amount", 0))
    except (TypeError, ValueError):
        return {"error": "amount must be an integer"}
    if amount == 0:
        return {"error": "amount is required and must be nonzero"}
    direction = (args.get("direction") or "down").lower()
    if direction not in VALID_DIRECTIONS:
        return {"error": f"direction must be one of {sorted(VALID_DIRECTIONS)}"}
    try:
        if direction == "up":
            pyautogui.scroll(abs(amount))
        elif direction == "down":
            pyautogui.scroll(-abs(amount))
        elif direction == "left":
            pyautogui.hscroll(-abs(amount))
        else:  # right
            pyautogui.hscroll(abs(amount))
    except Exception as e:
        return {"error": f"scroll failed: {e}"}
    return {"ok": True, "amount": amount, "direction": direction}


def tool_move_mouse(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    try:
        x = int(args.get("x"))
        y = int(args.get("y"))
    except (TypeError, ValueError):
        return {"error": "x and y are required integers"}
    try:
        duration = float(args.get("duration", 0.2))
    except (TypeError, ValueError):
        duration = 0.2
    duration = max(0.0, min(5.0, duration))
    try:
        pyautogui.moveTo(x, y, duration=duration)
    except Exception as e:
        return {"error": f"move_mouse failed: {e}"}
    return {"ok": True, "x": x, "y": y, "duration": duration}


def tool_click(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    x = args.get("x")
    y = args.get("y")
    button = (args.get("button") or "left").lower()
    if button not in VALID_BUTTONS:
        return {"error": f"button must be one of {sorted(VALID_BUTTONS)}"}
    clicks = args.get("clicks", 1)
    try:
        clicks = max(1, min(3, int(clicks)))
    except (TypeError, ValueError):
        clicks = 1
    kwargs = {"button": button, "clicks": clicks}
    if x is not None and y is not None:
        try:
            kwargs["x"] = int(x)
            kwargs["y"] = int(y)
        except (TypeError, ValueError):
            return {"error": "x and y must be integers when provided"}
    try:
        pyautogui.click(**kwargs)
    except Exception as e:
        return {"error": f"click failed: {e}"}
    pos = pyautogui.position()
    return {"ok": True, "x": pos.x, "y": pos.y, "button": button, "clicks": clicks}


def tool_drag(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    args = args or {}
    try:
        x1 = int(args.get("x1"))
        y1 = int(args.get("y1"))
        x2 = int(args.get("x2"))
        y2 = int(args.get("y2"))
    except (TypeError, ValueError):
        return {"error": "x1, y1, x2, y2 are required integers"}
    button = (args.get("button") or "left").lower()
    if button not in VALID_BUTTONS:
        return {"error": f"button must be one of {sorted(VALID_BUTTONS)}"}
    try:
        duration = float(args.get("duration", 0.3))
    except (TypeError, ValueError):
        duration = 0.3
    duration = max(0.0, min(5.0, duration))
    try:
        pyautogui.moveTo(x1, y1, duration=min(0.2, duration))
        pyautogui.dragTo(x2, y2, duration=duration, button=button)
    except Exception as e:
        return {"error": f"drag failed: {e}"}
    return {"ok": True, "from": [x1, y1], "to": [x2, y2], "button": button}


def tool_get_screen_size(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    try:
        size = pyautogui.size()
    except Exception as e:
        return {"error": f"get_screen_size failed: {e}"}
    return {"width": size.width, "height": size.height}


def tool_get_mouse_position(args=None):
    if pyautogui is None:
        return _no_pyautogui()
    try:
        pos = pyautogui.position()
    except Exception as e:
        return {"error": f"get_mouse_position failed: {e}"}
    return {"x": pos.x, "y": pos.y}


# ---------------------------------------------------------------------------
# Medium tier — window/app awareness (pygetwindow)
# ---------------------------------------------------------------------------

def _window_summary(win):
    try:
        return {
            "title": win.title,
            "left": win.left,
            "top": win.top,
            "width": win.width,
            "height": win.height,
            "isActive": bool(getattr(win, "isActive", False)),
            "isMinimized": bool(getattr(win, "isMinimized", False)),
        }
    except Exception:
        return {"title": getattr(win, "title", "")}


def tool_list_windows(args=None):
    if gw is None:
        return _no_pygetwindow()
    try:
        wins = gw.getAllWindows()
    except Exception as e:
        return {"error": f"list_windows failed: {e}"}
    windows = [_window_summary(w) for w in wins if (w.title or "").strip()]
    return {"ok": True, "count": len(windows), "windows": windows}


def tool_focus_window(args=None):
    if gw is None:
        return _no_pygetwindow()
    args = args or {}
    title = args.get("title")
    if not title:
        return {"error": "title is required"}
    try:
        matches = gw.getWindowsWithTitle(str(title))
    except Exception as e:
        return {"error": f"focus_window failed: {e}"}
    if not matches:
        return {"error": f"no window found matching title: {title!r}"}
    win = matches[0]
    try:
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
    except Exception as e:
        # Windows sometimes refuses activate() from a background process;
        # report it plainly rather than pretending it worked.
        return {"error": f"focus_window failed to activate: {e}", "title": win.title}
    return {"ok": True, "title": win.title}


def tool_get_active_window(args=None):
    if gw is None:
        return _no_pygetwindow()
    try:
        win = gw.getActiveWindow()
    except Exception as e:
        return {"error": f"get_active_window failed: {e}"}
    if win is None:
        return {"ok": True, "title": None}
    return {"ok": True, **_window_summary(win)}


def _find_window(title):
    """Shared lookup for the two by-title tools below \u2014 same (partial,
    first-match) semantics as tool_focus_window."""
    matches = gw.getWindowsWithTitle(str(title))
    if not matches:
        return None, {"error": f"no window found matching title: {title!r}"}
    return matches[0], None


def tool_get_window_size(args=None):
    # Position/size only, by title \u2014 for when a caller already knows which
    # window it wants (e.g. right before a coordinate-based click) and
    # doesn't need list_windows' full enumeration of every open window
    # just to get one window's rect.
    if gw is None:
        return _no_pygetwindow()
    args = args or {}
    title = args.get("title")
    if not title:
        return {"error": "title is required"}
    try:
        win, err = _find_window(title)
    except Exception as e:
        return {"error": f"get_window_size failed: {e}"}
    if err:
        return err
    try:
        return {"ok": True, "title": win.title, "left": win.left, "top": win.top, "width": win.width, "height": win.height}
    except Exception as e:
        return {"error": f"get_window_size failed: {e}"}


def tool_get_window_info(args=None):
    # Same targeted-by-title idea as get_window_size, but the full summary
    # (title/left/top/width/height/isActive/isMinimized) \u2014 for when the
    # caller wants to check on one specific window's full state without
    # paying for list_windows' whole-desktop enumeration.
    if gw is None:
        return _no_pygetwindow()
    args = args or {}
    title = args.get("title")
    if not title:
        return {"error": "title is required"}
    try:
        win, err = _find_window(title)
    except Exception as e:
        return {"error": f"get_window_info failed: {e}"}
    if err:
        return err
    return {"ok": True, **_window_summary(win)}


DESKTOP_TOOL_SCHEMAS = [
    {
        "name": "type_text",
        "description": (
            "Type a string of text at whatever currently has keyboard focus "
            "(a search box, chat window, address bar, etc.). Use this instead "
            "of OCR/vision when you just need to fill in a field — e.g. "
            "typing a song name into Spotify's search box after focus_window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to type."},
                "interval": {
                    "type": "number",
                    "description": "Seconds between keystrokes, 0-1 (default 0.02).",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": (
            "Press a single key (e.g. 'enter', 'esc', 'tab', 'f5', 'up'). "
            "For key combinations use hotkey instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name, e.g. 'enter'."},
                "presses": {"type": "integer", "description": "How many times to press it (default 1)."},
            },
            "required": ["key"],
        },
    },
    {
        "name": "hotkey",
        "description": (
            "Send a key combination, e.g. ['ctrl','s'] or 'ctrl+s', 'alt+tab', "
            "'win+d'. Most desktop automation is really 'press the right "
            "shortcut', not clicking — prefer this over click when a shortcut "
            "exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {
                    "description": "List of key names to hold together, e.g. ['ctrl','s']. A 'ctrl+s' style string also works.",
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "string"},
                    ],
                },
            },
            "required": ["keys"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the mouse wheel up/down/left/right by an amount.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Scroll amount (positive number of wheel units)."},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Scroll direction (default 'down').",
                },
            },
            "required": ["amount"],
        },
    },
    {
        "name": "move_mouse",
        "description": "Move the mouse cursor to absolute screen coordinates (x, y).",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "duration": {"type": "number", "description": "Seconds to glide over (default 0.2, max 5)."},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click the mouse. If x/y are given, moves there first; otherwise "
            "clicks at the current cursor position. Only useful when you (or "
            "the model) already know where the target is on screen — brittle "
            "across resolutions/window positions, so prefer hotkey/press_key "
            "when a keyboard shortcut exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Optional. X coordinate to click at."},
                "y": {"type": "integer", "description": "Optional. Y coordinate to click at."},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Default 'left'."},
                "clicks": {"type": "integer", "description": "1 for single click, 2 for double-click (default 1)."},
            },
            "required": [],
        },
    },
    {
        "name": "drag",
        "description": "Drag the mouse from (x1, y1) to (x2, y2), e.g. for drag-and-drop.",
        "parameters": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"},
                "y1": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Default 'left'."},
                "duration": {"type": "number", "description": "Seconds the drag takes (default 0.3, max 5)."},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "get_screen_size",
        "description": "Read-only. Get the primary screen's width/height in pixels.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_mouse_position",
        "description": "Read-only. Get the current mouse cursor's (x, y) position.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_windows",
        "description": (
            "Read-only. Enumerate open window titles (and their position/size/"
            "state) so you know what's on screen before deciding what to "
            "click or focus."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "focus_window",
        "description": (
            "Bring a specific app/window to the front by (partial) title match, "
            "e.g. focus 'Spotify' before sending keystrokes to it. Fixes most "
            "'clicked/typed into the wrong window' failures — use this before "
            "type_text/hotkey/press_key whenever the target app might not "
            "already have focus."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Window title or substring to match, e.g. 'Spotify'."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_active_window",
        "description": "Read-only. Get the title/position/size of whatever window currently has focus.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_window_size",
        "description": (
            "Read-only. Get just the position/size (left/top/width/height) of "
            "one window by (partial) title match. Cheaper than list_windows "
            "when you already know which window you want and only need its "
            "rect \u2014 e.g. right before a coordinate-based click or drag."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Window title or substring to match, e.g. 'Spotify'."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_window_info",
        "description": (
            "Read-only. Get the full title/position/size/active/minimized "
            "state of one window by (partial) title match. Cheaper than "
            "list_windows when you already know which window you want and "
            "just need to check on that one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Window title or substring to match, e.g. 'Spotify'."},
            },
            "required": ["title"],
        },
    },
]

DESKTOP_TOOLS = {
    "type_text": tool_type_text,
    "press_key": tool_press_key,
    "hotkey": tool_hotkey,
    "scroll": tool_scroll,
    "move_mouse": tool_move_mouse,
    "click": tool_click,
    "drag": tool_drag,
    "get_screen_size": tool_get_screen_size,
    "get_mouse_position": tool_get_mouse_position,
    "list_windows": tool_list_windows,
    "focus_window": tool_focus_window,
    "get_active_window": tool_get_active_window,
    "get_window_size": tool_get_window_size,
    "get_window_info": tool_get_window_info,
}