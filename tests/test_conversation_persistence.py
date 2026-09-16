"""Regression tests for the conversation-saving bugs.

Run: python3 ../tests/test_conversation_persistence.py   (from jarvis-cli/)

Three separate bugs, all of which looked to a user like "my conversation
didn't save properly":

  1. Persistence happened ONCE, inside `if result.ok:` in ai_client.ask().
     Anything that stopped the process first — the web UI's Stop button
     (which kills the child outright), every provider failing, a crash —
     discarded the user's message entirely. On a first message that also
     meant no title was ever generated, so the whole conversation looked
     like it had never happened.

  2. present_file was the only media-producing tool with no entry in
     ai_client._extras_from_runs, so its card existed only in the live
     JARVIS_MEDIA stream and vanished on reload.

  3. An unanswered turn, once saved, must not be fed back to a provider as
     an empty assistant message (a hard 400 on Anthropic).

These test the store-level contract, which is where all three were fixed.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, conversations  # noqa: E402

PASS, FAIL = [], []
CONV = "a1b2c3d4e5f60718"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_conv_test_"))
    conversations.JARVIS_DIR = tmp
    conversations.CONV_DIR = tmp / "conversations"
    conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
    conversations.CURRENT_FILE = tmp / "current_conversation.json"
    conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
    return tmp


def exchanges():
    return (conversations.get_conversation(CONV) or {}).get("exchanges") or []


def test_user_message_survives_an_abort():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        # Simulate the real sequence: ask() writes the user's half, then the
        # process is killed before any reply exists.
        conversations.begin_exchange(CONV, "what is the airspeed of a swallow")
        saved = exchanges()
        check("the user's message is on disk before any provider replies", len(saved) == 1)
        check("and it's exactly what they typed",
              saved[0]["user"] == "what is the airspeed of a swallow")
        check("marked pending until something answers", saved[0].get("pending") is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_abort_marks_the_turn_interrupted():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        conversations.begin_exchange(CONV, "long running question")
        check("abandoning a pending turn reports success",
              conversations.abandon_exchange(CONV, "long running question") is True)
        saved = exchanges()
        check("the turn is still there", len(saved) == 1)
        check("no longer pending", saved[0].get("pending") is None)
        check("and says why it has no reply", saved[0].get("interrupted") == "interrupted")
        check("abandoning again is a no-op",
              conversations.abandon_exchange(CONV, "long running question") is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_success_upgrades_in_place_without_duplicating():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        conversations.begin_exchange(CONV, "hello")
        count = conversations.complete_exchange(CONV, "hello", "hi there", "openai")
        saved = exchanges()
        # The duplication risk: begin + complete must produce ONE turn.
        check("begin+complete leaves exactly one exchange", len(saved) == 1, str(len(saved)))
        check("complete_exchange returns the new count", count == 1)
        check("the reply landed", saved[0]["jarvis"] == "hi there")
        check("the provider is recorded", saved[0]["provider"] == "openai")
        check("pending is cleared", saved[0].get("pending") is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_complete_still_works_if_begin_never_ran():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        # begin_exchange can fail (unwritable disk). Losing the REPLY because
        # of that would be strictly worse than the bug this all fixes.
        conversations.complete_exchange(CONV, "orphan", "answered anyway", "groq")
        saved = exchanges()
        check("a reply with no pending turn is still saved", len(saved) == 1)
        check("with both halves intact",
              saved[0]["user"] == "orphan" and saved[0]["jarvis"] == "answered anyway")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unanswered_turns_never_reach_a_provider():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        conversations.complete_exchange(CONV, "real question", "real answer", "openai")
        conversations.begin_exchange(CONV, "aborted question")
        conversations.abandon_exchange(CONV, "aborted question")

        check("both turns are stored for the UI", len(exchanges()) == 2)
        messages = conversations.conversation_messages(CONV)
        blob = " ".join(str(m.get("content") or "") for m in messages)
        check("the answered turn is in the prompt", "real answer" in blob)
        # An assistant message with empty content is a hard 400 on Anthropic.
        check("no empty assistant message is emitted",
              all(str(m.get("content") or "").strip() for m in messages))
        check("the aborted turn is excluded from the prompt", "aborted question" not in blob)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_abandon_pending_turn_hook():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        conversations.begin_exchange(CONV, "signal test")
        ai_client._pending_turn[0] = (CONV, "signal test")
        # This is what cli.py's SIGTERM handler calls.
        check("the interrupt hook flushes the turn",
              ai_client.abandon_pending_turn("cancelled") is True)
        check("recording the reason it gave", exchanges()[0].get("interrupted") == "cancelled")
        check("calling it twice is harmless", ai_client.abandon_pending_turn() is False)
    finally:
        ai_client._pending_turn[0] = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_present_file_is_persisted_as_an_extra():
    tmp = fresh()
    try:
        # The exact shape _make_tool_executor records for a present_file call.
        runs = [{
            "name": "present_file",
            "arguments": {"path": "/home/me/report.pdf"},
            "result": {
                "ok": True, "name": "report.pdf", "type": "file",
                "path": "/home/me/report.pdf", "size_bytes": 4096,
                "job_id": "job123", "download_filename": "report.pdf",
            },
        }]
        extras = ai_client._extras_from_runs(runs)
        kinds = [e["type"] for e in extras]
        check("present_file now produces an extra", "presentFile" in kinds, str(kinds))
        data = next(e["data"] for e in extras if e["type"] == "presentFile")
        # Field names must match app.js's showAskPresentFile(info) exactly,
        # so replay and live rendering share one renderer.
        for field in ("jobId", "filename", "name", "type", "sizeBytes", "path"):
            check(f"extra carries {field}", field in data, str(sorted(data)))
        check("the download job id survives", data["jobId"] == "job123")
        check("the path survives", data["path"] == "/home/me/report.pdf")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_present_file_extra_round_trips_through_storage():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        extras = ai_client._extras_from_runs([{
            "name": "present_file", "arguments": {},
            "result": {"ok": True, "name": "a.txt", "type": "file", "path": "/tmp/a.txt",
                       "size_bytes": 12, "job_id": "j1", "download_filename": "a.txt"},
        }])
        conversations.begin_exchange(CONV, "show me the file")
        conversations.complete_exchange(CONV, "show me the file", "Here it is.",
                                        "openai", extras=extras)
        reloaded = exchanges()[0].get("extras") or []
        # This is the actual bug: the card has to survive a reload.
        check("the card survives a save/reload cycle",
              any(e.get("type") == "presentFile" for e in reloaded), str(reloaded))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_extras_are_kept_on_an_aborted_turn():
    tmp = fresh()
    try:
        conversations.new_conversation(title="t")
        conversations.begin_exchange(CONV, "take a screenshot then think hard")
        extras = ai_client._extras_from_runs([
            {"name": "take_screenshot", "arguments": {}, "result": {"ok": True, "file": "s.png"}},
        ])
        # A turn aborted halfway may already have done real work; that work
        # happened whether or not a reply arrived.
        conversations.abandon_exchange(CONV, "take a screenshot then think hard",
                                       reason="interrupted", extras=extras)
        saved = exchanges()[0]
        check("work done before the abort is still recorded",
              any(e.get("type") == "screenshot" for e in saved.get("extras") or []))
        check("and the turn is still marked interrupted", saved.get("interrupted") == "interrupted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


for fn in [
    test_user_message_survives_an_abort, test_abort_marks_the_turn_interrupted,
    test_success_upgrades_in_place_without_duplicating,
    test_complete_still_works_if_begin_never_ran,
    test_unanswered_turns_never_reach_a_provider, test_abandon_pending_turn_hook,
    test_present_file_is_persisted_as_an_extra,
    test_present_file_extra_round_trips_through_storage,
    test_extras_are_kept_on_an_aborted_turn,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
