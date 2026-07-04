# MicBubble OTA 자동 설치 스크립트
$ADB = "C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe"
$APK_SRC = "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk\app-debug.apk"
$APK_TMP = "C:\Temp\micbubble.apk"

Write-Host "=== MicBubble OTA Push ==="

# 한글 경로 우회 — 영문 임시 경로로 복사
New-Item -ItemType Directory -Force "C:\Temp" | Out-Null
Copy-Item $APK_SRC $APK_TMP -Force

# USB ADB 직접 설치
$devices = & $ADB devices 2>&1
Write-Host "ADB: $devices"
if ($devices -match "device$") {
    Write-Host "USB 연결 → ADB install"
    $r = & $ADB install -r $APK_TMP 2>&1
    Write-Host $r
    if ("$r" -like "*Success*") { Write-Host "SUCCESS"; exit 0 }
}

# 7780 서버 경유
Write-Host "7780 /push-ota 경유"
$TOKEN = (Get-Content "E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk\token.txt") -join ""
$body = '{"token":"' + $TOKEN.Trim() + '"}'
try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:7780/push-ota" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 90
    Write-Host "결과: $($resp.ok) $($resp.msg)"
} catch {
    Write-Host "ERROR: $_"
}
