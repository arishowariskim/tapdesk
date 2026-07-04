# PC Remote one-click recovery: kill stale -> server.py+watchdog.py -> ADB tunnel -> popup
$ErrorActionPreference = "SilentlyContinue"
$ROOT = $PSScriptRoot
$PY   = "$ROOT\.venv\Scripts\python.exe"
$PYW  = "$ROOT\.venv\Scripts\pythonw.exe"
$ADB  = "C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe"

function Port-Up($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -EA SilentlyContinue)
}

# 1. kill stale server.py / watchdog.py processes
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'server\.py|watchdog\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
Start-Sleep -Milliseconds 600

# 1.5. TIME_WAIT 해제 대기 (최대 60초) — kill 후 OS가 포트를 잠궈두는 문제 방지
for ($i = 0; $i -lt 120; $i++) {
    $tw = Get-NetTCPConnection -LocalPort 7780 -State TimeWait -EA SilentlyContinue
    if (-not $tw) { break }
    Start-Sleep -Milliseconds 500
}

# 2. start watchdog (holds singleton on port 7781, monitors 7780)
if (-not (Port-Up 7781)) {
    Start-Process -FilePath $PYW -ArgumentList "watchdog.py" -WorkingDirectory $ROOT -WindowStyle Hidden
}

# 3. start server.py directly (no 8s watchdog grace wait)
if (-not (Port-Up 7780)) {
    Start-Process -FilePath $PY -ArgumentList "server.py" -WorkingDirectory $ROOT -WindowStyle Hidden
}

# 4. ADB reverse tunnel for USB phone connection
if (Test-Path $ADB) { & $ADB reverse tcp:7780 tcp:7780 2>$null }

# 5. wait for 7780 (up to 15s)
for ($i = 0; $i -lt 30; $i++) {
    if (Port-Up 7780) { break }
    Start-Sleep -Milliseconds 500
}

# 6. result popup (auto-close 3s on success, 5s on fail)
$wsh = New-Object -ComObject WScript.Shell
if (Port-Up 7780) {
    $wsh.Popup("7780 LISTENING`nPhone: close app then reopen", 3, "PC Remote OK", 64) | Out-Null
} else {
    $wsh.Popup("7780 not responding`nCheck watchdog.log", 5, "PC Remote FAIL", 48) | Out-Null
}
