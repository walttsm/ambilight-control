"""Linear-light color processing: saturation, white balance, smoothing, power limit, encode.

Everything here works on float32 arrays shaped (led_count, 3) holding LINEAR light in 0..1.
Only `encode()` converts to the 0..255 bytes the strip receives.
"""
from __future__ import annotations

import math

import numpy as np

from .power import PowerModel

# Rec.709 / sRGB luminance weights (valid for linear light)
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


class ColorPipeline:
    def __init__(self, cfg: dict, smoothing: dict, power: dict, led_count: int):
        self.state: np.ndarray | None = None   # smoothed linear colors
        self.led_count = led_count
        self.configure(cfg, smoothing, power)

    def configure(self, cfg: dict, smoothing: dict, power: dict) -> None:
        """(Re)load settings; keeps the smoothing state so a live reload doesn't flash."""
        self.saturation = float(cfg["saturation"])
        self.gain = np.asarray(cfg["white_balance"], np.float32) * float(cfg["brightness"])
        self.inv_gamma = 1.0 / float(cfg["led_gamma"])
        self.power = PowerModel(power, self.led_count)
        self.min_brightness = int(cfg["min_brightness"])
        self.tau = float(smoothing["time_constant_ms"]) / 1000.0

    def adjust(self, lin: np.ndarray) -> np.ndarray:
        if self.saturation != 1.0:
            y = lin @ LUMA                                   # luminance per LED
            lin = y[:, None] + (lin - y[:, None]) * self.saturation
        lin = lin * self.gain
        return np.clip(lin, 0.0, 1.0)

    def smooth(self, target: np.ndarray, dt: float) -> np.ndarray:
        """Exponential moving average whose strength depends on real time, not frame count:
        alpha = 1 - exp(-dt / tau). Same visual lag at 30 fps or 144 fps."""
        if self.state is None or self.tau <= 0:
            self.state = target.copy()
        else:
            alpha = 1.0 - math.exp(-dt / self.tau)
            self.state += (target - self.state) * alpha
        return self.state

    def settled(self, target: np.ndarray) -> bool:
        """True when smoothing has (visibly) reached the target: no need to animate further."""
        return self.state is not None and float(np.abs(self.state - target).max()) < 0.5 / 255

    def encode(self, lin: np.ndarray) -> np.ndarray:
        """Linear light -> bytes for the strip, with a USB current limit.

        The limit is applied AFTER gamma, because LED current is proportional to the byte
        value actually sent (PWM duty cycle), not to the light we intended.
        """
        out = lin ** self.inv_gamma if self.inv_gamma != 1.0 else lin
        out = self.power.limit(out)
        b = np.floor(out * 255.0).astype(np.uint8)   # floor: rounding up could exceed the budget
        b[b <= self.min_brightness] = 0
        return b
