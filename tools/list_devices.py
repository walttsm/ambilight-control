"""Lesson 1: list every HID interface exposed by the LED strip.

A USB device can expose several interfaces; each shows up as its own HID path.
SyncLight picks the one with interface_number == 0 and manufacturer "ROBOBLOQ".
"""
import hid

VID, PID = 0x1A86, 0xFE07  # WCH chip, Robobloq strip (from SyncLight's config.json)

for d in hid.enumerate(VID, PID):
    print(
        f"interface={d['interface_number']}  usage_page=0x{d['usage_page']:04X}  "
        f"usage=0x{d['usage']:04X}  manufacturer={d['manufacturer_string']!r}  "
        f"product={d['product_string']!r}\n    path={d['path'].decode()}"
    )
