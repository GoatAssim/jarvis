"""Windows Wi-Fi and Bluetooth radio on/off for Jarvis."""

import json
import subprocess
import sys

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PS_HELPERS = r"""
$ErrorActionPreference = 'Stop'
function Convert-ObjJson($obj) {
  $obj | ConvertTo-Json -Compress -Depth 6
}
function Get-NetRadios {
  $wifi = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
    $_.PhysicalMediaType -eq 'Native802_11' -or $_.Name -match 'Wi-?Fi' -or $_.InterfaceDescription -match 'Wireless|802\.11|Wi-?Fi'
  })
  $bt = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
    $_.PhysicalMediaType -eq 'BlueTooth' -or $_.Name -match 'Bluetooth' -or $_.InterfaceDescription -match 'Bluetooth'
  })
  [pscustomobject]@{ wifi = $wifi; bluetooth = $bt }
}
function Get-BtPnp {
  @(Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {
    $_.Class -eq 'Bluetooth' -and $_.FriendlyName -match 'Wireless Bluetooth|Bluetooth Radio|Bluetooth Adapter' -and $_.FriendlyName -notmatch 'Enumerator'
  })
}
function Try-SetWinRT($kindName, $stateName) {
  try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop | Out-Null
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    if (-not $asTaskGeneric) { return $false }
    function Await-WinRT($asyncOp, $ms) {
      $resultType = $asyncOp.GetType().GenericTypeArguments[0]
      $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
      $netTask = $asTask.Invoke($null, @($asyncOp))
      if (-not $netTask.Wait($ms)) { return $null }
      $netTask.Result
    }
    $null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
    $access = Await-WinRT ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) 4000
    $radios = Await-WinRT ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) 4000
    if (-not $radios) { return $false }
    $match = @($radios | Where-Object { $_.Kind.ToString() -eq $kindName })
    if ($match.Count -eq 0) { return $false }
    $target = [Windows.Devices.Radios.RadioState]::$stateName
    foreach ($r in $match) {
      $res = Await-WinRT ($r.SetStateAsync($target)) 8000
      if ([string]$res -eq 'Denied') { throw 'Radio access denied (Windows privacy / not admin).' }
    }
    return $true
  } catch {
    return $false
  }
}
"""


def _ps(script, timeout=40):
    if sys.platform != "win32":
        return None, "Wi-Fi/Bluetooth radio control is wired for Windows."
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return None, "PowerShell not found."
    except subprocess.TimeoutExpired:
        return None, f"Timed out after {timeout}s"
    out = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        blob = err or out or f"exit {result.returncode}"
        low = blob.lower()
        if "access" in low or "denied" in low or "administrator" in low:
            return None, (
                "Need Administrator privileges to change this radio. "
                "Run a terminal as administrator and retry, or use Windows Settings."
            )
        return None, blob[:500]
    return out, None


def _parse_obj(out):
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def tool_radio_status(args=None):
    if sys.platform != "win32":
        return {"error": "Radio control is Windows-only."}
    script = _PS_HELPERS + r"""
$nr = Get-NetRadios
$pnp = Get-BtPnp
$obj = @{
  wifi_adapters = @($nr.wifi | ForEach-Object { @{ name = $_.Name; status = [string]$_.Status; admin = [string]$_.AdminStatus; description = $_.InterfaceDescription } })
  bluetooth_adapters = @($nr.bluetooth | ForEach-Object { @{ name = $_.Name; status = [string]$_.Status; admin = [string]$_.AdminStatus; description = $_.InterfaceDescription } })
  bluetooth_radios = @($pnp | ForEach-Object { @{ name = $_.FriendlyName; status = [string]$_.Status; instance_id = $_.InstanceId } })
  note = 'wifi_set / bluetooth_set: action on|off. Off needs confirm=true. May need Administrator. Bluetooth Network Connection is the PAN adapter; radio PnP devices are listed separately.'
}
Convert-ObjJson $obj
"""
    out, err = _ps(script, timeout=25)
    if err:
        return {"error": err}
    data = _parse_obj(out)
    if not data:
        return {"error": out[:300] if out else "Could not read radios."}
    return data


def _set_radio(kind, action, confirm):
    if sys.platform != "win32":
        return {"error": "Radio control is Windows-only."}
    action = (action or "").strip().lower()
    if action in ("open", "enable", "on", "start"):
        action = "on"
    elif action in ("close", "disable", "off", "stop"):
        action = "off"
    else:
        return {"needs_clarification": True, "message": f"Turn {kind} on or off?"}
    if action == "off" and confirm is not True:
        return {
            "needs_confirmation": True,
            "radio": kind,
            "action": "off",
            "message": f"Ask the user to confirm turning {kind} OFF, then call again with confirm=true.",
        }

    kind_rt = "WiFi" if kind == "wifi" else "Bluetooth"
    state_ps = "On" if action == "on" else "Off"
    cmdlet = "Enable-NetAdapter" if action == "on" else "Disable-NetAdapter"
    pnp_cmd = "Enable-PnpDevice" if action == "on" else "Disable-PnpDevice"

    script = _PS_HELPERS + f"""
$used = @()
if (Try-SetWinRT '{kind_rt}' '{state_ps}') {{ $used += 'winrt' }}
$nr = Get-NetRadios
$list = if ('{kind}' -eq 'wifi') {{ $nr.wifi }} else {{ $nr.bluetooth }}
foreach ($a in @($list)) {{
  try {{
    {cmdlet} -Name $a.Name -Confirm:$false -ErrorAction Stop
    $used += ('netadapter:' + $a.Name)
  }} catch {{
    $used += ('netadapter-fail:' + $a.Name + ':' + $_.Exception.Message)
  }}
}}
if ('{kind}' -eq 'bluetooth') {{
  foreach ($d in Get-BtPnp) {{
    try {{
      {pnp_cmd} -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop
      $used += ('pnp:' + $d.FriendlyName)
    }} catch {{
      $used += ('pnp-fail:' + $d.FriendlyName + ':' + $_.Exception.Message)
    }}
  }}
}}
if ($used.Count -eq 0) {{
  Write-Error 'No {kind} adapter or radio found.'
  exit 2
}}
Convert-ObjJson @{{ ok = $true; attempted = $used }}
"""
    out, err = _ps(script, timeout=35)
    if err:
        return {"ok": False, "radio": kind, "action": action, "error": err}
    attempted = (_parse_obj(out) or {}).get("attempted")
    status = tool_radio_status()
    return {"ok": True, "radio": kind, "action": action, "attempted": attempted, "status": status}


def tool_wifi_set(args):
    args = args or {}
    return _set_radio("wifi", args.get("action"), args.get("confirm"))


def tool_bluetooth_set(args):
    args = args or {}
    return _set_radio("bluetooth", args.get("action"), args.get("confirm"))


RADIO_TOOL_SCHEMAS = [
    {
        "name": "radio_status",
        "description": "Windows Wi-Fi and Bluetooth adapter/radio on/off state.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "wifi_set",
        "description": "Turn Windows Wi-Fi on or off. action=on|off (open/close aliases). Off requires confirm=true. May need Administrator.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "on or off (open/close aliases work)."},
                "confirm": {"type": "boolean", "description": "Must be true to turn Wi-Fi off."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "bluetooth_set",
        "description": "Turn Windows Bluetooth on or off. action=on|off (open/close aliases). Off requires confirm=true. May need Administrator.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "on or off (open/close aliases work)."},
                "confirm": {"type": "boolean", "description": "Must be true to turn Bluetooth off."},
            },
            "required": ["action"],
        },
    },
]

RADIO_TOOLS = {
    "radio_status": tool_radio_status,
    "wifi_set": tool_wifi_set,
    "bluetooth_set": tool_bluetooth_set,
}
