# -*- coding: utf-8 -*-
"""물리 모니터 안정 식별 — 자리(인덱스)가 아니라 '고정 ID'로 인식.
   · 고정 ID = EnumDisplayDevices 장치 인터페이스 경로 (EDID 기반, 재부팅·재도킹해도 불변)
   · 노트북 내장패널 = QueryDisplayConfig OUTPUT_TECHNOLOGY_INTERNAL 로 자동 감지 → 항상 1번 앵커
   · mss 는 순서/사각형만 주므로 → 사각형(좌표·크기)으로 고정 ID에 매칭
   server.py 가 이 모듈로 모니터를 식별한다. 실패하면 좌표순 폴백.
"""
import ctypes
from ctypes import wintypes

try:
    import win32api
    import win32con
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

EDD_GET_DEVICE_INTERFACE_NAME = 0x1
QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL = 0x80000000
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]


class _PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", wintypes.UINT), ("modeInfoIdx", wintypes.UINT),
                ("outputTechnology", wintypes.UINT), ("rotation", wintypes.UINT),
                ("scaling", wintypes.UINT), ("refreshNum", wintypes.UINT),
                ("refreshDen", wintypes.UINT), ("scanLine", wintypes.UINT),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.UINT)]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _PATH_SOURCE), ("targetInfo", _PATH_TARGET), ("flags", wintypes.UINT)]


class _MODE_INFO(ctypes.Structure):       # 내용 안 씀 — 크기(64B)만 맞춤
    _fields_ = [("_pad", ctypes.c_byte * 64)]


class _DEVINFO_HEADER(ctypes.Structure):
    _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT),
                ("adapterId", LUID), ("id", wintypes.UINT)]


class _TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", _DEVINFO_HEADER), ("flags", wintypes.UINT),
                ("outputTechnology", wintypes.UINT),
                ("edidManufactureId", wintypes.USHORT),
                ("edidProductCodeId", wintypes.USHORT),
                ("connectorInstance", wintypes.UINT),
                ("friendlyName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


def _internal_device_paths():
    """내장 패널(노트북)들의 monitorDevicePath 집합. 실패 시 빈 집합."""
    paths = set()
    try:
        u32 = ctypes.windll.user32
        nPath = wintypes.UINT(0)
        nMode = wintypes.UINT(0)
        if u32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,
                                           ctypes.byref(nPath), ctypes.byref(nMode)) != 0:
            return paths
        paths_arr = (_PATH_INFO * nPath.value)()
        modes_arr = (_MODE_INFO * nMode.value)()
        if u32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS,
                                  ctypes.byref(nPath), paths_arr,
                                  ctypes.byref(nMode), modes_arr, None) != 0:
            return paths
        for k in range(nPath.value):
            tdn = _TARGET_DEVICE_NAME()
            tdn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
            tdn.header.size = ctypes.sizeof(_TARGET_DEVICE_NAME)
            tdn.header.adapterId = paths_arr[k].targetInfo.adapterId
            tdn.header.id = paths_arr[k].targetInfo.id
            if u32.DisplayConfigGetDeviceInfo(ctypes.byref(tdn)) == 0:
                if tdn.outputTechnology == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL:
                    if tdn.monitorDevicePath:
                        paths.add(tdn.monitorDevicePath.lower())
    except Exception:
        pass
    return paths


def physical_monitors():
    """활성 물리 모니터 목록.
    각 항목: {rect:(left,top,width,height), stable_id, friendly, internal:bool}
    win32 없으면 빈 리스트(서버는 좌표순 폴백)."""
    if not _HAS_WIN32:
        return []
    internal_paths = _internal_device_paths()
    out = []
    i = 0
    while True:
        try:
            adapter = win32api.EnumDisplayDevices(None, i, 0)
        except Exception:
            break
        if not adapter or not adapter.DeviceName:
            break
        try:
            attached = bool(adapter.StateFlags & win32con.DISPLAY_DEVICE_ATTACHED_TO_DESKTOP)
        except Exception:
            attached = True
        if attached:
            try:
                mon = win32api.EnumDisplayDevices(adapter.DeviceName, 0, EDD_GET_DEVICE_INTERFACE_NAME)
                stable_id = mon.DeviceID or adapter.DeviceName
                friendly = mon.DeviceString or ""
            except Exception:
                stable_id = adapter.DeviceName
                friendly = ""
            try:
                s = win32api.EnumDisplaySettings(adapter.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
                rect = (int(s.Position_x), int(s.Position_y),
                        int(s.PelsWidth), int(s.PelsHeight))
            except Exception:
                rect = None
            internal = stable_id.lower() in internal_paths
            if rect:
                out.append({"rect": rect, "stable_id": stable_id,
                            "friendly": friendly, "internal": internal})
        i += 1
    return out


def ordered():
    """논리 순서로 정렬한 물리 모니터: 노트북(내장) 먼저, 그 다음 (left, top) 순.
    server.py 가 이걸로 1=노트북 고정 + 결정적 순서를 만든다."""
    mons = physical_monitors()
    mons.sort(key=lambda m: (0 if m["internal"] else 1, m["rect"][0], m["rect"][1]))
    return mons


if __name__ == "__main__":
    import json
    mons = physical_monitors()
    od = ordered()
    print("=== physical_monitors ===")
    for m in mons:
        print("  internal=%-5s rect=%s  id=%s  (%s)" %
              (m["internal"], m["rect"], m["stable_id"][:60], m["friendly"]))
    print("=== ordered (1=노트북) ===")
    for n, m in enumerate(od, start=1):
        print("  %d번: internal=%-5s rect=%s" % (n, m["internal"], m["rect"]))
