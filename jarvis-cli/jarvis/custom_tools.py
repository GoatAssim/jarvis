"""Run an arbitrary, ad-hoc shell command — not one of the user's saved
commands.json entries (see command_tools.py for those). This is the most
powerful, and most dangerous, tool Jarvis has: it can do anything a saved
command could, plus whatever the model comes up with on the fly.

tool_safety.py defaults BOTH confirm_required and ai_review to True for
this one tool specifically. That means, by default, every call goes
through: the model decides to call it -> the user is shown the exact
command and a second AI provider's plain-language danger assessment -> the
user says yes or no. All three have to agree before anything runs. Either
flag can be turned off per-tool from the web console's Debug dashboard, but
nothing here does that on its own.
"""

import subprocess

CUSTOM_TOOL_SCHEMAS = [
    {
        "name": "run_custom_command",
        "description": (
            "Run an arbitrary shell command that isn't one of the user's saved "
            "commands. Powerful and potentially irreversible — only use this "
            "when no existing tool or saved command already does the job. "
            "Always confirmed by the user, and by default reviewed by a second "
            "AI provider for risk, before it actually runs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The full shell command line to run, exactly as it should be executed.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Max seconds to let it run before killing it. Defaults to 20.",
                },
            },
            "required": ["command"],
        },
    },
]

DEFAULT_TIMEOUT = 20.0
MAX_OUTPUT_CHARS = 8000


def run_custom_command(arguments):
    arguments = arguments or {}
    command = arguments.get("command")
    if not command or not isinstance(command, str) or not command.strip():
        return {"error": "command is required"}

    timeout = arguments.get("timeout")
    try:
        timeout = float(timeout) if timeout else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return {
            "ok": result.returncode == 0,
            "command": command,
            "exit_code": result.returncode,
            "stdout": (result.stdout or "")[:MAX_OUTPUT_CHARS],
            "stderr": (result.stderr or "")[:MAX_OUTPUT_CHARS // 2],
        }
    except subprocess.TimeoutExpired:
        return {"error": f"command timed out after {timeout}s", "command": command}
    except OSError as e:
        return {"error": f"couldn't run command: {e}", "command": command}


CUSTOM_TOOLS = {"run_custom_command": run_custom_command}
