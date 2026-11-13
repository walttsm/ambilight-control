"""Lesson 2: ask the strip who it is (firmware version, LED count, layout).

Request ("RB" protocol, reverse-engineered from SyncLight's getDeviceFullInfo):
    [ 'R', 'B', length=6, seq, action=0x82, checksum ]
checksum = sum(all previous bytes) % 256

HID detail: every write starts with a *report ID* byte. This device uses
unnumbered reports, so the report ID is 0 and the real payload follows it.
"""
import hid

VID, PID = 0x1A86, 0xFE07


def checksum(data: bytes) -> int:
    return sum(data) % 256


def open_strip() -> hid.device:
    for d in hid.enumerate(VID, PID):
        if d["interface_number"] == 0:
            dev = hid.device()
            dev.open_path(d["path"])
            return dev
    raise SystemExit("LED strip not found (is it plugged in?)")


dev = open_strip()
pkt = bytearray(b"RB") + bytes([6, 1, 0x82])
pkt.append(checksum(pkt))
dev.write(b"\x00" + pkt)          # leading 0x00 = report ID

resp = bytes(dev.read(64, timeout_ms=1000))
dev.close()
if not resp:
    raise SystemExit("No answer from device")

# Reply layout for the HID strip (from SyncLight's HID class, getDeviceFullInfo):
#   0-1 "RB" | 2 length | 3 seq echo | 4 action echo | 5-7 device id
#   8 display size (inches) | 11 LED count | 12-19 uuid | 21-23 firmware version
print("raw         :", resp.hex(" "))
print("device id   :", resp[5:8].hex())
print("display size:", resp[8], "inch")
print("LED count   :", resp[11])
print("uuid        :", resp[12:20].hex())
print("firmware    :", ".".join(str(b) for b in resp[21:24]))
