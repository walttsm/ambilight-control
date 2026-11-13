"""Per-column statistics of the captured frame: mean brightness and vertical spread (std)."""
import sys, time
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.capture_gpu import GpuCapture  # noqa: E402

cap = GpuCapture(r"\\.\DISPLAY1", 3)
f, t = None, time.time()
while f is None and time.time() - t < 5:
    f = cap.next_frame(200)
cap.close()
from ambilight.debugimg import save_png  # noqa: E402
(ROOT / "debug_bars").mkdir(exist_ok=True)
save_png(ROOT / "debug_bars" / "stats_frame.png", f)
f = f.astype(np.float32)
mx = np.maximum(np.maximum(f[:, :, 0], f[:, :, 1]), f[:, :, 2])   # (h, w)
mean, std = mx.mean(0), f[:, :, :3].std(axis=0).max(axis=1)      # per column
for name, cols in (("left", range(0, 70, 4)), ("right", range(360, 430, 4))):
    print(name)
    for c in cols:
        print(f"  col {c:3d}: mean {mean[c]:6.1f}  vertical std {std[c]:6.1f}")
