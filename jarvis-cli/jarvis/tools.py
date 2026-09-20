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
        return {
            "groups": sorted(tool_registry.TOOL_GROUPS),
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
    for name, schema in index.items():
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

    near = sorted(
        n for n in tool_registry.TOOL_INDEX
        if name in n or n in name or n.split("_")[0] == name.split("_")[0]
    )[:5]
    out = {"error": f"no such tool: {name}"}
    if near:
        out["did_you_mean"] = near
    else:
        out["hint"] = "Call search_tools with a keyword to find the right name."
    return out


def tool_get_tool_schema(args):
    """Tier-2 activation: hand back one tool's full argument schema.

    The direct analogue of Anthropic's defer_loading / Tool Search
    fetch-schema step. search_tools answers "does a tool for X exist"; this
    answers "what arguments does the tool I already know the name of take",
    which is the cheaper and far more common question once a catalog entry
    has been seen.

    Like search_tools, the reply is only half the job: ai_client's
    discover_sink promotes the named tool into this round's live schema sets,
    so it is genuinely callable on the model's very next reply rather than
    merely described.
    """
    from . import tool_registry

    raw = (args or {}).get("name") or ""
    name = raw.strip()
    if not name:
        return {"error": "Pass the exact name of the tool you want the schema for."}

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
            "Get the full argument schema for ONE tool you already know the name of. "
            "Use this when a tool is listed for you without its arguments (its "
            "description says to call this), instead of guessing arguments or "
            "searching again. The tool becomes callable on your next reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact tool name, e.g. 'playnite_launch_game'.",
                },
            },
            "required": ["name"],
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
    out = []
    seen = set()
    for name in names or []:
        if name in seen:
            continue
        schema = index.get(name)
        if schema is not None:
            out.append(schema)
            seen.add(name)
    return out


def tools_list_payload():
    """Full catalog for remote permission UIs — not filtered by session or env.

    Includes the full (uncompacted) parameter schema for each tool so a
    remote debug/permission UI can render argument inputs and docs without
    guessing — this is the same schema the model itself receives. Also
    includes each tool's current confirm_required/ai_review safety flags
    (see tool_safety.py) so the debug dashboard's toggles reflect real
    state, not a hardcoded guess.
    """
    from . import tool_safety

    items = []
    seen = set()
    for schema in TOOL_SCHEMAS:
        name = schema.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        flags = tool_safety.get_flags(name)
        items.append({
            "name": name,
            "description": schema.get("description") or "",
            "parameters": schema.get("parameters") or _NO_PARAMS,
            "confirm_required": flags["confirm_required"],
            "ai_review": flags["ai_review"],
        })
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
            })
    return items

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


def personas_list_payload():
    """Full catalog for the web UI's Skin modal (see `jarvis personas-list`
    / GET /api/personas). Every persona here is already fully validated and
    logo-resolved (PNGs are already base64 data URIs) — the web UI can
    render this straight through with no further backend round trip."""
    return {"personas": AUTO_PERSONAS, "attitudes": AUTO_ATTITUDES}


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


def execute_tool(name, arguments=None, verbosity=None, context=None):
    """Run one tool by name and return a JSON-serializable result — always,
    even on failure. Never raises.

    `verbosity` ("full"/"medium"/"low"), if given, shapes the result the
    exact same way ai_client._make_tool_executor already does for every
    tool call the model makes (see tool_result_shaping.shape_result) —
    this just lets a caller outside that flow (the debug dashboard's direct
    tool-run, via `mode` → verbosity in cli.py) opt into the same trimming.
    Omitted/None leaves the result untouched, same as before this
    parameter existed.

    `context`, if given, is a ToolContext — passed as a second positional
    argument to any handler whose signature accepts one (see
    _accepts_context). Every handler that only takes one argument keeps
    being called exactly as before; this is strictly additive.
    """
    allowed = allowed_tools_from_env()
    if allowed is not None and name not in allowed:
        return {"error": "tool not permitted"}
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