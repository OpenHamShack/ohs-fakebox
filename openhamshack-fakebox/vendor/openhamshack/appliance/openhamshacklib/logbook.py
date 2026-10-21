"""The logbook's rules and formats: what a contact may hold (clean_qso),
finding finished contacts in the signal log (rx_sessions), signal reports
read from the chat, the file written whole and read back with its .bak
(logbook_write / logbook_read), and the ADIF 3.1.6 and CSV exports.

openhamshack.py's Logbook sections keep the log's files, the suggestions and
the activity watch, and serve /api/log. HANDOVER's "Logbook" has why ADIF
says MODE=ATV with no submode.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

from .bands import mhz
from .qo100 import CALLSIGN_RE, chat_clean, service_callsign
from .settings import callsign


SESSION_GAP = 120.0        # lock lost for less than this is the same contact (fading, a dish nudged)
SESSION_MIN = 20           # seconds of lock before it is worth offering: a station tuned past is not a QSO
SESSION_TX_MARGIN = 120.0  # a transmission this close to a session belongs to it
REPORT_WINDOW = (-120.0, 600.0)   # chat reports this long before/after a session belong to it
LOG_TEXT_MAX = {"name": 40, "qth": 60, "rstSent": 40, "rstRcvd": 40, "notes": 500, "service": 40}
DATV_STANDARDS = ("DVB-S", "DVB-S2")
DATV_MODULATIONS = ("QPSK", "8PSK", "16APSK", "32APSK")
DATV_CODECS = ("H.264", "H.265", "MPEG-2")
QO100_SAT = "QO-100"
# ADIF 3.1.6's Band enumeration (lower and upper edges in MHz), the parts a
# DATV station can use. 23 cm stops at 1300 in ADIF although the UK band runs
# to 1325: a frequency above 1300 is exported with FREQ and no BAND rather
# than with a band the specification says it is not in.
ADIF_BANDS = (("6m", 50.0, 54.0), ("4m", 70.0, 71.0), ("2m", 144.0, 148.0), ("1.25m", 222.0, 225.0),
              ("70cm", 420.0, 450.0), ("33cm", 902.0, 928.0), ("23cm", 1240.0, 1300.0),
              ("13cm", 2300.0, 2450.0), ("9cm", 3300.0, 3500.0), ("6cm", 5650.0, 5925.0),
              ("3cm", 10000.0, 10500.0), ("1.25cm", 24000.0, 24250.0), ("6mm", 47000.0, 47200.0),
              ("4mm", 75500.0, 81000.0))
# A report in the room's own form (OpenTuner's SigReport, which this box's
# Send report also uses): "SigReport: CALL[/provider] - D<margin> - (<MER> dB) ..."
SIGREPORT_RE = re.compile(r"SigReport:\s*([A-Z0-9]+(?:/[A-Z0-9]{1,4})?)\S*\s*-\s*D(-?\d+(?:\.\d+)?)\s*-\s*\((-?\d+(?:\.\d+)?)\s*dB\)", re.I)
REPORT_D_RE = re.compile(r"(?<![A-Z])D\s?(-?\d+(?:\.\d+)?)", re.I)
REPORT_MER_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s?dB", re.I)


def band_for(mhz) -> str | None:
    try:
        f = float(mhz)
    except (TypeError, ValueError):
        return None
    return next((name for name, lo, hi in ADIF_BANDS if lo <= f <= hi), None)


def is_qo100(tx_mhz=None, rx_mhz=None) -> bool:
    """QO-100's wideband transponder: uplink 2401.5-2409.5, downlink
    10491-10499 (a little either side for the beacon and LNB drift)."""
    def within(v, lo, hi) -> bool:
        try:
            return lo <= float(v) <= hi
        except (TypeError, ValueError):
            return False
    return within(tx_mhz, 2400.0, 2410.0) or within(rx_mhz, 10489.0, 10500.0)


def datv_mode_text(m: dict | None) -> str:
    """"DVB-S2 8PSK 3/4 333 kS H.265", leaving out what is not known."""
    if not m:
        return ""
    sr = m.get("srKs")
    return " ".join(str(x) for x in (m.get("standard"), m.get("modulation"), m.get("fec"),
                                      f"{sr:g} kS" if isinstance(sr, (int, float)) and sr else None,
                                      m.get("codec")) if x)


def utc_hm(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t))


def report_numbers(text: str) -> tuple[float | None, float | None]:
    """(MER dB, D) read out of a free-text report such as "D4.3 (8.6 dB)",
    "8.6dB D4.3" or "D3": DATV reports are given that way rather than as
    RST, and the numbers are kept so the log can be compared and exported
    as data. The text is what the operator wrote and stays the record."""
    text = str(text or "")
    d = REPORT_D_RE.search(text)
    mer = REPORT_MER_RE.search(text)
    return (float(mer.group(1)) if mer else None, float(d.group(1)) if d else None)


def report_text(mer: float | None, d: float | None) -> str:
    if d is not None and mer is not None:
        return f"D{d:.1f} ({mer:.1f} dB)"
    if mer is not None:
        return f"{mer:.1f} dB"
    return f"D{d:.1f}" if d is not None else ""


def rx_sessions(samples: list[dict], rid: int = 1, gap: float = SESSION_GAP, min_locked: int = SESSION_MIN) -> list[dict]:
    """Contiguous lock on one station, from one receiver's signal-log
    samples. A gap shorter than `gap` (unlocked, no samples, or another
    station decoded in between) does not end a session; a station without
    a callsign at the head of its service name is not one anyone can log;
    under min_locked seconds of lock is a station tuned past.

    MER is per-second minima (rx_sample keeps the dips), so "best" is the
    best of those - what the link held, not one lucky reading. The best
    moment is the one with the most margin over its MODCOD's threshold,
    and the session's frequency and mode are that moment's."""
    open_: dict[str, dict] = {}
    done: list[dict] = []
    for s in sorted((s for s in samples if isinstance(s.get("t"), (int, float))), key=lambda s: s["t"]):
        if not s.get("locked"):
            continue
        call = service_callsign(s.get("name") or "")
        if not call:
            continue
        ses = open_.get(call)
        if ses is not None and s["t"] - ses["end"] > gap:
            done.append(ses)
            ses = None
        if ses is None:
            ses = open_[call] = {"rx": rid, "call": call, "service": chat_clean(s.get("name") or "")[:40],
                                 "start": s["t"], "end": s["t"], "locked": 0, "best": None, "mers": []}
        ses["end"] = s["t"]
        ses["locked"] += 1
        mer = s.get("mer")
        if isinstance(mer, (int, float)):
            ses["mers"].append(mer)
            need = s.get("needDb")
            score = (1, mer - need) if isinstance(need, (int, float)) else (0, mer)
            if ses["best"] is None or score > ses["best"][0]:
                ses["best"] = (score, s)
        elif ses["best"] is None:
            ses["best"] = ((-1, 0.0), s)
    done.extend(open_.values())
    out = []
    for ses in done:
        if ses["locked"] < min_locked:
            continue
        best = ses.pop("best")[1]
        mers = sorted(ses.pop("mers"))
        need = best.get("needDb")
        mer = best.get("mer") if isinstance(best.get("mer"), (int, float)) else None
        modcod = str(best.get("modcod") or "").split()
        ses.update(bestMer=mer,
                   bestD=round(mer - need, 1) if mer is not None and isinstance(need, (int, float)) else None,
                   medianMer=mers[len(mers) // 2] if mers else None, freqMHz=best.get("freqMHz"),
                   mode={"standard": {"dvbs": "DVB-S", "dvbs2": "DVB-S2"}.get(best.get("mode")),
                         "modulation": modcod[0] if modcod else None, "fec": modcod[1] if len(modcod) > 1 else None,
                         "srKs": best.get("srKs") or None, "codec": best.get("codec") or None})
        ses["id"] = f"rx{rid}-{ses['call']}-{int(ses['start'])}"
        out.append(ses)
    out.sort(key=lambda s: s["start"])
    return out


def logbook_write(path: Path, data: dict) -> None:
    """Whole-file replace that a power cut cannot leave half-written: the
    new copy is written and synced beside the old one, the old one becomes
    .bak, then the new one takes its name. A reader finds the new file or
    (between the two renames) only the .bak, which logbook_read falls back
    to; never a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        os.replace(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def logbook_read(path: Path, default: dict) -> dict:
    for p in (path, path.with_name(path.name + ".bak")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            print(f"logbook: {p.name} unreadable ({exc}); trying the backup", flush=True)
            continue
        if isinstance(data, dict):
            return data
    return json.loads(json.dumps(default))


def tx_mode_from(cfg: dict) -> dict:
    return {"standard": "DVB-S2" if cfg.get("mode") == "dvbs2" else "DVB-S",
            "modulation": str(cfg.get("constellation") or "qpsk").upper(), "fec": str(cfg.get("fec") or "") or None,
            "srKs": float(cfg.get("symbolRateKs") or 0) or None,
            "codec": {"hevc": "H.265", "h264": "H.264"}.get(str(cfg.get("codec")))}


def overlaps(a: dict, b: dict) -> bool:
    return (a.get("rx") == b.get("rx") and a.get("call") == b.get("call")
            and a.get("start", 0) <= b.get("end", 0) + SESSION_GAP and b.get("start", 0) <= a.get("end", 0) + SESSION_GAP)


def clean_mode(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    std = raw.get("standard") if raw.get("standard") in DATV_STANDARDS else None
    mod = str(raw.get("modulation") or "").upper()
    fec = str(raw.get("fec") or "")
    sr = raw.get("srKs")
    if sr not in (None, ""):
        try:
            sr = round(float(sr), 1)
        except (TypeError, ValueError):
            raise ValueError("symbol rate must be a number of kS")
        if not 1 <= sr <= 50000:
            raise ValueError("symbol rate must be 1-50000 kS")
    out = {"standard": std, "modulation": mod if mod in DATV_MODULATIONS else None,
           "fec": fec if re.fullmatch(r"\d{1,2}/\d{1,2}", fec) else None, "srKs": sr if sr not in (None, "") else None,
           "codec": raw.get("codec") if raw.get("codec") in DATV_CODECS else None}
    return out if any(v is not None for v in out.values()) else None


def log_text(raw, key: str) -> str:
    """One line of printable text (notes may have line breaks), cut to its
    field's length."""
    s = str(raw or "")
    if key == "notes":
        lines = ["".join(ch for ch in line if ch.isprintable()).strip() for line in s.replace("\r", "").split("\n")]
        return "\n".join(lines).strip()[:LOG_TEXT_MAX[key]]
    return chat_clean(s)[:LOG_TEXT_MAX[key]]


def clean_qso(raw, old: dict | None = None, now: float | None = None) -> dict:
    """A log entry as stored, from what a browser sent: every field checked
    and typed here, so the file only ever holds what the export and the
    page expect. ValueError says what is wrong in the operator's terms."""
    import secrets
    if not isinstance(raw, dict):
        raise ValueError("no entry sent")
    now = time.time() if now is None else now
    call = re.sub(r"\s+", "", str(raw.get("call") or "")).upper()
    if not re.fullmatch(r"[A-Z0-9/]{3,15}", call) or not re.search(r"\d", call) or not re.search(r"[A-Z]", call):
        raise ValueError("their callsign is missing or not a callsign")

    def num(key: str, lo: float, hi: float, what: str) -> float | None:
        v = raw.get(key)
        if v in (None, ""):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what} must be a number")
        if not lo <= f <= hi:
            raise ValueError(f"{what} must be between {lo:g} and {hi:g}")
        return f

    on = num("on", 0, 4102444800, "start time")
    if on is None:
        raise ValueError("the start time is missing")
    off = num("off", 0, 4102444800, "end time")
    if off is not None and off < on:
        raise ValueError("the end time is before the start")
    if off is not None and off - on > 86400:
        raise ValueError("a contact of more than a day: check the end time")
    freq = num("freqMHz", 1, 300000, "your transmit frequency")
    freq_rx = num("freqRxMHz", 1, 300000, "your receive frequency")
    band = raw.get("band") if any(raw.get("band") == b[0] for b in ADIF_BANDS) else None
    if freq is None and freq_rx is None and band is None:
        raise ValueError("give a frequency or a band")
    locator = re.sub(r"\s+", "", str(raw.get("locator") or ""))
    if locator:
        locator = locator[:2].upper() + locator[2:4] + locator[4:6].lower() + locator[6:8] + locator[8:10].lower()
        if len(locator) > 10 or not re.fullmatch(r"[A-R]{2}(\d\d([a-x]{2}(\d\d([a-x]{2})?)?)?)?", locator):
            raise ValueError("their locator must be a Maidenhead square, e.g. IO85 or IO85jw")
    rst_sent, rst_rcvd = log_text(raw.get("rstSent"), "rstSent"), log_text(raw.get("rstRcvd"), "rstRcvd")
    mer_s, d_s = report_numbers(rst_sent)
    mer_r, d_r = report_numbers(rst_rcvd)
    sat = bool(raw.get("sat"))
    return {"id": (old or {}).get("id") or secrets.token_hex(6), "call": call,
            "on": round(on), "off": round(off) if off is not None else None,
            "freqMHz": freq, "freqRxMHz": freq_rx,
            "band": band_for(freq if freq is not None else freq_rx) or (band if freq is None and freq_rx is None else None),
            "bandRx": band_for(freq_rx) if freq is not None else None,
            "rxMode": clean_mode(raw.get("rxMode")), "txMode": clean_mode(raw.get("txMode")),
            "rstSent": rst_sent, "rstRcvd": rst_rcvd, "merSent": mer_s, "dSent": d_s, "merRcvd": mer_r, "dRcvd": d_r,
            "name": log_text(raw.get("name"), "name"), "qth": log_text(raw.get("qth"), "qth"), "locator": locator,
            "sat": sat, "satName": QO100_SAT if sat else "",
            "gainDb": num("gainDb", -90, 10, "transmit gain"), "powerW": num("powerW", 0, 10000, "power"),
            "notes": log_text(raw.get("notes"), "notes"), "service": log_text(raw.get("service"), "service"),
            "source": (old or {}).get("source") or (raw.get("source") if raw.get("source") in ("suggested", "manual") else "manual"),
            "created": (old or {}).get("created") or round(now, 1), "updated": round(now, 1)}


def log_stats(qsos: list[dict]) -> dict:
    bands: dict[str, int] = {}
    for q in qsos:
        b = q.get("band") or "other"
        bands[b] = bands.get(b, 0) + 1
    order = {b[0]: i for i, b in enumerate(ADIF_BANDS)}
    return {"qsos": len(qsos), "calls": len({q.get("call") for q in qsos}),
            "bands": [{"band": b, "count": n} for b, n in sorted(bands.items(), key=lambda kv: order.get(kv[0], 99))],
            "sat": sum(1 for q in qsos if q.get("sat"))}


# ADIF mode for DATV. ADIF 3.1.6 (adif.org/316, 2025-09-15), section III.B.10,
# the Mode enumeration, has ATV with no submodes, and nothing for DVB-S/S2
# or digital ATV at all (DIGITALVOICE's submodes are C4FM, DMR, DSTAR,
# FREEDV, M17: voice, not television). So MODE is ATV and SUBMODE is left
# out: a submode outside the enumeration fails import into loggers and LoTW
# that check it, while ATV is accepted everywhere and is what DATV operators
# already log. What makes it digital - standard, modulation, FEC, symbol
# rate, codec - goes in COMMENT, which ADIF recommends for what is of
# interest to the other operator, and in APP_OPENHAMSHACK_* fields for a program
# that wants it as data. Reports: RST_SENT and RST_RCVD are String fields,
# so the DATV report goes there as given ("D4.3 (8.6 dB)"); the MER and D
# go out as numbers too (APP_OPENHAMSHACK_MER_SENT and so on).
ADIF_VERSION = "3.1.6"
ADIF_GRID_RE = re.compile(r"(?i)[A-R]{2}(\d\d([A-X]{2}(\d\d)?)?)?")


def adif_ascii(s, multiline: bool = False) -> str:
    """ADIF String is ASCII 32-126 (MultilineString adds CR LF); the Intl
    variants exist only in ADX. Accented letters lose their accents rather
    than the letter; anything else becomes '?'."""
    import unicodedata
    out = []
    for ch in unicodedata.normalize("NFKD", str(s)):
        if unicodedata.combining(ch) or ch == "\r":
            continue
        if ch == "\n":
            out.append("\r\n" if multiline else " ")
        elif 32 <= ord(ch) <= 126:
            out.append(ch)
        else:
            out.append(" " if ch == "\t" else "?")
    return "".join(out)


def adif_field(name: str, value, kind: str = "") -> str:
    """<NAME:len[:T]>value. The length counts characters, which for ADIF's
    ASCII-only types is also bytes; values are made ASCII first so the two
    can never differ."""
    if value is None or value == "":
        return ""
    v = adif_ascii(value, multiline=kind == "M")
    return f"<{name}:{len(v)}{':' + kind if kind else ''}>{v} "


def adif_mhz(f) -> str:
    return f"{float(f):.6f}".rstrip("0").rstrip(".")


def adif_record(q: dict, cfg: dict) -> str:
    on, off = q.get("on"), q.get("off")
    tx_f, rx_f = q.get("freqMHz"), q.get("freqRxMHz")
    # FREQ is the QSO frequency, the transmit one when the QSO crosses
    # bands; FREQ_RX/BAND_RX are for a split QSO. A contact with nothing
    # sent (a report only) has its receive frequency as FREQ.
    freq = tx_f if tx_f is not None else rx_f
    split = tx_f is not None and rx_f is not None
    rx_mode, tx_mode = datv_mode_text(q.get("rxMode")), datv_mode_text(q.get("txMode"))
    comment = "; ".join(x for x in ("DATV", f"rx {rx_mode}" if rx_mode else "", f"tx {tx_mode}" if tx_mode else "") if x)
    call = callsign(cfg)
    loc = re.sub(r"\s+", "", str(cfg.get("locator") or ""))[:8]
    out = [adif_field("CALL", q["call"]),
           adif_field("QSO_DATE", time.strftime("%Y%m%d", time.gmtime(on))),
           adif_field("TIME_ON", time.strftime("%H%M%S", time.gmtime(on)))]
    if off is not None:
        out += [adif_field("QSO_DATE_OFF", time.strftime("%Y%m%d", time.gmtime(off))),
                adif_field("TIME_OFF", time.strftime("%H%M%S", time.gmtime(off)))]
    out += [adif_field("BAND", band_for(freq) if freq is not None else q.get("band")),
            adif_field("FREQ", adif_mhz(freq) if freq is not None else None, "N"),
            adif_field("BAND_RX", band_for(rx_f) if split else None),
            adif_field("FREQ_RX", adif_mhz(rx_f) if split else None, "N"),
            adif_field("MODE", "ATV"),
            adif_field("RST_SENT", q.get("rstSent")), adif_field("RST_RCVD", q.get("rstRcvd"))]
    if q.get("sat"):
        out += [adif_field("PROP_MODE", "SAT"), adif_field("SAT_NAME", QO100_SAT)]
    out += [adif_field("NAME", q.get("name")), adif_field("QTH", q.get("qth")),
            adif_field("GRIDSQUARE", (q.get("locator") or "")[:8]),
            adif_field("TX_PWR", f"{q['powerW']:g}" if q.get("powerW") is not None else None, "N"),
            adif_field("STATION_CALLSIGN", call if CALLSIGN_RE.fullmatch(call) else None),
            adif_field("MY_GRIDSQUARE", loc if loc and ADIF_GRID_RE.fullmatch(loc) else None),
            adif_field("COMMENT", comment), adif_field("NOTES", q.get("notes"), "M"),
            adif_field("APP_OPENHAMSHACK_RX_MODE", rx_mode, "S"), adif_field("APP_OPENHAMSHACK_TX_MODE", tx_mode, "S")]
    for key, name in (("merSent", "MER_SENT"), ("dSent", "D_SENT"), ("merRcvd", "MER_RCVD"), ("dRcvd", "D_RCVD"),
                      ("gainDb", "GAIN_DB")):
        if q.get(key) is not None:
            out.append(adif_field(f"APP_OPENHAMSHACK_{name}", f"{q[key]:g}", "N"))
    out.append(adif_field("APP_OPENHAMSHACK_SERVICE", q.get("service"), "S"))
    return "".join(out).rstrip() + " <EOR>\r\n"


CSV_COLUMNS = (("date_utc", None), ("time_on_utc", None), ("time_off_utc", None), ("call", "call"),
               ("band", "band"), ("tx_mhz", "freqMHz"), ("rx_mhz", "freqRxMHz"), ("rx_mode", None),
               ("tx_mode", None), ("rst_sent", "rstSent"), ("rst_rcvd", "rstRcvd"), ("mer_sent_db", "merSent"),
               ("d_sent", "dSent"), ("mer_rcvd_db", "merRcvd"), ("d_rcvd", "dRcvd"), ("name", "name"),
               ("qth", "qth"), ("locator", "locator"), ("satellite", "satName"), ("gain_db", "gainDb"),
               ("power_w", "powerW"), ("notes", "notes"), ("service", "service"), ("source", "source"))


def csv_cell(v) -> str:
    """Numbers as numbers; text a spreadsheet would run as a formula (= + -
    @ at the start) gets a leading apostrophe, so a name or a note typed on
    a remote browser cannot become one when the file is opened."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, (int, float)):
        return f"{v:g}"
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_export(qsos: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([c for c, _ in CSV_COLUMNS])
    for q in sorted(qsos, key=lambda q: q.get("on") or 0):
        on, off = q.get("on"), q.get("off")
        derived = {"date_utc": time.strftime("%Y-%m-%d", time.gmtime(on)),
                   "time_on_utc": time.strftime("%H:%M:%S", time.gmtime(on)),
                   "time_off_utc": time.strftime("%H:%M:%S", time.gmtime(off)) if off is not None else "",
                   "rx_mode": datv_mode_text(q.get("rxMode")), "tx_mode": datv_mode_text(q.get("txMode"))}
        w.writerow([derived[c] if key is None else csv_cell(q.get(key)) for c, key in CSV_COLUMNS])
    # A byte-order mark: Excel otherwise reads UTF-8 (a name with an accent)
    # in the local code page.
    return "﻿" + buf.getvalue()
