"""Download Hub, N23 step one (brief 30): the two READ-ONLY probes the owner runs on Windows.

  tests/package_update_checks.py  winget, choco, scoop, npm -g, pip, pipx: records each tool's raw output.
  tests/steam_update_checks.py    Steam's manifests: records them raw, watches them change.

Tested on fakes (a stand-in for the command runner, a fake Steam folder): nothing here runs winget, choco,
scoop, npm, pip or pipx for real, and nothing touches a real Steam.  The probes' own rule is what is pinned:
they never change anything, never parse, never decide what is outdated, never use a shell or the network.

Run: python tests/test_update_probes.py
"""
import ast
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = tempfile.mkdtemp(prefix="jarvis-probes-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok      {name}")
    else:
        failed += 1
        print(f"FAILED  {name}")


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pkg = load("package_update_checks")
stm = load("steam_update_checks")

# --------------------------------------------------------------------------- package probe: the command list
every = [(m, lab, argv) for m, cmds in pkg.COMMANDS.items() for lab, argv in cmds]
check("all six managers are covered", set(pkg.COMMANDS) == {"winget", "choco", "scoop", "npm", "pip", "pipx"})
check("every listed command is read-only by the probe's own rule", all(pkg.is_read_only(a) for _, _, a in every))
check("no word of any command is a verb that changes things", not any(w in pkg.CHANGING for _, _, a in every for w in a))
check("winget uses 'list --upgrade-available', never 'upgrade'", ["winget", "list", "--upgrade-available"] in [a for m, _, a in every])
check("the rule refuses the changing verbs", all(not pkg.is_read_only(c) for c in (
    ["winget", "upgrade", "--all"], ["choco", "upgrade", "all"], ["scoop", "update", "*"], ["npm", "install", "-g", "x"],
    ["python", "-m", "pip", "install", "x"], ["pipx", "upgrade-all"], ["npm", "uninstall", "-g", "x"], [])))
check("npm -g, pip and pipx are in the list (the special cases)", any(m == "npm" and "-g" in a for m, _, a in every)
      and any(m == "pip" and "--outdated" in a for m, _, a in every) and any(m == "pipx" for m, _, a in every))

# --------------------------------------------------------------------------- package probe: a run on fakes
ran = []
outputs = {}


def fake_run(argv, timeout):
    ran.append(list(argv))
    key = " ".join(argv[1:]) if argv[0] == sys.executable else " ".join(argv)
    for needle, value in outputs.items():
        if needle in key:
            return value
    return 0, "", "", 0.01, ""


pkg.RUN = fake_run
installed = {"winget", "npm", "pipx"}
pkg.which = lambda name: ("C:\\tools\\%s.exe" % name) if name in installed else None
RAW = "Name   Id   Version  Available\nGit    Git.Git  2.40  2.45\n  weird   table\twith tabs\n"
outputs["list --upgrade-available"] = (0, RAW, "", 1.2, "")
outputs["outdated -g --json"] = (1, '{"left-pad": {"current": "1.0.0", "latest": "1.3.0"}}', "", 0.5, "")
outputs["outdated -g"] = (1, "Package  Current  Wanted  Latest\nleft-pad  1.0.0  1.0.0  1.3.0\n", "npm WARN something", 0.5, "")
outputs["list --short"] = (0, "black 24.1.0\nhttpie 3.2.2\nbad;name 1\ninstall 1\nruff 0.4\nawscli 1\npoetry 1\nyt-dlp 1\n", "", 0.2, "")
buf = io.StringIO()
out_path = Path(HOME) / "report.txt"
code = pkg.main(["--out", str(out_path)], out=buf)
text = out_path.read_text(encoding="utf-8")
check("the probe exits 0 and writes the report where --out says", code == 0 and "done." in text)
check("a manager that is not installed is said so, not an error", "[choco] version: not installed (no choco on PATH)" in text
      and "[scoop] status: not installed" in text and "[pip]" in text)
check("raw stdout is recorded exactly as printed (nothing parsed, tabs and odd spacing kept)", RAW.rstrip() in text)
check("the exit code is recorded (npm outdated exits 1 when something is out of date)", "exit code: 1" in text)
check("stderr is recorded apart from stdout", "--- stderr ---\nnpm WARN something" in text)
check("the report never says what is outdated: no verdict words of its own", not any(
    w in text.lower() for w in ("is outdated", "needs update", "update available", "up to date", "newer version")))
check("no ran command contains a changing verb", all(pkg.is_read_only(a) for a in ran))
check("python means this interpreter", [sys.executable, "-m", "pip", "list", "--outdated"] in ran)
pip_index = [a for a in ran if a[1:5] == ["-m", "pip", "index", "versions"]]
check("pipx packages get a read-only 'pip index versions' look, at most five", 1 <= len(pip_index) <= 5)
names = [a[-1] for a in pip_index]
check("a package name that is not a plain name, or is a changing verb, is never used", "bad;name" not in names and "install" not in names)
check("the names used are the first plain ones, in order", names[:3] == ["black", "httpie", "ruff"])
check("the file is only written when --out is given", not list(Path(HOME).glob("*.json")))

ran.clear()
buf = io.StringIO()
pkg.main(["--only", "npm"], out=buf)
shown = buf.getvalue()
check("--only runs just that manager", all(a[0].endswith("npm.exe") for a in ran) and "[winget] skipped" in shown)
ran.clear()
pkg.main(["--skip", "winget", "npm", "pipx", "pip"], out=io.StringIO())
check("--skip leaves managers out", ran == [])

outputs.clear()
outputs["--version"] = (None, "", "", 120.0, "TIMED OUT after 120.0 s (killed)")
buf = io.StringIO()
pkg.main(["--only", "winget"], out=buf)
check("a command that times out is recorded as that", "TIMED OUT after 120.0 s (killed)" in buf.getvalue() and "exit code: None" in buf.getvalue())

user = os.environ.get("USERNAME") or os.environ.get("USER") or "someuser"
os.environ["USERNAME"] = user
outputs.clear()
outputs["--version"] = (0, "path C:\\Users\\%s\\AppData\\x" % user, "", 0.1, "")
buf = io.StringIO()
pkg.main(["--only", "winget", "--redact"], out=buf)
check("--redact hides the user name", "<user>" in buf.getvalue() and user not in buf.getvalue().replace("<user>", ""))

big = pkg.clip("x" * 20000, 100)
check("a huge output is cut, and says how much", len(big) < 200 and "cut: 19900 more characters" in big)

# a command that would change something never runs, even if the table were edited by mistake
pkg.COMMANDS["winget"].append(("oops", ["winget", "upgrade", "--all"]))
ran.clear()
buf = io.StringIO()
pkg.main(["--only", "winget"], out=buf)
check("a command with a changing verb is refused at run time", "REFUSED" in buf.getvalue() and not any("upgrade" in a for a in ran))
pkg.COMMANDS["winget"].pop()

# the real runner: no shell, stdin closed, never raises
calls = {}


class Done:
    returncode, stdout, stderr = 0, b"ok", b""


def fake_subprocess_run(argv, **kw):
    calls.update(kw)
    return Done()


pkg.subprocess.run = fake_subprocess_run
res = pkg.run_command(["tool"], 5)
check("the real runner uses no shell, no stdin, a timeout", calls.get("shell") is False and calls.get("timeout") == 5
      and calls.get("stdin") is pkg.subprocess.DEVNULL and res[:3] == (0, "ok", ""))


def boom(argv, **kw):
    raise FileNotFoundError("nope")


pkg.subprocess.run = boom
check("a command that cannot start is a note, never an exception", pkg.run_command(["tool"], 5)[4].startswith("could not run"))

# --------------------------------------------------------------------------- code rules for both probes
for name in ("package_update_checks", "steam_update_checks"):
    tree = ast.parse((ROOT / "tests" / (name + ".py")).read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    check("%s imports no network library" % name, not ({"socket", "urllib", "http", "requests", "httpx", "ssl", "ftplib"} & mods))
    check("%s imports nothing from jarvis (it must run on its own on the owner's PC)" % name, "jarvis" not in mods)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("%s deletes and moves nothing" % name, not ({"unlink", "rmtree", "remove", "rename", "mkdir", "makedirs", "chmod", "startfile"} & attrs))
src = (ROOT / "tests" / "package_update_checks.py").read_text(encoding="utf-8")
check("package probe: the only subprocess call has shell=False", src.count("subprocess.run(") == 1 and "shell=False" in src)

# --------------------------------------------------------------------------- steam probe on a fake Steam
STEAM = Path(HOME) / "Steam"
(STEAM / "steamapps" / "downloading").mkdir(parents=True)
(STEAM / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % str(STEAM).replace("\\", "\\\\"))
ACF = STEAM / "steamapps" / "appmanifest_105600.acf"


def acf(flags, got, owner="76561198012345678"):
    return ('"AppState"\n{\n\t"appid"\t\t"105600"\n\t"name"\t\t"Terraria"\n\t"StateFlags"\t\t"%d"\n\t"BytesDownloaded"\t\t"%d"\n'
            '\t"LastOwner"\t\t"%s"\n\t"InstalledDepots"\n\t{\n\t\t"105601"\n\t\t{\n\t\t\t"manifest"\t\t"9999"\n\t\t}\n\t}\n}\n' % (flags, got, owner))


ACF.write_text(acf(4, 100), encoding="utf-8")


def tree_hash():
    h = hashlib.sha256()
    for p in sorted(STEAM.rglob("*")):
        if p.is_file():
            h.update(str(p).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


before = tree_hash()
buf = io.StringIO()
rep = Path(HOME) / "steam-report.txt"
stm.main(["--root", str(STEAM), "--out", str(rep), "--note", "Terraria: Steam shows an Update button"], out=buf)
report = rep.read_text(encoding="utf-8")
check("steam probe: the manifest is recorded raw", '"StateFlags"\t\t"4"' in report and '"InstalledDepots"' in report)
check("steam probe: the owner's note about Steam's screen is in the report", "Terraria: Steam shows an Update button" in report)
check("steam probe: a Steam account id is masked", "76561198012345678" not in report and "<steam-id>" in report)
check("steam probe: the downloading folder is listed", "downloading: 0 entries" in report)
check("steam probe: it states what it is not doing", "read-only" in report and "no network" in report)
check("steam probe: nothing in the Steam folder changed", tree_hash() == before)
check("steam probe: no verdict words of its own", not any(w in report.lower() for w in ("update available", "needs update", "is outdated")))

ticks = [0.0]
looks = {"n": 0}


def sleep(sec):
    ticks[0] += sec
    looks["n"] += 1
    if looks["n"] == 2:
        ACF.write_text(acf(1026, 5000), encoding="utf-8")
    if looks["n"] == 4:
        (STEAM / "steamapps" / "appmanifest_999.acf").write_text('"AppState"\n{\n\t"appid"\t\t"999"\n\t"StateFlags"\t\t"2"\n}\n')


buf = io.StringIO()
stm.main(["--root", str(STEAM), "--watch", "10", "--interval", "1"], out=buf, sleep=sleep, clock=lambda: ticks[0])
watched = buf.getvalue()
check("steam watch: a changed field is shown as before -> after", "StateFlags: 4 -> 1026" in watched and "BytesDownloaded: 100 -> 5000" in watched)
check("steam watch: a new manifest is shown", "NEW manifest" in watched and "appmanifest_999.acf" in watched)
check("steam watch: it says when it is over", "== watch over ==" in watched)
(STEAM / "steamapps" / "appmanifest_999.acf").unlink()
buf = io.StringIO()
stm.main(["--root", str(Path(HOME) / "nowhere")], out=buf)
check("steam probe: no Steam folder is said, not an error", "Steam folder not found" in buf.getvalue())
check("steam probe: a huge manifest is capped", len(stm.read_raw(ACF)) <= stm.MAX_RAW)

# --------------------------------------------------------------------------- round 10: --admin (still read-only)
pkg.RUN = fake_run
installed.update({"choco", "whoami", "icacls"})
outputs.clear()
outputs["/groups"] = (0, "Mandatory Label\\Medium Mandatory Level\n", "", 0.1, "")
outputs["icacls"] = (0, "C:\\ProgramData\\chocolatey BUILTIN\\Users:(RX)\n", "", 0.1, "")
os.environ["ChocolateyInstall"] = "C:\\ProgramData\\chocolatey"
ran.clear()
buf = io.StringIO()
pkg.main(["--only", "npm"], out=buf)
check("admin: without --admin nothing about administrator rights is recorded or run",
      "[admin]" not in buf.getvalue() and not any("icacls" in a[0] or "whoami" in a[0] for a in ran))
ran.clear()
buf = io.StringIO()
pkg.main(["--only", "npm", "--admin"], out=buf)
admin_text = buf.getvalue()
check("admin: it records whether this window runs as an administrator", "[admin] this window runs as an administrator: " in admin_text)
check("admin: it records where Chocolatey lives", "[admin] %ChocolateyInstall% = C:\\ProgramData\\chocolatey" in admin_text
      and "[admin] choco is at: C:\\tools\\choco.exe" in admin_text)
check("admin: it lists who may write in Chocolatey's folders (icacls with a folder and nothing else)",
      sum(1 for a in ran if "icacls" in a[0]) == 2 and all(len(a) == 2 for a in ran if "icacls" in a[0])
      and "BUILTIN\\Users:(RX)" in admin_text)
check("admin: every command it ran is read-only", all(pkg.is_read_only(a) for a in ran))
check("admin: the report still says nothing of its own about what is outdated", not any(
    w in admin_text.lower() for w in ("is outdated", "needs update", "update available", "up to date", "newer version")))
del os.environ["ChocolateyInstall"]
buf = io.StringIO()
pkg.main(["--only", "npm", "--admin"], out=buf)
check("admin: no ChocolateyInstall is said, not an error", "%ChocolateyInstall% = (not set)" in buf.getvalue())

print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
