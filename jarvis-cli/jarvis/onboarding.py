"""Guided first run: get Jarvis working before something breaks.

WHY THIS EXISTS
---------------
`doctor.py` is excellent and arrives too late. It tells you your key is a
placeholder, your reply_allowlist is empty and ffmpeg isn't on PATH — but
only once you have already tried something, watched it fail in a confusing
way, and thought to run a diagnostic. The gotchas are all known in advance;
the only reason they are discovered by failure is that nothing ever asks.

So this walks the same ground forwards. Each step knows what it needs, can
report whether it is already satisfied, and hands back the exact command or
config edit that would satisfy it.

ONE DEFINITION, THREE FRONT-ENDS
--------------------------------
The steps are plain data with pure predicate functions, so the CLI wizard,
the classic web UI and the new one all render the SAME list and can't drift
apart. A front-end asks for `steps()` and decides how to draw them; nothing
about a terminal, a modal or a websocket appears in this file.

It is also re-runnable and non-destructive. `jarvis onboard` on a fully
configured machine shows every step green and changes nothing — which
means it doubles as a setup checklist rather than being a one-shot rite
you can never see again.
"""

import os
import shutil
from pathlib import Path

from . import atomic_io

JARVIS_DIR = Path.home() / ".jarvis"
STATE_FILE = JARVIS_DIR / "onboarding.json"

# UI layout preference. Lives here rather than in ai_config because it is a
# property of the person, not of the model configuration, and because the
# web UI must be able to read it before anything else is set up.
UI_CLASSIC = "classic"
UI_FOCUS = "focus"
UI_MODES = (UI_CLASSIC, UI_FOCUS)
DEFAULT_UI_MODE = UI_CLASSIC

STATUS_OK = "ok"
STATUS_TODO = "todo"
STATUS_OPTIONAL = "optional"


def _state():
    return atomic_io.read_json(STATE_FILE, default={}, expect=dict)


def _write_state(**fields):
    state = _state()
    state.update(fields)
    atomic_io.write_json(STATE_FILE, state)
    return state


def ui_mode():
    """Which layout the web UI should render. Never raises, always valid."""
    mode = (_state().get("ui_mode") or "").strip().lower()
    return mode if mode in UI_MODES else DEFAULT_UI_MODE


def set_ui_mode(mode):
    mode = (mode or "").strip().lower()
    if mode not in UI_MODES:
        return False, f"unknown UI mode '{mode}' — use " + " or ".join(UI_MODES)
    _write_state(ui_mode=mode)
    return True, mode


def completed():
    return bool(_state().get("completed"))


def mark_complete(value=True):
    _write_state(completed=bool(value))
    return value


def should_prompt():
    """Has this machine never been through setup?

    Checked by the CLI and both UIs on start. Deliberately based on the
    state file rather than on whether config exists: someone who skipped
    the wizard on purpose should not be asked again every launch.
    """
    state = _state()
    return not state.get("completed") and not state.get("skipped")


def skip():
    _write_state(skipped=True)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def _check_provider():
    from . import ai_config
    try:
        cfg = ai_config.load_ai_config()
    except Exception:  # noqa: BLE001
        return False, "couldn't read ai_config.json"
    providers = cfg.get("providers") or []
    usable = []
    for provider in providers:
        if not provider.get("enabled", True):
            continue
        keys = [k for k in (provider.get("api_keys") or
                            ([provider.get("api_key")] if provider.get("api_key") else []))
                if str(k or "").strip()]
        # A placeholder key is worse than none: it looks configured and
        # fails at the worst moment, which is exactly the class of problem
        # this wizard exists to catch up front.
        real = [k for k in keys if not _looks_placeholder(k)]
        if real or (provider.get("type") == "ollama"):
            usable.append(provider.get("name") or provider.get("type") or "?")
    if usable:
        return True, "configured: " + ", ".join(usable[:4])
    return False, "no provider has a usable API key yet"


def _looks_placeholder(key):
    text = str(key or "").strip().lower()
    if len(text) < 12:
        return True
    return any(token in text for token in
               ("your", "paste", "xxxx", "placeholder", "changeme", "sk-..."))


def _check_ui_mode():
    state = _state()
    if state.get("ui_mode") in UI_MODES:
        return True, f"using the {state['ui_mode']} layout"
    return False, "not chosen yet — you'll be asked"


def _check_channels():
    try:
        from .channels import config as channel_config
        from .channels import PLATFORMS
    except Exception:  # noqa: BLE001
        return True, "channels unavailable — skipping"
    cfg = channel_config.load_config()
    enabled = [p for p in PLATFORMS if (cfg.get(p) or {}).get("enabled")]
    if not enabled:
        return True, "no chat channels enabled (that's fine)"
    problems = []
    for platform in enabled:
        block = cfg.get(platform) or {}
        if not block.get("reply_allowlist"):
            problems.append(f"{platform}: reply_allowlist is empty, so nobody gets an answer")
        if platform == "discord" and not str(block.get("bot_token") or "").strip():
            problems.append("discord: no bot_token")
        if platform == "instagram" and not str(block.get("access_token") or "").strip():
            problems.append("instagram: no access_token")
    if problems:
        return False, "; ".join(problems)
    return True, "enabled: " + ", ".join(enabled)


def _check_dependencies():
    missing = [name for name in ("ffmpeg", "git") if not shutil.which(name)]
    optional_missing = []
    for module, label in (("requests", "requests"), ("psutil", "psutil")):
        try:
            __import__(module)
        except ImportError:
            optional_missing.append(label)
    if optional_missing:
        return False, "missing required packages: " + ", ".join(optional_missing)
    if missing:
        return True, "optional tools not installed: " + ", ".join(missing)
    return True, "all present"


def _check_daemons():
    try:
        from . import daemons
    except Exception:  # noqa: BLE001
        return True, "unavailable"
    running = [d["id"] for d in daemons.list_daemons() if d.get("running")]
    if running:
        return True, "running: " + ", ".join(running)
    return True, "none running (start them when you need them)"


def _check_memory():
    try:
        from . import memory
        facts = memory.load_facts()
    except Exception:  # noqa: BLE001
        return True, "unavailable"
    if facts:
        return True, f"{len(facts)} facts saved"
    return False, "Jarvis doesn't know your name yet"


# Order matters: a step that blocks everything downstream comes first, so
# someone who quits halfway has a working assistant rather than a
# well-decorated broken one.
STEPS = [
    {
        "id": "ui_mode",
        "title": "Pick a layout",
        "why": "The classic layout shows everything at once; Focus keeps the "
               "chat front and centre and tucks the rest into a menu. You can "
               "switch any time.",
        "check": _check_ui_mode,
        "required": True,
        "action": "jarvis ui-mode <classic|focus>",
        "choices": [
            {"value": UI_CLASSIC, "label": "Classic",
             "hint": "Commands, detail and console panels all visible."},
            {"value": UI_FOCUS, "label": "Focus",
             "hint": "Chat first; everything else one click away."},
        ],
    },
    {
        "id": "provider",
        "title": "Add an AI provider key",
        "why": "Without a working key Jarvis can't answer anything at all. "
               "Several can be configured and Jarvis fails over between them.",
        "check": _check_provider,
        "required": True,
        "action": "jarvis ai-config   (then paste a key into api_keys)",
    },
    {
        "id": "memory",
        "title": "Tell Jarvis who you are",
        "why": "Your name, timezone and how you like to be addressed ride "
               "along in every prompt. Without them it has to guess.",
        "check": _check_memory,
        "required": False,
        "action": "Just say: 'remember my name is <name>'",
    },
    {
        "id": "dependencies",
        "title": "Check dependencies",
        "why": "Required packages are needed for basic tools. ffmpeg and git "
               "are optional but several tools quietly can't work without them.",
        "check": _check_dependencies,
        "required": True,
        "action": "jarvis doctor   (lists the exact install command for each)",
    },
    {
        "id": "channels",
        "title": "Chat channels (optional)",
        "why": "Reaching Jarvis from Discord or Instagram. The usual mistake "
               "is enabling a platform and leaving reply_allowlist empty, which "
               "looks like a broken bot.",
        "check": _check_channels,
        "required": False,
        "action": "jarvis channels-status",
    },
    {
        "id": "daemons",
        "title": "Background services (optional)",
        "why": "The scheduler has to be running for reminders to fire. Same "
               "for the chat gateways.",
        "check": _check_daemons,
        "required": False,
        "action": "jarvis daemon-start scheduler",
    },
]


def steps():
    """Every step with its current status. Safe to call repeatedly."""
    out = []
    for step in STEPS:
        try:
            ok, detail = step["check"]()
        except Exception as exc:  # noqa: BLE001 — a broken check reports
            # itself rather than taking down the whole wizard.
            ok, detail = False, f"couldn't check: {exc}"
        if ok:
            status = STATUS_OK
        else:
            status = STATUS_TODO if step.get("required") else STATUS_OPTIONAL
        entry = {k: v for k, v in step.items() if k != "check"}
        entry["status"] = status
        entry["detail"] = detail
        out.append(entry)
    return out


def summary():
    found = steps()
    blocking = [s for s in found if s["status"] == STATUS_TODO]
    return {
        "ok": True,
        "completed": completed(),
        "should_prompt": should_prompt(),
        "ui_mode": ui_mode(),
        "steps": found,
        "blocking": [s["id"] for s in blocking],
        "ready": not blocking,
    }


def render_for_terminal(data=None):
    """The CLI wizard's output. One block per step, nothing interactive —
    the caller drives any prompting, so this stays testable."""
    data = data or summary()
    icons = {STATUS_OK: "ok  ", STATUS_TODO: "TODO", STATUS_OPTIONAL: "--  "}
    lines = ["", "Jarvis setup", "=" * 44, ""]
    for step in data["steps"]:
        lines.append(f"[{icons.get(step['status'], '?   ')}] {step['title']}")
        lines.append(f"       {step['detail']}")
        if step["status"] != STATUS_OK:
            lines.append(f"       {step['why']}")
            lines.append(f"       -> {step['action']}")
        lines.append("")
    if data["ready"]:
        lines.append("Everything required is in place.")
    else:
        lines.append("Still needed: " + ", ".join(data["blocking"]))
    lines.append("")
    return "\n".join(lines)
