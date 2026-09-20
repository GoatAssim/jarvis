"""Tests for the F.10 fix (master plan Part F): the router scores the
whole message text, including a pasted copy of Jarvis's own previous
reply — so a closing line like "Let me know if you'd like me to handle
it..." can out-score the user's actual instruction sitting right next to
it. Case 2b: the user pasted Jarvis's reply back plus "YES PLZ RUN THIS
CUSTOM COMMAND PLZ"; the router matched `channels`/`scheduling` off
"let me know" and REPLACED the sticky `files` group the task actually
needed.

Covers items 1 and 2 of F.10's suggested fix (item 3 — extra weight for
a message's last line — touches tool_router.route()'s scoring algorithm
itself, which AGENTS.md says needs a matching update to
tests/interactive_inspector.py's mirror; not done here, see master plan):

1. `conversations.last_assistant_reply` + `ai_client._strip_pasted_previous_reply`
   — strip an exact, substantial paste of Jarvis's own last reply out of
   the text handed to tool_router.route(), without touching the actual
   user_text sent to the model / saved to history.
2. `ai_client._looks_like_short_confirmation` +
   `ai_client._merged_sticky_groups_for_confirmation` — a confident-but-
   narrow route on a short confirmation ("yes plz run this") merges into
   a still-live sticky group set instead of replacing it outright.

Run: python3 tests/test_router_paste_and_confirmation.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-test-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, conversations, tool_router  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


REAL_PREV_REPLY = (
    "I've looked into this. Let me know if you'd like me to handle it "
    "and I can take care of that for you right away."
)


def _conv_with_reply(reply=REAL_PREV_REPLY, user="can you look into moving that file"):
    conv_id = conversations.new_conversation(make_current=False)
    conversations.append_exchange(conv_id, user, reply, "test")
    return conv_id


# ---------------------------------------------------------------- item 1


def test_last_assistant_reply_returns_most_recent():
    conv_id = _conv_with_reply()
    check("last_assistant_reply returns the reply just appended", conversations.last_assistant_reply(conv_id) == REAL_PREV_REPLY)


def test_last_assistant_reply_none_cases():
    check("no conversation id -> None", conversations.last_assistant_reply(None) is None)
    check("bogus conversation id -> None", conversations.last_assistant_reply("not-a-real-id") is None)
    empty_conv = conversations.new_conversation(make_current=False)
    check("conversation with no exchanges yet -> None", conversations.last_assistant_reply(empty_conv) is None)


def test_strip_pasted_previous_reply_removes_exact_paste():
    conv_id = _conv_with_reply()
    pasted = REAL_PREV_REPLY + "\n\nYES PLZ RUN THIS CUSTOM COMMAND PLZ"
    stripped = ai_client._strip_pasted_previous_reply(pasted, conv_id)
    check("previous reply text is gone", "Let me know" not in stripped, stripped)
    check("user's own words survive", "RUN THIS CUSTOM COMMAND" in stripped, stripped)


def test_strip_pasted_previous_reply_fixes_the_real_case_2b_route():
    """End-to-end proof this actually fixes the observed failure: after
    stripping, tool_router.route() picks `commands`, not
    `channels`/`scheduling` off the assistant's own "let me know"."""
    conv_id = _conv_with_reply()
    pasted = REAL_PREV_REPLY + "\n\nYES PLZ RUN THIS CUSTOM COMMAND PLZ"
    stripped = ai_client._strip_pasted_previous_reply(pasted, conv_id)
    route = tool_router.route(stripped)
    check("route is confident", route.confident, route.groups)
    check("commands is matched", "commands" in route.groups, route.groups)
    check("channels is NOT matched", "channels" not in route.groups, route.groups)
    check("scheduling is NOT matched", "scheduling" not in route.groups, route.groups)


def test_strip_pasted_previous_reply_leaves_short_or_no_match_alone():
    conv_id = _conv_with_reply()
    check("no conv_id -> unchanged", ai_client._strip_pasted_previous_reply("hello there", None) == "hello there")
    check("text that doesn't contain the reply -> unchanged", ai_client._strip_pasted_previous_reply("just a normal message", conv_id) == "just a normal message")
    short_reply_conv = _conv_with_reply(reply="ok")
    check(
        "a too-short previous reply is never stripped (avoids eating real user text)",
        ai_client._strip_pasted_previous_reply("ok, do the thing", short_reply_conv) == "ok, do the thing",
    )


def test_strip_pasted_previous_reply_never_returns_empty_string():
    # If the WHOLE message is just the pasted reply with nothing added,
    # fall back to the original text rather than routing on "".
    conv_id = _conv_with_reply()
    check(
        "paste with nothing added falls back to original text",
        ai_client._strip_pasted_previous_reply(REAL_PREV_REPLY, conv_id) == REAL_PREV_REPLY,
    )


def test_strip_pasted_previous_reply_does_not_touch_original_text_object():
    # Sanity: this function must be side-effect-free on conversation state
    # and must not mutate its input.
    conv_id = _conv_with_reply()
    original = REAL_PREV_REPLY + "\n\nYES PLZ RUN THIS CUSTOM COMMAND PLZ"
    before = str(original)
    ai_client._strip_pasted_previous_reply(original, conv_id)
    check("input string unchanged", original == before)


# ---------------------------------------------------------------- item 2


def test_looks_like_short_confirmation_positive_cases():
    for text in ["yes", "Yes.", "yep!", "sure", "ok", "okay", "please", "plz",
                 "go ahead", "go for it", "do it", "run it", "run this",
                 "confirmed", "proceed", "sounds good", "YES PLZ RUN THIS CUSTOM COMMAND PLZ"]:
        check(f"'{text}' recognized as a short confirmation", ai_client._looks_like_short_confirmation(text), text)


def test_looks_like_short_confirmation_negative_cases():
    for text in ["", "  ", None,
                 "please open the browser and navigate to google and search for cats now",
                 "what time is it", "tell me a joke about penguins please",
                 # Regression: "please"/"sure"/"ok" are real confirmation
                 # words but are ALSO ordinary sentence openers — a
                 # prefix-only match previously let these false-positive.
                 "please install ffmpeg for me",
                 "ok let's do the taxes now",
                 "sure thing order me a pizza"]:
        check(f"'{text}' is NOT a short confirmation", not ai_client._looks_like_short_confirmation(text), text)


def test_merged_sticky_groups_none_when_not_confident():
    route = tool_router.route("")  # empty text -> never confident
    check("no merge when route isn't confident", ai_client._merged_sticky_groups_for_confirmation(route, ["files"], "yes") is None)


def test_merged_sticky_groups_none_without_existing_sticky():
    route = tool_router.route("run this custom command")
    check("no merge when there's no existing sticky context", ai_client._merged_sticky_groups_for_confirmation(route, [], "yes") is None)
    check("no merge when existing sticky is None", ai_client._merged_sticky_groups_for_confirmation(route, None, "yes") is None)


def test_merged_sticky_groups_none_when_not_a_confirmation():
    route = tool_router.route("run this custom command")
    check(
        "no merge for a confident, non-confirmation message (real topic change)",
        ai_client._merged_sticky_groups_for_confirmation(route, ["files"], "please install ffmpeg for me") is None,
    )


def test_merged_sticky_groups_merges_on_confirmation():
    route = tool_router.route("YES PLZ RUN THIS CUSTOM COMMAND PLZ")
    check("sanity: this route is confident and matches commands", route.confident and "commands" in route.groups, route.groups)
    merged = ai_client._merged_sticky_groups_for_confirmation(route, ["files"], "YES PLZ RUN THIS CUSTOM COMMAND PLZ")
    check("merge result is not None", merged is not None, merged)
    check("existing sticky group (files) is preserved", "files" in merged, merged)
    check("newly-matched group (commands) is added", "commands" in merged, merged)
    check("existing group stays first (order preserved)", merged[0] == "files", merged)


def test_merged_sticky_groups_does_not_duplicate_existing_group():
    route = tool_router.route("YES PLZ RUN THIS CUSTOM COMMAND PLZ")
    merged = ai_client._merged_sticky_groups_for_confirmation(route, ["commands"], "YES PLZ RUN THIS CUSTOM COMMAND PLZ")
    check("no duplicate group in merged list", merged.count("commands") == 1, merged)


for fn in [
    test_last_assistant_reply_returns_most_recent,
    test_last_assistant_reply_none_cases,
    test_strip_pasted_previous_reply_removes_exact_paste,
    test_strip_pasted_previous_reply_fixes_the_real_case_2b_route,
    test_strip_pasted_previous_reply_leaves_short_or_no_match_alone,
    test_strip_pasted_previous_reply_never_returns_empty_string,
    test_strip_pasted_previous_reply_does_not_touch_original_text_object,
    test_looks_like_short_confirmation_positive_cases,
    test_looks_like_short_confirmation_negative_cases,
    test_merged_sticky_groups_none_when_not_confident,
    test_merged_sticky_groups_none_without_existing_sticky,
    test_merged_sticky_groups_none_when_not_a_confirmation,
    test_merged_sticky_groups_merges_on_confirmation,
    test_merged_sticky_groups_does_not_duplicate_existing_group,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
