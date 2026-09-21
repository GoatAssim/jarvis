"""Auto-discovery for drop-in tool modules under jarvis/actions/.

This generalizes the manual pattern already used for every existing tool
file (git_tools.py's GIT_TOOL_SCHEMAS/GIT_TOOLS, imported and spread into
tools.py by hand) into a real convention a NEW tool file can opt into
without anyone editing tools.py, tool_registry.py, tool_safety.py, or
tool_result_shaping.py. Nothing already wired the manual way is touched or
required to migrate — this only adds a second, additive path into the same
catalog.

A file in jarvis/actions/ is discovered as a tool module if it exposes:

    TOOL_SCHEMAS = [ {...}, ... ]   # required — same shape as every
                                    # existing *_TOOL_SCHEMAS list
    TOOLS        = {"name": fn}    # required — fn(args: dict) -> a
                                    # JSON-serializable dict, never raises
    TOOL_GROUP   = "some_group"    # required — the tool_registry.py router
                                    # group these tools join (an existing
                                    # group name, or a new one)

...and optionally:

    TOOL_KEYWORDS          = {"name": {"phrase": weight, ...}}
    TOOL_PACK_INSTRUCTION  = "one short line of workflow guidance"
    TOOL_CONFIRM_REQUIRED  = {"name", ...}
    TOOL_AI_REVIEW         = {"name", ...}
    TOOL_RESULT_SPECS      = {"name": {...}}  # tool_result_shaping.py shape
    TEST_CHECKLIST         = {"name": {...}}  # this tool's Menu -> Test Checklist
                                              # entry (does/steps/needs/os/care/
                                              # watch), for tools that can't be in
                                              # web/public/test-checklist-data.js
    TEST_CHECKLIST_GROUP   = {"label": ..., "blurb": ...}  # names a brand-new
                                              # TOOL_GROUP's section in that panel

A file (with or without the TOOL_SCHEMAS/TOOLS/TOOL_GROUP trio above) can
ALSO optionally expose:

    PERSONAS = [ {"id": ..., "name": ..., "hex": ..., ...}, ... ]

...registering one or more Skin-modal personas — see persona_registry.py
for the full field-by-field validation and actions/_template.py §7 for
the human-facing writeup. This is a fully independent contract from the
tool one above: a persona-only file (no TOOL_SCHEMAS/TOOLS/TOOL_GROUP at
all) is valid and its personas are still discovered.

See actions/_template.py for the full contract, one field at a time,
including why TOOL_KEYWORDS matters for routing (the section called out
below) and how confirm-gating is supposed to work.

--- TEST_CHECKLIST / TEST_CHECKLIST_GROUP (master plan G.1) ---

The Test Checklist panel's shipped catalogue (web/public/test-checklist-data.js)
can only ever document tools that ship WITH jarvis. A tool a user wrote into
~/.jarvis/tools/ is unknown to it, so before G.1 it showed "NO CHECKLIST"
forever. A module can now carry its own entry: TEST_CHECKLIST maps tool name
-> entry, in exactly the shape the shipped file uses (checklist_schema.py is
the single definition and the single validator), and TEST_CHECKLIST_GROUP
labels a brand-new TOOL_GROUP so it gets a real section instead of a bare id.

Failure mode differs from TOOL_KEYWORDS on purpose: a malformed entry is
dropped and logged, and the TOOL FILE STILL LOADS. A checklist entry is notes
about a tool, and rejecting a working tool over a typo in them would be the
wrong trade — the loud place for the report is the log line here and the
Custom Tools editor's Check button (custom_tools_store.validate_source).

--- Why TOOL_KEYWORDS is not really optional ---

tool_router.route() (the "reroute" a normal user message goes through
before any tools are offered — see tool_router.py) only ever offers a tool
because *some* tool in its TOOL_GROUPS group scored a keyword hit. A tool
dropped into an EXISTING group (e.g. "files") can lean on a sibling's
keywords and skip its own — but a tool that introduces a brand-new group
with zero keywords anywhere in it can never be routed by tool_router.route()
at all: it would only ever be reachable through search_tools (the
low-confidence fallback), which is a real path but a strictly worse one
than a message routing straight to it. Any new-group action file, in
practice, needs at least one real keyword entry.

_validate() no longer leaves a keyword-less tool with zero router coverage:
any tool name with no explicit TOOL_KEYWORDS entry gets a minimal fallback
derived from its own name (see _derive_fallback_keywords() below), at
exactly tool_router.MIN_SCORE so it's real signal without outranking a
hand-picked keyword. This is deliberately a floor, not a fix — a name like
`spotify_search` derives "spotify"/"search", which is far weaker than
purpose-written phrases a person would actually type. discover_actions()
does not hard-reject a keyword-less new group (a tool used only for
testing, or only ever invoked by another tool, is legitimate) but it does
log a note (or, if even the fallback derives nothing, the original loud
warning), because silently-unroutable-by-design and forgot-to-add-keywords
look identical from outside and only the author can tell them apart.

--- actions/*.py vs skills/ ---

An action file adds a CAPABILITY: something that has to run code. If what
you want to add is KNOWLEDGE — how this user wants a recurring task done, a
house style, a checklist, a build convention — that's a skill, not an action.
See skills.py: skills are plain SKILL.md folders under ~/.jarvis/skills/,
need no Python, are editable without a restart, and cost nothing in the
prompt until the model actually loads one. An action file's schemas, by
contrast, are offered (at some tier) on every ask its group is routed to.

Rule of thumb: if you'd write it as instructions, make it a skill; if you'd
write it as a function, make it an action.

Discovery runs once, at jarvis/tools.py import time. Import errors,
validation errors, and name collisions with an existing tool (built-in or
another action file) are logged and that file is skipped — they NEVER
raise out of discover_actions() and never abort the scan of the remaining
files, so one broken action file can't take the rest of Jarvis down.

A handler registered here can optionally accept a second positional
argument, `fn(args, context)` instead of `fn(args)`, to get mid-call
progress emission, round-budget awareness, and conv_id/ui — see
tools.ToolContext and tools._accepts_context. discover_actions() itself
needs no change for this: it only ever stores `tools[name] = fn`, and
tools.execute_tool() introspects the handler's own signature at call
time, whichever registry it came from. See actions/_template.py for the
per-field contract.
"""

import importlib.util
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# Matches every existing jarvis tool name (get_datetime, git_commit_all,
# playnite_list_game_actions, ...) — lowercase snake_case, starts with a
# letter. Anything a JSON-schema-consuming model provider can call as a
# function name should satisfy this; keeping it stricter than
# provider-required (which is usually happy with far more) is deliberate,
# so every tool name — built-in or auto-discovered — reads the same way.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

ACTIONS_DIR = Path(__file__).resolve().parent / "actions"

# §7 fix (master plan): TOOL_KEYWORDS is optional per the contract above, but
# a tool with none of its own is only ever reachable through search_tools's
# low-confidence fallback (or by riding along on a sibling's keyword hit in
# the same group, which credits the trace to that sibling, not this tool —
# see the module docstring section this references). Rather than leave such
# a tool with literally zero router signal, derive a minimal keyword set
# from its own name as a fallback — real, human-picked TOOL_KEYWORDS always
# win; this only fills in for a tool that has none at all.
#
# Generic verbs/fillers carry no distinguishing signal and would false-hit
# on unrelated messages if turned into keywords, so they're filtered before
# any name is used as a keyword. len(word) < 4 already excludes most of the
# shortest generic verbs (get/set/run/add/...); this covers the remaining
# words worth naming explicitly.
_FALLBACK_KEYWORD_STOPWORDS = {
    "list", "make", "does", "with", "from", "into", "this", "that",
    "tool", "tools", "action", "actions", "execute", "call", "show",
    "find", "check", "start", "stop", "current", "info", "data",
    "value", "values", "item", "items", "name", "names", "file", "files",
    "all", "new", "the", "and", "for",
}

# Matches tool_router.MIN_SCORE — the lowest weight that still counts as
# real router signal (see tool_router.py). A fallback keyword is real
# coverage, not a thumb on the scale, so it earns exactly that floor and no
# more: a hand-picked keyword worth more than the minimum still outranks it.
_FALLBACK_KEYWORD_WEIGHT = 5


def _derive_fallback_keywords(tool_name):
    """A minimal {phrase: weight} dict derived from a tool's own name, used
    only when the author gave it no TOOL_KEYWORDS entry of its own. Can
    return {} (e.g. a name made up entirely of stopwords/short words) —
    callers must handle that, it just means no fallback was possible."""
    words = [
        w for w in tool_name.split("_")
        if len(w) >= 4 and w not in _FALLBACK_KEYWORD_STOPWORDS
    ]
    return {w: _FALLBACK_KEYWORD_WEIGHT for w in words}


@dataclass
class ActionModuleRecord:
    """One actions/*.py file's discovery result. `valid=False` means
    `error` explains why and every other field is empty — callers should
    never act on a non-valid record's group/schemas/tools/etc."""

    file: str
    valid: bool = False
    error: str = ""
    group: str = ""
    schemas: list = field(default_factory=list)
    tools: dict = field(default_factory=dict)
    keywords: dict = field(default_factory=dict)
    pack_instruction: str = ""
    confirm_required: set = field(default_factory=set)
    ai_review: set = field(default_factory=set)
    result_specs: dict = field(default_factory=dict)
    personas: list = field(default_factory=list)
    # G.1: already validated and normalised by checklist_schema.extract_supplied
    # ({tool name: entry, "group" filled in}); {} / {} when the module supplied none.
    checklist: dict = field(default_factory=dict)
    checklist_group: dict = field(default_factory=dict)


def _validate(module, filename, logger):
    schemas = getattr(module, "TOOL_SCHEMAS", None)
    tools = getattr(module, "TOOLS", None)
    group = getattr(module, "TOOL_GROUP", None)

    # PERSONAS (see persona_registry.py and actions/_template.py §7) is a
    # second, independent opt-in contract a file can carry — with or
    # without also being a tool file. Validated here (not deferred to
    # tools.py) so a bad persona entry is logged and dropped at the exact
    # same discovery pass a bad tool schema would be, instead of surfacing
    # somewhere unrelated later.
    raw_personas = getattr(module, "PERSONAS", None)
    personas = []
    if raw_personas is not None:
        from . import persona_registry
        base_dir = Path(module.__file__).resolve().parent if getattr(module, "__file__", None) else None
        personas, persona_errors = persona_registry.validate_personas(raw_personas, filename, base_dir)
        for e in persona_errors:
            logger(f"[personas] {e}")
        for p in personas:
            logger(f"[personas] Registered persona {p['id']!r} ({p['name']!r}) from {filename}")

    if schemas is None and tools is None and group is None:
        # No TOOL_SCHEMAS/TOOLS/TOOL_GROUP at all — this isn't an attempted
        # action file, it's a shared helper module (or a stray script) that
        # happens to live in actions/. Silently skip, same as Mark LIII's
        # action_loader treating a missing TOOL dict as "not an action" —
        # UNLESS it registered personas, in which case it's a legitimate
        # persona-only file and the personas still need to reach tools.py's
        # aggregation step (see discover_actions()'s "__not_an_action__"
        # handling below, which now keeps such a record instead of
        # dropping it).
        return ActionModuleRecord(file=filename, error="__not_an_action__", personas=personas)

    if not isinstance(schemas, list) or not schemas:
        return ActionModuleRecord(file=filename, error="TOOL_SCHEMAS must be a non-empty list.")
    if not isinstance(tools, dict) or not tools:
        return ActionModuleRecord(file=filename, error="TOOLS must be a non-empty dict of name -> handler.")
    if not isinstance(group, str) or not group.strip():
        return ActionModuleRecord(
            file=filename,
            error="TOOL_GROUP missing — every action needs a router group (existing or new) to be routable.",
        )
    group = group.strip()

    keywords = getattr(module, "TOOL_KEYWORDS", {}) or {}
    if not isinstance(keywords, dict):
        return ActionModuleRecord(file=filename, error="TOOL_KEYWORDS must be a dict if present.")

    pack_instruction = (getattr(module, "TOOL_PACK_INSTRUCTION", "") or "").strip()
    confirm_required = set(getattr(module, "TOOL_CONFIRM_REQUIRED", set()) or set())
    ai_review = set(getattr(module, "TOOL_AI_REVIEW", set()) or set())
    result_specs = getattr(module, "TOOL_RESULT_SPECS", {}) or {}
    if not isinstance(result_specs, dict):
        return ActionModuleRecord(file=filename, error="TOOL_RESULT_SPECS must be a dict if present.")

    names = set()
    for schema in schemas:
        if not isinstance(schema, dict):
            return ActionModuleRecord(file=filename, error="Every TOOL_SCHEMAS entry must be a dict.")
        name = schema.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return ActionModuleRecord(
                file=filename,
                error=f"Schema name {name!r} missing or doesn't match ^[a-z][a-z0-9_]{{0,63}}$.",
            )
        if not (schema.get("description") or "").strip():
            return ActionModuleRecord(file=filename, error=f"Schema {name!r} has no description.")
        if not isinstance(schema.get("parameters"), dict):
            return ActionModuleRecord(file=filename, error=f"Schema {name!r} has no parameters object.")
        if name not in tools or not callable(tools[name]):
            return ActionModuleRecord(file=filename, error=f"TOOLS[{name!r}] missing or not callable.")
        names.add(name)

    extra = set(tools) - names
    if extra:
        return ActionModuleRecord(
            file=filename,
            error=f"TOOLS has handler(s) with no matching TOOL_SCHEMAS entry: {sorted(extra)}.",
        )

    stray_kw = set(keywords) - names
    if stray_kw:
        return ActionModuleRecord(
            file=filename,
            error=f"TOOL_KEYWORDS has entries for name(s) not in this file's TOOLS: {sorted(stray_kw)}.",
        )

    # §7 fix: fill in a fallback for any tool with no human-authored
    # keywords, instead of leaving it with none at all. Never overrides an
    # explicit (non-empty) entry.
    had_any_explicit = bool(keywords)
    keywords = dict(keywords)
    auto_derived = {}
    for _name in names:
        if keywords.get(_name):
            continue
        derived = _derive_fallback_keywords(_name)
        if derived:
            keywords[_name] = derived
            auto_derived[_name] = derived

    if not keywords:
        # Fallback derivation couldn't help either (every name here is
        # short/generic-only) — this file really is only reachable through
        # search_tools, same as before.
        logger(
            f"[tools] Warning: {filename} (group={group!r}) has no TOOL_KEYWORDS, and "
            "no fallback keywords could be derived from its tool name(s) either — it "
            "will never be reachable through tool_router.route(), only through "
            "search_tools's low-confidence fallback, unless it's sharing an existing "
            "group that already has keyword coverage from another file."
        )
    elif not had_any_explicit:
        logger(
            f"[tools] Note: {filename} (group={group!r}) has no TOOL_KEYWORDS of its "
            f"own — auto-derived fallback keyword(s) from the tool name(s) "
            f"{sorted(auto_derived)} so it isn't completely unroutable, but a few real "
            "TOOL_KEYWORDS entries will match user phrasing far better than the name alone."
        )
    elif auto_derived:
        logger(
            f"[tools] Note: {filename} (group={group!r}): {sorted(auto_derived)} have no "
            "TOOL_KEYWORDS of their own — auto-derived a minimal keyword set from each "
            "name as a fallback."
        )

    # G.1: the module's own Test Checklist entry/entries, if any. Deliberately
    # AFTER every check that can reject the file, and never a reason to reject
    # it: problems are logged, the offending entry is dropped, the tool loads.
    raw_checklist = getattr(module, "TEST_CHECKLIST", None)
    raw_checklist_group = getattr(module, "TEST_CHECKLIST_GROUP", None)
    checklist, checklist_group = {}, {}
    if raw_checklist is not None or raw_checklist_group is not None:
        from . import checklist_schema
        checklist, group_meta, checklist_problems = checklist_schema.extract_supplied(
            raw_checklist, raw_checklist_group, names, group,
        )
        checklist_group = group_meta or {}
        for problem in checklist_problems:
            logger(f"[checklist] {filename}: {problem} — ignored; the tool itself still loads.")

    return ActionModuleRecord(
        file=filename, valid=True, group=group, schemas=schemas, tools=tools,
        keywords=keywords, pack_instruction=pack_instruction,
        confirm_required=confirm_required, ai_review=ai_review, result_specs=result_specs,
        personas=personas, checklist=checklist, checklist_group=checklist_group,
    )


def discover_actions(actions_dir=None, reserved_names=None, logger=print):
    """Scan actions_dir for *.py files (skips files starting with '_', so
    _template.py and any shared _helpers.py are never treated as actions
    themselves). Returns a list of ActionModuleRecord, valid and invalid
    both, in deterministic (sorted-by-filename) order — callers filter on
    .valid. Never raises."""
    actions_dir = actions_dir or ACTIONS_DIR
    reserved = reserved_names or set()

    # This setup step used to run unguarded, which meant a read-only
    # actions_dir (a packaged/site-packages install, a locked-down
    # filesystem, ...) would raise straight out of discover_actions() and
    # take down tools.py's import entirely — every built-in tool along with
    # auto-discovery, not just the feature this guards. That's the opposite
    # of "one broken action file can't take the rest of Jarvis down": this
    # isn't a broken action file, it's the loader's own setup, so it gets
    # the same fault tolerance every action file already gets below —
    # logged and skipped, never fatal. Listing the directory is guarded the
    # same way and for the same reason (a permission error enumerating
    # actions_dir is just as fatal to the import otherwise).
    try:
        actions_dir.mkdir(parents=True, exist_ok=True)
        init_file = actions_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")
        files = sorted(actions_dir.glob("*.py"), key=lambda p: p.name)
    except OSError as e:
        logger(
            f"[tools] Warning: couldn't prepare/list {actions_dir} ({e}) — "
            "auto-discovery skipped this run; built-in tools are unaffected."
        )
        return []

    records = []
    seen_names = {}  # name -> filename that claimed it, this scan only

    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"jarvis.actions.{path.stem}"
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not build import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

            rec = _validate(module, path.name, logger)
            if rec.error == "__not_an_action__":
                # Not a tool file — but if it registered PERSONAS, keep the
                # (invalid-as-a-tool, personas-populated) record so
                # tools.py's aggregation step still sees them. rec.valid
                # stays False, so nothing downstream mistakes this for a
                # real tool file (see tools.py's `if r.valid` filters).
                if rec.personas:
                    records.append(rec)
                continue

            if rec.valid:
                collisions = (set(rec.tools) & reserved) | (set(rec.tools) & set(seen_names))
                if collisions:
                    owners = {n: seen_names.get(n, "a built-in tool") for n in collisions}
                    rec = ActionModuleRecord(
                        file=path.name,
                        error=f"Name(s) already in use, rejected: {owners}.",
                        # Tool names collided, but any personas this same
                        # file registered are independent of that and
                        # shouldn't be thrown away too.
                        personas=rec.personas,
                    )

        except Exception as e:
            rec = ActionModuleRecord(file=path.name, error=f"Failed to load: {e}")
            traceback.print_exc()

        records.append(rec)
        if rec.valid:
            for n in rec.tools:
                seen_names[n] = path.name
            logger(f"[tools] Auto-discovered {sorted(rec.tools)} from {path.name} (group={rec.group!r})")
        else:
            logger(f"[tools] Rejected {path.name}: {rec.error}")

    return records