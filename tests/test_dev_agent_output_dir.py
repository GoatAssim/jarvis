"""BUG-3 regression: dev_agent ignored the folder the user asked for, and
BUG-4: raw provider reasoning was saved/shown while thinking was "off".

BUG-3 -- `output_dir` is validated up front, the build still happens in the
sandbox, and only a PASSED build is copied to the destination. Covers
validation (relative, root, home, ~/.jarvis, system dir, file, non-empty),
delivery (copies, skips node_modules/.venv/symlinks, never overwrites) and the
tool loop (rejects before any AI call, delivers only on success, a failed copy
does not fail a working build).

BUG-4 -- reasoning.should_save_trace.

    python3 tests/test_dev_agent_output_dir.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import dev_agent  # noqa: E402
from jarvis import dev_agent_sandbox as sb  # noqa: E402
from jarvis import reasoning  # noqa: E402

_tmp = []


def _dir():
    d = Path(tempfile.mkdtemp(prefix="dev_agent_od_")).resolve()
    _tmp.append(d)
    return d


def _fake_home():
    """A throwaway HOME so ~/.jarvis rules are tested without touching the real one."""
    home = _dir()
    return home


def _validate(raw, home=None, projects_root=None):
    home = home or _fake_home()
    projects_root = projects_root or (home / ".jarvis" / "dev_agent_projects")
    with mock.patch.object(Path, "home", return_value=home), \
         mock.patch.object(sb, "PROJECTS_ROOT", projects_root):
        return sb.validate_output_dir(raw)


def test_valid_new_folder_under_a_normal_parent():
    base = _dir()
    dest, err = _validate(str(base / "clock"))
    assert err is None and dest == (base / "clock").resolve()


def test_existing_empty_folder_is_accepted():
    base = _dir()
    (base / "empty").mkdir()
    dest, err = _validate(str(base / "empty"))
    assert err is None and dest is not None


def test_rejects_relative_and_empty_and_nul():
    for bad in ("", "   ", "clock", "./clock", "..\\x00x", None, 5):
        dest, err = _validate(bad)
        assert dest is None and err, bad


def test_rejects_filesystem_root():
    root = Path(Path(tempfile.gettempdir()).resolve().anchor)
    dest, err = _validate(str(root))
    assert dest is None and "root" in err


def test_rejects_home_and_its_parents():
    home = _fake_home()
    for p in (home, home.parent):
        dest, err = _validate(str(p), home=home)
        assert dest is None and "home" in err, p


def test_rejects_inside_jarvis_state_folder():
    home = _fake_home()
    for p in (home / ".jarvis", home / ".jarvis" / "dev_agent_projects" / "x", home / ".jarvis" / "memory"):
        dest, err = _validate(str(p), home=home)
        assert dest is None and ".jarvis" in err, p


def test_rejects_system_folder():
    home = _fake_home()
    sysroot = _dir()
    with mock.patch.dict(os.environ, {"SystemRoot": str(sysroot)}):
        dest, err = _validate(str(sysroot / "sub"), home=home)
    assert dest is None and "system folder" in err


def test_rejects_existing_file_and_non_empty_folder():
    base = _dir()
    f = base / "a.txt"
    f.write_text("x")
    dest, err = _validate(str(f))
    assert dest is None and "file" in err
    (base / "full").mkdir()
    (base / "full" / "keep.txt").write_text("mine")
    dest, err = _validate(str(base / "full"))
    assert dest is None and "not empty" in err


def _project():
    p = _dir()
    (p / "server.js").write_text("// s\n")
    (p / "public").mkdir()
    (p / "public" / "index.html").write_text("<p>hi</p>")
    (p / "node_modules" / "express").mkdir(parents=True)
    (p / "node_modules" / "express" / "i.js").write_text("x")
    (p / ".venv").mkdir()
    (p / ".venv" / "pyvenv.cfg").write_text("x")
    (p / "__pycache__").mkdir()
    (p / "__pycache__" / "a.pyc").write_text("x")
    return p


def test_deliver_copies_files_and_leaves_dependency_dirs_behind():
    proj, dest = _project(), _dir() / "out"
    ok, info = sb.deliver(proj, dest)
    assert ok, info
    assert (dest / "server.js").read_text() == "// s\n"
    assert (dest / "public" / "index.html").exists()
    assert not (dest / "node_modules").exists() and not (dest / ".venv").exists()
    assert not (dest / "__pycache__").exists()
    assert info["files_copied"] == 2
    assert info["not_copied"] == [".venv", "__pycache__", "node_modules"]


def test_deliver_never_copies_symlinks():
    proj, dest = _project(), _dir() / "out"
    secret = _dir() / "secret.txt"
    secret.write_text("private")
    try:
        os.symlink(secret, proj / "leak.txt")
        os.symlink(secret.parent, proj / "leakdir")
    except (OSError, NotImplementedError, AttributeError):
        return  # symlinks unavailable here (e.g. Windows without privilege)
    ok, info = sb.deliver(proj, dest)
    assert ok, info
    assert not (dest / "leak.txt").exists() and not (dest / "leakdir").exists()
    assert (dest / "server.js").exists()


def test_deliver_refuses_a_destination_that_filled_up_since_validation():
    proj, dest = _project(), _dir()
    (dest / "late.txt").write_text("someone's file")
    ok, info = sb.deliver(proj, dest)
    assert ok is False and "not empty" in info["error"]
    assert (dest / "late.txt").read_text() == "someone's file"
    assert not (dest / "server.js").exists()


# ---- the tool loop --------------------------------------------------------

class _Ctx:
    def __init__(self):
        self.events = []

    def round_budget_remaining(self):
        return 5

    def emit_event(self, job_id, seq, phase, status, **f):
        e = {"job_id": job_id, "seq": seq, "phase": phase, "status": status, **f}
        self.events.append(e)
        return e


def _plan():
    return {"files": ["server.js"], "dependencies": [], "run_command": "node server.js",
            "files_content": {"server.js": "// x\n"}}


_OK = (True, {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""})
_BAD = (False, {"exit_code": 1, "stdout_tail": "", "stderr_tail": "totally unrecognized"})


def test_bad_output_dir_is_rejected_before_any_ai_call():
    with mock.patch.object(dev_agent_sandbox_root(), "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project") as plan:
        r = dev_agent.tool_dev_agent({"description": "x", "output_dir": "relative/path"}, context=_Ctx())
    assert "error" in r and "output_dir rejected" in r["error"]
    plan.assert_not_called()


def dev_agent_sandbox_root():
    return sb


def test_successful_build_is_delivered_with_a_deliver_phase():
    dest = _dir() / "clock"
    ctx = _Ctx()
    with mock.patch.object(sb, "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(_plan(), None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=_OK):
        r = dev_agent.tool_dev_agent({"description": "clock", "output_dir": str(dest)}, context=ctx)
    assert r["ok"] is True and r["delivered"] is True and r["output_dir"] == str(dest.resolve())
    assert (dest / "server.js").exists()
    phases = [(s["phase"], s["status"]) for s in r["steps"]]
    assert ("deliver", "start") in phases and ("deliver", "ok") in phases
    assert phases.index(("deliver", "ok")) < phases.index(("done", "ok"))
    assert Path(r["project_dir"]).exists(), "sandbox copy is kept"


def test_failed_build_is_not_delivered():
    dest = _dir() / "clock"
    with mock.patch.object(sb, "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(_plan(), None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=_BAD), \
         mock.patch.object(dev_agent, "_fix_files", return_value=(False, "nope")):
        r = dev_agent.tool_dev_agent({"description": "clock", "output_dir": str(dest)}, context=_Ctx())
    assert r["ok"] is False and r["delivered"] is False
    assert not dest.exists()
    assert not [s for s in r["steps"] if s["phase"] == "deliver"]


def test_failed_copy_does_not_fail_a_working_build():
    dest = _dir() / "clock"
    with mock.patch.object(sb, "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(_plan(), None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=_OK), \
         mock.patch.object(sb, "deliver", return_value=(False, {"error": "disk full"})):
        r = dev_agent.tool_dev_agent({"description": "clock", "output_dir": str(dest)}, context=_Ctx())
    assert r["ok"] is True and r["delivered"] is False and r["delivery_error"] == "disk full"


def test_no_output_dir_behaves_exactly_as_before():
    with mock.patch.object(sb, "PROJECTS_ROOT", _dir()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(_plan(), None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=_OK):
        r = dev_agent.tool_dev_agent({"description": "clock"}, context=_Ctx())
    assert r["ok"] is True and "delivered" not in r and "output_dir" not in r
    assert not [s for s in r["steps"] if s["phase"] == "deliver"]


def test_schema_exposes_output_dir_as_optional():
    props = dev_agent.TOOL_SCHEMAS[0]["parameters"]["properties"]
    assert "output_dir" in props
    assert "output_dir" not in dev_agent.TOOL_SCHEMAS[0]["parameters"]["required"]


# ---- BUG-4 ----------------------------------------------------------------

def test_reasoning_is_not_saved_when_thinking_is_off():
    assert reasoning.should_save_trace("off", "we must not mention tools") is False
    assert reasoning.should_save_trace(None, "text") is False  # default level is off
    assert reasoning.should_save_trace("none", "text") is False  # alias of off


def test_reasoning_is_saved_when_thinking_is_on():
    for level in ("low", "medium", "high", "on", True):
        assert reasoning.should_save_trace(level, "some reasoning") is True, level


def test_empty_text_or_save_disabled_never_saves():
    assert reasoning.should_save_trace("high", "") is False
    assert reasoning.should_save_trace("high", None) is False
    assert reasoning.should_save_trace("high", "text", save=False) is False


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, "->", repr(e))
    for d in _tmp:
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(1 if failed else 0)
