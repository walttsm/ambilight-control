"""Lesson 14: what does Windows know about the USB port the strip is plugged into?

Uses the same hub IOCTLs as Microsoft's USBView:
  1. CM_Get_Device_Interface_List(GUID_DEVINTERFACE_USB_HUB)    -> every hub's device path
  2. IOCTL_USB_GET_NODE_INFORMATION                              -> number of ports
  3. IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX  (per port)   -> VID/PID, bcdUSB, actual speed
  4. IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2             -> USB versions the PORT supports
  5. IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES                     -> USB-C connector?
  6. IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION               -> config descriptor: bMaxPower,
                                                                    self/bus-powered
All structs in usbioctl.h are byte-packed, so we parse raw buffers with `struct`.
"""
import ctypes as C
import struct
import sys
import uuid
from ctypes import wintypes as W

VID, PID = 0x1A86, 0xFE07

# IOCTL code = (FILE_DEVICE_USB 0x22 << 16) | (function << 2)   [METHOD_BUFFERED, FILE_ANY_ACCESS]
IOCTL_USB_GET_NODE_INFORMATION = 0x220408
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = 0x220410
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = 0x220448
IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES = 0x220458
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 = 0x22045C
SPEEDS = {0: "Low (1.5 Mbit/s, USB 1.x)", 1: "Full (12 Mbit/s, USB 1.1/2.0)",
          2: "High (480 Mbit/s, USB 2.0)", 3: "Super (5 Gbit/s, USB 3.x)", 4: "SuperPlus (10+ Gbit/s)"}

GUID_DEVINTERFACE_USB_HUB = uuid.UUID("f18a0e88-c30c-11d0-8815-00a0c906bed8")


class GUID(C.Structure):
    _fields_ = [("a", W.DWORD), ("b", W.WORD), ("c", W.WORD), ("d", C.c_ubyte * 8)]


def guid(u: uuid.UUID) -> GUID:
    return GUID(u.fields[0], u.fields[1], u.fields[2], (C.c_ubyte * 8)(*u.bytes[8:]))


cfg = C.windll.cfgmgr32
k32 = C.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = W.HANDLE
k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
k32.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD,
                                C.POINTER(W.DWORD), C.c_void_p]
INVALID_HANDLE = W.HANDLE(-1).value


def hub_paths() -> list[str]:
    g = guid(GUID_DEVINTERFACE_USB_HUB)
    n = W.ULONG()
    cfg.CM_Get_Device_Interface_List_SizeW(C.byref(n), C.byref(g), None, 0)
    buf = C.create_unicode_buffer(n.value)
    cfg.CM_Get_Device_Interface_ListW(C.byref(g), None, buf, n.value, 0)
    return [p for p in buf[: n.value].split("\0") if p]


def ioctl(h, code: int, inbuf: bytes, outsize: int) -> bytes | None:
    ib = C.create_string_buffer(inbuf, max(len(inbuf), outsize))
    got = W.DWORD()
    ok = k32.DeviceIoControl(h, code, ib, len(inbuf), ib, max(len(inbuf), outsize), C.byref(got), None)
    return ib.raw[: got.value] if ok else None


def main() -> None:
    for path in hub_paths():
        h = k32.CreateFileW(path, 0x40000000, 0x2, None, 3, 0, None)  # GENERIC_WRITE, SHARE_WRITE
        if h in (None, INVALID_HANDLE):
            continue
        try:
            node = ioctl(h, IOCTL_USB_GET_NODE_INFORMATION, b"\0" * 80, 80)
            if not node:
                continue
            nports = node[6]                               # USB_HUB_DESCRIPTOR.bNumberOfPorts
            for port in range(1, nports + 1):
                info = ioctl(h, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX,
                             struct.pack("<I", port) + b"\0" * 64, 512)
                if not info or len(info) < 35:
                    continue
                bcd_usb, = struct.unpack_from("<H", info, 6)
                vid, pid = struct.unpack_from("<HH", info, 12)
                if (vid, pid) != (VID, PID):
                    continue
                speed = info[23]
                print(f"Strip found on hub port {port}")
                print(f"  hub path            : {path}")
                print(f"  device says USB     : {bcd_usb >> 8}.{(bcd_usb >> 4) & 0xF}{bcd_usb & 0xF} (bcdUSB)")
                print(f"  running at          : {SPEEDS.get(speed, speed)}")

                v2 = ioctl(h, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2,
                           struct.pack("<IIII", port, 16, 0b111, 0), 16)
                if v2:
                    _, _, protos, flags = struct.unpack("<IIII", v2)
                    names = [n for b, n in ((1, "1.1"), (2, "2.0"), (4, "3.x")) if protos & b]
                    print(f"  port supports USB   : {', '.join(names)}")
                    print(f"  SuperSpeed in use   : {bool(flags & 1)}")

                pc = ioctl(h, IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES,
                           struct.pack("<II", port, 0) + b"\0" * 8, 512)
                if pc:
                    props, = struct.unpack_from("<I", pc, 8)
                    print(f"  user-connectable    : {bool(props & 1)}")
                    print(f"  USB-C connector     : {bool(props & 8)}")

                # GET_DESCRIPTOR(CONFIGURATION, index 0): bmRequest 0x80, bRequest 6, wValue 0x0200
                req = struct.pack("<IBBHHH", port, 0x80, 6, 0x0200, 0, 9) + b"\0" * 9
                d = ioctl(h, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, req, len(req))
                if d and len(d) >= 21:
                    cd = d[12:]                              # after USB_DESCRIPTOR_REQUEST header
                    attrs, max_power = cd[7], cd[8]
                    unit = 8 if speed >= 3 else 2            # SuperSpeed: 8 mA units, else 2 mA
                    print(f"  device requested    : {max_power * unit} mA (bMaxPower)")
                    print(f"  device powered by   : {'its own supply' if attrs & 0x40 else 'USB bus'}")
                return
        finally:
            k32.CloseHandle(h)
    print("strip not found on any hub")
    sys.exit(1)


if __name__ == "__main__":
    main()
