"""L.16 round 2: the capability preflight (item 2) and the job authorization the
plan left for send_dm (L.20 item 8: "unless the job carries an explicit
authorization").

Part 1  jarvis/preflight.py      what an unattended ask needs, and what blocks it
Part 2  ask()                    an unattended ask stops BEFORE its first model call
Part 3  scheduler                the stop is a reported failure, not a success
Part 4  job_risk "message" kind  shown to the approver, never granted to an ask job
Part 5  scheduler._do_tool       a tool job now runs as the unattended run it is
Part 6  send_dm                  the ONE exception to "never unattended"

Nothing here touches a desktop, Tesseract, a key or a network: the capability
probes are injected, delivery is stubbed, HOME is a temp dir.

Run: python3 tests/test_l16_preflight_authorization.py
"""
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))
for _v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_JOB_ACTION",
           "JARVIS_JOB_APPROVED_KINDS", "JARVIS_TOKEN_BUDGET"):
    os.environ.pop(_v, None)

from jarvis import (ai_client, ai_config, ai_providers, conversations, job_risk, logs,  # noqa: E402
                    policy, preflight, scheduler)
from jarvis import tools as system_tools  # noqa: E402
from jarvis.actions import send_dm as sd  # noqa: E402
from jarvis.channels import directory, outbound, people  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


INCIDENT = ("Check the screen to see if usage is exhausted. If usage is exhausted, do not type anything "
            "and shutdown the PC. If usage is not exhausted, type 'continue' and press Enter, then "
            "shutdown the PC.")


@contextmanager
def probes(ocr=False, vision=False, power=True):
    saved = (preflight.ocr_ready, preflight.vision_ready, preflight.power_tool_ready)
    preflight.ocr_ready, preflight.vision_ready, preflight.power_tool_ready = (
        lambda: ocr, lambda: vision, lambda: power)
    try:
        yield
    finally:
        preflight.ocr_ready, preflight.vision_ready, preflight.power_tool_ready = saved


# ===========================================================================
# Part 1 -- preflight
# ===========================================================================
needs, tools = preflight.needs_of(INCIDENT)
check("the incident prompt needs the screen AND the power tool",
      set(needs) == {preflight.NEED_SEE, preflight.NEED_POWER}, (needs, tools[:6]))
check("'open notepad and type hello' reaches desktop tools but needs no eyes",
      preflight.NEED_SEE not in preflight.needs_of("open notepad and type hello")[0],
      preflight.needs_of("open notepad and type hello"))
check("a prompt about neither needs nothing", preflight.needs_of("what's the weather in Tunis")[0] == [])
check("a shutdown with no screen word needs only the power tool",
      preflight.needs_of("shut down the PC in an hour")[0] == [preflight.NEED_POWER],
      preflight.needs_of("shut down the PC in an hour"))

with probes(ocr=False, vision=False, power=True):
    r = preflight.check(INCIDENT)
check("no OCR and no vision: blocked on seeing the screen",
      r["ok"] is False and [b["need"] for b in r["blockers"]] == [preflight.NEED_SEE], r)
with probes(ocr=True, vision=False):
    check("OCR alone is enough to see", preflight.check(INCIDENT)["ok"] is True)
with probes(ocr=False, vision=True):
    check("a vision route alone is enough to see", preflight.check(INCIDENT)["ok"] is True)
with probes(ocr=True, power=False):
    r = preflight.check(INCIDENT)
check("no power tool: blocked on powering off, and ONLY that",
      r["ok"] is False and [b["need"] for b in r["blockers"]] == [preflight.NEED_POWER], r)
with probes(ocr=False, vision=False, power=False):
    r = preflight.check(INCIDENT)
check("both missing: both reported, in one result",
      {b["need"] for b in r["blockers"]} == {preflight.NEED_SEE, preflight.NEED_POWER}, r)
with probes(ocr=False, vision=False, power=False):
    check("a prompt that needs neither is never blocked, whatever the PC lacks",
          preflight.check("what's the weather in Tunis")["ok"] is True)
    check("typing without looking is never blocked on sight",
          preflight.check("open notepad and type hello")["ok"] is True)

line = preflight.reply(r)
check("the reply is ONE line, says nothing ran, and names both reasons",
      "\n" not in line and line.startswith("Not started:") and "no model was called" in line
      and "Tesseract" in line and "power_action" in line, line)
check("an ok result has no reply", preflight.reply({"ok": True, "blockers": []}) == "")

saved_assets = job_risk._ask_tools
job_risk._ask_tools = lambda prompt: (_ for _ in ()).throw(RuntimeError("router broke"))
try:
    r = preflight.check(INCIDENT)
finally:
    job_risk._ask_tools = saved_assets
check("FAILS OPEN: a router error never blocks a job", r["ok"] is True and r.get("error") == "router broke", r)
check("the real probes never raise on a PC with none of this installed",
      all(isinstance(f(), bool) for f in (preflight.ocr_ready, preflight.vision_ready, preflight.power_tool_ready)))

# ===========================================================================
# Part 2 -- ask(): stops before the first model call, unattended only
# ===========================================================================
def _ask(prompt, env, *, ocr=False, vision=False, power=True):
    """Run ask() with a fake provider that counts its calls."""
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(1)
        return ai_providers.AIResult(True, text="done")

    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS), conversations.JARVIS_DIR,
            conversations.CONV_DIR, conversations.INDEX_FILE, conversations.CURRENT_FILE,
            logs.JARVIS_DIR, logs.LOG_DIR)
    saved_env = {k: os.environ.get(k) for k in env}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        cfg = {"persona": {}, "defaults": {"tools_enabled": True, "prompt_mode": "full"},
               "providers": [{"name": "p", "type": "fake", "enabled": True, "api_keys": ["k"], "model": "m"}]}
        ai_config.load_ai_config = lambda: cfg
        ai_providers.ADAPTERS["fake"] = adapter
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        try:
            conv = conversations.get_current_id()
            with probes(ocr=ocr, vision=vision, power=power):
                result = ai_client.ask(prompt, commands=[], conversation_id=conv)
            record = conversations.load_conversation(conv) if hasattr(conversations, "load_conversation") else None
        finally:
            (ai_config.load_ai_config, _, conversations.JARVIS_DIR, conversations.CONV_DIR,
             conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])
            for k, v in saved_env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return result, len(calls), record


UNATT = {"JARVIS_SCHEDULED": "1", "JARVIS_CONTEXT": None, "JARVIS_JOB_ACTION": None}
INTER = {"JARVIS_SCHEDULED": None, "JARVIS_CONTEXT": None, "JARVIS_JOB_ACTION": None}

r, n, rec = _ask(INCIDENT, UNATT)
check("unattended + missing capability: ZERO provider calls", n == 0, n)
check("...the ask ends as 'preflight' with the one-line reason",
      r.ok and r.ending == "preflight" and r.text.startswith("Not started:") and "\n" not in r.text,
      (r.ok, r.ending, r.text))
check("...JARVIS_USAGE would carry ending=preflight and cost nothing (what the scheduler reads)",
      (r.usage or {}).get("ending") == "preflight" and (r.usage or {}).get("total_tokens") == 0
      and (r.usage or {}).get("preflight") == ["see_screen"], r.usage)
check("...and it is degraded, so nothing mistakes it for a model-written answer", r.degraded is True)
if rec:
    check("...the person's message and the stop are both saved in the conversation",
          INCIDENT in json.dumps(rec, default=str) and "Not started" in json.dumps(rec, default=str))

r, n, _ = _ask(INCIDENT, INTER)
check("CONTROL: the same prompt, interactive, is NOT preflighted -- the provider is called",
      n >= 1 and r.ending != "preflight", (n, r.ending))
r, n, _ = _ask(INCIDENT, UNATT, ocr=True)
check("CONTROL: unattended with OCR present runs normally", n >= 1 and r.ending != "preflight", (n, r.ending))
r, n, _ = _ask("what's the weather in Tunis", UNATT)
check("CONTROL: an unattended prompt that needs nothing special runs normally",
      n >= 1 and r.ending != "preflight", (n, r.ending))
r, n, _ = _ask(INCIDENT, {**UNATT, "JARVIS_SCHEDULED": None, "JARVIS_CONTEXT": "unattended"})
check("the other unattended signal (JARVIS_CONTEXT=unattended) is preflighted too",
      n == 0 and r.ending == "preflight", (n, r.ending))

# ===========================================================================
# Part 3 -- the scheduler reports it as a failure
# ===========================================================================
USAGE_PRE = "JARVIS_USAGE " + json.dumps({"ending": "preflight", "total_tokens": 0})
USAGE_BUDGET = "JARVIS_USAGE " + json.dumps({"ending": "token_budget"})
check("_ask_ending reads the marker", scheduler._ask_ending("x\n" + USAGE_PRE) == "preflight")
check("_ask_not_started is true only for preflight",
      scheduler._ask_not_started("x\n" + USAGE_PRE) is True
      and scheduler._ask_not_started("x\n" + USAGE_BUDGET) is False
      and scheduler._ask_not_started('the ending was "preflight" they said') is False
      and scheduler._ask_not_started(None) is False)
check("_ask_stopped_over_budget is unchanged", scheduler._ask_stopped_over_budget("x\n" + USAGE_BUDGET) is True
      and scheduler._ask_stopped_over_budget("x\n" + USAGE_PRE) is False)

seen_env = {}


class NotStarted:
    returncode = 0
    stdout = "Not started: this job can't run on this PC right now -- ...\n" + USAGE_PRE
    stderr = ""


def run_ask_job(proc, job):
    original = subprocess.run

    def fake_run(argv, **kw):
        seen_env.update(kw["env"])
        return proc()

    scheduler.subprocess.run = fake_run
    try:
        return scheduler._do_ask(job, job["action"])
    finally:
        scheduler.subprocess.run = original


job = {"id": "abcd9999", "title": "night shutdown", "report": False,
       "action": {"type": "ask", "prompt": INCIDENT, "report": False}}
out = run_ask_job(NotStarted, job)
check("a not-started run is a FAILURE with not_started set",
      out["ok"] is False and out.get("not_started") is True and "capability" in out["error"], out)
check("...and is notified even though the job normally stays quiet",
      bool(out.get("notification")) and "Couldn't start" in json.dumps(out["notification"]), out.get("notification"))
check("the ask launcher marks itself as 'ask' (never 'tool')", seen_env.get("JARVIS_JOB_ACTION") == "ask", seen_env.get("JARVIS_JOB_ACTION"))
leaky = os.environ.get("JARVIS_JOB_ACTION")
os.environ["JARVIS_JOB_ACTION"] = "tool"
try:
    run_ask_job(NotStarted, job)
finally:
    os.environ.pop("JARVIS_JOB_ACTION", None) if leaky is None else os.environ.__setitem__("JARVIS_JOB_ACTION", leaky)
check("a 'tool' inherited from an outer run does NOT reach an ask job's environment",
      seen_env.get("JARVIS_JOB_ACTION") == "ask", seen_env.get("JARVIS_JOB_ACTION"))
job_failed = {"trigger": {"type": "every"}, "run_count": 1}
scheduler._apply_next_state(job_failed, out, None)
check("a recurring not-started job stays pending (it re-checks next time; no re-approval needed)",
      job_failed["status"] == "pending", job_failed)

# ===========================================================================
# Part 4 -- job_risk's "message" kind
# ===========================================================================
dm_action = {"type": "tool", "tool": "send_dm", "args": {"person": "Maryem", "message": "I'll be late tonight"}}
risk = job_risk.assess(dm_action)
check("a scheduled send_dm TOOL job is flagged 'message'", risk["kinds"] == ["message"], risk)
check("the approver is told WHO and WHAT, as a certain statement",
      risk["lines"] == ['This job will send a direct message to Maryem: "I\'ll be late tonight".'], risk["lines"])
long_risk = job_risk.assess({**dm_action, "args": {"person": "Maryem", "message": "x" * 400}})
check("a long message is clipped in the approval line", len(long_risk["lines"][0]) < 260, len(long_risk["lines"][0]))
check("no recipient or text given: still flagged, with the generic phrase",
      job_risk.assess({"type": "tool", "tool": "send_dm", "args": {}})["lines"]
      == ["This job will send a direct message to someone you know, as you."])
ask_risk = job_risk.assess({"type": "ask", "prompt": "send a dm to Maryem on discord that I'll be late"})
check("an ASK job is never granted 'message', whatever its prompt says",
      "message" not in ask_risk["kinds"], ask_risk)
check("'message' is a known kind, listed before 'marked'",
      "message" in job_risk.KINDS and job_risk.KINDS.index("message") < job_risk.KINDS.index("marked"))
check("the other kinds are unchanged",
      job_risk.assess({"type": "tool", "tool": "power_action", "args": {"action": "shutdown"}})["kinds"] == ["power"]
      and job_risk.assess({"type": "command", "command": "x"})["kinds"] == ["shell"])
check("authorized_kinds: an unapproved job covers nothing", job_risk.authorized_kinds({"action": dm_action}) == [])
check("authorized_kinds: an approved job with the kind stored covers it",
      job_risk.authorized_kinds({"action": dm_action, "approved_at": "x", "approved_kinds": ["message"]}) == ["message"])
os.environ["JARVIS_JOB_APPROVED_KINDS"] = "message,bogus"
check("policy reads it back from the environment", policy.approved_kinds_from_env() == ["message"])
os.environ.pop("JARVIS_JOB_APPROVED_KINDS")

# ===========================================================================
# Part 5 -- scheduler._do_tool runs as an unattended run
# ===========================================================================
scheduler._new_scheduled_conversation = lambda j, a: "conv-x"
scheduler._log_run_exchange = lambda *a, **k: None
scheduler._should_report = lambda j: False

captured = {}


def probe_tool():       # zero-argument: execute_tool calls an uncategorised tool as fn()
    captured.update({k: os.environ.get(k) for k in ("JARVIS_SCHEDULED", "JARVIS_JOB_APPROVED_KINDS", "JARVIS_JOB_ACTION")})
    return {"ok": True}


system_tools.TOOLS["probe_tool_l16"] = probe_tool
tjob = {"id": "t1", "title": "t", "approved_at": "x", "approved_kinds": ["power", "message"],
        "action": {"type": "tool", "tool": "probe_tool_l16", "args": {}}}
scheduler._do_tool(tjob, tjob["action"])
check("during a tool job: the run IS unattended, carries its approved kinds, and says 'tool'",
      captured == {"JARVIS_SCHEDULED": "1", "JARVIS_JOB_APPROVED_KINDS": "power,message", "JARVIS_JOB_ACTION": "tool"}, captured)
check("afterwards every variable is exactly as it was (absent stays absent)",
      all(os.environ.get(k) is None for k in ("JARVIS_SCHEDULED", "JARVIS_JOB_APPROVED_KINDS", "JARVIS_JOB_ACTION")))

os.environ.update({"JARVIS_SCHEDULED": "outer", "JARVIS_JOB_ACTION": "ask", "JARVIS_JOB_APPROVED_KINDS": "files"})
scheduler._do_tool(tjob, tjob["action"])
check("a value that was already set is restored, not cleared",
      (os.environ["JARVIS_SCHEDULED"], os.environ["JARVIS_JOB_ACTION"], os.environ["JARVIS_JOB_APPROVED_KINDS"])
      == ("outer", "ask", "files"))
for k in ("JARVIS_SCHEDULED", "JARVIS_JOB_ACTION", "JARVIS_JOB_APPROVED_KINDS"):
    os.environ.pop(k, None)

orig_exec = system_tools.execute_tool


def exploding(name, arguments=None, **kw):
    raise RuntimeError("boom")


system_tools.execute_tool = exploding
try:
    try:
        with scheduler._unattended_tool_env(tjob):
            system_tools.execute_tool("x")
    except RuntimeError:
        pass
finally:
    system_tools.execute_tool = orig_exec
check("the environment is restored even when the tool raises",
      all(os.environ.get(k) is None for k in ("JARVIS_SCHEDULED", "JARVIS_JOB_APPROVED_KINDS", "JARVIS_JOB_ACTION")))
tjob2 = {"id": "t2", "title": "t", "action": {"type": "tool", "tool": "probe_tool_l16", "args": {}}}
scheduler._do_tool(tjob2, tjob2["action"])
check("a job that was never approved runs with an EMPTY approved list (covers nothing)",
      captured["JARVIS_JOB_APPROVED_KINDS"] == "" and captured["JARVIS_SCHEDULED"] == "1", captured)

# ===========================================================================
# Part 6 -- send_dm: the one exception to "never unattended"
# ===========================================================================
SENT = []


def _reset_dm():
    SENT.clear()
    for v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_JOB_ACTION", "JARVIS_JOB_APPROVED_KINDS"):
        os.environ.pop(v, None)
    for f in (people.PEOPLE_FILE, directory.DIRECTORY_FILE, sd._rate_file()):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass

    def fake(platform, person_id, text):
        SENT.append((platform, str(person_id), text))
        return True, "sent"
    outbound.dm_person = fake
    people.touch("discord", "111", handle="maryem_b")
    people.set_name("discord", "111", "Maryem")


def tool_job(kinds, approved=True, action=dm_action):
    job = {"id": "dm1", "title": "tell Maryem", "action": action}
    if approved:
        job.update({"approved_at": "x", "approved_kinds": kinds})
    return job


def fire(job):
    return scheduler._do_tool(job, job["action"])


_reset_dm()
out = fire(tool_job(["message"]))
check("an approved scheduled send_dm TOOL job sends, to the known contact, with the exact text",
      out["ok"] is True and SENT == [("discord", "111", "I'll be late tonight")], (out, SENT))

_reset_dm()
out = fire(tool_job([], approved=False))
check("the SAME job never approved: refused (it used to slip past -- see Part 5)",
      out["ok"] is False and "does not run unattended" in out["summary"] and SENT == [], (out, SENT))

_reset_dm()
out = fire(tool_job(["desktop", "shell"]))
check("approved for OTHER kinds but not 'message': refused", out["ok"] is False and SENT == [], (out, SENT))

_reset_dm()
os.environ.update({"JARVIS_SCHEDULED": "1", "JARVIS_JOB_ACTION": "ask", "JARVIS_JOB_APPROVED_KINDS": "message"})
refused = sd.tool_send_dm({"person": "Maryem", "message": "hi"})
check("an ASK job carrying the kind in its environment is still refused", refused["ok"] is False and SENT == [], refused)
for action in ("command", "task", "", None):
    _reset_dm()
    os.environ.update({"JARVIS_SCHEDULED": "1", "JARVIS_JOB_APPROVED_KINDS": "message"})
    if action is not None:
        os.environ["JARVIS_JOB_ACTION"] = action
    refused = sd.tool_send_dm({"person": "Maryem", "message": "hi"})
    check(f"launcher {action!r}: refused", refused["ok"] is False and SENT == [], refused)

_reset_dm()
os.environ.update({"JARVIS_SCHEDULED": "1", "JARVIS_JOB_ACTION": "tool", "JARVIS_JOB_APPROVED_KINDS": "message"})
out = sd.tool_send_dm({"person": "999999", "platform": "discord", "message": "hi"})
check("an authorized unattended run still refuses a bare numeric id (a known contact only)",
      out["ok"] is False and "known contact" in out["error"] and SENT == [], out)
people.touch("instagram", "222", handle="sam")
people.set_name("instagram", "222", "Maryem")
out = sd.tool_send_dm({"person": "Maryem", "message": "hi"})
check("...and still never guesses between two people with the same name",
      out["ok"] is False and SENT == [], out)

_reset_dm()
os.environ.update({"JARVIS_SCHEDULED": "1", "JARVIS_JOB_ACTION": "tool", "JARVIS_JOB_APPROVED_KINDS": "message",
                   "JARVIS_CHANNEL_SENDER": json.dumps({"platform": "discord", "user_id": "555", "is_owner": False})})
out = sd.tool_send_dm({"person": "Maryem", "message": "hi"})
check("owner-only still holds when authorized: a non-owner sender is refused",
      out["ok"] is False and out["error"] == "send_dm is owner-only" and SENT == [], out)

_reset_dm()
os.environ.update({"JARVIS_SCHEDULED": "1", "JARVIS_JOB_ACTION": "tool", "JARVIS_JOB_APPROVED_KINDS": "message"})
results = [sd.tool_send_dm({"person": "Maryem", "message": "m%d" % i}) for i in range(sd.MAX_PER_RECIPIENT + 1)]
check("the on-disk per-recipient rate limit still applies to an authorized run",
      all(r["ok"] for r in results[:-1]) and results[-1]["ok"] is False and "rate limit" in results[-1]["error"]
      and len(SENT) == sd.MAX_PER_RECIPIENT, (results[-1], len(SENT)))
_reset_dm()
check("interactive send_dm is exactly as before", sd.tool_send_dm({"person": "Maryem", "message": "yo"})["ok"] is True and len(SENT) == 1)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
