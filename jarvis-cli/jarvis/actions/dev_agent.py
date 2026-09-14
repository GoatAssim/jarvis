"""dev_agent \u2014 plan/write/install/run/self-fix a small project from a
plain-language description, in one tool call.

See docs/section-3.6-dev-agent-implementation-plan.md for the full design.
This file currently implements only \u00a74.1 (the module-level auto-discovery
contract) and \u00a74.2 (the schema). The actual orchestration loop
(\u00a74.3 sandbox, \u00a74.4 the plan\u2192write\u2192install\u2192run\u2192fix loop, \u00a74.5 result
shape, \u00a74.6 planner/writer AI calls) has NOT been built yet \u2014 tool_dev_agent
below is a placeholder that returns a clear "not implemented" error so this
file is honest about its own state, discoverable end-to-end (schema routes,
confirmation gate fires), and safe to drop in before the rest of \u00a74 lands.

Do not wire real file-writing/subprocess logic into tool_dev_agent until
dev_agent_sandbox.py (\u00a74.3) exists \u2014 that module is what makes an
autonomous multi-file write loop safe under a single up-front confirmation
instead of file_tools.write_file's per-call confirm.
"""

# ---------------------------------------------------------------------------
# 4.1 Module-level contract (per tool_loader.py's convention)
# ---------------------------------------------------------------------------


def _new_job_id():
    return uuid.uuid4().hex[:12]


def _run_subprocess(argv, cwd, timeout):
    """Run argv, capped/decoded the same way git_tools.tool_git_run and
    pkg_tools._run already do (capture_output, decode with errors="replace",
    CREATE_NO_WINDOW on Windows). Returns (ok, {"exit_code", "stdout_tail",
    "stderr_tail"}) -- never raises; timeout and OSError both come back as
    a non-ok result with an explanatory stderr_tail instead of propagating,
    so a single bad step never takes the whole dev_agent call down.
    """
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, {
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": f"timed out after {timeout}s",
        }
    except OSError as e:
        return False, {"exit_code": None, "stdout_tail": "", "stderr_tail": str(e)}

    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")

    def cap(s, n=4000):
        s = (s or "").strip()
        return s if len(s) <= n else s[-n:] + "\n...(truncated, showing tail)"

    return result.returncode == 0, {
        "exit_code": result.returncode,
        "stdout_tail": cap(stdout),
        "stderr_tail": cap(stderr, 2500),
    }


def _write_text_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else "", encoding="utf-8")
    return path.stat().st_size


# ---------------------------------------------------------------------------
# 4.6 Planner/writer AI calls -- single non-tool-calling completions, same
# adapter-call convention as ai_client.risk_review (see that function for
# the pattern this mirrors: _resolve() a provider, call the adapter
# directly with tools=None/tool_executor=None, never a nested ask()).
# ---------------------------------------------------------------------------

_PLAN_SYSTEM_PROMPT = (
    "You are the planning/writing step of an autonomous coding agent. Given a "
    "plain-language project description, respond with ONLY a single JSON object "
    "(no markdown fences, no commentary before or after) of this exact shape:\n"
    "{\n"
    '  "files": ["relative/path/one.py", "relative/path/two.py"],\n'
    '  "dependencies": ["package-name", ...],\n'
    '  "run_command": "python main.py",\n'
    '  "files_content": {"relative/path/one.py": "full file text", ...}\n'
    "}\n"
    "Rules: keep the project small and self-contained (prefer a single file "
    "unless the description clearly needs more). dependencies are package "
    "names only, installable via pip (Python) or npm (Node) -- never stdlib/"
    "builtin modules. run_command must work with cwd already set to the "
    "project directory (no cd, no absolute paths). files_content must have "
    "exactly one entry per path listed in files, with the complete file "
    "contents (no placeholders, no '...'). Prefer Python unless the "
    "description or language_hint clearly calls for Node/JavaScript."
)


def _extract_json(text):
    """Defensive parse matching ai_client's own convention elsewhere: strip
    a ```json ... ``` fence if present, then json.loads. Returns (obj, None)
    or (None, error_string) -- a parse failure is always a plan/fix failure,
    never a partially-trusted guess at what the model meant."""
    import json as _json

    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"```\s*$", "", s)
        s = s.strip()
    try:
        return _json.loads(s), None
    except ValueError as e:
        return None, f"could not parse model output as JSON: {e}"


def _ai_single_call(system_prompt, user_prompt, cfg=None):
    """One raw completion via the first eligible configured provider,
    structurally identical to ai_client.risk_review's adapter-call shape.
    Returns (text, None) or (None, error_string). Best-effort: any
    configuration/provider failure comes back as an error string, never
    an exception -- the caller turns that into a plan_failed/fix "fail"
    event, same as every other failure branch in the loop.
    """
    try:
        cfg = cfg or ai_config.load_config()
        providers = ai_client._eligible_providers(cfg["providers"], cfg["defaults"])
        if not providers:
            return None, "no configured AI provider available for dev_agent's planner/writer call"
        provider = providers[0]
        adapter = ai_client.ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            return None, f"no adapter for provider type {provider.get('type')!r}"
        keys = ai_config.provider_keys(provider) or [None]
        resolved = ai_client._resolve(provider, cfg["defaults"])
        if keys[0] is not None:
            resolved["api_key"] = keys[0]
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        result = adapter(resolved, messages, resolved.get("timeout", ai_client.DEFAULT_TIMEOUT),
                          tools=None, tool_executor=None)
        if result.ok and result.text:
            return result.text, None
        return None, (getattr(result, "error", None) or "planner/writer call returned no text")
    except Exception as e:
        return None, str(e)


def _plan_project(description, language_hint=None):
    """One model call -> {"files": [...], "dependencies": [...],
    "run_command": "...", "files_content": {...}}. See _PLAN_SYSTEM_PROMPT.
    Returns (plan_dict, None) or (None, error_string).
    """
    user_prompt = f"Project description: {description}"
    if language_hint:
        user_prompt += f"\nLanguage hint: {language_hint}"
    text, err = _ai_single_call(_PLAN_SYSTEM_PROMPT, user_prompt)
    if err:
        return None, err
    plan, err = _extract_json(text)
    if err:
        return None, err
    if not isinstance(plan, dict):
        return None, "planner response was valid JSON but not an object"
    files = plan.get("files")
    files_content = plan.get("files_content")
    run_command = (plan.get("run_command") or "").strip()
    if not isinstance(files, list) or not files:
        return None, "planner response is missing a non-empty 'files' list"
    if not isinstance(files_content, dict):
        return None, "planner response is missing 'files_content'"
    if not run_command:
        return None, "planner response is missing 'run_command'"
    missing = [f for f in files if f not in files_content]
    if missing:
        return None, f"planner listed files with no content: {missing}"
    dependencies = plan.get("dependencies")
    if not isinstance(dependencies, list):
        dependencies = []
    return {
        "files": files,
        "dependencies": [d for d in dependencies if isinstance(d, str) and d.strip()],
        "run_command": run_command,
        "files_content": {k: (v if isinstance(v, str) else "") for k, v in files_content.items()},
    }, None


def _fix_one_file(target_file, current_content, stderr_tail, description):
    """One model call asking for a corrected version of a single file.
    Returns (new_content, None) or (None, error_string)."""
    system_prompt = (
        "You are the self-fix step of an autonomous coding agent. You will be "
        "given one source file that failed to run, plus the error output. "
        "Respond with ONLY the complete corrected file contents -- no markdown "
        "fences, no explanation, no commentary. Keep the fix minimal and "
        "consistent with the rest of the file's existing style."
    )
    user_prompt = (
        f"Project description: {description}\n\n"
        f"File: {target_file}\n\n"
        f"Current content:\n{current_content}\n\n"
        f"Error output when run:\n{stderr_tail}\n\n"
        "Return the complete corrected file content, nothing else."
    )
    text, err = _ai_single_call(system_prompt, user_prompt)
    if err:
        return None, err
    fixed = (text or "").strip()
    if fixed.startswith("```"):
        fixed = re.sub(r"^```[a-zA-Z]*\n?", "", fixed)
        fixed = re.sub(r"```\s*$", "", fixed)
        fixed = fixed.strip()
    if not fixed:
        return None, "writer model returned an empty fix"
    return fixed, None


# ---------------------------------------------------------------------------
# 4.4 The loop -- _plan_project -> _write_files -> _install_dependencies ->
# _run_project -> _fix_files
# ---------------------------------------------------------------------------


def _write_files(project_dir, files_content, emit):
    """Write every planned file through dev_agent_sandbox.resolve_within,
    emitting a write start/ok/fail event per file. A rejected/failed path
    is skipped (not fatal to the whole job) -- same fault-tolerance
    philosophy tool_loader.discover_actions already uses for a broken
    action file (plan §4.3)."""
    written = []
    for rel_path, text in files_content.items():
        emit("write", "start", path=rel_path)
        try:
            resolved = dev_agent_sandbox.resolve_within(project_dir, rel_path)
        except ValueError as e:
            emit("write", "fail", path=rel_path, error=str(e))
            continue
        try:
            size = _write_text_file(resolved, text)
        except OSError as e:
            emit("write", "fail", path=rel_path, error=str(e))
            continue
        emit("write", "ok", path=rel_path, bytes=size, preview=(text or "")[:200])
        written.append(rel_path)
    return written


def _detect_language(project_dir, plan):
    """Cheap heuristic: presence of package.json means Node, anything else
    (or a requirements.txt / .py file) defaults to Python -- matches
    _plan_project's own "prefer Python unless clearly Node" instruction,
    so the two stay consistent."""
    if (project_dir / "package.json").exists():
        return "node"
    for rel_path in plan.get("files", []):
        if rel_path.endswith(".js") or rel_path.endswith(".ts"):
            return "node"
    return "python"


def _install_dependencies(project_dir, dependencies, plan):
    """Installs are scoped to project_dir so a generated project can never
    pollute or depend on jarvis's own interpreter environment (plan §5):
    Python gets its own venv inside project_dir, Node gets its own
    node_modules (npm's default, no extra work needed). Returns
    (ok, out_dict) where out_dict has exit_code/stdout_tail/stderr_tail,
    same shape _run_subprocess returns, so callers can **out_dict it
    straight into an emit() call."""
    language = _detect_language(project_dir, plan)

    if language == "node":
        if not dependencies:
            return True, {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
        argv = ["npm", "install", *dependencies]
        return _run_subprocess(argv, project_dir, INSTALL_TIMEOUT)

    # python: create a venv inside the sandboxed project dir, then pip
    # install into *that* interpreter -- never sys.executable/jarvis's own.
    venv_dir = project_dir / ".venv"
    venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not venv_python.exists():
        ok, out = _run_subprocess([sys.executable, "-m", "venv", str(venv_dir)], project_dir, INSTALL_TIMEOUT)
        if not ok:
            return False, out
    if not dependencies:
        return True, {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}
    argv = [str(venv_python), "-m", "pip", "install", *dependencies]
    return _run_subprocess(argv, project_dir, INSTALL_TIMEOUT)


def _run_project(project_dir, run_command, plan=None):
    """Launches run_command with cwd=project_dir and a hard RUN_TIMEOUT
    (plan §5). If this is a Python project with its own venv, rewrite a
    bare 'python'/'python3' leading token to the venv's interpreter so the
    run actually sees the packages _install_dependencies just installed."""
    argv = _shlex_split(run_command)
    if not argv:
        return False, {"exit_code": None, "stdout_tail": "", "stderr_tail": "empty run_command"}
    if plan is not None and _detect_language(project_dir, plan) == "python" and argv[0] in ("python", "python3"):
        venv_python = project_dir / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if venv_python.exists():
            argv[0] = str(venv_python)
    return _run_subprocess(argv, project_dir, RUN_TIMEOUT)


def _shlex_split(command):
    import shlex
    try:
        return shlex.split(command, posix=(sys.platform != "win32"))
    except ValueError:
        return command.split()


def _fix_files(project_dir, target_file, stderr_tail, plan, description):
    """One fix attempt. Branches on dev_agent_errors.classify's label:
    missing_dependency -> add to plan["dependencies"] (no file edit, no
    writer call -- the caller reinstalls and re-runs); everything else ->
    ask the writer model for a corrected version of target_file (falling
    back to the whole plan's file list if no target_file was identified)
    and rewrite it through the sandbox. Returns (fixed_bool, note_str).
    """
    classified, guessed_target = dev_agent_errors.classify(stderr_tail)

    if classified == "missing_dependency":
        name = dev_agent_errors.missing_dependency_name(stderr_tail)
        if not name:
            return False, "missing_dependency classified but no package name could be extracted"
        if name not in plan["dependencies"]:
            plan["dependencies"].append(name)
        return True, f"added missing dependency {name!r}; will reinstall and re-run"

    target = target_file or guessed_target
    if not target or target not in plan["files_content"]:
        # No specific file identified (or it's not one we wrote) -- fall
        # back to the first planned file, still better than giving up
        # outright on an unrecognized error shape (plan §5, last bullet).
        target = plan["files"][0] if plan.get("files") else None
    if not target:
        return False, "no target file to fix and no planned files to fall back to"

    current = plan["files_content"].get(target, "")
    fixed_content, err = _fix_one_file(target, current, stderr_tail, description)
    if err:
        return False, f"writer model failed: {err}"

    try:
        resolved = dev_agent_sandbox.resolve_within(project_dir, target)
        _write_text_file(resolved, fixed_content)
    except (ValueError, OSError) as e:
        return False, f"could not write fix for {target}: {e}"

    plan["files_content"][target] = fixed_content
    return True, f"rewrote {target} ({classified})"


def _final_result(job_id, steps, ok, project_dir, **extra):
    return {
        "ok": ok,
        "job_id": job_id,
        "project_dir": project_dir,
        "steps": steps,
        **extra,
    }


def tool_dev_agent(arguments, context=None):
    """Plan, write, install, run, and self-fix a small project from a
    plain-language description -- one tool call, live progress events,
    a full step timeline in the result. See §4.4 of the implementation
    plan for the design this mirrors almost line-for-line.

    NEVER raises -- every failure branch below returns a well-formed
    _final_result with ok=False and a reason, same contract as every
    other tool handler (actions/_template.py §1).
    """
    arguments = arguments or {}
    description = (arguments.get("description") or "").strip()
    if not description:
        return {"error": "description is required"}
    language_hint = (arguments.get("language_hint") or "").strip() or None

    job_id = _new_job_id()
    steps = []
    seq = [0]  # mutable cell so the emit closure below can increment it

    def emit(phase, status, **fields):
        if context is not None and getattr(context, "emit_event", None):
            e = context.emit_event(job_id, seq[0], phase, status, **fields)
        else:
            # No context (e.g. called directly / from a test) -- still
            # produce a well-formed event via the same emit() the context
            # would have wrapped, just without the CLI-hook/stderr side
            # effect a real context provides.
            e = dev_agent_events.emit(job_id, seq[0], phase, status, **fields)
        seq[0] += 1
        steps.append(e)
        return e

    try:
        project_dir = dev_agent_sandbox.new_project_dir(job_id, arguments.get("project_name"))
    except OSError as e:
        emit("plan", "fail", error=f"could not create project directory: {e}")
        return _final_result(job_id, steps, ok=False, project_dir=None, reason="sandbox_failed", last_error=str(e))

    emit("plan", "start", description=description[:300])
    plan, err = _plan_project(description, language_hint)
    if err:
        emit("plan", "fail", error=err)
        return _final_result(job_id, steps, ok=False, project_dir=str(project_dir), reason="plan_failed", last_error=err)
    emit("plan", "ok", files=plan["files"], dependencies=plan["dependencies"], run_command=plan["run_command"])

    _write_files(project_dir, plan["files_content"], emit)

    if plan["dependencies"]:
        emit("install", "start", dependencies=plan["dependencies"])
        install_ok, install_out = _install_dependencies(project_dir, plan["dependencies"], plan)
        emit("install", "ok" if install_ok else "fail", dependencies=plan["dependencies"], **install_out)
        if not install_ok:
            return _final_result(job_id, steps, ok=False, project_dir=str(project_dir),
                                  reason="install_failed", last_error=install_out.get("stderr_tail"))

    if context is not None and getattr(context, "round_budget_remaining", None):
        try:
            remaining = context.round_budget_remaining()
        except Exception:
            remaining = MAX_FIX_ATTEMPTS_CEILING
    else:
        remaining = MAX_FIX_ATTEMPTS_CEILING
    max_attempts = min(MAX_FIX_ATTEMPTS_CEILING, max(1, remaining))
    # dev_agent itself only ever consumes exactly ONE unit of the outer
    # RoundBudget (it's one tool call from the model's point of view) --
    # this local loop is internal to that single call, capped so a giveup
    # here still leaves enough shared budget for the model to read the
    # final result and answer in prose afterward (plan §4.4).

    attempt = 0
    emit("run", "start", command=plan["run_command"])
    run_ok, run_out = _run_project(project_dir, plan["run_command"], plan)
    emit("run", "ok" if run_ok else "fail", command=plan["run_command"], **run_out)

    while not run_ok and attempt < max_attempts:
        attempt += 1
        classified, target_file = dev_agent_errors.classify(run_out.get("stderr_tail", ""))
        emit("fix", "start", attempt=attempt, max_attempts=max_attempts,
             classified_error=classified, target_file=target_file)
        fixed, fix_note = _fix_files(project_dir, target_file, run_out.get("stderr_tail", ""), plan, description)
        emit("fix", "ok" if fixed else "fail", attempt=attempt, max_attempts=max_attempts,
             classified_error=classified, target_file=target_file, note=fix_note)
        if not fixed:
            break
        if classified == "missing_dependency":
            emit("install", "start", dependencies=plan["dependencies"])
            install_ok, install_out = _install_dependencies(project_dir, plan["dependencies"], plan)
            emit("install", "ok" if install_ok else "fail", dependencies=plan["dependencies"], **install_out)
            if not install_ok:
                return _final_result(job_id, steps, ok=False, project_dir=str(project_dir),
                                      reason="install_failed", last_error=install_out.get("stderr_tail"))
        emit("run", "start", command=plan["run_command"])
        run_ok, run_out = _run_project(project_dir, plan["run_command"], plan)
        emit("run", "ok" if run_ok else "fail", command=plan["run_command"], **run_out)

    if run_ok:
        emit("done", "ok", project_dir=str(project_dir), run_command=plan["run_command"], total_attempts=attempt)
        return _final_result(job_id, steps, ok=True, project_dir=str(project_dir),
                              run_command=plan["run_command"], total_attempts=attempt)

    reason = "budget_exhausted" if attempt >= max_attempts and max_attempts < MAX_FIX_ATTEMPTS_CEILING else "max_attempts"
    emit("done", "fail", project_dir=str(project_dir), reason=reason, last_error=run_out.get("stderr_tail"))
    return _final_result(job_id, steps, ok=False, project_dir=str(project_dir),
                          reason=reason, last_error=run_out.get("stderr_tail"))


# TOOL_SCHEMAS \u2014 required. See \u00a74.2 of the implementation plan.
TOOL_SCHEMAS = [
    {
        "name": "dev_agent",
        "description": (
            "Plan, write, install dependencies for, run, and self-fix a small project from a "
            "plain-language description \u2014 a bounded, self-correcting build loop, not a single "
            "file edit. Use this instead of write_file/run_command/package_install by hand when "
            "the user wants something runnable built from nothing (a script, a small web app, a "
            "CLI tool). Requires user confirmation before anything is written or run. Streams "
            "progress live; the final result includes the full step-by-step timeline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Plain-language description of what to build.",
                },
                "project_name": {
                    "type": "string",
                    "description": (
                        "Optional short slug for the project folder (letters/digits/hyphens "
                        "only). If omitted, one is derived from the description."
                    ),
                },
                "language_hint": {
                    "type": "string",
                    "description": (
                        "Optional hint, e.g. 'python', 'node' \u2014 the planner AI call decides "
                        "otherwise."
                    ),
                },
            },
            "required": ["description"],
        },
    },
]

# TOOLS \u2014 required. name -> handler, one entry per TOOL_SCHEMAS name.
TOOLS = {
    "dev_agent": tool_dev_agent,
}

# TOOL_GROUP \u2014 required. Brand-new group (nothing existing fits "plans and
# runs a whole project"), so TOOL_KEYWORDS below is not optional \u2014 see
# tool_loader.py's "Why TOOL_KEYWORDS is not really optional" docstring
# section: a new group with no keyword coverage is only ever reachable via
# search_tools's low-confidence fallback.
TOOL_GROUP = "dev_agent"

# TOOL_KEYWORDS \u2014 weights follow the real convention already used across
# tool_registry.TOOL_KEYWORDS (see e.g. take_screenshot: 10, web_search's
# "search the web": 10, run_command's "run": 4) where a phrase only counts
# as real signal at tool_router.MIN_SCORE (5) or above. NOTE: the
# implementation-plan doc's own §4.1 draft used weights of 1\u20133, which
# would never clear MIN_SCORE and would make this group permanently
# unroutable except through search_tools \u2014 that looks like exactly the
# "forgot to add real keywords" case tool_loader.py's discovery warning
# exists to catch, so the weights below are corrected to be >= 5 while
# keeping the same phrases the plan called for.
TOOL_KEYWORDS = {
    "dev_agent": {
        "build me a": 9,
        "build an app": 9,
        "make me an app": 9,
        "scaffold a project": 9,
        "create a small app": 8,
        "write a script that": 6,
        "code me": 7,
        "make a": 7,
        "change": 7,
        "program":6,
        "project":7,
    },
}

# TOOL_PACK_INSTRUCTION \u2014 one short line of workflow guidance for a
# brand-new group (see actions/_template.py \u00a73).
TOOL_PACK_INSTRUCTION = (
    "dev_agent plans, writes, installs, runs, and self-fixes a whole small project from a "
    "plain-language description, in one call. Prefer it over write_file+run_command by hand "
    "whenever the user wants a runnable project built from scratch, not a single file edited."
)

# TOOL_CONFIRM_REQUIRED \u2014 belt-and-suspenders: dev_agent writes files and
# runs a process, so it defaults to confirm_required=True the same way
# write_file/run_command/package_install do. This is also added to
# tool_safety.DEFAULT_CONFIRM_REQUIRED directly (\u00a78 of the plan, not yet
# done in this pass) so the gate holds even for someone who only reads
# tool_safety.py and never opens this file. Deliberately NOT a model-fillable
# "confirm" parameter on the schema above \u2014 confirmation is enforced by
# ai_client._make_tool_executor calling tool_safety.requires_confirmation(name)
# before the tool runs at all, the same real out-of-band gate every other
# confirm-gated tool uses (see \u00a74.2's note and \u00a72.3 of the plan).
TOOL_CONFIRM_REQUIRED = {"dev_agent"}

# TOOL_AI_REVIEW \u2014 left empty for the first cut, same as the plan; nothing
# here rules out turning this on later once real usage shows it's worth a
# second AI's risk opinion before the confirmation prompt.
TOOL_AI_REVIEW = set()

# TOOL_RESULT_SPECS \u2014 deferred to \u00a77 of the plan, once the real result
# shape (\u00a74.5's steps=[...] timeline) exists to shape. Left empty here
# rather than guessed at, since a wrong shape spec is worse than none (it
# would silently truncate fields nothing has decided are safe to drop yet).
TOOL_RESULT_SPECS = {}
