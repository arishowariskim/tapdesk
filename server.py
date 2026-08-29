"""
폴드5 PC 원격제어 — 경량 백엔드 (TapDesk)

FastAPI + WebSocket 1파일.
최적화:
  ① 안 변한 프레임 전송 스킵
  ② 폰이 보는 영역(뷰포트 rect)만 크롭 캡처
  ③ 폰 액정 픽셀 크기에 딱 맞춰 전송 — 과전송 0
  ④ 빠른 리사이즈(BILINEAR)
  ⑤ 큐 없는 직렬 루프 — 지연 누적(버퍼링) 구조상 불가

실행:  run.bat  또는  python server.py
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# DPI 인식 — 모니터마다 배율 다를 때 캡처 좌표 어긋남 방지 (깜빡임 무관 확인됨 → 복귀).
if sys.platform == "win32":
    try:
        import ctypes as _dpi_c
        try:
            _dpi_c.windll.user32.SetProcessDpiAwarenessContext(_dpi_c.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                _dpi_c.windll.shcore.SetProcessDpiAwareness(2)   # 폴백: PER_MONITOR_AWARE
            except Exception:
                _dpi_c.windll.user32.SetProcessDPIAware()        # 최후 폴백: system aware
    except Exception:
        pass

import mss
import pyautogui
import pyperclip
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image, ImageDraw

try:
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.0

# ───────────────────────── 설정 ─────────────────────────
ROOT = Path(__file__).resolve().parent
# ★ PHONE_HTML은 절대경로 고정 — 어느 경로에서 server.py가 실행돼도 18_TapDesk만 읽음
CANONICAL = Path(r"E:\1._AI_SaaS 제작소\1.SaaS_제품\18_TapDesk")
TOKEN_FILE = CANONICAL / "token.txt"
PHONE_HTML = CANONICAL / "phone.html"
ICON_FILE = CANONICAL / "icon.png"
LOG_FILE = CANONICAL / "agent.log"

PORT = int(os.environ.get("PORT", 7780))
TLS_PORT = int(os.environ.get("TLS_PORT", 7443))   # wss(터보 WebCodecs용) — _ts.crt/_ts.key 있을 때만
FPS = 30            # dxcam GPU 캡처 → 30fps 가능
MAX_WIDTH = 1600
JPEG_QUALITY = 55

# LTE 경량 모드
LTE_FPS = 20        # turbojpeg 고속 인코딩 → 20fps 가능
LTE_MAX_WIDTH = 1280
LTE_QUALITY = 58    # 역이용: fallback 화질 상향 (준HD)

# 소리 — PC 시스템 사운드를 폰으로 (WASAPI 루프백)
AUDIO_SR = 24000       # 샘플레이트 (mono)
AUDIO_CHUNK = 1920     # 청크 크기 (80ms)

# 런타임 상태 — 단일 사용자
#  view = 모니터 위 사각형(0~1 비율) + 폰이 원하는 출력 가로 w(px)
STATE = {
    "monitor": 1,
    "monitor_key": "",   # EDID 하드웨어 키(LAP/모델_UID) = 진실. 번호는 여기서 파생.
    "view": {"rx1": 0.0, "ry1": 0.0, "rx2": 1.0, "ry2": 1.0, "w": 1280},
    "lte": False,
    "audio": False,
    "video": False,
    "cap_target": "phone",  # 📸 캡처 대상: phone/mon0/mon1/mon2
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fold5")

# ⚖️ 색판정 기준 (명령서 제11조·23조) — 기준파일 없으면 프로브(자동판정) 기동 거부
_JUDGE_FILE = CANONICAL / "_judge" / "turbo_color.judge.json"
_judge = None
_judge_fail_streak = 0
try:
    _judge = json.loads(_JUDGE_FILE.read_text(encoding="utf-8"))
    log.info(f"⚖️ 색판정 기준 로드: PASS Δ≤{_judge['pass_max_delta']} / "
             f"FAIL Δ>{_judge['fail_delta']}×{_judge['fail_streak']}연속 → 자동 JPEG 폴백")
except Exception as _je:
    _judge = None
    log.warning(f"⚖️ judge.json 없음/불량 → 색검증 프로브 기동 거부 (제23조): {_je}")

# 🛩 블랙박스 — 네이티브 급사(액세스 위반 등) 시 전 스레드 스택을 남김 (2026-07-04 원인불명 프로세스 사망 추적)
import faulthandler
try:
    _crash_f = open(CANONICAL / "_crash_trace.log", "a", encoding="utf-8", errors="replace")
    _crash_f.write(f"\n===== 기동 {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} =====\n")
    _crash_f.flush()
    faulthandler.enable(_crash_f, all_threads=True)
except Exception:
    pass

# 소리 — PC 시스템 사운드 캡처 (WASAPI 루프백) → 큐 → 폰
audio_q: "queue.Queue" = queue.Queue(maxsize=8)    # /ws 폴백 채널
audio_q2: "queue.Queue" = queue.Queue(maxsize=8)   # /ws-audio 전용 채널 (JPEG 락 분리)

def video_detect_thread():
    """PC 영상 플레이어(VLC·PotPlayer·MPC·mpv 등) 창 제목으로 감지 → STATE['video']
    새로 떴을 때 STATE['view'] 영역에 맞춰 자동 배치 (폰 줌 화면에 꽉 차게)."""
    import win32gui
    PATTERNS = ("VLC media player", "PotPlayer", "MPC-HC", "MPC-BE",
                "mpv ", "- mpv", "Windows Media Player", "GOM Player",
                "KMPlayer", "BS.Player")
    last = False
    placed = set()                                       # 이미 view에 맞춰준 hwnd 기록 (중복 이동 방지)
    while True:
        try:
            found_hwnd = [0]
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t and any(p in t for p in PATTERNS):
                        found_hwnd[0] = hwnd
            win32gui.EnumWindows(cb, None)
            hwnd = found_hwnd[0]
            playing = bool(hwnd)
            if playing != last:
                last = playing
                STATE["video"] = playing
                log.info(f"🎬 영상 플레이어 {'감지' if playing else '닫힘'}")
                if not playing:
                    placed.clear()
            # 새로 뜬 플레이어 창 → STATE['view'] 영역에 맞춰 이동
            if hwnd and hwnd not in placed:
                try:
                    v = STATE.get("view") or {}
                    sw = ctypes.windll.user32.GetSystemMetrics(0)
                    sh = ctypes.windll.user32.GetSystemMetrics(1)
                    rx1 = max(0.0, min(1.0, float(v.get("rx1", 0.17))))
                    ry1 = max(0.0, min(1.0, float(v.get("ry1", 0.14))))
                    rx2 = max(0.0, min(1.0, float(v.get("rx2", 0.83))))
                    ry2 = max(0.0, min(1.0, float(v.get("ry2", 0.86))))
                    x = int(sw * rx1); y = int(sh * ry1)
                    w = max(320, int(sw * (rx2 - rx1)))
                    h = max(240, int(sh * (ry2 - ry1)))
                    win32gui.MoveWindow(hwnd, x, y, w, h, True)
                    placed.add(hwnd)
                    log.info(f"🎬 플레이어 view 맞춤 {x},{y} {w}x{h}")
                except Exception as e:
                    log.warning(f"플레이어 배치 실패: {e}")
        except Exception as e:
            log.warning(f"영상 감지 오류: {e}")
        time.sleep(1.0)


def audio_capture_thread():
    """STATE['audio']가 켜진 동안 PC 시스템 사운드를 캡처해 큐에 넣는다."""
    import numpy as np
    import soundcard as sc
    while True:
        if not STATE.get("audio"):
            time.sleep(0.2)
            continue
        try:
            mic = sc.get_microphone(str(sc.default_speaker().name), include_loopback=True)
            with mic.recorder(samplerate=AUDIO_SR, channels=1, blocksize=AUDIO_CHUNK) as rec:
                log.info("🔊 오디오 캡처 시작")
                while STATE.get("audio"):
                    data = rec.record(numframes=AUDIO_CHUNK)
                    pcm = (np.clip(data[:, 0], -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                    try:
                        audio_q.put_nowait(pcm)
                    except queue.Full:
                        pass
                    try:
                        audio_q2.put_nowait(pcm)   # 전용 채널에도 복사
                    except queue.Full:
                        pass
            log.info("🔊 오디오 캡처 정지")
        except Exception as e:
            log.warning(f"오디오 캡처 오류: {e}")
            time.sleep(1.0)


def load_or_create_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    log.info("새 토큰 생성 → token.txt")
    return t


TOKEN = load_or_create_token()

# ─────────────────── Win32 입력 (16번 검증됨) ───────────────────
_user32 = ctypes.windll.user32 if sys.platform == "win32" else None

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
INPUT_MOUSE = 0


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP   = 0x0002
_INPUT_KEYBOARD    = 1
_extra_kb = ctypes.c_ulong(0)


def _type_unicode_direct(text: str) -> None:
    """클립보드 없이 SendInput(KEYEVENTF_UNICODE)으로 직접 타이핑. 한글 포함 모든 유니코드 지원."""
    if not text or _user32 is None:
        return
    inputs = []
    for ch in text:
        scan = ord(ch)
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.ki = _KEYBDINPUT(0, scan, flags, 0, ctypes.pointer(_extra_kb))
            inputs.append(inp)
    if inputs:
        arr = (_INPUT * len(inputs))(*inputs)
        _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _send_backspace(n: int) -> None:
    """backspace N번 — pyautogui 대신 SendInput으로 즉각 처리."""
    if n <= 0 or _user32 is None:
        return
    inputs = []
    for _ in range(n):
        for flags in (0, 2):  # keydown, keyup
            inp = _INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.ki = _KEYBDINPUT(0x08, 0x0E, flags, 0, ctypes.pointer(_extra_kb))
            inputs.append(inp)
    arr = (_INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _send_mouse(flags: int, data: int = 0) -> None:
    if _user32 is None:
        return
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = _MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, None)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def fast_set_pos(x: int, y: int) -> None:
    if _user32 is not None:
        _user32.SetCursorPos(int(x), int(y))


def fast_click(button: str = "left", ctrl: bool = False, shift: bool = False) -> None:
    # ctrl=콕콕 다중선택 / shift=범위선택 (PC 파일 선택 그대로)
    if _user32 is not None:
        if ctrl:  _user32.keybd_event(0x11, 0, 0, 0)   # VK_CONTROL down
        if shift: _user32.keybd_event(0x10, 0, 0, 0)   # VK_SHIFT down
        if ctrl or shift: time.sleep(0.01)
    if button == "right":
        _send_mouse(MOUSEEVENTF_RIGHTDOWN)
        _send_mouse(MOUSEEVENTF_RIGHTUP)
    else:
        _send_mouse(MOUSEEVENTF_LEFTDOWN)
        _send_mouse(MOUSEEVENTF_LEFTUP)
    if _user32 is not None:
        if ctrl or shift: time.sleep(0.01)
        if shift: _user32.keybd_event(0x10, 0, 2, 0)   # VK_SHIFT up
        if ctrl:  _user32.keybd_event(0x11, 0, 2, 0)   # VK_CONTROL up


def fast_mouse_down(button: str = "left") -> None:
    _send_mouse(MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN)


def fast_mouse_up(button: str = "left") -> None:
    _send_mouse(MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP)


def fast_scroll(delta: int) -> None:
    _send_mouse(MOUSEEVENTF_WHEEL, data=int(delta))


def secure_desktop_active() -> bool:
    """🛡 UAC 등 보안 데스크톱이 입력을 잡고 있는가 (2026-08-07 대표님 발주 "사용불가 원인 중앙 알림").
    보안 데스크톱이 뜨면 SendInput·화면갱신이 통째로 막혀 원격(폰)에서는 이유 없이 먹통으로 보인다 —
    그 순간을 감지해 폰에 pc_block 신호를 보내기 위한 판정 함수. ★서버 재시작 후부터 유효."""
    if _user32 is None:
        return False
    try:
        h = _user32.OpenInputDesktop(0, False, 0x0001)   # DESKTOP_READOBJECTS
        if not h:
            return True    # 입력 데스크톱 자체를 못 연다 = 보안 데스크톱(UAC) 활성
        try:
            buf = ctypes.create_unicode_buffer(64)
            need = ctypes.c_ulong(0)   # ★DWORD=c_ulong — ctypes.wintypes는 이 파일에 임포트 안 돼 있어 AttributeError로 감지가 조용히 죽었다(2026-08-07 1차 실사격 불발 원인)
            if _user32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)):   # UOI_NAME=2
                return buf.value.lower() != "default"    # 평상시 입력 데스크톱 이름 = "Default"
            return False
        finally:
            _user32.CloseDesktop(h)
    except Exception:
        return False


# ───────────────────────── 화면 캡처 ─────────────────────────
_mss_local = threading.local()
_mss_generation = 0   # 모니터 재초기화 시 +1 → 각 스레드가 다음 get_mss() 호출 시 자동 재생성


def get_mss() -> "mss.MSS":
    gen = getattr(_mss_local, "gen", -1)
    if gen != _mss_generation:
        # 세대 바뀜 → 이 스레드 mss 재생성 (자폭 없이 모니터 재초기화)
        old = getattr(_mss_local, "inst", None)
        if old:
            try: old.close()
            except Exception: pass
        inst = mss.MSS()
        _mss_local.inst = inst
        _mss_local.gen = _mss_generation
        return inst
    inst = getattr(_mss_local, "inst", None)
    if inst is None:
        inst = mss.MSS()
        _mss_local.inst = inst
        _mss_local.gen = _mss_generation
    return inst


# dxcam 멀티-GPU 레지스트리 (GPU DDA 캡처)
# [실측 2026-07-03] 외장 4K 2대는 device_idx=1 (별도 어댑터) — 기존엔 primary만 dxcam이라
# 외장 모니터가 전부 mss(148ms)로 갔음. 데스크톱 좌표→(device,output) 매핑으로 전 모니터 GPU 캡처.
# dxcam Device1 실측 0.2ms/호출. 매핑 실패·에러 시 mss 폴백 (기존과 동일).
try:
    import dxcam as _dxcam_mod
except Exception as _dxcam_import_err:
    _dxcam_mod = None  # COM 오류 등 — mss 폴백
_dxcam_inst = None                # (구 호환) primary 인스턴스 별칭
_dxcam_insts = {}                 # (device_idx, output_idx) -> 인스턴스 | "FAIL"
_dxcam_rects = None               # [(left, top, right, bottom, dev, out), ...] | "FAIL"
_dxcam_lock = threading.Lock()
_dx_grab_lock = threading.Lock()  # grab 직렬화 — 같은 인스턴스 동시 grab = D3D INVALID_CALL (2026-07-03 실증)

# turbojpeg 싱글턴 (PIL WebP 대비 5배 빠른 JPEG 인코딩)
from turbojpeg import TurboJPEG, TJPF_RGB, TJPF_BGRA
_turbo_jpeg = TurboJPEG()


def _dxcam_rect_map():
    """DXGI 어댑터·출력 열거 → 데스크톱 절대좌표 매핑. 1회 캐시."""
    global _dxcam_rects
    if _dxcam_rects is not None:
        return [] if _dxcam_rects == "FAIL" else _dxcam_rects
    try:
        from dxcam.util.io import enum_dxgi_adapters, enum_dxgi_outputs
        from dxcam.core import Output
        rects = []
        for di, ad in enumerate(enum_dxgi_adapters()):
            try:
                for oi, po in enumerate(enum_dxgi_outputs(ad)):
                    r = Output(po).desc.DesktopCoordinates
                    rects.append((r.left, r.top, r.right, r.bottom, di, oi))
            except Exception:
                continue
        _dxcam_rects = rects if rects else "FAIL"
        if rects:
            log.info("🗺 dxcam 모니터 맵: " + ", ".join(
                f"dev{d}out{o}=({l},{t})" for l, t, _, _, d, o in rects))
        return rects
    except Exception as e:
        log.warning(f"dxcam 맵 열거 실패 → mss 폴백: {e}")
        _dxcam_rects = "FAIL"
        return []


def _dxcam_reset():
    """캡처 연속 실패 시 재초기화 — 전 인스턴스·맵 폐기 후 다음 호출 때 재생성."""
    global _dxcam_inst, _dxcam_insts, _dxcam_rects
    with _dxcam_lock:
        for v in list(_dxcam_insts.values()):
            try:
                if v not in (None, "FAIL"):
                    del v
            except Exception:
                pass
        _dxcam_insts = {}
        _dxcam_inst = None
        _dxcam_rects = None


def get_dxcam_for(mon: dict):
    """모니터 dict(left/top/width/height) → (dxcam 인스턴스, 출력원점x, 출력원점y).
    해당 모니터가 dxcam 출력에 정확히 매칭될 때만 인스턴스 반환, 아니면 (None,0,0)=mss 폴백."""
    if _dxcam_mod is None:
        return None, 0, 0
    tgt = None
    for l, t, r, b, di, oi in _dxcam_rect_map():
        if l == mon["left"] and t == mon["top"] and (r - l) == mon["width"] and (b - t) == mon["height"]:
            tgt = (di, oi, l, t)
            break
    if tgt is None:
        return None, 0, 0
    di, oi, ox, oy = tgt
    key = (di, oi)
    inst = _dxcam_insts.get(key)
    if inst == "FAIL":
        return None, 0, 0
    if inst is not None:
        return inst, ox, oy
    with _dxcam_lock:
        inst = _dxcam_insts.get(key)
        if inst == "FAIL":
            return None, 0, 0
        if inst is not None:
            return inst, ox, oy
        try:
            inst = _dxcam_mod.create(device_idx=di, output_idx=oi, output_color="BGRA")
            if inst is None:
                _dxcam_insts[key] = "FAIL"
                return None, 0, 0
            _dxcam_insts[key] = inst
            log.info(f"⚡ dxcam 활성화 dev{di}/out{oi} origin=({ox},{oy}) — GPU DDA 캡처")
            return inst, ox, oy
        except Exception as e:
            log.warning(f"dxcam 생성 실패 dev{di}/out{oi} → mss 폴백: {e}")
            _dxcam_insts[key] = "FAIL"
            return None, 0, 0


def get_dxcam():
    """(구 호환) primary(0,0) dxcam. 신규 코드는 get_dxcam_for(mon) 사용."""
    global _dxcam_inst
    for m in get_mss().monitors[1:]:
        if m["left"] == 0 and m["top"] == 0:
            cam, _, _ = get_dxcam_for(m)
            _dxcam_inst = cam
            return cam
    return None


# ── 모니터 고정 식별 (자리가 아니라 ID) — 노트북 항상 1번 + 결정적 순서 → 깜빡임 제거 ──
try:
    import mon_ident
except Exception:
    mon_ident = None

_order_cache = {"rects": None, "ts": 0.0}   # mon_ident.ordered()(느림) 캐시


def _ordered_rects():
    now = time.time()
    if _order_cache["rects"] is not None and (now - _order_cache["ts"]) < 3.0:
        return _order_cache["rects"]
    rects = []
    try:
        if mon_ident is not None:
            rects = [tuple(m["rect"]) for m in mon_ident.ordered()]   # 노트북 먼저, 그다음 위치순
    except Exception:
        rects = []
    _order_cache["rects"] = rects
    _order_cache["ts"] = now
    return rects


def _stable_mons():
    """mss.monitors 를 결정적 순서(1=노트북, 그다음 위치순)로 재배열.
    스레드마다 mss 순서가 달라도 동일 물리모니터→동일 인덱스 → 깜빡임 제거."""
    raw = get_mss().monitors
    phys = list(raw[1:])
    targets = _ordered_rects()
    if targets:
        result, used = [], set()
        for (tl, tt, tw, th) in targets:
            pick = None
            for j, m in enumerate(phys):
                if j in used:
                    continue
                if m["left"] == tl and m["top"] == tt and m["width"] == tw and m["height"] == th:
                    pick = j
                    break
            if pick is None:                       # 정확매칭 실패 → 좌상단 근접
                for j, m in enumerate(phys):
                    if j not in used and abs(m["left"] - tl) <= 4 and abs(m["top"] - tt) <= 4:
                        pick = j
                        break
            if pick is not None:
                used.add(pick)
                result.append(phys[pick])
        for j, m in enumerate(phys):               # 못 잡은 나머지(매칭 실패분) 뒤에
            if j not in used:
                result.append(m)
        if result:
            return [raw[0]] + result
    # 폴백 — 좌표순(주모니터 먼저)
    prim = [m for m in phys if m["left"] == 0 and m["top"] == 0]
    rest = sorted([m for m in phys if not (m["left"] == 0 and m["top"] == 0)],
                  key=lambda m: (m["left"], m["top"]))
    return [raw[0]] + prim + rest


# ───────────────────────── 모니터 하드웨어 고정 키 (EDID) ─────────────────────────
# 줌슬롯·뷰 상태를 "번호"(흔들림) 대신 "하드웨어 지문"에 묶기 위한 키.
#   · 노트북 내장패널 → "LAP" (모니터 다 바꿔도, 재설치해도 영구 불변)
#   · 외부 모니터    → "모델_UID" (예: HYC3200_4352) — 같은 모델 2대도 UID로 구분
_meta_cache = {"meta": None, "ts": 0.0}


def _mon_meta():
    """rect 튜플 → (stable_id, internal) 매핑. 3초 캐시. mon_ident 실패 시 빈 dict."""
    now = time.time()
    if _meta_cache["meta"] is not None and (now - _meta_cache["ts"]) < 3.0:
        return _meta_cache["meta"]
    meta = {}
    try:
        if mon_ident is not None:
            for m in mon_ident.ordered():
                meta[tuple(m["rect"])] = (m.get("stable_id", ""), bool(m.get("internal")))
    except Exception:
        pass
    _meta_cache["meta"] = meta
    _meta_cache["ts"] = now
    return meta


def _key_from_id(stable_id: str, internal: bool) -> str:
    """EDID 고정 ID → 짧고 안정적인 키. 내장패널=LAP, 외부=모델_UID."""
    if internal:
        return "LAP"
    import re
    sid = stable_id or ""
    mdl = re.search(r"DISPLAY#([^#]+)#", sid)
    uid = re.search(r"UID(\d+)", sid)
    tok = mdl.group(1) if mdl else "MON"
    if uid:
        tok += "_" + uid.group(1)
    return tok or "MON"


def _mon_key(mon: dict) -> str:
    """mss 모니터 dict → 하드웨어 고정 키. 못 찾으면 '' (폰이 번호 폴백)."""
    if not mon:
        return ""
    meta = _mon_meta()
    rect = (mon.get("left"), mon.get("top"), mon.get("width"), mon.get("height"))
    if rect in meta:
        return _key_from_id(*meta[rect])
    for (tl, tt, tw, th), (sid, internal) in meta.items():     # 근접 매칭
        if abs(mon.get("left", 0) - tl) <= 4 and abs(mon.get("top", 0) - tt) <= 4 \
           and mon.get("width") == tw and mon.get("height") == th:
            return _key_from_id(sid, internal)
    return ""


def _mon_key_num(n) -> str:
    """안정 모니터 번호 → 하드웨어 고정 키."""
    try:
        return _mon_key(_mon(int(n)))
    except Exception:
        return ""


def _mon(monitor_id: int):
    mons = _stable_mons()
    mid = monitor_id if 0 <= monitor_id < len(mons) else 1
    return mons[mid]


def _view_to_screen(view, min_w=280, min_h=200):
    """현재 스트리밍 모니터 기준으로 view(rx1..ry2) → 절대 픽셀 (x, y, w, h).
    GetSystemMetrics(0/1)은 주 모니터만 반환해 멀티모니터에서 틀림 — 이 함수로 교체."""
    mon = _mon(STATE["monitor"])
    if view and all(k in view for k in ("rx1", "ry1", "rx2", "ry2")):
        rx1 = max(0.0, min(1.0, float(view["rx1"])))
        ry1 = max(0.0, min(1.0, float(view["ry1"])))
        rx2 = max(0.0, min(1.0, float(view["rx2"])))
        ry2 = max(0.0, min(1.0, float(view["ry2"])))
        x = int(mon["left"] + mon["width"] * rx1)
        y = int(mon["top"] + mon["height"] * ry1)
        w = max(min_w, int(mon["width"] * (rx2 - rx1)))
        h = max(min_h, int(mon["height"] * (ry2 - ry1)))
    else:
        w = int(mon["width"] * 0.66)
        h = int(mon["height"] * 0.72)
        x = mon["left"] + (mon["width"] - w) // 2
        y = mon["top"] + (mon["height"] - h) // 2
    return x, y, w, h


def _idx_by_key(key: str):
    """EDID 하드웨어 키 → 현재 안정 번호. 못 찾으면 None (모니터가 빠졌거나 다른 환경)."""
    if not key:
        return None
    mons = _stable_mons()
    for i in range(1, len(mons)):
        if _mon_key(mons[i]) == key:
            return i
    return None


def _resync_monitor() -> int:
    """STATE['monitor_key'](진실)에 맞춰 STATE['monitor'] 번호를 현재 환경 기준으로 재계산.
    키가 없으면 현재 번호의 키를 채워 넣고, 키 모니터가 사라졌으면 주모니터(1)로 graceful fallback.
    반환: 동기화된 안정 번호. — 이게 '번호로 새던 꼬임'을 막는 핵심."""
    key = STATE.get("monitor_key") or ""
    if not key:
        # 키 미설정(최초/구버전) → 현재 번호에서 키를 역으로 채움
        cur = STATE.get("monitor", 1)
        STATE["monitor_key"] = _mon_key_num(cur) or ""
        return cur
    idx = _idx_by_key(key)
    if idx is None:
        # 이 환경엔 그 모니터가 없음 → 주모니터로 안전 폴백 (키는 1번 키로 갱신)
        STATE["monitor"] = 1
        STATE["monitor_key"] = _mon_key_num(1) or ""
        return 1
    STATE["monitor"] = idx
    return idx


# ─────────── 컨텍스트 자동 도크 — PC 활성 창 종류 감지 ───────────
# 폰 도크가 지금 보는 화면(파일/웹/영상)에 맞는 버튼 세트로 자동 교체되게.
def detect_context() -> str:
    """활성 창 종류 → 'file' / 'web' / 'video' / 'default'."""
    if not HAS_WIN32:
        return "default"
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "default"
        cls = win32gui.GetClassName(hwnd) or ""
        title = (win32gui.GetWindowText(hwnd) or "").lower()
        if cls == "CabinetWClass":                       # 파일 탐색기
            return "file"
        if "Chrome_WidgetWin" in cls:                    # 크롬·엣지·웨일 (크로미움)
            if any(v in title for v in ("youtube", "- youtube", "넷플릭스", "netflix")):
                return "video"
            return "web"
        if any(v in title for v in ("vlc", "potplayer", "팟플레이어", "곰플레이어",
                                    "mpc-hc", " mpv", "windows media")):
            return "video"
    except Exception:
        pass
    return "default"


def win_to_mss(win_no: int) -> int:
    """폰 모니터 번호 = 내부 안정 인덱스 (_stable_mons 가 이미 1=노트북 고정 순서)."""
    try:
        return int(win_no)
    except Exception:
        return 1


def mss_to_win(idx: int) -> int:
    try:
        return int(idx)
    except Exception:
        return 1


def monitor_list() -> list:
    s = get_mss()
    try:                                           # 모니터 추가/제거 감지 → mss 재생성 + 순서캐시 무효화
        real = ctypes.windll.user32.GetSystemMetrics(80)   # SM_CMONITORS = 활성 모니터 수
        if real >= 1 and (len(s.monitors) - 1) != real:
            try: s.close()
            except Exception: pass
            global _mss_generation
            _mss_generation += 1
            s = mss.MSS()
            _mss_local.inst = s
            _mss_local.gen = _mss_generation
            _order_cache["rects"] = None           # 구성 바뀜 → 고정ID 순서 재계산
            _meta_cache["meta"] = None             # 하드웨어 키 매핑도 재계산
            STATE["monitor"] = 1                   # 모니터 환경 변화 → 주모니터로 리셋
            STATE["monitor_key"] = ""              # 키 초기화 (_resync_monitor 가 재설정)
            STATE["view"] = {"rx1": 0.0, "ry1": 0.0, "rx2": 1.0, "ry2": 1.0,
                             "w": STATE["view"].get("w", 1280)}
    except Exception:
        pass
    result = []
    mons = _stable_mons()                          # [0]=전체, [1..]=노트북 먼저 고정 순서
    for i, m in enumerate(mons[1:], start=1):
        item = {"id": i, "w": m["width"], "h": m["height"],   # id = 안정 번호(1=노트북)
                "key": _mon_key(m)}                           # key = 하드웨어 고정 키(LAP/모델_UID)
        try:                                       # 각 모니터 작은 썸네일 (선택 미리보기용)
            raw = s.grab(m)
            img = Image.frombytes("RGB", raw.size, raw.rgb)
            img.thumbnail((240, 150))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=55)
            item["thumb"] = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
        result.append(item)
    return result


def _clamp01(v) -> float:
    return max(0.0, min(1.0, float(v)))


def crop_box(view: dict) -> dict:
    """뷰포트 rect(0~1) → mss 캡처 박스(절대 픽셀)."""
    # ⚠️ 매 프레임 _resync_monitor() 제거 — 캡처 영역이 흔들려 깜빡임 유발했음(모니터 전면수정 부작용).
    #    hotplug/도킹 추종은 thumbs 핸들러(5초 주기)의 _resync_monitor()로 충분.
    m = _mon(STATE["monitor"])
    rx1 = _clamp01(view.get("rx1", 0.0))
    ry1 = _clamp01(view.get("ry1", 0.0))
    rx2 = _clamp01(view.get("rx2", 1.0))
    ry2 = _clamp01(view.get("ry2", 1.0))
    if rx2 - rx1 < 0.02:
        rx2 = min(1.0, rx1 + 0.02)
    if ry2 - ry1 < 0.02:
        ry2 = min(1.0, ry1 + 0.02)
    return {
        "left": m["left"] + int(m["width"] * rx1),
        "top": m["top"] + int(m["height"] * ry1),
        "width": max(8, int(m["width"] * (rx2 - rx1))),
        "height": max(8, int(m["height"] * (ry2 - ry1))),
    }


_fm = {"n": 0, "bytes": 0, "enc": 0.0}             # 프레임 측정 누적 (최적화 진단용, 기능 영향 0)


def _frame_measure(nbytes: int, w: int, h: int, lte: bool, enc_ms: float) -> None:
    _fm["n"] += 1
    _fm["bytes"] += nbytes
    _fm["enc"] += enc_ms
    if _fm["n"] >= 30:
        avg_kb = _fm["bytes"] / _fm["n"] / 1024
        avg_enc = _fm["enc"] / _fm["n"]
        log.info(f"📊 프레임 평균 {avg_kb:.1f}KB · 인코딩 {avg_enc:.1f}ms · {w}x{h} · lte={lte} ({_fm['n']}장)")
        _fm["n"] = 0; _fm["bytes"] = 0; _fm["enc"] = 0.0


def grab_region(view: dict, last_hash):
    """뷰포트 영역만 캡처. 안 변했으면 (None,hash). 출력은 폰 액정 크기에 맞춤.
    primary(0,0)=dxcam(GPU DDA ~4ms), 그 외=mss. 둘 다 BGRA numpy → turbojpeg 직통 (공통 인코딩)."""
    import numpy as _np
    m = _mon(STATE["monitor"])
    box = crop_box(view)
    # [2026-07-03] 전 모니터 dxcam GPU 캡처 — 좌표맵으로 (device,output) 매칭, 실패 시 mss 폴백.
    cam, _ox, _oy = get_dxcam_for(m)
    arr = None
    if cam is not None:
        # dxcam region은 해당 출력 원점 기준 상대좌표. BGRA (H,W,4)
        region = (box["left"] - _ox, box["top"] - _oy,
                  box["left"] - _ox + box["width"], box["top"] - _oy + box["height"])
        # [2026-07-08 질식수술] 락 무한대기 → to_thread 풀 고갈 → 서버 6초+ 무응답(경비원 🧊 반복)의 뿌리.
        # 1초 초과면 그 dxcam은 병든 것 — 리셋 걸고 이번 프레임은 mss로.
        if _dx_grab_lock.acquire(timeout=1.0):
            try:
                raw_np = cam.grab(region=region)
            finally:
                _dx_grab_lock.release()
            if raw_np is None:
                return None, last_hash          # dxcam이 변화 없으면 None 반환 (정지화면 스킵)
            arr = raw_np                        # 이미 BGRA numpy — 변환 0
        else:
            log.warning("🧊 dxcam 락 1초 초과 — 질식 방지: dxcam 리셋 + 이번 프레임 mss 폴백")
            _dxcam_reset()
    if arr is None:
        # mss — numpy 직통 (BGRA). zero-copy: frombuffer로 내부 버퍼 직접 참조
        raw = get_mss().grab(box)
        arr = _np.frombuffer(raw.raw, _np.uint8).reshape((raw.height, raw.width, 4))
    # 균등 샘플 해시 (~0.1ms): 64×64 격자로 ~4096픽셀 샘플 — 변화 없으면 전송 스킵
    h = hash(arr[::max(1, arr.shape[0] // 64), ::max(1, arr.shape[1] // 64)].tobytes())
    if h == last_hash:
        return None, h
    # ── 공통: BGRA arr → 리사이즈 → turbojpeg BGRA 직통 인코딩 (dxcam·mss 동일) ──
    lte = STATE.get("lte", False)
    cap = STATE.get("q_width", LTE_MAX_WIDTH if lte else MAX_WIDTH)
    quality = STATE.get("q_quality", LTE_QUALITY if lte else JPEG_QUALITY)
    tw = max(160, min(int(view.get("w", 1280)), cap))
    if arr.shape[1] > tw:
        nh = max(1, round(arr.shape[0] * tw / arr.shape[1]))
        # PIL resize: BGRA(4채널) NEAREST (색상 무관, 크기만). 바이트순서 BGRA 유지
        arr = _np.asarray(Image.fromarray(arr).resize((tw, nh), Image.NEAREST))
    _t0 = time.time()
    # BGRA → JPEG 직통 (rgb 변환 0번)
    data = _turbo_jpeg.encode(arr, quality=quality, pixel_format=TJPF_BGRA)
    _frame_measure(len(data), arr.shape[1], arr.shape[0], lte, (time.time() - _t0) * 1000)
    return data, h


# ───────────────────────── 🚀 터보 엔진 (H.264 NVENC → 폰 WebCodecs) ─────────────────────────
# [2026-07-03 실측] 4090 NVENC 4K60=166fps(프레임당 6ms) · 폰 LTE Tailscale 직결 RTT 44ms.
# 원칙: 기존 JPEG 경로 0수정 동결. 폰이 {"type":"turbo_mode","on":true}를 보낼 때만 lazy 전환.
# 모니터 풀프레임(네이티브 해상도)을 항상 보냄 → 폰 줌인/아웃 = canvas 변환만(서버 왕복 0).
import subprocess as _sp
import shutil as _shutil

TURBO_FPS = 30
TURBO_KBPS = 20000     # 4K 텍스트 선명도 확보 — LTE 직결(다운 50~150Mbps) 여유 안
_FFMPEG = _shutil.which("ffmpeg") or (
    r"C:\Users\GAPER\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe")

_AU_START3 = b"\x00\x00\x01"


def _iter_nals(buf: bytes):
    """Annex-B 버퍼 → (nal_start_offset, nal_type) 목록. 마지막 NAL은 다음 시작코드 없어 미완 취급."""
    out = []
    i = buf.find(_AU_START3)
    while i != -1:
        s = i - 1 if i > 0 and buf[i - 1] == 0 else i   # 4바이트 시작코드(00 00 00 01) 보정
        j = buf.find(_AU_START3, i + 3)
        ntype = buf[i + 3] & 0x1F if i + 3 < len(buf) else -1
        out.append((s, ntype))
        i = j
    return out


class _TurboEncoder:
    """dxcam 풀프레임 → ffmpeg NVENC 저지연 파이프 → Annex-B AU(프레임) 큐.
    AUD(NAL 9) 삽입으로 AU 경계 결정적 파싱. 소비자 5초 없으면 자가 종료(누수 방지)."""

    def __init__(self, mon: dict, fps: int, kbps: int):
        self.mon = dict(mon)
        self.fps = int(fps)
        self.kbps = int(kbps)
        self.w = mon["width"] & ~1
        self.h = mon["height"] & ~1
        self.au_q: "queue.Queue" = queue.Queue(maxsize=8)
        self.alive = True
        self.last_get = time.time()
        self._dropping = False   # 큐 포화로 드랍 시작 → 다음 키프레임까지 통째 스킵(참조체인 보호)
        self.last_frame = None   # 최근 원본 프레임(BGRA) — 🎨 색검증 프로브용
        self._wrote = 0          # 📈 심박 계측 — 기록한 프레임 수
        self._read_aus = 0       # 📈 심박 계측 — 판독한 AU 수
        self.last_read = time.time()   # 판독 심박 시각 — 가뭄 판정은 이게 멎었을 때만 (GOP드랍≠가뭄)
        self.idle_grabs = 0      # 연속 무변화 캡처 수 — 프로브는 정지화면(≥judge 기준)에서만
        # [2026-07-04 실측] bufsize=1프레임분(0.03초)이면 키프레임이 굶어 1초마다 연붉은 펄스(R-G 진폭 7.0).
        # 버퍼 1초 + GOP 4초 + spatial-aq → 펄스 진폭 0.0 (lab2_pulse.py 실측)
        bufsize_k = self.kbps
        # [2026-08-29 페이블 실측] 저비트레이트에서 4K 키프레임 1장(수백KB)이 회선을 수 초 통째로 막음
        # (1.2M 수렴 후에도 줌 2~7초 실증 — 병목이 비트레이트가 아니라 키프레임 크기였다).
        # → 3M 이하는 인코딩 해상도를 낮춰 키프레임 자체를 작게. 캡처·커서합성·색프로브는 원본 해상도 유지.
        if self.kbps <= 2000:
            _tw = 1280
        elif self.kbps <= 3000:
            _tw = 1920
        else:
            _tw = self.w
        if _tw < self.w:
            self.ow = _tw & ~1
            self.oh = int(self.h * _tw / self.w) & ~1
        else:
            self.ow, self.oh = self.w, self.h
        # [2026-07-03 색보정] RGB→YUV를 BT.709로 변환하고 스트림에 명시 태깅.
        # 무표기면 폰 디코더가 601/709를 추측 → 검정이 붉게 뜨는 색틀어짐(실증). 범위도 limited로 통일.
        # 화질: p1→p4(4090이면 4K60도 여유), baseline→high(CABAC ~15% 효율), g=1초(밀림 복구 빠르게).
        cmd = [_FFMPEG, "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{self.w}x{self.h}",
               "-r", str(self.fps), "-i", "pipe:0",
               # [2026-07-03 2차] limited(tv)로 보냈더니 폰 디코더가 범위깃발 무시+full 해석 → 검정 들뜸(햇빛 워시).
               # full로 인코딩하면 디코더가 어느 쪽으로 읽어도 검정=0 유지 (틀려도 하이라이트만 살짝 눌림 = 안전한 실패)
               "-vf", f"scale={self.ow}:{self.oh}:out_color_matrix=bt709:out_range=full",
               "-pix_fmt", "yuv420p",
               "-color_range", "pc", "-colorspace", "bt709",
               "-color_primaries", "bt709", "-color_trc", "bt709",
               "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ull",
               # [2026-08-29 페이블 실측] -forced-idr 없으면 nvenc 주기 키가 IDR(NAL 5) 아닌 일반 I로 나와
               # is_key 판정 실패 → 드랍 후 "키 대기"가 영영 안 풀려 송신 0 (저비트레이트에서 실증). IDR 강제.
               "-forced-idr", "1",
               "-profile:v", "high", "-bf", "0", "-g", str(self.fps * 2),
               "-spatial-aq", "1", "-aq-strength", "8",
               "-b:v", f"{self.kbps}k", "-maxrate", f"{self.kbps}k",
               "-bufsize", f"{bufsize_k}k",
               "-bsf:v", ("h264_metadata=aud=insert:colour_primaries=1"
                          ":transfer_characteristics=1:matrix_coefficients=1"
                          ":video_full_range_flag=1"),   # VUI 강제 — full range 명시(폰 색해석 고정)
               "-flush_packets", "1", "-f", "h264", "pipe:1"]
        self._errf = open(CANONICAL / "_turbo_ffmpeg_err.log", "ab")   # 사인 규명용 (DEVNULL 금지)
        self.proc = _sp.Popen(cmd, stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=self._errf,
                              creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._cap_loop, daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()
        log.info(f"🚀 터보 인코더 기동: {self.w}x{self.h}→출력{self.ow}x{self.oh}@{self.fps}fps {self.kbps}kbps "
                 f"mon=({mon['left']},{mon['top']})")

    def matches(self, mon: dict, fps: int, kbps: int) -> bool:
        return (self.mon.get("left") == mon.get("left") and self.mon.get("top") == mon.get("top")
                and self.w == (mon["width"] & ~1) and self.h == (mon["height"] & ~1)
                and self.fps == int(fps) and self.kbps == int(kbps))

    def stop(self):
        self.alive = False
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        log.info("🛑 터보 인코더 종료")

    def probe_at(self, rx: float, ry: float):
        """🎨 원본 프레임 (rx,ry) 비율 지점 8x8 평균 RGB — 폰 캔버스 중앙과 비교(줌 상태 무관)."""
        buf = self.last_frame
        if not buf:
            return None
        import numpy as _np
        a = _np.frombuffer(buf, _np.uint8).reshape((self.h, self.w, 4))
        x = max(0, min(self.w - 8, int(self.w * rx) - 4))
        y = max(0, min(self.h - 8, int(self.h * ry) - 4))
        blk = a[y:y + 8, x:x + 8]
        return [round(float(blk[:, :, 2].mean()), 1),
                round(float(blk[:, :, 1].mean()), 1),
                round(float(blk[:, :, 0].mean()), 1)]

    def get_au(self, timeout: float):
        """(is_key, au_bytes) 또는 None. 소비 시각 갱신(자가종료 타이머 리셋)."""
        self.last_get = time.time()
        try:
            return self.au_q.get(True, timeout)
        except queue.Empty:
            return None

    # ── 캡처 스레드: fps 페이싱으로 풀프레임 → ffmpeg stdin ──
    def _cap_loop(self):
        import numpy as _np
        last_bytes = None
        nbytes = self.w * self.h * 4
        dx_fail = 0            # dxcam 연속 실패 — 10회면 이 세션은 mss로 영구 전환 (검은화면 방지)
        while self.alive:
            t0 = time.time()
            if time.time() - self.last_get > 15.0:     # 소비자 없음 → 자가 종료
                # [04:17 실측] LTE 혼잡 시 send가 수 초 블록 → 5초 룰이 산 인코더를 자살시켜
                # 15초 주기 재기동 루프(=폰 "5초 후에야 줌" 체감)의 진범. 15초로 완화.
                log.info("💤 터보: 15초간 소비자 없음 → 자가 종료")
                self.stop()
                return
            try:
                cam, ox, oy = (None, 0, 0) if dx_fail >= 10 else get_dxcam_for(self.mon)
                arr = None
                if cam is not None:
                    if _dx_grab_lock.acquire(timeout=1.0):   # 동시 grab 방지 + 질식수술(1초 상한)
                        try:
                            arr = cam.grab(region=(0, 0, self.w, self.h))   # 변화 없으면 None
                        finally:
                            _dx_grab_lock.release()
                        dx_fail = 0
                    else:
                        log.warning("🧊 터보 캡처: dxcam 락 1초 초과 → 리셋+이번 프레임 재전송")
                        _dxcam_reset()
                        arr = None
                    self.idle_grabs = self.idle_grabs + 1 if arr is None else 0   # 정지화면 연속 카운트
                else:
                    raw = get_mss().grab({"left": self.mon["left"], "top": self.mon["top"],
                                          "width": self.w, "height": self.h})
                    arr = _np.frombuffer(raw.raw, _np.uint8).reshape((raw.height, raw.width, 4))
                if arr is None and last_bytes is None:
                    # 💧 콜드스타트 마중물 [2026-07-05 실측] — 정지화면이면 dxcam이 None만 반환해
                    # 첫 프레임을 영영 못 받고 인코더가 굶어죽음(가뭄 3연속 사건의 진범, 단독재현 0.94s vs 서버 무한).
                    raw0 = get_mss().grab({"left": self.mon["left"], "top": self.mon["top"],
                                           "width": self.w, "height": self.h})
                    arr = _np.frombuffer(raw0.raw, _np.uint8).reshape((raw0.height, raw0.width, 4))
                    log.info("💧 터보: 정지화면 콜드스타트 → mss 마중물 1프레임")
                if arr is not None:
                    if arr.shape[0] != self.h or arr.shape[1] != self.w:
                        arr = _np.ascontiguousarray(arr[:self.h, :self.w])
                    last_bytes = arr.tobytes()
                if last_bytes is not None and len(last_bytes) == nbytes:
                    self.last_frame = last_bytes        # 🎨 프로브 기준(원본색)
                    self.proc.stdin.write(last_bytes)   # 정지화면도 재전송(비트 거의 0, fps 유지)
                    self._wrote += 1
                    if self._wrote % 90 == 0:           # 📈 심박(3초 주기) — 가뭄 단계 확정용 계측
                        log.info(f"📈 터보 심박: 기록 {self._wrote}프레임 / 판독 {self._read_aus}AU / 큐 {self.au_q.qsize()}")
            except (BrokenPipeError, OSError):
                self.alive = False
                return
            except Exception as e:
                dx_fail += 1
                if dx_fail <= 3 or dx_fail == 10:
                    log.warning(f"터보 캡처 오류({dx_fail}회): {e}")
                if dx_fail == 10:
                    log.warning("터보: dxcam 10연속 실패 → 이 세션 mss 폴백 (느리지만 화면 유지)")
                time.sleep(0.1)
            time.sleep(max(0.0, 1.0 / self.fps - (time.time() - t0)))

    # ── 리더 스레드: stdout Annex-B → AUD 경계로 AU 분리 ──
    def _read_loop(self):
        buf = b""
        while self.alive:
            try:
                chunk = self.proc.stdout.read(65536)
            except Exception:
                break
            if not chunk:
                log.warning(f"터보: ffmpeg stdout EOF (returncode={self.proc.poll()}) — _turbo_ffmpeg_err.log 확인")
                break
            buf += chunk
            while True:
                nals = _iter_nals(buf)
                # AUD(9)가 각 AU의 머리 — 두 번째 AUD가 보이면 그 앞까지가 완성된 AU
                aud_pos = [s for s, t in nals if t == 9]
                if len(aud_pos) < 2:
                    break
                au = buf[aud_pos[0]:aud_pos[1]]
                buf = buf[aud_pos[1]:]
                is_key = any(t == 5 for s, t in _iter_nals(au))
                self._read_aus += 1
                self.last_read = time.time()
                if self._dropping and not is_key:
                    continue                              # 드랍 중 — P프레임 하나만 빠져도 다음 키까지 다 깨지므로 통째 스킵
                try:
                    self.au_q.put_nowait((is_key, au))
                    self._dropping = False
                except queue.Full:
                    self._dropping = True                 # 큐 포화(느린 소비자) → 비우고 다음 키부터 재개
                    self.drops = getattr(self, "drops", 0) + 1   # [2026-08-29 페이블] 혼잡 실측 카운터 — 적응 사다리의 눈
                    try:
                        while True:
                            self.au_q.get_nowait()
                    except queue.Empty:
                        pass
                    if is_key:
                        try:
                            self.au_q.put_nowait((is_key, au))
                            self._dropping = False
                        except queue.Full:
                            pass
        self.alive = False


_turbo_enc = None          # 전역 싱글턴 (단일 사용자)
_turbo_gen = 0             # 인코더 세대 — 폰 재연결 시 turbo_start 재전송·IDR 재기동 판단
_turbo_lock = threading.Lock()   # [2026-07-03 사고] 무락 레이스로 인코더 2개 동시 기동 → 동일 dxcam 동시 grab → D3D INVALID_CALL 폭풍


def _turbo_stop():
    global _turbo_enc
    with _turbo_lock:
        if _turbo_enc is not None:
            try:
                _turbo_enc.stop()
            except Exception:
                pass
            _turbo_enc = None


def _turbo_ensure(mon: dict, fps: int, kbps: int, force_new: bool = False):
    """살아있고 설정 일치하면 재사용, 아니면 재기동. 반환 (인코더, 세대번호). 락 필수."""
    global _turbo_enc, _turbo_gen
    with _turbo_lock:
        e = _turbo_enc
        if force_new or e is None or not e.alive or not e.matches(mon, fps, kbps):
            if e is not None:
                try:
                    e.stop()
                except Exception:
                    pass
            _turbo_enc = e = _TurboEncoder(mon, fps, kbps)
            _turbo_gen += 1
        return e, _turbo_gen


def cursor_ratio(monitor_id: int):
    """PC 마우스 커서 → 모니터 비율 좌표 (rx, ry)."""
    if not HAS_WIN32:
        return None
    try:
        cx, cy = win32api.GetCursorPos()
    except Exception:
        return None
    m = _mon(monitor_id)
    return (round((cx - m["left"]) / m["width"], 4),
            round((cy - m["top"]) / m["height"], 4))


# 표준 시스템 커서 핸들 → 모양 이름 (창 끝 ↔ 등 — 폰이 같은 모양 그리게)
_CURSOR_SHAPES = {}
if HAS_WIN32:
    for _idc, _name in ((win32con.IDC_ARROW, "arrow"), (win32con.IDC_IBEAM, "ibeam"),
                        (win32con.IDC_HAND, "hand"), (win32con.IDC_SIZEWE, "we"),
                        (win32con.IDC_SIZENS, "ns"), (win32con.IDC_SIZENWSE, "nwse"),
                        (win32con.IDC_SIZENESW, "nesw"), (win32con.IDC_SIZEALL, "all")):
        try:
            _CURSOR_SHAPES[win32gui.LoadCursor(0, _idc)] = _name
        except Exception:
            pass


def cursor_shape() -> str:
    """현재 PC 커서 모양 이름. 표준 커서 아니면 'arrow'."""
    if not HAS_WIN32:
        return "arrow"
    try:
        hcur = win32gui.GetCursorInfo()[1]
        return _CURSOR_SHAPES.get(hcur, "arrow")
    except Exception:
        return "arrow"


def resolve_xy(monitor_id: int, rx: float, ry: float) -> tuple:
    m = _mon(monitor_id)
    return (int(m["left"] + m["width"] * rx),
            int(m["top"] + m["height"] * ry))


# ───────────────────────── 이미지 클립보드 붙여넣기 ─────────────────────────
def _paste_image(b64: str):
    """폰에서 받은 base64 JPEG → PC 클립보드(이미지) → Ctrl+V 자동 붙여넣기"""
    import base64, io
    img_bytes = base64.b64decode(b64)
    try:
        import win32clipboard
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        bmp = buf.getvalue()[14:]           # DIB (BMP 파일헤더 14바이트 제거)
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp)
        win32clipboard.CloseClipboard()
        time.sleep(0.06)
        pyautogui.hotkey("ctrl", "v")
    except Exception:
        # fallback: 임시 파일 저장 후 탐색기로 열기
        tmp = Path(r"C:\Users\Public\tapdesk_recv.jpg")
        tmp.write_bytes(img_bytes)
        import subprocess
        subprocess.Popen(["explorer.exe", str(tmp)])


# ───────────────────────── 클립보드 캡처 ─────────────────────────
def open_chrome():
    """크롬 브라우저 실행 + 전체화면(F11) — 폰에서 크게 보이게"""
    launched = False
    for p in (
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ):
        if os.path.exists(p):
            subprocess.Popen([p])
            launched = True
            break
    if not launched:
        subprocess.Popen('start "" chrome', shell=True)
    time.sleep(2.5)                  # 크롬 창 뜰 때까지 대기
    pyautogui.press("f11")           # 전체화면
    log.info("🌐 크롬 실행 + 전체화면(F11)")


def close_chrome():
    """크롬 브라우저 닫기 (정상 종료 — 강제 아님)"""
    subprocess.run(["taskkill", "/im", "chrome.exe"], capture_output=True)
    log.info("🌐 크롬 닫기")


def open_claude(view=None):
    """Claude 데스크탑/PWA 실행 — view 영역에 맞춰 배치."""
    lnk = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Chrome 앱" / "Claude.lnk"
    try:
        if lnk.exists():
            os.startfile(str(lnk))
        else:
            os.system('start "" "https://claude.ai/"')
        time.sleep(1.5)
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        x, y, w, h = _view_to_screen(view, min_w=360, min_h=260)
        win32gui.MoveWindow(hwnd, x, y, w, h, True)
        log.info(f"🤖 Claude mon={STATE['monitor']} {x},{y} {w}x{h}")
    except Exception as e:
        log.warning(f"Claude 실행/배치 실패: {e}")


def _telegram_main_hwnd():
    """Telegram.exe 의 메인 창 hwnd 반환 (없으면 0). 창 제목이 채팅명이라 process 매칭."""
    try:
        import win32gui, win32process
        hwnd_box = [0]
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd): return
            if win32gui.GetWindow(hwnd, 4) != 0: return       # 오너 있는 보조 창 제외
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                   capture_output=True, text=True, timeout=1,
                                   creationflags=0x08000000)
                if "Telegram.exe" in (p.stdout or ""): hwnd_box[0] = hwnd
            except Exception: pass
        win32gui.EnumWindows(cb, None)
        return hwnd_box[0]
    except Exception:
        return 0


def open_telegram(view=None):
    """텔레그램 — 항상 lnk 실행(있으면 frontmost) + 메인 창을 view 영역에 강제 맞춤."""
    try:
        import win32gui
        lnk = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Telegram Desktop" / "Telegram.lnk"
        if lnk.exists(): os.startfile(str(lnk))
        else: subprocess.Popen(["explorer.exe", "tg://"])
        time.sleep(1.4)
        hwnd = _telegram_main_hwnd() or win32gui.GetForegroundWindow()
        if not hwnd: return
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)                       # SW_RESTORE
        x, y, w, h = _view_to_screen(view, min_w=360, min_h=260)
        win32gui.MoveWindow(hwnd, x, y, w, h, True)
        log.info(f"✈ 텔레그램 mon={STATE['monitor']} {x},{y} {w}x{h}")
    except Exception as e:
        log.warning(f"텔레그램 열기 실패: {e}")


def close_telegram():
    """텔레그램 최소화 — 종료 X (대화 손실 방지). 다시 톡 = view 영역으로 복귀."""
    try:
        import win32gui
        hwnd = _telegram_main_hwnd()
        if hwnd:
            win32gui.ShowWindow(hwnd, 6)                       # SW_MINIMIZE
            log.info("✈ 텔레그램 최소화")
    except Exception as e:
        log.warning(f"텔레그램 닫기 실패: {e}")


def show_desktop():
    """Win+D 동등 — 모든 창 minimize 해서 바탕화면 보이게 (토글)."""
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command",
             "(New-Object -ComObject Shell.Application).ToggleDesktop()"],
            creationflags=0x08000000,                     # CREATE_NO_WINDOW
        )
        log.info("🖥 바탕화면 보기 (모든 창 최소화 토글)")
    except Exception as e:
        log.warning(f"바탕화면 보기 실패: {e}")


def open_claude_code(view=None):
    """Microsoft Store Claude 데스크탑 앱(별 아이콘) 실행.
    이미 1개 이상 떠 있으면 사용자 배치(4분할 등) 보존.
    바탕화면 보기로 minimize 된 경우엔 그 창들만 ShowWindow(SW_RESTORE)로 복귀."""
    try:
        import win32gui
        # 1) 이미 있는 Claude 창 — minimize 도 포함해서 모두 찾기
        existing = []
        def cb_scan(hwnd, _):
            if not win32gui.IsWindow(hwnd):
                return
            t = win32gui.GetWindowText(hwnd)
            if t == "Claude" or (t and t.startswith("Claude")):
                existing.append(hwnd)
        win32gui.EnumWindows(cb_scan, None)
        if existing:
            # 2) minimize 된 창만 SW_RESTORE 로 복귀 (위치·크기 유지)
            restored = 0
            for hwnd in existing:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, 9)              # SW_RESTORE
                    restored += 1
            try: win32gui.SetForegroundWindow(existing[0])
            except Exception: pass
            log.info(f"🤖 Claude 이미 있음({len(existing)}개, 복귀 {restored}개) — 배치 보존")
            return
        # 2) 없을 때만 새로 띄우고 view 영역에 맞춤
        subprocess.Popen(
            ["explorer.exe", r"shell:appsFolder\Claude_pzs8sxrjxfjjc!Claude"],
            creationflags=0x08000000,
        )
        time.sleep(1.8)
        hwnd_box = [0]
        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if t == "Claude" or (t and t.startswith("Claude")):
                    hwnd_box[0] = hwnd
        win32gui.EnumWindows(cb, None)
        hwnd = hwnd_box[0] or win32gui.GetForegroundWindow()
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        if view and all(k in view for k in ("rx1","ry1","rx2","ry2")):
            x = int(sw * max(0.0, min(1.0, float(view["rx1"]))))
            y = int(sh * max(0.0, min(1.0, float(view["ry1"]))))
            w = max(360, int(sw * (float(view["rx2"]) - float(view["rx1"]))))
            h = max(260, int(sh * (float(view["ry2"]) - float(view["ry1"]))))
            win32gui.MoveWindow(hwnd, x, y, w, h, True)
            log.info(f"⌨ Claude Code (view 맞춤) {x},{y} {w}x{h}")
        else:
            w, h = int(sw * 0.66), int(sh * 0.72)
            win32gui.MoveWindow(hwnd, (sw - w) // 2, (sh - h) // 2, w, h, True)
            log.info("⌨ Claude Code (중앙)")
    except Exception as e:
        log.warning(f"Claude Code 실행/배치 실패: {e}")


def open_desktop(view=None):
    """바탕화면 폴더를 PC 탐색기로 열고 — 현재 스트리밍 모니터 view 영역에 맞게 배치."""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        import win32gui
        # 열기 전 기존 탐색기 hwnd 스냅샷 — 새로 열린 창만 잡기 위해
        existing = set()
        def _cb_before(h, _):
            if win32gui.GetClassName(h) == "CabinetWClass" and win32gui.IsWindowVisible(h):
                existing.add(h)
        win32gui.EnumWindows(_cb_before, None)

        os.startfile(desktop)

        hwnd = 0
        for _ in range(20):
            time.sleep(0.1)
            fresh = []
            def _cb_after(h, _):
                if win32gui.GetClassName(h) == "CabinetWClass" and win32gui.IsWindowVisible(h) and h not in existing:
                    fresh.append(h)
            win32gui.EnumWindows(_cb_after, None)
            if fresh:
                hwnd = fresh[0]; break
        if not hwnd:
            # 새 창 감지 실패 시 포어그라운드 fallback
            hwnd = win32gui.GetForegroundWindow()
        x, y, w, h = _view_to_screen(view, min_w=280, min_h=200)
        win32gui.MoveWindow(hwnd, x, y, w, h, True)
        log.info(f"🗂 바탕화면 폴더 mon={STATE['monitor']} {x},{y} {w}x{h}")
    except Exception as e:
        log.warning(f"폴더 창 배치 실패: {e}")


def close_desktop():
    """열어둔 바탕화면 폴더(탐색기 창) 닫기"""
    try:
        import win32gui
        hwnd = win32gui.FindWindow("CabinetWClass", None)
        if hwnd:
            win32gui.PostMessage(hwnd, 0x0010, 0, 0)   # 0x0010 = WM_CLOSE
    except Exception as e:
        log.warning(f"폴더 닫기 실패: {e}")
    log.info("🗂 바탕화면 폴더 닫기")


def capture_monitor_n(mon_idx: int) -> dict:
    """모니터 N 전체 캡처 → 클립보드(CF_DIB) + Ctrl+V. mon_idx: 0=노트북, 1=보조1, 2=보조2…"""
    if not HAS_WIN32:
        return {"type": "capture_fail", "msg": "pywin32 없음"}
    mons = _stable_mons()
    phys = mons[1:]  # monitors[0]은 전체합산 가상 → 제외
    idx = max(0, min(mon_idx, len(phys) - 1))
    mon = phys[idx]
    raw = get_mss().grab(mon)
    img = Image.frombytes("RGB", raw.size, raw.rgb)
    out = io.BytesIO()
    img.save(out, "BMP")
    bmp = out.getvalue()[14:]
    with _clip_lock:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp)
        finally:
            win32clipboard.CloseClipboard()
    time.sleep(0.04)
    pyautogui.hotkey("ctrl", "v")
    log.info(f"📸 모니터{mon_idx} 캡처 {img.width}x{img.height}")
    return {"type": "capture_ok", "w": img.width, "h": img.height, "mon": mon_idx}


_last_capture_ts: float = 0.0
_capture_dedup_lock = threading.Lock()  # race condition 방지

def capture_to_clipboard() -> dict:
    """현재 보고 있는 영역 → 클립보드(CF_DIB) → Ctrl+V."""
    global _last_capture_ts
    with _capture_dedup_lock:
        now = time.time()
        if now - _last_capture_ts < 1.0:   # 1초 쿨다운 — Lock으로 race condition 차단
            return {"type": "capture_skip", "msg": "중복 요청"}
        _last_capture_ts = now
    _last_capture_ts = now
    if not HAS_WIN32:
        return {"type": "capture_fail", "msg": "pywin32 없음"}
    box = crop_box(STATE["view"])
    m = _mon(STATE["monitor"])
    use_dxcam = False  # mss 통일 — dxcam은 모니터 해상도 불일치 시 Invalid Region 에러 유발
    cam = get_dxcam() if use_dxcam else None
    img = None
    if cam is not None:
        try:
            _cw = getattr(cam, 'width', box["left"] + box["width"])
            _ch = getattr(cam, 'height', box["top"] + box["height"])
            region = (
                max(0, box["left"]),
                max(0, box["top"]),
                min(_cw, box["left"] + box["width"]),
                min(_ch, box["top"] + box["height"]),
            )
            raw_np = cam.grab(region=region)
            if raw_np is not None:
                img = Image.fromarray(raw_np[:, :, [2, 1, 0]])
        except Exception as _dx_err:
            log.warning(f"dxcam 캡처 실패 → mss 폴백: {_dx_err}")
    if img is None:
        raw = get_mss().grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    out = io.BytesIO()
    img.save(out, "BMP")
    bmp = out.getvalue()[14:]
    with _clip_lock:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp)
        finally:
            win32clipboard.CloseClipboard()
    time.sleep(0.04)
    pyautogui.hotkey("ctrl", "v")
    log.info(f"📸 캡처→붙여넣기 {img.width}x{img.height}")
    return {"type": "capture_ok", "w": img.width, "h": img.height}


_voice_clip = None                                    # 녹음 세션 동안 사용자 클립보드 1번 백업 (rec_start/end)
_clip_lock = threading.Lock()                         # 클립보드 동시 열기 방지 (TapDesk 캡처 ↔ 버블 캡처 충돌 방지)


def _set_clipboard_no_history(text: str) -> bool:
    """클립보드 set + 히스토리/클라우드 제외 마커. STT 자동입력이 Win+V 기록을 오염하는 것 방지.
    (복사 버튼=일반 Ctrl+C는 그대로 히스토리에 쌓임. STT만 제외.) 실패 시 False."""
    try:
        import win32clipboard as _wc
        cf = _wc.RegisterClipboardFormat("ExcludeClipboardContentFromMonitorProcessing")
        _wc.OpenClipboard()
        try:
            _wc.EmptyClipboard()
            _wc.SetClipboardData(_wc.CF_UNICODETEXT, text)
            _wc.SetClipboardData(cf, bytes([0]))      # 제외 마커 — Win+V 히스토리/클라우드 동기화 제외
        finally:
            _wc.CloseClipboard()
        return True
    except Exception:
        return False


def apply_live_typing(backspaces, append: str) -> None:
    backspaces = max(0, min(int(backspaces), 200))
    if backspaces > 0:
        _send_backspace(backspaces)   # SendInput 일괄 처리 (pyautogui 루프 제거)
    if append:
        _type_unicode_direct(append)  # 클립보드 없이 직접 SendInput 타이핑
    log.info(f"⌨ 음성입력 +{len(append)}자")


def move_active_window():
    """활성창을 다른 모니터로 좌표 직접 이동 — 옮긴 모니터 알림 반환."""
    if not HAS_WIN32:
        return None
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    mons = _stable_mons()                # [0]=전체, [1..]=노트북 먼저 고정 순서
    if len(mons) < 3:                    # 물리 모니터 2개 미만이면 패스
        return None
    wr = win32gui.GetWindowRect(hwnd)
    ww, wh = wr[2] - wr[0], wr[3] - wr[1]
    wcx, wcy = (wr[0] + wr[2]) // 2, (wr[1] + wr[3]) // 2
    cur = 1
    for i in range(1, len(mons)):
        m = mons[i]
        if (m["left"] <= wcx < m["left"] + m["width"]
                and m["top"] <= wcy < m["top"] + m["height"]):
            cur = i
            break
    tgt = 2 if cur == 1 else 1
    m = mons[tgt]
    pl = win32gui.GetWindowPlacement(hwnd)
    was_max = pl[1] == win32con.SW_SHOWMAXIMIZED
    if was_max:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        wr = win32gui.GetWindowRect(hwnd)
        ww, wh = wr[2] - wr[0], wr[3] - wr[1]
    ww, wh = min(ww, m["width"]), min(wh, m["height"])
    nx = m["left"] + (m["width"] - ww) // 2
    ny = m["top"] + (m["height"] - wh) // 3
    win32gui.MoveWindow(hwnd, nx, ny, ww, wh, True)
    if was_max:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED)
    STATE["monitor"] = tgt
    STATE["view"] = {"rx1": 0.0, "ry1": 0.0, "rx2": 1.0, "ry2": 1.0,
                     "w": STATE["view"].get("w", 1280)}
    log.info(f"🖥→🖥 창 이동: mss {cur}→{tgt}")
    return {"type": "monitor_ok", "monitor": mss_to_win(tgt),
            "key": _mon_key_num(mss_to_win(tgt))}


# ───────────────────────── 메시지 처리 ─────────────────────────
async def handle_message(msg: dict):
    t = msg.get("type", "")

    if t == "rec_start":           # 녹음 시작 → 클립보드 백업 + 소유권 폰 (버블이 폴링으로 보고 멈춤)
        global _voice_clip
        import time as _tt
        REC["owner"] = "phone"; REC["ts"] = _tt.time()
        try: _voice_clip = pyperclip.paste()
        except Exception: _voice_clip = None
        return None
    if t == "rec_end":             # 녹음 끝 → 클립보드 복구 + 소유권 해제
        if REC["owner"] == "phone":
            REC["owner"] = None
        if _voice_clip is not None:
            try: pyperclip.copy(_voice_clip)
            except Exception: pass
        return None
    if t == "mouse_move":          # 절대 좌표 — 폰에서 짚은 자리
        mid = win_to_mss(msg["monitor"]) if "monitor" in msg else STATE["monitor"]
        rx = float(msg.get("rx", 0.5)); ry = float(msg.get("ry", 0.5))
        x, y = resolve_xy(mid, rx, ry)
        try:
            with open(os.path.join(os.path.dirname(__file__), "tap_log.txt"), "a", encoding="utf-8") as _f:
                _f.write(f"MOVE mon={mid} rx={rx:.4f} ry={ry:.4f} → ({x},{y})\n")
        except Exception: pass
        fast_set_pos(x, y)
        return None
    if t == "mouse_click":         # 가상 마우스 좌/우 클릭 (ctrl=다중선택 / shift=범위선택)
        fast_click(msg.get("button", "left"), ctrl=bool(msg.get("ctrl")), shift=bool(msg.get("shift")))
        return None
    if t == "dbl_click":           # 더블탭 → PC 더블클릭 (파일/폴더 열기 등)
        try:
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            with open(os.path.join(os.path.dirname(__file__), "tap_log.txt"), "a", encoding="utf-8") as _f:
                _f.write(f"DBLCLICK cursor=({pt.x},{pt.y})\n")
        except Exception: pass
        fast_click("left"); time.sleep(0.05); fast_click("left")
        return None
    if t == "mouse_down":          # 마우스 모드 — 버튼 누름 유지 (잡고 끌기)
        fast_mouse_down(msg.get("button", "left"))
        return None
    if t == "mouse_up":            # 마우스 모드 — 버튼 놓음
        fast_mouse_up(msg.get("button", "left"))
    if t == "wheel":               # BT 마우스 휠 / 스크롤 버튼 (speed = 스크롤 줄 수)
        dy = int(msg.get("dy", 0))
        speed = int(msg.get("speed", 8))   # 미지정(BT 마우스) → 8줄 = 1노치(기존 유지)
        if dy: pyautogui.scroll(dy * speed * 15)   # PC 휠설정 8줄/노치 → 1줄=15. speed 1=1줄 / 12=12줄
        return None
    if t == "scroll":
        fast_scroll(int(msg.get("delta", 0)))
        return None
    if t == "set_view":            # 폰이 보는 영역(rect) + 원하는 출력 가로
        STATE["view"] = {
            "rx1": _clamp01(msg.get("rx1", 0.0)),
            "ry1": _clamp01(msg.get("ry1", 0.0)),
            "rx2": _clamp01(msg.get("rx2", 1.0)),
            "ry2": _clamp01(msg.get("ry2", 1.0)),
            "w": int(msg.get("w", 1280)),
        }
        return None
    if t == "lte_mode":            # 기존 호환 유지
        STATE["lte"] = bool(msg.get("on", False))
        log.info(f"📶 LTE 경량 모드 {'ON' if STATE['lte'] else 'OFF'}")
        return None
    if t == "quality_mode":        # SPEED/LTE/HD/AUTO 4단계 모드
        mode = str(msg.get("mode", "lte"))
        _QUALITY_PRESETS = {
            "speed":   {"lte": True,  "fps": 30, "width": 640,  "quality": 40},
            "lte":     {"lte": True,  "fps": 20, "width": 900,  "quality": 65},
            "hd":      {"lte": False, "fps": 30, "width": 1600, "quality": 90},
            "auto_lo": {"lte": True,  "fps": 30, "width": 640,  "quality": 40},
            "auto_hi": {"lte": False, "fps": 30, "width": 1600, "quality": 90},
        }
        p = _QUALITY_PRESETS.get(mode, _QUALITY_PRESETS["lte"])
        STATE["lte"] = p["lte"]
        STATE["q_fps"] = p["fps"]
        STATE["q_width"] = p["width"]
        STATE["q_quality"] = p["quality"]
        log.info(f"🎚 화질 모드: {mode} → {p}")
        return None
    if t == "turbo_mode":          # 🚀 터보(H.264 NVENC) 온/오프 — 폰이 WebCodecs 가능할 때만 보냄
        STATE["turbo"] = bool(msg.get("on", False))
        STATE["turbo_fps"] = max(10, min(60, int(msg.get("fps", TURBO_FPS))))
        _kb = int(msg.get("kbps", TURBO_KBPS))
        if STATE.get("lte"):
            _kb = min(_kb, 8000)   # 📶 LTE 시작 안전값 (04:17 TCP 정체 실측)
        STATE["turbo_kbps"] = max(2000, min(40000, _kb))
        STATE["turbo_kbps_max"] = STATE["turbo_kbps"]   # 사용자 상한 — 적응조절이 이 위로는 안 올라감
        if not STATE["turbo"]:
            _turbo_stop()
        log.info(f"🚀 터보 모드 {'ON' if STATE['turbo'] else 'OFF'} "
                 f"({STATE['turbo_fps']}fps {STATE['turbo_kbps']}kbps)")
        return {"type": "turbo_ok", "on": STATE["turbo"],
                "fps": STATE["turbo_fps"], "kbps": STATE["turbo_kbps"]}
    if t == "frame_report":        # 📊 검은화면 진단 계측 (2026-07-13) — 폰 수신·드로우 자가보고
        log.info(f"📊 폰 자가보고: jpeg수신 {msg.get('jpeg')} / vid수신 {msg.get('vid')} / "
                 f"드로우 {msg.get('drawn')} / sched={msg.get('sched')} pend={msg.get('pend')} cv={msg.get('cv')}")
        return None
    if t == "turbo_probe_result":  # 🎨 폰 디코드 색 회신 → ⚖️ judge.json 기준으로 프로그램이 PASS/FAIL 판정
        global _judge_fail_streak
        try:
            _d = float(msg.get("d", 0))
        except (TypeError, ValueError):
            return None
        if _judge is None:   # 제23조 — 기준 없으면 판정 없음 (프로브도 안 보내지만 이중 방어)
            log.info(f"🎨 색검증(판정기준 없음): Δ{_d}")
            return None
        if _d <= _judge["pass_max_delta"]:
            _judge_fail_streak = 0
            log.info(f"🎨 색판정 PASS: Δ{_d} ≤ {_judge['pass_max_delta']} "
                     f"(PC {msg.get('src')} → 폰 {msg.get('got')})")
        elif _d > _judge["fail_delta"]:
            _judge_fail_streak += 1
            log.warning(f"🎨 색판정 FAIL {_judge_fail_streak}/{_judge['fail_streak']}: "
                        f"Δ{_d} > {_judge['fail_delta']} (PC {msg.get('src')} → 폰 {msg.get('got')})")
            if _judge_fail_streak >= _judge["fail_streak"]:
                _judge_fail_streak = 0
                STATE["turbo"] = False
                _turbo_stop()
                log.warning("🎨 색판정 확정 FAIL → 자동조치 집행: 터보 OFF·JPEG 폴백 (judge.json, 사람 손 0)")
                if _push is not None:
                    try:
                        await _push({"type": "toast", "text": "🎨 색 이상 자동감지 → 일반 모드 복귀"})
                    except Exception:
                        pass
                return {"type": "turbo_ok", "on": False}
        else:
            _judge_fail_streak = 0   # 주의 구간(5<Δ≤10) — 연속성 끊김
            log.info(f"🎨 색판정 주의: Δ{_d} (합격선 초과·불합격선 이내)")
        return None
    if t == "monitor_reset":                    # [모니터 재셋팅] = 캡처 엔진 재초기화 (자폭 없음)
        global _mss_generation
        _mss_generation += 1                   # 모든 스레드가 다음 get_mss()에서 자동 재생성
        _order_cache["rects"] = None
        _meta_cache["meta"] = None
        STATE["monitor"] = 1
        STATE["monitor_key"] = ""
        STATE["view"] = {"rx1": 0.0, "ry1": 0.0, "rx2": 1.0, "ry2": 1.0,
                         "w": STATE["view"].get("w", 1280)}
        log.info("🔄 모니터 재초기화 완료 — 자폭 없음, 연결 유지")
        return {"type": "monitor_ok", "monitor": STATE.get("monitor", 1), "key": STATE.get("monitor_key", "")}
    if t == "monitor_switch":
        mons = _stable_mons()
        edid = str(msg.get("edid", "")).strip()
        req_num = msg.get("monitor", "?")
        log.info(f"[DBG] monitor_switch 수신: monitor={req_num} edid={edid[:20] if edid else '(없음)'}")
        n = None
        if edid:
            idx = _idx_by_key(edid)
            log.info(f"[DBG] _idx_by_key({edid[:20]}) → {idx}")
            if idx is not None:
                n = idx
                STATE["monitor_key"] = edid
        if n is None:
            n = int(msg.get("monitor", 1))
            if n < 1 or n > len(mons) - 1:
                n = 1
            STATE["monitor_key"] = _mon_key_num(n) or ""
        STATE["monitor"] = n
        before_resync = n
        _resync_monitor()
        log.info(f"[DBG] n={before_resync} → resync 후 STATE[monitor]={STATE['monitor']}")
        STATE["view"] = {"rx1": 0.0, "ry1": 0.0, "rx2": 1.0, "ry2": 1.0,
                         "w": STATE["view"].get("w", 1280)}
        log.info(f"🖥 모니터 전환 → 번호 {STATE['monitor']} / 키 {STATE['monitor_key']}")
        return {"type": "monitor_ok", "monitor": STATE["monitor"], "key": STATE["monitor_key"]}
    if t == "get_thumbs":                       # 폰이 5초마다 요청 → 모니터 목록·썸네일 갱신 (hotplug 추종)
        _resync_monitor()
        return {"type": "thumbs", "list": monitor_list(),
                "cur": STATE["monitor"], "curKey": STATE["monitor_key"]}
    if t == "key_enter":
        pyautogui.press("enter")
        return None
    if t == "hotkey":              # 범용 키 — 컨텍스트 도크(웹 뒤로/새로고침, 영상 재생/넘김 등)
        keys = msg.get("keys")
        try:
            if isinstance(keys, list) and keys:
                if len(keys) == 1:
                    pyautogui.press(keys[0])
                else:
                    pyautogui.hotkey(*keys)
        except Exception as e:
            log.warning(f"hotkey 오류: {e}")
        return None
    if t == "undo":                       # 뒤로 (Ctrl+Z)
        pyautogui.hotkey("ctrl", "z")
        return None
    if t == "redo":                       # 앞으로 (Ctrl+Y)
        pyautogui.hotkey("ctrl", "y")
        return None
    if t == "ctrl_click":                 # Ctrl+클릭 (다중 선택 — 누적)
        rx = float(msg.get("rx", 0.5)); ry = float(msg.get("ry", 0.5))
        x, y = resolve_xy(STATE["monitor"], rx, ry)
        pyautogui.moveTo(x, y, duration=0.0)
        pyautogui.keyDown("ctrl")
        try: pyautogui.click()
        finally: pyautogui.keyUp("ctrl")
        return None
    if t == "select_all":                 # 전체 선택 (Ctrl+A)
        pyautogui.hotkey("ctrl", "a")
        return None
    if t == "box_down":                   # 박스 선택 시작 — 절대좌표로 이동 후 좌버튼 누름
        rx = float(msg.get("rx", 0.5)); ry = float(msg.get("ry", 0.5))
        x, y = resolve_xy(STATE["monitor"], rx, ry)
        fast_set_pos(x, y)
        time.sleep(0.01)
        fast_mouse_down("left")
        return None
    if t == "box_move":                   # 박스 드래그 중 — 좌버튼 누른 채 이동 (네모는 윈도우가 그림)
        rx = float(msg.get("rx", 0.5)); ry = float(msg.get("ry", 0.5))
        x, y = resolve_xy(STATE["monitor"], rx, ry)
        fast_set_pos(x, y)
        return None
    if t == "box_up":                     # 박스 선택 끝 — 좌버튼 놓음
        fast_mouse_up("left")
        return None
    if t == "copy_sel":                   # 선택한 것 복사 (Ctrl+C)
        pyautogui.hotkey("ctrl", "c")
        return None
    if t == "cut_sel":                    # 선택한 것 잘라내기 (Ctrl+X) — 모니터 간 이동용
        pyautogui.hotkey("ctrl", "x")
        return None
    if t == "paste_sel":                  # 붙여넣기 (Ctrl+V)
        pyautogui.hotkey("ctrl", "v")
        return None
    if t == "backspace":
        pyautogui.press("backspace")
        return None
    if t == "select_all_delete":
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.03)
        pyautogui.press("delete")
        return None
    if t == "select_line":         # 줄 선택 (복붙 도구 — 커서 있는 줄 전체)
        pyautogui.press("home")
        time.sleep(0.02)
        pyautogui.hotkey("shift", "end")
        return None
    if t == "audio_mode":          # PC 소리 전송 on/off
        STATE["audio"] = bool(msg.get("on", False))
        log.info(f"🔊 소리 전송 {'ON' if STATE['audio'] else 'OFF'}")
        if not STATE["audio"]:
            try:
                while True:
                    audio_q.get_nowait()
            except queue.Empty:
                pass
        return None
    if t == "copy":                # 복사 (복붙 도구) + 자체 클립보드 적재
        pyautogui.hotkey("ctrl", "c")
        await asyncio.sleep(0.05)                      # 클립보드 sync 대기
        try:
            txt = await asyncio.to_thread(pyperclip.paste)
            if txt and _push is not None:
                await _push({"type": "clip_add", "text": txt[:5000]})   # 폰 자체 클립보드 목록에 카드 추가
        except Exception:
            pass
        return None
    if t == "grab_copy":           # 길게눌러 긁기 → 떼는 순간 자동 복사. '선택 텍스트가 실제로 생겼을 때'만 입고(오작동 방지). ※copy_sel(복붙도구)과 이름 분리
        global _clip_last
        try: before = await asyncio.to_thread(pyperclip.paste)
        except Exception: before = ""
        pyautogui.hotkey("ctrl", "c")
        await asyncio.sleep(0.06)                       # 클립보드 sync 대기
        try:
            txt = await asyncio.to_thread(pyperclip.paste)
            if txt and txt != before and _push is not None:   # 새로 선택된 텍스트가 있을 때만 (없으면=선택 0 → 무시)
                _clip_last = txt                              # 복사감시 기준선 갱신(중복 입고 방지)
                await _push({"type": "clip_add", "text": txt[:5000]})
                await _push({"type": "toast", "text": "📋 긁어서 복사 → 보관함"})
        except Exception:
            pass
        return None
    if t == "paste":               # 붙여넣기 (복붙 도구)
        pyautogui.hotkey("ctrl", "v")
        return None
    if t == "clip_to_pc":          # 자체 클립보드 카드 → PC 클립보드에 올림 (1탭) — 가서 직접 Ctrl+V
        txt = msg.get("text", "")
        if txt:
            await asyncio.to_thread(pyperclip.copy, txt)
        return None
    if t == "clip_paste":          # 자체 클립보드 카드 → PC 활성창에 즉시 붙여넣기 (길게)
        txt = msg.get("text", "")
        if txt:
            await asyncio.to_thread(pyperclip.copy, txt)
            time.sleep(0.03)
            pyautogui.hotkey("ctrl", "v")
        return None
    if t == "paste_image":         # 폰 이미지 → PC 클립보드에 붙여넣기
        data = msg.get("data", "")
        if data:
            await asyncio.to_thread(_paste_image, data)
        return None
    if t == "open_chrome":         # PC에서 크롬 브라우저 실행
        await asyncio.to_thread(open_chrome)
        return None
    if t == "close_chrome":        # 크롬 브라우저 닫기
        await asyncio.to_thread(close_chrome)
        return None
    if t == "open_desktop":        # 바탕화면 폴더 열기 (view 영역 맞춤)
        await asyncio.to_thread(open_desktop, msg.get("view"))
    if t == "open_claude":         # Claude 앱 열기 (view 영역 맞춤)
        await asyncio.to_thread(open_claude, msg.get("view"))
    if t == "open_claude_code":    # Claude Code CLI 새 cmd 창 (view 영역 맞춤)
        await asyncio.to_thread(open_claude_code, msg.get("view"))
    if t == "show_desktop":        # Win+D 동등 — 바탕화면 보기 토글
        await asyncio.to_thread(show_desktop)
    if t == "open_telegram":       # 텔레그램 — view 영역 맞춤 열기
        await asyncio.to_thread(open_telegram, msg.get("view"))
    if t == "close_telegram":      # 텔레그램 최소화 (토글 OFF)
        await asyncio.to_thread(close_telegram)
        return None
    if t == "close_desktop":       # 바탕화면 폴더 닫기
        await asyncio.to_thread(close_desktop)
        return None
    if t == "vid_ctrl":            # 영상 컨트롤 — 재생·정지 / 뒤로 / 앞으로
        a = msg.get("act")
        sec = int(msg.get("sec") or 1)
        sec = max(1, min(120, sec))                       # 1~120초 안전 범위
        if a == "play":
            pyautogui.press("space")
        elif a == "back":
            pyautogui.press("left", presses=sec, interval=0.02)
        elif a == "fwd":
            pyautogui.press("right", presses=sec, interval=0.02)
        return None
    if t == "move_window":         # 활성창을 다른 모니터로 (좌표 직접 이동)
        return await asyncio.to_thread(move_active_window)
    if t == "live_typing":
        # 키보드(kb=true)는 녹음과 무관하게 항상 처리 / STT는 녹음 소유권이 phone일 때만 (stuck 자동전송 방지)
        if not msg.get("kb") and REC["owner"] != "phone":
            return None
        await asyncio.to_thread(apply_live_typing,
                                msg.get("backspaces", 0), msg.get("append", ""))
        return None
    if t == "capture_to_clipboard":
        return await asyncio.to_thread(capture_to_clipboard)
    if t == "clip_watch":          # 복사 감시 on/off — 켜면 PC 복사가 보관함 자동 입고
        global _clip_watch          # _clip_last 는 위 copy_sel 에서 이미 global 선언됨(함수당 1회)
        _clip_watch = bool(msg.get("on"))
        if _clip_watch:
            try: _clip_last = pyperclip.paste()   # 켤 때 현재값 = 기준선 (이후 변화만 입고)
            except Exception: _clip_last = ""
        return None
    if t == "ping":
        return {"type": "pong"}
    return None


# ───────────────────────── FastAPI ─────────────────────────
REC = {"owner": None, "ts": 0.0}    # 녹음 소유권 — 마지막에 켠 쪽 우선 (폰 ↔ 마이크버블 상호배타)
app = FastAPI(title="폴드5 PC 원격제어")


def ensure_icon() -> None:
    """앱 아이콘 — 미니멀 골드 아이소메트릭 큐브 (없으면 생성)."""
    if ICON_FILE.exists():
        return
    img = Image.new("RGB", (256, 256), (11, 12, 18))
    d = ImageDraw.Draw(img)
    cx, ty, a, rh, H = 128, 98, 80, 40, 84
    T = (cx, ty - rh); R = (cx + a, ty); B = (cx, ty + rh); L = (cx - a, ty)
    Lb = (L[0], L[1] + H); Bb = (B[0], B[1] + H); Rb = (R[0], R[1] + H)
    d.polygon([T, R, B, L], fill=(216, 182, 128))      # 윗면
    d.polygon([L, B, Bb, Lb], fill=(150, 121, 78))     # 좌면
    d.polygon([B, R, Rb, Bb], fill=(101, 81, 53))      # 우면
    d.line([T, R, B, L, T], fill=(234, 202, 152), width=2, joint="curve")
    for x in (L, B, R):
        d.line([x, (x[0], x[1] + H)], fill=(60, 48, 32), width=2)
    d.line([Lb, Bb, Rb], fill=(60, 48, 32), width=2)
    img.save(ICON_FILE)


@app.get("/")
async def index():
    if not PHONE_HTML.exists():
        return HTMLResponse("<h1>phone.html 없음</h1>", status_code=500)
    ver = str(int(PHONE_HTML.stat().st_mtime))     # 파일 수정시각 = 버전 (폰 자동 새로고침 판별용)
    html = PHONE_HTML.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__VER__", ver)
    # no-store: 앱 열 때마다 최신 화면 — 옛 버전 캐시 안 됨
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/debug-capture")
async def debug_capture():
    result = await asyncio.to_thread(capture_to_clipboard)
    return result


@app.get("/cam")
async def camera_mouse():       # 🖐 카메라 슈퍼마우스(손·눈) — 추가 입력소스. 기존 입력 API(mouse_move/click)로만 연결, 본질 0변경.
    cam_html = CANONICAL / "camera_mouse.html"
    if not cam_html.exists():
        return HTMLResponse("<h1>camera_mouse.html 없음</h1>", status_code=500)
    html = cam_html.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ── 독립 마이크 버블 앱용 (WebView 없이 HTTP 직접) ──
@app.post("/type")
async def http_type(req: Request):
    try:
        d = await req.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    await asyncio.to_thread(apply_live_typing, int(d.get("backspaces", 0)), str(d.get("append", "")))
    return {"ok": True}


# ── 버블 앱용 키 명령 (엔터 등) — WebSocket handle_message 재사용 → phone.html 🎤과 동일 종착점 ──
@app.post("/key")
async def http_key(req: Request):
    try:
        d = await req.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    await handle_message({"type": str(d.get("type", "")), "keys": d.get("keys", [])})
    return {"ok": True}


# 폰 → PC: 텍스트 교정 (클로드 맥스 CLI). 폰은 CLI 못 돌리니 PC가 대신 추론해서 돌려줌.
def _claude_correct(text: str, mode: str = "correct") -> str:
    claude = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
    if mode == "intent":
        instr = ("You are cleaning up Korean speech-to-text. Understand the speaker's intent "
                 "and rewrite it into clean natural Korean that conveys what they meant: fix "
                 "grammar, remove filler words, repetition and false starts, make it read "
                 "smoothly. Keep the original meaning and tone, do NOT add new content. Keep it "
                 "Korean. Output ONLY the rewritten text, no explanation, no quotes.")
    else:
        instr = ("Fix only typos and spacing in the Korean text from stdin so it reads "
                 "naturally and matches intent. Keep it Korean. Output ONLY the corrected "
                 "text, no explanation, no quotes.")
    try:
        p = subprocess.run([claude, "-p", "--strict-mcp-config", instr],
                           input=text.encode("utf-8"), capture_output=True,
                           timeout=600, creationflags=0x08000000)   # 글자수 제한 없음
        out = p.stdout.decode("utf-8", "replace").strip()
        return out or text
    except Exception as e:
        log.warning(f"correct fail: {e}")
        return text


@app.post("/correct")
async def http_correct(req: Request):
    try:
        d = await req.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    text = str(d.get("text", ""))
    mode = str(d.get("mode", "correct"))     # "correct"=가벼운 교정 / "intent"=의도 다듬기
    if not text.strip():
        return {"ok": False, "text": ""}
    fixed = await asyncio.to_thread(_claude_correct, text, mode)
    return {"ok": True, "text": fixed}


@app.post("/rec")
async def http_rec(req: Request):
    import time as _tt
    try:
        d = await req.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    who = str(d.get("who", "?"))
    if d.get("on"):
        REC["owner"] = who; REC["ts"] = _tt.time()
        if _push is not None:
            try:
                await _push({"type": "rec_stop", "who": who})   # 다른 클라(폰) 멈춤
            except Exception:
                pass
    else:
        if REC["owner"] == who:
            REC["owner"] = None
    return {"ok": True, "owner": REC["owner"]}


@app.get("/rec")
async def http_rec_get(who: str = ""):
    import time as _tt
    if REC["owner"] and who and who == REC["owner"]:
        REC["ts"] = _tt.time()                  # 하트비트 — 내가 소유자면 갱신
    if REC["owner"] and (_tt.time() - REC["ts"]) > 15:
        REC["owner"] = None                     # 끊긴 소유권 만료
    return {"owner": REC["owner"]}


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "TapDesk", "short_name": "TapDesk",
        "start_url": "/", "display": "fullscreen", "orientation": "any",
        "background_color": "#05060a", "theme_color": "#05060a",
        "icons": [{"src": "/icon.png", "sizes": "256x256", "type": "image/png"}],
    })


@app.get("/icon.png")
async def icon():
    ensure_icon()
    return Response(ICON_FILE.read_bytes(), media_type="image/png")


# ── Claude 창 자동감지 → 줌 슬롯 자동세팅 ──
@app.get("/claude-windows")
async def claude_windows(n: int = 0):
    """Claude 앱 창 크기 감지 + n개 슬롯을 그리드로 자동 계산.
    Claude 앱은 단일 OS 창 안에서 세션을 타일링하므로 창 1개 크기 기준으로 그리드 계산.
    n=0 이면 Claude 창 감지만 하고 그리드 계산 안 함."""
    try:
        import win32gui

        # 1) 가상 데스크톱 전체 범위
        SM_XVIRTUALSCREEN  = 76; SM_YVIRTUALSCREEN  = 77
        SM_CXVIRTUALSCREEN = 78; SM_CYVIRTUALSCREEN = 79
        vx = ctypes.windll.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = ctypes.windll.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = ctypes.windll.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = ctypes.windll.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            return JSONResponse({"error": "화면 크기 감지 실패"}, status_code=500)

        # 2) Claude OS 창 1개 찾기 (타이틀 "Claude")
        claude_rect = None
        def cb(hwnd, _):
            nonlocal claude_rect
            if not win32gui.IsWindowVisible(hwnd): return
            t = win32gui.GetWindowText(hwnd)
            if t and ("Claude" in t):
                r2 = win32gui.GetWindowRect(hwnd)
                w2 = r2[2] - r2[0]; h2 = r2[3] - r2[1]
                if w2 > 400 and h2 > 400:
                    claude_rect = r2
        win32gui.EnumWindows(cb, None)

        if not claude_rect:
            return JSONResponse({"error": "Claude 창 없음", "count": 0})

        if n <= 0:
            return JSONResponse({"count": 0, "rect": list(claude_rect)})

        # 3) 그리드 계산 — 창 가로:세로 비율 반영해서 최대한 실제 타일 배치에 맞게
        import math
        aspect = (claude_rect[2] - claude_rect[0]) / max(claude_rect[3] - claude_rect[1], 1)
        cols = math.ceil(math.sqrt(n * aspect))   # 가로가 넓을수록 cols 더 큼
        rows = math.ceil(n / cols)

        # Claude 창 좌표 (가상 데스크톱 0~1 비율)
        cx1 = (claude_rect[0] - vx) / vw
        cy1 = (claude_rect[1] - vy) / vh
        cx2 = (claude_rect[2] - vx) / vw
        cy2 = (claude_rect[3] - vy) / vh
        cw  = cx2 - cx1   # 창 너비 비율
        ch  = cy2 - cy1   # 창 높이 비율

        cell_w = cw / cols
        cell_h = ch / rows

        slots = []
        for i in range(n):
            col = i % cols
            row = i // cols
            # 각 셀 중심 x
            cell_cx = cx1 + cell_w * (col + 0.5)
            # cy: 셀 하단 95% → 입력창/권한건너뛰기 딱 보이게
            cell_cy = cy1 + cell_h * row + cell_h * 0.95
            # z: 셀 너비 기준 (셀이 작을수록 z 높음)
            z = round(1 / max(cell_w, 0.01), 2)
            z = max(1.0, min(z, 16.0))

            slots.append({
                "cx": round(cell_cx, 4),
                "cy": round(cell_cy, 4),
                "z":  z,
            })

        return JSONResponse({"count": n, "cols": cols, "rows": rows, "slots": slots})

    except Exception as e:
        log.error(f"/claude-windows 오류: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# 핫리로드 — 코드 수정 후 GET /reload 호출하면 연결된 폰이 자동 새로고침
_push = None                       # 현재 연결된 폰으로 dict 보내는 함수 (없으면 None)
_active_socks = []                 # 🔒 살아있는 폰 WS들 (단일 폰 정책 — 새 연결 시 옛 것 닫음, 중복 스트림=깜빡임/더빨리 방지)


OTA_VERSION = 21  # [2026-08-29] v21 = 로드 워치독(5초 무응답→강제 전환) 추가, v20 = 7780 평문 폴백 차단 해제 + 7443 자가맥박 워치독
_OTA_APK = ROOT / "app-debug.apk"

@app.get("/ota/version")
async def ota_version():
    return {"version": OTA_VERSION}


@app.get("/api/phone-active")
async def phone_active():
    """★대표님 승인(2026-08-18): 클로드함교(8185)의 하트비트와 같은 목적 - 지금 폰이 TapDesk에
    실제로 연결돼 화면을 보고 있는지. 7777 폰캡처 서버가 이걸로 "TapDesk를 보고 있나" 판단한다.
    ★한계: 직접(로컬/Tailscale) 연결만 잡음 - 릴레이(외부망) 경유 연결은 아직 이 신호에 안 잡힘,
    그 경우엔 기존처럼 PC 포커스된 창에 붙여넣기로 폴백됨(정직하게 안 되는 척 안 함)."""
    return {"active": len(_active_socks) > 0}


@app.get("/cert-status")
async def cert_status():
    """🔒 터보엔진 TLS 인증서 상태 (대표님 확정 2026-08-17) — phone.html 배지용.
    server.py 자신과 같은 포트/프로토콜로 응답하므로 https(7443)에서도 mixed-content 차단 없이 fetch 가능
    (watchdog:7781은 평문 http뿐이라 https 페이지에서 직접 fetch하면 브라우저가 막는다 — 그래서 여기 별도 추가).
    실제 갱신 로직은 watchdog.py가 한다(여긴 순수 조회만, 부작용 없음)."""
    try:
        from cryptography import x509
        from datetime import datetime as _dt
        crt = ROOT / "_ts.crt"
        if not crt.exists():
            return {"days_left": None, "healthy": False}
        cert = x509.load_pem_x509_certificate(crt.read_bytes())
        not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after
        days = (not_after.replace(tzinfo=None) - _dt.utcnow()).days
        return {"days_left": days, "healthy": days > 14, "renew_threshold_days": 14}
    except Exception as e:
        return {"days_left": None, "healthy": False, "error": str(e)[:150]}


@app.get("/turbo-set")
async def turbo_set(token: str = "", on: int = 0, fps: int = 0, kbps: int = 0):
    """🎛 터보 원격 제어(김대리 자동튜닝용) — 폰 손대지 않고 켜고끄기+설정. 색검증 프로브와 세트."""
    if token != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    STATE["turbo"] = bool(on)
    if fps:
        STATE["turbo_fps"] = max(10, min(60, fps))
    if kbps:
        if STATE.get("lte"):
            kbps = min(kbps, 8000)   # 📶 LTE 시작 안전값 — 20M 버스트가 TCP 정체→지연 5초+의 뿌리 (04:17 실측)
        STATE["turbo_kbps"] = max(2000, min(40000, kbps))
        STATE["turbo_kbps_max"] = STATE["turbo_kbps"]
    if not STATE["turbo"]:
        _turbo_stop()
    if _push is not None:
        try:
            await _push({"type": "turbo_ok", "on": STATE["turbo"], "persist": 1,
                         "fps": STATE.get("turbo_fps", TURBO_FPS), "kbps": STATE.get("turbo_kbps", TURBO_KBPS)})
        except Exception:
            pass
    log.info(f"🎛 터보 원격 {'ON' if STATE['turbo'] else 'OFF'} ({STATE.get('turbo_fps')}fps {STATE.get('turbo_kbps')}kbps)")
    return {"ok": True, "turbo": STATE["turbo"], "fps": STATE.get("turbo_fps"), "kbps": STATE.get("turbo_kbps")}


@app.get("/restart-self")
async def restart_self(token: str = ""):
    """♻ 자가 재시작 — 코드 수정 반영용. UAC 불필요(자기 자신 종료 → watchdog(7781)이 새 코드로 부활).
    로컬/토큰 보유자만. [2026-07-03] 터보 반복 튜닝 때 관리자 재시작 반복 고통 해소."""
    if token != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    log.info("♻ 자가 재시작 요청 수신 — 0.5초 후 종료, watchdog이 부활")
    _turbo_stop()
    loop = asyncio.get_event_loop()
    loop.call_later(0.5, lambda: os._exit(0))
    return {"ok": True, "msg": "재시작 — 10초 내 부활"}

@app.get("/ota/app.apk")
async def ota_apk():
    if not _OTA_APK.exists():
        from fastapi import HTTPException
        raise HTTPException(404, "APK 없음")
    from fastapi.responses import FileResponse as _FR
    return _FR(str(_OTA_APK), media_type="application/vnd.android.package-archive", filename="tapdesk.apk")

@app.post("/push-ota")
async def push_ota(request: Request):
    d = await request.json()
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    _ADB = r"C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe"
    _APK = str(_OTA_APK)
    def _install():
        import subprocess as _sp
        r = _sp.run([_ADB, "install", "-r", _APK], capture_output=True, text=True, timeout=60)
        return r.returncode == 0, r.stdout + r.stderr
    ok, msg = await asyncio.get_event_loop().run_in_executor(None, _install)
    logger.info(f"push-ota: {'OK' if ok else 'FAIL'} {msg.strip()}")
    return {"ok": ok, "msg": msg.strip()}

@app.get("/reload")
async def reload_phone():
    import json as _json
    sent = 0
    # 1순위: _push (최근 직접 연결)
    if _push is not None:
        try:
            await _push({"type": "reload"})
            sent += 1
        except Exception:
            pass
    # 폴백: _active_socks 직접 순회 (relay 덮어쓰기 버그 방어)
    for _sock in list(_active_socks):
        try:
            await _sock.send_text(_json.dumps({"type": "reload"}, ensure_ascii=False))
            sent += 1
        except Exception:
            pass
    if sent:
        return JSONResponse({"ok": True, "msg": f"폰 새로고침 신호 전송 ({sent})"})
    return JSONResponse({"ok": False, "msg": "폰 미연결"})


# ⚙️ 설정 영구 백업 — 폰 localStorage(도크·위젯·줌 배치 등)를 PC 서버에 저장.
# 주소(origin)가 바뀌거나(localhost↔Tailscale) 앱 재설치·폰 교체해도 설정이 안 틀어지게 단일 진실 소스.
SETTINGS_FILE = CANONICAL / "settings.json"

@app.get("/settings")
async def get_settings():
    try:
        if SETTINGS_FILE.exists():
            return JSONResponse(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning(f"설정 읽기 오류: {e}")
    return JSONResponse({})

@app.post("/settings")
async def save_settings(request: Request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "msg": "dict 아님"})
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log.info(f"⚙️ 설정 백업 저장 ({len(data)}키)")
        return JSONResponse({"ok": True, "n": len(data)})
    except Exception as e:
        log.warning(f"설정 저장 오류: {e}")
        return JSONResponse({"ok": False, "msg": str(e)})


# 📡 LADB식 무선ADB 자가페어링(대표님 지시 2026-08-13, 설계도 Downloads\슈퍼 LADB 무선 바이너리 디버깅.md §6) —
#    MicBubbleApp이 폰 스스로 5555를 열면(AdbSelfPair) 여기로 보고 → PC가 붙어서 실측만(공유키라 허용팝업 없음).
ADB_EXE = r"C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe"
ADB_PHONE_TS = "100.96.143.83"          # 폰 Tailscale 고정 IP
ADB_PORT = 5555
ADB_STATE_FILE = CANONICAL / "adb_state.json"
_adb_lock = threading.Lock()
_adb_running = False

def _adb_state_load() -> dict:
    try:
        return json.loads(ADB_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"adb_ready": False, "last_connect_port": 0, "last_seen_at": 0.0,
                "last_ok_at": 0.0, "last_msg": "이력 없음", "log": []}

def _adb_state_save(st: dict) -> None:
    tmp = ADB_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ADB_STATE_FILE)   # ★원자적 쓰기(0바이트 사고 방지 — 셀리나 pcmic 관례)

def _adb_run(args, timeout=15):
    try:
        r = subprocess.run([ADB_EXE] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, f"EXC {e}"

def _adb_connect_and_verify():
    """폰이 이미 5555를 열었다는 전제 — PC는 붙어서 실측만. 공유키라 허용 팝업 없음."""
    st = _adb_state_load(); log_lines = []
    def rec(m):
        log_lines.append(m); st["last_msg"] = m; st["log"] = log_lines[-8:]; _adb_state_save(st)
    rc, out = _adb_run(["connect", f"{ADB_PHONE_TS}:{ADB_PORT}"])
    rec(f"connect {ADB_PHONE_TS}:{ADB_PORT} rc={rc} {out.strip()[:80]}")
    rc, out = _adb_run(["-s", f"{ADB_PHONE_TS}:{ADB_PORT}", "shell", "echo", "OK5555"])
    ok = (rc == 0 and "OK5555" in out)
    st.update({"adb_ready": ok, "last_ok_at": time.time() if ok else st.get("last_ok_at", 0.0),
               "last_msg": ("★5555 연결 성공" if ok else f"실패: echo 무응답 rc={rc}"), "log": log_lines[-8:]})
    _adb_state_save(st)

def _adb_kickoff():
    def run():
        global _adb_running
        with _adb_lock:
            if _adb_running: return
            _adb_running = True
        try: _adb_connect_and_verify()
        finally:
            with _adb_lock:
                _adb_running = False
    threading.Thread(target=run, daemon=True).start()

@app.post("/adb/report")
async def adb_report(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    st = _adb_state_load()
    st.update({"last_connect_port": int(body.get("connect_port", 0) or 0),
               "last_seen_at": time.time(),
               "last_msg": f"폰 보고 접수(event={body.get('event','?')}, phone5555={body.get('adb5555_ready')})"})
    _adb_state_save(st)
    _adb_kickoff()
    log.info(f"📡 ADB 보고: {body}")
    return JSONResponse({"ok": True, "queued": True})

@app.get("/adb/state")
async def adb_state():
    return JSONResponse(_adb_state_load())


# 🎙 음성 명령어 — 키워드→동작 매핑. phone.html 메뉴판 + 녹음 버블이 둘 다 읽어 씀 (단일 진실 소스).
VOICECMDS_FILE = CANONICAL / "voice_cmds.json"

@app.get("/voicecmds")
async def get_voicecmds():
    try:
        if VOICECMDS_FILE.exists():
            return JSONResponse(json.loads(VOICECMDS_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning(f"음성명령 읽기 오류: {e}")
    return JSONResponse({"cmds": [{"kw": "엔터", "act": "key_enter"}]})   # 기본 = 엔터만

@app.post("/voicecmds")
async def save_voicecmds(request: Request):
    try:
        data = await request.json()
        VOICECMDS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log.info("🎙 음성명령 매핑 저장")
        return JSONResponse({"ok": True})
    except Exception as e:
        log.warning(f"음성명령 저장 오류: {e}")
        return JSONResponse({"ok": False, "msg": str(e)})


# 📁 내 배치 — 이름 붙여 여러 개 저장/불러오기 (4개 화면형태가 한 스냅샷에 다 들어감).
# 틀어져도 골라서 불러오면 그 UI로 복원. 파일은 layouts/<이름>.json
LAYOUTS_DIR = CANONICAL / "layouts"

def _safe_name(name):
    name = (name or "").strip()
    name = "".join(c for c in name if c not in '/\\:*?"<>|').strip()
    return name[:60]

@app.get("/layouts")
async def list_layouts():
    try:
        LAYOUTS_DIR.mkdir(exist_ok=True)
        names = sorted([p.stem for p in LAYOUTS_DIR.glob("*.json")])
        return JSONResponse({"list": names})
    except Exception as e:
        return JSONResponse({"list": [], "msg": str(e)})

@app.get("/layouts/load")
async def load_layout(name: str = ""):
    nm = _safe_name(name)
    p = LAYOUTS_DIR / (nm + ".json")
    try:
        if nm and p.exists():
            return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning(f"배치 로드 오류: {e}")
    return JSONResponse({})

@app.post("/layouts/save")
async def save_layout(request: Request):
    try:
        body = await request.json()
        nm = _safe_name(body.get("name"))
        data = body.get("data")
        if not nm or not isinstance(data, dict):
            return JSONResponse({"ok": False, "msg": "이름/데이터 오류"})
        LAYOUTS_DIR.mkdir(exist_ok=True)
        (LAYOUTS_DIR / (nm + ".json")).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log.info(f"📁 배치 저장: {nm} ({len(data)}키)")
        return JSONResponse({"ok": True, "name": nm})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})

@app.post("/layouts/delete")
async def delete_layout(request: Request):
    try:
        body = await request.json()
        nm = _safe_name(body.get("name"))
        p = LAYOUTS_DIR / (nm + ".json")
        if nm and p.exists():
            p.unlink()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})


# phone.html 자동 감시 — 파일이 바뀌면 연결된 폰을 자동 새로고침
# (코드 고치면 대표님이 손 안 대도 폰이 알아서 reload. 폰 안 붙어 있으면 그냥 넘어감)
async def phone_html_watcher():
    try:
        last = PHONE_HTML.stat().st_mtime
    except Exception:
        last = 0
    while True:
        await asyncio.sleep(1.0)
        try:
            m = PHONE_HTML.stat().st_mtime
        except Exception:
            continue
        if m != last:
            last = m
            if _push is not None:
                try:
                    await _push({"type": "reload"})
                    log.info("📲 phone.html 변경 감지 → 폰 자동 새로고침")
                except Exception:
                    pass


DROP_DIR = CANONICAL / "_김대리_드롭박스"
DONE_DIR = DROP_DIR / "_처리됨"

_clip_watch = False
_clip_last = ""

async def clipboard_watcher():
    """복사 감시 — 켜면 PC 클립보드 변화 감지해서 보관함 자동 입고 (코드블록 복사·Ctrl+C 등).
    STT(녹음 중)는 제외 — 음성 입력이 보관함 오염하는 것 방지."""
    global _clip_last
    while True:
        await asyncio.sleep(1.0)
        if not _clip_watch or _push is None or REC.get("owner") == "phone":
            continue
        try:
            cur = await asyncio.to_thread(pyperclip.paste)
            if cur and cur != _clip_last:
                _clip_last = cur
                await _push({"type": "clip_add", "text": cur[:10000]})
        except Exception:
            pass


async def drop_watcher():
    """김대리 드롭박스 폴더 감시 → 새 .txt/.md 파일 → 폰 보관함(clip_add) 자동 입고."""
    while True:
        await asyncio.sleep(3.0)
        if _push is None:
            continue
        try:
            files = sorted([f for f in DROP_DIR.glob("*")
                            if f.is_file() and f.suffix.lower() in (".txt", ".md") and not f.name.startswith("_")])
            for f in files:
                try:
                    txt = f.read_text(encoding="utf-8", errors="replace").strip()
                    if txt:
                        await _push({"type": "clip_add", "text": txt[:10000], "label": f.stem})
                        log.info(f"📥 드롭박스 → 보관함: {f.name}")
                    DONE_DIR.mkdir(exist_ok=True)
                    tgt = DONE_DIR / f.name
                    if tgt.exists():
                        tgt = DONE_DIR / (f.stem + "_" + str(int(f.stat().st_mtime)) + f.suffix)
                    f.rename(tgt)                       # 처리한 파일은 _처리됨/ 으로 이동 (중복 방지)
                except Exception as e:
                    log.warning(f"드롭 처리 오류 {f.name}: {e}")
        except Exception:
            pass


@app.post("/drop")
async def _drop_text(request: Request):
    """김대리가 파일 안 거치고 직접 쏘기: curl -X POST .../drop -d '텍스트'"""
    try:
        txt = (await request.body()).decode("utf-8", errors="replace").strip()
    except Exception:
        txt = ""
    if not txt:
        return JSONResponse({"ok": False, "msg": "내용 없음"})
    if _push is not None:
        try: await _push({"type": "clip_add", "text": txt[:10000], "label": "drop"})
        except Exception: pass
    log.info(f"📥 /drop → 보관함 ({len(txt)}자)")
    return JSONResponse({"ok": True, "msg": "보관함 입고"})


@app.post("/upload")
async def upload_from_phone(request: Request):
    """폰(TypeService.captureAndSend)이 직접 찍어 보낸 이미지 → 클립보드(CF_DIB) + Ctrl+V."""
    if not HAS_WIN32:
        return JSONResponse({"ok": False, "msg": "pywin32 없음"})
    img_bytes = await request.body()
    if not img_bytes:
        return JSONResponse({"ok": False, "msg": "이미지 없음"})
    def _save():
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        out = io.BytesIO()
        img.save(out, "BMP")
        bmp = out.getvalue()[14:]
        with _clip_lock:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp)
            finally:
                win32clipboard.CloseClipboard()
        time.sleep(0.04)
        pyautogui.hotkey("ctrl", "v")
        log.info(f"📸 폰→클립보드 {img.width}x{img.height}")
    try:
        await asyncio.to_thread(_save)
        return JSONResponse({"ok": True})
    except Exception as e:
        log.warning(f"📸 /upload 실패: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ══════════ 폰버블 STT 엔진 (제미나이/위스퍼) — 31_구글STT 상주 전사 재사용 (2026-07-07) ══════════
# 폰이 발화 wav를 올리면 → PC 상주 전사 프로세스(--serve)가 받아써서 → 텍스트를 폰에 돌려줌
# (타이핑·자막·AI다듬기·음성명령 분기는 폰의 기존 로직이 그대로 처리)
_STT_PY = r"C:\Users\GAPER\AppData\Local\Programs\Python\Python313\python.exe"
_STT_ENGINES_DIR = r"E:\1._AI_SaaS 제작소\1.SaaS_제품\31_구글STT\3_백엔드\engines"
_STT_TMP = Path(r"C:\Users\Public\tapdesk_stt")
_stt_procs: dict = {}
_stt_locks = {"gemini": threading.Lock(), "whisper": threading.Lock()}


def _stt_get_proc(engine: str):
    p = _stt_procs.get(engine)
    if p is not None and p.poll() is None:
        return p
    script = os.path.join(_STT_ENGINES_DIR, f"engine_{engine}.py")
    _STT_TMP.mkdir(parents=True, exist_ok=True)
    errf = open(_STT_TMP / f"engine_{engine}_err.log", "ab")   # 사인 채증용 (침묵사 금지)
    p = subprocess.Popen([_STT_PY, script, "--serve"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=errf, cwd=_STT_ENGINES_DIR,
                         creationflags=0x08000000)
    deadline = time.time() + 90   # 위스퍼 모델 로딩 대기
    while time.time() < deadline:
        line = p.stdout.readline().decode("utf-8", "replace").strip()
        if line == "READY":
            _stt_procs[engine] = p
            log.info(f"🎙 폰버블 STT 상주엔진 기동: {engine}")
            return p
        if not line and p.poll() is not None:
            break
    try:
        p.kill()
    except Exception:
        pass
    raise RuntimeError(f"STT 엔진({engine}) 기동 실패")


def _stt_transcribe(engine: str, wav_bytes: bytes) -> str:
    with _stt_locks[engine]:
        p = _stt_get_proc(engine)
        _STT_TMP.mkdir(parents=True, exist_ok=True)
        f = _STT_TMP / f"u_{int(time.time() * 1000)}.wav"
        f.write_bytes(wav_bytes)
        try:
            p.stdin.write((str(f) + "\n").encode("utf-8"))
            p.stdin.flush()
            deadline = time.time() + 40
            while time.time() < deadline:
                line = p.stdout.readline().decode("utf-8", "replace").strip()
                if line.startswith("FINAL:"):
                    return line[6:].strip()
                if line.startswith("ERROR:"):
                    raise RuntimeError(line[6:])
                if not line and p.poll() is not None:
                    _stt_procs.pop(engine, None)
                    raise RuntimeError("STT 엔진 프로세스 사망")
            raise RuntimeError("STT 응답 시간 초과")
        finally:
            try:
                f.unlink()
            except Exception:
                pass


@app.post("/stt-audio")
async def stt_audio(request: Request):
    """폰버블 발화 wav → PC측 STT(제미나이/위스퍼) 전사 → {"text"} 반환."""
    if request.headers.get("x-token", "") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    engine = request.headers.get("x-engine", "whisper")
    if engine not in ("gemini", "whisper"):
        return JSONResponse({"ok": False, "msg": "unknown engine"}, status_code=400)
    wav = await request.body()
    if not wav or len(wav) < 1000:
        return {"ok": True, "text": ""}
    try:
        text = await asyncio.to_thread(_stt_transcribe, engine, wav)
    except Exception as e:
        log.warning(f"🎙 /stt-audio 실패({engine}): {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    log.info(f"🎙 폰버블 전사({engine}): {text[:40]}")
    return {"ok": True, "text": text}


# ── ★v2 진짜 스트리밍 (2026-07-07): 폰이 PCM을 흘리면 엔진 --stream 프로세스로 릴레이,
#    확정 문장(FINAL)은 폴링(/stt-stream-poll)으로 회수 → 폰이 기존 확정 로직으로 처리 ──
_stt_streams: dict = {}
_stt_stream_lock = threading.Lock()


def _stream_reader(sid: str, proc):
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("FINAL:"):
                st = _stt_streams.get(sid)
                if st:
                    st["q"].put(line[6:].strip())
                    log.info(f"🎙 v2 확정(sid={sid[:6]}): {line[6:46]}")
    except Exception:
        pass


def _stream_get(sid: str, engine: str):
    with _stt_stream_lock:
        st = _stt_streams.get(sid)
        if st and st["proc"].poll() is None:
            return st
        script = os.path.join(_STT_ENGINES_DIR, f"engine_{engine}.py")
        _STT_TMP.mkdir(parents=True, exist_ok=True)
        errf = open(_STT_TMP / f"stream_{engine}_err.log", "ab")
        proc = subprocess.Popen([_STT_PY, "-u", script, "--stream"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errf, cwd=_STT_ENGINES_DIR,
                                creationflags=0x08000000)
        st = {"proc": proc, "q": queue.Queue(), "t": time.time(), "closed": False}
        _stt_streams[sid] = st
        threading.Thread(target=_stream_reader, args=(sid, proc), daemon=True).start()
        log.info(f"🎙 v2 스트림 릴레이 기동: {engine} sid={sid[:6]}")
        return st


def _stream_janitor():
    """유휴 60초 스트림 정리 (폰이 갑자기 사라져도 프로세스 안 쌓이게)"""
    while True:
        time.sleep(30)
        now = time.time()
        with _stt_stream_lock:
            for sid in list(_stt_streams.keys()):
                st = _stt_streams[sid]
                if now - st["t"] > 60:
                    try:
                        st["proc"].kill()
                    except Exception:
                        pass
                    _stt_streams.pop(sid, None)


threading.Thread(target=_stream_janitor, daemon=True, name="SttStreamJanitor").start()


@app.post("/stt-stream-up")
async def stt_stream_up(request: Request):
    """폰 → PCM(16k mono 16bit) 연속 업로드 (chunked). 녹음 끝나면 연결 종료."""
    if request.headers.get("x-token", "") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    engine = request.headers.get("x-engine", "gemini")
    if engine != "gemini":
        return JSONResponse({"ok": False, "msg": "v2 스트리밍은 gemini 전용"}, status_code=400)
    sid = request.headers.get("x-sid", "")
    if not sid:
        return JSONResponse({"ok": False, "msg": "sid 없음"}, status_code=400)
    try:
        st = await asyncio.to_thread(_stream_get, sid, engine)
    except Exception as e:
        log.warning(f"🎙 v2 기동 실패: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    proc = st["proc"]
    got = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            await asyncio.to_thread(proc.stdin.write, chunk)
            await asyncio.to_thread(proc.stdin.flush)
            got += len(chunk)
            st["t"] = time.time()
    except Exception as e:
        log.warning(f"🎙 v2 업로드 끊김(sid={sid[:6]}): {e}")
    finally:
        try:
            proc.stdin.close()   # EOF → 엔진이 8초 유예 후 자기 정리
        except Exception:
            pass
        st["closed"] = True
    log.info(f"🎙 v2 업로드 종료(sid={sid[:6]}): {got}바이트")
    return {"ok": True, "bytes": got}


@app.post("/gemini-polish")
async def gemini_polish(request: Request):
    """★하이브리드(2026-07-07): 폰내장 STT 초벌 텍스트 → 제미나이 flash-lite 오타·띄어쓰기 교정 (~1초).
    STT 속도는 폰내장, 최종 문장력은 제미나이 — 두 마리 토끼."""
    try:
        d = await request.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    raw = (d.get("text") or "").strip()
    if not raw:
        return {"ok": True, "text": ""}
    try:
        text = await asyncio.to_thread(_gemini_polish_text, raw)
    except Exception as e:
        log.warning(f"✨ polish 실패: {e}")
        return {"ok": True, "text": raw}   # 실패 시 원문 유지 (타이핑 안 끊김)
    return {"ok": True, "text": text}


_gp_client = [None]


def _gemini_polish_text(raw: str) -> str:
    from google import genai
    from google.genai import types
    if _gp_client[0] is None:
        key = ""
        for kf in (r"C:\Users\Public\구글음성STT\gemini_key.txt",
                   os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "구글음성STT", "gemini_key.txt")):
            try:
                key = open(kf, encoding="utf-8").read().strip()
                if key:
                    break
            except Exception:
                pass
        _gp_client[0] = genai.Client(api_key=key)
    cfg = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0),   # ★핵심: thinking off = 9초→1초
        system_instruction="너는 받아쓰기 교정기다. 입력 문장의 오타·띄어쓰기·문장부호만 자연스럽게 고쳐라. "
                           "의미·단어를 바꾸거나 새 내용을 더하지 마라. 설명 없이 고친 문장만 출력하라.")
    r = _gp_client[0].models.generate_content(
        model="gemini-2.5-flash-lite", contents=raw, config=cfg)
    out = (r.text or "").strip()
    return out if out else raw


@app.get("/stt-stream-poll")
async def stt_stream_poll(request: Request):
    """폰이 확정 문장 회수 (400ms 주기 폴링)."""
    if request.headers.get("x-token", "") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    sid = request.query_params.get("sid", "")
    st = _stt_streams.get(sid)
    if st is None:
        return {"ok": True, "texts": [], "alive": False}
    texts = []
    try:
        while True:
            texts.append(st["q"].get_nowait())
    except Exception:
        pass
    if texts:
        st["t"] = time.time()
    return {"ok": True, "texts": texts, "alive": st["proc"].poll() is None}


@app.post("/phone-capture")
async def phone_capture(request: Request):
    """📸 target별 캡처: phone=폰(TypeService→/upload 직접), mon0~3=해당 모니터 mss 캡처."""
    try:
        d = await request.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    target = str(d.get("target", "mon0"))
    crop = d.get("crop", None)  # {rx1, ry1, rx2, ry2} 0~1 비율
    if target in ("phone", "phone_adb"):
        # adb screencap — USB 또는 무선ADB 자동 시도
        def _adb_cap():
            import base64 as _b64
            _ADB = r"C:\Users\GAPER\AppData\Local\Android\Sdk\platform-tools\adb.exe"
            _TMP = str(Path.home() / "AppData" / "Local" / "Temp" / "ph_cap.png")
            subprocess.run([_ADB, "shell", "screencap", "-p", "/data/local/tmp/ph_cap.png"], timeout=8, check=True)
            subprocess.run([_ADB, "pull", "/data/local/tmp/ph_cap.png", _TMP], timeout=8, check=True)
            img = Image.open(_TMP).convert("RGB")
            if crop:
                w, h = img.size
                x1 = max(0, int(crop["rx1"] * w))
                y1 = max(0, int(crop["ry1"] * h))
                x2 = min(w, int(crop["rx2"] * w))
                y2 = min(h, int(crop["ry2"] * h))
                if x2 > x1 and y2 > y1:
                    img = img.crop((x1, y1, x2, y2))
            out = io.BytesIO(); img.save(out, "BMP"); bmp = out.getvalue()[14:]
            with _clip_lock:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp)
                finally:
                    win32clipboard.CloseClipboard()
            import time as _t; _t.sleep(0.04)
            pyautogui.hotkey("ctrl", "v")
            png_out = io.BytesIO(); img.save(png_out, "PNG")
            img_b64 = _b64.b64encode(png_out.getvalue()).decode()
            log.info(f"📸 폰 adb 캡처 완료 {img.width}x{img.height}")
            return img_b64
        try:
            img_b64 = await asyncio.to_thread(_adb_cap)
            return JSONResponse({"ok": True, "img_b64": img_b64})
        except Exception as e:
            log.warning(f"📸 adb 캡처 실패: {e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if target == "phone_proj":
        # 폰 앱 MediaProjection 캡처 — LTE에서도 동작 (USB 불필요)
        import base64 as _b64
        img_b64_data = d.get("img_b64", "")
        if not img_b64_data:
            return JSONResponse({"ok": False, "error": "img_b64 없음"}, status_code=400)
        def _proj_cap():
            img_bytes = _b64.b64decode(img_b64_data)
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            out = io.BytesIO(); img.save(out, "BMP"); bmp = out.getvalue()[14:]
            with _clip_lock:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp)
                finally:
                    win32clipboard.CloseClipboard()
            time.sleep(0.04)
            pyautogui.hotkey("ctrl", "v")
            log.info(f"📸 폰 MediaProjection 캡처 완료 {img.width}x{img.height}")
        try:
            await asyncio.to_thread(_proj_cap)
            return JSONResponse({"ok": True})
        except Exception as e:
            log.warning(f"📸 phone_proj 실패: {e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        mon_idx = int(target.replace("mon", "")) if target.startswith("mon") else 0
        result = await asyncio.to_thread(capture_monitor_n, mon_idx)
        log.info(f"📸 phone-capture 모니터{mon_idx} 완료")
        return JSONResponse({"ok": True})
    except Exception as e:
        log.warning(f"📸 phone-capture 실패: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


_MICBUBBLE_APK = Path(__file__).parent / "_app" / "MicBubbleApp" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"  # 🩹 실제 위치는 _app/ 하위 — 경로 오타 수정 (대표님 지시 2026-07-06)

@app.get("/micbubble-version")
async def micbubble_version():
    """MicBubble 앱 OTA — 현재 APK 버전코드 반환."""
    if not _MICBUBBLE_APK.exists():
        return JSONResponse({"version": 0})
    import zipfile, re
    try:
        with zipfile.ZipFile(_MICBUBBLE_APK) as z:
            if "AndroidManifest.xml" in z.namelist():
                raw = z.read("AndroidManifest.xml")
                # versionCode는 binary XML에 있어 — build.gradle에서 읽기
                pass
    except Exception:
        pass
    gradle = Path(__file__).parent / "_app" / "MicBubbleApp" / "app" / "build.gradle"  # 🩹 경로 오타 수정 (대표님 지시 2026-07-06)
    version_code = 0
    if gradle.exists():
        for line in gradle.read_text(encoding="utf-8").splitlines():
            m = re.search(r"versionCode\s+(\d+)", line)
            if m:
                version_code = int(m.group(1))
                break
    return JSONResponse({"version": version_code})

@app.get("/micbubble.apk")
async def micbubble_apk():
    """MicBubble APK 다운로드."""
    if not _MICBUBBLE_APK.exists():
        return JSONResponse({"error": "APK 없음"}, status_code=404)
    from starlette.responses import FileResponse
    return FileResponse(str(_MICBUBBLE_APK), media_type="application/vnd.android.package-archive", filename="MicBubble.apk")

@app.get("/update-micbubble")
async def update_micbubble(request: Request):
    """폰 TapDesk WebView에 MicBubble APK 다운로드 URL 전송 → 폰에서 설치 팝업."""
    import json as _json
    apk_url = str(request.base_url).rstrip("/") + "/micbubble.apk"
    payload = _json.dumps({"type": "open_url", "url": apk_url}, ensure_ascii=False)
    sent = 0
    if _push is not None:
        try:
            await _push({"type": "open_url", "url": apk_url})
            sent += 1
        except Exception:
            pass
    for _sock in list(_active_socks):
        try:
            await _sock.send_text(payload)
            sent += 1
        except Exception:
            pass
    log.info(f"📦 update-micbubble 신호 {sent}개 전송, url={apk_url}")
    return JSONResponse({"ok": True, "sent": sent, "apk_url": apk_url})


@app.post("/set-cap-target")
async def set_cap_target(request: Request):
    try:
        d = await request.json()
    except Exception:
        d = {}
    if d.get("token") != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    ct = str(d.get("cap_target", "phone"))
    STATE["cap_target"] = ct
    log.info(f"📸 캡처 대상 변경: {ct}")
    return JSONResponse({"ok": True, "cap_target": ct})


@app.get("/get-cap-target")
async def get_cap_target(token: str = ""):
    if token != TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    return JSONResponse({"ok": True, "cap_target": STATE.get("cap_target", "phone")})


@app.get("/monitor-count")
async def monitor_count_api():
    """현재 연결된 모니터 수 반환 — APK 메뉴 동적 구성용. 인증 불필요."""
    try:
        s = get_mss()
        count = len(s.monitors) - 1  # monitors[0]은 전체합산 가상
        return JSONResponse({"count": max(1, count)})
    except Exception:
        return JSONResponse({"count": 1})


_watchers_started = False   # TLS(7443) 제2 uvicorn도 startup을 또 태우므로 1회만 기동

@app.on_event("startup")
async def _start_watcher():
    global _watchers_started
    if _watchers_started:
        return
    _watchers_started = True
    asyncio.create_task(phone_html_watcher())
    asyncio.create_task(drop_watcher())
    asyncio.create_task(clipboard_watcher())


_audio_ws: "WebSocket | None" = None   # 오디오 전용 채널 (JPEG send_lock 완전 분리)

@app.websocket("/ws-audio")
async def ws_audio_endpoint(ws: WebSocket):
    """오디오 전용 WebSocket — JPEG 전송 send_lock과 완전 분리."""
    global _audio_ws
    await ws.accept()
    _audio_ws = ws
    log.info(f"🔊 오디오 채널 연결: {ws.client}")

    async def audio_only_loop():
        while True:
            try:
                pcm = await asyncio.to_thread(audio_q2.get, True, 1.0)
            except queue.Empty:
                continue
            if not STATE.get("audio"):
                continue
            try:
                await ws.send_bytes(b"AUD" + pcm)
            except (WebSocketDisconnect, RuntimeError):
                return

    task = asyncio.create_task(audio_only_loop())
    try:
        # 연결 유지 — 폰 → 서버 방향 메시지 없음 (명령은 /ws 로만)
        await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        task.cancel()
        if _audio_ws is ws:
            _audio_ws = None
        log.info("🔊 오디오 채널 종료")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    peer = ws.client
    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
        auth = json.loads(first)
    except Exception:
        await ws.close(code=4001)
        return
    if auth.get("type") != "auth" or auth.get("token") != TOKEN:
        try:
            await ws.send_text(json.dumps({"type": "auth_fail"}))
        except Exception:
            pass
        await ws.close(code=4003)
        log.warning(f"인증 실패: {peer}")
        return

    # 버전 체크 — 폰이 들고 있는 phone.html이 구버전이면 즉시 자동 새로고침
    try:
        cur_ver = str(int(PHONE_HTML.stat().st_mtime))
        if auth.get("ver") and auth.get("ver") != cur_ver:
            await ws.send_text(json.dumps({"type": "reload"}))
            await ws.close()
            log.info(f"📲 구버전 폰 → 자동 새로고침 ({auth.get('ver')}→{cur_ver})")
            return
    except Exception:
        pass

    send_lock = asyncio.Lock()

    async def send_json(d: dict):
        async with send_lock:
            await ws.send_text(json.dumps(d, ensure_ascii=False))

    # 🔒 단일 폰 정책 — 로컬 연결만 적용. 릴레이(_RelayWS)는 제외.
    # (릴레이와 로컬이 서로 _active_socks를 통해 kick하면 무한 재연결 루프 발생)
    _is_relay = isinstance(ws, _RelayWS)
    # 🛡 릴레이 유령 가드 (2026-07-13) — 릴레이 뒤에 실제 폰이 없어도 PC가 초당 20~30장을
    # 인코딩·업로드하던 낭비/로그오염 차단. 폰의 실제 인바운드 신호가 최근일 때만 스트리밍.
    _relay_alive = [0.0]   # 릴레이 뒤 폰 마지막 인바운드 시각 (0=아직 없음 → 스트리밍 정지)
    # 🔋 ECO 모드 (페이블 설계 2026-08-13 밤) — 이 연결에만 적용되는 화면전송 정지 플래그.
    #   릴레이 유령 가드(_relay_alive)와 동일한 "연결별 클로저 리스트 + 루프 스킵" 패턴 재사용.
    _lite = [False]

    global _push
    # _push는 직접(Tailscale/로컬) 연결만 추적 — relay 덮어쓰기 방지
    if not _is_relay:
        _push = send_json
    if not _is_relay:
        for _old in list(_active_socks):
            if _old is ws:
                continue
            try:
                await _old.close(code=4000)
            except Exception:
                pass
        _active_socks.clear()
        _active_socks.append(ws)

    async def send_frame(b: bytes):
        async with send_lock:
            await ws.send_bytes(b)

    await send_json({"type": "auth_ok"})
    # 연결 시 저장된 설정 자동 전송 — 새 오리진(Render)에서 접속해도 동일한 레이아웃 복원
    try:
        _settings_file = CANONICAL / "settings_backup.json"
        if _settings_file.exists():
            _settings = json.loads(_settings_file.read_text(encoding="utf-8"))
            await send_json({"type": "restore_settings", "data": _settings})
    except Exception as _e:
        log.warning(f"설정 전송 실패: {_e}")
    # 연결 시 현재 보고 있는 모니터(cur)도 알려줌 → 폰 썸네일 선택이 실제 화면과 일치
    _cur = mss_to_win(STATE["monitor"])
    await send_json({"type": "monitors", "list": monitor_list(),
                     "cur": _cur, "curKey": _mon_key_num(_cur)})
    log.info(f"✅ 인증 성공: {peer}")

    async def capture_loop():
        # 직렬 루프: 캡처 → 전송완료까지 대기 → 다시 캡처. 큐 없음 = 버퍼링 불가.
        last_hash = None
        last_cur = None
        last_shape = None
        _err_streak = 0  # 연속 캡처 실패 카운터
        _my_turbo_gen = -1   # 이 연결이 아는 인코더 세대 — 다르면 turbo_start 재전송+키프레임 대기
        _turbo_wait_key = False
        _turbo_last_au = 0.0   # 마지막 AU 수신 시각 — 3초 가뭄이면 자동 OFF(검은화면 방지)
        # 📶 적응 비트레이트 (2026-07-05) — LTE 출렁임에 20M 고정이면 몇 초씩 밀려 "먹통+새로고침" 증상.
        # 전송 0.3초+ 지연 5회 누적 → 한 단계 강하(20→12→8→5M), 90초 원활 → 한 단계 승급(사용자 상한까지)
        # [2026-08-29 페이블 실측] 바닥 5M이 회선 실효속도보다 높으면 영구 혼잡(버퍼블로트) —
        # 핑 1813ms 스파이크 + 터보 OFF 순간 밀린 프레임 442장 일괄 도착으로 실증. 바닥을 1.2M까지 확장.
        _TB_LADDER = [20000, 12000, 8000, 5000, 3000, 2000, 1200]
        _tb_slow = 0
        _tb_last_step = time.time()
        # [2026-08-29 페이블 실측] 정지화면은 AU가 초소형이라 "전송 0.3초+" 감지가 장님이 됨 —
        # 실혼잡은 인코더 큐 넘침(P프레임 GOP드랍→키만 생존≈1fps)으로 나타남(19:55 실측).
        # 큐 드랍 카운터 증가 = 혼잡 확정 신호로 사다리에 배선.
        _tb_drops_seen = 0
        _au_sent = 0   # 🔎 [진단 2026-08-29] 이 연결이 실제 송신한 AU 수 — "서버는 보내는데 폰 수신 0" 미스터리 추적
        # 🛡 UAC 보안데스크톱 감시 상태 (2026-08-07 대표님 발주) — 1.5초마다 판정, 변화 순간에만 폰에 통보
        _sd_state = False
        _sd_last = 0.0
        while True:
            t0 = time.time()
            # 🛡 UAC 감지 → 폰 중앙 알림 신호 (★서버 재시작 후부터 유효)
            if t0 - _sd_last > 1.5:
                _sd_last = t0
                try:
                    _sd_now = await asyncio.to_thread(secure_desktop_active)
                    if _sd_now != _sd_state:
                        _sd_state = _sd_now
                        await send_json({"type": "pc_block", "on": _sd_now, "reason": "uac"})
                        log.info(f"🛡 보안데스크톱 {'감지 → 폰 알림' if _sd_now else '해제 → 폰 알림 소거'}")
                except Exception:
                    pass
            interval = 1.0 / STATE.get("q_fps", LTE_FPS if STATE.get("lte") else FPS)
            # 🛡 릴레이 유령 가드 — 릴레이 뒤 폰이 20초+ 조용하면 스트리밍 정지(캡처·인코딩·전송 스킵)
            if _is_relay and (time.time() - _relay_alive[0] > 20):
                await asyncio.sleep(1.0)
                continue
            # 🔋 ECO 모드 — 커서·터보·JPEG 전부 스킵(폰 라디오 웨이크업 0).
            #   해제 시 깨끗한 키프레임부터 재개되도록 터보 세대를 리셋(재개 핸드셰이크 강제 — 뭉개짐 방지).
            if _lite[0]:
                _my_turbo_gen = -1
                await asyncio.sleep(1.0)
                continue
            try:
                cur = cursor_ratio(STATE["monitor"])
                shp = cursor_shape()
                if cur is not None and (cur != last_cur or shp != last_shape):
                    await send_json({"type": "cursor", "rx": cur[0],
                                     "ry": cur[1], "shape": shp})
                    last_cur = cur
                    last_shape = shp
                # ── 🚀 터보(H.264) 경로 — JPEG 경로는 아래 그대로 동결 ──
                if STATE.get("turbo"):
                    # 🛡 영상 소유권 = 최신 연결 1개만. 유령(릴레이 옛페이지)이 AU 반씩 훔치면
                    # 참조체인 깨져 화면 뭉개짐 (2026-07-03 실증: 인증 2건→기동 2번→프레임 분탈)
                    if _active_socks and ws is not _active_socks[-1]:
                        await asyncio.sleep(0.5)
                        continue
                    _m = _mon(STATE["monitor"])
                    # 이 연결의 첫 진입이면 강제 재기동 → 첫 프레임 IDR 보장
                    _enc, _gen = await asyncio.to_thread(
                        _turbo_ensure, _m,
                        STATE.get("turbo_fps", TURBO_FPS),
                        STATE.get("turbo_kbps", TURBO_KBPS),
                        _my_turbo_gen == -1)
                    if _gen != _my_turbo_gen:
                        _my_turbo_gen = _gen
                        _turbo_wait_key = True
                        _turbo_last_au = time.time()
                        _tb_drops_seen = 0   # 새 인코더 = 드랍 카운터 0부터 — 기준점 동기화 [2026-08-29 페이블]
                        _au_sent = 0         # 🔎 [진단] 이 연결이 보낸 AU 수
                        await send_json({"type": "turbo_start", "w": _enc.ow, "h": _enc.oh,
                                         "fps": _enc.fps, "monitor": mss_to_win(STATE["monitor"])})
                    au = await asyncio.to_thread(_enc.get_au, 0.5)
                    if au is not None:
                        _turbo_last_au = time.time()
                        _is_key, _bytes = au
                        if _turbo_wait_key and not _is_key:
                            pass                        # 디코더가 못 여는 P프레임 스킵
                        else:
                            _turbo_wait_key = False
                            _t_send = time.time()
                            await send_frame(b"V264" + (b"\x01" if _is_key else b"\x00") + _bytes)
                            _au_sent += 1
                            if _au_sent % 100 == 1:   # 🔎 [진단] ~3초마다 1줄 — 누구에게 흐르는가
                                log.info(f"🔎 AU송신 {_au_sent}개째 → {getattr(ws, 'client', '?')} (relay={_is_relay})")
                            # 📶 적응 비트레이트 — 전송 지연으로 회선 혼잡 감지
                            _now = time.time()
                            if _now - _t_send > 0.3:
                                _tb_slow += 1
                            elif _tb_slow > 0:
                                _tb_slow -= 1
                            _cur_k = STATE.get("turbo_kbps", TURBO_KBPS)
                            _d_now = getattr(_enc, "drops", 0)
                            if _d_now > _tb_drops_seen:
                                _tb_drops_seen = _d_now
                                _tb_slow = max(_tb_slow, 2)   # 큐 넘침 = 실혼잡 확정 → 즉시 강하 자격 [2026-08-29 페이블]
                            if _now - _t_send > 1.0 and _now - _tb_last_step > 5:
                                _tb_slow = 99   # 🚨 1초+ 블록 = 비상 — 즉시 강하 자격
                            if _tb_slow >= 2 and _now - _tb_last_step > 8:
                                _low = next((k for k in _TB_LADDER if k < _cur_k), None)
                                if _low:
                                    STATE["turbo_kbps"] = _low
                                    _tb_last_step = _now
                                    _tb_slow = 0
                                    log.info(f"📶 회선 혼잡 → 터보 {_cur_k//1000}M→{_low//1000}M 강하")
                            elif (_tb_slow == 0 and _now - _tb_last_step > 90
                                    and _cur_k < STATE.get("turbo_kbps_max", TURBO_KBPS)):
                                _up = next((k for k in reversed(_TB_LADDER)
                                            if k > _cur_k and k <= STATE.get("turbo_kbps_max", TURBO_KBPS)), None)
                                if _up:
                                    STATE["turbo_kbps"] = _up
                                    _tb_last_step = _now
                                    log.info(f"📶 회선 원활 → 터보 {_cur_k//1000}M→{_up//1000}M 승급")
                            # 🎨 키프레임마다 뷰 중심색 프로브 — 폰 캔버스 중앙과 자동 비교(줌 무관)
                            # ⚖️ 제23조: 판정기준(_judge) 없으면 기동 거부 / 측정조건: 정지화면에서만
                            # (움직이는 화면 비교 = 색이 아니라 시차 측정 → Δ16 오탐, judge 개정 2026-07-05)
                            if (_is_key and _judge is not None
                                    and getattr(_enc, "idle_grabs", 0) >= _judge.get("측정조건_still_grabs", 5)):
                                _v = STATE.get("view", {})
                                _rgb = _enc.probe_at(
                                    (_v.get("rx1", 0.0) + _v.get("rx2", 1.0)) / 2,
                                    (_v.get("ry1", 0.0) + _v.get("ry2", 1.0)) / 2)
                                if _rgb:
                                    await send_json({"type": "turbo_probe", "rgb": _rgb})
                    elif (time.time() - _turbo_last_au > (8.0 if _turbo_wait_key else 3.0)
                          and time.time() - getattr(_enc, "last_read", 0) > 3.0):
                        # 가뭄 = "인코더 판독 심박까지 멎음"일 때만. GOP드랍(혼잡 시 다음 키까지 의도적 무송출)은
                        # 가뭄이 아니다 — 04:02 심박계측: 판독 173AU 정상인데 가뭄 오판으로 터보 오살(실증)
                        # 🛟 프레임 가뭄 3초 = 인코더/캡처 고장 → 자동 JPEG 복귀 (검은화면 원천봉쇄)
                        log.warning("터보: 3초간 프레임 없음 → 자동 OFF, JPEG 복귀")
                        STATE["turbo"] = False
                        await asyncio.to_thread(_turbo_stop)
                        _my_turbo_gen = -1
                        try:
                            await send_json({"type": "turbo_ok", "on": False})
                            await send_json({"type": "toast", "text": "터보 일시 중단 → 일반 모드 자동 복귀"})
                        except Exception:
                            pass
                    _err_streak = 0
                    continue    # JPEG 경로·interval 스킵 (인코더가 fps 페이싱)
                elif _my_turbo_gen != -1:
                    _my_turbo_gen = -1                  # 터보 껐음 → JPEG 복귀, 인코더는 자가종료
                jpeg, last_hash = await asyncio.to_thread(
                    grab_region, STATE["view"], last_hash)
                if jpeg is not None:
                    await send_frame(jpeg)
                _err_streak = 0  # 성공 시 초기화
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception as e:
                _err_streak += 1
                log.warning(f"캡처 오류: {e}")
                if _err_streak % 10 == 0:  # 10회 연속 실패마다 mss 재초기화 + dxcam 재시도
                    global _mss_generation, _dxcam_mod
                    _mss_generation += 1
                    _dxcam_reset()  # dxcam 전 인스턴스·맵 폐기 → 재시도 허용
                    if _dxcam_mod is None:  # import 실패였으면 다시 시도
                        try:
                            import dxcam as _dxcam_mod
                        except Exception:
                            pass
                    log.info(f"캡처 재초기화 시도 (연속 {_err_streak}회 실패)")
                    await asyncio.sleep(2.0)  # GPU 안정화 대기
            await asyncio.sleep(max(0.0, interval - (time.time() - t0)))

    async def audio_loop():
        # 오디오 청크 — /ws-audio 연결 시 드레인만, 미연결 시 폴백 전송
        while True:
            try:
                pcm = await asyncio.to_thread(audio_q.get, True, 1.0)
            except queue.Empty:
                continue
            if not STATE.get("audio"):
                continue
            if _audio_ws is not None:
                continue   # 전용 채널이 살아있으면 /ws 에선 보내지 않음
            try:
                await send_frame(b"AUD" + pcm)
            except (WebSocketDisconnect, RuntimeError):
                return

    async def video_loop():
        # 영상 플레이어 상태(STATE['video']) 변화 → 폰에 video_state 알림
        last = None
        while True:
            cur = STATE.get("video", False)
            if cur != last:
                last = cur
                try:
                    await send_json({"type": "video_state", "playing": cur})
                except (WebSocketDisconnect, RuntimeError):
                    return
            await asyncio.sleep(0.5)

    async def context_loop():
        # 활성 창 종류(파일/웹/영상) 변화 → 폰 도크 자동 교체용 context 알림
        last = None
        while True:
            try:
                cur = await asyncio.to_thread(detect_context)
                if cur != last:
                    last = cur
                    await send_json({"type": "context", "ctx": cur})
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception:
                pass
            await asyncio.sleep(0.8)

    cap_task = asyncio.create_task(capture_loop())
    audio_task = asyncio.create_task(audio_loop())
    video_task = asyncio.create_task(video_loop())
    context_task = asyncio.create_task(asyncio.sleep(0))  # context_loop 비활성화 (ctxDock 깜빡임 방지)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # 🛡 릴레이 유령가드 본질수정 [2026-08-29 페이블] — 예전엔 "모든 인바운드"가 생존 갱신이라
            # 백그라운드 유령 탭의 자동 비콘(ping 2초·frame_report 10초)만으로 영원히 살아있음 판정,
            # PC가 죽은 탭에 무한 송출(13:38 인코더 재기동 폭풍 실증). 자동 비콘 2종만 갱신에서 제외 —
            # 그 외 모든 메시지(터치·휠·키·뷰 등 사용자 행위)는 기존대로 생존 갱신.
            if _is_relay and msg.get("type") not in ("ping", "frame_report"):
                _relay_alive[0] = time.time()
            # 🔎 [2026-08-29 페이블 진단] 터보 ON/OFF를 "누가" 보냈는지 발신자 주소 기록 —
            # 유령 발신 turbo_mode OFF(기본 20000kbps) 추적용. 기능 무접촉, 로그 1줄.
            if msg.get("type") == "turbo_mode":
                log.info(f"🔎 turbo_mode 발신자: {getattr(ws, 'client', '?')} on={msg.get('on')} kbps={msg.get('kbps', '기본')}")
            # 🔋 ECO 모드 토글 + 스냅샷 1장 — 연결별 상태라 공용 handle_message가 아닌 여기서 처리.
            if msg.get("type") == "lite":
                _lite[0] = bool(msg.get("on"))
                log.info(f"🔋 ECO 모드 {'ON — 화면전송 정지' if _lite[0] else 'OFF — 화면전송 재개'}")
                continue
            if msg.get("type") == "lite_snap":
                try:
                    _jpeg, _ = await asyncio.to_thread(grab_region, STATE["view"], None)  # None=변화무관 강제 1장
                    if _jpeg is not None:
                        await send_frame(_jpeg)
                except Exception as _e:
                    log.warning(f"🔋 ECO 스냅샷 실패: {_e}")
                continue
            try:
                resp = await handle_message(msg)
                if resp is not None:
                    await send_json(resp)
            except Exception as e:
                log.warning(f"메시지 오류 ({msg.get('type')}): {e}")
    except WebSocketDisconnect:
        log.info(f"연결 종료: {peer}")
    except Exception as e:
        log.warning(f"WS 루프 오류: {e}")
    finally:
        cap_task.cancel()
        audio_task.cancel()
        video_task.cancel()
        context_task.cancel()
        if not _is_relay:
            try:
                _active_socks.remove(ws)
            except ValueError:
                pass
        if _push is send_json:         # 내 연결이 등록돼 있으면 해제
            _push = None


# ─────────────────────────── 메인 ───────────────────────────
def get_tailscale_ip() -> str:
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"],
                                      text=True, timeout=2).strip()
        return out.splitlines()[0] if out else "0.0.0.0"
    except Exception:
        return "(Tailscale IP 확인 필요)"


RELAY_URL = "wss://tapdesk.onrender.com/ws"


class _RelayWS:
    """websockets 클라이언트를 FastAPI WebSocket처럼 보이게 하는 어댑터.
    ws_endpoint(adapted) 로 호출하면 릴레이 연결을 직접 연결처럼 처리."""
    def __init__(self, raw_ws):
        self._ws = raw_ws
        # ws_endpoint가 첫 receive_text()로 인증 메시지를 기다림 → 미리 준비
        self._first = json.dumps({"type": "auth", "token": TOKEN, "ver": ""})
        self.client = type("C", (), {"host": "relay", "port": 443})()

    async def accept(self): pass  # 이미 연결됨

    async def send_text(self, text: str):
        await self._ws.send(text)

    async def send_bytes(self, data: bytes):
        await self._ws.send(data)

    async def receive_text(self) -> str:
        if self._first is not None:
            msg, self._first = self._first, None
            return msg
        msg = await self._ws.recv()
        return msg if isinstance(msg, str) else msg.decode("utf-8")

    async def receive(self) -> dict:
        msg = await self._ws.recv()
        if isinstance(msg, str):
            return {"text": msg, "bytes": None}
        return {"text": None, "bytes": msg}

    async def close(self, code: int = 1000):
        try:
            await self._ws.close()
        except Exception:
            pass


async def _relay_client():
    import websockets as _wss
    log.info(f"🌐 Render 릴레이 클라이언트 → {RELAY_URL}")
    while True:
        try:
            async with _wss.connect(
                RELAY_URL,
                additional_headers={"User-Agent": "TapDesk-PC/1.0"},
                ping_interval=30,
                ping_timeout=60,
                open_timeout=15,
            ) as conn:
                # 릴레이 서버에 PC로 등록
                await conn.send(json.dumps({"type": "auth", "token": TOKEN, "role": "pc"}))
                resp = await asyncio.wait_for(conn.recv(), timeout=10.0)
                data = json.loads(resp)
                if data.get("type") != "relay_ok":
                    log.warning(f"릴레이 auth 실패: {data}")
                    continue
                log.info("✅ Render 릴레이 연결됨 — 폰 대기 중")
                adapted = _RelayWS(conn)
                await ws_endpoint(adapted)
        except Exception as e:
            log.warning(f"릴레이 재연결 ({type(e).__name__}: {e}), 5초 후...")
            await asyncio.sleep(5)


_relay_started = False      # TLS 제2 uvicorn의 중복 릴레이 접속 방지

@app.on_event("startup")
async def _start_relay():
    global _relay_started
    if _relay_started:
        return
    _relay_started = True
    asyncio.create_task(_relay_client())


def _boot_trace(stage: str):
    """🔍 기동 단계 발자국 — 어디서 멈추는지 즉시 파악 (2026-07-04 좀비 20마리 사건)."""
    try:
        with open(CANONICAL / "_boot_trace.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] pid={os.getpid()} {stage}\n")
    except Exception:
        pass


def main():
    _boot_trace("main 진입")
    import uvicorn
    _boot_trace("uvicorn import OK")
    ensure_icon()
    _boot_trace("ensure_icon OK")
    threading.Thread(target=audio_capture_thread, daemon=True).start()
    # threading.Thread(target=video_detect_thread, daemon=True).start()  # 영상 감지 비활성화 (vidCtrl 깜빡임 방지)
    ip = get_tailscale_ip()
    _boot_trace(f"tailscale_ip OK ({ip})")
    print("=" * 54)
    print("  폴드5 PC 원격제어 — 경량 백엔드")
    print("=" * 54)
    print(f"  로컬       : http://localhost:{PORT}")
    print(f"  폰(직결)   : http://{ip}:{PORT}")
    print(f"  토큰       : {TOKEN}")
    print("=" * 54)
    # WebSocket 연결 유지 — 폰 백그라운드/LTE 약함에도 견디게 ping 간격/타임아웃 늘림
    # 포트 bind 재시도 — kill 직후 TIME_WAIT 소켓이 남아 있으면 해제될 때까지 대기
    import socket as _sock
    import urllib.request as _ur
    for _retry in range(60):
        try:
            _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            # [2026-07-08] REUSEADDR=1이면 점유 중에도 bind 통과(윈도우) → "비었다" 오판으로
            # 반쪽 서버(7443만) 탄생했던 사고. 검사는 REUSEADDR 없이 정직하게.
            _s.bind(("0.0.0.0", PORT))
            _s.close()
            break
        except OSError:
            _s.close()
            if _retry == 0:
                log.warning(f"⏳ 포트 {PORT} 점유 중 — 선임에게 은퇴 신호 보내며 대기...")
            try:
                # 👴 선임 강제 은퇴 — 늙은 주인이 관리자 권한이라 외부에서 못 죽여도,
                # 자기 스스로는 나갈 수 있다(/restart-self). 권한 문제 원천 소멸.
                _ur.urlopen(f"http://127.0.0.1:{PORT}/restart-self?token={TOKEN}", timeout=3)
                log.info("👴 기존 7780 주인에게 은퇴 신호 전송")
            except Exception:
                pass
            time.sleep(2)
    _boot_trace("7780 bind 사전확인 통과")
    # [2026-07-03 터보] 7780 평문 + 7443 TLS(wss — 폰 WebCodecs는 보안컨텍스트 필수)를
    # 같은 이벤트루프에서 동시 서빙 — 스레드 분리 시 /reload·클립보드 push가 루프 갈라져 죽음.
    _kw = dict(log_level="warning", ws_ping_interval=60, ws_ping_timeout=120)
    _crt, _key = CANONICAL / "_ts.crt", CANONICAL / "_ts.key"
    _tls_ok = _crt.exists() and _key.exists()
    if _tls_ok:
        print(f"  🔐 wss TLS 리스너: https://aris-kim.taild92ffe.ts.net:{TLS_PORT}")
    else:
        print("  ⚠ _ts.crt/_ts.key 없음 → TLS(7443) 생략, 평문 7780만")

    async def _tls_pulse_watchdog(s2, port):
        # [2026-08-29] Accept failed(WinError 64) 뒤 serve()가 반환을 안 해서
        # 아래 불사조 루프가 영영 안 돌던 사고(실증) → 자기 포트에 직접 TCP 접속해
        # 맥박을 재고, 3연속 무응답이면 should_exit로 serve()를 강제 반환시킨다.
        await asyncio.sleep(30)
        fails = 0
        while True:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=3)
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                fails = 0
            except Exception:
                fails += 1
                log.warning(f"TLS({port}) 맥박확인 실패 {fails}/3")
                if fails >= 3:
                    log.error(f"TLS({port}) 3연속 무응답 → serve() 강제 반환시켜 재기동")
                    s2.should_exit = True
                    return
            await asyncio.sleep(30)

    async def _serve_tls_forever():
        # [2026-07-04] 7443이 바인드 실패·소켓사고로 조용히 죽으면 v18 앱이 못 들어옴(실증)
        # → 죽어도 30초마다 부활하는 불사조 루프. 7780은 건드리지 않음.
        while True:
            wd = None
            try:
                s2 = uvicorn.Server(uvicorn.Config(
                    app, host="0.0.0.0", port=TLS_PORT,
                    ssl_certfile=str(_crt), ssl_keyfile=str(_key), **_kw))
                log.info(f"🔐 TLS({TLS_PORT}) 리스너 기동")
                wd = asyncio.create_task(_tls_pulse_watchdog(s2, TLS_PORT))
                await s2.serve()
                log.warning(f"TLS({TLS_PORT}) 서버 내려감 → 30초 후 재기동")
            except Exception as e:
                log.warning(f"TLS({TLS_PORT}) 오류: {e} → 30초 후 재기동")
            finally:
                if wd is not None:
                    wd.cancel()
            await asyncio.sleep(30)

    async def _serve_main():
        await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, **_kw)).serve()
        # [2026-07-08] 7780이 죽었는데 프로세스가 7443만 물고 반쪽 생존하던 사고 →
        # 본체가 죽으면 통째로 나간다. 경비원이 깨끗한 새 몸으로 부활시킴.
        log.error(f"⚰ {PORT} 본체 서버 종료 → 반쪽 생존 금지, 프로세스 전체 퇴장")
        os._exit(1)

    async def _serve_all():
        _boot_trace("serve_all 진입 — 7780 리스너 기동")
        tasks = [_serve_main()]
        if _tls_ok:
            tasks.append(_serve_tls_forever())
        await asyncio.gather(*tasks)

    _boot_trace("asyncio.run 직전")
    asyncio.run(_serve_all())


if __name__ == "__main__":
    _boot_trace("모듈 임포트 전체 완료")
    main()
