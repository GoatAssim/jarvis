"""Tests for F.7: failover used to throw the transcript away.

After a key failed mid-turn (a 429 after some tool rounds), ask() rebuilt the
messages from scratch and appended a recap of the tool runs cut to
`tool_result_budget` (1600 chars) — so the next key saw a truncated blob, and
repeated read_file / run_shell. AIResult.tool_history (the failed attempt's own
transcript) was filled in by every adapter and read by nobody. Now ask()
carries it to the next attempt, middle-truncating oversized results.

Also: code_agent / run_shell / edit_file count as "something real ran" in the
recap (recap wording only, NOT _completed_mutations), and the recap no longer
tells a model that ran nothing that it MUST call a tool.

Run: python3 tests/test_failover_transcript.py
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_config, ai_providers, conversations, logs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


HIST = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "[tool result] I pasted this myself"},   # a user message that LOOKS like scaffold
    {"role": "assistant", "content": '[called read_file with {"path": "main.py"}]'},
    {"role": "user", "content": "[tool result] HEAD" + "x" * 5000 + "TAILMARK"},
    {"role": "assistant", "content": '[called edit_file with {"path": "main.py"}]'},
    {"role": "user", "content": "[tool result] edited"},
]


def test_scaffold_extraction_and_truncation():
    sc = ai_client._carried_scaffold(HIST)
    check("carry starts at the first assistant CALL, not a user look-alike",
          sc and sc[0]["role"] == "assistant" and len(sc) == 4, [m["content"][:30] for m in sc])
    check("nothing to carry from None / plain history",
          ai_client._carried_scaffold(None) == [] and ai_client._carried_scaffold(HIST[:2]) == [])
    t = ai_client._truncate_middle("A" * 100 + "M" * 3000 + "Z" * 100, 600)
    check("truncation keeps head AND tail, cuts the middle", t.startswith("A") and t.endswith("Z") and "omitted" in t and len(t) < 700, len(t))
    msgs = ai_client._carried_messages(HIST, 1600)
    big = msgs[1]["content"]
    check("oversized result is shortened but its end survives", len(big) < 1200 and big.endswith("TAILMARK"), len(big))
    check("later results are NOT squeezed out (the 1600-char-recap bug)", msgs[-1]["content"] == "[tool result] edited" and len(msgs) == 4)


def test_recap_wording():
    runs = [{"name": "code_agent", "arguments": {"task": "x"}, "result": {"ok": False}}]
    note = ai_client._tool_runs_note(runs, 4000)
    check("code_agent counts as something that really ran", "DID run" in note and "code_agent" in note, note)
    check("...but it is still not a 'completed mutation'", ai_client._completed_mutations(runs) == [])
    check("run_shell / edit_file likewise count in the recap",
          all(ai_client._ran_something_real(n) for n in ("run_shell", "edit_file")) and not ai_client._ran_something_real("list_dir"))
    idle = ai_client._tool_runs_note([{"name": "list_dir", "arguments": {}, "result": {"ok": True}}], 4000)
    check("no 'you MUST call the real tool' when nothing real ran", "MUST" not in idle and "No launch/install/command" not in idle, idle)
    check("still forbids claiming an action that didn't happen", "do not claim" in idle.lower(), idle)
    dead = ai_client._tool_runs_note([{"name": "list_dir", "arguments": {}, "result": {"ok": True}}], 4000, can_call_tools=False)
    check("no 'call the tool' nudge when tools are gone", "call the real tool" not in dead and "undone" in dead, dead)


@contextmanager
def _env(adapter):
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS), ai_client._make_tool_executor,
            conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
            conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    cfg = {"persona": {}, "providers": [{"name": "fake1", "type": "fake", "enabled": True,
                                         "api_keys": ["k1", "k2"], "model": "m"}],
           "defaults": {"tools_enabled": True, "prompt_mode": "full"}}

    class Ex:
        runs = [{"name": "read_file", "arguments": {"path": "main.py"}, "result": {"ok": True}}]

        def __call__(self, *a, **k):
            raise AssertionError("not used")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        ai_config.load_ai_config = lambda: cfg
        ai_providers.ADAPTERS["fake"] = adapter
        ai_client._make_tool_executor = lambda *a, **k: Ex()
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, _, ai_client._make_tool_executor, conversations.JARVIS_DIR,
             conversations.CONV_DIR, conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def test_next_key_gets_the_transcript_not_a_recap():
    seen = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return ai_providers.AIResult(False, error="rate limited or quota exceeded", tool_history=HIST)
        return ai_providers.AIResult(True, text="all done")

    with _env(adapter) as conv_id:
        r = ai_client.ask("fix main.py", commands=[], conversation_id=conv_id)
    check("second key answered", r.ok and r.text == "all done", (r.ok, r.attempts))
    first, second = seen
    joined1 = "\n".join(str(m["content"]) for m in first)
    joined2 = "\n".join(str(m["content"]) for m in second)
    check("first attempt had nothing carried", "[called" not in joined1)
    check("second attempt continues from the real transcript (calls + results)",
          "[called read_file" in joined2 and "[called edit_file" in joined2 and "edited" in joined2, joined2[-300:])
    check("the big result's tail survived to the second key", "TAILMARK" in joined2)
    check("no 1600-char recap stacked on top of the transcript", "Some tools already ran" not in joined2)
    check("carry follows the fresh user turn, so the roles still alternate",
          [m["role"] for m in second][-4:] == ["assistant", "user", "assistant", "user"], [m["role"] for m in second])


def test_attempt_with_no_history_falls_back_to_the_recap():
    seen = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return ai_providers.AIResult(False, error="rate limited or quota exceeded")   # no tool_history
        return ai_providers.AIResult(True, text="ok")

    with _env(adapter) as conv_id:
        ai_client.ask("fix main.py", commands=[], conversation_id=conv_id)
    joined2 = "\n".join(str(m["content"]) for m in seen[1])
    check("no history to carry -> the recap still tells the next key what ran", "Some tools already ran" in joined2 and "read_file" in joined2, joined2[-200:])


for fn in (test_scaffold_extraction_and_truncation, test_recap_wording,
           test_next_key_gets_the_transcript_not_a_recap, test_attempt_with_no_history_falls_back_to_the_recap):
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
