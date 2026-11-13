# Ambilight (Python prototype)

Screen-synced LED strip for the Robobloq USB controller (VID 1A86 / PID FE07), replacing SyncLight.
See **DESIGN.md** for how it works and the plan for the native version.

## Setup
Windows, Python 3.14:
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run
Close SyncLight first (only one program can drive the strip).

```powershell
cd C:\Users\waltm\ambilight
.\.venv\Scripts\python.exe -m ambilight.main --stats
```
Ctrl+C stops it and turns the LEDs off. Most `config.toml` settings apply live when you save the file.

The LEDs start off, turn on automatically when a full-screen game or full-screen video starts,
and turn off 2 minutes after it ends. **Ctrl+Alt+L** toggles them on/off at any time.

## Autostart
A shortcut in the Startup folder starts it invisibly at logon:
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Ambilight.lnk`
→ `.venv\Scripts\pythonw.exe -m ambilight.main --log ambilight.log`

* Output/errors: `ambilight.log` in this folder (recreated at each start).
* Stop it: Task Manager → Details → end `pythonw.exe` (or reboot without the shortcut).
* Disable autostart: Task Manager → Startup apps → "Ambilight", or delete the shortcut.
* Only one copy can run (a second one exits). Errors (strip unplugged, sleep/resume, GPU reset)
  restart the pipeline after 5 s automatically.

## Files
| Path | What |
|---|---|
| `config.toml` | layout, capture, bars, color, power, smoothing settings |
| `ambilight/strip.py` | USB HID driver + background sender (enforces the USB current limit) |
| `ambilight/capture_gpu.py` | DXGI capture with GPU downscaling |
| `ambilight/zones.py`, `bars.py`, `color.py`, `power.py` | processing stages |
| `ambilight/main.py` | main loop |
| `tools/` | device info, layout tests, benchmarks, bar debugging, `swatches.html` calibration page |

## Calibrate colors
Open `tools\swatches.html` in a browser, press F11, click through the colors while the program
runs, and adjust `[color] white_balance` / `led_gamma` in `config.toml` (saved changes apply within ~1 s).
