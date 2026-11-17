"""HDR helpers: Windows' "SDR content brightness" (SDR white level) for a monitor, and the
scRGB -> 8-bit sRGB conversion used by the HDR capture path.

Background:
  * With HDR on, the desktop is composed in scRGB: linear light, Rec.709 primaries, 1.0 = 80 nits,
    values > 1.0 for highlights (and < 0 for colors outside sRGB).
  * Windows draws SDR content (desktop, browsers, SDR games) at the "SDR content brightness"
    level from Settings > Display > HDR. DisplayConfigGetDeviceInfo(GET_SDR_WHITE_LEVEL) reports it
    as SDRWhiteLevel, where 1000 = 80 nits. Dividing scRGB by SDRWhiteLevel/1000 maps SDR white to
    1.0, so our LEDs look the same with HDR on or off.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W

import numpy as np

QDC_ONLY_ACTIVE_PATHS = 2
GET_SOURCE_NAME = 1
GET_SDR_WHITE_LEVEL = 11


class LUID(C.Structure):
    _fields_ = [("LowPart", W.DWORD), ("HighPart", W.LONG)]


class PATH_SOURCE_INFO(C.Structure):
    _fields_ = [("adapterId", LUID), ("id", W.UINT), ("modeInfoIdx", W.UINT), ("statusFlags", W.UINT)]


class PATH_TARGET_INFO(C.Structure):
    _fields_ = [("adapterId", LUID), ("id", W.UINT), ("modeInfoIdx", W.UINT),
                ("outputTechnology", W.UINT), ("rotation", W.UINT), ("scaling", W.UINT),
                ("refreshNum", W.UINT), ("refreshDen", W.UINT), ("scanLineOrdering", W.UINT),
                ("targetAvailable", W.BOOL), ("statusFlags", W.UINT)]


class PATH_INFO(C.Structure):
    _fields_ = [("sourceInfo", PATH_SOURCE_INFO), ("targetInfo", PATH_TARGET_INFO), ("flags", W.UINT)]


class MODE_INFO(C.Structure):
    _fields_ = [("infoType", W.UINT), ("id", W.UINT), ("adapterId", LUID), ("data", C.c_byte * 48)]


class HEADER(C.Structure):
    _fields_ = [("type", W.UINT), ("size", W.UINT), ("adapterId", LUID), ("id", W.UINT)]


class SOURCE_NAME(C.Structure):
    _fields_ = [("header", HEADER), ("viewGdiDeviceName", W.WCHAR * 32)]


class SDR_WHITE_LEVEL(C.Structure):
    _fields_ = [("header", HEADER), ("SDRWhiteLevel", W.ULONG)]


def sdr_white_nits(gdi_name: str) -> float | None:
    """SDR white level in nits for the monitor whose GDI name is e.g. '\\\\.\\DISPLAY1'."""
    u32 = C.windll.user32
    n_paths, n_modes = W.UINT(), W.UINT()
    if u32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, C.byref(n_paths), C.byref(n_modes)):
        return None
    paths, modes = (PATH_INFO * n_paths.value)(), (MODE_INFO * n_modes.value)()
    if u32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, C.byref(n_paths), paths,
                              C.byref(n_modes), modes, None):
        return None
    for p in paths[: n_paths.value]:
        src = SOURCE_NAME()
        src.header = HEADER(GET_SOURCE_NAME, C.sizeof(SOURCE_NAME), p.sourceInfo.adapterId,
                            p.sourceInfo.id)
        if u32.DisplayConfigGetDeviceInfo(C.byref(src.header)) or src.viewGdiDeviceName != gdi_name:
            continue
        wl = SDR_WHITE_LEVEL()
        wl.header = HEADER(GET_SDR_WHITE_LEVEL, C.sizeof(SDR_WHITE_LEVEL), p.targetInfo.adapterId,
                           p.targetInfo.id)
        if u32.DisplayConfigGetDeviceInfo(C.byref(wl.header)):
            return None
        return wl.SDRWhiteLevel / 1000.0 * 80.0
    return None


# Linear (0..1) -> sRGB byte lookup table. 16384 steps keeps the darkest codes exact.
_LUT_N = 16384
_x = np.arange(_LUT_N, dtype=np.float64) / (_LUT_N - 1)
_SRGB8 = np.rint(255 * np.where(_x <= 0.0031308, 12.92 * _x,
                                1.055 * _x ** (1 / 2.4) - 0.055)).astype(np.uint8)


def scrgb_to_bgra8(rgba16: np.ndarray, white_nits: float, out: np.ndarray) -> None:
    """scRGB float RGBA (h, w, 4) -> sRGB-encoded BGRA uint8 written into `out` (h, w, 4).

    1. scale so SDR white (white_nits) = 1.0      (scRGB 1.0 = 80 nits)
    2. clip negatives (colors outside the sRGB gamut)
    3. highlights > 1.0: divide the pixel by its max channel (keeps hue instead of clipping
       each channel separately, which would shift e.g. bright orange towards yellow)
    4. encode to sRGB bytes via LUT; the rest of the pipeline is unchanged
    """
    # Per-channel 2-D ops: measured faster in numpy (1.1 ms) than one combined 3-D version
    # (1.4 ms: bigger temporaries, 3-D fancy indexing). Natively this is a trivial pixel loop.
    k = np.float32(80.0 / white_nits)
    r = np.maximum(rgba16[..., 0].astype(np.float32) * k, 0)
    g = np.maximum(rgba16[..., 1].astype(np.float32) * k, 0)
    b = np.maximum(rgba16[..., 2].astype(np.float32) * k, 0)
    m = np.maximum(np.maximum(r, g), np.maximum(b, 1.0))     # >= 1; == 1 for in-range pixels
    s = np.float32(_LUT_N - 1) / m
    out[..., 0] = _SRGB8[(b * s).astype(np.int32)]
    out[..., 1] = _SRGB8[(g * s).astype(np.int32)]
    out[..., 2] = _SRGB8[(r * s).astype(np.int32)]
    out[..., 3] = 255
