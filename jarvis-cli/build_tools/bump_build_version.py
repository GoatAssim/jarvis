"""Increments jarvis/build_info.py's BUILD_NUMBER by 1 and restamps
BUILT_AT with the current local time, every time script.bat builds.

WHY THIS EXISTS: pyproject.toml's own version string doesn't bump on every
build (script.bat's own comment on `pip install .` explains why --
--force-reinstall is required because pip only checks name==version and
silently no-ops otherwise). That means, without a separate counter, there
is no way to look at a running jarvis.exe and tell "this is actually the
build I just did" from "pip quietly did nothing and I'm still on
yesterday's exe, even though every build step printed success". This
counter is the one thing guaranteed to change every single build,
regardless of whether any source file actually changed.

Run by script.bat, from jarvis-cli/, BEFORE `pip install .` (so the bumped
number is baked into the wheel that gets installed this run):

    python build_tools\\bump_build_version.py

Prints the new build number to stdout on success (nothing else) --
mirrors sync_entry_point.py / find_cli_exe.py so the caller can capture it
the same way.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

BUILD_INFO_PATH = Path(__file__).resolve().parent.parent / "jarvis" / "build_info.py"

_NUMBER_RE = re.compile(r"(?m)^BUILD_NUMBER\s*=\s*(\d+)\s*$")

TEMPLATE = '''"""Auto-generated build metadata — DO NOT EDIT BY HAND.

Regenerated on every `script.bat` build run by
build_tools/bump_build_version.py, which increments BUILD_NUMBER by 1 and
restamps BUILT_AT, unconditionally, every single build. See that file's
docstring for why this exists (short version: pyproject.toml's version
doesn't change per-build, so this is the only reliable signal that a
running jarvis.exe is actually today's build and not a stale one).

Safe to commit. Safe to delete -- bump_build_version.py recreates it from
scratch (starting at build 1) if it's missing.
"""

BUILD_NUMBER = {number}
BUILT_AT = "{built_at}"


def version_string():
    """'jarvis-cli <pkg-version> build <N> (<built-at>)' -- <pkg-version>
    comes from the installed package's own metadata (pyproject.toml's
    [project].version), not duplicated here, so it can never drift out of
    sync with the real thing. Falls back gracefully if the package isn't
    installed the normal way (e.g. running straight from source)."""
    try:
        from importlib.metadata import version as _pkg_version
        pkg_version = _pkg_version("jarvis-cli")
    except Exception:
        pkg_version = "unknown"

    built = f" ({{BUILT_AT}})" if BUILT_AT else ""
    return f"jarvis-cli {{pkg_version}} build {{BUILD_NUMBER}}{{built}}"
'''


def bump(path=BUILD_INFO_PATH):
    if path.exists():
        text = path.read_text(encoding="utf-8")
        match = _NUMBER_RE.search(text)
        current = int(match.group(1)) if match else 0
    else:
        current = 0

    new_number = current + 1
    built_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(TEMPLATE.format(number=new_number, built_at=built_at), encoding="utf-8")
    return new_number


def main():
    try:
        new_number = bump()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    # Deliberately only the number on stdout -- mirrors sync_entry_point.py
    # / find_cli_exe.py so script.bat can capture it the same way.
    sys.stdout.write(str(new_number))


if __name__ == "__main__":
    main()
