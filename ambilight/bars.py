"""Black-bar (pillarbox / letterbox) detection with hysteresis.

Problem: 16:9 video on a 21:9 screen has bars left and right, so the side LEDs sample them.
We find the real picture's edges and crop the frame to it before sampling.

What is a bar? Measured on real YouTube full-screen frames, bars are NOT pure black: players
paint soft dim gradients/glows into them (means 1-13 out of 255, single pixels > 30). What
reliably separates a bar from picture is that a bar line is DIM *and* FLAT:
    mean(max(R,G,B)) < bar_max      and      std along the line < flat_std
Picture lines are brighter or have texture (std 15-90 in the measurements).

Rules:
  * Bars are measured from each side; centered video => use the SMALLER of left/right
    (top/bottom), so a dark half of a scene isn't mistaken for a bar.
  * New/larger bars must stay stable for `settle_s` before we crop (ignores fades to black).
  * If content appears where we were cropping (bars shrank/vanished): un-crop immediately.
  * Changes within `tolerance` pixels are ignored (edge jitter).
  * Bars wider than `max_bar` of the frame => dark scene, keep the current crop.
"""
from __future__ import annotations

import time

import numpy as np


class BarDetector:
    def __init__(self, bar_max: float = 40, flat_std: float = 12, settle_s: float = 1.0,
                 max_bar: float = 0.4, tolerance: int = 2, interval_s: float = 0.1):
        self.bar_max = bar_max
        self.flat_std = flat_std
        self.settle_s = settle_s
        self.max_bar = max_bar
        self.tol = tolerance
        self.interval_s = interval_s           # measure at most this often (formats change slowly)
        self.crop = None                       # (x0, y0, x1, y1) currently applied
        self._candidate = None
        self._candidate_since = 0.0
        self._last_measure = -1e9

    def _bar_len(self, mx: np.ndarray, axis: int) -> tuple[int, int]:
        """Leading/trailing run lengths of bar lines. axis=0 -> columns, axis=1 -> rows."""
        is_bar = (mx.mean(axis=axis) < self.bar_max) & (mx.std(axis=axis) < self.flat_std)
        content = np.flatnonzero(~is_bar)
        n = is_bar.size
        if content.size == 0:
            return n, n
        return int(content[0]), int(n - 1 - content[-1])

    def measure(self, frame_bgra: np.ndarray) -> tuple[int, int, int, int]:
        f = frame_bgra
        # max(B, G, R) per pixel. (frame[..., :3].max(axis=2) is ~20x slower in numpy.)
        mx = np.maximum(np.maximum(f[:, :, 0], f[:, :, 1]), f[:, :, 2]).astype(np.float32)
        left, right = self._bar_len(mx, 0)
        top, bottom = self._bar_len(mx, 1)
        return left, right, top, bottom

    @property
    def pending(self) -> bool:
        """A crop change is waiting for its settle time. The caller must keep calling update()
        (with the last frame) even if the screen stops changing, e.g. a paused video."""
        return self._candidate is not None

    def _close(self, a, b) -> bool:
        return all(abs(x - y) <= self.tol for x, y in zip(a, b))

    def update(self, frame_bgra: np.ndarray) -> tuple[int, int, int, int]:
        h, w = frame_bgra.shape[:2]
        if self.crop is None or getattr(self, "_size", None) != (w, h):   # first frame / new size
            self.crop, self._size, self._candidate = (0, 0, w, h), (w, h), None
        now = time.monotonic()
        if now - self._last_measure < self.interval_s:
            return self.crop                   # ~1 ms of numpy per measurement: 10x/s is plenty
        self._last_measure = now
        left, right, top, bottom = self.measure(frame_bgra)
        bx, by = min(left, right), min(top, bottom)                       # symmetric bars
        if bx > w * self.max_bar or by > h * self.max_bar:
            return self.crop                                              # dark scene: hold
        cand = (bx, by, w - bx, h - by)
        if self._close(cand, self.crop):
            self._candidate = None
            return self.crop

        x0, y0, x1, y1 = self.crop
        t = self.tol
        content_outside = cand[0] < x0 - t or cand[1] < y0 - t or cand[2] > x1 + t or cand[3] > y1 + t
        if content_outside:                                               # bars shrank: act now
            self.crop, self._candidate = cand, None
        else:                                                             # bars grew: wait
            if self._candidate is None or not self._close(cand, self._candidate):
                self._candidate, self._candidate_since = cand, now
            elif now - self._candidate_since >= self.settle_s:
                self.crop, self._candidate = cand, None
        return self.crop
