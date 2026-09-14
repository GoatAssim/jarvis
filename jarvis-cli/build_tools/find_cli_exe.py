"""Locates the console-script .exe that `pip install .` just built for the
current persona's CLI name, across the two places a Windows/non-venv `pip
install` can put it.

Run by script.bat, from jarvis-cli/, AFTER `pip install .`:

    python build_tools\\find_cli_exe.py <cli_name>

Prints the resolved absolute path to stdout on success (nothing else) so
the caller can capture it the same way it captures sync_entry_point.py's
and persona_name.py's output. Prints nothing and exits non-zero if no
matching exe is found in either location.

WHY THIS EXISTS (don't inline this logic back into script.bat):
`sysconfig.get_path('scripts')` only reports the BASE interpreter's
Scripts folder. If `pip install .` runs without admin rights against a
non-venv Python (very common), pip silently installs into the PER-USER
scripts folder instead. On Windows that's NOT simply "<userbase>\\Scripts"
-- pip uses sysconfig's "nt_user" install scheme, which adds a
version-specific subfolder, e.g.:

    %APPDATA%\\Python\\Python314\\Scripts

(confirmed by pip's own "The script X.exe is installed in ..." notice).
Guessing "<userbase>\\Scripts" without that subfolder silently misses it.

This used to be a one-line `python -c "..."` call embedded inside a batch
`for /f` backtick command substitution. That's fragile: once the snippet
needed its own parentheses (for the base-vs-user scheme ternary), cmd.exe's
paren-matching for the *outer* `for /f ... in (...)` block got confused by
the *inner* parens inside the quoted command, corrupting the parse of that
line (and, observed in practice, apparently everything after it on the
same logical block). Doing this in a real .py file sidesteps that whole
class of batch-quoting bug entirely -- same reason sync_entry_point.py
and persona_name.py already work this way instead of as inline one-liners.

If BOTH locations have a matching exe (stale leftovers happen), the one
with the newer mtime wins, so a rebuild always picks the exe pip just
built rather than an old one sitting in the other location.
"""

import os
import sys
import sysconfig


def candidate_paths(cli_name):
    """Every location `pip install .` might realistically have put
    ``<cli_name>.exe`` in, for the CURRENT interpreter (must be run with
    the exact same python that ran `pip install .`, or these won't match)."""
    paths = []

    try:
        paths.append(os.path.join(sysconfig.get_path("scripts"), cli_name + ".exe"))
    except Exception:
        pass

    user_scheme = "nt_user" if os.name == "nt" else "posix_user"
    try:
        paths.append(
            os.path.join(sysconfig.get_path("scripts", scheme=user_scheme), cli_name + ".exe")
        )
    except Exception:
        pass

    return paths


def find_cli_exe(cli_name):
    found = [
        (os.path.getmtime(path), path)
        for path in candidate_paths(cli_name)
        if os.path.isfile(path)
    ]
    if not found:
        return None
    found.sort()
    return found[-1][1]  # newest mtime wins


def main():
    if len(sys.argv) != 2 or not sys.argv[1]:
        print("ERROR: usage: find_cli_exe.py <cli_name>", file=sys.stderr)
        sys.exit(1)

    exe_path = find_cli_exe(sys.argv[1])
    if exe_path is None:
        print(f"ERROR: no {sys.argv[1]}.exe found in any known scripts dir.", file=sys.stderr)
        sys.exit(1)

    # Deliberately only the path on stdout — mirrors sync_entry_point.py
    # and persona_name.py so the batch script can capture it the same way.
    sys.stdout.write(exe_path)


if __name__ == "__main__":
    main()
