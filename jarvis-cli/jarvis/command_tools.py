"""AI tools for running and managing jarvis commands.

Only predefined commands from commands.json — never arbitrary shell.
Returns structured needs_clarification responses instead of guessing.
"""

from . import commands_config


def _resolve_command(commands, name):
    if not isinstance(name, str) or not name.strip():
        return None, {
            "needs_clarification": True,
            "message": "Which command should I run?",
        }
    name = name.strip()
    if name in commands:
        return name, commands[name]

    lower = name.lower()
    exact_ci = [n for n in commands if n.lower() == lower]
    if len(exact_ci) == 1:
        return exact_ci[0], commands[exact_ci[0]]
    if len(exact_ci) > 1:
        return None, {
            "needs_clarification": True,
            "message": f"Several commands match '{name}'.",
            "candidates": exact_ci,
        }

    partial = [n for n in commands if lower in n.lower() or n.lower() in lower]
    if len(partial) == 1:
        return partial[0], commands[partial[0]]
    if len(partial) > 1:
        return None, {
            "needs_clarification": True,
            "message": f"Several commands match '{name}' — which one?",
            "candidates": partial,
        }
    return None, {
        "needs_clarification": True,
        "message": f"No command named '{name}'.",
        "available": sorted(commands.keys())[:12],
    }


def _missing_required_vars(spec, provided):
    provided = provided if isinstance(provided, dict) else {}
    missing = []
    for var_name, var_spec in (spec.get("vars") or {}).items():
        if not isinstance(var_spec, dict) or "default" in var_spec:
            continue
        val = provided.get(var_name)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append({
                "name": var_name,
                "description": var_spec.get("description", ""),
            })
    return missing


def _merged_vars(spec, provided):
    provided = provided if isinstance(provided, dict) else {}
    merged = {}
    for var_name, var_spec in (spec.get("vars") or {}).items():
        if var_name in provided and provided[var_name] is not None:
            val = provided[var_name]
            if not (isinstance(val, str) and not val.strip()):
                merged[var_name] = val
                continue
        if isinstance(var_spec, dict) and "default" in var_spec:
            merged[var_name] = var_spec["default"]
    return merged


def _build_argv(name, spec, provided):
    merged = _merged_vars(spec, provided)
    missing = _missing_required_vars(spec, merged)
    if missing:
        return None, missing
    argv = [name]
    for var_name, val in merged.items():
        argv.extend([f"--{var_name}", str(val)])
    return argv, None


def _run_argv_segment(commands, parser, argv):
    from .cli import resolve_and_run

    if not argv or argv[0] not in commands:
        return {"ok": False, "exit_code": 1, "error": f"Unknown command: {argv[0] if argv else '?'}"}
    code = resolve_and_run(commands, parser, argv)
    return {"command": argv[0], "ok": code == 0, "exit_code": code}


def tool_run_command(args):
    commands = commands_config.load_commands_dict()
    resolved_name, spec_or_err = _resolve_command(commands, (args or {}).get("name"))
    if resolved_name is None:
        return spec_or_err

    provided = (args or {}).get("vars")
    argv, missing = _build_argv(resolved_name, spec_or_err, provided)
    if missing:
        return {
            "needs_clarification": True,
            "command": resolved_name,
            "missing_vars": missing,
            "message": "Required variables are missing — ask the user before running.",
        }

    from .cli import build_parser

    parser = build_parser(commands)
    result = _run_argv_segment(commands, parser, argv)
    result["command"] = resolved_name
    return result


def tool_run_chain(args):
    segments = (args or {}).get("segments")
    if not isinstance(segments, list) or not segments:
        return {
            "needs_clarification": True,
            "message": "Need at least one command in the chain.",
        }

    commands = commands_config.load_commands_dict()
    from .cli import build_parser, _run_segment_batch

    parser = build_parser(commands)
    batches = []
    pending = None

    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            return {"error": f"Segment {i + 1} must be an object."}
        resolved_name, spec_or_err = _resolve_command(commands, seg.get("name"))
        if resolved_name is None:
            spec_or_err["segment"] = i + 1
            return spec_or_err

        argv, missing = _build_argv(resolved_name, spec_or_err, seg.get("vars"))
        if missing:
            return {
                "needs_clarification": True,
                "command": resolved_name,
                "segment": i + 1,
                "missing_vars": missing,
                "message": "Required variables are missing — ask the user before running.",
            }

        mode = seg.get("mode", "then" if i > 0 else "then")
        if i == 0:
            pending = [argv]
            batches = [pending]
        elif mode == "and":
            pending.append(argv)
        else:
            pending = [argv]
            batches.append(pending)

    if not batches:
        return {"needs_clarification": True, "message": "Chain has no runnable segments."}

    all_results = []
    for batch in batches:
        batch_results = _run_segment_batch(commands, parser, batch)
        for seg, code in batch_results:
            all_results.append({
                "command": seg[0],
                "ok": code == 0,
                "exit_code": code,
            })
        failures = [r for r in all_results[-len(batch_results):] if not r["ok"]]
        if failures:
            return {
                "ok": False,
                "exit_code": failures[0]["exit_code"],
                "results": all_results,
                "message": "Chain stopped on failure.",
            }

    return {"ok": True, "exit_code": 0, "results": all_results}


def tool_create_command(args):
    args = args or {}
    name = args.get("name")
    err = commands_config.validate_command_name(name, forbid_existing=True,
                                               existing=commands_config.load_commands_dict())
    if err:
        return {"error": err}

    spec = {
        "description": args.get("description") or "",
        "run": args.get("run"),
        "vars": args.get("vars") or {},
    }
    err = commands_config.validate_command_spec(spec)
    if err:
        return {"error": err}

    commands = commands_config.load_commands_dict()
    commands[name] = spec
    commands_config.save_commands_dict(commands)
    return {"ok": True, "name": name, "message": f"Created command '{name}'."}


def tool_update_command(args):
    args = args or {}
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"needs_clarification": True, "message": "Which command should I update?"}

    commands = commands_config.load_commands_dict()
    if name not in commands:
        resolved_name, spec_or_err = _resolve_command(commands, name)
        if resolved_name is None:
            return spec_or_err
        name = resolved_name

    current = dict(commands[name])
    if "description" in args and args["description"] is not None:
        current["description"] = args["description"]
    if "run" in args and args["run"] is not None:
        current["run"] = args["run"]
    if "vars" in args and args["vars"] is not None:
        current["vars"] = args["vars"]

    new_name = args.get("new_name")
    if new_name is not None:
        new_name = new_name.strip()
        if new_name != name:
            err = commands_config.validate_command_name(
                new_name, forbid_existing=True, existing=commands
            )
            if err:
                return {"error": err}

    err = commands_config.validate_command_spec(current)
    if err:
        return {"error": err}

    if new_name and new_name != name:
        del commands[name]
        commands[new_name] = current
        saved_as = new_name
    else:
        commands[name] = current
        saved_as = name

    commands_config.save_commands_dict(commands)
    return {"ok": True, "name": saved_as, "message": f"Updated command '{saved_as}'."}


# Open object — no additionalProperties (Gemini rejects that field).
_VARS_OBJ = {
    "type": "object",
    "description": 'Variable values keyed by name, e.g. {"env": "prod", "name": "World"}.',
}

COMMAND_TOOL_SCHEMAS = [
    {
        "name": "run_command",
        "description": (
            "Run one saved jarvis command. Fill all required vars from the user's message; "
            "if any required var is missing or ambiguous, do NOT call — ask the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact command name."},
                "vars": _VARS_OBJ,
            },
            "required": ["name"],
        },
    },
    {
        "name": "run_chain",
        "description": (
            "Run multiple commands. First segment starts the chain; later segments use "
            "mode 'then' (sequential, default) or 'and' (parallel with previous). "
            "Ask the user if any command or required var is unclear."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "vars": _VARS_OBJ,
                            "mode": {
                                "type": "string",
                                "enum": ["then", "and"],
                                "description": "Omit on first segment. 'then' waits; 'and' runs in parallel.",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["segments"],
        },
    },
    {
        "name": "create_command",
        "description": (
            "Create a new jarvis command in commands.json. Confirm intent with the user "
            "if the shell command or variables aren't clear."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "run": {
                    "type": "string",
                    "description": "Shell command string, or JSON array of step strings/objects.",
                },
                "vars": {
                    "type": "object",
                    "description": "Var specs: {\"varName\": {\"default\": \"x\", \"description\": \"...\"}}.",
                },
            },
            "required": ["name", "run"],
        },
    },
    {
        "name": "update_command",
        "description": (
            "Change an existing command's name, description, run script, or vars. "
            "Only pass fields that should change. Ask if unsure which command or values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current command name."},
                "new_name": {"type": "string"},
                "description": {"type": "string"},
                "run": {
                    "type": "string",
                    "description": "New run script (string or JSON array of steps).",
                },
                "vars": {"type": "object"},
            },
            "required": ["name"],
        },
    },
]

COMMAND_TOOLS = {
    "run_command": tool_run_command,
    "run_chain": tool_run_chain,
    "create_command": tool_create_command,
    "update_command": tool_update_command,
}
