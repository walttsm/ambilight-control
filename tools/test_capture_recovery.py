"""Watch capture health while the display changes (e.g. press Win+Alt+B to toggle HDR).

Prints one line per second: frames received, rebuilds, and the captured size. A healthy capture
keeps delivering frames after each toggle (a 'capture rebuilt' line appears in between).
Usage: python tools/test_capture_recovery.py [seconds]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight.capture_gpu import GpuCapture  # noqa: E402

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
cap = GpuCapture("primary", 4)
print(f"capturing {cap.desc.DeviceName} {cap.width}x{cap.height} {cap.mode_text}", flush=True)
end, tick, frames = time.perf_counter() + seconds, time.perf_counter() + 1, 0
while time.perf_counter() < end:
    if cap.next_frame(50) is not None:
        frames += 1
    if time.perf_counter() >= tick:
        mean = cap._buf.reshape(-1, 4)[:, :3].mean() if frames else 0
        print(f"{time.strftime('%H:%M:%S')} frames/s {frames:3d}  rebuilds {cap.rebuilds}  "
              f"{cap.width}x{cap.height} {'HDR' if cap.hdr else 'SDR'}  "
              f"mean brightness {mean:5.1f}", flush=True)
        frames, tick = 0, tick + 1
cap.close()
