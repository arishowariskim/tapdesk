# 관리자 권한 자동 요청
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$nssm    = "C:\Users\GAPER\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$py      = "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk\.venv\Scripts\python.exe"
$wd      = "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk\watchdog.py"
$dir     = "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk"
$log     = "C:\Temp\tapdesk_watchdog.log"   # 영문 경로
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\TapDesk_7777\Parameters"

# C:\Temp 폴더 생성
if (-not (Test-Path "C:\Temp")) { New-Item -ItemType Directory "C:\Temp" | Out-Null }

Write-Host "[1] 서비스 중지..."
& $nssm stop TapDesk_7777 confirm
Start-Sleep -Seconds 3

Write-Host "[2] NSSM 기본 설정..."
& $nssm set TapDesk_7777 Application $py
& $nssm set TapDesk_7777 AppDirectory $dir
& $nssm set TapDesk_7777 AppStdout $log
& $nssm set TapDesk_7777 AppStderr $log

Write-Host "[3] AppParameters 레지스트리 직접 저장..."
Set-ItemProperty -Path $regPath -Name "AppParameters" -Value "`"$wd`""
Write-Host "  저장값: $(Get-ItemPropertyValue $regPath 'AppParameters')"

Write-Host "[4] 서비스 시작..."
& $nssm start TapDesk_7777
Start-Sleep -Seconds 6

Write-Host "[5] 상태..."
sc.exe query TapDesk_7777 | Select-String "STATE"

Write-Host "[6] NSSM 로그..."
if (Test-Path $log) { Get-Content $log -Tail 20 } else { Write-Host "로그 없음" }

Write-Host "[7] 크래시 로그..."
if (Test-Path "C:\Temp\wd_crash.log") { Get-Content "C:\Temp\wd_crash.log" -Tail 20 } else { Write-Host "크래시 없음" }

Read-Host "완료. 엔터"
