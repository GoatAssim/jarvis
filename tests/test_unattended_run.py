"""L.16 -- closing the unattended-run gap.

Covers, through the REAL tool executor (ai_client._make_tool_executor) with
only the tools' own side effects stubbed:

  1. policy.decide() is enforced on the live path: a scheduled call is denied by
     default, and allowed when the job was approved for that kind of action.
  2. The approval summary (job_risk.assess) is derived from the job's tool
     list, not from its prompt text.
  3. The unattended directive reaches the model's prompt tail when
     JARVIS_SCHEDULED is set, and only then.
  4. The circuit breaker: a `retryable: false` result (or a policy denial) stops
     an unattended run -- no further tool runs, the round budget is emptied.

No network, no live model, no real screen / shutdown.

Run: python3 tests/test_unattended_run.py
"""
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_JOB_APPROVED_KINDS",
             "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL", "JARVIS_POWER_DRY_RUN"):
    os.environ.pop(_var, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_providers, job_risk, policy, scheduler  # noqa: E402
from jarvis import tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Env:
    """Set env vars for one block, restore afterwards."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Stub:
    """Replace system_tools.execute_tool; record which tools really ran."""

    def __init__(self, results=None):
        self.ran = []
        self.results = results or {}

    def __enter__(self):
        self.original = system_tools.execute_tool

        def fake(name, arguments=None, verbosity=None, context=None):
            self.ran.append(name)
            return dict(self.results.get(name, {"ok": True}))

        system_tools.execute_tool = fake
        return self

    def __exit__(self, *exc):
        system_tools.execute_tool = self.original


def executor(confirm=None, budget=None):
    asked = []

    def on_confirm(name, arguments, risk_note=None):
        asked.append(name)
        return confirm if confirm is not None else False

    ex = ai_client._make_tool_executor(
        lambda *a, **k: None, schemas=[], on_confirm_request=on_confirm,
        round_budget=budget)
    return ex, asked


SCHEDULED = {"JARVIS_SCHEDULED": "1"}


# ---------------------------------------------------------------------------
# 1. policy.decide() on the live path
# ---------------------------------------------------------------------------

def test_scheduled_desktop_denied_by_default_through_executor():
    # The incident: a scheduled job's click ran because decide() was never called.
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=None), Stub() as stub:
        ex, _ = executor()
        result = ex("click", {"x": 10, "y": 1050})
    check("scheduled click is not run", "click" not in stub.ran, stub.ran)
    check("scheduled click result is blocked + non-retryable",
          result.get("blocked") is True and result.get("retryable") is False, result)
    check("denial names why", "Stopped" not in result.get("error", "") and
          "click" in result.get("error", ""), result)


def test_approved_desktop_job_may_click():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="desktop"), Stub() as stub:
        ex, asked = executor()
        result = ex("click", {"x": 10, "y": 10})
    check("approved desktop click runs", stub.ran == ["click"], stub.ran)
    check("approved click result is the tool's own", result.get("ok") is True, result)
    check("approval replaced any confirm", asked == [], asked)


def test_desktop_approval_covers_read_probes():
    # The incident job's first step was read_screen; the default rule denies the
    # whole desktop group, so approval has to cover the read-only members too.
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="desktop"), Stub() as stub:
        ex, _ = executor()
        ex("read_screen", {})
        ex("list_windows", {})
    check("read-only desktop tools run under desktop approval",
          stub.ran == ["read_screen", "list_windows"], stub.ran)


def test_unapproved_desktop_read_probe_is_denied_too():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=""), Stub() as stub:
        ex, _ = executor()
        result = ex("read_screen", {})
    check("default deny covers the whole desktop group", stub.ran == [] and result.get("blocked"),
          (stub.ran, result))


def test_power_action_runs_unattended_only_when_approved():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="desktop"), Stub() as stub:
        ex, _ = executor()
        denied = ex("power_action", {"action": "shutdown"})
    check("desktop approval does not cover power_action",
          stub.ran == [] and denied.get("blocked"), (stub.ran, denied))

    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="power"), Stub() as stub:
        ex, asked = executor()
        result = ex("power_action", {"action": "shutdown"})
    check("power approval lets the shutdown run", stub.ran == ["power_action"], (stub.ran, result))
    check("and asks no confirm (power_action is confirm_required)", asked == [], asked)


def test_approval_covers_only_what_was_listed():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="desktop"), Stub() as stub:
        ex, _ = executor()
        shell = ex("run_shell", {"command": "echo hi > out.txt"})
        files = ex("delete_path", {"path": "~/Documents/x.txt"})
    check("desktop approval does not cover run_shell", shell.get("blocked") and "run_shell" not in stub.ran,
          (shell, stub.ran))
    check("nor delete_path", "delete_path" not in stub.ran, stub.ran)


def test_approval_floor_sensitive_path_and_destructive_pattern():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="files,shell"), Stub() as stub:
        ex, _ = executor()
        ssh = ex("write_file", {"path": "~/.ssh/authorized_keys", "content": "x"})
        tree = ex("run_shell", {"command": "rm -rf ~/projects"})
    check("approval does not buy a write to ~/.ssh", ssh.get("blocked") and "write_file" not in stub.ran,
          (ssh, stub.ran))
    check("approval does not buy rm -rf", tree.get("blocked") and "run_shell" not in stub.ran,
          (tree, stub.ran))


def test_approved_ordinary_file_write_runs():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="files"), Stub() as stub:
        ex, _ = executor()
        ex("write_file", {"path": "~/Documents/notes.txt", "content": "hi"})
    check("approved file write inside safe roots runs", stub.ran == ["write_file"], stub.ran)


def test_owner_written_rule_outranks_job_approval():
    custom = {"enabled": True, "safe_roots": [], "audit": False, "rules": [
        {"when": {"context": "scheduled", "tool": "power_action"}, "then": "deny",
         "because": "never power off from a schedule"}]}
    verdict = policy.decide("power_action", {"action": "shutdown"}, context="scheduled",
                            policy=custom, approved_kinds=["power"])
    check("a rule the owner wrote still denies an approved job",
          verdict["decision"] == "deny" and not verdict["approved"], verdict)
    # ...whereas the built-in rule yields.
    verdict = policy.decide("click", {"x": 1, "y": 1}, context="scheduled",
                            policy=policy.load_policy()[0], approved_kinds=["desktop"])
    check("the built-in desktop rule yields to approval",
          verdict["decision"] == "allow" and verdict["approved"], verdict)


def test_interactive_ask_is_unchanged():
    with Env(JARVIS_SCHEDULED=None, JARVIS_CONTEXT=None, JARVIS_JOB_APPROVED_KINDS="desktop,power"), Stub() as stub:
        ex, asked = executor(confirm=True)
        ex("click", {"x": 1, "y": 1})
        ex("power_action", {"action": "lock"})
    check("interactive: click runs with no gate", "click" in stub.ran, stub.ran)
    check("interactive: power_action still asks a human (approved_kinds is ignored)",
          "power_action" in asked, asked)


def test_read_only_shell_and_task_steps_keep_working():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=""), Stub() as stub:
        ex, _ = executor()
        ex("run_shell", {"command": "dir"})
    check("allowlisted read-only shell is not a flagged action", stub.ran == ["run_shell"], stub.ran)
    # A task step: JARVIS_SCHEDULED set, no approved-kinds variable at all.
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=None), Stub() as stub:
        ex, asked = executor(confirm=True)
        ex("write_file", {"path": "~/Documents/n.txt", "content": "x"})
    check("a task step (no approval env) is not newly denied file writes", stub.ran == ["write_file"], stub.ran)


def test_unattended_non_flagged_tool_still_runs():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=""), Stub() as stub:
        ex, _ = executor()
        ex("get_datetime", {})
    check("a harmless tool runs in a scheduled job", stub.ran == ["get_datetime"], stub.ran)


def test_scheduled_wins_over_stray_interactive_context():
    check("JARVIS_SCHEDULED beats JARVIS_CONTEXT=interactive",
          _ctx(JARVIS_SCHEDULED="1", JARVIS_CONTEXT="interactive") == "scheduled")
    check("explicit unattended context enforces too",
          _ctx(JARVIS_SCHEDULED=None, JARVIS_CONTEXT="unattended") == "unattended")
    check("a bare non-tty run is NOT enforced",
          _ctx(JARVIS_SCHEDULED=None, JARVIS_CONTEXT=None) is None)


def _ctx(**values):
    with Env(**values):
        return policy.unattended_context_from_env()


# ---------------------------------------------------------------------------
# 2. Approval summary, from the tool list
# ---------------------------------------------------------------------------

def test_tool_job_summary_is_certain_and_names_the_power_action():
    risk = job_risk.assess({"type": "tool", "tool": "power_action", "args": {"action": "restart"}})
    check("power tool job flags power", risk["kinds"] == ["power"], risk)
    check("says 'will', and the concrete action", risk["certain"] and
          risk["lines"] == ["This job will restart this PC."], risk)


def test_summary_ignores_prompt_words_for_tool_and_command_jobs():
    risk = job_risk.assess({"type": "tool", "tool": "get_datetime", "args": {"note": "shutdown and delete everything"}})
    check("words in args do not flag a harmless tool", risk["kinds"] == [], risk)
    risk = job_risk.assess({"type": "command", "command": "nightly"})
    check("a saved command is flagged as shell", risk["kinds"] == ["shell"], risk)


def test_notify_job_flags_nothing():
    check("notify flags nothing", job_risk.assess({"type": "notify", "message": "shut down the PC"})["kinds"] == [])


def test_ask_job_summary_comes_from_routed_tools():
    prompt = ("Check the screen to see if usage is exhausted. If it is, do not type anything "
              "and shutdown the PC. Otherwise type 'continue' and press Enter, then shutdown the PC.")
    risk = job_risk.assess({"type": "ask", "prompt": prompt})
    check("incident prompt: desktop and power both flagged",
          "desktop" in risk["kinds"] and "power" in risk["kinds"], risk)
    check("power is listed before desktop", risk["kinds"].index("power") < risk["kinds"].index("desktop"), risk)
    check("an ask says 'can', not 'will'", not risk["certain"] and
          all(line.startswith("This job can") for line in risk["lines"]), risk)
    check("read-only desktop tools are not what flags it",
          all(t not in risk["tools"].get("desktop", []) for t in ("read_screen", "list_windows", "take_screenshot")),
          risk["tools"])


def test_plain_ask_flags_nothing_risky():
    risk = job_risk.assess({"type": "ask", "prompt": "what is the weather like and the date today"})
    check("no flagged kinds for a harmless ask", risk["kinds"] == [], risk)


def test_assess_never_raises():
    original = job_risk._ask_tools

    def boom(prompt):
        raise RuntimeError("router exploded")

    job_risk._ask_tools = boom
    try:
        risk = job_risk.assess({"type": "ask", "prompt": "anything"})
    finally:
        job_risk._ask_tools = original
    check("a failing classifier says so instead of showing nothing",
          risk["kinds"] == [] and "Couldn't work out" in risk["lines"][0], risk)


# ---------------------------------------------------------------------------
# Scheduler: risk at creation, kinds frozen at approval, env at run
# ---------------------------------------------------------------------------

def _fresh_scheduler():
    d = Path(tempfile.mkdtemp(prefix="jarvis-sched-"))
    scheduler.JARVIS_DIR = d
    scheduler.STORE_FILE = d / "scheduled.json"
    scheduler.LOCK_FILE = d / "scheduled.lock"
    scheduler.ASK_LOG_FILE = d / "scheduler_ask_log.jsonl"


def test_scheduler_stores_risk_and_freezes_kinds_on_approval():
    _fresh_scheduler()
    job = scheduler.create(kind="task", title="x", when="in 1 hour",
                           action={"type": "tool", "tool": "power_action",
                                   "args": {"action": "shutdown"}})
    check("job parked for approval", job["status"] == scheduler.STATUS_NEEDS_APPROVAL, job["status"])
    check("risk stored at creation", job["risk"]["kinds"] == ["power"], job.get("risk"))
    check("nothing authorised before approval", job_risk.authorized_kinds(job) == [], job)
    summary = scheduler.summarize(job)
    check("summarize carries the approval lines", summary["risk"]["lines"] == ["This job will shut down this PC."], summary)
    approved = scheduler.approve(job["id"])
    check("approval freezes the kinds", approved["approved_kinds"] == ["power"], approved)
    check("authorized_kinds follows", job_risk.authorized_kinds(approved) == ["power"], approved)


def test_job_approved_before_this_change_is_not_stripped():
    legacy = {"approved_at": "2026-09-01T10:00:00",
              "action": {"type": "tool", "tool": "power_action", "args": {"action": "sleep"}}}
    check("legacy approved job derives its kinds", job_risk.authorized_kinds(legacy) == ["power"], legacy)
    check("legacy unapproved job gets nothing",
          job_risk.authorized_kinds({"action": legacy["action"]}) == [])


def test_do_ask_always_sets_the_approved_kinds_env():
    _fresh_scheduler()
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "done", ""

    import subprocess
    original = subprocess.run

    def fake_run(argv, **kw):
        seen["env"] = kw["env"]
        return Done()

    scheduler.subprocess.run = fake_run
    try:
        with Env(JARVIS_JOB_APPROVED_KINDS="power,shell"):  # leaked from an outer run
            job = {"id": "abcd1234", "title": "t", "approved_at": "2026-10-01T10:00:00",
                   "approved_kinds": ["desktop"], "action": {"type": "ask", "prompt": "p"}}
            scheduler._do_ask(job, job["action"])
            unapproved = {"id": "abcd1235", "title": "t", "action": {"type": "ask", "prompt": "p"}}
            seen_first = seen["env"].get("JARVIS_JOB_APPROVED_KINDS")
            scheduler._do_ask(unapproved, unapproved["action"])
            seen_second = seen["env"].get("JARVIS_JOB_APPROVED_KINDS")
    finally:
        scheduler.subprocess.run = original
    check("run gets exactly the approved kinds", seen_first == "desktop", seen_first)
    check("an unapproved job's env is empty, not inherited", seen_second == "", repr(seen_second))


# ---------------------------------------------------------------------------
# 3. Unattended directive
# ---------------------------------------------------------------------------

def _prompt_parts():
    return ai_client._system_prompt_parts(
        {}, "", "", True, compact_tools=True, compact_persona=True, offered_names=set())


def test_directive_only_when_scheduled_and_only_in_the_tail():
    with Env(JARVIS_SCHEDULED=None):
        static_a, tail_a = _prompt_parts()
    with Env(JARVIS_SCHEDULED="1"):
        static_b, tail_b = _prompt_parts()
    check("interactive prompt has no directive",
          ai_client.UNATTENDED_DIRECTIVE not in static_a + tail_a)
    check("scheduled prompt tail carries it", ai_client.UNATTENDED_DIRECTIVE in tail_b)
    check("the cached static prefix is byte-identical either way", static_a == static_b)
    check("directive is first in the tail", tail_b.startswith(ai_client.UNATTENDED_DIRECTIVE))
    check("it forbids questions and names the retryable signal",
          "Never ask a question" in ai_client.UNATTENDED_DIRECTIVE and "retryable false" in ai_client.UNATTENDED_DIRECTIVE)


# ---------------------------------------------------------------------------
# 4. Circuit breaker
# ---------------------------------------------------------------------------

def test_retryable_false_stops_an_unattended_run():
    # The incident, replayed: read_screen cannot see; the model then tries
    # screenshot / list_windows / click. None of that may run.
    cannot_see = {"error": "Can't see the screen: the OCR engine (Tesseract) is not installed.",
                  "can_see_screen": False, "retryable": False}
    budget = ai_providers.RoundBudget(grace=True, discovery_limit=3, project_discovery_limit=2)
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="desktop,power"), Stub({"read_screen": cannot_see}) as stub:
        ex, _ = executor(budget=budget)
        first = ex("read_screen", {})
        second = ex("take_screenshot", {})
        third = ex("click", {"x": 10, "y": 1050})
        fourth = ex("power_action", {"action": "shutdown"})
    check("only the failing call ran", stub.ran == ["read_screen"], stub.ran)
    check("the failure itself is returned unchanged", first.get("retryable") is False and
          first.get("can_see_screen") is False, first)
    check("later calls are stopped, not run",
          all(r.get("blocked") and r.get("retryable") is False for r in (second, third, fourth)),
          (second, third, fourth))
    check("the stop names the original cause", "read_screen" in second["error"], second)
    check("no tool rounds left", budget.remaining() == 0, budget.remaining())
    check("discovery pools emptied too", budget.discovery_used >= budget.discovery_limit and
          budget.project_discovery_used >= budget.project_discovery_limit)
    check("no grace round left either", budget.grace_available() is False)
    check("take() refuses everything", budget.take(["search_tools"]) is False and budget.take() is False)


def test_interactive_retryable_false_does_not_trip():
    cannot_see = {"error": "Can't see the screen.", "can_see_screen": False, "retryable": False}
    budget = ai_providers.RoundBudget()
    with Env(JARVIS_SCHEDULED=None, JARVIS_CONTEXT=None), Stub({"read_screen": cannot_see}) as stub:
        ex, _ = executor(budget=budget)
        ex("read_screen", {})
        ex("list_windows", {})
    check("interactive asks keep going", stub.ran == ["read_screen", "list_windows"], stub.ran)
    check("and keep their budget", budget.remaining() > 0)


def test_policy_denial_trips_the_breaker():
    budget = ai_providers.RoundBudget(grace=True)
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=""), Stub() as stub:
        ex, _ = executor(budget=budget)
        ex("click", {"x": 1, "y": 1})
        other = ex("get_datetime", {})
    check("after a denial the run is stopped", stub.ran == [] and other.get("blocked"), (stub.ran, other))
    check("budget drained", budget.remaining() == 0 and not budget.grace_available())


def test_ordinary_failure_does_not_trip():
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS=""), Stub({"get_battery": {"ok": False, "error": "no battery"}}) as stub:
        ex, _ = executor()
        ex("get_battery", {})
        ex("get_datetime", {})
    check("a plain failure (no retryable flag) lets the run continue",
          stub.ran == ["get_battery", "get_datetime"], stub.ran)


def test_stopped_calls_are_not_cached():
    cannot_see = {"error": "x", "retryable": False}
    with Env(**SCHEDULED, JARVIS_JOB_APPROVED_KINDS="desktop"), Stub({"read_screen": cannot_see}):
        ex, _ = executor()
        ex("read_screen", {})
        ex("get_datetime", {})
        check("a stopped call leaves no cache hit behind", getattr(ex, "_cache_hit", None) is False)


# ---------------------------------------------------------------------------
# Runner -- keep every test above this line.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(f"\n# {_name}")
            _fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
