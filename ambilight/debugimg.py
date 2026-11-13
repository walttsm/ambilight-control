"""Tiny PNG writer for debug snapshots (no Pillow dependency)."""
import struct
import zlib
from pathlib import Path

import numpy as np


def save_png(path: Path, bgra: np.ndarray) -> None:
    rgb = np.ascontiguousarray(bgra[:, :, 2::-1])
    h, w = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
