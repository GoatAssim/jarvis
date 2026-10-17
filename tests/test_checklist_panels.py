"""Test Checklist "open" steps and the "panels" map -- a checklist entry can take
you to a Menu panel or a Settings section.

web/public/test-checklist-data.js has a top-level "panels" object: id -> either
{"label", "menu": <Menu item DOM id>} or {"label", "settings": <Settings section>,
"tab": <Advanced tab, optional>}. A shipped entry (tool or feature) can have a step
{"open": "<panel id>", "expect": "..."} with an Open button in the panel.

Covers: the map is well formed, and every menu id / Settings section / Advanced
tab it names really exists in web/public/index.html and settings.js (so a renamed
Menu item breaks this test rather than a tester's Open button); every open step
names a panel in the map; open steps are refused on a tool module's own
TEST_CHECKLIST (only shipped entries may open a panel); the panel script has the
OPEN renderer.

    python3 tests/test_checklist_panels.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Redirect HOME BEFORE importing any jarvis module (AGENTS.md > Testing).
_HOME = tempfile.mkdtemp(prefix="jarvis-panels-test-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

DATA_FILE = ROOT / "web" / "public" / "test-checklist-data.js"
INDEX_HTML = ROOT / "web" / "public" / "index.html"
SETTINGS_JS = ROOT / "web" / "public" / "settings.js"
PANEL_JS = ROOT / "web" / "public" / "test-checklist.js"


def _load():
    text = DATA_FILE.read_text(encoding="utf-8")
    m = re.search(r"/\*JSON-BEGIN\*/(.*)/\*JSON-END\*/", text, re.S)
    assert m, "test-checklist-data.js lost its /*JSON-BEGIN*/ ... /*JSON-END*/ markers"
    return json.loads(m.group(1))


def _menu_ids():
    return set(re.findall(r'id="(menu-item-[a-z0-9-]+)"', INDEX_HTML.read_text(encoding="utf-8")))


def _settings_sections():
    return set(re.findall(r'\{ id: "([a-z]+)", label:', SETTINGS_JS.read_text(encoding="utf-8")))


def _advanced_tabs():
    m = re.search(r"const ADV_TABS = \[([^\]]*)\]", SETTINGS_JS.read_text(encoding="utf-8"))
    assert m, "settings.js lost its ADV_TABS list"
    return {t.strip().strip('"') for t in m.group(1).split(",") if t.strip()}


def test_panels_map_is_well_formed_and_points_at_real_ui():
    from jarvis import checklist_schema
    panels = _load().get("panels")
    problems = checklist_schema.validate_panels(
        panels, menu_ids=_menu_ids(), settings_sections=_settings_sections(), advanced_tabs=_advanced_tabs())
    assert not problems, "\n".join(problems)


def test_every_open_step_names_a_real_panel():
    from jarvis import checklist_schema
    data = _load()
    panels = data["panels"]
    problems = []
    for bucket in ("tools", "features"):
        for name, e in data[bucket].items():
            for i, s in enumerate(e["steps"], 1):
                if "open" in s and s["open"] not in panels:
                    problems.append(f"{bucket} {name} step {i}: open {s['open']!r} is not in \"panels\"")
            problems.extend(checklist_schema.validate_entry(
                name, e, allow_do=(bucket == "features"), allow_open=True, panels=panels))
    assert not problems, "\n".join(problems)


def test_open_steps_are_refused_unless_allowed():
    from jarvis import checklist_schema
    entry = {"group": "g", "does": "d", "steps": [{"open": "calendar", "expect": "opens"}]}
    assert checklist_schema.validate_entry("t", entry), "an open step on a tool must be refused by default"
    assert not checklist_schema.validate_entry("t", entry, allow_open=True), "allow_open must accept it"
    # the panel check: an id not in the map is refused when a map is given
    bad = checklist_schema.validate_entry("t", entry, allow_open=True, panels={"debug": {"label": "Debug", "menu": "menu-item-debug"}})
    assert any("not a panel" in p for p in bad), bad
    # one action per step: open + ask together is refused
    both = {"group": "g", "does": "d", "steps": [{"open": "calendar", "ask": "a", "expect": "e"}]}
    assert any("exactly one" in p for p in checklist_schema.validate_entry("t", both, allow_open=True)), "open + ask in one step must be refused"


def test_a_tool_module_cannot_open_a_panel():
    from jarvis import checklist_schema
    raw = {"my_tool": {"does": "d", "steps": [{"open": "calendar", "expect": "opens"}]}}
    entries, _meta, problems = checklist_schema.extract_supplied(raw, None, {"my_tool"}, "custom")
    assert "my_tool" not in entries, "a module's entry with an open step must be dropped"
    assert problems, "and reported"


def test_panel_map_rejects_a_target_that_does_not_exist():
    from jarvis import checklist_schema
    bad = {
        "a": {"label": "A", "menu": "menu-item-nope"},
        "b": {"label": "B", "settings": "nosuch"},
        "c": {"label": "C", "menu": "menu-item-debug", "settings": "advanced"},
        "d": {"label": "D", "settings": "tools", "tab": "files"},
        "e": {"label": "E", "settings": "advanced", "tab": "nosuchtab"},
    }
    problems = checklist_schema.validate_panels(
        bad, menu_ids=_menu_ids(), settings_sections=_settings_sections(), advanced_tabs=_advanced_tabs())
    assert len(problems) == 5, problems


def test_js_panel_has_the_open_renderer():
    text = PANEL_JS.read_text(encoding="utf-8")
    assert 'typeof step.open === "string"' in text, "test-checklist.js must recognise open steps"
    assert "function openPanel(" in text, "test-checklist.js must define openPanel"
    if subprocess.run(["node", "--version"], capture_output=True).returncode == 0:
        r = subprocess.run(["node", "--check", str(PANEL_JS)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-800:]


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
            print(f"FAIL {n}: {e}")
    sys.exit(1 if failed else 0)
