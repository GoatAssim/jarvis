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
