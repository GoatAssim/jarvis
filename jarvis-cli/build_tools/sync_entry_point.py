"""Rewrites jarvis-cli/pyproject.toml's [project.scripts] entry so the
console-script/exe that `pip install .` generates is named after whatever
persona is currently equipped, instead of always being "jarvis".

Run by script.bat, from jarvis-cli/, BEFORE `pip install .`:

    python build_tools\\sync_entry_point.py

Prints the resolved CLI name to stdout on success (nothing else), so the
caller can capture it the same way it captures persona_name.py's output.
Exits non-zero with a message on stderr if pyproject.toml doesn't look
like what we expect, rather than silently corrupting it.

Only the *key* in `<name> = "jarvis.cli:entry"` ever changes. The value
(the actual import target) never does — this only ever changes what you
type on the command line, never what code actually runs.

Idempotent and safe to run every single build: if the name hasn't changed
since the last build, the file is left untouched (no needless rewrite/
git-dirtying), and if it has, whatever the previous name happened to be
is found and replaced by matching on the value, not the old key — so it
doesn't matter whether the last build was "jarvis", "friday", or anything
else.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.persona_name import current_cli_name  # noqa: E402

ENTRY_TARGET = "jarvis.cli:entry"
PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Matches a single `[project.scripts]` table entry whose value is our
# entry target, however it's currently spelled/quoted, e.g.:
#   jarvis = "jarvis.cli:entry"
#   friday   = "jarvis.cli:entry"
_SCRIPT_LINE_RE = re.compile(
    r'(?m)^[ \t]*([A-Za-z0-9_.\-]+)[ \t]*=[ \t]*"'
    + re.escape(ENTRY_TARGET)
    + r'"[ \t]*$'
)


def sync_entry_point(pyproject_path=PYPROJECT_PATH):
    cli_name = current_cli_name()
    text = pyproject_path.read_text(encoding="utf-8")

    match = _SCRIPT_LINE_RE.search(text)
    if not match:
        raise RuntimeError(
            f"Couldn't find a `<name> = \"{ENTRY_TARGET}\"` line in "
            f"{pyproject_path} to update. Has [project.scripts] been "
            f"restructured? Leaving the file untouched."
        )

    current_name = match.group(1)
    if current_name == cli_name:
        return cli_name  # Already correct — nothing to rewrite.

    new_line = f'{cli_name} = "{ENTRY_TARGET}"'
    new_text = text[: match.start()] + new_line + text[match.end() :]
    pyproject_path.write_text(new_text, encoding="utf-8")
    return cli_name


def main():
    try:
        cli_name = sync_entry_point()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    # Deliberately only the name on stdout — mirrors persona_name.py so
    # the batch script can capture it the same way.
    sys.stdout.write(cli_name)


if __name__ == "__main__":
    main()
