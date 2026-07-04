@echo off
chcp 65001 >nul
:: =====================================================
::  TapDesk 터보엔진 적용 (2026-07-03)
::  1) 방화벽 7443 열기 (wss TLS - 폰 WebCodecs용)
::  2) 서버 재시작 (새 코드 로드 - watchdog이 자동 부활)
::  더블클릭 1번이면 끝. 관리자 권한 자동 요청.
:: =====================================================
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo [1] 방화벽 7443 인바운드 허용...
netsh advfirewall firewall delete rule name="TapDesk 7443 (turbo wss)" >nul 2>&1
netsh advfirewall firewall add rule name="TapDesk 7443 (turbo wss)" dir=in action=allow protocol=TCP localport=7443

echo [2] 7780 서버 프로세스 찾아서 재시작...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :7780 ^| findstr LISTENING') do set SPID=%%p
if defined SPID (
    echo     PID %SPID% 종료 - watchdog(7781)이 새 코드로 자동 부활시킴
    taskkill /F /PID %SPID%
) else (
    echo     7780 리스너 없음 - watchdog이 곧 띄움
)

echo [3] 12초 대기 (재기동)...
timeout /t 12 /nobreak >nul

echo [4] 결과 확인:
netstat -ano | findstr ":7780 :7443" | findstr LISTENING
echo.
echo 위에 7780 하고 7443 둘 다 보이면 성공!
echo (7443 없으면 서버 콘솔 로그 확인 - _ts.crt 인증서 문제)
echo.
pause
