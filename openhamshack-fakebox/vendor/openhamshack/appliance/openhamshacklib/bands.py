"""Where a station may transmit: the licence regions' band lists, QO-100's
transponder edges and channel plan, and the check that keeps a signal's
occupied width inside them (tx_blocked).

set_ptt in openhamshack.py asks tx_blocked before every key-up; the console and
the pad offer qo100_channels. UK behaviour is frozen answer-for-answer by
LicenceRegions in test_openhamshack.py.
"""

from __future__ import annotations


# UK amateur transmit allocations (MHz), Full licence. The 146 MHz segment is
# the NoV: its upper limit is 146.9375 MHz in Scotland and within 40 km of the
# Scottish border or coast (protecting services above), 147.000 elsewhere.
UK_BANDS = [(50.0, 52.0), (70.0, 70.5), (144.0, 146.0), (430.0, 440.0), (1240.0, 1325.0),
            (2310.0, 2450.0), (3400.0, 3410.0), (5650.0, 5850.0), (10000.0, 10500.0)]
NOV146_TOP = {"uk": 147.0, "scotland": 146.9375}
# Outside the UK. Asked for when amateurs elsewhere began installing the ISO:
# a UK-only guard either stops them transmitting where they may, or teaches
# them to pick "Rest of UK" and so lose every check.
#
# "iaru1": IARU Region 1's band plan (Europe, Africa, the Middle East), the
# allocations OpenHamShack can reach, in the UK list's shape - the Pluto tunes
# 70 MHz to 6 GHz, and 3 cm is listed as the UK list lists it, for a
# transverter. Not 6 m or 4 m: Region 1 has no common allocation there wide
# enough for DATV. It is a band plan, not anyone's licence: 13 cm is
# 2300-2450 in the plan but narrower in many countries, 9 cm is a few MHz
# where it exists at all, and 23 cm stops at 1300 (the UK's 1300-1325 is a
# UK allocation). The console says so in as many words; the box makes no
# claim to legal accuracy. QO-100's uplink (2400-2450, the amateur-satellite
# allocation) is inside 13 cm here as in the UK list.
#
# "other": no band check. The QO-100 guard (beacon segment, narrowband
# transponder, the top edge) still applies, because it protects a shared
# satellite rather than interpreting a licence. The console shows a
# standing note while it is chosen.
IARU1_BANDS = [(144.0, 146.0), (430.0, 440.0), (1240.0, 1300.0), (2300.0, 2450.0), (3400.0, 3410.0),
               (5650.0, 5850.0), (10000.0, 10500.0)]
LICENCE_REGIONS = ("scotland", "uk", "iaru1", "other")
# Occupied bandwidth is taken as SR x 1.35, the widest roll-off: PlutoDVB2 has
# no roll-off control, so 0.35 is the safe assumption for every radio.
OCCUPIED_FACTOR = 1.35


def licence_region(cfg: dict) -> str:
    """The licence region as the guard reads it. Absent is "uk" and anything
    unrecognised Scotland, exactly as before regions outside the UK existed,
    so an old or hand-edited value never loosens the check."""
    region = str(cfg.get("licenceRegion") or "uk")
    return region if region in LICENCE_REGIONS else "scotland"


def tx_bands(cfg: dict) -> list[tuple[float, float]] | None:
    """The bands transmission may use, or None when the operator has chosen
    no band check ("other")."""
    region = licence_region(cfg)
    if region == "other":
        return None
    if region == "iaru1":
        return list(IARU1_BANDS)
    bands = list(UK_BANDS)
    if cfg.get("nov146", True):
        top = NOV146_TOP.get(str(cfg.get("licenceRegion") or "uk"), NOV146_TOP["scotland"])
        bands = [(lo, top) if (lo, hi) == (144.0, 146.0) else (lo, hi) for lo, hi in bands]
    return bands


def mhz(v: float) -> str:
    # Exact, never rounded up: :g printed the 146.9375 limit as 146.938.
    return f"{v:.4f}".rstrip("0").rstrip(".")


# QO-100 wideband transponder, from BATC's band plan (V3, Feb 2021,
# https://wiki.batc.org.uk/QO-100_WB_Bandplan). The transponder takes
# 2401.0-2410.0 MHz up and sends 10490.5-10499.5 down: downlink = uplink +
# 8089.5. Its bottom 2 MHz (up to 10492.5 down) is the beacon's alone, so
# users have 2403.0-2410.0. The narrowband transponder is 2400.0-2400.5 up.
QO100_OFFSET_MHZ = 8089.5
QO100_WB_UPLINK = (2401.0, 2410.0)
QO100_WB_USERS = (2403.0, 2410.0)
QO100_NB_UPLINK = (2400.0, 2400.5)


def qo100_channels(sr_ks: float) -> list[dict]:
    """The band plan's channels for a symbol rate: uplink and downlink
    centres, and whether it is where that rate should go first. 1 and 1.5 MS
    have three wide channels; 500/333/250 kS narrow channels every 0.5 MHz
    (500 only below 10497); 125/66/33 kS every 0.25 MHz. 333 kS and below
    should use the top segment (above 10497.0 down) first and move down only
    when it is full."""
    sr = round(float(sr_ks))
    if sr in (1000, 1500):
        down = [10493.25, 10494.75, 10496.25]
    elif sr == 500:
        down = [10492.75 + 0.5 * k for k in range(9)]
    elif sr in (333, 250):
        down = [10492.75 + 0.5 * k for k in range(14)]
    elif sr in (125, 66, 33):
        down = [10492.75 + 0.25 * k for k in range(27)]
    else:
        return []
    return [{"downlinkMHz": d, "uplinkMHz": round(d - QO100_OFFSET_MHZ, 3),
             "preferred": sr > 333 or d > 10497.0} for d in down]


def qo100_blocked(f: float, sr_ks: float) -> str:
    """Why a signal would hit QO-100 where it must not, or ''. Only signals
    touching the transponders are judged; 2.4 GHz elsewhere is the licence's
    business alone.

    The edges are judged at the symbol rate's width, not the 1.35 used for
    the licence: that is how BATC's band plan is drawn - its 1.5 MS wide
    channel 1 at 10493.25 reaches exactly the beacon segment's 10492.5 edge
    at its symbol rate - so 1.35 would forbid a channel the plan offers."""
    half = sr_ks / 2000.0
    lo, hi = f - half, f + half
    eps = 1e-6

    def touches(seg: tuple[float, float]) -> bool:
        return lo < seg[1] - eps and hi > seg[0] + eps

    if touches(QO100_NB_UPLINK):
        return f"Signal reaches the QO-100 narrowband transponder ({mhz(QO100_NB_UPLINK[0])}-{mhz(QO100_NB_UPLINK[1])} MHz)"
    if touches(QO100_WB_UPLINK):
        if lo < QO100_WB_USERS[0] - eps:
            return (f"Signal reaches down to {mhz(lo)} MHz, into QO-100's beacon segment"
                    f" (below {mhz(QO100_WB_USERS[0])} up, {mhz(QO100_WB_USERS[0] + QO100_OFFSET_MHZ)} down)")
        if hi > QO100_WB_USERS[1] + eps:
            return f"Signal reaches {mhz(hi)} MHz, above the top of QO-100's wideband transponder ({mhz(QO100_WB_USERS[1])})"
    return ""


def tx_blocked(cfg: dict) -> str:
    """Why this transmission would fall outside the licence, or onto part
    of QO-100 it must not touch, or '' if neither. Checks both edges of the
    occupied signal, not just the centre."""
    try:
        f = float(cfg["frequencyMHz"])
        half = float(cfg["symbolRateKs"]) * OCCUPIED_FACTOR / 2000.0
    except (KeyError, TypeError, ValueError):
        return "Frequency or symbol rate is not set"
    lo, hi = f - half, f + half
    bands = tx_bands(cfg)
    if bands is None:
        return qo100_blocked(f, float(cfg["symbolRateKs"]))
    for b_lo, b_hi in bands:
        if b_lo <= lo and hi <= b_hi:
            return qo100_blocked(f, float(cfg["symbolRateKs"]))
    for b_lo, b_hi in bands:
        if b_lo <= f <= b_hi:
            if hi > b_hi:
                where = " Scotland" if b_hi == NOV146_TOP["scotland"] else ""
                return f"Signal reaches {mhz(hi)} MHz, above the {mhz(b_hi)} MHz{where} limit"
            return f"Signal reaches down to {mhz(lo)} MHz, below the {mhz(b_lo)} MHz band edge"
    if licence_region(cfg) == "iaru1":
        return f"{mhz(f)} MHz is outside the IARU Region 1 amateur bands"
    return f"{mhz(f)} MHz is outside the amateur bands"
