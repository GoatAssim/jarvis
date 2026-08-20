"""jarvis: a tiny, JSON-configurable command runner."""

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path

from . import conditions, stats
from .palette import Palette

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
RESERVED_NAMES = {"config", "ai-config", "ai-clear", "ai-drop-from", "playnite-config", "spotify-config", "spotify-login", "memory-config", CHAIN_SEP, PARALLEL_SEP, "-h", "--help"}

OUT = Palette(sys.stdout)  # actual command output: the banner, the command list
ERR = Palette(sys.stderr)  # jarvis's own status/trace/error messages


def banner(p):
    return f"""{p.CYAN}{p.BOLD}
  -------------------------------
   J A R V I S
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
    parser = argparse.ArgumentParser(prog="jarvis", add_help=False)
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
            cmd = step["run"].format(**values)
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


def resolve_and_run(commands, parser, seg):
    """Look up, parse, and run one 'then'/'and'-separated segment. Returns the exit code."""
    if seg[0] not in commands:
        print(f"{ERR.RED}Unknown command: {seg[0]}{ERR.RESET}\n", file=sys.stderr)
        print_help(commands, file=sys.stderr)
        return 1
    args = parser.parse_args(seg)
    return run_command(args.command, commands[args.command], args)


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


def handle_ai_prompt(text, commands):
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

    def on_attempt(label):
        print(f"{ERR.DIM}\u21b3 asking {label}\u2026{ERR.RESET}", file=sys.stderr, flush=True)

    def on_tool_call(name, arguments=None):
        labels = {
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
        elif name in ("wifi_set", "bluetooth_set", "radio_status", "git_run"):
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

    result = ai_client.ask(text, commands, on_attempt=on_attempt, on_tool_call=on_tool_call)

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

    print(f"{prefix}{result.text}")
    return 0


def main():
    commands = load_commands()
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        print_help(commands)
        return

    if argv[0] == "config":
        print(CONFIG_FILE)
        return

    if argv[0] == "ai-config":
        from . import ai_config
        ai_config.ensure_ai_config()
        print(ai_config.AI_CONFIG_FILE)
        return

    if argv[0] == "ai-clear":
        from . import history
        history.clear()
        print("Conversation history cleared \u2014 next ask starts with a clean slate.")
        return

    if argv[0] == "ai-drop-from":
        from . import history
        target = " ".join(argv[1:]).strip()
        history.drop_from_user(target)
        print("ok")
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

    if argv[0] not in commands and argv[0] not in RESERVED_NAMES:
        sys.exit(handle_ai_prompt(" ".join(argv), commands))

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
