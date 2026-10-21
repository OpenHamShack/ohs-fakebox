"""Which cameras and sound cards a PC has, read from sysfs, /proc/asound and
one VIDIOC_QUERYCAP per node, and which of them a stored choice means.

openhamshack.py's "Camera and sound choice" section has the reasoning (capture
nodes asked, never guessed from their number; the by-id link stored; a
missing camera never swapped for another) and keeps the scan caches that
call these. Every function takes the filesystem root it reads, so
test_camera.py builds laptops out of files.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
from pathlib import Path

from .sysfs import diag_attr


VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability), 104 bytes
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
# Set by drivers whose nodes only work once a media-controller pipeline is
# configured: Intel's IPU6 ISYS registers dozens of "capture" nodes that
# deliver nothing to a plain open, which is all ffmpeg does.
V4L2_CAP_IO_MC = 0x20000000
V4L2_CAP_DEVICE_CAPS = 0x80000000
CAMERA_SILENT_NOTE = "This camera has no sound of its own: sending silence. Choose a microphone under Sound."
# Intel IPU6 / IPU7 (Tiger Lake to Panther Lake), by PCI device id. A laptop
# whose camera hangs off one has a MIPI sensor, not a UVC camera: the kernel
# can drive the IPU, but the picture needs libcamera and per-sensor support
# that Debian does not have, so no node ffmpeg can open ever appears.
IPU_PCI = {"9a19": "IPU6", "9a39": "IPU6", "4e19": "IPU6", "465d": "IPU6", "462e": "IPU6",
           "a75d": "IPU6", "7d19": "IPU6", "645d": "IPU7", "b05d": "IPU7"}
# ACPI ids of the camera sensors and the INT3472 power controller that sit
# behind an IPU on laptops. An N150 mini PC has the same IPU function on its
# die (ADL-N, 462e) with nothing behind it, and must not be told its camera
# needs drivers when it has no camera: a sensor, or a laptop chassis, too.
IPU_SENSOR_ACPI = ("INT3472", "INT347", "INT3537", "OVTI", "HIMX", "SONY", "GCTI")
PORTABLE_CHASSIS = {"8", "9", "10", "14", "30", "31", "32"}  # SMBIOS: portable, laptop, notebook, ...


def ipu_camera_text(gen: str) -> str:
    return f"This laptop's camera needs drivers Linux does not have yet (Intel {gen}); use a USB webcam"


def v4l2_querycap(path: str) -> dict | None:
    """driver, card, bus and the node's own capabilities, or None. One ioctl
    on an idle device: no streaming, so the camera's light stays off, and it
    works while the encoder holds the camera (V4L2 allows several opens)."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
    except OSError:
        return None
    finally:
        os.close(fd)

    def text(a: int, b: int) -> str:
        return bytes(buf[a:b]).split(b"\0", 1)[0].decode(errors="replace").strip()
    caps, device_caps = struct.unpack_from("II", buf, 84)
    # device_caps is this node's; capabilities is the whole device's, and on
    # a UVC camera says "capture" for the metadata node too.
    return {"driver": text(0, 16), "card": text(16, 48), "bus": text(48, 80),
            "caps": device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps}


def _node_number(name: str) -> int:
    digits = name.removeprefix("video")
    return int(digits) if digits.isdigit() else 1 << 30


def camera_list(root: Path = Path("/"), querycap=None) -> list[dict]:
    """Every node a picture can be captured from, lowest number first. id is
    what is stored: the by-id link, else by-path, else the node itself."""
    querycap = querycap or v4l2_querycap
    links: dict[str, list[str]] = {}
    for kind in ("by-id", "by-path"):
        try:
            entries = sorted((root / "dev/v4l" / kind).iterdir())
        except OSError:
            continue
        for link in entries:
            links.setdefault(os.path.basename(os.path.realpath(link)), []).append(f"/dev/v4l/{kind}/{link.name}")
    try:
        nodes = sorted((p.name for p in (root / "sys/class/video4linux").iterdir() if p.name.startswith("video")),
                       key=_node_number)
    except OSError:
        return []
    out = []
    for node in nodes:
        cap = querycap(str(root / "dev" / node))
        if not cap or not cap["caps"] & V4L2_CAP_VIDEO_CAPTURE or cap["caps"] & V4L2_CAP_IO_MC:
            continue
        paths = links.get(node, []) + [f"/dev/{node}"]
        name = cap.get("card") or diag_attr(root / "sys/class/video4linux" / node, "name") or node
        # uvcvideo often names a laptop camera "Integrated Camera: Integrated C"
        # (product, then its own copy cut at 32 bytes): say it once.
        head, _, tail = name.partition(": ")
        if tail and head.startswith(tail):
            name = head
        out.append({"id": paths[0], "node": f"/dev/{node}", "paths": paths, "name": name,
                    "bus": cap.get("bus") or "", "driver": cap.get("driver") or ""})
    return out


def camera_signature(root: Path = Path("/")) -> tuple:
    """What changes when a camera comes or goes: the nodes, where each sits,
    and the udev links. Directory reads only, so the 1 s status poll costs
    no ioctl - and no USB wake-up of an idle camera - until something moves."""
    sig = []
    for d in ("sys/class/video4linux", "dev/v4l/by-id", "dev/v4l/by-path"):
        try:
            sig.append(tuple(sorted((p.name, os.readlink(p) if p.is_symlink() else "") for p in (root / d).iterdir())))
        except OSError:
            sig.append(())
    return tuple(sig)


def ipu_camera(root: Path = Path("/")) -> str | None:
    """'IPU6' or 'IPU7' on a laptop whose built-in camera is behind Intel's
    image processing unit, else None."""
    gen = None
    try:
        for dev in (root / "sys/bus/pci/devices").iterdir():
            if diag_attr(dev, "vendor").lower() == "0x8086":
                gen = IPU_PCI.get(diag_attr(dev, "device").lower().removeprefix("0x"))
                if gen:
                    break
    except OSError:
        return None
    if not gen:
        return None
    try:
        sensor = any(p.name.upper().startswith(IPU_SENSOR_ACPI) for p in (root / "sys/bus/acpi/devices").iterdir())
    except OSError:
        sensor = False
    laptop = diag_attr(root / "sys/class/dmi/id", "chassis_type") in PORTABLE_CHASSIS
    return gen if sensor or laptop else None


def _pcm_number(p: Path) -> int:
    digits = re.sub(r"\D", "", p.name)
    return int(digits) if digits else 0


def sound_inputs(root: Path = Path("/")) -> list[dict]:
    """ALSA cards that can record, from /proc/asound: what arecord -l lists,
    without a subprocess on every status poll. device is plughw - ffmpeg asks
    for 48 kHz stereo whatever the card's own layout."""
    try:
        listing = (root / "proc/asound/cards").read_text()
    except OSError:
        return []
    found = []
    for num, ident, driver, short in re.findall(r"(?m)^\s*(\d+) \[(\S+)\s*\]: (\S+) - (.*)$", listing):
        card = root / "proc/asound" / f"card{num}"
        pcms = []
        for pcm in sorted(card.glob("pcm*c"), key=_pcm_number):
            m = re.search(r"(?m)^id: (.*)$", diag_attr(pcm, "info"))
            pcms.append((_pcm_number(pcm), m.group(1).strip() if m else ""))
        if not pcms:
            continue    # HDMI sound and other playback-only cards
        usb = (card / "usbid").exists()
        dev = pcms[0][0]
        if not usb:
            # Laptops with Intel SOF sound put the built-in microphone array
            # on a PCM of its own ("DMIC Raw", usually 6); PCM 0 is then the
            # headset jack, which records silence with nothing plugged in.
            dev = next((n for n, pid in pcms if "dmic" in pid.lower()), dev)
        found.append({"id": ident, "card": int(num), "usb": usb, "driver": driver,
                      "device": f"plughw:CARD={ident}" + (f",DEV={dev}" if dev else ""),
                      "name": short.strip() if usb else "Built-in microphone"})
    builtin = [c for c in found if not c["usb"]]
    if len(builtin) > 1:
        for c in builtin:
            c["name"] = f"Built-in microphone ({c['id']})"
    return found


def camera_choice(cfg: dict, cams: list[dict], ipu: str | None = None) -> dict:
    """The camera the camera source captures. A camera chosen in the picker
    (videoDeviceName set) that is not plugged in is reported, never swapped
    for another: a different picture would go on air from the one chosen -
    the laptop's own face instead of the ATEM - with nothing to say so (the
    rule media_file keeps for films). A config the picker never touched
    (/dev/video0 from before it) takes the only camera there is."""
    saved = str(cfg.get("videoDevice") or "")
    name = str(cfg.get("videoDeviceName") or "")
    hit = next((c for c in cams if saved in c["paths"]), None)
    if hit:
        return {"camera": hit, "device": saved, "problem": "", "auto": False}
    if not name and len(cams) == 1:
        return {"camera": cams[0], "device": cams[0]["id"], "problem": "", "auto": True}
    if name:
        problem = f"Camera not found: {name} — plugged in?"
    elif not cams:
        problem = ipu_camera_text(ipu) if ipu else "No camera or capture device found: plug one in"
    else:
        problem = "Choose the camera to send"
    return {"camera": None, "device": None, "problem": problem, "auto": False}


def camera_sound(cfg: dict, device: str | None, cards: list[dict], paired: str | None = None) -> dict:
    """The camera source's sound: device is an ALSA name, or None for
    silence. paired is the card on the camera's own USB device."""
    want = str(cfg.get("audioDevice") or "auto")
    if want == "none":
        return {"device": None, "problem": ""}
    if want == "auto":
        # Before the picker this fell back to ALSA "default", which is the
        # onboard codec: silence on every mini PC tried, and on a box with
        # no sound card at all ffmpeg could not open it and keying failed.
        if paired:
            return {"device": paired, "problem": ""}
        return {"device": None, "problem": CAMERA_SILENT_NOTE if device else ""}
    m = re.match(r"(?:plug)?hw:CARD=([^,]+)", want)
    if m and not any(c["id"] == m.group(1) for c in cards):
        name = str(cfg.get("audioDeviceName") or "") or m.group(1)
        return {"device": None, "problem": f"Sound not found: {name} — sending silence"}
    return {"device": want, "problem": ""}
