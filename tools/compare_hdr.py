"""Capture the same screen with the SDR-converted path (hdr="off") and the FP16 path (hdr="auto")
and compare brightness. Run it a third time with Windows HDR OFF for the true SDR reference.
Snapshots go to debug_bars/hdr_<mode>.png.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.capture_gpu import GpuCapture  # noqa: E402
from ambilight.debugimg import save_png  # noqa: E402

(ROOT / "debug_bars").mkdir(exist_ok=True)
for mode in ("off", "auto"):
    cap = GpuCapture("primary", 4, hdr=mode)
    f, t = None, time.time()
    while f is None and time.time() - t < 3:       # first frame after opening always comes
        f = cap.next_frame(200)
    label = cap.mode_text
    cap.close()
    if f is None:
        print(f"hdr={mode}: no frame")
        continue
    name = "hdr_fp16" if "HDR" in label else "sdr_converted" if mode == "off" else "sdr"
    save_png(ROOT / "debug_bars" / f"{name}.png", f)
    px = f.reshape(-1, 4)[:, :3]
    print(f"hdr={mode:<4} -> {label:<34} mean {px.mean():6.1f}   "
          f"brightest 1% {sorted(px.max(1))[int(len(px) * .99)]:4d}   saved {name}.png")
