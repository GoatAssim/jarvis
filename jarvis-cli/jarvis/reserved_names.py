"""The one canonical list of names a saved command can't use (I-B2 fix).

Before this module existed, `cli.py`, `web/server.js` and
`commands_config.py` each kept their own hand-copied "reserved names" set,
and all three disagreed: `commands_config.validate_command_name` (used when
*saving* a command) accepted names like `backlog`, `think` and `doctor` that
the CLI's own dispatcher (used when *running* one) would silently shadow
with a built-in instead of ever reaching the saved command. `cli.py`'s own
list was also missing 30 real public subcommands outright (`doctor`,
`version`, the `policy-*`/`memory-*`/`calendar-*`/`digest-*`/`ctools-*`
families) — a saved command with any of those names collided with **no
warning at all**.

This module is now the only place that knows the full set. `cli.py` and
`commands_config.py` both import `RESERVED_NAMES` (and `CHAIN_SEP` /
`PARALLEL_SEP`, moved here too so this module doesn't need to import
`cli.py` back and risk a cycle) from here instead of keeping their own copy.
`web/server.js` no longer keeps a copy at all — see `commands-check-name`
in `cli.py`, which is what it asks instead (REPO_MAP.md §6: "every web
route shells out", never reimplements a rule).

Kept as a plain, hand-maintained literal — same as the three lists it
replaces — rather than computed by importing every dispatch module at
runtime, because `RESERVED_NAMES` is consulted on the hot path of every
plain `jarvis <free text>` prompt (see `cli.py`'s final "is this actually
an AI prompt?" check), and two of the delegated modules
(`channels_cli`, which pulls in `discord.py`/Instagram gateway machinery)
are only imported lazily elsewhere specifically so a machine without those
extras installed can still run every other command. Importing them here
just to build a name set would undo that.

Correctness against drift is instead enforced by `tests/test_reserved_names.py`,
which — the way the mismatch that motivated this module was originally
found — regex-extracts every `argv[0] == "..."` / `argv[0] in (...)`
dispatch literal straight out of `cli.py`, adds `SCHEDULER_COMMANDS` and the
three delegated `COMMANDS` tuples, and fails if that computed set contains
anything this file doesn't. Adding a new subcommand without adding it here
is a test failure, not a silent gap.
"""

CHAIN_SEP = "then"      # starts a new batch — waits for the previous one to finish
PARALLEL_SEP = "and"    # joins the current batch — runs alongside whatever's already in it

RESERVED_NAMES = {
    # --- cli.py's own top-level dispatch ------------------------------------
    "config", "ai-config", "ai-clear", "ai-drop-from",
    "playnite-config", "spotify-config", "spotify-login",
    "memory-config", "everything-config",
    "tools-list", "tool-run", "tool-preview", "tool-safety-set",
    "personas-list",
    "skills-list", "skills-get", "skills-save", "skills-add", "skills-create",
    "skills-remove", "skillmake", "skilladd", "skillload", "skillunload",
    "conv-new", "conv-list", "conv-show", "conv-switch", "conv-delete", "conv-export",
    "logs", "logs-list", "logs-show", "logs-append-run", "logs-clear",
    "console-append-run", "console-read", "console-clear",
    "organize-json", "mode", "mode-set", "voice-config",
    "speak", "listen", "transcribe",
    "subagent-keys", "subagents", "subagent-spawn", "subagent-run",
    "subagent-status", "subagent-cancel",
    "think",
    "_internal_retitle",
    # doctor/version/browser-setup/browser-daemon: terminal-only, no web
    # route, but still real dispatch — a saved command with these names
    # was previously shadowed with no warning at all.
    "doctor", "version", "--version", "-v", "browser-setup", "browser-daemon",
    # policy-*/memory-*/calendar-*/digest-*/ctools-*: the 24 names that were
    # completely absent from every one of the three old lists (I-B2).
    "policy", "policy-check", "policy-dry-run",
    "memory-ns", "memory-list", "memory-consolidate", "memory-recall",
    "memory-reindex", "memory-stats",
    "calendar-add", "calendar-list", "calendar-remove", "calendar-events",
    "digest-status", "digest-on", "digest-off", "digest-now", "digest-preview",
    "ctools-list", "ctools-show", "ctools-write", "ctools-check",
    "ctools-delete", "ctools-run", "ctools-toggle", "ctools-templates",
    # commands-check-name: added by this same fix (I-B2 item 2) so
    # web/server.js can ask the CLI instead of keeping its own copy.
    "commands-check-name",
    # --- cli.py's SCHEDULER_COMMANDS ----------------------------------------
    "sched-list", "sched-tick", "sched-daemon", "sched-ask-log", "sched-add",
    "sched-show", "sched-cancel", "sched-pause", "sched-resume", "sched-snooze",
    "sched-approve", "sched-signal", "sched-clear",
    "notify-send", "notify-list", "notify-history", "notify-ack",
    "notify-clear", "notify-config",
    "conv-search", "mcp-status", "mcp-refresh", "mcp-config", "mcp-call", "mcp-tools",
    # --- workspace_cli.COMMANDS ----------------------------------------------
    "daemons", "daemon-start", "daemon-stop", "daemon-restart", "daemon-status",
    "daemon-console", "daemon-input", "daemon-schedule", "daemon-add",
    "daemon-edit", "daemon-remove", "daemon-run", "daemons-tick",
    "logs-files", "logs-tail", "logs-sets",
    "backlog", "backlog-add", "backlog-done", "backlog-update",
    "backlog-remove", "backlog-board",
    "ambient", "ambient-tick",
    "onboard", "ui-mode",
    # --- channels_cli.COMMANDS -----------------------------------------------
    "channels-config", "channels-status", "channels-set", "channels-allow",
    "channels-deny", "channels-test", "channels-whoami", "channels-log",
    "channels-directory", "channels-people", "channels-follow", "channels-block",
    "discord-daemon", "instagram-serve", "logs-search",
    # --- clipboard_cli.COMMANDS ----------------------------------------------
    "clipboard-watch", "clipboard-watch-config",
    # --- separators + argparse's own reserved flags -------------------------
    CHAIN_SEP, PARALLEL_SEP, "-h", "--help",
}
