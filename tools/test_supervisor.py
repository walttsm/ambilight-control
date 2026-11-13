"""Simulate a failing strip (twice 'not found', then OK) and check the supervisor recovers:
each attempt must clean up (hotkey re-registers OK) and the third attempt must run."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ambilight.main as m  # noqa: E402

real_strip, calls = m.Strip, {"n": 0}


def flaky_strip(*a, **k):
    calls["n"] += 1
    if calls["n"] <= 2:
        raise RuntimeError(f"simulated: LED strip not found (attempt {calls['n']})")
    return real_strip(*a, **k)


m.Strip = flaky_strip
real_sleep = time.sleep
# Note: m.time IS the global time module, so this patch affects everyone; keep real_sleep for us.
m.time.sleep = lambda s: real_sleep(min(s, 0.5))     # speed up the supervisor's 5 s waits
sys.argv = ["ambilight", "--stats"]
threading.Thread(target=m.main, daemon=True).start()
real_sleep(15)
print(f"--- strip open attempts: {calls['n']} (expect 3)")
