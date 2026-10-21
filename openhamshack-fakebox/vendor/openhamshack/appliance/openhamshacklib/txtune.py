"""Transmit tuning from the phone and tablet apps: the console's Transmit
panel and its Transmit mode window as rules the daemon can enforce
(clean_tx_tune), and the choices those windows offer (tx_options).

The console has always saved these with a bare POST /api/config, its own
buttons being the validation: only real rates on the FEC row, QPSK alone
for DVB-S, power stepped between -70 and 0 dB. An app has no such
buttons, and apply_config takes whatever it is sent - "dvbs3" would be
saved and the next key-up would fail inside ffmpeg. So POST /api/tx/tune
puts the same rules in front of apply_config. apply_config itself is
left as it is, so the console (and the pad) behave exactly as before.

The licence is not judged here, as it is not on the console: any
frequency may be dialled, and tx_blocked refuses the key-up - for every
client, the console, the pad and the apps alike - with the reason in
/api/status txBlocked from the moment the setting is saved. That is the
NoV's 146-147 MHz rule as well as every band edge and QO-100's.
"""

from __future__ import annotations

import math

from .bands import (QO100_NB_UPLINK, QO100_WB_UPLINK, QO100_WB_USERS, licence_region, qo100_channels,
                    tx_bands)
from .dvb import fec_choices

# The console's main page frequency buttons (index.html, the Transmit
# panel), and its symbol rates by band (sheets.js SR_SETS): the band plan's
# for QO-100's uplink, the terrestrial ones elsewhere. Kept in step with
# those by hand: the console builds its buttons in the browser.
TX_PRESETS = ({"label": "2 m", "frequencyMHz": 146.5}, {"label": "70 cm", "frequencyMHz": 437.0},
              {"label": "23 cm", "frequencyMHz": 1255.0})
TX_SYMBOL_RATES = {"qo100": (33, 66, 125, 250, 333, 500, 1000, 1500),
                   "terrestrial": (125, 250, 333, 500, 1000, 1500, 2000, 3000, 4000)}
# The console's power stepper (core.js): whole dB, full power down to -70.
TX_GAIN_RANGE = (-70, 0)
TX_TUNE_KEYS = ("frequencyMHz", "symbolRateKs", "mode", "constellation", "fec", "gainDb")
TX_MODES = ("dvbs2", "dvbs")


def _number(payload: dict, key: str) -> float:
    v = payload[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{key} must be a number")
    return v


def _rate(fec: str) -> float:
    n, d = fec.split("/")
    return int(n) / int(d)


def fecs_for(mode: str, constellation: str) -> list[str]:
    """The code rates a mode and modulation have (the console's fecsFor)."""
    choices = fec_choices()
    return list(choices["dvbs"]) if mode == "dvbs" else list(choices["dvbs2"].get(constellation, []))


def nearest_fec(mode: str, constellation: str, fec: str) -> str:
    """The rate nearest fec that the mode and modulation have, as the
    console moves it when the modulation changes (core.js nearestFec)."""
    rates = fecs_for(mode, constellation)
    if fec in rates:
        return fec
    try:
        want = _rate(fec)
    except (ValueError, ZeroDivisionError):
        return rates[0]
    return min(rates, key=lambda f: abs(_rate(f) - want))


def clean_tx_tune(payload: dict, cfg: dict) -> dict:
    """The patch apply_config should get for a transmit tuning request, or
    ValueError naming what is wrong. Follows the console's buttons: DVB-S
    takes QPSK and keeps a FEC DVB-S has (else 2/3); a new mode or
    modulation without a FEC moves the FEC to the nearest that it has; a
    FEC named outright must be one it has."""
    unknown = sorted(set(payload) - set(TX_TUNE_KEYS))
    if unknown:
        raise ValueError(f"Cannot set {', '.join(unknown)} here; transmit tuning takes "
                         + ", ".join(TX_TUNE_KEYS))
    if not payload:
        raise ValueError("Nothing to change")
    out: dict = {}
    if "frequencyMHz" in payload:
        f = _number(payload, "frequencyMHz")
        if f <= 0:
            raise ValueError("frequencyMHz must be above zero")
        out["frequencyMHz"] = f
    if "symbolRateKs" in payload:
        sr = _number(payload, "symbolRateKs")
        if sr <= 0:
            raise ValueError("symbolRateKs must be above zero")
        out["symbolRateKs"] = int(sr) if float(sr).is_integer() else sr
    if "gainDb" in payload:
        g = _number(payload, "gainDb")
        if not TX_GAIN_RANGE[0] <= g <= TX_GAIN_RANGE[1]:
            raise ValueError(f"gainDb must be {TX_GAIN_RANGE[0]} to {TX_GAIN_RANGE[1]} dB")
        out["gainDb"] = int(g) if float(g).is_integer() else g
    mode = payload.get("mode", cfg.get("mode") or "dvbs2")
    if mode not in TX_MODES:
        raise ValueError("mode must be dvbs2 or dvbs")
    const = payload.get("constellation", cfg.get("constellation") or "qpsk")
    if "constellation" in payload:
        allowed = ["qpsk"] if mode == "dvbs" else list(fec_choices()["dvbs2"])
        if const not in allowed:
            raise ValueError(f"constellation must be one of: {', '.join(allowed)}"
                             + (" (DVB-S is QPSK only)" if mode == "dvbs" else ""))
    elif mode == "dvbs":
        const = "qpsk"
    if "mode" in payload:
        out["mode"] = mode
    if "constellation" in payload or ("mode" in payload and mode == "dvbs"):
        out["constellation"] = const
    fec = str(cfg.get("fec") or "2/3")
    if "fec" in payload:
        fec = payload["fec"]
        if fec not in fecs_for(mode, const):
            raise ValueError(f"fec must be one of: {', '.join(fecs_for(mode, const))} for "
                             f"{'DVB-S' if mode == 'dvbs' else 'DVB-S2 ' + const.upper()}")
        out["fec"] = fec
    elif "mode" in payload or "constellation" in payload:
        if mode == "dvbs":
            new = fec if fec in fecs_for("dvbs", "qpsk") else "2/3"
        else:
            new = nearest_fec(mode, const, fec)
        if new != cfg.get("fec"):
            out["fec"] = new
    return out


def tx_options(cfg: dict) -> dict:
    """GET /api/tx/options: what the console's Transmit panel and Transmit
    mode window offer, and the licence the key-up will be judged by."""
    bands = tx_bands(cfg)
    choices = fec_choices()
    return {
        "presets": [dict(p) for p in TX_PRESETS],
        "symbolRates": {k: list(v) for k, v in TX_SYMBOL_RATES.items()},
        "modes": list(TX_MODES),
        "constellations": {"dvbs": ["qpsk"], "dvbs2": list(choices["dvbs2"])},
        "fec": {"dvbs": list(choices["dvbs"]), "dvbs2": {k: list(v) for k, v in choices["dvbs2"].items()}},
        "gainDb": {"min": TX_GAIN_RANGE[0], "max": TX_GAIN_RANGE[1], "step": 1},
        "qo100": {"uplinkMHz": list(QO100_WB_UPLINK), "usersMHz": list(QO100_WB_USERS),
                  "narrowbandMHz": list(QO100_NB_UPLINK),
                  "channels": {str(sr): qo100_channels(sr) for sr in TX_SYMBOL_RATES["qo100"]
                               if qo100_channels(sr)}},
        "licence": {"region": licence_region(cfg), "nov146": bool(cfg.get("nov146", True)),
                    "bands": None if bands is None else [[lo, hi] for lo, hi in bands]},
    }
