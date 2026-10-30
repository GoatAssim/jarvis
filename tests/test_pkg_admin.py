"""Download Hub, round 10 (DH-C): the choco "needs an administrator window" error.

Covers, against FAKES only (nothing here starts a real winget / choco / scoop / pip / pipx / npm, a real PowerShell
or a real UAC prompt; the fixtures are what the code expects, reconstructed from memory of how those programs
print, NOT recordings):

  Part 1  update_parsers.admin_verdict / strip_choco_banner / failure_line / parse_elevated_marker
          (and that the OLD single pattern really blamed administrator rights for unrelated failures);
          apply_pkg_update: the up-front check, honest messages, every message fits the 200 characters the hub
          keeps and a command is never cut.
  Part 2  the opt-in elevated run: off by default, no argument can switch it on, guest refused, only choco,
          the exact scripts, the exit code and the log read back, and every "unknown" said as unknown.

Run: python tests/test_pkg_admin.py
"""
import base64
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="jarvis-pkg-admin-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jarvis-cli"))

from jarvis import checklist_schema, tool_settings, update_parsers as up  # noqa: E402
from jarvis.actions import pkg_updates as pu  # noqa: E402

# --------------------------------------------------------------------------- fixtures (reconstructed, see the header)

CHOCO_EXE = "C:\\ProgramData\\chocolatey\\bin\\choco.EXE"
ADMIN_ROOTS = ("C:\\ProgramData", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\Windows")

# What Chocolatey prints at the start of every mutating run from a window that is not elevated.
BANNER = (
    "Chocolatey v2.2.2\n"
    "Upgrading the following packages:\n"
    "bitwarden\n"
    "By upgrading, you accept licenses for the packages.\n"
    "Chocolatey detected you are not running from an elevated command shell\n"
    " (cmd/powershell).\n"
    "\n"
    " You may experience errors - many functions/packages\n"
    " require admin rights. Only advanced users should run choco w/o an\n"
    " elevated shell. When you open the command shell, you should ensure\n"
    " that you do so with \"Run as Administrator\" selected. If you are\n"
    " attempting to use Chocolatey in a non-administrator setting, you\n"
    " must select a different location other than the default install\n"
    " location. See\n"
    " https://docs.chocolatey.org/en-us/choco/setup#non-administrative-install\n"
    " for details.\n"
    "\n"
    "Do you want to continue?([Y]es/[N]o): Y\n"
)
CHOCO_NETWORK = BANNER + (
    "bitwarden not upgraded. An error occurred during installation:\n"
    " The remote name could not be resolved: 'community.chocolatey.org'\n"
    "Chocolatey upgraded 0/1 packages. 1 packages failed.\n"
    " See the log for details (C:\\ProgramData\\chocolatey\\logs\\chocolatey.log).\n"
)
CHOCO_DENIED = BANNER + (
    "bitwarden not upgraded. An error occurred during installation:\n"
    " Access to the path 'C:\\ProgramData\\chocolatey\\lib\\bitwarden' is denied.\n"
    "Chocolatey upgraded 0/1 packages. 1 packages failed.\n"
)
CHOCO_LOCK = BANNER + (
    "Unable to obtain lock file access on 'C:\\ProgramData\\chocolatey\\lib\\xyz' for operations on "
    "'C:\\ProgramData\\chocolatey\\lib\\bitwarden'. This may mean that a different user or administrator is holding "
    "this lock.\n"
)
PIP_WINERROR5 = ("ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: "
                 "'c:\\\\python312\\\\scripts\\\\pip.exe'\nConsider using the `--user` option or check the permissions.\n")
NPM_EACCES = ("npm ERR! code EACCES\nnpm ERR! syscall mkdir\nnpm ERR! path /usr/local/lib/node_modules/x\n"
              "npm ERR! Error: EACCES: permission denied, mkdir '/usr/local/lib/node_modules/x'\n")
SCOOP_REFUSES = ("Running Scoop as administrator is disabled by default, and is not recommended for security reasons.\n"
                 "If you know what you are doing, you can install anyway.\n")
SCOOP_GLOBAL = "You need admin rights to install global apps.\n"
WINGET_740 = "Installer failed with exit code: 740\nThe requested operation requires elevation.\n"
WINGET_REFUSES = "This installer cannot be run from an administrator context.\n"
WINGET_CANCELLED = "The operation was canceled by the user.\n"

# The pattern this round replaced (copied from the code it replaced), kept to show what it got wrong.
OLD_ADMIN = re.compile(
    r"administrator|elevated|elevation|run as admin|requires? admin|access (?:to the path .{0,200}?)?is denied"
    r"|permission denied|0x80070005|UAC", re.I)


# --------------------------------------------------------------------------- the fake runner and the set-up

def decode_ps(b64):
    return base64.b64decode(b64).decode("utf-16-le")


class Fake:
    """A stand-in for pkg_tools._run.  `answers` = [(needle words, (code, stdout, stderr))] like the other H3 tests.
    A call whose argv[0] is 'powershell-test' is the elevation attempt: it is decoded (`seen`), the fake plays the
    elevated window by writing `log` into the log path the script names, and answers `ps`."""

    def __init__(self, answers=None, ps=(0, "JARVIS_ELEVATED_EXIT=0\r\n", ""), log="", write_log=True):
        self.calls = []
        self.answers = list(answers or [])
        self.ps, self.log, self.write_log = ps, log, write_log
        self.seen = None

    def __call__(self, argv, timeout=30, extra_env=None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "env": dict(extra_env or {})})
        if argv and argv[0] == "powershell-test":
            outer = decode_ps(argv[4])
            inner_b64 = re.search(r"'-EncodedCommand','([A-Za-z0-9+/=]+)'", outer).group(1)
            inner = decode_ps(inner_b64)
            log_path = re.search(r"\$log = '((?:[^']|'')*)'", inner).group(1).replace("''", "'")
            self.seen = {"outer": outer, "inner": inner, "log": log_path}
            if self.write_log:
                Path(log_path).write_text(self.log, encoding="utf-8")
            return self.ps
        for needle, result in self.answers:
            if all(word in argv for word in needle):
                return result
        return (0, "", "")

    def ran_managers(self):
        return [c for c in self.calls if c["argv"][0] != "powershell-test"]


def setup(fake, elevated=False, allowed=False, choco=CHOCO_EXE, windows=True, powershell="powershell-test"):
    pu.RUNNER = fake
    pu.WINDOWS = lambda: windows
    pu.ELEVATED = lambda: elevated
    pu.ADMIN_ROOTS = lambda: list(ADMIN_ROOTS)
    pu.UAC_ALLOWED = lambda: allowed
    pu.FIND_POWERSHELL = lambda: powershell
    pu.LOG_DIR = lambda: Path(HOME) / "logs"
    pu.FIND_CMD = lambda manager: [choco] if manager == "choco" else ["%s-bin" % manager]
    pu.FIND_PYTHON = lambda: (["py-test"], "")
    pu.JARVIS_REQUIRED = ("requests", "psutil")
    pu.CLOCK = time.monotonic
    pu._PY_CACHE.clear()
    pu._REQ_CACHE[:] = []
    return fake


def reset():
    pu.RUNNER = pu.FIND_CMD = pu.FIND_PYTHON = pu.JARVIS_REQUIRED = None
    pu.ELEVATED = pu.WINDOWS = pu.ADMIN_ROOTS = pu.UAC_ALLOWED = pu.FIND_POWERSHELL = pu.LOG_DIR = None
    pu.CLOCK = time.monotonic
    pu._PY_CACHE.clear()
    pu._REQ_CACHE[:] = []
    os.environ.pop("JARVIS_CHANNEL_SENDER", None)
    try:
        (Path(HOME) / ".jarvis" / "tool_settings.json").unlink()
    except OSError:
        pass
    logs = Path(HOME) / "logs"
    if logs.is_dir():
        for f in logs.glob("*"):
            f.unlink()


def as_guest():
    os.environ["JARVIS_CHANNEL_SENDER"] = json.dumps({"platform": "discord", "user_id": "77", "is_owner": False})


def apply(manager, package, **more):
    args = {"manager": manager, "id": package}
    args.update(more)
    return pu.tool_apply_pkg_update(args)


# =========================================================================== Part 1: the parsers

def test_old_pattern_blamed_admin_for_unrelated_failures():
    """The regression this round fixes: each of these matched the OLD pattern, and none says 'needs administrator'."""
    for name, text in (("choco failing on the network", CHOCO_NETWORK), ("scoop refusing administrator", SCOOP_REFUSES),
                       ("winget: cannot run as administrator", WINGET_REFUSES),
                       ("a URL with 'uac' in it", "Failed to download https://example.com/uac/setup.exe: 404"),
                       ("a package that does not need it", "This package does not require administrator rights.")):
        assert OLD_ADMIN.search(text), "the old pattern matched this one (%s)" % name
        assert up.admin_verdict(text)[0] != up.ADMIN_NEEDED, name
        assert up.looks_like_needs_admin(text) is False, name


def test_choco_banner_is_cut_and_is_not_evidence():
    rest, seen = up.strip_choco_banner(CHOCO_NETWORK)
    assert seen is True
    assert "require admin rights" not in rest and "Run as Administrator" not in rest and "elevated" not in rest
    assert "Upgrading the following packages" in rest and "remote name could not be resolved" in rest
    assert up.strip_choco_banner("nothing special\n") == ("nothing special\n", False)
    verdict, _evidence, banner = up.admin_verdict(CHOCO_NETWORK)
    assert verdict == "" and banner is True, "a failure that has nothing to do with rights makes no claim about them"
    # the banner alone, an upgrade that worked, says nothing either
    assert up.admin_verdict(BANNER + "bitwarden v2024.10.1 upgraded.\n")[0] == ""
    # a banner worded differently: only the lines that still read like the banner go, the real error is kept
    short = "Chocolatey detected you are not running from an elevated command shell\nERROR: package not found\n"
    rest, seen = up.strip_choco_banner(short)
    assert seen and "package not found" in rest and "elevated" not in rest, rest
    # twice in one output
    assert up.strip_choco_banner(BANNER + BANNER + "end\n")[0].count("elevated") == 0


def test_needs_admin_is_claimed_only_for_words_that_say_so():
    needs = {
        "choco banner + denied in its own folder": CHOCO_DENIED,
        "choco banner + lock file": CHOCO_LOCK,
        "winget 740": WINGET_740,
        "windows 740 text alone": "[WinError 740] The requested operation requires elevation",
        "msi 1925": "Installer failed with exit code: 1925",
        "scoop global": SCOOP_GLOBAL,
        "plain words": "This package requires administrator privileges to install.",
        "plain words 2": "Administrator rights are required.",
        "must be run": "This command must be run as administrator.",
    }
    for name, text in needs.items():
        assert up.admin_verdict(text)[0] == up.ADMIN_NEEDED, (name, up.admin_verdict(text))
        assert up.looks_like_needs_admin(text) is True, name
    # a negation right in front turns it round
    assert up.admin_verdict("Note: this does not require administrator rights.")[0] == ""
    # "Cannot continue:" is not a negation of what follows
    assert up.admin_verdict("Cannot continue: requires administrator privileges")[0] == up.ADMIN_NEEDED


def test_access_denied_alone_is_not_an_admin_claim():
    for name, text in (("pip WinError 5", PIP_WINERROR5), ("npm EACCES", NPM_EACCES),
                       ("denied outside choco's folder, no banner",
                        "Access to the path 'C:\\Users\\me\\AppData\\Local\\Temp\\x.exe' is denied."),
                       ("choco denied but no banner (Jarvis was elevated)",
                        "Access to the path 'C:\\ProgramData\\chocolatey\\lib\\x' is denied."),
                       ("a denial after 'could not'", "could not replace the file: Access is denied")):
        verdict = up.admin_verdict(text)[0]
        assert verdict == up.ADMIN_DENIED, (name, verdict)
        assert up.looks_like_needs_admin(text) is False, name
    assert up.admin_verdict("Installer failed with exit code: 0x80070005")[0] == up.ADMIN_DENIED


def test_refusing_administrator_is_the_opposite_verdict():
    for text in (SCOOP_REFUSES, WINGET_REFUSES):
        assert up.admin_verdict(text)[0] == up.ADMIN_REFUSED, text
    assert up.admin_verdict(WINGET_REFUSES + "requires administrator privileges")[0] == "", "both said: no claim"


def test_uac_cancelled_is_its_own_verdict():
    assert up.admin_verdict(WINGET_CANCELLED)[0] == up.ADMIN_UAC_CANCELLED
    assert up.admin_verdict("Installer failed with exit code: 1223")[0] == up.ADMIN_UAC_CANCELLED
    assert up.admin_verdict("")[0] == "" and up.admin_verdict(None)[0] == ""


def test_failure_line_skips_the_banner_and_finds_the_error():
    assert up.first_line(CHOCO_NETWORK) == "Chocolatey v2.2.2", "what the old message would have shown"
    line = up.failure_line(CHOCO_NETWORK, "")
    assert "error occurred" in line and "v2.2.2" not in line, line
    assert up.failure_line("", "scoop: something broke\nmore") == "scoop: something broke", "no error word: the first line"
    assert up.failure_line("fine\n0 errors\n", "") == "fine", "'0 errors' is not an error line"
    assert up.failure_line("", "") == ""
    assert len(up.failure_line("x" * 500 + " error", "", 90)) <= 90


def test_elevated_marker_parser():
    assert up.parse_elevated_marker("JARVIS_ELEVATED_EXIT=0\r\n") == ("exit", 0)
    assert up.parse_elevated_marker("noise\nJARVIS_ELEVATED_EXIT=-1978335189\n") == ("exit", -1978335189)
    assert up.parse_elevated_marker("JARVIS_ELEVATED_EXIT=3010") == ("exit", 3010)
    assert up.parse_elevated_marker("JARVIS_ELEVATED_CANCELLED") == ("cancelled", None)
    assert up.parse_elevated_marker("JARVIS_ELEVATED_NOSTART\n") == ("nostart", None)
    for junk in ("", None, "JARVIS_ELEVATED_EXIT=abc", "exit 0", "JARVIS_ELEVATED_EXIT="):
        assert up.parse_elevated_marker(junk) == ("unknown", None), junk


# =========================================================================== Part 1: apply_pkg_update

def test_up_front_choco_not_elevated_runs_nothing_and_gives_the_command():
    fake = setup(Fake())
    out = apply("choco", "bitwarden")
    assert fake.calls == [], "choco is never started"
    assert out["ok"] is False and out["state"] == "failed", out
    assert out["message"].startswith("Needs an administrator window: in an administrator terminal run: "
                                     "choco upgrade bitwarden -y"), out["message"]
    assert "Jarvis isn't an administrator" in out["message"] and "Or allow a UAC prompt in Tool settings" in out["message"]
    assert out["command"] == "choco upgrade bitwarden -y" and out["error"] == out["message"]
    assert len(out["message"]) <= 200
    # the dry run says the same, so the Updates panel stops there and shows no confirm dialog
    dry = apply("choco", "bitwarden", dry_run=True)
    assert fake.calls == [] and dry["ok"] is False and dry["message"] == out["message"], dry
    # through the hub's own reading of an apply answer, and the panel's 160-character cut of "No plan: ..."
    from jarvis.hub import updates as hub_updates
    parsed = hub_updates.parse_apply_result(dry, dry_run=True)
    assert parsed["ok"] is False and "choco upgrade bitwarden -y" in parsed["message"], parsed
    assert "choco upgrade bitwarden -y" in ("No plan: " + parsed["message"])[:160]


def test_up_front_never_blocks_what_it_does_not_know():
    # Jarvis IS an administrator: it runs
    fake = setup(Fake(), elevated=True)
    out = apply("choco", "bitwarden")
    assert out["ok"] is True and out["state"] == "done", out
    assert [c["argv"] for c in fake.calls] == [[CHOCO_EXE, "upgrade", "bitwarden", "-y"]]
    # Jarvis cannot tell (None): it runs
    setup(Fake())
    pu.ELEVATED = lambda: None
    assert apply("choco", "bitwarden")["state"] == "done"
    pu.ELEVATED = lambda: (_ for _ in ()).throw(OSError("boom"))
    assert apply("choco", "bitwarden")["state"] == "done", "a failing probe is 'unknown', never a block"
    # Chocolatey set up for a normal user (not under ProgramData / Program Files): it runs
    fake = setup(Fake(), choco="C:\\Users\\me\\chocolatey\\bin\\choco.exe")
    assert apply("choco", "bitwarden")["state"] == "done" and len(fake.calls) == 1
    # a program name with no path: unknown, runs
    fake = setup(Fake(), choco="choco")
    assert apply("choco", "bitwarden")["state"] == "done"
    # not Windows
    fake = setup(Fake(), windows=False)
    assert apply("choco", "bitwarden")["state"] == "done"
    # the up-front check is Chocolatey's only: winget even under Program Files runs (it asks for rights itself)
    fake = setup(Fake())
    pu.FIND_CMD = lambda manager: ["C:\\Program Files\\WinGet\\winget.exe"]
    out = apply("winget", "Git.Git")
    assert out["state"] == "done" and len(fake.calls) == 1, out


def test_admin_folder_rules():
    setup(Fake())
    assert pu._in_admin_folder([CHOCO_EXE]) is True
    assert pu._in_admin_folder(["c:/programdata/chocolatey/bin/choco.exe"]) is True, "case and slashes do not matter"
    assert pu._in_admin_folder(["C:\\ProgramDataX\\foo\\choco.exe"]) is False, "a longer folder name is not inside it"
    assert pu._in_admin_folder(["\\ProgramData\\x\\choco.exe"]) is False, "no drive: not known"
    assert pu._in_admin_folder(["choco-bin"]) is False and pu._in_admin_folder([]) is False
    assert pu._in_admin_folder(["D:\\tools\\chocolatey\\bin\\choco.exe"]) is False
    pu.ELEVATED = lambda: None
    assert pu._is_elevated() is None
    pu.ELEVATED = lambda: 1
    assert pu._is_elevated() is True


def test_post_run_messages_are_honest():
    # winget says in words that it needs elevation: the command comes back, and it DID run first
    fake = setup(Fake([(("upgrade", "--id"), (1, WINGET_740, ""))]), elevated=None)
    pu.ELEVATED = lambda: None
    out = apply("winget", "Git.Git")
    assert len(fake.calls) == 1 and out["ok"] is False
    assert out["message"].startswith("Needs an administrator window: in an administrator terminal run: winget upgrade --id Git.Git")
    assert "--accept-package-agreements" in out["message"] and "Tool settings" not in out["message"], "winget has no UAC option"
    assert out["command"].startswith("winget upgrade --id Git.Git") and len(out["message"]) <= 200
    # choco failing for a reason that is not rights: the banner is not blamed
    fake = setup(Fake([(("upgrade", "bitwarden"), (1, CHOCO_NETWORK, ""))]), choco="C:\\Users\\me\\chocolatey\\bin\\choco.exe")
    out = apply("choco", "bitwarden")
    assert out["state"] == "failed" and "administrator" not in out["message"].lower(), out["message"]
    assert "error occurred" in out["message"] and "exit code 1" in out["message"], out["message"]
    # access denied alone: says it cannot tell
    fake = setup(Fake([(("--upgrade",), (1, "", PIP_WINERROR5))]))
    out = apply("pip", "rich")
    assert "refused access" in out["message"] and "can't tell which" in out["message"], out["message"]
    assert not out["message"].startswith("Needs an administrator window"), out["message"]
    # scoop refuses administrator
    fake = setup(Fake([(("update", "git"), (1, SCOOP_REFUSES, ""))]))
    out = apply("scoop", "git")
    assert "won't run as administrator" in out["message"] and not out["message"].startswith("Needs"), out["message"]
    setup(Fake([(("update", "git"), (1, SCOOP_REFUSES, ""))]), elevated=True)
    out = apply("scoop", "git")
    assert "refuses to run from an administrator window, and Jarvis is running as one" in out["message"], out["message"]
    # it asks for rights although Jarvis is already an administrator: not blamed on missing rights, no command
    setup(Fake([(("upgrade", "--id"), (1, WINGET_740, ""))]), elevated=True)
    out = apply("winget", "Git.Git")
    assert "already running as administrator" in out["message"] and "command" not in out, out
    # a UAC prompt closed by someone
    setup(Fake([(("upgrade", "--id"), (1, WINGET_CANCELLED, ""))]))
    out = apply("winget", "Git.Git")
    assert "UAC" in out["message"] and "closed or declined" in out["message"], out["message"]
    # the existing contract: banner + denied in choco's folder keeps the lead of the old message
    setup(Fake([(("upgrade", "git"), (1, CHOCO_DENIED, ""))]), choco="C:\\Users\\me\\chocolatey\\bin\\choco.exe")
    out = apply("choco", "git")
    assert out["message"].startswith("Needs an administrator window"), out


def test_choco_restart_codes_are_success():
    for code in (3010, 1641):
        setup(Fake([(("upgrade", "git"), (code, "Chocolatey upgraded 1/1 packages. A restart is required.", ""))]), elevated=True)
        out = apply("choco", "git")
        assert out["ok"] is True and out["state"] == "done" and out["needs_restart"] is True, (code, out)
    setup(Fake([(("upgrade", "--id"), (3010, "", ""))]))
    out = apply("winget", "Git.Git")
    assert out["ok"] is False, "only Chocolatey's codes mean 'worked, restart needed'"
    setup(Fake([(("upgrade", "git"), (1, "Chocolatey upgraded 0/1 packages.", ""))]), elevated=True)
    assert "needs_restart" not in apply("choco", "git")


def test_a_command_in_a_message_is_never_cut_and_messages_fit():
    for n in range(1, 121):
        package = "a" * n
        setup(Fake())
        out = apply("choco", package)
        command = "choco upgrade %s -y" % package
        assert len(out["message"]) <= 200 and len(out["error"]) <= 200, (n, len(out["message"]))
        assert out["command"] == command, "the command field is whole"
        if "too long to show" in out["message"]:
            assert command not in out["message"] and n >= 100, (n, out["message"])   # said so, instead of cutting it
        else:
            assert command in out["message"], "a command that is shown is shown whole (n=%d)" % n
    # winget's long command, after a run that said it needs elevation
    setup(Fake([(("upgrade", "--id"), (1, WINGET_740, ""))]))
    pu.ELEVATED = lambda: None
    out = apply("winget", "V" * 120)
    assert len(out["message"]) <= 200
    assert ("too long to show" in out["message"]) or (out["command"] in out["message"]), out["message"]


# =========================================================================== Part 2: the opt-in elevated run

def test_setting_is_off_by_default_and_declared_properly():
    entries, errors = tool_settings.validate_settings(pu.TOOL_SETTINGS, "pkg_updates.py")
    assert errors == [] and len(entries) == 1, errors
    entry = entries[0]
    assert entry["key"] == "uac_upgrades" and entry["type"] == "boolean" and entry["default"] is False
    assert entry["help"] == pu.TOOL_SETTINGS[0]["help"], "the help text fits the 300 characters the validator keeps"
    assert "UAC" in entry["label"] and "Chocolatey" in entry["label"]
    reset()
    assert pu._uac_allowed() is False, "nothing stored: off"
    path = Path(HOME) / ".jarvis" / "tool_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pkg_updates": {"uac_upgrades": True}}), encoding="utf-8")
    assert pu._uac_allowed() is True, "the owner's own value"
    path.write_text(json.dumps({"pkg_updates": {"uac_upgrades": {"nope": 1}}}), encoding="utf-8")
    assert pu._uac_allowed() is False, "an unreadable value is off"
    path.write_text("{ not json", encoding="utf-8")
    assert pu._uac_allowed() is False


def test_dry_run_with_the_setting_on_says_a_uac_window_will_open():
    fake = setup(Fake(), allowed=True)
    out = apply("choco", "bitwarden", dry_run=True)
    assert fake.calls == [], "a dry run runs nothing"
    assert out["ok"] is True and out["state"] == "unchanged", out
    assert out["plan"] == ("A Windows UAC window will open and ask you to allow an administrator window. "
                           "If you allow it, this runs there: choco upgrade bitwarden -y"), out["plan"]
    assert "UAC" in out["message"] and out["command"] == "choco upgrade bitwarden -y"
    from jarvis.hub import updates as hub_updates
    parsed = hub_updates.parse_apply_result(out, dry_run=True)
    assert parsed["ok"] is True and parsed["state"] == "plan" and parsed["plan"].startswith("A Windows UAC window will open")


def test_elevated_run_succeeds_and_reads_exit_code_and_log():
    fake = setup(Fake(log="bitwarden v2024.10.1 upgraded\nChocolatey upgraded 1/1 packages.\n"), allowed=True)
    out = apply("choco", "bitwarden")
    assert out["ok"] is True and out["state"] == "done" and out["elevated"] is True, out
    assert out["message"] == "Upgraded bitwarden with choco in an administrator window."
    assert out["command"] == "choco upgrade bitwarden -y"
    assert len(fake.calls) == 1 and fake.ran_managers() == [], "choco itself was NOT started by Jarvis, only the UAC attempt"
    call = fake.calls[0]
    assert call["argv"][:4] == ["powershell-test", "-NoProfile", "-NonInteractive", "-EncodedCommand"] and len(call["argv"]) == 5
    assert call["timeout"] == pu.UAC_TIMEOUT_S
    assert all(isinstance(a, str) for a in call["argv"]), "an argv list, never one string for a shell"
    outer, inner, log = fake.seen["outer"], fake.seen["inner"], fake.seen["log"]
    assert "Start-Process" in outer and "-Verb RunAs" in outer and "-Wait" in outer and "-PassThru" in outer
    assert "JARVIS_ELEVATED_EXIT=" in outer and "JARVIS_ELEVATED_CANCELLED" in outer and "1223" in outer
    assert "@('upgrade','bitwarden','-y')" in inner and "& 'choco' @a" in inner
    assert "Out-File -LiteralPath $log -Append" in inner and "exit $LASTEXITCODE" in inner
    assert Path(log).parent == Path(HOME) / "logs" and out["log"] == log
    assert Path(log).is_file(), "the log is in a Jarvis-owned folder"


def test_elevated_run_every_outcome_is_said_as_it_is():
    cases = [
        ("exit 1 with a log", Fake(ps=(0, "JARVIS_ELEVATED_EXIT=1\r\n", ""), log="ERROR: something broke\n"),
         lambda o: o["ok"] is False and "exit code 1" in o["message"] and "something broke" in o["message"]),
        ("exit 0 and an empty log", Fake(log=""),
         lambda o: o["ok"] is True and o["state"] == "done" and "details are unknown" in o["message"]),
        ("exit 0 and 'already newest'", Fake(log="bitwarden v1.0 is the latest version available based on your source(s).\n"),
         lambda o: o["ok"] is True and o["state"] == "unchanged"),
        ("restart needed", Fake(ps=(0, "JARVIS_ELEVATED_EXIT=3010\r\n", ""), log="done\n"),
         lambda o: o["ok"] is True and o["state"] == "done" and o["needs_restart"] is True),
        ("exit 5 but 'already newest'", Fake(ps=(0, "JARVIS_ELEVATED_EXIT=5\r\n", ""), log="no available upgrade found\n"),
         lambda o: o["ok"] is True and o["state"] == "unchanged"),
        ("UAC closed", Fake(ps=(0, "JARVIS_ELEVATED_CANCELLED\r\n", ""), write_log=False),
         lambda o: o["ok"] is False and "closed or declined" in o["message"] and "nothing was upgraded" in o["message"]),
        ("Windows would not open it", Fake(ps=(0, "JARVIS_ELEVATED_NOSTART\r\n", ""), write_log=False),
         lambda o: o["ok"] is False and "would not open" in o["message"]),
        ("no marker: the result is unknown", Fake(ps=(0, "", ""), write_log=False),
         lambda o: o["ok"] is False and "unknown" in o["message"] and "Check choco by hand" in o["message"]),
        ("timed out", Fake(ps=(-1, "", "Timed out after 540s"), write_log=False),
         lambda o: o["ok"] is False and "didn't finish" in o["message"] and "may still be running" in o["message"]),
        ("powershell could not start", Fake(ps=(None, "", "Command not found: powershell"), write_log=False),
         lambda o: o["ok"] is False and "could not start" in o["message"] and "no UAC prompt was opened" in o["message"]),
    ]
    for name, fake, ok in cases:
        setup(fake, allowed=True)
        out = apply("choco", "bitwarden")
        assert ok(out), (name, out)
        assert len(out["message"]) <= 200, name
        if "state" in out and out["state"] == "failed":
            assert out["error"], name


def test_the_setting_alone_decides_no_argument_can_ask_for_it():
    fake = setup(Fake(), allowed=False)
    for extra in ({"elevate": True}, {"elevated": True}, {"uac": True}, {"admin": True}, {"runas": True},
                  {"run_as_admin": "yes"}, {"origin": "manual"}, {"allowed": True}, {"uac_upgrades": True},
                  {"verb": "RunAs"}):
        out = apply("choco", "bitwarden", **extra)
        assert out["ok"] is False and out["message"].startswith("Needs an administrator window"), (extra, out)
        out = apply("choco", "bitwarden", dry_run=True, **extra)
        assert out["ok"] is False, (extra, out)
    assert fake.calls == [], "no attempt of any kind"
    schema = [s for s in pu.TOOL_SCHEMAS if s["name"] == "apply_pkg_update"][0]
    assert set(schema["parameters"]["properties"]) == {"id", "manager", "dry_run"}, "the model is offered no such argument"
    assert pu.TOOL_CONFIRM_REQUIRED == {"apply_pkg_update"}


def test_auto_origin_can_only_switch_the_prompt_off():
    fake = setup(Fake(), allowed=True)
    out = apply("choco", "bitwarden", origin="auto")
    assert fake.calls == [] and out["ok"] is False and out["message"].startswith("Needs an administrator window"), out
    assert "Or allow a UAC prompt" not in out["message"], "it is already allowed: the hint would be wrong"
    out = apply("choco", "bitwarden", origin="AUTO", dry_run=True)
    assert out["ok"] is False and fake.calls == []


def test_setting_on_but_no_prompt_is_possible_falls_back_to_the_instruction():
    fake = setup(Fake(), allowed=True, powershell=None)
    out = apply("choco", "bitwarden")
    assert fake.calls == [] and out["ok"] is False and out["command"] == "choco upgrade bitwarden -y", out
    # Jarvis already an administrator: no prompt, an ordinary run
    fake = setup(Fake(), allowed=True, elevated=True)
    out = apply("choco", "bitwarden")
    assert out["state"] == "done" and fake.ran_managers() and not any(c["argv"][0] == "powershell-test" for c in fake.calls)
    # a log that cannot be made: no window is opened whose result could not be read
    blocker = Path(HOME) / "a-file"
    blocker.write_text("x", encoding="utf-8")
    fake = setup(Fake(), allowed=True)
    pu.LOG_DIR = lambda: blocker / "sub"
    out = apply("choco", "bitwarden")
    assert fake.calls == [] and out["ok"] is False and "log file" in out["message"] and "choco upgrade bitwarden -y" in out["message"], out


def test_guest_is_refused_everywhere():
    fake = setup(Fake(), allowed=True)
    as_guest()
    out = apply("choco", "bitwarden")
    assert out["ok"] is False and "owner's" in out["message"], out
    out = apply("choco", "bitwarden", dry_run=True)
    assert out["ok"] is False and "owner's" in out["message"], out
    out = pu._run_elevated("choco", "bitwarden")                   # the second lock, called directly
    assert out["ok"] is False and "owner's" in out["message"], out
    assert fake.calls == []


def test_only_choco_and_only_a_plain_id_reach_the_elevated_run():
    fake = setup(Fake(), allowed=True)
    for manager, package in (("winget", "Git.Git"), ("scoop", "git"), ("pip", "rich"), ("pipx", "black"), ("npm", "left-pad")):
        out = pu._run_elevated(manager, package)
        assert out["ok"] is False and "administrator window" in out["message"], (manager, out)
    for bad in ("x'; calc; '", "a b", "-y", "../x", "x\"y", "a;b", "x&y", ""):
        out = pu._run_elevated("choco", bad)
        assert out["ok"] is False, bad
        out = apply("choco", bad)
        assert out["ok"] is False and not out["message"].startswith("Needs an administrator window"), (bad, out)
    assert fake.calls == []
    # quoting in the script builder, whatever reaches it
    script = pu._elevated_inner_script(["choco", "upgrade", "x'; calc; '", "-y"], "C:\\it's\\log.txt")
    assert "'x''; calc; '''" in script and "$log = 'C:\\it''s\\log.txt'" in script, script


def test_logs_are_kept_in_a_jarvis_folder_pruned_and_unique():
    folder = Path(HOME) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i in range(30):
        f = folder / ("old-%02d.log" % i)
        f.write_text("x", encoding="utf-8")
        os.utime(f, (now - 1000 - i, now - 1000 - i))
    fake = setup(Fake(log="done\n"), allowed=True)
    first = apply("choco", "bitwarden")
    second = apply("choco", "bitwarden")
    assert first["log"] != second["log"] and Path(first["log"]).name.startswith("choco-bitwarden-")
    assert len(list(folder.glob("*.log"))) <= pu.KEEP_LOGS + 1, len(list(folder.glob("*.log")))
    assert Path(first["log"]).parent == folder


def test_log_text_is_untrusted_and_bounded():
    huge = "line\n" * 20000 + "ERROR: the last line\n"
    setup(Fake(ps=(0, "JARVIS_ELEVATED_EXIT=1\r\n", ""), log=huge), allowed=True)
    out = apply("choco", "bitwarden")
    assert "the last line" in out["message"] and len(out["message"]) <= 200, out["message"]
    setup(Fake(ps=(0, "JARVIS_ELEVATED_EXIT=1\r\n", ""), log="\ufeffERROR: \x00bad \x1b[31mcolour\n"), allowed=True)
    out = apply("choco", "bitwarden")
    assert "\x00" not in out["message"] and "\x1b" not in out["message"] and "\ufeff" not in out["message"], repr(out["message"])


# =========================================================================== rules about the code itself

def test_source_rules():
    source = (ROOT / "jarvis-cli" / "jarvis" / "actions" / "pkg_updates.py").read_text(encoding="utf-8")
    for word in ("shell=True", "os.system", "os.popen", "import subprocess", "Popen(", "ShellExecute"):
        assert word not in source, word
    assert source.count("-Verb RunAs") == 1, "one place opens a UAC prompt"
    at = source.index("-Verb RunAs")
    assert source.rindex("def ", 0, at) == source.index("def _elevated_outer_script"), "and it is the script builder"
    for name in ("policy", "tool_router", "ai_client"):
        assert not re.search(r"^\s*(?:from \S* ?import|import) .*\b%s\b" % name, source, re.M), "pkg_updates.py does not import %s" % name
    assert "pkg_tools" not in sys.modules.get("jarvis.actions.pkg_updates").__dict__, "pkg_tools stays a lazy import"
    spec = importlib.util.spec_from_file_location("jarvis.actions._pkg_updates_copy2", ROOT / "jarvis-cli" / "jarvis" / "actions" / "pkg_updates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.TOOLS["apply_pkg_update"]), "the file still loads on its own"


def test_checklist_entries_still_valid_and_mention_the_new_behaviour():
    for name, entry in pu.TEST_CHECKLIST.items():
        assert checklist_schema.validate_entry(name, entry) == [], name
    text = json.dumps(pu.TEST_CHECKLIST["apply_pkg_update"])
    assert "Needs an administrator window" in text and "A Windows UAC window will open" in text
    assert "package_update_checks.py --admin" in text, "the owner is told how to send back the real output"


def test_never_raises():
    setup(Fake())
    pu.ADMIN_ROOTS = lambda: 1 / 0
    out = apply("choco", "bitwarden")
    assert out["ok"] is False and out["state"] == "failed"
    setup(Fake(), allowed=True)
    pu.UAC_ALLOWED = lambda: 1 / 0
    assert pu._uac_allowed() is False
    assert apply("choco", "bitwarden")["ok"] is False


# --------------------------------------------------------------------------- runner (test functions are above this block)

if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        reset()
        try:
            fn()
            passed += 1
            print("  ok   %s" % name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  FAIL %s: %s: %s" % (name, type(exc).__name__, exc))
        finally:
            reset()
    print("\n%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)
