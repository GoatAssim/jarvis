"""Tests for install.py (the build/install/launch script) and the thin script.bat.

Run: python3 tests/test_install_script.py   (from the repo root)

No network, no pip, no admin, no Windows needed: everything here exercises
the pure parts -- which python gets picked (.venv vs plain), the exe
copy/cleanup rules, the PATH search that must never pick the copy
destination, how the elevation commands are put together, and that
script.bat stays a launcher instead of growing build logic again.

What this CAN'T cover (needs a real Windows box): the UAC prompt, the
de-elevation through Shell.Application, and the new console window.
"""

import base64
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import install  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))


def _touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_python_selection():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        saved = (install.ROOT, install.JARVIS_CLI)
        install.ROOT, install.JARVIS_CLI = root, root / "jarvis-cli"
        try:
            py, how = install.resolve_python()
            check("no venv anywhere -> the running interpreter",
                  py == Path(sys.executable) and "current" in how, (py, how))

            root_venv = _touch(install.venv_python(root / ".venv"))
            py, how = install.resolve_python()
            check("root .venv is found", py == root_venv and ".venv" in how, (py, how))

            cli_venv = _touch(install.venv_python(root / "jarvis-cli" / ".venv"))
            py, how = install.resolve_python()
            check("jarvis-cli/.venv wins over the root .venv", py == cli_venv, (py, how))

            py, how = install.resolve_python(venv_arg=str(root / ".venv"))
            check("--venv with a folder uses that venv's python", py == root_venv, (py, how))
            py, how = install.resolve_python(venv_arg=str(root_venv))
            check("--venv with a python path is used as given", py == root_venv, (py, how))

            py, how = install.resolve_python(no_venv=True)
            check("--no-venv skips a project .venv", py != cli_venv and py != root_venv, (py, how))

            try:
                install.resolve_python(venv_arg=str(root / "nope"))
                check("--venv pointing at nothing is an error", False)
            except install.InstallError:
                check("--venv pointing at nothing is an error", True)
        finally:
            install.ROOT, install.JARVIS_CLI = saved


def test_inspect_python():
    info = install.inspect_python(Path(sys.executable))
    check("inspect reports the venv flag, scripts dir and version",
          set(info) >= {"in_venv", "scripts", "version"} and os.path.isdir(info["scripts"]), info)
    try:
        install.inspect_python(Path("/definitely/not/python"))
        check("an unrunnable python is an InstallError, not a traceback", False)
    except install.InstallError:
        check("an unrunnable python is an InstallError, not a traceback", True)


def test_copy_exe():
    sfx = install.EXE_SUFFIX
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _touch(td / "built" / ("jarvis" + sfx), "new")
        dest_dir = td / "bin"
        _touch(dest_dir / ("jarvis" + sfx), "old")
        _touch(dest_dir / ("friday" + sfx), "previous persona")
        _touch(td / "victim", "keep me")

        install.copy_exe(src, dest_dir, "jarvis", prev_name="friday")
        check("copy replaces the current exe", (dest_dir / ("jarvis" + sfx)).read_text() == "new")
        check("a previous persona's exe is removed", not (dest_dir / ("friday" + sfx)).exists())

        install.copy_exe(src, dest_dir, "jarvis", prev_name="../victim")
        check("a hand-edited state name can't escape the install dir", (td / "victim").exists())

        _touch(dest_dir / ("other" + sfx), "unrelated tool")
        install.copy_exe(src, dest_dir, "jarvis", prev_name="")
        check("other tools in the dir are never touched", (dest_dir / ("other" + sfx)).exists())

        try:
            install.copy_exe(dest_dir / ("jarvis" + sfx), dest_dir, "jarvis")
            check("copying an exe onto itself is refused", False)
        except install.InstallError:
            check("copying an exe onto itself is refused", True)
        check("...and the only copy survives the refusal",
              (dest_dir / ("jarvis" + sfx)).read_text() == "new")


def test_find_exe_never_uses_destination():
    sfx = install.EXE_SUFFIX
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        dest_dir = td / "Utils" / "bin"
        other_dir = td / "elsewhere"
        _touch(dest_dir / ("zzcli" + sfx))
        info = {"in_venv": False, "scripts": str(td / "nowhere")}
        saved_path = os.environ.get("PATH", "")
        # run_tool would call a real python; the PATH fallback is what's under test.
        saved_run = install.run_tool

        class _Miss:
            returncode, stdout, stderr = 1, "", ""

        install.run_tool = lambda *a, **k: _Miss()
        try:
            os.environ["PATH"] = os.pathsep.join([str(dest_dir), saved_path])
            try:
                install.find_exe(Path(sys.executable), info, "zzcli", dest_dir)
                check("a PATH hit inside the install dir is skipped", False)
            except install.InstallError:
                check("a PATH hit inside the install dir is skipped", True)

            _touch(other_dir / ("zzcli" + sfx))
            os.environ["PATH"] = os.pathsep.join([str(dest_dir), str(other_dir), saved_path])
            found = install.find_exe(Path(sys.executable), info, "zzcli", dest_dir)
            check("...and a later PATH hit outside it is used", Path(found).parent == other_dir, found)

            venv_scripts = td / "venv" / "Scripts"
            _touch(venv_scripts / ("zzcli" + sfx))
            found = install.find_exe(
                Path(sys.executable), {"in_venv": True, "scripts": str(venv_scripts)}, "zzcli", dest_dir
            )
            check("a venv's own scripts dir is checked first", Path(found).parent == venv_scripts, found)
        finally:
            install.run_tool = saved_run
            os.environ["PATH"] = saved_path


def test_elevation_commands():
    seen = []
    saved = install._powershell
    install._powershell = lambda script: (seen.append(script) or 0)
    try:
        install.run_elevated(["--admin-copy", "--cli-name", "o'neil", "--src", "C:\\a b\\x.exe"])
        script = seen[-1]
        check("elevation uses RunAs and waits for the exit code",
              "-Verb RunAs" in script and "-Wait" in script and "-PassThru" in script, script)
        check("a cancelled UAC prompt maps to a non-zero exit", "catch { exit 1 }" in script, script)
        check("a single quote in an argument is escaped for PowerShell", "o''neil" in script, script)
        check("an argument with a space is quoted for the child's command line", '"C:\\a b\\x.exe"' in script, script)

        install.deelevate_self(["--serve-only"])
        script = seen[-1]
        check("de-elevation goes through Shell.Application.ShellExecute",
              "Shell.Application" in script and "ShellExecute" in script, script)
        check("...and forwards the original arguments", "--serve-only" in script, script)
    finally:
        install._powershell = saved

    # _powershell itself must hand PowerShell an -EncodedCommand (UTF-16LE base64).
    calls = []
    real_run = install.subprocess.run

    class _R:
        returncode = 0

    install.subprocess.run = lambda cmd, *a, **k: (calls.append(cmd) or _R())
    try:
        install._powershell("Write-Output 'hi'")
        cmd = calls[-1]
        decoded = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
        check("_powershell round-trips its script through -EncodedCommand", decoded == "Write-Output 'hi'", decoded)
    finally:
        install.subprocess.run = real_run


def test_install_dir_resolution():
    check("an empty --install-dir skips the Program Files copy", install.resolve_install_dir("") is None)
    check("an explicit --install-dir is used", install.resolve_install_dir("/x/y") == Path("/x/y"))


def test_launcher_files():
    bat = (REPO / "script.bat").read_text(encoding="utf-8", errors="replace")
    lower = bat.lower()
    check("script.bat hands everything to install.py with all its arguments",
          'install.py" %*' in bat, bat[-400:])
    check("script.bat no longer contains build logic",
          not any(w in lower for w in ("pip install", "npm install", "net session", "copy /y", "powershell")))
    check("script.bat is short (a launcher, not a build script)", len(bat.splitlines()) < 60, len(bat.splitlines()))
    check("script.bat prefers jarvis-cli\\.venv, then the root .venv",
          lower.index("jarvis-cli\\.venv") < lower.index('%root%.venv'))
    check("script.bat honours --no-venv", "--no-venv" in bat)
    code_lines = [ln for ln in bat.splitlines() if ln.strip() and not ln.lstrip().lower().startswith("rem")]
    check("no parenthesized if-blocks (a ')' in the repo path would end them early)",
          not any(ln.rstrip().endswith("(") for ln in code_lines))
    check("the old batch/PowerShell launchers are gone",
          not (REPO / "web" / "start-server.bat").exists() and not (REPO / "launch-web.ps1").exists())


for fn in [
    test_python_selection, test_inspect_python, test_copy_exe, test_find_exe_never_uses_destination,
    test_elevation_commands, test_install_dir_resolution, test_launcher_files,
]:
    fn()

for name, _ in PASS:
    print("ok  " + name)
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  - %s: %s" % (name, detail))
    sys.exit(1)
