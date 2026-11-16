"""Lesson 8: GPU-downscaled capture: what does it cost now?"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight.capture_gpu import GpuCapture  # noqa: E402

level = int(sys.argv[1]) if len(sys.argv) > 1 else 3
cap = GpuCapture("primary", mip_level=level)
print(f"monitor {cap.width}x{cap.height} rotation={cap.rotation} -> mip {level}: "
      f"{cap.out_w}x{cap.out_h} = {cap.out_w * cap.out_h * 4 / 1024:.0f} KB per frame")

w0, c0, new, none, last = time.perf_counter(), time.process_time(), 0, 0, None
while time.perf_counter() - w0 < 10:
    f = cap.next_frame(100)
    if f is None:
        none += 1
    else:
        new += 1
        last = f.copy()
    time.sleep(1 / 60)        # cap at ~60 Hz like the real loop
el = time.perf_counter() - w0
print(f"new frames {new / el:.1f}/s, timeouts/mouse-only {none}, "
      f"process CPU {(time.process_time() - c0) / el * 100:.1f}% of one core")
if last is not None:
    print("mean BGRA of last frame:", last.reshape(-1, 4).mean(0).round(1))
cap.close()
