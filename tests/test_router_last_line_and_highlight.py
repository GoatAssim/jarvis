"""F.10 items still open after jarvis-router-paste-confirmation.patch:
(3) the last line of a multi-paragraph message gets extra routing weight, and
(cause 3) the excerpt of a highlight-quote prompt must not vote in the router.
Run: python3 tests/test_router_last_line_and_highlight.py"""
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, tool_router  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


WRAP = ('The user highlighted this excerpt from the conversation and wants you to address it specifically:\n'
        '"""\n{quote}\n"""\n\n{own}')


def test_highlight_wrapper():
    quote = "I can do that.\nLet me know if you'd like me to handle it for you."
    msg = WRAP.format(quote=quote, own="run this custom command")
    check("the user's own words are what is left", ai_client._strip_highlight_excerpt(msg) == "run this custom command")
    raw = tool_router.route(msg)
    fixed = tool_router.route(ai_client._strip_highlight_excerpt(msg))
    check("BEFORE: the quoted 'let me know' pulled in channels/scheduling", {"channels", "scheduling"} & set(raw.groups), raw.groups)
    check("AFTER: routes on the user's own words (commands, no channels)", "commands" in fixed.groups and "channels" not in fixed.groups, fixed.groups)
    check("stock 'please respond about the excerpt' routes as empty",
          ai_client._strip_highlight_excerpt(WRAP.format(quote=quote, own="Please respond about the quoted excerpt.")) == "")
    plain = "move the report to the studying folder"
    check("an ordinary message is untouched", ai_client._strip_highlight_excerpt(plain) == plain)
    check("a multi-line quote is handled", ai_client._strip_highlight_excerpt(WRAP.format(quote="a\n\nb\nc", own="hi")) == "hi")


def test_last_line_bonus():
    orig = (tool_router.TOOL_KEYWORDS, tool_router.group_of, tool_router.TOOL_GROUPS)
    tool_router.TOOL_KEYWORDS = {"a_tool": {"alpha": 7}, "b_tool": {"beta": 6}}
    tool_router.group_of = lambda n: {"a_tool": "ga", "b_tool": "gb"}[n]
    tool_router.TOOL_GROUPS = {"ga": ["a_tool"], "gb": ["b_tool"]}
    try:
        filler = "some earlier context that goes on for a while. " * 6
        long_msg = f"{filler}\nalpha was mentioned here\n\nplease do beta now"
        short = "alpha and beta"
        multi_but_short = "alpha here\nmore\nbeta now"
        r_long = tool_router.route(long_msg).groups
        r_short = tool_router.route(short).groups
        r_msb = tool_router.route(multi_but_short).groups
        r_only_last = tool_router.route(f"{filler}\nx\nplease do beta now").groups
    finally:
        tool_router.TOOL_KEYWORDS, tool_router.group_of, tool_router.TOOL_GROUPS = orig
    check("a phrase on the last line of a long message beats a slightly stronger one earlier", r_long[0] == "gb", r_long)
    check("a one-line message routes exactly as before", r_short[0] == "ga", r_short)
    check("a short multi-line message routes exactly as before", r_msb[0] == "ga", r_msb)
    check("the bonus can't activate a group by itself (MIN_SCORE gate unchanged)", r_only_last == ["gb"], r_only_last)
    check("helper: None below the line/char thresholds", tool_router.last_line_for_bonus("a\nb") is None and tool_router.last_line_for_bonus("x" * 300) is None)


test_highlight_wrapper()
test_last_line_bonus()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
