"""Tools Jarvis can call: read-only system info plus run/create/update
user commands from commands.json.

Each tool is a plain Python function that takes no arguments and returns
a JSON-serializable dict \u2014 either the real data, or (never raising)
`{"error": "..."}` explaining what went wrong, so a broken tool degrades
to something Jarvis can honestly relay rather than crashing the ask.

TOOL_SCHEMAS is the provider-agnostic list ai_client.py hands to
ai_providers.py; each adapter there reshapes it into its own provider's
wire format. Keep descriptions specific about *when* to use each one \u2014
that's what steers correct tool selection, per every provider's own
tool-use guidance.
"""

import inspect
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import tool_disable
from .command_tools import COMMAND_TOOL_SCHEMAS, COMMAND_TOOLS
from .custom_tools import CUSTOM_TOOL_SCHEMAS, CUSTOM_TOOLS
from .desktop_tools import DESKTOP_TOOL_SCHEMAS, DESKTOP_TOOLS
from .everything_tools import EVERYTHING_TOOL_SCHEMAS, EVERYTHING_TOOLS
from .file_tools import FILE_TOOL_SCHEMAS, FILE_TOOLS
from .git_tools import GIT_TOOL_SCHEMAS, GIT_TOOLS
from .json_tools import ORGANIZE_JSON_TOOL_SCHEMAS, ORGANIZE_JSON_TOOLS
from .memory import MEMORY_TOOL_SCHEMAS, MEMORY_TOOLS
from .mode_tools import CAPACITY_TOOL_SCHEMAS, CAPACITY_TOOLS
from .ocr_tools import OCR_TOOL_SCHEMAS, OCR_TOOLS
from .pkg_tools import PKG_TOOL_SCHEMAS, PKG_TOOLS
from .playnite_api_tools import PLAYNITE_API_TOOL_SCHEMAS, PLAYNITE_API_TOOLS
from .playnite_tools import PLAYNITE_TOOL_SCHEMAS as _PLAYNITE_CORE_SCHEMAS, PLAYNITE_TOOLS as _PLAYNITE_CORE_TOOLS
from .present_tools import PRESENT_TOOL_SCHEMAS, PRESENT_TOOLS
from .skill_tools import TOOL_SCHEMAS as SKILL_TOOL_SCHEMAS, TOOLS as SKILL_TOOLS
from .radio_tools import RADIO_TOOL_SCHEMAS, RADIO_TOOLS
from .audio_tools import AUDIO_TOOL_SCHEMAS, AUDIO_TOOLS
from .clipboard_tools import CLIPBOARD_TOOL_SCHEMAS, CLIPBOARD_TOOLS
from .vision_tools import VISION_TOOL_SCHEMAS, VISION_TOOLS
from .subagent_tools import SUBAGENT_TOOL_SCHEMAS, SUBAGENT_TOOLS
from .screenshot_tools import SCREENSHOT_TOOL_SCHEMAS, SCREENSHOT_TOOLS
from .spotify_tools import SPOTIFY_TOOL_SCHEMAS, SPOTIFY_TOOLS
from .web_tools import WEB_TOOL_SCHEMAS, WEB_TOOLS
from .ytdl_tools import YTDL_TOOL_SCHEMAS, YTDL_TOOLS
# Appended after the block above rather than alphabetized into it, so this
# stays a self-contained one-line diff hunk (see browser_tools.py, master
# plan Part C) instead of landing in the same hunk as another module's
# import line.
from .browser_tools import BROWSER_TOOL_SCHEMAS, BROWSER_TOOLS

PLAYNITE_TOOL_SCHEMAS = [*_PLAYNITE_CORE_SCHEMAS, *PLAYNITE_API_TOOL_SCHEMAS]
PLAYNITE_TOOLS = {**_PLAYNITE_CORE_TOOLS, **PLAYNITE_API_TOOLS}


def _run(cmd, timeout=5):
    """Run a short-lived subprocess and return its stdout, or None if the
    command doesn't exist, fails, or times out. Never raises."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _fmt_duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _get_datetime():
    now = datetime.now()
    local = time.localtime()
    tzname = time.tzname[1] if local.tm_isdst and time.tzname[1] else time.tzname[0]
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone": tzname,
        "iso": now.isoformat(timespec="seconds"),
    }


def _get_battery():
    try:
        import psutil
    except ImportError:
        return {"error": "psutil isn't installed \u2014 run 'pip install -e .' in jarvis-cli/ to enable this"}
    b = psutil.sensors_battery()
    if b is None:
        return {"has_battery": False, "note": "No battery detected \u2014 likely a desktop (or a VM)."}
    result = {
        "has_battery": True,
        "percent": round(b.percent, 1),
        "plugged_in": bool(b.power_plugged),
    }
    if b.secsleft and b.secsleft > 0 and not b.power_plugged:
        result["time_remaining"] = _fmt_duration(b.secsleft)
    return result


def _get_wifi_info():
    system = platform.system()
    try:
        if system == "Windows":
            out = _run(["netsh", "wlan", "show", "interfaces"])
            if not out:
                return {"connected": False, "note": "couldn't run netsh, or no Wi-Fi adapter present"}
            ssid, signal, state = None, None, None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID") and not line.startswith("BSSID"):
                    ssid = line.split(":", 1)[1].strip()
                elif line.startswith("Signal"):
                    signal = line.split(":", 1)[1].strip()
                elif line.startswith("State"):
                    state = line.split(":", 1)[1].strip()
            if not ssid or (state and "connect" not in state.lower()):
                return {"connected": False}
            result = {"connected": True, "ssid": "[NETWORK_NAME_REDACTED]"}
            if signal:
                result["signal"] = signal
            return result

        if system == "Darwin":
            device = "en0"
            hw = _run(["networksetup", "-listallhardwareports"]) or ""
            lines = hw.splitlines()
            for i, line in enumerate(lines):
                if "Wi-Fi" in line or "AirPort" in line:
                    for follow in lines[i:i + 3]:
                        if follow.strip().startswith("Device:"):
                            device = follow.split(":", 1)[1].strip()
                            break
                    break
            out = _run(["networksetup", "-getairportnetwork", device])
            if not out or "You are not associated" in out:
                return {"connected": False}
            if ":" in out:
                return {"connected": True, "ssid": "[NETWORK_NAME_REDACTED]"}
            return {"connected": False, "note": out.strip()}

        # Linux, and anything else POSIX-ish
        out = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
        if out:
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return {"connected": True, "ssid": "[NETWORK_NAME_REDACTED]"}
            return {"connected": False}
        out = _run(["iwgetid", "-r"])
        if out and out.strip():
            return {"connected": True, "ssid": "[NETWORK_NAME_REDACTED]"}
        return {"connected": False, "note": "couldn't determine Wi-Fi status \u2014 nmcli/iwgetid not found"}
    except Exception as e:
        return {"error": f"couldn't determine Wi-Fi status: {e}"}


# Free, keyless IP geolocation \u2014 no signup, no key, generous rate limit for
# personal use. Note it's plain HTTP, not HTTPS, on the free tier (their
# paid plan adds HTTPS) \u2014 the only thing that travels is a lookup of the
# machine's own already-public IP, but swap the URL for another provider
# here if that matters to you. base_url is a parameter (not hardcoded
# inline) specifically so tests can point it at a local mock instead.
_DEFAULT_GEO_URL = "http://ip-api.com/json/"


def _get_location(base_url=_DEFAULT_GEO_URL):
    try:
        import requests
    except ImportError:
        return {"error": "the 'requests' package isn't installed"}
    try:
        resp = requests.get(
            base_url,
            params={"fields": "status,message,country,regionName,city,lat,lon,timezone,query"},
            timeout=5,
        )
        data = resp.json()
    except Exception as e:
        return {"error": f"couldn't reach the geolocation service: {e}"}
    if data.get("status") != "success":
        return {"error": data.get("message") or "geolocation lookup failed"}
    return {
        "city": "[CITY_REDACTED]",
        "region": "[REGION_REDACTED]",
        "country": data.get("country"),
        "timezone": data.get("timezone"),
        "public_ip": "[IP_REDACTED]",
        "note": "approximate location — city/region/IP redacted for privacy",
    }


def _get_system_info():
    info = {
        "os": platform.system(),
        "os_version": platform.release(),
        "hostname": "[HOSTNAME_REDACTED]",
        "architecture": platform.machine(),
    }
    try:
        import psutil
        info["uptime"] = _fmt_duration(time.time() - psutil.boot_time())
    except ImportError:
        info["uptime"] = None
    return info


def _get_disk_usage():
    try:
        total, used, free = shutil.disk_usage(str(Path.home()))
    except Exception as e:
        return {"error": f"couldn't read disk usage: {e}"}
    gb = 1024 ** 3
    return {
        "total_gb": round(total / gb, 1),
        "used_gb": round(used / gb, 1),
        "free_gb": round(free / gb, 1),
        "percent_used": round(used / total * 100, 1) if total else None,
    }


def _get_memory_usage():
    try:
        import psutil
    except ImportError:
        return {"error": "psutil isn't installed \u2014 run 'pip install -e .' in jarvis-cli/ to enable this"}
    m = psutil.virtual_memory()
    gb = 1024 ** 3
    return {
        "total_gb": round(m.total / gb, 1),
        "used_gb": round((m.total - m.available) / gb, 1),
        "available_gb": round(m.available / gb, 1),
        "percent_used": m.percent,
    }


_NO_PARAMS = {"type": "object", "properties": {}, "required": []}

_SEARCH_TOOLS_CAP = 8


_SEARCH_STOPWORDS = {
    "a", "an", "the", "to", "for", "of", "my", "me", "i", "is", "are", "do",
    "does", "you", "your", "this", "that", "on", "in", "at", "with", "and",
    "or", "please", "can", "could", "would", "what", "how", "check", "get",
    "want", "need", "it", "some", "up", "out", "about", "there", "any",
}


def _search_tokens(text):
    return [t for t in re.findall(r"[a-z0-9']+", text) if t and t not in _SEARCH_STOPWORDS]


def tool_search_tools(args):
    """Phase 5 of the token-optimization plan (see new_plan.md): a small,
    always-available, model-visible discovery tool. When the local router
    (tool_router.route(), Phase 3) isn't confident about a message, ask()
    now offers ONLY this tool instead of falling back to the full catalog
    (that fallback was Phase 1-4's explicitly-documented remaining gap —
    see "Not yet done" in jarvis-token-optimization-phases-1-4.md).

    Returns compact matches (name/group/one-line summary) only — never
    full argument schemas (new_plan.md section 9: a search_tools reply
    that dumped full schemas would just move the token cost, not remove
    it). The full schema for anything matched here is grown into this
    round's active/compact/name_only schemas by the caller
    (ai_client._make_tool_executor's discover_sink), so a match is
    actually callable on the model's next round — not just described and
    then unreachable.
    """
    from . import tool_registry

    raw_query = (args or {}).get("query") or ""
    query = raw_query.strip().lower()
    index = tool_registry.TOOL_INDEX

    if not query:
        # No query: hand back the group list, not every tool — still tiny,
        # still enough to narrow down on the next call.
        off_now = tool_disable.disabled_tools()
        return {
            # A group whose every tool is switched off is not offered either.
            "groups": sorted(g for g, members in tool_registry.TOOL_GROUPS.items()
                             if any(n not in off_now for n in members)),
            "message": "Pass a query (a group name, or a keyword) to see matching tools.",
        }

    # Whole-phrase matches (a bare keyword or group name — the common,
    # cheapest case) still win outright and skip tokenization entirely.
    # But a real query from the model is often a natural phrase ("download
    # youtube video", "take a screenshot") that will almost never appear
    # verbatim in any tool's name or description — requiring the *whole*
    # query string to be a substring silently returned zero matches for
    # exactly those realistic queries. Tokenizing and scoring on overlap
    # fixes that without changing behavior for the simple single-keyword
    # case (a single-token query degrades to the same substring checks).
    tokens = _search_tokens(query)
    if not tokens:
        tokens = [query]

    scored = []
    off = tool_disable.disabled_tools()
    hidden = hidden_from_sender()
    for name, schema in index.items():
        if name in off or name in hidden:
            continue
        group = tool_registry.group_of(name) or "misc"
        keywords = " ".join(tool_registry.keywords_for(name).keys())
        name_lower = name.replace("_", " ").lower()
        haystack = " ".join([
            name_lower,
            schema.get("description") or "",
            keywords,
            group,
        ]).lower()

        if query == group.lower():
            score = 100
        elif query in name.lower():
            score = 80
        elif query in haystack:
            score = 10
        else:
            score = 0
            matched_tokens = 0
            for tok in tokens:
                if tok == group.lower():
                    score += 12
                    matched_tokens += 1
                elif tok in name_lower:
                    score += 9
                    matched_tokens += 1
                elif tok in haystack:
                    score += 3
                    matched_tokens += 1
            if matched_tokens > 1:
                # Small bonus for a tool matching multiple distinct query
                # tokens (e.g. both "youtube" and "download") over one
                # matching only a single generic token.
                score += matched_tokens
            if score <= 0:
                continue
        scored.append((score, name, group, schema))

    if not scored:
        return {
            "matches": [],
            "message": f"No tools matched '{raw_query.strip()}'. Try a broader keyword or a group name.",
        }

    scored.sort(key=lambda x: (-x[0], x[1]))
    truncated = len(scored) > _SEARCH_TOOLS_CAP
    scored = scored[:_SEARCH_TOOLS_CAP]
    matches = [
        {
            "name": name,
            "group": group,
            "summary": _clip_text(schema.get("description") or "", _SCHEMA_DESC_MAX),
        }
        for _, name, group, schema in scored
    ]
    result = {"matches": matches}
    if truncated:
        result["message"] = "More tools matched — narrow the query if you don't see what you need."
    return result


def catalog_schemas_for_prompt(schemas):
    """Tier-1 catalog entries: name + one-line summary, no argument schema.

    This is the "discovery" tier from the Agent Skills progressive-disclosure
    model (see skills-and-token-optimization-research.md section 1), applied
    to tool schemas rather than skill files. A catalog entry costs roughly a
    tenth of a compacted schema, which is what makes offering a 31-tool group
    affordable.

    A tool gets its full schema back via get_tool_schema (below), which grows
    it into the live offering through ai_client's discover_sink — the same
    machinery search_tools already uses. `short_description` is honored when
    a schema declares one, matching stub_schemas()' existing convention.
    """
    stub_params = {"type": "object", "properties": {}}
    out = []
    for schema in schemas or []:
        if not isinstance(schema, dict) or not schema.get("name"):
            continue
        short = schema.get("short_description")
        text = short.strip() if isinstance(short, str) and short.strip() else _clip_text(
            schema.get("description") or "", 64
        )
        out.append({
            "name": schema["name"],
            "description": f"{text} (call get_tool_schema for arguments)",
            "parameters": stub_params,
        })
    return out


def _unknown_tool_hint(name):
    """Shared with tool_get_tool_schema: point an unknown tool name at the
    nearest real ones (did_you_mean) or, failing that, at search_tools —
    instead of a bare error a model has no way to act on (master plan F.2:
    execute_tool's unknown-name path used to skip this, so a model that
    invented a tool name burned a whole extra round finding out, with no
    hint which real tool it meant)."""
    from . import tool_registry

    off = tool_disable.disabled_tools()
    near = sorted(
        n for n in tool_registry.TOOL_INDEX
        if n not in off and (name in n or n in name or n.split("_")[0] == name.split("_")[0])
    )[:5]
    out = {"error": f"no such tool: {name}"}
    if near:
        out["did_you_mean"] = near
    else:
        out["hint"] = "Call search_tools with a keyword to find the right name."
    return out


# Most tools one get_tool_schema call may fetch. A demoted catalog group costs
# one extra model round per lookup (the whole prompt is re-sent); fetching the
# two or three siblings a task needs in ONE call is the point of the list form,
# and the cap keeps a lazy "give me everything" from rebuilding the group the
# catalog tier exists to avoid.
GET_TOOL_SCHEMA_MAX_NAMES = 6


def _one_tool_schema(name):
    """One tool's full-schema reply, or the same refusal/error get_tool_schema
    has always given for that name (disabled, owner-only, unknown)."""
    from . import tool_registry

    if tool_disable.is_tool_disabled(name):
        return tool_disable.refusal_for_tool(name)

    if name in hidden_from_sender():
        return {"error": f"{name} is owner-only and not available in this chat"}

    schema = tool_registry.TOOL_INDEX.get(name)
    if schema is None:
        # A wrong guess shouldn't cost a whole extra round: point at the
        # nearest real names instead of just saying no.
        return _unknown_tool_hint(name)

    return {
        "name": name,
        "group": tool_registry.group_of(name) or "misc",
        "schema": compact_schemas_for_prompt([schema])[0],
        "ready": True,
    }


def tool_get_tool_schema(args):
    """Tier-2 activation: hand back full argument schemas.

    The direct analogue of Anthropic's defer_loading / Tool Search
    fetch-schema step. search_tools answers "does a tool for X exist"; this
    answers "what arguments does the tool I already know the name of take",
    which is the cheaper and far more common question once a catalog entry
    has been seen.

    `name` fetches one tool (the original shape, unchanged). `names` fetches
    several in one call (L.24 T2): the reply is {"schemas": [...], "errors":
    [...], "ready": True}, so a task that needs three siblings pays one round,
    not three.

    Like search_tools, the reply is only half the job: ai_client's executor
    hands the fetched names to the discover sink's exact-promotion path, so
    each tool is genuinely callable on the model's very next reply rather
    than merely described (before L.24 nothing did this, and a tool demoted
    to a catalog line stayed a stub with no arguments).
    """
    args = args or {}
    wanted = []
    raw_list = args.get("names")
    if isinstance(raw_list, (list, tuple)):
        wanted.extend(str(n).strip() for n in raw_list if isinstance(n, str) and n.strip())
    raw = args.get("name")
    single = raw.strip() if isinstance(raw, str) else ""
    if single:
        wanted.insert(0, single)
    seen = set()
    wanted = [n for n in wanted if not (n in seen or seen.add(n))]
    if not wanted:
        return {"error": "Pass the exact name of the tool you want the schema for."}

    if len(wanted) == 1 and not raw_list:
        return _one_tool_schema(wanted[0])

    dropped = wanted[GET_TOOL_SCHEMA_MAX_NAMES:]
    schemas, errors = [], []
    for name in wanted[:GET_TOOL_SCHEMA_MAX_NAMES]:
        got = _one_tool_schema(name)
        if got.get("ready"):
            schemas.append({k: got[k] for k in ("name", "group", "schema")})
        else:
            errors.append(dict(got, name=got.get("name") or name))
    out = {"schemas": schemas, "ready": bool(schemas)}
    if errors:
        out["errors"] = errors
    if dropped:
        out["not_fetched"] = dropped
        out["message"] = (
            f"At most {GET_TOOL_SCHEMA_MAX_NAMES} tools per call — ask again for: "
            + ", ".join(dropped)
        )
    return out


DISCOVERY_TOOL_SCHEMAS = [
    {
        "name": "search_tools",
        "description": (
            "Discover which of Jarvis's tools are relevant right now. Call this FIRST "
            "when you might need a tool but don't see one offered for it — pass a "
            "keyword (e.g. 'spotify', 'battery', 'git') or a group name, or omit the "
            "query to list the groups. Matches become callable on your next reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or group name to search for. Omit to list groups.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_tool_schema",
        "description": (
            "Get the full argument schema for a tool you already know the name of. "
            "Use this when a tool is listed for you without its arguments (its "
            "description says to call this), instead of guessing arguments or "
            "searching again. Need several? Pass them all in `names` in ONE call. "
            "The tools become callable on your next reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact tool name, e.g. 'playnite_launch_game'.",
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Several exact tool names, fetched in one call (max 6).",
                },
            },
            "required": [],
        },
    },
]

# DISCOVERY_TOOL_SCHEMAS alone left the model with no way to tell "is this
# a tool?" from "is this a saved command?" when the router isn't confident
# — a message like "run the tts thing" (a saved command, not a tool) just
# made it call search_tools repeatedly with different keywords and never
# find anything, burning rounds/tokens. search_commands's schema already
# exists (it's part of COMMAND_TOOL_SCHEMAS below); reuse it here instead
# of duplicating it, so both discovery tools are offered together whenever
# the router has no opinion.
_SEARCH_COMMANDS_SCHEMA = next(
    (s for s in COMMAND_TOOL_SCHEMAS if s.get("name") == "search_commands"), None
)
DISCOVERY_AND_COMMANDS_SCHEMAS = (
    [*DISCOVERY_TOOL_SCHEMAS, _SEARCH_COMMANDS_SCHEMA]
    if _SEARCH_COMMANDS_SCHEMA else list(DISCOVERY_TOOL_SCHEMAS)
)

CORE_TOOL_SCHEMAS = [
    {
        "name": "get_datetime",
        "description": "Local date/time/timezone. Use for 'what time is it' or today's date.",
        "parameters": _NO_PARAMS,
    },
    {
        "name": "get_battery",
        "description": "Battery % and charging status. Desktop returns has_battery=false.",
        "parameters": _NO_PARAMS,
    },
    {
        "name": "get_wifi_info",
        "description": "Current Wi-Fi SSID if connected.",
        "parameters": _NO_PARAMS,
    },
    {
        "name": "get_location",
        "description": "Approx city/region from public IP — not GPS.",
        "parameters": _NO_PARAMS,
    },
    {
        "name": "get_system_info",
        "description": "OS, hostname, architecture, uptime.",
        "parameters": _NO_PARAMS,
    },
    {
        "name": "get_disk_usage",
        "description": "Main drive total/used/free space.",
        "parameters": _NO_PARAMS,
    },
    {
        "name": "get_memory_usage",
        "description": "RAM total/used/available.",
        "parameters": _NO_PARAMS,
    },
    *COMMAND_TOOL_SCHEMAS,
    *MEMORY_TOOL_SCHEMAS,
    *CAPACITY_TOOL_SCHEMAS,
    *RADIO_TOOL_SCHEMAS,
    *AUDIO_TOOL_SCHEMAS,
    *CLIPBOARD_TOOL_SCHEMAS,
    *VISION_TOOL_SCHEMAS,
    *SUBAGENT_TOOL_SCHEMAS,
    *GIT_TOOL_SCHEMAS,
    *SCREENSHOT_TOOL_SCHEMAS,
    *DESKTOP_TOOL_SCHEMAS,
    *OCR_TOOL_SCHEMAS,
    *ORGANIZE_JSON_TOOL_SCHEMAS,
    *WEB_TOOL_SCHEMAS,
    *PKG_TOOL_SCHEMAS,
    *FILE_TOOL_SCHEMAS,
    *CUSTOM_TOOL_SCHEMAS,
    *YTDL_TOOL_SCHEMAS,
    *EVERYTHING_TOOL_SCHEMAS,
    *PRESENT_TOOL_SCHEMAS,
    *SKILL_TOOL_SCHEMAS,
    # Appended at the end of this list (not alongside AUDIO_TOOL_SCHEMAS
    # etc. above) so this addition is a self-contained diff hunk — see
    # browser_tools.py, master plan Part C.
    *BROWSER_TOOL_SCHEMAS,
]

PLAYNITE_AND_SPOTIFY = [*PLAYNITE_TOOL_SCHEMAS, *SPOTIFY_TOOL_SCHEMAS]
# search_tools is deliberately NOT part of tool_schemas_for_session()'s
# normal offering (see that function below) — it's the Phase 5 discovery
# tool, offered instead of (not alongside) the full catalog. It IS part of
# TOOL_SCHEMAS/TOOL_INDEX so tool_registry.TOOL_INDEX, schemas_for_tools(),
# and the tool executor's arg-validation all know about it.
TOOL_SCHEMAS = [*CORE_TOOL_SCHEMAS, *PLAYNITE_AND_SPOTIFY, *DISCOVERY_TOOL_SCHEMAS]


# L.28 (guests with tools): tools whose handler refuses anyone but the owner
# (they read or write OTHER people's private DMs). A chat guest who is allowed
# tools was still offered them — ~2 schemas of tokens on every round — and
# search_tools listed them, so a guest's "dm ..." request cost rounds on calls
# that are guaranteed to be refused. They are now simply not OFFERED to a
# non-owner chat sender. Execution is untouched: the handlers still do their
# own owner check (that is the real gate, and tests/test_send_dm.py pins it),
# and TOOLS / tool_safety / confirmation are not involved at all.
OWNER_ONLY_TOOLS = frozenset({"send_dm", "recent_dms"})


def _chat_sender_is_guest():
    """True only when this process is answering a chat message from someone
    who is NOT the owner. No chat context (the PC, a scheduled job) is the
    owner's own trusted context, same convention as the handlers."""
    raw = os.environ.get("JARVIS_CHANNEL_SENDER") or ""
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict) or not data.get("platform") or not data.get("user_id"):
        return False
    return not data.get("is_owner")


def hidden_from_sender():
    """Tool names that must not be offered to / listed for the current sender."""
    return OWNER_ONLY_TOOLS if _chat_sender_is_guest() else frozenset()


def allowed_tools_from_env():
    """None means unrestricted (normal CLI). A set — even empty — is an allowlist
    used by the KDE Connect plugin via JARVIS_ALLOWED_TOOLS."""
    raw = os.environ.get("JARVIS_ALLOWED_TOOLS")
    if raw is None:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


_SCHEMA_DESC_MAX = 90


def _clip_text(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _compact_json_schema(schema):
    """Keep types/enums/required; drop verbose property descriptions."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key in ("type", "enum", "required", "minimum", "maximum", "default"):
        if key in schema:
            out[key] = schema[key]
    items = schema.get("items")
    if isinstance(items, dict):
        out["items"] = _compact_json_schema(items)
    props = schema.get("properties")
    if isinstance(props, dict):
        out["properties"] = {name: _compact_json_schema(prop) for name, prop in props.items()}
    elif "type" not in out and schema:
        # unknown shape — keep a shallow copy without description
        return {k: v for k, v in schema.items() if k != "description"}
    if "type" not in out and "properties" in out:
        out["type"] = "object"
    return out


def name_only_schemas_for_prompt(schemas):
    """Advertise tools by name (and a one-line hint) with no argument schema.

    The model learns parameters on first use: if required args are missing,
    the executor returns the compact summary instead of running the tool.

    A schema can set `short_description` to hand-write its 50% Capacity
    hint instead of having the full description blindly clipped to 48
    chars mid-sentence \u2014 useful for tools (like search_files) whose full
    description is long and front-loads detail that doesn't survive a hard
    clip. Falls back to clipping `description` when absent.
    """
    stub_params = {"type": "object", "properties": {}}
    out = []
    for schema in schemas or []:
        if not isinstance(schema, dict) or not schema.get("name"):
            continue
        short = schema.get("short_description")
        text = short.strip() if isinstance(short, str) and short.strip() else _clip_text(schema.get("description") or "", 48)
        out.append({
            "name": schema.get("name"),
            "description": text,
            "parameters": stub_params,
        })
    return out


def compact_schemas_for_prompt(schemas):
    """Shorter tool JSON for the model. Full schemas stay in tools-list / KDE.

    A schema can set `compact_description` to hand-write its 100% Capacity
    text instead of having `description` clipped to _SCHEMA_DESC_MAX chars
    \u2014 same reasoning as short_description above, just less aggressive.
    """
    out = []
    for schema in schemas or []:
        if not isinstance(schema, dict):
            continue
        compact = schema.get("compact_description")
        text = compact.strip() if isinstance(compact, str) and compact.strip() else _clip_text(schema.get("description") or "", _SCHEMA_DESC_MAX)
        item = {
            "name": schema.get("name"),
            "description": text,
        }
        params = schema.get("parameters")
        item["parameters"] = _compact_json_schema(params) if isinstance(params, dict) else params
        out.append(item)
    return out


def tool_schemas_for_session():
    """Playnite tools only when configured. Spotify is always offered so the
    model can search/play or get a login prompt — advertising spotify_* in
    the prompt without listing them makes Groq HTTP 400 (tool not in request)."""
    from . import playnite_config

    out = list(CORE_TOOL_SCHEMAS)
    out.extend(SPOTIFY_TOOL_SCHEMAS)
    if playnite_config.is_configured():
        out.extend(PLAYNITE_TOOL_SCHEMAS)
    # Auto-discovered tools (jarvis/actions/*.py) ride along in the full
    # fallback catalog too, not just the router-confident path — otherwise
    # they'd vanish whenever route is None (router disabled) while every
    # built-in tool stayed put.
    out.extend(AUTO_TOOL_SCHEMAS)
    allowed = allowed_tools_from_env()
    if allowed is not None:
        out = [schema for schema in out if schema.get("name") in allowed]
    # Tools the owner switched off in the Tool Manager are never offered. The
    # tools themselves stay in TOOLS (AGENTS.md); this only decides what the
    # model is shown. See tool_disable.py.
    off = tool_disable.disabled_tools()
    if off:
        out = [schema for schema in out if schema.get("name") not in off]
    return out


_TOOL_INDEX = None


def _tool_index():
    """name -> full schema, built once from TOOL_SCHEMAS (the full catalog,
    same source tools_list_payload uses) and cached. Private: use
    schemas_for_tools() below rather than reaching into this directly."""
    global _TOOL_INDEX
    if _TOOL_INDEX is None:
        _TOOL_INDEX = {}
        for schema in TOOL_SCHEMAS:
            name = schema.get("name")
            if name and name not in _TOOL_INDEX:
                _TOOL_INDEX[name] = schema
    return _TOOL_INDEX


def schemas_for_tools(names):
    """Phase 2 of the token-optimization plan (see new_plan.md): return only
    the full schemas for the given tool names, in the order `names` is
    given, dropping any name that isn't a real tool.

    Not called from ask() yet — tool_schemas_for_session() below still
    returns everything, exactly as before. This is purely the building
    block a later phase (once tool_router.py picks a small set of relevant
    tool names) will filter down to before handing schemas to a provider.

    Compatibility check: schemas_for_tools([s["name"] for s in
    tool_schemas_for_session()]) must return the exact same list
    tool_schemas_for_session() does (same names, same schemas) — see
    tests/test_schemas_for_tools.py.
    """
    index = _tool_index()
    off = tool_disable.disabled_tools()
    hidden = hidden_from_sender()
    out = []
    seen = set()
    for name in names or []:
        if name in seen or name in off or name in hidden:
            continue
        schema = index.get(name)
        if schema is not None:
            out.append(schema)
            seen.add(name)
    return out


def _tool_group(name):
    """The router group a tool belongs to ("" when it has none) — the Tool
    Manager groups its list by this. Never raises."""
    try:
        from . import tool_registry
        return tool_registry.group_of(name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _tool_file(name):
    """The tool file a discovered tool came from ("scheduler_tools.py"), or ""
    for a hand-wired built-in. The Tool Manager shows it so a person can tell
    which file to open or switch off. Never raises."""
    try:
        for record in _AUTO_RECORDS:
            if record.valid and name in record.tools:
                return os.path.basename(str(record.file or ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


_MCP_TAG_RE = re.compile(r"^\[MCP:\s*([^\]]+)\]")


def _tool_origin(name, source, file, description):
    """One human sentence for "where does this tool come from", shown in the Tool
    Manager's Source column: the file for a shipped or user tool, the server for an
    MCP one. Display only -- nothing reads it back. Never raises."""
    try:
        if source == "mcp":
            m = _MCP_TAG_RE.match(description or "")
            return "MCP server: %s" % m.group(1).strip() if m else "MCP (built-in helper)"
        if source == "user":
            return "Your file: ~/.jarvis/tools/%s" % file if file else "Your file in ~/.jarvis/tools/"
        if source == "auto":
            return "Ships with Jarvis: actions/%s" % file if file else "Ships with Jarvis (actions/)"
        return "Built in to Jarvis (tools.py)"
    except Exception:  # noqa: BLE001
        return ""


def _builtin_module_label(name):
    """The jarvis/ module a hand-wired built-in's handler is defined in, as a
    path-like label ("screenshot_tools.py", "channels/outbound.py").

    Read from the handler's own `__module__` (the plan's L.18: "the Python
    module for built-in tools"), looking through `functools.partial` and
    `functools.wraps` wrappers first so a wrapped handler still reports the
    module that wrote it. Anything that does not resolve to a module inside the
    jarvis package (a lambda from another library, a C builtin, no handler at
    all) falls back to "tools.py" -- the file the hand-wiring itself lives in --
    rather than showing a stdlib module name as a "source". Never raises."""
    try:
        fn = TOOLS.get(name)
        for _ in range(4):
            inner = getattr(fn, "__wrapped__", None) or getattr(fn, "func", None)
            if inner is None or not callable(inner):
                break
            fn = inner
        mod = str(getattr(fn, "__module__", "") or "")
        if mod.startswith("jarvis."):
            mod = mod[len("jarvis."):]
        elif mod != "tools":
            mod = ""
        return (mod.replace(".", "/") if mod else "tools") + ".py"
    except Exception:  # noqa: BLE001
        return "tools.py"


def _tool_source_detail(name, source, file, description):
    """L.18 -- WHERE a tool is loaded from, finer than the four `source` buckets.

    Returns {kind, module, label} plus `path` (user files) or `server` (MCP):

      kind    "module"  a hand-wired built-in; `module` is the jarvis/ file
              "file"    a discovered tool file, shipped (actions/) or yours
                        (~/.jarvis/tools/); `module` is the file's name
              "server"  an MCP tool; `module` is the actions/ file that carries
                        it and `server` the configured server's name
      label   what the Debug panel shows and filters on. Distinct across kinds
              by construction ("tools.py" / "actions/x.py" /
              "~/.jarvis/tools/x.py" / "MCP: name"), so it doubles as the key.

    This is a tool's SOURCE, not its group, name prefix, checklist section or
    safety flags -- those are separate fields and stay separate. It is derived
    from where discovery found the tool, never declared by the tool itself.
    `source` is untouched; this is additive. Never raises.
    """
    try:
        if source == "mcp":
            m = _MCP_TAG_RE.match(description or "")
            server = m.group(1).strip() if m else ""
            return {
                "kind": "server",
                "module": file or "",
                "server": server,
                "label": ("MCP: %s" % server) if server else "MCP: built-in helper",
            }
        if source == "user":
            base = file or ""
            detail = {"kind": "file", "module": base,
                      "label": ("~/.jarvis/tools/%s" % base) if base else "~/.jarvis/tools/"}
            user_dir = globals().get("_USER_DIR")
            if user_dir and base:
                detail["path"] = os.path.join(str(user_dir), base)
            return detail
        if source == "auto":
            base = file or ""
            return {"kind": "file", "module": base,
                    "label": ("actions/%s" % base) if base else "actions/"}
        label = _builtin_module_label(name)
        return {"kind": "module", "module": label, "label": label}
    except Exception:  # noqa: BLE001
        return {"kind": "module", "module": "", "label": "tools.py"}


def tools_list_payload():
    """Full catalog for remote permission UIs — not filtered by session or env.

    Includes the full (uncompacted) parameter schema for each tool so a
    remote debug/permission UI can render argument inputs and docs without
    guessing — this is the same schema the model itself receives. Also
    includes each tool's current confirm_required/ai_review/approval_summary
    safety flags (see tool_safety.py), its router `group`, whether the owner
    has switched it off (`disabled`, tool_disable.py) and, for the few tools that
    can't be switched off, why (`protected`) so the debug dashboard's toggles reflect real
    state, not a hardcoded guess.

    Additive (L.18): every item carries `source_detail` -- {kind, module, label,
    path? | server?} -- naming the module / file / MCP server the tool is loaded
    from, for the Debug panel's finer source filter. `source` (the four coarse
    buckets) is unchanged. See _tool_source_detail().

    Additive, only when present (G.1, extended by G.2): a tool whose own
    module supplied a Test Checklist entry carries it as `checklist` (same
    shape as the shipped web/public/test-checklist-data.js entries, `group`
    filled in — that tool's own section id, whether its module used the
    single-group or multi-group TEST_CHECKLIST_GROUP shape), plus
    `checklist_group` ({label, blurb}) for THAT entry's own group when its
    module named one. Two tools from the same multi-group module can carry
    different `checklist_group` values, one per section. The Test Checklist
    panel merges these into its shipped catalogue, per tool, so nothing here
    changes about how the panel folds them in. Every other consumer ignores
    unknown keys, and the payload stays a plain list.
    """
    from . import tool_safety

    off = tool_disable.disabled_tools()      # read once, not once per tool
    items = []
    seen = set()
    for schema in TOOL_SCHEMAS:
        name = schema.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        flags = tool_safety.get_flags(name)
        item = {
            "name": name,
            "description": schema.get("description") or "",
            "parameters": schema.get("parameters") or _NO_PARAMS,
            "confirm_required": flags["confirm_required"],
            "ai_review": flags["ai_review"],
            "approval_summary": flags["approval_summary"],
            "defaults": tool_safety.default_flags(name),
            "group": _tool_group(name),
            "file": _tool_file(name),
            "source": _tool_source(name),
            "disabled": name in off,
            "protected": tool_disable.protected_reason(name) or "",
        }
        item["origin"] = _tool_origin(name, item["source"], item["file"], item["description"])
        item["source_detail"] = _tool_source_detail(name, item["source"], item["file"], item["description"])
        supplied = AUTO_TEST_CHECKLIST.get(name)
        if supplied:
            item["checklist"] = supplied
            group_meta = AUTO_TEST_CHECKLIST_GROUPS.get(supplied.get("group"))
            if group_meta:
                item["checklist_group"] = group_meta
        items.append(item)
    for name in TOOLS:
        if name not in seen:
            seen.add(name)
            flags = tool_safety.get_flags(name)
            items.append({
                "name": name,
                "description": "",
                "parameters": _NO_PARAMS,
                "confirm_required": flags["confirm_required"],
                "ai_review": flags["ai_review"],
                "approval_summary": flags["approval_summary"],
                "defaults": tool_safety.default_flags(name),
                "group": _tool_group(name),
                "file": _tool_file(name),
                "source": _tool_source(name),
                "disabled": name in off,
                "protected": tool_disable.protected_reason(name) or "",
            })
            items[-1]["origin"] = _tool_origin(name, items[-1]["source"], items[-1]["file"], "")
            items[-1]["source_detail"] = _tool_source_detail(name, items[-1]["source"], items[-1]["file"], "")
    return items


def _tool_source(name):
    """§3 — classify a tool name into the Debug-menu source buckets.

    Resolved lazily (called from tools_list_payload(), never at import
    time) because USER_TOOL_NAMES / _MCP_TOOL_NAMES / _SHIPPED_AUTO_TOOL_NAMES
    are only populated later in this module, once the two discover_actions()
    scans below have run. Order matters and mirrors the discovery/collision
    rules above: a name in USER_TOOL_NAMES came from ~/.jarvis/tools (it
    cannot also be MCP, shipped-auto or built-in — collisions there are
    rejected at discovery time); a name in _MCP_TOOL_NAMES came from
    actions/mcp_tools.py's TOOL_GROUP == "mcp" (mcp_<server>_<tool> plus the
    always-present mcp_list_servers); anything else left in AUTO_TOOL_SCHEMAS
    came from the rest of the shipped jarvis/actions/ scan; everything left
    over is a built-in hand-wired in TOOL_SCHEMAS/TOOLS above.

    §3 item 2 — MCP bucket. actions/mcp_tools.py rides the same
    jarvis/actions/ auto-discovery path as any other shipped tool module
    (see its own docstring for why: no changes needed to tools.py or
    tool_loader.py to plug a foreign protocol in), so without this branch
    every mcp_* tool would silently land in "auto" — indistinguishable from
    a first-party shipped tool in the Debug panel, which defeats the point
    of a source filter once a server is actually configured. _MCP_TOOL_NAMES
    below is keyed off the discovered record's own `.group`, not a name
    prefix match, so it stays correct even if a future MCP-adjacent tool
    doesn't follow the `mcp_` naming convention.
    """
    if name in USER_TOOL_NAMES:
        return "user"
    if name in _MCP_TOOL_NAMES:
        return "mcp"
    if name in _SHIPPED_AUTO_TOOL_NAMES:
        return "auto"
    return "builtin"

TOOLS = {
    "get_datetime": _get_datetime,
    "get_battery": _get_battery,
    "get_wifi_info": _get_wifi_info,
    "get_location": _get_location,
    "get_system_info": _get_system_info,
    "get_disk_usage": _get_disk_usage,
    "get_memory_usage": _get_memory_usage,
    **COMMAND_TOOLS,
    **MEMORY_TOOLS,
    **CAPACITY_TOOLS,
    **RADIO_TOOLS,
    **AUDIO_TOOLS,
    **CLIPBOARD_TOOLS,
    **VISION_TOOLS,
    **SUBAGENT_TOOLS,
    **GIT_TOOLS,
    **SCREENSHOT_TOOLS,
    **DESKTOP_TOOLS,
    **OCR_TOOLS,
    **ORGANIZE_JSON_TOOLS,
    **WEB_TOOLS,
    **PKG_TOOLS,
    **PLAYNITE_TOOLS,
    **SPOTIFY_TOOLS,
    **FILE_TOOLS,
    **CUSTOM_TOOLS,
    **YTDL_TOOLS,
    **EVERYTHING_TOOLS,
    **PRESENT_TOOLS,
    "search_tools": tool_search_tools,
    "get_tool_schema": tool_get_tool_schema,
    **SKILL_TOOLS,
    # Appended at the end of this dict for the same reason as the
    # CORE_TOOL_SCHEMAS addition above — see browser_tools.py.
    **BROWSER_TOOLS,
}

# ---------------------------------------------------------------------------
# Auto-discovered tools — jarvis/actions/*.py (see tool_loader.py). Adding a
# new tool no longer requires touching this file: drop a module in actions/
# exposing TOOL_SCHEMAS/TOOLS/TOOL_GROUP (+ TOOL_KEYWORDS to be reachable
# through tool_router.route() the same way every built-in tool is — see
# actions/_template.py). Every manually-wired tool above is untouched by
# this; it's a second, additive path into the same catalog, not a
# replacement for the manual imports at the top of this file.
#
# reserved_names is computed from TOOLS *before* this block runs, so a
# collision between an action file and a built-in tool name is rejected —
# the built-in always wins and the action file is logged and skipped
# rather than silently shadowing something real.
# ---------------------------------------------------------------------------
import sys  # noqa: E402  (deliberately after TOOLS is built, see below)
from . import tool_loader  # noqa: E402  (deliberately after TOOLS is built)
from . import tool_ui as tool_ui_module  # noqa: E402  (import-light; used by tool_ui_payload below)

# logger=print (the default) would write auto-discovery log lines to
# stdout. That's fine for most invocations, but `jarvis tools-list`
# imports this module and then prints exactly one JSON payload to stdout
# for the web UI to JSON.parse — any stray print() before that call
# corrupts it. Every other diagnostic/log line in this codebase goes to
# stderr for the same reason (see cli.py); auto-discovery logging should
# not be the one exception.
_AUTO_RECORDS = tool_loader.discover_actions(
    reserved_names=set(TOOLS),
    logger=lambda msg: print(msg, file=sys.stderr),
)

# ---------------------------------------------------------------------------
# User-authored tools (~/.jarvis/tools/*.py) — the SAME contract and the same
# loader, just a different directory.
#
# Why a second directory at all: this one survives a reinstall. script.bat
# rebuilds and overwrites the install tree, so anything a user wrote into
# jarvis/actions/ is silently destroyed by the next build. ~/.jarvis is the
# directory this project already promises never to move (see script.bat's own
# comment about it), so that's where a user's own work belongs — next to
# their commands, memory and skills.
#
# Scanned SECOND and with the built-ins plus every actions/ name already
# reserved, so a shipped tool always wins a name collision. That ordering is
# deliberate: a jarvis update that adds a tool named the same as one of
# yours should disable yours with a logged rejection, not silently shadow the
# built-in and change what an existing prompt does.
#
# Failures here are logged and skipped exactly like actions/ — one broken
# user file can't take down discovery, let alone the CLI.
# ---------------------------------------------------------------------------
try:
    from . import custom_tools_store as _custom_store

    _USER_DIR = _custom_store.ensure_dir()
    _reserved_after_actions = set(TOOLS) | {
        name for _r in _AUTO_RECORDS if _r.valid for name in _r.tools
    }
    _user_scan_start = len(_AUTO_RECORDS)
    _AUTO_RECORDS = _AUTO_RECORDS + tool_loader.discover_actions(
        actions_dir=_USER_DIR,
        reserved_names=_reserved_after_actions,
        logger=lambda msg: print(msg, file=sys.stderr),
    )
    # Names contributed by the user directory specifically. custom_tools_store
    # needs this to answer "does this name collide with something that already
    # exists?" correctly: without it, validating an ALREADY-SAVED user tool
    # finds its own name in the live catalog and reports the file as clashing
    # with itself — so a tool would save fine once and then show as broken
    # forever afterward.
    USER_TOOL_NAMES = {
        name
        for _r in _AUTO_RECORDS[_user_scan_start:]
        if _r.valid
        for name in _r.tools
    }
except Exception as _e:  # noqa: BLE001 — a missing/unreadable ~/.jarvis/tools is normal
    print("[tools] user tool directory skipped: %s" % _e, file=sys.stderr)
    USER_TOOL_NAMES = set()

_AUTO_VALID = [r for r in _AUTO_RECORDS if r.valid]

AUTO_TOOL_SCHEMAS = [s for r in _AUTO_VALID for s in r.schemas]
AUTO_TOOLS = {name: fn for r in _AUTO_VALID for name, fn in r.tools.items()}

# §3 — names contributed by the SHIPPED jarvis/actions/ scan only. This is
# deliberately a set difference (AUTO_TOOLS' names minus USER_TOOL_NAMES)
# rather than slicing _AUTO_VALID by _user_scan_start: _AUTO_VALID has
# already dropped invalid records, so a positional split at that index
# would land on the wrong boundary once any record — shipped or user — is
# invalid. Since a user-authored name can never collide with a shipped one
# (discover_actions rejects that at scan time — see the block below), plain
# subtraction gives the same partition without depending on record order.
_SHIPPED_AUTO_TOOL_NAMES = set(AUTO_TOOLS) - USER_TOOL_NAMES

# §3 item 2 — names contributed specifically by actions/mcp_tools.py (its
# TOOL_GROUP is "mcp"), a subset of _SHIPPED_AUTO_TOOL_NAMES above. Pulled
# out into its own bucket so the Debug source filter can offer "mcp"
# distinctly from "auto" — see _tool_source() below. A user-authored tool
# can never land here: discover_actions() scans jarvis/actions/ (shipped)
# before ~/.jarvis/tools/ (user), so a name from the second scan is already
# excluded by the USER_TOOL_NAMES subtraction above, and _AUTO_VALID's
# records from the user scan carry whatever TOOL_GROUP that file declared,
# not "mcp", unless a user literally names their own group "mcp" — in which
# case treating it as the MCP bucket is arguably still correct, since
# USER_TOOL_NAMES is checked first in _tool_source() and always wins.
_MCP_TOOL_NAMES = {
    name
    for _r in _AUTO_VALID
    if _r.group == "mcp"
    for name in _r.tools
}

# group -> [tool names]; tool_registry.py merges this into its own
# TOOL_GROUPS so auto-discovered tools join the router exactly like a
# built-in tool would.
AUTO_TOOL_GROUPS = {}
for _r in _AUTO_VALID:
    AUTO_TOOL_GROUPS.setdefault(_r.group, []).extend(sorted(_r.tools))

# name -> {phrase: weight}; tool_registry.py merges this into TOOL_KEYWORDS.
AUTO_TOOL_KEYWORDS = {}
for _r in _AUTO_VALID:
    AUTO_TOOL_KEYWORDS.update(_r.keywords)

# group -> instruction; tool_registry.py only fills in groups that don't
# already have a curated instruction, so an action file joining an
# existing group can never silently override its established guidance.
AUTO_TOOL_PACK_INSTRUCTIONS = {r.group: r.pack_instruction for r in _AUTO_VALID if r.pack_instruction}

# Test Checklist entries tool modules supplied themselves (TEST_CHECKLIST /
# TEST_CHECKLIST_GROUP — master plan G.1, extended by G.2, see tool_loader.py
# and checklist_schema.py). Already validated and normalised by the loader.
#   name     -> entry, "group" filled in (the module's TOOL_GROUP, or — under
#               the multi-group shape — whichever of the module's own group
#               ids that entry named)
#   group id -> {"label", "blurb"}; first module to name a group wins, and
#               actions/ is scanned before ~/.jarvis/tools/, so a shipped label
#               can't be replaced by a user's file. A module under the
#               multi-group shape contributes one id per group it named, not
#               just its own TOOL_GROUP.
# Read by tools_list_payload() below, which is how the web panel sees them —
# there is no other route.
AUTO_TEST_CHECKLIST = {}
AUTO_TEST_CHECKLIST_GROUPS = {}
for _r in _AUTO_VALID:
    AUTO_TEST_CHECKLIST.update(_r.checklist)
    for _gid, _meta in _r.checklist_group.items():
        AUTO_TEST_CHECKLIST_GROUPS.setdefault(_gid, _meta)

TOOLS = {**TOOLS, **AUTO_TOOLS}
TOOL_SCHEMAS = [*TOOL_SCHEMAS, *AUTO_TOOL_SCHEMAS]

# Best-effort merges into tool_safety.py / tool_result_shaping.py's
# opt-in dicts, so TOOL_CONFIRM_REQUIRED / TOOL_AI_REVIEW / TOOL_RESULT_SPECS
# in an action file (see actions/_template.py) work the same one-file way —
# neither module imports tools.py, so this has to run from here. Wrapped so
# a problem merging safety/shaping defaults can never take tool discovery
# itself down.
try:
    from . import tool_safety as _tool_safety
    from . import tool_result_shaping as _tool_result_shaping
    for _r in _AUTO_VALID:
        _tool_safety.DEFAULT_CONFIRM_REQUIRED.update(_r.confirm_required)
        _tool_safety.DEFAULT_AI_REVIEW.update(_r.ai_review)
        _tool_result_shaping.TOOL_RESULT_SPECS.update(_r.result_specs)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Auto-discovered PERSONAS — same two directories (jarvis/actions/, then
# ~/.jarvis/tools/) and the same _AUTO_RECORDS list as the tools above, just
# a different optional attribute on each module (see persona_registry.py
# and actions/_template.py §7). Scanned across EVERY record regardless of
# `.valid` — a persona-only file is deliberately not a valid tool file (see
# tool_loader._validate's "__not_an_action__" branch) but its personas are
# still real. First registration of a given id wins (built-in actions/ is
# scanned before ~/.jarvis/tools/, matching the same "shipped beats
# user-authored on a name collision" rule tool names already follow); any
# later duplicate is logged and dropped rather than silently overriding the
# first.
# ---------------------------------------------------------------------------
AUTO_PERSONAS = []
_seen_persona_ids = {}
for _r in _AUTO_RECORDS:
    for _p in _r.personas:
        _pid = _p["id"]
        if _pid in _seen_persona_ids:
            print(
                f"[personas] Duplicate persona id {_pid!r} in {_r.file} — "
                f"already registered by {_seen_persona_ids[_pid]}, skipped.",
                file=sys.stderr,
            )
            continue
        _seen_persona_ids[_pid] = _r.file
        AUTO_PERSONAS.append(_p)

# id -> {label, full, compact} — every custom attitude any registered
# persona defined inline (see persona_registry._resolve_attitude). Merged
# into ai_client.ATTITUDE_PRESETS at import time there, so a persona's
# custom attitude actually renders in the system prompt the same way a
# built-in one does, not just in the Skin modal's dropdown.
AUTO_ATTITUDES = {}
for _p in AUTO_PERSONAS:
    _ca = _p.get("custom_attitude")
    if _ca:
        AUTO_ATTITUDES[_ca["id"]] = {"label": _ca["label"], "full": _ca["full"], "compact": _ca["compact"]}


def personas_list_payload(include_disabled=False):
    """Full catalog for the web UI's Skin modal (see `jarvis personas-list`
    / GET /api/personas). Every persona here is already fully validated and
    logo-resolved (PNGs are already base64 data URIs) — the web UI can
    render this straight through with no further backend round trip.

    A persona the owner switched off in the Tool Manager (tool_disable.py) is
    left out, so the Skin modal never offers it. The Tool Manager itself passes
    `include_disabled=True` (`personas-list --all`) to list every persona with a
    `disabled` flag, so there is something to switch back on. Entries are copied
    when flagged -- AUTO_PERSONAS is shared state and must not grow a key."""
    off = tool_disable.disabled_personas()
    if include_disabled:
        personas = [{**p, "disabled": p.get("id") in off, "file": _PERSONA_FILES.get(p.get("id"), "")}
                    for p in AUTO_PERSONAS]
    else:
        personas = [p for p in AUTO_PERSONAS if p.get("id") not in off]
    # `disabled_ids` / `disabled_skins` are the WHOLE off lists, not just the
    # registered personas': the five built-in personas and the accent swatches /
    # themes are drawn by the browser, which has no other way to learn they are off.
    return {"personas": personas, "attitudes": AUTO_ATTITUDES,
            "disabled_ids": sorted(off), "disabled_skins": sorted(tool_disable.disabled_skins())}


# ---------------------------------------------------------------------------
# Auto-discovered TOOL_UI elements — a button that opens a tool's own page, or a
# Menu entry that opens its panel (see tool_ui.py, actions/_template.py section 9).
# Same two directories and the same _AUTO_RECORDS as everything above; only a
# record that loaded as a valid tool file can carry any. First registration of an
# id wins (shipped actions/ is scanned before ~/.jarvis/tools/, so a user's file
# can't replace a shipped element), a later duplicate is logged and dropped.
# ---------------------------------------------------------------------------
AUTO_UI = []
_seen_ui_ids = {}
for _r in _AUTO_VALID:
    for _u in _r.ui:
        if _u["id"] in _seen_ui_ids:
            print(
                f"[tool-ui] Duplicate UI element id {_u['id']!r} in {_r.file} — "
                f"already registered by {_seen_ui_ids[_u['id']]}, skipped.",
                file=sys.stderr,
            )
            continue
        _seen_ui_ids[_u["id"]] = _r.file
        AUTO_UI.append(_u)

# persona id -> the file that registered it, for the Tool Manager's "where does this come from".
_PERSONA_FILES = {_p["id"]: _seen_persona_ids.get(_p["id"], "") for _p in AUTO_PERSONAS}


def tool_ui_payload(include_disabled=False):
    """What `jarvis tool-ui list` / GET /api/tool-ui return: every UI element a tool
    file shipped, without absolute paths. The browser asks for the default (switched-
    off ones left out) to build its buttons and Menu entries; the Tool Manager asks
    for `include_disabled=True` so it can list them all with a `disabled` flag."""
    off = tool_disable.disabled_ui()
    items = [tool_ui_module.public(u, disabled=u["id"] in off) for u in AUTO_UI]
    if not include_disabled:
        items = [u for u in items if not u["disabled"]]
    return {"ui": items}


def tool_ui_bundle(ui_id):
    """The html / js / css of one element, or {"ok": False, ...}. Refuses an element
    the owner switched off and one that doesn't exist (the browser never gets to
    ask for an arbitrary path: it names an id, the id names a declared folder)."""
    ui_id = (ui_id or "").strip()
    entry = next((u for u in AUTO_UI if u["id"] == ui_id), None)
    if entry is None:
        return {"ok": False, "error": "no UI element called %r" % ui_id}
    if tool_disable.is_ui_disabled(ui_id):
        return {"ok": False, "disabled": True, "error": "%s is switched off in the Tool Manager" % ui_id}
    return tool_ui_module.read_bundle(entry)


@dataclass
class ToolContext:
    """Optional second argument a tool handler can accept (see
    _accepts_context / execute_tool below) to get mid-call visibility that
    a plain `fn(arguments_dict)` signature has no way to expose: how much
    of the shared cross-provider tool-round budget is left, a place to
    emit progress events before the call returns, which conversation this
    is running under, and whether it's a web or CLI session.

    Every existing one-argument handler (built-in or auto-discovered via
    tool_loader.py) is completely unaffected — this is purely additive,
    detected per-handler via inspect.signature, not a new required arg.
    """

    conv_id: Optional[str]
    round_budget_remaining: Callable[[], int]  # zero-arg callable, not a
    # snapshot int — RoundBudget.used keeps changing after this context
    # object is built, so a frozen int would go stale immediately.
    emit_event: Callable[..., dict]  # bound to dev_agent_events.emit, i.e.
    # emit_event(job_id, seq, phase, status, **fields) -> the event dict it
    # just wrote to stderr (and, if a CLI hook is registered, handed to it
    # too — see dev_agent_events.set_hook). A handler's own steps=[] list
    # should append exactly this return value, not reconstruct its own
    # copy, so the live stream and the persisted/replayed result can never
    # drift apart. Any callable with this signature works as a test stub
    # (e.g. one that appends the event to a list instead of printing).
    ui: str  # "web" or "cli" — mirrors the JARVIS_UI env var, so a handler
    # can decide whether it's worth emitting UI-only events at all.


_ACCEPTS_CONTEXT_CACHE = {}  # fn -> bool, computed once per handler via
# inspect, not re-inspected on every single call.


def _accepts_context(fn):
    cached = _ACCEPTS_CONTEXT_CACHE.get(fn)
    if cached is not None:
        return cached
    try:
        sig = inspect.signature(fn)
        accepts = len(sig.parameters) >= 2
    except (TypeError, ValueError):
        accepts = False
    _ACCEPTS_CONTEXT_CACHE[fn] = accepts
    return accepts


def _drop_null_optionals(name, arguments):
    """Treat an explicit null for an OPTIONAL top-level argument as "omitted".

    Models routinely send {"parent_id": null} where they mean "not given".
    Hosts that validate tool arguments (Groq) reject that unless the schema
    declares the property nullable — ai_providers.nullable_optional_parameters
    does, at the wire — and once it gets through, the handler should see what
    it would have seen had the key been left out. That matters for the many
    handlers written `args.get("x", default)`, where a present-but-None key
    would bypass the default and blow up later (int(None), None.strip()).

    Deliberately narrow: only keys the tool's own schema lists as NOT
    required, only top-level, only a literal None. A required key, a tool
    with no known schema (a user-defined custom tool), and everything nested
    inside a value are left exactly as sent.
    """
    if not isinstance(arguments, dict) or not any(v is None for v in arguments.values()):
        return arguments
    params = (_tool_index().get(name) or {}).get("parameters")
    if not isinstance(params, dict) or not isinstance(params.get("properties"), dict):
        return arguments
    required = set(params.get("required") or [])
    return {k: v for k, v in arguments.items()
            if not (v is None and k in params["properties"] and k not in required)}


def execute_tool(name, arguments=None, verbosity=None, context=None, owner=False):
    """Run one tool by name and return a JSON-serializable result — always,
    even on failure. Never raises.

    `verbosity` ("full"/"medium"/"low"), if given, shapes the result the
    exact same way ai_client._make_tool_executor already does for every
    tool call the model makes (see tool_result_shaping.shape_result) —
    this just lets a caller outside that flow (the debug dashboard's direct
    tool-run, via `mode` → verbosity in cli.py) opt into the same trimming.
    Omitted/None leaves the result untouched, same as before this
    parameter existed.

    `owner=True` marks a run the owner started by hand (the Debug panel), the
    only caller allowed to run a tool switched off in the Tool Manager.

    `context`, if given, is a ToolContext — passed as a second positional
    argument to any handler whose signature accepts one (see
    _accepts_context). Every handler that only takes one argument keeps
    being called exactly as before; this is strictly additive.
    """
    allowed = allowed_tools_from_env()
    if allowed is not None and name not in allowed:
        return {"error": "tool not permitted"}
    # A tool the owner switched off (tool_disable.py) runs for the owner and for
    # nobody else. Default-deny: this is the single function every automatic
    # caller goes through (the model's executor, the scheduler), so a path that
    # forgets to check still can't run a disabled tool. Only the owner's own
    # hands (the Debug panel's direct run) pass owner=True.
    if not owner and tool_disable.is_tool_disabled(name):
        return tool_disable.refusal_for_tool(name)
    fn = TOOLS.get(name)
    if fn is None:
        return _unknown_tool_hint(name)
    arguments = _drop_null_optionals(name, arguments)
    try:
        if name in COMMAND_TOOLS or name in PLAYNITE_TOOLS or name in WEB_TOOLS or name in PKG_TOOLS or name in SPOTIFY_TOOLS or name in MEMORY_TOOLS or name in CAPACITY_TOOLS or name in RADIO_TOOLS or name in AUDIO_TOOLS or name in CLIPBOARD_TOOLS or name in VISION_TOOLS or name in SUBAGENT_TOOLS or name in GIT_TOOLS or name in SCREENSHOT_TOOLS or name in DESKTOP_TOOLS or name in OCR_TOOLS or name in FILE_TOOLS or name in CUSTOM_TOOLS or name in YTDL_TOOLS or name in EVERYTHING_TOOLS or name in ORGANIZE_JSON_TOOLS or name in PRESENT_TOOLS or name in SKILL_TOOLS or name in AUTO_TOOLS or name in BROWSER_TOOLS or name in ("search_tools", "get_tool_schema"):
            if _accepts_context(fn) and context is not None:
                result = fn(arguments or {}, context)
            else:
                result = fn(arguments or {})
        else:
            result = fn()
    except Exception as e:
        return {"error": f"{name} failed: {e}"}
    if verbosity:
        from . import tool_result_shaping
        result = tool_result_shaping.shape_result(name, result, verbosity)
    return result