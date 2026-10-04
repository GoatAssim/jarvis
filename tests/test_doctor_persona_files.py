"""`jarvis doctor` must not report a persona-only action file as rejected, and
must still report a genuinely broken one.

actions/starter_personas.py carries PERSONAS and no TOOL_SCHEMAS/TOOLS/
TOOL_GROUP — a legitimate file (actions/_template.py section 7). tool_loader
deliberately keeps it as `valid=False, error="__not_an_action__"` so its
personas still reach tools.py. doctor.check_tools() used to count every
`not valid` record as a rejection, so a healthy install printed

    [FAIL] Action file starter_personas.py — __not_an_action__
    Tool auto-discovery — 13 action files loaded, 1 rejected

and ended with "Something is broken." The first case pins the real tree clean;
the other two pin the fix from both sides, so it can't be "fixed" by hiding
every rejection.

Also pins that actions/_template.py's UI example (tool_example_ui_demo) runs
every branch headless and hands back the safe default for each question.

Run: python3 tests/test_doctor_persona_files.py
"""

import os
import sys
import tempfile
from pathlib import Path

# HOME first: jarvis modules resolve Path.home() at import time.
os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["USERPROFILE"] = os.environ["HOME"]
os.environ.pop("JARVIS_UI", None)  # make sure ui_bridge sees a headless run
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import doctor, tool_loader  # noqa: E402


def _tools_checks():
    return {c.id: c for c in doctor.check_tools()}


def _run_in_dir(files):
    """Run check_tools() against a temp actions dir holding `files`
    ({name: source}). ACTIONS_DIR is read at call time, so patching the
    module global is enough; always restored."""
    d = Path(tempfile.mkdtemp(prefix="jarvis-actions-"))
    for name, src in files.items():
        (d / name).write_text(src, encoding="utf-8")
    saved = tool_loader.ACTIONS_DIR
    tool_loader.ACTIONS_DIR = d
    try:
        return _tools_checks()
    finally:
        tool_loader.ACTIONS_DIR = saved


def test_real_tree_has_no_failing_tool_check():
    checks = _tools_checks()
    failing = [c.id for c in checks.values() if c.status == doctor.FAIL]
    assert not failing, failing
    assert not [i for i in checks if i.startswith("tools.rejected.")], list(checks)
    assert "0 rejected" in checks["tools.discovery"].detail, checks["tools.discovery"].detail
    assert checks["tools.discovery"].status == doctor.OK


def test_persona_only_file_is_reported_ok_not_rejected():
    checks = _run_in_dir({
        "persona_only_probe.py":
            'PERSONAS = [{"id": "probe-persona", "name": "Probe", "hex": "#7dd8a0"}]\n',
    })
    assert "tools.rejected.persona_only_probe.py" not in checks, list(checks)
    ok = checks["tools.personas.persona_only_probe.py"]
    assert ok.status == doctor.OK and "1 persona" in ok.detail, (ok.status, ok.detail)
    disc = checks["tools.discovery"]
    assert disc.status == doctor.OK and "0 rejected" in disc.detail \
        and "1 persona-only file" in disc.detail, disc.detail


def test_genuinely_broken_action_file_still_fails():
    # Has TOOL_SCHEMAS but no TOOLS / TOOL_GROUP -> a real rejection.
    checks = _run_in_dir({
        "broken_action_probe.py":
            'TOOL_SCHEMAS = [{"name": "x", "description": "d", "parameters": {"type": "object"}}]\n',
        "persona_only_probe2.py":
            'PERSONAS = [{"id": "probe-persona-2", "name": "Probe2", "hex": "#ff3b3b"}]\n',
    })
    bad = checks["tools.rejected.broken_action_probe.py"]
    assert bad.status == doctor.FAIL, bad.status
    assert "__not_an_action__" not in (bad.detail or ""), bad.detail
    # ...and the persona file next to it is still fine.
    assert checks["tools.personas.persona_only_probe2.py"].status == doctor.OK
    disc = checks["tools.discovery"]
    assert disc.status == doctor.WARN and "1 rejected" in disc.detail, (disc.status, disc.detail)


def test_template_ui_example_runs_every_branch_headless():
    import importlib.util
    path = (Path(__file__).resolve().parent.parent / "jarvis-cli" / "jarvis"
            / "actions" / "_template.py")
    spec = importlib.util.spec_from_file_location("jarvis_tmpl_ui", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    handler = m.TOOLS["example_ui_demo"]

    class Ctx:
        ui = "web"

    for kind in m._UI_KINDS:
        r = handler({"kind": kind, "message": "hi"}, Ctx())
        assert r.get("ok") is True and r["kind"] == kind and r["surface"] == "web", (kind, r)

    # Nobody is watching: every question must come back as its SAFE default.
    assert handler({"kind": "confirm"}, Ctx())["confirmed"] is False
    assert handler({"kind": "choose"}, Ctx())["picked"] == "green"
    assert handler({"kind": "prompt"}, Ctx())["text"] == "untitled"
    answers = handler({"kind": "form"}, Ctx())["answers"]
    assert set(answers) == {"title", "count", "mode", "notes", "dry_run"}, answers

    # Bad / missing input asks instead of guessing; the context arg is optional.
    for bad_args in ({"kind": "nope"}, {}, None):
        assert handler(bad_args).get("needs_clarification"), bad_args
    assert handler({"kind": "toast"})["surface"] == "unknown"

    # The whole template is still a valid tool file for the real loader.
    rec = tool_loader._validate(m, "_template.py", lambda *_a, **_k: None)
    assert rec.valid, rec.error
    assert "example_ui_demo" in rec.tools


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
