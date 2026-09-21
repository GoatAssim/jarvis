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
Before writing one: if what you're adding is instructions rather than code —
"here's how I want the weekly report built" — you want a SKILL, not an
action. Skills live in ~/.jarvis/skills/<name>/SKILL.md, need no Python, and
cost nothing in the prompt until they're loaded (see jarvis/skills.py, or
the Skills button in the web UI). Actions are for capabilities that have to
execute something.

jarvis/tools.py calls tool_loader.discover_actions() once, at import time,
which scans every actions/*.py file (except ones starting with "_") for
three REQUIRED module-level names: TOOL_SCHEMAS, TOOLS, TOOL_GROUP. A file
missing all three is silently treated as a non-action helper module, not
an error. A file with SOME of them but not all, or with a value of the
wrong shape, is REJECTED — logged and skipped, never crashes the rest of
discovery, but also never becomes callable. Check your terminal/log output
after adding a file; a typo here fails quiet, not loud.

SAFE IMPORTS AT MODULE LEVEL — READ THIS BEFORE YOU IMPORT ANYTHING
---------------------------------------------------------------------------
Your file is imported by tool_loader.discover_actions(), which itself runs
PARTWAY THROUGH jarvis/tools.py's own module initialization (after the
manually-wired TOOLS dict is built, but before AUTO_TOOL_GROUPS,
AUTO_TOOL_KEYWORDS, and friends are defined at the bottom of that file).
That means jarvis.tools is only *partially* initialized while your file's
top level is executing.

Anything that imports back from jarvis.tools — directly, or transitively
through another module — will raise ImportError at that moment, EVEN
THOUGH the exact same import works fine everywhere else in the codebase.
ai_client, tool_router, and tool_registry all import names FROM
jarvis.tools, so importing any of them (or anything that imports them) at
your file's module level creates exactly this cycle. This isn't
hypothetical: it's the actual bug that silently dropped tool_dev_agent
from dev_agent.py for a while — no crash, just a quiet
"[tools] Rejected your_file.py: Failed to load: cannot import name
'AUTO_TOOL_GROUPS' from partially initialized module 'jarvis.tools'
(most likely due to a circular import)" in the discovery log, and the
tool missing everywhere (CLI, debug menu, everywhere) with no other clue.

The fix: if you need ai_client, ai_config, tool_router, tool_registry, or
anything else that imports from jarvis.tools, import it LAZILY — inside
the function that actually uses it, not at the top of the file:

    def tool_my_thing(args):
        from .. import ai_client, ai_config  # imported here, not at module level
        ...

By the time a handler actually runs, jarvis.tools has long since finished
initializing, so the cycle never triggers. Imports of modules that don't
touch jarvis.tools (subprocess, pathlib, re, your own dev_agent_*.py-style
helper modules, ...) are completely unaffected — this only applies to the
handful of modules that import back from tools.py.
"""

# ---------------------------------------------------------------------------
# 1. THE HANDLER(S)
#
# Every handler takes the model's tool-call arguments as a plain dict
# (never None; discovery guarantees TOOLS/TOOL_SCHEMAS names line up, but
# a handler should still treat every key as optional/untrusted, same as
# every existing tool file does) — and returns a JSON-serializable dict.
# NEVER raise: catch your own exceptions and return {"error": "..."}
# instead, exactly like every built-in tool (see tools.py's own module
# docstring). An uncaught exception is still caught one layer up by
# tools.execute_tool's try/except and turned into {"error": "<name>
# failed: <e>"}, but returning your own clearer error message is almost
# always more useful to the model than a bare exception repr.
#
# To SHOW the user something (a toast, a card in the chat, a modal) or to
# ASK them something (confirm / choose / prompt / form), see section 6 at
# the bottom of this file — `from jarvis import ui_bridge as ui`. A return
# value goes to the model, not to the person; ui_bridge is the direct line.
#
# Optional second argument: `fn(args, context)` instead of `fn(args)`.
# tools.execute_tool() introspects your handler's own signature (via
# tools._accepts_context) and only passes a second arg if you declared
# one — a plain `fn(args)` handler is completely unaffected, so add the
# second parameter only if you actually need it. `context` is a
# tools.ToolContext with:
#   - context.round_budget_remaining() — zero-arg callable, current
#     remaining shared tool-call rounds for this ask(); call it fresh
#     each time, don't cache the result, since it changes as the turn
#     progresses.
#   - context.emit_event(job_id, seq, phase, status, **fields) — writes
#     one JARVIS_MEDIA progress line (see dev_agent_events.py) for a
#     handler that runs long enough to want live sub-step visibility
#     instead of a single return value at the end. Most tools don't need
#     this.
#   - context.conv_id — the active conversation id, or None.
#   - context.ui — "web" or "cli" (mirrors JARVIS_UI), if a handler wants
#     to skip emitting UI-only events cheaply.
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
#
#    How this ties into capacity mode: every mode in ai_client.
#    PROMPT_MODE_DEFS carries a tool_result_verbosity of "full", "medium",
#    or "low", and shape_result() only ever applies your "medium"/"low"
#    keys — "full" always passes your result through untouched, spec or
#    no spec. As of this writing the four built-in modes map like this:
#
#        mode      | capacity label | tool_result_verbosity
#        ----------+-----------------+-----------------------
#        full      | 400% Capacity   | full    (your spec is a no-op)
#        compact   | 100% Capacity   | medium  (the default mode — write
#                   |                 |          your "medium" key for this)
#        precise   | 150% Capacity   | full    (your spec is a no-op)
#        ultra     | 50% Capacity    | low     (write your "low" key for
#                   |                 |          the tightest trim)
#
#    Practically: "compact" is the default a person is in most of the
#    time, so your "medium" entries are the ones actually doing work
#    day-to-day; "low" only kicks in once someone's explicitly in "ultra".
#    "full" and "precise" both intentionally skip shaping entirely — they
#    exist for when someone wants the richest possible context, so
#    trimming there would defeat the point. (This table describes the
#    current registry, not a hardcoded rule — a custom mode appended to
#    PROMPT_MODE_DEFS with a different tool_result_verbosity would follow
#    the same "full"/"medium"/"low" mechanics, just under a new mode name.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. TALKING TO THE USER — popups, dialogs and questions (jarvis/ui_bridge.py)
#
# A handler's return value goes to the MODEL. It is not shown to the person.
# So if your tool needs to warn someone, show them a table, or ask them
# something, returning a dict is the wrong instrument — the model may
# paraphrase it, bury it, or not mention it at all.
#
# ui_bridge is the direct line:
#
#     def tool_my_thing(args):
#         from jarvis import ui_bridge as ui     # lazy import, see section 2
#         ui.toast("Backup finished", level="success")
#         return {"ok": True}
#
# SHOW SOMETHING (returns immediately, never blocks)
# --------------------------------------------------
#     ui.toast(message, level="info", title="", timeout=None)
#         Transient corner message. The lightest possible FYI.
#         level="error" stays until dismissed; everything else fades after
#         ~4s. This is the same widget the web UI already uses for its own
#         errors — now reachable from any tool.
#
#     ui.bubble(title, body="", level="info")
#         A card IN the chat thread. Persists, survives a page reload, the
#         user can scroll back to it. Use for output worth keeping: a table,
#         a diff, a summary, a report.
#
#     ui.dialog(title, body="", level="info", actions=None)
#         A real modal, outside the chat. It INTERRUPTS. Reserve it for
#         things that genuinely should — a misconfiguration, a destructive
#         result, something that needs attention before anything else.
#
#     ui.progress(label, value=None, total=None, job_id=None, done=False)
#         An updatable row, bottom-left. Returns the job_id, so:
#             pid = ui.progress("Indexing")
#             ui.progress("Indexing", 3, 10, job_id=pid)
#             ui.progress("Indexing", 10, 10, job_id=pid, done=True)
#
#     ui.dismiss(event_id)   removes a dialog or progress row early.
#
# `level` is one of: "info", "success", "warn", "error". It controls colour
# and icon, and it is the ONLY styling a tool can influence — see the safety
# note at the bottom of this section.
#
# ASK SOMETHING (blocks until answered)
# --------------------------------------
#     ui.confirm(question, body="", default=False, confirm_label="Yes",
#                cancel_label="No", level="warn")          -> bool
#     ui.choose(question, options, default=None)            -> str
#     ui.prompt(question, default="", placeholder="",
#               secret=False)                               -> str
#     ui.form(title, fields)                                -> dict
#
#         fields is a list of
#           {"name", "label", "type", "default", "placeholder",
#            "options", "required"}
#         type is text | number | password | select | checkbox | textarea.
#         An unrecognised type renders as text rather than breaking the
#         dialog.
#
# Escape, clicking the backdrop, and closing the window all resolve to the
# SAFE answer — a dismissed confirm is never a yes.
#
# *** THE HEADLESS RULE — READ THIS ONE ***
# -----------------------------------------
# EVERY blocking call takes a `default`, and that default is returned
# IMMEDIATELY when nobody is watching: a scheduled job at 3am, a channel
# daemon, a test run. ui_bridge checks for this BEFORE printing anything, so
# such a run never even emits a question.
#
# This is not a nicety. A tool that blocks on input() inside a scheduler tick
# hangs the entire tick — silently, and until the process is killed. Your
# tool WILL eventually be run by a scheduled job, whether you intended that
# or not, because the user can schedule any tool.
#
# So point the default at the safe outcome:
#
#     # RIGHT — an unattended run deletes nothing
#     if not ui.confirm("Delete %d files?" % n, default=False):
#         return {"ok": True, "deleted": 0, "note": "cancelled"}
#
#     # WRONG — an unattended run silently deletes everything
#     if ui.confirm("Delete %d files?" % n, default=True):
#
# There is also a timeout (120s by default) for a web dialog nobody answers,
# after which the default is returned the same way.
#
# ui.confirm() IS NOT A SECURITY GATE
# -----------------------------------
# It is a courtesy question, and the model can see that you asked it. The
# REAL gate is TOOL_CONFIRM_REQUIRED in section 4 above, enforced out of band
# against ~/.jarvis/tool_safety.json — a file the model cannot reach.
#
# A destructive tool wants BOTH:
#
#     TOOL_CONFIRM_REQUIRED = {"cleanup_temp"}   # the protection
#     ...and inside the handler:
#     ui.confirm("Delete these 12 files?", body=listing, default=False)
#                                                # the readable question
#
# There is also a policy layer (jarvis/policy.py) that scores every call by
# tool, arguments and execution context, and can escalate a call to
# confirm/review/deny on its own. You do not call it; it wraps you. Its one
# implication for a tool author is that a tool taking a `path` or `command`
# argument gets scored more accurately than one that hides the same thing
# inside an opaque blob — so name your arguments plainly.
#
# EVERYTHING IS TEXT, NEVER MARKUP
# --------------------------------
# Every field you pass is inserted with textContent on the browser side. A
# filename containing <script> is displayed, not executed. Do not try to
# smuggle HTML in; it will show up as literal angle brackets, which is the
# intended behaviour and not a bug to work around.
#
# ON A PLAIN TERMINAL
# -------------------
# All of the above still works: toasts and bubbles become styled stderr
# output, and the blocking prompts become input() / getpass(). You do not
# write two code paths — ui_bridge picks the surface per call.
# ---------------------------------------------------------------------------

TOOL_RESULT_SPECS = {}

# ---------------------------------------------------------------------------
# 7. PERSONAS — optional, and INDEPENDENT of everything above. A file can
#    carry PERSONAS with no TOOL_SCHEMAS/TOOLS/TOOL_GROUP at all (a
#    persona-only file), TOOL_SCHEMAS/TOOLS/TOOL_GROUP with no PERSONAS
#    (an ordinary tool file, everything above this section), or both at
#    once. Either way it's discovered the same way, by the same
#    discover_actions() scan, at the same startup — nothing extra to wire
#    up, no edits anywhere else.
#
#    A persona is what shows up as a labeled pill in the web UI's Skin
#    modal (the "Skin" button next to the topbar title), alongside the
#    built-in J.A.R.V.I.S/Verity/Friday/Edith/Karen presets. Picking one
#    sets, in a single click: the assistant's display name, how it
#    addresses the user, its attitude/personality, its accent color
#    palette, its logo (boot screen + every brand mark in the UI), and
#    optionally the Skin modal's own Interface (corner rounding / glow /
#    text size) and Saturation sliders. Every field below is genuinely
#    optional except id/name and a color — write only what you care about.
#
#        PERSONAS = [
#            {
#                "id": "orion",                 # slug; derived from `name`
#                                                # if omitted. Must not
#                                                # collide with a built-in
#                                                # persona id (jarvis,
#                                                # verity, friday, edith,
#                                                # karen) — that's rejected,
#                                                # logged, and dropped, the
#                                                # same as a tool name
#                                                # collision above.
#                "name": "Orion",               # REQUIRED — the label shown
#                                                # on the preset pill.
#                "assistant_name": "O.R.I.O.N.",# what the assistant is
#                                                # actually called once this
#                                                # persona is picked (and
#                                                # saved to ai_config.json's
#                                                # persona.assistant_name —
#                                                # see ai_config.py). Falls
#                                                # back to `name` if omitted.
#                "address_user_as": "commander",# optional — how it
#                                                # addresses the user
#                                                # (persona.address_user_as).
#                                                # Left alone if omitted, so
#                                                # a persona can change only
#                                                # the parts it cares about.
#                "attitude": "formal",          # optional — the id of an
#                                                # EXISTING attitude, built-in
#                                                # (see ai_client.
#                                                # ATTITUDE_PRESETS: dry,
#                                                # cheerful, snarky, formal,
#                                                # warm, blunt) or one
#                                                # another persona registered
#                                                # (below).
#                #   ...or register a brand new attitude inline instead of
#                #   referencing an id — this is the "(could add custom
#                #   ones too)" case:
#                # "attitude": {
#                #     "label": "Stoic",         # shown in the Skin
#                #                               # modal's Attitude dropdown
#                #     "full": "calm and unflinching under pressure, "
#                #             "measures every word before it's spoken",
#                #     "compact": "Calm, measured, unflinching.",
#                # },
#                # An inline attitude is globally registered under a slug
#                # derived from its label (or "id" if you give one) the
#                # moment this file is discovered — it becomes selectable
#                # for ANY persona from then on, not just this one, and
#                # actually changes ai_client._system_prompt()'s output
#                # (see ai_client.py's ATTITUDE_PRESETS.update() merge),
#                # not just what the dropdown displays.
#
#                "hex": "#7dd3fc",              # a single accent color —
#                                                # goes through the SAME
#                                                # saturation-aware
#                                                # derivation math every
#                                                # plain accent-color pick
#                                                # already uses (soft/dim/
#                                                # border/secondary/tertiary
#                                                # all computed from this).
#                                                # REQUIRED unless you give
#                                                # "vars" instead (or both).
#                # "vars": {                     # ...or a FULLY hardcoded
#                #     "--accent": "#7dd3fc",     # palette instead — the
#                #     "--accent-soft": "#4a9bc9",# exact literal values,
#                #     "--accent-dim": "#1c4a63", # never derived, never
#                #     "--accent-glow": "rgba(125, 211, 252, 0.35)",
#                #     "--accent-secondary": "#f2b544",
#                #     "--accent-tertiary": "#2b5cff",
#                #     "--accent-secondary-rgb": "242, 181, 68",
#                #     "--accent-tertiary-rgb": "43, 92, 255",
#                #     "--status-online": "var(--accent)",
#                #     "--border": "rgba(125, 211, 252, 0.16)",
#                #     "--border-strong": "rgba(125, 211, 252, 0.34)",
#                #     "--bg": "#04070d", "--bg-1": "#070d16",
#                #     "--bg-panel": "rgba(9, 18, 30, 0.68)",
#                #     "--bg-panel-2": "rgba(13, 24, 38, 0.55)",
#                #     "--bg-raised": "#0d1826",
#                # },                           # affected by saturation — see below. Only
#                                                # these exact keys are
#                                                # accepted (anything else is
#                                                # dropped with a warning);
#                                                # this is the same palette
#                                                # shape the built-in
#                                                # J.A.R.V.I.S/Verity/Friday/
#                                                # Edith/Karen presets use.
#                                                # IMPORTANT: giving "vars"
#                                                # makes this persona a
#                                                # FIXED palette — like every
#                                                # built-in preset, the Skin
#                                                # modal's Saturation slider
#                                                # has no effect on it. Give
#                                                # only "hex" (no "vars") if
#                                                # you want saturation to
#                                                # keep working.
#
#                "logo": {                      # optional — swaps the boot
#                                                # screen ring AND every
#                                                # brand mark in the UI.
#                                                # Omit entirely to keep the
#                                                # default arc-reactor rings.
#                    "svg_brand": (             # raw SVG markup, dropped
#                        '<circle cx="20" cy="20" r="18" '
#                        'class="brand-mark__ring"/>'
#                        '<circle cx="20" cy="20" r="3" '
#                        'class="brand-mark__core"/>'
#                    ),                         # required if using svg —
#                                                # inserted into the 40x40
#                                                # viewBox .brand-mark <svg>
#                                                # elements (topbar, buttons,
#                                                # etc.) exactly like the
#                                                # built-in Verity smiley-
#                                                # face mark already is.
#                    "svg_boot": '...',         # optional — same idea, for
#                                                # the once-per-session 200x200
#                                                # viewBox boot screen ring.
#                                                # Falls back to the default
#                                                # arc-reactor rings if
#                                                # omitted (the brand mark is
#                                                # seen far more often, so
#                                                # it's the one that matters
#                                                # most to get right).
#                },
#                # ...or a plain PNG instead of hand-drawn SVG — pick ONE of
#                # svg_brand (above) or one of these two, not several:
#                # "logo": {"png_path": "orion_logo.png"},   # resolved
#                #     # relative to THIS FILE's own directory (or give an
#                #     # absolute path). Read once at discovery time and
#                #     # embedded as a base64 data URI — nothing needs to be
#                #     # copied or served from anywhere. Capped at 512KB.
#                # "logo": {"png_base64": "iVBORw0KG..."},   # same thing,
#                #     # pre-encoded, for a PNG you already have in memory
#                #     # rather than sitting on disk.
#                #
#                # A PNG logo's background is used exactly as-is — Jarvis
#                # does not inspect, strip, key out, or otherwise touch it.
#                # A transparent PNG shows the app's own background through
#                # it (like the SVG rings do); a PNG with a solid/opaque
#                # background just shows that background as part of the
#                # mark. Neither is treated as a mistake to fix — if you
#                # want it transparent, export it transparent; if you don't,
#                # it stays exactly as given.
#
#                "interface": {                 # optional — the Skin
#                                                # modal's own "Interface"
#                                                # section, applied the
#                                                # moment this persona is
#                                                # picked (on top of
#                                                # whichever Theme is
#                                                # active — see that
#                                                # section's own "applies on
#                                                # top of any theme" hint).
#                    "corner_rounding": 40,      # 0-200, default 100 (100 =
#                                                # unchanged/normal).
#                    "glow": 20,                 # 0-200, default 100.
#                    "text_size": 110,           # 80-130, default 100.
#                },                             # Give only the keys you
#                                                # want to change — any
#                                                # omitted key leaves that
#                                                # slider wherever it
#                                                # already was.
#
#                "saturation": 80,              # optional, 0-150, default
#                                                # 100 — pre-sets the Skin
#                                                # modal's own Saturation
#                                                # slider when this persona
#                                                # is picked. Only has any
#                                                # visible effect if you
#                                                # gave "hex" above without
#                                                # "vars" (see that field's
#                                                # note) — a fully hardcoded
#                                                # "vars" palette ignores
#                                                # saturation entirely, same
#                                                # as every built-in preset.
#            },
#        ]
#
#    VALIDATION AND FAILURE MODE
#    ----------------------------
#    Every persona is validated independently at discovery time (see
#    jarvis/persona_registry.py) — a bad entry (missing name, malformed
#    hex, an unreadable png_path, a reserved id, ...) is logged and
#    dropped, exactly like a bad TOOL_SCHEMAS entry above; it never takes
#    down the rest of this file's personas, this file's tools, or
#    discovery as a whole. Check your terminal/log output after adding
#    one, same advice as this file's very first section.
#
#    A duplicate id across TWO DIFFERENT files (built-in actions/ or
#    ~/.jarvis/tools/) is also logged and dropped — first registration
#    wins, and built-in actions/ is always scanned before a user's own
#    ~/.jarvis/tools/, so a shipped persona can never be silently
#    shadowed by a user file the way a shipped tool name can't either.
#
#    WHERE THIS SURFACES
#    --------------------
#    `jarvis personas-list` prints the full validated/normalized catalog as
#    JSON (same one-shot-JSON-to-stdout contract as `jarvis tools-list`).
#    The web UI's Skin modal fetches this once from GET /api/personas (see
#    web/server.js) and merges it into the same preset-pill list the
#    built-in personas already render as — see web/public/app.js's
#    REGISTERED_PERSONAS / loadRegisteredPersonas(). No separate UI, no
#    separate save path: picking a registered persona and hitting Save in
#    the Skin modal writes assistant_name/address_user_as/attitude to
#    ai_config.json exactly the way picking Verity or Friday already does.
#
#    See DOCUMENTATION/PERSONAS_GUIDE.md for the end-to-end walkthrough
#    (a full worked example file, screenshots of where each field lands,
#    and the full list of allowed "vars" keys).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8. TEST_CHECKLIST / TEST_CHECKLIST_GROUP — optional, and the way a tool that
#    is NOT in the shipped web/public/test-checklist-data.js gets a real entry
#    in the web console's Menu -> Test Checklist.
#
#    Why this exists: that panel's catalogue is one static file that ships
#    with the app. A tool a user wrote into ~/.jarvis/tools/ (Menu -> Custom
#    Tools) can never be in it, so without this it is listed as "NO CHECKLIST"
#    forever, however good the tool is. A module supplies its own entry the
#    same way it supplies TOOL_KEYWORDS: a module-level name, discovered at
#    startup, no edits anywhere else.
#
#    WHICH HOME DOES MY TOOL USE?
#      - A tool that ships with jarvis (a file in jarvis/actions/): either
#        works, but AGENTS.md's rule ("adding a tool means updating its
#        checklist entry") is satisfied by whichever you pick. Use the module
#        if you want the test notes to sit next to the code. Never both — the
#        coverage test fails if a tool has an entry in both places.
#      - A tool in ~/.jarvis/tools/: this section is your only option.
#
#    TEST_CHECKLIST is a dict, tool name -> entry, in EXACTLY the shape the
#    shipped file uses under "tools" (documented at the top of
#    test-checklist-data.js), except "group": leave it out and it follows
#    TOOL_GROUP above; if you give it, it must equal TOOL_GROUP.
#
#        does    one line: what the tool is for                    (required)
#        steps   one or more tests                                 (required)
#                {"ask": "<prompt to type into Ask>", "expect": "..."}
#                {"run": {<arguments for a direct run from Debug>},
#                 "expect": "..."}          (exactly one of ask / run, each
#                                            step; "expect" is what a PASS
#                                            looks like, concretely)
#        needs   optional list: accounts, installed programs, hardware
#        os      optional: "windows" if it only works there
#        care    optional: side effects to warn a tester about
#        watch   optional list: known gotchas worth checking
#
#    Use <angle brackets> for things the tester must fill in — the panel
#    highlights them. Do NOT put results (ticks, verdicts, notes) here: those
#    live only in the tester's browser. Editing a step's text un-ticks it for
#    testers, which is what you want when the behaviour changed.
#
#    TEST_CHECKLIST_GROUP is only for a BRAND-NEW TOOL_GROUP (one that is
#    neither a router group jarvis already ships nor "custom", which the panel
#    already labels): it names your category's section. Without it the panel
#    still shows your tool, under a label made from the group id.
#
#    VALIDATION AND FAILURE MODE
#    ----------------------------
#    Validated at discovery by jarvis/checklist_schema.py — the same code that
#    checks the shipped file. Unlike TOOL_SCHEMAS, a malformed entry does NOT
#    reject your file: it is dropped, logged ("[checklist] your_file.py: ...")
#    and your tool loads normally, because a typo in test notes shouldn't take
#    a working tool offline. The quiet failure that leaves is why the Custom
#    Tools editor's Check button reports it too — use it.
#
#    WHERE THIS SURFACES
#    --------------------
#    tools.tools_list_payload() (`jarvis tools-list`, GET /api/tools) adds
#    `checklist` (and `checklist_group`) to a tool that supplied them; the
#    panel merges those into its shipped catalogue. No new route, no storage.
# ---------------------------------------------------------------------------

TEST_CHECKLIST = {
    "example_ping": {
        "does": "Replies 'pong' for whatever target it is given.",
        "steps": [
            {"ask": "Ping <a hostname>.",
             "expect": "Replies with pong for that target."},
            {"run": {"target": "<a hostname>"},
             "expect": "ok: true, result 'pong', and the target echoed back."},
            {"run": {},
             "expect": "Asks what to ping instead of guessing (needs_clarification)."},
        ],
        "watch": ["A blank target must ask a question, not return a fake pong."],
    },
}

# Only needed because TOOL_GROUP above is the brand-new group "example".
TEST_CHECKLIST_GROUP = {
    "label": "Examples",
    "blurb": "Template tools; delete this once you have written your own.",
}

# That's the whole contract. Delete example_ping and this comment block,
# write your real handler(s) and schema(s) above, and the file is live the
# next time Jarvis starts — no edits anywhere else.