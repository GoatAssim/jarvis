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

               TEST_CHECKLIST_GROUP itself has two shapes (master plan G.2):
               the plain {"label", "blurb"} shape names ONE section, tied to
               the module's own TOOL_GROUP (the original, still-default
               shape); a {group_id: {"label", "blurb"}, ...} shape — a dict
               whose keys are NOT a subset of {"label", "blurb"} — names as
               many sections as the module needs, each an id an entry's
               "group" can point at (in addition to TOOL_GROUP itself, which
               is always a valid target with or without its own meta). See
               extract_supplied()'s docstring below for exactly how each
               shape is told apart and what it resolves to.

Entry shape (identical in both places — see AGENTS.md > Test Checklist):

    {
      "group":  "files",                 # shipped: required. supplied: optional,
                                         #   defaults to the module's TOOL_GROUP
                                         #   and, if given, must be TOOL_GROUP or
                                         #   one of the module's own declared
                                         #   TEST_CHECKLIST_GROUP ids (G.2)
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
is REPORTED (discovery log, and the Tool Manager editor's Validate button) so it
doesn't fail quietly.

FEATURE ENTRIES (master plan L.19)
----------------------------------
Not everything worth a manual pass is a tool: the `/` command palette is a piece
of UI. The shipped data file therefore has a second top-level object,
"features", next to "tools" -- same entry shape, validated by the same function
(`allow_do=True`), with two differences: its entries sit in a group whose
"kind" is "feature", and its steps may be {"do": "...", "expect": "..."} -- a
manual instruction to carry out in the page ("type / in an empty composer") --
in addition to ask / run. `do` is for features only; a tool's own TEST_CHECKLIST
(extract_supplied below) never allows it. Features are not tools: the
tool-coverage tests ignore them, and nothing fakes a tool to hold one.

PANEL STEPS (Menu / Settings targets)
-----------------------------------
The shipped file's top-level "panels" maps an id to a Menu item or a Settings
section. A step {"open": "<panel id>", "expect": ...} in a SHIPPED entry (tool or
feature) opens it from the checklist. validate_entry(allow_open=True, panels=...)
accepts these; a tool module's TEST_CHECKLIST never does. validate_panels() checks
the map itself.

Pure functions, no I/O, no imports from the rest of jarvis: safe to import from
tool_loader at discovery time (see actions/_template.py > SAFE IMPORTS).
"""

import json
import re

ENTRY_KEYS = {"group", "does", "steps", "needs", "os", "care", "watch"}
STEP_KEYS = {"ask", "run", "expect"}
FEATURE_STEP_KEYS = STEP_KEYS | {"do"}      # L.19: features only
GROUP_META_KEYS = {"label", "blurb"}
# Panel targets (the shipped file's top-level "panels"): a step {"open": "<panel id>"}
# opens a Menu panel or a Settings section. Shipped data only -- a tool module never gets one.
PANEL_KEYS = {"label", "menu", "settings", "tab"}
PANEL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_OPEN_ID = 60

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


def validate_entry(name, entry, allow_do=False, allow_open=False, panels=None):
    """Problems with one entry, as a list of human-readable strings (empty =
    fine). Does NOT judge `group` — whether a group id exists is a question
    for the caller (the shipped file checks its own \"groups\" list; a module's
    group is its TOOL_GROUP by construction).

    allow_do=True (L.19, the shipped file's \"features\" only) additionally
    accepts {\"do\": \"<manual instruction>\", \"expect\": ...} steps.
    allow_open=True (shipped file only) additionally accepts
    {\"open\": \"<panel id>\", \"expect\": ...} steps. If `panels` (the file's
    \"panels\" object) is given, an open step must name one of its keys.
    A step then needs exactly one of its action keys (ask / run / do / open)."""
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

    allowed_keys = set(STEP_KEYS)
    if allow_do:
        allowed_keys.add("do")
    if allow_open:
        allowed_keys.add("open")
    known_panels = panels if isinstance(panels, dict) else None

    seen = set()
    for i, s in enumerate(steps[:MAX_STEPS], 1):
        if not isinstance(s, dict):
            problems.append(f"{name} step {i}: must be an object")
            continue
        bad = sorted(set(s) - allowed_keys)
        if bad:
            problems.append(f"{name} step {i}: unknown key(s) {bad} (allowed: {', '.join(sorted(allowed_keys))})")
        has_ask = isinstance(s.get("ask"), str) and bool(s["ask"].strip())
        has_run = isinstance(s.get("run"), dict)
        has_do = allow_do and isinstance(s.get("do"), str) and bool(s["do"].strip())
        has_open = allow_open and isinstance(s.get("open"), str) and bool(s["open"].strip())
        count = has_ask + has_run + has_do + has_open
        if count != 1:
            problems.append(
                f"{name} step {i}: needs exactly one of 'ask' (text), 'run' (object)"
                + (" or 'do' (text)" if allow_do else "")
                + (" or 'open' (a panel id)" if allow_open else "")
            )
        if has_ask and len(s["ask"]) > MAX_STEP_TEXT:
            problems.append(f"{name} step {i}: 'ask' is longer than {MAX_STEP_TEXT} characters")
        if has_do and len(s["do"]) > MAX_STEP_TEXT:
            problems.append(f"{name} step {i}: 'do' is longer than {MAX_STEP_TEXT} characters")
        if has_open:
            if len(s["open"]) > MAX_OPEN_ID:
                problems.append(f"{name} step {i}: 'open' is longer than {MAX_OPEN_ID} characters")
            elif known_panels is not None and s["open"] not in known_panels:
                problems.append(f"{name} step {i}: 'open' names {s['open']!r}, which is not a panel in the file's \"panels\"")
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
        if count == 1:
            if has_ask:
                key = s["ask"]
            elif has_do:
                key = s["do"]
            elif has_open:
                key = "open:" + s["open"]
            else:
                key = json.dumps(s.get("run"), sort_keys=True, default=str)
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


def validate_panels(panels, menu_ids=None, settings_sections=None, advanced_tabs=None):
    """Problems with the shipped file's top-level \"panels\" object: panel id ->
    {\"label\": ..., \"menu\": <Menu item DOM id>}  or  {\"label\": ...,
    \"settings\": <Settings section id>, \"tab\": <Advanced tab, optional>}.

    Exactly one of \"menu\" / \"settings\". If menu_ids / settings_sections /
    advanced_tabs are given, the target must be one of them (the caller reads
    index.html and settings.js for those, so this module stays pure)."""
    if not isinstance(panels, dict) or not panels:
        return ["'panels' must be a non-empty object of panel id -> target"]
    problems = []
    for pid, p in panels.items():
        if not isinstance(pid, str) or not PANEL_ID_RE.match(pid):
            problems.append(f"panel {pid!r}: id must be lowercase letters, digits and dashes, starting with a letter")
            continue
        if not isinstance(p, dict):
            problems.append(f"panel {pid!r}: must be an object, not {type(p).__name__}")
            continue
        unknown = sorted(set(p) - PANEL_KEYS)
        if unknown:
            problems.append(f"panel {pid!r}: unknown key(s) {unknown} (allowed: {sorted(PANEL_KEYS)})")
        if not _text(p.get("label"), MAX_LABEL):
            problems.append(f"panel {pid!r}: 'label' must be a non-empty string of at most {MAX_LABEL} characters")
        has_menu, has_settings = "menu" in p, "settings" in p
        if has_menu == has_settings:
            problems.append(f"panel {pid!r}: needs exactly one of 'menu' (a Menu item id) or 'settings' (a Settings section id)")
            continue
        if has_menu:
            if not _text(p["menu"], 80):
                problems.append(f"panel {pid!r}: 'menu' must be a non-empty string")
            elif menu_ids is not None and p["menu"] not in menu_ids:
                problems.append(f"panel {pid!r}: no Menu item with id {p['menu']!r} in index.html")
            if "tab" in p:
                problems.append(f"panel {pid!r}: 'tab' only goes with 'settings'")
        else:
            if not _text(p["settings"], 40):
                problems.append(f"panel {pid!r}: 'settings' must be a non-empty string")
            elif settings_sections is not None and p["settings"] not in settings_sections:
                problems.append(f"panel {pid!r}: no Settings section {p['settings']!r}")
            if "tab" in p:
                if p["settings"] != "advanced":
                    problems.append(f"panel {pid!r}: 'tab' only applies to the 'advanced' Settings section")
                elif not _text(p["tab"], 40):
                    problems.append(f"panel {pid!r}: 'tab' must be a non-empty string")
                elif advanced_tabs is not None and p["tab"] not in advanced_tabs:
                    problems.append(f"panel {pid!r}: no Advanced tab {p['tab']!r}")
    return problems


def validate_group_meta(meta, label="TEST_CHECKLIST_GROUP"):
    """Problems with ONE group's meta: {"label": ..., "blurb": ...}. Used for
    both the single-group TEST_CHECKLIST_GROUP shape and each value of the
    multi-group shape (see is_multi_group_shape() below) — `label` names the
    thing being checked in each problem string, so a multi-group caller can
    pass e.g. "TEST_CHECKLIST_GROUP['browser_read']" and get a message that
    points at the right entry instead of a bare "TEST_CHECKLIST_GROUP"."""
    if not isinstance(meta, dict):
        return [f"{label} must be a dict, not {type(meta).__name__}"]
    problems = []
    unknown = sorted(set(meta) - GROUP_META_KEYS)
    if unknown:
        problems.append(f"{label}: unknown key(s) {unknown} (allowed: label, blurb)")
    if not _text(meta.get("label"), MAX_LABEL):
        problems.append(f"{label}: 'label' must be a non-empty string of at most {MAX_LABEL} characters")
    if "blurb" in meta and not (isinstance(meta["blurb"], str) and len(meta["blurb"]) <= MAX_BLURB):
        problems.append(f"{label}: 'blurb' must be a string of at most {MAX_BLURB} characters")
    return problems


def is_multi_group_shape(raw_group_meta):
    """True if a TEST_CHECKLIST_GROUP value is the multi-group shape (G.2):
    a dict of {group_id: {"label", "blurb"}, ...} rather than one group's own
    {"label", "blurb"}. Told apart by key set alone, no separate marker: the
    single-group shape's keys are always a subset of {"label", "blurb"}, and
    a real group id is never exactly one of those two words — a module with
    a legitimate group literally called "label" or "blurb" is not a shape
    this schema can support, and is asked (via the problem this produces
    downstream) to rename it. An empty dict counts as single-group (nothing
    to tell it apart by), so `TEST_CHECKLIST_GROUP = {}` behaves the way it
    always has: dropped by validate_group_meta for a missing 'label'."""
    return isinstance(raw_group_meta, dict) and bool(raw_group_meta) and not set(raw_group_meta).issubset(GROUP_META_KEYS)


def extract_supplied(raw_entries, raw_group_meta, tool_names, group):
    """Turn a module's TEST_CHECKLIST / TEST_CHECKLIST_GROUP into clean data.

    raw_entries: the module's TEST_CHECKLIST, or None when absent.
    raw_group_meta: the module's TEST_CHECKLIST_GROUP, or None when absent.
                    Either the single-group shape {"label", "blurb"} (naming
                    TOOL_GROUP's own section) or the multi-group shape
                    {group_id: {"label", "blurb"}, ...} (naming one or more
                    of the module's OWN sections, distinct from TOOL_GROUP) —
                    see is_multi_group_shape() above for how they're told
                    apart. A module needs the multi-group shape only when its
                    tools split across more than one logical category; the
                    common case (one module, one category) keeps using the
                    single-group shape exactly as before.
    tool_names: the names in the module's TOOLS. group: its TOOL_GROUP.

    Returns (entries, group_meta, problems):
      entries     {tool name: entry}, each a fresh plain-JSON copy with "group"
                  filled in (defaulting to TOOL_GROUP, or the entry's own
                  explicit "group" once validated) — so it is safe to
                  json.dumps into `jarvis tools-list` no matter what the
                  module put in it
      group_meta  {group id: {"label", "blurb"}, ...} for every group the
                  module named meta for (one entry under the single-group
                  shape, keyed by TOOL_GROUP; one per id under the
                  multi-group shape), or {} when the module supplied none —
                  never None, so callers can always .items() it
      problems    strings, one per thing that was dropped and why

    Never raises."""
    entries, problems, group_meta = {}, [], {}

    # Valid targets for an entry's own explicit "group": TOOL_GROUP itself is
    # always allowed (with or without its own meta — this is unchanged from
    # before G.2), plus whatever multi-group ids the module declared meta
    # for. A module doesn't have to declare meta for a group id to use it on
    # an entry — TEST_CHECKLIST_GROUP is a place to NAME a section, not a
    # required registry — but declaring meta for an id the module's entries
    # never use is harmless and not itself a problem.
    declared_ids = set()

    if raw_group_meta is not None:
        if is_multi_group_shape(raw_group_meta):
            for gid, meta in raw_group_meta.items():
                if not isinstance(gid, str) or not gid.strip():
                    problems.append(f"TEST_CHECKLIST_GROUP has a non-string or empty group id {gid!r}")
                    continue
                found = validate_group_meta(meta, label=f"TEST_CHECKLIST_GROUP[{gid!r}]")
                if found:
                    problems.extend(found)
                    continue
                group_meta[gid] = {"label": meta["label"].strip(), "blurb": (meta.get("blurb") or "").strip()}
                declared_ids.add(gid)
        else:
            found = validate_group_meta(raw_group_meta)
            if found:
                problems.extend(found)
            else:
                group_meta[group] = {"label": raw_group_meta["label"].strip(), "blurb": (raw_group_meta.get("blurb") or "").strip()}

    valid_group_ids = {group} | declared_ids

    if raw_entries is not None:
        if not isinstance(raw_entries, dict):
            problems.append(f"TEST_CHECKLIST must be a dict of tool name -> entry, not {type(raw_entries).__name__}")
        else:
            for name, entry in raw_entries.items():
                if not isinstance(name, str) or name not in tool_names:
                    problems.append(f"TEST_CHECKLIST has an entry for {name!r}, which is not one of this file's tools ({sorted(tool_names)})")
                    continue
                entry_group = group
                if isinstance(entry, dict) and "group" in entry:
                    entry_group = entry["group"]
                    # isinstance-gated before the set membership check: entry_group
                    # is untrusted module data and could be unhashable (a list, a
                    # dict), which `in` on a set would raise on rather than just
                    # report as a mismatch — never allowed to happen here.
                    if not (isinstance(entry_group, str) and entry_group in valid_group_ids):
                        problems.append(
                            f"{name}: 'group' is {entry_group!r} but this file's TOOL_GROUP is {group!r} and its own "
                            f"TEST_CHECKLIST_GROUP declares {sorted(declared_ids)!r} — 'group' must be one of those, "
                            "or left out to follow TOOL_GROUP"
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
                clean["group"] = entry_group
                entries[name] = clean

    return entries, group_meta, problems
