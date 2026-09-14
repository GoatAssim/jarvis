"""Tests for jarvis/actions/dev_agent.py's tool_dev_agent orchestration
loop (§3.6 plan §4.4, §10): plan -> write -> install -> run -> fix, with
_plan_project/_install_dependencies/_run_project/_fix_files monkeypatched
so this never actually shells out or calls a real AI provider.

dev_agent_sandbox.new_project_dir still runs for real (against a
per-test temp PROJECTS_ROOT) and _write_files still runs for real too,
since the sandbox-rejection test specifically needs the real
resolve_within() containment check, not a mocked one.

No test framework dependency — plain asserts, runnable directly:

    python3 tests/test_dev_agent.py
"""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import dev_agent  # noqa: E402
from jarvis import dev_agent_sandbox  # noqa: E402

_tmp_roots = []  # cleaned up at the end of the run


def _tmp_root():
    d = Path(tempfile.mkdtemp(prefix="dev_agent_test_"))
    _tmp_roots.append(d)
    return d


class FakeContext:
    """Minimal stand-in for tools.ToolContext (§2.2 of the plan): records
    every emitted event (so a test can assert on the exact live stream,
    not just the returned `steps`) and lets round_budget_remaining be
    stubbed per-test."""

    def __init__(self, round_budget_remaining=None):
        self.events = []
        self._round_budget_remaining = round_budget_remaining or (lambda: 5)

    def round_budget_remaining(self):
        return self._round_budget_remaining()

    def emit_event(self, job_id, seq, phase, status, **fields):
        event = {"job_id": job_id, "seq": seq, "phase": phase, "status": status, **fields}
        self.events.append(event)
        return event


def _plan(files=None, deps=None, run_command="python app.py"):
    files = files if files is not None else ["app.py"]
    return {
        "files": list(files),
        "dependencies": list(deps) if deps is not None else [],
        "run_command": run_command,
        "files_content": {f: f"# content for {f}\n" for f in files},
    }


_OK_SUBPROC = {"exit_code": 0, "stdout_tail": "ok", "stderr_tail": ""}
_FAIL_SUBPROC_UNRECOGNIZED = {
    "exit_code": 1,
    "stdout_tail": "",
    "stderr_tail": "totally unrecognized failure text, no file, no module",
}


def test_happy_path_phase_sequence_and_monotonic_seq():
    plan = _plan(files=["app.py"], deps=["flask"])
    ctx = FakeContext()
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_install_dependencies", return_value=(True, dict(_OK_SUBPROC))), \
         mock.patch.object(dev_agent, "_run_project", return_value=(True, dict(_OK_SUBPROC))):
        result = dev_agent.tool_dev_agent({"description": "a cat fact app"}, context=ctx)

    assert result["ok"] is True
    assert result["run_command"] == "python app.py"
    assert result["total_attempts"] == 0

    phases = [(e["phase"], e["status"]) for e in result["steps"]]
    assert phases == [
        ("plan", "start"), ("plan", "ok"),
        ("write", "start"), ("write", "ok"),
        ("install", "start"), ("install", "ok"),
        ("run", "start"), ("run", "ok"),
        ("done", "ok"),
    ], phases

    seqs = [e["seq"] for e in result["steps"]]
    assert seqs == list(range(len(seqs))), seqs

    # The live stream (ctx.events, what a browser would have seen as
    # JARVIS_MEDIA lines) and the persisted `steps` must be the exact
    # same event objects, not two independently-built representations
    # that could drift apart (see dev_agent_events.emit's docstring).
    assert ctx.events == result["steps"]


def test_happy_path_skips_install_phase_when_no_dependencies():
    plan = _plan(files=["app.py"], deps=[])
    ctx = FakeContext()
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=(True, dict(_OK_SUBPROC))):
        result = dev_agent.tool_dev_agent({"description": "a tiny script"}, context=ctx)

    phases = [e["phase"] for e in result["steps"]]
    assert "install" not in phases
    assert result["ok"] is True


def test_fix_loop_exactly_one_fix_pair_between_two_run_pairs():
    plan = _plan(files=["app.py"], deps=[])
    run_results = [
        (False, dict(_FAIL_SUBPROC_UNRECOGNIZED)),
        (True, dict(_OK_SUBPROC)),
    ]

    def fake_run_project(project_dir, run_command, plan=None):
        return run_results.pop(0)

    ctx = FakeContext()
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_run_project", side_effect=fake_run_project), \
         mock.patch.object(dev_agent, "_fix_files", return_value=(True, "rewrote app.py")):
        result = dev_agent.tool_dev_agent({"description": "a thing that fails once"}, context=ctx)

    assert result["ok"] is True
    assert result["total_attempts"] == 1

    phases = [(e["phase"], e["status"]) for e in result["steps"]]
    assert phases == [
        ("plan", "start"), ("plan", "ok"),
        ("write", "start"), ("write", "ok"),
        ("run", "start"), ("run", "fail"),
        ("fix", "start"), ("fix", "ok"),
        ("run", "start"), ("run", "ok"),
        ("done", "ok"),
    ], phases

    # Exactly one fix start/ok pair, sitting strictly between the failing
    # run pair and the succeeding run pair.
    run_idxs = [i for i, p in enumerate(phases) if p[0] == "run"]
    fix_idxs = [i for i, p in enumerate(phases) if p[0] == "fix"]
    assert len(fix_idxs) == 2
    assert phases[fix_idxs[0]] == ("fix", "start")
    assert phases[fix_idxs[1]] == ("fix", "ok")
    assert run_idxs[1] < fix_idxs[0] < fix_idxs[1] < run_idxs[2]


def test_budget_exhaustion_caps_max_attempts_and_sets_reason():
    plan = _plan(files=["app.py"], deps=[])
    ctx = FakeContext(round_budget_remaining=lambda: 1)
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=(False, dict(_FAIL_SUBPROC_UNRECOGNIZED))), \
         mock.patch.object(dev_agent, "_fix_files", return_value=(True, "attempted a fix")):
        result = dev_agent.tool_dev_agent({"description": "a thing that never recovers"}, context=ctx)

    assert result["ok"] is False
    assert result["reason"] == "budget_exhausted"

    fix_starts = [e for e in result["steps"] if e["phase"] == "fix" and e["status"] == "start"]
    assert len(fix_starts) == 1, "budget of 1 must cap the loop to exactly one fix attempt"
    assert fix_starts[0]["max_attempts"] == 1, "must derive max_attempts from the stubbed budget, not the hardcoded ceiling"


def test_budget_remaining_raising_falls_back_to_ceiling_not_a_crash():
    plan = _plan(files=["app.py"], deps=[])

    class BrokenBudgetContext(FakeContext):
        def round_budget_remaining(self):
            raise RuntimeError("budget unavailable")

    ctx = BrokenBudgetContext()
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=(True, dict(_OK_SUBPROC))):
        result = dev_agent.tool_dev_agent({"description": "a thing"}, context=ctx)

    # A broken budget callable must never take the whole call down —
    # it should just fall back to the hard ceiling (dev_agent.tool_dev_agent
    # never raises, per its own docstring contract).
    assert result["ok"] is True


def test_sandbox_rejection_isolates_fault_other_files_still_written():
    plan = _plan(files=["app.py", "../evil.py", "templates/index.html"], deps=[])
    ctx = FakeContext()
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=(True, dict(_OK_SUBPROC))):
        result = dev_agent.tool_dev_agent({"description": "a thing with one bad path"}, context=ctx)

    write_events = [e for e in result["steps"] if e["phase"] == "write"]
    fails = [e for e in write_events if e["status"] == "fail"]
    oks = {e["path"] for e in write_events if e["status"] == "ok"}

    assert len(fails) == 1
    assert fails[0]["path"] == "../evil.py"
    assert "error" in fails[0]
    # Every OTHER planned file still got written despite the one rejection —
    # fault isolation, not an all-or-nothing write phase.
    assert oks == {"app.py", "templates/index.html"}
    # And the rest of the job still proceeds and can still succeed overall.
    assert result["ok"] is True


def test_missing_description_returns_error_without_raising():
    result = dev_agent.tool_dev_agent({"description": "   "}, context=None)
    assert result.get("error")


def test_plan_failure_short_circuits_before_any_write():
    ctx = FakeContext()
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(None, "planner exploded")):
        result = dev_agent.tool_dev_agent({"description": "a thing"}, context=ctx)

    assert result["ok"] is False
    assert result["reason"] == "plan_failed"
    assert result["last_error"] == "planner exploded"
    phases = [e["phase"] for e in result["steps"]]
    assert "write" not in phases


def test_works_without_a_context_at_all():
    # tool_dev_agent must degrade gracefully with context=None (e.g. called
    # directly from a test or an older call site) rather than requiring the
    # §2 context-injection mechanism to exist.
    plan = _plan(files=["app.py"], deps=[])
    with mock.patch.object(dev_agent_sandbox, "PROJECTS_ROOT", _tmp_root()), \
         mock.patch.object(dev_agent, "_plan_project", return_value=(plan, None)), \
         mock.patch.object(dev_agent, "_run_project", return_value=(True, dict(_OK_SUBPROC))):
        result = dev_agent.tool_dev_agent({"description": "a thing"}, context=None)

    assert result["ok"] is True
    assert result["steps"], "steps must still be recorded even with no context to emit through"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
        print(f"\n{len(tests)} passed")
    finally:
        for d in _tmp_roots:
            shutil.rmtree(d, ignore_errors=True)
