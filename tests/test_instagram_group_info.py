"""Tests for Instagram group detection (channel spaces S9a).

    python3 tests/test_instagram_group_info.py

`_group_info` accepts exactly the shapes the inline code accepted before (a thread_key
on the event, on a `thread` object, or on the entry) and nothing new; anything else
falls back to a DM, the safe direction. The first group event writes one log line.
Group messages are group spaces, and the 24h window stays keyed on the thread for groups
and on the person for DMs. No network, no token, no real ~/.jarvis.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-iggroupinfo-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import base, permissions, space                     # noqa: E402
from jarvis.channels import instagram_gateway as ig                     # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


def payload(event=None, entry_extra=None, text="hello"):
    event = dict({"sender": {"id": "u1", "username": "ann"}, "message": {"mid": "m1", "text": text}},
                 **(event or {}))
    return {"entry": [dict({"messaging": [event]}, **(entry_extra or {}))]}


def parse(**kw):
    return ig.parse_events(payload(**kw), bot_id="me", bot_handle="jarvis")


def test_accepted_shapes():
    out = parse(event={"thread_key": "t1"})
    check("thread_key on the event", out and out[0].context == permissions.CTX_GROUP and out[0].thread_id == "t1")
    out = parse(event={"thread": {"id": "t2"}})
    check("thread.id on the event", out and out[0].context == permissions.CTX_GROUP and out[0].thread_id == "t2")
    out = parse(entry_extra={"thread_key": "t3"})
    check("thread_key on the entry", out and out[0].context == permissions.CTX_GROUP and out[0].thread_id == "t3")
    check("the channel id is the thread key", out[0].channel_id == "t3")
    check("event wins over the entry", parse(event={"thread_key": "e"}, entry_extra={"thread_key": "n"})[0].thread_id == "e")


def test_everything_else_falls_back_to_a_dm():
    for name, kw in {"no key at all": {}, "thread_key equals the sender": {"event": {"thread_key": "u1"}},
                     "empty key": {"event": {"thread_key": ""}},
                     "thread is a string, not an object": {"event": {"thread": "t9"}},
                     "thread is a list": {"event": {"thread": ["t9"]}},
                     "thread object without an id": {"event": {"thread": {"name": "x"}}},
                     "an unrelated field": {"event": {"conversation_id": "c1"}},
                     "group_id is not a shape we accept": {"event": {"group_id": "g1"}}}.items():
        out = parse(**kw)
        check(f"{name} -> a DM", out and out[0].context == permissions.CTX_DM and out[0].mentioned is True
              and out[0].thread_id == "u1", str(out and (out[0].context, out[0].thread_id)))
    check("helper tolerates junk", ig._group_info(None, None, None) == ("", False)
          and ig._group_info("x", 5, "u") == ("", False))


def test_mention_rule_and_space_kind():
    out = parse(event={"thread_key": "t1"}, text="just chatting")
    check("an unmentioned group message is not addressed", out[0].mentioned is False)
    out = parse(event={"thread_key": "t1"}, text="@jarvis hi")
    check("a mentioned one is", out[0].mentioned is True)
    check("it is a group space", space.kind_of(out[0]) == space.KIND_GROUP and space.is_room(out[0]))
    dm = parse()
    check("a DM is a DM space", space.kind_of(dm[0]) == space.KIND_DM)


def test_one_log_line_for_the_first_group_event():
    lines = []
    saved = base._log
    base._log = lambda text, *a, **k: lines.append(str(text))
    try:
        ig._GROUP_NOTED = False
        parse()
        check("a DM writes no group line", not lines, str(lines))
        parse(event={"thread_key": "t1"})
        parse(event={"thread_key": "t2"})
        parse(entry_extra={"thread_key": "t3"})
    finally:
        base._log = saved
    hits = [l for l in lines if "group payload received" in l]
    check("exactly one line for several group events", len(hits) == 1, str(lines))
    check("it says what the docs say", "unsupported" in hits[0] and "experimental" in hits[0], hits[0] if hits else "")
    check("and holds no ids or text", "t1" not in hits[0] and "hello" not in hits[0])


def test_window_key():
    group = parse(event={"thread_key": "t1"})[0]
    dm = parse()[0]
    check("a group window is keyed on the thread and sent into the thread",
          ig._window_and_target(group) == ("t1", "t1"), str(ig._window_and_target(group)))
    check("a DM window is keyed on the person, sent by id", ig._window_and_target(dm) == ("u1", ""),
          str(ig._window_and_target(dm)))
    mention = ig.parse_events({"entry": [{"changes": [{"field": "mentions", "value": {
        "from": {"id": "u1"}, "text": "hi", "comment_id": "c1"}}]}]})[0]
    check("a comment mention is unchanged (keyed on the sender)", ig._window_and_target(mention) == ("u1", "u1"))


def test_docs_make_no_live_claim():
    doc = ig.__doc__
    check("the docstring says experimental and unverified", "EXPERIMENTAL" in doc and "NOT verified" in doc)
    check("and says the reliable route is an offline importer", "offline importer" in doc)


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=4))
    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    shutil.rmtree(_HOME, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
