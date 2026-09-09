"""Per-tool safety toggles for sensitive tool calls (write_file, run_command,
run_custom_command, etc.) — same philosophy as ai_config.py: a plain,
hand-editable JSON file at ~/.jarvis/tool_safety.json, created with sensible
defaults on first use, re-read fresh on every invocation. No separate
"apply"/reload step; a toggle flipped from the web console's Debug dashboard
takes effect on the very next tool call.

Two independent per-tool flags:

  confirm_required — pause and ask "do you want to do this? [Y/n]" (a real
    terminal prompt in the CLI, or Yes/No buttons in the web console) before
    the tool actually runs, showing its name, arguments, and (if ai_review
    is also on) a plain-language risk note.

  ai_review — before that confirmation is shown, ask a *different*
    configured AI provider than the one currently answering what the call
    will do and how dangerous it is (see ai_client._risk_review), and
    include that note alongside the prompt. Best-effort: if no second
    provider is configured, the prompt just shows without a risk note.

Both are toggled per tool from the web console's Debug dashboard, right
under a tool's description ("Toggle warning:" / "Toggle AI review:"), via
`jarvis tool-safety-set <name> <key> <true|false>`.
"""

import json
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "tool_safety.json"
ENCODING = "utf-8"

# Tools that actually mutate, delete, or execute something real default to
# confirm_required=True out of the box. Read-only lookups (get_battery,
# spotify_search, search_commands, ...) default to False for both flags —
# an explicit entry in tool_safety.json always overrides these.
DEFAULT_CONFIRM_REQUIRED = {
    "write_file",
    "run_command",
    "run_chain",
    "run_custom_command",
    "create_command",
    "update_command",
    "delete_command",
    "package_install",
    "package_uninstall",
    "memory_forget",
    "wifi_set",
    "bluetooth_set",
    "git_run",
    "ytdl_download",
    "type_text",
    "press_key",
    "hotkey",
    "click",
    "drag",
}

# run_custom_command is the one tool where a second AI opinion is on by
# default — it's arbitrary shell, so it's the whole reason ai_review exists.
DEFAULT_AI_REVIEW = {"run_custom_command"}

VALID_KEYS = ("confirm_required", "ai_review")


def ensure_config():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps({"tools": {}}, indent=2) + "\n", encoding=ENCODING)


def _load():
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {"tools": {}}
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        return {"tools": {}}
    return data


def _save(data):
    ensure_config()
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)


def get_flags(name):
    """{'confirm_required': bool, 'ai_review': bool} for one tool. An
    explicit entry in tool_safety.json always wins; otherwise falls back to
    the built-in defaults above (never raises, never returns partial dicts)."""
    data = _load()
    entry = data["tools"].get(name)
    confirm_default = name in DEFAULT_CONFIRM_REQUIRED
    review_default = name in DEFAULT_AI_REVIEW
    if not isinstance(entry, dict):
        return {"confirm_required": confirm_default, "ai_review": review_default}
    return {
        "confirm_required": bool(entry.get("confirm_required", confirm_default)),
        "ai_review": bool(entry.get("ai_review", review_default)),
    }


def requires_confirmation(name):
    return get_flags(name)["confirm_required"]


def requires_ai_review(name):
    return get_flags(name)["ai_review"]


def set_flag(name, key, value):
    if key not in VALID_KEYS:
        raise ValueError(f"unknown safety flag: {key} (expected one of {VALID_KEYS})")
    data = _load()
    entry = data["tools"].get(name)
    if not isinstance(entry, dict):
        entry = {}
    entry[key] = bool(value)
    data["tools"][name] = entry
    _save(data)
    return get_flags(name)


def all_flags(tool_names):
    """{name: {'confirm_required':..., 'ai_review':...}} for every name in
    tool_names — lets the debug dashboard paint every toggle's current
    state from a single pass."""
    return {name: get_flags(name) for name in tool_names}
