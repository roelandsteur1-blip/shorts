# Creates "Shorts Dashboard" shortcuts on the Desktop and in the Start menu that run launcher\dashboard.pyw.
# Run once:  powershell -ExecutionPolicy Bypass -File launcher\install_shortcuts.ps1 [-Startup]
#   -Startup  also start the dashboard (without opening the browser) when you log in.
#   -Remove   delete the shortcuts again.
param([switch]$Startup, [switch]$Remove)

$launcher = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $launcher
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
$script = Join-Path $launcher "dashboard.pyw"
$icon = Join-Path $launcher "dashboard.ico"

$places = @{
    Desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Shorts Dashboard.lnk"
    Start   = Join-Path ([Environment]::GetFolderPath("Programs")) "Shorts Dashboard.lnk"
    Login   = Join-Path ([Environment]::GetFolderPath("Startup")) "Shorts Dashboard.lnk"
}

if ($Remove) {
    $places.Values | Where-Object { Test-Path $_ } | ForEach-Object { Remove-Item $_; "removed $_" }
    return
}

if (-not (Test-Path $icon)) { & (Join-Path $repo ".venv\Scripts\python.exe") (Join-Path $launcher "make_icon.py") }

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path, $extraArgs) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $pythonw
    $s.Arguments = "`"$script`" $extraArgs".Trim()
    $s.WorkingDirectory = $repo
    $s.IconLocation = $icon
    $s.Description = "Open the Shorts dashboard"
    $s.Save()
    "created $path"
}

New-Shortcut $places.Desktop ""
New-Shortcut $places.Start ""
if ($Startup) { New-Shortcut $places.Login "--no-browser" }
elseif (Test-Path $places.Login) { Remove-Item $places.Login; "removed $($places.Login)" }
