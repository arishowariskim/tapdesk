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
TOKEN_FILE = ROOT / "token.txt"
PHONE_HTML = ROOT / "phone.html"
ICON_FILE = ROOT / "icon.png"
LOG_FILE = ROOT / "agent.log"

PORT = int(os.environ.get("PORT", 7780))
FPS = 30            # dxcam GPU 캡처 → 30fps 가능
MAX_WIDTH = 1600
JPEG_QUALITY = 85

# LTE 경량 모드
LTE_FPS = 20        # turbojpeg 고속 인코딩 → 20fps 가능
LTE_MAX_WIDTH = 900
LTE_QUALITY = 50    # 데이터 절반 → 체감 속도 2배

# 소리 — PC 시스템 사운드를 폰으로 (WASAPI 루프백)
AUDIO_SR = 24000       # 샘플레이트 (mono)
AUDIO_CHUNK = 960      # 청크 크기 (40ms)

# 런타임 상태 — 단일 사용자
#  view = 모니터 위 사각형(0~1 비율) + 폰이 원하는 출력 가로 w(px)
STATE = {
    "monitor": 1,
    "monitor_key": "",   # EDID 하드웨어 키(LAP/모델_UID) = 진실. 번호는 여기서 파생.
    "view": {"rx1": 0.0, "ry1": 0.0, "rx2": 1.0, "ry2": 1.0, "w": 1280},
    "lte": False,
    "audio": False,
    "video": False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fold5")

# 소리 — PC 시스템 사운드 캡처 (WASAPI 루프백) → 큐 → 폰
audio_q: "queue.Queue" = queue.Queue(maxsize=24)

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


# ───────────────────────── 화면 캡처 ─────────────────────────
_mss_local = threading.local()


def get_mss() -> "mss.MSS":
    inst = getattr(_mss_local, "inst", None)
    if inst is None:
        inst = mss.MSS()
        _mss_local.inst = inst
    return inst


# dxcam 싱글턴 (GPU DDA 캡처 — mss 대비 10배 빠름)
# 주의: dxcam은 output_idx 별로 별도 인스턴스가 필요하지만,
# 노트북(1번) 이외 모니터는 mss로 안전하게 처리
import dxcam as _dxcam_mod
_dxcam_inst = None
_dxcam_lock = threading.Lock()

# turbojpeg 싱글턴 (PIL WebP 대비 5배 빠른 JPEG 인코딩)
from turbojpeg import TurboJPEG, TJPF_RGB, TJPF_BGRA
_turbo_jpeg = TurboJPEG()


def get_dxcam():
    # dxcam 비활성화 — cv2 의존 충돌 + 멀티모니터 좌표 문제.
    # mss로 통일 (멀티모니터 완벽 지원, 속도 충분)
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
            _mss_local.inst = mss.MSS()
            s = _mss_local.inst
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
    """뷰포트 영역만 캡처. 안 변했으면 (None,hash). 출력은 폰 액정 크기에 맞춤."""
    m = _mon(STATE["monitor"])
    box = crop_box(view)
    # dxcam은 primary monitor(origin=0,0)에서만 사용. 그 외는 mss.
    use_dxcam = (m["left"] == 0 and m["top"] == 0)
    cam = get_dxcam() if use_dxcam else None
    if cam is not None:
        # dxcam: GPU DDA 캡처 (primary 한정) — region은 (0,0) 기준 상대좌표
        region = (box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"])
        raw_np = cam.grab(region=region)
        if raw_np is None:
            return None, last_hash              # dxcam이 변화 없으면 None 반환
        h = hash(raw_np.tobytes())
        if h == last_hash:
            return None, h
        img = Image.fromarray(raw_np[:, :, ::-1])  # BGR → RGB
    else:
        # mss — numpy 직통 경로 (rgb 변환 없이 BGRA→BGRA 그대로 처리)
        import numpy as _np
        raw = get_mss().grab(box)
        # zero-copy: frombuffer로 mss 내부 버퍼 직접 참조 (bytes() 전체복사 제거)
        arr = _np.frombuffer(raw.raw, _np.uint8).reshape((raw.height, raw.width, 4))
        # 균등 샘플 해시 (~0.1ms): 64×64 격자로 ~4096픽셀 샘플
        h = hash(arr[::max(1, arr.shape[0] // 64), ::max(1, arr.shape[1] // 64)].tobytes())
        if h == last_hash:
            return None, h
        lte = STATE.get("lte", False)
        cap = STATE.get("q_width", LTE_MAX_WIDTH if lte else MAX_WIDTH)
        quality = STATE.get("q_quality", LTE_QUALITY if lte else JPEG_QUALITY)
        tw = max(160, min(int(view.get("w", 1280)), cap))
        if arr.shape[1] > tw:
            nh = max(1, round(arr.shape[0] * tw / arr.shape[1]))
            # PIL resize: BGRA(4채널)로 NEAREST (색상 무관, 크기만 줄임)
            img_bgra = Image.fromarray(arr).resize((tw, nh), Image.NEAREST)
            arr = _np.asarray(img_bgra)
        _t0 = time.time()
        # BGRA → JPEG 직통 (rgb 변환 0번)
        data = _turbo_jpeg.encode(arr, quality=quality, pixel_format=TJPF_BGRA)
        _frame_measure(len(data), arr.shape[1], arr.shape[0], lte, (time.time() - _t0) * 1000)
        return data, h
    # dxcam 경로 (현재 비활성, get_dxcam()=None)
    lte = STATE.get("lte", False)
    cap = STATE.get("q_width", LTE_MAX_WIDTH if lte else MAX_WIDTH)
    quality = STATE.get("q_quality", LTE_QUALITY if lte else JPEG_QUALITY)
    tw = max(160, min(int(view.get("w", 1280)), cap))
    if img.width > tw:
        nh = max(1, round(img.height * tw / img.width))
        img = img.resize((tw, nh), Image.NEAREST)
    _t0 = time.time()
    import numpy as _np
    data = _turbo_jpeg.encode(_np.asarray(img), quality=quality, pixel_format=TJPF_RGB)
    _frame_measure(len(data), img.width, img.height, lte, (time.time() - _t0) * 1000)
    return data, h


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


def capture_to_clipboard() -> dict:
    """현재 보고 있는 영역 → 클립보드(CF_DIB) → Ctrl+V."""
    if not HAS_WIN32:
        return {"type": "capture_fail", "msg": "pywin32 없음"}
    box = crop_box(STATE["view"])
    cam = get_dxcam()
    if cam is not None:
        region = (box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"])
        raw_np = cam.grab(region=region)
        img = Image.fromarray(raw_np[:, :, ::-1]) if raw_np is not None else Image.new("RGB", (1,1))
    else:
        raw = get_mss().grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    out = io.BytesIO()
    img.save(out, "BMP")
    bmp = out.getvalue()[14:]
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
    for _ in range(backspaces):
        pyautogui.press("backspace")
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
    if t == "monitor_reset":                    # [모니터 재셋팅] = server 프로세스 재시작 트리거
        # mss(화면캡처 엔진)가 스레드로컬이라, STATE만 리셋하면 캡처 스레드의 mss는 옛 모니터를 그대로 봄.
        # → 프로세스를 통째로 재시작해야 캡처 스레드 mss까지 새 모니터로 갱신됨(=내가 한 재시작과 동일). watchdog(7781)이 재기동.
        log.info("🔄 폰 요청 → server 재시작 (새 모니터 환경 인식)")
        import threading as _th, os as _os
        _th.Timer(0.6, lambda: _os._exit(0)).start()   # 응답 전송 뒤 0.6초 후 종료 → 경비원이 6~14초 내 재기동
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


@app.get("/cam")
async def camera_mouse():       # 🖐 카메라 슈퍼마우스(손·눈) — 추가 입력소스. 기존 입력 API(mouse_move/click)로만 연결, 본질 0변경.
    cam_html = ROOT / "camera_mouse.html"
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


@app.get("/reload")
async def reload_phone():
    if _push is None:
        return JSONResponse({"ok": False, "msg": "폰 미연결"})
    try:
        await _push({"type": "reload"})
        return JSONResponse({"ok": True, "msg": "폰 새로고침 신호 전송"})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})


# ⚙️ 설정 영구 백업 — 폰 localStorage(도크·위젯·줌 배치 등)를 PC 서버에 저장.
# 주소(origin)가 바뀌거나(localhost↔Tailscale) 앱 재설치·폰 교체해도 설정이 안 틀어지게 단일 진실 소스.
SETTINGS_FILE = ROOT / "settings_backup.json"

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


# 📁 내 배치 — 이름 붙여 여러 개 저장/불러오기 (4개 화면형태가 한 스냅샷에 다 들어감).
# 틀어져도 골라서 불러오면 그 UI로 복원. 파일은 layouts/<이름>.json
LAYOUTS_DIR = ROOT / "layouts"

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


DROP_DIR = ROOT / "_김대리_드롭박스"
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


@app.on_event("startup")
async def _start_watcher():
    asyncio.create_task(phone_html_watcher())
    asyncio.create_task(drop_watcher())
    asyncio.create_task(clipboard_watcher())


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

    global _push
    _push = send_json                  # 핫리로드 푸시용으로 현재 연결 등록

    # 🔒 단일 폰 정책 — 로컬 연결만 적용. 릴레이(_RelayWS)는 제외.
    # (릴레이와 로컬이 서로 _active_socks를 통해 kick하면 무한 재연결 루프 발생)
    _is_relay = isinstance(ws, _RelayWS)
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
        _settings_file = ROOT / "settings_backup.json"
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
        while True:
            t0 = time.time()
            interval = 1.0 / STATE.get("q_fps", LTE_FPS if STATE.get("lte") else FPS)
            try:
                cur = cursor_ratio(STATE["monitor"])
                shp = cursor_shape()
                if cur is not None and (cur != last_cur or shp != last_shape):
                    await send_json({"type": "cursor", "rx": cur[0],
                                     "ry": cur[1], "shape": shp})
                    last_cur = cur
                    last_shape = shp
                jpeg, last_hash = await asyncio.to_thread(
                    grab_region, STATE["view"], last_hash)
                if jpeg is not None:
                    await send_frame(jpeg)
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception as e:
                log.warning(f"캡처 오류: {e}")
            await asyncio.sleep(max(0.0, interval - (time.time() - t0)))

    async def audio_loop():
        # 오디오 청크에 "AUD" 머리표 — 화면(JPEG)과 구분
        while True:
            try:
                pcm = await asyncio.to_thread(audio_q.get, True, 1.0)
            except queue.Empty:
                continue
            if not STATE.get("audio"):
                continue
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


@app.on_event("startup")
async def _start_relay():
    asyncio.create_task(_relay_client())


def main():
    import uvicorn
    ensure_icon()
    threading.Thread(target=audio_capture_thread, daemon=True).start()
    # threading.Thread(target=video_detect_thread, daemon=True).start()  # 영상 감지 비활성화 (vidCtrl 깜빡임 방지)
    ip = get_tailscale_ip()
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
    for _retry in range(60):
        try:
            _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            _s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            _s.bind(("0.0.0.0", PORT))
            _s.close()
            break
        except OSError:
            _s.close()
            if _retry == 0:
                log.warning(f"⏳ 포트 {PORT} TIME_WAIT 대기 중...")
            time.sleep(2)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning",
                ws_ping_interval=60, ws_ping_timeout=120)


if __name__ == "__main__":
    main()
