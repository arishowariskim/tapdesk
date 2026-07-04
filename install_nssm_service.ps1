# TapDesk NSSM 서비스 등록 스크립트 (관리자 권한 필요)
$TAPDESK_DIR = "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk"
$PYTHON_EXE = "$TAPDESK_DIR\.venv\Scripts\python.exe"
$WATCHDOG_PY = "$TAPDESK_DIR\watchdog.py"
$SERVER_PY = "$TAPDESK_DIR\server.py"

Write-Host "=== TapDesk NSSM + Task Scheduler 등록 ===" -ForegroundColor Cyan

# ─── 1단계: Task Scheduler에 TapDeskServer 등록 (Session 1에서 서버 실행) ───
Write-Host "`n[1/3] Task Scheduler TapDeskServer 등록..." -ForegroundColor Yellow

# XML 방식으로 등록 (경로 공백 안전)
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>TapDesk server.py 자동시작 (Session 1 - 모니터 인식 가능)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$env:USERDOMAIN\$env:USERNAME</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>$PYTHON_EXE</Command>
      <Arguments>"$SERVER_PY"</Arguments>
      <WorkingDirectory>$TAPDESK_DIR</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = "$env:TEMP\TapDeskServer_task.xml"
$xml | Out-File -FilePath $xmlPath -Encoding Unicode -Force

schtasks /create /tn "TapDeskServer" /xml $xmlPath /f
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✅ TapDeskServer Task Scheduler 등록 완료" -ForegroundColor Green
} else {
    Write-Host "  ❌ Task Scheduler 등록 실패" -ForegroundColor Red
}
Remove-Item $xmlPath -ErrorAction SilentlyContinue

# ─── 2단계: NSSM에 TapDesk(watchdog) 서비스 등록 ───
Write-Host "`n[2/3] NSSM TapDesk(watchdog) 서비스 등록..." -ForegroundColor Yellow

# 기존 서비스 있으면 제거
nssm stop TapDesk 2>$null
nssm remove TapDesk confirm 2>$null

nssm install TapDesk $PYTHON_EXE $WATCHDOG_PY
nssm set TapDesk AppDirectory $TAPDESK_DIR
nssm set TapDesk DisplayName "TapDesk Watchdog"
nssm set TapDesk Description "TapDesk 경비원 - server.py 감시 및 자동재시작"
nssm set TapDesk Start SERVICE_AUTO_START
nssm set TapDesk AppStdout "$TAPDESK_DIR\watchdog_nssm.log"
nssm set TapDesk AppStderr "$TAPDESK_DIR\watchdog_nssm.log"
nssm set TapDesk AppRotateFiles 1
nssm set TapDesk AppRotateBytes 1048576

if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✅ NSSM TapDesk 서비스 등록 완료" -ForegroundColor Green
} else {
    Write-Host "  ❌ NSSM 등록 실패" -ForegroundColor Red
}

# ─── 3단계: startup TapDesk.lnk 제거 (Task Scheduler가 대체) ───
Write-Host "`n[3/3] startup TapDesk.lnk 제거 (Task Scheduler 중복 방지)..." -ForegroundColor Yellow
$startupLnk = "C:\Users\GAPER\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\TapDesk.lnk"
$startupLnkAris = "C:\Users\$env:USERNAME\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\TapDesk.lnk"
foreach ($lnk in @($startupLnk, $startupLnkAris)) {
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Write-Host "  ✅ 제거: $lnk" -ForegroundColor Green
    }
}

Write-Host "`n=== 완료 ===" -ForegroundColor Cyan
Write-Host "NSSM watchdog: Session 0에서 소켓 감시" -ForegroundColor White
Write-Host "Task Scheduler: Session 1에서 server.py 실행 (모니터 인식)" -ForegroundColor White
Write-Host "`n지금 TapDesk 서비스 시작:" -ForegroundColor Yellow
Write-Host "  nssm start TapDesk" -ForegroundColor Gray
Write-Host "`n또는 재부팅하면 자동 시작" -ForegroundColor Gray
