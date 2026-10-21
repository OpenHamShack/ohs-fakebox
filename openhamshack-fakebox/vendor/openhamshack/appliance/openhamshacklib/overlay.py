"""The picture overlay as ffmpeg filter text: escaping a value for the three
parsers it passes, drawtext and the layout's elements checked and placed,
the uploaded images' names and kinds, and the weather in words.

openhamshack.py's Picture overlay section keeps where logos and text files live
(OVERLAY_DIR and OVERLAY_RUN, which tests move), writes the text files and
fetches the weather; overlay_ops there turns a layout into the encoder's
filters with these.
"""

from __future__ import annotations

import re
import struct
import time

from .orbit import maidenhead
from .settings import DEFAULT


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
OVERLAY_TYPES = ("text", "clock", "weather", "image", "ticker")
OVERLAY_MAX = 20
IMAGE_MAX_BYTES = 5 * 1024 * 1024
COLOURS = {"white", "black", "yellow", "red", "green", "blue", "cyan", "magenta", "orange", "grey", "gray"}
BOX = "black@0.55"


def escape_chars(s: str, chars: str) -> str:
    return "".join("\\" + c if c in chars else c for c in s)


# A drawtext value passes three parsers in turn, each undoing one layer of
# backslashes: the filter graph (which splits on , ; [ ]), the option list
# (which splits on :), and for text= the %{...} expansion (which splits its
# arguments on : and ends at }). Quoting with ' was tried by hand first and
# a ':' in a clock format still cut the option short, so every layer is
# escaped mechanically instead, innermost first.
def esc_expand_arg(s: str) -> str:
    # Whitespace too: each layer trims unescaped spaces from the ends.
    return "".join("\\" + c if c in "\\':}" or c.isspace() else c for c in s)


def esc_option(s: str) -> str:
    return escape_chars(s, "\\':")


def esc_graph(s: str) -> str:
    return escape_chars(s, "\\'[],;")


def filter_str(name: str, opts: list[tuple[str, object]]) -> str:
    """One filter, its option values escaped for the option parser and the
    whole for the graph parser."""
    return name + "=" + esc_graph(":".join(f"{k}={esc_option(str(v))}" for k, v in opts))


def ff_colour(c: str) -> str:
    return "0x" + c[1:] if c.startswith("#") else c


def weather_location(cfg: dict) -> tuple[float, float] | None:
    where = maidenhead(cfg.get("locator", ""))
    if where:
        return where
    try:
        lat, lon = float(cfg["latitude"]), float(cfg["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


# WMO weather interpretation codes as Open-Meteo reports them, in words short
# enough for a corner of the picture.
WMO = {0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
       80: "Light showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers",
       95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail"}


def weather_words(code) -> str:
    try:
        return WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def weather_line(current: dict) -> str:
    """'12°C Light rain, wind 9 mph' from Open-Meteo's current block."""
    temp = f"{round(float(current['temperature_2m']))}°C"
    words = weather_words(current.get("weather_code"))
    wind = current.get("wind_speed_10m")
    line = f"{temp} {words}".strip()
    if wind is not None:
        line += f", wind {round(float(wind))} mph"
    return line


def weather_wanted(cfg: dict) -> bool:
    if not cfg.get("callsignOverlay", True):
        return False
    return any(e.get("type") == "weather" or "{weather}" in str(e.get("text", ""))
               for e in overlay_elements(cfg))


def overlay_elements(cfg: dict) -> list[dict]:
    els = cfg.get("overlay")
    return els if isinstance(els, list) else DEFAULT["overlay"]


def clean_element(e, used: set[str]) -> dict:
    """One element as stored: known fields only, every value checked. Raises
    ValueError with a reason the console can show."""
    if not isinstance(e, dict):
        raise ValueError("each element must be an object")
    kind = e.get("type")
    if kind not in OVERLAY_TYPES:
        raise ValueError(f"unknown element type {kind!r}")

    def num(key, lo, hi, default):
        v = e.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{kind}: {key} must be a number from {lo} to {hi}")
        return round(float(v), 5)

    def text(key, limit, default=""):
        v = e.get(key, default)
        if not isinstance(v, str) or len(v) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError(f"{kind}: {key} must be one line of at most {limit} characters")
        return v

    ident = str(e.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", ident) or ident in used:
        ident = next(f"{kind}{i}" for i in range(1, 1000) if f"{kind}{i}" not in used)
    used.add(ident)
    out = {"id": ident, "type": kind, "x": num("x", 0, 1, 0.05), "y": num("y", 0, 1, 0.05)}
    if kind == "image":
        out["size"] = num("size", 0.01, 1, 0.2)
        name = e.get("file")
        if not isinstance(name, str) or safe_image_name(name) != name:
            raise ValueError("image: file must be the name of an uploaded image")
        out["file"] = name
        out["opacity"] = num("opacity", 0, 1, 1.0)
    else:
        out["size"] = num("size", 0.01, 0.5, 0.06)
        colour = e.get("color", "white")
        if not isinstance(colour, str) or not (colour.lower() in COLOURS or re.fullmatch(r"#[0-9A-Fa-f]{6}", colour)):
            raise ValueError(f"{kind}: color must be a colour name or #rrggbb")
        out["color"] = colour.lower()
        box = e.get("box", False)
        if not isinstance(box, bool):
            raise ValueError(f"{kind}: box must be true or false")
        out["box"] = box
    if kind != "ticker":
        align = e.get("align", "left")
        if align not in ("left", "right", "center"):
            raise ValueError(f"{kind}: align must be left, right or center")
        out["align"] = align
    if kind in ("text", "ticker"):
        out["text"] = text("text", 300 if kind == "ticker" else 120)
    if kind == "ticker":
        out["speed"] = num("speed", 0.01, 1, 0.12)
    if kind == "clock":
        fmt = text("format", 64, "%H:%M:%S")
        if not fmt:
            raise ValueError("clock: format is empty")
        try:
            time.strftime(fmt)
        except ValueError:
            raise ValueError("clock: format is not a valid strftime format") from None
        out["format"] = fmt
        utc = e.get("utc", False)
        if not isinstance(utc, bool):
            raise ValueError("clock: utc must be true or false")
        out["utc"] = utc
    return out


def clean_overlay(elements) -> list[dict]:
    if not isinstance(elements, list):
        raise ValueError("elements must be a list")
    if len(elements) > OVERLAY_MAX:
        raise ValueError(f"at most {OVERLAY_MAX} elements")
    used: set[str] = set()
    return [clean_element(e, used) for e in elements]


def safe_image_name(raw: str) -> str | None:
    """A file name safe to keep in OVERLAY_DIR: no directories, no leading
    dot, letters, digits . _ - only, and a PNG or JPEG extension."""
    base = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem.strip("_") or ext.lower() not in ("png", "jpg", "jpeg") or len(base) > 64:
        return None
    return base


def image_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def drawtext(fontsize: int, colour: str, x: str, y: str, box: bool, **src) -> str:
    opts: list[tuple[str, object]] = [("fontfile", FONT)]
    opts += list(src.items())
    opts += [("fontsize", fontsize), ("fontcolor", ff_colour(colour))]
    if box:
        # A third of the font size, as the old fixed overlay drew it at 432p.
        opts += [("box", 1), ("boxcolor", BOX), ("boxborderw", max(2, round(fontsize / 3)))]
    opts += [("x", x), ("y", y)]
    return filter_str("drawtext", opts)


def anchor_x(align: str, x: float, width: str, frame: str = "w") -> str:
    """x expression for the left edge, from an anchor at fraction x of the
    frame width, aligned by the element's own width. drawtext calls the
    frame w and the text tw; overlay calls them W and w."""
    return {"left": f"{frame}*{x}", "right": f"{frame}*{x}-{width}", "center": f"{frame}*{x}-{width}/2"}[align]


def video_graph(pre: list[str], ops: list[tuple], post: list[str], first_image: int,
                video_in: str) -> tuple[list[str], list[str], bool]:
    """(image input args, filter args, complex). Without images the chain stays
    a plain -vf, as it was before images existed; images are extra inputs, so
    they need -filter_complex and explicit -map."""
    images = [op for op in ops if op[0] == "img"]
    if not images:
        chain = pre + [op[1] for op in ops] + post
        return [], ["-vf", ",".join(chain)], False
    inputs: list[str] = []
    parts: list[str] = []
    cur, chain, n = video_in, list(pre), 0
    for op in ops:
        if op[0] == "f":
            chain.append(op[1])
            continue
        if op[1] is None:
            parts.append(f"{op[2]}[img{n}]")  # generated in the graph (a ticker band)
        else:
            # One frame, decoded once: overlay repeats an image input's last
            # frame for as long as the main input runs (eof_action=repeat).
            parts.append(f"[{first_image + len(inputs) // 2}:v]{op[2]}[img{n}]")
            inputs += ["-i", op[1]]
        if chain:
            parts.append(f"{cur}{','.join(chain)}[base{n}]")
            cur = f"[base{n}]"
        parts.append(f"{cur}[img{n}]{op[3]}[ov{n}]")
        cur, chain, n = f"[ov{n}]", [], n + 1
    parts.append(f"{cur}{','.join(chain + post) or 'null'}[vout]")
    return inputs, ["-filter_complex", ";".join(parts)], True


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR or a JPEG's start-of-frame marker,
    without decoding anything; None when the header cannot be read."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", data[16:24])
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                       # fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2                               # markers with no length
            continue
        (seg,) = struct.unpack(">H", data[i + 2:i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > len(data):
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker == 0xDA or seg < 2:            # image data before any frame header
            return None
        i += 2 + seg
    return None
