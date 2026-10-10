"""Tests for the Tool Maker conversation (master plan L.53): jarvis/tool_maker.py and
its hooks in tools.py, ai_client.py, cli.py (conv-new flags) and conversations.py.

What is pinned here:
  1. The boundary. While JARVIS_TOOL_MAKER_CONTEXT_FILE is set, every tool in
     tool_maker.WITHHELD_TOOLS is hidden (schema lists, search, get_tool_schema) and
     REFUSED by execute_tool, for the owner too; with it unset nothing changes. The
     restriction holds even when the context file is missing, corrupt or oversize
     (fail closed). Every withheld name is a real tool (a rename must not silently
     un-withhold it), and the set covers the generic ways to write a file / run a program.
  2. The context. load_context() accepts only a well-formed, correctly named file;
     prompt_context() carries the tool's name and WHOLE current source and the rules;
     the block lands in the per-turn TAIL of the system prompt, never the cached prefix.
  3. History. collapse_code_for_history() shrinks long python fences in ASSISTANT turns
     only, leaves short snippets and user turns alone, and does not mutate its input.
  4. The conversation record. conv-new accepts --origin tool-maker (and nothing else),
     does not make it the current conversation; set_origin() never rewrites a different
     origin; stamp_conversation() follows the tool's name.

No network. ~/.jarvis is redirected to a temp dir before any jarvis import.

Run: python3 tests/test_tool_maker.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("JARVIS_TOOL_MAKER_CONTEXT_FILE", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, cli, conversations, tool_maker, tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


_TMP = Path(tempfile.mkdtemp(prefix="jarvis-tm-test-"))


def write_ctx(payload, name=None, raw=None):
    path = _TMP / (name or (tool_maker.CONTEXT_PREFIX + os.urandom(4).hex() + ".json"))
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return path


@contextlib.contextmanager
def tool_maker_env(path):
    old = os.environ.get(tool_maker.CONTEXT_ENV)
    os.environ[tool_maker.CONTEXT_ENV] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(tool_maker.CONTEXT_ENV, None)
        else:
            os.environ[tool_maker.CONTEXT_ENV] = old


SOURCE = 'TOOL_GROUP = "demo"\n\ndef hello(args):\n    return {"ok": True}\n'

# ---- 1. the boundary ----------------------------------------------------------------------

def test_withheld_names_are_real_tools():
    names = {s["name"] for s in tools.TOOL_SCHEMAS}
    missing = sorted(tool_maker.WITHHELD_TOOLS - names)
    check("every withheld name is a real tool (a rename must not silently un-withhold it)", not missing, missing)


def test_withheld_covers_the_generic_write_and_run_paths():
    must = {"write_file", "edit_file", "run_shell", "run_command", "run_custom_command", "git_run",
            "code_agent", "dev_agent", "spawn_subagent"}
    check("the set covers the ways to write a file or run a program", must <= tool_maker.WITHHELD_TOOLS,
          sorted(must - tool_maker.WITHHELD_TOOLS))


def test_inactive_by_default():
    check("no variable: nothing is withheld", tool_maker.withheld_tools() == frozenset())
    check("no variable: not active", tool_maker.active() is False)
    names = {s["name"] for s in tools.tool_schemas_for_session()}
    check("no variable: write_file is offered as before", "write_file" in names)
    check("no variable: hidden_from_sender() gains nothing", not (tools.hidden_from_sender() & tool_maker.WITHHELD_TOOLS))


def test_active_hides_every_withheld_tool():
    with tool_maker_env(write_ctx({"name": "demo", "source": SOURCE})):
        check("active", tool_maker.active())
        offered = {s["name"] for s in tools.tool_schemas_for_session()}
        check("session schemas omit every withheld tool", not (offered & tool_maker.WITHHELD_TOOLS),
              sorted(offered & tool_maker.WITHHELD_TOOLS))
        check("ordinary tools are still offered", "read_file" in offered or len(offered) > 20)
        listed = {s["name"] for s in tools.schemas_for_tools(sorted(tool_maker.WITHHELD_TOOLS | {"read_file"}))}
        check("schemas_for_tools drops them too", not (listed & tool_maker.WITHHELD_TOOLS), sorted(listed))
        found = tools.tool_search_tools({"query": "write file"})
        text = json.dumps(found)
        check("search_tools never lists write_file", '"write_file"' not in text, text[:200])
        got = tools.tool_get_tool_schema({"name": "write_file"})
        check("get_tool_schema refuses it, naming the buttons",
              "error" in got and "Validate" in got["error"] and "Save" in got["error"], got)


def test_active_refuses_every_withheld_tool_at_execution():
    with tool_maker_env(write_ctx({"name": "demo", "source": SOURCE})):
        bad = []
        for name in sorted(tool_maker.WITHHELD_TOOLS):
            for owner in (False, True):
                out = tools.execute_tool(name, {}, owner=owner)
                if not (isinstance(out, dict) and out.get("ok") is False and "Tool Maker" in str(out.get("error"))):
                    bad.append((name, owner, str(out)[:80]))
        check("execute_tool refuses all of them, for the owner too", not bad, bad)


def test_fail_closed_when_the_context_file_is_unusable():
    cases = {
        "missing file": _TMP / (tool_maker.CONTEXT_PREFIX + "nope.json"),
        "corrupt JSON": write_ctx(None, raw="{not json"),
        "wrong shape": write_ctx(["a", "list"]),
        "oversize source": write_ctx({"name": "demo", "source": "x" * (tool_maker.MAX_SOURCE_CHARS + 1)}),
        "badly named file": write_ctx({"name": "demo", "source": SOURCE}, name="evil.json"),
    }
    for label, path in cases.items():
        with tool_maker_env(path):
            check(f"{label}: still active (restrictions hold)", tool_maker.active())
            check(f"{label}: still withholds write_file", "write_file" in tool_maker.withheld_tools())
            check(f"{label}: no context is read", tool_maker.load_context() is None)
            out = tools.execute_tool("write_file", {"path": str(_TMP / "x.txt"), "content": "x"})
            check(f"{label}: write_file still refused", isinstance(out, dict) and out.get("ok") is False, out)
            check(f"{label}: nothing was written", not (_TMP / "x.txt").exists())
    with tool_maker_env(""):
        check("an empty variable is not active", tool_maker.active() is False)


# ---- 2. the context -----------------------------------------------------------------------

def test_load_context_and_prompt():
    path = write_ctx({"name": "my_tool", "source": SOURCE})
    with tool_maker_env(path):
        ctx = tool_maker.load_context()
        check("load_context returns name and source", ctx == {"name": "my_tool", "source": SOURCE}, ctx)
        block = tool_maker.prompt_context(ctx)
        check("the block carries the whole current source", SOURCE.strip() in block)
        check("the block names the file", "my_tool.py" in block)
        check("the block tells the model it may only edit", "only EDIT" in block and "Validate" in block and "Save" in block)
        check("the block asks for the complete file in one python fence", "COMPLETE updated file" in block and "```python" in block)
    with tool_maker_env(write_ctx({"name": "Bad Name!", "source": ""})):
        c = tool_maker.load_context()
        check("an invalid name becomes empty, not an error", c == {"name": "", "source": ""}, c)
        check("an empty editor is said to be empty", "empty" in tool_maker.prompt_context(c))
        check("an unsaved tool is labelled as such", "unsaved new tool" in tool_maker.prompt_context({"name": "", "source": "x = 1"}))
    check("an unreadable context says so instead of guessing", "could not be read" in tool_maker.prompt_context(None))


def test_context_goes_in_the_tail_not_the_cached_prefix():
    marker = "TOOL-MAKER-MARKER-" + os.urandom(3).hex()
    base_prefix, base_tail = ai_client._system_prompt_parts("persona", False)
    prefix, tail = ai_client._system_prompt_parts("persona", False, tool_maker_ctx=marker)
    check("the static, cacheable prefix is byte-identical with and without it", prefix == base_prefix)
    check("the block is in the per-turn tail", marker in tail and marker not in prefix)
    check("without it nothing of the block leaks into either part", marker not in base_prefix and marker not in base_tail)


# ---- 3. history ---------------------------------------------------------------------------

def test_collapse_code_for_history():
    big = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(80)) + "\n```"
    small = "```python\nx = 1\n```"
    msgs = [
        {"role": "user", "content": "make it\n" + big},
        {"role": "assistant", "content": "Done.\n" + big + "\nThat's all."},
        {"role": "assistant", "content": "Try:\n" + small},
        {"role": "assistant", "content": "No code here."},
    ]
    snapshot = json.dumps(msgs)
    out = tool_maker.collapse_code_for_history(msgs)
    check("input is not mutated", json.dumps(msgs) == snapshot)
    check("a long fence in an assistant turn is collapsed", "line_5 = 5" not in out[1]["content"] and "80-line" in out[1]["content"], out[1]["content"])
    check("the words around it survive", out[1]["content"].startswith("Done.") and out[1]["content"].endswith("That's all."))
    check("a user turn is left alone", out[0]["content"] == msgs[0]["content"])
    check("a short snippet is conversation, not the file", out[2]["content"] == msgs[2]["content"])
    check("a turn without code is untouched", out[3] == msgs[3])
    cut = [{"role": "assistant", "content": "Here:\n```python\n" + "\n".join(f"v{i} = {i}" for i in range(80))}]
    check("an unterminated fence (a cut-off reply) is collapsed too", "v5 = 5" not in tool_maker.collapse_code_for_history(cut)[0]["content"])
    check("None and junk do not raise", tool_maker.collapse_code_for_history(None) == [] and tool_maker.collapse_code_for_history([None, 3]) == [None, 3])


# ---- 4. the conversation record -----------------------------------------------------------

def run_cli(argv):
    buf = io.StringIO()
    old = sys.argv
    sys.argv = ["jarvis"] + list(argv)
    try:
        with contextlib.redirect_stdout(buf):
            cli.main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_conv_new_origin_flags():
    before = conversations.get_current_id(auto_create=False)
    out = json.loads(run_cli(["conv-new", "--origin", "tool-maker", "--origin-detail", "my_tool"]))
    check("conv-new returns the origin", out.get("origin") == "tool-maker" and out.get("origin_detail") == "my_tool", out)
    rec = conversations.get_conversation(out["id"])
    check("it is stored on the record", rec.get("origin") == "tool-maker" and rec.get("origin_detail") == "my_tool", rec)
    check("a tagged conversation does not become the current one", conversations.get_current_id(auto_create=False) == before)
    out2 = json.loads(run_cli(["conv-new", "--origin", "discord", "--origin-detail", "x"]))
    check("any other origin is dropped (bots set theirs in code)", out2.get("origin") == "" and out2.get("origin_detail") == "", out2)
    out3 = json.loads(run_cli(["conv-new", "My", "title"]))
    check("a plain conv-new still takes a title", out3.get("title") == "My title" and out3.get("origin") == "", out3)


def test_set_origin_and_stamp():
    cid = conversations.new_conversation(make_current=False, origin="tool-maker", origin_detail="new tool")
    check("set_origin refreshes the same origin's detail", conversations.set_origin(cid, "tool-maker", "my_tool") is True)
    check("the detail changed", conversations.get_conversation(cid).get("origin_detail") == "my_tool")
    other = conversations.new_conversation(make_current=False, origin="scheduler", origin_detail="job")
    check("set_origin never rewrites a different origin", conversations.set_origin(other, "tool-maker", "x") is False)
    check("...and leaves it as it was", conversations.get_conversation(other).get("origin") == "scheduler")
    plain = conversations.new_conversation(make_current=False)
    check("an untagged conversation can be claimed", conversations.set_origin(plain, "tool-maker", "t") is True)
    check("a bad id is refused, not raised", conversations.set_origin("../etc", "tool-maker", "x") is False)
    with tool_maker_env(write_ctx({"name": "renamed_tool", "source": SOURCE})):
        check("stamp_conversation follows the tool's name", tool_maker.stamp_conversation(cid) is True)
        check("...and the record shows it", conversations.get_conversation(cid).get("origin_detail") == "renamed_tool")
    check("stamp_conversation is a no-op outside a Tool Maker turn", tool_maker.stamp_conversation(cid) is False)


for fn in [
    test_withheld_names_are_real_tools,
    test_withheld_covers_the_generic_write_and_run_paths,
    test_inactive_by_default,
    test_active_hides_every_withheld_tool,
    test_active_refuses_every_withheld_tool_at_execution,
    test_fail_closed_when_the_context_file_is_unusable,
    test_load_context_and_prompt,
    test_context_goes_in_the_tail_not_the_cached_prefix,
    test_collapse_code_for_history,
    test_conv_new_origin_flags,
    test_set_origin_and_stamp,
]:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- report, keep going
        check(fn.__name__ + " ran without raising", False, repr(exc))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
