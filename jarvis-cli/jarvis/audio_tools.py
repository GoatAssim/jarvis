"""Windows audio output control for Jarvis — volume, mute, default device.

WHY POWERSHELL + INLINE C# AND NOT A PYTHON PACKAGE
---------------------------------------------------
`pycaw`/`comtypes` would be the obvious Python route, but this repo ships a
CLI that has to run from a frozen exe on a machine we don't control, and
radio_tools.py already established the pattern: shell out to PowerShell,
keep the dependency surface at zero. Everything below uses Core Audio COM
interfaces that have shipped in every Windows since Vista, declared inline
via Add-Type, so there is nothing to install.

The three interfaces used:

    IMMDeviceEnumerator   enumerate render (output) endpoints, find default
    IAudioEndpointVolume  master volume scalar + mute, per endpoint
    IPolicyConfig         set the default endpoint (undocumented but stable
                          since Vista; it is what every "switch default
                          sound device" utility on Windows uses)

VTABLE ORDER IS THE WHOLE GAME
------------------------------
These are IUnknown-derived interfaces matched by *method order*, not by
name — a misplaced or omitted method silently calls the wrong slot and
usually hard-crashes the host process rather than raising. Every interface
below is declared in full vtable order even where a method is unused, with
unused slots stubbed as `void Unused_N()` placeholders so the shape stays
correct. Do not "clean up" by deleting a stub.

DEVICE MATCHING
---------------
`set_default_output` takes a substring of the friendly name ("headphones",
"lg tv") rather than an endpoint id, because the id is an opaque GUID path
no human or model will ever have on hand. Matching is case-insensitive
substring, and an ambiguous match returns the candidate list with
`needs_clarification` instead of guessing — same convention
`command_tools._resolve_command()` uses on a miss.

SAFETY
------
None of this is destructive: volume and mute are trivially reversible and
switching the default output device is a settings change, not a mutation of
anything on disk. So unlike `wifi_set`/`bluetooth_set` (which can cut the
machine's own network out from under a remote caller) nothing here requires
`confirm=true`. `tool_safety.py` is deliberately not touched.
"""

import json
import subprocess
import sys

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Volume step used by volume_up / volume_down when no amount is given.
# Windows' own media keys move in 2% increments, which takes a dozen presses
# to do anything noticeable; 10 matches what a person means by "turn it up".
DEFAULT_STEP = 10

# Add-Type is the slow part (~1-2s to compile on first run in a session), so
# every helper below is invoked through one script that defines the type once
# and then does the work, rather than one PowerShell launch per operation.
_PS_AUDIO = r"""
$ErrorActionPreference = 'Stop'
if (-not ([System.Management.Automation.PSTypeName]'JarvisAudio.Endpoints').Type) {
Add-Type -Language CSharp @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace JarvisAudio {

  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey { public Guid fmtid; public int pid; }

  [StructLayout(LayoutKind.Explicit)]
  public struct PropVariant {
    [FieldOffset(0)] public short vt;
    [FieldOffset(8)] public IntPtr pointerValue;
  }

  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
    int GetDevice(string id, out IMMDevice device);
    int NotImpl1();
    int NotImpl2();
  }

  [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IMMDeviceCollection {
    int GetCount(out int count);
    int Item(int index, out IMMDevice device);
  }

  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IMMDevice {
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
                 [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    int OpenPropertyStore(int stgmAccess, out IPropertyStore properties);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    int GetState(out int state);
  }

  [Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    int GetCount(out int props);
    int GetAt(int index, out PropertyKey key);
    int GetValue(ref PropertyKey key, out PropVariant value);
    int SetValue(ref PropertyKey key, ref PropVariant value);
    int Commit();
  }

  [Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IAudioEndpointVolume {
    int NotImpl1();
    int NotImpl2();
    int GetChannelCount(out uint count);
    int SetMasterVolumeLevel(float levelDb, ref Guid ctx);
    int SetMasterVolumeLevelScalar(float level, ref Guid ctx);
    int GetMasterVolumeLevel(out float levelDb);
    int GetMasterVolumeLevelScalar(out float level);
    int SetChannelVolumeLevel(uint channel, float levelDb, ref Guid ctx);
    int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid ctx);
    int GetChannelVolumeLevel(uint channel, out float levelDb);
    int GetChannelVolumeLevelScalar(uint channel, out float level);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid ctx);
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
  }

  // Undocumented, but the only way to set the default endpoint from user
  // code. Two CLSIDs exist in the wild: the Win7+ one and a Vista fallback.
  [Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPolicyConfig {
    int GetMixFormat();
    int GetDeviceFormat();
    int ResetDeviceFormat();
    int SetDeviceFormat();
    int GetProcessingPeriod();
    int SetProcessingPeriod();
    int GetShareMode();
    int SetShareMode();
    int GetPropertyValue();
    int SetPropertyValue();
    int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string deviceId, int role);
    int SetEndpointVisibility();
  }

  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
  public class MMDeviceEnumeratorComObject { }

  [ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")]
  public class PolicyConfigComObject { }

  public class Device {
    public string Id;
    public string Name;
    public bool IsDefault;
    public int Volume;
    public bool Muted;
  }

  public static class Endpoints {
    const int RENDER = 0;
    const int ACTIVE = 1;
    const int MULTIMEDIA = 1;
    const int CLSCTX_ALL = 23;
    static Guid IID_VOLUME = new Guid("5CDF2C82-841E-4546-9722-0CF74078229A");

    static IMMDeviceEnumerator Enumerator() {
      return (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
    }

    static string NameOf(IMMDevice dev) {
      IPropertyStore store;
      dev.OpenPropertyStore(0 /* STGM_READ */, out store);
      PropertyKey key = new PropertyKey();
      key.fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");
      key.pid = 14;  // PKEY_Device_FriendlyName
      PropVariant val;
      store.GetValue(ref key, out val);
      return val.pointerValue == IntPtr.Zero
        ? "(unnamed)"
        : Marshal.PtrToStringUni(val.pointerValue);
    }

    static IAudioEndpointVolume VolumeOf(IMMDevice dev) {
      object iface;
      dev.Activate(ref IID_VOLUME, CLSCTX_ALL, IntPtr.Zero, out iface);
      return (IAudioEndpointVolume)iface;
    }

    public static List<Device> List() {
      var result = new List<Device>();
      var en = Enumerator();
      IMMDevice def = null;
      string defId = "";
      try {
        en.GetDefaultAudioEndpoint(RENDER, MULTIMEDIA, out def);
        def.GetId(out defId);
      } catch { defId = ""; }

      IMMDeviceCollection all;
      en.EnumAudioEndpoints(RENDER, ACTIVE, out all);
      int count;
      all.GetCount(out count);
      for (int i = 0; i < count; i++) {
        IMMDevice dev;
        all.Item(i, out dev);
        string id;
        dev.GetId(out id);
        var d = new Device();
        d.Id = id;
        d.Name = NameOf(dev);
        d.IsDefault = (id == defId);
        try {
          var vol = VolumeOf(dev);
          float scalar; bool muted;
          vol.GetMasterVolumeLevelScalar(out scalar);
          vol.GetMute(out muted);
          d.Volume = (int)Math.Round(scalar * 100);
          d.Muted = muted;
        } catch {
          d.Volume = -1;
          d.Muted = false;
        }
        result.Add(d);
      }
      return result;
    }

    // All of the setters below act on the *default* endpoint. Per-device
    // volume is possible but nobody means "set the volume of the monitor I
    // am not listening through" when they say "turn it down".
    static IAudioEndpointVolume DefaultVolume() {
      IMMDevice dev;
      Enumerator().GetDefaultAudioEndpoint(RENDER, MULTIMEDIA, out dev);
      return VolumeOf(dev);
    }

    public static int GetVolume() {
      float scalar;
      DefaultVolume().GetMasterVolumeLevelScalar(out scalar);
      return (int)Math.Round(scalar * 100);
    }

    public static int SetVolume(int percent) {
      if (percent < 0) percent = 0;
      if (percent > 100) percent = 100;
      Guid ctx = Guid.Empty;
      DefaultVolume().SetMasterVolumeLevelScalar(percent / 100f, ref ctx);
      return percent;
    }

    public static bool GetMute() {
      bool muted;
      DefaultVolume().GetMute(out muted);
      return muted;
    }

    public static bool SetMute(bool mute) {
      Guid ctx = Guid.Empty;
      DefaultVolume().SetMute(mute, ref ctx);
      return mute;
    }

    // Role is set for all three roles (console/multimedia/communications),
    // because setting only the multimedia role leaves Discord and friends
    // on the old device and looks like the switch silently didn't work.
    public static void SetDefault(string deviceId) {
      var cfg = (IPolicyConfig)(new PolicyConfigComObject());
      for (int role = 0; role < 3; role++) {
        cfg.SetDefaultEndpoint(deviceId, role);
      }
    }
  }
}
'@
}
"""


def _ps(script, timeout=45):
    """Run one PowerShell script, return (stdout, error). Mirrors radio_tools._ps."""
    if sys.platform != "win32":
        return None, "Audio control is wired for Windows."
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
        return None, blob[:500]
    return out, None


def _parse(out):
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _devices():
    """Every active output endpoint, or (None, error)."""
    script = _PS_AUDIO + r"""
$list = [JarvisAudio.Endpoints]::List()
$out = @($list | ForEach-Object {
  @{ id = $_.Id; name = $_.Name; default = $_.IsDefault; volume = $_.Volume; muted = $_.Muted }
})
ConvertTo-Json -InputObject @($out) -Compress -Depth 5
"""
    out, err = _ps(script)
    if err:
        return None, err
    data = _parse(out)
    if data is None:
        return None, (out[:300] if out else "Could not read audio devices.")
    # ConvertTo-Json collapses a single-element array to a bare object.
    if isinstance(data, dict):
        data = [data]
    return data, None


def tool_audio_status(args=None):
    """Current default output device, its volume and mute state, plus the
    other endpoints available to switch to."""
    devices, err = _devices()
    if err:
        return {"error": err}
    current = next((d for d in devices if d.get("default")), None)
    return {
        "default_device": (current or {}).get("name"),
        "volume": (current or {}).get("volume"),
        "muted": (current or {}).get("muted"),
        "available_devices": [d.get("name") for d in devices],
        "note": "set_volume/volume_up/volume_down/set_mute act on the default device.",
    }


def _clamp_percent(value):
    try:
        pct = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, pct))


def _apply_volume(percent):
    script = _PS_AUDIO + (
        "\n$v = [JarvisAudio.Endpoints]::SetVolume(%d)\n"
        "ConvertTo-Json -InputObject @{ volume = $v; "
        "muted = [JarvisAudio.Endpoints]::GetMute() } -Compress\n" % percent
    )
    out, err = _ps(script)
    if err:
        return {"ok": False, "error": err}
    data = _parse(out) or {}
    return {"ok": True, "volume": data.get("volume", percent), "muted": data.get("muted")}


def tool_set_volume(args):
    """Set the default output device's volume to an absolute percentage."""
    args = args or {}
    pct = _clamp_percent(args.get("percent"))
    if pct is None:
        return {"needs_clarification": True,
                "message": "What volume percentage (0-100)?"}
    result = _apply_volume(pct)
    # Setting a volume while muted does nothing audible, which reads as a
    # broken tool. Unmute as part of any explicit non-zero volume request.
    if result.get("ok") and result.get("muted") and pct > 0:
        _set_mute_raw(False)
        result["muted"] = False
        result["note"] = "unmuted, since a volume was set explicitly"
    return result


def _adjust(direction, args):
    args = args or {}
    step = args.get("amount")
    step = _clamp_percent(step) if step is not None else DEFAULT_STEP
    if not step:
        step = DEFAULT_STEP
    delta = step if direction == "up" else -step
    script = _PS_AUDIO + (
        "\n$cur = [JarvisAudio.Endpoints]::GetVolume()\n"
        "$next = [JarvisAudio.Endpoints]::SetVolume($cur + (%d))\n"
        "ConvertTo-Json -InputObject @{ previous = $cur; volume = $next } -Compress\n" % delta
    )
    out, err = _ps(script)
    if err:
        return {"ok": False, "error": err}
    data = _parse(out) or {}
    result = {"ok": True, "previous": data.get("previous"),
              "volume": data.get("volume"), "step": step}
    if direction == "up":
        # Same reasoning as set_volume: turning it up while muted is a no-op
        # the user will read as a failure.
        state = _get_mute_raw()
        if state is True:
            _set_mute_raw(False)
            result["muted"] = False
            result["note"] = "unmuted, since the volume was turned up"
    return result


def tool_volume_up(args=None):
    return _adjust("up", args)


def tool_volume_down(args=None):
    return _adjust("down", args)


def _get_mute_raw():
    out, err = _ps(_PS_AUDIO + "\nConvertTo-Json -InputObject @{ muted = "
                               "[JarvisAudio.Endpoints]::GetMute() } -Compress\n")
    if err:
        return None
    return (_parse(out) or {}).get("muted")


def _set_mute_raw(mute):
    flag = "$true" if mute else "$false"
    out, err = _ps(_PS_AUDIO + (
        "\n$m = [JarvisAudio.Endpoints]::SetMute(%s)\n"
        "ConvertTo-Json -InputObject @{ muted = $m } -Compress\n" % flag
    ))
    if err:
        return None, err
    return (_parse(out) or {}).get("muted"), None


def tool_set_mute(args):
    """Mute, unmute, or toggle the default output device."""
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    if action in ("mute", "on", "true", "silence", "yes"):
        want = True
    elif action in ("unmute", "off", "false", "no"):
        want = False
    elif action in ("toggle", "flip", "switch", ""):
        current = _get_mute_raw()
        if current is None:
            return {"ok": False, "error": "Could not read the current mute state."}
        want = not current
    else:
        return {"needs_clarification": True,
                "message": "Mute, unmute, or toggle?"}
    muted, err = _set_mute_raw(want)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "muted": muted if muted is not None else want}


def tool_set_default_output(args):
    """Switch the default output device by a substring of its name."""
    args = args or {}
    query = str(args.get("device") or "").strip()
    devices, err = _devices()
    if err:
        return {"error": err}
    if not query:
        return {"needs_clarification": True,
                "message": "Which output device?",
                "available_devices": [d.get("name") for d in devices]}

    low = query.lower()
    matches = [d for d in devices if low in str(d.get("name") or "").lower()]
    if not matches:
        return {"ok": False,
                "error": "No output device matching %r." % query,
                "available_devices": [d.get("name") for d in devices]}
    if len(matches) > 1:
        # An exact (case-insensitive) name beats an ambiguous substring —
        # "Speakers" shouldn't be unresolvable just because "Speakers (USB)"
        # also exists.
        exact = [d for d in matches if str(d.get("name") or "").lower() == low]
        if len(exact) == 1:
            matches = exact
        else:
            return {"needs_clarification": True,
                    "message": "Several output devices match %r — which one?" % query,
                    "candidates": [d.get("name") for d in matches]}

    target = matches[0]
    if target.get("default"):
        return {"ok": True, "device": target.get("name"),
                "note": "already the default output device"}

    # The id is a quoted literal rather than an interpolated bare token
    # because endpoint ids contain {} and . characters.
    script = _PS_AUDIO + (
        "\n[JarvisAudio.Endpoints]::SetDefault(%s)\n"
        "ConvertTo-Json -InputObject @{ ok = $true } -Compress\n"
        % _ps_quote(target.get("id") or "")
    )
    out, err = _ps(script)
    if err:
        return {"ok": False, "device": target.get("name"), "error": err}
    return {"ok": True, "device": target.get("name"),
            "previous": next((d.get("name") for d in devices if d.get("default")), None)}


def _ps_quote(value):
    """Single-quoted PowerShell literal — the only escape inside one is ''."""
    return "'%s'" % str(value).replace("'", "''")


AUDIO_TOOL_SCHEMAS = [
    {
        "name": "audio_status",
        "description": "Current default sound output device, its volume (0-100), mute state, and the other output devices available.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_volume",
        "description": "Set the system output volume to an absolute percentage (0-100). Unmutes if muted.",
        "parameters": {
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "description": "Target volume, 0-100."},
            },
            "required": ["percent"],
        },
    },
    {
        "name": "volume_up",
        "description": "Raise the system output volume. Default step is 10 percentage points.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Percentage points to raise by. Defaults to 10."},
            },
            "required": [],
        },
    },
    {
        "name": "volume_down",
        "description": "Lower the system output volume. Default step is 10 percentage points.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Percentage points to lower by. Defaults to 10."},
            },
            "required": [],
        },
    },
    {
        "name": "set_mute",
        "description": "Mute, unmute, or toggle the system output. action=mute|unmute|toggle.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "mute, unmute, or toggle."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_default_output",
        "description": "Switch the default sound output device by part of its name (e.g. 'headphones', 'tv'). Call audio_status first if unsure what exists.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Part of the device's name, case-insensitive."},
            },
            "required": ["device"],
        },
    },
]

AUDIO_TOOLS = {
    "audio_status": tool_audio_status,
    "set_volume": tool_set_volume,
    "volume_up": tool_volume_up,
    "volume_down": tool_volume_down,
    "set_mute": tool_set_mute,
    "set_default_output": tool_set_default_output,
}
