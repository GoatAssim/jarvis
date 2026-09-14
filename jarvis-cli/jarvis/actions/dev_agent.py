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


def tool_dev_agent(arguments, context=None):
    """Placeholder handler \u2014 accepts the real (args, context) signature so
    call sites and tests written against \u00a74.4's eventual shape don't need
    to change again once the loop is implemented, but does none of the real
    work yet. context is optional here (defaults to None) purely so this
    stub is also callable the old one-argument way while nothing downstream
    of the executor requires the second arg.

    NEVER raise \u2014 same contract as every other tool handler (see
    actions/_template.py \u00a71): on the day this stops being a stub, keep
    returning {"error": "..."} for input errors instead of letting
    exceptions escape.
    """
    arguments = arguments or {}
    description = (arguments.get("description") or "").strip()
    if not description:
        return {"error": "description is required"}
    return {
        "error": (
            "dev_agent is registered and routable but not implemented yet \u2014 "
            "the plan/write/install/run/fix loop (\u00a74.3/\u00a74.4 of the "
            "implementation plan) hasn't been built. This call was a no-op; "
            "nothing was written or run."
        ),
    }


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
