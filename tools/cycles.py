"""Lesson 13b: exact per-call CPU cost using QueryThreadCycleTime (CPU cycles this thread executed).

Unlike thread_time() (15.6 ms ticks on Windows) or sampling profilers (which may count waiting as
work), cycle counts only grow while the thread really runs on a CPU.
"""
import ctypes as C
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight.capture_gpu import GpuCapture  # noqa: E402
from ambilight.strip import Strip  # noqa: E402

k32 = C.windll.kernel32
k32.GetCurrentThread.restype = C.c_void_p


def cycles() -> int:
    v = C.c_uint64()
    k32.QueryThreadCycleTime(C.c_void_p(k32.GetCurrentThread()), C.byref(v))
    return v.value


# Calibrate: cycles per second of pure CPU work
c0, t0 = cycles(), time.perf_counter()
x = 0
while time.perf_counter() - t0 < 0.3:
    x += 1
hz = (cycles() - c0) / (time.perf_counter() - t0)
print(f"~{hz / 1e9:.2f} GHz effective")


def cost(label, fn, n):
    c, w = cycles(), time.perf_counter()
    for _ in range(n):
        fn()
    cpu_ms = (cycles() - c) / hz / n * 1000
    wall_ms = (time.perf_counter() - w) / n * 1000
    print(f"{label:<34} wall {wall_ms:7.3f} ms   CPU {cpu_ms:7.3f} ms  per call")


with Strip() as s:
    frame = np.full((s.led_count, 3), 10, np.uint8)
    cost("Strip.send (6 HID reports)", lambda: s.send(frame), 100)
cost("time.sleep(0.016)", lambda: time.sleep(0.016), 50)
cap = GpuCapture("primary", 4)
cost("next_frame(20 ms timeout)", lambda: cap.next_frame(20), 100)
cap.close()
