"""Load, save, and validate ~/.jarvis/commands.json.

Shared by the CLI, web UI, and AI command tools so every path uses the
same rules for names and specs.
"""

import json
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
CONFIG_FILE = JARVIS_DIR / "commands.json"
ENCODING = "utf-8"

RESERVED_NAMES = {"config", "ai-config", "ai-clear", "ai-drop-from", "playnite-config", "spotify-config", "spotify-login", "memory-config", "tools-list", "then", "and", "-h", "--help"}


def ensure_config():
    from .cli import DEFAULT_CONFIG, ensure_config as cli_ensure

    cli_ensure()


def load_commands_dict():
    """Return the commands dict, or {} if the file is unreadable."""
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    commands = data.get("commands", {})
    if not isinstance(commands, dict):
        return {}
    return {n: s for n, s in commands.items() if isinstance(s, dict)}


def save_commands_dict(commands):
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["commands"] = commands
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)


def validate_command_name(name, *, forbid_existing=False, existing=None):
    if not isinstance(name, str) or not name.strip():
        return "Command name can't be empty."
    if any(ch.isspace() for ch in name):
        return "Command name can't contain spaces."
    if any(ch in name for ch in "\r\n\0"):
        return "Command name contains invalid characters."
    if name in RESERVED_NAMES:
        return f'"{name}" is reserved by jarvis.'
    if forbid_existing and existing and name in existing:
        return f'A command named "{name}" already exists.'
    return None


def validate_command_spec(spec):
    if not isinstance(spec, dict):
        return "Command spec must be an object."
    if spec.get("run") is None:
        return "Command must have a 'run' (string or list of steps)."
    steps = spec["run"] if isinstance(spec["run"], list) else [spec["run"]]
    if not steps:
        return "Command's 'run' list can't be empty."
    var_names = list((spec.get("vars") or {}).keys())
    for i, step in enumerate(steps, start=1):
        if isinstance(step, str):
            continue
        if isinstance(step, dict) and isinstance(step.get("run"), str):
            for field in ("if", "unless"):
                val = step.get(field)
                if val is not None and not isinstance(val, (str, dict)):
                    return f"Step {i}: '{field}' must be a string or object."
            for field in ("parallel", "showCommand", "continueOnError"):
                val = step.get(field)
                if val is not None and not isinstance(val, bool):
                    return f"Step {i}: '{field}' must be true or false."
            cond = step.get("if")
            if isinstance(cond, dict):
                for key in cond:
                    if key not in var_names:
                        return (
                            f'Step {i}: condition uses unknown variable "{key}" '
                            f"(vars: {', '.join(var_names) or '(none)'})."
                        )
            continue
        return f"Step {i} must be a string or an object with a 'run' field."
    vars_block = spec.get("vars")
    if vars_block is not None and (not isinstance(vars_block, dict) or isinstance(vars_block, list)):
        return "'vars' must be an object."
    return None
