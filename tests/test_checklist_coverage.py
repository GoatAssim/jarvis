"""The Test Checklist catalogue must cover every tool, and only real tools.

web/public/test-checklist-data.js is what Menu -> Test Checklist shows. AGENTS.md
says an agent that adds a tool updates its entry in the same change; this is
the check that makes that visible before a tester finds a bare "NO CHECKLIST"
card. Nothing here touches ~/.jarvis for real (HOME is redirected first), and
the test only reads the data file — the checklist itself stays front end only.
"""

import json
import re
import sys
import tempfile
import os
from pathlib import Path

# Redirect HOME BEFORE importing any jarvis module (see AGENTS.md > Testing):
# tool discovery resolves ~/.jarvis at import time, and a developer's own
# custom tools must not count as "built-in tools missing a checklist entry".
_HOME = tempfile.mkdtemp(prefix="jarvis-checklist-test-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

DATA_FILE = ROOT / "web" / "public" / "test-checklist-data.js"


def _load():
    text = DATA_FILE.read_text(encoding="utf-8")
    m = re.search(r"/\*JSON-BEGIN\*/(.*)/\*JSON-END\*/", text, re.S)
    assert m, "test-checklist-data.js lost its /*JSON-BEGIN*/ ... /*JSON-END*/ markers"
    return json.loads(m.group(1))


def _live_tool_names():
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from jarvis import tools
    return {s["name"] for s in tools.TOOL_SCHEMAS if s.get("name")}


def test_every_tool_has_a_checklist_entry():
    have = set(_load()["tools"])
    missing = sorted(_live_tool_names() - have)
    assert not missing, (
        "Tools with no entry in web/public/test-checklist-data.js: "
        + ", ".join(missing)
        + " -- add one (see AGENTS.md > Test Checklist)."
    )


def test_no_entry_for_a_tool_that_does_not_exist():
    extra = sorted(set(_load()["tools"]) - _live_tool_names())
    assert not extra, (
        "Checklist entries with no matching tool (renamed or removed?): "
        + ", ".join(extra)
    )


def test_entries_are_well_formed():
    data = _load()
    group_ids = {g["id"] for g in data["groups"]}
    problems = []
    for name, e in data["tools"].items():
        if e.get("group") not in group_ids:
            problems.append(f"{name}: unknown group {e.get('group')!r}")
        if not str(e.get("does", "")).strip():
            problems.append(f"{name}: empty 'does'")
        steps = e.get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append(f"{name}: needs at least one step")
            continue
        for i, s in enumerate(steps, 1):
            has_ask = isinstance(s.get("ask"), str) and s["ask"].strip()
            has_run = isinstance(s.get("run"), dict)
            if has_ask == bool(has_run) or (has_ask and has_run):
                problems.append(f"{name} step {i}: needs exactly one of 'ask' (text) or 'run' (object)")
            if not str(s.get("expect", "")).strip():
                problems.append(f"{name} step {i}: missing 'expect'")
        for key in ("needs", "watch"):
            if key in e and not (isinstance(e[key], list) and all(isinstance(x, str) for x in e[key])):
                problems.append(f"{name}: '{key}' must be a list of strings")
    assert not problems, "\n".join(problems)


def test_tool_groups_agree_with_the_registry():
    from jarvis import tool_registry
    data = _load()
    wrong = [
        f"{n}: checklist says {e['group']!r}, registry says {tool_registry.group_of(n)!r}"
        for n, e in data["tools"].items()
        if tool_registry.group_of(n) not in (None, e["group"])
    ]
    assert not wrong, "\n".join(wrong)


def test_a_step_is_not_duplicated_within_a_tool():
    # Ticks are keyed by the step's text, so two identical steps would tick together.
    dupes = []
    for name, e in _load()["tools"].items():
        seen = set()
        for s in e["steps"]:
            k = s.get("ask") or json.dumps(s.get("run"), sort_keys=True)
            if k in seen:
                dupes.append(f"{name}: duplicate step {k!r}")
            seen.add(k)
    assert not dupes, "\n".join(dupes)


# --- runner: keep test functions ABOVE this block (AGENTS.md > Testing) ---
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in fns:
        try:
            f()
            print(f"ok   {n}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {n}\n{e}")
    print(f"{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
