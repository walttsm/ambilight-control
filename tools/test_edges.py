"""Lesson 4: verify config.toml's edge layout.

Each edge gets its own color; the first LED of each edge is white.
If an edge's color spills around a corner (or stops short), adjust its `leds` count.
"""
import sys
import time
import tomllib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.strip import Strip  # noqa: E402

cfg = tomllib.loads((ROOT / "config.toml").read_text())
palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 160, 0)]

with Strip() as s:
    c = np.zeros((s.led_count, 3), np.uint8)
    pos = 0
    for i, edge in enumerate(cfg["layout"]["edges"]):
        c[pos : pos + edge["leds"]] = palette[i]
        c[pos] = (255, 255, 255)
        print(f"{edge['side']:<10} LEDs {pos + 1:>2}-{pos + edge['leds']:<2}  {palette[i]}")
        pos += edge["leds"]
    print(f"configured {pos} LEDs, device reports {s.led_count}")
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    end = time.time() + seconds
    while time.time() < end:
        s.send(c)
        time.sleep(0.1)
