"""Context-injection regression test (see the §3.6 dev_agent plan, §2 and
§10): a fake one-argument handler must keep behaving exactly as every
existing tool does today, and a fake two-argument handler must receive a
real tools.ToolContext as its second positional argument — both dispatched
through the *same* tools.execute_tool() codepath, so this is a direct
regression guard against breaking every existing one-argument tool while
adding the context mechanism.

No test framework dependency — plain asserts, runnable directly:

    python3 tests/test_tool_context.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import tools as system_tools  # noqa: E402


def _one_arg_handler(args):
    return {"ok": True, "got_args": args, "arity": 1}


def _two_arg_handler(args, context):
    return {
        "ok": True,
        "got_args": args,
        "arity": 2,
        "conv_id": context.conv_id,
        "ui": context.ui,
        "round_budget_remaining": context.round_budget_remaining(),
    }


def _with_scratch_tools(fn):
    """Temporarily register fake tools in the live TOOLS dict, routed
    through the same membership check execute_tool already uses
    (`name in CUSTOM_TOOLS`), so we exercise the real dispatch path rather
    than a copy of it. CUSTOM_TOOLS is itself a name -> handler dict (see
    custom_tools.CUSTOM_TOOLS), not a set — it needs the same two entries
    added to it as TOOLS, not just the bare names."""
    name_one = "_scratch_one_arg_tool"
    name_two = "_scratch_two_arg_tool"
    orig_tools = dict(system_tools.TOOLS)
    orig_custom = dict(system_tools.CUSTOM_TOOLS)
    try:
        system_tools.TOOLS[name_one] = _one_arg_handler
        system_tools.TOOLS[name_two] = _two_arg_handler
        system_tools.CUSTOM_TOOLS[name_one] = _one_arg_handler
        system_tools.CUSTOM_TOOLS[name_two] = _two_arg_handler
        fn(name_one, name_two)
    finally:
        system_tools.TOOLS.clear()
        system_tools.TOOLS.update(orig_tools)
        system_tools.CUSTOM_TOOLS.clear()
        system_tools.CUSTOM_TOOLS.update(orig_custom)


def test_one_arg_handler_unaffected_by_context():
    def body(name_one, _name_two):
        events = []
        context = system_tools.ToolContext(
            conv_id="conv-1",
            round_budget_remaining=lambda: 3,
            emit_event=lambda *a, **k: events.append((a, k)),
            ui="cli",
        )
        result = system_tools.execute_tool(name_one, {"x": 1}, context=context)
        assert result == {"ok": True, "got_args": {"x": 1}, "arity": 1}
        assert events == [], "a one-arg handler must never trigger emit_event itself"

    _with_scratch_tools(body)


def test_one_arg_handler_works_with_no_context_at_all():
    def body(name_one, _name_two):
        result = system_tools.execute_tool(name_one, {"x": 2})
        assert result == {"ok": True, "got_args": {"x": 2}, "arity": 1}

    _with_scratch_tools(body)


def test_two_arg_handler_receives_real_context():
    def body(_name_one, name_two):
        context = system_tools.ToolContext(
            conv_id="conv-42",
            round_budget_remaining=lambda: 5,
            emit_event=lambda *a, **k: None,
            ui="web",
        )
        result = system_tools.execute_tool(name_two, {"y": 1}, context=context)
        assert result["arity"] == 2
        assert result["conv_id"] == "conv-42"
        assert result["ui"] == "web"
        assert result["round_budget_remaining"] == 5

    _with_scratch_tools(body)


def test_two_arg_handler_without_context_falls_back_to_one_arg_call():
    # execute_tool only passes context when both _accepts_context(fn) AND
    # context is not None — a two-arg handler called with no context at
    # all must not blow up with a missing-argument TypeError; it degrades
    # to the plain fn(arguments) call and the handler must tolerate that
    # if it wants to be callable without a context (this test documents
    # the actual contract: today it raises, since _two_arg_handler
    # requires `context`; a real handler should give `context` a default
    # of None if it wants to support being called standalone).
    def body(_name_one, name_two):
        try:
            system_tools.execute_tool(name_two, {"y": 1})
        except Exception:
            raised = True
        else:
            raised = False
        # Documented current behavior: execute_tool never lets an
        # exception escape (it's caught and turned into {"error": ...}),
        # so even a handler that requires `context` and gets called
        # without one comes back as a clean error dict, not a crash.
        assert raised is False

    _with_scratch_tools(body)


def test_accepts_context_is_cached_per_handler():
    cache = system_tools._ACCEPTS_CONTEXT_CACHE
    assert system_tools._accepts_context(_one_arg_handler) is False
    assert system_tools._accepts_context(_two_arg_handler) is True
    assert cache.get(_one_arg_handler) is False
    assert cache.get(_two_arg_handler) is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
