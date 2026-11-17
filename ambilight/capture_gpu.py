"""Screen capture with GPU-side downscaling (DXGI Desktop Duplication + D3D11 GenerateMips).

Why: reading a full 3440x1440 frame back to the CPU (19.8 MB) cost ~17% of a core, mostly inside
the graphics driver. Instead we let the GPU shrink the frame and read back only a tiny mip level.

Per frame:
  1. AcquireNextFrame(timeout)    - blocks (sleeps) until the desktop changes; no polling.
  2. CopySubresourceRegion        - desktop texture -> mip-chain texture, level 0 (GPU->GPU copy).
  3. ReleaseFrame                 - hand the desktop image back to Windows as early as possible.
  4. GenerateMips                 - GPU builds 1/2, 1/4, 1/8... levels. The view is *_UNORM_SRGB, so
                                    the GPU decodes sRGB -> linear, averages, re-encodes: physically
                                    correct box averaging, done in hardware.
  5. CopySubresourceRegion        - chosen mip level -> small STAGING texture (CPU-readable).
  6. Map / copy / Unmap           - e.g. level 3 = 430x180x4 = ~310 KB instead of 19.8 MB.

COM primer: a COM object pointer points to a vtable (array of function pointers). Method #N is
vtable[N], called with the object pointer as the first argument. Indices below come from the
Windows SDK headers (d3d11.h, dxgi.h, dxgi1_2.h): inherited methods come first
(IUnknown: 0-2, IDXGIObject: 3-6, ID3D11DeviceChild: 3-6).
"""
from __future__ import annotations

import ctypes as C
import time
from ctypes import wintypes as W

import numpy as np

from .hdr import scrgb_to_bgra8, sdr_white_nits

HRESULT = C.c_long

# DPI awareness. Without it, Windows gives this process *scaled* coordinates on monitors with
# display scaling (a 3840x2160 monitor at 150% looks like 2560x1440), while the duplicated desktop
# texture is always in real pixels -> wrong texture sizes and wrong window/monitor geometry.
# Per-monitor-aware v2 (-4) makes every API report physical pixels. Must run before any window/
# monitor query, so it happens at import time. Fails harmlessly if already set.
try:
    C.windll.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
except (AttributeError, OSError):
    pass

# --- vtable indices -----------------------------------------------------------------------------
QUERY_INTERFACE, RELEASE = 0, 2
FACTORY1_ENUM_ADAPTERS1 = 12          # IDXGIFactory1::EnumAdapters1
FACTORY1_IS_CURRENT = 13              # IDXGIFactory1::IsCurrent
TEX2D_GET_DESC = 10                   # ID3D11Texture2D::GetDesc (Resource 7-9 before it)
ADAPTER_ENUM_OUTPUTS = 7              # IDXGIAdapter::EnumOutputs
OUTPUT_GET_DESC = 7                   # IDXGIOutput::GetDesc
OUTPUT1_DUPLICATE_OUTPUT = 22         # IDXGIOutput1::DuplicateOutput
OUTPUT5_DUPLICATE_OUTPUT1 = 26        # IDXGIOutput5::DuplicateOutput1 (Output2: 23, 3: 24, 4: 25)
OUTPUT6_GET_DESC1 = 27                # IDXGIOutput6::GetDesc1
DUPL_ACQUIRE_NEXT_FRAME = 8           # IDXGIOutputDuplication::AcquireNextFrame
DUPL_RELEASE_FRAME = 14               # IDXGIOutputDuplication::ReleaseFrame
DEV_CREATE_TEXTURE2D = 5              # ID3D11Device::CreateTexture2D
DEV_CREATE_SRV = 7                    # ID3D11Device::CreateShaderResourceView
CTX_MAP, CTX_UNMAP = 14, 15           # ID3D11DeviceContext::Map / Unmap
CTX_COPY_SUBRESOURCE_REGION = 46      # ID3D11DeviceContext::CopySubresourceRegion
CTX_GENERATE_MIPS = 54                # ID3D11DeviceContext::GenerateMips

# --- constants ----------------------------------------------------------------------------------
DXGI_FORMAT_B8G8R8A8_TYPELESS = 90
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_B8G8R8A8_UNORM_SRGB = 91
DXGI_FORMAT_R16G16B16A16_FLOAT = 10
DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12   # Windows HDR (PQ, Rec.2020)
D3D11_USAGE_DEFAULT, D3D11_USAGE_STAGING = 0, 3
D3D11_BIND_SHADER_RESOURCE, D3D11_BIND_RENDER_TARGET = 0x8, 0x20
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_RESOURCE_MISC_GENERATE_MIPS = 0x1
D3D11_SRV_DIMENSION_TEXTURE2D = 4
D3D11_MAP_READ = 1
D3D_FEATURE_LEVEL_11_0 = 0xB000
D3D11_SDK_VERSION = 7

DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_NOT_FOUND = 0x887A0002


class GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]

    @classmethod
    def parse(cls, s: str) -> "GUID":
        import uuid
        u = uuid.UUID(s)
        return cls(u.fields[0], u.fields[1], u.fields[2],
                   (C.c_ubyte * 8)(*u.bytes[8:]))


IID_IDXGIFactory1 = GUID.parse("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIOutput1 = GUID.parse("00cddea8-939b-4b83-a340-a685226666cc")
IID_IDXGIOutput5 = GUID.parse("80a07424-ab52-42eb-833c-0c42fd282d98")
IID_IDXGIOutput6 = GUID.parse("068346e8-aaec-4b84-add7-137f513f77a1")
IID_ID3D11Texture2D = GUID.parse("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


class DXGI_OUTPUT_DESC(C.Structure):
    _fields_ = [("DeviceName", W.WCHAR * 32), ("DesktopCoordinates", W.RECT),
                ("AttachedToDesktop", W.BOOL), ("Rotation", W.UINT), ("Monitor", W.HMONITOR)]


class DXGI_OUTPUT_DESC1(C.Structure):
    _fields_ = [("DeviceName", W.WCHAR * 32), ("DesktopCoordinates", W.RECT),
                ("AttachedToDesktop", W.BOOL), ("Rotation", W.UINT), ("Monitor", W.HMONITOR),
                ("BitsPerColor", W.UINT), ("ColorSpace", W.UINT),
                ("Primaries", C.c_float * 8), ("MinLuminance", C.c_float),
                ("MaxLuminance", C.c_float), ("MaxFullFrameLuminance", C.c_float)]


class DXGI_OUTDUPL_FRAME_INFO(C.Structure):
    _fields_ = [("LastPresentTime", C.c_int64), ("LastMouseUpdateTime", C.c_int64),
                ("AccumulatedFrames", W.UINT), ("RectsCoalesced", W.BOOL),
                ("ProtectedContentMaskedOut", W.BOOL),
                ("PointerPosition", W.POINT), ("PointerVisible", W.BOOL),
                ("TotalMetadataBufferSize", W.UINT), ("PointerShapeBufferSize", W.UINT)]


class D3D11_TEXTURE2D_DESC(C.Structure):
    _fields_ = [("Width", W.UINT), ("Height", W.UINT), ("MipLevels", W.UINT),
                ("ArraySize", W.UINT), ("Format", W.UINT), ("SampleCount", W.UINT),
                ("SampleQuality", W.UINT), ("Usage", W.UINT), ("BindFlags", W.UINT),
                ("CPUAccessFlags", W.UINT), ("MiscFlags", W.UINT)]


class D3D11_SHADER_RESOURCE_VIEW_DESC(C.Structure):
    # Format, ViewDimension, then a 16-byte union; for TEXTURE2D: MostDetailedMip, MipLevels.
    _fields_ = [("Format", W.UINT), ("ViewDimension", W.UINT),
                ("MostDetailedMip", W.UINT), ("MipLevels", W.UINT), ("_pad", W.UINT * 2)]


class D3D11_MAPPED_SUBRESOURCE(C.Structure):
    _fields_ = [("pData", C.c_void_p), ("RowPitch", W.UINT), ("DepthPitch", W.UINT)]


class ComError(RuntimeError):
    def __init__(self, what: str, hr: int):
        self.hr = hr & 0xFFFFFFFF
        super().__init__(f"{what} failed: HRESULT 0x{self.hr:08X}")


class CaptureLost(Exception):
    """The capture is no longer valid (display mode changed); rebuild it."""


def _method(obj: C.c_void_p, index: int, restype, *argtypes):
    """Return a callable for vtable[index] of a COM object (first arg = the object itself)."""
    vtbl = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p)))[0]
    return C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)(vtbl[index])


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise ComError(what, hr)


def _release(obj: C.c_void_p | None) -> None:
    if obj:
        _method(obj, RELEASE, W.ULONG)(obj)


class GpuCapture:
    """Captures one monitor and returns a small (h, w, 4) BGRA uint8 array per new frame."""

    def __init__(self, display_name: str = "primary", mip_level: int = 3,
                 stale_rebuild_s: float = 10.0, hdr: str = "auto", hdr_white_nits: float = 0):
        """hdr: "auto" = capture the real HDR (FP16) image when Windows HDR is on; "off" = always
        use the SDR-converted image. hdr_white_nits: brightness = 100% LED (0 = Windows' SDR
        content brightness)."""
        self.display_name = display_name
        self.mip_level = mip_level
        self.hdr_mode = hdr
        self.hdr_white_nits = hdr_white_nits
        self.hdr = False
        self.white_nits = 0.0
        self.protected = False      # last frame had DRM-protected content masked out
        self.stale_rebuild_s = stale_rebuild_s
        self.rebuilds = 0           # bumps whenever the capture was rebuilt (size/monitor may change)
        self._objs: list[C.c_void_p] = []
        self.dupl = C.c_void_p()
        self._broken = False        # rebuild failed (display mid-switch); retry at _retry_at
        self._retry_at = 0.0
        self._last_check = self._last_frame_t = time.perf_counter()
        self._open()

    # -- setup ---------------------------------------------------------------------------------
    def _open(self) -> None:
        dxgi, d3d11 = C.windll.dxgi, C.windll.d3d11
        factory = C.c_void_p()
        _check(dxgi.CreateDXGIFactory1(C.byref(IID_IDXGIFactory1), C.byref(factory)),
               "CreateDXGIFactory1")
        self._objs.append(factory)

        # Find the adapter (GPU) + output (monitor) whose device name matches.
        adapter = output = None
        i = 0
        while adapter is None:
            a = C.c_void_p()
            hr = _method(factory, FACTORY1_ENUM_ADAPTERS1, HRESULT, W.UINT, C.POINTER(C.c_void_p))(
                factory, i, C.byref(a))
            if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                break
            _check(hr, "EnumAdapters1")
            j = 0
            while True:
                o = C.c_void_p()
                hr = _method(a, ADAPTER_ENUM_OUTPUTS, HRESULT, W.UINT, C.POINTER(C.c_void_p))(
                    a, j, C.byref(o))
                if (hr & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND:
                    break
                _check(hr, "EnumOutputs")
                desc = DXGI_OUTPUT_DESC()
                _method(o, OUTPUT_GET_DESC, HRESULT, C.POINTER(DXGI_OUTPUT_DESC))(o, C.byref(desc))
                # "primary": the monitor at desktop origin (0,0). Display names (DISPLAY1/2...)
                # get reshuffled when monitors are swapped; "primary" follows Windows' setting.
                rc = desc.DesktopCoordinates
                is_primary = desc.AttachedToDesktop and rc.left == 0 and rc.top == 0
                if (self.display_name.lower() == "primary" and is_primary) or \
                        desc.DeviceName == self.display_name:
                    adapter, output, self.desc = a, o, desc
                    break
                _release(o)
                j += 1
            if adapter is None:
                _release(a)
            i += 1
        if output is None:
            raise RuntimeError(f"monitor {self.display_name!r} not found")
        self._objs += [adapter, output]
        r = self.desc.DesktopCoordinates
        self.width, self.height = r.right - r.left, r.bottom - r.top
        self.rotation = self.desc.Rotation  # 1 = identity; 2/3/4 = 90/180/270 (not handled yet)

        # D3D11 device on THAT adapter (duplication requires the GPU that drives the monitor).
        self.device, self.ctx = C.c_void_p(), C.c_void_p()
        levels = (W.UINT * 1)(D3D_FEATURE_LEVEL_11_0)
        _check(d3d11.D3D11CreateDevice(adapter, 0, None, 0, levels, 1, D3D11_SDK_VERSION,
                                       C.byref(self.device), None, C.byref(self.ctx)),
               "D3D11CreateDevice")
        self._objs += [self.device, self.ctx]

        # HDR? Then duplicate the desktop as FP16 scRGB instead of letting Windows tone-map it
        # down to 8-bit SDR (measured: that image is ~half as bright as the same desktop in SDR).
        self.hdr = self.hdr_mode != "off" and self._output_is_hdr()
        if self.hdr:
            self.white_nits = self._white_nits()
            # FP16 is linear light already, so plain GenerateMips averages correctly.
            store_fmt = view_fmt = stage_fmt = DXGI_FORMAT_R16G16B16A16_FLOAT
            self.desktop_formats = (DXGI_FORMAT_R16G16B16A16_FLOAT,)
        else:
            # TYPELESS storage lets us view it as sRGB for GenerateMips (linear-light averaging)
            # while still accepting a copy from the desktop's B8G8R8A8_UNORM texture.
            store_fmt, view_fmt = DXGI_FORMAT_B8G8R8A8_TYPELESS, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB
            stage_fmt = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB
            self.desktop_formats = (DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB)

        # Mip-chain texture.
        levels_needed = self.mip_level + 1
        td = D3D11_TEXTURE2D_DESC(self.width, self.height, levels_needed, 1,
                                  store_fmt, 1, 0, D3D11_USAGE_DEFAULT,
                                  D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET, 0,
                                  D3D11_RESOURCE_MISC_GENERATE_MIPS)
        self.mip_tex = self._create_texture(td)
        srvd = D3D11_SHADER_RESOURCE_VIEW_DESC(view_fmt,
                                               D3D11_SRV_DIMENSION_TEXTURE2D, 0, levels_needed)
        self.srv = C.c_void_p()
        _check(_method(self.device, DEV_CREATE_SRV, HRESULT, C.c_void_p,
                       C.POINTER(D3D11_SHADER_RESOURCE_VIEW_DESC), C.POINTER(C.c_void_p))(
            self.device, self.mip_tex, C.byref(srvd), C.byref(self.srv)), "CreateShaderResourceView")
        self._objs.append(self.srv)

        # Small CPU-readable texture the size of the chosen mip level.
        self.out_w = max(1, self.width >> self.mip_level)
        self.out_h = max(1, self.height >> self.mip_level)
        sd = D3D11_TEXTURE2D_DESC(self.out_w, self.out_h, 1, 1, stage_fmt,
                                  1, 0, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0)
        self.staging = self._create_texture(sd)

        # Cache hot-path vtable calls once (looking them up every frame is wasted work).
        c = self.ctx
        self._copy = _method(c, CTX_COPY_SUBRESOURCE_REGION, None, C.c_void_p, W.UINT, W.UINT,
                             W.UINT, W.UINT, C.c_void_p, W.UINT, C.c_void_p)
        self._genmips = _method(c, CTX_GENERATE_MIPS, None, C.c_void_p)
        self._map = _method(c, CTX_MAP, HRESULT, C.c_void_p, W.UINT, W.UINT, W.UINT,
                            C.POINTER(D3D11_MAPPED_SUBRESOURCE))
        self._unmap = _method(c, CTX_UNMAP, None, C.c_void_p, W.UINT)
        self._open_duplication()

    def _create_texture(self, desc: D3D11_TEXTURE2D_DESC) -> C.c_void_p:
        tex = C.c_void_p()
        _check(_method(self.device, DEV_CREATE_TEXTURE2D, HRESULT,
                       C.POINTER(D3D11_TEXTURE2D_DESC), C.c_void_p, C.POINTER(C.c_void_p))(
            self.device, C.byref(desc), None, C.byref(tex)), "CreateTexture2D")
        self._objs.append(tex)
        return tex

    def _query(self, obj: C.c_void_p, iid: GUID, what: str) -> C.c_void_p:
        out = C.c_void_p()
        _check(_method(obj, QUERY_INTERFACE, HRESULT, C.POINTER(GUID), C.POINTER(C.c_void_p))(
            obj, C.byref(iid), C.byref(out)), f"QueryInterface({what})")
        return out

    def _output_is_hdr(self) -> bool:
        """IDXGIOutput6::GetDesc1: color space PQ/Rec.2020 means Windows HDR is on."""
        try:
            o6 = self._query(self._objs[2], IID_IDXGIOutput6, "IDXGIOutput6")
        except ComError:
            return False                        # older Windows: no HDR desktop
        try:
            d = DXGI_OUTPUT_DESC1()
            _method(o6, OUTPUT6_GET_DESC1, HRESULT, C.POINTER(DXGI_OUTPUT_DESC1))(o6, C.byref(d))
            return d.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
        finally:
            _release(o6)

    def _white_nits(self) -> float:
        """Brightness that maps to 100% LED: configured, else Windows' SDR white level."""
        if self.hdr_white_nits:
            return float(self.hdr_white_nits)
        return sdr_white_nits(self.desc.DeviceName) or 240.0   # 240 = Windows' default level

    def _open_duplication(self) -> None:
        output = self._objs[2]
        self.dupl = C.c_void_p()
        if self.hdr:
            # IDXGIOutput5::DuplicateOutput1 lets us ask for FP16 (scRGB) desktop images.
            out5 = self._query(output, IID_IDXGIOutput5, "IDXGIOutput5")
            fmts = (W.UINT * 1)(DXGI_FORMAT_R16G16B16A16_FLOAT)
            try:
                _check(_method(out5, OUTPUT5_DUPLICATE_OUTPUT1, HRESULT, C.c_void_p, W.UINT, W.UINT,
                               C.POINTER(W.UINT), C.POINTER(C.c_void_p))(
                    out5, self.device, 0, 1, fmts, C.byref(self.dupl)), "DuplicateOutput1")
            finally:
                _release(out5)
        else:
            out1 = self._query(output, IID_IDXGIOutput1, "IDXGIOutput1")
            try:
                _check(_method(out1, OUTPUT1_DUPLICATE_OUTPUT, HRESULT, C.c_void_p,
                               C.POINTER(C.c_void_p))(out1, self.device, C.byref(self.dupl)),
                       "DuplicateOutput")
            finally:
                _release(out1)
        d = self.dupl
        self._acquire = _method(d, DUPL_ACQUIRE_NEXT_FRAME, HRESULT, W.UINT,
                                C.POINTER(DXGI_OUTDUPL_FRAME_INFO), C.POINTER(C.c_void_p))
        self._release_frame = _method(d, DUPL_RELEASE_FRAME, HRESULT)
        self._info = DXGI_OUTDUPL_FRAME_INFO()
        self._buf = np.empty((self.out_h, self.out_w, 4), np.uint8)

    # -- recovery ------------------------------------------------------------------------------
    # Display mode changes (HDR on/off, resolution, refresh rate, driver reset, fullscreen
    # switches) invalidate the capture. Re-opening only the duplication is NOT enough: the device,
    # output object and texture sizes can all be stale, and a stale capture fails *silently*
    # (AcquireNextFrame just times out forever -> frozen LEDs). So we rebuild everything, and also
    # detect staleness proactively instead of waiting for an error that may never come.
    @property
    def mode_text(self) -> str:
        return f"HDR (FP16, white = {self.white_nits:.0f} nits)" if self.hdr else "SDR"

    def _is_current(self) -> bool:
        """IDXGIFactory1::IsCurrent: False once adapters/outputs changed since we enumerated."""
        if not self._objs:
            return False
        f = self._objs[0]
        return bool(_method(f, FACTORY1_IS_CURRENT, W.BOOL)(f))

    def _rebuild(self, reason: str, quiet: bool = False) -> bool:
        now = time.perf_counter()
        try:
            self.close()
            self._open()
        except Exception as e:
            if not self._broken:
                print(f"{time.strftime('%H:%M:%S')} capture lost ({reason}); "
                      f"retrying every 0.5 s ({e})")
            self._broken, self._retry_at = True, now + 0.5
            return False
        if self._broken or not quiet:
            print(f"{time.strftime('%H:%M:%S')} capture rebuilt ({reason}): "
                  f"{self.desc.DeviceName} {self.width}x{self.height} {self.mode_text}")
        self._broken = False
        self.rebuilds += 1
        self._last_frame_t = self._last_check = now
        return True

    # -- per frame -----------------------------------------------------------------------------
    def next_frame(self, timeout_ms: int) -> np.ndarray | None:
        """Wait up to timeout_ms for a new desktop image. Returns BGRA (out_h, out_w, 4) or None.

        Never raises for display changes: it rebuilds the capture (retrying while the display is
        mid-switch) and returns None meanwhile. The returned array is reused between calls.
        """
        now = time.perf_counter()
        if self._broken:
            if now < self._retry_at:
                time.sleep(min(timeout_ms / 1000.0, self._retry_at - now))
                return None
            if not self._rebuild("retry"):
                return None
        elif now - self._last_check >= 1.0:          # cheap checks, once per second
            self._last_check = now
            if self.hdr:   # "SDR content brightness" slider changes don't reset the display
                self.white_nits = self._white_nits()
            if not self._is_current():
                if not self._rebuild("display configuration changed"):
                    return None
            elif now - self._last_frame_t >= self.stale_rebuild_s:
                # Safety net for failures Windows doesn't report. On a truly static screen this
                # just re-delivers the current image (the first frame after a rebuild always
                # comes), so it is harmless; it costs a few ms every stale_rebuild_s.
                if not self._rebuild(f"no new frame for {self.stale_rebuild_s:.0f} s", quiet=True):
                    return None
        try:
            frame = self._next_frame_inner(timeout_ms)
        except (ComError, CaptureLost) as e:
            self._rebuild(str(e))
            return None
        if frame is not None:
            self._last_frame_t = time.perf_counter()
        return frame

    def _next_frame_inner(self, timeout_ms: int) -> np.ndarray | None:
        deadline = time.perf_counter() + timeout_ms / 1000.0
        while True:
            resource = C.c_void_p()
            remaining = max(0, int((deadline - time.perf_counter()) * 1000))
            hr = self._acquire(self.dupl, remaining, C.byref(self._info), C.byref(resource))
            code = hr & 0xFFFFFFFF
            if code == DXGI_ERROR_WAIT_TIMEOUT:
                return None
            if code == DXGI_ERROR_ACCESS_LOST:      # mode change, HDR toggle, UAC prompt...
                raise CaptureLost("access lost (display mode changed)")
            _check(hr, "AcquireNextFrame")
            if self._info.LastPresentTime != 0:     # the desktop image really changed
                # DRM video (e.g. Netflix in Edge) is blacked out in captures; tell the caller.
                self.protected = bool(self._info.ProtectedContentMaskedOut)
                break
            # Pointer-only update (the mouse moved; the image didn't). A gaming mouse can cause
            # hundreds of these per second, so don't wake the caller: keep waiting.
            _release(resource)
            self._release_frame(self.dupl)
            if remaining == 0:
                return None
        try:
            tex = C.c_void_p()
            _check(_method(resource, QUERY_INTERFACE, HRESULT, C.POINTER(GUID),
                           C.POINTER(C.c_void_p))(resource, C.byref(IID_ID3D11Texture2D),
                                                  C.byref(tex)), "QueryInterface(Texture2D)")
            try:
                # Size changed under us (resolution change)? The copy would silently do nothing.
                td = D3D11_TEXTURE2D_DESC()
                _method(tex, TEX2D_GET_DESC, None, C.POINTER(D3D11_TEXTURE2D_DESC))(tex, C.byref(td))
                if (td.Width, td.Height) != (self.width, self.height):
                    raise CaptureLost(f"desktop size changed to {td.Width}x{td.Height}")
                if td.Format not in self.desktop_formats:      # e.g. HDR flipped unannounced
                    raise CaptureLost(f"desktop format changed to {td.Format}")
                self._copy(self.ctx, self.mip_tex, 0, 0, 0, 0, tex, 0, None)
            finally:
                _release(tex)
        finally:
            _release(resource)
            self._release_frame(self.dupl)

        self._genmips(self.ctx, self.srv)
        self._copy(self.ctx, self.staging, 0, 0, 0, 0, self.mip_tex, self.mip_level, None)

        m = D3D11_MAPPED_SUBRESOURCE()
        _check(self._map(self.ctx, self.staging, 0, D3D11_MAP_READ, 0, C.byref(m)), "Map")
        try:
            # Rows are RowPitch bytes apart (GPU alignment), which may exceed width * pixel size.
            raw = np.ctypeslib.as_array(C.cast(m.pData, C.POINTER(C.c_uint8)),
                                        shape=(self.out_h, m.RowPitch))
            if self.hdr:      # FP16 RGBA (8 bytes/pixel) -> 8-bit sRGB BGRA like the SDR path
                px = raw[:, : self.out_w * 8].view(np.float16).reshape(self.out_h, self.out_w, 4)
                scrgb_to_bgra8(px, self.white_nits, self._buf)
            else:
                self._buf[:] = raw[:, : self.out_w * 4].reshape(self.out_h, self.out_w, 4)
        finally:
            self._unmap(self.ctx, self.staging, 0)
        return self._buf

    def close(self) -> None:
        _release(getattr(self, "dupl", None))
        self.dupl = C.c_void_p()
        for o in reversed(self._objs):
            _release(o)
        self._objs.clear()
