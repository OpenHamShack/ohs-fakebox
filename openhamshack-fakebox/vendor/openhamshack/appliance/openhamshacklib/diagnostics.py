"""The diagnostics probes and the report they make: what the PC is (DMI,
CPU, PCI and USB with their drivers, video inputs, VA-API, sound,
screens, network), what the kernel and journal said, and the box's own
state, as plain-English problems plus a plain-text report with every
secret scrubbed.

Each probe takes its inputs - a filesystem root for /sys, /proc and /dev,
a runner for commands, the daemon's state as a dict - so the tests build a
machine out of files. openhamshack.py's Diagnostics section supplies the real
ones (diag_collect), caches the result and saves or sends the report; its
opening comment has the decisions (journal access through a group, not
dmesg; one failed probe never costs the report).
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from pathlib import Path

from .camera import _node_number, camera_list, ipu_camera, ipu_camera_text
from .settings import SECRET_KEYS
from .sysfs import diag_attr, diag_read


DIAG_SECTION_MAX = 256 * 1024       # one log section, before the report is assembled
DIAG_JOURNAL_LINES = 300
# Below either of these the root disk is "nearly full": recordings, uploads
# and the update's staging copy all start failing in ways that look unrelated.
DIAG_DISK_LOW = 2 * 1024 ** 3
DIAG_DISK_CRITICAL = 512 * 1024 ** 2
# A word in a setting's name that makes its value a secret. Matched as whole
# words of the camelCase name, so keyframeSeconds is kept and youtubeKey is not.
DIAG_SECRET_WORDS = {"key", "password", "passwd", "passphrase", "secret", "pin", "token", "psk"}
# PCI classes (top 16 bits of the class code) worth naming, and the ones
# that matter if no driver has taken them: a network, display, sound, USB or
# storage controller without a driver is a box missing that whole function.
# Bridges, SMBus and "system peripherals" commonly have none and are fine.
PCI_CLASSES = {
    0x0100: "SCSI controller", 0x0101: "IDE controller", 0x0104: "RAID controller", 0x0106: "SATA controller",
    0x0108: "NVMe controller", 0x0200: "Ethernet controller", 0x0280: "Network controller",
    0x0300: "VGA controller", 0x0302: "3D controller", 0x0380: "Display controller",
    0x0400: "Video device", 0x0401: "Audio device", 0x0403: "Audio device", 0x0480: "Multimedia controller",
    0x0500: "RAM memory", 0x0580: "Memory controller", 0x0600: "Host bridge", 0x0601: "ISA bridge",
    0x0604: "PCI bridge", 0x0680: "Bridge", 0x0780: "Communication controller", 0x0805: "SD host controller",
    0x0880: "System peripheral", 0x0c03: "USB controller", 0x0c05: "SMBus", 0x0c80: "Serial bus controller",
    0x0d80: "Wireless controller", 0x1180: "Signal processing controller",
}
PCI_NEEDS_DRIVER = (0x01, 0x02, 0x03, 0x04, 0x0c03, 0x0d)

# What OpenHamShack looks for on USB, by vendor:product.
USB_KIT = {
    ("0403", "6010"): "FTDI FT2232H (MiniTiouner)",
    ("2e8a", "ba2c"): "PicoTuner (USB firmware)",
    ("2e8a", "000a"): "Raspberry Pi Pico serial (PicoTuner, Ethernet firmware)",
    ("2e8a", "0003"): "Raspberry Pi Pico in BOOTSEL (RP2040)",
    ("2e8a", "000f"): "Raspberry Pi Pico in BOOTSEL (RP2350)",
    ("0456", "b673"): "ADALM-Pluto",
    ("0300", "3002"): "Ajazz AKP03E key pad",
}

# Kernel lines that mean a driver could not start for want of firmware, and
# the Debian package that carries each family (a hint for whoever reads the
# report; the package list is in mkimage.sh).
FIRMWARE_RE = [re.compile(r"firmware: failed to load (\S+)"),
               re.compile(r"Direct firmware load for (\S+) failed")]
# Asked for by cfg80211 on every box, Wi-Fi or not; listed here, and offered
# as a problem only by the Wi-Fi section, on a box that has Wi-Fi.
FIRMWARE_HARMLESS = ("regulatory.db",)
FIRMWARE_PACKAGES = (("iwlwifi", "firmware-iwlwifi"), ("i915/", "firmware-intel-graphics"),
                     ("xe/", "firmware-intel-graphics"), ("intel/sof", "firmware-sof-signed"),
                     ("intel/ibt", "firmware-iwlwifi"), ("rtl", "firmware-realtek"), ("rtw", "firmware-realtek"),
                     ("ath", "firmware-atheros"), ("qca/", "firmware-atheros"), ("brcm/", "firmware-brcm80211"),
                     ("mediatek/", "firmware-mediatek"), ("mt7", "firmware-mediatek"), ("amdgpu/", "firmware-amd-graphics"))
# The GPU's microcontrollers: on the N100/N150 (Alder Lake-N) the encoder is
# the low-power VDEnc path, which does not run without HuC.
GPU_FW_RE = re.compile(r"\b(i915|xe)\b.*\b(GuC|HuC|DMC|GSC)\b.*(fail|error|not found|missing)", re.I)
SOF_RE = re.compile(r"\b(sof|snd_sof)\S*.*(probe failed|failed to load|error)", re.I)
KERNEL_NOTABLE_RE = re.compile(r"fail|error|firmware|timed? ?out|not found|unable|missing", re.I)
# What journalctl is asked for: every line any of the above could match.
KERNEL_GREP = "firmware|fail|error|timed? ?out|not found|unable|missing|guc|huc|dmc|sof"


class DiagReport:
    """One collection: problems for the console, sections for the report."""

    LEVELS = ("error", "warn", "info")

    def __init__(self) -> None:
        self.sections: list[tuple[str, str]] = []
        self.problems: list[dict] = []
        self.summary: dict = {}

    def problem(self, level: str, text: str) -> None:
        if not any(p["text"] == text for p in self.problems):
            self.problems.append({"level": level, "text": text})

    def section(self, title: str, fn) -> None:
        """fn() returns the section's text. Whatever it raises becomes the
        text instead, so one probe's failure is one line of the report."""
        try:
            body = fn()
        except subprocess.TimeoutExpired as exc:
            body = f"could not read {title.lower()}: timed out after {exc.timeout:g} s"
        except Exception as exc:
            body = f"could not read {title.lower()}: {diag_why(exc)}"
        self.sections.append((title, str(body if body is not None else "").rstrip() or "(nothing)"))

    def sorted_problems(self) -> list[dict]:
        return sorted(self.problems, key=lambda p: self.LEVELS.index(p["level"]))


def diag_why(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError) and exc.filename:
        return f"{exc.filename} not found"
    if isinstance(exc, PermissionError) and exc.filename:
        return "permission denied"
    return (str(exc) or type(exc).__name__)[:200]


def diag_tail(text: str, max_bytes: int = DIAG_SECTION_MAX) -> str:
    """The end of a log, whole lines, no more than max_bytes."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    cut = raw[-max_bytes:].decode("utf-8", "ignore")
    return "[... earlier lines left out]\n" + cut.split("\n", 1)[-1]


def secret_setting(name: str) -> bool:
    if name in SECRET_KEYS:
        return True
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return any(w.lower() in DIAG_SECRET_WORDS for w in re.split(r"[^A-Za-z0-9]+", words) if w)


def diag_settings(obj):
    """Settings with every secret's value replaced by whether it is set."""
    if isinstance(obj, dict):
        return {k: (("[set]" if v not in ("", None, False, 0) else "[not set]") if secret_setting(k)
                    else diag_settings(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [diag_settings(v) for v in obj]
    return obj


def diag_secret_values(obj, out: set | None = None) -> set[str]:
    """Every secret value in the settings, to scrub from logs that might have
    echoed one (ffmpeg's errors quote the URL a YouTube key is part of)."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if secret_setting(k) and isinstance(v, (str, int)) and not isinstance(v, bool) and len(str(v)) >= 4:
                out.add(str(v))
            else:
                diag_secret_values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            diag_secret_values(v, out)
    return out


def diag_scrub(text: str, secrets: set[str]) -> str:
    for s in sorted(secrets, key=len, reverse=True):
        text = text.replace(s, "[removed]")
    return text


def gib(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


# Each probe below adds its section (and any problems) to the report.
def diag_system(rep: DiagReport, root: Path, live: dict) -> None:
    def text() -> str:
        lines = [f"OpenHamShack version: {live.get('version') or 'unknown'}"]
        for label, rel in (("Kernel", "proc/sys/kernel/osrelease"), ("Debian", "etc/debian_version")):
            try:
                lines.append(f"{label}: {diag_read(root, rel)}")
            except OSError as exc:
                lines.append(f"{label}: could not read: {diag_why(exc)}")
        try:
            pretty = re.search(r'^PRETTY_NAME="?([^"\n]*)', diag_read(root, "etc/os-release"), re.M)
            lines.append(f"Release: {pretty.group(1) if pretty else 'unknown'}")
        except OSError as exc:
            lines.append(f"Release: could not read: {diag_why(exc)}")
        try:
            up = float(diag_read(root, "proc/uptime").split()[0])
            lines.append(f"Uptime: {int(up // 86400)} d {int(up % 86400 // 3600)} h {int(up % 3600 // 60)} min")
        except (OSError, ValueError, IndexError) as exc:
            lines.append(f"Uptime: could not read: {diag_why(exc)}")
        try:
            info = diag_read(root, "proc/cpuinfo", 1 << 20)
            models = re.findall(r"^model name\s*:\s*(.+)$", info, re.M)
            lines.append(f"CPU: {models[0] if models else 'unknown'} ({len(models) or '?'} threads)")
            rep.summary["cpu"] = models[0] if models else None
        except OSError as exc:
            lines.append(f"CPU: could not read: {diag_why(exc)}")
        try:
            mem = dict(re.findall(r"^(\w+):\s+(\d+) kB", diag_read(root, "proc/meminfo"), re.M))
            lines.append(f"Memory: {gib(int(mem['MemTotal']) * 1024)} total, "
                         f"{gib(int(mem.get('MemAvailable', 0)) * 1024)} available")
        except (OSError, KeyError, ValueError) as exc:
            lines.append(f"Memory: could not read: {diag_why(exc)}")
        dmi = {k: diag_attr(root / "sys/class/dmi/id", k)
               for k in ("sys_vendor", "product_name", "product_version", "board_name", "bios_version")}
        machine = " ".join(v for v in (dmi["sys_vendor"], dmi["product_version"], dmi["product_name"]) if v)
        rep.summary["machine"] = machine or None
        lines.append(f"Machine: {machine or 'unknown'}")
        lines.append(f"Board: {dmi['board_name'] or 'unknown'}, BIOS {dmi['bios_version'] or 'unknown'}")
        for where, use in (live.get("disks") or {}).items():
            if isinstance(use, str):
                lines.append(f"Disk {where}: could not read: {use}")
                continue
            total, free = use
            lines.append(f"Disk {where}: {gib(free)} free of {gib(total)}")
            if where == "/" and total:
                if free < DIAG_DISK_CRITICAL:
                    rep.problem("error", f"Disk nearly full: {gib(free)} left")
                elif free < DIAG_DISK_LOW or free / total < 0.05:
                    rep.problem("warn", f"Disk nearly full: {gib(free)} left")
        return "\n".join(lines)
    rep.section("System", text)


def diag_pci(rep: DiagReport, root: Path, run) -> None:
    def text() -> str:
        base = root / "sys/bus/pci/devices"
        lines = []
        for dev in sorted(base.iterdir()):
            vendor = diag_attr(dev, "vendor").removeprefix("0x")
            device = diag_attr(dev, "device").removeprefix("0x")
            try:
                cls = int(diag_attr(dev, "class") or "0", 16) >> 8
            except ValueError:
                cls = 0
            name = PCI_CLASSES.get(cls, f"class {cls:04x}")
            drv = (dev / "driver").resolve().name if (dev / "driver").exists() else None
            lines.append(f"{dev.name} {vendor}:{device} {name}: {drv or 'NO DRIVER'}")
            if drv is None and (cls in PCI_NEEDS_DRIVER or cls >> 8 in PCI_NEEDS_DRIVER):
                rep.problem("warn", f"PCI device with no driver: {vendor}:{device} {name}")
        try:
            lines += ["", "lspci -nnk:", run(["lspci", "-nnk"]).rstrip()]
        except Exception as exc:
            lines += ["", f"lspci: {diag_why(exc)}"]
        return "\n".join(lines)
    rep.section("PCI devices", text)


def diag_usb(rep: DiagReport, root: Path, run, live: dict, cfg: dict) -> None:
    def text() -> str:
        lines, found = [], set()
        for dev in sorted((root / "sys/bus/usb/devices").iterdir()):
            vid, pid = diag_attr(dev, "idVendor").lower(), diag_attr(dev, "idProduct").lower()
            if not vid:
                continue        # an interface, not a device
            kit = USB_KIT.get((vid, pid))
            if kit:
                found.add((vid, pid))
            # No serial numbers: they identify the person's kit and are not
            # needed to say what it is.
            words = " ".join(v for v in (diag_attr(dev, "manufacturer"), diag_attr(dev, "product")) if v)
            lines.append(f"{dev.name} {vid}:{pid} {words or '?'}" + (f"  <- {kit}" if kit else ""))
        try:
            lines += ["", "lsusb:", re.sub(r"(?m)^(.*?) iSerial .*$", r"\1", run(["lsusb"])).rstrip()]
        except Exception as exc:
            lines += ["", f"lsusb: {diag_why(exc)}"]
        radio_iface = [i["name"] for i in live.get("interfaces") or [] if i.get("type") == "usb-radio"]
        kit_lines = [f"OpenHamShack kit:",
                     f"  MiniTiouner: {'found' if ('0403', '6010') in found else 'not found'}",
                     f"  PicoTuner on USB: {'found' if ('2e8a', 'ba2c') in found else 'not found'}",
                     f"  PicoTuner (Ethernet firmware) on USB serial: {'found' if ('2e8a', '000a') in found else 'not found'}",
                     f"  Pico in BOOTSEL: {'yes' if found & {('2e8a', '0003'), ('2e8a', '000f')} else 'no'}",
                     f"  Pluto/LibreSDR on USB (network link): {', '.join(radio_iface) if radio_iface else 'not found'}",
                     f"  Ajazz pad: {'found' if ('0300', '3002') in found else 'not found'}"]
        if found & {("2e8a", "0003"), ("2e8a", "000f")}:
            rep.problem("info", "A Raspberry Pi Pico is in BOOTSEL mode (a PicoTuner waiting for firmware?)")
        tuners = [n for k, n in ((("0403", "6010"), "MiniTiouner"), (("2e8a", "ba2c"), "PicoTuner")) if k in found]
        if cfg.get("rxEnabled", True) and cfg.get("rxTuner") != "picotuner" and not tuners:
            rep.problem("warn", "No tuner found: plug in a MiniTiouner or PicoTuner (USB)")
        rep.summary["tuner"] = ", ".join(tuners) or None
        return "\n".join(kit_lines + [""] + lines)
    rep.section("USB devices", text)


def diag_video_inputs(rep: DiagReport, root: Path, cfg: dict) -> None:
    def text() -> str:
        lines = []
        try:
            nodes = sorted(p.name for p in (root / "dev").iterdir() if p.name.startswith("video"))
        except OSError as exc:
            nodes = []
            lines.append(f"/dev: {diag_why(exc)}")
        cams = camera_list(root)
        capture = {c["node"]: c for c in cams}
        try:
            for dev in sorted((root / "sys/class/video4linux").iterdir(), key=lambda p: _node_number(p.name)):
                cam = capture.get(f"/dev/{dev.name}")
                # Which nodes can be captured from: a UVC camera's second
                # node is metadata only, and IPU6 nodes need a pipeline.
                kind = f"  capture, {cam['bus']}" if cam else ""
                lines.append(f"/dev/{dev.name}: {diag_attr(dev, 'name') or '?'}{kind}")
        except OSError:
            lines += [f"/dev/{n}" for n in nodes]
        if not lines:
            lines.append("No cameras or capture devices")
        lines += [f"{c['id']} -> {c['node']}" for c in cams if c["id"] != c["node"]]
        gen = None if cams else ipu_camera(root)
        if gen:
            lines.append(f"Built-in camera behind Intel {gen}: no V4L2 capture node")
            rep.problem("warn" if cfg.get("source") == "webcam" else "info", ipu_camera_text(gen))
        want = str(cfg.get("videoDevice") or "")
        # A by-id link resolves inside root as it does on the box.
        if cfg.get("source") == "webcam" and want and not (root / want.lstrip("/")).exists():
            rep.problem("warn", f"Picture source {cfg.get('videoDeviceName') or want} is not plugged in")
        return "\n".join(lines)
    rep.section("Cameras and capture", text)


def parse_vainfo(text: str) -> dict:
    """What VA-API says it can encode, from vainfo's listing."""
    enc = set()
    for profile, entry in re.findall(r"(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)", text):
        if entry.startswith("VAEntrypointEncSlice"):
            if profile.startswith("VAProfileHEVCMain"):
                enc.add("hevc")
            elif profile.startswith("VAProfileH264"):
                enc.add("h264")
    drv = re.search(r"Driver version:\s*(.+)", text)
    return {"driver": drv.group(1).strip() if drv else None, "encode": sorted(enc)}


def diag_gpu(rep: DiagReport, root: Path, run, cfg: dict) -> None:
    def text() -> str:
        lines = []
        try:
            nodes = sorted(p.name for p in (root / "dev/dri").iterdir())
        except OSError:
            nodes = []
        lines.append(f"/dev/dri: {', '.join(nodes) if nodes else 'none'}")
        try:
            for card in sorted((root / "sys/class/drm").glob("card[0-9]")):
                drv = (card / "device/driver").resolve().name if (card / "device/driver").exists() else "no driver"
                lines.append(f"{card.name}: {diag_attr(card / 'device', 'vendor')}:{diag_attr(card / 'device', 'device')} {drv}")
        except OSError:
            pass
        rep.summary["encoder"] = None
        if "renderD128" not in nodes:
            rep.problem("error", "No GPU render device (/dev/dri/renderD128): transmit will not work")
            return "\n".join(lines)
        try:
            out = run(["vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"])
        except FileNotFoundError as exc:
            # Every image has vainfo; without it the encoder cannot be asked,
            # which is not the same as there being none.
            rep.problem("info", "Hardware encoder not checked: vainfo is not installed")
            return "\n".join(lines + [f"vainfo: {diag_why(exc)}"])
        except Exception as exc:
            rep.problem("error", "No hardware video encoder (VA-API): transmit will not work")
            return "\n".join(lines + [f"vainfo: {diag_why(exc)}"])
        va = parse_vainfo(out)
        names = " + ".join({"hevc": "H.265", "h264": "H.264"}[e] for e in sorted(va["encode"], reverse=True))
        lines += [f"VA-API driver: {va['driver'] or 'unknown'}", f"Hardware encode: {names or 'none'}",
                  "", out.rstrip()]
        if not va["encode"]:
            rep.problem("error", "No hardware video encoder (VA-API): transmit will not work")
        elif "hevc" not in va["encode"]:
            rep.problem("warn" if cfg.get("codec") == "hevc" else "info",
                        "No H.265 hardware encoder: choose H.264 to transmit")
        rep.summary["encoder"] = names or None
        return "\n".join(lines)
    rep.section("Graphics and video encoder", text)


def vaapi_driver_hint(root: Path) -> str:
    """The fix for a GPU encoder that failed its test encode, where it is
    known: Debian's free Intel media driver, which on Iris Xe offers HEVC in
    CQP only (5 October 2026: "Driver does not support CBR RC mode"). The
    OpenHamShack image ships the non-free one, so this is a desktop install's
    problem; '' when that is not what is installed."""
    if deb_installed(root, "intel-media-va-driver") and not deb_installed(root, "intel-media-va-driver-non-free"):
        return ("Debian's free Intel driver is installed: intel-media-va-driver-non-free (from non-free) "
                "usually lets the graphics encode at a constant bit rate")
    return ""


def diag_encoder(rep: DiagReport, live: dict) -> None:
    """The test encodes the daemon has run (live["encoderProbes"]: codec ->
    {ok, error}) and what transmit uses because of them."""
    def text() -> str:
        probes = live.get("encoderProbes") or {}
        if not probes:
            return "No test encode yet"
        lines = []
        for codec, p in sorted(probes.items()):
            name = {"hevc": "H.265", "h264": "H.264"}.get(codec, codec)
            if p.get("ok"):
                lines.append(f"{name}: graphics encoder passed its test encode")
                continue
            lines.append(f"{name}: graphics encoder failed its test encode ({p.get('error') or 'no reason'}); "
                         "transmitting with the software encoder")
            hint = p.get("hint") or ""
            rep.problem("warn", f"{name} is encoded on the processor: the graphics encoder failed a test encode "
                                f"({p.get('error') or 'no reason'})" + (f". {hint}" if hint else ""))
        return "\n".join(lines)
    rep.section("Video encoder test", text)


def diag_audio(rep: DiagReport, root: Path) -> None:
    def text() -> str:
        cards = diag_read(root, "proc/asound/cards")
        if not cards or "no soundcards" in cards:
            rep.problem("info", "No sound devices found: received sound will be silent")
        return cards or "no soundcards"
    rep.section("Sound devices", text)


def diag_displays(rep: DiagReport, live: dict) -> None:
    def text() -> str:
        outs = live.get("outputs")
        if outs is None:
            rep.problem("warn", "The screen (sway) is not running")
            return "sway is not running"
        lines = []
        for o in outs:
            mode = o.get("current_mode") or {}
            size = f"{mode.get('width')}x{mode.get('height')}@{(mode.get('refresh') or 0) / 1000:g}" if mode else "off"
            words = " ".join(str(o.get(k) or "") for k in ("make", "model")).strip()
            lines.append(f"{o.get('name')}: {words or '?'} {size}{'' if o.get('active') else ' (off)'}")
        return "\n".join(lines) or "no screens"
    rep.section("Screens", text)


def diag_network(rep: DiagReport, live: dict) -> None:
    def text() -> str:
        ifaces = live.get("interfaces")
        if ifaces is None:
            raise RuntimeError("no interface list")
        lines = []
        for i in ifaces:
            if i.get("type") == "tailscale":
                continue        # the tailnet's name is the owner's, not ours to collect
            addrs = ", ".join(f"{a['address']}/{a.get('prefix')}" for a in i.get("ipv4") or [])
            lines.append(f"{i['name']} ({i.get('type')}): {'link up' if i.get('up') else 'no link'}  {addrs or 'no address'}")
        if not any(i.get("ipv4") for i in ifaces if i.get("type") in ("wired", "wifi")):
            rep.problem("warn", "No network connection (wired or Wi-Fi)")
        return "\n".join(lines) or "no interfaces"
    rep.section("Network", text)


def wifi_interfaces(root: Path, live: dict) -> list[str]:
    """Wi-Fi interfaces from sysfs, and any the daemon's list calls Wi-Fi."""
    names = set()
    try:
        for d in (root / "sys/class/net").iterdir():
            if (d / "wireless").exists() or (d / "phy80211").exists():
                names.add(d.name)
    except OSError:
        pass
    ifaces = live.get("interfaces")
    if isinstance(ifaces, list):
        names |= {str(i.get("name")) for i in ifaces if isinstance(i, dict) and i.get("type") == "wifi"}
    return sorted(names)


def rfkill_list(root: Path) -> list[dict]:
    out = []
    try:
        devs = sorted((root / "sys/class/rfkill").iterdir(), key=lambda p: int(re.sub(r"\D", "", p.name) or 1 << 30))
    except OSError:
        return out
    for d in devs:
        out.append({"id": d.name, "type": diag_attr(d, "type"), "name": diag_attr(d, "name"),
                    "soft": diag_attr(d, "soft") == "1", "hard": diag_attr(d, "hard") == "1"})
    return out


def deb_installed(root: Path, package: str) -> bool | None:
    """From dpkg's status file, so the tests need no dpkg. None when it
    cannot be read."""
    try:
        status = (root / "var/lib/dpkg/status").read_text(errors="replace")
    except OSError:
        return None
    for para in status.split("\n\n"):
        if re.search(rf"^Package: {re.escape(package)}$", para, re.M):
            return bool(re.search(r"^Status: install ok installed$", para, re.M))
    return False


def regdb_present(root: Path) -> bool:
    # wireless-regdb installs it under /usr/lib/firmware by alternatives;
    # /lib is the same place on a merged-/usr system, but not in a test tree.
    return any((root / d / "regulatory.db").exists() for d in ("lib/firmware", "usr/lib/firmware"))


def diag_wifi(rep: DiagReport, root: Path, run, live: dict) -> None:
    """One section per Wi-Fi interface: the rfkill switches, the service,
    what wpa_supplicant says, the regulatory domain. Asked for when Ray's
    Lenovo said "no wifi" and the report had nothing to say why: no switch
    state, no service state, no scan outcome."""
    for iface in wifi_interfaces(root, live):
        rep.section(f"Wi-Fi {iface}", lambda iface=iface: wifi_text(rep, root, run, live, iface))


def wifi_text(rep: DiagReport, root: Path, run, live: dict, iface: str) -> str:
    dev = root / "sys/class/net" / iface
    try:
        phy = (dev / "phy80211").resolve(strict=True).name
    except OSError:
        phy = None
    drv = (dev / "device/driver").resolve().name if (dev / "device/driver").exists() else None
    lines = [f"Interface: {iface}, {phy or 'no phy'}, driver {drv or 'unknown'}"]

    # rfkill: the card's own switch is named after its phy; a laptop's
    # platform driver (ideapad_laptop, thinkpad_acpi...) adds its own of type
    # wlan. Every switch is listed, Bluetooth too, as rfkill(8) would.
    switches = rfkill_list(root)
    own = [s for s in switches if s["type"] == "wlan" and s["name"] == phy]
    platform = [s for s in switches if s["type"] == "wlan" and not re.fullmatch(r"phy\d+", s["name"])]
    lines.append("Switches (rfkill):" if switches else "Switches (rfkill): none")
    for s in switches:
        mark = "  <- this card" if s in own else "  <- the laptop's Wi-Fi switch" if s in platform else ""
        lines.append(f"  {s['id']} {s['type']} {s['name']}: soft {'blocked' if s['soft'] else 'unblocked'}, "
                     f"hard {'blocked' if s['hard'] else 'unblocked'}{mark}")
    if any(s["hard"] for s in own + platform):
        rep.problem("warn", "Wi-Fi is switched off (hard block: the laptop's switch or Fn key, or the BIOS)")
        if not any(s["hard"] for s in own):
            # ideapad_laptop is known to report a hard block on IdeaPad-family
            # Lenovos that is not there; the card's own state is the truth.
            lines.append("  Only the laptop's switch says hard blocked, not the card: on some Lenovos "
                         "(ideapad_laptop) that report is false")
    if any(s["soft"] for s in own + platform):
        rep.problem("warn", "Wi-Fi switched off in software")

    unit = f"wpa_supplicant@{iface}.service"
    try:
        active = run(["systemctl", "is-active", unit]).strip() or "unknown"
    except Exception as exc:
        # is-active exits non-zero for anything but active and says which.
        active = diag_why(exc)
    lines.append(f"{unit}: {active}")

    # wpa_cli as the service's account: a socket made with GROUP=netdev
    # answers; the console's default socket is root's and does not, in which
    # case what openhamshack-net's wifi-list said for the console stands in.
    try:
        status = dict(l.split("=", 1) for l in run(["wpa_cli", "-i", iface, "status"]).splitlines() if "=" in l)
        lines.append("wpa_cli status: " + ", ".join(f"{k}={status.get(k, '-')}" for k in ("wpa_state", "ssid", "freq")))
    except Exception as exc:
        lines.append(f"wpa_cli status: not available to the OpenHamShack service ({diag_why(exc)})")
        seen = (live.get("wifi") or {}).get(iface) if isinstance(live.get("wifi"), dict) else None
        if isinstance(seen, dict):
            st = seen.get("state") or {}
            lines.append("openhamshack-net wifi-list: " + (f"error: {seen['error']}" if seen.get("error") else
                         ", ".join(f"{k}={st.get(k, '-')}" for k in ("wpa_state", "ssid", "freq"))
                         + f", {len(seen.get('saved') or [])} saved network(s)"
                         + ("" if seen.get("running") is None else
                            f", service {'running' if seen.get('running') else 'not running'}")))

    try:
        reg = run(["iw", "reg", "get"])
        lines.append("Regulatory domain (iw reg get):")
        lines += [f"  {l.strip()}" for l in reg.splitlines() if l.strip().startswith(("global", "country", "phy#"))]
    except Exception as exc:
        lines.append(f"Regulatory domain: {diag_why(exc)}")
    pkg = deb_installed(root, "wireless-regdb")
    have_db = regdb_present(root)
    lines.append(f"regulatory.db: {'present' if have_db else 'missing'}; wireless-regdb "
                 + {True: "installed", False: "not installed", None: "unknown (dpkg status unreadable)"}[pkg])
    if not have_db:
        # Without it cfg80211 stays on the world domain, where most 5 GHz
        # channels are passive-scan only. OS updates install the package.
        rep.problem("info", "regulatory.db missing: some 5 GHz networks may not be seen (package wireless-regdb)")

    lines += ["", f"{unit} log (this boot, last 30 lines):"]
    if not live.get("journalAccess"):
        lines.append("not readable yet: the OpenHamShack service is not in the systemd-journal group")
    else:
        try:
            lines.append(diag_tail(run(["journalctl", "-u", unit, "-b", "-n", "30", "--no-pager", "-o", "short-iso"])).rstrip()
                         or "(none)")
        except RuntimeError as exc:
            lines.append("(no entries this boot)" if "no entries" in str(exc).lower() else diag_why(exc))
        except Exception as exc:
            lines.append(diag_why(exc))
    return "\n".join(lines)


def diag_openhamshack(rep: DiagReport, live: dict, cfg: dict) -> None:
    def text() -> str:
        host = str(cfg.get("host") or "")
        radio = {"pluto": "Pluto", "libresdr": "LibreSDR"}.get(str(cfg.get("radio")), str(cfg.get("radio")))
        reach = live.get("radioReachable")
        rep.summary["radio"] = f"{radio} at {host}: " + {True: "reachable", False: "not reachable", None: "not checked"}[reach]
        # A LibreSDR says itself whether it is radiating (requests.php?onair);
        # None when it was not asked or did not answer as DL1JM's firmware.
        on = live.get("radioOnAir")
        if on is not None:
            rep.summary["radio"] += ", radio on air" if on else ", radio standby"
        if on and not live.get("txKeyed"):
            rep.problem("error", f"The {radio} at {host} says it is on air, and OpenHamShack is not transmitting")
        if live.get("radioStuck"):
            rep.problem("error", str(live["radioStuck"])[:200])
        if reach is False:
            rep.problem("warn", f"Radio not reachable at {host}")
        lines = [f"Radio: {rep.summary['radio']}",
                 f"Tuner setting: {cfg.get('rxTuner')}, receive {'on' if cfg.get('rxEnabled', True) else 'off'}"]
        for r in live.get("receivers") or []:
            lines.append(f"Receiver {r.get('id')}: {r.get('tuner')} - {r.get('label')}"
                         + (f", MER {r['mer']} dB" if r.get("mer") is not None else ""))
        if live.get("txError"):
            lines.append(f"Last transmit error: {live['txError']}")
            rep.problem("warn", f"Transmit stopped: {live['txError']}"[:200])
        if live.get("encoderLog"):
            lines += ["Encoder log (last lines):", live["encoderLog"]]
        job = live.get("updateJob") or {}
        lines.append(f"Update job: {job.get('kind') or '-'} {job.get('phase') or 'idle'} {job.get('message') or ''}".rstrip())
        if job.get("phase") == "failed":
            rep.problem("warn", f"Last update failed: {job.get('message') or 'no reason given'}"[:200])
        if live.get("updateApplied"):
            lines.append(f"Last update applied: {json.dumps(live['updateApplied'])}")
        for k in ("stream", "youtube"):
            if live.get(k):
                lines.append(f"{k.capitalize()}: {live[k].get('state')} {live[k].get('message') or ''}".rstrip())
        if live.get("pad"):
            lines.append(f"Key pad: {'connected' if live['pad'].get('connected') else 'not connected'}")
        lines.append("Storage: " + (", ".join(f"{d['label']} ({gib(d['free'])} free)" if d.get("free") is not None
                                              else d["label"] for d in live.get("storage") or []) or "none"))
        if live.get("liveMode"):
            lines.append("Running from the USB stick (Try OpenHamShack): nothing is saved to this PC")
        if live.get("osUpdates"):
            lines.append(os_updates_line(live["osUpdates"]))
            if live["osUpdates"].get("packages"):
                lines.append("OS packages last updated: " + ", ".join(live["osUpdates"]["packages"]))
            if live["osUpdates"].get("restartNeeded"):
                rep.problem("info", "Security updates installed: restart when convenient")
            if live["osUpdates"].get("result") == "failed":
                rep.problem("warn", f"Security updates failed: {live['osUpdates'].get('message') or 'no reason given'}"[:200])
        return "\n".join(lines)
    rep.section("OpenHamShack", text)


def firmware_present(root: Path, name: str) -> bool:
    """Whether a firmware the kernel failed to load is installed after all,
    or an older version of it is. Drivers ask for the newest version they
    know first and fall back: iwlwifi logs a failed load of -89.ucode on
    every boot of a box that then runs happily on -86. Counting that as
    missing would send a tester after a problem they do not have."""
    fw = root / "lib/firmware" / name
    if any(fw.with_name(fw.name + ext).exists() for ext in ("", ".zst", ".xz")):
        return True
    m = re.fullmatch(r"(.+?[-_])\d+(\.\w+)", fw.name)
    if not m:
        return False
    try:
        return any(p.name.startswith(m.group(1)) for p in fw.parent.iterdir())
    except OSError:
        return False


def diag_kernel(rep: DiagReport, root: Path, run, live: dict) -> None:
    def text() -> str:
        if not live.get("journalAccess"):
            rep.problem("info", "The system log cannot be read yet: it can after the next OpenHamShack update")
            raise PermissionError("the OpenHamShack service is not in the systemd-journal group yet "
                                  "(it is after the next OpenHamShack update)")
        # Firmware is loaded in the first seconds of a boot, so the last N
        # lines miss it on a box that has been up a while (shack's kernel log
        # was 43,000 lines and 9 MB after nine days). journalctl filters the
        # whole boot itself; one built without pattern support says so and
        # gets the first lines of the boot instead.
        base = ["journalctl", "-k", "-b", "--no-pager", "-o", "short-monotonic"]
        try:
            log = run(base + ["--case-sensitive=false", "--grep", KERNEL_GREP])
        except RuntimeError as exc:
            # Nothing matching is exit status 1 with "-- No entries --".
            log = "" if "no entries" in str(exc).lower() else "\n".join(run(base).splitlines()[:5000])
        missing: dict[str, str] = {}
        notable = []
        for line in log.splitlines():
            for rx in FIRMWARE_RE:
                m = rx.search(line)
                if m and m.group(1):
                    missing.setdefault(m.group(1), line)
            if GPU_FW_RE.search(line):
                rep.problem("error", "Graphics firmware (GuC/HuC) failed to load: the hardware encoder may not work")
            if SOF_RE.search(line):
                rep.problem("warn", "Sound driver (SOF) failed to start: no sound from the built-in audio")
            if KERNEL_NOTABLE_RE.search(line):
                notable.append(line)
        lines = []
        for name in missing:
            if firmware_present(root, name):
                lines.append(f"Firmware asked for and not loaded, but an older version is installed (normal): {name}")
                continue
            pkg = next((p for prefix, p in FIRMWARE_PACKAGES if name.startswith(prefix)), None)
            if not name.startswith(FIRMWARE_HARMLESS):
                rep.problem("warn", f"Firmware missing: {name}")
            lines.append(f"Missing firmware: {name}" + (f" (Debian package {pkg})" if pkg else ""))
        return "\n".join(lines + ["", "Kernel lines mentioning failures this boot:",
                                  diag_tail("\n".join(notable[-400:])) or "(none)"])
    rep.section("Kernel and firmware", text)


def diag_journals(rep: DiagReport, run, live: dict) -> None:
    def journal(args: list[str]):
        def text() -> str:
            if not live.get("journalAccess"):
                raise PermissionError("the OpenHamShack service is not in the systemd-journal group yet "
                                      "(it is after the next OpenHamShack update)")
            return diag_tail(run(["journalctl", *args, "-b", "--no-pager", "-o", "short-iso"]))
        return text
    rep.section("OpenHamShack service log (this boot)", journal(["-u", "openhamshack.service", "-n", str(DIAG_JOURNAL_LINES)]))
    rep.section("System errors (this boot)", journal(["-p", "err", "-n", "200"]))


# Sections trimmed first when a report is too big to send: the logs, which
# are long and old at the top, before anything describing the machine.
DIAG_LOG_SECTIONS = ("OpenHamShack service log (this boot)", "System errors (this boot)", "Kernel and firmware",
                     "OpenHamShack service log (before the restart)")


def diag_text(rep: DiagReport, cfg: dict, when: float | None = None, limit: int | None = None,
              preface: list[str] | None = None, extra: list[tuple[str, str]] | None = None) -> str:
    """The report as one plain-text file, every secret value scrubbed out.
    preface (report_preface: the tester's note, or what set off an automatic
    report) goes above everything else, where whoever opens it looks first;
    it is scrubbed like the rest, since a tester may paste a key into it.
    extra: sections of this report only (an automatic report's own log),
    after the shared collection's."""
    when = time.time() if when is None else when
    head = list(preface or []) + [f"OpenHamShack diagnostic report - {cfg.get('callsign') or 'OPENHAMSHACK'}",
            time.strftime("Made %Y-%m-%d %H:%M:%S UTC", time.gmtime(when)), "", "Problems found:"]
    head += [f"  [{p['level']}] {p['text']}" for p in rep.sorted_problems()] or ["  none"]
    secrets = diag_secret_values(cfg)
    head = [diag_scrub(line, secrets) for line in head]
    sections = [(t, diag_scrub(b, secrets)) for t, b in [*rep.sections, *(extra or [])]]

    def assemble() -> str:
        return "\n".join(head) + "\n\n" + "\n\n".join(f"== {t} ==\n{b}" for t, b in sections) + "\n"
    out = assemble()
    if limit is None:
        return out
    for title in DIAG_LOG_SECTIONS:
        over = len(out.encode()) - limit
        if over <= 0:
            break
        for i, (t, b) in enumerate(sections):
            if t == title:
                sections[i] = (t, diag_tail(b, max(0, len(b.encode()) - over - 64)))
        out = assemble()
    raw = out.encode()
    return out if len(raw) <= limit else raw[:limit - 40].decode("utf-8", "ignore") + "\n[... report cut short]\n"


def os_updates_line(v: dict) -> str:
    """One line for the diagnostics report."""
    if v["live"]:
        return "OS updates: off (trying from the USB stick)"
    if not v["installed"]:
        return "OS updates: not set up on this box"
    def when(t) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if isinstance(t, (int, float)) else "never"
    out = f"OS updates: last run {when(v['lastRun'])} ({v['result'] or 'none yet'}"
    out += f": {v['message']})" if v["message"] else ")"
    if v["updatedAt"]:
        out += f"; {len(v['packages'])} package(s) updated {when(v['updatedAt'])}"
    if v["restartNeeded"]:
        out += "; restart needed"
    return out
