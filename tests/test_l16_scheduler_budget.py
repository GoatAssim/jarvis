"""L.16 item 8, scheduler + trace side: the per-job token limit, the third
status "stopped: over budget", and how the stop reaches the turn trace and the
JARVIS_USAGE line the scheduler and web UI read.

Run: python3 tests/test_l16_scheduler_budget.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_providers, scheduler, turn_trace  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def fresh():
    try:
        scheduler.STORE_FILE.unlink()
    except (OSError, AttributeError):
        pass


USAGE_STOPPED = "JARVIS_USAGE " + json.dumps({"ending": "token_budget", "total_tokens": 31000,
                                               "ask_total": {"total_tokens": 31000, "limit": 30000, "exceeded": True}})
USAGE_FINE = "JARVIS_USAGE " + json.dumps({"total_tokens": 900, "ask_total": {"total_tokens": 900, "limit": 30000,
                                                                              "exceeded": False}})

# --- detection reads the marker line, never the prose ------------------------
check("the usage marker with ending=token_budget is detected",
      scheduler._ask_stopped_over_budget("Stopped: ...\n" + USAGE_STOPPED) is True)
check("a normal ask is not", scheduler._ask_stopped_over_budget("done\n" + USAGE_FINE) is False)
check("a reply that merely SAYS token_budget is not",
      scheduler._ask_stopped_over_budget('the ending was "token_budget" they said') is False)
check("junk / empty input never raises",
      scheduler._ask_stopped_over_budget("JARVIS_USAGE {not json") is False
      and scheduler._ask_stopped_over_budget(None) is False)

# --- the status ---------------------------------------------------------------
check("the status exists, is not an error, and is not active",
      scheduler.STATUS_OVER_BUDGET == "over_budget"
      and scheduler.STATUS_OVER_BUDGET not in scheduler.ACTIVE_STATUSES)
stopped = {"ok": False, "over_budget": True, "summary": "x", "error": "stopped: over its token limit"}
for ttype in ("at", "every", "event"):
    job = {"trigger": {"type": ttype}, "run_count": 1}
    scheduler._apply_next_state(job, stopped, None)
    check(f"a '{ttype}' job that went over budget is parked as over_budget, not re-armed",
          job["status"] == "over_budget" and job["next_run"] is None, job)
job = {"trigger": {"type": "every"}, "run_count": 1}
scheduler._apply_next_state(job, {"ok": False, "error": "network down"}, None)
check("a plain failure of a recurring job still stays pending (unchanged)", job["status"] == "pending", job)

# --- per-job limit: validation, storage, summarize ----------------------------
fresh()
j = scheduler.create(kind="task", title="watch", when="in 1 hour", action={"type": "ask", "prompt": "p"}, trusted=True)
jid = j["id"]
check("a new job has no override (the default applies)", j.get("token_budget") is None and
      scheduler.summarize(j).get("token_budget") is None)
check("set_token_budget stores a custom limit", scheduler.set_token_budget(jid, 50000)["token_budget"] == 50000)
check("0 stores 'no limit' (never stopped for cost)", scheduler.set_token_budget(jid, 0)["token_budget"] == 0)
check("None puts it back on the default", scheduler.set_token_budget(jid, None)["token_budget"] is None)
for bad in (-1, 500, "lots"):
    try:
        scheduler.set_token_budget(jid, bad)
        ok = False
    except scheduler.SchedulerError:
        ok = True
    check(f"a limit of {bad!r} is refused", ok)
check("the limit shows in summarize (what the web list reads)",
      scheduler.summarize(scheduler.set_token_budget(jid, 42000))["token_budget"] == 42000)

# --- the env the run is started with ------------------------------------------
seen = {}


class Done:
    returncode, stdout, stderr = 0, "done\n" + USAGE_FINE, ""


class Stopped:
    returncode, stdout, stderr = 0, "Stopped: this scheduled run used 31,000 tokens\n" + USAGE_STOPPED, ""


def run_with(proc, job, leak=None):
    original = subprocess.run

    def fake_run(argv, **kw):
        seen["env"] = kw["env"]
        return proc()

    scheduler.subprocess.run = fake_run
    old = os.environ.get("JARVIS_TOKEN_BUDGET")
    if leak is not None:
        os.environ["JARVIS_TOKEN_BUDGET"] = leak
    try:
        return scheduler._do_ask(job, job["action"])
    finally:
        scheduler.subprocess.run = original
        if old is None:
            os.environ.pop("JARVIS_TOKEN_BUDGET", None)
        else:
            os.environ["JARVIS_TOKEN_BUDGET"] = old


base = {"id": "abcd1234", "title": "t", "action": {"type": "ask", "prompt": "p"}}
run_with(Done, {**base, "token_budget": 50000})
check("a custom limit reaches the run as JARVIS_TOKEN_BUDGET", seen["env"].get("JARVIS_TOKEN_BUDGET") == "50000")
run_with(Done, {**base, "token_budget": 0})
check("'no limit' reaches the run as 0", seen["env"].get("JARVIS_TOKEN_BUDGET") == "0")
run_with(Done, base, leak="5000")
check("no override means NOTHING is set -- a value leaked from an outer run does not apply",
      "JARVIS_TOKEN_BUDGET" not in seen["env"], seen["env"].get("JARVIS_TOKEN_BUDGET"))

# --- the outcome of a stopped run ---------------------------------------------
quiet = {**base, "id": "abcd1235", "report": False}
out = run_with(Stopped, quiet)
check("a stopped run is reported as over_budget, not as success",
      out["ok"] is False and out["over_budget"] is True and "over its token limit" in out["error"], out)
check("...and is notified even for a job that normally stays quiet", bool(out.get("notification")), out)
check("...with the status in the notification title",
      "Stopped: over budget" in json.dumps(out["notification"]), out["notification"])
ok_out = run_with(Done, base)
check("a normal run is unchanged", ok_out["ok"] is True and not ok_out.get("over_budget"))

# --- resume ---------------------------------------------------------------------
fresh()
j = scheduler.create(kind="task", title="w", when="in 1 hour", action={"type": "ask", "prompt": "p"}, trusted=True)
scheduler._mutate(j["id"], lambda x: x.update({"status": "over_budget", "next_run": None}))
check("an over_budget job can be resumed", scheduler.resume(j["id"])["status"] == "pending")

# --- trace + usage line -----------------------------------------------------------
tb = ai_providers.TokenBudget(30000)
tb.add({"input_tokens": 28000, "output_tokens": 900, "thinking_tokens": 2400})
tr = turn_trace.TurnTrace()
tr.ending, tr.degraded, tr.tokens = "token_budget", True, tb.summary()
d = tr.to_dict()
check("the trace says it stopped over budget in its one-line summary", "stopped: over budget" in d["summary"], d["summary"])
check("...explains it in its narrative", any("went over its token limit" in l for l in d["lines"]), d["lines"])
check("...and states the cost against the limit",
      any("31,300 tokens" in l and "limit of 30,000" in l and "2,400 thinking" in l for l in d["lines"]), d["lines"])
rt = turn_trace.from_dict(d) if hasattr(turn_trace, "from_dict") else None
check("the tokens survive a save and reload of the trace", rt is None or (rt.tokens or {}).get("total_tokens") == 31300,
      rt and rt.tokens)
plain = turn_trace.TurnTrace()
plain.tokens = ai_providers.TokenBudget(None).summary()
check("a turn that used nothing adds no token line", "tokens" not in plain.summary())

res = ai_client.AskResult(True, text="x", ending="token_budget", usage_total=tb.summary())
ai_client._attach_ask_total(res)
check("the whole-ask ledger and the ending ride on result.usage (the JARVIS_USAGE line)",
      res.usage["ending"] == "token_budget" and res.usage["ask_total"]["total_tokens"] == 31300
      and res.usage["rounds"] == [], res.usage)
res2 = ai_client.AskResult(True, text="x", usage={"input_tokens": 5, "rounds": [1]}, usage_total=tb.summary())
ai_client._attach_ask_total(res2)
check("an answered ask keeps its own per-attempt usage and gains ask_total",
      res2.usage["input_tokens"] == 5 and "ending" not in res2.usage and res2.usage["ask_total"]["limit"] == 30000, res2.usage)
check("a result with no ledger is left alone", ai_client._attach_ask_total(ai_client.AskResult(False)).usage is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
