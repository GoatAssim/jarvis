"""Master plan G.1, extended by G.2: a tool module can supply its own Test
Checklist entry (or entries), and — since G.2 — its own checklist group (or
groups).

The shipped catalogue (web/public/test-checklist-data.js) can only document tools
that ship with jarvis. A tool the user wrote into ~/.jarvis/tools/ used to show
"NO CHECKLIST" forever. A module can now define TEST_CHECKLIST (tool name ->
entry, the shipped shape) and, for a brand-new TOOL_GROUP, TEST_CHECKLIST_GROUP.

G.2 removes the two remaining caps: a module can give each of its own tools its
own TEST_CHECKLIST entry (already true under G.1, regression-proofed here), and
TEST_CHECKLIST_GROUP can name more than one section — {group_id: {"label",
"blurb"}, ...} instead of one {"label", "blurb"} tied to TOOL_GROUP — for a
module whose tools genuinely split across more than one logical category.

What this pins down:
  * tool_loader extracts and normalises them; a malformed entry is DROPPED AND
    LOGGED but never rejects the tool file (a typo in test notes must not take
    a working tool offline);
  * a module can supply as many TEST_CHECKLIST entries as it has tools, and
    (G.2) as many TEST_CHECKLIST_GROUP sections as those entries need;
  * an entry's own "group" can be TOOL_GROUP or one of the module's own
    declared multi-group ids; anything else is dropped and logged;
  * the entry (and its OWN group's meta) rides on that tool's `jarvis
    tools-list` item (the panel's only source), end to end through a real
    ~/.jarvis/tools file;
  * whatever a module puts in an entry, the payload stays plain JSON;
  * the Custom Tools editor's check reports what was dropped, and every
    starter template carries a valid example;
  * actions/_template.py's example is valid, and (G.2) demonstrates both
    extensions at once;
  * the panel's JS merge (run under node; skipped if node is missing) —
    already tool-item-centric, so a multi-group module needed no JS change,
    only the regression test here that pins that down.

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


def test_two_tools_in_one_module_each_get_their_own_entry():
    # Regression-proofs the part of G.2 that was already true under G.1
    # (TEST_CHECKLIST is a dict keyed by tool name, so nothing capped a
    # module at one entry) — a module with 2+ tools, each with its own
    # entry, under the plain single-group shape.
    mod = _module(extra=(
        'TOOLS["bye_tool"] = tool_hi\n'
        'TOOL_SCHEMAS.append({"name": "bye_tool", "description": "Say bye.", '
        '"parameters": {"type": "object", "properties": {}}})\n'
        'TOOL_KEYWORDS["bye_tool"] = {"say bye": 10}\n'
        'TEST_CHECKLIST["bye_tool"] = {"does": "Says bye.", '
        '"steps": [{"ask": "Say bye.", "expect": "Says goodbye."}]}'
    ))
    rec, logs = _one(mod)
    assert rec.valid, rec.error
    assert not [l for l in logs if "[checklist]" in l], logs
    assert set(rec.checklist) == {"hi_tool", "bye_tool"}
    assert rec.checklist["hi_tool"]["group"] == rec.checklist["bye_tool"]["group"] == "mygroup"


def test_group_label_is_extracted_and_a_malformed_one_is_ignored():
    # Single-group shape: group_meta comes back keyed by TOOL_GROUP itself.
    rec, _ = _one(_module(extra='TEST_CHECKLIST_GROUP = {"label": "My things", "blurb": "Stuff I made."}'))
    assert rec.checklist_group == {"mygroup": {"label": "My things", "blurb": "Stuff I made."}}
    rec, logs = _one(_module(extra='TEST_CHECKLIST_GROUP = {"label": ""}'))
    assert rec.valid and rec.checklist_group == {} and "hi_tool" in rec.checklist
    assert any("TEST_CHECKLIST_GROUP" in l for l in logs), logs
    rec, logs = _one(_module(extra='TEST_CHECKLIST_GROUP = "My things"'))
    assert rec.checklist_group == {} and any("must be a dict" in l for l in logs)


# --- multi-group shape (master plan G.2) ---------------------------------------

def test_multi_group_shape_resolves_each_entry_to_its_own_group():
    # Two tools, each tagged into a DIFFERENT declared group; TOOL_GROUP
    # itself ("mygroup") is never used by either entry here.
    mod = _module(
        entry='{"does": "Says hi.", "group": "greetings", '
              '"steps": [{"ask": "Say hi.", "expect": "Greets."}]}',
        extra=(
            'TOOLS["bye_tool"] = tool_hi\n'
            'TOOL_SCHEMAS.append({"name": "bye_tool", "description": "Say bye.", '
            '"parameters": {"type": "object", "properties": {}}})\n'
            'TOOL_KEYWORDS["bye_tool"] = {"say bye": 10}\n'
            'TEST_CHECKLIST["bye_tool"] = {"does": "Says bye.", "group": "farewells", '
            '"steps": [{"ask": "Say bye.", "expect": "Says goodbye."}]}\n'
            'TEST_CHECKLIST_GROUP = {'
            '"greetings": {"label": "Greetings", "blurb": "Hello messages."}, '
            '"farewells": {"label": "Farewells", "blurb": "Goodbye messages."}}'
        ),
    )
    rec, logs = _one(mod)
    assert rec.valid, rec.error
    assert not [l for l in logs if "[checklist]" in l], logs
    assert rec.checklist["hi_tool"]["group"] == "greetings"
    assert rec.checklist["bye_tool"]["group"] == "farewells"
    assert rec.checklist_group == {
        "greetings": {"label": "Greetings", "blurb": "Hello messages."},
        "farewells": {"label": "Farewells", "blurb": "Goodbye messages."},
    }


def test_multi_group_entry_may_still_default_to_tool_group():
    # Under the multi-group shape, an entry with no explicit "group" still
    # falls back to TOOL_GROUP — the multi-group ids are additive, not a
    # replacement for the default.
    mod = _module(extra='TEST_CHECKLIST_GROUP = {"extra": {"label": "Extra"}}')
    rec, logs = _one(mod)
    assert rec.valid, rec.error
    assert rec.checklist["hi_tool"]["group"] == "mygroup"
    assert rec.checklist_group == {"extra": {"label": "Extra", "blurb": ""}}
    assert not [l for l in logs if "[checklist]" in l], logs


def test_multi_group_entry_naming_tool_group_itself_is_valid():
    mod = _module(entry='{"does": "Says hi.", "group": "mygroup", '
                         '"steps": [{"ask": "Say hi.", "expect": "Greets."}]}',
                  extra='TEST_CHECKLIST_GROUP = {"extra": {"label": "Extra"}}')
    rec, logs = _one(mod)
    assert rec.valid and rec.checklist["hi_tool"]["group"] == "mygroup"
    assert not [l for l in logs if "[checklist]" in l], logs


def test_multi_group_entry_with_an_undeclared_group_is_dropped_and_logged():
    mod = _module(entry='{"does": "Says hi.", "group": "nowhere", '
                         '"steps": [{"ask": "Say hi.", "expect": "Greets."}]}',
                  extra='TEST_CHECKLIST_GROUP = {"extra": {"label": "Extra"}}')
    rec, logs = _one(mod)
    assert rec.valid and rec.checklist == {}
    assert any("nowhere" in l and "[checklist]" in l for l in logs), logs


def test_multi_group_bad_id_or_meta_is_dropped_and_logged_but_others_survive():
    mod = _module(extra=(
        'TEST_CHECKLIST_GROUP = {'
        '"good": {"label": "Good"}, '
        '"bad": {"label": ""}, '
        '"": {"label": "No id"}}'
    ))
    rec, logs = _one(mod)
    assert rec.valid
    assert rec.checklist_group == {"good": {"label": "Good", "blurb": ""}}
    assert any("TEST_CHECKLIST_GROUP['bad']" in l for l in logs), logs
    assert any("non-string or empty group id" in l for l in logs), logs


def test_an_empty_dict_is_still_the_single_group_shape_not_multi():
    # {} has no keys outside {"label", "blurb"} (vacuously), so it's read as
    # a (malformed, missing 'label') single-group meta, not a zero-group
    # multi-group dict — same as before G.2 existed.
    rec, logs = _one(_module(extra="TEST_CHECKLIST_GROUP = {}"))
    assert rec.valid and rec.checklist_group == {}
    assert any("'label' must be a non-empty string" in l for l in logs), logs


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


def test_payload_carries_a_distinct_checklist_group_per_tool_in_a_multi_group_module():
    # G.2 end to end: one module, two tools, two declared groups — each
    # tool's `jarvis tools-list` item must carry ITS OWN checklist_group,
    # not the module's (there isn't one single one to fall back to).
    mod = _module(extra=(
        'TOOLS["bye_tool"] = tool_hi\n'
        'TOOL_SCHEMAS.append({"name": "bye_tool", "description": "Say bye.", '
        '"parameters": {"type": "object", "properties": {}}})\n'
        'TOOL_KEYWORDS["bye_tool"] = {"say bye": 10}\n'
        'TEST_CHECKLIST["bye_tool"] = {"does": "Says bye.", "group": "farewells", '
        '"steps": [{"ask": "Say bye.", "expect": "Says goodbye."}]}\n'
        'TEST_CHECKLIST_GROUP = {'
        '"mygroup": {"label": "Greetings", "blurb": "Hello messages."}, '
        '"farewells": {"label": "Farewells", "blurb": "Goodbye messages."}}'
    ))
    payload, stderr = _payload_for({"hi_tool_mod.py": mod})
    assert "[checklist]" not in stderr, stderr
    by_name = {t["name"]: t for t in payload}
    assert by_name["hi_tool"]["checklist"]["group"] == "mygroup"
    assert by_name["hi_tool"]["checklist_group"] == {"label": "Greetings", "blurb": "Hello messages."}
    assert by_name["bye_tool"]["checklist"]["group"] == "farewells"
    assert by_name["bye_tool"]["checklist_group"] == {"label": "Farewells", "blurb": "Goodbye messages."}


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


def test_validate_source_reports_a_multi_group_module_correctly():
    # A custom-tool author's own module uses the multi-group shape; the
    # editor's Check button must report both entries as present, and an
    # undeclared "group" reference as a problem — same channel as any other
    # malformed entry, never a reason to fail the check itself.
    good = custom_tools_store.validate_source(
        _module(entry='{"does": "Says hi.", "group": "greetings", '
                      '"steps": [{"ask": "Say hi.", "expect": "Greets."}]}',
                extra='TEST_CHECKLIST_GROUP = {"greetings": {"label": "Greetings"}}'),
        "hi_tool_mod",
    )
    assert good["ok"] and good["checklist"] == ["hi_tool"] and good["checklist_problems"] == []

    bad_ref = custom_tools_store.validate_source(
        _module(entry='{"does": "Says hi.", "group": "nowhere", '
                      '"steps": [{"ask": "Say hi.", "expect": "Greets."}]}',
                extra='TEST_CHECKLIST_GROUP = {"greetings": {"label": "Greetings"}}'),
        "hi_tool_mod",
    )
    assert bad_ref["ok"], "an entry naming an undeclared group must never fail the check itself"
    assert bad_ref["checklist_missing"] == ["hi_tool"]
    assert any("nowhere" in p for p in bad_ref["checklist_problems"])


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
    # G.2: the template now demonstrates BOTH extensions at once — two tools,
    # each with its own entry, split across two declared checklist groups.
    assert set(entries) == set(m.TOOLS) == {"example_ping", "example_echo"}
    assert entries["example_ping"]["group"] == m.TOOL_GROUP
    assert entries["example_echo"]["group"] != m.TOOL_GROUP
    assert set(group_meta) == {m.TOOL_GROUP, entries["example_echo"]["group"]}
    assert all(g["label"] for g in group_meta.values())


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
        [{"name": "greeter", "checklist": _entry(group="greetings"), "checklist_group": {"label": "Greetings", "blurb": "Hi."}},
         {"name": "farewell", "checklist": _entry(group="farewells"), "checklist_group": {"label": "Farewells", "blurb": "Bye."}}],  # 7 G.2: one module (by convention), two tools, two distinct groups
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

    # G.2: two tools (as if from one multi-group module) land in two
    # DIFFERENT new sections, each with its own supplied label/blurb — the
    # merge is per-tool, so this needed no code change, only this test.
    d7 = out[7]["data"]
    assert out[7]["from"] == ["greeter", "farewell"]
    new_groups = {g["id"]: g for g in d7["groups"] if g["id"] in ("greetings", "farewells")}
    assert set(new_groups) == {"greetings", "farewells"}
    assert new_groups["greetings"]["label"] == "Greetings" and new_groups["greetings"]["blurb"] == "Hi."
    assert new_groups["farewells"]["label"] == "Farewells" and new_groups["farewells"]["blurb"] == "Bye."
    assert d7["tools"]["greeter"]["group"] == "greetings"
    assert d7["tools"]["farewell"]["group"] == "farewells"


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
