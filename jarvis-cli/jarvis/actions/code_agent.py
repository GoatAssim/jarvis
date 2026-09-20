"""code_agent — read/explore/edit/run inside an EXISTING local codebase,
autonomously: look around first, form an understanding, make a small
targeted edit, verify it actually works, then report back. This is the
"you handed me a project" counterpart to dev_agent.py's "build a new
project from a description" — nothing here touches dev_agent.py's own
plan/write/install/run/fix pipeline.

Where dev_agent commits to one upfront AI call that must return a single
JSON blob (the whole project, files_content and all), code_agent instead
hands the model a small toolbox — read_file, list_dir, search_code,
edit_file, run_shell — and lets ai_providers' own tools+tool_executor loop
(see ai_providers.MAX_TOOL_ROUNDS) drive it round by round: the model sees
each tool's *real* result and decides its own next step, the way a person
actually working through an unfamiliar codebase does, instead of guessing
everything in one shot.

Same actions/ auto-discovery contract as every other file here (see
actions/_template.py), and the same module-level-import discipline
dev_agent.py documents at its own top (this file is loaded by
tool_loader.discover_actions() DURING tools.py's own partial
initialization) — ai_client/ai_config/ai_providers are only ever imported
lazily, inside function bodies, never at this file's module level.

TOOL_GROUP below is deliberately the SAME "dev_agent" group dev_agent.py
already uses, not a new one — per the actions template's own note, joining
an existing group means these tools are offered as siblings the moment
that group's keywords fire, so "build me a script that..." and "fix a bug
in my project" both surface this whole toolbox without duplicating
TOOL_KEYWORDS here for read_file/list_dir/search_code/edit_file/run_shell.
Only code_agent itself gets a couple of its own keyword phrases below,
since "work on my codebase" phrasing is distinct enough to be worth the
extra routing precision even inside a shared group.

Token efficiency note (the actual reason this file exists): jarvis's
existing file-attach path reads a whole file and pastes its full text into
the prompt — fine for a short note, expensive for a real source file.
read_file here never does that: it returns numbered lines (matching the
`view` tool's own convention), accepts an optional start_line/end_line
range, and reports total_lines so the model can ask for a narrower slice
instead of the whole thing. search_code is the same idea one level up —
grep for the relevant lines across the project instead of reading every
file end to end to find them.
"""

import fnmatch
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from .. import dev_agent_events, dev_agent_sandbox

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RUN_TIMEOUT = 30  # seconds; matches dev_agent.RUN_TIMEOUT's reasoning —
# a shell step the agent runs to verify its own edit shouldn't be able to
# hang the whole call.

MAX_ROUNDS_CEILING = 5  # ai_providers.MAX_TOOL_ROUNDS is 5 per adapter
# attempt regardless of what round_budget we hand it, so asking for more
# than this from any single provider attempt would be a no-op anyway.

_IGNORE_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".idea", ".vscode",
}
# Same spirit as the `view` tool's own "ignoring hidden items and
# node_modules" — list_dir/search_code both skip these and anything
# starting with "." so a walk over a real project doesn't drown in
# .git internals or a virtualenv.


# ---------------------------------------------------------------------------
# Path resolution — two deliberately different modes.
#
# _resolve_path (home-relative, no jail) is for the STANDALONE tools
# (read_file/list_dir/search_code/edit_file/run_shell called directly by
# the top-level model, one call at a time) — same convention
# file_tools._resolve_path already uses for write_file. A destructive one
# still only ever runs after tool_safety's confirm gate; a read-only one
# needs no jail at all, same as write_file needs none today.
#
# dev_agent_sandbox.resolve_within (jailed to one root) is for code_agent's
# OWN internal loop, which — like dev_agent's own write/run steps — makes
# many calls in a row with no per-call confirmation. That's exactly what
# dev_agent_sandbox.py's own docstring says it exists for: "fine for a
# single human-confirmed write, wrong for an autonomous loop ... without a
# per-file confirmation." resolve_within is already root-agnostic (any
# existing directory, not just a freshly created one), so code_agent reuses
# it directly rather than inventing a second sandbox module.
# ---------------------------------------------------------------------------


def _resolve_path(raw_path):
    p = Path(raw_path or ".").expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    return p


def _coerce_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Shared implementations — used both by the standalone tool_* handlers
# below and by tool_code_agent's own internal executor, so there's exactly
# one place that knows how to read/list/search/edit/run.
# ---------------------------------------------------------------------------


def _numbered(lines, start=1):
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start=start))


_DOTENV_NAME_RE = re.compile(r"^\.env(\..+)?$")
# Matches .env, .env.local, .env.production, etc. — not .environment or
# anything else that merely starts with ".env".

_DOTENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _mask_dotenv_text(text):
    """KEY=value -> KEY=•••• (N chars), so the inner agent can see which
    names exist (enough to write os.getenv("KEY")) without a raw secret
    value ever landing in a cloud provider's transcript (master plan F.5,
    decision D2). Lines that aren't KEY=value (comments, blank lines) pass
    through unchanged."""
    out = []
    for line in text.splitlines():
        m = _DOTENV_LINE_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            key, value = m.group(1), m.group(2)
            out.append(f"{key}=•••• ({len(value)} chars)" if value else f"{key}=")
        else:
            out.append(line)
    return "\n".join(out)


def _read_file_text(path, start_line=None, end_line=None, max_chars=12000, reveal_secrets=False):
    """Numbered-line read of a file, optionally restricted to a line range
    — never the whole file pasted blind. Returns (result_dict, None) or
    (None, error_string).

    A dotfile matching .env/.env.* is masked by default (key names only,
    values replaced with a length-only placeholder) — see
    _mask_dotenv_text. Pass reveal_secrets=True for the rare case a raw
    read is genuinely needed; that path is never used by the autonomous
    inner loop, only the explicit standalone tool call."""
    if not path.exists():
        return None, f"{path} does not exist"
    if path.is_dir():
        return None, f"{path} is a directory, not a file — use list_dir"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"couldn't read {path}: {e}"

    masked = False
    if _DOTENV_NAME_RE.match(path.name) and not reveal_secrets:
        text = _mask_dotenv_text(text)
        masked = True

    all_lines = text.splitlines()
    total = len(all_lines)
    lo = max(1, _coerce_int(start_line) or 1)
    hi = min(total, _coerce_int(end_line) or total) if total else 0
    if total and lo > hi:
        return None, f"start_line {lo} is after end_line {hi} (file has {total} lines)"
    selected = all_lines[lo - 1:hi] if total else []
    body = _numbered(selected, start=lo)

    truncated = False
    if len(body) > max_chars:
        half = max_chars // 2
        body = (body[:half] + "\n...(truncated — narrow start_line/end_line "
                              "and re-read for the rest)...\n" + body[-half:])
        truncated = True

    result = {
        "path": str(path),
        "total_lines": total,
        "start_line": lo,
        "end_line": hi,
        "content": body,
        "truncated": truncated,
    }
    if masked:
        result["values_masked"] = True
    return result, None


def _list_dir_impl(path, max_depth=2, max_entries=400):
    if not path.exists():
        return None, f"{path} does not exist"
    if not path.is_dir():
        return None, f"{path} is not a directory — use read_file"

    lines = []
    hidden = []  # top-level dotfiles by name (see master plan F.5) — not
    # walked into and not counted against max_entries; just named so the
    # agent knows they exist instead of the directory silently looking
    # like it doesn't contain them (list_dir on a folder with a ".env"
    # used to report only ["main.py"]).
    count = [0]

    def walk(d, depth, prefix):
        if depth > max_depth or count[0] >= max_entries:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            lines.append(f"{prefix}[error reading {d.name}: {e}]")
            return
        for entry in entries:
            if entry.name.startswith("."):
                # Still skip noisy/large dotdirs entirely (.git, .venv,
                # etc.) — only name plain top-level dotfiles.
                if depth == 1 and entry.is_file() and entry.name not in _IGNORE_NAMES:
                    hidden.append(entry.name)
                continue
            if entry.name in _IGNORE_NAMES:
                continue
            if count[0] >= max_entries:
                lines.append(f"{prefix}...(truncated at {max_entries} entries)")
                return
            marker = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{entry.name}{marker}")
            count[0] += 1
            if entry.is_dir():
                walk(entry, depth + 1, prefix + "  ")

    walk(path, 1, "")
    result = {"path": str(path), "entries": lines, "entry_count": count[0]}
    if hidden:
        result["hidden"] = sorted(hidden)
    return result, None


def _iter_source_files(root, glob_pattern, max_files=4000):
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _IGNORE_NAMES]
        for name in filenames:
            if name.startswith("."):
                # Dotfiles are skipped by default (same reasoning as
                # list_dir), but an explicit glob that itself targets
                # dotfiles (e.g. ".env*") can still reach them — the
                # default is "don't wade through .git", not "make .env
                # unfindable" (master plan F.5).
                if not glob_pattern or not fnmatch.fnmatch(name, glob_pattern):
                    continue
            elif glob_pattern and not fnmatch.fnmatch(name, glob_pattern):
                continue
            scanned += 1
            if scanned > max_files:
                return
            yield Path(dirpath) / name


def _search_code_impl(root, pattern, glob_pattern=None, is_regex=True, max_results=40, snippet_chars=160):
    if not pattern:
        return None, "pattern is required"
    try:
        rx = re.compile(pattern) if is_regex else re.compile(re.escape(pattern))
    except re.error as e:
        return None, f"invalid regex {pattern!r}: {e}"

    matches = []
    scanned = 0
    for file_path in _iter_source_files(root, glob_pattern):
        scanned += 1
        try:
            text = file_path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — this is a source search, skip silently
        for line_no, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                snippet = line.strip()
                if len(snippet) > snippet_chars:
                    snippet = snippet[:snippet_chars] + "…"
                try:
                    rel = file_path.relative_to(root)
                except ValueError:
                    rel = file_path
                matches.append({"path": str(rel), "line": line_no, "text": snippet})
                if len(matches) >= max_results:
                    return {"matches": matches, "files_scanned": scanned, "truncated": True}, None

    return {"matches": matches, "files_scanned": scanned, "truncated": False}, None


def _edit_file_impl(path, old_str, new_str):
    """str_replace-style edit: old_str must match the CURRENT file content
    exactly once. Token-efficient by design — the model never has to
    resend the whole file to change a few lines of it, and an ambiguous or
    stale old_str fails loudly instead of guessing which occurrence was
    meant."""
    if not path.exists():
        return None, f"{path} does not exist — use write_file to create a new file"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"couldn't read {path}: {e}"
    if not old_str:
        return None, "old_str is required and must be non-empty"
    count = text.count(old_str)
    if count == 0:
        return None, "old_str not found — it must match the file's CURRENT content exactly (re-read the file if unsure)"
    if count > 1:
        return None, f"old_str matches {count} places — add more surrounding context so it's unique"
    new_text = text.replace(old_str, new_str or "", 1)
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return None, f"couldn't write {path}: {e}"
    return {"path": str(path), "bytes_written": len(new_text.encode("utf-8")), "preview": (new_str or "")[:200]}, None


_CMD_BUILTINS = {
    # cmd.exe builtins have no standalone .exe, so subprocess.run can never
    # find them as a program (WinError 2) — they only work run through
    # cmd.exe itself (see master plan F.4). Not exhaustive, but covers
    # everything a model is likely to reach for.
    "dir", "type", "copy", "move", "del", "erase", "echo", "cd", "chdir",
    "md", "mkdir", "rd", "rmdir", "cls", "set", "ver", "vol", "path",
    "title", "pushd", "popd", "ren", "rename", "start", "assoc", "ftype",
    "if", "for", "call", "exit", "attrib", "more",
}


def _windows_argv_split(command):
    """Split a command line the way Windows programs themselves see their
    argv — via CommandLineToArgvW — instead of shlex(posix=False), which
    leaves quote characters IN the token. `shlex.split('python -c "import
    os; print(1)"', posix=False)` hands Python the two-character string
    `-c` plus a token that still has its surrounding quotes, so `python`
    parses it as a no-op string literal and exits 0 with no output (see
    master plan F.4 — a silent success is the worst failure a tool can
    return). CommandLineToArgvW strips the quotes and keeps the spaces
    inside them as one argument, matching what every real Windows program
    (including python.exe) actually receives.
    """
    import ctypes

    argc = ctypes.c_int(0)
    argv_p = ctypes.windll.shell32.CommandLineToArgvW(
        ctypes.c_wchar_p(command), ctypes.byref(argc)
    )
    try:
        return [argv_p[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv_p)


def _shlex_split(command):
    import shlex
    if sys.platform == "win32":
        try:
            return _windows_argv_split(command)
        except Exception:
            # WinAPI call itself failed for some reason — fall back to a
            # naive split rather than the old posix=False behavior, which
            # silently mis-splits quoted arguments (see docstring above).
            return command.split()
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _run_shell_impl(command, cwd, timeout=RUN_TIMEOUT):
    argv = _shlex_split(command or "")
    if not argv:
        return None, "command is required"

    if sys.platform == "win32" and argv[0].lower() in _CMD_BUILTINS:
        # Run the ORIGINAL command line through cmd /c rather than
        # re-joining argv, so quoting the model wrote (e.g. a quoted path
        # with spaces) survives exactly as given instead of being
        # re-escaped by us. This is intentionally narrower than blanket
        # shell=True (decision F.16/D3): only a small fixed list of known
        # builtins gets the cmd.exe treatment, nothing else gets `&`/`|`/
        # `>` shell-metacharacter behavior.
        argv = ["cmd", "/c", command]

    try:
        result = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "stdout_tail": "", "stderr_tail": f"timed out after {timeout}s"}, None
    except OSError as e:
        return {"exit_code": None, "stdout_tail": "", "stderr_tail": str(e)}, None

    def cap(raw, n=3000):
        s = (raw or b"").decode("utf-8", errors="replace").strip()
        return s if len(s) <= n else s[-n:] + "\n...(truncated, showing tail)"

    return {
        "exit_code": result.returncode,
        "stdout_tail": cap(result.stdout),
        "stderr_tail": cap(result.stderr, 2000),
    }, None


# ---------------------------------------------------------------------------
# Standalone tool handlers — home-relative, no jail (see the note above).
# Reachable directly by the top-level model, one call at a time, exactly
# like write_file already is.
# ---------------------------------------------------------------------------


def tool_read_file(arguments):
    arguments = arguments or {}
    raw_path = arguments.get("path")
    if not raw_path:
        return {"error": "path is required"}
    result, err = _read_file_text(
        _resolve_path(raw_path), arguments.get("start_line"), arguments.get("end_line"),
        reveal_secrets=bool(arguments.get("reveal_secrets")),
    )
    return result if err is None else {"error": err}


def tool_list_dir(arguments):
    arguments = arguments or {}
    result, err = _list_dir_impl(_resolve_path(arguments.get("path") or "."))
    return result if err is None else {"error": err}


def tool_search_code(arguments):
    arguments = arguments or {}
    path = _resolve_path(arguments.get("path") or ".")
    if not path.exists() or not path.is_dir():
        return {"error": f"{path} is not a directory"}
    result, err = _search_code_impl(
        path, (arguments.get("pattern") or "").strip(),
        glob_pattern=(arguments.get("glob") or "").strip() or None,
        is_regex=bool(arguments.get("is_regex", True)),
        max_results=min(_coerce_int(arguments.get("max_results")) or 40, 100),
    )
    return result if err is None else {"error": err}


def tool_edit_file(arguments):
    arguments = arguments or {}
    raw_path = arguments.get("path")
    if not raw_path:
        return {"error": "path is required"}
    if arguments.get("old_str") is None or arguments.get("new_str") is None:
        return {"error": "old_str and new_str are both required"}
    result, err = _edit_file_impl(_resolve_path(raw_path), arguments.get("old_str"), arguments.get("new_str"))
    return {"ok": True, **result} if err is None else {"error": err}


def tool_run_shell(arguments):
    arguments = arguments or {}
    command = (arguments.get("command") or "").strip()
    if not command:
        return {"error": "command is required"}
    cwd = _resolve_path(arguments.get("path") or ".")
    if not cwd.exists() or not cwd.is_dir():
        return {"error": f"{cwd} is not a directory"}
    result, err = _run_shell_impl(command, cwd)
    return result if err is None else {"error": err}


# ---------------------------------------------------------------------------
# code_agent — the orchestrator. One confirmation up front (see
# TOOL_CONFIRM_REQUIRED below), then it runs its own read/search/edit/
# verify loop with NO further per-step confirmation, same "confirm once,
# then the agent works freely inside its own sandbox" shape dev_agent
# already uses for install/run/fix.
# ---------------------------------------------------------------------------

_CODE_AGENT_SYSTEM_PROMPT = (
    "You are an autonomous coding agent working inside one existing, local "
    "project directory (your root — every path argument you use is relative "
    "to it, and you cannot leave it). Tools available: read_file, list_dir, "
    "search_code, edit_file, write_file, run_shell.\n\n"
    "Work the way a careful engineer would: look around and read enough of "
    "the relevant code to actually understand it BEFORE changing anything — "
    "don't guess a fix from the task description alone. Prefer search_code "
    "to locate where something lives over guessing file paths. Make the "
    "smallest edit that correctly does the job, using edit_file's exact "
    "old_str/new_str match (never rewrite a whole file just to change a few "
    "lines) — write_file is only for a genuinely new file. When the change "
    "can be run, tested, or syntax-checked, use run_shell to actually verify "
    "it before declaring success; don't assume an edit worked. "
    "list_dir also names top-level dotfiles like .env under \"hidden\" — "
    "check there before assuming a config file doesn't exist. read_file on "
    "a .env/.env.* file returns key NAMES only, values masked as "
    "'•••• (N chars)' — that's enough to write os.getenv(\"KEY\"); it is "
    "never a way to see the real value. When you're "
    "done (or genuinely stuck), reply with a short plain-text summary of "
    "what you found, what you changed and why, and how you verified it — "
    "no more tool calls after that."
)

# The AI-facing schema for "write_file" here is intentionally private —
# never added to this file's TOOL_SCHEMAS/TOOLS below, so it can share the
# name "write_file" with file_tools.py's own top-level tool without the
# name-collision check in tool_loader.discover_actions() ever seeing it:
# this dict is only ever handed directly to an adapter's `tools=` argument
# inside tool_code_agent's own internal loop, never registered as a
# reachable tool in its own right.
_INTERNAL_WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Create a NEW file (fails if used on an existing one that should be edited instead — use edit_file for that) within the code_agent root.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the code_agent root."},
            "content": {"type": "string", "description": "Full text content of the new file."},
        },
        "required": ["path", "content"],
    },
}


def _provider_label_safe(ai_client, provider):
    try:
        return ai_client._provider_label(provider)
    except Exception:
        return provider.get("type", "unknown-provider")


def _run_agent_loop(task, schemas, executor, round_limit):
    """Drives ai_providers' own tools+tool_executor loop directly (same
    adapter-call convention dev_agent._ai_single_call uses, just with real
    tools this time instead of tools=None): each adapter already loops
    internally — see ai_providers.MAX_TOOL_ROUNDS — calling
    executor(name, args) whenever the model wants a tool, until it settles
    on a final text answer or the round budget runs out. THIS is what
    makes code_agent an actual multi-step agent rather than dev_agent's
    one-shot JSON plan: the model sees each tool's real result and decides
    its own next step.

    Provider fallback only applies BEFORE any tool has actually run on a
    given attempt (round_budget.used stays 0). Once the executor has done
    real work — an edit written to disk, a shell command already run — a
    provider dying partway through is reported as-is, not silently retried
    on a different provider: replaying a half-finished multi-step job
    could re-apply the same edit twice or leave things in a confusing
    double-attempt state. A single stateless completion (dev_agent's
    planner call) can be safely retried elsewhere; a job with side effects
    already on disk cannot.
    """
    try:
        from .. import ai_client, ai_config, ai_providers

        cfg = ai_config.load_ai_config()
        providers = ai_client._eligible_providers(cfg["providers"], cfg["defaults"])
        if not providers:
            return None, "no configured AI provider available for code_agent"

        messages = [
            {"role": "system", "content": _CODE_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

        errors = []
        for provider in providers:
            adapter = ai_client.ai_providers.ADAPTERS.get(provider.get("type"))
            if adapter is None:
                errors.append(f"{_provider_label_safe(ai_client, provider)}: no adapter for provider type {provider.get('type')!r}")
                continue

            keys = ai_config.provider_keys(provider) or [None]
            for key in keys:
                resolved = ai_client._resolve(provider, cfg["defaults"])
                if key is not None:
                    resolved["api_key"] = key
                round_budget = ai_providers.RoundBudget(limit=round_limit)
                try:
                    result = adapter(resolved, messages, resolved.get("timeout", ai_client.DEFAULT_TIMEOUT),
                                      tools=schemas, tool_executor=executor, round_budget=round_budget)
                except Exception as e:
                    if round_budget.used > 0:
                        return None, (f"{_provider_label_safe(ai_client, provider)}: crashed mid-run after "
                                      f"{round_budget.used} tool call(s) already made: {e}")
                    errors.append(f"{_provider_label_safe(ai_client, provider)}: unexpected error: {e}")
                    continue

                if result.ok and result.text:
                    return result.text, None
                if round_budget.used > 0:
                    return None, (f"{_provider_label_safe(ai_client, provider)}: "
                                  f"{getattr(result, 'error', None) or 'no final answer after tool calls'}")
                errors.append(f"{_provider_label_safe(ai_client, provider)}: "
                               f"{getattr(result, 'error', None) or 'returned no text'}")

        return None, "all configured providers failed for code_agent: " + "; ".join(errors)
    except Exception as e:
        return None, str(e)


def tool_code_agent(arguments, context=None):
    """Autonomously read, search, edit, and run inside one EXISTING project
    directory to accomplish `task` — a bounded, self-directed loop, not a
    single file edit. NEVER raises — every failure branch returns a
    well-formed result with ok=False, same contract as tool_dev_agent.
    """
    arguments = arguments or {}
    task = (arguments.get("task") or "").strip()
    if not task:
        return {"error": "task is required"}

    raw_root = arguments.get("root")
    root = _resolve_path(raw_root) if raw_root else Path.cwd()
    if not root.exists() or not root.is_dir():
        return {"error": f"{root} does not exist or is not a directory — code_agent works on an "
                          f"EXISTING project; use dev_agent to build a new one from scratch"}
    root = root.resolve()

    job_id = uuid.uuid4().hex[:12]
    steps = []
    seq = [0]

    def emit(phase, status, **fields):
        if context is not None and getattr(context, "emit_event", None):
            e = context.emit_event(job_id, seq[0], phase, status, **fields)
        else:
            e = dev_agent_events.emit(job_id, seq[0], phase, status, **fields)
        seq[0] += 1
        steps.append(e)
        return e

    call_count = [0]

    def executor(name, tool_args):
        tool_args = tool_args or {}
        call_count[0] += 1
        emit("step", "start", tool=name, arguments=tool_args)
        try:
            if name == "read_file":
                path = dev_agent_sandbox.resolve_within(root, tool_args.get("path") or "")
                # reveal_secrets is deliberately NOT threaded through here:
                # this is the autonomous inner loop, whose read_file calls
                # go straight into a cloud provider's transcript — exactly
                # what masking .env values is meant to prevent (F.5,
                # decision D2). Only the standalone, human-invoked
                # tool_read_file honors reveal_secrets.
                result, err = _read_file_text(path, tool_args.get("start_line"), tool_args.get("end_line"))
            elif name == "list_dir":
                path = dev_agent_sandbox.resolve_within(root, tool_args.get("path") or ".")
                result, err = _list_dir_impl(path)
            elif name == "search_code":
                path = dev_agent_sandbox.resolve_within(root, tool_args.get("path") or ".")
                result, err = _search_code_impl(
                    path, (tool_args.get("pattern") or "").strip(),
                    glob_pattern=(tool_args.get("glob") or "").strip() or None,
                    is_regex=bool(tool_args.get("is_regex", True)),
                    max_results=min(_coerce_int(tool_args.get("max_results")) or 40, 100),
                )
            elif name == "edit_file":
                path = dev_agent_sandbox.resolve_within(root, tool_args.get("path") or "")
                result, err = _edit_file_impl(path, tool_args.get("old_str"), tool_args.get("new_str"))
            elif name == "write_file":
                path = dev_agent_sandbox.resolve_within(root, tool_args.get("path") or "")
                if path.exists():
                    result, err = None, f"{path} already exists — use edit_file to change it"
                else:
                    content = tool_args.get("content") if isinstance(tool_args.get("content"), str) else ""
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                    result, err = {"path": str(path), "bytes_written": len(content.encode("utf-8"))}, None
            elif name == "run_shell":
                result, err = _run_shell_impl(tool_args.get("command") or "", root)
            else:
                result, err = None, f"unknown tool {name!r}"
        except ValueError as e:  # sandbox rejection (path escapes root)
            result, err = None, str(e)
        except Exception as e:  # one bad step must never kill the whole job
            result, err = None, f"unexpected error: {e}"

        emit("step", "ok" if err is None else "fail", tool=name, **({"error": err} if err else {}))
        return result if err is None else {"error": err}

    ai_schemas = [
        s for s in TOOL_SCHEMAS
        if s["name"] in ("read_file", "list_dir", "search_code", "edit_file", "run_shell")
    ] + [_INTERNAL_WRITE_FILE_SCHEMA]

    if context is not None and getattr(context, "round_budget_remaining", None):
        try:
            remaining = context.round_budget_remaining()
        except Exception:
            remaining = MAX_ROUNDS_CEILING
    else:
        remaining = MAX_ROUNDS_CEILING
    round_limit = min(MAX_ROUNDS_CEILING, max(1, remaining))
    # Same reasoning as dev_agent's own max_attempts: code_agent consumes
    # exactly ONE unit of the OUTER round budget (one tool call from the
    # model's point of view) — this is the budget for the INNER loop of
    # read/search/edit/run steps, capped so a giveup here still leaves the
    # outer conversation room to read the result and answer afterward.

    emit("plan", "start", task=task[:300], root=str(root))
    text, err = _run_agent_loop(task, ai_schemas, executor, round_limit)
    if err:
        emit("done", "fail", error=err, tool_calls=call_count[0])
        return {
            "ok": False, "job_id": job_id, "root": str(root), "steps": steps,
            "tool_calls": call_count[0], "reason": "agent_failed", "last_error": err,
        }

    emit("done", "ok", tool_calls=call_count[0])
    return {
        "ok": True, "job_id": job_id, "root": str(root), "steps": steps,
        "tool_calls": call_count[0], "summary": text,
    }


# ---------------------------------------------------------------------------
# TOOL_SCHEMAS / TOOLS — required (see actions/_template.py §1-2).
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": (
            "Read a text file, returned as numbered lines (never the raw whole-file "
            "paste jarvis's file-attach path does — token-heavy for real source files). "
            "Pass start_line/end_line to read just a range; the result reports "
            "total_lines so you can request another range instead of guessing. Use "
            "search_code first if you don't already know which file/lines matter. "
            "A .env/.env.* file is returned with values masked (key names only) "
            "unless reveal_secrets is set."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path. Relative paths resolve against the user's home directory."},
                "start_line": {"type": "integer", "description": "First line to include (1-indexed). Omit to start at line 1."},
                "end_line": {"type": "integer", "description": "Last line to include (1-indexed). Omit to read to the end."},
                "reveal_secrets": {"type": "boolean", "description": "Return real values for a .env/.env.* file instead of masking them. Default false. Only honored for a direct, explicit call — never used by code_agent's own autonomous loop."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory's contents (up to 2 levels deep), skipping .git/node_modules/venv and similar. Top-level dotfiles (e.g. .env) aren't listed among entries but are named separately under \"hidden\" so you know they exist. Use to orient yourself in an unfamiliar project before reading specific files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path. Relative paths resolve against the user's home directory. Omit for the current/home directory."},
            },
            "required": [],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Grep-style search across every text file under a directory for a pattern, "
            "returning file/line/snippet per match. Use this to FIND where something "
            "lives before reading files end to end — much cheaper than read_file-ing "
            "an unfamiliar project one file at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regex to search for (regex by default; set is_regex false for a literal substring)."},
                "path": {"type": "string", "description": "Directory to search under. Relative paths resolve against the user's home directory. Omit for the current/home directory."},
                "glob": {"type": "string", "description": "Optional filename filter, e.g. '*.py'."},
                "is_regex": {"type": "boolean", "description": "Whether pattern is a regex (default true) or a literal string."},
                "max_results": {"type": "integer", "description": "Cap on matches returned (default 40, max 100)."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Make a targeted edit to an EXISTING file: old_str must match the file's "
            "current content exactly once (add surrounding context to disambiguate) and "
            "is replaced with new_str. Always read_file first if you haven't already seen "
            "the current content — never guess at old_str. Far cheaper than rewriting a "
            "whole file for a small change. Always confirmed with the user first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path. Relative paths resolve against the user's home directory."},
                "old_str": {"type": "string", "description": "Exact text to replace — must appear exactly once in the file."},
                "new_str": {"type": "string", "description": "Replacement text (empty string to delete old_str)."},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Run one shell command in a given directory (e.g. run a test suite, a linter, "
            "or the project itself) and return its exit code/stdout/stderr. Always "
            "confirmed with the user first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run, e.g. 'pytest -q' or 'python app.py'."},
                "path": {"type": "string", "description": "Working directory to run it in. Relative paths resolve against the user's home directory. Omit for the current/home directory."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "code_agent",
        "description": (
            "Autonomously read, search, edit, and run inside one EXISTING project directory "
            "to accomplish a task — a bounded, self-directed explore-then-edit-then-verify "
            "loop, the way a human engineer (or an agentic coding assistant sitting in a "
            "terminal) works through an unfamiliar codebase, rather than a single blind edit. "
            "Prefer this over calling read_file/search_code/edit_file yourself one at a time "
            "when the task needs real investigation first (\"fix this bug\", \"add this small "
            "feature\", \"find out why X is broken\") in a project that already exists — use "
            "dev_agent instead for building something new from nothing. Requires one user "
            "confirmation up front; after that it runs its own read/edit/verify loop without "
            "asking again per step. Streams progress live; the final result includes the full "
            "step-by-step timeline and a plain-text summary of what was done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Plain-language description of what to investigate/fix/change."},
                "root": {"type": "string", "description": "The existing project directory to work inside. Relative paths resolve against the user's home directory. Omit for the current directory."},
            },
            "required": ["task"],
        },
    },
]

TOOLS = {
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "search_code": tool_search_code,
    "edit_file": tool_edit_file,
    "run_shell": tool_run_shell,
    "code_agent": tool_code_agent,
}

# TOOL_GROUP — joins dev_agent.py's EXISTING group on purpose (see this
# file's module docstring): these tools become siblings of dev_agent
# whenever tool_router.route() decides that group is relevant, so "dev"
# keywords surface this whole toolbox too, without duplicating
# dev_agent.py's TOOL_KEYWORDS here.
TOOL_GROUP = "dev_agent"

# TOOL_KEYWORDS — optional when joining an existing group (see
# actions/_template.py §3), but code_agent's own "work on an existing
# project" phrasing is distinct enough from dev_agent's "build one from
# scratch" phrasing to be worth its own routing signal. Weights follow the
# same >= tool_router.MIN_SCORE (5) convention as dev_agent.py's own table.
TOOL_KEYWORDS = {
    "code_agent": {
        "fix a bug in": 8,
        "fix the bug in": 8,
        "work on my project": 8,
        "work on my codebase": 8,
        "in my codebase": 7,
        "in my existing project": 7,
        "refactor": 6,
        "debug my": 6,
    },
}

# TOOL_PACK_INSTRUCTION — a no-op here per the template (only takes effect
# for a brand-new TOOL_GROUP; this one joins dev_agent's existing group,
# whose instruction dev_agent.py already sets), left as documentation only.
TOOL_PACK_INSTRUCTION = ""

# TOOL_CONFIRM_REQUIRED — mirrors dev_agent.py's own belt-and-suspenders
# note: anything that mutates a file or executes something real gets
# confirm_required=True, matching write_file/run_command/package_install's
# own defaults. read_file/list_dir/search_code are read-only and need no
# gate, same as the `view`-style tools they're modeled on.
TOOL_CONFIRM_REQUIRED = {"edit_file", "run_shell", "code_agent"}

# TOOL_AI_REVIEW — arbitrary shell execution (direct or via code_agent's
# own internal loop) is exactly the kind of fuzzy/risky call a second AI's
# plain-language risk note helps a confirmation prompt convey, so both get
# it; edit_file's blast radius is one file and is self-explanatory from its
# own old_str/new_str diff, so it's left off.
TOOL_AI_REVIEW = {"run_shell", "code_agent"}

# TOOL_RESULT_SPECS — trims the two fields most likely to balloon: a big
# search_code hit list's per-match text, and code_agent's own step
# timeline (which echoes every tool call's raw arguments — including a
# large edit_file diff — at "full" verbosity).
TOOL_RESULT_SPECS = {
    "read_file": {
        "truncate_fields": {"content": {"medium": 6000, "low": 3000}},
    },
    "search_code": {
        "list_item_truncate": {"matches": {"text": {"medium": 160, "low": 80}}},
    },
    "code_agent": {
        "list_item_drop": {"steps": {"low": ["arguments"]}},
    },
}
