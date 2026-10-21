"""What every controller driver is, and the drawing of a key screen.

A driver knows one model of hardware and nothing about OpenHamShack: it says
what the model looks like (its controls and where they sit, for the
console's drawing and for checking a mapping), finds the devices that are
that model, turns the device's raw reports into events, and draws a face on
a screen key when it has screens. Which action a control runs is pad.py's
business, so a new model never touches the transmit logic.

An event is (kind, control id, step): kind is 'press', 'release' or 'turn';
step is the detents turned (+ clockwise) and 0 otherwise. The tuple shape is
the one the AKP03E's decoder has always returned, kept so a driver is
tested the same way whichever model it is.
"""

from __future__ import annotations

import subprocess

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Kinds of control. A screen key is a key with a picture on it; a knob turns
# and is pressed; a button is a key with no picture (on the AKP03E, the three
# small round ones), drawn smaller in the console so the picture matches the
# thing in the operator's hand.
KINDS = ("screen-key", "key", "button", "knob")


def control(cid: str, kind: str, row: int, col: int, label: str = "", w: int = 1, h: int = 1) -> dict:
    """One control in a driver's layout. row/col are grid cells for the
    console's drawing, w/h how many it spans (a keypad's Enter is two tall);
    label is what the console writes on it."""
    assert kind in KINDS, kind
    return {"id": cid, "kind": kind, "row": row, "col": col, "w": w, "h": h, "label": label or cid}


def slots(kind: str) -> tuple[str, ...]:
    """What can be mapped on a control of this kind: everything presses,
    only a knob also turns."""
    return ("press", "turn") if kind == "knob" else ("press",)


class Driver:
    """A model of controller. Subclass, fill in the class attributes, and
    implement find/open/read (and show/set_brightness when it has screens);
    then add it to DRIVERS in controllers/__init__.py."""

    model = ""                     # stable id: settings are stored under it
    name = ""                      # what the console calls it
    usb: list[tuple[str, str]] = []   # (vendor, product), lower-case hex
    controls: list[dict] = []
    screen: tuple[int, int] | None = None   # a screen key's pixels, if it has any
    # The mapping a new box starts with: {control: {"press": action, "turn": action}}.
    default_map: dict[str, dict[str, str]] = {}
    # Screen keys that show the margin and the peak while aiming, in that
    # order; pad.py skips any that hold transmit or the aim key itself.
    aim_overlay: list[str] = []
    # Any key code the hardware may send counts as a control even when it
    # is not drawn (a generic keypad sends whatever its maker chose).
    open_layout = False

    path = ""

    @classmethod
    def find(cls, cfg: dict) -> list[str]:
        """Device paths of this model that are plugged in and usable now.
        cfg is the daemon's settings, for drivers that only take devices
        the operator has chosen."""
        return []

    @classmethod
    def has_control(cls, cid: str) -> bool:
        return any(c["id"] == cid for c in cls.controls)

    @classmethod
    def control_kind(cls, cid: str) -> str | None:
        for c in cls.controls:
            if c["id"] == cid:
                return c["kind"]
        return None

    @classmethod
    def describe(cls) -> dict:
        """The model as the console draws it."""
        return {"model": cls.model, "name": cls.name, "controls": cls.controls,
                "screen": list(cls.screen) if cls.screen else None, "openLayout": cls.open_layout}

    # A device of this model ---------------------------------------------------
    def start(self, brightness: int) -> None:
        """Whatever the device needs before it sends events (the AKP03E is
        silent until woken)."""

    def read(self, timeout: float) -> list[tuple[str, str, int]]:
        """Events from the device, waiting at most timeout seconds. OSError
        when it has gone (unplugged): the caller then looks for it again."""
        raise NotImplementedError

    def show(self, cid: str, face: tuple) -> None:
        """Draw a face, (line1, line2, bg[, big]), on a screen key."""

    def set_brightness(self, level: int) -> None:
        pass

    def close(self) -> None:
        pass


# --- Key screens ---------------------------------------------------------------
# Bounded: the fixed faces are a few dozen images, but aiming draws a new
# number every few tenths of a dB and would otherwise grow it for as long
# as the box runs. Oldest first out (dicts keep insertion order).
_jpeg_cache: dict[tuple, bytes] = {}
JPEG_CACHE_MAX = 400


def key_image(line1: str, line2: str = "", bg: str = "0x1f2937", fg: str = "white", big: bool = False,
              size: int = 64, turn: str = "transpose=1") -> bytes:
    """A size x size JPEG for a key screen, drawn upright and then put
    through turn (an ffmpeg filter for how the model mounts its screens; ''
    for none). big: line1 as a large number with line2 as a small caption.
    The font sizes were judged on the AKP03E's 64 px keys and scale with
    size, so a bigger screen gets the same layout, not smaller text."""
    k = (line1, line2, bg, fg, big, size, turn)
    if k in _jpeg_cache:
        return _jpeg_cache[k]

    def esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "").replace("%", "\\%")

    def px(n: float) -> int:
        return round(n * size / 64)
    if big:
        fs = 26 if len(line1) <= 3 else 22 if len(line1) <= 4 else 17
        draw = (f"drawtext=fontfile={FONT}:text='{esc(line1)}':fontsize={px(fs)}:fontcolor={fg}:x=(w-tw)/2:y=({px(40)}-th)/2+{px(2)},"
                f"drawtext=fontfile={FONT}:text='{esc(line2)}':fontsize={px(12)}:fontcolor={fg}:x=(w-tw)/2:y={px(44)}")
    elif line2:
        draw = (f"drawtext=fontfile={FONT}:text='{esc(line1)}':fontsize={px(15)}:fontcolor={fg}:x=(w-tw)/2:y={px(14)},"
                f"drawtext=fontfile={FONT}:text='{esc(line2)}':fontsize={px(13)}:fontcolor={fg}:x=(w-tw)/2:y={px(36)}")
    else:
        fs = 20 if len(line1) <= 4 else 15 if len(line1) <= 6 else 12
        draw = f"drawtext=fontfile={FONT}:text='{esc(line1)}':fontsize={px(fs)}:fontcolor={fg}:x=(w-tw)/2:y=(h-th)/2"
    vf = f"{draw},{turn}" if turn else draw
    jpeg = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={bg}:s={size}x{size}", "-vf",
                           vf, "-frames:v", "1", "-q:v", "3", "-f", "mjpeg", "-"],
                          capture_output=True, timeout=10).stdout
    if jpeg:
        if len(_jpeg_cache) >= JPEG_CACHE_MAX:
            del _jpeg_cache[next(iter(_jpeg_cache))]
        _jpeg_cache[k] = jpeg
    return jpeg
