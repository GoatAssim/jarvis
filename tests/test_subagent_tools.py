"""Tests for subagent_tools.py — the tools that make subagents.py reachable
from an ask. No network, no real API key: task_runner._spawn_ask is
monkeypatched, the same technique test_tasks.py uses via the injectable
`ask` parameter, except here through the module attribute since the tools
call task_runner.run_step() with no injection point of their own (by
design — see subagent_tools.py's docstring on staying dumb wrappers).

Run with `python3 tests/test_subagent_tools.py`.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import subagents, tasks, task_runner, subagent_tools  # noqa: E402

MARKER = task_runner.CONTROL_MARKER


def _isolate():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-subagent-tools-test-"))
    tasks.TASKS_DIR = tmp / "tasks"
    subagents.CONFIG_FILE = tmp / "subagents.json"
    os.environ.pop("JARVIS_TASK_ID", None)
    return tmp


def _configure_pools():
    subagents.save_config({
        "max_concurrent": 3,
        "key_pools": {
            "advocate": {"provider": "anthropic", "api_keys": ["sk-adv"]},
            "skeptic": {"provider": "anthropic", "api_keys": ["sk-skep"]},
        },
        "agents": {},
    })


def _one_shot_ask(task, prompt):
    """Finishes any task in exactly one step, no planning phase — spawn
    with an explicit goal but skip the plan by pre-seeding one where tests
    don't care about the plan/execute split."""
    verdict = "advocate says yes" if task.get("agent") == "advocate" else "skeptic says no"
    reply = verdict + "\n" + MARKER + json.dumps(
        {"step": "done", "summary": verdict, "task": "complete"})
    return True, reply, None


# ---------------------------------------------------------------------------


def test_spawn_requires_role_and_goal():
    _isolate()
    out = subagent_tools.tool_spawn_subagent({})
    assert out.get("needs_clarification") is True
    out = subagent_tools.tool_spawn_subagent({"role": "coder"})
    assert out.get("needs_clarification") is True
    print("ok  spawn requires role+goal")


def test_spawn_without_pool_is_an_error_not_a_crash():
    _isolate()
    out = subagent_tools.tool_spawn_subagent({"role": "coder", "goal": "fix it"})
    assert "error" in out
    assert "never use the main" in out["error"]
    print("ok  spawn without pool errors cleanly")


def test_spawn_success_shape():
    _isolate()
    _configure_pools()
    out = subagent_tools.tool_spawn_subagent({"role": "advocate", "goal": "g"})
    assert out["role"] == "advocate"
    assert tasks.is_valid_id(out["task_id"])
    assert out["status"] == tasks.STATUS_PENDING
    print("ok  spawn success shape")


def test_spawn_inherits_parent_from_env():
    """spawn_subagent defaults parent_id to JARVIS_TASK_ID — the env var
    task_runner sets on every step's subprocess — so a subagent spawned
    from inside a running task is grouped under it automatically."""
    _isolate()
    _configure_pools()
    parent = tasks.create("outer job")
    os.environ["JARVIS_TASK_ID"] = parent["id"]
    try:
        out = subagent_tools.tool_spawn_subagent({"role": "advocate", "goal": "g"})
        assert out["parent_id"] == parent["id"]
        kid = tasks.load(out["task_id"])
        assert kid["parent_id"] == parent["id"]
    finally:
        os.environ.pop("JARVIS_TASK_ID", None)

    # An explicit parent_id in args wins over the env var.
    other = tasks.create("other parent")
    out2 = subagent_tools.tool_spawn_subagent(
        {"role": "advocate", "goal": "g", "parent_id": other["id"]})
    assert out2["parent_id"] == other["id"]
    print("ok  spawn parent linkage")


def test_run_subagents_by_task_ids():
    _isolate()
    _configure_pools()
    task_runner._spawn_ask = _one_shot_ask
    try:
        a = subagent_tools.tool_spawn_subagent({"role": "advocate", "goal": "g"})
        b = subagent_tools.tool_spawn_subagent({"role": "skeptic", "goal": "g"})
        out = subagent_tools.tool_run_subagents(
            {"task_ids": [a["task_id"], b["task_id"]]})
        assert out["all_finished"] is True
        statuses = {r["id"]: r["status"] for r in out["results"]}
        assert statuses[a["task_id"]] == tasks.STATUS_DONE
        assert statuses[b["task_id"]] == tasks.STATUS_DONE
        assert "advocate says yes" in out["summary"]
        assert "skeptic says no" in out["summary"]
    finally:
        task_runner._spawn_ask = task_runner._spawn_ask  # no real teardown needed; new process per test run
    print("ok  run_subagents by task_ids")


def test_run_subagents_by_parent_id():
    _isolate()
    _configure_pools()
    task_runner._spawn_ask = _one_shot_ask
    parent = tasks.create("outer")
    os.environ["JARVIS_TASK_ID"] = parent["id"]
    try:
        subagent_tools.tool_spawn_subagent({"role": "advocate", "goal": "g"})
        subagent_tools.tool_spawn_subagent({"role": "skeptic", "goal": "g"})
        out = subagent_tools.tool_run_subagents({"parent_id": parent["id"]})
        assert out["all_finished"] is True
        assert len(out["results"]) == 2
        # Grouped-by-parent path goes through subagents.summary_for_parent,
        # which includes an escalation line when relevant — here the two
        # verdicts disagree (yes vs no), so it must be flagged.
        assert "NEEDS A HUMAN" in out["summary"]
    finally:
        os.environ.pop("JARVIS_TASK_ID", None)
    print("ok  run_subagents by parent_id (with disagreement escalation)")


def test_run_subagents_needs_something_to_run():
    _isolate()
    out = subagent_tools.tool_run_subagents({})
    assert "error" in out
    out = subagent_tools.tool_run_subagents({"parent_id": "t_doesnotexist"})
    assert "error" in out
    print("ok  run_subagents requires ids or a real parent")


def test_run_subagents_respects_round_cap():
    """A task that never reaches a terminal state must not spin forever —
    max_rounds bounds it, and the tool reports partial completion honestly
    rather than claiming all_finished."""
    _isolate()
    _configure_pools()

    def continues_forever(task, prompt):
        # Explicit task="continue" every time, so _apply_step never treats
        # this as finished by verdict. A single-step fallback plan would
        # still terminate after one round (running off the end of the plan
        # finishes a task regardless of what the reply said), so this test
        # gives the task an explicit multi-step plan long enough that the
        # round cap — not the plan length — is what stops it.
        return True, "still working" + "\n" + MARKER + json.dumps(
            {"step": "done", "summary": "still going", "task": "continue"}), None
    task_runner._spawn_ask = continues_forever

    task = tasks.create("long job", agent="advocate", plan=["s%d" % i for i in range(20)])
    tasks.save(task)
    out = subagent_tools.tool_run_subagents(
        {"task_ids": [task["id"]], "max_rounds": 3})
    assert out["rounds_run"] == 3
    assert out["all_finished"] is False
    still_pending = tasks.load(task["id"])
    assert still_pending["status"] in tasks.ACTIVE_STATUSES
    print("ok  run_subagents respects max_rounds")


def test_status_tool():
    _isolate()
    _configure_pools()
    task_runner._spawn_ask = _one_shot_ask
    out = subagent_tools.tool_spawn_subagent({"role": "advocate", "goal": "g"})
    status = subagent_tools.tool_subagent_status({"task_id": out["task_id"]})
    assert status["status"] == tasks.STATUS_PENDING
    subagent_tools.tool_run_subagents({"task_ids": [out["task_id"]]})
    status = subagent_tools.tool_subagent_status({"task_id": out["task_id"]})
    assert status["status"] == tasks.STATUS_DONE

    missing = subagent_tools.tool_subagent_status({"task_id": "t_nope0000"})
    assert "error" in missing
    empty = subagent_tools.tool_subagent_status({})
    assert empty.get("needs_clarification") is True
    print("ok  subagent_status")


def test_list_roles_reports_pool_state():
    _isolate()
    _configure_pools()
    out = subagent_tools.tool_list_subagent_roles()
    by_name = {r["name"]: r for r in out["roles"]}
    assert by_name["advocate"]["pool_configured"] is True
    assert by_name["coder"]["pool_configured"] is False
    assert by_name["advocate"]["builtin"] is True
    print("ok  list_subagent_roles reports pool state")


if __name__ == "__main__":
    test_spawn_requires_role_and_goal()
    test_spawn_without_pool_is_an_error_not_a_crash()
    test_spawn_success_shape()
    test_spawn_inherits_parent_from_env()
    test_run_subagents_by_task_ids()
    test_run_subagents_by_parent_id()
    test_run_subagents_needs_something_to_run()
    test_run_subagents_respects_round_cap()
    test_status_tool()
    test_list_roles_reports_pool_state()
    print("\nall subagent_tools tests passed")
