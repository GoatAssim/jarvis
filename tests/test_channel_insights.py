"""Tests for the three read-only Channels views (L.36-P1, P2, P3).

    python3 tests/test_channel_insights.py

  P1  Conversation view per person   transcript.person_records / read_person
  P2  Usage per person               channels/usage.py + the line base writes
  P3  Test as this person            user_admin.simulate (a DRY RUN)

No bot token, no socket, no model, no real ~/.jarvis: HOME is redirected to a
temp dir BEFORE any jarvis module is imported (AGENTS.md).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-insights-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME  # Windows spelling of the same thing
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.channels import (DISCORD, INSTAGRAM, base, people, permissions,  # noqa: E402
                             transcript, usage, user_admin, user_perms)
from jarvis.channels import config as channel_config                          # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


OWNER, FRIEND, OTHER, STRANGER = "100000000000000001", "200000000000000002", "300000000000000003", "400000000000000004"


def wipe():
    root = Path(_HOME) / ".jarvis"
    if root.exists():
        import shutil
        shutil.rmtree(root)


def reset(cfg_discord=None):
    """Fresh store: OWNER, FRIEND, OTHER registered, a sane Discord config.
    Cooldown is off by default (the gate's in-process cooldown would otherwise
    refuse a second quick message from the same fake user and have nothing to do
    with what these tests check); test_simulate_notes turns it back on."""
    wipe()
    transcript.reset_cooldowns()
    for uid, handle in ((OWNER, "boss"), (FRIEND, "friend"), (OTHER, "other")):
        people.touch(DISCORD, uid, handle=handle)
    cfg = channel_config.load_config()
    cfg[DISCORD].update({
        "enabled": True, "owner": OWNER, "allow_tools": True, "cooldown_seconds": 0,
        "dm_allowlist": [OWNER, FRIEND, OTHER], "reply_allowlist": [OWNER, FRIEND, OTHER],
        "tool_allowlist": [OWNER, FRIEND],
    })
    cfg[DISCORD].update(cfg_discord or {})
    channel_config.save_config(cfg)


def corrupt_perms():
    """Make user_perms.json unreadable the way test_user_admin does: atomic_io
    falls back to the .bak, so that has to go too."""
    user_perms.PERMS_FILE.write_text("{ not json", encoding="utf-8")
    bak = user_perms.PERMS_FILE.with_suffix(user_perms.PERMS_FILE.suffix + ".bak")
    if bak.exists():
        bak.unlink()


def snapshot():
    """Every file under ~/.jarvis with its size and mtime: a dry run must leave
    this exactly as it found it."""
    root = Path(_HOME) / ".jarvis"
    out = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                st = path.stat()
                out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def put(thread, record, platform=DISCORD):
    assert transcript.append(platform, thread, record)


def inbound(uid, text, at, ctx="dm", allowed=True, stage="allowed", reason=""):
    return {"dir": "in", "context": ctx, "user_id": uid, "user_handle": "h" + uid[-2:],
            "text": text, "mentioned": True, "allowed": allowed, "stage": stage,
            "reason": reason, "may_use_tools": False, "at": at}


def outbound(text, at, to=None):
    rec = {"dir": "out", "kind": "reply", "text": text, "ok": True, "error": "",
           "provider": "fake", "at": at}
    if to:
        rec["to_user"] = to
    return rec


def cli(*argv):
    from jarvis import channels_cli
    buf, code = io.StringIO(), 0
    with contextlib.redirect_stdout(buf):
        try:
            channels_cli.handle(list(argv))
        except SystemExit as exc:
            code = exc.code or 0
    return code, json.loads(buf.getvalue())


# ============================================================ P1: conversation

def test_dm_thread_attributes_old_replies_by_thread():
    reset()
    put("dm-friend", inbound(FRIEND, "hello", "2026-10-01T10:00:00"))
    put("dm-friend", outbound("hi there", "2026-10-01T10:00:05"))   # an old line: no to_user
    out = user_admin.conversation_view(DISCORD, FRIEND)
    check("ok", out["ok"], str(out))
    texts = [(e["dir"], e["text"], e["via"]) for e in out["entries"]]
    check("both sides of a DM shown, oldest first",
          texts == [("in", "hello", "direct"), ("out", "hi there", "thread")], str(texts))
    check("nothing unattributed in a DM", out["unattributed_replies"] == 0)


def test_group_thread_does_not_leak_between_people():
    reset()
    t = "general"
    put(t, inbound(FRIEND, "friend asks", "2026-10-01T10:00:00", ctx="group"))
    put(t, outbound("to friend", "2026-10-01T10:00:05", to=FRIEND))
    put(t, inbound(OTHER, "other asks", "2026-10-01T10:01:00", ctx="group"))
    put(t, outbound("to other", "2026-10-01T10:01:05", to=OTHER))
    put(t, outbound("legacy reply, no id", "2026-09-01T10:01:05"))
    f = user_admin.conversation_view(DISCORD, FRIEND)
    o = user_admin.conversation_view(DISCORD, OTHER)
    check("friend sees only their own two lines",
          [e["text"] for e in f["entries"]] == ["friend asks", "to friend"], str(f["entries"]))
    check("other sees only their own two lines",
          [e["text"] for e in o["entries"]] == ["other asks", "to other"], str(o["entries"]))
    check("the legacy group reply is shown to NOBODY", all("legacy" not in e["text"] for e in f["entries"] + o["entries"]))
    check("...and is counted as unattributed, not hidden",
          f["unattributed_replies"] == 1 and o["unattributed_replies"] == 1, str((f, o)))


def test_solo_group_thread_is_not_assumed_to_be_theirs():
    """One person speaking in a group so far does not make the group theirs:
    someone else can join later and the old line would be misattributed."""
    reset()
    put("g", inbound(FRIEND, "only me", "2026-10-01T10:00:00", ctx="group"))
    put("g", outbound("legacy", "2026-10-01T10:00:05"))
    out = user_admin.conversation_view(DISCORD, FRIEND)
    check("legacy reply in a group is not attributed even when one person spoke",
          [e["text"] for e in out["entries"]] == ["only me"] and out["unattributed_replies"] == 1, str(out))


def test_rotated_copy_belongs_to_the_same_thread():
    reset()
    folder = transcript.CHANNELS_DIR / DISCORD
    folder.mkdir(parents=True, exist_ok=True)
    old = folder / "dm-friend.20260101-000000.jsonl"
    old.write_text(json.dumps(inbound(FRIEND, "from last year", "2026-01-01T00:00:00")) + "\n"
                   + json.dumps(outbound("old answer", "2026-01-01T00:00:03")) + "\n", encoding="utf-8")
    put("dm-friend", inbound(FRIEND, "today", "2026-10-01T10:00:00"))
    out = user_admin.conversation_view(DISCORD, FRIEND)
    check("rotated history is included, in order",
          [e["text"] for e in out["entries"]] == ["from last year", "old answer", "today"], str(out["entries"]))
    check("one thread, not two", out["threads"] == 1, str(out["threads"]))


def test_denied_messages_show_why():
    reset()
    put("dm-other", inbound(OTHER, "let me in", "2026-10-01T10:00:00", allowed=False,
                            stage="reply", reason="sender is not in reply_allowlist"))
    out = user_admin.conversation_view(DISCORD, OTHER)
    e = out["entries"][0]
    check("a refused message is shown with the stage and reason",
          e["allowed"] is False and e["stage"] == "reply" and "reply_allowlist" in e["reason"], str(e))


def test_limit_clip_and_torn_line():
    reset()
    for i in range(30):
        put("dm-friend", inbound(FRIEND, f"m{i:02d}", f"2026-10-01T10:{i:02d}:00"))
    put("dm-friend", inbound(FRIEND, "x" * 9000, "2026-10-01T11:00:00"))
    with transcript.thread_path(DISCORD, "dm-friend").open("a", encoding="utf-8") as fh:
        fh.write('{"dir": "in", "user_id": "' + FRIEND + '", "text": "torn')     # crash mid-write
    out = user_admin.conversation_view(DISCORD, FRIEND, limit=5)
    check("limit keeps the NEWEST", out["shown"] == 5 and out["entries"][-1]["text"].startswith("xxx"), str(out["shown"]))
    check("total reflects everything", out["total"] == 31, str(out["total"]))
    check("a long message is clipped and flagged",
          out["entries"][-1]["clipped"] is True and len(out["entries"][-1]["text"]) == transcript.MAX_TEXT_CHARS)
    check("limit is bounded", user_admin.conversation_view(DISCORD, FRIEND, limit=10**9)["shown"] == 31)
    check("a bad limit falls back instead of raising", user_admin.conversation_view(DISCORD, FRIEND, limit="lots")["ok"])


def test_unregistered_and_empty():
    reset()
    check("unregistered person refused", user_admin.conversation_view(DISCORD, STRANGER)["ok"] is False)
    check("unknown platform refused", user_admin.conversation_view("irc", FRIEND)["ok"] is False)
    out = user_admin.conversation_view(DISCORD, FRIEND)
    check("a registered person with no log is an empty view, not an error",
          out["ok"] and out["entries"] == [] and out["total"] == 0 and out["unattributed_replies"] == 0, str(out))


def test_log_outbound_records_to_user_only_when_given():
    reset()
    transcript.log_outbound(DISCORD, "t", "a", to_user=FRIEND)
    transcript.log_outbound(DISCORD, "t", "b")
    lines = [json.loads(x) for x in transcript.thread_path(DISCORD, "t").read_text(encoding="utf-8").splitlines()]
    check("to_user written when given", lines[0].get("to_user") == FRIEND, str(lines[0]))
    check("old call shape writes no to_user key", "to_user" not in lines[1], str(lines[1]))


# ============================================================ end to end: base

class _Result:
    def __init__(self, text="the answer", ok=True, usage_total=None, tools=()):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", ok
        self.usage_total = usage_total if usage_total is not None else {
            "input_tokens": 100, "output_tokens": 40, "thinking_tokens": 5,
            "total_tokens": 145, "requests": 2}
        self.usage = {"tool_calls": [{"name": n} for n in tools]}


def drive(user_id, text="hi", ctx="dm", thread="dm-x", result=None, raises=None, mid=[0]):
    """Run one message through the REAL base.handle_message with the model
    replaced by a fake. Returns (decision, sent, ask_args)."""
    mid[0] += 1
    sent, seen = [], {}
    orig_ask, orig_conv, orig_notify = base._ask_jarvis, base._conv_id_for, base._notify_owner_new_sender

    def fake_ask(text_, conv_id, may_use_tools, **kw):
        seen.update({"may_use_tools": may_use_tools, "tool_scope": kw.get("tool_scope")})
        if raises:
            raise raises
        return result or _Result()
    base._ask_jarvis = fake_ask
    base._conv_id_for = lambda *a, **k: "conv-" + thread
    base._notify_owner_new_sender = lambda *a, **k: True
    try:
        msg = permissions.IncomingMessage(
            DISCORD, ctx, user_id=user_id, user_handle="h", text=text, mentioned=True,
            guild_id="g1" if ctx == "group" else "", channel_id=thread if ctx == "group" else "",
            message_id=f"m{mid[0]}", thread_id=thread)
        decision = base.handle_message(DISCORD, msg, lambda chunk: sent.append(chunk) or True)
    finally:
        base._ask_jarvis, base._conv_id_for, base._notify_owner_new_sender = orig_ask, orig_conv, orig_notify
    return decision, sent, seen


def test_handle_message_stamps_replies_and_attributes_a_group():
    reset()
    drive(FRIEND, "friend q", ctx="group", thread="chan")
    drive(OTHER, "other q", ctx="group", thread="chan")
    f = [(e["dir"], e["text"], e["via"]) for e in user_admin.conversation_view(DISCORD, FRIEND)["entries"]]
    o = [(e["dir"], e["text"], e["via"]) for e in user_admin.conversation_view(DISCORD, OTHER)["entries"]]
    check("friend gets their question and THEIR reply, directly attributed",
          f == [("in", "friend q", "direct"), ("out", "the answer", "direct")], str(f))
    check("other gets theirs", o == [("in", "other q", "direct"), ("out", "the answer", "direct")], str(o))
    check("nothing was left unattributed", user_admin.conversation_view(DISCORD, FRIEND)["unattributed_replies"] == 0)


# ============================================================ P2: usage

def put_usage(**kw):
    entry = {"at": datetime.now().replace(microsecond=0).isoformat(), "platform": DISCORD,
             "user_id": FRIEND, "thread_id": "dm", "provider": "p1", "ok": True,
             "input_tokens": 10, "output_tokens": 5, "thinking_tokens": 0,
             "total_tokens": 15, "requests": 1, "tool_calls": 0, "tools": []}
    entry.update(kw)
    usage.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with usage.LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def test_handle_message_writes_one_counts_only_usage_line():
    reset()
    secret = "my-secret-question-xyz"
    drive(FRIEND, secret, result=_Result(text="the-secret-answer-abc", tools=["web_search", "web_search", "get_datetime"]))
    raw = usage.LEDGER.read_text(encoding="utf-8")
    line = json.loads(raw.strip())
    check("one line per ask", len(raw.strip().splitlines()) == 1)
    check("attributed to the sender", line["user_id"] == FRIEND and line["platform"] == DISCORD, str(line))
    check("real token counts from the ask's ledger",
          (line["input_tokens"], line["output_tokens"], line["thinking_tokens"], line["total_tokens"], line["requests"])
          == (100, 40, 5, 145, 2), str(line))
    check("tool names and a call count", line["tools"] == ["web_search", "web_search", "get_datetime"] and line["tool_calls"] == 3, str(line))
    check("provider recorded", line["provider"] == "fake-provider")
    check("NEITHER the question nor the answer is in the ledger", secret not in raw and "the-secret-answer-abc" not in raw)


def test_failed_asks_are_counted_too():
    reset()
    drive(FRIEND, raises=RuntimeError("provider down"))
    drive(FRIEND, result=_Result(text="", ok=False, usage_total={}))
    lines = [json.loads(x) for x in usage.LEDGER.read_text(encoding="utf-8").splitlines()]
    check("both failures recorded", len(lines) == 2 and all(l["ok"] is False for l in lines), str(lines))
    check("a raise records its reason", "provider down" in lines[0].get("error", ""), str(lines[0]))
    s = usage.summary(DISCORD, FRIEND)
    check("summary shows failures", s["window"]["asks"] == 2 and s["window"]["failed"] == 2, str(s["window"]))


def test_denied_message_writes_no_usage():
    reset()
    drive(STRANGER, "hello")
    check("a message the gate refused costs nothing and records nothing", not usage.LEDGER.exists())


def test_record_never_raises():
    reset()
    check("garbage in does not raise", usage.record(DISCORD, None, usage_total="nope", tool_names=[1, None]) in (True, False))
    saved = usage.LEDGER
    usage.LEDGER = Path(_HOME) / "no" / "such" / "dir" / "x" / "usage.jsonl"
    try:
        os.makedirs(Path(_HOME) / "no", exist_ok=True)
        (Path(_HOME) / "no" / "such").write_text("a file where a folder should be")
        check("an unwritable ledger returns False, not an exception", usage.record(DISCORD, FRIEND) is False)
    finally:
        usage.LEDGER = saved


def test_summary_windows_series_tools_and_share():
    reset()
    today = datetime.now().replace(microsecond=0)
    d = lambda n: (today - timedelta(days=n)).isoformat()   # noqa: E731
    put_usage(at=d(0), total_tokens=100, input_tokens=60, output_tokens=40, tool_calls=2, tools=["web_search", "read_file"], requests=3)
    put_usage(at=d(2), total_tokens=50, tools=["web_search"], tool_calls=1, provider="p2")
    put_usage(at=d(40), total_tokens=1000)                        # outside a 30-day window
    put_usage(at=d(1), user_id=OTHER, total_tokens=350)           # someone else
    put_usage(at=d(1), platform=INSTAGRAM, total_tokens=9999)     # another platform
    s = usage.summary(DISCORD, FRIEND, days=30)
    check("window counts only the last 30 days", s["window"]["asks"] == 2 and s["window"]["total_tokens"] == 150, str(s["window"]))
    check("all-time includes older asks", s["all_time"]["asks"] == 3 and s["all_time"]["total_tokens"] == 1150, str(s["all_time"]))
    check("avg per ask", s["window"]["avg_tokens_per_ask"] == 75)
    check("top tools ranked", s["tools"][0] == {"tool": "web_search", "calls": 2} and {"tool": "read_file", "calls": 1} in s["tools"], str(s["tools"]))
    check("providers split", {p["provider"] for p in s["providers"]} == {"p1", "p2"})
    check("series is zero-filled, one row per day", len(s["series"]) == 30 and s["series"][-1]["total_tokens"] == 100, str(s["series"][-3:]))
    check("share of the platform, excluding other platforms", s["platform_total_tokens"] == 500 and s["share_of_platform"] == 30.0, str((s["platform_total_tokens"], s["share_of_platform"])))
    check("7-day window", usage.summary(DISCORD, FRIEND, days=7)["window"]["asks"] == 2 and len(usage.summary(DISCORD, FRIEND, days=7)["series"]) == 7)
    check("days is bounded", usage.summary(DISCORD, FRIEND, days=10**6)["days"] == usage.MAX_DAYS)
    check("tracking_since is the first line anyone wrote", s["tracking_since"] == d(40) and s["has_ledger"])


def test_summary_reads_rotated_ledgers_and_skips_garbage():
    reset()
    put_usage(total_tokens=10)
    rotated = usage.LEDGER.with_name("usage.20260101-000000.jsonl")
    rotated.write_text(json.dumps({"at": datetime.now().replace(microsecond=0).isoformat(), "platform": DISCORD,
                                   "user_id": FRIEND, "total_tokens": 7, "ok": True}) + "\n{torn", encoding="utf-8")
    with usage.LEDGER.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    s = usage.summary(DISCORD, FRIEND)
    check("rotated copy counted, garbage skipped", s["window"]["asks"] == 2 and s["window"]["total_tokens"] == 17, str(s["window"]))


def test_no_ledger_is_honest_not_zero_filled_lies():
    reset()
    s = usage.summary(DISCORD, FRIEND)
    check("no ledger: says so", s["has_ledger"] is False and s["tracking_since"] is None and s["window"]["asks"] == 0)
    check("no ledger: share is unknown, not 0%", s["share_of_platform"] is None)


def test_message_counts_come_from_the_transcript_not_the_ledger():
    reset()
    now = datetime.now().replace(microsecond=0)
    put("dm-other", inbound(OTHER, "a", now.isoformat()))
    put("dm-other", outbound("r", now.isoformat()))
    put("dm-other", inbound(OTHER, "b", now.isoformat(), allowed=False, stage="reply"))
    put("dm-other", inbound(OTHER, "old", (now - timedelta(days=200)).isoformat()))
    m = usage.summary(DISCORD, OTHER, days=30)["messages"]
    check("window: 2 sent, 1 turned away, 1 answered", (m["sent"], m["turned_away"], m["replies"]) == (2, 1, 1), str(m))
    check("all-time includes the old one", m["sent_all_time"] == 3 and m["first_message"], str(m))


def test_usage_view_registered_only():
    reset()
    check("unregistered refused", user_admin.usage_view(DISCORD, STRANGER)["ok"] is False)
    check("registered ok", user_admin.usage_view(DISCORD, FRIEND)["ok"] is True)


# ============================================================ P3: test as this person

def test_gate_stage_list_matches_the_real_gate():
    """Drift guard: force a denial at EVERY stage of the real decide() and check
    the walkthrough names it, in the order the gate runs them."""
    def deny(cfg, ctx, mentioned=True, uid=FRIEND, now_gap=None):
        msg = permissions.IncomingMessage(DISCORD, ctx, user_id=uid, text="x", mentioned=mentioned,
                                          guild_id="g" if ctx == "group" else "", channel_id="c" if ctx == "group" else "")
        return permissions.decide(cfg, msg, last_seen_at=now_gap)
    base_cfg = {"enabled": True, "dm_allowlist": [FRIEND], "reply_allowlist": [FRIEND], "bot_user_id": "999"}
    cases = {
        "enabled": deny({**base_cfg, "enabled": False}, "dm"),
        "self": deny({**base_cfg}, "dm", uid="999"),
        "reachable": deny({**base_cfg, "respond_in_dms": False}, "dm"),
        "dm_allowed": deny({**base_cfg, "dm_allowlist": []}, "dm"),
        "reply": deny({**base_cfg, "reply_allowlist": []}, "dm"),
        "cooldown": deny({**base_cfg, "cooldown_seconds": 60}, "dm", now_gap=__import__("time").time()),
    }
    group = {
        "where": deny({**base_cfg, "allowed_channels": ["other"]}, "group"),
        "reachable": deny({**base_cfg}, "group", mentioned=False),
    }
    for stage, decision in cases.items():
        check(f"real gate denies at '{stage}'", not decision.allowed and decision.stage == stage, repr(decision))
        check(f"'{stage}' is in the DM walkthrough", stage in user_admin.GATE_STAGES["dm"])
    for stage, decision in group.items():
        check(f"real gate denies group at '{stage}'", not decision.allowed and decision.stage == stage, repr(decision))
        check(f"'{stage}' is in the group walkthrough", stage in user_admin.GATE_STAGES["group"])
    order = list(user_admin.GATE_STAGES["dm"])
    check("DM order is the gate's order", order == ["enabled", "self", "reachable", "dm_allowed", "reply", "cooldown"])
    check("every stage has a label", all(s in user_admin.STAGE_LABELS for ctx in user_admin.GATE_STAGES.values() for s in ctx))


def test_simulate_verdicts():
    reset()
    r = user_admin.simulate(DISCORD, FRIEND)
    check("friend is answered with all tools", r["ok"] and r["answered"] and r["tools"]["state"] == "all", str(r))
    check("every stage passes", [s["state"] for s in r["stages"]] == ["pass"] * 6, str(r["stages"]))
    r = user_admin.simulate(DISCORD, OTHER)
    check("other is answered but has no tools (not on the tool list)",
          r["answered"] and r["tools"]["state"] == "none" and "tool list" in r["tools"]["why"], str(r["tools"]))
    # take them off the reply list
    channel_config.remove_from_set(DISCORD, "reply_allowlist", OTHER)
    r = user_admin.simulate(DISCORD, OTHER)
    check("off the reply list -> turned away at 'reply'", not r["answered"] and r["stage"] == "reply", str(r))
    rows = {s["id"]: s["state"] for s in r["stages"]}
    check("stages before pass, the denial fails, later ones are skipped",
          rows["dm_allowed"] == "pass" and rows["reply"] == "fail" and rows["cooldown"] == "skipped", str(rows))
    check("tools are 'none' for someone not answered", r["tools"]["state"] == "none")
    reset({"allow_tools": False})
    r = user_admin.simulate(DISCORD, FRIEND)
    check("master switch off is named as the reason", r["tools"]["state"] == "none" and "master" in r["tools"]["why"] and "allow_tools" in r["tools"]["why"], str(r["tools"]))


def test_simulate_group_mention_and_filters():
    reset()
    r = user_admin.simulate(DISCORD, FRIEND, "group", mentioned=False)
    check("not mentioned in a group -> never addressed", not r["answered"] and r["stage"] == "reachable", str(r))
    r = user_admin.simulate(DISCORD, FRIEND, "group", mentioned=True)
    check("mentioned in a group -> answered", r["answered"] and r["context"] == "group" and r["mentioned"] is True, str(r))
    reset({"allowed_channels": ["123"]})
    r = user_admin.simulate(DISCORD, FRIEND, "group", mentioned=True)
    check("a channel filter does NOT falsely deny the dry run", r["answered"], str(r))
    check("...and the dry run says the filter isn't applied", any("servers or channels" in n for n in r["notes"]), str(r["notes"]))
    check("bad context refused", user_admin.simulate(DISCORD, FRIEND, "voice")["ok"] is False)


def test_simulate_custom_tools_and_failures():
    reset()
    user_perms.set_tools(DISCORD, FRIEND, "custom", ["web_search", "get_datetime"])
    r = user_admin.simulate(DISCORD, FRIEND)
    check("custom list: only what was ticked", r["tools"]["state"] == "custom" and r["tools"]["allowed"] == ["get_datetime", "web_search"], str(r["tools"]))
    check("custom list: plumbing helpers shown separately", "search_tools" in r["tools"]["plumbing"] and "search_tools" not in r["tools"]["allowed"])
    user_perms.set_tools(DISCORD, FRIEND, "custom", [])
    r = user_admin.simulate(DISCORD, FRIEND)
    check("an empty custom list means no tools", r["answered"] and r["tools"]["state"] == "none" and "empty" in r["tools"]["why"], str(r["tools"]))
    user_perms.set_tools(DISCORD, FRIEND, "inherit")
    user_perms.set_tools(DISCORD, OTHER, "custom", ["web_search"])   # so a file exists
    corrupt_perms()
    r = user_admin.simulate(DISCORD, FRIEND)
    check("unreadable limits: still answered, fails closed to NO tools",
          r["answered"] and r["tools"]["state"] == "none" and "could not be read" in r["tools"]["why"], str(r["tools"]))


def test_simulate_notes():
    reset({"cooldown_seconds": 30})
    check("cooldown is flagged as not simulated", any("Cooldown" in n for n in user_admin.simulate(DISCORD, FRIEND)["notes"]))
    reset()
    user_perms.set_can_dm(DISCORD, FRIEND, False)
    check("send_dm off is mentioned", any("send_dm" in n for n in user_admin.simulate(DISCORD, FRIEND)["notes"]))
    reset({"reply_allowlist": ["*"], "dm_allowlist": ["*"]})
    people.set_follow(DISCORD, OTHER, people.FOLLOW_BLOCKED)
    r = user_admin.simulate(DISCORD, OTHER)
    check("blocked but covered by '*' is called out", r["answered"] and any("blocked" in n for n in r["notes"]), str(r["notes"]))


def test_simulate_agrees_with_what_a_real_message_gets():
    """The point of a dry run is that it doesn't lie. For several configurations,
    compare it with what handle_message really hands the model."""
    setups = [
        ("all tools", {}, None),
        ("custom list", {}, ["web_search"]),
        ("empty custom list", {}, []),
        ("master off", {"allow_tools": False}, None),
        ("not on tool list", {"tool_allowlist": [OWNER]}, None),
    ]
    for label, cfg, custom in setups:
        reset(cfg)
        if custom is not None:
            user_perms.set_tools(DISCORD, FRIEND, "custom", custom)
        sim = user_admin.simulate(DISCORD, FRIEND)
        decision, _sent, seen = drive(FRIEND, "hi")
        real_tools = ("none" if not seen["may_use_tools"] else "all" if seen["tool_scope"] is None else "custom")
        check(f"[{label}] answered agrees", sim["answered"] == bool(decision.allowed), str((sim["answered"], decision)))
        check(f"[{label}] tools agree ({real_tools})", sim["tools"]["state"] == real_tools, str((sim["tools"], seen)))
        if real_tools == "custom":
            check(f"[{label}] ticked set agrees", set(sim["tools"]["allowed"]) | set(user_perms.PLUMBING_TOOLS) == set(seen["tool_scope"]))
    # and an unreadable limits file
    reset()
    user_perms.set_tools(DISCORD, OTHER, "custom", ["web_search"])   # so a file exists
    corrupt_perms()
    sim = user_admin.simulate(DISCORD, FRIEND)
    _d, _s, seen = drive(FRIEND, "hi")
    check("[unreadable] both fail closed to no tools", sim["tools"]["state"] == "none" and seen["may_use_tools"] is False, str((sim, seen)))


def test_simulate_saves_nothing_and_calls_no_model():
    """THE requirement: Test as this person does not save the conversation (or
    anything else). Snapshot the whole store around every read-only view."""
    reset()
    drive(FRIEND, "real message")                              # some real history exists
    from jarvis import ai_client
    called = []
    original = ai_client.ask
    ai_client.ask = lambda *a, **k: called.append(1)
    before_cooldown = transcript.last_accepted(DISCORD, FRIEND)
    before = snapshot()
    try:
        for ctx, mentioned in (("dm", True), ("group", True), ("group", False)):
            user_admin.simulate(DISCORD, FRIEND, ctx, mentioned)
            user_admin.simulate(DISCORD, OTHER, ctx, mentioned)
        user_admin.conversation_view(DISCORD, FRIEND)
        user_admin.usage_view(DISCORD, FRIEND)
        cli("channels-user-test", DISCORD, FRIEND, "dm")
        cli("channels-conversation", DISCORD, FRIEND)
        cli("channels-usage", DISCORD, FRIEND)
    finally:
        ai_client.ask = original
    after = snapshot()
    check("no file under ~/.jarvis was created, changed or removed", before == after,
          str({k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)}))
    check("no model was called", called == [])
    check("the cooldown clock was not touched", transcript.last_accepted(DISCORD, FRIEND) == before_cooldown)
    r = user_admin.simulate(DISCORD, FRIEND)
    check("the result says so", r["saved"] is False and r["model_called"] is False)
    thread = user_admin.conversation_view(DISCORD, FRIEND)
    check("the real conversation has only the real message and its reply (no 'test' lines)",
          [e["text"] for e in thread["entries"]] == ["real message", "the answer"], str(thread["entries"]))


def test_simulate_registered_only():
    reset()
    r = user_admin.simulate(DISCORD, STRANGER)
    check("an unregistered person is refused and still nothing is created", r["ok"] is False and people.get(DISCORD, STRANGER) is None)


# ============================================================ CLI

def test_cli_round_trip_and_exit_codes():
    reset()
    put("dm-friend", inbound(FRIEND, "hello", "2026-10-01T10:00:00"))
    code, out = cli("channels-conversation", DISCORD, "@friend")
    check("conversation: @handle of a registered person resolves", code == 0 and out["entries"][0]["text"] == "hello", str(out))
    code, out = cli("channels-conversation", DISCORD, FRIEND, "1")
    check("conversation: limit argument", code == 0 and out["shown"] == 1)
    code, out = cli("channels-usage", DISCORD, FRIEND, "7")
    check("usage: days argument", code == 0 and out["days"] == 7 and len(out["series"]) == 7)
    code, out = cli("channels-user-test", DISCORD, FRIEND, "group", "unmentioned")
    check("user-test: group unmentioned", code == 0 and out["answered"] is False and out["stage"] == "reachable", str(out))
    code, out = cli("channels-user-test", DISCORD, FRIEND)
    check("user-test: defaults to a DM", code == 0 and out["context"] == "dm" and out["answered"] is True)
    for cmd in ("channels-conversation", "channels-usage", "channels-user-test"):
        code, out = cli(cmd, DISCORD, "nobody-here")
        check(f"{cmd}: unknown person -> exit 1 and ok false", code == 1 and out["ok"] is False, str(out))
        code, out = cli(cmd, DISCORD)
        check(f"{cmd}: missing id -> usage error", code == 1 and out["ok"] is False)
    from jarvis import channels_cli, reserved_names
    for cmd in ("channels-conversation", "channels-usage", "channels-user-test"):
        check(f"{cmd} is a dispatched command", cmd in channels_cli.COMMANDS)
        check(f"{cmd} is a reserved name", cmd in reserved_names.RESERVED if hasattr(reserved_names, "RESERVED") else True)


# -------------------------------------------------------------- runner
# (Defined ABOVE the runner on purpose: AGENTS.md - a test appended below it
# would silently never run.)

if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{name} raised {exc!r}\n{traceback.format_exc()}")
    for line in FAILED:
        print("FAILED:", line)
    print(f"{PASSED} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
