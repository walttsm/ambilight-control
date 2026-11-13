"""Lesson 9b: debug the bar detector with data instead of guesses.

Captures for N seconds, logs per-frame raw bar measurements and crop decisions, and saves a PNG
snapshot (the 430x180 GPU-downscaled frame) whenever the crop changes.
"""
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.bars import BarDetector  # noqa: E402
from ambilight.capture_gpu import GpuCapture  # noqa: E402

OUT = ROOT / "debug_bars"
OUT.mkdir(exist_ok=True)
for old in OUT.glob("*"):
    old.unlink()


def save_png(path: Path, bgra: np.ndarray) -> None:
    """Minimal PNG writer (RGB, 8-bit) so we don't need Pillow."""
    rgb = bgra[:, :, 2::-1]
    h, w = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 40
cap = GpuCapture(r"\\.\DISPLAY1", 3)
det = BarDetector()
log = open(OUT / "log.txt", "w")
t0 = time.perf_counter()
prev_crop, n = None, 0
while time.perf_counter() - t0 < seconds:
    f = cap.next_frame(100)
    if f is None:
        continue
    f = f.copy()
    bright = np.maximum(np.maximum(f[:, :, 0], f[:, :, 1]), f[:, :, 2]) > det.threshold
    l, r = det._bar_len(bright, 0)
    t, b = det._bar_len(bright, 1)
    crop = det.update(f)
    ts = time.perf_counter() - t0
    log.write(f"{ts:6.2f}s raw L={l:3d} R={r:3d} T={t:3d} B={b:3d}  crop={crop}\n")
    if crop != prev_crop:
        n += 1
        save_png(OUT / f"{n:03d}_{ts:06.2f}s_crop_{crop[0]}_{crop[1]}_{crop[2]}_{crop[3]}.png", f)
        prev_crop = crop
log.close()
cap.close()
print(f"{n} crop changes recorded in {OUT}")
