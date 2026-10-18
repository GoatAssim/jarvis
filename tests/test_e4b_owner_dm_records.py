"""E4b -- every DM Jarvis sends the owner on its own initiative leaves an inbox record.

E4 did this for the `notify_owner` tool. E4b (owner decision 2026-10-18: yes) does it for the
rest: the new-sender ping and the rule notice (channels/base.py), the awaiting-reply DM
(channels/awaiting.py), the Instagram group-transport notice (channels/instagram_private.py) and
`jarvis channels-test`. They go through `outbound.notify_owner_recorded()`.

Covers:
  * `reply_digest` is a registered kind (silent, no Settings row);
  * record_owner_dm(): `level` and `title` overrides, the failure heading ignores `title`,
    level 2 adds the toast + desktop channels but never a DM, level None keeps the E4 behaviour;
  * outbound.notify_owner_recorded(): same outcome as notify_owner() plus `recorded`, silent by
    default, a failed DM is a `failed` record, a notifier that raises changes nothing;
  * each caller: the new-sender ping and the rule notice are silent records with their title and
    source; an urgent reply (urgency 2) is a STANDARD-level record (toast + desktop) and a
    non-urgent important one stays silent; neither makes a second `reply_digest` record; an
    unimportant reply is still just a `reply_digest` record; the Instagram notice and
    `channels-test` are silent records.

No network, no real DM (dm_owner is replaced), no real OS toast, HOME redirected to a temp folder.

Run: python3 tests/test_e4b_owner_dm_records.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

HOME = tempfile.mkdtemp(prefix="jarvis_e4b_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import notifier, notify_registry as reg  # noqa: E402  (after HOME is redirected)
from jarvis.channels import outbound, awaiting, base, instagram_private  # noqa: E402

notifier.JARVIS_DIR = Path(HOME) / ".jarvis"
notifier.INBOX_FILE = notifier.JARVIS_DIR / "notifications.json"
notifier.CONFIG_FILE = notifier.JARVIS_DIR / "notify_config.json"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def _reset(config=None):
    notifier.JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    for f in (notifier.INBOX_FILE, notifier.CONFIG_FILE):
        if f.exists():
            f.unlink()
    if config is not None:
        notifier.CONFIG_FILE.write_text(json.dumps(config), encoding="utf-8")


def _inbox(kind=None):
    items = notifier._load_inbox()
    return [i for i in items if kind is None or i.get("kind") == kind]


class _Fakes:
    """No real DM, no real toast. `dm` maps platform -> (ok, detail); `called` records live channels."""

    def __init__(self, dm=None, enabled=("discord", "instagram")):
        self.dm = dm or {"discord": (True, "sent"), "instagram": (False, "outside the 24-hour window")}
        self.enabled = enabled
        self.called = []
        self.sent = []
        self._saved = {}

    def __enter__(self):
        for name in ("stream", "desktop", "voice", "playnite", "discord", "instagram"):
            self._saved[name] = notifier._DELIVERERS[name]
            notifier._DELIVERERS[name] = (lambda record, config, _n=name:
                                          self.called.append(_n) or True)
        self._dm_owner = outbound.dm_owner
        outbound.dm_owner = lambda platform, text: (self.sent.append((platform, text))
                                                    or self.dm.get(platform, (False, "no")))
        self._cfg = outbound.channel_config.load_config
        outbound.channel_config.load_config = lambda: {p: {"enabled": p in self.enabled}
                                                       for p in ("discord", "instagram")}
        return self

    def __exit__(self, *exc):
        notifier._DELIVERERS.update(self._saved)
        outbound.dm_owner = self._dm_owner
        outbound.channel_config.load_config = self._cfg


# --- the kind ------------------------------------------------------------------------

def test_reply_digest_is_a_registered_silent_kind():
    row = reg.kind("reply_digest")
    check("`reply_digest` is a registered kind", row is not None)
    check("its default level is 1 (silent)", row and row["level"] == 1, row)
    check("it has no Settings row", row and row["settings"] is False, row)
    check("the registry is still consistent", notifier.registry_problems() == [],
          notifier.registry_problems())


# --- record_owner_dm: level and title ---------------------------------------------------

def test_record_owner_dm_level_and_title():
    ok = [{"platform": "discord", "ok": True, "detail": "sent"}]
    _reset()
    with _Fakes() as fk:
        rec = notifier.record_owner_dm("hello", ok, source="x")
        check("default: one record, silent, nothing live raised",
              rec and rec["kind"] == "owner_dm" and rec["level"] == 1 and fk.called == [], rec)
        check("default title is the E4 one", rec and rec["title"] == "Sent to your phone (discord)", rec)
        _reset()
        rec = notifier.record_owner_dm("hello", ok, source="x", title="New person on discord")
        check("a title replaces the heading", rec and rec["title"] == "New person on discord", rec)
        _reset()
        rec = notifier.record_owner_dm("hello", ok, source="x", level=2, title="Urgent reply")
        check("level=2 raises the toast and the desktop notification",
              rec and rec["level"] == 2 and sorted(fk.called) == ["desktop", "stream"], (rec, fk.called))
        check("...and never a DM channel", "discord" not in fk.called and "instagram" not in fk.called)
        check("...and it is one record in the inbox", len(_inbox()) == 1, _inbox())
        _reset()
        fk.called.clear()
        miss = [{"platform": "discord", "ok": False, "detail": "channel disabled"}]
        rec = notifier.record_owner_dm("hello", miss, source="x", title="Whatever")
        check("a failure ignores the title and says so", rec and rec["title"] ==
              "Couldn't reach you on any channel" and rec["failed"] is True, rec)
        _reset({"levels": {"owner_dm": 3}})
        rec = notifier.record_owner_dm("hello", ok, source="x")
        check("with no explicit level the configured one still applies", rec and rec["level"] == 3, rec)
        _reset({"levels": {"owner_dm": 3}})
        rec = notifier.record_owner_dm("hello", ok, source="x", level=2)
        check("an explicit level beats the configured one", rec and rec["level"] == 2, rec)


# --- outbound.notify_owner_recorded -------------------------------------------------------

def test_notify_owner_recorded():
    _reset()
    with _Fakes() as fk:
        out = outbound.notify_owner_recorded("ping", source="channel:discord", title="T")
        check("the DM went out once, first platform that worked", fk.sent == [("discord", "ping")], fk.sent)
        check("the outcome is notify_owner()'s, plus `recorded`",
              out["ok"] is True and out["results"][0]["platform"] == "discord" and out["recorded"] is True, out)
        recs = _inbox("owner_dm")
        check("one silent record with the title and source", len(recs) == 1 and recs[0]["title"] == "T"
              and recs[0]["source"] == "channel:discord" and recs[0]["level"] == 1, recs)

        _reset()
        fk.sent.clear()
        out = outbound.notify_owner_recorded("ping", platforms=["instagram"], source="x")
        recs = _inbox("owner_dm")
        check("a DM that could not be delivered is a failed record, not a lost one",
              out["ok"] is False and len(recs) == 1 and recs[0]["failed"] is True
              and "outside the 24-hour window" in recs[0]["message"], (out, recs))

        _reset()
        saved = notifier.record_owner_dm
        notifier.record_owner_dm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = outbound.notify_owner_recorded("ping", source="x")
        finally:
            notifier.record_owner_dm = saved
        check("a notifier that raises changes neither the DM nor the outcome",
              out["ok"] is True and out["recorded"] is False and fk.sent[-1] == ("discord", "ping"), out)


# --- the callers ------------------------------------------------------------------------

class _Msg:
    text = "can I buy a ticket?"
    context = "dm"
    user_id = "42"


def test_new_sender_ping_and_rule_notice():
    _reset()
    with _Fakes():
        base.permissions.CTX_DM = getattr(base.permissions, "CTX_DM", "dm")
        msg = _Msg()
        msg.context = base.permissions.CTX_DM
        ok = base._notify_owner_new_sender("discord", msg, {"handle": "sam", "user_id": "42"})
        recs = _inbox("owner_dm")
        check("the new-sender ping returns True and leaves a silent record",
              ok is True and len(recs) == 1 and recs[0]["level"] == 1, recs)
        check("...titled and sourced by the platform", recs and recs[0]["title"] == "New person on discord"
              and recs[0]["source"] == "channel:discord", recs)
        check("...and the record carries the message the owner got",
              recs and "New person on discord: @sam" in recs[0]["message"], recs)

        _reset()
        base._RULE_NOTICE.clear()
        rec = {"id": "r1", "instruction": {"topic": "the lease", "directive": "ask_owner_first"}}
        base._tell_owner_of_rule("discord", msg, {"handle": "sam"}, rec)
        recs = _inbox("owner_dm")
        check("the rule notice leaves a silent record", len(recs) == 1 and recs[0]["level"] == 1
              and recs[0]["title"] == "A rule you set came up", recs)


def _consider(urgency, important=True, deliver=None):
    awaiting.mark("discord", "900", ask="can you sign the lease?", topic="lease", now=1000)
    classifier = lambda thread, text: {"important": important, "urgency": urgency,  # noqa: E731
                                       "summary": "They will sign tomorrow."}
    return awaiting.consider("discord", "900", "Sam", "I'll sign tomorrow", now=1010,
                             classifier=classifier, deliver=deliver)


def test_awaiting_reply_records():
    awaiting.STORE.parent.mkdir(parents=True, exist_ok=True)
    for f in (awaiting.STORE, awaiting.LOG):
        if f.exists():
            f.unlink()
    _reset()
    with _Fakes() as fk:
        out = _consider(2)
        recs = _inbox()
        dm = [r for r in recs if r["kind"] == "owner_dm"]
        check("an urgent reply is notified", out["decision"] == "notified", out)
        check("...with ONE inbox record, no second reply_digest", len(recs) == 1 and len(dm) == 1, recs)
        check("...which is a normal notification: toast + desktop, not silent",
              dm and dm[0]["level"] == 2 and sorted(fk.called) == ["desktop", "stream"], (dm, fk.called))
        check("...titled as urgent, and the DM said URGENT",
              dm and dm[0]["title"] == "Urgent reply from Sam"
              and fk.sent and fk.sent[0][1].startswith("URGENT"), (dm, fk.sent))

    for f in (awaiting.STORE, awaiting.LOG):
        if f.exists():
            f.unlink()
    _reset()
    with _Fakes() as fk:
        out = _consider(1)
        recs = _inbox()
        check("an important but not urgent reply is notified", out["decision"] == "notified", out)
        check("...with one SILENT record and nothing live raised",
              len(recs) == 1 and recs[0]["kind"] == "owner_dm" and recs[0]["level"] == 1 and fk.called == [],
              (recs, fk.called))

    for f in (awaiting.STORE, awaiting.LOG):
        if f.exists():
            f.unlink()
    _reset()
    with _Fakes() as fk:
        out = _consider(0, important=False)
        recs = _inbox()
        check("an unimportant reply is a digest line, no DM",
              out["decision"] == "digest" and fk.sent == [] and len(recs) == 1
              and recs[0]["kind"] == "reply_digest" and recs[0]["level"] == 1, (out, recs))

    for f in (awaiting.STORE, awaiting.LOG):
        if f.exists():
            f.unlink()
    _reset()
    with _Fakes() as fk:
        out = _consider(2, deliver=lambda name, summary, urgency: {"ok": True})
        recs = _inbox()
        check("a stand-in deliver() (no record of its own) still gets the digest line",
              out["decision"] == "notified" and len(recs) == 1 and recs[0]["kind"] == "reply_digest", recs)


def test_instagram_notice_and_channels_test():
    _reset()
    with _Fakes():
        instagram_private._default_notify("Instagram group transport stopped: login required")
        recs = _inbox("owner_dm")
        check("the Instagram group-transport notice leaves a silent record",
              len(recs) == 1 and recs[0]["level"] == 1 and recs[0]["source"] == "channel:instagram"
              and recs[0]["title"] == "Instagram group transport", recs)

    _reset()
    from jarvis import channels_cli
    src = Path(channels_cli.__file__).read_text(encoding="utf-8")
    check("`jarvis channels-test` calls notify_owner_recorded, not the bare transport",
          "outbound.notify_owner_recorded(" in src and "outbound.notify_owner(" not in src)


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
