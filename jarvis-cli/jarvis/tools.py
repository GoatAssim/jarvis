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

import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .command_tools import COMMAND_TOOL_SCHEMAS, COMMAND_TOOLS
from .git_tools import GIT_TOOL_SCHEMAS, GIT_TOOLS
from .memory import MEMORY_TOOL_SCHEMAS, MEMORY_TOOLS
from .pkg_tools import PKG_TOOL_SCHEMAS, PKG_TOOLS
from .playnite_api_tools import PLAYNITE_API_TOOL_SCHEMAS, PLAYNITE_API_TOOLS
from .playnite_tools import PLAYNITE_TOOL_SCHEMAS as _PLAYNITE_CORE_SCHEMAS, PLAYNITE_TOOLS as _PLAYNITE_CORE_TOOLS
from .radio_tools import RADIO_TOOL_SCHEMAS, RADIO_TOOLS
from .spotify_tools import SPOTIFY_TOOL_SCHEMAS, SPOTIFY_TOOLS
from .web_tools import WEB_TOOL_SCHEMAS, WEB_TOOLS

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
    *RADIO_TOOL_SCHEMAS,
    *GIT_TOOL_SCHEMAS,
    *WEB_TOOL_SCHEMAS,
    *PKG_TOOL_SCHEMAS,
]

PLAYNITE_AND_SPOTIFY = [*PLAYNITE_TOOL_SCHEMAS, *SPOTIFY_TOOL_SCHEMAS]
TOOL_SCHEMAS = [*CORE_TOOL_SCHEMAS, *PLAYNITE_AND_SPOTIFY]


def tool_schemas_for_session():
    """Playnite tools only when configured. Spotify is always offered so the
    model can search/play or get a login prompt — advertising spotify_* in
    the prompt without listing them makes Groq HTTP 400 (tool not in request)."""
    from . import playnite_config

    out = list(CORE_TOOL_SCHEMAS)
    out.extend(SPOTIFY_TOOL_SCHEMAS)
    if playnite_config.is_configured():
        out.extend(PLAYNITE_TOOL_SCHEMAS)
    return out

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
    **RADIO_TOOLS,
    **GIT_TOOLS,
    **WEB_TOOLS,
    **PKG_TOOLS,
    **PLAYNITE_TOOLS,
    **SPOTIFY_TOOLS,
}


def execute_tool(name, arguments=None):
    """Run one tool by name and return a JSON-serializable result — always,
    even on failure. Never raises."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"no such tool: {name}"}
    try:
        if name in COMMAND_TOOLS or name in PLAYNITE_TOOLS or name in WEB_TOOLS or name in PKG_TOOLS or name in SPOTIFY_TOOLS or name in MEMORY_TOOLS or name in RADIO_TOOLS or name in GIT_TOOLS:
            return fn(arguments or {})
        return fn()
    except Exception as e:
        return {"error": f"{name} failed: {e}"}
