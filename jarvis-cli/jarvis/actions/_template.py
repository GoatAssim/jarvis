"""Template for a new auto-discovered Jarvis tool file.

Copy this file to actions/your_tool_name.py (NOT starting with "_" — a
leading underscore is how the loader knows to skip a file, which is why
this template and any shared helper module you split out are both safe
to leave in actions/ without being mistaken for a tool themselves).

Read this whole file before writing your tool. Every rule below exists
because skipping it either breaks discovery silently or reopens a bug
Jarvis already paid to fix once (see jarvis-enhancement-plan.md §3.2/§3.3
if you have it). You should be able to write a correct, complete tool file
from this template alone, without reading tool_loader.py, tool_registry.py,
tool_safety.py, or tool_result_shaping.py first.

WHAT MAKES A FILE DISCOVERABLE
-------------------------------
jarvis/tools.py calls tool_loader.discover_actions() once, at import time,
which scans every actions/*.py file (except ones starting with "_") for
three REQUIRED module-level names: TOOL_SCHEMAS, TOOLS, TOOL_GROUP. A file
missing all three is silently treated as a non-action helper module, not
an error. A file with SOME of them but not all, or with a value of the
wrong shape, is REJECTED — logged and skipped, never crashes the rest of
discovery, but also never becomes callable. Check your terminal/log output
after adding a file; a typo here fails quiet, not loud.
"""

# ---------------------------------------------------------------------------
# 1. THE HANDLER(S)
#
# Every handler takes exactly one argument — the model's tool-call
# arguments as a plain dict (never None; discovery guarantees TOOLS/
# TOOL_SCHEMAS names line up, but a handler should still treat every key
# as optional/untrusted, same as every existing tool file does) — and
# returns a JSON-serializable dict. NEVER raise: catch your own
# exceptions and return {"error": "..."} instead, exactly like every
# built-in tool (see tools.py's own module docstring). An uncaught
# exception is still caught one layer up by tools.execute_tool's try/except
# and turned into {"error": "<name> failed: <e>"}, but returning your own
# clearer error message is almost always more useful to the model than
# a bare exception repr.
# ---------------------------------------------------------------------------


def tool_example_ping(args):
    args = args or {}
    target = (args.get("target") or "").strip()
    if not target:
        return {"needs_clarification": True, "message": "Ping what?"}
    # ... do the real work here ...
    return {"ok": True, "target": target, "result": "pong"}


# ---------------------------------------------------------------------------
# 2. TOOL_SCHEMAS — required. Same shape as every existing *_TOOL_SCHEMAS
#    list (see git_tools.GIT_TOOL_SCHEMAS for a real, short example) — a
#    list of OpenAI-function-style schema dicts. Jarvis hands these
#    directly to whichever AI provider adapter is active; write
#    "description" the way you'd brief a coworker on exactly *when* to
#    reach for this tool, since that's what steers correct tool
#    selection.
#
#    Naming: every schema "name" must match ^[a-z][a-z0-9_]{0,63}$ (lower
#    snake_case, starts with a letter — the same pattern every built-in
#    tool name already follows: get_datetime, git_commit_all, ...).
#    Discovery rejects the whole file if any name doesn't match, or if a
#    name collides with an existing tool (built-in or from another
#    actions/ file) — the existing tool always wins, yours is logged and
#    skipped.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "example_ping",
        "description": (
            "One-line example schema — replace this with a real, specific "
            "description of when the model should call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "What to ping."},
            },
            "required": ["target"],
        },
    },
]

# TOOLS — required. name -> handler, one entry per TOOL_SCHEMAS name (and
# no extra entries with no matching schema — discovery rejects that too).
TOOLS = {
    "example_ping": tool_example_ping,
}

# ---------------------------------------------------------------------------
# 3. TOOL_GROUP — required. The tool_registry.py router group these tools
#    join. Two choices:
#
#    a) An EXISTING group name (see tool_registry.TOOL_GROUPS — "files",
#       "web", "system_control", etc.). Your tool becomes a sibling of
#       everything already in that group: when tool_router.route() decides
#       the group is relevant, your tool is offered alongside the rest of
#       it, and you can skip TOOL_KEYWORDS below and still be reachable
#       (piggybacking on a sibling's keyword hit) — though your own
#       keywords still make routing more precise, not less.
#
#    b) A NEW group name. This is the "backwards compatible with the
#       keyword router" case that actually matters: tool_router.route()
#       has no idea your group exists until TOOL_KEYWORDS gives it a
#       reason to activate. A brand-new group with an empty TOOL_KEYWORDS
#       is only ever reachable through search_tools's low-confidence
#       fallback path — discover_actions() will still load it, but logs a
#       loud warning, because "deliberately search-tools-only" and
#       "forgot to add keywords" look identical from outside.
# ---------------------------------------------------------------------------

TOOL_GROUP = "example"

# TOOL_KEYWORDS — required in practice for a NEW group (see above),
# optional but recommended even when joining an existing one. Same
# {phrase: weight} shape as tool_registry.TOOL_KEYWORDS — a phrase needs
# weight >= tool_router.MIN_SCORE (currently 5) to count as real signal,
# and is matched as a whole word/phrase (word-boundary, not raw substring
# — "ping" won't false-positive-match "pinging"). A phrase can instead be
# {"weight": w, "not_with": ["other phrase", ...]} if it needs to NOT
# match when some other phrase is also present — see
# tool_registry.TOOL_KEYWORDS's "screen shot" / "recording" entry for a
# real example of why that exists.
TOOL_KEYWORDS = {
    "example_ping": {"ping": 10, "ping the": 10},
}

# TOOL_PACK_INSTRUCTION — optional. One short line of workflow guidance,
# the auto-discovered equivalent of tool_registry.TOOL_PACK_INSTRUCTIONS's
# per-group entries (see that dict for real examples — e.g. system_control's
# "stage and commit -> git_commit_all in one call, not four"). Only takes
# effect if TOOL_GROUP above is a NEW group; joining an existing group
# keeps that group's already-curated instruction untouched, so don't rely
# on this to change how an existing group behaves.
TOOL_PACK_INSTRUCTION = ""

# ---------------------------------------------------------------------------
# 4. CONFIRM-GATING — optional, and NOT a model-fillable "confirm": true
#    argument on your own schema. That shape (a tool parameter the model
#    itself decides to set) is exactly the bug Mark LIII's own README
#    calls out fixing: nothing actually verifies a human ever saw the
#    prompt if the model can just fill in confirm=true itself. git_tools.py
#    still has one instance of this today (git_run's "confirm" param) —
#    don't copy it into a new tool; use the mechanism below instead, which
#    IS the real out-of-band gate: ai_client._make_tool_executor checks
#    tool_safety.requires_confirmation(name) BEFORE calling any tool, by
#    name, from a config file the model has no access to (~/.jarvis/
#    tool_safety.json, editable only via the CLI or the web console's
#    Debug dashboard) — so declaring your tool here is the same real
#    protection every DEFAULT_CONFIRM_REQUIRED built-in tool already gets,
#    not a weaker parallel system.
#
#    Add your tool's name here if it mutates, deletes, or executes
#    something real (mirroring tool_safety.DEFAULT_CONFIRM_REQUIRED's own
#    built-in set — write_file, run_command, package_install, ...); leave
#    both empty for a read-only tool.
# ---------------------------------------------------------------------------

TOOL_CONFIRM_REQUIRED = set()   # e.g. {"example_ping"} if it were destructive
TOOL_AI_REVIEW = set()          # e.g. for a tool fuzzy/risky enough to want
                                 # a second AI's opinion before confirming
                                 # (see tool_safety.DEFAULT_AI_REVIEW)

# ---------------------------------------------------------------------------
# 5. RESULT SHAPING — optional. Only add this if your tool's result can be
#    large (a search returning many rows, a long stdout capture, ...).
#    Same shape as tool_result_shaping.TOOL_RESULT_SPECS entries — see that
#    module's docstring for the full key list (drop_fields,
#    truncate_fields, list_item_drop, list_item_truncate). Leave this
#    empty and your tool's result is returned completely untouched at
#    every verbosity, which is the correct default for anything small.
# ---------------------------------------------------------------------------

TOOL_RESULT_SPECS = {}

# That's the whole contract. Delete example_ping and this comment block,
# write your real handler(s) and schema(s) above, and the file is live the
# next time Jarvis starts — no edits anywhere else.
