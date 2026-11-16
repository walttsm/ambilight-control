"""Which monitor will be captured, and is HDR on? (IDXGIOutput6::GetDesc1)

HDR matters: with HDR on, Desktop Duplication (DuplicateOutput) hands us an SDR-converted
BGRA8 image, which can look washed out / dimmer than what you see.
"""
import ctypes as C
import sys
from ctypes import wintypes as W
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambilight import capture_gpu as cg  # noqa: E402

IID_IDXGIOutput6 = cg.GUID.parse("068346e8-aaec-4b84-add7-137f513f77a1")
OUTPUT6_GET_DESC1 = 27      # IDXGIOutput (7-18), 1 (19-22), 2 (23), 3 (24), 4 (25), 5 (26), 6 (27)
COLOR_SPACES = {0: "SDR (sRGB)", 12: "HDR (PQ / Rec.2020)", 1: "scRGB (linear, FP16)"}


class DXGI_OUTPUT_DESC1(C.Structure):
    _fields_ = [("DeviceName", W.WCHAR * 32), ("DesktopCoordinates", W.RECT),
                ("AttachedToDesktop", W.BOOL), ("Rotation", W.UINT), ("Monitor", W.HMONITOR),
                ("BitsPerColor", W.UINT), ("ColorSpace", W.UINT),
                ("Primaries", C.c_float * 8), ("MinLuminance", C.c_float),
                ("MaxLuminance", C.c_float), ("MaxFullFrameLuminance", C.c_float)]


name = sys.argv[1] if len(sys.argv) > 1 else "primary"
cap = cg.GpuCapture(name, 4)
print(f"'{name}' -> {cap.desc.DeviceName}: {cap.width}x{cap.height}, rotation {cap.rotation} "
      f"(1 = none), downscaled {cap.out_w}x{cap.out_h}")
output = cap._objs[2]
o6 = C.c_void_p()
hr = cg._method(output, cg.QUERY_INTERFACE, cg.HRESULT, C.POINTER(cg.GUID), C.POINTER(C.c_void_p))(
    output, C.byref(IID_IDXGIOutput6), C.byref(o6))
if hr >= 0:
    d = DXGI_OUTPUT_DESC1()
    cg._method(o6, OUTPUT6_GET_DESC1, cg.HRESULT, C.POINTER(DXGI_OUTPUT_DESC1))(o6, C.byref(d))
    print(f"color: {COLOR_SPACES.get(d.ColorSpace, d.ColorSpace)}, {d.BitsPerColor} bits/channel, "
          f"peak {d.MaxLuminance:.0f} nits")
    cg._release(o6)
cap.close()
