"""L.19 -- Test Checklist entries for things that are not tools (the `/` palette).

web/public/test-checklist-data.js has a top-level "features" object next to
"tools". Covers: every feature entry is well formed under the same validator
(allow_do=True) and sits in a group whose kind is "feature"; a `do` step is
accepted for features and refused everywhere else; a step has exactly one of
ask / run / do; feature ids never collide with a tool entry or a live tool name;
the tool-coverage rules are not widened to features; the panel (JS, under node,
skipped if node is missing) folds features into its catalogue, reports them
apart from the tools, never mutates the shipped data, and still shows them with
no live catalogue at all.

    python3 tests/test_checklist_features.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Redirect HOME BEFORE importing any jarvis module (AGENTS.md > Testing).
_HOME = tempfile.mkdtemp(prefix="jarvis-features-test-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

DATA_FILE = ROOT / "web" / "public" / "test-checklist-data.js"
PANEL_JS = ROOT / "web" / "public" / "test-checklist.js"
PALETTE_DOC = ROOT / "DOCUMENTATION" / "COMMAND_PALETTE_TESTING.md"


def _load():
    text = DATA_FILE.read_text(encoding="utf-8")
    m = re.search(r"/\*JSON-BEGIN\*/(.*)/\*JSON-END\*/", text, re.S)
    assert m, "test-checklist-data.js lost its /*JSON-BEGIN*/ ... /*JSON-END*/ markers"
    return json.loads(m.group(1))


def _live_tool_names():
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from jarvis import tools
    return {s["name"] for s in tools.TOOL_SCHEMAS if s.get("name")}


def test_features_exist_and_are_well_formed():
    from jarvis import checklist_schema
    data = _load()
    features = data.get("features")
    assert isinstance(features, dict) and features, "the shipped file has no \"features\" object"
    groups = {g["id"]: g for g in data["groups"]}
    problems = []
    for name, e in features.items():
        g = groups.get(e.get("group"))
        if g is None:
            problems.append(f"{name}: unknown group {e.get('group')!r}")
        elif g.get("kind") != "feature":
            problems.append(f"{name}: group {e['group']!r} is not a feature group (kind != 'feature')")
        problems.extend(checklist_schema.validate_entry(name, e, allow_do=True))
    assert not problems, "\n".join(problems)


def test_every_feature_group_has_entries_and_tools_never_use_one():
    data = _load()
    feature_groups = {g["id"] for g in data["groups"] if g.get("kind") == "feature"}
    assert "command_palette" in feature_groups
    used = {e["group"] for e in data["features"].values()}
    assert feature_groups <= used, f"feature group(s) with no entry: {sorted(feature_groups - used)}"
    leaked = [n for n, e in data["tools"].items() if e.get("group") in feature_groups]
    assert not leaked, f"tool entries filed under a feature group: {leaked}"


def test_do_steps_are_for_features_only():
    from jarvis import checklist_schema
    entry = {"group": "g", "does": "d", "steps": [{"do": "type / in the box", "expect": "palette opens"}]}
    assert not checklist_schema.validate_entry("f", entry, allow_do=True)
    refused = checklist_schema.validate_entry("t", entry)          # default: tools
    assert refused, "a tool entry with a `do` step must be refused"
    # A module's own TEST_CHECKLIST goes through extract_supplied: never `do` there either.
    both = {"group": "g", "does": "d", "steps": [{"ask": "a", "do": "b", "expect": "e"}]}
    assert checklist_schema.validate_entry("f", both, allow_do=True), "ask + do in one step must be refused"
    none = {"group": "g", "does": "d", "steps": [{"expect": "e"}]}
    assert checklist_schema.validate_entry("f", none, allow_do=True), "a step with no action must be refused"
    # and the tool rules did not loosen
    ok = {"group": "g", "does": "d", "steps": [{"ask": "a", "expect": "e"}, {"run": {}, "expect": "e"}]}
    assert not checklist_schema.validate_entry("t", ok)


def test_feature_ids_do_not_collide_with_tools():
    data = _load()
    clash = sorted(set(data["features"]) & (set(data["tools"]) | _live_tool_names()))
    assert not clash, f"feature id(s) that are also tool names: {clash}"


def test_steps_are_not_duplicated_within_a_feature():
    dupes = []
    for name, e in _load()["features"].items():
        seen = set()
        for s in e["steps"]:
            k = s.get("do") or s.get("ask") or json.dumps(s.get("run"), sort_keys=True)
            if k in seen:
                dupes.append(f"{name}: duplicate step {k!r}")
            seen.add(k)
    assert not dupes, "\n".join(dupes)


def test_the_palette_group_covers_the_documented_behaviours():
    f = _load()["features"]
    expected = {"palette_open", "palette_search", "palette_open_command", "palette_switch",
                "palette_close_reopen", "palette_click_behaviour", "palette_busy_safety",
                "palette_regression_pairs"}
    assert expected <= set(f), f"missing: {sorted(expected - set(f))}"
    assert PALETTE_DOC.exists(), "the entries point at DOCUMENTATION/COMMAND_PALETTE_TESTING.md"
    # the I-B19 / I-B20 regressions are the reason palette_switch / palette_click_behaviour exist
    assert "I-B20" in f["palette_switch"]["does"]
    assert "I-B19" in f["palette_click_behaviour"]["does"]


# --- the panel's merge (JS, under node) ----------------------------------------

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const win = { JARVIS_TEST_CHECKLIST: input.shipped };
vm.runInNewContext(fs.readFileSync(input.panel, 'utf8'), { window: win, console });
const api = win.JarvisTestChecklist;
const before = JSON.stringify(input.shipped);
const live = api._mergeCatalogue(input.shipped, input.live);
const offline = api._mergeCatalogue(input.shipped, []);
process.stdout.write(JSON.stringify({
  toolNames: Object.keys(live.data.tools), features: [...live.features], supplied: [...live.from],
  offlineFeatures: [...offline.features],
  initialCatalogue: Object.keys(api.catalogue().tools),
  shippedUntouched: JSON.stringify(input.shipped) === before,
}));
"""


def _have_node():
    try:
        return subprocess.run(["node", "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(shipped, live):
    r = subprocess.run(["node", "-e", _HARNESS],
                       input=json.dumps({"shipped": shipped, "panel": str(PANEL_JS), "live": live}),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


def test_js_panel_folds_features_in_and_keeps_them_apart():
    if not _have_node():
        print("     (node not found - JS feature merge test skipped)")
        return
    shipped = {
        "schema": 1, "updated": "x",
        "groups": [{"id": "files", "label": "Files", "blurb": "b"},
                   {"id": "palette", "label": "Palette", "blurb": "p", "kind": "feature"}],
        "tools": {"read_file": {"group": "files", "does": "reads", "steps": [{"ask": "a", "expect": "b"}]}},
        "features": {"palette_x": {"group": "palette", "does": "opens", "steps": [{"do": "type /", "expect": "opens"}]},
                     "read_file": {"group": "palette", "does": "must never replace the tool", "steps": [{"do": "x", "expect": "y"}]}},
    }
    out = _run(shipped, [{"name": "my_tool", "checklist": {"group": "files", "does": "d", "steps": [{"ask": "a", "expect": "b"}]}}])
    assert out["shippedUntouched"], "the merge mutated the shipped data"
    assert "palette_x" in out["toolNames"] and out["features"] == ["palette_x"], out
    assert "read_file" not in out["features"], "a feature replaced a tool entry"
    assert out["supplied"] == ["my_tool"], out
    assert out["offlineFeatures"] == ["palette_x"], "features must show with no live catalogue"
    assert "palette_x" in out["initialCatalogue"], "features must be present before any live read"


def test_js_panel_without_a_features_object_is_unchanged():
    if not _have_node():
        print("     (node not found - JS feature merge test skipped)")
        return
    shipped = {"schema": 1, "updated": "x", "groups": [{"id": "files", "label": "Files", "blurb": "b"}],
               "tools": {"read_file": {"group": "files", "does": "reads", "steps": [{"ask": "a", "expect": "b"}]}}}
    out = _run(shipped, [])
    assert out["features"] == [] and out["toolNames"] == ["read_file"], out


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
