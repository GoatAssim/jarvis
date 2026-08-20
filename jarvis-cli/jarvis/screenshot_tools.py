"""Desktop screenshot for Jarvis — saved to disk, never sent to the AI as pixels."""

import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

JARVIS_DIR = Path.home() / ".jarvis"
SCREENSHOT_DIR = JARVIS_DIR / "screenshots"
ENCODING = "utf-8"
MAX_KEEP = 40


def ensure_dir():
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SCREENSHOT_DIR


def _prune():
    files = sorted(SCREENSHOT_DIR.glob("ss_*.png"), key=lambda p: p.stat().st_mtime)
    while len(files) > MAX_KEEP:
        try:
            files.pop(0).unlink(missing_ok=True)
        except OSError:
            break


def _emit_media(filename):
    """Machine line for the web UI — not for the model."""
    print(f"JARVIS_MEDIA\tscreenshot\t{filename}", file=sys.stderr, flush=True)


def _capture_windows(path):
    # Default PowerShell is DPI-unaware, so VirtualScreen is logical (scaled)
    # and CopyFromScreen only grabs a corner of a high-DPI / multi-monitor desk.
    dest = str(path).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class NativeScreen {{
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int nIndex);
  [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
  [DllImport("gdi32.dll")] public static extern bool BitBlt(IntPtr hdcDest, int xDest, int yDest, int w, int h, IntPtr hdcSrc, int xSrc, int ySrc, int rop);
  [DllImport("gdi32.dll")] public static extern int GetDeviceCaps(IntPtr hdc, int index);
  public const int SM_XVIRTUALSCREEN = 76;
  public const int SM_YVIRTUALSCREEN = 77;
  public const int SM_CXVIRTUALSCREEN = 78;
  public const int SM_CYVIRTUALSCREEN = 79;
  public const int DESKTOPHORZRES = 118;
  public const int DESKTOPVERTRES = 117;
  public const int SRCCOPY = 0x00CC0020;
  public static void MakeDpiAware() {{
    try {{ SetProcessDpiAwareness(2); }} catch {{}}
    try {{ SetProcessDPIAware(); }} catch {{}}
  }}
}}
"@
[NativeScreen]::MakeDpiAware()
$left = [NativeScreen]::GetSystemMetrics([NativeScreen]::SM_XVIRTUALSCREEN)
$top = [NativeScreen]::GetSystemMetrics([NativeScreen]::SM_YVIRTUALSCREEN)
$width = [NativeScreen]::GetSystemMetrics([NativeScreen]::SM_CXVIRTUALSCREEN)
$height = [NativeScreen]::GetSystemMetrics([NativeScreen]::SM_CYVIRTUALSCREEN)
# Fallback: physical desktop size from GDI when metrics look wrong
$hdcProbe = [NativeScreen]::GetDC([IntPtr]::Zero)
try {{
  $physW = [NativeScreen]::GetDeviceCaps($hdcProbe, [NativeScreen]::DESKTOPHORZRES)
  $physH = [NativeScreen]::GetDeviceCaps($hdcProbe, [NativeScreen]::DESKTOPVERTRES)
}} finally {{
  [void][NativeScreen]::ReleaseDC([IntPtr]::Zero, $hdcProbe)
}}
if ($width -lt $physW) {{ $width = $physW; $left = 0 }}
if ($height -lt $physH) {{ $height = $physH; $top = 0 }}
if ($width -le 0 -or $height -le 0) {{ throw "Could not read screen size." }}
$bmp = New-Object System.Drawing.Bitmap $width, $height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdcDest = $g.GetHdc()
$hdcSrc = [NativeScreen]::GetDC([IntPtr]::Zero)
try {{
  $ok = [NativeScreen]::BitBlt($hdcDest, 0, 0, $width, $height, $hdcSrc, $left, $top, [NativeScreen]::SRCCOPY)
  if (-not $ok) {{ throw "BitBlt failed." }}
}} finally {{
  [void][NativeScreen]::ReleaseDC([IntPtr]::Zero, $hdcSrc)
  $g.ReleaseHdc($hdcDest)
}}
$bmp.Save('{dest}', [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "$width $height"
$g.Dispose(); $bmp.Dispose()
"""
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
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return None, "PowerShell not found."
    except subprocess.TimeoutExpired:
        return None, "Screenshot timed out."
    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    out = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not path.exists():
        return None, (err or out or f"exit {result.returncode}")[:400]
    parts = out.split()
    try:
        w, h = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        w, h = None, None
    return {"width": w, "height": h}, None


def _capture_mss(path):
    try:
        import mss
        from PIL import Image
    except ImportError:
        return None, (
            "Need Windows PowerShell, or install: pip install mss Pillow"
        )
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img.save(path, "PNG")
        return {"width": shot.width, "height": shot.height}, None


def tool_take_screenshot(args=None):
    args = args or {}
    ensure_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fid = "ss_" + stamp + "_" + secrets.token_hex(3)
    path = SCREENSHOT_DIR / f"{fid}.png"

    meta, err = (None, None)
    if sys.platform == "win32":
        meta, err = _capture_windows(path)
    if err or meta is None:
        meta, err = _capture_mss(path)
    if err or not path.exists():
        return {"ok": False, "error": err or "Screenshot failed."}

    _prune()
    _emit_media(path.name)
    size = path.stat().st_size
    return {
        "ok": True,
        "id": fid,
        "file": path.name,
        "path": str(path),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "bytes": size,
        "note": (
            "Screenshot saved on disk and delivered to the user in the UI. "
            "Do NOT describe the pixels, do NOT invent what is on screen, "
            "and do NOT ask to upload the image. Reply in one short sentence that it is ready."
        ),
    }


SCREENSHOT_TOOL_SCHEMAS = [
    {
        "name": "take_screenshot",
        "description": (
            "Capture the desktop screen and show it to the user in the Jarvis UI. "
            "The image is NOT sent to you — only a tiny ok/path result. "
            "Use when the user asks for a screenshot / SS / capture the screen."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

SCREENSHOT_TOOLS = {
    "take_screenshot": tool_take_screenshot,
}
