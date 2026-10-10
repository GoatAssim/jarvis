"""L.2.4: the Logs Conversation view can only show an execution event if the
backend wrote a line for it. These pin the lines it now writes -- retry /
cooldown, forced ending, confirmation, task + scheduled-run start/finish --
as "info" entries whose data carries {"event": <name>, ...}.

Run: python3 tests/test_l2_4_log_events.py
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (ai_client, ai_config, ai_providers, conversations, logs,  # noqa: E402
                    scheduler, task_runner, tasks, tool_safety, tools as system_tools)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


CONV = "abcdef0123456789"


@contextmanager
def _logdir():
    orig = (logs.JARVIS_DIR, logs.LOG_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        logs.JARVIS_DIR, logs.LOG_DIR = Path(tmp), Path(tmp) / "logs"
        try:
            yield
        finally:
            logs.JARVIS_DIR, logs.LOG_DIR = orig


def _events(conv=CONV, name=None):
    out = []
    for e in logs.read_entries(conv) or []:
        d = e.get("data")
        if e.get("direction") == "info" and isinstance(d, dict) and "event" in d:
            if name is None or d["event"] == name:
                out.append(e)
    return out


# ---------------------------------------------------------------------------
def test_log_event_and_overrides():
    with _logdir():
        os.environ["JARVIS_TASK_ID"], os.environ["JARVIS_TASK_LABEL"] = "env-id", "From env"
        try:
            logs.log_event(CONV, "task", {"phase": "started"})
            logs.log_event(CONV, "task", {"phase": "finished"}, source="task", task_id="t1", task_label="Override")
        finally:
            os.environ.pop("JARVIS_TASK_ID", None)
            os.environ.pop("JARVIS_TASK_LABEL", None)
        a, b = _events()
        check("log_event writes an info line carrying the event name", a["direction"] == "info" and a["data"] == {"event": "task", "phase": "started"}, a)
        check("with no override the env task label still applies", a["task_id"] == "env-id" and a["task_label"] == "From env", a)
        check("an explicit task/source override wins over the env",
              (b["source"], b["task_id"], b["task_label"]) == ("task", "t1", "Override"), b)
        check("log_event never raises on a bad conversation id", logs.log_event("../nope", "task") is None)


# ---------------------------------------------------------------------------
def _run_gated(on_confirm, conv_id=CONV, args=None):
    original = system_tools.execute_tool
    system_tools.execute_tool = lambda name, arguments=None, verbosity=None, context=None: {"ok": True}
    try:
        ex = ai_client._make_tool_executor(on_tool_call=None, schemas=None,
                                           on_confirm_request=on_confirm, conv_id=conv_id)
        return ex("run_shell", args or {"command": "del file.txt"})
    finally:
        system_tools.execute_tool = original


def test_confirmation_events():
    check("precondition: run_shell is confirm-gated", tool_safety.requires_confirmation("run_shell"))
    with _logdir():
        _run_gated(lambda *a, **k: True)
        outcomes = [e["data"]["outcome"] for e in _events(name="confirmation")]
        check("approved: 'asked' is written first, then 'approved'", outcomes == ["asked", "approved"], outcomes)
        check("the event names the tool", all(e["data"]["tool"] == "run_shell" for e in _events(name="confirmation")))
    with _logdir():
        r = _run_gated(lambda *a, **k: False)
        outcomes = [e["data"]["outcome"] for e in _events(name="confirmation")]
        check("declined: 'asked' then 'declined', and the tool did not run", outcomes == ["asked", "declined"] and r.get("cancelled"), (outcomes, r))
    with _logdir():
        r = _run_gated(None)
        outcomes = [e["data"]["outcome"] for e in _events(name="confirmation")]
        check("no confirmation channel: one 'no_channel' line, tool not run", outcomes == ["no_channel"] and r.get("cancelled"), (outcomes, r))
    with _logdir():
        _run_gated(lambda *a, **k: True, args={"command": "dir"})
        check("an allow-listed read-only command never asked, so nothing is logged", _events(name="confirmation") == [])
    with _logdir():
        _run_gated(lambda *a, **k: True, conv_id=None)
        check("no conversation id: nothing written, nothing raised", not logs.LOG_DIR.exists() or not list(logs.LOG_DIR.glob("*")))


# ---------------------------------------------------------------------------
@contextmanager
def _ask_env(adapter, sleeps):
    import time as _time
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS), ai_client.time.sleep,
            conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
            conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    cfg = {"persona": {}, "providers": [{"name": "fake1", "type": "fake", "enabled": True,
                                         "api_keys": ["k1"], "model": "m"}],
           "defaults": {"tools_enabled": False, "prompt_mode": "full", "max_429_wait_seconds": 30}}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        ai_config.load_ai_config = lambda: cfg
        ai_providers.ADAPTERS["fake"] = adapter
        ai_client.time.sleep = lambda s: sleeps.append(s)   # never really wait
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, _, ai_client.time.sleep, conversations.JARVIS_DIR,
             conversations.CONV_DIR, conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def test_retry_cooldown_events():
    calls = []
    sleeps = []

    def adapter(resolved, messages, timeout, **kw):
        calls.append(1)
        if len(calls) == 1:
            return ai_providers.AIResult(False, error="rate limited or quota exceeded. Please retry in 4s",
                                         kind=ai_providers.KIND_KEY)
        return ai_providers.AIResult(True, text="fine")

    with _ask_env(adapter, sleeps) as conv:
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
        evs = _events(conv, "retry_cooldown")
        check("the retry still worked", r.ok and r.text == "fine" and len(sleeps) == 1, (r.ok, sleeps))
        check("a line is written before the wait, saying how long", evs and evs[0]["data"].get("wait_s") == sleeps[0] and evs[0]["data"].get("same_key") is True, evs[:1])
        check("a second line records that the retry succeeded", len(evs) == 2 and evs[1]["data"] == {"event": "retry_cooldown", "phase": "after", "ok": True, "error": ""}, evs[1:])
        check("both lines carry the attempt's key label", all(e["provider"] for e in evs), [e["provider"] for e in evs])


def test_forced_ending_events():
    sleeps = []

    def adapter(resolved, messages, timeout, **kw):
        return ai_providers.AIResult(False, error="ran out of steps", kind=ai_providers.KIND_BUDGET)

    with _ask_env(adapter, sleeps) as conv:
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
        evs = _events(conv, "forced_ending")
        check("a step-budget stop reports ending='forced'", r.ending == "forced", r.ending)
        check("...and writes one forced_ending line saying so", len(evs) == 1 and evs[0]["data"]["ending"] == "forced" and evs[0]["data"]["reason"], evs)

    def cutoff(resolved, messages, timeout, **kw):
        return ai_providers.AIResult(False, error="cut off", kind=ai_providers.KIND_CUTOFF)

    with _ask_env(cutoff, sleeps) as conv:
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
        evs = _events(conv, "forced_ending")
        check("a provider cutoff writes ending='cutoff'", r.ending == "cutoff" and [e["data"]["ending"] for e in evs] == ["cutoff"], (r.ending, evs))

    def plain(resolved, messages, timeout, **kw):
        return ai_providers.AIResult(True, text="all good")

    with _ask_env(plain, sleeps) as conv:
        ai_client.ask("hello", commands=[], conversation_id=conv)
        check("a normal answer writes no forced_ending line", _events(conv, "forced_ending") == [])


# ---------------------------------------------------------------------------
def test_task_step_events():
    with _logdir():
        orig_dir = getattr(tasks, "TASKS_DIR", None)
        with tempfile.TemporaryDirectory() as tmp:
            for attr in ("TASKS_DIR", "TASK_DIR"):
                if hasattr(tasks, attr):
                    setattr(tasks, attr, Path(tmp))
            t = tasks.create("say hi", plan=["greet"], conv_id=CONV, title="Greeter")

            def fake_ask(task, prompt):
                return True, 'done\n', None

            task_runner.run_step(t, ask=fake_ask)
            evs = _events(CONV, "task")
            phases = [e["data"]["phase"] for e in evs]
            check("a step writes a started line then a finished line", phases == ["started", "finished"], phases)
            check("both lines carry the task id, label and source, from the supervisor",
                  all((e["task_id"], e["task_label"], e["source"]) == (t["id"], "Greeter", "task") for e in evs), evs)
            check("the finished line carries the task's status", evs[-1]["data"].get("status") in (tasks.STATUS_DONE, tasks.STATUS_PENDING), evs[-1]["data"])
            if orig_dir is not None:
                tasks.TASKS_DIR = orig_dir

    with _logdir():
        with tempfile.TemporaryDirectory() as tmp:
            for attr in ("TASKS_DIR", "TASK_DIR"):
                if hasattr(tasks, attr):
                    setattr(tasks, attr, Path(tmp))
            t = tasks.create("no log home", plan=["x"], title="Orphan")   # no conv_id
            task_runner.run_step(t, ask=lambda task, prompt: (True, "ok", None))
            check("a task with no conversation has nowhere to log: nothing is written, nothing raised",
                  not logs.LOG_DIR.exists() or not list(logs.LOG_DIR.glob("*")))


def test_scheduled_run_events():
    job = {"id": "job1", "title": "Morning briefing", "action": {"type": "ask", "prompt": "hi"}}
    orig = scheduler._do_ask_run
    try:
        for result, want in (({"ok": True, "conv_id": CONV}, "ok"),
                             ({"ok": False, "error": "boom", "conv_id": CONV}, "failed"),
                             ({"ok": False, "over_budget": True, "conv_id": CONV}, "stopped_over_budget")):
            with _logdir():
                scheduler._do_ask_run = lambda j, a, _r=result: _r
                got = scheduler._do_ask(job, job["action"])
                evs = _events(CONV, "task")
                check(f"_do_ask returns the run's result unchanged ({want})", got is result)
                check(f"a finished line records status={want}", [e["data"].get("status") for e in evs] == [want], evs)
                check("it is labelled with the job and the scheduler source",
                      evs and (evs[0]["task_id"], evs[0]["task_label"], evs[0]["source"]) == ("job1", "Morning briefing", "scheduler"), evs)
    finally:
        scheduler._do_ask_run = orig


for fn in (test_log_event_and_overrides, test_confirmation_events, test_retry_cooldown_events,
           test_forced_ending_events, test_task_step_events, test_scheduled_run_events):
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
