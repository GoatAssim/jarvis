"""Regression tests for K.2.8 — 100k+ input-token burn containment.

Background (status audit / master plan, K.2.8, from a supplied ~123,368-
input-token conversation log): one conversation accumulated 77 requests
whose payloads, summed, contained 572 literal repeats of the same user
turn ("write_on_screen continue and enter"). The raw log itself is not
included in this repository — only its statistics are recorded in the
master plan — so the fixture below RECONSTRUCTS the documented pattern
(a long run of an unchanging turn, at a smaller but proportional scale)
rather than replaying the original file byte-for-byte. This mirrors how
K.2.1 handled the same gap for an earlier finding.

Two independent, complementary mechanisms are covered:

  K.2.8.1/.2 — conversations.conversation_messages() collapses a run of
  3+ consecutive identical user turns into one representative pair
  instead of resending every occurrence verbatim.

  K.2.8.3 — ai_client short-circuits a turn whose message is textually
  identical to the one that just got a tool call declined, without ever
  contacting a provider. A genuinely different message (including an
  explicit "yes"/"go ahead") is untouched by this.

No network, no live model.

Run: python3 tests/test_k28_token_burn.py
"""

import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp(prefix="jarvis_k28_"))

from jarvis import ai_client, conversations  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


REPEATED_TURN = "write_on_screen continue and enter"


# --- K.2.8.1/.2: collapsing a reconstructed stuck loop ----------------------

def test_collapse_function_directly():
    pairs = [
        (REPEATED_TURN, "ok trying"),
        (REPEATED_TURN, "still stuck"),
        (REPEATED_TURN, "trying again"),
        (REPEATED_TURN, "trying again"),
        ("do something else", "sure"),
    ]
    out = conversations._collapse_repeated_user_turns(pairs)
    check("4-run collapses to 1 pair", len(out) == 2, f"got {len(out)} pairs")
    check("collapsed pair keeps the LATEST assistant reply",
          out[0][1] == "trying again")
    check("repeat count is annotated", "4" in out[0][0] and "×" in out[0][0])
    check("unrelated trailing turn is untouched", out[1] == ("do something else", "sure"))


def test_short_runs_are_not_collapsed():
    # Two in a row is ordinary back-and-forth, not a stuck loop — left alone.
    pairs = [("no", "ok"), ("no", "understood"), ("something else", "sure")]
    out = conversations._collapse_repeated_user_turns(pairs)
    check("a run of 2 is left uncollapsed", out == pairs, f"got {out}")


def test_reconstructed_baseline_pattern_is_bounded():
    """A reconstruction of the A.1g pattern at a smaller scale: many
    consecutive turns, all the same unchanging nudge, none of them
    resolving anything. Assert the assembled request payload no longer
    grows linearly with the number of repeats."""
    conv_id = conversations.new_conversation("test")
    n = 40
    for _ in range(n):
        conversations.begin_exchange(conv_id, REPEATED_TURN)
        conversations.complete_exchange(
            conv_id, REPEATED_TURN,
            "I still need an explicit confirmation before typing that.",
            "(narration, no tool call)")

    msgs = conversations.conversation_messages(conv_id, max_exchanges=n)
    total_chars = sum(len(m["content"]) for m in msgs)

    # What the OLD (unfixed) behavior would have sent: every exchange
    # resent verbatim.
    naive_chars = n * (len(REPEATED_TURN) +
                        len("I still need an explicit confirmation before typing that."))

    check("collapsed payload is far smaller than the naive resend",
          total_chars < naive_chars * 0.2,
          f"collapsed={total_chars} naive={naive_chars}")
    check("collapsed payload does not grow with repeat count",
          len(msgs) <= 4, f"got {len(msgs)} messages for {n} identical exchanges")


def test_mixed_conversation_is_not_over_collapsed():
    """A normal, varied conversation must come through unchanged — this
    only targets an actual stuck loop, not ordinary dialogue that happens
    to repeat a short word here and there."""
    conv_id = conversations.new_conversation("test")
    turns = [
        ("what's the weather", "Sunny, 22C."),
        ("thanks", "You're welcome."),
        ("and tomorrow?", "Cloudy, 18C."),
        ("thanks", "Anytime."),
    ]
    for user_text, reply in turns:
        conversations.begin_exchange(conv_id, user_text)
        conversations.complete_exchange(conv_id, user_text, reply, "(ok)")

    msgs = conversations.conversation_messages(conv_id, max_exchanges=10)
    check("varied conversation keeps every exchange",
          len(msgs) == 2 * len(turns), f"got {len(msgs)} messages")
    check("no repeat annotation appears in a normal conversation",
          not any("repeated verbatim" in m["content"] for m in msgs))


# --- K.2.8.3: declined action is terminal within a stuck loop --------------

def _decline_result():
    return {"ok": False, "cancelled": True, "message": "The user declined to run 'write_on_screen'."}


def test_declined_action_extra_only_from_a_real_decline():
    extra = ai_client._declined_action_extra(
        [{"name": "write_on_screen", "arguments": {"text": "continue"}, "result": _decline_result()}])
    check("decline produces a declinedAction extra", extra is not None and extra["type"] == "declinedAction")

    no_extra = ai_client._declined_action_extra(
        [{"name": "write_on_screen", "arguments": {"text": "continue"}, "result": {"ok": True}}])
    check("a successful call produces no declinedAction extra", no_extra is None)

    check("no runs at all produces no extra", ai_client._declined_action_extra([]) is None)


def test_exact_repeat_after_decline_is_detected():
    conv_id = conversations.new_conversation("test")
    conversations.begin_exchange(conv_id, REPEATED_TURN)
    extra = ai_client._declined_action_extra(
        [{"name": "write_on_screen", "arguments": {"text": "continue"}, "result": _decline_result()}])
    conversations.complete_exchange(
        conv_id, REPEATED_TURN, "The user declined to run 'write_on_screen'.",
        "(declined)", extras=[extra])

    hit = ai_client._load_declined_repeat(conv_id, REPEATED_TURN)
    check("exact repeat after a decline is detected",
          hit == ("write_on_screen", {"text": "continue"}), f"got {hit}")

    case_and_space_varied = "  Write_On_Screen   Continue AND enter  "
    hit2 = ai_client._load_declined_repeat(conv_id, case_and_space_varied)
    check("case/whitespace-insensitive repeat is still detected", hit2 is not None)


def test_different_message_is_never_caught():
    conv_id = conversations.new_conversation("test")
    conversations.begin_exchange(conv_id, REPEATED_TURN)
    extra = ai_client._declined_action_extra(
        [{"name": "write_on_screen", "arguments": {"text": "continue"}, "result": _decline_result()}])
    conversations.complete_exchange(
        conv_id, REPEATED_TURN, "The user declined to run 'write_on_screen'.",
        "(declined)", extras=[extra])

    for different in ("yes go ahead", "okay do it", "write something else instead", ""):
        hit = ai_client._load_declined_repeat(conv_id, different)
        check(f"different message not caught: {different!r}", hit is None)


def test_declined_repeat_expires_after_ttl():
    conv_id = conversations.new_conversation("test")
    conversations.begin_exchange(conv_id, REPEATED_TURN)
    extra = ai_client._declined_action_extra(
        [{"name": "write_on_screen", "arguments": {"text": "continue"}, "result": _decline_result()}])
    conversations.complete_exchange(
        conv_id, REPEATED_TURN, "declined", "(declined)", extras=[extra])

    fresh = ai_client._load_declined_repeat(conv_id, REPEATED_TURN, now=None)
    check("fresh repeat (now=real time) is caught", fresh is not None)

    stale_now = extra["data"]["ts"] + ai_client.DECLINED_REPEAT_TTL_SECONDS + 1
    stale = ai_client._load_declined_repeat(conv_id, REPEATED_TURN, now=stale_now)
    check("repeat past the TTL is no longer caught", stale is None)


def test_only_the_last_exchange_counts():
    """A decline two exchanges back, followed by something that resolved
    it, must not keep blocking an unrelated later repeat of the old text."""
    conv_id = conversations.new_conversation("test")
    conversations.begin_exchange(conv_id, REPEATED_TURN)
    extra = ai_client._declined_action_extra(
        [{"name": "write_on_screen", "arguments": {"text": "continue"}, "result": _decline_result()}])
    conversations.complete_exchange(conv_id, REPEATED_TURN, "declined", "(declined)", extras=[extra])

    conversations.begin_exchange(conv_id, "never mind, do something else")
    conversations.complete_exchange(conv_id, "never mind, do something else", "Sure thing.", "(ok)")

    hit = ai_client._load_declined_repeat(conv_id, REPEATED_TURN)
    check("an unrelated intervening exchange clears the block", hit is None)


_TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_")]


def main():
    failures = []
    for test in _TESTS:
        before = len(FAIL)
        try:
            test()
        except AssertionError as e:
            failures.append((test.__name__, str(e)))
            continue
        if len(FAIL) > before:
            failures.append((test.__name__, "see FAILED lines above"))
    total = len(PASS) + len(FAIL)
    print(f"\n{len(PASS)}/{total} checks passed across "
          f"{len(_TESTS) - len(failures)}/{len(_TESTS)} tests")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
