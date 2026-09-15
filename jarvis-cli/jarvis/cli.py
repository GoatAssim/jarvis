"""jarvis: a tiny, JSON-configurable command runner."""

import argparse
import json
import os
import re
import subprocess
import signal
import sys
import threading
from pathlib import Path

from . import conditions, stats
from .palette import Palette
from .persona_name import current_cli_name, current_display_name, banner_letters

# Make stdout/stderr tolerant of any Unicode character, on every platform.
# AI responses can contain characters a legacy console codepage has no
# mapping for (e.g. U+202F narrow no-break space on Windows' cp1252),
# which would otherwise crash the whole program on a plain print(). This
# must run before colorama.init() below, so colorama wraps the
# already-reconfigured streams rather than the original ones.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # best-effort; worst case we're back to the old behavior

# On classic Windows consoles, ANSI color codes need to be turned into the
# right Win32 calls (or the console mode needs VT processing switched on).
# colorama does that; everywhere else this is a harmless no-op. It's only
# installed on Windows (see pyproject.toml), so the import is optional.
try:
    import colorama
    colorama.init()
except ImportError:
    pass

CONFIG_DIR = Path.home() / ".jarvis"
CONFIG_FILE = CONFIG_DIR / "commands.json"
ENCODING = "utf-8"

CHAIN_SEP = "then"      # starts a new batch \u2014 waits for the previous one to finish
PARALLEL_SEP = "and"    # joins the current batch \u2014 runs alongside whatever's already in it
RESERVED_NAMES = {"config", "ai-config", "ai-clear", "ai-drop-from", "playnite-config", "spotify-config", "spotify-login", "memory-config", "everything-config", "tools-list", "tool-run", "skills-list", "skills-get", "skills-save", "skills-add", "skills-create", "skills-remove", "skillmake", "skilladd", "skillload", "skillunload", "tool-preview", "tool-safety-set", "conv-new", "conv-list", "conv-show", "conv-switch", "conv-delete", "logs", "logs-list", "logs-show", "logs-clear", "organize-json", "mode", "mode-set", "voice-config", "speak", "listen", "transcribe", "sched-list", "sched-tick", "sched-add", "sched-show", "sched-cancel", "sched-pause", "sched-resume", "sched-snooze", "sched-approve", "sched-signal", "sched-clear", "notify-send", "notify-list", "notify-ack", "notify-clear", "notify-config", "conv-search", "mcp-status", "mcp-refresh", "mcp-config", "mcp-call", "mcp-tools", CHAIN_SEP, PARALLEL_SEP, "-h", "--help"}

OUT = Palette(sys.stdout)  # actual command output: the banner, the command list
ERR = Palette(sys.stderr)  # jarvis's own status/trace/error messages


def banner(p):
    # Letter-spaced from whichever persona is currently equipped (see
    # persona_name.banner_letters) instead of a permanently hardcoded
    # "J A R V I S" — so the startup banner actually matches the name
    # you renamed the assistant/exe to.
    return f"""{p.CYAN}{p.BOLD}
  -------------------------------
   {banner_letters()}
   your commands, your rules
  -------------------------------
{p.RESET}"""


DEFAULT_CONFIG = {
    "commands": {
        "hello": {
            "description": "Say hello to someone",
            "run": "echo Hello, {name}! Jarvis at your service.",
            "vars": {
                "name": {"default": "World", "description": "Who to greet"}
            },
        },
        "updateSpotify": {
            "description": "Example command \u2014 edit 'run' for your OS's real updater",
            "run": "echo Replace me with e.g. winget upgrade Spotify.Spotify",
            "vars": {},
        },
        "deployExample": {
            "description": "Example: multiple steps + conditions (edit or delete me)",
            "run": [
                "echo Step 1: this always runs",
                {
                    "name": "Prod deploy",
                    "if": {"env": "prod", "branch": "main"},
                    "run": "echo Step 2: deploying MAIN to PROD",
                },
                {
                    "name": "Prod hotfix",
                    "if": {"env": "prod", "branch": "hotfix"},
                    "run": "echo Step 2: deploying HOTFIX to PROD",
                },
                {
                    "name": "Staging deploy",
                    "if": {"env": "staging"},
                    "run": "echo Step 2: deploying {branch} to STAGING",
                },
            ],
            "vars": {
                "env": {"description": "Target environment (e.g. prod, staging)"},
                "branch": {"default": "main", "description": "Git branch to deploy"},
            },
        },
    }
}


def ensure_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding=ENCODING
        )
        print(f"{ERR.YELLOW}Created a starter config at {CONFIG_FILE}{ERR.RESET}", file=sys.stderr)


def load_commands():
    ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding=ENCODING))
    except json.JSONDecodeError as e:
        print(f"{ERR.RED}Invalid JSON in {CONFIG_FILE}: {e}{ERR.RESET}", file=sys.stderr)
        sys.exit(1)
    except UnicodeDecodeError as e:
        print(
            f"{ERR.RED}Couldn't read {CONFIG_FILE} as UTF-8: {e}\n"
            f"If you edited it in Notepad, re-save it with UTF-8 encoding.{ERR.RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

    commands = data.get("commands", {})
    if not isinstance(commands, dict):
        print(f"{ERR.RED}'commands' in {CONFIG_FILE} must be an object.{ERR.RESET}", file=sys.stderr)
        sys.exit(1)
    malformed = [n for n, s in commands.items() if not isinstance(s, dict)]
    if malformed:
        print(
            f"{ERR.YELLOW}Warning: ignoring malformed command(s) (must be an object): "
            f"{', '.join(malformed)}{ERR.RESET}",
            file=sys.stderr,
        )
        commands = {n: s for n, s in commands.items() if n not in malformed}

    collisions = sorted(set(commands) & RESERVED_NAMES)
    if collisions:
        print(
            f"{ERR.YELLOW}Warning: command name(s) {', '.join(collisions)} clash with "
            f"a reserved word ({', '.join(sorted(RESERVED_NAMES))}) and won't be reachable "
            f"as expected. Rename them in {CONFIG_FILE}.{ERR.RESET}",
            file=sys.stderr,
        )
    return commands


def print_help(commands, file=sys.stdout):
    p = OUT if file is sys.stdout else ERR
    print(banner(p), file=file)
    if not commands:
        print(f"No commands configured yet. Add some in {CONFIG_FILE}", file=file)
        return
    print(f"{p.BOLD}Available commands:{p.RESET}\n", file=file)
    width = max(len(name) for name in commands) + 2
    for name, spec in commands.items():
        print(f"  {p.GREEN}{name.ljust(width)}{p.RESET} {spec.get('description', '')}", file=file)
    print(f"\nRun '{p.CYAN}jarvis <command> --help{p.RESET}' for a command's options.", file=file)
    print(f"Chain several with '{p.CYAN}jarvis cmd1 then cmd2{p.RESET}'.", file=file)
    print(f"Built-in: {p.CYAN}config{p.RESET}, {p.CYAN}ai-config{p.RESET}, {p.CYAN}ai-clear{p.RESET}, {p.CYAN}tools-list{p.RESET} (prints every AI tool as JSON — not an ask), {p.CYAN}tool-run{p.RESET} (runs one AI tool directly), {p.CYAN}conv-new{p.RESET}/{p.CYAN}conv-list{p.RESET}/{p.CYAN}conv-show{p.RESET}/{p.CYAN}conv-switch{p.RESET}/{p.CYAN}conv-delete{p.RESET} (manage conversations), {p.CYAN}logs{p.RESET} (browse the raw model\u2194backend traffic for a conversation), {p.CYAN}mode{p.RESET}/{p.CYAN}mode-set <full|compact|precise|ultra>{p.RESET} (read/set the prompt's token-usage capacity — 400%/100%/150%/50%), {p.CYAN}voice-config{p.RESET} (prints the voice config file path), {p.CYAN}speak <text>{p.RESET} (text-to-speech), {p.CYAN}listen{p.RESET} (record → transcribe → ask → speak, one voice turn), {p.CYAN}transcribe <audio file>{p.RESET} (speech-to-text on an existing file), {p.CYAN}sched-list{p.RESET}/{p.CYAN}sched-add <when> <text>{p.RESET}/{p.CYAN}sched-cancel{p.RESET}/{p.CYAN}sched-snooze{p.RESET}/{p.CYAN}sched-approve{p.RESET} (scheduled tasks, reminders and notifications), {p.CYAN}sched-tick{p.RESET} (fire everything due now \\u2014 point Task Scheduler or cron at this), {p.CYAN}sched-signal <event>{p.RESET} (announce something finished, firing jobs waiting on it), {p.CYAN}notify-send <message>{p.RESET}, {p.CYAN}notify-config{p.RESET}.", file=file)
    print(f"Edit {p.DIM}{CONFIG_FILE}{p.RESET} to add or change commands.", file=file)


def describe_steps(spec):
    """Render a --help epilog listing each step and its condition, if 'run' is a list."""
    raw = spec.get("run")
    if not isinstance(raw, list):
        return None
    lines = ["Steps:"]
    for i, item in enumerate(raw, start=1):
        if isinstance(item, str):
            lines.append(f"  {i}. {item}")
            continue
        if not isinstance(item, dict):
            continue
        label = f"{item.get('name')}: " if item.get("name") else ""
        cond_bits = []
        if item.get("if") is not None:
            cond_bits.append(f"if {conditions.describe(item['if'])}")
        if item.get("unless") is not None:
            cond_bits.append(f"unless {conditions.describe(item['unless'])}")
        if item.get("parallel"):
            cond_bits.append("parallel with previous")
        if item.get("showCommand") is False:
            cond_bits.append("command hidden")
        cond_str = f"  [{'; '.join(cond_bits)}]" if cond_bits else ""
        lines.append(f"  {i}. {label}{item.get('run', '?')}{cond_str}")
    return "\n".join(lines)


def build_parser(commands):
    # `prog` matches whatever this build's entry point was actually named
    # (see persona_name.py / build_tools/sync_entry_point.py) so --help and
    # usage errors say e.g. "friday: error: ..." instead of always "jarvis:
    # error: ..." once the exe itself has been renamed.
    parser = argparse.ArgumentParser(prog=current_cli_name(), add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    for name, spec in commands.items():
        epilog = describe_steps(spec)
        sub = subparsers.add_parser(
            name,
            description=spec.get("description", ""),
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter if epilog else argparse.HelpFormatter,
        )
        for var_name, var_spec in spec.get("vars", {}).items():
            has_default = "default" in var_spec
            sub.add_argument(
                f"--{var_name}",
                dest=var_name.replace("-", "_"),
                default=var_spec.get("default"),
                required=not has_default,
                help=var_spec.get("description", ""),
            )
    return parser


def normalize_steps(name, spec):
    """Turn spec['run'] (a string, or a list of strings/objects) into a flat
    list of step dicts: {run, if, unless, continue_on_error, step_name,
    parallel, show_command}.

    parallel: if true, this step starts alongside whichever step(s) came
    right before it instead of waiting for them to finish (meaningless \u2014
    and ignored \u2014 on the very first step, since nothing precedes it).
    show_command: if false, the "\u25b6 name" / "$ cmd" trace line that
    normally prints right before a step runs is suppressed for that step.
    Defaults to true, so existing commands.json files that don't set it
    behave exactly as before. Either way this only affects that trace
    line \u2014 the step's own real output (anything it prints to stdout/
    stderr itself) always shows, same as always; there's no way to hide
    that and still know what actually happened."""
    raw = spec.get("run")
    if raw is None:
        print(f"{ERR.RED}'{name}' has no 'run' defined.{ERR.RESET}", file=sys.stderr)
        sys.exit(1)

    raw_list = raw if isinstance(raw, list) else [raw]
    steps = []
    for i, item in enumerate(raw_list, start=1):
        if isinstance(item, str):
            steps.append({"run": item, "if": None, "unless": None,
                          "continue_on_error": False, "step_name": None,
                          "parallel": False, "show_command": True})
        elif isinstance(item, dict):
            if "run" not in item:
                print(f"{ERR.RED}'{name}' step {i} is missing 'run'.{ERR.RESET}", file=sys.stderr)
                sys.exit(1)
            steps.append({
                "run": item["run"],
                "if": item.get("if"),
                "unless": item.get("unless"),
                "continue_on_error": bool(item.get("continueOnError", False)),
                "step_name": item.get("name"),
                "parallel": bool(item.get("parallel", False)),
                "show_command": bool(item.get("showCommand", True)),
            })
        else:
            print(
                f"{ERR.RED}'{name}' step {i} must be a string or object, "
                f"got {type(item).__name__}.{ERR.RESET}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not steps:
        print(f"{ERR.RED}'{name}' has an empty 'run' list \u2014 nothing to do.{ERR.RESET}", file=sys.stderr)
        sys.exit(1)
    return steps


def _group_into_batches(steps):
    """Group steps into batches based on each step's 'parallel' flag: a
    step marked parallel joins the same batch as the step(s) immediately
    before it (so they all start together and are waited on as a group);
    anything else starts a new batch that only begins once the previous
    batch has fully finished. The first step always starts its own batch
    \u2014 there's nothing before it to run alongside, so its own 'parallel'
    flag (if set) is meaningless and ignored, same as the CLI README says."""
    batches = []
    for step in steps:
        if step["parallel"] and batches:
            batches[-1].append(step)
        else:
            batches.append([step])
    return batches


# A saved command's "run" text can be an arbitrary shell/PowerShell/script
# snippet, and those are full of braces that have nothing to do with a
# jarvis {var} — a PowerShell script block (Where-Object { $_.Foo -like
# 'x' }), a regex quantifier ({2,4}), a printf/.NET format placeholder
# ({0}), a JSON literal, etc. str.format(**values) parses *every* '{...}'
# as a field, so any of those raised a spurious "uses variable ... that
# isn't defined" error (or, for something like {0}, an uncaught IndexError)
# even though nothing in the command was ever meant to be a jarvis
# variable. Only a bare {name} — letters/digits/_/- and nothing else
# between the braces — is a plausible jarvis variable reference; anything
# else inside braces is left exactly as written.
_RUN_TEMPLATE_TOKEN = re.compile(r"\{\{|\}\}|\{[A-Za-z_][A-Za-z0-9_-]*\}")


def _format_run_template(template, values):
    """Substitute {var} placeholders from a command's declared 'vars' —
    without treating every stray brace in the underlying command as one.

    {{ and }} still collapse to a literal brace (matches str.format's
    escaping, for anyone already relying on it). A bare {name} that isn't
    one of `values` still raises KeyError(name), same as before, so a
    genuine typo'd/undefined jarvis variable is still caught.
    """
    def _sub(match):
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = token[1:-1]
        if name in values:
            return str(values[name])
        raise KeyError(name)

    return _RUN_TEMPLATE_TOKEN.sub(_sub, template)


def _run_batch(name, batch, values, known_vars):
    """Run one batch of steps \u2014 concurrently if there's more than one.

    Two phases on purpose: first resolve every step's condition and
    {var} substitution (no side effects yet), THEN start every process
    that should run. That way a bad condition or an undefined variable
    anywhere in the batch is caught before anything in it is launched,
    instead of possibly leaving some of a "simultaneous" batch running
    with no way to have skipped them in hindsight.

    Returns (ran_any, last_code, should_stop).
    """
    to_run = []  # [(step, resolved_cmd), ...]
    for step in batch:
        try:
            ok = conditions.step_matches(step, values, known_vars)
        except conditions.ConditionError as e:
            print(f"{ERR.RED}'{name}': {e}{ERR.RESET}", file=sys.stderr)
            return False, 1, True
        except Exception as e:  # never let a bad condition crash the whole CLI
            print(f"{ERR.RED}'{name}': unexpected error evaluating condition: {e}{ERR.RESET}", file=sys.stderr)
            return False, 1, True

        if not ok:
            reason = conditions.describe(step.get("if")) or conditions.describe(step.get("unless"))
            print(f"{ERR.DIM}(skipping{' ' + step['step_name'] if step['step_name'] else ''} \u2014 condition not met: {reason}){ERR.RESET}", file=sys.stderr)
            continue

        try:
            cmd = _format_run_template(step["run"], values)
        except KeyError as e:
            print(f"{ERR.RED}'{name}' uses variable {e} that isn't defined in its 'vars'.{ERR.RESET}", file=sys.stderr)
            return False, 1, True

        to_run.append((step, cmd))

    if not to_run:
        return False, 0, False

    # Print each running step's trace line (unless toggled off) and start
    # its process, before waiting on any of them \u2014 so steps sharing a
    # batch genuinely run together rather than one at a time.
    procs = []
    for step, cmd in to_run:
        if step["show_command"]:
            if step["step_name"]:
                print(f"{ERR.BOLD}\u25b6 {step['step_name']}{ERR.RESET}", file=sys.stderr)
            print(f"{ERR.DIM}$ {cmd}{ERR.RESET}", file=sys.stderr)
        procs.append((step, subprocess.Popen(cmd, shell=True)))

    results = [(step, proc.wait()) for step, proc in procs]

    hard_failures = [(step, code) for step, code in results if code != 0 and not step["continue_on_error"]]
    for step, code in results:
        if code != 0 and step["continue_on_error"]:
            label = f" {step['step_name']}" if step["step_name"] else ""
            print(f"{ERR.YELLOW}(step{label} failed with exit code {code}, continuing \u2014 continueOnError){ERR.RESET}", file=sys.stderr)

    if hard_failures:
        if len(hard_failures) > 1:
            # Only ever possible for a genuinely parallel (>1 step) batch;
            # called out explicitly since interleaved output can otherwise
            # make it unclear which step(s) actually failed.
            detail = ", ".join(f"{s['step_name'] or 'step'} (exit {c})" for s, c in hard_failures)
            print(f"{ERR.RED}(parallel batch: {len(hard_failures)} step(s) failed \u2014 {detail}){ERR.RESET}", file=sys.stderr)
        return True, hard_failures[0][1], True

    return True, results[-1][1], False


def run_command(name, spec, args):
    """Run every step of a command, in batches (steps marked "parallel" run
    together with whichever step(s) came right before them; everything
    else runs one batch at a time, in order). Returns the process exit
    code (does not call sys.exit, so callers can chain multiple commands)."""
    stats.bump(name)
    known_vars = list(spec.get("vars", {}).keys())
    values = {v: getattr(args, v.replace("-", "_")) for v in known_vars}
    steps = normalize_steps(name, spec)
    any_conditional = any(s["if"] is not None or s["unless"] is not None for s in steps)
    batches = _group_into_batches(steps)

    ran_any = False
    last_code = 0

    for batch in batches:
        batch_ran, batch_code, should_stop = _run_batch(name, batch, values, known_vars)
        if batch_ran:
            ran_any = True
            last_code = batch_code
        if should_stop:
            return batch_code

    if not ran_any and any_conditional:
        print(f"{ERR.YELLOW}No step's condition matched for '{name}' \u2014 nothing ran.{ERR.RESET}", file=sys.stderr)
        return 1

    return last_code


def split_chain_batches(argv):
    """Split argv into a list of batches, each a list of per-command argv
    segments: 'then' starts a new batch (waits for the previous one to
    finish first); 'and' joins the current batch (runs alongside whatever
    else is already in it). Same then/parallel relationship as a single
    command's own "parallel" step field \u2014 just one level up, between
    whole commands instead of between steps of one command."""
    batches = [[[]]]
    for tok in argv:
        if tok == CHAIN_SEP:
            batches.append([[]])
        elif tok == PARALLEL_SEP:
            batches[-1].append([])
        else:
            batches[-1][-1].append(tok)
    cleaned = []
    for batch in batches:
        segs = [seg for seg in batch if seg]
        if segs:
            cleaned.append(segs)
    return cleaned


def confirm_tool_call(name, arguments, risk_note=None):
    """Shared confirm-required gate \u2014 same "are you sure" + AI-review
    protocol whether it's the AI deciding to call a tool (see
    ai_client._make_tool_executor's on_confirm_request) or a human typing a
    flagged saved command straight at the CLI (see confirm_direct_command
    below). Two protocols, auto-selected:
      - real terminal (stdin is a tty): a plain "Proceed? [y/N]" prompt,
        blocking on input() exactly like any other CLI confirmation.
      - piped stdin (this process spawned by the web server): prints one
        machine-readable "JARVIS_CONFIRM_REQUEST {...}" line to stdout and
        blocks reading one line back from stdin. server.js watches for
        that line on both the "ask" and "run" websocket flows, relays it to
        the browser as Yes/No buttons, and writes "y\\n"/"n\\n" back to this
        process's stdin once the user answers.

    risk_note, if given, is either the {"provider", "note"} dict from
    ai_client.risk_review() or that same dict with an extra
    "command_flags": {"confirm_required": bool, "ai_review": bool} key \u2014
    attached by ai_client when the call being confirmed is create_command
    or update_command, so the user sees exactly what safety flags the
    command they're about to create/change will carry, not just a generic
    danger rating.
    """
    payload = {"tool": name, "arguments": arguments or {}}
    if risk_note:
        payload["risk_note"] = risk_note

    command_flags = risk_note.get("command_flags") if isinstance(risk_note, dict) else None
    command_run = risk_note.get("command_run") if isinstance(risk_note, dict) else None

    if sys.stdin.isatty():
        print(
            f"\n{ERR.YELLOW}\u26a0 {current_display_name()} wants to run: {ERR.BOLD}{name}{ERR.RESET}"
            f"{ERR.YELLOW}({json.dumps(arguments or {}, default=str)}){ERR.RESET}"
        )
        if command_run is not None:
            print(f"{ERR.DIM}  Command: {json.dumps(command_run, default=str)}{ERR.RESET}")
        if risk_note:
            note_text = risk_note.get("note") if isinstance(risk_note, dict) else risk_note
            provider = risk_note.get("provider") if isinstance(risk_note, dict) else None
            label = f" ({provider})" if provider else ""
            if note_text:
                print(f"{ERR.DIM}  AI review{label}: {note_text}{ERR.RESET}")
        if command_flags:
            print(
                f"{ERR.DIM}  Flags: confirm_required={command_flags.get('confirm_required')}, "
                f"ai_review={command_flags.get('ai_review')}{ERR.RESET}"
            )
        try:
            answer = input(f"{ERR.BOLD}Proceed? [y/N]: {ERR.RESET}")
        except EOFError:
            answer = ""
        return answer.strip().lower().startswith("y")

    print("JARVIS_CONFIRM_REQUEST " + json.dumps(payload, default=str), flush=True)
    try:
        line = sys.stdin.readline()
    except Exception:
        line = ""
    return line.strip().lower().startswith("y")


def _risk_note_for(name, arguments):
    """Best-effort ai_client.risk_review() call for a saved command run
    directly at the CLI (outside the AI ask() loop entirely) \u2014 same
    "never raises, None means no second opinion available" contract as
    risk_review itself. Kept separate from that function only because it
    also has to load ai_config, which the AI ask() loop already does for
    itself elsewhere."""
    try:
        from . import ai_client, ai_config
    except ImportError:
        return None
    try:
        cfg = ai_config.load_ai_config()
    except Exception:
        return None
    return ai_client.risk_review(name, arguments, cfg)


def confirm_direct_command(name, spec, args):
    """Gate for a saved command run directly at the CLI \u2014 typed at a real
    shell ('jarvis <name> ...') or as part of a typed 'then'/'and' chain \u2014
    that has its own confirm_required flag (commands_config /
    command_tools.command_requires_confirmation). Completely separate from
    the AI-tool-call gate in ai_client, which only ever fires when the *AI*
    decides to call run_command/run_chain; this is what makes the same
    protection apply when a human runs the command by name themselves,
    from a real terminal or from the web console's command list. Returns
    True immediately, with no prompt at all, for the overwhelming majority
    of commands, which don't set the flag.
    """
    from . import command_tools

    # Used to early-return here on confirm_required alone, which meant a
    # command with ONLY ai_review set (no confirm_required) got no prompt
    # at all — the AI's risk note had nowhere to be shown. Either flag
    # should surface the popup; confirm_tool_call below already handles a
    # risk_note with no confirm_required note gracefully.
    if not (command_tools.command_requires_confirmation(name)
            or command_tools.command_requires_ai_review(name)):
        return True

    known_vars = list((spec or {}).get("vars", {}).keys())
    arguments = {v: getattr(args, v.replace("-", "_"), None) for v in known_vars}
    arguments = {k: v for k, v in arguments.items() if v is not None}

    risk_note = None
    if command_tools.command_requires_ai_review(name):
        risk_note = _risk_note_for(
            name, {"command": name, "vars": arguments, "run": (spec or {}).get("run")}
        )

    # Always attach the saved command's actual 'run' content (its real
    # shell script/steps) — not just the tool name and typed-in var
    # values — so the popup shows the user exactly what will execute,
    # regardless of whether ai_review is also on for a plain-language
    # summary of it.
    run_content = (spec or {}).get("run")
    if run_content is not None:
        risk_note = dict(risk_note) if isinstance(risk_note, dict) else (
            {"note": risk_note} if risk_note else {}
        )
        risk_note["command_run"] = run_content

    return confirm_tool_call(name, arguments, risk_note)


def resolve_and_run(commands, parser, seg, confirm=True):
    """Look up, parse, and run one 'then'/'and'-separated segment. Returns
    the exit code. `confirm=False` skips confirm_direct_command entirely \u2014
    used by command_tools._run_argv_segment, since that path is reached
    from the AI's run_command/run_chain tools, which already went through
    ai_client's own confirm-required gate (including the per-command flags
    via command_call_requires_confirmation) before this function was ever
    called; without confirm=False a flagged command would prompt twice."""
    if seg[0] not in commands:
        print(f"{ERR.RED}Unknown command: {seg[0]}{ERR.RESET}\n", file=sys.stderr)
        print_help(commands, file=sys.stderr)
        return 1
    args = parser.parse_args(seg)
    name = args.command
    spec = commands[name]
    if confirm and not confirm_direct_command(name, spec, args):
        print(f"{ERR.YELLOW}Cancelled '{name}'.{ERR.RESET}", file=sys.stderr)
        return 1
    return run_command(name, spec, args)


def _run_segment_batch(commands, parser, batch):
    """Run a batch of 'then'/'and'-separated argv segments \u2014 the whole-
    command equivalent of _run_batch() for steps. A batch of one runs
    directly; a batch of more than one runs each segment on its own
    (daemon) thread, concurrently, and waits for all of them. Returns
    [(seg, exit_code), ...] in the same order as `batch`."""
    if len(batch) == 1:
        return [(batch[0], resolve_and_run(commands, parser, batch[0]))]

    results = [None] * len(batch)

    def worker(i, seg):
        results[i] = resolve_and_run(commands, parser, seg)

    threads = [threading.Thread(target=worker, args=(i, seg), daemon=True) for i, seg in enumerate(batch)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return list(zip(batch, results))


def _parse_provider_override_value(raw):
    """'--provider' takes either one name or a comma-separated ordered list
    (e.g. 'anthropic,gemini,openai') \u2014 the order given becomes the
    try-order for that ask, see ai_client.ask()'s provider_override. Splits
    on commas, trims whitespace, drops empty entries; returns None if
    nothing usable is left (e.g. raw was just "," or blank)."""
    names = [part.strip() for part in raw.split(",")]
    names = [n for n in names if n]
    return names or None


def _extract_provider_override(argv):
    """Pull a leading '--provider NAME' / '--provider=NAME' / '--provider
    NAME1,NAME2,...' out of a raw argv list before it's joined into
    free-text for handle_ai_prompt. A comma-separated value is an ordered
    list \u2014 try NAME1 first, then NAME2, and so on \u2014 see
    ai_client.ask()'s provider_override. Returns (remaining_argv,
    override_list_or_None). Only the first match is honored; matching
    '--provider' with nothing after it (or as the very last token) is left
    alone and treated as ordinary text, since that's ambiguous rather than
    clearly a flag."""
    out = []
    override = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if override is None and tok.startswith("--provider="):
            override = _parse_provider_override_value(tok.split("=", 1)[1])
            i += 1
            continue
        if override is None and tok == "--provider" and i + 1 < len(argv):
            override = _parse_provider_override_value(argv[i + 1])
            i += 2
            continue
        out.append(tok)
        i += 1
    return out, override


def handle_ai_prompt(text, commands, provider_override=None):
    """Anything typed at jarvis that isn't a known command name lands here
    instead of the "Unknown command" error \u2014 it's treated as a message for
    the AI, not a CLI invocation. This is what 'jarvis "text"' (or even
    'jarvis some words with no quotes at all') actually runs.

    Unlike run_command, both the real reply AND a friendly explanation on
    failure are printed to stdout (in-character, as something Jarvis is
    "saying") \u2014 there's no separate underlying command output to keep
    stdout clean for here, the way there is for a real command. The
    per-provider blow-by-blow (which one was tried, why it failed) stays on
    stderr, same convention as everywhere else in jarvis.
    """
    try:
        from . import ai_client
    except ImportError:
        print(
            f"{ERR.RED}AI features need the 'requests' package, which isn't installed.{ERR.RESET}\n"
            f"From jarvis-cli/, run: {ERR.CYAN}pip install -e .{ERR.RESET}  (or just 'pip install requests')",
            file=sys.stderr,
        )
        return 1

    # The web console's Stop button kills this process outright (server.js
    # killTree -> taskkill /F on Windows, SIGTERM elsewhere). SIGTERM's
    # default action terminates immediately, skipping every finally block and
    # atexit hook, so without this handler an aborted ask left the user's
    # message unsaved — the "I aborted my first message and the whole
    # conversation was empty" bug. Handling it turns the kill into a normal
    # exit that gets one last chance to write.
    #
    # taskkill /F can't be caught at all, so this is not a complete fix on
    # Windows by itself; what makes it work there is that ai_client.ask()
    # already wrote the user's half BEFORE calling any provider (see
    # conversations.begin_exchange). This handler is the part that
    # additionally marks it interrupted rather than leaving it pending.
    def _on_interrupt(signum, _frame):
        try:
            ai_client.abandon_pending_turn(
                "interrupted" if signum != signal.SIGINT else "cancelled")
        finally:
            sys.exit(130)

    for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _on_interrupt)
        except (ValueError, OSError):
            # Not on the main thread, or the platform won't allow it. The
            # begin_exchange() write above still stands; only the
            # "interrupted" label is lost.
            pass

    def on_attempt(label):
        print(f"{ERR.DIM}\u21b3 asking {label}\u2026{ERR.RESET}", file=sys.stderr, flush=True)

    def on_route(route):
        # Phase 10 of the enhancements doc: trace the router's activation
        # reason, not just the final tool list \u2014 first match per group is
        # enough here, this is a debug trace, not a user-facing message.
        if not route.groups:
            return
        first_by_group = {}
        for group, name, phrase in route.matches:
            first_by_group.setdefault(group, (name, phrase))
        bits = ", ".join(
            f'{group} (matched "{first_by_group[group][1]}" on {first_by_group[group][0]})'
            if group in first_by_group else group
            for group in route.groups
        )
        print(f"{ERR.DIM}$ routed: {bits}{ERR.RESET}", file=sys.stderr, flush=True)

    def on_tool_call(name, arguments=None):
        labels = {
            "search_commands": "searching commands",
            "run_command": "running command",
            "run_chain": "running chain",
            "create_command": "creating command",
            "update_command": "updating command",
        }
        if name in labels:
            friendly = labels[name]
        elif name.startswith("web_"):
            friendly = name[4:].replace("_", " ")
        elif name.startswith("package_"):
            friendly = name.replace("_", " ")
        elif name.startswith("memory_"):
            friendly = name.replace("_", " ")
        elif name.startswith("spotify_"):
            friendly = name.replace("_", " ")
        elif name.startswith("playnite_"):
            friendly = name[9:].replace("_", " ")
        elif name.startswith("get_"):
            friendly = name[4:].replace("_", " ")
        elif name == "ytdl_info":
            friendly = "looking up video info"
        elif name == "ytdl_formats":
            friendly = "listing available formats"
        elif name == "ytdl_download":
            friendly = "downloading media"
        elif name in ("wifi_set", "bluetooth_set", "radio_status", "git_run", "take_screenshot"):
            friendly = name.replace("_", " ")
        else:
            friendly = name.replace("_", " ")
        detail = ""
        if arguments:
            bits = []
            for k, v in arguments.items():
                if v is None or v is False:
                    continue
                if isinstance(v, (dict, list)):
                    s = json.dumps(v, default=str, ensure_ascii=False)
                else:
                    s = str(v)
                s = s.replace("\n", " ")
                if len(s) > 90:
                    s = s[:87] + "..."
                bits.append(f"{k}={s}")
            if bits:
                detail = "  " + " ".join(bits)
                if len(detail) > 180:
                    detail = detail[:177] + "..."
        print(f"{ERR.DIM}  $ {friendly}{detail}{ERR.RESET}", file=sys.stderr, flush=True)

    def on_tool_result(name, input_tokens, output_tokens):
        # Fires once the tool call is done and BOTH halves of the (~estimated)
        # token count are known — see ai_providers._call_tool_safely. Printed
        # right under that call's "$ ..." line above, same stderr trace.
        print(
            f"{ERR.DIM}    tokens: in={input_tokens} out={output_tokens} "
            f"total={input_tokens + output_tokens}{ERR.RESET}",
            file=sys.stderr, flush=True,
        )

    # Gate for anything tool_safety.json flags confirm_required for (see
    # ai_client._make_tool_executor) — delegates to the same
    # confirm_tool_call() that confirm_direct_command() uses for a human
    # typing a flagged command straight at the CLI, so both paths behave
    # identically (tty prompt vs. piped JARVIS_CONFIRM_REQUEST protocol).
    def on_confirm_request(name, arguments, risk_note=None):
        return confirm_tool_call(name, arguments, risk_note)

    # The web UI sends one explicit conversation id per browser tab (see
    # server.js's "ask" WS handler); plain CLI use has no such id and falls
    # back to whatever conversation is "current" on disk (auto-created on
    # first ever use — see conversations.get_current_id).
    from . import conversations
    conv_id = os.environ.get("JARVIS_CONVERSATION_ID")
    if not conversations.is_valid_id(conv_id):
        conv_id = None

    # Same idea, one level up: a plain-CLI '--provider NAME' or '--provider
    # NAME1,NAME2,...' is already extracted from argv and passed in
    # directly (see _extract_provider_override / the call site in main()).
    # The web UI instead has no argv to put a flag in — its Ask-panel
    # provider-order picker sets JARVIS_PROVIDER_OVERRIDE to the same
    # comma-separated ordered-list shape per ask (see server.js's "ask" WS
    # handler) — so an explicit argv-derived override always wins, and the
    # env var is only consulted when the CLI path didn't supply one.
    if not provider_override:
        env_override = os.environ.get("JARVIS_PROVIDER_OVERRIDE")
        if env_override and env_override.strip():
            provider_override = _parse_provider_override_value(env_override)

    result = ai_client.ask(
        text, commands, on_attempt=on_attempt, on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        conversation_id=conv_id, on_confirm_request=on_confirm_request,
        on_route=on_route, provider_override=provider_override,
    )

    for label, err in result.attempts:
        print(f"{ERR.DIM}  \u2717 {label} \u2014 {err}{ERR.RESET}", file=sys.stderr, flush=True)

    prefix = f"{OUT.CYAN}{OUT.BOLD}{result.assistant_name}:{OUT.RESET} "

    if not result.ok:
        address = result.address_user_as
        if not result.attempts:
            print(
                f"{prefix}I don't have any AI providers configured yet, {address}. Run "
                f"'{OUT.CYAN}jarvis ai-config{OUT.RESET}' to find the file, then add an API key for "
                f"one of them \u2014 OpenAI, Anthropic, Gemini, xAI, Mistral, Groq, DeepSeek, "
                f"OpenRouter, or Cohere \u2014 or install Ollama locally, which needs no key at all."
            )
        else:
            print(
                f"{prefix}I tried every AI provider you've got configured and couldn't get a "
                f"response from any of them, {address}. The details are in the trace above \u2014 "
                f"it's usually a bad or missing API key, or a spending limit. "
                f"'{OUT.CYAN}jarvis ai-config{OUT.RESET}' shows you where to fix it."
            )
        return 1

    # Phase 0 (new_plan.md): baseline token/tool/round measurement for this
    # ask. Two lines: a human-readable one on stderr (visible in a plain CLI
    # trace and in the web console's stderr stream), and a machine-readable
    # "JARVIS_USAGE <json>" marker line (same protocol shape as
    # JARVIS_CONFIRM_REQUEST above) that server.js picks off and forwards to
    # the browser as a structured ws message for the debug menu.
    usage = result.usage
    if usage:
        rounds = usage.get("rounds") or []
        tool_calls = usage.get("tool_calls") or []
        print(
            f"{ERR.DIM}  tokens: in={usage.get('input_tokens', 0)} "
            f"out={usage.get('output_tokens', 0)} "
            f"total={usage.get('total_tokens', 0)}  "
            f"rounds={len(rounds)} tools={len(tool_calls)}{ERR.RESET}",
            file=sys.stderr, flush=True,
        )
        print("JARVIS_USAGE " + json.dumps(usage, default=str, ensure_ascii=False),
              flush=True)

    # Notifications raised while nothing was listening (a reminder that fired
    # from Task Scheduler, a scheduled task that finished overnight) are
    # surfaced here, immediately above the reply. This is the CLI's half of
    # notifier.py's durable inbox — the web console drains the same queue
    # over /api/notifications under a different consumer name, so neither
    # acknowledges on the other's behalf and a reminder shows up in both.
    #
    # Deliberately a drain, not a tick: reading a JSON file is microseconds,
    # whereas running due jobs here would put an unbounded amount of work
    # (an `ask` subprocess, a 300s command) on the latency path of every
    # single reply. Firing is sched-tick's job.
    if os.environ.get("JARVIS_UI") != "web":
        try:
            from . import notifier
            waiting = notifier.render_for_terminal(notifier.drain_for_cli())
            if waiting:
                print(f"{OUT.YELLOW}{waiting}{OUT.RESET}\n")
        except Exception:  # noqa: BLE001 — never block a reply over this
            pass

    print(f"{prefix}{result.text}")

    # Phase 0 (new_plan.md): baseline token/tool/round measurement for this
    # ask. Two lines: a human-readable one on stderr (visible in a plain CLI
    # trace and in the web console's stderr stream), and a machine-readable
    # "JARVIS_USAGE <json>" marker line (same protocol shape as
    # JARVIS_CONFIRM_REQUEST above) that server.js picks off and forwards to
    # the browser as a structured ws message for the debug menu.
    usage = result.usage
    if usage:
        rounds = usage.get("rounds") or []
        tool_calls = usage.get("tool_calls") or []
        print(
            f"{ERR.DIM}  tokens: in={usage.get('input_tokens', 0)} "
            f"out={usage.get('output_tokens', 0)} "
            f"total={usage.get('total_tokens', 0)}  "
            f"rounds={len(rounds)} tools={len(tool_calls)}{ERR.RESET}",
            file=sys.stderr, flush=True,
        )
        print("JARVIS_USAGE " + json.dumps(usage, default=str, ensure_ascii=False),
              flush=True)

    return 0


def _format_log_entry(entry):
    ts = (entry.get("ts") or "")[11:19] or entry.get("ts") or "?"
    direction = entry.get("direction") or "?"
    provider = entry.get("provider")
    color = {
        "request": ERR.CYAN, "response": ERR.GREEN, "tool_call": ERR.YELLOW,
        "tool_result": ERR.YELLOW, "error": ERR.RED,
    }.get(direction, ERR.DIM)
    head = f"{ERR.DIM}[{ts}]{ERR.RESET} {color}{direction:<11}{ERR.RESET}"
    if provider:
        head += f" {ERR.DIM}({provider}){ERR.RESET}"
    body = json.dumps(entry.get("data"), ensure_ascii=False, default=str)
    if len(body) > 500:
        body = body[:500] + "..."
    return f"{head}  {body}"


def run_logs_command(argv, commands):
    """'jarvis logs' \u2014 conversation-based viewer for the raw JSON traffic
    between the model and this CLI backend (see logs.py). Lists every
    conversation that has at least one logged entry, lets you pick one
    (or pass an id/number directly), prints its latest 50 log lines, then
    lets you keep going: pressing Enter just exits, or typing a message
    continues that exact conversation via the normal ask path.
    """
    from . import logs as logs_mod
    from . import conversations

    items = logs_mod.list_logged_conversations()
    if not items:
        print(f"{ERR.DIM}No logs yet \u2014 logs are written as soon as you ask {current_display_name()} something.{ERR.RESET}")
        return

    picked = None
    arg = argv[0].strip() if argv else ""
    if arg:
        if conversations.is_valid_id(arg):
            picked = arg
        else:
            try:
                idx = int(arg) - 1
                if 0 <= idx < len(items):
                    picked = items[idx]["id"]
            except ValueError:
                pass
        if not picked:
            print(json.dumps({"error": "no such conversation"}))
            sys.exit(1)
    else:
        print(f"{ERR.BOLD}Conversations with logs:{ERR.RESET}")
        for i, it in enumerate(items, start=1):
            marker = "" if it["exists"] else f" {ERR.DIM}(deleted){ERR.RESET}"
            when = (it.get("updated_at") or "")[:19].replace("T", " ")
            print(f"  {ERR.CYAN}{i}{ERR.RESET}. {it['title']}{marker}  {ERR.DIM}{when}  [{it['id']}]{ERR.RESET}")
        try:
            choice = input(f"\n{ERR.BOLD}Pick a conversation [1-{len(items)}]: {ERR.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        try:
            idx = int(choice) - 1
        except ValueError:
            idx = -1
        if not (0 <= idx < len(items)):
            print(f"{ERR.RED}Not a valid choice.{ERR.RESET}")
            sys.exit(1)
        picked = items[idx]["id"]

    entries = logs_mod.read_entries(picked, limit=50)
    print(f"\n{ERR.BOLD}Last {len(entries)} log line(s) for {picked}:{ERR.RESET}")
    if not entries:
        print(f"  {ERR.DIM}(empty){ERR.RESET}")
    for entry in entries:
        print("  " + _format_log_entry(entry))

    try:
        follow_up = input(
            f"\n{ERR.BOLD}Enter to exit, or type a message to continue this conversation: {ERR.RESET}"
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not follow_up:
        return
    os.environ["JARVIS_CONVERSATION_ID"] = picked
    sys.exit(handle_ai_prompt(follow_up, commands) or 0)


# Scheduling / notification subcommands. Grouped into one dispatcher rather
# than another dozen `if argv[0] == ...` blocks in main() — they share a
# module import, an output convention (JSON on stdout, like every other
# machine-readable subcommand server.js consumes), and an id-validation step.
#
# Every one of these is the HUMAN side of the engine, which is why they pass
# trusted=True to scheduler.create() and why sched-approve exists here and
# has no tool equivalent: the approval gate is only worth anything if the
# model can't reach it (see scheduler._needs_approval).
SCHEDULER_COMMANDS = {
    "sched-list", "sched-tick", "sched-add", "sched-show", "sched-cancel",
    "sched-pause", "sched-resume", "sched-snooze", "sched-approve",
    "sched-signal", "sched-clear", "notify-send", "notify-list",
    "notify-ack", "notify-clear", "notify-config",
    "conv-search", "mcp-status", "mcp-refresh", "mcp-config", "mcp-call",
    "mcp-tools",
}


def run_search_or_mcp_command(argv):
    """conv-search and the mcp-* family. Same JSON-on-stdout convention as
    the scheduler commands; kept in its own function only because the two
    feature areas share nothing but that convention."""
    cmd, rest = argv[0], argv[1:]

    def emit(payload, code=0):
        print(json.dumps(payload, indent=2, default=str))
        return code

    if cmd == "conv-search":
        from . import conv_search
        if not rest or not rest[0].strip():
            return emit({"error": "usage: jarvis conv-search <query> [--phrase|--regex] "
                                  "[--tools] [--text] [--in <conv-id>] [--since YYYY-MM-DD]"}, 1)
        query = rest[0]
        flags = rest[1:]
        mode = "regex" if "--regex" in flags else "phrase" if "--phrase" in flags else "words"

        def flag_value(flag):
            return flags[flags.index(flag) + 1] if flag in flags and len(flags) > flags.index(flag) + 1 else None

        try:
            results = conv_search.search(
                query, mode=mode,
                include_tools="--tools" in flags,
                conv_id=flag_value("--in"),
                since=flag_value("--since"),
                until=flag_value("--until"),
            )
        except conv_search.SearchError as e:
            return emit({"error": str(e)}, 1)
        if "--text" in flags:
            print(conv_search.render_for_terminal(results, query))
            return 0
        return emit({"query": query, "mode": mode,
                     **conv_search.summarize(results), "results": results})

    from . import mcp_client

    if cmd == "mcp-config":
        print(mcp_client.ensure_config())
        return 0

    if cmd == "mcp-status":
        return emit(mcp_client.status())

    if cmd == "mcp-tools":
        cached = mcp_client.cached_tools()
        return emit({"servers": {name: [t.get("name") for t in entry.get("tools") or []]
                                 for name, entry in cached.items()}})

    if cmd == "mcp-refresh":
        # The one command that actually spawns every configured server. Run
        # it after editing mcp_config.json — the catalog reads a cache, so a
        # newly-added server stays invisible until this populates it.
        try:
            summary = mcp_client.refresh(rest[0].strip() if rest and rest[0].strip() else None)
        except mcp_client.MCPError as e:
            return emit({"error": str(e)}, 1)
        failed = [s for s in summary if s.get("error")]
        return emit({"refreshed": summary, "ok": not failed}, 1 if failed else 0)

    if cmd == "mcp-call":
        # jarvis mcp-call <server> <tool> [json-args] — the debugging path,
        # so a user can prove a server works without going through the model.
        if len(rest) < 2:
            return emit({"error": "usage: jarvis mcp-call <server> <tool> [json-arguments]"}, 1)
        try:
            arguments = json.loads(rest[2]) if len(rest) > 2 and rest[2].strip() else {}
        except json.JSONDecodeError as e:
            return emit({"error": "arguments must be valid JSON: %s" % e}, 1)
        try:
            result = mcp_client.call_tool(rest[0].strip(), rest[1].strip(), arguments)
        except mcp_client.MCPError as e:
            return emit({"error": str(e)}, 1)
        finally:
            mcp_client.close_all()
        return emit(result)

    return emit({"error": "unknown command %r" % cmd}, 1)


def run_scheduler_command(argv):
    """Handle every sched-*/notify-* subcommand. Returns an exit code.

    Prints JSON on stdout throughout, because both consumers are machines:
    web/server.js's /api/scheduled* routes, and anything a user wires into
    Task Scheduler/cron. The one exception is `sched-list --text`, for
    reading at a terminal.
    """
    from . import notifier, scheduler

    cmd = argv[0]
    rest = argv[1:]

    def emit(payload, code=0):
        print(json.dumps(payload, indent=2, default=str))
        return code

    def need_id():
        if not rest or not scheduler.is_valid_id(rest[0]):
            emit({"error": "usage: jarvis %s <job-id>" % cmd}, 1)
            return None
        return rest[0].strip().lower()

    try:
        if cmd == "sched-list":
            if "--text" in rest:
                jobs = scheduler.list_jobs(include_finished="--all" in rest)
                if not jobs:
                    print("Nothing scheduled.")
                    return 0
                for job in jobs:
                    s = scheduler.summarize(job)
                    flag = "  [NEEDS APPROVAL]" if s["needs_approval"] else ""
                    eta = ("  (in %s)" % s["in"]) if s["in"] else ""
                    print("%s  %-8s %-9s %s%s%s" % (
                        s["id"], s["kind"], s["status"], s["when"], eta, flag))
                    print("    %s" % s["title"])
                return 0
            return emit(scheduler.overview())

        if cmd == "sched-tick":
            # The workhorse. Run from Task Scheduler/cron every minute, or by
            # web/server.js on an interval — see scheduler.py's docstring on
            # why an explicit tick exists at all.
            result = scheduler.tick(startup="--startup" in rest)
            return emit(result)

        if cmd == "sched-show":
            job_id = need_id()
            if not job_id:
                return 1
            job = scheduler.get(job_id)
            if not job:
                return emit({"error": "no such job"}, 1)
            return emit(job)

        if cmd == "sched-add":
            # jarvis sched-add <when> <message> [kind]
            # The deliberately minimal human path: a reminder/notification.
            # Anything that RUNS something is created through the web panel
            # or the AI tools, both of which can express an action properly —
            # cramming a tool's argument dict into positional argv would be
            # worse than not offering it here.
            if len(rest) < 2:
                return emit({"error": "usage: jarvis sched-add <when> <message> [task|notify|reminder]"}, 1)
            kind = rest[2].strip().lower() if len(rest) > 2 else "reminder"
            job = scheduler.create(kind=kind, title=rest[1], when=rest[0],
                                   message=rest[1], trusted=True)
            return emit(scheduler.summarize(job))

        if cmd in ("sched-cancel", "sched-pause", "sched-resume", "sched-approve"):
            job_id = need_id()
            if not job_id:
                return 1
            fn = {"sched-cancel": scheduler.cancel, "sched-pause": scheduler.pause,
                  "sched-resume": scheduler.resume, "sched-approve": scheduler.approve}[cmd]
            return emit(scheduler.summarize(fn(job_id)))

        if cmd == "sched-snooze":
            job_id = need_id()
            if not job_id:
                return 1
            delay = " ".join(rest[1:]).strip() or "10 minutes"
            return emit(scheduler.summarize(scheduler.snooze(job_id, delay)))

        if cmd == "sched-signal":
            if not rest or not rest[0].strip():
                return emit({"error": "usage: jarvis sched-signal <event> [detail]"}, 1)
            detail = " ".join(rest[1:]).strip() or None
            return emit(scheduler.signal(rest[0], payload=detail))

        if cmd == "sched-clear":
            return emit({"removed": scheduler.clear_finished()})

        if cmd == "notify-send":
            if len(rest) < 1:
                return emit({"error": "usage: jarvis notify-send <message> [title] [channels,csv]"}, 1)
            channels = [c.strip() for c in rest[2].split(",")] if len(rest) > 2 else None
            record = notifier.notify(
                title=rest[1] if len(rest) > 1 else "Jarvis",
                message=rest[0], channels=channels, kind="notify")
            return emit(record)

        if cmd == "notify-list":
            # Consumer defaults to "web" because server.js is the caller that
            # matters; a terminal drain goes through drain_for_cli instead so
            # the two never acknowledge on each other's behalf.
            consumer = rest[0].strip() if rest and rest[0].strip() else "web"
            return emit({"notifications": notifier.pending(consumer=consumer)})

        if cmd == "notify-ack":
            if not rest:
                return emit({"error": "usage: jarvis notify-ack <id[,id,...]> [consumer]"}, 1)
            ids = [i.strip() for i in rest[0].split(",") if i.strip()]
            consumer = rest[1].strip() if len(rest) > 1 else "web"
            return emit({"acknowledged": notifier.acknowledge(ids, consumer=consumer)})

        if cmd == "notify-clear":
            consumer = rest[0].strip() if rest and rest[0].strip() else None
            return emit({"cleared": notifier.clear(consumer=consumer)})

        if cmd == "notify-config":
            print(notifier.ensure_config())
            return 0

    except scheduler.SchedulerError as e:
        return emit({"error": str(e)}, 1)
    except Exception as e:  # noqa: BLE001
        # A scheduler subcommand is often running unattended out of Task
        # Scheduler, where a traceback on stderr goes nowhere anyone will
        # read. A JSON error on stdout at least lands in the same place the
        # success output would.
        return emit({"error": "%s: %s" % (type(e).__name__, e)}, 1)

    return emit({"error": "unknown scheduler command %r" % cmd}, 1)


def main():
    commands = load_commands()
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        print_help(commands)
        return

    if argv[0] == "config":
        print(CONFIG_FILE)
        return

    if argv[0] in ("version", "--version", "-v"):
        # Deliberately at the very top of dispatch, same tier as -h/--help
        # and config -- this needs to work even if commands.json is broken
        # or empty, since its whole point is "which exe am I even running".
        from . import build_info
        print(build_info.version_string())
        return

    if argv[0] == "_internal_retitle":
        # Hidden entry point: the detached subprocess ai_client._spawn_title_
        # update() launches to (re)title a conversation after this same
        # process's own reply has already been printed and it's about to
        # exit — see that function's docstring for why a background thread
        # doesn't survive to do this instead. Not documented in --help;
        # nothing but jarvis itself should ever call this directly.
        if len(argv) < 3:
            sys.exit(1)
        try:
            exchange_count = int(argv[2])
        except ValueError:
            sys.exit(1)
        from . import ai_client
        ai_client.run_internal_retitle(argv[1], exchange_count)
        return

    if argv[0] == "ai-config":
        from . import ai_config
        ai_config.ensure_ai_config()
        print(ai_config.AI_CONFIG_FILE)
        return

    if argv[0] == "mode":
        from . import ai_client
        current = ai_client.current_mode()
        print(json.dumps({
            "mode": current,
            "label": ai_client.MODE_LABELS.get(current, current),
            "options": ai_client.mode_options(),
        }, indent=2))
        return

    if argv[0] == "mode-set":
        from . import ai_client
        requested = argv[1].strip().lower() if len(argv) > 1 and argv[1].strip() else ""
        if requested not in ai_client.PROMPT_MODES:
            print(json.dumps({
                "error": f"usage: jarvis mode-set <{'|'.join(ai_client.PROMPT_MODES)}>",
                "options": ai_client.mode_options(),
            }))
            sys.exit(1)
        new_mode = ai_client.set_mode(requested)
        print(json.dumps({
            "mode": new_mode,
            "label": ai_client.MODE_LABELS[new_mode],
            "options": ai_client.mode_options(),
        }, indent=2))
        return

    if argv[0] == "ai-clear":
        from . import conversations
        conv_id = os.environ.get("JARVIS_CONVERSATION_ID")
        if not conversations.is_valid_id(conv_id):
            conv_id = conversations.get_current_id()
        conversations.clear(conv_id)
        print("Conversation history cleared \u2014 next ask starts with a clean slate.")
        return

    if argv[0] == "ai-drop-from":
        from . import conversations
        conv_id = os.environ.get("JARVIS_CONVERSATION_ID")
        if not conversations.is_valid_id(conv_id):
            conv_id = conversations.get_current_id()
        target = " ".join(argv[1:]).strip()
        conversations.drop_from_user(conv_id, target)
        print("ok")
        return

    if argv[0] == "conv-new":
        from . import conversations
        title = " ".join(argv[1:]).strip() or None
        conv_id = conversations.new_conversation(title=title)
        record = conversations.get_conversation(conv_id)
        print(json.dumps({
            "id": record["id"],
            "title": record["title"],
            "soft_context": record["soft_context"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "exchange_count": 0,
        }, indent=2))
        return

    if argv[0] == "conv-list":
        from . import conversations
        query = " ".join(argv[1:]).strip() or None
        print(json.dumps(conversations.list_conversations(query), indent=2))
        return

    if argv[0] == "conv-show":
        from . import conversations
        conv_id = argv[1].strip() if len(argv) > 1 else ""
        record = conversations.get_conversation(conv_id) if conversations.is_valid_id(conv_id) else None
        if not record:
            print(json.dumps({"error": "no such conversation"}))
            sys.exit(1)
        print(json.dumps(record, indent=2))
        return

    if argv[0] == "conv-switch":
        from . import conversations
        conv_id = argv[1].strip() if len(argv) > 1 else ""
        if not conversations.is_valid_id(conv_id) or not conversations.get_conversation(conv_id):
            print(json.dumps({"error": "no such conversation"}))
            sys.exit(1)
        conversations.set_current(conv_id)
        print(json.dumps({"ok": True, "id": conv_id}))
        return

    if argv[0] == "conv-delete":
        from . import conversations
        conv_id = argv[1].strip() if len(argv) > 1 else ""
        if not conversations.is_valid_id(conv_id):
            print(json.dumps({"error": "invalid conversation id"}))
            sys.exit(1)
        ok = conversations.delete_conversation(conv_id)
        print(json.dumps({"ok": ok}))
        if not ok:
            sys.exit(1)
        return

    if argv[0] == "logs":
        run_logs_command(argv[1:], commands)
        return

    if argv[0] == "logs-list":
        # Non-interactive, JSON-emitting sibling of 'jarvis logs' — used by
        # the web UI (see server.js's /api/logs) which has no tty to prompt
        # against. Plain CLI usage never calls this directly.
        from . import logs as logs_mod
        print(json.dumps(logs_mod.list_logged_conversations(), indent=2))
        return

    if argv[0] == "logs-show":
        from . import logs as logs_mod
        from . import conversations
        conv_id = argv[1].strip() if len(argv) > 1 else ""
        if not conversations.is_valid_id(conv_id):
            print(json.dumps({"error": "invalid conversation id"}))
            sys.exit(1)
        if not logs_mod.has_log(conv_id):
            print(json.dumps({"error": "no such conversation"}))
            sys.exit(1)
        limit = None
        if len(argv) > 2 and argv[2].strip():
            try:
                limit = int(argv[2].strip())
            except ValueError:
                limit = None
        entries = logs_mod.read_entries(conv_id, limit=limit or 50)
        print(json.dumps({"id": conv_id, "entries": entries}, indent=2))
        return

    if argv[0] == "logs-clear":
        from . import logs as logs_mod
        from . import conversations
        conv_id = argv[1].strip() if len(argv) > 1 else ""
        if not conversations.is_valid_id(conv_id):
            print(json.dumps({"error": "invalid conversation id"}))
            sys.exit(1)
        ok = logs_mod.clear(conv_id)
        print(json.dumps({"ok": ok}))
        if not ok:
            sys.exit(1)
        return

    if argv[0] == "playnite-config":
        from . import playnite_config
        playnite_config.ensure_config()
        print(playnite_config.CONFIG_FILE)
        return

    if argv[0] == "spotify-config":
        from . import spotify_config
        spotify_config.ensure_config()
        print(spotify_config.CONFIG_FILE)
        return

    if argv[0] == "spotify-login":
        from . import spotify_api, spotify_config
        spotify_config.ensure_config()
        ok, msg = spotify_api.login_interactive()
        print(msg)
        sys.exit(0 if ok else 1)

    if argv[0] == "memory-config":
        from . import memory
        memory.ensure_config()
        print(memory.CONFIG_FILE)
        return

    if argv[0] == "voice-config":
        from .voice import config as voice_config
        voice_config.ensure_voice_config()
        if "--json" in argv[1:]:
            # Machine-readable status for web/server.js's /api/status —
            # just the bit a UI needs to decide whether to show voice
            # controls at all, not the full (potentially secret-bearing,
            # e.g. elevenlabs.api_key) config.
            cfg = voice_config.load_voice_config()
            print(json.dumps({
                "path": str(voice_config.VOICE_CONFIG_FILE),
                "enabled": voice_config.voice_enabled(cfg),
            }))
            return
        print(voice_config.VOICE_CONFIG_FILE)
        return

    if argv[0] == "speak":
        # jarvis speak <text> — synthesize()+play() on this machine's
        # default output device. server.js's web path calls this same
        # subcommand but with --no-play (see below) so it can stream the
        # raw bytes to the browser instead of touching the host speaker.
        from .voice import config as voice_config, tts as voice_tts

        if not voice_config.voice_enabled():
            print(json.dumps({"error": "Voice is disabled (voice_config.json: \"enabled\": false)."}))
            sys.exit(1)

        no_play = "--no-play" in argv[1:]
        out_path = None
        rest = [a for a in argv[1:] if a != "--no-play"]
        if len(rest) >= 2 and rest[-2] == "--out":
            out_path = rest[-1]
            rest = rest[:-2]
        text = " ".join(rest).strip()
        if not text:
            print(json.dumps({"error": "usage: jarvis speak <text> [--no-play] [--out <path>]"}))
            sys.exit(1)

        result = voice_tts.speak(text, play=not no_play, out_path=out_path)
        if "error" in result:
            print(json.dumps({"error": result["error"]}))
            sys.exit(1)
        # Never dump raw audio bytes to stdout/JSON — report metadata only.
        # --out (or the browser path via server.js) is how the caller
        # actually gets the audio; stdout stays clean JSON, same
        # convention as every other machine-readable jarvis subcommand.
        print(json.dumps({
            "ok": True, "provider": result.get("provider"), "mime": result.get("mime"),
            "played": result.get("played"), "path": result.get("path"),
        }))
        return

    if argv[0] == "transcribe":
        # jarvis transcribe <audio file> — speech-to-text on an existing
        # WAV file. Used directly by a person with a recording on disk,
        # and by server.js's /api/voice/transcribe on a browser upload
        # saved to a temp file (see voice/stt.py's module docstring).
        from .voice import config as voice_config, stt as voice_stt

        if not voice_config.voice_enabled():
            print(json.dumps({"error": "Voice is disabled (voice_config.json: \"enabled\": false)."}))
            sys.exit(1)

        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis transcribe <audio file>"}))
            sys.exit(1)
        result = voice_stt.transcribe(argv[1].strip())
        if "error" in result:
            print(json.dumps({"error": result["error"]}))
            sys.exit(1)
        print(json.dumps(result))
        return

    if argv[0] == "listen":
        # jarvis listen — one full voice turn: record from the default mic
        # until silence, transcribe it, hand the text to the normal ask()
        # pipeline (same commands/tools/memory as typed input), then speak
        # the reply back. CLI-only (audio_io.record()/play() need a real
        # local mic/speaker) — the web UI does the mic/speaker parts itself
        # in the browser and calls /api/voice/transcribe + /api/voice/speak
        # around a normal "ask" instead (see jarvis-enhancement-plan.md §3a).
        from .voice import audio_io as voice_audio_io, config as voice_config, stt as voice_stt, tts as voice_tts
        from . import ai_client, conversations

        if not voice_config.voice_enabled():
            print(f"{ERR.RED}Voice is disabled (voice_config.json: \"enabled\": false).{ERR.RESET}", file=sys.stderr)
            sys.exit(1)

        def status(msg):
            print(f"{ERR.DIM}{msg}{ERR.RESET}", file=sys.stderr, flush=True)

        rec = voice_audio_io.record(on_status=status)
        if "error" in rec:
            print(f"{ERR.RED}{rec['error']}{ERR.RESET}", file=sys.stderr)
            sys.exit(1)

        try:
            transcript = voice_stt.transcribe(rec["path"])
        finally:
            Path(rec["path"]).unlink(missing_ok=True)

        if "error" in transcript:
            print(f"{ERR.RED}{transcript['error']}{ERR.RESET}", file=sys.stderr)
            sys.exit(1)

        heard = transcript["text"]
        print(f"{OUT.DIM}you said: {heard}{OUT.RESET}")

        conv_id = os.environ.get("JARVIS_CONVERSATION_ID")
        if not conversations.is_valid_id(conv_id):
            conv_id = None

        result = ai_client.ask(heard, commands, conversation_id=conv_id, on_attempt=status)
        prefix = f"{OUT.CYAN}{OUT.BOLD}{result.assistant_name}:{OUT.RESET} "
        if not result.ok:
            print(f"{prefix}I couldn't get a response from any configured AI provider, "
                  f"{result.address_user_as}.")
            sys.exit(1)

        print(f"{prefix}{result.text}")
        speak_result = voice_tts.speak(result.text)
        if "error" in speak_result:
            print(f"{ERR.DIM}(couldn't speak the reply: {speak_result['error']}){ERR.RESET}",
                  file=sys.stderr)
        return

    if argv[0] == "everything-config":
        from . import everything_config
        everything_config.ensure_config()
        print(everything_config.CONFIG_FILE)
        return

    # ---- Skills (see skills.py) -----------------------------------------
    # Dedicated commands rather than routing the web manager through
    # `tool-run`: the manager is a person editing their own files, so it must
    # not inherit the AI-facing confirm-gating on remove_skill, and it needs
    # two operations (read/write the raw SKILL.md) that are deliberately NOT
    # exposed as model tools — handing the model arbitrary file-overwrite on
    # its own instruction set is a bad idea regardless of how convenient it'd
    # be. Every one prints a single JSON object to stdout, same contract as
    # tools-list/tool-run, so server.js parses them all identically.
    if argv[0] == "skills-list":
        from . import skills as skills_mod
        print(json.dumps({"skills": skills_mod.list_skills(), "stats": skills_mod.stats()}, indent=2))
        return

    if argv[0] == "skills-get":
        from . import skills as skills_mod
        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis skills-get <name>"}))
            sys.exit(1)
        result = skills_mod.export_skill(argv[1].strip())
        print(json.dumps(result, indent=2))
        sys.exit(1 if result.get("error") else 0)

    if argv[0] == "skills-save":
        # jarvis skills-save <name> <full SKILL.md text>
        # Content arrives as one argv entry (spawn, no shell) so newlines and
        # quoting in the markdown never need escaping.
        from . import skills as skills_mod
        if len(argv) < 3:
            print(json.dumps({"error": "usage: jarvis skills-save <name> <content>"}))
            sys.exit(1)
        result = skills_mod.write_skill_file(argv[1].strip(), argv[2])
        print(json.dumps(result, indent=2))
        sys.exit(1 if result.get("error") else 0)

    if argv[0] in ("skills-add", "skilladd"):
        from . import skills as skills_mod
        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": f"usage: jarvis {argv[0]} <folder|file|.zip|markdown> [name]"}))
            sys.exit(1)
        name = argv[2].strip() if len(argv) > 2 and argv[2].strip() else None
        result = skills_mod.add_skill(argv[1], name)
        print(json.dumps(result, indent=2))
        sys.exit(1 if result.get("error") else 0)

    if argv[0] in ("skills-create", "skillmake"):
        # jarvis skills-create <name> <description> <instructions>
        from . import skills as skills_mod
        if len(argv) < 4:
            print(json.dumps({"error": f"usage: jarvis {argv[0]} <name> <description> <instructions>"}))
            sys.exit(1)
        result = skills_mod.create_skill(argv[1].strip(), argv[2].strip(), argv[3])
        print(json.dumps(result, indent=2))
        sys.exit(1 if result.get("error") else 0)

    if argv[0] == "skills-remove":
        from . import skills as skills_mod
        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis skills-remove <name>"}))
            sys.exit(1)
        result = skills_mod.remove_skill(argv[1].strip())
        print(json.dumps(result, indent=2))
        sys.exit(1 if result.get("error") else 0)

    if argv[0] == "skillload":
        # jarvis skillload <name> [conversation-id]
        # Force a skill's full instructions into every ask, either for one
        # conversation (pass its id) or globally (omit it) — see
        # skill_stickiness.py. The CLI half of the "/skillload <name>" chat
        # command; the web UI's slash command hits the same code path
        # through POST /api/skills/:name/load.
        from . import skill_stickiness, skills as skills_mod
        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis skillload <name> [conversation-id]"}))
            sys.exit(1)
        conv_id = argv[2].strip() if len(argv) > 2 else ""
        info = skills_mod.export_skill(argv[1].strip())
        if info.get("error"):
            print(json.dumps({"error": info["error"]}))
            sys.exit(1)
        meta, _ = skills_mod.parse_frontmatter(info["content"])
        real_name = (meta.get("name") or info["slug"]).strip()
        skill_stickiness.load(conv_id or None, real_name)
        print(json.dumps({
            "loaded": True, "name": real_name,
            "scope": conv_id or "global (every conversation)",
        }, indent=2))
        return

    if argv[0] == "skillunload":
        # jarvis skillunload <name> [conversation-id]   -- drop one
        # jarvis skillunload --all [conversation-id]    -- drop everything
        from . import skill_stickiness, skills as skills_mod
        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis skillunload <name>|--all [conversation-id]"}))
            sys.exit(1)
        raw_name = argv[1].strip()
        conv_id = argv[2].strip() if len(argv) > 2 else ""
        if raw_name == "--all":
            skill_stickiness.unload_all(conv_id or None)
            print(json.dumps({"unloaded_all": True, "scope": conv_id or "global (every conversation)"}, indent=2))
            return
        real_name = raw_name
        info = skills_mod.export_skill(raw_name)
        if not info.get("error"):
            meta, _ = skills_mod.parse_frontmatter(info["content"])
            real_name = (meta.get("name") or info["slug"]).strip()
        # Unload by whatever name was given even if the skill itself no
        # longer exists — that's exactly the case where cleanup matters.
        skill_stickiness.unload(conv_id or None, real_name)
        print(json.dumps({
            "unloaded": True, "name": real_name,
            "scope": conv_id or "global (every conversation)",
        }, indent=2))
        return

    if argv[0] in ("conv-search",) or argv[0].startswith("mcp-"):
        if argv[0] in SCHEDULER_COMMANDS:
            sys.exit(run_search_or_mcp_command(argv) or 0)

    if argv[0] in SCHEDULER_COMMANDS:
        sys.exit(run_scheduler_command(argv) or 0)

    if argv[0] == "tools-list":
        from . import tools as system_tools
        print(json.dumps(system_tools.tools_list_payload(), indent=2))
        return

    if argv[0] == "tool-run":
        # jarvis tool-run <name> [json-arguments] [mode]
        # Runs one AI tool directly (bypassing the model) and prints its
        # JSON result. Powers the web UI's debug dashboard so a person can
        # invoke any tool the AI can call and see exactly what comes back.
        #
        # The optional trailing `mode` is the debug panel's own local-only
        # capacity override (see its mode switch) — translated to a
        # tool_result_verbosity and applied via tool_result_shaping.
        # shape_result(), the exact same trimming a normal ask() already
        # applies to every tool result at that mode. Anything not one of
        # the real PROMPT_MODES (including omitted/blank) means "no
        # override", so the result comes back untouched, same as before
        # this argument existed.
        from . import tools as system_tools

        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis tool-run <name> [json-arguments] [mode]"}))
            sys.exit(1)

        tool_name = argv[1].strip()
        raw_args = argv[2] if len(argv) > 2 else "{}"
        raw_mode = argv[3].strip() if len(argv) > 3 and argv[3].strip() else None
        try:
            arguments = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"arguments must be valid JSON: {e}"}))
            sys.exit(1)
        if not isinstance(arguments, dict):
            print(json.dumps({"error": "arguments must be a JSON object"}))
            sys.exit(1)

        verbosity = None
        if raw_mode:
            from . import ai_client
            if raw_mode in ai_client.PROMPT_MODES:
                verbosity = ai_client._MODE_BY_NAME[raw_mode].get("tool_result_verbosity")

        # server.js's /api/tools/run treats this process's ENTIRE stdout as
        # one JSON document (see its `JSON.parse(result.stdout)`). But a
        # saved command's own steps run via `subprocess.Popen(cmd,
        # shell=True)` with no stdout redirection (see _run_batch) — they
        # inherit this process's real stdout file descriptor directly, so
        # anything a command actually prints (e.g. `echo hi`) lands on the
        # same stream, *before* the JSON below, and breaks that JSON.parse.
        # The debug dashboard then shows "Tool run failed" even though the
        # command's side effect already happened. Fixing this requires an
        # OS-level fd swap, not just redirecting Python's sys.stdout —
        # redirect_stdout doesn't touch a child process's inherited fd 1.
        # Any real output the command produced is discarded here; that's
        # fine, since tool-run's whole contract is a single JSON result,
        # never live output (that's what the "run"/"ask" websocket
        # streaming flows are for).
        saved_stdout_fd = os.dup(1)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            sys.stdout.flush()
            os.dup2(devnull_fd, 1)
            result = system_tools.execute_tool(tool_name, arguments, verbosity=verbosity)
        finally:
            sys.stdout.flush()
            os.dup2(saved_stdout_fd, 1)
            os.close(devnull_fd)
            os.close(saved_stdout_fd)
        print(json.dumps(result, indent=2, default=str))
        return

    if argv[0] == "tool-preview":
        # jarvis tool-preview <name> [json-arguments] [mode]
        # Reports whether a tool call would be gated by a confirmation
        # prompt and (if AI review is on for it) a risk note from a second
        # configured provider — without actually running the tool. Powers
        # the web UI's debug dashboard: the RUN button calls this first so
        # a person sees exactly what they're about to approve before
        # anything real happens.
        #
        # The optional trailing `mode` is a one-off, local-only override
        # (see the debug panel's own mode switch in the web UI) for how
        # long/detailed that risk note is — it's read once for this single
        # risk_review() call and never touches defaults.prompt_mode or
        # anything persisted, so it can't affect the real global capacity
        # mode "jarvis mode-set" controls. Anything other than one of the
        # real PROMPT_MODES (including omitted/blank) is treated as "no
        # override", same as before this argument existed.
        from . import tool_safety, command_tools

        if len(argv) < 2 or not argv[1].strip():
            print(json.dumps({"error": "usage: jarvis tool-preview <name> [json-arguments] [mode]"}))
            sys.exit(1)

        tool_name = argv[1].strip()
        raw_args = argv[2] if len(argv) > 2 else "{}"
        raw_mode = argv[3].strip() if len(argv) > 3 and argv[3].strip() else None
        try:
            arguments = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"arguments must be valid JSON: {e}"}))
            sys.exit(1)
        if not isinstance(arguments, dict):
            print(json.dumps({"error": "arguments must be a JSON object"}))
            sys.exit(1)

        flags = tool_safety.get_flags(tool_name)
        # Tool-level flags only cover run_command/run_chain the *tool* —
        # they say nothing about the specific saved *command* the debug
        # dashboard is about to run (its own confirm_required/ai_review,
        # same as command_tools.command_call_requires_confirmation/
        # command_call_requires_ai_review do for the real AI chat path in
        # ai_client._make_tool_executor). Without this OR, a command
        # flagged ai_review=True but relying on run_command's already-True
        # tool-level confirm_required never actually got its AI review
        # note computed here, so the debug popup showed a bare confirm
        # with no note where the chat path would have shown one.
        effective_confirm_required = flags["confirm_required"] or command_tools.command_call_requires_confirmation(tool_name, arguments)
        effective_ai_review = flags["ai_review"] or command_tools.command_call_requires_ai_review(tool_name, arguments)
        risk_note = None
        if effective_ai_review:
            try:
                from . import ai_client, ai_config
                mode = raw_mode if raw_mode in ai_client.PROMPT_MODES else None
                review_arguments = arguments
                if tool_name in ("run_command", "run_chain"):
                    expanded = command_tools.resolved_run_for_review(tool_name, arguments)
                    if expanded is not None:
                        review_arguments = expanded
                risk_note = ai_client.risk_review(tool_name, review_arguments, ai_config.load_ai_config(), mode=mode)
            except Exception:
                risk_note = None

        # Same as ai_client._make_tool_executor and confirm_direct_command:
        # always show the real resolved shell content for run_command/
        # run_chain, and the resulting command's own flags for
        # create_command/update_command, regardless of whether ai_review
        # produced a plain-language note above.
        if tool_name in ("run_command", "run_chain"):
            command_run = command_tools.resolved_run_for_review(tool_name, arguments)
            if command_run is not None:
                risk_note = dict(risk_note) if isinstance(risk_note, dict) else (
                    {"note": risk_note} if risk_note else {}
                )
                risk_note["command_run"] = command_run

        if tool_name in ("create_command", "update_command"):
            try:
                from . import ai_client
                command_flags = ai_client._command_flags_for_call(tool_name, arguments)
            except Exception:
                command_flags = None
            if command_flags is not None:
                risk_note = dict(risk_note) if isinstance(risk_note, dict) else (
                    {"note": risk_note} if risk_note else {}
                )
                risk_note["command_flags"] = command_flags

        print(json.dumps({
            "name": tool_name,
            "arguments": arguments,
            "confirm_required": effective_confirm_required,
            "ai_review": effective_ai_review,
            "risk_note": risk_note,
        }, indent=2, default=str))
        return

    if argv[0] == "tool-safety-set":
        # jarvis tool-safety-set <name> <confirm_required|ai_review> <true|false>
        # Flips one of a tool's two safety toggles (see tool_safety.py).
        # Powers the "Toggle warning:" / "Toggle AI review:" switches in
        # the web UI's debug dashboard, right under a tool's description.
        from . import tool_safety

        if len(argv) < 4:
            print(json.dumps({
                "error": "usage: jarvis tool-safety-set <name> <confirm_required|ai_review> <true|false>",
            }))
            sys.exit(1)

        tool_name, key, raw_value = argv[1], argv[2], argv[3]
        value = raw_value.strip().lower() in ("1", "true", "yes", "y", "on")
        try:
            flags = tool_safety.set_flag(tool_name, key, value)
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        print(json.dumps({"name": tool_name, **flags}, indent=2))
        return

    if argv[0] == "organize-json":
        # jarvis organize-json <path> [--raw] [--json]
        # Local file read + json.loads only — never touches ai_client, so
        # this costs zero API tokens no matter how big the file is.
        # Default: a human-readable tree (not JSON syntax). --raw: pretty
        # json.dumps. --json: machine-readable payload for the web server
        # (powers the "organize-json <path>" chat shortcut and its
        # Organized/Raw JSON toggle).
        from . import json_tools

        rest = argv[1:]
        want_raw = "--raw" in rest
        want_json = "--json" in rest
        positional = [a for a in rest if not a.startswith("--")]

        if not positional:
            usage = "usage: jarvis organize-json <path> [--raw] [--json]"
            if want_json:
                print(json.dumps({"ok": False, "error": usage}))
            else:
                print(f"{ERR.RED}{usage}{ERR.RESET}", file=sys.stderr)
            sys.exit(1)

        target = positional[0]
        data, text, error = json_tools.read_json_file(target)

        if error:
            if want_json:
                print(json.dumps({"ok": False, **error}))
            else:
                print(f"{ERR.RED}{error['error']}{ERR.RESET}", file=sys.stderr)
                if error.get("snippet"):
                    print(error["snippet"], file=sys.stderr)
            sys.exit(1)

        resolved = str(json_tools.resolve_path(target))
        if want_json:
            print(json.dumps({"ok": True, "path": resolved, "data": data, "text": text}))
            return
        if want_raw:
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return
        print(f"{OUT.DIM}{resolved}{OUT.RESET}")
        print(json_tools.render_tree(data))
        return

    if argv[0] not in commands and argv[0] not in RESERVED_NAMES:
        # '--provider NAME' / '--provider=NAME' can appear anywhere in the
        # free-text args (e.g. 'jarvis --provider anthropic what time is
        # it') and is stripped out before the rest is joined into the
        # actual message \u2014 see _extract_provider_override.
        rest, provider_override = _extract_provider_override(argv)
        sys.exit(handle_ai_prompt(" ".join(rest), commands, provider_override=provider_override))

    batches = split_chain_batches(argv)
    if not batches:
        print_help(commands)
        return

    parser = build_parser(commands)

    total = sum(len(b) for b in batches)
    if total == 1:
        sys.exit(resolve_and_run(commands, parser, batches[0][0]))

    # Chained mode: "jarvis cmd1 --flag x then cmd2 --flag y and cmd3 ..."
    done = 0
    for batch in batches:
        if len(batch) == 1:
            seg = batch[0]
            done += 1
            print(f"{ERR.BOLD}{ERR.CYAN}\u2192 [{done}/{total}] {seg[0]}{ERR.RESET}", file=sys.stderr)
        else:
            names = ", ".join(seg[0] for seg in batch)
            rng = f"{done + 1}\u2013{done + len(batch)}"
            print(f"{ERR.BOLD}{ERR.CYAN}\u2192 [{rng}/{total}] running in parallel: {names}{ERR.RESET}", file=sys.stderr)
            done += len(batch)

        results = _run_segment_batch(commands, parser, batch)
        failures = [(seg, code) for seg, code in results if code != 0]
        if failures:
            detail = ", ".join(f"'{seg[0]}' (exit {code})" for seg, code in failures)
            print(f"{ERR.RED}Chain stopped: {detail} failed.{ERR.RESET}", file=sys.stderr)
            sys.exit(failures[0][1])
    sys.exit(0)


def entry():
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{ERR.RED}Interrupted{ERR.RESET}", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    entry()