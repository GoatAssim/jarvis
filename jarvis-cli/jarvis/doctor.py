"""`jarvis doctor` — one command that checks everything that usually breaks.

WHY THIS EXISTS
---------------
The Guides panel documents a long list of gotchas: ffmpeg has to be on PATH
for audio merging, tesseract for click_on_text, the Playnite token has to be
copied out of the plugin, an empty allowlist means deny (not allow), a
Discord bot can only DM someone who shares a server with it, a stale pid
file blocks the scheduler daemon, keys that look present can be expired.

Every one of those is a thing a person reads, forgets, and then rediscovers
as a confusing failure an hour later — "click_on_text just returns an
error", "the reminder never fired", "it says it messaged me and I got
nothing". Documentation cannot fix that, because the failure and the
paragraph explaining it are separated by a week.

So this module turns the gotchas list into executable checks. Each one knows
how to detect its own failure and — the part that actually matters — what
the user should type to fix it.

DESIGN RULES
------------
1. **Never destructive.** Every check is read-only. `doctor` is the thing you
   run when something is already broken; it must not be able to make it
   worse. Nothing here writes a config, kills a process, or installs
   anything. (`--fix` is deliberately NOT implemented for this reason.)

2. **Never fatal.** One check raising must not stop the rest — a machine
   with no Playnite, no Discord and no voice extras is a completely normal
   machine, and it should still get its PATH and API-key results. Every
   check runs inside `_safe()`.

3. **Offline by default.** The default run touches no network: it reads
   config, stats files and PATH. `--deep` additionally makes real requests
   (a minimal API call per provider key, a Playnite ping, an MCP handshake),
   which costs a few tokens and a few seconds, and is the only way to tell
   "a key is present" from "a key works".

4. **Every failure carries a fix.** A check that can only say "broken" has
   moved the problem, not solved it. `fix` is a literal command to run or a
   file to edit, not prose.

STATUSES
--------
    ok      nothing to do
    warn    works, but something will bite later (near a cap, deprecated
            setting, optional dependency missing for a feature in use)
    fail    actively broken right now
    skip    not applicable (feature disabled / not installed / not on this OS)

`skip` is a first-class result, not a soft failure. Jarvis without Instagram
configured is not unhealthy, and a report that shouts about every unused
integration trains people to ignore it.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
ENCODING = "utf-8"

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_ORDER = {FAIL: 0, WARN: 1, OK: 2, SKIP: 3}

# Binaries, what breaks without them, and how to get them. Only ffmpeg and
# tesseract are called out in the Guides panel today, but the same "silently
# degrades" failure applies to each of these.
_BINARIES = {
    "ffmpeg": {
        "why": "merging video+audio for youtube_download, and voice recording formats",
        "needed_by": "youtube",
        "install": {
            "Windows": "winget install Gyan.FFmpeg   (then reopen the terminal)",
            "Darwin": "brew install ffmpeg",
            "Linux": "sudo apt install ffmpeg",
        },
    },
    "ffprobe": {
        "why": "reading media duration/format before a download is merged",
        "needed_by": "youtube",
        "install": {
            "Windows": "ships with ffmpeg — winget install Gyan.FFmpeg",
            "Darwin": "brew install ffmpeg",
            "Linux": "sudo apt install ffmpeg",
        },
    },
    "tesseract": {
        "why": "click_on_text — without it, clicking by visible label can't work at all",
        "needed_by": "ocr",
        "install": {
            "Windows": "winget install UB-Mannheim.TesseractOCR",
            "Darwin": "brew install tesseract",
            "Linux": "sudo apt install tesseract-ocr",
        },
    },
    "git": {
        "why": "the whole system_control git toolset",
        "needed_by": "git",
        "install": {
            "Windows": "winget install Git.Git",
            "Darwin": "brew install git",
            "Linux": "sudo apt install git",
        },
    },
    "node": {
        "why": "the web UI server (web/server.js)",
        "needed_by": "web",
        "install": {
            "Windows": "winget install OpenJS.NodeJS.LTS",
            "Darwin": "brew install node",
            "Linux": "sudo apt install nodejs npm",
        },
    },
}

# Optional Python packages, by the feature that needs them. Keyed on import
# name, which is NOT always the pip name (hence `pip`).
_PY_PACKAGES = {
    "requests": {"pip": "requests", "feature": "every HTTP call", "required": True},
    "psutil": {"pip": "psutil", "feature": "battery/memory/disk tools", "required": True},
    "ddgs": {"pip": "ddgs", "feature": "web_search", "required": True},
    "pyautogui": {"pip": "pyautogui", "feature": "desktop control (click/type/hotkey)", "extra": "desktop"},
    "pytesseract": {"pip": "pytesseract", "feature": "click_on_text", "extra": "ocr"},
    "PIL": {"pip": "Pillow", "feature": "screenshots and OCR preprocessing", "extra": "ocr"},
    "yt_dlp": {"pip": "yt-dlp", "feature": "youtube_download", "extra": "ytdl"},
    "discord": {"pip": "discord.py", "feature": "the Discord channel", "extra": None},
    "sounddevice": {"pip": "sounddevice", "feature": "voice record/playback", "extra": "voice-audio"},
}


class Check:
    """One diagnostic result.

    `fix` is the literal thing to do next. `detail` says what was observed.
    Keeping those separate is what lets the terminal renderer indent fixes
    under their finding and the web UI render them as a copyable command.
    """

    __slots__ = ("id", "title", "status", "detail", "fix", "group", "data")

    def __init__(self, check_id, title, status, detail="", fix="", group="general", data=None):
        self.id = check_id
        self.title = title
        self.status = status
        self.detail = detail
        self.fix = fix
        self.group = group
        self.data = data or {}

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "detail": self.detail, "fix": self.fix, "group": self.group,
            "data": self.data,
        }


def _safe(fn, check_id, title, group):
    """Run one check; turn any escape into a `warn` rather than a crash.

    A check that itself breaks is a bug worth seeing, but not one worth
    losing the other twenty results over — especially since the most likely
    cause is the very corruption the run was meant to find.
    """
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — see docstring
        return [Check(check_id, title, WARN,
                      detail="this check itself failed: %s" % exc,
                      fix="Report it — the rest of the report below is still valid.",
                      group=group)]
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding=ENCODING)), ""
    except FileNotFoundError:
        return None, "missing"
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, "unparseable: %s" % exc
    except OSError as exc:
        return None, "unreadable: %s" % exc


def _install_hint(entry):
    return entry.get("install", {}).get(platform.system(), "install it and reopen your terminal")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_runtime():
    out = []
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 8):
        out.append(Check("runtime.python", "Python version", FAIL,
                         "running %d.%d; jarvis needs 3.8+" % (major, minor),
                         "Install Python 3.8 or newer and reinstall jarvis.", "runtime"))
    else:
        out.append(Check("runtime.python", "Python version", OK,
                         "%d.%d on %s" % (major, minor, platform.system() or "?"),
                         group="runtime"))
    try:
        from . import build_info
        out.append(Check("runtime.version", "Jarvis build", OK,
                         build_info.version_string(), group="runtime"))
    except Exception:  # noqa: BLE001 — build_info is cosmetic
        pass

    if not JARVIS_DIR.exists():
        out.append(Check("runtime.home", "~/.jarvis", WARN,
                         "doesn't exist yet — nothing has been configured",
                         "Run any jarvis command once; it's created on first use.", "runtime"))
    elif not os.access(JARVIS_DIR, os.W_OK):
        out.append(Check("runtime.home", "~/.jarvis", FAIL,
                         "exists but isn't writable — no config, memory or "
                         "conversation can be saved",
                         "Fix the folder's permissions: %s" % JARVIS_DIR, "runtime"))
    else:
        out.append(Check("runtime.home", "~/.jarvis", OK, str(JARVIS_DIR), group="runtime"))
    return out


def check_binaries():
    """PATH check. The single most common class of "it just doesn't work"."""
    out = []
    system = platform.system()
    for name, entry in _BINARIES.items():
        found = shutil.which(name)
        if found:
            out.append(Check("bin.%s" % name, name, OK, found, group="binaries"))
            continue
        if name == "node":
            # Only matters if you actually run the web UI, which most CLI
            # users don't — so this is never a failure.
            out.append(Check("bin.node", "node", SKIP,
                             "not on PATH — only needed for the web UI",
                             _install_hint(entry), "binaries"))
            continue
        out.append(Check("bin.%s" % name, name, WARN,
                         "not on PATH — %s won't work" % entry["why"],
                         _install_hint(entry), "binaries",
                         {"needed_by": entry["needed_by"]}))
    if system == "Windows" and not shutil.which("powershell"):
        out.append(Check("bin.powershell", "powershell", WARN,
                         "not on PATH — toasts, wifi/bluetooth toggles and some "
                         "package managers go through it",
                         "It ships with Windows; check your PATH hasn't been trimmed.",
                         "binaries"))
    return out


def check_python_packages():
    out = []
    for import_name, entry in _PY_PACKAGES.items():
        try:
            __import__(import_name)
            installed = True
        except Exception:  # noqa: BLE001 — a broken package is as good as absent
            installed = False
        if installed:
            out.append(Check("py.%s" % import_name, entry["pip"], OK,
                             "installed", group="packages"))
            continue
        if entry.get("required"):
            out.append(Check("py.%s" % import_name, entry["pip"], FAIL,
                             "missing — %s cannot work" % entry["feature"],
                             "pip install -U %s" % entry["pip"], "packages"))
        else:
            extra = entry.get("extra")
            fix = ("pip install -U 'jarvis-cli[%s]'" % extra) if extra \
                else ("pip install -U %s" % entry["pip"])
            out.append(Check("py.%s" % import_name, entry["pip"], SKIP,
                             "not installed — %s is unavailable" % entry["feature"],
                             fix, "packages"))
    return out


def check_ai_config(deep=False):
    """Providers, keys, and — the part config inspection can't do — whether
    a key that's *present* is actually a key that *works*."""
    from . import ai_config

    out = []
    path = ai_config.AI_CONFIG_FILE
    data, err = _read_json(path)
    if err == "missing":
        return [Check("ai.config", "ai_config.json", FAIL,
                      "no AI config yet — jarvis can't ask anything",
                      "jarvis ai-config   (then paste a key into any provider block)",
                      "ai")]
    if err:
        return [Check("ai.config", "ai_config.json", FAIL, err,
                      "Fix or delete %s; it's recreated with defaults." % path, "ai")]

    providers = data.get("providers") or []
    defaults = data.get("defaults") or {}
    if not isinstance(providers, list):
        return [Check("ai.config", "ai_config.json", FAIL,
                      "\"providers\" is not a list",
                      "Fix the shape in %s" % path, "ai")]

    usable = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = provider.get("name") or provider.get("type") or "?"
        if not provider.get("enabled"):
            continue
        keys = ai_config.provider_keys(provider) or []
        if provider.get("type") == "ollama":
            usable.append((name, provider, []))
            down = _endpoint_refusing(provider)
            if down:
                # F.9/F.8: both Ollama entries in the 2026-09-20 logs failed the
                # same way on every ask (connection refused, server not running).
                out.append(Check("ai.ollama.%s" % name, "%s (Ollama)" % name, WARN,
                                 "nothing is listening on %s" % down,
                                 "Start it with `ollama serve`, or set \"enabled\": false on this "
                                 "provider in %s. Until then every ask spends a request on it." % path,
                                 "ai"))
            continue
        if not keys:
            continue
        placeholder = [k for k in keys if _looks_like_placeholder(k)]
        if placeholder and len(placeholder) == len(keys):
            out.append(Check("ai.key.%s" % name, "%s key" % name, FAIL,
                             "the key is still the template placeholder",
                             "Paste a real key into %s" % path, "ai"))
            continue
        usable.append((name, provider, keys))

    if not usable:
        out.append(Check("ai.providers", "Usable providers", FAIL,
                         "every provider is disabled or has no API key",
                         "Open %s and set \"enabled\": true plus an api_key on one "
                         "provider." % path, "ai"))
    else:
        out.append(Check("ai.providers", "Usable providers", OK,
                         ", ".join("%s (%d key%s)" % (n, len(k), "" if len(k) == 1 else "s")
                                   if k else "%s (local)" % n
                                   for n, _p, k in usable), "", "ai",
                         {"providers": [n for n, _p, _k in usable]}))

    # F.9: keys the last asks parked after a 429 / a bad-key answer.
    try:
        from . import key_health
        cooling = key_health.cooling_keys()
    except Exception:  # noqa: BLE001 — a doctor check never crashes the doctor
        cooling = []
    if cooling:
        out.append(Check("ai.cooldowns", "Keys cooling down", WARN,
                         "; ".join("%s (%s, %ds left)" % (k, st or "cooling", max(1, int(left)))
                                   for k, left, st in cooling),
                         "They are tried last until the timer ends; nothing to do unless it "
                         "keeps happening (then the key is over its quota).", "ai"))

    priority = defaults.get("provider_priority") or []
    if isinstance(priority, list) and priority:
        known = {(p.get("name") or "").lower() for p in providers if isinstance(p, dict)}
        unknown = [p for p in priority if isinstance(p, str) and p.lower() not in known]
        if unknown:
            out.append(Check("ai.priority", "provider_priority", WARN,
                             "names nothing configured: %s" % ", ".join(unknown),
                             "Remove them, or fix the spelling, in %s" % path, "ai"))

    # A misconfiguration with a very confusing symptom: a provider that is
    # first in priority but has no key silently does nothing, and the user
    # sees a *different* provider answering than the one they picked.
    usable_names = {n.lower() for n, _p, _k in usable}
    dead_first = [p for p in (priority or [])[:1]
                  if isinstance(p, str) and p.lower() not in usable_names]
    if dead_first:
        out.append(Check("ai.priority.first", "First-priority provider", WARN,
                         "%s is first in provider_priority but isn't usable, so "
                         "something else is silently answering" % dead_first[0],
                         "Add a key for it, or move it down the list.", "ai"))

    if deep:
        out.extend(_probe_keys(usable))
    return out


def _endpoint_refusing(provider, timeout=0.6):
    """"host:port" if a plain TCP connect to the provider's base_url is refused
    or times out, else None. A local server that isn't running is the one
    failure worth a pre-flight: it costs a request on every single ask."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(str(provider.get("base_url") or "http://localhost:11434"))
        host, port = u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError:
        return "%s:%s" % (host, port)


def _looks_like_placeholder(key):
    text = (key or "").strip().lower()
    if not text:
        return True
    return any(mark in text for mark in
               ("your-", "paste", "xxx", "<", "changeme", "sk-...", "api-key-here", "replace"))


def _probe_keys(usable):
    """--deep: one minimal real request per key.

    Deliberately tiny (a 1-token completion) — the goal is to separate 401/
    402/429 from "works", not to measure anything. Cheap enough to run on
    every key, which matters because the whole reason multi-key support
    exists is that individual keys die quietly.
    """
    from . import ai_config, ai_providers

    out = []
    probe_messages = [
        {"role": "system", "content": "Reply with the single word: ok"},
        {"role": "user", "content": "ping"},
    ]
    for name, provider, keys in usable:
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            out.append(Check("ai.probe.%s" % name, "%s reachable" % name, FAIL,
                             "unknown provider type '%s'" % provider.get("type"),
                             "Fix \"type\" in ai_config.json", "ai"))
            continue
        for i, key in enumerate(keys or [None], start=1):
            label = name if len(keys or [1]) <= 1 else "%s key %d/%d" % (name, i, len(keys))
            resolved = dict(provider)
            resolved.setdefault("timeout", 20)
            resolved["max_tokens"] = 16
            if key is not None:
                resolved["api_key"] = key
            try:
                result = adapter(resolved, list(probe_messages), 20, tools=None,
                                 tool_executor=None)
            except Exception as exc:  # noqa: BLE001
                result = ai_providers.AIResult(False, error=str(exc))
            if result.ok:
                out.append(Check("ai.probe.%s.%d" % (name, i), label, OK,
                                 "answered", group="ai"))
            else:
                out.append(Check("ai.probe.%s.%d" % (name, i), label, FAIL,
                                 str(result.error or "no answer")[:160],
                                 _key_fix(result.error), "ai"))
    return out


def _key_fix(error):
    text = str(error or "").lower()
    if "401" in text or "unauthor" in text or ("invalid" in text and "key" in text):
        return "The key is rejected — regenerate it in the provider's dashboard."
    if "402" in text or "quota" in text or "credit" in text or "billing" in text:
        return "Out of credit — top up, or move this provider down provider_priority."
    if "429" in text or "rate" in text:
        return "Rate limited right now. If it's constant, add more keys to api_keys."
    if "404" in text or "model" in text:
        return "The model name is probably wrong for this provider — check \"model\"."
    if "timed out" in text or "timeout" in text or "connect" in text:
        return "Couldn't reach the host — check your network, or base_url if it's custom."
    return "Check the provider block in ai_config.json."


def check_playnite(deep=False):
    from . import playnite_config

    cfg = playnite_config.load_config() if hasattr(playnite_config, "load_config") else {}
    if not cfg.get("enabled", True):
        return [Check("playnite.enabled", "Playnite Bridge", SKIP,
                      "disabled in playnite.json", group="playnite")]
    token = (cfg.get("token") or "").strip()
    base = (cfg.get("base_url") or "http://127.0.0.1:19821").rstrip("/")
    if not token:
        return [Check("playnite.token", "Playnite token", WARN,
                      "no token set — every playnite_* tool will fail",
                      "Copy it from Playnite > Main Menu > Playnite Bridge into "
                      "~/.jarvis/playnite.json", "playnite")]

    if not deep:
        return [Check("playnite.token", "Playnite token", OK,
                      "set (run with --deep to check the bridge actually answers)",
                      group="playnite")]

    try:
        from . import playnite_http
        result = playnite_http.api("GET", "/api/health", timeout=4)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    if isinstance(result, dict) and result.get("error"):
        detail = str(result["error"])
        if "401" in detail or "403" in detail or "unauthor" in detail.lower():
            return [Check("playnite.reach", "Playnite Bridge", FAIL,
                          "reachable but rejected the token",
                          "Re-copy the token from the Playnite Bridge menu — it "
                          "changes when the plugin regenerates it.", "playnite")]
        return [Check("playnite.reach", "Playnite Bridge", FAIL,
                      "no answer from %s (%s)" % (base, detail[:90]),
                      "Start Playnite and make sure the Playnite Bridge plugin is "
                      "installed and enabled.", "playnite")]
    return [Check("playnite.reach", "Playnite Bridge", OK,
                  "answering on %s" % base, group="playnite")]


def check_channels():
    """The allowlist semantics are the single biggest footgun in the project:
    empty means DENY. Someone who pasted a token and expected it to work
    gets silence with no error anywhere — exactly what this check is for."""
    from .channels import config as channel_config, PLATFORMS

    out = []
    path = channel_config.CONFIG_FILE
    if not Path(path).exists():
        return [Check("channels.config", "channels.json", SKIP,
                      "no channels configured", group="channels")]

    for platform_name in PLATFORMS:
        cfg = channel_config.platform_config(platform_name)
        prefix = "channels.%s" % platform_name
        if not cfg.get("enabled"):
            out.append(Check("%s.enabled" % prefix, platform_name.title(), SKIP,
                             "disabled", group="channels"))
            continue

        token_field = "bot_token" if platform_name == "discord" else "access_token"
        if not (cfg.get(token_field) or "").strip():
            out.append(Check("%s.token" % prefix, "%s token" % platform_name.title(), FAIL,
                             "enabled but %s is empty" % token_field,
                             "Add it to %s, or set enabled false." % path, "channels"))
            continue

        # The quiet one. Enabled + token + nobody allowed = a bot that
        # connects, appears online, and ignores every message forever.
        reply = cfg.get("reply_allowlist") or []
        dm = cfg.get("dm_allowlist") or []
        if not reply and not dm:
            out.append(Check("%s.allowlist" % prefix, "%s allowlist" % platform_name.title(),
                             FAIL,
                             "enabled with a token, but every allowlist is empty — "
                             "an empty list means DENY, so it will connect and then "
                             "silently ignore everyone including you",
                             "jarvis channels-allow %s reply <your-id>   "
                             "(jarvis channels-whoami prints your id)" % platform_name,
                             "channels"))
        elif not reply:
            out.append(Check("%s.allowlist" % prefix, "%s reply allowlist" % platform_name.title(),
                             WARN,
                             "nobody is in reply_allowlist, so nobody gets an answer "
                             "even if they're allowed to DM",
                             "jarvis channels-allow %s reply <your-id>" % platform_name,
                             "channels"))

        if not (cfg.get("owner") or "").strip():
            out.append(Check("%s.owner" % prefix, "%s owner" % platform_name.title(), WARN,
                             "no owner set — notify_owner and digest DMs have nowhere "
                             "to go on this platform",
                             "jarvis channels-set %s owner <your-id>" % platform_name,
                             "channels"))

        if cfg.get("allow_tools") and "*" in (cfg.get("tool_allowlist") or []):
            out.append(Check("%s.tools" % prefix, "%s tool access" % platform_name.title(),
                             WARN,
                             "tool_allowlist is \"*\" with allow_tools on — anyone who "
                             "can message the bot can run things on this PC",
                             "Replace \"*\" with your own id unless that's deliberate.",
                             "channels"))

        if platform_name == "discord":
            out.extend(_check_discord_extras(cfg))

    return out


def _check_discord_extras(cfg):
    out = []
    if cfg.get("message_content_intent"):
        out.append(Check("channels.discord.intent", "Message Content intent", WARN,
                         "turned on — the bot now receives the text of every message "
                         "in every channel it can see, and Discord will reject the "
                         "connection unless it's also ticked in the Developer Portal",
                         "Set message_content_intent false unless you need passive "
                         "channel reading; mentions and DMs work without it.",
                         "channels"))
    if not (cfg.get("bot_user_id") or "").strip():
        out.append(Check("channels.discord.botid", "Discord bot id", WARN,
                         "not recorded yet — it's filled in on the first successful "
                         "connect, so this usually means the daemon has never "
                         "connected",
                         "jarvis discord-daemon   (watch it for a connect line)",
                         "channels"))
    return out


def check_daemons():
    """Stale pid files are a silent, self-inflicted outage: the daemon
    refuses to start, says a daemon is already running, and no daemon is."""
    out = []
    daemons = [
        ("scheduler", JARVIS_DIR / "sched_daemon.pid", "jarvis sched-daemon"),
        ("discord", JARVIS_DIR / "discord_daemon.pid", "jarvis discord-daemon"),
    ]
    for name, pid_file, start_cmd in daemons:
        if not pid_file.exists():
            out.append(Check("daemon.%s" % name, "%s daemon" % name, SKIP,
                             "not running", start_cmd, "daemons"))
            continue
        try:
            pid = int(pid_file.read_text(encoding=ENCODING).strip())
        except (ValueError, OSError):
            out.append(Check("daemon.%s" % name, "%s daemon" % name, WARN,
                             "pid file is unreadable",
                             "Delete %s and start it again." % pid_file, "daemons"))
            continue
        if _pid_alive(pid):
            out.append(Check("daemon.%s" % name, "%s daemon" % name, OK,
                             "running (pid %d)" % pid, group="daemons"))
        else:
            out.append(Check("daemon.%s" % name, "%s daemon" % name, FAIL,
                             "pid file says %d but nothing is running — a new daemon "
                             "will refuse to start until this is cleared" % pid,
                             "Delete %s, then: %s" % (pid_file, start_cmd), "daemons"))
    return out


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False


def check_scheduler():
    from . import scheduler

    out = []
    try:
        jobs = scheduler.list_jobs()
        all_jobs = scheduler.list_jobs(include_finished=True)
    except Exception as exc:  # noqa: BLE001
        return [Check("sched.load", "Scheduled jobs", FAIL,
                      "couldn't read the job list: %s" % exc,
                      "Check ~/.jarvis/scheduled.json", "scheduler")]

    if not all_jobs:
        return [Check("sched.jobs", "Scheduled jobs", SKIP, "none", group="scheduler")]

    overdue = []
    now = datetime.now()
    for job in jobs:
        if job.get("status") != scheduler.STATUS_PENDING:
            continue
        try:
            due = datetime.fromisoformat(job.get("next_run") or "")
        except (TypeError, ValueError):
            continue
        # An hour late is well past any tick interval — it means nothing is
        # ticking, not that a tick was slow.
        if (now - due).total_seconds() > 3600:
            overdue.append(job)

    out.append(Check("sched.jobs", "Scheduled jobs", OK,
                     "%d active, %d total" % (len(jobs), len(all_jobs)),
                     group="scheduler"))

    if overdue:
        pid_file = JARVIS_DIR / "sched_daemon.pid"
        running = pid_file.exists() and _pid_alive_from(pid_file)
        out.append(Check("sched.overdue", "Overdue jobs", FAIL,
                         "%d job%s past due by over an hour — %s" % (
                             len(overdue), "" if len(overdue) == 1 else "s",
                             "the daemon is running but not firing them"
                             if running else "nothing is ticking the scheduler"),
                         "Check the daemon's output — a job may be failing on every tick."
                         if running else
                         "jarvis sched-tick   (once, to catch up), then leave "
                         "`jarvis sched-daemon` running.",
                         "scheduler",
                         {"jobs": [j.get("id") for j in overdue][:10]}))

    failing = [j for j in all_jobs if (j.get("last_error") or "").strip()]
    if failing:
        out.append(Check("sched.failing", "Jobs with errors", WARN,
                         "%d job%s last ran with an error" % (
                             len(failing), "" if len(failing) == 1 else "s"),
                         "jarvis sched-show <id> to see why; jarvis sched-clear <id> "
                         "to remove it.", "scheduler",
                         {"jobs": [j.get("id") for j in failing][:10]}))
    return out


def _pid_alive_from(pid_file):
    try:
        return _pid_alive(int(pid_file.read_text(encoding=ENCODING).strip()))
    except (ValueError, OSError):
        return False


def check_notifications():
    from . import notifier

    out = []
    try:
        items = notifier._load_inbox()
    except Exception as exc:  # noqa: BLE001
        return [Check("notify.inbox", "Notification inbox", WARN,
                      "unreadable: %s" % exc, group="notify")]
    unread = [i for i in items if not i.get("seen_by")]
    if len(items) >= notifier.MAX_INBOX * 0.9:
        out.append(Check("notify.inbox", "Notification inbox", WARN,
                         "%d items, near the %d cap — the oldest are being dropped"
                         % (len(items), notifier.MAX_INBOX),
                         "jarvis notify-clear   (or open the web UI, which drains it)",
                         "notify"))
    else:
        out.append(Check("notify.inbox", "Notification inbox", OK,
                         "%d stored, %d unread" % (len(items), len(unread)),
                         group="notify"))

    cfg = notifier._load_config()
    if not cfg.get("enabled", True):
        out.append(Check("notify.enabled", "Notifications", WARN,
                         "globally disabled — reminders fire but reach nothing",
                         "Set \"enabled\": true in ~/.jarvis/notify_config.json",
                         "notify"))

    # A channel named in config that isn't a real channel is a typo that
    # silently drops that delivery route.
    for kind, channels in (cfg.get("channels") or {}).items():
        unknown = [c for c in (channels or []) if c not in notifier.CHANNELS]
        if unknown:
            out.append(Check("notify.channels.%s" % kind, "Notify channels (%s)" % kind,
                             WARN,
                             "unknown channel(s): %s" % ", ".join(unknown),
                             "Valid channels: %s" % ", ".join(notifier.CHANNELS),
                             "notify"))
    return out


def check_memory():
    from . import memory

    facts = memory.load_facts()
    out = []
    if not facts:
        out.append(Check("memory.facts", "Long-term memory", SKIP,
                         "nothing saved yet", group="memory"))
        return out

    status = OK
    fix = ""
    detail = "%d fact%s stored" % (len(facts), "" if len(facts) == 1 else "s")
    if len(facts) >= memory.MAX_FACTS * 0.9:
        status = WARN
        detail += " — near the %d cap, oldest facts are dropped on save" % memory.MAX_FACTS
        fix = "jarvis memory-list to review, then memory_forget the stale ones."
    out.append(Check("memory.facts", "Long-term memory", status, detail, fix, "memory"))

    # A fact with no key and no tags can only ever be found by full-text
    # match, which is the case the semantic index exists to rescue.
    unkeyed = [f for f in facts if not (f.get("key") or "").strip() and not (f.get("tags") or [])]
    if len(unkeyed) > max(8, len(facts) // 3):
        out.append(Check("memory.unkeyed", "Unkeyed facts", WARN,
                         "%d of %d facts have neither a key nor tags, so they only "
                         "surface on an exact word match" % (len(unkeyed), len(facts)),
                         "Semantic search covers this: jarvis memory-reindex",
                         "memory"))
    return out


def check_conversations():
    from . import conversations

    out = []
    try:
        index = conversations.list_conversations()
    except Exception as exc:  # noqa: BLE001
        return [Check("conv.index", "Conversation index", FAIL,
                      "unreadable: %s" % exc,
                      "Delete ~/.jarvis/conversations/index.json — it's rebuilt "
                      "from the conversation files.", "conversations")]

    files = list(conversations.CONV_DIR.glob("*.json")) if conversations.CONV_DIR.exists() else []
    files = [f for f in files if f.name != "index.json"]
    out.append(Check("conv.count", "Conversations", OK,
                     "%d indexed, %d file%s on disk" % (len(index), len(files),
                                                        "" if len(files) == 1 else "s"),
                     group="conversations"))
    if len(files) - len(index) > 3:
        out.append(Check("conv.orphans", "Unindexed conversations", WARN,
                         "%d conversation file(s) aren't in the index, so they won't "
                         "show in the sidebar" % (len(files) - len(index)),
                         "Delete ~/.jarvis/conversations/index.json to force a rebuild.",
                         "conversations"))

    total = sum(f.stat().st_size for f in files if f.exists())
    log_dir = JARVIS_DIR / "logs"
    log_bytes = sum(f.stat().st_size for f in log_dir.glob("*") if f.is_file()) \
        if log_dir.exists() else 0
    if log_bytes > 200 * 1024 * 1024:
        out.append(Check("conv.logs", "Log size", WARN,
                         "raw request/response logs are %s" % _human_bytes(log_bytes),
                         "jarvis logs   (the viewer can delete per-conversation logs)",
                         "conversations"))
    else:
        out.append(Check("conv.disk", "Stored size", OK,
                         "%s of conversations, %s of logs"
                         % (_human_bytes(total), _human_bytes(log_bytes)),
                         group="conversations"))
    return out


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def check_tools():
    """Did every actions/*.py file actually load? A rejected action file is
    logged once at import and then invisible forever — the exact failure
    mode actions/_template.py warns about at length."""
    out = []
    try:
        from . import tool_loader
        records = tool_loader.discover_actions(logger=lambda *_a, **_k: None)
    except Exception as exc:  # noqa: BLE001
        return [Check("tools.discovery", "Tool auto-discovery", WARN,
                      "couldn't run: %s" % exc, group="tools")]

    bad = [r for r in records if not r.valid]
    good = [r for r in records if r.valid]
    if bad:
        for rec in bad:
            out.append(Check("tools.rejected.%s" % rec.file, "Action file %s" % rec.file,
                             FAIL, rec.error,
                             "Fix it against jarvis/actions/_template.py — until then "
                             "its tools don't exist anywhere.", "tools"))
    out.append(Check("tools.discovery", "Tool auto-discovery", OK if not bad else WARN,
                     "%d action file%s loaded, %d rejected"
                     % (len(good), "" if len(good) == 1 else "s", len(bad)),
                     group="tools"))

    try:
        from . import tool_registry
        missing = [n for n in tool_registry.TOOL_INDEX if not tool_registry.group_of(n)]
        if missing:
            out.append(Check("tools.ungrouped", "Ungrouped tools", WARN,
                             "%d tool(s) are in no router group, so only search_tools "
                             "can ever reach them" % len(missing),
                             "Add them to tool_registry.TOOL_GROUPS.", "tools"))
    except Exception:  # noqa: BLE001
        pass
    return out


def check_skills():
    try:
        from . import skills
    except Exception:  # noqa: BLE001
        return []
    try:
        listed = skills.list_skills()
    except Exception as exc:  # noqa: BLE001
        return [Check("skills.list", "Skills", WARN, "couldn't read: %s" % exc,
                      group="skills")]
    if not listed:
        return [Check("skills.list", "Skills", SKIP, "none installed", group="skills")]
    broken = [s for s in listed if isinstance(s, dict) and s.get("error")]
    if broken:
        return [Check("skills.list", "Skills", WARN,
                      "%d of %d skill(s) failed to parse" % (len(broken), len(listed)),
                      "Check each SKILL.md has a name and description in its front "
                      "matter.", "skills")]
    return [Check("skills.list", "Skills", OK, "%d installed" % len(listed), group="skills")]


def check_mcp(deep=False):
    try:
        from . import mcp_client
    except Exception:  # noqa: BLE001
        return []
    try:
        configured = (mcp_client.load_config() or {}).get("servers") or {}
        enabled = mcp_client.enabled_servers() or {}
    except Exception:  # noqa: BLE001
        return []
    if not configured:
        return [Check("mcp.servers", "MCP servers", SKIP, "none configured", group="mcp")]
    if not deep:
        return [Check("mcp.servers", "MCP servers", OK,
                      "%d configured, %d enabled (run --deep to test them)"
                      % (len(configured), len(enabled)), group="mcp")]
    if not enabled:
        return [Check("mcp.servers", "MCP servers", SKIP,
                      "%d configured, none enabled" % len(configured),
                      "jarvis mcp-config   to switch one on.", "mcp")]
    out = []
    for name in sorted(enabled):
        try:
            mcp_client.refresh(server=name)
            ok, err = True, ""
        except Exception as exc:  # noqa: BLE001 — a dead server is a finding, not a crash
            ok, err = False, str(exc)
        out.append(Check("mcp.%s" % name, "MCP: %s" % name, OK if ok else FAIL,
                         "responded" if ok else str(err)[:140],
                         "" if ok else "jarvis mcp-status   for the full error.", "mcp"))
    return out


def check_reasoning():
    """Thinking is configured per-provider-family; a level set against a
    family that has no knob is a silent no-op worth naming."""
    from . import ai_config, reasoning

    cfg = ai_config.load_ai_config()
    settings = reasoning.resolve_config(cfg.get("defaults") or {})
    level = settings.get("level", "off")
    if level == "off" and not settings.get("auto", True):
        return [Check("reasoning.level", "Thinking", SKIP,
                      "off, and auto-escalation is disabled",
                      "jarvis think medium   to turn it on.", "reasoning")]

    providers = [p for p in (cfg.get("providers") or [])
                 if isinstance(p, dict) and p.get("enabled")]
    supported = [p for p in providers if reasoning.CAPABILITIES.get(p.get("type"))]
    if providers and not supported:
        return [Check("reasoning.support", "Thinking", WARN,
                      "set to '%s' but no enabled provider supports a thinking knob"
                      % level,
                      "Anthropic, Gemini, Ollama and most OpenAI-compatible hosts do.",
                      "reasoning")]
    detail = "level '%s'%s" % (level, ", auto-escalation on" if settings.get("auto") else "")
    return [Check("reasoning.level", "Thinking", OK, detail, group="reasoning")]


def check_digest():
    try:
        from . import digest
    except Exception:  # noqa: BLE001
        return []
    cfg = digest.load_config()
    if not cfg.get("enabled"):
        return [Check("digest.enabled", "Notification digest", SKIP,
                      "off — every notification is delivered as it happens",
                      "jarvis digest-on daily", "digest")]
    pending = digest.pending_count()
    out = [Check("digest.enabled", "Notification digest", OK,
                 "%s, %d item%s waiting" % (cfg.get("schedule", "daily"), pending,
                                            "" if pending == 1 else "s"),
                 group="digest")]
    if pending and not digest.is_scheduled():
        out.append(Check("digest.scheduled", "Digest delivery", FAIL,
                         "items are being batched but nothing is scheduled to send "
                         "them — they will pile up silently",
                         "jarvis digest-on %s   (re-registers the scheduled job)"
                         % cfg.get("schedule", "daily"), "digest"))
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS = [
    ("runtime", "Runtime", lambda deep: check_runtime()),
    ("binaries", "Binaries on PATH", lambda deep: check_binaries()),
    ("packages", "Python packages", lambda deep: check_python_packages()),
    ("ai", "AI providers", check_ai_config),
    ("reasoning", "Thinking", lambda deep: check_reasoning()),
    ("tools", "Tools", lambda deep: check_tools()),
    ("skills", "Skills", lambda deep: check_skills()),
    ("memory", "Memory", lambda deep: check_memory()),
    ("conversations", "Conversations", lambda deep: check_conversations()),
    ("scheduler", "Scheduler", lambda deep: check_scheduler()),
    ("notify", "Notifications", lambda deep: check_notifications()),
    ("digest", "Digest", lambda deep: check_digest()),
    ("daemons", "Daemons", lambda deep: check_daemons()),
    ("channels", "Chat channels", lambda deep: check_channels()),
    ("playnite", "Playnite", check_playnite),
    ("mcp", "MCP", check_mcp),
]


def run(deep=False, only=None):
    """Run every check (or just the named groups). Returns a report dict.

    `only` takes group ids from _CHECKS — `jarvis doctor channels` is much
    faster than a full run and is what someone actually wants when they're
    iterating on one integration.
    """
    wanted = {g.strip().lower() for g in (only or []) if g and g.strip()}
    checks = []
    for group_id, title, fn in _CHECKS:
        if wanted and group_id not in wanted:
            continue
        checks.extend(_safe(lambda fn=fn: fn(deep), group_id, title, group_id))

    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1

    if counts[FAIL]:
        overall = FAIL
    elif counts[WARN]:
        overall = WARN
    else:
        overall = OK

    return {
        "overall": overall,
        "counts": counts,
        "deep": bool(deep),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "checks": [c.to_dict() for c in checks],
    }


_ICONS = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", SKIP: "--  "}


def render_for_terminal(report, verbose=False):
    """Human-readable report.

    Non-verbose hides `ok`/`skip` lines: a clean run should be three lines,
    not eighty, or nobody reads the one line that matters.
    """
    lines = []
    checks = report.get("checks") or []
    shown = [c for c in checks
             if verbose or c["status"] in (FAIL, WARN)]
    shown.sort(key=lambda c: (_ORDER.get(c["status"], 9), c["group"], c["id"]))

    current_group = None
    for check in shown:
        if check["group"] != current_group:
            current_group = check["group"]
            lines.append("")
            lines.append(current_group.upper())
        lines.append("  [%s] %s — %s" % (_ICONS.get(check["status"], "?"),
                                         check["title"], check["detail"] or ""))
        if check.get("fix") and check["status"] in (FAIL, WARN):
            lines.append("         fix: %s" % check["fix"])

    counts = report.get("counts") or {}
    summary = "%d ok, %d warning%s, %d failure%s, %d skipped" % (
        counts.get(OK, 0),
        counts.get(WARN, 0), "" if counts.get(WARN, 0) == 1 else "s",
        counts.get(FAIL, 0), "" if counts.get(FAIL, 0) == 1 else "s",
        counts.get(SKIP, 0),
    )
    head = {
        OK: "Everything looks healthy.",
        WARN: "Working, with things worth fixing.",
        FAIL: "Something is broken.",
    }.get(report.get("overall"), "")

    if not shown and not verbose:
        return "%s\n%s\n\n(run `jarvis doctor --verbose` to see every check)" % (head, summary)

    lines.append("")
    lines.append(summary)
    lines.append(head)
    if not verbose:
        lines.append("(run `jarvis doctor --verbose` to see passing checks too)")
    if not report.get("deep"):
        lines.append("(run `jarvis doctor --deep` to also test API keys and live "
                     "connections)")
    return "\n".join(lines).lstrip("\n")
