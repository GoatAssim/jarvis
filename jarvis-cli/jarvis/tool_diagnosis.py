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
