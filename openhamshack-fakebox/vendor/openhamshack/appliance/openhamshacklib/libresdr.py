"""Driving a LibreSDR with DL1JM's DATV firmware: the words, with no state.

Until 5 October 2026 OpenHamShack sent a LibreSDR nothing but the transport
stream on UDP 8282, so whatever the radio's own web page held went out:
frequency, mode, power. That night libre-2 ("Libre0326", DL1JM firmware)
showed how the firmware really behaves, and this is built on that alone
(~/openhamshack-desktop/docs/next-session.md, "Later the same night"):

- The settings form posts to save.php, which writes /settings.txt and
  answers 302; the AD9361 is set up from that file only when a stream
  arrives on 8282 - then the TX LO went to 1255.000062 MHz for 1255, at
  666 kS/s for 333 kS. pluto.php's form is static defaults, not the stored
  settings: the stored ones are read from /settings.txt (openhamshack-datv's
  ~/openhamshack-datv/docs/libre2-firmware.md, the firmware's scripts read).
- requests.php?gain=<dB> sets the power and KEYS the transmitter.
- requests.php?PTT=on|off keys and unkeys; requests.php?onair answers 0 on
  air and 1 in standby (that way round); getlevel.php answers "-20.0dB".
- After one PTT and one gain since boot, a stream arriving keys it by itself.
- Another program's iio settings (TX LO powered down, -89.75 dB) stay until
  a reboot, and the firmware's own reconfiguration does not undo them.
- No status topics; the AD9361's own state can be read from iiod (30431):
  altvoltage1 (TX_LO) frequency and powerdown, voltage0 hardwaregain and
  sampling_frequency, all outputs.

openhamshack.py's LibreSDR section does the talking and says why the key-up and
key-down run in the order they do; here are the stored settings and the
form made from them, the transverter LO worked out, the answers parsed, and
the readback judged.
"""

from __future__ import annotations

import re


# Power is sent the way the PlutoDVB2 path sends it: 0 is full, and nothing
# below -80 dB, which is as good as off and well inside the AD9361's
# 0 to -89.75 dB. One clamp for save.php's power and requests.php?gain=, so
# the stored setting and the live one cannot disagree.
LIBRESDR_GAIN_RANGE = (-80.0, 0.0)
# The LO came back 62 Hz high on libre-2 (the AD9361's synthesiser steps);
# a wrong frequency is MHz out, not Hz. 2 kHz says "this is ours".
LIBRESDR_LO_TOLERANCE_HZ = 2000.0
# The AD9361's TX attenuation steps are 0.25 dB; getlevel.php prints one
# decimal. Half a dB is "the power we asked for"; -89.75 against -20 is not.
LIBRESDR_GAIN_TOLERANCE_DB = 0.5
# The AD9361 driver's name for the device that holds the LO and the gain
# (ad9361-phy); iiod knows it by that name and by an iio:deviceN id.
LIBRESDR_PHY = "ad9361-phy"
# What is read back, as (channel, attribute) of the phy's outputs.
LIBRESDR_IIO_ATTRS = (("altvoltage1", "frequency"), ("altvoltage1", "powerdown"),
                      ("voltage0", "hardwaregain"), ("voltage0", "sampling_frequency"))


def libresdr_gain(cfg: dict) -> float:
    lo, hi = LIBRESDR_GAIN_RANGE
    try:
        g = float(cfg.get("gainDb"))
    except (TypeError, ValueError):
        g = lo
    return max(lo, min(hi, g))


def libresdr_gain_text(gain: float) -> str:
    """'-20', '-12.5': what requests.php?gain= and save.php's power get."""
    return f"{gain:g}"


# The fields of save.php that OpenHamShack chooses. Everything else the radio has
# stored - the transverter LO and its dropdown, the channel dropdown, the
# H.265 box address, codec, sound, audio input, PCR/PTS and PAT periods - is
# the operator's, set on the radio's own page, and goes back exactly as
# settings.txt held it. `channel` is in that list on purpose: the firmware
# transmits on `freq` alone (udpts.sh never reads `channel`), so the dropdown
# is the page's business, and 0.9.15's blank was not one of its options.
LIBRESDR_OURS = ("callsign", "provname", "freq", "mode", "mod", "sr", "srselect", "fec", "pilots", "frame",
                 "power", "rolloff", "remux")
# What the AD9361 and the page's own "Freq-Manual (70 MHz - 6 GHz)" allow.
# A transverter LO that puts our frequency outside it is a mistake somewhere,
# and the radio must not be keyed on something nobody asked for.
LIBRESDR_TX_RANGE_MHZ = (70.0, 6000.0)
_SETTINGS_KEY = re.compile(r"[A-Za-z0-9_]+")


def libresdr_stored_settings(text: str) -> dict[str, str] | None:
    """The radio's /settings.txt - the file save.php writes and udpts.sh
    reads, so the settings the radio actually holds - as {field: value} in
    the file's order. One `key value` line per field; the value is the rest
    of the line after the first space, and may be empty (`h265box ` on
    libre-2). None when the text is not that file: an HTML error page, an
    empty answer, or one without a `freq`, which every save.php writes.

    Not pluto.php: that page is static, and its form holds the firmware's
    defaults (NOCALL, 0 MHz, h265box 198.51.100.120, trvlo 0) whatever is
    stored. 0.9.15 read it as the radio's settings and posted it back, which
    reset everything OpenHamShack does not set - a transverter LO among them,
    and with it the transmit frequency."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        if not _SETTINGS_KEY.fullmatch(key):
            return None
        out[key] = value
    return out if "freq" in out else None


def libresdr_form(ours: dict[str, str], stored: dict[str, str]) -> dict[str, str]:
    """What goes to save.php: everything the radio has stored, with only the
    fields OpenHamShack owns (LIBRESDR_OURS) put over it. Anything else in `ours`
    is ignored, so a field added to the Pluto form for another firmware
    cannot quietly start overwriting an operator's setting here (0.9.15 did
    exactly that with pluto_form's h265box, pcrpts and patperiod)."""
    form = dict(stored)
    form.update({k: v for k, v in ours.items() if k in LIBRESDR_OURS})
    return form


def libresdr_tx_mhz(rf_mhz: float, stored: dict[str, str]) -> tuple[float | None, float, str]:
    """(the frequency for save.php's `freq`, the transverter LO, why not).
    OpenHamShack's frequency is what leaves the antenna; the radio's `freq` is
    what the radio itself transmits, which with a transverter is the IF.

    The firmware does not add the LO: udpts.sh hands settings.txt's `freq`
    straight to `pluto_dvb -t <freq>e6`, and `trvlo` is read by nothing but
    the page. The page's own JavaScript is what subtracts it - upd_freq()
    sets freq = channel - trvlo, and its LO list says the same ("2256 (IF
    144)": 2400 - 2256). So freq = RF - trvlo, the page's convention
    exactly, and the readback expects the LO at that IF. An empty trvlo is
    the page's 0 (its arithmetic treats "" as 0); one that is not a number,
    or an IF the radio cannot make, is refused rather than guessed at."""
    raw = (stored.get("trvlo") or "").strip()
    try:
        lo = float(raw) if raw else 0.0
    except ValueError:
        lo = float("nan")
    if lo != lo or lo in (float("inf"), float("-inf")):
        return None, 0.0, (f"has a transverter LO of '{raw}' stored, which is not a number of MHz, so the frequency "
                           "to put the radio on cannot be worked out; set the transverter LO on the LibreSDR's own page")
    tx = rf_mhz - lo
    low, high = LIBRESDR_TX_RANGE_MHZ
    if not low <= tx <= high:
        via = f" through its transverter LO of {libresdr_mhz(lo * 1e6)} MHz" if lo else ""
        return None, lo, (f"would have to transmit on {libresdr_mhz(tx * 1e6)} MHz for {libresdr_mhz(rf_mhz * 1e6)} "
                          f"MHz{via}, outside the {low:g}-{high:g} MHz it can; check the frequency in Settings › Radio "
                          "and the transverter LO on the LibreSDR's own page")
    return tx, lo, ""


def libresdr_onair(text: str) -> bool | None:
    """requests.php?onair: '0' is on air, '1' standby (the firmware's own
    way round); anything else means this is not that firmware."""
    t = (text or "").strip()
    return {"0": True, "1": False}.get(t)


def libresdr_level(text: str) -> float | None:
    """getlevel.php: '-20.0dB' -> -20.0."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*dB\s*", text or "", re.I)
    return float(m.group(1)) if m else None


def iio_number(text: str | None) -> float | None:
    """An iio attribute's value: '1255000062', '-20.000000 dB', '0'."""
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", text or "")
    return float(m.group(1)) if m else None


def iiod_reply(head: bytes) -> int | None:
    """iiod's answer line for a READ: the length of what follows, or a
    negative errno. None when the line is not a number at all."""
    try:
        return int(head.strip())
    except ValueError:
        return None


def libresdr_mhz(hz: float) -> str:
    return f"{hz / 1e6:.6f}".rstrip("0").rstrip(".")


def libresdr_config_seen(want_mhz: float, rb: dict | None) -> bool:
    """Whether the readback shows the radio set up for our frequency: the
    stream has arrived and the firmware has applied save.php's settings."""
    if not rb or rb.get("freqHz") is None:
        return False
    return abs(rb["freqHz"] - want_mhz * 1e6) <= LIBRESDR_LO_TOLERANCE_HZ


def libresdr_readback_problem(want_mhz: float, want_gain: float, rb: dict | None, level: float | None) -> str:
    """After keying: why what the radio reports is not what was asked, or ''.
    rb is the iiod readback (None when iiod cannot be read), level what
    getlevel.php said (None when it did not answer). With neither there is
    nothing to compare, and that is not a mismatch: the onair check and the
    settings posted are all there is on such a radio."""
    if rb:
        f = rb.get("freqHz")
        if f is not None and abs(f - want_mhz * 1e6) > LIBRESDR_LO_TOLERANCE_HZ:
            return (f"its transmitter is set to {libresdr_mhz(f)} MHz, not {libresdr_mhz(want_mhz * 1e6)} MHz: "
                    "it did not take OpenHamShack's settings")
        if rb.get("powerdown") is not None and rb["powerdown"] != 0:
            return ("its transmit oscillator is powered down, which another program left behind: "
                    "on the LibreSDR's own page, Save Settings, then Reboot")
        g = rb.get("gainDb")
        if g is not None and abs(g - want_gain) > LIBRESDR_GAIN_TOLERANCE_DB:
            return (f"its power is {g:g} dB, not {want_gain:g} dB"
                    + (": another program's setting is still in force; on the LibreSDR's own page, Save Settings, "
                       "then Reboot" if g <= -85 else ""))
        return ""
    if level is not None and abs(level - want_gain) > LIBRESDR_GAIN_TOLERANCE_DB:
        return f"it reports a power of {level:g} dB, not {want_gain:g} dB"
    return ""
