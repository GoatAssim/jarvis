"""CLI handlers for daemons, raw log search, the backlog, ambient
monitoring and onboarding.

Same shape and same reasoning as channels_cli.py: cli.py gains one dispatch
branch, every command's body lives next to the code it drives, and output
is JSON on stdout so web/server.js can shell out to these rather than
reimplementing any of it in Node.

The one exception is `onboard` and `daemon-console`, which print for a
human by default — a setup wizard rendered as JSON is not a wizard, and a
console tail is the one thing you always want raw. Both take --json for
the programmatic caller.
"""

import json
import sys

COMMANDS = (
    "daemons", "daemon-start", "daemon-stop", "daemon-restart",
    "daemon-status", "daemon-console", "daemon-input", "daemon-schedule",
    "daemon-add", "daemon-edit", "daemon-remove", "daemon-run",
    "daemons-tick",
    "logs-files", "logs-tail", "logs-sets",
    "backlog", "backlog-add", "backlog-done", "backlog-update",
    "backlog-remove", "backlog-board",
    "ambient", "ambient-tick",
    "onboard", "ui-mode",
)

USAGE = """workspace commands:
  daemons                              list every background service + status
  daemon-start  <id>                   start one
  daemon-stop   <id>                   stop one
  daemon-restart <id>                  stop then start
  daemon-status <id>                   detail for one
  daemon-console <id> [--lines N] [--file PATH]
                                       tail its captured output
  daemon-input  <id> <text>            type a line into its stdin
  daemon-schedule <id> <when>          start it later ("" clears)
  daemon-add    <id> <command> [--name N] [--cwd D] [--stdin] [--env K=V]
                     [--shell] [--restart never|on-failure|always]
                     [--restart-delay S] [--max-restarts N]
                     [--stop-signal TERM|INT|KILL] [--stop-timeout S]
                     [--autostart] [--description D]
  daemon-edit   <id> [--name N] [--cwd D] [--enabled true|false]
                     [--autostart true|false] [--command C] [--shell true|false]
                     [--restart P] [--restart-delay S] [--max-restarts N]
                     [--stop-signal S] [--stop-timeout S] [--env K=V]
  daemon-remove <id>
  daemon-run    <id>                   THE SUPERVISOR — runs in the foreground
  daemons-tick                         start whatever is scheduled and due

  logs-files <query> [--mode words|phrase|regex] [--set S] [--path P]
                     [--context N] [--limit N]
                                       grep the raw log files on disk
  logs-tail  <path> [--lines N]        last N lines of any log file
  logs-sets                            which log sets exist and their file counts

  backlog [--state S] [--project P] [--all]
  backlog-add <title> [--project P] [--state S] [--priority P] [--note N]
  backlog-done <item>                  item = id or part of the title
  backlog-update <item> [--state S] [--blocked-on X] [--priority P]
  backlog-remove <item>
  backlog-board                        everything grouped by state

  ambient                              what Jarvis has noticed
  ambient-tick                         run the checks now

  onboard [--json] [--skip]            guided setup / setup checklist
  ui-mode [classic|focus]              read or set the web UI layout"""


def _emit(payload, code=0):
    print(json.dumps(payload, indent=2, default=str))
    if code:
        sys.exit(code)


def _fail(message, code=1):
    _emit({"ok": False, "error": message}, code)


def _opts(rest):
    """Split argv into positionals and --flags. A flag with no value is a
    boolean; --key value consumes the next token."""
    positional, flags = [], {}
    i = 0
    while i < len(rest):
        token = rest[i]
        if token.startswith("--"):
            key = token[2:]
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                flags.setdefault(key, []).append(rest[i + 1])
                i += 2
            else:
                flags[key] = [True]
                i += 1
            continue
        positional.append(token)
        i += 1
    return positional, flags


def _one(flags, key, default=None):
    value = flags.get(key)
    return value[0] if value else default


def _bool(value, default=False):
    if value is None or value is True:
        return True if value is True else default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def handle(argv):
    cmd = argv[0]
    rest = argv[1:]
    positional, flags = _opts(rest)

    from . import daemons

    # --- daemons -----------------------------------------------------------
    if cmd == "daemons":
        entries = daemons.list_daemons()
        _emit({"ok": True, "count": len(entries), "daemons": entries})
        return

    if cmd in ("daemon-start", "daemon-stop", "daemon-restart"):
        if not positional:
            _fail(f"usage: {cmd} <id>")
        action = {"daemon-start": daemons.start, "daemon-stop": daemons.stop,
                  "daemon-restart": daemons.restart}[cmd]
        ok, message = action(positional[0])
        _emit({"ok": ok, "id": positional[0], "message": message}, 0 if ok else 1)
        return

    if cmd == "daemon-status":
        if not positional:
            _fail("usage: daemon-status <id>")
        entry = daemons.get(positional[0])
        if not entry:
            _fail(f"no daemon '{positional[0]}'")
        _emit({"ok": True, "daemon": daemons.describe(entry)})
        return

    if cmd == "daemon-console":
        if not positional:
            _fail("usage: daemon-console <id> [--lines N] [--file PATH]")
        did = positional[0]
        try:
            lines = int(_one(flags, "lines", 120))
        except (TypeError, ValueError):
            lines = 120
        output = daemons.read_console(did, lines=lines, path=_one(flags, "file"))
        if "json" in flags:
            _emit({"ok": True, "id": did, "lines": output,
                   "backups": daemons.console_backups(did)})
            return
        print("\n".join(output) if output else "(no console output yet)")
        return

    if cmd == "daemon-input":
        if len(positional) < 2:
            _fail("usage: daemon-input <id> <text>")
        ok, message = daemons.send_input(positional[0], " ".join(positional[1:]))
        _emit({"ok": ok, "id": positional[0], "message": message}, 0 if ok else 1)
        return

    if cmd == "daemon-schedule":
        if not positional:
            _fail("usage: daemon-schedule <id> <when>")
        ok, result = daemons.schedule(positional[0], " ".join(positional[1:]))
        _emit({"ok": ok, "id": positional[0],
               "next_start" if ok else "error": result}, 0 if ok else 1)
        return

    if cmd == "daemon-add":
        if len(positional) < 2:
            _fail('usage: daemon-add <id> "<command>" [--name N] [--cwd D] [--stdin]')
        env = {}
        for pair in flags.get("env", []):
            if pair is True or "=" not in str(pair):
                continue
            key, _, value = str(pair).partition("=")
            env[key.strip()] = value
        try:
            ok, err = daemons.add(
                positional[0],
                " ".join(positional[1:]),
                name=_one(flags, "name", ""),
                cwd=_one(flags, "cwd", ""),
                env=env,
                supports_stdin="stdin" in flags,
                description=_one(flags, "description", ""),
                autostart="autostart" in flags,
                shell="shell" in flags,
                restart=_one(flags, "restart", daemons.RESTART_NEVER),
                restart_delay=_one(flags, "restart-delay",
                                   daemons.DEFAULT_RESTART_DELAY),
                max_restarts=_one(flags, "max-restarts",
                                  daemons.DEFAULT_MAX_RESTARTS),
                stop_signal=_one(flags, "stop-signal", "TERM"),
                stop_timeout=_one(flags, "stop-timeout",
                                  daemons.DEFAULT_STOP_TIMEOUT),
                notes=_one(flags, "notes", ""),
            )
        except (TypeError, ValueError) as exc:
            _fail(f"bad value: {exc}")
        _emit({"ok": ok, "id": positional[0], "error": err}, 0 if ok else 1)
        return

    if cmd == "daemon-edit":
        if not positional:
            _fail("usage: daemon-edit <id> [--name N] [--enabled true|false] ...")
        fields = {}
        if "name" in flags:
            fields["name"] = _one(flags, "name")
        if "cwd" in flags:
            fields["cwd"] = _one(flags, "cwd")
        if "command" in flags:
            fields["argv"] = _one(flags, "command")
        if "enabled" in flags:
            fields["enabled"] = _bool(_one(flags, "enabled"), True)
        if "autostart" in flags:
            fields["autostart"] = _bool(_one(flags, "autostart"), True)
        if "stdin" in flags:
            fields["supports_stdin"] = _bool(_one(flags, "stdin"), True)
        if "notes" in flags:
            fields["notes"] = _one(flags, "notes")
        if "shell" in flags:
            fields["shell"] = _bool(_one(flags, "shell"), True)
        if "restart" in flags:
            fields["restart"] = _one(flags, "restart")
        for flag, field in (("restart-delay", "restart_delay"),
                            ("max-restarts", "max_restarts"),
                            ("stop-timeout", "stop_timeout")):
            if flag in flags:
                fields[field] = _one(flags, flag)
        if "stop-signal" in flags:
            fields["stop_signal"] = str(_one(flags, "stop-signal") or "").upper()
        if "env" in flags:
            # Merged onto whatever is already stored rather than replacing it,
            # so `--env PORT=81` doesn't silently drop every other variable.
            merged_env = dict((daemons.get(positional[0]) or {}).get("env") or {})
            for pair in flags.get("env", []):
                if pair is True or "=" not in str(pair):
                    continue
                key, _, value = str(pair).partition("=")
                merged_env[key.strip()] = value
            fields["env"] = merged_env
        if not fields:
            _fail("nothing to change")
        ok, err = daemons.edit(positional[0], **fields)
        _emit({"ok": ok, "id": positional[0], "error": err,
               "changed": sorted(fields)}, 0 if ok else 1)
        return

    if cmd == "daemon-remove":
        if not positional:
            _fail("usage: daemon-remove <id>")
        ok, err = daemons.remove(positional[0])
        _emit({"ok": ok, "id": positional[0], "error": err}, 0 if ok else 1)
        return

    if cmd == "daemon-run":
        # The supervisor. Blocks for the lifetime of the child — this is the
        # one command here that is not a quick query.
        if not positional:
            _fail("usage: daemon-run <id>")
        sys.exit(daemons.run_supervisor(positional[0]))

    if cmd == "daemons-tick":
        _emit({"ok": True, "started": daemons.tick()})
        return

    # --- raw log search ----------------------------------------------------
    if cmd == "logs-files":
        from . import log_files
        if not positional:
            _fail('usage: logs-files <query> [--mode words|phrase|regex] '
                  '[--set daemons] [--path /some/file.log] [--context 2]')
        sets = [s for s in flags.get("set", []) if s is not True] or None
        paths = [p for p in flags.get("path", []) if p is not True] or None
        result = log_files.search(
            " ".join(positional),
            mode=_one(flags, "mode", "words"),
            limit=int(_one(flags, "limit", log_files.DEFAULT_LIMIT) or 100),
            sets=sets,
            paths=paths,
            context=int(_one(flags, "context", 0) or 0),
        )
        _emit(result, 0 if result.get("ok") else 1)
        return

    if cmd == "logs-tail":
        from . import log_files
        if not positional:
            _fail("usage: logs-tail <path> [--lines N]")
        result = log_files.tail(positional[0],
                                lines=int(_one(flags, "lines", 200) or 200))
        if "json" in flags:
            _emit(result, 0 if result.get("ok") else 1)
            return
        if not result.get("ok"):
            _fail(result.get("error") or "could not read file")
        print("\n".join(result["lines"]))
        return

    if cmd == "logs-sets":
        from . import log_files
        _emit({"ok": True, "sets": log_files.available_sets()})
        return

    # --- backlog -----------------------------------------------------------
    from . import backlog

    if cmd == "backlog":
        found = backlog.items(
            state=_one(flags, "state"),
            project=_one(flags, "project"),
            tag=_one(flags, "tag"),
            include_done="all" in flags,
        )
        if "json" in flags:
            _emit({"ok": True, "count": len(found), "items": found})
            return
        print(backlog.render(found))
        return

    if cmd == "backlog-board":
        _emit({"ok": True, "board": backlog.board(),
               "summary": backlog.summary()})
        return

    if cmd == "backlog-add":
        if not positional:
            _fail('usage: backlog-add "<title>" [--project P] [--state S]')
        item, err = backlog.add(
            " ".join(positional),
            project=_one(flags, "project", ""),
            state=_one(flags, "state", backlog.TODO),
            priority=_one(flags, "priority", "normal"),
            note=_one(flags, "note", ""),
        )
        if item is None:
            _fail(err)
        _emit({"ok": True, "item": item})
        return

    if cmd in ("backlog-done", "backlog-update"):
        if not positional:
            _fail(f"usage: {cmd} <id-or-title>")
        query = positional[0] if cmd == "backlog-update" else " ".join(positional)
        fields = {}
        if cmd == "backlog-done":
            fields["state"] = backlog.DONE
        else:
            for flag, field in (("state", "state"), ("priority", "priority"),
                                ("blocked-on", "blocked_on"), ("note", "note"),
                                ("project", "project"), ("title", "title")):
                if flag in flags:
                    fields[field] = _one(flags, flag)
        if not fields:
            _fail("nothing to change")
        item, err = backlog.update(query, **fields)
        if item is None:
            _fail(err)
        _emit({"ok": True, "item": item})
        return

    if cmd == "backlog-remove":
        if not positional:
            _fail("usage: backlog-remove <id-or-title>")
        item, err = backlog.remove(" ".join(positional))
        if item is None:
            _fail(err)
        _emit({"ok": True, "removed": item})
        return

    # --- ambient -----------------------------------------------------------
    from . import ambient

    if cmd == "ambient":
        _emit(ambient.status())
        return

    if cmd == "ambient-tick":
        _emit({"ok": True, **ambient.tick(notify="quiet" not in flags)})
        return

    # --- onboarding --------------------------------------------------------
    from . import onboarding

    if cmd == "onboard":
        if "skip" in flags:
            onboarding.skip()
            _emit({"ok": True, "skipped": True})
            return
        if "complete" in flags:
            onboarding.mark_complete()
            _emit({"ok": True, "completed": True})
            return
        data = onboarding.summary()
        if "json" in flags:
            _emit(data)
            return
        print(onboarding.render_for_terminal(data))
        return

    if cmd == "ui-mode":
        if not positional:
            _emit({"ok": True, "ui_mode": onboarding.ui_mode(),
                   "available": list(onboarding.UI_MODES)})
            return
        ok, result = onboarding.set_ui_mode(positional[0])
        _emit({"ok": ok, "ui_mode" if ok else "error": result}, 0 if ok else 1)
        return

    _fail(f"unknown command '{cmd}'")
