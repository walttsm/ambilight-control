"""Run the bar detector on the current screen (works on a paused video: the first capture after
start-up always returns the current image)."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.bars import BarDetector  # noqa: E402
from ambilight.capture_gpu import GpuCapture  # noqa: E402

cap = GpuCapture(r"\\.\DISPLAY1", 3)
f, t = None, time.time()
while f is None and time.time() - t < 5:
    f = cap.next_frame(200)
cap.close()
det = BarDetector(settle_s=0)
l, r, tp, b = det.measure(f)
print(f"frame {f.shape[1]}x{f.shape[0]}: bars L={l} R={r} T={tp} B={b}")
det.update(f)
print("crop after settle:", det.update(f))
