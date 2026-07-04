# TapDesk 응급 소생 — 아리(김대리) 없이 더블클릭 1번용 (2026-07-04)
# 증상: 폰에서 TapDesk 안 열림 / 웹페이지 사용할 수 없음 / 무한 검은화면
Write-Host "===== TapDesk 응급 소생 =====" -ForegroundColor Yellow
# 1) 얼어붙은 서버(좀비) 전원 정리
$z = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match "^python") -and $_.CommandLine -match "18_TapDesk" }
Write-Host ("1) 정리 대상 프로세스: " + @($z).Count + "개")
$z | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 3
# 2) 경비원 재가동 (경비원이 서버를 새로 띄움)
$d = "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk"
Start-Process -FilePath "$d\.venv\Scripts\pythonw.exe" -ArgumentList "`"$d\watchdog.py`"" -WorkingDirectory $d -WindowStyle Hidden
Write-Host "2) 경비원 재가동 - 서버 부활 대기 (최대 90초)..."
$ok = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep 2
    if (netstat -ano | Select-String ":7780.*LISTENING") { $ok = $true; break }
}
if ($ok) { Write-Host "3) 성공! 서버(7780) 살아남" -ForegroundColor Green }
else { Write-Host "3) 실패 - 최후의 카드: PC 재부팅 (켜지면 자동 복구됨)" -ForegroundColor Red }
if (netstat -ano | Select-String ":7443.*LISTENING") { Write-Host "   보안포트(7443)도 정상" -ForegroundColor Green }
else { Write-Host "   보안포트(7443)는 최대 30초 내 자동 부활 예정" }
Write-Host ""
Write-Host "마지막 순서: 폰에서 TapDesk 앱을 완전히 껐다가 다시 실행!" -ForegroundColor Cyan
Read-Host "엔터 누르면 창 닫힘"
