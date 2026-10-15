"""Tests for L.20's send_dm tool (actions/send_dm.py) and outbound.dm_person.

Plain asserts, no framework. Delivery is stubbed (outbound.dm_person) so no
network or Discord library is needed; everything on disk goes to a temp HOME.

    python3 tests/test_send_dm.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

# Before any jarvis module that resolves Path.home() at import time.
_TMP = tempfile.mkdtemp(prefix="jarvis-send-dm-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
for _v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
    os.environ.pop(_v, None)

from jarvis.channels import directory, outbound, people, transcript  # noqa: E402
from jarvis.actions import send_dm as sd  # noqa: E402

SENT = []


def _stub(ok=True, detail="sent"):
    def fake(platform, person_id, text):
        SENT.append((platform, str(person_id), text))
        return ok, detail
    outbound.dm_person = fake


def _reset():
    SENT.clear()
    for v in ("JARVIS_CHANNEL_SENDER", "JARVIS_SCHEDULED", "JARVIS_CONTEXT"):
        os.environ.pop(v, None)
    # atomic_io keeps a .bak beside every file and read_json falls back to it
    # when the main file is missing, so a reset must remove the backup too.
    for f in (people.PEOPLE_FILE, directory.DIRECTORY_FILE, sd._rate_file()):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    _stub()


def _person(platform, uid, handle="", name="", **extra):
    people.touch(platform, uid, handle=handle)
    if name:
        people.set_name(platform, uid, name)
    for k, v in extra.items():
        people._update(platform, uid, **{k: v})


def _run_tool(**kw):
    return sd.tool_send_dm(kw)


# --- happy path --------------------------------------------------------

def test_sends_to_known_person_by_name():
    _reset()
    _person("discord", "111", handle="maryem_b", name="Maryem")
    out = _run_tool(person="maryem", message="I'll be late")
    assert out["ok"] is True, out
    assert SENT == [("discord", "111", "I'll be late")], SENT
    assert out["sent_to"]["name"] == "Maryem"


def test_matches_one_word_of_a_full_name_but_not_a_prefix():
    _reset()
    _person("discord", "111", name="Maryem Ben Ali")
    assert _run_tool(person="Maryem", message="hi")["ok"] is True
    SENT.clear()
    out = _run_tool(person="mar", message="hi")
    assert out["ok"] is False and not SENT


def test_handle_and_platform_id_forms():
    _reset()
    _person("instagram", "ig9", handle="sam.k")
    assert _run_tool(person="@sam.k", message="x")["ok"] is True
    assert _run_tool(person="instagram:ig9", message="x")["ok"] is True
    assert [s[0] for s in SENT] == ["instagram", "instagram"]


def test_directory_only_person_resolves():
    _reset()
    directory.record("discord", "newguy", "222")
    out = _run_tool(person="@newguy", message="hello")
    assert out["ok"] is True and SENT[0][1] == "222", out


# --- never guess -------------------------------------------------------

def test_unknown_person_lists_known_contacts_and_sends_nothing():
    _reset()
    _person("discord", "111", name="Maryem")
    out = _run_tool(person="Zed", message="hi")
    assert out["ok"] is False and "Zed" in out["error"]
    assert out["known_contacts"][0]["name"] == "Maryem"
    assert not SENT


def test_ambiguous_name_returns_candidates_and_sends_nothing():
    _reset()
    _person("discord", "111", name="Sam")
    _person("instagram", "ig1", name="Sam")
    out = _run_tool(person="sam", message="hi")
    assert out["ok"] is False and len(out["candidates"]) == 2, out
    assert not SENT
    # A platform disambiguates.
    assert _run_tool(person="sam", message="hi", platform="discord")["ok"] is True
    assert SENT[0][0] == "discord"


def test_bare_numeric_id_needs_a_platform():
    _reset()
    out = _run_tool(person="987654321", message="hi")
    assert out["ok"] is False and "platform" in out["error"], out
    out = _run_tool(person="987654321", message="hi", platform="discord")
    assert out["ok"] is True and out["known_contact"] is False, out
    assert SENT[0] == ("discord", "987654321", "hi")


def test_owner_and_blocked_are_refused():
    _reset()
    _person("discord", "1", name="Boss", is_owner=True)
    _person("discord", "2", name="Troll", follow="blocked")
    for who in ("Boss", "Troll"):
        out = _run_tool(person=who, message="hi")
        assert out["ok"] is False, (who, out)
    assert not SENT


# --- gates -------------------------------------------------------------

def test_non_owner_chat_sender_is_refused():
    _reset()
    _person("discord", "111", name="Maryem")
    os.environ[sd.SENDER_ENV] = json.dumps(
        {"platform": "discord", "user_id": "999", "is_owner": False})
    out = _run_tool(person="Maryem", message="hi")
    assert out["ok"] is False and "owner-only" in out["error"], out
    assert not SENT


def test_owner_chat_sender_is_allowed():
    _reset()
    _person("discord", "111", name="Maryem")
    os.environ[sd.SENDER_ENV] = json.dumps(
        {"platform": "discord", "user_id": "1", "is_owner": True})
    assert _run_tool(person="Maryem", message="hi")["ok"] is True


def test_malformed_sender_env_falls_back_to_pc_context():
    _reset()
    _person("discord", "111", name="Maryem")
    os.environ[sd.SENDER_ENV] = "not json"
    assert _run_tool(person="Maryem", message="hi")["ok"] is True


def test_refuses_when_scheduled_or_unattended():
    _reset()
    _person("discord", "111", name="Maryem")
    os.environ["JARVIS_SCHEDULED"] = "1"
    out = _run_tool(person="Maryem", message="hi")
    assert out["ok"] is False and "unattended" in out["error"], out
    os.environ.pop("JARVIS_SCHEDULED")
    os.environ["JARVIS_CONTEXT"] = "unattended"
    assert _run_tool(person="Maryem", message="hi")["ok"] is False
    assert not SENT


def test_input_validation():
    _reset()
    _person("discord", "111", name="Maryem")
    assert _run_tool(person="", message="hi")["ok"] is False
    assert _run_tool(person="Maryem", message="  ")["ok"] is False
    assert _run_tool(person="Maryem", message="x" * (sd.MAX_MESSAGE_CHARS + 1))["ok"] is False
    assert _run_tool(person="Maryem", message="hi", platform="signal")["ok"] is False
    assert not SENT


def test_dry_run_resolves_without_sending_or_counting():
    _reset()
    _person("discord", "111", name="Maryem")
    out = _run_tool(person="Maryem", message="hi", dry_run=True)
    assert out["ok"] and out["dry_run"] and out["would_send_to"]["user_id"] == "111"
    assert not SENT
    assert not sd._load_sends(time.time())


# --- rate limit --------------------------------------------------------

def test_per_recipient_rate_limit_persists_on_disk():
    _reset()
    _person("discord", "111", name="Maryem")
    _person("discord", "222", name="Other")
    for _ in range(sd.MAX_PER_RECIPIENT):
        assert _run_tool(person="Maryem", message="hi")["ok"] is True
    out = _run_tool(person="Maryem", message="again")
    assert out["ok"] is False and "rate limit" in out["error"], out
    assert len(SENT) == sd.MAX_PER_RECIPIENT
    # A different person is unaffected.
    assert _run_tool(person="Other", message="hi")["ok"] is True


def test_old_sends_age_out_of_the_window():
    _reset()
    _person("discord", "111", name="Maryem")
    old = time.time() - sd.RATE_WINDOW_SECONDS - 10
    from jarvis import atomic_io
    atomic_io.write_json(sd._rate_file(), {"discord:111": [old] * 50, "_all": [old] * 50})
    assert _run_tool(person="Maryem", message="hi")["ok"] is True


def test_overall_rate_limit():
    _reset()
    for i in range(sd.MAX_OVERALL):
        _person("discord", str(1000 + i), name=f"P{i}")
        assert _run_tool(person=f"P{i}", message="hi")["ok"] is True
    _person("discord", "5000", name="Late")
    out = _run_tool(person="Late", message="hi")
    assert out["ok"] is False and "rate limit" in out["error"], out


# --- failure + logging -------------------------------------------------

def test_failure_names_cause_is_logged_and_not_counted():
    _reset()
    _stub(False, "a bot can only DM a user who shares a server with it")
    _person("discord", "111", name="Maryem")
    out = _run_tool(person="Maryem", message="hello there")
    assert out["ok"] is False and "shares a server" in out["error"], out
    assert not sd._load_sends(time.time()), "a failed send must not use up the quota"
    recs = transcript.read_thread("discord", "dm-111")
    assert recs and recs[-1]["kind"] == "owner_dm" and recs[-1]["ok"] is False
    assert recs[-1]["text"] == "hello there"


def test_success_is_logged_in_the_persons_existing_thread():
    _reset()
    _person("discord", "111", name="Maryem")
    from jarvis.channels import permissions
    msg = permissions.IncomingMessage(
        platform="discord", context=permissions.CTX_DM, user_id="111",
        user_handle="m", text="hey", thread_id="chan-77", message_id="m1")
    transcript.log_inbound("discord", msg,
                           permissions.Decision(True, "allowed", "", may_use_tools=True))
    assert _run_tool(person="Maryem", message="running late")["ok"] is True
    recs = transcript.read_thread("discord", "chan-77")
    assert recs[-1]["dir"] == "out" and recs[-1]["kind"] == "owner_dm"
    assert recs[-1]["ok"] is True and recs[-1]["text"] == "running late"


def test_audit_line_never_lands_in_a_server_channel_thread():
    """S1.1: a room thread holding the target's message is not their DM."""
    _reset()
    _person("discord", "222", name="Sam")
    from jarvis.channels import permissions
    room = permissions.IncomingMessage(
        platform="discord", context=permissions.CTX_GROUP, user_id="222",
        user_handle="sam", text="hello room", thread_id="chan-900",
        message_id="r1", guild_id="g1", channel_id="chan-900")
    transcript.log_inbound("discord", room,
                           permissions.Decision(True, "allowed", "", may_use_tools=True))
    assert sd._thread_for("discord", "222") == "dm-222"
    assert _run_tool(person="Sam", message="psst")["ok"] is True
    assert not any(r.get("kind") == "owner_dm"
                   for r in transcript.read_thread("discord", "chan-900"))
    assert transcript.read_thread("discord", "dm-222")[-1]["kind"] == "owner_dm"
    # a real DM thread for the same person is still found, even though the
    # room thread is newer
    dm = permissions.IncomingMessage(
        platform="discord", context=permissions.CTX_DM, user_id="222",
        user_handle="sam", text="hi", thread_id="chan-55", message_id="d1")
    transcript.log_inbound("discord", dm,
                           permissions.Decision(True, "allowed", "", may_use_tools=True))
    transcript.log_inbound("discord", room,
                           permissions.Decision(True, "allowed", "", may_use_tools=True))
    assert sd._thread_for("discord", "222") == "chan-55"


# --- outbound.dm_person (real function, config stubbed) ----------------

def test_dm_person_rejects_bad_input_without_raising():
    import importlib
    real = importlib.reload(outbound)
    assert real.dm_person("signal", "1", "x")[0] is False
    assert real.dm_person("discord", "", "x")[0] is False
    assert real.dm_person("discord", "1", "  ")[0] is False
    ok, why = real.dm_person("discord", "not-a-number", "hi")
    assert ok is False and ("numeric" in why or "disabled" in why), why


# --- wiring ------------------------------------------------------------

def test_registered_confirmed_and_scored():
    assert "send_dm" in sd.TOOLS and sd.TOOL_CONFIRM_REQUIRED == {"send_dm"}
    from jarvis import policy
    assert policy._base_risk("send_dm") == 50
    d = policy.dry_run("send_dm", {"person": "Maryem", "message": "hi", "platform": "discord"})
    assert "Maryem" in d["would"] and "hi" in d["would"], d
    assert d["decision"] in ("confirm", "review"), d
    # Scheduled context pushes it to at least review.
    assert policy.decide("send_dm", {"person": "x", "message": "y"},
                         context=policy.CTX_SCHEDULED)["decision"] != policy.ALLOW


def test_router_routes_send_phrases_but_not_inbox_or_owner_phrases():
    from jarvis import tool_router
    for text in ("DM Maryem that I'll be late", "send a message to Sam",
                 "tell her I'm on my way", "dm sam about dinner"):
        names = {m[1] for m in tool_router.route(text).matches}
        assert "send_dm" in names, text
    for text in ("who dmed me", "dm me when it's done", "show recent dms"):
        names = {m[1] for m in tool_router.route(text).matches}
        assert "send_dm" not in names, (text, names)


# --- scheduling a DM for later (`when`) ---------------------------------

def _sched_reset():
    from jarvis import scheduler
    for f in (scheduler.STORE_FILE,):
        for suffix in ("", ".bak", ".tmp"):
            try:
                Path(str(f) + suffix).unlink()
            except OSError:
                pass
    return scheduler


def test_when_schedules_a_parked_one_off_job_and_sends_nothing_now():
    _reset()
    scheduler = _sched_reset()
    _person("discord", "111", name="Maryem")
    out = _run_tool(person="maryem", message="happy birthday", when="in 6 hours 30 minutes")
    assert out["ok"] is True and out["scheduled"] is True, out
    assert not SENT, SENT                      # nothing delivered at schedule time
    assert out["needs_approval"] is True and out["status"] == scheduler.STATUS_NEEDS_APPROVAL, out
    job = scheduler.get(out["id"])
    assert job["action"]["tool"] == "send_dm" and job["max_runs"] == 1, job
    assert job["action"]["args"]["person"] == "discord:111", job
    assert "message" in (job["risk"] or {}).get("kinds", []), job["risk"]


def test_when_refuses_an_unknown_numeric_id_and_creates_no_job():
    _reset()
    scheduler = _sched_reset()
    out = _run_tool(person="987654321", platform="discord", message="hi", when="in 2 hours")
    assert out["ok"] is False and "known contact" in out["error"], out
    assert not SENT and not scheduler.list_jobs()
    # Sending right now to that typed id is unchanged.
    assert _run_tool(person="987654321", platform="discord", message="hi")["ok"] is True


def test_when_must_be_a_single_point_in_time_and_parseable():
    _reset()
    scheduler = _sched_reset()
    _person("discord", "111", name="Maryem")
    for bad in ("every day at 9am", "on startup", "when backup_done", "whenever"):
        out = _run_tool(person="maryem", message="x", when=bad)
        assert out["ok"] is False, (bad, out)
    assert not scheduler.list_jobs() and not SENT


def test_when_is_still_owner_only_and_never_from_an_unattended_run():
    _reset()
    scheduler = _sched_reset()
    _person("discord", "111", name="Maryem")
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps(
        {"platform": "discord", "user_id": "999", "is_owner": False})
    assert _run_tool(person="maryem", message="x", when="in 1 hour")["ok"] is False
    os.environ.pop("JARVIS_CHANNEL_SENDER")
    os.environ["JARVIS_SCHEDULED"] = "1"       # a scheduled run may not schedule more sends
    assert _run_tool(person="maryem", message="x", when="in 1 hour")["ok"] is False
    os.environ.pop("JARVIS_SCHEDULED")
    assert not scheduler.list_jobs() and not SENT


def test_when_dry_run_reports_schedulability_without_creating_anything():
    _reset()
    scheduler = _sched_reset()
    _person("discord", "111", name="Maryem")
    out = _run_tool(person="maryem", message="x", when="in 1 hour", dry_run=True)
    assert out["ok"] and out["can_schedule"] is True and not scheduler.list_jobs(), out
    out = _run_tool(person="555", platform="discord", message="x", when="in 1 hour", dry_run=True)
    assert out["can_schedule"] is False and "hint" in out, out
    assert not SENT


def test_approved_scheduled_job_still_delivers_to_the_known_contact():
    """End to end: schedule, approve, make it due, tick -> one DM goes out."""
    _reset()
    scheduler = _sched_reset()
    _person("discord", "111", name="Maryem")
    out = _run_tool(person="maryem", message="happy birthday", when="in 1 hour")
    scheduler.approve(out["id"])
    store = scheduler.load_store()
    for j in store["jobs"]:
        j["next_run"] = "2000-01-01T00:00:00"
        j["trigger"]["at"] = "2000-01-01T00:00:00"
    scheduler.save_store(store)
    scheduler.tick()
    assert SENT == [("discord", "111", "happy birthday")], SENT


# --- several recipients (`person` as a list) ----------------------------

def _three_people():
    _person("discord", "111", name="Maryem")
    _person("discord", "222", name="Sam")
    _person("instagram", "333", handle="leila_i", name="Leila")


def test_list_sends_to_every_known_person_once_each():
    _reset()
    _three_people()
    out = _run_tool(person=["maryem", "sam", "@leila_i"], message="Among Us in 30")
    assert out["ok"] is True and out["sent_count"] == 3 and out["failed_count"] == 0, out
    assert sorted(SENT) == [("discord", "111", "Among Us in 30"),
                            ("discord", "222", "Among Us in 30"),
                            ("instagram", "333", "Among Us in 30")], SENT
    assert [w["name"] for w in out["sent_to"]] == ["Maryem", "Sam", "Leila"], out
    # Each person is counted, logged and rate limited on their own.
    sends = sd._load_sends(time.time())
    assert len(sends["discord:111"]) == len(sends["discord:222"]) == 1, sends
    assert len(sends["_all"]) == 3, sends


def test_single_string_and_one_item_list_keep_the_original_shape():
    _reset()
    _three_people()
    for person in ("maryem", ["maryem"], '["maryem"]'):
        SENT.clear()
        sd._rate_file().unlink(missing_ok=True)
        out = _run_tool(person=person, message="hi")
        assert out["ok"] is True and out["sent_to"]["name"] == "Maryem", (person, out)
        assert "sent_count" not in out and len(SENT) == 1, (person, out)


def test_json_encoded_list_in_a_string_is_unpacked():
    _reset()
    _three_people()
    out = _run_tool(person='["maryem", "sam"]', message="hi")
    assert out["ok"] is True and out["sent_count"] == 2, out


def test_one_bad_name_fails_alone_and_the_rest_still_send():
    _reset()
    _three_people()
    out = _run_tool(person=["maryem", "nobody-by-this-name", "sam"], message="hi")
    assert out["ok"] is False and out["sent_count"] == 2 and out["failed_count"] == 1, out
    assert sorted(p for _, p, _ in SENT) == ["111", "222"], SENT
    bad = out["failed"][0]
    assert bad["person"] == "nobody-by-this-name" and "don't know" in bad["error"], out
    assert "known_contacts" in bad and "Do NOT resend" in out["hint"], out


def test_blocked_and_ambiguous_recipients_are_skipped_never_guessed():
    _reset()
    _person("discord", "111", name="Maryem")
    _person("discord", "222", name="Sam Ali", follow="blocked")
    _person("discord", "333", name="Alex One")
    _person("discord", "444", name="Alex Two")
    out = _run_tool(person=["maryem", "sam", "alex"], message="hi")
    assert out["sent_count"] == 1 and out["failed_count"] == 2, out
    assert SENT == [("discord", "111", "hi")], SENT
    errors = {f["person"]: f["error"] for f in out["failed"]}
    assert "blocked" in errors["sam"] and "more than one person" in errors["alex"], errors


def test_two_spellings_of_one_person_get_it_once():
    _reset()
    _three_people()
    out = _run_tool(person=["maryem", "discord:111", "MARYEM", "sam"], message="hi")
    assert out["ok"] is True and out["sent_count"] == 2, out
    assert sorted(p for _, p, _ in SENT) == ["111", "222"], SENT
    assert out["skipped"][0]["person"] == "discord:111", out


def test_rate_limit_stops_only_the_recipient_who_hit_it():
    _reset()
    _three_people()
    for _ in range(sd.MAX_PER_RECIPIENT):
        assert _run_tool(person="maryem", message="x")["ok"] is True
    SENT.clear()
    out = _run_tool(person=["maryem", "sam"], message="y")
    assert out["sent_count"] == 1 and out["failed_count"] == 1, out
    assert SENT == [("discord", "222", "y")], SENT
    assert "rate limit" in out["failed"][0]["error"], out


def test_delivery_failure_for_one_person_is_reported_and_not_counted():
    _reset()
    _three_people()

    def fake(platform, person_id, text):
        SENT.append((platform, str(person_id), text))
        return (False, "DMs closed") if str(person_id) == "222" else (True, "sent")
    outbound.dm_person = fake
    out = _run_tool(person=["maryem", "sam"], message="hi")
    assert out["sent_count"] == 1 and "DMs closed" in out["failed"][0]["error"], out
    sends = sd._load_sends(time.time())
    assert "discord:222" not in sends and len(sends["discord:111"]) == 1, sends


def test_too_many_recipients_or_a_bad_entry_sends_nothing():
    _reset()
    _three_people()
    many = [f"discord:{900 + i}" for i in range(sd.MAX_RECIPIENTS + 1)]
    out = _run_tool(person=many, message="hi")
    assert out["ok"] is False and "limit" in out["error"], out
    out = _run_tool(person=["maryem", {"name": "sam"}], message="hi")
    assert out["ok"] is False and "each entry" in out["error"], out
    assert _run_tool(person=[], message="hi")["error"] == "person is required"
    assert _run_tool(person=["", None], message="hi")["error"] == "person is required"
    assert not SENT, SENT


def test_list_is_still_owner_only_and_never_unattended():
    _reset()
    _three_people()
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps(
        {"platform": "discord", "user_id": "999", "is_owner": False})
    assert _run_tool(person=["maryem", "sam"], message="x")["ok"] is False
    os.environ.pop("JARVIS_CHANNEL_SENDER")
    os.environ["JARVIS_SCHEDULED"] = "1"
    assert _run_tool(person=["maryem", "sam"], message="x")["ok"] is False
    os.environ.pop("JARVIS_SCHEDULED")
    assert not SENT, SENT


def test_dry_run_with_a_list_resolves_everyone_and_sends_nothing():
    _reset()
    _three_people()
    out = _run_tool(person=["maryem", "sam"], message="x", dry_run=True)
    assert out["ok"] and out["dry_run"] and not SENT, out
    assert [w["user_id"] for w in out["would_send_to"]] == ["111", "222"], out
    out = _run_tool(person=["maryem", "ghost"], message="x", dry_run=True)
    assert out["ok"] is False and out["problems"][0]["person"] == "ghost", out
    assert [w["user_id"] for w in out["would_send_to"]] == ["111"] and not SENT, out
    assert not sd._rate_file().exists(), "a dry run must not count against the limit"


def test_when_with_a_list_makes_one_parked_job_naming_everyone():
    _reset()
    scheduler = _sched_reset()
    _three_people()
    out = _run_tool(person=["maryem", "sam"], message="game night", when="in 2 hours")
    assert out["ok"] is True and out["scheduled"] is True and not SENT, out
    assert [w["user_id"] for w in out["to"]] == ["111", "222"], out
    jobs = scheduler.list_jobs()
    assert len(jobs) == 1, jobs
    job = scheduler.get(out["id"])
    assert job["action"]["args"]["person"] == ["discord:111", "discord:222"], job
    assert "platform" not in job["action"]["args"], job
    assert out["status"] == scheduler.STATUS_NEEDS_APPROVAL, out
    lines = " ".join((job["risk"] or {}).get("lines", []))
    assert "discord:111" in lines and "discord:222" in lines and "game night" in lines, lines


def test_approved_list_job_delivers_to_everyone_when_it_fires():
    _reset()
    scheduler = _sched_reset()
    _three_people()
    out = _run_tool(person=["maryem", "sam", "leila_i"], message="game night",
                    when="in 1 hour")
    scheduler.approve(out["id"])
    store = scheduler.load_store()
    for j in store["jobs"]:
        j["next_run"] = "2000-01-01T00:00:00"
        j["trigger"]["at"] = "2000-01-01T00:00:00"
    scheduler.save_store(store)
    scheduler.tick()
    assert sorted(SENT) == [("discord", "111", "game night"),
                            ("discord", "222", "game night"),
                            ("instagram", "333", "game night")], SENT


def test_when_with_a_list_is_all_or_nothing():
    _reset()
    scheduler = _sched_reset()
    _three_people()
    out = _run_tool(person=["maryem", "ghost"], message="x", when="in 1 hour")
    assert out["ok"] is False and out["scheduled"] is False, out
    assert out["problems"][0]["person"] == "ghost", out
    out = _run_tool(person=["maryem", "555"], platform="discord", message="x", when="in 1 hour")
    assert out["ok"] is False and out["scheduled"] is False, out
    assert not scheduler.list_jobs() and not SENT


def test_dry_run_plus_when_with_a_list_creates_no_job():
    _reset()
    scheduler = _sched_reset()
    _three_people()
    out = _run_tool(person=["maryem", "sam"], message="x", when="in 1 hour", dry_run=True)
    assert out["ok"] and out["can_schedule"] is True, out
    assert not scheduler.list_jobs() and not SENT


def test_policy_summary_and_schema_understand_a_list():
    from jarvis import policy
    d = policy.dry_run("send_dm", {"person": ["Maryem", "Sam"], "message": "hi"})
    assert "Maryem" in d["would"] and "Sam" in d["would"], d
    schema = sd.TOOL_SCHEMAS[0]["parameters"]["properties"]["person"]
    kinds = [alt["type"] for alt in schema["anyOf"]]
    assert kinds == ["string", "array"], schema
    assert sd.TOOL_SCHEMAS[0]["parameters"]["required"] == ["person", "message"]


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
