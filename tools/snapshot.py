"""Save what the capture pipeline sees (GPU-downscaled frame) and print per-column brightness."""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.capture_gpu import GpuCapture  # noqa: E402
from ambilight.debugimg import save_png  # noqa: E402

cap = GpuCapture("primary", 3)
f, t = None, time.time()
while f is None and time.time() - t < 5:
    f = cap.next_frame(200)
cap.close()
if f is None:
    raise SystemExit("screen didn't change for 5 s - nothing captured")
f = f.copy()
(ROOT / "debug_bars").mkdir(exist_ok=True)
save_png(ROOT / "debug_bars" / "now.png", f)
mx = np.maximum(np.maximum(f[:, :, 0], f[:, :, 1]), f[:, :, 2])
for name, cols in (("left 64", mx[:, :64]), ("right 64", mx[:, -64:])):
    print(f"{name}: share of pixels > 30 per column:")
    print("  ", (cols > 30).mean(0).round(2).tolist())
