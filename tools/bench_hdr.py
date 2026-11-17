"""CPU cost of the capture in SDR-converted mode vs HDR FP16 mode, at 30 fps."""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight import hdr  # noqa: E402
from ambilight.capture_gpu import GpuCapture  # noqa: E402

for mode in ("off", "auto"):
    cap = GpuCapture("primary", 4, hdr=mode)
    w0, c0, n = time.perf_counter(), time.process_time(), 0
    while time.perf_counter() - w0 < 8:
        t = time.perf_counter()
        if cap.next_frame(30) is not None:
            n += 1
        time.sleep(max(0, 1 / 30 - (time.perf_counter() - t)))
    el = time.perf_counter() - w0
    print(f"{cap.mode_text:<34} {n / el:5.1f} frames/s  process CPU "
          f"{(time.process_time() - c0) / el * 100:5.1f}% of one core")
    cap.close()

f = np.random.rand(135, 240, 4).astype(np.float16) * 4
out = np.empty((135, 240, 4), np.uint8)
t = time.perf_counter()
for _ in range(300):
    hdr.scrgb_to_bgra8(f, 320, out)
print(f"scrgb_to_bgra8: {(time.perf_counter() - t) / 300 * 1000:.2f} ms per frame")
t = time.perf_counter()
for _ in range(30):
    hdr.sdr_white_nits(r"\\.\DISPLAY1")
print(f"sdr_white_nits: {(time.perf_counter() - t) / 30 * 1000:.2f} ms per call (once per second)")
