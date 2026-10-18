"""E4 -- a DM Jarvis sends the owner now leaves a notifier record.

Before: the `notify_owner` tool sent a DM through channels/outbound and wrote NOTHING to the
notifier, yet its failure hint told the model "the notification is still in the Jarvis inbox
either way". Now the tool calls notifier.record_owner_dm(), which writes one inbox record.

Covers:
  * the new `owner_dm` kind: registered, silent (level 1) by default, no Settings row;
  * record_owner_dm(): success, total failure, partial success, junk input; the record is in the
    inbox, inbox-only by default, marked `failed` only when nothing was delivered, and carries the
    reason for each platform that missed;
  * a configured higher level adds the live channels (toast / desktop) but NEVER a DM channel,
    so it cannot send the same message twice; the digest never re-batches the record;
  * a notifier that raises cannot make record_owner_dm raise;
  * the notify_owner TOOL: result dict unchanged on success and on failure, one record per call,
    no record for a call that is refused (no message / unknown platform), and a record that cannot
    be written does not change what the model is told;
  * it still takes no recipient (the AGENTS.md invariant).

No network, no real DM (outbound.notify_owner is replaced), no real OS toast (the deliverers are
replaced), HOME redirected to a temp folder so your real inbox is never touched.

Run: python3 tests/test_e4_notify_owner_record.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_e4_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import notifier, notify_registry as reg  # noqa: E402  (after HOME is redirected)
from jarvis.channels import outbound  # noqa: E402
from jarvis.actions import notify_owner as tool_mod  # noqa: E402

notifier.JARVIS_DIR = Path(HOME) / ".jarvis"
notifier.INBOX_FILE = notifier.JARVIS_DIR / "notifications.json"
notifier.CONFIG_FILE = notifier.JARVIS_DIR / "notify_config.json"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _reset(config=None):
    """Empty inbox; notify_config.json with `config` (or none)."""
    notifier.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    for f in (notifier.INBOX_FILE, notifier.CONFIG_FILE):
        if f.exists():
            f.unlink()
    if config is not None:
        notifier.CONFIG_FILE.write_text(json.dumps(config), encoding="utf-8")


def _inbox_dm():
    return [i for i in notifier._load_inbox() if i.get("kind") == "owner_dm"]


class _FakeChannels:
    """Replaces the live deliverers so nothing real is raised, and records who was called."""

    def __init__(self):
        self.called = []
        self._saved = {}

    def __enter__(self):
        for name in ("stream", "desktop", "voice", "playnite", "discord", "instagram"):
            self._saved[name] = notifier._DELIVERERS[name]
            notifier._DELIVERERS[name] = (lambda record, config, _n=name:
                                          self.called.append(_n) or True)
        return self

    def __exit__(self, *exc):
        notifier._DELIVERERS.update(self._saved)


OK_DISCORD = {"platform": "discord", "ok": True, "detail": "sent"}
NO_DISCORD = {"platform": "discord", "ok": False, "detail": "channel disabled"}
NO_INSTA = {"platform": "instagram", "ok": False, "detail": "outside the 24-hour window"}


# --- the kind -----------------------------------------------------------------------

def test_the_owner_dm_kind_is_registered_and_silent():
    row = reg.kind("owner_dm")
    check("`owner_dm` is a registered kind", row is not None)
    check("its default level is 1 (silent)", row and row["level"] == 1, row)
    check("it has no Settings row", row and row["settings"] is False, row)
    _reset()
    check("normalize_level() gives 1 for it", notifier.normalize_level(None, kind="owner_dm") == 1)
    check("registry_problems() stays empty", notifier.registry_problems() == [],
          notifier.registry_problems())
    _reset({"levels": {"reminder": 3}})
    check("a config file that does not mention it still gets level 1",
          notifier.normalize_level(None, kind="owner_dm") == 1)


# --- record_owner_dm ----------------------------------------------------------------

def test_a_delivered_dm_leaves_a_silent_record():
    _reset()
    with _FakeChannels() as fake:
        rec = notifier.record_owner_dm("Your build finished.", [OK_DISCORD])
    check("a record comes back", isinstance(rec, dict), rec)
    check("kind owner_dm, level 1, source notify_owner",
          rec["kind"] == "owner_dm" and rec["level"] == 1 and rec["source"] == "notify_owner", rec)
    check("inbox only: nothing toasted, nothing DMed", rec["delivered"] == ["inbox"] and not fake.called,
          (rec["delivered"], fake.called))
    check("not marked failed", rec["failed"] is False)
    check("the title names where it went", "discord" in rec["title"].lower(), rec["title"])
    check("the body is the message", rec["message"] == "Your build finished.", rec["message"])
    check("it is in the inbox exactly once", len(_inbox_dm()) == 1)
    check("level 1 is not persistent and needs no confirm",
          rec.get("persistent") is False and rec.get("confirm_required") is False, rec)
    check("the digest did not take it", "deferred_to_digest" not in rec, rec)


def test_an_undelivered_dm_is_a_failed_record_with_the_reasons():
    _reset()
    with _FakeChannels() as fake:
        rec = notifier.record_owner_dm("Disk is almost full.", [NO_DISCORD, NO_INSTA])
    check("marked failed", rec["failed"] is True, rec)
    check("the title says nobody was reached", "couldn't reach" in rec["title"].lower(), rec["title"])
    check("the message is kept", rec["message"].startswith("Disk is almost full."), rec["message"])
    check("each platform's reason is in the body",
          "channel disabled" in rec["message"] and "24-hour window" in rec["message"], rec["message"])
    check("still silent: inbox only", rec["delivered"] == ["inbox"] and not fake.called, fake.called)
    check("the failure is in the inbox", len(_inbox_dm()) == 1 and _inbox_dm()[0]["failed"])


def test_a_partial_success_is_not_a_failure():
    _reset()
    with _FakeChannels():
        rec = notifier.record_owner_dm("Done.", [OK_DISCORD, NO_INSTA])
    check("not failed: the owner was reached", rec["failed"] is False, rec)
    check("the title names the platform that worked", "discord" in rec["title"].lower(), rec["title"])
    check("the one that missed is still explained", "instagram" in rec["message"].lower()
          and "24-hour window" in rec["message"], rec["message"])


def test_junk_input_does_not_raise():
    _reset()
    with _FakeChannels():
        a = notifier.record_owner_dm("", None)
        b = notifier.record_owner_dm(None, [None, "x", 3, {}])
    check("no results at all counts as not delivered", a is not None and a["failed"] is True, a)
    check("junk rows are ignored", b is not None and b["failed"] is True, b)


def test_a_higher_configured_level_never_sends_a_dm():
    _reset({"levels": {"owner_dm": 4}})
    with _FakeChannels() as fake:
        rec = notifier.record_owner_dm("Heads up.", [OK_DISCORD])
    check("level 4 is honoured on the record", rec["level"] == 4, rec["level"])
    check("the live channels ran (toast + desktop)", "stream" in fake.called and "desktop" in fake.called,
          fake.called)
    check("but no DM channel ran -- the DM already went out",
          "discord" not in fake.called and "instagram" not in fake.called, fake.called)
    check("level 4 still marks the record persistent", rec.get("persistent") is True, rec)
    _reset({"levels": {"owner_dm": 2}})
    with _FakeChannels() as fake:
        rec = notifier.record_owner_dm("Heads up.", [OK_DISCORD])
    check("level 2 toasts (stream + desktop) and nothing else",
          sorted(fake.called) == ["desktop", "stream"], fake.called)
    _reset({"levels": {"notify_owner": 1, "owner_dm": 3}})
    with _FakeChannels() as fake:
        rec = notifier.record_owner_dm("Heads up.", [OK_DISCORD])
    check("a level keyed by the source wins over the kind's", rec["level"] == 1 and not fake.called, rec["level"])


def test_a_broken_notifier_cannot_break_the_helper():
    _reset()
    saved = notifier.notify

    def boom(*a, **k):
        raise RuntimeError("notifier is broken")
    notifier.notify = boom
    try:
        got = notifier.record_owner_dm("x", [OK_DISCORD])
    finally:
        notifier.notify = saved
    check("returns None instead of raising", got is None, got)


# --- the tool -------------------------------------------------------------------------

class _Outbound:
    """Replaces outbound.notify_owner for the tool tests."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def __call__(self, text, platforms=None, first_success_only=True):
        self.calls.append((text, platforms, first_success_only))
        return self.outcome


def _run_tool(args, outcome):
    saved = outbound.notify_owner
    fake = _Outbound(outcome)
    outbound.notify_owner = fake
    try:
        with _FakeChannels():
            return tool_mod.tool_notify_owner(args), fake
    finally:
        outbound.notify_owner = saved


def test_the_tool_records_a_delivered_message_and_returns_the_same_result():
    _reset()
    result, fake = _run_tool({"message": "  Backup done.  "},
                             {"ok": True, "results": [OK_DISCORD]})
    check("the DM transport was called once with the trimmed text",
          fake.calls == [("Backup done.", None, True)], fake.calls)
    check("result is exactly what it was before E4",
          result == {"ok": True, "delivered_to": ["discord"], "not_delivered": []}, result)
    items = _inbox_dm()
    check("one inbox record", len(items) == 1, items)
    check("it holds the message", items and items[0]["message"] == "Backup done.", items)


def test_the_tool_records_a_failed_message_and_the_hint_is_now_true():
    _reset()
    result, _ = _run_tool({"message": "Disk full.", "all": True},
                          {"ok": False, "results": [NO_DISCORD, NO_INSTA]})
    check("result keeps its failure shape",
          result["ok"] is False and result["error"] == "could not reach the owner on any channel"
          and result["detail"] == ["discord: channel disabled",
                                   "instagram: outside the 24-hour window"], result)
    check("the hint still says the message is in the inbox", "still in the Jarvis inbox" in result["hint"])
    items = _inbox_dm()
    check("...and it IS: one failed record in the inbox", len(items) == 1 and items[0]["failed"], items)


def test_a_refused_call_leaves_no_record():
    _reset()
    r1, f1 = _run_tool({"message": "   "}, {"ok": True, "results": []})
    r2, f2 = _run_tool({"message": "hi", "platform": "carrier-pigeon"}, {"ok": True, "results": []})
    check("empty message is refused", r1 == {"ok": False, "error": "message is required"}, r1)
    check("unknown platform is refused", r2["ok"] is False and "carrier-pigeon" in r2["error"], r2)
    check("neither sent anything", f1.calls == [] and f2.calls == [])
    check("neither left a record", _inbox_dm() == [], _inbox_dm())


def test_a_record_that_cannot_be_written_does_not_change_the_answer():
    _reset()
    saved = notifier.record_owner_dm

    def boom(*a, **k):
        raise RuntimeError("inbox is read-only")
    notifier.record_owner_dm = boom
    try:
        result, _ = _run_tool({"message": "Hi."}, {"ok": True, "results": [OK_DISCORD]})
    finally:
        notifier.record_owner_dm = saved
    check("the model is still told it was delivered",
          result == {"ok": True, "delivered_to": ["discord"], "not_delivered": []}, result)


def test_the_tool_still_has_no_recipient():
    props = tool_mod.TOOL_SCHEMAS[0]["parameters"]["properties"]
    check("parameters are message / platform / all only", sorted(props) == ["all", "message", "platform"],
          sorted(props))


if __name__ == "__main__":
    for name in [n for n in sorted(globals()) if n.startswith("test_")]:
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAIL.append((name, repr(exc)))
            print(f"FAILED  {name}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
