"""DVB-S and DVB-S2 arithmetic: the net bit rate a setting carries, how that
is shared between the mux, audio and video, the picture size Auto picks,
and the MER each MODCOD needs.

net_bitrate is held to three vectors in test_openhamshack.py (Rates); HANDOVER's
"Bitrate math" has the formulas. Which MODCOD to suggest (best_modcod,
link_advice) stays in openhamshack.py, because it depends on the firmware found
on the Pluto.
"""

from __future__ import annotations

KBCH_LONG = {
    "1/4": 16008, "1/3": 21408, "2/5": 25728, "1/2": 32208, "3/5": 38688,
    "2/3": 43040, "3/4": 48408, "4/5": 51648, "5/6": 53840, "8/9": 57472, "9/10": 58192,
}
KBCH_SHORT = {
    "1/4": 3072, "1/3": 5232, "2/5": 6312, "1/2": 7032, "3/5": 9552,
    "2/3": 10632, "3/4": 11712, "4/5": 12432, "5/6": 13152, "8/9": 14232,
}
ETA = {"qpsk": 2, "8psk": 3, "16apsk": 4, "32apsk": 5}
# DVB-S2 MODCOD numbers as longmynd reports them in status field 18.
MODCODS = {i + 1: m for i, m in enumerate(
    [("QPSK", f) for f in ("1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10")]
    + [("8PSK", f) for f in ("3/5", "2/3", "3/4", "5/6", "8/9", "9/10")]
    + [("16APSK", f) for f in ("2/3", "3/4", "4/5", "5/6", "8/9", "9/10")]
    + [("32APSK", f) for f in ("3/4", "4/5", "5/6", "8/9", "9/10")])}
# DVB-S has no MODCOD field; longmynd reports the Viterbi puncture rate in $3
# as the numerator n of the code rate n/(n+1) (stv0910_read_puncture_rate).
PUNCTURE_RATES = {1: "1/2", 2: "2/3", 3: "3/4", 5: "5/6", 6: "6/7", 7: "7/8"}


def net_bitrate(cfg: dict) -> int:
    sr = int(round(float(cfg["symbolRateKs"]) * 1000))
    if sr <= 0:
        return 0
    mode = cfg["mode"]
    fec = str(cfg["fec"])
    if mode == "dvbs":
        n, d = (int(x) for x in fec.split("/"))
        return (sr * 2 * n * 188) // (d * 204)
    frame = cfg["frame"]
    table = KBCH_LONG if frame == "long" else KBCH_SHORT
    if frame == "short" and fec == "9/10":
        fec = "8/9"
    kbch = table.get(fec)
    if not kbch:
        return 0
    constellation = "qpsk" if mode == "dvbs" else cfg["constellation"]
    nldpc = 64800 if frame == "long" else 16200
    data = nldpc // ETA[constellation]
    slots = data // 90
    pilots = mode == "dvbs2" and bool(cfg.get("pilots"))
    pilot_syms = ((slots - 1) // 16) * 36 if pilots else 0
    total = 90 + data + pilot_syms
    return ((kbch - 80) * sr) // total


# DVB-S2 ideal Es/N0 for quasi-error-free reception (ETSI EN 302 307-1,
# table 13, normal frames, AWGN). A link needs these plus margin.
S2_ESN0 = {
    ("qpsk", "1/4"): -2.35, ("qpsk", "1/3"): -1.24, ("qpsk", "2/5"): -0.30, ("qpsk", "1/2"): 1.00,
    ("qpsk", "3/5"): 2.23, ("qpsk", "2/3"): 3.10, ("qpsk", "3/4"): 4.03, ("qpsk", "4/5"): 4.68,
    ("qpsk", "5/6"): 5.18, ("qpsk", "8/9"): 6.20, ("qpsk", "9/10"): 6.42,
    ("8psk", "3/5"): 5.50, ("8psk", "2/3"): 6.62, ("8psk", "3/4"): 7.91, ("8psk", "5/6"): 9.35,
    ("8psk", "8/9"): 10.69, ("8psk", "9/10"): 10.98,
    ("16apsk", "2/3"): 8.97, ("16apsk", "3/4"): 10.21, ("16apsk", "4/5"): 11.03, ("16apsk", "5/6"): 11.61,
    ("16apsk", "8/9"): 12.89, ("16apsk", "9/10"): 13.13,
    ("32apsk", "3/4"): 12.73, ("32apsk", "4/5"): 13.64, ("32apsk", "5/6"): 14.28,
    ("32apsk", "8/9"): 15.69, ("32apsk", "9/10"): 16.05,
}


# DVB-S (QPSK, Viterbi + Reed-Solomon) MER for quasi-error-free reception, as
# the BATC receivers (Ryde, Portsdown) use against a MiniTiouner's MER. The
# EN 300 421 figures are 1.5-2.5 dB higher, but they carry an implementation
# margin and a stricter error target; these are what a MiniTiouner actually
# decodes at, which is what advice to the other station has to be based on.
S1_MER = {"1/2": 1.7, "2/3": 3.3, "3/4": 4.2, "5/6": 5.1, "6/7": 5.5, "7/8": 5.8}
# The symbol rates DATV stations use (BATC's list, reduced-bandwidth included):
# advice only ever names one of these, so the other station has it on a button.
DATV_SYMBOL_RATES = (4000, 3000, 2000, 1500, 1000, 500, 333, 250, 125, 92, 66, 33)
# Below ADVICE_OK_DB of margin over the threshold a 70 cm path drops out on
# the next fade; suggestions aim a little higher, at ADVICE_TARGET_DB, so
# that taking one lands clearly inside "comfortable" rather than on its edge.
ADVICE_OK_DB = 2.5
ADVICE_TARGET_DB = 3.0


def fec_choices() -> dict:
    """The code rates each mode and modulation really has, in rate order, for
    the console's FEC row. DVB-S2 defines 8PSK, 16APSK and 32APSK only at
    some rates (EN 302 307-1, table 13: the same table as S2_ESN0, so the
    two cannot disagree); the console used to offer all eleven with every
    modulation, and 8PSK 1/4 is a setting no receiver can decode."""
    order = list(KBCH_LONG)
    s2: dict = {}
    for (const, fec) in S2_ESN0:
        s2.setdefault(const, []).append(fec)
    return {"dvbs": ["1/2", "2/3", "3/4", "5/6", "7/8"],
            "dvbs2": {c: sorted(v, key=order.index) for c, v in s2.items()}}


def modcod_needs() -> dict:
    """The MER each offered setting needs at the far end, for the console's
    "more robust ... more picture" line in the Transmit mode window: the
    same figures the margin and the advice are worked out from."""
    ch = fec_choices()
    return {"dvbs": {f: S1_MER[f] for f in ch["dvbs"]},
            "dvbs2": {c: {f: S2_ESN0[(c, f)] for f in fecs} for c, fecs in ch["dvbs2"].items()}}


def mer_needed(mode: str, constellation: str, fec: str) -> float | None:
    if mode == "dvbs":
        return S1_MER.get(fec) if constellation == "qpsk" else None
    return S2_ESN0.get((constellation, fec))


def low_mer(readings: list[float], fraction: float = 0.1) -> float | None:
    """The MER the link is at for all but its worst tenth of the time. Not
    the mean: a picture breaks up on the dips, and a mean hides them; nor the
    single minimum, which one bad reading would set."""
    if not readings:
        return None
    ordered = sorted(readings)
    return ordered[int(fraction * (len(ordered) - 1))]


def picture(video_bps: int) -> tuple[int, int, int]:
    # 4K from 5 Mbit/s: measured on air, 3000 kS 8PSK 3/4 carried 3840x2160
    # at 5.9 Mbit/s cleanly; below that 1080p looks better than starved 4K.
    if video_bps >= 5_000_000:
        return 3840, 2160, 25
    if video_bps < 180_000:
        return 640, 360, 15
    if video_bps < 450_000:
        return 768, 432, 15
    if video_bps < 1_200_000:
        return 1280, 720, 25
    return 1920, 1080, 25


def tx_plan(cfg: dict) -> dict:
    """Mux rate, video bitrate and picture for the current settings. Shared by
    the encoder command and /api/status, so the console shows what Auto picks."""
    net = net_bitrate(cfg)
    audio = int(cfg["audioKbps"]) * 1000
    head = min(20, max(1, int(cfg["headroomPct"])))
    mux = (net * (100 - head)) // 100
    # Measured on air (workshop, 3 Oct 2026): with video = mux - audio - 12k
    # the stream needed ~434 kbit/s against a 412.6k mux. TS packet headers,
    # PES headers, PCR adaptation fields and the tables cost ~5% on top of the
    # elementary streams, and the VA-API encoder overshoots a little. The mux
    # then fell behind real time (DTS up to 4 s behind PCR) and the receiver
    # stalled for ~0.5 s every few seconds. Leave room for all of it.
    # Tightened after captures showed ~3% of our own stream as padding on top
    # of the headroom: 0.95 still kept every frame ahead of its DTS.
    video = max(40_000, int((mux - audio * 1.08 - 20_000) * 0.95))
    if cfg.get("autoSize", True):
        w, h, fps = picture(video)
    else:
        w, h, fps = int(cfg["width"]), int(cfg["height"]), int(cfg["fps"])
    return {"mux": mux, "video": video, "audio": audio, "width": w, "height": h, "fps": fps}
