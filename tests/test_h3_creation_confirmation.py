"""H.3 — every scheduled-job creation sends a confirmation notification.

Covers:
  * remind_me, notify_me (with `when`), schedule_task and schedule_watch each
    send exactly one "scheduled" notification after the job is created, naming
    what and when, linked to the job id and the originating conversation;
  * notify_me WITHOUT `when` is the notification itself — no extra
    confirmation is layered on top;
  * `confirm: false` (bool or the usual string spellings) suppresses it;
  * nothing is sent when creation fails (needs_clarification / SchedulerError);
  * the explicit `channels` argument is honoured, the job's own `level` is NOT
    inherited (a level-4 job must not broadcast merely for being created),
    and the configured level for kind "scheduled" is used;
  * a job parked for approval says so in the confirmation;
  * a notifier that raises never turns a created job into a failed tool call
    and never changes the tool's result shape;
  * the four schemas declare `confirm`.

No network, no live model, no real time.sleep.

Run: python3 tests/test_h3_creation_confirmation.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))
os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis_h3_"))

from jarvis import notifier, scheduler  # noqa: E402
from jarvis.actions import scheduler_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class Ctx:
    conv_id = "conv-h3"


def fresh():
    """Isolated scheduler + notifier state, and a recorder in front of
    notifier.notify so each test can see exactly what was sent without any
    toast/stream/DM delivery happening."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_h3_case_"))
    scheduler.JARVIS_DIR = tmp
    scheduler.STORE_FILE = tmp / "scheduled.json"
    scheduler.LOCK_FILE = tmp / "scheduled.lock"
    scheduler.ASK_LOG_FILE = tmp / "scheduler_ask_log.jsonl"
    notifier.JARVIS_DIR = tmp
    notifier.INBOX_FILE = tmp / "notifications.json"
    notifier.CONFIG_FILE = tmp / "notify_config.json"
    sent = []
    real = notifier.notify

    def recorder(*args, **kwargs):
        sent.append(dict(kwargs, _args=args))
        return {"id": "n%d" % len(sent), "delivered": ["inbox"], "failed_channels": []}

    notifier.notify = recorder
    return tmp, sent, real


def restore(real):
    notifier.notify = real


def test_each_creating_tool_sends_one_confirmation():
    tmp, sent, real = fresh()
    try:
        r = scheduler_tools.tool_remind_me(
            {"message": "call mum", "when": "in 20 minutes"}, Ctx())
        check("remind_me ok", r.get("ok") is True, r)
        check("remind_me sent exactly one confirmation", len(sent) == 1, sent)
        n = sent[0]
        check("kind is 'scheduled'", n.get("kind") == "scheduled", n)
        check("linked to the job id", n.get("job_id") == r["id"], n)
        check("carries the conversation id", n.get("conv_id") == "conv-h3", n)
        check("title says reminder set", n.get("title") == "Reminder set", n)
        check("message has the reminder text", "call mum" in n.get("message", ""), n)
        check("message says when", "in 1" in n["message"] or "today" in n["message"], n)

        sent.clear()
        r = scheduler_tools.tool_notify_me(
            {"message": "build finished", "when": "tomorrow at 9am"}, Ctx())
        check("notify_me(when) ok", r.get("ok") is True, r)
        check("notify_me(when) sent one confirmation", len(sent) == 1, sent)
        check("notify_me(when) kind scheduled and not the job's own kind",
              sent[0].get("kind") == "scheduled", sent)
        check("notify_me(when) mentions the message",
              "build finished" in sent[0]["message"], sent)

        sent.clear()
        r = scheduler_tools.tool_schedule_task(
            {"when": "every weekday at 08:30", "prompt": "summarise my inbox",
             "title": "Morning inbox"}, Ctx())
        check("schedule_task ok", r.get("ok") is True, r)
        check("schedule_task sent one confirmation", len(sent) == 1, sent)
        check("schedule_task title is 'Task scheduled'",
              sent[0].get("title") == "Task scheduled", sent)
        check("schedule_task uses the job title as the label",
              "Morning inbox" in sent[0]["message"], sent)

        sent.clear()
        r = scheduler_tools.tool_schedule_watch({
            "when": "in 10 minutes",
            "checks": [{"if_screen_shows": "build failed", "then": "Tell me."}],
        }, Ctx())
        check("schedule_watch ok", r.get("ok") is True, r)
        check("schedule_watch sent one confirmation", len(sent) == 1, sent)
        check("schedule_watch confirmation is about the watch",
              "Watch: build failed" in sent[0]["message"], sent)
    finally:
        restore(real)


def test_notify_me_without_when_is_not_double_notified():
    tmp, sent, real = fresh()
    notifier.notify = real  # use the real one here: nothing may add a second call
    calls = []
    inner = real

    def spy(*a, **k):
        calls.append(k.get("kind"))
        return inner(*a, **k)

    notifier.notify = spy
    try:
        r = scheduler_tools.tool_notify_me({"message": "right now", "level": 1}, Ctx())
        check("immediate notify_me ok", r.get("ok") is True and r.get("sent") is True, r)
        check("exactly one notify() call, kind 'notify', no 'scheduled'",
              calls == ["notify"], calls)
    finally:
        restore(real)


def test_confirm_false_suppresses():
    tmp, sent, real = fresh()
    try:
        for value in (False, "false", "No", "0", "off"):
            sent.clear()
            r = scheduler_tools.tool_remind_me(
                {"message": "x", "when": "in 1 hour", "confirm": value}, Ctx())
            check("job still created with confirm=%r" % (value,), r.get("ok") is True, r)
            check("no confirmation with confirm=%r" % (value,), sent == [], sent)
        for value in (True, "true", None, "yes"):
            sent.clear()
            args = {"message": "x", "when": "in 1 hour"}
            if value is not None:
                args["confirm"] = value
            scheduler_tools.tool_remind_me(args, Ctx())
            check("confirmation sent with confirm=%r" % (value,), len(sent) == 1, sent)
    finally:
        restore(real)


def test_nothing_sent_when_creation_fails():
    tmp, sent, real = fresh()
    try:
        r = scheduler_tools.tool_remind_me({"message": "", "when": "in 1 hour"}, Ctx())
        check("missing message needs clarification", r.get("needs_clarification") is True, r)
        r = scheduler_tools.tool_remind_me({"message": "x", "when": "blorp o'clock"}, Ctx())
        check("unparseable time needs clarification", r.get("needs_clarification") is True, r)
        r = scheduler_tools.tool_schedule_task({"when": "in 5 minutes"}, Ctx())
        check("task with no action needs clarification", r.get("needs_clarification") is True, r)
        check("no confirmation for any failed creation", sent == [], sent)
        check("no job was stored", scheduler.list_jobs() == [], scheduler.list_jobs())
    finally:
        restore(real)


def test_channels_honoured_and_level_not_inherited():
    tmp, sent, real = fresh()
    try:
        scheduler_tools.tool_remind_me(
            {"message": "x", "when": "in 1 hour", "channels": ["inbox", "toast"],
             "level": 4}, Ctx())
        n = sent[0]
        check("explicit channels reach the confirmation", n.get("channels") == ["inbox", "toast"], n)
        check("the job's level 4 is NOT passed to the confirmation",
              n.get("level") is None, n)
        sent.clear()
        scheduler_tools.tool_remind_me({"message": "x", "when": "in 1 hour"}, Ctx())
        check("no channels given -> None (notifier decides)", sent[0].get("channels") is None, sent)
    finally:
        restore(real)


def test_configured_level_for_scheduled_kind():
    """Real notifier, real config: default is standard (2); lowering the
    'scheduled' level to 1 makes the confirmation inbox-only without touching
    the level of the reminder that fires later."""
    tmp, sent, real = fresh()
    restore(real)
    try:
        r = scheduler_tools.tool_remind_me({"message": "x", "when": "in 1 hour"}, Ctx())
        inbox = json.loads(notifier.INBOX_FILE.read_text(encoding="utf-8"))
        items = inbox if isinstance(inbox, list) else inbox.get("items", [])
        conf = [i for i in items if i.get("kind") == "scheduled"]
        check("a 'scheduled' record is in the durable inbox", len(conf) == 1, items)
        check("default level is 2 (standard)", conf and conf[0].get("level") == 2, conf)
        check("record is tied to the job", conf and conf[0].get("job_id") == r["id"], conf)

        notifier.CONFIG_FILE.write_text(json.dumps({"levels": {"scheduled": 1}}), encoding="utf-8")
        scheduler_tools.tool_remind_me({"message": "y", "when": "in 2 hours"}, Ctx())
        inbox = json.loads(notifier.INBOX_FILE.read_text(encoding="utf-8"))
        items = inbox if isinstance(inbox, list) else inbox.get("items", [])
        conf = [i for i in items if i.get("kind") == "scheduled"]
        check("configured level 1 is honoured for the next confirmation",
              any(i.get("level") == 1 for i in conf), conf)
        check("the level-1 confirmation is the silent (inbox-only) tier",
              all(i.get("level_name") == "silent" for i in conf if i.get("level") == 1)
              and notifier._channels_for_level(1) == ["inbox"], conf)
        check("the stored job's own level is untouched (still unset)",
              scheduler.get(r["id"]).get("level") in (None, 2), scheduler.get(r["id"]))
    finally:
        restore(real)


def test_approval_state_is_mentioned():
    tmp, sent, real = fresh()
    try:
        r = scheduler_tools.tool_schedule_task(
            {"when": "in 30 minutes", "do": "command", "command": "nightly",
             "title": "Nightly backup"}, Ctx())
        check("command task is parked for approval", r.get("needs_approval") is True, r)
        check("confirmation was sent", len(sent) == 1, sent)
        msg = sent[0]["message"]
        check("confirmation says it is waiting for approval",
              "approval" in msg and r["id"] in msg, msg)
    finally:
        restore(real)


def test_notifier_failure_never_breaks_creation():
    tmp, sent, real = fresh()
    try:
        def boom(*a, **k):
            raise RuntimeError("toast exploded")

        notifier.notify = boom
        r = scheduler_tools.tool_remind_me({"message": "x", "when": "in 1 hour"}, Ctx())
        check("job created despite the notifier raising", r.get("ok") is True and r.get("id"), r)
        check("the job really exists in the store", scheduler.get(r["id"]) is not None)
        check("result keys unchanged (no new keys added)",
              set(r) == {"ok", "id", "kind", "title", "when", "next_run", "in", "status"}, sorted(r))
    finally:
        restore(real)


def test_schemas_declare_confirm():
    for name in ("remind_me", "notify_me", "schedule_task", "schedule_watch"):
        schema = next((s for s in scheduler_tools.TOOL_SCHEMAS if s["name"] == name), None)
        props = (schema or {}).get("parameters", {}).get("properties", {})
        check("%s schema declares confirm (boolean)" % name,
              props.get("confirm", {}).get("type") == "boolean", props.keys())
    check("confirm is never required",
          all("confirm" not in (s["parameters"].get("required") or [])
              for s in scheduler_tools.TOOL_SCHEMAS))


def test_default_config_has_scheduled_level():
    check("DEFAULT_CONFIG levels has scheduled=2",
          notifier.DEFAULT_CONFIG["levels"].get("scheduled") == 2)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
