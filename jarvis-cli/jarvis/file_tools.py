"""File-system write tool: lets Jarvis create or overwrite a text file.

Deliberately narrow — text content only, no binary writes, no delete — and
paired with tool_safety.py's confirm_required (on by default for this tool)
so nothing gets written to disk without the user explicitly saying yes
first. See ai_client.py's tool-call confirmation gate for how that's wired
up, and cli.py's on_confirm_request for what the prompt actually looks like.
"""

from pathlib import Path

FILE_TOOL_SCHEMAS = [
    {
        "name": "write_file",
        "description": (
            "Create a new text file, or overwrite/append to an existing one, at a "
            "given path. Use for saving notes, generated code, config snippets, "
            "logs, drafts, etc. Always confirmed with the user first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write to. Relative paths resolve against the user's home directory.",
                },
                "content": {
                    "type": "string",
                    "description": "Full text content to write.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "create_only"],
                    "description": (
                        "overwrite (default): replace the file if it already exists. "
                        "append: add to the end of an existing file (or create it). "
                        "create_only: fail instead of touching anything if the file already exists."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
]


def _resolve_path(raw_path):
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    return p


def write_file(arguments):
    arguments = arguments or {}
    raw_path = arguments.get("path")
    content = arguments.get("content")
    mode = (arguments.get("mode") or "overwrite").strip().lower()

    if not raw_path or not isinstance(raw_path, str):
        return {"error": "path is required"}
    if content is None or not isinstance(content, str):
        return {"error": "content (text) is required"}
    if mode not in ("overwrite", "append", "create_only"):
        return {"error": f"unknown mode: {mode!r} (expected overwrite, append, or create_only)"}

    path = _resolve_path(raw_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "create_only" and path.exists():
            return {"error": f"{path} already exists (mode is create_only)"}
        if mode == "append":
            with path.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "path": str(path),
            "mode": mode,
            "bytes_written": len(content.encode("utf-8")),
        }
    except OSError as e:
        return {"error": f"couldn't write {path}: {e}"}


FILE_TOOLS = {"write_file": write_file}
