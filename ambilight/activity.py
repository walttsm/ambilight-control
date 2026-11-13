"""What is the user doing? Classifies activity once per second into "game", "video" or "desktop".

Signals (all cheap, polled on a background thread):
  * SHQueryUserNotificationState: Windows' own "full-screen app running?" state
        3 = exclusive full-screen Direct3D, 2 = BUSY (borderless full screen), 7 = store app full screen
  * Foreground window: owning process, and whether it is on the monitor we capture
  * Media sessions (WinRT GlobalSystemMediaTransportControlsSessionManager): which apps are
    *playing* media (browsers, players, Netflix...). Music apps are excluded via `audio_apps`.

Rules:
  game  : D3D full screen on our monitor, or borderless full screen by a process that is not a
          browser / video player / listed in `not_games`
  video : a dedicated video player or store app full screen, or any non-music media session playing
  else desktop. Reported every second without delay; the main loop debounces on/off.
"""
from __future__ import annotations

import asyncio
import ctypes as C
import threading
from ctypes import wintypes as W
from pathlib import Path

u32, k32 = C.windll.user32, C.windll.kernel32
u32.GetForegroundWindow.restype = W.HWND

QUNS_BUSY, QUNS_D3D_FULLSCREEN, QUNS_APP = 2, 3, 7
PLAYBACK_PLAYING = 4


def _notification_state() -> int:
    s = C.c_int()
    C.windll.shell32.SHQueryUserNotificationState(C.byref(s))
    return s.value


def _process_name(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf, n = C.create_unicode_buffer(512), W.DWORD(512)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, C.byref(n))
    k32.CloseHandle(h)
    return Path(buf.value).name if ok else ""


def _foreground(monitor_rect: tuple[int, int, int, int]) -> tuple[str, bool]:
    """(process name of the foreground window, is its center on our monitor?)"""
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return "", False
    pid = W.DWORD()
    u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
    r = W.RECT()
    u32.GetWindowRect(hwnd, C.byref(r))
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    x0, y0, x1, y1 = monitor_rect
    return _process_name(pid.value), (x0 <= cx < x1 and y0 <= cy < y1)


class ActivityMonitor:
    def __init__(self, cfg: dict, monitor_rect: tuple[int, int, int, int]):
        self.monitor_rect = monitor_rect
        self.configure(cfg)
        self.mode = "desktop"
        self.detail = ""
        self.fullscreen = False     # current mode is a full-screen game / full-screen video
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="activity", daemon=True)
        self._thread.start()

    def configure(self, cfg: dict) -> None:
        low = lambda xs: {x.lower() for x in xs}
        self.players = low(cfg.get("video_players", []))
        self.browsers = low(cfg.get("browsers", []))
        self.not_games = low(cfg.get("not_games", []))
        self.audio_apps = [a.lower() for a in cfg.get("audio_apps", [])]

    # -- media sessions (WinRT, async API driven from this thread's own event loop) -----------
    def _media_manager(self, loop):
        try:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as Manager
            return loop.run_until_complete(Manager.request_async())
        except Exception as e:                       # package missing / API unavailable
            print(f"media sessions unavailable ({e}); video detection uses full-screen only")
            return None

    def _playing(self, mgr) -> list[str]:
        if mgr is None:
            return []
        out = []
        try:
            for s in mgr.get_sessions():
                app = s.source_app_user_model_id or ""
                if s.get_playback_info().playback_status == PLAYBACK_PLAYING and \
                        not any(a in app.lower() for a in self.audio_apps):
                    out.append(app)
        except Exception:
            pass
        return out

    def classify(self, mgr) -> tuple[str, str, bool]:
        """Returns (mode, detail, fullscreen). `fullscreen` marks the trustworthy cases: a
        full-screen game or full-screen video, as opposed to media playing in a window/tab."""
        state = _notification_state()
        proc, on_ours = _foreground(self.monitor_rect)
        p = proc.lower()
        fs = on_ours and state in (QUNS_BUSY, QUNS_D3D_FULLSCREEN, QUNS_APP)
        if on_ours and state == QUNS_D3D_FULLSCREEN and p not in self.not_games:
            return "game", proc, True
        if fs:
            if state == QUNS_APP or p in self.players:
                return "video", proc, True
            if p not in self.browsers and p not in self.not_games:
                return "game", proc, True
        playing = self._playing(mgr)
        if playing:
            # Full screen only if the full-screen foreground app is a browser (it's playing).
            return "video", playing[0], fs and p in self.browsers
        return "desktop", proc, False

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        mgr = self._media_manager(loop)
        while not self._stop.is_set():
            try:
                mode, detail, fs = self.classify(mgr)
            except Exception as e:                    # never let detection kill the lights
                mode, detail, fs = self.mode, f"error: {e}", self.fullscreen
            # Reported as-is; the main loop debounces on/off decisions (off_delay_s).
            self.mode, self.fullscreen, self.detail = mode, fs, detail
            self._stop.wait(1.0)

    @property
    def autostart(self) -> bool:
        """Trustworthy 'content is on screen': a full-screen game or full-screen video."""
        return self.fullscreen and self.mode in ("game", "video")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(3)
