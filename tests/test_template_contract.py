"""actions/_template.py is documentation the loader never loads, so nothing
else notices when it drifts. This pins its contract:

  1. the CONTENTS block and the numbered section headers agree and run 1..N in
     order, and every "section N" the file cites exists;
  2. every module-level name tool_loader reads from a tool file is mentioned
     in the template (a new loader name with no template section fails here);
  3. the template defines no live TOOL_DOWNLOADS / DAEMONS / TOOL_UI / ... and
     is skipped by discovery (a copy of it must not register anything);
  4. the complete example in section 14 compiles, and its handlers behave as
     the section says (dry run changes nothing, a guest is refused, an
     unreadable sender is refused too);
  5. test_needs_h1_*: the example validates through the loader. It needs
     jarvis-updates-core.patch (the update_group / apply_tool keys) and fails
     on a tree without it, by design.

Run: python3 tests/test_template_contract.py
"""

import importlib.util
import os
import re
import sys
import tempfile
import types
from pathlib import Path

# HOME first: jarvis modules resolve Path.home() at import time.
os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["USERPROFILE"] = os.environ["HOME"]
os.environ.pop("JARVIS_CHANNEL_SENDER", None)
os.environ.pop("JARVIS_UI", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tool_loader  # noqa: E402

TEMPLATE = (Path(__file__).resolve().parent.parent
            / "jarvis-cli" / "jarvis" / "actions" / "_template.py")
LOADER = Path(tool_loader.__file__).resolve()


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def _contents():
    """[(number, first word)] from the CONTENTS block of the docstring."""
    block = _text().split("CONTENTS", 1)[1].split("EVERY NAME THE LOADER READS", 1)[0]
    return [(int(m.group(1)), m.group(2))
            for m in re.finditer(r"^\s+(\d+)\. (\S+)", block, re.M)]


def _headers():
    return [(int(m.group(1)), m.group(2))
            for m in re.finditer(r"^# (\d+)\. (\S+)", _text(), re.M)]


def test_contents_and_headers_agree_and_run_in_order():
    contents, headers = _contents(), _headers()
    assert contents, "CONTENTS block not found"
    nums = [n for n, _ in headers]
    assert nums == list(range(1, len(nums) + 1)), "headers out of order: %r" % nums
    assert [n for n, _ in contents] == nums, (contents, headers)
    for (n, c), (_, h) in zip(contents, headers):
        assert c.upper().strip(",") == h.upper().strip(",") or h.upper().startswith(c.upper()), \
            "section %d: contents says %r, header says %r" % (n, c, h)


def test_every_cited_section_exists():
    last = max(n for n, _ in _headers())
    text = _text()
    for m in re.finditer(r"\bsections? (\d+)(?:\s*(?:-|to|and)\s*(\d+))?", text, re.I):
        for g in m.groups():
            if g:
                assert 1 <= int(g) <= last, "cites section %s, but the last is %d" % (g, last)
    assert "lazy import, see section 2" not in text


def test_every_name_the_loader_reads_is_in_the_template():
    src = LOADER.read_text(encoding="utf-8")
    names = set(re.findall(r'getattr\(\s*module\s*,\s*"([A-Z][A-Z_]+)"', src))
    assert {"TOOL_SCHEMAS", "TOOLS", "TOOL_GROUP", "TOOL_DOWNLOADS"} <= names, names
    text = _text()
    missing = sorted(n for n in names if n not in text)
    assert not missing, "tool_loader reads %s but the template never mentions it" % missing


def _load_template_module():
    spec = importlib.util.spec_from_file_location("_template_under_test", str(TEMPLATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_template_has_no_live_optional_blocks():
    mod = _load_template_module()
    for name in ("TOOL_DOWNLOADS", "DAEMONS", "TOOL_UI", "TOOL_COMMANDS",
                 "TOOL_SETTINGS", "PERSONAS"):
        assert not hasattr(mod, name), "the template defines %s for real" % name


def test_discovery_skips_the_template():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-tpl-"))
    (tmp / "_template.py").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    assert tool_loader.discover_actions(actions_dir=str(tmp), logger=lambda *a, **k: None) == []


# --- the section 14 example --------------------------------------------------
def _example_source():
    lines, on = [], False
    for line in _text().split("\n"):
        if line.startswith("# >>> EXAMPLE FILE BEGINS"):
            on = True
            continue
        if line.startswith("# <<< EXAMPLE FILE ENDS"):
            break
        if on:
            assert line == "#" or line.startswith("# "), "example line not commented: %r" % line
            lines.append(line[2:] if line.startswith("# ") else "")
    assert lines, "example markers not found"
    return "\n".join(lines) + "\n"


def _example_module():
    mod = types.ModuleType("widget_updates")
    mod.__file__ = "widget_updates.py"
    exec(compile(_example_source(), "widget_updates.py", "exec"), mod.__dict__)
    return mod


def _with_sender(value):
    if value is None:
        os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    else:
        os.environ["JARVIS_CHANNEL_SENDER"] = value


def test_example_check_tool_answers_the_documented_shape():
    mod = _example_module()
    _with_sender(None)
    res = mod.tool_check_widget_updates({})
    assert res["complete"] is True and res["problems"] == []
    assert len(res["updates"]) == 1
    u = res["updates"][0]
    assert u["id"] == "gizmo" and u["installed"] == "1.2" and u["available"] == "1.3"
    assert u["detail"] == [{"label": "Channel", "value": "stable"}]


def test_example_apply_tool_dry_run_and_guest_rules():
    mod = _example_module()
    try:
        _with_sender(None)
        dry = mod.tool_apply_widget_update({"id": "gizmo", "dry_run": True})
        assert dry["ok"] is True and dry["state"] == "unchanged" and dry["plan"]
        real = mod.tool_apply_widget_update({"id": "gizmo"})
        assert real["ok"] is True and real["state"] == "done"
        assert mod.tool_apply_widget_update({})["ok"] is False

        owner = '{"platform": "telegram", "user_id": "1", "is_owner": true}'
        guest = '{"platform": "telegram", "user_id": "2", "is_owner": false}'
        _with_sender(owner)
        assert mod.tool_apply_widget_update({"id": "gizmo"})["ok"] is True
        for raw in (guest, "not json", "[1, 2]"):   # guest, unreadable, not an object
            _with_sender(raw)
            for dry_run in (False, True):          # a dry run is refused too
                res = mod.tool_apply_widget_update({"id": "gizmo", "dry_run": dry_run})
                assert res["ok"] is False and res["state"] == "failed", (raw, res)
    finally:
        _with_sender(None)


def test_example_keeps_its_confirm_and_schema_promises():
    mod = _example_module()
    assert mod.TOOL_CONFIRM_REQUIRED == {"apply_widget_update"}
    assert "check_widget_updates" not in mod.TOOL_CONFIRM_REQUIRED
    schema = {s["name"]: s for s in mod.TOOL_SCHEMAS}
    props = schema["apply_widget_update"]["parameters"]["properties"]
    assert props["id"]["type"] == "string" and props["dry_run"]["type"] == "boolean"
    assert set(mod.TOOLS) == set(schema)


def test_needs_h1_example_validates_through_the_loader():
    """Fails until jarvis-updates-core.patch is applied (update_group, apply_tool,
    apply_timeout_s and updates_only are unknown keys before it)."""
    mod = _example_module()
    problems = []
    rec = tool_loader._validate(mod, "widget_updates.py", lambda *a, **k: problems.append(" ".join(map(str, a))))
    assert rec.valid, rec.error
    bad = [p for p in problems if "[tool-downloads]" in p or "[checklist]" in p]
    assert not bad, bad
    assert len(rec.downloads) == 1, rec.downloads
    entry = rec.downloads[0]
    assert entry.get("apply_tool") == "apply_widget_update", entry
    assert entry.get("update_tool") == "check_widget_updates", entry


# --- runner (keep BELOW every test: it reads globals() when it executes) -----
if __name__ == "__main__":
    failed = 0
    names = sorted(n for n in globals() if n.startswith("test_"))
    for name in names:
        try:
            globals()[name]()
            print("ok      %s" % name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAILED  %s: %r" % (name, exc))
    print("%d passed, %d failed" % (len(names) - failed, failed))
    sys.exit(1 if failed else 0)
