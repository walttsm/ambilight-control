"""Global keyboard shortcut via Win32 RegisterHotKey.

Windows delivers hotkeys as WM_HOTKEY messages to the thread that registered them, so this runs a
tiny message loop (GetMessageW) on its own thread: the same mechanism tray apps use.
"""
from __future__ import annotations

import ctypes as C
import threading
from ctypes import wintypes as W
from typing import Callable

u32, k32 = C.windll.user32, C.windll.kernel32
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


def parse(spec: str) -> tuple[int, int]:
    """'ctrl+alt+l' -> (modifiers, virtual-key code). Supports letters, digits, F1-F24."""
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    m = MOD_NOREPEAT
    for x in mods:
        m |= MODS[x]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        vk = 0x6F + int(key[1:])                       # VK_F1 = 0x70
    else:
        raise ValueError(f"unsupported key {key!r}")
    return m, vk


class Hotkey:
    def __init__(self, spec: str, callback: Callable[[], None]):
        self.spec, self.callback = spec, callback
        self.ok = False
        self._tid = 0
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="hotkey", daemon=True)
        self._thread.start()
        ready.wait(2)

    def _run(self, ready: threading.Event) -> None:
        self._tid = k32.GetCurrentThreadId()
        mods, vk = parse(self.spec)
        self.ok = bool(u32.RegisterHotKey(None, 1, mods, vk))
        ready.set()
        if not self.ok:
            return                                      # another app already owns this shortcut
        msg = W.MSG()
        while u32.GetMessageW(C.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.callback()
                except Exception as e:
                    print("hotkey callback failed:", e)
        u32.UnregisterHotKey(None, 1)

    def close(self) -> None:
        if self._tid:
            u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)    # wait for UnregisterHotKey, so a restart can register again
