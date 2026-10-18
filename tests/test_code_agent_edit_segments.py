"""code_agent changes existing files in segments, never by rewriting them whole
(jarvis-plan.md, item 3 "Edit style" -- the second round; the first round had made
write_file replace whole files, which is what the owner saw in his own tests).

Now: `write_file` only CREATES a new file and refuses a path that already exists;
an existing file is changed with `edit_file` (old_str/new_str); the prompt and both
schemas say so.

Run: python3 tests/test_code_agent_edit_segments.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

# jarvis.tools scans ~/.jarvis/tools at import time: redirect HOME first (AGENTS.md).
_TMP_HOME = tempfile.mkdtemp(prefix="jarvis-codeagent-segments-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
Path(_TMP_HOME, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis.actions import code_agent  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _run_with(files, calls):
    """Run `calls` [(tool, args)] through the REAL executor via a fake loop."""
    d = Path(tempfile.mkdtemp(prefix="ca-seg-"))
    for rel, text in files.items():
        (d / rel).write_text(text, encoding="utf-8")
    orig = code_agent._run_agent_loop

    def fake_loop(task, schemas, executor, round_limit):
        for name, args in calls:
            executor(name, args)
        return "done", None, None

    code_agent._run_agent_loop = fake_loop
    try:
        result = code_agent.tool_code_agent({"task": "t", "root": str(d)})
        contents = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir() if p.is_file()}
    finally:
        code_agent._run_agent_loop = orig
        shutil.rmtree(d, ignore_errors=True)
    return result, contents


def test_write_file_refuses_an_existing_file():
    original = "print('old')\nprint('keep me')\n"
    result, files = _run_with(
        {"main.py": original},
        [("write_file", {"path": "main.py", "content": "print('whole new file')\n"})])
    check("existing file is untouched", files.get("main.py") == original, files)
    fails = [s for s in result["steps"] if s.get("status") == "fail"]
    check("the step failed", len(fails) == 1, result["steps"])
    err = str(fails[0].get("error")) if fails else ""
    check("the error points at edit_file", "already exists" in err and "edit_file" in err, err)


def test_edit_file_changes_only_the_segment():
    original = "a = 1\nb = 2\nc = 3\n"
    result, files = _run_with(
        {"main.py": original},
        [("edit_file", {"path": "main.py", "old_str": "b = 2", "new_str": "b = 20"})])
    check("only the segment changed", files.get("main.py") == "a = 1\nb = 20\nc = 3\n", files)
    check("log says edited", str(result.get("log")).startswith("['edit_file → edited"), result.get("log"))


def test_new_file_is_still_created():
    result, files = _run_with({}, [("write_file", {"path": "a.py", "content": "x = 1\n"})])
    check("file created", files.get("a.py") == "x = 1\n", files)
    check("log says created", result.get("log") == ["write_file → created, 6 bytes written"], result.get("log"))


def test_directory_target_is_refused():
    d = Path(tempfile.mkdtemp(prefix="ca-seg-"))
    (d / "sub").mkdir()
    orig = code_agent._run_agent_loop

    def fake_loop(task, schemas, executor, round_limit):
        executor("write_file", {"path": "sub", "content": "x"})
        return "done", None, None

    code_agent._run_agent_loop = fake_loop
    try:
        result = code_agent.tool_code_agent({"task": "t", "root": str(d)})
        still_dir = (d / "sub").is_dir()
    finally:
        code_agent._run_agent_loop = orig
        shutil.rmtree(d, ignore_errors=True)
    check("directory is left alone", still_dir)
    check("step failed with a clear error",
          any(s.get("status") == "fail" and "not a regular file" in str(s.get("error")) for s in result["steps"]),
          result["steps"])


def test_prompt_and_schemas_say_segments():
    prompt = code_agent._code_agent_system_prompt(5)
    check("prompt tells the model to change files with edit_file", "with edit_file" in prompt, prompt[:600])
    check("prompt forbids rewriting a whole existing file", "NEVER resend or rewrite" in prompt)
    check("prompt limits write_file to new files", "only to create a NEW file" in prompt)
    check("prompt no longer asks for whole-file writes", "COMPLETE updated file" not in prompt)
    desc = code_agent._INTERNAL_WRITE_FILE_SCHEMA["description"]
    check("write_file schema says new files only", "NEW file" in desc and "Refuses a path that already exists" in desc, desc)
    edit = next(s for s in code_agent.TOOL_SCHEMAS if s["name"] == "edit_file")
    check("edit_file schema says it is how existing files change", "never a whole-file rewrite" in edit["description"], edit["description"])


if __name__ == "__main__":
    try:
        test_write_file_refuses_an_existing_file()
        test_edit_file_changes_only_the_segment()
        test_new_file_is_still_created()
        test_directory_target_is_refused()
        test_prompt_and_schemas_say_segments()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
