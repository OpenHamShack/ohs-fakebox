"""QO-100 wideband, worked out from what BATC's servers send: signals found
in one spectrum frame as BATC's own page finds them (qo100_detect), the
waterfall's one-byte rows, the chat's nick and text rules, the callsign in
a received service name, and the over-power watch's verdict for one look.

openhamshack.py's QO-100 sections hold the websocket connections, the spectrum,
waterfall and chat state, and the loops; their opening comments say why
the feed is read the way it is and how the chat is paced.
"""

from __future__ import annotations

import math
import re

from .bands import QO100_OFFSET_MHZ
from .dvb import DATV_SYMBOL_RATES
from .settings import callsign


QO100_LOW_MHZ, QO100_SPAN_MHZ = 10490.5, 9.0
# The server's scale: 3000 counts per dB (eshail-ghy-wb-fft-airspy, which
# also holds the noise floor near 9000-11600). BATC's page draws at 3276.8
# counts per "dB" (65536 = 20 dB), which reads ~9% high; the box shows real dB.
QO100_COUNTS_PER_DB = 3000.0


def batc_align_symbolrate(width_mhz: float) -> float:
    """BATC's align_symbolrate: a signal's measured width (MHz) to the
    symbol rate (MS) it most likely is. Their thresholds, unchanged."""
    for edge, sr in ((0.022, 0.0), (0.060, 0.035), (0.086, 0.066), (0.185, 0.125), (0.277, 0.250),
                     (0.388, 0.333), (0.700, 0.500), (1.2, 1.0), (1.6, 1.5), (2.2, 2.0)):
        if width_mhz < edge:
            return sr
    return math.floor(width_mhz * 5 + 0.5) / 5.0   # JS Math.round: halves go up


def nearest_symbol_rate(sr_ks: float) -> int:
    """The standard DATV symbol rate a receiver should be set to for a
    detected one: BATC labels the narrowest class 35 kS, which stations
    transmit as 33. Nearest by ratio, as symbol rates are spaced."""
    return min(DATV_SYMBOL_RATES, key=lambda r: abs(math.log(r / sr_ks)))


def qo100_detect(bins, beacon: float = 0.0) -> tuple[list[dict], float, bool]:
    """Find the signals in one frame of BATC's feed, as BATC's own page does.

    A port of detect_signals() in eshail.batc.org.uk/wb/index.js (British
    Amateur Television Club; Phil Crump's QO-100 wideband monitor), kept to
    the same constants and the same arithmetic, so the box finds what an
    operator sees on BATC's page: a signal starts where the 3-bin average
    rises above 16000 and ends where it falls below; its strength is the
    mean of its middle 40%; each edge then moves in to where the bins reach
    75% of that strength over the noise level (11000); the width is aligned
    to a symbol-rate class and the centre is taken between the edges.
    Signals centred below 10492.0 are the beacon's band: one of 1 MS or more
    there is the beacon, whose strength (carried across frames, as BATC's
    global is) sets the over-power test; anything else there is ignored, as
    BATC ignores it. A signal still open at the top is only "out of band".

    Returns (signals, beacon strength, out_of_band). Each signal has the
    downlink frequency rounded as BATC labels it (12.5 kHz below 0.7 MS,
    25 kHz above), BATC's symbol-rate class and the standard rate to tune.
    """
    noise_level, threshold = 11000, 16000
    n = len(bins)
    found: list[dict] = []
    in_signal, start = False, 0

    def freq(k: float) -> float:
        return QO100_LOW_MHZ + ((k + 1) / n) * QO100_SPAN_MHZ

    for i in range(2, n):
        avg = (bins[i] + bins[i - 1] + bins[i - 2]) / 3.0
        if not in_signal:
            if avg > threshold:
                in_signal, start = True, i
            continue
        if avg >= threshold:
            continue
        in_signal, end = False, i
        lo, hi = start + 0.3 * (end - start), start + 0.7 * (end - start)
        middle = [bins[j] for j in range(int(lo), min(n, math.ceil(hi)))]
        strength = sum(middle) / len(middle)
        top = 0.75 * (strength - noise_level)
        # BATC's loops leave each edge one bin short of the first bin that
        # reaches the top, so these do too. Bounded, where the JS relied on
        # running off the array into undefined.
        j = start
        while j < n and bins[j] - noise_level < top:
            start, j = j, j + 1
        j = end
        while j >= 0 and bins[j] - noise_level < top:
            end, j = j, j - 1
        mid = start + (end - start) / 2.0
        sr = batc_align_symbolrate((end - start) * (QO100_SPAN_MHZ / n))
        centre = freq(mid)
        if centre < 10492.0:
            if sr >= 1.0:
                # Offered for tuning at its published frequency and rate
                # rather than as measured: it is the one fixed signal.
                beacon = strength
                found.append(dict(freqMHz=10491.5, beacon=True, overPower=False, symbolRateKs=1500,
                                  tuneSymbolRateKs=1500, lowMHz=round(freq(start), 3), highMHz=round(freq(end), 3),
                                  levelDb=round((strength - noise_level) / QO100_COUNTS_PER_DB, 1)))
            continue
        if sr == 0:
            continue
        step = 80.0 if sr < 0.7 else 40.0
        over = bool(beacon) and sr >= 0.4 and strength > beacon - 0.75 * 3276.8
        found.append(dict(freqMHz=round(math.floor(centre * step + 0.5) / step, 4), beacon=False, overPower=over,
                          symbolRateKs=round(sr * 1000), tuneSymbolRateKs=nearest_symbol_rate(sr * 1000),
                          lowMHz=round(freq(start), 3), highMHz=round(freq(end), 3),
                          levelDb=round((strength - noise_level) / QO100_COUNTS_PER_DB, 1)))
    return found, beacon, in_signal


WF_DB_LOW, WF_DB_STEP = -3.0, 0.125


def wf_row(bins) -> bytes:
    floor = 11000
    out = bytearray()
    for i in range(0, len(bins) - 1, 2):
        db = (max(bins[i], bins[i + 1]) - floor) / QO100_COUNTS_PER_DB
        out.append(max(0, min(255, round((db - WF_DB_LOW) / WF_DB_STEP))))
    return bytes(out)


CHAT_NICK_MAX = 20            # what BATC's page cuts a nick to
CHAT_TEXT_MAX = 300
CALLSIGN_RE = re.compile(r"(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(?:/[A-Z0-9]{1,4})?")


def chat_nick(cfg: dict) -> str | None:
    """The station's name in the chat: its callsign, and the operator's
    name after an underscore if one is set ("GM4BRI_Brian", the room's own
    convention). None until the callsign is a real one: the box's default
    OPENHAMSHACK must never appear in a club's chat."""
    call = callsign(cfg)
    if not CALLSIGN_RE.fullmatch(call):
        return None
    name = re.sub(r"[^\w .'-]", "", str(cfg.get("chatName") or ""), flags=re.ASCII).strip()
    return (f"{call}_{name}" if name else call)[:CHAT_NICK_MAX]


def chat_clean(text: str) -> str:
    """One line of printable text: the room shows messages as text, and a
    control character or line break has no business in it."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return re.sub(r"\s+", " ", text).strip()[:CHAT_TEXT_MAX]


def report_station(service: str) -> str:
    """Whose signal it is, from the service name in its SDT: stations put
    their callsign first and often more after it ("GM0OTB-Ray",
    "ZS6YI_Gary", "GB3VL.LINCOLN"); the callsign is what a report names.
    A name that does not start with one is used whole."""
    service = chat_clean(service)
    return service_callsign(service) or service[:20]


def service_callsign(service: str) -> str | None:
    """The callsign at the head of an SDT service name, or None when the
    name does not start with one. Separators are the ones stations use
    between callsign and the rest: - _ space / and . A portable suffix
    written with a slash ("M0ABC/P") is therefore cut to M0ABC, which is
    the station a report or a log entry names."""
    head = re.split(r"[-_ /.]", chat_clean(service), maxsplit=1)[0].upper()
    return head if CALLSIGN_RE.fullmatch(head) else None


# Over-power watch. On air on a QO-100 uplink our own signal comes back in
# BATC's spectrum about a second later; the band plan asks for a signal at
# least 1 dB below the beacon (the spectrum is power density, so this holds
# for any symbol rate). Above that the power is cut, live, by the excess plus
# half a dB (at most 6 dB a step), and if ten cuts or the bottom of the range
# do not bring it down, transmission stops. Only a PlutoDVB2 takes power
# while on air; on another radio an over-power signal is stopped at once.
QO100_POWER_MARGIN = 1.0
QO100_POWER_MAX_CUTS = 10
QO100_POWER_SETTLE = 4.0   # s after a change before judging again
QO100_POWER_FLOOR = -70.0
QO100_UNSEEN_AFTER = 15.0


def qo100_power_step(cfg: dict, signals: list[dict], cuts: int, live_gain: bool) -> tuple[str, float | None, str]:
    """("ok" | "cut" | "stop" | "unseen", new gainDb, words) for one look at
    the spectrum while on air. Pure, so it can be tested."""
    f, sr = float(cfg.get("frequencyMHz") or 0), float(cfg.get("symbolRateKs") or 0)
    down = f + QO100_OFFSET_MHZ
    beacon = next((s for s in signals if s.get("beacon")), None)
    own = next((s for s in signals if not s.get("beacon") and abs(s["freqMHz"] - down) <= max(0.06, sr / 2000)), None)
    if own is None:
        return "unseen", None, f"not seen on the downlink at {down:.3f} MHz"
    if beacon is None:
        return "ok", None, f"seen at {down:.3f} MHz; no beacon in view to compare with"
    over = own["levelDb"] - (beacon["levelDb"] - QO100_POWER_MARGIN)
    below = beacon["levelDb"] - own["levelDb"]
    if over <= 0:
        return "ok", None, f"{below:.1f} dB below the beacon"
    gain = float(cfg.get("gainDb") or 0)
    if not live_gain:
        return "stop", None, f"{-below:+.1f} dB against the beacon, and this radio cannot turn down while on air"
    new = gain - min(6.0, float(math.ceil(over + 0.5)))
    if cuts >= QO100_POWER_MAX_CUTS or new < QO100_POWER_FLOOR:
        return "stop", None, f"still over-power ({-below:+.1f} dB against the beacon) after {cuts} cuts"
    return "cut", new, f"over-power ({-below:+.1f} dB against the beacon): power cut to {new:g} dB"


def qo100_in_use(signals: list[dict], down: float, half: float) -> str:
    """The busy check's rule, for whichever spectrum the signals came from:
    the words for the first signal (the beacon included) overlapping our
    channel, down +/- half MHz, or "" when none does."""
    for s in signals:
        if s["lowMHz"] < down + half and s["highMHz"] > down - half:
            what = "the beacon" if s.get("beacon") else f"a {s['symbolRateKs']} kS signal at {s['freqMHz']:.3f} MHz"
            return f"{down:.3f} MHz is in use: {what}"
    return ""


# The Pluto's own spectrum (PlutoDVB2 "webfft") as the busy check's
# fallback when BATC's cannot be seen. From F5OEO/pluto-ori-ps iqtofft.cpp
# (before d452bee, which removed it): each bin is 100*log10 of the summed
# FFT magnitude, i.e. 5 counts per dB with no absolute reference, Hann
# windowed, at most two FFTs averaged, the middle half of each FFT kept, in
# ascending frequency. The frame is put on BATC's grid and scale and given
# to qo100_detect unchanged, so "a signal" means what it means on BATC's
# page and no per-station threshold is invented.
PLUTO_COUNTS_PER_DB = 5.0
QO100_BATC_BINS = 922
# BATC's bin width (9 MHz / 922 = 9.76 kHz): a coarser frame would smear
# the narrowest class BATC resolves (35 kS, about five of its bins).
QO100_BIN_MAX_HZ = QO100_SPAN_MHZ * 1e6 / QO100_BATC_BINS
QO100_BEACON_MHZ = 10491.5
QO100_BEACON_HALF_MHZ = 0.75          # 1.5 MS
PLUTO_BEACON_WINDOW_MHZ = 0.1         # LNB drift allowed
PLUTO_BEACON_MIN_DB = 6.0


def pluto_to_batc(bins, low_mhz: float, high_mhz: float, n: int = QO100_BATC_BINS) -> list[int]:
    """A Pluto frame spanning low..high MHz (downlink, drift already taken
    out) as a BATC frame: n bins over 10490.5-10499.5 MHz, each the mean of
    the Pluto bins whose centres fall in it, with the frame's median (its
    noise floor; the AD9363's DC spur or any one signal cannot move it) at
    BATC's 11000 counts and 3000 counts per dB. Where the Pluto frame does
    not reach, the floor. Relative levels only: the Pluto's AGC moves the
    absolute numbers, not the step from floor to signal."""
    m = len(bins)
    floor = sorted(bins)[m // 2]
    scale = QO100_COUNTS_PER_DB / PLUTO_COUNTS_PER_DB
    width = (high_mhz - low_mhz) / m
    sums, counts = [0.0] * n, [0] * n
    for j, v in enumerate(bins):
        f = low_mhz + (j + 0.5) * width
        k = math.floor((f - QO100_LOW_MHZ) / QO100_SPAN_MHZ * n)
        if 0 <= k < n:
            sums[k] += v
            counts[k] += 1
    out = []
    for k in range(n):
        if counts[k]:
            v = sums[k] / counts[k]
        else:
            # No Pluto bin's centre fell in this one: the bin under its
            # centre when the frame reaches it, else the floor.
            f = QO100_LOW_MHZ + (k + 0.5) * QO100_SPAN_MHZ / n
            j = math.floor((f - low_mhz) / width)
            v = bins[j] if 0 <= j < m else floor
        out.append(max(0, min(65535, round(11000 + (v - floor) * scale))))
    return out


def pluto_spectrum_verdict(bins, center_hz: float, span_hz: float, lnb_mhz: float,
                           down: float, half: float) -> tuple[str, str]:
    """("busy" | "clear" | "unknown", why) for our channel (down +/- half
    MHz) from one Pluto frame (max-held by the caller), given its centre and
    span at IF in Hz and the LNB's local oscillator. Unknown unless every
    condition holds: bins no wider than BATC's; the beacon and our channel
    both inside the frame; and the beacon where it should be - a signal of
    1 MS or more within 100 kHz of 10491.5 MHz, at least 6 dB over the
    floor. The beacon proves the dish, LNB and receiver are on QO-100 at
    all, and its measured offset is the LNB's drift, which is taken out of
    everything else before our channel is looked at."""
    m = len(bins)
    if m < 16 or span_hz <= 0:
        return "unknown", "the Pluto's spectrum frame is empty"
    width_hz = span_hz / m
    if width_hz > QO100_BIN_MAX_HZ:
        return "unknown", (f"the Pluto's spectrum is too coarse ({width_hz / 1000:.1f} kHz bins; "
                           f"BATC's are {QO100_BIN_MAX_HZ / 1000:.1f} kHz)")
    low = (center_hz - span_hz / 2) / 1e6 + lnb_mhz
    high = (center_hz + span_hz / 2) / 1e6 + lnb_mhz
    margin = PLUTO_BEACON_WINDOW_MHZ
    need_low = min(QO100_BEACON_MHZ - QO100_BEACON_HALF_MHZ, down - half) - margin
    need_high = max(QO100_BEACON_MHZ + QO100_BEACON_HALF_MHZ, down + half) + margin
    if low > need_low or high < need_high:
        return "unknown", (f"the Pluto's spectrum covers {low:.3f}-{high:.3f} MHz, "
                           f"not both the beacon and {down:.3f} MHz")
    found = qo100_detect(pluto_to_batc(bins, low, high))[0]
    beacon = next((s for s in found if s.get("beacon")), None)
    centre = (beacon["lowMHz"] + beacon["highMHz"]) / 2 if beacon else None
    if beacon is None or abs(centre - QO100_BEACON_MHZ) > PLUTO_BEACON_WINDOW_MHZ \
            or beacon["levelDb"] < PLUTO_BEACON_MIN_DB:
        seen = f"a {beacon['levelDb']:.1f} dB signal at {centre:.3f} MHz" if beacon else "no signal of 1 MS or more"
        return "unknown", (f"the Pluto's spectrum does not show the beacon where it should be ({seen}; "
                           f"wanted within {PLUTO_BEACON_WINDOW_MHZ * 1000:.0f} kHz of {QO100_BEACON_MHZ} MHz, "
                           f"{PLUTO_BEACON_MIN_DB:g} dB up)")
    drift = centre - QO100_BEACON_MHZ
    signals = qo100_detect(pluto_to_batc(bins, low - drift, high - drift))[0]
    busy = qo100_in_use(signals, down, half)
    if busy:
        return "busy", busy
    return "clear", f"{down:.3f} MHz is clear on your own receiver (LNB {drift * 1000:+.0f} kHz off)"
