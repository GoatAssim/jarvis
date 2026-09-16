"""Checks whether the jarvis exe that PATH actually resolves to -- i.e.
exactly what typing `jarvis` in a fresh terminal would run -- was built
from the source sitting in jarvis-cli/jarvis/ right now.

This is deliberately NOT the same exe find_cli_exe.py locates. That
script intentionally searches specific known install locations (a venv,
the base/per-user Python scripts dir) and picks the newest by mtime, so
script.bat knows what to copy from. This script instead asks the
operating system's own PATH search -- shutil.which(), the same
resolution order a plain `jarvis` invocation goes through -- for exactly
one thing: the exe a person actually gets when they run `jarvis` on this
machine, wherever that happens to be (which can legitimately differ from
what find_cli_exe.py just found, e.g. if something earlier on PATH
shadows the fresh build).

This is a real check, not another trust signal: it runs the actual
resolved exe, asks it for the source hash it was built with (embedded at
build time by bump_build_version.py, surfaced via `jarvis version`), and
compares that against a hash computed from source on disk right now
(hash_source.py -- the exact same function bump_build_version.py used to
decide whether to bump in the first place).

Run from jarvis-cli/, AFTER `pip install .` (so "source on disk right
now" and "what was just installed" are the same thing when this runs):

    python build_tools\\verify_exe.py <cli_name>

Prints one of three outcomes to stdout and sets the exit code
accordingly (0 only for a confirmed match):
  - "OK: ..."          exit 0 -- PATH's exe matches current source.
  - "MISMATCH: ..."    exit 1 -- PATH's exe was built from DIFFERENT
                        source than what's on disk right now.
  - "CANNOT VERIFY: ..." exit 1 -- no exe found on PATH, or it ran but
                        gave no hash to compare (e.g. a build from
                        before this feature existed). Deliberately never
                        reported as "OK" -- "can't tell" and "matches"
                        must never look the same to a caller checking
                        errorlevel.
Never raises -- every failure mode is a clear message plus a nonzero
exit code, since this is meant to be `if errorlevel 1` from a batch file.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hash_source import hash_source_tree, short_hash  # noqa: E402

_HASH_IN_VERSION_RE = re.compile(r"\[src ([0-9a-f]+)\]")


def resolve_path_exe(cli_name):
    """The literal first hit `where <cli_name>` (POSIX: `which`) would
    give you -- i.e. whatever a plain `jarvis` invocation in a fresh
    shell actually runs on THIS machine, right now, full stop. Returns
    None if PATH has no such command at all."""
    return shutil.which(cli_name)


def get_exe_hash(exe_path, timeout=10):
    """Runs `<exe_path> version` and pulls the "[src <hash>]" segment
    out of its stdout. Returns None -- meaning "couldn't verify", never
    treated as either a match or a mismatch -- if the exe can't be
    started, times out, exits nonzero, or its version output has no
    recognizable hash in it (e.g. it predates this feature)."""
    try:
        result = subprocess.run(
            [exe_path, "version"], capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    match = _HASH_IN_VERSION_RE.search(result.stdout or "")
    return match.group(1) if match else None


def main():
    if len(sys.argv) != 2 or not sys.argv[1]:
        print("ERROR: usage: verify_exe.py <cli_name>", file=sys.stderr)
        sys.exit(2)
    cli_name = sys.argv[1]

    exe_path = resolve_path_exe(cli_name)
    if not exe_path:
        print(f'CANNOT VERIFY: no "{cli_name}" found on PATH at all.')
        sys.exit(1)

    exe_hash = get_exe_hash(exe_path)
    if exe_hash is None:
        print(
            f'CANNOT VERIFY: "{exe_path}" ran but reported no source hash '
            "(a build from before this feature existed, or this isn't really jarvis)."
        )
        sys.exit(1)

    # `jarvis version` only ever prints the 12-char SHORT hash (see
    # build_info.py's version_string() -- SOURCE_HASH[:12] -- kept short
    # deliberately so a human running `jarvis version` isn't staring at a
    # 64-char hex string). Truncate our freshly computed source hash the
    # same way before comparing, so both sides of this comparison are the
    # same length -- comparing a 12-char capture against a 64-char full
    # hash would ALWAYS mismatch, which is a real bug, not a cautious
    # check. 12 hex chars (48 bits) of SHA-256 is effectively zero
    # collision risk for "does this dev build match this source tree";
    # this was never a security boundary.
    source_hash_short = short_hash(hash_source_tree())

    if exe_hash == source_hash_short:
        print(f'OK: PATH\'s "{cli_name}" ({exe_path}) matches current source [src {source_hash_short}].')
        sys.exit(0)

    print(
        f'MISMATCH: PATH\'s "{cli_name}" ({exe_path}) was built from source hash '
        f"[src {exe_hash}], but jarvis-cli/jarvis/ on disk right now "
        f"hashes to [src {source_hash_short}]. Whatever `{cli_name}` actually "
        "runs on this machine is NOT what you just built -- check for another "
        f"{cli_name}.exe earlier on PATH, or a stale install this build didn't overwrite."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
