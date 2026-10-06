# Open one mockup scene in its own terminal window and photograph that window.
#
# This script never ends a process. Windows Terminal runs every window of
# every session in a single process, so stopping "the demo's process" closes
# the windows somebody was working in — which is exactly what happened once.
# The window it opens is left for you to close: esc or q in the room list,
# ctrl+c anywhere else.
#
# The frame is taken from the screen rather than by reading the console
# buffer: ReadConsoleOutputCharacterW returns characters instead of cells, so
# one wide glyph shifts every row after it and a correct frame reads as
# smeared.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File demo/capture.ps1 -Scene chat
#
# If no window is found, nothing is touched and the scene is left on screen so
# you can shoot it yourself with Win+Shift+S.

param(
    [ValidateSet("rooms", "chat", "digest", "friends", "filter", "sent", "unseen", "real", "cli")]
    [string]$Scene = "chat",
    [int]$Columns = 112,
    [int]$Rows = 0
)

# cli is not the interface: it prints and exits, so the window has to be kept
# open by cmd itself, and it needs the rows its JSON occupies.
$interactive = $Scene -ne "cli"
if ($Rows -eq 0) { $Rows = if ($interactive) { 32 } else { 50 } }
$switch = if ($interactive) { "/c" } else { "/k" }

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$shots = Join-Path $root "demo\shots"
New-Item -ItemType Directory -Force -Path $shots | Out-Null
$target = Join-Path $shots "$Scene.png"
# Unique per run, so a window left open from an earlier scene is never the one
# that gets photographed.
$title = "kanao-cli-$Scene-$([guid]::NewGuid().ToString('N').Substring(0, 6))"

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;

public class Shot {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr window, int attribute, out RECT value, int size);

    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

    public static RECT Bounds(IntPtr window) {
        RECT rect;
        // Extended frame bounds: GetWindowRect includes the invisible resize
        // border, which would frame the shot in whatever sits behind it.
        if (DwmGetWindowAttribute(window, 9, out rect, Marshal.SizeOf(typeof(RECT))) != 0)
            throw new Exception("window bounds unavailable");
        return rect;
    }
}
"@

# -w -1 asks for a window of its own: without it the terminal adds a tab to
# whatever window is already open, and the shot would be of somebody else's
# session. --size is a global option and has to sit before new-tab; given
# after --title it is not rejected but swallowed, and the terminal opens a
# window whose only content is the word "Error". mode con sets the console
# buffer as well, which is what the interface measures itself against.
Start-Process wt.exe -ArgumentList @(
    "-w", "-1",
    "--size", "$Columns,$Rows",
    "new-tab", "--title", $title,
    "cmd", $switch,
    "mode con: cols=$Columns lines=$Rows & cd /d `"$root`" & python demo\mockup.py $Scene"
)

$window = [IntPtr]::Zero
foreach ($attempt in 1..40) {
    Start-Sleep -Milliseconds 250
    $found = Get-Process WindowsTerminal -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq $title -and $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1
    if ($found) { $window = $found.MainWindowHandle; break }
}
if ($window -eq [IntPtr]::Zero) {
    Write-Warning "no window titled $title was found; the scene may still be on screen - shoot it with Win+Shift+S"
    exit 1
}

[Shot]::SetForegroundWindow($window) | Out-Null
Start-Sleep -Milliseconds 1500   # the first frame, plus the terminal's fade-in

$bounds = [Shot]::Bounds($window)
$width = $bounds.Right - $bounds.Left
$height = $bounds.Bottom - $bounds.Top
$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bitmap.Size)
$bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()

Write-Output "$target  ${width}x${height}"
Write-Output "the window is still open on purpose - close it yourself (esc/q, or ctrl+c)"
