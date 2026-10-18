"""code_agent writes whole files (jarvis-plan.md, item 3 "Edit style").

Before: the inner `write_file` refused an existing path ("already exists -- use
edit_file") and the system prompt told the model to patch with small
edit_file old_str/new_str replacements. Now `write_file` creates OR replaces a
file with its complete content, the prompt and schema say so, and the outcome
line says "rewrote" when it replaced something ("created" is unchanged for a
new file).

Run: python3 tests/test_code_agent_write_overwrite.py
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
_TMP_HOME = tempfile.mkdtemp(prefix="jarvis-codeagent-overwrite-test-")
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
    d = Path(tempfile.mkdtemp(prefix="ca-ow-"))
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


def test_overwrites_existing_file():
    new = "print('new')\nprint('whole file')\n"
    result, files = _run_with(
        {"main.py": "print('old')\n"},
        [("write_file", {"path": "main.py", "content": new})])
    check("ok", result.get("ok") is True, result)
    check("existing file now holds the full new content", files.get("main.py") == new, files)
    n = len(new.encode("utf-8"))
    check("log says rewrote", result.get("log") == [f"write_file → rewrote, {n} bytes written"], result.get("log"))
    fails = [s for s in result["steps"] if s.get("status") == "fail"]
    check("no failed step", not fails, fails)


def test_new_file_still_says_created():
    result, files = _run_with({}, [("write_file", {"path": "a.py", "content": "x = 1\n"})])
    check("file created", files.get("a.py") == "x = 1\n", files)
    check("log says created", result.get("log") == ["write_file → created, 6 bytes written"], result.get("log"))


def test_directory_target_is_refused():
    d = Path(tempfile.mkdtemp(prefix="ca-ow-"))
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


def test_prompt_and_schema_say_whole_file():
    prompt = code_agent._code_agent_system_prompt(5)
    check("prompt asks for the complete file via write_file",
          "COMPLETE updated file with write_file" in prompt, prompt[:400])
    check("prompt no longer says write_file is only for new files",
          "genuinely new file" not in prompt)
    desc = code_agent._INTERNAL_WRITE_FILE_SCHEMA["description"]
    check("schema says it replaces an existing file", "replaces it if it already exists" in desc, desc)


if __name__ == "__main__":
    try:
        test_overwrites_existing_file()
        test_new_file_still_says_created()
        test_directory_target_is_refused()
        test_prompt_and_schema_say_whole_file()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
