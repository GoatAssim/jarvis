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

See actions/_template.py for the full contract, one field at a time,
including why TOOL_KEYWORDS matters for routing (the section called out
below) and how confirm-gating is supposed to work.

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
practice, needs at least one real keyword entry. discover_actions() does
not hard-reject a keyword-less new group (a tool used only for testing, or
only ever invoked by another tool, is legitimate) but it does log a loud
warning, because silently-unroutable-by-design and
forgot-to-add-keywords look identical from outside and only the author
can tell them apart.

Discovery runs once, at jarvis/tools.py import time. Import errors,
validation errors, and name collisions with an existing tool (built-in or
another action file) are logged and that file is skipped — they NEVER
raise out of discover_actions() and never abort the scan of the remaining
files, so one broken action file can't take the rest of Jarvis down.
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


def _validate(module, filename, logger):
    schemas = getattr(module, "TOOL_SCHEMAS", None)
    tools = getattr(module, "TOOLS", None)
    group = getattr(module, "TOOL_GROUP", None)

    if schemas is None and tools is None and group is None:
        # No TOOL_SCHEMAS/TOOLS/TOOL_GROUP at all — this isn't an attempted
        # action file, it's a shared helper module (or a stray script) that
        # happens to live in actions/. Silently skip, same as Mark LIII's
        # action_loader treating a missing TOOL dict as "not an action".
        return ActionModuleRecord(file=filename, error="__not_an_action__")

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

    if not keywords:
        logger(
            f"[tools] Warning: {filename} (group={group!r}) has no TOOL_KEYWORDS — "
            "it will never be reachable through tool_router.route(), only through "
            "search_tools's low-confidence fallback, unless it's sharing an existing "
            "group that already has keyword coverage from another file."
        )

    return ActionModuleRecord(
        file=filename, valid=True, group=group, schemas=schemas, tools=tools,
        keywords=keywords, pack_instruction=pack_instruction,
        confirm_required=confirm_required, ai_review=ai_review, result_specs=result_specs,
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
                continue

            if rec.valid:
                collisions = (set(rec.tools) & reserved) | (set(rec.tools) & set(seen_names))
                if collisions:
                    owners = {n: seen_names.get(n, "a built-in tool") for n in collisions}
                    rec = ActionModuleRecord(
                        file=path.name,
                        error=f"Name(s) already in use, rejected: {owners}.",
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
