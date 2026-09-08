"""Read, validate, and prettify arbitrary JSON files for humans.

Backs the `jarvis organize-json <path>` subcommand. This is pure local file
IO + stdlib `json` parsing — it never touches ai_client/ai_providers, so
pointing it at even a huge JSON file costs zero API tokens. The web server
shells out to `jarvis organize-json <path> --json` (see server.js's
`/api/json/organize`) to reuse this exact parsing/error logic instead of
reimplementing JSON-error reporting in Node.
"""

import json
from pathlib import Path


def resolve_path(raw):
    """Relative paths resolve against the home dir, same convention as
    file_tools.write_file."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    return p


def _error_snippet(text, lineno, colno, context=1):
    lines = text.splitlines() or [""]
    lo = max(0, lineno - 1 - context)
    hi = min(len(lines), lineno + context)
    out = []
    for i in range(lo, hi):
        marker = ">> " if (i + 1) == lineno else "   "
        out.append(f"{marker}{i + 1:>4} | {lines[i]}")
        if (i + 1) == lineno:
            out.append(" " * (8 + max(colno - 1, 0)) + "^")
    return "\n".join(out)


def read_json_file(raw_path):
    """Returns (data, raw_text, error). Exactly one of (data, error) is set;
    raw_text is populated whenever the file was at least readable, even if
    it failed to parse (so a caller can still show the offending line)."""
    p = resolve_path(raw_path)

    if not p.exists():
        return None, None, {"error": f"No such file: {p}"}
    if p.is_dir():
        return None, None, {"error": f"{p} is a directory, not a file."}

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return None, None, {"error": f"Couldn't read {p} as UTF-8: {e}"}
    except OSError as e:
        return None, None, {"error": f"Couldn't read {p}: {e}"}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, text, {
            "error": f"Invalid JSON in {p}: {e.msg} (line {e.lineno}, column {e.colno})",
            "line": e.lineno,
            "column": e.colno,
            "snippet": _error_snippet(text, e.lineno, e.colno),
        }

    return data, text, None


def _type_name(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _format_scalar(value, max_len=140):
    if isinstance(value, str):
        s = value if len(value) <= max_len else value[: max_len - 1] + "\u2026"
        return json.dumps(s, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def _child_entries(value):
    if isinstance(value, dict):
        return list(value.items())
    if isinstance(value, list):
        return [(f"[{i}]", v) for i, v in enumerate(value)]
    return []


def _branch_suffix(value):
    n = len(value)
    if isinstance(value, list):
        noun = "item" if n == 1 else "items"
        return f"[] ({n} {noun})" if n else "[] (empty)"
    noun = "key" if n == 1 else "keys"
    return f"{{}} ({n} {noun})" if n else "{} (empty)"


def _label_for(key, value):
    if isinstance(value, (dict, list)):
        return f"{key}: {_branch_suffix(value)}"
    return f"{key}: {_format_scalar(value)}"


def _render_children(value, prefix):
    lines = []
    entries = _child_entries(value)
    for i, (k, v) in enumerate(entries):
        last = i == len(entries) - 1
        branch = "\u2514\u2500 " if last else "\u251c\u2500 "
        lines.append(f"{prefix}{branch}{_label_for(k, v)}")
        child_prefix = prefix + ("   " if last else "\u2502  ")
        lines.extend(_render_children(v, child_prefix))
    return lines


def render_tree(data):
    """Human-readable outline (tree branches + type/size annotations) —
    deliberately NOT `json.dumps(indent=2)`; the point is to read like a
    file listing, not like source code."""
    if isinstance(data, (dict, list)):
        lines = [_branch_suffix(data)]
    else:
        lines = [_format_scalar(data)]
    lines.extend(_render_children(data, prefix=""))
    return "\n".join(lines)