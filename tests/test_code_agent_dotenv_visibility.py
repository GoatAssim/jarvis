"""Tests for the F.5 fix (master plan Part F): code_agent's `list_dir`
silently dropped every dotfile (including `.env`), and `search_code`
couldn't find one either, so an inner-loop agent asked to fix a `.env`
had no way to discover it existed and burned its round budget on shell
probes instead (see F.4/F.6).

The fix:
- `list_dir` now names top-level dotfiles under a separate "hidden" key
  (still skipping .git/.venv/node_modules/etc — this is about visibility,
  not walking into them).
- `read_file` on a `.env`/`.env.*` file returns key NAMES with values
  masked (`KEY=•••• (N chars)`) by default — enough to write
  `os.getenv("KEY")` without a raw secret ever reaching a cloud
  provider's transcript (decision D2). `reveal_secrets=True` is honored
  only by the standalone tool call, never by code_agent's own
  autonomous inner loop.
- `search_code` can still find a dotfile if pointed at one with an
  explicit glob (e.g. `.env*`), while the default (no glob, or a glob
  like `*.py`) continues to skip dotfiles entirely.

Run: python3 tests/test_code_agent_dotenv_visibility.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import code_agent  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _project(files):
    d = Path(tempfile.mkdtemp(prefix="jarvis-test-codeagent-"))
    for name, content in files.items():
        path = d / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return d


def test_list_dir_names_env_under_hidden_not_entries():
    d = _project({"main.py": "print(1)\n", ".env": "TOKEN=abc123\n"})
    try:
        result, err = code_agent._list_dir_impl(d)
        check("list_dir succeeds", err is None, err)
        check("main.py is a normal entry", result["entries"] == ["main.py"], result["entries"])
        check(".env is named under hidden", result.get("hidden") == [".env"], result.get("hidden"))
        check(".env is NOT in entries", ".env" not in result["entries"], result["entries"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_list_dir_still_ignores_git_and_venv_dirs():
    d = _project({"main.py": "x\n", ".git/config": "x\n", ".venv/pyvenv.cfg": "x\n"})
    try:
        result, err = code_agent._list_dir_impl(d)
        check(".git/.venv dirs stay out of hidden (not walked, not named)", result.get("hidden") in (None, []), result.get("hidden"))
        check("no .git/.venv content leaked into entries", not any("config" in e or "cfg" in e for e in result["entries"]), result["entries"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_read_file_masks_env_values_by_default():
    d = _project({".env": "TOKEN=abcdefg\nEMPTY=\n# a comment\nBAD LINE\n"})
    try:
        result, err = code_agent._read_file_text(d / ".env")
        check("read succeeds", err is None, err)
        check("values_masked flag set", result.get("values_masked") is True)
        check("real secret value is absent", "abcdefg" not in result["content"], result["content"])
        check("masked placeholder shows key name + length", "TOKEN=•••• (7 chars)" in result["content"], result["content"])
        check("empty value handled without a bogus length", "EMPTY=\n" in result["content"] or result["content"].rstrip().endswith("EMPTY="), result["content"])
        check("non key=value lines pass through unchanged", "# a comment" in result["content"], result["content"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_read_file_reveal_secrets_shows_real_value():
    d = _project({".env": "TOKEN=abcdefg\n"})
    try:
        result, err = code_agent._read_file_text(d / ".env", reveal_secrets=True)
        check("reveal_secrets returns the real value", err is None and "abcdefg" in result["content"], result)
        check("values_masked is not set when revealed", "values_masked" not in result, result)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_tool_read_file_threads_reveal_secrets_argument():
    d = _project({".env": "TOKEN=abcdefg\n"})
    try:
        masked = code_agent.tool_read_file({"path": str(d / ".env")})
        revealed = code_agent.tool_read_file({"path": str(d / ".env"), "reveal_secrets": True})
        check("standalone tool call defaults to masked", "abcdefg" not in masked["content"], masked)
        check("standalone tool call honors reveal_secrets=True", "abcdefg" in revealed["content"], revealed)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_inner_loop_never_reveals_secrets_even_if_model_asks():
    """The autonomous inner-loop executor (tool_code_agent's `executor`)
    must ignore a model-supplied reveal_secrets=True — that value is
    about to go straight into a cloud provider's transcript."""
    d = _project({".env": "TOKEN=abcdefg\n"})
    try:
        result = code_agent.tool_code_agent.__globals__  # sanity: module loaded
        assert result
        # Exercise the real executor closure the same way tool_code_agent
        # builds it, without needing a live AI provider: reconstruct the
        # read_file branch's call directly via _read_file_text semantics,
        # confirming the executor source never forwards reveal_secrets.
        import inspect
        src = inspect.getsource(code_agent.tool_code_agent)
        branch = src.split('if name == "read_file":')[1].split("elif name ==")[0]
        code_lines = [
            line for line in branch.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        check(
            "inner executor's read_file branch does not pass reveal_secrets",
            not any("reveal_secrets" in line for line in code_lines),
            code_lines,
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_search_code_default_skips_dotfiles():
    d = _project({".env": "TOKEN=abcdefg\n", "main.py": "TOKEN = 'x'\n"})
    try:
        result, err = code_agent._search_code_impl(d, "TOKEN")
        check("search_code succeeds", err is None, err)
        check("default search finds main.py but not .env", [m["path"] for m in result["matches"]] == ["main.py"], result["matches"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_search_code_explicit_dotfile_glob_finds_env():
    d = _project({".env": "TOKEN=abcdefg\n", "main.py": "TOKEN = 'x'\n"})
    try:
        result, err = code_agent._search_code_impl(d, "TOKEN", glob_pattern=".env")
        check("search_code succeeds", err is None, err)
        check("explicit .env glob finds it", [m["path"] for m in result["matches"]] == [".env"], result["matches"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


for fn in [
    test_list_dir_names_env_under_hidden_not_entries,
    test_list_dir_still_ignores_git_and_venv_dirs,
    test_read_file_masks_env_values_by_default,
    test_read_file_reveal_secrets_shows_real_value,
    test_tool_read_file_threads_reveal_secrets_argument,
    test_inner_loop_never_reveals_secrets_even_if_model_asks,
    test_search_code_default_skips_dotfiles,
    test_search_code_explicit_dotfile_glob_finds_env,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
