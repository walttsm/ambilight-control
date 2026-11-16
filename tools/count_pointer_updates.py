"""How many DXGI wake-ups are pointer-only (mouse moved, image unchanged) vs real frames?"""
import ctypes as C
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight import capture_gpu as cg  # noqa: E402

cap = cg.GpuCapture("primary", 3)
real = pointer = timeouts = 0
end = time.perf_counter() + 5
while time.perf_counter() < end:
    res = C.c_void_p()
    hr = cap._acquire(cap.dupl, 100, C.byref(cap._info), C.byref(res)) & 0xFFFFFFFF
    if hr == cg.DXGI_ERROR_WAIT_TIMEOUT:
        timeouts += 1
        continue
    if cap._info.LastPresentTime == 0:
        pointer += 1
    else:
        real += 1
    cg._release(res)
    cap._release_frame(cap.dupl)
cap.close()
print(f"per second: real frames {real / 5:.0f}, pointer-only {pointer / 5:.0f}, timeouts {timeouts / 5:.1f}")
