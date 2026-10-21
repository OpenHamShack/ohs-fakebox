"""PicoTuner firmware, as far as it is reading and checking: the version a
tuner answers or broadcasts, whether a UF2 image fits the chip in BOOTSEL,
which chip a BOOTSEL drive's INFO_UF2.TXT names, the 1200-baud touch, and
the copy onto the drive with its fsync.

openhamshack.py's "PicoTuner firmware" section has the sources and the flow
(download, hold the receivers, reboot to BOOTSEL, copy, wait, switch the
receiver over) and keeps the USB exchange that the tests replace.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path


def uf2_info(drive: Path) -> dict | None:
    """INFO_UF2.TXT of a UF2 boot loader's drive as {key: value}, or None for
    an ordinary drive. A Pico held in BOOTSEL (or rebooted into it for a
    PicoTuner firmware update) appears as a small FAT drive, RPI-RP2 or
    RP2350, that openhamshack-usb mounts like any other: the mount is what the
    updater copies the firmware through. Its ROM writes this file, e.g.
    "UF2 Bootloader v3.0 / Model: Raspberry Pi RP2 / Board-ID: RPI-RP2".
    Every UF2 boot loader carries one (Adafruit's, micro:bit's too), so its
    presence alone is enough to say "not a drive for recordings or films"."""
    try:
        text = (Path(drive) / "INFO_UF2.TXT").read_text(errors="replace")[:4096]
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
        elif line.strip():
            out.setdefault("_", line.strip())   # the "UF2 Bootloader v3.0" banner
    return out


# UF2 family ids (github.com/microsoft/uf2, uf2families.json). A Pico 2
# takes any of its three; 0xe48bff57 is the RP2350's "absolute" block that
# universal images carry for its E10 erratum, and no chip needs it alone.
UF2_FAMILIES = {"RP2040": {0xE48BFF56}, "RP2350": {0xE48BFF59, 0xE48BFF5A, 0xE48BFF5B}}


def parse_pico_version(text: str | None) -> str | None:
    """'0.12' from what G4EML names a release or its file: v0.12_alpha,
    v0.11-alpha, PicoTuner_V_0_12.uf2, PicoTunerV0-11.uf2, v0.0_Initial."""
    m = re.search(r"[vV][._-]?(\d+)[._-](\d+)", str(text or ""))
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else None


def parse_pico_eth_software(text: str | None) -> tuple[str, str] | None:
    """(version, board) from PicoTuneWH's file name as its broadcast reports
    it, 'ptwh0v3e-w5500.uf2' -> ('ptwh0v3e', 'w5500')."""
    m = re.search(r"\b([a-z]*wh\d+v\d+[a-z]?)-(w5500|w5100s)\b", str(text or "").lower())
    return (m.group(1), m.group(2)) if m else None


def pico_eth_newer(latest: str, current: str | None) -> bool:
    """ptwh0v3e is newer than ptwh0v3c; the trailing letter counts."""
    def key(v: str) -> tuple:
        m = re.search(r"(\d+)v(\d+)([a-z]?)$", v or "")
        return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else ()
    return bool(current) and key(latest) > key(current)


def uf2_families(data: bytes) -> set[int]:
    """The chip families a UF2 image is for. ValueError for anything that is
    not a whole, well-formed UF2: a truncated download, an HTML error page
    saved under the asset's name, a zip."""
    if not data or len(data) % 512:
        raise ValueError("not a UF2 file (size)")
    fams: set[int] = set()
    for off in range(0, len(data), 512):
        m0, m1, flags, _addr, size, _no, count, fam = struct.unpack_from("<8I", data, off)
        (m_end,) = struct.unpack_from("<I", data, off + 508)
        if (m0, m1, m_end) != (0x0A324655, 0x9E5D5157, 0x0AB16F30) or size > 476 or not count:
            raise ValueError(f"not a UF2 file (block {off // 512})")
        if flags & 0x2000:
            fams.add(fam)
    return fams


def uf2_fits(data: bytes, chip: str) -> bool:
    try:
        return bool(uf2_families(data) & UF2_FAMILIES.get(chip, set()))
    except ValueError:
        return False


def bootsel_chip(drive: Path) -> str | None:
    """'RP2040' or 'RP2350' for a Raspberry Pi boot ROM's drive, else None -
    another maker's UF2 boot loader included. Its INFO_UF2.TXT reads
    "Model: Raspberry Pi RP2 / Board-ID: RPI-RP2" (RP2040) or "Model:
    Raspberry Pi RP2350 / Board-ID: RP2350". Both lines must agree: nothing
    is ever written to a drive this does not name."""
    info = uf2_info(drive) or {}
    model, board = info.get("Model", ""), info.get("Board-ID", "")
    if model == "Raspberry Pi RP2" and board.startswith("RPI-RP2"):
        return "RP2040"
    if model == "Raspberry Pi RP2350" and board.startswith("RP2350"):
        return "RP2350"
    return None


def pico_version_reply(data: bytes) -> str | None:
    """'0.12' from the answer to 0xBA ([0, 0, major, minor], the first two
    the FTDI status bytes it imitates); '' from firmware too old to know the
    command (FA BA); None for no answer worth reading."""
    body = data[2:]
    if body[:2] == b"\xfa\xba":
        return ""
    return f"{body[0]}.{body[1]}" if len(body) >= 2 else None


def parse_pico_broadcast(text: str) -> dict:
    """The fields of PicoTuneWH's UDP 9997 broadcast: '   Software   ptwh0v3e-w5500.uf2'
    -> {'Software': 'ptwh0v3e-w5500.uf2'}. Name and value are parted by a
    run of spaces; names have single spaces in them ('Pico serial')."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\S+(?: \S+)*)\s{2,}(\S.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def pico_touch_1200(tty: str) -> None:
    """Set a pico-sdk serial port to 1200 baud: its stdio_usb takes that as
    'reboot to BOOTSEL'."""
    import termios
    fd = os.open(f"/dev/{tty}", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = termios.B1200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    finally:
        os.close(fd)


def pico_copy(image: bytes, drive: Path) -> str:
    """Write the image to the boot drive and flush it. The boot ROM reboots
    as soon as the last block is in - often before the kernel has written
    the file's directory entry - so a write or fsync error once the data is
    out is normal, not a failure: whether the Pico comes back is the test.
    Returns the error, if any, for the message when it does not."""
    try:
        fd = os.open(drive / "PICOTUNER.UF2", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            view = memoryview(image)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""
