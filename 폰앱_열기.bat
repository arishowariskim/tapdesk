@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

set ADB=C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe

echo === PC원격 네이티브 앱 열기 (USB) ===

rem 1) 폰 USB 연결 확인
"%ADB%" get-state >nul 2>&1
if errorlevel 1 (
    echo [에러] 폰이 USB로 안 잡힘. 케이블 연결 + USB 디버깅 허용하고 다시 실행.
    pause
    exit /b 1
)

rem 2) 서버(7780) 떠 있는지 확인 — 없으면 화면이 하얗게 뜨므로 경고
netstat -ano | findstr ":7780" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [경고] 7780 서버가 안 떠 있음. run.bat 먼저 실행해야 PC 화면이 보임. 그래도 앱은 켭니다.
)

rem 3) localhost 터널 재설정 — 하얀 화면 방지의 핵심
echo [USB] adb reverse tcp:7780 설정...
"%ADB%" reverse tcp:7780 tcp:7780

rem 4) 앱 강제종료 후 재실행 — WebView 를 새로 로드(터널 잡힌 뒤 접속)
echo [앱] PC원격 재시작...
"%ADB%" shell am force-stop com.aris.pcremote
"%ADB%" shell am start -n com.aris.pcremote/com.aris.silenttoggle.MainActivity >nul

echo.
echo 완료 — 폰에 PC 화면이 떠야 정상.
echo (또 하얀 화면이면 이 .bat 을 한 번 더 실행하면 됩니다.)
ping -n 4 127.0.0.1 >nul
