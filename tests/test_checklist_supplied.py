"""Master plan G.1: a tool module can supply its own Test Checklist entry.

The shipped catalogue (web/public/test-checklist-data.js) can only document tools
that ship with jarvis. A tool the user wrote into ~/.jarvis/tools/ used to show
"NO CHECKLIST" forever. A module can now define TEST_CHECKLIST (tool name ->
entry, the shipped shape) and, for a brand-new TOOL_GROUP, TEST_CHECKLIST_GROUP.

What this pins down:
  * tool_loader extracts and normalises them; a malformed entry is DROPPED AND
    LOGGED but never rejects the tool file (a typo in test notes must not take
    a working tool offline);
  * the entry rides on that tool's `jarvis tools-list` item (the panel's only
    source), end to end through a real ~/.jarvis/tools file;
  * whatever a module puts in an entry, the payload stays plain JSON;
  * the Custom Tools editor's check reports what was dropped, and every
    starter template carries a valid example;
  * actions/_template.py's example is valid;
  * the panel's JS merge (run under node; skipped if node is missing).

Run: python3 tests/test_checklist_supplied.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Redirect HOME BEFORE importing any jarvis module (AGENTS.md > Testing).
_HOME = tempfile.mkdtemp(prefix="jarvis-checklist-supplied-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import checklist_schema, custom_tools_store, tool_loader  # noqa: E402

PANEL_JS = ROOT / "web" / "public" / "test-checklist.js"
DATA_JS = ROOT / "web" / "public" / "test-checklist-data.js"

_GOOD_ENTRY = '''
{
    "does": "Says hi.",
    "steps": [
        {"ask": "Say hi to <a name>.", "expect": "Greets them."},
        {"run": {"name": "Ada"}, "expect": "ok: true."},
    ],
    "watch": ["Blank name still greets."],
}
'''


def _module(extra="", entry=_GOOD_ENTRY, group="mygroup", checklist=True):
    checklist_line = f'TEST_CHECKLIST = {{"hi_tool": {entry}}}' if checklist else ""
    return f'''
def tool_hi(args):
    return {{"ok": True}}

TOOL_SCHEMAS = [{{"name": "hi_tool", "description": "Say hi.",
                 "parameters": {{"type": "object", "properties": {{}}}}}}]
TOOLS = {{"hi_tool": tool_hi}}
TOOL_GROUP = {group!r}
TOOL_KEYWORDS = {{"hi_tool": {{"say hi": 10}}}}
{checklist_line}
{extra}
'''


def _discover(files, reserved=None):
    d = Path(tempfile.mkdtemp(prefix="jarvis-actions-"))
    logs = []
    try:
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        records = tool_loader.discover_actions(actions_dir=d, reserved_names=reserved, logger=logs.append)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return records, logs


_counter = [0]


def _one(source, reserved=None):
    # A UNIQUE filename per call: discover_actions() caches imported modules in
    # sys.modules by file stem, so reusing a name would silently hand back the
    # first module for every later case.
    _counter[0] += 1
    fname = f"hi_tool_mod{_counter[0]}.py"
    records, logs = _discover({fname: source}, reserved)
    assert len(records) == 1, records
    return records[0], logs


# --- loader -------------------------------------------------------------------

def test_valid_entry_is_extracted_and_group_defaults_to_tool_group():
    rec, logs = _one(_module())
    assert rec.valid, rec.error
    e = rec.checklist["hi_tool"]
    assert e["group"] == "mygroup", e
    assert e["does"] == "Says hi." and len(e["steps"]) == 2
    assert not [l for l in logs if "[checklist]" in l], logs


def test_an_explicit_group_must_match_tool_group():
    same = _GOOD_ENTRY.replace('"does"', '"group": "mygroup", "does"')
    rec, logs = _one(_module(entry=same))
    assert "hi_tool" in rec.checklist
    other = _GOOD_ENTRY.replace('"does"', '"group": "files", "does"')
    rec, logs = _one(_module(entry=other))
    assert rec.valid and not rec.checklist, "a disagreeing group is dropped, not honoured"
    assert any("TOOL_GROUP" in l and "[checklist]" in l for l in logs), logs


def test_malformed_entries_are_dropped_and_logged_but_the_tool_still_loads():
    bad = {
        "no steps": '{"does": "x"}',
        "empty steps": '{"does": "x", "steps": []}',
        "no does": '{"steps": [{"ask": "a", "expect": "b"}]}',
        "ask and run": '{"does": "x", "steps": [{"ask": "a", "run": {}, "expect": "b"}]}',
        "neither ask nor run": '{"does": "x", "steps": [{"expect": "b"}]}',
        "no expect": '{"does": "x", "steps": [{"ask": "a"}]}',
        "unknown key": '{"does": "x", "steps": [{"ask": "a", "expect": "b"}], "watchs": ["typo"]}',
        "unknown step key": '{"does": "x", "steps": [{"ask": "a", "expect": "b", "note": "?"}]}',
        "duplicate step": '{"does": "x", "steps": [{"ask": "a", "expect": "b"}, {"ask": "a", "expect": "c"}]}',
        "needs not a list": '{"does": "x", "steps": [{"ask": "a", "expect": "b"}], "needs": "a string"}',
        "run not JSON": '{"does": "x", "steps": [{"run": {"k": {1, 2}}, "expect": "b"}]}',
        "does too long": '{"does": "x" * 5000, "steps": [{"ask": "a", "expect": "b"}]}',
        "not a dict": '"just a string"',
    }
    for label, entry in bad.items():
        rec, logs = _one(_module(entry=entry))
        assert rec.valid, (label, rec.error)  # the TOOL is fine
        assert "hi_tool" in rec.tools
        assert rec.checklist == {}, (label, rec.checklist)
        assert any("[checklist]" in l and "hi_tool_mod" in l for l in logs), (label, logs)


def test_stray_entry_name_and_non_dict_checklist_are_reported_not_fatal():
    rec, logs = _one(_module(extra='TEST_CHECKLIST["ghost_tool"] = {"does": "x", "steps": []}'))
    assert rec.valid and "hi_tool" in rec.checklist and "ghost_tool" not in rec.checklist
    assert any("ghost_tool" in l for l in logs), logs

    rec, logs = _one(_module(extra="TEST_CHECKLIST = [1, 2]"))
    assert rec.valid and rec.checklist == {}
    assert any("must be a dict" in l for l in logs), logs


def test_group_label_is_extracted_and_a_malformed_one_is_ignored():
    rec, _ = _one(_module(extra='TEST_CHECKLIST_GROUP = {"label": "My things", "blurb": "Stuff I made."}'))
    assert rec.checklist_group == {"label": "My things", "blurb": "Stuff I made."}
    rec, logs = _one(_module(extra='TEST_CHECKLIST_GROUP = {"label": ""}'))
    assert rec.valid and rec.checklist_group == {} and "hi_tool" in rec.checklist
    assert any("TEST_CHECKLIST_GROUP" in l for l in logs), logs
    rec, logs = _one(_module(extra='TEST_CHECKLIST_GROUP = "My things"'))
    assert rec.checklist_group == {} and any("must be a dict" in l for l in logs)


def test_a_file_without_the_new_fields_is_unchanged():
    rec, logs = _one(_module(checklist=False))
    assert rec.valid and rec.checklist == {} and rec.checklist_group == {}
    assert not [l for l in logs if "[checklist]" in l]


def test_a_rejected_file_contributes_no_entry():
    # Name collision with a built-in -> the whole file is rejected; its entry goes with it.
    rec, logs = _one(_module(), reserved={"hi_tool"})
    assert not rec.valid and rec.checklist == {}


def test_supplied_entries_are_copies_not_the_modules_own_objects():
    src = {"hi_tool": json.loads(json.dumps({"does": "d", "steps": [{"ask": "a", "expect": "b"}]}))}
    out, _, problems = checklist_schema.extract_supplied(src, None, {"hi_tool"}, "g")
    assert not problems
    out["hi_tool"]["does"] = "changed"
    assert src["hi_tool"]["does"] == "d" and "group" not in src["hi_tool"]


# --- end to end: a real ~/.jarvis/tools file -> `jarvis tools-list` payload ------

def _payload_for(user_files):
    home = tempfile.mkdtemp(prefix="jarvis-e2e-home-")
    try:
        tdir = Path(home) / ".jarvis" / "tools"
        tdir.mkdir(parents=True)
        for name, text in user_files.items():
            (tdir / name).write_text(text, encoding="utf-8")
        env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONPATH=str(ROOT / "jarvis-cli"))
        code = "import json; from jarvis import tools; print('@@' + json.dumps(tools.tools_list_payload()))"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stderr[-800:]
        line = [l for l in r.stdout.splitlines() if l.startswith("@@")][0]
        return json.loads(line[2:]), r.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_payload_carries_a_custom_tools_entry_and_only_that_tools():
    payload, _ = _payload_for({"hi_tool_mod.py": _module(extra='TEST_CHECKLIST_GROUP = {"label": "My things", "blurb": "Mine."}')})
    assert isinstance(payload, list), "the /api/tools payload must stay a plain list"
    by_name = {t["name"]: t for t in payload}
    mine = by_name["hi_tool"]
    assert mine["checklist"]["group"] == "mygroup" and mine["checklist"]["steps"][1]["run"] == {"name": "Ada"}
    assert mine["checklist_group"] == {"label": "My things", "blurb": "Mine."}
    others = [t for n, t in by_name.items() if n != "hi_tool" and ("checklist" in t or "checklist_group" in t)]
    assert not others, [t["name"] for t in others]


def test_payload_is_still_json_when_a_module_puts_junk_in_its_entry():
    junk = '{"does": "x", "steps": [{"run": {"k": object()}, "expect": "b"}]}'
    payload, stderr = _payload_for({"hi_tool_mod.py": _module(entry=junk)})
    mine = {t["name"]: t for t in payload}["hi_tool"]
    assert "checklist" not in mine, "an unserialisable entry must be dropped, not break tools-list"
    assert "[checklist]" in stderr


def test_a_module_with_a_bad_entry_still_registers_its_tool_end_to_end():
    payload, stderr = _payload_for({"hi_tool_mod.py": _module(entry='{"does": "x"}')})
    assert "hi_tool" in {t["name"] for t in payload}


# --- Custom Tools editor: check / write / templates -----------------------------

def test_validate_source_reports_entries_missing_and_problems():
    ok = custom_tools_store.validate_source(_module(), "hi_tool_mod")
    assert ok["ok"] and ok["checklist"] == ["hi_tool"] and ok["checklist_missing"] == [] and ok["checklist_problems"] == []

    bare = custom_tools_store.validate_source(_module(checklist=False), "hi_tool_mod")
    assert bare["ok"] and bare["checklist_missing"] == ["hi_tool"] and bare["checklist"] == []

    broken = custom_tools_store.validate_source(_module(entry='{"does": "x"}'), "hi_tool_mod")
    assert broken["ok"], "a bad checklist entry must never fail the check itself"
    assert broken["checklist_missing"] == ["hi_tool"] and broken["checklist_problems"]


def test_write_tool_passes_the_checklist_report_through():
    r = custom_tools_store.write_tool("hi_tool_mod", _module(entry='{"does": "x"}'))
    assert r["ok"] and r["checklist_problems"] and r["checklist_missing"] == ["hi_tool"]
    custom_tools_store.delete_tool("hi_tool_mod")


def test_every_starter_template_carries_a_valid_checklist_example():
    for t in custom_tools_store.templates():
        result = custom_tools_store.validate_source(custom_tools_store.template_source(t["id"]), t["id"])
        assert result["ok"], (t["id"], result.get("error"))
        assert result["checklist_problems"] == [], (t["id"], result["checklist_problems"])
        assert result["checklist_missing"] == [], (t["id"], "template has no TEST_CHECKLIST for", result["checklist_missing"])


def test_the_actions_template_example_is_valid():
    import importlib.util
    spec = importlib.util.spec_from_file_location("jarvis_tmpl", ROOT / "jarvis-cli" / "jarvis" / "actions" / "_template.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    entries, group_meta, problems = checklist_schema.extract_supplied(
        m.TEST_CHECKLIST, m.TEST_CHECKLIST_GROUP, set(m.TOOLS), m.TOOL_GROUP)
    assert not problems, problems
    assert set(entries) == set(m.TOOLS) and group_meta and group_meta["label"]


def test_validator_accepts_every_shipped_entry_shape_and_rejects_typos():
    text = DATA_JS.read_text(encoding="utf-8")
    body = text.split("/*JSON-BEGIN*/", 1)[1].rsplit("/*JSON-END*/", 1)[0]
    data = json.loads(body)
    assert all(not checklist_schema.validate_entry(n, e) for n, e in data["tools"].items())
    typo = dict(next(iter(data["tools"].values())), watchs=["x"])
    assert checklist_schema.validate_entry("t", typo)


# --- the panel's merge (JS, under node) ----------------------------------------

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const win = { JARVIS_TEST_CHECKLIST: input.shipped };
vm.runInNewContext(fs.readFileSync(input.panel, 'utf8'), { window: win, console });
const api = win.JarvisTestChecklist;
const out = input.cases.map((c) => {
  const before = JSON.stringify(input.shipped);
  const m = api._mergeCatalogue(input.shipped, c.live);
  return { data: JSON.parse(JSON.stringify(m.data)), from: [...m.from], shippedUntouched: JSON.stringify(input.shipped) === before };
});
process.stdout.write(JSON.stringify(out));
"""


def _node_merge(shipped, lives):
    r = subprocess.run(["node", "-e", _HARNESS],
                       input=json.dumps({"shipped": shipped, "panel": str(PANEL_JS), "cases": [{"live": l} for l in lives]}),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


def _have_node():
    try:
        return subprocess.run(["node", "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _shipped_small():
    return {"schema": 1, "updated": "x",
            "groups": [{"id": "files", "label": "Files", "blurb": "b"}, {"id": "custom", "label": "Custom tools", "blurb": "c"}],
            "tools": {"read_file": {"group": "files", "does": "reads", "steps": [{"ask": "a", "expect": "b"}]}}}


def _entry(**kw):
    e = {"group": "custom", "does": "d", "steps": [{"ask": "a", "expect": "b"}]}
    e.update(kw)
    return e


def test_js_merge():
    if not _have_node():
        print("     (node not found - JS merge test skipped)")
        return
    shipped = _shipped_small()
    cases = [
        [{"name": "mine", "checklist": _entry()}],                                                  # 0 supplied, shipped group
        [{"name": "mine", "checklist": _entry(group="brand_new_group")}],                           # 1 new group, no label
        [{"name": "mine", "checklist": _entry(group="g2"), "checklist_group": {"label": "Named", "blurb": "B"}}],  # 2 labelled
        [{"name": "read_file", "checklist": _entry(does="SUPPLIED")}],                              # 3 shipped wins
        [{"name": "bad", "checklist": {"does": "x"}}, {"name": "bad2", "checklist": "nope"},
         {"name": "bad3", "checklist": _entry(steps=[{"ask": "a", "run": {}, "expect": "b"}])},
         {"name": "noentry"}, None, 7],                                                             # 4 junk tolerated
        "not a list",                                                                               # 5 bad payload
        [{"name": "a", "checklist": _entry(group="g2")}, {"name": "b", "checklist": _entry(group="g2"), "checklist_group": {"label": "Later"}}],  # 6 first label wins... a has none
    ]
    out = _node_merge(shipped, cases)
    assert all(o["shippedUntouched"] for o in out), "the shipped data must never be mutated"

    d0 = out[0]
    assert d0["from"] == ["mine"] and d0["data"]["tools"]["mine"]["group"] == "custom"
    assert [g["id"] for g in d0["data"]["groups"]] == ["files", "custom"], "an existing group is not duplicated"
    assert "read_file" in d0["data"]["tools"], "shipped entries survive"

    d1 = out[1]["data"]
    assert [g["id"] for g in d1["groups"]] == ["files", "custom", "brand_new_group"]
    assert d1["groups"][2]["label"] == "Brand new group", d1["groups"][2]

    d2 = out[2]["data"]["groups"][2]
    assert d2["label"] == "Named" and d2["blurb"] == "B"

    d3 = out[3]
    assert d3["from"] == [] and d3["data"]["tools"]["read_file"]["does"] == "reads", "shipped beats supplied"

    d4 = out[4]
    assert d4["from"] == [] and sorted(d4["data"]["tools"]) == ["read_file"], d4

    assert out[5]["from"] == [] and sorted(out[5]["data"]["tools"]) == ["read_file"]

    d6 = out[6]
    assert d6["from"] == ["a", "b"]
    g2 = [g for g in d6["data"]["groups"] if g["id"] == "g2"]
    assert len(g2) == 1, "a group is added once"


def test_js_merge_over_the_real_shipped_catalogue():
    if not _have_node():
        return
    text = DATA_JS.read_text(encoding="utf-8")
    shipped = json.loads(text.split("/*JSON-BEGIN*/", 1)[1].rsplit("/*JSON-END*/", 1)[0])
    out = _node_merge(shipped, [[{"name": "my_own_tool", "checklist": _entry()}]])[0]
    assert len(out["data"]["tools"]) == len(shipped["tools"]) + 1
    assert len(out["data"]["groups"]) == len(shipped["groups"]), "'custom' is a shipped group, so no extra section"


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
        except Exception as e:  # noqa: BLE001 — surface a crash as a failure, not a traceback wall
            failed += 1
            print(f"FAIL {n}\n{type(e).__name__}: {e}")
    print(f"{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
