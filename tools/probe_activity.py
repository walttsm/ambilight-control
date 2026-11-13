"""Lesson 15: what is the user doing? Probe the signals Windows offers.

1. SHQueryUserNotificationState: official "D3D full-screen app / presentation running?" signal.
2. Foreground window: owning process, and whether it covers its whole monitor.
3. (Media sessions need WinRT; see the print at the end.)
4. ProtectedContentMaskedOut: Windows blacked out DRM video in our capture.
"""
import ctypes as C
import sys
import time
from ctypes import wintypes as W
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight.capture_gpu import GpuCapture  # noqa: E402

QUNS = {1: "NOT_PRESENT (screensaver/locked)", 2: "BUSY (full-screen app or presentation)",
        3: "RUNNING_D3D_FULL_SCREEN (exclusive full-screen Direct3D)",
        4: "PRESENTATION_MODE", 5: "ACCEPTS_NOTIFICATIONS (normal desktop)",
        6: "QUIET_TIME", 7: "APP (Windows Store app full screen)"}

u32, k32 = C.windll.user32, C.windll.kernel32
u32.GetForegroundWindow.restype = W.HWND
u32.MonitorFromWindow.restype = W.HMONITOR


class MONITORINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


def process_name(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "?"
    buf, n = C.create_unicode_buffer(512), W.DWORD(512)
    k32.QueryFullProcessImageNameW(h, 0, buf, C.byref(n))
    k32.CloseHandle(h)
    return Path(buf.value).name


state = C.c_int()
C.windll.shell32.SHQueryUserNotificationState(C.byref(state))
print("1. notification state :", QUNS.get(state.value, state.value))

hwnd = u32.GetForegroundWindow()
pid = W.DWORD()
u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
title = C.create_unicode_buffer(256)
u32.GetWindowTextW(hwnd, title, 256)
r = W.RECT()
u32.GetWindowRect(hwnd, C.byref(r))
mi = MONITORINFO(C.sizeof(MONITORINFO))
u32.GetMonitorInfoW(u32.MonitorFromWindow(hwnd, 2), C.byref(mi))
m = mi.rcMonitor
covers = (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom)
print(f"2. foreground window  : {process_name(pid.value)}  \"{title.value[:60]}\"")
print(f"   covers its monitor : {covers}  (window {r.right - r.left}x{r.bottom - r.top}, "
      f"monitor {m.right - m.left}x{m.bottom - m.top})")

cap = GpuCapture(r"\\.\DISPLAY1", 4)
masked = frames = 0
end = time.perf_counter() + 2
while time.perf_counter() < end:
    if cap.next_frame(100) is not None:
        frames += 1
        masked += bool(cap._info.ProtectedContentMaskedOut)
cap.close()
print(f"4. capture            : {frames / 2:.0f} new frames/s, DRM-masked frames: {masked}")
print("3. media sessions     : need WinRT (GlobalSystemMediaTransportControlsSessionManager);"
      " not probed here")
