"""USB current model for the LED strip.

    I = controller + leds * idle + sum over all LEDs and channels of (value/255) * ma_per_channel

Defaults are deliberately conservative: WS2812B-class LEDs draw up to ~20 mA per color channel at
full brightness (real strips are often 12-16 mA), and this controller is a USB 2.0 device, so it
may only draw 500 mA. Measure with a USB power meter and adjust [power] in config.toml.
"""
from __future__ import annotations

import numpy as np

DEFAULTS = {
    "usb_budget_ma": 450,     # USB 2.0: 500 mA, minus a 10% margin
    "ma_per_channel": 20,
    "idle_ma_per_led": 1,
    "controller_ma": 30,
}


class PowerModel:
    def __init__(self, cfg: dict | None, led_count: int):
        c = {**DEFAULTS, **(cfg or {})}
        self.budget = float(c["usb_budget_ma"])
        self.per_channel = float(c["ma_per_channel"])
        self.fixed = float(c["controller_ma"]) + led_count * float(c["idle_ma_per_led"])
        self.available = max(0.0, self.budget - self.fixed)   # current left for actual light

    def estimate(self, values01: np.ndarray) -> float:
        """values01: (leds, 3) in 0..1 (byte value / 255). Returns estimated mA."""
        return self.fixed + float(values01.sum()) * self.per_channel

    def limit(self, values01: np.ndarray) -> np.ndarray:
        """Scale all LEDs equally (hues unchanged) so the estimate stays within the budget."""
        light = float(values01.sum()) * self.per_channel
        if light > self.available:
            return values01 * (self.available / light)
        return values01

    def limit_bytes(self, colors: np.ndarray) -> np.ndarray:
        """Same, for uint8 colors about to be sent. Floors, so it never rounds above budget."""
        light = float(colors.sum(dtype=np.int64)) / 255.0 * self.per_channel
        if light > self.available:
            return np.floor(colors * (self.available / light)).astype(np.uint8)
        return colors
