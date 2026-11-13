"""Lesson 7: profile each stage of the loop separately: wall time AND CPU time.

Wall time = how long the call took. CPU time = how long this thread actually computed.
The difference is waiting (GPU, USB, sleeping). Waiting blocks the loop but costs no CPU.
Windows counts thread CPU time in ~15.6 ms ticks, so we sum over many frames.
"""
import sys
import time
import tomllib
from collections import defaultdict
from pathlib import Path

import dxcam

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ambilight.color import ColorPipeline  # noqa: E402
from ambilight.zones import ZoneSampler  # noqa: E402

cfg = tomllib.loads((ROOT / "config.toml").read_text())
cam = dxcam.create(output_idx=0, output_color="BGRA")
z = ZoneSampler(cam.width, cam.height, cfg["layout"]["edges"], cfg["zones"]["depth"],
                cfg["capture"]["subsample"])
pipe = ColorPipeline(cfg["color"], cfg["smoothing"])
wall, cpu, n = defaultdict(float), defaultdict(float), defaultdict(int)


def timed(name, fn, *a, **k):
    w, c = time.perf_counter(), time.thread_time()
    r = fn(*a, **k)
    wall[name] += time.perf_counter() - w
    cpu[name] += time.thread_time() - c
    n[name] += 1
    return r


seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10
w0, c0 = time.perf_counter(), time.process_time()
end = w0 + seconds
while time.perf_counter() < end:
    v = timed("grab", cam.grab_view)
    if v is not None:
        n["new frames"] += 1
        lin = timed("sample", z.sample, v)
        timed("color", lambda: pipe.encode(pipe.smooth(pipe.adjust(lin), 1 / 60)))
    time.sleep(1 / 60)
cam.release()
tw, tc = time.perf_counter() - w0, time.process_time() - c0

print(f"{n['new frames']} new frames in {tw:.1f}s; process CPU {tc / tw * 100:.1f}% of one core")
for k in ("grab", "sample", "color"):
    print(f"{k:<7} calls={n[k]:4d}  wall/call {wall[k] / n[k] * 1000:6.3f} ms   "
          f"CPU share {cpu[k] / tw * 100:5.1f}% of one core")
