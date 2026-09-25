"""Guards against reserved_names.py drifting out of sync with what cli.py
actually dispatches (I-B2 fix).

Before reserved_names.py existed, cli.py, web/server.js and
commands_config.py each hand-kept their own "reserved names" list, and all
three disagreed — commands_config.validate_command_name (used when SAVING a
command) accepted names like "backlog", "think" and "doctor" that cli.py's
own dispatcher (used when RUNNING one) would silently shadow with a
built-in instead of ever reaching the saved command, and cli.py's own list
was missing 30 real public subcommands outright.

This test is the mechanism that originally found that: it regex-extracts
every `argv[0] == "..."` / `argv[0] in (...)` dispatch literal straight out
of cli.py's source, adds SCHEDULER_COMMANDS and the three delegated
COMMANDS tuples (workspace_cli, channels_cli, clipboard_cli), and fails if
that computed set contains anything reserved_names.RESERVED_NAMES doesn't.
Adding a new subcommand without adding it to reserved_names.py is now a
test failure, not a silent gap.

This only checks for MISSING coverage (dispatch names not yet reserved).
It intentionally does not fail the other direction (reserved_names.py
containing more than cli.py currently dispatches) since the canonical set
also carries names other pieces (web/server.js's own `commands-check-name`,
the delegated modules) legitimately reserve without necessarily using the
exact `argv[0] == "..."` literal shape this regex looks for.

Run with `python3 tests/test_reserved_names.py`. No API key or network needed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JARVIS_CLI = REPO_ROOT / "jarvis-cli"
sys.path.insert(0, str(JARVIS_CLI))

from jarvis import cli, channels_cli, workspace_cli, clipboard_cli  # noqa: E402
from jarvis.reserved_names import RESERVED_NAMES  # noqa: E402

pass_count = 0
fail_count = 0


def check(name, cond, detail=""):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print("ok  " + name)
    else:
        fail_count += 1
        print("FAIL " + name + (f": {detail}" if detail else ""))


def extract_dispatch_literals(src):
    """Same technique the I-B2 audit used: regex-scan cli.py's source for
    every `argv[0] == "literal"` and `argv[0] in ("a", "b", ...)` check."""
    names = set()
    for m in re.finditer(r'argv\[0\]\s*==\s*"([^"]+)"', src):
        names.add(m.group(1))
    for m in re.finditer(r'argv\[0\]\s+in\s+\(([^)]*)\)', src):
        for lit in re.finditer(r'"([^"]+)"', m.group(1)):
            names.add(lit.group(1))
    return names


def test_no_undispatched_drift():
    src = Path(cli.__file__).read_text(encoding="utf-8")
    discovered = extract_dispatch_literals(src)
    discovered |= set(cli.SCHEDULER_COMMANDS)
    discovered |= set(channels_cli.COMMANDS)
    discovered |= set(workspace_cli.COMMANDS)
    discovered |= set(clipboard_cli.COMMANDS)

    missing = sorted(discovered - RESERVED_NAMES)
    check(
        "every name cli.py actually dispatches is in RESERVED_NAMES",
        not missing,
        f"missing from reserved_names.RESERVED_NAMES: {missing}. "
        "Add each one to jarvis-cli/jarvis/reserved_names.py.",
    )


def test_known_previously_missing_names_are_now_covered():
    # The exact 30-name list the original I-B2 audit found absent from
    # every one of the three old lists. Pinned individually (not just via
    # the drift scan above) so a future refactor of reserved_names.py can't
    # silently drop one of these and still pass the set-difference check
    # above if it happens to be paired with an unrelated addition.
    previously_missing = {
        "doctor", "version", "conv-export", "browser-setup",
        "policy", "policy-check", "policy-dry-run",
        "memory-ns", "memory-list", "memory-consolidate", "memory-recall",
        "memory-reindex", "memory-stats",
        "calendar-add", "calendar-list", "calendar-remove", "calendar-events",
        "digest-status", "digest-on", "digest-off", "digest-now", "digest-preview",
        "ctools-list", "ctools-show", "ctools-write", "ctools-check",
        "ctools-delete", "ctools-run", "ctools-toggle", "ctools-templates",
    }
    still_missing = sorted(previously_missing - RESERVED_NAMES)
    check(
        "all 30 previously-uncovered subcommand names are now reserved",
        not still_missing,
        f"still missing: {still_missing}",
    )


def test_reserved_names_agrees_with_saving_a_command():
    from jarvis import commands_config

    for name in ("doctor", "think", "backlog", "daemons", "skillload",
                 "organize-json", "logs-search", "mode"):
        error = commands_config.validate_command_name(name)
        check(
            f'validate_command_name rejects "{name}" (was previously silently accepted)',
            error is not None,
            f"expected a reserved-word error, got: {error!r}",
        )

    error = commands_config.validate_command_name("a-totally-ordinary-name")
    check(
        'validate_command_name accepts an ordinary, non-reserved name',
        error is None,
        f"expected no error, got: {error!r}",
    )


def test_cli_and_commands_config_share_one_object():
    # Not just equal sets — the literal same object, so there is only ever
    # one place to update.
    from jarvis import commands_config

    check(
        "cli.RESERVED_NAMES and commands_config.RESERVED_NAMES are the same object",
        cli.RESERVED_NAMES is commands_config.RESERVED_NAMES is RESERVED_NAMES,
    )


if __name__ == "__main__":
    test_no_undispatched_drift()
    test_known_previously_missing_names_are_now_covered()
    test_reserved_names_agrees_with_saving_a_command()
    test_cli_and_commands_config_share_one_object()
    print(f"\n{pass_count} passed, {fail_count} failed")
    if fail_count:
        sys.exit(1)
