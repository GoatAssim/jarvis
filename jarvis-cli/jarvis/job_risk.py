"""What a scheduled job can do that is worth telling the person who approves it.

WHY THIS EXISTS (L.16, owner's 2026-10-01 follow-up)
----------------------------------------------------
A scheduled job that runs something is parked at status=needs_approval until a
human approves it (scheduler._needs_approval). Until now the approval surface
showed the job's prompt and nothing else, so approving was a leap of faith:
"check the screen, then shut down" reads fine and says nothing about the fact
that the job may click and type into whatever window is focused.

The owner's answer: approval IS the authorization, and the approval surface
must say what the job contains ("this job will shut down the PC", "this job
controls the desktop"). After approval the job runs unattended with no further
confirm, shutdown included.

DERIVED FROM TOOLS, NOT FROM WORDS
----------------------------------
The summary is computed from the job's TOOL LIST. It never greps the prompt for
"shutdown" or "delete": free text is exactly the thing a model (or a web page
the model read) controls, and a classifier that can be talked out of its answer
is not a classifier. Where the tool list comes from:

    action type "tool"     the one named tool (certain)
    action type "command"  a saved command, which runs shell (certain)
    action type "ask"      the tools the router would offer for the prompt

For an "ask" the model chooses its own tools at run time, so the list is a
PREDICTION of what it can reach, and the wording says "can", not "will". The
prediction is deliberately a superset of what the router offers: tool_router
trims to its top two groups, so a job that mentions the desktop, the power state
and a file would lose a group; the approval surface must not be less honest than
the router is lucky. It uses every group that matched, before trimming.

THE SAME LIST IS ENFORCED
-------------------------
The kinds computed here are frozen onto the job when it is approved and handed
to the run as JARVIS_JOB_APPROVED_KINDS. policy.decide() then allows a call
only if its tool's kind is one of them. A job approved for "controls the
desktop" that later reaches for run_shell is denied, because the person never
saw "runs shell commands". What was shown is what was authorised, no more.

Pure and import-light on purpose: policy.py, scheduler.py and ai_client.py all
use it, and scheduler.py is imported partway through tools.py's own
initialisation (see scheduler._needs_approval on the circular-import trap).
tool_router / tool_registry are therefore imported inside the functions.
"""

KIND_POWER = "power"
KIND_DESKTOP = "desktop"
KIND_FILES = "files"
KIND_SHELL = "shell"
# Tools the owner switched ON for the approval summary in the Tool Manager that
# job_risk has no built-in kind for (a custom tool, an MCP tool, ...). One
# shared kind on purpose: the summary names the tools, and approving it covers
# the tools it named -- "we'll get to more customization later" (owner).
KIND_MARKED = "marked"

# Most consequential first: this is the order the approval summary lists them.
KINDS = (KIND_POWER, KIND_DESKTOP, KIND_FILES, KIND_SHELL, KIND_MARKED)

# Which tools count, by name. Explicit rather than "everything in group X":
# the desktop group also holds read-only probes (list_windows, read_screen,
# take_screenshot, get_*) that cannot change anything, and telling someone "this
# job controls the desktop" because it can read a window title is crying wolf.
_TOOL_KIND = {
    "power_action": KIND_POWER,

    "type_text": KIND_DESKTOP, "write_on_screen": KIND_DESKTOP,
    "press_key": KIND_DESKTOP, "hotkey": KIND_DESKTOP,
    "click": KIND_DESKTOP, "click_on_text": KIND_DESKTOP, "drag": KIND_DESKTOP,
    "scroll": KIND_DESKTOP, "move_mouse": KIND_DESKTOP,
    "focus_window": KIND_DESKTOP,

    # "delete / overwrite": delete_path obviously; write_file and edit_file
    # replace content; move/rename/copy only overwrite when overwrite=true, but
    # the tool list cannot see arguments, so they are flagged for the capability.
    "delete_path": KIND_FILES, "write_file": KIND_FILES, "edit_file": KIND_FILES,
    "move_path": KIND_FILES, "rename_path": KIND_FILES, "copy_path": KIND_FILES,

    # dev_agent / code_agent write files AND run commands; they are listed here
    # because shell is the larger of the two powers they carry.
    "run_shell": KIND_SHELL, "run_custom_command": KIND_SHELL,
    "run_command": KIND_SHELL, "run_chain": KIND_SHELL,
    "dev_agent": KIND_SHELL, "code_agent": KIND_SHELL,
}

_PHRASE = {
    KIND_POWER: "shut down, restart, sleep or lock this PC",
    KIND_DESKTOP: "control the desktop (click, type and press keys in "
                  "whatever window is focused)",
    KIND_FILES: "delete or overwrite files",
    KIND_SHELL: "run shell commands",
    KIND_MARKED: "use tools you marked for review",
}

_POWER_VERBS = {
    "shutdown": "shut down this PC", "restart": "restart this PC",
    "sleep": "put this PC to sleep", "lock": "lock this PC",
    "cancel": "cancel a pending shutdown",
}


def is_builtin_flagged(tool_name):
    """True when job_risk flags this tool out of the box (the approval_summary
    default in tool_safety.py)."""
    return (tool_name or "").strip() in _TOOL_KIND


def _summary_flag(name):
    """The tool's approval_summary switch from tool_safety.json (Tool Manager).

    Falls back to the built-in default when tool_safety can't be read: during
    partial init (see the module docstring) the safe answer is "what job_risk
    always flagged", never "nothing".
    """
    try:
        from . import tool_safety  # lazy: tool_safety lazily imports us back
        return bool(tool_safety.get_flags(name)["approval_summary"])
    except Exception:  # noqa: BLE001
        return name in _TOOL_KIND


def kind_of(tool_name):
    """The flagged kind a tool belongs to, or None when it is not flagged.

    The Tool Manager's per-tool approval_summary switch decides: a built-in
    flagged tool switched OFF is not flagged (so an approval no longer covers
    it); any other tool switched ON is flagged as KIND_MARKED.
    """
    name = (tool_name or "").strip()
    if not name:
        return None
    builtin = _TOOL_KIND.get(name)
    on = _summary_flag(name)
    if builtin:
        return builtin if on else None
    return KIND_MARKED if on else None


def covering_kind(tool_name):
    """The approved kind that would cover a call to this tool, or None.

    Usually the tool's own kind. The exception is the desktop group: the default
    policy denies the WHOLE group when scheduled (read_screen, list_windows and
    take_screenshot included), and a job approved to "control the desktop" has to
    be able to look at it first -- the incident job's very first step was
    read_screen. So approving `desktop` covers the group's read-only probes too.
    The summary still only claims "controls the desktop" for tools that act,
    because that is what the person is being asked about.
    """
    own = kind_of(tool_name)
    if own:
        return own
    if is_builtin_flagged(tool_name):
        # The owner switched this tool out of the approval summary. It must not
        # sneak back in through the desktop group's read-only cover below.
        return None
    try:
        from . import tool_registry  # lazy: import cycle, see module docstring
        if tool_registry.group_of((tool_name or "").strip()) == "desktop":
            return KIND_DESKTOP
    except Exception:  # noqa: BLE001 -- during partial init: no extra cover
        pass
    return None


def kinds_of_tools(tool_names):
    """Flagged kinds present in a tool list, in display order."""
    found = {kind_of(n) for n in tool_names or ()}
    return [k for k in KINDS if k in found]


def phrase(kind):
    return _PHRASE.get(kind, kind)


def _ask_tools(prompt):
    """Every tool the router could offer for this prompt, untrimmed.

    route().matches records each qualifying keyword hit BEFORE the router trims
    to its top groups, so the groups seen there are a superset of what the live
    ask will be offered. Lets an import/router error propagate: assess() turns
    it into an explicit "couldn't work out what this job can do" line, because
    "no flagged actions" is the one wrong answer that must never be silent.
    """
    from . import tool_router, tool_registry  # lazy: import cycle, see docstring
    routed = tool_router.route(prompt)
    groups = []
    for group in list(routed.groups) + [m[0] for m in routed.matches]:
        if group and group not in groups:
            groups.append(group)
    tools = []
    for group in groups:
        for name in tool_registry.TOOL_GROUPS.get(group, []):
            if name not in tools:
                tools.append(name)
    return tools


def tools_for_action(action):
    """(tool names, certain) for a scheduler action dict.

    `certain` is True when the action names what it will run, False when the
    model picks its tools at run time (an "ask").
    """
    action = action or {}
    atype = action.get("type")
    if atype == "tool":
        return [str(action.get("tool") or "").strip()], True
    if atype == "command":
        return ["run_command"], True
    if atype == "ask":
        return _ask_tools(str(action.get("prompt") or "")), False
    return [], True  # notify: sends a message, runs nothing


def assess(action):
    """What this job contains, as data the approval surface can render.

        {"kinds":   ["power", "desktop"],            # frozen at approval
         "tools":   {"desktop": ["click", ...]},     # which tools put it there
         "lines":   ["This job can control the desktop (...)."],
         "certain": False}

    Never raises: a classifier that throws would leave the approval surface
    blank, which reads as "nothing risky here".
    """
    try:
        tools, certain = tools_for_action(action)
    except Exception as exc:  # noqa: BLE001
        return {"kinds": [], "tools": {}, "certain": False, "error": str(exc),
                "lines": ["Couldn't work out what this job can do (%s) -- "
                          "read its prompt carefully before approving." % exc]}

    kinds = kinds_of_tools(tools)
    by_kind = {k: [t for t in tools if kind_of(t) == k] for k in kinds}
    lead = "This job will" if certain else "This job can"
    lines = []
    for kind in kinds:
        text = _PHRASE[kind]
        if kind == KIND_MARKED:
            text = "%s (%s)" % (text, ", ".join(by_kind.get(kind) or []))
        if kind == KIND_POWER and certain:
            verb = _POWER_VERBS.get(str(((action or {}).get("args") or {})
                                        .get("action") or "").strip().lower())
            if verb:
                text = verb
        lines.append("%s %s." % (lead, text))
    return {"kinds": kinds, "tools": by_kind, "lines": lines, "certain": certain}


def authorized_kinds(job):
    """The kinds a job's approval covers. Empty list = approves nothing risky.

    A job that was never approved covers nothing. A job approved before this
    module existed carries `approved_at` but no `approved_kinds`; it was
    approved by a person under the rule "approval means it may run", so it is
    given what assess() derives now rather than silently losing its desktop or
    power access on upgrade.
    """
    job = job or {}
    if not job.get("approved_at"):
        return []
    stored = job.get("approved_kinds")
    if isinstance(stored, list):
        return [k for k in KINDS if k in stored]
    return list(assess(job.get("action") or {}).get("kinds") or [])
