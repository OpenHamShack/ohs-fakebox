"""Ajazz AKP03E: six keys with screens, three plain buttons, three knobs.

The protocol is undocumented. What is here was measured on workshop's pad
(USB 0300:3002, "HOTSPOTEKUSB HID DEMO") and agrees with FlexDeck's map of
the same model, which also measured the key screens:
- The pad is silent until woken: DIS, LIG 0, CLE all, LIG <level>, STP.
- Commands are `CRT\\0\\0` + a three-letter verb, padded to the output report
  (1024 bytes on this unit), written after a zero report id.
- Input reports (512 bytes, no report id on hidraw) start `ACK\\0\\0OK`; byte
  9 names the control, byte 10 is 1 on press and 0 on release. A knob sends
  one report per detent, a code per direction, with no press byte.
- A key screen takes a 64x64 JPEG: header `BAT\\0\\0` + length (2 bytes, big
  endian) + key, the data in report-sized chunks, then STP. The screens are
  mounted turned: drawn upright, the picture must be turned a quarter clockwise
  (ffmpeg transpose=1) - measured, the opposite quarter showed K1..K6 upside
  down on the pad.

Other Mirabox/Ajazz models (AKP153, AKP815, the 293 family) speak the same
CRT/ACK framing with different report sizes, key codes and screen sizes;
each would be a subclass here with its own tables, once measured.
"""

from __future__ import annotations

import os
import select
import threading
from pathlib import Path

from .base import Driver, control, key_image

VID_PID = "0003:00000300:00003002"
REPORT = 1024
CRT = b"CRT\0\0"
ACK = b"ACK\0\0OK"

# Control codes (byte 9).
KEYS = {1: "K1", 2: "K2", 3: "K3", 4: "K4", 5: "K5", 6: "K6"}
BUTTONS = {0x25: "B1", 0x30: "B2", 0x31: "B3"}
KNOB_PRESS = {0x35: "D1", 0x33: "D2", 0x34: "D3"}
KNOB_TURN = {0x51: ("D1", +1), 0x50: ("D1", -1), 0x91: ("D2", +1), 0x90: ("D2", -1),
             0x61: ("D3", +1), 0x60: ("D3", -1)}


def decode(report: bytes) -> tuple[str, str, int] | None:
    """('press'|'release'|'turn', control, step) for an input report, or None."""
    if len(report) < 11 or report[:7] != ACK:
        return None
    code, state = report[9], report[10]
    if code in KNOB_TURN:
        name, step = KNOB_TURN[code]
        return "turn", name, step
    name = KEYS.get(code) or BUTTONS.get(code) or KNOB_PRESS.get(code)
    if not name:
        return None
    return ("press" if state else "release"), name, 0


def command(verb: bytes, *args: int) -> bytes:
    return (CRT + verb + bytes(args)).ljust(REPORT, b"\0")


def wake_sequence(brightness: int) -> list[bytes]:
    level = max(0, min(100, int(brightness)))
    return [command(b"DIS"), command(b"LIG", 0, 0, 0), command(b"CLE", 0, 0, 0, 0xFF),
            command(b"LIG", 0, 0, level), command(b"STP")]


def image_reports(key: int, jpeg: bytes) -> list[bytes]:
    out = [command(b"BAT", 0, 0, (len(jpeg) >> 8) & 0xFF, len(jpeg) & 0xFF, key)]
    out += [jpeg[at:at + REPORT].ljust(REPORT, b"\0") for at in range(0, len(jpeg), REPORT)]
    out.append(command(b"STP"))
    return out


def find_device(sysfs: Path = Path("/sys/class/hidraw")) -> str | None:
    """The pad's vendor interface (input0; input1 is its keyboard face)."""
    try:
        nodes = sorted(sysfs.iterdir())
    except OSError:
        return None
    for node in nodes:
        try:
            uevent = (node / "device" / "uevent").read_text()
        except OSError:
            continue
        if f"HID_ID={VID_PID}" in uevent and "input0" in uevent:
            return f"/dev/{node.name}"
    return None


class AKP03E(Driver):
    """The pad and its screens. Writes go through one lock: an image is several
    reports and must not interleave with another."""

    model = "ajazz-akp03e"
    name = "Ajazz AKP03E"
    usb = [("0300", "3002")]
    screen = (64, 64)
    # Two rows of screen keys, the three small buttons under them, the three
    # knobs along the bottom - K1 top left, numbered across, as the pad's
    # own codes run.
    controls = [
        control("K1", "screen-key", 0, 0), control("K2", "screen-key", 0, 1), control("K3", "screen-key", 0, 2),
        control("K4", "screen-key", 1, 0), control("K5", "screen-key", 1, 1), control("K6", "screen-key", 1, 2),
        control("B1", "button", 2, 0), control("B2", "button", 2, 1), control("B3", "button", 2, 2),
        control("D1", "knob", 3, 0), control("D2", "knob", 3, 1), control("D3", "knob", 3, 2),
    ]
    # What the pad has always done, from before keys could be remapped.
    default_map = {
        "K1": {"press": "transmit"}, "K2": {"press": "preview"}, "K3": {"press": "record"},
        "K4": {"press": "source"}, "K5": {"press": "screens"}, "K6": {"press": "aim"},
        "B1": {"press": "tx-preset"}, "B2": {"press": "rx-preset"}, "B3": {"press": "modcod"},
        "D1": {"press": "none", "turn": "power"}, "D2": {"press": "rx-cycle", "turn": "rx-tune"},
        "D3": {"press": "none", "turn": "symbol-rate"},
    }
    aim_overlay = ["K4", "K5"]

    @classmethod
    def find(cls, cfg: dict, sysfs: Path = Path("/sys/class/hidraw")) -> list[str]:
        path = find_device(sysfs)
        return [path] if path else []

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.lock = threading.Lock()
        self.shown: dict[int, bytes] = {}

    def write(self, reports: list[bytes]) -> None:
        with self.lock:
            for r in reports:
                os.write(self.fd, b"\0" + r)

    def start(self, brightness: int) -> None:
        self.write(wake_sequence(brightness))
        self.shown.clear()

    def set_brightness(self, level: int) -> None:
        self.write([command(b"LIG", 0, 0, max(0, min(100, int(level)))), command(b"STP")])

    def show(self, cid: str, face: tuple) -> None:
        key = int(cid[1:]) if cid[:1] == "K" else 0
        if not 1 <= key <= 6:
            return
        a, b, bg, *big = face
        jpeg = key_image(a, b, bg, big=bool(big and big[0]))
        if jpeg and self.shown.get(key) != jpeg:   # only what changed: a key is ~2-3 kB of USB
            self.write(image_reports(key, jpeg))
            self.shown[key] = jpeg

    def read(self, timeout: float = 0.5) -> list[tuple[str, str, int]]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        event = decode(os.read(self.fd, 1024))
        return [event] if event else []

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
