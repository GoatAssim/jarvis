"""The shape of one Test Checklist entry — defined once, used everywhere.

Menu -> Test Checklist (web console) shows one entry per tool: what it does,
how to test it, what a pass looks like. An entry can now come from TWO places
and both go through the functions below, so "well formed" has one definition:

  1. SHIPPED   web/public/test-checklist-data.js, under "tools". The place for
               every tool that ships with jarvis. tests/test_checklist_coverage.py
               validates it with validate_entry().

  2. SUPPLIED  a tool module's own module-level TEST_CHECKLIST dict (and
               optionally TEST_CHECKLIST_GROUP), next to its TOOL_SCHEMAS /
               TOOLS / TOOL_GROUP. This is the ONLY place a user's own custom
               tool (~/.jarvis/tools/*.py) can ever have an entry, since the
               shipped file is by definition unknown to whoever wrote it.
               tool_loader._validate() and custom_tools_store.validate_source()
               both call extract_supplied() below.

Entry shape (identical in both places — see AGENTS.md > Test Checklist):

    {
      "group":  "files",                 # shipped: required. supplied: optional,
                                         #   defaults to the module's TOOL_GROUP
                                         #   and, if given, must equal it
      "does":   "one line: what it is for",
      "steps":  [ {"ask": "prompt to type into Ask", "expect": "what a pass looks like"},
                  {"run": {"arg": "value"},          "expect": "..."} ],   # >= 1
      "needs":  ["optional prerequisites"],
      "os":     "windows",               # optional
      "care":   "optional side-effect warning",
      "watch":  ["optional gotchas worth checking"],
    }

A malformed SUPPLIED entry is dropped and reported; it never rejects the tool
file it sits in. A checklist entry is documentation about a tool — breaking a
working tool over a typo in its test notes would be the wrong trade — but it
is REPORTED (discovery log, and the Custom Tools editor's Check button) so it
doesn't fail quietly.

Pure functions, no I/O, no imports from the rest of jarvis: safe to import from
tool_loader at discovery time (see actions/_template.py > SAFE IMPORTS).
"""

import json

ENTRY_KEYS = {"group", "does", "steps", "needs", "os", "care", "watch"}
STEP_KEYS = {"ask", "run", "expect"}
GROUP_META_KEYS = {"label", "blurb"}

# Generous ceilings — the shipped catalogue's longest fields are a fraction of
# these. They exist so one runaway module can't turn every GET /api/tools (which
# now carries supplied entries) into a megabyte payload, not to police style.
MAX_DOES = 600
MAX_STEPS = 30
MAX_STEP_TEXT = 1000          # an "ask" prompt or an "expect"
MAX_RUN_JSON = 4000           # a "run" argument set, as compact JSON
MAX_LIST_ITEMS = 20           # needs / watch
MAX_LIST_ITEM_TEXT = 600
MAX_CARE = 1000
MAX_LABEL = 60
MAX_BLURB = 300


def _text(v, limit):
    return isinstance(v, str) and bool(v.strip()) and len(v) <= limit


def validate_entry(name, entry):
    """Problems with one entry, as a list of human-readable strings (empty =
    fine). Does NOT judge `group` — whether a group id exists is a question
    for the caller (the shipped file checks its own "groups" list; a module's
    group is its TOOL_GROUP by construction)."""
    problems = []
    if not isinstance(entry, dict):
        return [f"{name}: entry must be an object, not {type(entry).__name__}"]

    unknown = sorted(set(entry) - ENTRY_KEYS)
    if unknown:
        problems.append(f"{name}: unknown key(s) {unknown} (allowed: {sorted(ENTRY_KEYS)})")

    if not _text(entry.get("does"), MAX_DOES):
        problems.append(f"{name}: 'does' must be a non-empty string of at most {MAX_DOES} characters")

    steps = entry.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append(f"{name}: needs at least one step")
        steps = []
    elif len(steps) > MAX_STEPS:
        problems.append(f"{name}: too many steps ({len(steps)} > {MAX_STEPS})")

    seen = set()
    for i, s in enumerate(steps[:MAX_STEPS], 1):
        if not isinstance(s, dict):
            problems.append(f"{name} step {i}: must be an object")
            continue
        bad = sorted(set(s) - STEP_KEYS)
        if bad:
            problems.append(f"{name} step {i}: unknown key(s) {bad} (allowed: ask, run, expect)")
        has_ask = isinstance(s.get("ask"), str) and bool(s["ask"].strip())
        has_run = isinstance(s.get("run"), dict)
        if has_ask == has_run:
            problems.append(f"{name} step {i}: needs exactly one of 'ask' (text) or 'run' (object)")
        if has_ask and len(s["ask"]) > MAX_STEP_TEXT:
            problems.append(f"{name} step {i}: 'ask' is longer than {MAX_STEP_TEXT} characters")
        if has_run:
            try:
                run_json = json.dumps(s["run"], sort_keys=True)
            except (TypeError, ValueError, RecursionError):
                problems.append(f"{name} step {i}: 'run' must be plain JSON (strings, numbers, booleans, lists, objects)")
                run_json = None
            if run_json is not None and len(run_json) > MAX_RUN_JSON:
                problems.append(f"{name} step {i}: 'run' is longer than {MAX_RUN_JSON} characters")
        if not _text(s.get("expect"), MAX_STEP_TEXT):
            problems.append(f"{name} step {i}: missing 'expect' (a non-empty string of at most {MAX_STEP_TEXT} characters)")
        # Ticks are keyed by the step's text, so two identical steps would tick together.
        if has_ask != has_run:
            key = s["ask"] if has_ask else json.dumps(s.get("run"), sort_keys=True, default=str)
            if key in seen:
                problems.append(f"{name}: duplicate step {key!r}")
            seen.add(key)

    for key in ("needs", "watch"):
        if key not in entry:
            continue
        v = entry[key]
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            problems.append(f"{name}: '{key}' must be a list of strings")
        elif len(v) > MAX_LIST_ITEMS or any(len(x) > MAX_LIST_ITEM_TEXT for x in v):
            problems.append(f"{name}: '{key}' has more than {MAX_LIST_ITEMS} items or an item over {MAX_LIST_ITEM_TEXT} characters")

    for key, limit in (("os", 40), ("care", MAX_CARE)):
        if key in entry and not (isinstance(entry[key], str) and len(entry[key]) <= limit):
            problems.append(f"{name}: '{key}' must be a string of at most {limit} characters")

    if "group" in entry and not (isinstance(entry["group"], str) and entry["group"].strip()):
        problems.append(f"{name}: 'group' must be a non-empty string")

    return problems


def validate_group_meta(meta):
    """Problems with a TEST_CHECKLIST_GROUP value: {"label": ..., "blurb": ...}."""
    if not isinstance(meta, dict):
        return [f"TEST_CHECKLIST_GROUP must be a dict, not {type(meta).__name__}"]
    problems = []
    unknown = sorted(set(meta) - GROUP_META_KEYS)
    if unknown:
        problems.append(f"TEST_CHECKLIST_GROUP: unknown key(s) {unknown} (allowed: label, blurb)")
    if not _text(meta.get("label"), MAX_LABEL):
        problems.append(f"TEST_CHECKLIST_GROUP: 'label' must be a non-empty string of at most {MAX_LABEL} characters")
    if "blurb" in meta and not (isinstance(meta["blurb"], str) and len(meta["blurb"]) <= MAX_BLURB):
        problems.append(f"TEST_CHECKLIST_GROUP: 'blurb' must be a string of at most {MAX_BLURB} characters")
    return problems


def extract_supplied(raw_entries, raw_group_meta, tool_names, group):
    """Turn a module's TEST_CHECKLIST / TEST_CHECKLIST_GROUP into clean data.

    raw_entries / raw_group_meta: the module attributes, or None when absent.
    tool_names: the names in the module's TOOLS. group: its TOOL_GROUP.

    Returns (entries, group_meta, problems):
      entries     {tool name: entry}, each a fresh plain-JSON copy with "group"
                  filled in — so it is safe to json.dumps into `jarvis
                  tools-list` no matter what the module put in it
      group_meta  {"label", "blurb"} or None
      problems    strings, one per thing that was dropped and why

    Never raises."""
    entries, problems, group_meta = {}, [], None

    if raw_entries is not None:
        if not isinstance(raw_entries, dict):
            problems.append(f"TEST_CHECKLIST must be a dict of tool name -> entry, not {type(raw_entries).__name__}")
        else:
            for name, entry in raw_entries.items():
                if not isinstance(name, str) or name not in tool_names:
                    problems.append(f"TEST_CHECKLIST has an entry for {name!r}, which is not one of this file's tools ({sorted(tool_names)})")
                    continue
                if isinstance(entry, dict) and "group" in entry and entry["group"] != group:
                    problems.append(
                        f"{name}: 'group' is {entry['group']!r} but this file's TOOL_GROUP is {group!r} — "
                        "leave 'group' out and it follows TOOL_GROUP"
                    )
                    continue
                found = validate_entry(name, entry)
                if found:
                    problems.extend(found)
                    continue
                try:
                    clean = json.loads(json.dumps(entry))
                except (TypeError, ValueError, RecursionError):
                    problems.append(f"{name}: entry must be plain JSON")
                    continue
                clean["group"] = group
                entries[name] = clean

    if raw_group_meta is not None:
        found = validate_group_meta(raw_group_meta)
        if found:
            problems.extend(found)
        else:
            group_meta = {"label": raw_group_meta["label"].strip(), "blurb": (raw_group_meta.get("blurb") or "").strip()}

    return entries, group_meta, problems
