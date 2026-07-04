"""
TapDesk 경비원 (watchdog)
- 30초마다 포트 7778 LISTEN 체크
- 없으면 server.py 자동 재시작 (새 콘솔 창)
- 자기 자신 죽으면 startup 등록된 .vbs 가 PC 켜질 때 다시 살림
"""
import socket
import subprocess
import time
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
SERVER = ROOT / "server.py"
LOG = ROOT / "watchdog.log"
PORT = 7780
CHECK_INTERVAL = 30      # 30초마다 체크
STARTUP_GRACE = 8        # 재시작 후 8초 부팅 대기
SINGLETON_PORT = 7781    # 워치독 자기 자신 중복 실행 방지용


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_listening(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        ok = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        return ok
    except Exception:
        return False


def singleton_guard() -> socket.socket | None:
    """워치독 중복 실행 방지 — 7779 포트 점유로 표시."""
    try:
        g = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        g.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        g.bind(("127.0.0.1", SINGLETON_PORT))
        g.listen(1)
        return g
    except OSError:
        return None


def start_server() -> bool:
    # LocalSystem(Session 0)에서 WTSQueryUserToken으로 활성 사용자 세션 토큰 획득 →
    # CreateProcessAsUser로 Session 1에서 server.py 실행 → 모니터 3개 정상 인식.
    py = PYTHON
    if not py.exists():
        log(f"FATAL: venv 파이썬 없음 — {py}")
        return False
    if not SERVER.exists():
        log(f"FATAL: server.py 없음 — {SERVER}")
        return False

    try:
        import win32ts
        import win32process
        import win32security
        import win32api
        import win32con

        session_id = win32ts.WTSGetActiveConsoleSessionId()
        if session_id == 0xFFFFFFFF:
            raise RuntimeError("활성 콘솔 세션 없음")
        token = win32ts.WTSQueryUserToken(session_id)
        env = win32process.CreateEnvironmentBlock(token, False)
        si = win32process.STARTUPINFO()
        si.dwFlags = win32con.STARTF_USESHOWWINDOW
        si.wShowWindow = win32con.SW_HIDE
        cmd = f'"{py}" "{SERVER}"'
        win32process.CreateProcessAsUser(
            token, None, cmd,
            None, None, False,
            win32process.CREATE_NEW_CONSOLE | win32process.CREATE_UNICODE_ENVIRONMENT,
            env, str(ROOT), si,
        )
        log(f"server.py 재시작 — Session {session_id} (WTSQueryUserToken, 모니터 정상)")
        return True
    except Exception as e:
        log(f"WTS 실행 실패: {e!r} — Session 0 폴백")

    # 폴백: WTS 실패 시 Session 0 직접 실행
    try:
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            [str(py), str(SERVER)],
            cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        log("server.py 재시작 (Session 0 폴백 — 모니터 감지 제한)")
        return True
    except Exception as e:
        log(f"재시작 실패: {e!r}")
        return False


def main() -> int:
    guard = singleton_guard()
    if guard is None:
        log("이미 워치독이 떠 있어 — 종료")
        return 0

    log("===== 경비원 시작 =====")
    log(f"감시: 127.0.0.1:{PORT}  체크 간격: {CHECK_INTERVAL}s  부팅대기: {STARTUP_GRACE}s")

    consecutive_fail = 0
    while True:
        try:
            if is_listening(PORT):
                if consecutive_fail:
                    log("정상 복귀 — server LISTEN 확인")
                    consecutive_fail = 0
            else:
                consecutive_fail += 1
                log(f"⚠️ 7778 죽음 감지 (연속 {consecutive_fail}회) → 재시작")
                if start_server():
                    time.sleep(STARTUP_GRACE)
                else:
                    time.sleep(STARTUP_GRACE * 2)
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log("===== 경비원 종료 (Ctrl+C) =====")
            return 0
        except Exception as e:
            log(f"루프 예외(무시): {e!r}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
