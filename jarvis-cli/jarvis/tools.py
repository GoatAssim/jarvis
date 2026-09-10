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

import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

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
from .radio_tools import RADIO_TOOL_SCHEMAS, RADIO_TOOLS
from .screenshot_tools import SCREENSHOT_TOOL_SCHEMAS, SCREENSHOT_TOOLS
from .spotify_tools import SPOTIFY_TOOL_SCHEMAS, SPOTIFY_TOOLS
from .web_tools import WEB_TOOL_SCHEMAS, WEB_TOOLS
from .ytdl_tools import YTDL_TOOL_SCHEMAS, YTDL_TOOLS

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
    it). The full schema for anything matched here is queued by the
    caller (ai_client._make_tool_executor) for the *next* round's tools
    payload (see ai_providers._pop_discovered_tools), so a match is
    actually callable later in this same ask() — not just described and
    then unreachable.
    """
    from . import tool_registry

    query = (args or {}).get("query") or ""
    query = query.strip().lower()
    index = tool_registry.TOOL_INDEX

    if not query:
        # No query: hand back the group list, not every tool — still tiny,
        # still enough to narrow down on the next call.
        return {
            "groups": sorted(tool_registry.TOOL_GROUPS),
            "message": "Pass a query (a group name, or a keyword) to see matching tools.",
        }

    scored = []
    for name, schema in index.items():
        group = tool_registry.group_of(name) or "misc"
        keywords = " ".join(tool_registry.keywords_for(name).keys())
        haystack = " ".join([
            name.replace("_", " "),
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
            continue
        scored.append((score, name, group, schema))

    if not scored:
        return {
            "matches": [],
            "message": f"No tools matched '{query}'. Try a broader keyword or a group name.",
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
]

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
]

PLAYNITE_AND_SPOTIFY = [*PLAYNITE_TOOL_SCHEMAS, *SPOTIFY_TOOL_SCHEMAS]
TOOL_SCHEMAS = [*CORE_TOOL_SCHEMAS, *PLAYNITE_AND_SPOTIFY]


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
}


def execute_tool(name, arguments=None, verbosity=None):
    """Run one tool by name and return a JSON-serializable result — always,
    even on failure. Never raises.

    `verbosity` ("full"/"medium"/"low"), if given, shapes the result the
    exact same way ai_client._make_tool_executor already does for every
    tool call the model makes (see tool_result_shaping.shape_result) —
    this just lets a caller outside that flow (the debug dashboard's direct
    tool-run, via `mode` → verbosity in cli.py) opt into the same trimming.
    Omitted/None leaves the result untouched, same as before this
    parameter existed.
    """
    allowed = allowed_tools_from_env()
    if allowed is not None and name not in allowed:
        return {"error": "tool not permitted"}
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"no such tool: {name}"}
    try:
        if name in COMMAND_TOOLS or name in PLAYNITE_TOOLS or name in WEB_TOOLS or name in PKG_TOOLS or name in SPOTIFY_TOOLS or name in MEMORY_TOOLS or name in CAPACITY_TOOLS or name in RADIO_TOOLS or name in GIT_TOOLS or name in SCREENSHOT_TOOLS or name in DESKTOP_TOOLS or name in OCR_TOOLS or name in FILE_TOOLS or name in CUSTOM_TOOLS or name in YTDL_TOOLS or name in EVERYTHING_TOOLS or name in ORGANIZE_JSON_TOOLS or name in PRESENT_TOOLS:
            result = fn(arguments or {})
        else:
            result = fn()
    except Exception as e:
        return {"error": f"{name} failed: {e}"}
    if verbosity:
        from . import tool_result_shaping
        result = tool_result_shaping.shape_result(name, result, verbosity)
    return result