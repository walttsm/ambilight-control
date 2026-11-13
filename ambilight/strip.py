"""Driver for the Robobloq USB-HID LED strip (VID 0x1A86, PID 0xFE07, firmware > 1.0.2).

Protocol (reverse-engineered from SyncLight 2.22.1):

  "Sync screen" frame:
      'S' 'C' | length (uint16, big-endian, whole packet) | seq | 0x80 |
      segments... | checksum
  segment (5 bytes): first_led, R, G, B, last_led    (LEDs are 1-based, inclusive)
  checksum: sum(all previous bytes) % 256
  seq: 1..254, incremented per packet (the device echoes it in replies)

  HID transport: split the packet into 64-byte chunks; prefix each chunk with
  report ID 0x00 before writing (unnumbered reports).
"""
from __future__ import annotations

import threading

import hid
import numpy as np

from .power import PowerModel

VID, PID = 0x1A86, 0xFE07
CHUNK = 64
ACTION_SYNC_SCREEN = 0x80
ACTION_READ_INFO = 0x82


def _checksum(data: bytes | bytearray) -> int:
    return sum(data) & 0xFF


class Strip:
    """power: [power] settings for the current-limit backstop (None = conservative defaults).
    Every frame sent through this driver is limited, whoever calls it."""

    def __init__(self, power: dict | None = None) -> None:
        path = next(
            (d["path"] for d in hid.enumerate(VID, PID) if d["interface_number"] == 0),
            None,
        )
        if path is None:
            raise RuntimeError("LED strip not found (plugged in? SyncLight closed?)")
        self.dev = hid.device()
        self.dev.open_path(path)
        self._seq = 0
        self.info = self.read_info()
        self.led_count: int = self.info["led_count"]
        self.power = PowerModel(power, self.led_count)

    # -- low level -----------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq = self._seq % 254 + 1
        return self._seq

    def _write(self, packet: bytes) -> None:
        for i in range(0, len(packet), CHUNK):
            if self.dev.write(b"\x00" + packet[i : i + CHUNK]) < 0:   # some hidapi versions
                raise OSError("HID write failed (device unplugged or asleep?)")  # return -1

    def read_info(self) -> dict:
        pkt = bytearray(b"RB") + bytes([6, self._next_seq(), ACTION_READ_INFO])
        pkt.append(_checksum(pkt))
        self._write(bytes(pkt))
        r = bytes(self.dev.read(64, timeout_ms=1000))
        if len(r) < 24 or r[:2] != b"RB":
            raise RuntimeError(f"unexpected reply: {r.hex(' ')}")
        return {
            "device_id": r[5:8].hex(),
            "display_inches": r[8],
            "led_count": r[11],
            "uuid": r[12:20].hex(),
            "firmware": f"{r[21]}.{r[22]}.{r[23]}",
        }

    # -- high level ----------------------------------------------------------
    def send(self, colors: np.ndarray) -> None:
        """colors: uint8 array shaped (led_count, 3), RGB, LED order = strip order."""
        colors = np.asarray(colors, dtype=np.uint8).reshape(-1, 3)
        colors = self.power.limit_bytes(colors)        # safety backstop: never exceed USB budget
        n = len(colors)
        # Build all segments at once with numpy: one segment per LED -> [i, r, g, b, i]
        idx = np.arange(1, n + 1, dtype=np.uint8)[:, None]
        body = np.hstack([idx, colors, idx]).tobytes()

        length = 2 + 2 + 1 + 1 + len(body) + 1
        pkt = bytearray(b"SC")
        pkt += length.to_bytes(2, "big")
        pkt += bytes([self._next_seq(), ACTION_SYNC_SCREEN])
        pkt += body
        pkt.append(_checksum(pkt))
        self._write(bytes(pkt))

    def fill(self, r: int, g: int, b: int) -> None:
        self.send(np.tile([r, g, b], (self.led_count, 1)))

    def close(self) -> None:
        self.dev.close()

    def __enter__(self) -> "Strip":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class AsyncSender:
    """Sends frames on a background thread so USB waits never block capture.

    One-slot mailbox ("latest wins"): if the strip is busy when new colors arrive, the
    older pending frame is simply replaced. We never build a queue of stale frames.
    A 6-report frame takes ~12 ms of pure *waiting* on USB (no CPU), and hidapi releases
    the GIL while waiting, so this thread costs almost nothing.
    """

    def __init__(self, strip: Strip) -> None:
        self.strip = strip
        self._pending: np.ndarray | None = None
        self._cv = threading.Condition()
        self._stop = False
        self.sent = 0
        self.error: Exception | None = None   # set if the device fails (unplugged, sleep...)
        self._thread = threading.Thread(target=self._run, name="hid-sender", daemon=True)
        self._thread.start()

    def submit(self, colors: np.ndarray) -> None:
        with self._cv:
            self._pending = colors
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._pending is not None or self._stop)
                if self._stop:
                    return
                colors, self._pending = self._pending, None
            try:
                self.strip.send(colors)
            except Exception as e:            # report to the main loop instead of dying silently
                self.error = e
                return
            self.sent += 1

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join()
