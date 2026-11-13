"""Turn a captured frame into one linear-light color per LED.

Design:
  1. For each configured edge, take a strided *view* of the band of screen along that edge
     (only every `subsample`-th pixel; no copy of the full frame is ever made).
  2. Decode those sRGB pixels to linear light through a 256-entry lookup table.
  3. Collapse the band's depth (mean across it) -> a 1-D color profile along the edge.
  4. Split the profile into N equal slices with np.add.reduceat -> mean color per LED.
  5. Orient each edge to match the strip direction and concatenate.
"""
from __future__ import annotations

import numpy as np


def srgb_to_linear_lut() -> np.ndarray:
    """Exact sRGB transfer function (IEC 61966-2-1), as a float32 table indexed by 0..255."""
    v = np.arange(256, dtype=np.float64) / 255.0
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    return lin.astype(np.float32)


SRGB_LUT = srgb_to_linear_lut()

# side name -> (which screen edge, reverse the natural direction?)
# natural directions: top/bottom run left->right, left/right run top->bottom
SIDES = {
    "top_ltr": ("top", False), "top_rtl": ("top", True),
    "bottom_ltr": ("bottom", False), "bottom_rtl": ("bottom", True),
    "left_down": ("left", False), "left_up": ("left", True),
    "right_down": ("right", False), "right_up": ("right", True),
}


class ZoneSampler:
    def __init__(self, width: int, height: int, edges: list[dict], depth: float, subsample: int):
        self.s = subsample
        self.edges = []
        for e in edges:
            edge, reverse = SIDES[e["side"]]
            n = e["leds"]
            if edge in ("top", "bottom"):
                d = max(1, int(height * depth))
                rows = slice(0, d) if edge == "top" else slice(height - d, height)
                cols = slice(0, width)
                length = len(range(0, width, subsample))
            else:
                d = max(1, int(width * depth))
                rows = slice(0, height)
                cols = slice(0, d) if edge == "left" else slice(width - d, width)
                length = len(range(0, height, subsample))
            # Slice boundaries along the (subsampled) edge: N nearly-equal parts.
            starts = np.linspace(0, length, n + 1).astype(np.int64)
            self.edges.append(dict(edge=edge, reverse=reverse, n=n, rows=rows, cols=cols,
                                   starts=starts[:-1], counts=np.diff(starts)))
        self.led_count = sum(e["n"] for e in self.edges)

    def sample(self, frame_bgra: np.ndarray) -> np.ndarray:
        """frame_bgra: (H, W, 4) uint8 view. Returns (led_count, 3) float32 linear RGB."""
        s = self.s
        out = []
        for e in self.edges:
            # Strided view of the band: every s-th row/col, channels B,G,R (drop alpha).
            band = frame_bgra[e["rows"], e["cols"]][::s, ::s, 2::-1]   # -> RGB order
            lin = SRGB_LUT[band]                                         # decode, float32
            # Collapse depth -> profile along the edge.
            axis = 0 if e["edge"] in ("top", "bottom") else 1
            profile = lin.mean(axis=axis)                                # (length, 3)
            leds = np.add.reduceat(profile, e["starts"], axis=0) / e["counts"][:, None]
            out.append(leds[::-1] if e["reverse"] else leds)
        return np.concatenate(out, axis=0)
