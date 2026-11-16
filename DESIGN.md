# Ambilight: design notes for the native port

A replacement for Robobloq **SyncLight 2.22.1** (Electron app) driving a USB LED strip behind a
3440x1440 monitor. The Python prototype in this folder works; this document records everything
learned so a Rust / C++ / Go version can be built without re-doing the research.

Reading order for the native-version session: §1 protocol → §3 pipeline → §5 pitfalls → §7 port plan.
`config.toml` is the shared configuration format and should be kept compatible.

---

## 1. Hardware & protocol (reverse-engineered from SyncLight, verified on the device)

| Item | Value |
|---|---|
| USB IDs | VID `0x1A86` (WCH chip), PID `0xFE07`, manufacturer string `ROBOBLOQ` |
| Interface to use | **interface 0**, usage page `0xFF00` (vendor). Interface 1 is a keyboard (ignore). |
| Transport | HID **output reports, 64 bytes payload**, prefixed with report ID `0x00` |
| Firmware tested | 1.9.4 (device id `000501`, 71 LEDs, 34") |
| Authentication | none for the HID path |

### 1.1 Framing
Packets longer than 64 bytes are split into 64-byte chunks; **each chunk** is written as
`[0x00] + chunk` (report ID 0). The last chunk may be short.

Checksum everywhere: `sum(all preceding bytes) & 0xFF`. Sequence byte: 1..254, incrementing
(device echoes it in replies).

### 1.2 Read device info (`"RB"` protocol)
Request (6 bytes): `'R' 'B' 0x06 seq 0x82 checksum`
Reply (read 64 bytes, timeout ~1 s):

| Offset | Meaning |
|---|---|
| 0-1 | `"RB"` |
| 2 | length (25) |
| 3 | seq echo |
| 4 | action echo (0x82) |
| 5-7 | device id (hex `000501`) |
| 8 | display size, inches |
| 11 | **LED count** (71) |
| 12-19 | uuid |
| 21-23 | **firmware version** major.minor.patch |

### 1.3 Set colors: "sync screen" (`"SC"` protocol, firmware > 1.0.2)
```
'S' 'C' | len_hi len_lo (uint16 BE, whole packet) | seq | 0x80 | segments... | checksum
segment = first_led, R, G, B, last_led          (1-based, inclusive ranges)
```
One segment per LED works (71 LEDs → 362 bytes → 6 HID reports). Adjacent LEDs with identical
color may be merged into one segment (fewer bytes; not implemented yet).
Firmware ≤ 1.0.2 used a different `"RB"` 0x86 `setSectionLED` packet (not needed here).

### 1.4 Firmware behaviour
* **Watchdog:** if frames stop arriving (>~2 s), the strip falls back to its stored effect.
  Keep sending at least every 200 ms (`keepalive_hz = 5`).
* **USB timing:** one 6-report frame takes **~12 ms wall time, ~0 CPU** (the host waits on the
  USB interrupt endpoint). Never send on the capture thread; see §3.6.
* Color order is RGB on this unit (SyncLight's `wireMap` supports permutations).

### 1.5 Physical layout (this monitor)
LED #1 is at the bottom-right; seen from the front, the strip runs **up the right edge (15),
right-to-left across the top (41), down the left edge (15)** = 71. No bottom edge. Verified with
`tools/test_edges.py`. Encoded in `config.toml` `[layout] edges`.

---

## 2. What SyncLight does (and why it was slow / inaccurate)

* Electron app (~430 MB RAM total), main process **~34% of one core** continuously.
* Native module `@warren-robobloq/quiklight` captures with **GDI** (`BitBlt`/`StretchBlt`/
  `GetDIBits`), downsampled 10x on the CPU, every 10 ms + `syncSpeed`.
* Frames are posted to the JS main thread and converted to `{red,green,blue}` objects per pixel
  (millions of allocations/s → GC churn).
* Each LED zone takes **one single pixel** from the cell centre (unless "average" is enabled).
* **No gamma handling**: gamma-encoded sRGB values sent to linear LEDs → lifted, washed-out darks.
* "Light compression": per LED, if R+G+B > 255, scale to 255. White → (85,85,85) while red
  stays 255: a hue-dependent brightness distortion. Allows ~1.5 A worst case (see §4: over USB 2.0 spec).
* Optional saturation boost ×3 (HSL), per-channel `colorCalibration` multipliers.

---

## 3. Our pipeline

```
DXGI Desktop Duplication ──► GPU copy into mip-chain texture ──► GenerateMips (sRGB view)
   (sleeps until change)        (GPU→GPU)                         (linear-light averaging on GPU)
        │
        ▼
copy mip level 4 (215x90, 77 KB) to STAGING ──► Map ──► numpy (BGRA)
        │
        ▼
black-bar detector (crop) ──► zone sampler (sRGB→linear LUT, mean per LED)
        │
        ▼
saturation, white balance (linear) ──► time-based EMA smoothing ──► LED gamma encode
        │
        ▼
USB current limit (mA model) ──► bytes ──► HID sender thread ("latest wins" mailbox)
```

### 3.1 Capture: `ambilight/capture_gpu.py`
Raw COM via vtable indices (documented in the file header; same indices apply to any language).
* Pick adapter+output by device name (`\\.\DISPLAY1`); create the D3D11 device **on that adapter**.
* `IDXGIOutput1::DuplicateOutput` → `AcquireNextFrame(timeout)`.
  * `DXGI_ERROR_WAIT_TIMEOUT` → no change. `DXGI_ERROR_ACCESS_LOST` → recreate duplication
    (fullscreen switches, resolution change, UAC/secure desktop).
  * `LastPresentTime == 0` → **pointer-only update**: release and keep waiting (a gaming mouse
    otherwise wakes the loop hundreds of times per second).
* Mip texture: format `B8G8R8A8_TYPELESS`, `MipLevels = level+1`, bind `SHADER_RESOURCE|RENDER_TARGET`,
  misc `GENERATE_MIPS`. SRV format **`B8G8R8A8_UNORM_SRGB`** so `GenerateMips` averages in linear light.
* Copy desktop texture → mip level 0, **ReleaseFrame immediately**, `GenerateMips`, copy level N →
  staging (`B8G8R8A8_UNORM_SRGB`, `USAGE_STAGING`, `CPU_ACCESS_READ`), Map, copy rows honouring
  `RowPitch`, Unmap.
* Rotation (`DXGI_OUTPUT_DESC.Rotation`) is **not handled** (primary is unrotated; the second
  monitor is rotated 270°).

### 3.2 Zones: `ambilight/zones.py`
For each edge: band of depth `zones.depth` (fraction of the perpendicular dimension), decode via
256-entry sRGB→linear LUT (exact IEC 61966-2-1 curve), mean across depth → 1-D profile, split into
N equal slices (`np.add.reduceat`), orient (`right_up`, `top_rtl`, …). Rebuilt per crop size (cached).

### 3.3 Black bars: `ambilight/bars.py`
Measured on real YouTube full-screen frames: bars are **not black**: players paint dim gradients
(means 1-13, single pixels >30). Rule that works: a column/row is a bar if
`mean(max(R,G,B)) < 40` **and** `std < 12` (dim AND flat). Then:
symmetric bars (`min(left,right)`), new bars must persist `settle_s = 1.0`, content appearing in
the cropped area → un-crop immediately, ±2 px tolerance, bars > 40% of frame → hold (dark scene).
**The settle timer must be ticked even with no new frames** (paused video): the main loop
re-evaluates the last frame while `bars.pending`.

### 3.4 Color: `ambilight/color.py`
All math in linear light, float 0..1.
* Saturation around Rec.709 luminance (`0.2126, 0.7152, 0.0722`), white-balance multipliers × brightness.
* EMA smoothing: `alpha = 1 - exp(-dt / tau)` (frame-rate independent). `settled()` lets the loop sleep.
* Encode: `out = lin ** (1/led_gamma)`. `led_gamma = 1.0` sends linear light (accurate darks, but
  oranges drift red on this strip); 1.6 tried as a compromise; 2.2 ≈ SyncLight behaviour.

### 3.5 Power: `ambilight/power.py` (see §4)
Applied after gamma (current ∝ byte value), and **again inside `Strip.send()` as a backstop** so no
caller (test tools included) can exceed the budget. Floor, never round up.

### 3.6 Activity, profiles, hotkey: `ambilight/activity.py`, `ambilight/hotkey.py`
**On/off (user's choice): start OFF; "content" = full-screen game or full-screen video, debounced
(starts immediately, ends only after `off_delay_s` = 120 s without it). Content starts → ON,
content ends → OFF, hotkey toggles any time.** Edge-triggered: switching off mid-game sticks,
switching on manually on the desktop sticks, a break < 120 s is neither an end nor a new start. Media in a window/background tab is
"video" for profile purposes but never triggers auto-on (unreliable: background music, previews).
Activity detection also selects the settings profile. Polled once per second on a background thread:
* `SHQueryUserNotificationState`: 3 = exclusive full-screen D3D, 2 = BUSY (borderless full
  screen), 7 = store app full screen. (Maximized normal windows report 5, so they don't count.)
* Foreground window → process name (`QueryFullProcessImageNameW`) and whether its center is on
  the captured monitor.
* WinRT `GlobalSystemMediaTransportControlsSessionManager`: sessions with playback status 4
  (Playing); music apps excluded by substring of `source_app_user_model_id`.
* Rules: game = D3D full screen, or BUSY by a process that isn't a browser/player/`not_games`;
  video = player/store app full screen, or any non-music session playing; else desktop.
* **"Off" means streaming black** at the keepalive rate (the firmware watchdog would otherwise
  restore its stored, lit effect). Capture is skipped entirely while off.
* On turning back on over a static screen, reuse the last captured image (DXGI only reports changes).
* `[profiles.game]` / `[profiles.video]` deep-merge over the base config (game: bars off,
  40 ms smoothing; video: bars on, 100 ms).
* Hotkey: `RegisterHotKey` + a `GetMessageW` loop on its own thread (WM_HOTKEY); toggles a
  simple `lights_on` flag.
* DRM: `DXGI_OUTDUPL_FRAME_INFO.ProtectedContentMaskedOut` → hold the last colors.

### 3.7 Running unattended (autostart)
* Startup-folder shortcut → `pythonw.exe -m ambilight.main --log ambilight.log` (no window).
* Single instance: named mutex `Local\AmbilightPython`; check it BEFORE opening (truncating) the log.
* Supervisor: any exception → log traceback, wait 5 s, rebuild the whole pipeline. Every created
  resource registers a cleanup (capture, activity thread, hotkey; hotkey `close()` joins its thread
  so the next attempt can `RegisterHotKey` again). On/off state persists across restarts.
* The HID sender thread stores write errors (`sender.error`) and the main loop raises them, so an
  unplugged strip or sleep/resume leads to a restart instead of a silently dead thread.
* Verified with `tools/test_supervisor.py` (strip "not found" twice, then OK → 3 attempts, hotkey
  re-registered each time). Off state: ~0.3% CPU, strip at idle draw.
* Native version: replace with a tray icon (on/off, open config, quit) and the same behaviour.

### 3.8 Loop & threading: `ambilight/main.py`, `ambilight/strip.py`
* Wait inside `AcquireNextFrame`: `frame_period` while animating, up to the keepalive deadline when settled.
* Never loop faster than `target_fps` (default 30; sleep the remainder unconditionally).
* Send only if some channel changed by more than `send_threshold` (1) or keepalive due.
* Bar detection measured at most every 100 ms. Sender thread with a one-slot "latest wins" mailbox.
* Live config reload (mtime check once per second); `[capture]`/`[layout]` need a restart.

---

## 4. USB power budget

The controller is a USB 2.0 full-speed device → **500 mA** per spec (USB 3's 900 mA is for
SuperSpeed devices). Model (all values in `[power]`):
```
I = controller_ma (30) + leds × idle_ma_per_led (1) + Σ(value/255) × ma_per_channel (20)
```
Budget 450 mA (10% margin) → 349 mA for light ≈ **8% of all-white**. Full-white screen → each LED
≈ 20/255; a single white LED can still be 255. SyncLight's rule allowed ≈ 1.5 A by this model.
**The 20 mA/channel figure is an assumption**: measure with a USB power meter (all LEDs at a known
value) and set `ma_per_channel` from data.

---

## 5. Measurements & pitfalls (things that cost time)

| Measurement (this PC: RTX 5060 Ti, 16 threads) | Result |
|---|---|
| SyncLight main process | ~34% of one core, ~430 MB (all processes) |
| dxcam full-frame grab (19.8 MB) | 5.4 ms; process CPU ~24.5% at 60 fps (driver threads!) |
| dxcam region (top band) / tiny | ~8% process CPU: polling overhead |
| **GPU mips + 302 KB readback** | **2.5% process CPU** |
| Python app, game at 136 fps, processing 59 fps, mip 3 + per-frame bar detection | 16-25% |
| **Python app, game at 136 fps, processing 30 fps, mip 4, bars at 10 Hz** | **~10% of one core**, 65 MB |
| Static desktop | ~0-1.5% |
| HID frame send (6 reports), exact via QueryThreadCycleTime | 12 ms wall, **0.50 ms CPU** |
| Capture call (acquire + copy + GenerateMips + Map), exact | 7.6 ms wall, **0.84 ms CPU** |

Per-frame CPU that a native rewrite will NOT remove (it's inside Windows/drivers): ~0.5 ms USB +
~0.8 ms capture. The lever for those is doing them less often (`target_fps`, send dead-band).
Python overhead (numpy calls, interpreter) is roughly the other half and does go away natively.

Pitfalls:
1. **Measure the right process**: a Windows venv `python.exe` is a launcher; the real interpreter is
   a child. Sum CPU over all matching processes (`tools/measure_cpu.ps1`).
2. **Our thread's CPU ≠ process CPU**: the GPU driver did ~17% of the work in its own threads during
   full-frame readback. Profile both.
3. **Forced vs real frames**: re-grabbing a cached frame (0.19 ms) hides the real cost (5-9 ms).
4. **numpy**: `x[..., :3].max(axis=2)` is ~20× slower than `np.maximum(np.maximum(b, g), r)`.
   (Irrelevant in native code: just loop.)
5. **Pointer-only DXGI updates** wake the loop at mouse-polling rate unless filtered.
6. **Event-driven loop + timers**: anything time-based (bar settle) needs a tick without new frames.
7. **Config encoding**: Windows PowerShell 5 `Set-Content -Encoding utf8` writes a **BOM**; read
   config as UTF-8 with optional BOM (Python: `encoding="utf-8-sig"`). **Open item in Python**.
8. Firmware watchdog: a single frame is not enough; stream continuously.
9. **Measuring CPU on Windows:** `thread_time()`/`GetThreadTimes` tick at 15.6 ms and reported
   the USB send as "0 CPU" (wrong); py-spy on Windows counted waits (sleep, Map, ReleaseFrame,
   HID write) as busy (also misleading). `QueryThreadCycleTime` gives exact per-thread cycles:
   use it (`tools/cycles.py`) for per-call costs, and whole-process CPU time for totals.
10. Bar detection costs ~1 ms per frame in numpy at 430x180; run it at 10 Hz (formats change slowly).
11. A 144 Hz game produces ~136 new frames/s; always cap processing (`target_fps`).
12. **DPI awareness** (found when moving to a 4K monitor at 150% scaling): a non-DPI-aware process
    gets *scaled* coordinates (`DesktopCoordinates` = 2560x1440) while the duplicated texture is
    3840x2160 → wrong texture sizes, failed copies. Call `SetProcessDpiAwarenessContext(
    PER_MONITOR_AWARE_V2)` first thing (or in the native app's manifest). Also fixes window rects
    used by activity detection.
13. **Display names are not stable**: swapping monitors renumbered `\\.\DISPLAY1/2`. Default to
    `monitor = "primary"` (the output whose desktop origin is 0,0).
14. Any code that reads `config.toml` must accept a UTF-8 BOM (tools included).

---

## 6. Open items / next steps

- [x] **BOM**: `main.py` now reads config as `utf-8-sig`.
- [ ] Confirm 30 fps looks as smooth as 60 by eye (`[capture] target_fps`).
- [x] **Colors**: accepted by the user with `led_gamma = 1.6`, `white_balance = [1, 1, 1]`
      (orange→red at gamma 1.0 was mostly the gamma). If needed later: `tools/swatches.html`
      (corner-patch mode) and tune `white_balance` (less blue if magenta looks purple).
- [ ] Measure real strip current → set `[power] ma_per_channel`.
- [ ] Rotated monitors (`Rotation` 2/3/4) not supported.
- [ ] HDR (now relevant: new 4K monitor runs HDR, PQ/Rec.2020, 630 nits; check with
      `tools/monitor_info.py`): `DuplicateOutput` returns an SDR-converted BGRA8 image (may look dim
      or washed out). For accurate colors use `IDXGIOutput5::DuplicateOutput1` with
      `R16G16B16A16_FLOAT` (scRGB, linear; GenerateMips then averages linear light natively),
      divide by the SDR white level (`DisplayConfigGetDeviceInfo`, SDR_WHITE_LEVEL / 1000 × 80 nits),
      clip/tone-map highlights, then continue the existing pipeline.
- [ ] Possible: merge equal adjacent LEDs into one segment; temporal dithering for low values.
- [ ] Optional safety check: lower `usb_budget_ma` automatically when the strip is behind a
      bus-powered hub (100 mA/port). `tools/usb_port_info.py` shows the hub IOCTLs; on this PC the
      strip is on the AMD root hub, port 14 (USB 2.0 half), Full Speed, `bMaxPower` = 70 mA.
- [ ] Windowed (non-full-screen) games count as desktop → off; use the hotkey, or add a
      `games = [...]` process list if needed.

---

## 7. Port plan (native version)

Recommended: **Rust** (`windows` crate for D3D11/DXGI, `hidapi` crate for HID, `toml`/`serde` for
config). C++ is equally direct (same COM APIs); Go works via `golang.org/x/sys/windows` + a hidapi
binding but COM is clumsier.

Module map (mirror the Python files):

| Python | Native responsibility |
|---|---|
| `strip.py` | HID open by VID/PID/interface 0, info query, `SC` packet builder, chunking, power backstop |
| `strip.AsyncSender` | sender thread + one-slot mailbox (Rust: `Mutex<Option<Frame>>` + `Condvar`) |
| `capture_gpu.py` | DXGI duplication + mips + staging readback; ACCESS_LOST recovery; pointer-only filter |
| `zones.py` | precompute per-LED rectangles per crop size; sum in linear light (plain loops) |
| `bars.py` | dim-and-flat line test, hysteresis state machine |
| `color.py`, `power.py` | per-LED float math, EMA, gamma encode, mA limit |
| `main.py` | event loop, pacing, keepalive, config hot-reload |

Native-only improvements worth doing:
* **Double-buffer the staging texture** (read frame N-1 while GPU copies frame N) so `Map` never stalls.
* Optionally sample zones on the GPU (compute shader) and read back only 71 colors.
* Run as a tray app; start with Windows; release the HID device cleanly on exit / sleep.
* Expected cost: well under 1% of a core.

Test tools to recreate early: device info (§1.2), edge layout test (`tools/test_edges.py`),
synthetic-frame zone test (right=red, top=green, left=blue → order R…G…B).
