@echo off
:: 관리자 권한 자동 요청
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

set NSSM=C:\Users\GAPER\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe
set PY=E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk\.venv\Scripts\pythonw.exe
set WD=E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk\watchdog.py
set DIR=E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk

echo [1] NSSM 설정 업데이트 중...
"%NSSM%" set TapDesk_7777 Application "%PY%"
"%NSSM%" set TapDesk_7777 AppParameters "\"%WD%\""
"%NSSM%" set TapDesk_7777 AppDirectory "%DIR%"

echo [2] 서비스 중지 중...
"%NSSM%" stop TapDesk_7777 confirm

timeout /t 3 /nobreak >nul

echo [3] 서비스 시작 중...
"%NSSM%" start TapDesk_7777

timeout /t 4 /nobreak >nul

echo [4] 상태 확인...
sc query TapDesk_7777

echo.
echo 완료. 엔터 누르면 닫힘.
pause
