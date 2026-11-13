"""Lesson 3: prove we control the strip, and discover its physical layout.

Step 1: solid red / green / blue  -> confirms the channel order (wire map).
Step 2: "marker" pattern          -> tells us where LED #1 is and which way the strip runs.
        LED 1 = white, LEDs 2-5 = red, then every 10th LED = green, rest = dim blue.
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight.strip import Strip  # noqa: E402

with Strip() as s:
    print("device:", s.info)
    for name, rgb in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]:
        print("solid", name)
        s.fill(*rgb)
        time.sleep(2)

    c = np.zeros((s.led_count, 3), np.uint8)
    c[:] = (0, 0, 40)            # dim blue background
    c[9::10] = (0, 255, 0)       # LED 10, 20, 30 ... green
    c[1:5] = (255, 0, 0)         # LEDs 2-5 red  (shows direction)
    c[0] = (255, 255, 255)       # LED 1 white   (shows the start)
    # The firmware drops out of sync mode if frames stop arriving, so keep streaming.
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(f"streaming marker pattern for {seconds:.0f}s ...")
    end = time.time() + seconds
    while time.time() < end:
        s.send(c)
        time.sleep(0.1)
