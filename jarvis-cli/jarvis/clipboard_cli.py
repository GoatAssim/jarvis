"""CLI handlers for the clipboard watcher.

Same shape and reasoning as channels_cli.py/workspace_cli.py: cli.py gains
one dispatch branch, the command bodies live next to the code they drive.

Two commands, deliberately not one:
  - `clipboard-watch` is the foreground worker (`clipboard_watch.run()`) —
    what the daemon supervisor actually execs. Not meant to be run by a
    human directly any more than `daemon-run` is.
  - `clipboard-watch-config` is the human-only way to set the optional
    match pattern. See clipboard_watch.py's module docstring for why this
    is a CLI command and not an AI tool: it's plain data (a regex used only
    for re.search), not an argv, so it doesn't trip the daemon_add
    invariant — but it was left as a CLI-only knob for now rather than
    assumed safe to hand to the model without the owner saying so.
"""

import json
import sys

COMMANDS = ("clipboard-watch", "clipboard-watch-config")

USAGE = """clipboard watch commands:
  clipboard-watch                        THE WORKER — runs in the foreground
                                          (start it via: jarvis daemon-start clipboard-watch)
  clipboard-watch-config [--pattern REGEX|--clear-pattern] [--poll-seconds N]
                                          show or set the watcher's config"""


def _emit(payload, code=0):
    print(json.dumps(payload, indent=2, default=str))
    if code:
        sys.exit(code)


def _fail(message, code=1):
    _emit({"ok": False, "error": message}, code)


def handle(argv):
    cmd = argv[0]
    rest = argv[1:]

    if cmd == "clipboard-watch":
        from . import clipboard_watch
        sys.exit(clipboard_watch.run())

    if cmd == "clipboard-watch-config":
        from . import clipboard_watch
        i = 0
        pattern_update = "__unset__"
        poll_seconds_update = None
        while i < len(rest):
            token = rest[i]
            if token == "--pattern" and i + 1 < len(rest):
                pattern_update = rest[i + 1]
                i += 2
                continue
            if token == "--clear-pattern":
                pattern_update = None
                i += 1
                continue
            if token == "--poll-seconds" and i + 1 < len(rest):
                try:
                    poll_seconds_update = float(rest[i + 1])
                except ValueError:
                    _fail(f"--poll-seconds must be a number, got {rest[i + 1]!r}")
                i += 2
                continue
            _fail(f"unrecognized argument: {token}\n\n{USAGE}")

        if pattern_update != "__unset__":
            try:
                clipboard_watch.set_pattern(pattern_update)
            except Exception as exc:  # noqa: BLE001 — surface a bad regex clearly
                _fail(f"invalid --pattern: {exc}")
        if poll_seconds_update is not None:
            clipboard_watch.save_config(poll_seconds=poll_seconds_update)

        _emit({"ok": True, "config": clipboard_watch.load_config()})
        return

    _fail(f"unknown clipboard command: {cmd}")
