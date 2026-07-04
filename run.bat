@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem venv 없으면 생성 + 의존성 설치 (activate.bat 의존 안 함 — 한글 경로 안전)
if not exist ".venv\Scripts\python.exe" (
    echo [setup] venv 생성 + 의존성 설치...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

rem 폰 USB 연결 시 — 폰이 PC 를 localhost 로 보도록 터널 (음성 기능엔 secure context 필요)
echo [USB] 폰 USB 터널 설정 (USB 케이블 연결돼 있으면 자동)...
"C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe" reverse tcp:7780 tcp:7780

echo.
echo === 서버 시작 — 이 창을 닫지 마세요 (창=서버) ===
".venv\Scripts\python.exe" server.py
pause
