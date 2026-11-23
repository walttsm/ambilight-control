"""Ambilight main loop.

    capture (DXGI + GPU mips) -> bars -> zones (linear average per LED)
    -> color (adjust, smooth, encode, USB current limit) -> strip (HID, own thread)

On/off: only the global hotkey (default Ctrl+Alt+L); the last state is restored at startup.
Activity detection (ambilight/activity.py: game / video / desktop) only selects the settings
profile: [profiles.game] / [profiles.video] in config.toml override the base settings.

Run:  .venv\\Scripts\\python.exe -m ambilight.main [--stats]
      .venv\\Scripts\\pythonw.exe -m ambilight.main --log ambilight.log   (no window; autostart)
Stop: Ctrl+C (console), or end "pythonw.exe" in Task Manager.

Robustness: a single-instance mutex prevents two copies fighting over the strip, and a supervisor
loop restarts the pipeline 5 s after any error (strip unplugged, sleep/resume, GPU reset, strip
not ready yet at logon).
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import json
import sys
import threading
import time
import tomllib
import traceback
from pathlib import Path

import numpy as np

from .activity import ActivityMonitor
from .bars import BarDetector
from .capture_gpu import GpuCapture
from .color import ColorPipeline
from .hotkey import Hotkey
from .strip import AsyncSender, Strip
from .zones import ZoneSampler

ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict:
    # utf-8-sig: accept UTF-8 with or without a BOM (Windows PowerShell 5 / Notepad may add one)
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


STATE_FILE = ROOT / "state.json"


def load_lights_state() -> bool:
    """Last on/off state chosen with the hotkey (survives reboots). Default: on."""
    try:
        return bool(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("lights_on", True))
    except (OSError, ValueError):
        return True


def save_lights_state(on: bool) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"lights_on": on}), encoding="utf-8")
    except OSError as e:
        print(f"could not save on/off state: {e}")


def merged(base: dict, override: dict) -> dict:
    """Deep-merge a profile's overrides into the base config (tables merge, values replace)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--stats", action="store_true", help="print timing every 5 s")
    ap.add_argument("--log", help="write output to this file (relative to the project folder)")
    args = ap.parse_args()

    # Single instance: a named mutex exists once per user session. If it already exists,
    # another copy is driving the strip and we leave (before touching its log file).
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = k32.CreateMutexW(None, False, "Local\\AmbilightPython")   # keep the handle alive
    if ctypes.get_last_error() == 183:                                 # ERROR_ALREADY_EXISTS
        print("another instance is already running; exiting")
        return

    if args.log:   # pythonw has no console: send prints/tracebacks to a file (fresh each start)
        f = open(ROOT / args.log, "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "ambilight starting")

    persist: dict = {}            # survives restarts (e.g. the on/off state)
    while True:                   # supervisor: restart the pipeline after any failure
        try:
            run(args, persist)
            return                # Ctrl+C
        except KeyboardInterrupt:
            return
        except Exception:
            print(time.strftime("%H:%M:%S"), "error, restarting in 5 s:")
            traceback.print_exc()
            time.sleep(5)


def run(args, persist: dict) -> None:
    """One pipeline lifetime. Everything created registers its cleanup, so a failure at any
    point (e.g. strip not plugged in yet) releases what exists: hotkey, threads, GPU objects."""
    cleanup: list = []
    try:
        _run(args, persist, cleanup)
    finally:
        for fn in reversed(cleanup):
            try:
                fn()
            except Exception:
                pass


def _run(args, persist: dict, cleanup: list) -> None:
    cfg_path = Path(args.config)
    base_cfg = load_config(cfg_path)
    cfg_mtime = cfg_path.stat().st_mtime

    cc = base_cfg["capture"]                              # [capture]/[layout]: restart to change
    cap = GpuCapture(cc["monitor"], cc["mip_level"], hdr=cc.get("hdr", "auto"),
                     hdr_white_nits=cc.get("hdr_white_nits", 0))
    cleanup.append(cap.close)
    r = cap.desc.DesktopCoordinates
    edges = base_cfg["layout"]["edges"]
    samplers: dict[tuple[int, int], ZoneSampler] = {}
    bars = BarDetector()
    pipeline = ColorPipeline(base_cfg["color"], base_cfg["smoothing"], base_cfg["power"],
                             sum(e["leds"] for e in edges))
    act_cfg = base_cfg.get("activity", {})
    activity = ActivityMonitor(act_cfg, (r.left, r.top, r.right, r.bottom)) \
        if act_cfg.get("enabled", True) else None
    if activity:
        cleanup.append(activity.close)

    # Effective settings = base config + the active profile. Rebuilt when either changes.
    eff: dict = {}
    profile_name = None
    frame_period = keepalive = 0.0

    def apply(profile: str | None) -> None:
        nonlocal eff, profile_name, frame_period, keepalive
        eff = merged(base_cfg, base_cfg.get("profiles", {}).get(profile, {})) if profile else base_cfg
        profile_name = profile
        pipeline.configure(eff["color"], eff["smoothing"], eff["power"])
        b = eff.get("bars", {})
        bars.bar_max, bars.flat_std = b.get("bar_max", 40), b.get("flat_std", 12)
        bars.settle_s = b.get("settle_s", 1.0)
        samplers.clear()                                  # zone depth may differ per profile
        frame_period = 1.0 / eff["capture"]["target_fps"]
        keepalive = 1.0 / eff["smoothing"]["keepalive_hz"]
        # HDR settings apply live (also per profile): white level is read every second by the
        # capture; switching hdr auto/off needs a capture rebuild.
        cap.hdr_white_nits = eff["capture"].get("hdr_white_nits", 0)
        new_mode = eff["capture"].get("hdr", "auto")
        if new_mode != cap.hdr_mode:
            cap.hdr_mode = new_mode
            cap._rebuild(f"hdr = {new_mode}")

    def sampler_for(w: int, h: int) -> ZoneSampler:
        """Zone geometry depends on the (cropped) frame size; build once per size and cache."""
        if (w, h) not in samplers:
            samplers[(w, h)] = ZoneSampler(w, h, edges, eff["zones"]["depth"], subsample=1)
        return samplers[(w, h)]

    apply(None)

    # On/off: ONLY the hotkey (user's choice: automatic on/off was annoying). The state is saved
    # to state.json, so a reboot restores what you chose last (first run: on). Activity detection
    # only selects the settings profile; it never switches the LEDs.
    lock = threading.Lock()
    state = {"lights_on": persist.get("lights_on", load_lights_state())}

    def on_hotkey() -> None:
        with lock:
            state["lights_on"] = not state["lights_on"]
            save_lights_state(state["lights_on"])
        print(f"hotkey: LEDs {'on' if state['lights_on'] else 'off'}")

    hotkey = Hotkey(act_cfg.get("hotkey", "ctrl+alt+l"), on_hotkey)
    cleanup.append(hotkey.close)
    print(f"hotkey {hotkey.spec}: {'registered' if hotkey.ok else 'FAILED (in use by another app?)'}")

    with Strip(base_cfg["power"]) as strip:
        n = pipeline.led_count
        if n != strip.led_count:
            print(f"warning: config has {n} LEDs, device reports {strip.led_count}")
        print(f"capturing {cap.width}x{cap.height} {cap.mode_text} (GPU-downscaled to "
              f"{cap.out_w}x{cap.out_h}) -> {n} LEDs, fw {strip.info['firmware']}. Ctrl+C to stop.")

        sender = AsyncSender(strip)
        black = np.zeros((n, 3), np.float32)
        target = black.copy()
        last_sent: np.ndarray | None = None
        last_frame: np.ndarray | None = None   # capture reuses its buffer; valid until next frame
        t_prev = t_sent = time.perf_counter()
        stat = dict(loops=0, new=0, sent=0, t0=t_prev, c0=time.process_time(), ma_max=0.0)
        t_cfg_check = t_prev
        last_mode = None
        was_lit = False
        seen_rebuilds = cap.rebuilds
        try:
            while True:
                t_loop = time.perf_counter()

                # --- once per second: config file changed? -------------------------------------
                if t_loop - t_cfg_check >= 1.0:
                    t_cfg_check = t_loop
                    try:
                        if cfg_path.stat().st_mtime != cfg_mtime:
                            base_cfg, cfg_mtime = load_config(cfg_path), cfg_path.stat().st_mtime
                            if activity:
                                activity.configure(base_cfg.get("activity", {}))
                            apply(profile_name)
                            print("config reloaded")
                    except (tomllib.TOMLDecodeError, OSError) as e:   # half-saved file etc.
                        print(f"config not reloaded: {e}")

                # --- what should the LEDs do? -------------------------------------------------
                mode = activity.mode if activity else "video"
                if mode != last_mode:
                    print(f"activity: {mode} ({activity.detail if activity else 'detection off'})")
                    last_mode = mode
                    apply(mode if mode in ("game", "video") else None)
                with lock:
                    lights = state["lights_on"]
                persist["lights_on"] = lights
                if sender.error:                         # USB failed: let the supervisor restart
                    raise RuntimeError(f"LED strip write failed: {sender.error}")

                # --- capture (only while lights are on) ---------------------------------------
                frame = None
                if lights:
                    settled = pipeline.settled(target) and not bars.pending
                    wait = keepalive - (t_loop - t_sent) if settled else frame_period
                    frame = cap.next_frame(max(1, int(wait * 1000)))
                    if cap.rebuilds != seen_rebuilds:    # display changed: monitor may differ
                        seen_rebuilds = cap.rebuilds
                        last_frame = None                # old buffer is gone / other size
                        rc = cap.desc.DesktopCoordinates
                        if activity:
                            activity.monitor_rect = (rc.left, rc.top, rc.right, rc.bottom)
                    if frame is not None:
                        last_frame = frame
                    elif bars.pending or not was_lit:
                        # Static screen: let the bar settle timer finish, or (just turned on)
                        # reuse the last image: DXGI only reports changes, and there were none.
                        frame = last_frame
                    if frame is not None and cap.protected:
                        frame = None             # DRM video blacked out: hold the last colors
                    if frame is not None:
                        if eff.get("bars", {}).get("enabled", True):
                            x0, y0, x1, y1 = bars.update(frame)
                            frame = frame[y0:y1, x0:x1]
                        target = pipeline.adjust(sampler_for(frame.shape[1], frame.shape[0]).sample(frame))
                        stat["new"] += 1
                else:
                    target = black               # fade out through the normal smoothing
                    if pipeline.settled(target):
                        # Fully dark: no capture at all, just wake for keepalive / hotkey.
                        time.sleep(min(0.2, max(0.0, keepalive - (t_loop - t_sent))))

                was_lit = lights

                # --- smooth, encode, send ------------------------------------------------------
                now = time.perf_counter()
                out = pipeline.encode(pipeline.smooth(target, now - t_prev))
                t_prev = now
                # Dead-band: invisible 1-step changes (common while smoothing) don't cost a USB
                # send. While "off" we keep sending black: if frames stop, the firmware falls
                # back to its stored (lit) effect.
                changed = last_sent is None or int(
                    np.abs(out.astype(np.int16) - last_sent).max()) > eff["smoothing"].get("send_threshold", 1)
                if changed or now - t_sent >= keepalive:
                    sender.submit(out)
                    last_sent, t_sent = out, now
                    stat["sent"] += 1
                    if args.stats:
                        stat["ma_max"] = max(stat["ma_max"], pipeline.power.estimate(out / 255.0))

                stat["loops"] += 1
                if args.stats and now - stat["t0"] >= 5:
                    el = now - stat["t0"]
                    cpu = (time.process_time() - stat["c0"]) / el * 100
                    print(f"[{mode}/{'on' if lights else 'off'}] "
                          f"loops {stat['loops']/el:5.1f}/s  new {stat['new']/el:5.1f}/s  "
                          f"sent {stat['sent']/el:5.1f}/s  CPU {cpu:4.1f}%  "
                          f"peak {stat['ma_max']:.0f} mA  crop {bars.crop}")
                    stat = dict(loops=0, new=0, sent=0, t0=now, c0=time.process_time(), ma_max=0.0)

                # Never loop faster than target_fps, whatever woke us up.
                spare = frame_period - (time.perf_counter() - t_loop)
                if spare > 0:
                    time.sleep(spare)
        except KeyboardInterrupt:
            pass
        finally:
            sender.close()
            try:
                strip.fill(0, 0, 0)       # may fail if the device is gone; cleanup continues
            except Exception:
                pass


if __name__ == "__main__":
    main()
