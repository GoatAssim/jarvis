"""Tests for ai_client.ask()'s "every provider failed" tail, specifically
the fix for: a scheduled task's tools could complete the real work and
then every provider would still fail on the follow-up call needed to
compose a closing reply — and the whole ask() used to report that as a
flat failure, discarding the fact that the actual requested action had
already happened. See KNOWN-ISSUES-AND-GAPS.md's "MCP schema translation"
neighbor entry for the analogous "don't discard information you already
have" fix; this one is ai_client._completed_mutations /
_summarize_completed_mutations plus the AskResult.degraded flag.

Same no-framework, plain-assert convention as test_provider_override.py —
runnable directly: python3 tests/test_provider_failover_completed_work.py
"""

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_config, ai_providers, conversations, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# ---------------------------------------------------------------------------
# Pure unit tests — _completed_mutations / _summarize_completed_mutations
# ---------------------------------------------------------------------------

def test_completed_mutations_filters_to_successful_mutating_tools():
    runs = [
        {"name": "search_tools", "arguments": {}, "result": {"ok": True}},  # read-only, excluded
        {"name": "write_file", "arguments": {"path": "x"}, "result": {"ok": True, "path": "x"}},
        {"name": "run_command", "arguments": {}, "result": {"error": "boom"}},  # mutating but failed
        {"name": "memory_save", "arguments": {}, "result": {"ok": True}},
    ]
    done = ai_client._completed_mutations(runs)
    names = [r["name"] for r in done]
    check("read-only tool is excluded", "search_tools" not in names, names)
    check("a mutating tool that itself errored is excluded", "run_command" not in names, names)
    check("successful mutating tools are included", set(names) == {"write_file", "memory_save"}, names)


def test_completed_mutations_empty_when_nothing_ran():
    check("no runs -> nothing completed", ai_client._completed_mutations([]) == [])
    check("None runs -> nothing completed", ai_client._completed_mutations(None) == [])


def test_summarize_completed_mutations_mentions_each_tool():
    runs = [
        {"name": "write_file", "arguments": {}, "result": {"ok": True, "path": "/tmp/x.txt"}},
        {"name": "memory_save", "arguments": {}, "result": {"ok": True}},
    ]
    text = ai_client._summarize_completed_mutations(runs)
    check("mentions the first tool and its path", "write_file" in text and "/tmp/x.txt" in text, text)
    check("mentions the second tool", "memory_save" in text, text)
    check("says the closing reply failed, not that everything failed",
          "closing reply" in text or "provider" in text, text)


# ---------------------------------------------------------------------------
# End-to-end through ask() — every provider fails, but a mutation already
# completed via a stubbed tool_executor.
# ---------------------------------------------------------------------------

@contextmanager
def _isolated_ask_env():
    orig = {
        "conv_dir": conversations.JARVIS_DIR, "conv_conv_dir": conversations.CONV_DIR,
        "conv_index": conversations.INDEX_FILE, "conv_current": conversations.CURRENT_FILE,
        "logs_dir": logs.JARVIS_DIR, "logs_log_dir": logs.LOG_DIR,
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR = tmp
        logs.LOG_DIR = tmp / "logs"
        try:
            yield conversations.get_current_id()
        finally:
            conversations.JARVIS_DIR = orig["conv_dir"]
            conversations.CONV_DIR = orig["conv_conv_dir"]
            conversations.INDEX_FILE = orig["conv_index"]
            conversations.CURRENT_FILE = orig["conv_current"]
            logs.JARVIS_DIR = orig["logs_dir"]
            logs.LOG_DIR = orig["logs_log_dir"]


class _StubExecutor:
    """Stands in for the real tool_executor _make_tool_executor() builds —
    just needs a `.runs` attribute and to be callable, matching how ask()
    uses it (as tool_executor and via getattr(tool_executor, "runs", ...))."""

    def __init__(self, runs):
        self.runs = runs

    def __call__(self, name, arguments):
        raise AssertionError("the fake adapter in this test never calls tool_executor directly")


@contextmanager
def _fake_failing_provider_with_completed_tool(mutating_tool_name="write_file"):
    """Two enabled fake providers, both of which always fail the actual
    text-generation call — simulating every configured provider being
    exhausted — while _make_tool_executor is monkeypatched to hand back a
    stub executor whose `.runs` already contains one successful mutating
    tool call, as if the model had called it earlier this turn before
    every remaining attempt failed."""
    orig_load = ai_config.load_ai_config
    orig_adapters = dict(ai_providers.ADAPTERS)
    orig_make_executor = ai_client._make_tool_executor

    cfg = {
        "persona": {},
        "providers": [
            {"name": "fakeprov1", "type": "fake", "enabled": True, "api_keys": ["k1"], "model": "m"},
            {"name": "fakeprov2", "type": "fake", "enabled": True, "api_keys": ["k2"], "model": "m"},
        ],
        "defaults": {"tools_enabled": True, "prompt_mode": "full"},
    }

    def fake_adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        return ai_providers.AIResult(False, error="fake provider exhausted (no quota left)")

    stub_runs = [{
        "name": mutating_tool_name,
        "arguments": {"path": "/tmp/example.txt", "content": "done"},
        "result": {"ok": True, "path": "/tmp/example.txt"},
    }]

    def fake_make_tool_executor(*a, **kw):
        return _StubExecutor(stub_runs)

    ai_config.load_ai_config = lambda: cfg
    ai_providers.ADAPTERS["fake"] = fake_adapter
    ai_client._make_tool_executor = fake_make_tool_executor
    try:
        yield stub_runs
    finally:
        ai_config.load_ai_config = orig_load
        ai_providers.ADAPTERS.clear()
        ai_providers.ADAPTERS.update(orig_adapters)
        ai_client._make_tool_executor = orig_make_executor


def test_ask_reports_degraded_success_when_tools_completed_before_exhaustion():
    with _isolated_ask_env() as conv_id, _fake_failing_provider_with_completed_tool() as stub_runs:
        result = ai_client.ask("please write the file", commands=[], conversation_id=conv_id)

        check("ask() reports ok=True, not a hard failure", result.ok is True)
        check("the result is flagged as degraded (mechanical, not model-written)",
              result.degraded is True)
        check("every configured provider was still recorded as an attempt",
              len(result.attempts) >= 2, result.attempts)
        check("the synthesized text mentions the tool that actually ran",
              "write_file" in result.text and "/tmp/example.txt" in result.text, result.text)

        # And the saved exchange reflects a completed turn, not an
        # abandoned one — this is the persistence half of the same fix.
        entries = conversations.get_conversation(conv_id)
        exchanges = entries.get("exchanges") or [] if isinstance(entries, dict) else []
        check("the exchange was saved as completed, not left pending/abandoned",
              bool(exchanges) and bool(exchanges[-1].get("jarvis")), str(exchanges))


def test_ask_still_reports_hard_failure_with_no_completed_tools():
    # Same total-exhaustion scenario, but nothing ever ran — the pre-fix
    # behavior (ok=False) must still hold when there's genuinely no work to
    # report.
    with _isolated_ask_env() as conv_id, _fake_failing_provider_with_completed_tool() as stub_runs:
        stub_runs.clear()  # nothing "ran" this turn
        result = ai_client.ask("please write the file", commands=[], conversation_id=conv_id)
        check("ask() still reports a hard failure when nothing completed", result.ok is False)
        check("degraded is not set on a genuine hard failure", not result.degraded)


for fn in [
    test_completed_mutations_filters_to_successful_mutating_tools,
    test_completed_mutations_empty_when_nothing_ran,
    test_summarize_completed_mutations_mentions_each_tool,
    test_ask_reports_degraded_success_when_tools_completed_before_exhaustion,
    test_ask_still_reports_hard_failure_with_no_completed_tools,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
