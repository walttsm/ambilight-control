"""Lesson 5: how expensive is DXGI Desktop Duplication capture?

Compares, per new frame:
  A. grab()       full frame, copied into a fresh numpy array
  B. grab_view()  full frame, numpy *view* of the mapped GPU staging memory (no extra copy)
  C. grab_view()  of only the top 12% band (smaller GPU->CPU transfer)

Also reports how often a NEW frame exists (DXGI only delivers frames when the screen changes).
"""
import time

import dxcam

cam = dxcam.create(output_idx=0, output_color="BGRA")
w, h = cam.width, cam.height
print(f"output 0: {w}x{h}, full frame = {w*h*4/1e6:.1f} MB")


def bench(label, fn, seconds=3):
    times, none = [], 0
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        t0 = time.perf_counter()
        f = fn()
        dt = time.perf_counter() - t0
        if f is None:
            none += 1
        else:
            f.sum(axis=(0, 1))          # touch the data so lazy views are really read
            times.append(dt)
        time.sleep(1 / 60)
    times.sort()
    med = times[len(times) // 2] * 1000 if times else float("nan")
    print(f"{label:<28} median {med:5.2f} ms   new={len(times)} unchanged={none}")


band = (0, 0, w, int(h * 0.12))
bench("A grab() full", lambda: cam.grab())
bench("B grab_view() full", lambda: cam.grab_view())
bench("C grab_view() top band", lambda: cam.grab_view(region=band))
cam.release()
