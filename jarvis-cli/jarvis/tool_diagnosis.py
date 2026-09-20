"""Turn a tool's raw error into "here's what broke and here's the fix".

THE GAP THIS FILLS
------------------
`doctor.py` already knows that ffmpeg is installed with
`winget install Gyan.FFmpeg`, that pyautogui is the desktop extra, that a
401 from a provider means the key is rejected rather than the model name
being wrong. But all of that knowledge is locked inside checks that only
run when someone types `jarvis doctor` — which is, by definition, after
something has already gone wrong and confused them.

Meanwhile a failing tool returns `{"ok": false, "error": "[WinError 2] The
system cannot find the file specified"}` and the model dutifully reports
that to the user, who now has to go and find out what file.

This module sits between the two: given a tool name and its error, it
matches against the same tables doctor uses and returns a plain-language
cause plus the actual command to fix it. `_make_tool_executor()` attaches
the result to the failed tool's own return value, so the model has the fix
in hand on the very next round and can say "ffmpeg isn't installed — run
`winget install Gyan.FFmpeg`" instead of quoting a WinError.

WHY MATCHING, NOT ASKING A MODEL
--------------------------------
The obvious alternative is a second AI call to explain the error. That
costs tokens and latency on the unhappy path, can hallucinate an install
command for a package that doesn't exist, and would need a provider to be
reachable — which is precisely what is broken in a meaningful share of
these cases. Pattern matching over a table Jarvis already maintains is
free, offline, deterministic, and wrong in obvious ways rather than
plausible ones.

It is also explicitly incomplete. No match returns None and the raw error
passes through untouched, exactly as before. This only ever adds.
"""

import platform
import re

# Imported lazily inside functions — doctor.py imports a lot and this
# module is reached from the tool executor's hot path, where a failed tool
# should not drag the whole diagnostic suite into memory. See _tables().
_TABLES = None


def _tables():
    global _TABLES
    if _TABLES is None:
        try:
            from . import doctor
            _TABLES = (doctor._BINARIES, doctor._PY_PACKAGES)
        except Exception:  # noqa: BLE001 — diagnosis must never be the thing
            # that breaks; an unavailable doctor just means no hints.
            _TABLES = ({}, {})
    return _TABLES


def _install_hint(entry):
    return entry.get("install", {}).get(
        platform.system(), "install it and reopen your terminal")


# Tool name -> the binaries/packages it actually needs. Kept here rather
# than in doctor because doctor's tables are keyed the other way round (by
# dependency, listing the feature) and this lookup runs per failure.
_TOOL_DEPS = {
    "ytdl_download": {"binaries": ["ffmpeg"], "packages": ["yt_dlp"]},
    "ytdl_info": {"packages": ["yt_dlp"]},
    "ytdl_formats": {"packages": ["yt_dlp"]},
    "take_screenshot": {"packages": ["PIL"]},
    "click_on_text": {"binaries": ["tesseract"], "packages": ["pytesseract", "PIL"]},
    "read_screen": {"binaries": ["tesseract"], "packages": ["pytesseract", "PIL"]},
    "write_on_screen": {"packages": ["pyautogui"]},
    "click": {"packages": ["pyautogui"]},
    "type_text": {"packages": ["pyautogui"]},
    "hotkey": {"packages": ["pyautogui"]},
    "git_run": {"binaries": ["git"]},
    "git_commit_all": {"binaries": ["git"]},
    "web_search": {"packages": ["ddgs"]},
    "web_fetch": {"packages": ["requests"]},
    "get_battery": {"packages": ["psutil"]},
    "get_memory": {"packages": ["psutil"]},
    "get_disk": {"packages": ["psutil"]},
    "speak": {"packages": ["sounddevice"]},
    "listen": {"packages": ["sounddevice"]},
    "transcribe": {"packages": ["sounddevice"]},
    "notify_owner": {"packages": ["discord"]},
}

# (pattern, cause, fix). Ordered — the first match wins, so put the
# specific patterns above the generic ones.
_ERROR_PATTERNS = (
    (r"winerror 2\b|cannot find the file specified|no such file or directory",
     "The program or file it tried to run isn't there.",
     "If this needs an external tool (ffmpeg, git, tesseract), install it "
     "and reopen the terminal so PATH refreshes. Run `jarvis doctor` to see "
     "which dependency is missing."),
    (r"winerror 5\b|permission denied|access is denied|eacces",
     "The OS refused the operation on permission grounds.",
     "Either the file is open in another program, or this needs elevation. "
     "Radio changes (wifi_set/bluetooth_set off) and some installs need an "
     "Admin terminal."),
    (r"modulenotfounderror|no module named ['\"]?(\w+)",
     "A Python package this tool depends on isn't installed.",
     "Install it with pip, then retry. `jarvis doctor` lists every missing "
     "optional package and the exact pip name for each."),
    (r"winerror 10061|connection refused|econnrefused",
     "Nothing is listening on the address it tried to reach.",
     "The service it talks to isn't running. For Playnite, start Playnite "
     "and confirm the web API plugin is enabled; for a local server, check "
     "`jarvis daemons` to see whether its daemon is up."),
    (r"timed out|timeout|etimedout",
     "The operation took longer than its limit and was abandoned.",
     "Usually a slow network or an unresponsive service. Retry once; if it "
     "is consistent, the far end is down rather than slow."),
    (r"\b401\b|unauthorized|invalid api key|invalid_api_key",
     "The API key was rejected.",
     "Regenerate the key in the provider's dashboard and update "
     "ai_config.json. `jarvis doctor --deep` probes every configured key."),
    (r"\b402\b|quota|insufficient.credit|billing",
     "The account behind this key is out of credit or quota.",
     "Top up, or move that provider down defaults.provider_priority so "
     "Jarvis tries a working one first."),
    (r"\b429\b|rate.?limit",
     "Rate limited by the provider.",
     "Wait, or add more keys to that provider's api_keys — Jarvis fails "
     "over between keys automatically."),
    (r"\b404\b.*model|model.*not found|unknown model",
     "The model name isn't valid for this provider.",
     "Check the \"model\" field in that provider's block in ai_config.json."),
    (r"winerror 1223|operation was canceled by the user",
     "A UAC elevation prompt was dismissed.",
     "Re-run and accept the prompt, or run Jarvis from an Admin terminal."),
    (r"disk|no space left|enospc",
     "The disk is full.",
     "Free space and retry. `jarvis doctor` reports what Jarvis's own "
     "directory is using."),
    (r"ssl|certificate verify failed",
     "TLS certificate verification failed.",
     "Usually a corporate proxy or a clock that's badly wrong. Check the "
     "system time first — it's the cheaper of the two to rule out."),
    (r"json|expecting value|decode",
     "Something returned data that wasn't the format expected.",
     "Often a service returning an HTML error page instead of JSON. Check "
     "the daemon console or `jarvis logs-files <tool name>` for the raw "
     "response."),
)


def _error_text(result):
    """Pull the human-readable error out of whatever shape a tool returned."""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    for field in ("error", "detail", "message", "stderr", "stderr_tail"):
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def looks_like_failure(result):
    """Did this tool call fail?

    Deliberately narrow. `ok: False` and a non-empty `error` are the two
    real signals; `needs_clarification` and `cancelled` are normal control
    flow (the model asking for an argument, the user declining a
    confirmation) and attaching a diagnosis to those would be noise on a
    path that is working correctly.
    """
    if not isinstance(result, dict):
        return False
    if result.get("needs_clarification") or result.get("cancelled"):
        return False
    if result.get("need_args"):
        return False
    if result.get("ok") is False:
        return True
    return bool(_error_text(result))


def _cmd_builtins():
    """The cmd.exe builtin names `run_shell` already knows to route through
    `cmd /c` (see actions/code_agent.py, master plan F.4). Imported lazily,
    same reasoning as _tables(): this module sits on the tool-executor hot
    path and shouldn't drag in an actions module for the common case where
    nothing failed."""
    try:
        from .actions import code_agent
        return code_agent._CMD_BUILTINS
    except Exception:  # noqa: BLE001 — diagnosis must never be the thing
        # that breaks; an unavailable table just means no special-case hint.
        return set()


# Tools whose failures are classified by _diagnose_ocr() rather than by the
# generic name-matching in diagnose().
_OCR_TOOLS = {"read_screen", "click_on_text"}

# ocr_tools.py's messages start with one of these (see _no_pytesseract(),
# _no_tesseract_binary() and the "OCR failed:" branches there). Matching on
# the START of the message, which the tool controls, is what makes this
# reliable: the whole message also carries a long install note that names
# tesseract, pytesseract and Pillow whatever actually went wrong.
# tests/test_ocr_diagnosis.py builds the real messages from ocr_tools so a
# reworded prefix fails a test instead of silently disabling this.
_OCR_NO_PIP = "pytesseract/pillow not installed"
_OCR_NO_BINARY = "tesseract binary not found on path"
_OCR_RAN_AND_FAILED = "ocr failed"
_TESSDATA_RE = re.compile(
    r"tessdata|traineddata|failed loading language|couldn't load any languages")


def _diagnose_ocr(error):
    """Classify a failed read_screen / click_on_text by what actually broke.

    THE BUG THIS REPLACES
    ---------------------
    The generic matcher treats any error containing "tesseract" or "not
    found" as "the Tesseract program is missing". For OCR that was wrong in
    three ways, each reproduced against the real ocr_tools code paths:

    - Tesseract found, language data missing or broken ("OCR failed: Error
      opening data file ... TESSDATA_PREFIX"): diagnosed as "Missing
      dependency: tesseract — install it". It is installed; the user is
      told to install what they already have while the real reason sits in
      the raw error the model was told not to repeat.
    - Tesseract genuinely not on PATH: also diagnosed as missing pytesseract
      and Pillow ("pip install ..."), because ocr_tools' install note names
      them. They are installed. The fix also said "reopen your terminal",
      which does nothing when the process that needs the new PATH is the
      long-running web server.
    - A pip-package-missing error also listed the Tesseract program as
      missing, since "pytesseract" contains "tesseract".

    Returns None for anything unrecognised (e.g. "screen capture failed:")
    rather than guessing: naming Tesseract for a capture failure is worse
    than saying nothing, and this module only ever adds.
    """
    lowered = error.lower().strip()
    binaries, _packages = _tables()
    tess = binaries.get("tesseract") or {}
    install = _install_hint(tess)
    windows = platform.system() == "Windows"

    if lowered.startswith(_OCR_NO_PIP):
        return {
            "cause": ("The Python packages OCR needs (pytesseract, Pillow) "
                      "aren't installed in the Python that runs Jarvis."),
            "fix": ("Run: pip install pytesseract Pillow  — into the same "
                    "Python that runs Jarvis (the web server uses whichever "
                    "`python` it finds first)."),
            "check": "jarvis doctor",
            "missing": [
                {"kind": "python package", "name": "pytesseract",
                 "why": "click_on_text / read_screen",
                 "install": "pip install pytesseract"},
                {"kind": "python package", "name": "Pillow",
                 "why": "screenshots and OCR preprocessing",
                 "install": "pip install Pillow"},
            ],
        }

    if lowered.startswith(_OCR_NO_BINARY):
        # Only the program is implicated: this message is raised by
        # pytesseract after both Python packages imported fine.
        if windows:
            fix = ("Open a NEW terminal and run `where tesseract`. If it "
                   "prints a path, Tesseract is installed: fully close and "
                   "restart the web server from that new terminal — a "
                   "process started earlier, or by double-clicking, keeps "
                   "the PATH it started with. If it prints nothing, install "
                   f"it ({install}) or add its folder (usually "
                   "C:\\Program Files\\Tesseract-OCR) to PATH.")
        else:
            fix = ("Open a new terminal and run `which tesseract`. If it "
                   "prints a path, restart whatever launched Jarvis (the web "
                   "server, if you use it) from that terminal — a process "
                   f"started earlier keeps its old PATH. If not: {install}.")
        return {
            "cause": ("Jarvis can't find the Tesseract program from the "
                      "process that ran this tool. Either it isn't "
                      "installed, or it is but this process started before "
                      "it was added to PATH."),
            "fix": fix,
            "check": "jarvis doctor",
            "missing": [{"kind": "program", "name": "tesseract",
                         "why": tess.get("why", ""), "install": install}],
        }

    if lowered.startswith(_OCR_RAN_AND_FAILED):
        # Tesseract was found and started — this is NOT a missing-program
        # error, so deliberately no "missing" list and no install command.
        if _TESSDATA_RE.search(lowered):
            return {
                "cause": ("Tesseract is installed and was found, but it "
                          "can't load its language data (normally "
                          "eng.traineddata)."),
                "fix": ("Set the TESSDATA_PREFIX environment variable to the "
                        "folder that contains eng.traineddata (the `tessdata` "
                        "folder inside Tesseract's install folder), or "
                        "reinstall Tesseract with the English language data. "
                        "Then restart whatever launched Jarvis so it sees "
                        "the variable."),
                "check": "jarvis doctor",
            }
        return {
            "cause": ("Tesseract was found but failed while running; the "
                      "exact reason is in the error text."),
            "fix": ("In a terminal run `tesseract --version` and `tesseract "
                    "--list-langs`. If either fails, that is the real "
                    "problem (a broken install, or a different tesseract "
                    "first on PATH — `where tesseract` on Windows, `which "
                    "tesseract` elsewhere, shows which one runs)."),
            "check": "jarvis doctor",
        }

    return None


def diagnose(tool_name, result):
    """Explain a failed tool call. Returns a dict, or None when there's
    nothing useful to add — in which case the caller changes nothing.

    Shape:
        {"cause": str, "fix": str, "missing": [...], "check": "jarvis doctor"}
    """
    error = _error_text(result)
    if not error:
        return None
    lowered = error.lower()
    binaries, packages = _tables()

    # Special case, checked before the generic WinError-2 pattern below:
    # for run_shell specifically, "[WinError 2] ... cannot find the file
    # specified" is at least as likely to mean "the model ran a cmd.exe
    # builtin (dir, type, echo, ...) directly" as "a real external program
    # is missing" — and the generic ffmpeg/git/tesseract advice is simply
    # wrong for the first case. This needs the actual command line, which
    # is why _run_shell_impl now carries a `command` field on every
    # outcome (see master plan F.4's "diagnosis text" follow-up). Only
    # fires for run_shell: no other tool's result has a `command` field to
    # check, and misreading some other tool's incidental "command" key as
    # this shape would be worse than saying nothing.
    if tool_name == "run_shell" and isinstance(result, dict) and \
            re.search(r"winerror 2\b|cannot find the file specified", lowered):
        command = (result.get("command") or "").strip()
        first_word = command.split(None, 1)[0].lower() if command else ""
        if first_word in _cmd_builtins():
            return {
                "cause": (
                    f"'{first_word}' is a cmd.exe builtin, not a standalone "
                    "program — subprocess has no .exe to find for it."
                ),
                "fix": f"Run it as: cmd /c {command}",
                "check": "jarvis doctor",
            }

    # OCR tools get their own classifier instead of the generic matching
    # below. See _diagnose_ocr() for why: the generic path reads the tool's
    # own install note as if it were the failure, and blames Tesseract for
    # every OCR error including ones where Tesseract was found and ran.
    if tool_name in _OCR_TOOLS:
        return _diagnose_ocr(error)

    missing = []
    fixes = []

    # 1. A dependency this specific tool is known to need, named directly
    #    in the error. This is the highest-confidence match: we know both
    #    what the tool requires and that the error mentions it.
    deps = _TOOL_DEPS.get(tool_name, {})
    for name in deps.get("binaries", []):
        entry = binaries.get(name) or {}
        if name in lowered or "winerror 2" in lowered or "not found" in lowered:
            missing.append({"kind": "program", "name": name,
                            "why": entry.get("why", ""),
                            "install": _install_hint(entry)})
    for name in deps.get("packages", []):
        entry = packages.get(name) or {}
        pip_name = entry.get("pip", name)
        if name.lower() in lowered or pip_name.lower() in lowered:
            missing.append({"kind": "python package", "name": pip_name,
                            "why": entry.get("feature", ""),
                            "install": f"pip install {pip_name}"})

    # 2. A ModuleNotFoundError names its module regardless of which tool
    #    raised it, so resolve that even for a tool with no table entry.
    match = re.search(r"no module named ['\"]?([\w\.]+)", lowered)
    if match:
        module = match.group(1).split(".")[0]
        entry = packages.get(module) or {}
        pip_name = entry.get("pip", module)
        if not any(m["name"] == pip_name for m in missing):
            missing.append({"kind": "python package", "name": pip_name,
                            "why": entry.get("feature", ""),
                            "install": f"pip install {pip_name}"})

    cause = ""
    for pattern, why, fix in _ERROR_PATTERNS:
        if re.search(pattern, lowered):
            cause = why
            fixes.append(fix)
            break

    if not cause and not missing:
        return None

    if missing:
        commands = [m["install"] for m in missing if m.get("install")]
        if commands:
            # A concrete command beats the generic advice from the pattern
            # table, so it goes first and the generic line is dropped.
            fixes = ["Run: " + "  ;  ".join(dict.fromkeys(commands))]
        if not cause:
            names = ", ".join(m["name"] for m in missing)
            cause = f"Missing dependency: {names}."

    out = {
        "cause": cause,
        "fix": " ".join(fixes).strip(),
        "check": "jarvis doctor",
    }
    if missing:
        out["missing"] = missing
    return out


def annotate(tool_name, result):
    """Attach a diagnosis to a failed tool result, in place-ish.

    Returns the result unchanged when there's nothing to say, so the call
    site is a bare assignment with no branching. The key is `diagnosis`
    rather than something that reads like the tool's own output, so the
    model can tell Jarvis's explanation apart from the tool's.
    """
    if not looks_like_failure(result):
        return result
    try:
        found = diagnose(tool_name, result)
    except Exception:  # noqa: BLE001 — never let diagnosis break a tool path
        return result
    if not found:
        return result
    enriched = dict(result)
    enriched["diagnosis"] = found
    enriched.setdefault(
        "hint",
        "Tell the user what broke and the fix above, in one short line — "
        "don't just repeat the raw error.")
    return enriched
