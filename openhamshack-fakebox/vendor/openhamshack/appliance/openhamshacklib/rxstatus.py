"""A receiver's status turned into readings: longmynd's lock states, the
MODCOD being decoded, and aiming an antenna - the margin over the decode
threshold as it is now, the AGC as a direction to turn when nothing locks,
the trace of the last AIM_SECONDS (Aim), and the TV readout's text.

openhamshack.py keeps the status itself (rx_state, per receiver), samples it,
and its Aiming section says why aiming reads the margin now rather than
the advice's low tenth.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from collections import deque
from pathlib import Path

from .dvb import ADVICE_OK_DB, MODCODS, PUNCTURE_RATES, mer_needed


LONGMYND_STATES = {"0": "Starting", "1": "Searching", "2": "Found header", "3": "Locked DVB-S", "4": "Locked DVB-S2"}


def rx_modcod(st: dict) -> tuple[str, str, str] | None:
    """(mode, constellation, fec) of the signal being decoded, from a copy of
    rx_state. Each mode's field is only believed in that mode's lock state:
    longmynd leaves the other one at whatever it last was."""
    if st.get("state") == "4" and st.get("modcod") in MODCODS:
        const, fec = MODCODS[st["modcod"]]
        return "dvbs2", const.lower(), fec
    if st.get("state") == "3" and st.get("puncture") in PUNCTURE_RATES:
        return "dvbs", "qpsk", PUNCTURE_RATES[st["puncture"]]
    return None


AIM_HZ = 5
AIM_SECONDS = 30
# The aiming display's colours: red below the threshold, amber inside the
# advice's "drops out on the next fade" band, green above it.
AIM_OK_DB = ADVICE_OK_DB
# mpv's on-screen overlay id for the TV readout (any number not used by a
# script; mpv's own scripts use their own ids per client).
AIM_OSD_ID = 63


def aim_band(margin_db: float | None) -> str | None:
    if margin_db is None:
        return None
    return "bad" if margin_db < 0 else "marginal" if margin_db < AIM_OK_DB else "good"


def agc_level(agc2: int | None) -> float | None:
    """A rough signal indicator from the demodulator's AGC2 (longmynd $27),
    for peaking a signal too weak to lock: higher is more power in the
    channel. longmynd documents $27 as the AGC2 gain, 0 minimum to 65535
    maximum; the STV0910 loop raises it as the power after the symbol-rate
    filter falls, so a signal coming up out of the noise pulls it down. As dB
    below full gain it reads the right way round and in steps an operator can
    compare, but it is not calibrated, counts noise and neighbours in the
    channel as much as the wanted signal, and shifts when the front end's own
    gain steps (the LNA, $2, and AGC1, $26). A direction to turn, not a level.
    """
    if agc2 is None or agc2 < 0:
        return None
    return round(20 * math.log10(65536 / (agc2 + 1)), 1)


def aim_reading(st: dict, now: float, stale_label: str = "No tuner") -> dict:
    """The receiver as it is this instant, from a copy of rx_state: lock,
    the latest MER, the threshold for the MODCOD being decoded and the margin
    over it, and the tuner's AGC readings for when there is no lock."""
    fresh = now - st.get("at", 0.0) <= 3
    locked = fresh and now - st.get("locked_at", 0.0) < 1.0
    mc = rx_modcod(st) if locked else None
    need = mer_needed(*mc) if mc else None
    mer = st.get("mer") if locked else None
    margin = round(mer - need, 1) if mer is not None and need is not None else None
    raw = st.get("raw") or {}
    agc2 = raw_int(raw, "27") if fresh else None
    return {"locked": locked,
            "label": LONGMYND_STATES.get(st.get("state"), "Searching") if fresh else stale_label,
            "name": st.get("name", "") if locked else "",
            "modcod": f"{mc[1].upper()} {mc[2]}" if mc else None,
            "merDb": mer, "needDb": need, "marginDb": margin, "band": aim_band(margin),
            "agc1": raw_int(raw, "26") if fresh else None, "agc2": agc2,
            "lna": raw_int(raw, "2") if fresh else None, "level": agc_level(agc2)}


class Aim:
    """Aiming mode: on or off (the pad's keys and the TV follow it), the
    readings of the last AIM_SECONDS, and the best margin and AGC level seen
    since the peak was last reset. Turning aiming on resets the peak: each
    session at the mast starts from nothing."""

    def __init__(self, seconds: float = AIM_SECONDS, hz: float = AIM_HZ):
        self.lock = threading.Lock()
        self.on = False
        self.history: deque = deque(maxlen=int(seconds * hz))
        self.last = aim_reading({"at": float("-inf")}, 0.0)   # nothing heard yet
        self.at = 0.0
        self.peak_margin: float | None = None
        self.peak_level: float | None = None
        self.rx = 1     # the receiver being aimed for

    def tick(self, reading: dict, now: float) -> None:
        with self.lock:
            self.last, self.at = reading, now
            self.history.append((now, reading["marginDb"], reading["level"]))
            m, lvl = reading["marginDb"], reading["level"]
            if m is not None and (self.peak_margin is None or m > self.peak_margin):
                self.peak_margin = m
            if lvl is not None and (self.peak_level is None or lvl > self.peak_level):
                self.peak_level = lvl

    def reset_peak(self) -> None:
        with self.lock:
            self.peak_margin = self.peak_level = None

    def set(self, on: bool) -> None:
        if on and not self.on:
            self.reset_peak()
        self.on = bool(on)

    def follow(self, rid: int) -> bool:
        """Aim by another receiver: its readings start from nothing, as a
        trace and a peak mixing two receivers would mean neither."""
        with self.lock:
            if rid == self.rx:
                return False
            self.rx = rid
            self.history.clear()
            self.peak_margin = self.peak_level = None
            self.last = aim_reading({"at": float("-inf")}, 0.0)
        return True

    def view(self, now: float, history: bool = True) -> dict:
        """What /api/rx/aim returns. History ages are seconds before now,
        so a browser needs no clock in common with the box."""
        with self.lock:
            out = {**self.last, "on": self.on, "rx": self.rx, "peakMarginDb": self.peak_margin, "peakLevel": self.peak_level,
                   "peakBand": aim_band(self.peak_margin), "hz": AIM_HZ, "seconds": AIM_SECONDS}
            if history:
                out["history"] = [[round(t - now, 1), m, lvl] for t, m, lvl in self.history]
        return out


def aim_osd(view: dict) -> str:
    """The TV readout, as ASS for mpv's osd-overlay (720 lines high): the
    margin in its colour, big, top left over the picture, and a line under it."""
    colour = {"bad": "&H4040FF&", "marginal": "&H00B0FF&", "good": "&H60E040&"}
    small = r"{\fs40\1c&HFFFFFF&}"
    peak = view.get("peakMarginDb")
    if view.get("locked") and view.get("marginDb") is not None:
        big = f"{view['marginDb']:+.1f} dB"
        tone = colour[view["band"]]
        line = " · ".join(x for x in (f"peak {peak:+.1f}" if peak is not None else "",
                                      f"MER {view['merDb']:.1f}", view.get("modcod") or "", view.get("name") or "") if x)
    elif view.get("locked"):
        big, tone = f"MER {view['merDb']:.1f}" if view.get("merDb") is not None else "Locked", "&HFFFFFF&"
        line = view.get("name") or ""
    else:
        big, tone = "No lock", "&H4040FF&"
        lvl = view.get("level")
        line = f"AGC level {lvl:.1f} (rough)" if lvl is not None else (view.get("label") or "")
        if view.get("peakLevel") is not None:
            line += f" · peak {view['peakLevel']:.1f}"
    # A station name is the other station's to choose: braces and
    # backslashes would be read as ASS overrides.
    line = line.translate({ord("{"): "(", ord("}"): ")", ord("\\"): "/"})
    return r"{\an7\bord5\3c&H000000&\b1\fs110\1c" + tone + "}" + big + r"\N" + small + "AIMING  " + line


def active_outputs() -> list[dict] | None:
    """sway's connected screens, or None where sway is not running."""
    try:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        sock = next(Path(runtime).glob("sway-ipc.*.sock"))
        outs = json.loads(subprocess.run(["swaymsg", "-s", str(sock), "-t", "get_outputs"],
                                         capture_output=True, text=True, timeout=3).stdout)
    except (OSError, StopIteration, ValueError, subprocess.TimeoutExpired):
        return None
    return [o for o in outs if o.get("active")]


def raw_int(raw: dict, key: str) -> int | None:
    v = str(raw.get(key, "")).strip()
    return int(v) if v.lstrip("-").isdigit() else None
