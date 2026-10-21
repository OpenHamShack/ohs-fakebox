"""Dock360 settings -> the FAKEBOX_* environment fakebox.py reads at import.

Kept apart from main.py, and free of any import of the box code, so the one
piece that decides what the fakebox actually does can be tested without a
container, ffmpeg or the vendored daemon.

Why a mapping at all, rather than naming the Dock360 settings FAKEBOX_* and
passing them straight through:

* fakebox.py tests its switches with bool(os.environ.get(...)), and Dock360
  delivers a bool setting as the string "false" - which is truthy. Passed
  straight through, "No signal: off" would have meant no signal.
* Packet loss is a fraction to fakebox (0.02) and a percentage to a person
  (2). Typing 2 into a field fakebox reads as a fraction drops every packet.
* A clip is a file name in the data volume, not a path: a path typed into a
  settings form is a way to make the stream read any file in the container.

Each bad value falls back to the harmless default and is reported, rather
than refusing to start: a fakebox that is up and saying "loss 250% is not a
percentage, using 0" is more use to whoever is testing a phone than one in a
restart loop whose reason is in a log nobody is reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

CODECS = ("h264", "hevc")
AUDIOS = ("mp2", "aac")
CLIP_SUFFIXES = (".mov", ".mp4", ".mkv", ".ts", ".m4v", ".webm")
TESTCARD = "testcard"
# A clip name is a plain file name: no directory part, nothing hidden.
CLIP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}")
PIN_RE = re.compile(r"[A-Za-z0-9]{1,32}")
DEFAULT_PIN = "1234"
TRUE = {"1", "true", "yes", "on"}


@dataclass
class Resolved:
    """What the fakebox will be started with, and anything that was wrong."""

    env: dict[str, str]
    pin: str
    clip: str            # the file name, or "" for the test card
    codec: str
    audio: str
    loss_percent: float
    cut_seconds: int
    no_signal: bool
    problems: list[str] = field(default_factory=list)


def clip_name_ok(name: str) -> bool:
    return bool(CLIP_RE.fullmatch(name)) and name.lower().endswith(CLIP_SUFFIXES)


def list_clips(clips_dir: Path) -> list[str]:
    try:
        return sorted(p.name for p in clips_dir.iterdir() if p.is_file() and clip_name_ok(p.name))
    except OSError:
        return []


def resolve(env: Mapping[str, str], clips_dir: Path) -> Resolved:
    problems: list[str] = []

    codec = (env.get("BOX_CODEC") or "h264").strip().lower()
    if codec not in CODECS:
        problems.append(f"video codec {codec!r} is not one of {', '.join(CODECS)}; using h264")
        codec = "h264"

    audio = (env.get("BOX_AUDIO") or "mp2").strip().lower()
    if audio not in AUDIOS:
        problems.append(f"audio codec {audio!r} is not one of {', '.join(AUDIOS)}; using mp2")
        audio = "mp2"

    raw_loss = (env.get("BOX_LOSS_PERCENT") or "0").strip().rstrip("%").strip()
    try:
        loss = float(raw_loss)
        if not 0 <= loss <= 100:
            raise ValueError
    except ValueError:
        problems.append(f"packet loss {raw_loss!r} is not a percentage between 0 and 100; using 0")
        loss = 0.0

    raw_cut = (env.get("BOX_CUT_SECONDS") or "0").strip()
    try:
        cut = int(raw_cut)
        if cut < 0:
            raise ValueError
    except ValueError:
        problems.append(f"cut interval {raw_cut!r} is not a whole number of seconds; not cutting")
        cut = 0

    no_signal = (env.get("BOX_NO_SIGNAL") or "").strip().lower() in TRUE

    pin = (env.get("BOX_PIN") or "").strip()
    if not PIN_RE.fullmatch(pin):
        # Empty is refused too: a box with no PIN never authorises a remote
        # at all, which reads to the app under test as a broken PIN screen.
        problems.append(f"PIN must be 1 to 32 letters or digits; using {DEFAULT_PIN}")
        pin = DEFAULT_PIN

    clip = (env.get("BOX_CLIP") or "").strip()
    if clip.lower() in ("", TESTCARD):
        clip = ""
    elif not clip_name_ok(clip):
        problems.append(f"clip {clip!r} is not a plain video file name; showing the test card")
        clip = ""
    elif not (clips_dir / clip).is_file():
        have = ", ".join(list_clips(clips_dir)) or "none"
        problems.append(f"clip {clip!r} is not in the clips folder (have: {have}); showing the test card")
        clip = ""

    out = {
        "FAKEBOX_CODEC": codec,
        "FAKEBOX_AUDIO": audio,
        # Formatted rather than str(): 2 -> "0.02", not "0.020000000000000004".
        "FAKEBOX_CORRUPT": f"{loss / 100:.6g}" if loss else "",
        "FAKEBOX_CUT": str(cut) if cut else "",
        # fakebox tests these with bool(), so "off" must be empty, not "false".
        "FAKEBOX_NOSIGNAL": "1" if no_signal else "",
        "FAKEBOX_FILE": str(clips_dir / clip) if clip else "",
    }
    return Resolved(env=out, pin=pin, clip=clip, codec=codec, audio=audio, loss_percent=loss,
                    cut_seconds=cut, no_signal=no_signal, problems=problems)


def apply(resolved: Resolved, environ: dict) -> None:
    """Put the resolved switches into `environ`, removing any left over.

    An empty value is removed rather than set to "", so the environment says
    the same thing to fakebox whichever way it tests a variable.
    """
    for key, value in resolved.env.items():
        if value:
            environ[key] = value
        else:
            environ.pop(key, None)
